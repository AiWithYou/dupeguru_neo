import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QMimeData, QThreadPool, QUrl  # noqa: E402
from PyQt6.QtGui import QAction, QColor, QImage  # noqa: E402
from PyQt6.QtTest import QSignalSpy  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.app import AppMode  # noqa: E402
from core.directories import Directories as CoreDirectories  # noqa: E402
from core.directories import DirectoryState  # noqa: E402
from core.exclude import ExcludeList  # noqa: E402
from core.pe.image_features import decode_image_features  # noqa: E402
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus  # noqa: E402
from core.visual_service import (  # noqa: E402
    VisualRelation,
    VisualScanConfig,
    VisualService,
)
from qt.app import DupeGuru  # noqa: E402
from qt.pe.review_gallery import ReviewRelation, ReviewRole  # noqa: E402
from qt.pe.visual_query import (  # noqa: E402
    VisualQueryController,
    VisualQueryDialog,
    VisualQuerySourcePolicy,
    visual_query_group,
)


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def _asset(asset_id, path, *, size=100, mtime_ns=123):
    return SimpleNamespace(
        asset_id=asset_id,
        path=str(path),
        size=size,
        mtime_ns=mtime_ns,
    )


def _evidence(target_id, score, relation, *, block_similarity, distance):
    return SimpleNamespace(
        first_id="reference",
        second_id=target_id,
        score=score,
        relation=relation,
        block_similarity=block_similarity,
        phash_distance=distance,
    )


def _report(tmp_path, *, receipt=None):
    assets = (
        _asset("reference", tmp_path / "reference.png"),
        _asset("lower", tmp_path / "lower.png"),
        _asset("higher", tmp_path / "higher.png"),
    )
    artifacts = tuple(SimpleNamespace(asset_id=asset.asset_id, dimensions=(64, 48)) for asset in assets)
    evidence = (
        _evidence(
            "lower",
            0.81,
            VisualRelation.RELATED,
            block_similarity=76,
            distance=7,
        ),
        _evidence(
            "higher",
            0.96,
            VisualRelation.SIMILAR,
            block_similarity=94,
            distance=2,
        ),
    )
    return SimpleNamespace(
        reference_asset_id="reference",
        assets=assets,
        artifacts=artifacts,
        evidence=evidence,
        scan_receipt=receipt or ScanReceipt.completed(3),
    )


def _write_report_images(report):
    for number, asset in enumerate(report.assets):
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor.fromHsv(number * 80, 180, 210))
        assert image.save(asset.path)


def test_visual_query_rows_are_similarity_sorted_and_never_exact(qapp, tmp_path):
    report = _report(tmp_path)
    _write_report_images(report)

    group = visual_query_group(report)

    assert [item.name for item in group.ordered] == [
        "reference.png",
        "higher.png",
        "lower.png",
    ]
    assert group.verification_kind.value == "similar"
    assert group.ordered[1].review_relation is ReviewRelation.VISUAL_APPROXIMATE
    assert group.ordered[2].review_relation is ReviewRelation.SEMANTIC_RELATED
    assert all(item.comparison_pool == "compare_only" for item in group.ordered)
    assert group.ref.review_keeper_label == "REFERENCE"
    assert "SIMILAR" in group.ordered[1].review_metadata
    assert "VISUALLY RELATED" in group.ordered[2].review_metadata
    assert "96.0%" in group.ordered[1].review_metadata


def test_crop_candidate_is_explicitly_review_only_in_gallery_metadata(
    qapp,
    tmp_path,
):
    assets = (
        _asset("reference", tmp_path / "reference.png"),
        _asset("crop", tmp_path / "crop.png"),
    )
    report = SimpleNamespace(
        reference_asset_id="reference",
        assets=assets,
        artifacts=tuple(SimpleNamespace(asset_id=asset.asset_id, dimensions=(64, 48)) for asset in assets),
        evidence=(
            _evidence(
                "crop",
                0.91,
                VisualRelation.CROP_CANDIDATE,
                block_similarity=70,
                distance=0,
            ),
        ),
        scan_receipt=ScanReceipt.completed(2),
    )

    group = visual_query_group(report)

    assert group.ordered[1].review_relation is ReviewRelation.VISUAL_APPROXIMATE
    assert "CROP CANDIDATE (REVIEW ONLY)" in group.ordered[1].review_metadata
    assert group.verification_kind.value == "similar"


