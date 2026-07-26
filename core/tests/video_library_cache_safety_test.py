# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import core.video.library as library_module
from core.video.library import _ArtifactCache, _capture_source, _metadata_to_json
from core.video.model import (
    AnalysisState,
    FrameFingerprint,
    VideoArtifact,
    VideoMetadata,
)


def _metadata():
    return VideoMetadata(
        duration_seconds=10,
        width=1920,
        height=1080,
        frame_rate=30,
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        audio_duration_seconds=10,
        bit_rate=1_000_000,
        container="mp4",
    )


def _source_and_cache(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    source_path = root / "video.mp4"
    source_path.write_bytes(b"video source")
    source = _capture_source(source_path, root, None)
    cache = _ArtifactCache(tmp_path / "video-cache.sqlite3", (root,), "safety-test-profile")
    return source, cache


def test_new_sqlite_cache_records_exact_owner_and_schema_markers(tmp_path):
    source, cache = _source_and_cache(tmp_path)

    assert source.path
    assert cache.connection.execute("PRAGMA user_version").fetchone() == (
        library_module.VIDEO_LIBRARY_CACHE_SCHEMA_VERSION,
    )
    assert cache.connection.execute("PRAGMA application_id").fetchone() == (
        library_module.VIDEO_LIBRARY_CACHE_APPLICATION_ID,
    )
    assert cache.path.stat().st_nlink == 1
    cache.close(commit=False)


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"),
    reason="sqlite3 runtime limits are unavailable",
)
def test_sqlite_cache_applies_runtime_limits_and_writable_safety_mode(tmp_path):
    _source, cache = _source_and_cache(tmp_path)

    for constant_name, maximum in library_module._VIDEO_CACHE_SQLITE_LIMITS:
        category = getattr(sqlite3, constant_name)
        assert cache.connection.getlimit(category) <= maximum
    assert cache.connection.execute("PRAGMA trusted_schema").fetchone() == (0,)
    assert cache.connection.execute("PRAGMA query_only").fetchone() == (0,)
    cache.close(commit=False)


