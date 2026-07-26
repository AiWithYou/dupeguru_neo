# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded, read-only CLI boundary for visual image search."""

from __future__ import annotations

import inspect
import os
import stat
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, TextIO

from core.pe.image_features import DEFAULT_MAX_DECODE_PIXELS
from core.safe_walk import is_reparse_point
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus
from core.services.jsonio import (
    _bounded_json_document,
    _write_validated_jsonl_output,
    json_line,
)
from core.services.models import SchemaError
from core.visual_service import (
    VISUAL_ARTIFACT_SCHEMA,
    VISUAL_ARTIFACT_SCHEMA_VERSION,
    VISUAL_REPORT_SCHEMA,
    VISUAL_REPORT_SCHEMA_VERSION,
    VisualRelation,
    VisualScanConfig,
    VisualService,
    VisualServiceError,
)

VISUAL_RECORD_SCHEMA = "dupeguru.visual-record"
VISUAL_RECORD_SCHEMA_VERSION = 1

VISUAL_OK = 0
VISUAL_PARTIAL = 4

DEFAULT_MAX_IMAGES = 250_000
DEFAULT_MAX_CANDIDATE_PAIRS = 250_000
DEFAULT_MAX_MATCHES = 50_000
DEFAULT_MAX_SECONDS = 4 * 60 * 60


def add_visual_parser(subparsers) -> None:
    visual_parser = subparsers.add_parser(
        "visual",
        help="run bounded, review-only visual image search",
        description=(
            "Read-only visual similarity search. Its evidence is never byte-exact "
            "proof and never authorizes destructive actions."
        ),
    )
    commands = visual_parser.add_subparsers(
        dest="visual_command",
        required=True,
    )
    scan = commands.add_parser(
        "scan",
        help="scan image roots for visual similarity",
    )
    scan.add_argument("roots", nargs="+", help="image library roots")
    _add_common_arguments(scan)

    query = commands.add_parser(
        "query",
        help="find images visually related to one reference image",
    )
    query.add_argument("reference", help="reference image")
    query.add_argument("roots", nargs="+", help="image library roots")
    _add_common_arguments(query)


def _add_common_arguments(parser) -> None:
    parser.add_argument(
        "--cache",
        help="SQLite feature cache outside every input root; omit for an in-memory cache",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=int,
        default=80,
        help="minimum 15x15 block similarity for a similar relation (0-100)",
    )
    parser.add_argument(
        "--phash-radius",
        type=int,
        default=8,
        help="maximum 64-bit perceptual-hash Hamming distance (0-64)",
    )
    parser.add_argument(
        "--dhash-distance",
        type=int,
        default=24,
        help="conservative 64-bit dHash filter distance (0-64)",
    )
    parser.add_argument(
        "--color-histogram-distance",
        type=float,
        default=0.55,
        help="conservative normalized color-histogram filter distance (0-1)",
    )
    parser.add_argument("--match-scaled", action="store_true")
    parser.add_argument("--match-rotated", action="store_true")
    parser.add_argument(
        "--no-crop-candidates",
        action="store_true",
        help="disable bounded center/content tile crop candidates",
    )
    parser.add_argument(
        "--similar-only",
        action="store_true",
        help="omit below-threshold related evidence",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=DEFAULT_MAX_IMAGES,
        help="hard upper bound for analyzed images",
    )
    parser.add_argument(
        "--max-candidate-pairs",
        type=int,
        default=DEFAULT_MAX_CANDIDATE_PAIRS,
        help="hard upper bound for refined visual candidate pairs",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=DEFAULT_MAX_MATCHES,
        help="hard upper bound for emitted visual relations",
    )
    parser.add_argument(
        "--max-decode-pixels",
        type=int,
        default=DEFAULT_MAX_DECODE_PIXELS,
        help="maximum decoded pixels per image",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="hard upper bound for total visual scan time",
    )
    parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="jsonl",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output (requires --format json)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")


