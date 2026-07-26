# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Fail-closed root discovery for image-dataset preparation.

The visual service supplies candidate and leakage edges only.  A cluster is marked
``verified_exact`` exclusively when the independent exact scan reports SHA-256
proofs backed by the core streaming byte comparison.  The dataset service then
re-verifies those members while it creates its immutable action plan.
"""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from PIL import Image

from core.dataset_service import (
    DatasetAsset,
    DatasetCluster,
    DatasetIssue,
    DatasetModeService,
    DatasetPreparation,
    DatasetRelation,
    PreparationState,
)
from core.file_generation import (
    get_file_generation_token,
    get_file_generation_token_from_fd,
)
from core.file_identity import (
    FileIdentityError,
    IdentityVerdict,
    get_file_identity,
    same_physical_file,
)
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_unsafe_path_component,
    is_within_reserved_internal_directory,
)
from core.safe_action import platform_file_system
from core.safe_walk import is_reparse_point
from core.services import ScanRequest, ScanService
from core.services.models import DELETION_PROOF_ALGORITHM, VERIFIED_EXACT
from core.visual_service import VisualRelation, VisualScanConfig, VisualService

DISCOVERY_EVIDENCE_VERSION = "dataset-discovery-evidence-v1"
EXACT_VERIFICATION_SUFFIX = "+core-streaming-byte-compare"
DEFAULT_SPLIT_WEIGHTS = (
    ("train", 0.8),
    ("validation", 0.1),
    ("test", 0.1),
)


@dataclass(frozen=True)
class DatasetRootRequest:
    """Normalized options for discovering a dataset directly from roots."""

    roots: Tuple[str, ...]
    destination_root: str
    protected_roots: Tuple[str, ...] = ()
    visual_cache: Optional[str] = None
    state_root: Optional[str] = None
    similarity_threshold: int = 80
    phash_radius: int = 8
    match_scaled: bool = False
    match_rotated: bool = False
    split_weights: Tuple[Tuple[str, float], ...] = DEFAULT_SPLIT_WEIGHTS
    split_seed: str = "dupeguru-dataset-root-v1"
    dry_run: bool = True

    def __post_init__(self) -> None:
        roots = tuple(str(root) for root in self.roots)
        protected = tuple(str(root) for root in self.protected_roots)
        weights = tuple((str(name), float(weight)) for name, weight in self.split_weights)
        if not roots or any(not root or "\0" in root for root in roots):
            raise ValueError("dataset root discovery requires safe input roots")
        if not self.destination_root or "\0" in self.destination_root:
            raise ValueError("dataset root discovery requires a safe destination root")
        if any(not root or "\0" in root for root in protected):
            raise ValueError("protected roots must be safe directory paths")
        if self.visual_cache is not None and (not self.visual_cache or "\0" in self.visual_cache):
            raise ValueError("visual cache must be a safe file path")
        if self.state_root is not None and (not self.state_root or "\0" in self.state_root):
            raise ValueError("executor state root must be a safe directory path")
        if not isinstance(self.match_scaled, bool) or not isinstance(self.match_rotated, bool):
            raise ValueError("visual transform flags must be boolean")
        if not isinstance(self.dry_run, bool):
            raise ValueError("dataset dry-run state must be boolean")
        if not self.split_seed or "\0" in self.split_seed:
            raise ValueError("dataset split seed must be a non-empty safe string")
        if not weights or any(not name or not math.isfinite(weight) or weight <= 0 for name, weight in weights):
            raise ValueError("dataset split weights must be named finite positive values")
        if len({name for name, _weight in weights}) != len(weights):
            raise ValueError("dataset split names must be unique")
        if any(is_unsafe_path_component(name) or is_reserved_internal_directory(name) for name, _weight in weights):
            raise ValueError("dataset split names must not use dupeGuru Neo internal directory names")
        # Reuse the visual service's public validation contract for both numeric options.
        VisualScanConfig(
            similarity_threshold=self.similarity_threshold,
            phash_radius=self.phash_radius,
            match_scaled=self.match_scaled,
            match_rotated=self.match_rotated,
        )
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "protected_roots", protected)
        object.__setattr__(self, "split_weights", weights)


class DatasetDiscoveryError(RuntimeError):
    """An audit-friendly discovery failure that can be converted to a dataset issue."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Optional[str | Path] = None,
        state: PreparationState = PreparationState.FAILED,
    ) -> None:
        self.code = code
        self.path = Path(path) if path is not None else None
        self.state = state
        super().__init__(message)

    def issue(self) -> DatasetIssue:
        paths = (str(self.path),) if self.path is not None else ()
        return DatasetIssue(self.code, str(self), paths)


