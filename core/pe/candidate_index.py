# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Deterministic perceptual hashes and exact Hamming-radius candidate lookup."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_QUERY_PAIR_LIMIT = 250_000


class CandidatePairLimitError(RuntimeError):
    """Raised before a candidate iterator would exceed its explicit materialization budget."""

    def __init__(self, limit: int, emitted: int) -> None:
        self.limit = limit
        self.emitted = emitted
        super().__init__("candidate pair limit {} reached after {} pairs".format(limit, emitted))


class CandidateQueryCancelled(RuntimeError):
    """Raised when a caller-provided cancellation check stops candidate enumeration."""


class CandidateQueryLimitError(RuntimeError):
    """Raised before candidate lookup exceeds its explicit examination budget."""

    def __init__(self, limit: int, examined: int) -> None:
        self.limit = limit
        self.examined = examined
        super().__init__(
            "candidate query limit {} reached after examining {} entries".format(
                limit,
                examined,
            )
        )


@dataclass
class CandidateQueryBudget:
    """A caller-owned hard bound shared across any number of streaming queries."""

    limit: int
    examined: int = 0

    def __post_init__(self) -> None:
        _validate_pair_limit(self.limit)
        if (
            not isinstance(self.examined, int)
            or isinstance(self.examined, bool)
            or not 0 <= self.examined <= self.limit
        ):
            raise ValueError("examined candidate count is outside its budget")

    def consume(self) -> None:
        if self.examined >= self.limit:
            raise CandidateQueryLimitError(self.limit, self.examined)
        self.examined += 1


def _validate_pair_limit(max_pairs: int) -> None:
    if not isinstance(max_pairs, int) or isinstance(max_pairs, bool) or max_pairs <= 0:
        raise ValueError("max_pairs must be a positive integer")


def _check_cancelled(cancel_check) -> None:
    if cancel_check is None:
        return
    try:
        cancelled = bool(cancel_check())
    except Exception as error:
        raise CandidateQueryCancelled("candidate cancellation check failed") from error
    if cancelled:
        raise CandidateQueryCancelled("candidate enumeration was cancelled")


@dataclass(frozen=True)
class PerceptualHash:
    value: int
    bit_width: int
    algorithm: str = "dct_phash_v1"

    def __post_init__(self) -> None:
        if self.bit_width <= 0:
            raise ValueError("bit_width must be positive")
        if self.value < 0 or self.value >= 1 << self.bit_width:
            raise ValueError("perceptual hash value does not fit bit_width")
        if not self.algorithm:
            raise ValueError("algorithm must not be empty")

    def distance(self, other: "PerceptualHash") -> int:
        if self.bit_width != other.bit_width:
            raise ValueError("cannot compare hashes with different bit widths")
        return hamming_distance(self.value, other.value, self.bit_width)

    def to_hex(self) -> str:
        width = (self.bit_width + 3) // 4
        return "{:0{}x}".format(self.value, width)


def hamming_distance(first: int, second: int, bit_width: Optional[int] = None) -> int:
    """Return the bit Hamming distance, validating the configured width when supplied."""

    if first < 0 or second < 0:
        raise ValueError("fingerprints must be non-negative")
    if bit_width is not None:
        if bit_width <= 0:
            raise ValueError("bit_width must be positive")
        limit = 1 << bit_width
        if first >= limit or second >= limit:
            raise ValueError("fingerprint does not fit bit_width")
    value = first ^ second
    try:
        return value.bit_count()
    except AttributeError:  # Python 3.7 compatibility
        return bin(value).count("1")


