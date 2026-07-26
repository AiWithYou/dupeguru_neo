import os
import random
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core import engine
from core.keeper import choose_keeper
from core.pe import matchblock
from core.pe.candidate_index import MultiIndexHamming
from core.pe.image_features import (
    DecoderUnavailableError,
    ImageFeatures,
    ImageQuality,
    ImageResourceLimitError,
)
from core.pe.scanner import ScannerPE
from core.scan_receipt import ScanStatus
from core.scanner import ScanType


class Picture:
    def __init__(self, path, is_ref=False):
        self.path = Path(path)
        self.name = self.path.name
        self.unicode_path = str(self.path)
        self.is_ref = is_ref
        self.size = self.path.stat().st_size
        self.dimensions = (0, 0)

    def exists(self):
        return self.path.exists()


def _synthetic_features(
    fingerprint,
    color=(0, 0, 0),
    orientations=1,
    dimensions=(30, 20),
    quality=None,
    dhash=None,
    histogram=None,
):
    block = tuple(color for _ in range(9))
    dhash_values = fingerprint if dhash is None else dhash
    return ImageFeatures(
        dimensions=dimensions,
        frame_count=1,
        blocks=tuple(block for _ in range(orientations)),
        phashes=tuple(
            fingerprint if isinstance(fingerprint, int) else fingerprint[index] for index in range(orientations)
        ),
        dhashes=tuple(
            dhash_values if isinstance(dhash_values, int) else dhash_values[index] for index in range(orientations)
        ),
        color_histogram=histogram or ((1024,) + (0,) * 63),
        tile_fingerprints=(),
        quality=quality or ImageQuality(8, 0, 0, 0.0),
        thumbnail_png=b"synthetic-thumbnail",
        thumbnail_size=(30, 20),
        thumbnail_key="synthetic-key-{}".format(fingerprint),
    )


def _install_synthetic_decoder(monkeypatch, by_name):
    def decode(path, block_count_per_side, include_orientations=False):
        features = by_name[Path(path).name]
        expected = 8 if include_orientations else 1
        assert features.orientation_count == expected
        return features

    monkeypatch.setattr(matchblock, "decode_image_features", decode)


@pytest.mark.parametrize("distance", (-1, 65, 1.5, True))
def test_phash_distance_is_validated_even_for_empty_scans(distance):
    with pytest.raises(ValueError, match="phash_distance"):
        matchblock.getmatches([], cache_path=None, threshold=80, phash_distance=distance)


@pytest.mark.parametrize(
    "name,value",
    (
        ("dhash_distance", -1),
        ("dhash_distance", 65),
        ("color_histogram_distance", -0.1),
        ("color_histogram_distance", float("inf")),
    ),
)
def test_secondary_candidate_filters_are_strictly_bounded(name, value):
    with pytest.raises(ValueError, match=name):
        matchblock.getmatches(
            [],
            cache_path=None,
            threshold=80,
            **{name: value},
        )


def test_dhash_and_color_histogram_conjunctively_reject_weak_candidates(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _install_synthetic_decoder(
        monkeypatch,
        {
            first.name: _synthetic_features(
                0,
                dhash=0,
                histogram=(1024,) + (0,) * 63,
            ),
            second.name: _synthetic_features(
                0,
                dhash=(1 << 64) - 1,
                histogram=(0,) * 63 + (1024,),
            ),
        },
    )

    result = matchblock.getmatches(
        [Picture(first), Picture(second)],
        cache_path=None,
        threshold=90,
        phash_distance=0,
        dhash_distance=24,
        color_histogram_distance=0.55,
    )

    assert result == []
    assert result.candidate_stats.candidate_pairs == 0


@pytest.mark.parametrize(
    "name",
    ("max_candidate_pairs", "max_refined_pairs", "max_matches"),
)
@pytest.mark.parametrize("value", (0, -1, 1.5, True))
def test_finite_picture_budgets_are_validated_for_empty_scans(name, value):
    with pytest.raises(ValueError, match=name):
        matchblock.getmatches(
            [],
            cache_path=None,
            threshold=80,
            **{name: value},
        )


def test_configured_hamming_candidates_are_the_only_pairs_refined(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png", "c.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0b0000),
            "b.png": _synthetic_features(0b0001),
            "c.png": _synthetic_features(0b1111),
        },
    )

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=90,
        phash_distance=1,
    )

    assert [(match.first.name, match.second.name, match.percentage) for match in result] == [("a.png", "b.png", 99)]
    assert result.candidate_stats.possible_pairs == 3
    assert result.candidate_stats.candidate_pairs == 1
    assert result.candidate_stats.refined_pairs == 1
    assert result.scan_receipt.status is ScanStatus.COMPLETE


