# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import io
import json
import os

import pytest

from core.cli import ExitCode, main
from core.safe_walk import WalkEvent, WalkEventKind
from core.services.jsonio import iter_video_library_jsonl
from core.services.schemas import get_schema
from core.services.video import VideoService
from core.video import (
    FramePlanPolicy,
    ToolCapability,
    ToolName,
    ToolState,
    VideoLibraryLimits,
    VideoLibraryScanner,
    VideoMetadata,
    build_frame_plan,
)
from core.video.tools import (
    FakeCommandRunner,
    ffmpeg_frame_command,
    ffmpeg_scene_command,
    ffprobe_command,
)
from core.video.library import _compatible_bucket_pairs

_RESERVED_VIDEO_SCAN_DIRECTORIES = (
    ".dupeguru-neo-quarantine",
    ".dupeguru-neo-dataset-executor",
    ".dupeguru-neo-dataset-quarantine",
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


def _probe_payload(*, audio=False):
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
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "10",
            }
        )
    return json.dumps(
        {
            "streams": streams,
            "format": {
                "duration": "10",
                "format_name": "mp4",
            },
        }
    ).encode()


def _configured_service(paths, *, audio=False, fpcalc=True, runner=None):
    policy = FramePlanPolicy(
        normalized_frames=3,
        maximum_frames=3,
        minimum_separation_seconds=0,
    )
    metadata = VideoMetadata(
        10,
        64,
        64,
        30,
        "h264",
        "yuv420p",
        "aac" if audio else "",
        10 if audio else None,
    )
    plan = build_frame_plan(metadata, (), policy)
    command_runner = runner or FakeCommandRunner()
    for path in paths:
        resolved = str(path.resolve())
        command_runner.add(
            ffprobe_command("probe", resolved),
            stdout=_probe_payload(audio=audio),
        )
        command_runner.add(
            ffmpeg_scene_command("mpeg", resolved, policy.scene_threshold),
            stderr=b"",
        )
        for index, request in enumerate(plan.requests):
            pixels = bytes((pixel + index * 17) % 256 for pixel in range(32 * 32))
            command_runner.add(
                ffmpeg_frame_command(
                    "mpeg",
                    resolved,
                    request.timestamp_seconds,
                ),
                stdout=pixels,
            )
    return (
        VideoService(
            runner=command_runner,
            capabilities=_capabilities(fpcalc=fpcalc),
            frame_policy=policy,
        ),
        command_runner,
    )


def _limits(**overrides):
    values = {
        "maximum_files": 20,
        "maximum_candidate_assessments": 100,
        "maximum_candidates": 50,
        "maximum_comparisons": 20,
        "maximum_fingerprint_files": 20,
        "maximum_groups": 20,
        "probe_timeout_seconds": 5,
        "maximum_probe_output_bytes": 1024 * 1024,
        "maximum_scan_seconds": 60,
    }
    values.update(overrides)
    return VideoLibraryLimits(**values)


def _make_library(tmp_path, count=2):
    root = tmp_path / "library"
    root.mkdir()
    paths = []
    for index in range(count):
        path = root / "video-{}.mp4".format(index)
        path.write_bytes(("different bytes {}".format(index)).encode())
        paths.append(path)
    (root / "ignored.txt").write_text("not a video", encoding="utf-8")
    return root, tuple(paths)


