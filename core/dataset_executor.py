# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Crash-recoverable executor for immutable dataset bundle plans.

``core.dataset_service`` deliberately stops at an immutable plan.  This module is the narrow
mutation boundary for that plan.  It has four non-negotiable properties:

* every source/reference and every destination is preflighted before the first mutation;
* a plan is one transaction, including all primary files and sidecars;
* a source is renamed to a same-volume quarantine rather than unlinked;
* an immutable operation document and an append-only, fsynced journal make replay explicit.

The normal ``apply`` path never permanently deletes source data.  ``finalize`` is a separate,
explicit operation and is the only method that can unlink a quarantined payload.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from core import fs as core_fs
from core.dataset_service import (
    DatasetFileProof,
    DatasetIssue,
    DatasetModeService,
    DatasetOperation,
    DatasetPlan,
    DatasetSafetyError,
    FilesystemInspector,
    PlanValidation,
)
from core.file_generation import FileGenerationToken
from core.file_identity import FileIdentityError, get_file_identity
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_reserved_internal_file,
    is_unsafe_path_component,
    is_within_reserved_internal_directory,
)
from core.safe_action import (
    FileSystemAdapter,
    RenameCommit,
    UnverifiedRenameCommitError,
    cleanup_created_regular_file,
    platform_file_system,
)
from core.safe_json import (
    DATASET_DOCUMENT_JSON_LIMITS,
    JOURNAL_RECORD_JSON_LIMITS,
    JsonStructureError,
    strict_bounded_json_loads,
)
from core.safe_walk import is_reparse_point

DOCUMENT_SCHEMA = "dupeguru.dataset-execution"
DOCUMENT_SCHEMA_VERSION = 2
JOURNAL_SCHEMA = "dupeguru.dataset-execution-journal"
JOURNAL_SCHEMA_VERSION = 1
COPY_CHUNK_SIZE = 1024 * 1024
STATE_DIRECTORY_NAME = ".dupeguru-neo-dataset-executor"
QUARANTINE_DIRECTORY_NAME = ".dupeguru-neo-dataset-quarantine"
OPERATION_DOCUMENT_FILENAME = "operation.json"
OPERATION_JOURNAL_FILENAME = "journal.jsonl"
MAX_EXECUTION_DOCUMENT_BYTES = 128 * 1024 * 1024
# Dataset plan interchange remains bounded at 250,000 file records.  One
# crash-recoverable filesystem transaction is intentionally smaller: its
# operation document and worst-case recovery journal must remain replayable
# after interruption.  Actual UTF-8 journal bytes are projected from the
# concrete paths and can lower this ceiling further.
MAX_EXECUTION_TRANSACTION_FILES = 10_000
MAX_PERSISTED_EXECUTION_OPERATIONS = 250_000
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
MAX_JOURNAL_LINE_BYTES = 64 * 1024
MAX_JOURNAL_EVENTS = 1_000_000


class ExecutionState(Enum):
    """Externally visible state of a dataset operation."""

    DRY_RUN = "dry_run"
    READY = "ready"
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    ROLLED_BACK = "rolled_back"
    RESTORED = "restored"
    FINALIZED = "finalized"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class ExecutionCode(Enum):
    NONE = "none"
    PLAN_INVALID = "plan_invalid"
    EXECUTION_NOT_EXPLICIT = "execution_not_explicit"
    PREFLIGHT_FAILED = "preflight_failed"
    DESTINATION_CONFLICT = "destination_conflict"
    SOURCE_CHANGED = "source_changed"
    CONTENT_MISMATCH = "content_mismatch"
    UNSAFE_PATH = "unsafe_path"
    VOLUME_MISMATCH = "volume_mismatch"
    INSUFFICIENT_SPACE = "insufficient_space"
    DOCUMENT_CONFLICT = "document_conflict"
    JOURNAL_CORRUPT = "journal_corrupt"
    BUSY = "busy"
    EXECUTION_FAILED = "execution_failed"
    ROLLBACK_FAILED = "rollback_failed"
    INVALID_STATE = "invalid_state"
    IO_ERROR = "io_error"


class FileExecutionState(Enum):
    PLANNED = "planned"
    DESTINATION_PUBLISHED = "destination_published"
    QUARANTINED = "quarantined"
    MOVED = "moved"
    APPLIED = "applied"
    RESTORED = "restored"
    FINALIZED = "finalized"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True)
class FileExecutionResult:
    action_id: str
    source: str
    destination: Optional[str]
    quarantine_path: Optional[str]
    state: FileExecutionState
    changed: bool
    message: str


@dataclass(frozen=True)
class DatasetExecutionReport:
    plan_id: str
    state: ExecutionState
    code: ExecutionCode
    message: str
    changed: bool
    files: Tuple[FileExecutionResult, ...] = ()
    issues: Tuple[DatasetIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.code is ExecutionCode.NONE


@dataclass(frozen=True)
class DatasetOperationSummary:
    plan_id: str
    state: ExecutionState
    document_path: str
    created_ns: int
    file_count: int
    message: str


class DatasetExecutionError(RuntimeError):
    def __init__(self, code: ExecutionCode, message: str, path: Optional[Path] = None):
        self.code = code
        self.path = path
        super().__init__(message)


class _Strategy(Enum):
    QUARANTINE = "quarantine"
    SAME_VOLUME_RENAME = "same_volume_rename"
    CROSS_VOLUME_COPY = "cross_volume_copy"


class _JournalEvent(Enum):
    PREPARED = "prepared"
    DESTINATION_PREPARED = "destination_prepared"
    DESTINATION_PUBLISHED = "destination_published"
    TEMPORARY_CREATED = "temporary_created"
    SOURCE_QUARANTINED = "source_quarantined"
    APPLIED = "applied"
    APPLIED_RECOVERED = "applied_recovered"
    ROLLBACK_PREPARED = "rollback_prepared"
    FILE_ROLLED_BACK = "file_rolled_back"
    ROLLED_BACK = "rolled_back"
    RESTORE_PREPARED = "restore_prepared"
    FILE_RESTORED = "file_restored"
    RESTORED = "restored"
    FINALIZE_PREPARED = "finalize_prepared"
    FINALIZE_TOMBSTONE_PREPARED = "finalize_tombstone_prepared"
    FILE_TOMBSTONED = "file_tombstoned"
    FILE_FINALIZED = "file_finalized"
    FINALIZED = "finalized"
    CLEANUP_TOMBSTONE_PREPARED = "cleanup_tombstone_prepared"
    CLEANUP_TOMBSTONED = "cleanup_tombstoned"
    CLEANUP_PURGED = "cleanup_purged"
    FAILED = "failed"


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(candidate), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _require_user_path_outside_internal_state(path: str | Path, label: str) -> Path:
    """Reject a user-controlled source/destination that aliases private state."""

    candidate = _absolute(path)
    candidates = [candidate]
    try:
        if os.path.lexists(candidate):
            candidates.append(candidate.resolve(strict=True))
        else:
            ancestor = candidate
            suffix = []
            while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
                suffix.append(ancestor.name)
                ancestor = ancestor.parent
            if os.path.lexists(ancestor):
                physical = ancestor.resolve(strict=True)
                for part in reversed(suffix):
                    physical = physical.joinpath(part)
                candidates.append(physical)
    except OSError:
        # The executor's no-follow preflight reports the concrete path error.
        pass
    for checked in candidates:
        if is_within_reserved_internal_directory(checked) or is_reserved_internal_file(checked):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "{} must not use a dupeGuru Neo internal path".format(label),
                checked,
            )
    return candidate


def _require_dataset_split_outside_internal_state(split_name: str) -> None:
    if is_unsafe_path_component(split_name) or is_reserved_internal_directory(split_name):
        raise DatasetExecutionError(
            ExecutionCode.UNSAFE_PATH,
            "dataset split must not use a dupeGuru Neo internal directory name",
        )


def _state_namespace_root(path: str | Path) -> Path:
    """Map a configured base to the one canonical private state namespace."""

    candidate = _absolute(path)
    if is_within_reserved_internal_directory(candidate):
        raise ValueError("dataset executor state base must be outside every private namespace")
    return candidate.joinpath(STATE_DIRECTORY_NAME)


def _validate_state_namespace(path: Path) -> None:
    candidate = _absolute(path)
    if (
        os.path.normcase(candidate.name) != os.path.normcase(STATE_DIRECTORY_NAME)
        or candidate.name.rstrip(" .") != candidate.name
        or ":" in candidate.name
        or is_within_reserved_internal_directory(candidate.parent)
    ):
        raise DatasetExecutionError(
            ExecutionCode.UNSAFE_PATH,
            "dataset executor state path is outside the canonical private namespace",
            candidate,
        )
    _require_user_path_outside_internal_state(
        candidate.parent,
        "dataset executor state parent",
    )


def _is_plain_directory(file_stat: os.stat_result) -> bool:
    return stat.S_ISDIR(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode) and not is_reparse_point(file_stat)


def _is_plain_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode) and not is_reparse_point(file_stat)


def _proof_from_dict(value: Mapping[str, Any]) -> DatasetFileProof:
    expected = {
        "path",
        "resolved_path",
        "size",
        "mtime_ns",
        "ctime_ns",
        "generation_token",
        "digest_algorithm",
        "digest_hex",
        "identity_namespace",
        "identity_capability",
        "volume_id",
        "file_id",
        "stat_device",
        "stat_inode",
    }
    if set(value) != expected:
        raise ValueError("execution document contains an invalid file proof")
    return DatasetFileProof(
        path=_required_string(value["path"], "proof path"),
        resolved_path=_required_string(value["resolved_path"], "proof resolved_path"),
        size=_required_int(value["size"], "proof size", minimum=0),
        mtime_ns=_required_int(value["mtime_ns"], "proof mtime_ns", minimum=0),
        ctime_ns=_required_int(value["ctime_ns"], "proof ctime_ns", minimum=0),
        generation_token=_required_string(
            value["generation_token"],
            "proof generation_token",
        ),
        digest_algorithm=_required_string(value["digest_algorithm"], "proof digest_algorithm"),
        digest_hex=_required_string(value["digest_hex"], "proof digest_hex"),
        identity_namespace=_required_string(value["identity_namespace"], "proof identity_namespace"),
        identity_capability=_required_string(value["identity_capability"], "proof identity_capability"),
        volume_id=_required_int(value["volume_id"], "proof volume_id", minimum=0),
        file_id=_required_string(value["file_id"], "proof file_id"),
        stat_device=_required_int(value["stat_device"], "proof stat_device", minimum=0),
        stat_inode=_required_int(value["stat_inode"], "proof stat_inode", minimum=1),
    )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("{} must be a non-empty safe string".format(label))
    return value


def _optional_string(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _required_string(value, label)


def _required_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("{} must be an integer >= {}".format(label, minimum))
    return value


@dataclass(frozen=True)
class _FileRecord:
    ordinal: int
    action_id: str
    asset_id: str
    operation: DatasetOperation
    role: str
    sidecar_slot: str
    source: DatasetFileProof
    reference: Optional[DatasetFileProof]
    destination: Optional[str]
    comparison_path: Optional[str]
    source_root: str
    quarantine_path: Optional[str]
    temporary_path: Optional[str]
    strategy: _Strategy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "action_id": self.action_id,
            "asset_id": self.asset_id,
            "operation": self.operation.value,
            "role": self.role,
            "sidecar_slot": self.sidecar_slot,
            "source": self.source.to_dict(),
            "reference": self.reference.to_dict() if self.reference is not None else None,
            "destination": self.destination,
            "comparison_path": self.comparison_path,
            "source_root": self.source_root,
            "quarantine_path": self.quarantine_path,
            "temporary_path": self.temporary_path,
            "strategy": self.strategy.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_FileRecord":
        expected = {
            "ordinal",
            "action_id",
            "asset_id",
            "operation",
            "role",
            "sidecar_slot",
            "source",
            "reference",
            "destination",
            "comparison_path",
            "source_root",
            "quarantine_path",
            "temporary_path",
            "strategy",
        }
        if set(value) != expected:
            raise ValueError("execution document contains an invalid file record")
        reference_value = value["reference"]
        if reference_value is not None and not isinstance(reference_value, dict):
            raise ValueError("file record reference must be an object or null")
        source_value = value["source"]
        if not isinstance(source_value, dict):
            raise ValueError("file record source must be an object")
        try:
            operation = DatasetOperation(value["operation"])
            strategy = _Strategy(value["strategy"])
        except (TypeError, ValueError) as error:
            raise ValueError("file record contains an unsupported operation") from error
        result = cls(
            ordinal=_required_int(value["ordinal"], "file ordinal"),
            action_id=_required_string(value["action_id"], "action ID"),
            asset_id=_required_string(value["asset_id"], "asset ID"),
            operation=operation,
            role=_required_string(value["role"], "file role"),
            sidecar_slot=value["sidecar_slot"] if isinstance(value["sidecar_slot"], str) else "",
            source=_proof_from_dict(source_value),
            reference=_proof_from_dict(reference_value) if reference_value is not None else None,
            destination=_optional_string(value["destination"], "destination"),
            comparison_path=_optional_string(value["comparison_path"], "comparison path"),
            source_root=_required_string(value["source_root"], "source root"),
            quarantine_path=_optional_string(value["quarantine_path"], "quarantine path"),
            temporary_path=_optional_string(value["temporary_path"], "temporary path"),
            strategy=strategy,
        )
        result._validate_shape()
        return result

    def _validate_shape(self) -> None:
        if self.role not in {"primary", "sidecar"}:
            raise ValueError("execution document has an invalid file role")
        if self.strategy is _Strategy.QUARANTINE:
            if self.operation is not DatasetOperation.QUARANTINE_BUNDLE:
                raise ValueError("quarantine strategy has an invalid operation")
            if (
                self.destination is not None
                or self.reference is None
                or self.comparison_path is None
                or self.quarantine_path is None
                or self.temporary_path is not None
            ):
                raise ValueError("quarantine strategy record is incomplete")
        elif self.operation is not DatasetOperation.MOVE_BUNDLE or self.destination is None:
            raise ValueError("move strategy has an invalid operation")
        elif self.strategy is _Strategy.SAME_VOLUME_RENAME:
            if self.quarantine_path is not None or self.temporary_path is not None:
                raise ValueError("same-volume move has unexpected staging paths")
        elif self.strategy is _Strategy.CROSS_VOLUME_COPY:
            if self.quarantine_path is None or self.temporary_path is None:
                raise ValueError("cross-volume move is missing staging paths")


@dataclass(frozen=True)
class _ExecutionDocument:
    plan_id: str
    plan_hash: str
    created_ns: int
    state_root: str
    destination_root: str
    plan: Mapping[str, Any]
    files: Tuple[_FileRecord, ...]
    schema: str = DOCUMENT_SCHEMA
    schema_version: int = DOCUMENT_SCHEMA_VERSION
    _document_hash: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Detach the immutable operation document from the mutable dictionary
        # returned by DatasetPlan.to_dict(), then hash its complete identity
        # exactly once. Journal records only read this cached digest.
        canonical_plan = json.loads(_canonical_json(dict(self.plan)))
        object.__setattr__(self, "plan", canonical_plan)
        object.__setattr__(
            self,
            "_document_hash",
            _content_hash(self.identity_dict()),
        )

    def identity_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "created_ns": self.created_ns,
            "state_root": self.state_root,
            "destination_root": self.destination_root,
            "plan": dict(self.plan),
            "files": [record.to_dict() for record in self.files],
        }

    @property
    def document_hash(self) -> str:
        return self._document_hash

    def to_dict(self) -> Dict[str, Any]:
        result = self.identity_dict()
        result["document_hash"] = self.document_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_ExecutionDocument":
        expected = {
            "schema",
            "schema_version",
            "plan_id",
            "plan_hash",
            "created_ns",
            "state_root",
            "destination_root",
            "plan",
            "files",
            "document_hash",
        }
        if set(value) != expected:
            raise ValueError("invalid execution document shape")
        if value["schema"] != DOCUMENT_SCHEMA or value["schema_version"] != DOCUMENT_SCHEMA_VERSION:
            raise ValueError("unsupported execution document schema")
        plan_value = value["plan"]
        file_values = value["files"]
        if not isinstance(plan_value, dict) or not isinstance(file_values, list) or not file_values:
            raise ValueError("execution document plan/files are invalid")
        if len(file_values) > MAX_EXECUTION_TRANSACTION_FILES:
            raise ValueError(
                "execution document contains {} file records; maximum is {}".format(
                    len(file_values),
                    MAX_EXECUTION_TRANSACTION_FILES,
                )
            )
        document = cls(
            plan_id=_required_string(value["plan_id"], "plan ID"),
            plan_hash=_required_string(value["plan_hash"], "plan hash"),
            created_ns=_required_int(value["created_ns"], "created_ns"),
            state_root=_required_string(value["state_root"], "state root"),
            destination_root=_required_string(value["destination_root"], "destination root"),
            plan=plan_value,
            files=tuple(_FileRecord.from_dict(item) for item in file_values if isinstance(item, dict)),
        )
        if len(document.files) != len(file_values):
            raise ValueError("execution document contains a non-object file record")
        if document.plan_id != plan_value.get("plan_id") or document.plan_hash != _content_hash(plan_value):
            raise ValueError("execution document is not bound to its dataset plan")
        claimed_hash = _required_string(
            value["document_hash"],
            "document hash",
        )
        if len(claimed_hash) != hashlib.sha256().digest_size * 2 or not hmac.compare_digest(
            claimed_hash, document.document_hash
        ):
            raise ValueError("execution document hash mismatch")
        ordinals = tuple(record.ordinal for record in document.files)
        if ordinals != tuple(range(len(document.files))):
            raise ValueError("execution document file order is invalid")
        return document


@dataclass(frozen=True)
class _JournalRecord:
    event_id: str
    timestamp_ns: int
    plan_id: str
    document_hash: str
    event: _JournalEvent
    details: Mapping[str, Any]
    previous_hash: str
    record_hash: str


@dataclass(frozen=True)
class _TombstoneHistory:
    original_path: Path
    tombstone_path: Path
    stat_device: int
    stat_inode: int
    tombstoned: bool
    purged: bool


@dataclass(frozen=True)
class _JournalBudget:
    events: int = 0
    bytes: int = 0

    def __add__(self, other: "_JournalBudget") -> "_JournalBudget":
        return _JournalBudget(
            self.events + other.events,
            self.bytes + other.bytes,
        )

    def scaled(self, multiplier: int) -> "_JournalBudget":
        return _JournalBudget(self.events * multiplier, self.bytes * multiplier)


@dataclass(frozen=True)
class _JournalEventIndex:
    """One-pass ordinal index for immutable journal history."""

    events: Tuple[_JournalRecord, ...]
    by_ordinal_kind: Mapping[
        Tuple[int, Optional[str]],
        Tuple[_JournalRecord, ...],
    ]

    @classmethod
    def build(
        cls,
        document: _ExecutionDocument,
        events: Tuple[_JournalRecord, ...],
    ) -> "_JournalEventIndex":
        indexed_types = {
            _JournalEvent.TEMPORARY_CREATED,
            _JournalEvent.DESTINATION_PREPARED,
            _JournalEvent.CLEANUP_TOMBSTONE_PREPARED,
            _JournalEvent.CLEANUP_TOMBSTONED,
            _JournalEvent.CLEANUP_PURGED,
            _JournalEvent.FINALIZE_TOMBSTONE_PREPARED,
            _JournalEvent.FILE_TOMBSTONED,
            _JournalEvent.FILE_FINALIZED,
        }
        cleanup_types = {
            _JournalEvent.CLEANUP_TOMBSTONE_PREPARED,
            _JournalEvent.CLEANUP_TOMBSTONED,
            _JournalEvent.CLEANUP_PURGED,
        }
        grouped: Dict[
            Tuple[int, Optional[str]],
            List[_JournalRecord],
        ] = {}
        for event in events:
            if event.event not in indexed_types:
                continue
            try:
                ordinal = _required_int(
                    event.details["ordinal"],
                    "indexed journal ordinal",
                )
            except (KeyError, ValueError) as error:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "indexed journal event has an invalid ordinal",
                ) from error
            if ordinal >= len(document.files):
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "indexed journal event references an unknown file",
                )
            kind: Optional[str] = None
            if event.event in cleanup_types:
                raw_kind = event.details.get("kind")
                if raw_kind not in {"destination", "temporary"}:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "cleanup journal event has an invalid kind",
                    )
                kind = str(raw_kind)
            grouped.setdefault((ordinal, kind), []).append(event)
        return cls(
            events=events,
            by_ordinal_kind={key: tuple(value) for key, value in grouped.items()},
        )

    def for_record(
        self,
        ordinal: int,
        *,
        kind: Optional[str] = None,
    ) -> Tuple[_JournalRecord, ...]:
        return self.by_ordinal_kind.get((ordinal, kind), ())


