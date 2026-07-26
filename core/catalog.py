# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Durable, resumable metadata catalog for large file libraries.

The catalog is deliberately independent from the existing hash and picture caches. Those caches are
rebuildable path-keyed accelerators, while this module records scan coverage, physical identity,
content generations, derived artifacts, and safety evidence.

The SQLite database **must live on a local filesystem**. The library being indexed may be on a NAS,
but SQLite WAL mode relies on same-host shared memory and must not be placed on a network share.
Callers are responsible for choosing a local application-data path.

Only one writer should own a :class:`Catalog`. Hashing and media workers should return results to
that writer through a bounded queue. Public mutation methods use explicit atomic transactions and
can be composed inside :meth:`Catalog.transaction`.
"""

import json
import ntpath
import os
import posixpath
import sqlite3
import stat
import sys
import threading
import time
import unicodedata

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

SCHEMA_VERSION = 4
DEFAULT_BUSY_TIMEOUT_MS = 5000
MAX_PAGE_SIZE = 10000
MAX_SQLITE_INTEGER = (1 << 63) - 1
MAX_WORK_ITEM_PAYLOAD_BYTES = 256 * 1024
CATALOG_APPLICATION_ID = 0x44474E43  # "DGNC"
CATALOG_OWNER = "dupeguru-neo-catalog"
_SQLITE_HEADER_MAGIC = b"SQLite format 3\0"
_SQLITE_HEADER_SIZE = 100
_SQLITE_APPLICATION_ID_OFFSET = 68
_MAX_CATALOG_SCHEMA_OBJECTS = 128
_MAX_CATALOG_SCHEMA_NAME_BYTES = 128
_MAX_CATALOG_META_ROWS = 2
_MAX_CATALOG_META_KEY_BYTES = 64
_MAX_CATALOG_META_VALUE_BYTES = 256
_SQLITE_CONNECTION_LIMITS = (
    ("SQLITE_LIMIT_LENGTH", 64 * 1024 * 1024),
    ("SQLITE_LIMIT_SQL_LENGTH", 1024 * 1024),
    ("SQLITE_LIMIT_COLUMN", 256),
    ("SQLITE_LIMIT_EXPR_DEPTH", 100),
    ("SQLITE_LIMIT_COMPOUND_SELECT", 20),
    ("SQLITE_LIMIT_VDBE_OP", 100_000),
    ("SQLITE_LIMIT_FUNCTION_ARG", 32),
    ("SQLITE_LIMIT_ATTACHED", 0),
    ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 4096),
    ("SQLITE_LIMIT_VARIABLE_NUMBER", 32_766),
    ("SQLITE_LIMIT_TRIGGER_DEPTH", 0),
    ("SQLITE_LIMIT_WORKER_THREADS", 0),
)

IDENTITY_CONFIDENCE_VALUES = {"stable", "session_only", "path_only"}
CONTENT_STATE_VALUES = {"stable", "unstable", "unreadable", "missing"}
PATH_STATE_VALUES = {"active", "missing", "unreadable"}
SCAN_STATUS_VALUES = {"running", "cancelled", "complete", "completed_with_errors", "failed"}
SCAN_DIRECTORY_STATUS_VALUES = {"pending", "in_progress", "complete", "unreachable", "failed"}
WORK_STATUS_VALUES = {"pending", "in_progress", "complete", "failed"}
VERIFICATION_STATE_VALUES = {"candidate", "verified", "invalidated"}
ACTION_STATUS_VALUES = {"planned", "in_progress", "completed", "failed", "cancelled"}

_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

_V1_TABLE_COLUMNS = {
    "catalog_meta": ("key", "value"),
    "volumes": (
        "id",
        "volume_key",
        "platform",
        "fs_type",
        "identity_capability",
        "timestamp_granularity_ns",
        "created_at",
        "last_probe_at",
    ),
    "scans": (
        "id",
        "started_at",
        "finished_at",
        "status",
        "phase",
        "app_version",
        "error_count",
        "resume_of_scan_id",
    ),
    "roots": (
        "id",
        "volume_id",
        "display_path",
        "path_key",
        "policy",
        "created_at",
        "updated_at",
        "last_complete_scan_id",
    ),
    "physical_files": (
        "id",
        "volume_id",
        "native_file_id",
        "identity_confidence",
        "current_content_version_id",
        "first_seen_scan_id",
        "last_seen_scan_id",
        "first_seen_at",
        "last_seen_at",
    ),
    "paths": (
        "id",
        "root_id",
        "physical_file_id",
        "display_path",
        "path_key",
        "parent_path_key",
        "state",
        "first_seen_scan_id",
        "last_seen_scan_id",
        "first_seen_at",
        "last_seen_at",
    ),
    "content_versions": (
        "id",
        "physical_file_id",
        "size",
        "mtime_ns",
        "change_token",
        "state",
        "observed_before",
        "observed_after",
        "first_seen_scan_id",
        "last_seen_scan_id",
    ),
    "artifacts": (
        "id",
        "content_version_id",
        "kind",
        "algorithm",
        "algorithm_version",
        "parameters_hash",
        "value",
        "verification_level",
        "created_at",
        "updated_at",
    ),
    "scan_dirs": (
        "id",
        "scan_id",
        "root_id",
        "display_path",
        "path_key",
        "status",
        "attempts",
        "lease_owner",
        "lease_until",
        "error_count",
        "created_at",
        "updated_at",
    ),
    "scan_errors": (
        "id",
        "scan_id",
        "scan_dir_id",
        "path",
        "operation",
        "error_code",
        "message",
        "transient",
        "created_at",
    ),
    "work_items": (
        "id",
        "scan_id",
        "content_version_id",
        "kind",
        "status",
        "priority",
        "attempts",
        "lease_owner",
        "lease_until",
        "payload_json",
        "last_error",
        "created_at",
        "updated_at",
    ),
    "verification_records": (
        "id",
        "first_content_version_id",
        "second_content_version_id",
        "algorithm",
        "algorithm_version",
        "full_digest",
        "byte_compare_at",
        "state",
        "created_at",
        "updated_at",
    ),
}

_V2_TABLE_COLUMNS = {
    **_V1_TABLE_COLUMNS,
    "action_journal": (
        "id",
        "action_type",
        "status",
        "scan_id",
        "verification_id",
        "physical_file_id",
        "path_id",
        "payload_json",
        "error",
        "created_at",
        "updated_at",
        "completed_at",
    ),
}

_V3_TABLE_COLUMNS = {
    **_V2_TABLE_COLUMNS,
    "scan_snapshots": (
        "scan_id",
        "completed_at",
        "root_count",
        "path_count",
    ),
    "scan_path_observations": (
        "id",
        "scan_id",
        "root_id",
        "volume_id",
        "path_id",
        "physical_file_id",
        "content_version_id",
        "display_path",
        "path_key",
        "parent_path_key",
        "path_state",
        "content_state",
        "native_file_id",
        "identity_confidence",
        "observed_at",
    ),
}

_SCHEMA_TABLE_COLUMNS = {
    1: _V1_TABLE_COLUMNS,
    2: _V2_TABLE_COLUMNS,
    3: _V3_TABLE_COLUMNS,
    4: _V3_TABLE_COLUMNS,
}

_V1_INDEXES = frozenset({"physical_files_native_identity"})
_V2_INDEXES = _V1_INDEXES | frozenset(
    {
        "paths_physical_file",
        "paths_last_seen",
        "content_versions_physical",
        "artifacts_lookup",
        "scan_dirs_claim",
        "scan_errors_scan",
        "work_items_claim",
        "verification_digest",
        "action_journal_status",
    }
)
_V3_INDEXES = _V2_INDEXES | frozenset(
    {
        "scan_path_observations_identity",
        "scan_path_observations_content",
    }
)
_SCHEMA_INDEXES = {
    1: _V1_INDEXES,
    2: _V2_INDEXES,
    3: _V3_INDEXES,
    4: _V3_INDEXES,
}


class CatalogError(Exception):
    """Base class for catalog failures."""


class CatalogIntegrityError(CatalogError):
    """The database failed an SQLite integrity check."""


class CatalogCorruptError(CatalogIntegrityError):
    """The database cannot be read as a valid SQLite database."""


class CatalogSchemaError(CatalogError):
    """The database schema is invalid or cannot be migrated safely."""


class CatalogTooNewError(CatalogSchemaError):
    """The database was created by a newer, unsupported catalog implementation."""


class CatalogStateError(CatalogError):
    """An operation is invalid for the current durable state."""


class ScanIncompleteError(CatalogStateError):
    """A scan cannot be finalized while durable work is still pending."""


class CatalogPathError(CatalogError):
    """The SQLite path cannot safely identify one private local catalog."""


@dataclass(frozen=True)
class CatalogFileInspection:
    """Read-only ownership and schema facts established before a writable open."""

    schema_version: int
    legacy: bool
    application_id: int


@dataclass(frozen=True)
class ExactDigestProjectionCounts:
    """Database-side size summary for current exact duplicate groups."""

    group_count: int
    file_count: int
    max_group_members: int


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _path_key(path: Union[str, os.PathLike]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _same_file_stat(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        int(first.st_dev),
        int(first.st_ino),
    ) == (
        int(second.st_dev),
        int(second.st_ino),
    )


def _path_components(path: Path) -> Iterator[Path]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    if current:
        yield current
    for part in absolute.parts[1:]:
        current = current / part
        yield current


def _require_plain_catalog_parent(parent: Path) -> Path:
    parent = Path(os.path.abspath(os.fspath(parent)))
    if str(parent).startswith(("\\\\", "//")):
        raise CatalogPathError("catalog database cannot be stored on a UNC path")
    inspected_components = []
    for component in _path_components(parent):
        try:
            component_stat = os.lstat(component)
        except OSError as error:
            raise CatalogPathError(
                "catalog database parent component is unavailable: '{}'".format(component)
            ) from error
        if (
            stat.S_ISLNK(component_stat.st_mode)
            or _is_reparse_point(component_stat)
            or not stat.S_ISDIR(component_stat.st_mode)
        ):
            raise CatalogPathError(
                "catalog database parent components must be plain directories: '{}'".format(component)
            )
        inspected_components.append((component, component_stat))
    try:
        canonical = Path(os.path.realpath(os.fspath(parent)))
    except OSError as error:
        raise CatalogPathError(
            "catalog database parent could not be resolved canonically: '{}'".format(parent)
        ) from error
    if _path_key(canonical) != _path_key(parent):
        raise CatalogPathError("catalog database parent must not traverse links or reparse points: '{}'".format(parent))
    if os.name != "nt":
        effective_uid = getattr(os, "geteuid", lambda: None)()
        parent_stat = inspected_components[-1][1]
        if effective_uid is not None and int(parent_stat.st_uid) != int(effective_uid):
            raise CatalogPathError(
                "catalog database parent must be owned by the current user: " "'{}'".format(canonical)
            )
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise CatalogPathError(
                "catalog database parent must not be writable by group or " "other users: '{}'".format(canonical)
            )
        for component, component_stat in inspected_components[:-1]:
            mode = stat.S_IMODE(component_stat.st_mode)
            if not mode & 0o022:
                continue
            trusted_sticky_owner = (
                bool(component_stat.st_mode & stat.S_ISVTX)
                and effective_uid is not None
                and int(component_stat.st_uid) in {0, int(effective_uid)}
            )
            if not trusted_sticky_owner:
                raise CatalogPathError(
                    "catalog database ancestor is replaceable by another user: " "'{}'".format(component)
                )
    _require_local_catalog_filesystem(canonical)
    return canonical


def _require_local_catalog_filesystem(path: Path) -> None:
    if os.name == "nt":
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(Path(path.anchor)))
        if drive_type in {0, 1, 4}:
            raise CatalogPathError("catalog SQLite database must be stored on a known local drive")
        return

    mount_info = Path("/proc/self/mountinfo")
    if not mount_info.is_file():
        return
    resolved = str(path.resolve(strict=True))
    selected = None
    try:
        lines = mount_info.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise CatalogPathError("catalog filesystem type could not be inspected") from error
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = fields[4].replace("\\040", " ")
            filesystem = fields[separator + 1]
        except (IndexError, ValueError):
            continue
        try:
            within = os.path.commonpath((resolved, mount_point)) == mount_point
        except ValueError:
            within = False
        if within and (selected is None or len(mount_point) > len(selected[0])):
            selected = (mount_point, filesystem)
    if selected is None:
        return
    network_filesystems = {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb3",
    }
    if selected[1].lower() in network_filesystems:
        raise CatalogPathError(
            "catalog SQLite database cannot be stored on network filesystem '{}'".format(selected[1])
        )


def preflight_catalog_path(
    database_path: Union[str, os.PathLike],
    *,
    must_exist: bool,
) -> Path:
    """Validate one filesystem-backed private catalog path without opening SQLite.

    Every existing ancestor is checked with ``lstat``. Existing catalog files
    must be plain, canonically addressed, and have exactly one filesystem link.
    This function never creates the database or a SQLite sidecar.
    """

    raw = os.fspath(database_path)
    if not raw or raw == ":memory:" or "\0" in raw:
        raise CatalogPathError("catalog requires a filesystem-backed local database path")
    if raw.startswith(("\\\\", "//")):
        raise CatalogPathError("catalog database cannot be stored on a UNC path")
    path = Path(os.path.abspath(raw))
    _require_plain_catalog_parent(path.parent)
    exists = os.path.lexists(path)
    if must_exist and not exists:
        raise CatalogPathError("catalog database does not exist: '{}'".format(path))
    if not exists:
        return path
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise CatalogPathError("catalog database is unavailable: '{}'".format(path)) from error
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise CatalogPathError("catalog database must be one plain regular file: '{}'".format(path))
    if int(getattr(file_stat, "st_nlink", 0)) != 1:
        raise CatalogPathError("catalog database must have exactly one filesystem link: '{}'".format(path))
    try:
        canonical = Path(os.path.realpath(os.fspath(path)))
    except OSError as error:
        raise CatalogPathError("catalog database could not be resolved canonically: '{}'".format(path)) from error
    if _path_key(canonical) != _path_key(path):
        raise CatalogPathError("catalog database must not resolve through an alias: '{}'".format(path))
    return path


def _sqlite_read_only_uri(path: Path) -> str:
    # immutable=1 prevents SQLite from creating or touching WAL/SHM sidecars.
    # A catalog with uncheckpointed WAL state therefore fails closed instead of
    # turning a nominally read-only command into a filesystem mutation.
    return "{}?mode=ro&immutable=1".format(path.as_uri())


def _require_catalog_application_id(descriptor: int) -> int:
    """Authenticate the SQLite header before asking SQLite to parse its schema."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    header = os.read(descriptor, _SQLITE_HEADER_SIZE)
    if len(header) < _SQLITE_HEADER_SIZE or not header.startswith(_SQLITE_HEADER_MAGIC):
        raise CatalogCorruptError("Catalog does not have a complete SQLite database header")
    start = _SQLITE_APPLICATION_ID_OFFSET
    application_id = int.from_bytes(header[start : start + 4], "big")
    if application_id != CATALOG_APPLICATION_ID:
        raise CatalogSchemaError("Catalog SQLite application_id is missing or belongs to another application")
    return application_id


