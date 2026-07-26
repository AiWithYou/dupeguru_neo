import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from core import engine
from core import fs as core_fs
from core import quarantine as quarantine_module
from core.reserved_paths import RESERVED_INTERNAL_DIRECTORY_NAMES
from core.quarantine import (
    PreparationBatch,
    QuarantineError,
    QuarantineManager,
    StoredOperation,
    operation_plan_id,
)
from core.safe_action import (
    ActionResult as SafeActionResult,
    ActionState,
    FailureCode,
)
from core.services import (
    SCAN_REPORT_SCHEMA,
    SCHEMA_VERSION,
    APPROXIMATE,
    ActionResult,
    ApplyService,
    DeletionPlan,
    PlanAction,
    PlanService,
    QuarantineService,
    QueryService,
    ScanGroup,
    ScanReport,
    ScanRequest,
    ScanService,
    ScanSummary,
    SchemaError,
)
from core.services.adapters import ApplyAdapter, ApplyPreparation, SafeActionApplyAdapter
from core.services.jsonio import (
    iter_plan_jsonl,
    iter_scan_jsonl,
    load_deletion_plan,
    load_scan_report,
)
from core.services.models import action_id_for, plan_id_for


def _scan_exact_fixture(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    different = tmp_path / "c.bin"
    unique = tmp_path / "unique.bin"
    first.write_bytes(b"verified duplicate")
    second.write_bytes(b"verified duplicate")
    different.write_bytes(b"different content!")
    unique.write_bytes(b"x")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    return report, first, second, different, unique


def test_local_scan_produces_only_byte_verified_exact_group(tmp_path):
    report, first, second, different, unique = _scan_exact_fixture(tmp_path)

    assert report.summary.complete
    assert report.summary.discovered_files == 4
    assert report.summary.verified_groups == 1
    assert report.summary.duplicate_files == 1
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.verification == "verified_exact"
    assert group.verification_method == "{}+core-streaming-byte-compare".format(core_fs.HASH_ALGORITHM)
    assert {item.digest_algorithm for item in group.files} == {"sha256"}
    assert {item.path for item in group.files} == {str(first), str(second)}
    assert len(report.coverage) == 1
    assert report.coverage[0].complete
    assert different.read_bytes() == b"different content!"
    assert unique.read_bytes() == b"x"


def test_exact_scan_uses_explainable_keeper_policy_for_cli_reference(tmp_path):
    preferred = tmp_path / "original.bin"
    copy = tmp_path / "original copy.bin"
    preferred.write_bytes(b"same verified payload")
    copy.write_bytes(b"same verified payload")

    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))

    assert report.summary.complete
    assert len(report.groups) == 1
    assert report.groups[0].reference.path == str(preferred)
    assert [item.path for item in report.groups[0].duplicates] == [str(copy)]


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES | {name.upper() for name in RESERVED_INTERNAL_DIRECTORY_NAMES}),
)
def test_exact_scan_always_prunes_reserved_internal_directories(
    tmp_path,
    reserved_name,
):
    visible_first = tmp_path / "visible-first.bin"
    visible_second = tmp_path / "visible-second.bin"
    visible_first.write_bytes(b"visible duplicate")
    visible_second.write_bytes(b"visible duplicate")
    reserved = tmp_path / reserved_name
    reserved.mkdir()
    (reserved / "hidden-first.bin").write_bytes(b"hidden duplicate")
    (reserved / "hidden-second.bin").write_bytes(b"hidden duplicate")

    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))

    assert report.summary.complete
    assert report.summary.discovered_files == 2
    assert len(report.groups) == 1
    assert {item.path for item in report.groups[0].files} == {
        str(visible_first),
        str(visible_second),
    }
    assert report.coverage[0].complete


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES),
)
def test_exact_scan_rejects_reserved_internal_directory_as_explicit_root(
    tmp_path,
    reserved_name,
):
    reserved = tmp_path / reserved_name
    reserved.mkdir()
    (reserved / "payload.bin").write_bytes(b"payload")

    report = ScanService().scan(ScanRequest(roots=(str(reserved),)))

    assert not report.summary.complete
    assert report.roots == ()
    assert report.groups == ()
    assert report.issues[0].code == "reserved-internal-root"


