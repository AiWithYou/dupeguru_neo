# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Argparse registration and dispatch for the standalone dataset command family."""

from __future__ import annotations

import argparse
import json
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TextIO

from core.dataset_discovery import DatasetDiscoveryService, DatasetRootRequest
from core.dataset_executor import (
    MAX_EXECUTION_TRANSACTION_FILES,
    ExecutionCode,
    ExecutionState,
    FileExecutionState,
)
from core.dataset_io import (
    DEFAULT_JSON_SIZE_LIMIT,
    EXECUTION_REPORT_SCHEMA,
    OPERATION_LIST_SCHEMA,
    OPERATION_SUMMARY_SCHEMA,
    PLAN_VALIDATION_SCHEMA,
    PREPARATION_RESULT_SCHEMA,
    PREPARE_INPUT_SCHEMA,
    DatasetIOError,
    DatasetWorkflowFacade,
    MAX_DATASET_PLAN_ACTIONS,
    MAX_DATASET_PLAN_FILE_RECORDS,
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
    write_json_file,
)
from core.dataset_service import (
    DATASET_PLAN_SCHEMA,
    DatasetOperation,
    DatasetPreparation,
    DatasetRelation,
    PlanValidation,
    PreparationState,
)


class DatasetExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    INPUT_ERROR = 3
    PARTIAL = 4
    OPERATION_FAILED = 7
    INTERRUPTED = 130


_MIB = 1024 * 1024
DATASET_INPUT_LIMITS_HELP = (
    "Input limit: one strict dataset JSON document <= {} MiB. "
    "Plan interchange is limited to {} actions and {} file records; one "
    "recoverable apply transaction is limited to {} file records and may be "
    "lower for unusually long paths. Split larger plans before apply. File "
    "and stdin inputs use the same seek-free bounded reader; JSONL is not "
    "accepted."
).format(
    DEFAULT_JSON_SIZE_LIMIT // _MIB,
    MAX_DATASET_PLAN_ACTIONS,
    MAX_DATASET_PLAN_FILE_RECORDS,
    MAX_EXECUTION_TRANSACTION_FILES,
)


