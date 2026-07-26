import io
import json
import tracemalloc

import pytest

from core.cli import ExitCode, build_parser, main
from core.services import api as services_api
from core.services import PlanService, ScanRequest, ScanService, SchemaError
from core.services import jsonio
from core.services import schemas
from core.services.jsonio import (
    iter_plan_jsonl,
    iter_scan_jsonl,
    json_line,
    load_deletion_plan,
    load_scan_report,
)


def _scan_with_duplicate(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"bounded input")
    second.write_bytes(b"bounded input")
    report = ScanService().scan(ScanRequest(roots=(str(tmp_path),)))
    return report, first, second


def _minimal_video_library_report():
    return {
        "schema": "dupeguru.video-library-scan",
        "schema_version": 1,
        "scan_id": "video-scan",
        "created_at_ns": 1,
        "state": "complete",
        "partial": False,
        "roots": ["C:/library"],
        "threshold": 0.9,
        "limits": {
            "maximum_files": 1,
            "maximum_candidate_assessments": 1,
            "maximum_candidates": 1,
            "maximum_comparisons": 1,
            "maximum_fingerprint_files": 1,
            "maximum_groups": 1,
            "probe_timeout_seconds": 1,
            "maximum_probe_output_bytes": 1,
            "maximum_scan_seconds": 1,
        },
        "issues": [],
        "receipt": {
            "status": "complete",
            "complete": True,
            "discovered": 0,
            "analyzed": 0,
            "skipped": 0,
            "failed": 0,
        },
        "cache": {
            "path": None,
            "persistent": False,
            "hits": 0,
            "misses": 0,
            "writes": 0,
        },
        "groups": [],
        "safety": {
            "source_read_only": True,
            "review_only": True,
            "byte_exact_proof": False,
            "destructive_actions_allowed": False,
        },
        "summary": {
            "video_files": 0,
            "metadata_complete": 0,
            "candidate_assessments": 0,
            "candidates": 0,
            "comparisons": 0,
            "relations": 0,
            "groups": 0,
        },
    }


class _GeneratedIssueStream:
    """Generate 100k records without ever materializing their source text."""

    def __init__(self, issue_count):
        envelope = {
            "schema": "dupeguru.scan-record",
            "schema_version": 1,
            "document_schema": "dupeguru.scan-report",
        }
        self.header = json_line(
            {
                **envelope,
                "record_type": "header",
                "scan_id": "generated-scan",
                "created_at": "2026-07-26T00:00:00Z",
                "engine_version": "test",
                "roots": ["C:/library"],
                "mode": "exact",
            }
        )
        self.issue = json_line(
            {
                **envelope,
                "record_type": "issue",
                "issue": {
                    "path": "",
                    "code": "generated_issue",
                    "message": "bounded streaming test",
                },
            }
        )
        self.coverage = json_line(
            {
                **envelope,
                "record_type": "coverage",
                "coverage": {
                    "root": "C:/library",
                    "complete": True,
                    "counters": {},
                    "identity_capabilities": [],
                },
            }
        )
        self.summary = json_line(
            {
                **envelope,
                "record_type": "summary",
                "summary": {
                    "discovered_files": 0,
                    "hashed_files": 0,
                    "verified_groups": 0,
                    "duplicate_files": 0,
                    "issues": issue_count,
                    "complete": False,
                },
            }
        )
        self.issue_count = issue_count
        self.index = 0
        self.maximum_readline_size = 0
        self.readline_sizes = []

    def read(self, *_args, **_kwargs):
        raise AssertionError("bounded loaders must not call read()")

    def readline(self, size=-1):
        self.maximum_readline_size = max(self.maximum_readline_size, size)
        self.readline_sizes.append(size)
        if self.index == 0:
            result = self.header
        elif self.index <= self.issue_count:
            result = self.issue
        elif self.index == self.issue_count + 1:
            result = self.coverage
        elif self.index == self.issue_count + 2:
            result = self.summary
        else:
            return ""
        self.index += 1
        if size >= 0:
            assert len(result) <= size
        return result