def test_real_visual_service_report_adapts_to_read_only_gallery(qapp, tmp_path):
    reference = tmp_path / "reference.png"
    root = tmp_path / "library"
    root.mkdir()
    candidate = root / "candidate.png"
    for path in (reference, candidate):
        image = QImage(40, 30, QImage.Format.Format_RGB32)
        image.fill(QColor("#4466AA"))
        assert image.save(str(path))

    report = VisualService(cache_path=tmp_path / "picture-cache.sqlite3").query_reference(
        reference,
        roots=(root,),
        config=VisualScanConfig(
            similarity_threshold=80,
            phash_radius=8,
        ),
    )
    group = visual_query_group(report)

    assert len(group.ordered) == 2
    assert group.ref.path == reference
    assert group.ordered[1].path == candidate
    assert report.allows_destructive_actions is False
    assert group.verification_kind.value == "similar"


def test_real_visual_service_honors_source_policy_exclusions(qapp, tmp_path):
    reference = tmp_path / "reference.png"
    root = tmp_path / "library"
    excluded_directory = root / "excluded"
    excluded_directory.mkdir(parents=True)
    kept = root / "kept.png"
    excluded_by_state = excluded_directory / "state-excluded.png"
    excluded_by_pattern = root / "ignored.png"
    for path in (
        reference,
        kept,
        excluded_by_state,
        excluded_by_pattern,
    ):
        image = QImage(40, 30, QImage.Format.Format_RGB32)
        image.fill(QColor("#4466AA"))
        assert image.save(str(path))

    exclude_list = ExcludeList()
    exclude_list.add(r"^ignored\.png$")
    exclude_list.mark(r"^ignored\.png$")
    directories = CoreDirectories(exclude_list)
    directories.add_path(root)
    directories.set_state(
        excluded_directory,
        DirectoryState.EXCLUDED,
    )
    policy = VisualQuerySourcePolicy.from_directories(
        directories,
        exclude_list,
    )

    report = VisualService(cache_path=tmp_path / "picture-cache.sqlite3").query_reference(
        reference,
        roots=(root,),
        config=VisualScanConfig(),
        directory_pruner=policy.directory_pruner,
        file_filter=policy.include_file,
    )

    assert {Path(asset.path) for asset in report.assets} == {
        reference,
        kept,
    }
    assert report.scan_receipt.complete


def test_real_visual_service_cancel_hook_stops_after_current_decode(
    qapp,
    tmp_path,
):
    reference = tmp_path / "reference.png"
    root = tmp_path / "library"
    root.mkdir()
    for path in (
        reference,
        root / "first.png",
        root / "second.png",
        root / "third.png",
    ):
        image = QImage(40, 30, QImage.Format.Format_RGB32)
        image.fill(QColor("#4466AA"))
        assert image.save(str(path))

    entered = Event()
    release = Event()
    decoded = []

    def blocking_decoder(path, **kwargs):
        decoded.append(str(path))
        entered.set()
        assert release.wait(3)
        return decode_image_features(path, **kwargs)

    pool = QThreadPool()
    controller = VisualQueryController(
        thread_pool=pool,
        service_factory=lambda cache: VisualService(
            cache_path=cache,
            feature_decoder=blocking_decoder,
        ),
    )
    ready = QSignalSpy(controller.reportReady)
    cancelled = QSignalSpy(controller.cancelled)

    assert controller.start(
        reference,
        (root,),
        tmp_path / "cancel-cache.sqlite3",
    )
    assert entered.wait(1)
    assert controller.cancel()
    release.set()
    assert cancelled.wait(3000)
    qapp.processEvents()

    assert len(decoded) == 1
    assert len(ready) == 0
    assert pool.waitForDone(3000)

    stop = Event()
    stop.set()
    report = VisualService().query_reference(
        reference,
        roots=(root,),
        cancel_check=stop.is_set,
    )
    assert report.scan_receipt.status is ScanStatus.CANCELLED
    assert report.evidence == ()


