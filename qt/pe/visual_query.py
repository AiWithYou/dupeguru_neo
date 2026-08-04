# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Asynchronous, read-only local-image query UI adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from threading import Event

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from core.engine import VerificationKind
from core.directories import DirectoryState
from core.visual_service import (
    VisualRelation,
    VisualScanConfig,
    VisualService,
)
from hscommon.trans import trget
from qt.pe.review_gallery import (
    ReviewGalleryWidget,
    ReviewRelation,
)

tr = trget("ui")


@dataclass(frozen=True)
class VisualQuerySourcePolicy:
    """Immutable snapshot of directory states and exclusion expressions."""

    states: tuple[tuple[str, int], ...]
    file_patterns: tuple[tuple[str, int], ...]
    path_patterns: tuple[tuple[str, int], ...]
    has_active_exclusions: bool
    _state_map: dict = field(init=False, repr=False, compare=False)
    _compiled_files: tuple = field(init=False, repr=False, compare=False)
    _compiled_paths: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "_state_map",
            {Path(path): state for path, state in self.states},
        )
        object.__setattr__(
            self,
            "_compiled_files",
            tuple(re.compile(pattern, flags) for pattern, flags in self.file_patterns),
        )
        object.__setattr__(
            self,
            "_compiled_paths",
            tuple(re.compile(pattern, flags) for pattern, flags in self.path_patterns),
        )

    @classmethod
    def from_directories(cls, directories, exclude_list=None):
        states = tuple(
            sorted(
                ((_normalized_path(path), int(state)) for path, state in directories.states.items()),
                key=lambda item: item[0],
            )
        )
        if exclude_list is None:
            exclude_list = getattr(directories, "_exclude_list", None)
        if exclude_list is None:
            return cls(states, (), (), False)
        active_count = int(
            getattr(
                exclude_list,
                "marked_count",
                getattr(exclude_list, "mark_count", 0),
            )
            or 0
        )
        if not active_count:
            return cls(states, (), (), False)
        file_patterns = tuple((pattern.pattern, pattern.flags) for pattern in exclude_list.compiled_files)
        path_patterns = tuple((pattern.pattern, pattern.flags) for pattern in exclude_list.compiled_paths)
        return cls(states, file_patterns, path_patterns, True)

    def directory_pruner(self, path):
        path = Path(path)
        if self.state_for(path) != DirectoryState.EXCLUDED:
            return None
        normalized_path = Path(_normalized_path(path))
        has_included_descendant = any(
            normalized_path in Path(override).parents and state != DirectoryState.EXCLUDED
            for override, state in self.states
        )
        if has_included_descendant:
            return None
        return "directory excluded by the visual-query source policy"

    def include_file(self, path):
        path = Path(path)
        return self.state_for(path.parent) != DirectoryState.EXCLUDED and not self._matches_exclusion(path)

    def state_for(self, path):
        path = Path(path)
        normalized_path = Path(_normalized_path(path))
        if normalized_path in self._state_map:
            return self._state_map[normalized_path]
        state = self._default_state(path)
        if state != DirectoryState.NORMAL:
            return state
        for parent in path.parents:
            normalized_parent = Path(_normalized_path(parent))
            if normalized_parent in self._state_map:
                return self._state_map[normalized_parent]
            parent_default = self._default_state(parent)
            if parent_default != DirectoryState.NORMAL:
                return parent_default
        return state

    def _default_state(self, path):
        if self.has_active_exclusions:
            return DirectoryState.EXCLUDED if self._matches_exclusion(path) else DirectoryState.NORMAL
        return DirectoryState.EXCLUDED if path.name.startswith(".") else DirectoryState.NORMAL

    def _matches_exclusion(self, path):
        name = path.name
        full_path = str(path)
        return any(pattern.fullmatch(name) for pattern in self._compiled_files) or any(
            pattern.fullmatch(full_path) for pattern in self._compiled_paths
        )


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


@dataclass(eq=False)
class VisualQueryItem:
    """File-like row consumed by the existing virtualized review gallery."""

    path: Path
    size: int
    mtime_ns: int
    dimensions: tuple[int, int]
    review_relation: ReviewRelation
    review_metadata: str
    review_keeper_label: str = ""
    comparison_pool: str = "compare_only"
    is_ref: bool = False

    @property
    def name(self):
        return self.path.name

    @property
    def extension(self):
        return self.path.suffix.lstrip(".")

    @property
    def mtime(self):
        return self.mtime_ns / 1_000_000_000


