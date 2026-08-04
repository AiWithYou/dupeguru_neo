# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import logging

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QWidget,
)
from PyQt6.QtGui import QResizeEvent
from core.destructive_eligibility import evaluate_duplicate
from hscommon.trans import trget
from qt.details_dialog import DetailsDialog as DetailsDialogBase
from qt.details_table import DetailsTable
from qt.pe.image_viewer import ViewerToolBar, ScrollAreaImageViewer, ScrollAreaController
from qt.pe.review_gallery import ReviewGalleryWidget

tr = trget("ui")


class DetailsDialog(DetailsDialogBase):
    keeperRequested = pyqtSignal(object)
    deleteCandidateRequested = pyqtSignal(object, bool)
    nextGroupRequested = pyqtSignal()

    def __init__(self, parent, app):
        self.vController = None
        self._review_group = None
        self._syncing_gallery = False
        self._accept_in_progress = False
        super().__init__(parent, app)

    def _setupUi(self):
        self.setWindowTitle(tr("Details"))
        self.resize(502, 502)
        self.setMinimumSize(QSize(250, 250))
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.topFrame = EmittingFrame()
        self.topFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.horizontalLayout = QGridLayout()
        # Minimum width for the toolbar in the middle:
        self.horizontalLayout.setColumnMinimumWidth(1, 10)
        self.horizontalLayout.setContentsMargins(0, 0, 0, 0)
        self.horizontalLayout.setColumnStretch(0, 32)
        # Smaller value for the toolbar in the middle to avoid excessive resize
        self.horizontalLayout.setColumnStretch(1, 2)
        self.horizontalLayout.setColumnStretch(2, 32)
        # This avoids toolbar getting incorrectly partially hidden when window resizes
        self.horizontalLayout.setRowStretch(0, 1)
        self.horizontalLayout.setRowStretch(1, 24)
        self.horizontalLayout.setRowStretch(2, 1)
        self.horizontalLayout.setRowStretch(3, 0)
        self.horizontalLayout.setSpacing(1)  # probably not important

        self.selectedImageViewer = ScrollAreaImageViewer(self, "selectedImage")
        self.horizontalLayout.addWidget(self.selectedImageViewer, 0, 0, 3, 1)
        # Use a specific type of controller depending on the underlying viewer type
        self.vController = ScrollAreaController(self)

        self.verticalToolBar = ViewerToolBar(self, self.vController)
        self.verticalToolBar.setOrientation(Qt.Orientation.Vertical)
        self.horizontalLayout.addWidget(
            self.verticalToolBar,
            1,
            1,
            1,
            1,
            Qt.AlignmentFlag.AlignCenter,
        )

        self.referenceImageViewer = ScrollAreaImageViewer(self, "referenceImage")
        self.horizontalLayout.addWidget(self.referenceImageViewer, 0, 2, 3, 1)
        self.comparisonStatusLabel = QLabel(tr("Side-by-side comparison"), self)
        self.comparisonStatusLabel.setObjectName("comparisonStatusLabel")
        self.comparisonStatusLabel.setWordWrap(True)
        self.comparisonStatusLabel.setMargin(5)
        self.comparisonFooter = QWidget(self)
        comparison_footer_layout = QHBoxLayout(self.comparisonFooter)
        comparison_footer_layout.setContentsMargins(0, 0, 0, 0)
        comparison_footer_layout.setSpacing(2)
        self.comparisonModeButtons = []
        for action in (
            self.verticalToolBar.actionSideBySide,
            self.verticalToolBar.actionAlphaOverlay,
            self.verticalToolBar.actionBlink,
            self.verticalToolBar.actionDifference,
        ):
            button = QToolButton(self.comparisonFooter)
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setAutoRaise(True)
            button.setMinimumWidth(28)
            comparison_footer_layout.addWidget(button)
            self.comparisonModeButtons.append(button)
        comparison_footer_layout.addWidget(self.comparisonStatusLabel, 1)
        self.horizontalLayout.addWidget(self.comparisonFooter, 3, 0, 1, 3)
        self.vController.comparisonStatusChanged.connect(self._comparison_status_changed)
        self._comparison_status_changed(tr("Side-by-side comparison"), False)
        self.topFrame.setLayout(self.horizontalLayout)
        self.splitter.addWidget(self.topFrame)
        self.splitter.setStretchFactor(0, 7)

        self.reviewGallery = ReviewGalleryWidget(self)
        self.reviewGallery.setMinimumHeight(210)
        self.reviewGallery.previewRequested.connect(self._preview_image)
        self.reviewGallery.keeperRequested.connect(self._make_keeper)
        self.reviewGallery.deleteCandidateRequested.connect(self._set_delete_candidate)
        self.reviewGallery.acceptKeeperRequested.connect(self._accept_keeper)
        self.reviewGallery.nextGroupRequested.connect(self._select_next_group)
        self.splitter.addWidget(self.reviewGallery)
        self.splitter.setStretchFactor(1, 5)

        self.tableView = DetailsTable(self)
        size_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        size_policy.setHorizontalStretch(0)
        size_policy.setVerticalStretch(0)
        self.tableView.setSizePolicy(size_policy)
        self.tableView.setAlternatingRowColors(True)
        self.tableView.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tableView.setShowGrid(False)
        self.splitter.addWidget(self.tableView)
        self.splitter.setStretchFactor(2, 1)
        # Late population needed here for connections to the toolbar
        self.vController.setupViewers(self.selectedImageViewer, self.referenceImageViewer)
        # self.setCentralWidget(self.splitter)  # only as QMainWindow
        self.setWidget(self.splitter)  # only as QDockWidget

        self.topFrame.resized.connect(self.resizeEvent)

    def _update(self):
        if self.vController is None:  # Not yet constructed!
            return
        if not self.app.model.selected_dupes:
            # No item from the model, disable and clear everything.
            self._review_group = None
            self.reviewGallery.clear()
            self.vController.resetViewersState()
            return
        dupe = self.app.model.selected_dupes[0]
        group = self.app.model.results.get_group_of_duplicate(dupe)
        if group is None:
            self._review_group = None
            self.reviewGallery.clear()
            self.vController.resetViewersState()
            return
        ref = group.ref

        self._review_group = group
        self._syncing_gallery = True
        try:
            if self.reviewGallery.model.group is group and self.reviewGallery.model.has_current_layout(group):
                self.reviewGallery.model.update_item(self.app.model.results, dupe)
                self.reviewGallery.view.select_item(dupe)
            else:
                self.reviewGallery.set_group(group, self.app.model.results, dupe)
        finally:
            self._syncing_gallery = False
        self.vController.updateView(ref, dupe, group)

    @pyqtSlot(object)
    def _preview_image(self, item):
        group = self._review_group
        if self._syncing_gallery or group is None or item not in group:
            return
        self.vController.updateView(group.ref, item, group)

    @pyqtSlot(str, bool)
    def _comparison_status_changed(self, message, is_error):
        self.comparisonStatusLabel.setText(message)
        self.comparisonStatusLabel.setProperty("comparisonError", is_error)
        if is_error:
            colors = "background-color: #6B2028; color: #FFFFFF;"
        else:
            colors = "background-color: #263544; color: #FFFFFF;"
        self.comparisonStatusLabel.setStyleSheet("QLabel {" f"{colors} border-radius: 3px; padding: 3px 6px;" "}")

    def _select_result_item(self, item) -> bool:
        result_table = getattr(self.app.model, "result_table", None)
        if result_table is None:
            return False
        row_count = len(result_table)
        current_group = self._review_group
        if not getattr(result_table, "power_marker", False):
            group_start = self._normal_result_group_start(result_table, current_group)
            item_index = self.reviewGallery.model.index_for_item(item)
            if group_start is not None and item_index.isValid():
                row_index = group_start + item_index.row()
                if row_index < row_count:
                    row = result_table[row_index]
                    if getattr(row, "_group", None) is current_group and getattr(row, "_dupe", None) is item:
                        result_table.select([row_index])
                        return True

        # Power-marker rows can interleave groups. This exceptional mode keeps
        # the historical full search, without copying the table first.
        if getattr(result_table, "power_marker", False):
            for row_index in range(row_count):
                if getattr(result_table[row_index], "_dupe", None) is item:
                    result_table.select([row_index])
                    return True
        return False

    def _normal_result_group_start(self, result_table, current_group):
        """Locate one contiguous result block with a constant number of reads."""

        row_count = len(result_table)
        selected_indexes = getattr(result_table, "selected_indexes", ())
        try:
            anchor = next(iter(selected_indexes))
        except (StopIteration, TypeError):
            return None
        if not isinstance(anchor, int) or not 0 <= anchor < row_count:
            return None
        anchor_row = result_table[anchor]
        if getattr(anchor_row, "_group", None) is not current_group:
            return None
        anchor_item = getattr(anchor_row, "_dupe", None)
        item_index = self.reviewGallery.model.index_for_item(anchor_item)
        if not item_index.isValid():
            return None
        group_start = anchor - item_index.row()
        if group_start < 0:
            return None
        first_row = result_table[group_start]
        if getattr(first_row, "_group", None) is not current_group:
            return None
        return group_start

    @pyqtSlot(object)
    def _make_keeper(self, item):
        model = self.app.model
        make_reference = getattr(model, "make_selected_reference", None)
        if not callable(make_reference) or not self._select_result_item(item):
            self.reviewGallery.model.report_blocked(tr("The selected image is not available in the current results."))
            return
        try:
            make_reference()
        except Exception:
            self.reviewGallery.model.report_blocked(tr("The keeper could not be updated in the current results."))
            return
        self._update()
        self.keeperRequested.emit(item)

    @pyqtSlot(object, bool)
    def _set_delete_candidate(self, item, marked):
        model = self.app.model
        results = getattr(model, "results", None)
        mark_dupe = getattr(model, "mark_dupe", None)
        if results is None or not callable(mark_dupe):
            self.reviewGallery.model.set_delete_candidate(item, False)
            self.reviewGallery.model.report_blocked(tr("The current results cannot be marked."))
            return

        if marked:
            try:
                eligibility = evaluate_duplicate(results, item)
            except Exception:
                eligibility = None
            if eligibility is None or not eligibility.allowed:
                message = (
                    eligibility.message
                    if eligibility is not None
                    else tr("The deletion proof could not be revalidated.")
                )
                self.reviewGallery.model.set_delete_candidate(item, False)
                self.reviewGallery.model.refresh_safety()
                self.reviewGallery.model.report_blocked(message)
                return

        try:
            mark_dupe(item, bool(marked))
        except Exception:
            is_marked = getattr(results, "is_marked", None)
            actual_marked = False
            if callable(is_marked):
                try:
                    actual_marked = bool(is_marked(item))
                except Exception:
                    actual_marked = False
            self.reviewGallery.model.set_delete_candidate(item, actual_marked)
            self.reviewGallery.model.report_blocked(tr("The deletion candidate could not be updated."))
            return
        is_marked = getattr(results, "is_marked", None)
        actual_marked = bool(marked)
        if callable(is_marked):
            try:
                actual_marked = bool(is_marked(item))
            except Exception:
                actual_marked = False
        self.reviewGallery.model.set_delete_candidate(item, actual_marked)
        self.reviewGallery.model.refresh_safety()
        self.deleteCandidateRequested.emit(item, actual_marked)

    @pyqtSlot(object, object)
    def _accept_keeper(self, keeper, candidates):
        if self._accept_in_progress:
            return
        group = self._review_group
        model = self.app.model
        results = getattr(model, "results", None)
        if group is None or results is None or keeper is not getattr(group, "ref", None):
            self.reviewGallery.model.report_blocked(tr("The current keeper changed before this review was accepted."))
            return
        current_candidates = tuple(item for item in getattr(group, "ordered", ()) if item is not keeper)
        candidates = tuple(candidates)
        if candidates != current_candidates:
            self.reviewGallery.model.report_blocked(tr("The duplicate group changed before this review was accepted."))
            return
        for candidate in candidates:
            try:
                eligibility = evaluate_duplicate(results, candidate)
            except Exception:
                eligibility = None
            if eligibility is None or not eligibility.allowed:
                message = (
                    eligibility.message
                    if eligibility is not None
                    else tr("The deletion proof could not be revalidated.")
                )
                self.reviewGallery.model.report_blocked(message)
                return
        mark_dupes = getattr(model, "mark_dupes", None)
        if not callable(mark_dupes):
            self.reviewGallery.model.report_blocked(tr("The current results cannot be marked as one review batch."))
            return
        self._accept_in_progress = True
        self.reviewGallery.set_accept_in_progress(True)
        mark_call_failed = False
        try:
            mark_dupes(candidates, True)
        except Exception:
            mark_call_failed = True
            logging.exception(
                "The review batch mark call raised; checking its committed state before reporting failure"
            )
        try:
            batch_is_marked = all(results.is_marked(candidate) for candidate in candidates)
        except Exception:
            logging.exception("The committed review batch state could not be read")
            batch_is_marked = False
        if not batch_is_marked:
            self._accept_in_progress = False
            self.reviewGallery.set_accept_in_progress(False)
            self.reviewGallery.model.update_results(results)
            message = (
                tr("The review batch could not be checked.")
                if mark_call_failed
                else tr("The review batch was not fully checked.")
            )
            self.reviewGallery.model.report_blocked(message)
            return
        self.reviewGallery.model.set_delete_candidates(candidates, True)
        # Finish the model's accept signal before resetting it for the next
        # group. Some Qt platforms cannot safely reset a list model from the
        # middle of its own signal delivery.
        expected_revision = getattr(group, "layout_revision", None)
        QTimer.singleShot(
            0,
            lambda: self._finish_accepted_group(group, expected_revision),
        )

    def _finish_accepted_group(self, expected_group, expected_revision):
        try:
            if self._review_group is not expected_group:
                return
            if getattr(expected_group, "layout_revision", None) != expected_revision:
                return
            self._select_next_group()
        finally:
            self._accept_in_progress = False
            self.reviewGallery.set_accept_in_progress(False)

    @pyqtSlot()
    def _select_next_group(self):
        result_table = getattr(self.app.model, "result_table", None)
        current_group = self._review_group
        groups = getattr(getattr(self.app.model, "results", None), "groups", ())
        if (
            result_table is not None
            and not getattr(result_table, "power_marker", False)
            and len(groups) > 1
            and len(result_table)
        ):
            row_count = len(result_table)
            group_start = self._normal_result_group_start(result_table, current_group)
            if group_start is not None:
                target_start = group_start + len(current_group)
                if target_start >= row_count:
                    target_start = 0
                target_row = result_table[target_start]
                target_group = getattr(target_row, "_group", None)
                if target_group is not None and target_group is not current_group:
                    target = target_start
                    candidate_index = target_start + 1
                    if candidate_index < row_count:
                        candidate_row = result_table[candidate_index]
                        if getattr(candidate_row, "_group", None) is target_group and getattr(
                            candidate_row, "_dupe", None
                        ) is not getattr(target_group, "ref", None):
                            target = candidate_index
                    result_table.select([target])
                    self._update()
        elif (
            result_table is not None
            and getattr(result_table, "power_marker", False)
            and len(groups) > 1
            and len(result_table)
        ):
            row_count = len(result_table)
            try:
                anchor = next(iter(getattr(result_table, "selected_indexes", ())))
            except (StopIteration, TypeError):
                anchor = None
            if (
                isinstance(anchor, int)
                and 0 <= anchor < row_count
                and getattr(result_table[anchor], "_group", None) is current_group
            ):
                for offset in range(1, row_count + 1):
                    target = (anchor + offset) % row_count
                    row = result_table[target]
                    target_group = getattr(row, "_group", None)
                    if target_group is current_group or target_group is None:
                        continue
                    if getattr(row, "_dupe", None) is getattr(target_group, "ref", None):
                        continue
                    result_table.select([target])
                    self._update()
                    break
        self.nextGroupRequested.emit()

    # --- Override
    @pyqtSlot(QResizeEvent)
    def resizeEvent(self, event):
        self.ensure_same_sizes()
        if self.vController is None or not self.vController.bestFit:
            return
        # Only update the scaled down pixmaps
        self.vController.updateBothImages()

    def show(self):
        # Give the splitter a maximum height to reach. This is assuming that
        # all rows below their headers have the same height
        self.tableView.setMaximumHeight(
            self.tableView.rowHeight(1) * self.tableModel.model.row_count()
            + self.tableView.verticalHeader().sectionSize(0)
            # looks like the handle is taken into account by the splitter
            + self.splitter.handle(2).size().height()
        )
        DetailsDialogBase.show(self)
        self.ensure_same_sizes()
        self._update()

    def hideEvent(self, event):
        if self.vController is not None:
            self.vController.pauseComparisonAnimation()
        super().hideEvent(event)

    def ensure_same_sizes(self):
        # HACK This ensures same size while shrinking.
        # ReferenceViewer might be 1 pixel shorter in width
        # due to the toolbar in the middle keeping the same width,
        # so resizing in the GridLayout's engine leads to not enough space
        # left for the panel on the right.
        # This work as a QMainWindow, but doesn't work as a QDockWidget:
        # resize can only grow. Might need some custom sizeHint somewhere...
        # self.horizontalLayout.setColumnMinimumWidth(
        #     0, self.selectedImageViewer.size().width())
        # self.horizontalLayout.setColumnMinimumWidth(
        #     2, self.selectedImageViewer.size().width())

        # This works when expanding but it's ugly:
        if self.selectedImageViewer.size().width() > self.referenceImageViewer.size().width():
            self.selectedImageViewer.resize(self.referenceImageViewer.size())

    # model --> view
    def refresh(self):
        DetailsDialogBase.refresh(self)
        if self.isVisible():
            self._update()


class EmittingFrame(QFrame):
    """Emits a signal whenever is resized"""

    resized = pyqtSignal(QResizeEvent)

    def resizeEvent(self, event):
        self.resized.emit(event)
