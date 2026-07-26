import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QSize, QStandardPaths  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt6.QtTest import QSignalSpy  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.file_generation import (  # noqa: E402
    FileGenerationToken,
    get_file_generation_token,
)
from core.file_identity import get_file_identity  # noqa: E402
from qt.pe.review_gallery import LazyThumbnailLoader  # noqa: E402
import qt.pe.review_gallery as review_gallery  # noqa: E402
from qt.pe.thumbnail_cache import (  # noqa: E402
    ThumbnailDiskCache,
    default_thumbnail_cache_dir,
    thumbnail_cache_key,
)

TEST_GENERATION_TOKEN = FileGenerationToken("test-thumbnail", 1).encoded


def source_generation_token(path):
    source_stat = path.stat()
    identity = get_file_identity(
        path,
        follow_symlinks=False,
        stat_result=source_stat,
    )
    return get_file_generation_token(
        path,
        follow_symlinks=False,
        stat_result=source_stat,
        expected_identity=identity,
    ).encoded


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def solid_image(size=QSize(40, 30), color="#336699"):
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def test_cache_key_uses_absolute_path_file_state_and_thumbnail_size(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative = Path("source.png")
    absolute = tmp_path / relative
    base = thumbnail_cache_key(
        relative,
        10,
        20,
        TEST_GENERATION_TOKEN,
        QSize(100, 80),
    )

    assert base == thumbnail_cache_key(
        absolute,
        10,
        20,
        TEST_GENERATION_TOKEN,
        QSize(100, 80),
    )
    assert base != thumbnail_cache_key(
        absolute,
        11,
        20,
        TEST_GENERATION_TOKEN,
        QSize(100, 80),
    )
    assert base != thumbnail_cache_key(
        absolute,
        10,
        21,
        TEST_GENERATION_TOKEN,
        QSize(100, 80),
    )
    assert base != thumbnail_cache_key(
        absolute,
        10,
        20,
        TEST_GENERATION_TOKEN,
        QSize(101, 80),
    )
    assert base != thumbnail_cache_key(
        absolute,
        10,
        20,
        FileGenerationToken("test-thumbnail", 2).encoded,
        QSize(100, 80),
    )
    assert len(base) == 64


def test_default_cache_directory_is_under_qt_user_cache_location(qapp):
    qt_cache = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation,
        )
    ).absolute()

    assert default_thumbnail_cache_dir().is_relative_to(qt_cache)


def test_store_is_atomic_png_and_loads_valid_image(qapp, tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = thumbnail_cache_key(
        tmp_path / "source.png",
        10,
        20,
        TEST_GENERATION_TOKEN,
        QSize(40, 30),
    )

    assert cache.store(key, solid_image())
    path = cache.path_for_key(key)
    loaded = cache.load(key, QSize(40, 30))

    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert loaded is not None
    assert loaded.size() == QSize(40, 30)
    assert [item for item in cache.cache_dir.rglob("*") if item.is_file()] == [path]


def test_corrupt_entry_is_removed_and_lazy_loader_regenerates_it(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40), "#AA3355").save(str(source_path))
    stat_result = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    cache = ThumbnailDiskCache(tmp_path / "cache")
    assert cache.store(key, solid_image(target_size))
    cache_path = cache.path_for_key(key)
    cache_path.write_bytes(b"corrupt")

    assert cache.load(key, target_size) is None
    assert not cache_path.exists()

    loader = LazyThumbnailLoader(target_size, disk_cache=cache)
    ready = QSignalSpy(loader.thumbnailReady)
    loader.request(
        key,
        str(source_path),
        expected_generation_token=source_generation_token(source_path),
    )
    assert ready.wait(3000)
    qapp.processEvents()

    regenerated = cache.load(key, target_size)
    assert regenerated is not None
    assert regenerated.width() <= target_size.width()
    assert regenerated.height() <= target_size.height()