def test_in_memory_cache_keeps_only_volatile_blocks_needed_for_refinement(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0),
            "b.png": _synthetic_features(0),
        },
    )

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        cache_path=None,
        threshold=90,
        phash_distance=0,
    )

    assert len(result) == 1
    assert result[0].percentage == 99
    assert result.scan_receipt.status is ScanStatus.COMPLETE


def test_picture_quality_is_hydrated_for_explainable_keeper_policy(
    tmp_path,
    monkeypatch,
):
    original = tmp_path / "original.png"
    copy = tmp_path / "original copy.jpg"
    original.write_bytes(b"original")
    copy.write_bytes(b"copy")
    _install_synthetic_decoder(
        monkeypatch,
        {
            original.name: _synthetic_features(
                0,
                quality=ImageQuality(16, 8, 10, 0.0),
            ),
            copy.name: _synthetic_features(
                0,
                quality=ImageQuality(8, 1, 2, 0.8),
            ),
        },
    )
    pictures = [Picture(original), Picture(copy)]

    result = matchblock.getmatches(
        pictures,
        cache_path=None,
        threshold=90,
        phash_distance=0,
    )
    decision = choose_keeper(pictures)

    assert len(result) == 1
    assert pictures[0].bit_depth == 16
    assert pictures[0].metadata_count == 10
    assert pictures[1].jpeg_artifact_score == 0.8
    assert decision.keeper is pictures[0]
    assert "lower bit depth than the keeper" in decision.explanation(pictures[1])
    assert "less metadata retained than the keeper" in decision.explanation(pictures[1])


def test_persistent_feature_cache_avoids_redecoding_unchanged_images(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0),
            "b.png": _synthetic_features(0),
        },
    )
    cache_path = str(tmp_path / "cache.sqlite3")
    first = matchblock.getmatches(
        [Picture(path) for path in paths],
        cache_path=cache_path,
        threshold=90,
        phash_distance=0,
    )

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("unchanged cached images must not be decoded again")

    monkeypatch.setattr(matchblock, "decode_image_features", unexpected_decode)
    second = matchblock.getmatches(
        [Picture(path) for path in paths],
        cache_path=cache_path,
        threshold=90,
        phash_distance=0,
    )

    assert len(first) == len(second) == 1
    assert second.candidate_stats.candidate_pairs == 1
    assert second.scan_receipt.status is ScanStatus.COMPLETE


def test_phash_hit_alone_never_becomes_a_match(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0, (0, 0, 0)),
            "b.png": _synthetic_features(0, (255, 255, 255)),
        },
    )

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
        phash_distance=0,
    )

    assert result == []
    assert result.candidate_stats.candidate_pairs == 1
    assert result.candidate_stats.refined_pairs == 1


def test_dimension_filter_is_preserved_unless_scaled_matching_is_enabled(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    features = {
        "a.png": _synthetic_features(0, dimensions=(30, 20)),
        "b.png": _synthetic_features(0, dimensions=(60, 40)),
    }
    _install_synthetic_decoder(monkeypatch, features)
    unscaled = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "unscaled-cache.sqlite3"),
        threshold=90,
        match_scaled=False,
        phash_distance=0,
    )
    _install_synthetic_decoder(monkeypatch, features)
    scaled = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "scaled-cache.sqlite3"),
        threshold=90,
        match_scaled=True,
        phash_distance=0,
    )

    assert unscaled == []
    assert unscaled.candidate_stats.candidate_pairs == 0
    assert len(scaled) == 1
    assert scaled.candidate_stats.candidate_pairs == 1


def test_real_decoder_index_and_legacy_block_refinement_reduce_pairs_end_to_end(tmp_path):
    generator = random.Random(20260726)
    base = Image.new("RGB", (32, 24))
    base.putdata(
        [
            ((x * 13) % 256, (y * 19) % 256, ((x * 7) + (y * 11)) % 256)
            for y in range(base.height)
            for x in range(base.width)
        ]
    )
    paths = [tmp_path / "00-base.png", tmp_path / "01-copy.png"]
    base.save(paths[0])
    base.save(paths[1])
    for index in range(10):
        image = Image.new("RGB", base.size)
        image.putdata(
            [
                (generator.randrange(256), generator.randrange(256), generator.randrange(256))
                for _ in range(base.width * base.height)
            ]
        )
        path = tmp_path / "{:02d}-noise.png".format(index + 2)
        image.save(path)
        paths.append(path)

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=95,
        phash_distance=0,
    )

    assert any({match.first.name, match.second.name} == {"00-base.png", "01-copy.png"} for match in result)
    assert all(match.percentage <= 99 for match in result)
    assert result.candidate_stats.refined_pairs == result.candidate_stats.candidate_pairs
    assert 0 < result.candidate_stats.candidate_pairs < result.candidate_stats.possible_pairs


