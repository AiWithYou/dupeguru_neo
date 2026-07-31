# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Read-only visual scan and query services.

This module deliberately stops at evidence and derived artifacts.  It has no delete, move, mark,
catalog-write, Qt, or CLI side effects.  Visual evidence is restricted to approximate
``similar``, ``transformed``, ``crop_candidate``, and ``related`` relations;
even byte-identical inputs are never represented as verified exact duplicates.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Tuple

from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
)
from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityCapability,
    IdentityConfidence,
    IdentityVerdict,
    get_file_identity,
    same_physical_file,
)
from core.pe.block import DifferentBlockCountError, NoBlocksError, avgdiff
from core.pe.cache_sqlite import SqliteCache, capture_source_binding
from core.pe.candidate_index import (
    CandidateQueryBudget,
    CandidateQueryCancelled,
    CandidateQueryLimitError,
    MultiIndexHamming,
    hamming_distance,
)
from core.pe.image_features import (
    COLOR_HISTOGRAM_LENGTH,
    DEFAULT_MAX_DECODE_PIXELS,
    FEATURE_VERSION,
    ImageDecodeError,
    ImageFeatureError,
    ImageQuality,
    ImageResourceLimitError,
    MAX_TILE_FINGERPRINTS,
    TILE_BOX_SCALE,
    TileFingerprint,
    decode_image_features,
)
from core.pe.matchblock import BLOCK_COUNT_PER_SIDE, MIN_ITERATIONS
from core.pe.photo import Photo
from core.reserved_paths import (
    is_reserved_internal_file,
    is_within_reserved_internal_directory,
)
from core.safe_json import JsonStructuralLimits, preflight_json_structure
from core.safe_walk import WalkEventKind, is_reparse_point, walk_no_follow
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus

VISUAL_REPORT_SCHEMA = "dupeguru.visual-report"
VISUAL_REPORT_SCHEMA_VERSION = 4
VISUAL_ARTIFACT_SCHEMA = "dupeguru.visual-feature-artifact"
VISUAL_ARTIFACT_SCHEMA_VERSION = 4
VISUAL_ALGORITHM = "pillow-normalized-phash-dhash-color-tiles-rgb-blocks"
VISUAL_ALGORITHM_VERSION = "{}+block15-v2".format(FEATURE_VERSION)
CATALOG_ARTIFACT_KIND = "visual_feature"
CATALOG_VERIFICATION_LEVEL = "candidate"
PHASH_BIT_WIDTH = 64
DHASH_BIT_WIDTH = 64
COLOR_HISTOGRAM_SAMPLE_COUNT = 32 * 32
MAX_REFINEMENT_BATCH_SIZE = 512
DEFAULT_MAX_IMAGES = 250_000
DEFAULT_MAX_CANDIDATE_PAIRS = 250_000
DEFAULT_MAX_MATCHES = 50_000
DEFAULT_MAX_SECONDS = 4 * 60 * 60
MAX_VISUAL_ARTIFACT_JSON_BYTES = 512 * 1024
MAX_VISUAL_ARTIFACT_PATH_CHARACTERS = 32_768
MAX_VISUAL_ARTIFACT_STRING_CHARACTERS = 32_768
VISUAL_ARTIFACT_JSON_LIMITS = JsonStructuralLimits(
    max_depth=8,
    max_container_entries=COLOR_HISTOGRAM_LENGTH,
    max_total_nodes=512,
    max_scalar_tokens=512,
    max_total_string_chars=128 * 1024,
    max_string_chars=MAX_VISUAL_ARTIFACT_STRING_CHARACTERS,
    max_scalar_chars=64,
)


class VisualRelation(str, Enum):
    SIMILAR = "similar"
    TRANSFORMED = "transformed"
    CROP_CANDIDATE = "crop_candidate"
    RELATED = "related"


class VisualReportKind(str, Enum):
    SCAN = "visual_scan"
    QUERY = "visual_query"


class VisualServiceError(Exception):
    """Invalid service input or an unsafe source boundary."""


class UnsafeVisualSourceError(VisualServiceError):
    def __init__(self, code, message, path):
        self.code = code
        self.path = str(path)
        super().__init__(message)


