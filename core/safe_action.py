# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Fail-closed, proof-bound filesystem actions.

This module deliberately does not integrate with the legacy deletion path.  It provides a small
transaction layer that can be wired into the application after its behaviour has been reviewed.

The safety model is intentionally narrow:

* Only regular files are accepted. Directories, links, and reparse points are rejected.
* Both the target and its keeper are byte-compared when a plan is built and immediately before the
  target is staged.
* Paths, file identities, file types, and version metadata are bound to an immutable plan.
* A target is first moved to a same-volume quarantine. Permanent deletion is a separate,
  idempotent ``finalize()`` operation.
* Every state transition is written to an append-only, fsynced JSONL journal.

The implementation protects against accidental replacement and ordinary concurrent changes. It
does not claim to defeat a privileged process or a hostile filesystem that can forge identities or
metadata. Such filesystems should not be presented as verified by callers.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import logging
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from core.file_generation import FileGenerationToken, get_file_generation_token_from_fd
from core.file_identity import (
    FileIdentity as PhysicalFileIdentity,
    FileIdentityError,
    IdentityCapability,
    IdentityConfidence,
    get_file_identity,
    get_file_identity_from_fd,
    windows_extended_path,
)
from core.safe_json import JOURNAL_RECORD_JSON_LIMITS, strict_bounded_json_loads
from hscommon.atomic_rename import (
    BoundDirectory,
    RenameCommit,
    open_bound_directory,
    rename_no_replace as atomic_rename_no_replace,
)

PLAN_SCHEMA_VERSION = 3
JOURNAL_SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"
READ_CHUNK_SIZE = 1024 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_JOURNAL_LINE_BYTES = 64 * 1024
MAX_JOURNAL_EVENTS = 250_000
# One operation journal must retain enough unused space for a complete
# stage-and-recover lifecycle.  Reserving worst-case line sizes makes this
# independent of path length and prevents a target from moving when its
# completion/restore records could no longer be persisted.
LIFECYCLE_JOURNAL_RESERVE_EVENTS = 12
RECOVERY_JOURNAL_RESERVE_EVENTS = 8


class EntryType(Enum):
    REGULAR_FILE = "regular_file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class ActionState(Enum):
    FAILED = "failed"
    PLANNED = "planned"
    STAGED = "staged"
    RESTORED = "restored"
    FINALIZED = "finalized"


class FailureCode(Enum):
    NONE = "none"
    INVALID_PLAN = "invalid_plan"
    PATH_OUTSIDE_ALLOWED_ROOTS = "path_outside_allowed_roots"
    PATH_CHANGED = "path_changed"
    PATH_HAS_LINK_COMPONENT = "path_has_link_component"
    MISSING_TARGET = "missing_target"
    MISSING_KEEPER = "missing_keeper"
    UNSUPPORTED_TYPE = "unsupported_type"
    TYPE_MISMATCH = "type_mismatch"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"
    METADATA_MISMATCH = "metadata_mismatch"
    CONTENT_MISMATCH = "content_mismatch"
    UNSTABLE_CONTENT = "unstable_content"
    SAME_IDENTITY = "same_identity"
    QUARANTINE_VOLUME_MISMATCH = "quarantine_volume_mismatch"
    QUARANTINE_CONFLICT = "quarantine_conflict"
    TARGET_CONFLICT = "target_conflict"
    INVALID_STATE = "invalid_state"
    JOURNAL_CORRUPT = "journal_corrupt"
    JOURNAL_ERROR = "journal_error"
    IO_ERROR = "io_error"
    INTERNAL_ERROR = "internal_error"


class JournalEventType(Enum):
    PREPARED = "prepared"
    STAGED = "staged"
    STAGED_RECOVERED = "staged_recovered"
    RESTORE_PREPARED = "restore_prepared"
    RESTORED = "restored"
    RESTORED_RECOVERED = "restored_recovered"
    FINALIZE_PREPARED = "finalize_prepared"
    FINALIZE_TOMBSTONED = "finalize_tombstoned"
    FINALIZED = "finalized"
    FINALIZED_RECOVERED = "finalized_recovered"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True)
class FileIdentity:
    namespace: str
    capability: str
    confidence: int
    volume_id: int
    file_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "capability": self.capability,
            "confidence": self.confidence,
            "volume_id": self.volume_id,
            "file_id": self.file_id,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "FileIdentity":
        _require_exact_keys(
            value,
            {"namespace", "capability", "confidence", "volume_id", "file_id"},
            "file identity",
        )
        identity = cls(
            namespace=_require_str(value["namespace"], "identity namespace"),
            capability=_require_str(value["capability"], "identity capability"),
            confidence=_require_int(value["confidence"], "identity confidence"),
            volume_id=_require_nonnegative_int(value["volume_id"], "volume_id"),
            file_id=_require_str(value["file_id"], "file_id"),
        )
        identity._validate()
        return identity

    @classmethod
    def from_physical(cls, value: PhysicalFileIdentity) -> "FileIdentity":
        if isinstance(value.file_id, bytes):
            file_id = value.file_id.hex()
        else:
            file_id = str(value.file_id)
        result = cls(
            namespace=value.namespace,
            capability=value.capability.value,
            confidence=int(value.confidence),
            volume_id=value.volume_id,
            file_id=file_id,
        )
        result._validate()
        return result

    def _validate(self) -> None:
        try:
            capability = IdentityCapability(self.capability)
            confidence = IdentityConfidence(self.confidence)
        except ValueError as error:
            raise ValueError("Invalid file identity capability or confidence") from error
        if not self.namespace or "\0" in self.namespace or self.volume_id < 0 or not self.file_id:
            raise ValueError("Invalid file identity fields")
        if capability is IdentityCapability.WINDOWS_FILE_ID_128:
            try:
                decoded = bytes.fromhex(self.file_id)
            except ValueError as error:
                raise ValueError("Invalid Windows FILE_ID_128 encoding") from error
            if (
                self.namespace != "windows"
                or self.volume_id <= 0
                or len(decoded) != 16
                or not any(decoded)
                or confidence is not IdentityConfidence.HIGH
            ):
                raise ValueError("Windows destructive operations require a nonzero high-confidence FILE_ID_128")
        elif self.namespace == "windows":
            raise ValueError("Windows destructive operations require FILE_ID_128")
        elif (
            self.namespace != "posix"
            or capability is not IdentityCapability.POSIX_DEVICE_INODE
            or confidence is not IdentityConfidence.HIGH
        ):
            raise ValueError("POSIX destructive operations require a high-confidence device/inode identity")
        else:
            try:
                if int(self.file_id) <= 0:
                    raise ValueError
            except ValueError as error:
                raise ValueError("Invalid POSIX inode identity") from error


@dataclass(frozen=True)
class FileProof:
    path: str
    resolved_path: str
    entry_type: EntryType
    identity: FileIdentity
    size: int
    mtime_ns: int
    generation_token: str
    digest_algorithm: str
    digest_hex: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "entry_type": self.entry_type.value,
            "identity": self.identity.to_dict(),
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "generation_token": self.generation_token,
            "digest_algorithm": self.digest_algorithm,
            "digest_hex": self.digest_hex,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "FileProof":
        _require_exact_keys(
            value,
            {
                "path",
                "resolved_path",
                "entry_type",
                "identity",
                "size",
                "mtime_ns",
                "generation_token",
                "digest_algorithm",
                "digest_hex",
            },
            "file proof",
        )
        try:
            entry_type = EntryType(value["entry_type"])
        except (TypeError, ValueError):
            raise ValueError("Invalid entry_type in file proof")
        digest_algorithm = _require_str(value["digest_algorithm"], "digest_algorithm")
        digest_hex = _require_str(value["digest_hex"], "digest_hex")
        if digest_algorithm != HASH_ALGORITHM:
            raise ValueError("Unsupported digest algorithm")
        if len(digest_hex) != hashlib.sha256().digest_size * 2:
            raise ValueError("Invalid digest length")
        try:
            bytes.fromhex(digest_hex)
        except ValueError:
            raise ValueError("Invalid digest encoding")
        generation_token = _require_str(value["generation_token"], "generation_token")
        try:
            FileGenerationToken.from_encoded(bytes.fromhex(generation_token))
        except ValueError as error:
            raise ValueError("Invalid generation_token in file proof") from error
        return cls(
            path=_require_str(value["path"], "path"),
            resolved_path=_require_str(value["resolved_path"], "resolved_path"),
            entry_type=entry_type,
            identity=FileIdentity.from_dict(_require_dict(value["identity"], "identity")),
            size=_require_nonnegative_int(value["size"], "size"),
            mtime_ns=_require_int(value["mtime_ns"], "mtime_ns"),
            generation_token=generation_token,
            digest_algorithm=digest_algorithm,
            digest_hex=digest_hex,
        )