def add_dataset_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``dataset`` and return its parser.

    Parent integration is intentionally two lines: call this while building the parent parser,
    then dispatch ``args.command == "dataset"`` through :func:`run_dataset_command`.
    """

    parser = subparsers.add_parser(
        "dataset",
        help="prepare, validate, and safely apply image-dataset plans",
        description="Strict schema-versioned dataset JSON; mutations always require an explicit flag.",
        epilog=DATASET_INPUT_LIMITS_HELP,
    )
    commands = parser.add_subparsers(dest="dataset_command", required=True)

    prepare = commands.add_parser("prepare", help="create an immutable dataset plan from a manifest")
    prepare.add_argument("input", metavar="INPUT", help="prepare-input JSON path, or - for stdin")
    prepare.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base under which the private executor directory is stored",
    )
    prepare.add_argument(
        "--plan-out",
        metavar="FILE",
        help="plan output path; this is a write preview unless --write is also supplied",
    )
    prepare.add_argument(
        "--write",
        action="store_true",
        help="atomically create --plan-out; existing files are never replaced",
    )

    prepare_root = commands.add_parser(
        "prepare-root",
        help="discover images, exact proofs, sidecars, and splits directly from roots",
    )
    prepare_root.add_argument(
        "roots",
        metavar="ROOT",
        nargs="+",
        help="one or more non-overlapping image-library roots",
    )
    prepare_root.add_argument(
        "--destination-root",
        metavar="DIR",
        required=True,
        help="existing, physically separate dataset destination directory",
    )
    prepare_root.add_argument(
        "--protected-root",
        metavar="DIR",
        action="append",
        default=[],
        help="protected library directory contained by an input root; repeatable",
    )
    prepare_root.add_argument(
        "--visual-cache",
        metavar="DB",
        help="SQLite visual-feature cache outside every input root",
    )
    prepare_root.add_argument(
        "--threshold",
        metavar="PERCENT",
        type=int,
        default=80,
        help="visual block-similarity threshold (0-100; default: 80)",
    )
    prepare_root.add_argument(
        "--phash-radius",
        metavar="BITS",
        type=int,
        default=8,
        help="pHash Hamming candidate radius (0-64; default: 8)",
    )
    prepare_root.add_argument(
        "--match-scaled",
        action="store_true",
        help="include scaled-image candidates",
    )
    prepare_root.add_argument(
        "--match-rotated",
        action="store_true",
        help="include rotated and mirrored image orientations",
    )
    prepare_root.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base under which the private executor directory will be stored",
    )
    prepare_root.add_argument(
        "--plan-out",
        metavar="FILE",
        help="plan output path; this is a write preview unless --write is also supplied",
    )
    prepare_root.add_argument(
        "--write",
        action="store_true",
        help="atomically create --plan-out; existing files are never replaced",
    )
    prepare_root.add_argument(
        "--allow-apply",
        action="store_true",
        help="create a plan that may later be applied; default plans remain dry-run only",
    )

    validate = commands.add_parser("validate", help="revalidate every source and destination in a plan")
    validate.add_argument("plan", metavar="PLAN", help="dataset-plan JSON path, or - for stdin")

    apply = commands.add_parser("apply", help="preview or execute an immutable dataset plan")
    apply.add_argument("plan", metavar="PLAN", help="dataset-plan JSON path, or - for stdin")
    apply.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base containing the private executor directory",
    )
    apply.add_argument(
        "--execute",
        action="store_true",
        help="request mutation; without this flag apply remains read-only",
    )

    list_parser = commands.add_parser("list", help="list persisted dataset operations")
    list_parser.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base containing the private executor directory",
    )
    list_parser.add_argument(
        "--destination-root",
        metavar="DIR",
        help="dataset destination root when the default state directory is used",
    )

    restore = commands.add_parser("restore", help="preview or restore one complete dataset operation")
    restore.add_argument("plan_id", metavar="PLAN_ID")
    restore.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base containing the private executor directory",
    )
    restore.add_argument("--destination-root", metavar="DIR", help="dataset destination root")
    restore.add_argument(
        "--execute",
        action="store_true",
        help="request mutation; without this flag restore remains read-only",
    )

    finalize = commands.add_parser(
        "finalize",
        help="preview or permanently finalize quarantined payloads",
    )
    finalize.add_argument("plan_id", metavar="PLAN_ID")
    finalize.add_argument(
        "--state-root",
        metavar="DIR",
        help="state base containing the private executor directory",
    )
    finalize.add_argument("--destination-root", metavar="DIR", help="dataset destination root")
    finalize.add_argument(
        "--execute",
        action="store_true",
        help="permanently unlink revalidated quarantine payloads",
    )
    return parser


def _read_input(path: str, stdin: TextIO) -> Any:
    if path == "-":
        return read_json_stream(
            stdin,
            maximum_bytes=DEFAULT_JSON_SIZE_LIMIT,
        )
    return read_json_file(
        Path(path),
        maximum_bytes=DEFAULT_JSON_SIZE_LIMIT,
    )


def _load_prepare_input(path: str, stdin: TextIO):
    value = _read_input(path, stdin)
    if isinstance(value, (str, bytes)):
        return dataset_prepare_input_from_json(value)
    return dataset_prepare_input_from_dict(value)


def _load_plan(path: str, stdin: TextIO):
    value = _read_input(path, stdin)
    if isinstance(value, (str, bytes)):
        return dataset_plan_from_json(value)
    return dataset_plan_from_dict(value)


def _write_stdout(stdout: TextIO, value: Mapping[str, Any]) -> None:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    size = 1  # trailing newline
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > DEFAULT_JSON_SIZE_LIMIT:
            raise DatasetIOError(
                "json_too_large",
                "dataset CLI output exceeds the {}-byte limit".format(
                    DEFAULT_JSON_SIZE_LIMIT,
                ),
            )
    for chunk in encoder.iterencode(value):
        stdout.write(chunk)
    stdout.write("\n")
    stdout.flush()


def _write_error(stderr: TextIO, error: BaseException) -> None:
    if isinstance(error, DatasetIOError):
        label = error.code
    else:
        label = type(error).__name__
    stderr.write("{}: {}\n".format(label, error))
    stderr.flush()


def _preparation_exit(result: DatasetPreparation) -> int:
    return int(DatasetExitCode.OK if result.complete else DatasetExitCode.PARTIAL)


def _validation_exit(result: PlanValidation) -> int:
    return int(DatasetExitCode.OK if result.valid else DatasetExitCode.PARTIAL)


def _execution_exit(state: ExecutionState, code: ExecutionCode) -> int:
    if state in {
        ExecutionState.DRY_RUN,
        ExecutionState.READY,
        ExecutionState.APPLIED,
        ExecutionState.ALREADY_APPLIED,
        ExecutionState.RESTORED,
        ExecutionState.FINALIZED,
    }:
        return int(DatasetExitCode.OK)
    if state is ExecutionState.RECOVERY_REQUIRED:
        return int(DatasetExitCode.PARTIAL)
    if state is ExecutionState.ROLLED_BACK:
        return int(DatasetExitCode.OPERATION_FAILED)
    if code in {
        ExecutionCode.PLAN_INVALID,
        ExecutionCode.PREFLIGHT_FAILED,
        ExecutionCode.DESTINATION_CONFLICT,
        ExecutionCode.SOURCE_CHANGED,
        ExecutionCode.CONTENT_MISMATCH,
        ExecutionCode.UNSAFE_PATH,
        ExecutionCode.VOLUME_MISMATCH,
        ExecutionCode.INSUFFICIENT_SPACE,
        ExecutionCode.DOCUMENT_CONFLICT,
        ExecutionCode.JOURNAL_CORRUPT,
        ExecutionCode.INVALID_STATE,
    }:
        return int(DatasetExitCode.INPUT_ERROR)
    return int(DatasetExitCode.OPERATION_FAILED)


def run_dataset_command(
    args: argparse.Namespace,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run a parsed dataset command without touching global stdio."""

    try:
        command = getattr(args, "dataset_command", None)
        if command == "prepare":
            if args.write and not args.plan_out:
                raise DatasetIOError("invalid_arguments", "--write requires --plan-out")
            request = _load_prepare_input(args.input, stdin)
            facade = DatasetWorkflowFacade(state_root=args.state_root)
            result = facade.prepare(request)
            if args.plan_out is not None and result.plan is not None:
                write_json_file(
                    result.plan.to_dict(),
                    args.plan_out,
                    execute=bool(args.write),
                )
            _write_stdout(stdout, dataset_preparation_to_dict(result))
            return _preparation_exit(result)

        if command == "prepare-root":
            if args.write and not args.plan_out:
                raise DatasetIOError("invalid_arguments", "--write requires --plan-out")
            result = DatasetDiscoveryService().prepare(
                DatasetRootRequest(
                    roots=tuple(args.roots),
                    destination_root=args.destination_root,
                    protected_roots=tuple(args.protected_root),
                    visual_cache=args.visual_cache,
                    state_root=args.state_root,
                    similarity_threshold=args.threshold,
                    phash_radius=args.phash_radius,
                    match_scaled=bool(args.match_scaled),
                    match_rotated=bool(args.match_rotated),
                    dry_run=not bool(args.allow_apply),
                )
            )
            if args.plan_out is not None and result.plan is not None:
                write_json_file(
                    result.plan.to_dict(),
                    args.plan_out,
                    execute=bool(args.write),
                )
            _write_stdout(stdout, dataset_preparation_to_dict(result))
            return _preparation_exit(result)

        if command == "validate":
            plan = _load_plan(args.plan, stdin)
            facade = DatasetWorkflowFacade()
            result = facade.validate(plan)
            _write_stdout(stdout, plan_validation_to_dict(result))
            return _validation_exit(result)

        if command == "apply":
            plan = _load_plan(args.plan, stdin)
            facade = DatasetWorkflowFacade(state_root=args.state_root)
            result = facade.apply(plan, execute=bool(args.execute))
            _write_stdout(stdout, dataset_execution_report_to_dict(result))
            return _execution_exit(result.state, result.code)

        if command == "list":
            if not args.state_root and not args.destination_root:
                raise DatasetIOError(
                    "invalid_arguments",
                    "list requires --state-root or --destination-root",
                )
            facade = DatasetWorkflowFacade(state_root=args.state_root)
            results = facade.list_operations(destination_root=args.destination_root)
            _write_stdout(stdout, dataset_operation_list_to_dict(results))
            if any(result.state is ExecutionState.RECOVERY_REQUIRED for result in results):
                return int(DatasetExitCode.PARTIAL)
            return int(DatasetExitCode.OK)

        if command in {"restore", "finalize"}:
            if not args.state_root and not args.destination_root:
                raise DatasetIOError(
                    "invalid_arguments",
                    "{} requires --state-root or --destination-root".format(command),
                )
            facade = DatasetWorkflowFacade(state_root=args.state_root)
            if command == "restore":
                result = facade.restore(
                    args.plan_id,
                    destination_root=args.destination_root,
                    execute=bool(args.execute),
                )
            else:
                result = facade.finalize(
                    args.plan_id,
                    destination_root=args.destination_root,
                    execute=bool(args.execute),
                )
            _write_stdout(stdout, dataset_execution_report_to_dict(result))
            return _execution_exit(result.state, result.code)

        stderr.write("Unknown dataset command: {}\n".format(command))
        stderr.flush()
        return int(DatasetExitCode.USAGE)
    except KeyboardInterrupt:
        stderr.write("Interrupted\n")
        stderr.flush()
        return int(DatasetExitCode.INTERRUPTED)
    except (DatasetIOError, OSError, TypeError, ValueError) as error:
        _write_error(stderr, error)
        return int(DatasetExitCode.INPUT_ERROR)
    except Exception as error:
        _write_error(stderr, error)
        return int(DatasetExitCode.INTERNAL_ERROR)


