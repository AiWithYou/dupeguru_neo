# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Handle-bound, atomic, no-replace rename primitives.

The destination directory is kept open for the whole operation.  Linux and macOS use
descriptor-relative native rename calls.  Windows keeps every destination path component open
without delete sharing and uses ``NtSetInformationFile`` with the destination directory handle as
``RootDirectory``.  Unsupported platforms fail closed; there is deliberately no check-then-rename
fallback.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import stat
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_FILE_RENAME_INFORMATION = 10


def _mtime_ns(value: os.stat_result) -> int:
    result = getattr(value, "st_mtime_ns", None)
    return int(result if result is not None else value.st_mtime * 1_000_000_000)


def _identity(value: os.stat_result) -> Tuple[int, int]:
    device = int(value.st_dev)
    inode = int(value.st_ino)
    if not device or not inode:
        raise OSError(errno.ENOTSUP, "The filesystem did not provide a stable entry identity")
    return (device, inode)


def _entry_signature(value: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        *_identity(value),
        stat.S_IFMT(value.st_mode),
        int(value.st_size),
        _mtime_ns(value),
        int(value.st_nlink),
    )


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(marker and attributes & marker)


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or _is_reparse_point(value)


def _absolute(path: Path) -> Path:
    result = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt" and result.drive.startswith("\\\\"):
        raise OSError(
            errno.ENOTSUP,
            "Network paths do not provide the required local handle semantics",
            str(result),
        )
    return result


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError("A bound rename requires a non-empty leaf name")
    if Path(name).name != name or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("A bound rename name must not contain path separators")
    if os.name == "nt":
        if any(character in name for character in '<>:"|?*'):
            raise ValueError("A Windows bound rename name contains a reserved character")
        if name.endswith((" ", ".")):
            raise ValueError("A Windows bound rename name must not end with a dot or space")
        device_stem = name.split(".", 1)[0].rstrip(" .").upper()
        reserved_devices = {"CON", "PRN", "AUX", "NUL"}
        reserved_devices.update("COM{}".format(index) for index in range(1, 10))
        reserved_devices.update("LPT{}".format(index) for index in range(1, 10))
        if device_stem in reserved_devices:
            raise ValueError("A Windows bound rename name uses a reserved DOS device alias")
    return name


def _validate_parts(parts: Sequence[str]) -> Tuple[str, ...]:
    result = tuple(_validate_name(part) for part in parts)
    if not result:
        raise ValueError("At least one relative path component is required")
    return result


