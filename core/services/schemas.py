from __future__ import annotations

from typing import Any, Dict

from core.services.jsonio import (
    MAX_DOCUMENT_ROOTS,
    MAX_PLAN_ACTIONS,
    MAX_SCAN_COVERAGE_RECORDS,
    MAX_SCAN_FILE_RECORDS,
    MAX_SCAN_GROUPS,
    MAX_SCAN_ISSUES,
)
from core.services.models import (
    APPLY_REPORT_SCHEMA,
    DELETION_PLAN_SCHEMA,
    DOCTOR_REPORT_SCHEMA,
    PLAN_RECORD_SCHEMA,
    QUARANTINE_ACTION_SCHEMA,
    QUARANTINE_LIST_SCHEMA,
    QUARANTINE_OPERATION_SCHEMA,
    QUERY_REPORT_SCHEMA,
    SCAN_RECORD_SCHEMA,
    SCAN_REPORT_SCHEMA,
    SCHEMA_VERSION,
    VIDEO_ANALYSIS_SCHEMA,
    VIDEO_CAPABILITIES_SCHEMA,
    VIDEO_COMPARISON_SCHEMA,
    VIDEO_LIBRARY_GROUP_SCHEMA,
    VIDEO_LIBRARY_RECORD_SCHEMA,
    VIDEO_LIBRARY_SCAN_SCHEMA,
)


def _file_schema(
    digest_algorithm: str = "",
    *,
    require_identity: bool = False,
) -> Dict[str, Any]:
    algorithm_schema: Dict[str, Any]
    digest_schema: Dict[str, Any]
    if digest_algorithm:
        algorithm_schema = {"const": digest_algorithm}
        digest_schema = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    else:
        algorithm_schema = {"type": "string", "minLength": 1}
        digest_schema = {"type": "string", "minLength": 1}
    required = ["path", "size", "mtime_ns", "digest_algorithm", "digest"]
    if require_identity:
        required.extend(("volume_id", "file_id"))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "size": {"type": "integer", "minimum": 0},
            "mtime_ns": {"type": "integer", "minimum": 0},
            "digest_algorithm": algorithm_schema,
            "digest": digest_schema,
            "volume_id": {"type": "string", "minLength": 1},
            "file_id": {"type": "string", "minLength": 1},
        },
    }


def scan_report_schema() -> Dict[str, Any]:
    file_schema = _file_schema()
    issue_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "code", "message"],
        "properties": {
            "path": {"type": "string"},
            "code": {"type": "string", "minLength": 1},
            "message": {"type": "string"},
        },
    }
    summary_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "discovered_files",
            "hashed_files",
            "verified_groups",
            "duplicate_files",
            "issues",
            "complete",
        ],
        "properties": {
            "discovered_files": {"type": "integer", "minimum": 0},
            "hashed_files": {"type": "integer", "minimum": 0},
            "verified_groups": {"type": "integer", "minimum": 0},
            "duplicate_files": {"type": "integer", "minimum": 0},
            "issues": {"type": "integer", "minimum": 0},
            "complete": {"type": "boolean"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:scan-report:1",
        "title": "dupeGuru Neo scan report",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "scan_id",
            "created_at",
            "engine_version",
            "roots",
            "mode",
            "groups",
            "issues",
            "coverage",
            "summary",
        ],
        "properties": {
            "schema": {"const": SCAN_REPORT_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "scan_id": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "minLength": 1},
            "engine_version": {"type": "string", "minLength": 1},
            "roots": {
                "type": "array",
                "maxItems": MAX_DOCUMENT_ROOTS,
                "items": {"type": "string"},
            },
            "mode": {"const": "exact"},
            "groups": {
                "type": "array",
                "maxItems": MAX_SCAN_GROUPS,
                "description": (
                    "The cumulative reference-plus-duplicate file-record count "
                    "across all groups is limited to {} by the loader."
                ).format(MAX_SCAN_FILE_RECORDS),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "group_id",
                        "verification",
                        "verification_method",
                        "reference",
                        "duplicates",
                    ],
                    "properties": {
                        "group_id": {"type": "string", "minLength": 1},
                        "verification": {"enum": ["verified_exact", "approximate", "related", "unknown"]},
                        "verification_method": {"type": "string", "minLength": 1},
                        "reference": file_schema,
                        "duplicates": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_SCAN_FILE_RECORDS,
                            "items": file_schema,
                        },
                    },
                },
            },
            "issues": {
                "type": "array",
                "maxItems": MAX_SCAN_ISSUES,
                "items": issue_schema,
            },
            "coverage": {
                "type": "array",
                "maxItems": MAX_SCAN_COVERAGE_RECORDS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["root", "complete", "counters", "identity_capabilities"],
                    "properties": {
                        "root": {"type": "string", "minLength": 1},
                        "complete": {"type": "boolean"},
                        "counters": {
                            "type": "object",
                            "additionalProperties": {"type": "integer", "minimum": 0},
                        },
                        "identity_capabilities": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "summary": summary_schema,
        },
    }


