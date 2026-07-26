# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded, read-only video-library similarity scanning.

The scanner deliberately separates cheap metadata candidate generation from expensive
fingerprinting.  Every loop has a configured upper bound; reaching one produces a partial
resource-limit receipt instead of silently claiming complete coverage.  Perceptual groups are
review-only and can never authorize deletion.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityVerdict,
    get_file_identity,
    get_file_identity_from_fd,
    same_physical_file,
)
from core.file_generation import (
    FileGenerationError,
    get_file_generation_token,
    get_file_generation_token_from_fd,
)
from core.reserved_paths import (
    RESERVED_INTERNAL_DIRECTORY_NAMES,
    is_reserved_internal_directory,
    is_within_reserved_internal_directory,
)
from core.safe_walk import WalkEventKind, is_reparse_point, walk_no_follow
from core.safe_json import JsonStructuralLimits
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus
from core.video_schema import (
    VIDEO_LIBRARY_GROUP_SCHEMA,
    VIDEO_LIBRARY_RECORD_SCHEMA,
    VIDEO_LIBRARY_SCAN_SCHEMA,
    VIDEO_LIBRARY_SCHEMA_VERSION,
)
from core.video.alignment import align_audio_fingerprints, align_frame_fingerprints
from core.video.analyzer import AnalysisLimits, VideoAnalyzer
from core.video.cache import (
    MAX_VIDEO_ARTIFACT_JSON_BYTES,
    artifact_from_json,
    artifact_to_json,
)
from core.video.fingerprint import (
    MetadataCandidatePolicy,
    RelationPolicy,
    classify_metadata_candidate,
    classify_video_relation,
)
from core.video.json_guard import strict_bounded_json_loads
from core.video.model import (
    ANALYZER_VERSION,
    AlignmentState,
    AnalysisState,
    SourceSnapshot,
    VideoArtifact,
    VideoMetadata,
)
from core.video.tools import (
    CommandRunner,
    CommandState,
    ToolCapability,
    ToolName,
    ToolState,
    capabilities_by_name,
    ffprobe_command,
    parse_ffprobe_json,
)

VIDEO_LIBRARY_CACHE_SCHEMA_VERSION = 3
VIDEO_LIBRARY_CACHE_APPLICATION_ID = 0x44474E56  # "DGNV"
_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
_SQLITE_HEADER_BYTES = 100
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
MAXIMUM_CACHE_ARTIFACT_BYTES = MAX_VIDEO_ARTIFACT_JSON_BYTES
MAXIMUM_CACHE_METADATA_BYTES = 64 * 1024
_VIDEO_CACHE_CREATE_TABLE_SQL = """
    CREATE TABLE artifacts (
        path TEXT PRIMARY KEY NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
        generation_token BLOB NOT NULL,
        identity_json TEXT NOT NULL,
        analyzer_version TEXT NOT NULL,
        analysis_profile TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        artifact_json TEXT
    )
"""
_VIDEO_CACHE_EXPECTED_OBJECTS = frozenset(
    {
        ("table", "artifacts", "artifacts", "text"),
        ("index", "sqlite_autoindex_artifacts_1", "artifacts", "null"),
    }
)
_VIDEO_CACHE_EXPECTED_COLUMNS = (
    ("path", "TEXT", 1, 1),
    ("size", "INTEGER", 1, 0),
    ("mtime_ns", "INTEGER", 1, 0),
    ("generation_token", "BLOB", 1, 0),
    ("identity_json", "TEXT", 1, 0),
    ("analyzer_version", "TEXT", 1, 0),
    ("analysis_profile", "TEXT", 1, 0),
    ("metadata_json", "TEXT", 1, 0),
    ("artifact_json", "TEXT", 0, 0),
)
_VIDEO_CACHE_SQLITE_LIMITS = (
    ("SQLITE_LIMIT_LENGTH", 32 * 1024 * 1024),
    ("SQLITE_LIMIT_SQL_LENGTH", 128 * 1024),
    ("SQLITE_LIMIT_COLUMN", 64),
    ("SQLITE_LIMIT_EXPR_DEPTH", 64),
    ("SQLITE_LIMIT_COMPOUND_SELECT", 16),
    ("SQLITE_LIMIT_VDBE_OP", 100_000),
    ("SQLITE_LIMIT_FUNCTION_ARG", 32),
    ("SQLITE_LIMIT_ATTACHED", 0),
    ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 4_096),
    ("SQLITE_LIMIT_VARIABLE_NUMBER", 128),
    ("SQLITE_LIMIT_TRIGGER_DEPTH", 0),
    ("SQLITE_LIMIT_WORKER_THREADS", 0),
)
_VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES = 128
_VIDEO_CACHE_MAX_SCHEMA_SQL_BYTES = 4_096
VIDEO_METADATA_JSON_LIMITS = JsonStructuralLimits(
    max_depth=2,
    max_container_entries=10,
    max_total_nodes=32,
    max_scalar_tokens=24,
    max_total_string_chars=32 * 1024,
    max_string_chars=4096,
    max_scalar_chars=128,
)

VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ogv",
        ".ts",
        ".webm",
        ".wmv",
    }
)

_COVERAGE_SKIP_EVENTS = {
    WalkEventKind.SYMLINK_SKIPPED,
    WalkEventKind.REPARSE_POINT_SKIPPED,
    WalkEventKind.MOUNT_SKIPPED,
    WalkEventKind.CYCLE_SKIPPED,
    WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
    WalkEventKind.SPECIAL_FILE_SKIPPED,
    WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
}


@dataclass(frozen=True)
class VideoLibraryLimits:
    maximum_files: int = 10_000
    maximum_candidate_assessments: int = 100_000
    maximum_candidates: int = 20_000
    maximum_comparisons: int = 2_000
    maximum_fingerprint_files: int = 4_000
    maximum_groups: int = 2_000
    probe_timeout_seconds: float = 20
    maximum_probe_output_bytes: int = 4 * 1024 * 1024
    maximum_scan_seconds: float = 4 * 60 * 60

    def __post_init__(self) -> None:
        integers = (
            self.maximum_files,
            self.maximum_candidate_assessments,
            self.maximum_candidates,
            self.maximum_comparisons,
            self.maximum_fingerprint_files,
            self.maximum_groups,
            self.maximum_probe_output_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integers):
            raise ValueError("video library limits must be positive integers")
        if self.maximum_candidates > self.maximum_candidate_assessments:
            raise ValueError("candidate count cannot exceed candidate assessments")
        if self.maximum_comparisons > self.maximum_candidates:
            raise ValueError("comparison count cannot exceed candidate count")
        if self.maximum_fingerprint_files > 2 * self.maximum_comparisons:
            raise ValueError("fingerprint-file limit cannot exceed two files per comparison")
        floats = (self.probe_timeout_seconds, self.maximum_scan_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in floats):
            raise ValueError("video library time limits must be positive and finite")


@dataclass(frozen=True)
class _VideoSource:
    path: str
    root: str
    size: int
    mtime_ns: int
    generation_token: bytes
    identity: FileIdentity

    @property
    def snapshot(self) -> SourceSnapshot:
        return SourceSnapshot(self.path, self.size, self.mtime_ns)