def test_scan_jsonl_streams_100k_records_without_read_or_source_buffer():
    stream = _GeneratedIssueStream(100_000)

    report = load_scan_report(stream)

    assert len(report.issues) == 100_000
    assert report.summary.issues == 100_000
    assert report.summary.complete is False
    assert stream.index == 100_003
    assert stream.readline_sizes[0] == jsonio.MAX_JSON_DOCUMENT_BYTES + 1
    assert set(stream.readline_sizes[1:]) == {jsonio.MAX_JSONL_LINE_BYTES + 1}


def test_jsonl_long_physical_line_is_rejected(monkeypatch):
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 128)
    record = {
        "schema": "dupeguru.scan-record",
        "schema_version": 1,
        "document_schema": "dupeguru.scan-report",
        "record_type": "header",
        "padding": "x" * 512,
    }

    with pytest.raises(SchemaError, match="128-byte line limit"):
        load_scan_report(io.StringIO(json_line(record)))


def test_compact_single_json_is_not_constrained_by_jsonl_line_cap(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    payload = json.dumps(report.to_dict())
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 128)
    monkeypatch.setattr(
        jsonio,
        "MAX_JSON_DOCUMENT_BYTES",
        len(payload.encode("utf-8")) + 1,
    )

    loaded = load_scan_report(io.StringIO(payload))

    assert loaded.scan_id == report.scan_id
    assert loaded.groups == report.groups
    assert loaded.summary == report.summary


def test_compact_documents_are_decoded_once_and_only_trailing_whitespace_is_consumed(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    plan = PlanService().create(report)
    original = jsonio._strict_json_loads
    calls = []

    def counted(payload, **kwargs):
        calls.append(kwargs.get("label"))
        return original(payload, **kwargs)

    monkeypatch.setattr(jsonio, "_strict_json_loads", counted)

    loaded_report = load_scan_report(io.StringIO(json.dumps(report.to_dict()) + "\n \t\n"))
    assert loaded_report.to_dict() == report.to_dict()
    assert calls == ["scan report first line"]

    calls.clear()
    assert load_deletion_plan(io.StringIO(json.dumps(plan.to_dict()) + "\n \t\n")) == plan
    assert calls == ["deletion plan first line"]


def test_complete_single_line_document_rejects_later_nonblank_data(tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    payload = json.dumps(report.to_dict()) + "\n{}\n"

    with pytest.raises(SchemaError, match="trailing data"):
        load_scan_report(io.StringIO(payload))


def test_jsonl_record_and_total_byte_caps_are_enforced(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    payload = "".join(iter_scan_jsonl(report))

    monkeypatch.setattr(jsonio, "MAX_JSONL_RECORDS", 2)
    with pytest.raises(SchemaError, match="2-record limit"):
        load_scan_report(io.StringIO(payload))

    monkeypatch.setattr(jsonio, "MAX_JSONL_RECORDS", 1_000_000)
    monkeypatch.setattr(jsonio, "MAX_JSONL_TOTAL_BYTES", 32)
    with pytest.raises(SchemaError, match="32-byte total limit"):
        load_scan_report(io.StringIO(payload))

    monkeypatch.setattr(jsonio, "MAX_JSONL_TOTAL_BYTES", 2 * 1024 * 1024 * 1024)
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINES", 2)
    with pytest.raises(SchemaError, match="2 physical-line limit"):
        load_scan_report(io.StringIO(payload))


def test_single_json_document_byte_cap_is_enforced(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    payload = json.dumps(report.to_dict())
    monkeypatch.setattr(
        jsonio,
        "MAX_JSON_DOCUMENT_BYTES",
        len(payload.encode("utf-8")) - 1,
    )

    with pytest.raises(SchemaError, match="document limit"):
        load_scan_report(io.StringIO(payload))


def test_excessive_json_nesting_is_reported_as_input_schema_error():
    payload = "[" * 2_000 + "0" + "]" * 2_000

    with pytest.raises(SchemaError):
        load_scan_report(io.StringIO(payload))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"value":' + "9" * 5_000 + "}", "scalar token"),
        ('{"value":1e9999}', "non-finite JSON number"),
    ],
)
def test_resource_amplifying_or_nonfinite_numbers_fail_as_typed_schema_errors(payload, message):
    with pytest.raises(SchemaError, match=message):
        load_scan_report(io.StringIO(payload))


def test_parser_memory_failure_is_converted_to_a_typed_schema_error(monkeypatch):
    def fail_parse(*_args, **_kwargs):
        raise MemoryError("simulated parser exhaustion")

    monkeypatch.setattr(jsonio.json, "loads", fail_parse)
    with pytest.raises(SchemaError, match="parser memory budget"):
        load_scan_report(io.StringIO("{}"))


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"schema":"one","schema":"two"}', "duplicate JSON object key"),
        ('{"schema":"one","value":NaN}', "non-finite JSON number"),
    ),
)
def test_ambiguous_json_values_are_rejected(payload, message):
    with pytest.raises(SchemaError, match=message):
        load_scan_report(io.StringIO(payload))


