import io
import itertools
import json
import math
import subprocess
import sys

import pytest

from core import fs as core_fs
from core.cli import ExitCode, main
from core.services import (
    DEFAULT_SCAN_MAX_FILES,
    DEFAULT_SCAN_MAX_GROUPS,
    DEFAULT_SCAN_MAX_ISSUES,
    DEFAULT_SCAN_MAX_SECONDS,
    PlanService,
    ScanRequest,
    ScanService,
)
from core.services import adapters as adapter_module
from core.services.jsonio import iter_scan_jsonl, load_scan_report


def _write_files(root, contents):
    paths = []
    for index, content in enumerate(contents):
        path = root / "{:04d}.bin".format(index)
        path.write_bytes(content)
        paths.append(path)
    return paths


def test_scan_request_has_finite_positive_public_resource_limits():
    request = ScanRequest(roots=("library",))

    assert request.max_files == DEFAULT_SCAN_MAX_FILES == 1_000_000
    assert request.max_issues == DEFAULT_SCAN_MAX_ISSUES == 100_000
    assert request.max_groups == DEFAULT_SCAN_MAX_GROUPS == 250_000
    assert request.max_seconds == DEFAULT_SCAN_MAX_SECONDS == 14_400

    for name in ("max_files", "max_issues", "max_groups"):
        for value in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match=name):
                ScanRequest(roots=("library",), **{name: value})
    for value in (0, -1, True, math.inf, math.nan):
        with pytest.raises(ValueError, match="max_seconds"):
            ScanRequest(roots=("library",), max_seconds=value)


def test_max_files_stops_discovery_without_materializing_the_extra_file(tmp_path):
    _write_files(tmp_path, (b"a", b"b", b"c"))

    report = ScanService().scan(
        ScanRequest(
            roots=(str(tmp_path),),
            max_files=2,
        )
    )

    assert report.summary.discovered_files == 2
    assert report.groups == ()
    assert not report.summary.complete
    assert any(issue.code == "resource-limit-files" for issue in report.issues)
    assert not report.coverage[0].complete
    with pytest.raises(ValueError, match="incomplete"):
        PlanService().create(report)

    loaded = load_scan_report(io.StringIO("".join(iter_scan_jsonl(report))))
    assert loaded.to_dict() == report.to_dict()


def test_max_issues_is_a_hard_storage_cap_with_explicit_partial_reason(tmp_path):
    missing_roots = tuple(str(tmp_path / "missing-{}".format(index)) for index in range(3))

    report = ScanService().scan(
        ScanRequest(
            roots=missing_roots,
            max_issues=1,
        )
    )

    assert len(report.issues) == 1
    assert report.issues[0].code == "resource-limit-issues"
    assert report.summary.issues == 1
    assert not report.summary.complete
    assert len(report.coverage) == len(missing_roots)
    assert all(not coverage.complete for coverage in report.coverage)


def test_max_groups_is_applied_before_generating_another_digest_bucket(
    tmp_path,
    monkeypatch,
):
    contents = tuple(content for marker in (b"a", b"b", b"c") for content in (marker * 32, marker * 32))
    _write_files(tmp_path, contents)
    calls = []
    original = adapter_module.engine.getgroups_by_contents

    def recording_engine(files, *args, **kwargs):
        materialized = list(files)
        calls.append(len(materialized))
        return original(materialized, *args, **kwargs)

    monkeypatch.setattr(
        adapter_module.engine,
        "getgroups_by_contents",
        recording_engine,
    )

    report = ScanService().scan(
        ScanRequest(
            roots=(str(tmp_path),),
            max_groups=1,
        )
    )

    assert calls == [2]
    assert len(report.groups) == 1
    assert report.summary.verified_groups == 1
    assert report.issues[-1].code == "resource-limit-groups"
    assert not report.summary.complete
    with pytest.raises(ValueError, match="incomplete"):
        PlanService().create(report)


def test_max_seconds_returns_schema_valid_partial_coverage_before_work(
    tmp_path,
    monkeypatch,
):
    _write_files(tmp_path, (b"same", b"same"))
    clock = itertools.chain((0.0, 2.0), itertools.repeat(2.0))
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: next(clock))

    report = ScanService().scan(
        ScanRequest(
            roots=(str(tmp_path),),
            max_seconds=1,
        )
    )

    assert report.summary.discovered_files == 0
    assert report.groups == ()
    assert report.issues[0].code == "resource-limit-seconds"
    assert not report.summary.complete
    assert len(report.coverage) == 1
    assert not report.coverage[0].complete
    loaded = load_scan_report(io.StringIO(json.dumps(report.to_dict())))
    assert loaded.to_dict() == report.to_dict()