class VisualQueryGroup:
    """Read-only group; visual evidence can never become verified exact."""

    verification_kind = VerificationKind.SIMILAR
    relation_kind = "visual_approximate"

    def __init__(self, ordered):
        self.ordered = list(ordered)
        self.ref = self.ordered[0] if self.ordered else None
        if self.ref is not None:
            self.ref.is_ref = True

    @property
    def dupes(self):
        return self.ordered[1:]

    def __contains__(self, item):
        return any(candidate is item for candidate in self.ordered)


class VisualQueryResults:
    """Minimal results facade used solely by the gallery's safety gate."""

    loaded_report = False

    def __init__(self, group, receipt):
        self.group = group
        self.scan_receipt = receipt

    def get_group_of_duplicate(self, item):
        return self.group if item in self.group else None

    @staticmethod
    def is_marked(item):
        return False


def visual_query_group(report):
    """Adapt a VisualReport to similarity-sorted, non-destructive gallery rows."""

    artifacts = {artifact.asset_id: artifact for artifact in report.artifacts}
    assets = {asset.asset_id: asset for asset in report.assets}
    reference_id = report.reference_asset_id
    reference = assets.get(reference_id)
    if reference is None:
        return VisualQueryGroup(())

    best_by_target = {}
    for evidence in report.evidence:
        if evidence.first_id == reference_id:
            target_id = evidence.second_id
        elif evidence.second_id == reference_id:
            target_id = evidence.first_id
        else:
            continue
        current = best_by_target.get(target_id)
        if current is None or evidence.score > current.score:
            best_by_target[target_id] = evidence

    reference_artifact = artifacts.get(reference_id)
    reference_item = _item_from_asset(
        reference,
        reference_artifact,
        ReviewRelation.VISUAL_APPROXIMATE,
        tr("QUERY REFERENCE"),
        keeper_label=tr("REFERENCE"),
    )
    matches = []
    for target_id, evidence in best_by_target.items():
        asset = assets.get(target_id)
        if asset is None:
            continue
        relation = (
            ReviewRelation.SEMANTIC_RELATED
            if evidence.relation is VisualRelation.RELATED
            else ReviewRelation.VISUAL_APPROXIMATE
        )
        relation_labels = {
            VisualRelation.SIMILAR: tr("SIMILAR"),
            VisualRelation.TRANSFORMED: tr("TRANSFORMED (REVIEW ONLY)"),
            VisualRelation.CROP_CANDIDATE: tr("CROP CANDIDATE (REVIEW ONLY)"),
            VisualRelation.RELATED: tr("VISUALLY RELATED"),
        }
        relation_label = relation_labels[evidence.relation]
        metadata = tr(
            "{relation} · {score:.1f}% · blocks {blocks}% · " "pHash {phash} · dHash {dhash} · color {color:.3f}"
        ).format(
            relation=relation_label,
            score=evidence.score * 100,
            blocks=evidence.block_similarity,
            phash=evidence.phash_distance,
            dhash=getattr(evidence, "dhash_distance", 0),
            color=getattr(evidence, "color_histogram_distance", 0.0),
        )
        matches.append(
            (
                evidence.score,
                evidence.block_similarity,
                str(asset.path).casefold(),
                _item_from_asset(asset, artifacts.get(target_id), relation, metadata),
            )
        )
    matches.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return VisualQueryGroup([reference_item, *(item[3] for item in matches)])


def _item_from_asset(
    asset,
    artifact,
    relation,
    metadata,
    *,
    keeper_label="",
):
    dimensions = tuple(getattr(artifact, "dimensions", (0, 0))) if artifact is not None else (0, 0)
    return VisualQueryItem(
        path=Path(asset.path),
        size=int(asset.size),
        mtime_ns=int(asset.mtime_ns),
        dimensions=dimensions,
        review_relation=relation,
        review_metadata=metadata,
        review_keeper_label=keeper_label,
    )


class _VisualQuerySignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()


