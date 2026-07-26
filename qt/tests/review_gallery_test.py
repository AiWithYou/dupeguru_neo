import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt6.QtTest import QSignalSpy, QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget  # noqa: E402

from core.engine import VerificationKind  # noqa: E402
from qt.pe.comparison import ComparisonError, ComparisonMode  # noqa: E402
from qt.pe.details_dialog import DetailsDialog  # noqa: E402
import qt.pe.image_viewer as image_viewer_module  # noqa: E402
from qt.pe.review_gallery import (  # noqa: E402
    RELATION_COLORS,
    LazyThumbnailLoader,
    ReviewGalleryModel,
    ReviewGalleryWidget,
    ReviewRelation,
    ReviewRole,
    relation_for_group,
)
from qt.pe.thumbnail_cache import ThumbnailDiskCache  # noqa: E402


class FakeFile:
    def __init__(
        self,
        path,
        *,
        size=2048,
        dimensions=(640, 480),
        comparison_pool="incoming",
        is_ref=False,
    ):
        self.path = Path(path)
        self.name = self.path.name
        self.extension = self.path.suffix.lstrip(".")
        self.size = size
        self.mtime = 123.0
        self.dimensions = dimensions
        self.comparison_pool = comparison_pool
        self.is_ref = is_ref


class FakeGroup:
    def __init__(
        self,
        files,
        verification_kind=VerificationKind.UNVERIFIED,
        *,
        relation_kind=None,
    ):
        self.ordered = list(files)
        self.ref = self.ordered[0] if self.ordered else None
        self.verification_kind = verification_kind
        self.relation_kind = relation_kind
        self._sync_reference_flags()

    def __contains__(self, item):
        return any(candidate is item for candidate in self.ordered)

    @property
    def dupes(self):
        return [item for item in self.ordered if item is not self.ref]

    def switch_ref(self, item):
        if item not in self or item is self.ref:
            return False
        self.ref = item
        self._sync_reference_flags()
        return True

    def _sync_reference_flags(self):
        for item in self.ordered:
            item.is_ref = item is self.ref


class FakeResults:
    def __init__(self, group, *, loaded_report=False, complete=True):
        self.groups = list(group) if isinstance(group, (list, tuple)) else [group]
        self.group = self.groups[0]
        self.loaded_report = loaded_report
        self.scan_receipt = SimpleNamespace(allows_destructive_actions=complete)
        self._marked = set()

    def get_group_of_duplicate(self, item):
        return next((group for group in self.groups if item in group), None)

    def is_marked(self, item):
        return item in self._marked

    def mark(self, item):
        self._marked.add(item)

    def unmark(self, item):
        self._marked.discard(item)


class FakeResultRow:
    def __init__(self, group, item):
        self._group = group
        self._dupe = item


class FakeResultTable(list):
    def __init__(self, model):
        self.model = model
        self.power_marker = False
        self.selected_indexes = []
        super().__init__()
        self.refresh()

    def refresh(self):
        self[:] = [
            FakeResultRow(group, item) for group in self.model.results.groups for item in [group.ref, *group.dupes]
        ]

    def select(self, indexes):
        self.selected_indexes = list(indexes)
        self.model.selected_dupes = [self[index]._dupe for index in indexes]


class FakeCoreModel:
    def __init__(self, results, selected):
        self.results = results
        self.selected_dupes = list(selected)
        self.details_panel = FakeDetailsPanel()
        self.result_table = FakeResultTable(self)

    def make_selected_reference(self):
        for item in list(self.selected_dupes):
            group = self.results.get_group_of_duplicate(item)
            if group is not None and group.switch_ref(item):
                self.results.unmark(item)
        self.result_table.refresh()

    def mark_dupe(self, item, marked):
        if marked:
            self.results.mark(item)
        else:
            self.results.unmark(item)


class FakeThumbnailLoader(QObject):
    thumbnailReady = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.pixmap = QPixmap(QSize(32, 24))
        self.pixmap.fill(QColor("#202020"))

    def request(self, key, path, **_generation):
        self.requests.append((key, path))
        return self.pixmap


class FakeDetailsPanel:
    def __init__(self):
        self.view = None
        self.rows = [
            ("Size", "2.0 KiB", "2.0 KiB"),
            ("Dimensions", "640×480", "640×480"),
        ]

    def row_count(self):
        return len(self.rows)

    def row(self, index):
        return self.rows[index]


