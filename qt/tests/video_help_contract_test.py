# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def test_qt_help_menu_explicitly_routes_to_the_cli_only_video_workflow():
    app_source = (REPOSITORY / "qt" / "app.py").read_text(encoding="utf-8")
    directory_source = (REPOSITORY / "qt" / "directories_dialog.py").read_text(encoding="utf-8")
    result_source = (REPOSITORY / "qt" / "result_window.py").read_text(encoding="utf-8")
    help_source = (REPOSITORY / "help" / "en" / "video.rst").read_text(encoding="utf-8")

    assert "actionShowVideoWorkflow" in app_source
    assert '"video.html", "help/en/video.rst"' in app_source
    assert "self.app.actionShowVideoWorkflow" in directory_source
    assert "self.app.actionShowVideoWorkflow" in result_source
    assert "if action not in self.menuHelp.actions()" in directory_source
    assert "if action not in self.menuHelp.actions()" in result_source
    assert "does not currently run the video scanner" in help_source
    assert "documentation only" in help_source
    assert "dupeguru video scan" in help_source
