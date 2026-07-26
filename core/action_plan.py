# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Build immutable service deletion plans from live GUI exact results.

This module is intentionally read-only.  It binds the live verified-exact scan
evidence to fresh stable-handle SHA-256 and byte-comparison proofs, but it does
not create quarantine directories or change files.
``core.quarantine.QuarantineManager`` still rebuilds those proofs immediately
before any filesystem transition.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

from core import fs as core_fs
from core.destructive_eligibility import evaluate_batch
from core.engine import VerificationKind
from core.file_identity import (
    FileIdentityError,
    IdentityVerdict,
    get_file_identity,
    get_file_identity_from_fd,
    identity_record_parts,
    same_physical_file,
)
from core.safe_action import (
    HASH_ALGORITHM as SAFE_PROOF_HASH_ALGORITHM,
    platform_file_system,
)
from core.services.models import (
    VERIFIED_EXACT,
    DeletionPlan,
    FileRecord,
    PlanAction,
    action_id_for,
    plan_id_for,
    utc_now,
)


class ActionPlanError(ValueError):
    """Raised when GUI state cannot be represented as a safe action plan."""


@dataclass(frozen=True)
class BoundDeletionPlan:
    """A service plan plus its in-memory GUI object bindings."""

    plan: DeletionPlan
    action_dupes: Tuple[Tuple[str, object], ...]

    def dupe_for_action(self, action_id: str):
        for candidate_id, dupe in self.action_dupes:
            if candidate_id == action_id:
                return dupe
        return None


