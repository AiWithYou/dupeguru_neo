# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Standalone parser and runner helpers for the durable catalog CLI."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, TextIO, Tuple

from core import __version__
from core.catalog import (
    Catalog,
    CatalogError,
    CatalogIntegrityError,
    CatalogSchemaError,
    CatalogStateError,
    preflight_catalog_path,
)
from core.catalog_indexer import CatalogIndexError
from core.catalog_service import (
    CatalogService,
    CatalogServiceError,
    CatalogServiceResult,
    CatalogServiceStatus,
)
from core.catalog_worker import CatalogWorkerError, VerifiedExactGroup
from core.safe_json import JsonStructuralLimits, preflight_json_structure

CATALOG_SCHEMA_VERSION = 1
CATALOG_RESULT_SCHEMA = "dupeguru.catalog-result"
CATALOG_STATUS_SCHEMA = "dupeguru.catalog-status"
CATALOG_GROUP_RECORD_SCHEMA = "dupeguru.catalog-group-record-v2"
CATALOG_GROUP_RECORD_SCHEMA_VERSION = 2
CATALOG_CHANGE_RECORD_SCHEMA = "dupeguru.catalog-change-record"
CATALOG_BACKUP_SCHEMA = "dupeguru.catalog-backup"
CATALOG_ERROR_SCHEMA = "dupeguru.catalog-error"

DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_WORK_ITEMS = 1_000_000
MAXIMUM_BATCH_SIZE = 10_000
CATALOG_MACHINE_MAX_LINE_BYTES = 8 * 1024 * 1024
CATALOG_MACHINE_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
CATALOG_MACHINE_MAX_RECORDS = 4_000_000
CATALOG_MAX_GROUPS = 1_000_000
CATALOG_MAX_GROUP_MEMBERS = 1_000_000
CATALOG_GROUP_PAGE_MAX_FILES = CATALOG_MAX_GROUP_MEMBERS
CATALOG_GROUP_CHUNK_MAX_MEMBERS = 40_000
CATALOG_MAX_CHANGES = CATALOG_MACHINE_MAX_RECORDS - 2
_CATALOG_MACHINE_COPY_BYTES = 1024 * 1024
_CATALOG_MACHINE_JSON_LIMITS = JsonStructuralLimits(
    max_depth=16,
    max_container_entries=200_000,
    max_total_nodes=500_000,
    max_scalar_tokens=500_000,
    max_total_string_chars=CATALOG_MACHINE_MAX_LINE_BYTES,
    max_string_chars=CATALOG_MACHINE_MAX_LINE_BYTES,
    max_scalar_chars=1024,
)


class CatalogExitCode(IntEnum):
    OK = 0
    INPUT_ERROR = 3
    PARTIAL = 6
    CANCELLED = 8
    RESOURCE_LIMIT = 9
    FAILED = 10


class _MachineOutputError(Exception):
    """Base class for failures that must leave machine stdout unpublished."""


class _MachineOutputResourceLimit(_MachineOutputError):
    pass


class _MachineOutputEncodingError(_MachineOutputError):
    pass


class _MachineOutputSpoolError(_MachineOutputError):
    pass


class _MachineOutputPublicationError(_MachineOutputError):
    """stdout publication failed and may already have written a prefix."""


class _MachineOutputSpool:
    """Private bounded staging file for one complete catalog machine response."""

    def __init__(self) -> None:
        self._file = None
        self.records = 0
        self.total_bytes = 0
        try:
            temporary = tempfile.TemporaryFile(
                mode="w+b",
                prefix="dupeguru-catalog-output-",
            )
            self._file = temporary
            file_stat = os.fstat(temporary.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise _MachineOutputSpoolError("catalog output spool is not a plain regular file")
            if hasattr(os, "geteuid") and int(file_stat.st_uid) != int(os.geteuid()):
                raise _MachineOutputSpoolError("catalog output spool is not owned by the current user")
            if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise _MachineOutputSpoolError("catalog output spool permissions are not private")
        except _MachineOutputError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise
        except (OSError, TypeError, ValueError) as error:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise _MachineOutputSpoolError("catalog output spool could not be created safely") from error

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        self.close()

    def close(self) -> None:
        temporary = self._file
        self._file = None
        if temporary is not None:
            try:
                temporary.close()
            except OSError:
                pass

    def checkpoint(self) -> Tuple[int, int, int]:
        if self._file is None:
            raise _MachineOutputSpoolError("catalog output spool is closed")
        try:
            return int(self._file.tell()), self.records, self.total_bytes
        except OSError as error:
            raise _MachineOutputSpoolError("catalog output spool position is unavailable") from error

    def rollback(self, checkpoint: Tuple[int, int, int]) -> None:
        if self._file is None:
            raise _MachineOutputSpoolError("catalog output spool is closed")
        position, records, total_bytes = checkpoint
        try:
            self._file.seek(position)
            self._file.truncate(position)
        except OSError as error:
            raise _MachineOutputSpoolError("catalog output spool could not roll back a record group") from error
        self.records = int(records)
        self.total_bytes = int(total_bytes)

    def write_record(self, payload: Mapping[str, Any]) -> None:
        if self._file is None:
            raise _MachineOutputSpoolError("catalog output spool is closed")
        encoded = _encode_machine_json_line(payload)
        next_records = self.records + 1
        next_total = self.total_bytes + len(encoded)
        if next_records > CATALOG_MACHINE_MAX_RECORDS:
            raise _MachineOutputResourceLimit(
                "catalog machine output exceeds the {} record limit".format(
                    CATALOG_MACHINE_MAX_RECORDS,
                )
            )
        if next_total > CATALOG_MACHINE_MAX_TOTAL_BYTES:
            raise _MachineOutputResourceLimit(
                "catalog machine output exceeds the {} byte total limit".format(
                    CATALOG_MACHINE_MAX_TOTAL_BYTES,
                )
            )
        try:
            written = self._file.write(encoded)
        except OSError as error:
            raise _MachineOutputSpoolError("catalog output spool write failed") from error
        if written != len(encoded):
            raise _MachineOutputSpoolError("catalog output spool accepted a short write")
        self.records = next_records
        self.total_bytes = next_total

    def publish(self, stdout: TextIO) -> None:
        if self._file is None:
            raise _MachineOutputSpoolError("catalog output spool is closed")
        try:
            self._file.flush()
            file_stat = os.fstat(self._file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or int(file_stat.st_size) != self.total_bytes:
                raise _MachineOutputSpoolError("catalog output spool changed before publication")
            self._validate_staged_output()
            self._file.seek(0)
        except _MachineOutputError:
            raise
        except OSError as error:
            raise _MachineOutputSpoolError("catalog output spool could not be finalized") from error

        try:
            binary_stdout = getattr(stdout, "buffer", None)
            if binary_stdout is not None:
                stdout.flush()
                while True:
                    chunk = self._file.read(_CATALOG_MACHINE_COPY_BYTES)
                    if not chunk:
                        break
                    _write_complete_bytes(binary_stdout, chunk)
                binary_stdout.flush()
                return
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            while True:
                chunk = self._file.read(_CATALOG_MACHINE_COPY_BYTES)
                if not chunk:
                    break
                text = decoder.decode(chunk, final=False)
                _write_complete_text(stdout, text)
            _write_complete_text(stdout, decoder.decode(b"", final=True))
            stdout.flush()
        except _MachineOutputPublicationError:
            raise
        except (OSError, TypeError, UnicodeError, ValueError) as error:
            raise _MachineOutputPublicationError("catalog stdout publication failed") from error

    def _validate_staged_output(self) -> None:
        if self._file is None:
            raise _MachineOutputSpoolError("catalog output spool is closed")
        validator = _CatalogMachineStreamValidator()
        records = 0
        total_bytes = 0
        try:
            self._file.seek(0)
            while True:
                encoded = self._file.readline(CATALOG_MACHINE_MAX_LINE_BYTES + 1)
                if not encoded:
                    break
                records += 1
                total_bytes += len(encoded)
                if len(encoded) > CATALOG_MACHINE_MAX_LINE_BYTES:
                    raise _MachineOutputResourceLimit("catalog staged output contains an over-limit physical line")
                if not encoded.endswith(b"\n"):
                    raise _MachineOutputEncodingError("catalog staged output contains an unterminated physical line")
                try:
                    text = encoded[:-1].decode("utf-8", errors="strict")
                    preflight_json_structure(
                        text,
                        limits=_CATALOG_MACHINE_JSON_LIMITS,
                        label="catalog staged output record",
                    )
                    payload = json.loads(text)
                except (UnicodeDecodeError, ValueError) as error:
                    raise _MachineOutputEncodingError("catalog staged output is not strict bounded JSONL") from error
                _validate_machine_payload_schema(payload)
                validator.consume(payload)
            validator.finish()
        except _MachineOutputError:
            raise
        except (MemoryError, RecursionError) as error:
            raise _MachineOutputResourceLimit(
                "catalog staged output validation exhausted its resource budget"
            ) from error
        except (OSError, TypeError, ValueError) as error:
            raise _MachineOutputSpoolError("catalog staged output validation failed") from error
        if records != self.records or total_bytes != self.total_bytes:
            raise _MachineOutputSpoolError("catalog staged output record or byte count changed before publication")


def _write_complete_bytes(stream, value: bytes) -> None:
    if not value:
        return
    try:
        written = stream.write(value)
    except (OSError, TypeError, ValueError) as error:
        raise _MachineOutputPublicationError("catalog stdout publication failed") from error
    if type(written) is not int or written != len(value):
        raise _MachineOutputPublicationError("catalog stdout publication produced a short binary write")


def _write_complete_text(stream: TextIO, value: str) -> None:
    if not value:
        return
    try:
        written = stream.write(value)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise _MachineOutputPublicationError("catalog stdout publication failed") from error
    if written is not None and int(written) != len(value):
        raise _MachineOutputPublicationError("catalog stdout publication produced a short write")


def _encode_machine_json_line(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except MemoryError as error:
        raise _MachineOutputResourceLimit("catalog JSON encoding exhausted memory") from error
    except RecursionError as error:
        raise _MachineOutputEncodingError("catalog JSON exceeds the structural depth limit") from error
    except (TypeError, ValueError) as error:
        raise _MachineOutputEncodingError("catalog machine output cannot be encoded as JSON") from error
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _MachineOutputEncodingError("catalog machine output is not strict UTF-8") from error
    if len(encoded) + 1 > CATALOG_MACHINE_MAX_LINE_BYTES:
        raise _MachineOutputResourceLimit(
            "catalog machine output line exceeds the {} byte limit".format(
                CATALOG_MACHINE_MAX_LINE_BYTES,
            )
        )
    try:
        preflight_json_structure(
            text,
            limits=_CATALOG_MACHINE_JSON_LIMITS,
            label="catalog machine output record",
        )
    except ValueError as error:
        raise _MachineOutputEncodingError("catalog machine output exceeds structural limits") from error
    return encoded + b"\n"


def _encode_machine_json_value(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return text.encode("utf-8", errors="strict")
    except MemoryError as error:
        raise _MachineOutputResourceLimit("catalog JSON value encoding exhausted memory") from error
    except UnicodeEncodeError as error:
        raise _MachineOutputEncodingError("catalog machine output is not strict UTF-8") from error
    except RecursionError as error:
        raise _MachineOutputEncodingError("catalog JSON value exceeds the structural depth limit") from error
    except (TypeError, ValueError) as error:
        raise _MachineOutputEncodingError("catalog machine value cannot be encoded as JSON") from error


def _issue_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
        },
    }


def _safety_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "database_location",
            "verification",
            "complete_scan_required",
            "allows_automatic_destructive_action",
            "fresh_action_proof_required",
            "destructive_workflow",
        ],
        "properties": {
            "database_location": {"const": "local"},
            "verification": {"const": "sha256+byte-compare"},
            "complete_scan_required": {"const": True},
            "allows_automatic_destructive_action": {"const": False},
            "fresh_action_proof_required": {"const": True},
            "destructive_workflow": {"const": "quarantine_then_explicit_finalize"},
        },
    }


