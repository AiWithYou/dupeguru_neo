# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Immutable data types used by the video similarity engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

ARTIFACT_SCHEMA_VERSION = 1
ANALYZER_VERSION = "video_similarity_v1"
MAX_ARTIFACT_FRAMES = 32
MAX_AUDIO_FINGERPRINT_WORDS = 16_384
MAX_ARTIFACT_ISSUES = 128
MAX_ARTIFACT_TOOL_VERSIONS = 16
MAX_FRAME_FINGERPRINT_BITS = 256
MAX_SOURCE_PATH_CHARACTERS = 32_768
MAX_ISSUE_CODE_CHARACTERS = 128
MAX_ISSUE_MESSAGE_CHARACTERS = 8_192
MAX_TOOL_TEXT_CHARACTERS = 4_096
MAX_ALGORITHM_CHARACTERS = 128


class AnalysisState(Enum):
    """Completeness of an analysis artifact.

    A non-complete state is intentionally part of the persisted artifact.  Callers must never
    mistake an interrupted or degraded analysis for a successful comparison.
    """

    COMPLETE = "complete"
    PARTIAL_MISSING_TOOL = "partial_missing_tool"
    PARTIAL_TIMEOUT = "partial_timeout"
    PARTIAL_CANCELLED = "partial_cancelled"
    PARTIAL_RESOURCE_LIMIT = "partial_resource_limit"
    PARTIAL_TOOL_ERROR = "partial_tool_error"
    FAILED = "failed"


