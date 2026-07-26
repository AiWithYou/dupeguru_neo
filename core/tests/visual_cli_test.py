import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

import core.services.jsonio as jsonio
import core.visual_cli as visual_cli
from core.cli import ExitCode, main
from core.services.models import SchemaError
from core.visual_service import VISUAL_REPORT_SCHEMA_VERSION


class _CountingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.write_count = 0

    def write(self, value):
        self.write_count += 1
        return super().write(value)


def _minimal_visual_report():
    return {
        "schema": "dupeguru.visual-report",
        "schema_version": VISUAL_REPORT_SCHEMA_VERSION,
        "report_id": "visual-report-for-writer-tests",
        "report_kind": "visual_scan",
        "created_at_ns": 1,
        "roots": ["C:\\library"],
        "reference_asset_id": "",
        "config": {"dry_run": True},
        "safety": {
            "verified_exact_evidence": False,
            "destructive_actions_allowed": False,
        },
        "assets": [{"asset_id": "asset-1", "path": "C:\\library\\one.png"}],
        "artifacts": [{"asset_id": "asset-1", "feature": {"phash": "0"}}],
        "evidence": [{"evidence_id": "evidence-1", "relation": "similar"}],
        "candidate_stats": {"candidate_pairs": 1},
        "scan_receipt": {
            "status": "complete_with_skips",
            "complete": False,
            "allows_destructive_actions": False,
            "issues": [{"code": "test", "message": "review required", "path": ""}],
        },
    }


def _write_image(path: Path, color=(20, 40, 80)):
    image = Image.new("RGB", (16, 16), color)
    image.save(path)