class _DisjointSet:
    def __init__(self, members: Iterable[str]) -> None:
        self._parent = {member: member for member in members}
        self._size = {member: 1 for member in self._parent}
        self._minimum = dict(self._parent)

    def _find_root(self, member: str) -> str:
        root = member
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[member] != member:
            parent = self._parent[member]
            self._parent[member] = root
            member = parent
        return root

    def find(self, member: str) -> str:
        return self._minimum[self._find_root(member)]

    def union(self, first: str, second: str) -> None:
        first_root = self._find_root(first)
        second_root = self._find_root(second)
        if first_root == second_root:
            return
        first_size = self._size[first_root]
        second_size = self._size[second_root]
        if first_size < second_size or (first_size == second_size and first_root > second_root):
            first_root, second_root = second_root, first_root
            first_size, second_size = second_size, first_size
        self._parent[second_root] = first_root
        self._size[first_root] = first_size + second_size
        self._minimum[first_root] = min(
            self._minimum[first_root],
            self._minimum[second_root],
        )
        del self._size[second_root]
        del self._minimum[second_root]

    def components(self) -> Tuple[Tuple[str, ...], ...]:
        grouped: Dict[str, list[str]] = {}
        for member in sorted(self._parent):
            grouped.setdefault(self.find(member), []).append(member)
        return tuple(
            sorted(
                (tuple(members) for members in grouped.values()),
                key=lambda members: members,
            )
        )


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: str | Path, root: str | Path) -> bool:
    try:
        candidate = _path_key(path)
        container = _path_key(root)
        return os.path.commonpath((candidate, container)) == container
    except ValueError:
        return False


def _path_generation(path, value, identity) -> Tuple[int, int, str]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        get_file_generation_token(
            path,
            follow_symlinks=False,
            stat_result=value,
            expected_identity=identity,
        ).encoded.hex(),
    )


def _handle_generation(handle, path, value, identity) -> Tuple[int, int, str]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        get_file_generation_token_from_fd(
            handle.fileno(),
            path,
            stat_result=value,
            expected_identity=identity,
        ).encoded.hex(),
    )


def _snapshot_generation(snapshot) -> Tuple[int, int, str]:
    return (
        int(snapshot.size),
        int(snapshot.mtime_ns),
        str(snapshot.generation_token),
    )