@dataclass(frozen=True)
class _LibraryIssue:
    code: str
    message: str
    source: Optional[str] = None
    tool: Optional[str] = None
    resource_limit: bool = False
    cancelled: bool = False
    timed_out: bool = False
    missing_tool: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "tool": self.tool,
            "source": self.source,
        }

    def receipt_issue(self) -> ScanIssue:
        return ScanIssue(self.code, self.message, self.source or "")


def _video_cache_readonly_uri(path: Path) -> str:
    return "{}?mode=ro&immutable=1".format(path.as_uri())


def _apply_video_cache_runtime_limits(connection) -> None:
    """Bound SQLite resources before an owned cache schema is inspected."""

    if not hasattr(connection, "setlimit"):
        return
    try:
        for constant_name, limit in _VIDEO_CACHE_SQLITE_LIMITS:
            category = getattr(sqlite3, constant_name, None)
            if category is None:
                continue
            connection.setlimit(category, limit)
            if connection.getlimit(category) > limit:
                raise ValueError(
                    "SQLite runtime limit {} could not be lowered for the video cache".format(
                        constant_name,
                    )
                )
    except ValueError:
        raise
    except (AttributeError, OverflowError, sqlite3.Error) as error:
        raise ValueError("video fingerprint cache SQLite runtime limits could not be configured") from error


def _configure_video_cache_connection(connection, *, query_only: bool) -> None:
    _apply_video_cache_runtime_limits(connection)
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        trusted_schema = connection.execute("PRAGMA trusted_schema").fetchone()
        if trusted_schema is None or int(trusted_schema[0]) != 0:
            raise ValueError("SQLite trusted_schema could not be disabled for the video cache")
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise ValueError("SQLite foreign keys could not be enabled for the video cache")
        connection.execute("PRAGMA query_only = {}".format("ON" if query_only else "OFF"))
        query_only_row = connection.execute("PRAGMA query_only").fetchone()
        expected = 1 if query_only else 0
        if query_only_row is None or int(query_only_row[0]) != expected:
            raise ValueError("SQLite query_only mode could not be configured for the video cache")
    except ValueError:
        raise
    except sqlite3.Error as error:
        raise ValueError("video fingerprint cache SQLite safety settings could not be configured") from error


def _normalize_video_cache_schema_sql(value) -> str:
    if not isinstance(value, str):
        raise ValueError("video fingerprint cache schema SQL is missing or invalid")
    normalized = " ".join(value.split()).upper()
    return normalized.replace("( ", "(").replace(" )", ")")


def _video_cache_schema_objects(connection):
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
            _VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES,
            _VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES,
            _VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES,
            len(_VIDEO_CACHE_EXPECTED_OBJECTS) + 1,
        ),
    ).fetchall()
    if len(rows) != len(_VIDEO_CACHE_EXPECTED_OBJECTS):
        return None
    objects = set()
    for object_type, name, table_name, sql_type in rows:
        if object_type is None or name is None or table_name is None:
            return None
        objects.add(
            (
                str(object_type),
                str(name),
                str(table_name),
                str(sql_type),
            )
        )
    return frozenset(objects)


def _video_cache_table_columns(connection):
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
            _VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES,
            _VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES,
            "artifacts",
            len(_VIDEO_CACHE_EXPECTED_COLUMNS) + 1,
        ),
    ).fetchall()
    if len(rows) != len(_VIDEO_CACHE_EXPECTED_COLUMNS):
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
        columns.append(
            (
                str(name),
                str(declared_type),
                int(not_null),
                int(primary_key),
            )
        )
    return tuple(columns)


def _video_cache_table_sql(connection):
    rows = connection.execute(
        """
        SELECT CASE
            WHEN typeof(sql) = 'text'
                AND length(CAST(sql AS BLOB)) BETWEEN 1 AND ?
            THEN sql
        END
        FROM sqlite_schema
        WHERE type = 'table' AND name = 'artifacts'
        LIMIT 2
        """,
        (_VIDEO_CACHE_MAX_SCHEMA_SQL_BYTES,),
    ).fetchall()
    if len(rows) != 1 or rows[0][0] is None:
        return None
    return _normalize_video_cache_schema_sql(str(rows[0][0]))


