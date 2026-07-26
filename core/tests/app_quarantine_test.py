import errno
from dataclasses import replace
from pathlib import Path

import pytest

from hscommon.jobprogress.job import Job, JobCancelled, nulljob

from core import engine, fs
from core.action_plan import build_bound_deletion_plan
from core.directories import DirectoryState
from core.scan_receipt import ScanReceipt
from core.tests.base import TestApp

PAYLOAD = b"proof-bound GUI quarantine payload\n" * 2048


def _app_with_exact_group(tmp_path, duplicate_count=1, keeper_pool="incoming"):
    root = tmp_path / "library"
    root.mkdir(parents=True)
    keeper_directory = root / "keeper"
    incoming_directory = root / "incoming"
    keeper_directory.mkdir()
    incoming_directory.mkdir()
    paths = [keeper_directory / "keeper.bin"] + [
        incoming_directory / "duplicate-{}.bin".format(index) for index in range(duplicate_count)
    ]
    for path in paths:
        path.write_bytes(PAYLOAD)
    files = [fs.File(path) for path in paths]
    for file in files:
        file.begin_review_scan()
    files[0].comparison_pool = keeper_pool
    files[0].is_ref = keeper_pool != "incoming"
    for file in files[1:]:
        file.is_ref = False
        file.comparison_pool = "incoming"
    digest, _ = files[0]._calc_digest_with_snapshot()
    group = engine.build_verified_exact_group(
        files,
        digest=digest,
        size=len(PAYLOAD),
        algorithm=files[0].digest_algorithm,
    )
    test_app = TestApp()
    app = test_app.app
    app.directories.add_path(root)
    if keeper_pool == "protected":
        app.directories.set_state(keeper_directory, DirectoryState.REFERENCE)
    elif keeper_pool == "compare_only":
        app.directories.set_state(keeper_directory, DirectoryState.COMPARE_ONLY)
    app.results.groups = [group]
    app.results.scan_receipt = ScanReceipt.completed(len(files))
    for duplicate in files[1:]:
        app.results.mark(duplicate)
    return app, root, files


def test_gui_default_action_quarantines_and_can_restore(tmp_path):
    app, root, files = _app_with_exact_group(tmp_path)
    target = files[1]
    bound = build_bound_deletion_plan(app.results, [target], [root])

    app._do_delete(nulljob, bound)

    assert not Path(target.path).exists()
    assert Path(files[0].path).read_bytes() == PAYLOAD
    assert len(app.last_quarantine_plan_paths) == 1
    plan_path = Path(app.last_quarantine_plan_paths[0])
    assert plan_path.is_file()
    assert len(app.results.dupes) == 0

    app._do_restore_last_quarantine(nulljob, app.last_quarantine_plan_paths)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert app.last_quarantine_plan_paths == ()


def test_gui_batch_preflight_is_all_or_nothing(tmp_path):
    app, root, files = _app_with_exact_group(tmp_path, duplicate_count=2)
    targets = files[1:]
    bound = build_bound_deletion_plan(app.results, targets, [root])
    Path(targets[1].path).write_bytes(b"x" * len(PAYLOAD))

    app._do_delete(nulljob, bound)

    assert Path(targets[0].path).read_bytes() == PAYLOAD
    assert Path(targets[1].path).read_bytes() != PAYLOAD
    assert len(app.results.problems) == 2
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_gui_move_refuses_compare_only_target_before_destination_prompt(tmp_path):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    target.comparison_pool = "compare_only"

    app.copy_or_move_marked(False)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert "No files were moved" in app.view.messages[-1]


def test_gui_copy_refuses_compare_only_target_before_destination_prompt(tmp_path):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    target.comparison_pool = "compare_only"

    app.copy_or_move_marked(True)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert "No files were copied" in app.view.messages[-1]


@pytest.mark.parametrize(
    ("copy", "operation"),
    (
        (True, "copied"),
        (False, "moved"),
    ),
)
def test_gui_organizer_refuses_unknown_relationship(tmp_path, copy, operation):
    app, _, files = _app_with_exact_group(tmp_path)
    group = app.results.get_group_of_duplicate(files[1])
    group.verification_kind = engine.VerificationKind.UNVERIFIED
    group.compact_relation = None

    app.copy_or_move_marked(copy)

    assert Path(files[1].path).read_bytes() == PAYLOAD
    assert "No files were {}".format(operation) in app.view.messages[-1]
    assert "Unknown or incomplete relationships" in app.view.messages[-1]


@pytest.mark.parametrize(
    "state",
    (
        DirectoryState.REFERENCE,
        DirectoryState.COMPARE_ONLY,
        DirectoryState.EXCLUDED,
    ),
)
def test_gui_delete_refuses_target_pool_changed_after_scan(tmp_path, state):
    app, root, files = _app_with_exact_group(tmp_path)
    target = files[1]
    app.directories.set_state(Path(target.path).parent, state)

    app.delete_marked()

    assert Path(target.path).read_bytes() == PAYLOAD
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()
    assert "Run a new scan" in app.view.messages[-1]


