# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Strict, versioned JSON serialization for video analysis artifacts."""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence

import json
import math

from core.safe_json import JsonStructuralLimits
from core.video.json_guard import strict_bounded_json_loads
from core.video.model import (
    ARTIFACT_SCHEMA_VERSION,
    MAX_ARTIFACT_FRAMES,
    MAX_ARTIFACT_ISSUES,
    MAX_ARTIFACT_TOOL_VERSIONS,
    MAX_AUDIO_FINGERPRINT_WORDS,
    AnalysisIssue,
    AnalysisState,
    AudioFingerprint,
    FrameFingerprint,
    SourceSnapshot,
    VideoArtifact,
    VideoMetadata,
)

MAX_VIDEO_ARTIFACT_JSON_BYTES = 16 * 1024 * 1024
VIDEO_ARTIFACT_JSON_LIMITS = JsonStructuralLimits(
    max_depth=8,
    max_container_entries=MAX_AUDIO_FINGERPRINT_WORDS,
    max_total_nodes=50_000,
    max_scalar_tokens=50_000,
    max_total_string_chars=2 * 1024 * 1024,
    max_string_chars=32_768,
    max_scalar_chars=1024,
)
_ARTIFACT_KEYS = {
    "schema_version",
    "analyzer_version",
    "source",
    "metadata",
    "frames",
    "audio",
    "state",
    "issues",
    "tool_versions",
}
_SOURCE_KEYS = {"path", "size", "mtime_ns"}
_METADATA_KEYS = {
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
}
_FRAME_KEYS = {
    "timestamp_seconds",
    "normalized_position",
    "value",
    "bit_width",
    "algorithm",
}
_AUDIO_KEYS = {"values", "duration_seconds", "algorithm"}
_ISSUE_KEYS = {"code", "message", "tool"}


def artifact_to_dict(artifact: VideoArtifact) -> Dict[str, object]:
    return {
        "schema_version": artifact.schema_version,
        "analyzer_version": artifact.analyzer_version,
        "source": artifact.source.to_dict(),
        "metadata": artifact.metadata.to_dict() if artifact.metadata is not None else None,
        "frames": [frame.to_dict() for frame in artifact.frames],
        "audio": artifact.audio.to_dict() if artifact.audio is not None else None,
        "state": artifact.state.value,
        "issues": [issue.to_dict() for issue in artifact.issues],
        "tool_versions": dict(artifact.tool_versions),
    }


