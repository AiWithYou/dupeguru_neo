# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os

from pathlib import Path

import pytest

from core import fs
from core.catalog import Catalog
from core.file_generation import FileGenerationError, FileGenerationToken
from core.catalog_indexer import (
    CatalogIndexer,
    CatalogPageChangedError,
    IndexOutcome,
)
from core.safe_walk import WalkCoverage, WalkEvent, WalkEventKind
from core.reserved_paths import RESERVED_INTERNAL_DIRECTORY_NAMES


def create_indexer(tmp_path, **kwargs):
    root_path = tmp_path / "library"
    root_path.mkdir()
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    return catalog, root_path, CatalogIndexer(catalog, root_path, **kwargs)


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES | {name.upper() for name in RESERVED_INTERNAL_DIRECTORY_NAMES}),
)
def test_indexer_rejects_an_internal_directory_as_root(tmp_path, reserved_name):
    root_path = tmp_path / reserved_name
    root_path.mkdir()
    catalog = Catalog(tmp_path / "catalog.sqlite3")

    with pytest.raises(ValueError, match="internal"):
        CatalogIndexer(catalog, root_path)

    catalog.close()


def finish_analysis_work(catalog, scan_id, owner="analysis-worker", now=1000):
    completed = 0
    while True:
        rows = catalog.claim_work_items(
            owner,
            scan_id=scan_id,
            limit=100,
            now=now,
        )
        if not rows:
            return completed
        for row in rows:
            catalog.complete_work_item(row["id"], owner=owner, now=now)
            completed += 1


def make_coverage(identity, **overrides):
    values = {
        "entries_seen": 0,
        "files": 0,
        "directories": 1,
        "pruned_directories": 0,
        "skipped_symlinks": 0,
        "skipped_reparse_points": 0,
        "skipped_mounts": 0,
        "skipped_cycles": 0,
        "skipped_outside_root": 0,
        "skipped_special_files": 0,
        "skipped_changed_directories": 0,
        "errors": 0,
        "identity_failures": 0,
        "high_confidence_identities": 1,
        "medium_confidence_identities": 0,
        "low_confidence_identities": 0,
        "identity_capabilities": (identity.capability,),
    }
    values.update(overrides)
    return WalkCoverage(**values)


def test_real_walk_registers_root_and_enqueues_only_new_content(tmp_path):
    catalog, root_path, indexer = create_indexer(
        tmp_path,
        analysis_kinds=("exact_hash", "thumbnail"),
    )
    (root_path / "one.bin").write_bytes(b"one")
    child = root_path / "child"
    child.mkdir()
    (child / "two.bin").write_bytes(b"two")

    first = indexer.run()

    assert first.outcome == IndexOutcome.PARTIAL
    assert first.catalog_status == "running"
    assert first.coverage is not None
    assert first.coverage.complete
    assert first.files_observed == 2
    assert first.changed_content == 2
    assert first.work_enqueued == 4
    assert catalog.scan_coverage(first.scan_id)["complete"] == 2
    assert catalog.scan_coverage(first.scan_id)["work_pending"] == 4
    assert catalog._connection.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 1
    assert catalog._connection.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1

    assert finish_analysis_work(catalog, first.scan_id) == 4
    finished = indexer.finalize_scan(first.scan_id)
    assert finished.outcome == IndexOutcome.FINISHED
    assert finished.catalog_status == "complete"

    first_rows = catalog.page_paths(root_id=first.root_id)
    first_content_ids = {row["display_path"]: row["current_content_version_id"] for row in first_rows}
    second = indexer.run()

    assert second.outcome == IndexOutcome.FINISHED
    assert second.catalog_status == "complete"
    assert second.files_observed == 2
    assert second.changed_content == 0
    assert second.work_enqueued == 0
    assert {
        row["display_path"]: row["current_content_version_id"] for row in catalog.page_paths(root_id=second.root_id)
    } == first_content_ids
    catalog.close()


def test_changed_file_is_the_only_content_reenqueued(tmp_path):
    catalog, root_path, indexer = create_indexer(tmp_path)
    first_path = root_path / "first.bin"
    second_path = root_path / "second.bin"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")

    initial = indexer.run()
    finish_analysis_work(catalog, initial.scan_id)
    assert indexer.finalize_scan(initial.scan_id).outcome == IndexOutcome.FINISHED

    first_path.write_bytes(b"first changed and longer")
    changed = indexer.run()

    assert changed.outcome == IndexOutcome.PARTIAL
    assert changed.files_observed == 2
    assert changed.changed_content == 1
    assert changed.work_enqueued == 1
    work = catalog.page_work_items(scan_id=changed.scan_id)
    assert len(work) == 1
    catalog.close()