class _VisualQueryTask(QRunnable):
    def __init__(
        self,
        service_factory,
        reference,
        roots,
        cache_path,
        config,
        source_policy,
    ):
        super().__init__()
        self.service_factory = service_factory
        self.reference = reference
        self.roots = tuple(roots)
        self.cache_path = cache_path
        self.config = config
        self.source_policy = source_policy
        self.signals = _VisualQuerySignals()
        self._cancelled = Event()

    def cancel(self):
        self._cancelled.set()

    @pyqtSlot()
    def run(self):
        try:
            service = self.service_factory(self.cache_path)
            query_options = {
                "roots": self.roots,
                "config": self.config,
                "cancel_check": self._cancelled.is_set,
            }
            if self.source_policy is not None:
                query_options.update(
                    {
                        "directory_pruner": self.source_policy.directory_pruner,
                        "file_filter": self.source_policy.include_file,
                    }
                )
            report = service.query_reference(
                self.reference,
                **query_options,
            )
        except Exception as error:
            if self._cancelled.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.failed.emit(str(error) or type(error).__name__)
            return
        if self._cancelled.is_set():
            self.signals.cancelled.emit()
        else:
            self.signals.finished.emit(report)


class VisualQueryController(QObject):
    """Own one background query and discard results after cancellation."""

    reportReady = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    cancelPending = pyqtSignal()
    runningChanged = pyqtSignal(bool)

    def __init__(
        self,
        parent=None,
        *,
        thread_pool=None,
        service_factory=None,
    ):
        super().__init__(parent)
        self.thread_pool = thread_pool or QThreadPool.globalInstance()
        self.service_factory = service_factory or (lambda cache: VisualService(cache_path=cache))
        self._task = None

    @property
    def running(self):
        return self._task is not None

    def start(
        self,
        reference,
        roots,
        cache_path,
        config=None,
        source_policy=None,
    ):
        if self.running:
            return False
        config = config or VisualScanConfig()
        task = _VisualQueryTask(
            self.service_factory,
            str(reference),
            tuple(str(root) for root in roots),
            str(cache_path),
            config,
            source_policy,
        )
        task.signals.finished.connect(self._finished)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._task = task
        self.runningChanged.emit(True)
        self.thread_pool.start(task)
        return True

    def cancel(self):
        if self._task is None:
            return False
        self._task.cancel()
        self.cancelPending.emit()
        return True

    @pyqtSlot(object)
    def _finished(self, report):
        if self._task is None:
            return
        self._task = None
        self.runningChanged.emit(False)
        self.reportReady.emit(report)

    @pyqtSlot(str)
    def _failed(self, message):
        if self._task is None:
            return
        self._task = None
        self.runningChanged.emit(False)
        self.failed.emit(message)

    @pyqtSlot()
    def _cancelled(self):
        if self._task is None:
            return
        self._task = None
        self.runningChanged.emit(False)
        self.cancelled.emit()


