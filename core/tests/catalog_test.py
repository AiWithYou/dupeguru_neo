# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os
import sqlite3
import sys

import pytest

import core.catalog as catalog_module
from core.catalog import (
    CATALOG_APPLICATION_ID,
    CATALOG_OWNER,
    SCHEMA_VERSION,
    Catalog,
    CatalogCorruptError,
    CatalogPathError,
    CatalogSchemaError,
    CatalogStateError,
    CatalogTooNewError,
    ScanIncompleteError,
)


def create_catalog(tmp_path):
    database_path = tmp_path / "catalog.sqlite3"
    catalog = Catalog(database_path)
    volume_id = catalog.upsert_volume(
        "test-volume",
        platform=sys.platform,
        fs_type="testfs",
        identity_capability="stable",
        timestamp_granularity_ns=1,
        now=1,
    )
    root_path = tmp_path / "library"
    root_id = catalog.upsert_root(volume_id, root_path, now=1)
    return catalog, database_path, root_id, root_path


def downgrade_catalog(database_path, version):
    """Reproduce the exact schema shape emitted by an older migration level."""

    assert version in {1, 2, 3}
    connection = sqlite3.connect(str(database_path))
    if version < 3:
        connection.execute("DROP TABLE scan_path_observations")
        connection.execute("DROP TABLE scan_snapshots")
    if version < 2:
        connection.execute("DROP TABLE action_journal")
        for index_name in sorted(catalog_module._V2_INDEXES - catalog_module._V1_INDEXES):
            connection.execute("DROP INDEX IF EXISTS {}".format(index_name))
    connection.execute("DELETE FROM catalog_meta WHERE key = 'owner'")
    connection.execute(
        "UPDATE catalog_meta SET value = ? WHERE key = 'schema_version'",
        (str(version),),
    )
    connection.execute("PRAGMA application_id = 0")
    connection.commit()
    connection.close()


def catalog_family_bytes(database_path):
    return {
        path.name: path.read_bytes() for path in database_path.parent.glob(database_path.name + "*") if path.is_file()
    }


def begin_root_scan(catalog, root_id, owner="enumerator", lease_seconds=60, now=10):
    scan_id = catalog.start_scan([root_id], app_version="test", now=now)
    claimed = catalog.claim_scan_directories(
        scan_id,
        owner,
        limit=1,
        lease_seconds=lease_seconds,
        now=now,
    )
    assert len(claimed) == 1
    return scan_id, claimed[0]


def observe(
    catalog,
    scan_id,
    root_id,
    path,
    native_file_id,
    size=100,
    mtime_ns=1000,
    now=10,
):
    return catalog.observe_file(
        scan_id,
        root_id,
        path,
        size=size,
        mtime_ns=mtime_ns,
        native_file_id=native_file_id,
        identity_confidence="stable",
        change_token="generation-{}".format(mtime_ns),
        now=now,
    )


def test_schema_contains_all_durable_catalog_tables(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    assert catalog.schema_version == SCHEMA_VERSION
    assert catalog.verify_integrity()
    catalog.close()

    connection = sqlite3.connect(str(database_path))
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    metadata = dict(connection.execute("SELECT key, value FROM catalog_meta"))
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    connection.close()

    assert {
        "catalog_meta",
        "volumes",
        "roots",
        "physical_files",
        "paths",
        "content_versions",
        "artifacts",
        "scans",
        "scan_dirs",
        "scan_errors",
        "work_items",
        "verification_records",
        "action_journal",
        "scan_snapshots",
        "scan_path_observations",
    } <= tables
    assert metadata == {
        "schema_version": str(SCHEMA_VERSION),
        "owner": CATALOG_OWNER,
    }
    assert application_id == CATALOG_APPLICATION_ID


def test_new_exclusive_reservation_commits_owner_markers_before_schema_build(tmp_path, monkeypatch):
    database_path = tmp_path / "catalog.sqlite3"
    monkeypatch.setitem(catalog_module._MIGRATIONS, 1, ("THIS IS NOT VALID SQL",))

    with pytest.raises(sqlite3.OperationalError):
        Catalog(database_path)

    header = database_path.read_bytes()[:100]
    assert header.startswith(b"SQLite format 3\0")
    assert int.from_bytes(header[68:72], "big") == CATALOG_APPLICATION_ID
    connection = sqlite3.connect(str(database_path))
    metadata = dict(connection.execute("SELECT key, value FROM catalog_meta"))
    connection.close()
    assert metadata == {
        "schema_version": "0",
        "owner": CATALOG_OWNER,
    }


def test_catalog_connections_disable_trusted_schema_and_apply_resource_limits(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    assert catalog._connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    if hasattr(catalog._connection, "getlimit"):
        assert catalog._connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 64 * 1024 * 1024
        assert catalog._connection.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
        assert catalog._connection.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH) == 0
    catalog.close()

    with Catalog.open_read_only(database_path) as read_only:
        assert read_only._connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert read_only._connection.execute("PRAGMA query_only").fetchone()[0] == 1


def test_second_guard_rechecks_raw_application_id_before_writable_connect(tmp_path, monkeypatch):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    real_inspect = catalog_module.inspect_catalog_file
    real_connect = sqlite3.connect
    inspected_connects = 0

    def counting_connect(*args, **kwargs):
        nonlocal inspected_connects
        inspected_connects += 1
        return real_connect(*args, **kwargs)

    def inspect_then_remove_application_id(path):
        inspection = real_inspect(path)
        connection = real_connect(str(path))
        connection.execute("PRAGMA application_id = 0")
        connection.close()
        return inspection

    monkeypatch.setattr(catalog_module.sqlite3, "connect", counting_connect)
    monkeypatch.setattr(catalog_module, "inspect_catalog_file", inspect_then_remove_application_id)

    with pytest.raises(CatalogSchemaError, match="application_id"):
        Catalog(database_path)

    assert inspected_connects == 1


@pytest.mark.parametrize("version", (1, 2, 3))
def test_unmarked_legacy_catalog_is_rejected_without_mutation(tmp_path, version):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    downgrade_catalog(database_path, version)
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="application_id"):
        Catalog(database_path)
    with pytest.raises(CatalogSchemaError, match="application_id"):
        Catalog.open_read_only(database_path)

    assert catalog_family_bytes(database_path) == original_family