def test_disk_cache_hit_is_rejected_when_source_file_disappears(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40), "#228844").save(str(source_path))
    stat_result = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    cache = ThumbnailDiskCache(tmp_path / "cache")
    first_loader = LazyThumbnailLoader(target_size, disk_cache=cache)
    first_ready = QSignalSpy(first_loader.thumbnailReady)
    generation_token = source_generation_token(source_path)
    first_loader.request(
        key,
        str(source_path),
        expected_generation_token=generation_token,
    )
    assert first_ready.wait(3000)
    qapp.processEvents()
    source_path.unlink()

    second_loader = LazyThumbnailLoader(target_size, disk_cache=cache)
    second_ready = QSignalSpy(second_loader.thumbnailReady)
    placeholder = second_loader.request(
        key,
        str(source_path),
        expected_generation_token=generation_token,
    )
    assert isinstance(placeholder, QPixmap)
    assert second_ready.wait(3000)
    qapp.processEvents()
    cached = second_loader.request(
        key,
        str(source_path),
        expected_generation_token=generation_token,
    )

    assert isinstance(cached, QPixmap)
    assert not cached.isNull()
    assert cached.size() == target_size
    assert cached.toImage().pixelColor(0, 0) == QColor("#252A33")


def test_disk_cache_hit_rejects_same_size_restored_mtime_edit(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40), "#228844").save(str(source_path))
    source_stat = source_path.stat()
    original_generation = source_generation_token(source_path)
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        original_generation,
        target_size,
    )
    cache = ThumbnailDiskCache(tmp_path / "cache")
    assert cache.store(key, solid_image(target_size, "#FF00FF"))

    changed = bytearray(source_path.read_bytes())
    changed[-1] ^= 1
    source_path.write_bytes(changed)
    os.utime(
        source_path,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
    )
    restored = source_path.stat()
    assert restored.st_size == source_stat.st_size
    assert restored.st_mtime_ns == source_stat.st_mtime_ns
    assert source_generation_token(source_path) != original_generation

    loader = LazyThumbnailLoader(target_size, disk_cache=cache)
    ready = QSignalSpy(loader.thumbnailReady)
    loader.request(
        key,
        str(source_path),
        expected_size=source_stat.st_size,
        expected_mtime_ns=source_stat.st_mtime_ns,
        expected_generation_token=original_generation,
    )
    assert ready.wait(3000)
    qapp.processEvents()

    result = loader.request(
        key,
        str(source_path),
        expected_generation_token=original_generation,
    )
    assert result.size() == target_size
    assert result.toImage().pixelColor(0, 0) == QColor("#252A33")


def test_thumbnail_source_rechecks_same_handle_generation_after_use(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"thumbnail-source")
    source_stat = source_path.stat()
    expected_generation = source_generation_token(source_path)
    real_generation = review_gallery.get_file_generation_token_from_fd
    calls = 0

    def changing_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        observed = real_generation(*args, **kwargs)
        if calls == 2:
            return FileGenerationToken(
                "test-thumbnail-source-race",
                2,
            )
        return observed

    monkeypatch.setattr(
        review_gallery,
        "get_file_generation_token_from_fd",
        changing_generation,
    )

    with pytest.raises(OSError, match="changed while"):
        with review_gallery._open_stable_thumbnail_source(
            source_path,
            expected_size=source_stat.st_size,
            expected_mtime_ns=source_stat.st_mtime_ns,
            expected_generation_token=expected_generation,
            max_source_bytes=1024,
        ):
            pass

    assert calls == 2


def test_cleanup_enforces_entry_and_total_byte_bounds(qapp, tmp_path):
    cache = ThumbnailDiskCache(
        tmp_path / "cache",
        max_entries=2,
        max_bytes=1_000_000,
    )
    for index in range(5):
        key = thumbnail_cache_key(
            tmp_path / f"source-{index}.png",
            index + 1,
            index + 2,
            TEST_GENERATION_TOKEN,
            QSize(40, 30),
        )
        assert cache.store(key, solid_image(color=f"#{index + 1:02x}4466"))

    count, total = cache.cleanup()
    assert count <= 2
    assert total <= cache.max_bytes

    tiny_cache = ThumbnailDiskCache(
        tmp_path / "tiny-cache",
        max_entries=10,
        max_bytes=1,
    )
    key = thumbnail_cache_key(
        tmp_path / "large.png",
        1,
        1,
        TEST_GENERATION_TOKEN,
        QSize(40, 30),
    )
    assert tiny_cache.store(key, solid_image())
    assert tiny_cache.usage() == (0, 0)


