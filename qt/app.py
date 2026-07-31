# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import sys
import os.path as op
import sqlite3
from pathlib import Path

from PyQt6.QtCore import QTimer, QObject, QUrl, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtGui import QColor, QDesktopServices, QPalette
from PyQt6.QtWidgets import QApplication, QFileDialog, QDialog, QMessageBox, QStyleFactory, QToolTip

from hscommon.trans import trget
from hscommon import desktop, plat, trans
from hscommon.util import format_size

from qt.about_box import AboutBox
from qt.recent import Recent
from qt.util import create_actions
from qt.progress_window import ProgressWindow

from core import __appname__, __project_url__
from core.app import AppMode, DupeGuru as DupeGuruModel
from core.catalog import CatalogError
from core.directories import DirectoryState
from core.visual_service import VisualScanConfig
import core.pe.photo
from qt import platform
from qt.preferences import Preferences
from qt.result_window import ResultWindow
from qt.directories_dialog import DirectoriesDialog
from qt.problem_dialog import ProblemDialog
from qt.ignore_list_dialog import IgnoreListDialog
from qt.exclude_list_dialog import ExcludeListDialog
from qt.deletion_options import DeletionOptions
from qt.se.details_dialog import DetailsDialog as DetailsDialogStandard
from qt.me.details_dialog import DetailsDialog as DetailsDialogMusic
from qt.pe.details_dialog import DetailsDialog as DetailsDialogPicture
from qt.pe.thumbnail_cache import ThumbnailCacheSafetyError, clear_default_thumbnail_cache
from qt.pe.visual_query import (
    VisualQueryController,
    VisualQueryDialog,
    VisualQuerySourcePolicy,
)
from qt.se.preferences_dialog import PreferencesDialog as PreferencesDialogStandard
from qt.me.preferences_dialog import PreferencesDialog as PreferencesDialogMusic
from qt.pe.preferences_dialog import PreferencesDialog as PreferencesDialogPicture
from qt.pe.photo import File as PlatSpecificPhoto
from qt.tabbed_window import TabBarWindow, TabWindow

tr = trget("ui")


