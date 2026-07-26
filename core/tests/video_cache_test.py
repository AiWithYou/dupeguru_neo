# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import json

import pytest

from core.video import cache as cache_module
from core.video.cache import artifact_from_dict, artifact_from_json, artifact_to_dict, artifact_to_json
from core.video.model import (
    ARTIFACT_SCHEMA_VERSION,
    MAX_ARTIFACT_FRAMES,
    MAX_ARTIFACT_ISSUES,
    MAX_ARTIFACT_TOOL_VERSIONS,
    MAX_AUDIO_FINGERPRINT_WORDS,
    MAX_FRAME_FINGERPRINT_BITS,
    AnalysisState,
    AudioFingerprint,
    FrameFingerprint,
    SourceSnapshot,
    VideoArtifact,
    VideoMetadata,
)


def complete_artifact():
    return VideoArtifact(
        source=SourceSnapshot("library/video.mp4", 12345, 987654321),
        metadata=VideoMetadata(12.5, 1920, 1080, 30, "h264", "yuv420p", "aac", 12.4, 1_000_000, "mp4"),
        frames=(FrameFingerprint(2, 0.16, 123), FrameFingerprint(10, 0.8, 456)),
        audio=AudioFingerprint((1, -2, 3), 12.4),
        state=AnalysisState.COMPLETE,
        tool_versions=(("ffmpeg", "ffmpeg version 9"),),
    )


def test_cache_artifact_round_trip_and_stable_json():
    artifact = complete_artifact()
    payload = artifact_to_json(artifact)
    assert artifact_from_json(payload) == artifact
    assert artifact_to_json(artifact_from_json(payload)) == payload
    assert json.loads(payload)["schema_version"] == ARTIFACT_SCHEMA_VERSION


def test_cache_rejects_unknown_schema_without_silent_fallback():
    document = artifact_to_dict(complete_artifact())
    document["schema_version"] = 999
    with pytest.raises(ValueError, match="unsupported"):
        artifact_from_dict(document)


def test_cache_rejects_invalid_state_and_non_array_frames():
    document = artifact_to_dict(complete_artifact())
    document["state"] = "mostly-fine"
    with pytest.raises(ValueError, match="unknown analysis state"):
        artifact_from_dict(document)
    document = artifact_to_dict(complete_artifact())
    document["frames"] = {}
    with pytest.raises(ValueError, match="array"):
        artifact_from_dict(document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "frames",
            [{}] * (MAX_ARTIFACT_FRAMES + 1),
            "frames exceeds",
        ),
        (
            "issues",
            [{}] * (MAX_ARTIFACT_ISSUES + 1),
            "issues exceeds",
        ),
        (
            "tool_versions",
            {"tool-{}".format(index): "version" for index in range(MAX_ARTIFACT_TOOL_VERSIONS + 1)},
            "tool_versions exceeds",
        ),
    ],
)
def test_cache_rejects_nested_collections_before_converting_members(field, value, message):
    document = artifact_to_dict(complete_artifact())
    document[field] = value

    with pytest.raises(ValueError, match=message):
        artifact_from_dict(document)


def test_cache_rejects_oversized_audio_fingerprint_before_tuple_conversion():
    document = artifact_to_dict(complete_artifact())
    document["audio"]["values"] = [0] * (MAX_AUDIO_FINGERPRINT_WORDS + 1)

    with pytest.raises(ValueError, match="audio.values exceeds"):
        artifact_from_dict(document)


def test_cache_rejects_oversized_frame_bit_width_before_large_integer_shift():
    document = artifact_to_dict(complete_artifact())
    document["frames"][0]["bit_width"] = MAX_FRAME_FINGERPRINT_BITS + 1

    with pytest.raises(ValueError, match="bit width must be between"):
        artifact_from_dict(document)


@pytest.mark.parametrize(("bit_width", "value"), [(True, 1), (64, True)])
def test_frame_fingerprint_rejects_boolean_integer_fields(bit_width, value):
    with pytest.raises(ValueError):
        FrameFingerprint(0, 0, value, bit_width)


def test_cache_rejects_excessive_json_nesting_as_invalid_input(monkeypatch):
    def reject_nesting(_payload, **_kwargs):
        raise RecursionError("maximum JSON nesting exceeded")

    monkeypatch.setattr(cache_module.json, "loads", reject_nesting)

    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        artifact_from_json("{}")


def test_cache_rejects_duplicate_and_unknown_object_keys():
    payload = artifact_to_json(complete_artifact())
    duplicate = '{"schema_version":1,' + payload[1:]
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        artifact_from_json(duplicate)

    document = artifact_to_dict(complete_artifact())
    document["unexpected"] = "not allowed"
    with pytest.raises(ValueError, match="unsupported object shape"):
        artifact_from_dict(document)


def test_cache_rejects_extreme_integers_as_typed_invalid_input():
    document = artifact_to_dict(complete_artifact())
    document["metadata"]["duration_seconds"] = 10**1000

    with pytest.raises(ValueError, match="finite number"):
        artifact_from_dict(document)
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        artifact_from_json(json.dumps(document))


def test_cache_preflights_depth_before_calling_json_decoder(monkeypatch):
    called = False

    def unexpected_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("decoder must not run")

    monkeypatch.setattr(cache_module.json, "loads", unexpected_decode)
    payload = "[" * (cache_module.VIDEO_ARTIFACT_JSON_LIMITS.max_depth + 1)

    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        artifact_from_json(payload)

    assert not called
