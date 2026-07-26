# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Durable, plan-bound quarantine orchestration for the Qt-free service API.

``core.safe_action`` owns the low-level proof and filesystem transition rules.
This module adds the batch contract needed by the CLI: every action is
preflighted before the first target moves, operation plans are atomically
persisted, and staged files can be listed, restored, or finalized later.
"""

from __future__ import annotations

import json
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core import fs as core_fs
from core.file_identity import FileIdentityError, get_file_identity
from core.safe_action import (
    HASH_ALGORITHM as SAFE_PROOF_HASH_ALGORITHM,
    LIFECYCLE_JOURNAL_RESERVE_EVENTS,
    MAX_JOURNAL_LINE_BYTES,
    ActionResult as SafeActionResult,
    AppendOnlyJournal,
    FailureCode,
    FileIdentity as SafeFileIdentity,
    JournalError,
    OperationPlan,
    SafeActionExecutor,
    build_operation_plan,
    cleanup_created_regular_file,
    platform_file_system,
)
from core.safe_json import DATASET_DOCUMENT_JSON_LIMITS, strict_bounded_json_loads

if TYPE_CHECKING:
    from core.services.models import DeletionPlan, FileRecord


QUARANTINE_DIRECTORY_NAME = ".dupeguru-neo-quarantine"
OPERATION_DIRECTORY_NAME = "operations"
OPERATION_PLAN_FILENAME = "operation.json"
OPERATION_JOURNAL_FILENAME = "journal.jsonl"
STORED_OPERATION_SCHEMA = "dupeguru.quarantine-operation"
STORED_OPERATION_SCHEMA_VERSION = 1
MAX_STORED_OPERATION_BYTES = 16 * 1024 * 1024
MAX_STORED_OPERATIONS = 250_000
_OPERATION_NAMESPACE = uuid.UUID("7513ab59-b899-4bd4-8e24-15d18d76dbd4")


class QuarantineError(RuntimeError):
    """Raised when quarantine metadata cannot be trusted or persisted."""


@dataclass(frozen=True)
class StoredOperation:
    service_plan_id: str
    action_id: str
    operation: str
    operation_plan: OperationPlan

    def __post_init__(self) -> None:
        if self.operation != "quarantine":
            raise QuarantineError("stored operation is not a recoverable quarantine operation")

    @property
    def operation_plan_fingerprint(self) -> str:
        return self.operation_plan.fingerprint

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": STORED_OPERATION_SCHEMA,
            "schema_version": STORED_OPERATION_SCHEMA_VERSION,
            "service_plan_id": self.service_plan_id,
            "action_id": self.action_id,
            "operation": self.operation,
            "operation_plan_fingerprint": self.operation_plan_fingerprint,
            "operation_plan": self.operation_plan.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StoredOperation":
        expected = {
            "schema",
            "schema_version",
            "service_plan_id",
            "action_id",
            "operation",
            "operation_plan_fingerprint",
            "operation_plan",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise QuarantineError("stored operation has unexpected or missing fields")
        if raw["schema"] != STORED_OPERATION_SCHEMA:
            raise QuarantineError("stored operation has an unsupported schema")
        if raw["schema_version"] != STORED_OPERATION_SCHEMA_VERSION:
            raise QuarantineError("stored operation has an unsupported schema version")
        service_plan_id = _require_sha256(raw["service_plan_id"], "service_plan_id")
        action_id = _require_sha256(raw["action_id"], "action_id")
        operation = raw["operation"]
        if operation != "quarantine":
            raise QuarantineError("stored operation is not a recoverable quarantine operation")
        operation_plan_raw = raw["operation_plan"]
        if not isinstance(operation_plan_raw, dict):
            raise QuarantineError("operation_plan must be an object")
        try:
            operation_plan = OperationPlan.from_dict(operation_plan_raw)
        except (TypeError, ValueError) as error:
            raise QuarantineError("invalid operation_plan: {}".format(error)) from error
        expected_id = operation_plan_id(service_plan_id, action_id)
        if operation_plan.plan_id != expected_id:
            raise QuarantineError("operation plan ID is not bound to its service plan and action")
        fingerprint = raw["operation_plan_fingerprint"]
        if not isinstance(fingerprint, str) or fingerprint != operation_plan.fingerprint:
            raise QuarantineError("operation plan fingerprint does not match its live proof document")
        return cls(
            service_plan_id=service_plan_id,
            action_id=action_id,
            operation=operation,
            operation_plan=operation_plan,
        )


@dataclass(frozen=True)
class _StoredActionView:
    action_id: str
    operation: str
    target: Any


@dataclass(frozen=True)
class _ProofRecordView:
    path: str
    size: int
    mtime_ns: int
    digest_algorithm: str
    digest: str
    volume_id: str
    file_id: str


@dataclass(frozen=True)
class PreparedOperation:
    action: Any
    stored: StoredOperation
    plan_path: Path


@dataclass(frozen=True)
class PreparationFailure:
    action_id: str
    target: str
    code: str
    message: str


@dataclass(frozen=True)
class PreparationBatch:
    service_plan_id: str
    prepared: Tuple[PreparedOperation, ...]
    failures: Tuple[PreparationFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.prepared)


@dataclass(frozen=True)
class ManagedResult:
    action_id: str
    target: str
    status: str
    safe_state: str
    failure_code: str
    message: str
    changed: bool
    operation_plan_path: str
    quarantine_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "target": self.target,
            "status": self.status,
            "safe_state": self.safe_state,
            "failure_code": self.failure_code,
            "message": self.message,
            "changed": self.changed,
            "operation_plan_path": self.operation_plan_path,
            "quarantine_path": self.quarantine_path,
        }


def operation_plan_id(service_plan_id: str, action_id: str) -> str:
    _require_sha256(service_plan_id, "service_plan_id")
    _require_sha256(action_id, "action_id")
    return str(uuid.uuid5(_OPERATION_NAMESPACE, "{}:{}".format(service_plan_id, action_id)))


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise QuarantineError("{} must be a SHA-256 hex string".format(label))
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise QuarantineError("{} must be a SHA-256 hex string".format(label)) from error
    return value.lower()


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normalized(candidate), _normalized(root))) == _normalized(root)
    except ValueError:
        return False


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _path_components(path: Path) -> Iterable[Path]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        yield current


def _created_ns(created_at: str) -> int:
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise QuarantineError("deletion plan created_at is not an ISO-8601 timestamp") from error
    seconds = int(parsed.timestamp())
    return seconds * 1_000_000_000 + parsed.microsecond * 1_000


@dataclass(frozen=True)
class _IndexedRoot:
    path: Path
    resolved: Path


class _RootTrieNode:
    __slots__ = ("children", "root")

    def __init__(self) -> None:
        self.children: Dict[str, "_RootTrieNode"] = {}
        self.root: Optional[_IndexedRoot] = None


class _AllowedRootIndex:
    """Component trie for deepest-ancestor lookup independent of root count."""

    def __init__(self, roots: Sequence[_IndexedRoot]):
        self.roots = tuple(roots)
        self._trie = _RootTrieNode()
        for root in self.roots:
            node = self._trie
            for component in self._parts(root.path):
                node = node.children.setdefault(component, _RootTrieNode())
            node.root = root

    @staticmethod
    def _parts(path: Path) -> Tuple[str, ...]:
        absolute = Path(os.path.abspath(os.fspath(path)))
        return tuple(os.path.normcase(component) for component in absolute.parts)

    @classmethod
    def build(
        cls,
        root_values: Sequence[str],
        manager: "QuarantineManager",
    ) -> "_AllowedRootIndex":
        indexed: List[_IndexedRoot] = []
        seen = set()
        for root_value in root_values:
            root = Path(os.path.abspath(root_value))
            key = _normalized(root)
            if key in seen:
                continue
            seen.add(key)
            manager._validate_plain_directory(root)
            resolved = manager.fs.resolve(root, strict=True)
            indexed.append(_IndexedRoot(root, resolved))
        if not indexed:
            raise QuarantineError("deletion plan has no allowed roots")
        return cls(indexed)

    def select(self, path: Path) -> _IndexedRoot:
        node = self._trie
        selected = node.root
        for component in self._parts(path):
            node = node.children.get(component)
            if node is None:
                break
            if node.root is not None:
                selected = node.root
        if selected is None:
            raise _PreparationRejected(
                FailureCode.PATH_OUTSIDE_ALLOWED_ROOTS.value,
                "path is outside the deletion plan roots: {}".format(path),
            )
        return selected


class QuarantineManager:
    """Batch coordinator around :class:`SafeActionExecutor`."""

    def __init__(self, fs=None):
        self.fs = fs or platform_file_system()

    def validate_read_only(self, service_plan: DeletionPlan) -> Tuple[PreparationFailure, ...]:
        """Revalidate a service plan without creating any filesystem entry."""

        if len(service_plan.actions) > MAX_STORED_OPERATIONS:
            action = service_plan.actions[0]
            return (
                PreparationFailure(
                    action_id=action.action_id,
                    target=action.target.path,
                    code=FailureCode.INVALID_PLAN.value,
                    message="deletion plan exceeds the {} operation safety limit".format(MAX_STORED_OPERATIONS),
                ),
            )
        failures: List[PreparationFailure] = []
        try:
            roots = _AllowedRootIndex.build(service_plan.roots, self)
        except (OSError, QuarantineError, JournalError) as error:
            return tuple(
                PreparationFailure(
                    action_id=action.action_id,
                    target=action.target.path,
                    code=FailureCode.INVALID_PLAN.value,
                    message=str(error),
                )
                for action in service_plan.actions
            )

        for action in service_plan.actions:
            try:
                target_file, target_snapshot = self._validate_read_only_record(
                    action.target,
                    roots,
                )
                keeper_file, keeper_snapshot = self._validate_read_only_record(
                    action.reference,
                    roots,
                )
                if target_snapshot[:2] == keeper_snapshot[:2]:
                    raise _PreparationRejected(
                        FailureCode.SAME_IDENTITY.value,
                        "target and keeper refer to the same physical file",
                    )
                comparison = target_file.compare_bytes(keeper_file)
                if comparison is None:
                    raise _PreparationRejected(
                        FailureCode.CONTENT_MISMATCH.value,
                        "target and keeper no longer have identical bytes",
                    )
                self._verify_read_only_snapshot(action.target.path, target_snapshot)
                self._verify_read_only_snapshot(action.reference.path, keeper_snapshot)
            except _PreparationRejected as error:
                failures.append(PreparationFailure(action.action_id, action.target.path, error.code, error.message))
            except (OSError, QuarantineError, ValueError) as error:
                failures.append(
                    PreparationFailure(
                        action.action_id,
                        action.target.path,
                        FailureCode.METADATA_MISMATCH.value,
                        str(error),
                    )
                )
        return tuple(failures)

    def prepare(self, service_plan: DeletionPlan) -> PreparationBatch:
        """Build every live proof, then persist all plans before any stage."""

        if not service_plan.actions:
            return PreparationBatch(service_plan.plan_id, (), ())
        if len(service_plan.actions) > MAX_STORED_OPERATIONS:
            action = service_plan.actions[0]
            failures = (
                PreparationFailure(
                    action_id=action.action_id,
                    target=action.target.path,
                    code=FailureCode.INVALID_PLAN.value,
                    message="deletion plan exceeds the {} operation safety limit".format(MAX_STORED_OPERATIONS),
                ),
            )
            return PreparationBatch(service_plan.plan_id, (), failures)
        created_directories: List[Path] = []
        prepared: List[PreparedOperation] = []
        failures: List[PreparationFailure] = []
        try:
            roots = _AllowedRootIndex.build(service_plan.roots, self)
        except (OSError, QuarantineError, JournalError) as error:
            failures = [
                PreparationFailure(
                    action_id=action.action_id,
                    target=action.target.path,
                    code=FailureCode.INVALID_PLAN.value,
                    message=str(error),
                )
                for action in service_plan.actions
            ]
            return PreparationBatch(
                service_plan.plan_id,
                (),
                tuple(failures),
            )

        action_roots = []
        for action in service_plan.actions:
            try:
                target_root = roots.select(Path(action.target.path))
                keeper_root = roots.select(Path(action.reference.path))
                selected_roots = tuple(
                    sorted(
                        {_normalized(root.path): root.path for root in (target_root, keeper_root)}.values(),
                        key=_normalized,
                    )
                )
                action_roots.append((action, target_root.path, selected_roots))
            except _PreparationRejected as error:
                failures.append(
                    PreparationFailure(
                        action_id=action.action_id,
                        target=action.target.path,
                        code=error.code,
                        message=error.message,
                    )
                )
        if failures:
            return PreparationBatch(
                service_plan.plan_id,
                (),
                tuple(failures),
            )

        for action, selected_root, selected_roots in action_roots:
            try:
                quarantine_root = selected_root.joinpath(QUARANTINE_DIRECTORY_NAME)
                self._ensure_plain_directory(quarantine_root, created_directories)
                built = build_operation_plan(
                    target=Path(action.target.path),
                    keeper=Path(action.reference.path),
                    allowed_roots=selected_roots,
                    quarantine_root=quarantine_root,
                    plan_id=operation_plan_id(service_plan.plan_id, action.action_id),
                    fs=self.fs,
                )
                if not built.ok or built.plan is None:
                    failure = built.failure
                    code = failure.code.value if failure is not None else FailureCode.INTERNAL_ERROR.value
                    message = failure.message if failure is not None else "safe action plan construction failed"
                    raise _PreparationRejected(code, message)
                operation_plan = replace(built.plan, created_ns=_created_ns(service_plan.created_at))
                self._verify_service_binding(action.target, operation_plan.target)
                self._verify_service_binding(action.reference, operation_plan.keeper)
                rebuilt = build_operation_plan(
                    target=Path(action.target.path),
                    keeper=Path(action.reference.path),
                    allowed_roots=selected_roots,
                    quarantine_root=quarantine_root,
                    plan_id=operation_plan.plan_id,
                    fs=self.fs,
                )
                if not rebuilt.ok or rebuilt.plan is None:
                    failure = rebuilt.failure
                    code = failure.code.value if failure is not None else FailureCode.INTERNAL_ERROR.value
                    message = failure.message if failure is not None else "safe action proof revalidation failed"
                    raise _PreparationRejected(code, message)
                revalidated_plan = replace(rebuilt.plan, created_ns=operation_plan.created_ns)
                self._verify_reproof(operation_plan, revalidated_plan)
                self._verify_proof_metadata(action.target, revalidated_plan.target)
                self._verify_proof_metadata(action.reference, revalidated_plan.keeper)
                operation_plan = revalidated_plan
                stored = StoredOperation(
                    service_plan_id=service_plan.plan_id,
                    action_id=action.action_id,
                    operation=action.operation,
                    operation_plan=operation_plan,
                )
                plan_path = (
                    quarantine_root.joinpath(OPERATION_DIRECTORY_NAME)
                    .joinpath(operation_plan.plan_id)
                    .joinpath(OPERATION_PLAN_FILENAME)
                )
                prepared.append(PreparedOperation(action=action, stored=stored, plan_path=plan_path))
            except _PreparationRejected as error:
                failures.append(
                    PreparationFailure(
                        action_id=action.action_id,
                        target=action.target.path,
                        code=error.code,
                        message=error.message,
                    )
                )
            except (OSError, QuarantineError, ValueError) as error:
                failures.append(
                    PreparationFailure(
                        action_id=action.action_id,
                        target=action.target.path,
                        code=(
                            FailureCode.INVALID_PLAN.value
                            if isinstance(error, (QuarantineError, ValueError))
                            else FailureCode.IO_ERROR.value
                        ),
                        message=str(error),
                    )
                )

        if failures:
            self._cleanup_created_directories(created_directories)
            return PreparationBatch(service_plan.plan_id, tuple(prepared), tuple(failures))

        try:
            self._persist_all(prepared, created_directories)
        except (OSError, QuarantineError, JournalError) as error:
            self._cleanup_created_directories(created_directories)
            failure_results = tuple(
                PreparationFailure(
                    action_id=item.action.action_id,
                    target=item.action.target.path,
                    code=FailureCode.IO_ERROR.value,
                    message="could not persist operation plans atomically: {}".format(error),
                )
                for item in prepared
            )
            return PreparationBatch(service_plan.plan_id, tuple(prepared), failure_results)
        return PreparationBatch(service_plan.plan_id, tuple(prepared), ())

    def execute(self, batch: PreparationBatch) -> Tuple[ManagedResult, ...]:
        """Stage every target into recoverable quarantine without finalizing it."""

        if batch.failures:
            raise QuarantineError("cannot execute a preparation batch that contains failures")
        if not batch.prepared:
            return ()
        if any(
            prepared.action.operation != "quarantine" or prepared.stored.operation != "quarantine"
            for prepared in batch.prepared
        ):
            raise QuarantineError("apply only supports recoverable quarantine; use explicit finalize later")

        # The preparation result is only a convenience object.  The immutable
        # document on disk is the recovery authority, so validate every
        # document before the first target is moved.  This also prevents a
        # later corrupt item in a batch from being discovered only after
        # earlier items have already been staged.
        for prepared in batch.prepared:
            persisted = self.load(prepared.plan_path)
            if persisted.to_dict() != prepared.stored.to_dict():
                raise QuarantineError("persisted operation no longer matches the prepared batch")

        staged: List[Tuple[PreparedOperation, SafeActionResult]] = []
        results: Dict[str, ManagedResult] = {}
        failed_index: Optional[int] = None
        failed_stage: Optional[Tuple[PreparedOperation, SafeActionResult]] = None
        for index, prepared in enumerate(batch.prepared):
            safe_result = self._executor(prepared.stored.operation_plan).stage(prepared.stored.operation_plan)
            if not safe_result.ok:
                failed_index = index
                failed_stage = (prepared, safe_result)
                results[prepared.action.action_id] = self._managed(prepared, safe_result, "failed")
                break
            staged.append((prepared, safe_result))

        if failed_index is not None:
            rollback_items = list(staged)
            if failed_stage is not None:
                failed_prepared, failed_result = failed_stage
                if (
                    failed_result.changed
                    or failed_result.state.value == "staged"
                    or self.fs.lexists(failed_prepared.stored.operation_plan.quarantine_path)
                ):
                    rollback_items.append(failed_stage)
            for prepared, stage_result in reversed(rollback_items):
                restore_result = self._executor(prepared.stored.operation_plan).restore(prepared.stored.operation_plan)
                message = "{}; rollback: {}".format(stage_result.message, restore_result.message)
                is_original_failure = failed_stage is not None and prepared is failed_stage[0]
                failure_code = (
                    stage_result.code.value
                    if is_original_failure and restore_result.ok
                    else (FailureCode.INVALID_STATE.value if restore_result.ok else restore_result.code.value)
                )
                results[prepared.action.action_id] = ManagedResult(
                    action_id=prepared.action.action_id,
                    target=prepared.action.target.path,
                    status="failed",
                    safe_state=restore_result.state.value,
                    failure_code=failure_code,
                    message=message,
                    changed=stage_result.changed or restore_result.changed,
                    operation_plan_path=str(prepared.plan_path),
                    quarantine_path=restore_result.quarantine_path,
                )
            for prepared in batch.prepared[failed_index + 1 :]:
                results[prepared.action.action_id] = self._not_executed(
                    prepared, "not executed because another stage operation failed"
                )
            return tuple(results[item.action.action_id] for item in batch.prepared)

        for prepared, stage_result in staged:
            results[prepared.action.action_id] = self._managed(
                prepared,
                stage_result,
                "applied",
            )
        return tuple(results[item.action.action_id] for item in batch.prepared)

    def list(self, roots: Sequence[str]) -> Tuple[Dict[str, Any], ...]:
        records: List[Dict[str, Any]] = []
        seen = set()
        for root_text in roots:
            root = Path(os.path.abspath(root_text))
            quarantine_root = root.joinpath(QUARANTINE_DIRECTORY_NAME)
            operations_root = quarantine_root.joinpath(OPERATION_DIRECTORY_NAME)
            if not self.fs.lexists(operations_root):
                continue
            self._validate_private_state_directory(operations_root)
            operation_directories: List[Path] = []
            with os.scandir(str(operations_root)) as entries:
                for entry in entries:
                    if len(operation_directories) >= MAX_STORED_OPERATIONS:
                        raise QuarantineError(
                            "quarantine state exceeds the {} operation safety limit".format(MAX_STORED_OPERATIONS)
                        )
                    operation_directories.append(Path(entry.path))
            operation_directories.sort(key=lambda item: os.path.normcase(str(item)))
            paths = []
            for operation_directory in operation_directories:
                self._validate_private_state_directory(operation_directory)
                paths.append(operation_directory.joinpath(OPERATION_PLAN_FILENAME))
            for plan_path in paths:
                stored = self.load(plan_path)
                if stored.operation_plan.plan_id in seen:
                    continue
                seen.add(stored.operation_plan.plan_id)
                records.append(self._describe(plan_path, stored))
        return tuple(records)

    def restore(self, plan_path: Path) -> ManagedResult:
        stored = self.load(plan_path)
        prepared = self._prepared_from_stored(Path(plan_path), stored)
        result = self._executor(stored.operation_plan).restore(stored.operation_plan)
        return self._managed(prepared, result, "applied" if result.ok else "failed")

    def finalize(self, plan_path: Path) -> ManagedResult:
        stored = self.load(plan_path)
        prepared = self._prepared_from_stored(Path(plan_path), stored)
        result = self._executor(stored.operation_plan).finalize(stored.operation_plan)
        return self._managed(prepared, result, "applied" if result.ok else "failed")

    def preflight_restore(self, plan_path: Path) -> ManagedResult:
        """Verify that restore is safe without changing files or the journal."""

        return self._preflight_stored_action(Path(plan_path), "restore")

    def preflight_finalize(self, plan_path: Path) -> ManagedResult:
        """Verify that finalization is safe without changing files or the journal."""

        return self._preflight_stored_action(Path(plan_path), "finalize")

    def _preflight_stored_action(
        self,
        plan_path: Path,
        command: str,
    ) -> ManagedResult:
        if command not in {"restore", "finalize"}:
            raise QuarantineError("unsupported quarantine preflight command")
        stored = self.load(plan_path)
        plan = stored.operation_plan
        executor = self._executor(plan)
        try:
            # These executor helpers only validate the immutable plan, journal,
            # roots, and staged payload. They never append an event.
            executor._validate_plan(plan)
            target_path = Path(plan.target.path)
            quarantine_path = plan.quarantine_path
            target_exists = self.fs.lexists(target_path)
            quarantine_exists = self.fs.lexists(quarantine_path)
            if target_exists and quarantine_exists:
                raise _PreparationRejected(
                    FailureCode.TARGET_CONFLICT.value,
                    "both the original target and staged payload exist",
                )

            events = executor._plan_events(plan)
            event_names = {event.event.value for event in events}
            if command == "restore":
                if target_exists:
                    self._verify_stored_proof(
                        target_path,
                        plan.target,
                        require_original_path=True,
                    )
                    safe_state = "restored"
                    message = "target is already restored and its proof is valid"
                elif quarantine_exists:
                    executor._verify_staged(plan, require_keeper=False)
                    self._validate_restore_destination(plan)
                    safe_state = "staged"
                    message = "staged target is verified and ready to restore"
                elif event_names.intersection({"finalized", "finalized_recovered"}):
                    raise _PreparationRejected(
                        FailureCode.INVALID_STATE.value,
                        "a finalized target cannot be restored",
                    )
                else:
                    raise _PreparationRejected(
                        FailureCode.MISSING_TARGET.value,
                        "neither target nor staged payload exists",
                    )
            else:
                if target_exists:
                    raise _PreparationRejected(
                        FailureCode.INVALID_STATE.value,
                        "target has not been staged",
                    )
                if quarantine_exists:
                    executor._verify_staged(plan, require_keeper=True)
                    safe_state = "staged"
                    message = "staged target and keeper are byte-verified and ready " "for explicit finalization"
                elif event_names.intersection({"finalized", "finalized_recovered"}):
                    safe_state = "finalized"
                    message = "target is already finalized"
                elif "finalize_tombstoned" in event_names:
                    self._verify_stored_proof(
                        Path(plan.keeper.path),
                        plan.keeper,
                        require_original_path=True,
                    )
                    safe_state = "staged"
                    message = (
                        "finalization appears complete and its keeper proof is "
                        "valid; execution can recover the journal"
                    )
                else:
                    raise _PreparationRejected(
                        FailureCode.MISSING_TARGET.value,
                        "staged payload is missing without a finalize record",
                    )
            return ManagedResult(
                action_id=stored.action_id,
                target=plan.target.path,
                status="ready",
                safe_state=safe_state,
                failure_code=FailureCode.NONE.value,
                message=message,
                changed=False,
                operation_plan_path=str(Path(os.path.abspath(plan_path))),
                quarantine_path=str(quarantine_path),
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if hasattr(code, "value"):
                code = code.value
            if not isinstance(code, str):
                code = FailureCode.IO_ERROR.value if isinstance(error, OSError) else FailureCode.INVALID_STATE.value
            message = getattr(error, "message", None) or str(error)
            return ManagedResult(
                action_id=stored.action_id,
                target=plan.target.path,
                status="failed",
                safe_state="failed",
                failure_code=code,
                message=message,
                changed=False,
                operation_plan_path=str(Path(os.path.abspath(plan_path))),
                quarantine_path=str(quarantine_path),
            )

    def _validate_restore_destination(self, plan: OperationPlan) -> None:
        target = Path(plan.target.path)
        self._validate_plain_directory(target.parent)
        resolved_parent = self.fs.resolve(target.parent, strict=True)
        resolved_roots = tuple(self.fs.resolve(Path(root), strict=True) for root in plan.allowed_roots)
        if not any(_is_within(resolved_parent, root) for root in resolved_roots):
            raise _PreparationRejected(
                FailureCode.PATH_OUTSIDE_ALLOWED_ROOTS.value,
                "restore destination parent is outside the allowed roots",
            )

    def _verify_stored_proof(
        self,
        path: Path,
        proof,
        *,
        require_original_path: bool,
    ) -> None:
        self._validate_plain_file(path)
        before = self.fs.lstat(path)
        snapshot = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
        )
        expected = (
            int(proof.identity.volume_id),
            int(proof.identity.file_id),
            int(proof.size),
            int(proof.mtime_ns),
        )
        if snapshot != expected:
            raise _PreparationRejected(
                FailureCode.METADATA_MISMATCH.value,
                "live file identity or generation differs from the stored proof",
            )
        if proof.digest_algorithm != SAFE_PROOF_HASH_ALGORITHM:
            raise _PreparationRejected(
                FailureCode.INVALID_PLAN.value,
                "stored proof does not use the required SHA-256 algorithm",
            )
        digest = self._read_only_sha256(path, snapshot)
        if digest != proof.digest_hex:
            raise _PreparationRejected(
                FailureCode.CONTENT_MISMATCH.value,
                "live file content differs from the stored proof",
            )
        self._verify_read_only_snapshot(str(path), snapshot)
        if require_original_path:
            resolved = self.fs.resolve(path, strict=True)
            if _normalized(resolved) != _normalized(Path(proof.resolved_path)):
                raise _PreparationRejected(
                    FailureCode.PATH_CHANGED.value,
                    "live file now resolves to a different path",
                )

    def load(self, plan_path: Path) -> StoredOperation:
        path = Path(os.path.abspath(os.fspath(plan_path)))
        self._validate_private_plan_file(path)
        try:
            text = self._read_stable_plain_file(path).decode("utf-8")
            raw = strict_bounded_json_loads(
                text,
                limits=DATASET_DOCUMENT_JSON_LIMITS,
                label="stored quarantine operation",
            )
        except MemoryError as error:
            raise QuarantineError("stored operation exceeded the JSON parser memory budget") from error
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
            OverflowError,
        ) as error:
            raise QuarantineError("could not read stored operation: {}".format(error)) from error
        stored = StoredOperation.from_dict(raw)
        self._validate_storage_location(path, stored)
        return stored

    def _read_stable_plain_file(self, path: Path) -> bytes:
        path_before = self.fs.lstat(path)
        if int(path_before.st_size) > MAX_STORED_OPERATION_BYTES:
            raise QuarantineError(
                "stored operation exceeds the {} byte safety limit".format(MAX_STORED_OPERATION_BYTES)
            )
        with self.fs.open_readonly(path) as stream:
            handle_before = os.fstat(stream.fileno())
            self._verify_same_file_stat(path_before, handle_before, path)
            chunks = []
            bytes_read = 0
            while True:
                remaining = MAX_STORED_OPERATION_BYTES + 1 - bytes_read
                if remaining <= 0:
                    raise QuarantineError(
                        "stored operation exceeds the {} byte safety limit".format(MAX_STORED_OPERATION_BYTES)
                    )
                chunk = stream.read(min(core_fs.CHUNK_SIZE, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
                if bytes_read > MAX_STORED_OPERATION_BYTES:
                    raise QuarantineError(
                        "stored operation exceeds the {} byte safety limit".format(MAX_STORED_OPERATION_BYTES)
                    )
            handle_after = os.fstat(stream.fileno())
            self._verify_same_file_stat(handle_before, handle_after, path)
        path_after = self.fs.lstat(path)
        self._verify_same_file_stat(path_before, path_after, path)
        self._verify_same_file_stat(path_after, handle_after, path)
        return b"".join(chunks)

    @staticmethod
    def _verify_same_file_stat(before: os.stat_result, after: os.stat_result, path: Path) -> None:
        before_version = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(getattr(before, "st_nlink", 0)),
        )
        after_version = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(getattr(after, "st_nlink", 0)),
        )
        if before_version != after_version or not stat.S_ISREG(after.st_mode) or _is_reparse_point(after):
            raise QuarantineError("stored operation changed while being read: {}".format(path))

    def _ensure_plain_directory(self, path: Path, created: List[Path]) -> None:
        path = Path(os.path.abspath(os.fspath(path)))
        if self.fs.lexists(path):
            self._validate_private_state_directory(path)
            return
        self._validate_plain_directory(path.parent)
        self.fs.make_directory(path)
        created.append(path)
        self._validate_private_state_directory(path)

    def _validate_plain_directory(self, path: Path) -> None:
        absolute = Path(os.path.abspath(os.fspath(path)))
        for component in _path_components(absolute):
            file_stat = self.fs.lstat(component)
            if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
                raise QuarantineError("directory path contains a link or reparse point: {}".format(component))
        file_stat = self.fs.lstat(absolute)
        if not stat.S_ISDIR(file_stat.st_mode) or _is_reparse_point(file_stat):
            raise QuarantineError("path is not a plain directory: {}".format(absolute))

    def _validate_plain_file(self, path: Path) -> None:
        for component in _path_components(path.parent):
            file_stat = self.fs.lstat(component)
            if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
                raise QuarantineError("file path contains a link or reparse point: {}".format(component))
        file_stat = self.fs.lstat(path)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
            raise QuarantineError("stored operation is not a plain regular file")
        if int(getattr(file_stat, "st_nlink", 0)) != 1:
            raise QuarantineError("stored operation must have exactly one filesystem link")

    def _validate_private_state_directory(self, path: Path) -> None:
        """Require private ownership where portable POSIX metadata permits it.

        Windows path/reparse checks remain enforced, but portable Python does
        not expose a reliable ACL-owner equivalence for this check.
        """

        self._validate_plain_directory(path)
        if os.name != "posix":
            return
        file_stat = self.fs.lstat(path)
        if int(file_stat.st_uid) != int(os.geteuid()):
            raise QuarantineError("quarantine state directory is not owned by the current user: {}".format(path))
        if stat.S_IMODE(file_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise QuarantineError("quarantine state directory is writable by group or other users: {}".format(path))

    def _validate_private_plan_file(self, path: Path) -> None:
        operation_directory = path.parent
        operations_root = operation_directory.parent
        quarantine_root = operations_root.parent
        if quarantine_root.name != QUARANTINE_DIRECTORY_NAME:
            raise QuarantineError("stored operation is outside the quarantine state directory")
        if operations_root.name != OPERATION_DIRECTORY_NAME or path.name != OPERATION_PLAN_FILENAME:
            raise QuarantineError("stored operation has an invalid operation-directory layout")
        self._validate_private_state_directory(quarantine_root)
        self._validate_private_state_directory(operations_root)
        self._validate_private_state_directory(operation_directory)
        self._validate_plain_file(path)
        if os.name != "posix":
            return
        file_stat = self.fs.lstat(path)
        if int(file_stat.st_uid) != int(os.geteuid()):
            raise QuarantineError("stored operation is not owned by the current user")
        if stat.S_IMODE(file_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise QuarantineError("stored operation is writable by group or other users")

    def _verify_service_binding(self, record: FileRecord, proof) -> None:
        if _normalized(Path(record.path)) != _normalized(Path(proof.path)):
            raise _PreparationRejected(FailureCode.PATH_CHANGED.value, "live proof path differs from scan record")
        if record.size != proof.size or record.mtime_ns != proof.mtime_ns:
            raise _PreparationRejected(
                FailureCode.METADATA_MISMATCH.value,
                "live size or modification time differs from scan record: {}".format(record.path),
            )
        expected_identity = (
            str(proof.identity.volume_id),
            proof.identity.file_id,
        )
        if (
            record.volume_id is None
            or record.file_id is None
            or (record.volume_id, record.file_id) != expected_identity
        ):
            raise _PreparationRejected(
                FailureCode.IDENTITY_MISMATCH.value,
                "live physical identity differs from the scan record: {}".format(record.path),
            )
        if record.digest_algorithm != SAFE_PROOF_HASH_ALGORITHM:
            raise _PreparationRejected(
                FailureCode.INVALID_PLAN.value,
                "scan record does not contain the required SHA-256 deletion proof",
            )
        if proof.digest_algorithm != SAFE_PROOF_HASH_ALGORITHM or proof.digest_hex != record.digest.lower():
            raise _PreparationRejected(
                FailureCode.CONTENT_MISMATCH.value,
                "live SHA-256 proof differs from scan record: {}".format(record.path),
            )
        after = os.stat(record.path, follow_symlinks=False)
        try:
            after_identity = SafeFileIdentity.from_physical(
                get_file_identity(
                    record.path,
                    follow_symlinks=False,
                    stat_result=after,
                )
            )
        except (FileIdentityError, ValueError) as error:
            raise _PreparationRejected(
                FailureCode.IDENTITY_UNAVAILABLE.value,
                "physical identity became unavailable while binding the live proof: {}".format(error),
            ) from error
        if (
            after_identity != proof.identity
            or int(after.st_size) != proof.size
            or int(after.st_mtime_ns) != proof.mtime_ns
        ):
            raise _PreparationRejected(
                FailureCode.UNSTABLE_CONTENT.value,
                "file changed while binding the scan record to the live proof: {}".format(record.path),
            )

    def _validate_read_only_record(
        self,
        record: FileRecord,
        roots: _AllowedRootIndex,
    ) -> Tuple[core_fs.File, Tuple[int, int, int, int]]:
        path = Path(os.path.abspath(record.path))
        selected_root = roots.select(path)
        for component in _path_components(path):
            file_stat = self.fs.lstat(component)
            if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
                raise _PreparationRejected(
                    FailureCode.PATH_HAS_LINK_COMPONENT.value,
                    "path contains a link or reparse point: {}".format(component),
                )
        file_stat = self.fs.lstat(path)
        if not stat.S_ISREG(file_stat.st_mode) or _is_reparse_point(file_stat):
            raise _PreparationRejected(
                FailureCode.UNSUPPORTED_TYPE.value,
                "path is not a plain regular file: {}".format(path),
            )
        resolved = self.fs.resolve(path, strict=True)
        if not _is_within(resolved, selected_root.resolved):
            raise _PreparationRejected(
                FailureCode.PATH_OUTSIDE_ALLOWED_ROOTS.value,
                "resolved path is outside the deletion plan roots: {}".format(path),
            )
        snapshot = (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
        )
        if snapshot[2] != record.size or snapshot[3] != record.mtime_ns:
            raise _PreparationRejected(
                FailureCode.METADATA_MISMATCH.value,
                "live size or modification time differs from scan record: {}".format(path),
            )
        try:
            live_identity = SafeFileIdentity.from_physical(
                get_file_identity(
                    path,
                    follow_symlinks=False,
                    stat_result=file_stat,
                )
            )
        except (FileIdentityError, ValueError) as error:
            raise _PreparationRejected(
                FailureCode.IDENTITY_UNAVAILABLE.value,
                "physical identity is unavailable during read-only validation: {}".format(error),
            ) from error
        expected_identity = (
            str(live_identity.volume_id),
            live_identity.file_id,
        )
        if (
            record.volume_id is None
            or record.file_id is None
            or (record.volume_id, record.file_id) != expected_identity
        ):
            raise _PreparationRejected(
                FailureCode.IDENTITY_MISMATCH.value,
                "live physical identity differs from the scan record: {}".format(path),
            )
        if record.digest_algorithm != SAFE_PROOF_HASH_ALGORITHM:
            raise _PreparationRejected(
                FailureCode.INVALID_PLAN.value,
                "scan record does not contain the required SHA-256 deletion proof",
            )
        live_file = core_fs.File(path)
        live_digest = self._read_only_sha256(path, snapshot)
        if live_digest != record.digest.lower():
            raise _PreparationRejected(
                FailureCode.CONTENT_MISMATCH.value,
                "live content digest differs from scan record: {}".format(path),
            )
        self._verify_read_only_snapshot(str(path), snapshot)
        after = self.fs.lstat(path)
        try:
            after_identity = SafeFileIdentity.from_physical(
                get_file_identity(
                    path,
                    follow_symlinks=False,
                    stat_result=after,
                )
            )
        except (FileIdentityError, ValueError) as error:
            raise _PreparationRejected(
                FailureCode.IDENTITY_UNAVAILABLE.value,
                "physical identity became unavailable during read-only validation: {}".format(error),
            ) from error
        if after_identity != live_identity:
            raise _PreparationRejected(
                FailureCode.UNSTABLE_CONTENT.value,
                "file identity changed during read-only validation: {}".format(path),
            )
        live_file.size = record.size
        return live_file, snapshot

    def _read_only_sha256(self, path: Path, expected: Tuple[int, int, int, int]) -> str:
        digest = hashlib.sha256()
        bytes_read = 0
        with self.fs.open_readonly(path) as stream:
            before = os.fstat(stream.fileno())
            before_version = (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(before.st_mtime_ns),
            )
            if before_version != expected:
                raise _PreparationRejected(
                    FailureCode.UNSTABLE_CONTENT.value,
                    "proof handle does not identify the planned file: {}".format(path),
                )
            while True:
                chunk = stream.read(core_fs.CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
            after = os.fstat(stream.fileno())
            after_version = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
            )
        if after_version != expected or bytes_read != expected[2]:
            raise _PreparationRejected(
                FailureCode.UNSTABLE_CONTENT.value,
                "file changed while hashing its read-only SHA-256 proof: {}".format(path),
            )
        return digest.hexdigest()

    def _verify_read_only_snapshot(self, path: str, expected: Tuple[int, int, int, int]) -> None:
        file_stat = self.fs.lstat(Path(path))
        current = (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
        )
        if current != expected:
            raise _PreparationRejected(
                FailureCode.UNSTABLE_CONTENT.value,
                "file changed during read-only preflight: {}".format(path),
            )

    @staticmethod
    def _verify_proof_metadata(record: FileRecord, proof) -> None:
        if (
            _normalized(Path(record.path)) != _normalized(Path(proof.path))
            or record.size != proof.size
            or record.mtime_ns != proof.mtime_ns
        ):
            raise _PreparationRejected(
                FailureCode.METADATA_MISMATCH.value,
                "revalidated live proof differs from the scan record: {}".format(record.path),
            )

    @staticmethod
    def _verify_reproof(first: OperationPlan, second: OperationPlan) -> None:
        if (
            first.plan_id != second.plan_id
            or first.allowed_roots != second.allowed_roots
            or first.quarantine_root != second.quarantine_root
        ):
            raise _PreparationRejected(FailureCode.PATH_CHANGED.value, "operation plan changed during preflight")
        for label, before, after in (
            ("target", first.target, second.target),
            ("keeper", first.keeper, second.keeper),
        ):
            stable_fields = (
                before.path == after.path,
                before.resolved_path == after.resolved_path,
                before.entry_type is after.entry_type,
                before.identity == after.identity,
                before.size == after.size,
                before.mtime_ns == after.mtime_ns,
                before.digest_algorithm == after.digest_algorithm,
                before.digest_hex == after.digest_hex,
            )
            if not all(stable_fields):
                raise _PreparationRejected(
                    FailureCode.UNSTABLE_CONTENT.value,
                    "{} changed while constructing the live operation proof".format(label),
                )

    def _persist_all(self, prepared: Sequence[PreparedOperation], created_directories: List[Path]) -> None:
        for item in prepared:
            operation_directory = item.plan_path.parent
            self._ensure_plain_directory(operation_directory.parent, created_directories)
            self._ensure_plain_directory(operation_directory, created_directories)
            journal = AppendOnlyJournal(
                operation_directory.joinpath(OPERATION_JOURNAL_FILENAME),
                fs=self.fs,
            )
            journal.ensure_capacity(
                additional_events=LIFECYCLE_JOURNAL_RESERVE_EVENTS,
                additional_bytes=(LIFECYCLE_JOURNAL_RESERVE_EVENTS * MAX_JOURNAL_LINE_BYTES),
            )
            if self.fs.lexists(item.plan_path):
                existing = self.load(item.plan_path)
                if existing.to_dict() != item.stored.to_dict():
                    raise QuarantineError("operation plan path is already bound to different content")
                continue
            # Published operation documents are immutable and harmless until a
            # caller explicitly executes the successful batch.  If a later
            # document cannot be persisted, preserve earlier publications for
            # deterministic retry instead of path-unlinking them: another
            # process may have replaced a name after publication.
            self._atomic_write_json(item.plan_path, item.stored.to_dict())

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_STORED_OPERATION_BYTES:
            raise QuarantineError(
                "stored operation exceeds the {} byte safety limit".format(MAX_STORED_OPERATION_BYTES)
            )
        temporary = path.parent.joinpath(".{}.{}.tmp".format(path.name, uuid.uuid4()))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(str(temporary), flags, 0o600)
        created = os.fstat(descriptor)
        created_identity = (int(created.st_dev), int(created.st_ino))
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while saving operation plan")
                view = view[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        else:
            os.close(descriptor)
        try:
            # Use the platform's native atomic no-replace rename on every
            # supported OS.  The former POSIX link/unlink publication briefly
            # exposed two names and made cleanup vulnerable to a source-name
            # replacement race.
            self.fs.rename_no_replace(temporary, path)
            self.fs.fsync_directory(path.parent)
        finally:
            cleanup_created_regular_file(
                temporary,
                created_identity,
                self.fs,
            )

    def _cleanup_created_directories(self, created: Sequence[Path]) -> None:
        for path in reversed(created):
            try:
                if self.fs.lexists(path):
                    self._validate_private_state_directory(path)
                    Path(path).rmdir()
                    self.fs.fsync_directory(Path(path).parent)
            except (OSError, QuarantineError):
                # A concurrently-created entry makes removal unsafe; leaving an
                # empty infrastructure directory is preferable to deleting it.
                continue

    def _validate_storage_location(self, path: Path, stored: StoredOperation) -> None:
        operation_plan = stored.operation_plan
        quarantine_root = Path(operation_plan.quarantine_root)
        if quarantine_root.name != QUARANTINE_DIRECTORY_NAME:
            raise QuarantineError("operation plan quarantine root has an unexpected name")
        allowed_roots = tuple(Path(root) for root in operation_plan.allowed_roots)
        if not any(_normalized(quarantine_root.parent) == _normalized(root) for root in allowed_roots):
            raise QuarantineError("quarantine root is not directly inside an allowed root")
        expected = (
            quarantine_root.joinpath(OPERATION_DIRECTORY_NAME)
            .joinpath(operation_plan.plan_id)
            .joinpath(OPERATION_PLAN_FILENAME)
        )
        if _normalized(path) != _normalized(expected):
            raise QuarantineError("stored operation path does not match its operation plan ID")
        if _is_within(Path(operation_plan.target.path), quarantine_root) or _is_within(
            Path(operation_plan.keeper.path), quarantine_root
        ):
            raise QuarantineError("target and keeper must not be inside quarantine")
        for candidate in (Path(operation_plan.target.path), Path(operation_plan.keeper.path)):
            if not any(_is_within(candidate, root) for root in allowed_roots):
                raise QuarantineError("operation path is outside the stored allowed roots")

    def _executor(self, plan: OperationPlan) -> SafeActionExecutor:
        journal = AppendOnlyJournal(
            Path(plan.quarantine_root)
            .joinpath(OPERATION_DIRECTORY_NAME)
            .joinpath(plan.plan_id)
            .joinpath(OPERATION_JOURNAL_FILENAME),
            fs=self.fs,
        )
        return SafeActionExecutor(journal, fs=self.fs)

    def _prepared_from_stored(self, path: Path, stored: StoredOperation) -> PreparedOperation:
        action = _StoredActionView(
            action_id=stored.action_id,
            operation=stored.operation,
            target=_record_from_proof(stored.operation_plan.target),
        )
        return PreparedOperation(action=action, stored=stored, plan_path=Path(os.path.abspath(path)))

    def _managed(self, prepared: PreparedOperation, result: SafeActionResult, status: str) -> ManagedResult:
        return ManagedResult(
            action_id=prepared.action.action_id,
            target=prepared.action.target.path,
            status=status,
            safe_state=result.state.value,
            failure_code=result.code.value,
            message=result.message,
            changed=result.changed,
            operation_plan_path=str(prepared.plan_path),
            quarantine_path=result.quarantine_path,
        )

    def _not_executed(self, prepared: PreparedOperation, message: str) -> ManagedResult:
        return ManagedResult(
            action_id=prepared.action.action_id,
            target=prepared.action.target.path,
            status="failed",
            safe_state="planned",
            failure_code=FailureCode.INVALID_STATE.value,
            message=message,
            changed=False,
            operation_plan_path=str(prepared.plan_path),
            quarantine_path=str(prepared.stored.operation_plan.quarantine_path),
        )

    def _describe(self, path: Path, stored: StoredOperation) -> Dict[str, Any]:
        plan = stored.operation_plan
        target_exists = self.fs.lexists(Path(plan.target.path))
        quarantine_exists = self.fs.lexists(plan.quarantine_path)
        journal = AppendOnlyJournal(
            Path(plan.quarantine_root)
            .joinpath(OPERATION_DIRECTORY_NAME)
            .joinpath(plan.plan_id)
            .joinpath(OPERATION_JOURNAL_FILENAME),
            fs=self.fs,
        )
        try:
            events = journal.events_for(plan.plan_id)
            event_names = [event.event.value for event in events]
            if quarantine_exists and not target_exists:
                state = "staged"
            elif "finalized" in event_names or "finalized_recovered" in event_names:
                state = "finalized"
            elif (
                target_exists
                and not quarantine_exists
                and ("restored" in event_names or "restored_recovered" in event_names)
            ):
                state = "restored"
            elif target_exists and not quarantine_exists:
                state = "planned"
            else:
                state = "unknown"
            journal_error = ""
        except Exception as error:
            event_names = []
            state = "journal_error"
            journal_error = str(error)
        return {
            "service_plan_id": stored.service_plan_id,
            "action_id": stored.action_id,
            "operation": stored.operation,
            "operation_plan_id": plan.plan_id,
            "operation_plan_fingerprint": plan.fingerprint,
            "operation_plan_path": str(path),
            "target": plan.target.path,
            "keeper": plan.keeper.path,
            "quarantine_path": str(plan.quarantine_path),
            "target_exists": target_exists,
            "quarantine_exists": quarantine_exists,
            "state": state,
            "journal_events": event_names,
            "journal_error": journal_error,
        }


class _PreparationRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _record_from_proof(proof) -> _ProofRecordView:
    return _ProofRecordView(
        path=proof.path,
        size=proof.size,
        mtime_ns=proof.mtime_ns,
        digest_algorithm=proof.digest_algorithm,
        digest=proof.digest_hex,
        volume_id=str(proof.identity.volume_id),
        file_id=str(proof.identity.file_id),
    )
