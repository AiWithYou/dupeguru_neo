# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded orchestration of FFprobe, FFmpeg frame sampling, and Chromaprint."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from core.video.fingerprint import FramePlanPolicy, build_frame_plan, parse_fpcalc_json, phash_gray_frame
from core.video.model import (
    AnalysisIssue,
    AnalysisState,
    AudioFingerprint,
    FrameFingerprint,
    MAX_ARTIFACT_FRAMES,
    MAX_AUDIO_FINGERPRINT_WORDS,
    SourceSnapshot,
    VideoArtifact,
    VideoMetadata,
)
from core.video.tools import (
    CancellationToken,
    CommandOutcome,
    CommandRunner,
    CommandState,
    SubprocessCommandRunner,
    ToolCapability,
    ToolName,
    ToolState,
    capabilities_by_name,
    detect_capabilities,
    ffmpeg_frame_command,
    ffmpeg_scene_command,
    ffprobe_command,
    fpcalc_command,
    parse_ffprobe_json,
    parse_scene_times,
    resolve_source_snapshot,
)


@dataclass(frozen=True)
class AnalysisLimits:
    capability_timeout_seconds: float = 5
    probe_timeout_seconds: float = 20
    scene_timeout_seconds: float = 90
    frame_timeout_seconds: float = 20
    audio_timeout_seconds: float = 180
    total_timeout_seconds: float = 300
    maximum_processes: int = 48
    maximum_command_output_bytes: int = 4 * 1024 * 1024
    maximum_scene_output_bytes: int = 2 * 1024 * 1024
    maximum_frames: int = MAX_ARTIFACT_FRAMES
    maximum_audio_seconds: int = 900
    maximum_audio_fingerprint_words: int = MAX_AUDIO_FINGERPRINT_WORDS
    frame_width: int = 32
    frame_height: int = 32

    def __post_init__(self) -> None:
        timeout_values = (
            self.capability_timeout_seconds,
            self.probe_timeout_seconds,
            self.scene_timeout_seconds,
            self.frame_timeout_seconds,
            self.audio_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            for value in timeout_values
        ):
            raise ValueError("analysis timeouts must be positive and finite")
        integer_values = (
            self.maximum_processes,
            self.maximum_command_output_bytes,
            self.maximum_scene_output_bytes,
            self.maximum_frames,
            self.maximum_audio_seconds,
            self.maximum_audio_fingerprint_words,
            self.frame_width,
            self.frame_height,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integer_values):
            raise ValueError("analysis resource limits must be positive integers")
        if self.maximum_frames > MAX_ARTIFACT_FRAMES:
            raise ValueError("maximum_frames exceeds the artifact format limit")
        if self.maximum_audio_fingerprint_words > MAX_AUDIO_FINGERPRINT_WORDS:
            raise ValueError("maximum_audio_fingerprint_words exceeds the artifact format limit")


class _ExecutionBudget:
    def __init__(
        self,
        runner: CommandRunner,
        limits: AnalysisLimits,
        cancel_event: Optional[CancellationToken],
    ) -> None:
        self.runner = runner
        self.limits = limits
        self.cancel_event = cancel_event
        self.started = time.monotonic()
        self.processes = 0

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandOutcome:
        command = tuple(argv)
        if self.cancel_event is not None and self.cancel_event.is_set():
            return CommandOutcome(command, CommandState.CANCELLED, None, error="analysis was cancelled")
        if self.processes >= self.limits.maximum_processes:
            return CommandOutcome(
                command,
                CommandState.OUTPUT_LIMIT,
                None,
                error="analysis process-count resource limit was reached",
            )
        remaining = self.limits.total_timeout_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            return CommandOutcome(command, CommandState.TIMED_OUT, None, error="total analysis timeout was reached")
        self.processes += 1
        return self.runner.run(
            command,
            timeout_seconds=min(timeout_seconds, remaining),
            cancel_event=self.cancel_event,
            max_output_bytes=max_output_bytes,
        )