_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_BASE = "https://dupeguru.com/schemas"


def _strict_object(
    properties: Mapping[str, Any],
    required: Optional[list[str]] = None,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required if required is not None else properties),
        "additionalProperties": False,
    }


def _schema_document(name: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    result = {
        "$schema": _DRAFT_2020_12,
        "$id": "{}/{}/v1".format(_SCHEMA_BASE, name),
        "title": name,
    }
    result.update(body)
    return result


def _string_schema(*, pattern: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"type": "string", "minLength": 1}
    if pattern is not None:
        result["pattern"] = pattern
    return result


def _file_proof_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "path": _string_schema(),
            "resolved_path": _string_schema(),
            "size": {"type": "integer", "minimum": 0},
            "mtime_ns": {"type": "integer", "minimum": 0},
            "ctime_ns": {"type": "integer", "minimum": 0},
            "generation_token": _string_schema(pattern="^[0-9a-f]+$"),
            "digest_algorithm": {"const": "sha256"},
            "digest_hex": _string_schema(pattern="^[0-9a-fA-F]{64}$"),
            "identity_namespace": _string_schema(),
            "identity_capability": _string_schema(),
            "volume_id": {"type": "integer", "minimum": 0},
            "file_id": _string_schema(),
            "stat_device": {"type": "integer", "minimum": 0},
            "stat_inode": {"type": "integer", "minimum": 1},
        }
    )


