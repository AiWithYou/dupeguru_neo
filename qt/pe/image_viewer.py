# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from math import isfinite

from PyQt6.QtCore import QObject, Qt, QSize, QRectF, QPointF, QPoint, QTimer, pyqtSlot, pyqtSignal, QEvent
from PyQt6.QtGui import QPixmap, QPainter, QPalette, QCursor, QIcon, QKeySequence, QAction, QActionGroup
from PyQt6.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QToolBar,
    QToolButton,
    QWidget,
    QScrollArea,
    QApplication,
    QAbstractScrollArea,
    QStyle,
)
from hscommon.trans import trget
from hscommon.plat import ISLINUX
from qt.pe.comparison import (
    ComparisonError,
    ComparisonMode,
    absolute_difference_heatmap,
    alpha_overlay,
    load_bounded_image,
    load_normalized_pair,
)
from qt.resources import resource_path

tr = trget("ui")

MAX_SCALE = 12.0
MIN_SCALE = 0.1


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _clamp_unit_point(point):
    return QPointF(
        _clamp(float(point.x()), 0.0, 1.0),
        _clamp(float(point.y()), 0.0, 1.0),
    )


def create_actions(actions, target):
    # actions are list of (name, shortcut, icon, desc, func)
    for name, shortcut, icon, desc, func in actions:
        action = QAction(target)
        if icon:
            action.setIcon(icon)
        if shortcut:
            action.setShortcut(shortcut)
        action.setText(desc)
        action.triggered.connect(func)
        setattr(target, name, action)


class ViewerToolBar(QToolBar):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.parent = parent
        self.controller = controller
        self.setupActions(controller)
        self.createButtons()
        self.buttonImgSwap.setEnabled(False)
        self.buttonZoomIn.setEnabled(False)
        self.buttonZoomOut.setEnabled(False)
        self.buttonNormalSize.setEnabled(False)
        self.buttonBestFit.setEnabled(False)

    def setupActions(self, controller):
        # actions are list of (name, shortcut, icon, desc, func)
        ACTIONS = [
            (
                "actionZoomIn",
                QKeySequence.StandardKey.ZoomIn,
                (
                    QIcon.fromTheme("zoom-in")
                    if ISLINUX and not self.parent.app.prefs.details_dialog_override_theme_icons
                    else QIcon(resource_path("zoom_in"))
                ),
                tr("Increase zoom"),
                controller.zoomIn,
            ),
            (
                "actionZoomOut",
                QKeySequence.StandardKey.ZoomOut,
                (
                    QIcon.fromTheme("zoom-out")
                    if ISLINUX and not self.parent.app.prefs.details_dialog_override_theme_icons
                    else QIcon(resource_path("zoom_out"))
                ),
                tr("Decrease zoom"),
                controller.zoomOut,
            ),
            (
                "actionNormalSize",
                tr("Ctrl+/"),
                (
                    QIcon.fromTheme("zoom-original")
                    if ISLINUX and not self.parent.app.prefs.details_dialog_override_theme_icons
                    else QIcon(resource_path("zoom_original"))
                ),
                tr("Normal size"),
                controller.zoomNormalSize,
            ),
            (
                "actionBestFit",
                tr("Ctrl+*"),
                (
                    QIcon.fromTheme("zoom-best-fit")
                    if ISLINUX and not self.parent.app.prefs.details_dialog_override_theme_icons
                    else QIcon(resource_path("zoom_best_fit"))
                ),
                tr("Best fit"),
                controller.zoomBestFit,
            ),
        ]
        # TODO try with QWidgetAction() instead in order to have
        # the popup menu work in the toolbar (if resized below minimum height)
        create_actions(ACTIONS, self)

    def createButtons(self):
        self.buttonImgSwap = QToolButton(self)
        self.buttonImgSwap.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.buttonImgSwap.setIcon(
            QIcon.fromTheme(
                "view-refresh",
                self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            )
            if ISLINUX and not self.parent.app.prefs.details_dialog_override_theme_icons
            else QIcon(resource_path("exchange"))
        )
        self.buttonImgSwap.setText(tr("Swap images"))
        self.buttonImgSwap.setToolTip(tr("Swap images"))
        self.buttonImgSwap.pressed.connect(self.controller.swapImages)
        self.buttonImgSwap.released.connect(self.controller.swapImages)

        self.buttonZoomIn = QToolButton(self)
        self.buttonZoomIn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.buttonZoomIn.setDefaultAction(self.actionZoomIn)
        self.buttonZoomIn.setEnabled(False)

        self.buttonZoomOut = QToolButton(self)
        self.buttonZoomOut.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.buttonZoomOut.setDefaultAction(self.actionZoomOut)
        self.buttonZoomOut.setEnabled(False)

        self.buttonNormalSize = QToolButton(self)
        self.buttonNormalSize.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.buttonNormalSize.setDefaultAction(self.actionNormalSize)
        self.buttonNormalSize.setEnabled(True)

        self.buttonBestFit = QToolButton(self)
        self.buttonBestFit.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.buttonBestFit.setDefaultAction(self.actionBestFit)
        self.buttonBestFit.setEnabled(False)

        self.addWidget(self.buttonImgSwap)
        self.addWidget(self.buttonZoomIn)
        self.addWidget(self.buttonZoomOut)
        self.addWidget(self.buttonNormalSize)
        self.addWidget(self.buttonBestFit)
        self._createModeActions()

    def _createModeActions(self):
        self.modeActionGroup = QActionGroup(self)
        self.modeActionGroup.setExclusive(True)
        actions = (
            (
                "actionSideBySide",
                "S",
                tr("Side-by-side comparison (Alt+1)"),
                "Alt+1",
                ComparisonMode.SIDE_BY_SIDE,
                self.controller.showSideBySide,
            ),
            (
                "actionAlphaOverlay",
                "O",
                tr("Alpha overlay comparison (Alt+2)"),
                "Alt+2",
                ComparisonMode.ALPHA_OVERLAY,
                self.controller.showAlphaOverlay,
            ),
            (
                "actionBlink",
                "B",
                tr("Blink comparison (Alt+3)"),
                "Alt+3",
                ComparisonMode.BLINK,
                self.controller.showBlink,
            ),
            (
                "actionDifference",
                "D",
                tr("Absolute difference heatmap (Alt+4)"),
                "Alt+4",
                ComparisonMode.DIFFERENCE_HEATMAP,
                self.controller.showDifferenceHeatmap,
            ),
        )
        self._mode_actions = {}
        for name, text, tooltip, shortcut, mode, handler in actions:
            action = QAction(text, self)
            action.setCheckable(True)
            action.setToolTip(tooltip)
            action.setStatusTip(tooltip)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(handler)
            self.modeActionGroup.addAction(action)
            self.parent.addAction(action)
            setattr(self, name, action)
            self._mode_actions[mode] = action
        self._mode_actions[ComparisonMode.SIDE_BY_SIDE].setChecked(True)

    def setComparisonMode(self, mode):
        action = self._mode_actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def setComparisonAvailable(self, available):
        for mode, action in self._mode_actions.items():
            if mode is not ComparisonMode.SIDE_BY_SIDE:
                action.setEnabled(available)


