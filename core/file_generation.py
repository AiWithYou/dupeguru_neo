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
import os
import stat

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
        super().__init__("Could not {} for '{}': {}".format(operation, self.path, detail))


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

    POSIX uses ``st_ctime_ns``. Windows uses
    ``FILE_BASIC_INFO.ChangeTime`` from the same no-follow handle used to
    validate file type, path metadata, and physical identity. There is no
    Windows creation-time fallback.
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
        return _get_windows_generation_from_handle(
            handle,
            path,
            stat_result,
            expected_identity,
            allow_directory=allow_directory,
        )

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
        raise FileGenerationError(path, "stat path before Windows ChangeTime query", error) from error
    if not (stat.S_ISREG(stat_result.st_mode) or (allow_directory and stat.S_ISDIR(stat_result.st_mode))):
        raise FileGenerationError(
            path,
            "read Windows ChangeTime",
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
        tag_info = _FILE_ATTRIBUTE_TAG_INFO()
        _query_windows_handle(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            tag_info,
            path,
            "query Windows reparse attributes",
        )
        if int(tag_info.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise FileGenerationError(path, "read Windows ChangeTime", ValueError("path is a reparse point"))

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
                "read Windows ChangeTime",
                ValueError("handle entry type does not match path metadata"),
            )
        if expected_directory and not allow_directory:
            raise FileGenerationError(path, "read Windows ChangeTime", ValueError("path is a directory"))
        if not expected_directory and int(standard_info.EndOfFile) != int(stat_result.st_size):
            raise FileGenerationError(
                path,
                "bind Windows ChangeTime to path metadata",
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
            raise FileGenerationError(path, "read Windows ChangeTime", ValueError("path is a reparse point"))
        if int(basic_info.ChangeTime) <= 0:
            raise FileGenerationError(
                path,
                "read Windows ChangeTime",
                NotImplementedError("filesystem returned no usable ChangeTime"),
            )
        handle_mtime_ns = (int(basic_info.LastWriteTime) - _WINDOWS_EPOCH_100NS) * 100
        if not expected_directory and handle_mtime_ns != int(stat_result.st_mtime_ns):
            raise FileGenerationError(
                path,
                "bind Windows ChangeTime to path metadata",
                ValueError("file mtime changed before handle validation"),
            )

        try:
            handle_identity = _windows_identity_from_handle(handle, path)
        except FileIdentityError as error:
            raise FileGenerationError(path, "bind Windows ChangeTime to file identity", error) from error
        if expected_identity is not None:
            comparison = same_physical_file(expected_identity, handle_identity)
            if comparison.verdict != IdentityVerdict.SAME:
                raise FileGenerationError(
                    path,
                    "bind Windows ChangeTime to file identity",
                    ValueError(comparison.reason),
                )
        return FileGenerationToken("windows-change-time-100ns", int(basic_info.ChangeTime))
    except FileGenerationError:
        raise
    except OSError as error:
        raise FileGenerationError(path, "read Windows ChangeTime", error) from error


__all__ = [
    "FileGenerationError",
    "FileGenerationToken",
    "get_entry_generation_token",
    "get_entry_generation_token_from_fd",
    "get_file_generation_token",
    "get_file_generation_token_from_fd",
]
