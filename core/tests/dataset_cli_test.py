# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import argparse
import copy
import io
import json
import shutil

import pytest
from PIL import Image

import core.dataset_cli as dataset_cli
from core.dataset_cli import (
    DATASET_SCHEMAS,
    DatasetExitCode,
    add_dataset_parser,
    run_dataset_command,
)
from core.dataset_io import (
    PREPARE_INPUT_SCHEMA,
    DatasetWorkflowFacade,
    write_json_file,
)
from core.dataset_service import PreparationState


def build_parser():
    parser = argparse.ArgumentParser(prog="dupeguru")
    commands = parser.add_subparsers(dest="command", required=True)
    add_dataset_parser(commands)
    return parser


def run_command(parser, argv, *, stdin_text="", stdin_stream=None):
    args = parser.parse_args(argv)
    stdin = stdin_stream if stdin_stream is not None else io.StringIO(stdin_text)
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_dataset_command(args, stdin, stdout, stderr)
    return code, stdout.getvalue(), stderr.getvalue()


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
                "jpeg_artifact_score": 1,
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
        "split_weights": {"train": 1},
        "split_seed": "dataset-cli-test",
        "dry_run": dry_run,
    }
    manifest_path = tmp_path / "manifest.json"
    write_json_file(manifest, manifest_path, execute=True)
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "source": source,
        "destination": destination,
        "state": state,
        "files": (best, duplicate, best_caption, duplicate_caption),
    }


def build_plan(case):
    facade = DatasetWorkflowFacade(state_root=case["state"])
    preparation = facade.prepare(case["manifest"])
    assert preparation.state is PreparationState.COMPLETE
    assert preparation.plan is not None
    plan_path = case["manifest_path"].with_name("plan.json")
    write_json_file(preparation.plan.to_dict(), plan_path, execute=True)
    return preparation.plan, plan_path


def parsed_stdout(stdout):
    assert stdout.endswith("\n")
    lines = stdout.splitlines()
    assert len(lines) == 1
    value = json.loads(lines[0])
    assert lines[0] == json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return value


def test_parser_registers_all_dataset_subcommands():
    parser = build_parser()
    assert parser.parse_args(["dataset", "prepare", "-"]).dataset_command == "prepare"
    assert (
        parser.parse_args(
            [
                "dataset",
                "prepare-root",
                "library",
                "--destination-root",
                "organized",
            ]
        ).dataset_command
        == "prepare-root"
    )
    assert parser.parse_args(["dataset", "validate", "-"]).dataset_command == "validate"
    assert parser.parse_args(["dataset", "apply", "-"]).dataset_command == "apply"
    assert parser.parse_args(["dataset", "list", "--state-root", "state"]).dataset_command == "list"
    assert parser.parse_args(["dataset", "restore", "a" * 64, "--state-root", "state"]).dataset_command == "restore"
    assert parser.parse_args(["dataset", "finalize", "a" * 64, "--state-root", "state"]).dataset_command == "finalize"


def test_prepare_supports_file_and_stdin_with_canonical_json(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "prepare", str(case["manifest_path"]), "--state-root", str(case["state"])],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    result = parsed_stdout(stdout)
    assert result["schema"] == "dupeguru.dataset-preparation-result"
    assert result["complete"]
    assert result["plan"]["plan_id"]
    assert not case["state"].exists()

    stdin_case = build_manifest(tmp_path / "stdin")
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "prepare", "-"],
        stdin_text=json.dumps(stdin_case["manifest"]),
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["complete"]


def test_dataset_stdin_uses_only_seek_free_bounded_reads(tmp_path):
    class BoundedReadOnlyStream(io.StringIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size=-1):
            assert size > 0, "dataset stdin must never use an unbounded read"
            self.read_sizes.append(size)
            return super().read(size)

        def seek(self, *_args, **_kwargs):
            raise AssertionError("dataset stdin must be seek-free")

    parser = build_parser()
    case = build_manifest(tmp_path)
    stream = BoundedReadOnlyStream(json.dumps(case["manifest"]))

    code, stdout, stderr = run_command(
        parser,
        ["dataset", "prepare", "-"],
        stdin_stream=stream,
    )

    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["complete"]
    assert stream.read_sizes
    assert all(size > 0 for size in stream.read_sizes)


