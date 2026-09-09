"""
Finance Module Background Tasks - Celery tasks for finance workflows.

Handles:
- Fiscal period close reminders
- Tax filing due date reminders
- Bank reconciliation overdue alerts
- AR collection reminders for overdue invoices
- Subledger reconciliation discrepancy alerts
"""

import asyncio
import html
import logging
from contextlib import closing
from datetime import date
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session_context import (
    cross_org_session,
    for_each_organization,
    prime_tenant_context,
    session_for_org,
)
from app.models.email_profile import EmailModule
from app.models.finance.rpt.report_instance import ReportInstance, ReportStatus
from app.models.notification import EntityType, NotificationType
from app.models.person import Person
from app.models.rbac import PersonRole, Role
from app.services.email import send_email
from app.services.finance.rpt.report_instance import ReportInstanceService
from app.services.notification import NotificationService
from app.services.storage import get_storage
from app.tenant_catalog import active_organization_ids

logger = logging.getLogger(__name__)


def _list_active_organization_ids() -> list[UUID]:
    return active_organization_ids()


def _resolve_report_instance_org(instance_id: str) -> UUID | None:
    with cross_org_session() as db:
        instance = db.get(ReportInstance, UUID(instance_id))
        return instance.organization_id if instance else None


def _get_export_instance(db: Session, instance_id: str) -> ReportInstance | None:
    """Load a queued report instance from an already tenant-scoped session."""
    return db.get(ReportInstance, UUID(instance_id))


def _notify_export_result(
    db: Session,
    instance: ReportInstance,
    *,
    title: str,
    message: str,
    action_url: str | None = None,
) -> None:
    """Create an in-app notification and send a direct email for an export."""
    # Capture identifiers as plain values BEFORE committing. db.commit() expires
    # every ORM attribute, and the org-scoped session's RLS GUC (SET LOCAL) is
    # reset on commit — so any post-commit attribute access would reload the row
    # under an unprimed session and RLS would hide it (ObjectDeletedError).
    org_id = instance.organization_id
    recipient_id = instance.generated_by_user_id
    entity_id = instance.instance_id

    NotificationService().create(
        db,
        organization_id=org_id,
        recipient_id=recipient_id,
        entity_type=EntityType.SYSTEM,
        entity_id=entity_id,
        notification_type=NotificationType.INFO,
        title=title,
        message=message,
        action_url=action_url,
    )
    db.commit()
    prime_tenant_context(db, org_id)

    recipient = db.get(Person, recipient_id)
    if not recipient or not recipient.email:
        return

    safe_message = html.escape(message)
    body_html = f"<p>{safe_message}</p>"
    body_text = message
    if action_url:
        download_url = f"{settings.app_url.rstrip('/')}{action_url}"
        safe_url = html.escape(download_url)
        body_html += f'<p><a href="{safe_url}">Download export</a></p>'
        body_text = f"{message}\n\nDownload export: {download_url}"

    send_email(
        db=db,
        to_email=recipient.email,
        subject=title,
        body_html=body_html,
        body_text=body_text,
        module=EmailModule.FINANCE,
        organization_id=org_id,
    )


@shared_task
def process_general_ledger_export(
    instance_id: str,
) -> dict[str, Any]:
    """Generate a queued General Ledger export and notify the requester."""
    owning_org_id = _resolve_report_instance_org(instance_id)
    if owning_org_id is None:
        return {"success": False, "error": "Report instance not found"}

    with session_for_org(owning_org_id) as db:
        instance = _get_export_instance(db, instance_id)
        if not instance:
            return {"success": False, "error": "Report instance not found"}

        if instance.status not in {ReportStatus.QUEUED, ReportStatus.FAILED}:
            return {"success": False, "status": instance.status.value}

        try:
            instance = ReportInstanceService.start_generation(db, instance.instance_id)
            params = instance.parameters_used or {}
            fmt = (instance.output_format or "CSV").upper()

            from app.services.finance.rpt.web import reports_web_service

            data: bytes
            if fmt == "PDF":
                data = reports_web_service.export_general_ledger_pdf(
                    str(instance.organization_id),
                    db,
                    params.get("account_id"),
                    params.get("start_date"),
                    params.get("end_date"),
                )
                suffix = "pdf"
            else:
                csv_content = reports_web_service.export_general_ledger_csv(
                    str(instance.organization_id),
                    db,
                    params.get("account_id"),
                    params.get("start_date"),
                    params.get("end_date"),
                )
                suffix = "csv"
                data = csv_content.encode("utf-8")

            storage_key = (
                f"generated_reports/{instance.organization_id}/"
                f"general_ledger_{instance.instance_id}.{suffix}"
            )
            media_type = "application/pdf" if fmt == "PDF" else "text/csv"
            get_storage().upload(storage_key, data, media_type)

            instance = ReportInstanceService.complete_generation(
                db=db,
                instance_id=instance.instance_id,
                output_file_path=f"s3://{storage_key}",
                output_size_bytes=len(data),
            )

            action_url = (
                f"/finance/reports/general-ledger/exports/"
                f"{instance.instance_id}/download"
            )
            _notify_export_result(
                db,
                instance,
                title="General Ledger export ready",
                message="Your General Ledger export is ready to download.",
                action_url=action_url,
            )
            return {
                "success": True,
                "instance_id": str(instance.instance_id),
                "output_size_bytes": len(data),
            }
        except Exception as exc:
            logger.exception("General Ledger export failed for %s", instance_id)
            try:
                instance = ReportInstanceService.fail_generation(
                    db=db,
                    instance_id=instance.instance_id,
                    error_message=str(exc),
                )
                _notify_export_result(
                    db,
                    instance,
                    title="General Ledger export failed",
                    message=(
                        "Your General Ledger export could not be generated. "
                        "Please narrow the filters and try again."
                    ),
                )
            except Exception:
                logger.exception("Failed to record General Ledger export failure")
            return {"success": False, "instance_id": instance_id, "error": str(exc)}


def _response_body_bytes(response: Any) -> bytes:
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return bytes(body)


def _export_action_url(report_code: str, instance_id: UUID) -> str:
    bases = {
        "GL_JOURNALS": "/finance/gl/journals/exports",
        "GL_LEDGER": "/finance/gl/ledger/exports",
        "AR_INVOICES": "/finance/ar/invoices/exports",
        "AR_RECEIPTS": "/finance/ar/receipts/exports",
    }
    return f"{bases[report_code]}/{instance_id}/download"


def _export_label(report_code: str) -> str:
    return {
        "GL_JOURNALS": "GL Journals",
        "GL_LEDGER": "Ledger Transactions",
        "AR_INVOICES": "AR Invoices",
        "AR_RECEIPTS": "AR Receipts",
    }[report_code]