def _canonical_directory(path: str | Path, label: str) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        entry = os.stat(candidate, follow_symlinks=False)
    except OSError as error:
        raise DatasetDiscoveryError(
            "{}_unavailable".format(label),
            str(error),
            path=candidate,
        ) from error
    if stat.S_ISLNK(entry.st_mode) or is_reparse_point(entry) or not stat.S_ISDIR(entry.st_mode):
        raise DatasetDiscoveryError(
            "unsafe_{}".format(label),
            "{} must be a plain physical directory".format(label.replace("_", " ")),
            path=candidate,
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise DatasetDiscoveryError(
            "{}_unavailable".format(label),
            str(error),
            path=candidate,
        ) from error
    if _path_key(candidate) != _path_key(resolved):
        raise DatasetDiscoveryError(
            "unsafe_{}".format(label),
            "{} must not traverse symbolic links or reparse points".format(label.replace("_", " ")),
            path=candidate,
        )
    return resolved


def _normalize_request_paths(
    request: DatasetRootRequest,
) -> Tuple[Tuple[Path, ...], Path, Tuple[Path, ...]]:
    raw_roots = tuple(Path(os.path.abspath(os.fspath(root))) for root in request.roots)
    for root in raw_roots:
        if is_within_reserved_internal_directory(root):
            raise DatasetDiscoveryError(
                "reserved_internal_path",
                "dataset input root must not use a dupeGuru Neo internal path",
                path=root,
            )
    roots = tuple(
        sorted(
            {_canonical_directory(root, "input_root") for root in raw_roots},
            key=_path_key,
        )
    )
    for root in roots:
        if is_within_reserved_internal_directory(root):
            raise DatasetDiscoveryError(
                "reserved_internal_path",
                "dataset input root must not use a dupeGuru Neo internal path",
                path=root,
            )
    for index, root in enumerate(roots):
        if any(_is_within(root, other) for other in roots[:index] + roots[index + 1 :]):
            raise DatasetDiscoveryError(
                "overlapping_input_roots",
                "dataset input roots must not overlap",
                path=root,
            )

    raw_destination = Path(os.path.abspath(os.fspath(request.destination_root)))
    if is_within_reserved_internal_directory(raw_destination):
        raise DatasetDiscoveryError(
            "reserved_internal_path",
            "destination root must not use a dupeGuru Neo internal path",
            path=raw_destination,
        )
    destination = _canonical_directory(raw_destination, "destination_root")
    if is_within_reserved_internal_directory(destination):
        raise DatasetDiscoveryError(
            "reserved_internal_path",
            "destination root must not use a dupeGuru Neo internal path",
            path=destination,
        )
    if any(_is_within(destination, root) or _is_within(root, destination) for root in roots):
        raise DatasetDiscoveryError(
            "destination_overlap",
            "destination root must be physically separate from every input root",
            path=destination,
        )

    raw_protected = tuple(Path(os.path.abspath(os.fspath(root))) for root in request.protected_roots)
    for root in raw_protected:
        if is_within_reserved_internal_directory(root):
            raise DatasetDiscoveryError(
                "reserved_internal_path",
                "protected root must not use a dupeGuru Neo internal path",
                path=root,
            )
    protected = tuple(
        sorted(
            {_canonical_directory(root, "protected_root") for root in raw_protected},
            key=_path_key,
        )
    )
    for root in protected:
        if is_within_reserved_internal_directory(root):
            raise DatasetDiscoveryError(
                "reserved_internal_path",
                "protected root must not use a dupeGuru Neo internal path",
                path=root,
            )
        if not any(_is_within(root, input_root) for input_root in roots):
            raise DatasetDiscoveryError(
                "protected_root_escape",
                "protected root must be contained by an input root",
                path=root,
            )

    if request.visual_cache is not None:
        cache = Path(os.path.abspath(os.fspath(request.visual_cache)))
        if any(_is_within(cache, root) for root in roots):
            raise DatasetDiscoveryError(
                "visual_cache_overlap",
                "visual cache must be outside every read-only input root",
                path=cache,
            )
        parent = _canonical_directory(cache.parent, "visual_cache_parent")
        if os.path.lexists(cache):
            try:
                cache_stat = os.stat(cache, follow_symlinks=False)
            except OSError as error:
                raise DatasetDiscoveryError(
                    "visual_cache_unavailable",
                    str(error),
                    path=cache,
                ) from error
            if stat.S_ISLNK(cache_stat.st_mode) or is_reparse_point(cache_stat) or not stat.S_ISREG(cache_stat.st_mode):
                raise DatasetDiscoveryError(
                    "unsafe_visual_cache",
                    "visual cache must be a plain regular file",
                    path=cache,
                )
        if not _is_within(cache, parent):
            raise DatasetDiscoveryError(
                "unsafe_visual_cache",
                "visual cache path escaped its physical parent",
                path=cache,
            )
    if request.state_root is not None:
        state_root = Path(os.path.abspath(os.fspath(request.state_root)))
        if is_within_reserved_internal_directory(state_root):
            raise DatasetDiscoveryError(
                "reserved_internal_path",
                "executor state base must be outside every dupeGuru Neo internal path",
                path=state_root,
            )
        if os.path.lexists(state_root):
            physical_state_root = _canonical_directory(state_root, "state_root")
        else:
            physical_parent = _canonical_directory(state_root.parent, "state_root_parent")
            physical_state_root = physical_parent.joinpath(state_root.name)
        if any(_is_within(state_root, root) or _is_within(physical_state_root, root) for root in roots):
            raise DatasetDiscoveryError(
                "state_root_overlap",
                "executor state root must be outside every read-only input root",
                path=state_root,
            )
    return roots, destination, protected


def _exact_group_is_proven(group) -> bool:
    if getattr(group, "verification", None) != VERIFIED_EXACT:
        return False
    method = getattr(group, "verification_method", "")
    if not isinstance(method, str) or not method.endswith(EXACT_VERIFICATION_SUFFIX):
        return False
    files = tuple(getattr(group, "files", ()))
    if len(files) < 2:
        return False
    first = files[0]
    digest = getattr(first, "digest", "")
    if (
        getattr(first, "digest_algorithm", None) != DELETION_PROOF_ALGORITHM
        or not isinstance(digest, str)
        or len(digest) != 64
        or digest != digest.lower()
    ):
        return False
    try:
        bytes.fromhex(digest)
    except ValueError:
        return False
    return all(
        getattr(item, "digest_algorithm", None) == DELETION_PROOF_ALGORITHM
        and getattr(item, "digest", None) == digest
        and getattr(item, "size", None) == getattr(first, "size", None)
        for item in files[1:]
    )


def unproven_exact_group_ids(exact_groups: Iterable[object]) -> Tuple[str, ...]:
    """Return group IDs that claim exactness without the required public proof contract."""

    return tuple(
        sorted(
            str(getattr(group, "group_id", "<unknown>"))
            for group in exact_groups
            if getattr(group, "verification", None) == VERIFIED_EXACT and not _exact_group_is_proven(group)
        )
    )


def _relation_value(evidence) -> str:
    relation = getattr(evidence, "relation", None)
    return relation.value if hasattr(relation, "value") else str(relation)


def build_dataset_clusters(
    visual_assets: Iterable[object],
    artifacts: Iterable[object],
    visual_evidence: Iterable[object],
    exact_groups: Iterable[object],
) -> Tuple[DatasetCluster, ...]:
    """Build conservative connected-component clusters from independent reports.

    Visual edges that are wholly contained in one proven exact equivalence class
    are redundant.  Every other visual edge makes the whole connected component
    non-exact, including a component containing an otherwise exact subset.
    """

    assets = tuple(visual_assets)
    asset_ids = tuple(str(asset.asset_id) for asset in assets)
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("visual assets must have unique IDs")
    asset_id_set = set(asset_ids)
    by_path = {_path_key(asset.path): str(asset.asset_id) for asset in assets}
    if len(by_path) != len(assets):
        raise ValueError("visual assets must have unique paths")

    dimensions = {}
    for artifact in artifacts:
        asset_id = str(artifact.asset_id)
        if asset_id not in asset_id_set:
            raise ValueError("visual artifact references an unknown asset")
        if asset_id in dimensions:
            raise ValueError("visual artifacts must have unique asset IDs")
        dimensions[asset_id] = tuple(int(value) for value in artifact.dimensions)

    exact = _DisjointSet(asset_ids)
    exact_edges = []
    for group in exact_groups:
        if not _exact_group_is_proven(group):
            continue
        members = tuple(
            sorted({by_path[key] for item in group.files for key in (_path_key(item.path),) if key in by_path})
        )
        if len(members) < 2:
            continue
        reference = members[0]
        for member in members[1:]:
            exact.union(reference, member)
            exact_edges.append((reference, member))

    evidence = tuple(visual_evidence)
    supported_visual_relations = {
        VisualRelation.SIMILAR.value,
        VisualRelation.TRANSFORMED.value,
        VisualRelation.CROP_CANDIDATE.value,
        VisualRelation.RELATED.value,
    }
    overall = _DisjointSet(asset_ids)
    for first, second in exact_edges:
        overall.union(first, second)
    for item in evidence:
        first = str(item.first_id)
        second = str(item.second_id)
        if first not in asset_id_set or second not in asset_id_set:
            raise ValueError("visual evidence references an unknown asset")
        relation = _relation_value(item)
        if relation not in supported_visual_relations:
            raise ValueError("visual evidence has an unsupported relation")
        overall.union(first, second)

    # Bucket edges once after all unions are complete.  Re-scanning every edge
    # for every connected component is O(components * evidence) for disjoint
    # libraries and becomes unusable at dataset scale.
    evidence_by_root = {}
    for item in evidence:
        root = overall.find(str(item.first_id))
        evidence_by_root.setdefault(root, []).append(item)

    clusters = []
    for members in overall.components():
        if len(members) < 2:
            continue
        component_evidence = evidence_by_root.get(overall.find(members[0]), ())
        non_exact_evidence = tuple(
            item for item in component_evidence if exact.find(str(item.first_id)) != exact.find(str(item.second_id))
        )
        exact_roots = {exact.find(member) for member in members}
        if not non_exact_evidence and len(exact_roots) == 1:
            relation = DatasetRelation.VERIFIED_EXACT
        elif any(_relation_value(item) == VisualRelation.RELATED.value for item in non_exact_evidence):
            relation = DatasetRelation.RELATED
        elif any(
            _relation_value(item)
            in {
                VisualRelation.TRANSFORMED.value,
                VisualRelation.CROP_CANDIDATE.value,
            }
            or int(getattr(item, "phash_orientation", 0)) != 0
            or int(getattr(item, "block_orientation", 0)) != 0
            or (
                dimensions.get(str(item.first_id)) is not None
                and dimensions.get(str(item.second_id)) is not None
                and dimensions[str(item.first_id)] != dimensions[str(item.second_id)]
            )
            for item in non_exact_evidence
        ):
            relation = DatasetRelation.TRANSFORMED
        else:
            relation = DatasetRelation.NEAR_DUPLICATE
        clusters.append(
            DatasetCluster(
                members=members,
                relation=relation,
                evidence_complete=True,
                evidence_version=DISCOVERY_EVIDENCE_VERSION,
            )
        )
    return tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id))