def _split_assignment_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "cluster_id": _string_schema(),
            "members": {
                "type": "array",
                "items": _string_schema(),
                "minItems": 1,
                "uniqueItems": True,
            },
            "split": _string_schema(),
            "reason": {
                "type": "string",
                "enum": [
                    "hashed",
                    "preserved_cluster",
                    "preserved_members",
                    "merged_previous_splits",
                ],
            },
            "previous_splits": {
                "type": "array",
                "items": _string_schema(),
                "uniqueItems": True,
            },
        }
    )


def _split_manifest_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "version": {"const": "stable_split_v1"},
            "seed": {"type": "string"},
            "split_weights": {
                "type": "object",
                "minProperties": 1,
                "propertyNames": _string_schema(),
                "additionalProperties": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
            },
            "assignments": {
                "type": "array",
                "items": _split_assignment_schema(),
                "maxItems": MAX_DATASET_PLAN_ACTIONS,
            },
        }
    )


def _keeper_reason_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "code": _string_schema(),
            "points": {"type": "number"},
            "message": _string_schema(),
        }
    )


def _keeper_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "cluster_id": _string_schema(),
            "keeper_id": _string_schema(),
            "explanations": {
                "type": "object",
                "additionalProperties": _string_schema(),
            },
            "scores": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "reasons": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": _keeper_reason_schema(),
                },
            },
        }
    )


def _file_action_schema() -> Dict[str, Any]:
    proof = _file_proof_schema()
    return _strict_object(
        {
            "source": proof,
            "destination": {
                "oneOf": [
                    _string_schema(),
                    {"type": "null"},
                ]
            },
            "reference": {
                "oneOf": [
                    _file_proof_schema(),
                    {"type": "null"},
                ]
            },
            "role": {"type": "string", "enum": ["primary", "sidecar"]},
            "sidecar_slot": {"type": "string"},
        }
    )