class FakePreferences:
    details_dialog_override_theme_icons = False
    details_dialog_viewers_show_scrollbars = False
    details_table_delta_foreground_color = QColor("#B00020")
    details_dialog_titlebar_enabled = True
    details_dialog_vertical_titlebar = False

    def restoreGeometry(self, name, widget):
        return False, Qt.DockWidgetArea.BottomDockWidgetArea

    def saveGeometry(self, name, widget):
        pass


class FakeApplication(QObject):
    willSavePrefs = pyqtSignal()

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.prefs = FakePreferences()


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def make_group(
    verification_kind=VerificationKind.VERIFIED_EXACT,
    *,
    relation_kind=None,
    candidate_pool="incoming",
):
    reference = FakeFile("reference.png")
    candidate = FakeFile("candidate.jpg", comparison_pool=candidate_pool)
    group = FakeGroup(
        [reference, candidate],
        verification_kind,
        relation_kind=relation_kind,
    )
    return group, reference, candidate


def test_relation_classes_have_distinct_required_colors(qapp):
    exact, _, _ = make_group(VerificationKind.VERIFIED_EXACT)
    approximate, _, _ = make_group(VerificationKind.SIMILAR)
    semantic, _, _ = make_group(
        VerificationKind.UNVERIFIED,
        relation_kind="semantic_related",
    )

    assert relation_for_group(exact) is ReviewRelation.BYTE_VERIFIED_EXACT
    assert relation_for_group(approximate) is ReviewRelation.VISUAL_APPROXIMATE
    assert relation_for_group(semantic) is ReviewRelation.SEMANTIC_RELATED
    assert QColor(RELATION_COLORS[ReviewRelation.BYTE_VERIFIED_EXACT]) == QColor("#2EAD62")
    assert QColor(RELATION_COLORS[ReviewRelation.VISUAL_APPROXIMATE]) == QColor("#E3B341")
    assert QColor(RELATION_COLORS[ReviewRelation.SEMANTIC_RELATED]) == QColor("#3487E8")

    semantic_model = ReviewGalleryModel(FakeThumbnailLoader())
    semantic_model.set_group(semantic, FakeResults(semantic))
    semantic_index = semantic_model.index(1, 0)
    assert semantic_model.data(semantic_index, ReviewRole.RELATION) is ReviewRelation.SEMANTIC_RELATED
    assert semantic_model.data(semantic_index, ReviewRole.RELATION_COLOR) == QColor("#3487E8")
    assert semantic_model.data(semantic_index, ReviewRole.DELETE_ENABLED) is False


@pytest.mark.parametrize("relation_kind", ("transformed", "crop_candidate"))
def test_review_only_visual_relations_are_approximate(qapp, relation_kind):
    group, _, _ = make_group(
        VerificationKind.UNVERIFIED,
        relation_kind=relation_kind,
    )

    assert relation_for_group(group) is ReviewRelation.VISUAL_APPROXIMATE

    model = ReviewGalleryModel(FakeThumbnailLoader())
    model.set_group(group, FakeResults(group))
    candidate_index = model.index(1, 0)
    assert model.data(candidate_index, ReviewRole.RELATION) is ReviewRelation.VISUAL_APPROXIMATE
    assert model.data(candidate_index, ReviewRole.DELETE_ENABLED) is False


def test_model_requests_thumbnail_and_metadata_only_on_demand(qapp):
    group, _, candidate = make_group()
    loader = FakeThumbnailLoader()
    model = ReviewGalleryModel(loader)
    model.set_group(group, FakeResults(group))

    assert model.rowCount() == 2
    assert loader.requests == []
    assert model.metadata_cache_size == 0

    index = model.index(1, 0)
    assert model.data(index, ReviewRole.FILE) is candidate
    assert loader.requests == []
    assert model.data(index, Qt.ItemDataRole.DecorationRole) is loader.pixmap
    assert len(loader.requests) == 1
    assert model.metadata_cache_size == 0

    metadata = model.data(index, ReviewRole.METADATA)
    assert "640×480" in metadata
    assert "JPG" in metadata
    assert model.metadata_cache_size == 1