def _harden_catalog_connection(connection: sqlite3.Connection, *, query_only: bool) -> None:
    """Apply connection-local limits before reading any owned schema metadata."""

    setlimit = getattr(connection, "setlimit", None)
    if setlimit is not None:
        for constant_name, value in _SQLITE_CONNECTION_LIMITS:
            category = getattr(sqlite3, constant_name, None)
            if category is not None:
                setlimit(category, value)
    connection.execute("PRAGMA trusted_schema = OFF")
    trusted_schema = connection.execute("PRAGMA trusted_schema").fetchone()
    if trusted_schema is None or int(trusted_schema[0]) != 0:
        raise CatalogSchemaError("SQLite trusted_schema could not be disabled")
    if query_only:
        connection.execute("PRAGMA query_only = ON")
        query_only_value = connection.execute("PRAGMA query_only").fetchone()
        if query_only_value is None or int(query_only_value[0]) != 1:
            raise CatalogSchemaError("SQLite query_only could not be enabled")


def _schema_objects(connection: sqlite3.Connection) -> Tuple[set, set, set]:
    rows = connection.execute(
        """
        SELECT
            CASE
                WHEN typeof(type) = 'text' AND length(CAST(type AS BLOB)) <= ?
                THEN type
                ELSE NULL
            END,
            CASE
                WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= ?
                THEN name
                ELSE NULL
            END
        FROM sqlite_master
        WHERE typeof(name) != 'text' OR substr(name, 1, 7) != 'sqlite_'
        ORDER BY rowid
        LIMIT ?
        """,
        (
            _MAX_CATALOG_SCHEMA_NAME_BYTES,
            _MAX_CATALOG_SCHEMA_NAME_BYTES,
            _MAX_CATALOG_SCHEMA_OBJECTS + 1,
        ),
    ).fetchall()
    if len(rows) > _MAX_CATALOG_SCHEMA_OBJECTS:
        raise CatalogSchemaError("Catalog contains more than {} schema objects".format(_MAX_CATALOG_SCHEMA_OBJECTS))
    tables = set()
    indexes = set()
    unsupported = set()
    for object_type, name in rows:
        if object_type is None or name is None:
            raise CatalogSchemaError("Catalog schema contains an invalid or oversized object name")
        if object_type == "table":
            tables.add(str(name))
        elif object_type == "index":
            indexes.add(str(name))
        else:
            unsupported.add((str(object_type), str(name)))
    return tables, indexes, unsupported


def _inspect_open_catalog(
    connection: sqlite3.Connection,
) -> CatalogFileInspection:
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    except sqlite3.DatabaseError as error:
        raise CatalogCorruptError("Catalog ownership metadata could not be read: {}".format(error)) from error
    if application_id != CATALOG_APPLICATION_ID:
        raise CatalogSchemaError("Catalog SQLite application_id is missing or invalid")
    try:
        tables, indexes, unsupported = _schema_objects(connection)
    except sqlite3.DatabaseError as error:
        raise CatalogCorruptError("Catalog schema metadata could not be read: {}".format(error)) from error
    if unsupported:
        raise CatalogSchemaError(
            "Catalog contains unsupported schema objects: {}".format(
                ", ".join("{}:{}".format(object_type, name) for object_type, name in sorted(unsupported))
            )
        )
    if "catalog_meta" not in tables:
        raise CatalogSchemaError("Refusing to modify an unowned SQLite database without catalog_meta")
    try:
        catalog_meta_columns = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT
                    CASE
                        WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= ?
                        THEN name
                        ELSE NULL
                    END
                FROM pragma_table_info('catalog_meta')
                ORDER BY cid
                LIMIT ?
                """,
                (
                    _MAX_CATALOG_SCHEMA_NAME_BYTES,
                    len(_V1_TABLE_COLUMNS["catalog_meta"]) + 1,
                ),
            )
        )
        meta_rows = list(
            connection.execute(
                """
                SELECT
                    CASE
                        WHEN typeof(key) = 'text' AND length(CAST(key AS BLOB)) <= ?
                        THEN key
                        ELSE NULL
                    END,
                    CASE
                        WHEN typeof(value) = 'text' AND length(CAST(value AS BLOB)) <= ?
                        THEN value
                        ELSE NULL
                    END
                FROM catalog_meta
                ORDER BY key
                LIMIT ?
                """,
                (
                    _MAX_CATALOG_META_KEY_BYTES,
                    _MAX_CATALOG_META_VALUE_BYTES,
                    _MAX_CATALOG_META_ROWS + 1,
                ),
            )
        )
    except sqlite3.DatabaseError as error:
        raise CatalogSchemaError("Catalog ownership metadata is unreadable: {}".format(error)) from error
    if catalog_meta_columns != _V1_TABLE_COLUMNS["catalog_meta"]:
        raise CatalogSchemaError("catalog_meta has an unsupported shape")
    if len(meta_rows) > _MAX_CATALOG_META_ROWS:
        raise CatalogSchemaError("catalog_meta contains too many ownership rows")
    metadata = {}
    for key, value in meta_rows:
        if not isinstance(key, str) or not isinstance(value, str) or key in metadata:
            raise CatalogSchemaError("catalog_meta contains invalid or duplicate keys")
        metadata[key] = value
    if "schema_version" not in metadata:
        raise CatalogSchemaError("catalog_meta does not contain schema_version")
    try:
        version = int(metadata["schema_version"])
    except (TypeError, ValueError) as error:
        raise CatalogSchemaError("Invalid schema_version {!r}".format(metadata["schema_version"])) from error
    if version < 1:
        raise CatalogSchemaError("Existing catalog schema version {} is not a complete owned catalog".format(version))
    if version > SCHEMA_VERSION:
        raise CatalogTooNewError(
            "Catalog schema {} is newer than supported schema {}".format(
                version,
                SCHEMA_VERSION,
            )
        )
    if version < SCHEMA_VERSION:
        raise CatalogSchemaError(
            "Catalog schema {} is older than the supported schema {}; "
            "automatic migration is not supported".format(version, SCHEMA_VERSION)
        )
    expected_columns = _SCHEMA_TABLE_COLUMNS[version]
    if tables != set(expected_columns):
        raise CatalogSchemaError("Catalog schema {} has unexpected or missing tables".format(version))
    if indexes != set(_SCHEMA_INDEXES[version]):
        raise CatalogSchemaError("Catalog schema {} has unexpected or missing indexes".format(version))
    for table_name, expected in expected_columns.items():
        try:
            actual_rows = list(
                connection.execute(
                    """
                    SELECT
                        CASE
                            WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= ?
                            THEN name
                            ELSE NULL
                        END
                    FROM pragma_table_info(?)
                    ORDER BY cid
                    LIMIT ?
                    """,
                    (
                        _MAX_CATALOG_SCHEMA_NAME_BYTES,
                        table_name,
                        len(expected) + 1,
                    ),
                )
            )
        except sqlite3.DatabaseError as error:
            raise CatalogSchemaError("Catalog table '{}' could not be inspected".format(table_name)) from error
        actual = tuple(str(row[0]) for row in actual_rows)
        if actual != expected:
            raise CatalogSchemaError("Catalog table '{}' has an unsupported shape".format(table_name))

    if metadata != {
        "schema_version": str(SCHEMA_VERSION),
        "owner": CATALOG_OWNER,
    }:
        raise CatalogSchemaError("Catalog owner metadata is missing or invalid")
    return CatalogFileInspection(version, False, application_id)


def inspect_catalog_file(
    database_path: Union[str, os.PathLike],
) -> CatalogFileInspection:
    """Identify an existing catalog through a read-only, non-migrating open."""

    path = preflight_catalog_path(database_path, must_exist=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CatalogPathError(
            "catalog database could not be opened without following links: '{}'".format(path)
        ) from error
    try:
        guarded_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(guarded_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or _is_reparse_point(path_stat)
            or not _same_file_stat(guarded_stat, path_stat)
            or int(getattr(path_stat, "st_nlink", 0)) != 1
        ):
            raise CatalogPathError("catalog database changed while it was guarded: '{}'".format(path))
        _require_catalog_application_id(descriptor)
        try:
            connection = sqlite3.connect(
                _sqlite_read_only_uri(path),
                uri=True,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as error:
            raise CatalogCorruptError("Catalog could not be opened read-only: {}".format(error)) from error
        try:
            _harden_catalog_connection(connection, query_only=True)
            opened_path_stat = os.lstat(path)
            if (
                not _same_file_stat(guarded_stat, opened_path_stat)
                or int(getattr(opened_path_stat, "st_nlink", 0)) != 1
            ):
                raise CatalogPathError("catalog database changed while SQLite opened it: '{}'".format(path))
            return _inspect_open_catalog(connection)
        finally:
            connection.close()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ObservationResult:
    """Result of atomically observing one path during a scan."""

    physical_file_id: int
    path_id: int
    content_version_id: int
    new_physical_file: bool
    new_path: bool
    new_content: bool
    identity_reused: bool


_MIGRATION_1 = (
    """
    CREATE TABLE volumes (
        id INTEGER PRIMARY KEY,
        volume_key TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL,
        fs_type TEXT,
        identity_capability TEXT NOT NULL
            CHECK (identity_capability IN ('stable', 'session_only', 'path_only')),
        timestamp_granularity_ns INTEGER,
        created_at REAL NOT NULL,
        last_probe_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE scans (
        id INTEGER PRIMARY KEY,
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'cancelled', 'complete', 'completed_with_errors', 'failed')
        ),
        phase TEXT NOT NULL,
        app_version TEXT,
        error_count INTEGER NOT NULL DEFAULT 0,
        resume_of_scan_id INTEGER REFERENCES scans(id)
    )
    """,
    """
    CREATE TABLE roots (
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE RESTRICT,
        display_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        policy TEXT NOT NULL DEFAULT 'normal',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_complete_scan_id INTEGER REFERENCES scans(id),
        UNIQUE (volume_id, path_key)
    )
    """,
    """
    CREATE TABLE physical_files (
        id INTEGER PRIMARY KEY,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE RESTRICT,
        native_file_id BLOB,
        identity_confidence TEXT NOT NULL CHECK (
            identity_confidence IN ('stable', 'session_only', 'path_only')
        ),
        current_content_version_id INTEGER,
        first_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
        last_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
        first_seen_at REAL NOT NULL,
        last_seen_at REAL NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX physical_files_native_identity
        ON physical_files(volume_id, native_file_id)
        WHERE native_file_id IS NOT NULL
    """,
    """
    CREATE TABLE paths (
        id INTEGER PRIMARY KEY,
        root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        physical_file_id INTEGER NOT NULL REFERENCES physical_files(id) ON DELETE RESTRICT,
        display_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        parent_path_key TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'missing', 'unreadable')),
        first_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
        last_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
        first_seen_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        UNIQUE (root_id, path_key)
    )
    """,
    """
    CREATE TABLE content_versions (
        id INTEGER PRIMARY KEY,
        physical_file_id INTEGER NOT NULL REFERENCES physical_files(id) ON DELETE CASCADE,
        size INTEGER NOT NULL CHECK (size >= 0),
        mtime_ns INTEGER NOT NULL,
        change_token BLOB,
        state TEXT NOT NULL CHECK (state IN ('stable', 'unstable', 'unreadable', 'missing')),
        observed_before REAL NOT NULL,
        observed_after REAL NOT NULL,
        first_seen_scan_id INTEGER NOT NULL REFERENCES scans(id),
        last_seen_scan_id INTEGER NOT NULL REFERENCES scans(id)
    )
    """,
    """
    CREATE TABLE artifacts (
        id INTEGER PRIMARY KEY,
        content_version_id INTEGER NOT NULL REFERENCES content_versions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        algorithm TEXT NOT NULL,
        algorithm_version TEXT NOT NULL,
        parameters_hash TEXT NOT NULL DEFAULT '',
        value BLOB NOT NULL,
        verification_level TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (content_version_id, kind, algorithm, algorithm_version, parameters_hash)
    )
    """,
    """
    CREATE TABLE scan_dirs (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        display_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'in_progress', 'complete', 'unreachable', 'failed')
        ),
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_until REAL,
        error_count INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (scan_id, root_id, path_key)
    )
    """,
    """
    CREATE TABLE scan_errors (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        scan_dir_id INTEGER REFERENCES scan_dirs(id) ON DELETE SET NULL,
        path TEXT NOT NULL,
        operation TEXT NOT NULL,
        error_code TEXT,
        message TEXT NOT NULL,
        transient INTEGER NOT NULL CHECK (transient IN (0, 1)),
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE work_items (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        content_version_id INTEGER NOT NULL REFERENCES content_versions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'in_progress', 'complete', 'failed')),
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_until REAL,
        payload_json TEXT,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (scan_id, content_version_id, kind)
    )
    """,
    """
    CREATE TABLE verification_records (
        id INTEGER PRIMARY KEY,
        first_content_version_id INTEGER NOT NULL REFERENCES content_versions(id) ON DELETE RESTRICT,
        second_content_version_id INTEGER NOT NULL REFERENCES content_versions(id) ON DELETE RESTRICT,
        algorithm TEXT NOT NULL,
        algorithm_version TEXT NOT NULL,
        full_digest BLOB NOT NULL,
        byte_compare_at REAL,
        state TEXT NOT NULL CHECK (state IN ('candidate', 'verified', 'invalidated')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (first_content_version_id <= second_content_version_id),
        UNIQUE (
            first_content_version_id,
            second_content_version_id,
            algorithm,
            algorithm_version,
            full_digest
        )
    )
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE action_journal (
        id INTEGER PRIMARY KEY,
        action_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('planned', 'in_progress', 'completed', 'failed', 'cancelled')
        ),
        scan_id INTEGER REFERENCES scans(id) ON DELETE SET NULL,
        verification_id INTEGER REFERENCES verification_records(id) ON DELETE SET NULL,
        physical_file_id INTEGER REFERENCES physical_files(id) ON DELETE SET NULL,
        path_id INTEGER REFERENCES paths(id) ON DELETE SET NULL,
        payload_json TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        completed_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS paths_physical_file ON paths(physical_file_id, state)",
    "CREATE INDEX IF NOT EXISTS paths_last_seen ON paths(root_id, parent_path_key, last_seen_scan_id)",
    "CREATE INDEX IF NOT EXISTS content_versions_physical ON content_versions(physical_file_id, id)",
    "CREATE INDEX IF NOT EXISTS artifacts_lookup ON artifacts(kind, algorithm, algorithm_version, value)",
    "CREATE INDEX IF NOT EXISTS scan_dirs_claim ON scan_dirs(scan_id, status, lease_until, id)",
    "CREATE INDEX IF NOT EXISTS scan_errors_scan ON scan_errors(scan_id, id)",
    "CREATE INDEX IF NOT EXISTS work_items_claim ON work_items(scan_id, status, priority DESC, lease_until, id)",
    "CREATE INDEX IF NOT EXISTS verification_digest ON verification_records(algorithm, algorithm_version, full_digest)",
    "CREATE INDEX IF NOT EXISTS action_journal_status ON action_journal(status, id)",
)