def _posix_directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _windows_extended_path(path: Path) -> str:
    value = str(_absolute(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _open_windows_directory_fd(path: Path) -> int:
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

    file_traverse = 0x00000020
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000

    handle = create_file(
        _windows_extended_path(path),
        file_traverse | file_read_attributes | synchronize,
        # Excluding FILE_SHARE_DELETE is intentional.  It leases every opened
        # path component against rename/replacement until the bound operation
        # has committed and been inspected.
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _open_windows_path_lease(path: Path) -> Tuple[int, ...]:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    components = [current]
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        components.append(current)
    handles = []
    try:
        for component in components:
            handle = _open_windows_directory_fd(component)
            handles.append(handle)
            value = os.fstat(handle)
            if _is_link_or_reparse(value):
                raise OSError(
                    errno.ELOOP,
                    "A bound directory component is a reparse point",
                    str(component),
                )
            if not stat.S_ISDIR(value.st_mode):
                raise NotADirectoryError(
                    errno.ENOTDIR,
                    "A bound path component is not a directory",
                    str(component),
                )
        return tuple(handles)
    except BaseException:
        for handle in reversed(handles):
            with contextlib.suppress(OSError):
                os.close(handle)
        raise


def _open_posix_directory(path: Path) -> int:
    if os.open not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "Descriptor-relative directory opens are unavailable",
            str(path),
        )
    absolute = _absolute(path)
    handle = os.open(absolute.anchor, _posix_directory_flags())
    try:
        for part in absolute.parts[1:]:
            next_handle = os.open(part, _posix_directory_flags(), dir_fd=handle)
            previous_handle, handle = handle, next_handle
            os.close(previous_handle)
        value = os.fstat(handle)
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(
                errno.ENOTDIR,
                "The bound entry is not a directory",
                str(absolute),
            )
        return handle
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(handle)
        raise


class BoundDirectory:
    """An opened directory object, independent of later pathname replacement."""

    def __init__(self, path: Path, handles: Sequence[int]) -> None:
        if not handles:
            raise ValueError("A bound directory requires at least one open handle")
        self.path = _absolute(path)
        self._handles = list(handles)
        value = os.fstat(self.fileno())
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            self.close()
            raise OSError(
                errno.ENOTDIR,
                "The opened destination parent is not a plain directory",
                str(self.path),
            )
        self.identity = _identity(value)

    def __enter__(self) -> "BoundDirectory":
        if not self._handles:
            raise ValueError("The bound directory is already closed")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def fileno(self) -> int:
        if not self._handles:
            raise ValueError("The bound directory is closed")
        return self._handles[-1]

    @property
    def native_handle(self) -> int:
        if os.name != "nt":
            return self.fileno()
        import msvcrt

        return int(msvcrt.get_osfhandle(self.fileno()))

    def close(self) -> None:
        handles, self._handles = self._handles, []
        for handle in reversed(handles):
            try:
                os.close(handle)
            except OSError:
                # Releasing a lease is cleanup, never a new operation outcome.
                # In particular, a close error must not turn an already
                # committed rename into a reported failure.
                continue

    def _windows_path(self, parts: Sequence[str], *, final_may_be_missing: bool = False) -> Path:
        normalized = _validate_parts(parts)
        current = self.path
        for index, part in enumerate(normalized):
            current = current.joinpath(part)
            if final_may_be_missing and index == len(normalized) - 1:
                break
            value = os.lstat(current)
            if _is_link_or_reparse(value):
                raise OSError(
                    errno.ELOOP,
                    "A bound relative path component is a reparse point",
                    str(current),
                )
            if index < len(normalized) - 1 and not stat.S_ISDIR(value.st_mode):
                raise NotADirectoryError(
                    errno.ENOTDIR,
                    "A bound relative path component is not a directory",
                    str(current),
                )
        return current

    @contextlib.contextmanager
    def _posix_relative_parent(self, parts: Sequence[str]) -> Iterator[Tuple[int, str]]:
        normalized = _validate_parts(parts)
        handle = self.fileno()
        opened = []
        try:
            for part in normalized[:-1]:
                handle = os.open(part, _posix_directory_flags(), dir_fd=handle)
                opened.append(handle)
            yield handle, normalized[-1]
        finally:
            for opened_handle in reversed(opened):
                os.close(opened_handle)

    def lstat(self, name: str) -> os.stat_result:
        return self.lstat_parts((name,))

    def lstat_parts(self, parts: Sequence[str]) -> os.stat_result:
        if os.name == "posix":
            if os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks:
                raise OSError(
                    errno.ENOTSUP,
                    "Descriptor-relative no-follow stat is unavailable",
                    str(self.path),
                )
            with self._posix_relative_parent(parts) as (parent, name):
                return os.stat(
                    name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
        return os.lstat(self._windows_path(parts))

    def open_entry(self, name: str, flags: int, mode: int = 0o777) -> int:
        name = _validate_name(name)
        if os.name == "posix":
            if os.open not in os.supports_dir_fd:
                raise OSError(
                    errno.ENOTSUP,
                    "Descriptor-relative entry opens are unavailable",
                    str(self.path),
                )
            return os.open(name, flags, mode, dir_fd=self.fileno())
        return os.open(self._windows_path((name,), final_may_be_missing=True), flags, mode)

    def mkdir(self, name: str, mode: int = 0o777) -> None:
        name = _validate_name(name)
        if os.name == "posix":
            if os.mkdir not in os.supports_dir_fd:
                raise OSError(
                    errno.ENOTSUP,
                    "Descriptor-relative directory creation is unavailable",
                    str(self.path),
                )
            os.mkdir(name, mode, dir_fd=self.fileno())
            return
        os.mkdir(self._windows_path((name,), final_may_be_missing=True), mode)

    def open_child(
        self,
        name: str,
        *,
        expected_identity: Optional[Tuple[int, int]] = None,
    ) -> "BoundDirectory":
        name = _validate_name(name)
        child_path = self.path.joinpath(name)
        if os.name == "posix":
            handle = os.open(
                name,
                _posix_directory_flags(),
                dir_fd=self.fileno(),
            )
            result = BoundDirectory(child_path, (handle,))
            if expected_identity is not None and result.identity != tuple(expected_identity):
                result.close()
                raise OSError(
                    errno.ESTALE,
                    "A bound child directory changed while it was opened",
                    str(child_path),
                )
            return result
        return open_bound_directory(
            child_path,
            expected_identity=expected_identity,
        )

    def unlink_parts(self, parts: Sequence[str]) -> None:
        if os.name == "posix":
            if os.unlink not in os.supports_dir_fd:
                raise OSError(
                    errno.ENOTSUP,
                    "Descriptor-relative unlink is unavailable",
                    str(self.path),
                )
            with self._posix_relative_parent(parts) as (parent, name):
                os.unlink(name, dir_fd=parent)
            return
        os.unlink(self._windows_path(parts))

    def rmdir_parts(self, parts: Sequence[str]) -> None:
        if os.name == "posix":
            if os.rmdir not in os.supports_dir_fd:
                raise OSError(
                    errno.ENOTSUP,
                    "Descriptor-relative directory removal is unavailable",
                    str(self.path),
                )
            with self._posix_relative_parent(parts) as (parent, name):
                os.rmdir(name, dir_fd=parent)
            return
        os.rmdir(self._windows_path(parts))


def open_bound_directory(
    path: Path,
    *,
    expected_identity: Optional[Tuple[int, int]] = None,
) -> BoundDirectory:
    """Open ``path`` as a stable directory object and optionally bind its identity."""

    absolute = _absolute(path)
    if os.name == "posix":
        handles = (_open_posix_directory(absolute),)
    elif os.name == "nt":
        handles = _open_windows_path_lease(absolute)
    else:
        raise OSError(
            errno.ENOTSUP,
            "Handle-bound directory operations are unsupported on this platform",
            str(absolute),
        )
    try:
        result = BoundDirectory(absolute, handles)
    except BaseException:
        # BoundDirectory owns and closes handles after construction starts.
        raise
    if expected_identity is not None and result.identity != tuple(expected_identity):
        result.close()
        raise OSError(
            errno.ESTALE,
            "The destination directory changed before it could be bound",
            str(absolute),
        )
    return result


@dataclass(frozen=True)
class RenameCommit:
    """The kernel-confirmed outcome of one native atomic rename."""

    source_identity: Tuple[int, int]
    destination_parent_identity: Tuple[int, int]
    destination_name: str
    postcondition_verified: bool
    verification_error: Optional[str] = None


def _raise_posix_rename_error(error_number: int, destination: str) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            errno.EEXIST,
            os.strerror(errno.EEXIST),
            destination,
        )
    if error_number in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        raise OSError(
            errno.ENOTSUP,
            "Atomic descriptor-relative no-replace rename is unavailable",
            destination,
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def _posix_rename_no_replace(
    source_directory: BoundDirectory,
    source_name: str,
    destination_directory: BoundDirectory,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flag = _RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flag = _RENAME_EXCL
    else:
        raise OSError(
            errno.ENOTSUP,
            "Atomic descriptor-relative no-replace rename is unsupported on this POSIX platform",
        )
    if rename is None:
        raise OSError(
            errno.ENOTSUP,
            "The platform does not expose a descriptor-relative no-replace rename",
        )
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename(
        source_directory.fileno(),
        os.fsencode(source_name),
        destination_directory.fileno(),
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        _raise_posix_rename_error(ctypes.get_errno(), destination_name)


def _open_windows_source(path: Path) -> int:
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

    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000

    handle = create_file(
        _windows_extended_path(path),
        delete_access | file_read_attributes | synchronize,
        # The source object remains leased against a second rename/delete until
        # the native result has been converted into a RenameCommit.
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _raise_windows_rename_error(status: int, destination: str) -> None:
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    rtl_nt_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_nt_status_to_dos_error.argtypes = [wintypes.ULONG]
    rtl_nt_status_to_dos_error.restype = wintypes.ULONG
    error_number = int(
        rtl_nt_status_to_dos_error(
            wintypes.ULONG(status).value,
        )
    )
    if error_number in {80, 183}:
        raise FileExistsError(
            errno.EEXIST,
            "The destination already exists",
            destination,
        )
    if error_number == 17:
        raise OSError(
            errno.EXDEV,
            "The source and destination are on different volumes",
            destination,
        )
    if error_number in {1, 50, 87, 120}:
        raise OSError(
            errno.ENOTSUP,
            "Handle-bound no-replace rename is unavailable on this filesystem",
            destination,
        )
    raise ctypes.WinError(error_number)


def _windows_rename_no_replace(
    source_handle: int,
    destination_directory: BoundDirectory,
    destination_name: str,
) -> None:
    import msvcrt
    from ctypes import wintypes

    class _IoStatusValue(ctypes.Union):
        _fields_ = [
            ("status", wintypes.LONG),
            ("pointer", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("value", _IoStatusValue),
            ("information", ctypes.c_size_t),
        ]

    class _FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.ULONG),
            ("file_name", wintypes.WCHAR * 1),
        ]

    encoded_name = destination_name.encode("utf-16-le")
    buffer_size = _FileRenameInformation.file_name.offset + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    information = ctypes.cast(
        buffer,
        ctypes.POINTER(_FileRenameInformation),
    ).contents
    information.replace_if_exists = False
    information.root_directory = destination_directory.native_handle
    information.file_name_length = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenameInformation.file_name.offset,
        encoded_name,
        len(encoded_name),
    )

    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.INT,
    ]
    set_information.restype = wintypes.LONG
    io_status = _IoStatusBlock()
    status = int(
        set_information(
            msvcrt.get_osfhandle(source_handle),
            ctypes.byref(io_status),
            buffer,
            buffer_size,
            _FILE_RENAME_INFORMATION,
        )
    )
    # NT_SUCCESS is defined as a non-negative signed NTSTATUS.  CreateFileW
    # produced a synchronous handle, so normal completion is STATUS_SUCCESS,
    # but informational success values must not be converted into failures.
    if status < 0:
        _raise_windows_rename_error(status, destination_name)


def _verify_commit(
    source_handle: int,
    source_directory: BoundDirectory,
    source_name: str,
    destination_directory: BoundDirectory,
    destination_name: str,
    expected_signature: Tuple[int, int, int, int, int, int],
) -> None:
    if _entry_signature(os.fstat(source_handle)) != expected_signature:
        raise OSError(
            errno.ESTALE,
            "The renamed object changed while its handle was open",
            destination_name,
        )
    destination = destination_directory.lstat(destination_name)
    if _is_link_or_reparse(destination) or _entry_signature(destination) != expected_signature:
        raise OSError(
            errno.ESTALE,
            "The committed destination does not match the opened source",
            destination_name,
        )
    try:
        source_directory.lstat(source_name)
    except FileNotFoundError:
        return
    raise OSError(
        errno.ESTALE,
        "The source name still exists after a successful native rename",
        source_name,
    )


def rename_no_replace(
    source_directory: BoundDirectory,
    source_name: str,
    destination_directory: BoundDirectory,
    destination_name: str,
) -> RenameCommit:
    """Atomically rename one bound entry and never overwrite ``destination_name``.

    A successful native call is itself the commit boundary.  Postcondition inspection is
    diagnostic: a later concurrent change cannot retroactively turn a committed move into a
    reported failure that makes the caller believe its source still exists.
    """

    source_name = _validate_name(source_name)
    destination_name = _validate_name(destination_name)
    source_before = source_directory.lstat(source_name)
    if _is_link_or_reparse(source_before):
        raise OSError(
            errno.ELOOP,
            "The bound rename source is a link or reparse point",
            source_name,
        )
    if not (stat.S_ISREG(source_before.st_mode) or stat.S_ISDIR(source_before.st_mode)):
        raise OSError(
            errno.ENOTSUP,
            "The bound rename source is not a regular file or directory",
            source_name,
        )
    expected_signature = _entry_signature(source_before)
    source_handle = -1
    committed = False
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if stat.S_ISDIR(source_before.st_mode):
                flags |= getattr(os, "O_DIRECTORY", 0)
            source_handle = source_directory.open_entry(source_name, flags)
        elif os.name == "nt":
            source_handle = _open_windows_source(
                source_directory.path.joinpath(source_name),
            )
        else:
            raise OSError(
                errno.ENOTSUP,
                "Handle-bound no-replace rename is unsupported on this platform",
            )
        if _entry_signature(os.fstat(source_handle)) != expected_signature:
            raise OSError(
                errno.ESTALE,
                "The bound rename source changed while it was opened",
                source_name,
            )

        if os.name == "posix":
            _posix_rename_no_replace(
                source_directory,
                source_name,
                destination_directory,
                destination_name,
            )
        else:
            _windows_rename_no_replace(
                source_handle,
                destination_directory,
                destination_name,
            )
        committed = True

        verification_error = None
        try:
            _verify_commit(
                source_handle,
                source_directory,
                source_name,
                destination_directory,
                destination_name,
                expected_signature,
            )
            verified = True
        except Exception as error:
            # The native operation has already committed.  Preserve that fact
            # instead of raising an error that falsely promises the source path
            # still exists.
            verified = False
            verification_error = "{}: {}".format(type(error).__name__, error)
        return RenameCommit(
            source_identity=expected_signature[:2],
            destination_parent_identity=destination_directory.identity,
            destination_name=destination_name,
            postcondition_verified=verified,
            verification_error=verification_error,
        )
    finally:
        if source_handle >= 0:
            try:
                os.close(source_handle)
            except OSError:
                if not committed:
                    raise