def test_generation_token_change_reenqueues_unchanged_metadata(tmp_path):
    generation = {"value": 1}

    def generation_getter(path, **kwargs):
        return FileGenerationToken("test-generation", generation["value"])

    catalog, root_path, indexer = create_indexer(
        tmp_path,
        generation_getter=generation_getter,
    )
    path = root_path / "file.bin"
    path.write_bytes(b"same metadata")
    initial = indexer.run()
    finish_analysis_work(catalog, initial.scan_id)
    assert indexer.finalize_scan(initial.scan_id).outcome == IndexOutcome.FINISHED

    generation["value"] = 2
    changed = indexer.run()

    assert changed.changed_content == 1
    assert changed.work_enqueued == 1
    assert len(catalog.page_work_items(scan_id=changed.scan_id)) == 1
    catalog.close()


def test_legacy_unversioned_change_token_is_rehashed_once(tmp_path):
    catalog, root_path, indexer = create_indexer(tmp_path)
    (root_path / "file.bin").write_bytes(b"unchanged")
    initial = indexer.run()
    finish_analysis_work(catalog, initial.scan_id)
    assert indexer.finalize_scan(initial.scan_id).outcome == IndexOutcome.FINISHED
    row = catalog.page_paths(root_id=initial.root_id)[0]
    catalog._connection.execute(
        "UPDATE content_versions SET change_token = ? WHERE id = ?",
        (b"123456789", row["current_content_version_id"]),
    )

    migrated = indexer.run()

    assert migrated.changed_content == 1
    assert migrated.work_enqueued == 1
    migrated_row = catalog.page_paths(root_id=migrated.root_id)[0]
    assert bytes(migrated_row["change_token"]).startswith(b"dupeguru-content-generation\0v1\0")
    catalog.close()


def test_generation_token_failure_records_incomplete_file_observation(tmp_path):
    def failing_generation(path, **kwargs):
        raise FileGenerationError(path, "injected generation failure")

    catalog, root_path, indexer = create_indexer(
        tmp_path,
        generation_getter=failing_generation,
    )
    (root_path / "file.bin").write_bytes(b"content")

    result = indexer.run()

    assert result.outcome == IndexOutcome.PARTIAL
    assert result.catalog_status == "completed_with_errors"
    assert result.files_observed == 0
    assert result.errors_recorded == 1
    assert "generation failure" in catalog.page_scan_errors(result.scan_id)[0]["message"]
    catalog.close()


def test_expired_directory_lease_resumes_after_reopening_catalog(tmp_path):
    catalog, root_path, indexer = create_indexer(tmp_path, clock=lambda: 1)
    registration, scan_id = indexer.begin_scan(now=1)
    claimed = catalog.claim_scan_directories(
        scan_id,
        "dead-indexer",
        limit=1,
        lease_seconds=1,
        now=1,
    )
    assert len(claimed) == 1
    database_path = tmp_path / "catalog.sqlite3"
    catalog.close()

    reopened = Catalog(database_path)
    resumed_indexer = CatalogIndexer(
        reopened,
        root_path,
        owner="replacement-indexer",
        clock=lambda: 10,
    )
    resumed = resumed_indexer.run(scan_id=scan_id)

    assert resumed.root_id == registration.root_id
    assert resumed.outcome == IndexOutcome.FINISHED
    assert resumed.catalog_status == "complete"
    directory = reopened._connection.execute(
        "SELECT * FROM scan_dirs WHERE scan_id = ?",
        (scan_id,),
    ).fetchone()
    assert directory["status"] == "complete"
    assert directory["attempts"] == 2
    reopened.close()


@pytest.mark.parametrize(
    ("event_kind", "coverage_field"),
    (
        (WalkEventKind.SYMLINK_SKIPPED, "skipped_symlinks"),
        (WalkEventKind.REPARSE_POINT_SKIPPED, "skipped_reparse_points"),
        (WalkEventKind.MOUNT_SKIPPED, "skipped_mounts"),
    ),
)
def test_coverage_reducing_skip_is_persisted_as_partial(
    tmp_path,
    event_kind,
    coverage_field,
):
    def walker(root, **kwargs):
        root = Path(root)
        identity = kwargs["identity_getter"](root, follow_symlinks=False)
        coverage = make_coverage(
            identity,
            entries_seen=1,
            **{coverage_field: 1},
        )
        yield WalkEvent(WalkEventKind.ROOT_STARTED, root)
        yield WalkEvent(WalkEventKind.DIRECTORY, root, identity=identity)
        yield WalkEvent(event_kind, root / "unsafe-entry", detail="deliberate test skip")
        yield WalkEvent(WalkEventKind.COVERAGE, root, coverage=coverage)
        yield WalkEvent(WalkEventKind.ROOT_COMPLETED, root)

    catalog, _root_path, indexer = create_indexer(tmp_path, walker=walker)

    result = indexer.run()

    assert result.outcome == IndexOutcome.PARTIAL
    assert result.catalog_status == "completed_with_errors"
    assert result.coverage is not None
    assert not result.coverage.complete
    assert result.errors_recorded == 1
    assert catalog.scan_coverage(result.scan_id)["errors"] == 1
    error = catalog.page_scan_errors(result.scan_id)[0]
    assert error["operation"] == event_kind.value
    catalog.close()