def test_dataset_stdout_limit_fails_before_emitting_a_partial_document(
    monkeypatch,
):
    monkeypatch.setattr(dataset_cli, "DEFAULT_JSON_SIZE_LIMIT", 32)
    stdout = io.StringIO()

    with pytest.raises(dataset_cli.DatasetIOError) as caught:
        dataset_cli._write_stdout(stdout, {"payload": "x" * 128})

    assert caught.value.code == "json_too_large"
    assert stdout.getvalue() == ""


def test_oversized_dataset_manifest_is_rejected_before_plan_write(
    tmp_path,
    monkeypatch,
):
    parser = build_parser()
    case = build_manifest(tmp_path)
    payload_size = len(case["manifest_path"].read_bytes())
    monkeypatch.setattr(
        dataset_cli,
        "DEFAULT_JSON_SIZE_LIMIT",
        payload_size - 1,
    )
    plan_out = tmp_path / "must-not-exist.json"

    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare",
            str(case["manifest_path"]),
            "--plan-out",
            str(plan_out),
            "--write",
        ],
    )

    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "json_too_large" in stderr
    assert not plan_out.exists()
    assert not case["state"].exists()
    assert all(path.exists() for path in case["files"])


def test_oversized_dataset_plan_is_rejected_before_execute_for_file_and_stdin(
    tmp_path,
    monkeypatch,
):
    parser = build_parser()
    for source_kind in ("file", "stdin"):
        case = build_manifest(tmp_path / source_kind)
        _plan, plan_path = build_plan(case)
        payload = plan_path.read_text(encoding="utf-8")
        monkeypatch.setattr(
            dataset_cli,
            "DEFAULT_JSON_SIZE_LIMIT",
            len(payload.encode("utf-8")) - 1,
        )
        argument = str(plan_path) if source_kind == "file" else "-"

        code, stdout, stderr = run_command(
            parser,
            [
                "dataset",
                "apply",
                argument,
                "--state-root",
                str(case["state"]),
                "--execute",
            ],
            stdin_text=payload if source_kind == "stdin" else "",
        )

        assert code == DatasetExitCode.INPUT_ERROR
        assert stdout == ""
        assert "json_too_large" in stderr
        assert not case["state"].exists()
        assert all(path.exists() for path in case["files"])


def test_prepare_plan_out_is_dry_run_until_write_and_never_overwrites(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    plan_out = tmp_path / "output-plan.json"
    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare",
            str(case["manifest_path"]),
            "--plan-out",
            str(plan_out),
        ],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["complete"]
    assert not plan_out.exists()

    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare",
            str(case["manifest_path"]),
            "--plan-out",
            str(plan_out),
            "--write",
        ],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["plan"]["plan_id"] == json.loads(plan_out.read_text())["plan_id"]
    original = plan_out.read_bytes()

    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare",
            str(case["manifest_path"]),
            "--plan-out",
            str(plan_out),
            "--write",
        ],
    )
    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "destination_conflict" in stderr
    assert plan_out.read_bytes() == original


def test_prepare_write_requires_plan_out_and_incomplete_is_partial(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "prepare", str(case["manifest_path"]), "--write"],
    )
    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "invalid_arguments" in stderr

    incomplete = build_manifest(tmp_path / "incomplete")
    incomplete["manifest"]["clusters"][0]["evidence_complete"] = False
    incomplete["manifest_path"].unlink()
    write_json_file(incomplete["manifest"], incomplete["manifest_path"], execute=True)
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "prepare", str(incomplete["manifest_path"])],
    )
    assert code == DatasetExitCode.PARTIAL
    assert stderr == ""
    result = parsed_stdout(stdout)
    assert not result["complete"]
    assert result["plan"] is None
    assert result["issues"]


