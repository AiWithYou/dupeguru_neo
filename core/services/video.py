# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Qt-free, schema-versioned service boundary for video analysis."""

from __future__ import annotations

import math
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from core import fs as core_fs
from core.safe_action import platform_file_system
from core.services.models import (
    SCHEMA_VERSION,
    VIDEO_ANALYSIS_SCHEMA,
    VIDEO_CAPABILITIES_SCHEMA,
    VIDEO_COMPARISON_SCHEMA,
    utc_now,
)
from core.video import (
    AlignmentState,
    AnalysisLimits,
    AnalysisState,
    FramePlanPolicy,
    RelationPolicy,
    SubprocessCommandRunner,
    ToolCapability,
    ToolName,
    VideoAnalyzer,
    VideoArtifact,
    VideoLibraryLimits,
    VideoLibraryScanner,
    VideoRelation,
    align_audio_fingerprints,
    align_frame_fingerprints,
    artifact_from_json,
    artifact_to_dict,
    artifact_to_json,
    classify_video_relation,
    detect_capabilities,
)
from core.video.tools import CommandRunner, capabilities_by_name, resolve_source_snapshot

MAXIMUM_ARTIFACT_BYTES = 16 * 1024 * 1024

_STATE_PRIORITY = {
    AnalysisState.COMPLETE: 0,
    AnalysisState.PARTIAL_MISSING_TOOL: 1,
    AnalysisState.PARTIAL_TOOL_ERROR: 2,
    AnalysisState.PARTIAL_RESOURCE_LIMIT: 3,
    AnalysisState.PARTIAL_TIMEOUT: 4,
    AnalysisState.PARTIAL_CANCELLED: 5,
    AnalysisState.FAILED: 6,
}


