from __future__ import annotations

import json
import math
import tempfile
from itertools import chain
from typing import Any, Dict, Iterable, Iterator, List, Mapping, TextIO, Tuple

from core.safe_json import (
    SERVICE_DOCUMENT_JSON_LIMITS,
    SERVICE_JSONL_RECORD_LIMITS,
    JsonStructureError,
    preflight_json_structure,
)
from core.services.models import (
    DELETION_PLAN_SCHEMA,
    PLAN_RECORD_SCHEMA,
    SCAN_RECORD_SCHEMA,
    SCAN_REPORT_SCHEMA,
    SCHEMA_VERSION,
    VIDEO_LIBRARY_RECORD_SCHEMA,
    VIDEO_LIBRARY_SCAN_SCHEMA,
    DeletionPlan,
    PlanAction,
    ScanGroup,
    ScanCoverage,
    ScanIssue,
    ScanReport,
    ScanSummary,
    SchemaError,
    validate_envelope,
)

# All CLI document loaders are fail-closed at these public, documented bounds.
# JSONL is the large-input format: it is parsed one physical line at a time and
# only the typed result objects—not a duplicate copy of the source text—remain
# in memory.
MAX_JSON_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_JSONL_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024
MAX_JSONL_LINES = 1_100_000
MAX_JSONL_RECORDS = 1_000_000
MAX_SCAN_GROUPS = 250_000
MAX_SCAN_FILE_RECORDS = 1_000_000
MAX_SCAN_ISSUES = 500_000
MAX_SCAN_COVERAGE_RECORDS = 100_000
MAX_PLAN_ACTIONS = 250_000
MAX_DOCUMENT_ROOTS = 100_000


