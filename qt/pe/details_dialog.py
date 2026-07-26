# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot
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
                self.reviewGallery.model.update_results(self.app.model.results)
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

    def _result_rows(self):
        result_table = getattr(self.app.model, "result_table", None)
        if result_table is None:
            return None, []
        try:
            return result_table, list(result_table)
        except TypeError:
            return result_table, []

    def _select_result_item(self, item) -> bool:
        result_table, rows = self._result_rows()
        if result_table is None:
            return False
        for row_index, row in enumerate(rows):
            if getattr(row, "_dupe", None) is item:
                result_table.select([row_index])
                return True
        return False

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

    @pyqtSlot()
    def _select_next_group(self):
        result_table, rows = self._result_rows()
        visible_groups = []
        visible_group_ids = set()
        for row in rows:
            group = getattr(row, "_group", None)
            group_id = id(group)
            if group is not None and group_id not in visible_group_ids:
                visible_group_ids.add(group_id)
                visible_groups.append(group)

        current_group = self._review_group
        if result_table is not None and len(visible_groups) > 1:
            try:
                current_index = next(index for index, group in enumerate(visible_groups) if group is current_group)
            except StopIteration:
                current_index = -1
            target_group = visible_groups[(current_index + 1) % len(visible_groups)]
            target_rows = [
                (row_index, row) for row_index, row in enumerate(rows) if getattr(row, "_group", None) is target_group
            ]
            target = next(
                (
                    (row_index, row)
                    for row_index, row in target_rows
                    if getattr(row, "_dupe", None) is not getattr(target_group, "ref", None)
                ),
                target_rows[0] if target_rows else None,
            )
            if target is not None:
                result_table.select([target[0]])
                self._update()
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
