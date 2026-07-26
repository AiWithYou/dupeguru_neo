import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSize, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QMouseEvent, QPixmap, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from qt.pe.image_viewer import (  # noqa: E402
    ScrollAreaController,
    ScrollAreaImageViewer,
    QWidgetImageViewer,
)


class FakeButton:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class FakeToolBar:
    def __init__(self):
        self.buttonZoomIn = FakeButton()
        self.buttonZoomOut = FakeButton()
        self.buttonNormalSize = FakeButton()
        self.buttonBestFit = FakeButton()
        self.buttonImgSwap = FakeButton()

    def setComparisonMode(self, mode):
        self.comparison_mode = mode

    def setComparisonAvailable(self, available):
        self.comparison_available = bool(available)


class ViewerParent(QWidget):
    def __init__(self):
        super().__init__()
        prefs = SimpleNamespace(
            details_dialog_viewers_show_scrollbars=False,
        )
        self.app = SimpleNamespace(prefs=prefs)
        self.verticalToolBar = FakeToolBar()


class QWidgetControllerStub:
    same_dimensions = False

    def onDraggedMouse(self, delta):
        self.last_drag = QPointF(delta)

    def scaleImagesBy(self, factor):
        self.last_scale_factor = factor


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


@pytest.fixture
def viewer_pair(qapp):
    parents = []

    def create(selected_size, reference_size):
        parent = ViewerParent()
        parent.resize(660, 260)
        selected = ScrollAreaImageViewer(parent, "selected")
        reference = ScrollAreaImageViewer(parent, "reference")
        selected.setGeometry(0, 0, 320, 240)
        reference.setGeometry(340, 0, 320, 240)
        for viewer in (selected, reference):
            viewer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            viewer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        controller = ScrollAreaController(parent)
        controller.setupViewers(selected, reference)
        controller.setBestFit(False)

        selected_pixmap = QPixmap(selected_size)
        selected_pixmap.fill(QColor("#5A7898"))
        reference_pixmap = QPixmap(reference_size)
        reference_pixmap.fill(QColor("#98785A"))
        selected.setImage(selected_pixmap)
        reference.setImage(reference_pixmap)
        selected.scaleAt(1.0)
        reference.scaleAt(1.0)
        controller.current_scale = 1.0
        controller.same_dimensions = selected_size == reference_size

        parent.show()
        selected.show()
        reference.show()
        qapp.processEvents()
        assert selected.horizontalScrollBar().maximum() > 0
        assert selected.verticalScrollBar().maximum() > 0
        assert reference.horizontalScrollBar().maximum() > 0
        assert reference.verticalScrollBar().maximum() > 0
        parents.append(parent)
        return controller, selected, reference

    yield create

    for parent in parents:
        parent.close()
        parent.deleteLater()
    qapp.processEvents()


VIEWER_DIMENSIONS = (
    (QSize(1200, 900), QSize(1200, 900), True),
    (QSize(1200, 900), QSize(2400, 1500), False),
)


def assert_normalized_centers_match(first, second):
    first_center = first.normalizedViewportCenter()
    second_center = second.normalizedViewportCenter()
    horizontal_tolerance = 2.0 / min(first.label.width(), second.label.width())
    vertical_tolerance = 2.0 / min(first.label.height(), second.label.height())
    assert second_center.x() == pytest.approx(
        first_center.x(),
        abs=horizontal_tolerance,
    )
    assert second_center.y() == pytest.approx(
        first_center.y(),
        abs=vertical_tolerance,
    )


def assert_target_center_is_clamped_to_source(source, target):
    source_center = source.normalizedViewportCenter()
    target_center = target.normalizedViewportCenter()
    viewport = target.viewport().size()

    def expected_axis(source_value, scrollbar, content_size, viewport_size):
        if content_size <= viewport_size:
            return 0.5
        minimum_center = (scrollbar.minimum() + viewport_size / 2.0) / content_size
        maximum_center = (scrollbar.maximum() + viewport_size / 2.0) / content_size
        return max(minimum_center, min(maximum_center, source_value))

    expected_x = expected_axis(
        source_center.x(),
        target.horizontalScrollBar(),
        target.label.width(),
        viewport.width(),
    )
    expected_y = expected_axis(
        source_center.y(),
        target.verticalScrollBar(),
        target.label.height(),
        viewport.height(),
    )
    assert target_center.x() == pytest.approx(
        expected_x,
        abs=2.0 / target.label.width(),
    )
    assert target_center.y() == pytest.approx(
        expected_y,
        abs=2.0 / target.label.height(),
    )


