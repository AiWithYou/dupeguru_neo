# Created By: Virgil Dupras
# Created On: 2009-10-22
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

# This is a fork from hsfs. The reason for this fork is that hsfs has been designed for musicGuru
# and was re-used for dupeGuru. The problem is that hsfs is way over-engineered for dupeGuru,
# resulting needless complexity and memory usage. It's been a while since I wanted to do that fork,
# and I'm doing it now.

import contextlib
import hashlib
import ntpath
import os
import posixpath
import stat
import sys

from dataclasses import dataclass, field as dataclass_field, replace
from math import floor
import logging
import sqlite3
from threading import Lock
from typing import Any, AnyStr, Union

from pathlib import Path
import xxhash

from hscommon.util import nonone, get_file_ext
from core.file_generation import (
    FileGenerationToken,
    get_file_generation_token,
    get_file_generation_token_from_fd,
)

hasher = xxhash.xxh128
HASH_ALGORITHM = "xxh128"
REVIEW_CONTENT_DIGEST_ALGORITHM = "sha256"

__all__ = [
    "File",
    "Folder",
    "get_file",
    "get_files",
    "FSError",
    "AlreadyExistsError",
    "InvalidPath",
    "InvalidDestinationError",
    "OperationError",
]

NOT_SET = object()

# The goal here is to not run out of memory on really big files. However, the chunk
# size has to be large enough so that the python loop isn't too costly in terms of
# CPU.
CHUNK_SIZE = 1024 * 1024  # 1 MiB

# Minimum size below which partial hashing is not used
MIN_FILE_SIZE = 3 * CHUNK_SIZE  # 3MiB, because we take 3 samples

# Partial hashing offset and size
PARTIAL_OFFSET_SIZE = (0x4000, 0x4000)


class FileChangedError(OSError):
    """Raised when a file changes while it is being read."""


@dataclass(frozen=True)
class FileSnapshot:
    """Identity and generation metadata captured from an open file handle."""

    device: str
    file_id: str
    size: int
    mtime_ns: int
    ctime_ns: bytes
    content_digest_algorithm: Union[str, None] = dataclass_field(default=None, compare=False)
    content_digest: Union[bytes, None] = dataclass_field(default=None, compare=False)

    @classmethod
    def from_stat(cls, stat_result, generation_token):
        """Create a snapshot only from an explicitly observed generation token."""

        if isinstance(generation_token, FileGenerationToken):
            generation_token = generation_token.encoded
        if not isinstance(generation_token, bytes) or not generation_token:
            raise ValueError("FileSnapshot generation token must be non-empty bytes")
        return cls(
            device=str(stat_result.st_dev),
            file_id=str(stat_result.st_ino),
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            ctime_ns=generation_token,
        )

    @classmethod
    def from_path(cls, path, stat_result=None):
        stat_result = stat_result or os.stat(path, follow_symlinks=False)
        token = get_file_generation_token(path, stat_result=stat_result)
        return cls.from_stat(stat_result, token)

    @classmethod
    def from_file(cls, file_object, path=None, stat_result=None):
        stat_result = stat_result or os.fstat(file_object.fileno())
        token = get_file_generation_token_from_fd(
            file_object.fileno(),
            path=path,
            stat_result=stat_result,
        )
        return cls.from_stat(stat_result, token)

    @classmethod
    def from_path_with_content_digest(cls, path, stop_check=None):
        """Capture metadata plus a stable full-content proof from one handle."""

        return _snapshot_path_with_content_digest(Path(path), stop_check=stop_check)

    def with_content_digest(self, algorithm, digest):
        """Attach a validated full-content proof without changing SQL metadata."""

        if algorithm != REVIEW_CONTENT_DIGEST_ALGORITHM:
            raise ValueError("Unsupported review content digest algorithm: {!r}".format(algorithm))
        if not isinstance(digest, bytes) or len(digest) != hashlib.sha256().digest_size:
            raise ValueError("Review content digest must be one SHA-256 byte string")
        return replace(
            self,
            content_digest_algorithm=algorithm,
            content_digest=digest,
        )

    def as_sql_params(self):
        return {
            "device": self.device,
            "file_id": self.file_id,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }

    def same_content_generation(self, other):
        """Compare every generation field captured while hashing.

        ``ctime_ns`` retains its legacy database field name but contains the
        shared versioned generation-token bytes. Filesystems that cannot
        provide a proven token must miss the cache rather than reuse evidence.
        """
        return (
            self.device,
            self.file_id,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        ) == (
            other.device,
            other.file_id,
            other.size,
            other.mtime_ns,
            other.ctime_ns,
        )

    def same_reviewed_content(self, other):
        """Require both stable generation metadata and equal SHA-256 proofs."""

        return (
            self.same_content_generation(other)
            and self.content_digest_algorithm == REVIEW_CONTENT_DIGEST_ALGORITHM
            and other.content_digest_algorithm == REVIEW_CONTENT_DIGEST_ALGORITHM
            and isinstance(self.content_digest, bytes)
            and isinstance(other.content_digest, bytes)
            and self.content_digest == other.content_digest
        )


@dataclass(frozen=True)
class ByteComparisonEvidence:
    """Evidence that two stable file-handle snapshots had identical bytes."""

    first: FileSnapshot
    second: FileSnapshot
    bytes_compared: int
    sha256_digest: Union[bytes, None] = None


def _snapshot_path(path: Path) -> FileSnapshot:
    path_stat = os.stat(path, follow_symlinks=False)
    reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    if not stat.S_ISREG(path_stat.st_mode) or (reparse_marker and file_attributes & reparse_marker):
        raise OSError(f"Path is not a plain regular file: {path}")
    return FileSnapshot.from_path(path, path_stat)


def _snapshot_handle(fp, path=None) -> FileSnapshot:
    return FileSnapshot.from_file(fp, path=path)


def _snapshot_path_with_content_digest(
    path: Path,
    stop_check=None,
    progress_callback=None,
) -> FileSnapshot:
    """Hash one no-follow handle and prove its path/generation remained stable."""

    snapshot, _fast_digest = _snapshot_path_with_review_digests(
        path,
        stop_check=stop_check,
        progress_callback=progress_callback,
        include_fast_digest=False,
    )
    return snapshot


def _snapshot_path_with_review_digests(
    path: Path,
    stop_check=None,
    progress_callback=None,
    *,
    include_fast_digest,
):
    """Capture SHA-256 and optionally xxh128 in one streaming pass."""

    with _open_readonly_no_follow(path) as file_handle:
        before = _snapshot_handle(file_handle, path)
        digest = hashlib.sha256()
        fast_digest = hasher() if include_fast_digest else None
        bytes_read = 0
        while True:
            _raise_if_scan_stopped(stop_check)
            block = file_handle.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            if fast_digest is not None:
                fast_digest.update(block)
            bytes_read += len(block)
            if progress_callback is not None:
                progress_callback(len(block))
        after = _snapshot_handle(file_handle, path)
        _ensure_unchanged(before, after, path)
        if bytes_read != before.size:
            raise FileChangedError("File size changed while its review proof was read: {}".format(path))
    current = _snapshot_path(path)
    _ensure_unchanged(before, current, path)
    return (
        before.with_content_digest(
            REVIEW_CONTENT_DIGEST_ALGORITHM,
            digest.digest(),
        ),
        fast_digest.digest() if fast_digest is not None else None,
    )


