import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from core.cli import AUXILIARY_SCHEMAS, SCHEMA_NAMES, ExitCode, build_parser, main
from core.services import PlanService, ScanRequest, ScanService
from core.services.adapters import LocalDoctorAdapter
from core.services.jsonio import (
    iter_plan_jsonl,
    iter_scan_jsonl,
    load_deletion_plan,
)


def _duplicates(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    return first, second


def test_scan_stdout_is_jsonl_and_progress_is_only_on_stderr(tmp_path):
    _duplicates(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["scan", str(tmp_path)], stdout=stdout, stderr=stderr)

    assert exit_code == ExitCode.OK
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["record_type"] for record in records] == ["header", "group", "coverage", "summary"]
    assert all(record["schema"] == "dupeguru.scan-record" for record in records)
    assert all(record["document_schema"] == "dupeguru.scan-report" for record in records)
    assert "[discovering]" in stderr.getvalue()
    assert not stderr.getvalue().lstrip().startswith("{")


def test_scan_quiet_keeps_stderr_empty(tmp_path):
    _duplicates(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["scan", str(tmp_path), "--quiet"], stdout=stdout, stderr=stderr)

    assert exit_code == ExitCode.OK
    assert stderr.getvalue() == ""
    assert all(json.loads(line) for line in stdout.getvalue().splitlines())


