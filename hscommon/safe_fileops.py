# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Fail-closed primitives for publishing copied or moved filesystem entries.

The caller supplies a handle-bound atomic ``rename_no_replace`` implementation.  The callback
receives already-open source and destination directories plus leaf names; a path-only,
check-then-rename implementation does not satisfy this contract.

Copies are completed, flushed, and byte-compared under an unpredictable sibling staging name
before publication. On Windows, regular staging files remain on one read/delete-capable handle
which denies write and delete sharing from final verification through the native rename. Windows
regular-file moves use the same capability from their terminal proof through the rename. Directory
trees are recursively re-proven immediately before each candidate rename. Moves are a single
same-filesystem no-replace rename. Links, reparse points, special files, and multiply-linked regular
files are rejected rather than followed or silently copied.

POSIX descriptor-relative rename and unlink syscalls do not offer an identity-conditional source
operand. The implementation binds parent directories and rechecks identity immediately before
those calls, but a hostile same-user process which can mutate the private staging directory retains
a final name-race window. Such an attacker is outside this module's POSIX safety claim.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import uuid
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Tuple

from core.file_generation import (
    get_entry_generation_token,
    get_entry_generation_token_from_fd,
)
from hscommon.atomic_rename import (
    BoundDirectory,
    PreopenedRenameSource,
    RenameCommit,
    delete_tracked_windows_entry,
    open_bound_directory,
    open_preverified_rename_source,
)

COPY_CHUNK_SIZE = 1024 * 1024
MAX_STAGING_NAME_ATTEMPTS = 128
MAX_TREE_DEPTH = 128
MAX_TREE_ENTRIES = 100_000
STAGING_PREFIX = ".dupeguru-copy-"
STAGING_SUFFIX = ".tmp"

RenameNoReplace = Callable[..., RenameCommit]


class UnverifiedRenameCommitError(RuntimeError):
    """A native rename committed, but its reviewed payload was not proven."""

    def __init__(self, destination: Path, commit: RenameCommit, reason: str):
        self.destination = Path(destination)
        self.commit = commit
        self.reason = str(reason)
        super().__init__(
            "Atomic rename committed at '{}', but the payload is unverified: {}".format(
                self.destination,
                self.reason,
            )
        )


@dataclass(frozen=True)
class _Snapshot:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: bytes
    links: int
    tree_generation: bool

    @property
    def identity(self) -> Tuple[int, int]:
        return (self.device, self.inode)


@dataclass(frozen=True)
class _TrackedEntry:
    """Minimum immutable identity needed for conservative staging cleanup."""

    identity: Tuple[int, int]
    mode: int


class _TreeBudget:
    def __init__(self) -> None:
        self.entries = 0

    def claim(self, path: Path, depth: int) -> None:
        if depth > MAX_TREE_DEPTH:
            raise OSError(errno.E2BIG, "Directory operation exceeded its depth limit", str(path))
        if self.entries >= MAX_TREE_ENTRIES:
            raise OSError(errno.E2BIG, "Directory operation exceeded its entry limit", str(path))
        self.entries += 1


def _mtime_ns(value: os.stat_result) -> int:
    result = getattr(value, "st_mtime_ns", None)
    return int(result if result is not None else value.st_mtime * 1_000_000_000)


def _stat_identity(value: os.stat_result, path: Path) -> Tuple[int, int]:
    device = int(value.st_dev)
    inode = int(value.st_ino)
    if not device or not inode:
        raise OSError(errno.ENOTSUP, "The filesystem did not provide a stable file identity", str(path))
    return (device, inode)


def _snapshot(
    value: os.stat_result,
    path: Path,
    handle: int = None,
    *,
    recursive_directory: bool = True,
) -> _Snapshot:
    device, inode = _stat_identity(value, path)
    tree_generation = stat.S_ISDIR(value.st_mode) and recursive_directory
    if stat.S_ISDIR(value.st_mode) and not recursive_directory:
        generation_token = b"dupeguru-safe-fileops-shallow-directory-v1"
    elif handle is None:
        generation = get_entry_generation_token(path, stat_result=value)
        generation_token = generation.encoded
    else:
        generation = get_entry_generation_token_from_fd(
            handle,
            path=path,
            stat_result=value,
        )
        generation_token = generation.encoded
    return _Snapshot(
        device=device,
        inode=inode,
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=_mtime_ns(value),
        ctime_ns=generation_token,
        links=int(value.st_nlink),
        tree_generation=tree_generation,
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
        raise OSError(errno.ENOTSUP, "Network paths do not provide the required local atomicity", str(result))
    return result


def _existing_components(path: Path) -> Iterator[Path]:
    absolute = _absolute(path)
    anchor = Path(absolute.anchor)
    current = anchor
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        yield current


def _validate_directory(path: Path) -> Tuple[int, int]:
    absolute = _absolute(path)
    for component in _existing_components(absolute):
        try:
            value = os.lstat(component)
        except FileNotFoundError:
            raise FileNotFoundError(errno.ENOENT, "A destination directory does not exist", str(component))
        if _is_link_or_reparse(value):
            raise OSError(errno.ELOOP, "A path component is a link or reparse point", str(component))
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "A path component is not a directory", str(component))
    return _stat_identity(os.lstat(absolute), absolute)


