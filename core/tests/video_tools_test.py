# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import json
import sys
import threading
import time
from pathlib import Path

import pytest

import core.video.json_guard as video_json_guard
from core.video.tools import (
    FFPROBE_JSON_LIMITS,
    CommandState,
    FakeCommandRunner,
    SubprocessCommandRunner,
    ToolName,
    ToolState,
    detect_capabilities,
    capabilities_by_name,
    ffmpeg_frame_command,
    parse_ffprobe_json,
    parse_scene_times,
)


def ffprobe_payload(audio=True):
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "duration": "12.5",
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "duration": "12.4"})
    return json.dumps(
        {
            "streams": streams,
            "format": {"duration": "12.5", "bit_rate": "1200000", "format_name": "mov,mp4"},
        }
    ).encode()


def test_parse_ffprobe_metadata():
    metadata = parse_ffprobe_json(ffprobe_payload())
    assert metadata.duration_seconds == 12.5
    assert metadata.width == 1920
    assert metadata.frame_rate == pytest.approx(29.97002997)
    assert metadata.audio_codec == "aac"
    assert metadata.bit_rate == 1_200_000


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"streams":[],"format":{}}',
        b'{"streams":[{"codec_type":"video","width":1,"height":1}],"format":{"duration":"1"}}',
    ],
)
def test_parse_ffprobe_rejects_incomplete_documents(payload):
    with pytest.raises(ValueError):
        parse_ffprobe_json(payload)


def test_ffprobe_preflights_deep_json_before_decoder_allocation(monkeypatch):
    called = False

    def unexpected_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("decoder must not run")

    monkeypatch.setattr(video_json_guard.json, "loads", unexpected_decode)
    payload = "[" * (FFPROBE_JSON_LIMITS.max_depth + 1)

    with pytest.raises(ValueError, match="invalid JSON"):
        parse_ffprobe_json(payload)

    assert not called


@pytest.mark.parametrize(
    "payload",
    [
        b'{"streams":[],"format":{"duration":NaN}}',
        b'{"streams":[{"codec_type":"video","width":true,"height":1,'
        b'"avg_frame_rate":"1/1","duration":"1","codec_name":"h264"}]}',
    ],
)
def test_ffprobe_rejects_nonfinite_or_boolean_numeric_values(payload):
    with pytest.raises(ValueError):
        parse_ffprobe_json(payload)


@pytest.mark.parametrize(
    "width",
    (
        10**100,
        "9" * 1000,
    ),
)
def test_ffprobe_rejects_extreme_integer_dimensions_without_overflow(width):
    document = json.loads(ffprobe_payload())
    document["streams"][0]["width"] = width

    with pytest.raises(ValueError):
        parse_ffprobe_json(json.dumps(document))


def test_scene_parser_is_bounded_and_deterministic():
    stderr = b"showinfo pts_time:2.5 x\npts_time:1.0\npts_time:2.5\npts_time:99.0"
    assert parse_scene_times(stderr, 10) == (1.0, 2.5)


def test_commands_keep_untrusted_path_in_one_argument():
    path = 'C:\\videos\\name"; Remove-Item important.mp4'
    argv = ffmpeg_frame_command("ffmpeg", path, 1.25)
    assert argv.count(path) == 1
    assert argv[0] == "ffmpeg"
    assert isinstance(argv, tuple)


def test_fake_capability_detection_reports_each_tool_state():
    runner = FakeCommandRunner()
    runner.add(("probe", "-version"), stdout=b"ffprobe version 9\n")
    runner.add(
        ("mpeg", "-version"),
        state=CommandState.TIMED_OUT,
        returncode=None,
        error="too slow",
    )
    capabilities = detect_capabilities(
        runner,
        executables={ToolName.FFPROBE: "probe", ToolName.FFMPEG: "mpeg"},
        resolver=lambda _name: None,
    )
    by_tool = {item.tool: item for item in capabilities}
    assert by_tool[ToolName.FFPROBE].state is ToolState.AVAILABLE
    assert by_tool[ToolName.FFPROBE].version == "ffprobe version 9"
    assert by_tool[ToolName.FFMPEG].state is ToolState.TIMED_OUT
    assert by_tool[ToolName.FPCALC].state is ToolState.MISSING


def test_capability_report_rejects_duplicate_tool_entries():
    runner = FakeCommandRunner()
    runner.add(("probe", "-version"), stdout=b"ffprobe version 9")
    capabilities = detect_capabilities(
        runner,
        executables={ToolName.FFPROBE: "probe"},
        resolver=lambda _name: None,
    )
    with pytest.raises(ValueError, match="exactly once"):
        capabilities_by_name(capabilities + (capabilities[0],))


def test_subprocess_runner_does_not_interpret_shell_metacharacters():
    runner = SubprocessCommandRunner()
    argument = "literal;echo SHOULD_NOT_RUN"
    outcome = runner.run(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", argument),
        timeout_seconds=5,
    )
    assert outcome.state is CommandState.SUCCESS
    assert outcome.stdout.decode().strip() == argument


def test_subprocess_runner_timeout_cancel_and_output_limit():
    runner = SubprocessCommandRunner()
    timeout = runner.run(
        (sys.executable, "-c", "import time; time.sleep(2)"),
        timeout_seconds=0.05,
    )
    assert timeout.state is CommandState.TIMED_OUT

    cancelled_event = threading.Event()
    cancelled_event.set()
    cancelled = runner.run(
        (sys.executable, "-c", "print('no')"),
        timeout_seconds=5,
        cancel_event=cancelled_event,
    )
    assert cancelled.state is CommandState.CANCELLED

    limited = runner.run(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 20000)"),
        timeout_seconds=5,
        max_output_bytes=1000,
    )
    assert limited.state is CommandState.OUTPUT_LIMIT
    assert len(limited.stdout) + len(limited.stderr) <= 1000


def test_subprocess_runner_applies_one_combined_stdout_stderr_limit():
    outcome = SubprocessCommandRunner().run(
        (
            sys.executable,
            "-c",
            "import sys,time;"
            "sys.stdout.write('o'*800);sys.stdout.flush();"
            "sys.stderr.write('e'*800);sys.stderr.flush();"
            "time.sleep(2)",
        ),
        timeout_seconds=5,
        max_output_bytes=1000,
    )

    assert outcome.state is CommandState.OUTPUT_LIMIT
    assert len(outcome.stdout) + len(outcome.stderr) == 1000


def test_subprocess_runner_timeout_terminates_descendant_processes(tmp_path):
    marker = Path(tmp_path) / "orphan-marker.txt"
    child_code = (
        "import pathlib,sys,time;" "time.sleep(1);" "pathlib.Path(sys.argv[1]).write_text('orphan',encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
        "time.sleep(10)"
    )

    outcome = SubprocessCommandRunner().run(
        (
            sys.executable,
            "-c",
            parent_code,
            child_code,
            str(marker),
        ),
        timeout_seconds=0.2,
    )
    time.sleep(1.2)

    assert outcome.state is CommandState.TIMED_OUT
    assert not marker.exists()


def test_subprocess_runner_reports_missing_executable():
    outcome = SubprocessCommandRunner().run(
        ("definitely-no-such-dupeguru-video-tool-6fc7a9", "--version"),
        timeout_seconds=1,
    )
    assert outcome.state is CommandState.MISSING_EXECUTABLE
