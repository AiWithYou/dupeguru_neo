import pytest

import core.pe.dataset as dataset_module
from core.pe.dataset import (
    AssetQuality,
    ClusterUnit,
    ConstraintOperator,
    DecisionStatus,
    HardConstraint,
    MetricDirection,
    QualityRule,
    SelectionPolicy,
    SplitAssignment,
    SplitManifest,
    SplitReason,
    build_stable_split_manifest,
    select_assets,
)


def test_stable_split_is_order_independent_and_keeps_cluster_together():
    clusters = [
        ClusterUnit.from_members(("a", "b")),
        ClusterUnit.from_members(("c",)),
        ClusterUnit.from_members(("d", "e", "f")),
    ]
    first = build_stable_split_manifest(clusters, {"train": 8, "validation": 1, "test": 1}, seed="dataset")
    second = build_stable_split_manifest(reversed(clusters), {"test": 1, "train": 8, "validation": 1}, seed="dataset")
    assert first.to_json() == second.to_json()
    member_splits = first.member_splits()
    assert member_splits["a"] == member_splits["b"]
    assert member_splits["d"] == member_splits["e"] == member_splits["f"]


def test_stable_split_preserves_previous_cluster_assignment():
    cluster = ClusterUnit.from_members(("a", "b"))
    previous = build_stable_split_manifest([cluster], {"train": 1, "validation": 1}, seed="old")
    updated = build_stable_split_manifest(
        [cluster],
        {"train": 1, "validation": 1},
        seed="new",
        previous=previous,
    )
    assert updated.assignments[0].split == previous.assignments[0].split
    assert updated.assignments[0].reason is SplitReason.PRESERVED_CLUSTER


def test_cluster_merge_chooses_deterministic_majority_and_records_conflict():
    first = ClusterUnit.from_members(("a", "b"))
    second = ClusterUnit.from_members(("c", "d"))
    previous = SplitManifest(
        seed="previous",
        split_weights=(("train", 0.5), ("validation", 0.5)),
        assignments=(
            SplitAssignment(first.cluster_id, first.members, "train", SplitReason.HASHED),
            SplitAssignment(second.cluster_id, second.members, "validation", SplitReason.HASHED),
        ),
    )
    merged = ClusterUnit.from_members(("a", "b", "c", "d"))
    manifest = build_stable_split_manifest(
        [merged],
        {"train": 1, "validation": 1},
        previous=previous,
    )
    assignment = manifest.assignments[0]
    assert assignment.split == "train"
    assert assignment.reason is SplitReason.MERGED_PREVIOUS_SPLITS
    assert assignment.previous_splits == ("train", "validation")


def test_split_rejects_overlapping_clusters():
    with pytest.raises(ValueError, match="must not overlap"):
        build_stable_split_manifest(
            [ClusterUnit.from_members(("a", "b")), ClusterUnit.from_members(("b", "c"))],
            {"train": 1},
        )


def test_quality_selection_respects_protection_constraint_and_diversity():
    assets = [
        AssetQuality.from_values("protected", {"resolution": 1}, flags=("protected",)),
        AssetQuality.from_values("sharp-similar", {"resolution": 10}),
        AssetQuality.from_values("diverse", {"resolution": 8}),
        AssetQuality.from_values("corrupt", {"resolution": 100}, flags=("corrupt",)),
    ]
    policy = SelectionPolicy(
        max_items=2,
        quality_rules=(QualityRule("resolution", 1),),
        hard_constraints=(HardConstraint("decodable", ConstraintOperator.FLAG_ABSENT, "corrupt"),),
        diversity_weight=2,
    )
    result = select_assets(
        assets,
        policy,
        distances={
            ("protected", "sharp-similar"): 0.1,
            ("protected", "diverse"): 0.9,
        },
    )
    assert result.selected_ids == ("protected", "diverse")
    assert result.decision_for("protected").forced
    assert result.decision_for("corrupt").status is DecisionStatus.INELIGIBLE
    assert result.decision_for("diverse").diversity_gain == 0.9
    assert result.decision_for("diverse").contributions[0].metric == "resolution"