def test_video_library_scan_groups_perceptual_matches_without_exact_proof(tmp_path):
    root, paths = _make_library(tmp_path)
    cache = tmp_path / "video-cache.sqlite3"
    service, _runner = _configured_service(paths)
    before = {path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns) for path in paths}

    report = service.scan(
        (root,),
        cache_path=cache,
        threshold=0.8,
        limits=_limits(),
    )

    assert report["schema"] == "dupeguru.video-library-scan"
    assert report["state"] == "complete"
    assert report["partial"] is False
    assert report["receipt"] == {
        "status": "complete",
        "complete": True,
        "discovered": 2,
        "analyzed": 2,
        "skipped": 0,
        "failed": 0,
    }
    assert report["summary"]["video_files"] == 2
    assert report["summary"]["comparisons"] == 1
    assert report["summary"]["groups"] == 1
    assert report["safety"] == {
        "source_read_only": True,
        "review_only": True,
        "byte_exact_proof": False,
        "destructive_actions_allowed": False,
    }
    group = report["groups"][0]
    assert group["review_only"] is True
    assert group["byte_exact_proof"] is None
    assert group["allows_automatic_destructive_action"] is False
    assert {member["path"] for member in group["members"]} == {str(path.resolve()) for path in paths}
    assert group["relations"][0]["relation"] == "near"
    assert group["relations"][0]["exact_proof"] is None
    assert group["relations"][0]["allows_automatic_destructive_action"] is False
    assert all(relation["relation"] != "exact" for relation in group["relations"])
    assert cache.is_file()
    assert {path: (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns) for path in paths} == before


def test_video_library_persistent_cache_avoids_all_tool_calls_on_warm_scan(tmp_path):
    root, paths = _make_library(tmp_path)
    cache = tmp_path / "video-cache.sqlite3"
    first_service, _first_runner = _configured_service(paths)
    first = first_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )
    empty_runner = FakeCommandRunner()
    second_service, _ = _configured_service(
        (),
        runner=empty_runner,
    )

    second = second_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )

    assert first["state"] == second["state"] == "complete"
    assert first["groups"][0]["group_id"] == second["groups"][0]["group_id"]
    assert second["cache"]["hits"] == 4
    assert second["cache"]["misses"] == 0
    assert second["cache"]["writes"] == 0
    assert empty_runner.calls == []


def test_video_library_cache_is_scoped_to_the_analysis_policy(tmp_path):
    root, paths = _make_library(tmp_path)
    cache = tmp_path / "video-cache.sqlite3"
    first_service, _first_runner = _configured_service(paths)
    first_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )
    empty_runner = FakeCommandRunner()
    changed_policy = FramePlanPolicy(
        normalized_frames=2,
        maximum_frames=2,
        minimum_separation_seconds=0,
    )
    second_service = VideoService(
        runner=empty_runner,
        capabilities=_capabilities(),
        frame_policy=changed_policy,
    )

    report = second_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )

    assert report["state"] == "partial_tool_error"
    assert report["cache"]["hits"] == 0
    assert report["cache"]["misses"] == 2
    assert report["groups"] == []
    assert len(empty_runner.calls) == 2
    assert all(call[0][0] == "probe" for call in empty_runner.calls)


