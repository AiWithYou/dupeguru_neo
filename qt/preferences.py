# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from PyQt6.QtWidgets import QApplication, QDockWidget
from PyQt6.QtCore import Qt, QRect, QObject, pyqtSignal
from PyQt6.QtGui import QColor

import math

from hscommon import trans
from hscommon.plat import ISLINUX
from core.app import AppMode
from core.directories import (
    DIRECT_DISCOVERY_MAX_FILES,
    DIRECT_DISCOVERY_MAX_FOLDERS,
    DIRECT_DISCOVERY_MAX_ISSUES,
    DIRECT_DISCOVERY_MAX_SECONDS,
)
from core.scanner import ScanType
from qt.column import MAX_COLUMN_WIDTH
from qt.util import create_qsettings

_QT_INT_MIN = -(2**31)
_QT_INT_MAX = (2**31) - 1
_MAX_SETTING_STRING_CHARS = 32_767
_MAX_SETTING_LIST_ITEMS = 10_000
_MAX_SETTING_DICT_ITEMS = 64
_MAX_SETTING_NESTING = 4
_MAX_SETTING_TOTAL_NODES = 20_000
_MAX_SETTING_TOTAL_STRING_CHARS = 2 * 1024 * 1024
_MAX_COLUMN_INDEX = 10_000
_MAX_COLUMN_WIDTH = MAX_COLUMN_WIDTH
_PERSISTED_DOCK_AREAS = frozenset(
    {
        Qt.DockWidgetArea.NoDockWidgetArea,
        Qt.DockWidgetArea.LeftDockWidgetArea,
        Qt.DockWidgetArea.RightDockWidgetArea,
        Qt.DockWidgetArea.TopDockWidgetArea,
        Qt.DockWidgetArea.BottomDockWidgetArea,
    }
)


def get_langnames():
    tr = trans.trget("ui")
    return {
        "cs": tr("Czech"),
        "de": tr("German"),
        "el": tr("Greek"),
        "en": tr("English"),
        "es": tr("Spanish"),
        "fr": tr("French"),
        "hy": tr("Armenian"),
        "it": tr("Italian"),
        "ja": tr("Japanese"),
        "ko": tr("Korean"),
        "ms": tr("Malay"),
        "nl": tr("Dutch"),
        "pl_PL": tr("Polish"),
        "pt_BR": tr("Brazilian"),
        "ru": tr("Russian"),
        "tr": tr("Turkish"),
        "uk": tr("Ukrainian"),
        "vi": tr("Vietnamese"),
        "zh_CN": tr("Chinese (Simplified)"),
    }


def _normalize_for_serialization(v):
    # QSettings doesn't consider set/tuple as "native" typs for serialization, so if we don't
    # change them into a list, we get a weird serialized QVariant value which isn't a very
    # "portable" value.
    if isinstance(v, Qt.DockWidgetArea):
        return int(v.value)
    if isinstance(v, (set, tuple)):
        v = list(v)
    if isinstance(v, list):
        v = [_normalize_for_serialization(item) for item in v]
    elif isinstance(v, dict):
        v = {key: _normalize_for_serialization(item) for key, item in v.items()}
    return v


def _adjust_after_deserialization(v):
    """Normalize containers while preserving strings until their schema is known."""

    if isinstance(v, list):
        return [_adjust_after_deserialization(sub) for sub in v]
    if isinstance(v, dict):
        return {key: _adjust_after_deserialization(sub) for key, sub in v.items()}
    return v


def _coerce_bool(value):
    if type(value) is bool:
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _coerce_int(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str):
        try:
            result = int(value)
        except ValueError:
            return None
    else:
        return None
    return result if _QT_INT_MIN <= result <= _QT_INT_MAX else None


def _coerce_dock_area(value):
    try:
        area = value if isinstance(value, Qt.DockWidgetArea) else Qt.DockWidgetArea(value)
    except (TypeError, ValueError):
        return None
    return area if area in _PERSISTED_DOCK_AREAS else None


def _bounded_positive_int(value, default, maximum):
    if type(value) is int and 1 <= value <= maximum:
        return value
    return default