def test_cache_key_cannot_escape_cache_directory(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.path_for_key("../../outside")


def test_thumbnail_failures_always_release_pending_key(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    source_stat = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    loader = LazyThumbnailLoader(
        target_size,
        disk_cache=ThumbnailDiskCache(tmp_path / "cache"),
        max_source_bytes=4,
    )
    ready = QSignalSpy(loader.thumbnailReady)

    loader.request(
        key,
        str(source_path),
        expected_size=source_stat.st_size,
        expected_mtime_ns=source_stat.st_mtime_ns,
        expected_generation_token=source_generation_token(source_path),
    )

    assert ready.wait(3000)
    qapp.processEvents()
    assert loader.pending_count == 0
    assert loader.cached_count == 1


def test_thumbnail_pixel_and_generation_limits_fail_closed(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    source_stat = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    for options in (
        {"max_source_pixels": 100},
        {},
    ):
        loader = LazyThumbnailLoader(
            target_size,
            disk_cache=ThumbnailDiskCache(tmp_path / "cache-{}".format(len(options))),
            **options,
        )
        ready = QSignalSpy(loader.thumbnailReady)
        loader.request(
            key,
            str(source_path),
            expected_size=source_stat.st_size + (0 if options else 1),
            expected_mtime_ns=source_stat.st_mtime_ns,
            expected_generation_token=source_generation_token(source_path),
        )
        assert ready.wait(3000)
        qapp.processEvents()
        assert loader.pending_count == 0
        assert loader.cached_count == 1


def test_thumbnail_cache_errors_do_not_strand_pending_or_hide_decoded_image(
    qapp,
    tmp_path,
):
    class FailingCache:
        def load(self, *_args):
            raise RuntimeError("unsafe cache")

        def store(self, *_args):
            raise RuntimeError("unsafe cache")

    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    source_stat = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    loader = LazyThumbnailLoader(target_size, disk_cache=FailingCache())
    ready = QSignalSpy(loader.thumbnailReady)

    loader.request(
        key,
        str(source_path),
        expected_size=source_stat.st_size,
        expected_mtime_ns=source_stat.st_mtime_ns,
        expected_generation_token=source_generation_token(source_path),
    )

    assert ready.wait(3000)
    qapp.processEvents()
    assert loader.pending_count == 0
    assert loader.cached_count == 1
    assert loader.request(
        key,
        str(source_path),
        expected_generation_token=source_generation_token(source_path),
    ).size() == QSize(40, 20)


def test_closed_thumbnail_loader_ignores_late_worker_publication(qapp, tmp_path):
    class DeferredPool:
        task = None

        def start(self, task):
            self.task = task

    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    source_stat = source_path.stat()
    target_size = QSize(40, 30)
    key = thumbnail_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_generation_token(source_path),
        target_size,
    )
    pool = DeferredPool()
    loader = LazyThumbnailLoader(
        target_size,
        thread_pool=pool,
        disk_cache=ThumbnailDiskCache(tmp_path / "cache"),
    )
    ready = QSignalSpy(loader.thumbnailReady)
    loader.request(
        key,
        str(source_path),
        expected_generation_token=source_generation_token(source_path),
    )
    assert loader.pending_count == 1

    loader.close()
    pool.task.run()
    qapp.processEvents()

    assert loader.pending_count == 0
    assert loader.cached_count == 0
    assert len(ready) == 0


def test_thumbnail_pending_queue_has_a_hard_cap(qapp, tmp_path):
    class DeferredPool:
        def __init__(self):
            self.tasks = []

        def start(self, task):
            self.tasks.append(task)

    pool = DeferredPool()
    loader = LazyThumbnailLoader(
        QSize(40, 30),
        thread_pool=pool,
        disk_cache=ThumbnailDiskCache(tmp_path / "cache"),
        max_pending_tasks=2,
    )

    for index in range(10):
        loader.request(
            "{:064x}".format(index + 1),
            str(tmp_path / "missing.png"),
            expected_generation_token=TEST_GENERATION_TOKEN,
        )

    assert loader.pending_count == 2
    assert len(pool.tasks) == 2
    loader.close()


def test_clear_drops_owned_pool_queue_and_ignores_old_terminal_signal(
    qapp,
    tmp_path,
    monkeypatch,
):
    class RecordingPool:
        def __init__(self, *_args):
            self.tasks = []
            self.clear_count = 0
            self.maximum = 0

        def setMaxThreadCount(self, value):
            self.maximum = value

        def maxThreadCount(self):
            return self.maximum

        def start(self, task):
            self.tasks.append(task)

        def clear(self):
            self.clear_count += 1
            self.tasks.clear()

    monkeypatch.setattr(review_gallery, "QThreadPool", RecordingPool)
    source_path = tmp_path / "source.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    source_stat = source_path.stat()
    loader = LazyThumbnailLoader(
        QSize(40, 30),
        disk_cache=ThumbnailDiskCache(tmp_path / "cache"),
    )
    old_key = "{:064x}".format(1)
    loader.request(
        old_key,
        str(source_path),
        expected_size=source_stat.st_size,
        expected_mtime_ns=source_stat.st_mtime_ns,
        expected_generation_token=source_generation_token(source_path),
    )
    old_task = loader.thread_pool.tasks[0]

    loader.clear()
    assert loader.thread_pool.clear_count == 1
    assert loader.thread_pool.tasks == []
    assert loader.pending_count == 0

    new_key = "{:064x}".format(2)
    loader.request(
        new_key,
        str(source_path),
        expected_size=source_stat.st_size,
        expected_mtime_ns=source_stat.st_mtime_ns,
        expected_generation_token=source_generation_token(source_path),
    )
    assert loader.pending_count == 1
    old_task.run()
    qapp.processEvents()
    assert loader.pending_count == 1
    assert loader.cached_count == 0

    loader.thread_pool.tasks[0].run()
    qapp.processEvents()
    assert loader.pending_count == 0
    assert loader.cached_count == 1
    loader.close()


def test_thumbnail_source_symlink_is_not_followed(qapp, tmp_path):
    source_path = tmp_path / "source.png"
    alias_path = tmp_path / "alias.png"
    assert solid_image(QSize(80, 40)).save(str(source_path))
    try:
        os.symlink(source_path, alias_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip("file symlinks are unavailable: {}".format(error))
    target_size = QSize(40, 30)
    alias_stat = alias_path.lstat()
    key = thumbnail_cache_key(
        alias_path,
        alias_stat.st_size,
        alias_stat.st_mtime_ns,
        TEST_GENERATION_TOKEN,
        target_size,
    )
    loader = LazyThumbnailLoader(
        target_size,
        disk_cache=ThumbnailDiskCache(tmp_path / "cache"),
    )
    ready = QSignalSpy(loader.thumbnailReady)

    loader.request(
        key,
        str(alias_path),
        expected_generation_token=TEST_GENERATION_TOKEN,
    )

    assert ready.wait(3000)
    qapp.processEvents()
    assert loader.pending_count == 0
    assert loader.cached_count == 1
    assert (
        loader.request(
            key,
            str(alias_path),
            expected_generation_token=TEST_GENERATION_TOKEN,
        ).size()
        == target_size
    )


def test_default_thumbnail_pool_bounds_concurrent_source_payloads(
    qapp,
    monkeypatch,
):
    class EmptyCache:
        def load(self, *_args):
            return None

        def store(self, *_args):
            return False

    lock = threading.Lock()
    release = threading.Event()
    three_started = threading.Event()
    active = [0]
    maximum = [0]

    def blocking_decode(*_args, **_kwargs):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            if active[0] == 3:
                three_started.set()
        assert release.wait(3)
        with lock:
            active[0] -= 1
        return solid_image(QSize(16, 12))

    monkeypatch.setattr(
        review_gallery,
        "_decode_stable_thumbnail_source",
        blocking_decode,
    )
    loader = LazyThumbnailLoader(
        QSize(40, 30),
        disk_cache=EmptyCache(),
        max_concurrent_tasks=3,
    )
    assert loader.thread_pool is not review_gallery.QThreadPool.globalInstance()
    assert loader.thread_pool.maxThreadCount() == 3

    for index in range(8):
        loader.request(
            "{:064x}".format(index + 1),
            "unused.png",
            expected_generation_token=TEST_GENERATION_TOKEN,
        )

    assert three_started.wait(2)
    with lock:
        assert maximum[0] == 3
    release.set()
    assert loader.thread_pool.waitForDone(3000)
    qapp.processEvents()

    assert loader.pending_count == 0
    assert loader.cached_count == 8
    loader.close()