def _ensure_plain_directory_by_path(path: Path) -> None:
    """Windows component creation with reparse checks after every create/open race."""

    for component in _existing_components(path):
        try:
            os.mkdir(component, 0o700)
        except FileExistsError:
            pass
        value = os.lstat(component)
        if _is_link_or_reparse(value):
            raise OSError(errno.ELOOP, "A directory component is a link or reparse point", str(component))
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "A directory component is not a directory", str(component))


def _posix_directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_plain_directory_posix(path: Path) -> int:
    path = _absolute(path)
    handle = os.open(path.anchor, _posix_directory_flags())
    try:
        for part in path.parts[1:]:
            next_handle = os.open(part, _posix_directory_flags(), dir_fd=handle)
            os.close(handle)
            handle = next_handle
        value = os.fstat(handle)
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "The opened entry is not a directory", str(path))
        return handle
    except BaseException:
        os.close(handle)
        raise


def _ensure_plain_directory_posix(path: Path) -> _Snapshot:
    if os.mkdir not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
        raise OSError(errno.ENOTSUP, "Descriptor-relative directory creation is unavailable", str(path))
    handle = os.open(path.anchor, _posix_directory_flags())
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=handle)
            except FileExistsError:
                pass
            next_handle = os.open(part, _posix_directory_flags(), dir_fd=handle)
            os.close(handle)
            handle = next_handle
        value = os.fstat(handle)
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "The created entry is not a directory", str(path))
        return _snapshot(value, path)
    finally:
        os.close(handle)


def ensure_plain_directory(path: Path) -> Path:
    """Create missing directory components without accepting links or reparse points."""

    absolute = _absolute(path)
    if os.name == "posix":
        _ensure_plain_directory_posix(absolute)
    else:
        _ensure_plain_directory_by_path(absolute)
    _validate_directory(absolute)
    return absolute


def _assert_directory_identity(path: Path, expected: _Snapshot) -> None:
    current = _validate_directory(path)
    if current != expected.identity:
        raise OSError(errno.ESTALE, "A destination directory changed during the operation", str(path))


def _is_within(path: Path, directory: Path) -> bool:
    path_value = os.path.normcase(os.fspath(_absolute(path)))
    directory_value = os.path.normcase(os.fspath(_absolute(directory)))
    try:
        return os.path.commonpath((path_value, directory_value)) == directory_value
    except ValueError:
        return False


def _reject_directory_into_itself(source: Path, destination_parent: Path, source_snapshot: _Snapshot) -> None:
    if stat.S_ISDIR(source_snapshot.mode) and _is_within(destination_parent, source):
        raise OSError(
            errno.EINVAL,
            "A directory cannot be copied or moved into itself",
            "{} -> {}".format(source, destination_parent),
        )


def validate_source_destination(source: Path, destination_parent: Path) -> None:
    """Preflight overlap before the caller creates any destination directories."""

    source = _absolute(source)
    source_snapshot = _inspect_source(source)
    _reject_directory_into_itself(source, _absolute(destination_parent), source_snapshot)


def validate_cleanup_path(path: Path, boundary: Path) -> Tuple[Path, Path]:
    """Validate a directory cleanup range, preserving ``boundary`` itself."""

    path = Path(path)
    boundary = Path(boundary)
    if not path.is_absolute() or not boundary.is_absolute():
        raise OSError(errno.EINVAL, "Cleanup paths must be absolute")
    path = _absolute(path)
    boundary = _absolute(boundary)
    if not _is_within(path, boundary):
        raise OSError(errno.EPERM, "Cleanup path is outside its selected root", "{} -> {}".format(path, boundary))
    _validate_directory(boundary)
    _validate_directory(path)
    return path, boundary


def _rmdir_relative_posix(path: Path) -> None:
    parent = path.parent
    handle = _open_plain_directory_posix(parent)
    try:
        os.rmdir(path.name, dir_fd=handle)
    finally:
        os.close(handle)


