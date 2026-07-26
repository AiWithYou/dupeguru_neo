# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os
import io
import time
import tempfile
import shutil

from contextlib import contextmanager

import pytest
from pytest import raises
from pathlib import Path
from hscommon.testutil import eq_
from hscommon.plat import ISWINDOWS
from hscommon.jobprogress.job import JobCancelled

import core.safe_walk as safe_walk_module
import core.directories as directories_module
from core.fs import File
from core.directories import (
    Directories,
    DirectoryState,
    AlreadyThereError,
    DirectDiscoveryBudget,
    DirectDiscoveryLimits,
    DirectDiscoveryResourceError,
    DirectoriesLoadError,
    DirectoriesSaveError,
    InvalidPathError,
)
from core.exclude import ExcludeList, ExcludeDict
from core.reserved_paths import RESERVED_INTERNAL_DIRECTORY_NAMES


def create_fake_fs(rootpath):
    # We have it as a separate function because other units are using it.
    rootpath = rootpath.joinpath("fs")
    rootpath.mkdir()
    rootpath.joinpath("dir1").mkdir()
    rootpath.joinpath("dir2").mkdir()
    rootpath.joinpath("dir3").mkdir()
    with rootpath.joinpath("file1.test").open("wt") as fp:
        fp.write("1")
    with rootpath.joinpath("file2.test").open("wt") as fp:
        fp.write("12")
    with rootpath.joinpath("file3.test").open("wt") as fp:
        fp.write("123")
    with rootpath.joinpath("dir1", "file1.test").open("wt") as fp:
        fp.write("1")
    with rootpath.joinpath("dir2", "file2.test").open("wt") as fp:
        fp.write("12")
    with rootpath.joinpath("dir3", "file3.test").open("wt") as fp:
        fp.write("123")
    return rootpath


testpath = None


def setup_module(module):
    # In this unit, we have tests depending on two directory structure. One with only one file in it
    # and another with a more complex structure.
    testpath = Path(tempfile.mkdtemp())
    module.testpath = testpath
    rootpath = testpath.joinpath("onefile")
    rootpath.mkdir()
    with rootpath.joinpath("test.txt").open("wt") as fp:
        fp.write("test_data")
    create_fake_fs(testpath)


def teardown_module(module):
    shutil.rmtree(str(module.testpath))


def test_empty():
    d = Directories()
    eq_(len(d), 0)
    assert "foobar" not in d


def test_add_path():
    d = Directories()
    p = testpath.joinpath("onefile")
    d.add_path(p)
    eq_(1, len(d))
    assert p in d
    assert (p.joinpath("foobar")) in d
    assert p.parent not in d
    p = testpath.joinpath("fs")
    d.add_path(p)
    eq_(2, len(d))
    assert p in d


def test_add_path_when_path_is_already_there():
    d = Directories()
    p = testpath.joinpath("onefile")
    d.add_path(p)
    with raises(AlreadyThereError):
        d.add_path(p)
    with raises(AlreadyThereError):
        d.add_path(p.joinpath("foobar"))
    eq_(1, len(d))


def test_add_path_containing_paths_already_there():
    d = Directories()
    d.add_path(testpath.joinpath("onefile"))
    eq_(1, len(d))
    d.add_path(testpath)
    eq_(len(d), 1)
    eq_(d[0], testpath)


def test_add_path_non_latin(tmpdir):
    p = Path(str(tmpdir))
    to_add = p.joinpath("unicode\u201a")
    os.mkdir(str(to_add))
    d = Directories()
    try:
        d.add_path(to_add)
    except UnicodeDecodeError:
        assert False


def test_del():
    d = Directories()
    d.add_path(testpath.joinpath("onefile"))
    try:
        del d[1]
        assert False
    except IndexError:
        pass
    d.add_path(testpath.joinpath("fs"))
    del d[1]
    eq_(1, len(d))


def test_states():
    d = Directories()
    p = testpath.joinpath("onefile")
    d.add_path(p)
    eq_(DirectoryState.NORMAL, d.get_state(p))
    d.set_state(p, DirectoryState.REFERENCE)
    eq_(DirectoryState.REFERENCE, d.get_state(p))
    eq_(DirectoryState.REFERENCE, d.get_state(p.joinpath("dir1")))
    eq_(1, len(d.states))
    eq_(p, list(d.states.keys())[0])
    eq_(DirectoryState.REFERENCE, d.states[p])


def test_get_state_with_path_not_there():
    # When the path's not there, just return DirectoryState.Normal
    d = Directories()
    d.add_path(testpath.joinpath("onefile"))
    eq_(d.get_state(testpath), DirectoryState.NORMAL)


def test_states_overwritten_when_larger_directory_eat_smaller_ones():
    # ref #248
    # When setting the state of a folder, we overwrite previously set states for subfolders.
    d = Directories()
    p = testpath.joinpath("onefile")
    d.add_path(p)
    d.set_state(p, DirectoryState.EXCLUDED)
    d.add_path(testpath)
    d.set_state(testpath, DirectoryState.REFERENCE)
    eq_(d.get_state(p), DirectoryState.REFERENCE)
    eq_(d.get_state(p.joinpath("dir1")), DirectoryState.REFERENCE)
    eq_(d.get_state(testpath), DirectoryState.REFERENCE)


def test_get_files():
    d = Directories()
    p = testpath.joinpath("fs")
    d.add_path(p)
    d.set_state(p.joinpath("dir1"), DirectoryState.REFERENCE)
    d.set_state(p.joinpath("dir2"), DirectoryState.EXCLUDED)
    files = list(d.get_files())
    eq_(5, len(files))
    for f in files:
        if f.path.parent == p.joinpath("dir1"):
            assert f.is_ref
        else:
            assert not f.is_ref


