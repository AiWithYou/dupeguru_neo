# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Virtualized, safety-aware image review widgets.

The gallery deliberately keeps one lightweight model row per file. Thumbnails
are decoded only when Qt asks for a row's decoration, and the delegate paints
all overlays itself instead of creating one QWidget per image.
"""

from __future__ import annotations

import os
import stat
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from enum import Enum, IntEnum
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import (
    QAbstractListModel,
    QBuffer,
    QEvent,
    QIODevice,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    QThreadPool,
    QTimer,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QImage, QImageReader, QKeyEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from core.destructive_eligibility import Eligibility, EligibilityCode, evaluate_duplicate
from core.engine import VerificationKind
from core.file_generation import (
    FileGenerationToken,
    get_file_generation_token,
    get_file_generation_token_from_fd,
)
from core.file_identity import FileIdentityError, get_file_identity
from core.fs import _open_readonly_no_follow
from core.safe_walk import is_reparse_point
from hscommon.trans import trget
from qt.pe.comparison import QIMAGE_READER_ALLOCATION_LOCK
from qt.pe.thumbnail_cache import ThumbnailDiskCache, thumbnail_cache_key

tr = trget("ui")

DEFAULT_MAX_THUMBNAIL_SOURCE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_THUMBNAIL_SOURCE_PIXELS = 64_000_000
DEFAULT_THUMBNAIL_ALLOCATION_LIMIT_MB = 128
DEFAULT_MAX_THUMBNAIL_WORKERS = 3
DEFAULT_MAX_PENDING_THUMBNAILS = 64


class ReviewRelation(str, Enum):
    """Evidence class shown by the review UI."""

    BYTE_VERIFIED_EXACT = "byte_verified_exact"
    VISUAL_APPROXIMATE = "visual_approximate"
    SEMANTIC_RELATED = "semantic_related"
    UNVERIFIED = "unverified"


RELATION_COLORS = {
    ReviewRelation.BYTE_VERIFIED_EXACT: "#2EAD62",
    ReviewRelation.VISUAL_APPROXIMATE: "#E3B341",
    ReviewRelation.SEMANTIC_RELATED: "#3487E8",
    ReviewRelation.UNVERIFIED: "#7C8797",
}

RELATION_LABELS = {
    ReviewRelation.BYTE_VERIFIED_EXACT: tr("Byte-verified exact"),
    ReviewRelation.VISUAL_APPROXIMATE: tr("Visual approximate"),
    ReviewRelation.SEMANTIC_RELATED: tr("Visually related"),
    ReviewRelation.UNVERIFIED: tr("Unverified"),
}


class ReviewRole(IntEnum):
    """Additional roles exposed by :class:`ReviewGalleryModel`."""

    FILE = Qt.ItemDataRole.UserRole.value + 1
    PATH = FILE + 1
    RELATION = FILE + 2
    RELATION_COLOR = FILE + 3
    METADATA = FILE + 4
    KEEPER = FILE + 5
    DELETE_CANDIDATE = FILE + 6
    DELETE_ENABLED = FILE + 7
    DELETE_REASON = FILE + 8
    COMPARISON_POOL = FILE + 9
    THUMBNAIL_KEY = FILE + 10


def relation_for_group(group) -> ReviewRelation:
    """Map typed or future relation metadata to the three review classes.

    ``verification_kind`` remains authoritative for exactness. Optional
    relation fields let the UI render semantic result providers without
    depending on a particular provider implementation.
    """

    values = []
    for attribute in ("verification_kind", "relation_kind", "relation", "match_kind"):
        value = getattr(group, attribute, None)
        if value is None:
            continue
        values.append(str(getattr(value, "value", value)).casefold())

    if VerificationKind.VERIFIED_EXACT.value in values:
        return ReviewRelation.BYTE_VERIFIED_EXACT
    if any("semantic" in value or "related" in value for value in values):
        return ReviewRelation.SEMANTIC_RELATED
    if any(
        token in value
        for value in values
        for token in (
            "similar",
            "visual",
            "approximate",
            "perceptual",
            "transformed",
            "crop_candidate",
        )
    ):
        return ReviewRelation.VISUAL_APPROXIMATE
    return ReviewRelation.UNVERIFIED


def relation_for_item(item, fallback: ReviewRelation) -> ReviewRelation:
    """Return optional row-level evidence without weakening group exactness."""

    value = getattr(item, "review_relation", None)
    if isinstance(value, ReviewRelation):
        return value
    value = str(getattr(value, "value", value) or "").casefold()
    if any(token in value for token in ("semantic", "related")):
        return ReviewRelation.SEMANTIC_RELATED
    if any(
        token in value
        for token in (
            "similar",
            "visual",
            "approximate",
            "perceptual",
            "transformed",
            "crop_candidate",
        )
    ):
        return ReviewRelation.VISUAL_APPROXIMATE
    return fallback


def _group_members(group) -> list[object]:
    if group is None:
        return []
    for attribute in ("ordered", "members", "files"):
        members = getattr(group, attribute, None)
        if members is not None:
            return list(members)
    try:
        return list(group)
    except TypeError:
        return []


def _item_path(item) -> str:
    return str(getattr(item, "path", ""))


def _item_name(item) -> str:
    name = getattr(item, "name", None)
    if name:
        return str(name)
    path = _item_path(item)
    return Path(path).name if path else tr("Unknown image")


def _format_size(size) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return tr("unknown size")
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return tr("unknown size")


class _ThumbnailTaskSignals(QObject):
    loaded = pyqtSignal(str, int, object)


class _ThumbnailTaskRuntime(QObject):
    """Keep non-auto-deleting runnables alive independently of their view."""

    finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._tasks: set[object] = set()
        self.finished.connect(self.release, Qt.ConnectionType.QueuedConnection)

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    def retain(self, task):
        task.signals.setParent(self)
        self._tasks.add(task)

    @pyqtSlot(object)
    def release(self, task):
        if task not in self._tasks:
            return
        task.signals.deleteLater()
        self._tasks.remove(task)

    def is_retained(self, task) -> bool:
        return task in self._tasks


_THUMBNAIL_TASK_RUNTIME: _ThumbnailTaskRuntime | None = None


def _thumbnail_task_runtime() -> _ThumbnailTaskRuntime:
    global _THUMBNAIL_TASK_RUNTIME
    if _THUMBNAIL_TASK_RUNTIME is not None and sip.isdeleted(_THUMBNAIL_TASK_RUNTIME):
        if _THUMBNAIL_TASK_RUNTIME.task_count:
            raise RuntimeError("thumbnail tasks outlived their Qt application")
        _THUMBNAIL_TASK_RUNTIME = None
    if _THUMBNAIL_TASK_RUNTIME is None:
        _THUMBNAIL_TASK_RUNTIME = _ThumbnailTaskRuntime()
    return _THUMBNAIL_TASK_RUNTIME


def _drain_owned_thumbnail_pool(thread_pool, tasks, task_runtime):
    """Finish Python runnables before QObject deletes their private pool."""

    for task_id, task in tuple(tasks.items()):
        if thread_pool.tryTake(task):
            tasks.pop(task_id, None)
            task_runtime.release(task)
    # Calling the PyQt binding explicitly releases the GIL while it waits.
    # Letting QObject delete the child pool would wait in C++ while retaining
    # the GIL, which can deadlock a Python QRunnable finishing on a worker.
    thread_pool.waitForDone()
    for task in tuple(tasks.values()):
        task_runtime.release(task)
    tasks.clear()


@contextmanager
def _open_stable_thumbnail_source(
    path,
    *,
    expected_size,
    expected_mtime_ns,
    expected_generation_token,
    max_source_bytes,
):
    """Open and validate the exact source generation behind a thumbnail."""

    source_path = Path(os.path.abspath(os.fspath(path)))
    expected_generation_token = FileGenerationToken.from_encoded(expected_generation_token).encoded
    with _open_readonly_no_follow(source_path) as stream:
        source_stat = os.fstat(stream.fileno())
        if not stat.S_ISREG(source_stat.st_mode) or is_reparse_point(source_stat):
            raise OSError("thumbnail source is not a plain regular file")
        if expected_size is not None and int(source_stat.st_size) != int(expected_size):
            raise OSError("thumbnail source size changed before decode")
        if expected_mtime_ns is not None and int(source_stat.st_mtime_ns) != int(expected_mtime_ns):
            raise OSError("thumbnail source timestamp changed before decode")
        source_identity = get_file_identity(
            source_path,
            follow_symlinks=False,
            stat_result=source_stat,
        )
        observed_generation = get_file_generation_token_from_fd(
            stream.fileno(),
            source_path,
            stat_result=source_stat,
            expected_identity=source_identity,
        ).encoded
        if observed_generation != expected_generation_token:
            raise OSError("thumbnail source generation changed before decode")
        if int(source_stat.st_size) > max_source_bytes:
            raise OSError("thumbnail source exceeds its encoded-byte limit")
        try:
            yield stream
        finally:
            final_stat = os.fstat(stream.fileno())
            final_generation = get_file_generation_token_from_fd(
                stream.fileno(),
                source_path,
                stat_result=final_stat,
                expected_identity=source_identity,
            ).encoded
            if (
                int(final_stat.st_size) != int(source_stat.st_size)
                or int(final_stat.st_mtime_ns) != int(source_stat.st_mtime_ns)
                or final_generation != observed_generation
            ):
                raise OSError("thumbnail source changed while it was being decoded")


def _decode_stable_thumbnail_source(
    path,
    target_size,
    *,
    expected_size,
    expected_mtime_ns,
    expected_generation_token,
    max_source_bytes,
    max_source_pixels,
    allocation_limit_mb,
):
    """Decode one bounded source generation without following filesystem aliases."""

    with _open_stable_thumbnail_source(
        path,
        expected_size=expected_size,
        expected_mtime_ns=expected_mtime_ns,
        expected_generation_token=expected_generation_token,
        max_source_bytes=max_source_bytes,
    ) as stream:
        payload = stream.read(max_source_bytes + 1)
        if len(payload) > max_source_bytes:
            raise OSError("thumbnail source grew beyond its encoded-byte limit")

        buffer = QBuffer()
        buffer.setData(payload)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise OSError("thumbnail source buffer could not be opened")
        try:
            with QIMAGE_READER_ALLOCATION_LOCK:
                previous_allocation_limit = QImageReader.allocationLimit()
                try:
                    effective_allocation_limit = allocation_limit_mb
                    if previous_allocation_limit > 0:
                        effective_allocation_limit = min(
                            effective_allocation_limit,
                            previous_allocation_limit,
                        )
                    QImageReader.setAllocationLimit(effective_allocation_limit)
                    reader = QImageReader(buffer)
                    reader.setDecideFormatFromContent(True)
                    reader.setAutoTransform(True)
                    source_size = reader.size()
                    if not source_size.isValid() or source_size.isEmpty():
                        raise OSError("thumbnail source dimensions are unavailable")
                    width = int(source_size.width())
                    height = int(source_size.height())
                    if width <= 0 or height <= 0 or width * height > max_source_pixels:
                        raise OSError("thumbnail source exceeds its decoded-pixel limit")
                    reader.setScaledSize(
                        source_size.scaled(
                            target_size,
                            Qt.AspectRatioMode.KeepAspectRatio,
                        )
                    )
                    image = reader.read()
                finally:
                    reader = None
                    QImageReader.setAllocationLimit(previous_allocation_limit)
        finally:
            buffer.close()
        if image.isNull():
            raise OSError("thumbnail source could not be decoded")
        if image.width() > target_size.width() or image.height() > target_size.height():
            raise OSError("thumbnail decoder ignored the configured scaled size")
        return image


class _ThumbnailTask(QRunnable):
    def __init__(
        self,
        key: str,
        path: str,
        target_size: QSize,
        generation: int,
        disk_cache: ThumbnailDiskCache,
        *,
        expected_size,
        expected_mtime_ns,
        expected_generation_token,
        max_source_bytes,
        max_source_pixels,
        allocation_limit_mb,
        on_finished,
    ):
        super().__init__()
        self.key = key
        self.path = path
        self.target_size = QSize(target_size)
        self.generation = generation
        self.disk_cache = disk_cache
        self.expected_size = expected_size
        self.expected_mtime_ns = expected_mtime_ns
        self.expected_generation_token = FileGenerationToken.from_encoded(expected_generation_token).encoded
        self.max_source_bytes = max_source_bytes
        self.max_source_pixels = max_source_pixels
        self.allocation_limit_mb = allocation_limit_mb
        self.on_finished = on_finished
        self.signals = _ThumbnailTaskSignals()

    @pyqtSlot()
    def run(self):
        image = QImage()
        try:
            try:
                cached = self.disk_cache.load(self.key, self.target_size)
            except Exception:
                cached = None
            if isinstance(cached, QImage) and not cached.isNull():
                try:
                    with _open_stable_thumbnail_source(
                        self.path,
                        expected_size=self.expected_size,
                        expected_mtime_ns=self.expected_mtime_ns,
                        expected_generation_token=self.expected_generation_token,
                        max_source_bytes=self.max_source_bytes,
                    ):
                        image = cached
                except Exception:
                    image = QImage()
            elif self.path:
                try:
                    image = _decode_stable_thumbnail_source(
                        self.path,
                        self.target_size,
                        expected_size=self.expected_size,
                        expected_mtime_ns=self.expected_mtime_ns,
                        expected_generation_token=self.expected_generation_token,
                        max_source_bytes=self.max_source_bytes,
                        max_source_pixels=self.max_source_pixels,
                        allocation_limit_mb=self.allocation_limit_mb,
                    )
                except Exception:
                    image = QImage()
                if not image.isNull():
                    try:
                        self.disk_cache.store(self.key, image)
                    except Exception:
                        # Cache safety or capacity failures must not strand the
                        # loader's pending key or hide a successfully decoded
                        # in-memory thumbnail.
                        pass
        finally:
            # A null image is an explicit terminal result.  The GUI thread can
            # always release ``_pending`` even when cache/source decoding fails.
            try:
                self.signals.loaded.emit(self.key, self.generation, image)
            finally:
                self.on_finished(self)


class LazyThumbnailLoader(QObject):
    """Bounded asynchronous thumbnail cache.

    QImage decoding happens in a dedicated, bounded worker pool by default.
    Before a short-lived gallery destroys that pool, it drains Python workers
    through the GIL-releasing PyQt API.  QPixmap conversion and cache mutation
    happen on the GUI thread in ``_thumbnail_loaded``.  ``max_pending_tasks``
    bounds active and deferred work together.  When that bound requires a
    deferred request to be evicted, ``thumbnailDiscarded`` tells clients to
    release its waiter without immediately resubmitting it; a later natural
    paint may request it again.
    """

    thumbnailReady = pyqtSignal(str)
    thumbnailDiscarded = pyqtSignal(str)

    def __init__(
        self,
        thumbnail_size: QSize = QSize(168, 112),
        cache_limit: int = 512,
        thread_pool: QThreadPool | None = None,
        disk_cache: ThumbnailDiskCache | None = None,
        max_source_bytes: int = DEFAULT_MAX_THUMBNAIL_SOURCE_BYTES,
        max_source_pixels: int = DEFAULT_MAX_THUMBNAIL_SOURCE_PIXELS,
        allocation_limit_mb: int = DEFAULT_THUMBNAIL_ALLOCATION_LIMIT_MB,
        max_concurrent_tasks: int = DEFAULT_MAX_THUMBNAIL_WORKERS,
        max_pending_tasks: int = DEFAULT_MAX_PENDING_THUMBNAILS,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        if cache_limit < 1:
            raise ValueError("cache_limit must be positive")
        for name, value in (
            ("max_source_bytes", max_source_bytes),
            ("max_source_pixels", max_source_pixels),
            ("allocation_limit_mb", allocation_limit_mb),
            ("max_concurrent_tasks", max_concurrent_tasks),
            ("max_pending_tasks", max_pending_tasks),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))
        if not thumbnail_size.isValid() or thumbnail_size.isEmpty():
            raise ValueError("thumbnail_size must be positive")
        self.thumbnail_size = QSize(thumbnail_size)
        self.cache_limit = cache_limit
        self.max_source_bytes = max_source_bytes
        self.max_source_pixels = max_source_pixels
        self.allocation_limit_mb = allocation_limit_mb
        self.max_concurrent_tasks = max_concurrent_tasks
        self.max_pending_tasks = max_pending_tasks
        self._owns_thread_pool = thread_pool is None
        if thread_pool is None:
            thread_pool = QThreadPool(self)
            thread_pool.setMaxThreadCount(max_concurrent_tasks)
        self.thread_pool = thread_pool
        self._task_runtime = _thumbnail_task_runtime()
        self.disk_cache = disk_cache or ThumbnailDiskCache()
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._active: set[str] = set()
        self._deferred: OrderedDict[str, tuple[object, ...]] = OrderedDict()
        self._tasks: dict[tuple[int, str], _ThumbnailTask] = {}
        if self._owns_thread_pool:
            owned_pool = self.thread_pool
            owned_tasks = self._tasks
            task_runtime = self._task_runtime

            def drain_owned_pool(*_args):
                _drain_owned_thumbnail_pool(owned_pool, owned_tasks, task_runtime)

            self.destroyed.connect(drain_owned_pool)
        self._active_limit = min(max_concurrent_tasks, max_pending_tasks)
        self._generation = 0
        self._closed = False
        self._placeholder = QPixmap(self.thumbnail_size)
        self._placeholder.fill(QColor("#252A33"))

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def cached_count(self) -> int:
        return len(self._cache)

    def request(
        self,
        key: str,
        path: str,
        *,
        expected_size=None,
        expected_mtime_ns=None,
        expected_generation_token,
    ) -> QPixmap:
        if self._closed:
            return self._placeholder
        pixmap = self._cache.get(key)
        if pixmap is not None:
            self._cache.move_to_end(key)
            return pixmap
        request = (
            path,
            expected_size,
            expected_mtime_ns,
            expected_generation_token,
        )
        if key in self._deferred:
            self._deferred[key] = request
            self._deferred.move_to_end(key)
            return self._placeholder
        if key in self._active:
            return self._placeholder

        if len(self._pending) >= self.max_pending_tasks:
            if not self._deferred:
                # Running work cannot be cancelled safely.  Tell clients that
                # this request was not retained, so they do not register a
                # waiter that can never receive ``thumbnailReady``.
                self.thumbnailDiscarded.emit(key)
                return self._placeholder
            discarded_key, _request = self._deferred.popitem(last=False)
            self._pending.discard(discarded_key)
            self.thumbnailDiscarded.emit(discarded_key)

        self._pending.add(key)
        if len(self._tasks) < self._active_limit:
            self._start_request(key, request)
        else:
            self._deferred[key] = request
        return self._placeholder

    def is_pending(self, key: str) -> bool:
        return key in self._pending

    def is_cached(self, key: str) -> bool:
        return key in self._cache

    def clear(self):
        """Forget cached rows and ignore results from already-running tasks."""

        self._generation += 1
        try_take = getattr(self.thread_pool, "tryTake", None)
        if try_take is not None:
            for task_id, task in tuple(self._tasks.items()):
                if try_take(task):
                    self._tasks.pop(task_id, None)
                    self._task_runtime.release(task)
        self._cache.clear()
        self._pending.clear()
        self._active.clear()
        self._deferred.clear()

    def close(self):
        """Cancel publication from in-flight tasks and release pending keys."""

        if self._closed:
            return
        self._closed = True
        self.clear()

    @pyqtSlot(str, int, object)
    def _thumbnail_loaded(self, key: str, generation: int, image):
        # Keep the runnable alive until its queued terminal signal has been
        # delivered.  Popping before the generation checks also releases old
        # work after clear(), while this local reference protects the signal
        # sender through the rest of the slot.
        task = self._tasks.pop((generation, key), None)
        if task is None:
            return
        if self._closed:
            return
        if generation != self._generation:
            self._start_next_deferred()
            return
        if key not in self._active:
            return
        self._active.remove(key)
        self._pending.discard(key)
        self._start_next_deferred()
        pixmap = self._placeholder
        if isinstance(image, QImage) and not image.isNull():
            pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)
        self.thumbnailReady.emit(key)

    def _start_request(self, key: str, request: tuple[object, ...]):
        path, expected_size, expected_mtime_ns, expected_generation_token = request
        self._active.add(key)
        task = _ThumbnailTask(
            key,
            path,
            self.thumbnail_size,
            self._generation,
            self.disk_cache,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
            expected_generation_token=expected_generation_token,
            max_source_bytes=self.max_source_bytes,
            max_source_pixels=self.max_source_pixels,
            allocation_limit_mb=self.allocation_limit_mb,
            on_finished=self._task_runtime.finished.emit,
        )
        task.setAutoDelete(False)
        task_id = (self._generation, key)
        self._tasks[task_id] = task
        self._task_runtime.retain(task)
        task.signals.loaded.connect(self._thumbnail_loaded)
        try:
            self.thread_pool.start(task)
        except Exception:
            self._tasks.pop(task_id, None)
            self._task_runtime.release(task)
            self._active.discard(key)
            self._pending.discard(key)
            raise

    def _start_next_deferred(self):
        if self._closed or len(self._tasks) >= self._active_limit or not self._deferred:
            return
        key, request = self._deferred.popitem(last=True)
        self._start_request(key, request)


class ReviewGalleryModel(QAbstractListModel):
    """Lightweight rows for every image in one duplicate group."""

    ROW_CACHE_LIMIT = 4096

    keeperRequested = pyqtSignal(object)
    deleteCandidateRequested = pyqtSignal(object, bool)
    acceptKeeperRequested = pyqtSignal(object, object)
    blockedAction = pyqtSignal(str)

    def __init__(self, thumbnail_loader: LazyThumbnailLoader | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.thumbnail_loader = thumbnail_loader or LazyThumbnailLoader(parent=self)
        self.thumbnail_loader.thumbnailReady.connect(self._thumbnail_ready)
        thumbnail_discarded = getattr(self.thumbnail_loader, "thumbnailDiscarded", None)
        if thumbnail_discarded is not None:
            thumbnail_discarded.connect(self._thumbnail_discarded)
        self._group = None
        self._results = None
        self._items: list[object] = []
        self._row_by_item_identity: dict[int, int] = {}
        self._group_layout_revision = None
        self._mark_revision = None
        self._relation = ReviewRelation.UNVERIFIED
        self._metadata_cache: OrderedDict[int, str] = OrderedDict()
        self._eligibility_cache: OrderedDict[int, Eligibility] = OrderedDict()
        self._rows_waiting_for_thumbnail: dict[str, set[int]] = defaultdict(set)
        self._delete_candidates: set[int] = set()
        self._pending_delete_states: dict[int, bool] = {}
        self._accept_in_progress = False

    @property
    def group(self):
        return self._group

    @property
    def relation(self) -> ReviewRelation:
        return self._relation

    @property
    def accept_in_progress(self) -> bool:
        return self._accept_in_progress

    @property
    def metadata_cache_size(self) -> int:
        return len(self._metadata_cache)

    @property
    def thumbnail_waiter_count(self) -> int:
        return sum(len(rows) for rows in self._rows_waiting_for_thumbnail.values())

    def set_group(self, group, results=None):
        self.beginResetModel()
        clear_loader = getattr(self.thumbnail_loader, "clear", None)
        if callable(clear_loader):
            clear_loader()
        self._group = group
        self._results = results
        self._items = _group_members(group)
        self._row_by_item_identity = {id(item): row for row, item in enumerate(self._items)}
        self._group_layout_revision = getattr(group, "layout_revision", None)
        self._mark_revision = getattr(results, "mark_revision", None)
        self._relation = relation_for_group(group)
        self._metadata_cache.clear()
        self._eligibility_cache.clear()
        self._rows_waiting_for_thumbnail.clear()
        self._delete_candidates.clear()
        self._pending_delete_states.clear()
        self._accept_in_progress = False
        self.endResetModel()

    def clear(self):
        self.set_group(None, None)

    def update_results(self, results):
        """Replace live scan context without rebuilding thumbnail rows."""

        self._results = results
        self._mark_revision = getattr(results, "mark_revision", None)
        self._pending_delete_states.clear()
        self.refresh_safety()
        if self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, 0),
                [ReviewRole.DELETE_CANDIDATE],
            )

    def update_item(self, results, item):
        """Refresh one selected card without rescanning a very large group."""

        self._results = results
        previous_mark_revision = self._mark_revision
        self._mark_revision = getattr(results, "mark_revision", None)
        marks_changed = (
            previous_mark_revision is not None
            and self._mark_revision is not None
            and previous_mark_revision != self._mark_revision
        )
        if marks_changed:
            self._pending_delete_states.clear()
        index = self.index_for_item(item)
        if not index.isValid():
            return
        row = index.row()
        self._eligibility_cache.pop(row, None)
        self.dataChanged.emit(
            index,
            index,
            [
                ReviewRole.DELETE_CANDIDATE,
                ReviewRole.DELETE_ENABLED,
                ReviewRole.DELETE_REASON,
            ],
        )
        if marks_changed and self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, 0),
                [ReviewRole.DELETE_CANDIDATE],
            )

    def has_current_layout(self, group) -> bool:
        if group is self._group:
            revision = getattr(group, "layout_revision", None)
            if revision is not None and self._group_layout_revision is not None:
                try:
                    return revision == self._group_layout_revision and len(group) == len(self._items)
                except TypeError:
                    return False
        members = _group_members(group)
        return len(members) == len(self._items) and all(
            current is member for current, member in zip(self._items, members)
        )

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        row = index.row()
        item = self._items[row]
        relation = relation_for_item(item, self._relation)
        role_value = role.value if hasattr(role, "value") else int(role)

        if role_value == Qt.ItemDataRole.DisplayRole.value:
            return _item_name(item)
        if role_value == Qt.ItemDataRole.DecorationRole.value:
            key, path, size, modified_ns, generation_token = self._thumbnail_key(item)
            pixmap = self.thumbnail_loader.request(
                key,
                path,
                expected_size=size,
                expected_mtime_ns=modified_ns,
                expected_generation_token=generation_token,
            )
            is_pending = getattr(self.thumbnail_loader, "is_pending", None)
            if is_pending is not None and is_pending(key):
                self._rows_waiting_for_thumbnail[key].add(row)
            return pixmap
        if role_value == Qt.ItemDataRole.ToolTipRole.value:
            eligibility = self._eligibility(row)
            return f"{_item_path(item)}\n{eligibility.message}"
        if role_value == Qt.ItemDataRole.AccessibleTextRole.value:
            return f"{_item_name(item)}, {RELATION_LABELS[relation]}, {self._metadata(row)}"
        if role_value == ReviewRole.FILE:
            return item
        if role_value == ReviewRole.PATH:
            return _item_path(item)
        if role_value == ReviewRole.RELATION:
            return relation
        if role_value == ReviewRole.RELATION_COLOR:
            return QColor(RELATION_COLORS[relation])
        if role_value == ReviewRole.METADATA:
            return self._metadata(row)
        if role_value == ReviewRole.KEEPER:
            return item is getattr(self._group, "ref", None)
        if role_value == ReviewRole.DELETE_CANDIDATE:
            return self._delete_candidate_state(item)
        if role_value == ReviewRole.DELETE_ENABLED:
            return self._eligibility(row).allowed
        if role_value == ReviewRole.DELETE_REASON:
            return self._eligibility(row).message
        if role_value == ReviewRole.COMPARISON_POOL:
            return str(getattr(item, "comparison_pool", "incoming"))
        if role_value == ReviewRole.THUMBNAIL_KEY:
            return self._thumbnail_key(item)[0]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def item_at(self, index):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        return self._items[index.row()]

    def index_for_item(self, item):
        row = self._row_by_item_identity.get(id(item))
        if row is not None and 0 <= row < len(self._items) and self._items[row] is item:
            return self.index(row, 0)
        return QModelIndex()

    def eligibility_at(self, index) -> Eligibility:
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return Eligibility(EligibilityCode.MISSING_KEEPER, tr("Select an image to review."))
        return self._eligibility(index.row())

    def request_keeper(self, index) -> bool:
        item = self.item_at(index)
        if item is None:
            return False
        self.keeperRequested.emit(item)
        if self._items:
            self._eligibility_cache.clear()
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, 0),
                [
                    ReviewRole.KEEPER,
                    ReviewRole.DELETE_ENABLED,
                    ReviewRole.DELETE_REASON,
                ],
            )
        return True

    def request_delete_candidate(self, index) -> bool:
        item = self.item_at(index)
        if item is None:
            return False
        item_id = id(item)
        if self._delete_candidate_state(item):
            self._delete_candidates.discard(item_id)
            marked = False
        else:
            eligibility = self.eligibility_at(index)
            if not eligibility.allowed:
                self.blockedAction.emit(eligibility.message)
                return False
            self._delete_candidates.add(item_id)
            marked = True
        self._pending_delete_states[item_id] = marked
        self.dataChanged.emit(index, index, [ReviewRole.DELETE_CANDIDATE])
        self.deleteCandidateRequested.emit(item, marked)
        return True

    def request_accept_keeper(self) -> bool:
        """Atomically approve one exact keeper and its remaining candidates."""

        if self._accept_in_progress:
            return False
        keeper = getattr(self._group, "ref", None)
        if keeper is None or self._results is None:
            self.blockedAction.emit(tr("No live keeper is available for this review."))
            return False
        candidates = tuple(item for item in self._items if item is not keeper)
        if not candidates:
            self.blockedAction.emit(tr("This group has no duplicate candidates to check."))
            return False
        for candidate in candidates:
            try:
                eligibility = evaluate_duplicate(self._results, candidate)
            except Exception:
                eligibility = Eligibility(
                    EligibilityCode.INCOMPLETE_SCAN,
                    tr("The safety gate could not verify this candidate."),
                )
            if not eligibility.allowed:
                self.blockedAction.emit(eligibility.message)
                return False
        self.acceptKeeperRequested.emit(keeper, candidates)
        return True

    def set_accept_in_progress(self, in_progress: bool):
        self._accept_in_progress = bool(in_progress)

    def set_delete_candidate(self, item, marked: bool):
        """Synchronize one card with the authoritative results marking state."""

        index = self.index_for_item(item)
        if not index.isValid():
            return
        item_id = id(item)
        self._pending_delete_states.pop(item_id, None)
        if marked:
            self._delete_candidates.add(item_id)
        else:
            self._delete_candidates.discard(item_id)
        self.dataChanged.emit(index, index, [ReviewRole.DELETE_CANDIDATE])

    def set_delete_candidates(self, items, marked: bool):
        """Synchronize a batch using one bounded model notification."""

        first_row = None
        last_row = None
        for item in items:
            row = self._row_by_item_identity.get(id(item))
            if row is None or not 0 <= row < len(self._items) or self._items[row] is not item:
                continue
            first_row = row if first_row is None else min(first_row, row)
            last_row = row if last_row is None else max(last_row, row)
            self._pending_delete_states.pop(id(item), None)
            if marked:
                self._delete_candidates.add(id(item))
            else:
                self._delete_candidates.discard(id(item))
        if first_row is not None:
            self.dataChanged.emit(
                self.index(first_row, 0),
                self.index(last_row, 0),
                [ReviewRole.DELETE_CANDIDATE],
            )

    def report_blocked(self, message: str):
        """Expose a late safety-gate failure through the gallery status UI."""

        self.blockedAction.emit(message)

    def refresh_safety(self):
        self._eligibility_cache.clear()
        if self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, 0),
                [ReviewRole.DELETE_ENABLED, ReviewRole.DELETE_REASON],
            )

    def _is_marked_in_results(self, item) -> bool:
        is_marked = getattr(self._results, "is_marked", None)
        if not callable(is_marked):
            return False
        try:
            return bool(is_marked(item))
        except (KeyError, TypeError, ValueError):
            return False

    def _delete_candidate_state(self, item) -> bool:
        item_id = id(item)
        pending = self._pending_delete_states.get(item_id)
        if pending is not None:
            return pending
        marked = self._is_marked_in_results(item)
        if marked:
            self._delete_candidates.add(item_id)
        else:
            self._delete_candidates.discard(item_id)
        return marked

    def _thumbnail_key(
        self,
        item,
    ) -> tuple[str, str, int | None, int | None, bytes]:
        path = _item_path(item)
        try:
            stat_result = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(stat_result.st_mode)
                or stat.S_ISLNK(stat_result.st_mode)
                or is_reparse_point(stat_result)
            ):
                raise OSError("thumbnail source is not a plain regular file")
            size = stat_result.st_size
            modified_ns = stat_result.st_mtime_ns
            identity = get_file_identity(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
            )
            generation_token = get_file_generation_token(
                path,
                follow_symlinks=False,
                stat_result=stat_result,
                expected_identity=identity,
            ).encoded
        except (OSError, FileIdentityError, ValueError):
            size = None
            modified_ns = getattr(item, "mtime_ns", None)
            if modified_ns is None:
                modified_ns = None
            key_size = int(getattr(item, "size", 0) or 0)
            key_modified_ns = (
                int(float(getattr(item, "mtime", 0) or 0) * 1_000_000_000) if modified_ns is None else int(modified_ns)
            )
            generation_token = FileGenerationToken(
                "unavailable-thumbnail-source",
                0,
            ).encoded
        else:
            key_size = int(size)
            key_modified_ns = int(modified_ns)
        thumbnail_size = getattr(self.thumbnail_loader, "thumbnail_size", QSize(168, 112))
        return (
            thumbnail_cache_key(
                path,
                key_size,
                key_modified_ns,
                generation_token,
                thumbnail_size,
            ),
            path,
            size,
            modified_ns,
            generation_token,
        )

    def _metadata(self, row: int) -> str:
        cached = self._metadata_cache.get(row)
        if cached is not None:
            self._metadata_cache.move_to_end(row)
            return cached
        item = self._items[row]
        dimensions = getattr(item, "dimensions", (0, 0))
        if isinstance(dimensions, (tuple, list)) and len(dimensions) >= 2 and dimensions[0] and dimensions[1]:
            dimension_text = f"{dimensions[0]}×{dimensions[1]}"
        else:
            dimension_text = tr("dimensions unknown")
        size_text = _format_size(getattr(item, "size", None))
        path = _item_path(item)
        extension = getattr(item, "extension", None) or Path(path).suffix.lstrip(".")
        pool = str(getattr(item, "comparison_pool", "incoming")).replace("_", " ")
        review_metadata = str(getattr(item, "review_metadata", "") or "")
        details = f"{dimension_text} · {size_text}"
        if extension:
            details += f" · {str(extension).upper()}"
        if review_metadata:
            details = f"{review_metadata}\n{details}"
        details += f"\n{pool}"
        self._metadata_cache[row] = details
        while len(self._metadata_cache) > self.ROW_CACHE_LIMIT:
            self._metadata_cache.popitem(last=False)
        return details

    def _eligibility(self, row: int) -> Eligibility:
        cached = self._eligibility_cache.get(row)
        if cached is not None:
            self._eligibility_cache.move_to_end(row)
            return cached
        if self._results is None:
            eligibility = Eligibility(
                EligibilityCode.INCOMPLETE_SCAN,
                tr("No live, complete scan proof is attached to this review."),
            )
        else:
            try:
                eligibility = evaluate_duplicate(self._results, self._items[row])
            except Exception:
                eligibility = Eligibility(
                    EligibilityCode.INCOMPLETE_SCAN,
                    tr("The safety gate could not verify this candidate."),
                )
        self._eligibility_cache[row] = eligibility
        while len(self._eligibility_cache) > self.ROW_CACHE_LIMIT:
            self._eligibility_cache.popitem(last=False)
        return eligibility

    @pyqtSlot(str)
    def _thumbnail_ready(self, key: str):
        rows = self._rows_waiting_for_thumbnail.pop(key, ())
        for row in rows:
            if 0 <= row < len(self._items):
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    @pyqtSlot(str)
    def _thumbnail_discarded(self, key: str):
        # Do not emit dataChanged here.  A repaint would immediately resubmit
        # the discarded row and could make two off-screen rows evict each other
        # forever.  A future natural DecorationRole request can enqueue it.
        self._rows_waiting_for_thumbnail.pop(key, None)


class ReviewGalleryDelegate(QStyledItemDelegate):
    """Paint thumbnails, metadata, and safety badges without child widgets."""

    CARD_SIZE = QSize(190, 172)
    CARD_MARGIN = 5
    IMAGE_HEIGHT = 132

    def sizeHint(self, option, index):
        return QSize(self.CARD_SIZE)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = option.rect.adjusted(self.CARD_MARGIN, self.CARD_MARGIN, -self.CARD_MARGIN, -self.CARD_MARGIN)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        base_color = option.palette.highlight().color() if selected else option.palette.base().color()
        if hovered and not selected:
            base_color = option.palette.alternateBase().color()
        painter.setBrush(base_color)
        relation_color = index.data(ReviewRole.RELATION_COLOR)
        if not isinstance(relation_color, QColor):
            relation_color = QColor(RELATION_COLORS[ReviewRelation.UNVERIFIED])
        painter.setPen(QPen(relation_color, 4))
        painter.drawRoundedRect(card, 7, 7)

        image_rect = QRect(
            card.left() + 4,
            card.top() + 4,
            card.width() - 8,
            min(self.IMAGE_HEIGHT, card.height() - 8),
        )
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target = QRect(QPoint(), scaled.size())
            target.moveCenter(image_rect.center())
            painter.drawPixmap(target, scaled)

        overlay = QRect(image_rect.left(), image_rect.bottom() - 47, image_rect.width(), 48)
        painter.fillRect(overlay, QColor(0, 0, 0, 176))
        painter.setPen(QColor("#FFFFFF"))
        normal_font = QFont(option.font)
        normal_font.setPointSize(max(7, normal_font.pointSize() - 1))
        painter.setFont(normal_font)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        name = painter.fontMetrics().elidedText(
            name,
            Qt.TextElideMode.ElideMiddle,
            overlay.width() - 10,
        )
        painter.drawText(
            overlay.adjusted(5, 2, -5, -25),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )
        metadata = str(index.data(ReviewRole.METADATA) or "").splitlines()[0]
        metadata = painter.fontMetrics().elidedText(
            metadata,
            Qt.TextElideMode.ElideRight,
            overlay.width() - 10,
        )
        painter.drawText(
            overlay.adjusted(5, 24, -5, -2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metadata,
        )

        if index.data(ReviewRole.KEEPER):
            item = index.data(ReviewRole.FILE)
            keeper_label = str(getattr(item, "review_keeper_label", "") or tr("KEEP"))
            self._draw_badge(
                painter,
                image_rect.adjusted(5, 5, -5, -5),
                keeper_label,
                relation_color,
                left=True,
            )
        if index.data(ReviewRole.DELETE_CANDIDATE):
            self._draw_badge(
                painter,
                image_rect.adjusted(5, 5, -5, -5),
                tr("DELETE"),
                QColor("#C83D4B"),
                left=False,
            )
        pool = str(index.data(ReviewRole.COMPARISON_POOL) or "incoming").replace("_", " ").upper()
        pool_rect = QRect(
            card.left() + 8,
            image_rect.bottom() + 3,
            card.width() - 16,
            max(18, card.bottom() - image_rect.bottom() - 5),
        )
        painter.setPen(relation_color if pool == "INCOMING" else QColor("#C57A16"))
        pool_font = QFont(option.font)
        pool_font.setBold(True)
        pool_font.setPointSize(max(7, pool_font.pointSize() - 1))
        painter.setFont(pool_font)
        painter.drawText(
            pool_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(
                pool,
                Qt.TextElideMode.ElideRight,
                pool_rect.width(),
            ),
        )
        painter.restore()

    @staticmethod
    def _draw_badge(painter: QPainter, rect: QRect, text: str, color: QColor, *, left: bool):
        badge_width = painter.fontMetrics().horizontalAdvance(text) + 12
        if left:
            badge = QRect(rect.left(), rect.top(), badge_width, 22)
        else:
            badge = QRect(rect.right() - badge_width + 1, rect.top(), badge_width, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(badge, 5, 5)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)


class ReviewGalleryView(QListView):
    """Icon-mode review surface with keyboard and hover preview signals."""

    HOVER_PREVIEW_DELAY_MS = 200

    previewRequested = pyqtSignal(object)
    nextGroupRequested = pyqtSignal()

    def __init__(self, model: ReviewGalleryModel | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(100)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(2)
        self.setItemDelegate(ReviewGalleryDelegate(self))
        self.setModel(model or ReviewGalleryModel(parent=self))
        self._hover_index = QPersistentModelIndex()
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(self.HOVER_PREVIEW_DELAY_MS)
        self._hover_timer.timeout.connect(self._emit_hover_preview)
        self.viewport().installEventFilter(self)
        self.gallery_model.modelAboutToBeReset.connect(self._cancel_hover_preview)
        self.entered.connect(self._schedule_hover_preview)
        self.selectionModel().currentChanged.connect(self._current_changed)

    @property
    def gallery_model(self) -> ReviewGalleryModel:
        return self.model()

    def select_item(self, item):
        index = self.gallery_model.index_for_item(item)
        if index.isValid():
            self.setCurrentIndex(index)
            self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def invoke_keeper(self) -> bool:
        return self.gallery_model.request_keeper(self.currentIndex())

    def invoke_delete_candidate(self) -> bool:
        return self.gallery_model.request_delete_candidate(self.currentIndex())

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_1:
            self.invoke_keeper()
            event.accept()
            return
        if event.key() == Qt.Key.Key_2:
            self.invoke_delete_candidate()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space:
            self.nextGroupRequested.emit()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.gallery_model.request_accept_keeper()
            event.accept()
            return
        super().keyPressEvent(event)

    @pyqtSlot(QModelIndex)
    def _preview_index(self, index: QModelIndex):
        item = self.gallery_model.item_at(index)
        if item is not None:
            self.previewRequested.emit(item)

    @pyqtSlot(QModelIndex)
    def _schedule_hover_preview(self, index: QModelIndex):
        self._cancel_hover_preview()
        if self.gallery_model.item_at(index) is None:
            return
        self._hover_index = QPersistentModelIndex(index)
        self._hover_timer.start()

    @pyqtSlot()
    def _emit_hover_preview(self):
        index = self._hover_index
        self._hover_index = QPersistentModelIndex()
        self._preview_index(index)

    @pyqtSlot()
    def _cancel_hover_preview(self):
        self._hover_timer.stop()
        self._hover_index = QPersistentModelIndex()

    @pyqtSlot(QModelIndex, QModelIndex)
    def _current_changed(self, current: QModelIndex, previous: QModelIndex):
        self._cancel_hover_preview()
        self._preview_index(current)

    def eventFilter(self, watched, event):
        if watched is self.viewport() and event.type() == QEvent.Type.Leave:
            self._cancel_hover_preview()
        return super().eventFilter(watched, event)


class ReviewGalleryWidget(QWidget):
    """Gallery plus an explicit destructive-action safety contract."""

    previewRequested = pyqtSignal(object)
    keeperRequested = pyqtSignal(object)
    deleteCandidateRequested = pyqtSignal(object, bool)
    acceptKeeperRequested = pyqtSignal(object, object)
    nextGroupRequested = pyqtSignal()
    blockedAction = pyqtSignal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        thumbnail_loader: LazyThumbnailLoader | None = None,
    ):
        super().__init__(parent)
        self.model = ReviewGalleryModel(thumbnail_loader=thumbnail_loader, parent=self)
        self.view = ReviewGalleryView(self.model, self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.relationLabel = QLabel(self)
        self.relationLabel.setObjectName("reviewRelationLabel")
        self.relationLabel.setMinimumWidth(120)
        self.relationLabel.setMaximumWidth(230)
        self.safetyLabel = QLabel(tr("Select an image to review."), self)
        self.safetyLabel.setObjectName("reviewSafetyLabel")
        self.safetyLabel.setWordWrap(True)
        self.safetyLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.keeperButton = QPushButton(tr("1 Keep"), self)
        self.keeperButton.setToolTip(tr("Choose the selected image as the keeper."))
        self.deleteButton = QPushButton(tr("2 Mark delete"), self)
        self.acceptButton = QPushButton(tr("Enter Accept keeper and next"), self)
        self.acceptButton.setToolTip(
            tr("Check every safe byte-identical copy of the current keeper, then review the next group.")
        )
        self.nextButton = QPushButton(tr("Space Next"), self)
        self.nextButton.setToolTip(tr("Review the next duplicate group."))
        self.deleteButton.setEnabled(False)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.relationLabel)
        status_row.addWidget(self.safetyLabel, 1)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.addStretch(1)
        action_row.addWidget(self.keeperButton)
        action_row.addWidget(self.deleteButton)
        action_row.addWidget(self.acceptButton)
        action_row.addWidget(self.nextButton)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(status_row)
        layout.addLayout(action_row)
        layout.addWidget(self.view, 1)

        self.view.previewRequested.connect(self.previewRequested)
        self.view.nextGroupRequested.connect(self.nextGroupRequested)
        self.model.keeperRequested.connect(self.keeperRequested)
        self.model.deleteCandidateRequested.connect(self.deleteCandidateRequested)
        self.model.acceptKeeperRequested.connect(self.acceptKeeperRequested)
        self.model.blockedAction.connect(self._blocked)
        self.model.blockedAction.connect(self.blockedAction)
        self.model.dataChanged.connect(self._model_data_changed)
        self.view.selectionModel().currentChanged.connect(self._selection_changed)
        self.keeperButton.clicked.connect(self.view.invoke_keeper)
        self.deleteButton.clicked.connect(self.view.invoke_delete_candidate)
        self.acceptButton.clicked.connect(self.model.request_accept_keeper)
        self.nextButton.clicked.connect(self.nextGroupRequested)
        self._update_relation_label()
        self._update_safety(QModelIndex())

    def set_group(self, group, results=None, selected=None):
        self.model.set_group(group, results)
        self._update_relation_label()
        if selected is not None:
            self.view.select_item(selected)
        elif self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0, 0))
        else:
            self._update_safety(QModelIndex())

    def clear(self):
        self.model.clear()
        self._update_relation_label()
        self._update_safety(QModelIndex())

    def _update_relation_label(self):
        relation = self.model.relation
        color = RELATION_COLORS[relation]
        count = self.model.rowCount()
        self.relationLabel.setText(tr("%s · %d") % (RELATION_LABELS[relation], count))
        self.relationLabel.setStyleSheet(
            "QLabel {"
            f"background-color: {color}; color: #10151D; border-radius: 4px; "
            "font-weight: 600; padding: 4px 8px;"
            "}"
        )

    def _update_safety(self, index):
        eligibility = self.model.eligibility_at(index)
        self.deleteButton.setEnabled(eligibility.allowed)
        self.acceptButton.setEnabled(
            not self.model.accept_in_progress
            and self.model.relation is ReviewRelation.BYTE_VERIFIED_EXACT
            and self.model.rowCount() > 1
        )
        delete_tooltip = eligibility.message
        if eligibility.allowed:
            delete_tooltip = (
                tr("Mark this image as a deletion candidate; this button does not delete the file.")
                + "\n"
                + eligibility.message
            )
        self.deleteButton.setToolTip(delete_tooltip)
        self.safetyLabel.setText(eligibility.message)
        self.safetyLabel.setToolTip(eligibility.message)
        item = self.model.item_at(index)
        self.keeperButton.setEnabled(item is not None)

    def set_accept_in_progress(self, in_progress: bool):
        self.model.set_accept_in_progress(in_progress)
        self._update_safety(self.view.currentIndex())

    @pyqtSlot(QModelIndex, QModelIndex)
    def _selection_changed(self, current, previous):
        self._update_safety(current)

    def _model_data_changed(self, top_left, bottom_right, roles):
        current = self.view.currentIndex()
        if current.isValid() and top_left.row() <= current.row() <= bottom_right.row():
            self._update_safety(current)

    @pyqtSlot(str)
    def _blocked(self, message: str):
        self.safetyLabel.setText(message)
        self.safetyLabel.setToolTip(message)
        self.deleteButton.setEnabled(False)
        self.deleteButton.setToolTip(message)
