# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Fail-closed service primitives for sidecar-aware image dataset preparation.

This module plans work; it never moves, quarantines, or deletes a source file.  Every planned
source is bound to a stable identity, metadata tuple, and SHA-256 digest so a future safe-action
adapter can revalidate the complete bundle immediately before mutation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from core import fs as core_fs
from core.file_generation import FileGenerationToken
from core.file_identity import FileIdentity, FileIdentityError, get_file_identity
from core.keeper import KeeperDecision, choose_keeper
from core.pe.asset_bundle import (
    AssetBundle,
    SidecarAsset,
    SidecarIssue,
    SidecarPolicy,
    SidecarReadStatus,
    audit_sidecar_conflicts,
    build_asset_bundles,
)
from core.pe.dataset import ClusterUnit, SplitManifest, build_stable_split_manifest
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_reserved_internal_file,
    is_unsafe_path_component,
    is_within_reserved_internal_directory,
)
from core.safe_action import cleanup_created_regular_file, platform_file_system
from core.safe_json import JsonStructuralLimits, JsonStructureError, preflight_json_structure
from core.safe_walk import WalkEventKind, is_reparse_point, walk_no_follow

DATASET_PLAN_SCHEMA = "dupeguru.dataset-plan"
DATASET_PLAN_SCHEMA_VERSION = 2
EXECUTOR_CONTRACT = "dupeguru.safe-action-quarantine-bundle.v1"
HASH_ALGORITHM = "sha256"
READ_CHUNK_SIZE = 1024 * 1024
IMMUTABLE_PROTECTED_REASON = "immutable_protected"
MAX_DATASET_PLAN_ACTIONS = 250_000
MAX_DATASET_PLAN_FILE_RECORDS = 250_000
MAX_DATASET_PLAN_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_DATASET_EXPORT_BYTES = MAX_DATASET_PLAN_DOCUMENT_BYTES
MAX_JSON_SIDECAR_DEPTH = 128
MAX_JSON_SIDECAR_NODES = 250_000
MAX_JSON_SIDECAR_CONTAINER_ITEMS = 100_000
MAX_JSON_SIDECAR_STRING_CHARACTERS = 4 * 1024 * 1024


class DatasetRelation(Enum):
    VERIFIED_EXACT = "verified_exact"
    NEAR_DUPLICATE = "near_duplicate"
    TRANSFORMED = "transformed"
    RELATED = "related"

    @property
    def quarantine_eligible(self) -> bool:
        return self is DatasetRelation.VERIFIED_EXACT


@dataclass(frozen=True)
class DatasetAsset:
    asset_id: str
    path: str
    dimensions: Optional[Tuple[int, int]] = None
    bit_depth: float = 0
    metadata_count: float = 0
    jpeg_artifact_score: float = 0
    protected: bool = False
    immutable: bool = False

    def __post_init__(self) -> None:
        if not self.asset_id or "\0" in self.asset_id or not self.path or "\0" in self.path:
            raise ValueError("dataset asset requires a safe ID and path")
        if self.dimensions is not None:
            if len(self.dimensions) != 2 or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in self.dimensions
            ):
                raise ValueError("asset dimensions must contain two positive values")
        for value in (self.bit_depth, self.metadata_count, self.jpeg_artifact_score):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("asset quality values must be non-negative numbers")
        if not isinstance(self.protected, bool):
            raise ValueError("asset protected state must be boolean")
        if not isinstance(self.immutable, bool):
            raise ValueError("asset immutable state must be boolean")
        if self.immutable and not self.protected:
            raise ValueError("immutable dataset assets must also be protected")


@dataclass(frozen=True)
class DatasetCluster:
    members: Tuple[str, ...]
    relation: DatasetRelation
    evidence_complete: bool = True
    evidence_version: str = "dataset-evidence-v1"

    def __post_init__(self) -> None:
        members = tuple(sorted(set(self.members)))
        if len(members) < 2 or any(not member for member in members):
            raise ValueError("dataset cluster requires at least two distinct assets")
        if not isinstance(self.relation, DatasetRelation):
            raise ValueError("dataset cluster relation is unsupported")
        if not isinstance(self.evidence_complete, bool):
            raise ValueError("dataset cluster evidence completeness must be boolean")
        if not self.evidence_version:
            raise ValueError("dataset cluster evidence version must not be empty")
        object.__setattr__(self, "members", members)

    @property
    def cluster_id(self) -> str:
        return ClusterUnit.from_members(self.members).cluster_id


class PreparationState(Enum):
    COMPLETE = "complete"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    CONFLICT = "conflict"
    SOURCE_CHANGED = "source_changed"
    FAILED = "failed"


@dataclass(frozen=True)
class DatasetIssue:
    code: str
    message: str
    paths: Tuple[str, ...] = ()
    asset_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("dataset issue requires a code and message")
        object.__setattr__(self, "paths", tuple(sorted(set(self.paths))))
        object.__setattr__(self, "asset_ids", tuple(sorted(set(self.asset_ids))))

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "paths": list(self.paths),
            "asset_ids": list(self.asset_ids),
        }


@dataclass(frozen=True)
class DatasetFileProof:
    path: str
    resolved_path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    generation_token: str
    digest_algorithm: str
    digest_hex: str
    identity_namespace: str
    identity_capability: str
    volume_id: int
    file_id: str
    stat_device: int
    stat_inode: int

    def __post_init__(self) -> None:
        if not self.path or "\0" in self.path or not self.resolved_path or "\0" in self.resolved_path:
            raise ValueError("file proof requires source and resolved paths")
        if self.size < 0 or self.mtime_ns < 0 or self.ctime_ns < 0:
            raise ValueError("file proof metadata must be non-negative")
        if self.digest_algorithm != HASH_ALGORITHM:
            raise ValueError("dataset file proof requires SHA-256")
        if len(self.digest_hex) != hashlib.sha256().digest_size * 2:
            raise ValueError("dataset file proof has an invalid digest length")
        try:
            bytes.fromhex(self.digest_hex)
        except ValueError as error:
            raise ValueError("dataset file proof digest is not hexadecimal") from error
        if not self.identity_namespace or not self.identity_capability or not self.file_id or self.volume_id < 0:
            raise ValueError("dataset file proof requires a physical identity")
        if self.stat_device < 0 or self.stat_inode <= 0:
            raise ValueError("dataset file proof requires a valid handle identity")
        try:
            FileGenerationToken.from_encoded(bytes.fromhex(self.generation_token))
        except ValueError as error:
            raise ValueError("dataset file proof requires a valid generation token") from error

    @property
    def identity_key(self) -> Tuple[str, str, int, str]:
        return (
            self.identity_namespace,
            self.identity_capability,
            self.volume_id,
            self.file_id,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "generation_token": self.generation_token,
            "digest_algorithm": self.digest_algorithm,
            "digest_hex": self.digest_hex,
            "identity_namespace": self.identity_namespace,
            "identity_capability": self.identity_capability,
            "volume_id": self.volume_id,
            "file_id": self.file_id,
            "stat_device": self.stat_device,
            "stat_inode": self.stat_inode,
        }


@dataclass(frozen=True)
class KeeperReasonRecord:
    code: str
    points: float
    message: str

    def to_dict(self) -> Dict[str, object]:
        return {"code": self.code, "points": self.points, "message": self.message}


@dataclass(frozen=True)
class KeeperRecord:
    cluster_id: str
    keeper_id: str
    explanations: Tuple[Tuple[str, str], ...]
    scores: Tuple[Tuple[str, float], ...]
    reasons: Tuple[Tuple[str, Tuple[KeeperReasonRecord, ...]], ...]

    def __post_init__(self) -> None:
        if not self.cluster_id or not self.keeper_id:
            raise ValueError("keeper record requires cluster and keeper IDs")
        object.__setattr__(self, "explanations", tuple(sorted(self.explanations)))
        object.__setattr__(self, "scores", tuple(sorted(self.scores)))
        object.__setattr__(self, "reasons", tuple(sorted(self.reasons)))

    def to_dict(self) -> Dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "keeper_id": self.keeper_id,
            "explanations": dict(self.explanations),
            "scores": dict(self.scores),
            "reasons": {asset_id: [reason.to_dict() for reason in reasons] for asset_id, reasons in self.reasons},
        }


class DatasetOperation(Enum):
    MOVE_BUNDLE = "move_bundle"
    QUARANTINE_BUNDLE = "quarantine_bundle"