def _unique_json_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError("duplicate JSON object key: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SchemaError("non-finite JSON number is not accepted: {}".format(value))


def _finite_json_float(value: str) -> float:
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise SchemaError("invalid JSON number: {}".format(value)) from error
    if not math.isfinite(result):
        raise SchemaError("non-finite JSON number is not accepted: {}".format(value))
    return result


def _strict_json_loads(
    payload: str,
    *,
    limits=SERVICE_DOCUMENT_JSON_LIMITS,
    label: str = "JSON input",
) -> Any:
    try:
        preflight_json_structure(
            payload,
            limits=limits,
            label=label,
        )
    except JsonStructureError as error:
        raise SchemaError(str(error)) from error
    try:
        return json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except (SchemaError, json.JSONDecodeError):
        raise
    except MemoryError as error:
        raise SchemaError("{} exceeded the JSON parser memory budget".format(label)) from error
    except (RecursionError, ValueError, OverflowError) as error:
        raise SchemaError("invalid {}: {}".format(label, error)) from error


class _BoundedLineReader:
    """Seek-free UTF-8 byte accounting around a text input stream."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.line_number = 0
        self.total_bytes = 0
        # Format is unknown until the first nonblank JSON value is inspected.
        # The dispatch limits are the larger of the two formats so a compact
        # single JSON document is not accidentally constrained by JSONL's
        # per-line cap (and vice versa).
        self._line_byte_limit = max(
            MAX_JSON_DOCUMENT_BYTES,
            MAX_JSONL_LINE_BYTES,
        )
        self._total_byte_limit = max(
            MAX_JSON_DOCUMENT_BYTES,
            MAX_JSONL_TOTAL_BYTES,
        )
        self._line_count_limit: int | None = None
        self._mode = "dispatch"
        self._jsonl_line_violation: Tuple[int, int] | None = None

    def use_jsonl_limits(self) -> None:
        self._mode = "JSONL"
        self._line_byte_limit = MAX_JSONL_LINE_BYTES
        self._total_byte_limit = MAX_JSONL_TOTAL_BYTES
        self._line_count_limit = MAX_JSONL_LINES
        if self._jsonl_line_violation is not None:
            line_number, _line_bytes = self._jsonl_line_violation
            raise SchemaError(
                "input line {} exceeds the {}-byte line limit".format(
                    line_number,
                    MAX_JSONL_LINE_BYTES,
                )
            )
        if self.line_number > MAX_JSONL_LINES:
            raise SchemaError("input exceeds the {} physical-line limit".format(MAX_JSONL_LINES))
        if self.total_bytes > MAX_JSONL_TOTAL_BYTES:
            raise SchemaError("JSONL input exceeds the {}-byte total limit".format(MAX_JSONL_TOTAL_BYTES))

    def use_json_document_limits(self, label: str) -> None:
        self._mode = "{} JSON".format(label)
        self._line_byte_limit = MAX_JSON_DOCUMENT_BYTES
        self._total_byte_limit = MAX_JSON_DOCUMENT_BYTES
        self._line_count_limit = None
        if self.total_bytes > MAX_JSON_DOCUMENT_BYTES:
            raise SchemaError(
                "{} JSON input exceeds the {}-byte document limit".format(
                    label,
                    MAX_JSON_DOCUMENT_BYTES,
                )
            )

    def readline(self) -> Tuple[int, str] | None:
        try:
            line = self.stream.readline(self._line_byte_limit + 1)
        except UnicodeError as error:
            raise SchemaError("input is not valid UTF-8 text: {}".format(error)) from error
        if line == "":
            return None
        if not isinstance(line, str):
            raise SchemaError("document loader requires a text input stream")
        self.line_number += 1
        if self._line_count_limit is not None and self.line_number > self._line_count_limit:
            raise SchemaError("input exceeds the {} physical-line limit".format(self._line_count_limit))
        try:
            line_bytes = len(line.encode("utf-8"))
        except UnicodeError as error:
            raise SchemaError("input contains invalid Unicode text: {}".format(error)) from error
        if self._mode == "dispatch" and line_bytes > MAX_JSONL_LINE_BYTES and self._jsonl_line_violation is None:
            self._jsonl_line_violation = (self.line_number, line_bytes)
        if line_bytes > self._line_byte_limit:
            raise SchemaError(
                "input line {} exceeds the {}-byte line limit".format(
                    self.line_number,
                    self._line_byte_limit,
                )
            )
        self.total_bytes += line_bytes
        if self.total_bytes > self._total_byte_limit:
            if self._mode == "JSONL":
                message = "JSONL input exceeds the {}-byte total limit".format(self._total_byte_limit)
            elif self._mode.endswith(" JSON"):
                message = "{} input exceeds the {}-byte document limit".format(
                    self._mode,
                    self._total_byte_limit,
                )
            else:
                message = "input exceeds the {}-byte dispatch limit".format(self._total_byte_limit)
            raise SchemaError(message)
        return self.line_number, line

    def __iter__(self) -> Iterator[Tuple[int, str]]:
        while True:
            item = self.readline()
            if item is None:
                return
            yield item


def _first_nonblank_line(
    reader: _BoundedLineReader,
    *,
    empty_message: str,
) -> Tuple[int, str]:
    for line_number, line in reader:
        if line.strip():
            return line_number, line
    raise SchemaError(empty_message)


def _looks_like_jsonl_record(
    payload: Any,
    *,
    record_schema: str,
    document_schema: str,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("schema") == record_schema
        or payload.get("document_schema") == document_schema
        or "record_type" in payload
    )


def _read_json_document(
    reader: _BoundedLineReader,
    first_line: str,
    *,
    label: str,
) -> Any:
    reader.use_json_document_limits(label)
    try:
        parts = [first_line]
        for _line_number, line in reader:
            parts.append(line)
        payload = "".join(parts)
    except MemoryError as error:
        raise SchemaError("{} JSON exceeded the input memory budget".format(label)) from error
    try:
        return _strict_json_loads(
            payload,
            limits=SERVICE_DOCUMENT_JSON_LIMITS,
            label="{} JSON document".format(label),
        )
    except SchemaError:
        raise
    except json.JSONDecodeError as error:
        raise SchemaError("invalid {} JSON document: {}".format(label, error)) from error


def _finish_single_line_document(
    reader: _BoundedLineReader,
    payload: Any,
    *,
    label: str,
) -> Any:
    """Consume only trailing whitespace without decoding the document twice."""

    reader.use_json_document_limits(label)
    for line_number, line in reader:
        if line.strip():
            raise SchemaError(
                "{} JSON document contains trailing data on line {}".format(
                    label,
                    line_number,
                )
            )
    return payload


def _iter_jsonl_records(
    lines: Iterable[Tuple[int, str]],
    *,
    label: str,
) -> Iterator[Tuple[int, Mapping[str, Any]]]:
    record_count = 0
    for line_number, line in lines:
        if not line.strip():
            continue
        record_count += 1
        if record_count > MAX_JSONL_RECORDS:
            raise SchemaError(
                "{} JSONL exceeds the {}-record limit".format(
                    label,
                    MAX_JSONL_RECORDS,
                )
            )
        try:
            record = _strict_json_loads(
                line,
                limits=SERVICE_JSONL_RECORD_LIMITS,
                label="JSONL record on line {}".format(line_number),
            )
        except SchemaError:
            raise
        except json.JSONDecodeError as error:
            raise SchemaError("invalid JSONL record on line {}: {}".format(line_number, error)) from error
        if not isinstance(record, Mapping):
            raise SchemaError("JSONL record on line {} must be an object".format(line_number))
        yield line_number, record


def _bounded_array_length(
    payload: Mapping[str, Any],
    field: str,
    maximum: int,
    *,
    label: str,
) -> None:
    value = payload.get(field)
    if isinstance(value, list) and len(value) > maximum:
        raise SchemaError(
            "{} exceeds the {} {} limit".format(
                label,
                maximum,
                field,
            )
        )


def _group_file_record_count(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    duplicates = payload.get("duplicates")
    if not isinstance(duplicates, list):
        return 0
    # Every valid group has exactly one reference in addition to duplicates.
    return 1 + len(duplicates)


def _enforce_scan_file_record_limit(groups: Any) -> None:
    if not isinstance(groups, list):
        return
    file_records = 0
    for group in groups:
        file_records += _group_file_record_count(group)
        if file_records > MAX_SCAN_FILE_RECORDS:
            raise SchemaError(
                "scan report exceeds the {} file-record limit".format(
                    MAX_SCAN_FILE_RECORDS,
                )
            )


_JSONL_ENVELOPE_FIELDS = {
    "schema",
    "schema_version",
    "document_schema",
    "record_type",
}
_SCAN_JSONL_RECORD_FIELDS = {
    "header": _JSONL_ENVELOPE_FIELDS
    | {
        "scan_id",
        "created_at",
        "engine_version",
        "roots",
        "mode",
    },
    "group": _JSONL_ENVELOPE_FIELDS | {"group"},
    "issue": _JSONL_ENVELOPE_FIELDS | {"issue"},
    "coverage": _JSONL_ENVELOPE_FIELDS | {"coverage"},
    "summary": _JSONL_ENVELOPE_FIELDS | {"summary"},
}
_PLAN_JSONL_RECORD_FIELDS = {
    "header": _JSONL_ENVELOPE_FIELDS
    | {
        "plan_id",
        "created_at",
        "engine_version",
        "source_scan_id",
        "roots",
    },
    "action": _JSONL_ENVELOPE_FIELDS | {"action"},
    "summary": _JSONL_ENVELOPE_FIELDS | {"summary"},
}


def _validate_jsonl_record_fields(
    record: Mapping[str, Any],
    *,
    record_type: Any,
    fields_by_type: Mapping[str, set[str]],
    label: str,
    line_number: int,
) -> None:
    expected = fields_by_type.get(record_type)
    if expected is None:
        raise SchemaError(
            "unknown {} JSONL record_type on line {}: {!r}".format(
                label,
                line_number,
                record_type,
            )
        )
    actual = set(record)
    missing = expected.difference(actual)
    if missing:
        raise SchemaError(
            "{} JSONL {} record on line {} is missing field(s): {}".format(
                label,
                record_type,
                line_number,
                ", ".join(sorted(missing)),
            )
        )
    unknown = actual.difference(expected)
    if unknown:
        raise SchemaError(
            "{} JSONL {} record on line {} contains unknown field(s): {}".format(
                label,
                record_type,
                line_number,
                ", ".join(sorted(str(field) for field in unknown)),
            )
        )


def json_line(payload: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def write_json(payload: Mapping[str, Any], stream: TextIO, pretty: bool = False) -> None:
    stream.write(
        _bounded_json_document(
            payload,
            pretty=pretty,
            label="service report",
            overflow_hint="reduce the generated data",
        )
    )


def _bounded_json_document(
    payload: Mapping[str, Any],
    *,
    pretty: bool,
    label: str,
    overflow_hint: str = "use --format jsonl",
) -> str:
    """Render only a single-JSON document that this module can read back."""

    try:
        if pretty:
            rendered = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
        else:
            rendered = json_line(payload)
        encoded_size = len(rendered.encode("utf-8"))
    except (
        MemoryError,
        RecursionError,
        TypeError,
        ValueError,
        OverflowError,
    ) as error:
        raise SchemaError(
            "{} JSON output could not be rendered safely: {}".format(
                label,
                error,
            )
        ) from error
    if encoded_size > MAX_JSON_DOCUMENT_BYTES:
        raise SchemaError(
            "{} JSON output would exceed the {}-byte loader limit; "
            "{}".format(
                label,
                MAX_JSON_DOCUMENT_BYTES,
                overflow_hint,
            )
        )
    try:
        preflight_json_structure(
            rendered,
            limits=SERVICE_DOCUMENT_JSON_LIMITS,
            label="{} JSON output".format(label),
        )
    except JsonStructureError as error:
        raise SchemaError("{}; {}".format(error, overflow_hint)) from error
    return rendered


def _validate_jsonl_output(
    lines: Iterable[str],
    *,
    label: str,
    validated_stream: TextIO | None = None,
) -> None:
    """Run the loader's complete JSONL resource contract before first write."""

    total_bytes = 0
    line_count = 0
    record_count = 0
    for line in lines:
        line_count += 1
        record_count += 1
        try:
            line_bytes = len(line.encode("utf-8"))
        except MemoryError as error:
            raise SchemaError("{} JSONL output exceeded the encoding memory budget".format(label)) from error
        if line_bytes > MAX_JSONL_LINE_BYTES:
            raise SchemaError(
                "{} JSONL record {} would exceed the {}-byte loader line "
                "limit; reduce the generated data or use --format json only "
                "when the complete document fits its limit".format(
                    label,
                    line_count,
                    MAX_JSONL_LINE_BYTES,
                )
            )
        total_bytes += line_bytes
        if total_bytes > MAX_JSONL_TOTAL_BYTES:
            raise SchemaError(
                "{} JSONL output would exceed the {}-byte loader total "
                "limit; reduce the generated data".format(
                    label,
                    MAX_JSONL_TOTAL_BYTES,
                )
            )
        if line_count > MAX_JSONL_LINES:
            raise SchemaError(
                "{} JSONL output would exceed the {} physical-line loader "
                "limit; reduce the generated data".format(
                    label,
                    MAX_JSONL_LINES,
                )
            )
        if record_count > MAX_JSONL_RECORDS:
            raise SchemaError(
                "{} JSONL output would exceed the {}-record loader limit; "
                "reduce the generated data".format(
                    label,
                    MAX_JSONL_RECORDS,
                )
            )
        try:
            preflight_json_structure(
                line,
                limits=SERVICE_JSONL_RECORD_LIMITS,
                label="{} JSONL record {}".format(label, line_count),
            )
        except JsonStructureError as error:
            raise SchemaError(
                "{}; reduce the generated data or use --format json only "
                "when the complete document fits its limit".format(error)
            ) from error
        if validated_stream is not None:
            validated_stream.write(line)


def _write_validated_jsonl_output(
    lines: Iterable[str],
    stream: TextIO,
    *,
    label: str,
) -> None:
    """Validate one generated snapshot, then publish that exact snapshot."""

    with tempfile.SpooledTemporaryFile(
        max_size=8 * 1024 * 1024,
        mode="w+t",
        encoding="utf-8",
        newline="",
    ) as validated:
        _validate_jsonl_output(
            lines,
            label=label,
            validated_stream=validated,
        )
        validated.seek(0)
        for line in validated:
            stream.write(line)


def _validate_scan_output_collections(report: ScanReport) -> None:
    payload = report.to_dict()
    _bounded_array_length(
        payload,
        "roots",
        MAX_DOCUMENT_ROOTS,
        label="scan report",
    )
    _bounded_array_length(
        payload,
        "groups",
        MAX_SCAN_GROUPS,
        label="scan report",
    )
    _bounded_array_length(
        payload,
        "issues",
        MAX_SCAN_ISSUES,
        label="scan report",
    )
    _bounded_array_length(
        payload,
        "coverage",
        MAX_SCAN_COVERAGE_RECORDS,
        label="scan report",
    )
    _enforce_scan_file_record_limit(payload.get("groups"))


def _validate_plan_output_collections(plan: DeletionPlan) -> None:
    if len(plan.roots) > MAX_DOCUMENT_ROOTS:
        raise SchemaError("deletion plan exceeds the {} roots limit".format(MAX_DOCUMENT_ROOTS))
    if len(plan.actions) > MAX_PLAN_ACTIONS:
        raise SchemaError("deletion plan exceeds the {} actions limit".format(MAX_PLAN_ACTIONS))


def iter_scan_jsonl(report: ScanReport) -> Iterator[str]:
    envelope = {
        "schema": SCAN_RECORD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "document_schema": SCAN_REPORT_SCHEMA,
    }
    yield json_line(
        {
            **envelope,
            "record_type": "header",
            "scan_id": report.scan_id,
            "created_at": report.created_at,
            "engine_version": report.engine_version,
            "roots": list(report.roots),
            "mode": report.mode,
        }
    )
    for group in report.groups:
        yield json_line({**envelope, "record_type": "group", "group": group.to_dict()})
    for issue in report.issues:
        yield json_line({**envelope, "record_type": "issue", "issue": issue.to_dict()})
    for coverage in report.coverage:
        yield json_line({**envelope, "record_type": "coverage", "coverage": coverage.to_dict()})
    yield json_line({**envelope, "record_type": "summary", "summary": report.summary.to_dict()})


def write_scan_report(report: ScanReport, stream: TextIO, output_format: str = "jsonl", pretty: bool = False) -> None:
    _validate_scan_output_collections(report)
    if output_format == "json":
        stream.write(
            _bounded_json_document(
                report.to_dict(),
                pretty=pretty,
                label="scan report",
            )
        )
        return
    if output_format != "jsonl":
        raise ValueError("Unknown output format: {}".format(output_format))
    if pretty:
        raise ValueError("--pretty requires --format json")
    _write_validated_jsonl_output(
        iter_scan_jsonl(report),
        stream,
        label="scan report",
    )


def iter_plan_jsonl(plan: DeletionPlan) -> Iterator[str]:
    envelope = {
        "schema": PLAN_RECORD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "document_schema": DELETION_PLAN_SCHEMA,
    }
    yield json_line(
        {
            **envelope,
            "record_type": "header",
            "plan_id": plan.plan_id,
            "created_at": plan.created_at,
            "engine_version": plan.engine_version,
            "source_scan_id": plan.source_scan_id,
            "roots": list(plan.roots),
        }
    )
    for action in plan.actions:
        yield json_line({**envelope, "record_type": "action", "action": action.to_dict()})
    yield json_line({**envelope, "record_type": "summary", "summary": {"actions": len(plan.actions)}})


def write_deletion_plan(
    plan: DeletionPlan,
    stream: TextIO,
    output_format: str = "jsonl",
    pretty: bool = False,
) -> None:
    _validate_plan_output_collections(plan)
    if output_format == "json":
        stream.write(
            _bounded_json_document(
                plan.to_dict(),
                pretty=pretty,
                label="deletion plan",
            )
        )
        return
    if output_format != "jsonl":
        raise ValueError("Unknown output format: {}".format(output_format))
    if pretty:
        raise ValueError("--pretty requires --format json")
    _write_validated_jsonl_output(
        iter_plan_jsonl(plan),
        stream,
        label="deletion plan",
    )


def _validate_video_library_report_envelope(
    report: Mapping[str, Any],
) -> None:
    allowed = {
        "schema",
        "schema_version",
        "scan_id",
        "created_at_ns",
        "state",
        "partial",
        "roots",
        "threshold",
        "limits",
        "issues",
        "receipt",
        "cache",
        "groups",
        "safety",
        "summary",
    }
    unknown = set(report).difference(allowed)
    if unknown:
        raise SchemaError(
            "video library report contains unknown field(s): {}".format(
                ", ".join(sorted(str(field) for field in unknown))
            )
        )
    missing = sorted(allowed.difference(report))
    if missing:
        raise SchemaError("video library report is missing fields: {}".format(", ".join(missing)))
    if report.get("schema") != VIDEO_LIBRARY_SCAN_SCHEMA:
        raise SchemaError("video library report has an invalid schema")
    if type(report.get("schema_version")) is not int or report["schema_version"] != SCHEMA_VERSION:
        raise SchemaError("video library report has an unsupported schema version")
    scan_id = report.get("scan_id")
    if not isinstance(scan_id, str) or not scan_id:
        raise SchemaError("video library report requires a scan_id")
    if type(report.get("created_at_ns")) is not int or report["created_at_ns"] < 0:
        raise SchemaError("video library report created_at_ns must be a non-negative integer")
    if type(report.get("partial")) is not bool:
        raise SchemaError("video library report partial must be a boolean")
    if report.get("state") not in {
        "complete",
        "partial_missing_tool",
        "partial_timeout",
        "partial_cancelled",
        "partial_resource_limit",
        "partial_tool_error",
        "failed",
    }:
        raise SchemaError("video library report has an invalid state")
    threshold = report.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise SchemaError("video library report threshold must be between 0 and 1")
    roots = report.get("roots")
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not root for root in roots):
        raise SchemaError("video library roots must be a non-empty string array")
    if not isinstance(report.get("groups"), list) or not isinstance(report.get("issues"), list):
        raise SchemaError("video library groups and issues must be arrays")
    for field in ("limits", "receipt", "cache", "safety", "summary"):
        if not isinstance(report.get(field), Mapping):
            raise SchemaError("video library {} must be an object".format(field))


