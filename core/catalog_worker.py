# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Bounded, durable analysis workers for catalog content generations."""

import hashlib
import json
import os
import stat
import threading
import time
import uuid

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import fs
from core.catalog import (
    MAX_WORK_ITEM_PAYLOAD_BYTES,
    Catalog,
    CatalogStateError,
)
from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    get_file_identity,
)
from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
)
from core.safe_walk import is_reparse_point
from core.safe_json import JsonStructuralLimits, preflight_json_structure

CATALOG_WORK_PAYLOAD_JSON_LIMITS = JsonStructuralLimits(
    max_depth=8,
    max_container_entries=32,
    max_total_nodes=128,
    max_scalar_tokens=128,
    max_total_string_chars=64 * 1024,
    max_string_chars=32 * 1024,
)


class CatalogWorkerError(Exception):
    """Base class for durable catalog worker failures."""


class CatalogWorkerBusy(CatalogWorkerError):
    """The same worker instance is already processing a batch."""


class UnsupportedWorkKind(CatalogWorkerError):
    """A claimed work kind has no safe analyzer."""


class ContentGenerationChanged(CatalogWorkerError):
    """The current path no longer proves the cataloged content generation."""


class FullDigestCollision(CatalogWorkerError):
    """Equal full digests failed the mandatory final byte comparison."""


class WorkerCancelled(CatalogWorkerError):
    """Cooperative cancellation interrupted a bounded operation."""


class WorkerOutcome(Enum):
    FINISHED = "finished"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    IDLE = "idle"


@dataclass(frozen=True)
class GenerationSnapshot:
    path: Path
    size: int
    mtime_ns: int
    generation_token: FileGenerationToken
    identity: FileIdentity
    identity_token: bytes
    file_snapshot: fs.FileSnapshot

    @property
    def change_token(self) -> bytes:
        return self.generation_token.encoded


@dataclass(frozen=True)
class HashArtifacts:
    sha256: bytes
    fast_digest: bytes
    bytes_read: int
    snapshot: GenerationSnapshot


@dataclass(frozen=True)
class WorkerBatchResult:
    outcome: WorkerOutcome
    claimed: int
    completed: int
    retried: int
    failed: int
    resumed_expired: int
    errors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExactCatalogFile:
    content_version_id: int
    physical_file_id: int
    path_id: int
    path: Path
    file: fs.File


@dataclass(frozen=True)
class VerifiedExactGroup:
    size: int
    full_digest: bytes
    files: Tuple[ExactCatalogFile, ...]
    verification_ids: Tuple[int, ...]


@dataclass(frozen=True)
class VerifiedExactPage:
    groups: Tuple[VerifiedExactGroup, ...]
    next_after_size: int
    next_after_digest: bytes
    comparisons: int


CancelCheck = Callable[[], bool]
IdentityGetter = Callable[..., FileIdentity]
GenerationGetter = Callable[..., FileGenerationToken]


