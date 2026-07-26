import os
import sqlite3
import tracemalloc
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.pe.cache_sqlite as cache_sqlite
from core.app import DupeGuru
from core.file_generation import FileGenerationToken, get_file_generation_token
from core.file_identity import get_file_identity
from core.pe.cache_sqlite import (
    CacheSafetyError,
    CacheSchemaError,
    SCHEMA_VERSION,
    SQLITE_APPLICATION_ID,
    SQLITE_APPLICATION_ID_BYTES,
    SQLITE_APPLICATION_ID_OFFSET,
    SQLITE_HEADER,
    SqliteCache,
    capture_source_binding,
    validate_sqlite_cache_location,
)
from core.pe.image_features import ImageFeatures, ImageQuality


def _features():
    return ImageFeatures(
        dimensions=(40, 20),
        frame_count=1,
        blocks=(((1, 2, 3), (4, 5, 6)),),
        phashes=(1,),
        dhashes=(1,),
        color_histogram=(1024,) + (0,) * 63,
        tile_fingerprints=(),
        quality=ImageQuality(8, 0, 0, 0.0),
        thumbnail_png=b"\x89PNG\r\ncache-test",
        thumbnail_size=(32, 16),
        thumbnail_key="thumbnail-key",
    )


def _create_cache(path):
    cache = SqliteCache(path)
    cache.close()


def _make_directory_symlink(target, alias):
    try:
        os.symlink(target, alias, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip("directory symlinks are unavailable: {}".format(error))


def test_new_database_has_exclusive_single_link_sqlite_schema(tmp_path):
    database = tmp_path / "cache.sqlite3"

    _create_cache(database)

    database_stat = os.lstat(database)
    assert database_stat.st_nlink == 1
    assert database.read_bytes().startswith(SQLITE_HEADER)
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    assert connection.execute("PRAGMA application_id").fetchone() == (SQLITE_APPLICATION_ID,)
    assert connection.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(pictures)"))
    connection.close()
    header = database.read_bytes()[:100]
    assert header[SQLITE_APPLICATION_ID_OFFSET : SQLITE_APPLICATION_ID_OFFSET + 4] == SQLITE_APPLICATION_ID_BYTES
    assert columns[-3:] == ("ctime_ns", "identity_json", "generation_token")


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"),
    reason="sqlite3 runtime limits are unavailable",
)
def test_connections_apply_bounded_sqlite_runtime_and_query_only_modes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    writable = SqliteCache(database)

    for constant_name, maximum in cache_sqlite._SQLITE_CONNECTION_LIMITS:
        category = getattr(sqlite3, constant_name)
        assert writable.con.getlimit(category) <= maximum
    assert writable.con.execute("PRAGMA trusted_schema").fetchone() == (0,)
    assert writable.con.execute("PRAGMA query_only").fetchone() == (0,)
    writable.close()

    readonly = SqliteCache(database, readonly=True)
    assert readonly.con.execute("PRAGMA trusted_schema").fetchone() == (0,)
    assert readonly.con.execute("PRAGMA query_only").fetchone() == (1,)
    readonly.close()