def test_protected_asset_is_retained_even_when_it_violates_hard_constraint():
    assets = [
        AssetQuality.from_values("protected", {"resolution": 1}, flags=("protected", "corrupt")),
        AssetQuality.from_values("clean", {"resolution": 10}),
    ]
    result = select_assets(
        assets,
        SelectionPolicy(
            max_items=1,
            quality_rules=(QualityRule("resolution", 1),),
            hard_constraints=(HardConstraint("decodable", ConstraintOperator.FLAG_ABSENT, "corrupt"),),
        ),
    )
    decision = result.decision_for("protected")
    assert result.selected_ids == ("protected",)
    assert decision.status is DecisionStatus.SELECTED
    assert any("despite" in reason for reason in decision.reasons)


def test_required_metric_and_numeric_constraint_are_explainable():
    assets = [
        AssetQuality.from_values("small", {"artifact": 0.1}),
        AssetQuality.from_values("good", {"resolution": 20, "artifact": 0.2}),
        AssetQuality.from_values("bad-artifact", {"resolution": 30, "artifact": 0.9}),
    ]
    policy = SelectionPolicy(
        max_items=1,
        quality_rules=(
            QualityRule("resolution", 1, required=True),
            QualityRule("artifact", 1, direction=MetricDirection.LOWER_IS_BETTER),
        ),
        hard_constraints=(HardConstraint("artifact ceiling", ConstraintOperator.MAXIMUM, "artifact", 0.5),),
    )
    result = select_assets(assets, policy)
    assert result.selected_ids == ("good",)
    assert result.decision_for("small").status is DecisionStatus.INELIGIBLE
    assert result.decision_for("bad-artifact").status is DecisionStatus.INELIGIBLE
    assert "normalized" in result.decision_for("good").contributions[0].explanation


def test_multiple_forced_assets_exceed_budget_without_dropping_protected_data():
    result = select_assets(
        [
            AssetQuality.from_values("a", {}, flags=("protected",)),
            AssetQuality.from_values("b", {}, flags=("protected",)),
        ],
        SelectionPolicy(max_items=1),
    )
    assert result.selected_ids == ("a", "b")
    assert len(result.warnings) == 1


def test_diversity_selection_updates_each_candidate_pair_at_most_once(monkeypatch):
    asset_count = 80
    assets = tuple(
        AssetQuality.from_values(
            "asset-{:03d}".format(index),
            {"quality": float(asset_count - index)},
        )
        for index in range(asset_count)
    )
    distances = {
        (assets[left].asset_id, assets[right].asset_id): (((left + 1) * (right + 3) % 101) / 100.0)
        for left in range(asset_count)
        for right in range(left + 1, asset_count)
    }
    original = dataset_module._diversity_distance
    lookups = 0

    def counted_distance(values, first_asset_id, second_asset_id):
        nonlocal lookups
        lookups += 1
        return original(values, first_asset_id, second_asset_id)

    monkeypatch.setattr(
        dataset_module,
        "_diversity_distance",
        counted_distance,
    )

    result = select_assets(
        assets,
        SelectionPolicy(
            max_items=asset_count,
            quality_rules=(QualityRule("quality", 1),),
            diversity_weight=1,
        ),
        distances,
    )

    assert len(result.selected_ids) == asset_count
    assert lookups == asset_count * (asset_count - 1) // 2


def test_zero_diversity_weight_uses_quality_order_without_distance_lookups(
    monkeypatch,
):
    assets = tuple(
        AssetQuality.from_values(
            "asset-{:04d}".format(index),
            {"quality": float(index)},
        )
        for index in range(1_000)
    )

    def forbidden_distance(*_args):
        raise AssertionError("zero-weight selection must not inspect distances")

    monkeypatch.setattr(
        dataset_module,
        "_diversity_distance",
        forbidden_distance,
    )

    result = select_assets(
        assets,
        SelectionPolicy(
            max_items=25,
            quality_rules=(QualityRule("quality", 1),),
            diversity_weight=0,
        ),
        {
            (assets[0].asset_id, assets[-1].asset_id): 1.0,
        },
    )

    assert result.selected_ids == tuple("asset-{:04d}".format(index) for index in range(999, 974, -1))
    assert all(decision.diversity_gain == 0 for decision in result.decisions)