def test_visual_query_dialog_reuses_gallery_and_surfaces_partial_issues(
    qapp,
    tmp_path,
):
    issue = ScanIssue("resource_limit", "image exceeds decode budget", "huge.png")
    receipt = ScanReceipt.incomplete(
        discovered=4,
        analyzed=3,
        failed=1,
        issues=(issue,),
        status=ScanStatus.RESOURCE_LIMIT,
    )
    report = _report(tmp_path, receipt=receipt)
    _write_report_images(report)
    dialog = VisualQueryDialog()

    dialog.show_report(report)
    qapp.processEvents()

    assert dialog.gallery.model.rowCount() == 3
    assert "resource limit" in dialog.statusLabel.text()
    assert "never exact" in dialog.statusLabel.text()
    assert dialog.issueList.isVisibleTo(dialog)
    assert "image exceeds decode budget" in dialog.issueList.toPlainText()
    candidate_index = dialog.gallery.model.index(1, 0)
    assert dialog.gallery.model.data(candidate_index, ReviewRole.RELATION) is ReviewRelation.VISUAL_APPROXIMATE
    assert not dialog.gallery.model.data(
        candidate_index,
        ReviewRole.DELETE_ENABLED,
    )
    assert dialog.gallery.keeperButton.isHidden()
    assert dialog.gallery.deleteButton.isHidden()
    dialog.close()


def test_visual_query_controller_runs_off_thread_and_returns_report(qapp, tmp_path):
    report = _report(tmp_path)
    calls = []

    class Service:
        def query_reference(
            self,
            reference,
            *,
            roots,
            config,
            cancel_check,
        ):
            calls.append((reference, roots, config, cancel_check))
            return report

    pool = QThreadPool()
    controller = VisualQueryController(
        thread_pool=pool,
        service_factory=lambda cache: Service(),
    )
    ready = QSignalSpy(controller.reportReady)
    running = QSignalSpy(controller.runningChanged)

    assert controller.start(
        tmp_path / "reference.png",
        (tmp_path,),
        tmp_path / "cache.sqlite3",
    )
    assert controller.running
    assert ready.wait(3000)
    qapp.processEvents()

    assert ready[0][0] is report
    assert calls[0][0] == str(tmp_path / "reference.png")
    assert calls[0][1] == (str(tmp_path),)
    assert [entry[0] for entry in running] == [True, False]
    assert not controller.running
    assert pool.waitForDone(3000)


def test_cancel_discards_late_worker_result(qapp, tmp_path):
    report = _report(tmp_path)
    entered = Event()
    release = Event()

    class BlockingService:
        def query_reference(
            self,
            reference,
            *,
            roots,
            config,
            cancel_check,
        ):
            entered.set()
            release.wait(3)
            return report

    pool = QThreadPool()
    controller = VisualQueryController(
        thread_pool=pool,
        service_factory=lambda cache: BlockingService(),
    )
    ready = QSignalSpy(controller.reportReady)
    cancelled = QSignalSpy(controller.cancelled)
    pending = QSignalSpy(controller.cancelPending)

    assert controller.start("reference.png", (tmp_path,), "cache.sqlite3")
    assert entered.wait(1)
    assert controller.cancel()
    assert len(pending) == 1
    release.set()
    assert cancelled.wait(3000)
    qapp.processEvents()

    assert len(ready) == 0
    assert not controller.running
    assert pool.waitForDone(3000)


def test_dialog_accepts_one_local_picture_drop(qapp, tmp_path):
    picture = tmp_path / "reference.png"
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#223344"))
    assert image.save(str(picture))
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(picture))])

    assert VisualQueryDialog._dropped_image(mime_data) == str(picture)

    text = tmp_path / "reference.txt"
    text.write_text("not an image", encoding="utf-8")
    mime_data.setUrls([QUrl.fromLocalFile(str(text))])
    assert VisualQueryDialog._dropped_image(mime_data) is None


def test_running_dialog_close_requests_cancellation(qapp):
    dialog = VisualQueryDialog()
    cancelled = QSignalSpy(dialog.cancelRequested)
    dialog.start_query("reference.png")
    qapp.processEvents()

    dialog.close()
    qapp.processEvents()

    assert len(cancelled) == 1
    assert dialog.isVisible()
    dialog.show_cancelled()
    dialog.close()
    assert not dialog.isVisible()


