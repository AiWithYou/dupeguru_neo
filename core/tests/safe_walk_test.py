# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os
import stat
import sys

from dataclasses import replace
from pathlib import Path

import pytest

from hscommon.plat import ISWINDOWS

import core.safe_walk as safe_walk_module
from core.file_identity import FileIdentityError, get_file_identity
from core.safe_walk import WalkEventKind, is_reparse_point, walk_no_follow


def _events_of_kind(events, kind):
    return [event for event in events if event.kind == kind]


def _coverage(events):
    coverage_events = _events_of_kind(events, WalkEventKind.COVERAGE)
    assert len(coverage_events) == 1
    return coverage_events[0].coverage


def test_walk_emits_identified_files_directories_and_complete_coverage(tmpdir):
    root = Path(str(tmpdir))
    root.joinpath("first.txt").write_text("first")
    child = root.joinpath("child")
    child.mkdir()
    child.joinpath("second.txt").write_text("second")

    events = list(walk_no_follow(root))

    files = _events_of_kind(events, WalkEventKind.FILE)
    directories = _events_of_kind(events, WalkEventKind.DIRECTORY)
    assert {event.path.name for event in files} == {"first.txt", "second.txt"}
    assert {event.path.name for event in directories} == {root.name, "child"}
    assert all(event.identity is not None for event in files + directories)
    coverage = _coverage(events)
    assert coverage.files == 2
    assert coverage.directories == 2
    assert coverage.entries_seen == 3
    assert coverage.errors == 0
    assert coverage.identity_failures == 0
    assert coverage.complete
    assert events[0].kind == WalkEventKind.ROOT_STARTED
    assert events[-1].kind == WalkEventKind.ROOT_COMPLETED
    assert events[-1].coverage == coverage


def test_walk_switches_to_an_authenticated_root_alias_target(tmp_path, monkeypatch):
    lexical_root = Path(tmp_path.anchor) / "dupeguru-lexical-root-alias"
    canonical_root = tmp_path / "canonical-root"
    canonical_root.mkdir()
    lexical_path = lexical_root / "nested"
    canonical_path = canonical_root / "nested"
    canonical_path.mkdir()
    canonical_path.joinpath("duplicate.bin").write_bytes(b"payload")
    observed = []

    def authenticate(candidate):
        observed.append(candidate)
        if candidate == lexical_root:
            return canonical_root
        return None

    monkeypatch.setattr(
        safe_walk_module,
        "_authenticated_darwin_root_alias",
        authenticate,
    )

    events = list(walk_no_follow(lexical_path))

    assert observed[0] == lexical_root
    assert [event.path for event in _events_of_kind(events, WalkEventKind.FILE)] == [canonical_path / "duplicate.bin"]
    assert _coverage(events).complete


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS standard root aliases")
def test_walk_accepts_authenticated_darwin_var_alias(tmp_path):
    canonical_root = tmp_path.resolve(strict=True)
    try:
        relative = canonical_root.relative_to(Path("/private/var"))
    except ValueError:
        pytest.skip("temporary directory is not below /private/var")
    lexical_root = Path("/var").joinpath(relative)
    file_path = canonical_root / "duplicate.bin"
    file_path.write_bytes(b"darwin alias payload")

    events = list(walk_no_follow(lexical_root))

    assert [event.path for event in _events_of_kind(events, WalkEventKind.FILE)] == [file_path]
    assert _coverage(events).complete


def test_intentional_directory_prune_happens_before_enumeration_and_keeps_complete_coverage(tmpdir, monkeypatch):
    root = Path(str(tmpdir))
    pruned = root.joinpath("pruned")
    pruned.mkdir()
    hidden = pruned.joinpath("hidden.txt")
    hidden.touch()
    real_scandir = os.scandir

    def guarded_scandir(path):
        if not isinstance(path, int) and Path(path) == pruned:
            raise AssertionError("the intentionally pruned directory was enumerated")
        return real_scandir(path)

    monkeypatch.setattr(safe_walk_module.os, "scandir", guarded_scandir)

    events = list(
        walk_no_follow(
            root,
            directory_pruner=lambda path: "test exclusion policy" if path == pruned else None,
        )
    )

    pruned_events = _events_of_kind(events, WalkEventKind.DIRECTORY_PRUNED)
    assert [event.path for event in pruned_events] == [pruned]
    assert pruned_events[0].identity is not None
    assert pruned_events[0].detail == "test exclusion policy"
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.directories == 2
    assert coverage.pruned_directories == 1
    assert coverage.errors == 0
    assert coverage.complete