class VideoService:
    """Analyze and compare videos without obscuring degraded outcomes."""

    def __init__(
        self,
        *,
        runner: Optional[CommandRunner] = None,
        executables: Optional[Mapping[ToolName, str]] = None,
        capabilities: Optional[Sequence[ToolCapability]] = None,
        limits: AnalysisLimits = AnalysisLimits(),
        frame_policy: FramePlanPolicy = FramePlanPolicy(),
    ) -> None:
        if capabilities is not None and executables is not None:
            raise ValueError("provide capabilities or executable paths, not both")
        self.runner = runner or SubprocessCommandRunner()
        self.executables = dict(executables or {})
        self.fixed_capabilities = tuple(capabilities) if capabilities is not None else None
        if self.fixed_capabilities is not None:
            capabilities_by_name(self.fixed_capabilities)
        self.limits = limits
        self.frame_policy = frame_policy

    def inspect_capabilities(self) -> Dict[str, Any]:
        capabilities = self._capabilities()
        issues = [
            {
                "code": "tool_{}".format(item.state.value),
                "message": item.message,
                "tool": item.tool.value,
            }
            for item in capabilities
            if not item.available
        ]
        tools = [
            {
                "name": item.tool.value,
                "state": item.state.value,
                "available": item.available,
                "required": item.tool in {ToolName.FFPROBE, ToolName.FFMPEG},
                "executable": item.executable,
                "version": item.version,
                "message": item.message,
            }
            for item in capabilities
        ]
        complete = not issues
        by_name = capabilities_by_name(capabilities)
        return {
            "schema": VIDEO_CAPABILITIES_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "state": "complete" if complete else "partial",
            "partial": not complete,
            "issues": issues,
            "tools": tools,
            "summary": {
                "available": sum(item.available for item in capabilities),
                "unavailable": sum(not item.available for item in capabilities),
                "visual_analysis_available": (
                    by_name[ToolName.FFPROBE].available and by_name[ToolName.FFMPEG].available
                ),
                "audio_fingerprint_available": by_name[ToolName.FPCALC].available,
            },
        }

    def analyze(
        self,
        path: str | Path,
        *,
        artifact_input: Optional[str | Path] = None,
        artifact_output: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        if artifact_input is None:
            artifact = self._analyzer(self._capabilities()).analyze(path)
            artifact_source = "analyzed"
        else:
            artifact = load_artifact(artifact_input)
            _validate_cached_source(path, artifact)
            artifact_source = "cache"
        if artifact_output is not None:
            write_artifact_new(artifact_output, artifact)
        return analysis_report(
            artifact,
            artifact_source=artifact_source,
            artifact_input=artifact_input,
            artifact_output=artifact_output,
        )

    def compare(
        self,
        first: str | Path,
        second: str | Path,
        *,
        threshold: float = RelationPolicy().related_score,
    ) -> Dict[str, Any]:
        _validate_threshold(threshold)
        analyzer = self._analyzer(self._capabilities())
        first_artifact = analyzer.analyze(first)
        second_artifact = analyzer.analyze(second)
        return comparison_report(first_artifact, second_artifact, threshold=threshold)

    def scan(
        self,
        roots: Sequence[str | Path],
        *,
        cache_path: Optional[str | Path] = None,
        threshold: float = RelationPolicy().related_score,
        limits: VideoLibraryLimits = VideoLibraryLimits(),
        progress: Optional[Callable[[str, Mapping[str, object]], None]] = None,
    ) -> Dict[str, object]:
        """Run a bounded source-read-only perceptual scan over video roots."""

        capabilities = self._capabilities()
        scanner = VideoLibraryScanner(
            runner=self.runner,
            capabilities=capabilities,
            analyzer_limits=self.limits,
            analyzer_factory=lambda: self._analyzer(capabilities),
        )
        return scanner.scan(
            roots,
            cache_path=cache_path,
            threshold=threshold,
            limits=limits,
            progress=progress,
        )

    def _capabilities(self) -> Tuple[ToolCapability, ...]:
        if self.fixed_capabilities is not None:
            return self.fixed_capabilities
        return detect_capabilities(
            self.runner,
            executables=self.executables,
            timeout_seconds=self.limits.capability_timeout_seconds,
        )

    def _analyzer(self, capabilities: Sequence[ToolCapability]) -> VideoAnalyzer:
        return VideoAnalyzer(
            runner=self.runner,
            capabilities=capabilities,
            limits=self.limits,
            frame_policy=self.frame_policy,
        )


def analysis_report(
    artifact: VideoArtifact,
    *,
    artifact_source: str,
    artifact_input: Optional[str | Path],
    artifact_output: Optional[str | Path],
) -> Dict[str, Any]:
    if artifact_source not in {"analyzed", "cache"}:
        raise ValueError("unknown video artifact source")
    return {
        "schema": VIDEO_ANALYSIS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "state": artifact.state.value,
        "partial": _is_partial(artifact.state),
        "issues": [_issue_dict(issue, artifact.source.path) for issue in artifact.issues],
        "artifact_source": artifact_source,
        "artifact_cache": {
            "input": _optional_absolute(artifact_input),
            "output": _optional_absolute(artifact_output),
        },
        "artifact": artifact_to_dict(artifact),
        "summary": {
            "comparable": artifact.comparable,
            "frames": len(artifact.frames),
            "has_audio_fingerprint": artifact.audio is not None,
        },
    }


def comparison_report(
    first: VideoArtifact,
    second: VideoArtifact,
    *,
    threshold: float = RelationPolicy().related_score,
) -> Dict[str, Any]:
    """Compare two artifacts; this perceptual path can never emit ``exact``."""

    _validate_threshold(threshold)
    issues = [
        *(_issue_dict(issue, first.source.path) for issue in first.issues),
        *(_issue_dict(issue, second.source.path) for issue in second.issues),
    ]
    state = _combined_state(first.state, second.state)
    relation = None

    if first.comparable and second.comparable:
        frame_alignment = align_frame_fingerprints(first.frames, second.frames)
        if frame_alignment.state is not AlignmentState.COMPLETE:
            state = _combined_state(state, AnalysisState.PARTIAL_RESOURCE_LIMIT)
            issues.append(
                {
                    "code": "frame_alignment_{}".format(frame_alignment.state.value),
                    "message": "frame fingerprint alignment did not complete",
                    "tool": None,
                    "source": None,
                }
            )
        else:
            audio_alignment = None
            alignment_complete = True
            if first.audio is not None and second.audio is not None:
                audio_alignment = align_audio_fingerprints(first.audio, second.audio)
                if audio_alignment.state is not AlignmentState.COMPLETE:
                    alignment_complete = False
                    state = _combined_state(state, AnalysisState.PARTIAL_RESOURCE_LIMIT)
                    issues.append(
                        {
                            "code": "audio_alignment_{}".format(audio_alignment.state.value),
                            "message": "audio fingerprint alignment did not complete",
                            "tool": None,
                            "source": None,
                        }
                    )
            if alignment_complete:
                candidate = classify_video_relation(
                    first,
                    second,
                    frame_alignment=frame_alignment,
                    audio_alignment=audio_alignment,
                    policy=RelationPolicy(related_score=threshold),
                )
                if candidate is not None and candidate.score >= threshold:
                    if candidate.relation is VideoRelation.EXACT or candidate.exact_proof is not None:
                        raise RuntimeError("perceptual video comparison attempted to emit byte-exact evidence")
                    relation = candidate

    return {
        "schema": VIDEO_COMPARISON_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "state": state.value,
        "partial": _is_partial(state),
        "issues": issues,
        "threshold": float(threshold),
        "first": artifact_to_dict(first),
        "second": artifact_to_dict(second),
        "relation": relation.to_dict() if relation is not None else None,
        "byte_exact_proof": None,
        "allows_automatic_destructive_action": False,
        "summary": {
            "comparable": first.comparable and second.comparable,
            "relation_found": relation is not None,
        },
    }


def load_artifact(path: str | Path) -> VideoArtifact:
    artifact_path = Path(os.path.abspath(os.fspath(path)))
    file_system = platform_file_system()
    try:
        path_before = file_system.lstat(artifact_path)
        _validate_cache_file(path_before, artifact_path)
        path_before_snapshot = core_fs.FileSnapshot.from_path(
            artifact_path,
            path_before,
        )
        if int(path_before.st_size) > MAXIMUM_ARTIFACT_BYTES:
            raise ValueError("video artifact cache exceeds the maximum supported size")
        with file_system.open_readonly(artifact_path) as handle:
            opened_before = os.fstat(handle.fileno())
            _validate_cache_file(opened_before, artifact_path)
            opened_before_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=artifact_path,
                stat_result=opened_before,
            )
            payload = handle.read(MAXIMUM_ARTIFACT_BYTES + 1)
            opened_after = os.fstat(handle.fileno())
            opened_after_snapshot = core_fs.FileSnapshot.from_file(
                handle,
                path=artifact_path,
                stat_result=opened_after,
            )
        path_after = file_system.lstat(artifact_path)
        _validate_cache_file(path_after, artifact_path)
        path_after_snapshot = core_fs.FileSnapshot.from_path(
            artifact_path,
            path_after,
        )
    except OSError as error:
        raise ValueError("could not read video artifact cache '{}': {}".format(artifact_path, error)) from error
    if len(payload) > MAXIMUM_ARTIFACT_BYTES:
        raise ValueError("video artifact cache exceeds the maximum supported size")
    snapshots = (
        path_before_snapshot,
        opened_before_snapshot,
        opened_after_snapshot,
        path_after_snapshot,
    )
    if any(item != snapshots[0] for item in snapshots[1:]):
        raise ValueError("video artifact cache changed while it was being read")
    return artifact_from_json(payload)