def _setting_structure_is_bounded(value):
    """Reject a hostile QVariant tree before recursively normalizing it."""

    pending = [(value, 0)]
    total_nodes = 0
    total_string_chars = 0
    while pending:
        current, depth = pending.pop()
        total_nodes += 1
        if total_nodes > _MAX_SETTING_TOTAL_NODES:
            return False
        if isinstance(current, str):
            if len(current) > _MAX_SETTING_STRING_CHARS or "\0" in current:
                return False
            total_string_chars += len(current)
            if total_string_chars > _MAX_SETTING_TOTAL_STRING_CHARS:
                return False
        elif type(current) in {list, tuple}:
            if len(current) > _MAX_SETTING_LIST_ITEMS:
                return False
            if current and depth >= _MAX_SETTING_NESTING:
                return False
            pending.extend((item, depth + 1) for item in current)
        elif type(current) is dict:
            if len(current) > _MAX_SETTING_DICT_ITEMS:
                return False
            if current and depth >= _MAX_SETTING_NESTING:
                return False
            for key, item in current.items():
                if type(key) is not str:
                    return False
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
        elif type(current) is int:
            if not _QT_INT_MIN <= current <= _QT_INT_MAX:
                return False
        elif type(current) is float:
            if not math.isfinite(current):
                return False
        elif current is None or type(current) is bool:
            continue
        elif isinstance(current, QColor):
            if not current.isValid():
                return False
        elif isinstance(current, QRect):
            if PreferencesBase._coerce_rect(current) is None:
                return False
        elif isinstance(current, Qt.DockWidgetArea):
            if _coerce_dock_area(current) is None:
                return False
        else:
            return False
    return True


