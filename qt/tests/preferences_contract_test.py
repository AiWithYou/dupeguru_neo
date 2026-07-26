# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def test_empty_folder_option_is_described_as_move_only():
    dialog_sources = (
        REPOSITORY / "qt" / "se" / "preferences_dialog.py",
        REPOSITORY / "qt" / "me" / "preferences_dialog.py",
        REPOSITORY / "qt" / "pe" / "preferences_dialog.py",
    )
    for source in dialog_sources:
        text = source.read_text(encoding="utf-8")
        assert "Remove empty folders after move" in text
        assert "Remove empty folders on delete or move" not in text

    help_text = (REPOSITORY / "help" / "en" / "preferences.rst").read_text(encoding="utf-8")
    assert "**Remove empty folders after move:**" in help_text
    assert "directory deletion" in " ".join(help_text.split())