def iter_video_library_jsonl(report: Mapping[str, Any]) -> Iterator[str]:
    """Yield a bounded video-library report as independently parseable records."""

    _validate_video_library_report_envelope(report)
    scan_id = report["scan_id"]
    groups = report["groups"]
    issues = report["issues"]

    envelope = {
        "schema": VIDEO_LIBRARY_RECORD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "document_schema": VIDEO_LIBRARY_SCAN_SCHEMA,
        "scan_id": scan_id,
    }
    empty = {
        "header": None,
        "group": None,
        "issue": None,
        "receipt": None,
        "summary": None,
    }

    def record(record_type: str, key: str, value: Any) -> str:
        return json_line(
            {
                **envelope,
                "record_type": record_type,
                **empty,
                key: value,
            }
        )

    header = {
        key: report[key]
        for key in (
            "created_at_ns",
            "state",
            "partial",
            "roots",
            "threshold",
            "limits",
            "cache",
            "safety",
        )
    }
    yield record("header", "header", header)
    for group in groups:
        yield record("group", "group", group)
    for issue in issues:
        yield record("issue", "issue", issue)
    yield record("receipt", "receipt", report["receipt"])
    yield record("summary", "summary", report["summary"])


def write_video_library_report(
    report: Mapping[str, Any],
    stream: TextIO,
    output_format: str = "jsonl",
    pretty: bool = False,
) -> None:
    _validate_video_library_report_envelope(report)
    if output_format == "json":
        stream.write(
            _bounded_json_document(
                report,
                pretty=pretty,
                label="video library report",
            )
        )
        return
    if output_format != "jsonl":
        raise ValueError("Unknown output format: {}".format(output_format))
    if pretty:
        raise ValueError("pretty output is only available with JSON")
    _write_validated_jsonl_output(
        iter_video_library_jsonl(report),
        stream,
        label="video library report",
    )