def test_rotated_candidate_hash_and_rotated_blocks_share_one_normalization_policy(tmp_path):
    source = Image.new("RGB", (24, 12), (0, 0, 0))
    for x in range(9):
        for y in range(5):
            source.putpixel((x, y), (250, 100, 5))
    first_path = tmp_path / "a-source.png"
    second_path = tmp_path / "b-rotated.png"
    source.save(first_path)
    source.transpose(Image.Transpose.ROTATE_270).save(second_path)

    without_rotation = matchblock.getmatches(
        [Picture(first_path), Picture(second_path)],
        str(tmp_path / "plain-cache.sqlite3"),
        threshold=95,
        match_rotated=False,
        phash_distance=0,
    )
    with_rotation = matchblock.getmatches(
        [Picture(first_path), Picture(second_path)],
        str(tmp_path / "rotated-cache.sqlite3"),
        threshold=95,
        match_rotated=True,
        phash_distance=0,
    )

    assert without_rotation == []
    assert len(with_rotation) == 1
    assert with_rotation[0].percentage == 99


def test_decoder_failure_is_explicit_complete_with_skips_coverage(tmp_path):
    valid_path = tmp_path / "valid.png"
    invalid_path = tmp_path / "invalid.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(valid_path)
    invalid_path.write_bytes(b"not an image")

    result = matchblock.getmatches(
        [Picture(valid_path), Picture(invalid_path)],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
    )

    assert result == []
    assert result.scan_receipt.status is ScanStatus.COMPLETE_WITH_SKIPS
    assert result.scan_receipt.discovered == 2
    assert result.scan_receipt.analyzed == 1
    assert result.scan_receipt.failed == 1
    assert not result.scan_receipt.allows_destructive_actions
    assert result.scan_receipt.issues[0].code == "decoder_failure"


def test_decoder_memory_limit_is_not_reported_as_complete(tmp_path, monkeypatch):
    path = tmp_path / "large.png"
    path.write_bytes(b"placeholder")

    def fail(*args, **kwargs):
        raise ImageResourceLimitError("test memory limit")

    monkeypatch.setattr(matchblock, "decode_image_features", fail)
    result = matchblock.getmatches(
        [Picture(path)],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
    )

    assert result.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert result.scan_receipt.analyzed == 0
    assert result.scan_receipt.failed == 1
    assert not result.scan_receipt.allows_destructive_actions


def test_missing_pillow_is_a_fatal_explicit_receipt_not_a_legacy_fallback(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))

    def unavailable(*args, **kwargs):
        raise DecoderUnavailableError("Pillow unavailable in test")

    monkeypatch.setattr(matchblock, "decode_image_features", unavailable)
    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
    )

    assert result == []
    assert result.scan_receipt.status is ScanStatus.FAILED
    assert result.scan_receipt.analyzed == 0
    assert result.scan_receipt.failed == 1
    assert result.scan_receipt.issues[0].code == "decoder_unavailable"


def test_file_changed_during_decode_is_skipped_instead_of_caching_stale_features(tmp_path, monkeypatch):
    path = tmp_path / "changing.png"
    path.write_bytes(b"before")
    features = _synthetic_features(0)

    def mutate_while_decoding(path_str, block_count_per_side, include_orientations=False):
        Path(path_str).write_bytes(b"changed-during-decode")
        return features

    monkeypatch.setattr(matchblock, "decode_image_features", mutate_while_decoding)
    result = matchblock.getmatches(
        [Picture(path)],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
    )

    assert result.scan_receipt.status is ScanStatus.COMPLETE_WITH_SKIPS
    assert result.scan_receipt.analyzed == 0
    assert result.scan_receipt.failed == 1
    assert "changed" in result.scan_receipt.issues[0].message


