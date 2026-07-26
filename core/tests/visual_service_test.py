import copy
import json
import os
import time
import tracemalloc
from pathlib import Path

import pytest
from PIL import Image

import core.visual_service as visual_service
from core.file_generation import FileGenerationToken
from core.file_identity import (
    IdentityCapability,
    IdentityConfidence,
    get_file_identity,
)
from core.safe_walk import WalkEvent, WalkEventKind
from core.scan_receipt import ScanStatus
from core.pe.candidate_index import CandidateQueryBudget
from core.visual_service import (
    MAX_VISUAL_ARTIFACT_JSON_BYTES,
    VISUAL_ARTIFACT_SCHEMA_VERSION,
    VISUAL_ARTIFACT_JSON_LIMITS,
    VISUAL_REPORT_SCHEMA_VERSION,
    VisualAssetSnapshot,
    VisualFeatureArtifact,
    VisualRelation,
    VisualReport,
    VisualReportKind,
    VisualScanConfig,
    VisualService,
)
from core.pe.image_features import ImageQuality, TileFingerprint


def _write_image(path, color=(30, 90, 170), size=(48, 32)):
    Image.new("RGB", size, color).save(path)


def _snapshot(index, root):
    path = Path(root, "synthetic-{:06d}.png".format(index)).absolute()
    return VisualAssetSnapshot(
        asset_id="asset:{:08d}".format(index),
        path=str(path),
        root=str(Path(root).absolute()),
        size=100 + index,
        mtime_ns=1,
        generation_token=FileGenerationToken(
            "test-visual-generation",
            index + 1,
        ).encoded.hex(),
        identity_namespace="windows",
        identity_capability=IdentityCapability.WINDOWS_FILE_INDEX_64.value,
        identity_confidence=int(IdentityConfidence.MEDIUM),
        volume_id=1,
        file_id_kind="integer",
        file_id=str(index + 1),
    )


def _artifact(
    index,
    root,
    phash=None,
    *,
    dhash=None,
    histogram=None,
    with_tiles=False,
):
    fingerprint = index if phash is None else phash
    secondary = fingerprint if dhash is None else dhash
    tiles = (
        (
            TileFingerprint("center_90", fingerprint, secondary, (500, 500, 9500, 9500)),
            TileFingerprint("center_75", fingerprint, secondary, (1250, 1250, 8750, 8750)),
            TileFingerprint("center_50", fingerprint, secondary, (2500, 2500, 7500, 7500)),
            TileFingerprint("content", fingerprint, secondary, (1000, 1000, 9000, 9000)),
        )
        if with_tiles
        else ()
    )
    return VisualFeatureArtifact(
        asset=_snapshot(index, root),
        dimensions=(48, 32),
        frame_count=1,
        phashes=(fingerprint,),
        dhashes=(secondary,),
        color_histogram=histogram or ((1024,) + (0,) * 63),
        tile_fingerprints=tiles,
        quality=ImageQuality(8, 0, 0, 0.0),
        thumbnail_key="{:064x}".format(index + 1),
        cache_record_id=index + 1,
    )


class _NoBlockReads:
    def get_multiple(self, _rowids):
        raise AssertionError("a zero-candidate scan must not load RGB blocks")


class _DenseBlocks:
    _blocks = (tuple([(20, 40, 60)] * (15 * 15)),)

    def get_multiple(self, rowids):
        return iter((rowid, self._blocks) for rowid in rowids)


def test_visual_scan_is_read_only_and_never_promotes_identical_files_to_exact(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    first = root / "first.png"
    second = root / "second.png"
    _write_image(first)
    second.write_bytes(first.read_bytes())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)}

    report = VisualService(cache_path=tmp_path / "cache.sqlite3").scan_roots(
        (root,),
        config=VisualScanConfig(
            similarity_threshold=90,
            phash_radius=0,
        ),
    )

    assert report.scan_receipt.status is ScanStatus.COMPLETE
    assert report.scan_receipt.complete
    assert not report.scan_receipt.allows_destructive_actions
    assert not report.allows_destructive_actions
    assert len(report.artifacts) == 2
    assert len(report.evidence) == 1
    assert report.evidence[0].relation is VisualRelation.SIMILAR
    assert not report.evidence[0].allows_destructive_actions
    assert all(not hasattr(artifact, "blocks") for artifact in report.artifacts)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)} == before

    payload = report.to_dict()
    encoded = report.to_json()
    assert payload["schema_version"] == VISUAL_REPORT_SCHEMA_VERSION
    assert payload["scan_receipt"]["allows_destructive_actions"] is False
    assert payload["safety"]["verified_exact_evidence"] is False
    assert payload["safety"]["destructive_actions_allowed"] is False
    assert '"blocks":' not in encoded
    assert len(encoded) < 12_000