def load_scan_report(stream: TextIO) -> ScanReport:
    reader = _BoundedLineReader(stream)
    first = _first_nonblank_line(reader, empty_message="scan report is empty")
    _first_line_number, first_line = first
    try:
        first_payload = _strict_json_loads(
            first_line,
            limits=SERVICE_DOCUMENT_JSON_LIMITS,
            label="scan report first line",
        )
    except SchemaError:
        raise
    except json.JSONDecodeError:
        payload = _read_json_document(reader, first_line, label="scan report")
    else:
        if _looks_like_jsonl_record(
            first_payload,
            record_schema=SCAN_RECORD_SCHEMA,
            document_schema=SCAN_REPORT_SCHEMA,
        ):
            reader.use_jsonl_limits()
            return _load_scan_jsonl(chain((first,), reader))
        payload = _finish_single_line_document(
            reader,
            first_payload,
            label="scan report",
        )
    if not isinstance(payload, Mapping):
        raise SchemaError("scan report must be an object")
    _bounded_array_length(
        payload,
        "roots",
        MAX_DOCUMENT_ROOTS,
        label="scan report",
    )
    _bounded_array_length(
        payload,
        "groups",
        MAX_SCAN_GROUPS,
        label="scan report",
    )
    _enforce_scan_file_record_limit(payload.get("groups"))
    _bounded_array_length(
        payload,
        "issues",
        MAX_SCAN_ISSUES,
        label="scan report",
    )
    _bounded_array_length(
        payload,
        "coverage",
        MAX_SCAN_COVERAGE_RECORDS,
        label="scan report",
    )
    return ScanReport.from_dict(payload)


