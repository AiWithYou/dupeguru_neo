# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""High-level orchestration for multi-root durable catalog scans."""

import logging
import os

from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple, Union

from core.catalog import (
    SCAN_DIRECTORY_STATUS_VALUES,
    WORK_STATUS_VALUES,
    Catalog,
    CatalogError,
    CatalogStateError,
    ExactDigestProjectionCounts,
    ScanIncompleteError,
    preflight_catalog_path,
)
from core.catalog_indexer import CatalogIndexProgress, CatalogIndexer, IndexOutcome
from core.catalog_worker import CatalogWorker, VerifiedExactGroup, WorkerOutcome
from core.reserved_paths import is_within_reserved_internal_directory


class CatalogServiceError(Exception):
    """A catalog service operation could not be completed."""


@dataclass(frozen=True)
class CatalogServiceStatus:
    scan_id: int
    status: str
    phase: str
    directory_counts: Dict[str, int]
    work_counts: Dict[str, int]
    error_count: int
    verified_projection_allowed: bool
    started_at: float
    finished_at: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogServiceResult:
    scan_id: int
    outcome: str
    catalog_status: str
    roots_total: int
    roots_processed: int
    files_observed: int
    changed_content: int
    work_enqueued: int
    worker_batches: int
    work_completed: int
    work_retried: int
    work_failed: int
    status: CatalogServiceStatus
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogProgress:
    """User-facing progress aggregated across all selected catalog roots."""

    scan_id: int
    phase: str
    roots_total: int
    roots_processed: int
    files_seen: int
    files_observed: int
    directories_seen: int
    work_total: int
    work_completed: int
    work_failed: int


CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[CatalogProgress], None]


def _path_is_within(path: Path, root: Path) -> bool:
    candidate = os.path.normcase(os.path.abspath(os.fspath(path)))
    container = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath((candidate, container)) == container
    except ValueError:
        return False


def _canonical_path(path: Path) -> Path:
    try:
        return Path(os.path.realpath(os.fspath(path)))
    except OSError as error:
        raise CatalogServiceError("catalog path could not be resolved canonically: '{}'".format(path)) from error


def _preflight_database_outside_roots(database_path: Path, roots) -> None:
    for root in roots:
        if _path_is_within(database_path, root):
            raise CatalogServiceError("catalog database must be outside every selected root: '{}'".format(root))
    try:
        preflight_catalog_path(database_path, must_exist=False)
    except CatalogError as error:
        raise CatalogServiceError(str(error)) from error
    canonical_database = _canonical_path(database_path)
    for root in roots:
        canonical_root = _canonical_path(root)
        if _path_is_within(canonical_database, canonical_root):
            raise CatalogServiceError(
                "catalog database resolves inside selected root '{}': '{}'".format(
                    root,
                    canonical_database,
                )
            )


class _ReadOnlyVerificationCatalog:
    """Permit projection reads only when the byte proof was persisted earlier."""

    def __init__(self, catalog: Catalog):
        self._catalog = catalog

    def __getattr__(self, name):
        return getattr(self._catalog, name)

    def record_verification(
        self,
        first_content_version_id,
        second_content_version_id,
        algorithm,
        algorithm_version,
        full_digest,
        state="candidate",
        byte_compare_at=None,
        now=None,
    ):
        del byte_compare_at, now
        if state != "verified":
            raise CatalogStateError("read-only projection cannot persist a verification invalidation")
        verification_id = self._catalog.find_verification_id(
            first_content_version_id,
            second_content_version_id,
            algorithm,
            algorithm_version,
            full_digest,
            state=state,
        )
        if verification_id is None:
            raise CatalogStateError(
                "read-only group projection requires a verification persisted " "during scan or resume"
            )
        return verification_id


