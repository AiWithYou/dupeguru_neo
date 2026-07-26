# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Deterministic dataset splitting and explainable keep-selection policies."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

from core.pe.evidence import LeakageComponent, stable_group_id


@dataclass(frozen=True)
class ClusterUnit:
    cluster_id: str
    members: Tuple[str, ...]

    def __post_init__(self) -> None:
        members = tuple(sorted(set(self.members)))
        if not self.cluster_id or len(members) < 1 or any(not member for member in members):
            raise ValueError("cluster unit requires an ID and at least one non-empty member")
        object.__setattr__(self, "members", members)

    @classmethod
    def from_members(cls, members: Iterable[str]) -> "ClusterUnit":
        member_tuple = tuple(sorted(set(members)))
        if not member_tuple:
            raise ValueError("cluster unit requires members")
        if len(member_tuple) == 1:
            digest = hashlib.sha256(("dataset-cluster\0" + member_tuple[0]).encode("utf-8")).hexdigest()
            cluster_id = "dataset-cluster:{}".format(digest)
        else:
            cluster_id = stable_group_id("dataset-cluster", member_tuple)
        return cls(cluster_id, member_tuple)

    @classmethod
    def from_leakage_component(cls, component: LeakageComponent) -> "ClusterUnit":
        return cls(component.component_id, component.members)


class SplitReason(Enum):
    HASHED = "hashed"
    PRESERVED_CLUSTER = "preserved_cluster"
    PRESERVED_MEMBERS = "preserved_members"
    MERGED_PREVIOUS_SPLITS = "merged_previous_splits"


@dataclass(frozen=True)
class SplitAssignment:
    cluster_id: str
    members: Tuple[str, ...]
    split: str
    reason: SplitReason
    previous_splits: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.cluster_id or not self.split:
            raise ValueError("split assignment requires cluster and split IDs")
        members = tuple(sorted(set(self.members)))
        if not members:
            raise ValueError("split assignment requires members")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "previous_splits", tuple(sorted(set(self.previous_splits))))

    def to_dict(self) -> Dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "members": list(self.members),
            "split": self.split,
            "reason": self.reason.value,
            "previous_splits": list(self.previous_splits),
        }


@dataclass(frozen=True)
class SplitManifest:
    seed: str
    split_weights: Tuple[Tuple[str, float], ...]
    assignments: Tuple[SplitAssignment, ...]
    version: str = "stable_split_v1"

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("split manifest version must not be empty")
        weights = tuple(sorted(self.split_weights))
        assignments = tuple(sorted(self.assignments, key=lambda assignment: assignment.cluster_id))
        if not weights:
            raise ValueError("split manifest requires weights")
        valid_splits = {name for name, _ in weights}
        if any(assignment.split not in valid_splits for assignment in assignments):
            raise ValueError("assignment references an unknown split")
        seen_members = set()
        for assignment in assignments:
            overlap = seen_members & set(assignment.members)
            if overlap:
                raise ValueError("assets must not occur in multiple split assignments")
            seen_members.update(assignment.members)
        object.__setattr__(self, "split_weights", weights)
        object.__setattr__(self, "assignments", assignments)

    def cluster_splits(self) -> Dict[str, str]:
        return {assignment.cluster_id: assignment.split for assignment in self.assignments}

    def member_splits(self) -> Dict[str, str]:
        return {member: assignment.split for assignment in self.assignments for member in assignment.members}

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "seed": self.seed,
            "split_weights": {name: weight for name, weight in self.split_weights},
            "assignments": [assignment.to_dict() for assignment in self.assignments],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_split_weights(split_weights: Mapping[str, float]) -> Tuple[Tuple[str, float], ...]:
    if not split_weights:
        raise ValueError("at least one split is required")
    values = []
    for name, weight in split_weights.items():
        if not name or not math.isfinite(weight) or weight <= 0:
            raise ValueError("split names must be non-empty and weights must be finite and positive")
        values.append((name, float(weight)))
    total = sum(weight for _, weight in values)
    return tuple(sorted((name, weight / total) for name, weight in values))