def test_video_library_cache_rejects_same_path_size_mtime_with_new_file_identity(
    tmp_path,
):
    root, paths = _make_library(tmp_path)
    cache = tmp_path / "video-cache.sqlite3"
    first_service, _first_runner = _configured_service(paths)
    first_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )
    replaced = paths[0]
    original_stat = replaced.stat()
    original = root / "replaced-original.not-video"
    replaced.rename(original)
    replaced.write_bytes(b"replacement bytes")
    assert replaced.stat().st_size == original_stat.st_size
    os.utime(
        replaced,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert replaced.stat().st_mtime_ns == original_stat.st_mtime_ns
    empty_runner = FakeCommandRunner()
    second_service, _ = _configured_service(
        (),
        runner=empty_runner,
    )

    report = second_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )

    assert report["state"] == "partial_tool_error"
    assert report["cache"]["hits"] == 1
    assert report["cache"]["misses"] == 1
    assert report["groups"] == []
    assert len(empty_runner.calls) == 1
    assert empty_runner.calls[0][0] == ffprobe_command(
        "probe",
        str(replaced.resolve()),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime integration")
def test_video_library_cache_rejects_in_place_edit_with_restored_mtime(tmp_path):
    root, paths = _make_library(tmp_path)
    cache = tmp_path / "video-cache.sqlite3"
    first_service, _first_runner = _configured_service(paths)
    first_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )
    changed = paths[0]
    original_stat = os.stat(changed, follow_symlinks=False)
    content = bytearray(changed.read_bytes())
    content[0] ^= 0x01
    changed.write_bytes(content)
    os.utime(changed, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    current_stat = os.stat(changed, follow_symlinks=False)
    assert current_stat.st_size == original_stat.st_size
    assert current_stat.st_mtime_ns == original_stat.st_mtime_ns
    empty_runner = FakeCommandRunner()
    second_service, _ = _configured_service((), runner=empty_runner)

    report = second_service.scan(
        (root,),
        cache_path=cache,
        limits=_limits(),
    )

    assert report["cache"]["hits"] == 1
    assert report["cache"]["misses"] == 1
    assert len(empty_runner.calls) == 1
    assert empty_runner.calls[0][0] == ffprobe_command(
        "probe",
        str(changed.resolve()),
    )


def test_video_library_candidate_limit_is_explicit_partial_receipt(tmp_path):
    root, paths = _make_library(tmp_path, count=3)
    service, _runner = _configured_service(paths)

    report = service.scan(
        (root,),
        threshold=0.8,
        limits=_limits(
            maximum_candidates=1,
            maximum_comparisons=1,
            maximum_fingerprint_files=2,
        ),
    )

    assert report["state"] == "partial_resource_limit"
    assert report["partial"] is True
    assert report["receipt"]["status"] == "resource_limit"
    assert report["receipt"]["complete"] is False
    assert report["summary"]["candidates"] == 1
    assert report["summary"]["comparisons"] == 1
    assert len(report["groups"]) == 1
    assert any(issue["code"] == "candidate_limit" for issue in report["issues"])
    assert report["safety"]["destructive_actions_allowed"] is False


def test_sparse_10000_bucket_neighborhood_lookup_is_linear():
    class CountingBuckets(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.lookups = 0

        def get(self, key, default=None):
            self.lookups += 1
            return super().get(key, default)

    bucket_count = 10_000
    duration_delta = 12
    buckets = CountingBuckets({(index * 3, 0): ("video-{}.mp4".format(index),) for index in range(bucket_count)})

    pairs = tuple(
        _compatible_bucket_pairs(
            buckets,
            duration_delta,
        )
    )

    assert len(pairs) == bucket_count
    assert all(left == right for left, right in pairs)
    assert buckets.lookups <= (bucket_count * 3 * (2 * duration_delta + 1))


def test_video_library_missing_ffprobe_is_partial_and_never_claims_coverage(tmp_path):
    root, paths = _make_library(tmp_path, count=1)
    runner = FakeCommandRunner()
    service = VideoService(
        runner=runner,
        capabilities=_capabilities(ffprobe=False),
    )

    report = service.scan((root,), limits=_limits())

    assert report["state"] == "partial_missing_tool"
    assert report["receipt"]["status"] == "complete_with_skips"
    assert report["receipt"]["complete"] is False
    assert report["receipt"]["discovered"] == 1
    assert report["receipt"]["analyzed"] == 0
    assert report["receipt"]["failed"] == 1
    assert report["groups"] == []
    assert report["issues"] == [
        {
            "code": "tool_missing",
            "message": "not installed",
            "tool": "ffprobe",
            "source": None,
        }
    ]
    assert paths[0].is_file()
    assert runner.calls == []


def test_video_library_rejects_unrepresentable_probe_metadata_as_partial(tmp_path):
    root, paths = _make_library(tmp_path, count=1)
    runner = FakeCommandRunner()
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 10**1000,
                    "height": 1,
                    "avg_frame_rate": "30/1",
                    "duration": "10",
                }
            ],
            "format": {
                "duration": "10",
                "format_name": "mp4",
            },
        }
    ).encode()
    runner.add(
        ffprobe_command("probe", str(paths[0].resolve())),
        stdout=payload,
    )
    service = VideoService(
        runner=runner,
        capabilities=_capabilities(),
    )

    report = service.scan((root,), limits=_limits())

    assert report["state"] == "partial_tool_error"
    assert report["receipt"]["complete"] is False
    assert report["receipt"]["failed"] == 1
    assert report["groups"] == []
    assert report["issues"][0]["code"] == "metadata_probe_invalid"


