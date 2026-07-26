import math
import random
import statistics
import tracemalloc

import pytest

from core.pe.candidate_index import (
    CandidatePairLimitError,
    CandidateQueryBudget,
    CandidateQueryCancelled,
    CandidateQueryLimitError,
    MultiIndexHamming,
    dct_phash,
    hamming_distance,
)


def brute_force(fingerprints, query, radius, bit_width, exclude_id=None):
    return tuple(
        sorted(
            (
                (asset_id, hamming_distance(query, fingerprint, bit_width))
                for asset_id, fingerprint in fingerprints.items()
                if asset_id != exclude_id and hamming_distance(query, fingerprint, bit_width) <= radius
            ),
            key=lambda item: (item[1], item[0]),
        )
    )


def direct_dct_phash(luma, hash_size=8):
    """Independent direct 2D-DCT reference for the separable production implementation."""

    height = len(luma)
    width = len(luma[0])
    coefficients = []
    for vertical_frequency in range(hash_size):
        alpha_y = math.sqrt(1 / height) if vertical_frequency == 0 else math.sqrt(2 / height)
        for horizontal_frequency in range(hash_size):
            alpha_x = math.sqrt(1 / width) if horizontal_frequency == 0 else math.sqrt(2 / width)
            total = 0.0
            for y, row in enumerate(luma):
                y_factor = math.cos(math.pi * (2 * y + 1) * vertical_frequency / (2 * height))
                for x, value in enumerate(row):
                    x_factor = math.cos(math.pi * (2 * x + 1) * horizontal_frequency / (2 * width))
                    total += value * x_factor * y_factor
            coefficients.append(total * alpha_x * alpha_y)
    tolerance = max(abs(coefficient) for coefficient in coefficients) * 1e-12
    coefficients = [0.0 if abs(coefficient) <= tolerance else coefficient for coefficient in coefficients]
    median = statistics.median(coefficients[1:])
    return sum(1 << index for index, coefficient in enumerate(coefficients) if index and coefficient > median)