class VisualQueryDialog(QDialog):
    """Progress and results surface backed by ReviewGalleryWidget."""

    cancelRequested = pyqtSignal()
    referenceDropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Find Similar Image"))
        self.resize(820, 620)
        self.setAcceptDrops(True)
        self._running = False

        self.referenceLabel = QLabel(self)
        self.referenceLabel.setWordWrap(True)
        self.statusLabel = QLabel(self)
        self.statusLabel.setWordWrap(True)
        self.progressBar = QProgressBar(self)
        self.issueList = QPlainTextEdit(self)
        self.issueList.setReadOnly(True)
        self.issueList.setMaximumHeight(105)
        self.issueList.hide()
        self.gallery = ReviewGalleryWidget(self)
        self.gallery.keeperButton.hide()
        self.gallery.deleteButton.hide()
        self.gallery.acceptButton.hide()
        self.gallery.nextButton.hide()

        self.cancelButton = QPushButton(tr("Cancel"), self)
        self.closeButton = QPushButton(tr("Close"), self)
        self.closeButton.clicked.connect(self.close)
        self.cancelButton.clicked.connect(self.cancelRequested)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.cancelButton)
        button_row.addWidget(self.closeButton)

        layout = QVBoxLayout(self)
        layout.addWidget(self.referenceLabel)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.progressBar)
        layout.addWidget(self.issueList)
        layout.addWidget(self.gallery, 1)
        layout.addLayout(button_row)
        self._set_idle()

    def start_query(self, reference):
        self._running = True
        self.referenceLabel.setText(tr("Reference: {}").format(reference))
        self.statusLabel.setText(tr("Searching selected picture roots. Source files remain read-only."))
        self.statusLabel.setStyleSheet("")
        self.progressBar.setRange(0, 0)
        self.progressBar.show()
        self.issueList.clear()
        self.issueList.hide()
        self.gallery.clear()
        self.cancelButton.setEnabled(True)
        self.cancelButton.show()
        self.closeButton.setEnabled(False)
        self.show()
        self.raise_()

    def show_report(self, report):
        group = visual_query_group(report)
        results = VisualQueryResults(group, report.scan_receipt)
        self.gallery.set_group(group, results, group.ref)
        match_count = max(0, len(group.ordered) - 1)
        receipt = report.scan_receipt
        self.statusLabel.setText(
            tr(
                "{matches} visual matches · {analyzed}/{discovered} analyzed · "
                "{status}. Read-only evidence; never exact or deletion-enabled."
            ).format(
                matches=match_count,
                analyzed=receipt.analyzed,
                discovered=receipt.discovered,
                status=receipt.status.value.replace("_", " "),
            )
        )
        if not receipt.complete:
            self.statusLabel.setStyleSheet(
                "QLabel { background-color: #6B4A16; color: #FFFFFF; " "border-radius: 3px; padding: 5px; }"
            )
        else:
            self.statusLabel.setStyleSheet(
                "QLabel { background-color: #263544; color: #FFFFFF; " "border-radius: 3px; padding: 5px; }"
            )
        if receipt.issues:
            self.issueList.setPlainText(
                "\n".join(
                    "{}: {}{}".format(
                        issue.code,
                        issue.message,
                        " — {}".format(issue.path) if issue.path else "",
                    )
                    for issue in receipt.issues
                )
            )
            self.issueList.show()
        self._finish()

    def show_error(self, message):
        self.statusLabel.setText(tr("Visual search failed: {}").format(message))
        self.statusLabel.setStyleSheet(
            "QLabel { background-color: #6B2028; color: #FFFFFF; " "border-radius: 3px; padding: 5px; }"
        )
        self._finish()

    def show_cancel_pending(self):
        self.statusLabel.setText(tr("Cancelling… The current image decode will finish before the worker stops."))
        self.cancelButton.setEnabled(False)

    def show_cancelled(self):
        self.statusLabel.setText(tr("Visual search cancelled; no result was applied."))
        self.statusLabel.setStyleSheet("")
        self._finish()

    def _finish(self):
        self._running = False
        self.progressBar.setRange(0, 1)
        self.progressBar.setValue(1)
        self.progressBar.hide()
        self.cancelButton.hide()
        self.closeButton.setEnabled(True)

    def _set_idle(self):
        self.referenceLabel.setText(tr("Drop an image here or choose Find Similar Image."))
        self.statusLabel.setText(tr("Visual matches are read-only and never promoted to exact duplicates."))
        self.progressBar.hide()
        self.cancelButton.hide()
        self.closeButton.setEnabled(True)

    def dragEnterEvent(self, event):
        if self._dropped_image(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        path = self._dropped_image(event.mimeData())
        if path is None:
            event.ignore()
            return
        self.referenceDropped.emit(path)
        event.acceptProposedAction()

    def closeEvent(self, event):
        if self._running:
            self.cancelRequested.emit()
            event.ignore()
            return
        super().closeEvent(event)

    @staticmethod
    def _dropped_image(mime_data):
        urls = mime_data.urls() if mime_data.hasUrls() else ()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = Path(urls[0].toLocalFile())
        if not path.is_file():
            return None
        from core.pe.photo import Photo

        if path.suffix.lower().lstrip(".") not in Photo.HANDLED_EXTS:
            return None
        return str(path)


__all__ = [
    "VisualQueryController",
    "VisualQueryDialog",
    "VisualQueryGroup",
    "VisualQueryItem",
    "VisualQueryResults",
    "VisualQuerySourcePolicy",
    "visual_query_group",
]