def test_existing_cache_is_validated_read_only_before_writable_open(
    tmp_path,
    monkeypatch,
):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=True)
    real_connect = sqlite3.connect
    calls = []

    def recording_connect(database, *args, **kwargs):
        calls.append((os.fspath(database), dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(library_module.sqlite3, "connect", recording_connect)

    reopened = _ArtifactCache(cache_path, (), "safety-test-profile")

    assert len(calls) == 2
    assert calls[0][1].get("uri") is True
    assert "mode=ro" in calls[0][0]
    assert "immutable=1" in calls[0][0]
    assert calls[1][0] == str(cache_path)
    assert calls[1][1].get("uri") is not True
    reopened.close(commit=False)


def test_sqlite_cache_rejects_unowned_exact_schema_before_sqlite_connect(
    tmp_path,
    monkeypatch,
):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=False)
    connection = sqlite3.connect(cache_path)
    connection.execute("PRAGMA application_id = 0")
    connection.commit()
    connection.close()
    original = cache_path.read_bytes()

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("unowned cache must be rejected before SQLite parses it")

    monkeypatch.setattr(library_module.sqlite3, "connect", unexpected_connect)

    with pytest.raises(ValueError, match="ownership header"):
        _ArtifactCache(cache_path, (), "safety-test-profile")

    assert cache_path.read_bytes() == original


def test_sqlite_cache_rejects_legacy_version_before_connect_without_mutation(
    tmp_path,
    monkeypatch,
):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=False)
    connection = sqlite3.connect(cache_path)
    connection.execute(
        "PRAGMA user_version = {}".format(
            library_module.VIDEO_LIBRARY_CACHE_SCHEMA_VERSION - 1,
        )
    )
    connection.commit()
    connection.close()
    original = cache_path.read_bytes()

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("legacy cache must be rejected before SQLite parses it")

    monkeypatch.setattr(library_module.sqlite3, "connect", unexpected_connect)

    with pytest.raises(ValueError, match="ownership header"):
        _ArtifactCache(cache_path, (), "safety-test-profile")

    assert cache_path.read_bytes() == original


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_existing_sqlite_cache_hardlink_is_rejected_before_opening_for_writes(tmp_path):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=False)
    alias = tmp_path / "video-cache-alias.sqlite3"
    try:
        os.link(cache_path, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    with pytest.raises(ValueError, match="exactly one filesystem link"):
        _ArtifactCache(alias, (), "safety-test-profile")

    connection = sqlite3.connect(cache_path)
    try:
        assert connection.execute("PRAGMA application_id").fetchone() == (
            library_module.VIDEO_LIBRARY_CACHE_APPLICATION_ID,
        )
    finally:
        connection.close()


def test_sqlite_cache_filters_oversized_text_inside_sql_before_returning_payload(
    tmp_path,
    monkeypatch,
):
    source, cache = _source_and_cache(tmp_path)
    metadata = _metadata()
    artifact = VideoArtifact(
        source=source.snapshot,
        metadata=metadata,
        frames=(FrameFingerprint(1, 0.1, 1),),
        audio=None,
        state=AnalysisState.COMPLETE,
    )
    cache.put_artifact(source, artifact)
    cache.connection.commit()
    valid_metadata = _metadata_to_json(metadata).encode("utf-8")
    valid_artifact = library_module.artifact_to_json(artifact).encode("utf-8")
    monkeypatch.setattr(
        library_module,
        "MAXIMUM_CACHE_METADATA_BYTES",
        len(valid_metadata),
    )
    monkeypatch.setattr(
        library_module,
        "MAXIMUM_CACHE_ARTIFACT_BYTES",
        len(valid_artifact),
    )
    statements = []
    cache.connection.set_trace_callback(statements.append)

    cache.connection.execute(
        "UPDATE artifacts SET metadata_json = CAST(zeroblob(?) AS TEXT)",
        (len(valid_metadata) + 1,),
    )
    row = cache._row(source)

    assert row[0] is None
    assert row[1] == 0
    assert row[2] == valid_artifact
    assert row[3] == 1
    assert cache.metadata(source) is None

    cache.connection.execute(
        """
        UPDATE artifacts
        SET metadata_json = ?,
            artifact_json = CAST(zeroblob(?) AS TEXT)
        """,
        (_metadata_to_json(metadata), len(valid_artifact) + 1),
    )
    row = cache._row(source)

    assert row[0] == valid_metadata
    assert row[1] == 1
    assert row[2] is None
    assert row[3] == 0
    assert cache.artifact(source) is None
    select_statements = [statement for statement in statements if "SELECT" in statement.upper()]
    assert any("typeof(metadata_json)" in statement for statement in select_statements)
    assert any("length(CAST(artifact_json AS BLOB))" in statement for statement in select_statements)
    cache.close(commit=False)


def test_valid_cached_artifact_remains_compatible_after_strict_reopen(tmp_path):
    source, cache = _source_and_cache(tmp_path)
    artifact = VideoArtifact(
        source=source.snapshot,
        metadata=_metadata(),
        frames=(FrameFingerprint(1, 0.1, 1),),
        audio=None,
        state=AnalysisState.COMPLETE,
    )
    cache.put_artifact(source, artifact)
    cache_path = cache.path
    cache.close(commit=True)

    reopened = _ArtifactCache(cache_path, (), "safety-test-profile")

    assert reopened.metadata(source) == artifact.metadata
    assert reopened.artifact(source) == artifact
    reopened.close(commit=False)


@pytest.mark.parametrize("method_name", ("put_metadata", "put_artifact"))
def test_cache_writers_reject_metadata_the_reader_cannot_load(
    tmp_path,
    method_name,
):
    source, cache = _source_and_cache(tmp_path)
    oversized_metadata = replace(
        _metadata(),
        video_codec="x" * (library_module.VIDEO_METADATA_JSON_LIMITS.max_string_chars + 1),
    )
    value = oversized_metadata
    if method_name == "put_artifact":
        value = VideoArtifact(
            source=source.snapshot,
            metadata=oversized_metadata,
            frames=(FrameFingerprint(1, 0.1, 1),),
            audio=None,
            state=AnalysisState.COMPLETE,
        )

    with pytest.raises(ValueError, match="bounded cache"):
        getattr(cache, method_name)(source, value)

    assert cache.connection.execute("SELECT count(*) FROM artifacts").fetchone() == (0,)
    cache.close(commit=False)


def test_sqlite_cache_treats_extreme_metadata_integer_as_a_cache_miss(tmp_path):
    source, cache = _source_and_cache(tmp_path)
    cache.put_metadata(source, _metadata())
    document = json.loads(_metadata_to_json(_metadata()))
    document["duration_seconds"] = 10**300
    cache.connection.execute(
        "UPDATE artifacts SET metadata_json = ?",
        (json.dumps(document),),
    )

    assert cache.metadata(source) is None
    cache.close(commit=False)


def test_sqlite_cache_rejects_same_named_table_with_wrong_declared_schema(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    cache_path = tmp_path / "crafted.sqlite3"
    connection = sqlite3.connect(cache_path)
    connection.execute("""
        CREATE TABLE artifacts (
            path BLOB,
            size BLOB,
            mtime_ns BLOB,
            generation_token BLOB,
            identity_json BLOB,
            analyzer_version BLOB,
            analysis_profile BLOB,
            metadata_json BLOB,
            artifact_json BLOB
        )
        """)
    connection.execute("PRAGMA application_id = {}".format(library_module.VIDEO_LIBRARY_CACHE_APPLICATION_ID))
    connection.execute("PRAGMA user_version = {}".format(library_module.VIDEO_LIBRARY_CACHE_SCHEMA_VERSION))
    connection.commit()
    connection.close()
    original = cache_path.read_bytes()

    with pytest.raises(ValueError, match="unsupported object set|table shape"):
        _ArtifactCache(cache_path, (root,), "safety-test-profile")

    assert cache_path.read_bytes() == original


def test_sqlite_cache_rejects_modified_table_sql_without_mutation(tmp_path):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=True)
    connection = sqlite3.connect(cache_path)
    original_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='artifacts'"
    ).fetchone()[0]
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='artifacts'",
        (original_sql[:-1] + ", CHECK (length(path) > 0))",),
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()
    original = cache_path.read_bytes()

    with pytest.raises(ValueError, match="table SQL"):
        _ArtifactCache(cache_path, (), "safety-test-profile")

    assert cache_path.read_bytes() == original


@pytest.mark.parametrize("suffix", library_module._SQLITE_SIDECAR_SUFFIXES)
def test_sqlite_cache_rejects_every_sidecar_before_connect(
    tmp_path,
    monkeypatch,
    suffix,
):
    _source, cache = _source_and_cache(tmp_path)
    cache_path = cache.path
    cache.close(commit=True)
    sidecar = Path("{}{}".format(cache_path, suffix))
    sidecar.write_bytes(b"foreign-sidecar")
    cache_before = cache_path.read_bytes()
    sidecar_before = sidecar.read_bytes()

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("a sidecar must be rejected before SQLite opens the cache")

    monkeypatch.setattr(library_module.sqlite3, "connect", unexpected_connect)

    with pytest.raises(ValueError, match="sidecar"):
        _ArtifactCache(cache_path, (), "safety-test-profile")

    assert cache_path.read_bytes() == cache_before
    assert sidecar.read_bytes() == sidecar_before