def test_directory_state_persisted_values_remain_compatible():
    assert DirectoryState.NORMAL == 0
    assert DirectoryState.REFERENCE == 1
    assert DirectoryState.EXCLUDED == 2
    assert DirectoryState.COMPARE_ONLY == 3


def test_get_files_assigns_comparison_pools_and_protects_non_incoming_files(tmpdir):
    root = Path(str(tmpdir))
    incoming = root.joinpath("incoming.txt")
    incoming.touch()
    protected_dir = root.joinpath("protected")
    protected_dir.mkdir()
    protected = protected_dir.joinpath("protected.txt")
    protected.touch()
    compare_only_dir = root.joinpath("compare-only")
    compare_only_dir.mkdir()
    compare_only = compare_only_dir.joinpath("compare-only.txt")
    compare_only.touch()

    d = Directories()
    d.add_path(root)
    d.set_state(protected_dir, DirectoryState.REFERENCE)
    d.set_state(compare_only_dir, DirectoryState.COMPARE_ONLY)

    files = {file.path: file for file in d.get_files()}

    assert files[incoming].comparison_pool == "incoming"
    assert not files[incoming].is_ref
    assert files[protected].comparison_pool == "protected"
    assert files[protected].is_ref
    assert files[compare_only].comparison_pool == "compare_only"
    assert files[compare_only].is_ref


def test_compare_only_state_round_trips_through_directory_xml(tmpdir):
    root = Path(str(tmpdir))
    compare_only_dir = root.joinpath("compare-only")
    compare_only_dir.mkdir()
    state_file = root.joinpath("directories.xml")
    first = Directories()
    first.add_path(root)
    first.set_state(compare_only_dir, DirectoryState.COMPARE_ONLY)

    first.save_to_file(state_file)
    second = Directories()
    second.load_from_file(state_file)

    assert second.get_state(compare_only_dir) == DirectoryState.COMPARE_ONLY


def test_get_files_does_not_duplicate_ordinary_events_and_retains_complete_coverage(tmpdir):
    root = Path(str(tmpdir))
    file_path = root.joinpath("file.txt")
    file_path.touch()
    d = Directories()
    d.add_path(root)

    files = list(d.get_files())

    assert [file.path for file in files] == [file_path]
    assert d.last_walk_events[root] == []
    assert not d.last_walk_events_truncated
    assert d.last_walk_coverages[root].complete
    assert d.last_walk_errors[root] == ()


def test_excluded_directory_is_pruned_without_making_coverage_incomplete(tmpdir):
    root = Path(str(tmpdir))
    visible = root.joinpath("visible.txt")
    visible.touch()
    excluded = root.joinpath("excluded")
    excluded.mkdir()
    excluded.joinpath("hidden.txt").touch()
    d = Directories()
    d.add_path(root)
    d.set_state(excluded, DirectoryState.EXCLUDED)

    files = list(d.get_files())

    assert [file.path for file in files] == [visible]
    assert d.last_walk_events[root] == []
    coverage = d.last_walk_coverages[root]
    assert coverage.pruned_directories == 1
    assert coverage.errors == 0
    assert coverage.complete


def test_exclude_list_path_rule_prunes_matching_directory(tmpdir):
    root = Path(str(tmpdir))
    excluded = root.joinpath("excluded")
    excluded.mkdir()
    excluded.joinpath("hidden.txt").touch()
    exclude_list = ExcludeList(union_regex=False)
    regex = r".*[\\/]excluded$"
    exclude_list.add(regex)
    exclude_list.mark(regex)
    d = Directories(exclude_list=exclude_list)
    d.add_path(root)

    assert not list(d.get_files())

    assert d.last_walk_events[root] == []
    assert d.last_walk_coverages[root].complete


def test_normal_child_override_is_scanned_below_excluded_parent(tmpdir):
    root = Path(str(tmpdir))
    excluded = root.joinpath("excluded")
    excluded.mkdir()
    excluded.joinpath("ignored.txt").touch()
    included = excluded.joinpath("included")
    included.mkdir()
    included_file = included.joinpath("included.txt")
    included_file.touch()
    d = Directories()
    d.add_path(root)
    d.set_state(excluded, DirectoryState.EXCLUDED)
    d.set_state(included, DirectoryState.NORMAL)

    files = list(d.get_files())

    assert [file.path for file in files] == [included_file]
    assert d.last_walk_coverages[root].complete


@pytest.mark.parametrize("collector_name", ["get_files", "get_folders"])
def test_directory_pruning_indexes_non_excluded_override_ancestors_once(tmp_path, collector_name):
    class CountingStates(dict):
        def __init__(self, values):
            super().__init__(values)
            self.items_calls = 0

        def items(self):
            self.items_calls += 1
            return super().items()

    root = tmp_path / "root"
    excluded = root / "excluded"
    included = excluded / "included"
    sibling = excluded / "sibling"
    included.mkdir(parents=True)
    sibling.mkdir()
    (included / "included.txt").write_text("included", encoding="utf-8")
    (sibling / "excluded.txt").write_text("excluded", encoding="utf-8")
    directories = Directories()
    directories.add_path(root)
    directories.set_state(excluded, DirectoryState.EXCLUDED)
    directories.set_state(included, DirectoryState.NORMAL)
    counting_states = CountingStates(directories.states)
    directories.states = counting_states

    list(getattr(directories, collector_name)())

    assert counting_states.items_calls == 1
    assert directories.last_walk_coverages[root].complete