class _ArtifactCache:
    """Small strict SQLite cache for metadata and complete fingerprint artifacts."""

    _COLUMNS = (
        "path",
        "size",
        "mtime_ns",
        "generation_token",
        "identity_json",
        "analyzer_version",
        "analysis_profile",
        "metadata_json",
        "artifact_json",
    )

    def __init__(
        self,
        path: Optional[str | Path],
        roots: Sequence[Path],
        analysis_profile: str,
    ) -> None:
        if not analysis_profile:
            raise ValueError("video analysis profile must not be empty")
        self.path = None if path is None else _validate_cache_path(path, roots)
        self.analysis_profile = analysis_profile
        created_database = False
        expected_identity = None
        if self.path is not None:
            _require_no_video_cache_sidecars(self.path)
            if os.path.lexists(self.path):
                expected_identity = _cache_file_identity(self.path)
                _read_owned_cache_header(
                    self.path,
                    expected_identity,
                )
            else:
                expected_identity = _reserve_new_cache_file(self.path)
                created_database = True
        database = ":memory:" if self.path is None else str(self.path)
        self.connection = None
        try:
            if self.path is not None and not created_database:
                inspection_connection = None
                try:
                    inspection_connection = sqlite3.connect(
                        _video_cache_readonly_uri(self.path),
                        timeout=10,
                        uri=True,
                    )
                    _configure_video_cache_connection(
                        inspection_connection,
                        query_only=True,
                    )
                    _require_unchanged_cache_identity(
                        self.path,
                        expected_identity,
                    )
                    self._validate_schema(inspection_connection)
                    _require_unchanged_cache_identity(
                        self.path,
                        expected_identity,
                    )
                    _require_no_video_cache_sidecars(self.path)
                finally:
                    if inspection_connection is not None:
                        inspection_connection.close()
                _require_no_video_cache_sidecars(self.path)
                _require_unchanged_cache_identity(
                    self.path,
                    expected_identity,
                )
                _read_owned_cache_header(
                    self.path,
                    expected_identity,
                )
            self.connection = sqlite3.connect(database, timeout=10)
            if self.path is not None:
                _require_unchanged_cache_identity(
                    self.path,
                    expected_identity,
                )
            _configure_video_cache_connection(
                self.connection,
                query_only=False,
            )
            self._initialize(new_database=self.path is None or created_database)
            if self.path is not None:
                _require_unchanged_cache_identity(
                    self.path,
                    expected_identity,
                )
                _require_no_video_cache_sidecars(self.path)
        except sqlite3.Error as error:
            if self.connection is not None:
                self.connection.close()
            raise ValueError("video fingerprint cache is unavailable: {}".format(error)) from error
        except BaseException:
            if self.connection is not None:
                self.connection.close()
            raise
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _initialize(self, *, new_database: bool) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(self.connection.execute("PRAGMA application_id").fetchone()[0])
        if new_database:
            objects = self.connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            if version != 0 or application_id != 0 or objects is not None:
                raise ValueError("new video fingerprint cache is not empty")
            self.connection.execute(_VIDEO_CACHE_CREATE_TABLE_SQL)
            self.connection.execute("PRAGMA application_id = {}".format(VIDEO_LIBRARY_CACHE_APPLICATION_ID))
            self.connection.execute("PRAGMA user_version = {}".format(VIDEO_LIBRARY_CACHE_SCHEMA_VERSION))
            self.connection.commit()
        self._validate_schema(self.connection)

    @staticmethod
    def _validate_schema(connection) -> None:
        quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
        if quick_check is None or tuple(quick_check) != ("ok",):
            raise ValueError("video fingerprint cache integrity check failed")
        version_row = connection.execute("PRAGMA user_version").fetchone()
        application_id_row = connection.execute("PRAGMA application_id").fetchone()
        if version_row is None or application_id_row is None:
            raise ValueError("video fingerprint cache ownership metadata is missing")
        version = int(version_row[0])
        application_id = int(application_id_row[0])
        if version != VIDEO_LIBRARY_CACHE_SCHEMA_VERSION or application_id != VIDEO_LIBRARY_CACHE_APPLICATION_ID:
            raise ValueError("video fingerprint cache schema is unsupported")
        if _video_cache_schema_objects(connection) != _VIDEO_CACHE_EXPECTED_OBJECTS:
            raise ValueError("video fingerprint cache contains an unsupported object set")
        if _video_cache_table_columns(connection) != _VIDEO_CACHE_EXPECTED_COLUMNS:
            raise ValueError("video fingerprint cache table shape is unsupported")
        if _video_cache_table_sql(connection) != _normalize_video_cache_schema_sql(_VIDEO_CACHE_CREATE_TABLE_SQL):
            raise ValueError("video fingerprint cache table SQL is unsupported")
        indexes = connection.execute(
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
            (_VIDEO_CACHE_MAX_SCHEMA_NAME_BYTES, "artifacts"),
        ).fetchall()
        if indexes != [(0, "sqlite_autoindex_artifacts_1", 1, "pk", 0)]:
            raise ValueError("video fingerprint cache primary-key index is unsupported")

    def metadata(self, source: _VideoSource) -> Optional[VideoMetadata]:
        row = self._row(source)
        if row is None or row[1] != 1 or row[0] is None:
            self.misses += 1
            return None
        try:
            metadata = _metadata_from_json(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            self.misses += 1
            return None
        self.hits += 1
        return metadata

    def artifact(self, source: _VideoSource) -> Optional[VideoArtifact]:
        row = self._row(source)
        if row is None or row[3] != 1 or row[2] is None:
            self.misses += 1
            return None
        encoded = bytes(row[2])
        if len(encoded) > MAXIMUM_CACHE_ARTIFACT_BYTES:
            self.misses += 1
            return None
        try:
            artifact = artifact_from_json(encoded)
        except ValueError:
            self.misses += 1
            return None
        if (
            artifact.state is not AnalysisState.COMPLETE
            or artifact.analyzer_version != ANALYZER_VERSION
            or artifact.source != source.snapshot
        ):
            self.misses += 1
            return None
        self.hits += 1
        return artifact

    def _row(self, source: _VideoSource):
        return self.connection.execute(
            """
            SELECT
                CASE
                    WHEN typeof(metadata_json) = 'text'
                        AND length(CAST(metadata_json AS BLOB)) <= ?
                    THEN CAST(metadata_json AS BLOB)
                    ELSE NULL
                END,
                CASE
                    WHEN typeof(metadata_json) = 'text'
                        AND length(CAST(metadata_json AS BLOB)) <= ?
                    THEN 1
                    ELSE 0
                END,
                CASE
                    WHEN typeof(artifact_json) = 'text'
                        AND length(CAST(artifact_json AS BLOB)) <= ?
                    THEN CAST(artifact_json AS BLOB)
                    ELSE NULL
                END,
                CASE
                    WHEN artifact_json IS NULL THEN 1
                    WHEN typeof(artifact_json) = 'text'
                        AND length(CAST(artifact_json AS BLOB)) <= ?
                    THEN 1
                    ELSE 0
                END
            FROM artifacts
            WHERE path = ? AND size = ? AND mtime_ns = ? AND generation_token = ?
                AND identity_json = ? AND analyzer_version = ?
                AND analysis_profile = ?
            """,
            (
                MAXIMUM_CACHE_METADATA_BYTES,
                MAXIMUM_CACHE_METADATA_BYTES,
                MAXIMUM_CACHE_ARTIFACT_BYTES,
                MAXIMUM_CACHE_ARTIFACT_BYTES,
                source.path,
                source.size,
                source.mtime_ns,
                source.generation_token,
                _identity_to_json(source.identity),
                ANALYZER_VERSION,
                self.analysis_profile,
            ),
        ).fetchone()

    def put_metadata(self, source: _VideoSource, metadata: VideoMetadata) -> None:
        metadata_json = _bounded_cache_metadata_json(metadata)
        self.connection.execute(
            """
            INSERT INTO artifacts (
                path, size, mtime_ns, generation_token, identity_json, analyzer_version,
                analysis_profile, metadata_json, artifact_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                generation_token = excluded.generation_token,
                identity_json = excluded.identity_json,
                analyzer_version = excluded.analyzer_version,
                analysis_profile = excluded.analysis_profile,
                metadata_json = excluded.metadata_json,
                artifact_json = NULL
            """,
            (
                source.path,
                source.size,
                source.mtime_ns,
                source.generation_token,
                _identity_to_json(source.identity),
                ANALYZER_VERSION,
                self.analysis_profile,
                metadata_json,
            ),
        )
        self.writes += 1

    def put_artifact(self, source: _VideoSource, artifact: VideoArtifact) -> None:
        if artifact.state is not AnalysisState.COMPLETE or artifact.source != source.snapshot:
            raise ValueError("only a complete matching video artifact can be cached")
        assert artifact.metadata is not None
        metadata_json = _bounded_cache_metadata_json(artifact.metadata)
        encoded = artifact_to_json(artifact)
        if len(encoded.encode("utf-8")) > MAXIMUM_CACHE_ARTIFACT_BYTES:
            raise ValueError("video fingerprint artifact exceeds the cache size limit")
        self.connection.execute(
            """
            INSERT INTO artifacts (
                path, size, mtime_ns, generation_token, identity_json, analyzer_version,
                analysis_profile, metadata_json, artifact_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                generation_token = excluded.generation_token,
                identity_json = excluded.identity_json,
                analyzer_version = excluded.analyzer_version,
                analysis_profile = excluded.analysis_profile,
                metadata_json = excluded.metadata_json,
                artifact_json = excluded.artifact_json
            """,
            (
                source.path,
                source.size,
                source.mtime_ns,
                source.generation_token,
                _identity_to_json(source.identity),
                ANALYZER_VERSION,
                self.analysis_profile,
                metadata_json,
                encoded,
            ),
        )
        self.writes += 1

    def close(self, *, commit: bool) -> None:
        connection = self.connection
        if connection is None:
            return
        self.connection = None
        try:
            if commit:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def add(self, value: str) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            parent = self.find(parent)
            self.parent[value] = parent
        return parent

    def union(self, first: str, second: str) -> None:
        self.add(first)
        self.add(second)
        left = self.find(first)
        right = self.find(second)
        if left != right:
            low, high = sorted((left, right))
            self.parent[high] = low


class VideoLibraryScanner:
    def __init__(
        self,
        *,
        runner: CommandRunner,
        capabilities: Sequence[ToolCapability],
        analyzer_limits: AnalysisLimits = AnalysisLimits(),
        analyzer_factory: Optional[Callable[[], VideoAnalyzer]] = None,
        walker=walk_no_follow,
    ) -> None:
        self.runner = runner
        self.capabilities = tuple(capabilities)
        self.capability_map = capabilities_by_name(self.capabilities)
        self.analyzer_limits = analyzer_limits
        self.analyzer_factory = analyzer_factory
        self.walker = walker

    def scan(
        self,
        roots: Iterable[str | Path],
        *,
        cache_path: Optional[str | Path] = None,
        threshold: float = RelationPolicy().related_score,
        limits: VideoLibraryLimits = VideoLibraryLimits(),
        progress: Optional[Callable[[str, Mapping[str, object]], None]] = None,
    ) -> Dict[str, object]:
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(float(threshold))
            or not 0 <= float(threshold) <= 1
        ):
            raise ValueError("video library threshold must be between 0 and 1")
        normalized_roots = _normalize_roots(roots)
        for root in normalized_roots:
            if is_within_reserved_internal_directory(root):
                raise ValueError(
                    "video library root is inside a reserved internal directory "
                    "({}): {}".format(
                        ", ".join(sorted(RESERVED_INTERNAL_DIRECTORY_NAMES)),
                        root,
                    )
                )
        normalized_cache_path = None if cache_path is None else _validate_cache_path(cache_path, normalized_roots)
        cache_identity = (
            None
            if normalized_cache_path is None or not os.path.lexists(normalized_cache_path)
            else _plain_file_identity(
                normalized_cache_path,
                description="video fingerprint cache",
            )
        )
        emit = progress or (lambda _stage, _fields: None)
        started = time.monotonic()
        started_at_ns = time.time_ns()
        issues: list[_LibraryIssue] = []
        sources: list[_VideoSource] = []
        discovered = skipped = walk_failed = 0
        seen_identities = set()

        emit("video-enumerating", {"roots": len(normalized_roots), "files": 0})
        limit_reached = False
        for root in normalized_roots:
            for event in self.walker(
                root,
                allowed_root=root,
                cross_mounts=False,
                directory_pruner=_reserved_directory_prune_reason,
            ):
                if time.monotonic() - started > limits.maximum_scan_seconds:
                    issues.append(
                        _LibraryIssue(
                            "scan_time_limit",
                            "video library scan exceeded its configured total time",
                            str(root),
                            resource_limit=True,
                        )
                    )
                    limit_reached = True
                    break
                if event.kind is WalkEventKind.FILE:
                    if (
                        cache_identity is not None
                        and event.identity is not None
                        and same_physical_file(
                            cache_identity,
                            event.identity,
                        ).verdict
                        is IdentityVerdict.SAME
                    ):
                        raise ValueError("video fingerprint cache aliases a file inside an input root")
                    if event.path.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    discovered += 1
                    if len(sources) >= limits.maximum_files:
                        skipped += 1
                        issues.append(
                            _LibraryIssue(
                                "file_limit",
                                "video file count exceeded the configured limit",
                                str(event.path),
                                resource_limit=True,
                            )
                        )
                        limit_reached = True
                        break
                    try:
                        source = _capture_source(event.path, root, event.identity)
                    except (OSError, ValueError, FileIdentityError) as error:
                        walk_failed += 1
                        issues.append(
                            _LibraryIssue(
                                "source_snapshot_failed",
                                str(error),
                                str(event.path),
                            )
                        )
                        continue
                    identity_key = source.identity.comparison_key
                    if identity_key in seen_identities:
                        skipped += 1
                        issues.append(
                            _LibraryIssue(
                                "physical_alias_skipped",
                                "the same physical video was already discovered",
                                source.path,
                            )
                        )
                        continue
                    seen_identities.add(identity_key)
                    sources.append(source)
                    if len(sources) % 100 == 0:
                        emit(
                            "video-enumerating",
                            {"roots": len(normalized_roots), "files": len(sources)},
                        )
                elif event.kind in _COVERAGE_SKIP_EVENTS:
                    discovered += 1
                    skipped += 1
                    issues.append(
                        _LibraryIssue(
                            "walk_{}".format(event.kind.value.replace("-", "_")),
                            event.detail or event.kind.value,
                            str(event.path),
                        )
                    )
                elif event.kind is WalkEventKind.ERROR:
                    discovered += 1
                    walk_failed += 1
                    message = (
                        event.error.message if event.error is not None else event.detail or "filesystem walk failed"
                    )
                    issues.append(_LibraryIssue("walk_error", message, str(event.path)))
            if limit_reached:
                break

        analyzer = self._analyzer()
        cache = _ArtifactCache(
            normalized_cache_path,
            normalized_roots,
            _analysis_profile(analyzer, self.capabilities),
        )
        metadata_by_path: Dict[str, VideoMetadata] = {}
        metadata_failures = 0
        try:
            probe_capability = self.capability_map[ToolName.FFPROBE]
            if not probe_capability.available:
                metadata_failures = len(sources)
                issues.append(_capability_issue(probe_capability))
            else:
                assert probe_capability.executable is not None
                for index, source in enumerate(sources, 1):
                    if time.monotonic() - started > limits.maximum_scan_seconds:
                        remaining = len(sources) - index + 1
                        skipped += remaining
                        issues.append(
                            _LibraryIssue(
                                "scan_time_limit",
                                "video metadata probing exceeded the configured total time",
                                source.path,
                                resource_limit=True,
                            )
                        )
                        break
                    cached_metadata = cache.metadata(source)
                    if cached_metadata is not None:
                        metadata_by_path[source.path] = cached_metadata
                    else:
                        metadata, issue = self._probe(source, probe_capability.executable, limits)
                        if issue is not None:
                            metadata_failures += 1
                            issues.append(issue)
                        else:
                            assert metadata is not None
                            metadata_by_path[source.path] = metadata
                            cache.put_metadata(source, metadata)
                    if index % 25 == 0 or index == len(sources):
                        emit(
                            "video-probing",
                            {
                                "files": len(sources),
                                "probed": index,
                                "failed": metadata_failures,
                            },
                        )

            candidates, candidate_assessments, candidate_limited = _metadata_candidates(
                sources,
                metadata_by_path,
                limits,
            )
            if candidate_limited:
                issues.append(
                    _LibraryIssue(
                        "candidate_limit",
                        "metadata candidate generation reached a configured hard limit",
                        resource_limit=True,
                    )
                )
            emit(
                "video-candidates",
                {
                    "assessments": candidate_assessments,
                    "candidates": len(candidates),
                },
            )
            selected_pairs = candidates[: limits.maximum_comparisons]
            if len(candidates) > limits.maximum_comparisons:
                issues.append(
                    _LibraryIssue(
                        "comparison_limit",
                        "video candidate count exceeded the fingerprint comparison limit",
                        resource_limit=True,
                    )
                )

            source_by_path = {source.path: source for source in sources}
            selected_paths = tuple(
                sorted({path for first_path, second_path in selected_pairs for path in (first_path, second_path)})
            )
            if len(selected_paths) > limits.maximum_fingerprint_files:
                selected_paths = selected_paths[: limits.maximum_fingerprint_files]
                selected_set = set(selected_paths)
                selected_pairs = tuple(
                    pair for pair in selected_pairs if pair[0] in selected_set and pair[1] in selected_set
                )
                issues.append(
                    _LibraryIssue(
                        "fingerprint_file_limit",
                        "candidate videos exceeded the configured fingerprint-file limit",
                        resource_limit=True,
                    )
                )

            artifacts: Dict[str, VideoArtifact] = {}
            for index, path in enumerate(selected_paths, 1):
                if time.monotonic() - started > limits.maximum_scan_seconds:
                    issues.append(
                        _LibraryIssue(
                            "scan_time_limit",
                            "video fingerprinting exceeded the configured total time",
                            path,
                            resource_limit=True,
                        )
                    )
                    break
                source = source_by_path[path]
                artifact = cache.artifact(source)
                if artifact is None:
                    artifact = analyzer.analyze(path)
                    try:
                        _validate_source(source)
                    except (OSError, ValueError, FileIdentityError) as error:
                        issues.append(
                            _LibraryIssue(
                                "source_changed",
                                str(error),
                                path,
                            )
                        )
                        continue
                    if artifact.state is AnalysisState.COMPLETE:
                        cache.put_artifact(source, artifact)
                if artifact.state is not AnalysisState.COMPLETE:
                    issues.extend(
                        _LibraryIssue(
                            issue.code,
                            issue.message,
                            path,
                            issue.tool,
                            resource_limit=artifact.state is AnalysisState.PARTIAL_RESOURCE_LIMIT,
                            cancelled=artifact.state is AnalysisState.PARTIAL_CANCELLED,
                            timed_out=artifact.state is AnalysisState.PARTIAL_TIMEOUT,
                            missing_tool=artifact.state is AnalysisState.PARTIAL_MISSING_TOOL,
                        )
                        for issue in artifact.issues
                    )
                if artifact.comparable:
                    artifacts[path] = artifact
                if index % 10 == 0 or index == len(selected_paths):
                    emit(
                        "video-fingerprinting",
                        {"files": len(selected_paths), "analyzed": index},
                    )

            relations = []
            compared = 0
            for first_path, second_path in selected_pairs:
                if time.monotonic() - started > limits.maximum_scan_seconds:
                    issues.append(
                        _LibraryIssue(
                            "scan_time_limit",
                            "video comparison exceeded the configured total time",
                            "{} | {}".format(first_path, second_path),
                            resource_limit=True,
                        )
                    )
                    break
                first = artifacts.get(first_path)
                second = artifacts.get(second_path)
                if first is None or second is None:
                    continue
                relation, issue = _compare_artifacts(first, second, float(threshold))
                compared += 1
                if issue is not None:
                    issues.append(issue)
                elif relation is not None:
                    relations.append(relation)
                if compared % 25 == 0 or compared == len(selected_pairs):
                    emit(
                        "video-comparing",
                        {"comparisons": len(selected_pairs), "completed": compared},
                    )

            groups, group_limited = _build_groups(
                relations,
                artifacts,
                limits.maximum_groups,
            )
            if group_limited:
                issues.append(
                    _LibraryIssue(
                        "group_limit",
                        "video relation groups exceeded the configured group limit",
                        resource_limit=True,
                    )
                )
            report = _report(
                roots=normalized_roots,
                threshold=float(threshold),
                limits=limits,
                sources=sources,
                metadata_count=len(metadata_by_path),
                metadata_failures=metadata_failures,
                discovered=discovered,
                skipped=skipped,
                walk_failed=walk_failed,
                candidate_assessments=candidate_assessments,
                candidates=len(candidates),
                comparisons=compared,
                relations=len(relations),
                groups=groups,
                issues=issues,
                cache=cache,
                started_at_ns=started_at_ns,
            )
            cache.close(commit=True)
            return report
        except BaseException:
            cache.close(commit=False)
            raise

    def _probe(
        self,
        source: _VideoSource,
        executable: str,
        limits: VideoLibraryLimits,
    ) -> Tuple[Optional[VideoMetadata], Optional[_LibraryIssue]]:
        outcome = self.runner.run(
            ffprobe_command(executable, source.path),
            timeout_seconds=limits.probe_timeout_seconds,
            max_output_bytes=limits.maximum_probe_output_bytes,
        )
        if outcome.state is not CommandState.SUCCESS:
            return None, _outcome_issue(outcome.state, outcome.error, source.path)
        try:
            metadata = parse_ffprobe_json(outcome.stdout)
            _validate_library_metadata(metadata)
            _validate_source(source)
        except (OSError, ValueError, FileIdentityError) as error:
            return None, _LibraryIssue(
                "metadata_probe_invalid",
                str(error),
                source.path,
                ToolName.FFPROBE.value,
            )
        return metadata, None

    def _analyzer(self) -> VideoAnalyzer:
        if self.analyzer_factory is not None:
            return self.analyzer_factory()
        return VideoAnalyzer(
            runner=self.runner,
            capabilities=self.capabilities,
            limits=self.analyzer_limits,
        )


