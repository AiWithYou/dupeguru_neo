# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Strict JSON I/O and a thin application facade for dataset workflows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, TextIO, Tuple

from core.dataset_executor import (
    DatasetBundleExecutor,
    DatasetExecutionReport,
    DatasetOperationSummary,
)
from core.dataset_service import (
    DATASET_PLAN_SCHEMA,
    DATASET_PLAN_SCHEMA_VERSION,
    EXECUTOR_CONTRACT,
    MAX_DATASET_PLAN_ACTIONS,
    MAX_DATASET_PLAN_DOCUMENT_BYTES,
    MAX_DATASET_PLAN_FILE_RECORDS,
    DatasetAsset,
    DatasetBundleAction,
    DatasetCluster,
    DatasetFileAction,
    DatasetFileProof,
    DatasetIssue,
    DatasetModeService,
    DatasetOperation,
    DatasetPlan,
    DatasetPreparation,
    DatasetRelation,
    KeeperReasonRecord,
    KeeperRecord,
    PlanValidation,
)
from core.safe_json import (
    DATASET_DOCUMENT_JSON_LIMITS,
    JsonStructureError,
    preflight_json_structure,
)
from core.pe.dataset import SplitAssignment, SplitManifest, SplitReason
from core.safe_action import (
    FileSystemAdapter,
    cleanup_created_regular_file,
    platform_file_system,
)
from core.safe_walk import is_reparse_point

PREPARE_INPUT_SCHEMA = "dupeguru.dataset-prepare-input"
PREPARE_INPUT_SCHEMA_VERSION = 1
PREPARATION_RESULT_SCHEMA = "dupeguru.dataset-preparation-result"
PLAN_VALIDATION_SCHEMA = "dupeguru.dataset-plan-validation"
EXECUTION_REPORT_SCHEMA = "dupeguru.dataset-execution-report"
OPERATION_SUMMARY_SCHEMA = "dupeguru.dataset-operation-summary"
OPERATION_LIST_SCHEMA = "dupeguru.dataset-operation-list"
RESULT_SCHEMA_VERSION = 1
DEFAULT_JSON_SIZE_LIMIT = MAX_DATASET_PLAN_DOCUMENT_BYTES
JSON_READ_CHUNK_SIZE = 64 * 1024


class DatasetIOError(ValueError):
    def __init__(self, code: str, message: str, path: Optional[Path] = None):
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True)
class JSONFileReceipt:
    path: str
    size: int
    sha256: str
    written: bool
    dry_run: bool


@dataclass(frozen=True)
class DatasetPrepareInput:
    allowed_roots: Tuple[str, ...]
    destination_root: str
    assets: Tuple[DatasetAsset, ...]
    clusters: Tuple[DatasetCluster, ...]
    sidecar_paths: Tuple[str, ...]
    split_weights: Tuple[Tuple[str, float], ...]
    split_seed: str
    dry_run: bool
    schema: str = PREPARE_INPUT_SCHEMA
    schema_version: int = PREPARE_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != PREPARE_INPUT_SCHEMA or self.schema_version != PREPARE_INPUT_SCHEMA_VERSION:
            raise ValueError("unsupported dataset prepare-input schema")
        if not self.allowed_roots or not self.destination_root or not self.assets:
            raise ValueError("dataset prepare input requires roots, a destination, and assets")
        if len(set(self.allowed_roots)) != len(self.allowed_roots):
            raise ValueError("dataset prepare input roots must be unique")
        if len({asset.asset_id for asset in self.assets}) != len(self.assets):
            raise ValueError("dataset prepare input asset IDs must be unique")
        if len(set(self.sidecar_paths)) != len(self.sidecar_paths):
            raise ValueError("dataset prepare input sidecar paths must be unique")
        if len(self.assets) > MAX_DATASET_PLAN_ACTIONS:
            raise ValueError(
                "dataset prepare input exceeds the {}-asset limit".format(
                    MAX_DATASET_PLAN_ACTIONS,
                )
            )
        if len(self.assets) + len(self.sidecar_paths) > MAX_DATASET_PLAN_FILE_RECORDS:
            raise ValueError(
                "dataset prepare input exceeds the {} potential file-record limit".format(
                    MAX_DATASET_PLAN_FILE_RECORDS,
                )
            )
        if not self.split_weights or len({name for name, _weight in self.split_weights}) != len(self.split_weights):
            raise ValueError("dataset prepare input split names must be unique")
        for name, weight in self.split_weights:
            if not name or not math.isfinite(weight) or weight <= 0:
                raise ValueError("dataset split weights must be finite and positive")
        if not isinstance(self.split_seed, str) or "\0" in self.split_seed:
            raise ValueError("dataset split seed must be a string without NUL")
        if not isinstance(self.dry_run, bool):
            raise ValueError("dataset dry_run must be boolean")

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "allowed_roots": list(self.allowed_roots),
            "destination_root": self.destination_root,
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "path": asset.path,
                    "dimensions": list(asset.dimensions) if asset.dimensions is not None else None,
                    "bit_depth": asset.bit_depth,
                    "metadata_count": asset.metadata_count,
                    "jpeg_artifact_score": asset.jpeg_artifact_score,
                    "protected": asset.protected,
                }
                for asset in self.assets
            ],
            "clusters": [
                {
                    "members": list(cluster.members),
                    "relation": cluster.relation.value,
                    "evidence_complete": cluster.evidence_complete,
                    "evidence_version": cluster.evidence_version,
                }
                for cluster in self.clusters
            ],
            "sidecar_paths": list(self.sidecar_paths),
            "split_weights": {name: weight for name, weight in self.split_weights},
            "split_seed": self.split_seed,
            "dry_run": self.dry_run,
        }


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _iter_canonical_json_bytes(value: Any) -> Iterator[bytes]:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        for chunk in encoder.iterencode(value):
            yield chunk.encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise DatasetIOError("invalid_json_value", str(error)) from error
    yield b"\n"


