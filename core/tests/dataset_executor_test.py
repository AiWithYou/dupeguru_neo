# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import core.dataset_executor as executor_module
import core.dataset_service as dataset_module
from core.dataset_executor import (
    DatasetBundleExecutor,
    DatasetExecutionError,
    ExecutionCode,
    ExecutionState,
)
from core.dataset_service import (
    DatasetAsset,
    DatasetCluster,
    DatasetModeService,
    DatasetOperation,
    DatasetRelation,
    PreparationState,
)
from core.reserved_paths import (
    is_within_reserved_internal_directory,
)


def build_plan(tmp_path, *, dry_run=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    destination = tmp_path / "organized"
    state = tmp_path / "state"
    source.mkdir()
    destination.mkdir()
    best = source / "best.jpg"
    duplicate = source / "photo copy.jpg"
    best_sidecar = source / "best.txt"
    duplicate_sidecar = source / "photo copy.txt"
    best.write_bytes(b"identical image payload")
    duplicate.write_bytes(b"identical image payload")
    best_sidecar.write_text("identical caption", encoding="utf-8")
    duplicate_sidecar.write_text("identical caption", encoding="utf-8")
    assets = (
        DatasetAsset(
            "best",
            str(best),
            dimensions=(4096, 3072),
            bit_depth=16,
            metadata_count=10,
            protected=True,
        ),
        DatasetAsset(
            "duplicate",
            str(duplicate),
            dimensions=(1024, 768),
            bit_depth=8,
            metadata_count=1,
        ),
    )
    clusters = (DatasetCluster(("best", "duplicate"), DatasetRelation.VERIFIED_EXACT),)
    service = DatasetModeService()
    preparation = service.prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
        dry_run=dry_run,
    )
    assert preparation.state is PreparationState.COMPLETE
    assert preparation.plan is not None
    return {
        "source": source,
        "destination": destination,
        "state": state,
        "best": best,
        "duplicate": duplicate,
        "best_sidecar": best_sidecar,
        "duplicate_sidecar": duplicate_sidecar,
        "service": service,
        "plan": preparation.plan,
    }


def source_payloads(case):
    return {
        path.name: path.read_bytes()
        for path in (
            case["best"],
            case["duplicate"],
            case["best_sidecar"],
            case["duplicate_sidecar"],
        )
        if path.exists()
    }


def state_path(case):
    return case["state"] / executor_module.STATE_DIRECTORY_NAME


def move_destinations(plan):
    return tuple(
        Path(item.destination)
        for action in plan.actions
        if action.operation is DatasetOperation.MOVE_BUNDLE
        for item in action.files
        if item.destination is not None
    )


def quarantine_records(executor, case):
    document = executor._load_required_document(state_path(case), case["plan"].plan_id)
    return tuple(record for record in document.files if record.quarantine_path is not None)


def operation_directory(case):
    return state_path(case) / "operations" / case["plan"].plan_id


def operation_document_path(case):
    return operation_directory(case) / "operation.json"


def operation_journal_path(case):
    return operation_directory(case) / "journal.jsonl"


def journal_values(case):
    return tuple(json.loads(line) for line in operation_journal_path(case).read_text(encoding="utf-8").splitlines())


def replace_action_contents(action, *, file_index=None, file_action=None, split=None):
    files = tuple(file_action if index == file_index else item for index, item in enumerate(action.files))
    replacement_split = split if split is not None else action.split
    identity = {
        "asset_id": action.asset_id,
        "cluster_id": action.cluster_id,
        "split": replacement_split,
        "operation": action.operation.value,
        "files": [
            item.to_dict() for item in sorted(files, key=lambda item: (item.role, item.sidecar_slot, item.source.path))
        ],
        "keeper_id": action.keeper_id,
        "atomic": action.atomic,
    }
    return replace(
        action,
        action_id=dataset_module._content_id(identity),
        files=files,
        split=replacement_split,
    )


def replace_plan(case, *, actions=None, destination_root=None, split_manifest=None):
    original = case["plan"]
    replacement_actions = tuple(actions if actions is not None else original.actions)
    replacement_destination = str(destination_root if destination_root is not None else original.destination_root)
    replacement_manifest = split_manifest if split_manifest is not None else original.split_manifest
    identity = {
        "schema": original.schema,
        "schema_version": original.schema_version,
        "allowed_roots": list(original.allowed_roots),
        "destination_root": replacement_destination,
        "split_manifest": replacement_manifest.to_dict(),
        "keepers": [keeper.to_dict() for keeper in original.keepers],
        "actions": [action.to_dict() for action in sorted(replacement_actions, key=lambda item: item.action_id)],
        "dry_run": original.dry_run,
        "executor_contract": original.executor_contract,
    }
    return replace(
        original,
        plan_id=dataset_module._content_id(identity),
        destination_root=replacement_destination,
        split_manifest=replacement_manifest,
        actions=replacement_actions,
    )


def latest_tombstone(case, event_name):
    event = next(value for value in reversed(journal_values(case)) if value["event"] == event_name)
    return Path(event["details"]["tombstone_path"])


def test_configured_state_root_has_one_state_base_contract(tmp_path):
    state_base = tmp_path / "state-base"
    executor = DatasetBundleExecutor(state_root=state_base)

    assert executor.configured_state_root == (state_base / executor_module.STATE_DIRECTORY_NAME)
    with pytest.raises(ValueError, match="outside every private namespace"):
        DatasetBundleExecutor(
            state_root=state_base / executor_module.STATE_DIRECTORY_NAME,
        )


