import hashlib
import os

import pytest

from core.pe import asset_bundle
from core.pe.asset_bundle import (
    AssetBundle,
    SidecarAsset,
    SidecarIssueKind,
    SidecarNaming,
    SidecarPolicy,
    SidecarReadStatus,
    build_asset_bundles,
    audit_sidecar_conflicts,
)


def issue_kinds(issues):
    return [issue.kind for issue in issues]


def test_sidecar_slot_lookup_uses_the_prebuilt_index():
    sidecars = tuple(
        SidecarAsset(
            "asset{}".format(index),
            ".slot{}".format(index),
            1,
            bytes((index,)),
            SidecarReadStatus.OK,
        )
        for index in range(200)
    )
    bundle = AssetBundle("asset", "image.jpg", sidecars)

    class NoIterationTuple(tuple):
        def __iter__(self):
            raise AssertionError("sidecars_for must not rescan sidecars")

    object.__setattr__(
        bundle,
        "sidecars",
        NoIterationTuple(bundle.sidecars),
    )

    for index in range(200):
        assert bundle.sidecars_for(".slot{}".format(index)) == (sidecars[index],)


def test_bundle_builder_reports_ambiguous_orphan_and_required_sidecars(tmp_path):
    first_jpg = tmp_path / "same.jpg"
    first_png = tmp_path / "same.png"
    other = tmp_path / "other.jpg"
    ambiguous = tmp_path / "same.txt"
    attached = tmp_path / "other.jpg.txt"
    orphan = tmp_path / "orphan.txt"
    ambiguous.write_text("caption", encoding="utf-8")
    attached.write_text("other caption", encoding="utf-8")
    orphan.write_text("orphan", encoding="utf-8")
    catalog = build_asset_bundles(
        {"jpg": first_jpg, "png": first_png, "other": other},
        [ambiguous, attached, orphan],
        SidecarPolicy(
            extensions=(".txt",),
            required_extensions=(".txt",),
            naming=SidecarNaming.BOTH,
        ),
    )
    assert issue_kinds(catalog.issues).count(SidecarIssueKind.AMBIGUOUS_OWNER) == 1
    assert issue_kinds(catalog.issues).count(SidecarIssueKind.ORPHAN) == 1
    assert issue_kinds(catalog.issues).count(SidecarIssueKind.MISSING_REQUIRED) == 2
    assert [sidecar.path for sidecar in catalog.by_id()["other"].sidecars] == [str(attached)]


def test_bundle_builder_reports_invalid_utf8_and_multiple_same_slot(tmp_path):
    primary = tmp_path / "image.jpg"
    stem_sidecar = tmp_path / "image.txt"
    full_sidecar = tmp_path / "image.jpg.txt"
    stem_sidecar.write_bytes(b"\xff")
    full_sidecar.write_text("valid", encoding="utf-8")
    catalog = build_asset_bundles(
        {"image": primary},
        [stem_sidecar, full_sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.BOTH),
    )
    assert SidecarIssueKind.INVALID_UTF8 in issue_kinds(catalog.issues)
    assert SidecarIssueKind.MULTIPLE_FOR_SLOT in issue_kinds(catalog.issues)


def test_conflict_audit_distinguishes_presence_and_content_mismatch(tmp_path):
    primary_a = tmp_path / "a.jpg"
    primary_b = tmp_path / "b.jpg"
    primary_c = tmp_path / "c.jpg"
    sidecar_a = tmp_path / "a.txt"
    sidecar_b = tmp_path / "b.txt"
    sidecar_a.write_text("cat", encoding="utf-8")
    sidecar_b.write_text("dog", encoding="utf-8")
    catalog = build_asset_bundles(
        {"a": primary_a, "b": primary_b, "c": primary_c},
        [sidecar_a, sidecar_b],
        SidecarPolicy(
            extensions=(".txt",),
            required_extensions=(".txt",),
            naming=SidecarNaming.STEM,
        ),
    )
    issues = audit_sidecar_conflicts(catalog, [("a", "b", "c")])
    assert issue_kinds(issues) == [
        SidecarIssueKind.CLUSTER_CONTENT_MISMATCH,
        SidecarIssueKind.CLUSTER_PRESENCE_MISMATCH,
    ]


def test_conflict_audit_accepts_identical_sidecars(tmp_path):
    sidecars = []
    primaries = {}
    for asset_id in ("a", "b"):
        primaries[asset_id] = tmp_path / "{}.jpg".format(asset_id)
        sidecar = tmp_path / "{}.txt".format(asset_id)
        sidecar.write_text("same caption", encoding="utf-8")
        sidecars.append(sidecar)
    catalog = build_asset_bundles(
        primaries,
        sidecars,
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )
    assert audit_sidecar_conflicts(catalog, [("a", "b")]) == ()


def test_conflict_audit_reports_unknown_group_member(tmp_path):
    sidecar = tmp_path / "a.txt"
    sidecar.write_text("caption", encoding="utf-8")
    catalog = build_asset_bundles(
        {"a": tmp_path / "a.jpg"},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )
    issues = audit_sidecar_conflicts(catalog, [("a", "missing")])
    assert len(issues) == 1
    assert issues[0].kind is SidecarIssueKind.UNKNOWN_ASSET
    assert issues[0].asset_ids == ("missing",)


