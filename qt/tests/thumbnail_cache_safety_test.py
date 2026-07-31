import errno
import os
import stat

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QSize  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402

from core.file_generation import FileGenerationToken  # noqa: E402
import qt.pe.thumbnail_cache as thumbnail_cache_module  # noqa: E402
from qt.pe.thumbnail_cache import (  # noqa: E402
    ThumbnailCacheSafetyError,
    ThumbnailDiskCache,
    thumbnail_cache_key,
)

TEST_GENERATION_TOKEN = FileGenerationToken("test-thumbnail-cache", 1).encoded


def _solid_image():
    image = QImage(QSize(40, 30), QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    return image


def _key(tmp_path, label="source"):
    return thumbnail_cache_key(
        tmp_path / "{}.png".format(label),
        10,
        20,
        TEST_GENERATION_TOKEN,
        QSize(40, 30),
    )


def _make_symlink(target, alias, *, directory=False):
    try:
        os.symlink(target, alias, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip("symlinks are unavailable: {}".format(error))


def test_cache_parent_symlink_is_rejected_before_creation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    _make_symlink(outside, linked_parent, directory=True)

    with pytest.raises(ThumbnailCacheSafetyError, match="plain directories"):
        ThumbnailDiskCache(linked_parent / "cache")

    assert not (outside / "cache").exists()


def test_linked_shard_blocks_store_load_cleanup_and_clear_without_touching_source(
    tmp_path,
):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    cache.cache_dir.mkdir()
    key = _key(tmp_path)
    shard = cache.path_for_key(key).parent
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "{}.png".format(key)
    original = b"outside user data"
    source.write_bytes(original)
    _make_symlink(outside, shard, directory=True)

    with pytest.raises(ThumbnailCacheSafetyError, match="plain directories"):
        cache.store(key, _solid_image())
    with pytest.raises(ThumbnailCacheSafetyError, match="plain directories"):
        cache.load(key, QSize(40, 30))
    with pytest.raises(ThumbnailCacheSafetyError, match="linked shards"):
        cache.cleanup()
    with pytest.raises(ThumbnailCacheSafetyError, match="linked shards"):
        cache.clear()

    assert source.read_bytes() == original


def test_existing_target_symlink_is_rejected_without_touching_source(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    target.parent.mkdir(parents=True)
    source = tmp_path / "user-data.bin"
    original = b"user data must remain unchanged"
    source.write_bytes(original)
    _make_symlink(source, target)

    with pytest.raises(ThumbnailCacheSafetyError):
        cache.store(key, _solid_image())
    with pytest.raises(ThumbnailCacheSafetyError):
        cache.load(key, QSize(40, 30))
    with pytest.raises(ThumbnailCacheSafetyError, match="linked entries"):
        cache.cleanup()

    assert source.read_bytes() == original


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_existing_target_hardlink_is_rejected_without_touching_source(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    target.parent.mkdir(parents=True)
    source = tmp_path / "user-data.bin"
    original = b"user data must remain unchanged"
    source.write_bytes(original)
    try:
        os.link(source, target)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    with pytest.raises(ThumbnailCacheSafetyError, match="hard-linked entries"):
        cache.store(key, _solid_image())
    with pytest.raises(ThumbnailCacheSafetyError, match="exactly one filesystem link"):
        cache.load(key, QSize(40, 30))
    with pytest.raises(ThumbnailCacheSafetyError, match="hard-linked entries"):
        cache.cleanup()
    with pytest.raises(ThumbnailCacheSafetyError, match="hard-linked entries"):
        cache.clear()

    assert source.read_bytes() == original
    assert target.read_bytes() == original


def test_cache_entry_read_rechecks_same_handle_generation(
    tmp_path,
    monkeypatch,
):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    assert cache.store(key, _solid_image())
    real_generation = thumbnail_cache_module.get_file_generation_token_from_fd
    calls = 0

    def changing_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        observed = real_generation(*args, **kwargs)
        if calls == 2:
            return FileGenerationToken(
                "test-thumbnail-cache-race",
                2,
            )
        return observed

    monkeypatch.setattr(
        thumbnail_cache_module,
        "get_file_generation_token_from_fd",
        changing_generation,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="changed while"):
        cache.load(key, QSize(40, 30))

    assert calls == 2


def test_store_closes_writer_before_no_write_reopen(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    real_open = thumbnail_cache_module.os.open
    real_reopen = thumbnail_cache_module._open_output_readonly
    observed = {}

    def capture_writer(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL and flags & os.O_WRONLY:
            observed["writer"] = descriptor
        return descriptor

    def require_closed_writer(path):
        with pytest.raises(OSError) as caught:
            os.fstat(observed["writer"])
        assert caught.value.errno == errno.EBADF
        observed["reopened"] = True
        return real_reopen(path)

    monkeypatch.setattr(thumbnail_cache_module.os, "open", capture_writer)
    monkeypatch.setattr(
        thumbnail_cache_module,
        "_open_output_readonly",
        require_closed_writer,
    )

    assert cache.store(key, _solid_image())
    assert observed["reopened"]


def test_store_rejects_name_replacement_before_no_write_reopen(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    replacement = tmp_path / "replacement.bin"
    stolen = tmp_path / "stolen-owned-output.png"
    external = b"external replacement must survive"
    replacement.write_bytes(external)
    real_reopen = thumbnail_cache_module._open_output_readonly
    replaced = False

    def replace_before_reopen(path):
        nonlocal replaced
        if not replaced:
            os.replace(path, stolen)
            os.replace(replacement, path)
            replaced = True
        return real_reopen(path)

    monkeypatch.setattr(
        thumbnail_cache_module,
        "_open_output_readonly",
        replace_before_reopen,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="does not identify"):
        cache.store(key, _solid_image())

    assert replaced
    assert target.read_bytes() == external
    assert stolen.is_file()


def test_store_rechecks_generation_around_byte_verification(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    real_generation = thumbnail_cache_module.get_file_generation_token_from_fd
    calls = 0

    def changing_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        observed = real_generation(*args, **kwargs)
        if calls == 2:
            return FileGenerationToken(
                "test-thumbnail-store-race",
                2,
            )
        return observed

    monkeypatch.setattr(
        thumbnail_cache_module,
        "get_file_generation_token_from_fd",
        changing_generation,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="changed while"):
        cache.store(key, _solid_image())

    assert calls == 2
    assert cache.path_for_key(key).is_file()


def test_store_closes_readonly_descriptor_when_verification_raises(
    tmp_path,
    monkeypatch,
):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    real_reopen = thumbnail_cache_module._open_output_readonly
    observed = {}

    def capture_reopen(path):
        descriptor = real_reopen(path)
        observed["descriptor"] = descriptor
        return descriptor

    def fail_verification(descriptor, maximum_bytes):
        assert descriptor == observed["descriptor"]
        assert maximum_bytes > 0
        raise ThumbnailCacheSafetyError("injected verification failure")

    monkeypatch.setattr(
        thumbnail_cache_module,
        "_open_output_readonly",
        capture_reopen,
    )
    monkeypatch.setattr(
        thumbnail_cache_module,
        "_read_open_output",
        fail_verification,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="injected verification failure"):
        cache.store(key, _solid_image())

    with pytest.raises(OSError) as caught:
        os.fstat(observed["descriptor"])
    assert caught.value.errno == errno.EBADF

    # On Windows this also proves that closing the CRT descriptor released the
    # no-write-sharing kernel handle.  A leaked handle would reject O_RDWR.
    if os.name == "nt":
        writer = os.open(
            cache.path_for_key(key),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        os.close(writer)


@pytest.mark.skipif(os.name != "nt", reason="Windows no-write sharing contract")
def test_windows_store_holds_no_write_lease_during_byte_verification(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    real_read = thumbnail_cache_module._read_open_output
    write_blocked = False

    def attempt_write(descriptor, maximum_bytes):
        nonlocal write_blocked
        target = cache.path_for_key(key)
        try:
            writer = os.open(
                target,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except OSError:
            write_blocked = True
        else:
            os.close(writer)
        return real_read(descriptor, maximum_bytes)

    monkeypatch.setattr(
        thumbnail_cache_module,
        "_read_open_output",
        attempt_write,
    )

    assert cache.store(key, _solid_image())
    assert write_blocked


def test_cleanup_refuses_reparse_marked_shard_without_deleting_it(
    tmp_path,
    monkeypatch,
):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    target.parent.mkdir(parents=True)
    original = b"simulated reparse content"
    target.write_bytes(original)
    shard_key = os.path.normcase(os.path.abspath(os.fspath(target.parent)))
    real_is_reparse_point = thumbnail_cache_module.is_reparse_point

    def injected_reparse(item):
        item_path = getattr(item, "path", None)
        if item_path is not None:
            return os.path.normcase(os.path.abspath(os.fspath(item_path))) == shard_key
        return real_is_reparse_point(item)

    # ``stat_result`` does not carry its path, so target the shard's unique
    # inode/file-index through the captured stat object in this test.
    shard_stat = os.lstat(target.parent)

    def injected_by_identity(item):
        if getattr(item, "st_dev", None) == shard_stat.st_dev and getattr(item, "st_ino", None) == shard_stat.st_ino:
            return True
        return injected_reparse(item)

    monkeypatch.setattr(
        thumbnail_cache_module,
        "is_reparse_point",
        injected_by_identity,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="linked shards"):
        cache.cleanup()

    assert target.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_posix_unsafe_cache_root_blocks_store_load_and_cleanup(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    cache.cache_dir.mkdir(mode=0o700)
    os.chmod(cache.cache_dir, 0o777)
    key = _key(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must remain untouched")

    try:
        with pytest.raises(ThumbnailCacheSafetyError, match="group/world-writable"):
            cache.store(key, _solid_image())
        with pytest.raises(ThumbnailCacheSafetyError, match="group/world-writable"):
            cache.load(key, QSize(40, 30))
        with pytest.raises(ThumbnailCacheSafetyError, match="group/world-writable"):
            cache.cleanup()
    finally:
        os.chmod(cache.cache_dir, 0o700)

    assert outside.read_bytes() == b"must remain untouched"
    assert not cache.path_for_key(key).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership contract")
def test_posix_wrong_owner_is_rejected_without_chown(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    cache.cache_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        thumbnail_cache_module,
        "_current_posix_uid",
        lambda: os.geteuid() + 1,
    )

    with pytest.raises(ThumbnailCacheSafetyError, match="owned by the current user"):
        cache.store(_key(tmp_path), _solid_image())
    with pytest.raises(ThumbnailCacheSafetyError, match="owned by the current user"):
        cache.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_posix_created_cache_hierarchy_is_private(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "new-parent" / "cache")
    key = _key(tmp_path)

    assert cache.store(key, _solid_image())

    for directory in (
        tmp_path / "new-parent",
        cache.cache_dir,
        cache.path_for_key(key).parent,
    ):
        directory_stat = os.lstat(directory)
        assert directory_stat.st_uid == os.geteuid()
        assert stat.S_IMODE(directory_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH) == 0


def test_store_never_replaces_existing_regular_target(tmp_path):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    target.parent.mkdir(parents=True)
    existing_payload = b"existing cache target must not be overwritten"
    target.write_bytes(existing_payload)

    assert cache.store(key, _solid_image()) is False

    assert target.read_bytes() == existing_payload


def test_store_loses_last_moment_create_race_without_touching_winner(
    tmp_path,
    monkeypatch,
):
    cache = ThumbnailDiskCache(tmp_path / "cache")
    key = _key(tmp_path)
    target = cache.path_for_key(key)
    winner_payload = b"last-moment create-only winner must remain byte-identical"
    real_open = os.open
    raced = False

    def create_winner_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        is_target = os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(
            os.path.abspath(os.fspath(target))
        )
        if is_target and flags & os.O_EXCL and not raced:
            raced = True
            winner_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            winner_fd = real_open(path, winner_flags, 0o600)
            try:
                assert os.write(winner_fd, winner_payload) == len(winner_payload)
                os.fsync(winner_fd)
            finally:
                os.close(winner_fd)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(thumbnail_cache_module.os, "open", create_winner_then_open)

    assert cache.store(key, _solid_image()) is False

    assert raced
    assert target.read_bytes() == winner_payload