@pytest.mark.parametrize(
    "forged_field",
    ("source", "destination", "reference", "destination_root"),
)
def test_executor_rejects_forged_plan_paths_inside_internal_directories(
    tmp_path,
    monkeypatch,
    forged_field,
):
    case = build_plan(tmp_path)
    reserved_name = ".dupeguru-neo-dataset-quarantine"
    actions = list(case["plan"].actions)
    if forged_field == "destination_root":
        reserved = tmp_path / reserved_name
        reserved.mkdir()
        forged = replace_plan(case, destination_root=reserved)
    else:
        if forged_field == "destination":
            action_index = next(
                index for index, action in enumerate(actions) if action.operation is DatasetOperation.MOVE_BUNDLE
            )
            action = actions[action_index]
            item = action.files[0]
            reserved_path = case["destination"] / reserved_name / "payload"
            forged_item = replace(item, destination=str(reserved_path))
        else:
            action_index = next(
                index for index, action in enumerate(actions) if action.operation is DatasetOperation.QUARANTINE_BUNDLE
            )
            action = actions[action_index]
            item = action.files[0]
            reserved_path = case["source"] / reserved_name / "operation" / "payload"
            if forged_field == "source":
                forged_item = replace(
                    item,
                    source=replace(
                        item.source,
                        path=str(reserved_path),
                        resolved_path=str(reserved_path),
                    ),
                )
            else:
                assert item.reference is not None
                forged_item = replace(
                    item,
                    reference=replace(
                        item.reference,
                        path=str(reserved_path),
                        resolved_path=str(reserved_path),
                    ),
                )
        actions[action_index] = replace_action_contents(
            action,
            file_index=0,
            file_action=forged_item,
        )
        forged = replace_plan(case, actions=actions)

    def forbidden_revalidation(_plan):
        raise AssertionError("executor must reject reserved paths before service revalidation")

    monkeypatch.setattr(case["service"], "revalidate", forbidden_revalidation)
    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).apply(forged, execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.UNSAFE_PATH
    assert not report.changed
    assert source_payloads(case) == {
        "best.jpg": b"identical image payload",
        "photo copy.jpg": b"identical image payload",
        "best.txt": b"identical caption",
        "photo copy.txt": b"identical caption",
    }
    assert list(case["destination"].iterdir()) == []
    assert not case["state"].exists()


def test_executor_rejects_forged_reserved_split_before_state_creation(tmp_path, monkeypatch):
    case = build_plan(tmp_path)
    reserved_split = ".dupeguru-neo-dataset-executor"
    manifest = replace(
        case["plan"].split_manifest,
        split_weights=((reserved_split, 1.0),),
        assignments=tuple(
            replace(assignment, split=reserved_split) for assignment in case["plan"].split_manifest.assignments
        ),
    )
    actions = tuple(replace_action_contents(action, split=reserved_split) for action in case["plan"].actions)
    forged = replace_plan(
        case,
        actions=actions,
        split_manifest=manifest,
    )

    def forbidden_revalidation(_plan):
        raise AssertionError("executor must reject reserved split before revalidation")

    monkeypatch.setattr(case["service"], "revalidate", forbidden_revalidation)
    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).apply(forged, execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.UNSAFE_PATH
    assert not report.changed
    assert not case["state"].exists()
    assert list(case["destination"].iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Win32 ADS path components")
def test_executor_rejects_forged_ads_split_before_state_creation(tmp_path, monkeypatch):
    case = build_plan(tmp_path)
    ads_split = ".dupeguru-neo-dataset-executor::$INDEX_ALLOCATION"
    manifest = replace(
        case["plan"].split_manifest,
        split_weights=((ads_split, 1.0),),
        assignments=tuple(
            replace(assignment, split=ads_split) for assignment in case["plan"].split_manifest.assignments
        ),
    )
    actions = tuple(replace_action_contents(action, split=ads_split) for action in case["plan"].actions)
    forged = replace_plan(
        case,
        actions=actions,
        split_manifest=manifest,
    )

    monkeypatch.setattr(
        case["service"],
        "revalidate",
        lambda _plan: (_ for _ in ()).throw(AssertionError("reserved ADS split reached revalidation")),
    )
    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).apply(forged, execute=True)

    assert report.code is ExecutionCode.UNSAFE_PATH
    assert not report.changed
    assert not case["state"].exists()


def test_mutation_boundary_rejects_reserved_destination_even_after_preflight(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    records = executor._preflight(case["plan"], state_path(case))
    record = next(item for item in records if item.operation is DatasetOperation.MOVE_BUNDLE and item.role == "primary")
    reserved_destination = case["destination"] / ".dupeguru-neo-dataset-executor" / "operations" / "payload"
    forged_record = replace(
        record,
        destination=str(reserved_destination),
        comparison_path=str(reserved_destination),
    )

    with pytest.raises(DatasetExecutionError) as raised:
        executor._stage_same_volume_move(forged_record)

    assert raised.value.code is ExecutionCode.UNSAFE_PATH
    assert case["best"].is_file()
    assert not reserved_destination.exists()


def test_organized_root_can_be_prepared_again_without_internal_state_sidecars(tmp_path):
    case = build_plan(tmp_path / "initial")
    executor = DatasetBundleExecutor(service=case["service"])
    applied = executor.apply(case["plan"], execute=True)
    assert applied.state is ExecutionState.APPLIED
    assert (case["destination"] / executor_module.STATE_DIRECTORY_NAME).is_dir()
    organized_primary = next(path for path in move_destinations(case["plan"]) if path.suffix.lower() == ".jpg")
    next_destination = tmp_path / "next-organized"
    next_destination.mkdir()

    prepared_again = DatasetModeService().prepare(
        (DatasetAsset("organized", str(organized_primary)),),
        (),
        allowed_roots=(case["destination"],),
        destination_root=next_destination,
        sidecar_paths=None,
        split_weights={"train": 1},
        dry_run=True,
    )

    assert prepared_again.state is PreparationState.COMPLETE
    assert prepared_again.plan is not None
    assert all(
        not is_within_reserved_internal_directory(item.source.path)
        for action in prepared_again.plan.actions
        for item in action.files
    )


def test_existing_dataset_quarantine_payload_is_untouched_and_restorable(tmp_path):
    case = build_plan(tmp_path / "initial")
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).state is ExecutionState.APPLIED
    quarantined = quarantine_records(executor, case)
    payload = Path(quarantined[0].quarantine_path)
    payload_bytes = payload.read_bytes()
    attempted_destination = tmp_path / "attempted-organized"
    attempted_destination.mkdir()

    attempted = DatasetModeService().prepare(
        (DatasetAsset("managed-payload", str(payload)),),
        (),
        allowed_roots=(case["source"],),
        destination_root=attempted_destination,
        sidecar_paths=(),
        split_weights={"train": 1},
        dry_run=False,
    )

    assert attempted.state is PreparationState.FAILED
    assert attempted.issues[0].code == "reserved_internal_path"
    assert payload.read_bytes() == payload_bytes
    assert list(attempted_destination.iterdir()) == []

    restored = executor.restore(case["plan"].plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert restored.ok
    assert source_payloads(case) == {
        "best.jpg": b"identical image payload",
        "photo copy.jpg": b"identical image payload",
        "best.txt": b"identical caption",
        "photo copy.txt": b"identical caption",
    }


def test_custom_state_base_inside_destination_uses_reserved_namespace_and_remains_restorable(
    tmp_path,
):
    case = build_plan(tmp_path / "initial")
    state_base = case["destination"] / "custom-state-base"
    executor = DatasetBundleExecutor(
        state_root=state_base,
        service=case["service"],
    )
    applied = executor.apply(case["plan"], execute=True)
    assert applied.state is ExecutionState.APPLIED
    actual_state = state_base / executor_module.STATE_DIRECTORY_NAME
    operation_document = (
        actual_state / "operations" / case["plan"].plan_id / executor_module.OPERATION_DOCUMENT_FILENAME
    )
    assert operation_document.is_file()

    attempted_destination = tmp_path / "metadata-escape"
    attempted_destination.mkdir()
    attempted = DatasetModeService().prepare(
        (DatasetAsset("operation-metadata", str(operation_document)),),
        (),
        allowed_roots=(case["destination"],),
        destination_root=attempted_destination,
        sidecar_paths=(),
        split_weights={"train": 1},
        dry_run=False,
    )
    assert attempted.state is PreparationState.FAILED
    assert attempted.issues[0].code == "reserved_internal_path"
    assert operation_document.is_file()
    assert list(attempted_destination.iterdir()) == []

    restored = executor.restore(case["plan"].plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert restored.ok
    assert len(source_payloads(case)) == 4


def test_state_base_cannot_be_a_planned_destination_path(tmp_path):
    case = build_plan(tmp_path)
    planned_destination = move_destinations(case["plan"])[0]
    executor = DatasetBundleExecutor(
        state_root=planned_destination,
        service=case["service"],
    )
    before = source_payloads(case)

    report = executor.apply(case["plan"], execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.UNSAFE_PATH
    assert not report.changed
    assert source_payloads(case) == before
    assert not planned_destination.exists()
    assert not planned_destination.joinpath(executor_module.STATE_DIRECTORY_NAME).exists()
    assert list(case["destination"].iterdir()) == []


def test_copied_operation_tree_cannot_be_replayed_from_another_state_base(tmp_path):
    case = build_plan(tmp_path / "original")
    original = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    assert original.apply(case["plan"], execute=True).state is ExecutionState.APPLIED
    original_operation = state_path(case) / "operations" / case["plan"].plan_id
    copied_base = tmp_path / "copied-state"
    copied_operation = copied_base / executor_module.STATE_DIRECTORY_NAME / "operations" / case["plan"].plan_id
    copied_operation.parent.mkdir(parents=True)
    shutil.copytree(original_operation, copied_operation)
    payloads_before = {
        str(path): path.read_bytes()
        for path in (
            *move_destinations(case["plan"]),
            *(Path(record.quarantine_path) for record in quarantine_records(original, case)),
        )
        if path.is_file()
    }
    copied = DatasetBundleExecutor(state_root=copied_base)

    summaries = copied.list_operations()
    restore = copied.restore(case["plan"].plan_id, execute=True)
    finalize = copied.finalize(case["plan"].plan_id, execute=True)

    assert len(summaries) == 1
    assert summaries[0].state is ExecutionState.RECOVERY_REQUIRED
    assert restore.state is ExecutionState.FAILED
    assert restore.code is ExecutionCode.DOCUMENT_CONFLICT
    assert finalize.state is ExecutionState.FAILED
    assert finalize.code is ExecutionCode.DOCUMENT_CONFLICT
    assert {path: Path(path).read_bytes() for path in payloads_before} == payloads_before


def test_dry_run_and_non_explicit_apply_are_completely_read_only(tmp_path):
    dry_case = build_plan(tmp_path / "dry", dry_run=True)
    dry_executor = DatasetBundleExecutor(
        state_root=dry_case["state"],
        service=dry_case["service"],
    )
    before = source_payloads(dry_case)
    dry_report = dry_executor.apply(dry_case["plan"], execute=True)
    assert dry_report.state is ExecutionState.DRY_RUN
    assert dry_report.ok
    assert source_payloads(dry_case) == before
    assert list(dry_case["destination"].iterdir()) == []
    assert not dry_case["state"].exists()

    ready_case = build_plan(tmp_path / "ready", dry_run=False)
    ready_executor = DatasetBundleExecutor(
        state_root=ready_case["state"],
        service=ready_case["service"],
    )
    before = source_payloads(ready_case)
    ready_report = ready_executor.apply(ready_case["plan"])
    assert ready_report.state is ExecutionState.READY
    assert ready_report.code is ExecutionCode.EXECUTION_NOT_EXPLICIT
    assert source_payloads(ready_case) == before
    assert list(ready_case["destination"].iterdir()) == []
    assert not ready_case["state"].exists()


def test_apply_moves_keeper_and_quarantines_complete_duplicate_bundle(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    report = executor.apply(case["plan"], execute=True)
    assert report.state is ExecutionState.APPLIED
    assert report.ok
    assert report.changed
    assert len(report.files) == 4
    assert not any(
        path.exists()
        for path in (
            case["best"],
            case["duplicate"],
            case["best_sidecar"],
            case["duplicate_sidecar"],
        )
    )
    destinations = move_destinations(case["plan"])
    assert len(destinations) == 2
    assert all(path.is_file() for path in destinations)
    quarantined = quarantine_records(executor, case)
    assert len(quarantined) == 2
    assert all(Path(record.quarantine_path).is_file() for record in quarantined)

    operation_document = operation_document_path(case)
    value = json.loads(operation_document.read_text(encoding="utf-8"))
    assert value["schema"] == "dupeguru.dataset-execution"
    assert value["plan_id"] == case["plan"].plan_id
    assert value["document_hash"]
    journal_lines = operation_journal_path(case).read_text(encoding="utf-8").splitlines()
    assert journal_lines
    assert all(json.loads(line)["record_hash"] for line in journal_lines)

    summaries = executor.list_operations()
    assert len(summaries) == 1
    assert summaries[0].state is ExecutionState.APPLIED
    assert summaries[0].file_count == 4


def test_apply_is_idempotent_and_recovers_a_missing_commit_record(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).state is ExecutionState.APPLIED
    journal_path = operation_journal_path(case)
    lines = journal_path.read_bytes().splitlines(keepends=True)
    assert json.loads(lines[-1])["event"] == "applied"
    journal_path.write_bytes(b"".join(lines[:-1]))

    replay = executor.apply(case["plan"], execute=True)
    assert replay.state is ExecutionState.ALREADY_APPLIED
    assert replay.ok
    assert not replay.changed
    final_event = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[-1])
    assert final_event["event"] == "applied_recovered"

    second = executor.apply(case["plan"], execute=True)
    assert second.state is ExecutionState.ALREADY_APPLIED
    assert second.ok
    assert not second.changed


def test_mixed_crash_state_is_rolled_back_then_replayed_as_one_plan(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).state is ExecutionState.APPLIED
    record = quarantine_records(executor, case)[0]
    Path(record.quarantine_path).rename(record.source.path)

    replay = executor.apply(case["plan"], execute=True)
    assert replay.state is ExecutionState.APPLIED
    assert replay.ok
    document = executor._load_required_document(state_path(case), case["plan"].plan_id)
    assert all(executor._record_presence(item) == "applied" for item in document.files)
    events = [value["event"] for value in journal_values(case)]
    assert "rolled_back" in events
    assert events[-1] == "applied"


def test_restore_is_explicit_and_restores_every_primary_and_sidecar(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).ok
    preview = executor.restore(case["plan"].plan_id)
    assert preview.state is ExecutionState.READY
    assert preview.code is ExecutionCode.EXECUTION_NOT_EXPLICIT
    assert not case["best"].exists()

    restored = executor.restore(case["plan"].plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert restored.ok
    assert source_payloads(case) == {
        "best.jpg": b"identical image payload",
        "photo copy.jpg": b"identical image payload",
        "best.txt": b"identical caption",
        "photo copy.txt": b"identical caption",
    }
    assert not any(path.exists() for path in move_destinations(case["plan"]))
    assert executor.restore(case["plan"].plan_id, execute=True).changed is False


def test_finalize_is_separate_explicit_and_irreversible(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).ok
    quarantine_paths = tuple(Path(record.quarantine_path) for record in quarantine_records(executor, case))

    preview = executor.finalize(case["plan"].plan_id)
    assert preview.state is ExecutionState.READY
    assert all(path.exists() for path in quarantine_paths)
    finalized = executor.finalize(case["plan"].plan_id, execute=True)
    assert finalized.state is ExecutionState.FINALIZED
    assert finalized.ok
    assert not any(path.exists() for path in quarantine_paths)
    assert all(path.exists() for path in move_destinations(case["plan"]))
    repeated = executor.finalize(case["plan"].plan_id, execute=True)
    assert repeated.state is ExecutionState.FINALIZED
    assert repeated.ok
    assert not repeated.changed
    restore = executor.restore(case["plan"].plan_id, execute=True)
    assert restore.state is ExecutionState.FAILED
    assert restore.code is ExecutionCode.INVALID_STATE


@pytest.mark.parametrize(
    "crash_phase",
    ("after_finalize_tombstone", "after_finalize_tombstoned_record", "after_finalize_purge"),
)
def test_finalize_replays_every_durable_tombstone_interruption(tmp_path, crash_phase):
    case = build_plan(tmp_path)
    assert (
        DatasetBundleExecutor(state_root=case["state"], service=case["service"])
        .apply(
            case["plan"],
            execute=True,
        )
        .ok
    )
    interrupted = False

    def stop_finalize(phase, _record):
        nonlocal interrupted
        if phase == crash_phase and not interrupted:
            interrupted = True
            raise OSError(5, "injected finalize interruption")

    first = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=stop_finalize,
    ).finalize(case["plan"].plan_id, execute=True)
    assert interrupted
    assert first.state is ExecutionState.RECOVERY_REQUIRED

    replay = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).finalize(case["plan"].plan_id, execute=True)
    assert replay.state is ExecutionState.FINALIZED
    assert replay.ok
    assert not any(
        Path(record.quarantine_path).exists()
        for record in quarantine_records(
            DatasetBundleExecutor(state_root=case["state"], service=case["service"]),
            case,
        )
    )
    assert all(path.is_file() for path in move_destinations(case["plan"]))
    assert journal_values(case)[-1]["event"] == "finalized"


def test_finalize_both_names_collision_preserves_every_byte(tmp_path):
    case = build_plan(tmp_path)
    base = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert base.apply(case["plan"], execute=True).ok
    stopped = False

    def interrupt_after_rename(phase, _record):
        nonlocal stopped
        if phase == "after_finalize_tombstone" and not stopped:
            stopped = True
            raise OSError(5, "stop after rename")

    first = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=interrupt_after_rename,
    ).finalize(case["plan"].plan_id, execute=True)
    assert first.state is ExecutionState.RECOVERY_REQUIRED
    tombstone = latest_tombstone(case, "finalize_tombstone_prepared")
    prepared = next(record for record in quarantine_records(base, case) if not Path(record.quarantine_path).exists())
    quarantine = Path(prepared.quarantine_path)
    external = b"external quarantine collision"
    quarantine.write_bytes(external)
    tombstone_before = tombstone.read_bytes()

    replay = base.finalize(case["plan"].plan_id, execute=True)
    assert replay.state is ExecutionState.FAILED
    assert replay.code is ExecutionCode.DESTINATION_CONFLICT
    assert quarantine.read_bytes() == external
    assert tombstone.read_bytes() == tombstone_before


def test_finalize_external_tombstone_collision_is_never_removed(tmp_path):
    case = build_plan(tmp_path)
    base = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert base.apply(case["plan"], execute=True).ok
    collision = None

    def create_collision(phase, _record):
        nonlocal collision
        if phase == "before_finalize_tombstone" and collision is None:
            collision = latest_tombstone(case, "finalize_tombstone_prepared")
            collision.write_bytes(b"external tombstone collision")

    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=create_collision,
    ).finalize(case["plan"].plan_id, execute=True)
    assert report.state is ExecutionState.RECOVERY_REQUIRED
    assert report.code is ExecutionCode.DESTINATION_CONFLICT
    assert collision is not None
    assert collision.read_bytes() == b"external tombstone collision"
    assert all(Path(record.quarantine_path).exists() for record in quarantine_records(base, case))


def test_finalize_tombstone_replacement_and_keeper_replacement_fail_closed(tmp_path):
    tombstone_case = build_plan(tmp_path / "tombstone")
    tombstone_base = DatasetBundleExecutor(
        state_root=tombstone_case["state"],
        service=tombstone_case["service"],
    )
    assert tombstone_base.apply(tombstone_case["plan"], execute=True).ok
    external_tombstone = None

    def replace_tombstone(phase, _record):
        nonlocal external_tombstone
        if phase == "before_finalize_purge" and external_tombstone is None:
            tombstone = latest_tombstone(tombstone_case, "file_tombstoned")
            tombstone.replace(tombstone.with_suffix(".verified-backup"))
            tombstone.write_bytes(b"external replacement")
            external_tombstone = tombstone

    replaced = DatasetBundleExecutor(
        state_root=tombstone_case["state"],
        service=tombstone_case["service"],
        fault_hook=replace_tombstone,
    ).finalize(tombstone_case["plan"].plan_id, execute=True)
    assert replaced.state is ExecutionState.RECOVERY_REQUIRED
    assert replaced.code is ExecutionCode.SOURCE_CHANGED
    assert external_tombstone is not None
    assert external_tombstone.read_bytes() == b"external replacement"

    keeper_case = build_plan(tmp_path / "keeper")
    keeper_base = DatasetBundleExecutor(
        state_root=keeper_case["state"],
        service=keeper_case["service"],
    )
    assert keeper_base.apply(keeper_case["plan"], execute=True).ok
    keeper_record = quarantine_records(keeper_base, keeper_case)[0]
    keeper_path = Path(keeper_record.comparison_path)
    original_bytes = keeper_path.read_bytes()
    keeper_replaced = False

    def replace_keeper(phase, _record):
        nonlocal keeper_replaced
        if phase == "before_finalize_tombstone" and not keeper_replaced:
            replacement = keeper_path.with_suffix(".replacement")
            replacement.write_bytes(original_bytes)
            keeper_replaced = True
            os.replace(replacement, keeper_path)

    keeper_report = DatasetBundleExecutor(
        state_root=keeper_case["state"],
        service=keeper_case["service"],
        fault_hook=replace_keeper,
    ).finalize(keeper_case["plan"].plan_id, execute=True)
    assert keeper_replaced, keeper_report
    assert keeper_report.state is ExecutionState.RECOVERY_REQUIRED
    assert keeper_report.code in {ExecutionCode.SOURCE_CHANGED, ExecutionCode.IO_ERROR}
    assert keeper_path.read_bytes() == original_bytes
    assert all(Path(record.quarantine_path).exists() for record in quarantine_records(keeper_base, keeper_case))


def test_finalize_rejects_same_bytes_at_the_quarantine_path_with_a_new_identity(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).ok
    record = quarantine_records(executor, case)[0]
    quarantine = Path(record.quarantine_path)
    payload = quarantine.read_bytes()
    displaced = quarantine.with_suffix(".original-inode")
    quarantine.replace(displaced)
    quarantine.write_bytes(payload)

    report = executor.finalize(case["plan"].plan_id, execute=True)
    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.SOURCE_CHANGED
    assert quarantine.read_bytes() == payload
    assert displaced.read_bytes() == payload


def test_destination_collision_fails_before_any_state_or_source_mutation(tmp_path):
    case = build_plan(tmp_path)
    collision = move_destinations(case["plan"])[0]
    collision.parent.mkdir()
    collision.write_bytes(b"unrelated")
    before = source_payloads(case)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    report = executor.apply(case["plan"], execute=True)
    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.PLAN_INVALID
    assert source_payloads(case) == before
    assert collision.read_bytes() == b"unrelated"
    assert not case["state"].exists()


def test_failure_after_keeper_moves_rolls_back_the_whole_plan(tmp_path):
    case = build_plan(tmp_path)

    def fail_before_quarantine(phase, _record):
        if phase == "before_source_quarantine":
            raise OSError(5, "injected quarantine failure")

    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=fail_before_quarantine,
    )
    report = executor.apply(case["plan"], execute=True)
    assert report.state is ExecutionState.ROLLED_BACK
    assert report.code is ExecutionCode.EXECUTION_FAILED
    assert source_payloads(case) == {
        "best.jpg": b"identical image payload",
        "photo copy.jpg": b"identical image payload",
        "best.txt": b"identical caption",
        "photo copy.txt": b"identical caption",
    }
    assert not any(path.exists() for path in move_destinations(case["plan"]))
    document = executor._load_required_document(state_path(case), case["plan"].plan_id)
    assert all(executor._record_presence(record) == "original" for record in document.files)


