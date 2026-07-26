# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Pure candidate, sampling, fingerprint, and relation-classification functions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from core.pe.candidate_index import dct_phash
from core.safe_json import JsonStructuralLimits
from core.video.json_guard import strict_bounded_json_loads
from core.video.model import (
    ANALYZER_VERSION,
    AlignmentState,
    AnalysisState,
    AudioFingerprint,
    ByteExactProof,
    CandidateAssessment,
    CandidateKind,
    FrameFingerprint,
    FrameOrigin,
    FramePlan,
    FrameRequest,
    MAX_AUDIO_FINGERPRINT_WORDS,
    RelationMetric,
    SequenceAlignment,
    VideoArtifact,
    VideoMetadata,
    VideoRelation,
    VideoRelationEvidence,
)

MAX_FPCALC_JSON_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class MetadataCandidatePolicy:
    duration_tolerance_seconds: float = 1.0
    duration_tolerance_ratio: float = 0.015
    aspect_tolerance_ratio: float = 0.025
    frame_rate_tolerance_ratio: float = 0.03
    minimum_trim_fraction: float = 0.20
    minimum_related_duration_fraction: float = 0.10

    def __post_init__(self) -> None:
        values = (
            self.duration_tolerance_seconds,
            self.duration_tolerance_ratio,
            self.aspect_tolerance_ratio,
            self.frame_rate_tolerance_ratio,
            self.minimum_trim_fraction,
            self.minimum_related_duration_fraction,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("metadata candidate thresholds must be finite and non-negative")
        if self.minimum_trim_fraction > 1 or self.minimum_related_duration_fraction > 1:
            raise ValueError("duration fractions cannot exceed one")


def classify_metadata_candidate(
    first: VideoMetadata,
    second: VideoMetadata,
    policy: MetadataCandidatePolicy = MetadataCandidatePolicy(),
) -> CandidateAssessment:
    """Classify a pair without claiming content equality."""

    duration_difference = abs(first.duration_seconds - second.duration_seconds)
    duration_limit = max(
        policy.duration_tolerance_seconds,
        max(first.duration_seconds, second.duration_seconds) * policy.duration_tolerance_ratio,
    )
    duration_fraction = min(first.duration_seconds, second.duration_seconds) / max(
        first.duration_seconds, second.duration_seconds
    )
    aspect_difference = abs(first.aspect_ratio - second.aspect_ratio) / max(first.aspect_ratio, second.aspect_ratio)
    frame_rate_difference = abs(first.frame_rate - second.frame_rate) / max(first.frame_rate, second.frame_rate)
    same_dimensions = (first.width, first.height) == (second.width, second.height)
    same_codec = first.video_codec == second.video_codec
    duration_matches = duration_difference <= duration_limit
    aspect_matches = aspect_difference <= policy.aspect_tolerance_ratio
    frame_rate_matches = frame_rate_difference <= policy.frame_rate_tolerance_ratio

    if duration_matches and same_dimensions and same_codec and frame_rate_matches:
        return CandidateAssessment(
            CandidateKind.SAME_ENCODE_CANDIDATE,
            0.98,
            ("duration, dimensions, codec, and frame rate agree; bytes are still unverified",),
        )
    if duration_matches and aspect_matches:
        score = _bounded(
            0.70
            + 0.12 * (1 - aspect_difference / max(policy.aspect_tolerance_ratio, 1e-12))
            + 0.08 * (1 - min(frame_rate_difference, 1))
        )
        return CandidateAssessment(
            CandidateKind.TRANSCODE_CANDIDATE,
            score,
            ("duration and aspect ratio agree while encoding properties differ",),
        )
    if aspect_matches and duration_fraction >= policy.minimum_trim_fraction:
        return CandidateAssessment(
            CandidateKind.TRIM_CANDIDATE,
            _bounded(0.45 + 0.35 * duration_fraction),
            ("aspect ratio agrees and the shorter duration is large enough for trim alignment",),
        )
    if duration_fraction >= policy.minimum_related_duration_fraction:
        return CandidateAssessment(
            CandidateKind.RELATED_CANDIDATE,
            _bounded(0.20 + 0.30 * duration_fraction + 0.10 * (1 - min(aspect_difference, 1))),
            ("metadata is weakly compatible; visual evidence is required",),
        )
    return CandidateAssessment(
        CandidateKind.REJECTED,
        0,
        ("duration ratio is below the configured candidate floor",),
    )


@dataclass(frozen=True)
class FramePlanPolicy:
    normalized_frames: int = 12
    maximum_frames: int = 32
    scene_threshold: float = 0.35
    minimum_separation_seconds: float = 0.20
    boundary_margin_fraction: float = 0.01

    def __post_init__(self) -> None:
        if self.normalized_frames <= 0 or self.maximum_frames <= 0:
            raise ValueError("frame counts must be positive")
        if self.normalized_frames > self.maximum_frames:
            raise ValueError("normalized frame count cannot exceed maximum frame count")
        if not 0 < self.scene_threshold < 1:
            raise ValueError("scene threshold must be between zero and one")
        if not math.isfinite(self.minimum_separation_seconds) or self.minimum_separation_seconds < 0:
            raise ValueError("minimum separation must be finite and non-negative")
        if not 0 <= self.boundary_margin_fraction < 0.5:
            raise ValueError("boundary margin must be between zero and one half")


def build_frame_plan(
    metadata: VideoMetadata,
    scene_times: Iterable[float] = (),
    policy: FramePlanPolicy = FramePlanPolicy(),
) -> FramePlan:
    """Combine deterministic normalized samples with bounded scene-change samples."""

    normalized = []
    usable_span = 1 - 2 * policy.boundary_margin_fraction
    for index in range(policy.normalized_frames):
        position = policy.boundary_margin_fraction + usable_span * (index + 1) / (policy.normalized_frames + 1)
        normalized.append(FrameRequest(metadata.duration_seconds * position, position, FrameOrigin.NORMALIZED))

    selected = list(normalized)
    valid_scenes = sorted(
        {
            float(timestamp)
            for timestamp in scene_times
            if math.isfinite(float(timestamp)) and 0 <= float(timestamp) <= metadata.duration_seconds
        }
    )
    scene_requests = [
        FrameRequest(timestamp, timestamp / metadata.duration_seconds, FrameOrigin.SCENE_CHANGE)
        for timestamp in valid_scenes
    ]
    # Prefer scene changes furthest from existing normalized samples, then restore chronological order.
    scene_requests.sort(
        key=lambda request: (
            -min(abs(request.timestamp_seconds - item.timestamp_seconds) for item in normalized),
            request.timestamp_seconds,
        )
    )
    for request in scene_requests:
        if len(selected) >= policy.maximum_frames:
            break
        if all(
            abs(request.timestamp_seconds - existing.timestamp_seconds) >= policy.minimum_separation_seconds
            for existing in selected
        ):
            selected.append(request)
    return FramePlan(
        tuple(selected),
        requested_normalized_count=policy.normalized_frames,
        scene_threshold=policy.scene_threshold,
        maximum_frames=policy.maximum_frames,
    )


def phash_gray_frame(
    pixels: bytes,
    *,
    width: int = 32,
    height: int = 32,
    timestamp_seconds: float = 0,
    normalized_position: float = 0,
) -> FrameFingerprint:
    """Compute the shared DCT pHash from an FFmpeg 8-bit gray frame."""

    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if len(pixels) != width * height:
        raise ValueError("gray frame byte length does not match dimensions")
    matrix = tuple(tuple(pixels[y * width : (y + 1) * width]) for y in range(height))
    fingerprint = dct_phash(matrix, hash_size=8)
    return FrameFingerprint(
        timestamp_seconds=timestamp_seconds,
        normalized_position=normalized_position,
        value=fingerprint.value,
        bit_width=fingerprint.bit_width,
        algorithm=fingerprint.algorithm,
    )


def parse_fpcalc_json(
    payload: bytes | str,
    *,
    maximum_words: int = MAX_AUDIO_FINGERPRINT_WORDS,
) -> AudioFingerprint:
    if (
        isinstance(maximum_words, bool)
        or not isinstance(maximum_words, int)
        or not 0 < maximum_words <= MAX_AUDIO_FINGERPRINT_WORDS
    ):
        raise ValueError(
            "maximum_words must be between 1 and {}".format(
                MAX_AUDIO_FINGERPRINT_WORDS,
            )
        )
    limits = JsonStructuralLimits(
        max_depth=4,
        max_container_entries=maximum_words,
        max_total_nodes=(2 * maximum_words) + 32,
        max_scalar_tokens=(2 * maximum_words) + 24,
        max_total_string_chars=(maximum_words * 12) + 4096,
        max_string_chars=maximum_words * 12,
        max_scalar_chars=32,
    )
    try:
        document = strict_bounded_json_loads(
            payload,
            max_bytes=MAX_FPCALC_JSON_BYTES,
            limits=limits,
            label="fpcalc JSON",
        )
    except ValueError as error:
        raise ValueError("fpcalc returned invalid JSON") from error
    if not isinstance(document, dict):
        raise ValueError("fpcalc document must be an object")
    raw_fingerprint = document.get("fingerprint")
    if isinstance(raw_fingerprint, str):
        if len(raw_fingerprint) > maximum_words * 12:
            raise ValueError("fpcalc fingerprint exceeds the supported word limit")
        parts = [part.strip() for part in raw_fingerprint.split(",") if part.strip()]
        if len(parts) > maximum_words:
            raise ValueError("fpcalc fingerprint exceeds the supported word limit")
        try:
            values = tuple(int(part) for part in parts)
        except ValueError as error:
            raise ValueError("fpcalc fingerprint contains a non-integer word") from error
    elif isinstance(raw_fingerprint, list):
        if len(raw_fingerprint) > maximum_words:
            raise ValueError("fpcalc fingerprint exceeds the supported word limit")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in raw_fingerprint):
            raise ValueError("fpcalc fingerprint array must contain integers")
        values = tuple(raw_fingerprint)
    else:
        raise ValueError("fpcalc raw fingerprint is missing")
    try:
        duration_value = document["duration"]
        if isinstance(duration_value, bool) or not isinstance(duration_value, (int, float, str)):
            raise ValueError("duration must be numeric")
        duration = float(duration_value)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("fpcalc duration is missing or invalid") from error
    return AudioFingerprint(values, duration)


