import re
from pathlib import Path

import polib
from PyQt6.QtCore import QSettings

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
