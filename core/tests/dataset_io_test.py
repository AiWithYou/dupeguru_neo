# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import copy
import io
import json
from pathlib import Path

import pytest

import core.dataset_io as dataset_io_module
from core.dataset_executor import ExecutionCode, ExecutionState
from core.dataset_io import (
    EXECUTION_REPORT_SCHEMA,
    OPERATION_LIST_SCHEMA,
    PLAN_VALIDATION_SCHEMA,
    PREPARATION_RESULT_SCHEMA,
    PREPARE_INPUT_SCHEMA,
    DatasetIOError,
    DatasetWorkflowFacade,
    dataset_execution_report_to_dict,
    dataset_operation_list_to_dict,
    dataset_plan_from_dict,
    dataset_plan_from_json,
    dataset_preparation_to_dict,
    dataset_prepare_input_from_dict,
    dataset_prepare_input_from_json,
    plan_validation_to_dict,
    read_json_file,
    read_json_stream,
    strict_json_loads,
    write_json_file,
)
from core.dataset_service import PreparationState
from core.safe_action import platform_file_system


def build_manifest(tmp_path, *, dry_run=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    destination = tmp_path / "organized"
    state = tmp_path / "state"
    source.mkdir()
    destination.mkdir()
    best = source / "best.jpg"
    duplicate = source / "duplicate.jpg"
    best_caption = source / "best.txt"
    duplicate_caption = source / "duplicate.txt"
    best.write_bytes(b"same image")
    duplicate.write_bytes(b"same image")
    best_caption.write_text("same caption", encoding="utf-8")
    duplicate_caption.write_text("same caption", encoding="utf-8")
    manifest = {
        "schema": PREPARE_INPUT_SCHEMA,
        "schema_version": 1,
        "allowed_roots": [str(source)],
        "destination_root": str(destination),
        "assets": [
            {
                "asset_id": "best",
                "path": str(best),
                "dimensions": [4000, 3000],
                "bit_depth": 16,
                "metadata_count": 10,
                "jpeg_artifact_score": 0,
                "protected": True,
            },
            {
                "asset_id": "duplicate",
                "path": str(duplicate),
                "dimensions": [1000, 750],
                "bit_depth": 8,
                "metadata_count": 1,
                "jpeg_artifact_score": 2.5,
                "protected": False,
            },
        ],
        "clusters": [
            {
                "members": ["best", "duplicate"],
                "relation": "verified_exact",
                "evidence_complete": True,
                "evidence_version": "dataset-evidence-v1",
            }
        ],
        "sidecar_paths": [str(best_caption), str(duplicate_caption)],
        "split_weights": {"train": 0.8, "validation": 0.2},
        "split_seed": "strict-io-test",
        "dry_run": dry_run,
    }
    return {
        "manifest": manifest,
        "source": source,
        "destination": destination,
        "state": state,
        "files": (best, duplicate, best_caption, duplicate_caption),
    }


def prepare_case(case):
    request = dataset_prepare_input_from_dict(case["manifest"])
    facade = DatasetWorkflowFacade(state_root=case["state"])
    preparation = facade.prepare(request)
    assert preparation.state is PreparationState.COMPLETE
    assert preparation.plan is not None
    return facade, preparation


def test_prepare_manifest_round_trip_and_facade_prepare(tmp_path):
    case = build_manifest(tmp_path)
    parsed = dataset_prepare_input_from_dict(case["manifest"])
    assert parsed.to_dict() == case["manifest"]
    from_json = dataset_prepare_input_from_json(json.dumps(case["manifest"], ensure_ascii=False).encode("utf-8"))
    assert from_json == parsed

    facade = DatasetWorkflowFacade(state_root=case["state"])
    preparation = facade.prepare_json(json.dumps(case["manifest"], ensure_ascii=False))
    assert preparation.state is PreparationState.COMPLETE
    assert preparation.plan is not None
    assert not case["state"].exists()
    assert list(case["destination"].iterdir()) == []


@pytest.mark.parametrize(
    "payload,code",
    [
        ('{"schema":"x","schema":"y"}', "duplicate_key"),
        ('{"value":NaN}', "non_finite_number"),
        ('{"value":Infinity}', "non_finite_number"),
        ('{"value":-Infinity}', "non_finite_number"),
        ('{"value":1e999}', "non_finite_number"),
        ("\ufeff{}", "bom_forbidden"),
        (b"\xef\xbb\xbf{}", "bom_forbidden"),
    ],
)
def test_strict_json_rejects_duplicate_keys_bom_and_non_finite_numbers(payload, code):
    with pytest.raises(DatasetIOError) as caught:
        strict_json_loads(payload)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "payload",
    [
        "[" * 65 + "0" + "]" * 65,
        '{"value":' + "9" * 1_025 + "}",
    ],
)
def test_strict_json_rejects_resource_amplifying_structure_before_decode(payload):
    with pytest.raises(DatasetIOError) as caught:
        strict_json_loads(payload)
    assert caught.value.code == "json_resource_limit"


