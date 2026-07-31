# Copyright 2016 Virgil Dupras
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Strict, identity-bound SQLite cache for normalized image features."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3 as sqlite
import stat
from dataclasses import dataclass
from pathlib import Path

from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
    get_file_generation_token_from_fd,
)
from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityVerdict,
    get_file_identity,
    get_file_identity_from_fd,
    same_physical_file,
)
from core.pe.cache import bytes_to_colors, colors_to_bytes
from core.pe.image_features import (
    COLOR_HISTOGRAM_LENGTH,
    FEATURE_VERSION,
    ImageQuality,
    MAX_TILE_FINGERPRINTS,
    TileFingerprint,
)
from core.safe_json import JsonStructuralLimits, preflight_json_structure
from core.safe_walk import is_reparse_point

SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_DATABASE_HEADER_BYTES = 100
SQLITE_APPLICATION_ID_OFFSET = 68
# ASCII "DGPE": dupeGuru picture-engine cache.  This is an ownership marker,
# not a schema version; the schema version remains independently validated.
SQLITE_APPLICATION_ID = 0x44475045
SQLITE_APPLICATION_ID_BYTES = SQLITE_APPLICATION_ID.to_bytes(4, "big")
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
SCHEMA_VERSION = 6
SCHEMA_DESCRIPTION = "Bound image features without persisted thumbnail image bytes."
_RETIRED_SCHEMA_VERSION = 5
_RETIRED_SCHEMA_DESCRIPTION = "Bound image features to versioned content generation, identity, and feature version."
FEATURE_PAYLOAD_SCHEMA = "dupeguru.image-feature-cache"
FEATURE_PAYLOAD_VERSION = 1
MAX_FEATURE_PAYLOAD_BYTES = 16_384
MAX_CACHE_PATH_CHARACTERS = 32_768
MAX_CACHE_BLOCK_BYTES = 15 * 15 * 3
MAX_CACHE_IDENTITY_CHARACTERS = 4_096
MAX_CACHE_GENERATION_TOKEN_BYTES = 256
MAX_CACHE_FEATURE_VERSION_CHARACTERS = 256
MAX_CACHE_THUMBNAIL_KEY_CHARACTERS = 128
MAX_CACHE_THUMBNAIL_EDGE = 256
MAX_FEATURE_BATCH_ROWS = 4_096
MAX_SQLITE_INTEGER = (1 << 63) - 1
FEATURE_PAYLOAD_JSON_LIMITS = JsonStructuralLimits(
    max_depth=8,
    max_container_entries=COLOR_HISTOGRAM_LENGTH,
    max_total_nodes=512,
    max_scalar_tokens=512,
    max_total_string_chars=4_096,
    max_string_chars=256,
    max_scalar_chars=64,
)
_SQLITE_CONNECTION_LIMITS = (
    ("SQLITE_LIMIT_LENGTH", 2 * 1024 * 1024),
    ("SQLITE_LIMIT_SQL_LENGTH", 128 * 1024),
    ("SQLITE_LIMIT_COLUMN", 64),
    ("SQLITE_LIMIT_EXPR_DEPTH", 64),
    ("SQLITE_LIMIT_COMPOUND_SELECT", 16),
    ("SQLITE_LIMIT_VDBE_OP", 100_000),
    ("SQLITE_LIMIT_FUNCTION_ARG", 32),
    ("SQLITE_LIMIT_ATTACHED", 0),
    ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 4_096),
    ("SQLITE_LIMIT_VARIABLE_NUMBER", MAX_FEATURE_BATCH_ROWS),
    ("SQLITE_LIMIT_TRIGGER_DEPTH", 0),
    ("SQLITE_LIMIT_WORKER_THREADS", 0),
)
_EXPECTED_SCHEMA_OBJECTS = frozenset(
    {
        ("table", "schema_version", "schema_version", "text"),
        ("table", "pictures", "pictures", "text"),
        ("index", "idx_path", "pictures", "text"),
    }
)
_MAX_SCHEMA_NAME_BYTES = 128
_MAX_SCHEMA_SQL_BYTES = 4_096
_MAX_SCHEMA_DESCRIPTION_BYTES = 512

_PICTURE_COLUMNS_V3 = (
    "path",
    "mtime_ns",
    "file_size",
    "blocks",
    "blocks2",
    "blocks3",
    "blocks4",
    "blocks5",
    "blocks6",
    "blocks7",
    "blocks8",
    "width",
    "height",
    "frame_count",
    "phashes",
    "phash_count",
    "thumbnail",
    "thumbnail_width",
    "thumbnail_height",
    "thumbnail_key",
    "feature_version",
)
_PICTURE_COLUMNS_V4 = _PICTURE_COLUMNS_V3 + (
    "ctime_ns",
    "identity_json",
)
_PICTURE_COLUMNS_V5 = _PICTURE_COLUMNS_V4 + ("generation_token",)
_PICTURE_COLUMNS_V6 = tuple(column for column in _PICTURE_COLUMNS_V5 if column != "thumbnail")

_CREATE_TABLE_QUERY_V5 = (
    "CREATE TABLE pictures("
    "path TEXT NOT NULL, mtime_ns INTEGER NOT NULL, file_size INTEGER NOT NULL, "
    "blocks BLOB, blocks2 BLOB, blocks3 BLOB, blocks4 BLOB, blocks5 BLOB, "
    "blocks6 BLOB, blocks7 BLOB, blocks8 BLOB, "
    "width INTEGER, height INTEGER, frame_count INTEGER, "
    "phashes BLOB, phash_count INTEGER, "
    "thumbnail BLOB, thumbnail_width INTEGER, thumbnail_height INTEGER, "
    "thumbnail_key TEXT, feature_version TEXT, "
    "ctime_ns INTEGER, identity_json TEXT, generation_token BLOB)"
)

_FEATURE_ROW_COLUMNS = (
    "path",
    "blocks",
    "blocks2",
    "blocks3",
    "blocks4",
    "blocks5",
    "blocks6",
    "blocks7",
    "blocks8",
    "width",
    "height",
    "frame_count",
    "phashes",
    "phash_count",
    "thumbnail_width",
    "thumbnail_height",
    "thumbnail_key",
    "feature_version",
    "mtime_ns",
    "file_size",
    "ctime_ns",
    "identity_json",
    "generation_token",
)


class CacheSafetyError(sqlite.DatabaseError):
    """A cache path, file, schema, or source binding is unsafe."""


class CacheSchemaError(CacheSafetyError):
    """An existing database does not have a supported exact schema."""


@dataclass(frozen=True)
class CacheSourceBinding:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    identity_json: str
    generation_token: bytes
    identity: FileIdentity

    @property
    def generation(self):
        return self.size, self.mtime_ns, self.generation_token


@dataclass(frozen=True)
class CachedImageFeatureMetadata:
    rowid: int
    path: str
    dimensions: tuple
    phashes: tuple
    dhashes: tuple
    color_histogram: tuple
    tile_fingerprints: tuple
    quality: ImageQuality
    feature_version: str

    @property
    def orientation_count(self):
        return len(self.phashes)


@dataclass(frozen=True)
class CachedImageFeatures(CachedImageFeatureMetadata):
    frame_count: int
    blocks: tuple
    thumbnail_size: tuple
    thumbnail_key: str


@dataclass(frozen=True)
class _FeaturePayload:
    phashes: tuple
    dhashes: tuple
    color_histogram: tuple
    tile_fingerprints: tuple
    quality: ImageQuality


def _hex64(value):
    return "{:016x}".format(value)


