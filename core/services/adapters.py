from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Mapping, Optional, Tuple

from core import __version__, engine
from core import fs as core_fs
from core.file_identity import (
    IdentityVerdict,
    identity_record_parts,
    same_physical_file,
)
from core.file_generation import FileGenerationError, get_file_generation_token
from core.keeper import choose_keeper
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_reserved_internal_file,
    is_within_reserved_internal_directory,
)
from core.quarantine import PreparationBatch, QuarantineManager
from core.safe_action import HASH_ALGORITHM as SAFE_PROOF_HASH_ALGORITHM
from core.safe_action import FailureCode, platform_file_system
from core.safe_walk import WalkEventKind, walk_no_follow
from core.services.models import (
    DOCTOR_REPORT_SCHEMA,
    SCHEMA_VERSION,
    VERIFIED_EXACT,
    ActionResult,
    DeletionPlan,
    FileRecord,
    ScanCoverage,
    ScanGroup,
    ScanIssue,
    ScanReport,
    ScanRequest,
    ScanSummary,
    utc_now,
)

ProgressCallback = Callable[[str, Mapping[str, Any]], None]


def null_progress(stage: str, fields: Mapping[str, Any]) -> None:
    pass


class ScanAdapter(ABC):
    """Boundary between the service layer and the verified duplicate engine."""

    @abstractmethod
    def scan(self, request: ScanRequest, progress: ProgressCallback = null_progress) -> ScanReport:
        raise NotImplementedError()


@dataclass(frozen=True)
class ApplyPreparation:
    batch: PreparationBatch
    results: Tuple[ActionResult, ...]