def test_real_thumbnail_loader_decodes_after_decoration_request(qapp, tmp_path):
    image_path = tmp_path / "source.png"
    source = QImage(80, 40, QImage.Format.Format_RGB32)
    source.fill(QColor("#CC2244"))
    assert source.save(str(image_path))

    file = FakeFile(image_path, dimensions=(80, 40))
    group = FakeGroup([file], VerificationKind.UNVERIFIED)
    loader = LazyThumbnailLoader(
        QSize(40, 30),
        cache_limit=2,
        disk_cache=ThumbnailDiskCache(tmp_path / "thumbnail-cache"),
    )
    model = ReviewGalleryModel(loader)
    model.set_group(group)
    ready = QSignalSpy(loader.thumbnailReady)

    assert loader.pending_count == 0
    placeholder = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
    assert isinstance(placeholder, QPixmap)
    assert loader.pending_count == 1
    assert ready.wait(3000)
    qapp.processEvents()

    thumbnail = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
    assert isinstance(thumbnail, QPixmap)
    assert not thumbnail.isNull()
    assert thumbnail.width() <= 40
    assert thumbnail.height() <= 30
    assert loader.cached_count == 1
    assert model.thumbnail_waiter_count == 0


@pytest.mark.parametrize(
    ("verification_kind", "loaded_report", "complete", "pool", "expected"),
    [
        (VerificationKind.VERIFIED_EXACT, False, True, "incoming", True),
        (VerificationKind.SIMILAR, False, True, "incoming", False),
        (VerificationKind.UNVERIFIED, False, True, "incoming", False),
        (VerificationKind.VERIFIED_EXACT, True, True, "incoming", False),
        (VerificationKind.VERIFIED_EXACT, False, False, "incoming", False),
        (VerificationKind.VERIFIED_EXACT, False, True, "compare_only", False),
        (VerificationKind.VERIFIED_EXACT, False, True, "protected", False),
    ],
)
def test_delete_enabled_role_follows_core_safety_gate(
    qapp,
    verification_kind,
    loaded_report,
    complete,
    pool,
    expected,
):
    group, _, _ = make_group(verification_kind, candidate_pool=pool)
    widget = ReviewGalleryWidget(thumbnail_loader=FakeThumbnailLoader())
    widget.set_group(
        group,
        FakeResults(group, loaded_report=loaded_report, complete=complete),
        group.ordered[1],
    )
    model = widget.model

    reference_index = model.index(0, 0)
    candidate_index = model.index(1, 0)
    assert model.data(reference_index, ReviewRole.DELETE_ENABLED) is False
    assert model.data(candidate_index, ReviewRole.DELETE_ENABLED) is expected
    assert model.data(candidate_index, ReviewRole.DELETE_REASON)
    assert widget.deleteButton.isEnabled() is expected
    widget.close()


def test_keyboard_operations_emit_signals_and_block_unsafe_delete(qapp):
    group, _, candidate = make_group()
    widget = ReviewGalleryWidget(thumbnail_loader=FakeThumbnailLoader())
    widget.set_group(group, FakeResults(group), candidate)
    widget.show()
    qapp.processEvents()

    keep_spy = QSignalSpy(widget.keeperRequested)
    delete_spy = QSignalSpy(widget.deleteCandidateRequested)
    next_spy = QSignalSpy(widget.nextGroupRequested)

    QTest.keyClick(widget.view, Qt.Key.Key_1)
    QTest.keyClick(widget.view, Qt.Key.Key_2)
    QTest.keyClick(widget.view, Qt.Key.Key_Space)
    qapp.processEvents()

    assert len(keep_spy) == 1
    assert keep_spy[0][0] is candidate
    assert len(delete_spy) == 1
    assert delete_spy[0][0] is candidate
    assert delete_spy[0][1] is True
    assert len(next_spy) == 1
    assert widget.deleteButton.isEnabled()

    QTest.keyClick(widget.view, Qt.Key.Key_2)
    assert len(delete_spy) == 2
    assert delete_spy[1][0] is candidate
    assert delete_spy[1][1] is False

    unsafe_group, _, unsafe_candidate = make_group(VerificationKind.SIMILAR)
    widget.set_group(
        unsafe_group,
        FakeResults(unsafe_group),
        unsafe_candidate,
    )
    blocked_spy = QSignalSpy(widget.blockedAction)
    unsafe_delete_spy = QSignalSpy(widget.deleteCandidateRequested)
    assert not widget.deleteButton.isEnabled()

    QTest.keyClick(widget.view, Qt.Key.Key_2)
    qapp.processEvents()

    assert len(unsafe_delete_spy) == 0
    assert len(blocked_spy) == 1
    assert not widget.deleteButton.isEnabled()
    widget.close()


