# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import hashlib
import json
import os

from pathlib import Path

import pytest

from core import fs
from core.catalog import MAX_WORK_ITEM_PAYLOAD_BYTES, Catalog
from core.catalog_indexer import CatalogIndexer, IndexOutcome
from core.catalog_worker import CatalogWorker, FullDigestCollision, WorkerOutcome
from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
)


def create_catalog_scan(tmp_path, file_contents, analysis_kinds=("exact_hash",)):
    root = tmp_path / "library"
    root.mkdir()
    paths = []
    for index, content in enumerate(file_contents):
        path = root / "file-{}.bin".format(index)
        path.write_bytes(content)
        paths.append(path)
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    indexer = CatalogIndexer(catalog, root, analysis_kinds=analysis_kinds)
    scan = indexer.run()
    return catalog, indexer, scan, paths


def test_worker_hashes_stable_generation_and_completes_work(tmp_path):
    content = b"stable content" * 100
    catalog, indexer, scan, _paths = create_catalog_scan(tmp_path, [content])
    worker = CatalogWorker(catalog, owner="worker")

    result = worker.run_batch(scan_id=scan.scan_id, limit=10)

    assert result.outcome == WorkerOutcome.FINISHED
    assert result.claimed == result.completed == 1
    assert result.retried == result.failed == 0
    work = catalog.page_work_items(scan_id=scan.scan_id)
    assert work[0]["status"] == "complete"
    sha256 = catalog.get_artifact(
        work[0]["content_version_id"],
        "full_hash",
        "sha256",
        "1",
    )
    assert bytes(sha256["value"]) == hashlib.sha256(content).digest()
    assert (
        catalog.get_artifact(
            work[0]["content_version_id"],
            "full_hash",
            fs.HASH_ALGORITHM,
            "1",
        )
        is not None
    )
    assert indexer.finalize_scan(scan.scan_id).outcome == IndexOutcome.FINISHED
    catalog.close()