def run_visual_command(
    args,
    stdout: TextIO,
    stderr: TextIO,
    *,
    service_factory=VisualService,
) -> int:
    roots = _normalize_roots(args.roots)
    cache_path = _validate_cache_path(args.cache, roots)
    limits = _validate_limits(args)
    config = _build_config(args, limits)
    if args.pretty and args.format != "json":
        raise ValueError("--pretty requires --format json for visual search")

    service_options: Dict[str, Any] = {
        "cache_path": None if cache_path is None else str(cache_path),
        "max_decode_pixels": limits["max_decode_pixels"],
    }

    service = service_factory(**service_options)
    _require_service_contract(service)
    if not args.quiet:
        stderr.write(
            "[visual-start] command={} roots={} max_images={} "
            "max_candidate_pairs={} max_matches={}\n".format(
                args.visual_command,
                len(roots),
                limits["max_images"],
                limits["max_candidate_pairs"],
                limits["max_matches"],
            )
        )
        stderr.flush()

    try:
        if args.visual_command == "scan":
            report = _call_scan(service, roots, config)
        else:
            report = _call_query(
                service,
                args.reference,
                roots,
                config,
            )
    except VisualServiceError as error:
        raise ValueError(str(error)) from error
    report = _limit_matches(report, limits["max_matches"])
    payload = report.to_dict()
    # Coverage completeness must never accidentally grant a destructive
    # capability to perceptual evidence.
    payload["scan_receipt"]["allows_destructive_actions"] = False
    _write_visual_report(
        payload,
        stdout,
        output_format=args.format,
        pretty=args.pretty,
    )
    complete = bool(payload["scan_receipt"]["complete"])
    if not args.quiet:
        stderr.write(
            "[visual-complete] status={} assets={} evidence={}\n".format(
                payload["scan_receipt"]["status"],
                len(payload["assets"]),
                len(payload["evidence"]),
            )
        )
        stderr.flush()
    return VISUAL_OK if complete else VISUAL_PARTIAL


def _call_scan(service, roots, config):
    return service.scan_roots(
        roots,
        config=config,
        cancel_check=None,
        directory_pruner=None,
        file_filter=None,
    )


def _call_query(service, reference, roots, config):
    return service.query_reference(
        reference,
        roots=roots,
        config=config,
        cancel_check=None,
        directory_pruner=None,
        file_filter=None,
    )


def _validate_limits(args) -> Dict[str, Any]:
    result = {
        "max_images": args.max_images,
        "max_candidate_pairs": args.max_candidate_pairs,
        "max_matches": args.max_matches,
        "max_decode_pixels": args.max_decode_pixels,
        "max_seconds": args.max_seconds,
    }
    for name, value in result.items():
        if name == "max_seconds":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError("max-seconds must be a positive number")
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("{} must be a positive integer".format(name.replace("_", "-")))
    if result["max_matches"] > result["max_candidate_pairs"]:
        raise ValueError("max-matches cannot exceed max-candidate-pairs")
    return result


def _build_config(args, limits):
    supported = {item.name for item in fields(VisualScanConfig)}
    values = {
        "similarity_threshold": args.similarity_threshold,
        "phash_radius": args.phash_radius,
        "dhash_distance": args.dhash_distance,
        "color_histogram_distance": args.color_histogram_distance,
        "match_scaled": args.match_scaled,
        "match_rotated": args.match_rotated,
        "match_crops": not args.no_crop_candidates,
        "include_related": not args.similar_only,
        "dry_run": True,
    }
    bounded_values = {
        "max_images": limits["max_images"],
        "max_candidate_pairs": limits["max_candidate_pairs"],
        "max_matches": limits["max_matches"],
        "max_seconds": limits["max_seconds"],
    }
    missing = sorted(set(bounded_values) - supported)
    if missing:
        raise ValueError("visual service lacks required bounded configuration: {}".format(", ".join(missing)))
    values.update(bounded_values)
    return VisualScanConfig(**values)


def _require_service_contract(service) -> None:
    required_methods = {
        "scan_roots": {
            "roots",
            "config",
            "cancel_check",
            "directory_pruner",
            "file_filter",
        },
        "query_reference": {
            "reference",
            "roots",
            "config",
            "cancel_check",
            "directory_pruner",
            "file_filter",
        },
    }
    for method_name, required in required_methods.items():
        method = getattr(service, method_name, None)
        if method is None:
            raise ValueError("visual service lacks required method: {}".format(method_name))
        supported = set(inspect.signature(method).parameters)
        missing = sorted(required - supported)
        if missing:
            raise ValueError(
                "visual service {} lacks required parameters: {}".format(
                    method_name,
                    ", ".join(missing),
                )
            )