def deletion_plan_schema() -> Dict[str, Any]:
    file_schema = _file_schema("sha256", require_identity=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:deletion-plan:1",
        "title": "dupeGuru Neo deletion plan",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "plan_id",
            "created_at",
            "engine_version",
            "source_scan_id",
            "roots",
            "actions",
        ],
        "properties": {
            "schema": {"const": DELETION_PLAN_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "plan_id": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "minLength": 1},
            "engine_version": {"type": "string", "minLength": 1},
            "source_scan_id": {"type": "string", "minLength": 1},
            "roots": {
                "type": "array",
                "maxItems": MAX_DOCUMENT_ROOTS,
                "items": {"type": "string"},
            },
            "actions": {
                "type": "array",
                "maxItems": MAX_PLAN_ACTIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "action_id",
                        "group_id",
                        "operation",
                        "target",
                        "reference",
                        "verification",
                    ],
                    "properties": {
                        "action_id": {"type": "string", "minLength": 1},
                        "group_id": {"type": "string", "minLength": 1},
                        "operation": {"const": "quarantine"},
                        "target": file_schema,
                        "reference": file_schema,
                        "verification": {"const": "verified_exact"},
                    },
                },
            },
        },
    }


def scan_record_schema() -> Dict[str, Any]:
    report = scan_report_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:scan-record:1",
        "title": "dupeGuru Neo streaming scan record",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "schema_version", "record_type"],
        "properties": {
            "schema": {"const": SCAN_RECORD_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "record_type": {"enum": ["header", "group", "issue", "coverage", "summary"]},
            "document_schema": {"const": SCAN_REPORT_SCHEMA},
            "scan_id": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "minLength": 1},
            "engine_version": {"type": "string", "minLength": 1},
            "roots": report["properties"]["roots"],
            "mode": {"const": "exact"},
            "group": report["properties"]["groups"]["items"],
            "issue": report["properties"]["issues"]["items"],
            "coverage": report["properties"]["coverage"]["items"],
            "summary": report["properties"]["summary"],
        },
    }


def plan_record_schema() -> Dict[str, Any]:
    plan = deletion_plan_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:plan-record:1",
        "title": "dupeGuru Neo streaming deletion-plan record",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "schema_version", "record_type"],
        "properties": {
            "schema": {"const": PLAN_RECORD_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "record_type": {"enum": ["header", "action", "summary"]},
            "document_schema": {"const": DELETION_PLAN_SCHEMA},
            "plan_id": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "minLength": 1},
            "engine_version": {"type": "string", "minLength": 1},
            "source_scan_id": {"type": "string", "minLength": 1},
            "roots": plan["properties"]["roots"],
            "action": plan["properties"]["actions"]["items"],
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": ["actions"],
                "properties": {
                    "actions": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_PLAN_ACTIONS,
                    }
                },
            },
        },
    }