class CatalogWorker:
    """Claim and execute catalog work one item at a time.

    Claiming one item per iteration keeps cancellation bounded and avoids
    stranding the rest of a claimed page behind a live lease.
    """

    FULL_HASH_KINDS = frozenset({"exact_hash", "full_hash", "sha256"})

    def __init__(
        self,
        catalog: Catalog,
        owner: Optional[str] = None,
        max_attempts: int = 3,
        chunk_size: int = fs.CHUNK_SIZE,
        identity_getter: IdentityGetter = get_file_identity,
        generation_getter: GenerationGetter = get_file_generation_token,
        files_db: Optional[fs.FilesDB] = None,
        clock: Callable[[], float] = time.time,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least one")
        self.catalog = catalog
        self.owner = owner or "catalog-worker-{}".format(uuid.uuid4().hex)
        self.max_attempts = max_attempts
        self.chunk_size = chunk_size
        self.identity_getter = identity_getter
        self.generation_getter = generation_getter
        self.files_db = files_db if files_db is not None else fs.filesdb
        self.clock = clock
        self._run_lock = threading.Lock()

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

    @staticmethod
    def _blob(value: Any) -> Optional[bytes]:
        if value is None:
            return None
        return bytes(value)

    def _snapshot_path(self, path: Path) -> GenerationSnapshot:
        try:
            stat_result = os.stat(str(path), follow_symlinks=False)
        except OSError as error:
            raise ContentGenerationChanged("Could not stat '{}': {}".format(path, error)) from error
        if stat.S_ISLNK(stat_result.st_mode):
            raise ContentGenerationChanged("'{}' became a symbolic link".format(path))
        if is_reparse_point(stat_result):
            raise ContentGenerationChanged("'{}' became a reparse point".format(path))
        if not stat.S_ISREG(stat_result.st_mode):
            raise ContentGenerationChanged("'{}' is not a regular file".format(path))
        try:
            identity = self.identity_getter(path, follow_symlinks=False, stat_result=stat_result)
        except (FileIdentityError, OSError) as error:
            raise ContentGenerationChanged("Could not identify '{}': {}".format(path, error)) from error
        try:
            generation_token = self.generation_getter(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
                expected_identity=identity,
            )
        except (FileGenerationError, OSError) as error:
            raise ContentGenerationChanged(
                "Could not prove content generation for '{}': {}".format(path, error)
            ) from error
        if not isinstance(generation_token, FileGenerationToken):
            raise ContentGenerationChanged("Generation getter returned an invalid token for '{}'".format(path))
        return GenerationSnapshot(
            path=path,
            size=int(stat_result.st_size),
            mtime_ns=int(stat_result.st_mtime_ns),
            generation_token=generation_token,
            identity=identity,
            identity_token=self._identity_token(identity),
            file_snapshot=fs.FileSnapshot.from_stat(stat_result, generation_token),
        )

    def _validate_context(self, snapshot: GenerationSnapshot, context: Any) -> None:
        if context is None or context["display_path"] is None:
            raise ContentGenerationChanged("Content generation has no current active path")
        if context["current_content_version_id"] != context["content_version_id"]:
            raise ContentGenerationChanged("Content generation is no longer current")
        if snapshot.size != context["size"]:
            raise ContentGenerationChanged("File size differs from the catalog generation")
        if snapshot.mtime_ns != context["mtime_ns"]:
            raise ContentGenerationChanged("File mtime differs from the catalog generation")
        expected_change_token = self._blob(context["change_token"])
        if snapshot.change_token != expected_change_token:
            raise ContentGenerationChanged("File change token differs from the catalog generation")
        expected_identity = self._blob(context["native_file_id"])
        if expected_identity is not None and snapshot.identity_token != expected_identity:
            raise ContentGenerationChanged("File identity differs from the catalog generation")

    def _open_no_follow(self, path: Path):
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        return os.fdopen(descriptor, "rb", closefd=True)

    def _stable_hash(
        self,
        context: Any,
        path: Optional[Path] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> HashArtifacts:
        path = Path(context["display_path"]) if path is None else Path(path)
        before = self._snapshot_path(path)
        self._validate_context(before, context)
        sha256 = hashlib.sha256()
        fast_hash = fs.hasher()
        bytes_read = 0
        with self._open_no_follow(path) as stream:
            handle_before = fs.FileSnapshot.from_file(stream, path=path)
            if not before.file_snapshot.same_content_generation(handle_before):
                raise ContentGenerationChanged("Opened handle does not match the catalog path")
            while True:
                if cancel_check is not None and cancel_check():
                    raise WorkerCancelled("cancel requested while hashing '{}'".format(path))
                block = stream.read(self.chunk_size)
                if not block:
                    break
                sha256.update(block)
                fast_hash.update(block)
                bytes_read += len(block)
            handle_after = fs.FileSnapshot.from_file(stream, path=path)
        if not handle_before.same_content_generation(handle_after):
            raise ContentGenerationChanged("File changed while hashing '{}'".format(path))
        if bytes_read != handle_before.size:
            raise ContentGenerationChanged("Unexpected end of file while hashing '{}'".format(path))
        after = self._snapshot_path(path)
        if before != after:
            raise ContentGenerationChanged("Path changed while hashing '{}'".format(path))
        self._validate_context(after, context)
        return HashArtifacts(sha256.digest(), fast_hash.digest(), bytes_read, before)

    def _process_full_hash(
        self,
        work_item: Any,
        context: Any,
        path: Path,
        cancel_check: Optional[CancelCheck],
    ) -> None:
        artifacts = self._stable_hash(context, path=path, cancel_check=cancel_check)
        with self.catalog.transaction():
            current_work = self.catalog.get_work_item(work_item["id"])
            if current_work["status"] != "in_progress" or current_work["lease_owner"] != self.owner:
                raise CatalogStateError("Work item lease changed before artifact commit")
            current_context = self.catalog.get_content_context(work_item["content_version_id"])
            if (
                current_context is None
                or current_context["current_content_version_id"] != work_item["content_version_id"]
            ):
                raise ContentGenerationChanged("Catalog generation changed before artifact commit")
            current_work_payload = current_work["payload_json"]
            if current_work_payload != work_item["payload_json"]:
                raise ContentGenerationChanged("Catalog work path changed before artifact commit")
            self.catalog.put_artifact(
                work_item["content_version_id"],
                "full_hash",
                "sha256",
                "1",
                artifacts.sha256,
                verification_level="full",
                now=self.clock(),
            )
            self.catalog.put_artifact(
                work_item["content_version_id"],
                "full_hash",
                fs.HASH_ALGORITHM,
                "1",
                artifacts.fast_digest,
                verification_level="full",
                now=self.clock(),
            )
            self.catalog.complete_work_item(work_item["id"], owner=self.owner, now=self.clock())

        if getattr(self.files_db, "conn", None) is not None:
            try:
                self.files_db.put(
                    artifacts.snapshot.path,
                    "digest",
                    artifacts.fast_digest,
                    artifacts.snapshot.file_snapshot,
                )
            except fs.FileChangedError:
                # The durable artifact still describes the catalog generation.
                # A path-cache race must only turn into a cache miss.
                pass

    def _process_claimed(
        self,
        work_item: Any,
        cancel_check: Optional[CancelCheck],
    ) -> None:
        if work_item["kind"] not in self.FULL_HASH_KINDS:
            raise UnsupportedWorkKind("Unsupported catalog work kind {!r}".format(work_item["kind"]))
        context = self.catalog.get_content_context(work_item["content_version_id"])
        if context is None:
            raise ContentGenerationChanged("Unknown content generation {}".format(work_item["content_version_id"]))
        try:
            payload_json_valid = bool(work_item["payload_json_valid"])
        except (IndexError, KeyError):
            payload_json_valid = True
        if not payload_json_valid:
            raise ContentGenerationChanged(
                "Catalog work payload is not text or exceeds the {}-byte limit".format(MAX_WORK_ITEM_PAYLOAD_BYTES)
            )
        payload_json = work_item["payload_json"]
        try:
            if payload_json is None:
                payload = {}
            else:
                if not isinstance(payload_json, str):
                    raise TypeError("Catalog work payload is not text")
                if len(payload_json) > MAX_WORK_ITEM_PAYLOAD_BYTES:
                    raise ValueError("Catalog work payload exceeds its character limit")
                if len(payload_json.encode("utf-8")) > MAX_WORK_ITEM_PAYLOAD_BYTES:
                    raise ValueError("Catalog work payload exceeds its byte limit")
                preflight_json_structure(
                    payload_json,
                    limits=CATALOG_WORK_PAYLOAD_JSON_LIMITS,
                    label="Catalog work payload",
                )
                payload = json.loads(payload_json)
        except (TypeError, ValueError) as error:
            raise ContentGenerationChanged("Catalog work payload is invalid JSON") from error
        work_path = payload.get("path")
        if not isinstance(work_path, str) or not work_path or len(work_path) > 32768 or "\0" in work_path:
            raise ContentGenerationChanged("Catalog work payload has no safe path")
        work_path = Path(work_path)
        if not work_path.is_absolute():
            raise ContentGenerationChanged("Catalog work payload path is not absolute")
        self._process_full_hash(work_item, context, work_path, cancel_check)

    def run_batch(
        self,
        scan_id: Optional[int] = None,
        limit: int = 100,
        lease_seconds: float = 300,
        cancel_check: Optional[CancelCheck] = None,
    ) -> WorkerBatchResult:
        """Process at most ``limit`` items, preserving explicit retry state."""

        if limit < 1:
            raise ValueError("limit must be at least one")
        if not self._run_lock.acquire(blocking=False):
            raise CatalogWorkerBusy("worker instance is already running")
        claimed = completed = retried = failed = resumed_expired = 0
        errors: List[str] = []
        try:
            if cancel_check is not None and cancel_check():
                return WorkerBatchResult(WorkerOutcome.CANCELLED, 0, 0, 0, 0, 0)
            while claimed < limit:
                rows = self.catalog.claim_work_items(
                    self.owner,
                    limit=1,
                    lease_seconds=lease_seconds,
                    scan_id=scan_id,
                    now=self.clock(),
                )
                if not rows:
                    break
                work_item = rows[0]
                claimed += 1
                if work_item["attempts"] > 1:
                    resumed_expired += 1
                try:
                    self._process_claimed(work_item, cancel_check)
                except WorkerCancelled as error:
                    retry = work_item["attempts"] < self.max_attempts
                    self.catalog.fail_work_item(
                        work_item["id"],
                        str(error),
                        retry=retry,
                        owner=self.owner,
                        now=self.clock(),
                    )
                    retried += int(retry)
                    failed += int(not retry)
                    errors.append(str(error))
                    return WorkerBatchResult(
                        WorkerOutcome.CANCELLED,
                        claimed,
                        completed,
                        retried,
                        failed,
                        resumed_expired,
                        tuple(errors),
                    )
                except Exception as error:
                    retry = work_item["attempts"] < self.max_attempts and not isinstance(error, UnsupportedWorkKind)
                    self.catalog.fail_work_item(
                        work_item["id"],
                        "{}: {}".format(type(error).__name__, error),
                        retry=retry,
                        owner=self.owner,
                        now=self.clock(),
                    )
                    retried += int(retry)
                    failed += int(not retry)
                    errors.append("{}: {}".format(type(error).__name__, error))
                else:
                    completed += 1
            if failed or retried:
                outcome = WorkerOutcome.PARTIAL
            elif claimed:
                outcome = WorkerOutcome.FINISHED
            else:
                outcome = WorkerOutcome.IDLE
            return WorkerBatchResult(
                outcome,
                claimed,
                completed,
                retried,
                failed,
                resumed_expired,
                tuple(errors),
            )
        finally:
            self._run_lock.release()

    def hydrate_file(self, file: fs.File, content_version_id: int) -> bool:
        """Hydrate dupeGuru's path cache from a catalog artifact without rehashing."""

        context = self.catalog.get_content_context(content_version_id)
        if context is None or context["display_path"] is None:
            return False
        if os.path.normcase(os.path.abspath(str(file.path))) != os.path.normcase(
            os.path.abspath(context["display_path"])
        ):
            return False
        artifact = self.catalog.get_artifact(
            content_version_id,
            "full_hash",
            fs.HASH_ALGORITHM,
            "1",
        )
        if artifact is None:
            return False
        before = self._snapshot_path(Path(file.path))
        self._validate_context(before, context)
        digest = bytes(artifact["value"])
        if getattr(self.files_db, "conn", None) is not None:
            self.files_db.put(file.path, "digest", digest, before.file_snapshot)
        after = self._snapshot_path(Path(file.path))
        if before != after:
            raise ContentGenerationChanged("Path changed while hydrating '{}'".format(file.path))
        file.size = before.size
        file.mtime = before.mtime_ns / 1_000_000_000
        file.prime_exact_digest("digest", digest, after.file_snapshot)
        return True

    def page_verified_exact_groups(
        self,
        after_size: int = -1,
        after_digest: bytes = b"",
        limit: int = 100,
        root_ids: Optional[Tuple[int, ...]] = None,
        max_rows: Optional[int] = None,
        max_group_members: Optional[int] = None,
    ) -> VerifiedExactPage:
        """Project verified exact groups with one representative comparison per member."""

        if not root_ids:
            raise ValueError("verified exact projection requires selected root IDs")
        rows = self.catalog.page_exact_digest_candidates(
            after_size=after_size,
            after_digest=after_digest,
            limit=limit,
            algorithm="sha256",
            algorithm_version="1",
            root_ids=root_ids,
            max_rows=max_rows,
            max_group_members=max_group_members,
        )
        grouped: Dict[Tuple[int, bytes], List[Any]] = {}
        for row in rows:
            key = (row["size"], bytes(row["full_digest"]))
            grouped.setdefault(key, []).append(row)

        verified_groups = []
        comparisons = 0
        for (size, digest), candidates in grouped.items():
            bucket_content_version_ids = tuple(row["content_version_id"] for row in candidates)
            reference_row = candidates[0]
            reference_context = self.catalog.get_content_context(reference_row["content_version_id"])
            if reference_context is None:
                continue
            reference_path = Path(reference_row["display_path"])
            reference = ExactCatalogFile(
                reference_row["content_version_id"],
                reference_row["physical_file_id"],
                reference_row["path_id"],
                reference_path,
                fs.File(reference_path),
            )
            verified = [reference]
            verification_ids = []
            for candidate_row in candidates[1:]:
                candidate_context = self.catalog.get_content_context(candidate_row["content_version_id"])
                if candidate_context is None:
                    continue
                candidate_path = Path(candidate_row["display_path"])
                reference_before = self._snapshot_path(reference_path)
                candidate_before = self._snapshot_path(candidate_path)
                self._validate_context(reference_before, reference_context)
                self._validate_context(candidate_before, candidate_context)
                candidate_file = fs.File(candidate_path)
                try:
                    evidence = reference.file.compare_bytes_with_sha256(candidate_file)
                except fs.FileChangedError as error:
                    raise ContentGenerationChanged("File changed during final byte comparison") from error
                comparisons += 1
                reference_after = self._snapshot_path(reference_path)
                candidate_after = self._snapshot_path(candidate_path)
                if reference_before != reference_after or candidate_before != candidate_after:
                    raise ContentGenerationChanged("File changed during final byte comparison")
                if evidence is None:
                    self._retire_invalid_exact_bucket(
                        reference.content_version_id,
                        candidate_row["content_version_id"],
                        bucket_content_version_ids,
                        digest,
                    )
                    raise FullDigestCollision(
                        "SHA-256 bucket for {}-byte files contains unequal bytes: "
                        "'{}' and '{}'".format(
                            size,
                            reference_path,
                            candidate_path,
                        )
                    )
                if evidence.sha256_digest != digest:
                    self._retire_invalid_exact_bucket(
                        reference.content_version_id,
                        candidate_row["content_version_id"],
                        bucket_content_version_ids,
                        digest,
                    )
                    raise ContentGenerationChanged(
                        "Live bytes no longer match the catalog SHA-256 for "
                        "'{}' and '{}'".format(
                            reference_path,
                            candidate_path,
                        )
                    )
                verification_id = self.catalog.record_verification(
                    reference.content_version_id,
                    candidate_row["content_version_id"],
                    "sha256+byte-compare",
                    "1",
                    digest,
                    state="verified",
                    byte_compare_at=self.clock(),
                    now=self.clock(),
                )
                verified.append(
                    ExactCatalogFile(
                        candidate_row["content_version_id"],
                        candidate_row["physical_file_id"],
                        candidate_row["path_id"],
                        candidate_path,
                        candidate_file,
                    )
                )
                verification_ids.append(verification_id)
            if len(verified) > 1:
                verified_groups.append(
                    VerifiedExactGroup(
                        size,
                        digest,
                        tuple(verified),
                        tuple(verification_ids),
                    )
                )

        if grouped:
            next_after_size, next_after_digest = next(reversed(grouped))
        else:
            next_after_size, next_after_digest = after_size, after_digest
        return VerifiedExactPage(
            tuple(verified_groups),
            next_after_size,
            next_after_digest,
            comparisons,
        )

    def _retire_invalid_exact_bucket(
        self,
        first_content_version_id,
        second_content_version_id,
        bucket_content_version_ids,
        digest,
    ):
        """Atomically retire every artifact which selected an invalid bucket."""

        now = self.clock()
        try:
            with self.catalog.transaction():
                self.catalog.record_verification(
                    first_content_version_id,
                    second_content_version_id,
                    "sha256+byte-compare",
                    "1",
                    digest,
                    state="invalidated",
                    byte_compare_at=now,
                    now=now,
                )
                self.catalog.retire_content_evidence(
                    bucket_content_version_ids,
                    now=now,
                )
        except CatalogStateError as repair_error:
            raise ContentGenerationChanged(
                "Live bytes no longer support the catalog SHA-256 bucket; "
                "open the catalog writable and run a repair scan"
            ) from repair_error


__all__ = [
    "CatalogWorker",
    "CatalogWorkerBusy",
    "CatalogWorkerError",
    "ContentGenerationChanged",
    "FullDigestCollision",
    "ExactCatalogFile",
    "GenerationSnapshot",
    "HashArtifacts",
    "UnsupportedWorkKind",
    "VerifiedExactGroup",
    "VerifiedExactPage",
    "WorkerBatchResult",
    "WorkerCancelled",
    "WorkerOutcome",
]
