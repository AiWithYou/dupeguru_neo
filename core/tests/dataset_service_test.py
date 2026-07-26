# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import csv
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import core.dataset_service as dataset_module
from core.dataset_executor import (
    MAX_EXECUTION_DOCUMENT_BYTES,
    MAX_EXECUTION_TRANSACTION_FILES,
)
from core.dataset_service import (
    DatasetAsset,
    DatasetCluster,
    DatasetModeService,
    DatasetOperation,
    DatasetRelation,
    DatasetSafetyError,
    FilesystemInspector,
    PreparationState,
    export_plan_csv,
    export_plan_json,
)
from core.pe.asset_bundle import (
    AssetBundle,
    SidecarAsset,
    SidecarReadStatus,
)
from core.reserved_paths import RESERVED_INTERNAL_DIRECTORY_NAMES


def dataset_roots(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "organized"
    exports = tmp_path / "exports"
    source.mkdir()
    destination.mkdir()
    exports.mkdir()
    return source, destination, exports


def test_plan_resource_limits_match_the_executor_contract():
    assert dataset_module.MAX_DATASET_PLAN_DOCUMENT_BYTES == MAX_EXECUTION_DOCUMENT_BYTES
    assert dataset_module.MAX_DATASET_PLAN_ACTIONS > MAX_EXECUTION_TRANSACTION_FILES
    assert dataset_module.MAX_DATASET_PLAN_FILE_RECORDS > MAX_EXECUTION_TRANSACTION_FILES
    assert dataset_module.MAX_DATASET_EXPORT_BYTES == MAX_EXECUTION_DOCUMENT_BYTES


def test_root_overlap_validation_uses_component_sort_not_all_pairs(
    tmp_path,
    monkeypatch,
):
    root_count = 2_000
    roots = tuple(tmp_path / "root-{:04d}".format(index) for index in range(root_count))
    containment_calls = 0
    original_containment = dataset_module._is_within

    def counted_containment(candidate, root):
        nonlocal containment_calls
        containment_calls += 1
        return original_containment(candidate, root)

    monkeypatch.setattr(
        dataset_module,
        "_validate_directory",
        lambda _path: None,
    )
    monkeypatch.setattr(
        dataset_module,
        "_is_within",
        counted_containment,
    )

    normalized = dataset_module._normalize_roots(roots)

    assert len(normalized) == root_count
    assert containment_calls == 0


def test_component_sorted_roots_detect_nested_path_despite_lexical_sibling(
    tmp_path,
    monkeypatch,
):
    ancestor = tmp_path / "asset"
    lexical_sibling = tmp_path / "asset-backup"
    descendant = ancestor / "nested"
    monkeypatch.setattr(
        dataset_module,
        "_validate_directory",
        lambda _path: None,
    )

    with pytest.raises(ValueError, match="must not overlap"):
        dataset_module._normalize_roots(
            (ancestor, lexical_sibling, descendant),
        )


@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_explicit_primary_inside_internal_directory_is_rejected(tmp_path, reserved_name):
    source, destination, _exports = dataset_roots(tmp_path)
    internal = source / reserved_name / "operation"
    internal.mkdir(parents=True)
    primary = internal / "image.jpg"
    sidecar = internal / "image.txt"
    primary.write_bytes(b"managed payload")
    sidecar.write_text("managed sidecar", encoding="utf-8")

    result = DatasetModeService().prepare(
        (DatasetAsset("managed", str(primary)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(sidecar,),
        split_weights={"train": 1},
    )

    assert result.state is PreparationState.FAILED
    assert result.issues[0].code == "reserved_internal_path"
    assert primary.read_bytes() == b"managed payload"
    assert sidecar.read_text(encoding="utf-8") == "managed sidecar"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_explicit_sidecar_inside_internal_directory_is_rejected(tmp_path, reserved_name):
    source, destination, _exports = dataset_roots(tmp_path)
    primary = source / "image.jpg"
    primary.write_bytes(b"image")
    internal = source / reserved_name
    internal.mkdir()
    sidecar = internal / "image.txt"
    sidecar.write_text("managed sidecar", encoding="utf-8")

    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(primary)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(sidecar,),
        split_weights={"train": 1},
    )

    assert result.state is PreparationState.FAILED
    assert result.issues[0].code == "reserved_internal_path"
    assert primary.read_bytes() == b"image"
    assert sidecar.read_text(encoding="utf-8") == "managed sidecar"


@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_destination_root_inside_internal_directory_is_rejected(tmp_path, reserved_name):
    source = tmp_path / "source"
    source.mkdir()
    primary = source / "image.jpg"
    primary.write_bytes(b"image")
    internal_destination = tmp_path / reserved_name
    internal_destination.mkdir()

    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(primary)),),
        (),
        allowed_roots=(source,),
        destination_root=internal_destination,
        sidecar_paths=(),
        split_weights={"train": 1},
    )

    assert result.state is PreparationState.FAILED
    assert result.issues[0].code == "reserved_internal_path"
    assert primary.read_bytes() == b"image"