def test_max_seconds_interrupts_full_hash_between_bounded_chunks(
    tmp_path,
    monkeypatch,
):
    _write_files(tmp_path, (b"a" * (2 * core_fs.CHUNK_SIZE),) * 2)
    hashing_started = False
    stop_checks = []
    original = core_fs.File._calc_digest_with_snapshot

    def expiring_hash(file, stop_check=None):
        nonlocal hashing_started
        hashing_started = True
        stop_checks.append(stop_check)
        return original(file, stop_check=stop_check)

    monkeypatch.setattr(core_fs.File, "_calc_digest_with_snapshot", expiring_hash)
    monkeypatch.setattr(
        adapter_module.time,
        "monotonic",
        lambda: 2.0 if hashing_started else 0.0,
    )

    report = ScanService().scan(
        ScanRequest(
            roots=(str(tmp_path),),
            max_seconds=1,
        )
    )

    assert stop_checks and stop_checks[0] is not None
    assert report.groups == ()
    assert report.issues[-1].code == "resource-limit-seconds"
    assert not report.summary.complete


def test_max_seconds_interrupts_final_byte_comparison_between_bounded_chunks(
    tmp_path,
    monkeypatch,
):
    _write_files(tmp_path, (b"a" * (2 * core_fs.CHUNK_SIZE),) * 2)
    comparing_started = False
    stop_checks = []
    original = core_fs.File.compare_bytes_interruptible

    def expiring_compare(file, other, stop_check, *, compute_sha256=False):
        nonlocal comparing_started
        comparing_started = True
        stop_checks.append(stop_check)
        return original(
            file,
            other,
            stop_check,
            compute_sha256=compute_sha256,
        )

    monkeypatch.setattr(
        core_fs.File,
        "compare_bytes_interruptible",
        expiring_compare,
    )
    monkeypatch.setattr(
        adapter_module.time,
        "monotonic",
        lambda: 2.0 if comparing_started else 0.0,
    )

    report = ScanService().scan(
        ScanRequest(
            roots=(str(tmp_path),),
            max_seconds=1,
        )
    )

    assert stop_checks and stop_checks[0] is not None
    assert report.groups == ()
    assert report.issues[-1].code == "resource-limit-seconds"
    assert not report.summary.complete


def test_cli_scan_limits_emit_partial_report_and_invalid_limits_are_input_errors(
    tmp_path,
):
    _write_files(tmp_path, (b"a", b"b"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "scan",
            str(tmp_path),
            "--max-files",
            "1",
            "--max-issues",
            "2",
            "--max-groups",
            "1",
            "--max-seconds",
            "60",
            "--quiet",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    report = load_scan_report(io.StringIO(stdout.getvalue()))
    assert exit_code == ExitCode.PARTIAL_SCAN
    assert report.issues[0].code == "resource-limit-files"
    assert not report.summary.complete
    assert stderr.getvalue() == ""

    invalid_stdout = io.StringIO()
    invalid_stderr = io.StringIO()
    invalid_exit = main(
        ["scan", str(tmp_path), "--max-files", "0"],
        stdout=invalid_stdout,
        stderr=invalid_stderr,
    )
    assert invalid_exit == ExitCode.INPUT_ERROR
    assert invalid_stdout.getvalue() == ""
    assert "max_files must be a positive integer" in invalid_stderr.getvalue()


def test_scan_help_documents_every_default_resource_limit():
    completed = subprocess.run(
        [sys.executable, "-m", "core.cli", "scan", "--help"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == ExitCode.OK
    assert completed.stderr == ""
    for option in ("--max-files", "--max-issues", "--max-groups", "--max-seconds"):
        assert option in completed.stdout
    for value in (
        DEFAULT_SCAN_MAX_FILES,
        DEFAULT_SCAN_MAX_ISSUES,
        DEFAULT_SCAN_MAX_GROUPS,
        DEFAULT_SCAN_MAX_SECONDS,
    ):
        assert str(value) in completed.stdout
    assert "cannot be used to create a destructive plan" in " ".join(completed.stdout.split())