def _hashed_split(cluster_id: str, seed: str, weights: Sequence[Tuple[str, float]]) -> str:
    digest = hashlib.sha256("{}\0{}".format(seed, cluster_id).encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") / float(1 << 64)
    cumulative = 0.0
    for name, weight in weights:
        cumulative += weight
        if value < cumulative:
            return name
    return weights[-1][0]


def build_stable_split_manifest(
    clusters: Iterable[ClusterUnit],
    split_weights: Mapping[str, float],
    seed: str = "",
    previous: Optional[SplitManifest] = None,
) -> SplitManifest:
    """Assign complete clusters deterministically, preserving prior assignments where possible."""

    weights = _normalized_split_weights(split_weights)
    valid_splits = {name for name, _ in weights}
    cluster_tuple = tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id))
    if len({cluster.cluster_id for cluster in cluster_tuple}) != len(cluster_tuple):
        raise ValueError("cluster IDs must be unique")
    seen_members: Set[str] = set()
    for cluster in cluster_tuple:
        overlap = seen_members & set(cluster.members)
        if overlap:
            raise ValueError("clusters must not overlap")
        seen_members.update(cluster.members)

    previous_cluster_splits = previous.cluster_splits() if previous is not None else {}
    previous_member_splits = previous.member_splits() if previous is not None else {}
    assignments = []
    for cluster in cluster_tuple:
        if (
            cluster.cluster_id in previous_cluster_splits
            and previous_cluster_splits[cluster.cluster_id] in valid_splits
        ):
            split = previous_cluster_splits[cluster.cluster_id]
            reason = SplitReason.PRESERVED_CLUSTER
            previous_splits = (split,)
        else:
            member_splits = tuple(
                previous_member_splits[member]
                for member in cluster.members
                if previous_member_splits.get(member) in valid_splits
            )
            unique_member_splits = tuple(sorted(set(member_splits)))
            if len(unique_member_splits) == 1:
                split = unique_member_splits[0]
                reason = SplitReason.PRESERVED_MEMBERS
                previous_splits = unique_member_splits
            elif len(unique_member_splits) > 1:
                counts = {
                    candidate_split: member_splits.count(candidate_split) for candidate_split in unique_member_splits
                }
                split = sorted(counts, key=lambda candidate_split: (-counts[candidate_split], candidate_split))[0]
                reason = SplitReason.MERGED_PREVIOUS_SPLITS
                previous_splits = unique_member_splits
            else:
                split = _hashed_split(cluster.cluster_id, seed, weights)
                reason = SplitReason.HASHED
                previous_splits = ()
        assignments.append(SplitAssignment(cluster.cluster_id, cluster.members, split, reason, previous_splits))
    return SplitManifest(seed, weights, tuple(assignments))