def test_strict_json_converts_parser_memory_failure_to_typed_error(monkeypatch):
    def fail_parse(*_args, **_kwargs):
        raise MemoryError("simulated parser exhaustion")

    monkeypatch.setattr(dataset_io_module.json, "loads", fail_parse)
    with pytest.raises(DatasetIOError) as caught:
        strict_json_loads("{}")
    assert caught.value.code == "json_resource_limit"
    assert "memory budget" in str(caught.value)


def test_prepare_manifest_rejects_unknown_fields_types_and_duplicate_values(tmp_path):
    case = build_manifest(tmp_path)
    unknown = copy.deepcopy(case["manifest"])
    unknown["unexpected"] = True
    with pytest.raises(DatasetIOError, match="unknown fields"):
        dataset_prepare_input_from_dict(unknown)

    nested_unknown = copy.deepcopy(case["manifest"])
    nested_unknown["assets"][0]["unexpected"] = 1
    with pytest.raises(DatasetIOError, match="unknown fields"):
        dataset_prepare_input_from_dict(nested_unknown)

    wrong_type = copy.deepcopy(case["manifest"])
    wrong_type["dry_run"] = 0
    with pytest.raises(DatasetIOError, match="boolean"):
        dataset_prepare_input_from_dict(wrong_type)

    duplicate_member = copy.deepcopy(case["manifest"])
    duplicate_member["clusters"][0]["members"] = ["best", "best"]
    with pytest.raises(DatasetIOError) as caught:
        dataset_prepare_input_from_dict(duplicate_member)
    assert caught.value.code == "duplicate_value"

    non_finite = copy.deepcopy(case["manifest"])
    non_finite["split_weights"]["train"] = float("nan")
    with pytest.raises(DatasetIOError):
        dataset_prepare_input_from_dict(non_finite)


def test_prepare_and_plan_parsers_apply_record_limits_before_object_building(
    tmp_path,
    monkeypatch,
):
    case = build_manifest(tmp_path)
    monkeypatch.setattr(dataset_io_module, "MAX_DATASET_PLAN_ACTIONS", 1)
    with pytest.raises(DatasetIOError) as caught:
        dataset_prepare_input_from_dict(case["manifest"])
    assert caught.value.code == "resource_limit"

    monkeypatch.setattr(dataset_io_module, "MAX_DATASET_PLAN_ACTIONS", 250_000)
    preparation = DatasetWorkflowFacade().prepare(case["manifest"])
    assert preparation.plan is not None
    plan_document = preparation.plan.to_dict()

    monkeypatch.setattr(dataset_io_module, "MAX_DATASET_PLAN_ACTIONS", 1)
    with pytest.raises(DatasetIOError) as caught:
        dataset_plan_from_dict(plan_document)
    assert caught.value.code == "resource_limit"

    monkeypatch.setattr(dataset_io_module, "MAX_DATASET_PLAN_ACTIONS", 250_000)
    monkeypatch.setattr(dataset_io_module, "MAX_DATASET_PLAN_FILE_RECORDS", 1)
    with pytest.raises(DatasetIOError) as caught:
        dataset_plan_from_dict(plan_document)
    assert caught.value.code == "resource_limit"