def test_prepare_root_discovers_exact_images_sidecars_and_writes_only_when_authorized(
    tmp_path,
):
    parser = build_parser()
    source = tmp_path / "source"
    protected = source / "protected"
    incoming = source / "incoming"
    destination = tmp_path / "destination"
    protected.mkdir(parents=True)
    incoming.mkdir()
    destination.mkdir()
    first = protected / "image.png"
    second = incoming / "image-copy.png"
    first_caption = protected / "image.txt"
    second_caption = incoming / "image-copy.txt"
    Image.new("RGB", (32, 24), "green").save(first)
    shutil.copyfile(first, second)
    first_caption.write_text("caption", encoding="utf-8")
    second_caption.write_text("caption", encoding="utf-8")
    plan_out = tmp_path / "root-plan.json"
    cache = tmp_path / "visual.sqlite3"

    command = [
        "dataset",
        "prepare-root",
        str(source),
        "--destination-root",
        str(destination),
        "--protected-root",
        str(protected),
        "--visual-cache",
        str(cache),
        "--threshold",
        "80",
        "--phash-radius",
        "8",
        "--match-scaled",
        "--match-rotated",
        "--state-root",
        str(tmp_path / "state"),
        "--plan-out",
        str(plan_out),
    ]
    code, stdout, stderr = run_command(parser, command)
    assert code == DatasetExitCode.OK, (stdout, stderr)
    assert stderr == ""
    preview = parsed_stdout(stdout)
    assert preview["complete"]
    assert preview["plan"]["dry_run"]
    assert not plan_out.exists()
    assert not (tmp_path / "state").exists()

    authorized = command + ["--allow-apply"]
    code, stdout, stderr = run_command(parser, authorized + ["--write"])
    assert code == DatasetExitCode.OK
    assert stderr == ""
    written = json.loads(plan_out.read_text(encoding="utf-8"))
    assert parsed_stdout(stdout)["plan"]["plan_id"] == written["plan_id"]
    assert not written["dry_run"]
    assert {action["split"] for action in written["actions"]} <= {
        "train",
        "validation",
        "test",
    }
    assert all(len(action["files"]) == 2 for action in written["actions"])
    original = plan_out.read_bytes()

    code, stdout, stderr = run_command(parser, authorized + ["--write"])
    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "destination_conflict" in stderr
    assert plan_out.read_bytes() == original


def test_prepare_root_fails_closed_with_canonical_partial_result(tmp_path):
    parser = build_parser()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare-root",
            str(source),
            "--destination-root",
            str(destination),
            "--write",
        ],
    )
    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "invalid_arguments" in stderr

    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "prepare-root",
            str(source),
            "--destination-root",
            str(destination),
        ],
    )
    assert code == DatasetExitCode.PARTIAL
    assert stderr == ""
    result = parsed_stdout(stdout)
    assert result["state"] == "incomplete_evidence"
    assert result["plan"] is None
    assert result["issues"][0]["code"] == "no_visual_assets"


def test_validate_complete_and_changed_plan_paths(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    _plan, plan_path = build_plan(case)
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "validate", str(plan_path)],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["valid"]

    case["files"][1].write_bytes(b"changed")
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "validate", str(plan_path)],
    )
    assert code == DatasetExitCode.PARTIAL
    assert stderr == ""
    result = parsed_stdout(stdout)
    assert not result["valid"]
    assert result["issues"]


