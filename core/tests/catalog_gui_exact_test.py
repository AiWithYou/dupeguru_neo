# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from pathlib import Path
import time
from types import SimpleNamespace

from hscommon.jobprogress.job import nulljob

import core.app as app_module
from core import engine
from core.action_plan import build_bound_deletion_plan
from core.catalog import Catalog, ExactDigestProjectionCounts
from core.catalog_worker import CatalogWorker
from core.directories import DirectoryState
from core.scan_receipt import ScanStatus
from core.scanner import ScanType
from core.tests.base import NamedObject, TestApp


def create_contents_app(tmp_path, roots):
    app = TestApp().app
    appdata = tmp_path / "appdata"
    appdata.mkdir(exist_ok=True)
    app.appdata = str(appdata)
    app.options["scan_type"] = ScanType.CONTENTS
    for root in roots:
        app.directories.add_path(root)
    return app


def run_scan(app):
    app.start_scanning()
    deadline = time.monotonic() + 10
    while app.progress_window._job_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not app.progress_window._job_running
    app.progress_window.reraise_if_error()


def test_gui_contents_scan_is_warm_and_revalidates_rename(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"identical")
    second.write_bytes(b"identical")
    app = create_contents_app(tmp_path, (root,))

    run_scan(app)

    assert len(app.results.groups) == 1
    assert app.results.scan_receipt.allows_destructive_actions
    assert app.results.groups[0].verification_kind is engine.VerificationKind.VERIFIED_EXACT
    catalog = Catalog(Path(app.appdata) / "catalog.sqlite3")
    first_scan_id = catalog.latest_complete_scan_id()
    first_artifact_count = catalog._connection.execute(
        "SELECT COUNT(*) FROM artifacts WHERE algorithm = 'sha256'"
    ).fetchone()[0]
    catalog.close()

    renamed = root / "renamed.bin"
    second.rename(renamed)
    run_scan(app)

    assert len(app.results.groups) == 1
    assert {file.path for file in app.results.groups[0]} == {first, renamed}
    catalog = Catalog(Path(app.appdata) / "catalog.sqlite3")
    second_scan_id = catalog.latest_complete_scan_id()
    coverage = catalog.scan_coverage(second_scan_id)
    assert second_scan_id != first_scan_id
    assert coverage.get("work_pending", 0) == 0
    assert coverage.get("work_complete", 0) == 1
    assert (
        catalog._connection.execute("SELECT COUNT(*) FROM artifacts WHERE algorithm = 'sha256'").fetchone()[0]
        == first_artifact_count + 1
    )
    catalog.close()


def test_gui_catalog_applies_excluded_protected_and_cross_pool_policies(tmp_path):
    root = tmp_path / "library"
    incoming = root / "incoming"
    protected = root / "protected"
    excluded = root / "excluded"
    incoming.mkdir(parents=True)
    protected.mkdir()
    excluded.mkdir()
    content = b"same"
    incoming_file = incoming / "incoming.bin"
    protected_file = protected / "protected.bin"
    excluded_file = excluded / "excluded.bin"
    incoming_file.write_bytes(content)
    protected_file.write_bytes(content)
    excluded_file.write_bytes(content)
    app = create_contents_app(tmp_path, (root,))
    app.directories.set_state(protected, DirectoryState.REFERENCE)
    app.directories.set_state(excluded, DirectoryState.EXCLUDED)
    app.options["comparison_scope"] = "cross_pool"

    run_scan(app)

    assert len(app.results.groups) == 1
    group = app.results.groups[0]
    assert {file.path for file in group} == {incoming_file, protected_file}
    assert excluded_file not in {file.path for file in group}
    by_path = {file.path: file for file in group}
    assert by_path[incoming_file].comparison_pool == "incoming"
    assert not by_path[incoming_file].is_ref
    assert by_path[protected_file].comparison_pool == "protected"
    assert by_path[protected_file].is_ref
    assert app.results.scan_receipt.allows_destructive_actions


