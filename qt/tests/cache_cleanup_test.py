from types import SimpleNamespace

import qt.app as app_module


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

    app_module.DupeGuru.clearCacheTriggered(window)

    assert calls == ["catalog", "picture", "hash"]
    assert len(prompts) == 1
    assert "Persistent Catalog" in prompts[0][1]
    assert "scan history and unfinished scans" in prompts[0][1]
    assert "2.0 KB" in prompts[0][1]
    assert len(FakeMessageBox.information_calls) == 1
    assert FakeMessageBox.critical_calls == []