def _bit_depth(mode: str, info: Mapping[str, object]) -> float:
    bits = info.get("bits")
    if isinstance(bits, int) and not isinstance(bits, bool) and bits > 0:
        return float(bits)
    if mode == "1":
        return 1.0
    if mode.startswith("I;16"):
        return 16.0
    if mode in {"I", "F"}:
        return 32.0
    if mode in {"L", "LA", "P", "PA", "RGB", "RGBA", "CMYK", "YCbCr", "HSV", "LAB"}:
        return 8.0
    return 0.0


def _quality_asset(snapshot, artifact, protected_roots: Sequence[Path]) -> DatasetAsset:
    path = Path(snapshot.path)
    expected_generation = _snapshot_generation(snapshot)
    file_system = platform_file_system()
    try:
        path_before = os.stat(path, follow_symlinks=False)
        path_identity = get_file_identity(
            path,
            follow_symlinks=False,
            stat_result=path_before,
        )
        if same_physical_file(snapshot.identity, path_identity).verdict is not IdentityVerdict.SAME:
            raise DatasetDiscoveryError(
                "source_identity_changed",
                "image identity changed after visual analysis",
                path=path,
                state=PreparationState.SOURCE_CHANGED,
            )
        if _path_generation(path, path_before, path_identity) != expected_generation:
            raise DatasetDiscoveryError(
                "source_generation_changed",
                "image changed after visual analysis",
                path=path,
                state=PreparationState.SOURCE_CHANGED,
            )
        with file_system.open_readonly(path) as handle:
            before = os.fstat(handle.fileno())
            before_generation = _handle_generation(
                handle,
                path,
                before,
                snapshot.identity,
            )
            if before_generation != expected_generation:
                raise DatasetDiscoveryError(
                    "source_generation_changed",
                    "opened image generation differs from its visual snapshot",
                    path=path,
                    state=PreparationState.SOURCE_CHANGED,
                )
            with Image.open(handle) as image:
                dimensions = tuple(int(value) for value in image.size)
                if dimensions != tuple(int(value) for value in artifact.dimensions):
                    raise DatasetDiscoveryError(
                        "visual_artifact_mismatch",
                        "image dimensions no longer match visual evidence",
                        path=path,
                        state=PreparationState.SOURCE_CHANGED,
                    )
                exif_count = len(image.getexif())
                info = dict(image.info)
                depth = _bit_depth(image.mode, info)
                metadata_count = float(exif_count + len(info))
            after = os.fstat(handle.fileno())
            if (
                _handle_generation(
                    handle,
                    path,
                    after,
                    snapshot.identity,
                )
                != before_generation
            ):
                raise DatasetDiscoveryError(
                    "source_generation_changed",
                    "image changed while quality metadata was read",
                    path=path,
                    state=PreparationState.SOURCE_CHANGED,
                )
        final_stat = os.stat(path, follow_symlinks=False)
        final_identity = get_file_identity(path, follow_symlinks=False, stat_result=final_stat)
        if (
            _path_generation(path, final_stat, final_identity) != expected_generation
            or same_physical_file(snapshot.identity, final_identity).verdict is not IdentityVerdict.SAME
        ):
            raise DatasetDiscoveryError(
                "source_generation_changed",
                "image changed after quality metadata was read",
                path=path,
                state=PreparationState.SOURCE_CHANGED,
            )
    except DatasetDiscoveryError:
        raise
    except (FileIdentityError, OSError, ValueError) as error:
        raise DatasetDiscoveryError(
            "quality_metadata_failed",
            str(error),
            path=path,
            state=PreparationState.INCOMPLETE_EVIDENCE,
        ) from error
    immutable = any(_is_within(path, root) for root in protected_roots)
    return DatasetAsset(
        asset_id=str(snapshot.asset_id),
        path=str(path),
        dimensions=tuple(int(value) for value in artifact.dimensions),
        bit_depth=depth,
        metadata_count=metadata_count,
        jpeg_artifact_score=0,
        protected=immutable,
        immutable=immutable,
    )


