# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Pure safety gates evaluated before building live filesystem action plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional, Tuple

from core.engine import VerificationKind
from hscommon.jobprogress.job import JobCancelled


class EligibilityCode(str, Enum):
    ELIGIBLE = "eligible"
    SAVED_REPORT = "saved_report"
    INCOMPLETE_SCAN = "incomplete_scan"
    NOT_VERIFIED_EXACT = "not_verified_exact"
    REFERENCE_FILE = "reference_file"
    PROTECTED_POOL = "protected_pool"
    MISSING_KEEPER = "missing_keeper"
    UNKNOWN_RELATION = "unknown_relation"
    STALE_SCAN_CONTEXT = "stale_scan_context"


@dataclass(frozen=True)
class Eligibility:
    code: EligibilityCode
    message: str

    @property
    def allowed(self) -> bool:
        return self.code is EligibilityCode.ELIGIBLE


@dataclass(frozen=True)
class BatchEligibility:
    allowed: Tuple[object, ...]
    blocked: Tuple[Tuple[object, Eligibility], ...]

    @property
    def ok(self) -> bool:
        return bool(self.allowed) and not self.blocked


def _evaluate_current_context(
    results,
    dupe,
    current_pool_resolver: Optional[Callable[[object], str]],
) -> Eligibility:
    if current_pool_resolver is None:
        return Eligibility(EligibilityCode.ELIGIBLE, "Current directory policy was not requested.")
    group = results.get_group_of_duplicate(dupe)
    if group is None or group.ref is None:
        return Eligibility(EligibilityCode.MISSING_KEEPER, "No keeper is available for this candidate.")
    for candidate in (dupe, group.ref):
        expected_pool = getattr(candidate, "comparison_pool", "incoming")
        try:
            current_pool = current_pool_resolver(candidate.path)
        except Exception:
            current_pool = "unavailable"
        if current_pool == "excluded" or current_pool != expected_pool:
            return Eligibility(
                EligibilityCode.STALE_SCAN_CONTEXT,
                (
                    "Directory pools or exclusion filters changed after this scan. "
                    "Run a new scan before changing files."
                ),
            )
    return Eligibility(EligibilityCode.ELIGIBLE, "Current directory policy still matches the scan.")


def _evaluate_review_generations(dupe, group) -> Eligibility:
    """Require both sides of the reviewed relationship to remain scan-bound."""

    for candidate in (dupe, group.ref):
        validate = getattr(candidate, "validate_review_scan", None)
        if not callable(validate):
            return Eligibility(
                EligibilityCode.STALE_SCAN_CONTEXT,
                "The result has no stable file-generation baseline. Run a new scan before organizing files.",
            )
        try:
            validate()
        except Exception:
            return Eligibility(
                EligibilityCode.STALE_SCAN_CONTEXT,
                "A result file changed after this scan. Run a new scan before organizing files.",
            )
    return Eligibility(EligibilityCode.ELIGIBLE, "Result file generations still match the scan.")


def _evaluate_review_contents(
    dupe,
    group,
    *,
    defer_target: bool,
    stop_check=None,
    progress_callback=None,
) -> Eligibility:
    """Require current review bytes, except a target proven by its executor."""

    candidates = (group.ref,) if defer_target else (dupe, group.ref)
    for candidate in candidates:
        validate = getattr(candidate, "validate_review_scan_content", None)
        if not callable(validate):
            return Eligibility(
                EligibilityCode.STALE_SCAN_CONTEXT,
                "A result has no live content proof. Run a new scan before organizing files.",
            )
        try:
            validate(
                stop_check=stop_check,
                progress_callback=progress_callback,
            )
        except (InterruptedError, JobCancelled):
            raise
        except Exception:
            return Eligibility(
                EligibilityCode.STALE_SCAN_CONTEXT,
                "A result file changed after this scan. Run a new scan before organizing files.",
            )
    return Eligibility(EligibilityCode.ELIGIBLE, "Result file bytes still match the scan.")