def _change_safety_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "database_location",
            "verification",
            "complete_scan_required",
            "move_classification",
            "allows_automatic_destructive_action",
            "fresh_action_proof_required",
            "destructive_workflow",
        ],
        "properties": {
            "database_location": {"const": "local"},
            "verification": {"const": "immutable-complete-snapshot-diff"},
            "complete_scan_required": {"const": True},
            "move_classification": {"const": "stable-native-identity-one-to-one-only"},
            "allows_automatic_destructive_action": {"const": False},
            "fresh_action_proof_required": {"const": True},
            "destructive_workflow": {"const": "quarantine_then_explicit_finalize"},
        },
    }


def _counts_schema(names: Sequence[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(names),
        "properties": {name: {"type": "integer", "minimum": 0} for name in names},
    }


def _status_value_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "scan_id",
            "status",
            "phase",
            "directory_counts",
            "work_counts",
            "error_count",
            "verified_projection_allowed",
            "started_at",
            "finished_at",
        ],
        "properties": {
            "scan_id": {"type": "integer", "minimum": 1},
            "status": {
                "enum": [
                    "running",
                    "cancelled",
                    "complete",
                    "completed_with_errors",
                    "failed",
                ]
            },
            "phase": {"type": "string", "minLength": 1},
            "directory_counts": _counts_schema(
                (
                    "complete",
                    "failed",
                    "in_progress",
                    "pending",
                    "unreachable",
                    "total",
                )
            ),
            "work_counts": _counts_schema(("complete", "failed", "in_progress", "pending", "total")),
            "error_count": {"type": "integer", "minimum": 0},
            "verified_projection_allowed": {"type": "boolean"},
            "started_at": {"type": "number", "minimum": 0},
            "finished_at": {"type": ["number", "null"], "minimum": 0},
        },
    }


def _service_result_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "scan_id",
            "outcome",
            "catalog_status",
            "roots_total",
            "roots_processed",
            "files_observed",
            "changed_content",
            "work_enqueued",
            "worker_batches",
            "work_completed",
            "work_retried",
            "work_failed",
            "status",
            "errors",
        ],
        "properties": {
            "scan_id": {"type": "integer", "minimum": 1},
            "outcome": {"enum": ["finished", "partial"]},
            "catalog_status": {
                "enum": [
                    "running",
                    "cancelled",
                    "complete",
                    "completed_with_errors",
                    "failed",
                ]
            },
            "roots_total": {"type": "integer", "minimum": 1},
            "roots_processed": {"type": "integer", "minimum": 0},
            "files_observed": {"type": "integer", "minimum": 0},
            "changed_content": {"type": "integer", "minimum": 0},
            "work_enqueued": {"type": "integer", "minimum": 0},
            "worker_batches": {"type": "integer", "minimum": 0},
            "work_completed": {"type": "integer", "minimum": 0},
            "work_retried": {"type": "integer", "minimum": 0},
            "work_failed": {"type": "integer", "minimum": 0},
            "status": _status_value_schema(),
            "errors": {"type": "array", "items": {"type": "string"}},
        },
    }


def _limits_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "max_work_items",
            "batch_size",
            "max_worker_batches",
            "effective_capacity",
        ],
        "properties": {
            "max_work_items": {"type": "integer", "minimum": 1},
            "batch_size": {"type": "integer", "minimum": 1},
            "max_worker_batches": {"type": "integer", "minimum": 1},
            "effective_capacity": {"type": "integer", "minimum": 1},
        },
    }


CATALOG_RESULT_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-result:1",
    "title": "dupeGuru Neo catalog scan or resume result",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "created_at",
        "command",
        "state",
        "partial",
        "database",
        "issues",
        "limits",
        "result",
        "safety",
    ],
    "properties": {
        "schema": {"const": CATALOG_RESULT_SCHEMA},
        "schema_version": {"const": CATALOG_SCHEMA_VERSION},
        "created_at": {"type": "string", "minLength": 1},
        "command": {"enum": ["scan", "resume"]},
        "state": {
            "enum": [
                "complete",
                "partial",
                "cancelled",
                "resource_limited",
                "failed",
            ]
        },
        "partial": {"type": "boolean"},
        "database": {"type": "string", "minLength": 1},
        "issues": {"type": "array", "items": _issue_schema()},
        "limits": _limits_schema(),
        "result": _service_result_schema(),
        "safety": _safety_schema(),
    },
}

CATALOG_STATUS_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-status:1",
    "title": "dupeGuru Neo catalog scan status",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "created_at",
        "state",
        "partial",
        "database",
        "issues",
        "status",
        "safety",
    ],
    "properties": {
        "schema": {"const": CATALOG_STATUS_SCHEMA},
        "schema_version": {"const": CATALOG_SCHEMA_VERSION},
        "created_at": {"type": "string", "minLength": 1},
        "state": {"enum": ["complete", "running", "partial", "cancelled", "failed"]},
        "partial": {"type": "boolean"},
        "database": {"type": "string", "minLength": 1},
        "issues": {"type": "array", "items": _issue_schema()},
        "status": _status_value_schema(),
        "safety": _safety_schema(),
    },
}

_CATALOG_GROUP_HEADER_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "group_id",
        "size",
        "sha256",
        "total_members",
        "total_verifications",
        "verification",
        "safety_state",
        "allows_automatic_destructive_action",
    ],
    "properties": {
        "group_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "size": {"type": "integer", "minimum": 0},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "total_members": {
            "type": "integer",
            "minimum": 2,
            "maximum": CATALOG_MAX_GROUP_MEMBERS,
        },
        "total_verifications": {
            "type": "integer",
            "minimum": 1,
            "maximum": CATALOG_MAX_GROUP_MEMBERS - 1,
        },
        "verification": {"const": "verified_exact"},
        "safety_state": {"const": "verified_exact_requires_fresh_action_proof"},
        "allows_automatic_destructive_action": {"const": False},
    },
}