def test_artifact_is_lightweight_and_catalog_payload_is_portable(tmp_path):
    artifact = _artifact(7, tmp_path, phash=0x1234)

    data = artifact.to_dict()
    report_data = artifact.to_report_dict()
    catalog = artifact.to_catalog_payload()
    restored = VisualFeatureArtifact.from_json(catalog["value"])

    assert data["schema_version"] == VISUAL_ARTIFACT_SCHEMA_VERSION
    assert "blocks" not in data["feature"]
    assert report_data["asset_id"] == artifact.asset_id
    assert "asset" not in report_data
    assert json.loads(catalog["value"])["feature"]["cache_record_id"] is None
    assert restored.asset == artifact.asset
    assert restored.phashes == artifact.phashes
    assert restored.dhashes == artifact.dhashes
    assert restored.color_histogram == artifact.color_histogram
    assert restored.quality == artifact.quality
    assert restored.cache_record_id == 0
    assert catalog["verification_level"] == "candidate"


def test_config_rejects_unbounded_or_invalid_limits():
    for values in (
        {"max_images": 0},
        {"max_candidate_pairs": 0},
        {"max_matches": 0},
        {"max_seconds": 0},
        {"max_seconds": float("inf")},
        {"dhash_distance": 65},
        {"color_histogram_distance": float("nan")},
        {"match_crops": 1},
        {"match_scaled": 1},
        {"dry_run": False},
    ):
        with pytest.raises(ValueError):
            VisualScanConfig(**values)


