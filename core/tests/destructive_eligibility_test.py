from types import SimpleNamespace

from core import engine
from core.destructive_eligibility import (
    EligibilityCode,
    evaluate_batch,
    evaluate_duplicate,
    evaluate_relocation,
    evaluate_relocation_batch,
)
from core.results import Results
from core.scan_receipt import ScanReceipt
from core.tests.base import DupeGuru, NamedObject


def _exact_results():
    keeper = NamedObject("keeper.bin", size=4)
    target = NamedObject("target.bin", size=4)
    keeper.comparison_pool = "protected"
    target.comparison_pool = "incoming"
    keeper.validate_review_scan = lambda: object()
    target.validate_review_scan = lambda: object()
    evidence = engine.ExactEvidence(
        kind=engine.VerificationKind.VERIFIED_EXACT,
        algorithm="sha256",
        digest=b"\x00" * 32,
        size=4,
    )
    group = engine.Group.from_exact_files([keeper, target], evidence)
    results = Results(DupeGuru())
    results.groups = [group]
    results.scan_receipt = ScanReceipt.completed(2)
    return results, keeper, target


def test_only_live_complete_verified_exact_incoming_target_is_eligible():
    results, _, target = _exact_results()

    eligibility = evaluate_duplicate(results, target)

    assert eligibility.allowed
    assert eligibility.code is EligibilityCode.ELIGIBLE


def test_saved_report_is_never_a_live_proof():
    results, _, target = _exact_results()
    results.loaded_report = True

    assert evaluate_duplicate(results, target).code is EligibilityCode.SAVED_REPORT


def test_missing_or_partial_receipt_fails_closed():
    results, _, target = _exact_results()
    results.scan_receipt = SimpleNamespace(allows_destructive_actions=False)

    assert evaluate_duplicate(results, target).code is EligibilityCode.INCOMPLETE_SCAN


def test_similar_group_is_review_only():
    first = NamedObject("first.jpg")
    second = NamedObject("second.jpg")
    group = engine.Group()
    group.add_match(engine.Match(first, second, 95))
    results = Results(DupeGuru())
    results.groups = [group]
    results.scan_receipt = ScanReceipt.completed(2)

    assert evaluate_duplicate(results, second).code is EligibilityCode.NOT_VERIFIED_EXACT


def test_protected_and_compare_only_targets_are_blocked():
    results, _, target = _exact_results()
    target.comparison_pool = "compare_only"

    assert evaluate_duplicate(results, target).code is EligibilityCode.PROTECTED_POOL


def test_batch_is_all_or_nothing_at_the_gate():
    results, keeper, target = _exact_results()

    batch = evaluate_batch(results, [target, keeper])

    assert not batch.ok
    assert batch.allowed == (target,)
    assert batch.blocked[0][1].code is EligibilityCode.REFERENCE_FILE


def test_current_approximate_incoming_result_may_be_relocated_but_not_deleted():
    first = NamedObject("first.jpg")
    second = NamedObject("second.jpg")
    first.comparison_pool = "incoming"
    second.comparison_pool = "incoming"
    first.validate_review_scan = lambda: object()
    second.validate_review_scan = lambda: object()
    group = engine.Group()
    group.add_match(engine.Match(first, second, 95))
    results = Results(DupeGuru())
    results.groups = [group]
    results.scan_receipt = ScanReceipt.completed(2)

    assert evaluate_relocation(results, second).allowed
    assert evaluate_duplicate(results, second).code is EligibilityCode.NOT_VERIFIED_EXACT


def test_relocation_requires_a_stable_scan_generation_for_target_and_keeper():
    results, keeper, target = _exact_results()
    target.validate_review_scan = None

    missing = evaluate_relocation(results, target)

    assert missing.code is EligibilityCode.STALE_SCAN_CONTEXT

    target.validate_review_scan = lambda: object()

    def changed():
        raise OSError("changed")

    keeper.validate_review_scan = changed

    stale = evaluate_relocation(results, target)

    assert stale.code is EligibilityCode.STALE_SCAN_CONTEXT


def test_unknown_relationship_cannot_be_relocated():
    results, _, target = _exact_results()
    group = results.get_group_of_duplicate(target)
    group.verification_kind = engine.VerificationKind.UNVERIFIED
    group.compact_relation = None

    eligibility = evaluate_relocation(results, target)

    assert not eligibility.allowed
    assert eligibility.code is EligibilityCode.UNKNOWN_RELATION


def test_unverified_folder_manifest_relation_cannot_be_relocated():
    results, _, target = _exact_results()
    group = results.get_group_of_duplicate(target)
    group.verification_kind = engine.VerificationKind.UNVERIFIED
    group.compact_relation = "folder_manifest"

    eligibility = evaluate_relocation(results, target)

    assert not eligibility.allowed
    assert eligibility.code is EligibilityCode.UNKNOWN_RELATION


def test_relocation_batch_blocks_immutable_pools_and_incomplete_scans():
    results, _, target = _exact_results()
    target.comparison_pool = "compare_only"

    blocked_pool = evaluate_relocation_batch(results, [target])

    assert not blocked_pool.ok
    assert blocked_pool.blocked[0][1].code is EligibilityCode.PROTECTED_POOL

    target.comparison_pool = "incoming"
    results.scan_receipt = SimpleNamespace(complete=False)
    blocked_coverage = evaluate_relocation_batch(results, [target])

    assert not blocked_coverage.ok
    assert blocked_coverage.blocked[0][1].code is EligibilityCode.INCOMPLETE_SCAN
