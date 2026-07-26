# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import io
import json

from core.cli import ExitCode, main
from core.services.video import VideoService, comparison_report
from core.video import (
    AnalysisState,
    FrameFingerprint,
    FramePlanPolicy,
    SourceSnapshot,
    ToolCapability,
    ToolName,
    ToolState,
    VideoArtifact,
    VideoMetadata,
    build_frame_plan,
)
from core.video.tools import (
    FakeCommandRunner,
    ffmpeg_frame_command,
    ffmpeg_scene_command,
    ffprobe_command,
)


def _capabilities(*, ffprobe=True, ffmpeg=True, fpcalc=True):
    states = {
        ToolName.FFPROBE: ffprobe,
        ToolName.FFMPEG: ffmpeg,
        ToolName.FPCALC: fpcalc,
    }
    executables = {
        ToolName.FFPROBE: "probe",
        ToolName.FFMPEG: "mpeg",
        ToolName.FPCALC: "calc",
    }
    return tuple(
        ToolCapability(
            tool=tool,
            state=ToolState.AVAILABLE if states[tool] else ToolState.MISSING,
            executable=executables[tool] if states[tool] else None,
            version="{} test".format(tool.value) if states[tool] else None,
            message="available" if states[tool] else "not installed",
        )
        for tool in ToolName
    )


def _probe_payload():
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 64,
                    "height": 64,
                    "avg_frame_rate": "30/1",
                    "duration": "10",
                }
            ],
            "format": {"duration": "10", "format_name": "mp4"},
        }
    ).encode()


def _configured_service(paths):
    policy = FramePlanPolicy(
        normalized_frames=3,
        maximum_frames=3,
        minimum_separation_seconds=0,
    )
    metadata = VideoMetadata(10, 64, 64, 30, "h264", "yuv420p")
    plan = build_frame_plan(metadata, (), policy)
    runner = FakeCommandRunner()
    for path in paths:
        resolved = str(path.resolve())
        runner.add(ffprobe_command("probe", resolved), stdout=_probe_payload())
        runner.add(
            ffmpeg_scene_command("mpeg", resolved, policy.scene_threshold),
            stderr=b"",
        )
        for index, request in enumerate(plan.requests):
            pixels = bytes((pixel + index * 17) % 256 for pixel in range(32 * 32))
            runner.add(
                ffmpeg_frame_command("mpeg", resolved, request.timestamp_seconds),
                stdout=pixels,
            )
    return VideoService(
        runner=runner,
        capabilities=_capabilities(),
        frame_policy=policy,
    )


def _artifact(path, values):
    metadata = VideoMetadata(10, 64, 64, 30, "h264", "yuv420p")
    frames = tuple(
        FrameFingerprint(
            timestamp_seconds=index + 1,
            normalized_position=(index + 1) / (len(values) + 1),
            value=value,
        )
        for index, value in enumerate(values)
    )
    return VideoArtifact(
        SourceSnapshot(path, 100, 123),
        metadata,
        frames,
        None,
        AnalysisState.COMPLETE,
    )