def dct_phash(luma: Sequence[Sequence[float]], hash_size: int = 8) -> PerceptualHash:
    """Compute a decoder-independent pHash from an already-normalized luminance grid.

    Callers own image decoding, orientation, color management, and downsampling.  The matrix may be
    rectangular but must have at least ``hash_size`` rows and columns.  The DC coefficient is kept at
    zero so a uniform brightness shift does not by itself set a hash bit.
    """

    if hash_size <= 1:
        raise ValueError("hash_size must be greater than 1")
    rows = tuple(tuple(float(value) for value in row) for row in luma)
    if not rows or not rows[0]:
        raise ValueError("luminance matrix must not be empty")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError("luminance matrix must be rectangular")
    height = len(rows)
    if width < hash_size or height < hash_size:
        raise ValueError("luminance matrix is smaller than hash_size")
    if any(not math.isfinite(value) for row in rows for value in row):
        raise ValueError("luminance values must be finite")

    cos_x = [
        [math.cos(math.pi * (2 * x + 1) * frequency / (2 * width)) for x in range(width)]
        for frequency in range(hash_size)
    ]
    cos_y = [
        [math.cos(math.pi * (2 * y + 1) * frequency / (2 * height)) for y in range(height)]
        for frequency in range(hash_size)
    ]
    alpha_x = [math.sqrt(1 / width) if frequency == 0 else math.sqrt(2 / width) for frequency in range(hash_size)]
    alpha_y = [math.sqrt(1 / height) if frequency == 0 else math.sqrt(2 / height) for frequency in range(hash_size)]

    # A separable 2D DCT is mathematically identical to the direct four-loop definition but avoids
    # repeating the horizontal projection for every vertical frequency.  At the 32x32 decoder
    # input used by the picture scanner this reduces the inner multiplications from 65,536 to
    # 10,240 per image.
    horizontal = []
    for row in rows:
        horizontal.append(
            [
                alpha_x[frequency] * sum(value * factor for value, factor in zip(row, cos_x[frequency]))
                for frequency in range(hash_size)
            ]
        )
    coefficients: List[float] = []
    for vertical_frequency in range(hash_size):
        for horizontal_frequency in range(hash_size):
            total = sum(horizontal[y][horizontal_frequency] * cos_y[vertical_frequency][y] for y in range(height))
            coefficients.append(total * alpha_y[vertical_frequency])

    tolerance = max(abs(coefficient) for coefficient in coefficients) * 1e-12
    coefficients = [0.0 if abs(coefficient) <= tolerance else coefficient for coefficient in coefficients]
    median = statistics.median(coefficients[1:])
    fingerprint = 0
    for index, coefficient in enumerate(coefficients):
        if index and coefficient > median:
            fingerprint |= 1 << index
    return PerceptualHash(fingerprint, hash_size * hash_size)


@dataclass(frozen=True)
class HammingCandidate:
    asset_id: str
    distance: int
    fingerprint: int


@dataclass(frozen=True)
class CandidatePair:
    first_id: str
    second_id: str
    distance: int

    def __post_init__(self) -> None:
        if not self.first_id or not self.second_id or self.first_id >= self.second_id:
            raise ValueError("candidate pair IDs must be non-empty and in canonical order")
        if self.distance < 0:
            raise ValueError("candidate distance must be non-negative")