def _serialize_feature_payload(features):
    payload = {
        "schema": FEATURE_PAYLOAD_SCHEMA,
        "version": FEATURE_PAYLOAD_VERSION,
        "phashes": [_hex64(value) for value in features.phashes],
        "dhashes": [_hex64(value) for value in features.dhashes],
        "color_histogram": list(features.color_histogram),
        "tile_fingerprints": [
            {
                "kind": item.kind,
                "phash": _hex64(item.phash),
                "dhash": _hex64(item.dhash),
                "box": list(item.box),
            }
            for item in features.tile_fingerprints
        ],
        "quality": {
            "bit_depth": features.quality.bit_depth,
            "exif_count": features.quality.exif_count,
            "metadata_count": features.quality.metadata_count,
            "jpeg_artifact_score": features.quality.jpeg_artifact_score,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > MAX_FEATURE_PAYLOAD_BYTES:
        raise ValueError("image feature cache payload exceeds its size limit")
    return encoded


def _parse_hex64(value):
    if (
        not isinstance(value, str)
        or len(value) != 16
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("image feature fingerprint is not fixed-width hexadecimal")
    try:
        result = int(value, 16)
    except ValueError as error:
        raise ValueError("image feature fingerprint is not hexadecimal") from error
    if not 0 <= result < 1 << 64:
        raise ValueError("image feature fingerprint exceeds 64 bits")
    return result


def _reject_duplicate_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("image feature cache payload contains a duplicate key: {!r}".format(key))
        result[key] = value
    return result


def _strict_json_float(value):
    result = float(value)
    if not result == result or result in {float("-inf"), float("inf")}:
        raise ValueError("image feature cache payload contains a non-finite number")
    return result


def _reject_nonfinite_json_constant(value):
    raise ValueError("image feature cache payload contains a non-finite value: {}".format(value))


def _deserialize_feature_payload(data, count):
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("image feature cache payload is not a binary value")
    encoded_length = data.nbytes if isinstance(data, memoryview) else len(data)
    if (
        not encoded_length
        or encoded_length > MAX_FEATURE_PAYLOAD_BYTES
        or type(count) is not int
        or count not in {1, 8}
    ):
        raise ValueError("image feature cache payload has an invalid size or count")
    encoded = bytes(data)
    try:
        text = encoded.decode("ascii")
        preflight_json_structure(
            text,
            limits=FEATURE_PAYLOAD_JSON_LIMITS,
            label="image feature cache payload",
        )
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_nonfinite_json_constant,
            parse_float=_strict_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("image feature cache payload is not strict JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "version",
        "phashes",
        "dhashes",
        "color_histogram",
        "tile_fingerprints",
        "quality",
    }:
        raise ValueError("image feature cache payload has unsupported fields")
    if (
        value["schema"] != FEATURE_PAYLOAD_SCHEMA
        or type(value["version"]) is not int
        or value["version"] != FEATURE_PAYLOAD_VERSION
    ):
        raise ValueError("image feature cache payload version is unsupported")
    if not isinstance(value["phashes"], list) or not isinstance(value["dhashes"], list):
        raise ValueError("image feature orientation payload must use lists")
    phashes = tuple(_parse_hex64(item) for item in value["phashes"])
    dhashes = tuple(_parse_hex64(item) for item in value["dhashes"])
    if len(phashes) != count or len(dhashes) != count:
        raise ValueError("image feature orientation payload is inconsistent")
    if not isinstance(value["color_histogram"], list):
        raise ValueError("image feature color histogram must be a list")
    histogram = tuple(value["color_histogram"])
    if (
        len(histogram) != COLOR_HISTOGRAM_LENGTH
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in histogram)
        or sum(histogram) != 32 * 32
    ):
        raise ValueError("image feature color histogram is corrupt")
    tile_values = value["tile_fingerprints"]
    if not isinstance(tile_values, list) or len(tile_values) > MAX_TILE_FINGERPRINTS:
        raise ValueError("image feature tile payload must be a list")
    tiles = []
    for item in tile_values:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "phash",
            "dhash",
            "box",
        }:
            raise ValueError("image feature tile payload is corrupt")
        if not isinstance(item["box"], list) or len(item["box"]) != 4:
            raise ValueError("image feature tile box payload is corrupt")
        tiles.append(
            TileFingerprint(
                item["kind"],
                _parse_hex64(item["phash"]),
                _parse_hex64(item["dhash"]),
                tuple(item["box"]),
            )
        )
    if len({item.kind for item in tiles}) != len(tiles):
        raise ValueError("image feature tile payload contains duplicate kinds")
    quality_value = value["quality"]
    if not isinstance(quality_value, dict) or set(quality_value) != {
        "bit_depth",
        "exif_count",
        "metadata_count",
        "jpeg_artifact_score",
    }:
        raise ValueError("image feature quality payload is corrupt")
    return _FeaturePayload(
        phashes=phashes,
        dhashes=dhashes,
        color_histogram=histogram,
        tile_fingerprints=tuple(tiles),
        quality=ImageQuality(
            bit_depth=quality_value["bit_depth"],
            exif_count=quality_value["exif_count"],
            metadata_count=quality_value["metadata_count"],
            jpeg_artifact_score=quality_value["jpeg_artifact_score"],
        ),
    )


def _path_key(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _path_is_within(path, root) -> bool:
    candidate = _path_key(path)
    container = _path_key(root)
    try:
        return os.path.commonpath((candidate, container)) == container
    except ValueError:
        return False


def _component_paths(path: Path):
    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor = Path(absolute.anchor)
    current = anchor
    if absolute.anchor:
        yield current
        parts = absolute.parts[1:]
    else:
        parts = absolute.parts
    for part in parts:
        current = current / part
        yield current


def _require_plain_local_directory_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if str(absolute).startswith(("\\\\", "//")):
        raise CacheSafetyError("image cache cannot be stored on a UNC path")
    for component in _component_paths(absolute):
        try:
            component_stat = os.lstat(component)
        except OSError as error:
            raise CacheSafetyError("image cache parent component is unavailable: '{}'".format(component)) from error
        if (
            stat.S_ISLNK(component_stat.st_mode)
            or is_reparse_point(component_stat)
            or not stat.S_ISDIR(component_stat.st_mode)
        ):
            raise CacheSafetyError("image cache parent components must be plain directories: '{}'".format(component))
    if os.name == "nt":
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(Path(absolute.anchor)))
        if drive_type in {0, 1, 4}:
            raise CacheSafetyError("image cache must be stored on a known local drive")


def _require_plain_regular_file(path: Path, *, single_link: bool) -> tuple:
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise CacheSafetyError("image cache file is unavailable: '{}'".format(path)) from error
    if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise CacheSafetyError("image cache must be a plain regular file: '{}'".format(path))
    if single_link and getattr(file_stat, "st_nlink", None) != 1:
        raise CacheSafetyError("image cache must have exactly one filesystem link: '{}'".format(path))
    try:
        identity = get_file_identity(path, follow_symlinks=False, stat_result=file_stat)
    except FileIdentityError as error:
        raise CacheSafetyError("image cache physical identity is unavailable: '{}'".format(path)) from error
    return file_stat, identity


def _require_no_sqlite_sidecars(path: Path) -> None:
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path("{}{}".format(path, suffix))
        if os.path.lexists(sidecar):
            raise CacheSafetyError("image cache refuses an existing SQLite sidecar: '{}'".format(sidecar))


def _coerce_identity(value) -> FileIdentity:
    if isinstance(value, FileIdentity):
        return value
    identity = getattr(value, "identity", None)
    if isinstance(identity, FileIdentity):
        return identity
    raise TypeError("input_identities must contain FileIdentity values or snapshots exposing .identity")


def _no_follow_open_flag() -> int:
    if hasattr(os, "O_NOFOLLOW"):
        return os.O_NOFOLLOW
    if os.name == "nt":
        # Windows reparse points are rejected through lstat/file attributes
        # and a FILE_FLAG_OPEN_REPARSE_POINT physical-identity check before
        # and after every read.  The CRT does not expose O_NOFOLLOW.
        return 0
    raise CacheSafetyError("this platform cannot open cache files without following links")


def _read_existing_header(path: Path, expected_identity: FileIdentity) -> None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _no_follow_open_flag()
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CacheSafetyError("image cache header could not be read: '{}'".format(path)) from error
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_point(opened_before)
            or getattr(opened_before, "st_nlink", None) != 1
        ):
            raise CacheSafetyError("opened image cache is not a single-link regular file")
        try:
            generation_before = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_before,
                expected_identity=expected_identity,
            )
        except FileGenerationError as error:
            raise CacheSafetyError("image cache generation could not be verified while reading its header") from error
        chunks = []
        remaining = SQLITE_DATABASE_HEADER_BYTES
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        header = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        try:
            generation_after = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_after,
                expected_identity=expected_identity,
            )
        except FileGenerationError as error:
            raise CacheSafetyError("image cache generation could not be verified after reading its header") from error
    finally:
        os.close(descriptor)
    if (
        int(opened_before.st_size) != int(opened_after.st_size)
        or int(opened_before.st_mtime_ns) != int(opened_after.st_mtime_ns)
        or generation_before != generation_after
    ):
        raise CacheSafetyError("image cache changed while its header was being read")
    _path_stat, current_identity = _require_plain_regular_file(path, single_link=True)
    if same_physical_file(expected_identity, current_identity).verdict is not IdentityVerdict.SAME:
        raise CacheSafetyError("image cache identity changed while it was being opened")
    if len(header) < SQLITE_DATABASE_HEADER_BYTES or header[: len(SQLITE_HEADER)] != SQLITE_HEADER:
        raise CacheSchemaError("existing image cache does not have a valid SQLite header")
    if header[18:20] != b"\x01\x01":
        raise CacheSchemaError("existing image cache uses an unsupported SQLite journal mode")
    application_id = header[SQLITE_APPLICATION_ID_OFFSET : SQLITE_APPLICATION_ID_OFFSET + 4]
    if application_id != SQLITE_APPLICATION_ID_BYTES:
        raise CacheSchemaError("existing SQLite database is not owned by the dupeGuru picture cache")