_MIGRATION_3 = (
    """
    CREATE TABLE scan_snapshots (
        scan_id INTEGER PRIMARY KEY REFERENCES scans(id) ON DELETE CASCADE,
        completed_at REAL NOT NULL,
        root_count INTEGER NOT NULL CHECK (root_count >= 1),
        path_count INTEGER NOT NULL CHECK (path_count >= 0)
    )
    """,
    """
    CREATE TABLE scan_path_observations (
        id INTEGER PRIMARY KEY,
        scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE RESTRICT,
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE RESTRICT,
        path_id INTEGER NOT NULL REFERENCES paths(id) ON DELETE RESTRICT,
        physical_file_id INTEGER NOT NULL REFERENCES physical_files(id) ON DELETE RESTRICT,
        content_version_id INTEGER NOT NULL REFERENCES content_versions(id) ON DELETE RESTRICT,
        display_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        parent_path_key TEXT NOT NULL,
        path_state TEXT NOT NULL CHECK (path_state IN ('active', 'missing', 'unreadable')),
        content_state TEXT NOT NULL CHECK (
            content_state IN ('stable', 'unstable', 'unreadable', 'missing')
        ),
        native_file_id BLOB,
        identity_confidence TEXT NOT NULL CHECK (
            identity_confidence IN ('stable', 'session_only', 'path_only')
        ),
        observed_at REAL NOT NULL,
        UNIQUE (scan_id, root_id, path_key)
    )
    """,
    """
    CREATE INDEX scan_path_observations_identity
        ON scan_path_observations(
            scan_id, volume_id, identity_confidence, native_file_id, id
        )
    """,
    """
    CREATE INDEX scan_path_observations_content
        ON scan_path_observations(scan_id, content_version_id, id)
    """,
)

_MIGRATION_4 = ()

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
}