@dataclass(frozen=True)
class OperationPlan:
    plan_id: str
    created_ns: int
    allowed_roots: Tuple[str, ...]
    quarantine_root: str
    target: FileProof
    keeper: FileProof
    schema_version: int = PLAN_SCHEMA_VERSION

    @property
    def quarantine_directory(self) -> Path:
        return Path(self.quarantine_root).joinpath(self.plan_id)

    @property
    def quarantine_path(self) -> Path:
        return self.quarantine_directory.joinpath("payload")

    @property
    def finalize_path(self) -> Path:
        return self.quarantine_directory.joinpath("finalizing")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_ns": self.created_ns,
            "allowed_roots": list(self.allowed_roots),
            "quarantine_root": self.quarantine_root,
            "target": self.target.to_dict(),
            "keeper": self.keeper.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "OperationPlan":
        _require_exact_keys(
            value,
            {
                "schema_version",
                "plan_id",
                "created_ns",
                "allowed_roots",
                "quarantine_root",
                "target",
                "keeper",
            },
            "operation plan",
        )
        schema_version = _require_int(value["schema_version"], "schema_version")
        if schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError("Unsupported operation plan schema")
        plan_id = _validate_plan_id(_require_str(value["plan_id"], "plan_id"))
        roots_value = value["allowed_roots"]
        if not isinstance(roots_value, list) or not roots_value:
            raise ValueError("allowed_roots must be a non-empty list")
        allowed_roots = tuple(_require_str(root, "allowed root") for root in roots_value)
        plan = cls(
            schema_version=schema_version,
            plan_id=plan_id,
            created_ns=_require_nonnegative_int(value["created_ns"], "created_ns"),
            allowed_roots=allowed_roots,
            quarantine_root=_require_str(value["quarantine_root"], "quarantine_root"),
            target=FileProof.from_dict(_require_dict(value["target"], "target")),
            keeper=FileProof.from_dict(_require_dict(value["keeper"], "keeper")),
        )
        _validate_plan_shape(plan)
        return plan

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Failure:
    code: FailureCode
    message: str
    path: Optional[str] = None


@dataclass(frozen=True)
class PlanBuildResult:
    plan: Optional[OperationPlan]
    failure: Optional[Failure]

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.failure is None


@dataclass(frozen=True)
class ActionResult:
    plan_id: str
    state: ActionState
    code: FailureCode
    message: str
    changed: bool
    quarantine_path: str

    @property
    def ok(self) -> bool:
        return self.code is FailureCode.NONE


@dataclass(frozen=True)
class JournalEvent:
    event_id: str
    timestamp_ns: int
    plan_id: str
    plan_fingerprint: str
    event: JournalEventType
    details: Dict[str, Any]


