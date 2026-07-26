# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import shutil
from types import SimpleNamespace

import pytest
from PIL import Image

from core.dataset_discovery import (
    DatasetDiscoveryService,
    DatasetRootRequest,
    _DisjointSet,
    build_dataset_clusters,
    unproven_exact_group_ids,
)
from core.dataset_executor import ExecutionState
from core.dataset_io import DatasetWorkflowFacade
from core.dataset_service import DatasetOperation, DatasetRelation, PreparationState
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus
from core.services.models import ScanIssue as ExactScanIssue
from core.visual_service import VisualRelation, VisualScanConfig, VisualService


def _asset(asset_id, path):
    return SimpleNamespace(asset_id=asset_id, path=str(path))


def _artifact(asset_id, dimensions=(100, 100)):
    return SimpleNamespace(asset_id=asset_id, dimensions=dimensions)


def _evidence(
    first,
    second,
    *,
    relation=VisualRelation.SIMILAR,
    phash_orientation=0,
    block_orientation=0,
):
    return SimpleNamespace(
        first_id=first,
        second_id=second,
        relation=relation,
        phash_orientation=phash_orientation,
        block_orientation=block_orientation,
    )


def _record(path, digest="a" * 64, algorithm="sha256", size=10):
    return SimpleNamespace(
        path=str(path),
        digest=digest,
        digest_algorithm=algorithm,
        size=size,
    )


def _exact_group(group_id, paths, *, method="sha256+core-streaming-byte-compare"):
    return SimpleNamespace(
        group_id=group_id,
        verification="verified_exact",
        verification_method=method,
        files=tuple(_record(path) for path in paths),
    )