def test_dct_phash_golden_constant_and_symmetric_quadrants():
    constant = [[17] * 8 for _ in range(8)]
    quadrants = [[255 if (x < 4 and y < 4) or (x >= 4 and y >= 4) else 0 for x in range(8)] for y in range(8)]
    assert dct_phash(constant).to_hex() == "0000000000000000"
    # The quadrant pattern is the product of two centered step functions. Its DC-free 2D DCT is
    # non-zero only for odd x/y frequencies, and positive where both 1D step coefficients have the
    # same alternating sign. This derives the expected row-major bits independently of dct_phash().
    odd_frequencies = (1, 3, 5, 7)
    expected_bits = {
        y_frequency * 8 + x_frequency
        for y_frequency in odd_frequencies
        for x_frequency in odd_frequencies
        if (x_frequency // 2) % 2 == (y_frequency // 2) % 2
    }
    expected = sum(1 << bit for bit in expected_bits)
    assert dct_phash(quadrants).value == expected


def test_dct_phash_ignores_uniform_brightness_offset():
    first = [[x * 3 + y for x in range(8)] for y in range(8)]
    second = [[value + 50 for value in row] for row in first]
    assert dct_phash(first) == dct_phash(second)


def test_separable_dct_matches_direct_2d_dct_property():
    generator = random.Random(614_903)
    for width, height in ((8, 8), (13, 11), (32, 32)):
        for _ in range(4):
            luma = [[generator.randrange(256) for _ in range(width)] for _ in range(height)]
            assert dct_phash(luma).value == direct_dct_phash(luma)


def test_multi_index_query_matches_brute_force_property():
    generator = random.Random(20260726)
    bit_width = 16
    radius = 3
    fingerprints = {"asset-{:03d}".format(index): generator.randrange(1 << bit_width) for index in range(150)}
    index = MultiIndexHamming.from_hashes(fingerprints, bit_width=bit_width, max_distance=radius)
    for _ in range(100):
        query = generator.randrange(1 << bit_width)
        actual = tuple((item.asset_id, item.distance) for item in index.query(query))
        assert actual == brute_force(fingerprints, query, radius, bit_width)


def test_multi_index_query_pairs_matches_brute_force_property():
    generator = random.Random(19)
    bit_width = 12
    radius = 2
    fingerprints = {"asset-{:03d}".format(index): generator.randrange(1 << bit_width) for index in range(80)}
    index = MultiIndexHamming.from_hashes(fingerprints, bit_width=bit_width, max_distance=radius)
    expected = []
    asset_ids = sorted(fingerprints)
    for position, first_id in enumerate(asset_ids):
        for second_id in asset_ids[position + 1 :]:
            distance = hamming_distance(fingerprints[first_id], fingerprints[second_id], bit_width)
            if distance <= radius:
                expected.append((first_id, second_id, distance))
    expected.sort(key=lambda item: (item[2], item[0], item[1]))
    assert [(pair.first_id, pair.second_id, pair.distance) for pair in index.query_pairs()] == expected


def test_dense_ten_thousand_pair_iteration_is_explicitly_bounded():
    count = 10_000
    limit = 1_234
    index = MultiIndexHamming(bit_width=64, max_distance=0)
    for item in range(count):
        index.add("asset-{:05d}".format(item), 0)

    tracemalloc.start()
    iterator = index.iter_query_pairs(max_pairs=limit)
    observed = [next(iterator) for _ in range(limit)]
    with pytest.raises(CandidatePairLimitError) as error:
        next(iterator)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(observed) == limit
    assert error.value.limit == limit
    assert error.value.emitted == limit
    assert peak < 32 * 1024 * 1024


def test_dense_query_enforces_budget_before_materializing_bucket():
    index = MultiIndexHamming(bit_width=64, max_distance=0)
    for item in range(100_000):
        index.add("asset-{:06d}".format(item), 0)
    budget = CandidateQueryBudget(1)

    tracemalloc.start()
    iterator = index.iter_query(0, budget=budget)
    first = next(iterator)
    with pytest.raises(CandidateQueryLimitError) as error:
        next(iterator)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert first.asset_id == "asset-000000"
    assert budget.examined == 1
    assert error.value.limit == 1
    assert peak < 1024 * 1024


def test_query_budget_counts_excluded_and_radius_rejected_entries():
    index = MultiIndexHamming(bit_width=8, max_distance=1)
    index.add("excluded", 0)
    index.add("radius-rejected", 0b11110000)
    index.add("exact", 0)
    budget = CandidateQueryBudget(2)

    iterator = index.iter_query(
        0,
        max_distance=1,
        exclude_id="excluded",
        budget=budget,
    )
    with pytest.raises(CandidateQueryLimitError) as error:
        tuple(iterator)

    assert budget.examined == 2
    assert error.value.examined == 2
    assert error.value.limit == 2


def test_dense_query_checks_cancellation_before_each_examined_entry():
    index = MultiIndexHamming(bit_width=64, max_distance=0)
    for item in range(100):
        index.add("asset-{:03d}".format(item), 0)
    budget = CandidateQueryBudget(100)
    checks = 0

    def cancel_after_ten_entries():
        nonlocal checks
        checks += 1
        return checks > 10

    with pytest.raises(CandidateQueryCancelled):
        tuple(
            index.iter_query(
                0,
                budget=budget,
                cancel_check=cancel_after_ten_entries,
            )
        )

    assert checks == 11
    assert budget.examined == 10


def test_pair_iteration_cancellation_is_explicit():
    index = MultiIndexHamming.from_hashes(
        {"first": 0, "second": 0},
        max_distance=0,
    )

    with pytest.raises(CandidateQueryCancelled):
        next(index.iter_query_pairs(cancel_check=lambda: True))


@pytest.mark.parametrize("limit", (0, -1, True, 1.5))
def test_pair_materialization_limit_is_validated(limit):
    index = MultiIndexHamming.from_hashes({"first": 0, "second": 0})

    with pytest.raises(ValueError, match="max_pairs"):
        index.query_pairs(max_pairs=limit)


def test_query_above_configured_radius_falls_back_without_false_negatives():
    fingerprints = {"a": 0b00000000, "b": 0b00001111, "c": 0b11111111}
    index = MultiIndexHamming.from_hashes(fingerprints, bit_width=8, max_distance=1)
    actual = tuple((item.asset_id, item.distance) for item in index.query(0, max_distance=4))
    assert actual == (("a", 0), ("b", 4))


def test_add_update_remove_keep_index_consistent():
    index = MultiIndexHamming(bit_width=8, max_distance=2)
    index.add("a", 0)
    index.add("b", 1)
    assert [item.asset_id for item in index.query(0)] == ["a", "b"]
    index.add("b", 255)
    assert [item.asset_id for item in index.query(0)] == ["a"]
    index.remove("a")
    assert len(index) == 1
    assert "a" not in index