def test_symlink_to_outside_directory_is_never_followed(tmpdir):
    base = Path(str(tmpdir))
    root = base.joinpath("root")
    outside = base.joinpath("outside")
    root.mkdir()
    outside.mkdir()
    outside.joinpath("secret.txt").write_text("secret")
    link = root.joinpath("escape")
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))

    events = list(walk_no_follow(root))

    assert not _events_of_kind(events, WalkEventKind.FILE)
    skipped = _events_of_kind(events, WalkEventKind.SYMLINK_SKIPPED)
    assert [event.path for event in skipped] == [link]
    coverage = _coverage(events)
    assert coverage.skipped_symlinks == 1
    assert not coverage.complete


def test_symlink_root_is_rejected_without_following(tmpdir):
    base = Path(str(tmpdir))
    target = base.joinpath("target")
    target.mkdir()
    target.joinpath("secret.txt").write_text("secret")
    link = base.joinpath("root-link")
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))

    events = list(walk_no_follow(link, allowed_root=base))

    assert _events_of_kind(events, WalkEventKind.SYMLINK_SKIPPED)
    assert not _events_of_kind(events, WalkEventKind.FILE)


def test_symlink_component_between_allowed_root_and_root_is_rejected(tmpdir):
    base = Path(str(tmpdir))
    allowed = base.joinpath("allowed")
    outside = base.joinpath("outside")
    allowed.mkdir()
    outside.mkdir()
    nested = outside.joinpath("nested")
    nested.mkdir()
    nested.joinpath("secret.txt").write_text("secret")
    link = allowed.joinpath("escape")
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))

    events = list(walk_no_follow(link.joinpath("nested"), allowed_root=allowed))

    skipped = _events_of_kind(events, WalkEventKind.SYMLINK_SKIPPED)
    assert [event.path for event in skipped] == [link]
    assert not _events_of_kind(events, WalkEventKind.FILE)


def test_symlink_component_above_default_allowed_root_is_rejected(tmpdir):
    base = Path(str(tmpdir))
    target = base.joinpath("target")
    nested = target.joinpath("nested")
    nested.mkdir(parents=True)
    nested.joinpath("secret.txt").write_text("secret")
    link = base.joinpath("linked-parent")
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))

    events = list(walk_no_follow(link.joinpath("nested")))

    skipped = _events_of_kind(events, WalkEventKind.SYMLINK_SKIPPED)
    assert [event.path for event in skipped] == [link]
    assert not _events_of_kind(events, WalkEventKind.FILE)
    assert not _coverage(events).complete


def test_root_outside_allowed_boundary_is_explicitly_skipped(tmpdir):
    base = Path(str(tmpdir))
    allowed = base.joinpath("allowed")
    outside = base.joinpath("outside")
    allowed.mkdir()
    outside.mkdir()
    outside.joinpath("secret.txt").touch()

    events = list(walk_no_follow(outside, allowed_root=allowed))

    skipped = _events_of_kind(events, WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED)
    assert [event.path for event in skipped] == [outside]
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.skipped_outside_root == 1
    assert not coverage.complete


def test_same_directory_identity_is_reported_as_cycle(tmpdir):
    root = Path(str(tmpdir))
    child = root.joinpath("child")
    child.mkdir()
    child.joinpath("hidden.txt").touch()
    root_identity = get_file_identity(root)

    def cycle_identity(path, **kwargs):
        if Path(path) == child:
            return root_identity
        return get_file_identity(path, **kwargs)

    events = list(walk_no_follow(root, identity_getter=cycle_identity))

    cycles = _events_of_kind(events, WalkEventKind.CYCLE_SKIPPED)
    assert [event.path for event in cycles] == [child]
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.skipped_cycles == 1
    assert not coverage.complete


def test_different_volume_is_not_crossed_by_default(tmpdir):
    root = Path(str(tmpdir))
    child = root.joinpath("mounted")
    child.mkdir()
    child.joinpath("hidden.txt").touch()

    def mounted_identity(path, **kwargs):
        identity = get_file_identity(path, **kwargs)
        if Path(path) == child:
            return replace(identity, volume_id=identity.volume_id + 1)
        return identity

    events = list(walk_no_follow(root, identity_getter=mounted_identity))

    skipped = _events_of_kind(events, WalkEventKind.MOUNT_SKIPPED)
    assert [event.path for event in skipped] == [child]
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.skipped_mounts == 1
    assert not coverage.complete


