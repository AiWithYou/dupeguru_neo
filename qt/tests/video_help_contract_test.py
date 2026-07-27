# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

from pathlib import Path

from qt import platform

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


def test_japanese_ui_routes_to_bundled_japanese_help():
    app_source = (REPOSITORY / "qt" / "app.py").read_text(encoding="utf-8")
    japanese_video = (REPOSITORY / "help" / "ja" / "video.rst").read_text(encoding="utf-8")

    assert platform.localized_help_path("ja").endswith(str(Path("help", "ja")))
    assert platform.localized_help_path("en") == platform.HELP_PATH
    assert '"help/ja/video.rst"' in app_source
    assert "類似動画ワークフロー" in japanese_video
    assert "dupeguru video scan" in japanese_video
