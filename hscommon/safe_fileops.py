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
before publication. Moves are a single same-filesystem no-replace rename. Links, reparse points,
special files, and multiply-linked regular files are rejected rather than followed or silently
copied.
"""

from __future__ import annotations

import errno
import logging
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
from hscommon.atomic_rename import BoundDirectory, RenameCommit, open_bound_directory

COPY_CHUNK_SIZE = 1024 * 1024
MAX_STAGING_NAME_ATTEMPTS = 128
MAX_TREE_DEPTH = 128
MAX_TREE_ENTRIES = 100_000
STAGING_PREFIX = ".dupeguru-copy-"
STAGING_SUFFIX = ".tmp"

RenameNoReplace = Callable[
    [BoundDirectory, str, BoundDirectory, str],
    RenameCommit,
]


@dataclass(frozen=True)
class _Snapshot:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: bytes
    links: int

    @property
    def identity(self) -> Tuple[int, int]:
        return (self.device, self.inode)


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


def _snapshot(value: os.stat_result, path: Path, handle: int = None) -> _Snapshot:
    device = int(value.st_dev)
    inode = int(value.st_ino)
    if not device or not inode:
        raise OSError(errno.ENOTSUP, "The filesystem did not provide a stable file identity", str(path))
    if handle is None:
        generation = get_entry_generation_token(path, stat_result=value)
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


def _validate_directory(path: Path) -> _Snapshot:
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
    return _snapshot(os.lstat(absolute), absolute)


def _ensure_plain_directory_by_path(path: Path) -> _Snapshot:
    """Windows component creation with reparse checks after every create/open race."""

    result = None
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
        result = _snapshot(value, component)
    if result is None:
        result = _snapshot(os.lstat(path), path)
    return result


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
    if current.identity != expected.identity:
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
    current = _snapshot(current_stat, path)
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
    current = _snapshot(os.fstat(handle), path, handle=handle)
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
) -> Tuple[int, _Snapshot]:
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
        result = _snapshot(value, path, handle=handle)
        created.add(path, result)
        if result.links != 1:
            raise OSError(errno.EMLINK, "The staging file has more than one link", str(path))
        return handle, result
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
    destination_snapshot = None
    try:
        destination_handle, destination_snapshot = _new_file(
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
        copied = _snapshot(
            os.fstat(destination_handle),
            destination,
            handle=destination_handle,
        )
        if copied.identity != destination_snapshot.identity or copied.size != source_snapshot.size:
            raise OSError(errno.EIO, "The staging copy is incomplete or changed", str(destination))
        if copied.links != 1:
            raise OSError(errno.EMLINK, "The staging file has more than one link", str(destination))
        try:
            os.fchmod(destination_handle, stat.S_IMODE(source_snapshot.mode))
        except AttributeError:
            pass
        os.fsync(destination_handle)
        os.lseek(source_handle, 0, os.SEEK_SET)
        os.lseek(destination_handle, 0, os.SEEK_SET)
        while True:
            source_data = os.read(source_handle, COPY_CHUNK_SIZE)
            destination_data = os.read(destination_handle, COPY_CHUNK_SIZE)
            if source_data != destination_data:
                raise OSError(errno.EIO, "The staging copy failed byte-integrity verification", str(destination))
            if not source_data:
                break
        _assert_handle_snapshot(source_handle, source, source_snapshot)
        _assert_snapshot(source, source_snapshot)
        copied = _snapshot(
            os.fstat(destination_handle),
            destination,
            handle=destination_handle,
        )
        if copied.identity != destination_snapshot.identity or copied.size != source_snapshot.size:
            raise OSError(errno.EIO, "The staging copy changed during integrity verification", str(destination))
        return copied
    finally:
        if destination_handle >= 0:
            os.close(destination_handle)
        os.close(source_handle)


class _CreatedEntries:
    """Track private staging entries so cleanup never removes an unrecognized replacement."""

    def __init__(self, root: BoundDirectory = None) -> None:
        self._root = root
        self._entries: List[Tuple[Path, _Snapshot, Tuple[str, ...]]] = []

    def add(self, path: Path, snapshot: _Snapshot) -> None:
        relative = ()
        if self._root is not None:
            try:
                relative = path.relative_to(self._root.path).parts
            except ValueError:
                raise ValueError("A tracked staging entry is outside its bound directory")
            if not relative:
                raise ValueError("The bound directory itself cannot be a staging entry")
        self._entries.append((path, snapshot, tuple(relative)))

    def discard(self) -> None:
        self._entries.clear()

    def cleanup(self) -> None:
        for path, expected, relative in reversed(self._entries):
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
    result = _snapshot(value, path)
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
                child_snapshot = _snapshot(child_stat, source_child)
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
        final_snapshot = _snapshot(
            final_stat,
            destination,
            handle=bound_destination.fileno(),
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


def _publish_candidates(
    staged_path: Path,
    staged_snapshot: _Snapshot,
    parent_directory: BoundDirectory,
    candidates: Iterable[Path],
    rename_no_replace: RenameNoReplace,
) -> Path:
    parent = staged_path.parent
    saw_candidate = False
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
        try:
            commit = rename_no_replace(
                parent_directory,
                staged_path.name,
                parent_directory,
                destination.name,
            )
        except FileExistsError:
            continue
        except OSError as error:
            if error.errno == errno.EEXIST:
                continue
            raise
        if not isinstance(commit, RenameCommit):
            raise TypeError("A handle-bound rename callback must return RenameCommit")
        if (
            commit.source_identity != staged_snapshot.identity
            or commit.destination_parent_identity != parent_directory.identity
            or commit.destination_name != destination.name
        ):
            # The callback reported a native commit.  Do not misreport the
            # staging source as still present; return the committed candidate
            # while recording the contract violation.
            logging.error(
                "Atomic rename returned inconsistent commit metadata for %s",
                destination,
            )
            return destination
        if not commit.postcondition_verified:
            logging.warning(
                "Atomic rename committed %s, but post-commit inspection failed: %s",
                destination,
                commit.verification_error,
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
    destination_parent_snapshot = _validate_directory(destination_parent)
    _reject_directory_into_itself(source, destination_parent, source_snapshot)
    with open_bound_directory(
        destination_parent,
        expected_identity=destination_parent_snapshot.identity,
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
            child_snapshot = _snapshot(child_stat, child)
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
    source_parent_snapshot = _validate_directory(source.parent)
    source_snapshot = _inspect_source(source)
    _assert_expected_source_snapshot(source, source_snapshot, expected_source_snapshot)
    _preflight_move_tree(source, source_snapshot, _TreeBudget(), 0)
    with open_bound_directory(
        source.parent,
        expected_identity=source_parent_snapshot.identity,
    ) as source_directory:
        _assert_bound_snapshot(
            source_directory,
            source.name,
            source_snapshot,
            source,
        )
        saw_candidate = False
        for destination in candidates:
            saw_candidate = True
            destination = _absolute(destination)
            destination_parent_snapshot = _validate_directory(destination.parent)
            _reject_directory_into_itself(source, destination.parent, source_snapshot)
            with open_bound_directory(
                destination.parent,
                expected_identity=destination_parent_snapshot.identity,
            ) as destination_directory:
                _assert_bound_snapshot(
                    source_directory,
                    source.name,
                    source_snapshot,
                    source,
                )
                try:
                    commit = rename_no_replace(
                        source_directory,
                        source.name,
                        destination_directory,
                        destination.name,
                    )
                except FileExistsError:
                    continue
                except OSError as error:
                    if error.errno == errno.EEXIST:
                        continue
                    raise
                if not isinstance(commit, RenameCommit):
                    raise TypeError("A handle-bound rename callback must return RenameCommit")
                if (
                    commit.source_identity != source_snapshot.identity
                    or commit.destination_parent_identity != destination_directory.identity
                    or commit.destination_name != destination.name
                ):
                    logging.error(
                        "Atomic rename returned inconsistent commit metadata for %s",
                        destination,
                    )
                    return destination
                if not commit.postcondition_verified:
                    logging.warning(
                        "Atomic rename committed %s, but post-commit inspection failed: %s",
                        destination,
                        commit.verification_error,
                    )
                return destination
        if not saw_candidate:
            raise ValueError("At least one destination candidate is required")
        raise FileExistsError(
            errno.EEXIST,
            "Every bounded destination candidate already exists",
            str(source.parent),
        )