def test_metadata_change_during_observation_rolls_back_file_and_work(tmp_path):
    catalog, root_path, _indexer = create_indexer(tmp_path)
    changing_path = root_path / "changing.bin"
    changing_path.write_bytes(b"content")

    class StatProxy:
        def __init__(self, wrapped, mtime_ns):
            self._wrapped = wrapped
            self.st_mtime_ns = mtime_ns

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    calls = 0

    def changing_stat(path, follow_symlinks=False):
        nonlocal calls
        current = os.stat(path, follow_symlinks=follow_symlinks)
        if Path(path) != changing_path:
            return current
        calls += 1
        if calls == 1:
            return current
        return StatProxy(current, current.st_mtime_ns + 1)

    indexer = CatalogIndexer(catalog, root_path, stat_getter=changing_stat)
    result = indexer.run()

    assert calls == 2
    assert result.outcome == IndexOutcome.PARTIAL
    assert result.catalog_status == "completed_with_errors"
    assert result.files_observed == 0
    assert result.changed_content == 0
    assert result.work_enqueued == 0
    assert catalog.page_paths(root_id=result.root_id) == []
    assert catalog.page_work_items(scan_id=result.scan_id) == []
    assert "metadata changed" in catalog.page_scan_errors(result.scan_id)[0]["message"]
    catalog.close()


def test_unexpected_walker_exception_keeps_scan_resumable_and_unfinalized(tmp_path):
    def broken_walker(root, **kwargs):
        root = Path(root)
        identity = kwargs["identity_getter"](root, follow_symlinks=False)
        yield WalkEvent(WalkEventKind.ROOT_STARTED, root)
        yield WalkEvent(WalkEventKind.DIRECTORY, root, identity=identity)
        raise OSError("injected enumeration failure")

    catalog, _root_path, indexer = create_indexer(tmp_path, walker=broken_walker)

    result = indexer.run()

    assert result.outcome == IndexOutcome.PARTIAL
    assert result.catalog_status == "running"
    assert "injected enumeration failure" in result.reason
    coverage = catalog.scan_coverage(result.scan_id)
    assert coverage["in_progress"] == 1
    assert coverage["errors"] == 1
    catalog.close()


def test_cancellation_is_explicit_and_terminal(tmp_path):
    catalog, _root_path, indexer = create_indexer(tmp_path)

    result = indexer.run(cancel_check=lambda: True)

    assert result.outcome == IndexOutcome.CANCELLED
    assert result.catalog_status == "cancelled"
    assert catalog.get_scan(result.scan_id)["phase"] == "cancelled"
    assert indexer.run(scan_id=result.scan_id).outcome == IndexOutcome.CANCELLED
    catalog.close()


def test_keyset_pages_materialize_files_and_reject_stale_rows(tmp_path):
    catalog, root_path, indexer = create_indexer(tmp_path)
    for name in ("one.bin", "two.bin", "three.bin"):
        (root_path / name).write_bytes(name.encode("ascii"))
    scanned = indexer.run()
    finish_analysis_work(catalog, scanned.scan_id)
    indexer.finalize_scan(scanned.scan_id)

    first_page = indexer.page_files(limit=2, root_id=scanned.root_id)
    second_page = indexer.page_files(
        after_id=first_page.next_after_id,
        limit=2,
        root_id=scanned.root_id,
    )

    assert len(first_page.files) == 2
    assert len(second_page.files) == 1
    assert all(isinstance(file, fs.File) for file in first_page.files)
    assert len(list(indexer.iter_files(page_size=1, root_id=scanned.root_id))) == 3

    stale_path = Path(first_page.files[0].path)
    stale_path.write_bytes(stale_path.read_bytes() + b" changed")
    with pytest.raises(CatalogPageChangedError):
        indexer.page_files(limit=1, root_id=scanned.root_id)
    catalog.close()