def _capture_source(
    path: Path,
    root: Path,
    expected_identity: Optional[FileIdentity],
) -> _VideoSource:
    candidate = Path(os.path.abspath(os.fspath(path)))
    file_stat = os.stat(candidate, follow_symlinks=False)
    if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("video source is not a plain regular file")
    identity = get_file_identity(candidate, follow_symlinks=False, stat_result=file_stat)
    if expected_identity is not None:
        comparison = same_physical_file(expected_identity, identity)
        if comparison.verdict is not IdentityVerdict.SAME:
            raise ValueError("video source identity changed after enumeration")
    resolved = candidate.resolve(strict=True)
    if _path_key(resolved) != _path_key(candidate):
        raise ValueError("video source path traverses a symbolic link or reparse point")
    return _VideoSource(
        str(resolved),
        str(root),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        get_file_generation_token(
            candidate,
            stat_result=file_stat,
            expected_identity=identity,
        ).encoded,
        identity,
    )


def _validate_source(source: _VideoSource) -> None:
    path = Path(source.path)
    file_stat = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("video source is no longer a plain regular file")
    identity = get_file_identity(path, follow_symlinks=False, stat_result=file_stat)
    if (
        int(file_stat.st_size) != source.size
        or int(file_stat.st_mtime_ns) != source.mtime_ns
        or get_file_generation_token(
            path,
            stat_result=file_stat,
            expected_identity=identity,
        ).encoded
        != source.generation_token
        or same_physical_file(source.identity, identity).verdict is not IdentityVerdict.SAME
    ):
        raise ValueError("video source changed during library analysis")