def apply_report_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:apply-report:1",
        "title": "dupeGuru Neo apply report",
        "type": "object",
        "properties": {
            "schema": {"const": APPLY_REPORT_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "plan_id": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "minLength": 1},
            "dry_run": {"type": "boolean"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "action_id",
                        "target",
                        "status",
                        "message",
                        "safe_state",
                        "failure_code",
                        "operation_plan_path",
                        "quarantine_path",
                        "changed",
                    ],
                    "properties": {
                        "action_id": {"type": "string", "minLength": 1},
                        "target": {"type": "string", "minLength": 1},
                        "status": {"enum": ["ready", "stale", "applied", "failed"]},
                        "message": {"type": "string"},
                        "safe_state": {"type": "string"},
                        "failure_code": {"type": "string"},
                        "operation_plan_path": {"type": "string"},
                        "quarantine_path": {"type": "string"},
                        "changed": {"type": "boolean"},
                    },
                },
            },
            "summary": {
                "type": "object",
                "required": ["actions", "ready", "stale", "applied", "failed"],
                "properties": {
                    "actions": {"type": "integer", "minimum": 0},
                    "ready": {"type": "integer", "minimum": 0},
                    "stale": {"type": "integer", "minimum": 0},
                    "applied": {"type": "integer", "minimum": 0},
                    "failed": {"type": "integer", "minimum": 0},
                },
            },
        },
        "required": ["schema", "schema_version", "plan_id", "created_at", "dry_run", "results", "summary"],
    }


def query_report_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:query-report:1",
        "title": "dupeGuru Neo query report",
        "type": "object",
        "properties": {
            "schema": {"const": QUERY_REPORT_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "source_scan_id": {"type": "string", "minLength": 1},
            "filters": {"type": "object"},
            "matches": {
                "type": "array",
                "items": scan_report_schema()["properties"]["groups"]["items"],
            },
            "summary": {
                "type": "object",
                "required": ["groups"],
                "properties": {"groups": {"type": "integer", "minimum": 0}},
            },
        },
        "required": [
            "schema",
            "schema_version",
            "source_scan_id",
            "filters",
            "matches",
            "summary",
        ],
    }


def doctor_report_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:doctor-report:1",
        "title": "dupeGuru Neo doctor report",
        "type": "object",
        "properties": {
            "schema": {"const": DOCTOR_REPORT_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "app_version": {"type": "string", "minLength": 1},
            "python": {"type": "string", "minLength": 1},
            "python_implementation": {"type": "string", "minLength": 1},
            "platform": {"type": "string", "minLength": 1},
            "pyqt_imported": {"type": "boolean"},
            "capabilities": {
                "type": "object",
                "additionalProperties": {"type": "boolean"},
            },
        },
        "required": [
            "schema",
            "schema_version",
            "app_version",
            "python",
            "python_implementation",
            "platform",
            "pyqt_imported",
            "capabilities",
        ],
    }


def quarantine_operation_schema() -> Dict[str, Any]:
    file_proof = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "path",
            "resolved_path",
            "entry_type",
            "identity",
            "size",
            "mtime_ns",
            "ctime_ns",
            "digest_algorithm",
            "digest_hex",
        ],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "resolved_path": {"type": "string", "minLength": 1},
            "entry_type": {"const": "regular_file"},
            "identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["volume_id", "file_id"],
                "properties": {
                    "volume_id": {"type": "integer"},
                    "file_id": {"type": "integer"},
                },
            },
            "size": {"type": "integer", "minimum": 0},
            "mtime_ns": {"type": "integer"},
            "ctime_ns": {"type": "integer"},
            "digest_algorithm": {"const": "sha256"},
            "digest_hex": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }
    operation_plan = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "plan_id",
            "created_ns",
            "allowed_roots",
            "quarantine_root",
            "target",
            "keeper",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "plan_id": {"type": "string", "format": "uuid"},
            "created_ns": {"type": "integer", "minimum": 0},
            "allowed_roots": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "quarantine_root": {"type": "string", "minLength": 1},
            "target": file_proof,
            "keeper": file_proof,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:quarantine-operation:1",
        "title": "dupeGuru Neo persisted safe operation",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "service_plan_id",
            "action_id",
            "operation",
            "operation_plan_fingerprint",
            "operation_plan",
        ],
        "properties": {
            "schema": {"const": QUARANTINE_OPERATION_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "service_plan_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "action_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "operation": {"const": "quarantine"},
            "operation_plan_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "operation_plan": operation_plan,
        },
    }


