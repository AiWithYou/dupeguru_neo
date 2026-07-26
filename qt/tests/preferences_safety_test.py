# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import os
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QRect, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow

import qt.preferences as preferences_module
import qt.util as qt_util
from core.directories import (
    DIRECT_DISCOVERY_MAX_FILES,
    DIRECT_DISCOVERY_MAX_FOLDERS,
    DIRECT_DISCOVERY_MAX_ISSUES,
    DIRECT_DISCOVERY_MAX_SECONDS,
)
from qt.column import MAX_COLUMN_WIDTH, Columns
from qt.preferences import Preferences, PreferencesBase
from qt.recent import MAX_RECENT_ITEM_CHARS, Recent


class _PreferenceProbe:
    _coerce_rect = staticmethod(PreferencesBase._coerce_rect)

    def __init__(self, value):
        self.value = value

    def get_value(self, _name, default=None):
        return self.value if self.value is not None else default


class _Signal:
    def connect(self, callback):
        self.callback = callback


class _Settings:
    def __init__(self, value):
        self.value_to_return = value

    def contains(self, _name):
        return True

    def value(self, _name):
        return self.value_to_return


class _SettingsProbe:
    _coerce_setting_value = staticmethod(PreferencesBase._coerce_setting_value)

    def __init__(self, value):
        self._settings = _Settings(value)


class _RealSettingsProbe:
    _coerce_rect = staticmethod(PreferencesBase._coerce_rect)
    _coerce_setting_value = staticmethod(PreferencesBase._coerce_setting_value)
    get_value = PreferencesBase.get_value
    set_value = PreferencesBase.set_value

    def __init__(self, settings):
        self._settings = settings


def test_corrupt_rect_values_recover_to_the_typed_default():
    default = QRect(10, 20, 640, 480)
    corrupt_values = (
        "10,20,640,480",
        [10, 20, 640],
        [True, 20, 640, 480],
        [10, 20, -1, 480],
        [2**63, 20, 640, 480],
    )

    for value in corrupt_values:
        probe = _PreferenceProbe(value)
        assert PreferencesBase.get_rect(probe, "window", default) == default


def test_valid_rect_sequence_is_loaded_without_mutating_the_input():
    value = [10, 20, 640, 480]
    probe = _PreferenceProbe(value)

    assert PreferencesBase.get_rect(probe, "window") == QRect(*value)
    assert value == [10, 20, 640, 480]


def test_qsettings_rect_sequence_is_accepted_under_a_qrect_default_contract():
    default = QRect(1, 2, 3, 4)
    probe = _SettingsProbe([10, 20, 640, 480])

    value = PreferencesBase.get_value(probe, "window", default)

    assert PreferencesBase._coerce_rect(value) == QRect(10, 20, 640, 480)


def test_corrupt_dock_geometry_returns_a_typed_safe_default():
    probe = _PreferenceProbe([0, 1, "invalid-area", 10, 20, 640, 480])
    widget = SimpleNamespace()

    result = PreferencesBase.restoreGeometry(probe, "dock", widget)

    assert result == (False, Qt.DockWidgetArea.NoDockWidgetArea)


def test_recent_preferences_accept_only_a_bounded_list_of_safe_strings():
    valid = ["one", "two", "one", "three", "four"]
    invalid = [None, 42, "", "nul\0path", "x" * (MAX_RECENT_ITEM_CHARS + 1)]
    app = SimpleNamespace(
        prefs=SimpleNamespace(recent=invalid + valid),
        willSavePrefs=_Signal(),
    )

    recent = Recent(app, "recent", max_item_count=3)

    assert recent._items == ["one", "two", "three"]


def test_typed_qsettings_values_recover_to_defaults_before_reaching_qt_widgets():
    cases = (
        ("not-a-bool", False, False),
        (2**63, 12, 12),
        (-5, 12, -5),
        ("12", 7, 12),
        (["ok"], [], ["ok"]),
        ("wrong", [], []),
    )

    for stored, default, expected in cases:
        probe = _SettingsProbe(stored)
        assert PreferencesBase.get_value(probe, "setting", default) == expected


@pytest.mark.parametrize("value", ["123", "true", "false"])
def test_schema_typed_string_round_trip_does_not_coerce_command_text(tmp_path, value):
    settings_path = tmp_path / "settings.ini"
    writer = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))

    PreferencesBase.set_value(writer, "CustomCommand", value)
    writer._settings.sync()
    reader = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))

    assert PreferencesBase.get_value(reader, "CustomCommand", "") == value