@dataclass(frozen=True)
class DatasetFileAction:
    source: DatasetFileProof
    destination: Optional[str]
    reference: Optional[DatasetFileProof]
    role: str
    sidecar_slot: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"primary", "sidecar"}:
            raise ValueError("dataset file action role must be primary or sidecar")
        if self.role == "sidecar" and not self.sidecar_slot:
            raise ValueError("sidecar file action requires a slot")
        if self.role == "primary" and self.sidecar_slot:
            raise ValueError("primary file action cannot have a sidecar slot")
        if self.destination is not None and not self.destination:
            raise ValueError("file action destination must not be empty")
        if self.destination is not None and "\0" in self.destination:
            raise ValueError("file action destination contains a NUL")

    def to_dict(self) -> Dict[str, object]:
        return {
            "source": self.source.to_dict(),
            "destination": self.destination,
            "reference": self.reference.to_dict() if self.reference is not None else None,
            "role": self.role,
            "sidecar_slot": self.sidecar_slot,
        }


@dataclass(frozen=True)
class DatasetBundleAction:
    action_id: str
    asset_id: str
    cluster_id: str
    split: str
    operation: DatasetOperation
    files: Tuple[DatasetFileAction, ...]
    keeper_id: Optional[str]
    atomic: bool = True

    def __post_init__(self) -> None:
        files = tuple(sorted(self.files, key=lambda item: (item.role, item.sidecar_slot, item.source.path)))
        if not self.action_id or not self.asset_id or not self.cluster_id or not self.split:
            raise ValueError("dataset bundle action has missing identifiers")
        if not isinstance(self.operation, DatasetOperation):
            raise ValueError("dataset bundle action operation is unsupported")
        if not files or sum(item.role == "primary" for item in files) != 1:
            raise ValueError("dataset bundle action requires exactly one primary")
        if not self.atomic:
            raise ValueError("dataset bundle actions must be atomic")
        if self.operation is DatasetOperation.MOVE_BUNDLE:
            if self.keeper_id is not None or any(
                item.destination is None or item.reference is not None for item in files
            ):
                raise ValueError("move bundle actions require destinations and no references")
        else:
            if (
                not self.keeper_id
                or self.keeper_id == self.asset_id
                or any(item.destination is not None or item.reference is None for item in files)
            ):
                raise ValueError("quarantine bundle actions require a keeper reference for every file")
            for item in files:
                assert item.reference is not None
                if _path_key(item.source.path) == _path_key(item.reference.path):
                    raise ValueError("quarantine source and keeper reference must be different files")
                if (
                    item.source.size != item.reference.size
                    or item.source.digest_algorithm != item.reference.digest_algorithm
                    or item.source.digest_hex != item.reference.digest_hex
                ):
                    raise ValueError("quarantine source and keeper reference must be byte-equivalent")
        object.__setattr__(self, "files", files)
        if self.action_id != _content_id(self._identity_document()):
            raise ValueError("dataset bundle action ID does not match its immutable contents")

    def _identity_document(self) -> Dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "cluster_id": self.cluster_id,
            "split": self.split,
            "operation": self.operation.value,
            "files": [item.to_dict() for item in self.files],
            "keeper_id": self.keeper_id,
            "atomic": self.atomic,
        }

    def to_dict(self) -> Dict[str, object]:
        result = self._identity_document()
        result["action_id"] = self.action_id
        return result


@dataclass(frozen=True)
class DatasetPlan:
    plan_id: str
    allowed_roots: Tuple[str, ...]
    destination_root: str
    split_manifest: SplitManifest
    keepers: Tuple[KeeperRecord, ...]
    actions: Tuple[DatasetBundleAction, ...]
    dry_run: bool
    executor_contract: str = EXECUTOR_CONTRACT
    schema: str = DATASET_PLAN_SCHEMA
    schema_version: int = DATASET_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        unsorted_actions = tuple(self.actions)
        if len(unsorted_actions) > MAX_DATASET_PLAN_ACTIONS:
            raise ValueError(
                "dataset plan contains {} actions; maximum is {}".format(
                    len(unsorted_actions),
                    MAX_DATASET_PLAN_ACTIONS,
                )
            )
        file_record_count = sum(len(action.files) for action in unsorted_actions)
        if file_record_count > MAX_DATASET_PLAN_FILE_RECORDS:
            raise ValueError(
                "dataset plan contains {} file records; maximum is {}".format(
                    file_record_count,
                    MAX_DATASET_PLAN_FILE_RECORDS,
                )
            )
        roots = tuple(sorted(set(self.allowed_roots), key=_path_key))
        keepers = tuple(sorted(self.keepers, key=lambda item: item.cluster_id))
        actions = tuple(sorted(unsorted_actions, key=lambda item: item.action_id))
        if self.schema != DATASET_PLAN_SCHEMA or self.schema_version != DATASET_PLAN_SCHEMA_VERSION:
            raise ValueError("dataset plan schema is unsupported")
        if not self.plan_id or not roots or not self.destination_root:
            raise ValueError("dataset plan requires an ID, roots, and destination")
        if not isinstance(self.dry_run, bool):
            raise ValueError("dataset plan dry_run state must be boolean")
        if self.executor_contract != EXECUTOR_CONTRACT:
            raise ValueError("dataset plan executor contract is unsupported")
        if len({action.action_id for action in actions}) != len(actions):
            raise ValueError("dataset plan action IDs must be unique")
        source_paths = [item.source.path for action in actions for item in action.files]
        if len({_path_key(path) for path in source_paths}) != len(source_paths):
            raise ValueError("a dataset plan must not mutate a source path more than once")
        destinations = [item.destination for action in actions for item in action.files if item.destination is not None]
        if len({_path_key(path) for path in destinations}) != len(destinations):
            raise ValueError("dataset plan destinations must be unique")
        member_splits = self.split_manifest.member_splits()
        immutable_members = {
            asset_id
            for keeper in keepers
            for asset_id, reasons in keeper.reasons
            if any(reason.code == IMMUTABLE_PROTECTED_REASON for reason in reasons)
        }
        if not immutable_members <= set(member_splits):
            raise ValueError("immutable protected assets must belong to the split manifest")
        expected_action_members = set(member_splits) - immutable_members
        if {action.asset_id for action in actions} != expected_action_members:
            raise ValueError("dataset plan actions must cover every mutable split-manifest member exactly once")
        if any(member_splits[action.asset_id] != action.split for action in actions):
            raise ValueError("dataset action split does not match its cluster assignment")
        if {keeper.cluster_id for keeper in keepers} != set(self.split_manifest.cluster_splits()):
            raise ValueError("dataset keeper records must cover every split cluster")
        object.__setattr__(self, "allowed_roots", roots)
        object.__setattr__(self, "keepers", keepers)
        object.__setattr__(self, "actions", actions)
        identity_document = self._identity_document()
        expected_id = _content_id(identity_document)
        if self.plan_id != expected_id:
            raise ValueError("dataset plan ID does not match its immutable contents")
        complete_document = dict(identity_document)
        complete_document["plan_id"] = self.plan_id
        if (
            _canonical_json_byte_size(
                complete_document,
                maximum_bytes=MAX_DATASET_PLAN_DOCUMENT_BYTES,
            )
            + 1
            > MAX_DATASET_PLAN_DOCUMENT_BYTES
        ):
            raise ValueError(
                "dataset plan JSON exceeds the {}-byte limit".format(
                    MAX_DATASET_PLAN_DOCUMENT_BYTES,
                )
            )

    def _identity_document(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "allowed_roots": list(self.allowed_roots),
            "destination_root": self.destination_root,
            "split_manifest": self.split_manifest.to_dict(),
            "keepers": [keeper.to_dict() for keeper in self.keepers],
            "actions": [action.to_dict() for action in self.actions],
            "dry_run": self.dry_run,
            "executor_contract": self.executor_contract,
        }

    def to_dict(self) -> Dict[str, object]:
        result = self._identity_document()
        result["plan_id"] = self.plan_id
        return result

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class DatasetPreparation:
    state: PreparationState
    plan: Optional[DatasetPlan]
    issues: Tuple[DatasetIssue, ...]

    def __post_init__(self) -> None:
        issues = tuple(sorted(self.issues, key=lambda item: (item.code, item.asset_ids, item.paths)))
        object.__setattr__(self, "issues", issues)
        if self.state is PreparationState.COMPLETE:
            if self.plan is None or issues:
                raise ValueError("complete dataset preparation requires a plan and no issues")
        elif self.plan is not None or not issues:
            raise ValueError("incomplete dataset preparation requires issues and no executable plan")

    @property
    def complete(self) -> bool:
        return self.state is PreparationState.COMPLETE


@dataclass(frozen=True)
class PlanValidation:
    valid: bool
    issues: Tuple[DatasetIssue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(self.issues, key=lambda item: (item.code, item.asset_ids, item.paths))),
        )
        if self.valid == bool(self.issues):
            raise ValueError("plan validation state and issues disagree")


@dataclass(frozen=True)
class ExportReceipt:
    destination: str
    format: str
    size: int
    sha256: str
    written: bool
    dry_run: bool


@dataclass(frozen=True)
class _InspectedFile:
    proof: DatasetFileProof
    content: Optional[bytes]


