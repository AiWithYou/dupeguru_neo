# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

from core.video.alignment import align_audio_fingerprints, align_frame_fingerprints
from core.video.model import (
    AlignmentState,
    AudioFingerprint,
    FrameFingerprint,
)


def frames(values):
    return tuple(FrameFingerprint(index, index / max(1, len(values) - 1), value) for index, value in enumerate(values))


def test_identical_frame_sequences_align_completely():
    first = frames((1, 2, 4, 8, 16))
    alignment = align_frame_fingerprints(first, first)
    assert alignment.state is AlignmentState.COMPLETE
    assert alignment.score == 1
    assert alignment.coverage_first == 1
    assert alignment.coverage_second == 1
    assert alignment.matched_pairs == tuple((index, index) for index in range(5))


def test_local_frame_alignment_finds_leading_and_trailing_cut():
    original = frames((1, 2, 4, 8, 16, 32, 64, 128))
    trimmed = frames((4, 8, 16, 32, 64))
    alignment = align_frame_fingerprints(original, trimmed, maximum_hamming_distance=0)
    assert alignment.state is AlignmentState.COMPLETE
    assert alignment.score == 1
    assert alignment.coverage_first == 5 / 8
    assert alignment.coverage_second == 1
    assert alignment.start_first == 2
    assert alignment.start_second == 0


def test_frame_alignment_reports_resource_limit_instead_of_partial_score():
    alignment = align_frame_fingerprints(frames(tuple(range(30))), frames(tuple(range(30))), maximum_cells=10)
    assert alignment.state is AlignmentState.RESOURCE_LIMIT
    assert alignment.score == 0
    assert alignment.matched_pairs == ()


def test_audio_alignment_uses_anchors_to_find_large_trim_offset():
    common = tuple(0x12340000 + index * 17 for index in range(120))
    first = AudioFingerprint(tuple(range(300, 360)) + common + tuple(range(600, 660)), 30)
    second = AudioFingerprint(tuple(range(900, 950)) + common, 20)
    alignment = align_audio_fingerprints(
        first,
        second,
        maximum_hamming_distance=0,
        band=8,
        maximum_cells=20_000,
    )
    assert alignment.state is AlignmentState.COMPLETE
    assert alignment.score > 0.95
    assert len(alignment.matched_pairs) == len(common)
    assert alignment.coverage_second > 0.70


def test_empty_alignment_is_explicit():
    alignment = align_frame_fingerprints((), ())
    assert alignment.state is AlignmentState.EMPTY_INPUT
    assert alignment.score == 0
