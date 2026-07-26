# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import shutil

import pytest

from core.video.analyzer import AnalysisLimits, VideoAnalyzer
from core.video.fingerprint import FramePlanPolicy
from core.video.model import AnalysisState
from core.video.tools import CommandState, SubprocessCommandRunner, ToolName


def test_local_ffmpeg_smoke_when_available(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("local FFmpeg tools are not installed")
    video = tmp_path / "sample.mkv"
    runner = SubprocessCommandRunner()
    generated = runner.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:r=10",
            "-t",
            "1",
            "-c:v",
            "ffv1",
            str(video),
        ),
        timeout_seconds=30,
    )
    assert generated.state is CommandState.SUCCESS, generated.stderr.decode(errors="replace")
    analyzer = VideoAnalyzer(
        runner=runner,
        executables={ToolName.FFMPEG: ffmpeg, ToolName.FFPROBE: ffprobe},
        limits=AnalysisLimits(total_timeout_seconds=60),
        frame_policy=FramePlanPolicy(normalized_frames=2, maximum_frames=3),
    )
    artifact = analyzer.analyze(video)
    assert artifact.state is AnalysisState.COMPLETE
    assert len(artifact.frames) >= 2