def test_exact_scan_skips_only_strict_dataset_staging_temporary_names(tmp_path):
    ordinary_first = tmp_path / ".ordinary-first.tmp"
    ordinary_second = tmp_path / ".ordinary-second.tmp"
    ordinary_first.write_bytes(b"ordinary duplicate")
    ordinary_second.write_bytes(b"ordinary duplicate")
    internal = tmp_path / ".image.png.dupeguru-abcdef012345-000001.tmp"
    internal.write_bytes(b"ordinary duplicate")
    near_miss = tmp_path / ".image.png.dupeguru-abcdef012345-1.tmp"
    near_miss.write_bytes(b"ordinary duplicate")

    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))

    assert report.summary.complete
    assert report.summary.discovered_files == 3
    assert len(report.groups) == 1
    assert {item.path for item in report.groups[0].files} == {
        str(ordinary_first),
        str(ordinary_second),
        str(near_miss),
    }


def test_scan_uses_partial_hash_only_as_candidate_filter(tmp_path, monkeypatch):
    first_data = bytearray(128 * 1024)
    second_data = bytearray(first_data)
    second_data[core_fs.PARTIAL_OFFSET_SIZE[0]] = 1
    (tmp_path / "first.bin").write_bytes(first_data)
    (tmp_path / "second.bin").write_bytes(second_data)
    full_hash_calls = []
    original = core_fs.File._calc_digest_with_snapshot

    def recording_full_hash(file):
        full_hash_calls.append(str(file.path))
        return original(file)

    monkeypatch.setattr(core_fs.File, "_calc_digest_with_snapshot", recording_full_hash)

    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))

    assert report.summary.complete
    assert report.summary.hashed_files == 0
    assert report.groups == ()
    assert full_hash_calls == []


def test_final_byte_read_failure_makes_service_report_incomplete(tmp_path, monkeypatch):
    (tmp_path / "first.bin").write_bytes(b"same bytes")
    (tmp_path / "second.bin").write_bytes(b"same bytes")

    def fail_final_comparison(first, second):
        raise OSError("simulated final read failure")

    monkeypatch.setattr(engine, "_compare_exact_files", fail_final_comparison)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))

    assert report.groups == ()
    assert not report.summary.complete
    assert report.summary.issues == 1
    assert report.issues[0].code == "byte-verification-failed"
    with pytest.raises(ValueError, match="incomplete"):
        PlanService().create(report)


def test_scan_report_jsonl_round_trip(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    payload = "".join(iter_scan_jsonl(report))

    loaded = load_scan_report(io.StringIO(payload))

    assert loaded.to_dict() == report.to_dict()
    assert all(json.loads(line)["schema_version"] == SCHEMA_VERSION for line in payload.splitlines())


def test_scan_report_json_round_trip(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    loaded = load_scan_report(io.StringIO(json.dumps(report.to_dict())))
    assert loaded.to_dict() == report.to_dict()


def test_unknown_scan_schema_version_fails_clearly(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    payload = report.to_dict()
    payload["schema_version"] = 999

    with pytest.raises(SchemaError, match="Unsupported"):
        load_scan_report(io.StringIO(json.dumps(payload)))


def test_plan_contains_only_verified_exact_groups(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    exact = report.groups[0]
    approximate = ScanGroup(
        group_id="approximate:test",
        verification=APPROXIMATE,
        verification_method="test",
        reference=exact.reference,
        duplicates=exact.duplicates,
    )
    mixed_report = ScanReport(
        scan_id=report.scan_id,
        created_at=report.created_at,
        roots=report.roots,
        mode=report.mode,
        groups=(exact, approximate),
        issues=(),
        coverage=report.coverage,
        summary=ScanSummary(
            discovered_files=2,
            hashed_files=2,
            verified_groups=1,
            duplicate_files=2,
            issues=0,
            complete=True,
        ),
    )

    plan = PlanService().create(mixed_report)

    assert len(plan.actions) == 1
    assert plan.actions[0].group_id == exact.group_id
    assert plan.actions[0].verification == "verified_exact"


def test_plan_always_refuses_an_incomplete_scan(tmp_path):
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path / "missing"),)))

    with pytest.raises(ValueError, match="disabled"):
        PlanService().create(report)