def _incomplete(
    state: PreparationState,
    issues: Iterable[DatasetIssue],
) -> DatasetPreparation:
    issue_tuple = tuple(issues)
    if not issue_tuple:
        raise ValueError("incomplete discovery requires an issue")
    return DatasetPreparation(state, None, issue_tuple)


def _enforce_protected_quarantine_boundary(
    result: DatasetPreparation,
    protected_roots: Sequence[Path],
) -> DatasetPreparation:
    if not result.complete or not protected_roots:
        return result
    assert result.plan is not None
    unsafe_paths = tuple(
        file_action.source.path
        for action in result.plan.actions
        if action.operation.value == "quarantine_bundle"
        for file_action in action.files
        if any(_is_within(file_action.source.path, root) for root in protected_roots)
    )
    if not unsafe_paths:
        return result
    return _incomplete(
        PreparationState.CONFLICT,
        (
            DatasetIssue(
                "protected_quarantine_target",
                "a protected library file cannot be a quarantine target",
                unsafe_paths,
            ),
        ),
    )


def _visual_incomplete(report) -> DatasetPreparation:
    issues = tuple(
        DatasetIssue(
            "visual_{}".format(issue.code),
            issue.message,
            (issue.path,) if issue.path else (),
        )
        for issue in report.scan_receipt.issues
    )
    if not issues:
        issues = (
            DatasetIssue(
                "visual_scan_incomplete",
                "visual scan did not provide complete filesystem and decode coverage",
                tuple(report.roots),
            ),
        )
    return _incomplete(PreparationState.INCOMPLETE_COVERAGE, issues)