def test_video_capabilities_missing_ffmpeg_is_valid_partial_json():
    stdout = io.StringIO()
    stderr = io.StringIO()
    service = VideoService(
        runner=FakeCommandRunner(),
        capabilities=_capabilities(ffmpeg=False, fpcalc=False),
    )

    exit_code = main(
        ["video", "capabilities"],
        stdout=stdout,
        stderr=stderr,
        video_service=service,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.VIDEO_PARTIAL
    assert payload["schema"] == "dupeguru.video-capabilities"
    assert payload["schema_version"] == 1
    assert payload["state"] == "partial"
    assert payload["partial"] is True
    assert payload["summary"]["visual_analysis_available"] is False
    assert {issue["tool"] for issue in payload["issues"]} == {"ffmpeg", "fpcalc"}
    assert stderr.getvalue() == ""


def test_video_analyze_missing_ffmpeg_is_partial_not_internal_error(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    runner = FakeCommandRunner()
    runner.add(ffprobe_command("probe", str(source.resolve())), stdout=_probe_payload())
    service = VideoService(
        runner=runner,
        capabilities=_capabilities(ffmpeg=False),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["video", "analyze", str(source)],
        stdout=stdout,
        stderr=stderr,
        video_service=service,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.VIDEO_PARTIAL
    assert payload["schema"] == "dupeguru.video-analysis"
    assert payload["state"] == "partial_missing_tool"
    assert payload["partial"] is True
    assert payload["summary"]["comparable"] is False
    assert payload["issues"][0]["tool"] == "ffmpeg"
    assert stderr.getvalue() == ""


def test_video_analyze_artifact_cache_round_trip_and_stale_rejection(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    cache = tmp_path / "source.video-artifact.json"
    stdout = io.StringIO()

    created_exit = main(
        ["video", "analyze", str(source), "--artifact-out", str(cache)],
        stdout=stdout,
        stderr=io.StringIO(),
        video_service=_configured_service((source,)),
    )

    created = json.loads(stdout.getvalue())
    assert created_exit == ExitCode.OK
    assert created["state"] == "complete"
    assert created["artifact_source"] == "analyzed"
    assert cache.is_file()
    assert json.loads(cache.read_text(encoding="utf-8"))["schema_version"] == 1

    cached_stdout = io.StringIO()
    cached_exit = main(
        ["video", "analyze", str(source), "--artifact-in", str(cache)],
        stdout=cached_stdout,
        stderr=io.StringIO(),
        video_service=VideoService(runner=FakeCommandRunner()),
    )
    cached = json.loads(cached_stdout.getvalue())
    assert cached_exit == ExitCode.OK
    assert cached["artifact_source"] == "cache"
    assert cached["artifact"] == created["artifact"]

    source.write_bytes(b"FAKE VIDEO")
    stale_stdout = io.StringIO()
    stale_stderr = io.StringIO()
    stale_exit = main(
        ["video", "analyze", str(source), "--artifact-in", str(cache)],
        stdout=stale_stdout,
        stderr=stale_stderr,
        video_service=VideoService(runner=FakeCommandRunner()),
    )
    assert stale_exit == ExitCode.INPUT_ERROR
    assert stale_stdout.getvalue() == ""
    assert "does not match" in stale_stderr.getvalue()


def test_video_artifact_output_never_replaces_existing_cache(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    cache = tmp_path / "artifact.json"
    cache.write_text("do not replace", encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["video", "analyze", str(source), "--artifact-out", str(cache)],
        stdout=stdout,
        stderr=stderr,
        video_service=_configured_service((source,)),
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert cache.read_text(encoding="utf-8") == "do not replace"
    assert "already exists" in stderr.getvalue()


def test_video_compare_identical_perceptual_content_never_claims_exact(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"same fake video bytes")
    second.write_bytes(b"same fake video bytes")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "video",
            "compare",
            str(first),
            str(second),
            "--threshold",
            "0.8",
        ],
        stdout=stdout,
        stderr=stderr,
        video_service=_configured_service((first, second)),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert payload["schema"] == "dupeguru.video-comparison"
    assert payload["state"] == "complete"
    assert payload["partial"] is False
    assert payload["threshold"] == 0.8
    assert payload["relation"]["relation"] == "near"
    assert payload["relation"]["exact_proof"] is None
    assert payload["byte_exact_proof"] is None
    assert payload["allows_automatic_destructive_action"] is False
    assert stderr.getvalue() == ""


def test_video_comparison_threshold_suppresses_lower_scored_relation():
    first = _artifact("first.mp4", (0, 0, 0))
    second = _artifact("second.mp4", (3, 3, 3))

    accepted = comparison_report(first, second, threshold=0.8)
    rejected = comparison_report(first, second, threshold=0.9)

    assert accepted["relation"] is not None
    assert accepted["relation"]["score"] < 0.9
    assert rejected["relation"] is None
    assert rejected["summary"]["relation_found"] is False


def test_video_compare_rejects_non_finite_threshold_before_output():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["video", "compare", "first.mp4", "second.mp4", "--threshold", "nan"],
        stdout=stdout,
        stderr=stderr,
        video_service=VideoService(
            runner=FakeCommandRunner(),
            capabilities=_capabilities(),
        ),
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "threshold" in stderr.getvalue()


def test_video_unavailable_source_is_failed_json_not_internal_error(tmp_path):
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["video", "analyze", str(tmp_path / "missing.mp4")],
        stdout=stdout,
        stderr=stderr,
        video_service=VideoService(
            runner=FakeCommandRunner(),
            capabilities=_capabilities(),
        ),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.INPUT_ERROR
    assert payload["state"] == "failed"
    assert payload["partial"] is False
    assert payload["issues"][0]["code"] == "source_unavailable"
    assert stderr.getvalue() == ""


def test_video_schemas_are_exposed_and_comparison_schema_forbids_exact():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["schema", "video-comparison"],
        stdout=stdout,
        stderr=stderr,
    )

    schema = json.loads(stdout.getvalue())
    relation = schema["properties"]["relation"]["properties"]
    assert exit_code == ExitCode.OK
    assert schema["$id"] == "urn:dupeguru-neo:schema:video-comparison:1"
    assert "exact" not in relation["relation"]["enum"]
    assert relation["exact_proof"]["const"] is None
    assert schema["properties"]["byte_exact_proof"]["const"] is None
    assert stderr.getvalue() == ""
