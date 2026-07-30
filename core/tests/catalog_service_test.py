# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import json
import os

import pytest

import core.catalog_service as catalog_service_module
from core.catalog import Catalog, CatalogStateError
from core.catalog_service import CatalogService, CatalogServiceError
from core.catalog_worker import ContentGenerationChanged, VerifiedExactPage
from core.reserved_paths import RESERVED_INTERNAL_DIRECTORY_NAMES


def create_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    return first, second


def test_multi_root_cold_warm_and_rename_are_one_scan_each(tmp_path):
    first, second = create_roots(tmp_path)
    duplicate = b"same bytes"
    original = first / "original.bin"
    original.write_bytes(duplicate)
    (second / "duplicate.bin").write_bytes(duplicate)
    (second / "unique.bin").write_bytes(b"unique")
    service = CatalogService(tmp_path / "catalog.sqlite3", (first, second))

    cold = service.run(app_version="test")
    exact_groups = list(service.iter_verified_exact_groups(page_size=1))
    warm = service.run(app_version="test")
    renamed = first / "renamed.bin"
    original.rename(renamed)
    rename_scan = service.run(app_version="test")

    assert cold.outcome == "finished"
    assert cold.roots_total == cold.roots_processed == 2
    assert cold.changed_content == cold.work_completed == 3
    assert cold.status.directory_counts["complete"] == 2
    assert len(exact_groups) == 1
    assert len(exact_groups[0].files) == 2
    assert warm.outcome == "finished"
    assert warm.changed_content == warm.work_enqueued == warm.work_completed == 0
    assert rename_scan.outcome == "finished"
    assert rename_scan.changed_content == rename_scan.work_enqueued == rename_scan.work_completed == 1
    assert json.loads(json.dumps(rename_scan.to_dict()))["catalog_status"] == "complete"
    active_paths = {row["display_path"] for row in service.catalog.page_paths()}
    assert str(original) not in active_paths
    assert str(renamed) in active_paths
    service.close()


def test_database_path_inside_a_selected_root_is_rejected_before_creation(
    tmp_path,
):
    first, second = create_roots(tmp_path)
    database = first / "catalog.sqlite3"

    with pytest.raises(CatalogServiceError):
        CatalogService(database, (first, second))

    assert not database.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_database_hardlink_alias_of_root_file_is_rejected_without_writes(tmp_path):
    first, second = create_roots(tmp_path)
    source = first / "user-data.bin"
    original = b"not a sqlite database and must never be modified"
    source.write_bytes(original)
    database = tmp_path / "catalog.sqlite3"
    try:
        os.link(source, database)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    with pytest.raises(
        CatalogServiceError,
        match="exactly one filesystem link",
    ):
        CatalogService(database, (first, second))

    assert source.read_bytes() == original
    assert database.read_bytes() == original


def test_canonical_database_alias_inside_root_is_rejected_before_open(
    tmp_path,
    monkeypatch,
):
    first, second = create_roots(tmp_path)
    database = tmp_path / "catalog.sqlite3"
    Catalog(database).close()
    original = database.read_bytes()
    realpath = os.path.realpath
    database_key = os.path.normcase(os.path.abspath(str(database)))

    def injected_realpath(path, *, strict=False):
        if os.path.normcase(os.path.abspath(os.fspath(path))) == database_key:
            return str(first / "catalog.sqlite3")
        return realpath(path, strict=strict)

    def unexpected_catalog_open(*_args, **_kwargs):
        raise AssertionError("canonical boundary must be checked before SQLite opens")

    monkeypatch.setattr(catalog_service_module.os.path, "realpath", injected_realpath)
    monkeypatch.setattr(catalog_service_module, "Catalog", unexpected_catalog_open)

    with pytest.raises(CatalogServiceError, match="must not resolve through an alias"):
        CatalogService(database, (first, second))

    assert database.read_bytes() == original


def test_single_link_existing_database_reopens_through_shared_preflight(tmp_path):
    first, second = create_roots(tmp_path)
    database = tmp_path / "catalog.sqlite3"
    CatalogService(database, (first, second)).close()

    reopened = CatalogService(database, (first, second))

    reopened.close()


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES | {name.upper() for name in RESERVED_INTERNAL_DIRECTORY_NAMES}),
)
def test_reserved_internal_directories_are_unoverrideably_pruned(
    tmp_path,
    reserved_name,
):
    first, second = create_roots(tmp_path)
    reserved = first / reserved_name
    reserved.mkdir()
    (reserved / "hidden.bin").write_bytes(b"same")
    temporary = first / ".image.png.dupeguru-abcdef012345-000001.tmp"
    temporary.write_bytes(b"same")
    visible = first / "visible.bin"
    visible.write_bytes(b"same")
    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first, second),
        directory_pruner=lambda _path: None,
    )

    result = service.run()

    assert result.outcome == "finished"
    assert {row["display_path"] for row in service.catalog.page_paths()} == {str(visible)}
    service.close()