def test_owned_older_catalog_is_rejected_instead_of_migrated(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    downgrade_catalog(database_path, 2)
    connection = sqlite3.connect(str(database_path))
    connection.execute(
        "INSERT INTO catalog_meta(key, value) VALUES ('owner', ?)",
        (CATALOG_OWNER,),
    )
    connection.execute("PRAGMA application_id = {}".format(CATALOG_APPLICATION_ID))
    connection.commit()
    connection.close()
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="older than"):
        Catalog(database_path)
    with pytest.raises(CatalogSchemaError, match="older than"):
        Catalog.open_read_only(database_path)

    assert catalog_family_bytes(database_path) == original_family


def test_refuses_unknown_newer_schema_without_modifying_it(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()

    connection = sqlite3.connect(str(database_path))
    connection.execute("UPDATE catalog_meta SET value = '999' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(CatalogTooNewError):
        Catalog(database_path)

    connection = sqlite3.connect(str(database_path))
    version = connection.execute("SELECT value FROM catalog_meta WHERE key = 'schema_version'").fetchone()[0]
    connection.close()
    assert version == "999"


def test_corrupt_database_is_preserved_for_recovery(tmp_path):
    database_path = tmp_path / "catalog.sqlite3"
    original = b"this is deliberately not sqlite"
    database_path.write_bytes(original)

    with pytest.raises(CatalogCorruptError):
        Catalog(database_path)

    assert database_path.read_bytes() == original


def test_foreign_sqlite_with_catalog_meta_and_sentinel_is_never_modified(tmp_path, monkeypatch):
    database_path = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(str(database_path))
    connection.execute("CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO catalog_meta(key, value) VALUES ('schema_version', '1')")
    connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel(value) VALUES ('must remain untouched')")
    connection.commit()
    connection.close()
    original_family = catalog_family_bytes(database_path)
    real_connect = sqlite3.connect

    def forbidden_connect(*_args, **_kwargs):
        pytest.fail("foreign application_id must be rejected before sqlite3.connect")

    monkeypatch.setattr(catalog_module.sqlite3, "connect", forbidden_connect)
    with pytest.raises(CatalogSchemaError, match="application_id"):
        Catalog(database_path)

    assert catalog_family_bytes(database_path) == original_family
    connection = real_connect(
        "{}?mode=ro&immutable=1".format(database_path.as_uri()),
        uri=True,
    )
    sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]
    metadata = dict(connection.execute("SELECT key, value FROM catalog_meta"))
    connection.close()
    assert sentinel == "must remain untouched"
    assert metadata == {"schema_version": "1"}


def test_current_read_only_open_does_not_create_sidecars(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    original_family = catalog_family_bytes(database_path)

    with Catalog.open_read_only(database_path) as read_only:
        assert read_only.read_only
        assert read_only.schema_version == SCHEMA_VERSION
        assert read_only.verify_integrity()
        with pytest.raises(CatalogStateError, match="read-only"):
            with read_only.transaction():
                pass

    assert catalog_family_bytes(database_path) == original_family


def test_current_application_id_without_owner_marker_is_rejected_without_mutation(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    connection = sqlite3.connect(str(database_path))
    connection.execute("DELETE FROM catalog_meta WHERE key = 'owner'")
    connection.commit()
    connection.close()
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="owner metadata"):
        Catalog.open_read_only(database_path)
    with pytest.raises(CatalogSchemaError, match="owner metadata"):
        Catalog(database_path)

    assert catalog_family_bytes(database_path) == original_family


def test_catalog_meta_row_and_value_bounds_reject_without_mutation(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    connection = sqlite3.connect(str(database_path))
    connection.execute("INSERT INTO catalog_meta(key, value) VALUES ('extra', 'unexpected')")
    connection.commit()
    connection.close()
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="too many ownership rows"):
        Catalog(database_path)

    assert catalog_family_bytes(database_path) == original_family

    connection = sqlite3.connect(str(database_path))
    connection.execute("DELETE FROM catalog_meta WHERE key = 'extra'")
    connection.execute(
        "UPDATE catalog_meta SET value = CAST(zeroblob(?) AS TEXT) WHERE key = 'owner'",
        (catalog_module._MAX_CATALOG_META_VALUE_BYTES + 1,),
    )
    connection.commit()
    connection.close()
    oversized_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="invalid"):
        Catalog.open_read_only(database_path)

    assert catalog_family_bytes(database_path) == oversized_family


def test_schema_object_counter_bound_rejects_without_mutation(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    connection = sqlite3.connect(str(database_path))
    for index in range(catalog_module._MAX_CATALOG_SCHEMA_OBJECTS + 1):
        connection.execute("CREATE TABLE overflow_{:03d}(value INTEGER)".format(index))
    connection.commit()
    connection.close()
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogSchemaError, match="schema objects"):
        Catalog(database_path)

    assert catalog_family_bytes(database_path) == original_family


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_catalog_hardlink_is_rejected_before_sqlite_open(tmp_path):
    catalog, database_path, _root_id, _root_path = create_catalog(tmp_path)
    catalog.close()
    alias_path = tmp_path / "catalog-alias.sqlite3"
    try:
        os.link(database_path, alias_path)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))
    original = database_path.read_bytes()
    original_names = sorted(path.name for path in tmp_path.glob("catalog*"))

    with pytest.raises(CatalogPathError, match="exactly one filesystem link"):
        Catalog(alias_path)

    assert database_path.read_bytes() == alias_path.read_bytes() == original
    assert sorted(path.name for path in tmp_path.glob("catalog*")) == original_names