@contextlib.contextmanager
def _hold_database_path_identity(
    path: Path,
    expected_identity: FileIdentity,
    *,
    allow_content_change=False,
):
    """Prevent Windows path replacement while the verified database is reopened."""

    if os.name == "nt":
        import ctypes
        import msvcrt

        from core.file_identity import (
            _FILE_FLAG_OPEN_REPARSE_POINT,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ,
            _FILE_SHARE_WRITE,
            _INVALID_HANDLE_VALUE,
            _OPEN_EXISTING,
            _close_handle,
            _create_file,
            windows_extended_path,
        )

        raw_handle = _create_file(
            windows_extended_path(path),
            _FILE_READ_ATTRIBUTES | 0x0001,  # FILE_READ_DATA
            # Write sharing lets the verified SQLite connection open. Omitting
            # delete sharing leases this exact directory entry across reopen.
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            error = ctypes.WinError(ctypes.get_last_error())
            raise CacheSafetyError("image cache path could not be leased across writable reopen") from error
        try:
            descriptor = msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_handle(raw_handle)
            raise
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _no_follow_open_flag()
        descriptor = os.open(path, flags)

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or is_reparse_point(before) or getattr(before, "st_nlink", None) != 1:
            raise CacheSafetyError("leased image cache is not a single-link regular file")
        before_identity = get_file_identity_from_fd(
            descriptor,
            path=path,
            stat_result=before,
        )
        if same_physical_file(expected_identity, before_identity).verdict is not IdentityVerdict.SAME:
            raise CacheSafetyError("image cache identity changed before writable reopen")
        yield
        after = os.fstat(descriptor)
        after_identity = get_file_identity_from_fd(
            descriptor,
            path=path,
            stat_result=after,
        )
        if (
            (
                not allow_content_change
                and (int(before.st_size) != int(after.st_size) or int(before.st_mtime_ns) != int(after.st_mtime_ns))
            )
            or getattr(after, "st_nlink", None) != 1
            or is_reparse_point(after)
            or same_physical_file(before_identity, after_identity).verdict is not IdentityVerdict.SAME
        ):
            raise CacheSafetyError("image cache changed while its writable handle was opened")
        _path_stat, path_identity = _require_plain_regular_file(path, single_link=True)
        if same_physical_file(expected_identity, path_identity).verdict is not IdentityVerdict.SAME:
            raise CacheSafetyError("image cache path changed while its writable handle was opened")
    except (FileIdentityError, OSError) as error:
        raise CacheSafetyError("image cache identity could not be leased across writable reopen") from error
    finally:
        os.close(descriptor)


def validate_sqlite_cache_location(
    db,
    *,
    input_roots=(),
    input_identities=(),
) -> Path | None:
    """Validate a cache location before SQLite can open or create it.

    ``VisualService`` can pass its selected roots and captured
    ``VisualAssetSnapshot.identity`` values through this API. No path-based or
    weak-identity fallback is performed.
    """

    if db == ":memory:":
        return None
    if not isinstance(db, (str, os.PathLike)) or not os.fspath(db) or "\0" in os.fspath(db):
        raise CacheSafetyError("image cache path must be a non-empty filesystem path")
    database_path = Path(os.path.abspath(os.fspath(db)))
    _require_plain_local_directory_components(database_path.parent)
    _require_no_sqlite_sidecars(database_path)
    for root in input_roots:
        root_path = Path(os.path.abspath(os.fspath(root)))
        canonical_root = Path(os.path.realpath(root_path))
        if _path_is_within(database_path, root_path) or _path_is_within(
            database_path,
            canonical_root,
        ):
            raise CacheSafetyError("image cache must be outside every visual input root: '{}'".format(root))
    if os.path.lexists(database_path):
        _file_stat, database_identity = _require_plain_regular_file(
            database_path,
            single_link=True,
        )
        _read_existing_header(database_path, database_identity)
        for value in input_identities:
            if same_physical_file(database_identity, _coerce_identity(value)).verdict is IdentityVerdict.SAME:
                raise CacheSafetyError("image cache aliases a captured visual input")
    else:
        tuple(_coerce_identity(value) for value in input_identities)
    return database_path


def _reserve_new_database(path: Path) -> FileIdentity:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | _no_follow_open_flag()
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise CacheSafetyError("image cache path appeared during exclusive creation") from error
    except OSError as error:
        raise CacheSafetyError("image cache could not be reserved safely: '{}'".format(path)) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or is_reparse_point(opened) or getattr(opened, "st_nlink", None) != 1:
            raise CacheSafetyError("reserved image cache is not a single-link regular file")
    finally:
        os.close(descriptor)
    _file_stat, identity = _require_plain_regular_file(path, single_link=True)
    return identity


def _identity_to_json(identity: FileIdentity) -> str:
    if isinstance(identity.file_id, bytes):
        file_id_kind = "bytes"
        file_id = identity.file_id.hex()
    else:
        file_id_kind = "integer"
        file_id = str(int(identity.file_id))
    return json.dumps(
        {
            "namespace": identity.namespace,
            "capability": identity.capability.value,
            "volume_id": int(identity.volume_id),
            "file_id_kind": file_id_kind,
            "file_id": file_id,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _source_metadata(file_stat) -> tuple:
    ctime_ns = getattr(file_stat, "st_ctime_ns", None)
    if ctime_ns is None:
        raise CacheSafetyError("source filesystem does not expose nanosecond ctime")
    values = (
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(ctime_ns),
    )
    if min(values) < 0:
        raise CacheSafetyError("source generation metadata must not be negative")
    return values


def capture_source_binding(path) -> CacheSourceBinding:
    """Capture a no-follow, physical identity-bound source generation."""

    source_path = Path(os.path.abspath(os.fspath(path)))
    _require_plain_local_directory_components(source_path.parent)
    try:
        first_stat = os.stat(source_path, follow_symlinks=False)
    except OSError as error:
        raise CacheSafetyError("image source is unavailable: '{}'".format(source_path)) from error
    if stat.S_ISLNK(first_stat.st_mode) or is_reparse_point(first_stat) or not stat.S_ISREG(first_stat.st_mode):
        raise CacheSafetyError("image source must be a plain regular file: '{}'".format(source_path))
    try:
        identity = get_file_identity(
            source_path,
            follow_symlinks=False,
            stat_result=first_stat,
        )
        first_generation = get_file_generation_token(
            source_path,
            follow_symlinks=False,
            stat_result=first_stat,
            expected_identity=identity,
        )
        second_stat = os.stat(source_path, follow_symlinks=False)
        second_identity = get_file_identity(
            source_path,
            follow_symlinks=False,
            stat_result=second_stat,
        )
        second_generation = get_file_generation_token(
            source_path,
            follow_symlinks=False,
            stat_result=second_stat,
            expected_identity=second_identity,
        )
    except (OSError, FileGenerationError, FileIdentityError) as error:
        raise CacheSafetyError("image source identity is unavailable: '{}'".format(source_path)) from error
    if (
        int(first_stat.st_size),
        int(first_stat.st_mtime_ns),
        first_generation,
    ) != (
        int(second_stat.st_size),
        int(second_stat.st_mtime_ns),
        second_generation,
    ) or same_physical_file(identity, second_identity).verdict is not IdentityVerdict.SAME:
        raise CacheSafetyError("image source changed while its cache binding was captured")
    size, mtime_ns, ctime_ns = _source_metadata(second_stat)
    return CacheSourceBinding(
        path=str(source_path),
        size=size,
        mtime_ns=mtime_ns,
        ctime_ns=ctime_ns,
        identity_json=_identity_to_json(second_identity),
        generation_token=second_generation.encoded,
        identity=second_identity,
    )


def _require_expected_source_binding(
    observed: CacheSourceBinding,
    expected: CacheSourceBinding,
) -> None:
    if not isinstance(expected, CacheSourceBinding):
        raise TypeError("expected_binding must be a CacheSourceBinding")
    if (
        observed.path != expected.path
        or observed.generation != expected.generation
        or same_physical_file(
            observed.identity,
            expected.identity,
        ).verdict
        is not IdentityVerdict.SAME
    ):
        raise CacheSafetyError("image source changed before its decoded features could be cached")


def _readonly_uri(path: Path) -> str:
    return "{}?mode=ro&immutable=1".format(path.as_uri())


def _apply_sqlite_runtime_limits(connection) -> None:
    """Bound SQLite parser and value resources before any owned schema is read."""

    if not hasattr(connection, "setlimit"):
        return
    try:
        for constant_name, limit in _SQLITE_CONNECTION_LIMITS:
            category = getattr(sqlite, constant_name, None)
            if category is None:
                continue
            connection.setlimit(category, limit)
            if connection.getlimit(category) > limit:
                raise CacheSafetyError(
                    "SQLite runtime limit {} could not be lowered for the image cache".format(
                        constant_name,
                    )
                )
    except CacheSafetyError:
        raise
    except (AttributeError, OverflowError, sqlite.Error) as error:
        raise CacheSafetyError("image cache SQLite runtime limits could not be configured") from error


def _vacuum_owned_database(connection) -> None:
    """Run SQLite's fixed VACUUM statement and restore the no-attach limit."""

    category = getattr(sqlite, "SQLITE_LIMIT_ATTACHED", None)
    if category is None or not hasattr(connection, "setlimit"):
        connection.execute("VACUUM")
        return
    original_limit = connection.getlimit(category)
    try:
        connection.setlimit(category, 1)
        if connection.getlimit(category) < 1:
            raise CacheSafetyError("SQLite could not permit its internal VACUUM database")
        connection.execute("VACUUM")
    finally:
        connection.setlimit(category, original_limit)
        if connection.getlimit(category) != original_limit:
            raise CacheSafetyError("SQLite attachment limit was not restored after VACUUM")


def _normalize_schema_sql(value) -> str:
    if not isinstance(value, str):
        raise CacheSchemaError("image cache schema SQL is missing or invalid")
    normalized = " ".join(value.split()).upper()
    return normalized.replace("( ", "(").replace(" )", ")")


def _bounded_schema_objects(connection):
    rows = connection.execute(
        """
        SELECT
            CASE
                WHEN typeof(type) = 'text'
                    AND length(CAST(type AS BLOB)) BETWEEN 1 AND ?
                THEN type
            END,
            CASE
                WHEN typeof(name) = 'text'
                    AND length(CAST(name AS BLOB)) BETWEEN 1 AND ?
                THEN name
            END,
            CASE
                WHEN typeof(tbl_name) = 'text'
                    AND length(CAST(tbl_name AS BLOB)) BETWEEN 1 AND ?
                THEN tbl_name
            END,
            typeof(sql)
        FROM sqlite_schema
        ORDER BY rowid
        LIMIT ?
        """,
        (
            _MAX_SCHEMA_NAME_BYTES,
            _MAX_SCHEMA_NAME_BYTES,
            _MAX_SCHEMA_NAME_BYTES,
            len(_EXPECTED_SCHEMA_OBJECTS) + 1,
        ),
    ).fetchall()
    if len(rows) != len(_EXPECTED_SCHEMA_OBJECTS):
        return None
    result = set()
    for object_type, name, table_name, sql_type in rows:
        if object_type is None or name is None or table_name is None:
            return None
        result.add(
            (
                str(object_type),
                str(name),
                str(table_name),
                str(sql_type),
            )
        )
    return frozenset(result)


def _bounded_table_columns(connection, table_name, expected_count):
    rows = connection.execute(
        """
        SELECT
            cid,
            CASE
                WHEN typeof(name) = 'text'
                    AND length(CAST(name AS BLOB)) BETWEEN 1 AND ?
                THEN name
            END,
            CASE
                WHEN typeof(type) = 'text'
                    AND length(CAST(type AS BLOB)) BETWEEN 1 AND ?
                THEN upper(type)
            END,
            "notnull",
            CASE WHEN dflt_value IS NULL THEN 0 ELSE 1 END,
            pk,
            hidden
        FROM pragma_table_xinfo(?)
        ORDER BY cid
        LIMIT ?
        """,
        (
            _MAX_SCHEMA_NAME_BYTES,
            _MAX_SCHEMA_NAME_BYTES,
            table_name,
            expected_count + 1,
        ),
    ).fetchall()
    if len(rows) != expected_count:
        return None
    result = []
    for expected_cid, row in enumerate(rows):
        cid, name, declared_type, not_null, has_default, primary_key, hidden = row
        if (
            int(cid) != expected_cid
            or name is None
            or declared_type is None
            or int(has_default) != 0
            or int(hidden) != 0
        ):
            return None
        result.append(
            (
                str(name),
                str(declared_type),
                int(not_null),
                int(primary_key),
            )
        )
    return tuple(result)


def _bounded_schema_sql(connection, object_type, name):
    rows = connection.execute(
        """
        SELECT CASE
            WHEN typeof(sql) = 'text'
                AND length(CAST(sql AS BLOB)) BETWEEN 1 AND ?
            THEN sql
        END
        FROM sqlite_schema
        WHERE type = ? AND name = ?
        LIMIT 2
        """,
        (_MAX_SCHEMA_SQL_BYTES, object_type, name),
    ).fetchall()
    if len(rows) != 1 or rows[0][0] is None:
        return None
    return _normalize_schema_sql(str(rows[0][0]))


def _require_bounded_cache_text(value, maximum, label):
    if not isinstance(value, str) or not value or "\0" in value or len(value) > maximum:
        raise ValueError("{} is invalid or exceeds the image cache limit".format(label))
    return value


def _require_bounded_cache_blob(value, maximum, label, *, allow_empty=False):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("{} must be binary data".format(label))
    length = value.nbytes if isinstance(value, memoryview) else len(value)
    minimum = 0 if allow_empty else 1
    if not minimum <= length <= maximum:
        raise ValueError("{} is invalid or exceeds the image cache limit".format(label))
    return value


def _require_sqlite_integer(value, label, *, positive=False):
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_SQLITE_INTEGER:
        raise ValueError("{} is outside the image cache integer domain".format(label))
    return value


def _validate_cache_binding_fields(binding):
    _require_bounded_cache_text(
        binding.path,
        MAX_CACHE_PATH_CHARACTERS,
        "image cache path",
    )
    _require_sqlite_integer(binding.size, "image cache file size")
    _require_sqlite_integer(binding.mtime_ns, "image cache modification time")
    _require_sqlite_integer(binding.ctime_ns, "image cache change time")
    _require_bounded_cache_text(
        binding.identity_json,
        MAX_CACHE_IDENTITY_CHARACTERS,
        "image cache identity",
    )
    _require_bounded_cache_blob(
        binding.generation_token,
        MAX_CACHE_GENERATION_TOKEN_BYTES,
        "image cache generation token",
    )


def _validate_feature_write_payload(features, encoded_blocks):
    if len(encoded_blocks) != features.orientation_count:
        raise ValueError("image cache orientation blocks are inconsistent")
    for block in encoded_blocks:
        _require_bounded_cache_blob(
            block,
            MAX_CACHE_BLOCK_BYTES,
            "image cache block",
        )
    if len({len(block) for block in encoded_blocks}) != 1:
        raise ValueError("image cache orientation blocks have inconsistent sizes")
    width, height = features.dimensions
    _require_sqlite_integer(width, "image feature width", positive=True)
    _require_sqlite_integer(height, "image feature height", positive=True)
    _require_sqlite_integer(features.frame_count, "image feature frame count", positive=True)
    thumbnail_width, thumbnail_height = features.thumbnail_size
    for value, label in (
        (thumbnail_width, "image cache thumbnail width"),
        (thumbnail_height, "image cache thumbnail height"),
    ):
        _require_sqlite_integer(value, label, positive=True)
        if value > MAX_CACHE_THUMBNAIL_EDGE:
            raise ValueError("{} exceeds the image cache limit".format(label))
    _require_bounded_cache_text(
        features.thumbnail_key,
        MAX_CACHE_THUMBNAIL_KEY_CHARACTERS,
        "image cache thumbnail key",
    )
    _require_bounded_cache_text(
        features.feature_version,
        MAX_CACHE_FEATURE_VERSION_CHARACTERS,
        "image cache feature version",
    )


class SqliteCache:
    """Cache normalized picture features without trusting paths as identity."""

    schema_version = SCHEMA_VERSION
    schema_version_description = SCHEMA_DESCRIPTION

    create_table_query = (
        "CREATE TABLE pictures("
        "path TEXT NOT NULL, mtime_ns INTEGER NOT NULL, file_size INTEGER NOT NULL, "
        "blocks BLOB, blocks2 BLOB, blocks3 BLOB, blocks4 BLOB, blocks5 BLOB, "
        "blocks6 BLOB, blocks7 BLOB, blocks8 BLOB, "
        "width INTEGER, height INTEGER, frame_count INTEGER, "
        "phashes BLOB, phash_count INTEGER, "
        "thumbnail_width INTEGER, thumbnail_height INTEGER, "
        "thumbnail_key TEXT, feature_version TEXT, "
        "ctime_ns INTEGER, identity_json TEXT, generation_token BLOB)"
    )
    create_schema_version_query = (
        "CREATE TABLE schema_version(" "version INTEGER PRIMARY KEY, description TEXT NOT NULL)"
    )
    create_index_query = "CREATE UNIQUE INDEX idx_path ON pictures(path)"

    def __init__(
        self,
        db=":memory:",
        readonly=False,
        *,
        input_roots=(),
        input_identities=(),
    ):
        self.readonly = bool(readonly)
        self.input_roots = tuple(input_roots)
        self.input_identities = tuple(input_identities)
        self.con = None
        self._database_identity = None
        validated = validate_sqlite_cache_location(
            db,
            input_roots=self.input_roots,
            input_identities=self.input_identities,
        )
        self.dbname = ":memory:" if validated is None else str(validated)
        self._create_con()

    @classmethod
    def validate_location(
        cls,
        db,
        *,
        input_roots=(),
        input_identities=(),
    ):
        return validate_sqlite_cache_location(
            db,
            input_roots=input_roots,
            input_identities=input_identities,
        )

    def __contains__(self, key):
        sql = "SELECT count(*) FROM pictures WHERE path = ?"
        result = self.con.execute(sql, [key]).fetchall()
        return result[0][0] > 0

    def __delitem__(self, key):
        if self.readonly:
            raise CacheSafetyError("read-only image cache cannot delete records")
        if key not in self:
            raise KeyError(key)
        self.con.execute("DELETE FROM pictures WHERE path = ?", [key])

    def __getitem__(self, key):
        where = "rowid = ?" if isinstance(key, int) else "path = ?"
        probe_names = (
            "path",
            "blocks",
            "blocks2",
            "blocks3",
            "blocks4",
            "blocks5",
            "blocks6",
            "blocks7",
            "blocks8",
            "file_size",
            "mtime_ns",
            "ctime_ns",
            "identity_json",
            "generation_token",
            "feature_version",
        )
        probe = self.con.execute(
            "SELECT {} FROM pictures WHERE {}".format(
                ",".join("typeof({0}),length({0})".format(name) for name in probe_names),
                where,
            ),
            [key],
        ).fetchone()
        if probe is None:
            raise KeyError(key)
        metadata = {name: (probe[index], probe[index + 1]) for name, index in zip(probe_names, range(0, len(probe), 2))}
        if metadata["path"][0] != "text" or not 0 < int(metadata["path"][1]) <= MAX_CACHE_PATH_CHARACTERS:
            raise ValueError("image cache path is invalid or oversized")
        for name in probe_names[1:9]:
            value_type, length = metadata[name]
            if value_type != "blob" or length is None or not 0 <= int(length) <= MAX_CACHE_BLOCK_BYTES:
                raise ValueError("image cache block is invalid or oversized")
        if any(metadata[name][0] != "integer" for name in ("file_size", "mtime_ns", "ctime_ns")):
            raise ValueError("image cache generation scalar has an invalid type")
        identity_type, identity_length = metadata["identity_json"]
        generation_type, generation_length = metadata["generation_token"]
        if identity_type == "null":
            if generation_type != "null":
                raise ValueError("unbound image cache row has a generation token")
        elif (
            identity_type != "text"
            or identity_length is None
            or not 0 < int(identity_length) <= MAX_CACHE_IDENTITY_CHARACTERS
            or generation_type != "blob"
            or generation_length is None
            or not 0 < int(generation_length) <= MAX_CACHE_GENERATION_TOKEN_BYTES
        ):
            raise ValueError("image cache source binding is invalid or oversized")
        feature_type, feature_length = metadata["feature_version"]
        if feature_type not in {"null", "text"} or (
            feature_type == "text"
            and (feature_length is None or not 0 < int(feature_length) <= MAX_CACHE_FEATURE_VERSION_CHARACTERS)
        ):
            raise ValueError("image cache feature version is invalid or oversized")
        sql = (
            "SELECT path,blocks,blocks2,blocks3,blocks4,blocks5,blocks6,blocks7,blocks8,"
            "file_size,mtime_ns,ctime_ns,identity_json,generation_token,feature_version "
            "FROM pictures WHERE {}".format(where)
        )
        row = self.con.execute(sql, [key]).fetchone()
        if row is None:
            raise KeyError(key)
        path_str = row[0]
        identity_json = row[12]
        generation_token = row[13]
        feature_version = row[14]
        if identity_json is not None:
            FileGenerationToken.from_encoded(bytes(generation_token))
            self._require_current_binding(
                path_str,
                row[9],
                row[10],
                row[11],
                identity_json,
                generation_token,
                feature_version,
                require_feature_version=False,
            )
        return [bytes_to_colors(block) for block in row[1:9]]

    def __iter__(self):
        def paths():
            sql = (
                "SELECT CASE WHEN typeof(path)='text' "
                "AND length(path) BETWEEN 1 AND ? THEN path ELSE NULL END "
                "FROM pictures"
            )
            for (path,) in self.con.execute(
                sql,
                (MAX_CACHE_PATH_CHARACTERS,),
            ):
                if path is None:
                    raise ValueError("image cache path is invalid or oversized")
                yield path

        return paths()

    def __len__(self):
        return self.con.execute("SELECT count(*) FROM pictures").fetchone()[0]

    def __setitem__(self, path_str, blocks):
        if self.readonly:
            raise CacheSafetyError("read-only image cache cannot write records")
        path_str = os.fspath(path_str)
        _require_bounded_cache_text(
            path_str,
            MAX_CACHE_PATH_CHARACTERS,
            "image cache path",
        )
        blocks = list(blocks)
        if len(blocks) > 8:
            raise ValueError("image block cache supports at most eight orientations")
        if any(len(block) > MAX_CACHE_BLOCK_BYTES // 3 for block in blocks):
            raise ValueError("image cache block exceeds its size limit")
        blocks = [colors_to_bytes(block) for block in blocks]
        for block in blocks:
            _require_bounded_cache_blob(
                block,
                MAX_CACHE_BLOCK_BYTES,
                "image cache block",
                allow_empty=True,
            )
        blocks.extend([b""] * (8 - len(blocks)))
        if os.path.lexists(path_str):
            binding = capture_source_binding(path_str)
            _validate_cache_binding_fields(binding)
            path_str = binding.path
            mtime_ns = binding.mtime_ns
            file_size = binding.size
            ctime_ns = binding.ctime_ns
            identity_json = binding.identity_json
            generation_token = binding.generation_token
        else:
            mtime_ns = file_size = ctime_ns = 0
            identity_json = None
            generation_token = None
        if path_str in self:
            sql = (
                "UPDATE pictures SET blocks=?,blocks2=?,blocks3=?,blocks4=?,blocks5=?,"
                "blocks6=?,blocks7=?,blocks8=?,mtime_ns=?,file_size=?,ctime_ns=?,identity_json=?,"
                "generation_token=?,"
                "width=NULL,height=NULL,frame_count=NULL,phashes=NULL,phash_count=NULL,"
                "thumbnail_width=NULL,thumbnail_height=NULL,"
                "thumbnail_key=NULL,feature_version=NULL WHERE path=?"
            )
        else:
            sql = (
                "INSERT INTO pictures("
                "blocks,blocks2,blocks3,blocks4,blocks5,blocks6,blocks7,blocks8,"
                "mtime_ns,file_size,ctime_ns,identity_json,generation_token,path"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            )
        self.con.execute(
            sql,
            blocks
            + [
                mtime_ns,
                file_size,
                ctime_ns,
                identity_json,
                generation_token,
                path_str,
            ],
        )

    def put_features(self, path_str, features, *, expected_binding=None):
        if self.readonly:
            raise CacheSafetyError("read-only image cache cannot write features")
        if features.orientation_count not in {1, 8}:
            raise ValueError("features require either one or eight orientations")
        if features.feature_version != FEATURE_VERSION:
            raise ValueError("features use an unsupported feature version")
        binding = capture_source_binding(path_str)
        if expected_binding is not None:
            _require_expected_source_binding(binding, expected_binding)
        _validate_cache_binding_fields(binding)
        path_str = binding.path
        if any(len(block) > MAX_CACHE_BLOCK_BYTES // 3 for block in features.blocks):
            raise ValueError("image cache block exceeds its size limit")
        encoded_blocks = [colors_to_bytes(block) for block in features.blocks]
        _validate_feature_write_payload(features, encoded_blocks)
        encoded_blocks.extend([b""] * (8 - len(encoded_blocks)))
        values = encoded_blocks + [
            binding.mtime_ns,
            binding.size,
            features.dimensions[0],
            features.dimensions[1],
            features.frame_count,
            _serialize_feature_payload(features),
            features.orientation_count,
            features.thumbnail_size[0],
            features.thumbnail_size[1],
            features.thumbnail_key,
            features.feature_version,
            binding.ctime_ns,
            binding.identity_json,
            binding.generation_token,
            path_str,
        ]
        if path_str in self:
            sql = (
                "UPDATE pictures SET "
                "blocks=?,blocks2=?,blocks3=?,blocks4=?,blocks5=?,blocks6=?,blocks7=?,blocks8=?,"
                "mtime_ns=?,file_size=?,width=?,height=?,frame_count=?,phashes=?,phash_count=?,"
                "thumbnail_width=?,thumbnail_height=?,thumbnail_key=?,feature_version=?,"
                "ctime_ns=?,identity_json=?,generation_token=? WHERE path=?"
            )
        else:
            sql = (
                "INSERT INTO pictures("
                "blocks,blocks2,blocks3,blocks4,blocks5,blocks6,blocks7,blocks8,"
                "mtime_ns,file_size,width,height,frame_count,phashes,phash_count,"
                "thumbnail_width,thumbnail_height,thumbnail_key,feature_version,"
                "ctime_ns,identity_json,generation_token,path"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            )
        self.con.execute(sql, values)

    def _probe_feature_rows(self, where, parameters):
        probe_columns = ",".join("typeof({0}),length({0})".format(column) for column in _FEATURE_ROW_COLUMNS)
        probes = self.con.execute(
            "SELECT rowid,{} FROM pictures WHERE {}".format(
                probe_columns,
                where,
            ),
            parameters,
        ).fetchall()
        result = {}
        for probe in probes:
            metadata = {
                column: (probe[index], probe[index + 1])
                for column, index in zip(
                    _FEATURE_ROW_COLUMNS,
                    range(1, len(probe), 2),
                )
            }
            self._validate_feature_probe(metadata)
            result[int(probe[0])] = metadata
        return result

    def _feature_row(self, key):
        where = "rowid = ?" if isinstance(key, int) else "path = ?"
        lookup = key if isinstance(key, int) else os.path.abspath(os.fspath(key))
        metadata_by_rowid = self._probe_feature_rows(where, [lookup])
        if not metadata_by_rowid:
            raise KeyError(key)
        row = self.con.execute(
            "SELECT rowid,{} FROM pictures WHERE {}".format(
                ",".join(_FEATURE_ROW_COLUMNS),
                where,
            ),
            [lookup],
        ).fetchone()
        if row is None:
            raise KeyError(key)
        metadata = metadata_by_rowid.get(int(row[0]))
        if metadata is None:
            raise KeyError(key)
        self._validate_feature_payload(row, metadata)
        self._require_current_binding(
            row[1],
            row[20],
            row[19],
            row[21],
            row[22],
            row[23],
            row[18],
        )
        return row

    @staticmethod
    def _validate_feature_probe(metadata):
        text_limits = {
            "path": MAX_CACHE_PATH_CHARACTERS,
            "thumbnail_key": MAX_CACHE_THUMBNAIL_KEY_CHARACTERS,
            "feature_version": MAX_CACHE_FEATURE_VERSION_CHARACTERS,
            "identity_json": MAX_CACHE_IDENTITY_CHARACTERS,
        }
        blob_limits = {
            **{
                name: MAX_CACHE_BLOCK_BYTES
                for name in (
                    "blocks",
                    "blocks2",
                    "blocks3",
                    "blocks4",
                    "blocks5",
                    "blocks6",
                    "blocks7",
                    "blocks8",
                )
            },
            "phashes": MAX_FEATURE_PAYLOAD_BYTES,
            "generation_token": MAX_CACHE_GENERATION_TOKEN_BYTES,
        }
        integer_columns = {
            "width",
            "height",
            "frame_count",
            "phash_count",
            "thumbnail_width",
            "thumbnail_height",
            "mtime_ns",
            "file_size",
            "ctime_ns",
        }
        for name, maximum in text_limits.items():
            value_type, length = metadata[name]
            if value_type != "text" or length is None or not 0 < int(length) <= maximum:
                raise ValueError("image cache {} text is invalid or oversized".format(name))
        for name, maximum in blob_limits.items():
            value_type, length = metadata[name]
            minimum = 0 if name.startswith("blocks") else 1
            if value_type != "blob" or length is None or not minimum <= int(length) <= maximum:
                raise ValueError("image cache {} blob is invalid or oversized".format(name))
        if any(metadata[name][0] != "integer" for name in integer_columns):
            raise ValueError("image cache scalar metadata has an invalid SQLite type")

    @staticmethod
    def _validate_feature_payload(row, metadata):
        (
            _rowid,
            path,
            *payload,
        ) = row
        (
            *encoded_blocks,
            width,
            height,
            frame_count,
            _phashes,
            phash_count,
            thumbnail_width,
            thumbnail_height,
            thumbnail_key,
            feature_version,
            mtime_ns,
            file_size,
            ctime_ns,
            identity_json,
            generation_token,
        ) = payload
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("image cache path payload is invalid")
        scalar_values = (
            width,
            height,
            frame_count,
            phash_count,
            thumbnail_width,
            thumbnail_height,
            mtime_ns,
            file_size,
            ctime_ns,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) for value in scalar_values):
            raise ValueError("image cache scalar payload is invalid")
        if (
            width <= 0
            or height <= 0
            or frame_count <= 0
            or phash_count not in {1, 8}
            or thumbnail_width <= 0
            or thumbnail_height <= 0
            or thumbnail_width > MAX_CACHE_THUMBNAIL_EDGE
            or thumbnail_height > MAX_CACHE_THUMBNAIL_EDGE
            or min(mtime_ns, file_size, ctime_ns) < 0
        ):
            raise ValueError("image cache scalar payload is outside its domain")
        block_lengths = [
            int(metadata[name][1])
            for name in (
                "blocks",
                "blocks2",
                "blocks3",
                "blocks4",
                "blocks5",
                "blocks6",
                "blocks7",
                "blocks8",
            )
        ]
        active_lengths = block_lengths[:phash_count]
        if (
            any(length <= 0 or length % 3 for length in active_lengths)
            or len(set(active_lengths)) != 1
            or any(length != 0 for length in block_lengths[phash_count:])
            or any(not isinstance(value, (bytes, bytearray, memoryview)) for value in encoded_blocks)
        ):
            raise ValueError("image cache block payload is inconsistent")
        if (
            not isinstance(thumbnail_key, str)
            or not isinstance(feature_version, str)
            or not isinstance(identity_json, str)
        ):
            raise ValueError("image cache text payload is invalid")
        if not isinstance(generation_token, (bytes, bytearray, memoryview)):
            raise ValueError("image cache generation token payload is invalid")
        FileGenerationToken.from_encoded(bytes(generation_token))

    @staticmethod
    def _require_current_binding(
        path_str,
        file_size,
        mtime_ns,
        ctime_ns,
        identity_json,
        generation_token,
        feature_version,
        *,
        require_feature_version=True,
    ):
        if require_feature_version and feature_version != FEATURE_VERSION:
            raise KeyError(path_str)
        current = capture_source_binding(path_str)
        if (
            current.size != int(file_size)
            or current.mtime_ns != int(mtime_ns)
            or current.identity_json != identity_json
            or current.generation_token != bytes(generation_token)
        ):
            raise KeyError(path_str)
        return current

    def get_features(self, key):
        row = self._feature_row(key)
        rowid, path_str = row[:2]
        encoded_blocks = row[2:10]
        width, height, frame_count, phashes, phash_count = row[10:15]
        thumbnail_width, thumbnail_height, thumbnail_key, feature_version = row[15:19]
        block_count = int(phash_count)
        blocks = tuple(tuple(bytes_to_colors(block or b"")) for block in encoded_blocks[:block_count])
        payload = _deserialize_feature_payload(phashes, block_count)
        result = CachedImageFeatures(
            rowid=rowid,
            path=path_str,
            dimensions=(int(width), int(height)),
            phashes=payload.phashes,
            dhashes=payload.dhashes,
            color_histogram=payload.color_histogram,
            tile_fingerprints=payload.tile_fingerprints,
            quality=payload.quality,
            feature_version=feature_version,
            frame_count=int(frame_count),
            blocks=blocks,
            thumbnail_size=(int(thumbnail_width), int(thumbnail_height)),
            thumbnail_key=thumbnail_key,
        )
        self._require_current_binding(
            path_str,
            row[20],
            row[19],
            row[21],
            row[22],
            row[23],
            feature_version,
        )
        return result

    def get_feature_metadata(self, key):
        row = self._feature_row(key)
        rowid, path_str, width, height, phashes, phash_count, feature_version = (
            row[0],
            row[1],
            row[10],
            row[11],
            row[13],
            row[14],
            row[18],
        )
        payload = _deserialize_feature_payload(phashes, int(phash_count))
        result = CachedImageFeatureMetadata(
            rowid=rowid,
            path=path_str,
            dimensions=(int(width), int(height)),
            phashes=payload.phashes,
            dhashes=payload.dhashes,
            color_histogram=payload.color_histogram,
            tile_fingerprints=payload.tile_fingerprints,
            quality=payload.quality,
            feature_version=feature_version,
        )
        self._require_current_binding(
            path_str,
            row[20],
            row[19],
            row[21],
            row[22],
            row[23],
            feature_version,
        )
        return result

    def _connect(self, *, readonly):
        if self.dbname == ":memory:":
            return sqlite.connect(":memory:", isolation_level=None)
        if readonly:
            return sqlite.connect(
                _readonly_uri(Path(self.dbname)),
                isolation_level=None,
                uri=True,
            )
        return sqlite.connect(self.dbname, isolation_level=None)

    def _configure_connection(self, *, query_only):
        _apply_sqlite_runtime_limits(self.con)
        try:
            self.con.execute("PRAGMA trusted_schema = OFF")
            trusted_schema = self.con.execute("PRAGMA trusted_schema").fetchone()
            if trusted_schema is None or int(trusted_schema[0]) != 0:
                raise CacheSafetyError("SQLite trusted_schema could not be disabled for the image cache")
            self.con.execute("PRAGMA foreign_keys = ON")
            foreign_keys = self.con.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or int(foreign_keys[0]) != 1:
                raise CacheSafetyError("SQLite foreign keys could not be enabled for the image cache")
            self.con.execute("PRAGMA query_only = {}".format("ON" if query_only else "OFF"))
            query_only_row = self.con.execute("PRAGMA query_only").fetchone()
            expected = 1 if query_only else 0
            if query_only_row is None or int(query_only_row[0]) != expected:
                raise CacheSafetyError("SQLite query_only mode could not be configured for the image cache")
        except CacheSafetyError:
            raise
        except sqlite.Error as error:
            raise CacheSafetyError("image cache SQLite safety settings could not be configured") from error

    @staticmethod
    def _require_database_identity(
        path,
        expected_identity,
        *,
        require_header,
        require_empty=False,
    ):
        if require_header:
            _read_existing_header(path, expected_identity)
        file_stat, current_identity = _require_plain_regular_file(
            path,
            single_link=True,
        )
        if (
            same_physical_file(
                expected_identity,
                current_identity,
            ).verdict
            is not IdentityVerdict.SAME
        ):
            raise CacheSafetyError("image cache identity changed while SQLite opened it")
        if require_empty and int(file_stat.st_size) != 0:
            raise CacheSafetyError("new image cache changed before SQLite initialization")
        return current_identity

    def _open_verified_writable(
        self,
        path,
        expected_identity,
        *,
        replace_retired=False,
    ):
        """Open the writable connection while a verified path lease is held."""

        with _hold_database_path_identity(
            path,
            expected_identity,
            allow_content_change=replace_retired,
        ):
            _read_existing_header(path, expected_identity)
            _require_no_sqlite_sidecars(path)
            self.con = self._connect(readonly=False)
            self._configure_connection(query_only=False)
            current_identity = self._require_database_identity(
                path,
                expected_identity,
                require_header=False,
            )
            self._validate_or_migrate()
            _require_no_sqlite_sidecars(path)
            return current_identity

    def _create_con(self):
        if self.dbname == ":memory:":
            if self.readonly:
                raise CacheSafetyError("an in-memory cache cannot be opened read-only")
            self.con = self._connect(readonly=False)
            self._configure_connection(query_only=False)
            self._initialize_new()
            return

        path = Path(self.dbname)
        _require_no_sqlite_sidecars(path)
        existed = os.path.lexists(path)
        if self.readonly and not existed:
            raise CacheSafetyError("read-only image cache does not exist")
        if existed:
            _file_stat, expected_identity = _require_plain_regular_file(path, single_link=True)
            _read_existing_header(path, expected_identity)
        else:
            expected_identity = _reserve_new_database(path)
        try:
            if existed:
                # An existing file is always inspected through a read-only
                # SQLite handle first.  Unknown/corrupt schemas therefore
                # cannot trigger recovery, replacement, or migration writes.
                self.con = self._connect(readonly=True)
                self._configure_connection(query_only=True)
                self._require_database_identity(
                    path,
                    expected_identity,
                    require_header=True,
                )
                _require_no_sqlite_sidecars(path)
                version = self._validate_schema(None)
                if version != self.schema_version and (self.readonly or version != _RETIRED_SCHEMA_VERSION):
                    raise CacheSchemaError("image cache schema {} is unsupported for this open mode".format(version))
            else:
                self.con = self._connect(readonly=False)
                self._require_database_identity(
                    path,
                    expected_identity,
                    require_header=False,
                    require_empty=True,
                )
                self._configure_connection(query_only=False)
                self._initialize_new()
            if self.readonly:
                _require_no_sqlite_sidecars(path)
                current_identity = self._require_database_identity(
                    path,
                    expected_identity,
                    require_header=True,
                )
            else:
                self.con.close()
                self.con = None
                _require_no_sqlite_sidecars(path)
                current_identity = self._open_verified_writable(
                    path,
                    expected_identity,
                    replace_retired=existed and version == _RETIRED_SCHEMA_VERSION,
                )
            self._database_identity = current_identity
        except BaseException:
            if self.con is not None:
                self.con.close()
                self.con = None
            raise

    def _initialize_new(self):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.con.execute(
                "PRAGMA application_id = {}".format(
                    SQLITE_APPLICATION_ID,
                )
            )
            self.con.execute(self.create_schema_version_query)
            self.con.execute(
                "INSERT INTO schema_version(version,description) VALUES(?,?)",
                (self.schema_version, self.schema_version_description),
            )
            self.con.execute(self.create_table_query)
            self.con.execute(self.create_index_query)
            self.con.execute("PRAGMA user_version = {}".format(self.schema_version))
            self.con.commit()
        except BaseException:
            self.con.rollback()
            raise
        self._validate_schema(self.schema_version)

    def _validate_or_migrate(self):
        version = self._validate_schema(None)
        if version == self.schema_version:
            return
        if version != _RETIRED_SCHEMA_VERSION:
            raise CacheSchemaError("unsupported image cache schema version: {}".format(version))
        self._replace_retired_schema()

    def _replace_retired_schema(self):
        """Discard a validated obsolete cache and reclaim all of its pages."""

        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.con.execute("DROP TABLE pictures")
            self.con.execute("DROP TABLE schema_version")
            self.con.execute(self.create_schema_version_query)
            self.con.execute(
                "INSERT INTO schema_version(version,description) VALUES(?,?)",
                (self.schema_version, self.schema_version_description),
            )
            self.con.execute(self.create_table_query)
            self.con.execute(self.create_index_query)
            self.con.execute("PRAGMA user_version = {}".format(self.schema_version))
            self.con.commit()
        except BaseException:
            self.con.rollback()
            raise
        _vacuum_owned_database(self.con)
        self._validate_schema(self.schema_version)

    def _validate_schema(self, expected_version):
        try:
            quick_check = self.con.execute("PRAGMA quick_check(1)").fetchone()
            if quick_check is None or tuple(quick_check) != ("ok",):
                raise CacheSchemaError("image cache integrity check failed")
            application_id = int(self.con.execute("PRAGMA application_id").fetchone()[0])
            if application_id != SQLITE_APPLICATION_ID:
                raise CacheSchemaError("image cache application ID is missing or unsupported")
            if _bounded_schema_objects(self.con) != _EXPECTED_SCHEMA_OBJECTS:
                raise CacheSchemaError("image cache contains an unsupported object set")
            schema_info = _bounded_table_columns(
                self.con,
                "schema_version",
                2,
            )
            if schema_info != (
                ("version", "INTEGER", 0, 1),
                ("description", "TEXT", 1, 0),
            ):
                raise CacheSchemaError("image cache schema-version table shape is unsupported")
            rows = self.con.execute(
                """
                SELECT
                    typeof(version),
                    CASE WHEN typeof(version) = 'integer' THEN version END,
                    typeof(description),
                    CASE
                        WHEN typeof(description) = 'text'
                            AND length(CAST(description AS BLOB)) BETWEEN 1 AND ?
                        THEN description
                    END
                FROM schema_version
                LIMIT 2
                """,
                (_MAX_SCHEMA_DESCRIPTION_BYTES,),
            ).fetchall()
            if len(rows) != 1:
                raise CacheSchemaError("image cache schema version record is invalid")
            version_type, version_value, description_type, description = rows[0]
            if version_type != "integer" or version_value is None or description_type != "text":
                raise CacheSchemaError("image cache schema version record is invalid")
            version = int(version_value)
            if version == self.schema_version:
                expected_description = self.schema_version_description
                picture_columns = _PICTURE_COLUMNS_V6
                picture_table_query = self.create_table_query
            elif version == _RETIRED_SCHEMA_VERSION:
                expected_description = _RETIRED_SCHEMA_DESCRIPTION
                picture_columns = _PICTURE_COLUMNS_V5
                picture_table_query = _CREATE_TABLE_QUERY_V5
            else:
                raise CacheSchemaError("image cache schema version is unsupported")
            if description != expected_description:
                raise CacheSchemaError("image cache schema version record is invalid")
            if expected_version is not None and version != expected_version:
                raise CacheSchemaError(
                    "image cache schema version mismatch: {} != {}".format(
                        version,
                        expected_version,
                    )
                )
            user_version = int(self.con.execute("PRAGMA user_version").fetchone()[0])
            if user_version != version:
                raise CacheSchemaError("image cache schema version metadata is unsupported")
            picture_info = _bounded_table_columns(
                self.con,
                "pictures",
                len(picture_columns),
            )
            if picture_info is None:
                raise CacheSchemaError("image cache pictures table shape is unsupported")
            columns = tuple(row[0] for row in picture_info)
            if columns != picture_columns:
                raise CacheSchemaError("image cache pictures table shape is unsupported")
            blob_columns = {
                "blocks",
                "blocks2",
                "blocks3",
                "blocks4",
                "blocks5",
                "blocks6",
                "blocks7",
                "blocks8",
                "phashes",
                "generation_token",
            }
            if version == _RETIRED_SCHEMA_VERSION:
                blob_columns.add("thumbnail")
            text_columns = {
                "path",
                "thumbnail_key",
                "feature_version",
                "identity_json",
            }
            for name, declared_type, not_null, primary_key in picture_info:
                expected_type = "BLOB" if name in blob_columns else "TEXT" if name in text_columns else "INTEGER"
                expected_not_null = int(name in {"path", "mtime_ns", "file_size"})
                if declared_type != expected_type or not_null != expected_not_null or primary_key != 0:
                    raise CacheSchemaError("image cache pictures table declaration is unsupported")
            indexes = self.con.execute(
                """
                SELECT
                    seq,
                    CASE
                        WHEN typeof(name) = 'text'
                            AND length(CAST(name AS BLOB)) BETWEEN 1 AND ?
                        THEN name
                    END,
                    "unique",
                    origin,
                    partial
                FROM pragma_index_list(?)
                ORDER BY seq
                LIMIT 2
                """,
                (_MAX_SCHEMA_NAME_BYTES, "pictures"),
            ).fetchall()
            if indexes != [(0, "idx_path", 1, "c", 0)]:
                raise CacheSchemaError("image cache path index is missing or not unique")
            index_columns = self.con.execute(
                """
                SELECT
                    seqno,
                    cid,
                    CASE
                        WHEN typeof(name) = 'text'
                            AND length(CAST(name AS BLOB)) BETWEEN 1 AND ?
                        THEN name
                    END
                FROM pragma_index_info(?)
                ORDER BY seqno
                LIMIT 2
                """,
                (_MAX_SCHEMA_NAME_BYTES, "idx_path"),
            ).fetchall()
            if index_columns != [(0, 0, "path")]:
                raise CacheSchemaError("image cache path index has an unsupported shape")
            expected_sql = (
                ("table", "schema_version", self.create_schema_version_query),
                ("table", "pictures", picture_table_query),
                ("index", "idx_path", self.create_index_query),
            )
            for object_type, name, sql in expected_sql:
                if _bounded_schema_sql(self.con, object_type, name) != _normalize_schema_sql(sql):
                    raise CacheSchemaError("image cache schema SQL is unsupported")
            return version
        except CacheSafetyError:
            raise
        except (OverflowError, TypeError, UnicodeError, ValueError, sqlite.DatabaseError) as error:
            raise CacheSchemaError("image cache schema could not be validated: {}".format(error)) from error

    def clear(self):
        if self.readonly:
            raise CacheSafetyError("read-only image cache cannot be cleared")
        if self.dbname != ":memory:":
            path = Path(self.dbname)
            _file_stat, current_identity = _require_plain_regular_file(
                path,
                single_link=True,
            )
            if (
                self._database_identity is None
                or same_physical_file(
                    self._database_identity,
                    current_identity,
                ).verdict
                is not IdentityVerdict.SAME
            ):
                raise CacheSafetyError("image cache identity changed before explicit clear")
        with self.con:
            self.con.execute("DELETE FROM pictures")
        _vacuum_owned_database(self.con)
        if self.dbname != ":memory:":
            _require_no_sqlite_sidecars(path)
            _file_stat, vacuumed_identity = _require_plain_regular_file(
                path,
                single_link=True,
            )
            if (
                same_physical_file(
                    self._database_identity,
                    vacuumed_identity,
                ).verdict
                is not IdentityVerdict.SAME
            ):
                raise CacheSafetyError("image cache identity changed while reclaiming cleared storage")

    def close(self):
        if self.con is not None:
            self.con.close()
        self.con = None

    def filter(self, func):
        to_delete = [key for key in self if not func(key)]
        for key in to_delete:
            del self[key]

    def get_id(self, path):
        result = self.con.execute(
            "SELECT rowid FROM pictures WHERE path = ?",
            [path],
        ).fetchone()
        if result:
            return result[0]
        raise ValueError(path)

    def get_multiple(self, rowids):
        ids = tuple(int(rowid) for rowid in rowids)
        if not ids:
            return iter(())
        if len(ids) > MAX_FEATURE_BATCH_ROWS:
            raise ValueError("image feature batch exceeds {} rows".format(MAX_FEATURE_BATCH_ROWS))
        placeholders = ",".join("?" for _ in ids)
        metadata_by_rowid = self._probe_feature_rows(
            "rowid IN ({})".format(placeholders),
            ids,
        )
        if len(metadata_by_rowid) != len(set(ids)):
            raise KeyError("one or more cached image feature rows are missing")
        sql = (
            "SELECT rowid,path,blocks,blocks2,blocks3,blocks4,blocks5,blocks6,blocks7,blocks8,"
            "file_size,mtime_ns,ctime_ns,identity_json,generation_token,feature_version,phash_count "
            "FROM pictures WHERE rowid IN ({}) ORDER BY rowid".format(placeholders)
        )
        rows = self.con.execute(sql, ids).fetchall()
        if len(rows) != len(set(ids)):
            raise KeyError("one or more cached image feature rows are missing")
        result = []
        for row in rows:
            metadata = metadata_by_rowid[int(row[0])]
            block_lengths = [
                int(metadata[name][1])
                for name in (
                    "blocks",
                    "blocks2",
                    "blocks3",
                    "blocks4",
                    "blocks5",
                    "blocks6",
                    "blocks7",
                    "blocks8",
                )
            ]
            phash_count = int(row[16])
            if (
                phash_count not in {1, 8}
                or any(length <= 0 or length % 3 for length in block_lengths[:phash_count])
                or len(set(block_lengths[:phash_count])) != 1
                or any(length != 0 for length in block_lengths[phash_count:])
            ):
                raise ValueError("image cache block payload is inconsistent")
            FileGenerationToken.from_encoded(bytes(row[14]))
            self._require_current_binding(
                row[1],
                row[10],
                row[11],
                row[12],
                row[13],
                row[14],
                row[15],
            )
            result.append(
                (
                    row[0],
                    [bytes_to_colors(block) for block in row[2:10]],
                )
            )
        return iter(result)

    def purge_outdated(self):
        if self.readonly:
            raise CacheSafetyError("read-only image cache cannot purge records")
        last_rowid = 0
        while True:
            rowids = tuple(
                int(row[0])
                for row in self.con.execute(
                    "SELECT rowid FROM pictures WHERE rowid > ? " "ORDER BY rowid LIMIT ?",
                    (last_rowid, MAX_FEATURE_BATCH_ROWS),
                )
            )
            if not rowids:
                break
            last_rowid = rowids[-1]
            to_delete = []
            for rowid in rowids:
                try:
                    self._feature_row(rowid)
                except (CacheSafetyError, KeyError, TypeError, ValueError):
                    to_delete.append(rowid)
            if to_delete:
                placeholders = ",".join("?" for _ in to_delete)
                self.con.execute(
                    "DELETE FROM pictures WHERE rowid IN ({})".format(placeholders),
                    to_delete,
                )


__all__ = [
    "CacheSafetyError",
    "CacheSchemaError",
    "CacheSourceBinding",
    "CachedImageFeatureMetadata",
    "CachedImageFeatures",
    "SqliteCache",
    "capture_source_binding",
    "validate_sqlite_cache_location",
]
