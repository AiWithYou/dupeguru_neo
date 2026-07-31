# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import errno
import io
import os
import os.path as op
import logging
import tempfile
from types import SimpleNamespace

import pytest
from pathlib import Path
import hscommon.conflict
from hscommon.testutil import eq_, log_calls
from hscommon.jobprogress.job import Job

from core.tests.base import TestApp
from core.tests.results_test import GetTestGroups
from core import app, engine, export, fs
from core.catalog import Catalog
from core.file_generation import FileGenerationToken
import core.ignore as ignore_module
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus
from core.scanner import ScanType


def test_desktop_uses_versioned_hash_cache_without_touching_legacy_file(tmp_path, monkeypatch):
    legacy = tmp_path / "hash_cache.db"
    legacy_contents = b"legacy cache must remain untouched"
    legacy.write_bytes(legacy_contents)
    connected_paths = []

    monkeypatch.setattr(
        app.desktop,
        "special_folder_path",
        lambda _folder, portable=False: str(tmp_path),
    )
    monkeypatch.setattr(
        fs.filesdb,
        "connect",
        lambda path: connected_paths.append(Path(path)),
    )

    TestApp()

    assert connected_paths == [tmp_path / app.HASH_CACHE_FILENAME]
    assert app.HASH_CACHE_FILENAME == "hash_cache_v3.sqlite3"
    assert legacy.read_bytes() == legacy_contents


def add_fake_files_to_directories(directories, files):
    directories.get_files = lambda j=None, **_kwargs: iter(files)
    directories._dirs.append("this is just so Scan() doesn't return 3")


