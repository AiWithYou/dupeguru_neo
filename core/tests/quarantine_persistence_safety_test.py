import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import quarantine as quarantine_module
from core import safe_action
from core.quarantine import QuarantineError, QuarantineManager
from core.safe_action import (
    AppendOnlyJournal,
    JournalEventType,
    SafeActionExecutor,
    platform_file_system,
)
from core.services import PlanService, ScanRequest, ScanService
from core.services.models import plan_id_for


def _exact_plan(root: Path, count: int = 2):
    for index in range(count):
        root.joinpath("duplicate-{}.bin".format(index)).write_bytes(b"verified duplicate")
    report = ScanService().scan(ScanRequest(roots=(str(root),)))
    return PlanService().create(report)


def _with_roots(plan, roots):
    roots = tuple(str(root) for root in roots)
    return replace(
        plan,
        roots=roots,
        plan_id=plan_id_for(plan.source_scan_id, roots, plan.actions),
    )


def test_read_only_validation_resolves_each_allowed_root_once(
    tmp_path,
    monkeypatch,
):
    plan = _exact_plan(tmp_path)
    extra_roots = []
    for index in range(128):
        root = tmp_path / "extra-roots" / str(index)
        root.mkdir(parents=True)
        extra_roots.append(root)
    plan = _with_roots(plan, (tmp_path, *extra_roots))
    manager = QuarantineManager()
    real_resolve = manager.fs.resolve
    calls = []

    def counted_resolve(path, strict=False):
        calls.append(os.path.normcase(os.path.abspath(os.fspath(path))))
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(manager.fs, "resolve", counted_resolve)

    assert manager.validate_read_only(plan) == ()

    normalized_roots = {os.path.normcase(os.path.abspath(root)) for root in plan.roots}
    assert sum(path in normalized_roots for path in calls) == len(normalized_roots)
    assert len(calls) == len(normalized_roots) + 2 * len(plan.actions)


def test_prepare_passes_only_selected_roots_to_safe_action_builder(
    tmp_path,
    monkeypatch,
):
    plan = _exact_plan(tmp_path)
    extra_roots = []
    for index in range(64):
        root = tmp_path / "unrelated-roots" / str(index)
        root.mkdir(parents=True)
        extra_roots.append(root)
    plan = _with_roots(plan, (tmp_path, *extra_roots))
    real_builder = quarantine_module.build_operation_plan
    allowed_root_counts = []

    def counted_builder(**kwargs):
        kwargs["allowed_roots"] = tuple(kwargs["allowed_roots"])
        allowed_root_counts.append(len(kwargs["allowed_roots"]))
        return real_builder(**kwargs)

    monkeypatch.setattr(
        quarantine_module,
        "build_operation_plan",
        counted_builder,
    )

    batch = QuarantineManager().prepare(plan)

    assert batch.ok
    assert allowed_root_counts == [1, 1]


def test_nested_allowed_roots_select_the_deepest_ancestor(
    tmp_path,
    monkeypatch,
):
    nested = tmp_path / "protected" / "library"
    nested.mkdir(parents=True)
    plan = _with_roots(_exact_plan(nested), (tmp_path, nested))
    real_builder = quarantine_module.build_operation_plan
    selections = []

    def capture_builder(**kwargs):
        kwargs["allowed_roots"] = tuple(kwargs["allowed_roots"])
        selections.append(
            (
                tuple(Path(root) for root in kwargs["allowed_roots"]),
                Path(kwargs["quarantine_root"]),
            )
        )
        return real_builder(**kwargs)

    monkeypatch.setattr(
        quarantine_module,
        "build_operation_plan",
        capture_builder,
    )

    batch = QuarantineManager().prepare(plan)

    assert batch.ok
    assert selections
    assert all(roots == (nested,) for roots, _ in selections)
    assert all(root.parent == nested for _, root in selections)