def remove_empty_directories(path: Path, boundary: Path) -> int:
    """Remove truly empty ancestors below ``boundary`` without deleting any files."""

    current, boundary = validate_cleanup_path(path, boundary)
    removed = 0
    while current != boundary:
        _validate_directory(current)
        try:
            if os.name == "posix":
                if os.rmdir not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
                    raise OSError(errno.ENOTSUP, "Descriptor-relative directory removal is unavailable", str(current))
                _rmdir_relative_posix(current)
            else:
                os.rmdir(current)
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
                break
            raise
        removed += 1
        current = current.parent
        _validate_directory(current)
    return removed


def _inspect_source(path: Path) -> _Snapshot:
    absolute = _absolute(path)
    _validate_directory(absolute.parent)
    value = os.lstat(absolute)
    if _is_link_or_reparse(value):
        raise OSError(errno.ELOOP, "The source is a link or reparse point", str(absolute))
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
        raise OSError(errno.ENOTSUP, "Only regular files and directories are supported", str(absolute))
    result = _snapshot(value, absolute)
    if stat.S_ISREG(result.mode) and result.links != 1:
        raise OSError(errno.EMLINK, "Multiply-linked source files are not moved or copied", str(absolute))
    return result


def _assert_expected_source_snapshot(path: Path, observed: _Snapshot, expected) -> None:
    """Bind a generic file operation to a caller-supplied scan snapshot."""

    if expected is None:
        return
    required = ("device", "file_id", "size", "mtime_ns", "ctime_ns")
    if any(not hasattr(expected, name) for name in required):
        raise TypeError("expected_source_snapshot does not implement the file snapshot contract")
    expected_values = (
        str(expected.device),
        str(expected.file_id),
        int(expected.size),
        int(expected.mtime_ns),
        bytes(expected.ctime_ns),
    )
    observed_values = (
        str(observed.device),
        str(observed.inode),
        observed.size,
        observed.mtime_ns,
        observed.ctime_ns,
    )
    if observed_values != expected_values:
        raise OSError(errno.ESTALE, "The source changed after it was reviewed", str(path))


def _assert_snapshot(path: Path, expected: _Snapshot) -> None:
    try:
        current_stat = os.lstat(path)
    except FileNotFoundError:
        raise OSError(errno.ESTALE, "The filesystem entry disappeared during the operation", str(path))
    if _is_link_or_reparse(current_stat):
        raise OSError(errno.ESTALE, "The filesystem entry became a link or reparse point", str(path))
    current = _snapshot(
        current_stat,
        path,
        recursive_directory=expected.tree_generation,
    )
    if current != expected:
        raise OSError(errno.ESTALE, "The filesystem entry changed during the operation", str(path))


def _bound_stat_matches_snapshot(value: os.stat_result, expected: _Snapshot) -> bool:
    """Compare stable entry facts without re-resolving a bound directory by path."""

    return (
        (int(value.st_dev), int(value.st_ino)) == expected.identity
        and int(value.st_mode) == expected.mode
        and int(value.st_size) == expected.size
        and _mtime_ns(value) == expected.mtime_ns
        and int(value.st_nlink) == expected.links
        and not _is_link_or_reparse(value)
    )


def _assert_bound_snapshot(
    directory: BoundDirectory,
    name: str,
    expected: _Snapshot,
    label: Path,
) -> None:
    try:
        current = directory.lstat(name)
    except FileNotFoundError:
        raise OSError(
            errno.ESTALE,
            "The bound filesystem entry disappeared during the operation",
            str(label),
        )
    if not _bound_stat_matches_snapshot(current, expected):
        raise OSError(
            errno.ESTALE,
            "The bound filesystem entry changed during the operation",
            str(label),
        )


def _assert_handle_snapshot(handle: int, path: Path, expected: _Snapshot) -> None:
    current = _snapshot(
        os.fstat(handle),
        path,
        handle=handle,
        recursive_directory=expected.tree_generation,
    )
    if current != expected:
        raise OSError(errno.ESTALE, "The open source changed during the operation", str(path))


def _open_source(path: Path, expected: _Snapshot) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags)
    try:
        _assert_handle_snapshot(handle, path, expected)
        _assert_snapshot(path, expected)
    except BaseException:
        os.close(handle)
        raise
    return handle


