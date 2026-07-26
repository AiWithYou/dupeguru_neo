from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from core import __version__
from core.video_schema import (  # noqa: F401 - service-model compatibility exports
    VIDEO_LIBRARY_GROUP_SCHEMA,
    VIDEO_LIBRARY_RECORD_SCHEMA,
    VIDEO_LIBRARY_SCAN_SCHEMA,
)

SCHEMA_VERSION = 1
SCAN_REPORT_SCHEMA = "dupeguru.scan-report"
SCAN_RECORD_SCHEMA = "dupeguru.scan-record"
DELETION_PLAN_SCHEMA = "dupeguru.deletion-plan"
PLAN_RECORD_SCHEMA = "dupeguru.plan-record"
APPLY_REPORT_SCHEMA = "dupeguru.apply-report"
QUERY_REPORT_SCHEMA = "dupeguru.query-report"
DOCTOR_REPORT_SCHEMA = "dupeguru.doctor-report"
QUARANTINE_LIST_SCHEMA = "dupeguru.quarantine-list"
QUARANTINE_ACTION_SCHEMA = "dupeguru.quarantine-action"
QUARANTINE_OPERATION_SCHEMA = "dupeguru.quarantine-operation"
VIDEO_CAPABILITIES_SCHEMA = "dupeguru.video-capabilities"
VIDEO_ANALYSIS_SCHEMA = "dupeguru.video-analysis"
VIDEO_COMPARISON_SCHEMA = "dupeguru.video-comparison"

VERIFIED_EXACT = "verified_exact"
DELETION_PROOF_ALGORITHM = "sha256"
APPROXIMATE = "approximate"
RELATED = "related"
UNKNOWN = "unknown"
VERIFICATION_LEVELS = {VERIFIED_EXACT, APPROXIMATE, RELATED, UNKNOWN}

DEFAULT_SCAN_MAX_FILES = 1_000_000
DEFAULT_SCAN_MAX_ISSUES = 100_000
DEFAULT_SCAN_MAX_GROUPS = 250_000
DEFAULT_SCAN_MAX_SECONDS = 4 * 60 * 60