def test_redundant_visual_edge_does_not_downgrade_proven_exact(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    assets = (_asset("a", first), _asset("b", second))
    clusters = build_dataset_clusters(
        assets,
        (_artifact("a"), _artifact("b")),
        (_evidence("a", "b"),),
        (_exact_group("exact", (first, second)),),
    )
    assert len(clusters) == 1
    assert clusters[0].members == ("a", "b")
    assert clusters[0].relation is DatasetRelation.VERIFIED_EXACT


def test_visual_connection_downgrades_an_exact_subset_for_the_whole_component(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    third = tmp_path / "third.png"
    assets = (
        _asset("a", first),
        _asset("b", second),
        _asset("c", third),
    )
    clusters = build_dataset_clusters(
        assets,
        (_artifact("a"), _artifact("b"), _artifact("c")),
        (
            _evidence("a", "b"),
            _evidence("b", "c"),
        ),
        (_exact_group("exact", (first, second)),),
    )
    assert len(clusters) == 1
    assert clusters[0].members == ("a", "b", "c")
    assert clusters[0].relation is DatasetRelation.NEAR_DUPLICATE
    assert not clusters[0].relation.quarantine_eligible


def test_visual_relations_distinguish_transformed_and_related(tmp_path):
    assets = (
        _asset("a", tmp_path / "a.png"),
        _asset("b", tmp_path / "b.png"),
        _asset("c", tmp_path / "c.png"),
        _asset("d", tmp_path / "d.png"),
    )
    clusters = build_dataset_clusters(
        assets,
        (
            _artifact("a", (100, 100)),
            _artifact("b", (200, 200)),
            _artifact("c"),
            _artifact("d"),
        ),
        (
            _evidence("a", "b"),
            _evidence("c", "d", relation=VisualRelation.RELATED),
        ),
        (),
    )
    by_members = {cluster.members: cluster.relation for cluster in clusters}
    assert by_members[("a", "b")] is DatasetRelation.TRANSFORMED
    assert by_members[("c", "d")] is DatasetRelation.RELATED


def test_explicit_review_relations_use_conservative_dataset_severity(tmp_path):
    assets = tuple(_asset(name, tmp_path / "{}.png".format(name)) for name in ("a", "b", "c", "d", "e", "f"))
    clusters = build_dataset_clusters(
        assets,
        tuple(_artifact(name) for name in ("a", "b", "c", "d", "e", "f")),
        (
            _evidence("a", "b", relation=VisualRelation.SIMILAR),
            _evidence("c", "d", relation=VisualRelation.TRANSFORMED),
            _evidence("e", "f", relation=VisualRelation.CROP_CANDIDATE),
        ),
        (),
    )

    by_members = {cluster.members: cluster.relation for cluster in clusters}
    assert by_members[("a", "b")] is DatasetRelation.NEAR_DUPLICATE
    assert by_members[("c", "d")] is DatasetRelation.TRANSFORMED
    assert by_members[("e", "f")] is DatasetRelation.TRANSFORMED


def test_fifty_thousand_disjoint_visual_edges_are_bucketed_once(tmp_path):
    edge_count = 50_000
    accesses = [0]

    class CountingEvidence:
        __slots__ = ("_first", "_second", "relation")

        def __init__(self, first, second):
            self._first = first
            self._second = second
            self.relation = VisualRelation.SIMILAR

        @property
        def first_id(self):
            accesses[0] += 1
            return self._first

        @property
        def second_id(self):
            accesses[0] += 1
            return self._second

    assets = []
    evidence = []
    for index in range(edge_count):
        first = "a{:05d}".format(index)
        second = "b{:05d}".format(index)
        assets.append(_asset(first, tmp_path / "{}.png".format(first)))
        assets.append(_asset(second, tmp_path / "{}.png".format(second)))
        evidence.append(CountingEvidence(first, second))

    clusters = build_dataset_clusters(assets, (), evidence, ())

    assert len(clusters) == edge_count
    assert accesses[0] <= edge_count * 16


def test_disjoint_set_handles_adversarial_descending_edge_order_iteratively():
    members = tuple("{:05d}".format(index) for index in range(5_001))
    disjoint = _DisjointSet(members)
    for index in range(len(members) - 1, 0, -1):
        disjoint.union(members[index - 1], members[index])

    root = disjoint.find(members[-1])

    assert root == members[0]
    assert disjoint.components() == (members,)


def test_orientation_evidence_is_transformed_even_at_the_same_dimensions(tmp_path):
    assets = (
        _asset("a", tmp_path / "a.png"),
        _asset("b", tmp_path / "b.png"),
    )
    clusters = build_dataset_clusters(
        assets,
        (_artifact("a"), _artifact("b")),
        (_evidence("a", "b", block_orientation=1),),
        (),
    )
    assert clusters[0].relation is DatasetRelation.TRANSFORMED


def test_unproven_exact_groups_are_auditable_and_never_exact(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    unsafe = _exact_group("unsafe", (first, second), method="sample-hash")
    assert unproven_exact_group_ids((unsafe,)) == ("unsafe",)
    clusters = build_dataset_clusters(
        (_asset("a", first), _asset("b", second)),
        (_artifact("a"), _artifact("b")),
        (_evidence("a", "b"),),
        (unsafe,),
    )
    assert clusters[0].relation is DatasetRelation.NEAR_DUPLICATE


class _NeverExactService:
    def scan(self, request):
        raise AssertionError("exact service must not run after an incomplete visual receipt")


class _StaticVisualService:
    def __init__(self, report):
        self.report = report

    def scan_roots(self, roots, *, config):
        return self.report


class _StaticExactService:
    def __init__(self, report):
        self.report = report

    def scan(self, request):
        return self.report


def _directories(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    return source, destination


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_dataset_root_request_rejects_nonfinite_or_nonpositive_split_weights(
    tmp_path,
    weight,
):
    source, destination = _directories(tmp_path)

    with pytest.raises(ValueError, match="finite positive"):
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
            split_weights=(("train", weight),),
        )


def test_incomplete_visual_receipt_returns_no_executable_plan(tmp_path):
    source, destination = _directories(tmp_path)
    report = SimpleNamespace(
        roots=(str(source),),
        scan_receipt=ScanReceipt.incomplete(
            discovered=1,
            analyzed=0,
            failed=1,
            issues=(
                ScanIssue(
                    "decode_failed",
                    "image could not be decoded",
                    str(source / "bad.png"),
                ),
            ),
        ),
    )
    service = DatasetDiscoveryService(
        visual_service=_StaticVisualService(report),
        exact_service=_NeverExactService(),
    )
    result = service.prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
        )
    )
    assert result.state is PreparationState.INCOMPLETE_COVERAGE
    assert result.plan is None
    assert result.issues[0].code == "visual_decode_failed"


def test_visual_resource_limit_is_propagated_without_a_plan(tmp_path):
    source, destination = _directories(tmp_path)
    report = SimpleNamespace(
        roots=(str(source),),
        scan_receipt=ScanReceipt.incomplete(
            discovered=2,
            analyzed=1,
            skipped=1,
            issues=(
                ScanIssue(
                    "candidate_resource_limit",
                    "candidate limit was reached",
                    str(source),
                ),
            ),
            status=ScanStatus.RESOURCE_LIMIT,
        ),
    )
    result = DatasetDiscoveryService(
        visual_service=_StaticVisualService(report),
        exact_service=_NeverExactService(),
    ).prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
        )
    )
    assert result.state is PreparationState.INCOMPLETE_COVERAGE
    assert result.plan is None
    assert result.issues[0].code == "visual_candidate_resource_limit"