class VideoAnalyzer:
    """Produce a cacheable artifact whose state describes every degraded outcome."""

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
        if frame_policy.maximum_frames > limits.maximum_frames:
            raise ValueError("frame plan exceeds analyzer frame resource limit")
        self.runner = runner or SubprocessCommandRunner()
        self.executables = dict(executables or {})
        self.capabilities = tuple(capabilities) if capabilities is not None else None
        if self.capabilities is not None:
            capabilities_by_name(self.capabilities)
        self.limits = limits
        self.frame_policy = frame_policy

    def analyze(
        self,
        path: str | Path,
        *,
        cancel_event: Optional[CancellationToken] = None,
    ) -> VideoArtifact:
        try:
            resolved_path, size, mtime_ns, generation_token = resolve_source_snapshot(path)
            source = SourceSnapshot(resolved_path, size, mtime_ns)
        except (OSError, ValueError) as error:
            source = SourceSnapshot(str(Path(path).absolute()), 0, 0)
            return VideoArtifact(
                source=source,
                metadata=None,
                frames=(),
                audio=None,
                state=AnalysisState.FAILED,
                issues=(AnalysisIssue("source_unavailable", str(error)),),
            )

        capabilities = self.capabilities
        if capabilities is None:
            capabilities = detect_capabilities(
                self.runner,
                executables=self.executables,
                timeout_seconds=self.limits.capability_timeout_seconds,
                cancel_event=cancel_event,
            )
        capability_map = capabilities_by_name(capabilities)
        tool_versions = tuple((item.tool.value, item.version or "unknown") for item in capabilities if item.available)
        budget = _ExecutionBudget(self.runner, self.limits, cancel_event)
        problems: list[Tuple[AnalysisState, AnalysisIssue]] = []

        probe_capability = capability_map[ToolName.FFPROBE]
        if not probe_capability.available:
            state, issue = _capability_problem(probe_capability, essential=True)
            return self._artifact(source, None, (), None, state, (issue,), tool_versions)
        assert probe_capability.executable is not None
        probe = budget.run(
            ffprobe_command(probe_capability.executable, source.path),
            timeout_seconds=self.limits.probe_timeout_seconds,
            max_output_bytes=self.limits.maximum_command_output_bytes,
        )
        if probe.state is not CommandState.SUCCESS:
            state, issue = _command_problem(probe, ToolName.FFPROBE, "metadata_probe_failed")
            if state is AnalysisState.PARTIAL_TOOL_ERROR:
                state = AnalysisState.FAILED
            return self._artifact(source, None, (), None, state, (issue,), tool_versions)
        try:
            metadata = parse_ffprobe_json(probe.stdout)
        except ValueError as error:
            return self._artifact(
                source,
                None,
                (),
                None,
                AnalysisState.FAILED,
                (AnalysisIssue("invalid_probe_output", str(error), ToolName.FFPROBE.value),),
                tool_versions,
            )

        ffmpeg_capability = capability_map[ToolName.FFMPEG]
        if not ffmpeg_capability.available:
            state, issue = _capability_problem(ffmpeg_capability, essential=False)
            problems.append((state, issue))
            return self._artifact(
                source,
                metadata,
                (),
                None,
                _combined_state(problems),
                tuple(item[1] for item in problems),
                tool_versions,
            )
        assert ffmpeg_capability.executable is not None

        scene_times: Tuple[float, ...] = ()
        scene = budget.run(
            ffmpeg_scene_command(
                ffmpeg_capability.executable,
                source.path,
                self.frame_policy.scene_threshold,
            ),
            timeout_seconds=self.limits.scene_timeout_seconds,
            max_output_bytes=self.limits.maximum_scene_output_bytes,
        )
        if scene.state is CommandState.SUCCESS:
            scene_times = parse_scene_times(scene.stderr, metadata.duration_seconds)
        else:
            problems.append(_command_problem(scene, ToolName.FFMPEG, "scene_detection_incomplete"))
        plan = build_frame_plan(metadata, scene_times, self.frame_policy)

        frames = []
        for request in plan.requests:
            outcome = budget.run(
                ffmpeg_frame_command(
                    ffmpeg_capability.executable,
                    source.path,
                    request.timestamp_seconds,
                    width=self.limits.frame_width,
                    height=self.limits.frame_height,
                ),
                timeout_seconds=self.limits.frame_timeout_seconds,
                max_output_bytes=self.limits.maximum_command_output_bytes,
            )
            if outcome.state is not CommandState.SUCCESS:
                problems.append(_command_problem(outcome, ToolName.FFMPEG, "frame_extraction_incomplete"))
                if outcome.state in {
                    CommandState.CANCELLED,
                    CommandState.TIMED_OUT,
                    CommandState.OUTPUT_LIMIT,
                }:
                    break
                continue
            try:
                frames.append(
                    phash_gray_frame(
                        outcome.stdout,
                        width=self.limits.frame_width,
                        height=self.limits.frame_height,
                        timestamp_seconds=request.timestamp_seconds,
                        normalized_position=request.normalized_position,
                    )
                )
            except ValueError as error:
                problems.append(
                    (
                        AnalysisState.PARTIAL_TOOL_ERROR,
                        AnalysisIssue("invalid_frame_output", str(error), ToolName.FFMPEG.value),
                    )
                )

        audio = None
        if metadata.audio_codec and not _has_terminal_problem(problems):
            fpcalc_capability = capability_map[ToolName.FPCALC]
            if not fpcalc_capability.available:
                problems.append(_capability_problem(fpcalc_capability, essential=False))
            else:
                assert fpcalc_capability.executable is not None
                audio_outcome = budget.run(
                    fpcalc_command(
                        fpcalc_capability.executable,
                        source.path,
                        maximum_seconds=self.limits.maximum_audio_seconds,
                    ),
                    timeout_seconds=self.limits.audio_timeout_seconds,
                    max_output_bytes=self.limits.maximum_command_output_bytes,
                )
                if audio_outcome.state is CommandState.SUCCESS:
                    try:
                        parsed_audio = parse_fpcalc_json(audio_outcome.stdout)
                        if len(parsed_audio.values) > self.limits.maximum_audio_fingerprint_words:
                            audio = AudioFingerprint(
                                parsed_audio.values[: self.limits.maximum_audio_fingerprint_words],
                                parsed_audio.duration_seconds,
                                parsed_audio.algorithm,
                            )
                            problems.append(
                                (
                                    AnalysisState.PARTIAL_RESOURCE_LIMIT,
                                    AnalysisIssue(
                                        "audio_fingerprint_truncated",
                                        "audio fingerprint exceeded the configured word limit",
                                        ToolName.FPCALC.value,
                                    ),
                                )
                            )
                        else:
                            audio = parsed_audio
                    except ValueError as error:
                        problems.append(
                            (
                                AnalysisState.PARTIAL_TOOL_ERROR,
                                AnalysisIssue("invalid_audio_output", str(error), ToolName.FPCALC.value),
                            )
                        )
                else:
                    problems.append(_command_problem(audio_outcome, ToolName.FPCALC, "audio_fingerprint_incomplete"))

        try:
            final_path, final_size, final_mtime_ns, final_generation_token = resolve_source_snapshot(source.path)
        except (OSError, ValueError) as error:
            return self._artifact(
                source,
                metadata,
                (),
                None,
                AnalysisState.FAILED,
                (AnalysisIssue("source_changed", str(error)),),
                tool_versions,
            )
        if (final_path, final_size, final_mtime_ns) != (
            source.path,
            source.size,
            source.mtime_ns,
        ) or final_generation_token != generation_token:
            return self._artifact(
                source,
                metadata,
                (),
                None,
                AnalysisState.FAILED,
                (AnalysisIssue("source_changed", "source identity changed while fingerprints were computed"),),
                tool_versions,
            )
        if not frames and not problems:
            problems.append(
                (
                    AnalysisState.PARTIAL_TOOL_ERROR,
                    AnalysisIssue("no_frames", "FFmpeg produced no usable video frame", ToolName.FFMPEG.value),
                )
            )
        state = AnalysisState.COMPLETE if not problems else _combined_state(problems)
        return self._artifact(
            source,
            metadata,
            tuple(frames),
            audio,
            state,
            tuple(problem[1] for problem in problems),
            tool_versions,
        )

    @staticmethod
    def _artifact(
        source: SourceSnapshot,
        metadata: Optional[VideoMetadata],
        frames: Tuple[FrameFingerprint, ...],
        audio: Optional[AudioFingerprint],
        state: AnalysisState,
        issues: Tuple[AnalysisIssue, ...],
        tool_versions: Tuple[Tuple[str, str], ...],
    ) -> VideoArtifact:
        return VideoArtifact(source, metadata, frames, audio, state, issues, tool_versions)