def _mark_resource_limited(report, path, message):
    receipt = report.scan_receipt
    issue = ScanIssue("resource_limit", message, path)
    failed = receipt.failed + 1
    discovered = max(
        receipt.discovered + 1,
        receipt.analyzed + receipt.skipped + failed,
    )
    updated = ScanReceipt(
        scan_id=receipt.scan_id,
        status=ScanStatus.RESOURCE_LIMIT,
        discovered=discovered,
        analyzed=receipt.analyzed,
        skipped=receipt.skipped,
        failed=failed,
        started_at_ns=receipt.started_at_ns,
        finished_at_ns=receipt.finished_at_ns,
        issues=receipt.issues + (issue,),
    )
    return replace(report, scan_receipt=updated)


def _limit_matches(report, maximum_matches):
    if len(report.evidence) <= maximum_matches:
        return report
    evidence = tuple(report.evidence[:maximum_matches])
    similar = sum(item.relation is VisualRelation.SIMILAR for item in evidence)
    transformed = sum(item.relation is VisualRelation.TRANSFORMED for item in evidence)
    crop_candidates = sum(item.relation is VisualRelation.CROP_CANDIDATE for item in evidence)
    related = sum(item.relation is VisualRelation.RELATED for item in evidence)
    stats = replace(
        report.candidate_stats,
        similar_count=similar,
        transformed_count=transformed,
        crop_candidate_count=crop_candidates,
        related_count=related,
    )
    limited = replace(
        report,
        evidence=evidence,
        candidate_stats=stats,
    )
    return _mark_resource_limited(
        limited,
        "",
        "visual relation count exceeded max-matches",
    )