def test_override_ancestor_index_is_scoped_and_bounded_before_walk(tmp_path):
    root = tmp_path / "root"
    included = root / "one" / "two" / "three"
    included.mkdir(parents=True)
    directories = Directories()
    directories.add_path(root)
    directories.set_state(root, DirectoryState.EXCLUDED)
    directories.set_state(included, DirectoryState.NORMAL)
    # An unrelated override must not consume retained ancestor capacity.
    directories.states[tmp_path / "outside" / "irrelevant"] = DirectoryState.NORMAL
    budget = DirectDiscoveryBudget(_small_discovery_limits(max_folders=2))

    with pytest.raises(DirectDiscoveryResourceError) as caught:
        list(directories.get_files(budget=budget))

    assert caught.value.code == "resource-limit-folders"
    assert budget.events == 0
    assert budget.folders == 0


def test_get_files_reports_incomplete_coverage_and_explicit_walk_error(tmpdir, monkeypatch):
    root = Path(str(tmpdir))
    visible = root.joinpath("visible.txt")
    visible.touch()
    denied = root.joinpath("denied")
    denied.mkdir()
    denied.joinpath("hidden.txt").touch()
    real_scandir_no_follow = safe_walk_module._scandir_no_follow

    @contextmanager
    def failing_scandir_no_follow(path, expected_identity, identity_getter):
        if Path(path) == denied:
            raise PermissionError(13, "denied", str(path))
        with real_scandir_no_follow(path, expected_identity, identity_getter) as entries:
            yield entries

    monkeypatch.setattr(safe_walk_module, "_scandir_no_follow", failing_scandir_no_follow)
    d = Directories()
    d.add_path(root)

    files = list(d.get_files())

    assert [file.path for file in files] == [visible]
    coverage = d.last_walk_coverages[root]
    assert coverage.errors == 1
    assert not coverage.complete
    assert len(d.last_walk_errors[root]) == 1
    assert d.last_walk_errors[root][0].path == denied
    assert d.last_walk_errors[root][0].error.operation == "scan directory"


def test_get_files_checks_cancellation_for_every_walk_event(tmpdir):
    root = Path(str(tmpdir))
    root.joinpath("first.txt").touch()
    child = root.joinpath("child")
    child.mkdir()
    child.joinpath("second.txt").touch()

    class CountingJob:
        def __init__(self):
            self.checks = 0

        def check_if_cancelled(self):
            self.checks += 1

        def set_progress(self, progress, description):
            pass

    counting_job = CountingJob()
    d = Directories()
    d.add_path(root)
    budget = DirectDiscoveryBudget()

    list(d.get_files(j=counting_job, budget=budget))

    # One check precedes and one follows every event, plus one final check
    # before the StopIteration probe.
    assert counting_job.checks == 2 * budget.events + 1
    assert budget.events > len(d.last_walk_events[root])


def test_already_cancelled_job_never_advances_the_filesystem_walker(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    directories = Directories()
    directories.add_path(root)
    advanced = False

    def observed_walk(*_args, **_kwargs):
        nonlocal advanced
        advanced = True
        yield

    class CancelledJob:
        def check_if_cancelled(self):
            raise JobCancelled()

    monkeypatch.setattr(directories_module, "walk_no_follow", observed_walk)
    budget = DirectDiscoveryBudget(_small_discovery_limits())

    with pytest.raises(JobCancelled):
        list(directories.get_files(j=CancelledJob(), budget=budget))

    assert not advanced
    assert budget.events == 0


def _small_discovery_limits(*, max_files=10, max_folders=10, max_issues=10, max_seconds=60):
    return DirectDiscoveryLimits(
        max_files=max_files,
        max_folders=max_folders,
        max_issues=max_issues,
        max_seconds=max_seconds,
    )


def test_direct_discovery_defaults_match_public_cli_limits():
    assert directories_module.DIRECT_DISCOVERY_MAX_FILES == 1_000_000
    assert directories_module.DIRECT_DISCOVERY_MAX_FOLDERS == 250_000
    assert directories_module.DIRECT_DISCOVERY_MAX_ISSUES == 100_000
    assert directories_module.DIRECT_DISCOVERY_MAX_SECONDS == 14_400


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("max_files", 0),
        ("max_files", True),
        ("max_files", directories_module.DIRECT_DISCOVERY_MAX_FILES + 1),
        ("max_folders", 0),
        ("max_issues", 1.5),
        ("max_seconds", 0),
        ("max_seconds", float("inf")),
        ("max_seconds", directories_module.DIRECT_DISCOVERY_MAX_SECONDS + 1),
    ),
)
def test_direct_discovery_limits_reject_invalid_or_above_hard_cap_values(name, value):
    with pytest.raises(ValueError, match=name):
        DirectDiscoveryLimits(**{name: value})


def test_discovery_clock_rejects_boolean_and_non_monotonic_values():
    with pytest.raises(ValueError, match="clock"):
        DirectDiscoveryBudget(_small_discovery_limits(), clock=lambda: True)

    ticks = iter((2.0, 1.0))
    budget = DirectDiscoveryBudget(
        _small_discovery_limits(),
        clock=lambda: next(ticks),
    )
    with pytest.raises(DirectDiscoveryResourceError) as caught:
        budget.check_time()
    assert caught.value.code == "resource-limit-seconds"
    assert "non-monotonic" in str(caught.value)


@pytest.mark.parametrize(
    ("file_count", "limited"),
    (
        (2, False),
        (3, False),
        (4, True),
    ),
)
def test_file_limit_minus_one_at_limit_and_over_limit_are_exact(tmp_path, file_count, limited):
    root = tmp_path / "root"
    root.mkdir()
    for index in range(file_count):
        (root / "{}.txt".format(index)).write_text("value", encoding="utf-8")
    directories = Directories()
    directories.add_path(root)
    budget = DirectDiscoveryBudget(_small_discovery_limits(max_files=3))

    if limited:
        with pytest.raises(DirectDiscoveryResourceError) as caught:
            list(directories.get_files(budget=budget))
        assert caught.value.code == "resource-limit-files"
        assert budget.files == 3
    else:
        assert len(list(directories.get_files(budget=budget))) == file_count
        assert budget.files == file_count