def write_artifact_new(path: str | Path, artifact: VideoArtifact) -> Path:
    """Atomically create a cache artifact without replacing an existing path."""

    destination = Path(os.path.abspath(os.fspath(path)))
    parent = destination.parent
    file_system = platform_file_system()
    try:
        parent_stat = file_system.lstat(parent)
    except OSError as error:
        raise ValueError("video artifact cache parent is unavailable: {}".format(error)) from error
    if stat.S_ISLNK(parent_stat.st_mode) or _is_reparse_point(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("video artifact cache parent must be a plain directory")
    if file_system.lexists(destination):
        raise ValueError("video artifact cache already exists: '{}'".format(destination))

    encoded = (artifact_to_json(artifact) + "\n").encode("utf-8")
    temporary = parent.joinpath(".{}.{}.tmp".format(destination.name, uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = None
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while saving video artifact cache")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        file_system.rename_no_replace(temporary, destination)
        file_system.fsync_directory(parent)
    except OSError as error:
        raise ValueError("could not create video artifact cache '{}': {}".format(destination, error)) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if file_system.lexists(temporary):
            try:
                file_system.unlink(temporary)
            except OSError:
                pass
    return destination


def _validate_cached_source(path: str | Path, artifact: VideoArtifact) -> None:
    try:
        current = resolve_source_snapshot(path)
    except (OSError, ValueError) as error:
        raise ValueError("could not validate cached video source: {}".format(error)) from error
    expected = (
        artifact.source.path,
        artifact.source.size,
        artifact.source.mtime_ns,
    )
    if current[:3] != expected:
        raise ValueError("video artifact cache does not match the requested source snapshot")


def _validate_cache_file(file_stat: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("video artifact cache must be a plain regular file: '{}'".format(path))


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _issue_dict(issue, source: str) -> Dict[str, Any]:
    return {
        "code": issue.code,
        "message": issue.message,
        "tool": issue.tool,
        "source": source,
    }


def _combined_state(*states: AnalysisState) -> AnalysisState:
    return max(states, key=_STATE_PRIORITY.__getitem__)


def _is_partial(state: AnalysisState) -> bool:
    return state not in {AnalysisState.COMPLETE, AnalysisState.FAILED}


def _validate_threshold(threshold: float) -> None:
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ValueError("video comparison threshold must be a number")
    if not math.isfinite(float(threshold)) or not 0 <= float(threshold) <= 1:
        raise ValueError("video comparison threshold must be between 0 and 1")


def _optional_absolute(path: Optional[str | Path]) -> Optional[str]:
    if path is None:
        return None
    return os.path.abspath(os.fspath(path))


__all__ = [
    "MAXIMUM_ARTIFACT_BYTES",
    "VIDEO_ANALYSIS_SCHEMA",
    "VIDEO_CAPABILITIES_SCHEMA",
    "VIDEO_COMPARISON_SCHEMA",
    "VideoService",
    "analysis_report",
    "comparison_report",
    "load_artifact",
    "write_artifact_new",
]
