# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded local sequence alignment for frame and Chromaprint fingerprints."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import DefaultDict, Dict, Optional, Sequence, Tuple

from core.pe.candidate_index import hamming_distance
from core.video.model import (
    AlignmentState,
    AudioFingerprint,
    FrameFingerprint,
    SequenceAlignment,
)


def align_frame_fingerprints(
    first: Sequence[FrameFingerprint],
    second: Sequence[FrameFingerprint],
    *,
    maximum_hamming_distance: int = 14,
    maximum_cells: int = 100_000,
) -> SequenceAlignment:
    """Locally align ordered pHashes, allowing leading/trailing cuts and dropped frames."""

    if maximum_hamming_distance < 0:
        raise ValueError("maximum Hamming distance must be non-negative")
    bit_widths = {item.bit_width for item in tuple(first) + tuple(second)}
    if len(bit_widths) > 1:
        raise ValueError("frame fingerprints must use the same bit width")
    bit_width = next(iter(bit_widths), 64)
    return _local_alignment(
        tuple(item.value for item in first),
        tuple(item.value for item in second),
        bit_width=bit_width,
        maximum_hamming_distance=maximum_hamming_distance,
        maximum_cells=maximum_cells,
        band=None,
        center_offset=0,
    )


def align_audio_fingerprints(
    first: AudioFingerprint,
    second: AudioFingerprint,
    *,
    maximum_hamming_distance: int = 8,
    band: int = 128,
    maximum_cells: int = 2_500_000,
) -> SequenceAlignment:
    """Use anchor-centered banded local alignment for raw Chromaprint words."""

    if first.algorithm != second.algorithm:
        raise ValueError("audio fingerprints must use the same algorithm")
    if band < 0:
        raise ValueError("alignment band must be non-negative")
    normalized_first = tuple(value & 0xFFFFFFFF for value in first.values)
    normalized_second = tuple(value & 0xFFFFFFFF for value in second.values)
    center_offset = _estimate_offset(normalized_first, normalized_second)
    return _local_alignment(
        normalized_first,
        normalized_second,
        bit_width=32,
        maximum_hamming_distance=maximum_hamming_distance,
        maximum_cells=maximum_cells,
        band=band,
        center_offset=center_offset,
    )


def _estimate_offset(first: Sequence[int], second: Sequence[int]) -> int:
    """Find a stable trim offset from bounded exact-word anchors."""

    positions: DefaultDict[int, list[int]] = defaultdict(list)
    for index, value in enumerate(second):
        if len(positions[value]) < 8:
            positions[value].append(index)
    offsets: Counter[int] = Counter()
    stride = max(1, len(first) // 4096)
    for first_index in range(0, len(first), stride):
        for second_index in positions.get(first[first_index], ()):
            offsets[second_index - first_index] += 1
    if not offsets:
        return 0
    # Count first, then prefer a smaller absolute displacement for deterministic ties.
    return min(offsets, key=lambda offset: (-offsets[offset], abs(offset), offset))


def _local_alignment(
    first: Sequence[int],
    second: Sequence[int],
    *,
    bit_width: int,
    maximum_hamming_distance: int,
    maximum_cells: int,
    band: Optional[int],
    center_offset: int,
) -> SequenceAlignment:
    if maximum_cells <= 0:
        raise ValueError("maximum alignment cells must be positive")
    if maximum_hamming_distance < 0 or maximum_hamming_distance > bit_width:
        raise ValueError("maximum Hamming distance must fit bit width")
    if not first or not second:
        return SequenceAlignment(AlignmentState.EMPTY_INPUT, 0, (), 0, 0, None, None, None, None, None, 0)
    if band is None and len(first) * len(second) > maximum_cells:
        return SequenceAlignment(
            AlignmentState.RESOURCE_LIMIT,
            0,
            (),
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            0,
        )

    gap_penalty = -0.85
    previous: Dict[int, float] = {}
    # One byte per evaluated cell keeps long audio alignments bounded.  Python dictionaries for
    # every score, direction, and distance would otherwise consume hundreds of MiB.
    trace_rows: list[Tuple[int, bytearray]] = []
    best_score = 0.0
    best_cell: Optional[Tuple[int, int]] = None
    cells = 0
    for first_index, first_value in enumerate(first, start=1):
        current: Dict[int, float] = {}
        if band is None:
            lower = 1
            upper = len(second)
        else:
            center = first_index - 1 + center_offset
            lower = max(1, center - band + 1)
            upper = min(len(second), center + band + 1)
        row_trace = bytearray(max(0, upper - lower + 1))
        for second_index in range(lower, upper + 1):
            cells += 1
            if cells > maximum_cells:
                return SequenceAlignment(
                    AlignmentState.RESOURCE_LIMIT,
                    0,
                    (),
                    0,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    cells - 1,
                )
            distance = hamming_distance(first_value, second[second_index - 1], bit_width)
            if distance <= maximum_hamming_distance:
                similarity = 2.0 * (1 - distance / (maximum_hamming_distance + 1))
            else:
                similarity = -1.25
            diagonal = previous.get(second_index - 1, 0) + similarity
            up = previous.get(second_index, 0) + gap_penalty
            left = current.get(second_index - 1, 0) + gap_penalty
            score = max(0.0, diagonal, up, left)
            current[second_index] = score
            if score == 0:
                move = 0
            elif score == diagonal:
                move = 1
            elif score == up:
                move = 2
            else:
                move = 3
            row_trace[second_index - lower] = move
            if score > best_score:
                best_score = score
                best_cell = (first_index, second_index)
        trace_rows.append((lower, row_trace))
        previous = current

    if best_cell is None:
        return SequenceAlignment(AlignmentState.COMPLETE, 0, (), 0, 0, None, None, None, None, None, cells)

    matched = []
    distances = []
    first_index, second_index = best_cell
    while first_index > 0 and second_index > 0:
        lower, row_trace = trace_rows[first_index - 1]
        trace_index = second_index - lower
        if trace_index < 0 or trace_index >= len(row_trace):
            break
        move = row_trace[trace_index]
        if move == 1:
            distance = hamming_distance(first[first_index - 1], second[second_index - 1], bit_width)
            if distance <= maximum_hamming_distance:
                matched.append((first_index - 1, second_index - 1))
                distances.append(distance)
            first_index -= 1
            second_index -= 1
        elif move == 2:
            first_index -= 1
        elif move == 3:
            second_index -= 1
        else:
            break
    matched.reverse()
    distances.reverse()
    if not matched:
        return SequenceAlignment(AlignmentState.COMPLETE, 0, (), 0, 0, None, None, None, None, None, cells)

    coverage_first = len(matched) / len(first)
    coverage_second = len(matched) / len(second)
    mean_distance = sum(distances) / len(distances)
    mean_similarity = 1 - mean_distance / max(1, maximum_hamming_distance + 1)
    coverage_factor = 0.85 + 0.15 * math.sqrt(max(coverage_first, coverage_second))
    normalized_score = max(0.0, min(1.0, mean_similarity * coverage_factor))
    return SequenceAlignment(
        state=AlignmentState.COMPLETE,
        score=normalized_score,
        matched_pairs=tuple(matched),
        coverage_first=coverage_first,
        coverage_second=coverage_second,
        mean_distance=mean_distance,
        start_first=matched[0][0],
        start_second=matched[0][1],
        end_first=matched[-1][0],
        end_second=matched[-1][1],
        cells_evaluated=cells,
    )