class DatasetSafetyError(RuntimeError):
    def __init__(self, code: str, message: str, path: Optional[Path] = None):
        self.code = code
        self.path = path
        super().__init__(message)


class FilesystemInspector:
    """Stable, no-follow file inspection used to bind plans to live content."""

    def snapshot(
        self,
        path: Path,
        allowed_roots: Sequence[Path],
        *,
        capture_content: bool,
        maximum_capture_bytes: int,
        maximum_file_bytes: Optional[int] = None,
    ) -> _InspectedFile:
        if not isinstance(capture_content, bool):
            raise ValueError("capture_content must be boolean")
        if (
            isinstance(maximum_capture_bytes, bool)
            or not isinstance(maximum_capture_bytes, int)
            or maximum_capture_bytes <= 0
        ):
            raise ValueError("maximum capture size must be a positive integer")
        if maximum_file_bytes is not None and (
            isinstance(maximum_file_bytes, bool) or not isinstance(maximum_file_bytes, int) or maximum_file_bytes <= 0
        ):
            raise ValueError("maximum file size must be a positive integer")
        last_change = None
        for _attempt in range(3):
            try:
                return self._snapshot_once(
                    path,
                    allowed_roots,
                    capture_content=capture_content,
                    maximum_capture_bytes=maximum_capture_bytes,
                    maximum_file_bytes=maximum_file_bytes,
                )
            except DatasetSafetyError as error:
                if error.code != "source_changed":
                    raise
                last_change = error
        assert last_change is not None
        raise last_change

    def _snapshot_once(
        self,
        path: Path,
        allowed_roots: Sequence[Path],
        *,
        capture_content: bool,
        maximum_capture_bytes: int,
        maximum_file_bytes: Optional[int],
    ) -> _InspectedFile:
        source = _absolute(path)
        root = _containing_root(source, allowed_roots)
        _validate_path_components(source, root, final_must_exist=True)
        before = _lstat_regular(source)
        before_snapshot = core_fs.FileSnapshot.from_path(source, before)
        try:
            identity_before = get_file_identity(source, follow_symlinks=False, stat_result=before)
        except FileIdentityError as error:
            raise DatasetSafetyError("identity_unavailable", str(error), source) from error
        effective_maximum = maximum_file_bytes
        if capture_content:
            effective_maximum = (
                maximum_capture_bytes if effective_maximum is None else min(maximum_capture_bytes, effective_maximum)
            )
        if effective_maximum is not None and before.st_size > effective_maximum:
            raise DatasetSafetyError(
                "sidecar_resource_limit",
                "sidecar exceeds the configured per-file limit",
                source,
            )
        digest = hashlib.sha256()
        captured = bytearray() if capture_content else None
        bytes_read = 0
        try:
            with platform_file_system().open_readonly(source) as handle:
                opened = os.fstat(handle.fileno())
                opened_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=source,
                    stat_result=opened,
                )
                _require_same_snapshot(before_snapshot, opened_snapshot, source)
                while True:
                    chunk = handle.read(READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if effective_maximum is not None and bytes_read > effective_maximum:
                        raise DatasetSafetyError(
                            "sidecar_resource_limit",
                            "sidecar grew beyond the configured per-file limit",
                            source,
                        )
                    digest.update(chunk)
                    if captured is not None:
                        captured.extend(chunk)
                finished = os.fstat(handle.fileno())
                finished_snapshot = core_fs.FileSnapshot.from_file(
                    handle,
                    path=source,
                    stat_result=finished,
                )
                _require_same_snapshot(opened_snapshot, finished_snapshot, source)
        except OSError as error:
            raise DatasetSafetyError("read_error", str(error), source) from error
        after = _lstat_regular(source)
        after_snapshot = core_fs.FileSnapshot.from_path(source, after)
        _require_same_snapshot(before_snapshot, after_snapshot, source)
        try:
            identity_after = get_file_identity(source, follow_symlinks=False, stat_result=after)
        except FileIdentityError as error:
            raise DatasetSafetyError("identity_unavailable", str(error), source) from error
        if identity_before.comparison_key != identity_after.comparison_key:
            raise DatasetSafetyError("source_changed", "physical identity changed while reading", source)
        resolved = source.resolve(strict=True)
        _validate_user_dataset_path(resolved, "resolved dataset source")
        if not _is_within(resolved, root):
            raise DatasetSafetyError("path_escape", "resolved path escaped its allowed root", source)
        proof = _proof_from(source, resolved, after, identity_after, digest.hexdigest())
        return _InspectedFile(proof, bytes(captured) if captured is not None else None)

    def byte_equal(
        self,
        first: DatasetFileProof,
        second: DatasetFileProof,
        allowed_roots: Sequence[Path],
    ) -> bool:
        if first.size != second.size or first.digest_hex != second.digest_hex:
            return False
        first_path = Path(first.path)
        second_path = Path(second.path)
        self._validate_live_proof(first, allowed_roots)
        self._validate_live_proof(second, allowed_roots)
        try:
            file_system = platform_file_system()
            with file_system.open_readonly(first_path) as first_handle, file_system.open_readonly(
                second_path
            ) as second_handle:
                first_opened = os.fstat(first_handle.fileno())
                second_opened = os.fstat(second_handle.fileno())
                first_snapshot = core_fs.FileSnapshot.from_file(
                    first_handle,
                    path=first_path,
                    stat_result=first_opened,
                )
                second_snapshot = core_fs.FileSnapshot.from_file(
                    second_handle,
                    path=second_path,
                    stat_result=second_opened,
                )
                _require_snapshot_matches_proof(first_snapshot, first)
                _require_snapshot_matches_proof(second_snapshot, second)
                while True:
                    first_chunk = first_handle.read(READ_CHUNK_SIZE)
                    second_chunk = second_handle.read(READ_CHUNK_SIZE)
                    if first_chunk != second_chunk:
                        return False
                    if not first_chunk:
                        break
                _require_same_snapshot(
                    first_snapshot,
                    core_fs.FileSnapshot.from_file(first_handle, path=first_path),
                    first_path,
                )
                _require_same_snapshot(
                    second_snapshot,
                    core_fs.FileSnapshot.from_file(second_handle, path=second_path),
                    second_path,
                )
        except OSError as error:
            raise DatasetSafetyError("read_error", str(error)) from error
        self._validate_live_proof(first, allowed_roots)
        self._validate_live_proof(second, allowed_roots)
        return True

    def validate_proof(
        self,
        proof: DatasetFileProof,
        allowed_roots: Sequence[Path],
    ) -> None:
        """Revalidate identity and metadata without mutating the source."""

        self._validate_live_proof(proof, allowed_roots)

    def _validate_live_proof(
        self,
        proof: DatasetFileProof,
        allowed_roots: Sequence[Path],
    ) -> None:
        path = Path(proof.path)
        root = _containing_root(path, allowed_roots)
        _validate_path_components(path, root, final_must_exist=True)
        current = _lstat_regular(path)
        _require_snapshot_matches_proof(
            core_fs.FileSnapshot.from_path(path, current),
            proof,
        )
        try:
            identity = get_file_identity(path, follow_symlinks=False, stat_result=current)
        except FileIdentityError as error:
            raise DatasetSafetyError("identity_unavailable", str(error), path) from error
        if _identity_parts(identity) != (
            proof.identity_namespace,
            proof.identity_capability,
            proof.volume_id,
            proof.file_id,
        ):
            raise DatasetSafetyError("source_changed", "physical identity no longer matches plan", path)


@dataclass(frozen=True)
class _KeeperView:
    asset_id: str
    path: str
    name: str
    extension: str
    dimensions: Optional[Tuple[int, int]]
    bit_depth: float
    metadata_count: float
    jpeg_artifact_score: float
    is_ref: bool
    comparison_pool: str
    size: int


class DatasetModeService:
    def __init__(
        self,
        *,
        inspector: Optional[FilesystemInspector] = None,
        maximum_sidecar_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if (
            isinstance(maximum_sidecar_bytes, bool)
            or not isinstance(maximum_sidecar_bytes, int)
            or maximum_sidecar_bytes <= 0
        ):
            raise ValueError("maximum sidecar size must be positive")
        self.inspector = inspector or FilesystemInspector()
        self.maximum_sidecar_bytes = maximum_sidecar_bytes

    def prepare(
        self,
        assets: Iterable[DatasetAsset],
        clusters: Iterable[DatasetCluster],
        *,
        allowed_roots: Iterable[str | Path],
        destination_root: str | Path,
        sidecar_paths: Optional[Iterable[str | Path]] = None,
        sidecar_policy: Optional[SidecarPolicy] = None,
        split_weights: Mapping[str, float] = (
            ("train", 0.8),
            ("validation", 0.1),
            ("test", 0.1),
        ),
        split_seed: str = "",
        previous_split: Optional[SplitManifest] = None,
        dry_run: bool = True,
    ) -> DatasetPreparation:
        asset_tuple = tuple(sorted(assets, key=lambda item: item.asset_id))
        cluster_tuple = tuple(sorted(clusters, key=lambda item: item.cluster_id))
        if not asset_tuple or len({asset.asset_id for asset in asset_tuple}) != len(asset_tuple):
            raise ValueError("dataset assets must contain unique IDs")
        mutable_asset_count = sum(not asset.immutable for asset in asset_tuple)
        if mutable_asset_count > MAX_DATASET_PLAN_ACTIONS:
            return _preparation_error(
                PreparationState.FAILED,
                DatasetSafetyError(
                    "plan_resource_limit",
                    "dataset preparation would require {} actions; maximum is {}".format(
                        mutable_asset_count,
                        MAX_DATASET_PLAN_ACTIONS,
                    ),
                ),
            )
        if mutable_asset_count > MAX_DATASET_PLAN_FILE_RECORDS:
            return _preparation_error(
                PreparationState.FAILED,
                DatasetSafetyError(
                    "plan_resource_limit",
                    "dataset preparation would require at least {} file records; maximum is {}".format(
                        mutable_asset_count,
                        MAX_DATASET_PLAN_FILE_RECORDS,
                    ),
                ),
            )
        if not isinstance(split_seed, str) or "\0" in split_seed:
            raise ValueError("dataset split seed must be a string without NUL")
        if not isinstance(dry_run, bool):
            raise ValueError("dataset dry_run state must be boolean")
        normalized_split_weights = dict(split_weights)
        for split_name in normalized_split_weights:
            if is_unsafe_path_component(split_name) or is_reserved_internal_directory(split_name):
                raise ValueError("dataset split names must be safe single path components")
        try:
            normalized_roots = _normalize_roots(allowed_roots)
        except DatasetSafetyError as error:
            return _preparation_error(PreparationState.FAILED, error)
        destination = _absolute(destination_root)
        try:
            _validate_user_dataset_path(destination, "destination root")
            _validate_directory(destination)
            if any(_is_within(destination, root) or _is_within(root, destination) for root in normalized_roots):
                raise DatasetSafetyError(
                    "destination_overlap",
                    "destination root must be physically separate from all input roots",
                    destination,
                )
        except DatasetSafetyError as error:
            return _preparation_error(PreparationState.FAILED, error)

        asset_by_id = {asset.asset_id: asset for asset in asset_tuple}
        primary_paths = {asset.asset_id: _absolute(asset.path) for asset in asset_tuple}
        if len({_path_key(path) for path in primary_paths.values()}) != len(primary_paths):
            raise ValueError("dataset primary paths must be unique")
        for asset_id, path in primary_paths.items():
            try:
                _validate_user_dataset_path(path, "dataset primary")
                _containing_root(path, normalized_roots)
            except DatasetSafetyError as error:
                return _preparation_error(
                    PreparationState.FAILED,
                    error,
                    asset_ids=(asset_id,),
                )

        relation_by_cluster, member_to_cluster, cluster_error = _validate_clusters(
            asset_by_id,
            cluster_tuple,
        )
        if cluster_error is not None:
            return DatasetPreparation(
                PreparationState.INCOMPLETE_EVIDENCE,
                None,
                (cluster_error,),
            )

        policy = sidecar_policy or SidecarPolicy(case_sensitive=os.name != "nt")
        discovered_paths = []
        if sidecar_paths is None:
            coverage_issues = []
            for root in normalized_roots:
                for event in walk_no_follow(
                    root,
                    allowed_root=root,
                    cross_mounts=False,
                    directory_pruner=lambda path: (
                        path != root and is_reserved_internal_directory(path) and "dupeGuru Neo internal directory"
                    ),
                ):
                    if event.kind is WalkEventKind.FILE and event.path.suffix.lower() in policy.extensions:
                        discovered_paths.append(event.path)
                    elif event.kind in {
                        WalkEventKind.SYMLINK_SKIPPED,
                        WalkEventKind.REPARSE_POINT_SKIPPED,
                        WalkEventKind.MOUNT_SKIPPED,
                        WalkEventKind.CYCLE_SKIPPED,
                        WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
                        WalkEventKind.SPECIAL_FILE_SKIPPED,
                        WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
                        WalkEventKind.ERROR,
                    }:
                        coverage_issues.append(
                            DatasetIssue(
                                "coverage_{}".format(event.kind.value.replace("-", "_")),
                                event.detail or (event.error.message if event.error is not None else event.kind.value),
                                (str(event.path),),
                            )
                        )
            if coverage_issues:
                return DatasetPreparation(
                    PreparationState.INCOMPLETE_COVERAGE,
                    None,
                    tuple(coverage_issues),
                )
        else:
            discovered_paths = [_absolute(path) for path in sidecar_paths]

        try:
            for path in tuple(primary_paths.values()) + tuple(discovered_paths):
                _validate_user_dataset_path(path, "dataset source")
                root = _containing_root(path, normalized_roots)
                _validate_path_components(path, root, final_must_exist=True)
                _lstat_regular(path)
        except DatasetSafetyError as error:
            return _preparation_error(PreparationState.FAILED, error)

        catalog = build_asset_bundles(primary_paths, discovered_paths, policy)
        catalog_issues = tuple(_sidecar_issue(item) for item in catalog.issues)
        exact_groups = [cluster.members for cluster in cluster_tuple if cluster.relation.quarantine_eligible]
        conflict_issues = tuple(_sidecar_issue(item) for item in audit_sidecar_conflicts(catalog, exact_groups))
        if catalog_issues or conflict_issues:
            return DatasetPreparation(
                PreparationState.CONFLICT,
                None,
                catalog_issues + conflict_issues,
            )

        bundle_by_id = catalog.by_id()
        try:
            sidecars_by_asset = _index_sidecars_by_asset(bundle_by_id)
        except DatasetSafetyError as error:
            return _preparation_error(
                PreparationState.CONFLICT,
                error,
            )
        inspected: Dict[str, _InspectedFile] = {}
        try:
            for bundle in catalog.bundles:
                inspected[bundle.primary_path] = self.inspector.snapshot(
                    Path(bundle.primary_path),
                    normalized_roots,
                    capture_content=False,
                    maximum_capture_bytes=self.maximum_sidecar_bytes,
                )
                for sidecar in bundle.sidecars:
                    capture_content = sidecar.slot == ".json"
                    result = self.inspector.snapshot(
                        Path(sidecar.path),
                        normalized_roots,
                        capture_content=capture_content,
                        maximum_capture_bytes=self.maximum_sidecar_bytes,
                        maximum_file_bytes=self.maximum_sidecar_bytes,
                    )
                    if sidecar.read_status is not SidecarReadStatus.OK:
                        raise DatasetSafetyError(
                            "invalid_sidecar",
                            sidecar.error or sidecar.read_status.value,
                            Path(sidecar.path),
                        )
                    if result.proof.digest_hex != sidecar.digest_hex:
                        raise DatasetSafetyError(
                            "source_changed",
                            "sidecar changed between association and stable snapshot",
                            Path(sidecar.path),
                        )
                    if sidecar.slot == ".json":
                        _validate_json_sidecar(result)
                    # Only the immutable proof is needed after optional JSON
                    # validation. Retaining sidecar payloads would make memory
                    # proportional to the cumulative dataset caption size.
                    inspected[sidecar.path] = _InspectedFile(result.proof, None)
        except DatasetSafetyError as error:
            state = PreparationState.SOURCE_CHANGED if error.code == "source_changed" else PreparationState.FAILED
            return _preparation_error(state, error)

        try:
            for result in inspected.values():
                self.inspector.validate_proof(result.proof, normalized_roots)
        except DatasetSafetyError as error:
            return _preparation_error(PreparationState.SOURCE_CHANGED, error)

        identity_paths: Dict[Tuple[str, str, int, str], str] = {}
        for result in inspected.values():
            previous_path = identity_paths.get(result.proof.identity_key)
            if previous_path is not None:
                return DatasetPreparation(
                    PreparationState.CONFLICT,
                    None,
                    (
                        DatasetIssue(
                            "same_physical_file",
                            "two dataset entries resolve to the same physical file",
                            (previous_path, result.proof.path),
                        ),
                    ),
                )
            identity_paths[result.proof.identity_key] = result.proof.path

        exact_issue = self._verify_exact_clusters(
            cluster_tuple,
            bundle_by_id,
            sidecars_by_asset,
            inspected,
            normalized_roots,
        )
        if exact_issue is not None:
            return DatasetPreparation(
                PreparationState.INCOMPLETE_EVIDENCE,
                None,
                (exact_issue,),
            )

        units = _cluster_units(asset_by_id, cluster_tuple, member_to_cluster)
        manifest = build_stable_split_manifest(
            units,
            normalized_split_weights,
            seed=split_seed,
            previous=previous_split,
        )
        split_by_member = manifest.member_splits()
        keeper_records, keeper_by_cluster = _select_keepers(
            units,
            asset_by_id,
            inspected,
        )
        identity_document = {
            "schema": DATASET_PLAN_SCHEMA,
            "schema_version": DATASET_PLAN_SCHEMA_VERSION,
            "allowed_roots": [str(root) for root in normalized_roots],
            "destination_root": str(destination),
            "split_manifest": manifest.to_dict(),
            "keepers": [keeper.to_dict() for keeper in keeper_records],
            "actions": [],
            "dry_run": dry_run,
            "executor_contract": EXECUTOR_CONTRACT,
        }
        try:
            initial_document_bytes = _canonical_json_byte_size(
                identity_document,
                maximum_bytes=MAX_DATASET_PLAN_DOCUMENT_BYTES,
            )
            _enforce_plan_document_budget(initial_document_bytes, 0)
        except ValueError as error:
            return _preparation_error(
                PreparationState.FAILED,
                DatasetSafetyError("plan_resource_limit", str(error)),
            )
        try:
            actions = _build_actions(
                units,
                relation_by_cluster,
                keeper_by_cluster,
                split_by_member,
                destination,
                asset_by_id,
                bundle_by_id,
                sidecars_by_asset,
                inspected,
                initial_document_bytes=initial_document_bytes,
            )
        except DatasetSafetyError as error:
            state = PreparationState.FAILED if error.code == "plan_resource_limit" else PreparationState.CONFLICT
            return _preparation_error(state, error)
        identity_document["actions"] = [action.to_dict() for action in actions]
        plan = DatasetPlan(
            plan_id=_content_id(identity_document),
            allowed_roots=tuple(str(root) for root in normalized_roots),
            destination_root=str(destination),
            split_manifest=manifest,
            keepers=keeper_records,
            actions=actions,
            dry_run=dry_run,
        )
        return DatasetPreparation(PreparationState.COMPLETE, plan, ())

    def _verify_exact_clusters(
        self,
        clusters: Sequence[DatasetCluster],
        bundles: Mapping[str, AssetBundle],
        sidecars_by_asset: Mapping[str, Mapping[str, SidecarAsset]],
        inspected: Mapping[str, _InspectedFile],
        roots: Sequence[Path],
    ) -> Optional[DatasetIssue]:
        for cluster in clusters:
            if not cluster.relation.quarantine_eligible:
                continue
            keeper_id = cluster.members[0]
            keeper_bundle = bundles[keeper_id]
            keeper_sidecars = sidecars_by_asset[keeper_id]
            keeper_primary = inspected[keeper_bundle.primary_path].proof
            for member in cluster.members[1:]:
                target_bundle = bundles[member]
                target_primary = inspected[target_bundle.primary_path].proof
                try:
                    if not self.inspector.byte_equal(target_primary, keeper_primary, roots):
                        return DatasetIssue(
                            "exact_evidence_mismatch",
                            "verified-exact cluster members are not byte-identical",
                            (target_primary.path, keeper_primary.path),
                            (member, keeper_id),
                        )
                    for target_sidecar in target_bundle.sidecars:
                        try:
                            keeper_sidecar = keeper_sidecars[target_sidecar.slot]
                        except KeyError:
                            return DatasetIssue(
                                "sidecar_cluster_presence_mismatch",
                                "keeper is missing a sidecar slot present on another exact-cluster member",
                                (target_sidecar.path,),
                                (member, keeper_id),
                            )
                        if not self.inspector.byte_equal(
                            inspected[target_sidecar.path].proof,
                            inspected[keeper_sidecar.path].proof,
                            roots,
                        ):
                            return DatasetIssue(
                                "sidecar_exact_evidence_mismatch",
                                "sidecars in an exact cluster are not byte-identical",
                                (target_sidecar.path, keeper_sidecar.path),
                                (member, keeper_id),
                            )
                except DatasetSafetyError as error:
                    return DatasetIssue(
                        error.code,
                        str(error),
                        (str(error.path),) if error.path is not None else (),
                        (member, keeper_id),
                    )
        return None

    def revalidate(self, plan: DatasetPlan) -> PlanValidation:
        roots = tuple(Path(root) for root in plan.allowed_roots)
        destination_root = Path(plan.destination_root)
        issues = []
        try:
            for root in roots:
                _validate_user_dataset_path(root, "allowed root")
            _validate_user_dataset_path(destination_root, "destination root")
            for split_name in plan.split_manifest.cluster_splits().values():
                _validate_dataset_split_name(split_name)
            for action in plan.actions:
                _validate_dataset_split_name(action.split)
                for file_action in action.files:
                    _validate_user_dataset_path(file_action.source.path, "dataset source")
                    _validate_user_dataset_path(file_action.source.resolved_path, "resolved dataset source")
                    if file_action.reference is not None:
                        _validate_user_dataset_path(file_action.reference.path, "dataset keeper reference")
                        _validate_user_dataset_path(
                            file_action.reference.resolved_path,
                            "resolved dataset keeper reference",
                        )
                    if file_action.destination is not None:
                        _validate_user_dataset_path(file_action.destination, "dataset destination")
        except DatasetSafetyError as error:
            return PlanValidation(
                False,
                (
                    DatasetIssue(
                        error.code,
                        str(error),
                        (str(error.path),) if error.path is not None else (),
                    ),
                ),
            )
        expected: Dict[str, DatasetFileProof] = {}
        for action in plan.actions:
            for file_action in action.files:
                expected[file_action.source.path] = file_action.source
                if file_action.reference is not None:
                    expected[file_action.reference.path] = file_action.reference
                if file_action.destination is not None:
                    try:
                        _validate_planned_destination(Path(file_action.destination), destination_root)
                        if os.path.lexists(file_action.destination):
                            issues.append(
                                DatasetIssue(
                                    "destination_conflict",
                                    "planned destination now exists",
                                    (file_action.destination,),
                                    (action.asset_id,),
                                )
                            )
                    except DatasetSafetyError as error:
                        issues.append(
                            DatasetIssue(
                                error.code,
                                str(error),
                                (str(error.path),) if error.path is not None else (file_action.destination,),
                                (action.asset_id,),
                            )
                        )
        for path, proof in sorted(expected.items()):
            try:
                current = self.inspector.snapshot(
                    Path(path),
                    roots,
                    capture_content=False,
                    maximum_capture_bytes=self.maximum_sidecar_bytes,
                ).proof
                if current != proof:
                    issues.append(
                        DatasetIssue(
                            "source_changed",
                            "source proof no longer matches the immutable plan",
                            (path,),
                        )
                    )
            except DatasetSafetyError as error:
                issues.append(
                    DatasetIssue(
                        error.code,
                        str(error),
                        (str(error.path),) if error.path is not None else (path,),
                    )
                )
        return PlanValidation(not issues, tuple(issues))


def _validate_clusters(
    assets: Mapping[str, DatasetAsset],
    clusters: Sequence[DatasetCluster],
) -> Tuple[Dict[str, DatasetRelation], Dict[str, str], Optional[DatasetIssue]]:
    relation_by_cluster: Dict[str, DatasetRelation] = {}
    member_to_cluster: Dict[str, str] = {}
    for cluster in clusters:
        if not cluster.evidence_complete:
            return (
                {},
                {},
                DatasetIssue(
                    "incomplete_cluster_evidence",
                    "cluster evidence is incomplete",
                    asset_ids=cluster.members,
                ),
            )
        unknown = tuple(member for member in cluster.members if member not in assets)
        if unknown:
            return (
                {},
                {},
                DatasetIssue(
                    "unknown_cluster_member",
                    "cluster references an unknown dataset asset",
                    asset_ids=unknown,
                ),
            )
        cluster_id = cluster.cluster_id
        relation_by_cluster[cluster_id] = cluster.relation
        for member in cluster.members:
            if member in member_to_cluster:
                return (
                    {},
                    {},
                    DatasetIssue(
                        "overlapping_clusters",
                        "an asset occurs in more than one leakage cluster",
                        asset_ids=(member,),
                    ),
                )
            member_to_cluster[member] = cluster_id
    return relation_by_cluster, member_to_cluster, None


def _cluster_units(
    assets: Mapping[str, DatasetAsset],
    clusters: Sequence[DatasetCluster],
    member_to_cluster: Mapping[str, str],
) -> Tuple[ClusterUnit, ...]:
    units = [ClusterUnit(cluster.cluster_id, cluster.members) for cluster in clusters]
    units.extend(ClusterUnit.from_members((asset_id,)) for asset_id in assets if asset_id not in member_to_cluster)
    return tuple(sorted(units, key=lambda item: item.cluster_id))


def _select_keepers(
    units: Sequence[ClusterUnit],
    assets: Mapping[str, DatasetAsset],
    inspected: Mapping[str, _InspectedFile],
) -> Tuple[Tuple[KeeperRecord, ...], Dict[str, str]]:
    records = []
    keeper_by_cluster = {}
    for unit in units:
        views = []
        for asset_id in unit.members:
            asset = assets[asset_id]
            path = _absolute(asset.path)
            proof = inspected[str(path)].proof
            views.append(
                _KeeperView(
                    asset_id=asset_id,
                    path=str(path),
                    name=path.name,
                    extension=path.suffix,
                    dimensions=asset.dimensions,
                    bit_depth=asset.bit_depth,
                    metadata_count=asset.metadata_count,
                    jpeg_artifact_score=asset.jpeg_artifact_score,
                    is_ref=asset.protected,
                    comparison_pool=("protected" if asset.protected else "incoming"),
                    size=proof.size,
                )
            )
        decision: KeeperDecision = choose_keeper(views)
        keeper_id = decision.keeper.asset_id
        keeper_by_cluster[unit.cluster_id] = keeper_id
        records.append(
            KeeperRecord(
                cluster_id=unit.cluster_id,
                keeper_id=keeper_id,
                explanations=tuple((view.asset_id, decision.explanation(view)) for view in views),
                scores=tuple((candidate.file.asset_id, candidate.score) for candidate in decision.candidates),
                reasons=tuple(
                    (
                        candidate.file.asset_id,
                        tuple(
                            KeeperReasonRecord(reason.code, reason.points, reason.message)
                            for reason in candidate.reasons
                        )
                        + (
                            (
                                KeeperReasonRecord(
                                    IMMUTABLE_PROTECTED_REASON,
                                    0,
                                    "protected library is reference-only",
                                ),
                            )
                            if assets[candidate.file.asset_id].immutable
                            else ()
                        ),
                    )
                    for candidate in decision.candidates
                ),
            )
        )
    return tuple(records), keeper_by_cluster


def _index_sidecars_by_asset(
    bundles: Mapping[str, AssetBundle],
) -> Dict[str, Dict[str, SidecarAsset]]:
    """Build the unique sidecar-slot index once for the complete plan."""

    result: Dict[str, Dict[str, SidecarAsset]] = {}
    for asset_id, bundle in bundles.items():
        by_slot: Dict[str, SidecarAsset] = {}
        for sidecar in bundle.sidecars:
            previous = by_slot.get(sidecar.slot)
            if previous is not None:
                raise DatasetSafetyError(
                    "duplicate_sidecar_slot",
                    ("asset '{}' has more than one sidecar for slot '{}': " "'{}' and '{}'").format(
                        asset_id,
                        sidecar.slot,
                        previous.path,
                        sidecar.path,
                    ),
                    Path(sidecar.path),
                )
            by_slot[sidecar.slot] = sidecar
        result[asset_id] = by_slot
    return result


def _build_actions(
    units: Sequence[ClusterUnit],
    relation_by_cluster: Mapping[str, DatasetRelation],
    keeper_by_cluster: Mapping[str, str],
    split_by_member: Mapping[str, str],
    destination_root: Path,
    assets: Mapping[str, DatasetAsset],
    bundles: Mapping[str, AssetBundle],
    sidecars_by_asset: Mapping[str, Mapping[str, SidecarAsset]],
    inspected: Mapping[str, _InspectedFile],
    *,
    initial_document_bytes: int,
) -> Tuple[DatasetBundleAction, ...]:
    actions = []
    destinations = set()
    source_paths = {_path_key(result.proof.path) for result in inspected.values()}
    file_record_count = 0
    serialized_file_bytes = 0
    serialized_action_bytes = 0
    for unit in units:
        relation = relation_by_cluster.get(unit.cluster_id)
        keeper_id = keeper_by_cluster[unit.cluster_id]
        keeper_bundle = bundles[keeper_id]
        keeper_sidecars = sidecars_by_asset[keeper_id]
        for asset_id in unit.members:
            if assets[asset_id].immutable:
                continue
            if len(actions) >= MAX_DATASET_PLAN_ACTIONS:
                raise DatasetSafetyError(
                    "plan_resource_limit",
                    "dataset plan exceeds the {}-action limit".format(
                        MAX_DATASET_PLAN_ACTIONS,
                    ),
                )
            bundle = bundles[asset_id]
            split = split_by_member[asset_id]
            quarantine = relation is not None and relation.quarantine_eligible and asset_id != keeper_id
            file_actions = []
            source_items = [
                ("primary", "", bundle.primary_path),
                *(("sidecar", sidecar.slot, sidecar.path) for sidecar in bundle.sidecars),
            ]
            for role, slot, path in source_items:
                file_record_count += 1
                if file_record_count > MAX_DATASET_PLAN_FILE_RECORDS:
                    raise DatasetSafetyError(
                        "plan_resource_limit",
                        "dataset plan exceeds the {} file-record limit".format(
                            MAX_DATASET_PLAN_FILE_RECORDS,
                        ),
                    )
                source = inspected[path].proof
                if quarantine:
                    if role == "primary":
                        reference_path = keeper_bundle.primary_path
                    else:
                        try:
                            reference_path = keeper_sidecars[slot].path
                        except KeyError as error:
                            raise DatasetSafetyError(
                                "missing_keeper_sidecar",
                                ("keeper '{}' has no sidecar for slot '{}'").format(
                                    keeper_id,
                                    slot,
                                ),
                                Path(path),
                            ) from error
                    destination = None
                    reference = inspected[reference_path].proof
                else:
                    destination_path = destination_root.joinpath(split, Path(path).name)
                    _validate_planned_destination(destination_path, destination_root)
                    key = _path_key(destination_path)
                    if key in destinations or key in source_paths:
                        raise DatasetSafetyError(
                            "rename_collision",
                            "two files would use the same destination or overwrite a source",
                            destination_path,
                        )
                    if os.path.lexists(destination_path):
                        raise DatasetSafetyError(
                            "destination_conflict",
                            "planned destination already exists",
                            destination_path,
                        )
                    destinations.add(key)
                    destination = str(destination_path)
                    reference = None
                file_action = DatasetFileAction(
                    source=source,
                    destination=destination,
                    reference=reference,
                    role=role,
                    sidecar_slot=slot,
                )
                try:
                    serialized_file_bytes += _canonical_json_byte_size(
                        file_action.to_dict(),
                        maximum_bytes=MAX_DATASET_PLAN_DOCUMENT_BYTES,
                    )
                    _enforce_plan_document_budget(
                        initial_document_bytes,
                        serialized_file_bytes,
                    )
                except ValueError as error:
                    raise DatasetSafetyError(
                        "plan_resource_limit",
                        str(error),
                    ) from error
                file_actions.append(file_action)
            operation = DatasetOperation.QUARANTINE_BUNDLE if quarantine else DatasetOperation.MOVE_BUNDLE
            action_document = {
                "asset_id": asset_id,
                "cluster_id": unit.cluster_id,
                "split": split,
                "operation": operation.value,
                "files": [item.to_dict() for item in file_actions],
                "keeper_id": keeper_id if quarantine else None,
                "atomic": True,
            }
            action = DatasetBundleAction(
                action_id=_content_id(action_document),
                asset_id=asset_id,
                cluster_id=unit.cluster_id,
                split=split,
                operation=operation,
                files=tuple(file_actions),
                keeper_id=keeper_id if quarantine else None,
            )
            try:
                serialized_action_bytes += (1 if actions else 0) + _canonical_json_byte_size(
                    action.to_dict(),
                    maximum_bytes=MAX_DATASET_PLAN_DOCUMENT_BYTES,
                )
                _enforce_plan_document_budget(
                    initial_document_bytes,
                    serialized_action_bytes,
                )
            except ValueError as error:
                raise DatasetSafetyError(
                    "plan_resource_limit",
                    str(error),
                ) from error
            actions.append(action)
    return tuple(sorted(actions, key=lambda item: item.action_id))


def export_plan_json(
    plan: DatasetPlan,
    destination: str | Path,
    *,
    allowed_output_root: str | Path,
    dry_run: bool = False,
    maximum_bytes: Optional[int] = None,
) -> ExportReceipt:
    _validate_export_plan_limits(plan)
    return _export(
        _iter_plan_json_bytes(plan),
        destination,
        allowed_output_root,
        "json",
        dry_run,
        maximum_bytes=(MAX_DATASET_EXPORT_BYTES if maximum_bytes is None else maximum_bytes),
    )


def export_plan_csv(
    plan: DatasetPlan,
    destination: str | Path,
    *,
    allowed_output_root: str | Path,
    dry_run: bool = False,
    maximum_bytes: Optional[int] = None,
    spreadsheet_safe: bool = False,
) -> ExportReceipt:
    """Export machine CSV, or an explicitly requested spreadsheet-safe view.

    The default preserves every machine value byte-for-byte after CSV decoding.
    Callers that intend to open untrusted data in spreadsheet software must
    explicitly request ``spreadsheet_safe=True``; that display-oriented form
    prefixes formula-like string cells with an apostrophe and is not suitable
    for exact round trips.
    """

    _validate_export_plan_limits(plan)
    fieldnames = (
        "plan_id",
        "action_id",
        "asset_id",
        "cluster_id",
        "split",
        "operation",
        "atomic",
        "role",
        "sidecar_slot",
        "source_path",
        "destination_path",
        "reference_path",
        "size",
        "mtime_ns",
        "digest_algorithm",
        "digest_hex",
    )
    return _export(
        _iter_plan_csv_bytes(
            plan,
            fieldnames,
            spreadsheet_safe=spreadsheet_safe,
        ),
        destination,
        allowed_output_root,
        "csv-spreadsheet-safe" if spreadsheet_safe else "csv",
        dry_run,
        maximum_bytes=(MAX_DATASET_EXPORT_BYTES if maximum_bytes is None else maximum_bytes),
    )


def _export(
    chunks: Iterable[bytes],
    destination: str | Path,
    allowed_output_root: str | Path,
    format_name: str,
    dry_run: bool,
    *,
    maximum_bytes: int,
) -> ExportReceipt:
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int) or maximum_bytes <= 0:
        raise ValueError("maximum export size must be a positive integer")
    destination_path = _absolute(destination)
    root = _absolute(allowed_output_root)
    _validate_directory(root)
    if not _is_within(destination_path, root):
        raise DatasetSafetyError("export_escape", "export path is outside its allowed root", destination_path)
    _validate_path_components(destination_path.parent, root, final_must_exist=True)
    if os.path.lexists(destination_path):
        raise FileExistsError("export destination already exists: {}".format(destination_path))
    if dry_run:
        size, digest = _consume_export_chunks(
            chunks,
            maximum_bytes=maximum_bytes,
        )
        return ExportReceipt(
            str(destination_path),
            format_name,
            size,
            digest,
            False,
            True,
        )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".dupeguru-dataset-",
        suffix=".tmp",
        dir=str(destination_path.parent),
    )
    temporary_path = Path(temporary_name)
    created = os.fstat(file_descriptor)
    created_identity = (int(created.st_dev), int(created.st_ino))
    file_system = platform_file_system()
    try:
        digest_state = hashlib.sha256()
        size = 0
        with os.fdopen(file_descriptor, "wb") as handle:
            for chunk in chunks:
                size = _validate_export_chunk(
                    chunk,
                    size=size,
                    maximum_bytes=maximum_bytes,
                )
                digest_state.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = handle.write(remaining)
                    if written is None or written <= 0:
                        raise OSError("short write while exporting dataset plan")
                    remaining = remaining[written:]
            handle.flush()
            os.fsync(handle.fileno())
        file_system.rename_no_replace(temporary_path, destination_path)
        file_system.fsync_directory(destination_path.parent)
    finally:
        cleanup_created_regular_file(
            temporary_path,
            created_identity,
            file_system,
        )
    return ExportReceipt(
        str(destination_path),
        format_name,
        size,
        digest_state.hexdigest(),
        True,
        False,
    )


