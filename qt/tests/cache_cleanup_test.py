from types import SimpleNamespace

import qt.app as app_module
import qt.pe.thumbnail_cache as thumbnail_cache_module
from qt.pe.thumbnail_cache import ThumbnailCacheSafetyError


class FakeMessageBox:
    class StandardButton:
        No = object()

    information_calls = []
    critical_calls = []

    @classmethod
    def information(cls, *args):
        cls.information_calls.append(args)

    @classmethod
    def critical(cls, *args):
        cls.critical_calls.append(args)


def test_clear_cache_action_names_scope_and_clears_catalog_first(monkeypatch):
    calls = []
    prompts = []
    model = SimpleNamespace(
        catalog_storage_size=lambda: 2048,
        clear_catalog=lambda: calls.append("catalog"),
        clear_picture_cache=lambda: calls.append("picture"),
        clear_hash_cache=lambda: calls.append("hash"),
    )
    window = SimpleNamespace(
        model=model,
        confirm=lambda title, message, default: prompts.append((title, message, default)) or True,
    )
    FakeMessageBox.information_calls = []
    FakeMessageBox.critical_calls = []
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(app_module, "QApplication", SimpleNamespace(activeWindow=lambda: None))
    monkeypatch.setattr(
        app_module,
        "clear_default_thumbnail_cache",
        lambda: calls.append("thumbnails"),
    )

    app_module.DupeGuru.clearCacheTriggered(window)

    assert calls == ["catalog", "picture", "hash", "thumbnails"]
    assert len(prompts) == 1
    assert "Persistent Catalog" in prompts[0][1]
    assert "scan history and unfinished scans" in prompts[0][1]
    assert "on-demand picture thumbnails" in prompts[0][1]
    assert "2.0 KB" in prompts[0][1]
    assert len(FakeMessageBox.information_calls) == 1
    assert FakeMessageBox.critical_calls == []


def test_clear_default_thumbnail_cache_removes_only_owned_entries(tmp_path, monkeypatch):
    app_cache_root = tmp_path / "dupeguru-neo"
    cache_dir = app_cache_root / "picture-thumbnails-v2"
    shard = cache_dir / "aa"
    shard.mkdir(parents=True)
    app_cache_root.chmod(0o700)
    cache_dir.chmod(0o700)
    shard.chmod(0o700)

    key = "a" * 64
    owned_entry = shard / "{}.png".format(key)
    payload = b"rebuildable thumbnail"
    owned_entry.write_bytes(payload)
    unrelated_inside_cache = cache_dir / "unrelated.txt"
    unrelated_inside_cache.write_bytes(b"not a thumbnail cache entry")
    unrelated_outside_cache = tmp_path / "user-data.txt"
    unrelated_outside_cache.write_bytes(b"user data")
    monkeypatch.setattr(
        thumbnail_cache_module,
        "default_thumbnail_cache_dir",
        lambda: cache_dir,
    )

    assert thumbnail_cache_module.clear_default_thumbnail_cache() == (1, len(payload))

    assert not owned_entry.exists()
    assert unrelated_inside_cache.read_bytes() == b"not a thumbnail cache entry"
    assert unrelated_outside_cache.read_bytes() == b"user data"


def test_clear_cache_reports_unsafe_thumbnail_cache_after_prior_cleanup(monkeypatch):
    calls = []
    model = SimpleNamespace(
        catalog_storage_size=lambda: 0,
        clear_catalog=lambda: calls.append("catalog"),
        clear_picture_cache=lambda: calls.append("picture"),
        clear_hash_cache=lambda: calls.append("hash"),
    )
    window = SimpleNamespace(
        model=model,
        confirm=lambda *_args: True,
    )
    FakeMessageBox.information_calls = []
    FakeMessageBox.critical_calls = []
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(app_module, "QApplication", SimpleNamespace(activeWindow=lambda: None))

    def reject_unsafe_cache():
        calls.append("thumbnails")
        raise ThumbnailCacheSafetyError("unsafe thumbnail cache")

    monkeypatch.setattr(app_module, "clear_default_thumbnail_cache", reject_unsafe_cache)

    app_module.DupeGuru.clearCacheTriggered(window)

    assert calls == ["catalog", "picture", "hash", "thumbnails"]
    assert FakeMessageBox.information_calls == []
    assert len(FakeMessageBox.critical_calls) == 1
    assert "Some rebuildable data may already have been cleared" in FakeMessageBox.critical_calls[0][2]
    assert "unsafe thumbnail cache" in FakeMessageBox.critical_calls[0][2]