def test_sidecar_read_hashes_in_chunks_and_incrementally_validates_utf8(tmp_path, monkeypatch):
    sidecar = tmp_path / "image.txt"
    content = "a€b".encode("utf-8")
    sidecar.write_bytes(content)
    observed_read_sizes = []
    real_read_chunk = asset_bundle._read_chunk

    def recording_read_chunk(stream, size):
        observed_read_sizes.append(size)
        return real_read_chunk(stream, size)

    monkeypatch.setattr(asset_bundle, "SIDECAR_READ_CHUNK_SIZE", 2)
    monkeypatch.setattr(asset_bundle, "_read_chunk", recording_read_chunk)
    result = SidecarAsset.read(sidecar, ".txt", text=True)

    assert result.read_status is SidecarReadStatus.OK
    assert result.size == len(content)
    assert result.digest == hashlib.sha256(content).digest()
    assert observed_read_sizes
    assert max(observed_read_sizes) <= 2


def test_sidecar_read_reports_typed_too_large_issue_without_opening(tmp_path, monkeypatch):
    primary = tmp_path / "image.jpg"
    sidecar = tmp_path / "image.txt"
    sidecar.write_bytes(b"12345")
    monkeypatch.setattr(asset_bundle, "MAX_SIDECAR_BYTES", 4)

    def forbidden_open(_path):
        raise AssertionError("oversized sidecar must be rejected before opening")

    monkeypatch.setattr(asset_bundle, "_open_no_follow", forbidden_open)
    catalog = build_asset_bundles(
        {"image": primary},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )

    assert catalog.by_id()["image"].sidecars[0].read_status is SidecarReadStatus.TOO_LARGE
    assert issue_kinds(catalog.issues) == [SidecarIssueKind.TOO_LARGE]
    assert "4 bytes" in catalog.issues[0].detail


def test_sidecar_read_reports_typed_unsafe_path_issue_for_hardlink(tmp_path):
    primary = tmp_path / "image.jpg"
    sidecar = tmp_path / "image.txt"
    other_name = tmp_path / "other.txt"
    sidecar.write_text("caption", encoding="utf-8")
    os.link(sidecar, other_name)

    catalog = build_asset_bundles(
        {"image": primary},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )

    assert catalog.by_id()["image"].sidecars[0].read_status is SidecarReadStatus.UNSAFE_PATH
    assert issue_kinds(catalog.issues) == [SidecarIssueKind.UNSAFE_PATH]
    assert "exactly one filesystem link" in catalog.issues[0].detail


def test_sidecar_read_reports_typed_unsafe_path_issue_for_symlink(tmp_path):
    primary = tmp_path / "image.jpg"
    target = tmp_path / "target.txt"
    sidecar = tmp_path / "image.txt"
    target.write_text("caption", encoding="utf-8")
    try:
        sidecar.symlink_to(target)
    except OSError as error:
        pytest.skip("symbolic links are unavailable: {}".format(error))

    catalog = build_asset_bundles(
        {"image": primary},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )

    assert catalog.by_id()["image"].sidecars[0].read_status is SidecarReadStatus.UNSAFE_PATH
    assert issue_kinds(catalog.issues) == [SidecarIssueKind.UNSAFE_PATH]
    assert "symbolic link" in catalog.issues[0].detail


def test_sidecar_read_reports_typed_change_when_content_grows(tmp_path, monkeypatch):
    primary = tmp_path / "image.jpg"
    sidecar = tmp_path / "image.txt"
    sidecar.write_text("original caption", encoding="utf-8")
    real_read_chunk = asset_bundle._read_chunk
    changed = False
    writer_blocked = False

    def changing_read_chunk(stream, size):
        nonlocal changed, writer_blocked
        chunk = real_read_chunk(stream, size)
        if chunk and not changed:
            try:
                with sidecar.open("ab") as sidecar_stream:
                    sidecar_stream.write(b" changed")
                changed = True
            except OSError:
                writer_blocked = True
        return chunk

    monkeypatch.setattr(asset_bundle, "_read_chunk", changing_read_chunk)
    catalog = build_asset_bundles(
        {"image": primary},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )

    sidecar_asset = catalog.by_id()["image"].sidecars[0]
    if os.name == "nt":
        assert writer_blocked
        assert not changed
        assert sidecar_asset.read_status is SidecarReadStatus.OK
        assert issue_kinds(catalog.issues) == []
    else:
        assert changed, sidecar_asset
        assert sidecar_asset.read_status is SidecarReadStatus.CHANGED_DURING_READ
        assert issue_kinds(catalog.issues) == [SidecarIssueKind.CHANGED_DURING_READ]
        assert sidecar_asset.digest == b""


def test_sidecar_read_reports_typed_read_error(tmp_path, monkeypatch):
    primary = tmp_path / "image.jpg"
    sidecar = tmp_path / "image.txt"
    sidecar.write_text("caption", encoding="utf-8")

    def failing_read_chunk(_stream, _size):
        raise OSError("injected sidecar read failure")

    monkeypatch.setattr(asset_bundle, "_read_chunk", failing_read_chunk)
    catalog = build_asset_bundles(
        {"image": primary},
        [sidecar],
        SidecarPolicy(extensions=(".txt",), naming=SidecarNaming.STEM),
    )

    assert catalog.by_id()["image"].sidecars[0].read_status is SidecarReadStatus.READ_ERROR
    assert issue_kinds(catalog.issues) == [SidecarIssueKind.READ_ERROR]
    assert "injected sidecar read failure" in catalog.issues[0].detail
