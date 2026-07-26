import json
import os
import sqlite3
import tracemalloc

import pytest

import core.pe.cache_sqlite as cache_sqlite
from core.pe.cache_sqlite import SqliteCache
from core.pe.image_features import FEATURE_VERSION, ImageFeatures, ImageQuality, TileFingerprint


def _features(orientation_count=1):
    block = ((1, 2, 3), (4, 5, 6))
    return ImageFeatures(
        dimensions=(40, 20),
        frame_count=2,
        blocks=tuple(block for _ in range(orientation_count)),
        phashes=tuple(range(orientation_count)),
        dhashes=tuple(range(orientation_count)),
        color_histogram=(1024,) + (0,) * 63,
        tile_fingerprints=(TileFingerprint("center_75", 7, 9, (1250, 1250, 8750, 8750)),),
        quality=ImageQuality(8, 2, 3, 0.125),
        thumbnail_png=b"\x89PNG\r\ncache-test",
        thumbnail_size=(32, 16),
        thumbnail_key="thumbnail-key",
    )


def test_normalized_features_round_trip_and_persist(tmp_path):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(b"content")
    database_path = tmp_path / "cache.sqlite3"

    cache = SqliteCache(str(database_path))
    cache.put_features(str(image_path), _features(8))
    rowid = cache.get_id(str(image_path))
    cached = cache.get_features(rowid)
    cache.close()

    assert cached.path == str(image_path)
    assert cached.dimensions == (40, 20)
    assert cached.frame_count == 2
    assert cached.orientation_count == 8
    assert cached.blocks == _features(8).blocks
    assert cached.phashes == tuple(range(8))
    assert cached.dhashes == tuple(range(8))
    assert cached.color_histogram == (1024,) + (0,) * 63
    assert cached.tile_fingerprints == (TileFingerprint("center_75", 7, 9, (1250, 1250, 8750, 8750)),)
    assert cached.quality == ImageQuality(8, 2, 3, 0.125)
    assert cached.thumbnail_png == b"\x89PNG\r\ncache-test"
    assert cached.thumbnail_size == (32, 16)
    assert cached.thumbnail_key == "thumbnail-key"
    assert cached.feature_version == FEATURE_VERSION
    readonly = SqliteCache(str(database_path), readonly=True)
    metadata = readonly.get_feature_metadata(rowid)
    readonly.close()
    assert metadata.phashes == tuple(range(8))
    assert metadata.dhashes == tuple(range(8))
    assert metadata.quality == ImageQuality(8, 2, 3, 0.125)
    assert not hasattr(metadata, "thumbnail_png")

    reopened = SqliteCache(str(database_path))
    assert reopened.get_features(str(image_path)) == cached
    reopened.close()


def test_identity_features_can_be_replaced_by_eight_orientations(tmp_path):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(b"content")
    cache = SqliteCache(str(tmp_path / "cache.sqlite3"))

    cache.put_features(str(image_path), _features(1))
    assert cache.get_features(str(image_path)).orientation_count == 1
    cache.put_features(str(image_path), _features(8))
    assert cache.get_features(str(image_path)).orientation_count == 8


def test_nanosecond_mtime_or_size_change_invalidates_features(tmp_path):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(b"first")
    cache = SqliteCache(str(tmp_path / "cache.sqlite3"))
    cache.put_features(str(image_path), _features())
    original = image_path.stat()

    image_path.write_bytes(b"second-with-a-different-size")
    os.utime(image_path, ns=(original.st_atime_ns, original.st_mtime_ns))
    cache.purge_outdated()

    assert str(image_path) not in cache
    with pytest.raises(KeyError):
        cache.get_features(str(image_path))


def test_unknown_old_schema_is_rejected_without_replacing_it(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE schema_version (version int PRIMARY KEY, description TEXT)")
    connection.execute("INSERT INTO schema_version VALUES (2, 'old blocks')")
    connection.execute(
        "CREATE TABLE pictures("
        "path TEXT, mtime_ns INTEGER, blocks BLOB, blocks2 BLOB, blocks3 BLOB, blocks4 BLOB, "
        "blocks5 BLOB, blocks6 BLOB, blocks7 BLOB, blocks8 BLOB)"
    )
    connection.execute("INSERT INTO pictures VALUES ('old.png',0,x'',x'',x'',x'',x'',x'',x'',x'')")
    connection.commit()
    connection.close()
    original = database_path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError):
        SqliteCache(str(database_path))

    assert database_path.read_bytes() == original
    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT version FROM schema_version").fetchone() == (2,)
    assert connection.execute("SELECT path FROM pictures").fetchone() == ("old.png",)
    connection.close()


