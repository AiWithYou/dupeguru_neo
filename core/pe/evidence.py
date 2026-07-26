# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Typed evidence and grouping primitives for picture-library scans.

The existing matching engine represents every result as a percentage.  This module deliberately
keeps destructive proof, visual similarity, and semantic relatedness as different relation types.
It has no Qt or image-decoder dependency and can therefore be shared by GUI, CLI, and catalog code.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple


class RelationType(Enum):
    """The meaning of an edge between two assets."""

    VERIFIED_EXACT = "verified_exact"
    PIXEL_EQUIVALENT = "pixel_equivalent"
    NEAR_DUPLICATE = "near_duplicate"
    TRANSFORMED_VARIANT = "transformed_variant"
    SEMANTIC_RELATED = "semantic_related"

    @property
    def allows_automatic_destructive_action(self) -> bool:
        return self is RelationType.VERIFIED_EXACT


class OrientationTransform(Enum):
    """The eight square symmetries used by EXIF and rotation-aware matching."""

    IDENTITY = "identity"
    ROTATE_90 = "rotate_90"
    ROTATE_180 = "rotate_180"
    ROTATE_270 = "rotate_270"
    FLIP_HORIZONTAL = "flip_horizontal"
    FLIP_VERTICAL = "flip_vertical"
    TRANSPOSE = "transpose"
    TRANSVERSE = "transverse"