class MetricDirection(Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class ConstraintOperator(Enum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    FLAG_PRESENT = "flag_present"
    FLAG_ABSENT = "flag_absent"


class DecisionStatus(Enum):
    SELECTED = "selected"
    NOT_SELECTED = "not_selected"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class QualityMetric:
    name: str
    value: float

    def __post_init__(self) -> None:
        if not self.name or not math.isfinite(self.value):
            raise ValueError("quality metric requires a name and finite value")


@dataclass(frozen=True)
class AssetQuality:
    asset_id: str
    metrics: Tuple[QualityMetric, ...] = ()
    flags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("asset quality requires an asset ID")
        metrics = tuple(sorted(self.metrics, key=lambda metric: metric.name))
        if len({metric.name for metric in metrics}) != len(metrics):
            raise ValueError("quality metric names must be unique per asset")
        if any(not flag for flag in self.flags):
            raise ValueError("quality flags must not be empty")
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "flags", tuple(sorted(set(self.flags))))

    @classmethod
    def from_values(
        cls,
        asset_id: str,
        metrics: Mapping[str, float],
        flags: Iterable[str] = (),
    ) -> "AssetQuality":
        return cls(
            asset_id,
            tuple(QualityMetric(name, float(value)) for name, value in metrics.items()),
            tuple(flags),
        )

    def metric(self, name: str) -> Optional[float]:
        for metric in self.metrics:
            if metric.name == name:
                return metric.value
        return None


@dataclass(frozen=True)
class QualityRule:
    metric: str
    weight: float
    direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER
    required: bool = False

    def __post_init__(self) -> None:
        if not self.metric or not math.isfinite(self.weight) or self.weight < 0:
            raise ValueError("quality rule requires a metric and a finite non-negative weight")


@dataclass(frozen=True)
class HardConstraint:
    name: str
    operator: ConstraintOperator
    subject: str
    threshold: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.name or not self.subject:
            raise ValueError("hard constraint requires a name and subject")
        numeric = self.operator in {ConstraintOperator.MINIMUM, ConstraintOperator.MAXIMUM}
        if numeric:
            if self.threshold is None or not math.isfinite(self.threshold):
                raise ValueError("numeric hard constraint requires a finite threshold")
        elif self.threshold is not None:
            raise ValueError("flag hard constraint does not use a threshold")

    def evaluate(self, asset: AssetQuality) -> Tuple[bool, str]:
        if self.operator is ConstraintOperator.FLAG_PRESENT:
            passed = self.subject in asset.flags
            return passed, "{}: flag '{}' must be present".format(self.name, self.subject)
        if self.operator is ConstraintOperator.FLAG_ABSENT:
            passed = self.subject not in asset.flags
            return passed, "{}: flag '{}' must be absent".format(self.name, self.subject)
        value = asset.metric(self.subject)
        if value is None:
            return False, "{}: metric '{}' is missing".format(self.name, self.subject)
        if self.operator is ConstraintOperator.MINIMUM:
            passed = value >= self.threshold  # type: ignore[operator]
            return passed, "{}: {} must be at least {}".format(self.name, value, self.threshold)
        passed = value <= self.threshold  # type: ignore[operator]
        return passed, "{}: {} must be at most {}".format(self.name, value, self.threshold)


@dataclass(frozen=True)
class SelectionPolicy:
    max_items: int
    quality_rules: Tuple[QualityRule, ...] = ()
    hard_constraints: Tuple[HardConstraint, ...] = ()
    force_keep_flags: Tuple[str, ...] = ("protected",)
    diversity_weight: float = 0.0

    def __post_init__(self) -> None:
        if self.max_items < 1:
            raise ValueError("selection policy max_items must be positive")
        if not math.isfinite(self.diversity_weight) or self.diversity_weight < 0:
            raise ValueError("diversity_weight must be finite and non-negative")
        if len({rule.metric for rule in self.quality_rules}) != len(self.quality_rules):
            raise ValueError("quality rules must use unique metrics")
        if len({constraint.name for constraint in self.hard_constraints}) != len(self.hard_constraints):
            raise ValueError("hard constraint names must be unique")
        if any(not flag for flag in self.force_keep_flags):
            raise ValueError("force-keep flags must not be empty")
        object.__setattr__(self, "quality_rules", tuple(self.quality_rules))
        object.__setattr__(self, "hard_constraints", tuple(self.hard_constraints))
        object.__setattr__(self, "force_keep_flags", tuple(sorted(set(self.force_keep_flags))))


@dataclass(frozen=True)
class MetricContribution:
    metric: str
    raw_value: Optional[float]
    normalized_value: float
    weighted_value: float
    explanation: str


@dataclass(frozen=True)
class AssetDecision:
    asset_id: str
    status: DecisionStatus
    forced: bool
    quality_score: float
    diversity_gain: float
    marginal_score: float
    contributions: Tuple[MetricContribution, ...]
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class SelectionResult:
    selected_ids: Tuple[str, ...]
    decisions: Tuple[AssetDecision, ...]
    warnings: Tuple[str, ...] = ()

    def decision_for(self, asset_id: str) -> AssetDecision:
        for decision in self.decisions:
            if decision.asset_id == asset_id:
                return decision
        raise KeyError(asset_id)


def _normalized_distances(
    distances: Mapping[Tuple[str, str], float],
) -> Dict[Tuple[str, str], float]:
    result: Dict[Tuple[str, str], float] = {}
    for pair, distance in distances.items():
        if len(pair) != 2 or not pair[0] or not pair[1] or pair[0] == pair[1]:
            raise ValueError("diversity distance key must contain two distinct asset IDs")
        if not math.isfinite(distance) or not 0 <= distance <= 1:
            raise ValueError("diversity distance must be between 0 and 1")
        key = tuple(sorted(pair))
        if key in result and result[key] != distance:
            raise ValueError("conflicting diversity distances for the same pair")
        result[key] = distance
    return result


def _diversity_distance(
    distances: Mapping[Tuple[str, str], float],
    first_asset_id: str,
    second_asset_id: str,
) -> float:
    """Return one normalized distance through a single instrumentable lookup."""

    return distances.get(
        tuple(sorted((first_asset_id, second_asset_id))),
        0.0,
    )


def select_assets(
    assets: Iterable[AssetQuality],
    policy: SelectionPolicy,
    distances: Optional[Mapping[Tuple[str, str], float]] = None,
) -> SelectionResult:
    """Select keep candidates with hard safety constraints and deterministic explanations."""

    asset_tuple = tuple(sorted(assets, key=lambda asset: asset.asset_id))
    if not asset_tuple:
        raise ValueError("selection requires assets")
    if len({asset.asset_id for asset in asset_tuple}) != len(asset_tuple):
        raise ValueError("asset IDs must be unique")
    normalized_distances = _normalized_distances(distances or {})

    constraint_failures: Dict[str, Tuple[str, ...]] = {}
    forced: Set[str] = set()
    for asset in asset_tuple:
        failures = []
        for constraint in policy.hard_constraints:
            passed, explanation = constraint.evaluate(asset)
            if not passed:
                failures.append(explanation)
        for rule in policy.quality_rules:
            if rule.required and asset.metric(rule.metric) is None:
                failures.append("required metric '{}' is missing".format(rule.metric))
        constraint_failures[asset.asset_id] = tuple(failures)
        if set(asset.flags) & set(policy.force_keep_flags):
            forced.add(asset.asset_id)

    metric_ranges: Dict[str, Tuple[float, float]] = {}
    for rule in policy.quality_rules:
        values = [value for asset in asset_tuple for value in [asset.metric(rule.metric)] if value is not None]
        if values:
            metric_ranges[rule.metric] = (min(values), max(values))

    contributions_by_asset: Dict[str, Tuple[MetricContribution, ...]] = {}
    quality_scores: Dict[str, float] = {}
    for asset in asset_tuple:
        contributions = []
        for rule in policy.quality_rules:
            raw_value = asset.metric(rule.metric)
            if raw_value is None:
                normalized = 0.0
                explanation = "{} is missing; contribution is 0".format(rule.metric)
            else:
                minimum, maximum = metric_ranges[rule.metric]
                if minimum == maximum:
                    normalized = 0.5
                else:
                    normalized = (raw_value - minimum) / (maximum - minimum)
                if rule.direction is MetricDirection.LOWER_IS_BETTER:
                    normalized = 1 - normalized
                explanation = "{}={} normalized to {:.6f}".format(rule.metric, raw_value, normalized)
            weighted = normalized * rule.weight
            contributions.append(MetricContribution(rule.metric, raw_value, normalized, weighted, explanation))
        contributions_by_asset[asset.asset_id] = tuple(contributions)
        quality_scores[asset.asset_id] = sum(item.weighted_value for item in contributions)

    selected = sorted(forced)
    warnings = []
    if len(selected) > policy.max_items:
        warnings.append(
            "{} forced-keep assets exceed max_items={}; all were retained".format(len(selected), policy.max_items)
        )
    eligible = {
        asset.asset_id
        for asset in asset_tuple
        if not constraint_failures[asset.asset_id] and asset.asset_id not in forced
    }
    slots = max(0, policy.max_items - len(selected))
    selection_diversity: Dict[str, float] = {asset_id: 0.0 for asset_id in selected}
    selection_marginal: Dict[str, float] = {asset_id: quality_scores[asset_id] for asset_id in selected}
    final_diversity: Dict[str, float] = {asset_id: 0.0 for asset_id in eligible}

    if policy.diversity_weight == 0:
        # With no diversity contribution, a single deterministic sort is both
        # sufficient and substantially cheaper for very large candidate sets.
        # Diversity is deliberately reported as zero because it has no bearing
        # on either selection or the marginal score in this policy.
        chosen_ids = sorted(
            eligible,
            key=lambda asset_id: (-quality_scores[asset_id], asset_id),
        )[:slots]
        for chosen in chosen_ids:
            selected.append(chosen)
            selection_diversity[chosen] = 0.0
            selection_marginal[chosen] = quality_scores[chosen]
            eligible.remove(chosen)
    else:
        has_selected = bool(selected)
        if has_selected:
            for asset_id in eligible:
                final_diversity[asset_id] = min(
                    _diversity_distance(
                        normalized_distances,
                        asset_id,
                        selected_id,
                    )
                    for selected_id in selected
                )

        # Each greedy round performs one linear winner scan and one linear
        # incremental min-distance update. Every candidate pair is therefore
        # considered at most once instead of rescanning the full selected set
        # for every eligible asset on every round.
        while slots and eligible:
            chosen = min(
                eligible,
                key=lambda asset_id: (
                    -(quality_scores[asset_id] + policy.diversity_weight * final_diversity[asset_id]),
                    -quality_scores[asset_id],
                    asset_id,
                ),
            )
            diversity = final_diversity[chosen]
            marginal = quality_scores[chosen] + policy.diversity_weight * diversity
            selected.append(chosen)
            selection_diversity[chosen] = diversity
            selection_marginal[chosen] = marginal
            eligible.remove(chosen)
            slots -= 1

            previously_selected = has_selected
            has_selected = True
            for asset_id in eligible:
                distance = _diversity_distance(
                    normalized_distances,
                    asset_id,
                    chosen,
                )
                if previously_selected:
                    final_diversity[asset_id] = min(
                        final_diversity[asset_id],
                        distance,
                    )
                else:
                    final_diversity[asset_id] = distance

    selected_set = set(selected)
    decisions = []
    for asset in asset_tuple:
        asset_id = asset.asset_id
        failures = constraint_failures[asset_id]
        is_forced = asset_id in forced
        if asset_id in selected_set:
            status = DecisionStatus.SELECTED
            reasons = []
            if is_forced:
                reasons.append("forced keep by flag")
                if failures:
                    reasons.extend("forced keep despite {}".format(failure) for failure in failures)
            else:
                reasons.append("selected by quality and diversity policy")
            diversity = selection_diversity[asset_id]
            marginal = selection_marginal[asset_id]
        elif failures:
            status = DecisionStatus.INELIGIBLE
            reasons = list(failures)
            diversity = 0.0
            marginal = quality_scores[asset_id]
        else:
            status = DecisionStatus.NOT_SELECTED
            reasons = ["not selected within max_items budget"]
            diversity = final_diversity.get(asset_id, 0.0)
            marginal = quality_scores[asset_id] + policy.diversity_weight * diversity
        decisions.append(
            AssetDecision(
                asset_id=asset_id,
                status=status,
                forced=is_forced,
                quality_score=quality_scores[asset_id],
                diversity_gain=diversity,
                marginal_score=marginal,
                contributions=contributions_by_asset[asset_id],
                reasons=tuple(reasons),
            )
        )
    return SelectionResult(
        selected_ids=tuple(selected),
        decisions=tuple(decisions),
        warnings=tuple(warnings),
    )
