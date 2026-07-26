from types import SimpleNamespace

import pytest

from core.scan_receipt import (
    ScanIssue,
    ScanReceipt,
    ScanStatus,
    receipt_from_walk_coverages,
)


def test_complete_receipt_is_the_only_destructive_state():
    complete = ScanReceipt.completed(3)
    partial = ScanReceipt.incomplete(
        discovered=3,
        analyzed=2,
        skipped=1,
        issues=(ScanIssue("decode", "one input failed"),),
    )

    assert complete.complete
    assert complete.allows_destructive_actions
    assert not partial.complete
    assert not partial.allows_destructive_actions


def test_accounting_cannot_exceed_discovered_inputs():
    with pytest.raises(ValueError):
        ScanReceipt(
            scan_id="scan",
            status=ScanStatus.COMPLETE_WITH_SKIPS,
            discovered=1,
            analyzed=1,
            skipped=1,
        )


def test_walk_coverage_errors_are_not_silently_complete():
    coverage = SimpleNamespace(
        complete=False,
        errors=1,
        skipped_symlinks=1,
        skipped_reparse_points=0,
        skipped_mounts=0,
        skipped_cycles=0,
        skipped_outside_root=0,
        skipped_special_files=0,
        skipped_changed_directories=0,
    )

    receipt = receipt_from_walk_coverages(5, [coverage])

    assert receipt.status is ScanStatus.COMPLETE_WITH_SKIPS
    assert receipt.discovered == 7
    assert receipt.analyzed == 5
    assert receipt.skipped == 1
    assert receipt.failed == 1