class TestCaseDupeGuru:
    def test_startup_reports_invalid_exclusion_list_without_replacing_state(
        self,
        tmp_path,
    ):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        dgapp.exclude_list.add("kept")
        dgapp.exclude_list.mark("kept")
        invalid_payload = b'<exclude_list><exclude regex="broken" unexpected="yes"/></exclude_list>'
        exclude_path = tmp_path / "exclude_list.xml"
        exclude_path.write_bytes(invalid_payload)
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.save()

        assert list(dgapp.exclude_list) == [(True, "kept")]
        assert "exclusion list could not be loaded" in dgapp.view.messages[-1]
        assert exclude_path.read_bytes() == invalid_payload

    def test_exclusion_list_edit_after_failed_load_replaces_invalid_source(
        self,
        tmp_path,
    ):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        exclude_path = tmp_path / "exclude_list.xml"
        exclude_path.write_bytes(b"<not_an_exclusion_list/>")
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.exclude_list.add("replacement")
        dgapp.exclude_list.mark("replacement")
        dgapp.save()

        reloaded = app.ExcludeList()
        assert reloaded.load_from_xml(exclude_path) is None
        assert list(reloaded) == [(True, "replacement")]

    def test_startup_reports_invalid_ignore_list_without_replacing_state(
        self,
        tmp_path,
    ):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        dgapp.ignore_list.ignore("kept-a", "kept-b")
        invalid_payload = b'<ignore_list><file path="broken" unexpected="yes"/></ignore_list>'
        ignore_path = tmp_path / "ignore_list.xml"
        ignore_path.write_bytes(invalid_payload)
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.save()

        assert list(dgapp.ignore_list) == [("kept-a", "kept-b")]
        assert "ignore list could not be loaded" in dgapp.view.messages[-1]
        assert ignore_path.read_bytes() == invalid_payload

    def test_startup_reports_invalid_directory_list_without_replacing_source(self, tmp_path):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        kept_root = tmp_path / "kept"
        kept_root.mkdir()
        dgapp.directories.add_path(kept_root)
        invalid_payload = b'<directories><root_directory path="relative"/></directories>'
        directories_path = tmp_path / "last_directories.xml"
        directories_path.write_bytes(invalid_payload)
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.save()

        assert list(dgapp.directories) == [kept_root]
        assert any("folder list could not be loaded" in message for message in dgapp.view.messages)
        assert directories_path.read_bytes() == invalid_payload

    def test_directory_edit_after_failed_startup_load_replaces_invalid_source(self, tmp_path):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        directories_path = tmp_path / "last_directories.xml"
        directories_path.write_bytes(b"<not_a_directory_list/>")
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.directories.add_path(replacement)
        dgapp.save()

        reloaded = app.directories.Directories()
        assert reloaded.load_from_file(directories_path) is None
        assert list(reloaded) == [replacement]

    def test_startup_and_shutdown_preserve_temporarily_offline_directory_root(self, tmp_path):
        offline_root = tmp_path / "removable-root"
        offline_root.mkdir()
        source = app.directories.Directories()
        source.add_path(offline_root)
        directories_path = tmp_path / "last_directories.xml"
        source.save_to_file(directories_path)
        original = directories_path.read_bytes()
        offline_root.rmdir()
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        dgapp.exclude_list_dialog.refresh = lambda: None

        dgapp.load()
        dgapp.save()

        assert list(dgapp.directories) == [offline_root]
        assert directories_path.read_bytes() == original

    def test_directory_save_limit_failure_preserves_existing_source(self, tmp_path, monkeypatch):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        dgapp.directories.add_path(first)
        dgapp.directories.add_path(second)
        directories_path = tmp_path / "last_directories.xml"
        original = b"existing directory selection"
        directories_path.write_bytes(original)
        monkeypatch.setattr(app.directories, "DIRECTORIES_XML_MAX_ROOTS", 1)

        dgapp.save()

        assert directories_path.read_bytes() == original
        assert "folder list was not saved" in dgapp.view.messages[-1]

    def test_load_directories_keeps_exclude_binding_and_existing_state_on_failure(self, tmp_path):
        dgapp = TestApp().app
        root = tmp_path / "root"
        root.mkdir()
        dgapp.directories.add_path(root)
        exclude_list = dgapp.exclude_list
        payload = io.BytesIO(
            b'<directories><root_directory path="partial"/>' b'<state path="bad" value="not-an-integer"/></directories>'
        )

        failure = dgapp.load_directories(payload)

        assert failure is not None
        assert list(dgapp.directories) == [root]
        assert dgapp.directories._exclude_list is exclude_list
        assert dgapp.view.messages

    def test_export_rows_are_lazy_at_100k_scale_and_late_failure_is_atomic(
        self,
        tmp_path,
    ):
        members = list(range(100_000))
        evaluated = 0

        def get_display_info(dupe, group):
            nonlocal evaluated
            evaluated += 1
            assert group is members
            if dupe == 10:
                raise RuntimeError("late row failure")
            return {"name": "file-{}.bin".format(dupe)}

        fake_app = SimpleNamespace(
            get_display_info=get_display_info,
            result_table=SimpleNamespace(
                _columns=SimpleNamespace(
                    ordered_columns=[
                        SimpleNamespace(
                            name="marked",
                            display="Marked",
                            visible=True,
                        ),
                        SimpleNamespace(
                            name="name",
                            display="Name",
                            visible=True,
                        ),
                    ]
                )
            ),
            results=SimpleNamespace(groups=[members]),
        )

        colnames, rows = app.DupeGuru._get_export_data(fake_app)

        assert colnames == ["Name"]
        assert evaluated == 0
        members.reverse()
        destination = tmp_path / "results.csv"
        destination.write_bytes(b"existing destination")
        with pytest.raises(RuntimeError, match="late row failure"):
            export.export_to_csv(destination, colnames, rows)

        assert evaluated == 11
        assert destination.read_bytes() == b"existing destination"
        assert not list(tmp_path.glob(".results.csv.*.tmp"))

    def test_apply_filter_calls_results_apply_filter(self, monkeypatch):
        dgapp = TestApp().app
        monkeypatch.setattr(dgapp.results, "apply_filter", log_calls(dgapp.results.apply_filter))
        dgapp.apply_filter("foo")
        eq_(2, len(dgapp.results.apply_filter.calls))
        call = dgapp.results.apply_filter.calls[0]
        assert call["filter_str"] is None
        call = dgapp.results.apply_filter.calls[1]
        eq_("foo", call["filter_str"])

    def test_apply_filter_escapes_regexp(self, monkeypatch):
        dgapp = TestApp().app
        monkeypatch.setattr(dgapp.results, "apply_filter", log_calls(dgapp.results.apply_filter))
        dgapp.apply_filter("()[]\\.|+?^abc")
        call = dgapp.results.apply_filter.calls[1]
        eq_("\\(\\)\\[\\]\\\\\\.\\|\\+\\?\\^abc", call["filter_str"])
        dgapp.apply_filter("(*)")  # In "simple mode", we want the * to behave as a wildcard
        call = dgapp.results.apply_filter.calls[3]
        eq_(r"\(.*\)", call["filter_str"])
        dgapp.options["escape_filter_regexp"] = False
        dgapp.apply_filter("(abc)")
        call = dgapp.results.apply_filter.calls[5]
        eq_("(abc)", call["filter_str"])

    def test_copy_or_move(self, tmpdir, monkeypatch):
        # The goal here is just to have a test for a previous blowup I had. I know my test coverage
        # for this unit is pathetic. What's done is done. My approach now is to add tests for
        # every change I want to make. The blowup was caused by a missing import.
        p = Path(str(tmpdir))
        p.joinpath("foo").touch()
        monkeypatch.setattr(
            hscommon.conflict,
            "smart_copy",
            log_calls(lambda source_path, dest_path, *, rename_no_replace, expected_source_snapshot: None),
        )
        # XXX This monkeypatch is temporary. will be fixed in a better monkeypatcher.
        monkeypatch.setattr(app, "smart_copy", hscommon.conflict.smart_copy)
        monkeypatch.setattr(os, "makedirs", lambda path: None)  # We don't want the test to create that fake directory
        dgapp = TestApp().app
        dgapp.directories.add_path(p)
        [f] = dgapp.directories.get_files()
        with tempfile.TemporaryDirectory() as tmp_dir:
            snapshot = fs.FileSnapshot.from_path_with_content_digest(f.path)
            dgapp.copy_or_move(f, True, tmp_dir, 0, snapshot)
            eq_(1, len(hscommon.conflict.smart_copy.calls))
            call = hscommon.conflict.smart_copy.calls[0]
            eq_(call["dest_path"], Path(tmp_dir, "foo"))
            eq_(call["source_path"], f.path)
            assert callable(call["rename_no_replace"])
            assert call["expected_source_snapshot"] == snapshot

    def test_copy_or_move_clean_empty_dirs(self, tmpdir, monkeypatch):
        tmppath = Path(str(tmpdir))
        sourcepath = tmppath.joinpath("source")
        sourcepath.mkdir()
        sourcepath.joinpath("myfile").touch()
        app = TestApp().app
        app.directories.add_path(tmppath)
        [myfile] = app.directories.get_files()
        monkeypatch.setattr(app, "clean_empty_dirs", log_calls(lambda path, boundary: None))
        app.copy_or_move(
            myfile,
            False,
            tmppath.joinpath("dest"),
            0,
            fs.FileSnapshot.from_path_with_content_digest(myfile.path),
        )
        calls = app.clean_empty_dirs.calls
        eq_(1, len(calls))
        eq_(sourcepath, calls[0]["path"])
        eq_(tmppath, calls[0]["boundary"])

    def test_copy_directory_into_its_own_tree_is_rejected_before_creating_destination(self, tmp_path):
        source_root = tmp_path / "root"
        source = source_root / "source"
        source.mkdir(parents=True)
        (source / "payload.bin").write_bytes(b"must remain")
        dgapp = TestApp().app
        dgapp.directories.add_path(source_root)
        destination = source / "not-created" / "nested"

        with pytest.raises(OSError) as caught:
            dgapp.copy_or_move(
                SimpleNamespace(path=source),
                True,
                destination,
                app.DestType.DIRECT,
                None,
            )

        assert caught.value.errno == errno.EINVAL
        assert (source / "payload.bin").read_bytes() == b"must remain"
        assert not (source / "not-created").exists()

    def test_move_cleanup_preserves_the_nearest_nested_selected_root(self, tmp_path):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        source_directory = inner / "source"
        source = source_directory / "payload.bin"
        destination = tmp_path / "destination"
        source_directory.mkdir(parents=True)
        source.write_bytes(b"moved payload")
        dgapp = TestApp().app
        dgapp.directories.add_path(outer)
        # Persisted/hand-edited configurations can still contain nested roots even though
        # add_path() normally de-duplicates them. The action boundary must remain safe.
        dgapp.directories._dirs.append(inner)
        dgapp.options["clean_empty_dirs"] = True

        dgapp.copy_or_move(
            SimpleNamespace(path=source),
            False,
            destination,
            app.DestType.DIRECT,
            fs.FileSnapshot.from_path_with_content_digest(source),
        )

        assert (destination / "payload.bin").read_bytes() == b"moved payload"
        assert inner.is_dir()
        assert not source_directory.exists()

    def test_post_move_cleanup_error_does_not_misreport_the_committed_move(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        source = root / "source" / "payload.bin"
        destination = tmp_path / "destination"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"moved payload")
        dgapp = TestApp().app
        dgapp.directories.add_path(root)
        dgapp.options["clean_empty_dirs"] = True

        def fail_cleanup(_path, _boundary):
            raise OSError(errno.EACCES, "simulated cleanup denial")

        monkeypatch.setattr(dgapp, "clean_empty_dirs", fail_cleanup)
        dgapp.copy_or_move(
            SimpleNamespace(path=source),
            False,
            destination,
            app.DestType.DIRECT,
            fs.FileSnapshot.from_path_with_content_digest(source),
        )

        assert not source.exists()
        assert (destination / "payload.bin").read_bytes() == b"moved payload"

    def test_scan_with_objects_evaluating_to_false(self):
        class FakeFile(fs.File):
            def __bool__(self):
                return False

        # At some point, any() was used in a wrong way that made Scan() wrongly return 1
        app = TestApp().app
        f1, f2 = (FakeFile("foo") for _ in range(2))
        f1.is_ref, f2.is_ref = (False, False)
        assert not (bool(f1) and bool(f2))
        add_fake_files_to_directories(app.directories, [f1, f2])
        app.start_scanning()  # no exception

    def test_catalog_storage_can_be_measured_and_cleared_without_touching_settings(self, tmp_path):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path)
        database_path = tmp_path / app.CATALOG_FILENAME
        Catalog(database_path).close()
        wal_path = Path(str(database_path) + "-wal")
        wal_path.write_bytes(b"rebuildable")
        settings_path = tmp_path / "settings.ini"
        settings_path.write_bytes(b"keep")
        measured = dgapp.catalog_storage_size()
        removed = dgapp.clear_catalog()

        assert removed == measured
        assert not database_path.exists()
        assert not wal_path.exists()
        assert settings_path.read_bytes() == b"keep"

    def test_incomplete_scan_without_groups_explains_that_nothing_was_published(self):
        dgapp = TestApp().app
        dgapp.results.groups = []
        dgapp.results.scan_receipt = ScanReceipt.incomplete(
            discovered=2,
            analyzed=1,
            failed=1,
            issues=(ScanIssue(code="catalog_scan_partial", message="example reason"),),
        )

        dgapp._job_completed(app.JobType.SCAN)

        message = dgapp.view.messages[-1]
        assert "no duplicate results were published" in message
        assert "No files were changed" in message
        assert "example reason" in message

    @pytest.mark.parametrize("scan_type", (ScanType.FILENAME, ScanType.CONTENTS))
    def test_direct_discovery_limit_discards_partial_input_before_scanner(
        self,
        tmp_path,
        monkeypatch,
        scan_type,
    ):
        dgapp = TestApp().app
        dgapp.appdata = str(tmp_path / "appdata")
        Path(dgapp.appdata).mkdir()
        root = tmp_path / "root"
        root.mkdir()
        dgapp.directories.add_path(root)
        dgapp.options["scan_type"] = scan_type
        dgapp.options["direct_scan_max_files"] = 1
        scanner_calls = []
        profile_calls = []

        class ProfileProbe:
            def enable(self):
                profile_calls.append("enable")

            def disable(self):
                profile_calls.append("disable")

            def dump_stats(self, path):
                profile_calls.append(("dump", Path(path).parent))

        def partial_discovery(*, fileclasses, j, budget):
            budget.count_file(root / "first.txt")
            yield SimpleNamespace(path=root / "first.txt")
            budget.count_file(root / "second.txt")

        def unexpected_scan(scanner, files, ignore_list, j):
            scanner_calls.append(tuple(files))
            raise AssertionError("partial discovery input reached the scanner")

        def run_now(jobid, function, args=()):
            function(dgapp.view.JOB, *args)

        monkeypatch.setattr(dgapp.directories, "get_files", partial_discovery)
        monkeypatch.setattr(app.se.scanner.ScannerSE, "get_dupe_groups", unexpected_scan)
        monkeypatch.setattr(dgapp, "_start_job", run_now)
        monkeypatch.setattr(app.cProfile, "Profile", ProfileProbe)

        dgapp.start_scanning(profile_scan=True)

        assert scanner_calls == []
        assert dgapp.results.groups == []
        assert dgapp.results.scan_receipt.status is ScanStatus.RESOURCE_LIMIT
        assert not dgapp.results.scan_receipt.allows_destructive_actions
        assert dgapp.results.scan_receipt.discovered == 1
        assert not (Path(dgapp.appdata) / app.CATALOG_FILENAME).exists()
        assert profile_calls == [
            "enable",
            "disable",
            ("dump", Path(dgapp.appdata)),
        ]
        dgapp._job_completed(app.JobType.SCAN)
        assert any("narrow the selected folders" in message for message in dgapp.view.messages)

    def test_direct_discovery_memory_error_is_resource_failure_not_job_crash(self, tmp_path, monkeypatch):
        dgapp = TestApp().app
        root = tmp_path / "root"
        root.mkdir()
        dgapp.directories.add_path(root)
        dgapp.options["scan_type"] = ScanType.FILENAME
        scanner_called = False

        def memory_failure(*, fileclasses, j, budget):
            raise MemoryError("synthetic exhaustion")
            yield  # pragma: no cover

        def unexpected_scan(scanner, files, ignore_list, j):
            nonlocal scanner_called
            scanner_called = True
            raise AssertionError("memory-limited discovery reached the scanner")

        def run_now(jobid, function, args=()):
            function(dgapp.view.JOB, *args)

        monkeypatch.setattr(dgapp.directories, "get_files", memory_failure)
        monkeypatch.setattr(app.se.scanner.ScannerSE, "get_dupe_groups", unexpected_scan)
        monkeypatch.setattr(dgapp, "_start_job", run_now)

        dgapp.start_scanning()

        assert not scanner_called
        assert dgapp.results.groups == []
        receipt = dgapp.results.scan_receipt
        assert receipt.status is ScanStatus.RESOURCE_LIMIT
        assert receipt.issues[0].code == "direct-discovery-resource-limit-memory"
        assert not receipt.allows_destructive_actions

    def test_direct_scan_refuses_results_when_a_file_generation_changes_during_matching(
        self,
        tmp_path,
        monkeypatch,
    ):
        dgapp = TestApp().app
        root = tmp_path / "root"
        root.mkdir()
        first = root / "same one.bin"
        second = root / "same two.bin"
        first.write_bytes(b"before")
        second.write_bytes(b"before")
        first_before = first.stat()
        dgapp.directories.add_path(root)
        dgapp.options["scan_type"] = ScanType.FILENAME
        generation = FileGenerationToken("test-fixed-generation", 1)
        monkeypatch.setattr(
            fs,
            "get_file_generation_token",
            lambda *args, **kwargs: generation,
        )
        monkeypatch.setattr(
            fs,
            "get_file_generation_token_from_fd",
            lambda *args, **kwargs: generation,
        )

        def change_during_scan(scanner, files, ignore_list, j):
            first.write_bytes(b"after!")
            os.utime(
                first,
                ns=(first_before.st_atime_ns, first_before.st_mtime_ns),
            )
            return []

        def run_now(jobid, function, args=()):
            function(dgapp.view.JOB, *args)

        monkeypatch.setattr(
            app.se.scanner.ScannerSE,
            "get_dupe_groups",
            change_during_scan,
        )
        monkeypatch.setattr(dgapp, "_start_job", run_now)

        dgapp.start_scanning()

        assert dgapp.results.groups == []
        receipt = dgapp.results.scan_receipt
        assert receipt.status is ScanStatus.FAILED
        assert receipt.issues[0].code == "scan_generation_changed"
        assert not receipt.allows_destructive_actions

    def test_direct_scan_content_proofs_report_phase_file_and_byte_progress(
        self,
        tmp_path,
        monkeypatch,
    ):
        first = tmp_path / "first.bin"
        second = tmp_path / "second.bin"
        first.write_bytes(b"abc")
        second.write_bytes(b"defgh")
        files = [fs.File(first), fs.File(second)]
        updates = []

        def report(progress, description=""):
            updates.append((progress, description))
            return True

        proof_job = Job([1, 1], report)
        monkeypatch.setattr(app, "DIRECT_PROOF_PROGRESS_BYTES", 1)

        app.DupeGuru._bind_direct_scan_generations(files, proof_job)
        app.DupeGuru._validate_direct_scan_generations(files, proof_job)

        descriptions = [description for _, description in updates if description]
        first_file_stream = next(
            description
            for description in descriptions
            if "Hashing scan-start proofs" in description and "3/8 bytes" in description
        )
        assert "0/2 files" in first_file_stream
        assert any(
            "Hashing scan-start proofs" in description and "2/2 files" in description and "8/8 bytes" in description
            for description in descriptions
        )
        assert any(
            "Confirming scan-end proofs" in description and "2/2 files" in description and "8/8 bytes" in description
            for description in descriptions
        )
        assert updates[-1][0] == 100

    def test_direct_scan_sequences_proof_matching_and_confirmation_jobs(
        self,
        tmp_path,
        monkeypatch,
    ):
        dgapp = TestApp().app
        root = tmp_path / "root"
        root.mkdir()
        (root / "same one.bin").write_bytes(b"payload")
        (root / "same two.bin").write_bytes(b"payload")
        dgapp.directories.add_path(root)
        dgapp.options["scan_type"] = ScanType.FILENAME
        updates = []

        def report(progress, description=""):
            updates.append((progress, description))
            return True

        def run_now(jobid, function, args=()):
            function(Job(1, report), *args)

        monkeypatch.setattr(dgapp, "_start_job", run_now)

        dgapp.start_scanning()

        descriptions = [description for _, description in updates if description]
        assert any("Hashing scan-start proofs" in value for value in descriptions)
        assert any("Matching files" in value for value in descriptions)
        assert any("Confirming scan-end proofs" in value for value in descriptions)
        assert updates[-1][0] == 100
        assert dgapp.results.scan_receipt.complete

    @pytest.mark.parametrize(
        ("device", "inode"),
        (
            (0, 17),
            (23, 0),
            (0, 0),
        ),
    )
    def test_unknown_zero_hardlink_identity_never_discards_distinct_files(self, device, inode):
        class PathProbe:
            def stat(self, *, follow_symlinks):
                assert not follow_symlinks
                return SimpleNamespace(st_dev=device, st_ino=inode)

        files = [
            SimpleNamespace(path=PathProbe()),
            SimpleNamespace(path=PathProbe()),
        ]

        assert app.DupeGuru._remove_hardlink_dupes(files) == files

    def test_hardlink_filter_stat_remains_inside_discovery_time_budget(self):
        now = [0.0]

        class SlowPath:
            def stat(self, *, follow_symlinks):
                assert not follow_symlinks
                now[0] = 2.0
                return SimpleNamespace(st_dev=1, st_ino=1)

        budget = app.directories.DirectDiscoveryBudget(
            app.directories.DirectDiscoveryLimits(
                max_files=10,
                max_folders=10,
                max_issues=10,
                max_seconds=1,
            ),
            clock=lambda: now[0],
        )

        with pytest.raises(app.directories.DirectDiscoveryResourceError) as caught:
            app.DupeGuru._remove_hardlink_dupes(
                [SimpleNamespace(path=SlowPath())],
                budget=budget,
            )

        assert caught.value.code == "resource-limit-seconds"

    @pytest.mark.skipif("not hasattr(os, 'link')")
    def test_ignore_hardlink_matches(self, tmpdir):
        # If the ignore_hardlink_matches option is set, don't match files hardlinking to the same
        # inode.
        tmppath = Path(str(tmpdir))
        tmppath.joinpath("myfile").open("wt").write("foo")
        os.link(str(tmppath.joinpath("myfile")), str(tmppath.joinpath("hardlink")))
        app = TestApp().app
        app.directories.add_path(tmppath)
        app.options["scan_type"] = ScanType.CONTENTS
        app.options["ignore_hardlink_matches"] = True
        app.start_scanning()
        eq_(len(app.results.groups), 0)

    def test_rename_when_nothing_is_selected(self):
        # Issue #140
        # It's possible that rename operation has its selected row swept off from under it, thus
        # making the selected row None. Don't crash when it happens.
        dgapp = TestApp().app
        # selected_row is None because there's no result.
        assert not dgapp.result_table.rename_selected("foo")  # no crash