def test_current_selection_requests_preview(qapp):
    group, _, candidate = make_group()
    model = ReviewGalleryModel(FakeThumbnailLoader())
    widget = ReviewGalleryWidget(thumbnail_loader=model.thumbnail_loader)
    widget.set_group(group, FakeResults(group))
    preview_spy = QSignalSpy(widget.previewRequested)

    widget.view.setCurrentIndex(widget.model.index_for_item(candidate))
    qapp.processEvents()

    assert len(preview_spy) == 1
    assert preview_spy[0][0] is candidate


def test_hovered_card_requests_preview(qapp):
    group, _, candidate = make_group()
    widget = ReviewGalleryWidget(thumbnail_loader=FakeThumbnailLoader())
    widget.resize(500, 420)
    widget.set_group(group, FakeResults(group))
    widget.show()
    qapp.processEvents()
    preview_spy = QSignalSpy(widget.previewRequested)

    candidate_rect = widget.view.visualRect(widget.model.index_for_item(candidate))
    QTest.mouseMove(widget.view.viewport(), candidate_rect.center())
    qapp.processEvents()

    assert len(preview_spy) >= 1
    assert preview_spy[-1][0] is candidate
    widget.close()


def test_picture_details_dialog_integrates_gallery_and_existing_comparison(qapp, tmp_path):
    reference_path = tmp_path / "reference.png"
    candidate_path = tmp_path / "candidate.png"
    for path, color in ((reference_path, "#2244CC"), (candidate_path, "#CC4422")):
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        assert image.save(str(path))

    reference = FakeFile(reference_path, dimensions=(640, 480))
    candidate = FakeFile(candidate_path, dimensions=(640, 480))
    group = FakeGroup(
        [reference, candidate],
        VerificationKind.VERIFIED_EXACT,
    )
    results = FakeResults(group)
    model = FakeCoreModel(results, [candidate])
    app = FakeApplication(model)
    parent = QMainWindow()
    dialog = DetailsDialog(parent, app)
    next_spy = QSignalSpy(dialog.nextGroupRequested)
    delete_spy = QSignalSpy(dialog.deleteCandidateRequested)

    dialog._update()
    qapp.processEvents()

    assert dialog.reviewGallery.model.rowCount() == 2
    assert dialog.reviewGallery.view.currentIndex().data(ReviewRole.FILE) is candidate
    assert not dialog.vController.selectedPixmap.isNull()
    assert not dialog.vController.referencePixmap.isNull()
    assert dialog.vController.same_dimensions
    assert dialog.verticalToolBar.actionSideBySide.shortcut().toString() == "Alt+1"
    assert dialog.verticalToolBar.actionAlphaOverlay.shortcut().toString() == "Alt+2"
    assert dialog.verticalToolBar.actionBlink.shortcut().toString() == "Alt+3"
    assert dialog.verticalToolBar.actionDifference.shortcut().toString() == "Alt+4"
    assert len(dialog.comparisonModeButtons) == 4
    assert dialog.verticalToolBar.actionAlphaOverlay in dialog.actions()
    assert all(button.defaultAction() is not None for button in dialog.comparisonModeButtons)

    parent.show()
    dialog.show()
    dialog.selectedImageViewer.setFocus()
    qapp.processEvents()
    QTest.keyClick(
        dialog.selectedImageViewer,
        Qt.Key.Key_2,
        Qt.KeyboardModifier.AltModifier,
    )
    assert dialog.vController.comparisonMode is ComparisonMode.ALPHA_OVERLAY
    assert dialog.vController.selectedPixmap.size() == dialog.vController.referencePixmap.size()
    assert dialog.comparisonStatusLabel.property("comparisonError") is False
    assert "originals unchanged" in dialog.comparisonStatusLabel.text()

    dialog.vController.zoomNormalSize()
    selected_bar = dialog.selectedImageViewer.horizontalScrollBar()
    reference_bar = dialog.referenceImageViewer.horizontalScrollBar()
    selected_bar.setValue(selected_bar.maximum() // 2)
    qapp.processEvents()
    assert reference_bar.value() == selected_bar.value()

    dialog.verticalToolBar.actionBlink.trigger()
    assert dialog.vController.comparisonMode is ComparisonMode.BLINK
    assert dialog.vController._blinkTimer.isActive()
    first_frame = dialog.vController.selectedPixmap.cacheKey()
    dialog.vController._advanceBlink()
    assert dialog.vController.selectedPixmap.cacheKey() != first_frame
    dialog.vController.pauseComparisonAnimation()
    assert not dialog.vController._blinkTimer.isActive()
    dialog.vController.showBlink()
    assert dialog.vController._blinkTimer.isActive()

    dialog.verticalToolBar.actionDifference.trigger()
    assert dialog.vController.comparisonMode is ComparisonMode.DIFFERENCE_HEATMAP
    assert not dialog.vController._blinkTimer.isActive()
    assert not dialog.vController.selectedPixmap.isNull()

    dialog.verticalToolBar.actionSideBySide.trigger()
    assert dialog.vController.comparisonMode is ComparisonMode.SIDE_BY_SIDE

    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_Space)
    assert len(next_spy) == 1
    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_2)
    assert len(delete_spy) == 1
    assert delete_spy[0][0] is candidate
    assert delete_spy[0][1] is True
    assert results.is_marked(candidate)
    dialog.close()
    parent.close()