def quarantine_list_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:quarantine-list:1",
        "title": "dupeGuru Neo quarantine list",
        "type": "object",
        "required": ["schema", "schema_version", "created_at", "roots", "operations", "summary"],
        "properties": {
            "schema": {"const": QUARANTINE_LIST_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "created_at": {"type": "string", "minLength": 1},
            "roots": {"type": "array", "items": {"type": "string"}},
            "operations": {"type": "array", "items": {"type": "object"}},
            "summary": {
                "type": "object",
                "required": ["operations"],
                "properties": {"operations": {"type": "integer", "minimum": 0}},
            },
        },
    }


def quarantine_action_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:quarantine-action:1",
        "title": "dupeGuru Neo quarantine action result",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "created_at",
            "command",
            "dry_run",
            "result",
        ],
        "properties": {
            "schema": {"const": QUARANTINE_ACTION_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "created_at": {"type": "string", "minLength": 1},
            "command": {"enum": ["restore", "finalize"]},
            "dry_run": {"type": "boolean"},
            "result": apply_report_schema()["properties"]["results"]["items"],
        },
    }


def _video_issue_schema(*, source: bool) -> Dict[str, Any]:
    required = ["code", "message", "tool"]
    properties = {
        "code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1},
        "tool": {"type": ["string", "null"]},
    }
    if source:
        required.append("source")
        properties["source"] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _video_artifact_schema() -> Dict[str, Any]:
    analysis_states = [
        "complete",
        "partial_missing_tool",
        "partial_timeout",
        "partial_cancelled",
        "partial_resource_limit",
        "partial_tool_error",
        "failed",
    ]
    metadata = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": [
            "duration_seconds",
            "width",
            "height",
            "frame_rate",
            "video_codec",
            "pixel_format",
            "audio_codec",
            "audio_duration_seconds",
            "bit_rate",
            "container",
        ],
        "properties": {
            "duration_seconds": {"type": "number", "exclusiveMinimum": 0},
            "width": {"type": "integer", "minimum": 1},
            "height": {"type": "integer", "minimum": 1},
            "frame_rate": {"type": "number", "exclusiveMinimum": 0},
            "video_codec": {"type": "string", "minLength": 1},
            "pixel_format": {"type": "string"},
            "audio_codec": {"type": "string"},
            "audio_duration_seconds": {"type": ["number", "null"], "minimum": 0},
            "bit_rate": {"type": ["integer", "null"], "minimum": 0},
            "container": {"type": "string"},
        },
    }
    frame = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "timestamp_seconds",
            "normalized_position",
            "value",
            "bit_width",
            "algorithm",
        ],
        "properties": {
            "timestamp_seconds": {"type": "number", "minimum": 0},
            "normalized_position": {"type": "number", "minimum": 0, "maximum": 1},
            "value": {"type": "integer", "minimum": 0},
            "bit_width": {"type": "integer", "minimum": 1},
            "algorithm": {"type": "string", "minLength": 1},
        },
    }
    audio = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["values", "duration_seconds", "algorithm"],
        "properties": {
            "values": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
            "duration_seconds": {"type": "number", "minimum": 0},
            "algorithm": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "analyzer_version",
            "source",
            "metadata",
            "frames",
            "audio",
            "state",
            "issues",
            "tool_versions",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "analyzer_version": {"type": "string", "minLength": 1},
            "source": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "size", "mtime_ns"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "size": {"type": "integer", "minimum": 0},
                    "mtime_ns": {"type": "integer", "minimum": 0},
                },
            },
            "metadata": metadata,
            "frames": {"type": "array", "items": frame},
            "audio": audio,
            "state": {"enum": analysis_states},
            "issues": {
                "type": "array",
                "items": _video_issue_schema(source=False),
            },
            "tool_versions": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
    }