_readonly_file_system = None


def _ensure_no_link_components(path):
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    authenticated_alias = None
    lexical_alias = None
    if parts:
        first = Path(absolute.anchor) / parts[0]
        first_stat = os.lstat(first)
        authenticated_alias = _authenticated_darwin_root_alias(first, first_stat)
        if authenticated_alias is not None:
            lexical_alias = first
            absolute = authenticated_alias.joinpath(*parts[1:])
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current.joinpath(component)
        component_stat = os.stat(current, follow_symlinks=False)
        reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(component_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(component_stat.st_mode) or (reparse_marker and file_attributes & reparse_marker):
            raise OSError(f"Path contains a symbolic link or reparse point: {current}")
    if authenticated_alias is not None:
        lexical_stat = os.lstat(lexical_alias)
        if _authenticated_darwin_root_alias(lexical_alias, lexical_stat) != authenticated_alias:
            raise OSError("Platform root alias changed while it was being authenticated: {}".format(lexical_alias))
    return absolute


@contextlib.contextmanager
def _open_readonly_no_follow(path):
    """Open a file without traversing a link or Windows reparse point.

    The action executor owns the platform-specific implementation because it
    uses the same primitive for live destructive proofs.  Importing lazily
    keeps the filesystem model independent during module initialization.
    """

    path = _ensure_no_link_components(path)
    before_path = _snapshot_path(path)
    global _readonly_file_system
    if _readonly_file_system is None:
        from core.safe_action import platform_file_system

        _readonly_file_system = platform_file_system()
    with _readonly_file_system.open_readonly(path) as file_handle:
        before_handle = _snapshot_handle(file_handle, path)
        _ensure_unchanged(before_path, before_handle, path)
        try:
            yield file_handle
        finally:
            after_handle = _snapshot_handle(file_handle, path)
            _ensure_no_link_components(path)
            after_path = _snapshot_path(path)
            _ensure_unchanged(before_handle, after_handle, path)
            _ensure_unchanged(before_path, after_path, path)


def _ensure_unchanged(before: FileSnapshot, after: FileSnapshot, path) -> None:
    if before != after:
        raise FileChangedError(f"File changed while being read: {path}")


def _raise_if_scan_stopped(stop_check) -> None:
    if stop_check is not None and stop_check():
        raise InterruptedError("exact scan resource limit reached")


class FSError(Exception):
    cls_message = "An error has occured on '{name}' in '{parent}'"

    def __init__(self, fsobject, parent=None):
        message = self.cls_message
        if isinstance(fsobject, str):
            name = fsobject
        elif isinstance(fsobject, File):
            name = fsobject.name
        else:
            name = ""
        parentname = str(parent) if parent is not None else ""
        Exception.__init__(self, message.format(name=name, parent=parentname))


class AlreadyExistsError(FSError):
    "The directory or file name we're trying to add already exists"

    cls_message = "'{name}' already exists in '{parent}'"


class InvalidPath(FSError):
    "The path of self is invalid, and cannot be worked with."

    cls_message = "'{name}' is invalid."


class InvalidDestinationError(FSError):
    """A copy/move operation has been called, but the destination is invalid."""

    cls_message = "'{name}' is an invalid destination for this operation."


class OperationError(FSError):
    """A copy/move/delete operation has been called, but the checkup after the
    operation shows that it didn't work."""

    cls_message = "Operation on '{name}' failed."


class HashCacheSafetyError(OSError):
    """The hash-cache path could not be proven to be private application state."""


_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_DATABASE_HEADER_SIZE = 100
_HASH_CACHE_APPLICATION_ID = 0x44474E48  # "DGNH"
_HASH_CACHE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_HASH_CACHE_SCHEMA_VERSION = 3
_HASH_CACHE_SCHEMA_DESCRIPTION = "Versioned OS generation tokens and atomic digest invalidation."
_HASH_CACHE_MAX_FILE_BYTES = 64 * 1024 * 1024 * 1024
_HASH_CACHE_SCHEMA_VERSION_COLUMNS = (
    ("version", "INT", 0, 1),
    ("description", "TEXT", 0, 0),
)
_HASH_CACHE_FILES_COLUMNS = (
    ("path", "TEXT", 0, 1),
    ("device", "TEXT", 1, 0),
    ("file_id", "TEXT", 1, 0),
    ("size", "INTEGER", 1, 0),
    ("mtime_ns", "INTEGER", 1, 0),
    ("ctime_ns", "BLOB", 1, 0),
    ("algorithm", "TEXT", 1, 0),
    ("entry_dt", "DATETIME", 0, 0),
    ("digest", "BLOB", 0, 0),
    ("digest_partial", "BLOB", 0, 0),
    ("digest_samples", "BLOB", 0, 0),
)
_HASH_CACHE_SCHEMA_VERSION_SQL = "CREATE TABLE SCHEMA_VERSION (VERSION INT PRIMARY KEY, DESCRIPTION TEXT)"
_HASH_CACHE_FILES_SQL = (
    "CREATE TABLE FILES (PATH TEXT PRIMARY KEY, DEVICE TEXT NOT NULL, FILE_ID TEXT NOT NULL, "
    "SIZE INTEGER NOT NULL, MTIME_NS INTEGER NOT NULL, CTIME_NS BLOB NOT NULL, "
    "ALGORITHM TEXT NOT NULL, ENTRY_DT DATETIME, DIGEST BLOB, DIGEST_PARTIAL BLOB, DIGEST_SAMPLES BLOB)"
)
_HASH_CACHE_SCHEMA_OBJECTS = frozenset(
    {
        ("table", "files", "files", "text"),
        ("table", "schema_version", "schema_version", "text"),
        ("index", "sqlite_autoindex_files_1", "files", "null"),
        ("index", "sqlite_autoindex_schema_version_1", "schema_version", "null"),
    }
)
_HASH_CACHE_MAX_SCHEMA_NAME_BYTES = 128
_HASH_CACHE_MAX_DESCRIPTION_BYTES = 256
_HASH_CACHE_MAX_SCHEMA_SQL_BYTES = 4096
_DARWIN_STANDARD_ROOT_ALIASES = {
    "etc": Path("/private/etc"),
    "tmp": Path("/private/tmp"),
    "var": Path("/private/var"),
}


@dataclass(frozen=True)
class _HashCacheInspection:
    application_id: int
    current_version: int
    version_rows: tuple


def _is_reparse_point(file_stat) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _same_file_identity(first, second) -> bool:
    return (
        int(first.st_dev),
        int(first.st_ino),
    ) == (
        int(second.st_dev),
        int(second.st_ino),
    )


def _authenticated_darwin_root_alias(alias: Path, alias_stat) -> Union[Path, None]:
    """Return the fixed physical target for one immutable macOS root alias."""

    if sys.platform != "darwin" or alias.parent != Path(alias.anchor):
        return None
    expected = _DARWIN_STANDARD_ROOT_ALIASES.get(alias.name)
    if expected is None or not stat.S_ISLNK(alias_stat.st_mode):
        return None
    try:
        root_stat = os.lstat(alias.parent)
        target_text = os.readlink(alias)
        target = Path(os.path.abspath(os.path.join(os.fspath(alias.parent), target_text)))
        target_stat = os.lstat(expected)
        followed_stat = os.stat(alias)
    except OSError:
        return None
    if (
        target != expected
        or int(getattr(root_stat, "st_uid", -1)) != 0
        or stat.S_IMODE(root_stat.st_mode) & 0o022
        or not stat.S_ISDIR(root_stat.st_mode)
        or int(getattr(alias_stat, "st_uid", -1)) != 0
        or int(getattr(target_stat, "st_uid", -1)) != 0
        or not stat.S_ISDIR(target_stat.st_mode)
        or stat.S_ISLNK(target_stat.st_mode)
        or _is_reparse_point(target_stat)
        or not _same_file_identity(target_stat, followed_stat)
    ):
        return None
    try:
        if Path(os.path.realpath(os.fspath(alias))) != expected:
            return None
    except OSError:
        return None
    return expected


def _require_plain_hash_cache_parent(parent: Path) -> Path:
    parent = Path(os.path.abspath(os.fspath(parent)))
    parts = parent.parts[1:] if parent.anchor else parent.parts
    authenticated_alias = None
    lexical_alias = None
    if parts:
        first = Path(parent.anchor) / parts[0]
        try:
            first_stat = os.lstat(first)
        except OSError as error:
            raise HashCacheSafetyError("Hash cache parent component is unavailable: '{}'".format(first)) from error
        authenticated_alias = _authenticated_darwin_root_alias(first, first_stat)
        if authenticated_alias is not None:
            lexical_alias = first
            parent = authenticated_alias.joinpath(*parts[1:])

    current = Path(parent.anchor)
    parts = parent.parts[1:] if parent.anchor else parent.parts
    for part in parts:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except OSError as error:
            raise HashCacheSafetyError("Hash cache parent component is unavailable: '{}'".format(current)) from error
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or _is_reparse_point(current_stat)
            or not stat.S_ISDIR(current_stat.st_mode)
        ):
            raise HashCacheSafetyError("Hash cache parent components must be plain directories: '{}'".format(current))
    if authenticated_alias is not None:
        assert lexical_alias is not None
        # Re-authenticate after validating the physical chain. The caller will
        # use only ``parent`` from this point onward.
        try:
            lexical_stat = os.lstat(lexical_alias)
        except OSError as error:
            raise HashCacheSafetyError(
                "Hash cache platform alias changed while it was being authenticated: '{}'".format(lexical_alias)
            ) from error
        if _authenticated_darwin_root_alias(lexical_alias, lexical_stat) != authenticated_alias:
            raise HashCacheSafetyError(
                "Hash cache platform alias changed while it was being authenticated: '{}'".format(lexical_alias)
            )
    return parent