class ApplyAdapter(ABC):
    """Batch boundary for live proof construction and safe filesystem actions."""

    @property
    @abstractmethod
    def supports_execute(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def preflight(self, plan: DeletionPlan, persist: bool) -> ApplyPreparation:
        raise NotImplementedError()

    @abstractmethod
    def execute(self, preparation: ApplyPreparation) -> Tuple[ActionResult, ...]:
        raise NotImplementedError()


class QueryAdapter(ABC):
    """Boundary that can later be implemented by the persistent catalog."""

    @abstractmethod
    def query(
        self,
        report: ScanReport,
        group_id: Optional[str] = None,
        path: Optional[str] = None,
        digest: Optional[str] = None,
    ) -> Tuple[ScanGroup, ...]:
        raise NotImplementedError()


class DoctorAdapter(ABC):
    """Boundary for platform and backend capability diagnostics."""

    @abstractmethod
    def inspect(self) -> Dict[str, Any]:
        raise NotImplementedError()


@dataclass(frozen=True)
class _Candidate:
    file: core_fs.File
    identity: Any
    device: int
    inode: int
    size: int
    mtime_ns: int
    generation_token: bytes

    @property
    def path(self) -> Path:
        return Path(self.file.path)


class _ScanRunState:
    def __init__(self, request: ScanRequest):
        self.request = request
        self.deadline = time.monotonic() + float(request.max_seconds)
        self.issues: List[ScanIssue] = []
        self.stopped = False
        self.discovery_incomplete = False

    def add_issue(self, issue: ScanIssue, *, during_discovery: bool = False) -> bool:
        if self.stopped:
            return False
        if len(self.issues) >= self.request.max_issues:
            self.reach_limit(
                "issues",
                "exact scan exceeded max_issues ({})".format(self.request.max_issues),
                issue.path,
                during_discovery=during_discovery,
            )
            return False
        self.issues.append(issue)
        return True

    def reach_limit(
        self,
        name: str,
        message: str,
        path: os.PathLike | str = "",
        *,
        during_discovery: bool = False,
    ) -> None:
        if self.stopped:
            return
        limit_issue = ScanIssue(
            path=str(path),
            code="resource-limit-{}".format(name),
            message=message,
        )
        if len(self.issues) < self.request.max_issues:
            self.issues.append(limit_issue)
        else:
            self.issues[-1] = limit_issue
        self.stopped = True
        self.discovery_incomplete = during_discovery

    def time_exceeded(
        self,
        path: os.PathLike | str = "",
        *,
        during_discovery: bool = False,
    ) -> bool:
        if self.stopped:
            return True
        if time.monotonic() >= self.deadline:
            self.reach_limit(
                "seconds",
                "exact scan exceeded max_seconds ({})".format(self.request.max_seconds),
                path,
                during_discovery=during_discovery,
            )
        return self.stopped


class CoreVerifiedScanAdapter(ScanAdapter):
    """Qt-free adapter over the production verified exact engine."""

    _COVERAGE_REDUCING_EVENTS = {
        WalkEventKind.SYMLINK_SKIPPED,
        WalkEventKind.REPARSE_POINT_SKIPPED,
        WalkEventKind.MOUNT_SKIPPED,
        WalkEventKind.CYCLE_SKIPPED,
        WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
        WalkEventKind.SPECIAL_FILE_SKIPPED,
        WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
        WalkEventKind.ERROR,
    }

    def __init__(self, fs=None):
        self.fs = fs or platform_file_system()

    def scan(self, request: ScanRequest, progress: ProgressCallback = null_progress) -> ScanReport:
        state = _ScanRunState(request)
        coverage_records: List[ScanCoverage] = []
        candidates: List[_Candidate] = []
        valid_roots: List[str] = []
        seen_identities = set()

        progress("discovering", {"roots": len(request.roots), "files": 0})
        for raw_root in request.roots:
            root = Path(os.path.abspath(os.path.expanduser(raw_root)))
            if state.time_exceeded(root, during_discovery=True):
                break
            root_was_opened = False
            reserved_root = is_within_reserved_internal_directory(root)
            if reserved_root:
                state.add_issue(
                    ScanIssue(
                        path=str(root),
                        code="reserved-internal-root",
                        message="dupeGuru Neo internal state cannot be used as a scan root",
                    ),
                    during_discovery=True,
                )
                if state.stopped:
                    break

            def directory_pruner(path: Path) -> Optional[str]:
                if reserved_root or is_reserved_internal_directory(path):
                    return "dupeGuru Neo internal state is intentionally excluded"
                if not request.recursive and os.path.normcase(str(path)) != os.path.normcase(str(root)):
                    return "non-recursive scan"
                return None

            for event in walk_no_follow(
                root,
                allowed_root=root,
                cross_mounts=False,
                directory_pruner=directory_pruner,
            ):
                if state.time_exceeded(event.path, during_discovery=True):
                    break
                if event.kind is WalkEventKind.DIRECTORY and event.path == root:
                    root_was_opened = True
                elif event.kind is WalkEventKind.FILE:
                    if is_reserved_internal_file(event.path):
                        continue
                    self._collect_candidate(
                        event,
                        request,
                        candidates,
                        seen_identities,
                        state,
                    )
                    if len(candidates) and len(candidates) % 1000 == 0:
                        progress("discovering", {"roots": len(request.roots), "files": len(candidates)})
                elif event.kind in self._COVERAGE_REDUCING_EVENTS:
                    state.add_issue(
                        self._walk_issue(event),
                        during_discovery=True,
                    )
                elif event.kind is WalkEventKind.COVERAGE and event.coverage is not None:
                    coverage_records.append(self._coverage_record(event.path, event.coverage))
                if state.stopped:
                    break
            if root_was_opened:
                normalized = os.path.normcase(str(root))
                if all(os.path.normcase(item) != normalized for item in valid_roots):
                    valid_roots.append(str(root))
            if state.stopped:
                break

        if state.discovery_incomplete:
            self._add_resource_limited_coverage(request, coverage_records)
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                (),
                (),
                state,
                coverage_records,
                progress,
            )

        by_size: DefaultDict[int, List[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            if state.time_exceeded(candidate.path):
                break
            by_size[candidate.size].append(candidate)
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                (),
                (),
                state,
                coverage_records,
                progress,
            )
        hash_candidates = sorted(
            (item for items in by_size.values() if len(items) > 1 for item in items),
            key=lambda item: os.path.normcase(str(item.path)),
        )
        if state.time_exceeded(valid_roots[0] if valid_roots else ""):
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                (),
                (),
                state,
                coverage_records,
                progress,
            )
        filtered_inputs: List[_Candidate] = []
        full_digest_files = set()
        progress("filtering", {"candidates": len(hash_candidates), "filtered": 0})
        for candidate in hash_candidates:
            if state.time_exceeded(candidate.path):
                break
            try:
                if self._populate_engine_candidate_filters(
                    candidate,
                    request,
                    stop_check=lambda: state.time_exceeded(candidate.path),
                ):
                    full_digest_files.add(candidate.file)
                self._validate_candidate_snapshot(candidate)
            except OSError as error:
                if state.stopped:
                    break
                state.add_issue(self._issue(candidate.path, "hash-failed", error))
                continue
            if state.time_exceeded(candidate.path):
                break
            filtered_inputs.append(candidate)
            if len(filtered_inputs) % 250 == 0 or len(filtered_inputs) == len(hash_candidates):
                progress("filtering", {"candidates": len(hash_candidates), "filtered": len(filtered_inputs)})
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                full_digest_files,
                (),
                state,
                coverage_records,
                progress,
            )

        by_candidate_filter: DefaultDict[Tuple[Any, ...], List[_Candidate]] = defaultdict(list)
        for candidate in filtered_inputs:
            if state.time_exceeded(candidate.path):
                break
            sample_digest = None
            if request.big_file_size and candidate.size > request.big_file_size:
                sample_digest = candidate.file.digest_samples
            by_candidate_filter[(candidate.size, candidate.file.digest_partial, sample_digest)].append(candidate)
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                full_digest_files,
                (),
                state,
                coverage_records,
                progress,
            )
        full_candidates = sorted(
            (item for items in by_candidate_filter.values() if len(items) > 1 for item in items),
            key=lambda item: os.path.normcase(str(item.path)),
        )
        if state.time_exceeded(valid_roots[0] if valid_roots else ""):
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                full_digest_files,
                (),
                state,
                coverage_records,
                progress,
            )
        verified_inputs: List[_Candidate] = []
        progress("hashing", {"candidates": len(full_candidates), "hashed": len(full_digest_files)})
        for candidate in full_candidates:
            if state.time_exceeded(candidate.path):
                break
            try:
                if candidate.file not in full_digest_files:
                    full_digest, snapshot = candidate.file._calc_digest_with_snapshot(
                        stop_check=lambda: state.time_exceeded(candidate.path),
                    )
                    candidate.file.prime_exact_digest(
                        "digest",
                        full_digest,
                        snapshot,
                    )
                    full_digest_files.add(candidate.file)
                self._validate_candidate_snapshot(candidate)
            except OSError as error:
                if state.stopped:
                    break
                state.add_issue(self._issue(candidate.path, "hash-failed", error))
                continue
            if state.time_exceeded(candidate.path):
                break
            verified_inputs.append(candidate)
            if len(verified_inputs) % 250 == 0 or len(verified_inputs) == len(full_candidates):
                progress("hashing", {"candidates": len(full_candidates), "hashed": len(full_digest_files)})
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                full_digest_files,
                (),
                state,
                coverage_records,
                progress,
            )

        candidate_by_file = {candidate.file: candidate for candidate in verified_inputs}
        by_full_digest: DefaultDict[Tuple[int, str, bytes], List[_Candidate]] = defaultdict(list)
        for candidate in verified_inputs:
            if state.time_exceeded(candidate.path):
                break
            digest = candidate.file.digest
            if digest is None:
                state.add_issue(
                    ScanIssue(
                        path=str(candidate.path),
                        code="hash-failed",
                        message="exact scan produced no full digest",
                    )
                )
                continue
            by_full_digest[(candidate.size, candidate.file.digest_algorithm, digest)].append(candidate)
        if state.stopped:
            return self._finish_report(
                request,
                valid_roots,
                candidates,
                full_digest_files,
                (),
                state,
                coverage_records,
                progress,
            )
        digest_buckets = sorted(
            ((key, items) for key, items in by_full_digest.items() if len(items) > 1),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[0][2],
                os.path.normcase(str(item[1][0].path)),
            ),
        )
        groups: List[ScanGroup] = []
        progress("verifying", {"engine_groups": 0, "verified_groups": 0})
        group_id_counts: DefaultDict[str, int] = defaultdict(int)
        engine_groups_seen = 0
        for _digest_key, digest_bucket in digest_buckets:
            if state.time_exceeded(digest_bucket[0].path):
                break
            if len(groups) >= request.max_groups:
                state.reach_limit(
                    "groups",
                    "exact scan exceeded max_groups ({})".format(request.max_groups),
                    digest_bucket[0].path,
                )
                break
            try:
                exact_groups = engine.getgroups_by_contents(
                    [candidate.file for candidate in digest_bucket],
                    bigsize=request.big_file_size,
                    stop_check=lambda: state.time_exceeded(digest_bucket[0].path),
                )
            except Exception as error:
                if state.stopped:
                    break
                state.add_issue(
                    self._issue(
                        digest_bucket[0].path,
                        "engine-failed",
                        error,
                    )
                )
                continue
            if state.stopped:
                break
            for failure in getattr(exact_groups, "verification_failures", ()):
                state.add_issue(self._verification_failure_issue(failure))
                if state.stopped:
                    break
            if state.stopped:
                break
            engine_groups_seen += len(exact_groups)
            for exact_group in exact_groups:
                if len(groups) >= request.max_groups:
                    state.reach_limit(
                        "groups",
                        "exact scan exceeded max_groups ({})".format(request.max_groups),
                        exact_group.ref.path,
                    )
                    break
                evidence = exact_group.evidence
                if (
                    exact_group.verification_kind is not engine.VerificationKind.VERIFIED_EXACT
                    or evidence is None
                    or evidence.kind is not engine.VerificationKind.VERIFIED_EXACT
                ):
                    state.add_issue(
                        ScanIssue(
                            path=str(exact_group.ref.path),
                            code="unverified-engine-group",
                            message="the production engine returned a group without verified exact evidence",
                        )
                    )
                    continue
                # The exact proof establishes equivalence, but it does not say
                # which pathname should survive.  Apply the same explainable,
                # deterministic keeper policy used by the desktop and dataset
                # workflows before serializing the CLI reference.  This only
                # changes review/action order; it cannot weaken exact evidence.
                for file in exact_group.ordered:
                    if not hasattr(file, "is_ref"):
                        file.is_ref = False
                keeper_decision = choose_keeper(exact_group.ordered)
                exact_group.prioritize(keeper_decision.sort_key)
                exact_group.keeper_decision = keeper_decision
                records: List[FileRecord] = []
                group_valid = True
                for file in exact_group.ordered:
                    if state.time_exceeded(file.path):
                        group_valid = False
                        break
                    try:
                        records.append(
                            self._record(
                                candidate_by_file[file],
                                evidence,
                                stop_check=lambda: state.time_exceeded(file.path),
                            )
                        )
                    except (KeyError, OSError, ValueError) as error:
                        if state.stopped:
                            group_valid = False
                            break
                        state.add_issue(
                            self._issue(
                                Path(file.path),
                                "post-verification-changed",
                                error,
                            )
                        )
                        group_valid = False
                    if state.stopped:
                        group_valid = False
                        break
                if records and any(
                    record.digest_algorithm != records[0].digest_algorithm or record.digest != records[0].digest
                    for record in records[1:]
                ):
                    state.add_issue(
                        ScanIssue(
                            path=records[0].path,
                            code="proof-digest-mismatch",
                            message="group members changed after core byte verification",
                        )
                    )
                    group_valid = False
                if state.stopped:
                    break
                if not group_valid or len(records) < 2:
                    continue
                digest_hex = evidence.digest.hex()
                base_group_id = "{}:{}".format(evidence.algorithm, digest_hex)
                collision_index = group_id_counts[base_group_id]
                group_id_counts[base_group_id] += 1
                if collision_index:
                    paths = "\0".join(sorted(record.path for record in records))
                    group_id = "{}:{}".format(
                        base_group_id,
                        hashlib.sha256(paths.encode("utf-8")).hexdigest()[:16],
                    )
                else:
                    group_id = base_group_id
                groups.append(
                    ScanGroup(
                        group_id=group_id,
                        verification=VERIFIED_EXACT,
                        verification_method="{}+core-streaming-byte-compare".format(evidence.algorithm),
                        reference=records[0],
                        duplicates=tuple(records[1:]),
                    )
                )
                progress(
                    "verifying",
                    {
                        "engine_groups": engine_groups_seen,
                        "verified_groups": len(groups),
                    },
                )
            if state.stopped:
                break

        if not state.stopped:
            state.time_exceeded(valid_roots[0] if valid_roots else "")
        return self._finish_report(
            request,
            valid_roots,
            candidates,
            full_digest_files,
            groups,
            state,
            coverage_records,
            progress,
        )

    @staticmethod
    def _verification_failure_issue(failure) -> ScanIssue:
        return ScanIssue(
            path=failure.second_path or failure.first_path,
            code=("byte-verification-failed" if failure.phase == "byte_compare" else "hash-failed"),
            message=(
                ("final byte comparison failed between {!r} and {!r}: {}: {}").format(
                    failure.first_path,
                    failure.second_path,
                    failure.error_type,
                    failure.message,
                )
                if failure.phase == "byte_compare"
                else ("exact-scan {} failed for {!r}: {}: {}").format(
                    failure.phase,
                    failure.first_path,
                    failure.error_type,
                    failure.message,
                )
            ),
        )

    @staticmethod
    def _add_resource_limited_coverage(
        request: ScanRequest,
        coverage_records: List[ScanCoverage],
    ) -> None:
        covered = {os.path.normcase(item.root) for item in coverage_records}
        for raw_root in request.roots:
            root = str(Path(os.path.abspath(os.path.expanduser(raw_root))))
            if os.path.normcase(root) in covered:
                continue
            coverage_records.append(
                ScanCoverage(
                    root=root,
                    complete=False,
                    counters=(("resource_limits", 1),),
                    identity_capabilities=(),
                )
            )
            covered.add(os.path.normcase(root))

    @staticmethod
    def _finish_report(
        request: ScanRequest,
        valid_roots: List[str],
        candidates: List[_Candidate],
        full_digest_files,
        groups,
        state: _ScanRunState,
        coverage_records: List[ScanCoverage],
        progress: ProgressCallback,
    ) -> ScanReport:
        ordered_groups = tuple(sorted(groups, key=lambda group: group.group_id))
        coverage_complete = bool(coverage_records) and all(item.complete for item in coverage_records)
        summary = ScanSummary(
            discovered_files=len(candidates),
            hashed_files=len(full_digest_files),
            verified_groups=len(ordered_groups),
            duplicate_files=sum(len(group.duplicates) for group in ordered_groups),
            issues=len(state.issues),
            complete=coverage_complete and not state.issues,
        )
        progress("complete", summary.to_dict())
        return ScanReport(
            scan_id=str(uuid.uuid4()),
            created_at=utc_now(),
            roots=tuple(sorted(valid_roots, key=os.path.normcase)),
            mode=request.mode,
            groups=ordered_groups,
            issues=tuple(state.issues),
            summary=summary,
            coverage=tuple(coverage_records),
        )

    def _collect_candidate(
        self,
        event,
        request: ScanRequest,
        candidates: List[_Candidate],
        seen_identities: set,
        state: _ScanRunState,
    ) -> None:
        try:
            file_stat = os.stat(event.path, follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode) or _is_reparse_point(file_stat):
                raise OSError("file type changed after safe discovery")
            if file_stat.st_size < request.min_size:
                return
            current_identity = _identity_for_event_path(event.path)
            comparison = same_physical_file(event.identity, current_identity)
            if comparison.verdict is not IdentityVerdict.SAME:
                raise OSError("file identity changed after safe discovery: {}".format(comparison.reason))
            identity_key = current_identity.comparison_key
            if identity_key in seen_identities:
                return
            if len(candidates) >= request.max_files:
                state.reach_limit(
                    "files",
                    "exact scan exceeded max_files ({})".format(request.max_files),
                    event.path,
                    during_discovery=True,
                )
                return
            seen_identities.add(identity_key)
            file = core_fs.File(Path(event.path))
            file.size = int(file_stat.st_size)
            file.mtime = float(file_stat.st_mtime)
            candidates.append(
                _Candidate(
                    file=file,
                    identity=current_identity,
                    device=int(file_stat.st_dev),
                    inode=int(file_stat.st_ino),
                    size=int(file_stat.st_size),
                    mtime_ns=int(file_stat.st_mtime_ns),
                    generation_token=get_file_generation_token(
                        event.path,
                        stat_result=file_stat,
                        expected_identity=current_identity,
                    ).encoded,
                )
            )
        except (OSError, FileGenerationError) as error:
            state.add_issue(
                self._issue(event.path, "candidate-changed", error),
                during_discovery=True,
            )

    @staticmethod
    def _validate_candidate_snapshot(candidate: _Candidate) -> os.stat_result:
        file_stat = os.stat(candidate.path, follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode) or _is_reparse_point(file_stat):
            raise OSError("candidate is no longer a plain regular file")
        if (
            int(file_stat.st_dev) != candidate.device
            or int(file_stat.st_ino) != candidate.inode
            or int(file_stat.st_size) != candidate.size
            or int(file_stat.st_mtime_ns) != candidate.mtime_ns
            or get_file_generation_token(
                candidate.path,
                stat_result=file_stat,
                expected_identity=candidate.identity,
            ).encoded
            != candidate.generation_token
        ):
            raise OSError("candidate changed during verified hashing")
        return file_stat

    @staticmethod
    def _populate_engine_candidate_filters(
        candidate: _Candidate,
        request: ScanRequest,
        stop_check=None,
    ) -> bool:
        """Populate production-engine filters without requiring the GUI cache singleton.

        Returns whether computing the partial filter necessarily computed the
        complete digest as well.
        """

        computed_full_digest = False
        if candidate.size < sum(core_fs.PARTIAL_OFFSET_SIZE):
            full_digest, snapshot = candidate.file._calc_digest_with_snapshot(
                stop_check=stop_check,
            )
            candidate.file.prime_exact_digest("digest", full_digest, snapshot)
            candidate.file.prime_exact_digest(
                "digest_partial",
                full_digest,
                snapshot,
            )
            computed_full_digest = True
        else:
            partial_digest, snapshot = candidate.file._calc_digest_partial_with_snapshot(
                stop_check=stop_check,
            )
            candidate.file.prime_exact_digest(
                "digest_partial",
                partial_digest,
                snapshot,
            )
        if request.big_file_size and candidate.size > request.big_file_size:
            if candidate.size <= core_fs.MIN_FILE_SIZE:
                if not computed_full_digest:
                    full_digest, snapshot = candidate.file._calc_digest_with_snapshot(
                        stop_check=stop_check,
                    )
                    candidate.file.prime_exact_digest(
                        "digest",
                        full_digest,
                        snapshot,
                    )
                    computed_full_digest = True
                candidate.file.prime_exact_digest(
                    "digest_samples",
                    candidate.file.digest,
                    snapshot,
                )
            else:
                sample_digest, snapshot = candidate.file._calc_digest_samples_with_snapshot(
                    stop_check=stop_check,
                )
                candidate.file.prime_exact_digest(
                    "digest_samples",
                    sample_digest,
                    snapshot,
                )
        return computed_full_digest

    def _record(self, candidate: _Candidate, evidence, stop_check=None) -> FileRecord:
        self._validate_candidate_snapshot(candidate)
        digest = candidate.file.digest
        if digest is None or digest != evidence.digest:
            raise OSError("candidate digest no longer matches the engine evidence")
        identity = _identity_for_event_path(candidate.path)
        comparison = same_physical_file(candidate.identity, identity)
        if comparison.verdict is not IdentityVerdict.SAME:
            raise OSError("candidate identity changed after byte verification")
        proof_digest = self._proof_digest(candidate, stop_check=stop_check)
        volume_id, file_id = identity_record_parts(identity)
        return FileRecord(
            path=str(candidate.path),
            size=candidate.size,
            mtime_ns=candidate.mtime_ns,
            digest_algorithm=SAFE_PROOF_HASH_ALGORITHM,
            digest=proof_digest,
            volume_id=volume_id,
            file_id=file_id,
        )

    def _proof_digest(self, candidate: _Candidate, stop_check=None) -> str:
        self._validate_candidate_snapshot(candidate)
        digest = hashlib.sha256()
        bytes_read = 0
        with self.fs.open_readonly(candidate.path) as stream:
            before = os.fstat(stream.fileno())
            while True:
                if stop_check is not None and stop_check():
                    raise InterruptedError("exact scan resource limit reached")
                chunk = stream.read(core_fs.CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
            after = os.fstat(stream.fileno())
        before_version = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
        )
        after_version = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
        )
        if before_version != after_version or bytes_read != candidate.size:
            raise OSError("candidate changed while computing its persistent SHA-256 proof")
        if before_version != (candidate.device, candidate.inode, candidate.size, candidate.mtime_ns):
            raise OSError("proof handle does not identify the discovered candidate")
        self._validate_candidate_snapshot(candidate)
        return digest.hexdigest()

    @staticmethod
    def _coverage_record(path: Path, coverage) -> ScanCoverage:
        counter_names = (
            "entries_seen",
            "files",
            "directories",
            "pruned_directories",
            "skipped_symlinks",
            "skipped_reparse_points",
            "skipped_mounts",
            "skipped_cycles",
            "skipped_outside_root",
            "skipped_special_files",
            "skipped_changed_directories",
            "errors",
            "identity_failures",
            "high_confidence_identities",
            "medium_confidence_identities",
            "low_confidence_identities",
        )
        return ScanCoverage(
            root=str(path),
            complete=coverage.complete,
            counters=tuple((name, int(getattr(coverage, name))) for name in counter_names),
            identity_capabilities=tuple(item.value for item in coverage.identity_capabilities),
        )

    @staticmethod
    def _walk_issue(event) -> ScanIssue:
        if event.error is not None:
            message = "{}: {}".format(event.error.error_type, event.error.message)
        else:
            message = event.detail or event.kind.value
        return ScanIssue(path=str(event.path), code=event.kind.value, message=message)

    @staticmethod
    def _issue(path: Path, code: str, error: BaseException) -> ScanIssue:
        return ScanIssue(path=str(path), code=code, message="{}: {}".format(type(error).__name__, error))