def _load_scan_jsonl(
    lines: Iterable[Tuple[int, str]],
) -> ScanReport:
    header: Dict[str, Any] = {}
    groups: List[ScanGroup] = []
    issues: List[ScanIssue] = []
    coverage: List[ScanCoverage] = []
    summary = None
    file_record_count = 0
    for line_number, record in _iter_jsonl_records(lines, label="scan report"):
        validate_envelope(record, SCAN_RECORD_SCHEMA)
        if record.get("document_schema") != SCAN_REPORT_SCHEMA:
            raise SchemaError("scan JSONL record has an invalid document_schema")
        record_type = record.get("record_type")
        _validate_jsonl_record_fields(
            record,
            record_type=record_type,
            fields_by_type=_SCAN_JSONL_RECORD_FIELDS,
            label="scan report",
            line_number=line_number,
        )
        if record_type == "header":
            if header:
                raise SchemaError("scan report contains more than one header")
            header = dict(record)
            roots = header.get("roots")
            if isinstance(roots, list) and len(roots) > MAX_DOCUMENT_ROOTS:
                raise SchemaError("scan report exceeds the {} roots limit".format(MAX_DOCUMENT_ROOTS))
        elif record_type == "group":
            if len(groups) >= MAX_SCAN_GROUPS:
                raise SchemaError("scan report JSONL exceeds the {} group limit".format(MAX_SCAN_GROUPS))
            group_payload = record.get("group", {})
            file_record_count += _group_file_record_count(group_payload)
            if file_record_count > MAX_SCAN_FILE_RECORDS:
                raise SchemaError(
                    "scan report JSONL exceeds the {} file-record limit".format(
                        MAX_SCAN_FILE_RECORDS,
                    )
                )
            groups.append(ScanGroup.from_dict(group_payload))
        elif record_type == "issue":
            if len(issues) >= MAX_SCAN_ISSUES:
                raise SchemaError("scan report JSONL exceeds the {} issue limit".format(MAX_SCAN_ISSUES))
            issues.append(ScanIssue.from_dict(record.get("issue", {})))
        elif record_type == "coverage":
            if len(coverage) >= MAX_SCAN_COVERAGE_RECORDS:
                raise SchemaError(
                    "scan report JSONL exceeds the {} coverage-record limit".format(MAX_SCAN_COVERAGE_RECORDS)
                )
            coverage.append(ScanCoverage.from_dict(record.get("coverage", {})))
        elif record_type == "summary":
            if summary is not None:
                raise SchemaError("scan report contains more than one summary")
            summary = ScanSummary.from_dict(record.get("summary", {}))
    if not header:
        raise SchemaError("scan JSONL report has no header")
    if summary is None:
        raise SchemaError("scan JSONL report has no summary")
    if not coverage:
        raise SchemaError("scan JSONL report has no coverage records")
    return ScanReport.from_dict(
        {
            "schema": SCAN_REPORT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "scan_id": header["scan_id"],
            "created_at": header["created_at"],
            "engine_version": header["engine_version"],
            "roots": header["roots"],
            "mode": header["mode"],
            "groups": [group.to_dict() for group in groups],
            "issues": [issue.to_dict() for issue in issues],
            "coverage": [item.to_dict() for item in coverage],
            "summary": summary.to_dict(),
        }
    )


