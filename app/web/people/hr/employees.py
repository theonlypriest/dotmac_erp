"""Employee CRUD and management routes."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.services.people.hr.web import hr_web_service
from app.web.deps import get_db_for_org, WebAuthContext, require_hr_access

router = APIRouter(tags=["employees"])


@router.get("/employees", response_class=HTMLResponse)
def list_employees(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    department_id: str | None = None,
    designation_id: str | None = None,
    date_of_joining_from: str | None = None,
    date_of_joining_to: str | None = None,
    date_of_leaving_from: str | None = None,
    date_of_leaving_to: str | None = None,
    filters: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=200),
    success: str | None = None,
    error: str | None = None,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Employee list page."""
    return hr_web_service.list_employees_response(
        request,
        auth,
        db,
        search,
        status,
        department_id,
        designation_id,
        date_of_joining_from,
        date_of_joining_to,
        date_of_leaving_from,
        date_of_leaving_to,
        filters,
        page,
        limit,
        success,
        error,
    )


@router.get("/employees/export", response_class=Response)
def export_employees(
    fields: list[str] = Query(default=[]),
    status: str | None = None,
    department_id: str | None = None,
    designation_id: str | None = None,
    date_of_joining_from: str | None = None,
    date_of_joining_to: str | None = None,
    include_archived: bool = False,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Download a configurable, tenant-scoped employee CSV export."""
    return hr_web_service.export_employees_csv_response(
        auth=auth,
        db=db,
        fields=fields,
        status=status,
        department_id=department_id,
        designation_id=designation_id,
        date_of_joining_from=date_of_joining_from,
        date_of_joining_to=date_of_joining_to,
        include_archived=include_archived,
    )


@router.get("/employees/org-chart", include_in_schema=False)
def view_org_chart_redirect() -> RedirectResponse:
    return RedirectResponse(url="/people/hr/org-chart", status_code=301)


@router.get("/employees/stats")
def employee_stats(
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Employee stats endpoint for dashboards."""
    return hr_web_service.employee_stats_response(auth, db)


@router.get("/employees/position-options", response_class=HTMLResponse)
def employee_position_options(
    selected_position_id: str | None = None,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Lazy-load vacant position options for employee forms."""
    html = hr_web_service.employee_position_options_response(
        auth,
        db,
        selected_position_id=selected_position_id,
    )
    return Response(content=html, media_type="text/html")


@router.get("/employees/new", response_class=HTMLResponse)
def new_employee_form(
    request: Request,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """New employee form page."""
    return hr_web_service.employee_new_form_response(request, auth, db)


@router.post("/employees/new")
async def create_employee(
    request: Request,
    background_tasks: BackgroundTasks,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Handle new employee form submission."""
    return await hr_web_service.create_employee_response(
        request=request,
        auth=auth,
        db=db,
        background_tasks=background_tasks,
    )


@router.get("/employees/{employee_id}", response_class=HTMLResponse)
def view_employee(
    request: Request,
    employee_id: UUID,
    saved: str | None = None,
    invite_status: str | None = None,
    invite_recipient_kind: str | None = None,
    invite_recipient_email: str | None = None,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Employee detail page."""
    return hr_web_service.employee_detail_response(
        request,
        auth,
        db,
        str(employee_id),
        saved=bool(saved),
        invite_status=invite_status,
        invite_recipient_kind=invite_recipient_kind,
        invite_recipient_email=invite_recipient_email,
    )


@router.get("/employees/{employee_id}/edit", response_class=HTMLResponse)
def edit_employee_form(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Edit employee form page."""
    return hr_web_service.employee_edit_form_response(
        request, auth, db, str(employee_id)
    )


@router.post("/employees/{employee_id}/edit")
async def update_employee(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Handle employee update form submission."""
    return await hr_web_service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/activate")
def activate_employee(
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Activate an employee."""
    return hr_web_service.activate_employee_response(
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/resend-invite")
def resend_employee_invite(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Resend employee access invite."""
    return hr_web_service.resend_employee_invite_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/suspend")
async def suspend_employee(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Suspend an employee."""
    return await hr_web_service.suspend_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/on-leave")
def set_employee_on_leave(
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Set an employee on leave."""
    return hr_web_service.set_employee_on_leave_response(
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/resign")
async def resign_employee(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Record employee resignation."""
    return await hr_web_service.resign_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/rehire")
async def rehire_employee(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Rehire a previously separated employee."""
    return await hr_web_service.rehire_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/terminate")
async def terminate_employee(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Terminate an employee."""
    return await hr_web_service.terminate_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/final-payroll")
async def update_final_payroll(
    request: Request,
    employee_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Update final payroll settings for an exited employee."""
    return await hr_web_service.update_final_payroll_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db,
    )


@router.post("/employees/{employee_id}/credentials/{credential_id}/toggle")
async def toggle_employee_credential(
    request: Request,
    employee_id: UUID,
    credential_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Enable/disable a user's login credential from the employee record."""
    return await hr_web_service.toggle_user_credential_response(
        request=request,
        employee_id=employee_id,
        credential_id=credential_id,
        auth=auth,
        db=db,
    )