def _normalize_roots(roots: Iterable[str | Path]) -> Tuple[Path, ...]:
    result = tuple(
        sorted(
            {Path(os.path.abspath(os.fspath(root))) for root in roots},
            key=_path_key,
        )
    )
    if not result:
        raise ValueError("video library scan requires at least one root")
    for index, root in enumerate(result):
        if any(_is_within(root, other) for other in result[:index] + result[index + 1 :]):
            raise ValueError("video library roots must not overlap")
    return result


def _reserved_directory_prune_reason(path: Path) -> Optional[str]:
    if is_reserved_internal_directory(path):
        return "dupeGuru Neo internal quarantine/state directory is intentionally excluded"
    return None


def _require_no_video_cache_sidecars(path: Path) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path("{}{}".format(path, suffix))
        if os.path.lexists(sidecar):
            raise ValueError(
                "video fingerprint cache refuses an existing SQLite sidecar: '{}'".format(
                    sidecar,
                )
            )


def _validate_cache_path(path: str | Path, roots: Sequence[Path]) -> Path:
    raw_path = os.fspath(path)
    if not raw_path or "\0" in raw_path:
        raise ValueError("video fingerprint cache path must be a non-empty filesystem path")
    candidate = Path(os.path.abspath(raw_path))
    if any(_is_within(candidate, root) for root in roots):
        raise ValueError("video fingerprint cache must be outside every input root")
    parent = candidate.parent
    parent_stat = os.stat(parent, follow_symlinks=False)
    if stat.S_ISLNK(parent_stat.st_mode) or is_reparse_point(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
        raise ValueError("video fingerprint cache parent must be a plain directory")
    resolved_parent = parent.resolve(strict=True)
    if _path_key(parent) != _path_key(resolved_parent):
        raise ValueError("video fingerprint cache parent traverses a link or reparse point")
    _require_no_video_cache_sidecars(candidate)
    if os.path.lexists(candidate):
        _cache_file_identity(candidate)
    return candidate


def _cache_file_identity(path: str | Path) -> FileIdentity:
    candidate = Path(path)
    try:
        cache_stat = os.stat(candidate, follow_symlinks=False)
    except OSError as error:
        raise ValueError("video fingerprint cache is unavailable") from error
    if stat.S_ISLNK(cache_stat.st_mode) or is_reparse_point(cache_stat) or not stat.S_ISREG(cache_stat.st_mode):
        raise ValueError("video fingerprint cache must be a plain regular file")
    if getattr(cache_stat, "st_nlink", None) != 1:
        raise ValueError(
            "video fingerprint cache aliases a file inside an input root or another path; "
            "it must have exactly one filesystem link"
        )
    try:
        return get_file_identity(
            candidate,
            follow_symlinks=False,
            stat_result=cache_stat,
        )
    except FileIdentityError as error:
        raise ValueError("video fingerprint cache physical identity is unavailable") from error


def _reserve_new_cache_file(path: Path) -> FileIdentity:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ValueError("video fingerprint cache path appeared during exclusive creation") from error
    except OSError as error:
        raise ValueError("video fingerprint cache could not be reserved safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or is_reparse_point(opened) or getattr(opened, "st_nlink", None) != 1:
            raise ValueError("reserved video fingerprint cache is not a single-link regular file")
    finally:
        os.close(descriptor)
    return _cache_file_identity(path)


def _read_owned_cache_header(
    path: Path,
    expected_identity: FileIdentity,
):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("video fingerprint cache header could not be opened safely") from error
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_point(opened_before)
            or getattr(opened_before, "st_nlink", None) != 1
        ):
            raise ValueError("opened video fingerprint cache is not a single-link regular file")
        try:
            identity_before = get_file_identity_from_fd(
                descriptor,
                path,
                stat_result=opened_before,
            )
            generation_before = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_before,
                expected_identity=identity_before,
            )
        except (FileGenerationError, FileIdentityError) as error:
            raise ValueError("video fingerprint cache header identity is unavailable") from error
        if (
            same_physical_file(
                expected_identity,
                identity_before,
            ).verdict
            is not IdentityVerdict.SAME
        ):
            raise ValueError("video fingerprint cache identity changed before its header was read")
        header = os.read(descriptor, _SQLITE_HEADER_BYTES)
        opened_after = os.fstat(descriptor)
        try:
            identity_after = get_file_identity_from_fd(
                descriptor,
                path,
                stat_result=opened_after,
            )
            generation_after = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_after,
                expected_identity=identity_after,
            )
        except (FileGenerationError, FileIdentityError) as error:
            raise ValueError("video fingerprint cache header identity became unavailable") from error
    finally:
        os.close(descriptor)
    if (
        len(header) != _SQLITE_HEADER_BYTES
        or header[:16] != _SQLITE_HEADER_MAGIC
        or header[18:20] != b"\x01\x01"
        or int.from_bytes(header[60:64], "big") != VIDEO_LIBRARY_CACHE_SCHEMA_VERSION
        or int.from_bytes(header[68:72], "big") != VIDEO_LIBRARY_CACHE_APPLICATION_ID
    ):
        raise ValueError("video fingerprint cache ownership header is missing or unsupported")
    if (
        int(opened_before.st_size) != int(opened_after.st_size)
        or int(opened_before.st_mtime_ns) != int(opened_after.st_mtime_ns)
        or generation_before != generation_after
        or same_physical_file(
            identity_before,
            identity_after,
        ).verdict
        is not IdentityVerdict.SAME
    ):
        raise ValueError("video fingerprint cache changed while its ownership header was read")
    _require_unchanged_cache_identity(
        path,
        expected_identity,
    )