class SafeActionApplyAdapter(ApplyAdapter):
    """All-actions-first adapter backed exclusively by ``core.safe_action``."""

    def __init__(self, manager: Optional[QuarantineManager] = None):
        self.manager = manager or QuarantineManager()

    @property
    def supports_execute(self) -> bool:
        return True

    def preflight(self, plan: DeletionPlan, persist: bool) -> ApplyPreparation:
        if persist:
            batch = self.manager.prepare(plan)
        else:
            failures = self.manager.validate_read_only(plan)
            batch = PreparationBatch(plan.plan_id, (), failures)
        failures = {failure.action_id: failure for failure in batch.failures}
        prepared = {item.action.action_id: item for item in batch.prepared}
        results: List[ActionResult] = []
        for action in plan.actions:
            failure = failures.get(action.action_id)
            if failure is not None:
                status = (
                    "failed"
                    if failure.code in {FailureCode.IO_ERROR.value, FailureCode.INTERNAL_ERROR.value}
                    else "stale"
                )
                results.append(
                    ActionResult(
                        action_id=action.action_id,
                        target=action.target.path,
                        status=status,
                        message=failure.message,
                        safe_state="failed",
                        failure_code=failure.code,
                    )
                )
                continue
            item = prepared.get(action.action_id)
            results.append(
                ActionResult(
                    action_id=action.action_id,
                    target=action.target.path,
                    status="ready",
                    message="live target and keeper are byte-verified; no target has been changed",
                    safe_state="planned",
                    failure_code=FailureCode.NONE.value,
                    operation_plan_path=str(item.plan_path) if item is not None and persist else "",
                    quarantine_path=str(item.stored.operation_plan.quarantine_path) if item is not None else "",
                )
            )
        return ApplyPreparation(batch=batch, results=tuple(results))

    def execute(self, preparation: ApplyPreparation) -> Tuple[ActionResult, ...]:
        managed = self.manager.execute(preparation.batch)
        return tuple(
            ActionResult(
                action_id=item.action_id,
                target=item.target,
                status=item.status,
                message=item.message,
                safe_state=item.safe_state,
                failure_code=item.failure_code,
                operation_plan_path=item.operation_plan_path,
                quarantine_path=item.quarantine_path,
                changed=item.changed,
            )
            for item in managed
        )


