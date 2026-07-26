# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Stable, path-independent file identities.

``Path`` equality is not enough to decide whether two directory entries name the
same physical file.  POSIX exposes a device/inode pair while Windows exposes a
volume serial number and a file ID through a file handle.  This module keeps the
capability and confidence next to the identity so callers cannot accidentally
treat a weak or foreign identity as conclusive.

There is deliberately no path-based fallback here.  A caller that cannot obtain
an operating-system identity must handle :class:`FileIdentityError` explicitly.
"""

import ctypes
import os

from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Optional, Tuple, Union


class IdentityCapability(Enum):
    """Operating-system facility used to obtain an identity."""

    POSIX_DEVICE_INODE = "posix-device-inode"
    WINDOWS_FILE_ID_128 = "windows-file-id-128"
    WINDOWS_FILE_INDEX_64 = "windows-file-index-64"


class IdentityConfidence(IntEnum):
    """How strongly an identity supports a physical-file comparison."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class IdentityVerdict(Enum):
    """Result of comparing two identities."""

    SAME = "same"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


FileId = Union[int, bytes]


@dataclass(frozen=True)
class FileIdentity:
    """Path-independent identity returned by the operating system."""

    namespace: str
    volume_id: int
    file_id: FileId
    capability: IdentityCapability
    confidence: IdentityConfidence

    @property
    def comparison_key(self) -> Tuple[str, IdentityCapability, int, FileId]:
        """Hashable key suitable for visited-directory and hardlink sets."""

        return (self.namespace, self.capability, self.volume_id, self.file_id)

    @property
    def volume_key(self) -> Tuple[str, int]:
        """Hashable volume key used to enforce mount boundaries."""

        return (self.namespace, self.volume_id)


def identity_record_parts(identity: FileIdentity) -> Tuple[str, str]:
    """Return the canonical service-document volume/file representation."""

    if not isinstance(identity, FileIdentity):
        raise TypeError("identity_record_parts requires a FileIdentity")
    file_id = identity.file_id.hex() if isinstance(identity.file_id, bytes) else str(identity.file_id)
    return str(identity.volume_id), file_id


@dataclass(frozen=True)
class IdentityComparison:
    """A comparison verdict with its confidence and an audit-friendly reason."""

    verdict: IdentityVerdict
    confidence: IdentityConfidence
    reason: str

    @property
    def is_same(self) -> Optional[bool]:
        if self.verdict == IdentityVerdict.SAME:
            return True
        if self.verdict == IdentityVerdict.DIFFERENT:
            return False
        return None


class FileIdentityError(Exception):
    """Raised when a physical identity cannot be obtained."""

    def __init__(self, path, operation, cause=None):
        self.path = Path(path)
        self.operation = operation
        self.cause = cause
        if cause is None:
            detail = "identity is unavailable"
        else:
            detail = "{}: {}".format(type(cause).__name__, cause)
        super().__init__("Could not {} for '{}': {}".format(operation, self.path, detail))


def same_physical_file(left: FileIdentity, right: FileIdentity) -> IdentityComparison:
    """Compare two identities without silently falling back to their paths."""

    confidence = IdentityConfidence(min(left.confidence, right.confidence))
    if left.namespace != right.namespace:
        return IdentityComparison(
            IdentityVerdict.UNKNOWN,
            IdentityConfidence.LOW,
            "identity namespaces differ",
        )
    if left.capability != right.capability:
        return IdentityComparison(
            IdentityVerdict.UNKNOWN,
            IdentityConfidence.LOW,
            "identity capabilities are not directly comparable",
        )
    if left.volume_id != right.volume_id:
        return IdentityComparison(
            IdentityVerdict.DIFFERENT,
            confidence,
            "volume identifiers differ",
        )
    if left.file_id == right.file_id:
        return IdentityComparison(
            IdentityVerdict.SAME,
            confidence,
            "volume and file identifiers match",
        )
    return IdentityComparison(
        IdentityVerdict.DIFFERENT,
        confidence,
        "file identifiers differ on the same volume",
    )