def test_bounded_tile_hit_is_only_a_review_only_crop_candidate(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    original = root / "original.png"
    cropped = root / "cropped.png"
    image = Image.new("RGB", (120, 80))
    image.putdata(
        [
            (
                (x * 7 + y * 3) % 256,
                (x * 11 + y * 13) % 256,
                (x * y + x * 5) % 256,
            )
            for y in range(80)
            for x in range(120)
        ]
    )
    image.save(original)
    image.crop((15, 10, 105, 70)).save(cropped)

    report = VisualService(cache_path=tmp_path / "cache.sqlite3").scan_roots(
        root,
        config=VisualScanConfig(
            similarity_threshold=99,
            phash_radius=0,
            include_related=False,
        ),
    )

    assert len(report.evidence) == 1
    evidence = report.evidence[0]
    assert evidence.relation is VisualRelation.CROP_CANDIDATE
    assert evidence.crop_verification == "bounded_fingerprint_candidate"
    assert evidence.first_fingerprint_kind != "whole" or evidence.second_fingerprint_kind != "whole"
    assert evidence.first_fingerprint_box != (0, 0, 10_000, 10_000) or evidence.second_fingerprint_box != (
        0,
        0,
        10_000,
        10_000,
    )
    assert evidence.to_dict()["safety"] == {
        "verified_exact": False,
        "destructive_actions_allowed": False,
    }
    assert report.to_dict()["safety"]["destructive_actions_allowed"] is False


def test_ten_thousand_image_candidate_index_keeps_blocks_off_heap(tmp_path):
    artifacts = tuple(_artifact(index, tmp_path) for index in range(10_000))
    config = VisualScanConfig(phash_radius=0, max_images=20_000)
    state = visual_service._RunState(time.time_ns(), config.max_seconds)
    service = VisualService()

    tracemalloc.start()
    evidence, stats = service._scan_evidence(
        artifacts,
        config,
        _NoBlockReads(),
        state=state,
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert evidence == ()
    assert stats.indexed_images == 10_000
    assert stats.possible_pairs == 49_995_000
    assert stats.candidate_pairs == 0
    assert peak < 32 * 1024 * 1024
    assert all(not hasattr(artifact, "blocks") for artifact in artifacts)


def test_dense_candidates_stop_at_budget_with_valid_partial_report(tmp_path):
    artifacts = tuple(_artifact(index, tmp_path, phash=0) for index in range(2_000))
    config = VisualScanConfig(
        phash_radius=0,
        max_images=3_000,
        max_candidate_pairs=1_234,
        max_matches=2_000,
    )
    state = visual_service._RunState(time.time_ns(), config.max_seconds)
    service = VisualService()

    evidence, stats = service._scan_evidence(
        artifacts,
        config,
        _DenseBlocks(),
        state=state,
    )
    receipt = state.receipt(len(artifacts), len(artifacts))
    report = VisualReport(
        report_id="bounded",
        kind=VisualReportKind.SCAN,
        roots=(str(tmp_path.absolute()),),
        config=config,
        assets=tuple(artifact.asset for artifact in artifacts),
        artifacts=artifacts,
        evidence=evidence,
        candidate_stats=stats,
        scan_receipt=receipt,
    )

    assert receipt.status is ScanStatus.RESOURCE_LIMIT
    assert not receipt.complete
    assert not receipt.allows_destructive_actions
    assert stats.candidate_pairs == config.max_candidate_pairs
    assert stats.refined_pairs <= stats.candidate_pairs
    assert len(evidence) <= config.max_matches
    assert not report.allows_destructive_actions
    assert '"blocks":' not in report.to_json()


def test_secondary_features_rank_best_candidate_before_pair_budget(tmp_path):
    reference = _artifact(0, tmp_path, phash=0, dhash=0)
    weaker = _artifact(
        1,
        tmp_path,
        phash=0,
        dhash=(1 << 20) - 1,
        histogram=(0,) * 63 + (1024,),
    )
    stronger = _artifact(2, tmp_path, phash=0, dhash=1)
    config = VisualScanConfig(
        phash_radius=0,
        dhash_distance=64,
        color_histogram_distance=1,
        max_candidate_pairs=1,
        max_matches=1,
    )
    state = visual_service._RunState(time.time_ns(), config.max_seconds)

    evidence, stats = VisualService()._scan_evidence(
        (reference, weaker, stronger),
        config,
        _DenseBlocks(),
        state=state,
    )

    assert stats.candidate_pairs == 1
    assert len(evidence) == 1
    assert {evidence[0].first_id, evidence[0].second_id} == {
        reference.asset_id,
        stronger.asset_id,
    }


def test_dense_ten_thousand_tile_index_stays_bounded(tmp_path):
    artifacts = tuple(_artifact(index, tmp_path, phash=0, with_tiles=True) for index in range(10_000))
    config = VisualScanConfig(
        phash_radius=0,
        max_images=10_001,
        max_candidate_pairs=1_234,
        max_matches=1_234,
    )
    state = visual_service._RunState(time.time_ns(), config.max_seconds)

    tracemalloc.start()
    evidence, stats = VisualService()._scan_evidence(
        artifacts,
        config,
        _DenseBlocks(),
        state=state,
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert stats.candidate_pairs == config.max_candidate_pairs
    assert len(evidence) <= config.max_matches
    assert state.resource_limited
    assert any(
        "candidate examination reached its budget" in issue.message
        and "examining 51234 index entries" in issue.message
        and "limit 51234" in issue.message
        for issue in state.issues
    )
    assert peak < 128 * 1024 * 1024


def test_candidate_examination_budget_counts_canonical_rejects_and_marks_partial(tmp_path):
    artifacts = tuple(_artifact(index, tmp_path, phash=0) for index in range(32))
    config = VisualScanConfig(phash_radius=0, max_candidate_pairs=3)
    service = VisualService()
    index, indexed_entries = service._candidate_index(
        artifacts,
        config,
        None,
        None,
    )
    budget = CandidateQueryBudget(7)
    state = visual_service._RunState(time.time_ns(), config.max_seconds)

    candidates, lookup_truncated, budget_exhausted = service._candidate_hits(
        artifacts[-1],
        index,
        indexed_entries,
        {artifact.asset_id: artifact for artifact in artifacts},
        config,
        state,
        None,
        query_budget=budget,
        max_results=3,
        canonical_only=True,
    )
    receipt = state.receipt(len(artifacts), len(artifacts))

    assert candidates == ()
    assert not lookup_truncated
    assert budget_exhausted
    assert budget.examined == 7
    assert receipt.status is ScanStatus.RESOURCE_LIMIT
    assert not receipt.complete
    assert any(
        "after examining 7 index entries" in issue.message and "limit 7" in issue.message for issue in receipt.issues
    )


def test_dense_candidate_lookup_checks_cancellation_inside_bucket(tmp_path):
    artifacts = tuple(_artifact(index, tmp_path, phash=0) for index in range(32))
    config = VisualScanConfig(phash_radius=0, max_candidate_pairs=100)
    service = VisualService()
    index, indexed_entries = service._candidate_index(
        artifacts,
        config,
        None,
        None,
    )
    budget = CandidateQueryBudget(100)
    state = visual_service._RunState(time.time_ns(), config.max_seconds)
    checks = 0

    def cancel_during_bucket():
        nonlocal checks
        checks += 1
        return checks >= 9

    candidates, lookup_truncated, budget_exhausted = service._candidate_hits(
        artifacts[-1],
        index,
        indexed_entries,
        {artifact.asset_id: artifact for artifact in artifacts},
        config,
        state,
        cancel_during_bucket,
        query_budget=budget,
        max_results=100,
        canonical_only=True,
    )

    assert candidates == ()
    assert not lookup_truncated
    assert not budget_exhausted
    assert checks == 9
    assert budget.examined == 7
    assert state.cancelled
    assert not state.resource_limited


def test_dense_candidate_lookup_checks_deadline_inside_bucket(monkeypatch, tmp_path):
    artifacts = tuple(_artifact(index, tmp_path, phash=0) for index in range(32))
    config = VisualScanConfig(phash_radius=0, max_candidate_pairs=100)
    service = VisualService()
    index, indexed_entries = service._candidate_index(
        artifacts,
        config,
        None,
        None,
    )
    budget = CandidateQueryBudget(100)
    state = visual_service._RunState(0, max_seconds=1)
    clock_checks = 0

    def deadline_after_seven_entries():
        nonlocal clock_checks
        clock_checks += 1
        if clock_checks >= 9:
            return 1_000_000_000
        return 0

    monkeypatch.setattr(visual_service.time, "time_ns", deadline_after_seven_entries)

    candidates, lookup_truncated, budget_exhausted = service._candidate_hits(
        artifacts[-1],
        index,
        indexed_entries,
        {artifact.asset_id: artifact for artifact in artifacts},
        config,
        state,
        None,
        query_budget=budget,
        max_results=100,
        canonical_only=True,
    )

    assert candidates == ()
    assert not lookup_truncated
    assert not budget_exhausted
    assert clock_checks == 9
    assert budget.examined == 7
    assert state.resource_limited
    assert state.hard_stop
    assert any("max_seconds" in issue.message for issue in state.issues)


def test_image_budget_returns_analyzed_bounded_partial_report(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    for name in ("a.png", "b.png", "c.png"):
        _write_image(root / name)

    report = VisualService(cache_path=tmp_path / "cache.sqlite3").scan_roots(
        (root,),
        config=VisualScanConfig(
            max_images=2,
            max_candidate_pairs=10,
            max_matches=10,
        ),
    )

    assert report.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert report.scan_receipt.discovered == 3
    assert report.scan_receipt.analyzed == 2
    assert len(report.artifacts) == 2
    assert len(report.evidence) == 1
    assert not report.allows_destructive_actions


def test_cache_inside_source_root_fails_before_creating_database(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    source = root / "source.png"
    _write_image(source)
    cache_path = root / "cache.sqlite3"
    before = source.read_bytes()

    report = VisualService(cache_path=cache_path).scan_roots((root,))

    assert report.scan_receipt.status is ScanStatus.FAILED
    assert any(issue.code == "cache_validation_failed" for issue in report.scan_receipt.issues)
    assert not cache_path.exists()
    assert source.read_bytes() == before


def test_cache_hardlink_alias_is_rejected_without_modifying_source(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    source = root / "source.png"
    _write_image(source)
    cache_alias = tmp_path / "cache.sqlite3"
    try:
        os.link(source, cache_alias)
    except OSError as error:
        pytest.skip("hard links are unavailable: {}".format(error))
    before = source.read_bytes()

    report = VisualService(cache_path=cache_alias).scan_roots((root,))

    assert report.scan_receipt.status is ScanStatus.FAILED
    assert source.read_bytes() == before
    assert cache_alias.read_bytes() == before
    assert os.path.samefile(source, cache_alias)


def test_source_change_during_decode_is_reported_and_not_cached_as_evidence(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    source = root / "source.png"
    _write_image(source)
    from core.pe.image_features import decode_image_features

    def racing_decoder(path, **kwargs):
        features = decode_image_features(path, **kwargs)
        with open(path, "ab") as stream:
            stream.write(b"changed")
        return features

    report = VisualService(
        cache_path=tmp_path / "cache.sqlite3",
        feature_decoder=racing_decoder,
    ).scan_roots((root,))

    assert report.scan_receipt.status in {
        ScanStatus.FAILED,
        ScanStatus.COMPLETE_WITH_SKIPS,
    }
    assert any(issue.code == "source_generation_changed" for issue in report.scan_receipt.issues)
    assert report.artifacts == ()
    assert report.evidence == ()
    assert not report.allows_destructive_actions


def test_same_size_restored_mtime_change_during_decode_is_not_cached(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    source = root / "source.png"
    _write_image(source)
    original_stat = source.stat()
    from core.pe.image_features import decode_image_features

    def racing_decoder(path, **kwargs):
        features = decode_image_features(path, **kwargs)
        changed = bytearray(Path(path).read_bytes())
        changed[-1] ^= 1
        Path(path).write_bytes(changed)
        os.utime(
            path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        return features

    cache_path = tmp_path / "cache.sqlite3"
    report = VisualService(
        cache_path=cache_path,
        feature_decoder=racing_decoder,
    ).scan_roots((root,))

    assert report.scan_receipt.status in {
        ScanStatus.FAILED,
        ScanStatus.COMPLETE_WITH_SKIPS,
    }
    assert any(issue.code == "source_generation_changed" for issue in report.scan_receipt.issues)
    assert report.artifacts == ()
    assert report.evidence == ()
    cache = visual_service.SqliteCache(cache_path)
    with pytest.raises(KeyError):
        cache.get_features(source)
    cache.close()


def test_reserved_internal_directories_are_pruned_and_cannot_be_roots(tmp_path):
    root = tmp_path / "pictures"
    reserved = root / ".dupeguru-neo-quarantine"
    root.mkdir()
    reserved.mkdir()
    normal = root / "normal.png"
    hidden = reserved / "hidden.png"
    _write_image(normal)
    hidden.write_bytes(normal.read_bytes())

    report = VisualService(cache_path=tmp_path / "cache.sqlite3").scan_roots((root,))
    rejected = VisualService(cache_path=tmp_path / "other-cache.sqlite3").scan_roots((reserved,))

    assert report.scan_receipt.status is ScanStatus.COMPLETE
    assert len(report.artifacts) == 1
    assert report.artifacts[0].asset.path == str(normal.absolute())
    assert rejected.scan_receipt.status is ScanStatus.FAILED
    assert any(issue.code == "reserved_internal_root" for issue in rejected.scan_receipt.issues)


def test_scan_cancel_hook_returns_fail_closed_receipt(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    _write_image(root / "one.png")

    report = VisualService(cache_path=tmp_path / "cache.sqlite3").scan_roots(
        (root,),
        cancel_check=lambda: True,
    )

    assert report.scan_receipt.status is ScanStatus.CANCELLED
    assert not report.scan_receipt.complete
    assert not report.allows_destructive_actions
    assert report.evidence == ()


def test_walker_event_outside_root_is_rejected_even_with_injected_walker(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    outside = tmp_path / "outside.png"
    _write_image(outside)
    identity = get_file_identity(outside, follow_symlinks=False)

    def escaped_walker(_root, **_options):
        yield WalkEvent(WalkEventKind.FILE, outside, identity=identity)

    report = VisualService(
        cache_path=tmp_path / "cache.sqlite3",
        walker=escaped_walker,
    ).scan_roots(root)

    assert report.scan_receipt.status is ScanStatus.FAILED
    assert report.artifacts == ()
    assert any(issue.code == "walk_event_outside_root" for issue in report.scan_receipt.issues)


def test_catalog_artifact_rejects_a_destructive_safety_declaration(tmp_path):
    data = _artifact(1, tmp_path).to_dict()
    data["safety"]["verified_exact"] = True
    data["safety"]["destructive_actions_allowed"] = True

    with pytest.raises(ValueError, match="safety"):
        VisualFeatureArtifact.from_dict(data)


def test_artifact_json_rejects_oversize_before_utf8_decode(
    monkeypatch,
):
    def unexpected_json_decode(*_args, **_kwargs):
        raise AssertionError("oversized input must not reach json.loads")

    monkeypatch.setattr(
        visual_service.json,
        "loads",
        unexpected_json_decode,
    )

    with pytest.raises(ValueError, match="byte length"):
        VisualFeatureArtifact.from_json(b"\xff" * (MAX_VISUAL_ARTIFACT_JSON_BYTES + 1))


def test_artifact_json_string_uses_utf8_byte_limit(monkeypatch):
    def unexpected_json_decode(*_args, **_kwargs):
        raise AssertionError("oversized input must not reach json.loads")

    monkeypatch.setattr(
        visual_service.json,
        "loads",
        unexpected_json_decode,
    )

    with pytest.raises(ValueError, match="byte length"):
        VisualFeatureArtifact.from_json("\N{GRINNING FACE}" * (MAX_VISUAL_ARTIFACT_JSON_BYTES // 4 + 1))


def test_artifact_json_preflights_depth_before_json_decoder(monkeypatch):
    def unexpected_json_decode(*_args, **_kwargs):
        raise AssertionError("structural limits must run before json.loads")

    monkeypatch.setattr(
        visual_service.json,
        "loads",
        unexpected_json_decode,
    )

    with pytest.raises(ValueError, match="depth"):
        VisualFeatureArtifact.from_json("[" * (VISUAL_ARTIFACT_JSON_LIMITS.max_depth + 1))


def test_artifact_json_rejects_duplicate_object_keys(tmp_path):
    encoded = _artifact(1, tmp_path).to_json()
    duplicate = '{"schema":"wrong",' + encoded[1:]

    with pytest.raises(ValueError, match="strict JSON"):
        VisualFeatureArtifact.from_json(duplicate)


@pytest.mark.parametrize(
    "number",
    (float("nan"), float("inf"), float("-inf")),
)
def test_artifact_json_rejects_nonfinite_constants(tmp_path, number):
    data = _artifact(1, tmp_path).to_dict()
    data["feature"]["quality"]["jpeg_artifact_score"] = number
    encoded = json.dumps(
        data,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="strict JSON"):
        VisualFeatureArtifact.from_json(encoded)


def test_artifact_json_rejects_finite_syntax_that_overflows_float(tmp_path):
    encoded = (
        _artifact(1, tmp_path)
        .to_json()
        .replace(
            '"jpeg_artifact_score":0.0',
            '"jpeg_artifact_score":1e9999',
        )
    )

    with pytest.raises(ValueError, match="strict JSON"):
        VisualFeatureArtifact.from_json(encoded)


@pytest.mark.parametrize(
    "field_path,replacement",
    (
        (("asset", "size"), "100"),
        (("asset", "identity", "confidence"), True),
        (("feature", "dimensions"), (48, 32)),
        (("feature", "frame_count"), True),
        (("feature", "phashes"), ("0000000000000001",)),
        (("feature", "cache_record_id"), True),
        (("feature", "tile_fingerprints", 0, "box"), (500, 500, 9500, 9500)),
        (("feature", "quality", "bit_depth"), True),
    ),
)
def test_artifact_dict_rejects_noncanonical_nested_types(
    tmp_path,
    field_path,
    replacement,
):
    data = copy.deepcopy(_artifact(1, tmp_path, with_tiles=True).to_dict())
    target = data
    for item in field_path[:-1]:
        target = target[item]
    target[field_path[-1]] = replacement

    with pytest.raises(ValueError):
        VisualFeatureArtifact.from_dict(data)


@pytest.mark.parametrize(
    "field_path",
    (
        ("asset",),
        ("asset", "identity"),
        ("feature",),
        ("feature", "tile_fingerprints", 0),
        ("feature", "quality"),
    ),
)
def test_artifact_dict_rejects_unknown_nested_fields(tmp_path, field_path):
    data = copy.deepcopy(_artifact(1, tmp_path, with_tiles=True).to_dict())
    target = data
    for item in field_path:
        target = target[item]
    target["unexpected"] = "value"

    with pytest.raises(ValueError, match="unsupported"):
        VisualFeatureArtifact.from_dict(data)


@pytest.mark.parametrize(
    "value",
    (
        "0" * 15,
        "0" * 17,
        " " + "0" * 15,
        "+" + "0" * 15,
        "A" + "0" * 15,
        "g" + "0" * 15,
    ),
)
def test_artifact_hashes_require_canonical_fixed_width_hex(tmp_path, value):
    data = _artifact(1, tmp_path).to_dict()
    data["feature"]["phashes"][0] = value

    with pytest.raises(ValueError, match="fixed-width lowercase"):
        VisualFeatureArtifact.from_dict(data)


@pytest.mark.parametrize("value", ("+2", "02", " 2"))
def test_artifact_integer_file_id_requires_canonical_text(tmp_path, value):
    data = _artifact(1, tmp_path).to_dict()
    data["asset"]["identity"]["file_id"] = value

    with pytest.raises(ValueError, match="canonical"):
        VisualFeatureArtifact.from_dict(data)