def test_destination_race_never_overwrites_or_removes_external_file(tmp_path):
    case = build_plan(tmp_path)
    collided = []

    def create_collision(phase, record):
        if phase == "before_same_volume_publish" and not collided:
            destination = Path(record.destination)
            destination.write_bytes(b"external winner")
            collided.append(destination)

    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=create_collision,
    )
    report = executor.apply(case["plan"], execute=True)
    assert report.state is ExecutionState.ROLLED_BACK
    assert report.code is ExecutionCode.EXECUTION_FAILED
    assert len(collided) == 1
    assert collided[0].read_bytes() == b"external winner"
    assert len(source_payloads(case)) == 4


def test_metadata_race_immediately_before_quarantine_rolls_back_every_file(tmp_path):
    case = build_plan(tmp_path)
    raced = False

    def mutate_source(phase, record):
        nonlocal raced
        if phase == "before_source_quarantine" and not raced:
            source = Path(record.source.path)
            payload = source.read_bytes()
            source.write_bytes(payload)
            raced = True

    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fault_hook=mutate_source,
    )
    report = executor.apply(case["plan"], execute=True)
    assert raced
    assert report.state is ExecutionState.ROLLED_BACK
    assert report.code is ExecutionCode.EXECUTION_FAILED
    assert len(source_payloads(case)) == 4
    assert not any(path.exists() for path in move_destinations(case["plan"]))