def test_plan_jsonl_round_trip(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    payload = "".join(iter_plan_jsonl(plan))

    loaded = load_deletion_plan(io.StringIO(payload))

    assert loaded.to_dict() == plan.to_dict()


def test_plan_rejects_roots_changed_without_matching_plan_id(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    payload = plan.to_dict()
    payload["roots"] = [str(tmp_path / "different-root")]

    with pytest.raises(SchemaError, match="plan_id"):
        load_deletion_plan(io.StringIO(json.dumps(payload)))


def test_apply_defaults_to_validating_dry_run_without_mutation(tmp_path):
    report, first, second, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    before = {str(path): path.read_bytes() for path in (first, second)}

    apply_report = ApplyService().apply(plan)

    assert apply_report.dry_run
    assert apply_report.ready == 1
    assert apply_report.stale == 0
    assert before == {str(path): path.read_bytes() for path in (first, second)}
    assert not (tmp_path / ".dupeguru-neo-quarantine").exists()


def test_dry_run_never_enters_operation_plan_or_directory_creation_path(tmp_path, monkeypatch):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    manager = QuarantineManager()

    def forbidden_prepare(*args, **kwargs):
        raise AssertionError("dry-run must not construct or persist an OperationPlan")

    monkeypatch.setattr(manager, "prepare", forbidden_prepare)
    apply_report = ApplyService(SafeActionApplyAdapter(manager)).apply(plan, dry_run=True)

    assert apply_report.ready == 1
    assert not (tmp_path / ".dupeguru-neo-quarantine").exists()


def test_apply_rejects_same_size_replacement_even_with_restored_mtime(tmp_path):
    report, first, second, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = plan.actions[0].target
    target_path = first if str(first) == target.path else second
    before_stat = target_path.stat()
    replacement = b"changed duplicate!"
    assert len(replacement) == target.size
    target_path.write_bytes(replacement)
    os.utime(target_path, ns=(before_stat.st_atime_ns, target.mtime_ns))

    apply_report = ApplyService().apply(plan)

    assert apply_report.dry_run
    assert apply_report.stale == 1
    assert target_path.read_bytes() == replacement


@pytest.mark.parametrize("dry_run", (True, False))
def test_apply_rejects_same_bytes_replacement_by_physical_identity(
    tmp_path,
    dry_run,
):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    action = plan.actions[0]
    target_path = Path(action.target.path)
    before = target_path.stat()
    payload = target_path.read_bytes()
    replacement = tmp_path / "same-bytes-replacement.tmp"
    replacement.write_bytes(payload)
    os.utime(
        replacement,
        ns=(before.st_atime_ns, action.target.mtime_ns),
    )
    os.replace(replacement, target_path)
    os.utime(
        target_path,
        ns=(before.st_atime_ns, action.target.mtime_ns),
    )

    apply_report = ApplyService().apply(plan, dry_run=dry_run)

    assert apply_report.stale == 1
    assert apply_report.applied == 0
    assert apply_report.results[0].failure_code == "identity_mismatch"
    assert target_path.read_bytes() == payload
    assert not tmp_path.joinpath(".dupeguru-neo-quarantine").exists()


def test_apply_rejects_action_outside_plan_roots(tmp_path):
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    report, first, _, _, _ = _scan_exact_fixture(scan_root)
    original_plan = PlanService().create(report)
    original_action = original_plan.actions[0]
    outside = tmp_path / "outside.bin"
    outside.write_bytes(first.read_bytes())
    outside_stat = outside.stat()
    outside_target = replace(
        original_action.target,
        path=str(outside),
        mtime_ns=outside_stat.st_mtime_ns,
        volume_id=str(outside_stat.st_dev) if outside_stat.st_dev else None,
        file_id=str(outside_stat.st_ino) if outside_stat.st_ino else None,
    )
    action = PlanAction(
        action_id=action_id_for(original_action.group_id, str(outside), original_action.operation),
        group_id=original_action.group_id,
        operation=original_action.operation,
        target=outside_target,
        reference=original_action.reference,
        verification=original_action.verification,
    )
    plan = DeletionPlan(
        plan_id=plan_id_for(original_plan.source_scan_id, original_plan.roots, [action]),
        created_at=original_plan.created_at,
        source_scan_id=original_plan.source_scan_id,
        roots=original_plan.roots,
        actions=(action,),
        engine_version=original_plan.engine_version,
    )

    apply_report = ApplyService().apply(plan)

    assert apply_report.stale == 1
    assert "outside" in apply_report.results[0].message
    assert outside.exists()


def test_execute_stages_target_and_persists_bound_operation_plan(tmp_path):
    report, first, second, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    keeper = Path(plan.actions[0].reference.path)

    apply_report = ApplyService().apply(plan, dry_run=False)

    assert apply_report.applied == 1
    result = apply_report.results[0]
    assert result.safe_state == "staged"
    assert result.failure_code == "none"
    assert not target.exists()
    assert keeper.exists()
    assert Path(result.quarantine_path).read_bytes() == b"verified duplicate"
    operation_path = Path(result.operation_plan_path)
    assert operation_path.is_file()
    assert operation_path.parent.joinpath("journal.jsonl").is_file()


def test_oversized_stored_operation_is_rejected_before_any_target_moves(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    keeper = Path(plan.actions[0].reference.path)
    before = {
        target: target.read_bytes(),
        keeper: keeper.read_bytes(),
    }
    manager = QuarantineManager()
    first = manager.prepare(plan)
    assert first.ok
    plan_path = first.prepared[0].plan_path
    stored_bytes = plan_path.read_bytes()
    monkeypatch.setattr(
        quarantine_module,
        "MAX_STORED_OPERATION_BYTES",
        len(stored_bytes) - 1,
    )

    second = manager.prepare(plan)

    assert not second.ok
    assert second.failures
    assert target.read_bytes() == before[target]
    assert keeper.read_bytes() == before[keeper]
    assert plan_path.read_bytes() == stored_bytes
    assert not first.prepared[0].stored.operation_plan.quarantine_path.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_hardlinked_stored_operation_is_rejected_before_any_target_moves(
    tmp_path,
):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    keeper = Path(plan.actions[0].reference.path)
    before = {
        target: target.read_bytes(),
        keeper: keeper.read_bytes(),
    }
    manager = QuarantineManager()
    first = manager.prepare(plan)
    assert first.ok
    plan_path = first.prepared[0].plan_path
    alias = tmp_path / "stored-operation-alias.json"
    try:
        os.link(plan_path, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    second = manager.prepare(plan)

    assert not second.ok
    assert second.failures
    assert target.read_bytes() == before[target]
    assert keeper.read_bytes() == before[keeper]
    assert plan_path.exists()
    assert alias.exists()
    assert not first.prepared[0].stored.operation_plan.quarantine_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode policy")
def test_world_writable_existing_quarantine_state_is_rejected_before_mutation(
    tmp_path,
):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    before = {Path(action.target.path): Path(action.target.path).read_bytes() for action in plan.actions}
    quarantine_root = tmp_path / ".dupeguru-neo-quarantine"
    quarantine_root.mkdir()
    quarantine_root.chmod(0o777)
    try:
        applied = ApplyService().apply(plan, dry_run=False)
    finally:
        quarantine_root.chmod(0o700)

    assert applied.applied == 0
    assert applied.stale
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_execute_validates_every_action_before_mutating_anything(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    third = tmp_path / "c.bin"
    for path in (first, second, third):
        path.write_bytes(b"same bytes")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)

    class RecordingAdapter(ApplyAdapter):
        def __init__(self):
            self.executed = []

        @property
        def supports_execute(self):
            return True

        def preflight(self, service_plan, persist):
            results = tuple(
                ActionResult(
                    action.action_id,
                    action.target.path,
                    "stale" if action == service_plan.actions[-1] else "ready",
                )
                for action in service_plan.actions
            )
            return ApplyPreparation(
                batch=PreparationBatch(service_plan.plan_id, (), ()),
                results=results,
            )

        def execute(self, preparation):
            self.executed.append("called")
            return ()

    adapter = RecordingAdapter()
    apply_report = ApplyService(adapter).apply(plan, dry_run=False)

    assert {result.status for result in apply_report.results} == {"ready", "stale"}
    assert adapter.executed == []


def test_real_execute_preflights_all_actions_before_moving_any_target(tmp_path):
    for name in ("a.bin", "b.bin", "c.bin"):
        (tmp_path / name).write_bytes(b"same bytes")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    stale_target = Path(plan.actions[-1].target.path)
    stale_target.write_bytes(b"new content")
    before = {path: path.read_bytes() for path in tmp_path.glob("*.bin")}

    apply_report = ApplyService().apply(plan, dry_run=False)

    assert apply_report.stale == 1
    assert apply_report.applied == 0
    assert before == {path: path.read_bytes() for path in tmp_path.glob("*.bin")}
    assert not (tmp_path / ".dupeguru-neo-quarantine").exists()


def test_quarantined_target_can_be_listed_and_restored(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    applied = ApplyService().apply(plan, dry_run=False)
    plan_path = Path(applied.results[0].operation_plan_path)
    manager = QuarantineManager()

    listed = manager.list([str(tmp_path)])
    restored = manager.restore(plan_path)

    assert len(listed) == 1
    assert listed[0]["state"] == "staged"
    assert restored.failure_code == "none"
    assert restored.safe_state == "restored"
    assert target.read_bytes() == b"verified duplicate"
    assert manager.list([str(tmp_path)])[0]["state"] == "restored"


def test_quarantine_service_restore_and_finalize_are_read_only_by_default(
    tmp_path,
):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    applied = ApplyService().apply(plan, dry_run=False)
    operation_path = Path(applied.results[0].operation_plan_path)
    quarantine_path = Path(applied.results[0].quarantine_path)
    journal_path = operation_path.parent / "journal.jsonl"
    journal_before = journal_path.read_bytes()
    service = QuarantineService()

    restore_preflight = service.restore(operation_path)
    finalize_preflight = service.finalize(operation_path)

    assert restore_preflight["dry_run"] is True
    assert restore_preflight["result"]["status"] == "ready"
    assert finalize_preflight["dry_run"] is True
    assert finalize_preflight["result"]["status"] == "ready"
    assert not target.exists()
    assert quarantine_path.exists()
    assert journal_path.read_bytes() == journal_before


def test_plan_service_rejects_legacy_delete_operation(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)

    with pytest.raises(
        ValueError,
        match="only support recoverable quarantine",
    ):
        PlanService().create(report, operation="delete")

    plan = PlanService().create(report)
    legacy = plan.to_dict()
    legacy["actions"][0]["operation"] = "delete"
    with pytest.raises(
        SchemaError,
        match="only support recoverable quarantine",
    ):
        load_deletion_plan(io.StringIO(json.dumps(legacy)))


def test_stored_operation_rejects_legacy_delete_mode(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    applied = ApplyService().apply(plan, dry_run=False)
    stored = QuarantineManager().load(Path(applied.results[0].operation_plan_path))
    legacy = stored.to_dict()
    legacy["operation"] = "delete"

    with pytest.raises(
        QuarantineError,
        match="not a recoverable quarantine operation",
    ):
        StoredOperation.from_dict(legacy)
    with pytest.raises(
        QuarantineError,
        match="not a recoverable quarantine operation",
    ):
        StoredOperation(
            service_plan_id=stored.service_plan_id,
            action_id=stored.action_id,
            operation="delete",
            operation_plan=stored.operation_plan,
        )


def test_apply_never_calls_finalize_and_leaves_every_action_staged(
    tmp_path,
    monkeypatch,
):
    for name in ("a.bin", "b.bin", "c.bin"):
        (tmp_path / name).write_bytes(b"same bytes")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    manager = QuarantineManager()
    batch = manager.prepare(plan)

    class FakeExecutor:
        def stage(self, operation_plan):
            return SafeActionResult(
                operation_plan.plan_id,
                ActionState.STAGED,
                FailureCode.NONE,
                "staged",
                True,
                str(operation_plan.quarantine_path),
            )

        def finalize(self, operation_plan):
            raise AssertionError("apply must not finalize staged payloads")

    monkeypatch.setattr(manager, "_executor", lambda operation_plan: FakeExecutor())

    results = manager.execute(batch)

    assert [item.status for item in results] == ["applied", "applied"]
    assert [item.safe_state for item in results] == ["staged", "staged"]
    assert all(item.changed for item in results)


def test_stage_failure_after_change_rolls_back_failed_and_prior_actions(tmp_path, monkeypatch):
    for name in ("a.bin", "b.bin", "c.bin"):
        (tmp_path / name).write_bytes(b"same bytes")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    manager = QuarantineManager()
    batch = manager.prepare(plan)
    failed_plan_id = batch.prepared[-1].stored.operation_plan.plan_id

    class FakeExecutor:
        def stage(self, operation_plan):
            if operation_plan.plan_id == failed_plan_id:
                return SafeActionResult(
                    operation_plan.plan_id,
                    ActionState.STAGED,
                    FailureCode.JOURNAL_ERROR,
                    "moved but completion journal failed",
                    True,
                    str(operation_plan.quarantine_path),
                )
            return SafeActionResult(
                operation_plan.plan_id,
                ActionState.STAGED,
                FailureCode.NONE,
                "staged",
                True,
                str(operation_plan.quarantine_path),
            )

        def restore(self, operation_plan):
            return SafeActionResult(
                operation_plan.plan_id,
                ActionState.RESTORED,
                FailureCode.NONE,
                "restored",
                True,
                str(operation_plan.quarantine_path),
            )

    monkeypatch.setattr(manager, "_executor", lambda operation_plan: FakeExecutor())

    results = manager.execute(batch)

    assert [item.status for item in results] == ["failed", "failed"]
    assert [item.safe_state for item in results] == ["restored", "restored"]
    assert results[-1].failure_code == "journal_error"
    assert all("rollback: restored" in item.message for item in results)


def test_changed_keeper_fails_closed_before_target_moves(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    target = Path(plan.actions[0].target.path)
    keeper = Path(plan.actions[0].reference.path)
    target_bytes = target.read_bytes()
    keeper.write_bytes(b"changed reference!")

    applied = ApplyService().apply(plan, dry_run=False)

    assert applied.applied == 0
    assert applied.stale == 1
    assert target.read_bytes() == target_bytes
    assert not (tmp_path / ".dupeguru-neo-quarantine").exists()


def test_trash_operation_is_not_part_of_the_safe_cli_contract(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)

    with pytest.raises(ValueError, match="Unsupported"):
        PlanService().create(report, operation="trash")


def test_operation_plan_persistence_never_overwrites_an_existing_path(tmp_path):
    report, *_ = _scan_exact_fixture(tmp_path)
    plan = PlanService().create(report)
    action = plan.actions[0]
    operation_root = (
        tmp_path / ".dupeguru-neo-quarantine" / "operations" / operation_plan_id(plan.plan_id, action.action_id)
    )
    operation_root.mkdir(parents=True)
    path = operation_root / "operation.json"
    original = b'{"untrusted":"existing"}\n'
    path.write_bytes(original)

    applied = ApplyService().apply(plan, dry_run=False)

    assert applied.failed == 1
    assert applied.applied == 0
    assert path.read_bytes() == original
    assert Path(action.target.path).exists()


def test_query_filters_by_path_digest_and_group_id(tmp_path):
    report, first, *_ = _scan_exact_fixture(tmp_path)
    group = report.groups[0]
    service = QueryService()

    by_path = service.query(report, path=str(first))
    by_digest = service.query(report, digest=group.reference.digest)
    by_group = service.query(report, group_id=group.group_id)
    missing = service.query(report, digest="not-present")

    assert by_path["summary"]["groups"] == 1
    assert by_digest["summary"]["groups"] == 1
    assert by_group["summary"]["groups"] == 1
    assert missing["summary"]["groups"] == 0


def test_scan_report_schema_name_is_stable():
    assert SCAN_REPORT_SCHEMA == "dupeguru.scan-report"