def test_legacy_dock_area_variant_is_bounded_and_new_writes_use_plain_integer(tmp_path):
    settings_path = tmp_path / "settings.ini"
    legacy = QSettings(str(settings_path), QSettings.Format.IniFormat)
    geometry = [0, 1, Qt.DockWidgetArea.LeftDockWidgetArea, 10, 20, 640, 480]
    legacy.setValue("DetailsWindowRect", geometry)
    legacy.sync()
    reader = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))

    loaded = PreferencesBase.get_value(reader, "DetailsWindowRect")

    assert loaded == geometry
    writer = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))
    PreferencesBase.set_value(writer, "DetailsWindowRect", geometry)
    writer._settings.sync()
    serialized = QSettings(str(settings_path), QSettings.Format.IniFormat).value("DetailsWindowRect")
    assert serialized[2] == Qt.DockWidgetArea.LeftDockWidgetArea.value
    assert type(serialized[2]) is int


def test_real_qsettings_dock_geometry_round_trip_supports_caller_defined_key(tmp_path):
    application = QApplication.instance() or QApplication([])
    settings_path = tmp_path / "settings.ini"
    writer = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))
    window = QMainWindow()
    dock = QDockWidget("dock", window)
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    # Production dock wrappers expose their owning window as an attribute.
    dock.parent = window

    PreferencesBase.saveGeometry(writer, "caller-defined-dock-key", dock)
    writer._settings.sync()
    reader = _RealSettingsProbe(QSettings(str(settings_path), QSettings.Format.IniFormat))

    restored = PreferencesBase.restoreGeometry(
        reader,
        "caller-defined-dock-key",
        dock,
    )
    serialized = reader._settings.value("caller-defined-dock-key")
    assert restored == (True, Qt.DockWidgetArea.LeftDockWidgetArea)
    assert type(serialized[2]) is int
    assert application is not None


def test_header_runtime_width_limit_matches_preference_loader():
    class HeaderProbe:
        def __init__(self):
            self.sectionMoved = _Signal()
            self.sectionResized = _Signal()
            self.maximum = None

        def setDefaultAlignment(self, _alignment):
            pass

        def setMaximumSectionSize(self, maximum):
            self.maximum = maximum

    header = HeaderProbe()
    model = SimpleNamespace(column_list=[])

    Columns(model, [], header)

    assert MAX_COLUMN_WIDTH == preferences_module._MAX_COLUMN_WIDTH
    assert header.maximum == preferences_module._MAX_COLUMN_WIDTH


def test_direct_discovery_preferences_accept_only_positive_values_within_hard_caps(
    tmp_path,
    monkeypatch,
):
    application = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("DirectScanMaxFiles", "17")
    settings.setValue("DirectScanMaxFolders", 0)
    settings.setValue("DirectScanMaxIssues", DIRECT_DISCOVERY_MAX_ISSUES + 1)
    settings.setValue("DirectScanMaxSeconds", True)
    settings.sync()
    monkeypatch.setattr(preferences_module, "create_qsettings", lambda: settings)

    prefs = Preferences()
    prefs.load()

    assert prefs.direct_scan_max_files == 17
    assert prefs.direct_scan_max_folders == DIRECT_DISCOVERY_MAX_FOLDERS
    assert prefs.direct_scan_max_issues == DIRECT_DISCOVERY_MAX_ISSUES
    assert prefs.direct_scan_max_seconds == DIRECT_DISCOVERY_MAX_SECONDS
    assert application is not None


def test_direct_discovery_preferences_normalize_invalid_runtime_values_before_save(
    tmp_path,
    monkeypatch,
):
    application = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(preferences_module, "create_qsettings", lambda: settings)
    prefs = Preferences()
    prefs.load()
    prefs.direct_scan_max_files = 0
    prefs.direct_scan_max_folders = DIRECT_DISCOVERY_MAX_FOLDERS + 1
    prefs.direct_scan_max_issues = "100"
    prefs.direct_scan_max_seconds = False

    prefs.save()
    settings.sync()

    assert settings.value("DirectScanMaxFiles", type=int) == DIRECT_DISCOVERY_MAX_FILES
    assert settings.value("DirectScanMaxFolders", type=int) == DIRECT_DISCOVERY_MAX_FOLDERS
    assert settings.value("DirectScanMaxIssues", type=int) == DIRECT_DISCOVERY_MAX_ISSUES
    assert settings.value("DirectScanMaxSeconds", type=int) == DIRECT_DISCOVERY_MAX_SECONDS
    assert application is not None