def test_cross_volume_copy_is_verified_then_source_is_quarantined(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
    )
    applied = executor.apply(case["plan"], execute=True)
    assert applied.state is ExecutionState.APPLIED
    document = executor._load_required_document(state_path(case), case["plan"].plan_id)
    move_records = tuple(record for record in document.files if record.operation is DatasetOperation.MOVE_BUNDLE)
    assert all(
        Path(record.destination).read_bytes() == Path(record.quarantine_path).read_bytes() for record in move_records
    )
    assert all(not Path(record.source.path).exists() for record in move_records)
    restored = executor.restore(case["plan"].plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert source_payloads(case).keys() == {
        "best.jpg",
        "photo copy.jpg",
        "best.txt",
        "photo copy.txt",
    }


def test_partial_cross_volume_copy_and_disk_full_roll_back_without_partial_bundle(tmp_path, monkeypatch):
    copy_case = build_plan(tmp_path / "copy")
    calls = 0

    def fail_copy(phase, _record):
        nonlocal calls
        if phase == "copy_chunk":
            calls += 1
            raise OSError(28, "injected disk full")

    copy_executor = DatasetBundleExecutor(
        state_root=copy_case["state"],
        service=copy_case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
        fault_hook=fail_copy,
        copy_chunk_size=4,
    )
    copied = copy_executor.apply(copy_case["plan"], execute=True)
    assert calls == 1
    assert copied.state is ExecutionState.ROLLED_BACK
    assert len(source_payloads(copy_case)) == 4
    assert not any(path.exists() for path in move_destinations(copy_case["plan"]))

    space_case = build_plan(tmp_path / "space")
    real_usage = executor_module.shutil.disk_usage

    def no_space(path):
        usage = real_usage(path)
        return usage._replace(free=0)

    monkeypatch.setattr(executor_module.shutil, "disk_usage", no_space)
    space_executor = DatasetBundleExecutor(
        state_root=space_case["state"],
        service=space_case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
    )
    no_space_report = space_executor.apply(space_case["plan"], execute=True)
    assert no_space_report.state is ExecutionState.FAILED
    assert no_space_report.code is ExecutionCode.INSUFFICIENT_SPACE
    assert len(source_payloads(space_case)) == 4
    assert not space_case["state"].exists()


def test_cross_volume_rollback_preserves_a_replaced_destination(tmp_path):
    case = build_plan(tmp_path)
    external_destination = None

    def replace_then_fail(phase, record):
        nonlocal external_destination
        if phase == "after_cross_volume_publish" and external_destination is None:
            destination = Path(record.destination)
            destination.replace(destination.with_suffix(".transaction-backup"))
            destination.write_bytes(b"external destination")
            external_destination = destination
            raise OSError(5, "force rollback after external replacement")

    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
        fault_hook=replace_then_fail,
    ).apply(case["plan"], execute=True)
    assert report.state is ExecutionState.ROLLED_BACK
    assert external_destination is not None
    assert external_destination.read_bytes() == b"external destination"
    assert len(source_payloads(case)) == 4