def test_apply_preview_execute_and_list_cover_the_facade_paths(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    plan, plan_path = build_plan(case)
    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "apply",
            str(plan_path),
            "--state-root",
            str(case["state"]),
        ],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    preview = parsed_stdout(stdout)
    assert preview["state"] == "ready"
    assert not case["state"].exists()
    assert all(path.exists() for path in case["files"])

    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "apply",
            str(plan_path),
            "--state-root",
            str(case["state"]),
            "--execute",
        ],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "applied"
    assert not any(path.exists() for path in case["files"])

    code, stdout, stderr = run_command(
        parser,
        ["dataset", "list", "--state-root", str(case["state"])],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    listing = parsed_stdout(stdout)
    assert listing["schema"] == "dupeguru.dataset-operation-list"
    assert listing["operations"][0]["plan_id"] == plan.plan_id


def test_restore_preview_and_execute_are_separate(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    plan, plan_path = build_plan(case)
    assert (
        run_command(
            parser,
            [
                "dataset",
                "apply",
                str(plan_path),
                "--state-root",
                str(case["state"]),
                "--execute",
            ],
        )[0]
        == DatasetExitCode.OK
    )
    base = [
        "dataset",
        "restore",
        plan.plan_id,
        "--state-root",
        str(case["state"]),
    ]
    code, stdout, stderr = run_command(parser, base)
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "ready"
    assert not any(path.exists() for path in case["files"])

    code, stdout, stderr = run_command(parser, base + ["--execute"])
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "restored"
    assert all(path.exists() for path in case["files"])


def test_finalize_preview_and_execute_are_separate(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    plan, plan_path = build_plan(case)
    run_command(
        parser,
        [
            "dataset",
            "apply",
            str(plan_path),
            "--state-root",
            str(case["state"]),
            "--execute",
        ],
    )
    base = [
        "dataset",
        "finalize",
        plan.plan_id,
        "--state-root",
        str(case["state"]),
    ]
    code, stdout, stderr = run_command(parser, base)
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "ready"

    code, stdout, stderr = run_command(parser, base + ["--execute"])
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "finalized"


def test_corrupt_unknown_and_bom_inputs_use_stderr_only(tmp_path):
    parser = build_parser()
    payloads = (
        '{"schema":"x","schema":"y"}',
        "\ufeff{}",
        "{broken",
        '{"schema":"dupeguru.dataset-plan","unknown":true}',
    )
    for index, payload in enumerate(payloads):
        path = tmp_path / "{}.json".format(index)
        path.write_text(payload, encoding="utf-8")
        code, stdout, stderr = run_command(
            parser,
            ["dataset", "validate", str(path)],
        )
        assert code == DatasetExitCode.INPUT_ERROR
        assert stdout == ""
        assert stderr
        assert stderr.endswith("\n")


def test_list_recovery_state_returns_partial_json_without_stderr(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path)
    plan, plan_path = build_plan(case)
    run_command(
        parser,
        [
            "dataset",
            "apply",
            str(plan_path),
            "--state-root",
            str(case["state"]),
            "--execute",
        ],
    )
    document = case["state"] / ".dupeguru-neo-dataset-executor" / "operations" / plan.plan_id / "operation.json"
    document.write_text("{}", encoding="utf-8")
    code, stdout, stderr = run_command(
        parser,
        ["dataset", "list", "--state-root", str(case["state"])],
    )
    assert code == DatasetExitCode.PARTIAL
    assert stderr == ""
    result = parsed_stdout(stdout)
    assert result["operations"][0]["state"] == "recovery_required"


def test_missing_location_and_unknown_dispatch_have_clear_exit_codes():
    parser = build_parser()
    code, stdout, stderr = run_command(parser, ["dataset", "list"])
    assert code == DatasetExitCode.INPUT_ERROR
    assert stdout == ""
    assert "invalid_arguments" in stderr

    args = argparse.Namespace(dataset_command="unknown")
    stdout_stream = io.StringIO()
    stderr_stream = io.StringIO()
    code = run_dataset_command(
        args,
        io.StringIO(),
        stdout_stream,
        stderr_stream,
    )
    assert code == DatasetExitCode.USAGE
    assert stdout_stream.getvalue() == ""
    assert "Unknown dataset command" in stderr_stream.getvalue()


def test_dry_run_plan_remains_read_only_even_with_execute(tmp_path):
    parser = build_parser()
    case = build_manifest(tmp_path, dry_run=True)
    _plan, plan_path = build_plan(case)
    code, stdout, stderr = run_command(
        parser,
        [
            "dataset",
            "apply",
            str(plan_path),
            "--state-root",
            str(case["state"]),
            "--execute",
        ],
    )
    assert code == DatasetExitCode.OK
    assert stderr == ""
    assert parsed_stdout(stdout)["state"] == "dry_run"
    assert not case["state"].exists()
    assert all(path.exists() for path in case["files"])


def test_dataset_schemas_cover_every_cli_input_and_output_top_level():
    expected = {
        "prepare-input",
        "prepare-root-request",
        "dataset-plan",
        "preparation-result",
        "plan-validation",
        "execution-report",
        "operation-summary",
        "operation-list",
    }
    assert set(DATASET_SCHEMAS) == expected
    for name, schema in DATASET_SCHEMAS.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].startswith("https://dupeguru.com/schemas/")
        assert schema["type"] == "object", name
        assert schema["additionalProperties"] is False, name
        assert set(schema["required"]) == set(schema["properties"]), name
        json.dumps(schema, allow_nan=False)
    plan_properties = DATASET_SCHEMAS["dataset-plan"]["properties"]
    assert plan_properties["actions"]["maxItems"] == 250_000
    assert plan_properties["keepers"]["maxItems"] == 250_000
    assert plan_properties["actions"]["items"]["properties"]["files"]["maxItems"] == 250_000
    prepare_properties = DATASET_SCHEMAS["prepare-input"]["properties"]
    assert prepare_properties["assets"]["maxItems"] == 250_000
    assert prepare_properties["clusters"]["maxItems"] == 250_000
    assert prepare_properties["sidecar_paths"]["maxItems"] == 250_000


def test_plan_schema_rejects_unknown_top_level_in_its_published_shape(tmp_path):
    case = build_manifest(tmp_path)
    plan, _plan_path = build_plan(case)
    forged = copy.deepcopy(plan.to_dict())
    forged["unknown"] = True
    properties = DATASET_SCHEMAS["dataset-plan"]["properties"]
    assert "unknown" not in properties
    assert DATASET_SCHEMAS["dataset-plan"]["additionalProperties"] is False