def test_file_limit_is_checked_before_constructing_an_extra_wrapper(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "first.txt").touch()
    (root / "second.txt").touch()
    constructed = []

    class TrackingFile:
        @classmethod
        def can_handle(cls, _path):
            return True

        def __init__(self, path):
            self.path = path
            constructed.append(path)

    directories = Directories()
    directories.add_path(root)
    budget = DirectDiscoveryBudget(_small_discovery_limits(max_files=1))

    with pytest.raises(DirectDiscoveryResourceError) as caught:
        list(
            directories.get_files(
                fileclasses=[TrackingFile],
                budget=budget,
            )
        )

    assert caught.value.code == "resource-limit-files"
    assert len(constructed) == 1


@pytest.mark.parametrize(
    ("folder_count", "limited"),
    (
        (2, False),
        (3, False),
        (4, True),
    ),
)
def test_folder_limit_minus_one_at_limit_and_over_limit_are_exact(tmp_path, folder_count, limited):
    root = tmp_path / "root"
    root.mkdir()
    for index in range(folder_count - 1):
        (root / "folder-{}".format(index)).mkdir()
    directories = Directories()
    directories.add_path(root)
    budget = DirectDiscoveryBudget(_small_discovery_limits(max_folders=3))

    if limited:
        with pytest.raises(DirectDiscoveryResourceError) as caught:
            list(directories.get_folders(budget=budget))
        assert caught.value.code == "resource-limit-folders"
        assert budget.folders == 3
    else:
        assert len(list(directories.get_folders(budget=budget))) == folder_count
        assert budget.folders == folder_count


@pytest.mark.parametrize(
    ("issue_count", "limited"),
    (
        (1, False),
        (2, False),
        (3, True),
    ),
)
def test_issue_limit_is_a_hard_audit_storage_cap(tmp_path, issue_count, limited):
    directories = Directories()
    directories._dirs = [tmp_path / "missing-{}".format(index) for index in range(issue_count)]
    budget = DirectDiscoveryBudget(_small_discovery_limits(max_issues=2))

    if limited:
        with pytest.raises(DirectDiscoveryResourceError) as caught:
            list(directories.get_files(budget=budget))
        assert caught.value.code == "resource-limit-issues"
        assert directories.last_walk_events_truncated
    else:
        assert list(directories.get_files(budget=budget)) == []
        assert not directories.last_walk_events_truncated
    assert budget.issues == min(issue_count, 2)
    assert sum(len(events) for events in directories.last_walk_events.values()) == min(issue_count, 2)


def test_discovery_clock_is_sampled_around_walker_events_without_sleeping(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    directories = Directories()
    directories.add_path(root)
    ticks = iter((0.0, 0.0, 2.0))

    def clock():
        return next(ticks, 2.0)

    budget = DirectDiscoveryBudget(
        _small_discovery_limits(max_seconds=1),
        clock=clock,
    )

    with pytest.raises(DirectDiscoveryResourceError) as caught:
        list(directories.get_files(budget=budget))

    assert caught.value.code == "resource-limit-seconds"
    assert budget.events == 1
    assert budget.files == 0


def test_folder_postorder_materialization_remains_inside_time_budget(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "child").mkdir()
    directories = Directories()
    directories.add_path(root)
    now = [0.0]

    class SlowFolder:
        def __init__(self, path):
            self.path = path
            now[0] = 2.0

    budget = DirectDiscoveryBudget(
        _small_discovery_limits(max_seconds=1),
        clock=lambda: now[0],
    )

    with pytest.raises(DirectDiscoveryResourceError) as caught:
        list(directories.get_folders(folderclass=SlowFolder, budget=budget))

    assert caught.value.code == "resource-limit-seconds"
    assert budget.folders == 2


def test_discovery_memory_error_becomes_typed_resource_failure(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    directories = Directories()
    directories.add_path(root)
    budget = DirectDiscoveryBudget(_small_discovery_limits())

    def failing_walk(*_args, **_kwargs):
        raise MemoryError("synthetic exhaustion")
        yield  # pragma: no cover

    monkeypatch.setattr(directories_module, "walk_no_follow", failing_walk)

    with pytest.raises(DirectDiscoveryResourceError) as caught:
        list(directories.get_files(budget=budget))

    assert caught.value.code == "resource-limit-memory"
    assert budget.files == 0


def test_get_files_with_folders():
    # When fileclasses handle folders, return them and stop recursing!
    class FakeFile(File):
        @classmethod
        def can_handle(cls, path):
            return True

    d = Directories()
    p = testpath.joinpath("fs")
    d.add_path(p)
    files = list(d.get_files(fileclasses=[FakeFile]))
    # We have the 3 root files and the 3 root dirs
    eq_(6, len(files))


def test_get_folders():
    d = Directories()
    p = testpath.joinpath("fs")
    d.add_path(p)
    d.set_state(p.joinpath("dir1"), DirectoryState.REFERENCE)
    d.set_state(p.joinpath("dir2"), DirectoryState.EXCLUDED)
    folders = list(d.get_folders())
    eq_(len(folders), 3)
    ref = [f for f in folders if f.is_ref]
    not_ref = [f for f in folders if not f.is_ref]
    eq_(len(ref), 1)
    eq_(ref[0].path, p.joinpath("dir1"))
    eq_(len(not_ref), 2)
    eq_(ref[0].size, 1)


def test_file_and_folder_modes_never_follow_directory_symlinks(tmpdir):
    base = Path(str(tmpdir))
    root = base.joinpath("root")
    outside = base.joinpath("outside")
    root.mkdir()
    outside.mkdir()
    outside_file = outside.joinpath("secret.txt")
    outside_file.touch()
    link = root.joinpath("escape")
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))
    d = Directories()
    d.add_path(root)

    assert not list(d.get_files())
    file_coverage = d.last_walk_coverages[root]
    assert file_coverage.skipped_symlinks == 1
    assert not file_coverage.complete
    folders = list(d.get_folders())

    assert [folder.path for folder in folders] == [root]
    folder_coverage = d.last_walk_coverages[root]
    assert folder_coverage.skipped_symlinks == 1
    assert not folder_coverage.complete


