# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Fail-closed, versioned file content-generation observations.

The public API in this module is intentionally independent from the catalog.
Other verified-freshness consumers can reuse it without mistaking Windows
creation time for a change counter.
"""

import ctypes
import errno
import hashlib
import os
import stat
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityCapability,
    IdentityConfidence,
    IdentityVerdict,
    get_file_identity,
    same_physical_file,
)


class FileGenerationError(OSError):
    """A generation token could not be proven for one plain regular file."""

    def __init__(self, path, operation, cause=None):
        self.path = Path(path)
        self.operation = operation
        self.cause = cause
        if cause is None:
            detail = "file generation is unavailable"
        else:
            detail = "{}: {}".format(type(cause).__name__, cause)
        self.message = "Could not {} for '{}': {}".format(operation, self.path, detail)
        super().__init__(self.message)
        if isinstance(cause, OSError):
            self.errno = cause.errno
            self.filename = str(self.path)

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class FileGenerationToken:
    """Typed generation value with a stable serialization namespace."""

    namespace: str
    value: int
    version: int = 1

    def __post_init__(self):
        if not isinstance(self.namespace, str) or not self.namespace or "\0" in self.namespace:
            raise ValueError("generation namespace must be a non-empty NUL-free string")
        try:
            self.namespace.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("generation namespace must be ASCII") from error
        if type(self.version) is not int or self.version < 1:
            raise ValueError("generation token version must be positive")
        if type(self.value) is not int or self.value < 0:
            raise ValueError("generation token value must be a non-negative integer")

    @property
    def encoded(self) -> bytes:
        return "\0".join(
            (
                "dupeguru-content-generation",
                "v{}".format(self.version),
                self.namespace,
                str(int(self.value)),
            )
        ).encode("ascii")

    @classmethod
    def from_encoded(cls, value: bytes) -> "FileGenerationToken":
        if not isinstance(value, bytes):
            raise ValueError("encoded generation token must be bytes")
        try:
            prefix, version_text, namespace, counter = value.decode("ascii").split("\0")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("encoded generation token has an invalid structure") from error
        if prefix != "dupeguru-content-generation" or not version_text.startswith("v"):
            raise ValueError("encoded generation token has an invalid namespace")
        try:
            token = cls(namespace, int(counter), int(version_text[1:]))
        except (TypeError, ValueError) as error:
            raise ValueError("encoded generation token has invalid fields") from error
        if token.encoded != value:
            raise ValueError("encoded generation token is not canonical")
        return token


def get_file_generation_token(
    path,
    follow_symlinks=False,
    stat_result=None,
    expected_identity: Optional[FileIdentity] = None,
) -> FileGenerationToken:
    """Observe one file generation without following links or reparses.

    POSIX uses ``st_ctime_ns``. Windows binds the file's latest update
    sequence number to the current volume change-journal identifier, using
    the same no-follow handle that validates file type, path metadata, and
    physical identity. There is no timestamp fallback.
    """

    return _get_entry_generation_token(
        path,
        follow_symlinks=follow_symlinks,
        stat_result=stat_result,
        expected_identity=expected_identity,
        allow_directory=False,
    )


def get_entry_generation_token(
    path,
    follow_symlinks=False,
    stat_result=None,
    expected_identity: Optional[FileIdentity] = None,
) -> FileGenerationToken:
    """Observe a regular-file or directory generation without following links."""

    return _get_entry_generation_token(
        path,
        follow_symlinks=follow_symlinks,
        stat_result=stat_result,
        expected_identity=expected_identity,
        allow_directory=True,
    )


def _get_entry_generation_token(
    path,
    *,
    follow_symlinks,
    stat_result,
    expected_identity: Optional[FileIdentity],
    allow_directory: bool,
) -> FileGenerationToken:
    path = Path(path)
    if "\0" in os.fspath(path):
        raise FileGenerationError(path, "validate generation path", ValueError("NUL byte in path"))
    if follow_symlinks:
        raise FileGenerationError(path, "read no-follow generation", ValueError("symlink following is unsafe"))
    if expected_identity is None:
        try:
            expected_identity = get_file_identity(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
            )
        except (FileIdentityError, OSError) as error:
            raise FileGenerationError(path, "identify generation path", error) from error

    if os.name == "nt":
        return _get_windows_generation_token(
            path,
            stat_result,
            expected_identity,
            allow_directory=allow_directory,
        )
    return _get_posix_generation_token(
        path,
        stat_result,
        expected_identity,
        allow_directory=allow_directory,
    )


def get_file_generation_token_from_fd(
    fd: int,
    path=None,
    stat_result=None,
    expected_identity: Optional[FileIdentity] = None,
) -> FileGenerationToken:
    """Observe a generation from an already-open, caller-owned descriptor."""

    return _get_entry_generation_token_from_fd(
        fd,
        path=path,
        stat_result=stat_result,
        expected_identity=expected_identity,
        allow_directory=False,
    )


def get_entry_generation_token_from_fd(
    fd: int,
    path=None,
    stat_result=None,
    expected_identity: Optional[FileIdentity] = None,
) -> FileGenerationToken:
    """Observe a regular-file or directory generation from a caller-owned descriptor."""

    return _get_entry_generation_token_from_fd(
        fd,
        path=path,
        stat_result=stat_result,
        expected_identity=expected_identity,
        allow_directory=True,
    )


def _get_entry_generation_token_from_fd(
    fd: int,
    *,
    path,
    stat_result,
    expected_identity: Optional[FileIdentity],
    allow_directory: bool,
) -> FileGenerationToken:
    if type(fd) is not int or fd < 0:
        raise FileGenerationError(path or "<open file>", "validate generation descriptor", ValueError("invalid fd"))
    path = Path(path) if path is not None else Path("<open file>")
    try:
        stat_result = stat_result or os.fstat(fd)
    except OSError as error:
        raise FileGenerationError(path, "stat open file descriptor", error) from error
    if not (stat.S_ISREG(stat_result.st_mode) or (allow_directory and stat.S_ISDIR(stat_result.st_mode))):
        raise FileGenerationError(
            path,
            "read open-entry generation",
            ValueError("handle is not a supported regular file or directory"),
        )
    if os.name == "nt":
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError as error:
            raise FileGenerationError(path, "resolve Windows file handle", error) from error
        try:
            original_identity = _windows_identity_from_handle(handle, path)
        except FileIdentityError as error:
            raise FileGenerationError(path, "identify open Windows generation handle", error) from error
        if expected_identity is not None:
            comparison = same_physical_file(expected_identity, original_identity)
            if comparison.verdict != IdentityVerdict.SAME:
                raise FileGenerationError(
                    path,
                    "bind open Windows generation handle to file identity",
                    ValueError(comparison.reason),
                )
        reopened = _reopen_windows_generation_handle(handle, path)
        try:
            return _get_windows_generation_from_handle(
                reopened,
                path,
                stat_result,
                original_identity,
                allow_directory=allow_directory,
            )
        finally:
            _close_handle(reopened)

    observed_identity = FileIdentity(
        namespace="posix",
        volume_id=int(stat_result.st_dev),
        file_id=int(stat_result.st_ino),
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )
    if expected_identity is not None:
        comparison = same_physical_file(expected_identity, observed_identity)
        if comparison.verdict != IdentityVerdict.SAME:
            raise FileGenerationError(
                path,
                "bind POSIX ctime to open-file identity",
                ValueError(comparison.reason),
            )
    return FileGenerationToken("posix-ctime-ns", int(stat_result.st_ctime_ns))


def _get_posix_generation_token(
    path: Path,
    stat_result,
    expected_identity: Optional[FileIdentity],
    *,
    allow_directory: bool,
) -> FileGenerationToken:
    try:
        stat_result = stat_result or os.stat(str(path), follow_symlinks=False)
    except OSError as error:
        raise FileGenerationError(path, "stat path for POSIX ctime", error) from error
    if not (stat.S_ISREG(stat_result.st_mode) or (allow_directory and stat.S_ISDIR(stat_result.st_mode))):
        raise FileGenerationError(
            path,
            "read POSIX ctime",
            ValueError("path is not a supported regular file or directory"),
        )
    if expected_identity is not None:
        try:
            observed_identity = get_file_identity(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
            )
        except (FileIdentityError, OSError) as error:
            raise FileGenerationError(path, "bind POSIX ctime to file identity", error) from error
        comparison = same_physical_file(expected_identity, observed_identity)
        if comparison.verdict != IdentityVerdict.SAME:
            raise FileGenerationError(
                path,
                "bind POSIX ctime to file identity",
                ValueError(comparison.reason),
            )
    return FileGenerationToken("posix-ctime-ns", int(stat_result.st_ctime_ns))


if os.name == "nt":
    from ctypes import wintypes

    from core.file_identity import (
        _FILE_FLAG_BACKUP_SEMANTICS,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_DELETE,
        _FILE_SHARE_READ,
        _INVALID_HANDLE_VALUE,
        _OPEN_EXISTING,
        _close_handle,
        _create_file,
        _get_file_information_ex,
        _windows_identity_from_handle,
        windows_extended_path,
    )

    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_READ_DATA = 0x0001
    _FILE_BASIC_INFO_CLASS = 0
    _FILE_STANDARD_INFO_CLASS = 1
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _WINDOWS_EPOCH_100NS = 116444736000000000
    _FSCTL_READ_FILE_USN_DATA = 0x000900EB
    _FSCTL_QUERY_USN_JOURNAL = 0x000900F4
    _USN_RECORD_MIN_MAJOR_VERSION = 2
    _USN_RECORD_MAX_MAJOR_VERSION = 3
    _USN_RECORD_BUFFER_BYTES = 4096
    _WINDOWS_USN_TOKEN_VERSION = 2
    _WINDOWS_USN_TOKEN_NAMESPACE = "windows-usn-journal-file"
    _WINDOWS_DIRECTORY_USN_TOKEN_NAMESPACE = "windows-usn-journal-directory-tree"
    _MAX_WINDOWS_DIRECTORY_GENERATION_ENTRIES = 1_000_000
    _MAX_WINDOWS_DIRECTORY_GENERATION_NAME_BYTES = 256 * 1024 * 1024
    _MAX_WINDOWS_DIRECTORY_GENERATION_METADATA_BYTES = 512 * 1024 * 1024
    _MAX_WINDOWS_DIRECTORY_GENERATION_DEPTH = 256
    _MAX_WINDOWS_DIRECTORY_GENERATION_SECONDS = 300.0

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    class _FILE_STANDARD_INFO(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BYTE),
            ("Directory", wintypes.BYTE),
        ]

    class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _READ_FILE_USN_DATA(ctypes.Structure):
        _fields_ = [
            ("MinMajorVersion", wintypes.WORD),
            ("MaxMajorVersion", wintypes.WORD),
        ]

    class _USN_JOURNAL_DATA_V0(ctypes.Structure):
        _fields_ = [
            ("UsnJournalID", ctypes.c_ulonglong),
            ("FirstUsn", ctypes.c_longlong),
            ("NextUsn", ctypes.c_longlong),
            ("LowestValidUsn", ctypes.c_longlong),
            ("MaxUsn", ctypes.c_longlong),
            ("MaximumSize", ctypes.c_ulonglong),
            ("AllocationDelta", ctypes.c_ulonglong),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _device_io_control = _kernel32.DeviceIoControl
    _device_io_control.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _device_io_control.restype = wintypes.BOOL

    _reopen_file = getattr(_kernel32, "ReOpenFile", None)
    if _reopen_file is not None:
        _reopen_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        _reopen_file.restype = wintypes.HANDLE


def _query_windows_handle(handle, info_class: int, info, path: Path, operation: str) -> None:
    if _get_file_information_ex is None:
        raise FileGenerationError(path, operation, NotImplementedError("GetFileInformationByHandleEx is unavailable"))
    if not _get_file_information_ex(handle, info_class, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileGenerationError(path, operation, error)


def _open_windows_generation_handle(path: Path):
    handle = _create_file(
        windows_extended_path(path),
        _FILE_READ_ATTRIBUTES | _FILE_READ_DATA,
        _FILE_SHARE_READ | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileGenerationError(path, "open no-follow Windows generation handle", error)
    return handle


def _reopen_windows_generation_handle(handle, path: Path):
    """Lease the same Windows object while denying concurrent writers."""

    if _reopen_file is None:
        raise FileGenerationError(
            path,
            "reopen Windows generation handle without write sharing",
            NotImplementedError("ReOpenFile is unavailable"),
        )
    reopened = _reopen_file(
        handle,
        _FILE_READ_ATTRIBUTES | _FILE_READ_DATA,
        _FILE_SHARE_READ | _FILE_SHARE_DELETE,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    if reopened == _INVALID_HANDLE_VALUE:
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileGenerationError(
            path,
            "reopen Windows generation handle without write sharing",
            error,
        )
    return reopened


def _query_windows_journal_id(handle, path: Path) -> int:
    journal = _USN_JOURNAL_DATA_V0()
    returned = wintypes.DWORD()
    if not _device_io_control(
        handle,
        _FSCTL_QUERY_USN_JOURNAL,
        None,
        0,
        ctypes.byref(journal),
        ctypes.sizeof(journal),
        ctypes.byref(returned),
        None,
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileGenerationError(path, "query Windows USN journal", error)
    if int(returned.value) < ctypes.sizeof(journal):
        raise FileGenerationError(
            path,
            "query Windows USN journal",
            ValueError("USN journal response is truncated"),
        )
    journal_id = int(journal.UsnJournalID)
    if journal_id <= 0:
        raise FileGenerationError(
            path,
            "query Windows USN journal",
            ValueError("USN journal has no usable identifier"),
        )
    if int(journal.NextUsn) < 0 or int(journal.MaxUsn) <= 0:
        raise FileGenerationError(
            path,
            "query Windows USN journal",
            ValueError("USN journal counters are invalid"),
        )
    return journal_id


def _parse_windows_file_usn(payload: bytes, path: Path) -> int:
    if len(payload) < 8:
        raise FileGenerationError(
            path,
            "parse Windows file USN",
            ValueError("USN record header is truncated"),
        )
    record_length = int.from_bytes(payload[0:4], "little", signed=False)
    major_version = int.from_bytes(payload[4:6], "little", signed=False)
    if record_length != len(payload):
        raise FileGenerationError(
            path,
            "parse Windows file USN",
            ValueError("USN record length is inconsistent"),
        )
    if major_version == 2:
        minimum_length = 60
        usn_offset = 24
    elif major_version == 3:
        minimum_length = 76
        usn_offset = 40
    else:
        raise FileGenerationError(
            path,
            "parse Windows file USN",
            ValueError("USN record version is unsupported"),
        )
    if len(payload) < minimum_length:
        raise FileGenerationError(
            path,
            "parse Windows file USN",
            ValueError("USN record body is truncated"),
        )
    file_usn = int.from_bytes(payload[usn_offset : usn_offset + 8], "little", signed=True)
    # Zero is a valid baseline for a pre-existing entry which has not received
    # a record since the current journal was created. The positive journal ID
    # namespaces that baseline; any later journaled mutation advances the USN.
    if file_usn < 0:
        raise FileGenerationError(
            path,
            "parse Windows file USN",
            ValueError("file USN is negative"),
        )
    return file_usn


def _query_windows_file_usn(handle, path: Path) -> int:
    supported = _READ_FILE_USN_DATA(
        _USN_RECORD_MIN_MAJOR_VERSION,
        _USN_RECORD_MAX_MAJOR_VERSION,
    )
    buffer = (ctypes.c_ubyte * _USN_RECORD_BUFFER_BYTES)()
    returned = wintypes.DWORD()
    if not _device_io_control(
        handle,
        _FSCTL_READ_FILE_USN_DATA,
        ctypes.byref(supported),
        ctypes.sizeof(supported),
        ctypes.byref(buffer),
        ctypes.sizeof(buffer),
        ctypes.byref(returned),
        None,
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileGenerationError(path, "query Windows file USN", error)
    byte_count = int(returned.value)
    if byte_count <= 0 or byte_count > ctypes.sizeof(buffer):
        raise FileGenerationError(
            path,
            "query Windows file USN",
            ValueError("USN record byte count is invalid"),
        )
    return _parse_windows_file_usn(bytes(buffer[:byte_count]), path)


def _validate_windows_generation_handle(
    handle,
    path: Path,
    stat_result,
    expected_identity: Optional[FileIdentity],
    *,
    allow_directory: bool,
) -> tuple[FileIdentity, int, int]:
    tag_info = _FILE_ATTRIBUTE_TAG_INFO()
    _query_windows_handle(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        tag_info,
        path,
        "query Windows reparse attributes",
    )
    if int(tag_info.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise FileGenerationError(
            path,
            "read Windows USN generation",
            OSError(errno.ELOOP, "path is a reparse point", str(path)),
        )

    standard_info = _FILE_STANDARD_INFO()
    _query_windows_handle(
        handle,
        _FILE_STANDARD_INFO_CLASS,
        standard_info,
        path,
        "query Windows standard file information",
    )
    expected_directory = stat.S_ISDIR(stat_result.st_mode)
    if bool(standard_info.Directory) != expected_directory:
        raise FileGenerationError(
            path,
            "read Windows USN generation",
            ValueError("handle entry type does not match path metadata"),
        )
    if expected_directory and not allow_directory:
        raise FileGenerationError(path, "read Windows USN generation", ValueError("path is a directory"))
    handle_links = int(standard_info.NumberOfLinks)
    if not expected_directory and int(standard_info.EndOfFile) != int(stat_result.st_size):
        raise FileGenerationError(
            path,
            "bind Windows USN generation to path metadata",
            ValueError("file size changed before handle validation"),
        )

    basic_info = _FILE_BASIC_INFO()
    _query_windows_handle(
        handle,
        _FILE_BASIC_INFO_CLASS,
        basic_info,
        path,
        "query Windows FILE_BASIC_INFO",
    )
    if int(basic_info.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise FileGenerationError(
            path,
            "read Windows USN generation",
            OSError(errno.ELOOP, "path is a reparse point", str(path)),
        )
    handle_mtime_ns = (int(basic_info.LastWriteTime) - _WINDOWS_EPOCH_100NS) * 100
    if not expected_directory and handle_mtime_ns != int(stat_result.st_mtime_ns):
        raise FileGenerationError(
            path,
            "bind Windows USN generation to path metadata",
            ValueError("file mtime changed before handle validation"),
        )

    try:
        handle_identity = _windows_identity_from_handle(handle, path)
    except FileIdentityError as error:
        raise FileGenerationError(path, "bind Windows USN generation to file identity", error) from error
    if expected_identity is not None:
        comparison = same_physical_file(expected_identity, handle_identity)
        if comparison.verdict != IdentityVerdict.SAME:
            raise FileGenerationError(
                path,
                "bind Windows USN generation to file identity",
                ValueError(comparison.reason),
            )
    return handle_identity, int(basic_info.ChangeTime), handle_links


@dataclass
class _WindowsDirectoryGenerationBudget:
    deadline: float
    entries: int = 0
    name_bytes: int = 0
    metadata_bytes: int = 0

    @classmethod
    def start(cls) -> "_WindowsDirectoryGenerationBudget":
        return cls(time.monotonic() + _MAX_WINDOWS_DIRECTORY_GENERATION_SECONDS)

    def claim(self, path: Path, name: bytes, depth: int) -> None:
        if depth > _MAX_WINDOWS_DIRECTORY_GENERATION_DEPTH:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("directory depth limit exceeded"),
            )
        if self.entries >= _MAX_WINDOWS_DIRECTORY_GENERATION_ENTRIES:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("directory entry limit exceeded"),
            )
        next_name_bytes = self.name_bytes + len(name)
        if next_name_bytes > _MAX_WINDOWS_DIRECTORY_GENERATION_NAME_BYTES:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("directory name-byte limit exceeded"),
            )
        next_metadata_bytes = self.metadata_bytes + len(name) + 256
        if next_metadata_bytes > _MAX_WINDOWS_DIRECTORY_GENERATION_METADATA_BYTES:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("directory metadata-byte limit exceeded"),
            )
        if time.monotonic() > self.deadline:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                TimeoutError("directory observation deadline exceeded"),
            )
        self.entries += 1
        self.name_bytes = next_name_bytes
        self.metadata_bytes = next_metadata_bytes


def _windows_tree_hash(fields) -> bytes:
    result = hashlib.sha256()
    for field in fields:
        result.update(len(field).to_bytes(8, "big"))
        result.update(field)
    return result.digest()


def _require_high_confidence_windows_tree_identity(
    identity: FileIdentity,
    path: Path,
) -> bytes:
    if (
        identity.namespace != "windows"
        or identity.capability != IdentityCapability.WINDOWS_FILE_ID_128
        or identity.confidence != IdentityConfidence.HIGH
        or int(identity.volume_id) <= 0
        or not isinstance(identity.file_id, bytes)
        or len(identity.file_id) != 16
        or not any(identity.file_id)
    ):
        raise FileGenerationError(
            path,
            "identify Windows directory tree entry",
            ValueError("high-confidence 128-bit file identity is unavailable"),
        )
    return identity.file_id


def _assert_stable_windows_tree_observation(
    path: Path,
    first_identity: FileIdentity,
    second_identity: FileIdentity,
    journal_before: int,
    journal_after: int,
    file_usn_before: int,
    file_usn_after: int,
    change_time_before: int,
    change_time_after: int,
    links_before: int,
    links_after: int,
) -> None:
    if first_identity != second_identity:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            ValueError("entry identity changed during observation"),
        )
    if journal_before != journal_after:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            ValueError("USN journal identifier changed during observation"),
        )
    if file_usn_before != file_usn_after:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            ValueError("entry USN changed during observation"),
        )
    if change_time_before != change_time_after:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            ValueError("entry ChangeTime changed during observation"),
        )
    if links_before != links_after:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            ValueError("entry link count changed during observation"),
        )


def _windows_directory_children_digest(
    path: Path,
    budget: _WindowsDirectoryGenerationBudget,
    depth: int,
) -> bytes:
    child_records = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name.encode("utf-16-le", "surrogatepass")
                child_path = path.joinpath(entry.name)
                budget.claim(child_path, name, depth + 1)
                child_records.append(
                    _windows_tree_entry_digest(
                        child_path,
                        name,
                        budget,
                        depth + 1,
                    )
                )
        if time.monotonic() > budget.deadline:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                TimeoutError("directory observation deadline exceeded"),
            )
        return _windows_tree_hash((b"directory-children", *sorted(child_records)))
    except FileGenerationError:
        raise
    except (MemoryError, OSError, RecursionError, UnicodeError) as error:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            error,
        ) from error


def _windows_tree_entry_digest(
    path: Path,
    name: bytes,
    budget: _WindowsDirectoryGenerationBudget,
    depth: int,
) -> bytes:
    try:
        entry_stat = os.lstat(path)
        expected_identity = get_file_identity(
            path,
            follow_symlinks=False,
            stat_result=entry_stat,
        )
    except (FileIdentityError, OSError) as error:
        raise FileGenerationError(
            path,
            "open Windows directory tree entry",
            error,
        ) from error

    handle = _open_windows_generation_handle(path)
    try:
        identity, change_time_before, links_before = _validate_windows_generation_handle(
            handle,
            path,
            entry_stat,
            expected_identity,
            allow_directory=True,
        )
        file_id = _require_high_confidence_windows_tree_identity(identity, path)
        if links_before <= 0 or links_before != int(entry_stat.st_nlink):
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("entry link count does not match handle metadata"),
            )
        journal_before = _query_windows_journal_id(handle, path)
        file_usn_before = _query_windows_file_usn(handle, path)
        if stat.S_ISDIR(entry_stat.st_mode):
            if change_time_before <= 0:
                raise FileGenerationError(
                    path,
                    "read Windows directory tree",
                    ValueError("directory ChangeTime is unavailable"),
                )
            kind = b"directory"
            descendants = _windows_directory_children_digest(path, budget, depth)
        elif stat.S_ISREG(entry_stat.st_mode):
            kind = b"regular-file"
            descendants = b""
        else:
            raise FileGenerationError(
                path,
                "read Windows directory tree",
                ValueError("unsupported directory entry type"),
            )
        _assert_windows_generation_path_identity(path, identity)
        second_identity, change_time_after, links_after = _validate_windows_generation_handle(
            handle,
            path,
            entry_stat,
            identity,
            allow_directory=True,
        )
        file_usn_after = _query_windows_file_usn(handle, path)
        journal_after = _query_windows_journal_id(handle, path)
        _assert_stable_windows_tree_observation(
            path,
            identity,
            second_identity,
            journal_before,
            journal_after,
            file_usn_before,
            file_usn_after,
            change_time_before,
            change_time_after,
            links_before,
            links_after,
        )
        fields = (
            b"windows-directory-tree-entry-v1",
            name,
            kind,
            str(int(identity.volume_id)).encode("ascii"),
            file_id,
            str(int(entry_stat.st_mode)).encode("ascii"),
            str(int(entry_stat.st_size)).encode("ascii"),
            str(int(entry_stat.st_mtime_ns)).encode("ascii"),
            str(int(entry_stat.st_nlink)).encode("ascii"),
            str(int(getattr(entry_stat, "st_file_attributes", 0))).encode("ascii"),
            str(int(getattr(entry_stat, "st_reparse_tag", 0))).encode("ascii"),
            str(journal_before).encode("ascii"),
            str(file_usn_before).encode("ascii"),
            str(change_time_before).encode("ascii"),
            str(links_before).encode("ascii"),
            descendants,
        )
        return _windows_tree_hash(fields)
    except FileGenerationError:
        raise
    except (MemoryError, OSError, RecursionError, UnicodeError) as error:
        raise FileGenerationError(
            path,
            "read Windows directory tree",
            error,
        ) from error
    finally:
        _close_handle(handle)


def _windows_directory_membership_digest(path: Path) -> bytes:
    """Hash one complete no-follow tree pass within explicit resource bounds."""

    return _windows_directory_children_digest(
        path,
        _WindowsDirectoryGenerationBudget.start(),
        0,
    )


def _assert_windows_generation_path_identity(
    path: Path,
    expected_identity: FileIdentity,
) -> None:
    try:
        path_identity = get_file_identity(path, follow_symlinks=False)
    except (FileIdentityError, OSError) as error:
        raise FileGenerationError(
            path,
            "bind Windows directory membership to path identity",
            error,
        ) from error
    comparison = same_physical_file(expected_identity, path_identity)
    if comparison.verdict != IdentityVerdict.SAME:
        raise FileGenerationError(
            path,
            "bind Windows directory membership to path identity",
            ValueError(comparison.reason),
        )


def _get_windows_generation_token(
    path: Path,
    stat_result,
    expected_identity: Optional[FileIdentity],
    *,
    allow_directory: bool,
) -> FileGenerationToken:
    try:
        stat_result = stat_result or os.stat(str(path), follow_symlinks=False)
    except OSError as error:
        raise FileGenerationError(path, "stat path before Windows USN query", error) from error
    if not (stat.S_ISREG(stat_result.st_mode) or (allow_directory and stat.S_ISDIR(stat_result.st_mode))):
        raise FileGenerationError(
            path,
            "read Windows USN generation",
            ValueError("path is not a supported regular file or directory"),
        )

    handle = _open_windows_generation_handle(path)
    try:
        return _get_windows_generation_from_handle(
            handle,
            path,
            stat_result,
            expected_identity,
            allow_directory=allow_directory,
        )
    finally:
        _close_handle(handle)


def _get_windows_generation_from_handle(
    handle,
    path: Path,
    stat_result,
    expected_identity: Optional[FileIdentity],
    *,
    allow_directory: bool,
) -> FileGenerationToken:
    try:
        handle_identity, change_time_before, links_before = _validate_windows_generation_handle(
            handle,
            path,
            stat_result,
            expected_identity,
            allow_directory=allow_directory,
        )
        journal_before = _query_windows_journal_id(handle, path)
        file_usn_before = _query_windows_file_usn(handle, path)
        expected_directory = stat.S_ISDIR(stat_result.st_mode)
        if expected_directory:
            _require_high_confidence_windows_tree_identity(handle_identity, path)
            membership_before = _windows_directory_membership_digest(path)
            _assert_windows_generation_path_identity(path, handle_identity)
            membership_after = _windows_directory_membership_digest(path)
            _assert_windows_generation_path_identity(path, handle_identity)
            if membership_before != membership_after:
                raise FileGenerationError(
                    path,
                    "read Windows directory generation",
                    ValueError("directory membership changed during observation"),
                )
        else:
            membership_after = None
        second_identity, change_time_after, links_after = _validate_windows_generation_handle(
            handle,
            path,
            stat_result,
            handle_identity,
            allow_directory=allow_directory,
        )
        file_usn_after = _query_windows_file_usn(handle, path)
        journal_after = _query_windows_journal_id(handle, path)
        if handle_identity != second_identity:
            raise FileGenerationError(
                path,
                "bind Windows USN generation to file identity",
                ValueError("file identity changed during USN observation"),
            )
        if journal_before != journal_after:
            raise FileGenerationError(
                path,
                "read Windows USN generation",
                ValueError("USN journal identifier changed during observation"),
            )
        if file_usn_before != file_usn_after:
            raise FileGenerationError(
                path,
                "read Windows USN generation",
                ValueError("file USN changed during observation"),
            )
        if change_time_before != change_time_after:
            raise FileGenerationError(
                path,
                "read Windows USN generation",
                ValueError("Windows ChangeTime changed during observation"),
            )
        if links_before != links_after:
            raise FileGenerationError(
                path,
                "read Windows USN generation",
                ValueError("file link count changed during observation"),
            )
        if expected_directory:
            if change_time_before <= 0:
                raise FileGenerationError(
                    path,
                    "read Windows directory generation",
                    ValueError("directory ChangeTime is unavailable"),
                )
            compound_generation = (
                (journal_before << 384)
                | (file_usn_before << 320)
                | (change_time_before << 256)
                | int.from_bytes(membership_after, "big")
            )
            namespace = _WINDOWS_DIRECTORY_USN_TOKEN_NAMESPACE
        else:
            compound_generation = (journal_before << 64) | file_usn_before
            namespace = _WINDOWS_USN_TOKEN_NAMESPACE
        return FileGenerationToken(
            namespace,
            compound_generation,
            _WINDOWS_USN_TOKEN_VERSION,
        )
    except FileGenerationError:
        raise
    except OSError as error:
        raise FileGenerationError(path, "read Windows USN generation", error) from error


__all__ = [
    "FileGenerationError",
    "FileGenerationToken",
    "get_entry_generation_token",
    "get_entry_generation_token_from_fd",
    "get_file_generation_token",
    "get_file_generation_token_from_fd",
]