class Catalog:
    """Local SQLite catalog with resumable scan and artifact primitives."""

    def __init__(
        self,
        database_path: Union[str, os.PathLike],
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        *,
        read_only: bool = False,
    ):
        self.database_path = os.fspath(database_path)
        self._read_only = bool(read_only)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._savepoint_counter = 0
        self._connection = None
        guard_descriptor = None
        created = False
        try:
            if self.database_path == ":memory:":
                if self._read_only:
                    raise CatalogPathError("an in-memory catalog cannot be opened read-only")
            else:
                path = preflight_catalog_path(
                    self.database_path,
                    must_exist=self._read_only,
                )
                self.database_path = str(path)
                if os.path.lexists(path):
                    inspect_catalog_file(path)
                    guard_flags = (
                        os.O_RDONLY
                        | getattr(os, "O_BINARY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    guard_descriptor = os.open(path, guard_flags)
                    guarded_stat = os.fstat(guard_descriptor)
                    current_stat = os.lstat(path)
                    if (
                        not stat.S_ISREG(guarded_stat.st_mode)
                        or not stat.S_ISREG(current_stat.st_mode)
                        or _is_reparse_point(current_stat)
                        or not _same_file_stat(guarded_stat, current_stat)
                        or int(getattr(current_stat, "st_nlink", 0)) != 1
                    ):
                        raise CatalogPathError("catalog database changed before SQLite opened it: '{}'".format(path))
                    _require_catalog_application_id(guard_descriptor)
                else:
                    if self._read_only:
                        raise CatalogPathError("catalog database does not exist: '{}'".format(path))
                    reserve_flags = (
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_BINARY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        guard_descriptor = os.open(path, reserve_flags, 0o600)
                    except FileExistsError:
                        # A concurrent creator is never adopted as a new catalog.
                        preflight_catalog_path(path, must_exist=True)
                        raise CatalogPathError(
                            "catalog database appeared during exclusive reservation: " "'{}'".format(path)
                        )
                    created = True

            connection_target = self.database_path
            connection_options = {}
            if self._read_only and self.database_path != ":memory:":
                connection_target = _sqlite_read_only_uri(Path(self.database_path))
                connection_options["uri"] = True
            self._connection = sqlite3.connect(
                connection_target,
                timeout=max(0, busy_timeout_ms) / 1000,
                isolation_level=None,
                check_same_thread=False,
                **connection_options,
            )
            self._connection.row_factory = sqlite3.Row
            _harden_catalog_connection(self._connection, query_only=self._read_only)
            self._connection.execute("PRAGMA busy_timeout = {}".format(max(0, int(busy_timeout_ms))))
            self._connection.execute("PRAGMA foreign_keys = ON")
            if guard_descriptor is not None:
                guarded_stat = os.fstat(guard_descriptor)
                current_stat = os.lstat(self.database_path)
                if (
                    not stat.S_ISREG(guarded_stat.st_mode)
                    or not stat.S_ISREG(current_stat.st_mode)
                    or _is_reparse_point(current_stat)
                    or not _same_file_stat(guarded_stat, current_stat)
                    or int(getattr(current_stat, "st_nlink", 0)) != 1
                ):
                    raise CatalogPathError(
                        "catalog database changed while SQLite opened it: '{}'".format(self.database_path)
                    )
            self.verify_integrity()
            if self._read_only:
                _inspect_open_catalog(self._connection)
            else:
                self._prepare_schema(new_database=created or self.database_path == ":memory:")
            self.verify_integrity()
            if not self._read_only:
                self._configure_local_wal()
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise
        finally:
            if guard_descriptor is not None:
                os.close(guard_descriptor)

    @classmethod
    def open_read_only(
        cls,
        database_path: Union[str, os.PathLike],
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> "Catalog":
        """Open one owned catalog without migration, WAL changes, or writes."""

        return cls(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            read_only=True,
        )

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._connection is None:
            raise CatalogStateError("Catalog is closed")

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _configure_local_wal(self) -> None:
        self._ensure_open()
        if self._read_only:
            raise CatalogStateError("read-only catalogs cannot change journal mode")
        journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower()
        if self.database_path != ":memory:" and journal_mode != "wal":
            raise CatalogStateError(
                "The catalog requires WAL mode on a local filesystem; SQLite returned {!r}".format(journal_mode)
            )
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA wal_autocheckpoint = 1000")

    def _prepare_schema(self, *, new_database: bool) -> None:
        self._ensure_open()
        if self._read_only:
            raise CatalogStateError("read-only catalogs cannot prepare schemas")
        if new_database:
            user_tables, user_indexes, unsupported = _schema_objects(self._connection)
            if user_tables or user_indexes or unsupported:
                raise CatalogSchemaError("new catalog reservation contains unexpected SQLite objects")
            with self.transaction():
                self._connection.execute("PRAGMA application_id = {}".format(CATALOG_APPLICATION_ID))
                self._connection.execute("CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._connection.execute("INSERT INTO catalog_meta(key, value) VALUES ('schema_version', '0')")
                self._connection.execute(
                    "INSERT INTO catalog_meta(key, value) VALUES ('owner', ?)",
                    (CATALOG_OWNER,),
                )
            version = 0
        else:
            inspection = _inspect_open_catalog(self._connection)
            version = inspection.schema_version

        while version < SCHEMA_VERSION:
            target_version = version + 1
            statements = _MIGRATIONS.get(target_version)
            if statements is None:
                raise CatalogSchemaError("No migration is available for schema {}".format(target_version))
            with self.transaction():
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "UPDATE catalog_meta SET value = ? WHERE key = 'schema_version'",
                    (str(target_version),),
                )
            version = target_version
        _inspect_open_catalog(self._connection)

    @property
    def schema_version(self) -> int:
        self._ensure_open()
        row = self._connection.execute("SELECT value FROM catalog_meta WHERE key = 'schema_version'").fetchone()
        return int(row[0])

    def verify_integrity(self) -> bool:
        """Run SQLite ``quick_check`` without deleting or replacing a damaged database."""

        self._ensure_open()
        with self._lock:
            try:
                rows = self._connection.execute("PRAGMA quick_check").fetchall()
            except sqlite3.DatabaseError as error:
                raise CatalogCorruptError(
                    "Catalog integrity check could not read {!r}: {}".format(self.database_path, error)
                )
        messages = [row[0] for row in rows]
        if messages != ["ok"]:
            raise CatalogIntegrityError(
                "Catalog integrity check failed for {!r}: {}".format(self.database_path, "; ".join(messages))
            )
        return True

    def backup_to(self, destination: Union[str, os.PathLike]) -> str:
        """Create and integrity-check an online backup without overwriting a file.

        The destination is reserved with ``O_EXCL`` before SQLite opens it, so a
        concurrent creator cannot be overwritten. A backup created by this call
        is removed again if copying or ``quick_check`` fails.
        """

        self._ensure_open()
        destination = str(preflight_catalog_path(destination, must_exist=False))
        source = os.path.abspath(self.database_path)
        if os.path.normcase(destination) == os.path.normcase(source):
            raise ValueError("backup destination must differ from the catalog")
        if os.path.lexists(destination):
            raise FileExistsError("catalog backup destination already exists: '{}'".format(destination))
        if self._transaction_depth:
            raise CatalogStateError("cannot back up during an active catalog transaction")

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(destination, flags, 0o600)
        os.close(descriptor)
        backup_connection = None
        try:
            backup_connection = sqlite3.connect(destination, isolation_level=None)
            with self._lock:
                self._connection.backup(backup_connection)
            messages = [row[0] for row in backup_connection.execute("PRAGMA quick_check").fetchall()]
            if messages != ["ok"]:
                raise CatalogIntegrityError(
                    "Catalog backup integrity check failed for {!r}: {}".format(
                        destination,
                        "; ".join(messages),
                    )
                )
        except BaseException:
            if backup_connection is not None:
                backup_connection.close()
            try:
                os.remove(destination)
            except OSError:
                pass
            raise
        backup_connection.close()
        return destination

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[None]:
        """Create an atomic transaction, using savepoints when composed by another caller."""

        self._ensure_open()
        if self._read_only:
            raise CatalogStateError("catalog is open read-only")
        with self._lock:
            depth = self._transaction_depth
            savepoint = None
            if depth == 0:
                self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                self._savepoint_counter += 1
                savepoint = "catalog_sp_{}".format(self._savepoint_counter)
                self._connection.execute("SAVEPOINT {}".format(savepoint))
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._transaction_depth -= 1
                if depth == 0:
                    self._connection.rollback()
                else:
                    self._connection.execute("ROLLBACK TO SAVEPOINT {}".format(savepoint))
                    self._connection.execute("RELEASE SAVEPOINT {}".format(savepoint))
                raise
            else:
                self._transaction_depth -= 1
                try:
                    if depth == 0:
                        self._connection.commit()
                    else:
                        self._connection.execute("RELEASE SAVEPOINT {}".format(savepoint))
                except BaseException:
                    if depth == 0:
                        self._connection.rollback()
                    raise

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                if self._transaction_depth:
                    self._connection.rollback()
                    self._transaction_depth = 0
                self._connection.close()
                self._connection = None

    @staticmethod
    def _validate_value(name: str, value: str, allowed_values: Iterable[str]) -> None:
        if value not in allowed_values:
            raise ValueError("Invalid {} {!r}".format(name, value))

    @staticmethod
    def _validate_page_size(limit: int) -> None:
        if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
            raise ValueError("limit must be an integer between 1 and {}".format(MAX_PAGE_SIZE))

    @staticmethod
    def _encode_blob(value: Any) -> Optional[bytes]:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, int):
            return str(value).encode("ascii")
        raise TypeError("Identity and change tokens must be bytes, text, integers, or None")

    @staticmethod
    def _json_payload(payload: Any) -> Optional[str]:
        if payload is None:
            return None
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def normalize_path(path: Union[str, os.PathLike], platform: Optional[str] = None) -> str:
        """Return a comparison key while preserving the original path separately."""

        value = unicodedata.normalize("NFC", os.fspath(path))
        platform = platform or sys.platform
        if platform.startswith("win"):
            return ntpath.normpath(value).casefold()
        return posixpath.normpath(value)

    @staticmethod
    def _parent_path(path: str, platform: str) -> str:
        if platform.startswith("win"):
            return ntpath.dirname(path)
        return posixpath.dirname(path)

    def _require_scan_running(self, scan_id: int) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            raise CatalogStateError("Unknown scan {}".format(scan_id))
        if row["status"] != "running":
            raise CatalogStateError("Scan {} is not running".format(scan_id))
        return row

    def upsert_volume(
        self,
        volume_key: str,
        platform: str,
        fs_type: Optional[str] = None,
        identity_capability: str = "path_only",
        timestamp_granularity_ns: Optional[int] = None,
        now: Optional[float] = None,
    ) -> int:
        self._validate_value("identity_capability", identity_capability, IDENTITY_CONFIDENCE_VALUES)
        if not volume_key:
            raise ValueError("volume_key must not be empty")
        if not platform:
            raise ValueError("platform must not be empty")
        now = time.time() if now is None else now
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO volumes(
                    volume_key, platform, fs_type, identity_capability,
                    timestamp_granularity_ns, created_at, last_probe_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(volume_key) DO UPDATE SET
                    platform = excluded.platform,
                    fs_type = excluded.fs_type,
                    identity_capability = excluded.identity_capability,
                    timestamp_granularity_ns = excluded.timestamp_granularity_ns,
                    last_probe_at = excluded.last_probe_at
                """,
                (
                    volume_key,
                    platform,
                    fs_type,
                    identity_capability,
                    timestamp_granularity_ns,
                    now,
                    now,
                ),
            )
            row = self._connection.execute("SELECT id FROM volumes WHERE volume_key = ?", (volume_key,)).fetchone()
            return row["id"]

    def upsert_root(
        self,
        volume_id: int,
        display_path: Union[str, os.PathLike],
        path_key: Optional[str] = None,
        policy: str = "normal",
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        display_path = os.fspath(display_path)
        with self.transaction():
            volume = self._connection.execute("SELECT platform FROM volumes WHERE id = ?", (volume_id,)).fetchone()
            if volume is None:
                raise CatalogStateError("Unknown volume {}".format(volume_id))
            path_key = path_key or self.normalize_path(display_path, volume["platform"])
            self._connection.execute(
                """
                INSERT INTO roots(volume_id, display_path, path_key, policy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(volume_id, path_key) DO UPDATE SET
                    display_path = excluded.display_path,
                    policy = excluded.policy,
                    updated_at = excluded.updated_at
                """,
                (volume_id, display_path, path_key, policy, now, now),
            )
            row = self._connection.execute(
                "SELECT id FROM roots WHERE volume_id = ? AND path_key = ?",
                (volume_id, path_key),
            ).fetchone()
            return row["id"]

    def start_scan(
        self,
        root_ids: Sequence[int],
        app_version: Optional[str] = None,
        resume_of_scan_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> int:
        root_ids = list(dict.fromkeys(root_ids))
        if not root_ids:
            raise ValueError("At least one root is required")
        now = time.time() if now is None else now
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO scans(started_at, status, phase, app_version, resume_of_scan_id)
                VALUES (?, 'running', 'enumerating', ?, ?)
                """,
                (now, app_version, resume_of_scan_id),
            )
            scan_id = cursor.lastrowid
            for root_id in root_ids:
                root = self._connection.execute(
                    "SELECT display_path, path_key FROM roots WHERE id = ?", (root_id,)
                ).fetchone()
                if root is None:
                    raise CatalogStateError("Unknown root {}".format(root_id))
                self._connection.execute(
                    """
                    INSERT INTO scan_dirs(
                        scan_id, root_id, display_path, path_key, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (scan_id, root_id, root["display_path"], root["path_key"], now, now),
                )
            return scan_id

    def get_scan(self, scan_id: int) -> sqlite3.Row:
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            raise CatalogStateError("Unknown scan {}".format(scan_id))
        return row

    def scan_roots(self, scan_id: int) -> List[sqlite3.Row]:
        """Return the roots explicitly selected when ``scan_id`` was created."""

        self.get_scan(scan_id)
        return list(
            self._connection.execute(
                """
                SELECT DISTINCT
                    roots.id AS root_id,
                    roots.display_path,
                    roots.path_key,
                    roots.policy,
                    roots.volume_id,
                    roots.last_complete_scan_id
                FROM scan_dirs
                JOIN roots ON roots.id = scan_dirs.root_id
                WHERE scan_dirs.scan_id = ?
                ORDER BY roots.id
                """,
                (scan_id,),
            )
        )

    def latest_complete_scan_id(self) -> Optional[int]:
        """Return the most recently finished fully complete scan."""

        self._ensure_open()
        row = self._connection.execute("""
            SELECT id
            FROM scans
            WHERE status = 'complete'
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """).fetchone()
        return None if row is None else row["id"]

    def require_roots_projectable(self, root_ids: Sequence[int]) -> Dict[int, int]:
        """Require each selected root's latest scan to be its latest complete scan.

        This rejects projection while a newer running, cancelled, failed, or
        partially completed scan exists for any selected root.
        """

        root_ids = tuple(dict.fromkeys(root_ids))
        if not root_ids:
            raise ValueError("at least one root is required for projection")
        projection_scans = {}
        for root_id in root_ids:
            row = self._connection.execute(
                """
                SELECT
                    roots.last_complete_scan_id,
                    latest.scan_id AS latest_scan_id,
                    latest.status AS latest_scan_status
                FROM roots
                LEFT JOIN (
                    SELECT scan_dirs.root_id, scans.id AS scan_id, scans.status
                    FROM scan_dirs
                    JOIN scans ON scans.id = scan_dirs.scan_id
                    WHERE scan_dirs.root_id = ?
                    ORDER BY scans.id DESC
                    LIMIT 1
                ) AS latest ON latest.root_id = roots.id
                WHERE roots.id = ?
                """,
                (root_id, root_id),
            ).fetchone()
            if row is None:
                raise CatalogStateError("Unknown root {}".format(root_id))
            if (
                row["last_complete_scan_id"] is None
                or row["latest_scan_status"] != "complete"
                or row["latest_scan_id"] != row["last_complete_scan_id"]
            ):
                raise CatalogStateError(
                    "Root {} has no current fully complete scan for verified projection".format(root_id)
                )
            projection_scans[root_id] = row["last_complete_scan_id"]
        return projection_scans

    def enqueue_directory(
        self,
        scan_id: int,
        root_id: int,
        display_path: Union[str, os.PathLike],
        path_key: Optional[str] = None,
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        display_path = os.fspath(display_path)
        with self.transaction():
            self._require_scan_running(scan_id)
            root = self._connection.execute(
                """
                SELECT roots.id, volumes.platform
                FROM roots JOIN volumes ON volumes.id = roots.volume_id
                WHERE roots.id = ?
                """,
                (root_id,),
            ).fetchone()
            if root is None:
                raise CatalogStateError("Unknown root {}".format(root_id))
            root_in_scan = self._connection.execute(
                "SELECT 1 FROM scan_dirs WHERE scan_id = ? AND root_id = ? LIMIT 1",
                (scan_id, root_id),
            ).fetchone()
            if root_in_scan is None:
                raise CatalogStateError("Root {} does not belong to scan {}".format(root_id, scan_id))
            path_key = path_key or self.normalize_path(display_path, root["platform"])
            self._connection.execute(
                """
                INSERT OR IGNORE INTO scan_dirs(
                    scan_id, root_id, display_path, path_key, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (scan_id, root_id, display_path, path_key, now, now),
            )
            row = self._connection.execute(
                "SELECT id FROM scan_dirs WHERE scan_id = ? AND root_id = ? AND path_key = ?",
                (scan_id, root_id, path_key),
            ).fetchone()
            return row["id"]

    def claim_scan_directories(
        self,
        scan_id: int,
        owner: str,
        limit: int = 1,
        lease_seconds: float = 60,
        now: Optional[float] = None,
        root_id: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        self._validate_page_size(limit)
        if not owner:
            raise ValueError("owner must not be empty")
        if lease_seconds < 0:
            raise ValueError("lease_seconds must not be negative")
        now = time.time() if now is None else now
        with self.transaction():
            self._require_scan_running(scan_id)
            root_filter = ""
            update_parameters: List[Any] = [now, scan_id]
            select_parameters: List[Any] = [scan_id]
            if root_id is not None:
                root_filter = " AND root_id = ?"
                update_parameters.append(root_id)
                select_parameters.append(root_id)
            update_parameters.append(now)
            self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE scan_id = ?{} AND status = 'in_progress' AND lease_until <= ?
                """.format(root_filter),
                update_parameters,
            )
            select_parameters.append(limit)
            ids = [
                row["id"]
                for row in self._connection.execute(
                    """
                    SELECT id FROM scan_dirs
                    WHERE scan_id = ?{} AND status = 'pending'
                    ORDER BY id
                    LIMIT ?
                    """.format(root_filter),
                    select_parameters,
                )
            ]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = 'in_progress',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_until = ?,
                    updated_at = ?
                WHERE id IN ({})
                """.format(placeholders),
                (owner, now + lease_seconds, now, *ids),
            )
            return list(
                self._connection.execute(
                    "SELECT * FROM scan_dirs WHERE id IN ({}) ORDER BY id".format(placeholders),
                    ids,
                )
            )

    def resume_expired_scan_directory_leases(
        self,
        scan_id: int,
        root_id: int,
        now: Optional[float] = None,
    ) -> int:
        """Return only one root's expired directory leases to ``pending``."""

        now = time.time() if now is None else now
        with self.transaction():
            self._require_scan_running(scan_id)
            cursor = self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE scan_id = ? AND root_id = ?
                    AND status = 'in_progress' AND lease_until <= ?
                """,
                (now, scan_id, root_id, now),
            )
            return cursor.rowcount

    def complete_scan_directory(
        self, scan_dir_id: int, owner: Optional[str] = None, now: Optional[float] = None
    ) -> None:
        now = time.time() if now is None else now
        with self.transaction():
            row = self._connection.execute(
                "SELECT status, lease_owner FROM scan_dirs WHERE id = ?", (scan_dir_id,)
            ).fetchone()
            if row is None:
                raise CatalogStateError("Unknown scan directory {}".format(scan_dir_id))
            if row["status"] != "in_progress":
                raise CatalogStateError("Scan directory {} is not in progress".format(scan_dir_id))
            if owner is not None and row["lease_owner"] != owner:
                raise CatalogStateError("Scan directory {} is leased by another owner".format(scan_dir_id))
            self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = 'complete', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, scan_dir_id),
            )

    def fail_scan_directory(
        self,
        scan_dir_id: int,
        operation: str,
        message: str,
        error_code: Optional[str] = None,
        transient: bool = True,
        owner: Optional[str] = None,
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        with self.transaction():
            row = self._connection.execute("SELECT * FROM scan_dirs WHERE id = ?", (scan_dir_id,)).fetchone()
            if row is None:
                raise CatalogStateError("Unknown scan directory {}".format(scan_dir_id))
            if row["status"] not in {"pending", "in_progress"}:
                raise CatalogStateError("Scan directory {} cannot fail from {}".format(scan_dir_id, row["status"]))
            if owner is not None and row["lease_owner"] != owner:
                raise CatalogStateError("Scan directory {} is leased by another owner".format(scan_dir_id))
            status = "unreachable" if transient else "failed"
            self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = ?, lease_owner = NULL, lease_until = NULL,
                    error_count = error_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (status, now, scan_dir_id),
            )
            return self._record_scan_error_locked(
                row["scan_id"],
                row["display_path"],
                operation,
                message,
                error_code,
                transient,
                scan_dir_id,
                now,
            )

    def _record_scan_error_locked(
        self,
        scan_id: int,
        path: str,
        operation: str,
        message: str,
        error_code: Optional[str],
        transient: bool,
        scan_dir_id: Optional[int],
        now: float,
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO scan_errors(
                scan_id, scan_dir_id, path, operation, error_code, message, transient, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (scan_id, scan_dir_id, path, operation, error_code, message, int(transient), now),
        )
        self._connection.execute("UPDATE scans SET error_count = error_count + 1 WHERE id = ?", (scan_id,))
        return cursor.lastrowid

    def record_scan_error(
        self,
        scan_id: int,
        path: Union[str, os.PathLike],
        operation: str,
        message: str,
        error_code: Optional[str] = None,
        transient: bool = False,
        scan_dir_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        with self.transaction():
            self._require_scan_running(scan_id)
            if scan_dir_id is not None:
                directory = self._connection.execute(
                    "SELECT scan_id FROM scan_dirs WHERE id = ?", (scan_dir_id,)
                ).fetchone()
                if directory is None or directory["scan_id"] != scan_id:
                    raise CatalogStateError("Scan directory {} does not belong to scan {}".format(scan_dir_id, scan_id))
                self._connection.execute(
                    """
                    UPDATE scan_dirs
                    SET error_count = error_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, scan_dir_id),
                )
            return self._record_scan_error_locked(
                scan_id,
                os.fspath(path),
                operation,
                message,
                error_code,
                transient,
                scan_dir_id,
                now,
            )

    def observe_file(
        self,
        scan_id: int,
        root_id: int,
        display_path: Union[str, os.PathLike],
        size: int,
        mtime_ns: int,
        native_file_id: Any = None,
        identity_confidence: str = "path_only",
        change_token: Any = None,
        content_state: str = "stable",
        path_key: Optional[str] = None,
        parent_path: Optional[Union[str, os.PathLike]] = None,
        now: Optional[float] = None,
    ) -> ObservationResult:
        """Upsert a path and create a new content generation only when its observed state changed."""

        self._validate_value("identity_confidence", identity_confidence, IDENTITY_CONFIDENCE_VALUES)
        self._validate_value("content_state", content_state, CONTENT_STATE_VALUES)
        if size < 0:
            raise ValueError("size must not be negative")
        now = time.time() if now is None else now
        display_path = os.fspath(display_path)
        native_file_id = self._encode_blob(native_file_id)
        change_token = self._encode_blob(change_token)
        if identity_confidence == "path_only":
            native_file_id = None

        with self.transaction():
            self._require_scan_running(scan_id)
            root = self._connection.execute(
                """
                SELECT roots.volume_id, volumes.platform
                FROM roots JOIN volumes ON volumes.id = roots.volume_id
                WHERE roots.id = ?
                """,
                (root_id,),
            ).fetchone()
            if root is None:
                raise CatalogStateError("Unknown root {}".format(root_id))
            path_key = path_key or self.normalize_path(display_path, root["platform"])
            parent_display_path = (
                os.fspath(parent_path) if parent_path is not None else self._parent_path(display_path, root["platform"])
            )
            parent_path_key = self.normalize_path(parent_display_path, root["platform"])
            directory = self._connection.execute(
                """
                SELECT id, status FROM scan_dirs
                WHERE scan_id = ? AND root_id = ? AND path_key = ?
                """,
                (scan_id, root_id, parent_path_key),
            ).fetchone()
            if directory is None:
                raise CatalogStateError(
                    "Parent directory {!r} has not been enqueued for scan {}".format(parent_display_path, scan_id)
                )
            if directory["status"] != "in_progress":
                raise CatalogStateError("Parent directory {!r} is not being enumerated".format(parent_display_path))

            existing_path = self._connection.execute(
                "SELECT * FROM paths WHERE root_id = ? AND path_key = ?",
                (root_id, path_key),
            ).fetchone()
            physical = None
            if native_file_id is not None:
                physical = self._connection.execute(
                    """
                    SELECT * FROM physical_files
                    WHERE volume_id = ? AND native_file_id = ?
                    """,
                    (root["volume_id"], native_file_id),
                ).fetchone()

            new_physical_file = False
            identity_reused = False
            if physical is None and existing_path is not None:
                candidate = self._connection.execute(
                    "SELECT * FROM physical_files WHERE id = ?",
                    (existing_path["physical_file_id"],),
                ).fetchone()
                if native_file_id is None or candidate["native_file_id"] in {None, native_file_id}:
                    physical = candidate
                    if native_file_id is not None and candidate["native_file_id"] is None:
                        self._connection.execute(
                            """
                            UPDATE physical_files
                            SET native_file_id = ?, identity_confidence = ?
                            WHERE id = ?
                            """,
                            (native_file_id, identity_confidence, candidate["id"]),
                        )

            if physical is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO physical_files(
                        volume_id, native_file_id, identity_confidence,
                        first_seen_scan_id, last_seen_scan_id, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        root["volume_id"],
                        native_file_id,
                        identity_confidence,
                        scan_id,
                        scan_id,
                        now,
                        now,
                    ),
                )
                physical_file_id = cursor.lastrowid
                current_content_version_id = None
                new_physical_file = True
            else:
                physical_file_id = physical["id"]
                current_content_version_id = physical["current_content_version_id"]
                identity_reused = native_file_id is not None and (
                    existing_path is None or existing_path["physical_file_id"] != physical_file_id
                )
                self._connection.execute(
                    """
                    UPDATE physical_files
                    SET identity_confidence = ?, last_seen_scan_id = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (identity_confidence, scan_id, now, physical_file_id),
                )

            new_path = existing_path is None
            if new_path:
                cursor = self._connection.execute(
                    """
                    INSERT INTO paths(
                        root_id, physical_file_id, display_path, path_key, parent_path_key, state,
                        first_seen_scan_id, last_seen_scan_id, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        root_id,
                        physical_file_id,
                        display_path,
                        path_key,
                        parent_path_key,
                        scan_id,
                        scan_id,
                        now,
                        now,
                    ),
                )
                path_id = cursor.lastrowid
            else:
                path_id = existing_path["id"]
                self._connection.execute(
                    """
                    UPDATE paths
                    SET physical_file_id = ?, display_path = ?, parent_path_key = ?,
                        state = 'active', last_seen_scan_id = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (physical_file_id, display_path, parent_path_key, scan_id, now, path_id),
                )

            current_version = None
            if current_content_version_id is not None:
                current_version = self._connection.execute(
                    "SELECT * FROM content_versions WHERE id = ?",
                    (current_content_version_id,),
                ).fetchone()
            same_content_generation = (
                current_version is not None
                and current_version["size"] == size
                and current_version["mtime_ns"] == mtime_ns
                and current_version["change_token"] == change_token
                and current_version["state"] == content_state
                and not (identity_reused and change_token is None)
            )
            if same_content_generation:
                content_version_id = current_version["id"]
                new_content = False
                self._connection.execute(
                    """
                    UPDATE content_versions
                    SET observed_after = ?, last_seen_scan_id = ?
                    WHERE id = ?
                    """,
                    (now, scan_id, content_version_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    INSERT INTO content_versions(
                        physical_file_id, size, mtime_ns, change_token, state,
                        observed_before, observed_after, first_seen_scan_id, last_seen_scan_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        physical_file_id,
                        size,
                        mtime_ns,
                        change_token,
                        content_state,
                        now,
                        now,
                        scan_id,
                        scan_id,
                    ),
                )
                content_version_id = cursor.lastrowid
                new_content = True
                if current_content_version_id is not None:
                    self._connection.execute(
                        """
                        UPDATE verification_records
                        SET state = 'invalidated', updated_at = ?
                        WHERE state != 'invalidated'
                            AND (
                                first_content_version_id = ?
                                OR second_content_version_id = ?
                            )
                        """,
                        (now, current_content_version_id, current_content_version_id),
                    )
                self._connection.execute(
                    "UPDATE physical_files SET current_content_version_id = ? WHERE id = ?",
                    (content_version_id, physical_file_id),
                )

            self._connection.execute(
                """
                INSERT INTO scan_path_observations(
                    scan_id, root_id, volume_id, path_id, physical_file_id,
                    content_version_id, display_path, path_key, parent_path_key,
                    path_state, content_state, native_file_id,
                    identity_confidence, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                ON CONFLICT(scan_id, root_id, path_key) DO UPDATE SET
                    volume_id = excluded.volume_id,
                    path_id = excluded.path_id,
                    physical_file_id = excluded.physical_file_id,
                    content_version_id = excluded.content_version_id,
                    display_path = excluded.display_path,
                    parent_path_key = excluded.parent_path_key,
                    path_state = excluded.path_state,
                    content_state = excluded.content_state,
                    native_file_id = excluded.native_file_id,
                    identity_confidence = excluded.identity_confidence,
                    observed_at = excluded.observed_at
                """,
                (
                    scan_id,
                    root_id,
                    root["volume_id"],
                    path_id,
                    physical_file_id,
                    content_version_id,
                    display_path,
                    path_key,
                    parent_path_key,
                    content_state,
                    native_file_id,
                    identity_confidence,
                    now,
                ),
            )

            return ObservationResult(
                physical_file_id=physical_file_id,
                path_id=path_id,
                content_version_id=content_version_id,
                new_physical_file=new_physical_file,
                new_path=new_path,
                new_content=new_content,
                identity_reused=identity_reused,
            )

    def put_artifact(
        self,
        content_version_id: int,
        kind: str,
        algorithm: str,
        algorithm_version: str,
        value: Any,
        parameters_hash: str = "",
        verification_level: str = "candidate",
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        with self.transaction():
            if (
                self._connection.execute(
                    "SELECT 1 FROM content_versions WHERE id = ?", (content_version_id,)
                ).fetchone()
                is None
            ):
                raise CatalogStateError("Unknown content version {}".format(content_version_id))
            self._connection.execute(
                """
                INSERT INTO artifacts(
                    content_version_id, kind, algorithm, algorithm_version, parameters_hash,
                    value, verification_level, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    content_version_id, kind, algorithm, algorithm_version, parameters_hash
                ) DO UPDATE SET
                    value = excluded.value,
                    verification_level = excluded.verification_level,
                    updated_at = excluded.updated_at
                """,
                (
                    content_version_id,
                    kind,
                    algorithm,
                    algorithm_version,
                    parameters_hash,
                    value,
                    verification_level,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id FROM artifacts
                WHERE content_version_id = ? AND kind = ? AND algorithm = ?
                    AND algorithm_version = ? AND parameters_hash = ?
                """,
                (content_version_id, kind, algorithm, algorithm_version, parameters_hash),
            ).fetchone()
            return row["id"]

    def get_artifact(
        self,
        content_version_id: int,
        kind: str,
        algorithm: str,
        algorithm_version: str,
        parameters_hash: str = "",
    ) -> Optional[sqlite3.Row]:
        self._ensure_open()
        return self._connection.execute(
            """
            SELECT * FROM artifacts
            WHERE content_version_id = ? AND kind = ? AND algorithm = ?
                AND algorithm_version = ? AND parameters_hash = ?
            """,
            (content_version_id, kind, algorithm, algorithm_version, parameters_hash),
        ).fetchone()

    def get_work_item(self, work_item_id: int) -> sqlite3.Row:
        """Return one durable work item without changing its lease."""

        self._ensure_open()
        row = self._connection.execute(
            """
            SELECT
                id, scan_id, content_version_id, kind, status, priority,
                attempts, lease_owner, lease_until,
                CASE
                    WHEN payload_json IS NULL THEN NULL
                    WHEN typeof(payload_json) = 'text'
                        AND length(CAST(payload_json AS BLOB)) <= ?
                    THEN payload_json
                    ELSE NULL
                END AS payload_json,
                CASE
                    WHEN payload_json IS NULL THEN 1
                    WHEN typeof(payload_json) = 'text'
                        AND length(CAST(payload_json AS BLOB)) <= ?
                    THEN 1
                    ELSE 0
                END AS payload_json_valid,
                last_error, created_at, updated_at
            FROM work_items
            WHERE id = ?
            """,
            (
                MAX_WORK_ITEM_PAYLOAD_BYTES,
                MAX_WORK_ITEM_PAYLOAD_BYTES,
                work_item_id,
            ),
        ).fetchone()
        if row is None:
            raise CatalogStateError("Unknown work item {}".format(work_item_id))
        return row

    def get_content_context(self, content_version_id: int) -> Optional[sqlite3.Row]:
        """Return a content generation and one current active path for it."""

        self._ensure_open()
        return self._connection.execute(
            """
            SELECT
                content_versions.id AS content_version_id,
                content_versions.size,
                content_versions.mtime_ns,
                content_versions.change_token,
                content_versions.state AS content_state,
                physical_files.id AS physical_file_id,
                physical_files.native_file_id,
                physical_files.identity_confidence,
                physical_files.current_content_version_id,
                paths.id AS path_id,
                paths.root_id,
                paths.display_path,
                paths.path_key,
                paths.state AS path_state
            FROM content_versions
            JOIN physical_files
                ON physical_files.id = content_versions.physical_file_id
            LEFT JOIN paths
                ON paths.id = (
                    SELECT candidate.id
                    FROM paths AS candidate
                    WHERE candidate.physical_file_id = physical_files.id
                        AND candidate.state = 'active'
                    ORDER BY candidate.id
                    LIMIT 1
                )
            WHERE content_versions.id = ?
            """,
            (content_version_id,),
        ).fetchone()

    def _exact_digest_root_scope(self, root_ids):
        root_filter = ""
        root_parameters: List[Any] = []
        if root_ids is not None:
            root_ids = tuple(dict.fromkeys(root_ids))
            if not root_ids:
                return None, []
            self.require_roots_projectable(root_ids)
            root_filter = " AND paths.root_id IN ({})".format(",".join("?" for _ in root_ids))
            root_parameters.extend(root_ids)
        return root_filter, root_parameters

    @staticmethod
    def _projection_cap(name: str, value: Optional[int]) -> int:
        if value is None:
            return MAX_SQLITE_INTEGER
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError("{} must be an integer of at least two".format(name))
        return value

    def exact_digest_projection_counts(
        self,
        algorithm: str = "sha256",
        algorithm_version: str = "1",
        root_ids: Optional[Sequence[int]] = None,
    ) -> ExactDigestProjectionCounts:
        """Count exact groups and members without returning any candidate rows."""

        self._ensure_open()
        root_filter, root_parameters = self._exact_digest_root_scope(root_ids)
        if root_filter is None:
            return ExactDigestProjectionCounts(0, 0, 0)
        row = self._connection.execute(
            """
            WITH candidate_groups AS (
                SELECT
                    content_versions.size AS size,
                    artifacts.value AS full_digest,
                    COUNT(DISTINCT content_versions.id) AS member_count
                FROM artifacts
                JOIN content_versions
                    ON content_versions.id = artifacts.content_version_id
                JOIN physical_files
                    ON physical_files.id = content_versions.physical_file_id
                    AND physical_files.current_content_version_id = content_versions.id
                WHERE artifacts.kind = 'full_hash'
                    AND artifacts.algorithm = ?
                    AND artifacts.algorithm_version = ?
                    AND artifacts.parameters_hash = ''
                    AND EXISTS (
                        SELECT 1
                        FROM paths
                        JOIN roots ON roots.id = paths.root_id
                        WHERE paths.physical_file_id = physical_files.id
                            AND paths.state = 'active'
                            AND paths.last_seen_scan_id = roots.last_complete_scan_id
                            {}
                    )
                GROUP BY content_versions.size, artifacts.value
                HAVING COUNT(DISTINCT content_versions.id) > 1
            )
            SELECT
                COUNT(*) AS group_count,
                COALESCE(SUM(member_count), 0) AS file_count,
                COALESCE(MAX(member_count), 0) AS max_group_members
            FROM candidate_groups
            """.format(root_filter),
            (algorithm, algorithm_version, *root_parameters),
        ).fetchone()
        return ExactDigestProjectionCounts(
            int(row["group_count"]),
            int(row["file_count"]),
            int(row["max_group_members"]),
        )

    def page_exact_digest_candidates(
        self,
        after_size: int = -1,
        after_digest: Any = b"",
        limit: int = 100,
        algorithm: str = "sha256",
        algorithm_version: str = "1",
        root_ids: Optional[Sequence[int]] = None,
        max_rows: Optional[int] = None,
        max_group_members: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        """Keyset-page complete exact groups under explicit member/row caps.

        ``limit`` bounds groups. ``max_rows`` bounds the total file rows
        returned by one page, and ``max_group_members`` excludes oversized
        groups in SQL. A group is therefore returned whole or not at all.
        """

        self._ensure_open()
        self._validate_page_size(limit)
        row_cap = self._projection_cap("max_rows", max_rows)
        member_cap = self._projection_cap("max_group_members", max_group_members)
        if max_rows is not None and max_group_members is not None and row_cap < member_cap:
            raise ValueError("max_rows must be at least max_group_members")
        member_cap = min(member_cap, row_cap)
        after_digest = self._encode_blob(after_digest)
        if after_digest is None:
            after_digest = b""
        root_filter, root_parameters = self._exact_digest_root_scope(root_ids)
        if root_filter is None:
            return []
        return list(
            self._connection.execute(
                """
                WITH candidate_group_counts AS (
                    SELECT
                        content_versions.size AS size,
                        artifacts.value AS full_digest,
                        COUNT(DISTINCT content_versions.id) AS member_count
                    FROM artifacts
                    JOIN content_versions
                        ON content_versions.id = artifacts.content_version_id
                    JOIN physical_files
                        ON physical_files.id = content_versions.physical_file_id
                        AND physical_files.current_content_version_id = content_versions.id
                    WHERE artifacts.kind = 'full_hash'
                        AND artifacts.algorithm = ?
                        AND artifacts.algorithm_version = ?
                        AND artifacts.parameters_hash = ''
                        AND EXISTS (
                            SELECT 1
                            FROM paths
                            JOIN roots ON roots.id = paths.root_id
                            WHERE paths.physical_file_id = physical_files.id
                                AND paths.state = 'active'
                                AND paths.last_seen_scan_id = roots.last_complete_scan_id
                                {}
                        )
                        AND (
                            content_versions.size > ?
                            OR (
                                content_versions.size = ?
                                AND artifacts.value > ?
                            )
                        )
                    GROUP BY content_versions.size, artifacts.value
                    HAVING COUNT(DISTINCT content_versions.id) > 1
                        AND COUNT(DISTINCT content_versions.id) <= ?
                ),
                ranked_candidate_groups AS (
                    SELECT
                        size,
                        full_digest,
                        member_count,
                        SUM(member_count) OVER (
                            ORDER BY size, full_digest
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS cumulative_members
                    FROM candidate_group_counts
                ),
                candidate_groups AS (
                    SELECT size, full_digest
                    FROM ranked_candidate_groups
                    WHERE cumulative_members <= ?
                    ORDER BY size, full_digest
                    LIMIT ?
                )
                SELECT
                    candidate_groups.size,
                    candidate_groups.full_digest,
                    content_versions.id AS content_version_id,
                    physical_files.id AS physical_file_id,
                    physical_files.native_file_id,
                    physical_files.identity_confidence,
                    content_versions.mtime_ns,
                    content_versions.change_token,
                    paths.id AS path_id,
                    paths.root_id,
                    paths.display_path
                FROM candidate_groups
                JOIN artifacts
                    ON artifacts.kind = 'full_hash'
                    AND artifacts.algorithm = ?
                    AND artifacts.algorithm_version = ?
                    AND artifacts.parameters_hash = ''
                    AND artifacts.value = candidate_groups.full_digest
                JOIN content_versions
                    ON content_versions.id = artifacts.content_version_id
                    AND content_versions.size = candidate_groups.size
                JOIN physical_files
                    ON physical_files.id = content_versions.physical_file_id
                    AND physical_files.current_content_version_id = content_versions.id
                JOIN paths
                    ON paths.id = (
                        SELECT candidate.id
                        FROM paths AS candidate
                        JOIN roots AS candidate_root
                            ON candidate_root.id = candidate.root_id
                        WHERE candidate.physical_file_id = physical_files.id
                            AND candidate.state = 'active'
                            AND candidate.last_seen_scan_id =
                                candidate_root.last_complete_scan_id
                            {}
                        ORDER BY candidate.id
                        LIMIT 1
                    )
                ORDER BY
                    candidate_groups.size,
                    candidate_groups.full_digest,
                    content_versions.id
                """.format(
                    root_filter,
                    root_filter.replace("paths.root_id", "candidate.root_id"),
                ),
                tuple(
                    [
                        algorithm,
                        algorithm_version,
                    ]
                    + root_parameters
                    + [
                        after_size,
                        after_size,
                        after_digest,
                        member_cap,
                        row_cap,
                        limit,
                        algorithm,
                        algorithm_version,
                    ]
                    + root_parameters
                ),
            )
        )

    def enqueue_work_item(
        self,
        scan_id: int,
        content_version_id: int,
        kind: str,
        priority: int = 0,
        payload: Any = None,
        now: Optional[float] = None,
    ) -> int:
        now = time.time() if now is None else now
        payload_json = self._json_payload(payload)
        if payload_json is not None and len(payload_json.encode("utf-8")) > MAX_WORK_ITEM_PAYLOAD_BYTES:
            raise ValueError("work item payload exceeds the {}-byte limit".format(MAX_WORK_ITEM_PAYLOAD_BYTES))
        with self.transaction():
            self._require_scan_running(scan_id)
            if (
                self._connection.execute(
                    "SELECT 1 FROM content_versions WHERE id = ?", (content_version_id,)
                ).fetchone()
                is None
            ):
                raise CatalogStateError("Unknown content version {}".format(content_version_id))
            self._connection.execute(
                """
                INSERT OR IGNORE INTO work_items(
                    scan_id, content_version_id, kind, status, priority,
                    payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (scan_id, content_version_id, kind, priority, payload_json, now, now),
            )
            row = self._connection.execute(
                """
                SELECT id FROM work_items
                WHERE scan_id = ? AND content_version_id = ? AND kind = ?
                """,
                (scan_id, content_version_id, kind),
            ).fetchone()
            return row["id"]

    def claim_work_items(
        self,
        owner: str,
        limit: int = 1,
        lease_seconds: float = 60,
        scan_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> List[sqlite3.Row]:
        self._validate_page_size(limit)
        if not owner:
            raise ValueError("owner must not be empty")
        if lease_seconds < 0:
            raise ValueError("lease_seconds must not be negative")
        now = time.time() if now is None else now
        with self.transaction():
            parameters: List[Any] = [now]
            scan_filter = ""
            if scan_id is not None:
                self._require_scan_running(scan_id)
                scan_filter = " AND scan_id = ?"
                parameters.append(scan_id)
            parameters.append(now)
            self._connection.execute(
                """
                UPDATE work_items
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'in_progress'{} AND lease_until <= ?
                """.format(scan_filter),
                parameters,
            )

            select_parameters: List[Any] = []
            select_filter = ""
            if scan_id is not None:
                select_filter = " AND scan_id = ?"
                select_parameters.append(scan_id)
            select_parameters.append(limit)
            ids = [
                row["id"]
                for row in self._connection.execute(
                    """
                    SELECT id FROM work_items
                    WHERE status = 'pending'{}
                    ORDER BY priority DESC, id
                    LIMIT ?
                    """.format(select_filter),
                    select_parameters,
                )
            ]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._connection.execute(
                """
                UPDATE work_items
                SET status = 'in_progress', attempts = attempts + 1,
                    lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE id IN ({})
                """.format(placeholders),
                (owner, now + lease_seconds, now, *ids),
            )
            return list(
                self._connection.execute(
                    """
                    SELECT
                        id, scan_id, content_version_id, kind, status, priority,
                        attempts, lease_owner, lease_until,
                        CASE
                            WHEN payload_json IS NULL THEN NULL
                            WHEN typeof(payload_json) = 'text'
                                AND length(CAST(payload_json AS BLOB)) <= ?
                            THEN payload_json
                            ELSE NULL
                        END AS payload_json,
                        CASE
                            WHEN payload_json IS NULL THEN 1
                            WHEN typeof(payload_json) = 'text'
                                AND length(CAST(payload_json AS BLOB)) <= ?
                            THEN 1
                            ELSE 0
                        END AS payload_json_valid,
                        last_error, created_at, updated_at
                    FROM work_items
                    WHERE id IN ({})
                    ORDER BY priority DESC, id
                    """.format(placeholders),
                    (
                        MAX_WORK_ITEM_PAYLOAD_BYTES,
                        MAX_WORK_ITEM_PAYLOAD_BYTES,
                        *ids,
                    ),
                )
            )

    def complete_work_item(self, work_item_id: int, owner: Optional[str] = None, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self.transaction():
            row = self._connection.execute(
                "SELECT status, lease_owner FROM work_items WHERE id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise CatalogStateError("Unknown work item {}".format(work_item_id))
            if row["status"] != "in_progress":
                raise CatalogStateError("Work item {} is not in progress".format(work_item_id))
            if owner is not None and row["lease_owner"] != owner:
                raise CatalogStateError("Work item {} is leased by another owner".format(work_item_id))
            self._connection.execute(
                """
                UPDATE work_items
                SET status = 'complete', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, work_item_id),
            )

    def fail_work_item(
        self,
        work_item_id: int,
        error: str,
        retry: bool = True,
        owner: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        now = time.time() if now is None else now
        with self.transaction():
            row = self._connection.execute(
                "SELECT status, lease_owner FROM work_items WHERE id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise CatalogStateError("Unknown work item {}".format(work_item_id))
            if row["status"] != "in_progress":
                raise CatalogStateError("Work item {} is not in progress".format(work_item_id))
            if owner is not None and row["lease_owner"] != owner:
                raise CatalogStateError("Work item {} is leased by another owner".format(work_item_id))
            self._connection.execute(
                """
                UPDATE work_items
                SET status = ?, lease_owner = NULL, lease_until = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("pending" if retry else "failed", error, now, work_item_id),
            )

    def resume_expired_leases(self, scan_id: Optional[int] = None, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        with self.transaction():
            scan_parameters: List[Any] = [now]
            scan_filter = ""
            if scan_id is not None:
                self._require_scan_running(scan_id)
                scan_filter = " AND scan_id = ?"
                scan_parameters.append(scan_id)
            scan_parameters.append(now)
            directory_cursor = self._connection.execute(
                """
                UPDATE scan_dirs
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'in_progress'{} AND lease_until <= ?
                """.format(scan_filter),
                scan_parameters,
            )
            work_cursor = self._connection.execute(
                """
                UPDATE work_items
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'in_progress'{} AND lease_until <= ?
                """.format(scan_filter),
                scan_parameters,
            )
            return {
                "scan_dirs": directory_cursor.rowcount,
                "work_items": work_cursor.rowcount,
            }

    def scan_coverage(self, scan_id: int) -> Dict[str, Any]:
        scan = self.get_scan(scan_id)
        counts = {status: 0 for status in SCAN_DIRECTORY_STATUS_VALUES}
        for row in self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM scan_dirs WHERE scan_id = ? GROUP BY status",
            (scan_id,),
        ):
            counts[row["status"]] = row["count"]
        counts["total"] = sum(counts[status] for status in SCAN_DIRECTORY_STATUS_VALUES)
        counts["errors"] = self._connection.execute(
            "SELECT COUNT(*) FROM scan_errors WHERE scan_id = ?", (scan_id,)
        ).fetchone()[0]
        for row in self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM work_items WHERE scan_id = ? GROUP BY status",
            (scan_id,),
        ):
            counts["work_{}".format(row["status"])] = row["count"]
        counts["scan_status"] = scan["status"]
        return counts

    def scan_root_coverage(self, scan_id: int, root_id: int) -> Dict[str, Any]:
        """Return directory/error coverage scoped to one root in a multi-root scan."""

        scan = self.get_scan(scan_id)
        counts = {status: 0 for status in SCAN_DIRECTORY_STATUS_VALUES}
        for row in self._connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM scan_dirs
            WHERE scan_id = ? AND root_id = ?
            GROUP BY status
            """,
            (scan_id, root_id),
        ):
            counts[row["status"]] = row["count"]
        counts["total"] = sum(counts[status] for status in SCAN_DIRECTORY_STATUS_VALUES)
        counts["errors"] = self._connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_errors
            LEFT JOIN scan_dirs ON scan_dirs.id = scan_errors.scan_dir_id
            WHERE scan_errors.scan_id = ?
                AND (scan_dirs.root_id = ? OR scan_errors.scan_dir_id IS NULL)
            """,
            (scan_id, root_id),
        ).fetchone()[0]
        counts["scan_status"] = scan["status"]
        return counts

    def finish_scan(self, scan_id: int, now: Optional[float] = None) -> str:
        """Finalize completed coverage and tombstone only entries in successfully enumerated dirs."""

        now = time.time() if now is None else now
        with self.transaction():
            self._require_scan_running(scan_id)
            unfinished = self._connection.execute(
                """
                SELECT COUNT(*) FROM scan_dirs
                WHERE scan_id = ? AND status IN ('pending', 'in_progress')
                """,
                (scan_id,),
            ).fetchone()[0]
            if unfinished:
                raise ScanIncompleteError(
                    "Scan {} still has {} pending or leased directories".format(scan_id, unfinished)
                )
            unfinished_work = self._connection.execute(
                """
                SELECT COUNT(*) FROM work_items
                WHERE scan_id = ? AND status IN ('pending', 'in_progress')
                """,
                (scan_id,),
            ).fetchone()[0]
            if unfinished_work:
                raise ScanIncompleteError(
                    "Scan {} still has {} pending or leased work items".format(scan_id, unfinished_work)
                )
            self._connection.execute("UPDATE scans SET phase = 'finalizing' WHERE id = ?", (scan_id,))
            global_error_count = self._connection.execute(
                """
                SELECT COUNT(*) FROM scan_errors
                WHERE scan_id = ? AND scan_dir_id IS NULL
                """,
                (scan_id,),
            ).fetchone()[0]
            completed_directories = (
                list(
                    self._connection.execute(
                        """
                    SELECT root_id, path_key FROM scan_dirs
                    WHERE scan_id = ? AND status = 'complete' AND error_count = 0
                    """,
                        (scan_id,),
                    )
                )
                if global_error_count == 0
                else []
            )
            for directory in completed_directories:
                self._connection.execute(
                    """
                    UPDATE paths
                    SET state = 'missing', last_seen_at = ?
                    WHERE root_id = ? AND parent_path_key = ?
                        AND state != 'missing' AND last_seen_scan_id != ?
                    """,
                    (now, directory["root_id"], directory["path_key"], scan_id),
                )

            root_ids = [
                row["root_id"]
                for row in self._connection.execute(
                    "SELECT DISTINCT root_id FROM scan_dirs WHERE scan_id = ?", (scan_id,)
                )
            ]
            error_count = self._connection.execute(
                "SELECT COUNT(*) FROM scan_errors WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            failed_work_count = self._connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE scan_id = ? AND status = 'failed'",
                (scan_id,),
            ).fetchone()[0]
            all_roots_complete = True
            for root_id in root_ids:
                incomplete_count = self._connection.execute(
                    """
                    SELECT COUNT(*) FROM scan_dirs
                    WHERE scan_id = ? AND root_id = ? AND status != 'complete'
                    """,
                    (scan_id, root_id),
                ).fetchone()[0]
                if incomplete_count:
                    all_roots_complete = False
                elif error_count == 0 and failed_work_count == 0:
                    self._connection.execute(
                        "UPDATE roots SET last_complete_scan_id = ?, updated_at = ? WHERE id = ?",
                        (scan_id, now, root_id),
                    )

            status = (
                "complete"
                if all_roots_complete and error_count == 0 and failed_work_count == 0
                else "completed_with_errors"
            )
            if status == "complete":
                path_count = self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM scan_path_observations
                    WHERE scan_id = ?
                    """,
                    (scan_id,),
                ).fetchone()[0]
                self._connection.execute(
                    """
                    INSERT INTO scan_snapshots(
                        scan_id, completed_at, root_count, path_count
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (scan_id, now, len(root_ids), path_count),
                )
            self._connection.execute(
                """
                UPDATE scans
                SET status = ?, phase = 'complete', finished_at = ?, error_count = ?
                WHERE id = ?
                """,
                (status, now, error_count + failed_work_count, scan_id),
            )
            return status

    def cancel_scan(self, scan_id: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self.transaction():
            self._require_scan_running(scan_id)
            self._connection.execute(
                """
                UPDATE scans
                SET status = 'cancelled', phase = 'cancelled', finished_at = ?
                WHERE id = ?
                """,
                (now, scan_id),
            )

    def record_verification(
        self,
        first_content_version_id: int,
        second_content_version_id: int,
        algorithm: str,
        algorithm_version: str,
        full_digest: Any,
        state: str = "candidate",
        byte_compare_at: Optional[float] = None,
        now: Optional[float] = None,
    ) -> int:
        self._validate_value("verification state", state, VERIFICATION_STATE_VALUES)
        first_content_version_id, second_content_version_id = sorted(
            (first_content_version_id, second_content_version_id)
        )
        full_digest = self._encode_blob(full_digest)
        if full_digest is None:
            raise ValueError("full_digest must not be None")
        now = time.time() if now is None else now
        with self.transaction():
            version_count = self._connection.execute(
                "SELECT COUNT(*) FROM content_versions WHERE id IN (?, ?)",
                (first_content_version_id, second_content_version_id),
            ).fetchone()[0]
            expected_count = 1 if first_content_version_id == second_content_version_id else 2
            if version_count != expected_count:
                raise CatalogStateError("Unknown content version in verification pair")
            self._connection.execute(
                """
                INSERT INTO verification_records(
                    first_content_version_id, second_content_version_id,
                    algorithm, algorithm_version, full_digest,
                    byte_compare_at, state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    first_content_version_id, second_content_version_id,
                    algorithm, algorithm_version, full_digest
                ) DO UPDATE SET
                    byte_compare_at = excluded.byte_compare_at,
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (
                    first_content_version_id,
                    second_content_version_id,
                    algorithm,
                    algorithm_version,
                    full_digest,
                    byte_compare_at,
                    state,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id FROM verification_records
                WHERE first_content_version_id = ? AND second_content_version_id = ?
                    AND algorithm = ? AND algorithm_version = ? AND full_digest = ?
                """,
                (
                    first_content_version_id,
                    second_content_version_id,
                    algorithm,
                    algorithm_version,
                    full_digest,
                ),
            ).fetchone()
            return row["id"]

    def find_verification_id(
        self,
        first_content_version_id: int,
        second_content_version_id: int,
        algorithm: str,
        algorithm_version: str,
        full_digest: Any,
        *,
        state: str = "verified",
    ) -> Optional[int]:
        """Return one already-persisted verification without changing the catalog."""

        self._ensure_open()
        self._validate_value("verification state", state, VERIFICATION_STATE_VALUES)
        first_content_version_id, second_content_version_id = sorted(
            (first_content_version_id, second_content_version_id)
        )
        encoded_digest = self._encode_blob(full_digest)
        if encoded_digest is None:
            raise ValueError("full_digest must not be None")
        row = self._connection.execute(
            """
            SELECT id
            FROM verification_records
            WHERE first_content_version_id = ?
                AND second_content_version_id = ?
                AND algorithm = ?
                AND algorithm_version = ?
                AND full_digest = ?
                AND state = ?
                AND byte_compare_at IS NOT NULL
            """,
            (
                first_content_version_id,
                second_content_version_id,
                algorithm,
                algorithm_version,
                encoded_digest,
                state,
            ),
        ).fetchone()
        return None if row is None else int(row["id"])

    def journal_action(
        self,
        action_type: str,
        status: str = "planned",
        scan_id: Optional[int] = None,
        verification_id: Optional[int] = None,
        physical_file_id: Optional[int] = None,
        path_id: Optional[int] = None,
        payload: Any = None,
        now: Optional[float] = None,
    ) -> int:
        self._validate_value("action status", status, ACTION_STATUS_VALUES)
        now = time.time() if now is None else now
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO action_journal(
                    action_type, status, scan_id, verification_id,
                    physical_file_id, path_id, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_type,
                    status,
                    scan_id,
                    verification_id,
                    physical_file_id,
                    path_id,
                    self._json_payload(payload),
                    now,
                    now,
                ),
            )
            return cursor.lastrowid

    def update_action(
        self,
        action_id: int,
        status: str,
        error: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        self._validate_value("action status", status, ACTION_STATUS_VALUES)
        now = time.time() if now is None else now
        completed_at = now if status in {"completed", "failed", "cancelled"} else None
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE action_journal
                SET status = ?, error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, error, now, completed_at, action_id),
            )
            if cursor.rowcount != 1:
                raise CatalogStateError("Unknown action {}".format(action_id))

    def _require_complete_scan_snapshot(
        self,
        scan_id: int,
        root_ids: Sequence[int],
    ) -> None:
        if self.schema_version < 3:
            raise CatalogStateError("Catalog schema predates immutable path snapshots")
        scan = self.get_scan(scan_id)
        if scan["status"] != "complete":
            raise CatalogStateError("Scan {} is not complete and cannot prove historical differences".format(scan_id))
        snapshot = self._connection.execute(
            "SELECT 1 FROM scan_snapshots WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if snapshot is None:
            raise CatalogStateError("Scan {} predates immutable path snapshots".format(scan_id))
        selected_roots = {row["root_id"] for row in self.scan_roots(scan_id)}
        unavailable = set(root_ids) - selected_roots
        if unavailable:
            raise CatalogStateError(
                "Scan {} did not cover roots {}".format(
                    scan_id,
                    ", ".join(str(root_id) for root_id in sorted(unavailable)),
                )
            )

    def page_scan_changes(
        self,
        before_scan_id: int,
        after_scan_id: int,
        root_ids: Sequence[int],
        after_root_id: int = 0,
        after_path_key: str = "",
        after_change_type: str = "",
        limit: int = 100,
    ) -> List[sqlite3.Row]:
        """Keyset-page proven changes between two immutable complete snapshots.

        A same-path content or identity change is ``modified``. A path present
        in only one snapshot is ``added`` or ``missing``. It becomes ``moved``
        only when exactly one old and one new unmatched path share the same
        non-null native identity on the same volume and both observations have
        ``stable`` identity confidence. Ambiguous hard-link/path-only cases are
        deliberately emitted as separate ``added`` and ``missing`` changes.

        Callers continue with the last row's ``sort_root_id``,
        ``sort_path_key``, and ``change_type`` values.
        """

        self._ensure_open()
        self._validate_page_size(limit)
        root_ids = tuple(dict.fromkeys(root_ids))
        if not root_ids:
            raise ValueError("at least one root is required for scan differences")
        if after_root_id < 0:
            raise ValueError("after_root_id must not be negative")
        if not isinstance(after_path_key, str) or not isinstance(
            after_change_type,
            str,
        ):
            raise TypeError("scan difference cursor fields must be strings")
        self._require_complete_scan_snapshot(before_scan_id, root_ids)
        self._require_complete_scan_snapshot(after_scan_id, root_ids)

        placeholders = ",".join("?" for _ in root_ids)
        parameters: List[Any] = [
            before_scan_id,
            *root_ids,
            after_scan_id,
            *root_ids,
            after_root_id,
            after_root_id,
            after_path_key,
            after_root_id,
            after_path_key,
            after_change_type,
            limit,
        ]
        return list(
            self._connection.execute(
                """
                WITH
                before_rows AS (
                    SELECT
                        observations.id AS observation_id,
                        observations.root_id,
                        observations.volume_id,
                        observations.path_id,
                        observations.physical_file_id,
                        observations.content_version_id,
                        observations.display_path,
                        observations.path_key,
                        observations.path_state,
                        observations.content_state,
                        observations.native_file_id,
                        observations.identity_confidence
                    FROM scan_path_observations AS observations
                    WHERE observations.scan_id = ?
                        AND observations.root_id IN ({roots})
                ),
                after_rows AS (
                    SELECT
                        observations.id AS observation_id,
                        observations.root_id,
                        observations.volume_id,
                        observations.path_id,
                        observations.physical_file_id,
                        observations.content_version_id,
                        observations.display_path,
                        observations.path_key,
                        observations.path_state,
                        observations.content_state,
                        observations.native_file_id,
                        observations.identity_confidence
                    FROM scan_path_observations AS observations
                    WHERE observations.scan_id = ?
                        AND observations.root_id IN ({roots})
                ),
                old_only AS (
                    SELECT before_rows.*
                    FROM before_rows
                    LEFT JOIN after_rows
                        ON after_rows.root_id = before_rows.root_id
                        AND after_rows.path_key = before_rows.path_key
                    WHERE after_rows.observation_id IS NULL
                ),
                new_only AS (
                    SELECT after_rows.*
                    FROM after_rows
                    LEFT JOIN before_rows
                        ON before_rows.root_id = after_rows.root_id
                        AND before_rows.path_key = after_rows.path_key
                    WHERE before_rows.observation_id IS NULL
                ),
                old_identity_counts AS (
                    SELECT volume_id, native_file_id, COUNT(*) AS path_count
                    FROM old_only
                    WHERE identity_confidence = 'stable'
                        AND native_file_id IS NOT NULL
                    GROUP BY volume_id, native_file_id
                ),
                new_identity_counts AS (
                    SELECT volume_id, native_file_id, COUNT(*) AS path_count
                    FROM new_only
                    WHERE identity_confidence = 'stable'
                        AND native_file_id IS NOT NULL
                    GROUP BY volume_id, native_file_id
                ),
                modified AS (
                    SELECT
                        'modified' AS change_type,
                        before_rows.observation_id AS old_observation_id,
                        after_rows.observation_id AS new_observation_id,
                        before_rows.root_id AS old_root_id,
                        after_rows.root_id AS new_root_id,
                        before_rows.path_id AS old_path_id,
                        after_rows.path_id AS new_path_id,
                        before_rows.display_path AS old_display_path,
                        after_rows.display_path AS new_display_path,
                        before_rows.path_key AS old_path_key,
                        after_rows.path_key AS new_path_key,
                        before_rows.physical_file_id AS old_physical_file_id,
                        after_rows.physical_file_id AS new_physical_file_id,
                        before_rows.content_version_id AS old_content_version_id,
                        after_rows.content_version_id AS new_content_version_id,
                        before_rows.path_state AS old_path_state,
                        after_rows.path_state AS new_path_state,
                        before_rows.content_state AS old_content_state,
                        after_rows.content_state AS new_content_state,
                        before_rows.identity_confidence AS old_identity_confidence,
                        after_rows.identity_confidence AS new_identity_confidence,
                        before_rows.native_file_id AS old_native_file_id,
                        after_rows.native_file_id AS new_native_file_id,
                        CASE
                            WHEN before_rows.content_version_id !=
                                after_rows.content_version_id
                            THEN 1 ELSE 0
                        END AS content_changed,
                        0 AS identity_proven,
                        after_rows.root_id AS sort_root_id,
                        after_rows.path_key AS sort_path_key
                    FROM before_rows
                    JOIN after_rows
                        ON after_rows.root_id = before_rows.root_id
                        AND after_rows.path_key = before_rows.path_key
                    WHERE before_rows.physical_file_id !=
                            after_rows.physical_file_id
                        OR before_rows.content_version_id !=
                            after_rows.content_version_id
                        OR before_rows.display_path != after_rows.display_path
                        OR before_rows.path_state != after_rows.path_state
                        OR before_rows.content_state != after_rows.content_state
                ),
                moved AS (
                    SELECT
                        'moved' AS change_type,
                        old_only.observation_id AS old_observation_id,
                        new_only.observation_id AS new_observation_id,
                        old_only.root_id AS old_root_id,
                        new_only.root_id AS new_root_id,
                        old_only.path_id AS old_path_id,
                        new_only.path_id AS new_path_id,
                        old_only.display_path AS old_display_path,
                        new_only.display_path AS new_display_path,
                        old_only.path_key AS old_path_key,
                        new_only.path_key AS new_path_key,
                        old_only.physical_file_id AS old_physical_file_id,
                        new_only.physical_file_id AS new_physical_file_id,
                        old_only.content_version_id AS old_content_version_id,
                        new_only.content_version_id AS new_content_version_id,
                        old_only.path_state AS old_path_state,
                        new_only.path_state AS new_path_state,
                        old_only.content_state AS old_content_state,
                        new_only.content_state AS new_content_state,
                        old_only.identity_confidence AS old_identity_confidence,
                        new_only.identity_confidence AS new_identity_confidence,
                        old_only.native_file_id AS old_native_file_id,
                        new_only.native_file_id AS new_native_file_id,
                        CASE
                            WHEN old_only.content_version_id !=
                                new_only.content_version_id
                            THEN 1 ELSE 0
                        END AS content_changed,
                        1 AS identity_proven,
                        new_only.root_id AS sort_root_id,
                        new_only.path_key AS sort_path_key
                    FROM old_only
                    JOIN new_only
                        ON new_only.volume_id = old_only.volume_id
                        AND new_only.native_file_id = old_only.native_file_id
                    JOIN old_identity_counts
                        ON old_identity_counts.volume_id = old_only.volume_id
                        AND old_identity_counts.native_file_id =
                            old_only.native_file_id
                        AND old_identity_counts.path_count = 1
                    JOIN new_identity_counts
                        ON new_identity_counts.volume_id = new_only.volume_id
                        AND new_identity_counts.native_file_id =
                            new_only.native_file_id
                        AND new_identity_counts.path_count = 1
                    WHERE old_only.identity_confidence = 'stable'
                        AND new_only.identity_confidence = 'stable'
                        AND old_only.native_file_id IS NOT NULL
                ),
                missing AS (
                    SELECT
                        'missing' AS change_type,
                        old_only.observation_id AS old_observation_id,
                        NULL AS new_observation_id,
                        old_only.root_id AS old_root_id,
                        NULL AS new_root_id,
                        old_only.path_id AS old_path_id,
                        NULL AS new_path_id,
                        old_only.display_path AS old_display_path,
                        NULL AS new_display_path,
                        old_only.path_key AS old_path_key,
                        NULL AS new_path_key,
                        old_only.physical_file_id AS old_physical_file_id,
                        NULL AS new_physical_file_id,
                        old_only.content_version_id AS old_content_version_id,
                        NULL AS new_content_version_id,
                        old_only.path_state AS old_path_state,
                        NULL AS new_path_state,
                        old_only.content_state AS old_content_state,
                        NULL AS new_content_state,
                        old_only.identity_confidence AS old_identity_confidence,
                        NULL AS new_identity_confidence,
                        old_only.native_file_id AS old_native_file_id,
                        NULL AS new_native_file_id,
                        0 AS content_changed,
                        0 AS identity_proven,
                        old_only.root_id AS sort_root_id,
                        old_only.path_key AS sort_path_key
                    FROM old_only
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM moved
                        WHERE moved.old_observation_id =
                            old_only.observation_id
                    )
                ),
                added AS (
                    SELECT
                        'added' AS change_type,
                        NULL AS old_observation_id,
                        new_only.observation_id AS new_observation_id,
                        NULL AS old_root_id,
                        new_only.root_id AS new_root_id,
                        NULL AS old_path_id,
                        new_only.path_id AS new_path_id,
                        NULL AS old_display_path,
                        new_only.display_path AS new_display_path,
                        NULL AS old_path_key,
                        new_only.path_key AS new_path_key,
                        NULL AS old_physical_file_id,
                        new_only.physical_file_id AS new_physical_file_id,
                        NULL AS old_content_version_id,
                        new_only.content_version_id AS new_content_version_id,
                        NULL AS old_path_state,
                        new_only.path_state AS new_path_state,
                        NULL AS old_content_state,
                        new_only.content_state AS new_content_state,
                        NULL AS old_identity_confidence,
                        new_only.identity_confidence AS new_identity_confidence,
                        NULL AS old_native_file_id,
                        new_only.native_file_id AS new_native_file_id,
                        0 AS content_changed,
                        0 AS identity_proven,
                        new_only.root_id AS sort_root_id,
                        new_only.path_key AS sort_path_key
                    FROM new_only
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM moved
                        WHERE moved.new_observation_id =
                            new_only.observation_id
                    )
                ),
                changes AS (
                    SELECT * FROM modified
                    UNION ALL
                    SELECT * FROM moved
                    UNION ALL
                    SELECT * FROM missing
                    UNION ALL
                    SELECT * FROM added
                )
                SELECT *
                FROM changes
                WHERE sort_root_id > ?
                    OR (
                        sort_root_id = ?
                        AND sort_path_key > ?
                    )
                    OR (
                        sort_root_id = ?
                        AND sort_path_key = ?
                        AND change_type > ?
                    )
                ORDER BY sort_root_id, sort_path_key, change_type
                LIMIT ?
                """.format(roots=placeholders),
                parameters,
            )
        )

    def page_paths(
        self,
        after_id: int = 0,
        limit: int = 100,
        root_id: Optional[int] = None,
        states: Optional[Sequence[str]] = ("active",),
    ) -> List[sqlite3.Row]:
        """Keyset-page catalog paths without materializing the whole library."""

        self._ensure_open()
        self._validate_page_size(limit)
        clauses = ["paths.id > ?"]
        parameters: List[Any] = [after_id]
        if root_id is not None:
            clauses.append("paths.root_id = ?")
            parameters.append(root_id)
        if states is not None:
            states = tuple(states)
            if not states:
                return []
            for state in states:
                self._validate_value("path state", state, PATH_STATE_VALUES)
            clauses.append("paths.state IN ({})".format(",".join("?" for _ in states)))
            parameters.extend(states)
        parameters.append(limit)
        return list(
            self._connection.execute(
                """
                SELECT
                    paths.id AS path_id,
                    paths.root_id,
                    paths.physical_file_id,
                    paths.display_path,
                    paths.path_key,
                    paths.parent_path_key,
                    paths.state AS path_state,
                    paths.last_seen_scan_id,
                    physical_files.native_file_id,
                    physical_files.identity_confidence,
                    physical_files.current_content_version_id,
                    content_versions.size,
                    content_versions.mtime_ns,
                    content_versions.change_token,
                    content_versions.state AS content_state
                FROM paths
                JOIN physical_files ON physical_files.id = paths.physical_file_id
                LEFT JOIN content_versions
                    ON content_versions.id = physical_files.current_content_version_id
                WHERE {}
                ORDER BY paths.id
                LIMIT ?
                """.format(" AND ".join(clauses)),
                parameters,
            )
        )

    def page_work_items(
        self,
        after_id: int = 0,
        limit: int = 100,
        scan_id: Optional[int] = None,
        statuses: Optional[Sequence[str]] = None,
    ) -> List[sqlite3.Row]:
        self._ensure_open()
        self._validate_page_size(limit)
        clauses = ["id > ?"]
        parameters: List[Any] = [after_id]
        if scan_id is not None:
            clauses.append("scan_id = ?")
            parameters.append(scan_id)
        if statuses is not None:
            statuses = tuple(statuses)
            if not statuses:
                return []
            for status in statuses:
                self._validate_value("work status", status, WORK_STATUS_VALUES)
            clauses.append("status IN ({})".format(",".join("?" for _ in statuses)))
            parameters.extend(statuses)
        parameters.append(limit)
        return list(
            self._connection.execute(
                "SELECT * FROM work_items WHERE {} ORDER BY id LIMIT ?".format(" AND ".join(clauses)),
                parameters,
            )
        )

    def page_scan_errors(self, scan_id: int, after_id: int = 0, limit: int = 100) -> List[sqlite3.Row]:
        self._ensure_open()
        self._validate_page_size(limit)
        return list(
            self._connection.execute(
                """
                SELECT * FROM scan_errors
                WHERE scan_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (scan_id, after_id, limit),
            )
        )