def make_mouse_event(event_type, position, button, buttons):
    position = QPointF(position)
    return QMouseEvent(
        event_type,
        position,
        position,
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


@pytest.mark.parametrize(
    ("selected_size", "reference_size", "same_dimensions"),
    VIEWER_DIMENSIONS,
)
@pytest.mark.parametrize("source_name", ("selected", "reference"))
def test_wheel_zoom_synchronizes_normalized_zoom_and_center(
    viewer_pair,
    selected_size,
    reference_size,
    same_dimensions,
    source_name,
):
    controller, selected, reference = viewer_pair(
        selected_size,
        reference_size,
    )
    selected.setNormalizedViewportCenter(QPointF(0.58, 0.61))
    reference.setNormalizedViewportCenter(QPointF(0.58, 0.61))
    source = selected if source_name == "selected" else reference
    viewport_position = QPointF(
        source.viewport().width() * 0.72,
        source.viewport().height() * 0.37,
    )
    event = QWheelEvent(
        viewport_position,
        viewport_position,
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )

    source.wheelEvent(event)

    assert controller.same_dimensions is same_dimensions
    assert selected.current_scale == pytest.approx(1.25)
    assert reference.current_scale == pytest.approx(1.25)
    assert selected.normalizedZoomState() == pytest.approx(reference.normalizedZoomState())
    assert_normalized_centers_match(selected, reference)
    assert not controller._syncing_viewports


@pytest.mark.parametrize(
    ("selected_size", "reference_size", "same_dimensions"),
    VIEWER_DIMENSIONS,
)
@pytest.mark.parametrize("source_name", ("selected", "reference"))
def test_drag_pan_synchronizes_normalized_center(
    viewer_pair,
    selected_size,
    reference_size,
    same_dimensions,
    source_name,
):
    controller, selected, reference = viewer_pair(
        selected_size,
        reference_size,
    )
    selected.setNormalizedViewportCenter(QPointF(0.5, 0.5))
    reference.setNormalizedViewportCenter(QPointF(0.5, 0.5))
    source = selected if source_name == "selected" else reference
    start = QPoint(180, 130)
    end = QPoint(105, 85)
    before = (
        source.horizontalScrollBar().value(),
        source.verticalScrollBar().value(),
    )

    source.mousePressEvent(
        make_mouse_event(
            QEvent.Type.MouseButtonPress,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    source.mouseMoveEvent(
        make_mouse_event(
            QEvent.Type.MouseMove,
            end,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    source.mouseReleaseEvent(
        make_mouse_event(
            QEvent.Type.MouseButtonRelease,
            end,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )

    assert controller.same_dimensions is same_dimensions
    assert source.horizontalScrollBar().value() > before[0]
    assert source.verticalScrollBar().value() > before[1]
    assert_normalized_centers_match(selected, reference)
    assert not controller._syncing_viewports


@pytest.mark.parametrize(
    ("selected_size", "reference_size", "same_dimensions"),
    VIEWER_DIMENSIONS,
)
@pytest.mark.parametrize("source_name", ("selected", "reference"))
def test_scrollbars_synchronize_normalized_center_and_clamp_boundaries(
    viewer_pair,
    selected_size,
    reference_size,
    same_dimensions,
    source_name,
):
    controller, selected, reference = viewer_pair(
        selected_size,
        reference_size,
    )
    source = selected if source_name == "selected" else reference
    target = reference if source is selected else selected
    horizontal = source.horizontalScrollBar()
    vertical = source.verticalScrollBar()

    horizontal.setValue(round(horizontal.maximum() * 0.73))
    vertical.setValue(round(vertical.maximum() * 0.64))

    assert controller.same_dimensions is same_dimensions
    assert_normalized_centers_match(selected, reference)

    source.setNormalizedViewportCenter(QPointF(2.0, -1.0))

    assert horizontal.value() == horizontal.maximum()
    assert vertical.value() == vertical.minimum()
    assert_target_center_is_clamped_to_source(source, target)
    assert not controller._syncing_viewports


def test_qwidget_zoomed_pan_changes_only_during_an_active_drag(qapp):
    parent = ViewerParent()
    viewer = QWidgetImageViewer(parent, "qwidget")
    viewer.controller = QWidgetControllerStub()
    pixmap = QPixmap(QSize(500, 400))
    pixmap.fill(QColor("#335577"))
    viewer.setImage(pixmap)
    viewer.bestFit = False
    viewer.current_scale = 2.0
    viewer._mousePanningDelta = QPointF(3.0, 4.0)
    viewer._lastMouseClickPoint = QPointF(10.0, 10.0)

    viewer.mouseMoveEvent(
        make_mouse_event(
            QEvent.Type.MouseMove,
            QPoint(30, 20),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert viewer._mousePanningDelta == QPointF(3.0, 4.0)

    viewer.mousePressEvent(
        make_mouse_event(
            QEvent.Type.MouseButtonPress,
            QPoint(10, 10),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    viewer.mouseMoveEvent(
        make_mouse_event(
            QEvent.Type.MouseMove,
            QPoint(30, 20),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    viewer.mouseReleaseEvent(
        make_mouse_event(
            QEvent.Type.MouseButtonRelease,
            QPoint(30, 20),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )

    assert viewer._mousePanningDelta == QPointF(13.0, 9.0)

    viewer.mouseMoveEvent(
        make_mouse_event(
            QEvent.Type.MouseMove,
            QPoint(80, 60),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert viewer._mousePanningDelta == QPointF(13.0, 9.0)
    parent.close()
    parent.deleteLater()
    qapp.processEvents()