@pytest.mark.parametrize(
    "state",
    (
        DirectoryState.REFERENCE,
        DirectoryState.COMPARE_ONLY,
        DirectoryState.EXCLUDED,
    ),
)
def test_gui_delete_refuses_keeper_pool_changed_after_scan(tmp_path, state):
    app, root, files = _app_with_exact_group(tmp_path)
    target = files[1]
    app.directories.set_state(Path(files[0].path).parent, state)

    app.delete_marked()

    assert Path(target.path).read_bytes() == PAYLOAD
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_delete_refuses_protected_keeper_changed_to_incoming(tmp_path):
    app, root, files = _app_with_exact_group(tmp_path, keeper_pool="protected")
    target = files[1]
    app.directories.set_state(Path(files[0].path).parent, DirectoryState.NORMAL)

    app.delete_marked()

    assert Path(target.path).read_bytes() == PAYLOAD
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_delete_refuses_new_exclude_rule_for_target_or_keeper(tmp_path):
    for excluded_name in ("duplicate-0.bin", "keeper.bin"):
        app, root, files = _app_with_exact_group(tmp_path / excluded_name.replace(".", "-"))
        regex = r"^{}$".format(excluded_name.replace(".", r"\."))
        app.exclude_list.add(regex)
        app.exclude_list.mark(regex)

        app.delete_marked()

        assert Path(files[1].path).read_bytes() == PAYLOAD
        assert not root.joinpath(".dupeguru-neo-quarantine").exists()
        assert "Run a new scan" in app.view.messages[-1]


@pytest.mark.parametrize(
    "state",
    (
        DirectoryState.REFERENCE,
        DirectoryState.COMPARE_ONLY,
        DirectoryState.EXCLUDED,
    ),
)
def test_gui_move_refuses_target_pool_changed_after_scan(tmp_path, state):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    app.directories.set_state(Path(target.path).parent, state)

    app.copy_or_move_marked(False)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_move_refuses_keeper_pool_changed_after_scan(tmp_path):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    app.directories.set_state(
        Path(files[0].path).parent,
        DirectoryState.REFERENCE,
    )

    app.copy_or_move_marked(False)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_copy_rechecks_pool_policy_inside_the_worker(tmp_path, monkeypatch):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    destination = tmp_path / "destination"
    destination.mkdir()
    app.view.select_dest_folder = lambda _prompt: str(destination)

    def run_after_policy_change(jobid, function, args=()):
        app.directories.set_state(Path(target.path).parent, DirectoryState.COMPARE_ONLY)
        function(nulljob, *args)

    monkeypatch.setattr(app, "_start_job", run_after_policy_change)

    app.copy_or_move_marked(True)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert list(destination.rglob("duplicate-0.bin")) == []
    assert len(app.results.problems) == 1
    assert "Run a new scan" in app.results.problems[0][1]


@pytest.mark.parametrize(
    ("copy", "operation"),
    (
        (True, "copied"),
        (False, "moved"),
    ),
)
@pytest.mark.parametrize("changed_member", ("target", "keeper"))
def test_gui_organizer_refuses_scan_generation_drift(
    tmp_path,
    copy,
    operation,
    changed_member,
):
    app, _, files = _app_with_exact_group(tmp_path)
    changed = files[1] if changed_member == "target" else files[0]
    Path(changed.path).write_bytes(b"x" * len(PAYLOAD))

    app.copy_or_move_marked(copy)

    assert Path(files[1].path).exists()
    assert "No files were {}".format(operation) in app.view.messages[-1]
    assert "changed after this scan" in app.view.messages[-1]


@pytest.mark.parametrize("copy", (True, False))
def test_gui_organizer_rechecks_generation_inside_the_worker(tmp_path, monkeypatch, copy):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    destination = tmp_path / "destination"
    destination.mkdir()
    app.view.select_dest_folder = lambda _prompt: str(destination)

    def run_after_source_change(jobid, function, args=()):
        Path(target.path).write_bytes(b"x" * len(PAYLOAD))
        function(nulljob, *args)

    monkeypatch.setattr(app, "_start_job", run_after_source_change)

    app.copy_or_move_marked(copy)

    assert Path(target.path).read_bytes() == b"x" * len(PAYLOAD)
    assert list(destination.rglob("duplicate-0.bin")) == []
    assert len(app.results.problems) == 1
    assert "changed after this scan" in app.results.problems[0][1]