def test_get_files_with_inherited_exclusion():
    d = Directories()
    p = testpath.joinpath("onefile")
    d.add_path(p)
    d.set_state(p, DirectoryState.EXCLUDED)
    eq_([], list(d.get_files()))


def test_save_and_load(tmpdir):
    d1 = Directories()
    d2 = Directories()
    p1 = Path(str(tmpdir.join("p1")))
    p1.mkdir()
    p2 = Path(str(tmpdir.join("p2")))
    p2.mkdir()
    d1.add_path(p1)
    d1.add_path(p2)
    d1.set_state(p1, DirectoryState.REFERENCE)
    d1.set_state(p1.joinpath("dir1"), DirectoryState.EXCLUDED)
    tmpxml = str(tmpdir.join("directories_testunit.xml"))
    d1.save_to_file(tmpxml)
    d2.load_from_file(tmpxml)
    eq_(2, len(d2))
    eq_(DirectoryState.REFERENCE, d2.get_state(p1))
    eq_(DirectoryState.EXCLUDED, d2.get_state(p1.joinpath("dir1")))


def test_invalid_path():
    d = Directories()
    p = Path("does_not_exist")
    with raises(InvalidPathError):
        d.add_path(p)
    eq_(0, len(d))


def test_regular_file_cannot_be_added_as_a_scan_root(tmp_path):
    candidate = tmp_path / "not-a-directory.txt"
    candidate.write_text("data", encoding="utf-8")
    d = Directories()

    with raises(InvalidPathError):
        d.add_path(candidate)

    eq_(0, len(d))


def test_set_state_on_invalid_path():
    d = Directories()
    try:
        d.set_state(
            Path(
                "foobar",
            ),
            DirectoryState.NORMAL,
        )
    except LookupError:
        assert False


def test_load_from_file_preserves_temporarily_unavailable_root(tmpdir):
    d1 = Directories()
    d1.add_path(testpath.joinpath("onefile"))
    p = Path(str(tmpdir.join("toremove")))
    p.mkdir()
    d1.add_path(p)
    p.rmdir()
    tmpxml = str(tmpdir.join("directories_testunit.xml"))
    d1.save_to_file(tmpxml)
    d2 = Directories()
    assert d2.load_from_file(tmpxml) is None
    assert list(d2) == [testpath.joinpath("onefile"), p]
    list(d2.get_files())
    assert d2.last_walk_coverages[p].errors == 1
    assert not d2.last_walk_coverages[p].complete


def test_invalid_directory_xml_preserves_roots_states_and_exclude_binding(tmp_path):
    old_root = tmp_path / "old"
    old_root.mkdir()
    exclude_list = ExcludeList(union_regex=False)
    directories = Directories(exclude_list=exclude_list)
    directories.add_path(old_root)
    directories.set_state(old_root, DirectoryState.REFERENCE)
    payload = (
        b'<directories><root_directory path="ignored">'
        b'<state path="nested" value="0"/></root_directory></directories>'
    )

    failure = directories.load_from_file(io.BytesIO(payload))

    assert isinstance(failure, DirectoriesLoadError)
    assert list(directories) == [old_root]
    assert directories.get_state(old_root) == DirectoryState.REFERENCE
    assert directories._exclude_list is exclude_list