class MultiIndexHamming:
    """An exact Hamming-radius index using the multi-index pigeonhole guarantee.

    The fingerprint is partitioned into ``max_distance + 1`` non-empty segments.  Any value within
    that radius must share at least one segment with the query, so bucket lookup does not introduce
    false negatives.  Queries above the configured radius intentionally fall back to exhaustive
    comparison rather than silently becoming approximate.
    """

    def __init__(self, bit_width: int = 64, max_distance: int = 8) -> None:
        if bit_width <= 0:
            raise ValueError("bit_width must be positive")
        if max_distance < 0 or max_distance > bit_width:
            raise ValueError("max_distance must be between 0 and bit_width")
        self.bit_width = bit_width
        self.max_distance = max_distance
        self._fingerprints: Dict[str, int] = {}
        # Dict values preserve deterministic insertion order and let streaming
        # queries avoid sorting or copying a dense bucket.
        self._buckets: Dict[Tuple[int, int], Dict[str, None]] = {}
        if max_distance >= bit_width:
            self._segments: Tuple[Tuple[int, int], ...] = ()
        else:
            self._segments = self._partition(bit_width, max_distance + 1)

    @staticmethod
    def _partition(bit_width: int, count: int) -> Tuple[Tuple[int, int], ...]:
        base_width, wider_count = divmod(bit_width, count)
        result = []
        offset = 0
        for index in range(count):
            width = base_width + (1 if index < wider_count else 0)
            result.append((offset, width))
            offset += width
        return tuple(result)

    def _validate_fingerprint(self, fingerprint: int) -> None:
        if fingerprint < 0 or fingerprint >= 1 << self.bit_width:
            raise ValueError("fingerprint does not fit configured bit_width")

    @staticmethod
    def _validate_asset_id(asset_id: str) -> None:
        if not asset_id:
            raise ValueError("asset_id must not be empty")

    def _bucket_keys(self, fingerprint: int) -> Iterable[Tuple[int, int]]:
        for index, (offset, width) in enumerate(self._segments):
            mask = (1 << width) - 1
            yield index, (fingerprint >> offset) & mask

    def add(self, asset_id: str, fingerprint: int) -> None:
        self._validate_asset_id(asset_id)
        self._validate_fingerprint(fingerprint)
        previous = self._fingerprints.get(asset_id)
        if previous == fingerprint:
            return
        if previous is not None:
            self.remove(asset_id)
        self._fingerprints[asset_id] = fingerprint
        for key in self._bucket_keys(fingerprint):
            self._buckets.setdefault(key, {})[asset_id] = None

    def remove(self, asset_id: str) -> None:
        fingerprint = self._fingerprints.pop(asset_id)
        for key in self._bucket_keys(fingerprint):
            bucket = self._buckets[key]
            del bucket[asset_id]
            if not bucket:
                del self._buckets[key]

    def fingerprint(self, asset_id: str) -> int:
        return self._fingerprints[asset_id]

    def query(
        self,
        fingerprint: int,
        max_distance: Optional[int] = None,
        exclude_id: Optional[str] = None,
    ) -> Tuple[HammingCandidate, ...]:
        return tuple(
            sorted(
                self.iter_query(
                    fingerprint,
                    max_distance=max_distance,
                    exclude_id=exclude_id,
                ),
                key=lambda candidate: (candidate.distance, candidate.asset_id),
            )
        )

    def _iter_candidate_ids(self, fingerprint: int, radius: int) -> Iterable[str]:
        if radius > self.max_distance or not self._segments:
            yield from self._fingerprints
            return

        query_keys = tuple(self._bucket_keys(fingerprint))
        for segment_index, key in enumerate(query_keys):
            for asset_id in self._buckets.get(key, ()):
                candidate_fingerprint = self._fingerprints[asset_id]
                # A candidate can share several segments. Assign it to the
                # first shared segment instead of retaining a per-query set.
                duplicate = False
                for earlier_index in range(segment_index):
                    offset, width = self._segments[earlier_index]
                    mask = (1 << width) - 1
                    candidate_part = (candidate_fingerprint >> offset) & mask
                    if candidate_part == query_keys[earlier_index][1]:
                        duplicate = True
                        break
                if not duplicate:
                    yield asset_id

    def iter_query(
        self,
        fingerprint: int,
        max_distance: Optional[int] = None,
        exclude_id: Optional[str] = None,
        *,
        budget: Optional[CandidateQueryBudget] = None,
        cancel_check=None,
    ) -> Iterable[HammingCandidate]:
        """Stream exact-radius candidates with constant auxiliary memory.

        ``budget`` counts every unique index entry examined, including bucket
        false positives, so a dense or adversarial index cannot allocate work
        before the caller's resource limit is enforced.
        """

        self._validate_fingerprint(fingerprint)
        radius = self.max_distance if max_distance is None else max_distance
        if radius < 0 or radius > self.bit_width:
            raise ValueError("query max_distance must be between 0 and bit_width")
        if budget is not None and not isinstance(budget, CandidateQueryBudget):
            raise TypeError("budget must be a CandidateQueryBudget")
        for asset_id in self._iter_candidate_ids(fingerprint, radius):
            _check_cancelled(cancel_check)
            if budget is not None:
                budget.consume()
            if asset_id == exclude_id:
                continue
            candidate_fingerprint = self._fingerprints[asset_id]
            distance = hamming_distance(fingerprint, candidate_fingerprint, self.bit_width)
            if distance <= radius:
                yield HammingCandidate(
                    asset_id,
                    distance,
                    candidate_fingerprint,
                )

    def iter_query_pairs(
        self,
        max_distance: Optional[int] = None,
        *,
        max_pairs: int = DEFAULT_QUERY_PAIR_LIMIT,
        cancel_check=None,
    ) -> Iterable[CandidatePair]:
        """Yield deterministic pairs without ever retaining the complete pair set.

        Iteration order is canonical asset order.  ``query_pairs`` retains its historic
        distance-first result ordering by sorting this explicitly bounded stream.
        """

        _validate_pair_limit(max_pairs)
        emitted = 0
        for first_id in sorted(self._fingerprints):
            _check_cancelled(cancel_check)
            fingerprint = self._fingerprints[first_id]
            for candidate in self.iter_query(
                fingerprint,
                max_distance=max_distance,
                exclude_id=first_id,
                cancel_check=cancel_check,
            ):
                _check_cancelled(cancel_check)
                if first_id < candidate.asset_id:
                    if emitted >= max_pairs:
                        raise CandidatePairLimitError(max_pairs, emitted)
                    emitted += 1
                    yield CandidatePair(
                        first_id,
                        candidate.asset_id,
                        candidate.distance,
                    )

    def query_pairs(
        self,
        max_distance: Optional[int] = None,
        *,
        max_pairs: int = DEFAULT_QUERY_PAIR_LIMIT,
        cancel_check=None,
    ) -> Tuple[CandidatePair, ...]:
        pairs = self.iter_query_pairs(
            max_distance=max_distance,
            max_pairs=max_pairs,
            cancel_check=cancel_check,
        )
        return tuple(
            sorted(
                pairs,
                key=lambda pair: (
                    pair.distance,
                    pair.first_id,
                    pair.second_id,
                ),
            )
        )

    def __contains__(self, asset_id: str) -> bool:
        return asset_id in self._fingerprints

    def __len__(self) -> int:
        return len(self._fingerprints)

    @classmethod
    def from_hashes(
        cls,
        fingerprints: Mapping[str, int],
        bit_width: int = 64,
        max_distance: int = 8,
    ) -> "MultiIndexHamming":
        index = cls(bit_width=bit_width, max_distance=max_distance)
        for asset_id in sorted(fingerprints):
            index.add(asset_id, fingerprints[asset_id])
        return index