def _require_no_hash_cache_sidecars(path: Path) -> None:
    for suffix in _HASH_CACHE_SIDECAR_SUFFIXES:
        sidecar = Path("{}{}".format(path, suffix))
        if os.path.lexists(sidecar):
            raise HashCacheSafetyError("Hash cache SQLite sidecar already exists: '{}'".format(sidecar))


def _read_hash_cache_header(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    remaining = _SQLITE_DATABASE_HEADER_SIZE
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _validate_owned_hash_cache_header(descriptor: int, opened_stat, path: Path) -> None:
    file_size = int(opened_stat.st_size)
    if file_size < _SQLITE_DATABASE_HEADER_SIZE or file_size > _HASH_CACHE_MAX_FILE_BYTES:
        raise HashCacheSafetyError("Existing hash cache has an unsupported file size: '{}'".format(path))
    header = _read_hash_cache_header(descriptor)
    if len(header) != _SQLITE_DATABASE_HEADER_SIZE or header[: len(_SQLITE_HEADER)] != _SQLITE_HEADER:
        raise HashCacheSafetyError("Existing hash cache does not have a complete SQLite header: '{}'".format(path))
    raw_page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise HashCacheSafetyError("Existing hash cache has an invalid SQLite page size: '{}'".format(path))
    if file_size < page_size or file_size % page_size != 0:
        raise HashCacheSafetyError("Existing hash cache has an inconsistent SQLite file size: '{}'".format(path))
    if header[18] != 1 or header[19] != 1:
        raise HashCacheSafetyError(
            "Existing hash cache is not in the supported rollback-journal format: '{}'".format(path)
        )
    application_id = int.from_bytes(header[68:72], "big")
    if application_id != _HASH_CACHE_APPLICATION_ID:
        raise HashCacheSafetyError("Existing SQLite file does not carry the hash-cache owner marker: '{}'".format(path))


def _open_hash_cache_guard(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HashCacheSafetyError(
            "Hash cache could not be opened without following links: '{}'".format(path)
        ) from error
    try:
        opened_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or _is_reparse_point(path_stat)
            or not stat.S_ISREG(path_stat.st_mode)
            or not stat.S_ISREG(opened_stat.st_mode)
            or not _same_file_identity(path_stat, opened_stat)
        ):
            raise HashCacheSafetyError("Hash cache must be one stable plain regular file: '{}'".format(path))
        if int(getattr(path_stat, "st_nlink", 0)) != 1:
            raise HashCacheSafetyError("Hash cache must have exactly one filesystem link: '{}'".format(path))
        _validate_owned_hash_cache_header(descriptor, opened_stat, path)
    except HashCacheSafetyError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise HashCacheSafetyError(
            "Hash cache changed while its guarded handle was opened: '{}'".format(path)
        ) from error
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened_stat


def _assert_hash_cache_path_identity(path: Path, expected_stat) -> None:
    try:
        current_stat = os.lstat(path)
    except OSError as error:
        raise HashCacheSafetyError("Hash cache path became unavailable: '{}'".format(path)) from error
    if (
        stat.S_ISLNK(current_stat.st_mode)
        or _is_reparse_point(current_stat)
        or not stat.S_ISREG(current_stat.st_mode)
        or not _same_file_identity(expected_stat, current_stat)
        or int(getattr(current_stat, "st_nlink", 0)) != 1
    ):
        raise HashCacheSafetyError("Hash cache path changed while it was being opened: '{}'".format(path))


def _configure_hash_cache_connection(connection, *, query_only: bool) -> None:
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        trusted_schema = connection.execute("PRAGMA trusted_schema").fetchone()
        if trusted_schema is None or int(trusted_schema[0]) != 0:
            raise HashCacheSafetyError("SQLite trusted_schema could not be disabled for the hash cache")
        connection.execute("PRAGMA query_only = {}".format("ON" if query_only else "OFF"))
        query_only_row = connection.execute("PRAGMA query_only").fetchone()
        expected = 1 if query_only else 0
        if query_only_row is None or int(query_only_row[0]) != expected:
            raise HashCacheSafetyError("SQLite query_only mode could not be configured for the hash cache")
    except sqlite3.Error as error:
        raise HashCacheSafetyError("Hash cache SQLite safety settings could not be configured") from error


def _apply_hash_cache_runtime_limits(connection) -> None:
    """Apply extra parser/value bounds when the CPython sqlite wrapper exposes sqlite3_limit()."""

    if not hasattr(connection, "setlimit"):
        return
    limits = (
        ("SQLITE_LIMIT_LENGTH", 1024 * 1024),
        ("SQLITE_LIMIT_SQL_LENGTH", 64 * 1024),
        ("SQLITE_LIMIT_COLUMN", 64),
        ("SQLITE_LIMIT_EXPR_DEPTH", 64),
        ("SQLITE_LIMIT_COMPOUND_SELECT", 16),
        ("SQLITE_LIMIT_VDBE_OP", 100_000),
        ("SQLITE_LIMIT_FUNCTION_ARG", 32),
        ("SQLITE_LIMIT_ATTACHED", 0),
        ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 4096),
        ("SQLITE_LIMIT_VARIABLE_NUMBER", 128),
        ("SQLITE_LIMIT_TRIGGER_DEPTH", 0),
        ("SQLITE_LIMIT_WORKER_THREADS", 0),
    )
    try:
        for constant_name, limit in limits:
            category = getattr(sqlite3, constant_name)
            connection.setlimit(category, limit)
            if connection.getlimit(category) > limit:
                raise HashCacheSafetyError(
                    "SQLite runtime limit {} could not be lowered for the hash cache".format(constant_name)
                )
    except HashCacheSafetyError:
        raise
    except (AttributeError, sqlite3.Error) as error:
        raise HashCacheSafetyError("Hash cache SQLite runtime limits could not be configured") from error