def test_expired_work_lease_is_reclaimed(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(tmp_path, [b"lease"])
    original = catalog.claim_work_items(
        "dead-worker",
        scan_id=scan.scan_id,
        limit=1,
        lease_seconds=1,
        now=1,
    )
    assert original[0]["attempts"] == 1

    result = CatalogWorker(catalog, owner="replacement", clock=lambda: 10).run_batch(
        scan_id=scan.scan_id,
        limit=1,
    )

    assert result.outcome == WorkerOutcome.FINISHED
    assert result.resumed_expired == 1
    work = catalog.page_work_items(scan_id=scan.scan_id)[0]
    assert work["status"] == "complete"
    assert work["attempts"] == 2
    catalog.close()


def test_worker_rejects_database_payload_before_materializing_oversized_text(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(tmp_path, [b"payload guard"])
    work_item = catalog.page_work_items(scan_id=scan.scan_id)[0]
    catalog._connection.execute(
        """
        UPDATE work_items
        SET payload_json = CAST(zeroblob(?) AS TEXT)
        WHERE id = ?
        """,
        (MAX_WORK_ITEM_PAYLOAD_BYTES + 1, work_item["id"]),
    )

    result = CatalogWorker(catalog, owner="worker", max_attempts=1).run_batch(
        scan_id=scan.scan_id,
        limit=1,
    )

    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.failed == 1
    status = catalog._connection.execute(
        "SELECT status, last_error FROM work_items WHERE id = ?",
        (work_item["id"],),
    ).fetchone()
    assert status["status"] == "failed"
    assert "exceeds the {}-byte limit".format(MAX_WORK_ITEM_PAYLOAD_BYTES) in status["last_error"]
    catalog.close()


def test_worker_structurally_preflights_bounded_payload_before_json_loads(tmp_path):
    catalog, _indexer, scan, paths = create_catalog_scan(tmp_path, [b"payload depth guard"])
    work_item = catalog.page_work_items(scan_id=scan.scan_id)[0]
    nested_path = json.dumps(str(paths[0]))
    payload = '{"path":' + "[" * 9 + nested_path + "]" * 9 + "}"
    catalog._connection.execute(
        "UPDATE work_items SET payload_json = ? WHERE id = ?",
        (payload, work_item["id"]),
    )

    result = CatalogWorker(catalog, owner="worker", max_attempts=1).run_batch(
        scan_id=scan.scan_id,
        limit=1,
    )

    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.failed == 1
    status = catalog._connection.execute(
        "SELECT status, last_error FROM work_items WHERE id = ?",
        (work_item["id"],),
    ).fetchone()
    assert status["status"] == "failed"
    assert "invalid JSON" in status["last_error"]
    catalog.close()


def test_changed_or_missing_file_retries_only_to_limit(tmp_path):
    catalog, _indexer, scan, paths = create_catalog_scan(tmp_path, [b"will disappear"])
    paths[0].unlink()
    worker = CatalogWorker(catalog, owner="worker", max_attempts=2)

    first = worker.run_batch(scan_id=scan.scan_id, limit=1)
    second = worker.run_batch(scan_id=scan.scan_id, limit=1)

    assert first.outcome == WorkerOutcome.PARTIAL
    assert first.retried == 1
    assert second.outcome == WorkerOutcome.PARTIAL
    assert second.failed == 1
    work = catalog.page_work_items(scan_id=scan.scan_id)[0]
    assert work["status"] == "failed"
    assert work["attempts"] == 2
    assert "ContentGenerationChanged" in work["last_error"]
    catalog.close()


def test_generation_change_during_streaming_hash_persists_no_artifact(tmp_path):
    catalog, _indexer, scan, paths = create_catalog_scan(tmp_path, [b"0123456789"])
    calls = 0

    def mutate_during_hash():
        nonlocal calls
        calls += 1
        if calls == 2:
            current = paths[0].stat()
            os.utime(
                paths[0],
                ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
            )
        return False

    worker = CatalogWorker(catalog, owner="worker", chunk_size=4)
    result = worker.run_batch(
        scan_id=scan.scan_id,
        limit=1,
        cancel_check=mutate_during_hash,
    )

    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.retried == 1
    work = catalog.page_work_items(scan_id=scan.scan_id)[0]
    assert work["status"] == "pending"
    assert (
        catalog.get_artifact(
            work["content_version_id"],
            "full_hash",
            "sha256",
            "1",
        )
        is None
    )
    catalog.close()


def test_generation_token_change_during_hash_fails_closed(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(tmp_path, [b"stable bytes"])
    calls = 0

    def changing_generation(path, **kwargs):
        nonlocal calls
        calls += 1
        observed = get_file_generation_token(path, **kwargs)
        if calls > 1:
            return FileGenerationToken(observed.namespace, observed.value + 1, observed.version)
        return observed

    result = CatalogWorker(
        catalog,
        owner="worker",
        generation_getter=changing_generation,
    ).run_batch(scan_id=scan.scan_id, limit=1)

    assert calls == 2
    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.retried == 1
    assert "ContentGenerationChanged" in catalog.page_work_items(scan_id=scan.scan_id)[0]["last_error"]
    catalog.close()


def test_generation_token_failure_prevents_artifact_reuse(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(tmp_path, [b"stable bytes"])

    def failing_generation(path, **kwargs):
        raise FileGenerationError(path, "injected generation failure")

    result = CatalogWorker(
        catalog,
        owner="worker",
        generation_getter=failing_generation,
    ).run_batch(scan_id=scan.scan_id, limit=1)

    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.retried == 1
    work = catalog.page_work_items(scan_id=scan.scan_id)[0]
    assert "ContentGenerationChanged" in work["last_error"]
    assert catalog.get_artifact(work["content_version_id"], "full_hash", "sha256", "1") is None
    catalog.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows USN generation integration")
def test_windows_restored_mtime_content_edit_is_rehashed_and_reported_modified(tmp_path):
    original = b"first-generation"
    replacement = b"other-generation"
    assert len(original) == len(replacement)
    catalog, indexer, first_scan, paths = create_catalog_scan(tmp_path, [original])
    worker = CatalogWorker(catalog, owner="worker")
    assert worker.run_batch(scan_id=first_scan.scan_id).completed == 1
    assert indexer.finalize_scan(first_scan.scan_id).outcome == IndexOutcome.FINISHED
    first_row = catalog.page_paths(root_id=first_scan.root_id)[0]
    first_version = first_row["current_content_version_id"]
    first_stat = os.stat(paths[0], follow_symlinks=False)

    paths[0].write_bytes(replacement)
    os.utime(paths[0], ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    restored_stat = os.stat(paths[0], follow_symlinks=False)
    assert restored_stat.st_size == first_stat.st_size
    assert restored_stat.st_mtime_ns == first_stat.st_mtime_ns

    second_scan = indexer.run()

    assert second_scan.changed_content == 1
    assert second_scan.work_enqueued == 1
    assert worker.run_batch(scan_id=second_scan.scan_id).completed == 1
    assert indexer.finalize_scan(second_scan.scan_id).outcome == IndexOutcome.FINISHED
    second_row = catalog.page_paths(root_id=second_scan.root_id)[0]
    second_version = second_row["current_content_version_id"]
    assert second_version != first_version
    artifact = catalog.get_artifact(second_version, "full_hash", "sha256", "1")
    assert bytes(artifact["value"]) == hashlib.sha256(replacement).digest()
    changes = catalog.page_scan_changes(
        first_scan.scan_id,
        second_scan.scan_id,
        (first_scan.root_id,),
    )
    assert [row["change_type"] for row in changes] == ["modified"]
    catalog.close()


def test_batch_limit_and_cancellation_are_bounded(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(
        tmp_path,
        [b"one", b"two", b"three"],
    )
    worker = CatalogWorker(catalog, owner="worker")

    cancelled = worker.run_batch(scan_id=scan.scan_id, limit=2, cancel_check=lambda: True)
    first = worker.run_batch(scan_id=scan.scan_id, limit=2)
    second = worker.run_batch(scan_id=scan.scan_id, limit=2)

    assert cancelled.outcome == WorkerOutcome.CANCELLED
    assert cancelled.claimed == 0
    assert first.claimed == first.completed == 2
    assert second.claimed == second.completed == 1
    catalog.close()


def test_rename_revalidates_generation_before_hydrating_filesdb(tmp_path):
    cache = fs.FilesDB()
    cache.connect(tmp_path / "files-cache.sqlite3")
    catalog, indexer, first_scan, paths = create_catalog_scan(tmp_path, [b"rename me"])
    worker = CatalogWorker(catalog, owner="worker", files_db=cache)
    assert worker.run_batch(scan_id=first_scan.scan_id).completed == 1
    assert indexer.finalize_scan(first_scan.scan_id).outcome == IndexOutcome.FINISHED
    first_row = catalog.page_paths(root_id=first_scan.root_id)[0]
    content_version_id = first_row["current_content_version_id"]

    renamed = paths[0].with_name("renamed.bin")
    paths[0].rename(renamed)
    warm_scan = indexer.run()

    assert warm_scan.outcome == IndexOutcome.PARTIAL
    assert warm_scan.changed_content == 1
    assert warm_scan.work_enqueued == 1
    assert worker.run_batch(scan_id=warm_scan.scan_id).outcome == WorkerOutcome.FINISHED
    assert indexer.finalize_scan(warm_scan.scan_id).outcome == IndexOutcome.FINISHED
    current = catalog.page_paths(root_id=warm_scan.root_id)
    active = [row for row in current if row["path_state"] == "active"]
    assert active[0]["current_content_version_id"] != content_version_id
    content_version_id = active[0]["current_content_version_id"]
    file = fs.File(renamed)
    assert worker.hydrate_file(file, content_version_id)
    expected = catalog.get_artifact(
        content_version_id,
        "full_hash",
        fs.HASH_ALGORITHM,
        "1",
    )
    assert file.digest == bytes(expected["value"])
    cache.close()
    catalog.close()


def test_verified_exact_projection_is_linear_in_group_size(tmp_path):
    duplicate = b"byte-identical"
    catalog, indexer, scan, _paths = create_catalog_scan(
        tmp_path,
        [duplicate, duplicate, duplicate, b"different"],
    )
    worker = CatalogWorker(catalog, owner="worker")
    assert worker.run_batch(scan_id=scan.scan_id, limit=10).completed == 4
    assert indexer.finalize_scan(scan.scan_id).outcome == IndexOutcome.FINISHED

    page = worker.page_verified_exact_groups(
        limit=10,
        root_ids=(scan.root_id,),
    )

    assert len(page.groups) == 1
    assert page.groups[0].size == len(duplicate)
    assert len(page.groups[0].files) == 3
    assert len(page.groups[0].verification_ids) == 2
    assert page.comparisons == 2
    verifications = catalog._connection.execute(
        "SELECT COUNT(*) FROM verification_records WHERE state = 'verified'"
    ).fetchone()[0]
    assert verifications == 2
    catalog.close()


def test_exact_projection_counts_and_sql_caps_never_split_an_oversized_group(
    tmp_path,
):
    large_payload = b"a"
    small_payload = b"bb"
    catalog, indexer, scan, _paths = create_catalog_scan(
        tmp_path,
        [large_payload] * 5 + [small_payload] * 2,
    )
    worker = CatalogWorker(catalog, owner="worker")
    assert worker.run_batch(scan_id=scan.scan_id, limit=10).completed == 7
    assert indexer.finalize_scan(scan.scan_id).outcome == IndexOutcome.FINISHED

    counts = catalog.exact_digest_projection_counts(root_ids=(scan.root_id,))
    bounded_rows = catalog.page_exact_digest_candidates(
        limit=10,
        root_ids=(scan.root_id,),
        max_rows=4,
        max_group_members=4,
    )
    boundary_rows = catalog.page_exact_digest_candidates(
        limit=10,
        root_ids=(scan.root_id,),
        max_rows=7,
        max_group_members=5,
    )

    assert counts.group_count == 2
    assert counts.file_count == 7
    assert counts.max_group_members == 5
    assert len(bounded_rows) == 2
    assert {row["size"] for row in bounded_rows} == {len(small_payload)}
    assert len(boundary_rows) == 7
    grouped = {}
    for row in boundary_rows:
        grouped.setdefault((row["size"], bytes(row["full_digest"])), 0)
        grouped[(row["size"], bytes(row["full_digest"]))] += 1
    assert sorted(grouped.values()) == [2, 5]
    catalog.close()


def test_full_digest_collision_rejects_the_entire_projection_bucket(tmp_path):
    catalog, indexer, scan, _paths = create_catalog_scan(
        tmp_path,
        [b"aaa", b"bbb", b"bbb"],
    )
    worker = CatalogWorker(catalog, owner="worker")
    assert worker.run_batch(scan_id=scan.scan_id, limit=10).completed == 3
    for work in catalog.page_work_items(scan_id=scan.scan_id):
        catalog.put_artifact(
            work["content_version_id"],
            "full_hash",
            "sha256",
            "1",
            b"forced-sha256-collision",
            verification_level="full",
        )
    assert indexer.finalize_scan(scan.scan_id).outcome == IndexOutcome.FINISHED

    with pytest.raises(FullDigestCollision):
        worker.page_verified_exact_groups(
            limit=10,
            root_ids=(scan.root_id,),
        )

    states = [row["state"] for row in catalog._connection.execute("SELECT state FROM verification_records ORDER BY id")]
    assert states == ["invalidated"]
    catalog.close()


def test_backup_is_integrity_checked_and_never_overwrites(tmp_path):
    catalog, _indexer, _scan, _paths = create_catalog_scan(tmp_path, [])
    destination = tmp_path / "catalog-backup.sqlite3"

    result = catalog.backup_to(destination)

    assert Path(result) == destination
    backup = Catalog(destination)
    assert backup.verify_integrity()
    backup.close()
    with pytest.raises(FileExistsError):
        catalog.backup_to(destination)
    catalog.close()


def test_unsupported_work_kind_fails_without_retry(tmp_path):
    catalog, _indexer, scan, _paths = create_catalog_scan(
        tmp_path,
        [b"unknown"],
        analysis_kinds=("unknown-analysis",),
    )

    result = CatalogWorker(catalog, owner="worker").run_batch(scan_id=scan.scan_id)

    assert result.outcome == WorkerOutcome.PARTIAL
    assert result.failed == 1
    work = catalog.page_work_items(scan_id=scan.scan_id)[0]
    assert work["status"] == "failed"
    assert work["attempts"] == 1
    catalog.close()