def _absolute_text(path) -> str:
    return os.path.abspath(os.fspath(path))


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _validate_regular(file_stat: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ActionPlanError("Only plain regular files can be placed in an action plan: '{}'".format(path))
    if not int(file_stat.st_dev) or not int(file_stat.st_ino):
        raise ActionPlanError("A stable file identity is unavailable for '{}'".format(path))


def _validate_stable_binding(
    path: Path,
    path_before: core_fs.FileSnapshot,
    opened_before: core_fs.FileSnapshot,
    opened_after: core_fs.FileSnapshot,
    path_after: core_fs.FileSnapshot,
) -> None:
    snapshots = (path_before, opened_before, opened_after, path_after)
    if any(candidate != snapshots[0] for candidate in snapshots[1:]):
        raise ActionPlanError("File changed while its deletion proof was being built: '{}'".format(path))


def _record(
    path: Path,
    file_stat: os.stat_result,
    digest: str,
    identity,
) -> FileRecord:
    volume_id, file_id = identity_record_parts(identity)
    return FileRecord(
        path=str(path),
        size=int(file_stat.st_size),
        mtime_ns=int(file_stat.st_mtime_ns),
        digest_algorithm=SAFE_PROOF_HASH_ALGORITHM,
        digest=digest,
        volume_id=volume_id,
        file_id=file_id,
    )


def _verified_pair_records(target, reference, evidence, file_system) -> Tuple[FileRecord, FileRecord]:
    """Build two SHA-256 records while proving the pair and scan evidence."""

    target_path = Path(_absolute_text(target.path))
    reference_path = Path(_absolute_text(reference.path))
    digest = evidence.digest
    if not isinstance(digest, bytes) or not digest:
        raise ActionPlanError("Exact evidence has no binary full digest")
    algorithm = str(evidence.algorithm)
    if not algorithm:
        raise ActionPlanError("Exact evidence has no digest algorithm")
    supported_algorithms = {core_fs.HASH_ALGORITHM, SAFE_PROOF_HASH_ALGORITHM}
    if algorithm not in supported_algorithms:
        raise ActionPlanError(
            "Exact evidence algorithm '{}' is not one of the supported live-proof algorithms: {}".format(
                algorithm,
                ", ".join(sorted(supported_algorithms)),
            )
        )
    try:
        expected_size = int(evidence.size)
    except (TypeError, ValueError) as error:
        raise ActionPlanError("Exact evidence has an invalid file size") from error
    if expected_size < 0:
        raise ActionPlanError("Exact evidence has an invalid file size")

    try:
        target_path_before = file_system.lstat(target_path)
        reference_path_before = file_system.lstat(reference_path)
        _validate_regular(target_path_before, target_path)
        _validate_regular(reference_path_before, reference_path)
        target_path_before_snapshot = core_fs.FileSnapshot.from_path(
            target_path,
            target_path_before,
        )
        reference_path_before_snapshot = core_fs.FileSnapshot.from_path(
            reference_path,
            reference_path_before,
        )

        with contextlib.ExitStack() as stack:
            target_handle = stack.enter_context(file_system.open_readonly(target_path))
            reference_handle = stack.enter_context(file_system.open_readonly(reference_path))
            target_before = os.fstat(target_handle.fileno())
            reference_before = os.fstat(reference_handle.fileno())
            target_physical_identity = get_file_identity_from_fd(
                target_handle.fileno(),
                path=target_path,
                stat_result=target_before,
            )
            reference_physical_identity = get_file_identity_from_fd(
                reference_handle.fileno(),
                path=reference_path,
                stat_result=reference_before,
            )
            target_before_snapshot = core_fs.FileSnapshot.from_file(
                target_handle,
                path=target_path,
                stat_result=target_before,
            )
            reference_before_snapshot = core_fs.FileSnapshot.from_file(
                reference_handle,
                path=reference_path,
                stat_result=reference_before,
            )
            _validate_regular(target_before, target_path)
            _validate_regular(reference_before, reference_path)
            if (
                same_physical_file(
                    target_physical_identity,
                    reference_physical_identity,
                ).verdict
                is IdentityVerdict.SAME
            ):
                raise ActionPlanError("Target and reference resolve to the same physical file")
            if int(target_before.st_size) != expected_size:
                raise ActionPlanError("File size changed after exact verification: '{}'".format(target_path))
            if int(reference_before.st_size) != expected_size:
                raise ActionPlanError("File size changed after exact verification: '{}'".format(reference_path))

            target_sha256 = hashlib.sha256()
            reference_sha256 = hashlib.sha256()
            target_exact = core_fs.hasher()
            reference_exact = core_fs.hasher()
            bytes_compared = 0
            while True:
                target_chunk = target_handle.read(core_fs.CHUNK_SIZE)
                reference_chunk = reference_handle.read(core_fs.CHUNK_SIZE)
                if target_chunk != reference_chunk:
                    raise ActionPlanError(
                        "Files are no longer byte-identical: '{}' and '{}'".format(target_path, reference_path)
                    )
                if not target_chunk:
                    break
                bytes_compared += len(target_chunk)
                target_sha256.update(target_chunk)
                reference_sha256.update(reference_chunk)
                target_exact.update(target_chunk)
                reference_exact.update(reference_chunk)

            target_after = os.fstat(target_handle.fileno())
            reference_after = os.fstat(reference_handle.fileno())
            target_after_snapshot = core_fs.FileSnapshot.from_file(
                target_handle,
                path=target_path,
                stat_result=target_after,
            )
            reference_after_snapshot = core_fs.FileSnapshot.from_file(
                reference_handle,
                path=reference_path,
                stat_result=reference_after,
            )

        target_path_after = file_system.lstat(target_path)
        reference_path_after = file_system.lstat(reference_path)
        _validate_regular(target_path_after, target_path)
        _validate_regular(reference_path_after, reference_path)
        target_path_after_snapshot = core_fs.FileSnapshot.from_path(
            target_path,
            target_path_after,
        )
        reference_path_after_snapshot = core_fs.FileSnapshot.from_path(
            reference_path,
            reference_path_after,
        )
        _validate_stable_binding(
            target_path,
            target_path_before_snapshot,
            target_before_snapshot,
            target_after_snapshot,
            target_path_after_snapshot,
        )
        _validate_stable_binding(
            reference_path,
            reference_path_before_snapshot,
            reference_before_snapshot,
            reference_after_snapshot,
            reference_path_after_snapshot,
        )
        for path, file_stat, opened_identity in (
            (target_path, target_path_after, target_physical_identity),
            (
                reference_path,
                reference_path_after,
                reference_physical_identity,
            ),
        ):
            current_identity = get_file_identity(
                path,
                follow_symlinks=False,
                stat_result=file_stat,
            )
            if same_physical_file(opened_identity, current_identity).verdict is not IdentityVerdict.SAME:
                raise ActionPlanError(
                    "File identity changed while its deletion proof was being built: '{}'".format(path)
                )
    except ActionPlanError:
        raise
    except (OSError, FileIdentityError) as error:
        raise ActionPlanError(
            "Could not build stable deletion proof for '{}' and '{}': {}".format(target_path, reference_path, error)
        ) from error

    if bytes_compared != expected_size:
        raise ActionPlanError("File size changed while exact bytes were being verified: '{}'".format(target_path))
    if algorithm == SAFE_PROOF_HASH_ALGORITHM:
        target_evidence_digest = target_sha256.digest()
        reference_evidence_digest = reference_sha256.digest()
    else:
        target_evidence_digest = target_exact.digest()
        reference_evidence_digest = reference_exact.digest()
    if target_evidence_digest != digest or reference_evidence_digest != digest:
        raise ActionPlanError("A file's full digest no longer matches its verified-exact scan evidence")
    target_sha256_hex = target_sha256.hexdigest()
    reference_sha256_hex = reference_sha256.hexdigest()
    if target_sha256_hex != reference_sha256_hex:
        raise ActionPlanError("Files no longer have the same SHA-256 deletion proof")
    return (
        _record(
            target_path,
            target_after,
            target_sha256_hex,
            target_physical_identity,
        ),
        _record(
            reference_path,
            reference_after,
            reference_sha256_hex,
            reference_physical_identity,
        ),
    )


def _remember_record(records: Dict[int, FileRecord], file, current: FileRecord) -> FileRecord:
    previous = records.get(id(file))
    if previous is not None and previous != current:
        raise ActionPlanError("File changed while the deletion plan was being assembled: '{}'".format(current.path))
    records[id(file)] = current
    return current


def _group_id(scan_id: str, group) -> str:
    evidence = group.evidence
    canonical = json.dumps(
        {
            "scan_id": scan_id,
            "algorithm": str(evidence.algorithm),
            "digest": evidence.digest.hex(),
            "size": int(evidence.size),
            "paths": sorted(os.path.normcase(_absolute_text(file.path)) for file in group),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_bound_deletion_plan(
    results,
    marked: Iterable[object],
    roots: Sequence[os.PathLike],
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> BoundDeletionPlan:
    """Create a deterministic, proof-labelled plan without changing the disk."""

    marked = tuple(marked)
    eligibility = evaluate_batch(results, marked, current_pool_resolver)
    if not eligibility.ok:
        messages = sorted({item.message for _, item in eligibility.blocked})
        if not marked:
            messages.append("No marked files were supplied.")
        raise ActionPlanError(" ".join(messages))
    receipt = results.scan_receipt
    root_texts = tuple(
        sorted(
            {_absolute_text(root) for root in roots},
            key=lambda value: os.path.normcase(value),
        )
    )
    if not root_texts:
        raise ActionPlanError("At least one selected scan root is required")

    operation = "quarantine"
    actions = []
    bindings: Dict[str, object] = {}
    records: Dict[int, FileRecord] = {}
    group_ids: Dict[int, str] = {}
    file_system = platform_file_system()
    for dupe in eligibility.allowed:
        group = results.get_group_of_duplicate(dupe)
        evidence = getattr(group, "evidence", None)
        if (
            getattr(group, "verification_kind", VerificationKind.UNVERIFIED) is not VerificationKind.VERIFIED_EXACT
            or evidence is None
        ):
            raise ActionPlanError("A marked group lost its verified-exact evidence")
        group_identity = id(group)
        group_id = group_ids.get(group_identity)
        if group_id is None:
            group_id = _group_id(receipt.scan_id, group)
            group_ids[group_identity] = group_id
        current_target, current_reference = _verified_pair_records(
            dupe,
            group.ref,
            evidence,
            file_system,
        )
        current_eligibility = evaluate_batch(
            results,
            (dupe,),
            current_pool_resolver,
        )
        if not current_eligibility.ok:
            messages = sorted({item.message for _, item in current_eligibility.blocked})
            raise ActionPlanError(" ".join(messages))
        target = _remember_record(records, dupe, current_target)
        reference = _remember_record(records, group.ref, current_reference)
        action_id = action_id_for(group_id, target.path, operation)
        actions.append(
            PlanAction(
                action_id=action_id,
                group_id=group_id,
                operation=operation,
                target=target,
                reference=reference,
                verification=VERIFIED_EXACT,
            )
        )
        bindings[action_id] = dupe

    final_eligibility = evaluate_batch(
        results,
        eligibility.allowed,
        current_pool_resolver,
    )
    if not final_eligibility.ok:
        messages = sorted({item.message for _, item in final_eligibility.blocked})
        raise ActionPlanError(" ".join(messages))

    actions.sort(key=lambda action: (action.group_id, os.path.normcase(action.target.path), action.action_id))
    plan_id = plan_id_for(receipt.scan_id, root_texts, actions)
    plan = DeletionPlan(
        plan_id=plan_id,
        created_at=utc_now(),
        source_scan_id=receipt.scan_id,
        roots=root_texts,
        actions=tuple(actions),
    )
    action_dupes = tuple((action.action_id, bindings[action.action_id]) for action in actions)
    return BoundDeletionPlan(plan=plan, action_dupes=action_dupes)


__all__ = [
    "ActionPlanError",
    "BoundDeletionPlan",
    "build_bound_deletion_plan",
]
