# Created By: Virgil Dupras
# Created On: 2009-10-23
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os
import sqlite3
from os import urandom

from pathlib import Path
import pytest
from hscommon.testutil import eq_
from core.tests.directories_test import create_fake_fs

from core import fs

hasher = fs.hasher


def test_hash_algorithm_is_the_required_xxhash_implementation():
    assert fs.HASH_ALGORITHM == "xxh128"


def test_hash_cache_invalidates_other_digests_on_generation_change(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"a" * 65536)
    initial_mtime = path.stat().st_mtime_ns
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        first_snapshot = fs._snapshot_path(path)
        database.put(path, "digest", b"old-full", first_snapshot)
        database.put(path, "digest_partial", b"old-partial", first_snapshot)
        database.put(path, "digest_samples", b"old-samples", first_snapshot)

        changed = bytearray(path.read_bytes())
        changed[50000] = ord("b")
        path.write_bytes(changed)
        os.utime(path, ns=(initial_mtime + 1_000_000_000, initial_mtime + 1_000_000_000))
        second_snapshot = fs._snapshot_path(path)
        database.put(path, "digest_partial", b"new-partial", second_snapshot)

        row = database.conn.execute(
            "SELECT digest, digest_partial, digest_samples, algorithm FROM files WHERE path=?",
            (str(path),),
        ).fetchone()
        assert row == (None, b"new-partial", None, fs.HASH_ALGORITHM)
    finally:
        database.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime integration")
def test_hash_cache_misses_same_size_edit_with_restored_mtime(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"first")
    original_stat = os.stat(path, follow_symlinks=False)
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        original_snapshot = fs._snapshot_path(path)
        database.put(path, "digest", b"old-digest", original_snapshot)

        path.write_bytes(b"other")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed_snapshot = fs._snapshot_path(path)

        assert changed_snapshot.size == original_snapshot.size
        assert changed_snapshot.mtime_ns == original_snapshot.mtime_ns
        assert changed_snapshot.device == original_snapshot.device
        assert changed_snapshot.file_id == original_snapshot.file_id
        assert changed_snapshot.ctime_ns != original_snapshot.ctime_ns
        assert database.get(path, "digest") is None
    finally:
        database.close()


def test_hash_cache_invalidates_other_digests_on_algorithm_change(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"contents")
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        snapshot = fs._snapshot_path(path)
        database.put(path, "digest", b"old-full", snapshot)
        monkeypatch.setattr(fs, "HASH_ALGORITHM", "test-algorithm-v2")
        assert database.get(path, "digest") is None
        database.put(path, "digest_partial", b"new-partial", snapshot)

        row = database.conn.execute(
            "SELECT digest, digest_partial, digest_samples, algorithm FROM files WHERE path=?",
            (str(path),),
        ).fetchone()
        assert row == (None, b"new-partial", None, "test-algorithm-v2")
    finally:
        database.close()


def test_hash_cache_generation_and_digest_update_are_atomic(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"original")
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        original_snapshot = fs._snapshot_path(path)
        database.put(path, "digest", b"old-full", original_snapshot)
        original_row = database.conn.execute(
            """SELECT device, file_id, size, mtime_ns, ctime_ns, algorithm,
                digest, digest_partial, digest_samples
                FROM files WHERE path=?""",
            (str(path),),
        ).fetchone()

        path.write_bytes(b"replacement")
        replacement_snapshot = fs._snapshot_path(path)
        database.update_digest_query = "UPDATE table_that_does_not_exist SET {key}=:value"
        database.put(path, "digest_partial", b"new-partial", replacement_snapshot)

        row_after_failed_put = database.conn.execute(
            """SELECT device, file_id, size, mtime_ns, ctime_ns, algorithm,
                digest, digest_partial, digest_samples
                FROM files WHERE path=?""",
            (str(path),),
        ).fetchone()
        assert row_after_failed_put == original_row
    finally:
        database.close()


def test_hash_cache_rejects_unknown_digest_column(tmp_path):
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        with pytest.raises(ValueError):
            database.get(tmp_path / "data.bin", "not_a_digest")
    finally:
        database.close()