def _new_file(
    path: Path,
    mode: int,
    created: "_CreatedEntries",
    *,
    directory: BoundDirectory = None,
) -> Tuple[int, Tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory is None:
        handle = os.open(path, flags, mode)
    else:
        if path.parent != directory.path:
            raise ValueError("A bound staging file must be an immediate child of its directory")
        handle = directory.open_entry(path.name, flags, mode)
    try:
        value = os.fstat(handle)
        if not stat.S_ISREG(value.st_mode) or _is_reparse_point(value):
            raise OSError(errno.ENOTSUP, "The staging entry is not a regular file", str(path))
        identity = _stat_identity(value, path)
        created.add_stat(path, value)
        if int(value.st_nlink) != 1:
            raise OSError(errno.EMLINK, "The staging file has more than one link", str(path))
        return handle, identity
    except BaseException:
        os.close(handle)
        raise


def _write_all(handle: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(handle, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "A staging-file write made no progress")
        remaining = remaining[written:]


def _staging_stat_matches(
    value: os.stat_result,
    expected: os.stat_result,
    expected_identity: Tuple[int, int],
) -> bool:
    return (
        _stat_identity(value, Path("<staging file>")) == expected_identity
        and int(value.st_mode) == int(expected.st_mode)
        and int(value.st_size) == int(expected.st_size)
        and _mtime_ns(value) == _mtime_ns(expected)
        and int(value.st_nlink) == int(expected.st_nlink) == 1
        and stat.S_ISREG(value.st_mode)
        and not _is_reparse_point(value)
    )


def _open_staging_readonly(
    path: Path,
    *,
    directory: BoundDirectory = None,
) -> int:
    """Open a staging file without following reparses or sharing writes."""

    if directory is not None and path.parent != directory.path:
        raise ValueError("A bound staging file must be an immediate child of its directory")
    if os.name == "nt":
        import ctypes
        import msvcrt

        from core.file_identity import (
            _FILE_FLAG_OPEN_REPARSE_POINT,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_DELETE,
            _FILE_SHARE_READ,
            _INVALID_HANDLE_VALUE,
            _OPEN_EXISTING,
            _close_handle,
            _create_file,
            windows_extended_path,
        )

        raw_handle = _create_file(
            windows_extended_path(path),
            _FILE_READ_ATTRIBUTES | 0x0001,  # FILE_READ_DATA
            # Delete sharing is required by the later native atomic rename.
            # Write sharing is deliberately omitted for the whole verification.
            _FILE_SHARE_READ | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_handle(raw_handle)
            raise

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(errno.ENOTSUP, "No-follow staging-file opens are unavailable", str(path))
    flags |= no_follow
    if directory is None:
        return os.open(path, flags)
    return directory.open_entry(path.name, flags)


def _bound_staging_stat(
    path: Path,
    *,
    directory: BoundDirectory = None,
) -> os.stat_result:
    if directory is None:
        return os.lstat(path)
    if path.parent != directory.path:
        raise ValueError("A bound staging file must be an immediate child of its directory")
    return directory.lstat(path.name)


@contextlib.contextmanager
def _verified_staging_reader(
    path: Path,
    expected_stat: os.stat_result,
    expected_identity: Tuple[int, int],
    *,
    directory: BoundDirectory = None,
) -> Iterator[Tuple[int, _Snapshot]]:
    """Lease and prove the completed staging object while it is read."""

    handle = _open_staging_readonly(path, directory=directory)
    try:
        opened_before = os.fstat(handle)
        if not _staging_stat_matches(opened_before, expected_stat, expected_identity):
            raise OSError(errno.ESTALE, "The staging file changed before verification", str(path))
        generation_before = _snapshot(opened_before, path, handle=handle)
        path_before = _bound_staging_stat(path, directory=directory)
        if not _staging_stat_matches(path_before, opened_before, expected_identity):
            raise OSError(errno.ESTALE, "The staging path changed before verification", str(path))

        yield handle, generation_before

        opened_after = os.fstat(handle)
        generation_after = _snapshot(opened_after, path, handle=handle)
        path_after = _bound_staging_stat(path, directory=directory)
        if (
            not _staging_stat_matches(opened_after, opened_before, expected_identity)
            or generation_after != generation_before
            or not _staging_stat_matches(path_after, opened_after, expected_identity)
        ):
            raise OSError(errno.ESTALE, "The staging file changed during verification", str(path))
    finally:
        os.close(handle)


def _compare_open_files(source_handle: int, destination_handle: int, destination: Path) -> None:
    os.lseek(source_handle, 0, os.SEEK_SET)
    os.lseek(destination_handle, 0, os.SEEK_SET)
    while True:
        source_data = os.read(source_handle, COPY_CHUNK_SIZE)
        destination_data = os.read(destination_handle, COPY_CHUNK_SIZE)
        if source_data != destination_data:
            raise OSError(errno.EIO, "The staging copy failed byte-integrity verification", str(destination))
        if not source_data:
            return


def _copy_regular(
    source: Path,
    destination: Path,
    source_snapshot: _Snapshot,
    created: "_CreatedEntries",
    *,
    destination_directory: BoundDirectory = None,
) -> _Snapshot:
    source_handle = _open_source(source, source_snapshot)
    destination_handle = -1
    destination_identity = None
    try:
        destination_handle, destination_identity = _new_file(
            destination,
            stat.S_IMODE(source_snapshot.mode),
            created,
            directory=destination_directory,
        )
        while True:
            data = os.read(source_handle, COPY_CHUNK_SIZE)
            if not data:
                break
            _write_all(destination_handle, data)
        _assert_handle_snapshot(source_handle, source, source_snapshot)
        _assert_snapshot(source, source_snapshot)
        copied_stat = os.fstat(destination_handle)
        if (
            _stat_identity(copied_stat, destination) != destination_identity
            or int(copied_stat.st_size) != source_snapshot.size
        ):
            raise OSError(errno.EIO, "The staging copy is incomplete or changed", str(destination))
        if int(copied_stat.st_nlink) != 1:
            raise OSError(errno.EMLINK, "The staging file has more than one link", str(destination))
        try:
            os.fchmod(destination_handle, stat.S_IMODE(source_snapshot.mode))
        except AttributeError:
            pass
        os.fsync(destination_handle)
        copied_stat = os.fstat(destination_handle)
        os.close(destination_handle)
        destination_handle = -1

        with _verified_staging_reader(
            destination,
            copied_stat,
            destination_identity,
            directory=destination_directory,
        ) as (verified_handle, copied):
            _compare_open_files(source_handle, verified_handle, destination)
            _assert_handle_snapshot(source_handle, source, source_snapshot)
            _assert_snapshot(source, source_snapshot)

        if copied.identity != destination_identity or copied.size != source_snapshot.size:
            raise OSError(errno.EIO, "The staging copy changed during integrity verification", str(destination))
        _assert_handle_snapshot(source_handle, source, source_snapshot)
        _assert_snapshot(source, source_snapshot)
        return copied
    finally:
        if destination_handle >= 0:
            os.close(destination_handle)
        os.close(source_handle)


class _CreatedEntries:
    """Track private staging entries so cleanup never removes an unrecognized replacement."""

    def __init__(self, root: BoundDirectory = None) -> None:
        self._root = root
        self._entries: List[Tuple[Path, _TrackedEntry, Tuple[str, ...]]] = []

    def add(self, path: Path, snapshot: _Snapshot) -> None:
        self._add(path, _TrackedEntry(snapshot.identity, snapshot.mode))

    def add_stat(self, path: Path, value: os.stat_result) -> None:
        self._add(
            path,
            _TrackedEntry(
                _stat_identity(value, path),
                int(value.st_mode),
            ),
        )

    def _add(self, path: Path, tracked: _TrackedEntry) -> None:
        relative = ()
        if self._root is not None:
            try:
                relative = path.relative_to(self._root.path).parts
            except ValueError:
                raise ValueError("A tracked staging entry is outside its bound directory")
            if not relative:
                raise ValueError("The bound directory itself cannot be a staging entry")
        self._entries.append((path, tracked, tuple(relative)))

    def discard(self) -> None:
        self._entries.clear()

    def cleanup(self) -> None:
        for path, expected, relative in reversed(self._entries):
            if os.name == "nt":
                try:
                    # The native helper validates and deletes the same opened
                    # object.  Never fall back to a path unlink after an
                    # unsupported API, sharing conflict, or identity mismatch.
                    delete_tracked_windows_entry(
                        path,
                        expected.identity,
                        expected.mode,
                    )
                except OSError:
                    # Ambiguous entries are intentionally left for inspection.
                    pass
                continue
            try:
                if self._root is None:
                    value = os.lstat(path)
                else:
                    value = self._root.lstat_parts(relative)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if (
                _is_link_or_reparse(value)
                or (int(value.st_dev), int(value.st_ino)) != expected.identity
                or stat.S_IFMT(value.st_mode) != stat.S_IFMT(expected.mode)
            ):
                continue
            try:
                if self._root is None:
                    if stat.S_ISDIR(expected.mode):
                        os.rmdir(path)
                    else:
                        os.unlink(path)
                elif stat.S_ISDIR(expected.mode):
                    self._root.rmdir_parts(relative)
                else:
                    self._root.unlink_parts(relative)
            except OSError:
                # A concurrently changed or non-empty entry is deliberately left behind.
                continue
        self._entries.clear()


def _create_directory(
    path: Path,
    source_snapshot: _Snapshot,
    created: _CreatedEntries,
    *,
    directory: BoundDirectory = None,
) -> _Snapshot:
    if directory is None:
        os.mkdir(path, stat.S_IMODE(source_snapshot.mode))
        value = os.lstat(path)
    else:
        if path.parent != directory.path:
            raise ValueError("A bound staging directory must be an immediate child of its directory")
        directory.mkdir(path.name, stat.S_IMODE(source_snapshot.mode))
        value = directory.lstat(path.name)
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise OSError(errno.ENOTSUP, "The staging entry is not a plain directory", str(path))
    result = _snapshot(value, path, recursive_directory=False)
    created.add(path, result)
    return result


def _copy_tree(
    source: Path,
    destination: Path,
    source_snapshot: _Snapshot,
    created: _CreatedEntries,
    budget: _TreeBudget,
    depth: int,
    *,
    destination_directory: BoundDirectory = None,
    verify_tree_root: bool = True,
) -> _Snapshot:
    budget.claim(source, depth)
    destination_snapshot = _create_directory(
        destination,
        source_snapshot,
        created,
        directory=destination_directory,
    )
    if destination_directory is None:
        opened_destination = open_bound_directory(
            destination,
            expected_identity=destination_snapshot.identity,
        )
    else:
        opened_destination = destination_directory.open_child(
            destination.name,
            expected_identity=destination_snapshot.identity,
        )
    with opened_destination as bound_destination:
        with os.scandir(source) as entries:
            for entry in entries:
                source_child = source.joinpath(entry.name)
                destination_child = destination.joinpath(entry.name)
                # CPython's Windows DirEntry cache can omit stable file IDs.  lstat() is also
                # required here so the snapshot has the same identity contract on every platform.
                child_stat = os.lstat(source_child)
                if _is_link_or_reparse(child_stat):
                    raise OSError(
                        errno.ELOOP,
                        "Directory copies do not follow or reproduce links",
                        str(source_child),
                    )
                child_snapshot = _snapshot(
                    child_stat,
                    source_child,
                    recursive_directory=False,
                )
                if stat.S_ISREG(child_snapshot.mode):
                    budget.claim(source_child, depth + 1)
                    if child_snapshot.links != 1:
                        raise OSError(
                            errno.EMLINK,
                            "Directory copies reject multiply-linked regular files",
                            str(source_child),
                        )
                    _copy_regular(
                        source_child,
                        destination_child,
                        child_snapshot,
                        created,
                        destination_directory=bound_destination,
                    )
                elif stat.S_ISDIR(child_snapshot.mode):
                    _copy_tree(
                        source_child,
                        destination_child,
                        child_snapshot,
                        created,
                        budget,
                        depth + 1,
                        destination_directory=bound_destination,
                        verify_tree_root=False,
                    )
                else:
                    raise OSError(
                        errno.ENOTSUP,
                        "Directory copies only support files and directories",
                        str(source_child),
                    )
        final_stat = os.fstat(bound_destination.fileno())
        if _is_link_or_reparse(final_stat) or not stat.S_ISDIR(final_stat.st_mode):
            raise OSError(
                errno.ESTALE,
                "The staging directory changed type",
                str(destination),
            )
        if os.name == "nt":
            # ReOpenFile cannot reopen a directory descriptor with a stricter
            # share mode. The bound directory lease prevents replacement, so
            # obtain the USN through a separate no-follow/no-write path handle.
            final_snapshot = _snapshot(
                final_stat,
                destination,
                recursive_directory=verify_tree_root,
            )
        else:
            final_snapshot = _snapshot(
                final_stat,
                destination,
                handle=bound_destination.fileno(),
                recursive_directory=verify_tree_root,
            )
        if final_snapshot.identity != destination_snapshot.identity:
            raise OSError(
                errno.ESTALE,
                "The staging directory identity changed",
                str(destination),
            )
    _assert_snapshot(source, source_snapshot)
    return final_snapshot


def _new_staging_path(parent: Path, directory: BoundDirectory) -> Path:
    if parent != directory.path:
        raise ValueError("The staging parent must match its bound directory")
    for _ in range(MAX_STAGING_NAME_ATTEMPTS):
        candidate = parent.joinpath("{}{}{}".format(STAGING_PREFIX, uuid.uuid4().hex, STAGING_SUFFIX))
        try:
            directory.lstat(candidate.name)
        except FileNotFoundError:
            return candidate
    raise FileExistsError(errno.EEXIST, "Could not allocate a unique staging name", str(parent))


@contextlib.contextmanager
def _verified_publish_source(
    staged_path: Path,
    staged_snapshot: _Snapshot,
    parent_directory: BoundDirectory,
) -> Iterator[PreopenedRenameSource | None]:
    """Hold a no-write lease from final generation verification through commit."""

    if not stat.S_ISREG(staged_snapshot.mode):
        _assert_bound_snapshot(
            parent_directory,
            staged_path.name,
            staged_snapshot,
            staged_path,
        )
        _assert_snapshot(staged_path, staged_snapshot)
        yield None
        return

    if os.name == "nt":
        expected_signature = (
            *staged_snapshot.identity,
            stat.S_IFMT(staged_snapshot.mode),
            staged_snapshot.size,
            staged_snapshot.mtime_ns,
            staged_snapshot.links,
        )
        with open_preverified_rename_source(
            parent_directory,
            staged_path.name,
            expected_signature,
        ) as preopened_source:
            opened = os.fstat(preopened_source.descriptor)
            observed = _snapshot(
                opened,
                staged_path,
                handle=preopened_source.descriptor,
            )
            path_stat = parent_directory.lstat(staged_path.name)
            if observed != staged_snapshot or not _bound_stat_matches_snapshot(path_stat, staged_snapshot):
                raise OSError(errno.ESTALE, "The staging file changed before publication", str(staged_path))
            yield preopened_source
        return

    handle = _open_staging_readonly(
        staged_path,
        directory=parent_directory,
    )
    try:
        opened = os.fstat(handle)
        observed = _snapshot(opened, staged_path, handle=handle)
        path_stat = parent_directory.lstat(staged_path.name)
        if observed != staged_snapshot or not _bound_stat_matches_snapshot(path_stat, staged_snapshot):
            raise OSError(errno.ESTALE, "The staging file changed before publication", str(staged_path))
        yield None
    finally:
        os.close(handle)


def _require_verified_commit(
    commit: RenameCommit,
    *,
    destination: Path,
    expected_source_identity: Tuple[int, int],
    expected_destination_parent_identity: Tuple[int, int],
    require_preopened_source: bool,
) -> None:
    if not isinstance(commit, RenameCommit):
        raise TypeError("A handle-bound rename callback must return RenameCommit")
    mismatches = []
    if commit.source_identity != expected_source_identity:
        mismatches.append("source identity")
    if commit.destination_parent_identity != expected_destination_parent_identity:
        mismatches.append("destination parent identity")
    if commit.destination_name != destination.name:
        mismatches.append("destination name")
    if require_preopened_source and not commit.preopened_source_used:
        mismatches.append("preopened source capability")
    if mismatches:
        raise UnverifiedRenameCommitError(
            destination,
            commit,
            "commit metadata mismatched: {}".format(", ".join(mismatches)),
        )
    if not commit.postcondition_verified:
        raise UnverifiedRenameCommitError(
            destination,
            commit,
            commit.verification_error or "post-commit verification did not succeed",
        )


def _publish_candidates(
    staged_path: Path,
    staged_snapshot: _Snapshot,
    parent_directory: BoundDirectory,
    candidates: Iterable[Path],
    rename_no_replace: RenameNoReplace,
) -> Path:
    parent = staged_path.parent
    saw_candidate = False
    with _verified_publish_source(
        staged_path,
        staged_snapshot,
        parent_directory,
    ) as preopened_source:
        for destination in candidates:
            saw_candidate = True
            destination = _absolute(destination)
            if destination.parent != parent:
                raise ValueError("All destination candidates must have the staging directory as their parent")
            _assert_bound_snapshot(
                parent_directory,
                staged_path.name,
                staged_snapshot,
                staged_path,
            )
            if preopened_source is None:
                _assert_snapshot(staged_path, staged_snapshot)
            try:
                if preopened_source is None:
                    commit = rename_no_replace(
                        parent_directory,
                        staged_path.name,
                        parent_directory,
                        destination.name,
                    )
                else:
                    commit = rename_no_replace(
                        parent_directory,
                        staged_path.name,
                        parent_directory,
                        destination.name,
                        preopened_source=preopened_source,
                    )
            except FileExistsError:
                continue
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                raise
            _require_verified_commit(
                commit,
                destination=destination,
                expected_source_identity=staged_snapshot.identity,
                expected_destination_parent_identity=parent_directory.identity,
                require_preopened_source=preopened_source is not None,
            )
            return destination
    if not saw_candidate:
        raise ValueError("At least one destination candidate is required")
    raise FileExistsError(errno.EEXIST, "Every bounded destination candidate already exists", str(parent))


def copy_to_first_available(
    source: Path,
    candidates: Iterable[Path],
    rename_no_replace: RenameNoReplace,
    *,
    expected_source_snapshot=None,
) -> Path:
    """Copy ``source`` once, then atomically publish it at the first available candidate."""

    source = _absolute(source)
    source_snapshot = _inspect_source(source)
    _assert_expected_source_snapshot(source, source_snapshot, expected_source_snapshot)
    candidates_iterator = iter(candidates)
    try:
        first = _absolute(next(candidates_iterator))
    except StopIteration:
        raise ValueError("At least one destination candidate is required")
    destination_parent = first.parent
    destination_parent_identity = _validate_directory(destination_parent)
    _reject_directory_into_itself(source, destination_parent, source_snapshot)
    with open_bound_directory(
        destination_parent,
        expected_identity=destination_parent_identity,
    ) as destination_directory:
        staging_path = _new_staging_path(destination_parent, destination_directory)
        created = _CreatedEntries(destination_directory)
        budget = _TreeBudget()
        try:
            if stat.S_ISREG(source_snapshot.mode):
                staged_snapshot = _copy_regular(
                    source,
                    staging_path,
                    source_snapshot,
                    created,
                    destination_directory=destination_directory,
                )
            else:
                staged_snapshot = _copy_tree(
                    source,
                    staging_path,
                    source_snapshot,
                    created,
                    budget,
                    0,
                    destination_directory=destination_directory,
                )
            published = _publish_candidates(
                staging_path,
                staged_snapshot,
                destination_directory,
                chain((first,), candidates_iterator),
                rename_no_replace,
            )
            created.discard()
            return published
        except BaseException:
            created.cleanup()
            raise


def _preflight_move_tree(
    source: Path,
    source_snapshot: _Snapshot,
    budget: _TreeBudget,
    depth: int,
) -> None:
    budget.claim(source, depth)
    if stat.S_ISREG(source_snapshot.mode):
        return
    with os.scandir(source) as entries:
        for entry in entries:
            child = source.joinpath(entry.name)
            child_stat = os.lstat(child)
            if _is_link_or_reparse(child_stat):
                raise OSError(errno.ELOOP, "Directory moves reject links and reparse points", str(child))
            child_snapshot = _snapshot(
                child_stat,
                child,
                recursive_directory=False,
            )
            if stat.S_ISREG(child_snapshot.mode):
                budget.claim(child, depth + 1)
                if child_snapshot.links != 1:
                    raise OSError(errno.EMLINK, "Directory moves reject multiply-linked files", str(child))
            elif stat.S_ISDIR(child_snapshot.mode):
                _preflight_move_tree(child, child_snapshot, budget, depth + 1)
            else:
                raise OSError(errno.ENOTSUP, "Directory moves only support files and directories", str(child))
    _assert_snapshot(source, source_snapshot)


def move_to_first_available(
    source: Path,
    candidates: Iterable[Path],
    rename_no_replace: RenameNoReplace,
    *,
    expected_source_snapshot=None,
) -> Path:
    """Atomically move ``source`` to the first candidate which does not exist.

    Cross-volume moves are intentionally not emulated with copy-and-delete.  ``EXDEV`` and an
    unsupported no-replace primitive are returned to the caller with the source untouched.
    """

    source = _absolute(source)
    source_parent_identity = _validate_directory(source.parent)
    source_snapshot = _inspect_source(source)
    _assert_expected_source_snapshot(source, source_snapshot, expected_source_snapshot)
    _preflight_move_tree(source, source_snapshot, _TreeBudget(), 0)
    with open_bound_directory(
        source.parent,
        expected_identity=source_parent_identity,
    ) as source_directory:
        _assert_bound_snapshot(
            source_directory,
            source.name,
            source_snapshot,
            source,
        )
        with _verified_publish_source(
            source,
            source_snapshot,
            source_directory,
        ) as preopened_source:
            saw_candidate = False
            for destination in candidates:
                saw_candidate = True
                destination = _absolute(destination)
                destination_parent_identity = _validate_directory(destination.parent)
                _reject_directory_into_itself(source, destination.parent, source_snapshot)
                with open_bound_directory(
                    destination.parent,
                    expected_identity=destination_parent_identity,
                ) as destination_directory:
                    _assert_bound_snapshot(
                        source_directory,
                        source.name,
                        source_snapshot,
                        source,
                    )
                    if preopened_source is None:
                        _assert_snapshot(source, source_snapshot)
                    try:
                        if preopened_source is None:
                            commit = rename_no_replace(
                                source_directory,
                                source.name,
                                destination_directory,
                                destination.name,
                            )
                        else:
                            commit = rename_no_replace(
                                source_directory,
                                source.name,
                                destination_directory,
                                destination.name,
                                preopened_source=preopened_source,
                            )
                    except FileExistsError:
                        continue
                    except OSError as error:
                        if error.errno == errno.EEXIST:
                            continue
                        raise
                    _require_verified_commit(
                        commit,
                        destination=destination,
                        expected_source_identity=source_snapshot.identity,
                        expected_destination_parent_identity=destination_directory.identity,
                        require_preopened_source=preopened_source is not None,
                    )
                    return destination
            if not saw_candidate:
                raise ValueError("At least one destination candidate is required")
            raise FileExistsError(
                errno.EEXIST,
                "Every bounded destination candidate already exists",
                str(source.parent),
            )
