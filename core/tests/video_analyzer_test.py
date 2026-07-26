# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import json
import threading

import pytest

from core.video.analyzer import AnalysisLimits, VideoAnalyzer
from core.video.fingerprint import FramePlanPolicy
from core.video.model import AnalysisState
from core.video.tools import (
    CommandState,
    FakeCommandRunner,
    ToolCapability,
    ToolName,
    ToolState,
    ffmpeg_frame_command,
    ffmpeg_scene_command,
    ffprobe_command,
    fpcalc_command,
)


def capabilities(*, fpcalc=True, ffmpeg=True):
    return (
        ToolCapability(
            ToolName.FFPROBE,
            ToolState.AVAILABLE,
            "probe",
            "ffprobe test",
            "available",
        ),
        ToolCapability(
            ToolName.FFMPEG,
            ToolState.AVAILABLE if ffmpeg else ToolState.MISSING,
            "mpeg" if ffmpeg else None,
            "ffmpeg test" if ffmpeg else None,
            "available" if ffmpeg else "not installed",
        ),
        ToolCapability(
            ToolName.FPCALC,
            ToolState.AVAILABLE if fpcalc else ToolState.MISSING,
            "calc" if fpcalc else None,
            "fpcalc test" if fpcalc else None,
            "available" if fpcalc else "not installed",
        ),
    )


def probe_json(*, audio=False):
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "width": 64,
            "height": 64,
            "avg_frame_rate": "30/1",
            "duration": "10",
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "duration": "10"})
    return json.dumps({"streams": streams, "format": {"duration": "10", "format_name": "mp4"}}).encode()


def configured_analyzer(tmp_path, *, audio=False, fpcalc=True, limits=None):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    resolved = str(source.resolve())
    policy = FramePlanPolicy(
        normalized_frames=2,
        maximum_frames=2,
        minimum_separation_seconds=0,
    )
    runner = FakeCommandRunner()
    runner.add(ffprobe_command("probe", resolved), stdout=probe_json(audio=audio))
    runner.add(
        ffmpeg_scene_command("mpeg", resolved, policy.scene_threshold),
        stderr=b"",
    )
    metadata_duration = 10
    for index in range(policy.normalized_frames):
        position = policy.boundary_margin_fraction + (1 - 2 * policy.boundary_margin_fraction) * (index + 1) / (
            policy.normalized_frames + 1
        )
        runner.add(
            ffmpeg_frame_command("mpeg", resolved, metadata_duration * position),
            stdout=bytes([index * 30]) * (32 * 32),
        )
    if audio and fpcalc:
        runner.add(
            fpcalc_command("calc", resolved, maximum_seconds=(limits or AnalysisLimits()).maximum_audio_seconds),
            stdout=b'{"duration":10,"fingerprint":"1,2,3"}',
        )
    analyzer = VideoAnalyzer(
        runner=runner,
        capabilities=capabilities(fpcalc=fpcalc),
        limits=limits or AnalysisLimits(),
        frame_policy=policy,
    )
    return source, resolved, runner, analyzer


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("probe_timeout_seconds", float("nan")),
        ("audio_timeout_seconds", float("inf")),
        ("maximum_command_output_bytes", True),
    ),
)
def test_analysis_limits_reject_unbounded_or_wrong_typed_values(name, value):
    with pytest.raises(ValueError):
        AnalysisLimits(**{name: value})


def test_analyzer_produces_complete_artifact_with_fake_tools(tmp_path):
    source, _resolved, runner, analyzer = configured_analyzer(tmp_path, audio=True)
    artifact = analyzer.analyze(source)
    assert artifact.state is AnalysisState.COMPLETE
    assert len(artifact.frames) == 2
    assert artifact.audio is not None
    assert not artifact.issues
    assert all(call[0][0] in {"probe", "mpeg", "calc"} for call in runner.calls)


def test_missing_optional_fpcalc_is_explicit_partial_result(tmp_path):
    source, _resolved, _runner, analyzer = configured_analyzer(tmp_path, audio=True, fpcalc=False)
    artifact = analyzer.analyze(source)
    assert artifact.state is AnalysisState.PARTIAL_MISSING_TOOL
    assert artifact.comparable
    assert artifact.audio is None
    assert any(issue.tool == "fpcalc" for issue in artifact.issues)


def test_missing_ffmpeg_never_reports_success(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    resolved = str(source.resolve())
    runner = FakeCommandRunner()
    runner.add(ffprobe_command("probe", resolved), stdout=probe_json())
    analyzer = VideoAnalyzer(runner=runner, capabilities=capabilities(ffmpeg=False))
    artifact = analyzer.analyze(source)
    assert artifact.state is AnalysisState.PARTIAL_MISSING_TOOL
    assert not artifact.frames
    assert not artifact.comparable


def test_timeout_cancel_and_invalid_frame_are_explicit(tmp_path):
    source, resolved, runner, analyzer = configured_analyzer(tmp_path)
    probe_argv = ffprobe_command("probe", resolved)
    runner.add(
        probe_argv,
        state=CommandState.TIMED_OUT,
        returncode=None,
        error="probe timed out",
    )
    assert analyzer.analyze(source).state is AnalysisState.PARTIAL_TIMEOUT

    source, _resolved, _runner, analyzer = configured_analyzer(tmp_path)
    cancel = threading.Event()
    cancel.set()
    assert analyzer.analyze(source, cancel_event=cancel).state is AnalysisState.PARTIAL_CANCELLED

    source, resolved, runner, analyzer = configured_analyzer(tmp_path)
    policy = analyzer.frame_policy
    position = policy.boundary_margin_fraction + (1 - 2 * policy.boundary_margin_fraction) / (
        policy.normalized_frames + 1
    )
    runner.add(ffmpeg_frame_command("mpeg", resolved, 10 * position), stdout=b"too short")
    invalid = analyzer.analyze(source)
    assert invalid.state is AnalysisState.PARTIAL_TOOL_ERROR
    assert len(invalid.frames) == 1


def test_process_limit_is_reported_as_resource_partial(tmp_path):
    limits = AnalysisLimits(maximum_processes=2)
    source, _resolved, _runner, analyzer = configured_analyzer(tmp_path, limits=limits)
    artifact = analyzer.analyze(source)
    assert artifact.state is AnalysisState.PARTIAL_RESOURCE_LIMIT
    assert any("resource" in issue.message for issue in artifact.issues)


def test_unavailable_source_is_failed_artifact(tmp_path):
    artifact = VideoAnalyzer(runner=FakeCommandRunner(), capabilities=capabilities()).analyze(tmp_path / "missing.mp4")
    assert artifact.state is AnalysisState.FAILED
    assert artifact.issues[0].code == "source_unavailable"