class _AppendOnlyJournal:
    """Fsynced JSONL journal with a hash chain.

    A process can be terminated between the write and the final newline.  Replay ignores only that
    final partial record.  A malformed complete record or a broken hash chain is fatal.
    """

    def __init__(self, path: Path, fs: FileSystemAdapter):
        self.path = _absolute(path)
        self.fs = fs
        self._thread_lock = threading.Lock()
        self._cached_signature: Optional[Tuple[int, int, int, int, bytes]] = None
        self._cached_last_hash = ""
        self._cached_event_count = 0
        self._cached_has_partial = False
        self._cached_plan_events: Dict[str, Tuple[_JournalRecord, ...]] = {}

    def _signature(self, file_stat: os.stat_result) -> Tuple[int, int, int, int, bytes]:
        return (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
            core_fs.FileSnapshot.from_path(self.path, file_stat).ctime_ns,
        )

    def _invalidate_cache(self) -> None:
        self._cached_signature = None
        self._cached_last_hash = ""
        self._cached_event_count = 0
        self._cached_has_partial = False
        self._cached_plan_events.clear()

    def _validate_path(self) -> None:
        if not self.fs.lexists(self.path):
            return
        file_stat = self.fs.lstat(self.path)
        if not _is_plain_file(file_stat) or int(file_stat.st_nlink) != 1:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "dataset execution journal is not a private single-link regular file",
                self.path,
            )

    def read(self) -> Tuple[_JournalRecord, ...]:
        records, _last_hash, _event_count, _has_partial = self._scan(plan_id=None, retain=True)
        return records

    def _scan(
        self,
        *,
        plan_id: Optional[str],
        retain: bool,
    ) -> Tuple[Tuple[_JournalRecord, ...], str, int, bool]:
        self._validate_path()
        if not self.fs.lexists(self.path):
            return (), "", 0, False
        records: List[_JournalRecord] = []
        previous_hash = ""
        total_bytes = 0
        event_count = 0
        has_partial = False
        try:
            before = os.stat(self.path, follow_symlinks=False)
            signature = self._signature(before)
            if signature == self._cached_signature:
                if not retain:
                    return (
                        (),
                        self._cached_last_hash,
                        self._cached_event_count,
                        self._cached_has_partial,
                    )
                if plan_id is not None and plan_id in self._cached_plan_events:
                    return (
                        self._cached_plan_events[plan_id],
                        self._cached_last_hash,
                        self._cached_event_count,
                        self._cached_has_partial,
                    )
            if before.st_size > MAX_JOURNAL_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "dataset execution journal exceeds the {} byte limit".format(MAX_JOURNAL_BYTES),
                    self.path,
                )
            with self.fs.open_readonly(self.path) as handle:
                opened = os.fstat(handle.fileno())
                before_snapshot = core_fs.FileSnapshot.from_path(self.path, before)
                opened_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=self.path,
                    stat_result=opened,
                )
                if (
                    not _is_plain_file(before)
                    or not _is_plain_file(opened)
                    or not before_snapshot.same_content_generation(opened_snapshot)
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "journal changed while it was opened",
                        self.path,
                    )
                while True:
                    line = handle.readline(MAX_JOURNAL_LINE_BYTES + 1)
                    if not line:
                        break
                    total_bytes += len(line)
                    if total_bytes > MAX_JOURNAL_BYTES:
                        raise DatasetExecutionError(
                            ExecutionCode.JOURNAL_CORRUPT,
                            "dataset execution journal exceeds the {} byte limit".format(MAX_JOURNAL_BYTES),
                            self.path,
                        )
                    if len(line) > MAX_JOURNAL_LINE_BYTES:
                        raise DatasetExecutionError(
                            ExecutionCode.JOURNAL_CORRUPT,
                            "dataset execution journal line exceeds the {} byte limit".format(MAX_JOURNAL_LINE_BYTES),
                            self.path,
                        )
                    # A process can be terminated before the final newline.  Ignore only that
                    # bounded final fragment; a complete malformed record is rejected below.
                    if not line.endswith(b"\n"):
                        if handle.read(1):
                            raise DatasetExecutionError(
                                ExecutionCode.JOURNAL_CORRUPT,
                                "journal contains a truncated non-final record",
                                self.path,
                            )
                        has_partial = True
                        break
                    if event_count >= MAX_JOURNAL_EVENTS:
                        raise DatasetExecutionError(
                            ExecutionCode.JOURNAL_CORRUPT,
                            "dataset execution journal exceeds the {} event limit".format(MAX_JOURNAL_EVENTS),
                            self.path,
                        )
                    record = self._parse_line(line, previous_hash)
                    event_count += 1
                    if retain and (plan_id is None or record.plan_id == plan_id):
                        records.append(record)
                    previous_hash = record.record_hash
                finished = os.fstat(handle.fileno())
                finished_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=self.path,
                    stat_result=finished,
                )
                if not opened_snapshot.same_content_generation(finished_snapshot):
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "journal changed while it was read",
                        self.path,
                    )
            current = os.stat(self.path, follow_symlinks=False)
            current_snapshot = core_fs.FileSnapshot.from_path(self.path, current)
            if not opened_snapshot.same_content_generation(current_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "journal path changed while it was read",
                    self.path,
                )
        except DatasetExecutionError:
            self._invalidate_cache()
            raise
        except OSError as error:
            self._invalidate_cache()
            raise DatasetExecutionError(ExecutionCode.JOURNAL_CORRUPT, str(error), self.path) from error
        result = tuple(records)
        self._cached_signature = self._signature(current)
        self._cached_last_hash = previous_hash
        self._cached_event_count = event_count
        self._cached_has_partial = has_partial
        if retain and plan_id is not None:
            self._cached_plan_events[plan_id] = result
        return result, previous_hash, event_count, has_partial

    def _parse_line(self, line: bytes, previous_hash: str) -> _JournalRecord:
        try:
            text = line.decode("utf-8")
            value = strict_bounded_json_loads(
                text,
                limits=JOURNAL_RECORD_JSON_LIMITS,
                label="dataset execution journal record",
            )
            expected = {
                "schema",
                "schema_version",
                "event_id",
                "timestamp_ns",
                "plan_id",
                "document_hash",
                "event",
                "details",
                "previous_hash",
                "record_hash",
            }
            if not isinstance(value, dict) or set(value) != expected:
                raise ValueError("invalid journal record shape")
            if value["schema"] != JOURNAL_SCHEMA or value["schema_version"] != JOURNAL_SCHEMA_VERSION:
                raise ValueError("unsupported journal schema")
            details = value["details"]
            if not isinstance(details, dict):
                raise ValueError("journal details must be an object")
            identity = dict(value)
            claimed_hash = identity.pop("record_hash")
            calculated_hash = _content_hash(identity)
            if claimed_hash != calculated_hash:
                raise ValueError("journal record hash mismatch")
            if value["previous_hash"] != previous_hash:
                raise ValueError("journal hash chain is broken")
            return _JournalRecord(
                event_id=_required_string(value["event_id"], "event ID"),
                timestamp_ns=_required_int(value["timestamp_ns"], "timestamp_ns"),
                plan_id=_required_string(value["plan_id"], "plan ID"),
                document_hash=_required_string(value["document_hash"], "document hash"),
                event=_JournalEvent(value["event"]),
                details=details,
                previous_hash=value["previous_hash"],
                record_hash=claimed_hash,
            )
        except MemoryError as error:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "dataset execution journal exceeded the parser memory budget",
                self.path,
            ) from error
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ) as error:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "invalid journal record: {}".format(error),
                self.path,
            ) from error

    def events_for(self, document: _ExecutionDocument) -> Tuple[_JournalRecord, ...]:
        records, _last_hash, _event_count, _has_partial = self._scan(
            plan_id=document.plan_id,
            retain=True,
        )
        if any(record.document_hash != document.document_hash for record in records):
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "journal history belongs to a different execution document",
                self.path,
            )
        return records

    @staticmethod
    def projected_record_size(
        document: _ExecutionDocument,
        event: _JournalEvent,
        details: Mapping[str, Any],
    ) -> int:
        """Return a deterministic upper bound for one future JSONL record."""

        identity: Dict[str, Any] = {
            "schema": JOURNAL_SCHEMA,
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "event_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            # Twenty decimal digits exceed current time_ns values.
            "timestamp_ns": 9_999_999_999_999_999_999,
            "plan_id": document.plan_id,
            "document_hash": document.document_hash,
            "event": event.value,
            "details": dict(details),
            "previous_hash": "f" * 64,
            "record_hash": "f" * 64,
        }
        encoded = (_canonical_json(identity) + "\n").encode("utf-8")
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise DatasetExecutionError(
                ExecutionCode.PLAN_INVALID,
                "a required dataset journal record would exceed the {} byte line limit".format(MAX_JOURNAL_LINE_BYTES),
            )
        return len(encoded)

    def ensure_capacity(
        self,
        document: _ExecutionDocument,
        *,
        additional_events: int,
        additional_bytes: int,
    ) -> None:
        """Prove a phase's complete journal budget before its first mutation."""

        if (
            isinstance(additional_events, bool)
            or not isinstance(additional_events, int)
            or additional_events < 0
            or isinstance(additional_bytes, bool)
            or not isinstance(additional_bytes, int)
            or additional_bytes < 0
        ):
            raise ValueError("dataset journal reservations must be non-negative integers")
        with self._thread_lock:
            records, _last_hash, event_count, has_partial = self._scan(
                plan_id=None,
                retain=True,
            )
            if has_partial:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "refusing mutation after a truncated final journal record",
                    self.path,
                )
            if any(
                record.plan_id != document.plan_id or record.document_hash != document.document_hash
                for record in records
            ):
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "per-operation journal contains foreign operation history",
                    self.path,
                )
            current_size = int(self.fs.lstat(self.path).st_size) if self.fs.lexists(self.path) else 0
            if event_count + additional_events > MAX_JOURNAL_EVENTS:
                raise DatasetExecutionError(
                    ExecutionCode.PLAN_INVALID,
                    "dataset operation lacks capacity for {} required journal events".format(additional_events),
                    self.path,
                )
            if current_size + additional_bytes > MAX_JOURNAL_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.PLAN_INVALID,
                    "dataset operation lacks capacity for {} required journal bytes".format(additional_bytes),
                    self.path,
                )

    def append(
        self,
        document: _ExecutionDocument,
        event: _JournalEvent,
        details: Optional[Mapping[str, Any]] = None,
    ) -> _JournalRecord:
        with self._thread_lock:
            _records, previous_hash, event_count, has_partial = self._scan(plan_id=None, retain=False)
            if has_partial:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "refusing to append after a truncated final journal record",
                    self.path,
                )
            if event_count >= MAX_JOURNAL_EVENTS:
                raise DatasetExecutionError(
                    ExecutionCode.IO_ERROR,
                    "dataset execution journal reached the {} event limit".format(MAX_JOURNAL_EVENTS),
                    self.path,
                )
            identity: Dict[str, Any] = {
                "schema": JOURNAL_SCHEMA,
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "event_id": str(uuid.uuid4()),
                "timestamp_ns": time.time_ns(),
                "plan_id": document.plan_id,
                "document_hash": document.document_hash,
                "event": event.value,
                "details": dict(details or {}),
                "previous_hash": previous_hash,
            }
            identity["record_hash"] = _content_hash(identity)
            encoded = (_canonical_json(identity) + "\n").encode("utf-8")
            if len(encoded) > MAX_JOURNAL_LINE_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.IO_ERROR,
                    "dataset execution journal record exceeds the {} byte line limit".format(MAX_JOURNAL_LINE_BYTES),
                    self.path,
                )
            current_size = self.path.stat().st_size if self.fs.lexists(self.path) else 0
            if current_size + len(encoded) > MAX_JOURNAL_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.IO_ERROR,
                    "dataset execution journal would exceed the {} byte limit".format(MAX_JOURNAL_BYTES),
                    self.path,
                )
            _validate_existing_path_chain(self.path.parent)
            if not self.path.parent.is_dir():
                raise DatasetExecutionError(
                    ExecutionCode.IO_ERROR,
                    "journal parent directory does not exist",
                    self.path.parent,
                )
            existed = self.fs.lexists(self.path)
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            flags |= os.O_CREAT | os.O_EXCL if not existed else 0
            try:
                fd = os.open(str(self.path), flags, 0o600)
                try:
                    opened_stat = os.fstat(fd)
                    current_stat = os.stat(self.path, follow_symlinks=False)
                    if (
                        not _is_plain_file(opened_stat)
                        or not _is_plain_file(current_stat)
                        or opened_stat.st_dev != current_stat.st_dev
                        or opened_stat.st_ino != current_stat.st_ino
                        or int(opened_stat.st_nlink) != 1
                        or int(current_stat.st_nlink) != 1
                        or int(opened_stat.st_size) != int(current_size)
                    ):
                        raise OSError("journal path changed while it was opened")
                    written = os.write(fd, encoded)
                    if written != len(encoded):
                        raise OSError("short write while appending the dataset journal")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if not existed:
                    self.fs.fsync_directory(self.path.parent)
            except OSError as error:
                self._invalidate_cache()
                raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), self.path) from error
            record = _JournalRecord(
                event_id=identity["event_id"],
                timestamp_ns=identity["timestamp_ns"],
                plan_id=document.plan_id,
                document_hash=document.document_hash,
                event=event,
                details=identity["details"],
                previous_hash=previous_hash,
                record_hash=identity["record_hash"],
            )
            current = os.stat(self.path, follow_symlinks=False)
            self._cached_signature = self._signature(current)
            self._cached_last_hash = record.record_hash
            self._cached_event_count = event_count + 1
            self._cached_has_partial = False
            if document.plan_id in self._cached_plan_events:
                self._cached_plan_events[document.plan_id] += (record,)
            return record


def _path_parts(path: Path) -> Iterator[Path]:
    absolute = _absolute(path)
    anchor = Path(absolute.anchor)
    if anchor:
        yield anchor
    current = anchor
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        yield current


def _validate_existing_path_chain(path: Path) -> None:
    """Reject every existing link/reparse component of ``path``."""

    missing = False
    for component in _path_parts(path):
        if missing:
            continue
        try:
            file_stat = os.stat(component, follow_symlinks=False)
        except FileNotFoundError:
            missing = True
            continue
        except OSError as error:
            raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, str(error), component) from error
        if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "path contains a symbolic link or reparse point",
                component,
            )
        if component != _absolute(path) and not stat.S_ISDIR(file_stat.st_mode):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "path contains a non-directory parent component",
                component,
            )


def _validate_plain_directory(path: Path) -> os.stat_result:
    candidate = _absolute(path)
    _validate_existing_path_chain(candidate)
    try:
        file_stat = os.stat(candidate, follow_symlinks=False)
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, str(error), candidate) from error
    if not _is_plain_directory(file_stat):
        raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, "path is not a plain directory", candidate)
    return file_stat


def _validate_private_directory(path: Path, label: str) -> os.stat_result:
    """Require a tool-owned private directory on POSIX.

    Windows ACL ownership cannot be represented by ``st_uid``/mode bits; Windows still receives
    the no-link/reparse and plain-directory validation above.
    """

    candidate = _absolute(path)
    file_stat = _validate_plain_directory(candidate)
    if os.name != "nt":
        current_uid = os.geteuid()
        if int(file_stat.st_uid) != int(current_uid):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "{} is not owned by the current user".format(label),
                candidate,
            )
        if stat.S_IMODE(file_stat.st_mode) & 0o022:
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "{} is group/world writable".format(label),
                candidate,
            )
    return file_stat


def _validate_private_directory_tree(base: Path, leaf: Path, label: str) -> None:
    base = _absolute(base)
    leaf = _absolute(leaf)
    if not _is_within(leaf, base):
        raise DatasetExecutionError(
            ExecutionCode.UNSAFE_PATH,
            "{} escaped its private root".format(label),
            leaf,
        )
    current = base
    while True:
        if os.path.lexists(current):
            _validate_private_directory(current, label)
        if current == leaf:
            break
        relative = leaf.relative_to(current)
        current = current.joinpath(relative.parts[0])


def _existing_ancestor(path: Path) -> Path:
    candidate = _absolute(path)
    while not os.path.lexists(candidate):
        parent = candidate.parent
        if parent == candidate:
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "path has no accessible existing ancestor",
                path,
            )
        candidate = parent
    _validate_plain_directory(candidate)
    return candidate


def _ensure_directory(path: Path, created: List[Path], fs: FileSystemAdapter) -> None:
    candidate = _absolute(path)
    if fs.lexists(candidate):
        _validate_plain_directory(candidate)
        return
    parent = candidate.parent
    if parent == candidate:
        raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, "cannot create a filesystem root", candidate)
    _ensure_directory(parent, created, fs)
    try:
        fs.make_directory(candidate)
    except FileExistsError:
        _validate_plain_directory(candidate)
        return
    _validate_plain_directory(candidate)
    created.append(candidate)


def _cleanup_empty_directories(paths: Iterable[Path], fs: FileSystemAdapter) -> None:
    for path in reversed(tuple(paths)):
        try:
            _validate_plain_directory(path)
            os.rmdir(str(path))
            fs.fsync_directory(path.parent)
        except (FileNotFoundError, OSError, DatasetExecutionError):
            continue


def _identity_parts(path: Path, file_stat: os.stat_result) -> Tuple[str, str, int, str]:
    try:
        identity = get_file_identity(path, follow_symlinks=False, stat_result=file_stat)
    except FileIdentityError as error:
        raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), path) from error
    file_id = identity.file_id.hex() if isinstance(identity.file_id, bytes) else str(identity.file_id)
    return identity.namespace, identity.capability.value, identity.volume_id, file_id


def _identity_matches(file_stat: os.stat_result, path: Path, proof: DatasetFileProof) -> bool:
    return (
        file_stat.st_dev == proof.stat_device
        and file_stat.st_ino == proof.stat_inode
        and _identity_parts(path, file_stat) == proof.identity_key
    )


def _stable_digest(
    path: Path,
    fs: FileSystemAdapter,
    *,
    expected: DatasetFileProof,
    require_original_identity: bool,
) -> None:
    candidate = _absolute(path)
    _validate_existing_path_chain(candidate)
    try:
        before = os.stat(candidate, follow_symlinks=False)
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), candidate) from error
    if not _is_plain_file(before):
        raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, "payload is not a plain regular file", candidate)
    if before.st_size != expected.size:
        raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, "payload size changed", candidate)
    if require_original_identity and not _identity_matches(before, candidate, expected):
        raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, "payload identity changed", candidate)
    before_snapshot = core_fs.FileSnapshot.from_path(candidate, before)
    digest = hashlib.sha256()
    try:
        with fs.open_readonly(candidate) as handle:
            opened = os.fstat(handle.fileno())
            opened_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=candidate,
                stat_result=opened,
            )
            if not before_snapshot.same_content_generation(opened_snapshot):
                raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, "payload changed while opening", candidate)
            while True:
                chunk = handle.read(COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
            finished_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=candidate,
                stat_result=finished,
            )
            if not opened_snapshot.same_content_generation(finished_snapshot):
                raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, "payload changed while reading", candidate)
    except DatasetExecutionError:
        raise
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), candidate) from error
    after = os.stat(candidate, follow_symlinks=False)
    after_snapshot = core_fs.FileSnapshot.from_path(candidate, after)
    if not before_snapshot.same_content_generation(after_snapshot):
        raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, "payload changed after reading", candidate)
    if digest.hexdigest() != expected.digest_hex:
        raise DatasetExecutionError(ExecutionCode.CONTENT_MISMATCH, "payload SHA-256 does not match plan", candidate)


@contextlib.contextmanager
def _open_verified_payload(
    path: Path,
    fs: FileSystemAdapter,
    *,
    expected: DatasetFileProof,
    require_original_identity: bool,
    created_identity: Optional[Tuple[int, int]] = None,
) -> Iterator[Tuple[BinaryIO, os.stat_result]]:
    """Keep one fully verified payload handle live across a namespace mutation."""

    candidate = _absolute(path)
    _validate_existing_path_chain(candidate)
    try:
        path_before = os.stat(candidate, follow_symlinks=False)
        if not _is_plain_file(path_before):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "payload is not a plain regular file",
                candidate,
            )
        if path_before.st_size != expected.size:
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "payload size changed",
                candidate,
            )
        if require_original_identity and not _identity_matches(path_before, candidate, expected):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "payload identity changed",
                candidate,
            )
        if created_identity is not None and (
            int(path_before.st_dev),
            int(path_before.st_ino),
        ) != tuple(map(int, created_identity)):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "transaction-created payload identity changed",
                candidate,
            )
        path_snapshot = core_fs.FileSnapshot.from_path(candidate, path_before)
        with fs.open_readonly(candidate) as handle:
            opened = os.fstat(handle.fileno())
            opened_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=candidate,
                stat_result=opened,
            )
            if not path_snapshot.same_content_generation(opened_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "payload changed while it was opened",
                    candidate,
                )
            digest = hashlib.sha256()
            while True:
                chunk = handle.read(COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
            finished_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=candidate,
                stat_result=finished,
            )
            if not opened_snapshot.same_content_generation(finished_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "payload changed while it was read",
                    candidate,
                )
            if digest.hexdigest() != expected.digest_hex:
                raise DatasetExecutionError(
                    ExecutionCode.CONTENT_MISMATCH,
                    "payload SHA-256 does not match plan",
                    candidate,
                )
            current_path = _require_path_matches_stat(candidate, finished)
            current_snapshot = core_fs.FileSnapshot.from_path(candidate, current_path)
            if not finished_snapshot.same_content_generation(current_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "payload changed after it was read",
                    candidate,
                )
            yield handle, finished
    except DatasetExecutionError:
        raise
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), candidate) from error