class SchemaError(ValueError):
    """Raised when a service document does not match the supported schema."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError("{} must be an object".format(name))
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SchemaError("{} must be an array".format(name))
    return value


def _required(data: Mapping[str, Any], key: str, expected_type: Any = None) -> Any:
    if key not in data:
        raise SchemaError("Missing required field: {}".format(key))
    value = data[key]
    if expected_type is not None:
        if expected_type is int:
            valid = type(value) is int
        elif expected_type is bool:
            valid = type(value) is bool
        else:
            valid = isinstance(value, expected_type)
        if not valid:
            raise SchemaError("{} has an invalid type".format(key))
    return value


def _reject_unknown_fields(
    data: Mapping[str, Any],
    allowed: Iterable[str],
    name: str,
) -> None:
    unknown = set(data).difference(allowed)
    if unknown:
        raise SchemaError(
            "{} contains unknown field(s): {}".format(
                name,
                ", ".join(sorted(str(key) for key in unknown)),
            )
        )


def _string_sequence(value: Any, name: str) -> Tuple[str, ...]:
    items = _sequence(value, name)
    if any(not isinstance(item, str) for item in items):
        raise SchemaError("{} must contain only strings".format(name))
    return tuple(items)


def validate_envelope(data: Mapping[str, Any], schema: str) -> None:
    actual_schema = _required(data, "schema", str)
    if actual_schema != schema:
        raise SchemaError("Expected schema {!r}, got {!r}".format(schema, actual_schema))
    version = _required(data, "schema_version", int)
    if version != SCHEMA_VERSION:
        raise SchemaError(
            "Unsupported {} schema version {}; supported version is {}".format(schema, version, SCHEMA_VERSION)
        )


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    digest_algorithm: str
    digest: str
    volume_id: Optional[str] = None
    file_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise SchemaError("file path must not be empty")
        if type(self.size) is not int or self.size < 0:
            raise SchemaError("file size must not be negative")
        if type(self.mtime_ns) is not int or self.mtime_ns < 0:
            raise SchemaError("file mtime_ns must not be negative")
        if not isinstance(self.digest_algorithm, str) or not self.digest_algorithm:
            raise SchemaError("digest_algorithm must not be empty")
        if not isinstance(self.digest, str) or not self.digest:
            raise SchemaError("digest must not be empty")
        for name, value in (
            ("volume_id", self.volume_id),
            ("file_id", self.file_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise SchemaError("{} must be a non-empty string or null".format(name))

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "digest_algorithm": self.digest_algorithm,
            "digest": self.digest,
        }
        if self.volume_id is not None:
            result["volume_id"] = self.volume_id
        if self.file_id is not None:
            result["file_id"] = self.file_id
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FileRecord":
        data = _mapping(raw, "file")
        _reject_unknown_fields(
            data,
            {
                "path",
                "size",
                "mtime_ns",
                "digest_algorithm",
                "digest",
                "volume_id",
                "file_id",
            },
            "file",
        )
        volume_id = data.get("volume_id")
        file_id = data.get("file_id")
        if volume_id is not None and not isinstance(volume_id, str):
            raise SchemaError("volume_id must be a string or null")
        if file_id is not None and not isinstance(file_id, str):
            raise SchemaError("file_id must be a string or null")
        return cls(
            path=_required(data, "path", str),
            size=_required(data, "size", int),
            mtime_ns=_required(data, "mtime_ns", int),
            digest_algorithm=_required(data, "digest_algorithm", str),
            digest=_required(data, "digest", str),
            volume_id=volume_id,
            file_id=file_id,
        )


@dataclass(frozen=True)
class ScanGroup:
    group_id: str
    verification: str
    verification_method: str
    reference: FileRecord
    duplicates: Tuple[FileRecord, ...]

    def __post_init__(self) -> None:
        if not self.group_id:
            raise SchemaError("group_id must not be empty")
        if self.verification not in VERIFICATION_LEVELS:
            raise SchemaError("Unknown verification level: {}".format(self.verification))
        if not self.verification_method:
            raise SchemaError("verification_method must not be empty")
        if not self.duplicates:
            raise SchemaError("a duplicate group must contain at least one duplicate")
        paths = [self.reference.path] + [item.path for item in self.duplicates]
        if len(paths) != len(set(paths)):
            raise SchemaError("a duplicate group must not contain the same path more than once")

    @property
    def files(self) -> Tuple[FileRecord, ...]:
        return (self.reference,) + self.duplicates

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "verification": self.verification,
            "verification_method": self.verification_method,
            "reference": self.reference.to_dict(),
            "duplicates": [item.to_dict() for item in self.duplicates],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScanGroup":
        data = _mapping(raw, "group")
        _reject_unknown_fields(
            data,
            {
                "group_id",
                "verification",
                "verification_method",
                "reference",
                "duplicates",
            },
            "group",
        )
        duplicate_data = _sequence(_required(data, "duplicates"), "duplicates")
        return cls(
            group_id=_required(data, "group_id", str),
            verification=_required(data, "verification", str),
            verification_method=_required(data, "verification_method", str),
            reference=FileRecord.from_dict(_mapping(_required(data, "reference"), "reference")),
            duplicates=tuple(FileRecord.from_dict(_mapping(item, "duplicate")) for item in duplicate_data),
        )


@dataclass(frozen=True)
class ScanIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScanIssue":
        data = _mapping(raw, "issue")
        _reject_unknown_fields(
            data,
            {"path", "code", "message"},
            "issue",
        )
        return cls(
            path=_required(data, "path", str),
            code=_required(data, "code", str),
            message=_required(data, "message", str),
        )


@dataclass(frozen=True)
class ScanSummary:
    discovered_files: int
    hashed_files: int
    verified_groups: int
    duplicate_files: int
    issues: int
    complete: bool

    def __post_init__(self) -> None:
        counters = (
            self.discovered_files,
            self.hashed_files,
            self.verified_groups,
            self.duplicate_files,
            self.issues,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise SchemaError("summary counters must be non-negative integers")
        if type(self.complete) is not bool:
            raise SchemaError("summary complete must be boolean")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovered_files": self.discovered_files,
            "hashed_files": self.hashed_files,
            "verified_groups": self.verified_groups,
            "duplicate_files": self.duplicate_files,
            "issues": self.issues,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScanSummary":
        data = _mapping(raw, "summary")
        _reject_unknown_fields(
            data,
            {
                "discovered_files",
                "hashed_files",
                "verified_groups",
                "duplicate_files",
                "issues",
                "complete",
            },
            "summary",
        )
        values = {
            "discovered_files": _required(data, "discovered_files", int),
            "hashed_files": _required(data, "hashed_files", int),
            "verified_groups": _required(data, "verified_groups", int),
            "duplicate_files": _required(data, "duplicate_files", int),
            "issues": _required(data, "issues", int),
        }
        if any(value < 0 for value in values.values()):
            raise SchemaError("summary counters must not be negative")
        complete = _required(data, "complete", bool)
        return cls(complete=complete, **values)


@dataclass(frozen=True)
class ScanCoverage:
    """Serializable safe-walk coverage for one requested root."""

    root: str
    complete: bool
    counters: Tuple[Tuple[str, int], ...]
    identity_capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root:
            raise SchemaError("coverage root must not be empty")
        if type(self.complete) is not bool:
            raise SchemaError("coverage complete must be boolean")
        if any(not isinstance(capability, str) or not capability for capability in self.identity_capabilities):
            raise SchemaError("identity capabilities must be non-empty strings")
        names = [name for name, _ in self.counters]
        if len(names) != len(set(names)):
            raise SchemaError("coverage counter names must be unique")
        for name, value in self.counters:
            if not name or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaError("coverage counters must be named non-negative integers")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "complete": self.complete,
            "counters": dict(self.counters),
            "identity_capabilities": list(self.identity_capabilities),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScanCoverage":
        data = _mapping(raw, "coverage")
        _reject_unknown_fields(
            data,
            {
                "root",
                "complete",
                "counters",
                "identity_capabilities",
            },
            "coverage",
        )
        raw_counters = _mapping(_required(data, "counters"), "coverage counters")
        counters = []
        for name, value in raw_counters.items():
            if not isinstance(name, str):
                raise SchemaError("coverage counter names must be strings")
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaError("coverage counter values must be integers")
            counters.append((name, value))
        capabilities = _string_sequence(
            _required(data, "identity_capabilities"),
            "identity_capabilities",
        )
        return cls(
            root=_required(data, "root", str),
            complete=_required(data, "complete", bool),
            counters=tuple(sorted(counters)),
            identity_capabilities=capabilities,
        )


@dataclass(frozen=True)
class ScanRequest:
    roots: Tuple[str, ...]
    mode: str = "exact"
    recursive: bool = True
    min_size: int = 0
    big_file_size: int = 0
    max_files: int = DEFAULT_SCAN_MAX_FILES
    max_issues: int = DEFAULT_SCAN_MAX_ISSUES
    max_groups: int = DEFAULT_SCAN_MAX_GROUPS
    max_seconds: float = DEFAULT_SCAN_MAX_SECONDS

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("at least one scan root is required")
        if self.mode != "exact":
            raise ValueError("the built-in adapter currently supports only exact scans")
        if type(self.recursive) is not bool:
            raise ValueError("recursive must be boolean")
        if type(self.min_size) is not int or self.min_size < 0:
            raise ValueError("min_size must not be negative")
        if type(self.big_file_size) is not int or self.big_file_size < 0:
            raise ValueError("big_file_size must not be negative")
        for name in ("max_files", "max_issues", "max_groups"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be a finite positive number")


@dataclass(frozen=True)
class ScanReport:
    scan_id: str
    created_at: str
    roots: Tuple[str, ...]
    mode: str
    groups: Tuple[ScanGroup, ...]
    issues: Tuple[ScanIssue, ...]
    coverage: Tuple[ScanCoverage, ...]
    summary: ScanSummary
    engine_version: str = __version__
    schema: str = SCAN_REPORT_SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.scan_id or not self.created_at or not self.engine_version:
            raise SchemaError("scan_id, created_at, and engine_version must not be empty")
        if (
            self.schema != SCAN_REPORT_SCHEMA
            or type(self.schema_version) is not int
            or self.schema_version != SCHEMA_VERSION
        ):
            raise SchemaError("scan report envelope does not match the supported schema")
        if not self.coverage:
            raise SchemaError("scan report must contain safe-walk coverage")
        verified_groups = sum(group.verification == VERIFIED_EXACT for group in self.groups)
        duplicate_files = sum(len(group.duplicates) for group in self.groups)
        if self.summary.verified_groups != verified_groups:
            raise SchemaError("summary verified_groups does not match the group records")
        if self.summary.duplicate_files != duplicate_files:
            raise SchemaError("summary duplicate_files does not match the group records")
        if self.summary.issues != len(self.issues):
            raise SchemaError("summary issues does not match the issue records")
        expected_complete = not self.issues and all(item.complete for item in self.coverage)
        if self.summary.complete != expected_complete:
            raise SchemaError("summary complete does not match safe-walk coverage and issues")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "scan_id": self.scan_id,
            "created_at": self.created_at,
            "engine_version": self.engine_version,
            "roots": list(self.roots),
            "mode": self.mode,
            "groups": [group.to_dict() for group in self.groups],
            "issues": [issue.to_dict() for issue in self.issues],
            "coverage": [item.to_dict() for item in self.coverage],
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScanReport":
        data = _mapping(raw, "scan report")
        _reject_unknown_fields(
            data,
            {
                "schema",
                "schema_version",
                "scan_id",
                "created_at",
                "engine_version",
                "roots",
                "mode",
                "groups",
                "issues",
                "coverage",
                "summary",
            },
            "scan report",
        )
        validate_envelope(data, SCAN_REPORT_SCHEMA)
        roots = _sequence(_required(data, "roots"), "roots")
        groups = _sequence(_required(data, "groups"), "groups")
        issues = _sequence(_required(data, "issues"), "issues")
        coverage = _sequence(_required(data, "coverage"), "coverage")
        return cls(
            scan_id=_required(data, "scan_id", str),
            created_at=_required(data, "created_at", str),
            engine_version=_required(data, "engine_version", str),
            roots=_string_sequence(roots, "roots"),
            mode=_required(data, "mode", str),
            groups=tuple(ScanGroup.from_dict(_mapping(group, "group")) for group in groups),
            issues=tuple(ScanIssue.from_dict(_mapping(issue, "issue")) for issue in issues),
            coverage=tuple(ScanCoverage.from_dict(_mapping(item, "coverage")) for item in coverage),
            summary=ScanSummary.from_dict(_mapping(_required(data, "summary"), "summary")),
        )


@dataclass(frozen=True)
class PlanAction:
    action_id: str
    group_id: str
    operation: str
    target: FileRecord
    reference: FileRecord
    verification: str

    def __post_init__(self) -> None:
        if not self.action_id or not self.group_id:
            raise SchemaError("action_id and group_id must not be empty")
        if self.operation != "quarantine":
            raise SchemaError(
                "Unsupported operation: {}; exact plans only support recoverable quarantine".format(self.operation)
            )
        if self.verification != VERIFIED_EXACT:
            raise SchemaError("Only verified_exact actions may be placed in a deletion plan")
        if self.target.path == self.reference.path:
            raise SchemaError("target and reference must be different files")
        if self.target.size != self.reference.size:
            raise SchemaError("target and reference sizes differ")
        if self.target.digest_algorithm != self.reference.digest_algorithm:
            raise SchemaError("target and reference digest algorithms differ")
        if self.target.digest_algorithm != DELETION_PROOF_ALGORITHM:
            raise SchemaError("deletion plans require SHA-256 file proofs")
        if self.target.digest != self.reference.digest:
            raise SchemaError("target and reference digests differ")
        if any(not record.volume_id or not record.file_id for record in (self.target, self.reference)):
            raise SchemaError("deletion plans require target and reference physical identities")
        if len(self.target.digest) != 64 or self.target.digest != self.target.digest.lower():
            raise SchemaError("SHA-256 file proofs must use 64 lowercase hexadecimal characters")
        try:
            bytes.fromhex(self.target.digest)
        except ValueError as error:
            raise SchemaError("SHA-256 file proof is not hexadecimal") from error
        expected_action_id = action_id_for(self.group_id, self.target.path, self.operation)
        if self.action_id != expected_action_id:
            raise SchemaError("action_id does not match the action contents")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "group_id": self.group_id,
            "operation": self.operation,
            "target": self.target.to_dict(),
            "reference": self.reference.to_dict(),
            "verification": self.verification,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanAction":
        data = _mapping(raw, "action")
        _reject_unknown_fields(
            data,
            {
                "action_id",
                "group_id",
                "operation",
                "target",
                "reference",
                "verification",
            },
            "action",
        )
        return cls(
            action_id=_required(data, "action_id", str),
            group_id=_required(data, "group_id", str),
            operation=_required(data, "operation", str),
            target=FileRecord.from_dict(_mapping(_required(data, "target"), "target")),
            reference=FileRecord.from_dict(_mapping(_required(data, "reference"), "reference")),
            verification=_required(data, "verification", str),
        )


def action_id_for(group_id: str, target_path: str, operation: str) -> str:
    value = "\0".join([group_id, target_path, operation]).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def plan_id_for(source_scan_id: str, roots: Sequence[str], actions: Sequence[PlanAction]) -> str:
    canonical = json.dumps(
        {
            "source_scan_id": source_scan_id,
            "roots": list(roots),
            "actions": [action.to_dict() for action in actions],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeletionPlan:
    plan_id: str
    created_at: str
    source_scan_id: str
    roots: Tuple[str, ...]
    actions: Tuple[PlanAction, ...]
    engine_version: str = __version__
    schema: str = DELETION_PLAN_SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.plan_id or not self.source_scan_id or not self.created_at or not self.engine_version:
            raise SchemaError("plan_id, source_scan_id, created_at, and engine_version must not be empty")
        if (
            self.schema != DELETION_PLAN_SCHEMA
            or type(self.schema_version) is not int
            or self.schema_version != SCHEMA_VERSION
        ):
            raise SchemaError("deletion plan envelope does not match the supported schema")
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise SchemaError("a deletion plan must not contain duplicate action IDs")
        normalized_targets = [os.path.normcase(os.path.abspath(action.target.path)) for action in self.actions]
        if len(normalized_targets) != len(set(normalized_targets)):
            raise SchemaError("a deletion plan must not target the same path more than once")
        normalized_references = {os.path.normcase(os.path.abspath(action.reference.path)) for action in self.actions}
        if normalized_references.intersection(normalized_targets):
            raise SchemaError("a deletion plan must not also target one of its reference paths")
        expected_plan_id = plan_id_for(self.source_scan_id, self.roots, self.actions)
        if self.plan_id != expected_plan_id:
            raise SchemaError("plan_id does not match the plan contents")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "engine_version": self.engine_version,
            "source_scan_id": self.source_scan_id,
            "roots": list(self.roots),
            "actions": [action.to_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeletionPlan":
        data = _mapping(raw, "deletion plan")
        _reject_unknown_fields(
            data,
            {
                "schema",
                "schema_version",
                "plan_id",
                "created_at",
                "engine_version",
                "source_scan_id",
                "roots",
                "actions",
            },
            "deletion plan",
        )
        validate_envelope(data, DELETION_PLAN_SCHEMA)
        roots = _string_sequence(_required(data, "roots"), "roots")
        actions = tuple(
            PlanAction.from_dict(_mapping(action, "action"))
            for action in _sequence(_required(data, "actions"), "actions")
        )
        return cls(
            plan_id=_required(data, "plan_id", str),
            created_at=_required(data, "created_at", str),
            engine_version=_required(data, "engine_version", str),
            source_scan_id=_required(data, "source_scan_id", str),
            roots=roots,
            actions=actions,
        )


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    target: str
    status: str
    message: str = ""
    safe_state: str = ""
    failure_code: str = ""
    operation_plan_path: str = ""
    quarantine_path: str = ""
    changed: bool = False

    def __post_init__(self) -> None:
        if not self.action_id or not self.target:
            raise SchemaError("action result ID and target must not be empty")
        if self.status not in {"ready", "stale", "applied", "failed"}:
            raise SchemaError("unsupported action result status: {}".format(self.status))
        if type(self.changed) is not bool:
            raise SchemaError("action result changed must be a boolean")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "target": self.target,
            "status": self.status,
            "message": self.message,
            "safe_state": self.safe_state,
            "failure_code": self.failure_code,
            "operation_plan_path": self.operation_plan_path,
            "quarantine_path": self.quarantine_path,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class ApplyReport:
    plan_id: str
    dry_run: bool
    results: Tuple[ActionResult, ...]
    created_at: str = field(default_factory=utc_now)
    schema: str = APPLY_REPORT_SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan_id, str)
            or not self.plan_id
            or not isinstance(self.created_at, str)
            or not self.created_at
        ):
            raise SchemaError("apply report plan_id and created_at must not be empty")
        if type(self.dry_run) is not bool:
            raise SchemaError("apply report dry_run must be boolean")
        if (
            self.schema != APPLY_REPORT_SCHEMA
            or type(self.schema_version) is not int
            or self.schema_version != SCHEMA_VERSION
        ):
            raise SchemaError("apply report envelope does not match the supported schema")

    @property
    def ready(self) -> int:
        return sum(item.status == "ready" for item in self.results)

    @property
    def stale(self) -> int:
        return sum(item.status == "stale" for item in self.results)

    @property
    def applied(self) -> int:
        return sum(item.status == "applied" for item in self.results)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "dry_run": self.dry_run,
            "results": [item.to_dict() for item in self.results],
            "summary": {
                "actions": len(self.results),
                "ready": self.ready,
                "stale": self.stale,
                "applied": self.applied,
                "failed": self.failed,
            },
        }


def groups_from_iterable(groups: Iterable[ScanGroup]) -> Tuple[ScanGroup, ...]:
    return tuple(groups)