def _hash_cache_schema_objects(connection):
    limit = len(_HASH_CACHE_SCHEMA_OBJECTS) + 1
    rows = connection.execute(
        """
        SELECT
            type,
            CASE
                WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= ?
                THEN name
            END,
            CASE
                WHEN typeof(tbl_name) = 'text' AND length(CAST(tbl_name AS BLOB)) <= ?
                THEN tbl_name
            END,
            typeof(sql)
        FROM sqlite_schema
        LIMIT ?
        """,
        (
            _HASH_CACHE_MAX_SCHEMA_NAME_BYTES,
            _HASH_CACHE_MAX_SCHEMA_NAME_BYTES,
            limit,
        ),
    ).fetchall()
    if len(rows) != len(_HASH_CACHE_SCHEMA_OBJECTS):
        return None
    objects = set()
    for object_type, name, table_name, sql_type in rows:
        if name is None or table_name is None:
            return None
        objects.add((str(object_type), str(name), str(table_name), str(sql_type)))
    return frozenset(objects)


def _hash_cache_table_columns(connection, table_name: str, expected_count: int):
    rows = connection.execute(
        """
        SELECT
            cid,
            CASE
                WHEN typeof(name) = 'text' AND length(CAST(name AS BLOB)) <= ?
                THEN name
            END,
            CASE
                WHEN typeof(type) = 'text' AND length(CAST(type AS BLOB)) <= ?
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
            _HASH_CACHE_MAX_SCHEMA_NAME_BYTES,
            _HASH_CACHE_MAX_SCHEMA_NAME_BYTES,
            table_name,
            expected_count + 1,
        ),
    ).fetchall()
    if len(rows) != expected_count:
        return None
    columns = []
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
        columns.append((str(name), str(declared_type), int(not_null), int(primary_key)))
    return tuple(columns)


def _normalize_hash_cache_schema_sql(sql: str) -> str:
    normalized = " ".join(sql.split()).upper()
    return normalized.replace("( ", "(").replace(" )", ")")


def _hash_cache_table_sql(connection, table_name: str):
    row = connection.execute(
        """
        SELECT CASE
            WHEN typeof(sql) = 'text' AND length(CAST(sql AS BLOB)) <= ?
            THEN sql
        END
        FROM sqlite_schema
        WHERE type = 'table' AND name = ?
        LIMIT 2
        """,
        (_HASH_CACHE_MAX_SCHEMA_SQL_BYTES, table_name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _normalize_hash_cache_schema_sql(str(row[0]))


def _hash_cache_version_rows(connection):
    rows = connection.execute(
        """
        SELECT
            typeof(version),
            CASE WHEN typeof(version) = 'integer' THEN version END,
            typeof(description),
            CASE
                WHEN typeof(description) = 'text'
                    AND length(CAST(description AS BLOB)) <= ?
                THEN description
            END
        FROM schema_version
        LIMIT 2
        """,
        (_HASH_CACHE_MAX_DESCRIPTION_BYTES,),
    ).fetchall()
    if len(rows) != 1:
        return None
    version_type, version, description_type, description = rows[0]
    if (
        version_type != "integer"
        or int(version) != _HASH_CACHE_SCHEMA_VERSION
        or description_type != "text"
        or description != _HASH_CACHE_SCHEMA_DESCRIPTION
    ):
        return None
    return _HASH_CACHE_SCHEMA_VERSION, ((_HASH_CACHE_SCHEMA_VERSION, _HASH_CACHE_SCHEMA_DESCRIPTION),)


def _inspect_hash_cache_connection(connection, path: Path) -> _HashCacheInspection:
    try:
        application_id_row = connection.execute("PRAGMA application_id").fetchone()
        if application_id_row is None:
            raise HashCacheSafetyError("Existing hash cache has no SQLite application identifier: '{}'".format(path))
        application_id = int(application_id_row[0])
        if application_id != _HASH_CACHE_APPLICATION_ID:
            raise HashCacheSafetyError(
                "Existing SQLite file does not carry the hash-cache owner marker: '{}'".format(path)
            )

        if _hash_cache_schema_objects(connection) != _HASH_CACHE_SCHEMA_OBJECTS:
            raise HashCacheSafetyError("Existing SQLite file is not an owned hash cache: '{}'".format(path))
        schema_columns = _hash_cache_table_columns(
            connection,
            "schema_version",
            len(_HASH_CACHE_SCHEMA_VERSION_COLUMNS),
        )
        if schema_columns != _HASH_CACHE_SCHEMA_VERSION_COLUMNS:
            raise HashCacheSafetyError("Existing hash cache has an unsupported owner schema: '{}'".format(path))

        version_result = _hash_cache_version_rows(connection)
        if version_result is None:
            raise HashCacheSafetyError("Existing hash cache has unsupported schema history: '{}'".format(path))
        current_version, version_rows = version_result
        files_columns = _hash_cache_table_columns(
            connection,
            "files",
            len(_HASH_CACHE_FILES_COLUMNS),
        )
        if files_columns != _HASH_CACHE_FILES_COLUMNS:
            raise HashCacheSafetyError("Existing hash cache has an unsupported files schema: '{}'".format(path))
        if _hash_cache_table_sql(connection, "schema_version") != _HASH_CACHE_SCHEMA_VERSION_SQL:
            raise HashCacheSafetyError("Existing hash cache has unexpected schema-version SQL: '{}'".format(path))
        if _hash_cache_table_sql(connection, "files") != _HASH_CACHE_FILES_SQL:
            raise HashCacheSafetyError("Existing hash cache has unexpected files SQL: '{}'".format(path))
        return _HashCacheInspection(
            application_id=application_id,
            current_version=current_version,
            version_rows=version_rows,
        )
    except HashCacheSafetyError:
        raise
    except (sqlite3.Error, UnicodeError, ValueError, TypeError, OverflowError) as error:
        raise HashCacheSafetyError("Existing hash cache schema could not be verified: '{}'".format(path)) from error


def _inspect_empty_hash_cache_connection(connection, path: Path) -> None:
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise HashCacheSafetyError("New hash cache reservation could not be verified: '{}'".format(path)) from error
    if application_id != 0 or user_version != 0 or objects is not None:
        raise HashCacheSafetyError("New hash cache reservation unexpectedly contains SQLite state: '{}'".format(path))


def _validate_owned_hash_cache(path: Path, expected_stat) -> _HashCacheInspection:
    try:
        _assert_hash_cache_path_identity(path, expected_stat)
        connection = sqlite3.connect(
            "{}?mode=ro".format(path.resolve(strict=True).as_uri()),
            uri=True,
        )
    except (OSError, sqlite3.Error) as error:
        raise HashCacheSafetyError("Existing hash cache could not be opened read-only: '{}'".format(path)) from error
    try:
        _apply_hash_cache_runtime_limits(connection)
        _configure_hash_cache_connection(connection, query_only=True)
        _assert_hash_cache_path_identity(path, expected_stat)
        inspection = _inspect_hash_cache_connection(connection, path)
        _assert_hash_cache_path_identity(path, expected_stat)
        return inspection
    finally:
        connection.close()


class FilesDB:
    schema_version = _HASH_CACHE_SCHEMA_VERSION
    schema_version_description = _HASH_CACHE_SCHEMA_DESCRIPTION
    digest_keys = frozenset({"digest", "digest_partial", "digest_samples"})

    create_table_query = """CREATE TABLE IF NOT EXISTS files (
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
        digest_samples BLOB
    )"""
    drop_table_query = "DROP TABLE IF EXISTS files;"
    select_query = """SELECT {key} FROM files
        WHERE path=:path AND device=:device AND file_id=:file_id AND size=:size
        AND mtime_ns=:mtime_ns AND ctime_ns=:ctime_ns AND algorithm=:algorithm"""
    select_query_ignore_mtime = """SELECT {key} FROM files
        WHERE path=:path AND device=:device AND file_id=:file_id AND size=:size
        AND ctime_ns=:ctime_ns AND algorithm=:algorithm"""
    upsert_generation_query = """
        INSERT INTO files (
            path, device, file_id, size, mtime_ns, ctime_ns, algorithm, entry_dt,
            digest, digest_partial, digest_samples
        )
        VALUES (
            :path, :device, :file_id, :size, :mtime_ns, :ctime_ns, :algorithm, datetime('now'),
            NULL, NULL, NULL
        )
        ON CONFLICT(path) DO UPDATE SET
            digest=CASE
                WHEN files.device=:device AND files.file_id=:file_id AND files.size=:size
                    AND files.mtime_ns=:mtime_ns AND files.ctime_ns=:ctime_ns
                    AND files.algorithm=:algorithm
                THEN files.digest ELSE NULL END,
            digest_partial=CASE
                WHEN files.device=:device AND files.file_id=:file_id AND files.size=:size
                    AND files.mtime_ns=:mtime_ns AND files.ctime_ns=:ctime_ns
                    AND files.algorithm=:algorithm
                THEN files.digest_partial ELSE NULL END,
            digest_samples=CASE
                WHEN files.device=:device AND files.file_id=:file_id AND files.size=:size
                    AND files.mtime_ns=:mtime_ns AND files.ctime_ns=:ctime_ns
                    AND files.algorithm=:algorithm
                THEN files.digest_samples ELSE NULL END,
            device=:device,
            file_id=:file_id,
            size=:size,
            mtime_ns=:mtime_ns,
            ctime_ns=:ctime_ns,
            algorithm=:algorithm,
            entry_dt=datetime('now')
    """
    update_digest_query = """UPDATE files SET {key}=:value, entry_dt=datetime('now')
        WHERE path=:path AND device=:device AND file_id=:file_id AND size=:size
        AND mtime_ns=:mtime_ns AND ctime_ns=:ctime_ns AND algorithm=:algorithm"""

    ignore_mtime = False

    def __init__(self):
        self.conn = None
        self.lock = None

    def connect(self, path: Union[AnyStr, os.PathLike]) -> None:
        # Keep SQLite's transactional isolation enabled on every platform. A
        # generation upsert and its digest update must commit or roll back as a
        # unit so no caller can observe mixed-generation cache columns.
        path = Path(os.path.abspath(os.fspath(path)))
        path = _require_plain_hash_cache_parent(path.parent).joinpath(path.name)
        _require_no_hash_cache_sidecars(path)
        guard = None
        guard_stat = None
        existing_inspection = None
        new_database = False
        try:
            if os.path.lexists(path):
                guard, guard_stat = _open_hash_cache_guard(path)
                existing_inspection = _validate_owned_hash_cache(path, guard_stat)
            else:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_BINARY", 0)
                try:
                    guard = os.open(path, flags, 0o600)
                    guard_stat = os.fstat(guard)
                    new_database = True
                except FileExistsError:
                    guard, guard_stat = _open_hash_cache_guard(path)
                    existing_inspection = _validate_owned_hash_cache(path, guard_stat)

            self.conn = sqlite3.connect(str(path), check_same_thread=False)
            self.lock = Lock()
            _apply_hash_cache_runtime_limits(self.conn)
            _configure_hash_cache_connection(self.conn, query_only=True)
            _assert_hash_cache_path_identity(path, guard_stat)
            if new_database:
                _inspect_empty_hash_cache_connection(self.conn, path)
            else:
                writable_inspection = _inspect_hash_cache_connection(self.conn, path)
                if writable_inspection != existing_inspection:
                    raise HashCacheSafetyError(
                        "Hash cache ownership changed between read-only inspection and writable reopen: "
                        "'{}'".format(path)
                    )
            _assert_hash_cache_path_identity(path, guard_stat)
            _configure_hash_cache_connection(self.conn, query_only=False)
            if new_database:
                self._initialize_new_database(path=path, expected_stat=guard_stat)
            _assert_hash_cache_path_identity(path, guard_stat)
        except BaseException:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            self.lock = None
            raise
        finally:
            if guard is not None:
                os.close(guard)

    def _initialize_new_database(self, *, path: Path, expected_stat) -> None:
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                _assert_hash_cache_path_identity(path, expected_stat)
                _inspect_empty_hash_cache_connection(self.conn, path)
                self.conn.execute("CREATE TABLE schema_version (version int PRIMARY KEY, description TEXT)")
                self.conn.execute(
                    "INSERT INTO schema_version VALUES (:version, :description)",
                    {"version": self.schema_version, "description": self.schema_version_description},
                )
                self.conn.execute(self.create_table_query)
                self.conn.execute("PRAGMA application_id = {}".format(_HASH_CACHE_APPLICATION_ID))
                initialized = _inspect_hash_cache_connection(self.conn, path)
                if (
                    initialized.application_id != _HASH_CACHE_APPLICATION_ID
                    or initialized.current_version != self.schema_version
                ):
                    raise HashCacheSafetyError("Hash cache initialization verification failed: '{}'".format(path))
                _assert_hash_cache_path_identity(path, expected_stat)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def clear(self) -> None:
        with self.lock, self.conn as conn:
            conn.execute(self.drop_table_query)
            conn.execute(self.create_table_query)

    @classmethod
    def _validate_key(cls, key: str) -> None:
        if key not in cls.digest_keys:
            raise ValueError(f"Invalid digest cache key: {key}")

    def get_strict(self, path: Path, key: str) -> Union[bytes, None]:
        """Read one cache value without converting cache/read errors to misses."""

        self._validate_key(key)
        # A cache is optional for direct engine use. Once connected, however,
        # every database/read error is material and strict callers must see it.
        if self.conn is None:
            return None
        before = _snapshot_path(path)
        params = {"path": str(path), "algorithm": HASH_ALGORITHM, **before.as_sql_params()}
        with self.lock, self.conn as conn:
            if self.ignore_mtime:
                cursor = conn.execute(self.select_query_ignore_mtime.format(key=key), params)
            else:
                cursor = conn.execute(self.select_query.format(key=key), params)
            result = cursor.fetchone()
            cursor.close()
        after = _snapshot_path(path)
        _ensure_unchanged(before, after, path)
        if result:
            return result[0]
        return None

    def get(self, path: Path, key: str) -> Union[bytes, None]:
        self._validate_key(key)
        try:
            return self.get_strict(path, key)
        except Exception as ex:
            logging.warning("Couldn't get %s for %s: %s", key, path, ex)
        return None

    def put_strict(self, path: Path, key: str, value: Any, snapshot: FileSnapshot = None) -> None:
        """Store one cache value without suppressing transaction failures."""

        self._validate_key(key)
        if self.conn is None:
            return
        current = _snapshot_path(path)
        if snapshot is not None:
            if not snapshot.same_content_generation(current):
                raise FileChangedError(f"File changed before digest could be cached: {path}")
        snapshot = current
        params = {
            "path": str(path),
            "algorithm": HASH_ALGORITHM,
            "value": value,
            **snapshot.as_sql_params(),
        }
        with self.lock, self.conn as conn:
            conn.execute(self.upsert_generation_query, params)
            cursor = conn.execute(self.update_digest_query.format(key=key), params)
            if cursor.rowcount != 1:
                raise FileChangedError(f"File changed before digest could be cached: {path}")
        after = _snapshot_path(path)
        _ensure_unchanged(snapshot, after, path)

    def put(self, path: Path, key: str, value: Any, snapshot: FileSnapshot = None) -> None:
        self._validate_key(key)
        try:
            self.put_strict(path, key, value, snapshot)
        except FileChangedError:
            raise
        except Exception as ex:
            logging.warning("Couldn't put %s for %s: %s", key, path, ex)

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


filesdb = FilesDB()  # Singleton


class File:
    """Represents a file and holds metadata to be used for scanning."""

    INITIAL_INFO = {"size": 0, "mtime": 0, "digest": None, "digest_partial": None, "digest_samples": None}
    # Slots for File make us save quite a bit of memory. In a memory test I've made with a lot of
    # files, I saved 35% memory usage with "unread" files (no _read_info() call) and gains become
    # even greater when we take into account read attributes (70%!). Yeah, it's worth it.
    __slots__ = (
        "path",
        "unicode_path",
        "is_ref",
        "comparison_pool",
        "words",
        "_exact_scan_snapshot",
        "_review_scan_snapshot",
        "_strict_digest_snapshots",
    ) + tuple(INITIAL_INFO.keys())

    def __init__(self, path):
        for attrname in self.INITIAL_INFO:
            setattr(self, attrname, NOT_SET)
        self._exact_scan_snapshot = None
        self._review_scan_snapshot = None
        self._strict_digest_snapshots = {}
        self.comparison_pool = "incoming"
        if type(path) is os.DirEntry:
            self.path = Path(path.path)
            self.size = nonone(path.stat().st_size, 0)
            self.mtime = nonone(path.stat().st_mtime, 0)
        else:
            self.path = path
        if self.path:
            self.unicode_path = str(self.path)

    def __repr__(self):
        return f"<{self.__class__.__name__} {str(self.path)}>"

    def __getattribute__(self, attrname):
        result = object.__getattribute__(self, attrname)
        if result is NOT_SET:
            try:
                self._read_info(attrname)
            except Exception as e:
                logging.warning("An error '%s' was raised while decoding '%s'", e, repr(self.path))
            result = object.__getattribute__(self, attrname)
            if result is NOT_SET:
                result = self.INITIAL_INFO[attrname]
        return result

    def _calc_digest_with_snapshot(self, stop_check=None):
        with _open_readonly_no_follow(self.path) as fp:
            before = _snapshot_handle(fp, self.path)
            file_hash = hasher()
            bytes_read = 0
            _raise_if_scan_stopped(stop_check)
            filedata = fp.read(CHUNK_SIZE)
            while filedata:
                file_hash.update(filedata)
                bytes_read += len(filedata)
                _raise_if_scan_stopped(stop_check)
                filedata = fp.read(CHUNK_SIZE)
            after = _snapshot_handle(fp, self.path)
            _ensure_unchanged(before, after, self.path)
            if bytes_read != before.size:
                raise FileChangedError(f"File size changed while being hashed: {self.path}")
            return file_hash.digest(), before

    def _calc_digest(self):
        # type: () -> bytes
        return self._calc_digest_with_snapshot()[0]

    def _calc_digest_partial_with_snapshot(self, stop_check=None):
        with _open_readonly_no_follow(self.path) as fp:
            before = _snapshot_handle(fp, self.path)
            _raise_if_scan_stopped(stop_check)
            fp.seek(PARTIAL_OFFSET_SIZE[0])
            partial_data = fp.read(PARTIAL_OFFSET_SIZE[1])
            _raise_if_scan_stopped(stop_check)
            after = _snapshot_handle(fp, self.path)
            _ensure_unchanged(before, after, self.path)
            return hasher(partial_data).digest(), before

    def _calc_digest_partial(self):
        # type: () -> bytes
        return self._calc_digest_partial_with_snapshot()[0]

    def _calc_digest_samples_with_snapshot(self, stop_check=None):
        with _open_readonly_no_follow(self.path) as fp:
            before = _snapshot_handle(fp, self.path)
            size = before.size
            _raise_if_scan_stopped(stop_check)
            # Chunk at 25% of the file
            fp.seek(floor(size * 25 / 100), 0)
            file_data = fp.read(CHUNK_SIZE)
            file_hash = hasher(file_data)
            _raise_if_scan_stopped(stop_check)

            # Chunk at 60% of the file
            fp.seek(floor(size * 60 / 100), 0)
            file_data = fp.read(CHUNK_SIZE)
            file_hash.update(file_data)
            _raise_if_scan_stopped(stop_check)

            # Last chunk of the file
            fp.seek(-CHUNK_SIZE, 2)
            file_data = fp.read(CHUNK_SIZE)
            file_hash.update(file_data)
            _raise_if_scan_stopped(stop_check)
            after = _snapshot_handle(fp, self.path)
            _ensure_unchanged(before, after, self.path)
            return file_hash.digest(), before

    def _calc_digest_samples(self) -> bytes:
        return self._calc_digest_samples_with_snapshot()[0]

    def read_info_strict(self, field):
        """Read exact-scan metadata without the UI getter's tolerant fallback.

        Normal metadata rendering deliberately substitutes defaults when a
        decoder or filesystem read fails. Exact scans cannot do that: a missing
        candidate/full digest would silently reduce coverage and could leave a
        scan looking complete. Existing values are accepted only when the
        service adapter supplied a matching stable generation snapshot.
        """

        if field not in self.INITIAL_INFO:
            raise ValueError("Unsupported strict file-info field: {}".format(field))
        if field in {"size", "mtime"}:
            snapshot = _snapshot_path(self.path)
            if self._exact_scan_snapshot is not None:
                _ensure_unchanged(
                    self._exact_scan_snapshot,
                    snapshot,
                    self.path,
                )
            self.size = snapshot.size
            self.mtime = snapshot.mtime_ns / 1_000_000_000
            return object.__getattribute__(self, field)
        if field in FilesDB.digest_keys:
            if self._exact_scan_snapshot is None:
                self.begin_exact_scan()
            baseline = self._exact_scan_snapshot
            generation = self._strict_digest_snapshots.get(field)
            result = object.__getattribute__(self, field)
            if (
                result is not NOT_SET
                and result is not None
                and generation is not None
                and generation.same_content_generation(baseline)
            ):
                current = _snapshot_path(self.path)
                _ensure_unchanged(baseline, current, self.path)
                return result
            # Values populated by the tolerant UI getter carry no strict
            # generation proof and must not influence exact candidate
            # filtering.
            setattr(self, field, NOT_SET)
        result = object.__getattribute__(self, field)
        if result is NOT_SET or (field in FilesDB.digest_keys and result is None):
            self._read_info(field, strict=True)
            result = object.__getattribute__(self, field)
        if result is NOT_SET or (field in FilesDB.digest_keys and result is None):
            raise OSError(
                "Exact-scan metadata {!r} is unavailable for {}".format(
                    field,
                    self.path,
                )
            )
        if field in FilesDB.digest_keys:
            current = _snapshot_path(self.path)
            _ensure_unchanged(self._exact_scan_snapshot, current, self.path)
            self._strict_digest_snapshots[field] = current
        return result

    def begin_exact_scan(self):
        """Bind all strict exact reads to one fresh content generation."""

        snapshot = _snapshot_path(self.path)
        if self._review_scan_snapshot is None:
            self._review_scan_snapshot = snapshot
        else:
            _ensure_unchanged(self._review_scan_snapshot, snapshot, self.path)
        self._exact_scan_snapshot = snapshot
        self.size = snapshot.size
        self.mtime = snapshot.mtime_ns / 1_000_000_000
        for field in FilesDB.digest_keys:
            generation = self._strict_digest_snapshots.get(field)
            if generation is None or not generation.same_content_generation(snapshot):
                setattr(self, field, NOT_SET)
                self._strict_digest_snapshots.pop(field, None)
        return snapshot.size

    def begin_review_scan(self, stop_check=None, progress_callback=None):
        """Bind later organizer actions to metadata and bytes entering this scan."""

        snapshot, fast_digest = _snapshot_path_with_review_digests(
            self.path,
            stop_check=stop_check,
            progress_callback=progress_callback,
            include_fast_digest=True,
        )
        self._review_scan_snapshot = snapshot
        self.size = snapshot.size
        self.mtime = snapshot.mtime_ns / 1_000_000_000
        if fast_digest is None:
            raise FileChangedError("The scan did not capture a fast content digest for: {}".format(self.path))
        for digest_field in FilesDB.digest_keys:
            setattr(self, digest_field, fast_digest)
            self._strict_digest_snapshots[digest_field] = snapshot
        return snapshot

    def validate_review_scan(self):
        """Metadata-check and return the scan-bound full-content proof.

        The organizer executor consumes the returned SHA-256 proof while it
        copies or holds the source.  This method intentionally does not reread
        the full file on every eligibility/UI check.
        """

        if self._review_scan_snapshot is None:
            raise FileChangedError("The scan did not capture an organizer baseline for: {}".format(self.path))
        if (
            self._review_scan_snapshot.content_digest_algorithm != REVIEW_CONTENT_DIGEST_ALGORITHM
            or self._review_scan_snapshot.content_digest is None
        ):
            raise FileChangedError("The scan did not capture a content proof for: {}".format(self.path))
        current = _snapshot_path(self.path)
        _ensure_unchanged(self._review_scan_snapshot, current, self.path)
        return self._review_scan_snapshot

    def validate_review_scan_content(self, stop_check=None, progress_callback=None):
        """Reread bytes once and reject same-tick, same-size scan mutations."""

        baseline = self.validate_review_scan()
        current = _snapshot_path_with_content_digest(
            self.path,
            stop_check=stop_check,
            progress_callback=progress_callback,
        )
        if not baseline.same_reviewed_content(current):
            raise FileChangedError("File content changed while it was being reviewed: {}".format(self.path))
        return baseline

    def validate_exact_scan(self):
        """Fail if the path no longer names the generation this scan began on."""

        if self._exact_scan_snapshot is None:
            raise FileChangedError("Exact scan did not capture a baseline for: {}".format(self.path))
        current = _snapshot_path(self.path)
        _ensure_unchanged(self._exact_scan_snapshot, current, self.path)
        return current

    def prime_exact_digest(self, field, digest, snapshot):
        """Install a service-computed digest with its stable generation proof."""

        if field not in FilesDB.digest_keys:
            raise ValueError("Invalid exact digest field: {}".format(field))
        if digest is None:
            raise ValueError("Exact digest must not be None")
        current = _snapshot_path(self.path)
        _ensure_unchanged(snapshot, current, self.path)
        self._exact_scan_snapshot = snapshot
        if self._review_scan_snapshot is None:
            self._review_scan_snapshot = snapshot
        else:
            _ensure_unchanged(self._review_scan_snapshot, snapshot, self.path)
        setattr(self, field, digest)
        self._strict_digest_snapshots[field] = snapshot

    def prime_review_content_digest(self, digest, algorithm=REVIEW_CONTENT_DIGEST_ALGORITHM):
        """Bind trusted service evidence to the current organizer generation."""

        if self._review_scan_snapshot is None:
            raise FileChangedError("No organizer generation exists for: {}".format(self.path))
        current = _snapshot_path(self.path)
        _ensure_unchanged(self._review_scan_snapshot, current, self.path)
        self._review_scan_snapshot = self._review_scan_snapshot.with_content_digest(
            algorithm,
            digest,
        )
        return self._review_scan_snapshot

    def _read_info(self, field, strict=False):
        # print(f"_read_info({field}) for {self}")
        cache_get = filesdb.get_strict if strict else filesdb.get
        cache_put = filesdb.put_strict if strict else filesdb.put
        if field in ("size", "mtime"):
            stats = self.path.stat()
            self.size = nonone(stats.st_size, 0)
            self.mtime = nonone(stats.st_mtime, 0)
        elif field == "digest_partial":
            self.digest_partial = cache_get(self.path, "digest_partial")
            if self.digest_partial is None:
                # If file is smaller than partial requirements just use the full digest
                size = self.read_info_strict("size") if strict else self.size
                if size < PARTIAL_OFFSET_SIZE[0] + PARTIAL_OFFSET_SIZE[1]:
                    digest = self.read_info_strict("digest") if strict else self.digest
                    self.digest_partial = digest
                    return
                else:
                    digest, snapshot = self._calc_digest_partial_with_snapshot()
                cache_put(self.path, "digest_partial", digest, snapshot)
                self.digest_partial = digest
        elif field == "digest":
            self.digest = cache_get(self.path, "digest")
            if self.digest is None:
                digest, snapshot = self._calc_digest_with_snapshot()
                cache_put(self.path, "digest", digest, snapshot)
                self.digest = digest
        elif field == "digest_samples":
            size = self.read_info_strict("size") if strict else self.size
            # Might as well hash such small files entirely.
            if size <= MIN_FILE_SIZE:
                self.digest_samples = self.read_info_strict("digest") if strict else self.digest
                return
            self.digest_samples = cache_get(self.path, "digest_samples")
            if self.digest_samples is None:
                digest, snapshot = self._calc_digest_samples_with_snapshot()
                cache_put(self.path, "digest_samples", digest, snapshot)
                self.digest_samples = digest

    def _read_all_info(self, attrnames=None):
        """Cache all possible info.

        If `attrnames` is not None, caches only attrnames.
        """
        if attrnames is None:
            attrnames = self.INITIAL_INFO.keys()
        for attrname in attrnames:
            getattr(self, attrname)

    # --- Public
    @classmethod
    def can_handle(cls, path):
        """Returns whether this file wrapper class can handle ``path``."""
        return not path.is_symlink() and path.is_file()

    def exists(self) -> bool:
        """Safely check if the underlying file exists, treat error as non-existent"""
        try:
            return self.path.exists()
        except OSError as ex:
            logging.warning(f"Checking {self.path} raised: {ex}")
            return False

    @property
    def digest_algorithm(self):
        return HASH_ALGORITHM

    def compare_bytes(self, other):
        return self.compare_bytes_interruptible(other, None)

    def compare_bytes_with_sha256(self, other):
        """Compare bytes and return their SHA-256 in the same streaming pass."""

        return self.compare_bytes_interruptible(
            other,
            None,
            compute_sha256=True,
        )

    def compare_bytes_interruptible(self, other, stop_check, *, compute_sha256=False):
        """Compare two files through stable open handles.

        Returns evidence for equal files, ``None`` for unequal files, and raises
        :class:`FileChangedError` when either file changes during comparison.
        """
        with _open_readonly_no_follow(self.path) as first_fp, _open_readonly_no_follow(other.path) as second_fp:
            first_before = _snapshot_handle(first_fp, self.path)
            second_before = _snapshot_handle(second_fp, other.path)
            if first_before.size != second_before.size:
                return None
            bytes_compared = 0
            equal = True
            sha256 = hashlib.sha256() if compute_sha256 else None
            while True:
                _raise_if_scan_stopped(stop_check)
                first_data = first_fp.read(CHUNK_SIZE)
                second_data = second_fp.read(CHUNK_SIZE)
                if first_data != second_data:
                    equal = False
                    break
                if not first_data:
                    break
                if sha256 is not None:
                    sha256.update(first_data)
                bytes_compared += len(first_data)
            first_after = _snapshot_handle(first_fp, self.path)
            second_after = _snapshot_handle(second_fp, other.path)
            _ensure_unchanged(first_before, first_after, self.path)
            _ensure_unchanged(second_before, second_after, other.path)
            if not equal:
                return None
            if bytes_compared != first_before.size:
                raise FileChangedError("Unexpected end of file during byte comparison")
            return ByteComparisonEvidence(
                first_before,
                second_before,
                bytes_compared,
                sha256.digest() if sha256 is not None else None,
            )

    def rename(self, newname):
        if (
            not isinstance(newname, str)
            or not newname
            or newname in {".", ".."}
            or "\0" in newname
            or "/" in newname
            or "\\" in newname
            or ":" in newname
            or ntpath.isabs(newname)
            or posixpath.isabs(newname)
            or ntpath.splitdrive(newname)[0]
            or ntpath.basename(newname) != newname
            or posixpath.basename(newname) != newname
        ):
            raise InvalidPath(newname if isinstance(newname, str) else "")
        if newname == self.name:
            return
        destpath = self.path.parent.joinpath(newname)
        if destpath.parent != self.path.parent:
            raise InvalidPath(newname)
        try:
            # Import lazily to keep the low-level filesystem wrapper free from
            # an eager dependency on the transaction layer.
            from core.safe_action import platform_file_system

            platform_file_system().rename_no_replace(self.path, destpath)
        except FileExistsError:
            raise AlreadyExistsError(newname, self.path.parent)
        except OSError:
            raise OperationError(self)
        if not destpath.exists():
            raise OperationError(self)
        self.path = destpath

    def get_display_info(self, group, delta):
        """Returns a display-ready dict of dupe's data."""
        raise NotImplementedError()

    # --- Properties
    @property
    def extension(self):
        return get_file_ext(self.name)

    @property
    def name(self):
        return self.path.name

    @property
    def folder_path(self):
        return self.path.parent


class Folder(File):
    """A wrapper around a folder path.

    It has the size/digest info of a File, but its value is the sum of its subitems.
    """

    __slots__ = File.__slots__ + ("_subfolders",)

    def __init__(self, path):
        File.__init__(self, path)
        self.size = NOT_SET
        self._subfolders = None

    def _all_items(self):
        folders = self.subfolders
        files = get_files(self.path)
        return folders + files

    def _read_info(self, field):
        # print(f"_read_info({field}) for Folder {self}")
        if field in {"size", "mtime"}:
            size = sum((f.size for f in self._all_items()), 0)
            self.size = size
            stats = self.path.stat()
            self.mtime = nonone(stats.st_mtime, 0)
        elif field in {"digest", "digest_partial", "digest_samples"}:
            # What's sensitive here is that we must make sure that subfiles'
            # digest are always added up in the same order, but we also want a
            # different digest if a file gets moved in a different subdirectory.

            def get_dir_digest_concat():
                items = self._all_items()
                items.sort(key=lambda f: f.path)
                digests = [getattr(f, field) for f in items]
                return b"".join(digests)

            digest = hasher(get_dir_digest_concat()).digest()
            setattr(self, field, digest)

    @property
    def subfolders(self):
        if self._subfolders is None:
            with os.scandir(self.path) as iter:
                subfolders = [p for p in iter if not p.is_symlink() and p.is_dir()]
            self._subfolders = [self.__class__(p) for p in subfolders]
        return self._subfolders

    @classmethod
    def can_handle(cls, path):
        return not path.is_symlink() and path.is_dir()


def get_file(path, fileclasses=[File]):
    """Wraps ``path`` around its appropriate :class:`File` class.

    Whether a class is "appropriate" is decided by :meth:`File.can_handle`

    :param Path path: path to wrap
    :param fileclasses: List of candidate :class:`File` classes
    """
    for fileclass in fileclasses:
        if fileclass.can_handle(path):
            return fileclass(path)


def get_files(path, fileclasses=[File]):
    """Returns a list of :class:`File` for each file contained in ``path``.

    :param Path path: path to scan
    :param fileclasses: List of candidate :class:`File` classes
    """
    assert all(issubclass(fileclass, File) for fileclass in fileclasses)
    try:
        result = []
        with os.scandir(path) as iter:
            for item in iter:
                file = get_file(item, fileclasses=fileclasses)
                if file is not None:
                    result.append(file)
        return result
    except OSError:
        raise InvalidPath(path)