def _stable_byte_equal(
    first: Path,
    second: Path,
    fs: FileSystemAdapter,
    expected: DatasetFileProof,
    *,
    first_original_identity: bool,
    second_original_identity: bool,
    second_expected: Optional[DatasetFileProof] = None,
) -> None:
    _stable_digest(first, fs, expected=expected, require_original_identity=first_original_identity)
    _stable_digest(
        second,
        fs,
        expected=second_expected or expected,
        require_original_identity=second_original_identity,
    )
    try:
        first_path_before = os.stat(first, follow_symlinks=False)
        second_path_before = os.stat(second, follow_symlinks=False)
        if not _is_plain_file(first_path_before) or not _is_plain_file(second_path_before):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "byte-comparison path is not a plain regular file",
            )
        first_path_snapshot = core_fs.FileSnapshot.from_path(first, first_path_before)
        second_path_snapshot = core_fs.FileSnapshot.from_path(second, second_path_before)
        with fs.open_readonly(first) as first_handle, fs.open_readonly(second) as second_handle:
            first_before = os.fstat(first_handle.fileno())
            second_before = os.fstat(second_handle.fileno())
            first_before_snapshot = core_fs.FileSnapshot.from_file(
                first_handle,
                path=first,
                stat_result=first_before,
            )
            second_before_snapshot = core_fs.FileSnapshot.from_file(
                second_handle,
                path=second,
                stat_result=second_before,
            )
            if not first_path_snapshot.same_content_generation(
                first_before_snapshot
            ) or not second_path_snapshot.same_content_generation(second_before_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "byte-comparison path changed while it was opened",
                )
            while True:
                first_chunk = first_handle.read(COPY_CHUNK_SIZE)
                second_chunk = second_handle.read(COPY_CHUNK_SIZE)
                if first_chunk != second_chunk:
                    raise DatasetExecutionError(
                        ExecutionCode.CONTENT_MISMATCH,
                        "payloads differ during final byte comparison",
                        first,
                    )
                if not first_chunk:
                    break
            first_after = os.fstat(first_handle.fileno())
            second_after = os.fstat(second_handle.fileno())
            first_after_snapshot = core_fs.FileSnapshot.from_file(
                first_handle,
                path=first,
                stat_result=first_after,
            )
            second_after_snapshot = core_fs.FileSnapshot.from_file(
                second_handle,
                path=second,
                stat_result=second_after,
            )
            if not first_before_snapshot.same_content_generation(
                first_after_snapshot
            ) or not second_before_snapshot.same_content_generation(second_after_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "payload changed during final byte comparison",
                )
        first_path_after = os.stat(first, follow_symlinks=False)
        second_path_after = os.stat(second, follow_symlinks=False)
        if not first_path_snapshot.same_content_generation(
            core_fs.FileSnapshot.from_path(first, first_path_after)
        ) or not second_path_snapshot.same_content_generation(
            core_fs.FileSnapshot.from_path(second, second_path_after)
        ):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "byte-comparison path changed after it was read",
            )
    except DatasetExecutionError:
        raise
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error)) from error


def _require_path_matches_stat(
    path: Path,
    expected: os.stat_result,
    *,
    code: ExecutionCode = ExecutionCode.SOURCE_CHANGED,
) -> os.stat_result:
    candidate = _absolute(path)
    _validate_existing_path_chain(candidate)
    try:
        current = os.stat(candidate, follow_symlinks=False)
    except OSError as error:
        raise DatasetExecutionError(code, str(error), candidate) from error
    if not _is_plain_file(current) or current.st_dev != expected.st_dev or current.st_ino != expected.st_ino:
        raise DatasetExecutionError(code, "path no longer names the verified payload", candidate)
    return current


def _require_open_version(first: os.stat_result, second: os.stat_result, path: Path) -> None:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if os.name != "nt":
        fields += ("st_ctime_ns",)
    if any(getattr(first, field, None) != getattr(second, field, None) for field in fields):
        raise DatasetExecutionError(
            ExecutionCode.SOURCE_CHANGED,
            "open payload changed during verification",
            path,
        )


def _rebind_open_version_after_atomic_rename(
    handle: BinaryIO,
    before: os.stat_result,
    path: Path,
    commit: RenameCommit,
) -> os.stat_result:
    """Bind an open payload to the generation created by our own atomic rename."""

    source_identity = (int(before.st_dev), int(before.st_ino))
    if (
        not commit.postcondition_verified
        or tuple(map(int, commit.source_identity)) != source_identity
        or commit.destination_name != path.name
    ):
        raise DatasetExecutionError(
            ExecutionCode.SOURCE_CHANGED,
            "atomic rename did not verify the expected payload and destination",
            path,
        )
    current_path = _require_path_matches_stat(path, before)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_flags",
    )
    if any(getattr(before, field, None) != getattr(current_path, field, None) for field in stable_fields):
        raise DatasetExecutionError(
            ExecutionCode.SOURCE_CHANGED,
            "payload changed during atomic rename",
            path,
        )
    try:
        current_open = os.fstat(handle.fileno())
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), path) from error
    # The path and held handle must agree on the complete post-rename stat
    # version, including the new POSIX ctime. Only the comparison with
    # ``before`` intentionally excludes that rename-generated field. Windows
    # keeps the opened object under a no-write lease and rechecks its bytes.
    _require_open_version(current_path, current_open, path)
    return current_open


def _reverify_open_equal_pair(
    first_handle: BinaryIO,
    second_handle: BinaryIO,
    first_opened: os.stat_result,
    second_opened: os.stat_result,
    *,
    first_expected: DatasetFileProof,
    second_expected: DatasetFileProof,
    first_path: Path,
    second_path: Path,
    require_byte_equal: bool = True,
) -> None:
    first_handle.seek(0)
    second_handle.seek(0)
    first_digest = hashlib.sha256()
    second_digest = hashlib.sha256()
    while True:
        first_chunk = first_handle.read(COPY_CHUNK_SIZE) if require_byte_equal else b""
        second_chunk = second_handle.read(COPY_CHUNK_SIZE)
        if require_byte_equal and first_chunk != second_chunk:
            raise DatasetExecutionError(
                ExecutionCode.CONTENT_MISMATCH,
                "payloads differ during final byte comparison",
                first_path,
            )
        if not second_chunk:
            break
        if require_byte_equal:
            first_digest.update(first_chunk)
        second_digest.update(second_chunk)
    _require_open_version(first_opened, os.fstat(first_handle.fileno()), first_path)
    _require_open_version(second_opened, os.fstat(second_handle.fileno()), second_path)
    if (
        require_byte_equal and first_digest.hexdigest() != first_expected.digest_hex
    ) or second_digest.hexdigest() != second_expected.digest_hex:
        raise DatasetExecutionError(
            ExecutionCode.CONTENT_MISMATCH,
            "verified payload digest does not match its immutable proof",
        )


@contextlib.contextmanager
def _open_verified_equal_pair(
    first: Path,
    second: Path,
    fs: FileSystemAdapter,
    *,
    first_expected: DatasetFileProof,
    second_expected: DatasetFileProof,
    first_original_identity: bool,
    second_original_identity: bool,
    first_created_identity: Optional[Tuple[int, int]] = None,
    second_created_identity: Optional[Tuple[int, int]] = None,
    require_byte_equal: bool = True,
) -> Iterator[Tuple[BinaryIO, BinaryIO, os.stat_result, os.stat_result]]:
    """Keep two byte-equal, identity-bound payloads open across a namespace mutation."""

    first = _absolute(first)
    second = _absolute(second)
    _validate_existing_path_chain(first)
    _validate_existing_path_chain(second)
    try:
        first_path = os.stat(first, follow_symlinks=False)
        second_path = os.stat(second, follow_symlinks=False)
        if not _is_plain_file(first_path) or not _is_plain_file(second_path):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "verified payload is not a plain regular file",
            )
        if (
            require_byte_equal and first_path.st_size != first_expected.size
        ) or second_path.st_size != second_expected.size:
            raise DatasetExecutionError(
                ExecutionCode.CONTENT_MISMATCH,
                "verified payload size does not match its immutable proof",
            )
        if first_original_identity and not _identity_matches(first_path, first, first_expected):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "first payload identity changed",
                first,
            )
        if (
            first_created_identity is not None
            and (
                int(first_path.st_dev),
                int(first_path.st_ino),
            )
            != first_created_identity
        ):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "transaction-created payload identity changed",
                first,
            )
        if second_original_identity and not _identity_matches(second_path, second, second_expected):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "surviving payload identity changed",
                second,
            )
        if (
            second_created_identity is not None
            and (
                int(second_path.st_dev),
                int(second_path.st_ino),
            )
            != second_created_identity
        ):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "transaction-created survivor identity changed",
                second,
            )
        with fs.open_readonly(first) as first_handle, fs.open_readonly(second) as second_handle:
            first_opened = os.fstat(first_handle.fileno())
            second_opened = os.fstat(second_handle.fileno())
            if (
                first_opened.st_dev != first_path.st_dev
                or first_opened.st_ino != first_path.st_ino
                or second_opened.st_dev != second_path.st_dev
                or second_opened.st_ino != second_path.st_ino
            ):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "verified path changed while it was opened",
                )
            _reverify_open_equal_pair(
                first_handle,
                second_handle,
                first_opened,
                second_opened,
                first_expected=first_expected,
                second_expected=second_expected,
                first_path=first,
                second_path=second,
                require_byte_equal=require_byte_equal,
            )
            _require_path_matches_stat(first, first_opened)
            _require_path_matches_stat(second, second_opened)
            yield first_handle, second_handle, first_opened, second_opened
    except DatasetExecutionError:
        raise
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error)) from error


@contextlib.contextmanager
def _process_lock(path: Path, fs: FileSystemAdapter) -> Iterator[None]:
    """Hold a cross-process one-byte advisory lock for all journal mutations."""

    _validate_existing_path_chain(path.parent)
    if fs.lexists(path):
        existing_stat = os.stat(path, follow_symlinks=False)
        if not _is_plain_file(existing_stat) or int(getattr(existing_stat, "st_nlink", 0)) != 1:
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "executor lock path is not a private single-link regular file",
                path,
            )
    flags = (
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as error:
        raise DatasetExecutionError(ExecutionCode.BUSY, str(error), path) from error
    locked = False
    try:
        opened_stat = os.fstat(fd)
        current_stat = os.stat(path, follow_symlinks=False)
        if (
            not _is_plain_file(opened_stat)
            or not _is_plain_file(current_stat)
            or opened_stat.st_dev != current_stat.st_dev
            or opened_stat.st_ino != current_stat.st_ino
            or int(getattr(opened_stat, "st_nlink", 0)) != 1
            or int(getattr(current_stat, "st_nlink", 0)) != 1
        ):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "executor lock path changed while it was opened",
                path,
            )
        if os.name == "nt":
            import msvcrt

            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as error:
                raise DatasetExecutionError(ExecutionCode.BUSY, "another dataset execution is active", path) from error
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                raise DatasetExecutionError(ExecutionCode.BUSY, "another dataset execution is active", path) from error
        current_stat = os.stat(path, follow_symlinks=False)
        if (
            not _is_plain_file(current_stat)
            or current_stat.st_dev != opened_stat.st_dev
            or current_stat.st_ino != opened_stat.st_ino
            or int(getattr(current_stat, "st_nlink", 0)) != 1
        ):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "executor lock path changed while it was being locked",
                path,
            )
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
        try:
            fs.fsync_directory(path.parent)
        except OSError:
            pass


VolumeChecker = Callable[[DatasetFileProof, Path, os.stat_result], bool]
FaultHook = Callable[[str, _FileRecord], None]


def _default_volume_checker(
    source: DatasetFileProof,
    _destination_ancestor: Path,
    destination_stat: os.stat_result,
) -> bool:
    return source.stat_device == int(destination_stat.st_dev)