def test_source_policy_snapshots_nested_states_and_exclude_list(tmp_path):
    root = tmp_path / "library"
    excluded = root / "excluded"
    included_override = excluded / "included"
    excluded_sibling = excluded / "discard"
    for path in (included_override, excluded_sibling):
        path.mkdir(parents=True)
    exclude_list = ExcludeList()
    exclude_list.add(r"^ignored\.png$")
    exclude_list.mark(r"^ignored\.png$")
    directories = CoreDirectories(exclude_list)
    directories.add_path(root)
    directories.set_state(excluded, DirectoryState.EXCLUDED)
    directories.set_state(included_override, DirectoryState.NORMAL)

    policy = VisualQuerySourcePolicy.from_directories(
        directories,
        exclude_list,
    )

    assert policy.directory_pruner(excluded) is None
    assert policy.directory_pruner(excluded_sibling)
    assert policy.directory_pruner(included_override) is None
    assert policy.include_file(included_override / "kept.png")
    assert not policy.include_file(excluded_sibling / "removed.png")
    assert not policy.include_file(included_override / "ignored.png")


def test_source_policy_keeps_default_hidden_directory_exclusion(tmp_path):
    directories = CoreDirectories()
    policy = VisualQuerySourcePolicy.from_directories(directories)

    assert policy.directory_pruner(tmp_path / ".hidden")
    assert policy.directory_pruner(tmp_path / "visible") is None


def test_app_adapter_uses_picture_roots_cache_and_read_only_config(qapp, tmp_path):
    reference = tmp_path / "reference.png"
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#223344"))
    assert image.save(str(reference))
    included = tmp_path / "included"
    excluded = tmp_path / "excluded"
    included.mkdir()
    excluded.mkdir()

    class Directories(list):
        states = {}

        def get_state(self, path):
            return DirectoryState.EXCLUDED if path == excluded else DirectoryState.NORMAL

    calls = []

    class Controller:
        running = False

        def start(
            self,
            reference_path,
            roots,
            cache_path,
            config,
            source_policy,
        ):
            calls.append(
                (
                    reference_path,
                    roots,
                    cache_path,
                    config,
                    source_policy,
                )
            )
            return True

    shell = SimpleNamespace(
        model=SimpleNamespace(
            app_mode=AppMode.PICTURE,
            directories=Directories([included, excluded]),
            exclude_list=ExcludeList(),
            _get_picture_cache_path=lambda: str(tmp_path / "pictures.sqlite3"),
        ),
        prefs=SimpleNamespace(
            filter_hardness=88,
            match_scaled=True,
            match_rotated=False,
        ),
        visualQueryController=Controller(),
        visualQueryDialog=SimpleNamespace(start_query=lambda value: calls.append(("dialog", value))),
        show_message=lambda message: calls.append(("message", message)),
    )

    assert DupeGuru.startVisualQuery(shell, str(reference))
    reference_path, roots, cache_path, config, source_policy = calls[0]
    assert reference_path == str(reference)
    assert roots == [included]
    assert cache_path == str(tmp_path / "pictures.sqlite3")
    assert config.similarity_threshold == 88
    assert config.match_scaled
    assert config.dry_run
    assert isinstance(source_policy, VisualQuerySourcePolicy)
    assert calls[1] == ("dialog", str(reference))


def test_picture_query_action_visibility_tracks_mode_and_running(qapp):
    action = QAction()
    shell = SimpleNamespace(
        model=SimpleNamespace(app_mode=AppMode.STANDARD),
        visualQueryController=SimpleNamespace(running=False),
        actionFindSimilarImage=action,
    )

    DupeGuru.updatePictureQueryAction(shell)
    assert not action.isVisible()

    shell.model.app_mode = AppMode.PICTURE
    DupeGuru.updatePictureQueryAction(shell)
    assert action.isVisible()
    assert action.isEnabled()

    shell.visualQueryController.running = True
    DupeGuru.updatePictureQueryAction(shell)
    assert action.isVisible()
    assert not action.isEnabled()