def _validate_export_plan_limits(plan: DatasetPlan) -> None:
    if len(plan.actions) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetSafetyError(
            "export_record_limit",
            "dataset plan exceeds the {}-action export limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    file_records = sum(len(action.files) for action in plan.actions)
    if file_records > MAX_DATASET_PLAN_FILE_RECORDS:
        raise DatasetSafetyError(
            "export_record_limit",
            "dataset plan exceeds the {} file-record export limit".format(
                MAX_DATASET_PLAN_FILE_RECORDS,
            ),
        )


def _iter_plan_json_bytes(plan: DatasetPlan) -> Iterator[bytes]:
    for chunk in _iter_canonical_json(plan.to_dict()):
        yield chunk.encode("utf-8")
    yield b"\n"


def _iter_plan_csv_bytes(
    plan: DatasetPlan,
    fieldnames: Sequence[str],
    *,
    spreadsheet_safe: bool,
) -> Iterator[bytes]:
    yield _csv_row_bytes(fieldnames)
    for action in plan.actions:
        for item in action.files:
            row = {
                "plan_id": plan.plan_id,
                "action_id": action.action_id,
                "asset_id": action.asset_id,
                "cluster_id": action.cluster_id,
                "split": action.split,
                "operation": action.operation.value,
                "atomic": "true",
                "role": item.role,
                "sidecar_slot": item.sidecar_slot,
                "source_path": item.source.path,
                "destination_path": item.destination or "",
                "reference_path": (item.reference.path if item.reference is not None else ""),
                "size": item.source.size,
                "mtime_ns": item.source.mtime_ns,
                "digest_algorithm": item.source.digest_algorithm,
                "digest_hex": item.source.digest_hex,
            }
            values = tuple(row[field] for field in fieldnames)
            if spreadsheet_safe:
                values = tuple(_spreadsheet_safe_cell(value) for value in values)
            yield _csv_row_bytes(values)


def _csv_row_bytes(values: Sequence[object]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(values)
    return output.getvalue().encode("utf-8")


def _spreadsheet_safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _validate_export_chunk(
    chunk: bytes,
    *,
    size: int,
    maximum_bytes: int,
) -> int:
    if not isinstance(chunk, bytes):
        raise TypeError("dataset export chunks must be bytes")
    next_size = size + len(chunk)
    if next_size > maximum_bytes:
        raise DatasetSafetyError(
            "export_too_large",
            "dataset export exceeds the {}-byte limit".format(
                maximum_bytes,
            ),
        )
    return next_size


def _consume_export_chunks(
    chunks: Iterable[bytes],
    *,
    maximum_bytes: int,
) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size = _validate_export_chunk(
            chunk,
            size=size,
            maximum_bytes=maximum_bytes,
        )
        digest.update(chunk)
    return size, digest.hexdigest()


def _normalize_roots(roots: Iterable[str | Path]) -> Tuple[Path, ...]:
    decorated = sorted(
        (
            _root_component_key(path),
            str(path),
            path,
        )
        for path in (_absolute(root) for root in roots)
    )
    if not decorated:
        raise ValueError("dataset mode requires at least one allowed root")

    unique = []
    previous_key: Optional[Tuple[str, ...]] = None
    for component_key, _path_text, path in decorated:
        if component_key == previous_key:
            continue
        unique.append((component_key, path))
        previous_key = component_key

    normalized = tuple(path for _component_key, path in unique)
    for root in normalized:
        _validate_user_dataset_path(root, "allowed root")
        _validate_directory(root)

    # Sorting by normalized path *components* (rather than raw path text)
    # makes every descendant immediately follow an ancestor or another
    # descendant in that ancestor's contiguous prefix range. An adjacent
    # prefix check therefore replaces the former all-pairs containment scan.
    for (previous_components, _previous), (current_components, _current) in zip(
        unique,
        unique[1:],
    ):
        if (
            len(previous_components) < len(current_components)
            and current_components[: len(previous_components)] == previous_components
        ):
            raise ValueError("dataset allowed roots must not overlap")
    return normalized


def _validate_user_dataset_path(path: str | Path, label: str) -> Path:
    """Reject user-controlled paths that overlap dupeGuru Neo private state."""

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
        # The existing path validators report the concrete availability or
        # link error.  This guard must not turn that into a permissive path.
        pass
    for checked in candidates:
        if is_within_reserved_internal_directory(checked) or is_reserved_internal_file(checked):
            raise DatasetSafetyError(
                "reserved_internal_path",
                "{} must not use a dupeGuru Neo internal path".format(label),
                checked,
            )
    return candidate


def _validate_dataset_split_name(split_name: str) -> None:
    if is_unsafe_path_component(split_name) or is_reserved_internal_directory(split_name):
        raise DatasetSafetyError(
            "reserved_internal_path",
            "dataset split must not use a dupeGuru Neo internal directory name",
        )


def _root_component_key(path: Path) -> Tuple[str, ...]:
    normalized = Path(
        os.path.normpath(
            os.path.abspath(os.fspath(path)),
        )
    )
    return tuple(os.path.normcase(part) for part in normalized.parts)


def _validate_directory(path: Path) -> None:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise DatasetSafetyError("directory_unavailable", str(error), path) from error
    if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISDIR(file_stat.st_mode):
        raise DatasetSafetyError("unsafe_directory", "path is not a plain physical directory", path)


def _validate_path_components(
    path: Path,
    root: Path,
    *,
    final_must_exist: bool,
) -> None:
    candidate = _absolute(path)
    root = _absolute(root)
    if not _is_within(candidate, root):
        raise DatasetSafetyError("path_escape", "path is outside its allowed root", candidate)
    _validate_directory(root)
    current = root
    relative_parts = candidate.relative_to(root).parts
    for index, part in enumerate(relative_parts):
        current = current.joinpath(part)
        is_final = index == len(relative_parts) - 1
        if is_final and not final_must_exist and not os.path.lexists(current):
            continue
        try:
            file_stat = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise DatasetSafetyError("path_unavailable", str(error), current) from error
        if stat.S_ISLNK(file_stat.st_mode):
            raise DatasetSafetyError("symlink_escape", "symbolic-link path components are forbidden", current)
        if is_reparse_point(file_stat):
            raise DatasetSafetyError("reparse_escape", "reparse-point path components are forbidden", current)
        if not is_final and not stat.S_ISDIR(file_stat.st_mode):
            raise DatasetSafetyError("unsafe_path_component", "parent path is not a directory", current)


def _validate_planned_destination(path: Path, root: Path) -> None:
    if not _is_within(path, root):
        raise DatasetSafetyError("destination_escape", "planned destination escaped its root", path)
    # The split directory may not exist yet.  Its closest existing parent is still validated here,
    # and the downstream executor must create it without following links.
    existing = path.parent
    while not os.path.lexists(existing):
        if existing == root:
            break
        existing = existing.parent
    _validate_path_components(existing, root, final_must_exist=True)


def _containing_root(path: Path, roots: Sequence[Path]) -> Path:
    matches = [root for root in roots if _is_within(path, root)]
    if not matches:
        raise DatasetSafetyError("path_escape", "path is outside every allowed root", path)
    return sorted(matches, key=lambda item: len(str(item)), reverse=True)[0]


def _lstat_regular(path: Path) -> os.stat_result:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise DatasetSafetyError("source_unavailable", str(error), path) from error
    if stat.S_ISLNK(file_stat.st_mode):
        raise DatasetSafetyError("symlink_escape", "symbolic-link sources are forbidden", path)
    if is_reparse_point(file_stat):
        raise DatasetSafetyError("reparse_escape", "reparse-point sources are forbidden", path)
    if not stat.S_ISREG(file_stat.st_mode):
        raise DatasetSafetyError("unsupported_type", "dataset source is not a regular file", path)
    return file_stat


def _require_same_snapshot(
    first: core_fs.FileSnapshot,
    second: core_fs.FileSnapshot,
    path: Path,
) -> None:
    if not first.same_content_generation(second):
        raise DatasetSafetyError(
            "source_changed",
            "source identity or generation changed while reading",
            path,
        )


def _require_snapshot_matches_proof(
    snapshot: core_fs.FileSnapshot,
    proof: DatasetFileProof,
) -> None:
    if (
        snapshot.device != str(proof.stat_device)
        or snapshot.file_id != str(proof.stat_inode)
        or snapshot.size != proof.size
        or snapshot.mtime_ns != proof.mtime_ns
        or snapshot.ctime_ns.hex() != proof.generation_token
    ):
        raise DatasetSafetyError(
            "source_changed",
            "source identity or generation no longer matches plan",
            Path(proof.path),
        )


def _proof_from(
    path: Path,
    resolved: Path,
    file_stat: os.stat_result,
    identity: FileIdentity,
    digest_hex: str,
) -> DatasetFileProof:
    namespace, capability, volume_id, file_id = _identity_parts(identity)
    return DatasetFileProof(
        path=str(path),
        resolved_path=str(resolved),
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
        generation_token=core_fs.FileSnapshot.from_path(path, file_stat).ctime_ns.hex(),
        digest_algorithm=HASH_ALGORITHM,
        digest_hex=digest_hex,
        identity_namespace=namespace,
        identity_capability=capability,
        volume_id=volume_id,
        file_id=file_id,
        stat_device=file_stat.st_dev,
        stat_inode=file_stat.st_ino,
    )


def _identity_parts(identity: FileIdentity) -> Tuple[str, str, int, str]:
    file_id = identity.file_id.hex() if isinstance(identity.file_id, bytes) else str(identity.file_id)
    return identity.namespace, identity.capability.value, identity.volume_id, file_id


def _validate_json_sidecar(result: _InspectedFile) -> None:
    if result.content is None:
        raise DatasetSafetyError(
            "invalid_json_sidecar",
            "JSON sidecar content was not captured",
            Path(result.proof.path),
        )
    try:
        text = result.content.decode("utf-8")
        _preflight_json_sidecar(text, Path(result.proof.path))
        json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except MemoryError as error:
        raise DatasetSafetyError(
            "sidecar_resource_limit",
            "JSON sidecar exceeded the parser memory budget",
            Path(result.proof.path),
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise DatasetSafetyError("invalid_json_sidecar", str(error), Path(result.proof.path)) from error


def _preflight_json_sidecar(text: str, path: Path) -> None:
    """Bound JSON structure before constructing Python lists and dictionaries."""

    limits = JsonStructuralLimits(
        max_depth=MAX_JSON_SIDECAR_DEPTH,
        max_container_entries=MAX_JSON_SIDECAR_CONTAINER_ITEMS,
        max_total_nodes=MAX_JSON_SIDECAR_NODES,
        max_scalar_tokens=MAX_JSON_SIDECAR_NODES,
        max_total_string_chars=MAX_JSON_SIDECAR_STRING_CHARACTERS,
        max_string_chars=MAX_JSON_SIDECAR_STRING_CHARACTERS,
    )
    try:
        preflight_json_structure(
            text,
            limits=limits,
            label="JSON sidecar",
        )
    except JsonStructureError as error:
        raise DatasetSafetyError(
            "sidecar_resource_limit",
            str(error),
            path,
        ) from error


def _json_object_without_duplicates(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite JSON number: {}".format(value))


def _sidecar_issue(issue: SidecarIssue) -> DatasetIssue:
    return DatasetIssue(
        "sidecar_{}".format(issue.kind.value),
        issue.detail or issue.kind.value.replace("_", " "),
        issue.paths,
        issue.asset_ids,
    )


def _preparation_error(
    state: PreparationState,
    error: DatasetSafetyError,
    *,
    asset_ids: Tuple[str, ...] = (),
) -> DatasetPreparation:
    return DatasetPreparation(
        state,
        None,
        (
            DatasetIssue(
                error.code,
                str(error),
                (str(error.path),) if error.path is not None else (),
                asset_ids,
            ),
        ),
    )


def _content_id(value: Mapping[str, object]) -> str:
    digest = hashlib.sha256()
    for chunk in _iter_canonical_json(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, object]) -> str:
    return "".join(_iter_canonical_json(value))


def _iter_canonical_json(value: Mapping[str, object]) -> Iterator[str]:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    yield from encoder.iterencode(value)


def _canonical_json_byte_size(
    value: Mapping[str, object],
    *,
    maximum_bytes: Optional[int] = None,
) -> int:
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum JSON size must be positive")
    size = 0
    for chunk in _iter_canonical_json(value):
        size += len(chunk.encode("utf-8"))
        if maximum_bytes is not None and size > maximum_bytes:
            raise ValueError(
                "canonical JSON exceeds the {}-byte limit".format(
                    maximum_bytes,
                )
            )
    return size


_PLAN_ID_EXPORT_OVERHEAD = len((',"plan_id":"{}"'.format("0" * 64) + "\n").encode("utf-8"))


def _enforce_plan_document_budget(
    initial_document_bytes: int,
    serialized_action_bytes: int,
) -> None:
    projected_size = initial_document_bytes + serialized_action_bytes + _PLAN_ID_EXPORT_OVERHEAD
    if projected_size > MAX_DATASET_PLAN_DOCUMENT_BYTES:
        raise ValueError(
            "dataset plan JSON would be {} bytes; maximum is {}".format(
                projected_size,
                MAX_DATASET_PLAN_DOCUMENT_BYTES,
            )
        )


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(candidate), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DATASET_PLAN_SCHEMA",
    "DATASET_PLAN_SCHEMA_VERSION",
    "DatasetAsset",
    "DatasetBundleAction",
    "DatasetCluster",
    "DatasetFileAction",
    "DatasetFileProof",
    "DatasetIssue",
    "DatasetModeService",
    "DatasetOperation",
    "DatasetPlan",
    "DatasetPreparation",
    "DatasetRelation",
    "DatasetSafetyError",
    "EXECUTOR_CONTRACT",
    "ExportReceipt",
    "FilesystemInspector",
    "KeeperReasonRecord",
    "KeeperRecord",
    "MAX_DATASET_EXPORT_BYTES",
    "MAX_DATASET_PLAN_ACTIONS",
    "MAX_DATASET_PLAN_DOCUMENT_BYTES",
    "MAX_DATASET_PLAN_FILE_RECORDS",
    "MAX_JSON_SIDECAR_CONTAINER_ITEMS",
    "MAX_JSON_SIDECAR_DEPTH",
    "MAX_JSON_SIDECAR_NODES",
    "MAX_JSON_SIDECAR_STRING_CHARACTERS",
    "PlanValidation",
    "PreparationState",
    "export_plan_csv",
    "export_plan_json",
]