def test_visual_scan_emits_review_only_versioned_json(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")
    _write_image(root / "second.png")
    cache = tmp_path / "visual-cache.sqlite3"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "visual",
            "scan",
            str(root),
            "--cache",
            str(cache),
            "--max-images",
            "10",
            "--max-candidate-pairs",
            "45",
            "--max-matches",
            "10",
            "--format",
            "json",
            "--quiet",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["schema"] == "dupeguru.visual-report"
    assert payload["schema_version"] == VISUAL_REPORT_SCHEMA_VERSION
    assert payload["report_kind"] == "visual_scan"
    assert payload["scan_receipt"]["complete"] is True
    assert payload["scan_receipt"]["allows_destructive_actions"] is False
    assert payload["safety"]["verified_exact_evidence"] is False
    assert payload["safety"]["destructive_actions_allowed"] is False
    assert payload["candidate_stats"]["possible_pairs"] == 1
    assert len(payload["evidence"]) == 1
    assert payload["config"]["max_images"] == 10
    assert payload["config"]["max_candidate_pairs"] == 45
    assert payload["config"]["max_matches"] == 10
    assert all("blocks" not in artifact["feature"] for artifact in payload["artifacts"])
    assert cache.exists()
    assert stderr.getvalue() == ""


def test_visual_query_emits_streaming_jsonl(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    reference = tmp_path / "reference.png"
    _write_image(reference)
    _write_image(root / "same.png")
    _write_image(root / "different.png", (220, 10, 40))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "visual",
            "query",
            str(reference),
            str(root),
            "--max-images",
            "10",
            "--max-candidate-pairs",
            "10",
            "--max-matches",
            "10",
            "--quiet",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert exit_code == ExitCode.OK
    assert records[0]["record_type"] == "header"
    assert records[-2]["record_type"] == "receipt"
    assert records[-1]["record_type"] == "summary"
    assert all(record["schema"] == "dupeguru.visual-record" for record in records)
    assert all(record["document_schema"] == "dupeguru.visual-report" for record in records)
    assert records[0]["payload"]["report_kind"] == "visual_query"
    assert records[-2]["payload"]["allows_destructive_actions"] is False
    assert stderr.getvalue() == ""


def test_visual_resource_limit_is_partial_and_keeps_valid_report(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")
    _write_image(root / "second.png")
    stdout = io.StringIO()

    exit_code = main(
        [
            "visual",
            "scan",
            str(root),
            "--max-images",
            "1",
            "--max-candidate-pairs",
            "1",
            "--max-matches",
            "1",
            "--format",
            "json",
            "--quiet",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.PARTIAL_SCAN
    assert payload["scan_receipt"]["status"] == "resource_limit"
    assert payload["scan_receipt"]["complete"] is False
    assert any(issue["code"] == "resource_limit" for issue in payload["scan_receipt"]["issues"])
    assert payload["safety"]["destructive_actions_allowed"] is False


def test_visual_cache_inside_input_root_is_rejected_without_creation(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")
    cache = root / "visual-cache.sqlite3"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "visual",
            "scan",
            str(root),
            "--cache",
            str(cache),
            "--format",
            "json",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "outside every input root" in stderr.getvalue()
    assert not cache.exists()


def test_visual_cache_hardlink_alias_is_rejected_before_service_open(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    source = root / "first.png"
    _write_image(source)
    cache = tmp_path / "visual-cache.sqlite3"
    try:
        os.link(source, cache)
    except OSError as error:
        pytest.skip("hard links unavailable: {}".format(error))
    original = source.read_bytes()
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "visual",
            "scan",
            str(root),
            "--cache",
            str(cache),
            "--format",
            "json",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "must not be a hard-linked file" in stderr.getvalue()
    assert source.read_bytes() == original


def test_visual_cli_refuses_a_service_without_the_bounded_hook_contract(
    tmp_path,
):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")

    class LegacyService:
        def scan_roots(self, roots, *, config=None):
            raise AssertionError("unsupported service must not be invoked")

        def query_reference(self, reference, *, roots=(), config=None):
            raise AssertionError("unsupported service must not be invoked")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["visual", "scan", str(root), "--format", "json"],
        stdout=stdout,
        stderr=stderr,
        visual_service_factory=lambda **_kwargs: LegacyService(),
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "lacks required parameters" in stderr.getvalue()


def test_visual_schemas_are_exposed_by_the_main_cli():
    for name, expected_id in (
        (
            "visual-report",
            "urn:dupeguru-neo:schema:visual-report:{}".format(VISUAL_REPORT_SCHEMA_VERSION),
        ),
        ("visual-record", "urn:dupeguru-neo:schema:visual-record:1"),
    ):
        stdout = io.StringIO()
        exit_code = main(
            ["schema", name],
            stdout=stdout,
            stderr=io.StringIO(),
        )
        payload = json.loads(stdout.getvalue())
        assert exit_code == ExitCode.OK
        assert payload["$id"] == expected_id
        assert payload["additionalProperties"] is False
        if name == "visual-report":
            properties = payload["properties"]
            assert properties["schema_version"]["const"] == VISUAL_REPORT_SCHEMA_VERSION
            assert properties["safety"]["properties"]["destructive_actions_allowed"]["const"] is False
            artifact = properties["artifacts"]["items"]
            assert artifact["additionalProperties"] is False
            assert artifact["properties"]["feature"]["additionalProperties"] is False
            assert "dhashes" in artifact["properties"]["feature"]["required"]
            assert "tile_fingerprints" in artifact["properties"]["feature"]["required"]
            assert "crop_candidate" in properties["evidence"]["items"]["properties"]["relation"]["enum"]
        else:
            assert len(payload["oneOf"]) == 7


def test_visual_pretty_jsonl_and_unbounded_values_are_rejected(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")

    for extra, message in (
        (["--pretty"], "--pretty requires --format json"),
        (["--max-images", "0"], "max-images must be a positive integer"),
        (
            ["--max-candidate-pairs", "1", "--max-matches", "2"],
            "max-matches cannot exceed max-candidate-pairs",
        ),
        (
            ["--dhash-distance", "65"],
            "dhash_distance must be an integer between 0 and 64",
        ),
        (
            ["--color-histogram-distance", "1.1"],
            "color_histogram_distance must be between 0 and 1",
        ),
    ):
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            ["visual", "scan", str(root), *extra],
            stdout=stdout,
            stderr=stderr,
        )
        assert exit_code == ExitCode.INPUT_ERROR
        assert stdout.getvalue() == ""
        assert message in stderr.getvalue()


def test_visual_json_writer_accepts_exact_byte_cap_and_fails_atomically_above_it(
    monkeypatch,
):
    report = _minimal_visual_report()
    rendered = jsonio.json_line(report)
    boundary = len(rendered.encode("utf-8"))
    successful = _CountingStream()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", boundary)

    visual_cli._write_visual_report(
        report,
        successful,
        output_format="json",
        pretty=False,
    )

    assert successful.getvalue() == rendered
    assert successful.write_count == 1

    rejected = _CountingStream()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", boundary - 1)
    with pytest.raises(SchemaError, match="loader limit"):
        visual_cli._write_visual_report(
            report,
            rejected,
            output_format="json",
            pretty=False,
        )

    assert rejected.getvalue() == ""
    assert rejected.write_count == 0


@pytest.mark.parametrize(
    "limit_name",
    (
        "MAX_JSONL_LINE_BYTES",
        "MAX_JSONL_TOTAL_BYTES",
        "MAX_JSONL_LINES",
        "MAX_JSONL_RECORDS",
    ),
)
def test_visual_jsonl_writer_accepts_exact_loader_caps_and_rejects_before_first_write(
    monkeypatch,
    limit_name,
):
    report = _minimal_visual_report()
    lines = [jsonio.json_line(record) for record in visual_cli.iter_visual_jsonl(report)]
    if limit_name == "MAX_JSONL_LINE_BYTES":
        boundary = max(len(line.encode("utf-8")) for line in lines)
    elif limit_name == "MAX_JSONL_TOTAL_BYTES":
        boundary = sum(len(line.encode("utf-8")) for line in lines)
    else:
        boundary = len(lines)
    successful = _CountingStream()
    monkeypatch.setattr(jsonio, limit_name, boundary)

    visual_cli._write_visual_report(
        report,
        successful,
        output_format="jsonl",
        pretty=False,
    )

    assert successful.getvalue() == "".join(lines)
    assert successful.write_count == len(lines)

    rejected = _CountingStream()
    monkeypatch.setattr(jsonio, limit_name, boundary - 1)
    with pytest.raises(SchemaError):
        visual_cli._write_visual_report(
            report,
            rejected,
            output_format="jsonl",
            pretty=False,
        )

    assert rejected.getvalue() == ""
    assert rejected.write_count == 0


@pytest.mark.parametrize(
    "oversized_record",
    ("header", "asset", "artifact", "evidence", "issue"),
)
def test_visual_jsonl_preflights_every_record_kind_before_first_write(
    monkeypatch,
    oversized_record,
):
    report = _minimal_visual_report()
    padding = "x" * 1_000
    if oversized_record == "header":
        report["roots"] = [padding]
    elif oversized_record == "issue":
        report["scan_receipt"]["issues"][0]["message"] = padding
    else:
        collection = {
            "asset": "assets",
            "artifact": "artifacts",
            "evidence": "evidence",
        }[oversized_record]
        report[collection][0]["padding"] = padding
    stream = _CountingStream()
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 512)

    with pytest.raises(SchemaError, match="loader line limit"):
        visual_cli._write_visual_report(
            report,
            stream,
            output_format="jsonl",
            pretty=False,
        )

    assert stream.getvalue() == ""
    assert stream.write_count == 0


@pytest.mark.parametrize("output_format", ("json", "jsonl"))
def test_visual_writer_rejects_structural_overflow_before_first_write(
    output_format,
):
    report = _minimal_visual_report()
    nested = {}
    for _ in range(70):
        nested = {"nested": nested}
    report["assets"][0]["nested"] = nested
    stream = _CountingStream()

    with pytest.raises(SchemaError):
        visual_cli._write_visual_report(
            report,
            stream,
            output_format=output_format,
            pretty=False,
        )

    assert stream.getvalue() == ""
    assert stream.write_count == 0


def test_visual_jsonl_success_publishes_one_preflighted_snapshot(
    monkeypatch,
):
    report = _minimal_visual_report()
    expected = tuple(visual_cli.iter_visual_jsonl(report))
    passes = 0

    def counted_records(_report):
        nonlocal passes
        passes += 1
        yield from expected

    monkeypatch.setattr(visual_cli, "iter_visual_jsonl", counted_records)
    stream = _CountingStream()

    visual_cli._write_visual_report(
        report,
        stream,
        output_format="jsonl",
        pretty=False,
    )

    assert passes == 1
    assert stream.write_count == len(expected)


@pytest.mark.parametrize("output_format", ("json", "jsonl"))
def test_visual_writer_translates_memory_failure_before_first_write(
    monkeypatch,
    output_format,
):
    report = _minimal_visual_report()
    stream = _CountingStream()

    def memory_failure(*_args, **_kwargs):
        raise MemoryError("synthetic exhaustion")

    if output_format == "json":
        monkeypatch.setattr(jsonio.json, "dumps", memory_failure)
    else:
        monkeypatch.setattr(visual_cli, "json_line", memory_failure)

    with pytest.raises(SchemaError, match="could not"):
        visual_cli._write_visual_report(
            report,
            stream,
            output_format=output_format,
            pretty=False,
        )

    assert stream.getvalue() == ""
    assert stream.write_count == 0


def test_visual_missing_reference_is_an_input_error(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    _write_image(root / "first.png")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "visual",
            "query",
            str(tmp_path / "missing.png"),
            str(root),
            "--format",
            "json",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "missing.png" in stderr.getvalue()
