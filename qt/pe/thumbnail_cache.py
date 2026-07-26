# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Persistent, bounded, no-follow cache for lazily generated thumbnails."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QSize, QStandardPaths
from PyQt6.QtGui import QImage, QImageReader

from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_file_generation_token,
    get_file_generation_token_from_fd,
)
from core.file_identity import (
    FileIdentityError,
    IdentityVerdict,
    get_file_identity,
    get_file_identity_from_fd,
    same_physical_file,
)
from core.safe_walk import is_reparse_point

DEFAULT_MAX_ENTRIES = 4096
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
MAXIMUM_ENTRY_BYTES = 64 * 1024 * 1024
_CACHE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_SHARD_PATTERN = re.compile(r"[0-9a-f]{2}")
_ENTRY_PATTERN = re.compile(r"([0-9a-f]{64})\.png")


class ThumbnailCacheSafetyError(RuntimeError):
    """A cache directory or entry could escape the cache trust boundary."""


def normalized_absolute_path(path) -> str:
    """Return the platform-normalized absolute path used in cache identities."""

    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def thumbnail_cache_key(
    path,
    size: int,
    mtime_ns: int,
    generation_token: bytes,
    thumbnail_size: QSize,
) -> str:
    """Build a non-reversible identity from path, file state, and output size."""

    if not thumbnail_size.isValid() or thumbnail_size.isEmpty():
        raise ValueError("thumbnail_size must be positive")
    for name, value in (("size", size), ("mtime_ns", mtime_ns)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("{} must be a non-negative integer".format(name))
    token = FileGenerationToken.from_encoded(generation_token)
    identity = (
        "\0".join(
            (
                normalized_absolute_path(path),
                str(size),
                str(mtime_ns),
                f"{thumbnail_size.width()}x{thumbnail_size.height()}",
            )
        ).encode("utf-8", errors="surrogatepass")
        + b"\0"
        + token.encoded
    )
    return hashlib.sha256(identity).hexdigest()


def default_thumbnail_cache_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    if not base:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not base:
        raise RuntimeError("Qt could not locate a writable per-user application data directory")
    return Path(base) / "dupeguru-neo" / "picture-thumbnails-v2"


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


def _first_missing_component(path: Path) -> Path | None:
    for component in _component_paths(path):
        try:
            os.lstat(component)
        except FileNotFoundError:
            return component
        except OSError as error:
            raise ThumbnailCacheSafetyError(
                "thumbnail cache component is unavailable: '{}'".format(component)
            ) from error
    return None


def _current_posix_uid() -> int:
    return os.geteuid()


def _is_at_or_below(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    normalized_path = os.path.normcase(os.path.abspath(os.fspath(path)))
    normalized_root = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _validate_directory_stat(path: Path, directory_stat, *, require_private: bool = False) -> None:
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or is_reparse_point(directory_stat)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise ThumbnailCacheSafetyError("thumbnail cache components must be plain directories: '{}'".format(path))
    if os.name != "nt" and require_private:
        if int(directory_stat.st_uid) != _current_posix_uid():
            raise ThumbnailCacheSafetyError(
                "thumbnail cache app directories must be owned by the current user: '{}'".format(path)
            )
        if stat.S_IMODE(directory_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise ThumbnailCacheSafetyError(
                "thumbnail cache app directories must not be group/world-writable: '{}'".format(path)
            )


def _validate_existing_directory_prefix(path: Path, *, private_root: Path | None = None) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if str(absolute).startswith(("\\\\", "//")):
        raise ThumbnailCacheSafetyError("thumbnail cache cannot use a UNC path")
    complete = True
    for component in _component_paths(absolute):
        if not complete:
            continue
        try:
            component_stat = os.lstat(component)
        except FileNotFoundError:
            complete = False
            continue
        except OSError as error:
            raise ThumbnailCacheSafetyError(
                "thumbnail cache component is unavailable: '{}'".format(component)
            ) from error
        _validate_directory_stat(
            component,
            component_stat,
            require_private=_is_at_or_below(component, private_root),
        )
    if os.name == "nt":
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(Path(absolute.anchor)))
        if drive_type in {0, 1, 4}:
            raise ThumbnailCacheSafetyError("thumbnail cache must be stored on a known local drive")
    return complete


def _ensure_plain_directory_tree(path: Path, *, private_root: Path | None = None) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if str(absolute).startswith(("\\\\", "//")):
        raise ThumbnailCacheSafetyError("thumbnail cache cannot use a UNC path")
    for component in _component_paths(absolute):
        try:
            component_stat = os.lstat(component)
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise ThumbnailCacheSafetyError(
                    "thumbnail cache directory could not be created safely: '{}'".format(component)
                ) from error
            try:
                component_stat = os.lstat(component)
            except OSError as error:
                raise ThumbnailCacheSafetyError(
                    "created thumbnail cache directory is unavailable: '{}'".format(component)
                ) from error
        except OSError as error:
            raise ThumbnailCacheSafetyError(
                "thumbnail cache component is unavailable: '{}'".format(component)
            ) from error
        _validate_directory_stat(
            component,
            component_stat,
            require_private=_is_at_or_below(component, private_root),
        )


def _target_snapshot(path: Path, *, allow_missing: bool, private_root: Path | None = None):
    if not _validate_existing_directory_prefix(path.parent, private_root=private_root):
        if allow_missing:
            return None
        raise ThumbnailCacheSafetyError("thumbnail cache target parent does not exist: '{}'".format(path.parent))
    try:
        target_stat = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ThumbnailCacheSafetyError("thumbnail cache entry disappeared: '{}'".format(path))
    except OSError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache entry is unavailable: '{}'".format(path)) from error
    if stat.S_ISLNK(target_stat.st_mode) or is_reparse_point(target_stat) or not stat.S_ISREG(target_stat.st_mode):
        raise ThumbnailCacheSafetyError("thumbnail cache entry must be a plain regular file: '{}'".format(path))
    if getattr(target_stat, "st_nlink", None) != 1:
        raise ThumbnailCacheSafetyError(
            "thumbnail cache entry must have exactly one filesystem link: '{}'".format(path)
        )
    try:
        identity = get_file_identity(
            path,
            follow_symlinks=False,
            stat_result=target_stat,
        )
    except FileIdentityError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache entry identity is unavailable: '{}'".format(path)) from error
    return target_stat, identity


def _target_snapshot_with_generation(path: Path, *, allow_missing: bool, private_root: Path | None = None):
    snapshot = _target_snapshot(
        path,
        allow_missing=allow_missing,
        private_root=private_root,
    )
    if snapshot is None:
        return None
    target_stat, identity = snapshot
    try:
        generation = get_file_generation_token(
            path,
            follow_symlinks=False,
            stat_result=target_stat,
            expected_identity=identity,
        )
    except FileGenerationError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache entry generation is unavailable: '{}'".format(path)) from error
    return target_stat, identity, generation


def _same_snapshot(left, right) -> bool:
    if left is None or right is None:
        return left is right
    left_stat, left_identity, left_generation = left
    right_stat, right_identity, right_generation = right
    return (
        same_physical_file(left_identity, right_identity).verdict is IdentityVerdict.SAME
        and int(left_stat.st_size) == int(right_stat.st_size)
        and int(left_stat.st_mtime_ns) == int(right_stat.st_mtime_ns)
        and left_generation == right_generation
    )


def _open_output_identity(descriptor: int, path: Path):
    try:
        output_stat = os.fstat(descriptor)
    except OSError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache output could not be inspected") from error
    if (
        not stat.S_ISREG(output_stat.st_mode)
        or is_reparse_point(output_stat)
        or getattr(output_stat, "st_nlink", None) != 1
    ):
        raise ThumbnailCacheSafetyError("thumbnail cache output is not a single-link regular file")
    try:
        identity = get_file_identity_from_fd(
            descriptor,
            path,
            stat_result=output_stat,
        )
    except FileIdentityError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache output identity is unavailable") from error
    return output_stat, identity


def _open_output_snapshot(descriptor: int, path: Path):
    output_stat, identity = _open_output_identity(descriptor, path)
    try:
        generation = get_file_generation_token_from_fd(
            descriptor,
            path,
            stat_result=output_stat,
            expected_identity=identity,
        )
    except FileGenerationError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache output generation is unavailable") from error
    return output_stat, identity, generation


def _open_output_readonly(path: Path) -> int:
    """Open one completed output without following or sharing mutations."""

    if os.name == "nt":
        import ctypes
        import msvcrt

        from core.file_identity import (
            _FILE_FLAG_OPEN_REPARSE_POINT,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ,
            _INVALID_HANDLE_VALUE,
            _OPEN_EXISTING,
            _close_handle,
            _create_file,
            windows_extended_path,
        )

        raw_handle = _create_file(
            windows_extended_path(path),
            _FILE_READ_ATTRIBUTES | 0x0001,  # FILE_READ_DATA
            # The verified name cannot be written, renamed, or deleted while
            # its generation and bytes are inspected.
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(
                raw_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_handle(raw_handle)
            raise

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ThumbnailCacheSafetyError("this platform cannot reopen thumbnail outputs without following links")
    return os.open(path, flags | no_follow)


def _read_open_output(descriptor: int, maximum_bytes: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            raise ThumbnailCacheSafetyError("thumbnail cache output exceeds its verified payload size")


def _encode_png(image: QImage) -> bytes | None:
    """Encode before touching the final cache path."""

    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None
    try:
        if not image.save(buffer, "PNG"):
            return None
        payload = bytes(buffer.data())
    finally:
        buffer.close()
    if not payload or len(payload) > MAXIMUM_ENTRY_BYTES:
        return None
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("thumbnail cache output write made no progress")
        offset += written


def _create_entry_exclusive(
    path: Path,
    payload: bytes,
    *,
    private_root: Path | None = None,
):
    """Create one entry without any operation that can replace a target."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | _no_follow_open_flag()
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        # Validate a raced target, but never retry or replace it.  A target
        # that disappeared again is still treated as a lost publication race.
        _target_snapshot(
            path,
            allow_missing=True,
            private_root=private_root,
        )
        return None
    except OSError as error:
        raise ThumbnailCacheSafetyError(
            "thumbnail cache entry could not be created exclusively: '{}'".format(path)
        ) from error

    try:
        os.set_inheritable(descriptor, False)
        opened_before = _open_output_identity(descriptor, path)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        opened_after = _open_output_identity(descriptor, path)
        if same_physical_file(
            opened_before[1],
            opened_after[1],
        ).verdict is not IdentityVerdict.SAME or int(
            opened_after[0].st_size
        ) != len(payload):
            raise ThumbnailCacheSafetyError("thumbnail cache output changed while it was being created")
    except OSError as error:
        # The create-only target may remain incomplete.  It is deliberately
        # not unlinked here: a path-based cleanup after a race could delete a
        # third-party replacement.  A later validated load can reject it.
        raise ThumbnailCacheSafetyError(
            "thumbnail cache entry could not be written safely: '{}'".format(path)
        ) from error
    finally:
        os.close(descriptor)

    try:
        readonly_descriptor = _open_output_readonly(path)
    except OSError as error:
        raise ThumbnailCacheSafetyError(
            "thumbnail cache output could not be reopened without mutation sharing: '{}'".format(path)
        ) from error
    try:
        verified_before = _open_output_snapshot(readonly_descriptor, path)
        if same_physical_file(
            opened_after[1],
            verified_before[1],
        ).verdict is not IdentityVerdict.SAME or int(
            verified_before[0].st_size
        ) != len(payload):
            raise ThumbnailCacheSafetyError("thumbnail cache target does not identify the exclusively created output")
        verified_payload = _read_open_output(
            readonly_descriptor,
            len(payload),
        )
        verified_after = _open_output_snapshot(readonly_descriptor, path)
        if verified_payload != payload or not _same_snapshot(
            verified_before,
            verified_after,
        ):
            raise ThumbnailCacheSafetyError("thumbnail cache output changed while its bytes were being verified")
        installed = _target_snapshot_with_generation(
            path,
            allow_missing=False,
            private_root=private_root,
        )
        if not _same_snapshot(verified_after, installed):
            raise ThumbnailCacheSafetyError("thumbnail cache target changed while its verified handle was open")
    except OSError as error:
        raise ThumbnailCacheSafetyError(
            "thumbnail cache output could not be verified safely: '{}'".format(path)
        ) from error
    finally:
        os.close(readonly_descriptor)

    stable_installed = _target_snapshot_with_generation(
        path,
        allow_missing=False,
        private_root=private_root,
    )
    if not _same_snapshot(installed, stable_installed):
        raise ThumbnailCacheSafetyError("thumbnail cache target changed after exclusive creation")
    return installed


def _no_follow_open_flag() -> int:
    if hasattr(os, "O_NOFOLLOW"):
        return os.O_NOFOLLOW
    if os.name == "nt":
        # Windows reparse points are rejected using file attributes and
        # FILE_FLAG_OPEN_REPARSE_POINT identities around the CRT read.
        return 0
    raise ThumbnailCacheSafetyError("this platform cannot open thumbnail entries without following links")


def _read_entry(path: Path, maximum_bytes: int, *, private_root: Path | None = None):
    before = _target_snapshot(
        path,
        allow_missing=False,
        private_root=private_root,
    )
    before_stat, before_identity = before
    try:
        before_generation = get_file_generation_token(
            path,
            follow_symlinks=False,
            stat_result=before_stat,
            expected_identity=before_identity,
        )
    except FileGenerationError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache entry generation is unavailable before reading") from error
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _no_follow_open_flag()
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ThumbnailCacheSafetyError(
            "thumbnail cache entry could not be opened safely: '{}'".format(path)
        ) from error
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_point(opened_before)
            or getattr(opened_before, "st_nlink", None) != 1
        ):
            raise ThumbnailCacheSafetyError("opened thumbnail cache entry is not a single-link regular file")
        try:
            opened_generation_before = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_before,
                expected_identity=before_identity,
            )
        except FileGenerationError as error:
            raise ThumbnailCacheSafetyError("opened thumbnail cache entry generation is unavailable") from error
        chunks = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        try:
            opened_generation_after = get_file_generation_token_from_fd(
                descriptor,
                path,
                stat_result=opened_after,
                expected_identity=before_identity,
            )
        except FileGenerationError as error:
            raise ThumbnailCacheSafetyError("thumbnail cache entry generation is unavailable after reading") from error
    finally:
        os.close(descriptor)
    after_stat, after_identity = _target_snapshot(
        path,
        allow_missing=False,
        private_root=private_root,
    )
    try:
        after_generation = get_file_generation_token(
            path,
            follow_symlinks=False,
            stat_result=after_stat,
            expected_identity=after_identity,
        )
    except FileGenerationError as error:
        raise ThumbnailCacheSafetyError("thumbnail cache entry generation is unavailable after reading") from error

    def generation(item, token):
        return (
            int(item.st_size),
            int(item.st_mtime_ns),
            token,
        )

    if not (
        generation(before_stat, before_generation)
        == generation(opened_before, opened_generation_before)
        == generation(opened_after, opened_generation_after)
        == generation(after_stat, after_generation)
    ):
        raise ThumbnailCacheSafetyError("thumbnail cache entry changed while it was being read")
    if same_physical_file(before_identity, after_identity).verdict is not IdentityVerdict.SAME:
        raise ThumbnailCacheSafetyError("thumbnail cache entry identity changed while it was being read")
    if len(payload) > maximum_bytes:
        raise ThumbnailCacheSafetyError("thumbnail cache entry exceeds the configured safe size")
    return payload, before_identity


class ThumbnailDiskCache:
    """Thread-safe PNG cache with strict path and link validation."""

    def __init__(
        self,
        cache_dir=None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if cache_dir is None:
            self.cache_dir = Path(os.path.abspath(os.fspath(default_thumbnail_cache_dir())))
            self._private_root = self.cache_dir.parent
        else:
            self.cache_dir = Path(os.path.abspath(os.fspath(cache_dir)))
            self._private_root = _first_missing_component(self.cache_dir) or self.cache_dir
        _validate_existing_directory_prefix(
            self.cache_dir,
            private_root=self._private_root,
        )
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._known_entries = None
        self._known_bytes = None
        self._stores_since_reconcile = 0

    def path_for_key(self, key: str) -> Path:
        if _CACHE_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("thumbnail cache keys must be lowercase SHA-256 hex")
        return self.cache_dir / key[:2] / f"{key}.png"

    def load(self, key: str, max_size: QSize) -> QImage | None:
        """Load a valid cache entry without following any filesystem alias."""

        if not max_size.isValid() or max_size.isEmpty():
            raise ValueError("max_size must be positive")
        path = self.path_for_key(key)
        with self._lock:
            if (
                _target_snapshot(
                    path,
                    allow_missing=True,
                    private_root=self._private_root,
                )
                is None
            ):
                return None
            maximum_bytes = min(self.max_bytes, MAXIMUM_ENTRY_BYTES)
            payload, identity = _read_entry(
                path,
                maximum_bytes,
                private_root=self._private_root,
            )
            buffer = QBuffer()
            buffer.setData(payload)
            if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
                raise ThumbnailCacheSafetyError("thumbnail cache buffer could not be opened")
            reader = QImageReader(buffer)
            reader.setFormat(b"PNG")
            reader.setDecideFormatFromContent(True)
            stored_size = reader.size()
            if (
                not stored_size.isValid()
                or stored_size.isEmpty()
                or stored_size.width() > max_size.width()
                or stored_size.height() > max_size.height()
            ):
                del reader
                buffer.close()
                self._remove_locked(path, expected_identity=identity)
                return None
            image = reader.read()
            del reader
            buffer.close()
            if image.isNull():
                self._remove_locked(path, expected_identity=identity)
                return None
            return image

    def store(self, key: str, image: QImage) -> bool:
        """Create one safe entry; an existing target is never replaced."""

        if image.isNull():
            return False
        payload = _encode_png(image)
        if payload is None:
            return False
        path = self.path_for_key(key)
        with self._lock:
            _ensure_plain_directory_tree(
                self.cache_dir,
                private_root=self._private_root,
            )
            _ensure_plain_directory_tree(
                path.parent,
                private_root=self._private_root,
            )
            self._ensure_stats_locked()
            installed = _create_entry_exclusive(
                path,
                payload,
                private_root=self._private_root,
            )
            if installed is None:
                # Another process may have won the create-only race after the
                # last reconciliation.  Reconcile lazily on the next store.
                self._known_entries = None
                self._known_bytes = None
                return False
            new_size = int(installed[0].st_size)
            self._known_entries += 1
            self._known_bytes += new_size
            self._stores_since_reconcile += 1
            if (
                self._known_entries > self.max_entries
                or self._known_bytes > self.max_bytes
                or self._stores_since_reconcile >= 128
            ):
                self._cleanup_locked()
            return True

    def cleanup(self) -> tuple[int, int]:
        """Enforce bounds without traversing links or deleting aliased files."""

        with self._lock:
            return self._cleanup_locked()

    def usage(self) -> tuple[int, int]:
        with self._lock:
            entries = self._entries_locked()
            return len(entries), sum(size for _, size, _, _ in entries)

    def _ensure_stats_locked(self):
        if self._known_entries is None or self._known_bytes is None:
            self._cleanup_locked()

    def _entries_locked(self):
        if not _validate_existing_directory_prefix(
            self.cache_dir,
            private_root=self._private_root,
        ):
            return []
        entries = []
        try:
            shards = tuple(os.scandir(self.cache_dir))
        except OSError as error:
            raise ThumbnailCacheSafetyError("thumbnail cache directory could not be enumerated") from error
        for shard_entry in shards:
            shard_path = Path(shard_entry.path)
            try:
                # ``DirEntry.stat()`` exposes ``st_nlink == 0`` on Windows
                # because its fast-path metadata omits the link count.  A real
                # lstat is required before making a hard-link safety decision.
                shard_stat = os.lstat(shard_path)
            except OSError as error:
                raise ThumbnailCacheSafetyError(
                    "thumbnail cache shard could not be inspected: '{}'".format(shard_path)
                ) from error
            if stat.S_ISLNK(shard_stat.st_mode) or is_reparse_point(shard_stat):
                raise ThumbnailCacheSafetyError(
                    "thumbnail cache cleanup refuses linked shards: '{}'".format(shard_path)
                )
            if not stat.S_ISDIR(shard_stat.st_mode):
                continue
            _validate_directory_stat(
                shard_path,
                shard_stat,
                require_private=_is_at_or_below(
                    shard_path,
                    self._private_root,
                ),
            )
            if _SHARD_PATTERN.fullmatch(shard_entry.name) is None:
                continue
            try:
                files = tuple(os.scandir(shard_path))
            except OSError as error:
                raise ThumbnailCacheSafetyError(
                    "thumbnail cache shard could not be enumerated: '{}'".format(shard_path)
                ) from error
            for file_entry in files:
                path = Path(file_entry.path)
                try:
                    file_stat = os.lstat(path)
                except OSError as error:
                    raise ThumbnailCacheSafetyError(
                        "thumbnail cache entry could not be inspected: '{}'".format(path)
                    ) from error
                if stat.S_ISLNK(file_stat.st_mode) or is_reparse_point(file_stat):
                    raise ThumbnailCacheSafetyError("thumbnail cache cleanup refuses linked entries: '{}'".format(path))
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                if getattr(file_stat, "st_nlink", None) != 1:
                    raise ThumbnailCacheSafetyError(
                        "thumbnail cache cleanup refuses hard-linked entries: '{}'".format(path)
                    )
                match = _ENTRY_PATTERN.fullmatch(file_entry.name)
                if match is None or match.group(1)[:2] != shard_entry.name:
                    continue
                try:
                    identity = get_file_identity(
                        path,
                        follow_symlinks=False,
                        stat_result=file_stat,
                    )
                except FileIdentityError as error:
                    raise ThumbnailCacheSafetyError(
                        "thumbnail cache entry identity is unavailable: '{}'".format(path)
                    ) from error
                entries.append(
                    (
                        int(file_stat.st_mtime_ns),
                        int(file_stat.st_size),
                        path,
                        identity,
                    )
                )
        return entries

    def _cleanup_locked(self) -> tuple[int, int]:
        entries = self._entries_locked()
        count = len(entries)
        total = sum(size for _, size, _, _ in entries)
        entries.sort(key=lambda item: (item[0], str(item[2])))
        for _, size, path, identity in entries:
            if count <= self.max_entries and total <= self.max_bytes:
                break
            self._remove_locked(path, expected_identity=identity)
            count -= 1
            total -= size
        self._known_entries = count
        self._known_bytes = total
        self._stores_since_reconcile = 0
        return count, total

    def _remove_locked(self, path: Path, *, expected_identity):
        target = _target_snapshot(
            path,
            allow_missing=False,
            private_root=self._private_root,
        )
        target_stat, current_identity = target
        if same_physical_file(expected_identity, current_identity).verdict is not IdentityVerdict.SAME:
            raise ThumbnailCacheSafetyError("thumbnail cache entry identity changed before removal")
        size = int(target_stat.st_size)
        try:
            path.unlink()
        except OSError as error:
            raise ThumbnailCacheSafetyError(
                "thumbnail cache entry could not be removed safely: '{}'".format(path)
            ) from error
        if self._known_entries is not None and self._known_bytes is not None:
            self._known_entries = max(0, self._known_entries - 1)
            self._known_bytes = max(0, self._known_bytes - size)


__all__ = [
    "ThumbnailCacheSafetyError",
    "ThumbnailDiskCache",
    "default_thumbnail_cache_dir",
    "normalized_absolute_path",
    "thumbnail_cache_key",
]