def test_gallery_actions_mutate_results_and_advance_visible_group(qapp, tmp_path):
    files = []
    for number, color in enumerate(("#2244CC", "#CC4422", "#228855", "#AA33AA")):
        path = tmp_path / f"image-{number}.png"
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        assert image.save(str(path))
        files.append(FakeFile(path, dimensions=(80, 60)))
    first_group = FakeGroup(files[:2], VerificationKind.VERIFIED_EXACT)
    second_group = FakeGroup(files[2:], VerificationKind.VERIFIED_EXACT)
    results = FakeResults([first_group, second_group])
    model = FakeCoreModel(results, [files[1]])
    app = FakeApplication(model)
    parent = QMainWindow()
    dialog = DetailsDialog(parent, app)
    dialog._update()
    dialog.show()
    qapp.processEvents()

    delete_spy = QSignalSpy(dialog.deleteCandidateRequested)
    keeper_spy = QSignalSpy(dialog.keeperRequested)
    next_spy = QSignalSpy(dialog.nextGroupRequested)

    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_2)
    assert results.is_marked(files[1])
    assert dialog.reviewGallery.model.data(
        dialog.reviewGallery.model.index_for_item(files[1]),
        ReviewRole.DELETE_CANDIDATE,
    )
    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_2)
    assert not results.is_marked(files[1])
    assert len(delete_spy) == 2

    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_1)
    assert first_group.ref is files[1]
    assert model.selected_dupes == [files[1]]
    assert len(keeper_spy) == 1

    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_Space)
    assert dialog._review_group is second_group
    assert model.selected_dupes == [files[3]]
    assert dialog.reviewGallery.view.currentIndex().data(ReviewRole.FILE) is files[3]
    assert len(next_spy) == 1

    dialog.close()
    parent.close()


def test_delete_request_revalidates_live_proof_before_marking(qapp, tmp_path):
    reference_path = tmp_path / "reference.png"
    candidate_path = tmp_path / "candidate.png"
    for path in (reference_path, candidate_path):
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(QColor("#456789"))
        assert image.save(str(path))

    reference = FakeFile(reference_path, dimensions=(32, 24))
    candidate = FakeFile(candidate_path, dimensions=(32, 24))
    group = FakeGroup([reference, candidate], VerificationKind.VERIFIED_EXACT)
    results = FakeResults(group)
    model = FakeCoreModel(results, [candidate])
    app = FakeApplication(model)
    parent = QMainWindow()
    dialog = DetailsDialog(parent, app)
    dialog._update()
    qapp.processEvents()

    candidate_index = dialog.reviewGallery.model.index_for_item(candidate)
    assert dialog.reviewGallery.model.eligibility_at(candidate_index).allowed
    results.scan_receipt.allows_destructive_actions = False
    blocked_spy = QSignalSpy(dialog.reviewGallery.blockedAction)
    changed_spy = QSignalSpy(dialog.deleteCandidateRequested)

    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_2)
    qapp.processEvents()

    assert not results.is_marked(candidate)
    assert not dialog.reviewGallery.model.data(
        candidate_index,
        ReviewRole.DELETE_CANDIDATE,
    )
    assert len(blocked_spy) == 1
    assert len(changed_spy) == 0
    assert not dialog.reviewGallery.deleteButton.isEnabled()

    results.mark(candidate)
    dialog.reviewGallery.model.update_results(results)
    assert dialog.reviewGallery.model.data(
        candidate_index,
        ReviewRole.DELETE_CANDIDATE,
    )
    QTest.keyClick(dialog.reviewGallery.view, Qt.Key.Key_2)
    assert not results.is_marked(candidate)
    assert len(changed_spy) == 1
    assert changed_spy[0][1] is False

    dialog.close()
    parent.close()