def test_same_size_restored_mtime_change_during_decode_is_skipped(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "changing.png"
    path.write_bytes(b"before")
    original_stat = path.stat()
    features = _synthetic_features(0)

    def mutate_while_decoding(
        path_str,
        block_count_per_side,
        include_orientations=False,
    ):
        Path(path_str).write_bytes(b"after!")
        os.utime(
            path_str,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        return features

    monkeypatch.setattr(
        matchblock,
        "decode_image_features",
        mutate_while_decoding,
    )
    cache_path = tmp_path / "cache.sqlite3"
    result = matchblock.getmatches(
        [Picture(path)],
        str(cache_path),
        threshold=80,
    )

    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert result.scan_receipt.status is ScanStatus.COMPLETE_WITH_SKIPS
    assert result.scan_receipt.analyzed == 0
    assert result.scan_receipt.failed == 1
    assert "changed" in result.scan_receipt.issues[0].message
    cache = matchblock.SqliteCache(cache_path)
    with pytest.raises(KeyError):
        cache.get_features(path)
    cache.close()


def test_candidate_index_memory_error_is_not_reported_as_complete(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0),
            "b.png": _synthetic_features(0),
        },
    )

    def out_of_memory(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(MultiIndexHamming, "iter_query", out_of_memory)
    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=80,
    )

    assert result == []
    assert result.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert result.scan_receipt.analyzed == 2
    assert not result.scan_receipt.allows_destructive_actions


def test_bounded_worker_refinement_reads_candidates_from_persistent_cache(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png", "c.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0),
            "b.png": _synthetic_features(0),
            "c.png": _synthetic_features((1 << 64) - 1),
        },
    )
    monkeypatch.setattr(matchblock, "MIN_MULTIPROCESS_PICTURES", 3)

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=90,
        phash_distance=0,
    )

    assert [(item.first.name, item.second.name, item.percentage) for item in result] == [("a.png", "b.png", 99)]
    assert result.candidate_stats.candidate_pairs == 1
    assert result.candidate_stats.refined_pairs == 1
    assert result.scan_receipt.status is ScanStatus.COMPLETE


def test_scanner_exposes_receipt_and_never_promotes_visual_match_to_exact(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.png", "b.png")]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {
            "a.png": _synthetic_features(0),
            "b.png": _synthetic_features(0),
        },
    )
    scanner = ScannerPE()
    scanner.scan_type = ScanType.FUZZYBLOCK
    scanner.cache_path = str(tmp_path / "cache.sqlite3")
    scanner.phash_distance = 0

    [group] = scanner.get_dupe_groups([Picture(path) for path in paths])

    assert scanner.scan_receipt.status is ScanStatus.COMPLETE
    assert scanner.candidate_stats.candidate_pairs == 1
    assert scanner.candidate_stats.max_candidate_pairs == matchblock.DEFAULT_MAX_CANDIDATE_PAIRS
    assert scanner.candidate_stats.max_refined_pairs == matchblock.DEFAULT_MAX_REFINED_PAIRS
    assert scanner.candidate_stats.max_matches == matchblock.DEFAULT_MAX_MATCHES
    assert group.verification_kind is engine.VerificationKind.SIMILAR
    assert group.percentage == 99


def test_ten_thousand_image_index_avoids_all_pairs_materialization():
    generator = random.Random(932_741)
    count = 10_000
    hashes = [generator.getrandbits(64) for _ in range(count)]
    for offset in range(0, count, 1000):
        hashes[offset + 1] = hashes[offset] ^ 1
    pictures = [SimpleNamespace(unicode_path="asset-{:05d}".format(index), is_ref=False) for index in range(count)]
    features = {
        picture.unicode_path: SimpleNamespace(
            phashes=(hashes[index],),
            dhashes=(hashes[index],),
            color_histogram=(1024,) + (0,) * 63,
            dimensions=(100, 100),
            rowid=index + 1,
        )
        for index, picture in enumerate(pictures)
    }
    index = MultiIndexHamming(bit_width=64, max_distance=3)
    for picture in pictures:
        index.add(picture.unicode_path, features[picture.unicode_path].phashes[0])
    counters = {
        "candidate_pairs": 0,
        "pictures_by_path": {picture.unicode_path: picture for picture in pictures},
    }

    batches = list(
        matchblock._iter_candidate_batches(
            pictures,
            features,
            index,
            max_distance=3,
            match_scaled=False,
            match_rotated=False,
            counters=counters,
            j=matchblock.job.nulljob,
        )
    )
    emitted = sum(len(batch) for batch in batches)
    possible = count * (count - 1) // 2

    assert emitted == counters["candidate_pairs"]
    assert emitted >= 10
    assert emitted < 100
    assert possible == 49_995_000
    assert emitted < possible // 100_000