def test_catalog_ancestor_symlink_is_rejected_before_sqlite_open(tmp_path):
    real_parent = tmp_path / "private"
    real_parent.mkdir()
    database_path = real_parent / "catalog.sqlite3"
    Catalog(database_path).close()
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))
    original_family = catalog_family_bytes(database_path)

    with pytest.raises(CatalogPathError, match="plain directories"):
        Catalog(alias_parent / database_path.name)

    assert catalog_family_bytes(database_path) == original_family


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode boundary")
def test_catalog_group_writable_parent_is_rejected_without_mutation(tmp_path):
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o770)
    database_path = unsafe_parent / "catalog.sqlite3"
    original = b"foreign bytes must not be opened"
    database_path.write_bytes(original)
    try:
        with pytest.raises(CatalogPathError, match="writable by group or other"):
            Catalog(database_path)
        assert database_path.read_bytes() == original
        assert catalog_family_bytes(database_path) == {
            database_path.name: original,
        }
    finally:
        unsafe_parent.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode boundary")
def test_catalog_replaceable_ancestor_is_rejected_without_mutation(tmp_path):
    unsafe_ancestor = tmp_path / "replaceable"
    unsafe_ancestor.mkdir()
    private_parent = unsafe_ancestor / "private"
    private_parent.mkdir(mode=0o700)
    unsafe_ancestor.chmod(0o777)
    database_path = private_parent / "catalog.sqlite3"
    original = b"foreign bytes must not be opened through an unsafe ancestor"
    database_path.write_bytes(original)
    try:
        with pytest.raises(CatalogPathError, match="replaceable by another user"):
            Catalog(database_path)
        assert database_path.read_bytes() == original
        assert catalog_family_bytes(database_path) == {
            database_path.name: original,
        }
    finally:
        unsafe_ancestor.chmod(0o700)


def test_composed_transactions_roll_back_atomically(tmp_path):
    catalog, _database_path, _root_id, root_path = create_catalog(tmp_path)

    with pytest.raises(RuntimeError):
        with catalog.transaction():
            rolled_back_volume_id = catalog.upsert_volume(
                "rolled-back-volume",
                platform=sys.platform,
                identity_capability="path_only",
                now=2,
            )
            raise RuntimeError("inject rollback")

    with pytest.raises(CatalogStateError):
        catalog.upsert_root(rolled_back_volume_id, root_path / "rolled-back", now=3)
    catalog.close()