def _exact_incomplete(report) -> DatasetPreparation:
    issues = tuple(
        DatasetIssue(
            "exact_{}".format(issue.code.replace("-", "_")),
            issue.message,
            (issue.path,) if issue.path else (),
        )
        for issue in report.issues
    )
    if not issues:
        incomplete_roots = tuple(item.root for item in report.coverage if not item.complete)
        issues = (
            DatasetIssue(
                "exact_scan_incomplete",
                "exact scan did not provide complete filesystem and byte-verification coverage",
                incomplete_roots or tuple(report.roots),
            ),
        )
    return _incomplete(PreparationState.INCOMPLETE_COVERAGE, issues)


class DatasetDiscoveryService:
    """Compose visual, exact, quality, sidecar, split, and plan services."""

    def __init__(
        self,
        *,
        visual_service=None,
        exact_service=None,
        dataset_service: Optional[DatasetModeService] = None,
    ) -> None:
        self.visual_service = visual_service
        self.exact_service = exact_service or ScanService()
        self.dataset_service = dataset_service or DatasetModeService()

    def prepare(self, request: DatasetRootRequest) -> DatasetPreparation:
        if not isinstance(request, DatasetRootRequest):
            raise TypeError("dataset discovery requires a DatasetRootRequest")
        try:
            roots, destination, protected = _normalize_request_paths(request)
        except DatasetDiscoveryError as error:
            return _incomplete(error.state, (error.issue(),))

        visual_service = self.visual_service or VisualService(cache_path=request.visual_cache)
        visual_report = visual_service.scan_roots(
            tuple(str(root) for root in roots),
            config=VisualScanConfig(
                similarity_threshold=request.similarity_threshold,
                phash_radius=request.phash_radius,
                match_scaled=request.match_scaled,
                match_rotated=request.match_rotated,
                include_related=True,
                dry_run=True,
            ),
        )
        if not visual_report.scan_receipt.complete:
            return _visual_incomplete(visual_report)
        if not visual_report.assets:
            return _incomplete(
                PreparationState.INCOMPLETE_EVIDENCE,
                (
                    DatasetIssue(
                        "no_visual_assets",
                        "no supported images were discovered",
                        tuple(str(root) for root in roots),
                    ),
                ),
            )

        exact_report = self.exact_service.scan(ScanRequest(roots=tuple(str(root) for root in roots)))
        if not exact_report.summary.complete:
            return _exact_incomplete(exact_report)
        unsafe_groups = unproven_exact_group_ids(exact_report.groups)
        if unsafe_groups:
            return _incomplete(
                PreparationState.INCOMPLETE_EVIDENCE,
                (
                    DatasetIssue(
                        "unproven_exact_groups",
                        "exact scan returned groups without SHA-256 plus streaming byte-comparison proof",
                        asset_ids=unsafe_groups,
                    ),
                ),
            )

        artifacts = {str(artifact.asset_id): artifact for artifact in visual_report.artifacts}
        if len(artifacts) != len(visual_report.assets) or any(
            str(asset.asset_id) not in artifacts for asset in visual_report.assets
        ):
            return _incomplete(
                PreparationState.INCOMPLETE_EVIDENCE,
                (
                    DatasetIssue(
                        "visual_artifacts_incomplete",
                        "not every visual asset has one stable feature artifact",
                        tuple(asset.path for asset in visual_report.assets),
                    ),
                ),
            )
        try:
            assets = tuple(
                _quality_asset(asset, artifacts[str(asset.asset_id)], protected) for asset in visual_report.assets
            )
            clusters = build_dataset_clusters(
                visual_report.assets,
                visual_report.artifacts,
                visual_report.evidence,
                exact_report.groups,
            )
        except DatasetDiscoveryError as error:
            return _incomplete(error.state, (error.issue(),))
        except (TypeError, ValueError) as error:
            return _incomplete(
                PreparationState.INCOMPLETE_EVIDENCE,
                (
                    DatasetIssue(
                        "discovery_evidence_invalid",
                        str(error),
                    ),
                ),
            )

        result = self.dataset_service.prepare(
            assets,
            clusters,
            allowed_roots=tuple(str(root) for root in roots),
            destination_root=destination,
            sidecar_paths=None,
            split_weights=dict(request.split_weights),
            split_seed=request.split_seed,
            dry_run=request.dry_run,
        )
        return _enforce_protected_quarantine_boundary(result, protected)


__all__ = [
    "DEFAULT_SPLIT_WEIGHTS",
    "DISCOVERY_EVIDENCE_VERSION",
    "DatasetDiscoveryError",
    "DatasetDiscoveryService",
    "DatasetRootRequest",
    "build_dataset_clusters",
    "unproven_exact_group_ids",
]