def test_dense_ten_thousand_image_candidates_stop_at_explicit_budget():
    count = 10_000
    limit = 1_234
    pictures = [
        SimpleNamespace(
            unicode_path="asset-{:05d}".format(index),
            is_ref=False,
        )
        for index in range(count)
    ]
    features = {
        picture.unicode_path: SimpleNamespace(
            phashes=(0,),
            dhashes=(0,),
            color_histogram=(1024,) + (0,) * 63,
            dimensions=(100, 100),
            rowid=index + 1,
        )
        for index, picture in enumerate(pictures)
    }
    index = MultiIndexHamming(bit_width=64, max_distance=0)
    for picture in pictures:
        index.add(picture.unicode_path, 0)
    counters = {
        "candidate_pairs": 0,
        "candidate_limit_reached": False,
        "pictures_by_path": {picture.unicode_path: picture for picture in pictures},
    }

    tracemalloc.start()
    batches = tuple(
        matchblock._iter_candidate_batches(
            pictures,
            features,
            index,
            max_distance=0,
            match_scaled=False,
            match_rotated=False,
            counters=counters,
            j=matchblock.job.nulljob,
            max_candidate_pairs=limit,
        )
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert sum(len(batch) for batch in batches) == limit
    assert counters["candidate_pairs"] == limit
    assert counters["candidate_limit_reached"]
    assert peak < 32 * 1024 * 1024


def test_candidate_budget_returns_bounded_resource_limit_receipt(
    tmp_path,
    monkeypatch,
):
    paths = [tmp_path / "{:02d}.png".format(index) for index in range(10)]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {path.name: _synthetic_features(0) for path in paths},
    )
    monkeypatch.setattr(matchblock, "MIN_MULTIPROCESS_PICTURES", 5)

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=90,
        phash_distance=0,
        max_candidate_pairs=25,
        max_refined_pairs=100,
        max_matches=100,
    )

    assert result.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert not result.scan_receipt.complete
    assert not result.scan_receipt.allows_destructive_actions
    assert result.scan_receipt.issues[-1].code == "candidate_pair_limit"
    assert result.candidate_stats.candidate_pairs == 25
    assert result.candidate_stats.refined_pairs == 25
    assert len(result) == 25
    assert result.candidate_stats.candidate_limit_reached
    assert not result.candidate_stats.refinement_limit_reached
    assert not result.candidate_stats.match_limit_reached


def test_refinement_budget_returns_bounded_resource_limit_receipt(
    tmp_path,
    monkeypatch,
):
    paths = [tmp_path / "{:02d}.png".format(index) for index in range(5)]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {path.name: _synthetic_features(0) for path in paths},
    )
    monkeypatch.setattr(matchblock, "MIN_MULTIPROCESS_PICTURES", 5)

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=90,
        phash_distance=0,
        max_candidate_pairs=100,
        max_refined_pairs=4,
        max_matches=100,
    )

    assert result.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert result.candidate_stats.candidate_pairs == 10
    assert result.candidate_stats.refined_pairs == 4
    assert len(result) == 4
    assert result.candidate_stats.refinement_limit_reached
    assert not result.candidate_stats.candidate_limit_reached


def test_match_budget_bounds_materialized_matches_and_marks_partial(
    tmp_path,
    monkeypatch,
):
    paths = [tmp_path / "{:02d}.png".format(index) for index in range(5)]
    for path in paths:
        path.write_bytes(path.name.encode("ascii"))
    _install_synthetic_decoder(
        monkeypatch,
        {path.name: _synthetic_features(0) for path in paths},
    )
    monkeypatch.setattr(matchblock, "MIN_MULTIPROCESS_PICTURES", 5)

    result = matchblock.getmatches(
        [Picture(path) for path in paths],
        str(tmp_path / "cache.sqlite3"),
        threshold=90,
        phash_distance=0,
        max_candidate_pairs=100,
        max_refined_pairs=100,
        max_matches=3,
    )

    assert result.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert len(result) == 3
    assert result.candidate_stats.match_count == 3
    assert (
        result.candidate_stats.match_count
        <= result.candidate_stats.refined_pairs
        <= result.candidate_stats.max_refined_pairs
    )
    assert result.candidate_stats.match_limit_reached
    assert result.scan_receipt.issues[-1].code == "match_limit"