def _export_filename_prefix(report_code: str) -> str:
    return {
        "GL_JOURNALS": "gl_journals",
        "GL_LEDGER": "gl_ledger",
        "AR_INVOICES": "ar_invoices",
        "AR_RECEIPTS": "ar_receipts",
    }[report_code]


async def _build_list_export_response(
    db: Session,
    instance: ReportInstance,
    report_code: str,
) -> Any:
    params = instance.parameters_used or {}
    search = str(params.get("search") or "")
    status = str(params.get("status") or "")
    start_date = str(params.get("start_date") or "")
    end_date = str(params.get("end_date") or "")

    if report_code == "GL_JOURNALS":
        from app.services.finance.gl.bulk import get_journal_bulk_service

        journal_service = get_journal_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await journal_service.export_all(search, status, start_date, end_date)

    if report_code == "GL_LEDGER":
        from app.services.finance.gl.bulk import get_ledger_bulk_service

        ledger_service = get_ledger_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await ledger_service.export_all(
            search,
            status,
            start_date,
            end_date,
            {"account_id": params.get("account_id") or None},
        )

    if report_code == "AR_INVOICES":
        from app.services.finance.ar.invoice_bulk import get_ar_invoice_bulk_service

        invoice_service = get_ar_invoice_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await invoice_service.export_all(
            search,
            status,
            start_date,
            end_date,
            {"customer_id": params.get("customer_id") or ""},
        )

    if report_code == "AR_RECEIPTS":
        from app.services.finance.ar.receipt_bulk import get_ar_receipt_bulk_service

        receipt_service = get_ar_receipt_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await receipt_service.export_all(
            search,
            status,
            start_date,
            end_date,
            {"customer_id": params.get("customer_id") or ""},
        )

    if report_code == "AP_INVOICES":
        from app.services.finance.ap.invoice_bulk import get_ap_invoice_bulk_service

        ap_invoice_service = get_ap_invoice_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await ap_invoice_service.export_all(
            search,
            status,
            start_date,
            end_date,
            {"supplier_id": params.get("supplier_id") or ""},
        )

    if report_code == "AP_PAYMENTS":
        from app.services.finance.ap.payment_bulk import get_ap_payment_bulk_service

        ap_payment_service = get_ap_payment_bulk_service(
            db,
            instance.organization_id,
            instance.generated_by_user_id,
        )
        return await ap_payment_service.export_all(
            search,
            status,
            start_date,
            end_date,
            {"supplier_id": params.get("supplier_id") or ""},
        )

    raise ValueError(f"Unsupported export report code: {report_code}")


def _process_list_export(instance_id: str, report_code: str) -> dict[str, Any]:
    label = _export_label(report_code)
    owning_org_id = _resolve_report_instance_org(instance_id)
    if owning_org_id is None:
        return {"success": False, "error": "Report instance not found"}

    with session_for_org(owning_org_id) as db:
        instance = _get_export_instance(db, instance_id)
        if not instance:
            return {"success": False, "error": "Report instance not found"}

        if instance.status not in {ReportStatus.QUEUED, ReportStatus.FAILED}:
            return {"success": False, "status": instance.status.value}

        try:
            instance = ReportInstanceService.start_generation(db, instance.instance_id)
            response = asyncio.run(
                _build_list_export_response(db, instance, report_code)
            )
            data = _response_body_bytes(response)
            storage_key = (
                f"generated_reports/{instance.organization_id}/"
                f"{_export_filename_prefix(report_code)}_{instance.instance_id}.csv"
            )
            get_storage().upload(storage_key, data, "text/csv")

            instance = ReportInstanceService.complete_generation(
                db=db,
                instance_id=instance.instance_id,
                output_file_path=f"s3://{storage_key}",
                output_size_bytes=len(data),
            )

            action_url = _export_action_url(report_code, instance.instance_id)
            _notify_export_result(
                db,
                instance,
                title=f"{label} export ready",
                message=f"Your {label} export is ready to download.",
                action_url=action_url,
            )
            return {
                "success": True,
                "instance_id": str(instance.instance_id),
                "output_size_bytes": len(data),
            }
        except Exception as exc:
            logger.exception("%s export failed for %s", label, instance_id)
            try:
                instance = ReportInstanceService.fail_generation(
                    db=db,
                    instance_id=instance.instance_id,
                    error_message=str(exc),
                )
                _notify_export_result(
                    db,
                    instance,
                    title=f"{label} export failed",
                    message=(
                        f"Your {label} export could not be generated. "
                        "Please narrow the filters and try again."
                    ),
                )
            except Exception:
                logger.exception("Failed to record %s export failure", label)
            return {"success": False, "instance_id": instance_id, "error": str(exc)}


@shared_task
def process_gl_journals_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued GL Journals export and notify the requester."""
    return _process_list_export(instance_id, "GL_JOURNALS")


@shared_task
def process_gl_ledger_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued Ledger Transactions export and notify the requester."""
    return _process_list_export(instance_id, "GL_LEDGER")


@shared_task
def process_ar_invoices_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued AR Invoices export and notify the requester."""
    return _process_list_export(instance_id, "AR_INVOICES")


@shared_task
def process_ar_receipts_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued AR Receipts export and notify the requester."""
    return _process_list_export(instance_id, "AR_RECEIPTS")