def test_plan_reads_jsonl_from_stdin_and_emits_self_compatible_jsonl_by_default(
    tmp_path,
):
    _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    stdin = io.StringIO("".join(iter_scan_jsonl(report)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["plan", "-"], stdin=stdin, stdout=stdout, stderr=stderr)

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    loaded = load_deletion_plan(io.StringIO(stdout.getvalue()))
    assert exit_code == ExitCode.OK
    assert [record["record_type"] for record in records] == [
        "header",
        "action",
        "summary",
    ]
    assert all(record["schema"] == "dupeguru.plan-record" for record in records)
    assert len(loaded.actions) == 1
    assert stderr.getvalue() == ""


def test_plan_explicit_json_emits_a_versioned_document(tmp_path):
    _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    stdout = io.StringIO()

    exit_code = main(
        ["plan", "-", "--format", "json"],
        stdin=io.StringIO("".join(iter_scan_jsonl(report))),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["schema"] == "dupeguru.deletion-plan"
    assert payload["schema_version"] == 1
    assert len(payload["actions"]) == 1


def test_apply_is_dry_run_by_default_and_does_not_delete(tmp_path):
    first, second = _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    stdin = io.StringIO("".join(iter_plan_jsonl(plan)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["apply", "-", "--quiet"], stdin=stdin, stdout=stdout, stderr=stderr)

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["dry_run"] is True
    assert payload["summary"]["ready"] == 1
    assert first.exists() and second.exists()
    assert stderr.getvalue() == ""


def test_apply_accepts_explicit_dry_run_and_rejects_conflicting_mode_flags(
    tmp_path,
):
    first, second = _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["apply", "-", "--dry-run", "--quiet"],
        stdin=io.StringIO("".join(iter_plan_jsonl(plan))),
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["dry_run"] is True
    assert first.exists() and second.exists()
    assert stderr.getvalue() == ""

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(
            ["apply", "-", "--dry-run", "--execute"],
        )
    assert raised.value.code == ExitCode.USAGE

    with pytest.raises(SystemExit) as legacy_delete:
        build_parser().parse_args(
            ["plan", "scan.jsonl", "--operation", "delete"],
        )
    assert legacy_delete.value.code == ExitCode.USAGE

    for command in ("restore", "finalize"):
        with pytest.raises(SystemExit) as conflicting_quarantine_mode:
            build_parser().parse_args(
                [
                    "quarantine",
                    command,
                    "operation.json",
                    "--dry-run",
                    "--execute",
                ],
            )
        assert conflicting_quarantine_mode.value.code == ExitCode.USAGE


def test_apply_execute_quarantines_and_restore_requires_execute(tmp_path):
    first, second = _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    stdin = io.StringIO("".join(iter_plan_jsonl(plan)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["apply", "-", "--execute", "--quiet"], stdin=stdin, stdout=stdout, stderr=stderr)

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["dry_run"] is False
    assert payload["results"][0]["status"] == "applied"
    assert payload["results"][0]["safe_state"] == "staged"
    assert first.exists() != second.exists()

    list_stdout = io.StringIO()
    list_exit = main(
        ["quarantine", "list", str(tmp_path)],
        stdout=list_stdout,
        stderr=io.StringIO(),
    )
    listed = json.loads(list_stdout.getvalue())
    assert list_exit == ExitCode.OK
    assert listed["summary"]["operations"] == 1
    assert listed["operations"][0]["state"] == "staged"

    preflight_stdout = io.StringIO()
    preflight_exit = main(
        ["quarantine", "restore", payload["results"][0]["operation_plan_path"]],
        stdout=preflight_stdout,
        stderr=io.StringIO(),
    )
    preflight = json.loads(preflight_stdout.getvalue())
    assert preflight_exit == ExitCode.OK
    assert preflight["dry_run"] is True
    assert preflight["result"]["status"] == "ready"
    assert preflight["result"]["safe_state"] == "staged"
    assert first.exists() != second.exists()

    restore_stdout = io.StringIO()
    restore_exit = main(
        [
            "quarantine",
            "restore",
            payload["results"][0]["operation_plan_path"],
            "--execute",
        ],
        stdout=restore_stdout,
        stderr=io.StringIO(),
    )
    restored = json.loads(restore_stdout.getvalue())
    assert restore_exit == ExitCode.OK
    assert restored["dry_run"] is False
    assert restored["result"]["safe_state"] == "restored"
    assert first.exists() and second.exists()


def test_quarantine_finalize_requires_execute_for_permanent_removal(tmp_path):
    first, second = _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    plan = PlanService().create(report)
    apply_stdout = io.StringIO()
    apply_exit = main(
        ["apply", "-", "--execute", "--quiet"],
        stdin=io.StringIO("".join(iter_plan_jsonl(plan))),
        stdout=apply_stdout,
        stderr=io.StringIO(),
    )
    applied = json.loads(apply_stdout.getvalue())
    result = applied["results"][0]

    preflight_stdout = io.StringIO()
    preflight_exit = main(
        ["quarantine", "finalize", result["operation_plan_path"]],
        stdout=preflight_stdout,
        stderr=io.StringIO(),
    )
    preflight = json.loads(preflight_stdout.getvalue())
    assert preflight_exit == ExitCode.OK
    assert preflight["dry_run"] is True
    assert preflight["result"]["status"] == "ready"
    assert preflight["result"]["safe_state"] == "staged"
    assert os.path.exists(result["quarantine_path"])

    finalize_stdout = io.StringIO()
    finalize_exit = main(
        [
            "quarantine",
            "finalize",
            result["operation_plan_path"],
            "--execute",
        ],
        stdout=finalize_stdout,
        stderr=io.StringIO(),
    )
    finalized = json.loads(finalize_stdout.getvalue())

    assert apply_exit == ExitCode.OK
    assert preflight_exit == ExitCode.OK
    assert finalize_exit == ExitCode.OK
    assert finalized["dry_run"] is False
    assert finalized["result"]["safe_state"] == "finalized"
    assert not os.path.exists(result["quarantine_path"])
    assert first.exists() != second.exists()


def test_quarantine_rejects_untrusted_operation_file_as_input_error(tmp_path):
    operation = tmp_path / "operation.json"
    operation.write_text('{"untrusted":true}', encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["quarantine", "restore", str(operation)],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "QuarantineError" in stderr.getvalue()


def test_invalid_schema_has_input_error_exit_and_no_stdout():
    stdin = io.StringIO('{"schema":"dupeguru.scan-report","schema_version":999}')
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["plan", "-"], stdin=stdin, stdout=stdout, stderr=stderr)

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "Unsupported" in stderr.getvalue()


def test_missing_root_returns_partial_scan_exit_with_valid_payload(tmp_path):
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["scan", str(tmp_path / "missing"), "--quiet"],
        stdout=stdout,
        stderr=stderr,
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert exit_code == ExitCode.PARTIAL_SCAN
    assert [record["record_type"] for record in records] == ["header", "issue", "coverage", "summary"]
    assert records[-1]["summary"]["complete"] is False
    assert stderr.getvalue() == ""


def test_plan_refuses_an_incomplete_scan_report(tmp_path):
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path / "missing"),)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["plan", "-"],
        stdin=io.StringIO("".join(iter_scan_jsonl(report))),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "destructive plans are disabled" in stderr.getvalue()


def test_query_emits_machine_readable_payload(tmp_path):
    first, _ = _duplicates(tmp_path)
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    stdin = io.StringIO("".join(iter_scan_jsonl(report)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["query", "-", "--path", str(first)],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["schema"] == "dupeguru.query-report"
    assert payload["summary"]["groups"] == 1
    assert stderr.getvalue() == ""


def test_doctor_entry_point_does_not_import_pyqt():
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "core.cli", "doctor"],
        cwd=os.getcwd(),
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == ExitCode.OK
    assert completed.stderr == ""
    assert payload["pyqt_imported"] is False
    assert payload["capabilities"]["apply_execute"] is True
    assert payload["capabilities"]["verified_exact_engine"] is True
    assert payload["capabilities"]["same_volume_quarantine"] is True
    assert payload["capabilities"]["trash"] is False
    assert payload["capabilities"]["persistent_catalog"] is True
    assert payload["capabilities"]["catalog_resumable_scan"] is True
    assert payload["capabilities"]["catalog_immutable_changes"] is True
    assert payload["capabilities"]["catalog_verified_groups"] is True
    assert payload["capabilities"]["dataset_workflow"] is True
    assert payload["capabilities"]["visual_similarity_review"] is True
    assert payload["capabilities"]["visual_bounded_scan"] is True
    assert payload["capabilities"]["visual_reference_query"] is True
    assert payload["capabilities"]["video_similarity_review"] is True
    assert payload["capabilities"]["video_library_scan"] is True


def test_top_level_help_lists_every_integrated_command():
    completed = subprocess.run(
        [sys.executable, "-m", "core.cli", "--help"],
        cwd=os.getcwd(),
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == ExitCode.OK
    assert completed.stderr == ""
    for command in {
        "scan",
        "plan",
        "apply",
        "query",
        "doctor",
        "quarantine",
        "video",
        "dataset",
        "catalog",
        "visual",
        "schema",
    }:
        assert command in completed.stdout


def test_doctor_diagnoses_only_the_supported_pyqt6_runtime(monkeypatch):
    for name in tuple(sys.modules):
        if name == "PyQt6" or name.startswith("PyQt6."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "PyQt5", object())
    assert LocalDoctorAdapter().inspect()["pyqt_imported"] is False

    monkeypatch.setitem(sys.modules, "PyQt6", object())
    assert LocalDoctorAdapter().inspect()["pyqt_imported"] is True


def test_schema_command_returns_bundled_versioned_json_schema():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["schema", "deletion-plan"], stdout=stdout, stderr=stderr)

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["$id"] == "urn:dupeguru-neo:schema:deletion-plan:1"
    assert payload["properties"]["schema_version"]["const"] == 1
    assert payload["properties"]["actions"]["items"]["properties"]["operation"]["const"] == "quarantine"
    assert (
        payload["properties"]["actions"]["items"]["properties"]["target"]["properties"]["digest_algorithm"]["const"]
        == "sha256"
    )
    assert stderr.getvalue() == ""


def test_quarantine_operation_schema_is_exposed():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["schema", "quarantine-operation"], stdout=stdout, stderr=stderr)

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["$id"] == "urn:dupeguru-neo:schema:quarantine-operation:1"
    assert payload["properties"]["operation"]["const"] == "quarantine"
    assert stderr.getvalue() == ""


def test_quarantine_action_schema_records_the_dry_run_boundary():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["schema", "quarantine-action"],
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert "dry_run" in payload["required"]
    assert payload["properties"]["dry_run"] == {"type": "boolean"}
    assert payload["additionalProperties"] is False
    assert stderr.getvalue() == ""


def test_dataset_command_is_integrated_without_creating_preview_state(tmp_path):
    state_root = tmp_path / "dataset-state"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["dataset", "list", "--state-root", str(state_root)],
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload == {
        "operations": [],
        "schema": "dupeguru.dataset-operation-list",
        "schema_version": 1,
    }
    assert not state_root.exists()
    assert stderr.getvalue() == ""


def test_catalog_command_is_integrated_with_versioned_group_stream(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    duplicate = b"catalog integration duplicate"
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(duplicate)
    second.write_bytes(duplicate)
    database = tmp_path / "catalog.sqlite3"
    scan_stdout = io.StringIO()
    scan_stderr = io.StringIO()

    scan_exit = main(
        ["catalog", "scan", str(database), str(root)],
        stdout=scan_stdout,
        stderr=scan_stderr,
    )

    scan = json.loads(scan_stdout.getvalue())
    assert scan_exit == ExitCode.OK
    assert scan["schema"] == "dupeguru.catalog-result"
    assert scan["state"] == "complete"
    assert scan["result"]["status"]["verified_projection_allowed"] is True
    assert scan_stderr.getvalue() == ""

    groups_stdout = io.StringIO()
    groups_stderr = io.StringIO()
    groups_exit = main(
        ["catalog", "groups", str(database), "--page-size", "1"],
        stdout=groups_stdout,
        stderr=groups_stderr,
    )

    records = [json.loads(line) for line in groups_stdout.getvalue().splitlines()]
    assert groups_exit == ExitCode.OK
    assert [record["record_type"] for record in records] == [
        "header",
        "group_header",
        "member_chunk",
        "group_end",
        "summary",
    ]
    assert {member["path"] for member in records[2]["member_chunk"]["members"]} == {
        str(first),
        str(second),
    }
    assert records[1]["safety"]["destructive_workflow"] == "quarantine_then_explicit_finalize"
    assert records[1]["schema_version"] == 2
    assert records[3]["group_end"]["total_verifications"] == 1
    assert groups_stderr.getvalue() == ""


def test_dataset_and_catalog_schemas_are_exposed_directly():
    expected = {
        "dataset-plan": "https://dupeguru.com/schemas/dataset-plan/v1",
        "catalog-result": "urn:dupeguru-neo:schema:catalog-result:1",
        "catalog-group-record": "urn:dupeguru-neo:schema:catalog-group-record:2",
        "catalog-change-record": "urn:dupeguru-neo:schema:catalog-change-record:2",
    }

    for name, schema_id in expected.items():
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            ["schema", name],
            stdout=stdout,
            stderr=stderr,
        )
        payload = json.loads(stdout.getvalue())
        assert exit_code == ExitCode.OK
        assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert payload["$id"] == schema_id
        assert stderr.getvalue() == ""


def test_every_advertised_schema_name_emits_draft_2020_12_json():
    for name in sorted({*SCHEMA_NAMES, *AUXILIARY_SCHEMAS}):
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            ["schema", name],
            stdout=stdout,
            stderr=stderr,
        )
        payload = json.loads(stdout.getvalue())
        assert exit_code == ExitCode.OK, name
        assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema", name
        assert stderr.getvalue() == "", name


def test_documented_cli_examples_are_accepted_by_the_installed_parser():
    repository = Path(__file__).resolve().parents[2]
    documents = (
        repository / "README.md",
        repository / "help" / "en" / "automation.rst",
        repository / "help" / "en" / "catalog.rst",
        repository / "help" / "en" / "dataset.rst",
        repository / "help" / "en" / "video.rst",
    )
    parser = build_parser()
    parsed_examples = []

    for document in documents:
        for raw_line in document.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith("dupeguru "):
                continue
            command = stripped[len("dupeguru ") :]
            command = command.split(" > ", 1)[0]
            parser.parse_args(shlex.split(command, posix=True))
            parsed_examples.append((document.name, command))

    assert len(parsed_examples) >= 20