@pytest.mark.parametrize("copy", (True, False))
def test_gui_organizer_rechecks_keeper_bytes_when_metadata_looks_unchanged(
    tmp_path,
    monkeypatch,
    copy,
):
    app, _, files = _app_with_exact_group(tmp_path)
    keeper = files[0]
    target = files[1]
    destination = tmp_path / "destination"
    destination.mkdir()
    app.view.select_dest_folder = lambda _prompt: str(destination)

    def run_after_same_tick_keeper_change(jobid, function, args=()):
        Path(keeper.path).write_bytes(b"k" * len(PAYLOAD))
        current = fs._snapshot_path(keeper.path)
        keeper._review_scan_snapshot = replace(
            keeper._review_scan_snapshot,
            device=current.device,
            file_id=current.file_id,
            size=current.size,
            mtime_ns=current.mtime_ns,
            ctime_ns=current.ctime_ns,
        )
        function(nulljob, *args)

    monkeypatch.setattr(app, "_start_job", run_after_same_tick_keeper_change)

    app.copy_or_move_marked(copy)

    assert Path(target.path).read_bytes() == PAYLOAD
    assert Path(keeper.path).read_bytes() == b"k" * len(PAYLOAD)
    assert list(destination.rglob("duplicate-0.bin")) == []
    assert len(app.results.problems) == 1
    assert "changed after this scan" in app.results.problems[0][1]


def test_gui_organizer_keeper_proof_reports_bytes_before_action_completion(
    tmp_path,
    monkeypatch,
):
    app, _, files = _app_with_exact_group(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    app.view.select_dest_folder = lambda _prompt: str(destination)
    updates = []

    def report(progress, description=""):
        updates.append((progress, description))
        return True

    def run_now(jobid, function, args=()):
        function(Job(1, report), *args)

    monkeypatch.setattr(app, "_start_job", run_now)

    app.copy_or_move_marked(True)

    assert Path(files[1].path).read_bytes() == PAYLOAD
    assert len(list(destination.rglob("duplicate-0.bin"))) == 1
    descriptions = [description for _, description in updates if description]
    assert any(
        "Verifying keeper" in description
        and "0/1 organizer actions completed" in description
        and "{} bytes read".format(len(PAYLOAD)) in description
        for description in descriptions
    )
    assert descriptions[-1] == "Organizer action complete: 1/1 files"
    assert updates[-1][0] == 100


def test_gui_organizer_keeper_proof_honors_chunk_cancellation(
    tmp_path,
    monkeypatch,
):
    app, _, files = _app_with_exact_group(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    app.view.select_dest_folder = lambda _prompt: str(destination)
    cancellation_polls = 0

    def report(_progress, description=""):
        nonlocal cancellation_polls
        if description:
            return True
        cancellation_polls += 1
        return cancellation_polls < 2

    def run_now(jobid, function, args=()):
        function(Job(1, report), *args)

    monkeypatch.setattr(app, "_start_job", run_now)

    with pytest.raises(JobCancelled):
        app.copy_or_move_marked(True)

    assert cancellation_polls == 2
    assert Path(files[1].path).read_bytes() == PAYLOAD
    assert list(destination.rglob("duplicate-0.bin")) == []


@pytest.mark.parametrize("copy", (True, False))
def test_low_level_organizer_commit_rejects_a_changed_expected_source(tmp_path, copy):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    expected = target.validate_review_scan()
    Path(target.path).write_bytes(b"x" * len(PAYLOAD))
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(OSError) as caught:
        app.copy_or_move(
            target,
            copy,
            str(destination),
            0,
            expected,
        )

    assert caught.value.errno == errno.ESTALE
    assert Path(target.path).read_bytes() == b"x" * len(PAYLOAD)
    assert list(destination.rglob("duplicate-0.bin")) == []


@pytest.mark.parametrize(
    "state",
    (
        DirectoryState.REFERENCE,
        DirectoryState.COMPARE_ONLY,
        DirectoryState.EXCLUDED,
    ),
)
def test_gui_rename_refuses_current_nonincoming_state(tmp_path, state):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    original_path = Path(target.path)
    app.selected_dupes = [target]
    app.directories.set_state(original_path.parent, state)

    assert not app.rename_selected("renamed.bin")

    assert original_path.read_bytes() == PAYLOAD
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_rename_refuses_keeper_pool_drift(tmp_path):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    original_path = Path(target.path)
    app.selected_dupes = [target]
    app.directories.set_state(
        Path(files[0].path).parent,
        DirectoryState.REFERENCE,
    )

    assert not app.rename_selected("renamed.bin")

    assert original_path.read_bytes() == PAYLOAD
    assert "Run a new scan" in app.view.messages[-1]


def test_gui_incoming_rename_invalidates_live_scan(tmp_path):
    app, _, files = _app_with_exact_group(tmp_path)
    target = files[1]
    old_path = Path(target.path)
    app.selected_dupes = [target]

    assert app.rename_selected("renamed.bin")

    assert not old_path.exists()
    assert Path(target.path).read_bytes() == PAYLOAD
    assert not app.results.scan_receipt.complete
    assert app.results.scan_receipt.issues[0].code == "result_path_changed"