def test_warm_scan_reuses_content_generation_and_artifacts(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    image_path = root_path / "image.jpg"

    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    first = observe(catalog, first_scan, root_id, image_path, b"file-id-1", now=10)
    artifact_id = catalog.put_artifact(
        first.content_version_id,
        "full_hash",
        "sha256",
        "1",
        b"full-digest",
        verification_level="full",
        now=10,
    )
    catalog.complete_scan_directory(first_directory["id"], owner="enumerator", now=11)
    assert catalog.finish_scan(first_scan, now=12) == "complete"

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    second = observe(catalog, second_scan, root_id, image_path, b"file-id-1", now=20)
    catalog.complete_scan_directory(second_directory["id"], owner="enumerator", now=21)
    assert catalog.finish_scan(second_scan, now=22) == "complete"

    assert second.physical_file_id == first.physical_file_id
    assert second.path_id == first.path_id
    assert second.content_version_id == first.content_version_id
    assert not second.new_physical_file
    assert not second.new_path
    assert not second.new_content
    assert catalog.get_artifact(second.content_version_id, "full_hash", "sha256", "1")["id"] == artifact_id
    catalog.close()


def test_changed_file_gets_new_content_generation_without_stale_artifact(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    image_path = root_path / "image.jpg"

    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    first = observe(catalog, first_scan, root_id, image_path, b"file-id-1", now=10)
    catalog.put_artifact(
        first.content_version_id,
        "full_hash",
        "sha256",
        "1",
        b"old-digest",
        verification_level="full",
        now=10,
    )
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    second = observe(
        catalog,
        second_scan,
        root_id,
        image_path,
        b"file-id-1",
        size=101,
        mtime_ns=2000,
        now=20,
    )
    catalog.complete_scan_directory(second_directory["id"], now=21)
    catalog.finish_scan(second_scan, now=22)

    assert second.physical_file_id == first.physical_file_id
    assert second.content_version_id != first.content_version_id
    assert second.new_content
    assert (
        catalog.get_artifact(
            second.content_version_id,
            "full_hash",
            "sha256",
            "1",
        )
        is None
    )
    catalog.close()


def test_rename_reuses_identity_and_hash_then_tombstones_old_path(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    old_path = root_path / "old.jpg"
    new_path = root_path / "renamed.jpg"

    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    first = observe(catalog, first_scan, root_id, old_path, b"stable-file-id", now=10)
    catalog.put_artifact(
        first.content_version_id,
        "full_hash",
        "sha256",
        "1",
        b"digest",
        verification_level="full",
        now=10,
    )
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    renamed = observe(
        catalog,
        second_scan,
        root_id,
        new_path,
        b"stable-file-id",
        now=20,
    )
    catalog.complete_scan_directory(second_directory["id"], now=21)
    catalog.finish_scan(second_scan, now=22)

    assert renamed.physical_file_id == first.physical_file_id
    assert renamed.content_version_id == first.content_version_id
    assert renamed.new_path
    assert renamed.identity_reused
    assert not renamed.new_content
    assert catalog.get_artifact(renamed.content_version_id, "full_hash", "sha256", "1") is not None
    paths = {
        row["display_path"]: row["path_state"]
        for row in catalog.page_paths(root_id=root_id, states=("active", "missing"))
    }
    assert paths[str(old_path)] == "missing"
    assert paths[str(new_path)] == "active"
    catalog.close()


def test_complete_scan_differences_are_keyset_paged_and_identity_proven(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    modified_path = root_path / "modified.bin"
    old_move_path = root_path / "old-move.bin"
    new_move_path = root_path / "new-move.bin"
    missing_path = root_path / "missing.bin"
    added_path = root_path / "added.bin"
    old_path_only = root_path / "old-path-only.bin"
    new_path_only = root_path / "new-path-only.bin"
    old_hardlinks = (
        root_path / "old-hardlink-a.bin",
        root_path / "old-hardlink-b.bin",
    )
    new_hardlinks = (
        root_path / "new-hardlink-a.bin",
        root_path / "new-hardlink-b.bin",
    )

    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    observe(catalog, first_scan, root_id, modified_path, b"modified", now=10)
    observe(catalog, first_scan, root_id, old_move_path, b"move", now=10)
    observe(catalog, first_scan, root_id, missing_path, b"missing", now=10)
    catalog.observe_file(
        first_scan,
        root_id,
        old_path_only,
        size=100,
        mtime_ns=1000,
        identity_confidence="path_only",
        change_token=b"path-only",
        now=10,
    )
    for path in old_hardlinks:
        observe(catalog, first_scan, root_id, path, b"hardlink", now=10)
    catalog.complete_scan_directory(first_directory["id"], now=11)
    assert catalog.finish_scan(first_scan, now=12) == "complete"

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    observe(
        catalog,
        second_scan,
        root_id,
        modified_path,
        b"modified",
        size=101,
        mtime_ns=2000,
        now=20,
    )
    observe(catalog, second_scan, root_id, new_move_path, b"move", now=20)
    observe(catalog, second_scan, root_id, added_path, b"added", now=20)
    catalog.observe_file(
        second_scan,
        root_id,
        new_path_only,
        size=100,
        mtime_ns=1000,
        identity_confidence="path_only",
        change_token=b"path-only",
        now=20,
    )
    for path in new_hardlinks:
        observe(catalog, second_scan, root_id, path, b"hardlink", now=20)
    catalog.complete_scan_directory(second_directory["id"], now=21)
    assert catalog.finish_scan(second_scan, now=22) == "complete"

    changes = []
    cursor = (0, "", "")
    while True:
        page = catalog.page_scan_changes(
            first_scan,
            second_scan,
            (root_id,),
            after_root_id=cursor[0],
            after_path_key=cursor[1],
            after_change_type=cursor[2],
            limit=3,
        )
        if not page:
            break
        changes.extend(page)
        last = page[-1]
        cursor = (
            last["sort_root_id"],
            last["sort_path_key"],
            last["change_type"],
        )

    assert len(changes) == 10
    assert [row["change_type"] for row in changes].count("modified") == 1
    assert [row["change_type"] for row in changes].count("moved") == 1
    assert [row["change_type"] for row in changes].count("missing") == 4
    assert [row["change_type"] for row in changes].count("added") == 4
    modified = next(row for row in changes if row["change_type"] == "modified")
    assert modified["old_display_path"] == str(modified_path)
    assert modified["new_display_path"] == str(modified_path)
    assert modified["content_changed"] == 1
    moved = next(row for row in changes if row["change_type"] == "moved")
    assert moved["old_display_path"] == str(old_move_path)
    assert moved["new_display_path"] == str(new_move_path)
    assert moved["identity_proven"] == 1
    assert moved["old_native_file_id"] == moved["new_native_file_id"] == b"move"
    assert not any(
        row["change_type"] == "moved"
        and row["old_display_path"] in {str(old_path_only), *(str(p) for p in old_hardlinks)}
        for row in changes
    )
    catalog.close()


def test_scan_differences_reject_partial_or_snapshotless_history(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    observe(catalog, first_scan, root_id, root_path / "first.bin", b"first", now=10)
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)

    running_scan, _running_directory = begin_root_scan(catalog, root_id, now=20)
    with pytest.raises(CatalogStateError, match="not complete"):
        catalog.page_scan_changes(first_scan, running_scan, (root_id,))

    catalog._connection.execute(
        "DELETE FROM scan_snapshots WHERE scan_id = ?",
        (first_scan,),
    )
    with pytest.raises(CatalogStateError, match="predates immutable"):
        catalog.page_scan_changes(first_scan, first_scan, (root_id,))
    catalog.close()


def test_scan_differences_are_strictly_scoped_to_requested_roots(tmp_path):
    catalog, _database_path, first_root_id, first_root = create_catalog(tmp_path)
    volume_id = catalog._connection.execute(
        "SELECT volume_id FROM roots WHERE id = ?",
        (first_root_id,),
    ).fetchone()[0]
    second_root = tmp_path / "other-library"
    second_root_id = catalog.upsert_root(volume_id, second_root, now=1)

    first_scan = catalog.start_scan((first_root_id, second_root_id), now=10)
    first_directories = catalog.claim_scan_directories(
        first_scan,
        "first",
        limit=2,
        now=10,
    )
    observe(
        catalog,
        first_scan,
        first_root_id,
        first_root / "changed.bin",
        b"first-root",
        now=10,
    )
    observe(
        catalog,
        first_scan,
        second_root_id,
        second_root / "changed.bin",
        b"second-root",
        now=10,
    )
    for directory in first_directories:
        catalog.complete_scan_directory(directory["id"], owner="first", now=11)
    catalog.finish_scan(first_scan, now=12)

    second_scan = catalog.start_scan((first_root_id, second_root_id), now=20)
    second_directories = catalog.claim_scan_directories(
        second_scan,
        "second",
        limit=2,
        now=20,
    )
    observe(
        catalog,
        second_scan,
        first_root_id,
        first_root / "changed.bin",
        b"first-root",
        size=101,
        mtime_ns=2000,
        now=20,
    )
    observe(
        catalog,
        second_scan,
        second_root_id,
        second_root / "changed.bin",
        b"second-root",
        size=102,
        mtime_ns=3000,
        now=20,
    )
    for directory in second_directories:
        catalog.complete_scan_directory(directory["id"], owner="second", now=21)
    catalog.finish_scan(second_scan, now=22)

    first_root_changes = catalog.page_scan_changes(
        first_scan,
        second_scan,
        (first_root_id,),
    )

    assert len(first_root_changes) == 1
    assert first_root_changes[0]["old_root_id"] == first_root_id
    assert first_root_changes[0]["new_root_id"] == first_root_id
    catalog.close()


def test_interrupted_directory_and_work_leases_resume_after_reopen(tmp_path):
    catalog, database_path, root_id, root_path = create_catalog(tmp_path)
    scan_id, scan_directory = begin_root_scan(catalog, root_id, owner="first", lease_seconds=5, now=10)
    observation = observe(
        catalog,
        scan_id,
        root_id,
        root_path / "image.jpg",
        b"file-id-1",
        now=10,
    )
    work_item_id = catalog.enqueue_work_item(
        scan_id,
        observation.content_version_id,
        "full_hash",
        payload={"path": str(root_path / "image.jpg")},
        now=10,
    )
    claimed_work = catalog.claim_work_items(
        "first",
        scan_id=scan_id,
        lease_seconds=5,
        now=10,
    )
    assert [item["id"] for item in claimed_work] == [work_item_id]
    catalog.close()

    catalog = Catalog(database_path)
    assert catalog.claim_scan_directories(scan_id, "second", now=14) == []
    assert catalog.claim_work_items("second", scan_id=scan_id, now=14) == []

    resumed = catalog.resume_expired_leases(scan_id=scan_id, now=16)
    assert resumed == {"scan_dirs": 1, "work_items": 1}
    resumed_directory = catalog.claim_scan_directories(
        scan_id,
        "second",
        lease_seconds=5,
        now=16,
    )
    resumed_work = catalog.claim_work_items(
        "second",
        scan_id=scan_id,
        lease_seconds=5,
        now=16,
    )
    assert [row["id"] for row in resumed_directory] == [scan_directory["id"]]
    assert [row["id"] for row in resumed_work] == [work_item_id]

    catalog.complete_work_item(work_item_id, owner="second", now=17)
    catalog.complete_scan_directory(scan_directory["id"], owner="second", now=17)
    assert catalog.finish_scan(scan_id, now=18) == "complete"
    assert catalog.page_work_items(scan_id=scan_id)[0]["status"] == "complete"
    catalog.close()


def test_scan_cannot_finish_with_durable_work_still_pending(tmp_path):
    catalog, _database_path, root_id, _root_path = create_catalog(tmp_path)
    scan_id = catalog.start_scan([root_id], now=10)

    with pytest.raises(ScanIncompleteError):
        catalog.finish_scan(scan_id, now=11)
    assert catalog.get_scan(scan_id)["status"] == "running"
    catalog.close()


def test_scan_cannot_finish_with_analysis_work_still_pending(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    scan_id, scan_directory = begin_root_scan(catalog, root_id, now=10)
    observation = observe(
        catalog,
        scan_id,
        root_id,
        root_path / "image.jpg",
        b"file-id-1",
        now=10,
    )
    work_item_id = catalog.enqueue_work_item(scan_id, observation.content_version_id, "full_hash", now=10)
    catalog.complete_scan_directory(scan_directory["id"], now=11)

    with pytest.raises(ScanIncompleteError):
        catalog.finish_scan(scan_id, now=12)

    catalog.claim_work_items("worker", scan_id=scan_id, now=13)
    catalog.complete_work_item(work_item_id, owner="worker", now=14)
    assert catalog.finish_scan(scan_id, now=15) == "complete"
    catalog.close()


def test_failed_subtree_is_reported_and_not_tombstoned(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    child_path = root_path / "child"
    direct_file = root_path / "direct.jpg"
    child_file = child_path / "child.jpg"

    first_scan = catalog.start_scan([root_id], now=10)
    catalog.enqueue_directory(first_scan, root_id, child_path, now=10)
    first_directories = catalog.claim_scan_directories(
        first_scan,
        "first",
        limit=2,
        now=10,
    )
    observe(catalog, first_scan, root_id, direct_file, b"direct", now=10)
    observe(catalog, first_scan, root_id, child_file, b"child", now=10)
    for directory in first_directories:
        catalog.complete_scan_directory(directory["id"], owner="first", now=11)
    assert catalog.finish_scan(first_scan, now=12) == "complete"

    second_scan = catalog.start_scan([root_id], now=20)
    second_child = catalog.enqueue_directory(second_scan, root_id, child_path, now=20)
    second_directories = catalog.claim_scan_directories(
        second_scan,
        "second",
        limit=2,
        now=20,
    )
    root_directory = next(row for row in second_directories if row["id"] != second_child)
    catalog.complete_scan_directory(root_directory["id"], owner="second", now=21)
    catalog.fail_scan_directory(
        second_child,
        "scandir",
        "network share disconnected",
        error_code="ENETDOWN",
        transient=True,
        owner="second",
        now=21,
    )
    assert catalog.finish_scan(second_scan, now=22) == "completed_with_errors"

    coverage = catalog.scan_coverage(second_scan)
    assert coverage["complete"] == 1
    assert coverage["unreachable"] == 1
    assert coverage["errors"] == 1
    assert coverage["scan_status"] == "completed_with_errors"
    paths = {
        row["display_path"]: row["path_state"]
        for row in catalog.page_paths(root_id=root_id, states=("active", "missing"))
    }
    assert paths[str(direct_file)] == "missing"
    assert paths[str(child_file)] == "active"
    catalog.close()


def test_error_in_completed_directory_prevents_missing_sweep(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    existing_path = root_path / "existing.jpg"

    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    observe(catalog, first_scan, root_id, existing_path, b"existing", now=10)
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    catalog.record_scan_error(
        second_scan,
        existing_path,
        "stat",
        "permission denied",
        error_code="EACCES",
        scan_dir_id=second_directory["id"],
        now=20,
    )
    catalog.complete_scan_directory(second_directory["id"], now=21)
    assert catalog.finish_scan(second_scan, now=22) == "completed_with_errors"

    path = catalog.page_paths(root_id=root_id, states=("active", "missing"))[0]
    assert path["path_state"] == "active"
    last_complete_scan_id = catalog._connection.execute(
        "SELECT last_complete_scan_id FROM roots WHERE id = ?", (root_id,)
    ).fetchone()[0]
    assert last_complete_scan_id == first_scan
    catalog.close()


def test_page_paths_uses_stable_keyset_cursor(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    scan_id, scan_directory = begin_root_scan(catalog, root_id, now=10)
    for index in range(5):
        observe(
            catalog,
            scan_id,
            root_id,
            root_path / "{}.jpg".format(index),
            "file-id-{}".format(index),
            now=10,
        )
    catalog.complete_scan_directory(scan_directory["id"], now=11)
    catalog.finish_scan(scan_id, now=12)

    first_page = catalog.page_paths(root_id=root_id, limit=2)
    second_page = catalog.page_paths(
        root_id=root_id,
        after_id=first_page[-1]["path_id"],
        limit=2,
    )
    third_page = catalog.page_paths(
        root_id=root_id,
        after_id=second_page[-1]["path_id"],
        limit=2,
    )
    ids = [row["path_id"] for row in first_page + second_page + third_page]
    assert len(ids) == 5
    assert ids == sorted(set(ids))
    catalog.close()


def test_100k_snapshot_changes_remain_keyset_page_bounded(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    seed_path = root_path / "item-000000.bin"
    seed = observe(
        catalog,
        first_scan,
        root_id,
        seed_path,
        b"scale-hardlink",
        now=10,
    )
    root = catalog._connection.execute(
        "SELECT volume_id, path_key FROM roots WHERE id = ?",
        (root_id,),
    ).fetchone()
    display_prefix = str(root_path) + os.sep
    path_key_prefix = root["path_key"] + os.sep
    with catalog.transaction():
        catalog._connection.execute(
            """
            WITH digits(value) AS (
                VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)
            ),
            numbers(value) AS (
                SELECT
                    ones.value
                    + tens.value * 10
                    + hundreds.value * 100
                    + thousands.value * 1000
                    + ten_thousands.value * 10000
                FROM digits AS ones
                CROSS JOIN digits AS tens
                CROSS JOIN digits AS hundreds
                CROSS JOIN digits AS thousands
                CROSS JOIN digits AS ten_thousands
            )
            INSERT INTO scan_path_observations(
                scan_id, root_id, volume_id, path_id, physical_file_id,
                content_version_id, display_path, path_key, parent_path_key,
                path_state, content_state, native_file_id,
                identity_confidence, observed_at
            )
            SELECT
                ?, ?, ?, ?, ?, ?,
                ? || printf('item-%06d.bin', numbers.value),
                ? || printf('item-%06d.bin', numbers.value),
                ?, 'active', 'stable', ?, 'stable', 10
            FROM numbers
            WHERE numbers.value > 0
            """,
            (
                first_scan,
                root_id,
                root["volume_id"],
                seed.path_id,
                seed.physical_file_id,
                seed.content_version_id,
                display_prefix,
                path_key_prefix,
                root["path_key"],
                b"scale-hardlink",
            ),
        )
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    changed = observe(
        catalog,
        second_scan,
        root_id,
        seed_path,
        b"scale-hardlink",
        size=101,
        mtime_ns=2000,
        now=20,
    )
    with catalog.transaction():
        catalog._connection.execute(
            """
            WITH digits(value) AS (
                VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)
            ),
            numbers(value) AS (
                SELECT
                    ones.value
                    + tens.value * 10
                    + hundreds.value * 100
                    + thousands.value * 1000
                    + ten_thousands.value * 10000
                FROM digits AS ones
                CROSS JOIN digits AS tens
                CROSS JOIN digits AS hundreds
                CROSS JOIN digits AS thousands
                CROSS JOIN digits AS ten_thousands
            )
            INSERT INTO scan_path_observations(
                scan_id, root_id, volume_id, path_id, physical_file_id,
                content_version_id, display_path, path_key, parent_path_key,
                path_state, content_state, native_file_id,
                identity_confidence, observed_at
            )
            SELECT
                ?, ?, ?, ?, ?, ?,
                ? || printf('item-%06d.bin', numbers.value),
                ? || printf('item-%06d.bin', numbers.value),
                ?, 'active', 'stable', ?, 'stable', 20
            FROM numbers
            WHERE numbers.value > 0
            """,
            (
                second_scan,
                root_id,
                root["volume_id"],
                changed.path_id,
                changed.physical_file_id,
                changed.content_version_id,
                display_prefix,
                path_key_prefix,
                root["path_key"],
                b"scale-hardlink",
            ),
        )
    catalog.complete_scan_directory(second_directory["id"], now=21)
    catalog.finish_scan(second_scan, now=22)

    assert (
        catalog._connection.execute(
            "SELECT path_count FROM scan_snapshots WHERE scan_id = ?",
            (second_scan,),
        ).fetchone()[0]
        == 100_000
    )
    first_page = catalog.page_scan_changes(
        first_scan,
        second_scan,
        (root_id,),
        limit=257,
    )
    last = first_page[-1]
    second_page = catalog.page_scan_changes(
        first_scan,
        second_scan,
        (root_id,),
        after_root_id=last["sort_root_id"],
        after_path_key=last["sort_path_key"],
        after_change_type=last["change_type"],
        limit=257,
    )

    assert len(first_page) == len(second_page) == 257
    assert all(row["change_type"] == "modified" for row in first_page + second_page)
    assert {row["old_observation_id"] for row in first_page}.isdisjoint(
        row["old_observation_id"] for row in second_page
    )
    catalog.close()


def test_verification_and_action_journal_bind_a_planned_change(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    scan_id, scan_directory = begin_root_scan(catalog, root_id, now=10)
    first = observe(catalog, scan_id, root_id, root_path / "a.jpg", b"a", now=10)
    second = observe(catalog, scan_id, root_id, root_path / "b.jpg", b"b", now=10)
    catalog.complete_scan_directory(scan_directory["id"], now=11)
    catalog.finish_scan(scan_id, now=12)

    verification_id = catalog.record_verification(
        first.content_version_id,
        second.content_version_id,
        "sha256",
        "1",
        b"same-digest",
        state="verified",
        byte_compare_at=13,
        now=13,
    )
    action_id = catalog.journal_action(
        "trash",
        scan_id=scan_id,
        verification_id=verification_id,
        physical_file_id=second.physical_file_id,
        path_id=second.path_id,
        payload={"expected_path": str(root_path / "b.jpg")},
        now=14,
    )
    catalog.update_action(action_id, "completed", now=15)

    action = catalog._connection.execute("SELECT * FROM action_journal WHERE id = ?", (action_id,)).fetchone()
    assert action["status"] == "completed"
    assert action["verification_id"] == verification_id
    assert action["completed_at"] == 15
    catalog.close()


def test_new_content_generation_invalidates_prior_verification(tmp_path):
    catalog, _database_path, root_id, root_path = create_catalog(tmp_path)
    first_scan, first_directory = begin_root_scan(catalog, root_id, now=10)
    first = observe(catalog, first_scan, root_id, root_path / "a.jpg", b"a", now=10)
    second = observe(catalog, first_scan, root_id, root_path / "b.jpg", b"b", now=10)
    catalog.complete_scan_directory(first_directory["id"], now=11)
    catalog.finish_scan(first_scan, now=12)
    verification_id = catalog.record_verification(
        first.content_version_id,
        second.content_version_id,
        "sha256",
        "1",
        b"same-digest",
        state="verified",
        byte_compare_at=13,
        now=13,
    )

    second_scan, second_directory = begin_root_scan(catalog, root_id, now=20)
    changed = observe(
        catalog,
        second_scan,
        root_id,
        root_path / "a.jpg",
        b"a",
        size=101,
        mtime_ns=2000,
        now=20,
    )
    observe(catalog, second_scan, root_id, root_path / "b.jpg", b"b", now=20)
    catalog.complete_scan_directory(second_directory["id"], now=21)
    catalog.finish_scan(second_scan, now=22)

    assert changed.new_content
    state = catalog._connection.execute(
        "SELECT state FROM verification_records WHERE id = ?", (verification_id,)
    ).fetchone()[0]
    assert state == "invalidated"
    catalog.close()