class BaseController(QObject):
    """Abstract Base class. Singleton.
    Base proxy interface to keep image viewers synchronized.
    Relays function calls, keep tracks of things."""

    comparisonStatusChanged = pyqtSignal(str, bool)

    def __init__(self, parent):
        super().__init__()
        self.selectedViewer = None
        self.referenceViewer = None
        # cached pixmaps
        self.selectedPixmap = QPixmap()
        self.referencePixmap = QPixmap()
        self.scaledSelectedPixmap = QPixmap()
        self.scaledReferencePixmap = QPixmap()
        self.current_scale = 1.0
        self.bestFit = True
        self.parent = parent  # To change buttons' states
        self.cached_group = None
        self.same_dimensions = True
        self.comparisonMode = ComparisonMode.SIDE_BY_SIDE
        self._comparison_key = None
        self._comparison_pair = None
        self._comparison_error = ""
        self._comparison_rendered_pixmaps = {}
        self._sideSelectedPixmap = QPixmap()
        self._sideReferencePixmap = QPixmap()
        self._blink_show_reference = False
        self._blinkTimer = QTimer(self)
        self._blinkTimer.setInterval(450)
        self._blinkTimer.timeout.connect(self._advanceBlink)

    def setupViewers(self, selected_viewer, reference_viewer):
        self.selectedViewer = selected_viewer
        self.referenceViewer = reference_viewer
        self.selectedViewer.controller = self
        self.referenceViewer.controller = self
        self._setupConnections()

    def _setupConnections(self):
        self.selectedViewer.connectMouseSignals()
        self.referenceViewer.connectMouseSignals()

    def updateView(self, ref, dupe, group):
        # To keep current scale accross dupes from the same group
        previous_same_dimensions = self.same_dimensions
        same_group = True
        if group != self.cached_group:
            same_group = False
            self.resetState()
        self.cached_group = group

        comparison_key = self._comparisonKey(ref, dupe)
        if comparison_key != self._comparison_key:
            self._comparison_key = comparison_key
            self._loadComparisonImages(ref, dupe)
        self._applyComparisonMode()
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(not self.selectedPixmap.isNull())
        self.updateButtonsAsPerDimensions(previous_same_dimensions)
        self._updateComparisonControls()
        self.updateBothImages(same_group)
        self.centerViews(same_group and self.referencePixmap.isNull())

    @staticmethod
    def _comparisonKey(ref, dupe):
        def file_key(file):
            return (
                str(getattr(file, "path", "")),
                getattr(file, "size", None),
                getattr(file, "mtime", None),
            )

        return file_key(dupe), file_key(ref)

    def _loadComparisonImages(self, ref, dupe):
        self._blinkTimer.stop()
        self._comparison_pair = None
        self._comparison_error = ""
        self._comparison_rendered_pixmaps.clear()
        self._sideSelectedPixmap = QPixmap()
        self._sideReferencePixmap = QPixmap()
        selected_path = getattr(dupe, "path", "")
        reference_path = getattr(ref, "path", "")

        if ref is dupe:
            try:
                bounded = load_bounded_image(selected_path)
                self._sideSelectedPixmap = QPixmap.fromImage(bounded.image)
            except ComparisonError as error:
                self._comparison_error = str(error)
            return

        try:
            self._comparison_pair = load_normalized_pair(selected_path, reference_path)
            self._sideSelectedPixmap = QPixmap.fromImage(self._comparison_pair.selected)
            self._sideReferencePixmap = QPixmap.fromImage(self._comparison_pair.reference)
            return
        except ComparisonError as error:
            self._comparison_error = str(error)

        # A malformed image must not make the other pane disappear. Decode
        # each side independently with the same hard display limits.
        try:
            selected = load_bounded_image(selected_path)
            self._sideSelectedPixmap = QPixmap.fromImage(selected.image)
        except ComparisonError:
            pass
        try:
            reference = load_bounded_image(reference_path)
            self._sideReferencePixmap = QPixmap.fromImage(reference.image)
        except ComparisonError:
            pass

    def _applyComparisonMode(self):
        self._blinkTimer.stop()
        self._blink_show_reference = False
        self.selectedPixmap = self._sideSelectedPixmap
        self.referencePixmap = self._sideReferencePixmap
        mode = self.comparisonMode
        pair = self._comparison_pair

        if mode is not ComparisonMode.SIDE_BY_SIDE and pair is None:
            label = self._comparisonModeLabel(mode)
            reason = self._comparison_error or tr("A second readable image is required.")
            self._setComparisonStatus(tr("%s unavailable: %s") % (label, reason), True)
        elif pair is None:
            if self._comparison_error:
                self._setComparisonStatus(
                    tr("Side-by-side fallback: %s") % self._comparison_error,
                    True,
                )
            elif self.selectedPixmap.isNull():
                self._setComparisonStatus(tr("No readable image is selected."), True)
            else:
                self._setComparisonStatus(
                    tr("Side-by-side · select another image to compare."),
                    False,
                )
        else:
            try:
                if mode is ComparisonMode.ALPHA_OVERLAY:
                    self.selectedPixmap = self._derivedComparisonPixmap(mode)
                elif mode is ComparisonMode.BLINK:
                    self.selectedPixmap = self._sideSelectedPixmap
                    self._blinkTimer.start()
                elif mode is ComparisonMode.DIFFERENCE_HEATMAP:
                    self.selectedPixmap = self._derivedComparisonPixmap(mode)
                self.referencePixmap = self._sideReferencePixmap
                self._setComparisonStatus(self._comparisonSuccessStatus(mode, pair), False)
            except (RuntimeError, ValueError) as error:
                self.selectedPixmap = self._sideSelectedPixmap
                self.referencePixmap = self._sideReferencePixmap
                self._setComparisonStatus(
                    tr("%s failed: %s") % (self._comparisonModeLabel(mode), error),
                    True,
                )

        self.same_dimensions = (
            not self.selectedPixmap.isNull()
            and not self.referencePixmap.isNull()
            and self.selectedPixmap.size() == self.referencePixmap.size()
        )

    def _derivedComparisonPixmap(self, mode):
        cached = self._comparison_rendered_pixmaps.get(mode)
        if cached is not None:
            return cached
        if mode is ComparisonMode.ALPHA_OVERLAY:
            image = alpha_overlay(self._comparison_pair)
        elif mode is ComparisonMode.DIFFERENCE_HEATMAP:
            image = absolute_difference_heatmap(self._comparison_pair)
        else:
            raise ValueError(f"{mode.value} has no derived comparison frame")
        pixmap = QPixmap.fromImage(image)
        self._comparison_rendered_pixmaps[mode] = pixmap
        return pixmap

    @staticmethod
    def _comparisonModeLabel(mode):
        labels = {
            ComparisonMode.SIDE_BY_SIDE: tr("Side-by-side"),
            ComparisonMode.ALPHA_OVERLAY: tr("Alpha overlay"),
            ComparisonMode.BLINK: tr("Blink"),
            ComparisonMode.DIFFERENCE_HEATMAP: tr("Difference heatmap"),
        }
        return labels[mode]

    def _comparisonSuccessStatus(self, mode, pair):
        size = pair.display_size
        bounded = tr(" · display-bounded") if pair.bounded else ""
        return tr("%s · normalized to %d×%d%s · originals unchanged") % (
            self._comparisonModeLabel(mode),
            size.width(),
            size.height(),
            bounded,
        )

    def _setComparisonStatus(self, message, is_error):
        self.comparisonStatusChanged.emit(message, is_error)

    def _updateComparisonControls(self):
        toolbar = getattr(self.parent, "verticalToolBar", None)
        if toolbar is None:
            return
        toolbar.setComparisonMode(self.comparisonMode)
        toolbar.setComparisonAvailable(self._comparison_pair is not None)
        toolbar.buttonImgSwap.setEnabled(
            self.comparisonMode is ComparisonMode.SIDE_BY_SIDE
            and not self.selectedPixmap.isNull()
            and not self.referencePixmap.isNull()
        )

    def _clearComparisonContext(self):
        self._blinkTimer.stop()
        self._comparison_key = None
        self._comparison_pair = None
        self._comparison_error = ""
        self._comparison_rendered_pixmaps.clear()
        self._sideSelectedPixmap = QPixmap()
        self._sideReferencePixmap = QPixmap()
        self._blink_show_reference = False

    def setComparisonMode(self, mode):
        mode = ComparisonMode(mode)
        self.comparisonMode = mode
        previous_same_dimensions = self.same_dimensions
        self._applyComparisonMode()
        self.updateButtonsAsPerDimensions(previous_same_dimensions)
        self._updateComparisonControls()
        if self.selectedViewer is not None and self.referenceViewer is not None:
            self.updateBothImages(True)

    @pyqtSlot()
    def showSideBySide(self):
        self.setComparisonMode(ComparisonMode.SIDE_BY_SIDE)

    @pyqtSlot()
    def showAlphaOverlay(self):
        self.setComparisonMode(ComparisonMode.ALPHA_OVERLAY)

    @pyqtSlot()
    def showBlink(self):
        self.setComparisonMode(ComparisonMode.BLINK)

    @pyqtSlot()
    def showDifferenceHeatmap(self):
        self.setComparisonMode(ComparisonMode.DIFFERENCE_HEATMAP)

    def pauseComparisonAnimation(self):
        self._blinkTimer.stop()

    @pyqtSlot()
    def _advanceBlink(self):
        if self.comparisonMode is not ComparisonMode.BLINK or self._comparison_pair is None:
            self._blinkTimer.stop()
            return
        self._blink_show_reference = not self._blink_show_reference
        self.selectedPixmap = self._sideReferencePixmap if self._blink_show_reference else self._sideSelectedPixmap
        if self.selectedViewer is not None:
            self._updateImage(self.selectedPixmap, self.selectedViewer, True)

    def updateBothImages(self, same_group=False):
        # WARNING this is called on every resize event,
        ignore_update = self.referencePixmap.isNull()
        if ignore_update:
            self.selectedViewer.ignore_signal = True
        # the SelectedImageViewer widget sometimes ends up being bigger
        # than the ReferenceImageViewer by one pixel, which distorts the
        # scaled down pixmap for the reference, hence we'll reuse its size here.
        self._updateImage(self.selectedPixmap, self.selectedViewer, same_group)
        self._updateImage(self.referencePixmap, self.referenceViewer, same_group)
        if ignore_update:
            self.selectedViewer.ignore_signal = False

    def _updateImage(self, pixmap, viewer, same_group=False):
        # WARNING this is called on every resize event, might need to split
        # into a separate function depending on the implementation used
        if pixmap.isNull():
            # This should disable the blank widget
            viewer.setImage(pixmap)
            return
        target_size = viewer.size()
        if not viewer.bestFit:
            if same_group:
                viewer.setImage(pixmap)
                return target_size
            # zoomed in state, expand
            # only if not same_group, we need full update
            scaledpixmap = pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.FastTransformation,
            )
        else:
            # best fit, keep ratio always
            scaledpixmap = pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        viewer.setImage(scaledpixmap)
        return target_size

    def resetState(self):
        """Only called when the group of dupes has changed. We reset our
        controller internal state and buttons, center view on viewers."""
        self._clearComparisonContext()
        self.selectedPixmap = QPixmap()
        self.scaledSelectedPixmap = QPixmap()
        self.referencePixmap = QPixmap()
        self.scaledReferencePixmap = QPixmap()
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.current_scale = 1.0
        self.referenceViewer.current_scale = 1.0
        self.selectedViewer.resetCenter()
        self.referenceViewer.resetCenter()
        self.selectedViewer.scaleAt(1.0)
        self.referenceViewer.scaleAt(1.0)
        self.centerViews()
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(True)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)  # active mode by default

    def resetViewersState(self):
        """No item from the model, disable and clear everything."""
        # only called by the details dialog
        self._clearComparisonContext()
        self.selectedPixmap = QPixmap()
        self.scaledSelectedPixmap = QPixmap()
        self.referencePixmap = QPixmap()
        self.scaledReferencePixmap = QPixmap()
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.current_scale = 1.0
        self.referenceViewer.current_scale = 1.0
        self.selectedViewer.resetCenter()
        self.referenceViewer.resetCenter()
        self.selectedViewer.scaleAt(1.0)
        self.referenceViewer.scaleAt(1.0)
        self.centerViews()

        self.parent.verticalToolBar.buttonImgSwap.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(False)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)  # active mode by default

        self.selectedViewer.setImage(self.selectedPixmap)  # null
        self.selectedViewer.setEnabled(False)
        self.referenceViewer.setImage(self.referencePixmap)  # null
        self.referenceViewer.setEnabled(False)
        self._setComparisonStatus(tr("No image is selected for comparison."), False)
        self._updateComparisonControls()

    @pyqtSlot()
    def zoomIn(self):
        self.scaleImagesBy(1.25)

    @pyqtSlot()
    def zoomOut(self):
        self.scaleImagesBy(0.8)

    @pyqtSlot(float)
    def scaleImagesBy(self, factor):
        """Compute new scale from factor and scale."""
        self.current_scale *= factor
        self.selectedViewer.scaleBy(factor)
        self.referenceViewer.scaleBy(factor)
        self.updateButtons()

    @pyqtSlot(float)
    def scaleImagesAt(self, scale):
        """Scale at a pre-computed scale."""
        self.current_scale = scale
        self.selectedViewer.scaleAt(scale)
        self.referenceViewer.scaleAt(scale)
        self.updateButtons()

    def updateButtons(self):
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(self.current_scale < MAX_SCALE)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(self.current_scale > MIN_SCALE)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(round(self.current_scale, 1) != 1.0)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(self.bestFit is False)
        self._updateComparisonControls()

    def updateButtonsAsPerDimensions(self, previous_same_dimensions):
        del previous_same_dimensions
        if not self.bestFit:
            self.updateButtons()

    @pyqtSlot()
    def zoomBestFit(self):
        """Setup before scaling to bestfit"""
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.current_scale = 1.0
        self.referenceViewer.current_scale = 1.0

        self.selectedViewer.scaleAt(1.0)
        self.referenceViewer.scaleAt(1.0)

        self.selectedViewer.resetCenter()
        self.referenceViewer.resetCenter()

        self._updateImage(self.selectedPixmap, self.selectedViewer, True)
        self._updateImage(self.referencePixmap, self.referenceViewer, True)
        self.centerViews()

        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(True)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)
        self.parent.verticalToolBar.buttonImgSwap.setEnabled(True)
        self._updateComparisonControls()

    def setBestFit(self, value):
        self.bestFit = value
        self.selectedViewer.bestFit = value
        self.referenceViewer.bestFit = value

    @pyqtSlot()
    def zoomNormalSize(self):
        self.setBestFit(False)
        self.current_scale = 1.0

        self.selectedViewer.setImage(self.selectedPixmap)
        self.referenceViewer.setImage(self.referencePixmap)

        self.centerViews()

        self.selectedViewer.scaleToNormalSize()
        self.referenceViewer.scaleToNormalSize()

        self.parent.verticalToolBar.buttonZoomIn.setEnabled(True)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(True)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(False)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(True)
        self._updateComparisonControls()

    def centerViews(self, only_selected=False):
        self.selectedViewer.centerViewAndUpdate()
        if only_selected:
            return
        self.referenceViewer.centerViewAndUpdate()

    @pyqtSlot()
    def swapImages(self):
        # swap the columns in the details table as well
        self.parent.tableView.horizontalHeader().swapSections(0, 1)