_CATALOG_GROUP_MEMBER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "path",
        "path_id",
        "physical_file_id",
        "content_version_id",
        "verification_id",
    ],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "path_id": {"type": "integer", "minimum": 1},
        "physical_file_id": {"type": "integer", "minimum": 1},
        "content_version_id": {"type": "integer", "minimum": 1},
        "verification_id": {
            "type": ["integer", "null"],
            "minimum": 1,
        },
    },
}

_CATALOG_GROUP_MEMBER_CHUNK_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "group_id",
        "chunk_index",
        "first_member_index",
        "members",
    ],
    "properties": {
        "group_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "chunk_index": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MACHINE_MAX_RECORDS - 1,
        },
        "first_member_index": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_GROUP_MEMBERS - 1,
        },
        "members": {
            "type": "array",
            "minItems": 1,
            "maxItems": CATALOG_GROUP_CHUNK_MAX_MEMBERS,
            "items": _CATALOG_GROUP_MEMBER_SCHEMA,
        },
    },
}

_CATALOG_GROUP_END_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "group_id",
        "chunk_count",
        "total_members",
        "total_verifications",
        "verification_complete",
    ],
    "properties": {
        "group_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "chunk_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": CATALOG_MACHINE_MAX_RECORDS - 1,
        },
        "total_members": {
            "type": "integer",
            "minimum": 2,
            "maximum": CATALOG_MAX_GROUP_MEMBERS,
        },
        "total_verifications": {
            "type": "integer",
            "minimum": 1,
            "maximum": CATALOG_MAX_GROUP_MEMBERS - 1,
        },
        "verification_complete": {"const": True},
    },
}

_CATALOG_GROUP_SUMMARY_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["groups", "files", "member_chunks", "page_size"],
    "properties": {
        "groups": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_GROUPS,
        },
        "files": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_GROUPS * CATALOG_MAX_GROUP_MEMBERS,
        },
        "member_chunks": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MACHINE_MAX_RECORDS - 2,
        },
        "page_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAXIMUM_BATCH_SIZE,
        },
    },
}

CATALOG_GROUP_RECORD_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-group-record:2",
    "title": "dupeGuru Neo chunked streaming verified catalog group record v2",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "record_type",
        "created_at",
        "scan_id",
        "state",
        "partial",
        "database",
        "issues",
        "group_header",
        "member_chunk",
        "group_end",
        "summary",
        "safety",
    ],
    "properties": {
        "schema": {"const": CATALOG_GROUP_RECORD_SCHEMA},
        "schema_version": {"const": CATALOG_GROUP_RECORD_SCHEMA_VERSION},
        "record_type": {
            "enum": [
                "header",
                "group_header",
                "member_chunk",
                "group_end",
                "summary",
            ]
        },
        "created_at": {"type": "string", "minLength": 1},
        "scan_id": {"type": "integer", "minimum": 1},
        "state": {"enum": ["streaming", "complete", "partial", "failed"]},
        "partial": {"type": "boolean"},
        "database": {"type": "string", "minLength": 1},
        "issues": {"type": "array", "items": _issue_schema()},
        "group_header": _CATALOG_GROUP_HEADER_SCHEMA,
        "member_chunk": _CATALOG_GROUP_MEMBER_CHUNK_SCHEMA,
        "group_end": _CATALOG_GROUP_END_SCHEMA,
        "summary": _CATALOG_GROUP_SUMMARY_SCHEMA,
        "safety": _safety_schema(),
    },
}

_CATALOG_CHANGE_SNAPSHOT_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "observation_id",
        "root_id",
        "path_id",
        "physical_file_id",
        "content_version_id",
        "path",
        "path_state",
        "content_state",
        "identity_confidence",
    ],
    "properties": {
        "observation_id": {"type": "integer", "minimum": 1},
        "root_id": {"type": "integer", "minimum": 1},
        "path_id": {"type": "integer", "minimum": 1},
        "physical_file_id": {"type": "integer", "minimum": 1},
        "content_version_id": {"type": "integer", "minimum": 1},
        "path": {"type": "string", "minLength": 1},
        "path_state": {
            "enum": ["active", "missing", "unreadable"],
        },
        "content_state": {
            "enum": ["stable", "unstable", "unreadable", "missing"],
        },
        "identity_confidence": {
            "enum": ["stable", "session_only", "path_only"],
        },
    },
}

_CATALOG_CHANGE_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "change_id",
        "change_type",
        "old",
        "new",
        "content_changed",
        "move_identity_proven",
        "classification",
        "allows_automatic_destructive_action",
    ],
    "properties": {
        "change_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "change_type": {
            "enum": ["added", "modified", "moved", "missing"],
        },
        "old": _CATALOG_CHANGE_SNAPSHOT_SCHEMA,
        "new": _CATALOG_CHANGE_SNAPSHOT_SCHEMA,
        "content_changed": {"type": "boolean"},
        "move_identity_proven": {"type": "boolean"},
        "classification": {
            "enum": [
                "immutable_snapshot_added",
                "immutable_snapshot_modified",
                "stable_native_identity_1_to_1_move",
                "immutable_snapshot_missing",
            ],
        },
        "allows_automatic_destructive_action": {"const": False},
    },
}

_CATALOG_CHANGE_SUMMARY_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": [
        "total",
        "added",
        "modified",
        "moved",
        "missing",
        "page_size",
    ],
    "properties": {
        "total": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_CHANGES,
        },
        "added": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_CHANGES,
        },
        "modified": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_CHANGES,
        },
        "moved": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_CHANGES,
        },
        "missing": {
            "type": "integer",
            "minimum": 0,
            "maximum": CATALOG_MAX_CHANGES,
        },
        "page_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAXIMUM_BATCH_SIZE,
        },
    },
}

CATALOG_CHANGE_RECORD_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-change-record:1",
    "title": "dupeGuru Neo immutable catalog change stream record",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "record_type",
        "created_at",
        "state",
        "partial",
        "database",
        "before_scan_id",
        "after_scan_id",
        "root_ids",
        "issues",
        "change",
        "summary",
        "safety",
    ],
    "properties": {
        "schema": {"const": CATALOG_CHANGE_RECORD_SCHEMA},
        "schema_version": {"const": CATALOG_SCHEMA_VERSION},
        "record_type": {"enum": ["header", "change", "summary"]},
        "created_at": {"type": "string", "minLength": 1},
        "state": {"enum": ["streaming", "complete", "partial"]},
        "partial": {"type": "boolean"},
        "database": {"type": "string", "minLength": 1},
        "before_scan_id": {"type": "integer", "minimum": 1},
        "after_scan_id": {"type": "integer", "minimum": 1},
        "root_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
        "issues": {"type": "array", "items": _issue_schema()},
        "change": _CATALOG_CHANGE_SCHEMA,
        "summary": _CATALOG_CHANGE_SUMMARY_SCHEMA,
        "safety": _change_safety_schema(),
    },
}

CATALOG_BACKUP_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-backup:1",
    "title": "dupeGuru Neo catalog backup result",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "created_at",
        "state",
        "partial",
        "database",
        "destination",
        "issues",
        "integrity_checked",
        "overwrote_existing",
        "safety",
    ],
    "properties": {
        "schema": {"const": CATALOG_BACKUP_SCHEMA},
        "schema_version": {"const": CATALOG_SCHEMA_VERSION},
        "created_at": {"type": "string", "minLength": 1},
        "state": {"const": "complete"},
        "partial": {"const": False},
        "database": {"type": "string", "minLength": 1},
        "destination": {"type": "string", "minLength": 1},
        "issues": {"type": "array", "maxItems": 0},
        "integrity_checked": {"const": True},
        "overwrote_existing": {"const": False},
        "safety": _safety_schema(),
    },
}

CATALOG_ERROR_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dupeguru-neo:schema:catalog-error:1",
    "title": "dupeGuru Neo catalog command failure",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "created_at",
        "command",
        "state",
        "partial",
        "database",
        "issues",
    ],
    "properties": {
        "schema": {"const": CATALOG_ERROR_SCHEMA},
        "schema_version": {"const": CATALOG_SCHEMA_VERSION},
        "created_at": {"type": "string", "minLength": 1},
        "command": {
            "enum": [
                "scan",
                "resume",
                "status",
                "groups",
                "changes",
                "backup",
            ]
        },
        "state": {"const": "failed"},
        "partial": {"const": False},
        "database": {"type": "string"},
        "issues": {
            "type": "array",
            "minItems": 1,
            "items": _issue_schema(),
        },
    },
}