def _require_unchanged_cache_identity(
    path: Path,
    expected_identity: FileIdentity,
) -> None:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or is_reparse_point(file_stat)
            or not stat.S_ISREG(file_stat.st_mode)
            or getattr(file_stat, "st_nlink", None) != 1
        ):
            raise ValueError("video fingerprint cache is no longer a single-link regular file")
        current_identity = get_file_identity(
            path,
            follow_symlinks=False,
            stat_result=file_stat,
        )
    except (FileIdentityError, OSError) as error:
        raise ValueError("video fingerprint cache identity is unavailable") from error
    if (
        same_physical_file(
            expected_identity,
            current_identity,
        ).verdict
        is not IdentityVerdict.SAME
    ):
        raise ValueError("video fingerprint cache identity changed while SQLite opened it")


def _plain_file_identity(
    path: str | Path,
    *,
    description: str,
) -> FileIdentity:
    candidate = Path(path)
    file_stat = os.stat(candidate, follow_symlinks=False)
    if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("{} must be a plain regular file".format(description))
    return get_file_identity(
        candidate,
        follow_symlinks=False,
        stat_result=file_stat,
    )


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: str | Path, root: str | Path) -> bool:
    try:
        candidate = _path_key(path)
        container = _path_key(root)
        return os.path.commonpath((candidate, container)) == container
    except ValueError:
        return False