def test_existing_picture_cache_is_validated_read_only_before_writable_open(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    real_connect = sqlite3.connect
    calls = []

    def recording_connect(target, *args, **kwargs):
        calls.append((os.fspath(target), dict(kwargs)))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(cache_sqlite.sqlite, "connect", recording_connect)

    cache = SqliteCache(database)

    assert len(calls) == 2
    assert calls[0][1].get("uri") is True
    assert "mode=ro" in calls[0][0]
    assert "immutable=1" in calls[0][0]
    assert calls[1][0] == str(database)
    assert calls[1][1].get("uri") is not True
    cache.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows path leases use native no-delete sharing")
def test_writable_reopen_leases_verified_path_against_replacement(tmp_path, monkeypatch):
    database = tmp_path / "cache.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _create_cache(database)
    replacement.write_bytes(database.read_bytes())
    real_connect = sqlite3.connect
    replacement_attempted = False
    replacement_blocked = False

    def racing_connect(target, *args, **kwargs):
        nonlocal replacement_attempted, replacement_blocked
        if not replacement_attempted and os.fspath(target) == str(database) and kwargs.get("uri") is not True:
            replacement_attempted = True
            try:
                os.replace(replacement, database)
            except OSError as error:
                replacement_blocked = getattr(error, "winerror", None) in {5, 32}
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(cache_sqlite.sqlite, "connect", racing_connect)

    cache = SqliteCache(database)

    assert replacement_attempted
    assert replacement_blocked
    assert replacement.exists()
    cache.close()


def test_new_database_exclusive_create_does_not_overwrite_racing_file(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "cache.sqlite3"
    marker = b"user file created during validation"
    real_open = cache_sqlite.os.open
    injected = False

    def racing_open(path, flags, mode=0o777):
        nonlocal injected
        if (
            not injected
            and os.path.abspath(os.fspath(path)) == os.path.abspath(os.fspath(database))
            and flags & os.O_EXCL
        ):
            injected = True
            database.write_bytes(marker)
        return real_open(path, flags, mode)

    monkeypatch.setattr(cache_sqlite.os, "open", racing_open)

    with pytest.raises(CacheSafetyError, match="exclusive creation"):
        SqliteCache(database)

    assert injected
    assert database.read_bytes() == marker


def test_database_inside_input_root_is_rejected_before_creation(tmp_path):
    input_root = tmp_path / "pictures"
    input_root.mkdir()
    database = input_root / "cache.sqlite3"

    with pytest.raises(CacheSafetyError, match="outside every visual input root"):
        SqliteCache(database, input_roots=(input_root,))

    assert not database.exists()


def test_database_inside_canonical_input_root_alias_is_rejected_before_creation(
    tmp_path,
):
    storage = tmp_path / "storage"
    storage.mkdir()
    input_alias = tmp_path / "pictures"
    _make_directory_symlink(storage, input_alias)
    database = storage / "cache.sqlite3"

    with pytest.raises(CacheSafetyError, match="outside every visual input root"):
        SqliteCache(database, input_roots=(input_alias,))

    assert not database.exists()


def test_database_physical_identity_cannot_alias_captured_input(tmp_path):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    original = database.read_bytes()
    database_identity = get_file_identity(database, follow_symlinks=False)

    with pytest.raises(CacheSafetyError, match="aliases a captured visual input"):
        validate_sqlite_cache_location(
            database,
            input_identities=(database_identity,),
        )

    assert database.read_bytes() == original


def test_database_parent_symlink_is_rejected_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "linked-parent"
    _make_directory_symlink(outside, alias)

    with pytest.raises(CacheSafetyError, match="plain directories"):
        SqliteCache(alias / "cache.sqlite3")

    assert not (outside / "cache.sqlite3").exists()


def test_database_parent_reparse_point_is_rejected_before_creation(
    tmp_path,
    monkeypatch,
):
    parent = tmp_path / "cache-parent"
    parent.mkdir()
    parent_stat = os.lstat(parent)
    real_is_reparse_point = cache_sqlite.is_reparse_point

    def injected_reparse(item):
        if getattr(item, "st_dev", None) == parent_stat.st_dev and getattr(item, "st_ino", None) == parent_stat.st_ino:
            return True
        return real_is_reparse_point(item)

    monkeypatch.setattr(cache_sqlite, "is_reparse_point", injected_reparse)
    database = parent / "cache.sqlite3"

    with pytest.raises(CacheSafetyError, match="plain directories"):
        SqliteCache(database)

    assert not database.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_existing_database_hardlink_is_rejected_without_writes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    alias = tmp_path / "cache-alias.sqlite3"
    _create_cache(database)
    try:
        os.link(database, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))
    original = database.read_bytes()

    with pytest.raises(CacheSafetyError, match="exactly one filesystem link"):
        SqliteCache(alias)

    assert database.read_bytes() == original
    assert alias.read_bytes() == original


def test_valid_sqlite_with_unknown_objects_is_rejected_without_writes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE foreign_data(value TEXT)")
    connection.commit()
    connection.close()
    original = database.read_bytes()

    with pytest.raises(CacheSafetyError, match="unsupported object set"):
        SqliteCache(database)

    assert database.read_bytes() == original


def test_same_columns_with_modified_table_sql_are_rejected_without_writes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    connection = sqlite3.connect(database)
    original_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='pictures'"
    ).fetchone()[0]
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='pictures'",
        (original_sql[:-1] + ", CHECK (file_size >= 0))",),
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()
    original = database.read_bytes()

    with pytest.raises(CacheSchemaError, match="schema SQL"):
        SqliteCache(database)

    assert database.read_bytes() == original