def test_video_library_still_uses_visual_evidence_when_optional_audio_tool_is_missing(
    tmp_path,
):
    root, paths = _make_library(tmp_path)
    service, _runner = _configured_service(
        paths,
        audio=True,
        fpcalc=False,
    )

    report = service.scan(
        (root,),
        threshold=0.8,
        limits=_limits(),
    )

    assert report["state"] == "partial_missing_tool"
    assert report["receipt"]["complete"] is False
    assert len(report["groups"]) == 1
    assert report["groups"][0]["relations"][0]["relation"] == "near"
    assert any(issue["tool"] == "fpcalc" for issue in report["issues"])


def test_video_library_rejects_a_cache_path_inside_a_source_root(tmp_path):
    root, paths = _make_library(tmp_path)
    service, runner = _configured_service(paths)
    cache = root / "unsafe.sqlite3"

    with pytest.raises(ValueError, match="outside every input root"):
        service.scan(
            (root,),
            cache_path=cache,
            limits=_limits(),
        )

    assert not cache.exists()
    assert runner.calls == []


def test_video_library_rejects_cache_hardlink_alias_of_an_input_file(tmp_path):
    root, paths = _make_library(tmp_path, count=1)
    service, runner = _configured_service(paths)
    cache_alias = tmp_path / "cache-alias.sqlite3"
    os.link(paths[0], cache_alias)
    original = paths[0].read_bytes()

    with pytest.raises(ValueError, match="aliases a file inside an input root"):
        service.scan(
            (root,),
            cache_path=cache_alias,
            limits=_limits(),
        )

    assert paths[0].read_bytes() == original
    assert cache_alias.read_bytes() == original
    assert runner.calls == []


def test_video_library_reports_safe_walk_skips_as_incomplete(tmp_path):
    root = tmp_path / "library"
    root.mkdir()

    def walker(_root, **_kwargs):
        yield WalkEvent(
            WalkEventKind.SYMLINK_SKIPPED,
            root / "external-link",
            detail="test link was not followed",
        )

    scanner = VideoLibraryScanner(
        runner=FakeCommandRunner(),
        capabilities=_capabilities(),
        walker=walker,
    )

    report = scanner.scan((root,), limits=_limits())

    assert report["state"] == "partial_tool_error"
    assert report["receipt"]["complete"] is False
    assert report["receipt"]["skipped"] == 1
    assert report["summary"]["video_files"] == 0
    assert report["issues"][0]["code"] == "walk_symlink_skipped"


def test_video_library_prunes_all_internal_quarantine_payloads(tmp_path):
    root, paths = _make_library(tmp_path)
    payloads = []
    for name in _RESERVED_VIDEO_SCAN_DIRECTORIES:
        reserved = root / name
        reserved.mkdir()
        payload = reserved / "quarantined-video.mp4"
        payload.write_bytes(b"internal payload")
        payloads.append(payload)
    service, runner = _configured_service(paths)

    report = service.scan(
        (root,),
        threshold=0.8,
        limits=_limits(),
    )

    assert report["state"] == "complete"
    assert report["receipt"]["complete"] is True
    assert report["summary"]["video_files"] == 2
    assert report["summary"]["metadata_complete"] == 2
    assert len(report["groups"]) == 1
    assert all(payload.is_file() for payload in payloads)
    assert all(
        reserved_name not in argument
        for command, _timeout, _output_limit in runner.calls
        for argument in command
        for reserved_name in _RESERVED_VIDEO_SCAN_DIRECTORIES
    )


@pytest.mark.parametrize("reserved_name", _RESERVED_VIDEO_SCAN_DIRECTORIES)
def test_video_library_rejects_reserved_internal_directory_as_root(
    tmp_path,
    reserved_name,
):
    root = tmp_path / reserved_name
    root.mkdir()
    source = root / "quarantined-video.mp4"
    source.write_bytes(b"internal payload")
    service, runner = _configured_service((source,))

    with pytest.raises(ValueError, match="reserved internal directory"):
        service.scan(
            (root,),
            limits=_limits(),
        )

    assert source.read_bytes() == b"internal payload"
    assert runner.calls == []


