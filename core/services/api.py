from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from core.services.adapters import (
    ApplyAdapter,
    CoreVerifiedScanAdapter,
    DoctorAdapter,
    LocalDoctorAdapter,
    ProgressCallback,
    QueryAdapter,
    ReportQueryAdapter,
    SafeActionApplyAdapter,
    ScanAdapter,
    null_progress,
)
from core.services.jsonio import MAX_PLAN_ACTIONS
from core.services.models import (
    QUERY_REPORT_SCHEMA,
    QUARANTINE_ACTION_SCHEMA,
    QUARANTINE_LIST_SCHEMA,
    SCHEMA_VERSION,
    VERIFIED_EXACT,
    ApplyReport,
    DeletionPlan,
    PlanAction,
    ScanReport,
    ScanRequest,
    action_id_for,
    plan_id_for,
    utc_now,
)


class ScanService:
    def __init__(self, adapter: Optional[ScanAdapter] = None):
        self.adapter = adapter or CoreVerifiedScanAdapter()

    def scan(self, request: ScanRequest, progress: ProgressCallback = null_progress) -> ScanReport:
        return self.adapter.scan(request, progress)


class PlanService:
    def create(
        self,
        report: ScanReport,
        operation: str = "quarantine",
    ) -> DeletionPlan:
        if operation != "quarantine":
            raise ValueError(
                "Unsupported operation: {}; exact plans only support recoverable quarantine".format(operation)
            )
        if not report.summary.complete:
            raise ValueError("scan report is incomplete; destructive plans are disabled")
        actions = []
        for group in report.groups:
            if group.verification != VERIFIED_EXACT:
                continue
            for target in group.duplicates:
                if len(actions) >= MAX_PLAN_ACTIONS:
                    raise ValueError(
                        "deletion plan exceeds the {}-action limit".format(
                            MAX_PLAN_ACTIONS,
                        )
                    )
                action_id = action_id_for(group.group_id, target.path, operation)
                actions.append(
                    PlanAction(
                        action_id=action_id,
                        group_id=group.group_id,
                        operation=operation,
                        target=target,
                        reference=group.reference,
                        verification=group.verification,
                    )
                )
        actions.sort(key=lambda action: (action.group_id, action.target.path, action.action_id))
        plan_id = plan_id_for(report.scan_id, report.roots, actions)
        return DeletionPlan(
            plan_id=plan_id,
            created_at=utc_now(),
            source_scan_id=report.scan_id,
            roots=report.roots,
            actions=tuple(actions),
            engine_version=report.engine_version,
        )


class ApplyService:
    def __init__(self, adapter: Optional[ApplyAdapter] = None):
        self.adapter = adapter or SafeActionApplyAdapter()

    def apply(
        self,
        plan: DeletionPlan,
        dry_run: bool = True,
        progress: ProgressCallback = null_progress,
    ) -> ApplyReport:
        preparation = self.adapter.preflight(plan, persist=not dry_run)
        for index, result in enumerate(preparation.results, 1):
            progress(
                "validating",
                {
                    "action": index,
                    "actions": len(plan.actions),
                    "status": result.status,
                },
            )
        if dry_run or any(result.status != "ready" for result in preparation.results):
            return ApplyReport(plan_id=plan.plan_id, dry_run=dry_run, results=preparation.results)

        execution_results = self.adapter.execute(preparation)
        for index, result in enumerate(execution_results, 1):
            progress(
                "applying",
                {
                    "action": index,
                    "actions": len(plan.actions),
                    "status": result.status,
                },
            )
        return ApplyReport(plan_id=plan.plan_id, dry_run=False, results=tuple(execution_results))


class QueryService:
    def __init__(self, adapter: Optional[QueryAdapter] = None):
        self.adapter = adapter or ReportQueryAdapter()

    def query(
        self,
        report: ScanReport,
        group_id: Optional[str] = None,
        path: Optional[str] = None,
        digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        groups = self.adapter.query(report, group_id=group_id, path=path, digest=digest)
        return {
            "schema": QUERY_REPORT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "source_scan_id": report.scan_id,
            "filters": {
                "group_id": group_id,
                "path": path,
                "digest": digest,
            },
            "matches": [group.to_dict() for group in groups],
            "summary": {"groups": len(groups)},
        }


class DoctorService:
    def __init__(self, adapter: Optional[DoctorAdapter] = None):
        self.adapter = adapter or LocalDoctorAdapter()

    def inspect(self) -> Mapping[str, Any]:
        return self.adapter.inspect()


class QuarantineService:
    def __init__(self, manager=None):
        if manager is None:
            from core.quarantine import QuarantineManager

            manager = QuarantineManager()
        self.manager = manager

    def list(self, roots):
        operations = self.manager.list(roots)
        return {
            "schema": QUARANTINE_LIST_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "roots": list(roots),
            "operations": list(operations),
            "summary": {"operations": len(operations)},
        }

    def restore(self, operation_plan_path, *, execute=False):
        result = (
            self.manager.restore(operation_plan_path)
            if execute
            else self.manager.preflight_restore(operation_plan_path)
        )
        return self._action("restore", result, dry_run=not execute)

    def finalize(self, operation_plan_path, *, execute=False):
        result = (
            self.manager.finalize(operation_plan_path)
            if execute
            else self.manager.preflight_finalize(operation_plan_path)
        )
        return self._action("finalize", result, dry_run=not execute)

    @staticmethod
    def _action(command, result, *, dry_run):
        return {
            "schema": QUARANTINE_ACTION_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "command": command,
            "dry_run": dry_run,
            "result": result.to_dict(),
        }


class Services:
    """Injectable service collection used by CLI and future GUI integration."""

    def __init__(
        self,
        scan: Optional[ScanService] = None,
        plan: Optional[PlanService] = None,
        apply: Optional[ApplyService] = None,
        query: Optional[QueryService] = None,
        doctor: Optional[DoctorService] = None,
        quarantine: Optional[QuarantineService] = None,
        video=None,
    ):
        self.scan = scan or ScanService()
        self.plan = plan or PlanService()
        self.apply = apply or ApplyService()
        self.query = query or QueryService()
        self.doctor = doctor or DoctorService()
        self.quarantine = quarantine or QuarantineService()
        if video is None:
            from core.services.video import VideoService

            video = VideoService()
        self.video = video