@shared_task
def process_ap_invoices_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued AP Invoices export and notify the requester."""
    return _process_list_export(instance_id, "AP_INVOICES")


@shared_task
def process_ap_payments_export(instance_id: str) -> dict[str, Any]:
    """Generate a queued AP Payments export and notify the requester."""
    return _process_list_export(instance_id, "AP_PAYMENTS")


def _get_finance_recipients(
    db: Session,
    organization_id: UUID,
    role_names: list[str],
) -> list[UUID]:
    """
    Get user IDs with specified finance roles within a single organization.

    Args:
        db: Database session
        organization_id: Organization to scope recipients to
        role_names: List of role names to include

    Returns:
        List of person_ids with the specified roles in the given organization
    """
    stmt = (
        select(PersonRole.person_id)
        .join(Role, PersonRole.role_id == Role.id)
        .join(Person, PersonRole.person_id == Person.id)
        .where(
            Person.organization_id == organization_id,
            Role.name.in_(role_names),
            Role.is_active.is_(True),
        )
    )
    return list(db.scalars(stmt).all())


@shared_task
def process_monthly_depreciation_runs(
    auto_post: bool | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """
    Create due monthly fixed-asset depreciation runs for active organizations.

    The task scans for the next open/reopened fiscal period that has already
    ended and does not already have a non-failed depreciation run.
    """
    from app.services.fixed_assets.depreciation import DepreciationService

    logger.info("Processing monthly fixed-asset depreciation runs")

    results: dict[str, Any] = {
        "automation_enabled": False,
        "auto_post": False,
        "organizations_checked": 0,
        "runs_calculated": 0,
        "runs_posted": 0,
        "runs_reconciled": 0,
        "runs_requiring_review": 0,
        "skipped": 0,
        "errors": [],
    }

    run_cutoff_date = date.fromisoformat(as_of_date) if as_of_date else date.today()

    # Discovery is the tenant catalogue, not a scan of `core_org.organization`
    # through the ORM-listener bypass: that bypass never touched PostgreSQL RLS,
    # so under `app_user` it enumerated nothing and the job reported a clean run
    # having created no depreciation at all.
    #
    # The automation switches move inside the tenant session with it. They are
    # `SettingDomain.automation` values, which resolve tenant -> platform ->
    # default *through the session*; asked on an unscoped session they answered
    # for no particular tenant. Asked here they answer for this one, which is
    # what a per-tenant switch was always supposed to mean.
    #
    # Default `include_inactive=False`: the scan this replaces was
    # `DepreciationService.list_active_organization_ids`, which filtered
    # `Organization.is_active`. A deactivated tenant does not start new runs.
    for organization_id, db in for_each_organization():
        if not DepreciationService.automation_enabled(db):
            logger.info(
                "Monthly FA depreciation automation is disabled for org %s",
                organization_id,
            )
            continue

        effective_auto_post = (
            auto_post
            if auto_post is not None
            else DepreciationService.automation_auto_post_enabled(db)
        )
        results["automation_enabled"] = True
        results["auto_post"] = effective_auto_post
        results["organizations_checked"] += 1

        try:
            outcome = DepreciationService.create_automated_monthly_run(
                db,
                organization_id,
                as_of_date=run_cutoff_date,
                auto_post=effective_auto_post,
            )
            if outcome["status"] == "posted":
                results["runs_posted"] += 1
                reconciliation = outcome.get("gl_reconciliation")
                if isinstance(reconciliation, dict):
                    if reconciliation.get("is_reconciled"):
                        results["runs_reconciled"] += 1
                    else:
                        results["runs_requiring_review"] += 1
            elif outcome["status"] == "calculated":
                results["runs_calculated"] += 1
            else:
                results["skipped"] += 1
            db.commit()
        except Exception as exc:
            logger.exception(
                "Monthly FA depreciation automation failed for org %s",
                organization_id,
            )
            db.rollback()
            results["errors"].append(
                {
                    "organization_id": str(organization_id),
                    "error": str(exc),
                }
            )

    logger.info(
        "Monthly FA depreciation automation complete: orgs=%d, calculated=%d, "
        "posted=%d, reconciled=%d, review=%d, skipped=%d, errors=%d",
        results["organizations_checked"],
        results["runs_calculated"],
        results["runs_posted"],
        results["runs_reconciled"],
        results["runs_requiring_review"],
        results["skipped"],
        len(results["errors"]),
    )
    return results


@shared_task
def process_depreciation_gl_reconciliation(
    organization_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Auto-match a posted fixed-asset depreciation run against its GL journal."""
    from app.services.fixed_assets.reconciliation import (
        FixedAssetDepreciationReconciliationService,
    )

    org_id = UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            result = FixedAssetDepreciationReconciliationService.reconcile_run(
                db,
                org_id,
                UUID(run_id),
            )
            db.commit()
            return {"success": True, **result.as_dict()}
        except Exception as exc:
            db.rollback()
            logger.exception(
                "FA depreciation GL reconciliation failed for run %s in org %s",
                run_id,
                organization_id,
            )
            return {
                "success": False,
                "organization_id": organization_id,
                "run_id": run_id,
                "error": str(exc),
            }


@shared_task
def process_fixed_asset_gl_reconciliation_package(
    organization_id: str | None = None,
    as_of_date: str | None = None,
    submit_for_approval: bool = True,
) -> dict[str, Any]:
    """Create FA GL reconciliation packages; variances require approval."""
    from app.services.fixed_assets.reconciliation import (
        FixedAssetGLReconciliationPackageService,
    )

    report_date = date.fromisoformat(as_of_date) if as_of_date else date.today()
    organization_ids = (
        [UUID(organization_id)] if organization_id else _list_active_organization_ids()
    )
    results: dict[str, Any] = {
        "organizations_checked": len(organization_ids),
        "balanced": 0,
        "pending_approval": 0,
        "approval_not_configured": 0,
        "errors": [],
        "runs": [],
    }

    for org_id in organization_ids:
        with session_for_org(org_id) as db:
            try:
                package = FixedAssetGLReconciliationPackageService.create_package(
                    db,
                    org_id,
                    as_of=report_date,
                    submit_for_approval=submit_for_approval,
                )
                status = package.status.lower()
                if (
                    package.status
                    == FixedAssetGLReconciliationPackageService.STATUS_BALANCED
                ):
                    results["balanced"] += 1
                elif package.status == (
                    FixedAssetGLReconciliationPackageService.STATUS_PENDING_APPROVAL
                ):
                    results["pending_approval"] += 1
                else:
                    results["approval_not_configured"] += 1

                results["runs"].append(
                    {
                        "organization_id": str(org_id),
                        "run_id": str(package.run_id),
                        "status": package.status,
                        "as_of": package.as_of_date.isoformat(),
                        "approval_request_id": (
                            str(package.approval_request_id)
                            if package.approval_request_id
                            else None
                        ),
                        "total_variance_abs": str(package.total_variance_abs),
                    }
                )
                logger.info(
                    "FA GL reconciliation package %s for org %s finished as %s",
                    package.run_id,
                    org_id,
                    status,
                )
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "FA GL reconciliation package failed for org %s", org_id
                )
                results["errors"].append(
                    {"organization_id": str(org_id), "error": str(exc)}
                )

    return results