def get_file_identity(path, follow_symlinks=False, stat_result=None) -> FileIdentity:
    """Return the physical identity for ``path``.

    ``follow_symlinks`` defaults to ``False`` so a link is identified as the
    link itself.  ``stat_result`` is an optional no-follow result that lets a
    POSIX directory walker avoid a second system call; Windows always opens a
    handle because ``os.stat`` does not expose the full volume/file ID contract.
    """

    path = Path(path)
    if os.name == "nt":
        try:
            return _get_windows_file_identity(path, follow_symlinks)
        except FileIdentityError:
            raise
        except OSError as error:
            raise FileIdentityError(path, "read Windows file identity", error) from error

    try:
        stat_result = stat_result or os.stat(str(path), follow_symlinks=follow_symlinks)
    except OSError as error:
        raise FileIdentityError(path, "stat path", error) from error

    inode = getattr(stat_result, "st_ino", None)
    device = getattr(stat_result, "st_dev", None)
    if inode in (None, 0) or device is None:
        raise FileIdentityError(path, "read POSIX device/inode")
    return FileIdentity(
        namespace="posix",
        volume_id=int(device),
        file_id=int(inode),
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )


def get_file_identity_from_fd(fd: int, path=None, stat_result=None) -> FileIdentity:
    """Return the physical identity of a caller-owned open descriptor."""

    path = Path(path) if path is not None else Path("<open file>")
    if type(fd) is not int or fd < 0:
        raise FileIdentityError(path, "validate file identity descriptor", ValueError("invalid fd"))
    try:
        stat_result = stat_result or os.fstat(fd)
    except OSError as error:
        raise FileIdentityError(path, "stat open file descriptor", error) from error
    if os.name == "nt":
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError as error:
            raise FileIdentityError(path, "resolve Windows file handle", error) from error
        return _windows_identity_from_handle(handle, path)
    inode = getattr(stat_result, "st_ino", None)
    device = getattr(stat_result, "st_dev", None)
    if inode in (None, 0) or device is None:
        raise FileIdentityError(path, "read POSIX device/inode from open descriptor")
    return FileIdentity(
        namespace="posix",
        volume_id=int(device),
        file_id=int(inode),
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )


if os.name == "nt":
    from ctypes import wintypes

    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ID_INFO_CLASS = 18
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FILE_ID_128),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE

    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL

    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _get_file_information.restype = wintypes.BOOL

    _get_file_information_ex = getattr(_kernel32, "GetFileInformationByHandleEx", None)
    if _get_file_information_ex is not None:
        _get_file_information_ex.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        _get_file_information_ex.restype = wintypes.BOOL


def _windows_identity_from_handle(handle, path: Path) -> FileIdentity:
    if _get_file_information_ex is not None:
        info = _FILE_ID_INFO()
        if _get_file_information_ex(handle, _FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
            file_id = bytes(info.FileId.Identifier)
            if int(info.VolumeSerialNumber) > 0 and any(file_id):
                return FileIdentity(
                    namespace="windows",
                    volume_id=int(info.VolumeSerialNumber),
                    file_id=file_id,
                    capability=IdentityCapability.WINDOWS_FILE_ID_128,
                    confidence=IdentityConfidence.HIGH,
                )

    legacy_info = _BY_HANDLE_FILE_INFORMATION()
    if not _get_file_information(handle, ctypes.byref(legacy_info)):
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileIdentityError(path, "query Windows file handle", error)
    file_index = (int(legacy_info.nFileIndexHigh) << 32) | int(legacy_info.nFileIndexLow)
    return FileIdentity(
        namespace="windows",
        volume_id=int(legacy_info.dwVolumeSerialNumber),
        file_id=file_index,
        capability=IdentityCapability.WINDOWS_FILE_INDEX_64,
        confidence=IdentityConfidence.MEDIUM,
    )


def windows_extended_path(path: Path) -> str:
    """Return an absolute Win32 path that also supports long path names."""

    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


_windows_extended_path = windows_extended_path


def _get_windows_file_identity(path: Path, follow_symlinks: bool) -> FileIdentity:
    if os.name != "nt":
        raise FileIdentityError(path, "read Windows file identity")

    flags = _FILE_FLAG_BACKUP_SEMANTICS
    if not follow_symlinks:
        flags |= _FILE_FLAG_OPEN_REPARSE_POINT
    handle = _create_file(
        _windows_extended_path(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.WinError(ctypes.get_last_error())
        raise FileIdentityError(path, "open Windows file handle", error)

    try:
        return _windows_identity_from_handle(handle, path)
    finally:
        _close_handle(handle)


__all__ = [
    "FileIdentity",
    "FileIdentityError",
    "IdentityCapability",
    "IdentityComparison",
    "IdentityConfidence",
    "IdentityVerdict",
    "get_file_identity",
    "get_file_identity_from_fd",
    "same_physical_file",
    "windows_extended_path",
]