def test_exact_schema_without_picture_cache_application_id_is_rejected_raw(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA application_id = 0")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    sqlite_connected = False

    def unexpected_connect(*_args, **_kwargs):
        nonlocal sqlite_connected
        sqlite_connected = True
        raise AssertionError("foreign SQLite files must be rejected before connect")

    monkeypatch.setattr(
        cache_sqlite.sqlite,
        "connect",
        unexpected_connect,
    )

    with pytest.raises(CacheSchemaError, match="not owned"):
        SqliteCache(database)

    assert not sqlite_connected
    assert database.read_bytes() == original


def test_unmarked_schema_three_is_rejected_without_writes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE schema_version(" "version INTEGER PRIMARY KEY, description TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES(3, 'normalized image features')")
    connection.execute(
        "CREATE TABLE pictures("
        "path TEXT NOT NULL, mtime_ns INTEGER NOT NULL, "
        "file_size INTEGER NOT NULL, "
        "blocks BLOB, blocks2 BLOB, blocks3 BLOB, blocks4 BLOB, "
        "blocks5 BLOB, blocks6 BLOB, blocks7 BLOB, blocks8 BLOB, "
        "width INTEGER, height INTEGER, frame_count INTEGER, "
        "phashes BLOB, phash_count INTEGER, thumbnail BLOB, "
        "thumbnail_width INTEGER, thumbnail_height INTEGER, "
        "thumbnail_key TEXT, feature_version TEXT)"
    )
    connection.execute("CREATE UNIQUE INDEX idx_path ON pictures(path)")
    connection.execute("INSERT INTO pictures(path,mtime_ns,file_size) VALUES('old.png',0,0)")
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()
    original = database.read_bytes()

    with pytest.raises(CacheSchemaError, match="not owned"):
        SqliteCache(database)

    assert database.read_bytes() == original
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    assert connection.execute("SELECT path FROM pictures").fetchone() == ("old.png",)
    connection.close()


def test_unmarked_schema_four_is_rejected_without_writes(tmp_path):
    database = tmp_path / "cache.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE schema_version(" "version INTEGER PRIMARY KEY, description TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES(4, 'identity-bound image features')")
    connection.execute(
        "CREATE TABLE pictures("
        "path TEXT NOT NULL, mtime_ns INTEGER NOT NULL, "
        "file_size INTEGER NOT NULL, "
        "blocks BLOB, blocks2 BLOB, blocks3 BLOB, blocks4 BLOB, "
        "blocks5 BLOB, blocks6 BLOB, blocks7 BLOB, blocks8 BLOB, "
        "width INTEGER, height INTEGER, frame_count INTEGER, "
        "phashes BLOB, phash_count INTEGER, thumbnail BLOB, "
        "thumbnail_width INTEGER, thumbnail_height INTEGER, "
        "thumbnail_key TEXT, feature_version TEXT, "
        "ctime_ns INTEGER, identity_json TEXT)"
    )
    connection.execute("CREATE UNIQUE INDEX idx_path ON pictures(path)")
    connection.execute("INSERT INTO pictures(path,mtime_ns,file_size,ctime_ns) " "VALUES('old.png',0,0,0)")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()
    original = database.read_bytes()

    with pytest.raises(CacheSchemaError, match="not owned"):
        SqliteCache(database)

    assert database.read_bytes() == original
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone() == (4,)
    assert connection.execute("SELECT path FROM pictures").fetchone() == ("old.png",)
    connection.close()


def test_same_path_size_and_mtime_replacement_never_reuses_features(tmp_path):
    source = tmp_path / "picture.png"
    displaced = tmp_path / "displaced.png"
    source.write_bytes(b"first")
    original_stat = source.stat()
    original_identity = get_file_identity(source, follow_symlinks=False)
    cache = SqliteCache(tmp_path / "cache.sqlite3")
    cache.put_features(source, _features())

    source.rename(displaced)
    source.write_bytes(b"other")
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert source.stat().st_size == original_stat.st_size
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert get_file_identity(source, follow_symlinks=False) != original_identity
    with pytest.raises(KeyError):
        cache.get_features(source)

    cache.purge_outdated()
    assert os.path.abspath(os.fspath(source)) not in cache
    cache.close()


def test_same_file_size_and_restored_mtime_edit_never_reuses_features(tmp_path):
    source = tmp_path / "picture.png"
    source.write_bytes(b"first")
    original_stat = source.stat()
    original_generation = get_file_generation_token(
        source,
        follow_symlinks=False,
        stat_result=original_stat,
        expected_identity=get_file_identity(
            source,
            follow_symlinks=False,
            stat_result=original_stat,
        ),
    )
    cache = SqliteCache(tmp_path / "cache.sqlite3")
    cache.put_features(source, _features())

    source.write_bytes(b"other")
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert source.stat().st_size == original_stat.st_size
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert get_file_generation_token(source, follow_symlinks=False) != original_generation
    with pytest.raises(KeyError):
        cache.get_features(source)

    cache.purge_outdated()
    assert os.path.abspath(os.fspath(source)) not in cache
    cache.close()


def test_conditional_feature_write_rejects_changed_expected_binding(tmp_path):
    source = tmp_path / "picture.png"
    source.write_bytes(b"first")
    expected = capture_source_binding(source)
    original_stat = source.stat()

    source.write_bytes(b"other")
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    cache = SqliteCache(tmp_path / "cache.sqlite3")

    with pytest.raises(CacheSafetyError, match="changed before"):
        cache.put_features(
            source,
            _features(),
            expected_binding=expected,
        )

    assert os.path.abspath(os.fspath(source)) not in cache
    cache.close()


@pytest.mark.parametrize(
    "features",
    (
        replace(
            _features(),
            blocks=(tuple((1, 2, 3) for _ in range(cache_sqlite.MAX_CACHE_BLOCK_BYTES // 3 + 1)),),
        ),
        replace(
            _features(),
            thumbnail_png=b"x" * (cache_sqlite.MAX_CACHE_THUMBNAIL_BYTES + 1),
        ),
        replace(
            _features(),
            thumbnail_size=(cache_sqlite.MAX_CACHE_THUMBNAIL_EDGE + 1, 16),
        ),
        replace(
            _features(),
            thumbnail_key="x" * (cache_sqlite.MAX_CACHE_THUMBNAIL_KEY_CHARACTERS + 1),
        ),
    ),
)
def test_feature_writer_rejects_payloads_its_reader_would_reject(
    tmp_path,
    features,
):
    source = tmp_path / "picture.png"
    source.write_bytes(b"content")
    cache = SqliteCache(tmp_path / "cache.sqlite3")

    with pytest.raises(ValueError, match="image cache"):
        cache.put_features(source, features)

    assert os.path.abspath(os.fspath(source)) not in cache
    cache.close()


def test_header_read_uses_same_handle_generation_before_and_after(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    calls = 0

    def changing_generation(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FileGenerationToken("test-cache-header", calls)

    monkeypatch.setattr(
        cache_sqlite,
        "get_file_generation_token_from_fd",
        changing_generation,
    )

    with pytest.raises(CacheSafetyError, match="changed while"):
        SqliteCache(database)

    assert calls == 2


@pytest.mark.parametrize(
    "column,oversized_bytes",
    (
        ("blocks", cache_sqlite.MAX_CACHE_BLOCK_BYTES + 1),
        ("phashes", cache_sqlite.MAX_FEATURE_PAYLOAD_BYTES + 1),
        ("thumbnail", cache_sqlite.MAX_CACHE_THUMBNAIL_BYTES + 1),
    ),
)
def test_crafted_oversized_blob_is_rejected_before_materialization(
    tmp_path,
    column,
    oversized_bytes,
):
    source = tmp_path / "picture.png"
    source.write_bytes(b"content")
    cache = SqliteCache(tmp_path / "cache.sqlite3")
    cache.put_features(source, _features())
    cache.con.execute(
        "UPDATE pictures SET {}=zeroblob(?) WHERE path=?".format(column),
        (oversized_bytes, str(source)),
    )

    tracemalloc.start()
    with pytest.raises(ValueError, match="oversized"):
        cache.get_features(source)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 512 * 1024
    cache.close()


def test_readonly_connection_is_enforced_by_sqlite(tmp_path):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    cache = SqliteCache(database, readonly=True)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        cache.con.execute("DELETE FROM pictures")
    with pytest.raises(CacheSafetyError, match="read-only"):
        cache.clear()

    cache.close()


def test_app_picture_cache_clear_preserves_owned_database_and_marker(tmp_path):
    database = tmp_path / "cached_pictures_v5.db"
    source = tmp_path / "picture.png"
    source.write_bytes(b"content")
    cache = SqliteCache(database)
    cache.put_features(source, _features())
    cache.close()
    app = SimpleNamespace(
        _get_picture_cache_path=lambda: str(database),
    )

    DupeGuru.clear_picture_cache(app)

    assert database.exists()
    assert (
        database.read_bytes()[SQLITE_APPLICATION_ID_OFFSET : SQLITE_APPLICATION_ID_OFFSET + 4]
        == SQLITE_APPLICATION_ID_BYTES
    )
    reopened = SqliteCache(database)
    assert len(reopened) == 0
    reopened.close()


def test_app_picture_cache_clear_rejects_foreign_database_unchanged(tmp_path):
    database = tmp_path / "cached_pictures_v5.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE user_data(value TEXT)")
    connection.execute("INSERT INTO user_data VALUES('keep')")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    app = SimpleNamespace(
        _get_picture_cache_path=lambda: str(database),
    )

    with pytest.raises(CacheSchemaError, match="not owned"):
        DupeGuru.clear_picture_cache(app)

    assert database.read_bytes() == original


def test_app_picture_cache_clear_rejects_existing_sidecar_unchanged(tmp_path):
    database = tmp_path / "cached_pictures_v5.db"
    _create_cache(database)
    sidecar = Path("{}-wal".format(database))
    sidecar.write_bytes(b"foreign-sidecar")
    database_before = database.read_bytes()
    sidecar_before = sidecar.read_bytes()
    app = SimpleNamespace(
        _get_picture_cache_path=lambda: str(database),
    )

    with pytest.raises(CacheSafetyError, match="sidecar"):
        DupeGuru.clear_picture_cache(app)

    assert database.read_bytes() == database_before
    assert sidecar.read_bytes() == sidecar_before


@pytest.mark.parametrize("suffix", cache_sqlite.SQLITE_SIDECAR_SUFFIXES)
def test_picture_cache_open_rejects_every_existing_sidecar_before_sqlite(
    tmp_path,
    monkeypatch,
    suffix,
):
    database = tmp_path / "cache.sqlite3"
    _create_cache(database)
    sidecar = Path("{}{}".format(database, suffix))
    sidecar.write_bytes(b"foreign-sidecar")
    database_before = database.read_bytes()
    sidecar_before = sidecar.read_bytes()

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("a cache with a sidecar must be rejected before SQLite opens it")

    monkeypatch.setattr(cache_sqlite.sqlite, "connect", unexpected_connect)

    with pytest.raises(CacheSafetyError, match="sidecar"):
        SqliteCache(database)

    assert database.read_bytes() == database_before
    assert sidecar.read_bytes() == sidecar_before


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_app_picture_cache_clear_rejects_hardlink_unchanged(tmp_path):
    database = tmp_path / "cache.sqlite3"
    alias = tmp_path / "cached_pictures_v5.db"
    _create_cache(database)
    try:
        os.link(database, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))
    original = database.read_bytes()
    app = SimpleNamespace(
        _get_picture_cache_path=lambda: str(alias),
    )

    with pytest.raises(CacheSafetyError, match="exactly one"):
        DupeGuru.clear_picture_cache(app)

    assert database.read_bytes() == original
    assert alias.read_bytes() == original


def test_app_picture_cache_clear_rejects_symlink_unchanged(tmp_path):
    database = tmp_path / "cache.sqlite3"
    alias = tmp_path / "cached_pictures_v5.db"
    _create_cache(database)
    try:
        os.symlink(database, alias)
    except (NotImplementedError, OSError) as error:
        pytest.skip("file symlinks are unavailable: {}".format(error))
    original = database.read_bytes()
    app = SimpleNamespace(
        _get_picture_cache_path=lambda: str(alias),
    )

    with pytest.raises(CacheSafetyError, match="plain regular"):
        DupeGuru.clear_picture_cache(app)

    assert database.read_bytes() == original