@dataclass(frozen=True)
class CropRect:
    """A normalized crop rectangle in the source image coordinate space."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("crop coordinates must be finite")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("crop rectangle must have a positive size and non-negative origin")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop rectangle must fit inside normalized image bounds")

    def to_dict(self) -> Dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class ImageTransform:
    """The alignment which explains a visual relationship.

    Scale values are relative to the first asset.  ``crop`` is also expressed in the first asset's
    normalized coordinate space.  The transform records evidence; it does not mutate either asset.
    """

    orientation: OrientationTransform = OrientationTransform.IDENTITY
    scale_x: float = 1.0
    scale_y: float = 1.0
    crop: Optional[CropRect] = None
    color_adjusted: bool = False
    watermark_changed: bool = False
    alignment_score: Optional[float] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale_x) or not math.isfinite(self.scale_y):
            raise ValueError("scale must be finite")
        if self.scale_x <= 0 or self.scale_y <= 0:
            raise ValueError("scale must be positive")
        if self.alignment_score is not None and not 0 <= self.alignment_score <= 1:
            raise ValueError("alignment_score must be between 0 and 1")
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def is_identity(self) -> bool:
        return (
            self.orientation is OrientationTransform.IDENTITY
            and self.scale_x == 1.0
            and self.scale_y == 1.0
            and self.crop is None
            and not self.color_adjusted
            and not self.watermark_changed
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "orientation": self.orientation.value,
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "crop": self.crop.to_dict() if self.crop is not None else None,
            "color_adjusted": self.color_adjusted,
            "watermark_changed": self.watermark_changed,
            "alignment_score": self.alignment_score,
            "notes": list(self.notes),
        }


def _validate_asset_id(asset_id: str) -> None:
    if not asset_id or "\0" in asset_id:
        raise ValueError("asset_id must be a non-empty string without NUL characters")


@dataclass(frozen=True)
class FileSnapshot:
    """The identity and content state used by an exact comparison."""

    asset_id: str
    path: str
    size: int
    mtime_ns: int
    digest_algorithm: str
    digest: bytes
    volume_id: str = ""
    file_id: str = ""

    def __post_init__(self) -> None:
        _validate_asset_id(self.asset_id)
        if not self.path:
            raise ValueError("snapshot path must not be empty")
        if self.size < 0 or self.mtime_ns < 0:
            raise ValueError("snapshot size and mtime_ns must be non-negative")
        if not self.digest_algorithm:
            raise ValueError("digest_algorithm must not be empty")
        if not isinstance(self.digest, bytes) or not self.digest:
            raise ValueError("digest must be non-empty bytes")

    def to_dict(self) -> Dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "digest_algorithm": self.digest_algorithm,
            "digest": self.digest.hex(),
            "volume_id": self.volume_id,
            "file_id": self.file_id,
        }


@dataclass(frozen=True)
class ExactProof:
    """Proof that two snapshots were byte-compared in full.

    A digest match alone is intentionally insufficient to construct this type.  ``bytes_compared``
    must cover the complete file, including the valid zero-byte-file case.
    """

    first: FileSnapshot
    second: FileSnapshot
    bytes_compared: int
    comparator_version: str = "byte_compare_v1"
    compared_at_ns: int = 0

    def __post_init__(self) -> None:
        if self.first.asset_id == self.second.asset_id:
            raise ValueError("exact proof requires two distinct assets")
        if self.first.size != self.second.size:
            raise ValueError("exact proof requires equal file sizes")
        if self.first.digest_algorithm != self.second.digest_algorithm:
            raise ValueError("exact proof requires the same digest algorithm")
        if self.first.digest != self.second.digest:
            raise ValueError("exact proof requires equal full digests")
        if self.bytes_compared != self.first.size:
            raise ValueError("exact proof requires a full byte comparison")
        if not self.comparator_version:
            raise ValueError("comparator_version must not be empty")
        if self.compared_at_ns < 0:
            raise ValueError("compared_at_ns must be non-negative")

    @property
    def pair(self) -> Tuple[str, str]:
        return tuple(sorted((self.first.asset_id, self.second.asset_id)))  # type: ignore[return-value]

    def to_dict(self) -> Dict[str, object]:
        return {
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
            "bytes_compared": self.bytes_compared,
            "comparator_version": self.comparator_version,
            "compared_at_ns": self.compared_at_ns,
        }


@dataclass(frozen=True)
class EvidenceMetric:
    name: str
    value: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("metric name must not be empty")
        if not math.isfinite(self.value):
            raise ValueError("metric value must be finite")


@dataclass(frozen=True)
class MatchEvidence:
    """Typed, versioned evidence for one directed comparison."""

    first_id: str
    second_id: str
    relation: RelationType
    score: float
    algorithm: str
    algorithm_version: str
    transform: ImageTransform = field(default_factory=ImageTransform)
    metrics: Tuple[EvidenceMetric, ...] = ()
    exact_proof: Optional[ExactProof] = None

    def __post_init__(self) -> None:
        _validate_asset_id(self.first_id)
        _validate_asset_id(self.second_id)
        if self.first_id == self.second_id:
            raise ValueError("match evidence requires two distinct assets")
        if not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("score must be a finite number between 0 and 1")
        if not self.algorithm or not self.algorithm_version:
            raise ValueError("algorithm and algorithm_version must not be empty")
        metrics = tuple(sorted(self.metrics, key=lambda metric: metric.name))
        if len({metric.name for metric in metrics}) != len(metrics):
            raise ValueError("metric names must be unique")
        object.__setattr__(self, "metrics", metrics)
        if self.relation is RelationType.VERIFIED_EXACT:
            if self.exact_proof is None:
                raise ValueError("verified exact evidence requires ExactProof")
            if set(self.exact_proof.pair) != {self.first_id, self.second_id}:
                raise ValueError("exact proof assets must match evidence assets")
            if self.score != 1:
                raise ValueError("verified exact evidence must have score 1")
            if not self.transform.is_identity:
                raise ValueError("verified exact evidence must use the identity transform")
        elif self.exact_proof is not None:
            raise ValueError("ExactProof is only valid for the verified exact relation")

    @property
    def pair(self) -> Tuple[str, str]:
        return tuple(sorted((self.first_id, self.second_id)))  # type: ignore[return-value]

    def metric(self, name: str) -> Optional[float]:
        for metric in self.metrics:
            if metric.name == name:
                return metric.value
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "first_id": self.first_id,
            "second_id": self.second_id,
            "relation": self.relation.value,
            "score": self.score,
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "transform": self.transform.to_dict(),
            "metrics": {metric.name: metric.value for metric in self.metrics},
            "exact_proof": self.exact_proof.to_dict() if self.exact_proof is not None else None,
        }


class ScanState(Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_SKIPS = "complete_with_skips"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INCOMPLETE_RESOURCE_LIMIT = "incomplete_resource_limit"


@dataclass(frozen=True)
class ScanReceipt:
    """Coverage evidence for a scan, independent of its match results."""

    scan_id: str
    state: ScanState
    discovered: int
    indexed: int
    analyzed: int
    failed: int = 0
    skipped: int = 0
    algorithm_versions: Tuple[str, ...] = ()
    messages: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.scan_id:
            raise ValueError("scan_id must not be empty")
        counts = (self.discovered, self.indexed, self.analyzed, self.failed, self.skipped)
        if any(count < 0 for count in counts):
            raise ValueError("scan counts must be non-negative")
        if self.indexed > self.discovered:
            raise ValueError("indexed count cannot exceed discovered count")
        if self.analyzed + self.failed + self.skipped > self.discovered:
            raise ValueError("analyzed, failed, and skipped counts cannot exceed discovered count")
        object.__setattr__(self, "algorithm_versions", tuple(sorted(set(self.algorithm_versions))))
        object.__setattr__(self, "messages", tuple(self.messages))

    @property
    def allows_automatic_destructive_action(self) -> bool:
        return self.state is ScanState.COMPLETE and self.failed == 0 and self.skipped == 0


def stable_group_id(kind: str, members: Iterable[str], discriminator: str = "") -> str:
    """Return an order-independent stable identifier."""

    normalized = _normalized_members(members)
    hasher = hashlib.sha256()
    hasher.update(kind.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(discriminator.encode("utf-8"))
    for member in normalized:
        hasher.update(b"\0")
        hasher.update(member.encode("utf-8"))
    return "{}:{}".format(kind, hasher.hexdigest())


def _normalized_members(members: Iterable[str]) -> Tuple[str, ...]:
    result = tuple(sorted(set(members)))
    if len(result) < 2:
        raise ValueError("a group requires at least two distinct assets")
    for member in result:
        _validate_asset_id(member)
    return result


def _evidence_sort_key(evidence: MatchEvidence) -> Tuple[object, ...]:
    return (
        evidence.pair,
        evidence.relation.value,
        evidence.algorithm,
        evidence.algorithm_version,
        evidence.score,
    )


def _ensure_unique_edges(evidences: Sequence[MatchEvidence]) -> None:
    pairs = [evidence.pair for evidence in evidences]
    if len(set(pairs)) != len(pairs):
        raise ValueError("a group cannot contain multiple evidence records for the same asset pair")


def _is_connected(members: Sequence[str], edges: Iterable[Tuple[str, str]]) -> bool:
    adjacency: Dict[str, Set[str]] = {member: set() for member in members}
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    visited = set()
    pending = [members[0]]
    while pending:
        member = pending.pop()
        if member in visited:
            continue
        visited.add(member)
        pending.extend(adjacency[member] - visited)
    return visited == set(members)


@dataclass(frozen=True)
class ExactGroup:
    """An exact equivalence class proven with O(k) connected byte comparisons."""

    members: Tuple[str, ...]
    canonical_id: str
    digest_algorithm: str
    digest: bytes
    size: int
    proofs: Tuple[ExactProof, ...]
    group_id: str = field(init=False)

    def __post_init__(self) -> None:
        members = _normalized_members(self.members)
        proofs = tuple(sorted(self.proofs, key=lambda proof: proof.pair))
        if self.canonical_id not in members:
            raise ValueError("canonical_id must be an exact-group member")
        if not self.digest_algorithm or not isinstance(self.digest, bytes) or not self.digest:
            raise ValueError("exact group requires a digest algorithm and digest")
        if self.size < 0:
            raise ValueError("exact group size must be non-negative")
        proof_pairs = [proof.pair for proof in proofs]
        if len(set(proof_pairs)) != len(proof_pairs):
            raise ValueError("duplicate exact proof pair")
        proof_members = set()
        for proof in proofs:
            proof_members.update(proof.pair)
            if proof.first.digest_algorithm != self.digest_algorithm or proof.first.digest != self.digest:
                raise ValueError("all exact proofs must use the group digest")
            if proof.first.size != self.size:
                raise ValueError("all exact proofs must use the group size")
        if proof_members != set(members):
            raise ValueError("exact proofs must cover every group member")
        if not _is_connected(members, proof_pairs):
            raise ValueError("exact proofs must form a connected graph")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "proofs", proofs)
        object.__setattr__(
            self,
            "group_id",
            stable_group_id("exact", members, "{}:{}".format(self.digest_algorithm, self.digest.hex())),
        )

    @classmethod
    def from_proofs(cls, proofs: Iterable[ExactProof], canonical_id: Optional[str] = None) -> "ExactGroup":
        proof_tuple = tuple(proofs)
        if not proof_tuple:
            raise ValueError("exact group requires proofs")
        members = tuple(sorted({asset_id for proof in proof_tuple for asset_id in proof.pair}))
        first = proof_tuple[0].first
        return cls(
            members=members,
            canonical_id=canonical_id or members[0],
            digest_algorithm=first.digest_algorithm,
            digest=first.digest,
            size=first.size,
            proofs=proof_tuple,
        )


_REVIEW_RELATIONS = frozenset(
    {
        RelationType.VERIFIED_EXACT,
        RelationType.PIXEL_EQUIVALENT,
        RelationType.NEAR_DUPLICATE,
        RelationType.TRANSFORMED_VARIANT,
    }
)


@dataclass(frozen=True)
class ReviewGroup:
    """A bounded set intended for a human duplicate-review decision."""

    members: Tuple[str, ...]
    representative_id: str
    evidence: Tuple[MatchEvidence, ...]
    require_clique: bool = True
    group_id: str = field(init=False)

    def __post_init__(self) -> None:
        members = _normalized_members(self.members)
        evidence = tuple(sorted(self.evidence, key=_evidence_sort_key))
        if self.representative_id not in members:
            raise ValueError("representative_id must be a review-group member")
        if not evidence:
            raise ValueError("review group requires evidence")
        if any(item.relation not in _REVIEW_RELATIONS for item in evidence):
            raise ValueError("semantic-only evidence is not valid in a duplicate review group")
        _ensure_unique_edges(evidence)
        evidence_members = {asset_id for item in evidence for asset_id in item.pair}
        if evidence_members != set(members):
            raise ValueError("review evidence must cover every group member")
        pairs = [item.pair for item in evidence]
        if not _is_connected(members, pairs):
            raise ValueError("review evidence must form a connected graph")
        if self.require_clique and len(pairs) != len(members) * (len(members) - 1) // 2:
            raise ValueError("clique review group requires evidence for every pair")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "group_id", stable_group_id("review", members))

    @classmethod
    def from_evidence(
        cls,
        evidence: Iterable[MatchEvidence],
        representative_id: Optional[str] = None,
        require_clique: bool = True,
    ) -> "ReviewGroup":
        evidence_tuple = tuple(evidence)
        members = tuple(sorted({asset_id for item in evidence_tuple for asset_id in item.pair}))
        if not members:
            raise ValueError("review group requires evidence")
        return cls(members, representative_id or members[0], evidence_tuple, require_clique)


@dataclass(frozen=True)
class LeakageComponent:
    """A connected visual component which must stay inside one dataset split."""

    members: Tuple[str, ...]
    evidence: Tuple[MatchEvidence, ...]
    component_id: str = field(init=False)

    def __post_init__(self) -> None:
        members = _normalized_members(self.members)
        evidence = tuple(sorted(self.evidence, key=_evidence_sort_key))
        if not evidence:
            raise ValueError("leakage component requires evidence")
        evidence_members = {asset_id for item in evidence for asset_id in item.pair}
        if evidence_members != set(members):
            raise ValueError("leakage evidence must cover every component member")
        if not _is_connected(members, (item.pair for item in evidence)):
            raise ValueError("leakage evidence must form a connected graph")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "component_id", stable_group_id("leakage", members))


def build_leakage_components(
    evidences: Iterable[MatchEvidence],
    relation_types: FrozenSet[RelationType] = _REVIEW_RELATIONS,
) -> Tuple[LeakageComponent, ...]:
    """Build deterministic connected components from the selected relation types."""

    filtered = tuple(item for item in evidences if item.relation in relation_types)
    parent: Dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != item:
            next_item = parent[item]
            parent[item] = root
            item = next_item
        return root

    def union(first: str, second: str) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        low, high = sorted((first_root, second_root))
        parent[high] = low

    for evidence in filtered:
        union(evidence.first_id, evidence.second_id)
    members_by_root: Dict[str, Set[str]] = {}
    for member in parent:
        members_by_root.setdefault(find(member), set()).add(member)
    result: List[LeakageComponent] = []
    for members_set in members_by_root.values():
        if len(members_set) < 2:
            continue
        component_evidence = tuple(
            item for item in filtered if item.first_id in members_set and item.second_id in members_set
        )
        result.append(LeakageComponent(tuple(members_set), component_evidence))
    return tuple(sorted(result, key=lambda component: component.component_id))


@dataclass(frozen=True)
class SemanticGroup:
    """A star-shaped related-items result which is never a deletion group."""

    center_id: str
    related_ids: Tuple[str, ...]
    evidence: Tuple[MatchEvidence, ...]
    group_id: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_asset_id(self.center_id)
        related_ids = tuple(sorted(set(self.related_ids)))
        if not related_ids or self.center_id in related_ids:
            raise ValueError("semantic group requires at least one distinct related asset")
        evidence = tuple(sorted(self.evidence, key=_evidence_sort_key))
        if any(item.relation is not RelationType.SEMANTIC_RELATED for item in evidence):
            raise ValueError("semantic group only accepts semantic-related evidence")
        _ensure_unique_edges(evidence)
        for item in evidence:
            if self.center_id not in item.pair:
                raise ValueError("semantic evidence must connect to the center asset")
        evidence_related = {next(asset_id for asset_id in item.pair if asset_id != self.center_id) for item in evidence}
        if evidence_related != set(related_ids):
            raise ValueError("semantic evidence must cover every related asset")
        object.__setattr__(self, "related_ids", related_ids)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "group_id", stable_group_id("semantic", (self.center_id,) + related_ids))

    @classmethod
    def from_evidence(cls, center_id: str, evidence: Iterable[MatchEvidence]) -> "SemanticGroup":
        evidence_tuple = tuple(evidence)
        related_ids = tuple(
            sorted({asset_id for item in evidence_tuple for asset_id in item.pair if asset_id != center_id})
        )
        return cls(center_id, related_ids, evidence_tuple)