def _normalize_roots(roots: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(
        sorted(
            {os.path.abspath(os.path.expanduser(os.fspath(root))) for root in roots},
            key=os.path.normcase,
        )
    )
    if not normalized:
        raise ValueError("visual search requires at least one root")
    canonical = tuple(Path(root).resolve(strict=False) for root in normalized)
    for index, root in enumerate(canonical):
        for other in canonical[:index] + canonical[index + 1 :]:
            if _is_within(root, other):
                raise ValueError("visual roots must not overlap")
    return normalized


def _validate_cache_path(
    raw_path: Optional[str],
    roots: Sequence[str],
) -> Optional[Path]:
    if raw_path is None:
        return None
    candidate = Path(os.path.abspath(os.path.expanduser(raw_path)))
    if any(_is_within(candidate, Path(root)) for root in roots):
        raise ValueError("visual cache must be outside every input root")
    parent = candidate.parent
    parent_stat = os.stat(parent, follow_symlinks=False)
    if stat.S_ISLNK(parent_stat.st_mode) or is_reparse_point(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("visual cache parent must be a plain directory")
    resolved_parent = parent.resolve(strict=True)
    if os.path.normcase(str(parent)) != os.path.normcase(str(resolved_parent)):
        raise ValueError("visual cache parent traverses a link or reparse point")
    canonical_candidate = resolved_parent.joinpath(candidate.name)
    canonical_roots = tuple(Path(root).resolve(strict=False) for root in roots)
    if any(_is_within(canonical_candidate, root) for root in canonical_roots):
        raise ValueError("visual cache resolves inside an input root through a canonical alias")
    if os.path.lexists(candidate):
        file_stat = os.stat(candidate, follow_symlinks=False)
        if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("visual cache must be a plain regular file")
        if int(getattr(file_stat, "st_nlink", 1)) != 1:
            raise ValueError("visual cache must not be a hard-linked file")
    return candidate


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate_key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        root_key = os.path.normcase(os.path.abspath(os.fspath(root)))
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except ValueError:
        return False


def _write_visual_report(
    report: Mapping[str, Any],
    stream: TextIO,
    *,
    output_format: str,
    pretty: bool,
) -> None:
    if output_format == "json":
        stream.write(
            _bounded_json_document(
                report,
                pretty=pretty,
                label="visual report",
            )
        )
        return
    if output_format != "jsonl":
        raise ValueError("unknown visual output format")
    if pretty:
        raise ValueError("pretty output is only available with JSON")
    _write_validated_jsonl_output(
        _iter_visual_jsonl_lines(report),
        stream,
        label="visual report",
    )


def _iter_visual_jsonl_lines(report: Mapping[str, Any]) -> Iterator[str]:
    """Render deterministic JSONL records, translating unsafe output failures."""

    record_number = 0
    try:
        for record_number, record in enumerate(iter_visual_jsonl(report), 1):
            line = json_line(record)
            # Reject unpaired surrogates before validation or a real UTF-8 stdout
            # can fail after earlier records have already been emitted.
            line.encode("utf-8")
            yield line
    except SchemaError:
        raise
    except (
        KeyError,
        MemoryError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        OverflowError,
    ) as error:
        location = "record {}".format(record_number) if record_number else "report header"
        raise SchemaError(
            "visual report JSONL output could not render {} safely: {}".format(
                location,
                error,
            )
        ) from error


def iter_visual_jsonl(report: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    if report.get("schema") != VISUAL_REPORT_SCHEMA:
        raise ValueError("visual report has an invalid schema")
    if report.get("schema_version") != VISUAL_REPORT_SCHEMA_VERSION:
        raise ValueError("visual report has an unsupported schema version")
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or not report_id:
        raise ValueError("visual report requires a report_id")
    envelope = {
        "schema": VISUAL_RECORD_SCHEMA,
        "schema_version": VISUAL_RECORD_SCHEMA_VERSION,
        "document_schema": VISUAL_REPORT_SCHEMA,
        "report_id": report_id,
    }

    def record(record_type, payload):
        return {
            **envelope,
            "record_type": record_type,
            "payload": payload,
        }

    header = {
        key: report[key]
        for key in (
            "report_kind",
            "created_at_ns",
            "roots",
            "reference_asset_id",
            "config",
            "safety",
        )
    }
    yield record("header", header)
    for asset in report["assets"]:
        yield record("asset", asset)
    for artifact in report["artifacts"]:
        yield record("artifact", artifact)
    for evidence in report["evidence"]:
        yield record("evidence", evidence)
    for issue in report["scan_receipt"]["issues"]:
        yield record("issue", issue)
    yield record("receipt", report["scan_receipt"])
    yield record(
        "summary",
        {
            "candidate_stats": report["candidate_stats"],
            "assets": len(report["assets"]),
            "artifacts": len(report["artifacts"]),
            "evidence": len(report["evidence"]),
            "issues": len(report["scan_receipt"]["issues"]),
            "complete": report["scan_receipt"]["complete"],
        },
    )


def _object_schema(required, properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
    }


def _visual_report_schema() -> Mapping[str, Any]:
    identity = _object_schema(
        (
            "namespace",
            "capability",
            "confidence",
            "volume_id",
            "file_id_kind",
            "file_id",
        ),
        {
            "namespace": {"type": "string", "minLength": 1},
            "capability": {"type": "string", "minLength": 1},
            "confidence": {"type": "integer", "minimum": 0},
            "volume_id": {"type": "integer", "minimum": 0},
            "file_id_kind": {"enum": ["integer", "bytes"]},
            "file_id": {"type": "string", "minLength": 1},
        },
    )
    asset = _object_schema(
        (
            "asset_id",
            "path",
            "root",
            "size",
            "mtime_ns",
            "generation_token",
            "identity",
        ),
        {
            "asset_id": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1},
            "root": {"type": "string"},
            "size": {"type": "integer", "minimum": 0},
            "mtime_ns": {"type": "integer", "minimum": 0},
            "generation_token": {
                "type": "string",
                "pattern": "^[0-9a-f]+$",
                "maxLength": 512,
            },
            "identity": identity,
        },
    )
    safety = _object_schema(
        (
            "source_read_only",
            "dry_run",
            "verified_exact_evidence",
            "destructive_actions_allowed",
            "allowed_relations",
        ),
        {
            "source_read_only": {"const": True},
            "dry_run": {"const": True},
            "verified_exact_evidence": {"const": False},
            "destructive_actions_allowed": {"const": False},
            "allowed_relations": {
                "type": "array",
                "items": {
                    "enum": [
                        "similar",
                        "transformed",
                        "crop_candidate",
                        "related",
                    ]
                },
                "uniqueItems": True,
            },
        },
    )
    config_properties = {
        "similarity_threshold": {"type": "integer", "minimum": 0, "maximum": 100},
        "phash_radius": {"type": "integer", "minimum": 0, "maximum": 64},
        "dhash_distance": {"type": "integer", "minimum": 0, "maximum": 64},
        "color_histogram_distance": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "match_scaled": {"type": "boolean"},
        "match_rotated": {"type": "boolean"},
        "match_crops": {"type": "boolean"},
        "include_related": {"type": "boolean"},
        "dry_run": {"const": True},
        "source_read_only": {"const": True},
        "max_images": {"type": "integer", "minimum": 1},
        "max_candidate_pairs": {"type": "integer", "minimum": 1},
        "max_matches": {"type": "integer", "minimum": 1},
        "max_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
        },
    }
    config = _object_schema(tuple(config_properties), config_properties)
    issue = _object_schema(
        ("code", "message", "path"),
        {
            "code": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
            "path": {"type": "string"},
        },
    )
    receipt = _object_schema(
        (
            "scan_id",
            "status",
            "complete",
            "allows_destructive_actions",
            "discovered",
            "analyzed",
            "skipped",
            "failed",
            "started_at_ns",
            "finished_at_ns",
            "issues",
        ),
        {
            "scan_id": {"type": "string", "minLength": 1},
            "status": {
                "enum": [
                    "complete",
                    "complete_with_skips",
                    "cancelled",
                    "failed",
                    "resource_limit",
                ]
            },
            "complete": {"type": "boolean"},
            "allows_destructive_actions": {"const": False},
            "discovered": {"type": "integer", "minimum": 0},
            "analyzed": {"type": "integer", "minimum": 0},
            "skipped": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
            "started_at_ns": {"type": "integer", "minimum": 0},
            "finished_at_ns": {"type": "integer", "minimum": 0},
            "issues": {"type": "array", "items": issue},
        },
    )
    stats = _object_schema(
        (
            "indexed_images",
            "possible_pairs",
            "candidate_pairs",
            "refined_pairs",
            "similar_count",
            "transformed_count",
            "crop_candidate_count",
            "related_count",
            "phash_radius",
            "reduction_ratio",
        ),
        {
            "indexed_images": {"type": "integer", "minimum": 0},
            "possible_pairs": {"type": "integer", "minimum": 0},
            "candidate_pairs": {"type": "integer", "minimum": 0},
            "refined_pairs": {"type": "integer", "minimum": 0},
            "similar_count": {"type": "integer", "minimum": 0},
            "transformed_count": {"type": "integer", "minimum": 0},
            "crop_candidate_count": {"type": "integer", "minimum": 0},
            "related_count": {"type": "integer", "minimum": 0},
            "phash_radius": {"type": "integer", "minimum": 0, "maximum": 64},
            "reduction_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        },
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:visual-report:{}".format(VISUAL_REPORT_SCHEMA_VERSION),
        "title": "dupeGuru Neo read-only visual search report",
        **_object_schema(
            (
                "schema",
                "schema_version",
                "report_id",
                "report_kind",
                "created_at_ns",
                "roots",
                "reference_asset_id",
                "config",
                "assets",
                "artifacts",
                "evidence",
                "candidate_stats",
                "scan_receipt",
                "safety",
            ),
            {
                "schema": {"const": VISUAL_REPORT_SCHEMA},
                "schema_version": {"const": VISUAL_REPORT_SCHEMA_VERSION},
                "report_id": {"type": "string", "minLength": 1},
                "report_kind": {"enum": ["visual_scan", "visual_query"]},
                "created_at_ns": {"type": "integer", "minimum": 0},
                "roots": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "reference_asset_id": {"type": ["string", "null"]},
                "config": config,
                "assets": {"type": "array", "items": asset},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "schema",
                            "schema_version",
                            "asset_id",
                            "feature",
                            "safety",
                        ],
                        "properties": {
                            "schema": {"const": VISUAL_ARTIFACT_SCHEMA},
                            "schema_version": {"const": VISUAL_ARTIFACT_SCHEMA_VERSION},
                            "asset_id": {"type": "string", "minLength": 1},
                            "feature": _object_schema(
                                (
                                    "algorithm",
                                    "algorithm_version",
                                    "feature_version",
                                    "parameters_hash",
                                    "block_count_per_side",
                                    "dimensions",
                                    "frame_count",
                                    "phashes",
                                    "dhashes",
                                    "color_histogram",
                                    "tile_fingerprints",
                                    "quality",
                                    "thumbnail_key",
                                    "cache_record_id",
                                    "block_storage",
                                ),
                                {
                                    "algorithm": {"type": "string", "minLength": 1},
                                    "algorithm_version": {"type": "string", "minLength": 1},
                                    "feature_version": {"type": "string", "minLength": 1},
                                    "parameters_hash": {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{64}$",
                                    },
                                    "block_count_per_side": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                    "dimensions": {
                                        "type": "array",
                                        "minItems": 2,
                                        "maxItems": 2,
                                        "items": {
                                            "type": "integer",
                                            "minimum": 1,
                                        },
                                    },
                                    "frame_count": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                    "phashes": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 8,
                                        "items": {
                                            "type": "string",
                                            "pattern": "^[0-9a-f]{16}$",
                                        },
                                    },
                                    "dhashes": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 8,
                                        "items": {
                                            "type": "string",
                                            "pattern": "^[0-9a-f]{16}$",
                                        },
                                    },
                                    "color_histogram": {
                                        "type": "array",
                                        "minItems": 64,
                                        "maxItems": 64,
                                        "items": {
                                            "type": "integer",
                                            "minimum": 0,
                                        },
                                    },
                                    "tile_fingerprints": {
                                        "type": "array",
                                        "maxItems": 4,
                                        "items": _object_schema(
                                            ("kind", "phash", "dhash", "box"),
                                            {
                                                "kind": {
                                                    "enum": [
                                                        "center_90",
                                                        "center_75",
                                                        "center_50",
                                                        "content",
                                                    ]
                                                },
                                                "phash": {
                                                    "type": "string",
                                                    "pattern": "^[0-9a-f]{16}$",
                                                },
                                                "dhash": {
                                                    "type": "string",
                                                    "pattern": "^[0-9a-f]{16}$",
                                                },
                                                "box": {
                                                    "type": "array",
                                                    "minItems": 4,
                                                    "maxItems": 4,
                                                    "items": {
                                                        "type": "integer",
                                                        "minimum": 0,
                                                        "maximum": 10_000,
                                                    },
                                                },
                                            },
                                        ),
                                    },
                                    "quality": _object_schema(
                                        (
                                            "bit_depth",
                                            "exif_count",
                                            "metadata_count",
                                            "jpeg_artifact_score",
                                        ),
                                        {
                                            "bit_depth": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "exif_count": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "metadata_count": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "jpeg_artifact_score": {
                                                "type": "number",
                                                "minimum": 0,
                                                "maximum": 1,
                                            },
                                        },
                                    ),
                                    "thumbnail_key": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "cache_record_id": {
                                        "type": [
                                            "integer",
                                            "null",
                                        ],
                                        "minimum": 1,
                                    },
                                    "block_storage": {"const": "sqlite"},
                                },
                            ),
                            "safety": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "verification_level",
                                    "verified_exact",
                                    "destructive_actions_allowed",
                                ],
                                "properties": {
                                    "verification_level": {"const": "candidate"},
                                    "verified_exact": {"const": False},
                                    "destructive_actions_allowed": {"const": False},
                                },
                            },
                        },
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "evidence_id",
                            "first_id",
                            "second_id",
                            "relation",
                            "score",
                            "algorithm",
                            "algorithm_version",
                            "metrics",
                            "safety",
                        ],
                        "properties": {
                            "evidence_id": {"type": "string", "minLength": 1},
                            "first_id": {"type": "string", "minLength": 1},
                            "second_id": {"type": "string", "minLength": 1},
                            "relation": {
                                "enum": [
                                    "similar",
                                    "transformed",
                                    "crop_candidate",
                                    "related",
                                ]
                            },
                            "score": {"type": "number", "minimum": 0, "maximum": 1},
                            "algorithm": {"type": "string", "minLength": 1},
                            "algorithm_version": {"type": "string", "minLength": 1},
                            "metrics": _object_schema(
                                (
                                    "block_similarity",
                                    "phash_distance",
                                    "dhash_distance",
                                    "color_histogram_distance",
                                    "first_fingerprint_kind",
                                    "second_fingerprint_kind",
                                    "first_fingerprint_box",
                                    "second_fingerprint_box",
                                    "crop_verification",
                                    "transformation_kind",
                                    "phash_orientation",
                                    "block_orientation",
                                    "similarity_threshold",
                                    "phash_radius",
                                ),
                                {
                                    "block_similarity": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "phash_distance": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 64,
                                    },
                                    "dhash_distance": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 64,
                                    },
                                    "color_histogram_distance": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 1,
                                    },
                                    "first_fingerprint_kind": {
                                        "enum": [
                                            "whole",
                                            "center_90",
                                            "center_75",
                                            "center_50",
                                            "content",
                                        ]
                                    },
                                    "second_fingerprint_kind": {
                                        "enum": [
                                            "whole",
                                            "center_90",
                                            "center_75",
                                            "center_50",
                                            "content",
                                        ]
                                    },
                                    "first_fingerprint_box": {
                                        "type": "array",
                                        "minItems": 4,
                                        "maxItems": 4,
                                        "items": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 10_000,
                                        },
                                    },
                                    "second_fingerprint_box": {
                                        "type": "array",
                                        "minItems": 4,
                                        "maxItems": 4,
                                        "items": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 10_000,
                                        },
                                    },
                                    "crop_verification": {
                                        "enum": [
                                            "not_applicable",
                                            "bounded_fingerprint_candidate",
                                        ]
                                    },
                                    "transformation_kind": {
                                        "enum": [
                                            "none",
                                            "orientation",
                                            "scaled_or_resized",
                                        ]
                                    },
                                    "phash_orientation": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 7,
                                    },
                                    "block_orientation": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 7,
                                    },
                                    "similarity_threshold": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "phash_radius": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 64,
                                    },
                                },
                            ),
                            "safety": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "verified_exact",
                                    "destructive_actions_allowed",
                                ],
                                "properties": {
                                    "verified_exact": {"const": False},
                                    "destructive_actions_allowed": {"const": False},
                                },
                            },
                        },
                    },
                },
                "candidate_stats": stats,
                "scan_receipt": receipt,
                "safety": safety,
            },
        ),
    }