@pytest.mark.parametrize(
    "reserved_name",
    sorted(RESERVED_INTERNAL_DIRECTORY_NAMES | {name.upper() for name in RESERVED_INTERNAL_DIRECTORY_NAMES}),
)
def test_reserved_internal_directory_cannot_be_a_catalog_root(
    tmp_path,
    reserved_name,
):
    root = tmp_path / reserved_name
    root.mkdir()
    database = tmp_path / "catalog.sqlite3"

    with pytest.raises(CatalogServiceError, match="internal"):
        CatalogService(database, (root,))

    assert not database.exists()


def test_expired_directory_lease_resumes_after_service_reopen(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    (second / "two.bin").write_bytes(b"two")
    database = tmp_path / "catalog.sqlite3"
    service = CatalogService(database, (first, second))
    scan_id = service.start_scan()
    claimed = service.catalog.claim_scan_directories(
        scan_id,
        "dead-service",
        limit=1,
        lease_seconds=1,
        now=1,
    )
    assert len(claimed) == 1
    service.close()

    resumed_service = CatalogService(database, (first, second))
    resumed = resumed_service.resume(scan_id)

    assert resumed.outcome == "finished"
    assert resumed.catalog_status == "complete"
    assert resumed.status.directory_counts["complete"] == 2
    assert resumed.work_completed == 2
    resumed_service.close()


def test_interrupt_is_partial_and_same_scan_can_resume(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    service = CatalogService(tmp_path / "catalog.sqlite3", (first, second))
    scan_id = service.start_scan()

    interrupted = service.resume(scan_id, cancel_check=lambda: True)
    resumed = service.resume(scan_id)

    assert interrupted.outcome == "partial"
    assert interrupted.catalog_status == "running"
    assert interrupted.errors
    assert resumed.scan_id == scan_id
    assert resumed.outcome == "finished"
    service.close()


def test_resuming_a_complete_scan_is_idempotent(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    service = CatalogService(tmp_path / "catalog.sqlite3", (first, second))

    completed = service.run()
    observation_count = service.catalog._connection.execute(
        "SELECT COUNT(*) FROM scan_path_observations WHERE scan_id = ?",
        (completed.scan_id,),
    ).fetchone()[0]
    snapshot = dict(
        service.catalog._connection.execute(
            "SELECT * FROM scan_snapshots WHERE scan_id = ?",
            (completed.scan_id,),
        ).fetchone()
    )

    repeated = service.resume(completed.scan_id)

    assert repeated.outcome == "finished"
    assert repeated.catalog_status == "complete"
    assert repeated.scan_id == completed.scan_id
    assert (
        service.catalog._connection.execute(
            "SELECT COUNT(*) FROM scan_path_observations WHERE scan_id = ?",
            (completed.scan_id,),
        ).fetchone()[0]
        == observation_count
    )
    assert (
        dict(
            service.catalog._connection.execute(
                "SELECT * FROM scan_snapshots WHERE scan_id = ?",
                (completed.scan_id,),
            ).fetchone()
        )
        == snapshot
    )
    service.close()


def test_worker_batch_bound_returns_partial_then_resumes(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    (second / "two.bin").write_bytes(b"two")
    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first, second),
        worker_batch_size=1,
        max_worker_batches=1,
    )

    partial = service.run()
    finished = service.resume(partial.scan_id)

    assert partial.outcome == "partial"
    assert partial.catalog_status == "running"
    assert partial.work_completed == 1
    assert partial.status.work_counts["pending"] == 1
    assert finished.outcome == "finished"
    assert finished.work_completed == 1
    service.close()


def test_analysis_failure_is_explicit_completed_with_errors(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "unknown.bin").write_bytes(b"unknown")
    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first, second),
        analysis_kinds=("unsupported-analysis",),
    )

    result = service.run()

    assert result.outcome == "partial"
    assert result.catalog_status == "completed_with_errors"
    assert result.work_failed == 1
    assert result.errors
    assert result.status.work_counts["failed"] == 1
    service.close()


def test_backup_delegates_to_integrity_checked_catalog_backup(tmp_path):
    first, second = create_roots(tmp_path)
    service = CatalogService(tmp_path / "catalog.sqlite3", (first, second))
    service.run()
    destination = tmp_path / "backup.sqlite3"

    returned = service.backup(destination)

    assert returned == str(destination)
    backup = Catalog(destination)
    assert backup.verify_integrity()
    backup.close()
    service.close()


def test_verified_iterator_keeps_100k_synthetic_groups_page_bounded(tmp_path):
    first, second = create_roots(tmp_path)
    service = CatalogService(tmp_path / "catalog.sqlite3", (first, second))
    assert service.run().outcome == "finished"
    requested_limits = []

    def fake_page(after_size=-1, after_digest=b"", limit=100, root_ids=None):
        requested_limits.append(limit)
        start = after_size + 1
        if start >= 100000:
            return VerifiedExactPage((), after_size, after_digest, 0)
        stop = min(start + limit, 100000)
        return VerifiedExactPage(
            tuple(range(start, stop)),
            stop - 1,
            b"",
            0,
        )

    service.worker.page_verified_exact_groups = fake_page

    count = sum(1 for _group in service.iter_verified_exact_groups(page_size=777))

    assert count == 100000
    assert len(requested_limits) == 130
    assert set(requested_limits) == {777}
    service.close()