class QWidgetController(BaseController):
    """Specialized version for QWidget-based viewers."""

    def __init__(self, parent):
        super().__init__(parent)

    def _updateImage(self, *args):
        ret = super()._updateImage(*args)
        # Fix alignment when resizing window
        self.centerViews()
        return ret

    @pyqtSlot(QPointF)
    def onDraggedMouse(self, delta):
        if self.sender() is self.referenceViewer:
            self.selectedViewer.onDraggedMouse(delta)
        else:
            self.referenceViewer.onDraggedMouse(delta)

    @pyqtSlot()
    def swapImages(self):
        self.selectedViewer._pixmap.swap(self.referenceViewer._pixmap)
        self.selectedViewer.centerViewAndUpdate()
        self.referenceViewer.centerViewAndUpdate()
        super().swapImages()


class ScrollAreaController(BaseController):
    """Specialized version fro QLabel-based viewers."""

    def __init__(self, parent):
        super().__init__(parent)
        self._syncing_viewports = False

    def _setupConnections(self):
        super()._setupConnections()
        self.selectedViewer.connectScrollBars()
        self.referenceViewer.connectScrollBars()

    def updateBothImages(self, same_group=False):
        center = self.selectedViewer.normalizedViewportCenter()
        self._syncing_viewports = True
        try:
            super().updateBothImages(same_group)
            if self.selectedViewer.isEnabled():
                self.selectedViewer.setNormalizedViewportCenter(center)
            if self.referenceViewer.isEnabled():
                self.referenceViewer.setNormalizedViewportCenter(center)
        finally:
            self._syncing_viewports = False

    def _otherViewer(self, source):
        if source is self.selectedViewer:
            return self.referenceViewer
        if source is self.referenceViewer:
            return self.selectedViewer
        return None

    def _syncViewportFrom(self, source):
        if self._syncing_viewports or source is None or source.ignore_signal or not source.isEnabled():
            return
        target = self._otherViewer(source)
        if target is None or not target.isEnabled():
            return
        center = source.normalizedViewportCenter()
        self._syncing_viewports = True
        try:
            target.setNormalizedViewportCenter(center)
        finally:
            self._syncing_viewports = False

    @pyqtSlot(QPoint)
    def onDraggedMouse(self, delta):
        source = self.sender()
        target = self._otherViewer(source)
        if self._syncing_viewports or target is None:
            return
        self._syncing_viewports = True
        try:
            source.panBy(delta)
            if target.isEnabled():
                target.setNormalizedViewportCenter(source.normalizedViewportCenter())
        finally:
            self._syncing_viewports = False

    @pyqtSlot()
    def swapImages(self):
        self.referenceViewer._pixmap.swap(self.selectedViewer._pixmap)
        self.referenceViewer.setCachedPixmap()
        self.selectedViewer.setCachedPixmap()
        super().swapImages()

    @pyqtSlot(float, QPointF, QPointF)
    def onMouseWheel(self, zoom_state, image_anchor, viewport_anchor):
        source = self.sender()
        target = self._otherViewer(source)
        if self._syncing_viewports or target is None:
            return
        self._syncing_viewports = True
        try:
            source.setNormalizedZoomState(zoom_state)
            target.setNormalizedZoomState(zoom_state)
            source.setNormalizedPointAtViewportFraction(
                image_anchor,
                viewport_anchor,
            )
            if target.isEnabled():
                target.setNormalizedViewportCenter(source.normalizedViewportCenter())
            self.current_scale = source.current_scale
        finally:
            self._syncing_viewports = False
        self.updateButtons()

    @pyqtSlot(int)
    def onVScrollBarChanged(self, value):
        del value
        sender = self.sender()
        if sender is self.referenceViewer._verticalScrollBar:
            source = self.referenceViewer
        elif sender is self.selectedViewer._verticalScrollBar:
            source = self.selectedViewer
        else:
            return
        self._syncViewportFrom(source)

    @pyqtSlot(int)
    def onHScrollBarChanged(self, value):
        del value
        sender = self.sender()
        if sender is self.referenceViewer._horizontalScrollBar:
            source = self.referenceViewer
        elif sender is self.selectedViewer._horizontalScrollBar:
            source = self.selectedViewer
        else:
            return
        self._syncViewportFrom(source)

    @pyqtSlot(float)
    def scaleImagesBy(self, factor):
        scale = _clamp(
            self.current_scale * factor,
            MIN_SCALE,
            MAX_SCALE,
        )
        self.scaleImagesAt(scale)

    @pyqtSlot(float)
    def scaleImagesAt(self, scale):
        source = self.selectedViewer if self.selectedViewer.isEnabled() else self.referenceViewer
        center = source.normalizedViewportCenter()
        zoom_state = source.normalizedZoomStateForScale(scale)
        self._syncing_viewports = True
        try:
            self.selectedViewer.setNormalizedZoomState(zoom_state)
            self.referenceViewer.setNormalizedZoomState(zoom_state)
            self.selectedViewer.setNormalizedViewportCenter(center)
            self.referenceViewer.setNormalizedViewportCenter(center)
            self.current_scale = self.selectedViewer.current_scale
        finally:
            self._syncing_viewports = False
        self.updateButtons()

    @pyqtSlot()
    def zoomBestFit(self):
        # Disable scrollbars to avoid GridLayout size rounding glitch
        super().zoomBestFit()
        if self.referencePixmap.isNull():
            self.parent.verticalToolBar.buttonImgSwap.setEnabled(False)
        self.selectedViewer.toggleScrollBars()
        self.referenceViewer.toggleScrollBars()