def test_rollback_cleanup_rechecks_destination_and_temporary_tombstones(tmp_path):
    destination_case = build_plan(tmp_path / "destination")
    failed_after_publish = False
    external_destination = None

    def replace_destination_cleanup(phase, record):
        nonlocal failed_after_publish, external_destination
        if phase == "after_cross_volume_publish" and not failed_after_publish:
            failed_after_publish = True
            raise OSError(5, "force destination rollback")
        if (
            phase == "before_cleanup_tombstone"
            and failed_after_publish
            and record.destination is not None
            and not Path(record.temporary_path).exists()
            and external_destination is None
        ):
            destination = Path(record.destination)
            destination.replace(destination.with_suffix(".transaction-backup"))
            destination.write_bytes(b"external cleanup destination")
            external_destination = destination

    destination_report = DatasetBundleExecutor(
        state_root=destination_case["state"],
        service=destination_case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
        fault_hook=replace_destination_cleanup,
    ).apply(destination_case["plan"], execute=True)
    assert destination_report.state is ExecutionState.RECOVERY_REQUIRED
    assert external_destination is not None
    assert external_destination.read_bytes() == b"external cleanup destination"
    assert len(source_payloads(destination_case)) == 4

    temporary_case = build_plan(tmp_path / "temporary")
    copy_failed = False
    external_temporary = None

    def replace_partial_temporary(phase, record):
        nonlocal copy_failed, external_temporary
        if phase == "copy_chunk" and not copy_failed:
            copy_failed = True
            raise OSError(28, "force temporary rollback")
        if phase == "before_cleanup_tombstone" and copy_failed and external_temporary is None:
            temporary = Path(record.temporary_path)
            temporary.replace(temporary.with_suffix(".transaction-backup"))
            temporary.write_bytes(b"external cleanup temporary")
            external_temporary = temporary

    temporary_report = DatasetBundleExecutor(
        state_root=temporary_case["state"],
        service=temporary_case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
        fault_hook=replace_partial_temporary,
        copy_chunk_size=4,
    ).apply(temporary_case["plan"], execute=True)
    assert temporary_report.state is ExecutionState.RECOVERY_REQUIRED
    assert external_temporary is not None
    assert external_temporary.read_bytes() == b"external cleanup temporary"
    assert len(source_payloads(temporary_case)) == 4