def video_capabilities_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-capabilities:1",
        "title": "dupeGuru Neo video tool capabilities",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "created_at",
            "state",
            "partial",
            "issues",
            "tools",
            "summary",
        ],
        "properties": {
            "schema": {"const": VIDEO_CAPABILITIES_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "created_at": {"type": "string", "minLength": 1},
            "state": {"enum": ["complete", "partial"]},
            "partial": {"type": "boolean"},
            "issues": {
                "type": "array",
                "items": _video_issue_schema(source=False),
            },
            "tools": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "name",
                        "state",
                        "available",
                        "required",
                        "executable",
                        "version",
                        "message",
                    ],
                    "properties": {
                        "name": {"enum": ["ffprobe", "ffmpeg", "fpcalc"]},
                        "state": {
                            "enum": [
                                "available",
                                "missing",
                                "timed_out",
                                "cancelled",
                                "error",
                            ]
                        },
                        "available": {"type": "boolean"},
                        "required": {"type": "boolean"},
                        "executable": {"type": ["string", "null"]},
                        "version": {"type": ["string", "null"]},
                        "message": {"type": "string", "minLength": 1},
                    },
                },
            },
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "available",
                    "unavailable",
                    "visual_analysis_available",
                    "audio_fingerprint_available",
                ],
                "properties": {
                    "available": {"type": "integer", "minimum": 0, "maximum": 3},
                    "unavailable": {"type": "integer", "minimum": 0, "maximum": 3},
                    "visual_analysis_available": {"type": "boolean"},
                    "audio_fingerprint_available": {"type": "boolean"},
                },
            },
        },
    }


def video_analysis_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-analysis:1",
        "title": "dupeGuru Neo video analysis report",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "created_at",
            "state",
            "partial",
            "issues",
            "artifact_source",
            "artifact_cache",
            "artifact",
            "summary",
        ],
        "properties": {
            "schema": {"const": VIDEO_ANALYSIS_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "created_at": {"type": "string", "minLength": 1},
            "state": _video_artifact_schema()["properties"]["state"],
            "partial": {"type": "boolean"},
            "issues": {
                "type": "array",
                "items": _video_issue_schema(source=True),
            },
            "artifact_source": {"enum": ["analyzed", "cache"]},
            "artifact_cache": {
                "type": "object",
                "additionalProperties": False,
                "required": ["input", "output"],
                "properties": {
                    "input": {"type": ["string", "null"]},
                    "output": {"type": ["string", "null"]},
                },
            },
            "artifact": _video_artifact_schema(),
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comparable", "frames", "has_audio_fingerprint"],
                "properties": {
                    "comparable": {"type": "boolean"},
                    "frames": {"type": "integer", "minimum": 0},
                    "has_audio_fingerprint": {"type": "boolean"},
                },
            },
        },
    }


def video_comparison_schema() -> Dict[str, Any]:
    relation = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": [
            "first_path",
            "second_path",
            "relation",
            "score",
            "metrics",
            "algorithm_version",
            "exact_proof",
            "notes",
            "allows_automatic_destructive_action",
        ],
        "properties": {
            "first_path": {"type": "string", "minLength": 1},
            "second_path": {"type": "string", "minLength": 1},
            "relation": {"enum": ["near", "transcoded", "trimmed", "related"]},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "metrics": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "algorithm_version": {"type": "string", "minLength": 1},
            "exact_proof": {"const": None},
            "notes": {"type": "array", "items": {"type": "string"}},
            "allows_automatic_destructive_action": {"const": False},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-comparison:1",
        "title": "dupeGuru Neo perceptual video comparison",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "created_at",
            "state",
            "partial",
            "issues",
            "threshold",
            "first",
            "second",
            "relation",
            "byte_exact_proof",
            "allows_automatic_destructive_action",
            "summary",
        ],
        "properties": {
            "schema": {"const": VIDEO_COMPARISON_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "created_at": {"type": "string", "minLength": 1},
            "state": _video_artifact_schema()["properties"]["state"],
            "partial": {"type": "boolean"},
            "issues": {
                "type": "array",
                "items": _video_issue_schema(source=True),
            },
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "first": _video_artifact_schema(),
            "second": _video_artifact_schema(),
            "relation": relation,
            "byte_exact_proof": {"const": None},
            "allows_automatic_destructive_action": {"const": False},
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comparable", "relation_found"],
                "properties": {
                    "comparable": {"type": "boolean"},
                    "relation_found": {"type": "boolean"},
                },
            },
        },
    }