@dataclass(frozen=True)
class VisualScanReceipt(ScanReceipt):
    """Coverage receipt whose destructive gate is permanently closed."""

    @property
    def allows_destructive_actions(self):
        return False


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_exact_dict(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("{} contains unsupported fields".format(label))
    return value


def _require_string(
    value,
    label,
    *,
    maximum=MAX_VISUAL_ARTIFACT_STRING_CHARACTERS,
    allow_empty=False,
):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum or "\0" in value:
        raise ValueError("{} must be a bounded NUL-free string".format(label))
    return value


def _require_integer(value, label, *, minimum=0, maximum=(1 << 63) - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("{} must be an integer in its supported range".format(label))
    return value


def _parse_fixed_hex(value, label, width=16):
    if (
        not isinstance(value, str)
        or len(value) != width
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("{} must be fixed-width lowercase hexadecimal".format(label))
    return int(value, 16)


def _reject_visual_duplicate_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("visual artifact contains a duplicate key: {!r}".format(key))
        result[key] = value
    return result


def _strict_visual_json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("visual artifact contains a non-finite number")
    return result


def _reject_visual_json_constant(value):
    raise ValueError("visual artifact contains a non-finite value: {}".format(value))


def _utf8_size_exceeds(text, maximum):
    total = 0
    for offset in range(0, len(text), 64 * 1024):
        total += len(
            text[offset : offset + 64 * 1024].encode(
                "utf-8",
                errors="strict",
            )
        )
        if total > maximum:
            return True
    return False


def _stable_id(kind: str, value) -> str:
    digest = hashlib.sha256(_canonical_json_bytes(value)).hexdigest()
    return "{}:{}".format(kind, digest)


def _normalize_file_id(file_id):
    if isinstance(file_id, bytes):
        return "bytes", file_id.hex()
    return "integer", str(int(file_id))


def _restore_file_id(kind, value):
    if kind == "bytes":
        _require_string(value, "visual file ID", maximum=512)
        if len(value) % 2 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("visual byte file ID is not canonical hexadecimal")
        result = bytes.fromhex(value)
        if result.hex() != value:
            raise ValueError("visual byte file ID is not canonical hexadecimal")
        return result
    if kind == "integer":
        _require_string(value, "visual file ID", maximum=64)
        if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
            raise ValueError("visual integer file ID is not canonical")
        return int(value)
    raise ValueError("unknown file ID encoding")


def _canonical_generation_token_hex(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("visual generation token must be bounded hexadecimal")
    try:
        encoded = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("visual generation token is not hexadecimal") from error
    token = FileGenerationToken.from_encoded(encoded)
    canonical = token.encoded.hex()
    if value != canonical:
        raise ValueError("visual generation token is not canonical")
    return canonical


@dataclass(frozen=True)
class VisualAssetSnapshot:
    """Stable physical identity plus the content generation observed for one path."""

    asset_id: str
    path: str
    root: str
    size: int
    mtime_ns: int
    generation_token: str
    identity_namespace: str
    identity_capability: str
    identity_confidence: int
    volume_id: int
    file_id_kind: str
    file_id: str

    def __post_init__(self):
        _require_string(self.asset_id, "visual asset ID", maximum=256)
        _require_string(
            self.path,
            "visual asset path",
            maximum=MAX_VISUAL_ARTIFACT_PATH_CHARACTERS,
        )
        _require_string(
            self.root,
            "visual asset root",
            maximum=MAX_VISUAL_ARTIFACT_PATH_CHARACTERS,
            allow_empty=True,
        )
        if not os.path.isabs(self.path):
            raise ValueError("visual asset requires an absolute path and stable ID")
        if self.root and not os.path.isabs(self.root):
            raise ValueError("visual asset root must be absolute")
        _require_integer(self.size, "visual asset size")
        _require_integer(self.mtime_ns, "visual asset timestamp")
        _require_integer(
            self.volume_id,
            "visual asset volume ID",
            maximum=(1 << 64) - 1,
        )
        _canonical_generation_token_hex(self.generation_token)
        _require_string(
            self.identity_namespace,
            "visual identity namespace",
            maximum=128,
        )
        _require_string(
            self.identity_capability,
            "visual identity capability",
            maximum=128,
        )
        _require_integer(
            self.identity_confidence,
            "visual identity confidence",
            maximum=255,
        )
        _require_string(
            self.file_id_kind,
            "visual file ID kind",
            maximum=16,
        )
        _require_string(self.file_id, "visual file ID", maximum=512)
        IdentityCapability(self.identity_capability)
        IdentityConfidence(self.identity_confidence)
        _restore_file_id(self.file_id_kind, self.file_id)

    @property
    def identity(self):
        return FileIdentity(
            namespace=self.identity_namespace,
            volume_id=self.volume_id,
            file_id=_restore_file_id(self.file_id_kind, self.file_id),
            capability=IdentityCapability(self.identity_capability),
            confidence=IdentityConfidence(self.identity_confidence),
        )

    @property
    def generation(self):
        return self.size, self.mtime_ns, self.generation_token

    def to_dict(self):
        return {
            "asset_id": self.asset_id,
            "path": self.path,
            "root": self.root,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "generation_token": self.generation_token,
            "identity": {
                "namespace": self.identity_namespace,
                "capability": self.identity_capability,
                "confidence": self.identity_confidence,
                "volume_id": self.volume_id,
                "file_id_kind": self.file_id_kind,
                "file_id": self.file_id,
            },
        }

    @classmethod
    def from_dict(cls, value):
        value = _require_exact_dict(
            value,
            {
                "asset_id",
                "path",
                "root",
                "size",
                "mtime_ns",
                "generation_token",
                "identity",
            },
            "visual asset snapshot",
        )
        identity = _require_exact_dict(
            value["identity"],
            {
                "namespace",
                "capability",
                "confidence",
                "volume_id",
                "file_id_kind",
                "file_id",
            },
            "visual asset identity",
        )
        return cls(
            asset_id=_require_string(
                value["asset_id"],
                "visual asset ID",
                maximum=256,
            ),
            path=_require_string(
                value["path"],
                "visual asset path",
                maximum=MAX_VISUAL_ARTIFACT_PATH_CHARACTERS,
            ),
            root=_require_string(
                value["root"],
                "visual asset root",
                maximum=MAX_VISUAL_ARTIFACT_PATH_CHARACTERS,
                allow_empty=True,
            ),
            size=_require_integer(value["size"], "visual asset size"),
            mtime_ns=_require_integer(
                value["mtime_ns"],
                "visual asset timestamp",
            ),
            generation_token=_require_string(
                value["generation_token"],
                "visual generation token",
                maximum=512,
            ),
            identity_namespace=_require_string(
                identity["namespace"],
                "visual identity namespace",
                maximum=128,
            ),
            identity_capability=_require_string(
                identity["capability"],
                "visual identity capability",
                maximum=128,
            ),
            identity_confidence=_require_integer(
                identity["confidence"],
                "visual identity confidence",
                maximum=255,
            ),
            volume_id=_require_integer(
                identity["volume_id"],
                "visual volume ID",
                maximum=(1 << 64) - 1,
            ),
            file_id_kind=_require_string(
                identity["file_id_kind"],
                "visual file ID kind",
                maximum=16,
            ),
            file_id=_require_string(
                identity["file_id"],
                "visual file ID",
                maximum=512,
            ),
        )


def _artifact_parameters_hash(block_count, orientation_count):
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "feature_version": FEATURE_VERSION,
                "block_count_per_side": block_count,
                "phash_bit_width": PHASH_BIT_WIDTH,
                "dhash_bit_width": DHASH_BIT_WIDTH,
                "color_histogram_bins": COLOR_HISTOGRAM_LENGTH,
                "max_tile_fingerprints": MAX_TILE_FINGERPRINTS,
                "orientation_count": orientation_count,
                "animated_frame_policy": "first_frame",
                "alpha_policy": "composite_srgb_white",
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class VisualFeatureArtifact:
    """Lightweight derived data suitable for reports and catalog persistence.

    The expensive 15x15 RGB blocks remain in SQLite, while display thumbnails are decoded lazily
    by the UI.  ``cache_record_id`` is only a run-local refinement reference and is deliberately
    cleared in portable catalog payloads.  A catalog artifact can therefore be paged without
    retaining decoded pixels.
    """

    asset: VisualAssetSnapshot
    dimensions: Tuple[int, int]
    frame_count: int
    phashes: Tuple[int, ...]
    dhashes: Tuple[int, ...]
    color_histogram: Tuple[int, ...]
    tile_fingerprints: Tuple[TileFingerprint, ...]
    quality: ImageQuality
    thumbnail_key: str
    cache_record_id: int = 0
    feature_version: str = FEATURE_VERSION
    block_count_per_side: int = BLOCK_COUNT_PER_SIDE
    parameters_hash: str = field(init=False)

    def __post_init__(self):
        if self.feature_version != FEATURE_VERSION:
            raise ValueError("visual artifact feature version is incompatible")
        if self.block_count_per_side != BLOCK_COUNT_PER_SIDE:
            raise ValueError("visual artifact block policy is incompatible")
        if len(self.phashes) not in {1, 8} or len(self.dhashes) != len(self.phashes):
            raise ValueError("visual artifact requires one or eight aligned pHash/dHash orientations")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 or value >= 1 << PHASH_BIT_WIDTH
            for value in self.phashes
        ):
            raise ValueError("visual artifact pHash exceeds 64 bits")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 or value >= 1 << DHASH_BIT_WIDTH
            for value in self.dhashes
        ):
            raise ValueError("visual artifact dHash exceeds 64 bits")
        if (
            len(self.color_histogram) != COLOR_HISTOGRAM_LENGTH
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in self.color_histogram)
            or sum(self.color_histogram) != COLOR_HISTOGRAM_SAMPLE_COUNT
        ):
            raise ValueError("visual artifact color histogram is incompatible")
        if (
            len(self.tile_fingerprints) > MAX_TILE_FINGERPRINTS
            or any(not isinstance(item, TileFingerprint) for item in self.tile_fingerprints)
            or len({item.kind for item in self.tile_fingerprints}) != len(self.tile_fingerprints)
        ):
            raise ValueError("visual artifact tile fingerprints are incompatible")
        if not isinstance(self.quality, ImageQuality):
            raise ValueError("visual artifact requires measured image quality")
        if len(self.dimensions) != 2 or any(
            not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0
            for dimension in self.dimensions
        ):
            raise ValueError("visual artifact dimensions must be positive")
        if (
            not isinstance(self.frame_count, int)
            or isinstance(self.frame_count, bool)
            or self.frame_count <= 0
            or not self.thumbnail_key
        ):
            raise ValueError("visual artifact frame count and thumbnail key are required")
        if (
            not isinstance(self.cache_record_id, int)
            or isinstance(self.cache_record_id, bool)
            or self.cache_record_id < 0
        ):
            raise ValueError("visual artifact cache record ID must be a non-negative integer")
        object.__setattr__(
            self,
            "parameters_hash",
            _artifact_parameters_hash(self.block_count_per_side, len(self.phashes)),
        )

    @property
    def asset_id(self):
        return self.asset.asset_id

    @property
    def orientation_count(self):
        return len(self.phashes)

    def _feature_dict(self):
        return {
            "algorithm": VISUAL_ALGORITHM,
            "algorithm_version": VISUAL_ALGORITHM_VERSION,
            "feature_version": self.feature_version,
            "parameters_hash": self.parameters_hash,
            "block_count_per_side": self.block_count_per_side,
            "dimensions": list(self.dimensions),
            "frame_count": self.frame_count,
            "phashes": ["{:016x}".format(value) for value in self.phashes],
            "dhashes": ["{:016x}".format(value) for value in self.dhashes],
            "color_histogram": list(self.color_histogram),
            "tile_fingerprints": [
                {
                    "kind": item.kind,
                    "phash": "{:016x}".format(item.phash),
                    "dhash": "{:016x}".format(item.dhash),
                    "box": list(item.box),
                }
                for item in self.tile_fingerprints
            ],
            "quality": {
                "bit_depth": self.quality.bit_depth,
                "exif_count": self.quality.exif_count,
                "metadata_count": self.quality.metadata_count,
                "jpeg_artifact_score": self.quality.jpeg_artifact_score,
            },
            "thumbnail_key": self.thumbnail_key,
            "cache_record_id": self.cache_record_id or None,
            "block_storage": "sqlite",
        }

    def to_dict(self):
        return {
            "schema": VISUAL_ARTIFACT_SCHEMA,
            "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
            "asset": self.asset.to_dict(),
            "feature": self._feature_dict(),
            "safety": {
                "verification_level": CATALOG_VERIFICATION_LEVEL,
                "verified_exact": False,
                "destructive_actions_allowed": False,
            },
        }

    def to_report_dict(self):
        """Return the compact report form; the asset snapshot is stored once in ``assets``."""

        return {
            "schema": VISUAL_ARTIFACT_SCHEMA,
            "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
            "asset_id": self.asset_id,
            "feature": self._feature_dict(),
            "safety": {
                "verification_level": CATALOG_VERIFICATION_LEVEL,
                "verified_exact": False,
                "destructive_actions_allowed": False,
            },
        }

    def to_json(self):
        return _canonical_json_bytes(self.to_dict()).decode("utf-8")

    def to_catalog_payload(self):
        """Return keyword-compatible pure data for ``Catalog.put_artifact`` except content ID."""

        value = self.to_dict()
        value["feature"]["cache_record_id"] = None
        return {
            "kind": CATALOG_ARTIFACT_KIND,
            "algorithm": VISUAL_ALGORITHM,
            "algorithm_version": VISUAL_ALGORITHM_VERSION,
            "parameters_hash": self.parameters_hash,
            "verification_level": CATALOG_VERIFICATION_LEVEL,
            "value": _canonical_json_bytes(value),
        }

    @classmethod
    def from_dict(cls, value):
        value = _require_exact_dict(
            value,
            {
                "schema",
                "schema_version",
                "asset",
                "feature",
                "safety",
            },
            "visual artifact",
        )
        if value.get("schema") != VISUAL_ARTIFACT_SCHEMA:
            raise ValueError("unsupported visual artifact schema")
        if (
            type(value.get("schema_version")) is not int
            or value.get("schema_version") != VISUAL_ARTIFACT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported visual artifact schema version")
        feature = _require_exact_dict(
            value["feature"],
            {
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
            },
            "visual artifact feature",
        )
        if feature.get("block_storage") != "sqlite":
            raise ValueError("visual artifact block storage is unsupported")
        if feature.get("algorithm") != VISUAL_ALGORITHM:
            raise ValueError("unsupported visual artifact algorithm")
        if feature.get("algorithm_version") != VISUAL_ALGORITHM_VERSION:
            raise ValueError("unsupported visual artifact algorithm version")
        safety = _require_exact_dict(
            value["safety"],
            {
                "verification_level",
                "verified_exact",
                "destructive_actions_allowed",
            },
            "visual artifact safety declaration",
        )
        if (
            safety.get("verification_level") != CATALOG_VERIFICATION_LEVEL
            or safety.get("verified_exact") is not False
            or safety.get("destructive_actions_allowed") is not False
        ):
            raise ValueError("visual artifact safety declaration is incompatible")

        dimensions = feature["dimensions"]
        if (
            type(dimensions) is not list
            or len(dimensions) != 2
            or any(type(item) is not int or not 0 < item <= (1 << 31) - 1 for item in dimensions)
        ):
            raise ValueError("visual artifact dimensions must be two bounded positive integers")
        frame_count = _require_integer(
            feature["frame_count"],
            "visual artifact frame count",
            minimum=1,
            maximum=(1 << 31) - 1,
        )
        if type(feature["phashes"]) is not list or len(feature["phashes"]) not in {1, 8}:
            raise ValueError("visual artifact pHash list has an invalid shape")
        if type(feature["dhashes"]) is not list or len(feature["dhashes"]) != len(feature["phashes"]):
            raise ValueError("visual artifact dHash list has an invalid shape")
        phashes = tuple(_parse_fixed_hex(item, "visual artifact pHash") for item in feature["phashes"])
        dhashes = tuple(_parse_fixed_hex(item, "visual artifact dHash") for item in feature["dhashes"])

        histogram_value = feature["color_histogram"]
        if (
            type(histogram_value) is not list
            or len(histogram_value) != COLOR_HISTOGRAM_LENGTH
            or any(type(item) is not int or not 0 <= item <= COLOR_HISTOGRAM_SAMPLE_COUNT for item in histogram_value)
            or sum(histogram_value) != COLOR_HISTOGRAM_SAMPLE_COUNT
        ):
            raise ValueError("visual artifact color histogram has an invalid shape")

        tile_values = feature["tile_fingerprints"]
        if type(tile_values) is not list or len(tile_values) > MAX_TILE_FINGERPRINTS:
            raise ValueError("visual artifact tile fingerprint list is invalid")
        tiles = []
        for tile_value in tile_values:
            tile_value = _require_exact_dict(
                tile_value,
                {"kind", "phash", "dhash", "box"},
                "visual artifact tile fingerprint",
            )
            box = tile_value["box"]
            if type(box) is not list or len(box) != 4 or any(type(item) is not int for item in box):
                raise ValueError("visual artifact tile box has an invalid shape")
            tiles.append(
                TileFingerprint(
                    _require_string(
                        tile_value["kind"],
                        "visual artifact tile kind",
                        maximum=32,
                    ),
                    _parse_fixed_hex(
                        tile_value["phash"],
                        "visual artifact tile pHash",
                    ),
                    _parse_fixed_hex(
                        tile_value["dhash"],
                        "visual artifact tile dHash",
                    ),
                    tuple(box),
                )
            )

        quality_value = _require_exact_dict(
            feature["quality"],
            {
                "bit_depth",
                "exif_count",
                "metadata_count",
                "jpeg_artifact_score",
            },
            "visual artifact quality",
        )
        quality_counts = {
            name: _require_integer(
                quality_value[name],
                "visual artifact quality {}".format(name),
                maximum=(1 << 31) - 1,
            )
            for name in ("bit_depth", "exif_count", "metadata_count")
        }
        jpeg_artifact_score = quality_value["jpeg_artifact_score"]
        if (
            not isinstance(jpeg_artifact_score, (int, float))
            or isinstance(jpeg_artifact_score, bool)
            or not math.isfinite(jpeg_artifact_score)
            or not 0 <= jpeg_artifact_score <= 1
        ):
            raise ValueError("visual artifact JPEG artifact score is outside its domain")
        cache_record_id = feature["cache_record_id"]
        if cache_record_id is None:
            cache_record_id = 0
        else:
            cache_record_id = _require_integer(
                cache_record_id,
                "visual artifact cache record ID",
                maximum=(1 << 63) - 1,
            )
        _parse_fixed_hex(
            feature["parameters_hash"],
            "visual artifact parameters hash",
            width=64,
        )
        _parse_fixed_hex(
            feature["thumbnail_key"],
            "visual artifact thumbnail key",
            width=64,
        )
        artifact = cls(
            asset=VisualAssetSnapshot.from_dict(value["asset"]),
            dimensions=tuple(dimensions),
            frame_count=frame_count,
            phashes=phashes,
            dhashes=dhashes,
            color_histogram=tuple(histogram_value),
            tile_fingerprints=tuple(tiles),
            quality=ImageQuality(
                bit_depth=quality_counts["bit_depth"],
                exif_count=quality_counts["exif_count"],
                metadata_count=quality_counts["metadata_count"],
                jpeg_artifact_score=jpeg_artifact_score,
            ),
            thumbnail_key=_require_string(
                feature["thumbnail_key"],
                "visual artifact thumbnail key",
                maximum=256,
            ),
            cache_record_id=cache_record_id,
            feature_version=_require_string(
                feature["feature_version"],
                "visual artifact feature version",
                maximum=256,
            ),
            block_count_per_side=_require_integer(
                feature["block_count_per_side"],
                "visual artifact block count",
                minimum=1,
                maximum=1_024,
            ),
        )
        if feature.get("parameters_hash") != artifact.parameters_hash:
            raise ValueError("visual artifact parameters hash does not match its content")
        return artifact

    @classmethod
    def from_json(cls, value):
        if isinstance(value, bytes):
            if not value or len(value) > MAX_VISUAL_ARTIFACT_JSON_BYTES:
                raise ValueError("visual artifact JSON has an invalid byte length")
            try:
                text = value.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ValueError("visual artifact JSON is not valid UTF-8") from error
        elif isinstance(value, str):
            try:
                oversized = (
                    not value
                    or len(value) > MAX_VISUAL_ARTIFACT_JSON_BYTES
                    or _utf8_size_exceeds(
                        value,
                        MAX_VISUAL_ARTIFACT_JSON_BYTES,
                    )
                )
            except UnicodeEncodeError as error:
                raise ValueError("visual artifact JSON contains invalid Unicode") from error
            if oversized:
                raise ValueError("visual artifact JSON has an invalid byte length")
            text = value
        else:
            raise TypeError("visual artifact JSON must be text or bytes")
        preflight_json_structure(
            text,
            limits=VISUAL_ARTIFACT_JSON_LIMITS,
            label="visual artifact JSON",
        )
        try:
            parsed = json.loads(
                text,
                object_pairs_hook=_reject_visual_duplicate_object_pairs,
                parse_constant=_reject_visual_json_constant,
                parse_float=_strict_visual_json_float,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("visual artifact is not strict JSON") from error
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class VisualEvidence:
    first_id: str
    second_id: str
    relation: VisualRelation
    score: float
    block_similarity: int
    phash_distance: int
    dhash_distance: int
    color_histogram_distance: float
    first_fingerprint_kind: str
    second_fingerprint_kind: str
    first_fingerprint_box: Tuple[int, int, int, int]
    second_fingerprint_box: Tuple[int, int, int, int]
    crop_verification: str
    transformation_kind: str
    phash_orientation: int
    block_orientation: int
    similarity_threshold: int
    phash_radius: int
    evidence_id: str = field(init=False)

    def __post_init__(self):
        if not self.first_id or not self.second_id or self.first_id == self.second_id:
            raise ValueError("visual evidence requires two distinct assets")
        if not isinstance(self.relation, VisualRelation):
            raise ValueError("visual evidence relation is unsupported")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("visual evidence score must be between zero and one")
        for name in (
            "block_similarity",
            "phash_distance",
            "dhash_distance",
            "phash_orientation",
            "block_orientation",
            "similarity_threshold",
            "phash_radius",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("visual evidence metrics must be integers")
        if not 0 <= self.block_similarity <= 100:
            raise ValueError("block similarity must be between zero and 100")
        if not 0 <= self.similarity_threshold <= 100:
            raise ValueError("visual evidence threshold must be between zero and 100")
        if not 0 <= self.phash_distance <= self.phash_radius <= PHASH_BIT_WIDTH:
            raise ValueError("pHash evidence exceeds its configured radius")
        if not 0 <= self.dhash_distance <= DHASH_BIT_WIDTH:
            raise ValueError("dHash evidence exceeds 64 bits")
        if not (0 <= self.phash_orientation <= 7 and 0 <= self.block_orientation <= 7):
            raise ValueError("visual evidence orientation is out of range")
        if (
            not isinstance(self.color_histogram_distance, (int, float))
            or isinstance(self.color_histogram_distance, bool)
            or not math.isfinite(self.color_histogram_distance)
            or not 0 <= self.color_histogram_distance <= 1
        ):
            raise ValueError("color histogram evidence must be between zero and one")
        valid_kinds = {"whole", "center_90", "center_75", "center_50", "content"}
        if self.first_fingerprint_kind not in valid_kinds or self.second_fingerprint_kind not in valid_kinds:
            raise ValueError("visual evidence fingerprint kind is unsupported")
        for box in (
            self.first_fingerprint_box,
            self.second_fingerprint_box,
        ):
            if (
                not isinstance(box, tuple)
                or len(box) != 4
                or any(not isinstance(value, int) or isinstance(value, bool) for value in box)
            ):
                raise ValueError("visual evidence fingerprint box is invalid")
            left, top, right, bottom = box
            if not (0 <= left < right <= TILE_BOX_SCALE and 0 <= top < bottom <= TILE_BOX_SCALE):
                raise ValueError("visual evidence fingerprint box is out of bounds")
        if self.relation is VisualRelation.SIMILAR and self.block_similarity < self.similarity_threshold:
            raise ValueError("similar evidence must meet the configured threshold")
        if self.relation is VisualRelation.RELATED and self.block_similarity >= self.similarity_threshold:
            raise ValueError("related evidence must remain below the similar threshold")
        crop_fingerprint = self.first_fingerprint_kind != "whole" or self.second_fingerprint_kind != "whole"
        if (self.relation is VisualRelation.CROP_CANDIDATE) != crop_fingerprint:
            raise ValueError("crop-candidate evidence requires a bounded tile fingerprint")
        expected_crop_verification = "bounded_fingerprint_candidate" if crop_fingerprint else "not_applicable"
        if self.crop_verification != expected_crop_verification:
            raise ValueError("crop verification declaration is incompatible")
        if self.transformation_kind not in {
            "none",
            "orientation",
            "scaled_or_resized",
        }:
            raise ValueError("visual transformation kind is unsupported")
        if (self.relation is VisualRelation.TRANSFORMED) != (self.transformation_kind != "none"):
            raise ValueError("transformed evidence declaration is incompatible")
        if self.relation is VisualRelation.TRANSFORMED and crop_fingerprint:
            raise ValueError("transformed evidence cannot contain crop evidence")
        object.__setattr__(
            self,
            "evidence_id",
            _stable_id(
                "visual-evidence",
                {
                    "first": self.first_id,
                    "second": self.second_id,
                    "relation": self.relation.value,
                    "threshold": self.similarity_threshold,
                    "radius": self.phash_radius,
                    "phash_distance": self.phash_distance,
                    "dhash_distance": self.dhash_distance,
                    "color_histogram_distance": self.color_histogram_distance,
                    "first_fingerprint_kind": self.first_fingerprint_kind,
                    "second_fingerprint_kind": self.second_fingerprint_kind,
                    "first_fingerprint_box": list(self.first_fingerprint_box),
                    "second_fingerprint_box": list(self.second_fingerprint_box),
                    "crop_verification": self.crop_verification,
                    "transformation_kind": self.transformation_kind,
                    "block_similarity": self.block_similarity,
                    "phash_orientation": self.phash_orientation,
                    "block_orientation": self.block_orientation,
                },
            ),
        )

    @property
    def allows_destructive_actions(self):
        return False

    def to_dict(self):
        return {
            "evidence_id": self.evidence_id,
            "first_id": self.first_id,
            "second_id": self.second_id,
            "relation": self.relation.value,
            "score": self.score,
            "algorithm": VISUAL_ALGORITHM,
            "algorithm_version": VISUAL_ALGORITHM_VERSION,
            "metrics": {
                "block_similarity": self.block_similarity,
                "phash_distance": self.phash_distance,
                "dhash_distance": self.dhash_distance,
                "color_histogram_distance": self.color_histogram_distance,
                "first_fingerprint_kind": self.first_fingerprint_kind,
                "second_fingerprint_kind": self.second_fingerprint_kind,
                "first_fingerprint_box": list(self.first_fingerprint_box),
                "second_fingerprint_box": list(self.second_fingerprint_box),
                "crop_verification": self.crop_verification,
                "transformation_kind": self.transformation_kind,
                "phash_orientation": self.phash_orientation,
                "block_orientation": self.block_orientation,
                "similarity_threshold": self.similarity_threshold,
                "phash_radius": self.phash_radius,
            },
            "safety": {
                "verified_exact": False,
                "destructive_actions_allowed": False,
            },
        }


@dataclass(frozen=True)
class VisualCandidateStats:
    indexed_images: int
    possible_pairs: int
    candidate_pairs: int
    refined_pairs: int
    similar_count: int
    transformed_count: int
    crop_candidate_count: int
    related_count: int
    phash_radius: int

    def __post_init__(self):
        counts = (
            self.indexed_images,
            self.possible_pairs,
            self.candidate_pairs,
            self.refined_pairs,
            self.similar_count,
            self.transformed_count,
            self.crop_candidate_count,
            self.related_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("visual candidate statistics must not be negative")
        if self.candidate_pairs > self.possible_pairs or self.refined_pairs > self.candidate_pairs:
            raise ValueError("visual candidate statistics are inconsistent")
        if (
            self.similar_count + self.transformed_count + self.crop_candidate_count + self.related_count
            > self.refined_pairs
        ):
            raise ValueError("visual evidence counts exceed refined candidates")
        if not 0 <= self.phash_radius <= PHASH_BIT_WIDTH:
            raise ValueError("visual candidate radius is invalid")

    @property
    def reduction_ratio(self):
        if not self.possible_pairs:
            return 1.0
        return 1 - (self.candidate_pairs / self.possible_pairs)

    def to_dict(self):
        return {
            "indexed_images": self.indexed_images,
            "possible_pairs": self.possible_pairs,
            "candidate_pairs": self.candidate_pairs,
            "refined_pairs": self.refined_pairs,
            "similar_count": self.similar_count,
            "transformed_count": self.transformed_count,
            "crop_candidate_count": self.crop_candidate_count,
            "related_count": self.related_count,
            "phash_radius": self.phash_radius,
            "reduction_ratio": self.reduction_ratio,
        }


@dataclass(frozen=True)
class VisualScanConfig:
    similarity_threshold: int = 80
    phash_radius: int = 8
    dhash_distance: int = 24
    color_histogram_distance: float = 0.55
    match_scaled: bool = False
    match_rotated: bool = False
    match_crops: bool = True
    include_related: bool = True
    dry_run: bool = True
    max_images: int = DEFAULT_MAX_IMAGES
    max_candidate_pairs: int = DEFAULT_MAX_CANDIDATE_PAIRS
    max_matches: int = DEFAULT_MAX_MATCHES
    max_seconds: float = DEFAULT_MAX_SECONDS

    def __post_init__(self):
        if (
            not isinstance(self.similarity_threshold, int)
            or isinstance(self.similarity_threshold, bool)
            or not 0 <= self.similarity_threshold <= 100
        ):
            raise ValueError("similarity_threshold must be an integer between 0 and 100")
        if (
            not isinstance(self.phash_radius, int)
            or isinstance(self.phash_radius, bool)
            or not 0 <= self.phash_radius <= PHASH_BIT_WIDTH
        ):
            raise ValueError("phash_radius must be an integer between 0 and 64")
        if (
            not isinstance(self.dhash_distance, int)
            or isinstance(self.dhash_distance, bool)
            or not 0 <= self.dhash_distance <= DHASH_BIT_WIDTH
        ):
            raise ValueError("dhash_distance must be an integer between 0 and 64")
        if (
            isinstance(self.color_histogram_distance, bool)
            or not isinstance(self.color_histogram_distance, (int, float))
            or not math.isfinite(self.color_histogram_distance)
            or not 0 <= self.color_histogram_distance <= 1
        ):
            raise ValueError("color_histogram_distance must be between 0 and 1")
        for name in (
            "match_scaled",
            "match_rotated",
            "match_crops",
            "include_related",
            "dry_run",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("{} must be boolean".format(name))
        if not self.dry_run:
            raise ValueError("visual service is read-only and only supports dry_run=True")
        for name in ("max_images", "max_candidate_pairs", "max_matches"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be a finite positive number")

    def to_dict(self):
        return {
            "similarity_threshold": self.similarity_threshold,
            "phash_radius": self.phash_radius,
            "dhash_distance": self.dhash_distance,
            "color_histogram_distance": self.color_histogram_distance,
            "match_scaled": self.match_scaled,
            "match_rotated": self.match_rotated,
            "match_crops": self.match_crops,
            "include_related": self.include_related,
            "dry_run": self.dry_run,
            "source_read_only": True,
            "max_images": self.max_images,
            "max_candidate_pairs": self.max_candidate_pairs,
            "max_matches": self.max_matches,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class _FingerprintEntry:
    asset_id: str
    kind: str
    orientation: int
    phash: int
    dhash: int
    box: Tuple[int, int, int, int]


@dataclass(frozen=True)
class _CandidateWork:
    first_id: str
    second_id: str
    phash_distance: int
    dhash_distance: int
    color_histogram_distance: float
    first_kind: str
    second_kind: str
    first_box: Tuple[int, int, int, int]
    second_box: Tuple[int, int, int, int]
    phash_orientation: int

    @property
    def rank(self):
        return (
            self.phash_distance / PHASH_BIT_WIDTH
            + self.dhash_distance / DHASH_BIT_WIDTH
            + self.color_histogram_distance
        )


def _candidate_sort_key(item):
    return (
        item.rank,
        item.first_kind != "whole" or item.second_kind != "whole",
        item.phash_distance,
        item.dhash_distance,
        item.color_histogram_distance,
        item.second_id,
    )


@dataclass(frozen=True)
class _WorstCandidate:
    """Reverse a candidate key so heap root is the worst retained result."""

    work: _CandidateWork

    def __lt__(self, other):
        if not isinstance(other, _WorstCandidate):
            return NotImplemented
        return _candidate_sort_key(self.work) > _candidate_sort_key(other.work)


def _receipt_to_dict(receipt):
    return {
        "scan_id": receipt.scan_id,
        "status": receipt.status.value,
        "complete": receipt.complete,
        # Coverage completeness remains visible, but visual evidence can never authorize
        # destructive work even if a foreign ScanReceipt instance is supplied.
        "allows_destructive_actions": False,
        "discovered": receipt.discovered,
        "analyzed": receipt.analyzed,
        "skipped": receipt.skipped,
        "failed": receipt.failed,
        "started_at_ns": receipt.started_at_ns,
        "finished_at_ns": receipt.finished_at_ns,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in receipt.issues
        ],
    }


@dataclass(frozen=True)
class VisualReport:
    report_id: str
    kind: VisualReportKind
    roots: Tuple[str, ...]
    config: VisualScanConfig
    assets: Tuple[VisualAssetSnapshot, ...]
    artifacts: Tuple[VisualFeatureArtifact, ...]
    evidence: Tuple[VisualEvidence, ...]
    candidate_stats: VisualCandidateStats
    scan_receipt: ScanReceipt
    reference_asset_id: str = ""
    created_at_ns: int = 0

    def __post_init__(self):
        if not self.report_id:
            raise ValueError("visual report ID must not be empty")
        asset_ids = {asset.asset_id for asset in self.assets}
        if len(asset_ids) != len(self.assets):
            raise ValueError("visual report cannot contain duplicate physical assets")
        if any(artifact.asset_id not in asset_ids for artifact in self.artifacts):
            raise ValueError("visual artifact must reference a report asset")
        if any(evidence.first_id not in asset_ids or evidence.second_id not in asset_ids for evidence in self.evidence):
            raise ValueError("visual evidence must reference report assets")
        if self.kind is VisualReportKind.QUERY and self.reference_asset_id not in asset_ids:
            raise ValueError("visual query report requires its reference asset")
        if self.kind is VisualReportKind.SCAN and self.reference_asset_id:
            raise ValueError("visual scan report cannot have a query reference")

    @property
    def allows_destructive_actions(self):
        return False

    def to_dict(self):
        return {
            "schema": VISUAL_REPORT_SCHEMA,
            "schema_version": VISUAL_REPORT_SCHEMA_VERSION,
            "report_id": self.report_id,
            "report_kind": self.kind.value,
            "created_at_ns": self.created_at_ns,
            "roots": list(self.roots),
            "reference_asset_id": self.reference_asset_id or None,
            "config": self.config.to_dict(),
            "assets": [asset.to_dict() for asset in sorted(self.assets, key=lambda item: item.asset_id)],
            "artifacts": [
                artifact.to_report_dict() for artifact in sorted(self.artifacts, key=lambda item: item.asset_id)
            ],
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.evidence_id)],
            "candidate_stats": self.candidate_stats.to_dict(),
            "scan_receipt": _receipt_to_dict(self.scan_receipt),
            "safety": {
                "source_read_only": True,
                "dry_run": True,
                "verified_exact_evidence": False,
                "destructive_actions_allowed": False,
                "allowed_relations": [
                    VisualRelation.SIMILAR.value,
                    VisualRelation.TRANSFORMED.value,
                    VisualRelation.CROP_CANDIDATE.value,
                    VisualRelation.RELATED.value,
                ],
            },
        }

    def to_json(self, indent=None):
        if indent is None:
            return _canonical_json_bytes(self.to_dict()).decode("utf-8")
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )


class _RunState:
    def __init__(self, started_at_ns, max_seconds=0):
        self.started_at_ns = started_at_ns
        self.deadline_ns = started_at_ns + int(max_seconds * 1_000_000_000) if max_seconds else 0
        self.issues = []
        self.skipped = 0
        self.failed = 0
        self.resource_limited = False
        self.hard_stop = False
        self.fatal = False
        self.cancelled = False

    def add_skip(self, code, message, path=""):
        self.skipped += 1
        self.issues.append(ScanIssue(code, message, str(path)))

    def add_failure(self, code, message, path=""):
        self.failed += 1
        self.issues.append(ScanIssue(code, message, str(path)))

    def add_resource_limit(self, message, path="", *, stop=True):
        self.resource_limited = True
        self.hard_stop = self.hard_stop or stop
        self.add_failure("resource_limit", message, path)

    def receipt(self, discovered, analyzed):
        if self.cancelled:
            status = ScanStatus.CANCELLED
        elif self.fatal:
            status = ScanStatus.FAILED
        elif self.resource_limited:
            status = ScanStatus.RESOURCE_LIMIT
        elif analyzed == 0 and self.failed:
            status = ScanStatus.FAILED
        elif self.issues:
            status = ScanStatus.COMPLETE_WITH_SKIPS
        else:
            status = ScanStatus.COMPLETE
        accounting_floor = analyzed + self.skipped + self.failed
        return VisualScanReceipt(
            scan_id=str(uuid.uuid4()),
            status=status,
            discovered=max(discovered, accounting_floor),
            analyzed=analyzed,
            skipped=self.skipped,
            failed=self.failed,
            started_at_ns=self.started_at_ns,
            finished_at_ns=time.time_ns(),
            issues=tuple(self.issues),
        )


def _stop_requested(cancel_check, state):
    if state.cancelled or state.hard_stop or state.fatal:
        return True
    if state.deadline_ns and time.time_ns() >= state.deadline_ns:
        state.add_resource_limit("visual scan exceeded max_seconds")
        return True
    if cancel_check is None:
        return False
    try:
        requested = bool(cancel_check())
    except Exception as error:
        state.fatal = True
        state.add_failure(
            "cancel_check_failed",
            "visual cancellation check failed: {}".format(error),
        )
        return True
    if requested:
        state.cancelled = True
    return requested


def _cancel_requested(cancel_check, state):
    """Compatibility name retained for the Qt query integration."""

    return _stop_requested(cancel_check, state)


def _is_image(path):
    return path.suffix.lower().lstrip(".") in Photo.HANDLED_EXTS


def _snapshot_id(identity):
    file_id_kind, file_id = _normalize_file_id(identity.file_id)
    return _stable_id(
        "visual-asset",
        {
            "namespace": identity.namespace,
            "capability": identity.capability.value,
            "volume_id": identity.volume_id,
            "file_id_kind": file_id_kind,
            "file_id": file_id,
        },
    )


def _path_is_within(path, root):
    candidate = os.path.normcase(os.path.abspath(os.fspath(path)))
    boundary = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


class VisualService:
    """High-level visual scan/query API with fail-closed filesystem coverage."""

    def __init__(
        self,
        cache_path=None,
        *,
        max_decode_pixels=DEFAULT_MAX_DECODE_PIXELS,
        walker=walk_no_follow,
        identity_getter=get_file_identity,
        feature_decoder=decode_image_features,
    ):
        if max_decode_pixels <= 0:
            raise ValueError("max_decode_pixels must be positive")
        self.cache_path = cache_path
        self.max_decode_pixels = max_decode_pixels
        self._walker = walker
        self._identity_getter = identity_getter
        self._feature_decoder = feature_decoder

    def scan_roots(
        self,
        roots,
        *,
        config=None,
        cancel_check=None,
        directory_pruner=None,
        file_filter=None,
    ):
        config = config or VisualScanConfig()
        roots = self._normalize_roots(roots)
        started_at_ns = time.time_ns()
        state = _RunState(started_at_ns, config.max_seconds)
        snapshots, discovered = self._enumerate_roots(
            roots,
            state,
            config=config,
            cancel_check=cancel_check,
            directory_pruner=directory_pruner,
            file_filter=file_filter,
        )
        artifacts = ()
        evidence = ()
        stats = self._empty_stats(config)
        cache = None
        try:
            if not _stop_requested(cancel_check, state):
                self._validate_cache_location(roots, snapshots, state)
            if not _stop_requested(cancel_check, state):
                cache = SqliteCache(
                    self.cache_path or ":memory:",
                    input_roots=roots,
                    input_identities=snapshots,
                )
                cache.purge_outdated()
                artifacts = self._features_for_snapshots(
                    snapshots,
                    include_orientations=config.match_rotated,
                    state=state,
                    cache=cache,
                    cancel_check=cancel_check,
                )
                artifacts = self._stable_artifacts(
                    artifacts,
                    state,
                    cancel_check=cancel_check,
                )
            if cache is not None and not _stop_requested(cancel_check, state):
                evidence, stats = self._scan_evidence(
                    artifacts,
                    config,
                    cache,
                    state=state,
                    cancel_check=cancel_check,
                )
                artifacts, evidence = self._final_stability_filter(
                    artifacts,
                    evidence,
                    state,
                    cancel_check=cancel_check,
                )
        except MemoryError:
            state.add_resource_limit("not enough memory to finish the visual scan")
        except (OSError, sqlite3.DatabaseError) as error:
            state.fatal = True
            state.add_failure("visual_cache_failure", str(error), self.cache_path or "")
        finally:
            if cache is not None:
                cache.close()
        assets = tuple(artifact.asset for artifact in artifacts)
        receipt = state.receipt(discovered, len(artifacts))
        return VisualReport(
            report_id=str(uuid.uuid4()),
            kind=VisualReportKind.SCAN,
            roots=roots,
            config=config,
            assets=assets,
            artifacts=artifacts,
            evidence=evidence,
            candidate_stats=stats,
            scan_receipt=receipt,
            created_at_ns=time.time_ns(),
        )

    def query_reference(
        self,
        reference,
        *,
        roots=(),
        catalog_artifacts=(),
        config=None,
        cancel_check=None,
        directory_pruner=None,
        file_filter=None,
    ):
        config = config or VisualScanConfig()
        roots = self._normalize_roots(roots, allow_empty=True)
        started_at_ns = time.time_ns()
        state = _RunState(started_at_ns, config.max_seconds)
        parsed_catalog = self._collect_catalog_artifacts(
            catalog_artifacts,
            config,
            state,
            cancel_check,
        )
        if not roots and not parsed_catalog:
            if state.cancelled or state.resource_limited or state.fatal:
                raise VisualServiceError("visual query stopped before a search source was accepted")
            raise ValueError("visual query requires roots or catalog_artifacts")
        if is_within_reserved_internal_directory(reference) or is_reserved_internal_file(reference):
            raise UnsafeVisualSourceError(
                "reserved_internal_source",
                "reserved dupeGuru internal files cannot be visual query references",
                reference,
            )
        reference_snapshot = self._capture_snapshot(Path(reference), "")
        if _stop_requested(cancel_check, state):
            return self._partial_query_report(
                reference_snapshot,
                roots,
                config,
                state,
                discovered_targets=len(parsed_catalog),
            )
        root_snapshots, discovered_roots = self._enumerate_roots(
            roots,
            state,
            config=config,
            cancel_check=cancel_check,
            directory_pruner=directory_pruner,
            file_filter=file_filter,
        )
        target_snapshots_by_id = {artifact.asset_id: artifact.asset for artifact in parsed_catalog}
        target_snapshots_by_id.update({snapshot.asset_id: snapshot for snapshot in root_snapshots})
        target_snapshots_by_id.pop(reference_snapshot.asset_id, None)
        ordered_targets = tuple(sorted(target_snapshots_by_id.values(), key=lambda item: item.asset_id))
        discovered_targets = max(discovered_roots, len(ordered_targets))
        if len(ordered_targets) + 1 > config.max_images:
            state.add_resource_limit(
                "visual query exceeded max_images ({})".format(config.max_images),
                stop=False,
            )
            ordered_targets = ordered_targets[: max(0, config.max_images - 1)]
        target_snapshots = ordered_targets
        if _stop_requested(cancel_check, state):
            return self._partial_query_report(
                reference_snapshot,
                roots,
                config,
                state,
                discovered_targets=discovered_targets,
            )
        all_artifacts = ()
        evidence = ()
        stats = self._empty_stats(
            config,
            indexed_images=len(target_snapshots),
            possible_pairs=len(target_snapshots),
        )
        cache = None
        try:
            self._validate_cache_location(
                roots,
                (reference_snapshot,) + target_snapshots,
                state,
            )
            if not _stop_requested(cancel_check, state):
                cache = SqliteCache(
                    self.cache_path or ":memory:",
                    input_roots=roots,
                    input_identities=(reference_snapshot,) + target_snapshots,
                )
                cache.purge_outdated()
                targets = self._features_for_snapshots(
                    target_snapshots,
                    include_orientations=False,
                    state=state,
                    cache=cache,
                    cancel_check=cancel_check,
                )
                reference_artifacts = self._features_for_snapshots(
                    (reference_snapshot,),
                    include_orientations=config.match_rotated,
                    state=state,
                    cache=cache,
                    cancel_check=cancel_check,
                )
                if reference_artifacts:
                    reference_artifact = reference_artifacts[0]
                    targets = self._stable_artifacts(
                        targets,
                        state,
                        cancel_check=cancel_check,
                    )
                    if not _stop_requested(cancel_check, state):
                        evidence, stats = self._query_evidence(
                            reference_artifact,
                            targets,
                            config,
                            cache,
                            state=state,
                            cancel_check=cancel_check,
                        )
                    all_artifacts = (reference_artifact,) + targets
                else:
                    all_artifacts = targets
                all_artifacts, evidence = self._final_stability_filter(
                    all_artifacts,
                    evidence,
                    state,
                    cancel_check=cancel_check,
                )
        except MemoryError:
            state.add_resource_limit("not enough memory to finish the visual query")
        except (OSError, sqlite3.DatabaseError) as error:
            state.fatal = True
            state.add_failure("visual_cache_failure", str(error), self.cache_path or "")
        finally:
            if cache is not None:
                cache.close()

        stable_ids = {artifact.asset_id for artifact in all_artifacts}
        reference_id = reference_snapshot.asset_id if reference_snapshot.asset_id in stable_ids else ""
        assets = tuple(artifact.asset for artifact in all_artifacts)
        receipt = state.receipt(
            1 + discovered_targets,
            len(all_artifacts),
        )
        if not reference_id:
            # ``VisualReport`` requires a live reference.  Return a failed report with the original
            # reference snapshot but no artifact/evidence so the failure remains serializable.
            assets = (reference_snapshot,) + tuple(
                artifact.asset for artifact in all_artifacts if artifact.asset_id != reference_snapshot.asset_id
            )
        return VisualReport(
            report_id=str(uuid.uuid4()),
            kind=VisualReportKind.QUERY,
            roots=roots,
            config=config,
            assets=assets,
            artifacts=all_artifacts,
            evidence=evidence if reference_id else (),
            candidate_stats=stats,
            scan_receipt=receipt,
            reference_asset_id=reference_id or reference_snapshot.asset_id,
            created_at_ns=time.time_ns(),
        )

    @staticmethod
    def _partial_query_report(
        reference_snapshot,
        roots,
        config,
        state,
        *,
        artifacts=(),
        discovered_targets,
    ):
        artifacts_by_id = {
            artifact.asset_id: artifact for artifact in artifacts if artifact.asset_id != reference_snapshot.asset_id
        }
        artifacts = tuple(sorted(artifacts_by_id.values(), key=lambda item: item.asset_id))
        assets = (reference_snapshot,) + tuple(artifact.asset for artifact in artifacts)
        receipt = state.receipt(
            1 + discovered_targets,
            len(artifacts),
        )
        return VisualReport(
            report_id=str(uuid.uuid4()),
            kind=VisualReportKind.QUERY,
            roots=roots,
            config=config,
            assets=assets,
            artifacts=artifacts,
            evidence=(),
            candidate_stats=VisualCandidateStats(
                indexed_images=len(artifacts),
                possible_pairs=len(artifacts),
                candidate_pairs=0,
                refined_pairs=0,
                similar_count=0,
                transformed_count=0,
                crop_candidate_count=0,
                related_count=0,
                phash_radius=config.phash_radius,
            ),
            scan_receipt=receipt,
            reference_asset_id=reference_snapshot.asset_id,
            created_at_ns=time.time_ns(),
        )

    _cancelled_query_report = _partial_query_report

    def _collect_catalog_artifacts(
        self,
        values,
        config,
        state,
        cancel_check,
    ):
        """Consume catalog data incrementally under the same image budget as filesystem scans."""

        result = {}
        for value in values:
            if _stop_requested(cancel_check, state):
                break
            artifact = self._parse_artifact(value)
            if artifact.asset_id not in result and len(result) >= config.max_images:
                state.add_resource_limit(
                    "visual catalog input exceeded max_images ({})".format(config.max_images),
                    stop=False,
                )
                break
            result.setdefault(artifact.asset_id, artifact)
        return tuple(sorted(result.values(), key=lambda item: item.asset_id))

    @staticmethod
    def _parse_artifact(value):
        if isinstance(value, VisualFeatureArtifact):
            return value
        if isinstance(value, Mapping):
            return VisualFeatureArtifact.from_dict(value)
        if isinstance(value, (str, bytes)):
            return VisualFeatureArtifact.from_json(value)
        raise TypeError("catalog_artifacts must contain visual feature artifacts")

    @staticmethod
    def _normalize_roots(roots, allow_empty=False):
        if isinstance(roots, (str, os.PathLike)):
            roots = (roots,)
        result = tuple(sorted({str(Path(os.path.abspath(os.fspath(root)))) for root in roots}))
        if not result and not allow_empty:
            raise ValueError("visual scan requires at least one root")
        return result

    def _enumerate_roots(
        self,
        roots,
        state,
        *,
        config,
        cancel_check=None,
        directory_pruner=None,
        file_filter=None,
    ):
        snapshots_by_id = {}
        discovered = 0
        for root_text in roots:
            root = Path(root_text)
            if is_within_reserved_internal_directory(root):
                discovered += 1
                state.add_failure(
                    "reserved_internal_root",
                    "reserved dupeGuru internal directories cannot be visual roots",
                    root,
                )
                continue

            def combined_pruner(path):
                if is_within_reserved_internal_directory(path):
                    return "reserved dupeGuru internal directory"
                if directory_pruner is None:
                    return None
                try:
                    return directory_pruner(path)
                except Exception as error:
                    state.fatal = True
                    state.add_failure(
                        "directory_filter_failed",
                        str(error) or type(error).__name__,
                        path,
                    )
                    return "directory policy failed closed"

            walk_options = {
                "allowed_root": root,
                "cross_mounts": False,
                "directory_pruner": combined_pruner,
            }
            for event in self._walk_events(root, walk_options, state):
                if _stop_requested(cancel_check, state):
                    return (
                        tuple(
                            sorted(
                                snapshots_by_id.values(),
                                key=lambda item: item.asset_id,
                            )
                        ),
                        discovered,
                    )
                if event.kind is WalkEventKind.FILE:
                    if not _path_is_within(event.path, root):
                        discovered += 1
                        state.add_failure(
                            "walk_event_outside_root",
                            "visual walker emitted a file outside its bounded root",
                            event.path,
                        )
                        continue
                    if not _is_image(event.path):
                        continue
                    if is_within_reserved_internal_directory(event.path) or is_reserved_internal_file(event.path):
                        continue
                    if file_filter is not None:
                        try:
                            included = bool(file_filter(event.path))
                        except Exception as error:
                            discovered += 1
                            state.add_failure(
                                "source_filter_failed",
                                str(error) or type(error).__name__,
                                event.path,
                            )
                            continue
                        if not included:
                            continue
                    try:
                        snapshot = self._capture_snapshot(event.path, root_text, event.identity)
                    except UnsafeVisualSourceError as error:
                        discovered += 1
                        state.add_failure(error.code, str(error), error.path)
                        continue
                    if snapshot.asset_id in snapshots_by_id:
                        continue
                    if len(snapshots_by_id) >= config.max_images:
                        discovered += 1
                        state.add_resource_limit(
                            "visual scan exceeded max_images ({})".format(config.max_images),
                            event.path,
                            stop=False,
                        )
                        return (
                            tuple(
                                sorted(
                                    snapshots_by_id.values(),
                                    key=lambda item: item.asset_id,
                                )
                            ),
                            discovered,
                        )
                    snapshots_by_id[snapshot.asset_id] = snapshot
                    discovered += 1
                elif event.kind is WalkEventKind.ERROR:
                    discovered += 1
                    message = event.error.message if event.error is not None else event.detail or "walk error"
                    state.add_failure("walk_error", message, event.path)
                elif event.kind in {
                    WalkEventKind.SYMLINK_SKIPPED,
                    WalkEventKind.REPARSE_POINT_SKIPPED,
                    WalkEventKind.MOUNT_SKIPPED,
                    WalkEventKind.CYCLE_SKIPPED,
                    WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
                    WalkEventKind.SPECIAL_FILE_SKIPPED,
                    WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
                }:
                    discovered += 1
                    state.add_skip(
                        "walk_{}".format(event.kind.value.replace("-", "_")),
                        event.detail or event.kind.value,
                        event.path,
                    )
        return tuple(sorted(snapshots_by_id.values(), key=lambda item: item.asset_id)), discovered

    def _walk_events(self, root, options, state):
        try:
            yield from self._walker(root, **options)
        except Exception as error:
            state.fatal = True
            state.add_failure(
                "walk_failed",
                str(error) or type(error).__name__,
                root,
            )

    def _validate_cache_location(self, roots, snapshots, state):
        """Reject cache placement that could mutate a source or a selected tree."""

        try:
            SqliteCache.validate_location(
                self.cache_path or ":memory:",
                input_roots=roots,
                input_identities=snapshots,
            )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
            state.fatal = True
            state.add_failure(
                "cache_validation_failed",
                str(error),
                self.cache_path or "",
            )

    def _capture_snapshot(self, path, root, expected_identity=None):
        path = Path(os.path.abspath(os.fspath(path)))
        try:
            first_stat = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise UnsafeVisualSourceError("source_stat_failed", str(error), path) from error
        if stat.S_ISLNK(first_stat.st_mode):
            raise UnsafeVisualSourceError("source_symlink_rejected", "symbolic links are not visual inputs", path)
        if is_reparse_point(first_stat):
            raise UnsafeVisualSourceError(
                "source_reparse_rejected",
                "junctions and reparse points are not visual inputs",
                path,
            )
        if not stat.S_ISREG(first_stat.st_mode):
            raise UnsafeVisualSourceError("source_not_regular", "visual input is not a regular file", path)
        try:
            identity = self._identity_getter(path, follow_symlinks=False, stat_result=first_stat)
        except FileIdentityError as error:
            raise UnsafeVisualSourceError("source_identity_failed", str(error), path) from error
        if identity.confidence < IdentityConfidence.MEDIUM:
            raise UnsafeVisualSourceError(
                "source_identity_weak",
                "visual input lacks a stable physical identity",
                path,
            )
        if expected_identity is not None:
            comparison = same_physical_file(expected_identity, identity)
            if comparison.verdict is not IdentityVerdict.SAME:
                raise UnsafeVisualSourceError(
                    "source_identity_changed",
                    "visual input identity changed after enumeration",
                    path,
                )
        try:
            first_generation = get_file_generation_token(
                path,
                follow_symlinks=False,
                stat_result=first_stat,
                expected_identity=identity,
            )
        except FileGenerationError as error:
            raise UnsafeVisualSourceError(
                "source_generation_failed",
                str(error),
                path,
            ) from error
        try:
            second_stat = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise UnsafeVisualSourceError("source_restat_failed", str(error), path) from error
        try:
            second_generation = get_file_generation_token(
                path,
                follow_symlinks=False,
                stat_result=second_stat,
                expected_identity=identity,
            )
        except FileGenerationError as error:
            raise UnsafeVisualSourceError(
                "source_generation_failed",
                str(error),
                path,
            ) from error
        if (
            int(first_stat.st_size),
            int(first_stat.st_mtime_ns),
            first_generation,
        ) != (
            int(second_stat.st_size),
            int(second_stat.st_mtime_ns),
            second_generation,
        ):
            raise UnsafeVisualSourceError(
                "source_generation_changed",
                "visual input metadata changed while it was identified",
                path,
            )
        file_id_kind, file_id = _normalize_file_id(identity.file_id)
        return VisualAssetSnapshot(
            asset_id=_snapshot_id(identity),
            path=str(path),
            root=str(root),
            size=int(first_stat.st_size),
            mtime_ns=int(first_stat.st_mtime_ns),
            generation_token=first_generation.encoded.hex(),
            identity_namespace=identity.namespace,
            identity_capability=identity.capability.value,
            identity_confidence=int(identity.confidence),
            volume_id=int(identity.volume_id),
            file_id_kind=file_id_kind,
            file_id=file_id,
        )

    def _validate_snapshot(self, snapshot):
        current = self._capture_snapshot(snapshot.path, snapshot.root, snapshot.identity)
        if current.asset_id != snapshot.asset_id or current.generation != snapshot.generation:
            raise UnsafeVisualSourceError(
                "source_generation_changed",
                "visual input changed after its snapshot was captured",
                snapshot.path,
            )

    def _features_for_snapshots(
        self,
        snapshots,
        include_orientations,
        state,
        cache,
        cancel_check=None,
    ):
        if not snapshots:
            return ()
        artifacts = []
        for snapshot in snapshots:
            if _stop_requested(cancel_check, state):
                break
            try:
                self._validate_snapshot(snapshot)
                try:
                    features = cache.get_features(snapshot.path)
                    if features.feature_version != FEATURE_VERSION:
                        raise KeyError(snapshot.path)
                    if include_orientations and features.orientation_count != 8:
                        raise KeyError(snapshot.path)
                except (KeyError, ValueError):
                    decoded = self._feature_decoder(
                        snapshot.path,
                        block_count_per_side=BLOCK_COUNT_PER_SIDE,
                        include_orientations=include_orientations,
                        max_pixels=self.max_decode_pixels,
                    )
                    if _stop_requested(cancel_check, state):
                        break
                    expected_orientations = 8 if include_orientations else 1
                    if decoded.feature_version != FEATURE_VERSION or decoded.orientation_count != expected_orientations:
                        raise ImageDecodeError("decoder returned incompatible visual features")
                    self._validate_snapshot(snapshot)
                    expected_binding = capture_source_binding(snapshot.path)
                    if (
                        expected_binding.size != snapshot.size
                        or expected_binding.mtime_ns != snapshot.mtime_ns
                        or expected_binding.generation_token != bytes.fromhex(snapshot.generation_token)
                        or same_physical_file(
                            expected_binding.identity,
                            snapshot.identity,
                        ).verdict
                        is not IdentityVerdict.SAME
                    ):
                        raise UnsafeVisualSourceError(
                            "source_generation_changed",
                            "visual input changed before its features could be cached",
                            snapshot.path,
                        )
                    cache.put_features(
                        snapshot.path,
                        decoded,
                        expected_binding=expected_binding,
                    )
                    features = cache.get_features(snapshot.path)
                self._validate_snapshot(snapshot)
                artifacts.append(
                    VisualFeatureArtifact(
                        asset=snapshot,
                        dimensions=features.dimensions,
                        frame_count=features.frame_count,
                        phashes=tuple(features.phashes),
                        dhashes=tuple(features.dhashes),
                        color_histogram=tuple(features.color_histogram),
                        tile_fingerprints=tuple(features.tile_fingerprints),
                        quality=features.quality,
                        thumbnail_key=features.thumbnail_key,
                        cache_record_id=features.rowid,
                    )
                )
                del features
            except ImageResourceLimitError as error:
                state.add_resource_limit(str(error), snapshot.path)
                break
            except ImageFeatureError as error:
                state.add_failure(error.code, str(error), snapshot.path)
            except UnsafeVisualSourceError as error:
                state.add_failure(error.code, str(error), error.path)
            except MemoryError:
                state.add_resource_limit(
                    "not enough memory to analyze visual inputs",
                    snapshot.path,
                )
                break
            except (OSError, sqlite3.DatabaseError, ValueError) as error:
                state.add_failure("visual_cache_failure", str(error), snapshot.path)
        return tuple(artifacts)

    def _stable_artifacts(self, artifacts, state, cancel_check=None):
        result = []
        for artifact in artifacts:
            if _cancel_requested(cancel_check, state):
                break
            try:
                self._validate_snapshot(artifact.asset)
                result.append(artifact)
            except UnsafeVisualSourceError as error:
                state.add_failure(error.code, str(error), error.path)
        return tuple(sorted(result, key=lambda item: item.asset_id))

    def _final_stability_filter(
        self,
        artifacts,
        evidence,
        state,
        cancel_check=None,
    ):
        stable = self._stable_artifacts(
            artifacts,
            state,
            cancel_check=cancel_check,
        )
        stable_ids = {artifact.asset_id for artifact in stable}
        evidence = tuple(item for item in evidence if item.first_id in stable_ids and item.second_id in stable_ids)
        return stable, evidence

    @staticmethod
    def _dimensions_compatible(first, second, config):
        if config.match_scaled or first == second:
            return True
        return bool(config.match_rotated and (first[1], first[0]) == second)

    @staticmethod
    def _histogram_distance(first, second):
        return sum(abs(left - right) for left, right in zip(first, second)) / (2 * COLOR_HISTOGRAM_SAMPLE_COUNT)

    @staticmethod
    def _fingerprint_entries(artifact, config, *, query):
        orientation_count = artifact.orientation_count if query and config.match_rotated else 1
        result = [
            _FingerprintEntry(
                artifact.asset_id,
                "whole",
                orientation,
                artifact.phashes[orientation],
                artifact.dhashes[orientation],
                (0, 0, TILE_BOX_SCALE, TILE_BOX_SCALE),
            )
            for orientation in range(orientation_count)
        ]
        if config.match_crops:
            result.extend(
                _FingerprintEntry(
                    artifact.asset_id,
                    item.kind,
                    0,
                    item.phash,
                    item.dhash,
                    item.box,
                )
                for item in artifact.tile_fingerprints
            )
        return tuple(result)

    @classmethod
    def _candidate_index(cls, artifacts, config, state, cancel_check):
        index = MultiIndexHamming(
            bit_width=PHASH_BIT_WIDTH,
            max_distance=config.phash_radius,
        )
        entries = {}
        for artifact in artifacts:
            if state is not None and _stop_requested(cancel_check, state):
                break
            for number, entry in enumerate(cls._fingerprint_entries(artifact, config, query=False)):
                entry_id = "{}|{}|{}".format(
                    artifact.asset_id,
                    entry.kind,
                    number,
                )
                entries[entry_id] = entry
                index.add(entry_id, entry.phash)
        return index, entries

    @classmethod
    def _candidate_hits(
        cls,
        first,
        index,
        indexed_entries,
        by_id,
        config,
        state,
        cancel_check,
        *,
        query_budget,
        max_results,
        canonical_only,
    ):
        if max_results <= 0:
            return (), True, False
        if not isinstance(query_budget, CandidateQueryBudget):
            raise TypeError("query_budget must be a CandidateQueryBudget")
        best_by_asset = {}
        worst_first = []
        truncated = False
        budget_exhausted = False

        def clean_heap():
            while worst_first:
                retained = best_by_asset.get(worst_first[0].work.second_id)
                if retained == worst_first[0].work:
                    break
                heapq.heappop(worst_first)

        def retain(work):
            nonlocal truncated, worst_first
            previous = best_by_asset.get(work.second_id)
            if previous is not None:
                if _candidate_sort_key(work) >= _candidate_sort_key(previous):
                    return
                best_by_asset[work.second_id] = work
                heapq.heappush(worst_first, _WorstCandidate(work))
            elif len(best_by_asset) < max_results:
                best_by_asset[work.second_id] = work
                heapq.heappush(worst_first, _WorstCandidate(work))
            else:
                truncated = True
                clean_heap()
                worst = worst_first[0].work
                if _candidate_sort_key(work) >= _candidate_sort_key(worst):
                    return
                heapq.heappop(worst_first)
                del best_by_asset[worst.second_id]
                best_by_asset[work.second_id] = work
                heapq.heappush(worst_first, _WorstCandidate(work))
            if len(worst_first) > max(64, len(best_by_asset) * 2 + 16):
                worst_first = [_WorstCandidate(candidate) for candidate in best_by_asset.values()]
                heapq.heapify(worst_first)

        def query_stop_requested():
            if state is None:
                return bool(cancel_check and cancel_check())
            return _stop_requested(cancel_check, state)

        try:
            for query_entry in cls._fingerprint_entries(first, config, query=True):
                if query_stop_requested():
                    break
                for candidate in index.iter_query(
                    query_entry.phash,
                    max_distance=config.phash_radius,
                    budget=query_budget,
                    cancel_check=query_stop_requested,
                ):
                    indexed_entry = indexed_entries[candidate.asset_id]
                    second_id = indexed_entry.asset_id
                    if second_id == first.asset_id or (canonical_only and second_id <= first.asset_id):
                        continue
                    second = by_id[second_id]
                    is_crop = query_entry.kind != "whole" or indexed_entry.kind != "whole"
                    if not is_crop and not cls._dimensions_compatible(
                        first.dimensions,
                        second.dimensions,
                        config,
                    ):
                        continue
                    dhash_distance = hamming_distance(
                        query_entry.dhash,
                        indexed_entry.dhash,
                        DHASH_BIT_WIDTH,
                    )
                    histogram_distance = cls._histogram_distance(
                        first.color_histogram,
                        second.color_histogram,
                    )
                    # pHash is the no-false-negative candidate gate.  dHash and the color
                    # histogram are deliberately a conservative conjunctive reject so a
                    # brightness shift or crop does not disappear merely because one cheap
                    # descriptor changed.
                    if dhash_distance > config.dhash_distance and histogram_distance > config.color_histogram_distance:
                        continue
                    work = _CandidateWork(
                        first_id=first.asset_id,
                        second_id=second_id,
                        phash_distance=candidate.distance,
                        dhash_distance=dhash_distance,
                        color_histogram_distance=histogram_distance,
                        first_kind=query_entry.kind,
                        second_kind=indexed_entry.kind,
                        first_box=query_entry.box,
                        second_box=indexed_entry.box,
                        phash_orientation=query_entry.orientation,
                    )
                    previous = best_by_asset.get(second_id)
                    if previous is None or _candidate_sort_key(work) < _candidate_sort_key(previous):
                        retain(work)
        except CandidateQueryLimitError as error:
            budget_exhausted = True
            if state is not None:
                message = (
                    "visual candidate examination reached its budget after examining "
                    "{} index entries (limit {}; max_candidate_pairs {})"
                ).format(
                    error.examined,
                    error.limit,
                    config.max_candidate_pairs,
                )
                if state.resource_limited:
                    # One resource-limit reason may already account for an incomplete
                    # input set. Preserve this independent reason without counting it
                    # as another failed file in the receipt's item accounting.
                    state.issues.append(ScanIssue("resource_limit", message, ""))
                else:
                    state.add_resource_limit(message, stop=False)
        except CandidateQueryCancelled:
            # The state-aware callback records cancellation, deadline, or callback
            # failure before the index turns the stop request into this exception.
            if state is None:
                raise
        return (
            tuple(sorted(best_by_asset.values(), key=_candidate_sort_key)),
            truncated,
            budget_exhausted,
        )

    @staticmethod
    def _block_similarity(first_blocks, second_blocks, match_rotated):
        count = 8 if match_rotated else 1
        best_similarity = 0
        best_orientation = 0
        for orientation in range(count):
            try:
                difference = avgdiff(
                    first_blocks[orientation],
                    second_blocks[0],
                    768,
                    MIN_ITERATIONS,
                )
                similarity = max(0, min(100, 100 - difference))
            except (DifferentBlockCountError, NoBlocksError):
                similarity = 0
            if similarity > best_similarity:
                best_similarity = similarity
                best_orientation = orientation
        return best_similarity, best_orientation

    def _make_evidence(
        self,
        first,
        second,
        first_blocks,
        second_blocks,
        candidate,
        config,
    ):
        block_similarity, block_orientation = self._block_similarity(
            first_blocks,
            second_blocks,
            config.match_rotated,
        )
        is_crop = candidate.first_kind != "whole" or candidate.second_kind != "whole"
        is_transformed = (
            candidate.phash_orientation != 0 or block_orientation != 0 or first.dimensions != second.dimensions
        )
        if is_crop:
            relation = VisualRelation.CROP_CANDIDATE
        elif is_transformed:
            relation = VisualRelation.TRANSFORMED
        elif block_similarity >= config.similarity_threshold:
            relation = VisualRelation.SIMILAR
        else:
            relation = VisualRelation.RELATED
        if relation is VisualRelation.TRANSFORMED:
            transformation_kind = (
                "orientation" if candidate.phash_orientation != 0 or block_orientation != 0 else "scaled_or_resized"
            )
        else:
            transformation_kind = "none"
        if relation is VisualRelation.RELATED and not config.include_related:
            return None
        fingerprint_score = max(
            0.0,
            1
            - (
                candidate.phash_distance / PHASH_BIT_WIDTH * 0.5
                + candidate.dhash_distance / DHASH_BIT_WIDTH * 0.35
                + candidate.color_histogram_distance * 0.15
            ),
        )
        return VisualEvidence(
            first_id=first.asset_id,
            second_id=second.asset_id,
            relation=relation,
            score=round(max(block_similarity / 100, fingerprint_score), 6),
            block_similarity=block_similarity,
            phash_distance=candidate.phash_distance,
            dhash_distance=candidate.dhash_distance,
            color_histogram_distance=candidate.color_histogram_distance,
            first_fingerprint_kind=candidate.first_kind,
            second_fingerprint_kind=candidate.second_kind,
            first_fingerprint_box=candidate.first_box,
            second_fingerprint_box=candidate.second_box,
            crop_verification=("bounded_fingerprint_candidate" if is_crop else "not_applicable"),
            transformation_kind=transformation_kind,
            phash_orientation=candidate.phash_orientation,
            block_orientation=block_orientation,
            similarity_threshold=config.similarity_threshold,
            phash_radius=config.phash_radius,
        )

    def _refine_batch(
        self,
        work,
        by_id,
        cache,
        config,
        state,
        cancel_check,
        evidence,
    ):
        row_ids = sorted(
            {by_id[asset_id].cache_record_id for item in work for asset_id in (item.first_id, item.second_id)}
        )
        try:
            blocks_by_id = dict(cache.get_multiple(row_ids))
        except (OSError, sqlite3.DatabaseError, ValueError) as error:
            state.fatal = True
            state.add_failure("visual_cache_failure", str(error))
            return 0
        refined = 0
        for candidate in work:
            if _stop_requested(cancel_check, state):
                break
            first = by_id[candidate.first_id]
            second = by_id[candidate.second_id]
            first_blocks = blocks_by_id.get(first.cache_record_id)
            second_blocks = blocks_by_id.get(second.cache_record_id)
            if first_blocks is None or second_blocks is None:
                state.fatal = True
                state.add_failure(
                    "visual_cache_record_missing",
                    "visual refinement cache record disappeared",
                )
                break
            item = self._make_evidence(
                first,
                second,
                first_blocks,
                second_blocks,
                candidate,
                config,
            )
            refined += 1
            if item is None:
                continue
            if len(evidence) >= config.max_matches:
                state.add_resource_limit("visual evidence exceeded max_matches ({})".format(config.max_matches))
                break
            evidence.append(item)
        return refined

    def _scan_evidence(
        self,
        artifacts,
        config,
        cache,
        *,
        state,
        cancel_check=None,
    ):
        possible_pairs = len(artifacts) * (len(artifacts) - 1) // 2
        by_id = {artifact.asset_id: artifact for artifact in artifacts}
        index, indexed_entries = self._candidate_index(
            artifacts,
            config,
            state,
            cancel_check,
        )
        query_budget = CandidateQueryBudget(config.max_candidate_pairs + len(indexed_entries))
        evidence = []
        work = []
        candidate_pairs = 0
        refined_pairs = 0
        candidate_limit_reached = False
        for first in artifacts:
            if _stop_requested(cancel_check, state):
                break
            candidates, lookup_limit_reached, query_budget_exhausted = self._candidate_hits(
                first,
                index,
                indexed_entries,
                by_id,
                config,
                state,
                cancel_check,
                query_budget=query_budget,
                max_results=config.max_candidate_pairs - candidate_pairs,
                canonical_only=True,
            )
            for candidate in candidates:
                if _stop_requested(cancel_check, state):
                    break
                candidate_pairs += 1
                work.append(candidate)
                if candidate_pairs >= config.max_candidate_pairs:
                    if not query_budget_exhausted:
                        state.add_resource_limit(
                            "visual candidates reached max_candidate_pairs ({})".format(config.max_candidate_pairs),
                            stop=False,
                        )
                    candidate_limit_reached = True
                    break
                if len(work) >= MAX_REFINEMENT_BATCH_SIZE:
                    refined_pairs += self._refine_batch(
                        work,
                        by_id,
                        cache,
                        config,
                        state,
                        cancel_check,
                        evidence,
                    )
                    work = []
            if query_budget_exhausted:
                candidate_limit_reached = True
            elif (
                lookup_limit_reached
                and not candidate_limit_reached
                and not state.cancelled
                and not state.hard_stop
                and not state.fatal
            ):
                state.add_resource_limit(
                    "visual candidate lookup exceeded max_candidate_pairs ({})".format(config.max_candidate_pairs),
                    stop=False,
                )
                candidate_limit_reached = True
            if candidate_limit_reached:
                break
        if work and not state.cancelled and not state.fatal:
            refined_pairs += self._refine_batch(
                work,
                by_id,
                cache,
                config,
                state,
                cancel_check,
                evidence,
            )
        evidence = tuple(sorted(evidence, key=lambda item: item.evidence_id))
        return evidence, self._stats(
            len(artifacts),
            possible_pairs,
            candidate_pairs,
            refined_pairs,
            evidence,
            config,
        )

    def _query_evidence(
        self,
        reference,
        targets,
        config,
        cache,
        *,
        state=None,
        cancel_check=None,
    ):
        by_id = {artifact.asset_id: artifact for artifact in targets}
        index, indexed_entries = self._candidate_index(
            targets,
            config,
            state,
            cancel_check,
        )
        query_budget = CandidateQueryBudget(config.max_candidate_pairs + len(indexed_entries))
        candidates, lookup_limit_reached, query_budget_exhausted = self._candidate_hits(
            reference,
            index,
            indexed_entries,
            by_id,
            config,
            state,
            cancel_check,
            query_budget=query_budget,
            max_results=config.max_candidate_pairs,
            canonical_only=False,
        )
        evidence = []
        work = []
        candidate_pairs = 0
        refined_pairs = 0
        for candidate in candidates:
            if state is not None and _stop_requested(cancel_check, state):
                break
            candidate_pairs += 1
            work.append(candidate)
            if candidate_pairs >= config.max_candidate_pairs:
                if not query_budget_exhausted:
                    state.add_resource_limit(
                        "visual candidates reached max_candidate_pairs ({})".format(config.max_candidate_pairs),
                        stop=False,
                    )
                break
            if len(work) >= MAX_REFINEMENT_BATCH_SIZE:
                batch_assets = dict(by_id)
                batch_assets[reference.asset_id] = reference
                refined_pairs += self._refine_batch(
                    work,
                    batch_assets,
                    cache,
                    config,
                    state,
                    cancel_check,
                    evidence,
                )
                work = []
        if (
            lookup_limit_reached
            and not query_budget_exhausted
            and candidate_pairs < config.max_candidate_pairs
            and not state.cancelled
            and not state.hard_stop
            and not state.fatal
        ):
            state.add_resource_limit(
                "visual candidate lookup exceeded max_candidate_pairs ({})".format(config.max_candidate_pairs),
                stop=False,
            )
        if work and not state.cancelled and not state.fatal:
            batch_assets = dict(by_id)
            batch_assets[reference.asset_id] = reference
            refined_pairs += self._refine_batch(
                work,
                batch_assets,
                cache,
                config,
                state,
                cancel_check,
                evidence,
            )
        evidence = tuple(sorted(evidence, key=lambda item: item.evidence_id))
        return evidence, self._stats(
            len(targets),
            len(targets),
            candidate_pairs,
            refined_pairs,
            evidence,
            config,
        )

    @staticmethod
    def _stats(indexed, possible, candidates, refined, evidence, config):
        similar = sum(item.relation is VisualRelation.SIMILAR for item in evidence)
        transformed = sum(item.relation is VisualRelation.TRANSFORMED for item in evidence)
        crop_candidates = sum(item.relation is VisualRelation.CROP_CANDIDATE for item in evidence)
        related = sum(item.relation is VisualRelation.RELATED for item in evidence)
        return VisualCandidateStats(
            indexed_images=indexed,
            possible_pairs=possible,
            candidate_pairs=candidates,
            refined_pairs=refined,
            similar_count=similar,
            transformed_count=transformed,
            crop_candidate_count=crop_candidates,
            related_count=related,
            phash_radius=config.phash_radius,
        )

    @staticmethod
    def _empty_stats(config, indexed_images=0, possible_pairs=None):
        if possible_pairs is None:
            possible_pairs = indexed_images * (indexed_images - 1) // 2
        return VisualCandidateStats(
            indexed_images=indexed_images,
            possible_pairs=possible_pairs,
            candidate_pairs=0,
            refined_pairs=0,
            similar_count=0,
            transformed_count=0,
            crop_candidate_count=0,
            related_count=0,
            phash_radius=config.phash_radius,
        )


__all__ = [
    "CATALOG_ARTIFACT_KIND",
    "CATALOG_VERIFICATION_LEVEL",
    "VISUAL_ALGORITHM",
    "VISUAL_ALGORITHM_VERSION",
    "VISUAL_ARTIFACT_SCHEMA",
    "VISUAL_ARTIFACT_SCHEMA_VERSION",
    "VISUAL_REPORT_SCHEMA",
    "VISUAL_REPORT_SCHEMA_VERSION",
    "UnsafeVisualSourceError",
    "VisualAssetSnapshot",
    "VisualCandidateStats",
    "VisualEvidence",
    "VisualFeatureArtifact",
    "VisualRelation",
    "VisualReport",
    "VisualReportKind",
    "VisualScanConfig",
    "VisualScanReceipt",
    "VisualService",
    "VisualServiceError",
]