@dataclass(frozen=True)
class AnalysisIssue:
    code: str
    message: str
    tool: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("analysis issue requires a code and message")
        if len(self.code) > MAX_ISSUE_CODE_CHARACTERS:
            raise ValueError("analysis issue code exceeds the supported length")
        if len(self.message) > MAX_ISSUE_MESSAGE_CHARACTERS:
            raise ValueError("analysis issue message exceeds the supported length")
        if self.tool is not None and len(self.tool) > MAX_TOOL_TEXT_CHARACTERS:
            raise ValueError("analysis issue tool exceeds the supported length")
        if "\0" in self.code or "\0" in self.message or (self.tool is not None and "\0" in self.tool):
            raise ValueError("analysis issue text must not contain NUL")

    def to_dict(self) -> Dict[str, object]:
        return {"code": self.code, "message": self.message, "tool": self.tool}


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        if not self.path or "\0" in self.path:
            raise ValueError("source path must be non-empty and contain no NUL")
        if len(self.path) > MAX_SOURCE_PATH_CHARACTERS:
            raise ValueError("source path exceeds the supported length")
        if self.size < 0 or self.mtime_ns < 0:
            raise ValueError("source size and mtime_ns must be non-negative")

    def to_dict(self) -> Dict[str, object]:
        return {"path": self.path, "size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float
    width: int
    height: int
    frame_rate: float
    video_codec: str
    pixel_format: str = ""
    audio_codec: str = ""
    audio_duration_seconds: Optional[float] = None
    bit_rate: Optional[int] = None
    container: str = ""

    def __post_init__(self) -> None:
        numeric = (self.duration_seconds, self.frame_rate)
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("duration and frame rate must be finite")
        if self.duration_seconds <= 0:
            raise ValueError("duration must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("video dimensions must be positive")
        if self.frame_rate <= 0:
            raise ValueError("frame rate must be positive")
        if not self.video_codec:
            raise ValueError("video codec must not be empty")
        if self.audio_duration_seconds is not None:
            if not math.isfinite(self.audio_duration_seconds) or self.audio_duration_seconds < 0:
                raise ValueError("audio duration must be finite and non-negative")
        if self.bit_rate is not None and self.bit_rate < 0:
            raise ValueError("bit rate must be non-negative")

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    def to_dict(self) -> Dict[str, object]:
        return {
            "duration_seconds": float(self.duration_seconds),
            "width": self.width,
            "height": self.height,
            "frame_rate": float(self.frame_rate),
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "audio_codec": self.audio_codec,
            "audio_duration_seconds": (
                float(self.audio_duration_seconds) if self.audio_duration_seconds is not None else None
            ),
            "bit_rate": self.bit_rate,
            "container": self.container,
        }


class CandidateKind(Enum):
    SAME_ENCODE_CANDIDATE = "same_encode_candidate"
    TRANSCODE_CANDIDATE = "transcode_candidate"
    TRIM_CANDIDATE = "trim_candidate"
    RELATED_CANDIDATE = "related_candidate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CandidateAssessment:
    kind: CandidateKind
    score: float
    reasons: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("candidate score must be between 0 and 1")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if not self.reasons:
            raise ValueError("candidate assessment requires at least one reason")

    @property
    def should_compare(self) -> bool:
        return self.kind is not CandidateKind.REJECTED


class FrameOrigin(Enum):
    NORMALIZED = "normalized"
    SCENE_CHANGE = "scene_change"


@dataclass(frozen=True)
class FrameRequest:
    timestamp_seconds: float
    normalized_position: float
    origin: FrameOrigin

    def __post_init__(self) -> None:
        if not math.isfinite(self.timestamp_seconds) or self.timestamp_seconds < 0:
            raise ValueError("frame timestamp must be finite and non-negative")
        if not math.isfinite(self.normalized_position) or not 0 <= self.normalized_position <= 1:
            raise ValueError("normalized frame position must be between 0 and 1")

    def to_dict(self) -> Dict[str, object]:
        return {
            "timestamp_seconds": float(self.timestamp_seconds),
            "normalized_position": float(self.normalized_position),
            "origin": self.origin.value,
        }


@dataclass(frozen=True)
class FramePlan:
    requests: Tuple[FrameRequest, ...]
    requested_normalized_count: int
    scene_threshold: float
    maximum_frames: int

    def __post_init__(self) -> None:
        requests = tuple(sorted(self.requests, key=lambda item: (item.timestamp_seconds, item.origin.value)))
        if not requests:
            raise ValueError("frame plan requires at least one frame")
        if self.requested_normalized_count <= 0 or self.maximum_frames <= 0:
            raise ValueError("frame counts must be positive")
        if len(requests) > self.maximum_frames:
            raise ValueError("frame plan exceeds maximum_frames")
        if not math.isfinite(self.scene_threshold) or not 0 < self.scene_threshold < 1:
            raise ValueError("scene threshold must be between 0 and 1")
        object.__setattr__(self, "requests", requests)


@dataclass(frozen=True)
class FrameFingerprint:
    timestamp_seconds: float
    normalized_position: float
    value: int
    bit_width: int = 64
    algorithm: str = "dct_phash_v1"

    def __post_init__(self) -> None:
        if not math.isfinite(self.timestamp_seconds) or self.timestamp_seconds < 0:
            raise ValueError("frame timestamp must be finite and non-negative")
        if not math.isfinite(self.normalized_position) or not 0 <= self.normalized_position <= 1:
            raise ValueError("normalized frame position must be between 0 and 1")
        if (
            isinstance(self.bit_width, bool)
            or not isinstance(self.bit_width, int)
            or not 0 < self.bit_width <= MAX_FRAME_FINGERPRINT_BITS
        ):
            raise ValueError("frame fingerprint bit width must be between 1 and {}".format(MAX_FRAME_FINGERPRINT_BITS))
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise ValueError("frame fingerprint value must be an integer")
        if self.value < 0 or self.value >= 1 << self.bit_width:
            raise ValueError("frame fingerprint does not fit bit width")
        if not self.algorithm:
            raise ValueError("frame fingerprint algorithm must not be empty")
        if len(self.algorithm) > MAX_ALGORITHM_CHARACTERS or "\0" in self.algorithm:
            raise ValueError("frame fingerprint algorithm is invalid")

    def to_dict(self) -> Dict[str, object]:
        return {
            "timestamp_seconds": float(self.timestamp_seconds),
            "normalized_position": float(self.normalized_position),
            "value": self.value,
            "bit_width": self.bit_width,
            "algorithm": self.algorithm,
        }


@dataclass(frozen=True)
class AudioFingerprint:
    values: Tuple[int, ...]
    duration_seconds: float
    algorithm: str = "chromaprint_raw_v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        if not self.values:
            raise ValueError("audio fingerprint must not be empty")
        if len(self.values) > MAX_AUDIO_FINGERPRINT_WORDS:
            raise ValueError("audio fingerprint exceeds the supported word limit")
        if any(value < -(1 << 31) or value >= 1 << 32 for value in self.values):
            raise ValueError("audio fingerprint values must fit a 32-bit fpcalc word")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("audio fingerprint duration must be finite and non-negative")
        if not self.algorithm:
            raise ValueError("audio fingerprint algorithm must not be empty")
        if len(self.algorithm) > MAX_ALGORITHM_CHARACTERS or "\0" in self.algorithm:
            raise ValueError("audio fingerprint algorithm is invalid")

    def to_dict(self) -> Dict[str, object]:
        return {
            "values": list(self.values),
            "duration_seconds": float(self.duration_seconds),
            "algorithm": self.algorithm,
        }


@dataclass(frozen=True)
class VideoArtifact:
    source: SourceSnapshot
    metadata: Optional[VideoMetadata]
    frames: Tuple[FrameFingerprint, ...]
    audio: Optional[AudioFingerprint]
    state: AnalysisState
    issues: Tuple[AnalysisIssue, ...] = ()
    tool_versions: Tuple[Tuple[str, str], ...] = ()
    analyzer_version: str = ANALYZER_VERSION
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "frames", tuple(sorted(self.frames, key=lambda item: item.timestamp_seconds)))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "tool_versions", tuple(sorted(self.tool_versions)))
        if len(self.frames) > MAX_ARTIFACT_FRAMES:
            raise ValueError("video artifact exceeds the supported frame limit")
        if len(self.issues) > MAX_ARTIFACT_ISSUES:
            raise ValueError("video artifact exceeds the supported issue limit")
        if len(self.tool_versions) > MAX_ARTIFACT_TOOL_VERSIONS:
            raise ValueError("video artifact exceeds the supported tool-version limit")
        if any(
            not isinstance(name, str)
            or not isinstance(version, str)
            or not name
            or len(name) > MAX_TOOL_TEXT_CHARACTERS
            or len(version) > MAX_TOOL_TEXT_CHARACTERS
            or "\0" in name
            or "\0" in version
            for name, version in self.tool_versions
        ):
            raise ValueError("video artifact contains an invalid tool version")
        if len({name for name, _version in self.tool_versions}) != len(self.tool_versions):
            raise ValueError("video artifact tool names must be unique")
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported video artifact schema version")
        if not self.analyzer_version:
            raise ValueError("analyzer version must not be empty")
        if len(self.analyzer_version) > MAX_ALGORITHM_CHARACTERS or "\0" in self.analyzer_version:
            raise ValueError("analyzer version is invalid")
        if self.state is AnalysisState.COMPLETE:
            if self.metadata is None or not self.frames:
                raise ValueError("complete artifact requires metadata and frame fingerprints")
            if self.issues:
                raise ValueError("complete artifact cannot contain analysis issues")
        elif not self.issues:
            raise ValueError("partial or failed artifact must explain its incompleteness")

    @property
    def comparable(self) -> bool:
        return self.metadata is not None and bool(self.frames)