def test_verified_projection_is_scoped_to_selected_roots_and_rejects_running_scan(
    tmp_path,
):
    first, second = create_roots(tmp_path)
    content = b"shared"
    (first / "a.bin").write_bytes(content)
    (first / "b.bin").write_bytes(content)
    outside = second / "outside.bin"
    outside.write_bytes(content)
    database = tmp_path / "catalog.sqlite3"
    combined = CatalogService(database, (first, second))
    assert combined.run().outcome == "finished"
    combined.close()

    selected = CatalogService(database, (first,))
    groups = list(selected.iter_verified_exact_groups())

    assert len(groups) == 1
    assert {item.path for item in groups[0].files} == {
        first / "a.bin",
        first / "b.bin",
    }
    assert outside not in {item.path for item in groups[0].files}

    running_scan = selected.start_scan()
    assert not selected.status(running_scan).verified_projection_allowed
    with pytest.raises(CatalogStateError):
        list(selected.iter_verified_exact_groups())
    selected.close()


def test_read_only_projection_reports_explicit_writable_repair_requirement(
    tmp_path,
):
    first, second = create_roots(tmp_path)
    payload = b"read-only repair proof"
    (first / "first.bin").write_bytes(payload)
    (second / "second.bin").write_bytes(payload)
    database = tmp_path / "catalog.sqlite3"
    writable = CatalogService(database, (first, second))
    result = writable.run()
    root_ids = writable.selected_root_ids()
    assert result.outcome == "finished"
    for artifact in writable.catalog._connection.execute(
        "SELECT id FROM artifacts WHERE kind = 'full_hash' AND algorithm = 'sha256'"
    ):
        writable.catalog._connection.execute(
            "UPDATE artifacts SET value = ? WHERE id = ?",
            (b"\x7f" * 32, artifact["id"]),
        )
    writable.catalog._connection.commit()
    writable.close()

    read_only_catalog = Catalog.open_read_only(database)
    read_only = CatalogService(
        database,
        (first, second),
        catalog=read_only_catalog,
        selected_root_ids=root_ids,
    )
    with read_only:
        with pytest.raises(
            ContentGenerationChanged,
            match="open the catalog writable and run a repair scan",
        ):
            list(read_only.iter_verified_exact_groups())

    with Catalog.open_read_only(database) as unchanged:
        assert (
            unchanged._connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE value = ?",
                (b"\x7f" * 32,),
            ).fetchone()[0]
            == 2
        )


def test_intentionally_pruned_subtree_is_recorded_but_scan_remains_complete(tmp_path):
    first, second = create_roots(tmp_path)
    excluded = first / "excluded"
    excluded.mkdir()
    (excluded / "ignored.bin").write_bytes(b"ignored")
    (second / "included.bin").write_bytes(b"included")

    def directory_pruner(path):
        if path == excluded:
            return "test excluded subtree"
        return None

    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first, second),
        directory_pruner=directory_pruner,
    )
    result = service.run()

    assert result.outcome == "finished"
    assert result.status.verified_projection_allowed
    assert {row["display_path"] for row in service.catalog.page_paths()} == {str(second / "included.bin")}
    action = service.catalog._connection.execute(
        "SELECT * FROM action_journal WHERE action_type = 'directory_pruned'"
    ).fetchone()
    assert action is not None
    assert action["status"] == "completed"
    service.close()


def test_service_reports_enumeration_analysis_and_finalization_progress(tmp_path):
    first, second = create_roots(tmp_path)
    (first / "first.bin").write_bytes(b"same")
    (second / "second.bin").write_bytes(b"same")
    progress = []
    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first, second),
        worker_batch_size=1,
        progress_callback=progress.append,
    )

    result = service.run()

    assert result.outcome == "finished"
    phases = [update.phase for update in progress]
    assert phases[0] == "enumerating"
    assert "analyzing" in phases
    assert "finalizing" in phases
    assert phases[-1] == "finished"
    assert max(update.files_seen for update in progress) == 2
    assert max(update.files_observed for update in progress) == 2
    assert max(update.directories_seen for update in progress) >= 2
    assert max(update.work_total for update in progress) == 2
    assert progress[-1].work_completed == 2
    assert [update.roots_processed for update in progress] == sorted(update.roots_processed for update in progress)
    service.close()


def test_progress_callback_failure_never_changes_scan_outcome(tmp_path):
    first, _second = create_roots(tmp_path)
    (first / "first.bin").write_bytes(b"content")

    def broken_progress_callback(_progress):
        raise RuntimeError("presentation failed")

    service = CatalogService(
        tmp_path / "catalog.sqlite3",
        (first,),
        progress_callback=broken_progress_callback,
    )

    result = service.run()

    assert result.outcome == "finished"
    assert result.catalog_status == "complete"
    service.close()