class PreferencesBase(QObject):
    prefsChanged = pyqtSignal()

    def __init__(self):
        QObject.__init__(self)
        self.reset()
        self._settings = create_qsettings()

    def _load_values(self, settings):
        # Implemented in subclasses
        pass

    @staticmethod
    def _coerce_rect(value):
        if isinstance(value, QRect):
            values = (value.x(), value.y(), value.width(), value.height())
        elif isinstance(value, (list, tuple)) and len(value) == 4:
            values = tuple(value)
        else:
            return None
        if any(type(item) is not int for item in values):
            return None
        x, y, width, height = values
        if not (
            _QT_INT_MIN <= x <= _QT_INT_MAX
            and _QT_INT_MIN <= y <= _QT_INT_MAX
            and 0 < width <= _QT_INT_MAX
            and 0 < height <= _QT_INT_MAX
        ):
            return None
        try:
            return QRect(x, y, width, height)
        except (OverflowError, TypeError, ValueError):
            return None

    def get_rect(self, name, default=None):
        value = self.get_value(name, default)
        rect = self._coerce_rect(value)
        if rect is not None:
            return rect
        return self._coerce_rect(default)

    def get_value(self, name, default=None):
        if self._settings.contains(name):
            raw_value = self._settings.value(name)
            if not _setting_structure_is_bounded(raw_value):
                return default
            result = _adjust_after_deserialization(raw_value)
            if result is not None:
                return self._coerce_setting_value(
                    result,
                    default,
                    name=name,
                )
            else:
                # If result is None, but still present in self._settings, it usually means a value
                # like "@Invalid".
                return default
        else:
            return default

    @staticmethod
    def _coerce_setting_value(value, default, *, name=None):
        """Use a typed default as the deserialization contract when available."""

        if default is None:
            return PreferencesBase._coerce_untyped_setting(value, name=name)
        if type(default) is bool:
            coerced = _coerce_bool(value)
            return default if coerced is None else coerced
        if type(default) is int:
            coerced = _coerce_int(value)
            return default if coerced is None else coerced
        if type(default) is float:
            if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
                return float(value)
            return default
        if isinstance(default, str):
            if isinstance(value, str) and len(value) <= _MAX_SETTING_STRING_CHARS and "\0" not in value:
                return value
            return default
        if isinstance(default, QColor):
            return value if isinstance(value, QColor) and value.isValid() else default
        if isinstance(default, list):
            return value if isinstance(value, list) and len(value) <= _MAX_SETTING_LIST_ITEMS else default
        if isinstance(default, QRect):
            return value if isinstance(value, (QRect, list, tuple)) else default
        return value if isinstance(value, type(default)) else default

    @staticmethod
    def _coerce_untyped_setting(value, *, name=None):
        """Validate the small set of legacy settings without typed defaults."""

        if name == "DebugMode":
            return _coerce_bool(value)
        if name == "CustomCommand":
            return value if isinstance(value, str) else None
        # saveGeometry() accepts caller-defined keys. Keep the bounded
        # seven-field candidate intact so restoreGeometry() can apply its
        # stricter maximized/docked/area/rectangle schema validation.
        if isinstance(value, (list, tuple)) and len(value) == 7:
            return value
        if isinstance(name, str) and name.endswith("WindowRect"):
            return value if isinstance(value, (QRect, list, tuple)) else None
        if isinstance(name, str) and ".Columns." in name and type(value) is dict:
            if (
                not {"index", "width"}
                <= set(value)
                <= {
                    "index",
                    "width",
                    "visible",
                }
            ):
                return None
            index = _coerce_int(value["index"])
            width = _coerce_int(value["width"])
            visible = _coerce_bool(value.get("visible")) if "visible" in value else None
            if (
                index is None
                or not 0 <= index <= _MAX_COLUMN_INDEX
                or width is None
                or not 0 <= width <= _MAX_COLUMN_WIDTH
                or ("visible" in value and visible is None)
            ):
                return None
            result = {"index": index, "width": width}
            if "visible" in value:
                result["visible"] = visible
            return result
        return None

    def load(self):
        self.reset()
        self._load_values(self._settings)

    def reset(self):
        # Implemented in subclasses
        pass

    def _save_values(self, settings):
        # Implemented in subclasses
        pass

    def save(self):
        self._save_values(self._settings)
        self._settings.sync()

    def set_rect(self, name, r):
        # About QRect conversion:
        # I think Qt supports putting basic structures like QRect directly in QSettings, but I prefer not
        # to rely on it and stay with generic structures.
        if isinstance(r, QRect):
            rect_as_list = [r.x(), r.y(), r.width(), r.height()]
            self.set_value(name, rect_as_list)

    def set_value(self, name, value):
        self._settings.setValue(name, _normalize_for_serialization(value))

    def saveGeometry(self, name, widget):
        # We save geometry under a 7-sized int array: first item is a flag
        # for whether the widget is maximized, second item is a flag for whether
        # the widget is docked, third item is a Qt::DockWidgetArea enum value,
        # serialized as an integer, and the other 4 are (x, y, w, h).
        m = 1 if widget.isMaximized() else 0
        d = 1 if isinstance(widget, QDockWidget) and not widget.isFloating() else 0
        area = widget.parent.dockWidgetArea(widget) if d else 0
        r = widget.geometry()
        rect_as_list = [r.x(), r.y(), r.width(), r.height()]
        self.set_value(name, [m, d, area] + rect_as_list)

    def restoreGeometry(self, name, widget):
        geometry = self.get_value(name)
        if not isinstance(geometry, (list, tuple)) or len(geometry) != 7:
            return False, Qt.DockWidgetArea.NoDockWidgetArea
        maximized, docked, area, x, y, width, height = geometry
        if type(maximized) is not int or maximized not in {0, 1}:
            return False, Qt.DockWidgetArea.NoDockWidgetArea
        if type(docked) is not int or docked not in {0, 1}:
            return False, Qt.DockWidgetArea.NoDockWidgetArea
        dock_area = _coerce_dock_area(area)
        if dock_area is None:
            return False, Qt.DockWidgetArea.NoDockWidgetArea
        rect = self._coerce_rect((x, y, width, height))
        if rect is None:
            return False, Qt.DockWidgetArea.NoDockWidgetArea
        if maximized:
            widget.setWindowState(Qt.WindowState.WindowMaximized)
        else:
            widget.setGeometry(rect)
            if isinstance(widget, QDockWidget):
                # Inform of the previous dock state and the area used
                return bool(docked), dock_area
        return False, Qt.DockWidgetArea.NoDockWidgetArea