def test_scan_group_and_plan_action_caps_are_enforced(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    plan = PlanService().create(report)

    monkeypatch.setattr(jsonio, "MAX_SCAN_GROUPS", 0)
    with pytest.raises(SchemaError, match="0 group limit"):
        load_scan_report(io.StringIO("".join(iter_scan_jsonl(report))))

    monkeypatch.setattr(jsonio, "MAX_PLAN_ACTIONS", 0)
    with pytest.raises(SchemaError, match="0 action limit"):
        load_deletion_plan(io.StringIO("".join(iter_plan_jsonl(plan))))


def test_scan_file_record_cap_applies_to_json_and_jsonl(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    monkeypatch.setattr(jsonio, "MAX_SCAN_FILE_RECORDS", 1)

    with pytest.raises(SchemaError, match="1 file-record limit"):
        load_scan_report(io.StringIO(json.dumps(report.to_dict())))
    with pytest.raises(SchemaError, match="1 file-record limit"):
        load_scan_report(io.StringIO("".join(iter_scan_jsonl(report))))


def test_oversized_nested_group_fails_before_file_record_objects_are_built(
    monkeypatch,
):
    file_record = {
        "path": "C:/library/file.bin",
        "size": 1,
        "mtime_ns": 1,
        "digest_algorithm": "sha256",
        "digest": "0" * 64,
    }
    duplicates = []
    for index in range(20_000):
        duplicate = dict(file_record)
        duplicate["path"] = "C:/library/duplicate-{}.bin".format(index)
        duplicates.append(duplicate)
    group = {
        "group_id": "oversized-group",
        "verification": "verified_exact",
        "verification_method": "sha256+byte-compare",
        "reference": file_record,
        "duplicates": duplicates,
    }
    record = {
        "schema": "dupeguru.scan-record",
        "schema_version": 1,
        "document_schema": "dupeguru.scan-report",
        "record_type": "group",
        "group": group,
    }
    header = {
        "schema": "dupeguru.scan-record",
        "schema_version": 1,
        "document_schema": "dupeguru.scan-report",
        "record_type": "header",
        "scan_id": "nested-limit",
        "created_at": "2026-07-26T00:00:00Z",
        "engine_version": "test",
        "roots": ["C:/library"],
        "mode": "exact",
    }
    payload = json_line(header) + json_line(record)
    monkeypatch.setattr(jsonio, "MAX_SCAN_FILE_RECORDS", 100)
    monkeypatch.setattr(
        jsonio.ScanGroup,
        "from_dict",
        classmethod(
            lambda _cls, _value: (_ for _ in ()).throw(
                AssertionError("file records must not be constructed after the cap is exceeded")
            )
        ),
    )

    tracemalloc.start()
    try:
        with pytest.raises(SchemaError, match="100 file-record limit"):
            load_scan_report(io.StringIO(payload))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 128 * 1024 * 1024


def test_plan_service_checks_action_cap_before_append(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    monkeypatch.setattr(services_api, "MAX_PLAN_ACTIONS", 0)

    with pytest.raises(ValueError, match="0-action limit"):
        PlanService().create(report)


def test_oversized_plan_is_rejected_before_execute_can_mutate(
    tmp_path,
    monkeypatch,
):
    report, first, second = _scan_with_duplicate(tmp_path)
    plan = PlanService().create(report)
    payload = json.dumps(plan.to_dict())
    monkeypatch.setattr(
        jsonio,
        "MAX_JSON_DOCUMENT_BYTES",
        len(payload.encode("utf-8")) - 1,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["apply", "-", "--execute", "--quiet"],
        stdin=io.StringIO(payload),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "document limit" in stderr.getvalue()
    assert first.exists() and second.exists()
    assert not (tmp_path / ".dupeguru-neo-quarantine").exists()


def test_compact_plan_json_uses_seek_free_bounded_readlines(tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    plan = PlanService().create(report)

    class ReadForbidden(io.StringIO):
        def read(self, *_args, **_kwargs):
            raise AssertionError("bounded loaders must not call read()")

    loaded = load_deletion_plan(ReadForbidden(json.dumps(plan.to_dict())))

    assert loaded == plan


@pytest.mark.parametrize("command", ("plan", "apply", "query"))
def test_exact_input_limits_are_visible_in_command_help(command, capsys):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args([command, "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "single JSON <= 64 MiB" in help_text
    assert "JSONL line <= 8 MiB" in help_text
    assert "total <= 2 GiB" in help_text
    assert "records <= 1000000" in help_text
    assert "scan groups <= 250000" in help_text
    assert "file records <= 1000000" in help_text
    assert "plan actions <= 250000" in help_text


def test_dataset_json_limit_is_visible_in_command_help(capsys):
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["dataset", "--help"])

    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "strict dataset JSON document <= 128 MiB" in help_text
    assert "seek-free bounded reader" in help_text
    assert "JSONL is not accepted" in help_text
    assert "recoverable apply transaction is limited to 10000 file records" in " ".join(help_text.split())


def test_published_schemas_express_loader_collection_limits_and_closed_objects():
    report_schema = schemas.scan_report_schema()
    plan_schema = schemas.deletion_plan_schema()
    scan_record = schemas.scan_record_schema()
    plan_record = schemas.plan_record_schema()

    assert report_schema["additionalProperties"] is False
    assert report_schema["properties"]["roots"]["maxItems"] == jsonio.MAX_DOCUMENT_ROOTS
    assert report_schema["properties"]["groups"]["maxItems"] == jsonio.MAX_SCAN_GROUPS
    assert report_schema["properties"]["issues"]["maxItems"] == jsonio.MAX_SCAN_ISSUES
    assert report_schema["properties"]["coverage"]["maxItems"] == jsonio.MAX_SCAN_COVERAGE_RECORDS
    assert report_schema["properties"]["groups"]["items"]["additionalProperties"] is False
    assert str(jsonio.MAX_SCAN_FILE_RECORDS) in report_schema["properties"]["groups"]["description"]

    assert plan_schema["additionalProperties"] is False
    assert plan_schema["properties"]["roots"]["maxItems"] == jsonio.MAX_DOCUMENT_ROOTS
    assert plan_schema["properties"]["actions"]["maxItems"] == jsonio.MAX_PLAN_ACTIONS
    assert plan_schema["properties"]["actions"]["items"]["additionalProperties"] is False
    file_schema = plan_schema["properties"]["actions"]["items"]["properties"]["target"]
    assert {"volume_id", "file_id"} <= set(file_schema["required"])
    assert file_schema["properties"]["volume_id"]["minLength"] == 1
    assert file_schema["properties"]["file_id"]["minLength"] == 1
    assert scan_record["additionalProperties"] is False
    assert plan_record["additionalProperties"] is False


@pytest.mark.parametrize(
    "writer,loader,document_kind",
    (
        (jsonio.write_scan_report, load_scan_report, "scan report"),
        (jsonio.write_deletion_plan, load_deletion_plan, "deletion plan"),
    ),
)
def test_json_writers_preflight_loader_byte_limit_before_first_write(
    tmp_path,
    monkeypatch,
    writer,
    loader,
    document_kind,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report if document_kind == "scan report" else PlanService().create(report)
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", 1)

    with pytest.raises(SchemaError, match="use --format jsonl"):
        writer(document, stream, output_format="json")

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "writer,document_kind",
    (
        (jsonio.write_scan_report, "scan report"),
        (jsonio.write_deletion_plan, "deletion plan"),
    ),
)
def test_jsonl_writers_preflight_line_limit_before_first_write(
    tmp_path,
    monkeypatch,
    writer,
    document_kind,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report if document_kind == "scan report" else PlanService().create(report)
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 1)

    with pytest.raises(SchemaError, match="loader line limit"):
        writer(document, stream, output_format="jsonl")

    assert stream.getvalue() == ""


def test_default_writers_emit_documents_accepted_by_their_own_loaders(tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    plan = PlanService().create(report)
    report_stream = io.StringIO()
    plan_stream = io.StringIO()

    jsonio.write_scan_report(report, report_stream)
    jsonio.write_deletion_plan(plan, plan_stream)

    assert load_scan_report(io.StringIO(report_stream.getvalue())).to_dict() == report.to_dict()
    assert load_deletion_plan(io.StringIO(plan_stream.getvalue())).to_dict() == plan.to_dict()


def test_cli_json_output_limit_failure_is_nonzero_atomic_and_actionable(
    tmp_path,
    monkeypatch,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", 1)

    exit_code = main(
        ["plan", "-", "--format", "json"],
        stdin=io.StringIO("".join(iter_scan_jsonl(report))),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "use --format jsonl" in stderr.getvalue()


def test_generic_service_json_writer_preflights_before_first_write(
    monkeypatch,
):
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", 1)

    with pytest.raises(SchemaError, match="loader limit"):
        jsonio.write_json({"schema": "service-report"}, stream)

    assert stream.getvalue() == ""


def test_generic_service_json_writer_rejects_nonfinite_numbers_atomically():
    stream = io.StringIO()

    with pytest.raises(SchemaError, match="could not be rendered safely"):
        jsonio.write_json({"score": float("nan")}, stream)

    assert stream.getvalue() == ""


def test_video_json_writer_preflights_loader_limit_before_first_write(
    monkeypatch,
):
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSON_DOCUMENT_BYTES", 1)

    with pytest.raises(SchemaError, match="use --format jsonl"):
        jsonio.write_video_library_report(
            _minimal_video_library_report(),
            stream,
            output_format="json",
        )

    assert stream.getvalue() == ""


def test_video_jsonl_writer_preflights_every_record_before_first_write(
    monkeypatch,
):
    report = _minimal_video_library_report()
    report["groups"] = [{"padding": "x" * 1_000}]
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 256)

    with pytest.raises(SchemaError, match="loader line limit"):
        jsonio.write_video_library_report(
            report,
            stream,
            output_format="jsonl",
        )

    assert stream.getvalue() == ""


def test_video_jsonl_publishes_the_exact_single_preflight_snapshot(
    monkeypatch,
):
    class ChangingGroups(list):
        def __init__(self):
            super().__init__()
            self.passes = 0

        def __iter__(self):
            self.passes += 1
            padding = "small" if self.passes == 1 else "x" * 5_000
            yield {"padding": padding}

    report = _minimal_video_library_report()
    groups = ChangingGroups()
    report["groups"] = groups
    stream = io.StringIO()
    monkeypatch.setattr(jsonio, "MAX_JSONL_LINE_BYTES", 2_000)

    jsonio.write_video_library_report(
        report,
        stream,
        output_format="jsonl",
    )

    assert groups.passes == 1
    assert max(len((line + "\n").encode("utf-8")) for line in stream.getvalue().splitlines()) <= 2_000


@pytest.mark.parametrize("output_format", ("json", "jsonl"))
def test_video_writer_success_is_strict_json_and_within_its_loader_caps(
    output_format,
):
    stream = io.StringIO()

    jsonio.write_video_library_report(
        _minimal_video_library_report(),
        stream,
        output_format=output_format,
    )

    lines = stream.getvalue().splitlines()
    assert lines
    assert all(json.loads(line) for line in lines)
    assert all(len((line + "\n").encode("utf-8")) <= jsonio.MAX_JSONL_LINE_BYTES for line in lines)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", True, "schema version"),
        ("unexpected", "rejected", "unknown field"),
    ),
)
@pytest.mark.parametrize("output_format", ("json", "jsonl"))
def test_video_writer_rejects_non_schema_envelopes_before_first_write(
    field,
    value,
    message,
    output_format,
):
    report = _minimal_video_library_report()
    report[field] = value
    stream = io.StringIO()

    with pytest.raises(SchemaError, match=message):
        jsonio.write_video_library_report(
            report,
            stream,
            output_format=output_format,
        )

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "document_kind",
    ("scan", "plan"),
)
def test_boolean_schema_versions_are_rejected(document_kind, tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report.to_dict()
    loader = load_scan_report
    if document_kind == "plan":
        document = PlanService().create(report).to_dict()
        loader = load_deletion_plan
    document["schema_version"] = True

    with pytest.raises(SchemaError, match="schema_version has an invalid type"):
        loader(io.StringIO(json.dumps(document)))


@pytest.mark.parametrize("field", ("size", "mtime_ns"))
def test_boolean_file_record_integers_are_rejected(field, tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report.to_dict()
    document["groups"][0]["reference"][field] = True

    with pytest.raises(SchemaError, match="{} has an invalid type".format(field)):
        load_scan_report(io.StringIO(json.dumps(document)))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("discovered_files", True),
        ("hashed_files", False),
        ("complete", 1),
    ),
)
def test_scan_summary_does_not_treat_booleans_and_integers_as_interchangeable(
    field,
    value,
    tmp_path,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report.to_dict()
    document["summary"][field] = value

    with pytest.raises(SchemaError):
        load_scan_report(io.StringIO(json.dumps(document)))


def test_plan_jsonl_summary_rejects_boolean_action_count(tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    records = [json.loads(line) for line in iter_plan_jsonl(PlanService().create(report))]
    records[-1]["summary"]["actions"] = True

    with pytest.raises(SchemaError, match="integer actions count"):
        load_deletion_plan(io.StringIO("".join(json_line(record) for record in records)))


def _nested_value(document, path):
    value = document
    for component in path:
        value = value[component]
    return value


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("groups", 0),
        ("groups", 0, "reference"),
        ("groups", 0, "duplicates", 0),
        ("issues", 0),
        ("coverage", 0),
        ("summary",),
    ),
)
def test_scan_documents_reject_unknown_fields_at_every_object_level(
    path,
    tmp_path,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = report.to_dict()
    document["issues"] = [
        {
            "path": "",
            "code": "test_issue",
            "message": "strict nested object validation",
        }
    ]
    document["summary"]["issues"] = 1
    document["summary"]["complete"] = False
    _nested_value(document, path)["unexpected"] = "must be rejected"

    with pytest.raises(SchemaError, match="unknown field"):
        load_scan_report(io.StringIO(json.dumps(document)))


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("actions", 0),
        ("actions", 0, "target"),
        ("actions", 0, "reference"),
    ),
)
def test_plan_documents_reject_unknown_fields_at_every_object_level(
    path,
    tmp_path,
):
    report, *_ = _scan_with_duplicate(tmp_path)
    document = PlanService().create(report).to_dict()
    _nested_value(document, path)["unexpected"] = "must be rejected"

    with pytest.raises(SchemaError, match="unknown field"):
        load_deletion_plan(io.StringIO(json.dumps(document)))


@pytest.mark.parametrize("document_kind", ("scan", "plan"))
def test_jsonl_wrappers_reject_unknown_fields(document_kind, tmp_path):
    report, *_ = _scan_with_duplicate(tmp_path)
    if document_kind == "scan":
        records = [json.loads(line) for line in iter_scan_jsonl(report)]
        loader = load_scan_report
    else:
        records = [json.loads(line) for line in iter_plan_jsonl(PlanService().create(report))]
        loader = load_deletion_plan
    for record in records:
        record["unexpected"] = "must be rejected"
        with pytest.raises(SchemaError, match="unknown field"):
            loader(io.StringIO("".join(json_line(item) for item in records)))
        del record["unexpected"]