def test_rollback_replays_an_interrupted_transaction_tombstone(tmp_path):
    case = build_plan(tmp_path)
    copy_failed = False
    cleanup_interrupted = False

    def interrupt_cleanup(phase, _record):
        nonlocal copy_failed, cleanup_interrupted
        if phase == "copy_chunk" and not copy_failed:
            copy_failed = True
            raise OSError(28, "force temporary rollback")
        if phase == "after_cleanup_tombstone" and not cleanup_interrupted:
            cleanup_interrupted = True
            raise OSError(5, "interrupt cleanup after atomic rename")

    first = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
        fault_hook=interrupt_cleanup,
        copy_chunk_size=4,
    ).apply(case["plan"], execute=True)
    assert first.state is ExecutionState.RECOVERY_REQUIRED
    assert cleanup_interrupted
    cleanup_tombstone = latest_tombstone(case, "cleanup_tombstone_prepared")
    assert cleanup_tombstone.exists()

    restored = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).restore(case["plan"].plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert restored.ok
    assert not cleanup_tombstone.exists()
    assert len(source_payloads(case)) == 4


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    (
        ("MAX_EXECUTION_DOCUMENT_BYTES", 64),
        ("MAX_EXECUTION_TRANSACTION_FILES", 1),
        ("MAX_JOURNAL_BYTES", 64),
        ("MAX_JOURNAL_LINE_BYTES", 32),
        ("MAX_JOURNAL_EVENTS", 1),
    ),
)
def test_oversized_executor_state_fails_before_mutation_and_is_byte_stable(
    tmp_path,
    monkeypatch,
    limit_name,
    limit_value,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).ok
    document_path = operation_document_path(case)
    journal_path = operation_journal_path(case)
    document_before = document_path.read_bytes()
    journal_before = journal_path.read_bytes()
    filesystem_before = {
        str(path): path.read_bytes()
        for path in (
            *move_destinations(case["plan"]),
            *(Path(record.quarantine_path) for record in quarantine_records(executor, case)),
        )
    }
    monkeypatch.setattr(executor_module, limit_name, limit_value)

    report = executor.finalize(case["plan"].plan_id, execute=True)
    assert report.state is ExecutionState.FAILED
    assert report.code in {ExecutionCode.DOCUMENT_CONFLICT, ExecutionCode.JOURNAL_CORRUPT}
    assert document_path.read_bytes() == document_before
    assert journal_path.read_bytes() == journal_before
    assert {path: Path(path).read_bytes() for path in filesystem_before} == filesystem_before