CATALOG_SCHEMAS = {
    "catalog-result": CATALOG_RESULT_JSON_SCHEMA,
    "catalog-status": CATALOG_STATUS_JSON_SCHEMA,
    "catalog-group-record": CATALOG_GROUP_RECORD_JSON_SCHEMA,
    "catalog-change-record": CATALOG_CHANGE_RECORD_JSON_SCHEMA,
    "catalog-backup": CATALOG_BACKUP_JSON_SCHEMA,
    "catalog-error": CATALOG_ERROR_JSON_SCHEMA,
}
_CATALOG_SCHEMA_BY_WIRE_NAME = {schema["properties"]["schema"]["const"]: schema for schema in CATALOG_SCHEMAS.values()}


def _schema_values_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return type(value) is int
    if expected_type == "number":
        return type(value) in (int, float)
    if expected_type == "boolean":
        return type(value) is bool
    return False


def _validate_schema_value(value: Any, schema: Mapping[str, Any], label: str) -> None:
    if "const" in schema and not _schema_values_equal(value, schema["const"]):
        raise _MachineOutputEncodingError("{} does not match its schema constant".format(label))
    if "enum" in schema and not any(_schema_values_equal(value, option) for option in schema["enum"]):
        raise _MachineOutputEncodingError("{} is not an allowed schema value".format(label))

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = (expected_types,)
    elif expected_types is None:
        expected_types = ()
    if expected_types and not any(_schema_type_matches(value, expected_type) for expected_type in expected_types):
        raise _MachineOutputEncodingError("{} has an invalid schema type".format(label))
    if value is None:
        return

    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        if not required.issubset(value):
            raise _MachineOutputEncodingError("{} is missing required fields".format(label))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and not set(value).issubset(properties):
            raise _MachineOutputEncodingError("{} contains unknown fields".format(label))
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                _validate_schema_value(item, child_schema, "{}.{}".format(label, key))
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise _MachineOutputEncodingError("{} contains too few items".format(label))
        maximum_items = schema.get("maxItems")
        if maximum_items is not None and len(value) > int(maximum_items):
            raise _MachineOutputEncodingError("{} contains too many items".format(label))
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, ensure_ascii=False, allow_nan=False, sort_keys=True) for item in value]
            if len(canonical) != len(set(canonical)):
                raise _MachineOutputEncodingError("{} contains duplicate items".format(label))
        child_schema = schema.get("items")
        if child_schema is not None:
            for index, item in enumerate(value):
                _validate_schema_value(
                    item,
                    child_schema,
                    "{}[{}]".format(label, index),
                )
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise _MachineOutputEncodingError("{} is shorter than allowed".format(label))
        maximum_length = schema.get("maxLength")
        if maximum_length is not None and len(value) > int(maximum_length):
            raise _MachineOutputEncodingError("{} is longer than allowed".format(label))
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise _MachineOutputEncodingError("{} does not match its schema pattern".format(label))
    elif type(value) in (int, float):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise _MachineOutputEncodingError("{} is below its schema minimum".format(label))
        if maximum is not None and value > maximum:
            raise _MachineOutputEncodingError("{} is above its schema maximum".format(label))


def _validate_machine_payload_schema(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise _MachineOutputEncodingError("catalog machine output record must be an object")
    schema_name = payload.get("schema")
    if not isinstance(schema_name, str):
        raise _MachineOutputEncodingError("catalog machine output record has no valid schema name")
    schema = _CATALOG_SCHEMA_BY_WIRE_NAME.get(schema_name)
    if schema is None:
        raise _MachineOutputEncodingError("catalog machine output uses an unknown schema")
    _validate_schema_value(payload, schema, "catalog machine output record")


class _CatalogMachineStreamValidator:
    """Validate cross-record ordering and counts without materializing a stream."""

    _SINGLE_RECORD_SCHEMAS = {
        CATALOG_RESULT_SCHEMA,
        CATALOG_STATUS_SCHEMA,
        CATALOG_BACKUP_SCHEMA,
        CATALOG_ERROR_SCHEMA,
    }

    def __init__(self) -> None:
        self.schema = None
        self.records = 0
        self.finished = False
        self.group_active = None
        self.group_count = 0
        self.group_file_count = 0
        self.group_chunk_count = 0
        self.change_counts = {
            "added": 0,
            "modified": 0,
            "moved": 0,
            "missing": 0,
        }

    def consume(self, payload: Mapping[str, Any]) -> None:
        schema = payload["schema"]
        if self.finished:
            raise _MachineOutputEncodingError("catalog machine output contains records after its terminal record")
        if self.schema is None:
            self.schema = schema
        elif schema != self.schema:
            raise _MachineOutputEncodingError("catalog machine output mixes incompatible schemas")
        self.records += 1

        if schema in self._SINGLE_RECORD_SCHEMAS:
            if self.records != 1:
                raise _MachineOutputEncodingError("catalog single-document output contains multiple records")
            self.finished = True
            return
        if schema == CATALOG_GROUP_RECORD_SCHEMA:
            self._consume_group(payload)
            return
        if schema == CATALOG_CHANGE_RECORD_SCHEMA:
            self._consume_change(payload)
            return
        raise _MachineOutputEncodingError("catalog machine output schema is not publishable")

    def finish(self) -> None:
        if self.records < 1:
            raise _MachineOutputEncodingError("catalog machine output is empty")
        if not self.finished:
            raise _MachineOutputEncodingError("catalog machine output has no valid terminal record")
        if self.group_active is not None:
            raise _MachineOutputEncodingError("catalog group stream ends inside an incomplete group")

    @staticmethod
    def _require_only(payload: Mapping[str, Any], selected: str) -> None:
        for name in ("group_header", "member_chunk", "group_end", "summary"):
            if name == selected:
                if payload[name] is None:
                    raise _MachineOutputEncodingError("catalog group record omits its selected payload")
            elif payload[name] is not None:
                raise _MachineOutputEncodingError("catalog group record contains conflicting payloads")

    @staticmethod
    def _require_complete_record(payload: Mapping[str, Any], label: str) -> None:
        if payload["state"] != "complete" or payload["partial"] is not False or payload["issues"]:
            raise _MachineOutputEncodingError("{} is not a complete issue-free record".format(label))

    def _consume_group(self, payload: Mapping[str, Any]) -> None:
        record_type = payload["record_type"]
        if self.records == 1:
            if (
                record_type != "header"
                or payload["state"] != "streaming"
                or payload["partial"] is not False
                or payload["issues"]
                or any(payload[name] is not None for name in ("group_header", "member_chunk", "group_end", "summary"))
            ):
                raise _MachineOutputEncodingError("catalog group stream does not start with a valid header")
            return
        if record_type == "header":
            raise _MachineOutputEncodingError("catalog group stream contains a repeated header")
        if record_type == "group_header":
            self._require_only(payload, "group_header")
            self._require_complete_record(payload, "catalog group header")
            if self.group_active is not None:
                raise _MachineOutputEncodingError("catalog group stream starts a group before ending the prior group")
            header = payload["group_header"]
            if header["total_verifications"] != header["total_members"] - 1:
                raise _MachineOutputEncodingError("catalog group header has an invalid verification count")
            self.group_active = {
                "group_id": header["group_id"],
                "members": 0,
                "verifications": 0,
                "chunks": 0,
                "expected_members": header["total_members"],
                "expected_verifications": header["total_verifications"],
            }
            return
        if record_type == "member_chunk":
            self._require_only(payload, "member_chunk")
            self._require_complete_record(payload, "catalog group member chunk")
            if self.group_active is None:
                raise _MachineOutputEncodingError("catalog group stream contains an orphan member chunk")
            chunk = payload["member_chunk"]
            active = self.group_active
            if (
                chunk["group_id"] != active["group_id"]
                or chunk["chunk_index"] != active["chunks"]
                or chunk["first_member_index"] != active["members"]
            ):
                raise _MachineOutputEncodingError("catalog group stream contains a discontinuous member chunk")
            for offset, member in enumerate(chunk["members"]):
                member_index = active["members"] + offset
                verification_id = member["verification_id"]
                if (member_index == 0) != (verification_id is None):
                    raise _MachineOutputEncodingError("catalog group stream has an invalid verification assignment")
                if verification_id is not None:
                    active["verifications"] += 1
            active["members"] += len(chunk["members"])
            active["chunks"] += 1
            if active["members"] > active["expected_members"]:
                raise _MachineOutputEncodingError("catalog group stream contains too many members")
            return
        if record_type == "group_end":
            self._require_only(payload, "group_end")
            self._require_complete_record(payload, "catalog group terminator")
            if self.group_active is None:
                raise _MachineOutputEncodingError("catalog group stream contains an orphan group terminator")
            end = payload["group_end"]
            active = self.group_active
            expected = (
                active["group_id"],
                active["chunks"],
                active["members"],
                active["verifications"],
            )
            actual = (
                end["group_id"],
                end["chunk_count"],
                end["total_members"],
                end["total_verifications"],
            )
            if actual != expected or (
                active["members"] != active["expected_members"]
                or active["verifications"] != active["expected_verifications"]
                or active["verifications"] != active["members"] - 1
            ):
                raise _MachineOutputEncodingError("catalog group terminator does not match its member chunks")
            self.group_count += 1
            self.group_file_count += active["members"]
            self.group_chunk_count += active["chunks"]
            self.group_active = None
            return
        if record_type == "summary":
            self._require_only(payload, "summary")
            if self.group_active is not None:
                raise _MachineOutputEncodingError("catalog group stream summarizes an incomplete group")
            summary = payload["summary"]
            if (
                summary["groups"] != self.group_count
                or summary["files"] != self.group_file_count
                or summary["member_chunks"] != self.group_chunk_count
            ):
                raise _MachineOutputEncodingError("catalog group summary counts do not match the stream")
            if payload["state"] not in ("complete", "partial") or (
                (payload["state"] == "partial") != payload["partial"]
            ):
                raise _MachineOutputEncodingError("catalog group summary has inconsistent partial state")
            if (payload["state"] == "complete" and payload["issues"]) or (
                payload["state"] == "partial" and not payload["issues"]
            ):
                raise _MachineOutputEncodingError("catalog group summary has inconsistent issues")
            self.finished = True
            return
        raise _MachineOutputEncodingError("catalog group stream has an unknown record type")

    def _consume_change(self, payload: Mapping[str, Any]) -> None:
        record_type = payload["record_type"]
        if self.records == 1:
            if (
                record_type != "header"
                or payload["state"] != "streaming"
                or payload["partial"] is not False
                or payload["issues"]
                or payload["change"] is not None
                or payload["summary"] is not None
            ):
                raise _MachineOutputEncodingError("catalog change stream does not start with a valid header")
            return
        if record_type == "change":
            if payload["change"] is None or payload["summary"] is not None:
                raise _MachineOutputEncodingError("catalog change record has conflicting payloads")
            self._require_complete_record(payload, "catalog change")
            self.change_counts[payload["change"]["change_type"]] += 1
            return
        if record_type == "summary":
            if payload["change"] is not None or payload["summary"] is None:
                raise _MachineOutputEncodingError("catalog change summary has conflicting payloads")
            summary = payload["summary"]
            expected_total = sum(self.change_counts.values())
            if summary["total"] != expected_total or any(
                summary[name] != count for name, count in self.change_counts.items()
            ):
                raise _MachineOutputEncodingError("catalog change summary counts do not match the stream")
            if payload["state"] not in ("complete", "partial") or (
                (payload["state"] == "partial") != payload["partial"]
            ):
                raise _MachineOutputEncodingError("catalog change summary has inconsistent partial state")
            if (payload["state"] == "complete" and payload["issues"]) or (
                payload["state"] == "partial" and not payload["issues"]
            ):
                raise _MachineOutputEncodingError("catalog change summary has inconsistent issues")
            self.finished = True
            return
        raise _MachineOutputEncodingError("catalog change stream contains an invalid record sequence")


def add_catalog_parser(subparsers) -> argparse.ArgumentParser:
    """Add the isolated ``catalog`` command tree to an existing parser."""

    parser = subparsers.add_parser(
        "catalog",
        help="scan and query a durable local metadata catalog",
        description=(
            "The catalog SQLite database must be on a local filesystem. "
            "Partial results use exit 6, cancellation 8, and resource limits 9."
        ),
    )
    commands = parser.add_subparsers(dest="catalog_command", required=True)

    scan = commands.add_parser("scan", help="start a new durable catalog scan")
    scan.add_argument("database", help="local SQLite catalog path")
    scan.add_argument("roots", nargs="+", help="one or more library roots")
    scan.add_argument(
        "--max-work-items",
        type=_positive_int,
        default=DEFAULT_MAX_WORK_ITEMS,
        help="maximum analysis items processed in this invocation",
    )
    scan.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help="bounded worker batch size",
    )

    resume = commands.add_parser("resume", help="resume one durable running scan")
    resume.add_argument("database", help="existing local SQLite catalog path")
    resume.add_argument("scan_id", type=_positive_int, help="durable scan identifier")

    status = commands.add_parser("status", help="show durable state for one scan")
    status.add_argument("database", help="existing local SQLite catalog path")
    status.add_argument("scan_id", type=_positive_int, help="durable scan identifier")

    groups = commands.add_parser(
        "groups",
        help="stream byte-verified exact groups from the latest complete scan",
    )
    groups.add_argument("database", help="existing local SQLite catalog path")
    groups.add_argument(
        "--page-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help="keyset page size for verified groups",
    )

    changes = commands.add_parser(
        "changes",
        help="stream proven changes between two immutable complete scans",
    )
    changes.add_argument("database", help="existing local SQLite catalog path")
    changes.add_argument(
        "--from",
        dest="before_scan_id",
        type=_positive_int,
        required=True,
        help="earlier complete scan identifier",
    )
    changes.add_argument(
        "--to",
        dest="after_scan_id",
        type=_positive_int,
        required=True,
        help="later complete scan identifier",
    )
    changes.add_argument(
        "--page-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help="bounded keyset page size",
    )

    backup = commands.add_parser("backup", help="create an integrity-checked catalog backup")
    backup.add_argument("database", help="existing local SQLite catalog path")
    backup.add_argument("destination", help="new local backup path; never overwritten")
    return parser