class GraphicsViewController(BaseController):
    """Specialized version fro QGraphicsView-based viewers."""

    def __init__(self, parent):
        super().__init__(parent)

    def _setupConnections(self):
        super()._setupConnections()
        self.selectedViewer.connectScrollBars()
        self.referenceViewer.connectScrollBars()
        # Special case for mouse wheel event conflicting with scrollbar adjustments
        self.selectedViewer.other_viewer = self.referenceViewer
        self.referenceViewer.other_viewer = self.selectedViewer

    @pyqtSlot()
    def syncCenters(self):
        if self.sender() is self.referenceViewer:
            self.selectedViewer.setCenter(self.referenceViewer._centerPoint)
        else:
            self.referenceViewer.setCenter(self.selectedViewer._centerPoint)

    @pyqtSlot(float, QPointF)
    def onMouseWheel(self, factor, new_center):
        self.current_scale *= factor
        if self.sender() is self.referenceViewer:
            self.selectedViewer.scaleBy(factor)
            self.selectedViewer.setCenter(new_center)
        else:
            self.referenceViewer.scaleBy(factor)
            self.referenceViewer.setCenter(new_center)

    @pyqtSlot(int)
    def onVScrollBarChanged(self, value):
        if not self.same_dimensions:
            return
        if self.sender() is self.referenceViewer._verticalScrollBar:
            if not self.selectedViewer.ignore_signal:
                self.selectedViewer._verticalScrollBar.setValue(value)
        else:
            if not self.referenceViewer.ignore_signal:
                self.referenceViewer._verticalScrollBar.setValue(value)

    @pyqtSlot(int)
    def onHScrollBarChanged(self, value):
        if not self.same_dimensions:
            return
        if self.sender() is self.referenceViewer._horizontalScrollBar:
            if not self.selectedViewer.ignore_signal:
                self.selectedViewer._horizontalScrollBar.setValue(value)
        else:
            if not self.referenceViewer.ignore_signal:
                self.referenceViewer._horizontalScrollBar.setValue(value)

    @pyqtSlot()
    def swapImages(self):
        self.referenceViewer._pixmap.swap(self.selectedViewer._pixmap)
        self.referenceViewer.setCachedPixmap()
        self.selectedViewer.setCachedPixmap()
        super().swapImages()

    @pyqtSlot()
    def zoomBestFit(self):
        """Setup before scaling to bestfit"""
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.fitScale()
        self.referenceViewer.fitScale()
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(True)
        if not self.referencePixmap.isNull():
            self.parent.verticalToolBar.buttonImgSwap.setEnabled(True)
        self._updateComparisonControls()
        # else:
        #     self.referenceViewer.setVerticalScrollBarPolicy(
        #         Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        #     )
        #     self.referenceViewer.setHorizontalScrollBarPolicy(
        #         Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        #     )

    def updateView(self, ref, dupe, group):
        super().updateView(ref, dupe, group)

    def updateBothImages(self, same_group=False):
        """This is called only during resize events and while bestFit."""
        ignore_update = self.referencePixmap.isNull()
        if ignore_update:
            self.selectedViewer.ignore_signal = True

        self._updateFitImage(self.selectedPixmap, self.selectedViewer)
        self._updateFitImage(self.referencePixmap, self.referenceViewer)

        if ignore_update:
            self.selectedViewer.ignore_signal = False

    def _updateFitImage(self, pixmap, viewer):
        # If not same_group, we need full update"""
        viewer.setImage(pixmap)
        if pixmap.isNull():
            return
        if viewer.bestFit:
            viewer.fitScale()

    def resetState(self):
        """Only called when the group of dupes has changed. We reset our
        controller internal state and buttons, center view on viewers."""
        self._clearComparisonContext()
        self.selectedPixmap = QPixmap()
        self.referencePixmap = QPixmap()
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.current_scale = 1.0
        self.referenceViewer.current_scale = 1.0

        self.selectedViewer.resetCenter()
        self.referenceViewer.resetCenter()

        self.selectedViewer.fitScale()
        self.referenceViewer.fitScale()
        # self.centerViews()
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(True)

    def resetViewersState(self):
        """No item from the model, disable and clear everything."""
        # only called by the details dialog
        self._clearComparisonContext()
        self.selectedPixmap = QPixmap()
        self.scaledSelectedPixmap = QPixmap()
        self.referencePixmap = QPixmap()
        self.scaledReferencePixmap = QPixmap()
        self.setBestFit(True)
        self.current_scale = 1.0
        self.selectedViewer.current_scale = 1.0
        self.referenceViewer.current_scale = 1.0
        self.selectedViewer.resetCenter()
        self.referenceViewer.resetCenter()
        # self.centerViews()
        self.parent.verticalToolBar.buttonZoomIn.setEnabled(False)
        self.parent.verticalToolBar.buttonZoomOut.setEnabled(False)
        self.parent.verticalToolBar.buttonBestFit.setEnabled(False)
        self.parent.verticalToolBar.buttonImgSwap.setEnabled(False)
        self.parent.verticalToolBar.buttonNormalSize.setEnabled(False)

        self.selectedViewer.setImage(self.selectedPixmap)  # null
        self.selectedViewer.setEnabled(False)
        self.referenceViewer.setImage(self.referencePixmap)  # null
        self.referenceViewer.setEnabled(False)
        self._setComparisonStatus(tr("No image is selected for comparison."), False)
        self._updateComparisonControls()

    @pyqtSlot(float)
    def scaleImagesBy(self, factor):
        self.selectedViewer.updateCenterPoint()
        self.referenceViewer.updateCenterPoint()
        super().scaleImagesBy(factor)
        self.selectedViewer.centerOn(self.selectedViewer._centerPoint)
        # Scrollbars sync themselves here