def test_hash_cache_rejects_symlink_alias_without_changing_source(tmp_path):
    source = tmp_path / "important.bin"
    source.write_bytes(b"important source bytes")
    alias = tmp_path / "hashes.db"
    try:
        alias.symlink_to(source)
    except (NotImplementedError, OSError) as error:
        pytest.skip("file symlinks are unavailable: {}".format(error))

    with pytest.raises(fs.HashCacheSafetyError):
        fs.FilesDB().connect(alias)

    assert source.read_bytes() == b"important source bytes"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_hash_cache_rejects_hardlink_alias_without_changing_source(tmp_path):
    source = tmp_path / "important.bin"
    source.write_bytes(b"important source bytes")
    alias = tmp_path / "hashes.db"
    try:
        os.link(source, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    with pytest.raises(fs.HashCacheSafetyError):
        fs.FilesDB().connect(alias)

    assert source.read_bytes() == b"important source bytes"
    assert alias.read_bytes() == b"important source bytes"


def test_hash_cache_rejects_empty_arbitrary_file_without_replacing_it(tmp_path):
    candidate = tmp_path / "hashes.db"
    candidate.touch()
    before = candidate.stat()

    with pytest.raises(fs.HashCacheSafetyError):
        fs.FilesDB().connect(candidate)

    after = candidate.stat()
    assert candidate.read_bytes() == b""
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_hash_cache_rejects_unowned_sqlite_before_parser_without_modifying_it(tmp_path, monkeypatch):
    candidate = tmp_path / "hashes.db"
    connection = sqlite3.connect(candidate)
    connection.execute("CREATE TABLE user_data(value TEXT)")
    connection.execute("INSERT INTO user_data VALUES ('keep me')")
    connection.commit()
    connection.close()
    before = candidate.read_bytes()
    real_connect = sqlite3.connect

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("an unmarked SQLite file reached sqlite3.connect")

    monkeypatch.setattr(fs.sqlite3, "connect", parser_must_not_run)

    with pytest.raises(fs.HashCacheSafetyError, match="owner marker"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = real_connect(candidate)
    try:
        assert check.execute("SELECT value FROM user_data").fetchone() == ("keep me",)
        assert check.execute("PRAGMA application_id").fetchone()[0] == 0
    finally:
        check.close()


def _create_legacy_hash_cache(path):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE schema_version (version int PRIMARY KEY, description TEXT)")
        connection.execute("""CREATE TABLE files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime_ns INTEGER,
                entry_dt DATETIME,
                digest BLOB,
                digest_partial BLOB,
                digest_samples BLOB
            )""")
        connection.execute(
            "INSERT INTO schema_version(version, description) VALUES (?, ?)",
            (1, "Changed from md5 to xxhash if available."),
        )
        connection.commit()
    finally:
        connection.close()


def test_hash_cache_never_migrates_or_marks_an_unmarked_legacy_cache(tmp_path):
    candidate = tmp_path / "hash_cache.db"
    _create_legacy_hash_cache(candidate)
    before = candidate.read_bytes()

    with pytest.raises(fs.HashCacheSafetyError, match="owner marker"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = sqlite3.connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == 0
        assert check.execute("SELECT version, description FROM schema_version").fetchone() == (
            1,
            "Changed from md5 to xxhash if available.",
        )
    finally:
        check.close()


@pytest.mark.parametrize(
    "unexpected_sql",
    (
        "CREATE TABLE unexpected(value TEXT)",
        "CREATE VIEW unexpected AS SELECT path FROM files",
        "CREATE INDEX unexpected ON files(size)",
        "CREATE TRIGGER unexpected AFTER INSERT ON files BEGIN SELECT 1; END",
    ),
)
def test_hash_cache_rejects_every_unexpected_schema_object_without_modifying_it(tmp_path, unexpected_sql):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    connection = sqlite3.connect(candidate)
    connection.execute(unexpected_sql)
    connection.commit()
    connection.close()
    before = candidate.read_bytes()

    with pytest.raises(fs.HashCacheSafetyError, match="owned hash cache"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = sqlite3.connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == fs._HASH_CACHE_APPLICATION_ID
        assert check.execute("SELECT name FROM sqlite_schema WHERE name = 'unexpected'").fetchone() == ("unexpected",)
    finally:
        check.close()


def test_hash_cache_rejects_wrong_owned_description_without_modifying_it(tmp_path):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    connection = sqlite3.connect(candidate)
    connection.execute("UPDATE schema_version SET description = 'unrelated schema'")
    connection.commit()
    connection.close()
    before = candidate.read_bytes()

    with pytest.raises(fs.HashCacheSafetyError, match="schema history"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = sqlite3.connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == fs._HASH_CACHE_APPLICATION_ID
        assert check.execute("SELECT description FROM schema_version").fetchone() == ("unrelated schema",)
    finally:
        check.close()


def test_hash_cache_rejects_another_application_id_before_parser_without_modifying_it(tmp_path, monkeypatch):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    connection = sqlite3.connect(candidate)
    connection.execute("PRAGMA application_id = 305419896")
    connection.commit()
    connection.close()
    before = candidate.read_bytes()
    real_connect = sqlite3.connect

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("a differently marked SQLite file reached sqlite3.connect")

    monkeypatch.setattr(fs.sqlite3, "connect", parser_must_not_run)

    with pytest.raises(fs.HashCacheSafetyError, match="owner marker"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = real_connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == 305419896
    finally:
        check.close()


def test_hash_cache_rejects_an_unmarked_current_schema_without_modifying_it(tmp_path):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    connection = sqlite3.connect(candidate)
    connection.execute("PRAGMA application_id = 0")
    connection.commit()
    connection.close()
    before = candidate.read_bytes()

    with pytest.raises(fs.HashCacheSafetyError, match="owner marker"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = sqlite3.connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == 0
    finally:
        check.close()


@pytest.mark.parametrize(
    "files_ddl",
    (
        """CREATE TABLE files (
            path TEXT PRIMARY KEY,
            device TEXT NOT NULL,
            file_id TEXT NOT NULL,
            size INTEGER NOT NULL CHECK(size >= 0),
            mtime_ns INTEGER NOT NULL,
            ctime_ns BLOB NOT NULL,
            algorithm TEXT NOT NULL,
            entry_dt DATETIME,
            digest BLOB,
            digest_partial BLOB,
            digest_samples BLOB
        )""",
        """CREATE TABLE files (
            path TEXT PRIMARY KEY,
            device TEXT NOT NULL,
            file_id TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            ctime_ns BLOB NOT NULL,
            algorithm TEXT NOT NULL,
            entry_dt DATETIME,
            digest BLOB,
            digest_partial BLOB,
            digest_samples BLOB,
            hidden_value TEXT GENERATED ALWAYS AS (path) VIRTUAL
        )""",
    ),
)
def test_hash_cache_rejects_extra_constraints_or_hidden_columns_without_modifying_it(tmp_path, files_ddl):
    candidate = tmp_path / "hashes.db"
    connection = sqlite3.connect(candidate)
    connection.execute("CREATE TABLE schema_version (version int PRIMARY KEY, description TEXT)")
    connection.execute(files_ddl)
    connection.execute(
        "INSERT INTO schema_version VALUES (?, ?)",
        (fs.FilesDB.schema_version, fs.FilesDB.schema_version_description),
    )
    connection.execute("PRAGMA application_id = {}".format(fs._HASH_CACHE_APPLICATION_ID))
    connection.commit()
    connection.close()
    before = candidate.read_bytes()

    with pytest.raises(fs.HashCacheSafetyError):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before
    check = sqlite3.connect(candidate)
    try:
        assert check.execute("PRAGMA application_id").fetchone()[0] == fs._HASH_CACHE_APPLICATION_ID
    finally:
        check.close()


def test_hash_cache_rejects_invalid_page_size_before_parser_without_modifying_it(tmp_path, monkeypatch):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    corrupted = bytearray(candidate.read_bytes())
    corrupted[16:18] = b"\x00\x03"
    candidate.write_bytes(corrupted)
    before = candidate.read_bytes()

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("an invalid SQLite header reached sqlite3.connect")

    monkeypatch.setattr(fs.sqlite3, "connect", parser_must_not_run)

    with pytest.raises(fs.HashCacheSafetyError, match="page size"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before


def test_hash_cache_rejects_oversize_header_before_parser_without_modifying_it(tmp_path, monkeypatch):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    before = candidate.read_bytes()
    monkeypatch.setattr(fs, "_HASH_CACHE_MAX_FILE_BYTES", len(before) - 1)

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("an oversized SQLite file reached sqlite3.connect")

    monkeypatch.setattr(fs.sqlite3, "connect", parser_must_not_run)

    with pytest.raises(fs.HashCacheSafetyError, match="file size"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == before


def test_hash_cache_revalidates_the_actual_writable_connection_before_mutation(tmp_path, monkeypatch):
    candidate = tmp_path / "hashes.db"
    database = fs.FilesDB()
    database.connect(candidate)
    database.close()
    foreign = tmp_path / "foreign.db"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE user_data(value TEXT)")
    connection.execute("INSERT INTO user_data VALUES ('keep me')")
    connection.commit()
    connection.close()
    candidate_before = candidate.read_bytes()
    foreign_before = foreign.read_bytes()
    real_connect = sqlite3.connect

    def redirect_writable_reopen(database_path, *args, **kwargs):
        if kwargs.get("uri"):
            return real_connect(database_path, *args, **kwargs)
        return real_connect(foreign, *args, **kwargs)

    monkeypatch.setattr(fs.sqlite3, "connect", redirect_writable_reopen)

    with pytest.raises(fs.HashCacheSafetyError, match="owner marker"):
        fs.FilesDB().connect(candidate)

    assert candidate.read_bytes() == candidate_before
    assert foreign.read_bytes() == foreign_before
    check = real_connect(foreign)
    try:
        assert check.execute("SELECT value FROM user_data").fetchone() == ("keep me",)
        assert check.execute("PRAGMA application_id").fetchone()[0] == 0
    finally:
        check.close()


def test_hash_cache_rejects_linked_parent_without_creating_database(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))
    candidate = linked_parent / "hashes.db"

    with pytest.raises(fs.HashCacheSafetyError):
        fs.FilesDB().connect(candidate)

    assert not (real_parent / "hashes.db").exists()


@pytest.mark.parametrize("suffix", ("-journal", "-wal", "-shm"))
def test_hash_cache_rejects_preexisting_sqlite_sidecar(tmp_path, suffix):
    candidate = tmp_path / "hashes.db"
    sidecar = Path("{}{}".format(candidate, suffix))
    sidecar.write_bytes(b"do not overwrite")

    with pytest.raises(fs.HashCacheSafetyError, match="sidecar"):
        fs.FilesDB().connect(candidate)

    assert not candidate.exists()
    assert sidecar.read_bytes() == b"do not overwrite"


def test_owned_hash_cache_reopens_after_clean_close(tmp_path):
    candidate = tmp_path / "hashes.db"
    first = fs.FilesDB()
    first.connect(candidate)
    first.close()

    second = fs.FilesDB()
    second.connect(candidate)
    try:
        assert second.conn.execute("PRAGMA application_id").fetchone()[0] != 0
    finally:
        second.close()


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "getlimit"), reason="CPython sqlite3_limit wrapper is unavailable")
def test_hash_cache_lowers_sqlite_parser_and_value_limits(tmp_path):
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    try:
        expected_limits = {
            sqlite3.SQLITE_LIMIT_LENGTH: 1024 * 1024,
            sqlite3.SQLITE_LIMIT_SQL_LENGTH: 64 * 1024,
            sqlite3.SQLITE_LIMIT_COLUMN: 64,
            sqlite3.SQLITE_LIMIT_EXPR_DEPTH: 64,
            sqlite3.SQLITE_LIMIT_COMPOUND_SELECT: 16,
            sqlite3.SQLITE_LIMIT_VDBE_OP: 100_000,
            sqlite3.SQLITE_LIMIT_FUNCTION_ARG: 32,
            sqlite3.SQLITE_LIMIT_ATTACHED: 0,
            sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH: 4096,
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER: 128,
            sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH: 0,
            sqlite3.SQLITE_LIMIT_WORKER_THREADS: 0,
        }
        for category, maximum in expected_limits.items():
            assert database.conn.getlimit(category) <= maximum
    finally:
        database.close()


def test_hash_cache_strict_read_propagates_database_failure(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"contents")
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    database.select_query = "SELECT digest FROM missing_hash_cache_table"
    try:
        with pytest.raises(sqlite3.DatabaseError):
            database.get_strict(path, "digest")
        assert database.get(path, "digest") is None
    finally:
        database.close()


def test_file_rename_uses_no_replace_boundary_when_destination_appears(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source payload")

    class DestinationWinsRace:
        def rename_no_replace(self, source_path, destination_path):
            destination_path.write_bytes(b"concurrent winner")
            raise FileExistsError(str(destination_path))

    from core import safe_action

    monkeypatch.setattr(
        safe_action,
        "platform_file_system",
        lambda: DestinationWinsRace(),
    )
    file = fs.File(source)

    with pytest.raises(fs.AlreadyExistsError):
        file.rename(destination.name)

    assert source.read_bytes() == b"source payload"
    assert destination.read_bytes() == b"concurrent winner"
    assert file.path == source


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "",
        ".",
        "..",
        "../outside.bin",
        r"..\outside.bin",
        "/absolute.bin",
        r"C:\absolute.bin",
        "C:drive-relative.bin",
        "payload.bin:alternate-stream",
        "nested/name.bin",
        r"nested\name.bin",
        "nul\0name.bin",
    ),
)
def test_file_rename_rejects_every_non_leaf_name_without_mutation(tmp_path, unsafe_name):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source payload")
    file = fs.File(source)

    with pytest.raises(fs.InvalidPath):
        file.rename(unsafe_name)

    assert source.read_bytes() == b"source payload"
    assert file.path == source


def test_file_rename_accepts_a_plain_leaf_name(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "renamed.bin"
    source.write_bytes(b"source payload")
    file = fs.File(source)

    file.rename(destination.name)

    assert not source.exists()
    assert destination.read_bytes() == b"source payload"
    assert file.path == destination


def test_compare_bytes_returns_stable_evidence(tmp_path):
    first_path = tmp_path / "first.bin"
    second_path = tmp_path / "second.bin"
    first_path.write_bytes(b"verified contents")
    second_path.write_bytes(b"verified contents")
    evidence = fs.File(first_path).compare_bytes(fs.File(second_path))
    assert evidence.bytes_compared == len(b"verified contents")
    assert evidence.first.size == evidence.second.size
    second_path.write_bytes(b"different content")
    assert fs.File(first_path).compare_bytes(fs.File(second_path)) is None


def test_hashing_and_byte_comparison_never_follow_symlinks(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside payload")
    first_link = tmp_path / "first-link.bin"
    second_link = tmp_path / "second-link.bin"
    try:
        first_link.symlink_to(outside)
        second_link.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip("file symlinks are unavailable: {}".format(error))

    with pytest.raises(OSError):
        fs._snapshot_path(first_link)
    with pytest.raises(OSError):
        fs.File(first_link)._calc_digest_with_snapshot()
    with pytest.raises(OSError):
        fs.File(first_link).compare_bytes(fs.File(second_link))


def test_hashing_rejects_a_symlinked_parent_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("payload.bin").write_bytes(b"outside payload")
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))

    with pytest.raises(OSError):
        fs.File(linked_parent / "payload.bin")._calc_digest_with_snapshot()


def test_hashing_fails_closed_when_handle_generation_changes(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"contents")
    snapshot = fs._snapshot_path(path)
    changed_snapshot = fs.FileSnapshot(
        device=snapshot.device,
        file_id=snapshot.file_id,
        size=snapshot.size,
        mtime_ns=snapshot.mtime_ns + 1,
        ctime_ns=snapshot.ctime_ns,
    )
    # The outer no-follow context and the digest loop independently capture
    # handle generations before and after reading.
    snapshots = iter((snapshot, snapshot, changed_snapshot, changed_snapshot))
    monkeypatch.setattr(fs, "_snapshot_handle", lambda fp, path=None: next(snapshots))
    with pytest.raises(fs.FileChangedError):
        fs.File(path)._calc_digest_with_snapshot()


def test_cached_digest_rejects_a_ctime_generation_change(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"contents")
    snapshot = fs._snapshot_path(path)
    changed_snapshot = fs.FileSnapshot(
        device=snapshot.device,
        file_id=snapshot.file_id,
        size=snapshot.size,
        mtime_ns=snapshot.mtime_ns,
        ctime_ns=snapshot.ctime_ns + b"-changed",
    )

    assert not snapshot.same_content_generation(changed_snapshot)


def create_fake_fs_with_random_data(rootpath):
    rootpath = rootpath.joinpath("fs")
    rootpath.mkdir()
    rootpath.joinpath("dir1").mkdir()
    rootpath.joinpath("dir2").mkdir()
    rootpath.joinpath("dir3").mkdir()
    data1 = urandom(200 * 1024)  # 200KiB
    data2 = urandom(1024 * 1024)  # 1MiB
    data3 = urandom(10 * 1024 * 1024)  # 10MiB
    with rootpath.joinpath("file1.test").open("wb") as fp:
        fp.write(data1)
    with rootpath.joinpath("file2.test").open("wb") as fp:
        fp.write(data2)
    with rootpath.joinpath("file3.test").open("wb") as fp:
        fp.write(data3)
    with rootpath.joinpath("dir1", "file1.test").open("wb") as fp:
        fp.write(data1)
    with rootpath.joinpath("dir2", "file2.test").open("wb") as fp:
        fp.write(data2)
    with rootpath.joinpath("dir3", "file3.test").open("wb") as fp:
        fp.write(data3)
    return rootpath


def test_size_aggregates_subfiles(tmpdir):
    p = create_fake_fs(Path(str(tmpdir)))
    b = fs.Folder(p)
    eq_(b.size, 12)


def test_digest_aggregate_subfiles_sorted(tmpdir):
    # dir.allfiles can return child in any order. Thus, bundle.digest must aggregate
    # all files' digests it contains, but it must make sure that it does so in the
    # same order everytime.
    p = create_fake_fs_with_random_data(Path(str(tmpdir)))
    b = fs.Folder(p)
    digest1 = fs.File(p.joinpath("dir1", "file1.test")).digest
    digest2 = fs.File(p.joinpath("dir2", "file2.test")).digest
    digest3 = fs.File(p.joinpath("dir3", "file3.test")).digest
    digest4 = fs.File(p.joinpath("file1.test")).digest
    digest5 = fs.File(p.joinpath("file2.test")).digest
    digest6 = fs.File(p.joinpath("file3.test")).digest
    # The expected digest is the hash of digests for folders and the direct digest for files
    folder_digest1 = hasher(digest1).digest()
    folder_digest2 = hasher(digest2).digest()
    folder_digest3 = hasher(digest3).digest()
    digest = hasher(folder_digest1 + folder_digest2 + folder_digest3 + digest4 + digest5 + digest6).digest()
    eq_(b.digest, digest)


def test_partial_digest_aggregate_subfile_sorted(tmpdir):
    p = create_fake_fs_with_random_data(Path(str(tmpdir)))
    b = fs.Folder(p)
    digest1 = fs.File(p.joinpath("dir1", "file1.test")).digest_partial
    digest2 = fs.File(p.joinpath("dir2", "file2.test")).digest_partial
    digest3 = fs.File(p.joinpath("dir3", "file3.test")).digest_partial
    digest4 = fs.File(p.joinpath("file1.test")).digest_partial
    digest5 = fs.File(p.joinpath("file2.test")).digest_partial
    digest6 = fs.File(p.joinpath("file3.test")).digest_partial
    # The expected digest is the hash of digests for folders and the direct digest for files
    folder_digest1 = hasher(digest1).digest()
    folder_digest2 = hasher(digest2).digest()
    folder_digest3 = hasher(digest3).digest()
    digest = hasher(folder_digest1 + folder_digest2 + folder_digest3 + digest4 + digest5 + digest6).digest()
    eq_(b.digest_partial, digest)

    digest1 = fs.File(p.joinpath("dir1", "file1.test")).digest_samples
    digest2 = fs.File(p.joinpath("dir2", "file2.test")).digest_samples
    digest3 = fs.File(p.joinpath("dir3", "file3.test")).digest_samples
    digest4 = fs.File(p.joinpath("file1.test")).digest_samples
    digest5 = fs.File(p.joinpath("file2.test")).digest_samples
    digest6 = fs.File(p.joinpath("file3.test")).digest_samples
    # The expected digest is the digest of digests for folders and the direct digest for files
    folder_digest1 = hasher(digest1).digest()
    folder_digest2 = hasher(digest2).digest()
    folder_digest3 = hasher(digest3).digest()
    digest = hasher(folder_digest1 + folder_digest2 + folder_digest3 + digest4 + digest5 + digest6).digest()
    eq_(b.digest_samples, digest)


def test_has_file_attrs(tmpdir):
    # a Folder must behave like a file, so it must have mtime attributes
    b = fs.Folder(Path(str(tmpdir)))
    assert b.mtime > 0
    eq_(b.extension, "")