class AlignmentState(Enum):
    COMPLETE = "complete"
    RESOURCE_LIMIT = "resource_limit"
    EMPTY_INPUT = "empty_input"


@dataclass(frozen=True)
class SequenceAlignment:
    state: AlignmentState
    score: float
    matched_pairs: Tuple[Tuple[int, int], ...]
    coverage_first: float
    coverage_second: float
    mean_distance: Optional[float]
    start_first: Optional[int]
    start_second: Optional[int]
    end_first: Optional[int]
    end_second: Optional[int]
    cells_evaluated: int

    def __post_init__(self) -> None:
        values = (self.score, self.coverage_first, self.coverage_second)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError("alignment scores and coverage must be between 0 and 1")
        if self.mean_distance is not None and (not math.isfinite(self.mean_distance) or self.mean_distance < 0):
            raise ValueError("mean distance must be finite and non-negative")
        if self.cells_evaluated < 0:
            raise ValueError("cells evaluated must be non-negative")


class VideoRelation(Enum):
    EXACT = "exact"
    NEAR_DUPLICATE = "near"
    TRANSCODED = "transcoded"
    TRIMMED = "trimmed"
    RELATED = "related"


@dataclass(frozen=True)
class ByteExactProof:
    first_path: str
    second_path: str
    size: int
    digest_algorithm: str
    digest_hex: str
    bytes_compared: int

    def __post_init__(self) -> None:
        if not self.first_path or not self.second_path or self.first_path == self.second_path:
            raise ValueError("byte proof requires two distinct paths")
        if self.size < 0 or self.bytes_compared != self.size:
            raise ValueError("byte proof must cover the complete file")
        if not self.digest_algorithm or not self.digest_hex:
            raise ValueError("byte proof requires a full digest")
        try:
            digest = bytes.fromhex(self.digest_hex)
        except ValueError as error:
            raise ValueError("byte proof digest must be hexadecimal") from error
        if not digest:
            raise ValueError("byte proof digest must not be empty")

    def to_dict(self) -> Dict[str, object]:
        return {
            "first_path": self.first_path,
            "second_path": self.second_path,
            "size": self.size,
            "digest_algorithm": self.digest_algorithm,
            "digest_hex": self.digest_hex,
            "bytes_compared": self.bytes_compared,
        }


