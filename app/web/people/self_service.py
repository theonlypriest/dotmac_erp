"""
Self-service web routes for employees.

Thin wrappers around self-service web service.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.people.self_service_web import self_service_web_service
from app.web.deps import (
    WebAuthContext,
    get_db_for_org,
    require_self_service_access,
    require_self_service_discipline_manager,
    require_self_service_expense_approver,
    require_self_service_leave_approver,
)

router = APIRouter(prefix="/self", tags=["people-self-service"])


def _safe_form_text(value: object | None, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return default


def _safe_form_float(value: object | None) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_iso_date(value: object | None, field_name: str) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "type": "value_error.date",
                        "loc": ["body", field_name],
                        "msg": "Invalid date format (expected YYYY-MM-DD)",
                        "input": value,
                    }
                ],
            ) from exc
    return None


def _collect_indexed_rows(
    form,
    *,
    fields: list[str],
    file_fields: list[str] | None = None,
) -> list[dict[str, object]]:
    indexes: set[int] = set()
    file_fields = file_fields or []
    for key in form:
        for field_name in [*fields, *file_fields]:
            prefix = f"{field_name}_"
            if key.startswith(prefix):
                suffix = key[len(prefix) :]
                if suffix.isdigit():
                    indexes.add(int(suffix))
    rows: list[dict[str, object]] = []
    for index in sorted(indexes):
        row: dict[str, object] = {}
        for field_name in fields:
            if field_name in {
                "is_ongoing",
                "does_not_expire",
                "is_primary",
                "is_emergency_contact",
                "is_beneficiary",
            }:
                row[field_name] = bool(form.get(f"{field_name}_{index}"))
            else:
                row[field_name] = (
                    _safe_form_text(form.get(f"{field_name}_{index}")) or None
                )
        for field_name in file_fields:
            row["_upload"] = form.get(f"{field_name}_{index}")
        non_blank_values = [
            value
            for key, value in row.items()
            if key != "_upload" and value not in (None, "", False)
        ]
        has_upload = bool(getattr(row.get("_upload"), "filename", None))
        if non_blank_values or has_upload:
            rows.append(row)
    return rows


def _has_indexed_row_fields(
    form,
    *,
    fields: list[str],
    file_fields: list[str] | None = None,
) -> bool:
    for key in form:
        for field_name in [*fields, *(file_fields or [])]:
            prefix = f"{field_name}_"
            if key.startswith(prefix) and key[len(prefix) :].isdigit():
                return True
    return False


def require_self_service_profile_read(
    auth: WebAuthContext = Depends(require_self_service_access),
) -> WebAuthContext:
    if not auth.has_permission("selfservice:profile:read"):
        raise HTTPException(
            status_code=403,
            detail="Permission 'selfservice:profile:read' required",
        )
    return auth


def require_self_service_profile_update(
    auth: WebAuthContext = Depends(require_self_service_profile_read),
) -> WebAuthContext:
    if not auth.has_permission("selfservice:profile:update"):
        raise HTTPException(
            status_code=403,
            detail="Permission 'selfservice:profile:update' required",
        )
    return auth


def require_self_service_documents_read(
    auth: WebAuthContext = Depends(require_self_service_access),
) -> WebAuthContext:
    if not auth.has_permission("selfservice:documents:read"):
        raise HTTPException(
            status_code=403,
            detail="Permission 'selfservice:documents:read' required",
        )
    return auth


def require_self_service_documents_upload(
    auth: WebAuthContext = Depends(require_self_service_documents_read),
) -> WebAuthContext:
    if not auth.has_permission("selfservice:documents:upload"):
        raise HTTPException(
            status_code=403,
            detail="Permission 'selfservice:documents:upload' required",
        )
    return auth


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def self_service_index(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service landing page."""
    return self_service_web_service.index_response(request, auth, db)