class QWidgetImageViewer(QWidget):
    """Use a QPixmap, but no scrollbars and no keyboard key sequence for navigation."""

    mouseDragged = pyqtSignal(QPointF)
    mouseWheeled = pyqtSignal(float)

    def __init__(self, parent, name=""):
        super().__init__(parent)
        self._app = QApplication
        self._pixmap = QPixmap()
        self._rect = QRectF()
        self._lastMouseClickPoint = QPointF()
        self._mousePanningDelta = QPointF()
        self.current_scale = 1.0
        self._drag = False
        self._dragConnection = None
        self._wheelConnection = None
        self._instance_name = name
        self.bestFit = True
        self.controller = None
        self.setMouseTracking(False)

    def __repr__(self):
        return f"{self._instance_name}"

    def connectMouseSignals(self):
        if not self._dragConnection:
            self._dragConnection = self.mouseDragged.connect(self.controller.onDraggedMouse)
        if not self._wheelConnection:
            self._wheelConnection = self.mouseWheeled.connect(self.controller.scaleImagesBy)

    def disconnectMouseSignals(self):
        if self._dragConnection:
            self.mouseDragged.disconnect()
            self._dragConnection = None
        if self._wheelConnection:
            self.mouseWheeled.disconnect()
            self._wheelConnection = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.translate(self.rect().center())
        painter.scale(self.current_scale, self.current_scale)
        painter.translate(self._mousePanningDelta)
        painter.drawPixmap(self._rect.topLeft(), self._pixmap)

    def resetCenter(self):
        """Resets origin"""
        # Make sure we are not still panning around
        self._mousePanningDelta = QPointF()
        self.update()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange:
            if self.isEnabled():
                self.connectMouseSignals()
                return
            self.disconnectMouseSignals()

    def contextMenuEvent(self, event):
        """Block parent's (main window) context menu on right click."""
        event.accept()

    def mousePressEvent(self, event):
        if self.bestFit or not self.isEnabled():
            event.ignore()
            return
        if event.button() & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton | Qt.MouseButton.RightButton):
            self._drag = True
        else:
            self._drag = False
            event.ignore()
            return

        self._lastMouseClickPoint = event.position()
        self._app.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self.setMouseTracking(True)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.bestFit or not self.isEnabled():
            event.ignore()
            return

        if self._drag:
            self._mousePanningDelta += (event.position() - self._lastMouseClickPoint) * (1.0 / self.current_scale)
            self._lastMouseClickPoint = event.position()
            self.mouseDragged.emit(self._mousePanningDelta)
            self.update()
            event.accept()
            return
        event.ignore()

    def mouseReleaseEvent(self, event):
        if self.bestFit or not self.isEnabled():
            event.ignore()
            return
        # if event.button() == Qt.MouseButton.LeftButton:
        self._drag = False

        self._app.restoreOverrideCursor()
        self.setMouseTracking(False)

    def wheelEvent(self, event):
        if self.bestFit or not self.isEnabled():
            event.ignore()
            return

        if event.angleDelta().y() > 0:
            if self.current_scale >= MAX_SCALE:
                return
            self.mouseWheeled.emit(1.25)  # zoom-in
        else:
            if self.current_scale <= MIN_SCALE:
                return
            self.mouseWheeled.emit(0.8)  # zoom-out

    def setImage(self, pixmap):
        if pixmap.isNull():
            if not self._pixmap.isNull():
                self._pixmap = pixmap
            self.disconnectMouseSignals()
            self.setEnabled(False)
            self.update()
            return
        elif not self.isEnabled():
            self.setEnabled(True)
            self.connectMouseSignals()
        self._pixmap = pixmap

    def centerViewAndUpdate(self):
        self._rect = self._pixmap.rect()
        self._rect.translate(-self._rect.center())
        self.update()

    def shouldBeActive(self):
        return True if not self.pixmap.isNull() else False

    def scaleBy(self, factor):
        self.current_scale *= factor
        self.update()

    def scaleAt(self, scale):
        self.current_scale = scale
        self.update()

    def sizeHint(self):
        return QSize(400, 400)

    @pyqtSlot()
    def scaleToNormalSize(self):
        """Called when the pixmap is set back to original size."""
        self.current_scale = 1.0
        self.update()

    @pyqtSlot(QPointF)
    def onDraggedMouse(self, delta):
        self._mousePanningDelta = delta
        self.update()