def test_reserved_split_and_windows_trailing_alias_are_rejected(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    primary = source / "image.jpg"
    primary.write_bytes(b"image")
    service = DatasetModeService()

    with pytest.raises(ValueError, match="safe single path components"):
        service.prepare(
            (DatasetAsset("image", str(primary)),),
            (),
            allowed_roots=(source,),
            destination_root=destination,
            sidecar_paths=(),
            split_weights={".dupeguru-neo-dataset-executor": 1},
        )

    if os.name == "nt":
        for unsafe_split in (
            ".dupeguru-neo-dataset-quarantine. ",
            ".dupeguru-neo-dataset-executor::$INDEX_ALLOCATION",
            ".dupeguru-neo-dataset-executor:$I30:$INDEX_ALLOCATION",
            "train:alternate-stream",
            "con",
        ):
            with pytest.raises(ValueError, match="safe single path components"):
                service.prepare(
                    (DatasetAsset("image", str(primary)),),
                    (),
                    allowed_roots=(source,),
                    destination_root=destination,
                    sidecar_paths=(),
                    split_weights={unsafe_split: 1},
                )


@pytest.mark.skipif(os.name != "nt", reason="Win32 internal-directory aliases")
@pytest.mark.parametrize(
    "alias",
    (
        ".dupeguru-neo-dataset-quarantine.",
        ".dupeguru-neo-dataset-quarantine ",
        ".dupeguru-neo-dataset-executor::$INDEX_ALLOCATION",
        ".dupeguru-neo-dataset-executor:$I30:$INDEX_ALLOCATION",
        ".DUPEGURU-NEO-QUARANTINE",
    ),
)
def test_reserved_directory_aliases_are_recognized_on_windows(alias):
    assert dataset_module.is_reserved_internal_directory(alias)


def test_physical_path_alias_into_internal_directory_is_rejected(tmp_path, monkeypatch):
    source, _destination, _exports = dataset_roots(tmp_path)
    lexical = source / "lexical.jpg"
    lexical.write_bytes(b"image")
    physical = source / ".dupeguru-neo-quarantine" / "payload"
    physical.parent.mkdir()
    physical.write_bytes(b"managed payload")
    original_resolve = Path.resolve

    def resolve_alias(path, *args, **kwargs):
        if path == lexical:
            return physical
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_alias)

    with pytest.raises(DatasetSafetyError) as raised:
        dataset_module._validate_user_dataset_path(lexical, "dataset primary")

    assert raised.value.code == "reserved_internal_path"
    assert raised.value.path == physical