class DupeGuru(QObject):
    LOGO_NAME = "logo_se"
    NAME = __appname__

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prefs = Preferences()
        self.prefs.load()
        # Enable tabs instead of separate floating windows for each dialog
        # Could be passed as an argument to this class if we wanted
        self.use_tabs = True
        self.model = DupeGuruModel(view=self, portable=self.prefs.portable)
        self._setup()

    # --- Private
    def _setup(self):
        core.pe.photo.PLAT_SPECIFIC_PHOTO_CLASS = PlatSpecificPhoto
        self._setupActions()
        self.details_dialog = None
        self._update_options()
        self.recentResults = Recent(self, "recentResults")
        self.recentResults.mustOpenItem.connect(self.model.load_from)
        self.resultWindow = None
        if self.use_tabs:
            self.main_window = TabBarWindow(self) if not self.prefs.tabs_default_pos else TabWindow(self)
            parent_window = self.main_window
            self.directories_dialog = self.main_window.createPage("DirectoriesDialog", app=self)
            self.main_window.addTab(self.directories_dialog, tr("Directories"), switch=False)
            self.actionDirectoriesWindow.setEnabled(False)
        else:  # floating windows only
            self.main_window = None
            self.directories_dialog = DirectoriesDialog(self)
            parent_window = self.directories_dialog

        self.progress_window = ProgressWindow(parent_window, self.model.progress_window)
        self.visualQueryController = VisualQueryController(self)
        self.visualQueryDialog = VisualQueryDialog(parent_window)
        self.visualQueryController.reportReady.connect(self.visualQueryDialog.show_report)
        self.visualQueryController.failed.connect(self.visualQueryDialog.show_error)
        self.visualQueryController.cancelled.connect(self.visualQueryDialog.show_cancelled)
        self.visualQueryController.cancelPending.connect(self.visualQueryDialog.show_cancel_pending)
        self.visualQueryController.runningChanged.connect(self._visualQueryRunningChanged)
        self.visualQueryDialog.cancelRequested.connect(self.visualQueryController.cancel)
        self.visualQueryDialog.referenceDropped.connect(self.startVisualQuery)
        self.updatePictureQueryAction()
        self.problemDialog = ProblemDialog(parent=parent_window, model=self.model.problem_dialog)
        if self.use_tabs:
            self.ignoreListDialog = self.main_window.createPage(
                "IgnoreListDialog",
                parent=self.main_window,
                model=self.model.ignore_list_dialog,
            )

            self.excludeListDialog = self.main_window.createPage(
                "ExcludeListDialog",
                app=self,
                parent=self.main_window,
                model=self.model.exclude_list_dialog,
            )
        else:
            self.ignoreListDialog = IgnoreListDialog(parent=parent_window, model=self.model.ignore_list_dialog)
            self.excludeDialog = ExcludeListDialog(app=self, parent=parent_window, model=self.model.exclude_list_dialog)

        self.deletionOptions = DeletionOptions(parent=parent_window, model=self.model.deletion_options)
        self.about_box = AboutBox(parent_window, self)

        parent_window.show()
        self.model.load()

        self.SIGTERM.connect(self.handleSIGTERM)

        # The timer scheme is because if the nag is not shown before the application is
        # completely initialized, the nag will be shown before the app shows up in the task bar
        # In some circumstances, the nag is hidden by other window, which may make the user think
        # that the application haven't launched.
        QTimer.singleShot(0, self.finishedLaunching)

    def _setupActions(self):
        # Setup actions that are common to both the directory dialog and the results window.
        # (name, shortcut, icon, desc, func)
        ACTIONS = [
            ("actionQuit", "Ctrl+Q", "", tr("Quit"), self.quitTriggered),
            (
                "actionPreferences",
                "Ctrl+P",
                "",
                tr("Options"),
                self.preferencesTriggered,
            ),
            ("actionIgnoreList", "", "", tr("Ignore List"), self.ignoreListTriggered),
            (
                "actionDirectoriesWindow",
                "",
                "",
                tr("Directories"),
                self.showDirectoriesWindow,
            ),
            (
                "actionClearCache",
                "Ctrl+Shift+P",
                "",
                tr("Clear Cache"),
                self.clearCacheTriggered,
            ),
            (
                "actionExcludeList",
                "",
                "",
                tr("Exclusion Filters"),
                self.excludeListTriggered,
            ),
            ("actionShowHelp", "F1", "", tr("dupeGuru Neo Help"), self.showHelpTriggered),
            (
                "actionShowVideoWorkflow",
                "",
                "",
                tr("Similar Video CLI Workflow…"),
                self.showVideoWorkflowTriggered,
            ),
            ("actionAbout", "", "", tr("About dupeGuru Neo"), self.showAboutBoxTriggered),
            (
                "actionOpenDebugLog",
                "",
                "",
                tr("Open Debug Log"),
                self.openDebugLogTriggered,
            ),
            (
                "actionFindSimilarImage",
                "Ctrl+Shift+F",
                "",
                tr("Find Similar Image…"),
                self.findSimilarImageTriggered,
            ),
        ]
        create_actions(ACTIONS, self)

    def _update_options(self):
        self.model.options["mix_file_kind"] = self.prefs.mix_file_kind
        self.model.options["escape_filter_regexp"] = not self.prefs.use_regexp
        self.model.options["clean_empty_dirs"] = self.prefs.remove_empty_folders
        self.model.options["ignore_hardlink_matches"] = self.prefs.ignore_hardlink_matches
        self.model.options["comparison_scope"] = "cross_pool" if self.prefs.cross_pool_only else "all"
        self.model.options["copymove_dest_type"] = self.prefs.destination_type
        self.model.options["scan_type"] = self.prefs.get_scan_type(self.model.app_mode)
        self.model.options["min_match_percentage"] = self.prefs.filter_hardness
        self.model.options["word_weighting"] = self.prefs.word_weighting
        self.model.options["match_similar_words"] = self.prefs.match_similar
        threshold = self.prefs.small_file_threshold if self.prefs.ignore_small_files else 0
        self.model.options["size_threshold"] = threshold * 1024  # threshold is in KB. The scanner wants bytes
        large_threshold = self.prefs.large_file_threshold if self.prefs.ignore_large_files else 0
        self.model.options["large_size_threshold"] = (
            large_threshold * 1024 * 1024
        )  # threshold is in MB. The Scanner wants bytes
        big_file_size_threshold = self.prefs.big_file_size_threshold if self.prefs.big_file_partial_hashes else 0
        self.model.options["big_file_size_threshold"] = (
            big_file_size_threshold
            * 1024
            * 1024
            # threshold is in MiB. The scanner wants bytes
        )
        scanned_tags = set()
        if self.prefs.scan_tag_track:
            scanned_tags.add("track")
        if self.prefs.scan_tag_artist:
            scanned_tags.add("artist")
        if self.prefs.scan_tag_album:
            scanned_tags.add("album")
        if self.prefs.scan_tag_title:
            scanned_tags.add("title")
        if self.prefs.scan_tag_genre:
            scanned_tags.add("genre")
        if self.prefs.scan_tag_year:
            scanned_tags.add("year")
        self.model.options["scanned_tags"] = scanned_tags
        self.model.options["match_scaled"] = self.prefs.match_scaled
        self.model.options["match_rotated"] = self.prefs.match_rotated
        self.model.options["include_exists_check"] = self.prefs.include_exists_check
        self.model.options["rehash_ignore_mtime"] = self.prefs.rehash_ignore_mtime
        self.model.options["direct_scan_max_files"] = self.prefs.direct_scan_max_files
        self.model.options["direct_scan_max_folders"] = self.prefs.direct_scan_max_folders
        self.model.options["direct_scan_max_issues"] = self.prefs.direct_scan_max_issues
        self.model.options["direct_scan_max_seconds"] = self.prefs.direct_scan_max_seconds

        if self.details_dialog:
            self.details_dialog.update_options()

        self._set_style("dark" if self.prefs.use_dark_style else "light")

    # --- Private
    def _get_details_dialog_class(self):
        if self.model.app_mode == AppMode.PICTURE:
            return DetailsDialogPicture
        elif self.model.app_mode == AppMode.MUSIC:
            return DetailsDialogMusic
        else:
            return DetailsDialogStandard

    def _get_preferences_dialog_class(self):
        if self.model.app_mode == AppMode.PICTURE:
            return PreferencesDialogPicture
        elif self.model.app_mode == AppMode.MUSIC:
            return PreferencesDialogMusic
        else:
            return PreferencesDialogStandard

    def _set_style(self, style="light"):
        # Only support this feature on windows for now
        if not plat.ISWINDOWS:
            return
        if style == "dark":
            QApplication.setStyle(QStyleFactory.create("Fusion"))
            palette = QApplication.style().standardPalette()
            palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
            palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(164, 166, 168))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(164, 166, 168))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(164, 166, 168))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, QColor(164, 166, 168))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(68, 68, 68))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window, QColor(68, 68, 68))
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor(68, 68, 68))
        else:
            QApplication.setStyle(QStyleFactory.create("windowsvista" if plat.ISWINDOWS else "Fusion"))
            palette = QApplication.style().standardPalette()
        QToolTip.setPalette(palette)
        QApplication.setPalette(palette)

    # --- Public
    def add_selected_to_ignore_list(self):
        self.model.add_selected_to_ignore_list()

    def remove_selected(self):
        self.model.remove_selected(self)

    def confirm(self, title, msg, default_button=QMessageBox.StandardButton.Yes):
        active = QApplication.activeWindow()
        buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        answer = QMessageBox.question(active, title, msg, buttons, default_button)
        return answer == QMessageBox.StandardButton.Yes

    def invokeCustomCommand(self):
        self.model.invoke_custom_command()

    def show_details(self):
        if self.details_dialog is not None:
            if not self.details_dialog.isVisible():
                self.details_dialog.show()
            else:
                self.details_dialog.hide()

    def showResultsWindow(self):
        if self.resultWindow is not None:
            if self.use_tabs:
                if self.main_window.indexOfWidget(self.resultWindow) < 0:
                    self.main_window.addTab(self.resultWindow, tr("Results"), switch=True)
                    return
                self.main_window.showTab(self.resultWindow)
            else:
                self.resultWindow.show()

    def showDirectoriesWindow(self):
        if self.directories_dialog is not None:
            if self.use_tabs:
                self.main_window.showTab(self.directories_dialog)
            else:
                self.directories_dialog.show()

    def shutdown(self):
        if self.visualQueryController.running:
            self.visualQueryController.cancel()
        self.willSavePrefs.emit()
        self.prefs.save()
        self.model.save()
        self.model.close()
        # Workaround for #857, hide() or close().
        if self.details_dialog is not None:
            self.details_dialog.close()
        QApplication.quit()

    # --- Signals
    willSavePrefs = pyqtSignal()
    SIGTERM = pyqtSignal()

    # --- Events
    def finishedLaunching(self):
        if sys.getfilesystemencoding() == "ascii":
            # No need to localize this, it's a debugging message.
            msg = (
                "Something is wrong with the way your system locale is set. If the files you're "
                "scanning have accented letters, you'll probably get a crash. It is advised that "
                "you set your system locale properly."
            )
            QMessageBox.warning(
                self.main_window if self.main_window else self.directories_dialog,
                "Wrong Locale",
                msg,
            )
        # Load results on open if passed a .dupeguru file
        if len(sys.argv) > 1:
            results = sys.argv[1]
            if results.endswith(".dupeguru"):
                self.model.load_from(results)
                self.recentResults.insertItem(results)

    def clearCacheTriggered(self):
        title = tr("Clear Cache")
        active = QApplication.activeWindow()
        try:
            catalog_bytes = self.model.catalog_storage_size()
        except (CatalogError, OSError, sqlite3.Error, ValueError) as error:
            QMessageBox.critical(
                active,
                title,
                tr("Cache information could not be read. Nothing was cleared.\n\n{}").format(error),
            )
            return
        catalog_size = format_size(catalog_bytes, decimal=1) if catalog_bytes else tr("not created")
        msg = tr(
            "Clear all rebuildable scan data?\n\n"
            "This removes cached file hashes, picture analysis, on-demand picture thumbnails, "
            "and the Persistent Catalog (including scan history and unfinished scans). Your "
            "files, settings, exclusions, and saved result files are not removed.\n\n"
            "Persistent Catalog: {}"
        ).format(catalog_size)
        if self.confirm(title, msg, QMessageBox.StandardButton.No):
            try:
                self.model.clear_catalog()
                self.model.clear_picture_cache()
                self.model.clear_hash_cache()
                clear_default_thumbnail_cache()
            except (CatalogError, OSError, sqlite3.Error, ThumbnailCacheSafetyError, ValueError) as error:
                QMessageBox.critical(
                    active,
                    title,
                    tr("Cache cleanup stopped. Some rebuildable data may already have been " "cleared.\n\n{}").format(
                        error
                    ),
                )
                return
            QMessageBox.information(active, title, tr("Rebuildable scan data cleared."))

    def updatePictureQueryAction(self):
        is_picture = self.model.app_mode == AppMode.PICTURE
        running = bool(getattr(self, "visualQueryController", None) and self.visualQueryController.running)
        self.actionFindSimilarImage.setVisible(is_picture)
        self.actionFindSimilarImage.setEnabled(is_picture and not running)

    @pyqtSlot(bool)
    def _visualQueryRunningChanged(self, running):
        self.updatePictureQueryAction()

    def findSimilarImageTriggered(self):
        if self.model.app_mode != AppMode.PICTURE:
            return
        extensions = " ".join("*.{}".format(extension) for extension in sorted(core.pe.photo.Photo.HANDLED_EXTS))
        reference, _ = QFileDialog.getOpenFileName(
            self.main_window or self.directories_dialog,
            tr("Choose an image to find visually similar files"),
            "",
            tr("Images ({})").format(extensions),
        )
        if reference:
            self.startVisualQuery(reference)

    @pyqtSlot(str)
    def startVisualQuery(self, reference):
        if self.model.app_mode != AppMode.PICTURE:
            return False
        reference_path = Path(reference)
        if (
            not reference_path.is_file()
            or reference_path.suffix.lower().lstrip(".") not in core.pe.photo.Photo.HANDLED_EXTS
        ):
            self.show_message(tr("Choose a readable image file."))
            return False
        directories = self.model.directories
        roots = [path for path in directories if directories.get_state(path) != DirectoryState.EXCLUDED]
        if not roots:
            self.show_message(tr("Add at least one non-excluded picture folder before searching."))
            return False
        cache_path = self.model._get_picture_cache_path()
        config = VisualScanConfig(
            similarity_threshold=int(self.prefs.filter_hardness),
            match_scaled=bool(self.prefs.match_scaled),
            match_rotated=bool(self.prefs.match_rotated),
            include_related=True,
        )
        source_policy = VisualQuerySourcePolicy.from_directories(
            directories,
            self.model.exclude_list,
        )
        if not self.visualQueryController.start(
            str(reference_path),
            roots,
            cache_path,
            config,
            source_policy,
        ):
            self.show_message(tr("A visual search is already running."))
            return False
        self.visualQueryDialog.start_query(str(reference_path))
        return True

    def ignoreListTriggered(self):
        if self.use_tabs:
            self.showTriggeredTabbedDialog(self.ignoreListDialog, tr("Ignore List"))
        else:  # floating windows
            self.model.ignore_list_dialog.show()

    def excludeListTriggered(self):
        if self.use_tabs:
            self.showTriggeredTabbedDialog(self.excludeListDialog, tr("Exclusion Filters"))
        else:  # floating windows
            self.model.exclude_list_dialog.show()

    def showTriggeredTabbedDialog(self, dialog, desc_string):
        """Add tab for dialog, name the tab with desc_string, then show it."""
        index = self.main_window.indexOfWidget(dialog)
        # Create the tab if it doesn't exist already
        if index < 0:  # or (not dialog.isVisible() and not self.main_window.isTabVisible(index)):
            index = self.main_window.addTab(dialog, desc_string, switch=True)
        # Show the tab for that widget
        self.main_window.setCurrentIndex(index)

    def openDebugLogTriggered(self):
        debug_log_path = op.join(self.model.appdata, "debug.log")
        desktop.open_path(debug_log_path)

    def preferencesTriggered(self):
        preferences_dialog = self._get_preferences_dialog_class()(
            self.main_window if self.main_window else self.directories_dialog, self
        )
        preferences_dialog.load()
        result = preferences_dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            preferences_dialog.save()
            self.prefs.save()
            self._update_options()
        preferences_dialog.setParent(None)

    def quitTriggered(self):
        if self.details_dialog is not None:
            self.details_dialog.close()

        if self.main_window:
            self.main_window.close()
        else:
            self.directories_dialog.close()

    def showAboutBoxTriggered(self):
        self.about_box.show()

    def showHelpTriggered(self):
        self._openHelpPage("index.html", "README.md")

    def showVideoWorkflowTriggered(self):
        self._openHelpPage("video.html", "help/en/video.rst", "help/ja/video.rst")

    @staticmethod
    def _openHelpPage(local_name, repository_name, japanese_repository_name=None):
        language = trans.installed_lang
        base_path = platform.localized_help_path(language)
        help_path = op.abspath(op.join(base_path, local_name))
        if op.exists(help_path):
            url = QUrl.fromLocalFile(help_path)
        else:
            if language == "ja" and japanese_repository_name is not None:
                repository_name = japanese_repository_name
            url = QUrl(
                "{}/blob/master/{}".format(
                    __project_url__,
                    repository_name,
                )
            )
        QDesktopServices.openUrl(url)

    def handleSIGTERM(self):
        self.shutdown()

    # --- model --> view
    def get_default(self, key):
        return self.prefs.get_value(key)

    def set_default(self, key, value):
        self.prefs.set_value(key, value)

    def show_message(self, msg):
        window = QApplication.activeWindow()
        QMessageBox.information(window, "", msg)

    def ask_yes_no(self, prompt):
        return self.confirm("", prompt)

    def create_results_window(self):
        """Creates resultWindow and details_dialog depending on the selected ``app_mode``."""
        if self.details_dialog is not None:
            # The object is not deleted entirely, avoid saving its geometry in the future
            # self.willSavePrefs.disconnect(self.details_dialog.appWillSavePrefs)
            # or simply delete it on close which is probably cleaner:
            self.details_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.details_dialog.close()
            # if we don't do the following, Qt will crash when we recreate the Results dialog
            self.details_dialog.setParent(None)
        if self.resultWindow is not None:
            self.resultWindow.close()
            # This is better for tabs, as it takes care of duplicate items in menu bar
            self.resultWindow.deleteLater() if self.use_tabs else self.resultWindow.setParent(None)
        if self.use_tabs:
            self.resultWindow = self.main_window.createPage("ResultWindow", parent=self.main_window, app=self)
        else:  # We don't use a tab widget, regular floating QMainWindow
            self.resultWindow = ResultWindow(self.directories_dialog, self)
            self.directories_dialog._updateActionsState()
        self.details_dialog = self._get_details_dialog_class()(self.resultWindow, self)

    def show_results_window(self):
        self.showResultsWindow()

    def show_problem_dialog(self):
        self.problemDialog.show()

    def select_dest_folder(self, prompt):
        flags = QFileDialog.Option.ShowDirsOnly
        return QFileDialog.getExistingDirectory(self.resultWindow, prompt, "", flags)

    def select_dest_file(self, prompt, extension):
        files = tr("{} file (*.{})").format(extension.upper(), extension)
        destination, chosen_filter = QFileDialog.getSaveFileName(self.resultWindow, prompt, "", files)
        if not destination.endswith(f".{extension}"):
            destination = f"{destination}.{extension}"
        return destination