def _size_bytes(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise DatasetIOError("invalid_utf8", str(error)) from error


def _check_size(size: int, maximum_bytes: int) -> None:
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int) or maximum_bytes <= 0:
        raise ValueError("maximum JSON size must be a positive integer")
    if size > maximum_bytes:
        raise DatasetIOError(
            "json_too_large",
            "JSON payload is {} bytes; maximum is {}".format(size, maximum_bytes),
        )


def _reject_constant(value: str) -> None:
    raise DatasetIOError("non_finite_number", "JSON number {} is not finite".format(value))


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise DatasetIOError("non_finite_number", "JSON number {} is not finite".format(value))
    return parsed


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetIOError("duplicate_key", "duplicate JSON object key: {}".format(key))
        result[key] = value
    return result


def strict_json_loads(
    payload: str | bytes,
    *,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
) -> Any:
    if isinstance(payload, bytes):
        _check_size(len(payload), maximum_bytes)
        if payload.startswith(b"\xef\xbb\xbf"):
            raise DatasetIOError("bom_forbidden", "UTF-8 BOM is not accepted")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise DatasetIOError("invalid_utf8", str(error)) from error
    elif isinstance(payload, str):
        _check_size(_size_bytes(payload), maximum_bytes)
        if payload.startswith("\ufeff"):
            raise DatasetIOError("bom_forbidden", "Unicode BOM is not accepted")
        text = payload
    else:
        raise TypeError("JSON payload must be str or bytes")
    try:
        preflight_json_structure(
            text,
            limits=DATASET_DOCUMENT_JSON_LIMITS,
            label="dataset JSON document",
        )
    except JsonStructureError as error:
        raise DatasetIOError("json_resource_limit", str(error)) from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except DatasetIOError:
        raise
    except MemoryError as error:
        raise DatasetIOError(
            "json_resource_limit",
            "dataset JSON exceeded the parser memory budget",
        ) from error
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as error:
        raise DatasetIOError("invalid_json", str(error)) from error


def _read_bounded_json_payload(
    stream: TextIO | BinaryIO,
    *,
    maximum_bytes: int,
) -> str | bytes:
    """Read one JSON document without seeking or issuing an unbounded read."""

    _check_size(0, maximum_bytes)
    binary_payload = bytearray()
    text_parts: List[str] = []
    payload_kind: Optional[str] = None
    total_bytes = 0
    while True:
        # One extra character/byte is sufficient to prove that the limit was
        # crossed.  A fixed chunk cap also bounds overshoot for multi-byte text.
        read_size = min(JSON_READ_CHUNK_SIZE, maximum_bytes - total_bytes + 1)
        try:
            chunk = stream.read(read_size)
        except UnicodeError as error:
            raise DatasetIOError("invalid_utf8", str(error)) from error
        if chunk in ("", b""):
            break
        if isinstance(chunk, bytes):
            if payload_kind == "text":
                raise DatasetIOError(
                    "invalid_stream",
                    "JSON input stream changed from text to bytes",
                )
            payload_kind = "bytes"
            chunk_bytes = len(chunk)
            binary_payload.extend(chunk)
        elif isinstance(chunk, str):
            if payload_kind == "bytes":
                raise DatasetIOError(
                    "invalid_stream",
                    "JSON input stream changed from bytes to text",
                )
            payload_kind = "text"
            chunk_bytes = _size_bytes(chunk)
            text_parts.append(chunk)
        else:
            raise DatasetIOError(
                "invalid_stream",
                "JSON input stream must return text or bytes",
            )
        total_bytes += chunk_bytes
        _check_size(total_bytes, maximum_bytes)
    if payload_kind == "text":
        return "".join(text_parts)
    return bytes(binary_payload)


def read_json_stream(
    stream: TextIO | BinaryIO,
    *,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
) -> Any:
    """Parse one strict, size-bounded JSON document from a seek-free stream."""

    payload = _read_bounded_json_payload(
        stream,
        maximum_bytes=maximum_bytes,
    )
    return strict_json_loads(payload, maximum_bytes=maximum_bytes)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DatasetIOError("invalid_type", "{} must be a JSON object".format(label))
    return value


def _exact(value: Mapping[str, Any], expected: Iterable[str], label: str) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        unknown = sorted(actual - expected_set)
        missing = sorted(expected_set - actual)
        raise DatasetIOError(
            "invalid_fields",
            "{} has unknown fields {} and missing fields {}".format(label, unknown, missing),
        )


def _list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise DatasetIOError("invalid_type", "{} must be a JSON array".format(label))
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not allow_empty and not value):
        raise DatasetIOError("invalid_type", "{} must be a safe string".format(label))
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DatasetIOError("invalid_utf8", "{} is not valid UTF-8 text".format(label)) from error
    return value