def test_automatic_sidecar_discovery_prunes_all_internal_subtrees(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    primary = source / "image.jpg"
    primary.write_bytes(b"image")
    for reserved_name in RESERVED_INTERNAL_DIRECTORY_NAMES:
        internal = source / reserved_name / "nested"
        internal.mkdir(parents=True)
        (internal / "operation.json").write_text('{"internal":true}', encoding="utf-8")
        (internal / "orphan.txt").write_text("managed", encoding="utf-8")

    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(primary)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=None,
        split_weights={"train": 1},
    )

    assert result.state is PreparationState.COMPLETE
    assert result.plan is not None
    assert all(len(action.files) == 1 for action in result.plan.actions)


def build_exact_dataset(tmp_path):
    source, destination, exports = dataset_roots(tmp_path)
    best = source / "best.jpg"
    copy = source / "photo copy.jpg"
    content = b"identical-image-content"
    best.write_bytes(content)
    copy.write_bytes(content)
    best.with_suffix(".txt").write_text("same caption", encoding="utf-8")
    copy.with_suffix(".txt").write_text("same caption", encoding="utf-8")
    assets = (
        DatasetAsset(
            "best",
            str(best),
            dimensions=(4000, 3000),
            bit_depth=16,
            metadata_count=20,
            protected=True,
        ),
        DatasetAsset(
            "copy",
            str(copy),
            dimensions=(1000, 750),
            bit_depth=8,
            metadata_count=2,
        ),
    )
    clusters = (
        DatasetCluster(
            ("best", "copy"),
            DatasetRelation.VERIFIED_EXACT,
        ),
    )
    return source, destination, exports, assets, clusters


def test_exact_bundle_plan_is_atomic_explainable_and_read_only(tmp_path):
    source, destination, _exports, assets, clusters = build_exact_dataset(tmp_path)
    service = DatasetModeService()
    first = service.prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
        dry_run=True,
    )
    second = service.prepare(
        reversed(assets),
        reversed(clusters),
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
        dry_run=True,
    )
    assert first.state is PreparationState.COMPLETE
    assert first.plan is not None
    assert second.plan is not None
    assert first.plan.plan_id == second.plan.plan_id
    assert first.plan.to_json() == second.plan.to_json()
    assert list(destination.iterdir()) == []

    plan = first.plan
    assert plan.dry_run
    assert plan.split_manifest.member_splits() == {"best": "train", "copy": "train"}
    keeper = plan.keepers[0]
    assert keeper.keeper_id == "best"
    assert "higher resolution" in dict(keeper.explanations)["best"]
    assert any(reason.code == "protected" for reason in dict(keeper.reasons)["best"])

    move = next(action for action in plan.actions if action.operation is DatasetOperation.MOVE_BUNDLE)
    quarantine = next(action for action in plan.actions if action.operation is DatasetOperation.QUARANTINE_BUNDLE)
    assert move.asset_id == "best"
    assert quarantine.asset_id == "copy"
    assert quarantine.atomic
    assert len(quarantine.files) == 2
    assert all(item.reference is not None for item in quarantine.files)
    assert all(item.destination is None for item in quarantine.files)
    assert all(item.source.digest_hex == item.reference.digest_hex for item in quarantine.files)
    assert service.revalidate(plan).valid


def test_related_cluster_is_kept_in_one_split_without_quarantine(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    first_path = source / "first.jpg"
    second_path = source / "second.jpg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    assets = (
        DatasetAsset("first", str(first_path), dimensions=(100, 100)),
        DatasetAsset("second", str(second_path), dimensions=(200, 200)),
    )
    result = DatasetModeService().prepare(
        assets,
        (DatasetCluster(("first", "second"), DatasetRelation.RELATED),),
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 0.5, "test": 0.5},
        split_seed="stable",
    )
    assert result.complete
    assert result.plan is not None
    assert len(set(result.plan.split_manifest.member_splits().values())) == 1
    assert {action.operation for action in result.plan.actions} == {DatasetOperation.MOVE_BUNDLE}