def load_deletion_plan(stream: TextIO) -> DeletionPlan:
    reader = _BoundedLineReader(stream)
    first = _first_nonblank_line(reader, empty_message="deletion plan is empty")
    _first_line_number, first_line = first
    try:
        first_payload = _strict_json_loads(
            first_line,
            limits=SERVICE_DOCUMENT_JSON_LIMITS,
            label="deletion plan first line",
        )
    except SchemaError:
        raise
    except json.JSONDecodeError:
        payload = _read_json_document(reader, first_line, label="deletion plan")
    else:
        if _looks_like_jsonl_record(
            first_payload,
            record_schema=PLAN_RECORD_SCHEMA,
            document_schema=DELETION_PLAN_SCHEMA,
        ):
            reader.use_jsonl_limits()
            return _load_plan_jsonl(chain((first,), reader))
        payload = _finish_single_line_document(
            reader,
            first_payload,
            label="deletion plan",
        )
    if not isinstance(payload, Mapping):
        raise SchemaError("deletion plan must be an object")
    _bounded_array_length(
        payload,
        "roots",
        MAX_DOCUMENT_ROOTS,
        label="deletion plan",
    )
    _bounded_array_length(
        payload,
        "actions",
        MAX_PLAN_ACTIONS,
        label="deletion plan",
    )
    return DeletionPlan.from_dict(payload)