def _capability_problem(
    capability: ToolCapability,
    *,
    essential: bool,
) -> Tuple[AnalysisState, AnalysisIssue]:
    if capability.state is ToolState.MISSING:
        state = AnalysisState.PARTIAL_MISSING_TOOL
    elif capability.state is ToolState.TIMED_OUT:
        state = AnalysisState.PARTIAL_TIMEOUT
    elif capability.state is ToolState.CANCELLED:
        state = AnalysisState.PARTIAL_CANCELLED
    else:
        state = AnalysisState.FAILED if essential else AnalysisState.PARTIAL_TOOL_ERROR
    return state, AnalysisIssue(
        "tool_{}".format(capability.state.value),
        capability.message,
        capability.tool.value,
    )


def _command_problem(
    outcome: CommandOutcome,
    tool: ToolName,
    fallback_code: str,
) -> Tuple[AnalysisState, AnalysisIssue]:
    if outcome.state is CommandState.MISSING_EXECUTABLE:
        state = AnalysisState.PARTIAL_MISSING_TOOL
    elif outcome.state is CommandState.TIMED_OUT:
        state = AnalysisState.PARTIAL_TIMEOUT
    elif outcome.state is CommandState.CANCELLED:
        state = AnalysisState.PARTIAL_CANCELLED
    elif outcome.state is CommandState.OUTPUT_LIMIT:
        state = AnalysisState.PARTIAL_RESOURCE_LIMIT
    else:
        state = AnalysisState.PARTIAL_TOOL_ERROR
    message = outcome.error
    if not message and outcome.stderr:
        message = outcome.stderr.decode("utf-8", errors="replace").strip()
        if len(message) > 2048:
            message = message[:2048] + "\N{HORIZONTAL ELLIPSIS}"
    return state, AnalysisIssue(fallback_code, message or outcome.state.value, tool.value)


def _combined_state(problems: Sequence[Tuple[AnalysisState, AnalysisIssue]]) -> AnalysisState:
    priorities: Dict[AnalysisState, int] = {
        AnalysisState.COMPLETE: 0,
        AnalysisState.PARTIAL_MISSING_TOOL: 1,
        AnalysisState.PARTIAL_TOOL_ERROR: 2,
        AnalysisState.PARTIAL_RESOURCE_LIMIT: 3,
        AnalysisState.PARTIAL_TIMEOUT: 4,
        AnalysisState.PARTIAL_CANCELLED: 5,
        AnalysisState.FAILED: 6,
    }
    if not problems:
        return AnalysisState.COMPLETE
    return max((problem[0] for problem in problems), key=priorities.__getitem__)


def _has_terminal_problem(problems: Sequence[Tuple[AnalysisState, AnalysisIssue]]) -> bool:
    return any(
        state
        in {
            AnalysisState.PARTIAL_CANCELLED,
            AnalysisState.PARTIAL_TIMEOUT,
            AnalysisState.PARTIAL_RESOURCE_LIMIT,
            AnalysisState.FAILED,
        }
        for state, _issue in problems
    )