def _bundle_action_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "asset_id": _string_schema(),
            "cluster_id": _string_schema(),
            "split": _string_schema(),
            "operation": {
                "type": "string",
                "enum": [item.value for item in DatasetOperation],
            },
            "files": {
                "type": "array",
                "items": _file_action_schema(),
                "minItems": 1,
                "maxItems": MAX_DATASET_PLAN_FILE_RECORDS,
            },
            "keeper_id": {
                "oneOf": [
                    _string_schema(),
                    {"type": "null"},
                ]
            },
            "atomic": {"const": True},
            "action_id": _string_schema(pattern="^[0-9a-f]{64}$"),
        }
    )


def _plan_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": DATASET_PLAN_SCHEMA},
            "schema_version": {"const": 1},
            "allowed_roots": {
                "type": "array",
                "items": _string_schema(),
                "minItems": 1,
                "uniqueItems": True,
            },
            "destination_root": _string_schema(),
            "split_manifest": _split_manifest_schema(),
            "keepers": {
                "type": "array",
                "items": _keeper_schema(),
                "maxItems": MAX_DATASET_PLAN_ACTIONS,
            },
            "actions": {
                "type": "array",
                "items": _bundle_action_schema(),
                "maxItems": MAX_DATASET_PLAN_ACTIONS,
            },
            "dry_run": {"type": "boolean"},
            "executor_contract": {
                "const": "dupeguru.safe-action-quarantine-bundle.v1",
            },
            "plan_id": _string_schema(pattern="^[0-9a-f]{64}$"),
        }
    )


def _prepare_asset_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "asset_id": _string_schema(),
            "path": _string_schema(),
            "dimensions": {
                "oneOf": [
                    {
                        "type": "array",
                        "prefixItems": [
                            {"type": "integer", "minimum": 1},
                            {"type": "integer", "minimum": 1},
                        ],
                        "items": False,
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    {"type": "null"},
                ]
            },
            "bit_depth": {"type": "number", "minimum": 0},
            "metadata_count": {"type": "number", "minimum": 0},
            "jpeg_artifact_score": {"type": "number", "minimum": 0},
            "protected": {"type": "boolean"},
        }
    )


def _prepare_cluster_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "members": {
                "type": "array",
                "items": _string_schema(),
                "minItems": 2,
                "uniqueItems": True,
            },
            "relation": {
                "type": "string",
                "enum": [item.value for item in DatasetRelation],
            },
            "evidence_complete": {"type": "boolean"},
            "evidence_version": _string_schema(),
        }
    )


def _prepare_input_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": PREPARE_INPUT_SCHEMA},
            "schema_version": {"const": 1},
            "allowed_roots": {
                "type": "array",
                "items": _string_schema(),
                "minItems": 1,
                "uniqueItems": True,
            },
            "destination_root": _string_schema(),
            "assets": {
                "type": "array",
                "items": _prepare_asset_schema(),
                "minItems": 1,
                "maxItems": MAX_DATASET_PLAN_ACTIONS,
            },
            "clusters": {
                "type": "array",
                "items": _prepare_cluster_schema(),
                "maxItems": MAX_DATASET_PLAN_ACTIONS,
            },
            "sidecar_paths": {
                "type": "array",
                "items": _string_schema(),
                "uniqueItems": True,
                "maxItems": MAX_DATASET_PLAN_FILE_RECORDS,
            },
            "split_weights": {
                "type": "object",
                "minProperties": 1,
                "propertyNames": _string_schema(),
                "additionalProperties": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
            },
            "split_seed": {"type": "string"},
            "dry_run": {"type": "boolean"},
        }
    )


def _prepare_root_request_body() -> Dict[str, Any]:
    nullable_path = {
        "oneOf": [
            _string_schema(),
            {"type": "null"},
        ]
    }
    return _strict_object(
        {
            "schema": {"const": "dupeguru.dataset-root-request"},
            "schema_version": {"const": 1},
            "roots": {
                "type": "array",
                "items": _string_schema(),
                "minItems": 1,
                "uniqueItems": True,
            },
            "destination_root": _string_schema(),
            "protected_roots": {
                "type": "array",
                "items": _string_schema(),
                "uniqueItems": True,
            },
            "visual_cache": nullable_path,
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
            "match_scaled": {"type": "boolean"},
            "match_rotated": {"type": "boolean"},
            "state_root": nullable_path,
            "plan_out": nullable_path,
            "write": {"type": "boolean"},
            "allow_apply": {"type": "boolean"},
        }
    )