def test_incomplete_exact_receipt_returns_no_executable_plan(tmp_path):
    source, destination = _directories(tmp_path)
    Image.new("RGB", (20, 20), "red").save(source / "image.png")
    exact_report = SimpleNamespace(
        roots=(str(source),),
        summary=SimpleNamespace(complete=False),
        groups=(),
        issues=(
            ExactScanIssue(
                path=str(source),
                code="walk-error",
                message="coverage failed",
            ),
        ),
        coverage=(),
    )
    service = DatasetDiscoveryService(
        visual_service=VisualService(),
        exact_service=_StaticExactService(exact_report),
    )
    result = service.prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
        )
    )
    assert result.state is PreparationState.INCOMPLETE_COVERAGE
    assert result.plan is None
    assert result.issues[0].code == "exact_walk_error"


def test_cache_state_and_destination_are_rejected_inside_an_input_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    external_destination = tmp_path / "destination"
    external_destination.mkdir()

    destination_inside = source / "destination"
    destination_inside.mkdir()
    result = DatasetDiscoveryService().prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination_inside),
        )
    )
    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "destination_overlap"

    result = DatasetDiscoveryService().prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(external_destination),
            visual_cache=str(source / "cache.sqlite3"),
        )
    )
    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "visual_cache_overlap"

    result = DatasetDiscoveryService().prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(external_destination),
            state_root=str(source / "state"),
        )
    )
    assert result.state is PreparationState.FAILED
    assert result.plan is None
    assert result.issues[0].code == "state_root_overlap"


def test_protected_files_are_reference_only_and_have_no_actions(tmp_path):
    source, destination = _directories(tmp_path)
    protected = source / "protected"
    protected.mkdir()
    first = protected / "first.png"
    second = protected / "second.png"
    Image.new("RGB", (32, 24), "purple").save(first)
    shutil.copyfile(first, second)

    result = DatasetDiscoveryService().prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
            protected_roots=(str(protected),),
        )
    )
    assert result.state is PreparationState.COMPLETE
    assert result.plan is not None
    assert result.plan.actions == ()
    assert result.plan.split_manifest.member_splits()
    immutable_reasons = [
        reason
        for keeper in result.plan.keepers
        for _asset_id, reasons in keeper.reasons
        for reason in reasons
        if reason.code == "immutable_protected"
    ]
    assert immutable_reasons