class TestCaseDupeGuruCleanEmptyDirs:
    @pytest.fixture
    def do_setup(self):
        self.app = TestApp().app

    def test_option_off_does_not_validate_or_remove(self, do_setup, tmp_path):
        root = tmp_path / "root"
        child = root / "child"
        child.mkdir(parents=True)
        self.app.clean_empty_dirs(child, root)
        assert child.is_dir()

    def test_option_on_removes_only_below_selected_root(self, do_setup, tmp_path):
        root = tmp_path / "root"
        child = root / "one" / "two"
        child.mkdir(parents=True)
        self.app.options["clean_empty_dirs"] = True
        self.app.clean_empty_dirs(child, root)
        assert root.is_dir()
        assert not (root / "one").exists()

    def test_ds_store_is_never_unlinked(self, do_setup, tmp_path):
        root = tmp_path / "root"
        child = root / "child"
        child.mkdir(parents=True)
        marker = child / ".DS_Store"
        marker.write_bytes(b"must remain")
        self.app.options["clean_empty_dirs"] = True
        self.app.clean_empty_dirs(child, root)
        assert marker.read_bytes() == b"must remain"
        assert child.is_dir()

    def test_outside_or_relative_cleanup_range_is_rejected(self, do_setup, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        self.app.options["clean_empty_dirs"] = True
        with pytest.raises(OSError):
            self.app.clean_empty_dirs(outside, root)
        with pytest.raises(OSError):
            self.app.clean_empty_dirs(Path("relative"), root)
        assert root.is_dir()
        assert outside.is_dir()


class TestCaseDupeGuruWithResults:
    @pytest.fixture
    def do_setup(self, request):
        app = TestApp()
        self.app = app.app
        self.objects, self.matches, self.groups = GetTestGroups()
        self.app.results.groups = self.groups
        self.dpanel = app.dpanel
        self.dtree = app.dtree
        self.rtable = app.rtable
        self.rtable.refresh()
        tmpdir = request.getfixturevalue("tmpdir")
        tmppath = Path(str(tmpdir))
        tmppath.joinpath("foo").mkdir()
        tmppath.joinpath("bar").mkdir()
        self.app.directories.add_path(tmppath)

    def test_get_objects(self, do_setup):
        objects = self.objects
        groups = self.groups
        r = self.rtable[0]
        assert r._group is groups[0]
        assert r._dupe is objects[0]
        r = self.rtable[1]
        assert r._group is groups[0]
        assert r._dupe is objects[1]
        r = self.rtable[4]
        assert r._group is groups[1]
        assert r._dupe is objects[4]

    def test_save_as_reports_invalid_xml_text_and_preserves_destination(self, do_setup, tmp_path):
        destination = tmp_path / "results.xml"
        original = b"existing results"
        destination.write_bytes(original)
        self.objects[0]._folder = Path("invalid\0folder")

        self.app.save_as(destination)

        assert destination.read_bytes() == original
        assert "Couldn't write to file" in self.app.view.messages[-1]
        assert "XML 1.0" in self.app.view.messages[-1]

    def test_save_as_reports_bounded_preflight_and_preserves_destination(
        self,
        do_setup,
        tmp_path,
        monkeypatch,
    ):
        destination = tmp_path / "results.xml"
        original = b"existing results"
        destination.write_bytes(original)
        monkeypatch.setattr(app.results, "MAX_RESULTS_GROUPS", 1)

        self.app.save_as(destination)

        assert destination.read_bytes() == original
        assert "Couldn't write to file" in self.app.view.messages[-1]
        assert "group count" in self.app.view.messages[-1]

    def test_get_objects_after_sort(self, do_setup):
        objects = self.objects
        groups = self.groups[:]  # we need an un-sorted reference
        self.rtable.sort("name", False)
        r = self.rtable[1]
        assert r._group is groups[1]
        assert r._dupe is objects[4]

    def test_selected_result_node_paths_after_deletion(self, do_setup):
        # cases where the selected dupes aren't there are correctly handled
        self.rtable.select([1, 2, 3])
        self.app.remove_selected()
        # The first 2 dupes have been removed. The 3rd one is a ref. it stays there, in first pos.
        eq_(self.rtable.selected_indexes, [1])  # no exception

    def test_select_result_node_paths(self, do_setup):
        app = self.app
        objects = self.objects
        self.rtable.select([1, 2])
        eq_(len(app.selected_dupes), 2)
        assert app.selected_dupes[0] is objects[1]
        assert app.selected_dupes[1] is objects[2]

    def test_select_result_node_paths_with_ref(self, do_setup):
        app = self.app
        objects = self.objects
        self.rtable.select([1, 2, 3])
        eq_(len(app.selected_dupes), 3)
        assert app.selected_dupes[0] is objects[1]
        assert app.selected_dupes[1] is objects[2]
        assert app.selected_dupes[2] is self.groups[1].ref

    def test_select_result_node_paths_after_sort(self, do_setup):
        app = self.app
        objects = self.objects
        groups = self.groups[:]  # To keep the old order in memory
        self.rtable.sort("name", False)  # 0
        # Now, the group order is supposed to be reversed
        self.rtable.select([1, 2, 3])
        eq_(len(app.selected_dupes), 3)
        assert app.selected_dupes[0] is objects[4]
        assert app.selected_dupes[1] is groups[0].ref
        assert app.selected_dupes[2] is objects[1]

    def test_selected_powermarker_node_paths(self, do_setup):
        # app.selected_dupes is correctly converted into paths
        self.rtable.power_marker = True
        self.rtable.select([0, 1, 2])
        self.rtable.power_marker = False
        eq_(self.rtable.selected_indexes, [1, 2, 4])

    def test_selected_powermarker_node_paths_after_deletion(self, do_setup):
        # cases where the selected dupes aren't there are correctly handled
        app = self.app
        self.rtable.power_marker = True
        self.rtable.select([0, 1, 2])
        app.remove_selected()
        eq_(self.rtable.selected_indexes, [])  # no exception

    def test_select_powermarker_rows_after_sort(self, do_setup):
        app = self.app
        objects = self.objects
        self.rtable.power_marker = True
        self.rtable.sort("name", False)
        self.rtable.select([0, 1, 2])
        eq_(len(app.selected_dupes), 3)
        assert app.selected_dupes[0] is objects[4]
        assert app.selected_dupes[1] is objects[2]
        assert app.selected_dupes[2] is objects[1]

    def test_toggle_selected_mark_state(self, do_setup):
        app = self.app
        objects = self.objects
        app.toggle_selected_mark_state()
        eq_(app.results.mark_count, 0)
        self.rtable.select([1, 4])
        app.toggle_selected_mark_state()
        eq_(app.results.mark_count, 2)
        assert not app.results.is_marked(objects[0])
        assert app.results.is_marked(objects[1])
        assert not app.results.is_marked(objects[2])
        assert not app.results.is_marked(objects[3])
        assert app.results.is_marked(objects[4])

    def test_toggle_selected_mark_state_with_different_selected_state(self, do_setup):
        # When marking selected dupes with a heterogenous selection, mark all selected dupes. When
        # it's homogenous, simply toggle.
        app = self.app
        self.rtable.select([1])
        app.toggle_selected_mark_state()
        # index 0 is unmarkable, but we throw it in the bunch to be sure that it doesn't make the
        # selection heterogenoug when it shouldn't.
        self.rtable.select([0, 1, 4])
        app.toggle_selected_mark_state()
        eq_(app.results.mark_count, 2)
        app.toggle_selected_mark_state()
        eq_(app.results.mark_count, 0)

    def test_refresh_details_with_selected(self, do_setup):
        self.rtable.select([1, 4])
        eq_(self.dpanel.row(0), ("Filename", "bar bleh", "foo bar"))
        self.dpanel.view.check_gui_calls(["refresh"])
        self.rtable.select([])
        eq_(self.dpanel.row(0), ("Filename", "---", "---"))
        self.dpanel.view.check_gui_calls(["refresh"])

    def test_make_selected_reference(self, do_setup):
        app = self.app
        objects = self.objects
        groups = self.groups
        self.rtable.select([1, 4])
        app.make_selected_reference()
        assert groups[0].ref is objects[1]
        assert groups[1].ref is objects[4]

    def test_make_selected_reference_by_selecting_two_dupes_in_the_same_group(self, do_setup):
        app = self.app
        objects = self.objects
        groups = self.groups
        self.rtable.select([1, 2, 4])
        # Only [0, 0] and [1, 0] must go ref, not [0, 1] because it is a part of the same group
        app.make_selected_reference()
        assert groups[0].ref is objects[1]
        assert groups[1].ref is objects[4]

    def test_remove_selected(self, do_setup):
        app = self.app
        self.rtable.select([1, 4])
        app.remove_selected()
        eq_(len(app.results.dupes), 1)  # the first path is now selected
        app.remove_selected()
        eq_(len(app.results.dupes), 0)

    def test_add_directory_simple(self, do_setup):
        # There's already a directory in self.app, so adding another once makes 2 of em
        app = self.app
        # any other path that isn't a parent or child of the already added path
        otherpath = Path(op.dirname(__file__))
        app.add_directory(otherpath)
        eq_(len(app.directories), 2)

    def test_add_directory_already_there(self, do_setup):
        app = self.app
        otherpath = Path(op.dirname(__file__))
        app.add_directory(otherpath)
        app.add_directory(otherpath)
        eq_(len(app.view.messages), 1)
        assert "already" in app.view.messages[0]

    def test_add_directory_does_not_exist(self, do_setup):
        app = self.app
        app.add_directory("/does_not_exist")
        eq_(len(app.view.messages), 1)
        assert "exist" in app.view.messages[0]

    def test_ignore(self, do_setup):
        app = self.app
        # The synthetic fixture gives both members the same path by default,
        # which a real scan removes before grouping and which cannot form a
        # persistent path-pair relationship.
        self.objects[4]._folder = Path("other-basepath")
        self.rtable.select([4])  # The dupe of the second, 2 sized group
        app.add_selected_to_ignore_list()
        assert len(app.ignore_list) == 1, app.view.messages
        self.rtable.select([1])  # first dupe of the 3 dupes group
        app.add_selected_to_ignore_list()
        # BOTH the ref and the other dupe should have been added
        eq_(len(app.ignore_list), 3)

    def test_ignore_selection_over_limit_is_atomic(self, do_setup, monkeypatch):
        app = self.app
        self.rtable.select([1])
        selected_before = tuple(app.selected_dupes)
        result_count_before = len(app.results.dupes)
        monkeypatch.setattr(ignore_module, "IGNORE_XML_MAX_EDGES", 1)

        app.add_selected_to_ignore_list()

        assert len(app.ignore_list) == 0
        assert tuple(app.selected_dupes) == selected_before
        assert len(app.results.dupes) == result_count_before
        assert "safety limits" in app.view.messages[-1]

    def test_purge_ignorelist(self, do_setup, tmpdir):
        app = self.app
        p1 = str(tmpdir.join("file1"))
        p2 = str(tmpdir.join("file2"))
        open(p1, "w").close()
        open(p2, "w").close()
        dne = "/does_not_exist"
        app.ignore_list.ignore(dne, p1)
        app.ignore_list.ignore(p2, dne)
        app.ignore_list.ignore(p1, p2)
        app.purge_ignore_list()
        eq_(1, len(app.ignore_list))
        assert app.ignore_list.are_ignored(p1, p2)
        assert not app.ignore_list.are_ignored(dne, p1)

    def test_only_unicode_is_added_to_ignore_list(self, do_setup):
        def fake_ignore(first, second):
            if not isinstance(first, str):
                self.fail()
            if not isinstance(second, str):
                self.fail()

        app = self.app
        app.ignore_list.ignore = fake_ignore
        self.rtable.select([4])
        app.add_selected_to_ignore_list()

    def test_cancel_scan_with_previous_results(self, do_setup):
        # When doing a scan with results being present prior to the scan, correctly invalidate the
        # results table.
        app = self.app
        app.JOB = Job(1, lambda *args, **kw: False)  # Cancels the task
        add_fake_files_to_directories(app.directories, self.objects)  # We want the scan to at least start
        app.start_scanning()  # will be cancelled immediately
        eq_(len(app.result_table), 0)

    def test_selected_dupes_after_removal(self, do_setup):
        # Purge the app's `selected_dupes` attribute when removing dupes, or else it might cause a
        # crash later with None refs.
        app = self.app
        app.results.mark_all()
        self.rtable.select([0, 1, 2, 3, 4])
        app.remove_marked()
        eq_(len(self.rtable), 0)
        eq_(app.selected_dupes, [])

    def test_dont_crash_on_delta_powermarker_dupecount_sort(self, do_setup):
        # Don't crash when sorting by dupe count or percentage while delta+powermarker are enabled.
        # Ref #238
        self.rtable.delta_values = True
        self.rtable.power_marker = True
        self.rtable.sort("dupe_count", False)
        # don't crash
        self.rtable.sort("percentage", False)
        # don't crash


class TestCaseDupeGuruRenameSelected:
    @pytest.fixture
    def do_setup(self, request):
        tmpdir = request.getfixturevalue("tmpdir")
        p = Path(str(tmpdir))
        p.joinpath("foo bar 1").touch()
        p.joinpath("foo bar 2").touch()
        p.joinpath("foo bar 3").touch()
        files = fs.get_files(p)
        for f in files:
            f.is_ref = False
            f.begin_review_scan()
        matches = engine.getmatches(files)
        groups = engine.get_groups(matches)
        g = groups[0]
        g.prioritize(lambda x: x.name)
        app = TestApp()
        app.app.results.groups = groups
        self.app = app.app
        self.app.directories.add_path(p)
        self.app.results.scan_receipt = ScanReceipt.completed(len(files))
        self.rtable = app.rtable
        self.rtable.refresh()
        self.groups = groups
        self.p = p
        self.files = files

    def test_simple(self, do_setup):
        app = self.app
        g = self.groups[0]
        self.rtable.select([1])
        assert app.rename_selected("renamed")
        names = [p.name for p in self.p.glob("*")]
        assert "renamed" in names
        assert "foo bar 2" not in names
        eq_(g.dupes[0].name, "renamed")

    def test_none_selected(self, do_setup, monkeypatch):
        app = self.app
        g = self.groups[0]
        self.rtable.select([])
        monkeypatch.setattr(logging, "warning", log_calls(lambda msg: None))
        assert not app.rename_selected("renamed")
        msg = logging.warning.calls[0]["msg"]
        eq_("dupeGuru Warning: list index out of range", msg)
        names = [p.name for p in self.p.glob("*")]
        assert "renamed" not in names
        assert "foo bar 2" in names
        eq_(g.dupes[0].name, "foo bar 2")

    def test_name_already_exists(self, do_setup, monkeypatch):
        app = self.app
        g = self.groups[0]
        self.rtable.select([1])
        monkeypatch.setattr(logging, "warning", log_calls(lambda msg: None))
        assert not app.rename_selected("foo bar 1")
        msg = logging.warning.calls[0]["msg"]
        assert msg.startswith("dupeGuru Warning: 'foo bar 1' already exists in")
        names = [p.name for p in self.p.glob("*")]
        assert "foo bar 1" in names
        assert "foo bar 2" in names
        eq_(g.dupes[0].name, "foo bar 2")


class TestAppWithDirectoriesInTree:
    @pytest.fixture
    def do_setup(self, request):
        tmpdir = request.getfixturevalue("tmpdir")
        p = Path(str(tmpdir))
        p.joinpath("sub1").mkdir()
        p.joinpath("sub2").mkdir()
        p.joinpath("sub3").mkdir()
        app = TestApp()
        self.app = app.app
        self.dtree = app.dtree
        self.dtree.add_directory(p)
        self.dtree.view.clear_calls()

    def test_set_root_as_ref_makes_subfolders_ref_as_well(self, do_setup):
        # Setting a node state to something also affect subnodes. These subnodes must be correctly
        # refreshed.
        node = self.dtree[0]
        eq_(len(node), 3)  # a len() call is required for subnodes to be loaded
        node.state = 1  # the state property is a state index
        node = self.dtree[0]
        eq_(len(node), 3)
        subnode = node[0]
        eq_(subnode.state, 1)
        self.dtree.view.check_gui_calls(["refresh_states"])