def test_orphan_and_broken_sidecars_fail_closed(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    orphan = source / "orphan.txt"
    orphan.write_text("no owner", encoding="utf-8")
    asset = DatasetAsset("image", str(image))
    orphan_result = DatasetModeService().prepare(
        (asset,),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert orphan_result.state is PreparationState.CONFLICT
    assert orphan_result.plan is None
    assert any(issue.code == "sidecar_orphan" for issue in orphan_result.issues)

    orphan.unlink()
    image.with_suffix(".caption").write_bytes(b"\xff\xfe")
    broken_result = DatasetModeService().prepare(
        (asset,),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert broken_result.state is PreparationState.CONFLICT
    assert any(issue.code == "sidecar_invalid_utf8" for issue in broken_result.issues)


@pytest.mark.parametrize(
    "payload",
    (
        "{not valid json",
        '{"duplicate":1,"duplicate":2}',
        '{"value":NaN}',
    ),
)
def test_invalid_json_sidecar_fails_closed(tmp_path, payload):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    image.with_suffix(".json").write_text(payload, encoding="utf-8")
    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "invalid_json_sidecar"


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload"),
    [
        ("MAX_JSON_SIDECAR_DEPTH", 4, "[[[[[0]]]]]"),
        ("MAX_JSON_SIDECAR_NODES", 10, "[0,0,0,0,0,0,0,0,0,0]"),
        ("MAX_JSON_SIDECAR_CONTAINER_ITEMS", 3, "[0,1,2,3]"),
        ("MAX_JSON_SIDECAR_STRING_CHARACTERS", 3, '"four"'),
    ],
)
def test_json_sidecar_structural_limits_fail_before_object_graph_use(
    tmp_path,
    monkeypatch,
    limit_name,
    limit,
    payload,
):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    image.with_suffix(".json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(dataset_module, limit_name, limit)

    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )

    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "sidecar_resource_limit"


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (RecursionError("too deep"), "invalid_json_sidecar"),
        (MemoryError("out of memory"), "sidecar_resource_limit"),
    ],
)
def test_json_sidecar_parser_failures_are_typed(tmp_path, monkeypatch, exception, expected_code):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    image.with_suffix(".json").write_text('{"valid":true}', encoding="utf-8")

    def fail_parse(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(dataset_module.json, "loads", fail_parse)
    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )

    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == expected_code


def test_dataset_preparation_retains_only_proofs_for_all_sidecars(tmp_path, monkeypatch):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    image.with_suffix(".txt").write_text("caption", encoding="utf-8")
    image.with_suffix(".json").write_text('{"label":"sample"}', encoding="utf-8")
    observed = {}
    original_select_keepers = dataset_module._select_keepers

    def assert_proof_only(units, assets, inspected):
        observed["count"] = len(inspected)
        observed["retained_bytes"] = sum(len(item.content or b"") for item in inspected.values())
        return original_select_keepers(units, assets, inspected)

    monkeypatch.setattr(dataset_module, "_select_keepers", assert_proof_only)
    result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )

    assert result.complete
    assert observed == {"count": 3, "retained_bytes": 0}


def test_sidecar_slot_index_is_built_once_and_reused(tmp_path, monkeypatch):
    source, destination, _exports, assets, clusters = build_exact_dataset(
        tmp_path,
    )
    for suffix in (".caption", ".json"):
        payload = '{"value":"same"}' if suffix == ".json" else "same"
        (source / "best.jpg").with_suffix(suffix).write_text(
            payload,
            encoding="utf-8",
        )
        (source / "photo copy.jpg").with_suffix(suffix).write_text(
            payload,
            encoding="utf-8",
        )

    original_index = dataset_module._index_sidecars_by_asset
    original_lookup = AssetBundle.sidecars_for
    index_calls = 0
    indexed_sidecars = 0
    lookup_calls = 0

    def counted_index(bundles):
        nonlocal index_calls, indexed_sidecars
        index_calls += 1
        indexed_sidecars += sum(len(bundle.sidecars) for bundle in bundles.values())
        return original_index(bundles)

    def counted_lookup(bundle, slot):
        nonlocal lookup_calls
        lookup_calls += 1
        return original_lookup(bundle, slot)

    monkeypatch.setattr(
        dataset_module,
        "_index_sidecars_by_asset",
        counted_index,
    )
    monkeypatch.setattr(
        AssetBundle,
        "sidecars_for",
        counted_lookup,
    )

    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
    )

    assert result.complete
    assert index_calls == 1
    assert indexed_sidecars == 6
    # The six calls belong to the pre-plan conflict audit (three slots across
    # two members). Exact verification and action construction add none.
    assert lookup_calls == 6


