# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Fail-closed integration between :mod:`core.safe_walk` and the durable catalog.

``CatalogIndexer`` owns filesystem enumeration and durable observation only. It
does not hash file contents itself. Changed content is placed in the catalog work
queue with the exact metadata and identity snapshot that a worker must validate
before and after reading. A scan remains ``running``/``partial`` until those work
items reach a terminal state and :meth:`CatalogIndexer.finalize_scan` succeeds.
"""

import os
import stat
import sys
import time
import uuid

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple, Union

from core import fs
from core.catalog import Catalog, CatalogStateError, ScanIncompleteError
from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityConfidence,
    IdentityVerdict,
    get_file_identity,
    same_physical_file,
)
from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
)
from core.safe_walk import WalkCoverage, WalkEvent, WalkEventKind, is_reparse_point, walk_no_follow
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_reserved_internal_file,
    is_within_reserved_internal_directory,
)


class CatalogIndexError(Exception):
    """Base class for catalog indexing failures."""


class FileChangedDuringIndexing(CatalogIndexError):
    """A file no longer matches the identity and metadata being cataloged."""

    def __init__(self, path, reason):
        self.path = Path(path)
        self.reason = reason
        super().__init__("File changed while indexing '{}': {}".format(self.path, reason))


class CatalogPageChangedError(CatalogIndexError):
    """A catalog row cannot safely be materialized as a current ``fs.File``."""

    def __init__(self, path, reason):
        self.path = Path(path)
        self.reason = reason
        super().__init__("Catalog path '{}' is stale: {}".format(self.path, reason))


class IndexOutcome(Enum):
    FINISHED = "finished"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RootRegistration:
    volume_id: int
    root_id: int
    root_path: Path
    identity: FileIdentity


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    size: int
    mtime_ns: int
    generation_token: FileGenerationToken
    identity_token: bytes

    @property
    def change_token(self) -> bytes:
        return self.generation_token.encoded


@dataclass(frozen=True)
class IndexRunResult:
    scan_id: int
    root_id: int
    volume_id: int
    outcome: IndexOutcome
    catalog_status: str
    coverage: Optional[WalkCoverage]
    files_observed: int
    changed_content: int
    work_enqueued: int
    errors_recorded: int
    reason: str = ""


@dataclass(frozen=True)
class CatalogFilePage:
    files: Tuple[fs.File, ...]
    rows: Tuple[Any, ...]
    next_after_id: int


CancelCheck = Callable[[], bool]
IdentityGetter = Callable[..., FileIdentity]
Walker = Callable[..., Iterator[WalkEvent]]
StatGetter = Callable[..., os.stat_result]
GenerationGetter = Callable[..., FileGenerationToken]
DirectoryPruner = Callable[[Path], Optional[Union[bool, str]]]
FileFilter = Callable[[Path], Optional[Union[bool, str]]]


class CatalogIndexer:
    """Index one bounded root using explicit walk events and durable leases."""

    def __init__(
        self,
        catalog: Catalog,
        root_path: Union[str, os.PathLike],
        allowed_root: Optional[Union[str, os.PathLike]] = None,
        cross_mounts: bool = False,
        analysis_kinds: Sequence[str] = ("exact_hash",),
        owner: Optional[str] = None,
        fs_type: Optional[str] = None,
        identity_getter: IdentityGetter = get_file_identity,
        generation_getter: GenerationGetter = get_file_generation_token,
        walker: Walker = walk_no_follow,
        stat_getter: StatGetter = os.stat,
        clock: Callable[[], float] = time.time,
        directory_pruner: Optional[DirectoryPruner] = None,
        file_filter: Optional[FileFilter] = None,
    ):
        self.catalog = catalog
        self.root_path = Path(os.path.abspath(os.fspath(root_path)))
        if is_within_reserved_internal_directory(self.root_path):
            raise ValueError(
                "catalog root cannot be an internal dupeGuru Neo directory: " "'{}'".format(self.root_path)
            )
        self.allowed_root = Path(
            os.path.abspath(os.fspath(allowed_root if allowed_root is not None else self.root_path))
        )
        if cross_mounts:
            raise ValueError(
                "CatalogIndexer cannot safely associate more than one volume with a root; "
                "register each mounted volume as a separate root"
            )
        self.cross_mounts = cross_mounts
        if isinstance(analysis_kinds, str):
            analysis_kinds = (analysis_kinds,)
        self.analysis_kinds = tuple(dict.fromkeys(analysis_kinds))
        if any(not kind for kind in self.analysis_kinds):
            raise ValueError("analysis kinds must not be empty strings")
        self.owner = owner or "catalog-indexer-{}".format(uuid.uuid4().hex)
        self.fs_type = fs_type
        self.identity_getter = identity_getter
        self.generation_getter = generation_getter
        self.walker = walker
        self.stat_getter = stat_getter
        self.clock = clock
        self.directory_pruner = directory_pruner
        self.file_filter = file_filter

    @staticmethod
    def _identity_confidence(identity: FileIdentity) -> str:
        if identity.confidence == IdentityConfidence.HIGH:
            return "stable"
        if identity.confidence == IdentityConfidence.MEDIUM:
            return "session_only"
        return "path_only"

    @staticmethod
    def _volume_key(identity: FileIdentity) -> str:
        return "{}:{}".format(identity.namespace, identity.volume_id)

    @staticmethod
    def _identity_token(identity: FileIdentity) -> bytes:
        if isinstance(identity.file_id, bytes):
            file_id_type = "bytes"
            file_id_value = identity.file_id.hex()
        else:
            file_id_type = "int"
            file_id_value = str(identity.file_id)
        return "\0".join(
            (
                identity.namespace,
                identity.capability.value,
                str(identity.volume_id),
                file_id_type,
                file_id_value,
            )
        ).encode("utf-8")

    def _validate_walk_event(self, event: WalkEvent, registration: RootRegistration) -> None:
        candidate = os.path.normcase(os.path.abspath(os.fspath(event.path)))
        root = os.path.normcase(os.path.abspath(os.fspath(self.root_path)))
        try:
            within_root = os.path.commonpath((candidate, root)) == root
        except ValueError:
            within_root = False
        if not within_root:
            raise CatalogIndexError("Walk event escaped the registered root: '{}'".format(event.path))

        if (
            event.kind
            in {
                WalkEventKind.ROOT_STARTED,
                WalkEventKind.COVERAGE,
                WalkEventKind.ROOT_COMPLETED,
            }
            and candidate != root
        ):
            raise CatalogIndexError("{} event does not name the registered root".format(event.kind.value))

        if event.kind not in {WalkEventKind.DIRECTORY, WalkEventKind.FILE}:
            return
        if event.identity is None:
            raise CatalogIndexError("{} event has no physical identity".format(event.kind.value))
        if event.identity.volume_key != registration.identity.volume_key:
            raise CatalogIndexError(
                "{} event crossed from volume {!r} to {!r}".format(
                    event.kind.value,
                    registration.identity.volume_key,
                    event.identity.volume_key,
                )
            )
        if event.kind == WalkEventKind.DIRECTORY and candidate == root:
            comparison = same_physical_file(registration.identity, event.identity)
            if comparison.verdict != IdentityVerdict.SAME:
                raise FileChangedDuringIndexing(self.root_path, comparison.reason)

    def register_root(self, now: Optional[float] = None) -> RootRegistration:
        """Register the root and its identity-bearing volume before starting a scan."""

        now = self.clock() if now is None else now
        try:
            identity = self.identity_getter(self.root_path, follow_symlinks=False)
        except (FileIdentityError, OSError) as error:
            raise CatalogIndexError("Could not identify root '{}': {}".format(self.root_path, error))
        confidence = self._identity_confidence(identity)
        volume_id = self.catalog.upsert_volume(
            self._volume_key(identity),
            platform=sys.platform,
            fs_type=self.fs_type,
            identity_capability=confidence,
            now=now,
        )
        root_id = self.catalog.upsert_root(volume_id, self.root_path, now=now)
        return RootRegistration(volume_id, root_id, self.root_path, identity)

    def begin_scan(
        self,
        resume_of_scan_id: Optional[int] = None,
        app_version: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Tuple[RootRegistration, int]:
        now = self.clock() if now is None else now
        registration = self.register_root(now=now)
        scan_id = self.catalog.start_scan(
            [registration.root_id],
            app_version=app_version,
            resume_of_scan_id=resume_of_scan_id,
            now=now,
        )
        return registration, scan_id

    def run(
        self,
        scan_id: Optional[int] = None,
        cancel_check: Optional[CancelCheck] = None,
        app_version: Optional[str] = None,
        lease_seconds: float = 60,
        cancel_scan_on_cancel: bool = True,
    ) -> IndexRunResult:
        """Run or resume enumeration.

        A first scan that discovers changed content normally returns ``PARTIAL``
        because analysis work is pending. After workers complete those items,
        call :meth:`finalize_scan`.
        """

        now = self.clock()
        registration = self.register_root(now=now)
        if scan_id is None:
            scan_id = self.catalog.start_scan(
                [registration.root_id],
                app_version=app_version,
                now=now,
            )
        scan = self.catalog.get_scan(scan_id)
        if scan["status"] != "running":
            return self._terminal_result(registration, scan_id, scan["status"])

        self.catalog.resume_expired_scan_directory_leases(
            scan_id,
            registration.root_id,
            now=now,
        )
        active_directories = self._claim_all_directories(
            scan_id,
            registration.root_id,
            lease_seconds,
            now,
        )
        coverage_before = self.catalog.scan_root_coverage(scan_id, registration.root_id)
        if coverage_before["in_progress"] > len(active_directories):
            return self._result(
                registration,
                scan_id,
                IndexOutcome.PARTIAL,
                coverage=None,
                reason="another owner still holds a directory lease",
            )
        if not active_directories and coverage_before["pending"] == 0 and coverage_before["in_progress"] == 0:
            return self.finalize_scan(scan_id, registration=registration)

        directory_ids = {path_key: row["id"] for path_key, row in active_directories.items()}
        seen_directories = set()
        files_observed = 0
        changed_content = 0
        work_enqueued = 0
        errors_recorded = 0
        walk_issues_recorded = 0
        coverage = None

        try:
            for event in self.walker(
                self.root_path,
                allowed_root=self.allowed_root,
                cross_mounts=self.cross_mounts,
                identity_getter=self.identity_getter,
                directory_pruner=self._directory_prune_reason,
            ):
                if cancel_check is not None and cancel_check():
                    if cancel_scan_on_cancel:
                        self.catalog.cancel_scan(scan_id, now=self.clock())
                    return IndexRunResult(
                        scan_id=scan_id,
                        root_id=registration.root_id,
                        volume_id=registration.volume_id,
                        outcome=IndexOutcome.CANCELLED,
                        catalog_status="cancelled" if cancel_scan_on_cancel else "running",
                        coverage=coverage,
                        files_observed=files_observed,
                        changed_content=changed_content,
                        work_enqueued=work_enqueued,
                        errors_recorded=errors_recorded,
                        reason="cancel requested",
                    )

                self._validate_walk_event(event, registration)
                if event.kind == WalkEventKind.DIRECTORY:
                    path_key, directory_id, active = self._activate_directory(
                        scan_id,
                        registration.root_id,
                        event.path,
                        active_directories,
                        lease_seconds,
                    )
                    directory_ids[path_key] = directory_id
                    seen_directories.add(path_key)
                    if active is not None:
                        active_directories[path_key] = active
                elif event.kind == WalkEventKind.FILE:
                    filter_reason = self._file_filter_reason(event.path)
                    if filter_reason is not None:
                        self.catalog.journal_action(
                            "file_filtered",
                            status="completed",
                            scan_id=scan_id,
                            payload={
                                "path": str(event.path),
                                "reason": filter_reason,
                            },
                            now=self.clock(),
                        )
                        continue
                    parent_key = self.catalog.normalize_path(event.path.parent, sys.platform)
                    if parent_key not in active_directories:
                        if parent_key not in seen_directories:
                            self._record_file_error(
                                scan_id,
                                directory_ids.get(parent_key),
                                event.path,
                                CatalogIndexError("FILE event parent does not have a directory lease"),
                            )
                            errors_recorded += 1
                        continue
                    try:
                        observation, enqueued = self._observe_file(
                            scan_id,
                            registration.root_id,
                            event,
                        )
                    except (CatalogIndexError, CatalogStateError, FileIdentityError, OSError) as error:
                        self._record_file_error(
                            scan_id,
                            directory_ids.get(parent_key),
                            event.path,
                            error,
                        )
                        errors_recorded += 1
                    else:
                        files_observed += 1
                        changed_content += int(observation.new_content)
                        work_enqueued += enqueued
                elif event.kind == WalkEventKind.COVERAGE:
                    coverage = event.coverage
                elif event.kind == WalkEventKind.DIRECTORY_PRUNED:
                    path_key = self.catalog.normalize_path(event.path, sys.platform)
                    root_key = self.catalog.normalize_path(self.root_path, sys.platform)
                    if path_key == root_key:
                        self._record_walk_issue(
                            scan_id,
                            event,
                            active_directories,
                            directory_ids,
                        )
                        errors_recorded += 1
                        walk_issues_recorded += 1
                    else:
                        self.catalog.journal_action(
                            "directory_pruned",
                            status="completed",
                            scan_id=scan_id,
                            payload={
                                "path": str(event.path),
                                "reason": event.detail or "directory intentionally pruned",
                            },
                            now=self.clock(),
                        )
                elif event.kind not in {
                    WalkEventKind.ROOT_STARTED,
                    WalkEventKind.ROOT_COMPLETED,
                }:
                    self._record_walk_issue(
                        scan_id,
                        event,
                        active_directories,
                        directory_ids,
                    )
                    errors_recorded += 1
                    walk_issues_recorded += 1

            if coverage is None:
                self._record_file_error(
                    scan_id,
                    directory_ids.get(self.catalog.normalize_path(self.root_path, sys.platform)),
                    self.root_path,
                    CatalogIndexError("walker ended without a coverage event"),
                )
                return self._result(
                    registration,
                    scan_id,
                    IndexOutcome.PARTIAL,
                    coverage=None,
                    files_observed=files_observed,
                    changed_content=changed_content,
                    work_enqueued=work_enqueued,
                    errors_recorded=errors_recorded + 1,
                    reason="walker ended without durable coverage",
                )

            if not coverage.complete and walk_issues_recorded == 0:
                self._record_file_error(
                    scan_id,
                    None,
                    self.root_path,
                    CatalogIndexError("walker reported incomplete coverage without an issue event"),
                    operation="validate coverage",
                )
                errors_recorded += 1

            for path_key, row in list(active_directories.items()):
                if path_key in seen_directories:
                    self.catalog.complete_scan_directory(
                        row["id"],
                        owner=self.owner,
                        now=self.clock(),
                    )
                else:
                    self.catalog.fail_scan_directory(
                        row["id"],
                        "resume coverage",
                        "previously leased directory was not emitted by the resumed walk",
                        transient=True,
                        owner=self.owner,
                        now=self.clock(),
                    )
                    errors_recorded += 1

            self.catalog.journal_action(
                "scan_coverage",
                status="completed" if coverage.complete else "failed",
                scan_id=scan_id,
                payload=self._coverage_payload(coverage),
                now=self.clock(),
            )
        except Exception as error:
            root_key = self.catalog.normalize_path(self.root_path, sys.platform)
            self._record_file_error(
                scan_id,
                directory_ids.get(root_key),
                self.root_path,
                error,
            )
            return self._result(
                registration,
                scan_id,
                IndexOutcome.PARTIAL,
                coverage=coverage,
                files_observed=files_observed,
                changed_content=changed_content,
                work_enqueued=work_enqueued,
                errors_recorded=errors_recorded + 1,
                reason="indexer exception: {}".format(error),
            )

        return self.finalize_scan(
            scan_id,
            registration=registration,
            coverage=coverage,
            files_observed=files_observed,
            changed_content=changed_content,
            work_enqueued=work_enqueued,
            errors_recorded=errors_recorded,
        )

    def _file_filter_reason(self, path: Path) -> Optional[str]:
        if is_reserved_internal_file(path):
            return "internal dupeGuru Neo temporary payload is always excluded"
        if self.file_filter is None:
            return None
        decision = self.file_filter(path)
        if decision is None or decision is True:
            return None
        if decision is False:
            return "file rejected by catalog filter"
        if isinstance(decision, str):
            return decision
        raise ValueError("file_filter must return None, bool, or a reason string")

    def _directory_prune_reason(self, path: Path) -> Optional[Union[bool, str]]:
        if path != self.root_path and is_reserved_internal_directory(path):
            return "internal dupeGuru Neo directory is always excluded"
        if self.directory_pruner is None:
            return None
        return self.directory_pruner(path)

    def _claim_all_directories(
        self,
        scan_id: int,
        root_id: int,
        lease_seconds: float,
        now: float,
    ) -> Dict[str, Any]:
        claimed = {}
        while True:
            page = self.catalog.claim_scan_directories(
                scan_id,
                self.owner,
                limit=1000,
                lease_seconds=lease_seconds,
                now=now,
                root_id=root_id,
            )
            if not page:
                return claimed
            for row in page:
                claimed[row["path_key"]] = row

    def _activate_directory(
        self,
        scan_id: int,
        root_id: int,
        path: Path,
        active_directories: Dict[str, Any],
        lease_seconds: float,
    ) -> Tuple[str, int, Optional[Any]]:
        path_key = self.catalog.normalize_path(path, sys.platform)
        directory_id = self.catalog.enqueue_directory(
            scan_id,
            root_id,
            path,
            path_key=path_key,
            now=self.clock(),
        )
        if path_key in active_directories:
            return path_key, directory_id, active_directories[path_key]
        newly_claimed = self._claim_all_directories(
            scan_id,
            root_id,
            lease_seconds,
            self.clock(),
        )
        active_directories.update(newly_claimed)
        return path_key, directory_id, active_directories.get(path_key)

    def _observe_file(self, scan_id: int, root_id: int, event: WalkEvent):
        if event.identity is None:
            raise CatalogIndexError("FILE event has no physical identity")
        with self.catalog.transaction():
            before = self._snapshot(event.path, expected_identity=event.identity)
            observation = self.catalog.observe_file(
                scan_id,
                root_id,
                event.path,
                size=before.size,
                mtime_ns=before.mtime_ns,
                native_file_id=before.identity_token,
                identity_confidence=self._identity_confidence(event.identity),
                change_token=before.change_token,
                now=self.clock(),
            )
            enqueued = 0
            if observation.new_content:
                for kind in self.analysis_kinds:
                    self.catalog.enqueue_work_item(
                        scan_id,
                        observation.content_version_id,
                        kind,
                        payload={
                            "path": str(before.path),
                            "size": before.size,
                            "mtime_ns": before.mtime_ns,
                            "change_token": before.change_token.hex(),
                            "identity_token": before.identity_token.hex(),
                        },
                        now=self.clock(),
                    )
                    enqueued += 1
            try:
                after = self._snapshot(event.path, expected_identity=event.identity)
            except (CatalogIndexError, FileIdentityError, OSError) as error:
                raise FileChangedDuringIndexing(
                    event.path,
                    "metadata changed during durable observation: {}".format(error),
                ) from error
            if before != after:
                raise FileChangedDuringIndexing(event.path, "metadata changed during durable observation")
            return observation, enqueued

    def _snapshot(self, path: Path, expected_identity: Optional[FileIdentity] = None) -> FileSnapshot:
        stat_result = self.stat_getter(str(path), follow_symlinks=False)
        if stat.S_ISLNK(stat_result.st_mode):
            raise FileChangedDuringIndexing(path, "path became a symbolic link")
        if is_reparse_point(stat_result):
            raise FileChangedDuringIndexing(path, "path became a reparse point")
        if not stat.S_ISREG(stat_result.st_mode):
            raise FileChangedDuringIndexing(path, "path is no longer a regular file")
        identity = self.identity_getter(path, follow_symlinks=False, stat_result=stat_result)
        if expected_identity is not None:
            comparison = same_physical_file(expected_identity, identity)
            if comparison.verdict != IdentityVerdict.SAME:
                raise FileChangedDuringIndexing(path, comparison.reason)
        try:
            generation_token = self.generation_getter(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
                expected_identity=identity,
            )
        except (FileGenerationError, OSError) as error:
            raise FileChangedDuringIndexing(path, str(error)) from error
        if not isinstance(generation_token, FileGenerationToken):
            raise FileChangedDuringIndexing(path, "generation getter returned an invalid token")
        return FileSnapshot(
            path=Path(path),
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
            generation_token=generation_token,
            identity_token=self._identity_token(identity),
        )

    def _record_file_error(
        self,
        scan_id: int,
        scan_dir_id: Optional[int],
        path: Path,
        error: Exception,
        operation: str = "observe file",
    ) -> None:
        cause = error.cause if isinstance(error, FileIdentityError) and error.cause is not None else error
        self.catalog.record_scan_error(
            scan_id,
            path,
            operation,
            str(error),
            error_code=str(getattr(cause, "winerror", None) or getattr(cause, "errno", None) or type(cause).__name__),
            transient=True,
            scan_dir_id=scan_dir_id,
            now=self.clock(),
        )

    def _record_walk_issue(
        self,
        scan_id: int,
        event: WalkEvent,
        active_directories: Dict[str, Any],
        directory_ids: Dict[str, int],
    ) -> None:
        target_key = self.catalog.normalize_path(event.path, sys.platform)
        error = event.error
        operation = error.operation if error is not None else event.kind.value
        message = error.message if error is not None else event.detail or event.kind.value
        error_code = None
        if error is not None:
            error_code = str(error.winerror or error.errno or error.error_type)

        if target_key in active_directories:
            row = active_directories.pop(target_key)
            self.catalog.fail_scan_directory(
                row["id"],
                operation,
                message,
                error_code=error_code,
                transient=event.kind in {WalkEventKind.ERROR, WalkEventKind.DIRECTORY_CHANGED_SKIPPED},
                owner=self.owner,
                now=self.clock(),
            )
            return

        parent_key = self.catalog.normalize_path(event.path.parent, sys.platform)
        scan_dir_id = directory_ids.get(parent_key)
        self.catalog.record_scan_error(
            scan_id,
            event.path,
            operation,
            message,
            error_code=error_code,
            transient=event.kind in {WalkEventKind.ERROR, WalkEventKind.DIRECTORY_CHANGED_SKIPPED},
            scan_dir_id=scan_dir_id,
            now=self.clock(),
        )

    @staticmethod
    def _coverage_payload(coverage: WalkCoverage) -> Dict[str, Any]:
        payload = asdict(coverage)
        payload["identity_capabilities"] = [capability.value for capability in coverage.identity_capabilities]
        payload["complete"] = coverage.complete
        return payload

    def finalize_scan(
        self,
        scan_id: int,
        registration: Optional[RootRegistration] = None,
        coverage: Optional[WalkCoverage] = None,
        files_observed: int = 0,
        changed_content: int = 0,
        work_enqueued: int = 0,
        errors_recorded: int = 0,
    ) -> IndexRunResult:
        registration = registration or self.register_root()
        scan = self.catalog.get_scan(scan_id)
        if scan["status"] != "running":
            return self._terminal_result(registration, scan_id, scan["status"])
        try:
            status = self.catalog.finish_scan(scan_id, now=self.clock())
        except ScanIncompleteError as error:
            return IndexRunResult(
                scan_id=scan_id,
                root_id=registration.root_id,
                volume_id=registration.volume_id,
                outcome=IndexOutcome.PARTIAL,
                catalog_status="running",
                coverage=coverage,
                files_observed=files_observed,
                changed_content=changed_content,
                work_enqueued=work_enqueued,
                errors_recorded=errors_recorded,
                reason=str(error),
            )
        outcome = IndexOutcome.FINISHED if status == "complete" else IndexOutcome.PARTIAL
        return IndexRunResult(
            scan_id=scan_id,
            root_id=registration.root_id,
            volume_id=registration.volume_id,
            outcome=outcome,
            catalog_status=status,
            coverage=coverage,
            files_observed=files_observed,
            changed_content=changed_content,
            work_enqueued=work_enqueued,
            errors_recorded=errors_recorded,
            reason="" if outcome == IndexOutcome.FINISHED else "scan completed with incomplete coverage",
        )

    def _terminal_result(
        self,
        registration: RootRegistration,
        scan_id: int,
        status: str,
    ) -> IndexRunResult:
        if status == "complete":
            outcome = IndexOutcome.FINISHED
        elif status == "cancelled":
            outcome = IndexOutcome.CANCELLED
        else:
            outcome = IndexOutcome.PARTIAL
        return IndexRunResult(
            scan_id=scan_id,
            root_id=registration.root_id,
            volume_id=registration.volume_id,
            outcome=outcome,
            catalog_status=status,
            coverage=None,
            files_observed=0,
            changed_content=0,
            work_enqueued=0,
            errors_recorded=0,
        )

    def _result(
        self,
        registration: RootRegistration,
        scan_id: int,
        outcome: IndexOutcome,
        coverage: Optional[WalkCoverage],
        files_observed: int = 0,
        changed_content: int = 0,
        work_enqueued: int = 0,
        errors_recorded: int = 0,
        reason: str = "",
    ) -> IndexRunResult:
        return IndexRunResult(
            scan_id=scan_id,
            root_id=registration.root_id,
            volume_id=registration.volume_id,
            outcome=outcome,
            catalog_status=self.catalog.get_scan(scan_id)["status"],
            coverage=coverage,
            files_observed=files_observed,
            changed_content=changed_content,
            work_enqueued=work_enqueued,
            errors_recorded=errors_recorded,
            reason=reason,
        )

    def page_files(
        self,
        after_id: int = 0,
        limit: int = 100,
        root_id: Optional[int] = None,
        verify_current: bool = True,
    ) -> CatalogFilePage:
        """Return a keyset page of current catalog entries as ``core.fs.File`` objects."""

        rows = self.catalog.page_paths(
            after_id=after_id,
            limit=limit,
            root_id=root_id,
            states=("active",),
        )
        files = []
        for row in rows:
            path = Path(row["display_path"])
            if verify_current:
                try:
                    snapshot = self._snapshot(path)
                except (CatalogIndexError, FileIdentityError, OSError) as error:
                    raise CatalogPageChangedError(path, str(error)) from error
                expected_change_token = bytes(row["change_token"]) if row["change_token"] is not None else None
                if snapshot.size != row["size"]:
                    raise CatalogPageChangedError(path, "size differs from the catalog")
                if snapshot.mtime_ns != row["mtime_ns"]:
                    raise CatalogPageChangedError(path, "mtime differs from the catalog")
                if snapshot.change_token != expected_change_token:
                    raise CatalogPageChangedError(path, "change token differs from the catalog")
                expected_identity_token = bytes(row["native_file_id"]) if row["native_file_id"] is not None else None
                if expected_identity_token is not None and snapshot.identity_token != expected_identity_token:
                    raise CatalogPageChangedError(path, "physical identity differs from the catalog")
            files.append(fs.File(path))
        next_after_id = rows[-1]["path_id"] if rows else after_id
        return CatalogFilePage(tuple(files), tuple(rows), next_after_id)

    def iter_files(
        self,
        page_size: int = 100,
        root_id: Optional[int] = None,
        verify_current: bool = True,
    ) -> Iterator[fs.File]:
        after_id = 0
        while True:
            page = self.page_files(
                after_id=after_id,
                limit=page_size,
                root_id=root_id,
                verify_current=verify_current,
            )
            if not page.rows:
                return
            for file in page.files:
                yield file
            after_id = page.next_after_id


__all__ = [
    "CatalogFilePage",
    "CatalogIndexError",
    "CatalogIndexer",
    "CatalogPageChangedError",
    "FileChangedDuringIndexing",
    "FileSnapshot",
    "IndexOutcome",
    "IndexRunResult",
    "RootRegistration",
]