def _load_plan_jsonl(
    lines: Iterable[Tuple[int, str]],
) -> DeletionPlan:
    header: Dict[str, Any] = {}
    actions: List[PlanAction] = []
    saw_summary = False
    summary_actions = None
    for line_number, record in _iter_jsonl_records(lines, label="deletion plan"):
        validate_envelope(record, PLAN_RECORD_SCHEMA)
        if record.get("document_schema") != DELETION_PLAN_SCHEMA:
            raise SchemaError("plan JSONL record has an invalid document_schema")
        record_type = record.get("record_type")
        _validate_jsonl_record_fields(
            record,
            record_type=record_type,
            fields_by_type=_PLAN_JSONL_RECORD_FIELDS,
            label="deletion plan",
            line_number=line_number,
        )
        if record_type == "header":
            if header:
                raise SchemaError("deletion plan contains more than one header")
            header = dict(record)
            roots = header.get("roots")
            if isinstance(roots, list) and len(roots) > MAX_DOCUMENT_ROOTS:
                raise SchemaError("deletion plan exceeds the {} roots limit".format(MAX_DOCUMENT_ROOTS))
        elif record_type == "action":
            if len(actions) >= MAX_PLAN_ACTIONS:
                raise SchemaError("deletion plan JSONL exceeds the {} action limit".format(MAX_PLAN_ACTIONS))
            actions.append(PlanAction.from_dict(record.get("action", {})))
        elif record_type == "summary":
            if saw_summary:
                raise SchemaError("deletion plan contains more than one summary")
            saw_summary = True
            summary = record.get("summary")
            if (
                not isinstance(summary, Mapping)
                or set(summary) != {"actions"}
                or type(summary.get("actions")) is not int
                or summary["actions"] < 0
                or summary["actions"] > MAX_PLAN_ACTIONS
            ):
                raise SchemaError("plan JSONL summary must contain an integer actions count")
            summary_actions = summary["actions"]
    if not header:
        raise SchemaError("deletion plan JSONL has no header")
    if not saw_summary:
        raise SchemaError("deletion plan JSONL has no summary")
    if summary_actions != len(actions):
        raise SchemaError("plan JSONL summary action count does not match its action records")
    return DeletionPlan.from_dict(
        {
            "schema": DELETION_PLAN_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "plan_id": header.get("plan_id", ""),
            "created_at": header.get("created_at", ""),
            "engine_version": header.get("engine_version", ""),
            "source_scan_id": header.get("source_scan_id", ""),
            "roots": header.get("roots", []),
            "actions": [action.to_dict() for action in actions],
        }
    )
