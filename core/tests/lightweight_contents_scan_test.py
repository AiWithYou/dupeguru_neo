# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os
import time
from pathlib import Path

from hscommon.jobprogress.job import nulljob

import core.app as app_module
from core import engine, fs
from core.action_plan import build_bound_deletion_plan
from core.directories import DirectoryState
from core.destructive_eligibility import evaluate_relocation, evaluate_rename
from core.scanner import ScanType
from core.tests.base import TestApp


def create_contents_app(tmp_path, roots, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir(exist_ok=True)
    monkeypatch.setattr(
        app_module.desktop,
        "special_folder_path",
        lambda _folder, portable=False: str(appdata),
    )
    app = TestApp().app
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


def catalog_members(app):
    database = Path(app.appdata) / app_module.CATALOG_FILENAME
    return tuple(
        path
        for path in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
            Path(str(database) + "-journal"),
        )
        if path.exists()
    )


def test_gui_contents_scan_is_direct_lightweight_and_finds_duplicates(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"identical payload")
    second.write_bytes(b"identical payload")
    for index in range(32):
        (root / f"unique-{index}.bin").write_bytes(bytes([index]) * (100 + index))
    app = create_contents_app(tmp_path, (root,), monkeypatch)
    full_digest_paths = []
    original_digest = fs.File._calc_digest_with_snapshot

    def record_full_digest(file, *args, **kwargs):
        full_digest_paths.append(file.path)
        return original_digest(file, *args, **kwargs)

    monkeypatch.setattr(fs.File, "_calc_digest_with_snapshot", record_full_digest)
    monkeypatch.setattr(
        fs.File,
        "begin_review_scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Contents scan must not build a full-library organizer proof")
        ),
    )
    monkeypatch.setattr(
        fs.File,
        "validate_review_scan_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Contents scan must not reread every file after matching")
        ),
    )
    monkeypatch.setattr(
        fs.File,
        "begin_review_scan_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Contents scan owns its exact generation baseline")
        ),
    )
    monkeypatch.setattr(
        fs.File,
        "validate_review_scan_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Contents scan owns its exact generation validation")
        ),
    )
    monkeypatch.setattr(
        fs.File,
        "seal_review_scan_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Contents scan must reuse exact-engine content proofs")
        ),
    )

    run_scan(app)

    assert len(app.results.groups) == 1
    group = app.results.groups[0]
    assert {file.path for file in group} == {first, second}
    assert group.verification_kind is engine.VerificationKind.VERIFIED_EXACT
    assert app.results.scan_receipt.complete
    assert set(full_digest_paths) == {first, second}
    assert catalog_members(app) == ()
    assert fs.filesdb.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_gui_contents_rescan_reuses_bounded_hash_rows_without_catalog_history(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    app = create_contents_app(tmp_path, (root,), monkeypatch)

    run_scan(app)
    first_rows = fs.filesdb.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    run_scan(app)
    second_rows = fs.filesdb.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    assert first_rows == second_rows == 2
    assert len(app.results.groups) == 1
    assert catalog_members(app) == ()


def test_gui_contents_scan_revalidates_rename_without_persistent_catalog(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"identical")
    second.write_bytes(b"identical")
    app = create_contents_app(tmp_path, (root,), monkeypatch)

    run_scan(app)
    renamed = root / "renamed.bin"
    second.rename(renamed)
    run_scan(app)

    assert len(app.results.groups) == 1
    assert {file.path for file in app.results.groups[0]} == {first, renamed}
    assert catalog_members(app) == ()


def test_gui_contents_scan_applies_excluded_protected_and_cross_pool_policies(tmp_path, monkeypatch):
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
    app = create_contents_app(tmp_path, (root,), monkeypatch)
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
    assert catalog_members(app) == ()


def test_gui_direct_exact_group_builds_live_quarantine_plan(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    payload = b"direct exact payload\n" * 128
    first.write_bytes(payload)
    second.write_bytes(payload)
    app = create_contents_app(tmp_path, (root,), monkeypatch)

    run_scan(app)

    [group] = app.results.groups
    assert group.evidence.algorithm == fs.HASH_ALGORITHM
    target = group.dupes[0]
    for member in group:
        proof = member.validate_review_scan()
        assert proof.content_digest_algorithm == fs.REVIEW_CONTENT_DIGEST_ALGORITHM
        assert proof.content_digest is not None
    assert evaluate_relocation(app.results, target).allowed
    assert evaluate_rename(app.results, target).allowed
    keeper_path = Path(group.ref.path)
    bound = build_bound_deletion_plan(app.results, [target], [root])
    app._do_delete(nulljob, bound)

    assert not Path(target.path).exists()
    assert keeper_path.read_bytes() == payload
    assert len(app.last_quarantine_plan_paths) == 1
    assert catalog_members(app) == ()


def test_gui_contents_scan_ignores_same_physical_hardlinks(tmp_path, monkeypatch):
    if not hasattr(os, "link"):
        return
    root = tmp_path / "library"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"same physical file")
    os.link(first, second)
    app = create_contents_app(tmp_path, (root,), monkeypatch)

    run_scan(app)

    assert app.results.groups == []
    assert catalog_members(app) == ()
