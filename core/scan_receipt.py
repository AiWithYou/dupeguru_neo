# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Coverage accounting shared by GUI scans and destructive safety gates."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Tuple


class ScanStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_SKIPS = "complete_with_skips"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RESOURCE_LIMIT = "resource_limit"


@dataclass(frozen=True)
class ScanIssue:
    code: str
    message: str
    path: str = ""

    def __post_init__(self):
        if not self.code or not self.message:
            raise ValueError("scan issues require a code and message")


@dataclass(frozen=True)
class ScanReceipt:
    scan_id: str
    status: ScanStatus
    discovered: int
    analyzed: int
    skipped: int = 0
    failed: int = 0
    started_at_ns: int = 0
    finished_at_ns: int = 0
    issues: Tuple[ScanIssue, ...] = ()

    def __post_init__(self):
        if not self.scan_id:
            raise ValueError("scan_id must not be empty")
        counts = (self.discovered, self.analyzed, self.skipped, self.failed)
        if any(count < 0 for count in counts):
            raise ValueError("scan counts must not be negative")
        if self.analyzed + self.skipped + self.failed > self.discovered:
            raise ValueError("scan accounting exceeds discovered inputs")
        if self.finished_at_ns and self.started_at_ns > self.finished_at_ns:
            raise ValueError("scan finish time precedes its start")
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def complete(self) -> bool:
        return (
            self.status is ScanStatus.COMPLETE
            and self.skipped == 0
            and self.failed == 0
            and self.analyzed == self.discovered
        )

    @property
    def allows_destructive_actions(self) -> bool:
        return self.complete

    @classmethod
    def completed(cls, discovered: int, issues: Iterable[ScanIssue] = ()):
        issues = tuple(issues)
        now = time.time_ns()
        status = ScanStatus.COMPLETE if not issues else ScanStatus.COMPLETE_WITH_SKIPS
        return cls(
            scan_id=str(uuid.uuid4()),
            status=status,
            discovered=discovered,
            analyzed=discovered,
            started_at_ns=now,
            finished_at_ns=now,
            issues=issues,
        )

    @classmethod
    def incomplete(
        cls,
        *,
        discovered: int,
        analyzed: int,
        skipped: int = 0,
        failed: int = 0,
        issues: Iterable[ScanIssue],
        status: ScanStatus = ScanStatus.COMPLETE_WITH_SKIPS,
    ):
        if status is ScanStatus.COMPLETE:
            raise ValueError("incomplete receipt cannot use complete status")
        now = time.time_ns()
        return cls(
            scan_id=str(uuid.uuid4()),
            status=status,
            discovered=discovered,
            analyzed=analyzed,
            skipped=skipped,
            failed=failed,
            started_at_ns=now,
            finished_at_ns=now,
            issues=tuple(issues),
        )


def receipt_from_walk_coverages(file_count, coverages):
    """Convert root walk coverage into a conservative GUI scan receipt."""

    issues = []
    skipped = 0
    failed = 0
    for coverage in coverages:
        failed += int(getattr(coverage, "errors", 0))
        coverage_skips = sum(
            int(getattr(coverage, name, 0))
            for name in (
                "skipped_symlinks",
                "skipped_reparse_points",
                "skipped_mounts",
                "skipped_cycles",
                "skipped_outside_root",
                "skipped_special_files",
                "skipped_changed_directories",
            )
        )
        skipped += coverage_skips
        if not getattr(coverage, "complete", False):
            issues.append(
                ScanIssue(
                    code="filesystem_coverage_incomplete",
                    message="A selected root could not be enumerated completely",
                )
            )
    if issues:
        return ScanReceipt.incomplete(
            discovered=file_count + skipped + failed,
            analyzed=file_count,
            skipped=skipped,
            failed=failed,
            issues=issues,
        )
    return ScanReceipt.completed(file_count)