class CatalogService:
    """Own a local catalog and drive one durable scan across several roots."""

    def __init__(
        self,
        database_path: Union[str, os.PathLike],
        roots: Sequence[Union[str, os.PathLike]],
        analysis_kinds: Sequence[str] = ("exact_hash",),
        worker_batch_size: int = 100,
        max_worker_batches: int = 10000,
        directory_pruner=None,
        file_filter=None,
        catalog: Optional[Catalog] = None,
        selected_root_ids: Optional[Sequence[int]] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        if worker_batch_size < 1:
            raise ValueError("worker_batch_size must be at least one")
        if max_worker_batches < 1:
            raise ValueError("max_worker_batches must be at least one")
        normalized_roots = []
        seen = set()
        for root in roots:
            path = Path(os.path.abspath(os.fspath(root)))
            if is_within_reserved_internal_directory(path):
                raise CatalogServiceError(
                    "catalog root cannot be an internal dupeGuru Neo directory: " "'{}'".format(path)
                )
            key = os.path.normcase(str(path))
            if key not in seen:
                seen.add(key)
                normalized_roots.append(path)
        if not normalized_roots:
            raise ValueError("at least one catalog root is required")

        self.database_path = Path(os.path.abspath(os.fspath(database_path)))
        self.roots = tuple(normalized_roots)
        self.analysis_kinds = tuple(analysis_kinds)
        self.worker_batch_size = worker_batch_size
        self.max_worker_batches = max_worker_batches
        self.directory_pruner = directory_pruner
        self.file_filter = file_filter
        self.progress_callback = progress_callback
        if catalog is None:
            if selected_root_ids is not None:
                raise ValueError("selected_root_ids require an explicitly supplied catalog")
            _preflight_database_outside_roots(self.database_path, self.roots)
            self.catalog = Catalog(self.database_path)
        else:
            self.catalog = catalog
        if selected_root_ids is None:
            self._selected_root_ids = None
        else:
            normalized_root_ids = tuple(int(value) for value in selected_root_ids)
            if (
                len(normalized_root_ids) != len(self.roots)
                or len(set(normalized_root_ids)) != len(normalized_root_ids)
                or any(value < 1 for value in normalized_root_ids)
            ):
                raise ValueError("selected_root_ids must uniquely identify every service root")
            self._selected_root_ids = normalized_root_ids
        self._progress_scan_id = 0
        self._progress_roots_processed = 0
        self._progress_files_seen = [0] * len(self.roots)
        self._progress_files_observed = [0] * len(self.roots)
        self._progress_directories_seen = [0] * len(self.roots)
        self._progress_work_total = 0
        self._progress_work_completed = 0
        self._progress_work_failed = 0
        indexers = []
        for index, root in enumerate(self.roots):
            index_progress_callback = None
            if self.progress_callback is not None:
                index_progress_callback = partial(self._on_index_progress, index)
            indexers.append(
                CatalogIndexer(
                    self.catalog,
                    root,
                    analysis_kinds=self.analysis_kinds,
                    owner="catalog-service-indexer-{}".format(index),
                    directory_pruner=self.directory_pruner,
                    file_filter=self.file_filter,
                    progress_callback=index_progress_callback,
                )
            )
        self.indexers = tuple(indexers)
        self.worker = CatalogWorker(self.catalog, owner="catalog-service-worker")

    def _reset_progress(self, scan_id: int) -> None:
        self._progress_scan_id = scan_id
        self._progress_roots_processed = 0
        self._progress_files_seen = [0] * len(self.roots)
        self._progress_files_observed = [0] * len(self.roots)
        self._progress_directories_seen = [0] * len(self.roots)
        self._progress_work_total = 0
        self._progress_work_completed = 0
        self._progress_work_failed = 0

    def _emit_progress(self, phase: str) -> None:
        if self.progress_callback is None:
            return
        progress = CatalogProgress(
            scan_id=self._progress_scan_id,
            phase=phase,
            roots_total=len(self.roots),
            roots_processed=self._progress_roots_processed,
            files_seen=sum(self._progress_files_seen),
            files_observed=sum(self._progress_files_observed),
            directories_seen=sum(self._progress_directories_seen),
            work_total=self._progress_work_total,
            work_completed=self._progress_work_completed,
            work_failed=self._progress_work_failed,
        )
        try:
            self.progress_callback(progress)
        except Exception:
            logging.debug("Catalog service progress callback failed", exc_info=True)

    def _on_index_progress(self, root_index: int, progress: CatalogIndexProgress) -> None:
        if progress.scan_id != self._progress_scan_id:
            return
        self._progress_files_seen[root_index] = progress.files_seen
        self._progress_files_observed[root_index] = progress.files_observed
        self._progress_directories_seen[root_index] = progress.directories_seen
        self._emit_progress("enumerating")

    def _set_work_progress_from_coverage(self, coverage) -> None:
        self._progress_work_total = sum(int(coverage.get("work_{}".format(status), 0)) for status in WORK_STATUS_VALUES)
        self._progress_work_completed = int(coverage.get("work_complete", 0))
        self._progress_work_failed = int(coverage.get("work_failed", 0))

    def close(self) -> None:
        self.catalog.close()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.close()

    def start_scan(self, app_version: Optional[str] = None) -> int:
        registrations = [indexer.register_root() for indexer in self.indexers]
        self._selected_root_ids = tuple(registration.root_id for registration in registrations)
        return self.catalog.start_scan(
            self._selected_root_ids,
            app_version=app_version,
        )

    def selected_root_ids(self) -> Tuple[int, ...]:
        if self._selected_root_ids is None:
            if self.catalog.read_only:
                raise CatalogStateError("read-only catalog services require bound selected_root_ids")
            self._selected_root_ids = tuple(indexer.register_root().root_id for indexer in self.indexers)
        return self._selected_root_ids

    def status(self, scan_id: int) -> CatalogServiceStatus:
        return self.status_for_catalog(self.catalog, scan_id)

    @staticmethod
    def status_for_catalog(
        catalog: Catalog,
        scan_id: int,
    ) -> CatalogServiceStatus:
        """Build status from either a writable or read-only catalog handle."""

        scan = catalog.get_scan(scan_id)
        scan_root_ids = tuple(row["root_id"] for row in catalog.scan_roots(scan_id))
        coverage = catalog.scan_coverage(scan_id)
        directory_counts = {state: int(coverage.get(state, 0)) for state in sorted(SCAN_DIRECTORY_STATUS_VALUES)}
        directory_counts["total"] = int(coverage.get("total", 0))
        work_counts = {state: int(coverage.get("work_{}".format(state), 0)) for state in sorted(WORK_STATUS_VALUES)}
        work_counts["total"] = sum(work_counts.values())
        projection_allowed = False
        try:
            catalog.require_roots_projectable(scan_root_ids)
        except (CatalogStateError, ValueError):
            pass
        else:
            projection_allowed = scan["status"] == "complete"
        return CatalogServiceStatus(
            scan_id=scan_id,
            status=scan["status"],
            phase=scan["phase"],
            directory_counts=directory_counts,
            work_counts=work_counts,
            error_count=int(coverage.get("errors", 0)),
            verified_projection_allowed=projection_allowed,
            started_at=scan["started_at"],
            finished_at=scan["finished_at"],
        )

    def _result(
        self,
        scan_id: int,
        outcome: str,
        roots_processed: int,
        files_observed: int,
        changed_content: int,
        work_enqueued: int,
        worker_batches: int,
        work_completed: int,
        work_retried: int,
        work_failed: int,
        errors,
    ) -> CatalogServiceResult:
        status = self.status(scan_id)
        reported_errors = list(errors)
        if status.status != "complete":
            for row in self.catalog.page_scan_errors(scan_id, limit=3):
                detail = "{} on '{}': {}".format(
                    row["operation"],
                    row["path"],
                    row["message"],
                )
                if detail not in reported_errors:
                    reported_errors.append(detail)
        self._progress_work_total = status.work_counts["total"]
        self._progress_work_completed = status.work_counts["complete"]
        self._progress_work_failed = status.work_counts["failed"]
        result = CatalogServiceResult(
            scan_id=scan_id,
            outcome=outcome,
            catalog_status=status.status,
            roots_total=len(self.indexers),
            roots_processed=roots_processed,
            files_observed=files_observed,
            changed_content=changed_content,
            work_enqueued=work_enqueued,
            worker_batches=worker_batches,
            work_completed=work_completed,
            work_retried=work_retried,
            work_failed=work_failed,
            status=status,
            errors=tuple(reported_errors),
        )
        self._emit_progress("finished" if outcome == "finished" else "partial")
        return result

    def run(
        self,
        app_version: Optional[str] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> CatalogServiceResult:
        scan_id = self.start_scan(app_version=app_version)
        return self.resume(scan_id, cancel_check=cancel_check)

    def resume(
        self,
        scan_id: int,
        cancel_check: Optional[CancelCheck] = None,
    ) -> CatalogServiceResult:
        """Resume root enumeration, bounded work batches, and finalization."""

        self._reset_progress(scan_id)
        self._emit_progress("enumerating")
        scan = self.catalog.get_scan(scan_id)
        selected_root_ids = self.selected_root_ids()
        scan_root_ids = tuple(row["root_id"] for row in self.catalog.scan_roots(scan_id))
        if set(scan_root_ids) != set(selected_root_ids):
            return self._result(
                scan_id,
                "partial",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                ("service roots do not match the roots selected by this scan",),
            )
        if scan["status"] != "running":
            outcome = "finished" if scan["status"] == "complete" else "partial"
            return self._result(scan_id, outcome, 0, 0, 0, 0, 0, 0, 0, 0, ())

        roots_processed = 0
        files_observed = changed_content = work_enqueued = 0
        worker_batches = work_completed = work_retried = work_failed = 0
        errors = []

        for indexer in self.indexers:
            if cancel_check is not None and cancel_check():
                errors.append("catalog service interrupted before the next root")
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )
            try:
                indexed = indexer.run(
                    scan_id=scan_id,
                    cancel_check=cancel_check,
                    cancel_scan_on_cancel=False,
                )
            except Exception as error:
                errors.append("{}: {}".format(type(error).__name__, error))
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )
            roots_processed += 1
            self._progress_roots_processed = roots_processed
            files_observed += indexed.files_observed
            changed_content += indexed.changed_content
            work_enqueued += indexed.work_enqueued
            self._emit_progress("enumerating")
            if indexed.outcome == IndexOutcome.CANCELLED:
                errors.append(indexed.reason or "catalog service interrupted during root enumeration")
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )
            if indexed.errors_recorded or indexed.catalog_status == "completed_with_errors":
                errors.append(indexed.reason or "root enumeration completed with errors")
            if self.catalog.get_scan(scan_id)["status"] != "running":
                break

        scan = self.catalog.get_scan(scan_id)
        if scan["status"] == "running":
            coverage = self.catalog.scan_coverage(scan_id)
            if coverage.get("pending", 0) or coverage.get("in_progress", 0):
                errors.append("one or more root directory leases remain pending")
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )

            self._set_work_progress_from_coverage(coverage)
            self._emit_progress("analyzing")
            while worker_batches < self.max_worker_batches:
                if cancel_check is not None and cancel_check():
                    errors.append("catalog service interrupted before the next worker batch")
                    return self._result(
                        scan_id,
                        "partial",
                        roots_processed,
                        files_observed,
                        changed_content,
                        work_enqueued,
                        worker_batches,
                        work_completed,
                        work_retried,
                        work_failed,
                        errors,
                    )
                batch = self.worker.run_batch(
                    scan_id=scan_id,
                    limit=self.worker_batch_size,
                    cancel_check=cancel_check,
                )
                if batch.outcome == WorkerOutcome.IDLE:
                    break
                worker_batches += 1
                work_completed += batch.completed
                work_retried += batch.retried
                work_failed += batch.failed
                errors.extend(batch.errors)
                self._progress_work_completed += batch.completed
                self._progress_work_failed += batch.failed
                self._emit_progress("analyzing")
                if batch.outcome == WorkerOutcome.CANCELLED:
                    return self._result(
                        scan_id,
                        "partial",
                        roots_processed,
                        files_observed,
                        changed_content,
                        work_enqueued,
                        worker_batches,
                        work_completed,
                        work_retried,
                        work_failed,
                        errors,
                    )

            coverage = self.catalog.scan_coverage(scan_id)
            if coverage.get("work_pending", 0) or coverage.get("work_in_progress", 0):
                errors.append("worker batch bound reached with unfinished work")
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )
            try:
                self._emit_progress("finalizing")
                self.catalog.finish_scan(scan_id)
            except ScanIncompleteError as error:
                errors.append(str(error))
                return self._result(
                    scan_id,
                    "partial",
                    roots_processed,
                    files_observed,
                    changed_content,
                    work_enqueued,
                    worker_batches,
                    work_completed,
                    work_retried,
                    work_failed,
                    errors,
                )

        final_status = self.catalog.get_scan(scan_id)["status"]
        outcome = "finished" if final_status == "complete" else "partial"
        return self._result(
            scan_id,
            outcome,
            roots_processed,
            files_observed,
            changed_content,
            work_enqueued,
            worker_batches,
            work_completed,
            work_retried,
            work_failed,
            errors,
        )

    def backup(self, destination: Union[str, os.PathLike]) -> str:
        return self.catalog.backup_to(destination)

    def verified_exact_projection_counts(self) -> ExactDigestProjectionCounts:
        """Return exact projection size facts without materializing file rows."""

        root_ids = self.selected_root_ids()
        self.catalog.require_roots_projectable(root_ids)
        return self.catalog.exact_digest_projection_counts(
            algorithm="sha256",
            algorithm_version="1",
            root_ids=root_ids,
        )

    def iter_verified_exact_groups(
        self,
        page_size: int = 100,
        max_page_files: Optional[int] = None,
        max_group_members: Optional[int] = None,
    ) -> Iterator[VerifiedExactGroup]:
        root_ids = self.selected_root_ids()
        self.catalog.require_roots_projectable(root_ids)
        worker = self.worker
        if self.catalog.read_only:
            worker = CatalogWorker(
                _ReadOnlyVerificationCatalog(self.catalog),
                owner="catalog-service-read-only-projection",
            )
        after_size = -1
        after_digest = b""
        while True:
            page_arguments = {
                "after_size": after_size,
                "after_digest": after_digest,
                "limit": page_size,
                "root_ids": root_ids,
            }
            if max_page_files is not None:
                page_arguments["max_rows"] = max_page_files
            if max_group_members is not None:
                page_arguments["max_group_members"] = max_group_members
            page = worker.page_verified_exact_groups(**page_arguments)
            for group in page.groups:
                yield group
            next_key = (page.next_after_size, page.next_after_digest)
            if next_key == (after_size, after_digest):
                return
            after_size, after_digest = next_key


__all__ = [
    "CatalogService",
    "CatalogServiceError",
    "CatalogServiceResult",
    "CatalogServiceStatus",
]