def test_prepare_root_end_to_end_keeps_protected_quality_and_sidecars_together(tmp_path):
    source, destination = _directories(tmp_path)
    protected = source / "protected"
    incoming = source / "incoming"
    protected.mkdir()
    incoming.mkdir()
    keeper = protected / "image.png"
    duplicate = incoming / "image-copy.png"
    keeper_caption = protected / "image.txt"
    duplicate_caption = incoming / "image-copy.txt"
    unique = incoming / "unique.png"
    unique_caption = incoming / "unique.txt"

    image = Image.new("RGB", (64, 48), "navy")
    image.save(keeper, pnginfo=None)
    shutil.copyfile(keeper, duplicate)
    keeper_caption.write_text("same caption", encoding="utf-8")
    duplicate_caption.write_text("same caption", encoding="utf-8")
    unique_image = Image.new("RGB", (64, 48))
    unique_image.putdata(
        [
            (
                (x * 7 + y * 3) % 256,
                (x * 13 + y * 11) % 256,
                (x * y + x * 5) % 256,
            )
            for y in range(48)
            for x in range(64)
        ]
    )
    unique_image.save(unique)
    unique_caption.write_text("unique caption", encoding="utf-8")

    cache = tmp_path / "visual-cache.sqlite3"
    result = DatasetDiscoveryService().prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
            protected_roots=(str(protected),),
            visual_cache=str(cache),
            split_weights=(("train", 1.0),),
            split_seed="root-e2e",
            dry_run=False,
        )
    )
    assert result.state is PreparationState.COMPLETE
    assert result.plan is not None
    plan = result.plan
    assert not plan.dry_run
    assert len(plan.actions) == 2
    assert {action.split for action in plan.actions} == {"train"}
    assert {action.operation for action in plan.actions} == {
        DatasetOperation.MOVE_BUNDLE,
        DatasetOperation.QUARANTINE_BUNDLE,
    }
    assert all(len(action.files) == 2 for action in plan.actions)

    move_action = next(action for action in plan.actions if action.operation is DatasetOperation.MOVE_BUNDLE)
    duplicate_action = next(action for action in plan.actions if action.operation is DatasetOperation.QUARANTINE_BUNDLE)
    assert move_action.files[0].source.path == str(unique)
    assert duplicate_action.files[0].source.path == str(duplicate)
    assert all(
        not file_action.source.path.startswith(str(protected))
        for action in plan.actions
        for file_action in action.files
    )
    keeper_record = next(keeper for keeper in plan.keepers if keeper.keeper_id == duplicate_action.keeper_id)
    keeper_reasons = dict(keeper_record.reasons)[keeper_record.keeper_id]
    assert any(reason.code == "protected" for reason in keeper_reasons), keeper_reasons
    assert any(reason.code == "immutable_protected" for reason in keeper_reasons)
    assert any(reason.code == "resolution" for reason in keeper_reasons)
    assert any(reason.code == "bit_depth" for reason in keeper_reasons)

    state_root = tmp_path / "state"
    execution = DatasetWorkflowFacade(state_root=state_root).apply(plan, execute=True)
    assert execution.state is ExecutionState.APPLIED, execution
    assert keeper.exists()
    assert not duplicate.exists()
    assert not unique.exists()
    assert keeper_caption.exists()
    assert not duplicate_caption.exists()
    assert not unique_caption.exists()
    assert (destination / "train" / unique.name).read_bytes()
    assert (destination / "train" / unique_caption.name).read_text(encoding="utf-8") == "unique caption"


def test_prepare_root_keeps_crop_candidates_review_only(tmp_path):
    source, destination = _directories(tmp_path)
    original = source / "original.png"
    cropped = source / "cropped.png"
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

    visual_report = VisualService(cache_path=tmp_path / "visual-cache.sqlite3").scan_roots(
        source,
        config=VisualScanConfig(
            similarity_threshold=99,
            phash_radius=0,
            include_related=False,
        ),
    )
    assert len(visual_report.evidence) == 1
    assert visual_report.evidence[0].relation is VisualRelation.CROP_CANDIDATE

    result = DatasetDiscoveryService(
        visual_service=_StaticVisualService(visual_report),
    ).prepare(
        DatasetRootRequest(
            roots=(str(source),),
            destination_root=str(destination),
            split_weights=(("train", 1.0),),
            split_seed="crop-candidate-e2e",
        )
    )

    assert result.state is PreparationState.COMPLETE
    assert result.plan is not None
    assert len(result.plan.actions) == 2
    assert all(action.operation is DatasetOperation.MOVE_BUNDLE for action in result.plan.actions)