class Preferences(PreferencesBase):
    def _load_values(self, settings):
        get = self.get_value
        self.filter_hardness = get("FilterHardness", self.filter_hardness)
        self.mix_file_kind = get("MixFileKind", self.mix_file_kind)
        self.ignore_hardlink_matches = get("IgnoreHardlinkMatches", self.ignore_hardlink_matches)
        self.cross_pool_only = get("CrossPoolOnly", self.cross_pool_only)
        self.use_regexp = get("UseRegexp", self.use_regexp)
        self.remove_empty_folders = get("RemoveEmptyFolders", self.remove_empty_folders)
        self.rehash_ignore_mtime = get("RehashIgnoreMTime", self.rehash_ignore_mtime)
        self.include_exists_check = get("IncludeExistsCheck", self.include_exists_check)
        self.direct_scan_max_files = _bounded_positive_int(
            get("DirectScanMaxFiles", self.direct_scan_max_files),
            DIRECT_DISCOVERY_MAX_FILES,
            DIRECT_DISCOVERY_MAX_FILES,
        )
        self.direct_scan_max_folders = _bounded_positive_int(
            get("DirectScanMaxFolders", self.direct_scan_max_folders),
            DIRECT_DISCOVERY_MAX_FOLDERS,
            DIRECT_DISCOVERY_MAX_FOLDERS,
        )
        self.direct_scan_max_issues = _bounded_positive_int(
            get("DirectScanMaxIssues", self.direct_scan_max_issues),
            DIRECT_DISCOVERY_MAX_ISSUES,
            DIRECT_DISCOVERY_MAX_ISSUES,
        )
        self.direct_scan_max_seconds = _bounded_positive_int(
            get("DirectScanMaxSeconds", self.direct_scan_max_seconds),
            DIRECT_DISCOVERY_MAX_SECONDS,
            DIRECT_DISCOVERY_MAX_SECONDS,
        )
        self.debug_mode = get("DebugMode", self.debug_mode)
        self.profile_scan = get("ProfileScan", self.profile_scan)
        self.destination_type = get("DestinationType", self.destination_type)
        self.custom_command = get("CustomCommand", self.custom_command)
        self.language = get("Language", self.language)
        if not self.language and trans.installed_lang:
            self.language = trans.installed_lang
        self.portable = get("Portable", False)
        self.use_dark_style = get("UseDarkStyle", False)
        self.use_native_dialogs = get("UseNativeDialogs", True)

        self.tableFontSize = get("TableFontSize", self.tableFontSize)
        self.reference_bold_font = get("ReferenceBoldFont", self.reference_bold_font)
        self.details_dialog_titlebar_enabled = get("DetailsDialogTitleBarEnabled", self.details_dialog_titlebar_enabled)
        self.details_dialog_vertical_titlebar = get(
            "DetailsDialogVerticalTitleBar", self.details_dialog_vertical_titlebar
        )
        # On Windows and MacOS, use internal icons by default
        self.details_dialog_override_theme_icons = (
            get("DetailsDialogOverrideThemeIcons", self.details_dialog_override_theme_icons) if ISLINUX else True
        )
        self.details_table_delta_foreground_color = get(
            "DetailsTableDeltaForegroundColor", self.details_table_delta_foreground_color
        )
        self.details_dialog_viewers_show_scrollbars = get(
            "DetailsDialogViewersShowScrollbars", self.details_dialog_viewers_show_scrollbars
        )

        self.result_table_ref_foreground_color = get(
            "ResultTableRefForegroundColor", self.result_table_ref_foreground_color
        )
        self.result_table_ref_background_color = get(
            "ResultTableRefBackgroundColor", self.result_table_ref_background_color
        )
        self.result_table_delta_foreground_color = get(
            "ResultTableDeltaForegroundColor", self.result_table_delta_foreground_color
        )

        self.resultWindowIsMaximized = get("ResultWindowIsMaximized", self.resultWindowIsMaximized)
        self.resultWindowRect = self.get_rect("ResultWindowRect", self.resultWindowRect)
        self.mainWindowIsMaximized = get("MainWindowIsMaximized", self.mainWindowIsMaximized)
        self.mainWindowRect = self.get_rect("MainWindowRect", self.mainWindowRect)
        self.directoriesWindowRect = self.get_rect("DirectoriesWindowRect", self.directoriesWindowRect)

        self.recentResults = get("RecentResults", self.recentResults)
        self.recentFolders = get("RecentFolders", self.recentFolders)
        self.tabs_default_pos = get("TabsDefaultPosition", self.tabs_default_pos)
        self.word_weighting = get("WordWeighting", self.word_weighting)
        self.match_similar = get("MatchSimilar", self.match_similar)
        self.ignore_small_files = get("IgnoreSmallFiles", self.ignore_small_files)
        self.small_file_threshold = get("SmallFileThreshold", self.small_file_threshold)
        self.ignore_large_files = get("IgnoreLargeFiles", self.ignore_large_files)
        self.large_file_threshold = get("LargeFileThreshold", self.large_file_threshold)
        self.big_file_partial_hashes = get("BigFilePartialHashes", self.big_file_partial_hashes)
        self.big_file_size_threshold = get("BigFileSizeThreshold", self.big_file_size_threshold)
        self.scan_tag_track = get("ScanTagTrack", self.scan_tag_track)
        self.scan_tag_artist = get("ScanTagArtist", self.scan_tag_artist)
        self.scan_tag_album = get("ScanTagAlbum", self.scan_tag_album)
        self.scan_tag_title = get("ScanTagTitle", self.scan_tag_title)
        self.scan_tag_genre = get("ScanTagGenre", self.scan_tag_genre)
        self.scan_tag_year = get("ScanTagYear", self.scan_tag_year)
        self.match_scaled = get("MatchScaled", self.match_scaled)
        self.match_rotated = get("MatchRotated", self.match_rotated)

    def reset(self):
        self.filter_hardness = 95
        self.mix_file_kind = True
        self.use_regexp = False
        self.ignore_hardlink_matches = False
        self.cross_pool_only = False
        self.remove_empty_folders = False
        self.rehash_ignore_mtime = False
        self.include_exists_check = True
        self.direct_scan_max_files = DIRECT_DISCOVERY_MAX_FILES
        self.direct_scan_max_folders = DIRECT_DISCOVERY_MAX_FOLDERS
        self.direct_scan_max_issues = DIRECT_DISCOVERY_MAX_ISSUES
        self.direct_scan_max_seconds = DIRECT_DISCOVERY_MAX_SECONDS
        self.debug_mode = False
        self.profile_scan = False
        self.destination_type = 1
        self.custom_command = ""
        self.language = trans.installed_lang if trans.installed_lang else ""
        self.use_dark_style = False
        self.use_native_dialogs = True

        self.tableFontSize = QApplication.font().pointSize()
        self.reference_bold_font = True
        self.details_dialog_titlebar_enabled = True
        self.details_dialog_vertical_titlebar = True
        self.details_table_delta_foreground_color = QColor(250, 20, 20)  # red
        # By default use internal icons on platforms other than Linux for now
        self.details_dialog_override_theme_icons = False if not ISLINUX else True
        self.details_dialog_viewers_show_scrollbars = True
        self.result_table_ref_foreground_color = QColor(Qt.GlobalColor.blue)
        self.result_table_ref_background_color = QColor(Qt.GlobalColor.lightGray)
        self.result_table_delta_foreground_color = QColor(255, 142, 40)  # orange
        self.resultWindowIsMaximized = False
        self.resultWindowRect = None
        self.directoriesWindowRect = None
        self.mainWindowRect = None
        self.mainWindowIsMaximized = False
        self.recentResults = []
        self.recentFolders = []

        self.tabs_default_pos = True
        self.word_weighting = True
        self.match_similar = False
        self.ignore_small_files = True
        self.small_file_threshold = 10  # KB
        self.ignore_large_files = False
        self.large_file_threshold = 1000  # MB
        self.big_file_partial_hashes = False
        self.big_file_size_threshold = 100  # MB
        self.scan_tag_track = False
        self.scan_tag_artist = True
        self.scan_tag_album = True
        self.scan_tag_title = True
        self.scan_tag_genre = False
        self.scan_tag_year = False
        self.match_scaled = False
        self.match_rotated = False

    def _save_values(self, settings):
        set_ = self.set_value
        set_("FilterHardness", self.filter_hardness)
        set_("MixFileKind", self.mix_file_kind)
        set_("IgnoreHardlinkMatches", self.ignore_hardlink_matches)
        set_("CrossPoolOnly", self.cross_pool_only)
        set_("UseRegexp", self.use_regexp)
        set_("RemoveEmptyFolders", self.remove_empty_folders)
        set_("RehashIgnoreMTime", self.rehash_ignore_mtime)
        set_("IncludeExistsCheck", self.include_exists_check)
        self.direct_scan_max_files = _bounded_positive_int(
            self.direct_scan_max_files,
            DIRECT_DISCOVERY_MAX_FILES,
            DIRECT_DISCOVERY_MAX_FILES,
        )
        self.direct_scan_max_folders = _bounded_positive_int(
            self.direct_scan_max_folders,
            DIRECT_DISCOVERY_MAX_FOLDERS,
            DIRECT_DISCOVERY_MAX_FOLDERS,
        )
        self.direct_scan_max_issues = _bounded_positive_int(
            self.direct_scan_max_issues,
            DIRECT_DISCOVERY_MAX_ISSUES,
            DIRECT_DISCOVERY_MAX_ISSUES,
        )
        self.direct_scan_max_seconds = _bounded_positive_int(
            self.direct_scan_max_seconds,
            DIRECT_DISCOVERY_MAX_SECONDS,
            DIRECT_DISCOVERY_MAX_SECONDS,
        )
        set_("DirectScanMaxFiles", self.direct_scan_max_files)
        set_("DirectScanMaxFolders", self.direct_scan_max_folders)
        set_("DirectScanMaxIssues", self.direct_scan_max_issues)
        set_("DirectScanMaxSeconds", self.direct_scan_max_seconds)
        set_("DebugMode", self.debug_mode)
        set_("ProfileScan", self.profile_scan)
        set_("DestinationType", self.destination_type)
        set_("CustomCommand", self.custom_command)
        set_("Language", self.language)
        set_("Portable", self.portable)
        set_("UseDarkStyle", self.use_dark_style)
        set_("UseNativeDialogs", self.use_native_dialogs)

        set_("TableFontSize", self.tableFontSize)
        set_("ReferenceBoldFont", self.reference_bold_font)
        set_("DetailsDialogTitleBarEnabled", self.details_dialog_titlebar_enabled)
        set_("DetailsDialogVerticalTitleBar", self.details_dialog_vertical_titlebar)
        set_("DetailsDialogOverrideThemeIcons", self.details_dialog_override_theme_icons)
        set_("DetailsDialogViewersShowScrollbars", self.details_dialog_viewers_show_scrollbars)
        set_("DetailsTableDeltaForegroundColor", self.details_table_delta_foreground_color)
        set_("ResultTableRefForegroundColor", self.result_table_ref_foreground_color)
        set_("ResultTableRefBackgroundColor", self.result_table_ref_background_color)
        set_("ResultTableDeltaForegroundColor", self.result_table_delta_foreground_color)
        set_("ResultWindowIsMaximized", self.resultWindowIsMaximized)
        set_("MainWindowIsMaximized", self.mainWindowIsMaximized)
        self.set_rect("ResultWindowRect", self.resultWindowRect)
        self.set_rect("MainWindowRect", self.mainWindowRect)
        self.set_rect("DirectoriesWindowRect", self.directoriesWindowRect)
        set_("RecentResults", self.recentResults)
        set_("RecentFolders", self.recentFolders)

        set_("TabsDefaultPosition", self.tabs_default_pos)
        set_("WordWeighting", self.word_weighting)
        set_("MatchSimilar", self.match_similar)
        set_("IgnoreSmallFiles", self.ignore_small_files)
        set_("SmallFileThreshold", self.small_file_threshold)
        set_("IgnoreLargeFiles", self.ignore_large_files)
        set_("LargeFileThreshold", self.large_file_threshold)
        set_("BigFilePartialHashes", self.big_file_partial_hashes)
        set_("BigFileSizeThreshold", self.big_file_size_threshold)
        set_("ScanTagTrack", self.scan_tag_track)
        set_("ScanTagArtist", self.scan_tag_artist)
        set_("ScanTagAlbum", self.scan_tag_album)
        set_("ScanTagTitle", self.scan_tag_title)
        set_("ScanTagGenre", self.scan_tag_genre)
        set_("ScanTagYear", self.scan_tag_year)
        set_("MatchScaled", self.match_scaled)
        set_("MatchRotated", self.match_rotated)

    # scan_type is special because we save it immediately when we set it.
    def get_scan_type(self, app_mode):
        if app_mode == AppMode.PICTURE:
            return self.get_value("ScanTypePicture", ScanType.FUZZYBLOCK)
        elif app_mode == AppMode.MUSIC:
            return self.get_value("ScanTypeMusic", ScanType.TAG)
        else:
            return self.get_value("ScanTypeStandard", ScanType.CONTENTS)

    def set_scan_type(self, app_mode, value):
        if app_mode == AppMode.PICTURE:
            self.set_value("ScanTypePicture", value)
        elif app_mode == AppMode.MUSIC:
            self.set_value("ScanTypeMusic", value)
        else:
            self.set_value("ScanTypeStandard", value)