def evaluate_duplicate(
    results,
    dupe,
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> Eligibility:
    if getattr(results, "loaded_report", False):
        return Eligibility(
            EligibilityCode.SAVED_REPORT,
            "Saved results are historical reports; run a new scan before changing files.",
        )
    receipt = getattr(results, "scan_receipt", None)
    if receipt is None or not receipt.allows_destructive_actions:
        return Eligibility(
            EligibilityCode.INCOMPLETE_SCAN,
            "The scan has incomplete or missing coverage evidence.",
        )
    group = results.get_group_of_duplicate(dupe)
    if group is None or group.ref is None:
        return Eligibility(EligibilityCode.MISSING_KEEPER, "No keeper is available for this candidate.")
    if getattr(group, "verification_kind", VerificationKind.UNVERIFIED) is not VerificationKind.VERIFIED_EXACT:
        return Eligibility(
            EligibilityCode.NOT_VERIFIED_EXACT,
            "Only byte-verified exact duplicates can be changed in bulk.",
        )
    if dupe is group.ref or bool(getattr(dupe, "is_ref", False)):
        return Eligibility(EligibilityCode.REFERENCE_FILE, "The selected file is a protected keeper.")
    comparison_pool = getattr(dupe, "comparison_pool", "incoming")
    if comparison_pool != "incoming":
        return Eligibility(
            EligibilityCode.PROTECTED_POOL,
            "Files in protected or compare-only pools cannot be changed.",
        )
    current_context = _evaluate_current_context(results, dupe, current_pool_resolver)
    if not current_context.allowed:
        return current_context
    return Eligibility(EligibilityCode.ELIGIBLE, "Live proof can be built for this exact duplicate.")


def evaluate_batch(
    results,
    dupes: Iterable[object],
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> BatchEligibility:
    allowed = []
    blocked = []
    seen = set()
    for dupe in dupes:
        if id(dupe) in seen:
            continue
        seen.add(id(dupe))
        eligibility = evaluate_duplicate(results, dupe, current_pool_resolver)
        if eligibility.allowed:
            allowed.append(dupe)
        else:
            blocked.append((dupe, eligibility))
    return BatchEligibility(tuple(allowed), tuple(blocked))


def evaluate_relocation(
    results,
    dupe,
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> Eligibility:
    """Gate explicit organizer copies and moves without requiring exact evidence.

    Copying or moving preserves the selected payload at a destination, so
    approximate results may be organized. Historical/incomplete results and
    immutable source pools still cannot become organizer inputs.
    """

    if getattr(results, "loaded_report", False):
        return Eligibility(
            EligibilityCode.SAVED_REPORT,
            "Saved results are historical reports; run a new scan before organizing files.",
        )
    receipt = getattr(results, "scan_receipt", None)
    if receipt is None or not receipt.complete:
        return Eligibility(
            EligibilityCode.INCOMPLETE_SCAN,
            "The scan has incomplete or missing coverage evidence.",
        )
    group = results.get_group_of_duplicate(dupe)
    if group is None or group.ref is None:
        return Eligibility(EligibilityCode.MISSING_KEEPER, "The selected file is no longer in a result group.")
    verification_kind = getattr(group, "verification_kind", VerificationKind.UNVERIFIED)
    if verification_kind not in {
        VerificationKind.VERIFIED_EXACT,
        VerificationKind.SIMILAR,
    }:
        return Eligibility(
            EligibilityCode.UNKNOWN_RELATION,
            "Unknown or incomplete relationships cannot be copied or moved by the organizer.",
        )
    current_generations = _evaluate_review_generations(dupe, group)
    if not current_generations.allowed:
        return current_generations
    if dupe is group.ref or bool(getattr(dupe, "is_ref", False)):
        return Eligibility(EligibilityCode.REFERENCE_FILE, "The selected file is a protected keeper.")
    if getattr(dupe, "comparison_pool", "incoming") != "incoming":
        return Eligibility(
            EligibilityCode.PROTECTED_POOL,
            "Files in protected or compare-only pools cannot be copied or moved by the organizer.",
        )
    current_context = _evaluate_current_context(results, dupe, current_pool_resolver)
    if not current_context.allowed:
        return current_context
    return Eligibility(EligibilityCode.ELIGIBLE, "The current incoming file may be copied or moved.")


def evaluate_relocation_batch(
    results,
    dupes: Iterable[object],
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> BatchEligibility:
    allowed = []
    blocked = []
    seen = set()
    for dupe in dupes:
        if id(dupe) in seen:
            continue
        seen.add(id(dupe))
        eligibility = evaluate_relocation(results, dupe, current_pool_resolver)
        if eligibility.allowed:
            allowed.append(dupe)
        else:
            blocked.append((dupe, eligibility))
    return BatchEligibility(tuple(allowed), tuple(blocked))


def evaluate_relocation_action(
    results,
    dupe,
    current_pool_resolver: Optional[Callable[[object], str]] = None,
    *,
    stop_check=None,
    progress_callback=None,
) -> Eligibility:
    """Gate one imminent Copy/Move and live-verify its review keeper.

    The selected source's SHA-256 is deliberately deferred to the copy/move
    executor, which consumes it from the held source handle at publication.
    This avoids a redundant full read without weakening the terminal proof.
    """

    eligibility = evaluate_relocation(results, dupe, current_pool_resolver)
    if not eligibility.allowed:
        return eligibility
    group = results.get_group_of_duplicate(dupe)
    return _evaluate_review_contents(
        dupe,
        group,
        defer_target=True,
        stop_check=stop_check,
        progress_callback=progress_callback,
    )


def evaluate_rename(
    results,
    item,
    current_pool_resolver: Optional[Callable[[object], str]] = None,
) -> Eligibility:
    """Gate an in-place rename and bind it to the current directory policy."""

    if getattr(results, "loaded_report", False):
        return Eligibility(
            EligibilityCode.SAVED_REPORT,
            "Saved results are historical reports; run a new scan before renaming files.",
        )
    receipt = getattr(results, "scan_receipt", None)
    if receipt is None or not receipt.complete:
        return Eligibility(
            EligibilityCode.INCOMPLETE_SCAN,
            "The scan has incomplete or missing coverage evidence.",
        )
    group = results.get_group_of_duplicate(item)
    if group is None or group.ref is None:
        return Eligibility(EligibilityCode.MISSING_KEEPER, "The selected file is no longer in a result group.")
    current_generations = _evaluate_review_generations(item, group)
    if not current_generations.allowed:
        return current_generations
    if getattr(item, "comparison_pool", "incoming") != "incoming" or bool(getattr(item, "is_ref", False)):
        return Eligibility(
            EligibilityCode.PROTECTED_POOL,
            "Files in protected or compare-only pools cannot be renamed.",
        )
    current_context = _evaluate_current_context(results, item, current_pool_resolver)
    if not current_context.allowed:
        return current_context
    return Eligibility(EligibilityCode.ELIGIBLE, "The current incoming file may be renamed.")