def _identity_to_json(identity: FileIdentity) -> str:
    if isinstance(identity.file_id, bytes):
        file_id_kind = "bytes"
        file_id_value: object = identity.file_id.hex()
    else:
        file_id_kind = "integer"
        file_id_value = int(identity.file_id)
    return json.dumps(
        {
            "namespace": identity.namespace,
            "capability": identity.capability.value,
            "volume_id": identity.volume_id,
            "file_id_kind": file_id_kind,
            "file_id": file_id_value,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _analysis_profile(
    analyzer: VideoAnalyzer,
    capabilities: Sequence[ToolCapability],
) -> str:
    return json.dumps(
        {
            "analyzer_version": ANALYZER_VERSION,
            "limits": asdict(analyzer.limits),
            "frame_policy": asdict(analyzer.frame_policy),
            "tools": [
                {
                    "name": capability.tool.value,
                    "state": capability.state.value,
                    "executable": capability.executable,
                    "version": capability.version,
                }
                for capability in sorted(
                    capabilities,
                    key=lambda item: item.tool.value,
                )
            ],
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _metadata_to_json(metadata: VideoMetadata) -> str:
    return json.dumps(
        metadata.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_cache_metadata_json(metadata: VideoMetadata) -> str:
    encoded = _metadata_to_json(metadata)
    if len(encoded.encode("utf-8")) > MAXIMUM_CACHE_METADATA_BYTES:
        raise ValueError("video metadata exceeds the cache size limit")
    try:
        _metadata_from_json(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("video metadata cannot be represented by the bounded cache format") from error
    return encoded


def _metadata_from_json(payload: str | bytes) -> VideoMetadata:
    try:
        value = strict_bounded_json_loads(
            payload,
            max_bytes=MAXIMUM_CACHE_METADATA_BYTES,
            limits=VIDEO_METADATA_JSON_LIMITS,
            label="video metadata JSON",
        )
    except ValueError as error:
        raise ValueError("cached video metadata is not valid bounded JSON") from error
    if not isinstance(value, dict):
        raise ValueError("cached video metadata must be an object")
    expected = {
        "duration_seconds",
        "width",
        "height",
        "frame_rate",
        "video_codec",
        "pixel_format",
        "audio_codec",
        "audio_duration_seconds",
        "bit_rate",
        "container",
    }
    if set(value) != expected:
        raise ValueError("cached video metadata has an unsupported shape")
    metadata = VideoMetadata(
        duration_seconds=_metadata_number(value["duration_seconds"], "duration_seconds"),
        width=_metadata_integer(value["width"], "width"),
        height=_metadata_integer(value["height"], "height"),
        frame_rate=_metadata_number(value["frame_rate"], "frame_rate"),
        video_codec=_metadata_string(value["video_codec"], "video_codec", allow_empty=False),
        pixel_format=_metadata_string(value["pixel_format"], "pixel_format"),
        audio_codec=_metadata_string(value["audio_codec"], "audio_codec"),
        audio_duration_seconds=(
            None
            if value["audio_duration_seconds"] is None
            else _metadata_number(value["audio_duration_seconds"], "audio_duration_seconds")
        ),
        bit_rate=None if value["bit_rate"] is None else _metadata_integer(value["bit_rate"], "bit_rate"),
        container=_metadata_string(value["container"], "container"),
    )
    _validate_library_metadata(metadata)
    return metadata


def _metadata_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cached video metadata {} must be a finite number".format(name))
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError("cached video metadata {} must be a finite number".format(name)) from error
    if not math.isfinite(result):
        raise ValueError("cached video metadata {} must be a finite number".format(name))
    return result


def _metadata_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 63) <= value <= (1 << 63) - 1:
        raise ValueError("cached video metadata {} must be a signed 64-bit integer".format(name))
    return value


def _metadata_string(value, name, *, allow_empty=True):
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > VIDEO_METADATA_JSON_LIMITS.max_string_chars
        or "\0" in value
    ):
        raise ValueError("cached video metadata {} must be a bounded string".format(name))
    return value


def _validate_library_metadata(metadata: VideoMetadata) -> None:
    try:
        aspect_ratio = metadata.aspect_ratio
    except ArithmeticError as error:
        raise ValueError("video aspect ratio cannot be represented") from error
    if not math.isfinite(aspect_ratio) or aspect_ratio <= 0:
        raise ValueError("video aspect ratio must be positive and finite")


def _aspect_bucket(metadata: VideoMetadata, policy: MetadataCandidatePolicy) -> int:
    step = max(policy.aspect_tolerance_ratio, 0.01)
    return int(round(math.log(metadata.aspect_ratio) / math.log1p(step)))


def _duration_bucket(metadata: VideoMetadata) -> int:
    return int(math.floor(math.log(metadata.duration_seconds) / math.log(1.25)))


def _metadata_candidates(
    sources: Sequence[_VideoSource],
    metadata_by_path: Mapping[str, VideoMetadata],
    limits: VideoLibraryLimits,
    policy: MetadataCandidatePolicy = MetadataCandidatePolicy(),
) -> Tuple[Tuple[Tuple[str, str], ...], int, bool]:
    buckets: Dict[Tuple[int, int], list[str]] = {}
    for source in sources:
        metadata = metadata_by_path.get(source.path)
        if metadata is None:
            continue
        key = (_aspect_bucket(metadata, policy), _duration_bucket(metadata))
        buckets.setdefault(key, []).append(source.path)
    for values in buckets.values():
        values.sort(key=_path_key)

    maximum_duration_delta = (
        int(math.ceil(math.log(1 / max(policy.minimum_related_duration_fraction, 1e-9)) / math.log(1.25))) + 1
    )
    candidates = []
    assessments = 0
    limited = False
    for left_key, right_key in _compatible_bucket_pairs(
        buckets,
        maximum_duration_delta,
    ):
        left_values = buckets[left_key]
        right_values = buckets[right_key]
        for first_index, first_path in enumerate(left_values):
            start = first_index + 1 if left_key == right_key else 0
            for second_path in right_values[start:]:
                assessments += 1
                if assessments > limits.maximum_candidate_assessments:
                    return tuple(candidates), limits.maximum_candidate_assessments, True
                assessment = classify_metadata_candidate(
                    metadata_by_path[first_path],
                    metadata_by_path[second_path],
                    policy,
                )
                if not assessment.should_compare:
                    continue
                if len(candidates) >= limits.maximum_candidates:
                    return tuple(sorted(set(candidates))), assessments, True
                candidates.append(tuple(sorted((first_path, second_path), key=_path_key)))
    return tuple(sorted(set(candidates))), assessments, limited


def _compatible_bucket_pairs(
    buckets: Mapping[Tuple[int, int], Sequence[str]],
    maximum_duration_delta: int,
) -> Iterator[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Yield only populated neighboring buckets in canonical order.

    Looking up the finite aspect/duration neighborhood directly keeps sparse
    libraries linear in their number of populated buckets.  In particular, no
    all-bucket-pairs loop exists outside the candidate-assessment limit.
    """

    for left_key in sorted(buckets):
        for aspect_bucket in range(left_key[0] - 1, left_key[0] + 2):
            for duration_bucket in range(
                left_key[1] - maximum_duration_delta,
                left_key[1] + maximum_duration_delta + 1,
            ):
                right_key = (aspect_bucket, duration_bucket)
                if right_key < left_key or buckets.get(right_key) is None:
                    continue
                yield left_key, right_key


def _compare_artifacts(
    first: VideoArtifact,
    second: VideoArtifact,
    threshold: float,
) -> Tuple[Optional[Dict[str, object]], Optional[_LibraryIssue]]:
    frames = align_frame_fingerprints(first.frames, second.frames)
    if frames.state is not AlignmentState.COMPLETE:
        return None, _LibraryIssue(
            "frame_alignment_{}".format(frames.state.value),
            "frame fingerprint alignment did not complete",
            "{} | {}".format(first.source.path, second.source.path),
            resource_limit=frames.state is AlignmentState.RESOURCE_LIMIT,
        )
    audio = None
    if first.audio is not None and second.audio is not None:
        audio = align_audio_fingerprints(first.audio, second.audio)
        if audio.state is not AlignmentState.COMPLETE:
            return None, _LibraryIssue(
                "audio_alignment_{}".format(audio.state.value),
                "audio fingerprint alignment did not complete",
                "{} | {}".format(first.source.path, second.source.path),
                resource_limit=audio.state is AlignmentState.RESOURCE_LIMIT,
            )
    relation = classify_video_relation(
        first,
        second,
        frame_alignment=frames,
        audio_alignment=audio,
        policy=RelationPolicy(related_score=threshold),
    )
    if relation is None or relation.score < threshold:
        return None, None
    if relation.exact_proof is not None or relation.allows_automatic_destructive_action:
        raise RuntimeError("perceptual video library scan attempted to emit exact proof")
    value = relation.to_dict()
    value["exact_proof"] = None
    value["allows_automatic_destructive_action"] = False
    return value, None


def _build_groups(
    relations: Sequence[Mapping[str, object]],
    artifacts: Mapping[str, VideoArtifact],
    maximum_groups: int,
) -> Tuple[Tuple[Dict[str, object], ...], bool]:
    union = _UnionFind()
    for relation in relations:
        union.union(str(relation["first_path"]), str(relation["second_path"]))
    components: Dict[str, set[str]] = {}
    for path in union.parent:
        components.setdefault(union.find(path), set()).add(path)
    groups = []
    limited = False
    for members in sorted((tuple(sorted(values, key=_path_key)) for values in components.values())):
        if len(groups) >= maximum_groups:
            limited = True
            break
        member_set = set(members)
        evidence = tuple(
            relation
            for relation in relations
            if str(relation["first_path"]) in member_set and str(relation["second_path"]) in member_set
        )
        member_records = []
        for path in members:
            artifact = artifacts[path]
            assert artifact.metadata is not None
            member_records.append(
                {
                    "path": path,
                    "size": artifact.source.size,
                    "mtime_ns": artifact.source.mtime_ns,
                    "metadata": artifact.metadata.to_dict(),
                }
            )
        identity = json.dumps(
            {
                "members": list(members),
                "relations": list(evidence),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        groups.append(
            {
                "schema": VIDEO_LIBRARY_GROUP_SCHEMA,
                "schema_version": VIDEO_LIBRARY_SCHEMA_VERSION,
                "group_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "members": member_records,
                "relations": list(evidence),
                "review_only": True,
                "byte_exact_proof": None,
                "allows_automatic_destructive_action": False,
            }
        )
    return tuple(groups), limited


def _outcome_issue(
    state: CommandState,
    message: Optional[str],
    source: str,
) -> _LibraryIssue:
    base = message or "FFprobe metadata command failed"
    return _LibraryIssue(
        "metadata_probe_{}".format(state.value),
        base,
        source,
        ToolName.FFPROBE.value,
        resource_limit=state is CommandState.OUTPUT_LIMIT,
        cancelled=state is CommandState.CANCELLED,
        timed_out=state is CommandState.TIMED_OUT,
        missing_tool=state is CommandState.MISSING_EXECUTABLE,
    )


def _capability_issue(capability: ToolCapability) -> _LibraryIssue:
    return _LibraryIssue(
        "tool_{}".format(capability.state.value),
        capability.message,
        tool=capability.tool.value,
        cancelled=capability.state is ToolState.CANCELLED,
        timed_out=capability.state is ToolState.TIMED_OUT,
        missing_tool=capability.state is ToolState.MISSING,
    )


def _report(
    *,
    roots: Sequence[Path],
    threshold: float,
    limits: VideoLibraryLimits,
    sources: Sequence[_VideoSource],
    metadata_count: int,
    metadata_failures: int,
    discovered: int,
    skipped: int,
    walk_failed: int,
    candidate_assessments: int,
    candidates: int,
    comparisons: int,
    relations: int,
    groups: Sequence[Mapping[str, object]],
    issues: Sequence[_LibraryIssue],
    cache: _ArtifactCache,
    started_at_ns: int,
) -> Dict[str, object]:
    resource_limited = any(issue.resource_limit for issue in issues)
    cancelled = any(issue.cancelled for issue in issues)
    if resource_limited:
        receipt_status = ScanStatus.RESOURCE_LIMIT
        state = AnalysisState.PARTIAL_RESOURCE_LIMIT
    elif cancelled:
        receipt_status = ScanStatus.CANCELLED
        state = AnalysisState.PARTIAL_CANCELLED
    elif any(issue.missing_tool for issue in issues):
        receipt_status = ScanStatus.COMPLETE_WITH_SKIPS
        state = AnalysisState.PARTIAL_MISSING_TOOL
    elif any(issue.timed_out for issue in issues):
        receipt_status = ScanStatus.COMPLETE_WITH_SKIPS
        state = AnalysisState.PARTIAL_TIMEOUT
    elif issues:
        receipt_status = ScanStatus.COMPLETE_WITH_SKIPS
        state = AnalysisState.PARTIAL_TOOL_ERROR
    else:
        receipt_status = ScanStatus.COMPLETE
        state = AnalysisState.COMPLETE

    analyzed = metadata_count
    failed = metadata_failures + walk_failed
    accounted = analyzed + skipped + failed
    if accounted < discovered:
        skipped += discovered - accounted
    elif accounted > discovered:
        discovered = accounted
    scan_id = str(uuid.uuid4())
    receipt = ScanReceipt(
        scan_id=scan_id,
        status=receipt_status,
        discovered=discovered,
        analyzed=analyzed,
        skipped=skipped,
        failed=failed,
        started_at_ns=started_at_ns,
        finished_at_ns=time.time_ns(),
        issues=tuple(issue.receipt_issue() for issue in issues),
    )
    return {
        "schema": VIDEO_LIBRARY_SCAN_SCHEMA,
        "schema_version": VIDEO_LIBRARY_SCHEMA_VERSION,
        "scan_id": scan_id,
        "created_at_ns": receipt.finished_at_ns,
        "state": state.value,
        "partial": state is not AnalysisState.COMPLETE,
        "roots": [str(root) for root in roots],
        "threshold": threshold,
        "limits": {
            "maximum_files": limits.maximum_files,
            "maximum_candidate_assessments": limits.maximum_candidate_assessments,
            "maximum_candidates": limits.maximum_candidates,
            "maximum_comparisons": limits.maximum_comparisons,
            "maximum_fingerprint_files": limits.maximum_fingerprint_files,
            "maximum_groups": limits.maximum_groups,
            "probe_timeout_seconds": limits.probe_timeout_seconds,
            "maximum_probe_output_bytes": limits.maximum_probe_output_bytes,
            "maximum_scan_seconds": limits.maximum_scan_seconds,
        },
        "issues": [issue.to_dict() for issue in issues],
        "receipt": {
            "status": receipt.status.value,
            "complete": receipt.complete,
            "discovered": receipt.discovered,
            "analyzed": receipt.analyzed,
            "skipped": receipt.skipped,
            "failed": receipt.failed,
        },
        "cache": {
            "path": str(cache.path) if cache.path is not None else None,
            "persistent": cache.path is not None,
            "hits": cache.hits,
            "misses": cache.misses,
            "writes": cache.writes,
        },
        "groups": list(groups),
        "safety": {
            "source_read_only": True,
            "review_only": True,
            "byte_exact_proof": False,
            "destructive_actions_allowed": False,
        },
        "summary": {
            "video_files": len(sources),
            "metadata_complete": metadata_count,
            "candidate_assessments": candidate_assessments,
            "candidates": candidates,
            "comparisons": comparisons,
            "relations": relations,
            "groups": len(groups),
        },
    }


__all__ = [
    "VIDEO_EXTENSIONS",
    "VIDEO_LIBRARY_GROUP_SCHEMA",
    "VIDEO_LIBRARY_RECORD_SCHEMA",
    "VIDEO_LIBRARY_SCAN_SCHEMA",
    "VideoLibraryLimits",
    "VideoLibraryScanner",
]
