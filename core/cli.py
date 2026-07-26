from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from typing import Any, Mapping, Optional, Sequence, TextIO

from core import __version__
from core.catalog_cli import (
    CATALOG_SCHEMAS,
    add_catalog_parser,
    run_catalog_command,
)
from core.dataset_cli import (
    DATASET_SCHEMAS,
    add_dataset_parser,
    run_dataset_command,
)
from core.quarantine import QuarantineError
from core.services import (
    DEFAULT_SCAN_MAX_FILES,
    DEFAULT_SCAN_MAX_GROUPS,
    DEFAULT_SCAN_MAX_ISSUES,
    DEFAULT_SCAN_MAX_SECONDS,
    ScanRequest,
    SchemaError,
    Services,
    VideoService,
)
from core.services.jsonio import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_LINES,
    MAX_JSONL_RECORDS,
    MAX_JSONL_TOTAL_BYTES,
    MAX_JSON_DOCUMENT_BYTES,
    MAX_PLAN_ACTIONS,
    MAX_SCAN_FILE_RECORDS,
    MAX_SCAN_GROUPS,
    load_deletion_plan,
    load_scan_report,
    write_deletion_plan,
    write_json,
    write_scan_report,
    write_video_library_report,
)
from core.services.models import (
    APPLY_REPORT_SCHEMA,
    DELETION_PLAN_SCHEMA,
    DOCTOR_REPORT_SCHEMA,
    PLAN_RECORD_SCHEMA,
    QUERY_REPORT_SCHEMA,
    QUARANTINE_ACTION_SCHEMA,
    QUARANTINE_LIST_SCHEMA,
    QUARANTINE_OPERATION_SCHEMA,
    SCAN_RECORD_SCHEMA,
    SCAN_REPORT_SCHEMA,
    VIDEO_ANALYSIS_SCHEMA,
    VIDEO_CAPABILITIES_SCHEMA,
    VIDEO_COMPARISON_SCHEMA,
    VIDEO_LIBRARY_GROUP_SCHEMA,
    VIDEO_LIBRARY_RECORD_SCHEMA,
    VIDEO_LIBRARY_SCAN_SCHEMA,
)
from core.services.schemas import get_schema
from core.video import VideoLibraryLimits
from core.visual_cli import (
    VISUAL_SCHEMAS,
    add_visual_parser,
    run_visual_command,
)

_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024
INPUT_LIMITS_HELP = (
    "Input/output limits: single JSON <= {json_mib} MiB; JSONL line <= "
    "{line_mib} MiB, total <= {total_gib} GiB, physical lines <= "
    "{lines}, records <= {records}; scan groups <= {groups}; scan "
    "file records <= {files}; plan actions <= {actions}."
).format(
    json_mib=MAX_JSON_DOCUMENT_BYTES // _MIB,
    line_mib=MAX_JSONL_LINE_BYTES // _MIB,
    total_gib=MAX_JSONL_TOTAL_BYTES // _GIB,
    lines=MAX_JSONL_LINES,
    records=MAX_JSONL_RECORDS,
    groups=MAX_SCAN_GROUPS,
    files=MAX_SCAN_FILE_RECORDS,
    actions=MAX_PLAN_ACTIONS,
)


class ExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    INPUT_ERROR = 3
    PARTIAL_SCAN = 4
    VERIFICATION_FAILED = 5
    VIDEO_PARTIAL = 6
    OPERATION_FAILED = 7
    INTERRUPTED = 130


SCHEMA_NAMES = {
    "scan-report": SCAN_REPORT_SCHEMA,
    "scan-record": SCAN_RECORD_SCHEMA,
    "deletion-plan": DELETION_PLAN_SCHEMA,
    "plan-record": PLAN_RECORD_SCHEMA,
    "apply-report": APPLY_REPORT_SCHEMA,
    "query-report": QUERY_REPORT_SCHEMA,
    "doctor-report": DOCTOR_REPORT_SCHEMA,
    "quarantine-operation": QUARANTINE_OPERATION_SCHEMA,
    "quarantine-list": QUARANTINE_LIST_SCHEMA,
    "quarantine-action": QUARANTINE_ACTION_SCHEMA,
    "video-capabilities": VIDEO_CAPABILITIES_SCHEMA,
    "video-analysis": VIDEO_ANALYSIS_SCHEMA,
    "video-comparison": VIDEO_COMPARISON_SCHEMA,
    "video-library-group": VIDEO_LIBRARY_GROUP_SCHEMA,
    "video-library-record": VIDEO_LIBRARY_RECORD_SCHEMA,
    "video-library-scan": VIDEO_LIBRARY_SCAN_SCHEMA,
}