def test_dataset_plan_strict_round_trip_revalidates_all_content_ids(tmp_path):
    case = build_manifest(tmp_path)
    _facade, preparation = prepare_case(case)
    plan = preparation.plan
    assert plan is not None
    restored = dataset_plan_from_dict(plan.to_dict())
    restored_json = dataset_plan_from_json(plan.to_json())
    assert restored == plan
    assert restored_json == plan
    assert restored.to_dict() == plan.to_dict()

    changed_plan_id = copy.deepcopy(plan.to_dict())
    changed_plan_id["plan_id"] = "0" * 64
    with pytest.raises(DatasetIOError, match="ID does not match"):
        dataset_plan_from_dict(changed_plan_id)

    changed_action = copy.deepcopy(plan.to_dict())
    changed_action["actions"][0]["asset_id"] = "forged"
    with pytest.raises(DatasetIOError, match="ID does not match"):
        dataset_plan_from_dict(changed_action)

    unknown_proof = copy.deepcopy(plan.to_dict())
    unknown_proof["actions"][0]["files"][0]["source"]["unknown"] = 1
    with pytest.raises(DatasetIOError, match="unknown fields"):
        dataset_plan_from_dict(unknown_proof)


def test_result_envelopes_are_schema_versioned_and_json_safe(tmp_path):
    case = build_manifest(tmp_path)
    facade, preparation = prepare_case(case)
    plan = preparation.plan
    assert plan is not None
    validation = facade.validate(plan)
    preview = facade.apply(plan)

    preparation_value = dataset_preparation_to_dict(preparation)
    validation_value = plan_validation_to_dict(validation)
    report_value = dataset_execution_report_to_dict(preview)
    assert preparation_value["schema"] == PREPARATION_RESULT_SCHEMA
    assert validation_value["schema"] == PLAN_VALIDATION_SCHEMA
    assert report_value["schema"] == EXECUTION_REPORT_SCHEMA
    assert preparation_value["schema_version"] == 1
    assert validation_value["schema_version"] == 1
    assert report_value["schema_version"] == 1
    assert report_value["code"] == "execution_not_explicit"
    json.dumps(preparation_value, allow_nan=False)
    json.dumps(validation_value, allow_nan=False)
    json.dumps(report_value, allow_nan=False)

    applied = facade.apply(plan, execute=True)
    assert applied.state is ExecutionState.APPLIED
    operations_value = dataset_operation_list_to_dict(facade.list_operations())
    assert operations_value["schema"] == OPERATION_LIST_SCHEMA
    assert operations_value["schema_version"] == 1
    assert len(operations_value["operations"]) == 1
    output = tmp_path / "validation.json"
    output_preview = facade.write_result(validation, output)
    assert not output_preview.written
    assert not output.exists()
    output_receipt = facade.write_result(validation, output, execute=True)
    assert output_receipt.written
    assert read_json_file(output)["schema"] == PLAN_VALIDATION_SCHEMA


def test_atomic_json_file_io_is_no_replace_bom_free_and_size_bounded(tmp_path):
    destination = tmp_path / "result.json"
    value = {"schema": "test", "message": "日本語", "number": 1}
    preview = write_json_file(value, destination)
    assert preview.dry_run
    assert not preview.written
    assert not destination.exists()

    written = write_json_file(value, destination, execute=True)
    assert written.written
    assert not destination.read_bytes().startswith(b"\xef\xbb\xbf")
    assert read_json_file(destination) == value
    original = destination.read_bytes()
    with pytest.raises(DatasetIOError) as caught:
        write_json_file({"changed": True}, destination, execute=True)
    assert caught.value.code == "destination_conflict"
    assert destination.read_bytes() == original

    with pytest.raises(DatasetIOError) as caught:
        read_json_file(destination, maximum_bytes=4)
    assert caught.value.code == "json_too_large"
    with pytest.raises(DatasetIOError) as caught:
        write_json_file(value, tmp_path / "too-large.json", execute=True, maximum_bytes=4)
    assert caught.value.code == "json_too_large"
    assert not (tmp_path / "too-large.json").exists()