@router.get("/attendance", response_class=HTMLResponse)
def my_attendance(
    request: Request,
    month: str | None = Query(None, description="Month in YYYY-MM format"),
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service attendance page with check-in/out actions."""
    return self_service_web_service.attendance_response(request, auth, db, month=month)


@router.get("/scheduling/schedules", response_class=HTMLResponse)
def my_schedules(
    request: Request,
    year_month: str | None = Query(None, description="Month in YYYY-MM format"),
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service schedule view."""
    return self_service_web_service.scheduling_schedules_response(
        request, auth, db, year_month=year_month
    )


@router.get("/scheduling/swaps", response_class=HTMLResponse)
def my_swap_requests(
    request: Request,
    year_month: str | None = Query(None, description="Month in YYYY-MM format"),
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service swap request page."""
    return self_service_web_service.scheduling_swaps_response(
        request, auth, db, year_month=year_month, page=page
    )


@router.post("/scheduling/swaps/request")
async def request_swap(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Create a shift swap request."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    return self_service_web_service.scheduling_create_swap_response(auth, db, form=form)


@router.post("/scheduling/swaps/{request_id}/accept")
async def accept_swap(
    request_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Accept a swap request as target employee."""
    return self_service_web_service.scheduling_accept_swap_response(
        auth, db, request_id=request_id
    )


@router.post("/scheduling/swaps/{request_id}/decline")
async def decline_swap(
    request_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Decline a swap request as target employee."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    return self_service_web_service.scheduling_decline_swap_response(
        auth, db, request_id=request_id, form=form
    )


@router.post("/scheduling/swaps/{request_id}/cancel")
async def cancel_swap(
    request_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Cancel a swap request as requester."""
    return self_service_web_service.scheduling_cancel_swap_response(
        auth, db, request_id=request_id
    )


@router.post("/attendance/check-in")
async def my_check_in(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Check in for the current employee."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    notes = _safe_form_text(form.get("notes")) or None
    latitude = _safe_form_float(form.get("latitude"))
    longitude = _safe_form_float(form.get("longitude"))
    return self_service_web_service.check_in_response(
        auth,
        db,
        notes=notes,
        latitude=latitude,
        longitude=longitude,
    )


@router.post("/attendance/check-out")
async def my_check_out(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Check out for the current employee."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    notes = _safe_form_text(form.get("notes")) or None
    latitude = _safe_form_float(form.get("latitude"))
    longitude = _safe_form_float(form.get("longitude"))
    return self_service_web_service.check_out_response(
        auth,
        db,
        notes=notes,
        latitude=latitude,
        longitude=longitude,
    )


@router.get("/leave", response_class=HTMLResponse)
def my_leave(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service leave page."""
    return self_service_web_service.leave_response(request, auth, db)


@router.get("/leave/{application_id}", response_class=HTMLResponse)
def my_leave_detail(
    application_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """View a leave application for the current employee."""
    return self_service_web_service.leave_detail_response(
        request,
        auth,
        db,
        application_id=application_id,
    )


@router.get("/tax-info", response_class=HTMLResponse)
def my_tax_info(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    """Self-service tax, bank, and personal info page."""
    return self_service_web_service.tax_info_response(
        request,
        auth,
        db,
        success=success,
        error=error,
    )


@router.get("/documents", response_class=HTMLResponse)
def my_documents(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    auth: WebAuthContext = Depends(require_self_service_documents_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.documents_response(
        request,
        auth,
        db,
        success=success,
        error=error,
    )


@router.get("/documents/new", response_class=HTMLResponse)
def new_my_document(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_documents_upload),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.document_upload_form_response(request, auth, db)


@router.post("/documents/new", response_class=HTMLResponse)
async def submit_my_document(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_documents_upload),
    db: Session = Depends(get_db_for_org),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    indexed_fields = [
        "document_type",
        "document_name",
        "description",
        "issue_date",
        "expiry_date",
    ]
    indexed_file_fields = ["file"]
    rows = _collect_indexed_rows(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    )
    if _has_indexed_row_fields(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    ):
        return self_service_web_service.submit_document_upload_batch_response(
            request,
            auth,
            db,
            rows=rows,
        )
    payload = {
        "document_type": _safe_form_text(form.get("document_type")) or None,
        "document_name": _safe_form_text(form.get("document_name")) or None,
        "description": _safe_form_text(form.get("description")) or None,
        "issue_date": _safe_form_text(form.get("issue_date")) or None,
        "expiry_date": _safe_form_text(form.get("expiry_date")) or None,
    }
    return self_service_web_service.submit_document_upload_response(
        request,
        auth,
        db,
        payload=payload,
        upload=form.get("file"),
    )


@router.get("/documents/{document_id}/download")
def download_my_document(
    document_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_documents_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.download_document_response(
        auth,
        db,
        document_id=document_id,
    )


@router.get("/documents/pending/{request_id}/download")
def download_my_pending_document(
    request_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_documents_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.download_pending_info_change_evidence_response(
        auth,
        db,
        request_id=request_id,
        require_owner_only=True,
    )


@router.get("/qualifications", response_class=HTMLResponse)
def my_qualifications(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    edit_id: UUID | None = None,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.extended_profile_response(
        request,
        auth,
        db,
        section="qualifications",
        success=success,
        error=error,
        edit_id=edit_id,
    )


@router.post("/qualifications")
async def submit_qualification(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_profile_update),
    db: Session = Depends(get_db_for_org),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    indexed_fields = [
        "qualification_type",
        "qualification_name",
        "field_of_study",
        "institution_name",
        "institution_location",
        "start_date",
        "end_date",
        "is_ongoing",
        "grade",
        "score",
        "max_score",
        "notes",
    ]
    indexed_file_fields = ["supporting_file"]
    rows = _collect_indexed_rows(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    )
    if _has_indexed_row_fields(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    ) and not _safe_form_text(form.get("qualification_id")):
        return self_service_web_service.submit_extended_profile_batch_response(
            request,
            auth,
            db,
            section="qualifications",
            rows=rows,
        )
    qualification_id = (
        coerce_uuid(_safe_form_text(form.get("qualification_id")))
        if _safe_form_text(form.get("qualification_id"))
        else None
    )
    payload = {
        "qualification_type": _safe_form_text(form.get("qualification_type")) or None,
        "qualification_name": _safe_form_text(form.get("qualification_name")) or None,
        "field_of_study": _safe_form_text(form.get("field_of_study")) or None,
        "institution_name": _safe_form_text(form.get("institution_name")) or None,
        "institution_location": _safe_form_text(form.get("institution_location"))
        or None,
        "start_date": _safe_form_text(form.get("start_date")) or None,
        "end_date": _safe_form_text(form.get("end_date")) or None,
        "is_ongoing": bool(form.get("is_ongoing")),
        "grade": _safe_form_text(form.get("grade")) or None,
        "score": _safe_form_text(form.get("score")) or None,
        "max_score": _safe_form_text(form.get("max_score")) or None,
        "notes": _safe_form_text(form.get("notes")) or None,
    }
    return self_service_web_service.submit_extended_profile_response(
        request,
        auth,
        db,
        section="qualifications",
        payload=payload,
        upload=form.get("supporting_file"),
        record_id=qualification_id,
    )


@router.get("/certifications", response_class=HTMLResponse)
def my_certifications(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    edit_id: UUID | None = None,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.extended_profile_response(
        request,
        auth,
        db,
        section="certifications",
        success=success,
        error=error,
        edit_id=edit_id,
    )


@router.post("/certifications")
async def submit_certification(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_profile_update),
    db: Session = Depends(get_db_for_org),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    indexed_fields = [
        "certification_name",
        "issuing_authority",
        "issue_date",
        "expiry_date",
        "does_not_expire",
        "credential_id",
        "credential_url",
        "notes",
    ]
    indexed_file_fields = ["supporting_file"]
    rows = _collect_indexed_rows(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    )
    if _has_indexed_row_fields(
        form,
        fields=indexed_fields,
        file_fields=indexed_file_fields,
    ) and not _safe_form_text(form.get("certification_id")):
        return self_service_web_service.submit_extended_profile_batch_response(
            request,
            auth,
            db,
            section="certifications",
            rows=rows,
        )
    certification_id = (
        coerce_uuid(_safe_form_text(form.get("certification_id")))
        if _safe_form_text(form.get("certification_id"))
        else None
    )
    payload = {
        "certification_name": _safe_form_text(form.get("certification_name")) or None,
        "issuing_authority": _safe_form_text(form.get("issuing_authority")) or None,
        "issue_date": _safe_form_text(form.get("issue_date")) or None,
        "expiry_date": _safe_form_text(form.get("expiry_date")) or None,
        "does_not_expire": bool(form.get("does_not_expire")),
        "credential_id": _safe_form_text(form.get("credential_id")) or None,
        "credential_url": _safe_form_text(form.get("credential_url")) or None,
        "notes": _safe_form_text(form.get("notes")) or None,
    }
    return self_service_web_service.submit_extended_profile_response(
        request,
        auth,
        db,
        section="certifications",
        payload=payload,
        upload=form.get("supporting_file"),
        record_id=certification_id,
    )


@router.get("/skills", response_class=HTMLResponse)
def my_skills(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    edit_id: UUID | None = None,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.extended_profile_response(
        request,
        auth,
        db,
        section="skills",
        success=success,
        error=error,
        edit_id=edit_id,
    )


@router.post("/skills")
async def submit_skill(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_profile_update),
    db: Session = Depends(get_db_for_org),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    indexed_fields = [
        "skill_id",
        "proficiency_level",
        "years_experience",
        "last_used_date",
        "is_primary",
        "notes",
    ]
    rows = _collect_indexed_rows(
        form,
        fields=indexed_fields,
    )
    if _has_indexed_row_fields(form, fields=indexed_fields) and not _safe_form_text(
        form.get("employee_skill_id")
    ):
        return self_service_web_service.submit_extended_profile_batch_response(
            request,
            auth,
            db,
            section="skills",
            rows=rows,
        )
    employee_skill_id = (
        coerce_uuid(_safe_form_text(form.get("employee_skill_id")))
        if _safe_form_text(form.get("employee_skill_id"))
        else None
    )
    payload = {
        "skill_id": _safe_form_text(form.get("skill_id")) or None,
        "proficiency_level": _safe_form_text(form.get("proficiency_level")) or None,
        "years_experience": _safe_form_text(form.get("years_experience")) or None,
        "last_used_date": _safe_form_text(form.get("last_used_date")) or None,
        "is_primary": bool(form.get("is_primary")),
        "notes": _safe_form_text(form.get("notes")) or None,
    }
    return self_service_web_service.submit_extended_profile_response(
        request,
        auth,
        db,
        section="skills",
        payload=payload,
        record_id=employee_skill_id,
    )


@router.get("/dependents", response_class=HTMLResponse)
def my_dependents(
    request: Request,
    success: str | None = None,
    error: str | None = None,
    edit_id: UUID | None = None,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.extended_profile_response(
        request,
        auth,
        db,
        section="dependents",
        success=success,
        error=error,
        edit_id=edit_id,
    )


@router.post("/dependents")
async def submit_dependent(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_profile_update),
    db: Session = Depends(get_db_for_org),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    indexed_fields = [
        "full_name",
        "relationship",
        "date_of_birth",
        "gender",
        "phone",
        "email",
        "address",
        "is_emergency_contact",
        "emergency_contact_priority",
        "is_beneficiary",
        "beneficiary_percentage",
        "notes",
    ]
    rows = _collect_indexed_rows(
        form,
        fields=indexed_fields,
    )
    if _has_indexed_row_fields(form, fields=indexed_fields) and not _safe_form_text(
        form.get("dependent_id")
    ):
        return self_service_web_service.submit_extended_profile_batch_response(
            request,
            auth,
            db,
            section="dependents",
            rows=rows,
        )
    dependent_id = (
        coerce_uuid(_safe_form_text(form.get("dependent_id")))
        if _safe_form_text(form.get("dependent_id"))
        else None
    )
    payload = {
        "full_name": _safe_form_text(form.get("full_name")) or None,
        "relationship": _safe_form_text(form.get("relationship")) or None,
        "date_of_birth": _safe_form_text(form.get("date_of_birth")) or None,
        "gender": _safe_form_text(form.get("gender")) or None,
        "phone": _safe_form_text(form.get("phone")) or None,
        "email": _safe_form_text(form.get("email")) or None,
        "address": _safe_form_text(form.get("address")) or None,
        "is_emergency_contact": bool(form.get("is_emergency_contact")),
        "emergency_contact_priority": _safe_form_text(
            form.get("emergency_contact_priority")
        )
        or None,
        "is_beneficiary": bool(form.get("is_beneficiary")),
        "beneficiary_percentage": _safe_form_text(form.get("beneficiary_percentage"))
        or None,
        "notes": _safe_form_text(form.get("notes")) or None,
    }
    return self_service_web_service.submit_extended_profile_response(
        request,
        auth,
        db,
        section="dependents",
        payload=payload,
        record_id=dependent_id,
    )


@router.get("/info-change-evidence/{request_id}")
def download_my_pending_info_change_evidence(
    request_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_profile_read),
    db: Session = Depends(get_db_for_org),
):
    return self_service_web_service.download_pending_info_change_evidence_response(
        auth,
        db,
        request_id=request_id,
        require_owner_only=True,
    )


@router.post("/tax-info")
async def update_tax_info(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_profile_update),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Submit a change request for tax, bank, and personal info."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    payload = {
        "phone": _safe_form_text(form.get("phone")) or None,
        "date_of_birth": _coerce_iso_date(form.get("date_of_birth"), "date_of_birth"),
        "gender": _safe_form_text(form.get("gender")) or None,
        "address_line1": _safe_form_text(form.get("address_line1")) or None,
        "address_line2": _safe_form_text(form.get("address_line2")) or None,
        "city": _safe_form_text(form.get("city")) or None,
        "region": _safe_form_text(form.get("region")) or None,
        "postal_code": _safe_form_text(form.get("postal_code")) or None,
        "country_code": _safe_form_text(form.get("country_code")) or None,
        "personal_email": _safe_form_text(form.get("personal_email")) or None,
        "personal_phone": _safe_form_text(form.get("personal_phone")) or None,
        "emergency_contact_name": _safe_form_text(form.get("emergency_contact_name"))
        or None,
        "emergency_contact_phone": _safe_form_text(form.get("emergency_contact_phone"))
        or None,
        "bank_name": _safe_form_text(form.get("bank_name")) or None,
        "bank_account_number": _safe_form_text(form.get("bank_account_number")) or None,
        "bank_account_name": _safe_form_text(form.get("bank_account_name")) or None,
        "bank_branch_code": _safe_form_text(form.get("bank_branch_code")) or None,
        "tin": _safe_form_text(form.get("tin")) or None,
        "tax_state": _safe_form_text(form.get("tax_state")) or None,
        "rsa_pin": _safe_form_text(form.get("rsa_pin")) or None,
        "pfa_code": _safe_form_text(form.get("pfa_code")) or None,
        "nhf_number": _safe_form_text(form.get("nhf_number")) or None,
    }

    return self_service_web_service.tax_info_submit_response(
        auth,
        db,
        payload=payload,
    )


@router.post("/leave/{application_id}/cancel")
def cancel_leave(
    application_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Cancel a leave application for the current employee."""
    return self_service_web_service.leave_cancel_response(
        auth,
        db,
        application_id=application_id,
    )


@router.post("/leave/apply")
async def apply_leave(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
    leave_type_id: str | None = Form(default=None),
    from_date: date | None = Form(default=None),
    to_date: date | None = Form(default=None),
    half_day: str | None = Form(default=None),
    reason: str | None = Form(default=None),
) -> RedirectResponse:
    """Submit a leave application for the current employee."""
    if leave_type_id is None or from_date is None or to_date is None:
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if leave_type_id is None:
                leave_type_id = payload.get("leave_type_id")
            if from_date is None:
                from_date = _coerce_iso_date(payload.get("from_date"), "from_date")
            if to_date is None:
                to_date = _coerce_iso_date(payload.get("to_date"), "to_date")
            if half_day is None and "half_day" in payload:
                half_day_value = payload.get("half_day")
                if isinstance(half_day_value, bool):
                    half_day = "on" if half_day_value else None
                elif half_day_value is not None:
                    half_day = str(half_day_value)
            if reason is None and "reason" in payload:
                reason = payload.get("reason")
        else:
            form = getattr(request.state, "csrf_form", None)
            if form is None:
                try:
                    form = await request.form()
                except Exception:
                    form = None
            if form is not None:
                if leave_type_id is None:
                    leave_type_id = _safe_form_text(form.get("leave_type_id")) or None
                if from_date is None:
                    from_date = _coerce_iso_date(form.get("from_date"), "from_date")
                if to_date is None:
                    to_date = _coerce_iso_date(form.get("to_date"), "to_date")
                if half_day is None:
                    half_day = _safe_form_text(form.get("half_day")) or None
                if reason is None:
                    reason = _safe_form_text(form.get("reason")) or None

    missing_fields: list[str] = []
    if not leave_type_id:
        missing_fields.append("leave_type_id")
    if not from_date:
        missing_fields.append("from_date")
    if not to_date:
        missing_fields.append("to_date")
    if missing_fields:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "missing",
                    "loc": ["body", field],
                    "msg": "Field required",
                    "input": None,
                }
                for field in missing_fields
            ],
        )

    return self_service_web_service.leave_apply_response(
        auth,
        db,
        leave_type_id=leave_type_id,
        from_date=from_date,
        to_date=to_date,
        half_day=half_day,
        reason=reason,
    )


@router.get("/payslips", response_class=HTMLResponse)
def my_payslips(
    request: Request,
    year: int | None = Query(None, description="Filter by year"),
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service payslips page."""
    return self_service_web_service.payslips_response(
        request, auth, db, year=year, page=page
    )


@router.get("/payslips/{slip_id}", response_class=HTMLResponse)
def my_payslip_detail(
    slip_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """View a payslip for the current employee."""
    return self_service_web_service.payslip_detail_response(
        request,
        auth,
        db,
        slip_id=slip_id,
    )


@router.get("/expenses", response_class=HTMLResponse)
def my_expenses(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service expenses page."""
    return self_service_web_service.expenses_response(request, auth, db)


@router.get("/tickets", response_class=HTMLResponse)
def my_tickets(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db_for_org),
):
    """Self-service tickets page."""
    return self_service_web_service.tickets_response(request, auth, db, page=page)


@router.post("/tickets")
async def create_ticket(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Create a self-service support ticket."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    return self_service_web_service.ticket_create_response(
        request,
        auth,
        db,
        subject=_safe_form_text(form.get("subject")),
        description=_safe_form_text(form.get("description")) or None,
        priority=_safe_form_text(form.get("priority"), "MEDIUM"),
        category_id=_safe_form_text(form.get("category_id")) or None,
    )


@router.get("/tasks", response_class=HTMLResponse)
def my_tasks(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db_for_org),
):
    """Self-service tasks page."""
    return self_service_web_service.tasks_response(request, auth, db, page=page)


@router.post("/expenses/claims")
async def create_expense_claim(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Create an expense claim with one or more line items."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    claim_date_str = _safe_form_text(form.get("claim_date"))
    purpose = _safe_form_text(form.get("purpose"))
    expense_date_str = _safe_form_text(form.get("expense_date"))
    recipient_bank_code = _safe_form_text(form.get("recipient_bank_code"))
    recipient_bank_name = _safe_form_text(form.get("recipient_bank_name"))
    recipient_account_number = _safe_form_text(form.get("recipient_account_number"))
    recipient_name = _safe_form_text(form.get("recipient_name"))
    requested_approver_id = _safe_form_text(form.get("requested_approver_id"))
    receipt_url = _safe_form_text(form.get("receipt_url"))
    receipt_number = _safe_form_text(form.get("receipt_number"))
    receipt_files = form.getlist("receipt_file")
    receipt_file = form.get("receipt_file")
    submit_now = form.get("submit_now")
    project_id = _safe_form_text(form.get("project_id"))
    ticket_id = _safe_form_text(form.get("ticket_id"))
    task_id = _safe_form_text(form.get("task_id"))
    vehicle_id = _safe_form_text(form.get("vehicle_id"))
    cost_center_id = _safe_form_text(form.get("cost_center_id"))

    if (
        receipt_file
        and getattr(receipt_file, "filename", None)
        and receipt_file not in receipt_files
    ):
        receipt_files.append(receipt_file)

    item_keys = [key for key in form.getlist("item_key") if _safe_form_text(key)]
    items: list[dict] = []
    if item_keys:
        for item_key in item_keys:
            category_id = _safe_form_text(form.get(f"category_id_{item_key}"))
            description = _safe_form_text(form.get(f"description_{item_key}"))
            claimed_amount = _safe_form_text(form.get(f"claimed_amount_{item_key}"))

            if not all(
                [
                    category_id,
                    description,
                    claimed_amount,
                ]
            ):
                raise HTTPException(
                    status_code=400, detail="Missing required item fields"
                )

            try:
                amount = Decimal(claimed_amount)
            except (InvalidOperation, TypeError) as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid claimed amount"
                ) from exc

            items.append(
                {
                    "category_id": category_id,
                    "description": description,
                    "claimed_amount": amount,
                }
            )
    else:
        category_id = _safe_form_text(form.get("category_id"))
        description = _safe_form_text(form.get("description"))
        claimed_amount = _safe_form_text(form.get("claimed_amount"))

        if not all(
            [
                category_id,
                description,
                claimed_amount,
            ]
        ):
            raise HTTPException(status_code=400, detail="Missing required item fields")

        try:
            amount = Decimal(claimed_amount)
        except (InvalidOperation, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid claimed amount"
            ) from exc

        items.append(
            {
                "category_id": category_id,
                "description": description,
                "claimed_amount": amount,
            }
        )

    if not all(
        [
            claim_date_str,
            purpose,
            expense_date_str,
            recipient_bank_code,
            recipient_account_number,
            requested_approver_id,
        ]
    ):
        raise HTTPException(status_code=400, detail="Missing required fields")

    try:
        claim_date = date.fromisoformat(claim_date_str) if claim_date_str else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid claim date") from exc

    try:
        expense_date = (
            date.fromisoformat(expense_date_str) if expense_date_str else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid expense date") from exc

    if not claim_date:
        raise HTTPException(status_code=400, detail="Invalid dates submitted")
    if not expense_date:
        raise HTTPException(status_code=400, detail="Invalid expense date")

    return self_service_web_service.expense_claim_create_response(
        auth,
        db,
        claim_date=claim_date,
        purpose=purpose,
        expense_date=expense_date,
        items=items,
        recipient_bank_code=recipient_bank_code or None,
        recipient_bank_name=recipient_bank_name or None,
        recipient_account_number=recipient_account_number or None,
        recipient_name=recipient_name or None,
        requested_approver_id=requested_approver_id or None,
        receipt_url=receipt_url or None,
        receipt_number=receipt_number or None,
        receipt_files=receipt_files,
        submit_now=submit_now,
        project_id=project_id or None,
        ticket_id=ticket_id or None,
        task_id=task_id or None,
        vehicle_id=vehicle_id or None,
        cost_center_id=cost_center_id or None,
    )


@router.get("/expenses/claims/{claim_id}/edit", response_class=HTMLResponse)
def edit_expense_claim(
    claim_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Edit a draft expense claim."""
    return self_service_web_service.expense_claim_edit_response(
        request,
        auth,
        db,
        claim_id=claim_id,
    )


@router.post("/expenses/claims/{claim_id}/edit")
async def update_expense_claim(
    claim_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Update items on a draft expense claim."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    item_ids = [_safe_form_text(item_id) for item_id in form.getlist("item_id")]
    item_ids = [item_id for item_id in item_ids if item_id]
    item_keys = [_safe_form_text(item_key) for item_key in form.getlist("item_key")]
    item_keys = [item_key for item_key in item_keys if item_key]
    if not item_ids and not item_keys:
        raise HTTPException(status_code=400, detail="No claim items submitted")

    recipient_bank_code = _safe_form_text(form.get("recipient_bank_code"))
    recipient_bank_name = _safe_form_text(form.get("recipient_bank_name"))
    recipient_account_number = _safe_form_text(form.get("recipient_account_number"))
    recipient_name = _safe_form_text(form.get("recipient_name"))
    requested_approver_id = _safe_form_text(form.get("requested_approver_id"))
    if (
        not recipient_bank_code
        or not recipient_account_number
        or not requested_approver_id
    ):
        raise HTTPException(
            status_code=400,
            detail="Bank code, account number, and expense approver are required",
        )

    # Extract optional project/ticket/task/vehicle/cost center linkage
    project_id_str = _safe_form_text(form.get("project_id"))
    ticket_id_str = _safe_form_text(form.get("ticket_id"))
    task_id_str = _safe_form_text(form.get("task_id"))
    vehicle_id_str = _safe_form_text(form.get("vehicle_id"))
    cost_center_id_str = _safe_form_text(form.get("cost_center_id"))

    project_id = UUID(project_id_str) if project_id_str else None
    ticket_id = UUID(ticket_id_str) if ticket_id_str else None
    task_id = UUID(task_id_str) if task_id_str else None
    vehicle_id = UUID(vehicle_id_str) if vehicle_id_str else None
    cost_center_id = UUID(cost_center_id_str) if cost_center_id_str else None

    items = []
    for item_id in item_ids:
        remove = form.get(f"remove_item_{item_id}")
        if remove:
            items.append({"item_id": item_id, "remove": True})
            continue

        expense_date_str = _safe_form_text(form.get(f"expense_date_{item_id}"))
        category_id = _safe_form_text(form.get(f"category_id_{item_id}"))
        description = _safe_form_text(form.get(f"description_{item_id}"))
        claimed_amount_str = _safe_form_text(form.get(f"claimed_amount_{item_id}"))
        receipt_number = _safe_form_text(form.get(f"receipt_number_{item_id}"))
        receipt_url = _safe_form_text(form.get(f"receipt_url_{item_id}"))

        if not all([expense_date_str, category_id, description, claimed_amount_str]):
            raise HTTPException(status_code=400, detail="Missing required item fields")

        try:
            expense_date = date.fromisoformat(expense_date_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid expense date") from exc

        try:
            claimed_amount = Decimal(claimed_amount_str)
        except (InvalidOperation, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid claimed amount"
            ) from exc

        items.append(
            {
                "item_id": item_id,
                "expense_date": expense_date,
                "category_id": category_id,
                "description": description,
                "claimed_amount": claimed_amount,
                "receipt_number": receipt_number or None,
                "receipt_url": receipt_url or None,
            }
        )

    for item_key in item_keys:
        expense_date_str = _safe_form_text(form.get(f"expense_date_{item_key}"))
        category_id = _safe_form_text(form.get(f"category_id_{item_key}"))
        description = _safe_form_text(form.get(f"description_{item_key}"))
        claimed_amount_str = _safe_form_text(form.get(f"claimed_amount_{item_key}"))
        receipt_number = _safe_form_text(form.get(f"receipt_number_{item_key}"))
        receipt_url = _safe_form_text(form.get(f"receipt_url_{item_key}"))

        if not all([expense_date_str, category_id, description, claimed_amount_str]):
            raise HTTPException(status_code=400, detail="Missing required item fields")

        try:
            expense_date = date.fromisoformat(expense_date_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid expense date") from exc

        try:
            claimed_amount = Decimal(claimed_amount_str)
        except (InvalidOperation, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid claimed amount"
            ) from exc

        items.append(
            {
                "expense_date": expense_date,
                "category_id": category_id,
                "description": description,
                "claimed_amount": claimed_amount,
                "receipt_number": receipt_number or None,
                "receipt_url": receipt_url or None,
            }
        )

    return self_service_web_service.expense_claim_update_response(
        auth,
        db,
        claim_id=claim_id,
        items=items,
        recipient_bank_code=recipient_bank_code or None,
        recipient_bank_name=recipient_bank_name or None,
        recipient_account_number=recipient_account_number or None,
        recipient_name=recipient_name or None,
        requested_approver_id=coerce_uuid(requested_approver_id)
        if requested_approver_id
        else None,
        project_id=project_id,
        ticket_id=ticket_id,
        task_id=task_id,
        vehicle_id=vehicle_id,
        cost_center_id=cost_center_id,
    )


@router.post("/expenses/claims/{claim_id}/submit")
async def submit_expense_claim(
    claim_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Submit a draft expense claim for approval."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    return self_service_web_service.expense_claim_submit_response(
        auth,
        db,
        claim_id=claim_id,
    )


@router.post("/expenses/claims/{claim_id}/delete")
async def delete_expense_claim(
    claim_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Delete a draft expense claim owned by the current employee."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    return self_service_web_service.expense_claim_delete_response(
        auth,
        db,
        claim_id=claim_id,
    )


@router.get("/team/leave", response_class=HTMLResponse)
def team_leave_requests(
    request: Request,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_leave_approver),
    db: Session = Depends(get_db_for_org),
):
    """Team leave approvals for direct reports."""
    return self_service_web_service.team_leave_response(
        request,
        auth,
        db,
        status=status,
        page=page,
    )


@router.post("/team/leave/{application_id}/approve")
def approve_team_leave(
    application_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_leave_approver),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Approve a direct report leave request."""
    return self_service_web_service.team_leave_approve_response(
        auth,
        db,
        application_id=application_id,
    )


@router.post("/team/leave/{application_id}/reject")
def reject_team_leave(
    application_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_leave_approver),
    db: Session = Depends(get_db_for_org),
    reason: str | None = Form(default=None),
) -> RedirectResponse:
    """Reject a direct report leave request."""
    return self_service_web_service.team_leave_reject_response(
        auth,
        db,
        application_id=application_id,
        reason=reason,
    )


@router.get("/team/expenses", response_class=HTMLResponse)
def team_expense_requests(
    request: Request,
    status: str | None = None,
    decision: str | None = None,
    employee_id: UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_expense_approver),
    db: Session = Depends(get_db_for_org),
):
    """Expense approvals history for current approver."""
    return self_service_web_service.team_expenses_response(
        request,
        auth,
        db,
        status=status,
        decision=decision,
        employee_id=employee_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
    )


@router.get("/my-approvals", response_class=HTMLResponse)
def my_expense_approvals(
    request: Request,
    status: str | None = None,
    decision: str | None = None,
    employee_id: UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_expense_approver),
    db: Session = Depends(get_db_for_org),
):
    """My approved/rejected expense decisions."""
    return self_service_web_service.team_expenses_response(
        request,
        auth,
        db,
        status=status,
        decision=decision,
        employee_id=employee_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
    )


@router.post("/team/expenses/{claim_id}/approve")
def approve_team_expense(
    claim_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_expense_approver),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Approve a direct report expense claim."""
    return self_service_web_service.team_expense_approve_response(
        auth,
        db,
        claim_id=claim_id,
    )


@router.post("/team/expenses/{claim_id}/reject")
def reject_team_expense(
    claim_id: UUID,
    auth: WebAuthContext = Depends(require_self_service_expense_approver),
    db: Session = Depends(get_db_for_org),
    reason: str | None = Form(default=None),
) -> RedirectResponse:
    """Reject a direct report expense claim."""
    return self_service_web_service.team_expense_reject_response(
        auth,
        db,
        claim_id=claim_id,
        reason=reason,
    )


# =============================================================================
# Discipline Self-Service Routes
# =============================================================================


@router.get("/discipline", response_class=HTMLResponse)
def my_discipline_cases(
    request: Request,
    include_closed: bool = Query(default=False),
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """Self-service discipline cases page - view my disciplinary cases."""
    return self_service_web_service.discipline_cases_response(
        request, auth, db, include_closed=include_closed
    )


@router.get("/discipline/{case_id}", response_class=HTMLResponse)
def my_discipline_case_detail(
    case_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
):
    """View details of a specific disciplinary case."""
    return self_service_web_service.discipline_case_detail_response(
        request, auth, db, case_id=case_id
    )


@router.post("/discipline/{case_id}/respond")
async def submit_discipline_response(
    case_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Submit employee response to a disciplinary query."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    response_text = _safe_form_text(form.get("response_text"))
    if not response_text:
        raise HTTPException(status_code=400, detail="Response text is required")

    return self_service_web_service.discipline_submit_response(
        auth, db, case_id=case_id, response_text=response_text
    )


@router.post("/discipline/{case_id}/appeal")
async def file_discipline_appeal(
    case_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """File an appeal against a disciplinary decision."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    appeal_reason = _safe_form_text(form.get("appeal_reason"))
    if not appeal_reason:
        raise HTTPException(status_code=400, detail="Appeal reason is required")

    return self_service_web_service.discipline_file_appeal_response(
        auth, db, case_id=case_id, appeal_reason=appeal_reason
    )


@router.get("/team/discipline", response_class=HTMLResponse)
def team_discipline_cases(
    request: Request,
    include_closed: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    auth: WebAuthContext = Depends(require_self_service_discipline_manager),
    db: Session = Depends(get_db_for_org),
):
    """List disciplinary cases for direct reports."""
    return self_service_web_service.team_discipline_cases_response(
        request=request,
        auth=auth,
        db=db,
        include_closed=include_closed,
        page=page,
    )


@router.get("/team/discipline/new", response_class=HTMLResponse)
def team_discipline_new_case_form(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_discipline_manager),
    db: Session = Depends(get_db_for_org),
):
    """Render form to create a discipline case for a direct report."""
    return self_service_web_service.team_discipline_new_form_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.post("/team/discipline/new")
async def team_discipline_create_case(
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_discipline_manager),
    db: Session = Depends(get_db_for_org),
):
    """Create a discipline case for a direct report and issue query."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    employee_id = _safe_form_text(form.get("employee_id"))
    violation_type = _safe_form_text(form.get("violation_type"))
    severity = _safe_form_text(form.get("severity"))
    subject = _safe_form_text(form.get("subject"))
    description = _safe_form_text(form.get("description")) or None
    incident_date = _safe_form_text(form.get("incident_date")) or None
    query_text = _safe_form_text(form.get("query_text"))
    response_due_date = _safe_form_text(form.get("response_due_date"))

    return self_service_web_service.team_discipline_create_case_response(
        request=request,
        auth=auth,
        db=db,
        employee_id=employee_id,
        violation_type=violation_type,
        severity=severity,
        subject=subject,
        description=description,
        incident_date=incident_date,
        query_text=query_text,
        response_due_date=response_due_date,
    )


@router.get("/team/discipline/{case_id}", response_class=HTMLResponse)
def team_discipline_case_detail(
    case_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_discipline_manager),
    db: Session = Depends(get_db_for_org),
):
    """View a team discipline case for a direct report."""
    return self_service_web_service.team_discipline_case_detail_response(
        request=request,
        auth=auth,
        db=db,
        case_id=case_id,
    )


@router.post("/team/discipline/{case_id}/issue-query")
async def team_discipline_issue_query(
    case_id: UUID,
    request: Request,
    auth: WebAuthContext = Depends(require_self_service_discipline_manager),
    db: Session = Depends(get_db_for_org),
) -> RedirectResponse:
    """Issue query on a team discipline case."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    query_text = _safe_form_text(form.get("query_text"))
    response_due_date = _safe_form_text(form.get("response_due_date"))
    return self_service_web_service.team_discipline_issue_query_response(
        auth=auth,
        db=db,
        case_id=case_id,
        query_text=query_text,
        response_due_date=response_due_date,
    )
