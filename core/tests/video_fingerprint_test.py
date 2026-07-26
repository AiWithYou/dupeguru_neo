# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import pytest

import core.video.json_guard as video_json_guard
from core.video.alignment import align_frame_fingerprints
from core.video.fingerprint import (
    FramePlanPolicy,
    build_frame_plan,
    classify_metadata_candidate,
    classify_video_relation,
    evidence_from_exact_proof,
    parse_fpcalc_json,
    phash_gray_frame,
)
from core.video.model import MAX_AUDIO_FINGERPRINT_WORDS
from core.video.model import (
    AnalysisState,
    ByteExactProof,
    CandidateKind,
    FrameFingerprint,
    SourceSnapshot,
    VideoArtifact,
    VideoMetadata,
    VideoRelation,
)


def metadata(duration=100, width=1920, height=1080, codec="h264", fps=30):
    return VideoMetadata(duration, width, height, fps, codec, "yuv420p")


def artifact(path, values, *, details=None):
    details = details or metadata()
    frames = tuple(
        FrameFingerprint(
            details.duration_seconds * (index + 1) / (len(values) + 1),
            (index + 1) / (len(values) + 1),
            value,
        )
        for index, value in enumerate(values)
    )
    return VideoArtifact(
        SourceSnapshot(path, 100, 123),
        details,
        frames,
        None,
        AnalysisState.COMPLETE,
    )


def test_metadata_classification_never_claims_exactness():
    same = classify_metadata_candidate(metadata(), metadata())
    assert same.kind is CandidateKind.SAME_ENCODE_CANDIDATE
    assert "unverified" in same.reasons[0]

    transcoded = classify_metadata_candidate(metadata(), metadata(width=1280, height=720, codec="hevc"))
    assert transcoded.kind is CandidateKind.TRANSCODE_CANDIDATE

    trimmed = classify_metadata_candidate(metadata(100), metadata(40))
    assert trimmed.kind is CandidateKind.TRIM_CANDIDATE

    rejected = classify_metadata_candidate(metadata(100), metadata(5))
    assert rejected.kind is CandidateKind.REJECTED
    assert not rejected.should_compare


def test_frame_plan_combines_normalized_and_scene_samples_under_limit():
    policy = FramePlanPolicy(
        normalized_frames=3,
        maximum_frames=5,
        minimum_separation_seconds=0.5,
    )
    plan = build_frame_plan(metadata(duration=10), (0.01, 1, 4.9, 5.0, 9), policy)
    assert len(plan.requests) == 5
    assert sum(item.origin.value == "normalized" for item in plan.requests) == 3
    assert all(0 <= item.timestamp_seconds <= 10 for item in plan.requests)
    assert list(plan.requests) == sorted(plan.requests, key=lambda item: (item.timestamp_seconds, item.origin.value))


def test_gray_frame_phash_is_deterministic_and_validates_shape():
    pixels = bytes(index % 256 for index in range(32 * 32))
    assert phash_gray_frame(pixels).value == phash_gray_frame(pixels).value
    with pytest.raises(ValueError):
        phash_gray_frame(pixels[:-1])


def test_fpcalc_raw_json_parser_accepts_string_and_array():
    string = parse_fpcalc_json(b'{"duration":12.5,"fingerprint":"1,-2,3"}')
    array = parse_fpcalc_json('{"duration":12.5,"fingerprint":[1,-2,3]}')
    assert string == array
    with pytest.raises(ValueError):
        parse_fpcalc_json('{"duration":12.5,"fingerprint":"not-an-int"}')


def test_fpcalc_preflights_container_limit_before_decoder_allocation(monkeypatch):
    called = False

    def unexpected_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("decoder must not run")

    monkeypatch.setattr(video_json_guard.json, "loads", unexpected_decode)
    payload = '{"duration":1,"fingerprint":[' + ",".join("0" for _ in range(4)) + "]}"

    with pytest.raises(ValueError, match="invalid JSON"):
        parse_fpcalc_json(payload, maximum_words=3)

    assert not called


def test_fpcalc_rejects_nonfinite_duration_and_oversized_word_contract():
    with pytest.raises(ValueError):
        parse_fpcalc_json('{"duration":NaN,"fingerprint":[1]}')
    with pytest.raises(ValueError, match="between 1"):
        parse_fpcalc_json(
            '{"duration":1,"fingerprint":[1]}',
            maximum_words=MAX_AUDIO_FINGERPRINT_WORDS + 1,
        )


def test_transcode_and_trim_relations_are_never_deletion_proofs():
    values = (1, 2, 4, 8, 16, 32, 64, 128)
    original = artifact("original.mp4", values)
    transcoded = artifact("transcoded.mkv", values, details=metadata(width=1280, height=720, codec="hevc"))
    transcode_evidence = classify_video_relation(original, transcoded)
    assert transcode_evidence is not None
    assert transcode_evidence.relation is VideoRelation.TRANSCODED
    assert not transcode_evidence.allows_automatic_destructive_action

    trimmed = artifact("trimmed.mp4", values[2:-1], details=metadata(duration=60))
    alignment = align_frame_fingerprints(original.frames, trimmed.frames, maximum_hamming_distance=0)
    trim_evidence = classify_video_relation(original, trimmed, frame_alignment=alignment)
    assert trim_evidence is not None
    assert trim_evidence.relation is VideoRelation.TRIMMED
    assert not trim_evidence.allows_automatic_destructive_action


def test_near_and_related_relations_remain_review_only():
    values = (0, 1, 2, 3)
    first = artifact("first.mp4", values)
    near = classify_video_relation(first, artifact("near.mp4", values))
    assert near is not None
    assert near.relation is VideoRelation.NEAR_DUPLICATE
    assert not near.allows_automatic_destructive_action

    visually_distant = tuple(value ^ 0xFF for value in values)
    related = classify_video_relation(first, artifact("related.mp4", visually_distant))
    assert related is not None
    assert related.relation is VideoRelation.RELATED
    assert not related.allows_automatic_destructive_action


def test_only_byte_exact_proof_allows_automatic_destructive_action():
    proof = ByteExactProof("a.mp4", "b.mp4", 1024, "sha256", "ab" * 32, 1024)
    evidence = evidence_from_exact_proof(proof)
    assert evidence.relation is VideoRelation.EXACT
    assert evidence.allows_automatic_destructive_action
    with pytest.raises(ValueError):
        ByteExactProof("a.mp4", "b.mp4", 1024, "sha256", "ab" * 32, 512)