def test_json_publication_cleanup_preserves_replaced_temp_name(tmp_path):
    destination = tmp_path / "published.json"
    adapter_base = type(platform_file_system())
    external = b"external temp replacement"

    class ReplaceTempAfterPublicationFileSystem(adapter_base):
        replacement = None

        def rename_no_replace(self, source, target):
            commit = super().rename_no_replace(source, target)
            Path(source).write_bytes(external)
            self.replacement = Path(source)
            return commit

        def fsync_directory(self, directory):
            if self.replacement is not None and Path(directory) == tmp_path:
                raise OSError(5, "injected JSON durability failure")
            return super().fsync_directory(directory)

    adapter = ReplaceTempAfterPublicationFileSystem()

    with pytest.raises(DatasetIOError, match="durability failure"):
        write_json_file(
            {"proof": "published"},
            destination,
            execute=True,
            fs=adapter,
        )

    assert destination.is_file()
    assert adapter.replacement is not None
    assert adapter.replacement.read_bytes() == external


def test_json_stream_reader_is_seek_free_chunked_and_size_bounded():
    class ChunkedTextStream(io.StringIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size=-1):
            assert size > 0, "JSON stream reads must always be explicitly bounded"
            self.read_sizes.append(size)
            return super().read(size)

        def seek(self, *_args, **_kwargs):
            raise AssertionError("JSON stream reads must be seek-free")

    payload = '{"message":"日本語","value":1}'
    stream = ChunkedTextStream(payload)

    assert read_json_stream(stream) == {"message": "日本語", "value": 1}
    assert stream.read_sizes
    assert all(size > 0 for size in stream.read_sizes)

    with pytest.raises(DatasetIOError) as caught:
        read_json_stream(
            ChunkedTextStream(payload),
            maximum_bytes=len(payload.encode("utf-8")) - 1,
        )
    assert caught.value.code == "json_too_large"


def test_json_file_reader_rejects_duplicate_keys_bom_and_links(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"x":1,"x":2}', encoding="utf-8")
    with pytest.raises(DatasetIOError) as caught:
        read_json_file(duplicate)
    assert caught.value.code == "duplicate_key"

    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(DatasetIOError) as caught:
        read_json_file(bom)
    assert caught.value.code == "bom_forbidden"

    link = tmp_path / "link.json"
    try:
        link.symlink_to(duplicate)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows configuration")
    with pytest.raises(DatasetIOError) as caught:
        read_json_file(link)
    assert caught.value.code == "unsafe_path"


def test_facade_enforces_dry_run_and_explicit_execute_for_all_mutations(tmp_path):
    ready_case = build_manifest(tmp_path / "ready", dry_run=False)
    ready_facade, ready_preparation = prepare_case(ready_case)
    ready_plan = ready_preparation.plan
    assert ready_plan is not None
    preview = ready_facade.apply(ready_plan)
    assert preview.state is ExecutionState.READY
    assert preview.code is ExecutionCode.EXECUTION_NOT_EXPLICIT
    assert not ready_case["state"].exists()
    assert all(path.exists() for path in ready_case["files"])

    applied = ready_facade.apply(ready_plan, execute=True)
    assert applied.state is ExecutionState.APPLIED
    restore_preview = ready_facade.restore(ready_plan.plan_id)
    assert restore_preview.state is ExecutionState.READY
    restored = ready_facade.restore(ready_plan.plan_id, execute=True)
    assert restored.state is ExecutionState.RESTORED
    assert all(path.exists() for path in ready_case["files"])

    dry_case = build_manifest(tmp_path / "dry", dry_run=True)
    dry_facade, dry_preparation = prepare_case(dry_case)
    dry_plan = dry_preparation.plan
    assert dry_plan is not None
    dry_report = dry_facade.apply(dry_plan, execute=True)
    assert dry_report.state is ExecutionState.DRY_RUN
    assert not dry_case["state"].exists()
    assert all(path.exists() for path in dry_case["files"])


def test_facade_finalize_is_a_separate_explicit_operation(tmp_path):
    case = build_manifest(tmp_path)
    facade, preparation = prepare_case(case)
    plan = preparation.plan
    assert plan is not None
    assert facade.apply(plan, execute=True).state is ExecutionState.APPLIED
    preview = facade.finalize(plan.plan_id)
    assert preview.state is ExecutionState.READY
    assert preview.code is ExecutionCode.EXECUTION_NOT_EXPLICIT
    finalized = facade.finalize(plan.plan_id, execute=True)
    assert finalized.state is ExecutionState.FINALIZED
    summaries = facade.list()
    assert len(summaries) == 1
    assert summaries[0].state is ExecutionState.FINALIZED