def test_oversized_qsettings_array_is_rejected_before_recursive_adjustment(
    monkeypatch,
):
    adjusted = False

    def unexpected_adjustment(_value):
        nonlocal adjusted
        adjusted = True
        raise AssertionError("oversized settings must be rejected first")

    monkeypatch.setattr(
        preferences_module,
        "_adjust_after_deserialization",
        unexpected_adjustment,
    )
    value = ["path"] * (preferences_module._MAX_SETTING_LIST_ITEMS + 1)
    probe = _SettingsProbe(value)

    assert PreferencesBase.get_value(probe, "recent", []) == []
    assert adjusted is False


def test_deep_or_excessive_external_path_settings_recover_to_empty_defaults():
    deep = "leaf"
    for _index in range(preferences_module._MAX_SETTING_NESTING + 1):
        deep = [deep]
    maximum_path = "x" * preferences_module._MAX_SETTING_STRING_CHARS
    excessive_paths = [
        maximum_path for _index in range(preferences_module._MAX_SETTING_TOTAL_STRING_CHARS // len(maximum_path) + 1)
    ]

    assert PreferencesBase.get_value(_SettingsProbe(deep), "recent", []) == []
    assert (
        PreferencesBase.get_value(
            _SettingsProbe(excessive_paths),
            "recent",
            [],
        )
        == []
    )


def test_untyped_column_settings_require_bounded_exact_fields():
    valid = {"index": "2", "width": "120", "visible": "true"}
    invalid_values = (
        {"index": 0, "width": preferences_module._MAX_COLUMN_WIDTH + 1},
        {"index": -1, "width": 120},
        {"index": 0, "width": 120, "unexpected": True},
        {"index": 0, "width": 120, "visible": 1},
    )

    assert PreferencesBase.get_value(
        _SettingsProbe(valid),
        "ResultTable.Columns.name",
    ) == {
        "index": 2,
        "width": 120,
        "visible": True,
    }
    for value in invalid_values:
        assert (
            PreferencesBase.get_value(
                _SettingsProbe(value),
                "ResultTable.Columns.name",
            )
            is None
        )


def test_untyped_debug_and_custom_command_keys_reject_cross_typed_values():
    assert PreferencesBase.get_value(_SettingsProbe(True), "DebugMode") is True
    assert PreferencesBase.get_value(_SettingsProbe(["truthy"]), "DebugMode") is None
    assert (
        PreferencesBase.get_value(
            _SettingsProbe("tool --argument"),
            "CustomCommand",
        )
        == "tool --argument"
    )
    assert (
        PreferencesBase.get_value(
            _SettingsProbe(["tool", "--argument"]),
            "CustomCommand",
        )
        is None
    )


def test_oversized_settings_file_is_not_opened_or_modified(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.ini"
    original = b"x" * 32
    settings_path.write_bytes(original)
    monkeypatch.setattr(qt_util, "MAX_QSETTINGS_FILE_BYTES", 16)

    def unexpected_qsettings(*_args, **_kwargs):
        raise AssertionError("unsafe settings file must not be opened by QSettings")

    monkeypatch.setattr(qt_util, "QSettings", unexpected_qsettings)

    settings = qt_util._ini_settings(str(settings_path), portable=True)

    assert settings.value("Portable") is True
    assert settings_path.read_bytes() == original


def test_network_settings_path_is_rejected_before_qsettings_open():
    assert not qt_util._settings_file_is_safe(r"\\server\share\dupeguru\settings.ini")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_hardlinked_settings_file_is_ignored_without_modifying_either_path(
    tmp_path,
):
    original_path = tmp_path / "original.ini"
    settings_path = tmp_path / "settings.ini"
    original = b"[General]\nPortable=false\n"
    original_path.write_bytes(original)
    try:
        os.link(original_path, settings_path)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    settings = qt_util._ini_settings(str(settings_path), portable=True)

    assert settings.value("Portable") is True
    assert original_path.read_bytes() == original
    assert settings_path.read_bytes() == original