class _SafetyFailure(Exception):
    def __init__(self, code: FailureCode, message: str, path: Optional[Path] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None


class JournalError(Exception):
    pass


def _require_exact_keys(value: Dict[str, Any], expected: set, label: str) -> None:
    if set(value) != expected:
        raise ValueError("Unexpected or missing keys in {}".format(label))


def _require_dict(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("{} must be an object".format(label))
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("{} must be a non-empty string".format(label))
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("{} must be an integer".format(label))
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    result = _require_int(value, label)
    if result < 0:
        raise ValueError("{} must not be negative".format(label))
    return result


def _validate_plan_id(plan_id: str) -> str:
    try:
        parsed = uuid.UUID(plan_id)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("plan_id must be a UUID")
    if parsed.hex != plan_id.replace("-", "").lower():
        raise ValueError("plan_id must use a canonical UUID representation")
    return str(parsed)


def _validate_plan_shape(plan: OperationPlan) -> None:
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported operation plan schema")
    _validate_plan_id(plan.plan_id)
    if not plan.allowed_roots:
        raise ValueError("Operation plan has no allowed roots")
    if plan.target.entry_type is not EntryType.REGULAR_FILE or plan.keeper.entry_type is not EntryType.REGULAR_FILE:
        raise ValueError("Operation plans only support regular files")
    if plan.target.identity == plan.keeper.identity:
        raise ValueError("Target and keeper have the same identity")
    if plan.target.digest_hex != plan.keeper.digest_hex or plan.target.size != plan.keeper.size:
        raise ValueError("Target and keeper proofs do not have identical content")


def _entry_type(file_stat: os.stat_result) -> EntryType:
    mode = file_stat.st_mode
    if stat.S_ISREG(mode):
        return EntryType.REGULAR_FILE
    if stat.S_ISDIR(mode):
        return EntryType.DIRECTORY
    if stat.S_ISLNK(mode):
        return EntryType.SYMLINK
    return EntryType.OTHER


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and attributes & marker)


def _identity_from_fd(
    file_handle: BinaryIO,
    file_stat: os.stat_result,
    path: Optional[Path] = None,
) -> FileIdentity:
    try:
        return FileIdentity.from_physical(
            get_file_identity_from_fd(
                file_handle.fileno(),
                path=path,
                stat_result=file_stat,
            )
        )
    except (FileIdentityError, ValueError) as error:
        raise _SafetyFailure(FailureCode.IDENTITY_UNAVAILABLE, str(error), path) from error


def _identity_from_path(path: Path, file_stat: os.stat_result) -> FileIdentity:
    try:
        return FileIdentity.from_physical(
            get_file_identity(
                path,
                follow_symlinks=False,
                stat_result=file_stat,
            )
        )
    except (FileIdentityError, ValueError) as error:
        raise _SafetyFailure(FailureCode.IDENTITY_UNAVAILABLE, str(error), path) from error


def _mtime_ns(file_stat: os.stat_result) -> int:
    value = getattr(file_stat, "st_mtime_ns", None)
    if value is not None:
        return int(value)
    return int(file_stat.st_mtime * 1_000_000_000)


def _version_tuple(file_stat: os.stat_result) -> Tuple[EntryType, int, int, int]:
    return (
        _entry_type(file_stat),
        int(file_stat.st_size),
        _mtime_ns(file_stat),
        int(file_stat.st_ctime_ns),
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    normalized_path = _normalized(path)
    for root in roots:
        normalized_root = _normalized(root)
        try:
            if os.path.commonpath([normalized_path, normalized_root]) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _path_components(path: Path) -> Iterator[Path]:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        yield current


class FileSystemAdapter:
    """Platform boundary for no-follow opens and durable filesystem operations."""

    @contextlib.contextmanager
    def open_readonly(self, path: Path) -> Iterator[BinaryIO]:
        fd = self._open_fd(path)
        with os.fdopen(fd, "rb", closefd=True) as file_handle:
            yield file_handle

    def _open_fd(self, path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.open(str(path), flags)

    def lstat(self, path: Path) -> os.stat_result:
        return os.lstat(str(path))

    def resolve(self, path: Path, strict: bool = True) -> Path:
        return path.resolve(strict=strict)

    def lexists(self, path: Path) -> bool:
        return os.path.lexists(str(path))

    def make_directory(self, path: Path) -> None:
        os.mkdir(str(path), 0o700)
        self.fsync_directory(path.parent)

    def rename_no_replace_bound(
        self,
        source_directory: BoundDirectory,
        source_name: str,
        destination_directory: BoundDirectory,
        destination_name: str,
    ) -> RenameCommit:
        return atomic_rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )

    def rename_no_replace(self, source: Path, destination: Path) -> RenameCommit:
        """Bind both parents before issuing the native no-replace rename."""

        source = _absolute(source)
        destination = _absolute(destination)
        with open_bound_directory(source.parent) as source_directory:
            with open_bound_directory(destination.parent) as destination_directory:
                commit = self.rename_no_replace_bound(
                    source_directory,
                    source.name,
                    destination_directory,
                    destination.name,
                )
                if not commit.postcondition_verified:
                    logging.warning(
                        "Atomic rename committed %s, but post-commit inspection failed: %s",
                        destination,
                        commit.verification_error,
                    )
                return commit

    def unlink(self, path: Path) -> None:
        os.unlink(str(path))

    def fsync_directory(self, path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(str(path), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def same_volume(self, identity: FileIdentity, directory: Path) -> bool:
        return (
            identity.volume_id
            == _identity_from_path(
                directory,
                self.lstat(directory),
            ).volume_id
        )


class WindowsFileSystemAdapter(FileSystemAdapter):
    """Windows implementation that opens the final component as a reparse point, not through it."""

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    def _open_fd(self, path: Path) -> int:
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            windows_extended_path(path),
            self._GENERIC_READ,
            # Staging renames the verified object while these handles remain
            # open, then checks the moved entry's identity against the handle.
            # Windows therefore needs delete sharing for that same-handle move.
            self._FILE_SHARE_READ | self._FILE_SHARE_DELETE,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_SEQUENTIAL_SCAN | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except Exception:
            close_handle(handle)
            raise


class PosixFileSystemAdapter(FileSystemAdapter):
    def rename_no_replace(self, source: Path, destination: Path) -> RenameCommit:
        """Use the shared descriptor-bound primitive on supported POSIX systems."""

        return super().rename_no_replace(source, destination)


def platform_file_system() -> FileSystemAdapter:
    if os.name == "nt":
        return WindowsFileSystemAdapter()
    return PosixFileSystemAdapter()


def cleanup_created_regular_file(
    path: Path,
    created_identity: Tuple[int, int],
    fs: FileSystemAdapter,
) -> bool:
    """Best-effort cleanup of one private staging name bound at creation.

    The parent directory is held by identity and the final entry is inspected
    without following links.  A missing path or any replacement is preserved.
    The caller must obtain ``created_identity`` from ``fstat()`` on the
    descriptor returned by the exclusive create.
    """

    absolute = _absolute(path)
    try:
        with open_bound_directory(absolute.parent) as parent:
            try:
                current = parent.lstat(absolute.name)
            except FileNotFoundError:
                return False
            if (
                not stat.S_ISREG(current.st_mode)
                or _is_reparse_point(current)
                or (int(current.st_dev), int(current.st_ino)) != tuple(created_identity)
            ):
                return False
            parent.unlink_parts((absolute.name,))
        fs.fsync_directory(absolute.parent)
        return True
    except (FileNotFoundError, NotADirectoryError, OSError):
        # Cleanup is deliberately subordinate to the publication error.  Any
        # ambiguity leaves the entry for inspection instead of deleting it.
        return False


def _ensure_no_link_components(path: Path, fs: FileSystemAdapter) -> None:
    for component in _path_components(path):
        try:
            component_stat = fs.lstat(component)
        except FileNotFoundError:
            raise
        if _entry_type(component_stat) is EntryType.SYMLINK or _is_reparse_point(component_stat):
            raise _SafetyFailure(
                FailureCode.PATH_HAS_LINK_COMPONENT,
                "A path component is a symbolic link or reparse point",
                component,
            )


def _validate_directory(path: Path, fs: FileSystemAdapter, label: str) -> Tuple[Path, os.stat_result]:
    absolute = _absolute(path)
    _ensure_no_link_components(absolute, fs)
    directory_stat = fs.lstat(absolute)
    if _entry_type(directory_stat) is not EntryType.DIRECTORY or _is_reparse_point(directory_stat):
        raise _SafetyFailure(FailureCode.UNSUPPORTED_TYPE, "{} is not a plain directory".format(label), absolute)
    return fs.resolve(absolute, strict=True), directory_stat


def _validate_private_directory(
    path: Path,
    fs: FileSystemAdapter,
    label: str,
) -> Tuple[Path, os.stat_result]:
    """Validate application state separately from user library roots.

    POSIX exposes stable owner/mode bits, so existing state must be owned by
    the current effective user and not writable by group or other users.
    Windows reparse-point and handle checks still apply, but Python's portable
    ``stat`` API cannot prove an equivalent ACL ownership policy.
    """

    resolved, directory_stat = _validate_directory(path, fs, label)
    if os.name == "posix":
        if int(directory_stat.st_uid) != int(os.geteuid()):
            raise _SafetyFailure(
                FailureCode.INVALID_PLAN,
                "{} is not owned by the current user".format(label),
                Path(path),
            )
        if stat.S_IMODE(directory_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise _SafetyFailure(
                FailureCode.INVALID_PLAN,
                "{} is writable by group or other users".format(label),
                Path(path),
            )
    return resolved, directory_stat


def _validate_allowed_path(path: Path, roots: Sequence[Path], fs: FileSystemAdapter) -> Path:
    absolute = _absolute(path)
    _ensure_no_link_components(absolute, fs)
    resolved = fs.resolve(absolute, strict=True)
    if not _is_within(resolved, roots):
        raise _SafetyFailure(
            FailureCode.PATH_OUTSIDE_ALLOWED_ROOTS,
            "Path is outside the operation plan's allowed roots",
            absolute,
        )
    return resolved


def _validate_restore_parent(proof: FileProof, fs: FileSystemAdapter) -> None:
    target = Path(proof.path)
    parent = target.parent
    _ensure_no_link_components(parent, fs)
    current_parent = fs.resolve(parent, strict=True)
    expected_parent = Path(proof.resolved_path).parent
    if _normalized(current_parent) != _normalized(expected_parent):
        raise _SafetyFailure(FailureCode.PATH_CHANGED, "The target's parent path now resolves elsewhere", parent)


@dataclass(frozen=True)
class _OpenedProof:
    identity: FileIdentity
    entry_type: EntryType
    size: int
    mtime_ns: int
    generation_token: str
    digest_hex: str


def _opened_proof(
    file_handle: BinaryIO,
    digest_hex: str,
    before: os.stat_result,
    after: os.stat_result,
    generation_before: FileGenerationToken,
) -> _OpenedProof:
    generation_after = get_file_generation_token_from_fd(
        file_handle.fileno(),
        stat_result=after,
    )
    if _version_tuple(before) != _version_tuple(after) or generation_before != generation_after:
        raise _SafetyFailure(FailureCode.UNSTABLE_CONTENT, "File changed while it was being read")
    return _OpenedProof(
        identity=_identity_from_fd(file_handle, after),
        entry_type=_entry_type(after),
        size=int(after.st_size),
        mtime_ns=_mtime_ns(after),
        generation_token=generation_after.encoded.hex(),
        digest_hex=digest_hex,
    )


def _compare_open_files(target_handle: BinaryIO, keeper_handle: BinaryIO) -> Tuple[_OpenedProof, _OpenedProof]:
    target_before = os.fstat(target_handle.fileno())
    keeper_before = os.fstat(keeper_handle.fileno())
    if _entry_type(target_before) is not EntryType.REGULAR_FILE:
        raise _SafetyFailure(FailureCode.UNSUPPORTED_TYPE, "Target is not a regular file")
    if _entry_type(keeper_before) is not EntryType.REGULAR_FILE:
        raise _SafetyFailure(FailureCode.UNSUPPORTED_TYPE, "Keeper is not a regular file")
    target_generation_before = get_file_generation_token_from_fd(
        target_handle.fileno(),
        stat_result=target_before,
    )
    keeper_generation_before = get_file_generation_token_from_fd(
        keeper_handle.fileno(),
        stat_result=keeper_before,
    )
    target_handle.seek(0)
    keeper_handle.seek(0)
    target_hash = hashlib.sha256()
    keeper_hash = hashlib.sha256()
    while True:
        target_chunk = target_handle.read(READ_CHUNK_SIZE)
        keeper_chunk = keeper_handle.read(READ_CHUNK_SIZE)
        if target_chunk != keeper_chunk:
            raise _SafetyFailure(FailureCode.CONTENT_MISMATCH, "Target and keeper contents differ")
        if not target_chunk:
            break
        target_hash.update(target_chunk)
        keeper_hash.update(keeper_chunk)
    target_after = os.fstat(target_handle.fileno())
    keeper_after = os.fstat(keeper_handle.fileno())
    return (
        _opened_proof(
            target_handle,
            target_hash.hexdigest(),
            target_before,
            target_after,
            target_generation_before,
        ),
        _opened_proof(
            keeper_handle,
            keeper_hash.hexdigest(),
            keeper_before,
            keeper_after,
            keeper_generation_before,
        ),
    )


def _compare_lstat_to_open(path_stat: os.stat_result, opened: _OpenedProof, path: Path) -> None:
    if _entry_type(path_stat) is not opened.entry_type:
        raise _SafetyFailure(FailureCode.TYPE_MISMATCH, "Path type changed while it was opened", path)
    if _identity_from_path(path, path_stat) != opened.identity:
        raise _SafetyFailure(FailureCode.IDENTITY_MISMATCH, "Path identity changed while it was opened", path)


def _proof_from_opened(path: Path, resolved: Path, opened: _OpenedProof) -> FileProof:
    return FileProof(
        path=str(path),
        resolved_path=str(resolved),
        entry_type=opened.entry_type,
        identity=opened.identity,
        size=opened.size,
        mtime_ns=opened.mtime_ns,
        generation_token=opened.generation_token,
        digest_algorithm=HASH_ALGORITHM,
        digest_hex=opened.digest_hex,
    )


def _check_expected(opened: _OpenedProof, expected: FileProof, strict_metadata: bool) -> None:
    if opened.entry_type is not expected.entry_type:
        raise _SafetyFailure(FailureCode.TYPE_MISMATCH, "File type no longer matches its proof")
    if opened.identity != expected.identity:
        raise _SafetyFailure(FailureCode.IDENTITY_MISMATCH, "File identity no longer matches its proof")
    if opened.size != expected.size:
        raise _SafetyFailure(FailureCode.METADATA_MISMATCH, "File size no longer matches its proof")
    if strict_metadata and (
        opened.mtime_ns != expected.mtime_ns or opened.generation_token != expected.generation_token
    ):
        raise _SafetyFailure(FailureCode.METADATA_MISMATCH, "File metadata no longer matches its proof")
    if opened.digest_hex != expected.digest_hex:
        raise _SafetyFailure(FailureCode.CONTENT_MISMATCH, "File content no longer matches its proof")


def _check_expected_stat(file_stat: os.stat_result, expected: FileProof, strict_metadata: bool) -> None:
    if _entry_type(file_stat) is not expected.entry_type:
        raise _SafetyFailure(FailureCode.TYPE_MISMATCH, "File type no longer matches its proof")
    if int(file_stat.st_size) != expected.size:
        raise _SafetyFailure(FailureCode.METADATA_MISMATCH, "File size no longer matches its proof")
    if strict_metadata and _mtime_ns(file_stat) != expected.mtime_ns:
        raise _SafetyFailure(FailureCode.METADATA_MISMATCH, "File metadata no longer matches its proof")


@contextlib.contextmanager
def _open_verified_pair(
    target_path: Path,
    keeper_path: Path,
    fs: FileSystemAdapter,
    roots: Optional[Sequence[Path]] = None,
    expected_target: Optional[FileProof] = None,
    expected_keeper: Optional[FileProof] = None,
    strict_target_metadata: bool = True,
) -> Iterator[Tuple[BinaryIO, BinaryIO]]:
    if roots is not None:
        target_resolved = _validate_allowed_path(target_path, roots, fs)
        keeper_resolved = _validate_allowed_path(keeper_path, roots, fs)
        if expected_target is not None and _normalized(target_resolved) != _normalized(
            Path(expected_target.resolved_path)
        ):
            raise _SafetyFailure(FailureCode.PATH_CHANGED, "Target path now resolves elsewhere", target_path)
        if expected_keeper is not None and _normalized(keeper_resolved) != _normalized(
            Path(expected_keeper.resolved_path)
        ):
            raise _SafetyFailure(FailureCode.PATH_CHANGED, "Keeper path now resolves elsewhere", keeper_path)
    else:
        _ensure_no_link_components(target_path, fs)
        _ensure_no_link_components(keeper_path, fs)

    target_lstat = fs.lstat(target_path)
    keeper_lstat = fs.lstat(keeper_path)
    if _is_reparse_point(target_lstat) or _is_reparse_point(keeper_lstat):
        raise _SafetyFailure(FailureCode.UNSUPPORTED_TYPE, "Reparse points cannot be verified")
    if _entry_type(target_lstat) is not EntryType.REGULAR_FILE:
        code = FailureCode.TYPE_MISMATCH if expected_target is not None else FailureCode.UNSUPPORTED_TYPE
        raise _SafetyFailure(code, "Target is not a regular file", target_path)
    if _entry_type(keeper_lstat) is not EntryType.REGULAR_FILE:
        code = FailureCode.TYPE_MISMATCH if expected_keeper is not None else FailureCode.UNSUPPORTED_TYPE
        raise _SafetyFailure(code, "Keeper is not a regular file", keeper_path)

    with contextlib.ExitStack() as stack:
        target_handle = stack.enter_context(fs.open_readonly(target_path))
        keeper_handle = stack.enter_context(fs.open_readonly(keeper_path))
        target_handle_stat = os.fstat(target_handle.fileno())
        keeper_handle_stat = os.fstat(keeper_handle.fileno())
        target_identity = _identity_from_fd(target_handle, target_handle_stat, target_path)
        keeper_identity = _identity_from_fd(keeper_handle, keeper_handle_stat, keeper_path)
        if target_identity == keeper_identity:
            raise _SafetyFailure(FailureCode.SAME_IDENTITY, "Target and keeper refer to the same underlying file")
        if expected_target is not None and target_identity != expected_target.identity:
            raise _SafetyFailure(FailureCode.IDENTITY_MISMATCH, "Target identity no longer matches its proof")
        if expected_keeper is not None and keeper_identity != expected_keeper.identity:
            raise _SafetyFailure(FailureCode.IDENTITY_MISMATCH, "Keeper identity no longer matches its proof")
        if expected_target is not None:
            _check_expected_stat(target_handle_stat, expected_target, strict_target_metadata)
        if expected_keeper is not None:
            _check_expected_stat(keeper_handle_stat, expected_keeper, True)
        target_opened, keeper_opened = _compare_open_files(target_handle, keeper_handle)
        _compare_lstat_to_open(target_lstat, target_opened, target_path)
        _compare_lstat_to_open(keeper_lstat, keeper_opened, keeper_path)
        if target_opened.identity == keeper_opened.identity:
            raise _SafetyFailure(FailureCode.SAME_IDENTITY, "Target and keeper refer to the same underlying file")
        if expected_target is not None:
            _check_expected(target_opened, expected_target, strict_target_metadata)
        if expected_keeper is not None:
            _check_expected(keeper_opened, expected_keeper, True)
        yield target_handle, keeper_handle


def _failure_from_exception(error: _SafetyFailure) -> Failure:
    return Failure(code=error.code, message=error.message, path=error.path)


def build_operation_plan(
    target: Path,
    keeper: Path,
    allowed_roots: Iterable[Path],
    quarantine_root: Path,
    plan_id: Optional[str] = None,
    fs: Optional[FileSystemAdapter] = None,
) -> PlanBuildResult:
    """Build an immutable plan after a stable, byte-for-byte comparison."""

    file_system = fs or platform_file_system()
    try:
        root_paths: List[Path] = []
        for root in allowed_roots:
            resolved_root, _ = _validate_directory(Path(root), file_system, "Allowed root")
            root_paths.append(resolved_root)
        if not root_paths:
            raise _SafetyFailure(FailureCode.INVALID_PLAN, "At least one allowed root is required")
        roots = tuple(sorted({_normalized(root): root for root in root_paths}.values(), key=_normalized))

        quarantine_resolved, quarantine_stat = _validate_private_directory(
            Path(quarantine_root), file_system, "Quarantine root"
        )
        target_path = _absolute(Path(target))
        keeper_path = _absolute(Path(keeper))
        target_resolved = _validate_allowed_path(target_path, roots, file_system)
        keeper_resolved = _validate_allowed_path(keeper_path, roots, file_system)
        if _is_within(target_resolved, [quarantine_resolved]) or _is_within(keeper_resolved, [quarantine_resolved]):
            raise _SafetyFailure(FailureCode.INVALID_PLAN, "Target and keeper must not be inside quarantine")

        with _open_verified_pair(target_path, keeper_path, file_system, roots) as (target_handle, keeper_handle):
            target_opened, keeper_opened = _compare_open_files(target_handle, keeper_handle)
        if not file_system.same_volume(target_opened.identity, quarantine_resolved):
            raise _SafetyFailure(
                FailureCode.QUARANTINE_VOLUME_MISMATCH,
                "Quarantine must be on the same volume as the target",
                quarantine_resolved,
            )
        target_proof = _proof_from_opened(target_path, target_resolved, target_opened)
        keeper_proof = _proof_from_opened(keeper_path, keeper_resolved, keeper_opened)
        chosen_plan_id = _validate_plan_id(plan_id) if plan_id is not None else str(uuid.uuid4())
        plan = OperationPlan(
            plan_id=chosen_plan_id,
            created_ns=time.time_ns(),
            allowed_roots=tuple(str(root) for root in roots),
            quarantine_root=str(quarantine_resolved),
            target=target_proof,
            keeper=keeper_proof,
        )
        _validate_plan_shape(plan)
        if file_system.lexists(plan.quarantine_directory):
            raise _SafetyFailure(
                FailureCode.QUARANTINE_CONFLICT,
                "The plan's quarantine directory already exists",
                plan.quarantine_directory,
            )
        return PlanBuildResult(plan=plan, failure=None)
    except _SafetyFailure as error:
        return PlanBuildResult(plan=None, failure=_failure_from_exception(error))
    except (OSError, RuntimeError) as error:
        code = FailureCode.IO_ERROR
        if isinstance(error, FileNotFoundError):
            if not os.path.lexists(str(target)):
                code = FailureCode.MISSING_TARGET
            elif not os.path.lexists(str(keeper)):
                code = FailureCode.MISSING_KEEPER
        return PlanBuildResult(plan=None, failure=Failure(code=code, message=str(error)))
    except ValueError as error:
        return PlanBuildResult(plan=None, failure=Failure(code=FailureCode.INVALID_PLAN, message=str(error)))


class AppendOnlyJournal:
    """An fsynced JSONL journal.

    A final partial line is ignored during replay because process termination can interrupt a write.
    Any malformed complete line is considered corruption and fails closed.
    """

    def __init__(self, path: Path, fs: Optional[FileSystemAdapter] = None):
        self.path = _absolute(Path(path))
        self.fs = fs or platform_file_system()
        self._lock = threading.Lock()

    def _validate_parent(self) -> None:
        try:
            _validate_private_directory(
                self.path.parent,
                self.fs,
                "Journal parent",
            )
        except _SafetyFailure as error:
            raise JournalError(error.message) from error

    def _validate_existing_path(self) -> None:
        if not self.fs.lexists(self.path):
            return
        journal_stat = self.fs.lstat(self.path)
        if _entry_type(journal_stat) is not EntryType.REGULAR_FILE or _is_reparse_point(journal_stat):
            raise JournalError("Journal path is not a plain regular file")
        if int(getattr(journal_stat, "st_nlink", 0)) != 1:
            raise JournalError("Journal must have exactly one filesystem link")
        if os.name == "posix":
            if int(journal_stat.st_uid) != int(os.geteuid()):
                raise JournalError("Journal is not owned by the current user")
            if stat.S_IMODE(journal_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
                raise JournalError("Journal is writable by group or other users")

    @staticmethod
    def _same_version(
        before: os.stat_result,
        after: os.stat_result,
    ) -> bool:
        return (
            int(before.st_dev),
            int(before.st_ino),
            _entry_type(before),
            int(before.st_size),
            _mtime_ns(before),
            int(getattr(before, "st_nlink", 0)),
        ) == (
            int(after.st_dev),
            int(after.st_ino),
            _entry_type(after),
            int(after.st_size),
            _mtime_ns(after),
            int(getattr(after, "st_nlink", 0)),
        )

    @staticmethod
    def _parse_record(line: bytes) -> JournalEvent:
        try:
            text = line.decode("utf-8")
            value = strict_bounded_json_loads(
                text,
                limits=JOURNAL_RECORD_JSON_LIMITS,
                label="safe-action journal record",
            )
            _require_exact_keys(
                value,
                {
                    "schema_version",
                    "event_id",
                    "timestamp_ns",
                    "plan_id",
                    "plan_fingerprint",
                    "event",
                    "details",
                },
                "journal event",
            )
            if _require_int(value["schema_version"], "schema_version") != JOURNAL_SCHEMA_VERSION:
                raise ValueError("Unsupported journal schema")
            return JournalEvent(
                event_id=_require_str(value["event_id"], "event_id"),
                timestamp_ns=_require_nonnegative_int(value["timestamp_ns"], "timestamp_ns"),
                plan_id=_validate_plan_id(_require_str(value["plan_id"], "plan_id")),
                plan_fingerprint=_require_str(value["plan_fingerprint"], "plan_fingerprint"),
                event=JournalEventType(value["event"]),
                details=_require_dict(value["details"], "details"),
            )
        except MemoryError as error:
            raise JournalError("Journal record exceeded the JSON parser memory budget") from error
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ) as error:
            raise JournalError("Invalid journal record: {}".format(error))

    def _read_locked(
        self,
        *,
        plan_id: Optional[str] = None,
        collect: bool = True,
    ) -> Tuple[List[JournalEvent], int, bool]:
        self._validate_parent()
        self._validate_existing_path()
        if not self.fs.lexists(self.path):
            return [], 0, False
        path_before = self.fs.lstat(self.path)
        if int(path_before.st_size) > MAX_JOURNAL_BYTES:
            raise JournalError("Journal exceeds the {} byte safety limit".format(MAX_JOURNAL_BYTES))
        events: List[JournalEvent] = []
        event_count = 0
        bytes_read = 0
        has_partial_record = False
        try:
            with self.fs.open_readonly(self.path) as stream:
                handle_before = os.fstat(stream.fileno())
                if not self._same_version(path_before, handle_before):
                    raise JournalError("Journal changed while it was being opened")
                while True:
                    line = stream.readline(MAX_JOURNAL_LINE_BYTES + 1)
                    if not line:
                        break
                    bytes_read += len(line)
                    if bytes_read > MAX_JOURNAL_BYTES:
                        raise JournalError("Journal exceeds the {} byte safety limit".format(MAX_JOURNAL_BYTES))
                    if len(line) > MAX_JOURNAL_LINE_BYTES:
                        raise JournalError(
                            "Journal record exceeds the {} byte line limit".format(MAX_JOURNAL_LINE_BYTES)
                        )
                    if not line.endswith(b"\n"):
                        # Only a bounded final partial write is ignored.
                        if stream.read(1):
                            raise JournalError(
                                "Journal record exceeds the {} byte line limit".format(MAX_JOURNAL_LINE_BYTES)
                            )
                        has_partial_record = True
                        break
                    event_count += 1
                    if event_count > MAX_JOURNAL_EVENTS:
                        raise JournalError("Journal exceeds the {} event safety limit".format(MAX_JOURNAL_EVENTS))
                    event = self._parse_record(line)
                    if collect and (plan_id is None or event.plan_id == plan_id):
                        events.append(event)
                handle_after = os.fstat(stream.fileno())
                if not self._same_version(handle_before, handle_after):
                    raise JournalError("Journal changed while it was being read")
            path_after = self.fs.lstat(self.path)
            if not self._same_version(path_before, path_after):
                raise JournalError("Journal path changed while it was read")
        except JournalError:
            raise
        except OSError as error:
            raise JournalError(str(error))
        return events, event_count, has_partial_record

    def append(
        self,
        plan: OperationPlan,
        event: JournalEventType,
        details: Optional[Dict[str, Any]] = None,
    ) -> JournalEvent:
        record = JournalEvent(
            event_id=str(uuid.uuid4()),
            timestamp_ns=time.time_ns(),
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            event=event,
            details=dict(details or {}),
        )
        payload = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "event_id": record.event_id,
            "timestamp_ns": record.timestamp_ns,
            "plan_id": record.plan_id,
            "plan_fingerprint": record.plan_fingerprint,
            "event": record.event.value,
            "details": record.details,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise JournalError("Journal record exceeds the {} byte line limit".format(MAX_JOURNAL_LINE_BYTES))
        with self._lock:
            _, event_count, has_partial_record = self._read_locked(collect=False)
            if has_partial_record:
                raise JournalError("Journal ends with a partial record and cannot be appended safely")
            if event_count >= MAX_JOURNAL_EVENTS:
                raise JournalError("Journal exceeds the {} event safety limit".format(MAX_JOURNAL_EVENTS))
            parent = self.path.parent
            existed = self.fs.lexists(self.path)
            existing_stat = self.fs.lstat(self.path) if existed else None
            existing_size = int(existing_stat.st_size) if existing_stat is not None else 0
            if existing_size + len(encoded) > MAX_JOURNAL_BYTES:
                raise JournalError("Journal exceeds the {} byte safety limit".format(MAX_JOURNAL_BYTES))
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
            if existed:
                # Do not recreate a journal that disappeared after its stable
                # replay.  Recreating it would silently discard the recovery
                # history that was just used to authorize this transition.
                pass
            else:
                # Reserve a new journal name atomically.  A plain O_CREAT open
                # could append to an unrelated file that appeared after the
                # lexists() observation.
                flags |= os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(str(self.path), flags, 0o600)
                try:
                    opened_stat = os.fstat(fd)
                    current_stat = self.fs.lstat(self.path)
                    if (
                        _entry_type(opened_stat) is not EntryType.REGULAR_FILE
                        or _is_reparse_point(opened_stat)
                        or _entry_type(current_stat) is not EntryType.REGULAR_FILE
                        or _is_reparse_point(current_stat)
                        or int(getattr(opened_stat, "st_nlink", 0)) != 1
                        or int(getattr(current_stat, "st_nlink", 0)) != 1
                        or int(opened_stat.st_dev) != int(current_stat.st_dev)
                        or int(opened_stat.st_ino) != int(current_stat.st_ino)
                        or int(opened_stat.st_size) + len(encoded) > MAX_JOURNAL_BYTES
                    ):
                        raise OSError("Journal path or size changed before append")
                    if existing_stat is not None and not self._same_version(
                        existing_stat,
                        opened_stat,
                    ):
                        raise OSError("Journal changed after its stable replay")
                    if existing_stat is None and int(opened_stat.st_size) != 0:
                        raise OSError("New journal was not created empty")
                    written = os.write(fd, encoded)
                    if written != len(encoded):
                        raise OSError("Short write while appending the action journal")
                    os.fsync(fd)
                    finished_stat = os.fstat(fd)
                    current_after = self.fs.lstat(self.path)
                    if (
                        _entry_type(finished_stat) is not EntryType.REGULAR_FILE
                        or _is_reparse_point(finished_stat)
                        or _entry_type(current_after) is not EntryType.REGULAR_FILE
                        or _is_reparse_point(current_after)
                        or int(getattr(finished_stat, "st_nlink", 0)) != 1
                        or int(getattr(current_after, "st_nlink", 0)) != 1
                        or int(finished_stat.st_dev) != int(current_after.st_dev)
                        or int(finished_stat.st_ino) != int(current_after.st_ino)
                        or int(finished_stat.st_size) != existing_size + len(encoded)
                        or int(current_after.st_size) != int(finished_stat.st_size)
                    ):
                        raise OSError("Journal path changed during append")
                finally:
                    os.close(fd)
                if not existed:
                    self.fs.fsync_directory(parent)
            except OSError as error:
                raise JournalError(str(error))
        return record

    def ensure_capacity(
        self,
        *,
        additional_events: int,
        additional_bytes: int,
    ) -> None:
        """Fail before mutation unless the bounded journal has reserved space."""

        if (
            isinstance(additional_events, bool)
            or not isinstance(additional_events, int)
            or additional_events < 0
            or isinstance(additional_bytes, bool)
            or not isinstance(additional_bytes, int)
            or additional_bytes < 0
        ):
            raise ValueError("Journal capacity reservations must be non-negative integers")
        with self._lock:
            _, event_count, has_partial_record = self._read_locked(collect=False)
            if has_partial_record:
                raise JournalError("Journal ends with a partial record and cannot reserve recovery capacity")
            current_size = int(self.fs.lstat(self.path).st_size) if self.fs.lexists(self.path) else 0
            if event_count + additional_events > MAX_JOURNAL_EVENTS:
                raise JournalError("Journal lacks capacity for {} required recovery events".format(additional_events))
            if current_size + additional_bytes > MAX_JOURNAL_BYTES:
                raise JournalError("Journal lacks capacity for {} required recovery bytes".format(additional_bytes))

    def read(self) -> List[JournalEvent]:
        with self._lock:
            events, _, _ = self._read_locked()
            return events

    def events_for(self, plan_id: str) -> List[JournalEvent]:
        with self._lock:
            events, _, _ = self._read_locked(plan_id=plan_id)
            return events


class SafeActionExecutor:
    def __init__(self, journal: AppendOnlyJournal, fs: Optional[FileSystemAdapter] = None):
        self.journal = journal
        self.fs = fs or platform_file_system()

    def _result(
        self,
        plan: OperationPlan,
        state: ActionState,
        code: FailureCode,
        message: str,
        changed: bool = False,
    ) -> ActionResult:
        return ActionResult(
            plan_id=plan.plan_id,
            state=state,
            code=code,
            message=message,
            changed=changed,
            quarantine_path=str(plan.quarantine_path),
        )

    def _roots(self, plan: OperationPlan) -> Tuple[Path, ...]:
        return tuple(Path(root) for root in plan.allowed_roots)

    def _validate_plan(self, plan: OperationPlan) -> None:
        try:
            _validate_plan_shape(plan)
        except ValueError as error:
            raise _SafetyFailure(FailureCode.INVALID_PLAN, str(error))
        for root in self._roots(plan):
            current, _ = _validate_directory(root, self.fs, "Allowed root")
            if _normalized(current) != _normalized(root):
                raise _SafetyFailure(FailureCode.PATH_CHANGED, "An allowed root now resolves elsewhere", root)
        quarantine, quarantine_stat = _validate_private_directory(
            Path(plan.quarantine_root),
            self.fs,
            "Quarantine root",
        )
        if _normalized(quarantine) != _normalized(Path(plan.quarantine_root)):
            raise _SafetyFailure(
                FailureCode.PATH_CHANGED,
                "The quarantine root now resolves elsewhere",
                Path(plan.quarantine_root),
            )
        if not self.fs.same_volume(plan.target.identity, quarantine):
            raise _SafetyFailure(
                FailureCode.QUARANTINE_VOLUME_MISMATCH,
                "Quarantine is no longer on the target's volume",
                quarantine,
            )
        try:
            events = self.journal.events_for(plan.plan_id)
        except JournalError as error:
            raise _SafetyFailure(FailureCode.JOURNAL_CORRUPT, str(error))
        if any(event.plan_fingerprint != plan.fingerprint for event in events):
            raise _SafetyFailure(FailureCode.INVALID_PLAN, "Journal history belongs to a different operation plan")

    def _append(self, plan: OperationPlan, event: JournalEventType, **details: Any) -> None:
        try:
            self.journal.append(plan, event, details)
        except JournalError as error:
            raise _SafetyFailure(FailureCode.JOURNAL_ERROR, str(error))

    def _ensure_journal_capacity(self, events: int) -> None:
        try:
            self.journal.ensure_capacity(
                additional_events=events,
                additional_bytes=events * MAX_JOURNAL_LINE_BYTES,
            )
        except JournalError as error:
            raise _SafetyFailure(FailureCode.JOURNAL_ERROR, str(error)) from error

    def _append_failure(self, plan: OperationPlan, error: _SafetyFailure) -> None:
        try:
            # Failure diagnostics are non-authoritative.  Never let repeated
            # failures consume the space reserved for restore/finalize.
            self.journal.ensure_capacity(
                additional_events=RECOVERY_JOURNAL_RESERVE_EVENTS + 1,
                additional_bytes=(RECOVERY_JOURNAL_RESERVE_EVENTS + 1) * MAX_JOURNAL_LINE_BYTES,
            )
            self.journal.append(
                plan,
                JournalEventType.FAILED,
                {
                    "code": error.code.value,
                    "message": error.message,
                    "path": str(error.path) if error.path is not None else None,
                },
            )
        except (JournalError, OSError):
            pass

    def _plan_events(self, plan: OperationPlan) -> List[JournalEvent]:
        try:
            return self.journal.events_for(plan.plan_id)
        except JournalError as error:
            raise _SafetyFailure(FailureCode.JOURNAL_CORRUPT, str(error))

    def _ensure_quarantine_directory(self, plan: OperationPlan) -> None:
        directory = plan.quarantine_directory
        if self.fs.lexists(directory):
            try:
                _validate_private_directory(
                    directory,
                    self.fs,
                    "Plan quarantine directory",
                )
            except _SafetyFailure as error:
                raise _SafetyFailure(
                    FailureCode.QUARANTINE_CONFLICT,
                    error.message,
                    directory,
                )
            expected_entries = {"payload", "finalizing"}
            unexpected = [entry.name for entry in os.scandir(str(directory)) if entry.name not in expected_entries]
            if unexpected:
                raise _SafetyFailure(
                    FailureCode.QUARANTINE_CONFLICT,
                    "Plan quarantine directory contains unexpected entries",
                    directory,
                )
            return
        self.fs.make_directory(directory)
        _validate_private_directory(
            directory,
            self.fs,
            "Plan quarantine directory",
        )

    def _verify_staged(self, plan: OperationPlan, require_keeper: bool) -> None:
        quarantine_path = plan.quarantine_path
        if require_keeper:
            with _open_verified_pair(
                quarantine_path,
                Path(plan.keeper.path),
                self.fs,
                roots=None,
                expected_target=plan.target,
                expected_keeper=plan.keeper,
                strict_target_metadata=False,
            ):
                pass
            keeper_resolved = _validate_allowed_path(Path(plan.keeper.path), self._roots(plan), self.fs)
            if _normalized(keeper_resolved) != _normalized(Path(plan.keeper.resolved_path)):
                raise _SafetyFailure(FailureCode.PATH_CHANGED, "Keeper path now resolves elsewhere")
            return

        _ensure_no_link_components(quarantine_path, self.fs)
        quarantine_lstat = self.fs.lstat(quarantine_path)
        if _is_reparse_point(quarantine_lstat):
            raise _SafetyFailure(FailureCode.UNSUPPORTED_TYPE, "Staged payload is a reparse point")
        with self.fs.open_readonly(quarantine_path) as target_handle:
            before = os.fstat(target_handle.fileno())
            generation_before = get_file_generation_token_from_fd(
                target_handle.fileno(),
                stat_result=before,
            )
            target_handle.seek(0)
            digest = hashlib.sha256()
            while True:
                chunk = target_handle.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(target_handle.fileno())
            opened = _opened_proof(
                target_handle,
                digest.hexdigest(),
                before,
                after,
                generation_before,
            )
        _compare_lstat_to_open(quarantine_lstat, opened, quarantine_path)
        _check_expected(opened, plan.target, False)

    def _recover_existing_stage(self, plan: OperationPlan) -> ActionResult:
        self._verify_staged(plan, require_keeper=True)
        if any(
            event.event in {JournalEventType.STAGED, JournalEventType.STAGED_RECOVERED}
            for event in self._plan_events(plan)
        ):
            return self._result(plan, ActionState.STAGED, FailureCode.NONE, "Target is already safely staged")
        self._append(plan, JournalEventType.STAGED_RECOVERED, quarantine_path=str(plan.quarantine_path))
        return self._result(plan, ActionState.STAGED, FailureCode.NONE, "Target is already safely staged")

    def stage(self, plan: OperationPlan) -> ActionResult:
        """Revalidate target and keeper, then atomically move target to quarantine."""

        try:
            self._validate_plan(plan)
            target_path = Path(plan.target.path)
            quarantine_path = plan.quarantine_path
            target_exists = self.fs.lexists(target_path)
            quarantine_exists = self.fs.lexists(quarantine_path)
            if target_exists and quarantine_exists:
                raise _SafetyFailure(
                    FailureCode.TARGET_CONFLICT,
                    "Both the original target and staged payload exist",
                    target_path,
                )
            if quarantine_exists:
                return self._recover_existing_stage(plan)
            if not target_exists:
                raise _SafetyFailure(FailureCode.MISSING_TARGET, "Target no longer exists", target_path)

            roots = self._roots(plan)
            self._ensure_journal_capacity(LIFECYCLE_JOURNAL_RESERVE_EVENTS)
            with _open_verified_pair(
                target_path,
                Path(plan.keeper.path),
                self.fs,
                roots=roots,
                expected_target=plan.target,
                expected_keeper=plan.keeper,
            ) as (target_handle, keeper_handle):
                self._append(plan, JournalEventType.PREPARED, target=str(target_path))
                self._ensure_quarantine_directory(plan)
                if self.fs.lexists(quarantine_path):
                    raise _SafetyFailure(
                        FailureCode.QUARANTINE_CONFLICT,
                        "Quarantine payload already exists",
                        quarantine_path,
                    )
                try:
                    self.fs.rename_no_replace(target_path, quarantine_path)
                except FileExistsError:
                    raise _SafetyFailure(
                        FailureCode.QUARANTINE_CONFLICT,
                        "Quarantine payload appeared during staging",
                        quarantine_path,
                    )
                self.fs.fsync_directory(target_path.parent)
                self.fs.fsync_directory(plan.quarantine_directory)
                try:
                    quarantine_lstat = self.fs.lstat(quarantine_path)
                    target_opened, keeper_opened = _compare_open_files(target_handle, keeper_handle)
                    _compare_lstat_to_open(quarantine_lstat, target_opened, quarantine_path)
                    _check_expected(target_opened, plan.target, False)
                    _check_expected(keeper_opened, plan.keeper, True)
                except _SafetyFailure:
                    if self.fs.lexists(quarantine_path) and not self.fs.lexists(target_path):
                        self.fs.rename_no_replace(quarantine_path, target_path)
                        self.fs.fsync_directory(target_path.parent)
                        self._append(plan, JournalEventType.ROLLED_BACK, reason="post_stage_verification_failed")
                    raise
            try:
                self._append(plan, JournalEventType.STAGED, quarantine_path=str(quarantine_path))
            except _SafetyFailure as error:
                return self._result(
                    plan,
                    ActionState.STAGED,
                    error.code,
                    "Target is staged, but the journal could not record completion: {}".format(error.message),
                    changed=True,
                )
            if self.fs.lexists(target_path):
                return self._result(
                    plan,
                    ActionState.STAGED,
                    FailureCode.TARGET_CONFLICT,
                    "Target was staged, but a new entry appeared at the original path",
                    changed=True,
                )
            return self._result(
                plan,
                ActionState.STAGED,
                FailureCode.NONE,
                "Target revalidated and moved to quarantine",
                changed=True,
            )
        except _SafetyFailure as error:
            self._append_failure(plan, error)
            return self._result(plan, ActionState.FAILED, error.code, error.message)
        except FileNotFoundError as error:
            failure = _SafetyFailure(
                FailureCode.MISSING_KEEPER, str(error), Path(error.filename) if error.filename else None
            )
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except OSError as error:
            failure = _SafetyFailure(FailureCode.IO_ERROR, str(error), Path(error.filename) if error.filename else None)
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except (TypeError, ValueError) as error:
            failure = _SafetyFailure(FailureCode.INVALID_PLAN, str(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except Exception as error:
            failure = _SafetyFailure(FailureCode.INTERNAL_ERROR, repr(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)

    def restore(self, plan: OperationPlan) -> ActionResult:
        """Restore a staged target without overwriting an occupied original path."""

        try:
            self._validate_plan(plan)
            target_path = Path(plan.target.path)
            quarantine_path = plan.quarantine_path
            target_exists = self.fs.lexists(target_path)
            quarantine_exists = self.fs.lexists(quarantine_path)
            if target_exists and quarantine_exists:
                raise _SafetyFailure(
                    FailureCode.TARGET_CONFLICT,
                    "Restore would overwrite an entry at the original target path",
                    target_path,
                )
            if target_exists:
                with self.fs.open_readonly(target_path) as target_handle:
                    before = os.fstat(target_handle.fileno())
                    generation_before = get_file_generation_token_from_fd(
                        target_handle.fileno(),
                        stat_result=before,
                    )
                    target_handle.seek(0)
                    digest = hashlib.sha256()
                    while True:
                        chunk = target_handle.read(READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
                    after = os.fstat(target_handle.fileno())
                    opened = _opened_proof(
                        target_handle,
                        digest.hexdigest(),
                        before,
                        after,
                        generation_before,
                    )
                _check_expected(opened, plan.target, False)
                events = self._plan_events(plan)
                event_types = {event.event for event in events}
                restore_was_committed = (
                    JournalEventType.RESTORE_PREPARED in event_types
                    and bool(
                        {
                            JournalEventType.STAGED,
                            JournalEventType.STAGED_RECOVERED,
                        }
                        & event_types
                    )
                    and JournalEventType.RESTORED not in event_types
                    and JournalEventType.RESTORED_RECOVERED not in event_types
                )
                if restore_was_committed:
                    self._ensure_journal_capacity(1)
                    self._append(
                        plan,
                        JournalEventType.RESTORED_RECOVERED,
                        target=str(target_path),
                    )
                return self._result(
                    plan,
                    ActionState.RESTORED,
                    FailureCode.NONE,
                    "Target is already restored",
                    changed=False,
                )
            if not quarantine_exists:
                events = self._plan_events(plan)
                if any(
                    event.event in {JournalEventType.FINALIZED, JournalEventType.FINALIZED_RECOVERED}
                    for event in events
                ):
                    raise _SafetyFailure(FailureCode.INVALID_STATE, "A finalized target cannot be restored")
                raise _SafetyFailure(FailureCode.MISSING_TARGET, "Neither target nor staged payload exists")

            self._verify_staged(plan, require_keeper=False)
            _validate_restore_parent(plan.target, self.fs)
            self._ensure_journal_capacity(2)
            self._append(plan, JournalEventType.RESTORE_PREPARED, target=str(target_path))
            self.fs.rename_no_replace(quarantine_path, target_path)
            self.fs.fsync_directory(target_path.parent)
            self.fs.fsync_directory(plan.quarantine_directory)
            target_resolved = _validate_allowed_path(target_path, self._roots(plan), self.fs)
            if _normalized(target_resolved) != _normalized(Path(plan.target.resolved_path)):
                raise _SafetyFailure(FailureCode.PATH_CHANGED, "Restored path resolves elsewhere", target_path)
            with self.fs.open_readonly(target_path) as target_handle:
                before = os.fstat(target_handle.fileno())
                generation_before = get_file_generation_token_from_fd(
                    target_handle.fileno(),
                    stat_result=before,
                )
                target_handle.seek(0)
                digest = hashlib.sha256()
                while True:
                    chunk = target_handle.read(READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(target_handle.fileno())
                opened = _opened_proof(
                    target_handle,
                    digest.hexdigest(),
                    before,
                    after,
                    generation_before,
                )
            _check_expected(opened, plan.target, False)
            self._append(plan, JournalEventType.RESTORED, target=str(target_path))
            return self._result(
                plan,
                ActionState.RESTORED,
                FailureCode.NONE,
                "Staged target restored",
                changed=True,
            )
        except _SafetyFailure as error:
            self._append_failure(plan, error)
            return self._result(plan, ActionState.FAILED, error.code, error.message)
        except FileExistsError:
            error = _SafetyFailure(FailureCode.TARGET_CONFLICT, "Restore target appeared during restore")
            self._append_failure(plan, error)
            return self._result(plan, ActionState.FAILED, error.code, error.message)
        except FileNotFoundError as error:
            failure = _SafetyFailure(FailureCode.MISSING_TARGET, str(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except OSError as error:
            failure = _SafetyFailure(FailureCode.IO_ERROR, str(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except Exception as error:
            failure = _SafetyFailure(FailureCode.INTERNAL_ERROR, repr(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)

    def finalize(self, plan: OperationPlan) -> ActionResult:
        """Permanently unlink a staged payload after revalidating its keeper."""

        try:
            self._validate_plan(plan)
            target_path = Path(plan.target.path)
            quarantine_path = plan.quarantine_path
            finalize_path = plan.finalize_path
            target_exists = self.fs.lexists(target_path)
            quarantine_exists = self.fs.lexists(quarantine_path)
            finalize_exists = self.fs.lexists(finalize_path)
            events = self._plan_events(plan)
            event_types = {event.event for event in events}
            if target_exists and (quarantine_exists or finalize_exists):
                raise _SafetyFailure(
                    FailureCode.TARGET_CONFLICT,
                    "Both original target and staged payload exist",
                    target_path,
                )
            if target_exists and not quarantine_exists and not finalize_exists:
                raise _SafetyFailure(FailureCode.INVALID_STATE, "Target has not been staged", target_path)
            if quarantine_exists and finalize_exists:
                # Native no-replace rename is one atomic operation and cannot
                # legitimately leave both names behind.  Never try to clean up
                # either path here: an lstat-then-unlink recovery would permit
                # a concurrent replacement to redirect the unlink to unrelated
                # data.
                raise _SafetyFailure(
                    FailureCode.QUARANTINE_CONFLICT,
                    "Both staged payload and finalize tombstone exist",
                    finalize_path,
                )

            if not quarantine_exists and not finalize_exists:
                if JournalEventType.FINALIZED in event_types or JournalEventType.FINALIZED_RECOVERED in event_types:
                    return self._result(
                        plan,
                        ActionState.FINALIZED,
                        FailureCode.NONE,
                        "Target is already finalized",
                        changed=False,
                    )
                if JournalEventType.FINALIZE_TOMBSTONED in event_types:
                    keeper_resolved = _validate_allowed_path(Path(plan.keeper.path), self._roots(plan), self.fs)
                    if _normalized(keeper_resolved) != _normalized(Path(plan.keeper.resolved_path)):
                        raise _SafetyFailure(FailureCode.PATH_CHANGED, "Keeper path now resolves elsewhere")
                    with self.fs.open_readonly(Path(plan.keeper.path)) as keeper_handle:
                        before = os.fstat(keeper_handle.fileno())
                        generation_before = get_file_generation_token_from_fd(
                            keeper_handle.fileno(),
                            stat_result=before,
                        )
                        keeper_handle.seek(0)
                        digest = hashlib.sha256()
                        while True:
                            chunk = keeper_handle.read(READ_CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                        after = os.fstat(keeper_handle.fileno())
                        keeper_opened = _opened_proof(
                            keeper_handle,
                            digest.hexdigest(),
                            before,
                            after,
                            generation_before,
                        )
                    _check_expected(keeper_opened, plan.keeper, True)
                    self._append(plan, JournalEventType.FINALIZED_RECOVERED)
                    return self._result(
                        plan,
                        ActionState.FINALIZED,
                        FailureCode.NONE,
                        "Recovered a completed finalize operation",
                        changed=False,
                    )
                raise _SafetyFailure(FailureCode.MISSING_TARGET, "Staged payload is missing without a finalize record")

            staged_path = finalize_path if finalize_exists else quarantine_path
            self._ensure_journal_capacity(3)
            with _open_verified_pair(
                staged_path,
                Path(plan.keeper.path),
                self.fs,
                roots=None,
                expected_target=plan.target,
                expected_keeper=plan.keeper,
                strict_target_metadata=False,
            ) as (target_handle, keeper_handle):
                keeper_resolved = _validate_allowed_path(Path(plan.keeper.path), self._roots(plan), self.fs)
                if _normalized(keeper_resolved) != _normalized(Path(plan.keeper.resolved_path)):
                    raise _SafetyFailure(FailureCode.PATH_CHANGED, "Keeper path now resolves elsewhere")
                if JournalEventType.FINALIZE_PREPARED not in event_types:
                    self._append(
                        plan,
                        JournalEventType.FINALIZE_PREPARED,
                        quarantine_path=str(quarantine_path),
                        finalize_path=str(finalize_path),
                    )
                if staged_path == quarantine_path:
                    try:
                        self.fs.rename_no_replace(quarantine_path, finalize_path)
                    except FileExistsError:
                        raise _SafetyFailure(
                            FailureCode.QUARANTINE_CONFLICT,
                            "Finalize tombstone appeared during finalization",
                            finalize_path,
                        )
                    self.fs.fsync_directory(plan.quarantine_directory)
                    tombstone_stat = self.fs.lstat(finalize_path)
                    target_after_move, keeper_after_move = _compare_open_files(
                        target_handle,
                        keeper_handle,
                    )
                    _compare_lstat_to_open(
                        tombstone_stat,
                        target_after_move,
                        finalize_path,
                    )
                    _check_expected(target_after_move, plan.target, False)
                    _check_expected(keeper_after_move, plan.keeper, True)
                self._append(
                    plan,
                    JournalEventType.FINALIZE_TOMBSTONED,
                    finalize_path=str(finalize_path),
                )
                tombstone_stat = self.fs.lstat(finalize_path)
                target_before_unlink, keeper_before_unlink = _compare_open_files(
                    target_handle,
                    keeper_handle,
                )
                _compare_lstat_to_open(
                    tombstone_stat,
                    target_before_unlink,
                    finalize_path,
                )
                _check_expected(target_before_unlink, plan.target, False)
                _check_expected(keeper_before_unlink, plan.keeper, True)
                self.fs.unlink(finalize_path)
                self.fs.fsync_directory(plan.quarantine_directory)
                _, keeper_after = _compare_open_files(target_handle, keeper_handle)
                _check_expected(keeper_after, plan.keeper, True)
            self._append(plan, JournalEventType.FINALIZED)
            return self._result(
                plan,
                ActionState.FINALIZED,
                FailureCode.NONE,
                "Staged target permanently finalized",
                changed=True,
            )
        except _SafetyFailure as error:
            self._append_failure(plan, error)
            return self._result(plan, ActionState.FAILED, error.code, error.message)
        except FileNotFoundError as error:
            failure = _SafetyFailure(FailureCode.MISSING_KEEPER, str(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except OSError as error:
            failure = _SafetyFailure(FailureCode.IO_ERROR, str(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)
        except Exception as error:
            failure = _SafetyFailure(FailureCode.INTERNAL_ERROR, repr(error))
            self._append_failure(plan, failure)
            return self._result(plan, ActionState.FAILED, failure.code, failure.message)


__all__ = [
    "ActionResult",
    "ActionState",
    "AppendOnlyJournal",
    "EntryType",
    "Failure",
    "FailureCode",
    "FileIdentity",
    "FileProof",
    "FileSystemAdapter",
    "JournalError",
    "JournalEvent",
    "JournalEventType",
    "OperationPlan",
    "PlanBuildResult",
    "PosixFileSystemAdapter",
    "SafeActionExecutor",
    "WindowsFileSystemAdapter",
    "build_operation_plan",
    "cleanup_created_regular_file",
    "platform_file_system",
]