@dataclass(frozen=True)
class RelationPolicy:
    near_score: float = 0.88
    transcode_score: float = 0.84
    trim_score: float = 0.76
    related_score: float = 0.35
    complete_coverage: float = 0.82
    trimmed_duration_ratio: float = 0.90
    minimum_aligned_frames: int = 3

    def __post_init__(self) -> None:
        for value in (
            self.near_score,
            self.transcode_score,
            self.trim_score,
            self.related_score,
            self.complete_coverage,
            self.trimmed_duration_ratio,
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("relation thresholds must be between zero and one")
        if self.minimum_aligned_frames <= 0:
            raise ValueError("minimum aligned frame count must be positive")


def classify_video_relation(
    first: VideoArtifact,
    second: VideoArtifact,
    *,
    frame_alignment: Optional[SequenceAlignment] = None,
    audio_alignment: Optional[SequenceAlignment] = None,
    policy: RelationPolicy = RelationPolicy(),
) -> Optional[VideoRelationEvidence]:
    """Classify perceptual evidence without ever upgrading it to byte-exact proof."""

    if not first.comparable or not second.comparable:
        raise ValueError("video artifacts require metadata and frame fingerprints for comparison")
    if frame_alignment is None:
        from core.video.alignment import align_frame_fingerprints

        frame_alignment = align_frame_fingerprints(first.frames, second.frames)
    if frame_alignment.state is not AlignmentState.COMPLETE:
        raise ValueError("frame alignment is incomplete: {}".format(frame_alignment.state.value))
    if audio_alignment is None and first.audio is not None and second.audio is not None:
        from core.video.alignment import align_audio_fingerprints

        audio_alignment = align_audio_fingerprints(first.audio, second.audio)
    if audio_alignment is not None and audio_alignment.state is not AlignmentState.COMPLETE:
        raise ValueError("audio alignment is incomplete: {}".format(audio_alignment.state.value))

    frame_score = frame_alignment.score
    audio_score = audio_alignment.score if audio_alignment is not None else None
    combined_score = frame_score if audio_score is None else 0.78 * frame_score + 0.22 * audio_score
    first_metadata = first.metadata
    second_metadata = second.metadata
    assert first_metadata is not None
    assert second_metadata is not None
    duration_ratio = min(first_metadata.duration_seconds, second_metadata.duration_seconds) / max(
        first_metadata.duration_seconds, second_metadata.duration_seconds
    )
    coverage_min = min(frame_alignment.coverage_first, frame_alignment.coverage_second)
    encoding_changed = (
        first_metadata.video_codec != second_metadata.video_codec
        or (first_metadata.width, first_metadata.height) != (second_metadata.width, second_metadata.height)
        or first_metadata.pixel_format != second_metadata.pixel_format
    )

    notes = []
    enough_frames = len(frame_alignment.matched_pairs) >= policy.minimum_aligned_frames
    if first.state is not AnalysisState.COMPLETE or second.state is not AnalysisState.COMPLETE:
        notes.append("one or both fingerprints are partial; manual review is required")
    if (
        enough_frames
        and combined_score >= policy.transcode_score
        and coverage_min >= policy.complete_coverage
        and encoding_changed
    ):
        relation = VideoRelation.TRANSCODED
    elif (
        enough_frames
        and combined_score >= policy.trim_score
        and max(frame_alignment.coverage_first, frame_alignment.coverage_second) >= policy.complete_coverage
        and (coverage_min < policy.complete_coverage or duration_ratio < policy.trimmed_duration_ratio)
    ):
        relation = VideoRelation.TRIMMED
    elif enough_frames and combined_score >= policy.near_score and coverage_min >= policy.complete_coverage:
        relation = VideoRelation.NEAR_DUPLICATE
    elif combined_score >= policy.related_score:
        relation = VideoRelation.RELATED
    else:
        return None

    metrics = [
        RelationMetric("frame_score", frame_score),
        RelationMetric("frame_coverage_first", frame_alignment.coverage_first),
        RelationMetric("frame_coverage_second", frame_alignment.coverage_second),
        RelationMetric("duration_ratio", duration_ratio),
        RelationMetric("aligned_frame_count", float(len(frame_alignment.matched_pairs))),
    ]
    if audio_score is not None:
        metrics.append(RelationMetric("audio_score", audio_score))
    return VideoRelationEvidence(
        first_path=first.source.path,
        second_path=second.source.path,
        relation=relation,
        score=_bounded(combined_score),
        metrics=tuple(metrics),
        algorithm_version=ANALYZER_VERSION,
        notes=tuple(notes),
    )


def evidence_from_exact_proof(proof: ByteExactProof) -> VideoRelationEvidence:
    """Create the only video relation which permits automatic destructive action."""

    return VideoRelationEvidence(
        first_path=proof.first_path,
        second_path=proof.second_path,
        relation=VideoRelation.EXACT,
        score=1,
        metrics=(RelationMetric("bytes_compared", float(proof.bytes_compared)),),
        exact_proof=proof,
        notes=("full byte digest and byte-for-byte comparison were supplied by the exact engine",),
    )


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))