def run_catalog_command(
    args,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Execute one command and publish only a complete validated machine stream."""

    del stdin
    try:
        with _MachineOutputSpool() as spool:
            command_checkpoint = spool.checkpoint()
            try:
                exit_code = _dispatch_catalog_command(args, spool)
            except (
                CatalogError,
                CatalogIndexError,
                CatalogServiceError,
                CatalogWorkerError,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                spool.rollback(command_checkpoint)
                _write_error(args, error, spool, stderr)
                exit_code = int(_exception_exit_code(error))
            spool.publish(stdout)
            return int(exit_code)
    except _MachineOutputResourceLimit as error:
        _write_diagnostic(stderr, "{}: {}\n".format(type(error).__name__, error))
        return int(CatalogExitCode.RESOURCE_LIMIT)
    except MemoryError:
        _write_diagnostic(stderr, "MemoryError: catalog machine output exhausted memory\n")
        return int(CatalogExitCode.RESOURCE_LIMIT)
    except (
        _MachineOutputEncodingError,
        _MachineOutputPublicationError,
        _MachineOutputSpoolError,
    ) as error:
        _write_diagnostic(stderr, "{}: {}\n".format(type(error).__name__, error))
        return int(CatalogExitCode.FAILED)


def _dispatch_catalog_command(args, output: _MachineOutputSpool) -> int:
    command = getattr(args, "catalog_command", "")
    if command == "scan":
        return _run_scan(args, output)
    if command == "resume":
        return _run_resume(args, output)
    if command == "status":
        return _run_status(args, output)
    if command == "groups":
        return _run_groups(args, output)
    if command == "changes":
        return _run_changes(args, output)
    if command == "backup":
        return _run_backup(args, output)
    raise ValueError("unknown catalog command: {!r}".format(command))


def _run_scan(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=False)
    roots = tuple(_root_path(root) for root in args.roots)
    limits = _limits(args.max_work_items, args.batch_size)
    with CatalogService(
        database,
        roots,
        worker_batch_size=limits["batch_size"],
        max_worker_batches=limits["max_worker_batches"],
    ) as service:
        result = service.run(app_version=__version__)
        if result.catalog_status == "complete" and result.outcome == "finished":
            for _group in service.iter_verified_exact_groups(page_size=limits["batch_size"]):
                pass
    return _write_service_result("scan", database, result, limits, stdout)


def _run_resume(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=True)
    roots = _scan_roots(database, args.scan_id)
    limits = _limits(DEFAULT_MAX_WORK_ITEMS, DEFAULT_BATCH_SIZE)
    with CatalogService(
        database,
        roots,
        worker_batch_size=limits["batch_size"],
        max_worker_batches=limits["max_worker_batches"],
    ) as service:
        result = service.resume(args.scan_id)
        if result.catalog_status == "complete" and result.outcome == "finished":
            for _group in service.iter_verified_exact_groups(page_size=limits["batch_size"]):
                pass
    return _write_service_result("resume", database, result, limits, stdout)


def _run_status(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=True)
    with Catalog.open_read_only(database) as catalog:
        status = CatalogService.status_for_catalog(catalog, args.scan_id)
    state, partial, exit_code = _status_state(status)
    issues = []
    if status.error_count:
        issues.append(
            {
                "code": "catalog_errors_recorded",
                "message": "{} durable catalog error(s) were recorded".format(status.error_count),
            }
        )
    payload = {
        "schema": CATALOG_STATUS_SCHEMA,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "state": state,
        "partial": partial,
        "database": str(database),
        "issues": issues,
        "status": status.to_dict(),
        "safety": _safety(),
    }
    _write_json_line(stdout, payload)
    return int(exit_code)


def _run_groups(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=True)
    scan_id, roots, root_ids = _latest_complete_scan_roots(database)
    catalog = Catalog.open_read_only(database)
    try:
        service = CatalogService(
            database,
            roots,
            catalog=catalog,
            selected_root_ids=root_ids,
        )
    except BaseException:
        catalog.close()
        raise
    with service:
        status = service.status(scan_id)
        if status.status != "complete" or not status.verified_projection_allowed:
            raise CatalogStateError("verified groups require a complete, currently projectable scan")
        projection = service.verified_exact_projection_counts()
        if projection.group_count > CATALOG_MAX_GROUPS:
            raise _MachineOutputResourceLimit(
                "catalog group output exceeds the {} group limit".format(
                    CATALOG_MAX_GROUPS,
                )
            )
        if projection.max_group_members > CATALOG_MAX_GROUP_MEMBERS:
            raise _MachineOutputResourceLimit(
                "one catalog group exceeds the {} member limit".format(
                    CATALOG_MAX_GROUP_MEMBERS,
                )
            )
        return _stream_groups(service, database, scan_id, args.page_size, stdout)


def _run_changes(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=True)
    if args.before_scan_id >= args.after_scan_id:
        raise ValueError("--from must identify an earlier scan than --to")
    with Catalog.open_read_only(database) as catalog:
        root_ids = _comparison_root_ids(
            catalog,
            args.before_scan_id,
            args.after_scan_id,
        )
        first_page = catalog.page_scan_changes(
            args.before_scan_id,
            args.after_scan_id,
            root_ids,
            limit=args.page_size,
        )
        return _stream_changes(
            catalog,
            database,
            args.before_scan_id,
            args.after_scan_id,
            root_ids,
            args.page_size,
            first_page,
            stdout,
        )


def _run_backup(args, stdout: TextIO) -> int:
    database = _database_path(args.database, must_exist=True)
    destination = _database_path(args.destination, must_exist=False)
    if os.path.lexists(destination):
        raise FileExistsError("catalog backup destination already exists: '{}'".format(destination))
    with Catalog.open_read_only(database) as catalog:
        returned = catalog.backup_to(destination)
    payload = {
        "schema": CATALOG_BACKUP_SCHEMA,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "state": "complete",
        "partial": False,
        "database": str(database),
        "destination": os.path.abspath(returned),
        "issues": [],
        "integrity_checked": True,
        "overwrote_existing": False,
        "safety": _safety(),
    }
    _write_json_line(stdout, payload)
    return int(CatalogExitCode.OK)


def _stream_changes(
    catalog: Catalog,
    database: Path,
    before_scan_id: int,
    after_scan_id: int,
    root_ids: Sequence[int],
    page_size: int,
    first_page: Sequence[Any],
    stdout: TextIO,
) -> int:
    created_at = _utc_now()
    base = {
        "schema": CATALOG_CHANGE_RECORD_SCHEMA,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "created_at": created_at,
        "database": str(database),
        "before_scan_id": before_scan_id,
        "after_scan_id": after_scan_id,
        "root_ids": list(root_ids),
        "safety": _change_safety(),
    }
    _write_json_line(
        stdout,
        {
            **base,
            "record_type": "header",
            "state": "streaming",
            "partial": False,
            "issues": [],
            "change": None,
            "summary": None,
        },
    )
    counts = {
        "added": 0,
        "modified": 0,
        "moved": 0,
        "missing": 0,
    }
    cursor = (0, "", "")
    page = first_page
    try:
        while page:
            for row in page:
                if sum(counts.values()) >= CATALOG_MAX_CHANGES:
                    raise _MachineOutputResourceLimit(
                        "catalog change output exceeds the {} change limit".format(
                            CATALOG_MAX_CHANGES,
                        )
                    )
                change = _change_value(
                    row,
                    before_scan_id,
                    after_scan_id,
                )
                _write_json_line(
                    stdout,
                    {
                        **base,
                        "record_type": "change",
                        "state": "complete",
                        "partial": False,
                        "issues": [],
                        "change": change,
                        "summary": None,
                    },
                )
                counts[change["change_type"]] += 1

            last = page[-1]
            next_cursor = (
                int(last["sort_root_id"]),
                str(last["sort_path_key"]),
                str(last["change_type"]),
            )
            if next_cursor <= cursor:
                raise CatalogStateError("catalog change cursor did not advance")
            cursor = next_cursor
            if len(page) < page_size:
                break
            page = catalog.page_scan_changes(
                before_scan_id,
                after_scan_id,
                root_ids,
                after_root_id=cursor[0],
                after_path_key=cursor[1],
                after_change_type=cursor[2],
                limit=page_size,
            )
    except (
        CatalogError,
        CatalogIndexError,
        CatalogServiceError,
        CatalogWorkerError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        _write_json_line(
            stdout,
            {
                **base,
                "record_type": "summary",
                "state": "partial",
                "partial": True,
                "issues": [
                    {
                        "code": _error_code(error),
                        "message": _error_message(error),
                    }
                ],
                "change": None,
                "summary": _change_summary(counts, page_size),
            },
        )
        return int(CatalogExitCode.PARTIAL)

    _write_json_line(
        stdout,
        {
            **base,
            "record_type": "summary",
            "state": "complete",
            "partial": False,
            "issues": [],
            "change": None,
            "summary": _change_summary(counts, page_size),
        },
    )
    return int(CatalogExitCode.OK)


def _change_value(
    row: Mapping[str, Any],
    before_scan_id: int,
    after_scan_id: int,
) -> Dict[str, Any]:
    change_type = str(row["change_type"])
    if change_type not in {"added", "modified", "moved", "missing"}:
        raise ValueError("catalog emitted an unknown change type: '{}'".format(change_type))
    old = _change_snapshot(row, "old")
    new = _change_snapshot(row, "new")
    expected_sides = {
        "added": (False, True),
        "modified": (True, True),
        "moved": (True, True),
        "missing": (True, False),
    }
    if (old is not None, new is not None) != expected_sides[change_type]:
        raise ValueError("catalog {} change has inconsistent snapshot sides".format(change_type))
    identity_proven = bool(row["identity_proven"])
    if identity_proven != (change_type == "moved"):
        raise ValueError("catalog move identity proof is inconsistent with change type")
    if change_type == "moved" and (old["identity_confidence"] != "stable" or new["identity_confidence"] != "stable"):
        raise ValueError("catalog moved change lacks stable identity evidence")

    classifications = {
        "added": "immutable_snapshot_added",
        "modified": "immutable_snapshot_modified",
        "moved": "stable_native_identity_1_to_1_move",
        "missing": "immutable_snapshot_missing",
    }
    canonical = json.dumps(
        {
            "before_scan_id": before_scan_id,
            "after_scan_id": after_scan_id,
            "change_type": change_type,
            "old_observation_id": row["old_observation_id"],
            "new_observation_id": row["new_observation_id"],
            "old_path_key": row["old_path_key"],
            "new_path_key": row["new_path_key"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "change_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "change_type": change_type,
        "old": old,
        "new": new,
        "content_changed": bool(row["content_changed"]),
        "move_identity_proven": identity_proven,
        "classification": classifications[change_type],
        "allows_automatic_destructive_action": False,
    }


def _change_snapshot(
    row: Mapping[str, Any],
    prefix: str,
) -> Any:
    observation_id = row["{}_observation_id".format(prefix)]
    if observation_id is None:
        return None
    return {
        "observation_id": int(observation_id),
        "root_id": int(row["{}_root_id".format(prefix)]),
        "path_id": int(row["{}_path_id".format(prefix)]),
        "physical_file_id": int(row["{}_physical_file_id".format(prefix)]),
        "content_version_id": int(row["{}_content_version_id".format(prefix)]),
        "path": str(row["{}_display_path".format(prefix)]),
        "path_state": str(row["{}_path_state".format(prefix)]),
        "content_state": str(row["{}_content_state".format(prefix)]),
        "identity_confidence": str(row["{}_identity_confidence".format(prefix)]),
    }


def _change_summary(
    counts: Mapping[str, int],
    page_size: int,
) -> Dict[str, int]:
    return {
        "total": sum(counts.values()),
        "added": counts["added"],
        "modified": counts["modified"],
        "moved": counts["moved"],
        "missing": counts["missing"],
        "page_size": page_size,
    }


def _write_service_result(
    command: str,
    database: Path,
    result: CatalogServiceResult,
    limits: Mapping[str, int],
    stdout: TextIO,
) -> int:
    state, partial, exit_code = _result_state(result, limits)
    payload = {
        "schema": CATALOG_RESULT_SCHEMA,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "command": command,
        "state": state,
        "partial": partial,
        "database": str(database),
        "issues": [{"code": "catalog_service_issue", "message": message} for message in result.errors],
        "limits": dict(limits),
        "result": result.to_dict(),
        "safety": _safety(),
    }
    _write_json_line(stdout, payload)
    return int(exit_code)


def _stream_groups(
    service: CatalogService,
    database: Path,
    scan_id: int,
    page_size: int,
    stdout: TextIO,
) -> int:
    created_at = _utc_now()
    base = {
        "schema": CATALOG_GROUP_RECORD_SCHEMA,
        "schema_version": CATALOG_GROUP_RECORD_SCHEMA_VERSION,
        "created_at": created_at,
        "scan_id": scan_id,
        "database": str(database),
        "safety": _safety(),
    }
    _write_json_line(
        stdout,
        {
            **base,
            "record_type": "header",
            "state": "streaming",
            "partial": False,
            "issues": [],
            "group_header": None,
            "member_chunk": None,
            "group_end": None,
            "summary": None,
        },
    )
    group_count = 0
    file_count = 0
    member_chunk_count = 0
    try:
        for group in service.iter_verified_exact_groups(
            page_size=page_size,
            max_page_files=CATALOG_GROUP_PAGE_MAX_FILES,
            max_group_members=CATALOG_MAX_GROUP_MEMBERS,
        ):
            if group_count >= CATALOG_MAX_GROUPS:
                raise _MachineOutputResourceLimit(
                    "catalog group output exceeds the {} group limit".format(
                        CATALOG_MAX_GROUPS,
                    )
                )
            checkpoint = stdout.checkpoint() if isinstance(stdout, _MachineOutputSpool) else None
            try:
                record = _group_value(group, scan_id)
                chunks = _write_group_records(stdout, base, record)
            except _MachineOutputError:
                raise
            except (
                CatalogError,
                CatalogIndexError,
                CatalogServiceError,
                CatalogWorkerError,
                OSError,
                TypeError,
                ValueError,
            ):
                if checkpoint is not None:
                    stdout.rollback(checkpoint)
                raise
            group_count += 1
            file_count += int(record["group_header"]["total_members"])
            member_chunk_count += chunks
    except (
        CatalogError,
        CatalogIndexError,
        CatalogServiceError,
        CatalogWorkerError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        _write_json_line(
            stdout,
            {
                **base,
                "record_type": "summary",
                "state": "partial",
                "partial": True,
                "issues": [
                    {
                        "code": _error_code(error),
                        "message": _error_message(error),
                    }
                ],
                "group_header": None,
                "member_chunk": None,
                "group_end": None,
                "summary": {
                    "groups": group_count,
                    "files": file_count,
                    "member_chunks": member_chunk_count,
                    "page_size": page_size,
                },
            },
        )
        return int(CatalogExitCode.PARTIAL)

    _write_json_line(
        stdout,
        {
            **base,
            "record_type": "summary",
            "state": "complete",
            "partial": False,
            "issues": [],
            "group_header": None,
            "member_chunk": None,
            "group_end": None,
            "summary": {
                "groups": group_count,
                "files": file_count,
                "member_chunks": member_chunk_count,
                "page_size": page_size,
            },
        },
    )
    return int(CatalogExitCode.OK)


def _group_value(group: VerifiedExactGroup, scan_id: int) -> Dict[str, Any]:
    digest = bytes(group.full_digest).hex()
    if len(digest) != 64:
        raise ValueError("catalog exact group does not contain a SHA-256 digest")
    size = int(group.size)
    if size < 0:
        raise ValueError("catalog exact group has a negative file size")
    files = tuple(group.files)
    if len(files) < 2:
        raise ValueError("catalog exact group requires at least two files")
    if len(files) > CATALOG_MAX_GROUP_MEMBERS:
        raise _MachineOutputResourceLimit(
            "one catalog group exceeds the {} member limit".format(
                CATALOG_MAX_GROUP_MEMBERS,
            )
        )
    verification_ids = tuple(int(value) for value in group.verification_ids)
    if (
        len(verification_ids) != len(files) - 1
        or any(value < 1 for value in verification_ids)
        or len(set(verification_ids)) != len(verification_ids)
    ):
        raise ValueError("catalog exact group has incomplete verification IDs")

    seen_paths = set()
    member_digests = []
    for index, file in enumerate(files):
        path = str(file.path)
        if not path or "\0" in path or path in seen_paths:
            raise ValueError("catalog exact group requires distinct paths")
        seen_paths.add(path)
        identifiers = (
            int(file.path_id),
            int(file.physical_file_id),
            int(file.content_version_id),
        )
        if any(value < 1 for value in identifiers):
            raise ValueError("catalog exact group has invalid member identifiers")
        verification_id = None if index == 0 else verification_ids[index - 1]
        member_digests.append(
            _catalog_group_member_digest(
                path,
                identifiers[0],
                identifiers[1],
                identifiers[2],
                verification_id,
            )
        )
    if len(seen_paths) != len(files):
        raise ValueError("catalog exact group requires distinct paths")
    group_id = _catalog_group_id(
        scan_id,
        size,
        digest,
        member_digests,
    )
    return {
        "group_header": {
            "group_id": group_id,
            "size": size,
            "sha256": digest,
            "total_members": len(files),
            "total_verifications": len(verification_ids),
            "verification": "verified_exact",
            "safety_state": "verified_exact_requires_fresh_action_proof",
            "allows_automatic_destructive_action": False,
        },
        "files": files,
        "verification_ids": verification_ids,
    }


def _catalog_group_id(
    scan_id: int,
    size: int,
    digest: str,
    member_digests: Sequence[bytes],
) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"dupeguru.catalog-group-id.v2\0")
    for value in (str(int(scan_id)), str(int(size)), digest):
        _update_length_prefixed_hash(hasher, value)
    for member_digest in sorted(member_digests):
        hasher.update(member_digest)
    return hasher.hexdigest()


def _catalog_group_member_digest(
    path: str,
    path_id: int,
    physical_file_id: int,
    content_version_id: int,
    verification_id: Any,
) -> bytes:
    hasher = hashlib.sha256()
    hasher.update(b"dupeguru.catalog-group-member.v2\0")
    values = (
        path,
        str(path_id),
        str(physical_file_id),
        str(content_version_id),
        "-" if verification_id is None else str(verification_id),
    )
    for value in values:
        _update_length_prefixed_hash(hasher, value)
    return hasher.digest()


def _update_length_prefixed_hash(hasher, value: str) -> None:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _MachineOutputEncodingError("catalog group identity is not strict UTF-8") from error
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _write_group_records(
    output,
    base: Mapping[str, Any],
    record: Mapping[str, Any],
) -> int:
    header = record["group_header"]
    files = record["files"]
    verification_ids = record["verification_ids"]
    group_id = header["group_id"]
    _write_json_line(
        output,
        {
            **base,
            "record_type": "group_header",
            "state": "complete",
            "partial": False,
            "issues": [],
            "group_header": header,
            "member_chunk": None,
            "group_end": None,
            "summary": None,
        },
    )

    chunk_index = 0
    first_member_index = 0
    current_members = []
    current_member_bytes = 0
    empty_line_bytes = len(
        _encode_machine_json_line(
            _group_member_chunk_payload(
                base,
                group_id,
                chunk_index,
                first_member_index,
                (),
            )
        )
    )
    for member_index, file in enumerate(files):
        member = {
            "path": str(file.path),
            "path_id": int(file.path_id),
            "physical_file_id": int(file.physical_file_id),
            "content_version_id": int(file.content_version_id),
            "verification_id": None if member_index == 0 else verification_ids[member_index - 1],
        }
        encoded_member = _encode_machine_json_value(member)
        separator_bytes = 1 if current_members else 0
        candidate_line_bytes = empty_line_bytes + current_member_bytes + separator_bytes + len(encoded_member)
        if current_members and (
            candidate_line_bytes > CATALOG_MACHINE_MAX_LINE_BYTES
            or len(current_members) >= CATALOG_GROUP_CHUNK_MAX_MEMBERS
        ):
            _write_json_line(
                output,
                _group_member_chunk_payload(
                    base,
                    group_id,
                    chunk_index,
                    first_member_index,
                    current_members,
                ),
            )
            first_member_index += len(current_members)
            chunk_index += 1
            current_members = []
            current_member_bytes = 0
            empty_line_bytes = len(
                _encode_machine_json_line(
                    _group_member_chunk_payload(
                        base,
                        group_id,
                        chunk_index,
                        first_member_index,
                        (),
                    )
                )
            )
        if empty_line_bytes + len(encoded_member) > CATALOG_MACHINE_MAX_LINE_BYTES:
            raise _MachineOutputResourceLimit(
                "one catalog group member cannot fit in the {} byte line limit".format(
                    CATALOG_MACHINE_MAX_LINE_BYTES,
                )
            )
        if current_members:
            current_member_bytes += 1
        current_members.append(member)
        current_member_bytes += len(encoded_member)

    if not current_members:
        raise ValueError("catalog exact group produced no member chunks")
    _write_json_line(
        output,
        _group_member_chunk_payload(
            base,
            group_id,
            chunk_index,
            first_member_index,
            current_members,
        ),
    )
    chunk_count = chunk_index + 1
    _write_json_line(
        output,
        {
            **base,
            "record_type": "group_end",
            "state": "complete",
            "partial": False,
            "issues": [],
            "group_header": None,
            "member_chunk": None,
            "group_end": {
                "group_id": group_id,
                "chunk_count": chunk_count,
                "total_members": header["total_members"],
                "total_verifications": header["total_verifications"],
                "verification_complete": True,
            },
            "summary": None,
        },
    )
    return chunk_count


def _group_member_chunk_payload(
    base: Mapping[str, Any],
    group_id: str,
    chunk_index: int,
    first_member_index: int,
    members: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    return {
        **base,
        "record_type": "member_chunk",
        "state": "complete",
        "partial": False,
        "issues": [],
        "group_header": None,
        "member_chunk": {
            "group_id": group_id,
            "chunk_index": chunk_index,
            "first_member_index": first_member_index,
            "members": list(members),
        },
        "group_end": None,
        "summary": None,
    }


def _scan_roots(database: Path, scan_id: int) -> Tuple[Path, ...]:
    with Catalog.open_read_only(database) as catalog:
        rows = catalog.scan_roots(scan_id)
    roots = tuple(Path(row["display_path"]) for row in rows)
    if not roots:
        raise ValueError("catalog scan {} has no selected roots".format(scan_id))
    return roots


def _comparison_root_ids(
    catalog: Catalog,
    before_scan_id: int,
    after_scan_id: int,
) -> Tuple[int, ...]:
    before_root_ids = {int(row["root_id"]) for row in catalog.scan_roots(before_scan_id)}
    after_root_ids = {int(row["root_id"]) for row in catalog.scan_roots(after_scan_id)}
    if not before_root_ids or not after_root_ids:
        raise ValueError("catalog scans must each select at least one root")
    if before_root_ids != after_root_ids:
        raise ValueError("catalog changes require --from and --to scans with identical root sets")
    return tuple(sorted(before_root_ids))


def _latest_complete_scan_roots(
    database: Path,
) -> Tuple[int, Tuple[Path, ...], Tuple[int, ...]]:
    with Catalog.open_read_only(database) as catalog:
        scan_id = catalog.latest_complete_scan_id()
        if scan_id is None:
            raise ValueError("catalog has no complete scan; verified groups are unavailable")
        rows = catalog.scan_roots(scan_id)
    roots = tuple(Path(row["display_path"]) for row in rows)
    root_ids = tuple(int(row["root_id"]) for row in rows)
    if not roots:
        raise ValueError("latest complete catalog scan {} has no selected roots".format(scan_id))
    return int(scan_id), roots, root_ids


def _result_state(
    result: CatalogServiceResult,
    limits: Mapping[str, int],
) -> Tuple[str, bool, CatalogExitCode]:
    if result.catalog_status == "complete" and result.outcome == "finished":
        return "complete", False, CatalogExitCode.OK
    if result.catalog_status == "cancelled":
        return "cancelled", True, CatalogExitCode.CANCELLED
    if result.catalog_status == "failed":
        return "failed", False, CatalogExitCode.FAILED
    pending_work = result.status.work_counts.get("pending", 0) + result.status.work_counts.get("in_progress", 0)
    if result.catalog_status == "running" and pending_work and result.worker_batches >= limits["max_worker_batches"]:
        return "resource_limited", True, CatalogExitCode.RESOURCE_LIMIT
    return "partial", True, CatalogExitCode.PARTIAL


def _status_state(
    status: CatalogServiceStatus,
) -> Tuple[str, bool, CatalogExitCode]:
    if status.status == "complete":
        return "complete", False, CatalogExitCode.OK
    if status.status == "running":
        return "running", True, CatalogExitCode.PARTIAL
    if status.status == "cancelled":
        return "cancelled", True, CatalogExitCode.CANCELLED
    if status.status == "failed":
        return "failed", False, CatalogExitCode.FAILED
    return "partial", True, CatalogExitCode.PARTIAL


def _limits(max_work_items: int, batch_size: int) -> Dict[str, int]:
    if max_work_items < 1:
        raise ValueError("max_work_items must be at least one")
    if batch_size < 1 or batch_size > MAXIMUM_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and {}".format(MAXIMUM_BATCH_SIZE))
    effective_batch_size = min(batch_size, max_work_items)
    max_worker_batches = max(1, max_work_items // effective_batch_size)
    return {
        "max_work_items": max_work_items,
        "batch_size": effective_batch_size,
        "max_worker_batches": max_worker_batches,
        "effective_capacity": effective_batch_size * max_worker_batches,
    }


def _database_path(value: str, *, must_exist: bool) -> Path:
    return preflight_catalog_path(value, must_exist=must_exist)


def _root_path(value: str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    file_stat = os.lstat(path)
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat) or not stat.S_ISDIR(file_stat.st_mode):
        raise ValueError("catalog root must be a plain directory: '{}'".format(path))
    return path


def _write_error(args, error: Exception, stdout: TextIO, stderr: TextIO) -> None:
    database = getattr(args, "database", "")
    try:
        database = os.path.abspath(os.fspath(database)) if database else ""
    except (TypeError, ValueError):
        database = ""
    payload = {
        "schema": CATALOG_ERROR_SCHEMA,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "command": getattr(args, "catalog_command", ""),
        "state": "failed",
        "partial": False,
        "database": database,
        "issues": [{"code": _error_code(error), "message": _error_message(error)}],
    }
    _write_json_line(stdout, payload)
    _write_diagnostic(
        stderr,
        "{}: {}\n".format(type(error).__name__, error),
    )


def _write_diagnostic(stderr: TextIO, message: str) -> None:
    safe_message = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
    try:
        stderr.write(safe_message)
        stderr.flush()
    except (OSError, TypeError, UnicodeError, ValueError):
        pass


def _exception_exit_code(error: Exception) -> CatalogExitCode:
    if isinstance(error, (CatalogIntegrityError, CatalogSchemaError)):
        return CatalogExitCode.FAILED
    return CatalogExitCode.INPUT_ERROR


def _error_code(error: Exception) -> str:
    name = type(error).__name__
    characters = []
    for index, character in enumerate(name):
        if character.isupper() and index:
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _error_message(error: Exception) -> str:
    message = str(error)
    return message if message else type(error).__name__


def _safety() -> Dict[str, Any]:
    return {
        "database_location": "local",
        "verification": "sha256+byte-compare",
        "complete_scan_required": True,
        "allows_automatic_destructive_action": False,
        "fresh_action_proof_required": True,
        "destructive_workflow": "quarantine_then_explicit_finalize",
    }


def _change_safety() -> Dict[str, Any]:
    return {
        "database_location": "local",
        "verification": "immutable-complete-snapshot-diff",
        "complete_scan_required": True,
        "move_classification": "stable-native-identity-one-to-one-only",
        "allows_automatic_destructive_action": False,
        "fresh_action_proof_required": True,
        "destructive_workflow": "quarantine_then_explicit_finalize",
    }


def _write_json_line(stream: TextIO, payload: Mapping[str, Any]) -> None:
    if isinstance(stream, _MachineOutputSpool):
        stream.write_record(payload)
        return
    encoded = _encode_machine_json_line(payload)
    _write_complete_text(stream, encoded.decode("utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("value must be at least one")
    return result


def _batch_size(value: str) -> int:
    result = _positive_int(value)
    if result > MAXIMUM_BATCH_SIZE:
        raise argparse.ArgumentTypeError("value must not exceed {}".format(MAXIMUM_BATCH_SIZE))
    return result


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


__all__ = [
    "CATALOG_BACKUP_SCHEMA",
    "CATALOG_CHANGE_RECORD_SCHEMA",
    "CATALOG_ERROR_SCHEMA",
    "CATALOG_GROUP_CHUNK_MAX_MEMBERS",
    "CATALOG_GROUP_PAGE_MAX_FILES",
    "CATALOG_GROUP_RECORD_SCHEMA",
    "CATALOG_GROUP_RECORD_SCHEMA_VERSION",
    "CATALOG_MACHINE_MAX_LINE_BYTES",
    "CATALOG_MACHINE_MAX_RECORDS",
    "CATALOG_MACHINE_MAX_TOTAL_BYTES",
    "CATALOG_MAX_CHANGES",
    "CATALOG_MAX_GROUP_MEMBERS",
    "CATALOG_MAX_GROUPS",
    "CATALOG_RESULT_SCHEMA",
    "CATALOG_SCHEMA_VERSION",
    "CATALOG_SCHEMAS",
    "CATALOG_STATUS_SCHEMA",
    "CatalogExitCode",
    "add_catalog_parser",
    "run_catalog_command",
]