class ScalablePixmap(QWidget):
    """Container for a pixmap that scales up very fast, used in ScrollAreaImageViewer."""

    def __init__(self, parent):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self.current_scale = 1.0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.scale(self.current_scale, self.current_scale)
        # painter.drawPixmap(self.rect().topLeft(), self._pixmap)
        # should be the same as:
        painter.drawPixmap(0, 0, self._pixmap)

    def sizeHint(self):
        return self._pixmap.size() * self.current_scale

    def minimumSizeHint(self):
        return self.sizeHint()


class ScrollAreaImageViewer(QScrollArea):
    """Implementation using a pixmap container in a simple scroll area."""

    mouseDragged = pyqtSignal(QPoint)
    mouseWheeled = pyqtSignal(float, QPointF, QPointF)

    def __init__(self, parent, name=""):
        super().__init__(parent)
        self._parent = parent
        self._app = QApplication
        self._pixmap = QPixmap()
        self._scaledpixmap = None
        self._rect = QRectF()
        self._lastMouseClickPoint = QPointF()
        self.current_scale = 1.0
        self._drag = False
        self._dragConnection = None
        self._wheelConnection = None
        self._instance_name = name
        self.prefs = parent.app.prefs
        self.bestFit = True
        self.controller = None
        self.label = ScalablePixmap(self)
        # This is to avoid sending signals twice on scrollbar updates
        self.ignore_signal = False
        self.setBackgroundRole(QPalette.ColorRole.Dark)
        self.setWidgetResizable(False)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._verticalScrollBar = self.verticalScrollBar()
        self._horizontalScrollBar = self.horizontalScrollBar()

        if self.prefs.details_dialog_viewers_show_scrollbars:
            self.toggleScrollBars()
        else:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.setWidget(self.label)
        self.setVisible(True)

    def __repr__(self):
        return f"{self._instance_name}"

    def toggleScrollBars(self, force_on=False):
        if not self.prefs.details_dialog_viewers_show_scrollbars:
            return
        # Ensure that it's off on the first run
        if self.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded:
            if force_on:
                return
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def connectMouseSignals(self):
        if not self._dragConnection:
            self._dragConnection = self.mouseDragged.connect(self.controller.onDraggedMouse)
        if not self._wheelConnection:
            self._wheelConnection = self.mouseWheeled.connect(self.controller.onMouseWheel)

    def disconnectMouseSignals(self):
        if self._dragConnection:
            self.mouseDragged.disconnect()
            self._dragConnection = None
        if self._wheelConnection:
            self.mouseWheeled.disconnect()
            self._wheelConnection = None

    def connectScrollBars(self):
        """Only call once controller is connected."""
        # Cyclic connections are handled by Qt
        self._verticalScrollBar.valueChanged.connect(
            self.controller.onVScrollBarChanged,
            Qt.ConnectionType.UniqueConnection,
        )
        self._horizontalScrollBar.valueChanged.connect(
            self.controller.onHScrollBarChanged,
            Qt.ConnectionType.UniqueConnection,
        )

    def contextMenuEvent(self, event):
        """Block parent's (main window) context menu on right click."""
        # Even though we don't have a context menu right now, and the default
        # contextMenuPolicy is DefaultContextMenu, we leverage that handler to
        # avoid raising the Result window's Actions menu
        event.accept()

    def mousePressEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        if event.button() & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton | Qt.MouseButton.RightButton):
            self._drag = True
        else:
            self._drag = False
            event.ignore()
            return
        self._lastMouseClickPoint = event.pos()
        self._app.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self.setMouseTracking(True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        if self._drag:
            delta = event.pos() - self._lastMouseClickPoint
            self._lastMouseClickPoint = event.pos()
            self.mouseDragged.emit(delta)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        self._drag = False
        self._app.restoreOverrideCursor()
        self.setMouseTracking(False)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self.bestFit or not self.isEnabled():
            event.ignore()
            return
        old_scale = _clamp(self.current_scale, MIN_SCALE, MAX_SCALE)
        if event.angleDelta().y() > 0:  # zoom-in
            new_scale = min(MAX_SCALE, old_scale * 1.25)
        else:
            new_scale = max(MIN_SCALE, old_scale * 0.8)
        if old_scale == new_scale:
            event.accept()
            return

        position = QPointF(event.position())
        viewport_size = self.viewport().size()
        viewport_anchor = QPointF(
            position.x() / max(1, viewport_size.width()),
            position.y() / max(1, viewport_size.height()),
        )
        self.mouseWheeled.emit(
            self.normalizedZoomStateForScale(new_scale),
            self.normalizedPointAtViewportPosition(position),
            _clamp_unit_point(viewport_anchor),
        )
        event.accept()

    def setImage(self, pixmap):
        self._pixmap = pixmap
        self.label._pixmap = pixmap
        self.label.update()
        self.label.adjustSize()
        if pixmap.isNull():
            self.setEnabled(False)
            self.disconnectMouseSignals()
        elif not self.isEnabled():
            self.setEnabled(True)
            self.connectMouseSignals()

    def centerViewAndUpdate(self):
        self._rect = self.label.rect()
        self.label.rect().translate(-self._rect.center())
        self.label.current_scale = self.current_scale
        self.label.update()
        # self.viewport().update()

    def setCachedPixmap(self):
        """In case we have changed the cached pixmap, reset it."""
        self.label._pixmap = self._pixmap
        self.label.update()

    def shouldBeActive(self):
        return True if not self.pixmap.isNull() else False

    def scaleBy(self, factor):
        self.scaleAt(self.current_scale * factor)

    def scaleAt(self, scale):
        scale = _clamp(float(scale), MIN_SCALE, MAX_SCALE)
        self.current_scale = scale
        self.label.resize(
            QSize(
                max(0, round(self._pixmap.width() * scale)),
                max(0, round(self._pixmap.height() * scale)),
            )
        )
        self.label.current_scale = self.current_scale
        self.label.update()

    @staticmethod
    def normalizedZoomStateForScale(scale):
        scale = float(scale)
        if not isfinite(scale):
            raise ValueError("scale must be finite")
        return (_clamp(scale, MIN_SCALE, MAX_SCALE) - MIN_SCALE) / (MAX_SCALE - MIN_SCALE)

    def normalizedZoomState(self):
        return self.normalizedZoomStateForScale(self.current_scale)

    def setNormalizedZoomState(self, state):
        state = float(state)
        if not isfinite(state):
            raise ValueError("normalized zoom state must be finite")
        self.scaleAt(MIN_SCALE + _clamp(state, 0.0, 1.0) * (MAX_SCALE - MIN_SCALE))

    @staticmethod
    def _normalizedAxisCenter(scrollbar, content_size, viewport_size):
        if content_size <= 0 or content_size <= viewport_size:
            return 0.5
        center = scrollbar.value() + (viewport_size / 2.0)
        return _clamp(center / content_size, 0.0, 1.0)

    def normalizedViewportCenter(self):
        viewport_size = self.viewport().size()
        return QPointF(
            self._normalizedAxisCenter(
                self._horizontalScrollBar,
                self.label.width(),
                viewport_size.width(),
            ),
            self._normalizedAxisCenter(
                self._verticalScrollBar,
                self.label.height(),
                viewport_size.height(),
            ),
        )

    @staticmethod
    def _setNormalizedAxisPoint(
        scrollbar,
        content_size,
        viewport_size,
        normalized_point,
        viewport_fraction,
    ):
        if content_size <= viewport_size:
            scrollbar.setValue(scrollbar.minimum())
            return
        desired = normalized_point * content_size - viewport_fraction * viewport_size
        scrollbar.setValue(
            round(
                _clamp(
                    desired,
                    scrollbar.minimum(),
                    scrollbar.maximum(),
                )
            )
        )

    def setNormalizedPointAtViewportFraction(
        self,
        normalized_point,
        viewport_fraction,
    ):
        normalized_point = _clamp_unit_point(normalized_point)
        viewport_fraction = _clamp_unit_point(viewport_fraction)
        viewport_size = self.viewport().size()
        self._setNormalizedAxisPoint(
            self._horizontalScrollBar,
            self.label.width(),
            viewport_size.width(),
            normalized_point.x(),
            viewport_fraction.x(),
        )
        self._setNormalizedAxisPoint(
            self._verticalScrollBar,
            self.label.height(),
            viewport_size.height(),
            normalized_point.y(),
            viewport_fraction.y(),
        )

    def setNormalizedViewportCenter(self, center):
        self.setNormalizedPointAtViewportFraction(
            center,
            QPointF(0.5, 0.5),
        )

    def normalizedPointAtViewportPosition(self, position):
        position = QPointF(position)
        label_position = self.label.pos()
        width = self.label.width()
        height = self.label.height()
        if width <= 0 or height <= 0:
            return QPointF(0.5, 0.5)
        return _clamp_unit_point(
            QPointF(
                (position.x() - label_position.x()) / width,
                (position.y() - label_position.y()) / height,
            )
        )

    @staticmethod
    def _setScrollBarClamped(scrollbar, value):
        scrollbar.setValue(
            round(
                _clamp(
                    value,
                    scrollbar.minimum(),
                    scrollbar.maximum(),
                )
            )
        )

    def panBy(self, delta):
        self._setScrollBarClamped(
            self._horizontalScrollBar,
            self._horizontalScrollBar.value() - delta.x(),
        )
        self._setScrollBarClamped(
            self._verticalScrollBar,
            self._verticalScrollBar.value() - delta.y(),
        )

    def resetCenter(self):
        """Resets origin"""
        self.current_scale = 1.0

    def setCenter(self, point):
        self._lastMouseClickPoint = point

    def sizeHint(self):
        return self.viewport().rect().size()

    @pyqtSlot()
    def scaleToNormalSize(self):
        """Called when the pixmap is set back to original size."""
        self.scaleAt(1.0)
        self.ensureWidgetVisible(self.label)  # needed for centering
        self.toggleScrollBars(True)

    @pyqtSlot(QPoint)
    def onDraggedMouse(self, delta):
        """Pan this viewport by a screen-space mouse delta."""
        self.panBy(delta)


class GraphicsViewViewer(QGraphicsView):
    """Re-Implementation a full-fledged GraphicsView but is a bit buggy."""

    mouseDragged = pyqtSignal()
    mouseWheeled = pyqtSignal(float, QPointF)

    def __init__(self, parent, name=""):
        super().__init__(parent)
        self._parent = parent
        self._app = QApplication
        self._pixmap = QPixmap()
        self._scaledpixmap = None
        self._rect = QRectF()
        self._lastMouseClickPoint = QPointF()
        self._mousePanningDelta = QPointF()
        self._scaleFactor = 1.3
        self.zoomInFactor = self._scaleFactor
        self.zoomOutFactor = 1.0 / self._scaleFactor
        self.current_scale = 1.0
        self._drag = False
        self._dragConnection = None
        self._wheelConnection = None
        self._instance_name = name
        self.prefs = parent.app.prefs
        self.bestFit = True
        self.controller = None
        self._centerPoint = QPointF()
        self.centerOn(self._centerPoint)
        self.other_viewer = None
        # specific to this class
        self._scene = QGraphicsScene()
        self._scene.setBackgroundBrush(Qt.GlobalColor.black)
        self._item = QGraphicsPixmapItem()
        self.setScene(self._scene)
        self._scene.addItem(self._item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._horizontalScrollBar = self.horizontalScrollBar()
        self._verticalScrollBar = self.verticalScrollBar()
        self.ignore_signal = False

        if self.prefs.details_dialog_viewers_show_scrollbars:
            self.toggleScrollBars()
        else:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)

    def connectMouseSignals(self):
        if not self._dragConnection:
            self._dragConnection = self.mouseDragged.connect(self.controller.syncCenters)
        if not self._wheelConnection:
            self._wheelConnection = self.mouseWheeled.connect(self.controller.onMouseWheel)

    def disconnectMouseSignals(self):
        if self._dragConnection:
            self.mouseDragged.disconnect()
            self._dragConnection = None
        if self._wheelConnection:
            self.mouseWheeled.disconnect()
            self._wheelConnection = None

    def connectScrollBars(self):
        """Only call once controller is connected."""
        # Cyclic connections are handled by Qt
        self._verticalScrollBar.valueChanged.connect(
            self.controller.onVScrollBarChanged,
            Qt.ConnectionType.UniqueConnection,
        )
        self._horizontalScrollBar.valueChanged.connect(
            self.controller.onHScrollBarChanged,
            Qt.ConnectionType.UniqueConnection,
        )

    def toggleScrollBars(self, force_on=False):
        if not self.prefs.details_dialog_viewers_show_scrollbars:
            return
        # Ensure that it's off on the first run
        if self.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded:
            if force_on:
                return
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def contextMenuEvent(self, event):
        """Block parent's (main window) context menu on right click."""
        event.accept()

    def mousePressEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        if event.button() & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton | Qt.MouseButton.RightButton):
            self._drag = True
        else:
            self._drag = False
            event.ignore()
            return
        self._lastMouseClickPoint = event.pos()
        self._app.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self.setMouseTracking(True)
        # We need to propagate to scrollbars, so we send back up
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        self._drag = False
        self._app.restoreOverrideCursor()
        self.setMouseTracking(False)
        self.updateCenterPoint()
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self.bestFit:
            event.ignore()
            return
        if self._drag:
            self._lastMouseClickPoint = event.pos()
            # We can simply rely on the scrollbar updating each other here
            # self.mouseDragged.emit()
            self.updateCenterPoint()
            super().mouseMoveEvent(event)

    def updateCenterPoint(self):
        self._centerPoint = self.mapToScene(self.rect().center())

    def wheelEvent(self, event):
        if self.bestFit or MIN_SCALE > self.current_scale > MAX_SCALE or not self.controller.same_dimensions:
            event.ignore()
            return
        point_before_scale = QPointF(self.mapToScene(self.mapFromGlobal(QCursor.pos())))
        # Get the original screen centerpoint
        screen_center = QPointF(self.mapToScene(self.rect().center()))
        if event.angleDelta().y() > 0:
            factor = self.zoomInFactor
        else:
            factor = self.zoomOutFactor
        # Avoid scrollbars conflict:
        self.other_viewer.ignore_signal = True
        self.scaleBy(factor)
        point_after_scale = QPointF(self.mapToScene(self.mapFromGlobal(QCursor.pos())))
        # Get the offset of how the screen moved
        offset = point_before_scale - point_after_scale
        # Adjust to the new center for correct zooming
        new_center = screen_center + offset
        self.setCenter(new_center)
        self.mouseWheeled.emit(factor, new_center)
        self.other_viewer.ignore_signal = False

    def setImage(self, pixmap):
        if pixmap.isNull():
            self.ignore_signal = True
        elif self.ignore_signal:
            self.ignore_signal = False
        self._pixmap = pixmap
        self._item.setPixmap(pixmap)
        self.translate(1, 1)

    def centerViewAndUpdate(self):
        # Called from the base controller for Normal Size
        pass

    def setCenter(self, point):
        self._centerPoint = point
        self.centerOn(self._centerPoint)

    def resetCenter(self):
        """Resets origin"""
        self._mousePanningDelta = QPointF()
        self.current_scale = 1.0

    def setNewCenter(self, position):
        self._centerPoint = position
        self.centerOn(self._centerPoint)

    def setCachedPixmap(self):
        """In case we have changed the cached pixmap, reset it."""
        self._item.setPixmap(self._pixmap)
        self._item.update()

    def scaleAt(self, scale):
        if scale == 1.0:
            self.resetScale()
        # self.setTransform( QTransform() )
        self.scale(scale, scale)

    def getScale(self):
        return self.transform().m22()

    def scaleBy(self, factor):
        self.current_scale *= factor
        super().scale(factor, factor)

    def resetScale(self):
        # self.setTransform( QTransform() )
        self.resetTransform()  # probably same as above
        self.setCenter(self.scene().sceneRect().center())

    def fitScale(self):
        self.bestFit = True
        super().fitInView(
            self._scene.sceneRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self.setNewCenter(self._scene.sceneRect().center())

    @pyqtSlot()
    def scaleToNormalSize(self):
        """Called when the pixmap is set back to original size."""
        self.bestFit = False
        self.scaleAt(1.0)
        self.toggleScrollBars(True)
        self.update()

    def adjustScrollBarsScaled(self, delta):
        """After scaling with the mouse, update relative to mouse position."""
        self._horizontalScrollBar.setValue(self._horizontalScrollBar.value() + delta.x())
        self._verticalScrollBar.setValue(self._verticalScrollBar.value() + delta.y())

    def sizeHint(self):
        return self.viewport().rect().size()

    def adjustScrollBarsFactor(self, factor):
        """After scaling, no mouse position, default to center."""
        self._horizontalScrollBar.setValue(
            int(factor * self._horizontalScrollBar.value() + ((factor - 1) * self._horizontalScrollBar.pageStep() / 2))
        )
        self._verticalScrollBar.setValue(
            int(factor * self._verticalScrollBar.value() + ((factor - 1) * self._verticalScrollBar.pageStep() / 2))
        )

    def adjustScrollBarsAuto(self):
        """After panning, update accordingly."""
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - self._mousePanningDelta.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - self._mousePanningDelta.y())