@dataclass(frozen=True)
class RelationMetric:
    name: str
    value: float

    def __post_init__(self) -> None:
        if not self.name or not math.isfinite(self.value):
            raise ValueError("relation metric requires a name and finite value")


@dataclass(frozen=True)
class VideoRelationEvidence:
    first_path: str
    second_path: str
    relation: VideoRelation
    score: float
    metrics: Tuple[RelationMetric, ...]
    algorithm_version: str = ANALYZER_VERSION
    exact_proof: Optional[ByteExactProof] = None
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.first_path or not self.second_path or self.first_path == self.second_path:
            raise ValueError("relation evidence requires two distinct paths")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("relation score must be between 0 and 1")
        metrics = tuple(sorted(self.metrics, key=lambda item: item.name))
        if len({item.name for item in metrics}) != len(metrics):
            raise ValueError("relation metric names must be unique")
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "notes", tuple(self.notes))
        if not self.algorithm_version:
            raise ValueError("relation algorithm version must not be empty")
        if self.relation is VideoRelation.EXACT:
            if self.exact_proof is None:
                raise ValueError("exact video relation requires a full byte proof")
            if {self.first_path, self.second_path} != {
                self.exact_proof.first_path,
                self.exact_proof.second_path,
            }:
                raise ValueError("exact proof paths must match relation paths")
            if self.score != 1:
                raise ValueError("exact video relation must have score 1")
        elif self.exact_proof is not None:
            raise ValueError("only an exact relation may carry a byte proof")

    @property
    def allows_automatic_destructive_action(self) -> bool:
        return self.relation is VideoRelation.EXACT and self.exact_proof is not None

    def to_dict(self) -> Dict[str, object]:
        return {
            "first_path": self.first_path,
            "second_path": self.second_path,
            "relation": self.relation.value,
            "score": self.score,
            "metrics": {metric.name: metric.value for metric in self.metrics},
            "algorithm_version": self.algorithm_version,
            "exact_proof": self.exact_proof.to_dict() if self.exact_proof is not None else None,
            "notes": list(self.notes),
            "allows_automatic_destructive_action": self.allows_automatic_destructive_action,
        }