AUXILIARY_SCHEMAS = {
    **DATASET_SCHEMAS,
    **CATALOG_SCHEMAS,
    **VISUAL_SCHEMAS,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dupeguru",
        description="Qt-free dupeGuru service CLI",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan",
        help="scan roots for verified exact duplicates",
        epilog=(
            "Reaching any resource limit emits a valid incomplete report "
            "that cannot be used to create a destructive plan."
        ),
    )
    scan_parser.add_argument("roots", nargs="+", help="directory roots")
    scan_parser.add_argument("--min-size", type=int, default=0, help="minimum file size in bytes")
    scan_parser.add_argument(
        "--big-file-size",
        type=int,
        default=0,
        help="use sample hashes as an additional candidate filter above this byte size",
    )
    scan_parser.add_argument("--no-recursive", action="store_true", help="do not recurse into subdirectories")
    scan_parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_SCAN_MAX_FILES,
        help="maximum discovered files retained (default: {})".format(DEFAULT_SCAN_MAX_FILES),
    )
    scan_parser.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_SCAN_MAX_ISSUES,
        help="maximum issue records retained (default: {})".format(DEFAULT_SCAN_MAX_ISSUES),
    )
    scan_parser.add_argument(
        "--max-groups",
        type=int,
        default=DEFAULT_SCAN_MAX_GROUPS,
        help="maximum verified groups retained (default: {})".format(DEFAULT_SCAN_MAX_GROUPS),
    )
    scan_parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_SCAN_MAX_SECONDS,
        help="maximum total scan seconds (default: {})".format(DEFAULT_SCAN_MAX_SECONDS),
    )
    scan_parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="jsonl",
        help=(
            "output format (default: jsonl); explicit JSON is emitted only " "when the bounded loader can read it back"
        ),
    )
    scan_parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print output (requires --format json)",
    )
    scan_parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")

    plan_parser = subparsers.add_parser(
        "plan",
        help="create a versioned deletion plan from a scan report",
        epilog=INPUT_LIMITS_HELP,
    )
    plan_parser.add_argument("report", help="scan report path, or - for stdin")
    plan_parser.add_argument(
        "--operation",
        choices=("quarantine",),
        default="quarantine",
        help="recoverable quarantine (the only supported exact-plan action)",
    )
    plan_parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="jsonl",
        help=(
            "output format (default: jsonl); explicit JSON is emitted only " "when the bounded loader can read it back"
        ),
    )
    plan_parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print output (requires --format json)",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="validate or apply a versioned deletion plan",
        epilog=INPUT_LIMITS_HELP,
    )
    apply_parser.add_argument("plan", help="deletion plan path, or - for stdin")
    apply_mode = apply_parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="request mutation; without this flag apply is always a dry-run",
    )
    apply_mode.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="explicitly request the default read-only validation mode",
    )
    apply_parser.set_defaults(execute=False)
    apply_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    apply_parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")

    query_parser = subparsers.add_parser(
        "query",
        help="query groups in a scan report",
        epilog=INPUT_LIMITS_HELP,
    )
    query_parser.add_argument("report", help="scan report path, or - for stdin")
    query_parser.add_argument("--group-id")
    query_parser.add_argument("--path")
    query_parser.add_argument("--digest")
    query_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    doctor_parser = subparsers.add_parser("doctor", help="report local service capabilities")
    doctor_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    quarantine_parser = subparsers.add_parser(
        "quarantine",
        help="list, restore, or permanently finalize persisted quarantine operations",
    )
    quarantine_subparsers = quarantine_parser.add_subparsers(dest="quarantine_command", required=True)
    quarantine_list_parser = quarantine_subparsers.add_parser("list", help="list persisted operations")
    quarantine_list_parser.add_argument("roots", nargs="+", help="original scan roots")
    quarantine_list_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    restore_parser = quarantine_subparsers.add_parser(
        "restore",
        help="validate or restore one staged target",
    )
    restore_parser.add_argument("operation_plan", help="persisted operation-plan JSON path")
    restore_mode = restore_parser.add_mutually_exclusive_group()
    restore_mode.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="perform the validated restore; without this flag the command is read-only",
    )
    restore_mode.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="explicitly request the default read-only restore preflight",
    )
    restore_parser.set_defaults(execute=False)
    restore_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    finalize_parser = quarantine_subparsers.add_parser(
        "finalize",
        help="validate or permanently finalize one staged target",
    )
    finalize_parser.add_argument("operation_plan", help="persisted operation-plan JSON path")
    finalize_mode = finalize_parser.add_mutually_exclusive_group()
    finalize_mode.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="perform permanent finalization; without this flag the command is read-only",
    )
    finalize_mode.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="explicitly request the default read-only finalization preflight",
    )
    finalize_parser.set_defaults(execute=False)
    finalize_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    video_parser = subparsers.add_parser(
        "video",
        help="inspect, analyze, or perceptually compare videos",
        description=(
            "Schema-versioned video JSON. Exit status 6 means the JSON result is valid "
            "but partial or capability-limited."
        ),
    )
    video_subparsers = video_parser.add_subparsers(dest="video_command", required=True)
    video_capabilities_parser = video_subparsers.add_parser(
        "capabilities",
        help="report FFprobe, FFmpeg, and Chromaprint availability",
    )
    video_capabilities_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    video_analyze_parser = video_subparsers.add_parser(
        "analyze",
        help="build or load a versioned video fingerprint artifact",
    )
    video_analyze_parser.add_argument("path", help="video file to analyze")
    video_analyze_parser.add_argument(
        "--artifact-in",
        "--cache-in",
        dest="artifact_in",
        help="load this strict artifact cache instead of running external tools",
    )
    video_analyze_parser.add_argument(
        "--artifact-out",
        "--cache-out",
        dest="artifact_out",
        help="atomically create this artifact cache; existing paths are never replaced",
    )
    video_analyze_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    video_scan_parser = video_subparsers.add_parser(
        "scan",
        help="scan video roots for bounded, review-only perceptual groups",
        description=(
            "Read-only video-library scan. Perceptual groups are never byte-exact "
            "proof and never authorize destructive actions."
        ),
    )
    video_scan_parser.add_argument("roots", nargs="+", help="video library roots")
    video_scan_parser.add_argument(
        "--cache",
        "--fingerprint-cache",
        dest="cache",
        help="SQLite fingerprint cache outside all input roots",
    )
    video_scan_parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="minimum reported perceptual relation score from 0 through 1 (default: 0.35)",
    )
    video_scan_parser.add_argument("--max-files", type=int, default=10_000)
    video_scan_parser.add_argument(
        "--max-candidate-assessments",
        type=int,
        default=100_000,
    )
    video_scan_parser.add_argument("--max-candidates", type=int, default=20_000)
    video_scan_parser.add_argument("--max-comparisons", type=int, default=2_000)
    video_scan_parser.add_argument(
        "--max-fingerprint-files",
        type=int,
        default=None,
        help="defaults to the smaller of 4000 or twice --max-comparisons",
    )
    video_scan_parser.add_argument("--max-groups", type=int, default=2_000)
    video_scan_parser.add_argument("--probe-timeout", type=float, default=20)
    video_scan_parser.add_argument(
        "--max-probe-output-bytes",
        type=int,
        default=4 * 1024 * 1024,
    )
    video_scan_parser.add_argument("--max-scan-seconds", type=float, default=4 * 60 * 60)
    video_scan_parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="jsonl",
        help=(
            "output format (default: jsonl); all output is preflighted "
            "against the exact-report loader byte and structure limits"
        ),
    )
    video_scan_parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output (requires --format json)",
    )
    video_scan_parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")
    video_compare_parser = video_subparsers.add_parser(
        "compare",
        help="compare perceptual fingerprints; this command never emits byte-exact proof",
    )
    video_compare_parser.add_argument("first", help="first video")
    video_compare_parser.add_argument("second", help="second video")
    video_compare_parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="minimum reported perceptual relation score from 0 through 1 (default: 0.35)",
    )
    video_compare_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")

    add_dataset_parser(subparsers)
    add_catalog_parser(subparsers)
    add_visual_parser(subparsers)

    schema_parser = subparsers.add_parser("schema", help="print a bundled JSON Schema")
    schema_parser.add_argument(
        "name",
        choices=tuple(sorted({*SCHEMA_NAMES, *AUXILIARY_SCHEMAS})),
    )
    schema_parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    return parser


