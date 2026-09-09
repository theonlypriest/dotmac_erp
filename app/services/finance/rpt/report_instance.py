"""
ReportInstanceService - Report generation and instance management.

Manages report generation, execution, and output storage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.finance.rpt.report_definition import ReportDefinition, ReportType
from app.models.finance.rpt.report_instance import ReportInstance, ReportStatus
from app.rls import set_current_organization_sync
from app.services.common import coerce_uuid
from app.services.finance.rpt.report_definition import (
    ReportDefinitionInput,
    report_definition_service,
)
from app.services.file_upload import get_generated_report_upload
from app.services.response import ListResponseMixin
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


@dataclass
class ReportGenerationRequest:
    """Request to generate a report."""

    report_def_id: UUID | None = None
    report_code: str | None = None
    output_format: str = "PDF"
    fiscal_period_id: UUID | None = None
    parameters: dict | None = None
    schedule_id: UUID | None = None


@dataclass
class ReportGenerationResult:
    """Result of report generation."""

    instance_id: UUID
    status: ReportStatus
    output_file_path: str | None = None
    generation_time_ms: int | None = None
    error_message: str | None = None


class ReportInstanceService(ListResponseMixin):
    """
    Service for report instance management.

    Handles:
    - Report generation requests
    - Execution status tracking
    - Output file management
    - Instance history
    """

    @staticmethod
    def _commit_and_refresh(db: Session, instance: ReportInstance) -> ReportInstance:
        """Commit and restore tenant context before refreshing the row."""
        organization_id = instance.organization_id
        db.commit()
        set_current_organization_sync(db, organization_id)
        db.refresh(instance)
        return instance

    @staticmethod
    def queue_report(
        db: Session,
        organization_id: UUID,
        request: ReportGenerationRequest,
        requested_by_user_id: UUID,
    ) -> ReportInstance:
        """
        Queue a report for generation.

        Args:
            db: Database session
            organization_id: Organization scope
            request: Generation request
            requested_by_user_id: User requesting the report

        Returns:
            Created ReportInstance in QUEUED status
        """
        org_id = coerce_uuid(organization_id)
        user_id = coerce_uuid(requested_by_user_id)

        definition = ReportInstanceService._resolve_definition(
            db,
            org_id,
            request,
            requested_by_user_id,
        )

        if not definition.is_active:
            raise HTTPException(
                status_code=400,
                detail="Report definition is not active",
            )

        # Validate output format
        if (
            definition.supported_formats
            and request.output_format not in definition.supported_formats
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Output format {request.output_format} not supported for this report",
            )

        instance = ReportInstance(
            report_def_id=definition.report_def_id,
            organization_id=org_id,
            schedule_id=request.schedule_id,
            fiscal_period_id=request.fiscal_period_id,
            parameters_used=request.parameters,
            output_format=request.output_format,
            status=ReportStatus.QUEUED,
            generated_by_user_id=user_id,
        )

        db.add(instance)
        return ReportInstanceService._commit_and_refresh(db, instance)

    @staticmethod
    def create_instance(
        db: Session,
        organization_id: UUID,
        request: ReportGenerationRequest,
        created_by_user_id: UUID,
    ) -> ReportInstance:
        """Create a report instance (queued for generation)."""
        return ReportInstanceService.queue_report(
            db=db,
            organization_id=organization_id,
            request=request,
            requested_by_user_id=created_by_user_id,
        )

    @staticmethod
    def start_generation(
        db: Session,
        instance_id: UUID,
    ) -> ReportInstance:
        """
        Mark report generation as started.

        Args:
            db: Database session
            instance_id: Instance to start

        Returns:
            Updated ReportInstance
        """
        inst_id = coerce_uuid(instance_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Report instance not found")

        if instance.status != ReportStatus.QUEUED:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start report in {instance.status} status",
            )

        instance.status = ReportStatus.GENERATING
        instance.started_at = datetime.now(UTC)

        return ReportInstanceService._commit_and_refresh(db, instance)

    @staticmethod
    def generate_report(
        db: Session,
        organization_id: UUID,
        instance_id: UUID,
        generated_by_user_id: UUID,
    ) -> ReportInstance:
        """
        Generate a report instance and persist output payload.
        """
        org_id = coerce_uuid(organization_id)
        inst_id = coerce_uuid(instance_id)
        user_id = coerce_uuid(generated_by_user_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance or instance.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Report instance not found")

        if instance.status not in {ReportStatus.QUEUED, ReportStatus.FAILED}:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot generate report in {instance.status} status",
            )

        definition = db.get(ReportDefinition, instance.report_def_id)
        if not definition or definition.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Report definition not found")

        instance.status = ReportStatus.GENERATING
        instance.started_at = datetime.now(UTC)
        instance.generated_by_user_id = user_id
        instance = ReportInstanceService._commit_and_refresh(db, instance)

        try:
            payload = ReportInstanceService._generate_payload(
                db=db,
                organization_id=org_id,
                definition=definition,
                parameters=instance.parameters_used or {},
                fiscal_period_id=instance.fiscal_period_id,
            )

            output_bytes = json.dumps(payload, default=str, indent=2).encode("utf-8")
            upload = get_generated_report_upload().save(
                output_bytes,
                content_type="application/json",
                subdirs=(str(org_id),),
                prefix=str(instance.instance_id),
                original_filename=f"{instance.instance_id}.json",
            )
            instance = ReportInstanceService.complete_generation(
                db=db,
                instance_id=instance.instance_id,
                output_file_path=f"s3://{upload.s3_key}",
                output_size_bytes=upload.file_size,
            )
        except Exception as exc:  # noqa: BLE001
            instance = ReportInstanceService.fail_generation(
                db=db,
                instance_id=instance.instance_id,
                error_message=str(exc),
            )
            raise

        return instance

    @staticmethod
    def complete_generation(
        db: Session,
        instance_id: UUID,
        output_file_path: str,
        output_size_bytes: int,
    ) -> ReportInstance:
        """
        Mark report generation as completed.

        Args:
            db: Database session
            instance_id: Instance to complete
            output_file_path: Path to generated file
            output_size_bytes: Size of output file

        Returns:
            Updated ReportInstance
        """
        inst_id = coerce_uuid(instance_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Report instance not found")

        if instance.status != ReportStatus.GENERATING:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot complete report in {instance.status} status",
            )

        now = datetime.now(UTC)
        generation_time_ms = None
        if instance.started_at:
            delta = now - instance.started_at
            generation_time_ms = int(delta.total_seconds() * 1000)

        instance.status = ReportStatus.COMPLETED
        instance.completed_at = now
        instance.output_file_path = output_file_path
        instance.output_size_bytes = output_size_bytes
        instance.generation_time_ms = generation_time_ms

        return ReportInstanceService._commit_and_refresh(db, instance)

    @staticmethod
    def fail_generation(
        db: Session,
        instance_id: UUID,
        error_message: str,
    ) -> ReportInstance:
        """
        Mark report generation as failed.

        Args:
            db: Database session
            instance_id: Instance that failed
            error_message: Error description

        Returns:
            Updated ReportInstance
        """
        inst_id = coerce_uuid(instance_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Report instance not found")

        now = datetime.now(UTC)
        generation_time_ms = None
        if instance.started_at:
            delta = now - instance.started_at
            generation_time_ms = int(delta.total_seconds() * 1000)

        instance.status = ReportStatus.FAILED
        instance.completed_at = now
        instance.error_message = error_message
        instance.generation_time_ms = generation_time_ms

        return ReportInstanceService._commit_and_refresh(db, instance)

    @staticmethod
    def cancel_report(
        db: Session,
        organization_id: UUID,
        instance_id: UUID,
    ) -> ReportInstance:
        """
        Cancel a queued report.

        Args:
            db: Database session
            organization_id: Organization scope
            instance_id: Instance to cancel

        Returns:
            Updated ReportInstance
        """
        org_id = coerce_uuid(organization_id)
        inst_id = coerce_uuid(instance_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance or instance.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Report instance not found")

        if instance.status != ReportStatus.QUEUED:
            raise HTTPException(
                status_code=400,
                detail=f"Can only cancel queued reports, current status: {instance.status}",
            )

        instance.status = ReportStatus.CANCELLED
        instance.completed_at = datetime.now(UTC)

        return ReportInstanceService._commit_and_refresh(db, instance)

    @staticmethod
    def get_report_data(
        db: Session,
        organization_id: str,
        instance_id: str,
    ) -> dict:
        """
        Return the generated report payload for an instance.
        """
        org_id = coerce_uuid(organization_id)
        inst_id = coerce_uuid(instance_id)

        instance = db.get(ReportInstance, inst_id)
        if not instance or instance.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Report instance not found")

        if instance.status != ReportStatus.COMPLETED:
            raise HTTPException(
                status_code=400,
                detail=f"Report instance is {instance.status.value}, output not available",
            )

        if not instance.output_file_path or not instance.output_file_path.startswith(
            "s3://"
        ):
            raise HTTPException(status_code=404, detail="Report output not found")

        key = instance.output_file_path.removeprefix("s3://")
        try:
            storage = get_storage()
            if not storage.exists(key):
                raise HTTPException(status_code=404, detail="Report output not found")
            content = storage.download(key)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Report storage is temporarily unavailable"
            ) from exc

        try:
            return cast(dict[Any, Any], json.loads(content))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500, detail="Report output is invalid"
            ) from exc

    @staticmethod
    def _resolve_definition(
        db: Session,
        organization_id: UUID,
        request: ReportGenerationRequest,
        created_by_user_id: UUID,
    ) -> ReportDefinition:
        """Resolve report definition by id or code."""
        if request.report_def_id:
            definition = db.get(ReportDefinition, request.report_def_id)
            if not definition or definition.organization_id != organization_id:
                raise HTTPException(
                    status_code=404, detail="Report definition not found"
                )
            return definition

        if request.report_code:
            definition = db.scalars(
                select(ReportDefinition).where(
                    and_(
                        ReportDefinition.organization_id == organization_id,
                        ReportDefinition.report_code == request.report_code,
                    )
                )
            ).first()
            if definition:
                return definition

            standard = ReportInstanceService._standard_definition(request.report_code)
            if standard:
                input_data = ReportDefinitionInput(
                    report_code=standard["report_code"],
                    report_name=standard["report_name"],
                    report_type=standard["report_type"],
                    data_source_type=standard["data_source_type"],
                    description=standard.get("description"),
                    category=standard.get("category"),
                    subcategory=standard.get("subcategory"),
                    default_format="PDF",
                    supported_formats=["PDF", "XLSX", "CSV"],
                    is_system_report=True,
                )
                return report_definition_service.create_definition(
                    db=db,
                    organization_id=organization_id,
                    input=input_data,
                    created_by_user_id=created_by_user_id,
                )

        raise HTTPException(status_code=400, detail="Report definition is required")

    @staticmethod
    def _standard_definition(report_code: str) -> dict | None:
        code = report_code.upper()
        standard = {
            "TRIAL_BALANCE": {
                "report_code": "TRIAL_BALANCE",
                "report_name": "Trial Balance",
                "report_type": ReportType.TRIAL_BALANCE,
                "data_source_type": "GL",
                "category": "financial",
            },
            "INCOME_STATEMENT": {
                "report_code": "INCOME_STATEMENT",
                "report_name": "Statement of Profit or Loss",
                "report_type": ReportType.INCOME_STATEMENT,
                "data_source_type": "GL",
                "category": "financial",
            },
            "BALANCE_SHEET": {
                "report_code": "BALANCE_SHEET",
                "report_name": "Statement of Financial Position",
                "report_type": ReportType.BALANCE_SHEET,
                "data_source_type": "GL",
                "category": "financial",
            },
            "GENERAL_LEDGER": {
                "report_code": "GENERAL_LEDGER",
                "report_name": "General Ledger",
                "report_type": ReportType.GENERAL_LEDGER,
                "data_source_type": "GL",
                "category": "operational",
            },
            "GL_JOURNALS": {
                "report_code": "GL_JOURNALS",
                "report_name": "GL Journals Export",
                "report_type": ReportType.CUSTOM,
                "data_source_type": "GL",
                "category": "operational",
            },
            "GL_LEDGER": {
                "report_code": "GL_LEDGER",
                "report_name": "Ledger Transactions Export",
                "report_type": ReportType.CUSTOM,
                "data_source_type": "GL",
                "category": "operational",
            },
            "AR_INVOICES": {
                "report_code": "AR_INVOICES",
                "report_name": "AR Invoices Export",
                "report_type": ReportType.CUSTOM,
                "data_source_type": "AR",
                "category": "operational",
            },
            "AR_RECEIPTS": {
                "report_code": "AR_RECEIPTS",
                "report_name": "AR Receipts Export",
                "report_type": ReportType.CUSTOM,
                "data_source_type": "AR",
                "category": "operational",
            },
            "EXPENSE_SUMMARY": {
                "report_code": "EXPENSE_SUMMARY",
                "report_name": "Expense Summary",
                "report_type": ReportType.CUSTOM,
                "data_source_type": "GL",
                "category": "operational",
            },
            "AP_AGING": {
                "report_code": "AP_AGING",
                "report_name": "AP Aging",
                "report_type": ReportType.AGING,
                "data_source_type": "AP",
                "category": "operational",
            },
            "AR_AGING": {
                "report_code": "AR_AGING",
                "report_name": "AR Aging",
                "report_type": ReportType.AGING,
                "data_source_type": "AR",
                "category": "operational",
            },
            "TAX_SUMMARY": {
                "report_code": "TAX_SUMMARY",
                "report_name": "Tax Summary",
                "report_type": ReportType.TAX,
                "data_source_type": "TAX",
                "category": "compliance",
            },
            "CASH_FLOW": {
                "report_code": "CASH_FLOW",
                "report_name": "Cash Flow Statement",
                "report_type": ReportType.CASH_FLOW,
                "data_source_type": "GL",
                "category": "financial",
            },
            "CHANGES_IN_EQUITY": {
                "report_code": "CHANGES_IN_EQUITY",
                "report_name": "Changes in Equity",
                "report_type": ReportType.CHANGES_IN_EQUITY,
                "data_source_type": "GL",
                "category": "financial",
            },
            "BUDGET_VS_ACTUAL": {
                "report_code": "BUDGET_VS_ACTUAL",
                "report_name": "Budget vs Actual",
                "report_type": ReportType.BUDGET_VS_ACTUAL,
                "data_source_type": "GL",
                "category": "operational",
            },
        }
        return standard.get(code)

    @staticmethod
    def _generate_payload(
        db: Session,
        organization_id: UUID,
        definition: ReportDefinition,
        parameters: dict,
        fiscal_period_id: UUID | None,
    ) -> dict:
        """Generate report payload based on definition."""
        report_code = (definition.report_code or "").upper()

        if report_code == "TRIAL_BALANCE":
            from app.services.finance.rpt.trial_balance import trial_balance_context

            return trial_balance_context(
                db, str(organization_id), as_of_date=parameters.get("as_of_date")
            )
        if report_code == "INCOME_STATEMENT":
            from app.services.finance.rpt.income_statement import (
                income_statement_context,
            )

            return income_statement_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "BALANCE_SHEET":
            from app.services.finance.rpt.balance_sheet import balance_sheet_context

            return balance_sheet_context(
                db, str(organization_id), as_of_date=parameters.get("as_of_date")
            )
        if report_code == "GENERAL_LEDGER":
            from app.services.finance.rpt.general_ledger import general_ledger_context

            return general_ledger_context(
                db,
                str(organization_id),
                account_id=parameters.get("account_id"),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "EXPENSE_SUMMARY":
            from app.services.finance.rpt.expense_summary import expense_summary_context

            return expense_summary_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "AP_AGING":
            from app.services.finance.rpt.aging import ap_aging_context

            return ap_aging_context(
                db, str(organization_id), as_of_date=parameters.get("as_of_date")
            )
        if report_code == "AR_AGING":
            from app.services.finance.rpt.aging import ar_aging_context

            return ar_aging_context(
                db, str(organization_id), as_of_date=parameters.get("as_of_date")
            )
        if report_code == "TAX_SUMMARY":
            from app.services.finance.rpt.tax_summary import tax_summary_context

            return tax_summary_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "CASH_FLOW":
            from app.services.finance.rpt.cash_flow import cash_flow_context

            return cash_flow_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "CHANGES_IN_EQUITY":
            from app.services.finance.rpt.changes_in_equity import (
                changes_in_equity_context,
            )

            return changes_in_equity_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
            )
        if report_code == "BUDGET_VS_ACTUAL":
            from app.services.finance.rpt.budget_vs_actual import (
                budget_vs_actual_context,
            )

            return budget_vs_actual_context(
                db,
                str(organization_id),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
                budget_id=parameters.get("budget_id"),
                budget_code=parameters.get("budget_code"),
            )

        raise HTTPException(
            status_code=400, detail=f"Unsupported report code {report_code}"
        )

    @staticmethod
    def get_queued_reports(
        db: Session,
        organization_id: str | None = None,
        limit: int = 50,
    ) -> list[ReportInstance]:
        """
        Get queued reports awaiting generation.

        Args:
            db: Database session
            organization_id: Optional org filter
            limit: Maximum reports to return

        Returns:
            List of queued report instances
        """
        stmt = select(ReportInstance).where(
            ReportInstance.status == ReportStatus.QUEUED
        )

        if organization_id:
            stmt = stmt.where(
                ReportInstance.organization_id == coerce_uuid(organization_id)
            )

        stmt = stmt.order_by(ReportInstance.queued_at).limit(limit)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get_generation_statistics(
        db: Session,
        organization_id: str,
        report_def_id: str | None = None,
    ) -> dict:
        """
        Get report generation statistics.

        Args:
            db: Database session
            organization_id: Organization scope
            report_def_id: Optional filter by report definition

        Returns:
            Dict with statistics
        """
        org_id = coerce_uuid(organization_id)

        stmt = select(ReportInstance).where(ReportInstance.organization_id == org_id)

        if report_def_id:
            stmt = stmt.where(
                ReportInstance.report_def_id == coerce_uuid(report_def_id)
            )

        instances = list(db.scalars(stmt).all())

        total = len(instances)
        completed = len([i for i in instances if i.status == ReportStatus.COMPLETED])
        failed = len([i for i in instances if i.status == ReportStatus.FAILED])
        queued = len([i for i in instances if i.status == ReportStatus.QUEUED])
        generating = len([i for i in instances if i.status == ReportStatus.GENERATING])

        generation_times = [
            i.generation_time_ms for i in instances if i.generation_time_ms is not None
        ]
        avg_generation_time = (
            sum(generation_times) / len(generation_times) if generation_times else 0
        )

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "queued": queued,
            "generating": generating,
            "success_rate": completed / total if total > 0 else 0,
            "avg_generation_time_ms": avg_generation_time,
        }

    @staticmethod
    def cleanup_old_instances(
        db: Session,
        organization_id: UUID,
        retention_days: int,
    ) -> int:
        """
        Clean up old report instances.

        Args:
            db: Database session
            organization_id: Organization scope
            retention_days: Days to retain

        Returns:
            Number of instances deleted
        """
        from datetime import timedelta

        org_id = coerce_uuid(organization_id)
        cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

        instances = list(
            db.scalars(
                select(ReportInstance).where(
                    and_(
                        ReportInstance.organization_id == org_id,
                        ReportInstance.generated_at < cutoff_date,
                        ReportInstance.status.in_(
                            [
                                ReportStatus.COMPLETED,
                                ReportStatus.FAILED,
                                ReportStatus.CANCELLED,
                            ]
                        ),
                    )
                )
            ).all()
        )

        count = len(instances)
        for instance in instances:
            db.delete(instance)

        db.commit()
        return count

    @staticmethod
    def regenerate_report(
        db: Session,
        organization_id: UUID,
        instance_id: UUID,
        requested_by_user_id: UUID,
    ) -> ReportInstance:
        """
        Regenerate a previously run report.

        Args:
            db: Database session
            organization_id: Organization scope
            instance_id: Instance to regenerate
            requested_by_user_id: User requesting regeneration

        Returns:
            New ReportInstance
        """
        org_id = coerce_uuid(organization_id)
        inst_id = coerce_uuid(instance_id)
        user_id = coerce_uuid(requested_by_user_id)

        original = db.get(ReportInstance, inst_id)
        if not original or original.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Report instance not found")

        request = ReportGenerationRequest(
            report_def_id=original.report_def_id,
            output_format=original.output_format,
            fiscal_period_id=original.fiscal_period_id,
            parameters=original.parameters_used,
            schedule_id=original.schedule_id,
        )

        return ReportInstanceService.queue_report(
            db=db,
            organization_id=organization_id,
            request=request,
            requested_by_user_id=user_id,
        )

    @staticmethod
    def get(
        db: Session,
        instance_id: str,
        organization_id: UUID | None = None,
    ) -> ReportInstance:
        """Get a report instance by ID."""
        instance = db.get(ReportInstance, coerce_uuid(instance_id))
        if not instance:
            raise HTTPException(status_code=404, detail="Report instance not found")
        if organization_id is not None and instance.organization_id != coerce_uuid(
            organization_id
        ):
            raise HTTPException(status_code=404, detail="Report instance not found")
        return instance

    @staticmethod
    def list(
        db: Session,
        organization_id: str | None = None,
        report_def_id: str | None = None,
        status: ReportStatus | None = None,
        fiscal_period_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReportInstance]:
        """List report instances with optional filters."""
        stmt = select(ReportInstance)

        if organization_id:
            stmt = stmt.where(
                ReportInstance.organization_id == coerce_uuid(organization_id)
            )

        if report_def_id:
            stmt = stmt.where(
                ReportInstance.report_def_id == coerce_uuid(report_def_id)
            )

        if status:
            stmt = stmt.where(ReportInstance.status == status)

        if fiscal_period_id:
            stmt = stmt.where(
                ReportInstance.fiscal_period_id == coerce_uuid(fiscal_period_id)
            )

        stmt = (
            stmt.order_by(ReportInstance.generated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.scalars(stmt).all())


# Module-level singleton instance
report_instance_service = ReportInstanceService()
