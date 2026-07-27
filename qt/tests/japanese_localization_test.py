import re
from pathlib import Path

import polib
from PyQt6.QtCore import QSettings

import run
from qt import platform
from qt import preferences as preferences_module
from qt.preferences import Preferences, get_langnames

ROOT = Path(__file__).parents[2]
DOMAINS = ("core", "columns", "ui")
BRACE_FIELD = re.compile(r"\{[^{}]*\}")
PERCENT_FIELD = re.compile(r"%(?:\([^)]+\))?[#0\- +]?(?:\d+|\*)?(?:\.\d+|\.\*)?[hlL]?[diouxXeEfFgGcrs%]")


def _fields(text):
    return sorted(BRACE_FIELD.findall(text)), sorted(field for field in PERCENT_FIELD.findall(text) if field != "%%")


def test_japanese_catalogs_cover_every_current_message():
    for domain in DOMAINS:
        template = polib.pofile(str(ROOT / "locale" / f"{domain}.pot"))
        catalog = polib.pofile(str(ROOT / "locale" / "ja" / "LC_MESSAGES" / f"{domain}.po"))
        translated = {entry.msgid: entry for entry in catalog if not entry.obsolete}

        assert set(translated) == {entry.msgid for entry in template}
        assert all(entry.translated() for entry in translated.values())
        assert all("fuzzy" not in entry.flags for entry in translated.values())


def test_japanese_translations_preserve_format_fields():
    for domain in DOMAINS:
        catalog = polib.pofile(str(ROOT / "locale" / "ja" / "LC_MESSAGES" / f"{domain}.po"))
        for entry in catalog:
            if entry.obsolete:
                continue
            assert _fields(entry.msgid) == _fields(entry.msgstr), entry.msgid


def test_japanese_is_selectable_and_persisted(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.ini"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    monkeypatch.setattr(preferences_module, "create_qsettings", lambda: settings)

    assert "ja" in get_langnames()
    preferences = Preferences()
    preferences.load()
    preferences.language = "ja"
    preferences.save()

    loaded = Preferences()
    loaded.load()
    assert loaded.language == "ja"


def test_frozen_data_root_does_not_depend_on_a_synthetic_module_file(tmp_path):
    frozen_root = tmp_path / "_internal"
    synthetic_module = tmp_path / "_internal" / "qt" / "platform.py"

    assert platform._application_base_path(synthetic_module, frozen_root) == str(frozen_root.resolve())


def test_frozen_self_test_requires_reachable_japanese_ui_and_help(monkeypatch, tmp_path):
    messages = tmp_path / "locale" / "ja" / "LC_MESSAGES"
    messages.mkdir(parents=True)
    catalog = polib.POFile()
    catalog.metadata = {
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
        "Language": "ja",
    }
    catalog.append(polib.POEntry(msgid="File", msgstr="ファイル"))
    catalog.save_as_mofile(str(messages / "ui.mo"))
    help_entry = tmp_path / "help" / "ja" / "index.html"
    help_entry.parent.mkdir(parents=True)
    help_entry.write_text('<html lang="ja"></html>', encoding="utf-8")

    monkeypatch.setattr(run, "BASE_PATH", str(tmp_path))
    monkeypatch.setattr(run.sys, "frozen", True, raising=False)

    run._validate_frozen_localizations()