def artifact_to_json(artifact: VideoArtifact) -> str:
    return json.dumps(
        artifact_to_dict(artifact),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def artifact_from_json(payload: str | bytes) -> VideoArtifact:
    try:
        document = strict_bounded_json_loads(
            payload,
            max_bytes=MAX_VIDEO_ARTIFACT_JSON_BYTES,
            limits=VIDEO_ARTIFACT_JSON_LIMITS,
            label="video artifact JSON",
        )
    except ValueError as error:
        raise ValueError("video cache artifact is not valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ValueError("video cache artifact must be a JSON object")
    return artifact_from_dict(document)


def artifact_from_dict(document: Mapping[str, object]) -> VideoArtifact:
    _require_exact_keys(document, _ARTIFACT_KEYS, "artifact")
    schema_version = _integer(document.get("schema_version"), "schema_version")
    if schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            "unsupported video cache schema version {}; expected {}".format(
                schema_version,
                ARTIFACT_SCHEMA_VERSION,
            )
        )
    analyzer_version = _string(document.get("analyzer_version"), "analyzer_version")
    source_document = _mapping(document.get("source"), "source")
    _require_exact_keys(source_document, _SOURCE_KEYS, "source")
    source = SourceSnapshot(
        path=_string(source_document.get("path"), "source.path"),
        size=_integer(source_document.get("size"), "source.size"),
        mtime_ns=_integer(source_document.get("mtime_ns"), "source.mtime_ns"),
    )

    metadata_document = _optional_mapping(document.get("metadata"), "metadata")
    metadata = _metadata_from_dict(metadata_document) if metadata_document is not None else None

    frame_documents = _sequence(document.get("frames"), "frames")
    _bounded_count(frame_documents, MAX_ARTIFACT_FRAMES, "frames")
    frames = []
    for index, item in enumerate(frame_documents):
        frame = _mapping(item, "frames[{}]".format(index))
        _require_exact_keys(frame, _FRAME_KEYS, "frames[{}]".format(index))
        frames.append(
            FrameFingerprint(
                timestamp_seconds=_number(frame.get("timestamp_seconds"), "frame.timestamp_seconds"),
                normalized_position=_number(frame.get("normalized_position"), "frame.normalized_position"),
                value=_integer(frame.get("value"), "frame.value"),
                bit_width=_integer(frame.get("bit_width"), "frame.bit_width"),
                algorithm=_string(frame.get("algorithm"), "frame.algorithm"),
            )
        )

    audio_document = _optional_mapping(document.get("audio"), "audio")
    audio = None
    if audio_document is not None:
        _require_exact_keys(audio_document, _AUDIO_KEYS, "audio")
        audio_values = _sequence(audio_document.get("values"), "audio.values")
        _bounded_count(
            audio_values,
            MAX_AUDIO_FINGERPRINT_WORDS,
            "audio.values",
        )
        audio = AudioFingerprint(
            tuple(_integer(value, "audio value") for value in audio_values),
            _number(audio_document.get("duration_seconds"), "audio.duration_seconds"),
            _string(audio_document.get("algorithm"), "audio.algorithm"),
        )

    try:
        state = AnalysisState(_string(document.get("state"), "state"))
    except ValueError as error:
        raise ValueError("video cache artifact contains an unknown analysis state") from error
    issue_documents = _sequence(document.get("issues"), "issues")
    _bounded_count(issue_documents, MAX_ARTIFACT_ISSUES, "issues")
    issues = []
    for index, item in enumerate(issue_documents):
        issue = _mapping(item, "issues[{}]".format(index))
        _require_exact_keys(issue, _ISSUE_KEYS, "issues[{}]".format(index))
        tool = issue.get("tool")
        if tool is not None:
            tool = _string(tool, "issue.tool")
        issues.append(
            AnalysisIssue(
                code=_string(issue.get("code"), "issue.code"),
                message=_string(issue.get("message"), "issue.message"),
                tool=tool,
            )
        )

    versions_document = _mapping(document.get("tool_versions"), "tool_versions")
    _bounded_count(
        versions_document,
        MAX_ARTIFACT_TOOL_VERSIONS,
        "tool_versions",
    )
    tool_versions = tuple(
        (_string(name, "tool version name"), _string(version, "tool version value"))
        for name, version in versions_document.items()
    )
    return VideoArtifact(
        source=source,
        metadata=metadata,
        frames=tuple(frames),
        audio=audio,
        state=state,
        issues=tuple(issues),
        tool_versions=tool_versions,
        analyzer_version=analyzer_version,
        schema_version=schema_version,
    )


def _metadata_from_dict(document: Mapping[str, object]) -> VideoMetadata:
    _require_exact_keys(document, _METADATA_KEYS, "metadata")
    return VideoMetadata(
        duration_seconds=_number(document.get("duration_seconds"), "metadata.duration_seconds"),
        width=_integer(document.get("width"), "metadata.width"),
        height=_integer(document.get("height"), "metadata.height"),
        frame_rate=_number(document.get("frame_rate"), "metadata.frame_rate"),
        video_codec=_string(document.get("video_codec"), "metadata.video_codec"),
        pixel_format=_string(document.get("pixel_format"), "metadata.pixel_format", allow_empty=True),
        audio_codec=_string(document.get("audio_codec"), "metadata.audio_codec", allow_empty=True),
        audio_duration_seconds=_optional_number(
            document.get("audio_duration_seconds"),
            "metadata.audio_duration_seconds",
        ),
        bit_rate=_optional_integer(document.get("bit_rate"), "metadata.bit_rate"),
        container=_string(document.get("container"), "metadata.container", allow_empty=True),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("{} must be an object with string keys".format(name))
    return value


def _require_exact_keys(value: Mapping[str, object], expected, name: str) -> None:
    if set(value) != expected:
        raise ValueError("{} has an unsupported object shape".format(name))


def _optional_mapping(value: object, name: str) -> Optional[Mapping[str, object]]:
    return None if value is None else _mapping(value, name)


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ValueError("{} must be an array".format(name))
    return value


def _bounded_count(value: Sequence[object] | Mapping[str, object], maximum: int, name: str) -> None:
    if len(value) > maximum:
        raise ValueError("{} exceeds the supported item limit of {}".format(name, maximum))


def _string(value: object, name: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > VIDEO_ARTIFACT_JSON_LIMITS.max_string_chars
        or "\0" in value
    ):
        raise ValueError("{} must be {}string".format(name, "" if allow_empty else "a non-empty "))
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value.bit_length() > 256:
        raise ValueError("{} must be an integer no wider than 256 bits".format(name))
    return value


def _optional_integer(value: object, name: str) -> Optional[int]:
    return None if value is None else _integer(value, name)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a number".format(name))
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError("{} must be a finite number".format(name)) from error
    if not math.isfinite(result):
        raise ValueError("{} must be a finite number".format(name))
    return result


def _optional_number(value: object, name: str) -> Optional[float]:
    return None if value is None else _number(value, name)