def test_comparison_failure_is_visible_and_disables_derived_modes(qapp, tmp_path):
    reference_path = tmp_path / "reference.png"
    invalid_path = tmp_path / "invalid.png"
    reference_image = QImage(64, 48, QImage.Format.Format_RGB32)
    reference_image.fill(QColor("#2255AA"))
    assert reference_image.save(str(reference_path))
    invalid_path.write_bytes(b"not an image")

    reference = FakeFile(reference_path, dimensions=(64, 48))
    candidate = FakeFile(invalid_path, dimensions=(64, 48))
    group = FakeGroup(
        [reference, candidate],
        VerificationKind.VERIFIED_EXACT,
    )
    results = FakeResults(group)
    model = SimpleNamespace(
        selected_dupes=[candidate],
        results=results,
        details_panel=FakeDetailsPanel(),
    )
    app = FakeApplication(model)
    parent = QMainWindow()
    dialog = DetailsDialog(parent, app)

    dialog._update()
    qapp.processEvents()

    assert dialog.comparisonStatusLabel.property("comparisonError") is True
    assert "fallback" in dialog.comparisonStatusLabel.text()
    assert not dialog.verticalToolBar.actionAlphaOverlay.isEnabled()
    assert not dialog.verticalToolBar.actionBlink.isEnabled()
    assert not dialog.verticalToolBar.actionDifference.isEnabled()

    dialog.vController.showAlphaOverlay()
    assert dialog.comparisonStatusLabel.property("comparisonError") is True
    assert "unavailable" in dialog.comparisonStatusLabel.text()
    assert not dialog.vController.referencePixmap.isNull()
    dialog.close()
    parent.close()


def test_comparison_resource_limit_failure_is_visible(
    qapp,
    tmp_path,
    monkeypatch,
):
    reference_path = tmp_path / "reference.png"
    candidate_path = tmp_path / "candidate.png"
    image = QImage(64, 48, QImage.Format.Format_RGB32)
    image.fill(QColor("#2255AA"))
    assert image.save(str(reference_path))
    assert image.save(str(candidate_path))

    def reject_pair(*_args, **_kwargs):
        raise ComparisonError("source dimensions 8001x8000 exceed the safety limit")

    monkeypatch.setattr(
        image_viewer_module,
        "load_normalized_pair",
        reject_pair,
    )
    reference = FakeFile(reference_path, dimensions=(8000, 8000))
    candidate = FakeFile(candidate_path, dimensions=(8001, 8000))
    group = FakeGroup([reference, candidate], VerificationKind.SIMILAR)
    model = SimpleNamespace(
        selected_dupes=[candidate],
        results=FakeResults(group),
        details_panel=FakeDetailsPanel(),
    )
    dialog = DetailsDialog(QMainWindow(), FakeApplication(model))

    dialog._update()
    qapp.processEvents()

    assert dialog.comparisonStatusLabel.property("comparisonError") is True
    assert "safety limit" in dialog.comparisonStatusLabel.text()
    assert not dialog.verticalToolBar.actionAlphaOverlay.isEnabled()
    assert not dialog.verticalToolBar.actionBlink.isEnabled()
    assert not dialog.verticalToolBar.actionDifference.isEnabled()
    dialog.close()


def test_ten_thousand_rows_remain_virtualized(qapp):
    files = [FakeFile(f"library/image-{number:05}.png") for number in range(10_000)]
    group = FakeGroup(files, VerificationKind.SIMILAR)
    loader = FakeThumbnailLoader()
    widget = ReviewGalleryWidget(thumbnail_loader=loader)

    started = time.perf_counter()
    widget.set_group(group, FakeResults(group))
    elapsed = time.perf_counter() - started

    assert widget.model.rowCount() == 10_000
    assert loader.requests == []
    assert widget.model.metadata_cache_size == 0
    assert elapsed < 2.0
    assert widget.view.layoutMode() == widget.view.LayoutMode.Batched

    widget.resize(800, 500)
    widget.show()
    qapp.processEvents()

    assert 0 < len(loader.requests) < 100
    assert 0 < widget.model.metadata_cache_size < 100
    assert len(widget.findChildren(QWidget)) < 100
    widget.close()
