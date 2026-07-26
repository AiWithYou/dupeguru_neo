# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""A bounded-root, no-follow filesystem walker.

The event stream is intentionally explicit: files are emitted only after a
physical identity has been obtained, while links, reparse points, mount
boundaries, cycles, special files, and errors each have a distinct event.  A
caller can therefore report incomplete coverage instead of silently presenting
an incomplete scan as authoritative.
"""

import os
import stat
import sys

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, Set, Tuple, Union

from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityCapability,
    IdentityConfidence,
    IdentityVerdict,
    get_file_identity,
    same_physical_file,
)


class WalkEventKind(Enum):
    ROOT_STARTED = "root-started"
    DIRECTORY = "directory"
    FILE = "file"
    DIRECTORY_PRUNED = "directory-pruned"
    SYMLINK_SKIPPED = "symlink-skipped"
    REPARSE_POINT_SKIPPED = "reparse-point-skipped"
    MOUNT_SKIPPED = "mount-skipped"
    CYCLE_SKIPPED = "cycle-skipped"
    OUTSIDE_ALLOWED_ROOT_SKIPPED = "outside-allowed-root-skipped"
    SPECIAL_FILE_SKIPPED = "special-file-skipped"
    DIRECTORY_CHANGED_SKIPPED = "directory-changed-skipped"
    ERROR = "error"
    COVERAGE = "coverage"
    ROOT_COMPLETED = "root-completed"


@dataclass(frozen=True)
class WalkError:
    """Serializable error details attached to an ``ERROR`` event."""

    operation: str
    error_type: str
    message: str
    errno: Optional[int] = None
    winerror: Optional[int] = None


@dataclass(frozen=True)
class WalkCoverage:
    """Final accounting for every entry the walker observed."""

    entries_seen: int
    files: int
    directories: int
    pruned_directories: int
    skipped_symlinks: int
    skipped_reparse_points: int
    skipped_mounts: int
    skipped_cycles: int
    skipped_outside_root: int
    skipped_special_files: int
    skipped_changed_directories: int
    errors: int
    identity_failures: int
    high_confidence_identities: int
    medium_confidence_identities: int
    low_confidence_identities: int
    identity_capabilities: Tuple[IdentityCapability, ...]

    @property
    def complete(self) -> bool:
        """Whether no error or coverage-reducing skip occurred."""

        return not any(
            (
                self.skipped_symlinks,
                self.skipped_reparse_points,
                self.skipped_mounts,
                self.skipped_cycles,
                self.skipped_outside_root,
                self.skipped_special_files,
                self.skipped_changed_directories,
                self.errors,
            )
        )


@dataclass(frozen=True)
class WalkEvent:
    kind: WalkEventKind
    path: Path
    identity: Optional[FileIdentity] = None
    error: Optional[WalkError] = None
    coverage: Optional[WalkCoverage] = None
    detail: str = ""


class _CoverageCounter:
    def __init__(self):
        self.entries_seen = 0
        self.files = 0
        self.directories = 0
        self.pruned_directories = 0
        self.skipped_symlinks = 0
        self.skipped_reparse_points = 0
        self.skipped_mounts = 0
        self.skipped_cycles = 0
        self.skipped_outside_root = 0
        self.skipped_special_files = 0
        self.skipped_changed_directories = 0
        self.errors = 0
        self.identity_failures = 0
        self.high_confidence_identities = 0
        self.medium_confidence_identities = 0
        self.low_confidence_identities = 0
        self.identity_capabilities = set()

    def add_identity(self, identity):
        self.identity_capabilities.add(identity.capability)
        if identity.confidence == IdentityConfidence.HIGH:
            self.high_confidence_identities += 1
        elif identity.confidence == IdentityConfidence.MEDIUM:
            self.medium_confidence_identities += 1
        else:
            self.low_confidence_identities += 1

    def snapshot(self):
        capabilities = tuple(sorted(self.identity_capabilities, key=lambda item: item.value))
        return WalkCoverage(
            entries_seen=self.entries_seen,
            files=self.files,
            directories=self.directories,
            pruned_directories=self.pruned_directories,
            skipped_symlinks=self.skipped_symlinks,
            skipped_reparse_points=self.skipped_reparse_points,
            skipped_mounts=self.skipped_mounts,
            skipped_cycles=self.skipped_cycles,
            skipped_outside_root=self.skipped_outside_root,
            skipped_special_files=self.skipped_special_files,
            skipped_changed_directories=self.skipped_changed_directories,
            errors=self.errors,
            identity_failures=self.identity_failures,
            high_confidence_identities=self.high_confidence_identities,
            medium_confidence_identities=self.medium_confidence_identities,
            low_confidence_identities=self.low_confidence_identities,
            identity_capabilities=capabilities,
        )


IdentityGetter = Callable[..., FileIdentity]
DirectoryPruner = Callable[[Path], Optional[Union[bool, str]]]
_DARWIN_STANDARD_ROOT_ALIASES = {
    "etc": Path("/private/etc"),
    "tmp": Path("/private/tmp"),
    "var": Path("/private/var"),
}


class _DirectoryHandleChanged(Exception):
    def __init__(self, current_identity, reason):
        self.current_identity = current_identity
        self.reason = reason
        super().__init__(reason)


def is_reparse_point(stat_result) -> bool:
    """Return whether a stat result represents a Windows reparse point."""

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def walk_no_follow(
    root,
    allowed_root=None,
    cross_mounts=False,
    identity_getter: IdentityGetter = get_file_identity,
    directory_pruner: Optional[DirectoryPruner] = None,
) -> Iterator[WalkEvent]:
    """Walk ``root`` without following links or reparse points.

    ``allowed_root`` defaults to ``root``.  Every candidate is checked against
    that lexical boundary before any traversal.  Mounts are not crossed unless
    ``cross_mounts`` is explicitly enabled.  ``directory_pruner`` is called
    after a directory has been safely identified but before it is enumerated.
    Returning a true value intentionally prunes the directory; a returned
    string is also retained as the event detail.

    The function yields events rather than swallowing errors.  Consumers should
    use only ``FILE`` events as scan inputs and should retain the final
    ``COVERAGE`` event with their scan report.
    """

    root_path = _absolute_path(root)
    allowed_path = _absolute_path(allowed_root if allowed_root is not None else root_path)
    coverage = _CoverageCounter()
    yield WalkEvent(WalkEventKind.ROOT_STARTED, root_path)

    unsafe_ancestor = _find_unsafe_ancestor_component(allowed_path)
    if unsafe_ancestor is not None:
        component_path, component_kind, component_error = unsafe_ancestor
        if component_kind is WalkEventKind.ERROR:
            coverage.errors += 1
            yield WalkEvent(
                component_kind,
                component_path,
                error=_make_walk_error("validate allowed-root component", component_error),
                detail="an allowed-root path component could not be validated",
            )
        elif component_kind is WalkEventKind.SYMLINK_SKIPPED:
            coverage.skipped_symlinks += 1
            yield WalkEvent(
                component_kind,
                component_path,
                detail="an allowed-root path component is a symbolic link",
            )
        else:
            coverage.skipped_reparse_points += 1
            yield WalkEvent(
                component_kind,
                component_path,
                detail="an allowed-root path component is a junction or reparse point",
            )
        yield from _completion_events(root_path, coverage)
        return

    boundary_error = _validate_boundary(allowed_path)
    if boundary_error is not None:
        coverage.errors += 1
        yield WalkEvent(
            WalkEventKind.ERROR,
            allowed_path,
            error=_make_walk_error("validate allowed root", boundary_error),
            detail="allowed root is not a safe physical directory",
        )
        yield from _completion_events(root_path, coverage)
        return

    if not _is_within(root_path, allowed_path):
        coverage.skipped_outside_root += 1
        yield WalkEvent(
            WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
            root_path,
            detail="root is outside the allowed root",
        )
        yield from _completion_events(root_path, coverage)
        return

    unsafe_component = _find_unsafe_component(root_path, allowed_path)
    if unsafe_component is not None:
        component_path, component_kind, component_error = unsafe_component
        if component_error is not None:
            coverage.errors += 1
            yield WalkEvent(
                WalkEventKind.ERROR,
                component_path,
                error=_make_walk_error("validate root component", component_error),
            )
        elif component_kind == WalkEventKind.SYMLINK_SKIPPED:
            coverage.skipped_symlinks += 1
            yield WalkEvent(
                component_kind,
                component_path,
                detail="a root path component is a symbolic link",
            )
        else:
            coverage.skipped_reparse_points += 1
            yield WalkEvent(
                component_kind,
                component_path,
                detail="a root path component is a junction or reparse point",
            )
        yield from _completion_events(root_path, coverage)
        return

    root_stat, root_error = _stat_no_follow(root_path)
    if root_error is not None:
        coverage.errors += 1
        yield WalkEvent(
            WalkEventKind.ERROR,
            root_path,
            error=_make_walk_error("stat root", root_error),
        )
        yield from _completion_events(root_path, coverage)
        return
    if stat.S_ISLNK(root_stat.st_mode):
        coverage.skipped_symlinks += 1
        yield WalkEvent(WalkEventKind.SYMLINK_SKIPPED, root_path, detail="root is a symbolic link")
        yield from _completion_events(root_path, coverage)
        return
    if is_reparse_point(root_stat):
        coverage.skipped_reparse_points += 1
        yield WalkEvent(
            WalkEventKind.REPARSE_POINT_SKIPPED,
            root_path,
            detail="root is a junction or reparse point",
        )
        yield from _completion_events(root_path, coverage)
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        coverage.errors += 1
        error = NotADirectoryError("root is not a directory")
        yield WalkEvent(
            WalkEventKind.ERROR,
            root_path,
            error=_make_walk_error("validate root", error),
        )
        yield from _completion_events(root_path, coverage)
        return

    try:
        root_identity = identity_getter(root_path, follow_symlinks=False, stat_result=root_stat)
    except FileIdentityError as error:
        coverage.errors += 1
        coverage.identity_failures += 1
        yield WalkEvent(
            WalkEventKind.ERROR,
            root_path,
            error=_make_walk_error("identify root", error),
        )
        yield from _completion_events(root_path, coverage)
        return

    coverage.directories += 1
    coverage.add_identity(root_identity)
    prune_detail = _directory_prune_detail(directory_pruner, root_path)
    if prune_detail is not None:
        coverage.pruned_directories += 1
        yield WalkEvent(
            WalkEventKind.DIRECTORY_PRUNED,
            root_path,
            identity=root_identity,
            detail=prune_detail,
        )
        yield from _completion_events(root_path, coverage)
        return
    yield WalkEvent(WalkEventKind.DIRECTORY, root_path, identity=root_identity)

    visited: Set[Tuple] = {root_identity.comparison_key}
    stack = [(root_path, root_identity)]
    while stack:
        current_path, expected_identity = stack.pop()
        changed_event = _directory_changed_event(current_path, expected_identity, identity_getter)
        if changed_event is not None:
            coverage.skipped_changed_directories += 1
            if changed_event.error is not None:
                coverage.errors += 1
                if changed_event.error.operation == "identify directory":
                    coverage.identity_failures += 1
            yield changed_event
            continue

        try:
            entries_context = _scandir_no_follow(current_path, expected_identity, identity_getter)
            with entries_context as entries:
                child_directories = []
                for entry in entries:
                    coverage.entries_seen += 1
                    entry_path = _absolute_path(current_path.joinpath(entry.name))
                    if not _is_within(entry_path, allowed_path):
                        coverage.skipped_outside_root += 1
                        yield WalkEvent(
                            WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
                            entry_path,
                            detail="entry is outside the allowed root",
                        )
                        continue

                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        coverage.errors += 1
                        yield WalkEvent(
                            WalkEventKind.ERROR,
                            entry_path,
                            error=_make_walk_error("stat entry", error),
                        )
                        continue

                    if stat.S_ISLNK(entry_stat.st_mode):
                        coverage.skipped_symlinks += 1
                        yield WalkEvent(
                            WalkEventKind.SYMLINK_SKIPPED,
                            entry_path,
                            detail="symbolic links are never followed",
                        )
                        continue
                    if is_reparse_point(entry_stat):
                        coverage.skipped_reparse_points += 1
                        yield WalkEvent(
                            WalkEventKind.REPARSE_POINT_SKIPPED,
                            entry_path,
                            detail="junctions and reparse points are never followed",
                        )
                        continue

                    if not (stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode)):
                        coverage.skipped_special_files += 1
                        yield WalkEvent(
                            WalkEventKind.SPECIAL_FILE_SKIPPED,
                            entry_path,
                            detail="entry is not a regular file or directory",
                        )
                        continue

                    try:
                        identity = identity_getter(entry_path, follow_symlinks=False, stat_result=entry_stat)
                    except FileIdentityError as error:
                        coverage.errors += 1
                        coverage.identity_failures += 1
                        yield WalkEvent(
                            WalkEventKind.ERROR,
                            entry_path,
                            error=_make_walk_error("identify entry", error),
                        )
                        continue

                    if stat.S_ISDIR(entry_stat.st_mode):
                        if not cross_mounts and _is_mount_boundary(entry_path, identity, root_identity):
                            coverage.skipped_mounts += 1
                            yield WalkEvent(
                                WalkEventKind.MOUNT_SKIPPED,
                                entry_path,
                                identity=identity,
                                detail="directory crosses a mount or volume boundary",
                            )
                            continue
                        if identity.comparison_key in visited:
                            coverage.skipped_cycles += 1
                            yield WalkEvent(
                                WalkEventKind.CYCLE_SKIPPED,
                                entry_path,
                                identity=identity,
                                detail="directory identity has already been visited",
                            )
                            continue
                        prune_detail = _directory_prune_detail(directory_pruner, entry_path)
                        if prune_detail is not None:
                            coverage.directories += 1
                            coverage.pruned_directories += 1
                            coverage.add_identity(identity)
                            yield WalkEvent(
                                WalkEventKind.DIRECTORY_PRUNED,
                                entry_path,
                                identity=identity,
                                detail=prune_detail,
                            )
                            continue
                        visited.add(identity.comparison_key)
                        coverage.directories += 1
                        coverage.add_identity(identity)
                        yield WalkEvent(WalkEventKind.DIRECTORY, entry_path, identity=identity)
                        child_directories.append((entry_path, identity))
                    else:
                        coverage.files += 1
                        coverage.add_identity(identity)
                        yield WalkEvent(WalkEventKind.FILE, entry_path, identity=identity)
        except _DirectoryHandleChanged as error:
            coverage.skipped_changed_directories += 1
            yield WalkEvent(
                WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
                current_path,
                identity=error.current_identity,
                detail=error.reason,
            )
            continue
        except FileIdentityError as error:
            coverage.skipped_changed_directories += 1
            coverage.errors += 1
            coverage.identity_failures += 1
            yield WalkEvent(
                WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
                current_path,
                error=_make_walk_error("identify directory handle", error),
                detail="opened directory identity could not be revalidated",
            )
            continue
        except OSError as error:
            coverage.errors += 1
            yield WalkEvent(
                WalkEventKind.ERROR,
                current_path,
                error=_make_walk_error("scan directory", error),
            )
            continue

        stack.extend(reversed(child_directories))

    yield from _completion_events(root_path, coverage)


def _darwin_alias_signature(value: os.stat_result) -> Tuple[int, ...]:
    mtime_ns = getattr(value, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(value.st_mtime * 1_000_000_000)
    ctime_ns = getattr(value, "st_ctime_ns", None)
    if ctime_ns is None:
        ctime_ns = int(value.st_ctime * 1_000_000_000)
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(mtime_ns),
        int(ctime_ns),
        int(getattr(value, "st_uid", -1)),
        int(getattr(value, "st_gid", -1)),
    )


def _authenticated_darwin_root_alias(alias: Path) -> Optional[Path]:
    """Authenticate one fixed macOS root alias before using its physical target."""

    if sys.platform != "darwin" or alias.parent != Path(alias.anchor):
        return None
    expected = _DARWIN_STANDARD_ROOT_ALIASES.get(alias.name)
    if expected is None:
        return None
    try:
        alias_before = os.lstat(alias)
        root_before = os.lstat(alias.parent)
        target_text = os.readlink(alias)
        target = Path(os.path.abspath(os.path.join(os.fspath(alias.parent), target_text)))
        target_before = os.lstat(expected)
        followed_before = os.stat(alias)
        resolved = Path(os.path.realpath(os.fspath(alias)))
        alias_after = os.lstat(alias)
        root_after = os.lstat(alias.parent)
        target_after = os.lstat(expected)
        followed_after = os.stat(alias)
        target_text_after = os.readlink(alias)
    except OSError:
        return None
    if (
        target != expected
        or resolved != expected
        or target_text_after != target_text
        or not stat.S_ISLNK(alias_before.st_mode)
        or int(getattr(alias_before, "st_uid", -1)) != 0
        or _darwin_alias_signature(alias_before) != _darwin_alias_signature(alias_after)
        or int(getattr(root_before, "st_uid", -1)) != 0
        or not stat.S_ISDIR(root_before.st_mode)
        or stat.S_IMODE(root_before.st_mode) & 0o022
        or _darwin_alias_signature(root_before) != _darwin_alias_signature(root_after)
        or int(getattr(target_before, "st_uid", -1)) != 0
        or not stat.S_ISDIR(target_before.st_mode)
        or stat.S_ISLNK(target_before.st_mode)
        or is_reparse_point(target_before)
        or _darwin_alias_signature(target_before) != _darwin_alias_signature(target_after)
        or (int(target_before.st_dev), int(target_before.st_ino))
        != (int(followed_before.st_dev), int(followed_before.st_ino))
        or (int(target_after.st_dev), int(target_after.st_ino))
        != (int(followed_after.st_dev), int(followed_after.st_ino))
    ):
        return None
    return expected


def _canonicalize_authenticated_root_alias(path: Path) -> Path:
    parts = path.parts[1:] if path.anchor else path.parts
    if not parts:
        return path
    lexical_root_entry = Path(path.anchor).joinpath(parts[0])
    authenticated = _authenticated_darwin_root_alias(lexical_root_entry)
    if authenticated is None:
        return path
    return authenticated.joinpath(*parts[1:])


def _absolute_path(path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    return _canonicalize_authenticated_root_alias(absolute)


def _directory_prune_detail(directory_pruner, path):
    if directory_pruner is None:
        return None
    decision = directory_pruner(path)
    if not decision:
        return None
    if isinstance(decision, str):
        return decision
    return "directory intentionally pruned by policy"


def _is_within(candidate: Path, allowed_root: Path) -> bool:
    candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    allowed_text = os.path.normcase(os.path.abspath(os.fspath(allowed_root)))
    try:
        return os.path.commonpath((candidate_text, allowed_text)) == allowed_text
    except ValueError:
        return False


def _stat_no_follow(path):
    try:
        return os.stat(str(path), follow_symlinks=False), None
    except OSError as error:
        return None, error


def _validate_boundary(path):
    boundary_stat, error = _stat_no_follow(path)
    if error is not None:
        return error
    if stat.S_ISLNK(boundary_stat.st_mode):
        return OSError("allowed root is a symbolic link")
    if is_reparse_point(boundary_stat):
        return OSError("allowed root is a junction or reparse point")
    if not stat.S_ISDIR(boundary_stat.st_mode):
        return NotADirectoryError("allowed root is not a directory")
    return None


@contextmanager
def _scandir_no_follow(path, expected_identity, identity_getter):
    """Open a POSIX directory without following its final path component."""

    can_use_directory_fd = (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.scandir in getattr(os, "supports_fd", set())
    )
    if not can_use_directory_fd:
        with os.scandir(str(path)) as entries:
            yield entries
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(str(path), flags)
    try:
        handle_stat = os.fstat(descriptor)
        handle_identity = identity_getter(path, follow_symlinks=False, stat_result=handle_stat)
        comparison = same_physical_file(expected_identity, handle_identity)
        if comparison.verdict != IdentityVerdict.SAME:
            raise _DirectoryHandleChanged(handle_identity, comparison.reason)
        with os.scandir(descriptor) as entries:
            yield entries
    finally:
        os.close(descriptor)


def _is_mount_boundary(path, identity, root_identity):
    if identity.volume_key != root_identity.volume_key:
        return True
    try:
        return os.path.ismount(str(path))
    except OSError:
        # The identity comparison remains valid even if the optional mount-point
        # probe is unsupported by a network filesystem.
        return False


def _find_unsafe_component(candidate, allowed_root):
    """Find a link/reparse component below the trusted allowed-root anchor."""

    relative = os.path.relpath(os.fspath(candidate), os.fspath(allowed_root))
    if relative == os.curdir:
        return None
    current = allowed_root
    for component in Path(relative).parts:
        current = current.joinpath(component)
        component_stat, error = _stat_no_follow(current)
        if error is not None:
            return current, WalkEventKind.ERROR, error
        if stat.S_ISLNK(component_stat.st_mode):
            return current, WalkEventKind.SYMLINK_SKIPPED, None
        if is_reparse_point(component_stat):
            return current, WalkEventKind.REPARSE_POINT_SKIPPED, None
    return None


def _find_unsafe_ancestor_component(path):
    """Validate every named component from the filesystem anchor to ``path``."""

    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    components = absolute.parts[1:] if absolute.anchor else absolute.parts
    for component in components:
        current = current.joinpath(component)
        component_stat, error = _stat_no_follow(current)
        if error is not None:
            return current, WalkEventKind.ERROR, error
        if stat.S_ISLNK(component_stat.st_mode):
            return current, WalkEventKind.SYMLINK_SKIPPED, None
        if is_reparse_point(component_stat):
            return current, WalkEventKind.REPARSE_POINT_SKIPPED, None
    return None


def _directory_changed_event(path, expected_identity, identity_getter):
    current_stat, error = _stat_no_follow(path)
    if error is not None:
        return WalkEvent(
            WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
            path,
            error=_make_walk_error("restat directory", error),
            detail="directory disappeared or became inaccessible before traversal",
        )
    if stat.S_ISLNK(current_stat.st_mode) or is_reparse_point(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
        return WalkEvent(
            WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
            path,
            detail="directory type changed before traversal",
        )
    try:
        current_identity = identity_getter(path, follow_symlinks=False, stat_result=current_stat)
    except FileIdentityError as error:
        return WalkEvent(
            WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
            path,
            error=_make_walk_error("identify directory", error),
            detail="directory identity could not be revalidated",
        )
    comparison = same_physical_file(expected_identity, current_identity)
    if comparison.verdict != IdentityVerdict.SAME:
        return WalkEvent(
            WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
            path,
            identity=current_identity,
            detail=comparison.reason,
        )
    return None


def _make_walk_error(operation, error):
    cause = error.cause if isinstance(error, FileIdentityError) and error.cause is not None else error
    return WalkError(
        operation=operation,
        error_type=type(cause).__name__,
        message=str(cause),
        errno=getattr(cause, "errno", None),
        winerror=getattr(cause, "winerror", None),
    )


def _completion_events(root_path, coverage):
    snapshot = coverage.snapshot()
    yield WalkEvent(WalkEventKind.COVERAGE, root_path, coverage=snapshot)
    yield WalkEvent(WalkEventKind.ROOT_COMPLETED, root_path, coverage=snapshot)


__all__ = [
    "DirectoryPruner",
    "WalkCoverage",
    "WalkError",
    "WalkEvent",
    "WalkEventKind",
    "is_reparse_point",
    "walk_no_follow",
]