def test_valid_directory_load_replaces_state_and_preserves_exclude_binding(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    exclude_list = ExcludeList(union_regex=False)
    directories = Directories(exclude_list=exclude_list)
    directories.add_path(old_root)
    payload = ('<directories><root_directory path="{}"/>' '<state path="{}" value="1"/></directories>').format(
        new_root, new_root
    )

    assert directories.load_from_file(io.BytesIO(payload.encode("utf-8"))) is None
    assert list(directories) == [new_root]
    assert directories.get_state(new_root) == DirectoryState.REFERENCE
    assert directories._exclude_list is exclude_list


def test_invalid_directory_state_is_typed_failure_without_partial_commit(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    directories = Directories()
    directories.add_path(old_root)
    payload = ('<directories><root_directory path="{}"/>' '<state path="{}" value="999"/></directories>').format(
        new_root, new_root
    )

    failure = directories.load_from_file(io.BytesIO(payload.encode("utf-8")))

    assert isinstance(failure, DirectoriesLoadError)
    assert list(directories) == [old_root]


def test_relative_or_internal_directory_path_is_rejected_transactionally(tmp_path):
    old_root = tmp_path / "old"
    old_root.mkdir()
    directories = Directories()
    directories.add_path(old_root)

    relative_failure = directories.load_from_file(
        io.BytesIO(b'<directories><root_directory path="relative"/></directories>')
    )
    internal_path = tmp_path / ".dupeguru-neo-quarantine"
    internal_failure = directories.load_from_file(
        io.BytesIO(('<directories><root_directory path="{}"/></directories>').format(internal_path).encode("utf-8"))
    )

    assert isinstance(relative_failure, DirectoriesLoadError)
    assert isinstance(internal_failure, DirectoriesLoadError)
    assert list(directories) == [old_root]


def test_directory_caller_specific_root_limit_is_transactional(tmp_path, monkeypatch):
    old_root = tmp_path / "old"
    first = tmp_path / "first"
    second = tmp_path / "second"
    old_root.mkdir()
    first.mkdir()
    second.mkdir()
    directories = Directories()
    directories.add_path(old_root)
    monkeypatch.setattr(directories_module, "DIRECTORIES_XML_MAX_ROOTS", 1)
    payload = ('<directories><root_directory path="{}"/>' '<root_directory path="{}"/></directories>').format(
        first, second
    )

    failure = directories.load_from_file(io.BytesIO(payload.encode("utf-8")))

    assert isinstance(failure, DirectoriesLoadError)
    assert list(directories) == [old_root]


@pytest.mark.parametrize(
    "violated_limit",
    ["roots", "states", "path_chars", "document_bytes", "total_chars"],
)
def test_directory_save_rejects_loader_limit_violation_atomically(tmp_path, monkeypatch, violated_limit):
    directories = Directories()
    root = tmp_path / "root"
    root.mkdir()
    directories.add_path(root)
    if violated_limit == "roots":
        second = tmp_path / "second"
        second.mkdir()
        directories.add_path(second)
        monkeypatch.setattr(directories_module, "DIRECTORIES_XML_MAX_ROOTS", 1)
    elif violated_limit == "states":
        directories.set_state(root / "first", DirectoryState.REFERENCE)
        directories.set_state(root / "second", DirectoryState.COMPARE_ONLY)
        monkeypatch.setattr(directories_module, "DIRECTORIES_XML_MAX_STATES", 1)
    elif violated_limit == "path_chars":
        monkeypatch.setattr(
            directories_module,
            "DIRECTORIES_XML_MAX_PATH_CHARS",
            len(str(root)) - 1,
        )
    elif violated_limit == "document_bytes":
        monkeypatch.setattr(directories_module, "DIRECTORIES_XML_MAX_BYTES", 16)
    else:
        monkeypatch.setattr(directories_module, "DIRECTORIES_XML_MAX_TOTAL_CHARS", 8)
    destination = tmp_path / "directories.xml"
    original = b"existing directory selection"
    destination.write_bytes(original)

    with pytest.raises(DirectoriesSaveError):
        directories.save_to_file(destination)

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob(".directories.xml.*.tmp"))


def test_unicode_save(tmpdir):
    d = Directories()
    p1 = Path(str(tmpdir), "hello\xe9")
    p1.mkdir()
    p1.joinpath("foo\xe9").mkdir()
    d.add_path(p1)
    d.set_state(p1.joinpath("foo\xe9"), DirectoryState.EXCLUDED)
    tmpxml = str(tmpdir.join("directories_testunit.xml"))
    try:
        d.save_to_file(tmpxml)
    except UnicodeDecodeError:
        assert False


def test_get_files_refreshes_its_directories():
    d = Directories()
    p = testpath.joinpath("fs")
    d.add_path(p)
    files = d.get_files()
    eq_(6, len(list(files)))
    time.sleep(1)
    os.remove(str(p.joinpath("dir1", "file1.test")))
    files = d.get_files()
    eq_(5, len(list(files)))


def test_get_files_does_not_choke_on_non_existing_directories(tmpdir):
    d = Directories()
    p = Path(str(tmpdir))
    d.add_path(p)
    shutil.rmtree(str(p))
    eq_([], list(d.get_files()))


def test_get_state_returns_excluded_by_default_for_hidden_directories(tmpdir):
    d = Directories()
    p = Path(str(tmpdir))
    hidden_dir_path = p.joinpath(".foo")
    p.joinpath(".foo").mkdir()
    d.add_path(p)
    eq_(d.get_state(hidden_dir_path), DirectoryState.EXCLUDED)
    # But it can be overriden
    d.set_state(hidden_dir_path, DirectoryState.NORMAL)
    eq_(d.get_state(hidden_dir_path), DirectoryState.NORMAL)


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES | {name.upper() for name in RESERVED_INTERNAL_DIRECTORY_NAMES}),
)
def test_internal_directories_cannot_be_overridden_or_selected(tmp_path, reserved_name):
    root = tmp_path / "library"
    root.mkdir()
    reserved = root / reserved_name
    reserved.mkdir()
    hidden = reserved / "hidden.bin"
    hidden.write_bytes(b"hidden")
    temporary = root / ".image.png.dupeguru-ABCDEF012345-000001.TMP"
    temporary.write_bytes(b"temporary")
    visible = root / "visible.bin"
    visible.write_bytes(b"visible")
    directories = Directories()
    directories.add_path(root)
    directories.set_state(reserved, DirectoryState.NORMAL)

    files = list(directories.get_files(fileclasses=[File]))

    assert {file.path for file in files} == {visible}
    with pytest.raises(InvalidPathError):
        directories.add_path(reserved)


def test_default_path_state_override(tmpdir):
    # It's possible for a subclass to override the default state of a path
    class MyDirectories(Directories):
        def _default_state_for_path(self, path):
            if "foobar" in path.parts:
                return DirectoryState.EXCLUDED
            return DirectoryState.NORMAL

    d = MyDirectories()
    p1 = Path(str(tmpdir))
    p1.joinpath("foobar").mkdir()
    p1.joinpath("foobar/somefile").touch()
    p1.joinpath("foobaz").mkdir()
    p1.joinpath("foobaz/somefile").touch()
    d.add_path(p1)
    eq_(d.get_state(p1.joinpath("foobaz")), DirectoryState.NORMAL)
    eq_(d.get_state(p1.joinpath("foobar")), DirectoryState.EXCLUDED)
    eq_(len(list(d.get_files())), 1)  # only the 'foobaz' file is there
    # However, the default state can be changed
    d.set_state(p1.joinpath("foobar"), DirectoryState.NORMAL)
    eq_(d.get_state(p1.joinpath("foobar")), DirectoryState.NORMAL)
    eq_(len(list(d.get_files())), 2)