class ReportQueryAdapter(QueryAdapter):
    def query(
        self,
        report: ScanReport,
        group_id: Optional[str] = None,
        path: Optional[str] = None,
        digest: Optional[str] = None,
    ) -> Tuple[ScanGroup, ...]:
        normalized_path = os.path.normcase(os.path.abspath(path)) if path else None
        matches: List[ScanGroup] = []
        for group in report.groups:
            if group_id is not None and group.group_id != group_id:
                continue
            if digest is not None and not any(item.digest == digest for item in group.files):
                continue
            if normalized_path is not None and not any(
                os.path.normcase(os.path.abspath(item.path)) == normalized_path for item in group.files
            ):
                continue
            matches.append(group)
        return tuple(matches)


class LocalDoctorAdapter(DoctorAdapter):
    def inspect(self) -> Dict[str, Any]:
        return {
            "schema": DOCTOR_REPORT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "app_version": __version__,
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "pyqt_imported": any(name == "PyQt6" or name.startswith("PyQt6.") for name in sys.modules),
            "capabilities": {
                "verified_exact_engine": True,
                "safe_no_follow_walk": True,
                "scan_coverage_reporting": True,
                "sha256_plan_binding": True,
                "jsonl_streaming": True,
                "plan_generation": True,
                "apply_dry_run_validation": True,
                "apply_execute": True,
                "same_volume_quarantine": True,
                "quarantine_restore": True,
                "quarantine_finalize": True,
                "trash": False,
                "persistent_catalog": True,
                "catalog_resumable_scan": True,
                "catalog_immutable_changes": True,
                "catalog_verified_groups": True,
                "dataset_workflow": True,
                "visual_similarity_review": True,
                "visual_bounded_scan": True,
                "visual_reference_query": True,
                "video_similarity_review": True,
                "video_library_scan": True,
                "filesystem_file_id": hasattr(os.stat_result, "st_ino"),
            },
        }


def _identity_for_event_path(path: Path):
    from core.file_identity import get_file_identity

    return get_file_identity(path, follow_symlinks=False)


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)