@shared_task
def process_approved_fixed_asset_gl_reconciliation_drafts(
    organization_id: str | None = None,
) -> dict[str, Any]:
    """Create draft FA GL correction journals for approved reconciliation packages."""
    from app.models.finance.audit.approval_request import (
        ApprovalRequest,
        ApprovalRequestStatus,
    )
    from app.models.fixed_assets.gl_reconciliation import (
        FixedAssetGLReconciliationRun,
    )
    from app.services.fixed_assets.reconciliation import (
        FA_GL_RECONCILIATION_DOCUMENT_TYPE,
        FixedAssetGLReconciliationPackageService,
    )

    organization_ids = (
        [UUID(organization_id)] if organization_id else _list_active_organization_ids()
    )
    results: dict[str, Any] = {
        "organizations_checked": len(organization_ids),
        "drafts_created": 0,
        "skipped": 0,
        "errors": [],
        "journals": [],
    }

    for org_id in organization_ids:
        with session_for_org(org_id) as db:
            approved_runs = list(
                db.execute(
                    select(FixedAssetGLReconciliationRun)
                    .join(
                        ApprovalRequest,
                        ApprovalRequest.request_id
                        == FixedAssetGLReconciliationRun.approval_request_id,
                    )
                    .where(
                        FixedAssetGLReconciliationRun.organization_id == org_id,
                        FixedAssetGLReconciliationRun.proposed_journal_entry_id.is_(
                            None
                        ),
                        FixedAssetGLReconciliationRun.status
                        == FixedAssetGLReconciliationPackageService.STATUS_PENDING_APPROVAL,
                        ApprovalRequest.organization_id == org_id,
                        ApprovalRequest.document_type
                        == FA_GL_RECONCILIATION_DOCUMENT_TYPE,
                        ApprovalRequest.status == ApprovalRequestStatus.APPROVED,
                    )
                    .order_by(FixedAssetGLReconciliationRun.created_at.asc())
                ).scalars()
            )

            if not approved_runs:
                results["skipped"] += 1
                continue

            for run in approved_runs:
                try:
                    journal = FixedAssetGLReconciliationPackageService.create_draft_correction_journal(
                        db,
                        org_id,
                        run.run_id,
                    )
                    results["drafts_created"] += 1
                    results["journals"].append(
                        {
                            "organization_id": str(org_id),
                            "run_id": str(run.run_id),
                            "journal_entry_id": str(journal.journal_entry_id),
                            "journal_number": journal.journal_number,
                            "status": journal.status.value
                            if hasattr(journal.status, "value")
                            else str(journal.status),
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    logger.exception(
                        "Approved FA GL reconciliation draft failed for run %s",
                        run.run_id,
                    )
                    results["errors"].append(
                        {
                            "organization_id": str(org_id),
                            "run_id": str(run.run_id),
                            "error": str(exc),
                        }
                    )

    return results


@shared_task
def process_fiscal_period_reminders() -> dict[str, Any]:
    """
    Send notifications for fiscal periods that are ending soon.

    Sends reminders:
    - 7 days before period ends
    - 3 days before period ends
    - 1 day before period ends

    Returns:
        Dict with notification statistics
    """
    from app.services.finance.reminder_service import FinanceReminderService

    logger.info("Processing fiscal period close reminders")

    results: dict[str, Any] = {
        "periods_checked": 0,
        "notifications_sent": 0,
        "errors": [],
    }

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            service = FinanceReminderService(db)
            periods = service.get_periods_closing_soon()
            results["periods_checked"] += len(periods)

            for period in periods:
                if period.organization_id != org_id:
                    continue
                try:
                    notice_type = service.get_period_notice_type(period)
                    if not notice_type:
                        continue

                    # Get accountants and finance managers for this org
                    recipients = _get_finance_recipients(
                        db,
                        period.organization_id,
                        ["accountant", "finance_manager", "controller", "cfo"],
                    )

                    if not recipients:
                        logger.warning(
                            "No finance recipients found for period %s (org %s)",
                            period.fiscal_period_id,
                            period.organization_id,
                        )
                        continue

                    sent = service.send_fiscal_period_reminder(
                        period, recipients, notice_type
                    )
                    results["notifications_sent"] += sent

                except Exception as e:
                    logger.exception(
                        "Failed to send reminder for period %s",
                        period.fiscal_period_id,
                    )
                    results["errors"].append(
                        f"Period {period.fiscal_period_id}: {str(e)}"
                    )

            db.commit()

    logger.info(
        "Fiscal period reminders complete: %d periods, %d notifications, %d errors",
        results["periods_checked"],
        results["notifications_sent"],
        len(results["errors"]),
    )
    return results


@shared_task
def process_tax_period_reminders() -> dict[str, Any]:
    """
    Send notifications for tax periods with upcoming or overdue filing deadlines.

    Sends reminders at:
    - 30 days before due
    - 14 days before due
    - 7 days before due
    - 3 days before due (urgent)
    - Daily when overdue

    Returns:
        Dict with notification statistics
    """
    from app.services.finance.reminder_service import FinanceReminderService

    logger.info("Processing tax period filing reminders")

    results: dict[str, Any] = {
        "periods_due_soon": 0,
        "periods_overdue": 0,
        "notifications_sent": 0,
        "errors": [],
    }

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            service = FinanceReminderService(db)

            # Process periods due soon
            due_soon = [
                period
                for period in service.get_tax_periods_due_soon()
                if period.organization_id == org_id
            ]
            results["periods_due_soon"] += len(due_soon)

            for period in due_soon:
                try:
                    notice_type = service.get_tax_period_notice_type(period)
                    if not notice_type:
                        continue

                    recipients = _get_finance_recipients(
                        db,
                        period.organization_id,
                        [
                            "accountant",
                            "finance_manager",
                            "tax_accountant",
                            "controller",
                        ],
                    )

                    if not recipients:
                        continue

                    sent = service.send_tax_period_reminder(
                        period, recipients, notice_type
                    )
                    results["notifications_sent"] += sent

                except Exception as e:
                    logger.exception(
                        "Failed to send tax reminder for period %s",
                        period.period_id,
                    )
                    results["errors"].append(f"Tax period {period.period_id}: {str(e)}")

            # Process overdue periods — send ONE digest per org instead of N
            # individual notifications (prevents email spam when many periods
            # are overdue)
            overdue = [
                period
                for period in service.get_overdue_tax_periods()
                if period.organization_id == org_id
            ]
            results["periods_overdue"] += len(overdue)

            if overdue:
                try:
                    recipients = _get_finance_recipients(
                        db,
                        org_id,
                        [
                            "accountant",
                            "finance_manager",
                            "tax_accountant",
                            "controller",
                            "cfo",
                        ],
                    )

                    if not recipients:
                        db.commit()
                        continue

                    sent = service.send_tax_period_digest(overdue, recipients, org_id)
                    results["notifications_sent"] += sent
                except Exception as e:
                    logger.exception(
                        "Failed to send tax overdue digest for org %s",
                        org_id,
                    )
                    results["errors"].append(
                        f"Tax overdue digest org {org_id}: {str(e)}"
                    )

            db.commit()

    logger.info(
        "Tax period reminders complete: %d due soon, %d overdue, %d notifications",
        results["periods_due_soon"],
        results["periods_overdue"],
        results["notifications_sent"],
    )
    return results


@shared_task
def process_bank_reconciliation_reminders() -> dict[str, Any]:
    """
    Send notifications for bank accounts that need reconciliation.

    Alerts when:
    - Account has never been reconciled
    - Last reconciliation is 15+ days old (warning)
    - Last reconciliation is 30+ days old (overdue)
    - Last reconciliation is 45+ days old (critical)

    Returns:
        Dict with notification statistics
    """
    from app.services.finance.reminder_service import FinanceReminderService

    logger.info("Processing bank reconciliation reminders")

    results: dict[str, Any] = {
        "accounts_checked": 0,
        "accounts_needing_action": 0,
        "notifications_sent": 0,
        "errors": [],
    }

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            service = FinanceReminderService(db)
            accounts = [
                account
                for account in service.get_accounts_needing_reconciliation()
                if account.organization_id == org_id
            ]
            results["accounts_checked"] += len(accounts)

            # Classify accounts by urgency, then send ONE digest per org
            # instead of N individual notifications
            accounts_with_urgency: list[tuple] = []
            for account in accounts:
                urgency = service.get_reconciliation_urgency(account)
                if urgency:
                    accounts_with_urgency.append((account, urgency))
                    results["accounts_needing_action"] += 1

            if accounts_with_urgency:
                try:
                    recipients = _get_finance_recipients(
                        db,
                        org_id,
                        ["accountant", "finance_manager", "controller"],
                    )

                    if not recipients:
                        db.commit()
                        continue

                    sent = service.send_reconciliation_digest(
                        accounts_with_urgency, recipients, org_id
                    )
                    results["notifications_sent"] += sent
                except Exception as e:
                    logger.exception(
                        "Failed to send reconciliation digest for org %s",
                        org_id,
                    )
                    results["errors"].append(f"Recon digest org {org_id}: {str(e)}")

            db.commit()

    logger.info(
        "Bank reconciliation reminders complete: %d accounts, %d need action, %d notifications",
        results["accounts_checked"],
        results["accounts_needing_action"],
        results["notifications_sent"],
    )
    return results


@shared_task
def process_ar_collection_reminders() -> dict[str, Any]:
    """
    Send notifications for overdue AR invoices that need collection follow-up.

    Prioritizes by aging bucket:
    - 90+ days: Critical alert
    - 60-90 days: Overdue notification
    - 30-60 days: Overdue notification
    - 1-30 days: Due soon notification

    Returns:
        Dict with notification statistics
    """
    from app.services.finance.reminder_service import FinanceReminderService

    logger.info("Processing AR collection reminders")

    results: dict[str, Any] = {
        "invoices_checked": 0,
        "by_bucket": {
            "1-30": 0,
            "31-60": 0,
            "61-90": 0,
            "over-90": 0,
        },
        "notifications_sent": 0,
        "errors": [],
    }

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            service = FinanceReminderService(db)
            invoices = [
                invoice
                for invoice in service.get_overdue_invoices(min_days_overdue=1)
                if invoice.organization_id == org_id
            ]
            results["invoices_checked"] += len(invoices)

            for invoice in invoices:
                try:
                    bucket = service.get_invoice_aging_bucket(invoice)
                    if bucket != "current" and bucket in results["by_bucket"]:
                        results["by_bucket"][bucket] += 1

                    recipients = _get_finance_recipients(
                        db,
                        invoice.organization_id,
                        ["accountant", "ar_clerk", "finance_manager", "collections"],
                    )

                    if not recipients:
                        continue

                    sent = service.send_collection_reminder(invoice, recipients)
                    results["notifications_sent"] += sent

                except Exception as e:
                    logger.exception(
                        "Failed to send collection reminder for invoice %s",
                        invoice.invoice_id,
                    )
                    results["errors"].append(f"Invoice {invoice.invoice_id}: {str(e)}")

            db.commit()

    logger.info(
        "AR collection reminders complete: %d invoices, %d notifications, buckets=%s",
        results["invoices_checked"],
        results["notifications_sent"],
        results["by_bucket"],
    )
    return results


@shared_task
def process_subledger_reconciliation() -> dict[str, Any]:
    """
    Check for discrepancies between GL control accounts and subledgers.

    Compares:
    - AR control account vs sum of open customer balances
    - AP control account vs sum of open supplier balances

    Sends alerts when discrepancies are found.

    Returns:
        Dict with reconciliation statistics
    """
    from decimal import Decimal

    from app.services.finance.dashboard import DashboardService
    from app.services.finance.reminder_service import FinanceReminderService

    logger.info("Processing subledger reconciliation checks")

    results: dict[str, Any] = {
        "organizations_checked": 0,
        "ar_discrepancies": 0,
        "ap_discrepancies": 0,
        "notifications_sent": 0,
        "errors": [],
    }

    org_ids = _list_active_organization_ids()
    results["organizations_checked"] = len(org_ids)
    for org_id in org_ids:
        with session_for_org(org_id) as db:
            reminder_service = FinanceReminderService(db)
            try:
                # Use dashboard service to get reconciliation status
                recon_data = DashboardService.get_subledger_reconciliation(db, org_id)

                # Check AR discrepancy
                if not recon_data.get("ar_ok", True):
                    results["ar_discrepancies"] += 1

                    recipients = _get_finance_recipients(
                        db,
                        org_id,
                        ["accountant", "finance_manager", "controller"],
                    )

                    if recipients:
                        sent = reminder_service.send_subledger_discrepancy_alert(
                            organization_id=org_id,
                            recipient_ids=recipients,
                            subledger_type="AR",
                            gl_balance=Decimal(str(recon_data.get("gl_ar_balance", 0))),
                            subledger_balance=Decimal(
                                str(recon_data.get("subledger_ar_balance", 0))
                            ),
                        )
                        results["notifications_sent"] += sent

                # Check AP discrepancy
                if not recon_data.get("ap_ok", True):
                    results["ap_discrepancies"] += 1

                    recipients = _get_finance_recipients(
                        db,
                        org_id,
                        ["accountant", "finance_manager", "controller"],
                    )

                    if recipients:
                        sent = reminder_service.send_subledger_discrepancy_alert(
                            organization_id=org_id,
                            recipient_ids=recipients,
                            subledger_type="AP",
                            gl_balance=Decimal(str(recon_data.get("gl_ap_balance", 0))),
                            subledger_balance=Decimal(
                                str(recon_data.get("subledger_ap_balance", 0))
                            ),
                        )
                        results["notifications_sent"] += sent

            except Exception as e:
                logger.exception(
                    "Failed to check subledger reconciliation for org %s",
                    org_id,
                )
                results["errors"].append(f"Org {org_id}: {str(e)}")

            db.commit()

    logger.info(
        "Subledger reconciliation complete: %d orgs, %d AR discrepancies, "
        "%d AP discrepancies, %d notifications",
        results["organizations_checked"],
        results["ar_discrepancies"],
        results["ap_discrepancies"],
        results["notifications_sent"],
    )
    return results


@shared_task
def process_all_finance_reminders() -> dict[str, Any]:
    """
    Master task that runs all finance reminder tasks.

    This can be scheduled as a single daily task, or individual tasks
    can be scheduled separately with different frequencies.

    Each subtask is run independently - failures in one don't stop others.

    Returns:
        Dict with combined results from all tasks
    """
    logger.info("Processing all finance reminders")

    results: dict[str, Any] = {
        "fiscal_periods": {},
        "tax_periods": {},
        "bank_reconciliation": {},
        "ar_collection": {},
        "subledger_reconciliation": {},
        "task_errors": [],
    }

    # Run each subtask directly (not via .delay()) so we can aggregate
    # results into a single return dict for monitoring. Each call is wrapped
    # in its own try/except so one failure does not prevent the others.
    task_runners = [
        ("fiscal_periods", process_fiscal_period_reminders),
        ("tax_periods", process_tax_period_reminders),
        ("bank_reconciliation", process_bank_reconciliation_reminders),
        ("ar_collection", process_ar_collection_reminders),
        ("subledger_reconciliation", process_subledger_reconciliation),
    ]

    for task_name, task_func in task_runners:
        try:
            results[task_name] = task_func()
        except Exception as e:
            logger.exception("Finance reminder subtask '%s' failed", task_name)
            results[task_name] = {"error": str(e)}
            results["task_errors"].append(f"{task_name}: {str(e)}")

    total_notifications = sum(
        r.get("notifications_sent", 0)
        for r in results.values()
        if isinstance(r, dict) and "notifications_sent" in r
    )

    logger.info(
        "All finance reminders complete: %d total notifications sent, %d task errors",
        total_notifications,
        len(results["task_errors"]),
    )

    return results


@shared_task
def sync_paystack_transactions(days_back: int = 1) -> dict[str, Any]:
    """
    Sync Paystack transactions to bank statements for reconciliation.

    This task fetches transactions and transfers from Paystack and creates
    bank statement lines for reconciliation.

    Args:
        days_back: Number of days to sync (default: 1 for daily sync)

    Returns:
        Dict with sync statistics per organization
    """
    from datetime import date, timedelta

    logger.info("Starting Paystack sync for last %d days", days_back)

    results: dict[str, Any] = {
        "organizations_synced": 0,
        "total_collections": 0,
        "total_transfers": 0,
        "total_credits": "0.00",
        "total_debits": "0.00",
        "errors": [],
    }

    to_date = date.today()
    from_date = to_date - timedelta(days=days_back)

    from app.services.finance.payments.paystack_sync import PaystackSyncService

    total_credits = 0.0
    total_debits = 0.0

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            try:
                sync_svc = PaystackSyncService(db, org_id)

                # Check if Paystack is configured for this org
                try:
                    sync_svc._get_paystack_config()
                except ValueError:
                    # Paystack not configured for this org
                    continue

                result = sync_svc.sync_transactions(from_date, to_date)

                if result.success:
                    results["organizations_synced"] += 1
                    results["total_collections"] += result.transactions_synced
                    results["total_transfers"] += result.transfers_synced
                    total_credits += float(result.total_credits)
                    total_debits += float(result.total_debits)

                    logger.info(
                        "Paystack sync for org %s: %d collections, %d transfers",
                        org_id,
                        result.transactions_synced,
                        result.transfers_synced,
                    )
                else:
                    results["errors"].append(f"Org {org_id}: {result.message}")

            except Exception as e:
                logger.exception("Failed to sync Paystack for org %s", org_id)
                results["errors"].append(f"Org {org_id}: {str(e)}")

            db.commit()

    results["total_credits"] = f"{total_credits:,.2f}"
    results["total_debits"] = f"{total_debits:,.2f}"

    logger.info(
        "Paystack sync complete: %d orgs, %d collections (₦%s), %d transfers (₦%s)",
        results["organizations_synced"],
        results["total_collections"],
        results["total_credits"],
        results["total_transfers"],
        results["total_debits"],
    )

    return results


@shared_task
def sync_mono_transactions(**_legacy_kwargs: Any) -> dict[str, Any]:
    """Incremental Mono sync across every linked bank account.

    Stateful — each account computes its own window from its own watermark,
    so missed runs self-heal on the next invocation without requiring a
    lookback parameter. ``**_legacy_kwargs`` swallows any ``days_back``
    kwarg from scheduled_tasks rows seeded before the incremental refactor.
    """
    logger.info("Starting Mono sync for all linked accounts")

    from app.models.finance.banking.bank_account import BankAccount, BankAccountStatus

    # One tenant session per organization instead of a single cross-tenant
    # SELECT: `cross_org_session` lifted only the ORM listener, so under
    # `app_user` this listed zero accounts and the sweep reported "no Mono-linked
    # bank accounts" for a fleet full of them. The `organization_id` leg of the
    # old ORDER BY is now the enumeration order itself.
    #
    # `include_inactive=True`: the old SELECT had no `Organization` predicate,
    # and a deactivated tenant's linked account still has settled lines to pull.
    mono_account_ids: list[str | None] = []
    for _org_id, db in for_each_organization(include_inactive=True):
        mono_account_ids.extend(
            db.scalars(
                select(BankAccount.mono_account_id)
                .where(
                    BankAccount.mono_account_id.isnot(None),
                    BankAccount.status == BankAccountStatus.active,
                )
                .order_by(BankAccount.bank_account_id)
            ).all()
        )

    if not mono_account_ids:
        return {
            "success": True,
            "accounts_synced": 0,
            "message": "No Mono-linked bank accounts found",
        }

    # Per-account isolation. These run in-process (not via .delay), so an
    # exception escaping one account — IntegrityError from a raced line,
    # OperationalError on a DB blip — used to kill the whole sweep and every
    # account after it. Accounts iterate in a stable order, so that starved
    # the same suffix of the fleet every single day, for a full 24h until the
    # next run. Isolate each one and keep going.
    account_results: list[dict[str, Any]] = []
    for mono_account_id in mono_account_ids:
        if not mono_account_id:
            continue
        try:
            account_results.append(
                sync_mono_account(str(mono_account_id), refresh_first=True)
            )
        except Exception as exc:  # noqa: BLE001 — one bad account must not
            # abort the sweep; the error is recorded and the next runs.
            logger.exception("Mono sweep failed for mono_id=%s", mono_account_id)
            account_results.append(
                {
                    "success": False,
                    "mono_account_id": str(mono_account_id),
                    "message": str(exc) or exc.__class__.__name__,
                }
            )

    total_errors = sum(0 if result.get("success") else 1 for result in account_results)
    results = {
        "success": total_errors == 0,
        "accounts_synced": sum(
            1 for result in account_results if result.get("success")
        ),
        "accounts_failed": total_errors,
        "total_transactions": sum(
            int(result.get("transactions_synced") or 0) for result in account_results
        ),
        "total_errors": total_errors,
        "errors": [
            str(result.get("message") or result)
            for result in account_results
            if not result.get("success")
        ],
    }

    logger.info(
        "Mono sync complete: %s accounts synced, %s transactions, %s errors",
        results.get("accounts_synced", 0),
        results.get("total_transactions", 0),
        results.get("total_errors", 0),
    )

    return results


@shared_task(
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def sync_mono_account(
    mono_account_id: str,
    *,
    refresh_first: bool = False,
    **_legacy_kwargs: Any,
) -> dict[str, Any]:
    """Incremental sync for a single Mono-linked account.

    Enqueued by the Mono webhook handler when an account transitions to
    ``data_status=AVAILABLE``, so freshly linked accounts get their first
    transaction pull without waiting for the next beat cycle.
    ``**_legacy_kwargs`` swallows any pre-refactor ``days_back`` kwarg.

    ``refresh_first`` is used by the scheduled sweep. ``/transactions`` reads
    Mono's *indexed cache*, which does not advance on its own — without a
    ``trigger_data_refresh`` the sweep re-reads the same stale cache every
    day and reports "nothing new" even when the bank has new lines. The
    scheduled path lost that trigger when it was refactored to fan out to
    this task (the only caller that still refreshed, ``sync_all_linked_accounts``,
    was left with no production callers), so the daily sweep quietly became a
    no-op. Refresh first to advance the cache, then pull: the refresh's own
    ``account_updated`` webhook drives ingest of anything newly scraped, and
    the pull drains whatever is already sitting in the cache — so ingest does
    not depend on the webhook arriving.

    Mono only fires ``account_updated`` once per scrape, so transient DB
    blips during webhook handling would otherwise lose the signal until the
    next beat cycle. ``OperationalError`` covers connection drops and
    deadlocks; persistent failures fall through to the next scheduled
    ``sync_mono_transactions`` run.
    """
    logger.info("Syncing Mono account %s", mono_account_id)

    # Mode 3 — we have the Mono account ID but not its owning org.
    # Resolve BankAccount.mono_account_id → bank_account.organization_id
    # under cross-org bypass, then sync under a session primed for that
    # org so all the GL/junction writes inside MonoSyncService hit a
    # tenant-scoped session.
    from app.db.session_context import cross_org_session, session_for_org
    from app.models.finance.banking.bank_account import BankAccount

    with cross_org_session() as cross_db:
        bank_account = cross_db.scalar(
            select(BankAccount).where(BankAccount.mono_account_id == mono_account_id)
        )
        if bank_account is None:
            logger.warning(
                "sync_mono_account: no BankAccount linked to mono_id=%s",
                mono_account_id,
            )
            return {"success": True, "skipped": True, "reason": "unlinked"}
        owning_org_id = bank_account.organization_id
        bank_account_id = bank_account.bank_account_id

    with session_for_org(owning_org_id) as db:
        from app.services.finance.banking.mono_client import MonoError
        from app.services.finance.banking.mono_sync import MonoSyncService

        sync_svc = MonoSyncService(db)
        if not sync_svc.is_configured():
            logger.info("Mono Connect not configured, skipping webhook sync")
            return {"success": True, "skipped": True}

        if refresh_first:
            account = db.get(BankAccount, bank_account_id)
            if account is not None:
                try:
                    sync_svc.sync_account_via_refresh(account)
                except (MonoError, ValueError, RuntimeError) as exc:
                    # A refresh failure must not block the cache pull below —
                    # there may be lines already indexed that we can still
                    # ingest. The error is recorded on the account by the
                    # service; carry on and try to drain the cache.
                    logger.warning(
                        "Mono refresh failed for mono_id=%s (%s); "
                        "continuing to cache pull",
                        mono_account_id,
                        exc,
                    )

        result = sync_svc.sync_by_mono_account_id(mono_account_id)
        db.commit()

    if not result.success:
        logger.warning(
            "Mono account sync failed: mono_id=%s message=%s errors=%s",
            mono_account_id,
            result.message,
            result.errors,
        )

    return {
        "success": result.success,
        "mono_account_id": mono_account_id,
        "transactions_synced": result.transactions_synced,
        "duplicates_skipped": result.duplicates_skipped,
        "message": result.message,
    }


@shared_task
def rebuild_account_balances() -> dict[str, Any]:
    """
    Safety-net: rebuild all account balances from posted_ledger_line.

    The primary balance update mechanism is event-driven (outbox_relay.py
    handles ledger.posting.completed → AccountBalanceService.update_balance_for_posting).
    This daily task catches any missed events by recalculating balances for
    all open fiscal periods across all active organizations.

    Returns:
        Dict with rebuild statistics
    """
    from app.models.finance.gl.fiscal_period import FiscalPeriod, PeriodStatus
    from app.services.finance.gl.account_balance import AccountBalanceService

    logger.info("Starting daily account balance rebuild (safety net)")

    results: dict[str, Any] = {
        "organizations_processed": 0,
        "periods_rebuilt": 0,
        "total_balance_records": 0,
        "errors": [],
    }

    for org_id in _list_active_organization_ids():
        with session_for_org(org_id) as db:
            try:
                # Get open/reopened periods for this org
                open_periods = db.scalars(
                    select(FiscalPeriod).where(
                        FiscalPeriod.organization_id == org_id,
                        FiscalPeriod.status.in_(
                            [PeriodStatus.OPEN, PeriodStatus.REOPENED]
                        ),
                    )
                ).all()

                if not open_periods:
                    continue

                results["organizations_processed"] += 1

                for period in open_periods:
                    try:
                        count = AccountBalanceService.rebuild_balances_for_period(
                            db,
                            organization_id=org_id,
                            fiscal_period_id=period.fiscal_period_id,
                        )
                        results["periods_rebuilt"] += 1
                        results["total_balance_records"] += count

                        logger.debug(
                            "Rebuilt %d balance records for org %s period %s",
                            count,
                            org_id,
                            period.fiscal_period_id,
                        )
                    except Exception as e:
                        logger.exception(
                            "Failed to rebuild balances for period %s (org %s)",
                            period.fiscal_period_id,
                            org_id,
                        )
                        results["errors"].append(
                            f"Period {period.fiscal_period_id}: {str(e)}"
                        )

            except Exception as e:
                logger.exception(
                    "Failed to process balance rebuild for org %s",
                    org_id,
                )
                results["errors"].append(f"Org {org_id}: {str(e)}")

            db.commit()

    logger.info(
        "Account balance rebuild complete: %d orgs, %d periods, %d records, %d errors",
        results["organizations_processed"],
        results["periods_rebuilt"],
        results["total_balance_records"],
        len(results["errors"]),
    )
    return results


@shared_task
def refresh_stale_balances(batch_size: int = 200) -> dict[str, Any]:
    """
    Process queued stale balance refresh entries.

    Runs frequently and refreshes only account/period keys invalidated by
    recent postings, keeping reporting aggregates current.
    """
    from app.services.finance.gl.balance_refresh import BalanceRefreshService

    # The queue no longer has to be probed across tenants to find out who has
    # work: `process_queue` takes no organization_id and reads the queue through
    # the session, so inside a tenant session it sees exactly that tenant's
    # pending entries and returns zeros for a tenant with none. The old
    # cross-tenant DISTINCT that fed this loop returned zero rows under
    # `app_user`, which made every balance look fresh and stopped the refresh
    # entirely — with no error to notice.
    #
    # `closing` because of the budget break below: leaving a generator
    # mid-iteration must close the tenant session it is holding open now, not
    # whenever the abandoned generator is collected. The break moved to the
    # BOTTOM of the body for the same reason — `for_each_organization` opens a
    # session before the body runs, so a top-of-body budget check would open one
    # session purely to discover the budget was already spent.
    results = {"processed": 0, "refreshed": 0, "errors": 0}
    remaining = batch_size
    with closing(for_each_organization(include_inactive=True)) as tenants:
        for _org_id, db in tenants:
            service = BalanceRefreshService(db)
            org_results = service.process_queue(batch_size=remaining)
            db.commit()
            results["processed"] += org_results["processed"]
            results["refreshed"] += org_results["refreshed"]
            results["errors"] += org_results["errors"]
            remaining -= org_results["processed"]
            if remaining <= 0:
                break

    if results["refreshed"] > 0 or results["errors"] > 0:
        logger.info(
            "Balance refresh: processed=%d refreshed=%d errors=%d",
            results["processed"],
            results["refreshed"],
            results["errors"],
        )
    return results


@shared_task
def release_expired_stock_reservations(batch_size: int = 200) -> dict[str, Any]:
    """Release inventory reservations that passed expiry timestamp."""
    from app.services.inventory.stock_reservation import StockReservationService

    # Same shape as `refresh_stale_balances`: `release_expired` scopes through
    # the session, so the cross-tenant DISTINCT that used to pick the orgs is
    # not needed — and under `app_user` it found nobody, leaving every expired
    # reservation holding stock forever.
    #
    # Same `closing` + bottom-of-body break as `refresh_stale_balances`, for the
    # same reason: the budget must not cost an extra tenant session, and
    # abandoning the generator must close the one it is holding.
    results = {"checked": 0, "released": 0, "errors": 0}
    remaining = batch_size
    with closing(for_each_organization(include_inactive=True)) as tenants:
        for _org_id, db in tenants:
            service = StockReservationService(db)
            org_results = service.release_expired(batch_size=remaining)
            db.commit()
            results["checked"] += org_results["checked"]
            results["released"] += org_results["released"]
            results["errors"] += org_results["errors"]
            remaining -= org_results["checked"]
            if remaining <= 0:
                break

    if results["released"] > 0 or results["errors"] > 0:
        logger.info(
            "Expired stock reservations: checked=%d released=%d errors=%d",
            results["checked"],
            results["released"],
            results["errors"],
        )
    return results


@shared_task
def refresh_analysis_cubes() -> dict[str, Any]:
    """Refresh due analysis cube materialized views."""
    from app.models.finance.rpt.analysis_cube import AnalysisCube
    from app.services.finance.rpt.analysis_cube import AnalysisCubeService

    with cross_org_session() as db:
        org_ids = list(
            db.scalars(
                select(AnalysisCube.organization_id)
                .where(AnalysisCube.is_active.is_(True))
                .distinct()
            ).all()
        )

    results = {"checked": 0, "refreshed": 0, "errors": 0}
    for org_id in org_ids:
        if org_id is None:
            with cross_org_session() as db:
                org_results = AnalysisCubeService(db).refresh_due_cubes()
                db.commit()
        else:
            with session_for_org(org_id) as db:
                org_results = AnalysisCubeService(db).refresh_due_cubes()
                db.commit()
        results["checked"] += org_results["checked"]
        results["refreshed"] += org_results["refreshed"]
        results["errors"] += org_results["errors"]

    if results["refreshed"] > 0 or results["errors"] > 0:
        logger.info(
            "Analysis cube refresh: checked=%d refreshed=%d errors=%d",
            results["checked"],
            results["refreshed"],
            results["errors"],
        )
    return results


@shared_task
def auto_generate_aging_snapshots(
    organization_id: str, fiscal_period_id: str, user_id: str
) -> dict[str, Any]:
    """
    Auto-generate AR + AP aging snapshots when a period is soft-closed.

    Deletes existing snapshots for the period first (unique constraint),
    then creates fresh snapshots from current outstanding balances.

    Args:
        organization_id: Organization UUID as string
        fiscal_period_id: Fiscal period UUID as string
        user_id: User who triggered the close

    Returns:
        Dict with snapshot generation statistics
    """
    from uuid import UUID as UUIDType

    logger.info(
        "Generating aging snapshots for period %s (org %s)",
        fiscal_period_id,
        organization_id,
    )

    results: dict[str, Any] = {
        "ar_snapshots": 0,
        "ap_snapshots": 0,
        "errors": [],
    }

    org_id = UUIDType(organization_id)
    period_id = UUIDType(fiscal_period_id)
    uid = UUIDType(user_id)

    with session_for_org(org_id) as db:
        from sqlalchemy import delete as sa_delete

        # AR aging snapshots — use savepoint so failure doesn't affect AP
        try:
            with db.begin_nested():
                from app.models.finance.ar.ar_aging_snapshot import ARAgingSnapshot
                from app.services.finance.ar.ar_aging import ARAgingService

                # Delete existing snapshots for this period (unique constraint)
                db.execute(
                    sa_delete(ARAgingSnapshot).where(
                        ARAgingSnapshot.organization_id == org_id,
                        ARAgingSnapshot.fiscal_period_id == period_id,
                    )
                )
                db.flush()

                ar_snaps = ARAgingService.create_aging_snapshot(
                    db,
                    organization_id=org_id,
                    fiscal_period_id=period_id,
                    created_by_user_id=uid,
                )
                results["ar_snapshots"] = len(ar_snaps)
        except Exception as e:
            logger.exception(
                "Failed to generate AR aging snapshots for period %s", period_id
            )
            results["errors"].append(f"AR: {e}")

        # AP aging snapshots — use savepoint so failure doesn't affect AR
        try:
            with db.begin_nested():
                from app.models.finance.ap.ap_aging_snapshot import (
                    APAgingSnapshot,  # pragma: allowlist secret
                )
                from app.services.finance.ap.ap_aging import (
                    APAgingService,  # pragma: allowlist secret
                )

                db.execute(
                    sa_delete(APAgingSnapshot).where(
                        APAgingSnapshot.organization_id == org_id,
                        APAgingSnapshot.fiscal_period_id == period_id,
                    )
                )
                db.flush()

                ap_snaps = APAgingService.create_aging_snapshot(
                    db,
                    organization_id=org_id,
                    fiscal_period_id=period_id,
                    created_by_user_id=uid,
                )
                results["ap_snapshots"] = len(ap_snaps)
        except Exception as e:
            logger.exception(
                "Failed to generate AP aging snapshots for period %s", period_id
            )
            results["errors"].append(f"AP: {e}")

        db.commit()  # commit whatever succeeded

    logger.info(
        "Aging snapshots complete: AR=%d, AP=%d, errors=%d",
        results["ar_snapshots"],
        results["ap_snapshots"],
        len(results["errors"]),
    )
    return results
