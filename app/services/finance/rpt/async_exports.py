"""Async report export helpers."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.finance.rpt.report_instance import ReportInstance, ReportStatus
from app.services.common import coerce_uuid
from app.services.finance.rpt.report_instance import (
    ReportGenerationRequest,
    ReportInstanceService,
)
from app.services.storage import get_storage


# Result sets at or below this row count are exported inline (synchronous CSV
# response); larger sets are queued to a background worker and the requester is
# emailed a download link when ready.
INLINE_EXPORT_ROW_THRESHOLD = 5000

EXPORT_DEFINITIONS = {
    "GENERAL_LEDGER": {
        "download_base": "/finance/reports/general-ledger/exports",
        "filename_prefix": "general_ledger",
        "label": "General Ledger",
        "task": "process_general_ledger_export",
        "formats": {"CSV", "PDF"},
    },
    "GL_JOURNALS": {
        "download_base": "/finance/gl/journals/exports",
        "filename_prefix": "gl_journals",
        "label": "GL Journals",
        "task": "process_gl_journals_export",
        "formats": {"CSV"},
    },
    "GL_LEDGER": {
        "download_base": "/finance/gl/ledger/exports",
        "filename_prefix": "gl_ledger",
        "label": "Ledger Transactions",
        "task": "process_gl_ledger_export",
        "formats": {"CSV"},
    },
    "AR_INVOICES": {
        "download_base": "/finance/ar/invoices/exports",
        "filename_prefix": "ar_invoices",
        "label": "AR Invoices",
        "task": "process_ar_invoices_export",
        "formats": {"CSV"},
    },
    "AR_RECEIPTS": {
        "download_base": "/finance/ar/receipts/exports",
        "filename_prefix": "ar_receipts",
        "label": "AR Receipts",
        "task": "process_ar_receipts_export",
        "formats": {"CSV"},
    },
    "AP_INVOICES": {
        "download_base": "/finance/ap/invoices/exports",
        "filename_prefix": "ap_invoices",
        "label": "AP Invoices",
        "task": "process_ap_invoices_export",
        "formats": {"CSV"},
    },
    "AP_PAYMENTS": {
        "download_base": "/finance/ap/payments/exports",
        "filename_prefix": "ap_payments",
        "label": "AP Payments",
        "task": "process_ap_payments_export",
        "formats": {"CSV"},
    },
}


def queue_background_export(
    db: Session,
    organization_id: UUID,
    user_id: UUID,
    *,
    report_code: str,
    parameters: dict[str, object | None],
    output_format: str,
) -> ReportInstance:
    """Create a queued report export instance and dispatch the worker."""
    code = report_code.upper()
    config = EXPORT_DEFINITIONS.get(code)
    if not config:
        raise HTTPException(status_code=400, detail="Unsupported export")

    fmt = output_format.upper()
    if fmt not in config["formats"]:
        raise HTTPException(status_code=400, detail="Unsupported export format")

    instance = ReportInstanceService.queue_report(
        db=db,
        organization_id=organization_id,
        request=ReportGenerationRequest(
            report_code=code,
            output_format=fmt,
            parameters=parameters,
        ),
        requested_by_user_id=user_id,
    )

    from app.tasks import finance as finance_tasks

    getattr(finance_tasks, str(config["task"])).delay(str(instance.instance_id))
    return instance


def queue_general_ledger_export(
    db: Session,
    organization_id: UUID,
    user_id: UUID,
    *,
    account_id: str | None,
    start_date: str | None,
    end_date: str | None,
    output_format: str,
) -> ReportInstance:
    """Create a queued General Ledger report instance and dispatch the worker."""
    return queue_background_export(
        db,
        organization_id,
        user_id,
        report_code="GENERAL_LEDGER",
        parameters={
            "account_id": account_id,
            "start_date": start_date,
            "end_date": end_date,
        },
        output_format=output_format,
    )


def get_completed_export_for_download(
    db: Session,
    organization_id: UUID,
    user_id: UUID,
    instance_id: str,
    *,
    report_code: str | None = None,
) -> tuple[object, str, str, int | None]:
    """Return a tenant/user-scoped generated report file for download."""
    org_id = coerce_uuid(organization_id)
    requested_by = coerce_uuid(user_id)
    inst_id = coerce_uuid(instance_id)

    instance = db.get(ReportInstance, inst_id)
    if (
        not instance
        or instance.organization_id != org_id
        or instance.generated_by_user_id != requested_by
    ):
        raise HTTPException(status_code=404, detail="Export not found")

    if instance.status != ReportStatus.COMPLETED or not instance.output_file_path:
        raise HTTPException(status_code=409, detail="Export is not ready")

    code = (report_code or "").upper()
    config = EXPORT_DEFINITIONS.get(code, EXPORT_DEFINITIONS["GENERAL_LEDGER"])
    fmt = instance.output_format.upper()
    suffix = "pdf" if fmt == "PDF" else "csv"
    media_type = "application/pdf" if fmt == "PDF" else "text/csv"
    filename = f"{config['filename_prefix']}_{instance.instance_id}.{suffix}"
    output_path = instance.output_file_path

    if not output_path.startswith("s3://"):
        raise HTTPException(status_code=404, detail="Export file not found")

    key = output_path.removeprefix("s3://")
    try:
        storage = get_storage()
        if not storage.exists(key):
            raise HTTPException(status_code=404, detail="Export file not found")
        chunks, content_type, content_length = storage.stream(key)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Report storage is temporarily unavailable"
        ) from exc
    return chunks, filename, content_type or media_type, content_length


def get_export_status(
    db: Session,
    organization_id: UUID,
    user_id: UUID,
    instance_id: str,
    *,
    report_code: str | None = None,
) -> dict[str, object]:
    """Return status for a queued export scoped to the requesting user."""
    org_id = coerce_uuid(organization_id)
    requested_by = coerce_uuid(user_id)
    inst_id = coerce_uuid(instance_id)

    instance = db.get(ReportInstance, inst_id)
    if (
        not instance
        or instance.organization_id != org_id
        or instance.generated_by_user_id != requested_by
    ):
        raise HTTPException(status_code=404, detail="Export not found")

    code = (report_code or "").upper()
    config = EXPORT_DEFINITIONS.get(code, EXPORT_DEFINITIONS["GENERAL_LEDGER"])

    download_url = None
    if instance.status == ReportStatus.COMPLETED:
        download_url = f"{config['download_base']}/{instance.instance_id}/download"

    return {
        "instance_id": str(instance.instance_id),
        "status": instance.status.value,
        "download_url": download_url,
        "error": instance.error_message,
    }