def test_plan_publication_durability_failure_preserves_replacement(
    tmp_path,
):
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    destination = plans_root / "operation.json"
    external = b"unrelated replacement must survive"
    preserved = plans_root / "published-plan-preserved.json"
    adapter_base = type(platform_file_system())

    class ReplaceAfterPublicationFileSystem(adapter_base):
        def __init__(self):
            self.published = False
            self.source_replacement = None

        def rename_no_replace(self, source, target):
            commit = super().rename_no_replace(source, target)
            Path(source).write_bytes(b"external source-name replacement")
            self.source_replacement = Path(source)
            self.published = True
            return commit

        def fsync_directory(self, directory):
            if self.published and Path(directory) == plans_root:
                self.published = False
                os.rename(destination, preserved)
                destination.write_bytes(external)
                raise OSError(5, "injected directory durability failure")
            return super().fsync_directory(directory)

    adapter = ReplaceAfterPublicationFileSystem()
    manager = QuarantineManager(fs=adapter)

    with pytest.raises(OSError, match="durability failure"):
        manager._atomic_write_json(destination, {"proof": "immutable"})

    assert destination.read_bytes() == external
    assert preserved.is_file()
    assert adapter.source_replacement is not None
    assert adapter.source_replacement.read_bytes() == b"external source-name replacement"