def test_sidecar_slot_index_rejects_duplicate_slots_explicitly():
    first = SidecarAsset(
        "first.txt",
        ".txt",
        1,
        b"a",
        SidecarReadStatus.OK,
    )
    second = SidecarAsset(
        "second.txt",
        ".txt",
        1,
        b"b",
        SidecarReadStatus.OK,
    )
    bundle = AssetBundle(
        "asset",
        "image.jpg",
        (first, second),
    )

    with pytest.raises(
        DatasetSafetyError,
        match="more than one sidecar",
    ) as caught:
        dataset_module._index_sidecars_by_asset(
            {"asset": bundle},
        )

    assert caught.value.code == "duplicate_sidecar_slot"


def test_non_json_sidecar_is_streamed_under_the_configured_per_file_limit(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"image")
    image.with_suffix(".txt").write_bytes(b"12345")

    result = DatasetModeService(maximum_sidecar_bytes=4).prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
    )

    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "sidecar_resource_limit"


def test_dataset_service_rejects_boolean_sidecar_limit():
    with pytest.raises(ValueError, match="maximum sidecar size"):
        DatasetModeService(maximum_sidecar_bytes=True)


def test_incomplete_or_false_exact_evidence_never_produces_a_plan(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    first = source / "first.jpg"
    second = source / "second.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    assets = (DatasetAsset("a", str(first)), DatasetAsset("b", str(second)))
    service = DatasetModeService()
    incomplete = service.prepare(
        assets,
        (DatasetCluster(("a", "b"), DatasetRelation.VERIFIED_EXACT, evidence_complete=False),),
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert incomplete.state is PreparationState.INCOMPLETE_EVIDENCE
    assert incomplete.issues[0].code == "incomplete_cluster_evidence"

    mismatch = service.prepare(
        assets,
        (DatasetCluster(("a", "b"), DatasetRelation.VERIFIED_EXACT),),
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert mismatch.state is PreparationState.INCOMPLETE_EVIDENCE
    assert mismatch.issues[0].code == "exact_evidence_mismatch"


def test_sidecar_mismatch_in_exact_cluster_blocks_whole_bundle(tmp_path):
    source, destination, _exports, assets, clusters = build_exact_dataset(tmp_path)
    (source / "photo copy.txt").write_text("different caption", encoding="utf-8")
    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
    )
    assert result.state is PreparationState.CONFLICT
    assert result.plan is None
    assert any(issue.code == "sidecar_cluster_content_mismatch" for issue in result.issues)


def test_flat_output_rename_collision_is_rejected(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    left_dir = source / "left"
    right_dir = source / "right"
    left_dir.mkdir()
    right_dir.mkdir()
    left = left_dir / "same.jpg"
    right = right_dir / "same.jpg"
    left.write_bytes(b"left")
    right.write_bytes(b"right")
    result = DatasetModeService().prepare(
        (DatasetAsset("left", str(left)), DatasetAsset("right", str(right))),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.state is PreparationState.CONFLICT
    assert result.plan is None
    assert result.issues[0].code == "rename_collision"


def test_same_physical_file_alias_is_rejected(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    first = source / "first.jpg"
    second = source / "second.jpg"
    first.write_bytes(b"same inode")
    os.link(first, second)
    result = DatasetModeService().prepare(
        (DatasetAsset("first", str(first)), DatasetAsset("second", str(second))),
        (DatasetCluster(("first", "second"), DatasetRelation.VERIFIED_EXACT),),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(),
    )
    assert result.state is PreparationState.CONFLICT
    assert result.issues[0].code == "same_physical_file"


def test_symlink_and_reparse_sources_are_rejected(tmp_path, monkeypatch):
    source, destination, _exports = dataset_roots(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = source / "link.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this Windows configuration cannot create an unprivileged symlink")
    symlink_result = DatasetModeService().prepare(
        (DatasetAsset("link", str(link)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(),
    )
    assert symlink_result.state is PreparationState.FAILED
    assert symlink_result.issues[0].code == "symlink_escape"

    link.unlink()
    image = source / "image.jpg"
    image.write_bytes(b"image")
    outside_text = tmp_path / "outside.txt"
    outside_text.write_text("outside secret", encoding="utf-8")
    sidecar_link = source / "image.txt"
    sidecar_link.symlink_to(outside_text)
    sidecar_result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(sidecar_link,),
    )
    assert sidecar_result.state is PreparationState.FAILED
    assert sidecar_result.issues[0].code == "symlink_escape"
    sidecar_link.unlink()

    image_inode = os.stat(image, follow_symlinks=False).st_ino
    original = dataset_module.is_reparse_point
    monkeypatch.setattr(
        dataset_module,
        "is_reparse_point",
        lambda file_stat: file_stat.st_ino == image_inode or original(file_stat),
    )
    reparse_result = DatasetModeService().prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(),
    )
    assert reparse_result.state is PreparationState.FAILED
    assert reparse_result.issues[0].code == "reparse_escape"


class MutatingInspector(FilesystemInspector):
    def __init__(self):
        self.mutated = False

    def snapshot(
        self,
        path,
        allowed_roots,
        *,
        capture_content,
        maximum_capture_bytes,
        maximum_file_bytes=None,
    ):
        result = super().snapshot(
            path,
            allowed_roots,
            capture_content=capture_content,
            maximum_capture_bytes=maximum_capture_bytes,
            maximum_file_bytes=maximum_file_bytes,
        )
        if not self.mutated and path.suffix == ".jpg":
            path.write_bytes(b"changed-after-snapshot")
            self.mutated = True
        return result


def test_change_race_during_plan_construction_fails_closed(tmp_path):
    source, destination, _exports = dataset_roots(tmp_path)
    image = source / "image.jpg"
    image.write_bytes(b"original-content")
    result = DatasetModeService(inspector=MutatingInspector()).prepare(
        (DatasetAsset("image", str(image)),),
        (),
        allowed_roots=(source,),
        destination_root=destination,
        sidecar_paths=(),
    )
    assert result.state is PreparationState.SOURCE_CHANGED
    assert result.plan is None
    assert result.issues[0].code == "source_changed"


def test_revalidation_detects_change_after_plan(tmp_path):
    source, destination, _exports, assets, clusters = build_exact_dataset(tmp_path)
    service = DatasetModeService()
    result = service.prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.plan is not None
    Path(assets[1].path).write_bytes(b"changed")
    validation = service.revalidate(result.plan)
    assert not validation.valid
    assert any(issue.code == "source_changed" for issue in validation.issues)


def test_plan_and_action_ids_are_bound_to_immutable_contents(tmp_path):
    source, destination, _exports, assets, clusters = build_exact_dataset(tmp_path)
    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.plan is not None
    with pytest.raises(ValueError, match="action ID"):
        replace(result.plan.actions[0], split="tampered")
    with pytest.raises(ValueError, match="plan ID"):
        replace(result.plan, dry_run=not result.plan.dry_run)


def test_json_and_csv_exports_are_deterministic_atomic_and_no_clobber(tmp_path, monkeypatch):
    source, destination, exports, assets, clusters = build_exact_dataset(tmp_path)
    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.plan is not None
    plan = result.plan

    json_path = exports / "plan.json"
    monkeypatch.setattr(
        dataset_module.DatasetPlan,
        "to_json",
        lambda _self: (_ for _ in ()).throw(AssertionError("JSON export must stream instead of calling to_json()")),
    )
    json_receipt = export_plan_json(plan, json_path, allowed_output_root=exports)
    assert json_receipt.written
    assert json.loads(json_path.read_text(encoding="utf-8"))["plan_id"] == plan.plan_id
    original_payload = json_path.read_bytes()
    with pytest.raises(FileExistsError):
        export_plan_json(plan, json_path, allowed_output_root=exports)
    assert json_path.read_bytes() == original_payload

    first_csv = exports / "plan-1.csv"
    second_csv = exports / "plan-2.csv"
    export_plan_csv(plan, first_csv, allowed_output_root=exports)
    export_plan_csv(plan, second_csv, allowed_output_root=exports)
    assert first_csv.read_bytes() == second_csv.read_bytes()
    rows = tuple(csv.DictReader(io.StringIO(first_csv.read_text(encoding="utf-8"))))
    assert len(rows) == sum(len(action.files) for action in plan.actions)
    assert {row["operation"] for row in rows} == {"move_bundle", "quarantine_bundle"}

    dry_path = exports / "dry-run.json"
    dry_receipt = export_plan_json(
        plan,
        dry_path,
        allowed_output_root=exports,
        dry_run=True,
    )
    assert dry_receipt.dry_run and not dry_receipt.written
    assert not dry_path.exists()
    with pytest.raises(DatasetSafetyError, match="outside"):
        export_plan_json(
            plan,
            tmp_path / "outside-export.json",
            allowed_output_root=exports,
            dry_run=True,
        )

    failed_root = exports / "failed"
    failed_root.mkdir()
    adapter_base = type(dataset_module.platform_file_system())

    class FailBeforePublicationFileSystem(adapter_base):
        def rename_no_replace(self, _source, _destination):
            raise OSError("fail")

    monkeypatch.setattr(
        dataset_module,
        "platform_file_system",
        lambda: FailBeforePublicationFileSystem(),
    )
    with pytest.raises(OSError, match="fail"):
        export_plan_json(
            plan,
            failed_root / "plan.json",
            allowed_output_root=failed_root,
        )
    assert list(failed_root.iterdir()) == []


def test_export_cleanup_preserves_a_replacement_at_the_consumed_temp_name(
    tmp_path,
    monkeypatch,
):
    source, destination, exports, assets, clusters = build_exact_dataset(tmp_path)
    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.plan is not None
    adapter_base = type(dataset_module.platform_file_system())
    external = b"external temp replacement"

    class ReplaceTempAfterPublicationFileSystem(adapter_base):
        replacement = None

        def rename_no_replace(self, source_path, destination_path):
            commit = super().rename_no_replace(source_path, destination_path)
            Path(source_path).write_bytes(external)
            self.replacement = Path(source_path)
            return commit

        def fsync_directory(self, directory):
            if self.replacement is not None and Path(directory) == exports:
                raise OSError(5, "injected export durability failure")
            return super().fsync_directory(directory)

    adapter = ReplaceTempAfterPublicationFileSystem()
    monkeypatch.setattr(
        dataset_module,
        "platform_file_system",
        lambda: adapter,
    )
    published = exports / "published.json"

    with pytest.raises(OSError, match="durability failure"):
        export_plan_json(
            result.plan,
            published,
            allowed_output_root=exports,
        )

    assert published.is_file()
    assert adapter.replacement is not None
    assert adapter.replacement.read_bytes() == external


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    (
        ("MAX_DATASET_PLAN_ACTIONS", 1),
        ("MAX_DATASET_PLAN_FILE_RECORDS", 1),
        ("MAX_DATASET_PLAN_DOCUMENT_BYTES", 512),
    ),
)
def test_prepare_fails_before_plan_creation_when_resource_limit_is_exceeded(
    tmp_path,
    monkeypatch,
    limit_name,
    limit,
):
    source, destination, exports, assets, clusters = build_exact_dataset(tmp_path)
    monkeypatch.setattr(dataset_module, limit_name, limit)

    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )

    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert [issue.code for issue in result.issues] == ["plan_resource_limit"]
    assert list(destination.iterdir()) == []
    assert list(exports.iterdir()) == []


@pytest.mark.parametrize("format_name", ("json", "csv"))
def test_export_byte_limit_fails_without_publication_or_clobber(
    tmp_path,
    format_name,
):
    source, destination, exports, assets, clusters = build_exact_dataset(tmp_path)
    result = DatasetModeService().prepare(
        assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"train": 1},
    )
    assert result.plan is not None
    export = export_plan_json if format_name == "json" else export_plan_csv
    missing = exports / "oversized.{}".format(format_name)

    with pytest.raises(DatasetSafetyError) as caught:
        export(
            result.plan,
            missing,
            allowed_output_root=exports,
            maximum_bytes=1,
        )

    assert caught.value.code == "export_too_large"
    assert not missing.exists()
    assert list(exports.iterdir()) == []

    existing = exports / "existing.{}".format(format_name)
    existing.write_bytes(b"sentinel")
    with pytest.raises(FileExistsError):
        export(
            result.plan,
            existing,
            allowed_output_root=exports,
            maximum_bytes=1,
        )
    assert existing.read_bytes() == b"sentinel"
    assert tuple(exports.iterdir()) == (existing,)


def test_csv_preserves_raw_values_by_default_and_requires_safe_opt_in(
    tmp_path,
):
    source, destination, exports, assets, _clusters = build_exact_dataset(tmp_path)
    dangerous_assets = (
        replace(assets[0], asset_id="=best"),
        replace(assets[1], asset_id="+copy"),
    )
    clusters = (
        DatasetCluster(
            ("=best", "+copy"),
            DatasetRelation.VERIFIED_EXACT,
        ),
    )
    result = DatasetModeService().prepare(
        dangerous_assets,
        clusters,
        allowed_roots=(source,),
        destination_root=destination,
        split_weights={"@train": 1},
    )
    assert result.plan is not None

    raw_path = exports / "raw.csv"
    raw_receipt = export_plan_csv(
        result.plan,
        raw_path,
        allowed_output_root=exports,
    )
    raw_rows = tuple(csv.DictReader(io.StringIO(raw_path.read_text(encoding="utf-8"))))
    assert raw_receipt.format == "csv"
    assert {row["asset_id"] for row in raw_rows} == {"=best", "+copy"}
    assert {row["split"] for row in raw_rows} == {"@train"}

    safe_path = exports / "spreadsheet-safe.csv"
    safe_receipt = export_plan_csv(
        result.plan,
        safe_path,
        allowed_output_root=exports,
        spreadsheet_safe=True,
    )
    safe_rows = tuple(csv.DictReader(io.StringIO(safe_path.read_text(encoding="utf-8"))))
    assert safe_receipt.format == "csv-spreadsheet-safe"
    assert {row["asset_id"] for row in safe_rows} == {"'=best", "'+copy"}
    assert {row["split"] for row in safe_rows} == {"'@train"}
    assert dataset_module._spreadsheet_safe_cell("\tvalue") == "'\tvalue"
    assert dataset_module._spreadsheet_safe_cell("\rvalue") == "'\rvalue"
    assert dataset_module._spreadsheet_safe_cell("-value") == "'-value"