def _optional_string(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _string(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise DatasetIOError("invalid_type", "{} must be boolean".format(label))
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DatasetIOError(
            "invalid_type",
            "{} must be an integer >= {}".format(label, minimum),
        )
    return value


def _number(value: Any, label: str, *, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetIOError("invalid_type", "{} must be a number".format(label))
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise DatasetIOError("invalid_number", "{} has an invalid numeric value".format(label)) from error
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise DatasetIOError("invalid_number", "{} has an invalid numeric value".format(label))
    return result


def _unique_strings(
    value: Any,
    label: str,
    *,
    allow_empty_items: bool = False,
) -> Tuple[str, ...]:
    items = tuple(_string(item, "{} item".format(label), allow_empty=allow_empty_items) for item in _list(value, label))
    if len(set(items)) != len(items):
        raise DatasetIOError("duplicate_value", "{} contains duplicate values".format(label))
    return items


def _unique_paths(value: Any, label: str) -> Tuple[str, ...]:
    items = _unique_strings(value, label)
    normalized = tuple(os.path.normcase(os.path.abspath(item)) for item in items)
    if len(set(normalized)) != len(normalized):
        raise DatasetIOError(
            "duplicate_value",
            "{} contains duplicate physical path spellings".format(label),
        )
    return items


def _safe_component(value: Any, label: str) -> str:
    component = _string(value, label)
    if Path(component).name != component or component in {".", ".."}:
        raise DatasetIOError(
            "invalid_value",
            "{} must be a safe single path component".format(label),
        )
    return component


def dataset_prepare_input_from_dict(value: Mapping[str, Any]) -> DatasetPrepareInput:
    document = _mapping(value, "dataset prepare input")
    _exact(
        document,
        {
            "schema",
            "schema_version",
            "allowed_roots",
            "destination_root",
            "assets",
            "clusters",
            "sidecar_paths",
            "split_weights",
            "split_seed",
            "dry_run",
        },
        "dataset prepare input",
    )
    if document["schema"] != PREPARE_INPUT_SCHEMA:
        raise DatasetIOError("unsupported_schema", "unsupported dataset prepare-input schema")
    if _integer(document["schema_version"], "prepare schema_version") != PREPARE_INPUT_SCHEMA_VERSION:
        raise DatasetIOError("unsupported_schema", "unsupported dataset prepare-input version")
    roots = _unique_paths(document["allowed_roots"], "allowed_roots")
    if not roots:
        raise DatasetIOError("invalid_value", "allowed_roots must not be empty")
    destination = _string(document["destination_root"], "destination_root")

    raw_assets = _list(document["assets"], "assets")
    if len(raw_assets) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetIOError(
            "resource_limit",
            "assets exceeds the {}-record limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    assets = []
    asset_ids = set()
    asset_paths = set()
    for index, raw_asset in enumerate(raw_assets):
        asset = _mapping(raw_asset, "asset {}".format(index))
        _exact(
            asset,
            {
                "asset_id",
                "path",
                "dimensions",
                "bit_depth",
                "metadata_count",
                "jpeg_artifact_score",
                "protected",
            },
            "asset {}".format(index),
        )
        asset_id = _string(asset["asset_id"], "asset_id")
        path = _string(asset["path"], "asset path")
        if asset_id in asset_ids or os.path.normcase(os.path.abspath(path)) in asset_paths:
            raise DatasetIOError("duplicate_value", "asset IDs and paths must be unique")
        asset_ids.add(asset_id)
        asset_paths.add(os.path.normcase(os.path.abspath(path)))
        dimensions_value = asset["dimensions"]
        if dimensions_value is None:
            dimensions = None
        else:
            dimensions_items = _list(dimensions_value, "asset dimensions")
            if len(dimensions_items) != 2:
                raise DatasetIOError("invalid_value", "asset dimensions require two values")
            dimensions = (
                _integer(dimensions_items[0], "asset width", minimum=1),
                _integer(dimensions_items[1], "asset height", minimum=1),
            )
        try:
            assets.append(
                DatasetAsset(
                    asset_id=asset_id,
                    path=path,
                    dimensions=dimensions,
                    bit_depth=_number(asset["bit_depth"], "asset bit_depth", minimum=0),
                    metadata_count=_number(
                        asset["metadata_count"],
                        "asset metadata_count",
                        minimum=0,
                    ),
                    jpeg_artifact_score=_number(
                        asset["jpeg_artifact_score"],
                        "asset jpeg_artifact_score",
                        minimum=0,
                    ),
                    protected=_boolean(asset["protected"], "asset protected"),
                )
            )
        except ValueError as error:
            raise DatasetIOError("invalid_asset", str(error)) from error
    if not assets:
        raise DatasetIOError("invalid_value", "assets must not be empty")

    raw_clusters = _list(document["clusters"], "clusters")
    if len(raw_clusters) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetIOError(
            "resource_limit",
            "clusters exceeds the {}-record limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    clusters = []
    seen_cluster_members: set[str] = set()
    seen_cluster_ids = set()
    for index, raw_cluster in enumerate(raw_clusters):
        cluster = _mapping(raw_cluster, "cluster {}".format(index))
        _exact(
            cluster,
            {
                "members",
                "relation",
                "evidence_complete",
                "evidence_version",
            },
            "cluster {}".format(index),
        )
        members = _unique_strings(cluster["members"], "cluster members")
        if len(members) < 2:
            raise DatasetIOError("invalid_cluster", "cluster requires at least two members")
        unknown_members = set(members) - asset_ids
        if unknown_members:
            raise DatasetIOError(
                "invalid_cluster",
                "cluster contains unknown asset IDs: {}".format(sorted(unknown_members)),
            )
        overlap = seen_cluster_members & set(members)
        if overlap:
            raise DatasetIOError(
                "invalid_cluster",
                "asset IDs occur in multiple clusters: {}".format(sorted(overlap)),
            )
        try:
            relation = DatasetRelation(_string(cluster["relation"], "cluster relation"))
            parsed = DatasetCluster(
                members=members,
                relation=relation,
                evidence_complete=_boolean(
                    cluster["evidence_complete"],
                    "cluster evidence_complete",
                ),
                evidence_version=_string(
                    cluster["evidence_version"],
                    "cluster evidence_version",
                ),
            )
        except ValueError as error:
            raise DatasetIOError("invalid_cluster", str(error)) from error
        if parsed.cluster_id in seen_cluster_ids:
            raise DatasetIOError("duplicate_value", "cluster IDs must be unique")
        seen_cluster_ids.add(parsed.cluster_id)
        seen_cluster_members.update(members)
        clusters.append(parsed)

    raw_sidecar_paths = _list(document["sidecar_paths"], "sidecar_paths")
    if len(raw_assets) + len(raw_sidecar_paths) > MAX_DATASET_PLAN_FILE_RECORDS:
        raise DatasetIOError(
            "resource_limit",
            "assets and sidecars exceed the {} potential file-record limit".format(
                MAX_DATASET_PLAN_FILE_RECORDS,
            ),
        )
    sidecar_paths = _unique_paths(raw_sidecar_paths, "sidecar_paths")
    split_weights_value = _mapping(document["split_weights"], "split_weights")
    if not split_weights_value:
        raise DatasetIOError("invalid_value", "split_weights must not be empty")
    split_weights = []
    for name, weight in split_weights_value.items():
        split_name = _safe_component(name, "split name")
        parsed_weight = _number(weight, "split weight", minimum=0)
        if parsed_weight <= 0:
            raise DatasetIOError("invalid_number", "split weights must be positive")
        split_weights.append((split_name, parsed_weight))

    try:
        return DatasetPrepareInput(
            allowed_roots=roots,
            destination_root=destination,
            assets=tuple(assets),
            clusters=tuple(clusters),
            sidecar_paths=sidecar_paths,
            split_weights=tuple(sorted(split_weights)),
            split_seed=_string(document["split_seed"], "split_seed", allow_empty=True),
            dry_run=_boolean(document["dry_run"], "dry_run"),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_prepare_input", str(error)) from error


def dataset_prepare_input_from_json(
    payload: str | bytes,
    *,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
) -> DatasetPrepareInput:
    value = strict_json_loads(payload, maximum_bytes=maximum_bytes)
    return dataset_prepare_input_from_dict(_mapping(value, "dataset prepare input"))


def _file_proof_from_dict(value: Any, label: str) -> DatasetFileProof:
    proof = _mapping(value, label)
    _exact(
        proof,
        {
            "path",
            "resolved_path",
            "size",
            "mtime_ns",
            "ctime_ns",
            "generation_token",
            "digest_algorithm",
            "digest_hex",
            "identity_namespace",
            "identity_capability",
            "volume_id",
            "file_id",
            "stat_device",
            "stat_inode",
        },
        label,
    )
    try:
        return DatasetFileProof(
            path=_string(proof["path"], "{} path".format(label)),
            resolved_path=_string(
                proof["resolved_path"],
                "{} resolved_path".format(label),
            ),
            size=_integer(proof["size"], "{} size".format(label)),
            mtime_ns=_integer(proof["mtime_ns"], "{} mtime_ns".format(label)),
            ctime_ns=_integer(proof["ctime_ns"], "{} ctime_ns".format(label)),
            generation_token=_string(
                proof["generation_token"],
                "{} generation_token".format(label),
            ),
            digest_algorithm=_string(
                proof["digest_algorithm"],
                "{} digest_algorithm".format(label),
            ),
            digest_hex=_string(proof["digest_hex"], "{} digest_hex".format(label)),
            identity_namespace=_string(
                proof["identity_namespace"],
                "{} identity_namespace".format(label),
            ),
            identity_capability=_string(
                proof["identity_capability"],
                "{} identity_capability".format(label),
            ),
            volume_id=_integer(proof["volume_id"], "{} volume_id".format(label)),
            file_id=_string(proof["file_id"], "{} file_id".format(label)),
            stat_device=_integer(
                proof["stat_device"],
                "{} stat_device".format(label),
            ),
            stat_inode=_integer(
                proof["stat_inode"],
                "{} stat_inode".format(label),
                minimum=1,
            ),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_file_proof", str(error)) from error


def _split_manifest_from_dict(value: Any) -> SplitManifest:
    manifest = _mapping(value, "split_manifest")
    _exact(
        manifest,
        {"version", "seed", "split_weights", "assignments"},
        "split_manifest",
    )
    if manifest["version"] != "stable_split_v1":
        raise DatasetIOError(
            "unsupported_schema",
            "unsupported split manifest version",
        )
    weights_value = _mapping(manifest["split_weights"], "split_manifest.split_weights")
    if not weights_value:
        raise DatasetIOError("invalid_split_manifest", "split weights must not be empty")
    weights = []
    for name, weight in weights_value.items():
        split_name = _safe_component(name, "split name")
        parsed_weight = _number(weight, "split weight", minimum=0)
        if parsed_weight <= 0:
            raise DatasetIOError("invalid_split_manifest", "split weights must be positive")
        weights.append((split_name, parsed_weight))
    if not math.isclose(sum(weight for _name, weight in weights), 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise DatasetIOError("invalid_split_manifest", "stored split weights must sum to 1")

    raw_assignments = _list(manifest["assignments"], "split assignments")
    if len(raw_assignments) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetIOError(
            "resource_limit",
            "split assignments exceeds the {}-record limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    assignments = []
    seen_clusters = set()
    seen_members = set()
    for index, raw_assignment in enumerate(raw_assignments):
        assignment = _mapping(raw_assignment, "split assignment {}".format(index))
        _exact(
            assignment,
            {
                "cluster_id",
                "members",
                "split",
                "reason",
                "previous_splits",
            },
            "split assignment {}".format(index),
        )
        cluster_id = _string(assignment["cluster_id"], "assignment cluster_id")
        members = _unique_strings(assignment["members"], "assignment members")
        previous_splits = tuple(
            _safe_component(item, "assignment previous split")
            for item in _unique_strings(
                assignment["previous_splits"],
                "assignment previous_splits",
            )
        )
        if cluster_id in seen_clusters or seen_members & set(members):
            raise DatasetIOError(
                "invalid_split_manifest",
                "split assignments must have unique cluster IDs and members",
            )
        try:
            parsed_assignment = SplitAssignment(
                cluster_id=cluster_id,
                members=members,
                split=_safe_component(assignment["split"], "assignment split"),
                reason=SplitReason(_string(assignment["reason"], "assignment reason")),
                previous_splits=previous_splits,
            )
        except ValueError as error:
            raise DatasetIOError("invalid_split_manifest", str(error)) from error
        seen_clusters.add(cluster_id)
        seen_members.update(members)
        assignments.append(parsed_assignment)
    try:
        return SplitManifest(
            seed=_string(manifest["seed"], "split seed", allow_empty=True),
            split_weights=tuple(weights),
            assignments=tuple(assignments),
            version=_string(manifest["version"], "split manifest version"),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_split_manifest", str(error)) from error


def _keeper_record_from_dict(value: Any, index: int) -> KeeperRecord:
    keeper = _mapping(value, "keeper {}".format(index))
    _exact(
        keeper,
        {
            "cluster_id",
            "keeper_id",
            "explanations",
            "scores",
            "reasons",
        },
        "keeper {}".format(index),
    )
    explanations_value = _mapping(keeper["explanations"], "keeper explanations")
    scores_value = _mapping(keeper["scores"], "keeper scores")
    reasons_value = _mapping(keeper["reasons"], "keeper reasons")
    explanations = tuple(
        (
            _string(asset_id, "explanation asset ID"),
            _string(message, "keeper explanation"),
        )
        for asset_id, message in explanations_value.items()
    )
    scores = tuple(
        (
            _string(asset_id, "score asset ID"),
            _number(score, "keeper score"),
        )
        for asset_id, score in scores_value.items()
    )
    reasons = []
    for asset_id, raw_reasons in reasons_value.items():
        parsed_reasons = []
        for reason_index, raw_reason in enumerate(_list(raw_reasons, "keeper reasons")):
            reason = _mapping(
                raw_reason,
                "keeper reason {}".format(reason_index),
            )
            _exact(
                reason,
                {"code", "points", "message"},
                "keeper reason {}".format(reason_index),
            )
            parsed_reasons.append(
                KeeperReasonRecord(
                    code=_string(reason["code"], "keeper reason code"),
                    points=_number(reason["points"], "keeper reason points"),
                    message=_string(reason["message"], "keeper reason message"),
                )
            )
        reasons.append(
            (
                _string(asset_id, "reason asset ID"),
                tuple(parsed_reasons),
            )
        )
    explanation_ids = {asset_id for asset_id, _message in explanations}
    score_ids = {asset_id for asset_id, _score in scores}
    reason_ids = {asset_id for asset_id, _reasons in reasons}
    if explanation_ids != score_ids or explanation_ids != reason_ids:
        raise DatasetIOError(
            "invalid_keeper",
            "keeper explanations, scores, and reasons must cover the same assets",
        )
    try:
        return KeeperRecord(
            cluster_id=_string(keeper["cluster_id"], "keeper cluster_id"),
            keeper_id=_string(keeper["keeper_id"], "keeper_id"),
            explanations=explanations,
            scores=scores,
            reasons=tuple(reasons),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_keeper", str(error)) from error


def _file_action_from_dict(value: Any, label: str) -> DatasetFileAction:
    action = _mapping(value, label)
    _exact(
        action,
        {"source", "destination", "reference", "role", "sidecar_slot"},
        label,
    )
    reference = (
        None
        if action["reference"] is None
        else _file_proof_from_dict(action["reference"], "{} reference".format(label))
    )
    try:
        return DatasetFileAction(
            source=_file_proof_from_dict(action["source"], "{} source".format(label)),
            destination=_optional_string(
                action["destination"],
                "{} destination".format(label),
            ),
            reference=reference,
            role=_string(action["role"], "{} role".format(label)),
            sidecar_slot=_string(
                action["sidecar_slot"],
                "{} sidecar_slot".format(label),
                allow_empty=True,
            ),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_file_action", str(error)) from error


def _bundle_action_from_dict(value: Any, index: int) -> DatasetBundleAction:
    action = _mapping(value, "bundle action {}".format(index))
    _exact(
        action,
        {
            "action_id",
            "asset_id",
            "cluster_id",
            "split",
            "operation",
            "files",
            "keeper_id",
            "atomic",
        },
        "bundle action {}".format(index),
    )
    files = tuple(
        _file_action_from_dict(
            raw_file,
            "bundle action {} file {}".format(index, file_index),
        )
        for file_index, raw_file in enumerate(_list(action["files"], "bundle action files"))
    )
    source_paths = [os.path.normcase(os.path.abspath(item.source.path)) for item in files]
    if len(set(source_paths)) != len(source_paths):
        raise DatasetIOError("duplicate_value", "bundle action source paths must be unique")
    try:
        operation = DatasetOperation(
            _string(action["operation"], "bundle action operation"),
        )
        return DatasetBundleAction(
            action_id=_string(action["action_id"], "bundle action action_id"),
            asset_id=_string(action["asset_id"], "bundle action asset_id"),
            cluster_id=_string(action["cluster_id"], "bundle action cluster_id"),
            split=_string(action["split"], "bundle action split"),
            operation=operation,
            files=files,
            keeper_id=_optional_string(action["keeper_id"], "bundle action keeper_id"),
            atomic=_boolean(action["atomic"], "bundle action atomic"),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_bundle_action", str(error)) from error


def dataset_plan_from_dict(value: Mapping[str, Any]) -> DatasetPlan:
    document = _mapping(value, "dataset plan")
    _exact(
        document,
        {
            "schema",
            "schema_version",
            "plan_id",
            "allowed_roots",
            "destination_root",
            "split_manifest",
            "keepers",
            "actions",
            "dry_run",
            "executor_contract",
        },
        "dataset plan",
    )
    if document["schema"] != DATASET_PLAN_SCHEMA:
        raise DatasetIOError("unsupported_schema", "unsupported dataset plan schema")
    if _integer(document["schema_version"], "plan schema_version") != DATASET_PLAN_SCHEMA_VERSION:
        raise DatasetIOError("unsupported_schema", "unsupported dataset plan version")
    if document["executor_contract"] != EXECUTOR_CONTRACT:
        raise DatasetIOError("unsupported_schema", "unsupported dataset executor contract")
    roots = _unique_paths(document["allowed_roots"], "plan allowed_roots")
    if not roots:
        raise DatasetIOError("invalid_plan", "dataset plan roots must not be empty")
    raw_keepers = _list(document["keepers"], "plan keepers")
    if len(raw_keepers) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetIOError(
            "resource_limit",
            "plan keepers exceeds the {}-record limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    keepers = tuple(_keeper_record_from_dict(raw_keeper, index) for index, raw_keeper in enumerate(raw_keepers))
    if len({keeper.cluster_id for keeper in keepers}) != len(keepers):
        raise DatasetIOError("duplicate_value", "keeper cluster IDs must be unique")
    raw_actions = _list(document["actions"], "plan actions")
    if len(raw_actions) > MAX_DATASET_PLAN_ACTIONS:
        raise DatasetIOError(
            "resource_limit",
            "plan actions exceeds the {}-record limit".format(
                MAX_DATASET_PLAN_ACTIONS,
            ),
        )
    file_record_count = 0
    for raw_action in raw_actions:
        if isinstance(raw_action, Mapping):
            raw_files = raw_action.get("files")
            if isinstance(raw_files, list):
                file_record_count += len(raw_files)
                if file_record_count > MAX_DATASET_PLAN_FILE_RECORDS:
                    raise DatasetIOError(
                        "resource_limit",
                        "plan files exceeds the {}-record limit".format(
                            MAX_DATASET_PLAN_FILE_RECORDS,
                        ),
                    )
    actions = tuple(_bundle_action_from_dict(raw_action, index) for index, raw_action in enumerate(raw_actions))
    if len({action.action_id for action in actions}) != len(actions):
        raise DatasetIOError("duplicate_value", "bundle action IDs must be unique")
    try:
        # DatasetBundleAction and DatasetPlan constructors recompute and verify their immutable
        # content IDs.  No parser normalization can silently bless an altered document.
        return DatasetPlan(
            plan_id=_string(document["plan_id"], "plan_id"),
            allowed_roots=roots,
            destination_root=_string(
                document["destination_root"],
                "plan destination_root",
            ),
            split_manifest=_split_manifest_from_dict(document["split_manifest"]),
            keepers=keepers,
            actions=actions,
            dry_run=_boolean(document["dry_run"], "plan dry_run"),
            executor_contract=_string(
                document["executor_contract"],
                "executor_contract",
            ),
            schema=_string(document["schema"], "plan schema"),
            schema_version=_integer(
                document["schema_version"],
                "plan schema_version",
            ),
        )
    except ValueError as error:
        raise DatasetIOError("invalid_plan", str(error)) from error


def dataset_plan_from_json(
    payload: str | bytes,
    *,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
) -> DatasetPlan:
    value = strict_json_loads(payload, maximum_bytes=maximum_bytes)
    return dataset_plan_from_dict(_mapping(value, "dataset plan"))


def _issue_to_dict(issue: DatasetIssue) -> Dict[str, object]:
    return issue.to_dict()


def dataset_preparation_to_dict(result: DatasetPreparation) -> Dict[str, object]:
    return {
        "schema": PREPARATION_RESULT_SCHEMA,
        "schema_version": RESULT_SCHEMA_VERSION,
        "state": result.state.value,
        "complete": result.complete,
        "plan": result.plan.to_dict() if result.plan is not None else None,
        "issues": [_issue_to_dict(issue) for issue in result.issues],
    }


def plan_validation_to_dict(result: PlanValidation) -> Dict[str, object]:
    return {
        "schema": PLAN_VALIDATION_SCHEMA,
        "schema_version": RESULT_SCHEMA_VERSION,
        "valid": result.valid,
        "issues": [_issue_to_dict(issue) for issue in result.issues],
    }


def dataset_execution_report_to_dict(result: DatasetExecutionReport) -> Dict[str, object]:
    return {
        "schema": EXECUTION_REPORT_SCHEMA,
        "schema_version": RESULT_SCHEMA_VERSION,
        "plan_id": result.plan_id,
        "state": result.state.value,
        "code": result.code.value,
        "message": result.message,
        "changed": result.changed,
        "ok": result.ok,
        "files": [
            {
                "action_id": file_result.action_id,
                "source": file_result.source,
                "destination": file_result.destination,
                "quarantine_path": file_result.quarantine_path,
                "state": file_result.state.value,
                "changed": file_result.changed,
                "message": file_result.message,
            }
            for file_result in result.files
        ],
        "issues": [_issue_to_dict(issue) for issue in result.issues],
    }


def dataset_operation_summary_to_dict(result: DatasetOperationSummary) -> Dict[str, object]:
    return {
        "schema": OPERATION_SUMMARY_SCHEMA,
        "schema_version": RESULT_SCHEMA_VERSION,
        "plan_id": result.plan_id,
        "state": result.state.value,
        "document_path": result.document_path,
        "created_ns": result.created_ns,
        "file_count": result.file_count,
        "message": result.message,
    }


def dataset_operation_list_to_dict(
    results: Sequence[DatasetOperationSummary],
) -> Dict[str, object]:
    return {
        "schema": OPERATION_LIST_SCHEMA,
        "schema_version": RESULT_SCHEMA_VERSION,
        "operations": [dataset_operation_summary_to_dict(result) for result in results],
    }


def result_to_dict(
    result: (
        DatasetPreparation
        | PlanValidation
        | DatasetExecutionReport
        | DatasetOperationSummary
        | Sequence[DatasetOperationSummary]
    ),
) -> Dict[str, object]:
    if isinstance(result, DatasetPreparation):
        return dataset_preparation_to_dict(result)
    if isinstance(result, PlanValidation):
        return plan_validation_to_dict(result)
    if isinstance(result, DatasetExecutionReport):
        return dataset_execution_report_to_dict(result)
    if isinstance(result, DatasetOperationSummary):
        return dataset_operation_summary_to_dict(result)
    if isinstance(result, Sequence) and all(isinstance(item, DatasetOperationSummary) for item in result):
        return dataset_operation_list_to_dict(result)
    raise TypeError("unsupported dataset result type")


def _path_components(path: Path) -> Iterable[Path]:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    if current:
        yield current
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        yield current


def _validate_plain_path(path: Path, *, final_file: bool) -> os.stat_result:
    candidate = _absolute(path)
    for component in _path_components(candidate):
        try:
            file_stat = os.stat(component, follow_symlinks=False)
        except OSError as error:
            raise DatasetIOError("path_unavailable", str(error), component) from error
        if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat):
            raise DatasetIOError(
                "unsafe_path",
                "JSON path contains a symbolic link or reparse point",
                component,
            )
        is_final = component == candidate
        if is_final and final_file:
            if not stat.S_ISREG(file_stat.st_mode):
                raise DatasetIOError(
                    "unsafe_path",
                    "JSON source is not a regular file",
                    component,
                )
        elif not stat.S_ISDIR(file_stat.st_mode):
            raise DatasetIOError(
                "unsafe_path",
                "JSON path parent is not a directory",
                component,
            )
    return os.stat(candidate, follow_symlinks=False)


def read_json_file(
    path: str | Path,
    *,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    fs: Optional[FileSystemAdapter] = None,
) -> Any:
    file_system = fs or platform_file_system()
    source = _absolute(path)
    before = _validate_plain_path(source, final_file=True)
    _check_size(int(before.st_size), maximum_bytes)
    try:
        with file_system.open_readonly(source) as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino or opened.st_size != before.st_size:
                raise DatasetIOError(
                    "source_changed",
                    "JSON source changed while it was opened",
                    source,
                )
            payload = _read_bounded_json_payload(
                handle,
                maximum_bytes=maximum_bytes,
            )
            finished = os.fstat(handle.fileno())
            fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(opened, field, None) != getattr(finished, field, None) for field in fields):
                raise DatasetIOError(
                    "source_changed",
                    "JSON source changed while it was read",
                    source,
                )
    except DatasetIOError:
        raise
    except OSError as error:
        raise DatasetIOError("read_error", str(error), source) from error
    _check_size(len(payload), maximum_bytes)
    after = os.stat(source, follow_symlinks=False)
    if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino:
        raise DatasetIOError(
            "source_changed",
            "JSON source path changed after it was read",
            source,
        )
    return strict_json_loads(payload, maximum_bytes=maximum_bytes)


def _validate_destination_parent(path: Path) -> Path:
    destination = _absolute(path)
    parent = destination.parent
    _validate_plain_path(parent, final_file=False)
    if os.path.lexists(destination):
        raise DatasetIOError(
            "destination_conflict",
            "JSON destination already exists",
            destination,
        )
    return destination


def write_json_file(
    value: Any,
    path: str | Path,
    *,
    execute: bool = False,
    maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    fs: Optional[FileSystemAdapter] = None,
) -> JSONFileReceipt:
    _check_size(0, maximum_bytes)
    destination = _absolute(path)
    if not execute:
        size, digest = _consume_json_chunks(
            _iter_canonical_json_bytes(value),
            maximum_bytes=maximum_bytes,
        )
        return JSONFileReceipt(
            path=str(destination),
            size=size,
            sha256=digest,
            written=False,
            dry_run=True,
        )
    file_system = fs or platform_file_system()
    destination = _validate_destination_parent(destination)
    temporary = destination.with_name(".{}.{}.tmp".format(destination.name, uuid.uuid4().hex))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created_identity: Optional[Tuple[int, int]] = None
    try:
        fd = os.open(str(temporary), flags, 0o600)
        try:
            created = os.fstat(fd)
            created_identity = (int(created.st_dev), int(created.st_ino))
            digest_state = hashlib.sha256()
            size = 0
            for chunk in _iter_canonical_json_bytes(value):
                size += len(chunk)
                _check_size(size, maximum_bytes)
                digest_state.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short write while writing JSON")
                    remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        file_system.rename_no_replace(temporary, destination)
        file_system.fsync_directory(destination.parent)
    except FileExistsError as error:
        raise DatasetIOError(
            "destination_conflict",
            "JSON destination appeared during publication",
            destination,
        ) from error
    except OSError as error:
        raise DatasetIOError("write_error", str(error), destination) from error
    finally:
        if created_identity is not None:
            cleanup_created_regular_file(
                temporary,
                created_identity,
                file_system,
            )
    return JSONFileReceipt(
        path=str(destination),
        size=size,
        sha256=digest_state.hexdigest(),
        written=True,
        dry_run=False,
    )


def _consume_json_chunks(
    chunks: Iterable[bytes],
    *,
    maximum_bytes: int,
) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size += len(chunk)
        _check_size(size, maximum_bytes)
        digest.update(chunk)
    return size, digest.hexdigest()


class DatasetWorkflowFacade:
    """Thin, non-interactive boundary suitable for a future CLI."""

    def __init__(
        self,
        *,
        service: Optional[DatasetModeService] = None,
        executor: Optional[DatasetBundleExecutor] = None,
        state_root: Optional[str | Path] = None,
    ) -> None:
        if executor is not None and state_root is not None:
            raise ValueError("state_root cannot be supplied with an existing executor")
        if executor is not None:
            if service is not None and executor.service is not service:
                raise ValueError("facade service must be the executor's service")
            self.service = service or executor.service
            self.executor = executor
        else:
            self.service = service or DatasetModeService()
            self.executor = DatasetBundleExecutor(
                state_root=state_root,
                service=self.service,
            )

    def prepare(
        self,
        request: DatasetPrepareInput | Mapping[str, Any],
    ) -> DatasetPreparation:
        parsed = request if isinstance(request, DatasetPrepareInput) else dataset_prepare_input_from_dict(request)
        return self.service.prepare(
            parsed.assets,
            parsed.clusters,
            allowed_roots=parsed.allowed_roots,
            destination_root=parsed.destination_root,
            sidecar_paths=parsed.sidecar_paths,
            split_weights=dict(parsed.split_weights),
            split_seed=parsed.split_seed,
            dry_run=parsed.dry_run,
        )

    def prepare_json(
        self,
        payload: str | bytes,
        *,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> DatasetPreparation:
        return self.prepare(
            dataset_prepare_input_from_json(
                payload,
                maximum_bytes=maximum_bytes,
            )
        )

    def prepare_file(
        self,
        path: str | Path,
        *,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> DatasetPreparation:
        value = read_json_file(path, maximum_bytes=maximum_bytes)
        return self.prepare(dataset_prepare_input_from_dict(_mapping(value, "dataset prepare input")))

    def validate(self, plan: DatasetPlan) -> PlanValidation:
        return self.service.revalidate(plan)

    def validate_json(
        self,
        payload: str | bytes,
        *,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> PlanValidation:
        return self.validate(dataset_plan_from_json(payload, maximum_bytes=maximum_bytes))

    def validate_file(
        self,
        path: str | Path,
        *,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> PlanValidation:
        value = read_json_file(path, maximum_bytes=maximum_bytes)
        return self.validate(dataset_plan_from_dict(_mapping(value, "dataset plan")))

    def apply(
        self,
        plan: DatasetPlan,
        *,
        execute: bool = False,
    ) -> DatasetExecutionReport:
        return self.executor.apply(plan, execute=execute)

    def apply_json(
        self,
        payload: str | bytes,
        *,
        execute: bool = False,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> DatasetExecutionReport:
        return self.apply(
            dataset_plan_from_json(payload, maximum_bytes=maximum_bytes),
            execute=execute,
        )

    def apply_file(
        self,
        path: str | Path,
        *,
        execute: bool = False,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> DatasetExecutionReport:
        value = read_json_file(path, maximum_bytes=maximum_bytes)
        return self.apply(
            dataset_plan_from_dict(_mapping(value, "dataset plan")),
            execute=execute,
        )

    def list_operations(
        self,
        *,
        destination_root: Optional[str | Path] = None,
    ) -> Tuple[DatasetOperationSummary, ...]:
        return self.executor.list_operations(destination_root=destination_root)

    list = list_operations

    def restore(
        self,
        plan_id: str,
        *,
        destination_root: Optional[str | Path] = None,
        execute: bool = False,
    ) -> DatasetExecutionReport:
        return self.executor.restore(
            plan_id,
            destination_root=destination_root,
            execute=execute,
        )

    def finalize(
        self,
        plan_id: str,
        *,
        destination_root: Optional[str | Path] = None,
        execute: bool = False,
    ) -> DatasetExecutionReport:
        return self.executor.finalize(
            plan_id,
            destination_root=destination_root,
            execute=execute,
        )

    @staticmethod
    def result_dict(
        result: (
            DatasetPreparation
            | PlanValidation
            | DatasetExecutionReport
            | DatasetOperationSummary
            | Sequence[DatasetOperationSummary]
        ),
    ) -> Dict[str, object]:
        return result_to_dict(result)

    @staticmethod
    def write_result(
        result: (
            DatasetPreparation
            | PlanValidation
            | DatasetExecutionReport
            | DatasetOperationSummary
            | Sequence[DatasetOperationSummary]
        ),
        path: str | Path,
        *,
        execute: bool = False,
        maximum_bytes: int = DEFAULT_JSON_SIZE_LIMIT,
    ) -> JSONFileReceipt:
        return write_json_file(
            result_to_dict(result),
            path,
            execute=execute,
            maximum_bytes=maximum_bytes,
        )


DatasetIOFacade = DatasetWorkflowFacade
DatasetPrepareManifest = DatasetPrepareInput


__all__ = [
    "DEFAULT_JSON_SIZE_LIMIT",
    "JSON_READ_CHUNK_SIZE",
    "MAX_DATASET_PLAN_ACTIONS",
    "MAX_DATASET_PLAN_DOCUMENT_BYTES",
    "MAX_DATASET_PLAN_FILE_RECORDS",
    "EXECUTION_REPORT_SCHEMA",
    "OPERATION_LIST_SCHEMA",
    "OPERATION_SUMMARY_SCHEMA",
    "PLAN_VALIDATION_SCHEMA",
    "PREPARATION_RESULT_SCHEMA",
    "PREPARE_INPUT_SCHEMA",
    "PREPARE_INPUT_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "DatasetIOError",
    "DatasetIOFacade",
    "DatasetPrepareInput",
    "DatasetPrepareManifest",
    "DatasetWorkflowFacade",
    "JSONFileReceipt",
    "dataset_execution_report_to_dict",
    "dataset_operation_list_to_dict",
    "dataset_operation_summary_to_dict",
    "dataset_plan_from_dict",
    "dataset_plan_from_json",
    "dataset_preparation_to_dict",
    "dataset_prepare_input_from_dict",
    "dataset_prepare_input_from_json",
    "plan_validation_to_dict",
    "read_json_file",
    "read_json_stream",
    "result_to_dict",
    "strict_json_loads",
    "write_json_file",
]