def _visual_record_schema() -> Mapping[str, Any]:
    report_properties = _visual_report_schema()["properties"]
    header = _object_schema(
        (
            "report_kind",
            "created_at_ns",
            "roots",
            "reference_asset_id",
            "config",
            "safety",
        ),
        {
            key: report_properties[key]
            for key in (
                "report_kind",
                "created_at_ns",
                "roots",
                "reference_asset_id",
                "config",
                "safety",
            )
        },
    )
    receipt = report_properties["scan_receipt"]
    payloads = {
        "header": header,
        "asset": report_properties["assets"]["items"],
        "artifact": report_properties["artifacts"]["items"],
        "evidence": report_properties["evidence"]["items"],
        "issue": receipt["properties"]["issues"]["items"],
        "receipt": receipt,
        "summary": _object_schema(
            (
                "candidate_stats",
                "assets",
                "artifacts",
                "evidence",
                "issues",
                "complete",
            ),
            {
                "candidate_stats": report_properties["candidate_stats"],
                "assets": {"type": "integer", "minimum": 0},
                "artifacts": {"type": "integer", "minimum": 0},
                "evidence": {"type": "integer", "minimum": 0},
                "issues": {"type": "integer", "minimum": 0},
                "complete": {"type": "boolean"},
            },
        ),
    }
    document = _object_schema(
        (
            "schema",
            "schema_version",
            "document_schema",
            "report_id",
            "record_type",
            "payload",
        ),
        {
            "schema": {"const": VISUAL_RECORD_SCHEMA},
            "schema_version": {"const": VISUAL_RECORD_SCHEMA_VERSION},
            "document_schema": {"const": VISUAL_REPORT_SCHEMA},
            "report_id": {"type": "string", "minLength": 1},
            "record_type": {"enum": list(payloads)},
            "payload": {"type": "object"},
        },
    )
    document["oneOf"] = [
        {
            "properties": {
                "record_type": {"const": record_type},
                "payload": payload,
            }
        }
        for record_type, payload in payloads.items()
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:visual-record:1",
        "title": "dupeGuru Neo visual JSONL record",
        **document,
    }


VISUAL_SCHEMAS = {
    "visual-report": _visual_report_schema(),
    "visual-record": _visual_record_schema(),
}


__all__ = [
    "DEFAULT_MAX_CANDIDATE_PAIRS",
    "DEFAULT_MAX_IMAGES",
    "DEFAULT_MAX_MATCHES",
    "DEFAULT_MAX_SECONDS",
    "VISUAL_RECORD_SCHEMA",
    "VISUAL_SCHEMAS",
    "add_visual_parser",
    "iter_visual_jsonl",
    "run_visual_command",
]