def test_cross_mounts_requires_explicit_opt_in(tmpdir):
    root = Path(str(tmpdir))
    child = root.joinpath("mounted")
    child.mkdir()
    file_path = child.joinpath("visible.txt")
    file_path.touch()

    def mounted_identity(path, **kwargs):
        identity = get_file_identity(path, **kwargs)
        if Path(path) == child:
            return replace(identity, volume_id=identity.volume_id + 1)
        return identity

    events = list(walk_no_follow(root, cross_mounts=True, identity_getter=mounted_identity))

    files = _events_of_kind(events, WalkEventKind.FILE)
    assert [event.path for event in files] == [file_path]
    assert not _events_of_kind(events, WalkEventKind.MOUNT_SKIPPED)


def test_explicit_mount_point_is_not_crossed_even_when_volume_id_matches(tmpdir, monkeypatch):
    root = Path(str(tmpdir))
    mounted = root.joinpath("mounted")
    mounted.mkdir()
    mounted.joinpath("hidden.txt").touch()
    original_ismount = os.path.ismount

    def fake_ismount(path):
        return Path(path) == mounted or original_ismount(path)

    monkeypatch.setattr(os.path, "ismount", fake_ismount)

    events = list(walk_no_follow(root))

    skipped = _events_of_kind(events, WalkEventKind.MOUNT_SKIPPED)
    assert [event.path for event in skipped] == [mounted]
    assert not _events_of_kind(events, WalkEventKind.FILE)


def test_identity_failure_is_an_error_and_file_is_not_emitted(tmpdir):
    root = Path(str(tmpdir))
    denied = root.joinpath("denied.txt")
    denied.touch()

    def failing_identity(path, **kwargs):
        if Path(path) == denied:
            raise FileIdentityError(path, "test identity failure", PermissionError("denied"))
        return get_file_identity(path, **kwargs)

    events = list(walk_no_follow(root, identity_getter=failing_identity))

    errors = _events_of_kind(events, WalkEventKind.ERROR)
    assert len(errors) == 1
    assert errors[0].path == denied
    assert errors[0].error.operation == "identify entry"
    assert errors[0].error.error_type == "PermissionError"
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.errors == 1
    assert coverage.identity_failures == 1
    assert not coverage.complete


def test_directory_access_error_is_reported_in_coverage(tmpdir, monkeypatch):
    root = Path(str(tmpdir))
    denied = root.joinpath("denied")
    denied.mkdir()
    denied.joinpath("hidden.txt").touch()
    real_scandir = os.scandir

    def failing_scandir(path):
        if Path(path) == denied:
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(safe_walk_module.os, "scandir", failing_scandir)

    events = list(walk_no_follow(root))

    errors = _events_of_kind(events, WalkEventKind.ERROR)
    assert len(errors) == 1
    assert errors[0].path == denied
    assert errors[0].error.operation == "scan directory"
    assert errors[0].error.errno == 13
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.errors == 1
    assert coverage.identity_failures == 0
    assert not coverage.complete


def test_changed_directory_identity_is_not_traversed(tmpdir):
    root = Path(str(tmpdir))
    child = root.joinpath("child")
    child.mkdir()
    child.joinpath("hidden.txt").touch()
    calls = {}

    def changing_identity(path, **kwargs):
        path = Path(path)
        identity = get_file_identity(path, **kwargs)
        calls[path] = calls.get(path, 0) + 1
        if path == child and calls[path] > 1:
            return replace(identity, file_id=_different_file_id(identity.file_id))
        return identity

    events = list(walk_no_follow(root, identity_getter=changing_identity))

    changed = _events_of_kind(events, WalkEventKind.DIRECTORY_CHANGED_SKIPPED)
    assert [event.path for event in changed] == [child]
    assert not _events_of_kind(events, WalkEventKind.FILE)
    coverage = _coverage(events)
    assert coverage.skipped_changed_directories == 1
    assert not coverage.complete


def _different_file_id(file_id):
    if isinstance(file_id, bytes):
        return bytes([file_id[0] ^ 0xFF]) + file_id[1:]
    return file_id + 1


@pytest.mark.skipif(ISWINDOWS, reason="POSIX special-file test")
def test_special_file_is_skipped_explicitly(tmpdir):
    root = Path(str(tmpdir))
    fifo = root.joinpath("named-pipe")
    os.mkfifo(str(fifo))

    events = list(walk_no_follow(root))

    skipped = _events_of_kind(events, WalkEventKind.SPECIAL_FILE_SKIPPED)
    assert [event.path for event in skipped] == [fifo]
    assert _coverage(events).skipped_special_files == 1


@pytest.mark.skipif(not ISWINDOWS, reason="Windows reparse attribute test")
def test_windows_reparse_attribute_is_detected():
    class FakeStat:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    assert is_reparse_point(FakeStat())