class DatasetBundleExecutor:
    """Apply a :class:`DatasetPlan` as one recoverable filesystem transaction."""

    def __init__(
        self,
        *,
        state_root: Optional[str | Path] = None,
        service: Optional[DatasetModeService] = None,
        inspector: Optional[FilesystemInspector] = None,
        fs: Optional[FileSystemAdapter] = None,
        volume_checker: Optional[VolumeChecker] = None,
        fault_hook: Optional[FaultHook] = None,
        copy_chunk_size: int = COPY_CHUNK_SIZE,
    ) -> None:
        if copy_chunk_size <= 0:
            raise ValueError("copy chunk size must be positive")
        if service is not None and inspector is not None and service.inspector is not inspector:
            raise ValueError("service and inspector must use the same FilesystemInspector")
        self.inspector = inspector or (service.inspector if service is not None else FilesystemInspector())
        self.service = service or DatasetModeService(inspector=self.inspector)
        self.fs = fs or platform_file_system()
        self.configured_state_root = _state_namespace_root(state_root) if state_root is not None else None
        self.volume_checker = volume_checker or _default_volume_checker
        self.fault_hook = fault_hook
        self.copy_chunk_size = copy_chunk_size
        self._accepted_generation_tokens: Dict[str, str] = {}

    def apply(self, plan: DatasetPlan, *, execute: bool = False) -> DatasetExecutionReport:
        """Preflight a plan and, only with ``execute=True``, apply it.

        The plan's immutable ``dry_run`` flag is authoritative.  Passing ``execute=True`` cannot
        turn a dry-run plan into a mutating plan.
        """

        self._accepted_generation_tokens = {}
        file_count = sum(len(action.files) for action in plan.actions)
        if file_count > MAX_EXECUTION_TRANSACTION_FILES:
            return self._failure_report(
                plan.plan_id,
                ExecutionCode.PLAN_INVALID,
                (
                    "dataset execution contains {} file records; one "
                    "recoverable transaction supports at most {}. Split "
                    "the plan into smaller transactions."
                ).format(
                    file_count,
                    MAX_EXECUTION_TRANSACTION_FILES,
                ),
            )
        try:
            self._validate_plan_user_paths(plan)
            # This call is deliberately the first interaction with plan paths.  It re-hashes every
            # source/reference and checks every planned destination.
            validation = self.service.revalidate(plan)
        except DatasetExecutionError as error:
            return self._failure_report(
                plan.plan_id,
                error.code,
                str(error),
                path=error.path,
            )
        except Exception as error:
            return self._failure_report(
                plan.plan_id,
                ExecutionCode.PLAN_INVALID,
                "dataset plan revalidation failed: {}".format(error),
            )

        try:
            state_root = self._state_root_for(plan)
        except DatasetExecutionError as error:
            return self._failure_report(
                plan.plan_id,
                error.code,
                str(error),
                path=error.path,
            )
        recovery_retry = False
        recovery_document: Optional[_ExecutionDocument] = None
        try:
            existing = self._load_document_if_present(state_root, plan.plan_id)
            if existing is not None:
                self._require_document_matches_plan(existing, plan, state_root)
                replay = self._handle_existing_document(
                    existing,
                    plan,
                    validation_valid=validation.valid,
                    validation_issues=validation.issues,
                    execute=execute and not plan.dry_run,
                )
                if replay is not None:
                    return replay
                recovery_retry = True
                recovery_document = existing
                # A successful durable rollback preserves file identity and bytes, but an atomic
                # rename legitimately advances the filesystem generation.  Bind those newly
                # observed generations only after the immutable operation document has been
                # matched and every restored source has been fully re-hashed.
                self._bind_recovered_generations(plan)
                validation = PlanValidation(True, ())
            if not validation.valid:
                return DatasetExecutionReport(
                    plan_id=plan.plan_id,
                    state=ExecutionState.FAILED,
                    code=ExecutionCode.PLAN_INVALID,
                    message="dataset plan revalidation found {} issue(s)".format(len(validation.issues)),
                    changed=False,
                    issues=validation.issues,
                )

            records = self._preflight(plan, state_root)
            preview_document = recovery_document or self._build_document(
                plan,
                state_root,
                records,
            )
            lifecycle_budgets = self._validate_document_lifecycle_budgets(
                preview_document,
            )
            reservation = lifecycle_budgets["retry" if recovery_retry else "initial"]
            # This is intentionally read-only even when the state root does
            # not exist. It proves the same actual journal capacity that the
            # mutating path will recheck after taking its operation lock.
            self._journal_for(
                state_root,
                preview_document.plan_id,
            ).ensure_capacity(
                preview_document,
                additional_events=reservation.events,
                additional_bytes=reservation.bytes,
            )
            planned_results = tuple(self._file_result(record, FileExecutionState.PLANNED, False) for record in records)
            if plan.dry_run:
                return DatasetExecutionReport(
                    plan_id=plan.plan_id,
                    state=ExecutionState.DRY_RUN,
                    code=ExecutionCode.NONE,
                    message="dry-run plan fully revalidated; no filesystem state was changed",
                    changed=False,
                    files=planned_results,
                )
            if not execute:
                return DatasetExecutionReport(
                    plan_id=plan.plan_id,
                    state=ExecutionState.READY,
                    code=ExecutionCode.EXECUTION_NOT_EXPLICIT,
                    message="plan is safe to apply, but execute=True was not supplied",
                    changed=False,
                    files=planned_results,
                )
            return self._execute_fresh(
                plan,
                state_root,
                records,
                recovery_retry=recovery_retry,
                existing_document=preview_document,
            )
        except DatasetExecutionError as error:
            return self._failure_report(
                plan.plan_id,
                error.code,
                str(error),
                path=error.path,
                issues=validation.issues if not validation.valid else (),
            )
        except (OSError, ValueError) as error:
            return self._failure_report(plan.plan_id, ExecutionCode.IO_ERROR, str(error))

    @staticmethod
    def _validate_plan_user_paths(plan: DatasetPlan) -> None:
        for root in plan.allowed_roots:
            _require_user_path_outside_internal_state(root, "dataset allowed root")
        _require_user_path_outside_internal_state(plan.destination_root, "dataset destination root")
        for split_name in plan.split_manifest.cluster_splits().values():
            _require_dataset_split_outside_internal_state(split_name)
        for action in plan.actions:
            _require_dataset_split_outside_internal_state(action.split)
            for item in action.files:
                _require_user_path_outside_internal_state(item.source.path, "dataset source")
                _require_user_path_outside_internal_state(
                    item.source.resolved_path,
                    "resolved dataset source",
                )
                if item.reference is not None:
                    _require_user_path_outside_internal_state(
                        item.reference.path,
                        "dataset keeper reference",
                    )
                    _require_user_path_outside_internal_state(
                        item.reference.resolved_path,
                        "resolved dataset keeper reference",
                    )
                if item.destination is not None:
                    _require_user_path_outside_internal_state(
                        item.destination,
                        "dataset destination",
                    )

    @staticmethod
    def _validate_record_user_paths(record: _FileRecord) -> None:
        _require_user_path_outside_internal_state(record.source.path, "dataset source")
        _require_user_path_outside_internal_state(
            record.source.resolved_path,
            "resolved dataset source",
        )
        if record.reference is not None:
            _require_user_path_outside_internal_state(
                record.reference.path,
                "dataset keeper reference",
            )
            _require_user_path_outside_internal_state(
                record.reference.resolved_path,
                "resolved dataset keeper reference",
            )
        if record.destination is not None:
            _require_user_path_outside_internal_state(
                record.destination,
                "dataset destination",
            )
        if record.comparison_path is not None:
            _require_user_path_outside_internal_state(
                record.comparison_path,
                "dataset comparison path",
            )

    @staticmethod
    def _proof_matches_except_generation(
        current: DatasetFileProof,
        expected: DatasetFileProof,
    ) -> bool:
        current_fields = current.to_dict()
        expected_fields = expected.to_dict()
        for field_name in ("ctime_ns", "generation_token"):
            current_fields.pop(field_name)
            expected_fields.pop(field_name)
        return current_fields == expected_fields

    def _bind_recovered_generations(self, plan: DatasetPlan) -> None:
        roots = tuple(Path(root) for root in plan.allowed_roots)
        expected: Dict[str, DatasetFileProof] = {}
        for action in plan.actions:
            for item in action.files:
                expected[_path_key(item.source.path)] = item.source
                if item.reference is not None:
                    expected[_path_key(item.reference.path)] = item.reference
        accepted: Dict[str, str] = {}
        for key, proof in sorted(expected.items()):
            try:
                current = self.inspector.snapshot(
                    Path(proof.path),
                    roots,
                    capture_content=False,
                    maximum_capture_bytes=1,
                ).proof
            except DatasetSafetyError as error:
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    str(error),
                    error.path,
                ) from error
            if not self._proof_matches_except_generation(current, proof):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "restored source no longer matches the immutable content and identity proof",
                    Path(proof.path),
                )
            accepted[key] = current.generation_token
        self._accepted_generation_tokens = accepted

    def _runtime_proof_matches(
        self,
        current: DatasetFileProof,
        expected: DatasetFileProof,
    ) -> bool:
        accepted = self._accepted_generation_tokens.get(_path_key(expected.path))
        if accepted is None:
            return current == expected
        return self._proof_matches_except_generation(current, expected) and current.generation_token == accepted

    def _validate_runtime_proof(
        self,
        proof: DatasetFileProof,
        allowed_roots: Sequence[Path],
    ) -> None:
        if _path_key(proof.path) not in self._accepted_generation_tokens:
            self.inspector.validate_proof(proof, allowed_roots)
            return
        current = self.inspector.snapshot(
            Path(proof.path),
            allowed_roots,
            capture_content=False,
            maximum_capture_bytes=1,
        ).proof
        if not self._runtime_proof_matches(current, proof):
            raise DatasetSafetyError(
                "source_changed",
                "source proof no longer matches the recovered filesystem generation",
                Path(proof.path),
            )

    def list_operations(self, *, destination_root: Optional[str | Path] = None) -> Tuple[DatasetOperationSummary, ...]:
        """List immutable operation documents without creating any state."""

        if self.configured_state_root is not None:
            state_root = self.configured_state_root
        elif destination_root is not None:
            state_root = _absolute(destination_root).joinpath(STATE_DIRECTORY_NAME)
        else:
            raise ValueError("destination_root is required when the executor has no configured state_root")
        _validate_state_namespace(state_root)
        operations = state_root.joinpath("operations")
        if not os.path.lexists(operations):
            return ()
        _validate_private_directory(state_root, "dataset executor state directory")
        _validate_private_directory(operations, "dataset executor operations directory")
        summaries = []
        operation_directories = []
        with os.scandir(operations) as entries:
            for entry in entries:
                if len(operation_directories) >= MAX_PERSISTED_EXECUTION_OPERATIONS:
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "dataset executor state exceeds the {} operation limit".format(
                            MAX_PERSISTED_EXECUTION_OPERATIONS
                        ),
                        operations,
                    )
                operation_directories.append(Path(entry.path))
        for operation_directory in sorted(
            operation_directories,
            key=lambda item: item.name,
        ):
            path = operation_directory.joinpath(OPERATION_DOCUMENT_FILENAME)
            try:
                _validate_private_directory(
                    operation_directory,
                    "dataset executor operation directory",
                )
                document = self._read_document(path)
                state, message = self._document_state(
                    document,
                    self._journal_for(state_root, document.plan_id),
                )
                summaries.append(
                    DatasetOperationSummary(
                        plan_id=document.plan_id,
                        state=state,
                        document_path=str(path),
                        created_ns=document.created_ns,
                        file_count=len(document.files),
                        message=message,
                    )
                )
            except (DatasetExecutionError, ValueError) as error:
                summaries.append(
                    DatasetOperationSummary(
                        plan_id=operation_directory.name,
                        state=ExecutionState.RECOVERY_REQUIRED,
                        document_path=str(path),
                        created_ns=0,
                        file_count=0,
                        message=str(error),
                    )
                )
        return tuple(summaries)

    def restore(
        self,
        plan_id: str,
        *,
        destination_root: Optional[str | Path] = None,
        execute: bool = False,
    ) -> DatasetExecutionReport:
        """Restore every source in a committed plan and remove only transaction-created copies."""

        restore_started = False
        try:
            state_root = self._resolve_state_root(destination_root)
            document = self._load_required_document(state_root, plan_id)
            journal = self._journal_for(state_root, document.plan_id)
            state, _message = self._document_state(
                document,
                journal,
            )
            files = tuple(self._file_result(record, FileExecutionState.PLANNED, False) for record in document.files)
            if state is ExecutionState.FINALIZED:
                raise DatasetExecutionError(
                    ExecutionCode.INVALID_STATE,
                    "a finalized operation cannot be restored",
                )
            if state in {ExecutionState.RESTORED, ExecutionState.ROLLED_BACK}:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.RESTORED,
                    code=ExecutionCode.NONE,
                    message="all source bundles are already restored",
                    changed=False,
                    files=tuple(
                        self._file_result(record, FileExecutionState.RESTORED, False) for record in document.files
                    ),
                )
            reservation = self._journal_phase_budgets(document)["restore"]
            journal.ensure_capacity(
                document,
                additional_events=reservation.events,
                additional_bytes=reservation.bytes,
            )
            if not execute:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.READY,
                    code=ExecutionCode.EXECUTION_NOT_EXPLICIT,
                    message="restore preflight succeeded, but execute=True was not supplied",
                    changed=False,
                    files=files,
                )
            _validate_private_directory(state_root, "dataset executor state directory")
            with _process_lock(
                self._lock_path(state_root, document.plan_id),
                self.fs,
            ):
                persisted = self._load_required_document(
                    state_root,
                    document.plan_id,
                )
                if persisted.to_dict() != document.to_dict():
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "dataset execution document changed before restore",
                        self._document_path(state_root, document.plan_id),
                    )
                journal = self._journal_for(state_root, document.plan_id)
                journal.ensure_capacity(
                    document,
                    additional_events=reservation.events,
                    additional_bytes=reservation.bytes,
                )
                self._append_event_once(
                    document,
                    journal,
                    _JournalEvent.RESTORE_PREPARED,
                )
                restore_started = True
                self._rollback_document(document, journal, restore=True)
            return DatasetExecutionReport(
                plan_id=plan_id,
                state=ExecutionState.RESTORED,
                code=ExecutionCode.NONE,
                message="all dataset bundles were restored",
                changed=True,
                files=tuple(self._file_result(record, FileExecutionState.RESTORED, True) for record in document.files),
            )
        except DatasetExecutionError as error:
            if restore_started:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.RECOVERY_REQUIRED,
                    code=error.code,
                    message=str(error),
                    changed=True,
                )
            return self._failure_report(plan_id, error.code, str(error), path=error.path)
        except (OSError, ValueError) as error:
            if restore_started:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.RECOVERY_REQUIRED,
                    code=ExecutionCode.IO_ERROR,
                    message=str(error),
                    changed=True,
                )
            return self._failure_report(plan_id, ExecutionCode.IO_ERROR, str(error))

    def finalize(
        self,
        plan_id: str,
        *,
        destination_root: Optional[str | Path] = None,
        execute: bool = False,
    ) -> DatasetExecutionReport:
        """Explicitly purge quarantined payloads after a complete all-file preflight.

        ``apply`` never invokes this method.  Callers must surface the irreversible nature of this
        separate operation and pass ``execute=True``.
        """

        finalize_started = False
        finalize_changed = False
        try:
            state_root = self._resolve_state_root(destination_root)
            document = self._load_required_document(state_root, plan_id)
            journal = self._journal_for(state_root, document.plan_id)
            state, _message = self._document_state(document, journal)
            if state is ExecutionState.FINALIZED:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.FINALIZED,
                    code=ExecutionCode.NONE,
                    message="operation is already finalized",
                    changed=False,
                    files=tuple(
                        self._file_result(record, FileExecutionState.FINALIZED, False) for record in document.files
                    ),
                )
            if state not in {ExecutionState.APPLIED, ExecutionState.ALREADY_APPLIED, ExecutionState.RECOVERY_REQUIRED}:
                raise DatasetExecutionError(
                    ExecutionCode.INVALID_STATE,
                    "only a completely applied operation can be finalized",
                )
            self._preflight_finalize(document, journal)
            reservation = self._journal_phase_budgets(document)["finalize"]
            journal.ensure_capacity(
                document,
                additional_events=reservation.events,
                additional_bytes=reservation.bytes,
            )
            if not execute:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.READY,
                    code=ExecutionCode.EXECUTION_NOT_EXPLICIT,
                    message="finalize preflight succeeded, but execute=True was not supplied",
                    changed=False,
                    files=tuple(
                        self._file_result(record, FileExecutionState.PLANNED, False) for record in document.files
                    ),
                )
            _validate_private_directory(state_root, "dataset executor state directory")
            changed = False

            def mark_finalize_mutation() -> None:
                nonlocal finalize_changed
                finalize_changed = True

            with _process_lock(
                self._lock_path(state_root, document.plan_id),
                self.fs,
            ):
                persisted = self._load_required_document(
                    state_root,
                    document.plan_id,
                )
                if persisted.to_dict() != document.to_dict():
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "dataset execution document changed before finalize",
                        self._document_path(state_root, document.plan_id),
                    )
                # Repeat the complete preflight after taking the cross-process lock.  The lock
                # serializes dupeGuru, while the held handles and identity checks below detect
                # ordinary external replacements at each irreversible boundary.
                self._preflight_finalize(document, journal)
                journal.ensure_capacity(
                    document,
                    additional_events=reservation.events,
                    additional_bytes=reservation.bytes,
                )
                self._append_event_once(
                    document,
                    journal,
                    _JournalEvent.FINALIZE_PREPARED,
                )
                finalize_started = True
                event_index = self._journal_event_index(document, journal)
                destination_records = {
                    _path_key(record.destination): record for record in document.files if record.destination is not None
                }
                for record in document.files:
                    if record.quarantine_path is None:
                        continue
                    record_changed = self._finalize_record(
                        document,
                        journal,
                        record,
                        on_mutation=mark_finalize_mutation,
                        destination_records=destination_records,
                        event_index=event_index,
                    )
                    changed = changed or record_changed
                    finalize_changed = finalize_changed or record_changed
                self._append_event_once(
                    document,
                    journal,
                    _JournalEvent.FINALIZED,
                )
            return DatasetExecutionReport(
                plan_id=plan_id,
                state=ExecutionState.FINALIZED,
                code=ExecutionCode.NONE,
                message="quarantined payloads were explicitly finalized",
                changed=changed,
                files=tuple(
                    self._file_result(record, FileExecutionState.FINALIZED, changed) for record in document.files
                ),
            )
        except DatasetExecutionError as error:
            if finalize_started:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.RECOVERY_REQUIRED,
                    code=error.code,
                    message=str(error),
                    changed=finalize_changed,
                )
            return self._failure_report(plan_id, error.code, str(error), path=error.path)
        except (OSError, ValueError) as error:
            if finalize_started:
                return DatasetExecutionReport(
                    plan_id=plan_id,
                    state=ExecutionState.RECOVERY_REQUIRED,
                    code=ExecutionCode.IO_ERROR,
                    message=str(error),
                    changed=finalize_changed,
                )
            return self._failure_report(plan_id, ExecutionCode.IO_ERROR, str(error))

    # ``list`` is intentionally supplied as an ergonomic alias while avoiding a built-in name
    # inside the implementation.
    list = list_operations

    def _state_root_for(self, plan: DatasetPlan) -> Path:
        if self.configured_state_root is not None:
            state_root = self.configured_state_root
        else:
            state_root = _absolute(plan.destination_root).joinpath(STATE_DIRECTORY_NAME)
        _validate_state_namespace(state_root)
        return state_root

    def _resolve_state_root(self, destination_root: Optional[str | Path]) -> Path:
        if self.configured_state_root is not None:
            state_root = self.configured_state_root
        else:
            if destination_root is None:
                raise DatasetExecutionError(
                    ExecutionCode.INVALID_STATE,
                    "destination_root is required when the executor has no configured state_root",
                )
            state_root = _absolute(destination_root).joinpath(STATE_DIRECTORY_NAME)
        _validate_state_namespace(state_root)
        return state_root

    def _journal_for(self, state_root: Path, plan_id: str) -> _AppendOnlyJournal:
        return _AppendOnlyJournal(
            self._document_path(state_root, plan_id).parent.joinpath(OPERATION_JOURNAL_FILENAME),
            self.fs,
        )

    def _lock_path(self, state_root: Path, plan_id: str) -> Path:
        return self._document_path(state_root, plan_id).parent.joinpath("executor.lock")

    @staticmethod
    def _append_event_once(
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        event: _JournalEvent,
        details: Optional[Mapping[str, Any]] = None,
        *,
        match_event_only: bool = False,
    ) -> bool:
        """Append an idempotent lifecycle record and report whether it was new."""

        payload = dict(details or {})
        for existing in journal.events_for(document):
            if existing.event is not event:
                continue
            if match_event_only or dict(existing.details) == payload:
                return False
        journal.append(document, event, payload)
        return True

    @staticmethod
    def _journal_event_index(
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
    ) -> _JournalEventIndex:
        return _JournalEventIndex.build(
            document,
            journal.events_for(document),
        )

    @staticmethod
    def _new_tombstone_path(
        document: _ExecutionDocument,
        record: _FileRecord,
        original_path: Path,
        marker: str,
    ) -> Path:
        return original_path.with_name(
            ".dg-{}-{}-{:x}-{}".format(
                marker,
                document.plan_id[:12],
                record.ordinal,
                uuid.uuid4().hex,
            )
        )

    @staticmethod
    def _projected_tombstone_path(
        document: _ExecutionDocument,
        record: _FileRecord,
        original_path: Path,
        marker: str,
    ) -> Path:
        """Return the longest fixed-shape tombstone path without reserving it."""

        return original_path.with_name(
            ".dg-{}-{}-{:x}-{}".format(
                marker,
                document.plan_id[:12],
                record.ordinal,
                "f" * 32,
            )
        )

    def _journal_phase_budgets(
        self,
        document: _ExecutionDocument,
    ) -> Mapping[str, _JournalBudget]:
        """Project every bounded lifecycle branch from immutable records.

        The initial reservation includes a complete failed attempt, rollback,
        one recovery retry, and the larger of explicit restore/finalize.  This
        intentionally over-counts mutually exclusive events so interruption
        recovery can never exhaust the journal after a namespace mutation.
        """

        def add(
            budget: _JournalBudget,
            event: _JournalEvent,
            details: Optional[Mapping[str, Any]] = None,
        ) -> _JournalBudget:
            payload = dict(details or {})
            return _JournalBudget(
                budget.events + 1,
                budget.bytes
                + _AppendOnlyJournal.projected_record_size(
                    document,
                    event,
                    payload,
                ),
            )

        max_identity = 18_446_744_073_709_551_615

        def cleanup(
            budget: _JournalBudget,
            record: _FileRecord,
            *,
            kind: str,
            original_path: Path,
        ) -> _JournalBudget:
            marker = "cd" if kind == "destination" else "ct"
            tombstone = self._projected_tombstone_path(
                document,
                record,
                original_path,
                marker,
            )
            binding = {
                "ordinal": record.ordinal,
                "kind": kind,
                "original_path": str(original_path),
                "tombstone_path": str(tombstone),
                "stat_device": max_identity,
                "stat_inode": max_identity,
            }
            budget = add(
                budget,
                _JournalEvent.CLEANUP_TOMBSTONE_PREPARED,
                binding,
            )
            budget = add(
                budget,
                _JournalEvent.CLEANUP_TOMBSTONED,
                binding,
            )
            return add(
                budget,
                _JournalEvent.CLEANUP_PURGED,
                {
                    "ordinal": record.ordinal,
                    "kind": kind,
                    "tombstone_path": str(tombstone),
                },
            )

        apply_budget = add(
            _JournalBudget(),
            _JournalEvent.PREPARED,
            {"file_count": len(document.files)},
        )
        for record in document.files:
            if record.strategy is _Strategy.SAME_VOLUME_RENAME:
                apply_budget = add(
                    apply_budget,
                    _JournalEvent.DESTINATION_PUBLISHED,
                    {
                        "ordinal": record.ordinal,
                        "strategy": record.strategy.value,
                    },
                )
            elif record.strategy is _Strategy.CROSS_VOLUME_COPY:
                assert record.temporary_path is not None
                created = {
                    "ordinal": record.ordinal,
                    "stat_device": max_identity,
                    "stat_inode": max_identity,
                }
                apply_budget = add(
                    apply_budget,
                    _JournalEvent.TEMPORARY_CREATED,
                    created,
                )
                apply_budget = add(
                    apply_budget,
                    _JournalEvent.DESTINATION_PREPARED,
                    created,
                )
                apply_budget = add(
                    apply_budget,
                    _JournalEvent.DESTINATION_PUBLISHED,
                    {
                        "ordinal": record.ordinal,
                        "strategy": record.strategy.value,
                    },
                )
            if record.strategy in {
                _Strategy.QUARANTINE,
                _Strategy.CROSS_VOLUME_COPY,
            }:
                apply_budget = add(
                    apply_budget,
                    _JournalEvent.SOURCE_QUARANTINED,
                    {
                        "ordinal": record.ordinal,
                        "quarantine_path": record.quarantine_path,
                    },
                )
        apply_budget = add(apply_budget, _JournalEvent.APPLIED)
        apply_budget = add(apply_budget, _JournalEvent.APPLIED_RECOVERED)

        rollback_budget = add(
            _JournalBudget(),
            _JournalEvent.ROLLBACK_PREPARED,
            {"reason": "crash_replay"},
        )
        for record in reversed(document.files):
            if record.strategy in {
                _Strategy.QUARANTINE,
                _Strategy.CROSS_VOLUME_COPY,
            }:
                rollback_budget = add(
                    rollback_budget,
                    _JournalEvent.FILE_ROLLED_BACK,
                    {"ordinal": record.ordinal, "phase": "source"},
                )
        for record in reversed(document.files):
            if record.strategy is _Strategy.CROSS_VOLUME_COPY:
                assert record.destination is not None
                assert record.temporary_path is not None
                rollback_budget = cleanup(
                    rollback_budget,
                    record,
                    kind="destination",
                    original_path=Path(record.destination),
                )
                rollback_budget = cleanup(
                    rollback_budget,
                    record,
                    kind="temporary",
                    original_path=Path(record.temporary_path),
                )
            rollback_budget = add(
                rollback_budget,
                _JournalEvent.FILE_ROLLED_BACK,
                {"ordinal": record.ordinal, "phase": "destination"},
            )
        rollback_budget = add(rollback_budget, _JournalEvent.ROLLED_BACK)

        restore_budget = add(
            _JournalBudget(),
            _JournalEvent.RESTORE_PREPARED,
        )
        for record in reversed(document.files):
            if record.strategy in {
                _Strategy.QUARANTINE,
                _Strategy.CROSS_VOLUME_COPY,
            }:
                restore_budget = add(
                    restore_budget,
                    _JournalEvent.FILE_RESTORED,
                    {"ordinal": record.ordinal, "phase": "source"},
                )
        for record in reversed(document.files):
            if record.strategy is _Strategy.CROSS_VOLUME_COPY:
                assert record.destination is not None
                assert record.temporary_path is not None
                restore_budget = cleanup(
                    restore_budget,
                    record,
                    kind="destination",
                    original_path=Path(record.destination),
                )
                restore_budget = cleanup(
                    restore_budget,
                    record,
                    kind="temporary",
                    original_path=Path(record.temporary_path),
                )
            restore_budget = add(
                restore_budget,
                _JournalEvent.FILE_RESTORED,
                {"ordinal": record.ordinal, "phase": "destination"},
            )
        restore_budget = add(restore_budget, _JournalEvent.RESTORED)

        finalize_budget = add(
            _JournalBudget(),
            _JournalEvent.FINALIZE_PREPARED,
        )
        for record in document.files:
            if record.quarantine_path is None:
                continue
            quarantine = Path(record.quarantine_path)
            tombstone = self._projected_tombstone_path(
                document,
                record,
                quarantine,
                "f",
            )
            binding = {
                "ordinal": record.ordinal,
                "original_path": str(quarantine),
                "tombstone_path": str(tombstone),
                "stat_device": max_identity,
                "stat_inode": max_identity,
            }
            finalize_budget = add(
                finalize_budget,
                _JournalEvent.FINALIZE_TOMBSTONE_PREPARED,
                binding,
            )
            finalize_budget = add(
                finalize_budget,
                _JournalEvent.FILE_TOMBSTONED,
                binding,
            )
            finalize_budget = add(
                finalize_budget,
                _JournalEvent.FILE_FINALIZED,
                {
                    "ordinal": record.ordinal,
                    "tombstone_path": str(tombstone),
                },
            )
        finalize_budget = add(finalize_budget, _JournalEvent.FINALIZED)

        future_budget = _JournalBudget(
            max(restore_budget.events, finalize_budget.events),
            max(restore_budget.bytes, finalize_budget.bytes),
        )
        recovery_branch = _JournalBudget(
            max(rollback_budget.events, future_budget.events),
            max(rollback_budget.bytes, future_budget.bytes),
        )
        retry_budget = apply_budget + recovery_branch
        initial_budget = apply_budget.scaled(2) + rollback_budget + recovery_branch
        return {
            "apply": apply_budget,
            "rollback": rollback_budget,
            "restore": restore_budget,
            "finalize": finalize_budget,
            "retry": retry_budget,
            "initial": initial_budget,
        }

    @staticmethod
    def _validate_tombstone_name(
        document: _ExecutionDocument,
        record: _FileRecord,
        original_path: Path,
        tombstone_path: Path,
        marker: str,
    ) -> None:
        original = _absolute(original_path)
        tombstone = _absolute(tombstone_path)
        prefix = ".dg-{}-{}-{:x}-".format(marker, document.plan_id[:12], record.ordinal)
        suffix = tombstone.name[len(prefix) :] if tombstone.name.startswith(prefix) else ""
        try:
            valid_suffix = len(suffix) == 32 and len(bytes.fromhex(suffix)) == 16
        except ValueError:
            valid_suffix = False
        if _path_key(tombstone.parent) != _path_key(original.parent) or not valid_suffix:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "journal contains an invalid transaction tombstone path",
                tombstone,
            )

    def _tombstone_history(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        original_path: Path,
        marker: str,
        prepared_event: _JournalEvent,
        tombstoned_event: _JournalEvent,
        purged_event: _JournalEvent,
        kind: Optional[str] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> Optional[_TombstoneHistory]:
        prepared: Optional[_TombstoneHistory] = None
        tombstoned = False
        purged = False
        indexed = event_index or self._journal_event_index(
            document,
            journal,
        )
        for event in indexed.for_record(record.ordinal, kind=kind):
            if event.event is prepared_event:
                expected = {
                    "ordinal",
                    "original_path",
                    "tombstone_path",
                    "stat_device",
                    "stat_inode",
                }
                if kind is not None:
                    expected.add("kind")
                if set(event.details) != expected:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "invalid tombstone preparation record",
                    )
                if kind is not None and event.details["kind"] != kind:
                    continue
                ordinal = _required_int(event.details["ordinal"], "tombstone ordinal")
                if ordinal != record.ordinal:
                    continue
                recorded_original = Path(_required_string(event.details["original_path"], "original path"))
                recorded_tombstone = Path(_required_string(event.details["tombstone_path"], "tombstone path"))
                if _path_key(recorded_original) != _path_key(original_path):
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "tombstone preparation is bound to the wrong original path",
                        recorded_original,
                    )
                self._validate_tombstone_name(
                    document,
                    record,
                    original_path,
                    recorded_tombstone,
                    marker,
                )
                candidate = _TombstoneHistory(
                    original_path=_absolute(recorded_original),
                    tombstone_path=_absolute(recorded_tombstone),
                    stat_device=_required_int(event.details["stat_device"], "tombstone device"),
                    stat_inode=_required_int(event.details["stat_inode"], "tombstone inode", minimum=1),
                    tombstoned=False,
                    purged=False,
                )
                if prepared is not None and prepared != candidate:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "conflicting tombstone preparations exist for one dataset file",
                    )
                prepared = candidate
            elif event.event is tombstoned_event:
                expected = {
                    "ordinal",
                    "original_path",
                    "tombstone_path",
                    "stat_device",
                    "stat_inode",
                }
                if kind is not None:
                    expected.add("kind")
                if set(event.details) != expected:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "invalid tombstoned record",
                    )
                if kind is not None and event.details["kind"] != kind:
                    continue
                if _required_int(event.details["ordinal"], "tombstoned ordinal") != record.ordinal:
                    continue
                if prepared is None:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "tombstoned record has no durable preparation",
                    )
                tombstoned_original = _required_string(
                    event.details["original_path"],
                    "tombstoned original path",
                )
                tombstoned_path = _required_string(
                    event.details["tombstone_path"],
                    "tombstoned path",
                )
                if (
                    _path_key(tombstoned_original) != _path_key(prepared.original_path)
                    or _path_key(tombstoned_path) != _path_key(prepared.tombstone_path)
                    or _required_int(event.details["stat_device"], "tombstoned device") != prepared.stat_device
                    or _required_int(event.details["stat_inode"], "tombstoned inode", minimum=1) != prepared.stat_inode
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "tombstoned record does not match its durable preparation",
                    )
                tombstoned = True
            elif event.event is purged_event:
                expected = {"ordinal", "tombstone_path"}
                if kind is not None:
                    expected.add("kind")
                if set(event.details) != expected:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "invalid tombstone purge record",
                    )
                if kind is not None and event.details["kind"] != kind:
                    continue
                if _required_int(event.details["ordinal"], "purged ordinal") != record.ordinal:
                    continue
                purged_path = _required_string(
                    event.details["tombstone_path"],
                    "purged tombstone path",
                )
                if prepared is None or _path_key(purged_path) != _path_key(prepared.tombstone_path):
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "tombstone purge record has no matching durable preparation",
                    )
                if not tombstoned:
                    raise DatasetExecutionError(
                        ExecutionCode.JOURNAL_CORRUPT,
                        "tombstone purge record precedes the tombstoned record",
                    )
                purged = True
        if prepared is None:
            return None
        return _TombstoneHistory(
            original_path=prepared.original_path,
            tombstone_path=prepared.tombstone_path,
            stat_device=prepared.stat_device,
            stat_inode=prepared.stat_inode,
            tombstoned=tombstoned,
            purged=purged,
        )

    @staticmethod
    def _created_identity(
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        event_type: _JournalEvent,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> Optional[Tuple[int, int]]:
        result: Optional[Tuple[int, int]] = None
        indexed = event_index or _JournalEventIndex.build(
            document,
            journal.events_for(document),
        )
        for event in indexed.for_record(record.ordinal):
            if event.event is not event_type:
                continue
            if set(event.details) != {"ordinal", "stat_device", "stat_inode"}:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "invalid transaction-created identity record",
                )
            if _required_int(event.details["ordinal"], "created ordinal") != record.ordinal:
                continue
            candidate = (
                _required_int(event.details["stat_device"], "created device"),
                _required_int(event.details["stat_inode"], "created inode", minimum=1),
            )
            if result is not None and result != candidate:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "conflicting transaction-created identities exist",
                )
            result = candidate
        return result

    def _document_path(self, state_root: Path, plan_id: str) -> Path:
        if len(plan_id) != 64:
            raise DatasetExecutionError(ExecutionCode.PLAN_INVALID, "dataset plan ID has an invalid length")
        try:
            bytes.fromhex(plan_id)
        except ValueError as error:
            raise DatasetExecutionError(ExecutionCode.PLAN_INVALID, "dataset plan ID is not hexadecimal") from error
        return state_root.joinpath("operations").joinpath(plan_id).joinpath(OPERATION_DOCUMENT_FILENAME)

    def _load_document_if_present(self, state_root: Path, plan_id: str) -> Optional[_ExecutionDocument]:
        path = self._document_path(state_root, plan_id)
        if not self.fs.lexists(path):
            return None
        return self._read_document(path)

    def _load_required_document(self, state_root: Path, plan_id: str) -> _ExecutionDocument:
        path = self._document_path(state_root, plan_id)
        if not self.fs.lexists(path):
            raise DatasetExecutionError(ExecutionCode.INVALID_STATE, "dataset execution document does not exist", path)
        return self._read_document(path)

    def _read_document(self, path: Path) -> _ExecutionDocument:
        path = _absolute(path)
        operation_directory = path.parent
        operations_directory = operation_directory.parent
        state_root = operations_directory.parent
        _validate_state_namespace(state_root)
        if (
            path.name != OPERATION_DOCUMENT_FILENAME
            or operations_directory.name != "operations"
            or len(operation_directory.name) != 64
        ):
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "execution document has an invalid operation-directory layout",
                path,
            )
        try:
            bytes.fromhex(operation_directory.name)
        except ValueError as error:
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "execution document operation directory is not a SHA-256 plan ID",
                path,
            ) from error
        _validate_private_directory(
            state_root,
            "dataset executor state directory",
        )
        _validate_private_directory(
            operations_directory,
            "dataset executor operations directory",
        )
        _validate_private_directory(
            operation_directory,
            "dataset executor operation directory",
        )
        _validate_existing_path_chain(path)
        try:
            before = os.stat(path, follow_symlinks=False)
            if not _is_plain_file(before) or int(getattr(before, "st_nlink", 0)) != 1:
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "execution document is not a private single-link regular file",
                    path,
                )
            if before.st_size > MAX_EXECUTION_DOCUMENT_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "execution document exceeds the {} byte limit".format(MAX_EXECUTION_DOCUMENT_BYTES),
                    path,
                )
            before_snapshot = core_fs.FileSnapshot.from_path(path, before)
            with self.fs.open_readonly(path) as handle:
                opened = os.fstat(handle.fileno())
                opened_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=path,
                    stat_result=opened,
                )
                if (
                    not _is_plain_file(opened)
                    or int(getattr(opened, "st_nlink", 0)) != 1
                    or not before_snapshot.same_content_generation(opened_snapshot)
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "execution document changed while it was opened",
                        path,
                    )
                payload = handle.read(MAX_EXECUTION_DOCUMENT_BYTES + 1)
                if len(payload) > MAX_EXECUTION_DOCUMENT_BYTES:
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "execution document exceeds the {} byte limit".format(MAX_EXECUTION_DOCUMENT_BYTES),
                        path,
                    )
                finished = os.fstat(handle.fileno())
                finished_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=path,
                    stat_result=finished,
                )
                if not opened_snapshot.same_content_generation(finished_snapshot):
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "execution document changed while it was read",
                        path,
                    )
            after = os.stat(path, follow_symlinks=False)
            if int(getattr(after, "st_nlink", 0)) != 1:
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "execution document gained another filesystem link",
                    path,
                )
            after_snapshot = core_fs.FileSnapshot.from_path(path, after)
            if not opened_snapshot.same_content_generation(after_snapshot):
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "execution document path changed after it was read",
                    path,
                )
            text = payload.decode("utf-8")
            value = strict_bounded_json_loads(
                text,
                limits=DATASET_DOCUMENT_JSON_LIMITS,
                label="dataset execution document",
            )
            if not isinstance(value, dict):
                raise ValueError("execution document must be an object")
            document = _ExecutionDocument.from_dict(value)
            if _path_key(document.state_root) != _path_key(state_root):
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "execution document is bound to a different state namespace",
                    path,
                )
            return document
        except DatasetExecutionError:
            raise
        except MemoryError as error:
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "execution document exceeded the JSON parser memory budget",
                path,
            ) from error
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            JsonStructureError,
            RecursionError,
            ValueError,
            OverflowError,
        ) as error:
            raise DatasetExecutionError(ExecutionCode.DOCUMENT_CONFLICT, str(error), path) from error

    def _require_document_matches_plan(
        self,
        document: _ExecutionDocument,
        plan: DatasetPlan,
        state_root: Path,
    ) -> None:
        plan_dict = plan.to_dict()
        if (
            document.plan_id != plan.plan_id
            or document.plan_hash != _content_hash(plan_dict)
            or document.plan != plan_dict
            or _path_key(document.state_root) != _path_key(state_root)
            or _path_key(document.destination_root) != _path_key(plan.destination_root)
        ):
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "existing execution document does not match the immutable dataset plan",
            )

    def _preflight(self, plan: DatasetPlan, state_root: Path) -> Tuple[_FileRecord, ...]:
        self._validate_plan_user_paths(plan)
        _validate_state_namespace(state_root)
        roots = tuple(_absolute(root) for root in plan.allowed_roots)
        destination_root = _absolute(plan.destination_root)
        _validate_plain_directory(destination_root)
        for root in roots:
            _validate_plain_directory(root)
        if any(_is_within(state_root, root) or _is_within(root, state_root) for root in roots):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "executor state root must be physically separate from every input root",
                state_root,
            )
        if self.fs.lexists(state_root):
            _validate_private_directory(state_root, "dataset executor state directory")
        else:
            _existing_ancestor(state_root)

        live_proofs: Dict[str, DatasetFileProof] = {}
        expected_proofs: Dict[str, DatasetFileProof] = {}
        for action in plan.actions:
            for item in action.files:
                expected_proofs[_path_key(item.source.path)] = item.source
                if item.reference is not None:
                    expected_proofs[_path_key(item.reference.path)] = item.reference
        for key, proof in sorted(expected_proofs.items()):
            try:
                inspected = self.inspector.snapshot(
                    Path(proof.path),
                    roots,
                    capture_content=False,
                    maximum_capture_bytes=1,
                ).proof
            except DatasetSafetyError as error:
                raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
            if not self._runtime_proof_matches(inspected, proof):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "live file proof does not match the immutable dataset plan",
                    Path(proof.path),
                )
            live_proofs[key] = inspected

        # Exact quarantine eligibility is established by a fresh streaming byte comparison.  A
        # digest match alone is never treated as deletion-grade evidence.
        for action in plan.actions:
            if action.operation is not DatasetOperation.QUARANTINE_BUNDLE:
                continue
            for item in action.files:
                assert item.reference is not None
                try:
                    if (
                        _path_key(item.source.path) in self._accepted_generation_tokens
                        or _path_key(item.reference.path) in self._accepted_generation_tokens
                    ):
                        _stable_byte_equal(
                            Path(item.source.path),
                            Path(item.reference.path),
                            self.fs,
                            item.source,
                            first_original_identity=True,
                            second_original_identity=True,
                            second_expected=item.reference,
                        )
                        equal = True
                    else:
                        equal = self.inspector.byte_equal(item.source, item.reference, roots)
                except DatasetSafetyError as error:
                    raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
                if not equal:
                    raise DatasetExecutionError(
                        ExecutionCode.CONTENT_MISMATCH,
                        "quarantine source and keeper are not byte-identical",
                        Path(item.source.path),
                    )

        move_destinations: Dict[str, str] = {}
        flattened: List[Tuple[Any, Any]] = []
        for action in plan.actions:
            for item in action.files:
                flattened.append((action, item))
                if action.operation is DatasetOperation.MOVE_BUNDLE:
                    assert item.destination is not None
                    move_destinations[_path_key(item.source.path)] = str(_absolute(item.destination))

        records: List[_FileRecord] = []
        copy_requirements: Dict[str, Tuple[Path, int]] = {}
        for ordinal, (action, item) in enumerate(flattened):
            source_path = _absolute(item.source.path)
            source_root = self._containing_root(source_path, roots)
            if action.operation is DatasetOperation.QUARANTINE_BUNDLE:
                assert item.reference is not None
                # Immutable protected keepers are reference-only and deliberately have no move
                # action.  In that case the original, freshly verified path remains the comparison
                # payload throughout execution.
                comparison_path = move_destinations.get(
                    _path_key(item.reference.path),
                    str(_absolute(item.reference.path)),
                )
                quarantine_path = (
                    source_root.joinpath(QUARANTINE_DIRECTORY_NAME)
                    .joinpath(plan.plan_id)
                    .joinpath(action.action_id)
                    .joinpath("{:06d}.payload".format(ordinal))
                )
                self._preflight_quarantine_path(quarantine_path, source_root, item.source)
                record = _FileRecord(
                    ordinal=ordinal,
                    action_id=action.action_id,
                    asset_id=action.asset_id,
                    operation=action.operation,
                    role=item.role,
                    sidecar_slot=item.sidecar_slot,
                    source=item.source,
                    reference=item.reference,
                    destination=None,
                    comparison_path=comparison_path,
                    source_root=str(source_root),
                    quarantine_path=str(quarantine_path),
                    temporary_path=None,
                    strategy=_Strategy.QUARANTINE,
                )
                record._validate_shape()
                self._validate_record_user_paths(record)
                records.append(record)
                continue

            assert item.destination is not None
            destination = _absolute(item.destination)
            if _is_within(destination, state_root) or _is_within(state_root, destination):
                raise DatasetExecutionError(
                    ExecutionCode.UNSAFE_PATH,
                    "dataset destination overlaps the private executor state namespace",
                    destination,
                )
            if not _is_within(destination, destination_root) or destination == destination_root:
                raise DatasetExecutionError(
                    ExecutionCode.UNSAFE_PATH,
                    "move destination is outside the destination root",
                    destination,
                )
            if self.fs.lexists(destination):
                raise DatasetExecutionError(
                    ExecutionCode.DESTINATION_CONFLICT,
                    "move destination already exists",
                    destination,
                )
            ancestor = _existing_ancestor(destination.parent)
            if not _is_within(ancestor, destination_root):
                raise DatasetExecutionError(
                    ExecutionCode.UNSAFE_PATH,
                    "destination parent escaped its root",
                    destination.parent,
                )
            ancestor_stat = _validate_plain_directory(ancestor)
            same_volume = bool(self.volume_checker(item.source, ancestor, ancestor_stat))
            if same_volume:
                strategy = _Strategy.SAME_VOLUME_RENAME
                quarantine_path_value = None
                temporary_path_value = None
            else:
                strategy = _Strategy.CROSS_VOLUME_COPY
                quarantine_path = (
                    source_root.joinpath(QUARANTINE_DIRECTORY_NAME)
                    .joinpath(plan.plan_id)
                    .joinpath(action.action_id)
                    .joinpath("{:06d}.payload".format(ordinal))
                )
                self._preflight_quarantine_path(quarantine_path, source_root, item.source)
                temporary_path = destination.with_name(
                    ".{}.dupeguru-{}-{:06d}.tmp".format(destination.name, plan.plan_id[:12], ordinal)
                )
                if _is_within(temporary_path, state_root) or _is_within(state_root, temporary_path):
                    raise DatasetExecutionError(
                        ExecutionCode.UNSAFE_PATH,
                        "dataset staging path overlaps the private executor state namespace",
                        temporary_path,
                    )
                if self.fs.lexists(temporary_path):
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "copy staging path already exists",
                        temporary_path,
                    )
                quarantine_path_value = str(quarantine_path)
                temporary_path_value = str(temporary_path)
                volume_key = str(ancestor_stat.st_dev)
                current_ancestor, current_required = copy_requirements.get(volume_key, (ancestor, 0))
                copy_requirements[volume_key] = (current_ancestor, current_required + item.source.size)
            record = _FileRecord(
                ordinal=ordinal,
                action_id=action.action_id,
                asset_id=action.asset_id,
                operation=action.operation,
                role=item.role,
                sidecar_slot=item.sidecar_slot,
                source=item.source,
                reference=None,
                destination=str(destination),
                comparison_path=str(destination),
                source_root=str(source_root),
                quarantine_path=quarantine_path_value,
                temporary_path=temporary_path_value,
                strategy=strategy,
            )
            record._validate_shape()
            self._validate_record_user_paths(record)
            records.append(record)

        for ancestor, required in copy_requirements.values():
            try:
                available = shutil.disk_usage(ancestor).free
            except OSError as error:
                raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), ancestor) from error
            if available < required:
                raise DatasetExecutionError(
                    ExecutionCode.INSUFFICIENT_SPACE,
                    "cross-volume staging requires {} bytes but only {} are available".format(required, available),
                    ancestor,
                )
        return tuple(records)

    def _preflight_quarantine_path(
        self,
        quarantine_path: Path,
        source_root: Path,
        proof: DatasetFileProof,
    ) -> None:
        if not _is_within(quarantine_path, source_root):
            raise DatasetExecutionError(
                ExecutionCode.UNSAFE_PATH,
                "quarantine path escaped its source root",
                quarantine_path,
            )
        if self.fs.lexists(quarantine_path):
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "quarantine payload path already exists",
                quarantine_path,
            )
        ancestor = _existing_ancestor(quarantine_path.parent)
        ancestor_stat = _validate_plain_directory(ancestor)
        private_root = source_root.joinpath(QUARANTINE_DIRECTORY_NAME)
        if self.fs.lexists(private_root):
            _validate_private_directory_tree(
                private_root,
                ancestor if _is_within(ancestor, private_root) else private_root,
                "dataset quarantine directory",
            )
        if proof.stat_device != int(ancestor_stat.st_dev):
            raise DatasetExecutionError(
                ExecutionCode.VOLUME_MISMATCH,
                "quarantine is not on the source volume",
                quarantine_path,
            )

    @staticmethod
    def _containing_root(path: Path, roots: Sequence[Path]) -> Path:
        matches = [root for root in roots if _is_within(path, root)]
        if not matches:
            raise DatasetExecutionError(ExecutionCode.UNSAFE_PATH, "source escaped every allowed root", path)
        return max(matches, key=lambda item: len(str(item)))

    def _build_document(
        self,
        plan: DatasetPlan,
        state_root: Path,
        records: Tuple[_FileRecord, ...],
    ) -> _ExecutionDocument:
        return _ExecutionDocument(
            plan_id=plan.plan_id,
            plan_hash=_content_hash(plan.to_dict()),
            created_ns=time.time_ns(),
            state_root=str(state_root),
            destination_root=str(_absolute(plan.destination_root)),
            plan=plan.to_dict(),
            files=records,
        )

    def _execution_document_payload(
        self,
        document: _ExecutionDocument,
    ) -> bytes:
        payload = (_canonical_json(document.to_dict()) + "\n").encode(
            "utf-8",
        )
        path = self._document_path(
            Path(document.state_root),
            document.plan_id,
        )
        if len(document.files) > MAX_EXECUTION_TRANSACTION_FILES:
            raise DatasetExecutionError(
                ExecutionCode.PLAN_INVALID,
                ("execution document contains {} file records; maximum is " "{}").format(
                    len(document.files),
                    MAX_EXECUTION_TRANSACTION_FILES,
                ),
                path,
            )
        if len(payload) > MAX_EXECUTION_DOCUMENT_BYTES:
            raise DatasetExecutionError(
                ExecutionCode.PLAN_INVALID,
                ("execution document is {} bytes; maximum is {}").format(
                    len(payload),
                    MAX_EXECUTION_DOCUMENT_BYTES,
                ),
                path,
            )
        return payload

    def _validate_document_lifecycle_budgets(
        self,
        document: _ExecutionDocument,
    ) -> Mapping[str, _JournalBudget]:
        """Purely prove document and every lifecycle branch resource bound."""

        self._execution_document_payload(document)
        budgets = self._journal_phase_budgets(document)
        for phase, budget in budgets.items():
            if budget.events > MAX_JOURNAL_EVENTS:
                raise DatasetExecutionError(
                    ExecutionCode.PLAN_INVALID,
                    ("projected '{}' journal branch requires {} events; " "maximum is {}").format(
                        phase,
                        budget.events,
                        MAX_JOURNAL_EVENTS,
                    ),
                )
            if budget.bytes > MAX_JOURNAL_BYTES:
                raise DatasetExecutionError(
                    ExecutionCode.PLAN_INVALID,
                    ("projected '{}' journal branch requires {} bytes; " "maximum is {}").format(
                        phase,
                        budget.bytes,
                        MAX_JOURNAL_BYTES,
                    ),
                )
        return budgets

    def _persist_document(self, document: _ExecutionDocument, state_root: Path) -> _ExecutionDocument:
        path = self._document_path(state_root, document.plan_id)
        if self.fs.lexists(path):
            existing = self._read_document(path)
            if (
                existing.plan_hash != document.plan_hash
                or existing.plan != document.plan
                or existing.files != document.files
                or _path_key(existing.state_root) != _path_key(document.state_root)
            ):
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "an incompatible immutable operation document already exists",
                    path,
                )
            return existing
        temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
        payload = self._execution_document_payload(document)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        created_identity: Optional[Tuple[int, int]] = None
        try:
            fd = os.open(str(temporary), flags, 0o600)
            try:
                created = os.fstat(fd)
                created_identity = (int(created.st_dev), int(created.st_ino))
                written = os.write(fd, payload)
                if written != len(payload):
                    raise OSError("short write while persisting execution document")
                os.fsync(fd)
            finally:
                os.close(fd)
            with self.fs.open_readonly(temporary) as temporary_handle:
                opened = os.fstat(temporary_handle.fileno())
                if (
                    not _is_plain_file(opened)
                    or int(getattr(opened, "st_nlink", 0)) != 1
                    or (int(opened.st_dev), int(opened.st_ino)) != created_identity
                    or temporary_handle.read(len(payload) + 1) != payload
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.SOURCE_CHANGED,
                        "execution document temporary changed before publication",
                        temporary,
                    )
                _require_open_version(
                    opened,
                    os.fstat(temporary_handle.fileno()),
                    temporary,
                )
                _require_path_matches_stat(temporary, opened)
                try:
                    rename_commit = self.fs.rename_no_replace_verified(
                        temporary,
                        path,
                        temporary_handle,
                    )
                except UnverifiedRenameCommitError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.IO_ERROR,
                        str(error),
                        error.destination,
                    ) from error
                published_opened = _rebind_open_version_after_atomic_rename(
                    temporary_handle,
                    opened,
                    path,
                    rename_commit,
                )
                temporary_handle.seek(0)
                if temporary_handle.read(len(payload) + 1) != payload:
                    raise DatasetExecutionError(
                        ExecutionCode.SOURCE_CHANGED,
                        "execution document changed during publication",
                        path,
                    )
                _require_open_version(
                    published_opened,
                    os.fstat(temporary_handle.fileno()),
                    path,
                )
                _require_path_matches_stat(path, published_opened)
            self.fs.fsync_directory(path.parent)
        except FileExistsError:
            if self.fs.lexists(path):
                existing = self._read_document(path)
                if existing.plan_hash == document.plan_hash and existing.plan == document.plan:
                    return existing
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "execution document destination appeared during publication",
                path,
            )
        except OSError as error:
            raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), path) from error
        finally:
            if created_identity is not None:
                cleanup_created_regular_file(
                    temporary,
                    created_identity,
                    self.fs,
                )
        return document

    def _execute_fresh(
        self,
        plan: DatasetPlan,
        state_root: Path,
        records: Tuple[_FileRecord, ...],
        *,
        recovery_retry: bool = False,
        existing_document: Optional[_ExecutionDocument] = None,
    ) -> DatasetExecutionReport:
        created_directories: List[Path] = []
        document: Optional[_ExecutionDocument] = existing_document or self._build_document(
            plan,
            state_root,
            records,
        )
        changed_records: List[_FileRecord] = []
        transaction_prepared = False
        try:
            _validate_state_namespace(state_root)
            self._validate_plan_user_paths(plan)
            for record in records:
                self._validate_record_user_paths(record)
            budgets = self._validate_document_lifecycle_budgets(document)
            reservation = budgets["retry" if recovery_retry else "initial"]
            journal = self._journal_for(state_root, document.plan_id)
            journal.ensure_capacity(
                document,
                additional_events=reservation.events,
                additional_bytes=reservation.bytes,
            )
            # Directory creation and immutable metadata publication happen only after every source,
            # reference, destination, volume, and free-space check above has succeeded.
            _ensure_directory(state_root, created_directories, self.fs)
            _ensure_directory(state_root.joinpath("operations"), created_directories, self.fs)
            _ensure_directory(
                self._document_path(state_root, plan.plan_id).parent,
                created_directories,
                self.fs,
            )
            _validate_private_directory(state_root, "dataset executor state directory")
            _validate_private_directory(
                state_root.joinpath("operations"),
                "dataset executor operations directory",
            )
            _validate_private_directory(
                self._document_path(state_root, plan.plan_id).parent,
                "dataset executor operation directory",
            )
            with _process_lock(
                self._lock_path(state_root, document.plan_id),
                self.fs,
            ):
                for record in records:
                    if record.destination is not None:
                        _ensure_directory(Path(record.destination).parent, created_directories, self.fs)
                    if record.quarantine_path is not None:
                        _ensure_directory(Path(record.quarantine_path).parent, created_directories, self.fs)
                        quarantine_root = Path(record.source_root).joinpath(QUARANTINE_DIRECTORY_NAME)
                        _validate_private_directory_tree(
                            quarantine_root,
                            Path(record.quarantine_path).parent,
                            "dataset quarantine directory",
                        )
                document = self._persist_document(document, state_root)
                persisted = self._read_document(self._document_path(state_root, document.plan_id))
                if persisted.to_dict() != document.to_dict():
                    raise DatasetExecutionError(
                        ExecutionCode.DOCUMENT_CONFLICT,
                        "immutable execution document changed before apply",
                        self._document_path(state_root, document.plan_id),
                    )
                document = persisted
                journal = self._journal_for(state_root, document.plan_id)
                existing_events = journal.events_for(document)
                if existing_events:
                    terminal = existing_events[-1].event
                    if not recovery_retry or terminal is not _JournalEvent.ROLLED_BACK:
                        raise DatasetExecutionError(
                            ExecutionCode.BUSY,
                            "dataset operation acquired journal history while waiting for its lock",
                            journal.path,
                        )
                journal.ensure_capacity(
                    document,
                    additional_events=reservation.events,
                    additional_bytes=reservation.bytes,
                )
                journal.append(document, _JournalEvent.PREPARED, {"file_count": len(records)})
                transaction_prepared = True

                # Phase one publishes every move destination.  Cross-volume sources remain at their
                # original paths until every destination has been verified.
                for record in records:
                    if record.strategy is _Strategy.SAME_VOLUME_RENAME:
                        self._stage_same_volume_move(record)
                        changed_records.append(record)
                        journal.append(
                            document,
                            _JournalEvent.DESTINATION_PUBLISHED,
                            {"ordinal": record.ordinal, "strategy": record.strategy.value},
                        )
                    elif record.strategy is _Strategy.CROSS_VOLUME_COPY:
                        self._stage_cross_volume_destination(
                            record,
                            after_temporary_created=lambda file_stat, current=record: journal.append(
                                document,
                                _JournalEvent.TEMPORARY_CREATED,
                                {
                                    "ordinal": current.ordinal,
                                    "stat_device": int(file_stat.st_dev),
                                    "stat_inode": int(file_stat.st_ino),
                                },
                            ),
                            before_publish=lambda file_stat, current=record: journal.append(
                                document,
                                _JournalEvent.DESTINATION_PREPARED,
                                {
                                    "ordinal": current.ordinal,
                                    "stat_device": int(file_stat.st_dev),
                                    "stat_inode": int(file_stat.st_ino),
                                },
                            ),
                        )
                        changed_records.append(record)
                        journal.append(
                            document,
                            _JournalEvent.DESTINATION_PUBLISHED,
                            {"ordinal": record.ordinal, "strategy": record.strategy.value},
                        )

                # Phase two quarantines exact duplicates and the now-proven cross-volume sources.
                # A failure anywhere rolls the complete plan back in reverse mutation order.
                source_strategies = {_path_key(candidate.source.path): candidate.strategy for candidate in records}
                for record in records:
                    if record.strategy not in {_Strategy.QUARANTINE, _Strategy.CROSS_VOLUME_COPY}:
                        continue
                    self._stage_source_quarantine(record, source_strategies)
                    changed_records.append(record)
                    journal.append(
                        document,
                        _JournalEvent.SOURCE_QUARANTINED,
                        {"ordinal": record.ordinal, "quarantine_path": record.quarantine_path},
                    )
                journal.append(document, _JournalEvent.APPLIED)
            return DatasetExecutionReport(
                plan_id=plan.plan_id,
                state=ExecutionState.APPLIED,
                code=ExecutionCode.NONE,
                message="all dataset bundles were applied as one recoverable transaction",
                changed=True,
                files=tuple(self._file_result(record, FileExecutionState.APPLIED, True) for record in records),
            )
        except BaseException as original_error:
            if document is None or not transaction_prepared:
                _cleanup_empty_directories(created_directories, self.fs)
                if isinstance(original_error, DatasetExecutionError):
                    return self._failure_report(
                        plan.plan_id,
                        original_error.code,
                        str(original_error),
                        path=original_error.path,
                    )
                return self._failure_report(plan.plan_id, ExecutionCode.EXECUTION_FAILED, repr(original_error))
            journal = self._journal_for(state_root, document.plan_id)
            try:
                with _process_lock(
                    self._lock_path(state_root, document.plan_id),
                    self.fs,
                ):
                    rollback_reservation = self._journal_phase_budgets(document)["rollback"]
                    journal.ensure_capacity(
                        document,
                        additional_events=rollback_reservation.events,
                        additional_bytes=rollback_reservation.bytes,
                    )
                    failure_details = {
                        "message": repr(original_error),
                        "changed_records": len(changed_records),
                    }
                    try:
                        failure_bytes = _AppendOnlyJournal.projected_record_size(
                            document,
                            _JournalEvent.FAILED,
                            failure_details,
                        )
                        journal.ensure_capacity(
                            document,
                            additional_events=rollback_reservation.events + 1,
                            additional_bytes=(rollback_reservation.bytes + failure_bytes),
                        )
                        journal.append(
                            document,
                            _JournalEvent.FAILED,
                            failure_details,
                        )
                    except DatasetExecutionError:
                        pass
                    self._append_event_once(
                        document,
                        journal,
                        _JournalEvent.ROLLBACK_PREPARED,
                        match_event_only=True,
                    )
                    self._rollback_document(
                        document,
                        journal,
                        restore=False,
                        known_published={
                            record.ordinal
                            for record in changed_records
                            if record.operation is DatasetOperation.MOVE_BUNDLE
                        },
                    )
                _cleanup_empty_directories(created_directories, self.fs)
                return DatasetExecutionReport(
                    plan_id=plan.plan_id,
                    state=ExecutionState.ROLLED_BACK,
                    code=ExecutionCode.EXECUTION_FAILED,
                    message="execution failed and every staged file was rolled back: {}".format(original_error),
                    changed=bool(changed_records),
                    files=tuple(self._file_result(record, FileExecutionState.UNCHANGED, False) for record in records),
                )
            except BaseException as rollback_error:
                return DatasetExecutionReport(
                    plan_id=plan.plan_id,
                    state=ExecutionState.RECOVERY_REQUIRED,
                    code=ExecutionCode.ROLLBACK_FAILED,
                    message="execution failed ({!r}); rollback also failed ({!r})".format(
                        original_error,
                        rollback_error,
                    ),
                    changed=True,
                    files=tuple(self._file_result(record, FileExecutionState.FAILED, True) for record in records),
                )

    def _stage_same_volume_move(self, record: _FileRecord) -> None:
        assert record.destination is not None
        self._validate_record_user_paths(record)
        source = Path(record.source.path)
        destination = Path(record.destination)
        self._call_fault("before_same_volume_move", record)
        try:
            self._validate_runtime_proof(record.source, (Path(record.source_root),))
        except DatasetSafetyError as error:
            raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
        _validate_plain_directory(destination.parent)
        if self.fs.lexists(destination):
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "destination appeared before same-volume publication",
                destination,
            )
        with self.fs.open_readonly(source) as source_handle:
            moved = False
            try:
                opened = os.fstat(source_handle.fileno())
                self._require_open_source(source_handle, opened, record.source, source)
                self._call_fault("before_same_volume_publish", record)
                _validate_plain_directory(destination.parent)
                current_source = os.stat(source, follow_symlinks=False)
                if (
                    not _is_plain_file(current_source)
                    or current_source.st_dev != opened.st_dev
                    or current_source.st_ino != opened.st_ino
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.SOURCE_CHANGED,
                        "source path changed immediately before same-volume publication",
                        source,
                    )
                self._validate_record_user_paths(record)
                try:
                    try:
                        rename_commit = self.fs.rename_no_replace_verified(
                            source,
                            destination,
                            source_handle,
                        )
                    except UnverifiedRenameCommitError as error:
                        moved = True
                        raise DatasetExecutionError(
                            ExecutionCode.SOURCE_CHANGED,
                            str(error),
                            error.destination,
                        ) from error
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "destination appeared during same-volume publication",
                        destination,
                    ) from error
                moved = True
                self.fs.fsync_directory(source.parent)
                self.fs.fsync_directory(destination.parent)
                opened = _rebind_open_version_after_atomic_rename(
                    source_handle,
                    opened,
                    destination,
                    rename_commit,
                )
                self._call_fault("after_same_volume_publish", record)
                _stable_digest(
                    destination,
                    self.fs,
                    expected=record.source,
                    require_original_identity=True,
                )
            except BaseException:
                if moved and self.fs.lexists(destination) and not self.fs.lexists(source):
                    try:
                        self.fs.rename_no_replace_verified(
                            destination,
                            source,
                            source_handle,
                        )
                        self.fs.fsync_directory(destination.parent)
                        self.fs.fsync_directory(source.parent)
                    except BaseException:
                        pass
                raise

    def _stage_cross_volume_destination(
        self,
        record: _FileRecord,
        *,
        after_temporary_created: Optional[Callable[[os.stat_result], None]] = None,
        before_publish: Optional[Callable[[os.stat_result], None]] = None,
    ) -> None:
        assert record.destination is not None
        assert record.temporary_path is not None
        self._validate_record_user_paths(record)
        source = Path(record.source.path)
        destination = Path(record.destination)
        temporary = Path(record.temporary_path)
        try:
            self._validate_runtime_proof(record.source, (Path(record.source_root),))
        except DatasetSafetyError as error:
            raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
        _validate_plain_directory(destination.parent)
        if self.fs.lexists(destination) or self.fs.lexists(temporary):
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "cross-volume destination or temporary reservation already exists",
                destination,
            )
        self._call_fault("before_cross_volume_copy", record)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        temporary_created_identity: Optional[Tuple[int, int]] = None
        temporary_bound_durably = False
        try:
            with self.fs.open_readonly(source) as source_handle:
                source_before = os.fstat(source_handle.fileno())
                self._require_open_source(source_handle, source_before, record.source, source)
                try:
                    temporary_fd = os.open(str(temporary), flags, 0o600)
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "copy temporary reservation appeared",
                        temporary,
                    ) from error
                temporary_created_stat = os.fstat(temporary_fd)
                temporary_created_identity = (
                    int(temporary_created_stat.st_dev),
                    int(temporary_created_stat.st_ino),
                )
                if not _is_plain_file(temporary_created_stat):
                    os.close(temporary_fd)
                    raise DatasetExecutionError(
                        ExecutionCode.UNSAFE_PATH,
                        "copy temporary reservation is not a plain file",
                        temporary,
                    )
                if after_temporary_created is not None:
                    try:
                        after_temporary_created(temporary_created_stat)
                        temporary_bound_durably = True
                    except BaseException:
                        os.close(temporary_fd)
                        raise
                digest = hashlib.sha256()
                copied = 0
                try:
                    while True:
                        chunk = source_handle.read(self.copy_chunk_size)
                        if not chunk:
                            break
                        self._call_fault("copy_chunk", record)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(temporary_fd, view)
                            if written <= 0:
                                raise OSError(errno.ENOSPC, "short write while copying dataset payload")
                            view = view[written:]
                        digest.update(chunk)
                        copied += len(chunk)
                    os.fsync(temporary_fd)
                finally:
                    os.close(temporary_fd)
                source_after = os.fstat(source_handle.fileno())
                self._require_same_open_version(source_before, source_after, source)
                if copied != record.source.size or digest.hexdigest() != record.source.digest_hex:
                    raise DatasetExecutionError(
                        ExecutionCode.CONTENT_MISMATCH,
                        "cross-volume copy does not match the immutable source proof",
                        temporary,
                    )
            try:
                if os.name == "nt":
                    # Windows rejects follow_symlinks=False for os.utime.  The path was created
                    # O_EXCL by this transaction and is revalidated as a plain file below.
                    os.utime(temporary, ns=(record.source.mtime_ns, record.source.mtime_ns))
                else:
                    os.utime(
                        temporary,
                        ns=(record.source.mtime_ns, record.source.mtime_ns),
                        follow_symlinks=False,
                    )
                metadata_fd = os.open(
                    str(temporary),
                    os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(metadata_fd)
                finally:
                    os.close(metadata_fd)
            except OSError as error:
                raise DatasetExecutionError(ExecutionCode.IO_ERROR, str(error), temporary) from error
            _stable_byte_equal(
                source,
                temporary,
                self.fs,
                record.source,
                first_original_identity=True,
                second_original_identity=False,
            )
            temporary_stat = os.stat(temporary, follow_symlinks=False)
            if not _is_plain_file(temporary_stat):
                raise DatasetExecutionError(
                    ExecutionCode.UNSAFE_PATH,
                    "copy temporary path changed type before publication",
                    temporary,
                )
            if before_publish is not None:
                before_publish(temporary_stat)
            self._call_fault("before_cross_volume_publish", record)
            _validate_plain_directory(destination.parent)
            assert temporary_created_identity is not None
            with _open_verified_payload(
                temporary,
                self.fs,
                expected=record.source,
                require_original_identity=False,
                created_identity=temporary_created_identity,
            ) as (temporary_handle, temporary_opened):
                self._validate_record_user_paths(record)
                try:
                    try:
                        rename_commit = self.fs.rename_no_replace_verified(
                            temporary,
                            destination,
                            temporary_handle,
                        )
                    except UnverifiedRenameCommitError as error:
                        raise DatasetExecutionError(
                            ExecutionCode.SOURCE_CHANGED,
                            str(error),
                            error.destination,
                        ) from error
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "destination appeared during cross-volume publication",
                        destination,
                    ) from error
                self.fs.fsync_directory(destination.parent)
                _rebind_open_version_after_atomic_rename(
                    temporary_handle,
                    temporary_opened,
                    destination,
                    rename_commit,
                )
                _stable_byte_equal(
                    source,
                    destination,
                    self.fs,
                    record.source,
                    first_original_identity=True,
                    second_original_identity=False,
                )
                try:
                    self._validate_runtime_proof(record.source, (Path(record.source_root),))
                except DatasetSafetyError as error:
                    raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
                self._call_fault("after_cross_volume_publish", record)
        except BaseException:
            if temporary_created_identity is not None and not temporary_bound_durably:
                cleanup_created_regular_file(
                    temporary,
                    temporary_created_identity,
                    self.fs,
                )
            raise

    def _stage_source_quarantine(
        self,
        record: _FileRecord,
        source_strategies: Mapping[str, _Strategy],
    ) -> None:
        assert record.quarantine_path is not None
        assert record.comparison_path is not None
        self._validate_record_user_paths(record)
        source = Path(record.source.path)
        comparison = Path(record.comparison_path)
        quarantine = Path(record.quarantine_path)
        self._validate_record_quarantine_directory(record)
        _validate_plain_directory(quarantine.parent)
        if self.fs.lexists(quarantine):
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "quarantine payload appeared before staging",
                quarantine,
            )
        try:
            self._validate_runtime_proof(record.source, (Path(record.source_root),))
        except DatasetSafetyError as error:
            raise DatasetExecutionError(ExecutionCode.SOURCE_CHANGED, str(error), error.path) from error
        comparison_original = self._comparison_preserves_identity(
            record,
            source_strategies,
        )
        _stable_byte_equal(
            source,
            comparison,
            self.fs,
            record.source,
            first_original_identity=True,
            second_original_identity=comparison_original,
            second_expected=record.reference,
        )
        self._call_fault("before_source_quarantine", record)
        _validate_plain_directory(quarantine.parent)
        with self.fs.open_readonly(source) as source_handle, self.fs.open_readonly(comparison) as comparison_handle:
            moved = False
            try:
                source_before = os.fstat(source_handle.fileno())
                comparison_before = os.fstat(comparison_handle.fileno())
                self._require_open_source(source_handle, source_before, record.source, source)
                if comparison_before.st_size != record.source.size:
                    raise DatasetExecutionError(
                        ExecutionCode.CONTENT_MISMATCH,
                        "comparison payload size changed",
                        comparison,
                    )
                source_handle.seek(0)
                comparison_handle.seek(0)
                source_digest = hashlib.sha256()
                comparison_digest = hashlib.sha256()
                while True:
                    source_chunk = source_handle.read(self.copy_chunk_size)
                    comparison_chunk = comparison_handle.read(self.copy_chunk_size)
                    if source_chunk != comparison_chunk:
                        raise DatasetExecutionError(
                            ExecutionCode.CONTENT_MISMATCH,
                            "source and keeper differ immediately before quarantine",
                            source,
                        )
                    if not source_chunk:
                        break
                    source_digest.update(source_chunk)
                    comparison_digest.update(comparison_chunk)
                self._require_same_open_version(source_before, os.fstat(source_handle.fileno()), source)
                self._require_same_open_version(
                    comparison_before,
                    os.fstat(comparison_handle.fileno()),
                    comparison,
                )
                if (
                    source_digest.hexdigest() != record.source.digest_hex
                    or comparison_digest.hexdigest() != record.source.digest_hex
                ):
                    raise DatasetExecutionError(
                        ExecutionCode.CONTENT_MISMATCH,
                        "source or keeper digest changed immediately before quarantine",
                        source,
                    )
                self._validate_record_user_paths(record)
                try:
                    try:
                        rename_commit = self.fs.rename_no_replace_verified(
                            source,
                            quarantine,
                            source_handle,
                        )
                    except UnverifiedRenameCommitError as error:
                        moved = True
                        raise DatasetExecutionError(
                            ExecutionCode.SOURCE_CHANGED,
                            str(error),
                            error.destination,
                        ) from error
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "quarantine payload appeared during staging",
                        quarantine,
                    ) from error
                moved = True
                self.fs.fsync_directory(source.parent)
                self.fs.fsync_directory(quarantine.parent)
                source_before = _rebind_open_version_after_atomic_rename(
                    source_handle,
                    source_before,
                    quarantine,
                    rename_commit,
                )
                _require_path_matches_stat(comparison, comparison_before)
                _reverify_open_equal_pair(
                    source_handle,
                    comparison_handle,
                    source_before,
                    comparison_before,
                    first_expected=record.source,
                    second_expected=record.reference or record.source,
                    first_path=quarantine,
                    second_path=comparison,
                )
                self._call_fault("after_source_quarantine", record)
            except BaseException:
                if moved and self.fs.lexists(quarantine) and not self.fs.lexists(source):
                    try:
                        self.fs.rename_no_replace_verified(
                            quarantine,
                            source,
                            source_handle,
                        )
                        self.fs.fsync_directory(quarantine.parent)
                        self.fs.fsync_directory(source.parent)
                    except BaseException:
                        pass
                raise

    @staticmethod
    def _comparison_preserves_identity(
        record: _FileRecord,
        source_strategies: Mapping[str, _Strategy],
    ) -> bool:
        if record.strategy is _Strategy.CROSS_VOLUME_COPY:
            return False
        assert record.reference is not None
        reference_key = _path_key(record.reference.path)
        keeper_strategy = source_strategies.get(reference_key)
        if keeper_strategy is None:
            return True
        return keeper_strategy is _Strategy.SAME_VOLUME_RENAME

    @staticmethod
    def _validate_record_quarantine_directory(record: _FileRecord) -> None:
        if record.quarantine_path is None:
            return
        quarantine_root = Path(record.source_root).joinpath(QUARANTINE_DIRECTORY_NAME)
        _validate_private_directory_tree(
            quarantine_root,
            Path(record.quarantine_path).parent,
            "dataset quarantine directory",
        )

    def _require_open_source(
        self,
        handle: BinaryIO,
        file_stat: os.stat_result,
        proof: DatasetFileProof,
        path: Path,
    ) -> None:
        accepted_generation = self._accepted_generation_tokens.get(_path_key(proof.path))
        try:
            proof_token = FileGenerationToken.from_encoded(bytes.fromhex(proof.generation_token))
        except (TypeError, ValueError) as error:
            raise DatasetExecutionError(
                ExecutionCode.PLAN_INVALID,
                "source proof has an invalid generation token",
                path,
            ) from error
        if os.name != "nt" and (proof_token.namespace != "posix-ctime-ns" or proof_token.value != proof.ctime_ns):
            raise DatasetExecutionError(
                ExecutionCode.PLAN_INVALID,
                "source proof ctime disagrees with its generation token",
                path,
            )
        expected_generation = accepted_generation if accepted_generation is not None else proof.generation_token
        if accepted_generation is None:
            expected_token = proof_token
        else:
            try:
                expected_token = FileGenerationToken.from_encoded(bytes.fromhex(accepted_generation))
            except (TypeError, ValueError) as error:
                raise DatasetExecutionError(
                    ExecutionCode.PLAN_INVALID,
                    "recovered source has an invalid accepted generation token",
                    path,
                ) from error
        if (
            not _is_plain_file(file_stat)
            or file_stat.st_dev != proof.stat_device
            or file_stat.st_ino != proof.stat_inode
            or file_stat.st_size != proof.size
            or file_stat.st_mtime_ns != proof.mtime_ns
            or (
                os.name != "nt"
                and (expected_token.namespace != "posix-ctime-ns" or file_stat.st_ctime_ns != expected_token.value)
            )
        ):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "open source handle no longer matches the immutable proof",
                path,
            )
        snapshot = core_fs.FileSnapshot.from_file(
            handle,
            path=path,
            stat_result=file_stat,
        )
        if snapshot.ctime_ns.hex() != expected_generation:
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "open source generation no longer matches the immutable proof",
                path,
            )

    @staticmethod
    def _require_same_open_version(
        first: os.stat_result,
        second: os.stat_result,
        path: Path,
    ) -> None:
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(first, field, None) != getattr(second, field, None) for field in fields):
            raise DatasetExecutionError(
                ExecutionCode.SOURCE_CHANGED,
                "open payload changed during the operation",
                path,
            )

    def _call_fault(self, phase: str, record: _FileRecord) -> None:
        if self.fault_hook is not None:
            self.fault_hook(phase, record)

    def _handle_existing_document(
        self,
        document: _ExecutionDocument,
        plan: DatasetPlan,
        *,
        validation_valid: bool,
        validation_issues: Tuple[DatasetIssue, ...],
        execute: bool,
    ) -> Optional[DatasetExecutionReport]:
        state_root = Path(document.state_root)
        journal = self._journal_for(state_root, document.plan_id)
        state, message = self._document_state(document, journal)
        if state in {ExecutionState.APPLIED, ExecutionState.ALREADY_APPLIED}:
            events = journal.events_for(document)
            terminal = events[-1].event if events else None
            if terminal not in {_JournalEvent.APPLIED, _JournalEvent.APPLIED_RECOVERED}:
                if not execute:
                    return DatasetExecutionReport(
                        plan_id=plan.plan_id,
                        state=ExecutionState.RECOVERY_REQUIRED,
                        code=ExecutionCode.INVALID_STATE,
                        message="all files are staged, but the commit journal record is missing",
                        changed=True,
                        files=tuple(
                            self._file_result(record, FileExecutionState.APPLIED, True) for record in document.files
                        ),
                    )
                _validate_private_directory(state_root, "dataset executor state directory")
                with _process_lock(
                    self._lock_path(state_root, document.plan_id),
                    self.fs,
                ):
                    event_bytes = _AppendOnlyJournal.projected_record_size(
                        document,
                        _JournalEvent.APPLIED_RECOVERED,
                        {},
                    )
                    journal.ensure_capacity(
                        document,
                        additional_events=1,
                        additional_bytes=event_bytes,
                    )
                    self._append_event_once(
                        document,
                        journal,
                        _JournalEvent.APPLIED_RECOVERED,
                        match_event_only=True,
                    )
            return DatasetExecutionReport(
                plan_id=plan.plan_id,
                state=ExecutionState.ALREADY_APPLIED,
                code=ExecutionCode.NONE,
                message="dataset plan is already completely applied",
                changed=False,
                files=tuple(self._file_result(record, FileExecutionState.APPLIED, False) for record in document.files),
            )
        if state is ExecutionState.FINALIZED:
            return DatasetExecutionReport(
                plan_id=plan.plan_id,
                state=ExecutionState.FINALIZED,
                code=ExecutionCode.NONE,
                message="dataset plan is already finalized",
                changed=False,
                files=tuple(
                    self._file_result(record, FileExecutionState.FINALIZED, False) for record in document.files
                ),
            )
        if state in {ExecutionState.ROLLED_BACK, ExecutionState.RESTORED}:
            return DatasetExecutionReport(
                plan_id=plan.plan_id,
                state=state,
                code=ExecutionCode.INVALID_STATE,
                message=(
                    "a terminally restored/rolled-back operation cannot be reused; " "create a fresh dataset plan"
                ),
                changed=False,
                files=tuple(
                    self._file_result(
                        record,
                        FileExecutionState.RESTORED,
                        False,
                    )
                    for record in document.files
                ),
            )
        if not execute:
            return DatasetExecutionReport(
                plan_id=plan.plan_id,
                state=ExecutionState.RECOVERY_REQUIRED,
                code=ExecutionCode.INVALID_STATE,
                message=message,
                changed=True,
                files=tuple(self._file_result(record, FileExecutionState.FAILED, True) for record in document.files),
                issues=validation_issues,
            )
        _validate_private_directory(state_root, "dataset executor state directory")
        rollback_reservation = self._journal_phase_budgets(document)["rollback"]
        journal.ensure_capacity(
            document,
            additional_events=rollback_reservation.events,
            additional_bytes=rollback_reservation.bytes,
        )
        with _process_lock(
            self._lock_path(state_root, document.plan_id),
            self.fs,
        ):
            persisted = self._load_required_document(
                state_root,
                document.plan_id,
            )
            if persisted.to_dict() != document.to_dict():
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "dataset execution document changed before crash recovery",
                    self._document_path(state_root, document.plan_id),
                )
            journal.ensure_capacity(
                document,
                additional_events=rollback_reservation.events,
                additional_bytes=rollback_reservation.bytes,
            )
            self._append_event_once(
                document,
                journal,
                _JournalEvent.ROLLBACK_PREPARED,
                {"reason": "crash_replay"},
                match_event_only=True,
            )
            self._rollback_document(document, journal, restore=False)
        if not validation_valid:
            # The failed validation was taken before recovery, when missing source paths are
            # expected.  The caller immediately performs a fresh full revalidation.
            return None
        return None

    def _document_state(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
    ) -> Tuple[ExecutionState, str]:
        events = journal.events_for(document)
        event_index = _JournalEventIndex.build(document, events)
        terminal = events[-1].event if events else None
        statuses = tuple(
            self._record_presence(
                record,
                document=document,
                journal=journal,
                event_index=event_index,
            )
            for record in document.files
        )
        all_original = all(status in {"original", "original_conflict"} for status in statuses)
        all_applied = all(status == "applied" for status in statuses)
        all_finalized = all(
            status == ("finalized" if record.quarantine_path is not None else "applied")
            for record, status in zip(document.files, statuses)
        )
        if all_original:
            if terminal is _JournalEvent.RESTORED:
                return ExecutionState.RESTORED, "all source bundles are restored"
            return ExecutionState.ROLLED_BACK, "all source bundles are at their original paths"
        if all_applied:
            if terminal in {_JournalEvent.APPLIED, _JournalEvent.APPLIED_RECOVERED}:
                return ExecutionState.APPLIED, "all dataset bundles are completely applied"
            return ExecutionState.ALREADY_APPLIED, "all files are staged; commit record needs replay"
        if all_finalized and terminal is _JournalEvent.FINALIZED:
            return ExecutionState.FINALIZED, "all quarantined payloads are finalized"
        if all_finalized and terminal is _JournalEvent.FINALIZE_PREPARED:
            return ExecutionState.RECOVERY_REQUIRED, "finalize completed on disk but needs journal replay"
        return (
            ExecutionState.RECOVERY_REQUIRED,
            "operation has a mixed filesystem state and requires deterministic rollback",
        )

    def _record_presence(
        self,
        record: _FileRecord,
        *,
        document: Optional[_ExecutionDocument] = None,
        journal: Optional[_AppendOnlyJournal] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> str:
        source_exists = self.fs.lexists(Path(record.source.path))
        destination_exists = record.destination is not None and self.fs.lexists(Path(record.destination))
        quarantine_exists = record.quarantine_path is not None and self.fs.lexists(Path(record.quarantine_path))
        temporary_exists = record.temporary_path is not None and self.fs.lexists(Path(record.temporary_path))
        finalize_history = None
        finalize_tombstone_exists = False
        if document is not None and journal is not None and record.quarantine_path is not None:
            finalize_history = self._finalize_history(
                document,
                journal,
                record,
                event_index=event_index,
            )
            finalize_tombstone_exists = finalize_history is not None and self.fs.lexists(
                finalize_history.tombstone_path
            )
        if finalize_tombstone_exists:
            return "mixed"
        if document is not None and journal is not None:
            cleanup_targets = []
            if record.destination is not None:
                cleanup_targets.append(("destination", Path(record.destination)))
            if record.temporary_path is not None:
                cleanup_targets.append(("temporary", Path(record.temporary_path)))
            for kind, target in cleanup_targets:
                cleanup_history = self._cleanup_history(
                    document,
                    journal,
                    record,
                    kind=kind,
                    original_path=target,
                    event_index=event_index,
                )
                if cleanup_history is None:
                    continue
                if self.fs.lexists(cleanup_history.tombstone_path):
                    return "mixed"
                if cleanup_history.tombstoned and not cleanup_history.purged:
                    return "mixed"
        if temporary_exists:
            return "mixed"
        if record.strategy is _Strategy.SAME_VOLUME_RENAME:
            if source_exists and not destination_exists:
                return "original"
            if source_exists and destination_exists:
                _stable_digest(
                    Path(record.source.path),
                    self.fs,
                    expected=record.source,
                    require_original_identity=True,
                )
                try:
                    self._verify_record_destination(record, original_identity=True)
                except DatasetExecutionError:
                    return "original_conflict"
                return "mixed"
            if not source_exists and destination_exists:
                self._verify_record_destination(record, original_identity=True)
                return "applied"
            return "mixed"
        if record.strategy is _Strategy.CROSS_VOLUME_COPY:
            if source_exists and not destination_exists and not quarantine_exists:
                return "original"
            if source_exists and destination_exists and not quarantine_exists:
                _stable_digest(
                    Path(record.source.path),
                    self.fs,
                    expected=record.source,
                    require_original_identity=True,
                )
                return "original_conflict"
            if not source_exists and destination_exists and quarantine_exists:
                self._verify_record_destination(record, original_identity=False)
                _stable_digest(
                    Path(record.quarantine_path),
                    self.fs,
                    expected=record.source,
                    require_original_identity=True,
                )
                return "applied"
            if not source_exists and destination_exists and not quarantine_exists:
                self._verify_record_destination(record, original_identity=False)
                if finalize_history is not None and (finalize_history.tombstoned or finalize_history.purged):
                    return "finalized"
                return "mixed"
            return "mixed"
        if source_exists and not quarantine_exists:
            return "original"
        if source_exists and quarantine_exists:
            _stable_digest(
                Path(record.source.path),
                self.fs,
                expected=record.source,
                require_original_identity=True,
            )
            try:
                _stable_digest(
                    Path(record.quarantine_path),
                    self.fs,
                    expected=record.source,
                    require_original_identity=True,
                )
            except DatasetExecutionError:
                return "original_conflict"
            return "mixed"
        if not source_exists and quarantine_exists:
            _stable_digest(
                Path(record.quarantine_path),
                self.fs,
                expected=record.source,
                require_original_identity=True,
            )
            return "applied"
        if not source_exists and not quarantine_exists:
            if finalize_history is not None and (finalize_history.tombstoned or finalize_history.purged):
                return "finalized"
            return "mixed"
        return "mixed"

    def _verify_record_destination(self, record: _FileRecord, *, original_identity: bool) -> None:
        assert record.destination is not None
        _stable_digest(
            Path(record.destination),
            self.fs,
            expected=record.source,
            require_original_identity=original_identity,
        )

    def _cleanup_history(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        kind: str,
        original_path: Path,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> Optional[_TombstoneHistory]:
        if kind not in {"destination", "temporary"}:
            raise ValueError("unsupported dataset cleanup kind")
        return self._tombstone_history(
            document,
            journal,
            record,
            original_path=original_path,
            marker="cd" if kind == "destination" else "ct",
            prepared_event=_JournalEvent.CLEANUP_TOMBSTONE_PREPARED,
            tombstoned_event=_JournalEvent.CLEANUP_TOMBSTONED,
            purged_event=_JournalEvent.CLEANUP_PURGED,
            kind=kind,
            event_index=event_index,
        )

    def _purge_transaction_created_entry(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        kind: str,
        original_path: Path,
        created_identity: Optional[Tuple[int, int]],
        keeper_path: Path,
        keeper_proof: DatasetFileProof,
        keeper_original_identity: bool,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> bool:
        """Remove only a journal-bound transaction-created inode via a random tombstone."""

        marker = "cd" if kind == "destination" else "ct"
        history = self._cleanup_history(
            document,
            journal,
            record,
            kind=kind,
            original_path=original_path,
            event_index=event_index,
        )
        tombstone = history.tombstone_path if history is not None else None
        original_exists = self.fs.lexists(original_path)
        tombstone_exists = tombstone is not None and self.fs.lexists(tombstone)
        if original_exists and tombstone_exists:
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "both transaction path and cleanup tombstone exist; neither will be removed",
                tombstone,
            )
        if history is not None and history.purged:
            if original_exists or tombstone_exists:
                raise DatasetExecutionError(
                    ExecutionCode.ROLLBACK_FAILED,
                    "a purged transaction-created path reappeared",
                    original_path if original_exists else tombstone,
                )
            _stable_digest(
                keeper_path,
                self.fs,
                expected=keeper_proof,
                require_original_identity=keeper_original_identity,
            )
            return False
        if not original_exists and not tombstone_exists:
            if history is None:
                return False
            if not history.tombstoned:
                raise DatasetExecutionError(
                    ExecutionCode.ROLLBACK_FAILED,
                    "transaction-created entry disappeared before a durable tombstoned record",
                    original_path,
                )
            _stable_digest(
                keeper_path,
                self.fs,
                expected=keeper_proof,
                require_original_identity=keeper_original_identity,
            )
            journal.append(
                document,
                _JournalEvent.CLEANUP_PURGED,
                {
                    "ordinal": record.ordinal,
                    "kind": kind,
                    "tombstone_path": str(history.tombstone_path),
                },
            )
            return False
        if original_exists and created_identity is None and history is None:
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "transaction-created path has no durable inode binding; preserving it",
                original_path,
            )
        staged = tombstone if tombstone_exists else original_path
        assert staged is not None
        expected_identity = (history.stat_device, history.stat_inode) if history is not None else created_identity
        if expected_identity is None:
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "cleanup tombstone has no durable inode binding",
                staged,
            )
        if created_identity is not None and expected_identity != created_identity:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "cleanup history does not match the transaction-created inode",
                staged,
            )
        with _open_verified_equal_pair(
            staged,
            keeper_path,
            self.fs,
            first_expected=record.source,
            second_expected=keeper_proof,
            first_original_identity=False,
            second_original_identity=keeper_original_identity,
            first_created_identity=expected_identity,
            require_byte_equal=kind == "destination",
        ) as (target_handle, keeper_handle, target_opened, keeper_opened):
            if staged == original_path:
                if history is None:
                    for _attempt in range(8):
                        candidate = self._new_tombstone_path(
                            document,
                            record,
                            original_path,
                            marker,
                        )
                        if not self.fs.lexists(candidate):
                            tombstone = candidate
                            break
                    else:
                        raise DatasetExecutionError(
                            ExecutionCode.DESTINATION_CONFLICT,
                            "could not reserve an unpredictable cleanup tombstone name",
                            original_path.parent,
                        )
                    journal.append(
                        document,
                        _JournalEvent.CLEANUP_TOMBSTONE_PREPARED,
                        {
                            "ordinal": record.ordinal,
                            "kind": kind,
                            "original_path": str(original_path),
                            "tombstone_path": str(tombstone),
                            "stat_device": int(target_opened.st_dev),
                            "stat_inode": int(target_opened.st_ino),
                        },
                    )
                    history = _TombstoneHistory(
                        original_path=_absolute(original_path),
                        tombstone_path=_absolute(tombstone),
                        stat_device=int(target_opened.st_dev),
                        stat_inode=int(target_opened.st_ino),
                        tombstoned=False,
                        purged=False,
                    )
                else:
                    tombstone = history.tombstone_path
                self._call_fault("before_cleanup_tombstone", record)
                _require_path_matches_stat(original_path, target_opened)
                _require_path_matches_stat(keeper_path, keeper_opened)
                try:
                    try:
                        rename_commit = self.fs.rename_no_replace_verified(
                            original_path,
                            tombstone,
                            target_handle,
                        )
                    except UnverifiedRenameCommitError as error:
                        raise DatasetExecutionError(
                            ExecutionCode.ROLLBACK_FAILED,
                            str(error),
                            error.destination,
                        ) from error
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "cleanup tombstone appeared during atomic isolation",
                        tombstone,
                    ) from error
                self.fs.fsync_directory(original_path.parent)
                self._call_fault("after_cleanup_tombstone", record)
                if self.fs.lexists(original_path):
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "transaction-created path reappeared after tombstoning",
                        original_path,
                    )
                target_opened = _rebind_open_version_after_atomic_rename(
                    target_handle,
                    target_opened,
                    tombstone,
                    rename_commit,
                )
                _require_path_matches_stat(keeper_path, keeper_opened)
                journal.append(
                    document,
                    _JournalEvent.CLEANUP_TOMBSTONED,
                    {
                        "ordinal": record.ordinal,
                        "kind": kind,
                        "original_path": str(original_path),
                        "tombstone_path": str(tombstone),
                        "stat_device": int(target_opened.st_dev),
                        "stat_inode": int(target_opened.st_ino),
                    },
                )
                history = _TombstoneHistory(
                    original_path=history.original_path,
                    tombstone_path=history.tombstone_path,
                    stat_device=history.stat_device,
                    stat_inode=history.stat_inode,
                    tombstoned=True,
                    purged=False,
                )
            else:
                assert history is not None
                tombstone = history.tombstone_path
                if not history.tombstoned:
                    journal.append(
                        document,
                        _JournalEvent.CLEANUP_TOMBSTONED,
                        {
                            "ordinal": record.ordinal,
                            "kind": kind,
                            "original_path": str(original_path),
                            "tombstone_path": str(tombstone),
                            "stat_device": history.stat_device,
                            "stat_inode": history.stat_inode,
                        },
                    )
                    history = _TombstoneHistory(
                        original_path=history.original_path,
                        tombstone_path=history.tombstone_path,
                        stat_device=history.stat_device,
                        stat_inode=history.stat_inode,
                        tombstoned=True,
                        purged=False,
                    )
            self._call_fault("before_cleanup_purge", record)
            _require_path_matches_stat(tombstone, target_opened)
            _require_path_matches_stat(keeper_path, keeper_opened)
            _reverify_open_equal_pair(
                target_handle,
                keeper_handle,
                target_opened,
                keeper_opened,
                first_expected=record.source,
                second_expected=keeper_proof,
                first_path=tombstone,
                second_path=keeper_path,
                require_byte_equal=kind == "destination",
            )
            if not self.fs.delete_verified_regular_file(
                tombstone,
                target_handle,
            ):
                raise DatasetExecutionError(
                    ExecutionCode.ROLLBACK_FAILED,
                    "cleanup tombstone no longer identifies the verified transaction payload",
                    tombstone,
                )
            _require_open_version(keeper_opened, os.fstat(keeper_handle.fileno()), keeper_path)
            _require_path_matches_stat(keeper_path, keeper_opened)
        journal.append(
            document,
            _JournalEvent.CLEANUP_PURGED,
            {
                "ordinal": record.ordinal,
                "kind": kind,
                "tombstone_path": str(tombstone),
            },
        )
        return True

    def _rollback_document(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        *,
        restore: bool,
        known_published: Optional[set[int]] = None,
    ) -> None:
        published = set(known_published or ())
        prepared_identities: Dict[int, Tuple[int, int]] = {}
        events = journal.events_for(document)
        event_index = _JournalEventIndex.build(document, events)
        event_signatures = {(event.event, _canonical_json(dict(event.details))) for event in events}

        def append_once(
            event: _JournalEvent,
            details: Optional[Mapping[str, Any]] = None,
        ) -> None:
            payload = dict(details or {})
            signature = (event, _canonical_json(payload))
            if signature in event_signatures:
                return
            journal.append(document, event, payload)
            event_signatures.add(signature)

        published.update(
            _required_int(event.details["ordinal"], "published ordinal")
            for event in events
            if event.event is _JournalEvent.DESTINATION_PUBLISHED and "ordinal" in event.details
        )
        record_by_ordinal = {record.ordinal: record for record in document.files}
        for event in events:
            if event.event is not _JournalEvent.DESTINATION_PREPARED:
                continue
            try:
                ordinal = _required_int(event.details["ordinal"], "prepared ordinal")
                expected_device = _required_int(event.details["stat_device"], "prepared device")
                expected_inode = _required_int(event.details["stat_inode"], "prepared inode", minimum=1)
            except (KeyError, ValueError) as error:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "invalid destination preparation record",
                ) from error
            record = record_by_ordinal.get(ordinal)
            if record is None or record.destination is None:
                raise DatasetExecutionError(
                    ExecutionCode.JOURNAL_CORRUPT,
                    "destination preparation references an unknown file",
                )
            prepared_identities[ordinal] = (expected_device, expected_inode)
            destination = Path(record.destination)
            if not self.fs.lexists(destination):
                continue
            destination_stat = os.stat(destination, follow_symlinks=False)
            if (
                _is_plain_file(destination_stat)
                and int(destination_stat.st_dev) == expected_device
                and int(destination_stat.st_ino) == expected_inode
            ):
                published.add(ordinal)
        # Quarantined originals are restored before transaction-created copies are removed or
        # same-volume keeper moves are reversed.  This keeps every exact-duplicate comparison
        # target available throughout recovery.
        for record in reversed(document.files):
            if record.strategy not in {_Strategy.QUARANTINE, _Strategy.CROSS_VOLUME_COPY}:
                continue
            self._restore_quarantined_source(record)
            append_once(
                _JournalEvent.FILE_RESTORED if restore else _JournalEvent.FILE_ROLLED_BACK,
                {"ordinal": record.ordinal, "phase": "source"},
            )

        for record in reversed(document.files):
            if record.strategy is _Strategy.SAME_VOLUME_RENAME:
                self._reverse_same_volume_move(
                    record,
                    may_remove=record.ordinal in published,
                )
            elif record.strategy is _Strategy.CROSS_VOLUME_COPY:
                self._remove_cross_volume_destination(
                    document,
                    journal,
                    record,
                    may_remove=record.ordinal in published,
                    created_identity=prepared_identities.get(record.ordinal),
                    event_index=event_index,
                )
            self._remove_temporary(
                document,
                journal,
                record,
                event_index=event_index,
            )
            append_once(
                _JournalEvent.FILE_RESTORED if restore else _JournalEvent.FILE_ROLLED_BACK,
                {"ordinal": record.ordinal, "phase": "destination"},
            )

        status_index = self._journal_event_index(document, journal)
        statuses = tuple(
            self._record_presence(
                record,
                document=document,
                journal=journal,
                event_index=status_index,
            )
            for record in document.files
        )
        if any(status not in {"original", "original_conflict"} for status in statuses):
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "rollback did not restore every record to its original state: {}".format(statuses),
            )
        append_once(_JournalEvent.RESTORED if restore else _JournalEvent.ROLLED_BACK)

    def _restore_quarantined_source(self, record: _FileRecord) -> None:
        assert record.quarantine_path is not None
        source = Path(record.source.path)
        quarantine = Path(record.quarantine_path)
        self._validate_record_quarantine_directory(record)
        source_exists = self.fs.lexists(source)
        quarantine_exists = self.fs.lexists(quarantine)
        if source_exists and quarantine_exists:
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)
            # Native no-replace rename is atomic and cannot legitimately leave both names.
            # Whether the second name is a distinct file or an externally-created hardlink, it is
            # not safe to path-unlink during recovery.
            return
        if source_exists:
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)
            return
        if not quarantine_exists:
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "neither original source nor quarantine payload exists",
                source,
            )
        _validate_plain_directory(source.parent)
        with _open_verified_payload(
            quarantine,
            self.fs,
            expected=record.source,
            require_original_identity=True,
        ) as (quarantine_handle, quarantine_opened):
            try:
                try:
                    rename_commit = self.fs.rename_no_replace_verified(
                        quarantine,
                        source,
                        quarantine_handle,
                    )
                except UnverifiedRenameCommitError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.ROLLBACK_FAILED,
                        str(error),
                        error.destination,
                    ) from error
            except FileExistsError as error:
                raise DatasetExecutionError(
                    ExecutionCode.ROLLBACK_FAILED,
                    "source path appeared during restore",
                    source,
                ) from error
            self.fs.fsync_directory(quarantine.parent)
            self.fs.fsync_directory(source.parent)
            _rebind_open_version_after_atomic_rename(
                quarantine_handle,
                quarantine_opened,
                source,
                rename_commit,
            )
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)

    def _reverse_same_volume_move(
        self,
        record: _FileRecord,
        *,
        may_remove: bool,
    ) -> None:
        assert record.destination is not None
        source = Path(record.source.path)
        destination = Path(record.destination)
        source_exists = self.fs.lexists(source)
        destination_exists = self.fs.lexists(destination)
        if source_exists and destination_exists:
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)
            # Atomic rename-no-replace cannot produce two names.  Preserve an external collision
            # (including a hardlink to the original) instead of attempting a verify-then-unlink.
            return
        if source_exists:
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)
            return
        if not destination_exists:
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "neither original source nor move destination exists",
                source,
            )
        _validate_plain_directory(source.parent)
        with _open_verified_payload(
            destination,
            self.fs,
            expected=record.source,
            require_original_identity=True,
        ) as (destination_handle, destination_opened):
            try:
                try:
                    rename_commit = self.fs.rename_no_replace_verified(
                        destination,
                        source,
                        destination_handle,
                    )
                except UnverifiedRenameCommitError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.ROLLBACK_FAILED,
                        str(error),
                        error.destination,
                    ) from error
            except FileExistsError as error:
                raise DatasetExecutionError(
                    ExecutionCode.ROLLBACK_FAILED,
                    "source path appeared during move rollback",
                    source,
                ) from error
            self.fs.fsync_directory(destination.parent)
            self.fs.fsync_directory(source.parent)
            _rebind_open_version_after_atomic_rename(
                destination_handle,
                destination_opened,
                source,
                rename_commit,
            )
            _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)

    def _remove_cross_volume_destination(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        may_remove: bool,
        created_identity: Optional[Tuple[int, int]],
        event_index: Optional[_JournalEventIndex] = None,
    ) -> None:
        assert record.destination is not None
        source = Path(record.source.path)
        destination = Path(record.destination)
        if not self.fs.lexists(source):
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "cross-volume source must be restored before removing its copy",
                source,
            )
        _stable_digest(source, self.fs, expected=record.source, require_original_identity=True)
        if not self.fs.lexists(destination):
            return
        if not may_remove:
            # Without a durable publication record (or in-process proof) an identical external
            # file is indistinguishable from a crash-completed copy.  Preserve it fail-closed.
            return
        self._purge_transaction_created_entry(
            document,
            journal,
            record,
            kind="destination",
            original_path=destination,
            created_identity=created_identity,
            keeper_path=source,
            keeper_proof=record.source,
            keeper_original_identity=True,
            event_index=event_index,
        )

    def _remove_temporary(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> None:
        if record.temporary_path is None:
            return
        temporary = Path(record.temporary_path)
        created_identity = self._created_identity(
            document,
            journal,
            record,
            _JournalEvent.TEMPORARY_CREATED,
            event_index=event_index,
        )
        if not self.fs.lexists(temporary):
            history = self._cleanup_history(
                document,
                journal,
                record,
                kind="temporary",
                original_path=temporary,
                event_index=event_index,
            )
            if history is None or history.purged:
                return
        source = Path(record.source.path)
        if not self.fs.lexists(source):
            raise DatasetExecutionError(
                ExecutionCode.ROLLBACK_FAILED,
                "source must be restored before removing a copy temporary",
                source,
            )
        self._purge_transaction_created_entry(
            document,
            journal,
            record,
            kind="temporary",
            original_path=temporary,
            created_identity=created_identity,
            keeper_path=source,
            keeper_proof=record.source,
            keeper_original_identity=True,
            event_index=event_index,
        )

    def _finalize_history(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> Optional[_TombstoneHistory]:
        assert record.quarantine_path is not None
        return self._tombstone_history(
            document,
            journal,
            record,
            original_path=Path(record.quarantine_path),
            marker="f",
            prepared_event=_JournalEvent.FINALIZE_TOMBSTONE_PREPARED,
            tombstoned_event=_JournalEvent.FILE_TOMBSTONED,
            purged_event=_JournalEvent.FILE_FINALIZED,
            event_index=event_index,
        )

    def _comparison_binding(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        destination_records: Optional[Mapping[str, _FileRecord]] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> Tuple[DatasetFileProof, bool, Optional[Tuple[int, int]]]:
        assert record.comparison_path is not None
        comparison_key = _path_key(record.comparison_path)
        if destination_records is None:
            destination_records = {
                _path_key(candidate.destination): candidate
                for candidate in document.files
                if candidate.destination is not None
            }
        keeper_record = destination_records.get(comparison_key)
        if keeper_record is None:
            if record.reference is None or _path_key(record.reference.path) != comparison_key:
                raise DatasetExecutionError(
                    ExecutionCode.DOCUMENT_CONFLICT,
                    "finalize comparison path has no immutable keeper binding",
                    Path(record.comparison_path),
                )
            return record.reference, True, None
        if keeper_record.strategy is _Strategy.SAME_VOLUME_RENAME:
            return keeper_record.source, True, None
        if keeper_record.strategy is not _Strategy.CROSS_VOLUME_COPY:
            raise DatasetExecutionError(
                ExecutionCode.DOCUMENT_CONFLICT,
                "finalize comparison path is not a surviving move destination",
                Path(record.comparison_path),
            )
        created_identity = self._created_identity(
            document,
            journal,
            keeper_record,
            _JournalEvent.DESTINATION_PREPARED,
            event_index=event_index,
        )
        if created_identity is None:
            raise DatasetExecutionError(
                ExecutionCode.JOURNAL_CORRUPT,
                "cross-volume keeper has no durable transaction-created identity",
                Path(record.comparison_path),
            )
        return keeper_record.source, False, created_identity

    def _verify_bound_keeper(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        destination_records: Optional[Mapping[str, _FileRecord]] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> None:
        assert record.comparison_path is not None
        comparison = Path(record.comparison_path)
        proof, original_identity, created_identity = self._comparison_binding(
            document,
            journal,
            record,
            destination_records=destination_records,
            event_index=event_index,
        )
        _stable_digest(
            comparison,
            self.fs,
            expected=proof,
            require_original_identity=original_identity,
        )
        if created_identity is not None:
            current = os.stat(comparison, follow_symlinks=False)
            if (int(current.st_dev), int(current.st_ino)) != created_identity:
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "transaction-created keeper identity changed",
                    comparison,
                )

    def _preflight_finalize(self, document: _ExecutionDocument, journal: _AppendOnlyJournal) -> None:
        # Every file is checked before the first irreversible mutation.  Missing quarantine names
        # are accepted only when the per-file durable tombstone history proves an interrupted purge.
        event_index = self._journal_event_index(document, journal)
        destination_records = {
            _path_key(record.destination): record for record in document.files if record.destination is not None
        }
        for record in document.files:
            if record.quarantine_path is None:
                self._verify_record_destination(
                    record,
                    original_identity=record.strategy is _Strategy.SAME_VOLUME_RENAME,
                )
                continue
            self._preflight_finalize_record(
                document,
                journal,
                record,
                destination_records=destination_records,
                event_index=event_index,
            )

    def _preflight_finalize_record(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        destination_records: Optional[Mapping[str, _FileRecord]] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> None:
        assert record.quarantine_path is not None
        assert record.comparison_path is not None
        quarantine = Path(record.quarantine_path)
        self._validate_record_quarantine_directory(record)
        history = self._finalize_history(
            document,
            journal,
            record,
            event_index=event_index,
        )
        tombstone = history.tombstone_path if history is not None else None
        quarantine_exists = self.fs.lexists(quarantine)
        tombstone_exists = tombstone is not None and self.fs.lexists(tombstone)
        if quarantine_exists and tombstone_exists:
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "both quarantine and finalize tombstone exist; neither will be removed",
                tombstone,
            )
        if history is not None and history.purged:
            if quarantine_exists or tombstone_exists:
                raise DatasetExecutionError(
                    ExecutionCode.DESTINATION_CONFLICT,
                    "a finalized dataset payload path reappeared",
                    quarantine if quarantine_exists else tombstone,
                )
            self._verify_bound_keeper(
                document,
                journal,
                record,
                destination_records=destination_records,
                event_index=event_index,
            )
            return
        if not quarantine_exists and not tombstone_exists:
            if history is not None and history.tombstoned:
                self._verify_bound_keeper(
                    document,
                    journal,
                    record,
                    destination_records=destination_records,
                    event_index=event_index,
                )
                return
            raise DatasetExecutionError(
                ExecutionCode.INVALID_STATE,
                "dataset quarantine is missing without a durable tombstoned record",
                quarantine,
            )
        if tombstone_exists and (history is None or not history.tombstoned):
            # A crash can happen after the atomic rename and before FILE_TOMBSTONED is appended.
            # Its durable preparation still identifies the unpredictable tombstone and inode.
            if history is None:
                raise DatasetExecutionError(
                    ExecutionCode.DESTINATION_CONFLICT,
                    "unbound finalize tombstone exists",
                    tombstone,
                )
        staged = tombstone if tombstone_exists else quarantine
        assert staged is not None
        keeper_proof, keeper_original, keeper_created = self._comparison_binding(
            document,
            journal,
            record,
            destination_records=destination_records,
            event_index=event_index,
        )
        with _open_verified_equal_pair(
            staged,
            Path(record.comparison_path),
            self.fs,
            first_expected=record.source,
            second_expected=keeper_proof,
            first_original_identity=True,
            second_original_identity=keeper_original,
            second_created_identity=keeper_created,
        ):
            pass
        if history is not None:
            staged_stat = os.stat(staged, follow_symlinks=False)
            if (int(staged_stat.st_dev), int(staged_stat.st_ino)) != (
                history.stat_device,
                history.stat_inode,
            ):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "finalize tombstone identity no longer matches its durable preparation",
                    staged,
                )

    def _finalize_record(
        self,
        document: _ExecutionDocument,
        journal: _AppendOnlyJournal,
        record: _FileRecord,
        *,
        on_mutation: Optional[Callable[[], None]] = None,
        destination_records: Optional[Mapping[str, _FileRecord]] = None,
        event_index: Optional[_JournalEventIndex] = None,
    ) -> bool:
        assert record.quarantine_path is not None
        assert record.comparison_path is not None
        quarantine = Path(record.quarantine_path)
        comparison = Path(record.comparison_path)
        self._validate_record_quarantine_directory(record)
        history = self._finalize_history(
            document,
            journal,
            record,
            event_index=event_index,
        )
        tombstone = history.tombstone_path if history is not None else None
        quarantine_exists = self.fs.lexists(quarantine)
        tombstone_exists = tombstone is not None and self.fs.lexists(tombstone)
        if quarantine_exists and tombstone_exists:
            raise DatasetExecutionError(
                ExecutionCode.DESTINATION_CONFLICT,
                "both quarantine and finalize tombstone exist; neither will be removed",
                tombstone,
            )
        if history is not None and history.purged:
            if quarantine_exists or tombstone_exists:
                raise DatasetExecutionError(
                    ExecutionCode.DESTINATION_CONFLICT,
                    "a finalized dataset payload path reappeared",
                    quarantine if quarantine_exists else tombstone,
                )
            self._verify_bound_keeper(
                document,
                journal,
                record,
                destination_records=destination_records,
                event_index=event_index,
            )
            return False
        if not quarantine_exists and not tombstone_exists:
            if history is None or not history.tombstoned:
                raise DatasetExecutionError(
                    ExecutionCode.INVALID_STATE,
                    "dataset quarantine is missing without a durable tombstoned record",
                    quarantine,
                )
            self._verify_bound_keeper(
                document,
                journal,
                record,
                destination_records=destination_records,
                event_index=event_index,
            )
            journal.append(
                document,
                _JournalEvent.FILE_FINALIZED,
                {"ordinal": record.ordinal, "tombstone_path": str(history.tombstone_path)},
            )
            return False

        staged = tombstone if tombstone_exists else quarantine
        assert staged is not None
        keeper_proof, keeper_original, keeper_created = self._comparison_binding(
            document,
            journal,
            record,
            destination_records=destination_records,
            event_index=event_index,
        )
        with _open_verified_equal_pair(
            staged,
            comparison,
            self.fs,
            first_expected=record.source,
            second_expected=keeper_proof,
            first_original_identity=True,
            second_original_identity=keeper_original,
            second_created_identity=keeper_created,
        ) as (target_handle, keeper_handle, target_opened, keeper_opened):
            if staged == quarantine:
                if history is None:
                    for _attempt in range(8):
                        candidate = self._new_tombstone_path(
                            document,
                            record,
                            quarantine,
                            "f",
                        )
                        if not self.fs.lexists(candidate):
                            tombstone = candidate
                            break
                    else:
                        raise DatasetExecutionError(
                            ExecutionCode.DESTINATION_CONFLICT,
                            "could not reserve an unpredictable finalize tombstone name",
                            quarantine.parent,
                        )
                    history_details = {
                        "ordinal": record.ordinal,
                        "original_path": str(quarantine),
                        "tombstone_path": str(tombstone),
                        "stat_device": int(target_opened.st_dev),
                        "stat_inode": int(target_opened.st_ino),
                    }
                    journal.append(
                        document,
                        _JournalEvent.FINALIZE_TOMBSTONE_PREPARED,
                        history_details,
                    )
                    history = _TombstoneHistory(
                        original_path=_absolute(quarantine),
                        tombstone_path=_absolute(tombstone),
                        stat_device=int(target_opened.st_dev),
                        stat_inode=int(target_opened.st_ino),
                        tombstoned=False,
                        purged=False,
                    )
                else:
                    tombstone = history.tombstone_path
                self._call_fault("before_finalize_tombstone", record)
                _require_path_matches_stat(quarantine, target_opened)
                _require_path_matches_stat(comparison, keeper_opened)
                try:
                    try:
                        rename_commit = self.fs.rename_no_replace_verified(
                            quarantine,
                            tombstone,
                            target_handle,
                        )
                    except UnverifiedRenameCommitError as error:
                        if on_mutation is not None:
                            on_mutation()
                        raise DatasetExecutionError(
                            ExecutionCode.SOURCE_CHANGED,
                            str(error),
                            error.destination,
                        ) from error
                    if on_mutation is not None:
                        on_mutation()
                except FileExistsError as error:
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "finalize tombstone appeared during atomic isolation",
                        tombstone,
                    ) from error
                self.fs.fsync_directory(quarantine.parent)
                self._call_fault("after_finalize_tombstone", record)
                if self.fs.lexists(quarantine):
                    raise DatasetExecutionError(
                        ExecutionCode.DESTINATION_CONFLICT,
                        "quarantine name reappeared after atomic tombstoning",
                        quarantine,
                    )
                target_opened = _rebind_open_version_after_atomic_rename(
                    target_handle,
                    target_opened,
                    tombstone,
                    rename_commit,
                )
                _require_path_matches_stat(comparison, keeper_opened)
                _reverify_open_equal_pair(
                    target_handle,
                    keeper_handle,
                    target_opened,
                    keeper_opened,
                    first_expected=record.source,
                    second_expected=keeper_proof,
                    first_path=tombstone,
                    second_path=comparison,
                )
                journal.append(
                    document,
                    _JournalEvent.FILE_TOMBSTONED,
                    {
                        "ordinal": record.ordinal,
                        "original_path": str(quarantine),
                        "tombstone_path": str(tombstone),
                        "stat_device": int(target_opened.st_dev),
                        "stat_inode": int(target_opened.st_ino),
                    },
                )
                history = _TombstoneHistory(
                    original_path=history.original_path,
                    tombstone_path=history.tombstone_path,
                    stat_device=history.stat_device,
                    stat_inode=history.stat_inode,
                    tombstoned=True,
                    purged=False,
                )
                self._call_fault("after_finalize_tombstoned_record", record)
            else:
                assert history is not None
                tombstone = history.tombstone_path
                if not history.tombstoned:
                    journal.append(
                        document,
                        _JournalEvent.FILE_TOMBSTONED,
                        {
                            "ordinal": record.ordinal,
                            "original_path": str(quarantine),
                            "tombstone_path": str(tombstone),
                            "stat_device": history.stat_device,
                            "stat_inode": history.stat_inode,
                        },
                    )
                    history = _TombstoneHistory(
                        original_path=history.original_path,
                        tombstone_path=history.tombstone_path,
                        stat_device=history.stat_device,
                        stat_inode=history.stat_inode,
                        tombstoned=True,
                        purged=False,
                    )

            self._call_fault("before_finalize_purge", record)
            _require_path_matches_stat(tombstone, target_opened)
            _require_path_matches_stat(comparison, keeper_opened)
            _reverify_open_equal_pair(
                target_handle,
                keeper_handle,
                target_opened,
                keeper_opened,
                first_expected=record.source,
                second_expected=keeper_proof,
                first_path=tombstone,
                second_path=comparison,
            )
            if not self.fs.delete_verified_regular_file(
                tombstone,
                target_handle,
            ):
                raise DatasetExecutionError(
                    ExecutionCode.SOURCE_CHANGED,
                    "finalize tombstone no longer identifies the verified quarantine payload",
                    tombstone,
                )
            if on_mutation is not None:
                on_mutation()
            _require_open_version(keeper_opened, os.fstat(keeper_handle.fileno()), comparison)
            _require_path_matches_stat(comparison, keeper_opened)
            self._call_fault("after_finalize_purge", record)
        journal.append(
            document,
            _JournalEvent.FILE_FINALIZED,
            {"ordinal": record.ordinal, "tombstone_path": str(tombstone)},
        )
        return True

    def _file_result(
        self,
        record: _FileRecord,
        state: FileExecutionState,
        changed: bool,
    ) -> FileExecutionResult:
        messages = {
            FileExecutionState.PLANNED: "fully preflighted",
            FileExecutionState.DESTINATION_PUBLISHED: "destination safely published",
            FileExecutionState.QUARANTINED: "source moved to same-volume quarantine",
            FileExecutionState.MOVED: "source moved with no-overwrite semantics",
            FileExecutionState.APPLIED: "bundle file completely applied",
            FileExecutionState.RESTORED: "source restored",
            FileExecutionState.FINALIZED: "quarantine explicitly finalized",
            FileExecutionState.UNCHANGED: "source remains at its original path",
            FileExecutionState.FAILED: "file requires recovery inspection",
        }
        return FileExecutionResult(
            action_id=record.action_id,
            source=record.source.path,
            destination=record.destination,
            quarantine_path=record.quarantine_path,
            state=state,
            changed=changed,
            message=messages[state],
        )

    @staticmethod
    def _failure_report(
        plan_id: str,
        code: ExecutionCode,
        message: str,
        *,
        path: Optional[Path] = None,
        issues: Tuple[DatasetIssue, ...] = (),
    ) -> DatasetExecutionReport:
        if path is not None:
            message = "{} ({})".format(message, path)
        return DatasetExecutionReport(
            plan_id=plan_id,
            state=ExecutionState.FAILED,
            code=code,
            message=message,
            changed=False,
            issues=issues,
        )


# The plan-level name is useful for callers that do not otherwise expose bundles.
DatasetPlanExecutor = DatasetBundleExecutor


__all__ = [
    "DatasetBundleExecutor",
    "DatasetExecutionError",
    "DatasetExecutionReport",
    "DatasetOperationSummary",
    "DatasetPlanExecutor",
    "ExecutionCode",
    "ExecutionState",
    "FileExecutionResult",
    "FileExecutionState",
    "MAX_EXECUTION_TRANSACTION_FILES",
    "MAX_PERSISTED_EXECUTION_OPERATIONS",
]