def test_video_library_jsonl_emits_groups_and_partial_receipt_records(tmp_path):
    root, paths = _make_library(tmp_path, count=3)
    service, _runner = _configured_service(paths)
    report = service.scan(
        (root,),
        threshold=0.8,
        limits=_limits(
            maximum_candidates=1,
            maximum_comparisons=1,
            maximum_fingerprint_files=2,
        ),
    )

    records = [json.loads(line) for line in iter_video_library_jsonl(report)]

    assert [record["record_type"] for record in records] == [
        "header",
        "group",
        "issue",
        "receipt",
        "summary",
    ]
    assert all(
        record["schema"] == "dupeguru.video-library-record"
        and record["document_schema"] == "dupeguru.video-library-scan"
        and record["scan_id"] == report["scan_id"]
        for record in records
    )
    assert records[1]["group"]["review_only"] is True
    assert records[-2]["receipt"]["complete"] is False
    assert records[-2]["receipt"]["status"] == "resource_limit"
    payload_names = {"header", "group", "issue", "receipt", "summary"}
    for record in records:
        assert {name for name in payload_names if record[name] is not None} == {record["record_type"]}


def test_video_scan_cli_defaults_to_jsonl_and_returns_partial_exit(tmp_path):
    root, paths = _make_library(tmp_path, count=3)
    service, _runner = _configured_service(paths)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "video",
            "scan",
            str(root),
            "--max-candidates",
            "1",
            "--max-comparisons",
            "1",
            "--threshold",
            "0.8",
            "--quiet",
        ],
        stdout=stdout,
        stderr=stderr,
        video_service=service,
    )

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    receipt = next(record["receipt"] for record in records if record["record_type"] == "receipt")
    assert exit_code == ExitCode.VIDEO_PARTIAL
    assert records[0]["record_type"] == "header"
    assert any(record["record_type"] == "group" for record in records)
    assert receipt["status"] == "resource_limit"
    assert receipt["complete"] is False
    assert stderr.getvalue() == ""


def test_video_scan_cli_rejects_pretty_jsonl_before_starting_scan(tmp_path):
    class UnexpectedVideoService:
        def scan(self, *_args, **_kwargs):
            raise AssertionError("scan must not start for an invalid output request")

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "video",
            "scan",
            str(tmp_path),
            "--pretty",
        ],
        stdout=stdout,
        stderr=stderr,
        video_service=UnexpectedVideoService(),
    )

    assert exit_code == ExitCode.INPUT_ERROR
    assert stdout.getvalue() == ""
    assert "--pretty requires --format json" in stderr.getvalue()


@pytest.mark.parametrize(
    "name,expected_id",
    [
        (
            "video-library-scan",
            "urn:dupeguru-neo:schema:video-library-scan:1",
        ),
        (
            "video-library-group",
            "urn:dupeguru-neo:schema:video-library-group:1",
        ),
        (
            "video-library-record",
            "urn:dupeguru-neo:schema:video-library-record:1",
        ),
    ],
)
def test_video_library_schemas_are_exposed_by_cli(name, expected_id):
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["schema", name],
        stdout=stdout,
        stderr=stderr,
    )

    schema = json.loads(stdout.getvalue())
    assert exit_code == ExitCode.OK
    assert schema["$id"] == expected_id
    assert stderr.getvalue() == ""


def test_video_library_schemas_forbid_exact_or_destructive_group_evidence():
    group = get_schema("dupeguru.video-library-group")
    relation = group["properties"]["relations"]["items"]["properties"]
    scan = get_schema("dupeguru.video-library-scan")

    assert "exact" not in relation["relation"]["enum"]
    assert relation["exact_proof"]["const"] is None
    assert relation["allows_automatic_destructive_action"]["const"] is False
    assert group["properties"]["byte_exact_proof"]["const"] is None
    assert group["properties"]["allows_automatic_destructive_action"]["const"] is False
    assert scan["properties"]["safety"]["properties"]["destructive_actions_allowed"]["const"] is False