def test_batch_persistence_failure_never_unlinks_prior_replacement(
    tmp_path,
    monkeypatch,
):
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    first_path = plans_root / "first.json"
    second_path = plans_root / "second.json"
    preserved = plans_root / "first-plan-preserved.json"
    external = b"concurrent replacement must survive"
    manager = QuarantineManager()
    real_write = manager._atomic_write_json
    calls = 0

    def persist_then_fail(path, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_write(path, payload)
            return
        os.rename(first_path, preserved)
        first_path.write_bytes(external)
        raise OSError(5, "injected second-plan failure")

    monkeypatch.setattr(manager, "_atomic_write_json", persist_then_fail)
    prepared = (
        SimpleNamespace(
            plan_path=first_path,
            stored=SimpleNamespace(to_dict=lambda: {"ordinal": 1}),
        ),
        SimpleNamespace(
            plan_path=second_path,
            stored=SimpleNamespace(to_dict=lambda: {"ordinal": 2}),
        ),
    )

    with pytest.raises(OSError, match="second-plan failure"):
        manager._persist_all(prepared, [])

    assert first_path.read_bytes() == external
    assert preserved.is_file()
    assert not second_path.exists()


def test_prepare_reserves_complete_lifecycle_journal_budget_before_mutation(
    tmp_path,
    monkeypatch,
):
    plan = _exact_plan(tmp_path)
    target = Path(plan.actions[0].target.path)
    keeper = Path(plan.actions[0].reference.path)
    before = {target: target.read_bytes(), keeper: keeper.read_bytes()}
    required = safe_action.LIFECYCLE_JOURNAL_RESERVE_EVENTS * safe_action.MAX_JOURNAL_LINE_BYTES
    monkeypatch.setattr(safe_action, "MAX_JOURNAL_BYTES", required - 1)

    batch = QuarantineManager().prepare(plan)

    assert not batch.ok
    assert batch.failures
    assert {path: path.read_bytes() for path in before} == before
    assert not tmp_path.joinpath(".dupeguru-neo-quarantine").exists()


def test_each_operation_has_isolated_list_restore_and_finalize_history(
    tmp_path,
):
    plan = _exact_plan(tmp_path, count=3)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    assert batch.ok

    applied = manager.execute(batch)

    assert all(result.failure_code == "none" for result in applied)
    journal_paths = tuple(item.plan_path.parent / "journal.jsonl" for item in batch.prepared)
    assert len(set(journal_paths)) == 2
    for item, journal_path in zip(batch.prepared, journal_paths):
        events = AppendOnlyJournal(journal_path).read()
        assert {event.plan_id for event in events} == {item.stored.operation_plan.plan_id}
        assert [event.event for event in events] == [
            JournalEventType.PREPARED,
            JournalEventType.STAGED,
        ]
    assert len(manager.list([str(tmp_path)])) == 2

    restored = manager.restore(batch.prepared[0].plan_path)
    finalized = manager.finalize(batch.prepared[1].plan_path)

    assert restored.failure_code == "none"
    assert restored.safe_state == "restored"
    assert finalized.failure_code == "none"
    assert finalized.safe_state == "finalized"
    assert Path(batch.prepared[0].stored.operation_plan.target.path).is_file()
    assert not batch.prepared[1].stored.operation_plan.quarantine_path.exists()
    assert Path(batch.prepared[1].stored.operation_plan.keeper.path).is_file()


def test_per_operation_journal_replays_crash_after_stage_rename(
    tmp_path,
):
    class SimulatedCrash(BaseException):
        pass

    class CrashBeforeStagedJournal(AppendOnlyJournal):
        def append(self, plan, event, details=None):
            if event is JournalEventType.STAGED:
                raise SimulatedCrash()
            return super().append(plan, event, details)

    plan = _exact_plan(tmp_path)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    assert batch.ok
    prepared = batch.prepared[0]
    operation_plan = prepared.stored.operation_plan
    journal_path = prepared.plan_path.parent / "journal.jsonl"
    crashing = SafeActionExecutor(
        CrashBeforeStagedJournal(journal_path),
        fs=manager.fs,
    )

    with pytest.raises(SimulatedCrash):
        crashing.stage(operation_plan)

    assert not Path(operation_plan.target.path).exists()
    assert operation_plan.quarantine_path.is_file()

    [replayed] = QuarantineManager().execute(batch)

    assert replayed.failure_code == "none"
    assert replayed.safe_state == "staged"
    events = AppendOnlyJournal(journal_path).events_for(operation_plan.plan_id)
    assert events[-1].event is JournalEventType.STAGED_RECOVERED


def test_recovered_restore_is_persistently_listed_as_restored(tmp_path):
    class SimulatedCrash(BaseException):
        pass

    class CrashBeforeRestoredJournal(AppendOnlyJournal):
        def append(self, plan, event, details=None):
            if event is JournalEventType.RESTORED:
                raise SimulatedCrash()
            return super().append(plan, event, details)

    plan = _exact_plan(tmp_path)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    assert batch.ok
    prepared = batch.prepared[0]
    operation_plan = prepared.stored.operation_plan
    assert manager.execute(batch)[0].failure_code == "none"
    journal_path = prepared.plan_path.parent / "journal.jsonl"
    crashing = SafeActionExecutor(
        CrashBeforeRestoredJournal(journal_path),
        fs=manager.fs,
    )

    with pytest.raises(SimulatedCrash):
        crashing.restore(operation_plan)

    replay = SafeActionExecutor(
        AppendOnlyJournal(journal_path),
        fs=manager.fs,
    ).restore(operation_plan)
    listed = QuarantineManager().list([str(tmp_path)])

    assert replay.ok
    assert replay.state.value == "restored"
    assert any(
        item["operation_plan_id"] == operation_plan.plan_id
        and item["state"] == "restored"
        and "restored_recovered" in item["journal_events"]
        for item in listed
    )


def test_corrupt_second_operation_journal_rolls_back_first_without_cross_talk(
    tmp_path,
):
    plan = _exact_plan(tmp_path, count=3)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    assert batch.ok
    second_journal = batch.prepared[1].plan_path.parent / "journal.jsonl"
    second_journal.write_bytes(b"not-json\n")
    original_payloads = {Path(item.stored.operation_plan.target.path): b"verified duplicate" for item in batch.prepared}

    results = manager.execute(batch)

    assert all(result.status == "failed" for result in results)
    assert results[1].failure_code == "journal_corrupt"
    assert {path: path.read_bytes() for path in original_payloads} == original_payloads
    assert not any(item.stored.operation_plan.quarantine_path.exists() for item in batch.prepared)
    first_events = AppendOnlyJournal(batch.prepared[0].plan_path.parent / "journal.jsonl").events_for(
        batch.prepared[0].stored.operation_plan.plan_id
    )
    assert JournalEventType.RESTORED in {event.event for event in first_events}


@pytest.mark.parametrize(
    "malformed_document",
    (
        b'{"schema":"x","schema":"x"}\n',
        b'{"value":NaN}\n',
        b'{"value":1e9999}\n',
        ('{"value":' + str(1 << 300) + "}\n").encode("ascii"),
    ),
    ids=("duplicate-key", "nan", "overflowing-float", "huge-integer"),
)
def test_ambiguous_operation_document_aborts_entire_batch_before_staging(
    tmp_path,
    malformed_document,
):
    plan = _exact_plan(tmp_path)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    assert batch.ok
    before = {
        Path(item.stored.operation_plan.target.path): Path(item.stored.operation_plan.target.path).read_bytes()
        for item in batch.prepared
    }
    prepared = batch.prepared[0]
    prepared.plan_path.write_bytes(malformed_document)

    with pytest.raises(QuarantineError):
        manager.execute(batch)

    assert {path: path.read_bytes() for path in before} == before
    assert prepared.plan_path.read_bytes() == malformed_document
    assert not any(item.stored.operation_plan.quarantine_path.exists() for item in batch.prepared)