def test_gui_partial_catalog_scan_has_no_results_and_can_resume(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    (root / "one.bin").write_bytes(b"same")
    (root / "two.bin").write_bytes(b"same")
    app = create_contents_app(tmp_path, (root,))
    monkeypatch.setattr(app, "_catalog_cancel_requested", lambda _job: True)

    run_scan(app)

    assert app.results.groups == []
    assert not app.results.scan_receipt.allows_destructive_actions
    scan_id = app._catalog_resume_scan_id
    assert scan_id is not None
    catalog = Catalog(Path(app.appdata) / "catalog.sqlite3")
    assert catalog.get_scan(scan_id)["status"] == "running"
    catalog.close()

    monkeypatch.setattr(app, "_catalog_cancel_requested", lambda _job: False)
    run_scan(app)

    assert app._catalog_resume_scan_id is None
    assert len(app.results.groups) == 1
    assert app.results.scan_receipt.allows_destructive_actions


def test_gui_hydration_failure_discards_all_results_and_disables_actions(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    root.mkdir()
    (root / "one.bin").write_bytes(b"same")
    (root / "two.bin").write_bytes(b"same")
    app = create_contents_app(tmp_path, (root,))
    monkeypatch.setattr(
        CatalogWorker,
        "hydrate_file",
        lambda _worker, _file, _content_version_id: False,
    )

    run_scan(app)

    assert app.results.groups == []
    assert not app.results.scan_receipt.allows_destructive_actions
    assert app.results.scan_receipt.failed == 1


def test_gui_selected_root_scope_never_returns_old_unselected_root(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    content = b"same"
    first_file = first / "first.bin"
    second_file = second / "second.bin"
    first_file.write_bytes(content)
    second_file.write_bytes(content)
    combined = create_contents_app(tmp_path, (first, second))
    run_scan(combined)
    assert len(combined.results.groups) == 1

    selected = create_contents_app(tmp_path, (first,))
    selected.appdata = combined.appdata
    run_scan(selected)

    assert selected.results.groups == []
    assert selected.results.scan_receipt.allows_destructive_actions


def test_gui_catalog_exact_group_builds_live_sha256_quarantine_plan(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    payload = b"catalog-backed exact payload\n" * 128
    first.write_bytes(payload)
    second.write_bytes(payload)
    app = create_contents_app(tmp_path, (root,))

    run_scan(app)

    [group] = app.results.groups
    assert group.evidence.algorithm == "sha256"
    target = group.dupes[0]
    keeper_path = Path(group.ref.path)
    bound = build_bound_deletion_plan(app.results, [target], [root])
    app._do_delete(nulljob, bound)

    assert not Path(target.path).exists()
    assert keeper_path.read_bytes() == payload
    assert len(app.last_quarantine_plan_paths) == 1


def _projection_service_result(files_observed):
    return SimpleNamespace(
        outcome="finished",
        catalog_status="complete",
        status=SimpleNamespace(
            verified_projection_allowed=True,
            error_count=0,
            work_counts={"total": files_observed},
        ),
        files_observed=files_observed,
        work_failed=0,
        scan_id=123,
        errors=(),
    )


def _projection_group():
    files = [NamedObject("review-keeper.bin"), NamedObject("review-target.bin")]
    evidence = engine.ExactEvidence(
        kind=engine.VerificationKind.VERIFIED_EXACT,
        algorithm="sha256",
        digest=b"\x01" * 32,
        size=1,
    )
    return engine.Group.from_exact_files(files, evidence)


def test_gui_projection_preflights_counts_and_keeps_only_complete_bounded_groups(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    root.mkdir()
    app = create_contents_app(tmp_path, (root,))
    review_group = _projection_group()
    catalog_group = SimpleNamespace(files=(object(), object()))
    events = []

    class FakeCatalogService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cancel_check=None):
            return _projection_service_result(6)

        def verified_exact_projection_counts(self):
            events.append("counts")
            return ExactDigestProjectionCounts(
                group_count=2,
                file_count=6,
                max_group_members=4,
            )

        def iter_verified_exact_groups(
            self,
            page_size,
            max_page_files,
            max_group_members,
        ):
            assert events == ["counts"]
            assert page_size == 2
            assert max_page_files == 2
            assert max_group_members == 2
            events.append("iterate")
            yield catalog_group

        def close(self):
            events.append("close")

    class FakeScanner:
        discarded_file_count = 0

        @staticmethod
        def get_dupe_groups_from_verified_exact(groups, _ignore_list, _job):
            return list(groups)

    def materialize(_service, candidate):
        assert candidate is catalog_group
        events.append("materialize")
        return review_group

    monkeypatch.setattr(app_module, "CatalogService", FakeCatalogService)
    monkeypatch.setattr(app_module, "CATALOG_GUI_EXACT_PAGE_GROUPS", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_EXACT_PAGE_FILES", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_GROUPS", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_FILES", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_GROUP_MEMBERS", 2)
    monkeypatch.setattr(app, "_materialize_catalog_exact_group", materialize)

    app._run_catalog_contents_scan(FakeScanner(), nulljob)

    assert events == ["counts", "iterate", "materialize", "close"]
    assert app.results.groups == [review_group]
    assert app.results.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
    assert not app.results.scan_receipt.allows_destructive_actions
    assert [issue.code for issue in app.results.scan_receipt.issues] == ["catalog_projection_limit"]


def test_gui_projection_at_exact_limits_remains_complete(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    app = create_contents_app(tmp_path, (root,))
    review_group = _projection_group()
    catalog_group = SimpleNamespace(files=(object(), object()))

    class FakeCatalogService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cancel_check=None):
            return _projection_service_result(2)

        def verified_exact_projection_counts(self):
            return ExactDigestProjectionCounts(1, 2, 2)

        def iter_verified_exact_groups(self, **kwargs):
            assert kwargs == {
                "page_size": 1,
                "max_page_files": 2,
                "max_group_members": 2,
            }
            yield catalog_group

        def close(self):
            pass

    class FakeScanner:
        discarded_file_count = 0

        @staticmethod
        def get_dupe_groups_from_verified_exact(groups, _ignore_list, _job):
            return list(groups)

    monkeypatch.setattr(app_module, "CatalogService", FakeCatalogService)
    monkeypatch.setattr(app_module, "CATALOG_GUI_EXACT_PAGE_GROUPS", 1)
    monkeypatch.setattr(app_module, "CATALOG_GUI_EXACT_PAGE_FILES", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_GROUPS", 1)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_FILES", 2)
    monkeypatch.setattr(app_module, "CATALOG_GUI_MAX_EXACT_GROUP_MEMBERS", 2)
    monkeypatch.setattr(
        app,
        "_materialize_catalog_exact_group",
        lambda _service, _catalog_group: review_group,
    )

    app._run_catalog_contents_scan(FakeScanner(), nulljob)

    assert app.results.groups == [review_group]
    assert app.results.scan_receipt.complete
    assert app.results.scan_receipt.allows_destructive_actions


def test_gui_projection_cancel_after_preflight_never_materializes_groups(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    root.mkdir()
    app = create_contents_app(tmp_path, (root,))
    calls = []

    class FakeCatalogService:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, cancel_check=None):
            return _projection_service_result(2)

        def verified_exact_projection_counts(self):
            calls.append("counts")
            return ExactDigestProjectionCounts(1, 2, 2)

        def iter_verified_exact_groups(self, **_kwargs):
            raise AssertionError("cancelled projection must not fetch candidate rows")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(app_module, "CatalogService", FakeCatalogService)
    monkeypatch.setattr(app, "_catalog_cancel_requested", lambda _job: True)
    monkeypatch.setattr(
        app,
        "_materialize_catalog_exact_group",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cancelled projection must not materialize files")),
    )

    app._run_catalog_contents_scan(SimpleNamespace(), nulljob)

    assert calls == ["counts", "close"]
    assert app.results.groups == []
    assert not app.results.scan_receipt.allows_destructive_actions
    assert app._catalog_resume_scan_id is None