def test_truncated_journal_tail_is_readable_but_never_appended_or_mutated(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(state_root=case["state"], service=case["service"])
    assert executor.apply(case["plan"], execute=True).ok
    quarantines = tuple(Path(record.quarantine_path) for record in quarantine_records(executor, case))
    payloads_before = {str(path): path.read_bytes() for path in quarantines}
    journal_path = operation_journal_path(case)
    journal_path.write_bytes(journal_path.read_bytes() + b'{"truncated"')
    journal_before = journal_path.read_bytes()

    report = executor.finalize(case["plan"].plan_id, execute=True)
    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.JOURNAL_CORRUPT
    assert journal_path.read_bytes() == journal_before
    assert {path: Path(path).read_bytes() for path in payloads_before} == payloads_before


@pytest.mark.parametrize("state_file", ("document", "journal"))
@pytest.mark.parametrize(
    "malformed_json",
    (
        b'{"schema":"x","schema":"x"}\n',
        b'{"value":NaN}\n',
        b'{"value":1e9999}\n',
        ('{"value":' + str(1 << 300) + "}\n").encode("ascii"),
    ),
    ids=("duplicate-key", "nan", "overflowing-float", "huge-integer"),
)
def test_ambiguous_persisted_json_blocks_finalize_without_mutation(
    tmp_path,
    state_file,
    malformed_json,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    assert executor.apply(case["plan"], execute=True).ok
    records = quarantine_records(executor, case)
    filesystem_before = {
        str(path): path.read_bytes()
        for path in (
            *move_destinations(case["plan"]),
            *(Path(record.quarantine_path) for record in records),
        )
    }
    document_path = operation_document_path(case)
    journal_path = operation_journal_path(case)
    if state_file == "document":
        document_path.write_bytes(malformed_json)
    else:
        journal_path.write_bytes(journal_path.read_bytes() + malformed_json)
    document_before = document_path.read_bytes()
    journal_before = journal_path.read_bytes()

    report = executor.finalize(case["plan"].plan_id, execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is (
        ExecutionCode.DOCUMENT_CONFLICT if state_file == "document" else ExecutionCode.JOURNAL_CORRUPT
    )
    assert document_path.read_bytes() == document_before
    assert journal_path.read_bytes() == journal_before
    assert {path: Path(path).read_bytes() for path in filesystem_before} == filesystem_before


@pytest.mark.parametrize(
    ("remaining_events", "expected_state"),
    ((-1, ExecutionState.FAILED), (0, ExecutionState.APPLIED)),
    ids=("one-event-short", "exactly-sufficient"),
)
def test_initial_journal_lifecycle_budget_is_proved_before_mutation(
    tmp_path,
    monkeypatch,
    remaining_events,
    expected_state,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    records = executor._preflight(case["plan"], state_path(case))
    document = executor._build_document(
        case["plan"],
        state_path(case),
        records,
    )
    required = executor._journal_phase_budgets(document)["initial"].events
    monkeypatch.setattr(
        executor_module,
        "MAX_JOURNAL_EVENTS",
        required + remaining_events,
    )
    before = source_payloads(case)

    report = executor.apply(case["plan"], execute=True)

    assert report.state is expected_state
    if remaining_events < 0:
        assert report.code is ExecutionCode.PLAN_INVALID
        assert source_payloads(case) == before
        assert list(case["destination"].iterdir()) == []
        assert not case["state"].exists()
    else:
        assert report.ok


@pytest.mark.parametrize(
    "limit_name",
    (
        "MAX_EXECUTION_DOCUMENT_BYTES",
        "MAX_JOURNAL_EVENTS",
        "MAX_JOURNAL_BYTES",
    ),
)
def test_apply_preview_and_execute_reject_the_same_resource_limit_without_state(
    tmp_path,
    monkeypatch,
    limit_name,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    records = executor._preflight(case["plan"], state_path(case))
    document = executor._build_document(
        case["plan"],
        state_path(case),
        records,
    )
    if limit_name == "MAX_EXECUTION_DOCUMENT_BYTES":
        required = len(executor._execution_document_payload(document))
    else:
        initial_budget = executor._journal_phase_budgets(document)["initial"]
        required = initial_budget.events if limit_name == "MAX_JOURNAL_EVENTS" else initial_budget.bytes
    monkeypatch.setattr(
        executor_module,
        limit_name,
        required - 1,
    )
    before = source_payloads(case)

    preview = executor.apply(case["plan"])
    execute = executor.apply(case["plan"], execute=True)

    for report in (preview, execute):
        assert report.state is ExecutionState.FAILED
        assert report.code is ExecutionCode.PLAN_INVALID
        assert not report.changed
    assert source_payloads(case) == before
    assert list(case["destination"].iterdir()) == []
    assert not case["state"].exists()


def test_apply_preview_validates_every_projected_journal_branch(
    tmp_path,
    monkeypatch,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    original_budgets = executor._journal_phase_budgets

    def over_limit_finalize(document):
        budgets = dict(original_budgets(document))
        budgets["finalize"] = executor_module._JournalBudget(
            executor_module.MAX_JOURNAL_EVENTS + 1,
            1,
        )
        # Keep the aggregate branch intentionally below the cap so this test
        # proves that validation inspects each named lifecycle branch.
        budgets["initial"] = executor_module._JournalBudget(1, 1)
        return budgets

    monkeypatch.setattr(
        executor,
        "_journal_phase_budgets",
        over_limit_finalize,
    )

    report = executor.apply(case["plan"])

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.PLAN_INVALID
    assert "'finalize'" in report.message
    assert not case["state"].exists()
    assert len(source_payloads(case)) == 4


def test_ready_preview_builds_and_checks_document_without_persisting_state(
    tmp_path,
    monkeypatch,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    original_build = executor._build_document
    builds = 0

    def counted_build(plan, state_root, records):
        nonlocal builds
        builds += 1
        return original_build(plan, state_root, records)

    monkeypatch.setattr(
        executor,
        "_build_document",
        counted_build,
    )

    report = executor.apply(case["plan"])

    assert report.state is ExecutionState.READY
    assert report.code is ExecutionCode.EXECUTION_NOT_EXPLICIT
    assert builds == 1
    assert not case["state"].exists()


def test_journal_event_budget_formulas_cover_every_recovery_branch(tmp_path):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
    )
    records = executor._preflight(case["plan"], state_path(case))
    document = executor._build_document(
        case["plan"],
        state_path(case),
        records,
    )
    budgets = executor._journal_phase_budgets(document)
    quarantine = sum(record.strategy is executor_module._Strategy.QUARANTINE for record in records)
    same_volume = sum(record.strategy is executor_module._Strategy.SAME_VOLUME_RENAME for record in records)
    cross_volume = sum(record.strategy is executor_module._Strategy.CROSS_VOLUME_COPY for record in records)
    apply_events = quarantine + same_volume + 4 * cross_volume + 3
    rollback_events = 2 * quarantine + same_volume + 8 * cross_volume + 2
    finalize_events = 3 * quarantine + 3 * cross_volume + 2
    recovery_events = max(rollback_events, finalize_events)

    assert budgets["apply"].events == apply_events
    assert budgets["rollback"].events == rollback_events
    assert budgets["restore"].events == rollback_events
    assert budgets["finalize"].events == finalize_events
    assert budgets["retry"].events == apply_events + recovery_events
    assert budgets["initial"].events == 2 * apply_events + rollback_events + recovery_events


def test_execution_transaction_cap_rejects_before_path_revalidation(
    tmp_path,
    monkeypatch,
):
    case = build_plan(tmp_path)
    file_count = sum(len(action.files) for action in case["plan"].actions)
    monkeypatch.setattr(
        executor_module,
        "MAX_EXECUTION_TRANSACTION_FILES",
        file_count - 1,
    )

    def forbidden_revalidation(_plan):
        raise AssertionError("over-limit apply must not inspect plan paths")

    monkeypatch.setattr(
        case["service"],
        "revalidate",
        forbidden_revalidation,
    )

    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).apply(case["plan"], execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.PLAN_INVALID
    assert "Split the plan" in report.message
    assert not case["state"].exists()
    assert len(source_payloads(case)) == file_count


def test_execution_document_hash_is_cached_and_compared_constant_time(
    tmp_path,
    monkeypatch,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    records = executor._preflight(case["plan"], state_path(case))
    document = executor._build_document(
        case["plan"],
        state_path(case),
        records,
    )
    expected = document.document_hash

    def forbidden_rehash(_value):
        raise AssertionError("cached document_hash must not be recomputed")

    monkeypatch.setattr(executor_module, "_content_hash", forbidden_rehash)
    assert document.document_hash == expected
    assert document.document_hash == expected

    monkeypatch.undo()
    compare_calls = []
    real_compare = executor_module.hmac.compare_digest

    def recording_compare(first, second):
        compare_calls.append((first, second))
        return real_compare(first, second)

    monkeypatch.setattr(
        executor_module.hmac,
        "compare_digest",
        recording_compare,
    )
    loaded = executor_module._ExecutionDocument.from_dict(document.to_dict())

    assert loaded.document_hash == expected
    assert compare_calls == [(expected, expected)]


def test_execution_document_cleanup_preserves_replaced_temp_name(
    tmp_path,
):
    case = build_plan(tmp_path)
    operation_directory(case).mkdir(parents=True)
    adapter_base = type(executor_module.platform_file_system())
    external = b"external execution temp replacement"

    class ReplaceTempAfterPublicationFileSystem(adapter_base):
        replacement = None

        def rename_no_replace(self, source, destination):
            commit = super().rename_no_replace(source, destination)
            Path(source).write_bytes(external)
            self.replacement = Path(source)
            return commit

        def fsync_directory(self, directory):
            if self.replacement is not None and Path(directory) == operation_directory(case):
                raise OSError(5, "injected document durability failure")
            return super().fsync_directory(directory)

    adapter = ReplaceTempAfterPublicationFileSystem()
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        fs=adapter,
    )
    records = executor._preflight(case["plan"], state_path(case))
    document = executor._build_document(
        case["plan"],
        state_path(case),
        records,
    )

    with pytest.raises(
        executor_module.DatasetExecutionError,
        match="durability failure",
    ):
        executor._persist_document(document, state_path(case))

    assert operation_document_path(case).is_file()
    assert adapter.replacement is not None
    assert adapter.replacement.read_bytes() == external


def test_unjournaled_cross_volume_temporary_is_cleaned_by_creation_identity(
    tmp_path,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
        volume_checker=lambda _proof, _path, _stat: False,
    )
    records = executor._preflight(case["plan"], state_path(case))
    record = next(item for item in records if item.strategy is executor_module._Strategy.CROSS_VOLUME_COPY)
    Path(record.destination).parent.mkdir(parents=True, exist_ok=True)

    def fail_before_journal(_created):
        raise OSError(5, "journal binding failed")

    with pytest.raises(OSError, match="journal binding failed"):
        executor._stage_cross_volume_destination(
            record,
            after_temporary_created=fail_before_journal,
        )

    assert not Path(record.temporary_path).exists()
    assert Path(record.source.path).is_file()
    assert not Path(record.destination).exists()


def test_ten_thousand_record_identity_replay_uses_one_event_index(
    tmp_path,
):
    case = build_plan(tmp_path)
    executor = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    )
    base_records = executor._preflight(case["plan"], state_path(case))
    records = tuple(
        replace(base_records[0], ordinal=ordinal) for ordinal in range(executor_module.MAX_EXECUTION_TRANSACTION_FILES)
    )
    document = executor_module._ExecutionDocument(
        plan_id=case["plan"].plan_id,
        plan_hash=executor_module._content_hash(case["plan"].to_dict()),
        created_ns=1,
        state_root=str(state_path(case)),
        destination_root=str(case["destination"]),
        plan=case["plan"].to_dict(),
        files=records,
    )
    events = tuple(
        executor_module._JournalRecord(
            event_id=str(ordinal),
            timestamp_ns=ordinal,
            plan_id=document.plan_id,
            document_hash=document.document_hash,
            event=executor_module._JournalEvent.TEMPORARY_CREATED,
            details={
                "ordinal": ordinal,
                "stat_device": 1,
                "stat_inode": ordinal + 1,
            },
            previous_hash="",
            record_hash="",
        )
        for ordinal in range(len(records))
    )
    event_index = executor_module._JournalEventIndex.build(
        document,
        events,
    )

    class NoRescanJournal:
        @staticmethod
        def events_for(_document):
            raise AssertionError("indexed replay must not rescan the journal")

    identities = tuple(
        executor._created_identity(
            document,
            NoRescanJournal(),
            record,
            executor_module._JournalEvent.TEMPORARY_CREATED,
            event_index=event_index,
        )
        for record in records
    )

    assert identities[0] == (1, 1)
    assert identities[-1] == (
        1,
        executor_module.MAX_EXECUTION_TRANSACTION_FILES,
    )


def test_per_plan_journal_corruption_cannot_block_another_operation(
    tmp_path,
):
    shared_state = tmp_path / "shared-state"
    first = build_plan(tmp_path / "first")
    second = build_plan(tmp_path / "second")
    first_executor = DatasetBundleExecutor(
        state_root=shared_state,
        service=first["service"],
    )
    second_executor = DatasetBundleExecutor(
        state_root=shared_state,
        service=second["service"],
    )
    assert first_executor.apply(first["plan"], execute=True).ok
    assert second_executor.apply(second["plan"], execute=True).ok
    first_journal = (
        shared_state / executor_module.STATE_DIRECTORY_NAME / "operations" / first["plan"].plan_id / "journal.jsonl"
    )
    first_journal.write_bytes(first_journal.read_bytes() + b'{"value":NaN}\n')
    first_files_before = {str(path): path.read_bytes() for path in move_destinations(first["plan"]) if path.exists()}

    summaries = {summary.plan_id: summary for summary in first_executor.list_operations()}
    restored = second_executor.restore(
        second["plan"].plan_id,
        execute=True,
    )

    assert summaries[first["plan"].plan_id].state is ExecutionState.RECOVERY_REQUIRED
    assert summaries[second["plan"].plan_id].state is ExecutionState.APPLIED
    assert restored.state is ExecutionState.RESTORED
    assert restored.ok
    assert {path: Path(path).read_bytes() for path in first_files_before} == first_files_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner and mode bits are not Windows ACLs")
def test_insecure_existing_private_directories_fail_before_mutation(tmp_path):
    state_case = build_plan(tmp_path / "state")
    state_path(state_case).mkdir(parents=True)
    os.chmod(state_path(state_case), 0o777)
    state_before = state_path(state_case).stat()
    state_report = DatasetBundleExecutor(
        state_root=state_case["state"],
        service=state_case["service"],
    ).apply(state_case["plan"], execute=True)
    assert state_report.state is ExecutionState.FAILED
    assert state_report.code is ExecutionCode.UNSAFE_PATH
    assert source_payloads(state_case).keys() == {
        "best.jpg",
        "photo copy.jpg",
        "best.txt",
        "photo copy.txt",
    }
    assert not move_destinations(state_case["plan"])[0].exists()
    assert state_path(state_case).stat().st_ino == state_before.st_ino

    quarantine_case = build_plan(tmp_path / "quarantine")
    quarantine_root = quarantine_case["source"] / executor_module.QUARANTINE_DIRECTORY_NAME
    quarantine_root.mkdir()
    os.chmod(quarantine_root, 0o777)
    quarantine_report = DatasetBundleExecutor(
        state_root=quarantine_case["state"],
        service=quarantine_case["service"],
    ).apply(quarantine_case["plan"], execute=True)
    assert quarantine_report.state is ExecutionState.FAILED
    assert quarantine_report.code is ExecutionCode.UNSAFE_PATH
    assert len(source_payloads(quarantine_case)) == 4
    assert not quarantine_case["state"].exists()


def test_source_mtime_or_symlink_change_fails_closed_before_execution(tmp_path):
    changed_case = build_plan(tmp_path / "changed")
    changed_case["duplicate"].write_bytes(b"changed after planning")
    changed_executor = DatasetBundleExecutor(
        state_root=changed_case["state"],
        service=changed_case["service"],
    )
    changed = changed_executor.apply(changed_case["plan"], execute=True)
    assert changed.state is ExecutionState.FAILED
    assert changed.code is ExecutionCode.PLAN_INVALID
    assert changed_case["best"].exists()
    assert not changed_case["state"].exists()

    link_case = build_plan(tmp_path / "link")
    original = link_case["duplicate"]
    replacement = link_case["source"] / "replacement.jpg"
    replacement.write_bytes(b"identical image payload")
    original.unlink()
    try:
        original.symlink_to(replacement)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows configuration")
    link_executor = DatasetBundleExecutor(state_root=link_case["state"], service=link_case["service"])
    linked = link_executor.apply(link_case["plan"], execute=True)
    assert linked.state is ExecutionState.FAILED
    assert linked.code is ExecutionCode.PLAN_INVALID
    assert link_case["best"].exists()
    assert not link_case["state"].exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows ChangeTime generation token")
def test_same_content_rewrite_with_restored_mtime_fails_closed_before_execution(tmp_path):
    case = build_plan(tmp_path)
    source = case["duplicate"]
    before = source.stat()
    original = source.read_bytes()
    source.write_bytes(bytes(byte ^ 0xFF for byte in original))
    source.write_bytes(original)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    report = DatasetBundleExecutor(
        state_root=case["state"],
        service=case["service"],
    ).apply(case["plan"], execute=True)

    assert report.state is ExecutionState.FAILED
    assert report.code is ExecutionCode.PLAN_INVALID
    assert source.read_bytes() == original
    assert not case["state"].exists()