class TestExcludeList:
    def setup_method(self, method):
        self.d = Directories(exclude_list=ExcludeList(union_regex=False))

    def get_files_and_expect_num_result(self, num_result):
        """Calls get_files(), get the filenames only, print for debugging.
        num_result is how many files are expected as a result."""
        print(f"EXCLUDED REGEX: paths {self.d._exclude_list.compiled_paths} \
files: {self.d._exclude_list.compiled_files} all: {self.d._exclude_list.compiled}")
        files = list(self.d.get_files())
        files = [file.name for file in files]
        print(f"FINAL FILES {files}")
        eq_(len(files), num_result)
        return files

    def test_exclude_recycle_bin_by_default(self, tmpdir):
        regex = r"^.*Recycle\.Bin$"
        self.d._exclude_list.add(regex)
        self.d._exclude_list.mark(regex)
        p1 = Path(str(tmpdir))
        p1.joinpath("$Recycle.Bin").mkdir()
        p1.joinpath("$Recycle.Bin", "subdir").mkdir()
        self.d.add_path(p1)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin")), DirectoryState.EXCLUDED)
        # By default, subdirs should be excluded too, but this can be overridden separately
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.EXCLUDED)
        self.d.set_state(p1.joinpath("$Recycle.Bin", "subdir"), DirectoryState.NORMAL)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)

    def test_exclude_refined(self, tmpdir):
        regex1 = r"^\$Recycle\.Bin$"
        self.d._exclude_list.add(regex1)
        self.d._exclude_list.mark(regex1)
        p1 = Path(str(tmpdir))
        p1.joinpath("$Recycle.Bin").mkdir()
        p1.joinpath("$Recycle.Bin", "somefile.png").touch()
        p1.joinpath("$Recycle.Bin", "some_unwanted_file.jpg").touch()
        p1.joinpath("$Recycle.Bin", "subdir").mkdir()
        p1.joinpath("$Recycle.Bin", "subdir", "somesubdirfile.png").touch()
        p1.joinpath("$Recycle.Bin", "subdir", "unwanted_subdirfile.gif").touch()
        p1.joinpath("$Recycle.Bin", "subdar").mkdir()
        p1.joinpath("$Recycle.Bin", "subdar", "somesubdarfile.jpeg").touch()
        p1.joinpath("$Recycle.Bin", "subdar", "unwanted_subdarfile.png").touch()
        self.d.add_path(p1.joinpath("$Recycle.Bin"))

        # Filter should set the default state to Excluded
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin")), DirectoryState.EXCLUDED)
        # The subdir should inherit its parent state
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.EXCLUDED)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdar")), DirectoryState.EXCLUDED)
        # Override a child path's state
        self.d.set_state(p1.joinpath("$Recycle.Bin", "subdir"), DirectoryState.NORMAL)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)
        # Parent should keep its default state, and the other child too
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin")), DirectoryState.EXCLUDED)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdar")), DirectoryState.EXCLUDED)
        # print(f"get_folders(): {[x for x in self.d.get_folders()]}")

        # only the 2 files directly under the Normal directory
        files = self.get_files_and_expect_num_result(2)
        assert "somefile.png" not in files
        assert "some_unwanted_file.jpg" not in files
        assert "somesubdarfile.jpeg" not in files
        assert "unwanted_subdarfile.png" not in files
        assert "somesubdirfile.png" in files
        assert "unwanted_subdirfile.gif" in files
        # Overriding the parent should enable all children
        self.d.set_state(p1.joinpath("$Recycle.Bin"), DirectoryState.NORMAL)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdar")), DirectoryState.NORMAL)
        # all files there
        files = self.get_files_and_expect_num_result(6)
        assert "somefile.png" in files
        assert "some_unwanted_file.jpg" in files

        # This should still filter out files under directory, despite the Normal state
        regex2 = r".*unwanted.*"
        self.d._exclude_list.add(regex2)
        self.d._exclude_list.mark(regex2)
        files = self.get_files_and_expect_num_result(3)
        assert "somefile.png" in files
        assert "some_unwanted_file.jpg" not in files
        assert "unwanted_subdirfile.gif" not in files
        assert "unwanted_subdarfile.png" not in files

        if ISWINDOWS:
            regex3 = r".*Recycle\.Bin\\.*unwanted.*subdirfile.*"
        else:
            regex3 = r".*Recycle\.Bin\/.*unwanted.*subdirfile.*"
        self.d._exclude_list.rename(regex2, regex3)
        assert self.d._exclude_list.error(regex3) is None
        # print(f"get_folders(): {[x for x in self.d.get_folders()]}")
        # Directory shouldn't change its state here, unless explicitely done by user
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)
        files = self.get_files_and_expect_num_result(5)
        assert "unwanted_subdirfile.gif" not in files
        assert "unwanted_subdarfile.png" in files

        # using end of line character should only filter the directory, or file ending with subdir
        regex4 = r".*subdir$"
        self.d._exclude_list.rename(regex3, regex4)
        assert self.d._exclude_list.error(regex4) is None
        p1.joinpath("$Recycle.Bin", "subdar", "file_ending_with_subdir").touch()
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.EXCLUDED)
        files = self.get_files_and_expect_num_result(4)
        assert "file_ending_with_subdir" not in files
        assert "somesubdarfile.jpeg" in files
        assert "somesubdirfile.png" not in files
        assert "unwanted_subdirfile.gif" not in files
        self.d.set_state(p1.joinpath("$Recycle.Bin", "subdir"), DirectoryState.NORMAL)
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)
        # print(f"get_folders(): {[x for x in self.d.get_folders()]}")
        files = self.get_files_and_expect_num_result(6)
        assert "file_ending_with_subdir" not in files
        assert "somesubdirfile.png" in files
        assert "unwanted_subdirfile.gif" in files

        regex5 = r".*subdir.*"
        self.d._exclude_list.rename(regex4, regex5)
        # Files containing substring should be filtered
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)
        # The path should not match, only the filename, the "subdir" in the directory name shouldn't matter
        p1.joinpath("$Recycle.Bin", "subdir", "file_which_shouldnt_match").touch()
        files = self.get_files_and_expect_num_result(5)
        assert "somesubdirfile.png" not in files
        assert "unwanted_subdirfile.gif" not in files
        assert "file_ending_with_subdir" not in files
        assert "file_which_shouldnt_match" in files

        # This should match the directory only
        regex6 = r".*/.*subdir.*/.*"
        if ISWINDOWS:
            regex6 = r".*\\.*subdir.*\\.*"
        assert os.sep in regex6
        self.d._exclude_list.rename(regex5, regex6)
        self.d._exclude_list.remove(regex1)
        eq_(len(self.d._exclude_list.compiled), 1)
        assert regex1 not in self.d._exclude_list
        assert regex5 not in self.d._exclude_list
        assert self.d._exclude_list.error(regex6) is None
        assert regex6 in self.d._exclude_list
        # This still should not be affected
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "subdir")), DirectoryState.NORMAL)
        files = self.get_files_and_expect_num_result(5)
        # These files are under the "/subdir" directory
        assert "somesubdirfile.png" not in files
        assert "unwanted_subdirfile.gif" not in files
        # This file under "subdar" directory should not be filtered out
        assert "file_ending_with_subdir" in files
        # This file is in a directory that should be filtered out
        assert "file_which_shouldnt_match" not in files

    def test_japanese_unicode(self, tmpdir):
        p1 = Path(str(tmpdir))
        p1.joinpath("$Recycle.Bin").mkdir()
        p1.joinpath("$Recycle.Bin", "somerecycledfile.png").touch()
        p1.joinpath("$Recycle.Bin", "some_unwanted_file.jpg").touch()
        p1.joinpath("$Recycle.Bin", "subdir").mkdir()
        p1.joinpath("$Recycle.Bin", "subdir", "過去白濁物語～]_カラー.jpg").touch()
        p1.joinpath("$Recycle.Bin", "思叫物語").mkdir()
        p1.joinpath("$Recycle.Bin", "思叫物語", "なししろ会う前").touch()
        p1.joinpath("$Recycle.Bin", "思叫物語", "堂～ロ").touch()
        self.d.add_path(p1.joinpath("$Recycle.Bin"))
        regex3 = r".*物語.*"
        self.d._exclude_list.add(regex3)
        self.d._exclude_list.mark(regex3)
        # print(f"get_folders(): {[x for x in self.d.get_folders()]}")
        eq_(self.d.get_state(p1.joinpath("$Recycle.Bin", "思叫物語")), DirectoryState.EXCLUDED)
        files = self.get_files_and_expect_num_result(2)
        assert "過去白濁物語～]_カラー.jpg" not in files
        assert "なししろ会う前" not in files
        assert "堂～ロ" not in files
        # using end of line character should only filter that directory, not affecting its files
        regex4 = r".*物語$"
        self.d._exclude_list.rename(regex3, regex4)
        assert self.d._exclude_list.error(regex4) is None
        self.d.set_state(p1.joinpath("$Recycle.Bin", "思叫物語"), DirectoryState.NORMAL)
        files = self.get_files_and_expect_num_result(5)
        assert "過去白濁物語～]_カラー.jpg" in files
        assert "なししろ会う前" in files
        assert "堂～ロ" in files

    def test_get_state_returns_excluded_for_hidden_directories_and_files(self, tmpdir):
        # This regex only work for files, not paths
        regex = r"^\..*$"
        self.d._exclude_list.add(regex)
        self.d._exclude_list.mark(regex)
        p1 = Path(str(tmpdir))
        p1.joinpath("foobar").mkdir()
        p1.joinpath("foobar", ".hidden_file.txt").touch()
        p1.joinpath("foobar", ".hidden_dir").mkdir()
        p1.joinpath("foobar", ".hidden_dir", "foobar.jpg").touch()
        p1.joinpath("foobar", ".hidden_dir", ".hidden_subfile.png").touch()
        self.d.add_path(p1.joinpath("foobar"))
        # It should not inherit its parent's state originally
        eq_(self.d.get_state(p1.joinpath("foobar", ".hidden_dir")), DirectoryState.EXCLUDED)
        self.d.set_state(p1.joinpath("foobar", ".hidden_dir"), DirectoryState.NORMAL)
        # The files should still be filtered
        files = self.get_files_and_expect_num_result(1)
        eq_(len(self.d._exclude_list.compiled_paths), 0)
        eq_(len(self.d._exclude_list.compiled_files), 1)
        assert ".hidden_file.txt" not in files
        assert ".hidden_subfile.png" not in files
        assert "foobar.jpg" in files


class TestExcludeDict(TestExcludeList):
    def setup_method(self, method):
        self.d = Directories(exclude_list=ExcludeDict(union_regex=False))


class TestExcludeListunion(TestExcludeList):
    def setup_method(self, method):
        self.d = Directories(exclude_list=ExcludeList(union_regex=True))


class TestExcludeDictunion(TestExcludeList):
    def setup_method(self, method):
        self.d = Directories(exclude_list=ExcludeDict(union_regex=True))