def video_library_group_schema() -> Dict[str, Any]:
    relation = video_comparison_schema()["properties"]["relation"]
    relation["type"] = "object"
    metadata = _video_artifact_schema()["properties"]["metadata"]
    metadata["type"] = "object"
    member = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "size", "mtime_ns", "metadata"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "size": {"type": "integer", "minimum": 0},
            "mtime_ns": {"type": "integer", "minimum": 0},
            "metadata": metadata,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-library-group:1",
        "title": "dupeGuru Neo review-only video similarity group",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "group_id",
            "members",
            "relations",
            "review_only",
            "byte_exact_proof",
            "allows_automatic_destructive_action",
        ],
        "properties": {
            "schema": {"const": VIDEO_LIBRARY_GROUP_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "group_id": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "members": {
                "type": "array",
                "minItems": 2,
                "items": member,
            },
            "relations": {
                "type": "array",
                "minItems": 1,
                "items": relation,
            },
            "review_only": {"const": True},
            "byte_exact_proof": {"const": None},
            "allows_automatic_destructive_action": {"const": False},
        },
    }


def video_library_scan_schema() -> Dict[str, Any]:
    issue = _video_issue_schema(source=True)
    limits = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "maximum_files",
            "maximum_candidate_assessments",
            "maximum_candidates",
            "maximum_comparisons",
            "maximum_fingerprint_files",
            "maximum_groups",
            "probe_timeout_seconds",
            "maximum_probe_output_bytes",
            "maximum_scan_seconds",
        ],
        "properties": {
            "maximum_files": {"type": "integer", "minimum": 1},
            "maximum_candidate_assessments": {"type": "integer", "minimum": 1},
            "maximum_candidates": {"type": "integer", "minimum": 1},
            "maximum_comparisons": {"type": "integer", "minimum": 1},
            "maximum_fingerprint_files": {"type": "integer", "minimum": 1},
            "maximum_groups": {"type": "integer", "minimum": 1},
            "probe_timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
            },
            "maximum_probe_output_bytes": {
                "type": "integer",
                "minimum": 1,
            },
            "maximum_scan_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
            },
        },
    }
    receipt = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "complete",
            "discovered",
            "analyzed",
            "skipped",
            "failed",
        ],
        "properties": {
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
            "discovered": {"type": "integer", "minimum": 0},
            "analyzed": {"type": "integer", "minimum": 0},
            "skipped": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
        },
    }
    cache = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "persistent", "hits", "misses", "writes"],
        "properties": {
            "path": {"type": ["string", "null"]},
            "persistent": {"type": "boolean"},
            "hits": {"type": "integer", "minimum": 0},
            "misses": {"type": "integer", "minimum": 0},
            "writes": {"type": "integer", "minimum": 0},
        },
    }
    safety = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "source_read_only",
            "review_only",
            "byte_exact_proof",
            "destructive_actions_allowed",
        ],
        "properties": {
            "source_read_only": {"const": True},
            "review_only": {"const": True},
            "byte_exact_proof": {"const": False},
            "destructive_actions_allowed": {"const": False},
        },
    }
    summary_names = [
        "video_files",
        "metadata_complete",
        "candidate_assessments",
        "candidates",
        "comparisons",
        "relations",
        "groups",
    ]
    summary = {
        "type": "object",
        "additionalProperties": False,
        "required": summary_names,
        "properties": {name: {"type": "integer", "minimum": 0} for name in summary_names},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-library-scan:1",
        "title": "dupeGuru Neo bounded video-library scan",
        "type": "object",
        "additionalProperties": False,
        "required": [
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
        ],
        "properties": {
            "schema": {"const": VIDEO_LIBRARY_SCAN_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "scan_id": {"type": "string", "minLength": 1},
            "created_at_ns": {"type": "integer", "minimum": 0},
            "state": {
                "enum": [
                    "complete",
                    "partial_missing_tool",
                    "partial_timeout",
                    "partial_cancelled",
                    "partial_resource_limit",
                    "partial_tool_error",
                    "failed",
                ]
            },
            "partial": {"type": "boolean"},
            "roots": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "limits": limits,
            "issues": {
                "type": "array",
                "items": issue,
            },
            "receipt": receipt,
            "cache": cache,
            "groups": {
                "type": "array",
                "items": video_library_group_schema(),
            },
            "safety": safety,
            "summary": summary,
        },
    }


def video_library_record_schema() -> Dict[str, Any]:
    document = video_library_scan_schema()
    document_properties = document["properties"]
    header_names = [
        "created_at_ns",
        "state",
        "partial",
        "roots",
        "threshold",
        "limits",
        "cache",
        "safety",
    ]
    header = {
        "type": "object",
        "additionalProperties": False,
        "required": header_names,
        "properties": {name: document_properties[name] for name in header_names},
    }
    issue = document_properties["issues"]["items"]
    receipt = document_properties["receipt"]
    summary = document_properties["summary"]
    nullable = {"type": "null"}
    record_payloads = {
        "header": header,
        "group": video_library_group_schema(),
        "issue": issue,
        "receipt": receipt,
        "summary": summary,
    }
    variants = []
    for record_type, payload_schema in record_payloads.items():
        payload_properties = {name: payload_schema if name == record_type else nullable for name in record_payloads}
        payload_properties["record_type"] = {"const": record_type}
        variants.append(
            {
                "properties": payload_properties,
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:dupeguru-neo:schema:video-library-record:1",
        "title": "dupeGuru Neo video-library JSONL record",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "schema_version",
            "document_schema",
            "record_type",
            "scan_id",
            "header",
            "group",
            "issue",
            "receipt",
            "summary",
        ],
        "properties": {
            "schema": {"const": VIDEO_LIBRARY_RECORD_SCHEMA},
            "schema_version": {"const": SCHEMA_VERSION},
            "document_schema": {"const": VIDEO_LIBRARY_SCAN_SCHEMA},
            "record_type": {"enum": ["header", "group", "issue", "receipt", "summary"]},
            "scan_id": {"type": "string", "minLength": 1},
            "header": {
                "anyOf": [
                    header,
                    nullable,
                ]
            },
            "group": {
                "anyOf": [
                    video_library_group_schema(),
                    nullable,
                ]
            },
            "issue": {
                "anyOf": [
                    issue,
                    nullable,
                ]
            },
            "receipt": {
                "anyOf": [
                    receipt,
                    nullable,
                ]
            },
            "summary": {
                "anyOf": [
                    summary,
                    nullable,
                ]
            },
        },
        "oneOf": variants,
    }


SCHEMAS = {
    SCAN_REPORT_SCHEMA: scan_report_schema,
    SCAN_RECORD_SCHEMA: scan_record_schema,
    DELETION_PLAN_SCHEMA: deletion_plan_schema,
    PLAN_RECORD_SCHEMA: plan_record_schema,
    APPLY_REPORT_SCHEMA: apply_report_schema,
    QUERY_REPORT_SCHEMA: query_report_schema,
    DOCTOR_REPORT_SCHEMA: doctor_report_schema,
    QUARANTINE_OPERATION_SCHEMA: quarantine_operation_schema,
    QUARANTINE_LIST_SCHEMA: quarantine_list_schema,
    QUARANTINE_ACTION_SCHEMA: quarantine_action_schema,
    VIDEO_CAPABILITIES_SCHEMA: video_capabilities_schema,
    VIDEO_ANALYSIS_SCHEMA: video_analysis_schema,
    VIDEO_COMPARISON_SCHEMA: video_comparison_schema,
    VIDEO_LIBRARY_GROUP_SCHEMA: video_library_group_schema,
    VIDEO_LIBRARY_RECORD_SCHEMA: video_library_record_schema,
    VIDEO_LIBRARY_SCAN_SCHEMA: video_library_scan_schema,
}


def get_schema(name: str) -> Dict[str, Any]:
    try:
        return SCHEMAS[name]()
    except KeyError:
        raise KeyError("Unknown schema: {}".format(name))