def _progress_writer(stderr: TextIO, quiet: bool):
    def progress(stage: str, fields: Mapping[str, Any]) -> None:
        if quiet:
            return
        rendered = " ".join("{}={}".format(key, value) for key, value in sorted(fields.items()) if value is not None)
        stderr.write("[{}]{}\n".format(stage, " " + rendered if rendered else ""))
        stderr.flush()

    return progress


def _load_report(path: str, stdin: TextIO):
    if path == "-":
        return load_scan_report(stdin)
    with open(path, "r", encoding="utf-8") as stream:
        return load_scan_report(stream)


def _load_plan(path: str, stdin: TextIO):
    if path == "-":
        return load_deletion_plan(stdin)
    with open(path, "r", encoding="utf-8") as stream:
        return load_deletion_plan(stream)


def _video_exit_code(payload: Mapping[str, Any]) -> int:
    state = payload.get("state")
    if state == "complete":
        return int(ExitCode.OK)
    if state == "failed":
        return int(ExitCode.INPUT_ERROR)
    if state in {
        "partial",
        "partial_missing_tool",
        "partial_timeout",
        "partial_cancelled",
        "partial_resource_limit",
        "partial_tool_error",
    }:
        return int(ExitCode.VIDEO_PARTIAL)
    raise RuntimeError("video service returned an unsupported result state")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    services: Optional[Services] = None,
    video_service: Optional[VideoService] = None,
    visual_service_factory=None,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    service_collection = services or Services()
    args = build_parser().parse_args(argv)

    try:
        if args.command == "scan":
            request = ScanRequest(
                roots=tuple(args.roots),
                recursive=not args.no_recursive,
                min_size=args.min_size,
                big_file_size=args.big_file_size,
                max_files=args.max_files,
                max_issues=args.max_issues,
                max_groups=args.max_groups,
                max_seconds=args.max_seconds,
            )
            report = service_collection.scan.scan(
                request,
                progress=_progress_writer(error_stream, args.quiet),
            )
            write_scan_report(report, output_stream, output_format=args.format, pretty=args.pretty)
            return int(ExitCode.OK if report.summary.complete else ExitCode.PARTIAL_SCAN)

        if args.command == "plan":
            report = _load_report(args.report, input_stream)
            plan = service_collection.plan.create(
                report,
                operation=args.operation,
            )
            write_deletion_plan(plan, output_stream, output_format=args.format, pretty=args.pretty)
            return int(ExitCode.OK)

        if args.command == "apply":
            plan = _load_plan(args.plan, input_stream)
            dry_run = not args.execute
            report = service_collection.apply.apply(
                plan,
                dry_run=dry_run,
                progress=_progress_writer(error_stream, args.quiet),
            )
            write_json(report.to_dict(), output_stream, pretty=args.pretty)
            statuses = {result.status for result in report.results}
            if "stale" in statuses:
                return int(ExitCode.VERIFICATION_FAILED)
            if statuses.intersection({"failed"}):
                return int(ExitCode.OPERATION_FAILED)
            return int(ExitCode.OK)

        if args.command == "query":
            report = _load_report(args.report, input_stream)
            result = service_collection.query.query(
                report,
                group_id=args.group_id,
                path=args.path,
                digest=args.digest,
            )
            write_json(result, output_stream, pretty=args.pretty)
            return int(ExitCode.OK)

        if args.command == "doctor":
            write_json(service_collection.doctor.inspect(), output_stream, pretty=args.pretty)
            return int(ExitCode.OK)

        if args.command == "quarantine":
            if args.quarantine_command == "list":
                payload = service_collection.quarantine.list(args.roots)
                write_json(payload, output_stream, pretty=args.pretty)
                return int(ExitCode.OK)
            if args.quarantine_command == "restore":
                payload = service_collection.quarantine.restore(
                    args.operation_plan,
                    execute=args.execute,
                )
            else:
                payload = service_collection.quarantine.finalize(
                    args.operation_plan,
                    execute=args.execute,
                )
            write_json(payload, output_stream, pretty=args.pretty)
            if payload["result"]["failure_code"] == "none":
                return int(ExitCode.OK)
            return int(ExitCode.OPERATION_FAILED)

        if args.command == "video":
            video = video_service or service_collection.video
            if args.video_command == "capabilities":
                payload = video.inspect_capabilities()
            elif args.video_command == "analyze":
                payload = video.analyze(
                    args.path,
                    artifact_input=args.artifact_in,
                    artifact_output=args.artifact_out,
                )
            elif args.video_command == "scan":
                if args.pretty and args.format != "json":
                    raise ValueError("--pretty requires --format json for video library scans")
                maximum_fingerprint_files = args.max_fingerprint_files
                if maximum_fingerprint_files is None:
                    maximum_fingerprint_files = min(
                        VideoLibraryLimits().maximum_fingerprint_files,
                        2 * args.max_comparisons,
                    )
                limits = VideoLibraryLimits(
                    maximum_files=args.max_files,
                    maximum_candidate_assessments=args.max_candidate_assessments,
                    maximum_candidates=args.max_candidates,
                    maximum_comparisons=args.max_comparisons,
                    maximum_fingerprint_files=maximum_fingerprint_files,
                    maximum_groups=args.max_groups,
                    probe_timeout_seconds=args.probe_timeout,
                    maximum_probe_output_bytes=args.max_probe_output_bytes,
                    maximum_scan_seconds=args.max_scan_seconds,
                )
                payload = video.scan(
                    args.roots,
                    cache_path=args.cache,
                    threshold=args.threshold,
                    limits=limits,
                    progress=_progress_writer(error_stream, args.quiet),
                )
                exit_code = _video_exit_code(payload)
                write_video_library_report(
                    payload,
                    output_stream,
                    output_format=args.format,
                    pretty=args.pretty,
                )
                return exit_code
            else:
                payload = video.compare(
                    args.first,
                    args.second,
                    threshold=args.threshold,
                )
            exit_code = _video_exit_code(payload)
            write_json(payload, output_stream, pretty=args.pretty)
            return exit_code

        if args.command == "dataset":
            return run_dataset_command(
                args,
                input_stream,
                output_stream,
                error_stream,
            )

        if args.command == "catalog":
            return run_catalog_command(
                args,
                input_stream,
                output_stream,
                error_stream,
            )

        if args.command == "visual":
            options = {}
            if visual_service_factory is not None:
                options["service_factory"] = visual_service_factory
            return run_visual_command(
                args,
                output_stream,
                error_stream,
                **options,
            )

        if args.command == "schema":
            schema = AUXILIARY_SCHEMAS.get(args.name)
            if schema is None:
                schema = get_schema(SCHEMA_NAMES[args.name])
            write_json(schema, output_stream, pretty=args.pretty)
            return int(ExitCode.OK)

        error_stream.write("Unknown command: {}\n".format(args.command))
        return int(ExitCode.USAGE)
    except KeyboardInterrupt:
        error_stream.write("Interrupted\n")
        return int(ExitCode.INTERRUPTED)
    except (OSError, QuarantineError, SchemaError, ValueError) as error:
        error_stream.write("{}: {}\n".format(type(error).__name__, error))
        return int(ExitCode.INPUT_ERROR)
    except Exception as error:
        error_stream.write("InternalError: {}\n".format(error))
        return int(ExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    sys.exit(main())