def test_old_raw_phash_blob_is_never_silently_reused(tmp_path):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(b"content")
    cache = SqliteCache(str(tmp_path / "cache.sqlite3"))
    cache.put_features(str(image_path), _features())
    cache.con.execute(
        "UPDATE pictures SET phashes=? WHERE path=?",
        (b"\0" * 8, str(image_path)),
    )

    with pytest.raises(ValueError, match="payload"):
        cache.get_features(str(image_path))

    cache.close()


def test_outdated_feature_version_is_purged_before_payload_decode(tmp_path):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(b"content")
    cache = SqliteCache(str(tmp_path / "cache.sqlite3"))
    cache.put_features(str(image_path), _features())
    cache.con.execute(
        "UPDATE pictures SET feature_version=?,phashes=? WHERE path=?",
        ("legacy-pillow-policy", b"\0" * 8, str(image_path)),
    )

    with pytest.raises(KeyError):
        cache.get_features(str(image_path))
    cache.purge_outdated()

    assert str(image_path) not in cache
    cache.close()


def test_feature_payload_rejects_oversize_memoryview_before_copy():
    backing = bytearray(cache_sqlite.MAX_FEATURE_PAYLOAD_BYTES * 640)
    payload = memoryview(backing)

    tracemalloc.start()
    with pytest.raises(ValueError, match="invalid size"):
        cache_sqlite._deserialize_feature_payload(payload, 1)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 128 * 1024


def test_feature_payload_preflights_depth_before_json_decoder(monkeypatch):
    payload = ("[" * (cache_sqlite.FEATURE_PAYLOAD_JSON_LIMITS.max_depth + 1)).encode("ascii")

    def unexpected_json_decode(*_args, **_kwargs):
        raise AssertionError("structural limits must run before json.loads")

    monkeypatch.setattr(
        cache_sqlite.json,
        "loads",
        unexpected_json_decode,
    )

    with pytest.raises(ValueError, match="strict JSON"):
        cache_sqlite._deserialize_feature_payload(payload, 1)


def test_feature_payload_rejects_duplicate_object_keys():
    encoded = cache_sqlite._serialize_feature_payload(_features())
    duplicate = b'{"schema":"wrong",' + encoded[1:]

    with pytest.raises(ValueError, match="strict JSON"):
        cache_sqlite._deserialize_feature_payload(duplicate, 1)


@pytest.mark.parametrize(
    "number",
    ("NaN", "Infinity", "-Infinity", "1e9999"),
)
def test_feature_payload_rejects_nonfinite_numbers(number):
    value = json.loads(cache_sqlite._serialize_feature_payload(_features()).decode("ascii"))
    encoded = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace(
        '"jpeg_artifact_score":0.125',
        '"jpeg_artifact_score":{}'.format(number),
    )

    with pytest.raises(ValueError, match="strict JSON"):
        cache_sqlite._deserialize_feature_payload(encoded.encode("ascii"), 1)


def test_feature_payload_rejects_boolean_version_and_orientation_count():
    encoded = cache_sqlite._serialize_feature_payload(_features())
    boolean_version = encoded.replace(b'"version":1', b'"version":true')

    with pytest.raises(ValueError, match="version"):
        cache_sqlite._deserialize_feature_payload(boolean_version, 1)
    with pytest.raises(ValueError, match="count"):
        cache_sqlite._deserialize_feature_payload(encoded, True)


@pytest.mark.parametrize(
    "value",
    (
        "0" * 15,
        "0" * 17,
        " " + "0" * 15,
        "+" + "0" * 15,
        "A" + "0" * 15,
        "g" + "0" * 15,
    ),
)
def test_feature_payload_fingerprints_require_canonical_fixed_hex(value):
    with pytest.raises(ValueError, match="fixed-width"):
        cache_sqlite._parse_hex64(value)