def _issue_schema() -> Dict[str, Any]:
    return _strict_object(
        {
            "code": _string_schema(),
            "message": _string_schema(),
            "paths": {
                "type": "array",
                "items": _string_schema(),
                "uniqueItems": True,
            },
            "asset_ids": {
                "type": "array",
                "items": _string_schema(),
                "uniqueItems": True,
            },
        }
    )


def _preparation_result_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": PREPARATION_RESULT_SCHEMA},
            "schema_version": {"const": 1},
            "state": {
                "type": "string",
                "enum": [item.value for item in PreparationState],
            },
            "complete": {"type": "boolean"},
            "plan": {
                "oneOf": [
                    _plan_body(),
                    {"type": "null"},
                ]
            },
            "issues": {
                "type": "array",
                "items": _issue_schema(),
            },
        }
    )


def _validation_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": PLAN_VALIDATION_SCHEMA},
            "schema_version": {"const": 1},
            "valid": {"type": "boolean"},
            "issues": {
                "type": "array",
                "items": _issue_schema(),
            },
        }
    )


def _file_execution_result_schema() -> Dict[str, Any]:
    nullable_string = {
        "oneOf": [
            _string_schema(),
            {"type": "null"},
        ]
    }
    return _strict_object(
        {
            "action_id": _string_schema(),
            "source": _string_schema(),
            "destination": nullable_string,
            "quarantine_path": {
                "oneOf": [
                    _string_schema(),
                    {"type": "null"},
                ]
            },
            "state": {
                "type": "string",
                "enum": [item.value for item in FileExecutionState],
            },
            "changed": {"type": "boolean"},
            "message": _string_schema(),
        }
    )


def _execution_report_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": EXECUTION_REPORT_SCHEMA},
            "schema_version": {"const": 1},
            "plan_id": _string_schema(),
            "state": {
                "type": "string",
                "enum": [item.value for item in ExecutionState],
            },
            "code": {
                "type": "string",
                "enum": [item.value for item in ExecutionCode],
            },
            "message": _string_schema(),
            "changed": {"type": "boolean"},
            "ok": {"type": "boolean"},
            "files": {
                "type": "array",
                "items": _file_execution_result_schema(),
            },
            "issues": {
                "type": "array",
                "items": _issue_schema(),
            },
        }
    )


def _operation_summary_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": OPERATION_SUMMARY_SCHEMA},
            "schema_version": {"const": 1},
            "plan_id": _string_schema(),
            "state": {
                "type": "string",
                "enum": [item.value for item in ExecutionState],
            },
            "document_path": _string_schema(),
            "created_ns": {"type": "integer", "minimum": 0},
            "file_count": {"type": "integer", "minimum": 0},
            "message": _string_schema(),
        }
    )


def _operation_list_body() -> Dict[str, Any]:
    return _strict_object(
        {
            "schema": {"const": OPERATION_LIST_SCHEMA},
            "schema_version": {"const": 1},
            "operations": {
                "type": "array",
                "items": _operation_summary_body(),
            },
        }
    )


DATASET_SCHEMAS: Dict[str, Mapping[str, Any]] = {
    "prepare-input": _schema_document("dataset-prepare-input", _prepare_input_body()),
    "prepare-root-request": _schema_document(
        "dataset-prepare-root-request",
        _prepare_root_request_body(),
    ),
    "dataset-plan": _schema_document("dataset-plan", _plan_body()),
    "preparation-result": _schema_document(
        "dataset-preparation-result",
        _preparation_result_body(),
    ),
    "plan-validation": _schema_document(
        "dataset-plan-validation",
        _validation_body(),
    ),
    "execution-report": _schema_document(
        "dataset-execution-report",
        _execution_report_body(),
    ),
    "operation-summary": _schema_document(
        "dataset-operation-summary",
        _operation_summary_body(),
    ),
    "operation-list": _schema_document(
        "dataset-operation-list",
        _operation_list_body(),
    ),
}


__all__ = [
    "DATASET_SCHEMAS",
    "DatasetExitCode",
    "add_dataset_parser",
    "run_dataset_command",
]
