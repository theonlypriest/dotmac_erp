"""
Admin web routes.

Provides admin dashboard and management pages with admin role requirement.
"""

from html import escape
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.services.admin.settings_web import admin_settings_web_service
from app.services.admin.web import admin_web_service
from app.services.branding_assets import delete_branding_asset, save_branding_asset
from app.services.hooks.web import service_hook_web_service
from app.templates import templates
from app.web.deps import (
    WebAuthContext,
    get_db,
    optional_web_auth,
    require_admin_access,
    resolve_brand_context,
)

# Authorization is enforced once, at the router, so it cannot be forgotten on a
# new route. Every handler below still takes ``optional_web_auth`` for its
# template context; that dependency does not gate anything, and until this
# router-level guard was added nothing else did either — any authenticated user
# of any role could read and POST the admin settings pages, credential forms
# included.
router = APIRouter(
    prefix="/admin",
    tags=["admin-web"],
    dependencies=[Depends(require_admin_access)],
)


def _normalize_form(form: Any) -> dict[str, str]:
    if form is None:
        return {}
    return {key: value if isinstance(value, str) else "" for key, value in form.items()}


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin dashboard page."""
    return admin_web_service.dashboard_response(request, db, auth)


@router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin users list page."""
    return admin_web_service.users_response(request, db, auth, page, search, status)


@router.get("/users/new", response_class=HTMLResponse)
def admin_users_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create user form."""
    return admin_web_service.users_new_response(request, db, auth)


@router.post("/users/new", response_class=HTMLResponse)
async def admin_users_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create user form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    roles = form.getlist("roles") if hasattr(form, "getlist") else []
    form = _normalize_form(form)

    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    email = (form.get("email") or "").strip()
    username = (form.get("username") or "").strip()
    organization_id = (form.get("organization_id") or "").strip()
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""
    display_name = (form.get("display_name") or "").strip()
    phone = (form.get("phone") or "").strip()
    status = (form.get("status") or "active").strip()
    must_change_password = form.get("must_change_password") or ""

    return admin_web_service.users_create_response(
        request,
        db,
        auth,
        first_name,
        last_name,
        email,
        username,
        organization_id,
        password,
        password_confirm,
        display_name,
        phone,
        status,
        must_change_password,
        roles,
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
def admin_users_view(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View user details (same as edit)."""
    return admin_web_service.users_view_response(request, db, auth, user_id)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def admin_users_edit(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit user form."""
    return admin_web_service.users_edit_response(request, db, auth, user_id)


@router.post("/users/{user_id}/edit", response_class=HTMLResponse)
async def admin_users_update(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit user form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    roles = form.getlist("roles") if hasattr(form, "getlist") else []
    form = _normalize_form(form)

    first_name = (form.get("first_name") or "").strip()
    last_name = (form.get("last_name") or "").strip()
    email = (form.get("email") or "").strip()
    username = (form.get("username") or "").strip()
    organization_id = (form.get("organization_id") or "").strip()
    password = form.get("password") or ""
    password_confirm = form.get("password_confirm") or ""
    display_name = (form.get("display_name") or "").strip()
    phone = (form.get("phone") or "").strip()
    status = (form.get("status") or "active").strip()
    must_change_password = form.get("must_change_password") or ""
    email_verified = form.get("email_verified") or ""

    return admin_web_service.users_update_response(
        request,
        db,
        auth,
        user_id,
        first_name,
        last_name,
        email,
        username,
        organization_id,
        password,
        password_confirm,
        display_name,
        phone,
        status,
        must_change_password,
        email_verified,
        roles,
    )


@router.post("/users/{user_id}/delete")
def admin_users_delete(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a user."""
    return admin_web_service.users_delete_response(request, db, auth, user_id)


@router.post("/users/{user_id}/activate")
def admin_users_activate(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Reactivate a user account."""
    return admin_web_service.users_activate_response(request, db, auth, user_id)


@router.get("/roles", response_class=HTMLResponse)
def admin_roles(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin roles management page."""
    return admin_web_service.roles_response(request, db, auth, page, search, status)


@router.get("/roles/new", response_class=HTMLResponse)
def admin_roles_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create role form."""
    return admin_web_service.roles_new_response(request, db, auth)


@router.post("/roles/new", response_class=HTMLResponse)
async def admin_roles_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create role form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)

    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    is_active = form.get("is_active") or ""
    permissions = form.getlist("permissions") if hasattr(form, "getlist") else []

    return admin_web_service.roles_create_response(
        request,
        db,
        auth,
        name,
        description,
        is_active,
        permissions,
    )


@router.get("/roles/{role_id}", response_class=HTMLResponse)
def admin_roles_view(
    request: Request,
    role_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View role details (same as edit)."""
    return admin_web_service.roles_view_response(request, db, auth, role_id)


@router.get("/roles/{role_id}/edit", response_class=HTMLResponse)
def admin_roles_edit(
    request: Request,
    role_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit role form."""
    return admin_web_service.roles_edit_response(request, db, auth, role_id)


@router.post("/roles/{role_id}/edit", response_class=HTMLResponse)
async def admin_roles_update(
    request: Request,
    role_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit role form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()

    permissions = form.getlist("permissions") if hasattr(form, "getlist") else []
    form = _normalize_form(form)

    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()
    is_active = form.get("is_active") or ""

    return admin_web_service.roles_update_response(
        request,
        db,
        auth,
        role_id,
        name,
        description,
        is_active,
        permissions,
    )


@router.post("/roles/{role_id}/delete")
def admin_roles_delete(
    request: Request,
    role_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a role."""
    return admin_web_service.roles_delete_response(request, db, auth, role_id)


@router.get("/permissions", response_class=HTMLResponse)
def admin_permissions(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin permissions management page."""
    return admin_web_service.permissions_response(
        request, db, auth, page, search, status
    )


@router.get("/permissions/new", response_class=HTMLResponse)
def admin_permissions_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create permission form."""
    return admin_web_service.permissions_new_response(request, db, auth)


@router.post("/permissions/new", response_class=HTMLResponse)
async def admin_permissions_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create permission form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)
    request.state.csrf_form = form

    key = (form.get("key") or "").strip()
    description = (form.get("description") or "").strip()
    is_active = form.get("is_active") or ""

    return admin_web_service.permissions_create_response(
        request,
        db,
        auth,
        key,
        description,
        is_active,
    )


@router.get("/permissions/{permission_id}", response_class=HTMLResponse)
def admin_permissions_view(
    request: Request,
    permission_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View permission details (same as edit)."""
    return admin_web_service.permissions_view_response(request, db, auth, permission_id)


@router.get("/permissions/{permission_id}/edit", response_class=HTMLResponse)
def admin_permissions_edit(
    request: Request,
    permission_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit permission form."""
    return admin_web_service.permissions_edit_response(request, db, auth, permission_id)


@router.post("/permissions/{permission_id}/edit", response_class=HTMLResponse)
async def admin_permissions_update(
    request: Request,
    permission_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit permission form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)
    request.state.csrf_form = form

    key = (form.get("key") or "").strip()
    description = (form.get("description") or "").strip()
    is_active = form.get("is_active") or ""

    return admin_web_service.permissions_update_response(
        request,
        db,
        auth,
        permission_id,
        key,
        description,
        is_active,
    )


@router.post("/permissions/{permission_id}/delete")
def admin_permissions_delete(
    request: Request,
    permission_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a permission."""
    return admin_web_service.permissions_delete_response(
        request, db, auth, permission_id
    )


@router.get("/organizations", response_class=HTMLResponse)
def admin_organizations(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin organizations management page."""
    return admin_web_service.organizations_response(
        request, db, auth, page, search, status
    )


@router.get("/organizations/new", response_class=HTMLResponse)
def admin_organizations_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create organization form."""
    return admin_web_service.organizations_new_response(request, db, auth)


@router.post("/organizations/new", response_class=HTMLResponse)
def admin_organizations_create(
    request: Request,
    organization_code: str = Form(...),
    legal_name: str = Form(...),
    slug: str = Form(default=""),
    functional_currency_code: str = Form(...),
    presentation_currency_code: str = Form(...),
    fiscal_year_end_month: int = Form(...),
    fiscal_year_end_day: int = Form(...),
    trading_name: str = Form(default=""),
    registration_number: str = Form(default=""),
    tax_identification_number: str = Form(default=""),
    incorporation_date: str = Form(default=""),
    jurisdiction_country_code: str = Form(default=""),
    parent_organization_id: str = Form(default=""),
    consolidation_method: str = Form(default=""),
    ownership_percentage: str = Form(default=""),
    is_active: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create organization form submission."""
    return admin_web_service.organizations_create_response(
        request,
        db,
        auth,
        organization_code,
        legal_name,
        slug,
        functional_currency_code,
        presentation_currency_code,
        fiscal_year_end_month,
        fiscal_year_end_day,
        trading_name,
        registration_number,
        tax_identification_number,
        incorporation_date,
        jurisdiction_country_code,
        parent_organization_id,
        consolidation_method,
        ownership_percentage,
        is_active,
    )


@router.get("/organizations/{org_id}", response_class=HTMLResponse)
def admin_organizations_view(
    request: Request,
    org_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View organization details (same as edit)."""
    return admin_web_service.organizations_view_response(request, db, auth, org_id)


@router.get("/organizations/{org_id}/edit", response_class=HTMLResponse)
def admin_organizations_edit(
    request: Request,
    org_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit organization form."""
    return admin_web_service.organizations_edit_response(request, db, auth, org_id)


@router.post("/organizations/{org_id}/edit", response_class=HTMLResponse)
def admin_organizations_update(
    request: Request,
    org_id: str,
    organization_code: str = Form(...),
    legal_name: str = Form(...),
    slug: str = Form(default=""),
    functional_currency_code: str = Form(...),
    presentation_currency_code: str = Form(...),
    fiscal_year_end_month: int = Form(...),
    fiscal_year_end_day: int = Form(...),
    trading_name: str = Form(default=""),
    registration_number: str = Form(default=""),
    tax_identification_number: str = Form(default=""),
    incorporation_date: str = Form(default=""),
    jurisdiction_country_code: str = Form(default=""),
    parent_organization_id: str = Form(default=""),
    consolidation_method: str = Form(default=""),
    ownership_percentage: str = Form(default=""),
    is_active: str = Form(default=""),
    salaries_expense_account_id: str = Form(default=""),
    salary_payable_account_id: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit organization form submission."""
    return admin_web_service.organizations_update_response(
        request,
        db,
        auth,
        org_id,
        organization_code,
        legal_name,
        slug,
        functional_currency_code,
        presentation_currency_code,
        fiscal_year_end_month,
        fiscal_year_end_day,
        trading_name,
        registration_number,
        tax_identification_number,
        incorporation_date,
        jurisdiction_country_code,
        parent_organization_id,
        consolidation_method,
        ownership_percentage,
        is_active,
        salaries_expense_account_id,
        salary_payable_account_id,
    )


@router.post("/organizations/{org_id}/delete")
def admin_organizations_delete(
    request: Request,
    org_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete an organization."""
    return admin_web_service.organizations_delete_response(request, db, auth, org_id)


@router.get("/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    domain: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin system settings page."""
    return admin_web_service.settings_response(
        request, db, auth, page, search, status, domain
    )


@router.get("/settings/new", response_class=HTMLResponse)
def admin_settings_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create setting form."""
    return admin_web_service.settings_new_response(request, db, auth)


@router.post("/settings/new", response_class=HTMLResponse)
async def admin_settings_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create setting form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)

    domain = (form.get("domain") or "").strip()
    key = (form.get("key") or "").strip()
    value_type = (form.get("value_type") or "").strip()
    value = form.get("value") or ""
    is_secret = form.get("is_secret") or ""
    is_active = form.get("is_active") or ""
    if not (domain and key and value_type):
        content_type = (request.headers.get("content-type") or "").lower()
        if content_type.startswith("application/json"):
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                domain = domain or str(payload.get("domain") or "").strip()
                key = key or str(payload.get("key") or "").strip()
                value_type = value_type or str(payload.get("value_type") or "").strip()
                if not value:
                    value = payload.get("value") or ""
                if not is_secret:
                    is_secret = payload.get("is_secret") or ""
                if not is_active:
                    is_active = payload.get("is_active") or ""

    return admin_web_service.settings_create_response(
        request,
        db,
        auth,
        domain,
        key,
        value_type,
        value,
        is_secret,
        is_active,
    )


@router.get("/audit-logs", response_class=HTMLResponse)
def admin_audit_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    actor_type: str = Query(default=""),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin audit logs page."""
    return admin_web_service.audit_logs_response(
        request,
        db,
        auth,
        page,
        search,
        status,
        actor_type,
        start_date,
        end_date,
    )


@router.get("/data-changes", response_class=HTMLResponse)
def admin_data_changes(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    module: str = Query(default=""),
    entity: str = Query(default=""),
    action: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin data changes audit trail page."""
    return admin_web_service.data_changes_response(
        request,
        db,
        auth,
        page,
        module,
        entity,
        action,
        search,
    )


@router.get("/tasks", response_class=HTMLResponse)
def admin_tasks(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin scheduled tasks page."""
    return admin_web_service.tasks_response(request, db, auth, page, search, status)


@router.get("/system/health", response_class=HTMLResponse)
def admin_system_health(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin System section infrastructure health page."""
    return admin_web_service.system_health_response(request, db, auth)


@router.get("/system/health/alerts", response_class=HTMLResponse)
def admin_system_alerts(
    request: Request,
    category: str = Query(default=""),
    severity: str = Query(default=""),
    status: str = Query(default=""),
    period: str = Query(default="7d"),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin System section infrastructure alerts page."""
    return admin_web_service.infrastructure_alerts_response(
        request, db, auth, category, severity, status, period
    )


@router.get("/system/health/alerts/{alert_id}", response_class=HTMLResponse)
def admin_system_alert_detail(
    request: Request,
    alert_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin System section infrastructure alert detail page."""
    return admin_web_service.infrastructure_alert_detail_response(
        request, db, auth, alert_id
    )


@router.get("/tasks/new", response_class=HTMLResponse)
def admin_tasks_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show create task form."""
    return admin_web_service.tasks_new_response(request, db, auth)


@router.post("/tasks/new", response_class=HTMLResponse)
def admin_tasks_create(
    request: Request,
    name: str = Form(...),
    task_name: str = Form(...),
    schedule_type: str = Form(...),
    interval_seconds: int = Form(...),
    args_json: str = Form(default=""),
    kwargs_json: str = Form(default=""),
    enabled: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle create task form submission."""
    return admin_web_service.tasks_create_response(
        request,
        db,
        auth,
        name,
        task_name,
        schedule_type,
        interval_seconds,
        args_json,
        kwargs_json,
        enabled,
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def admin_tasks_view(
    request: Request,
    task_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View task details (same as edit)."""
    return admin_web_service.tasks_view_response(request, db, auth, task_id)


@router.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def admin_tasks_edit(
    request: Request,
    task_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit task form."""
    return admin_web_service.tasks_edit_response(request, db, auth, task_id)


@router.post("/tasks/{task_id}/edit", response_class=HTMLResponse)
def admin_tasks_update(
    request: Request,
    task_id: str,
    name: str = Form(...),
    task_name: str = Form(...),
    schedule_type: str = Form(...),
    interval_seconds: int = Form(...),
    args_json: str = Form(default=""),
    kwargs_json: str = Form(default=""),
    enabled: str = Form(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit task form submission."""
    return admin_web_service.tasks_update_response(
        request,
        db,
        auth,
        task_id,
        name,
        task_name,
        schedule_type,
        interval_seconds,
        args_json,
        kwargs_json,
        enabled,
    )


@router.post("/tasks/{task_id}/delete")
def admin_tasks_delete(
    request: Request,
    task_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a task."""
    return admin_web_service.tasks_delete_response(request, db, auth, task_id)


# ========== Settings Hub ==========


def _admin_base_context(
    request: Request, auth: WebAuthContext, page_title: str, db: Session
) -> dict:
    """Build base context for admin settings pages."""
    csrf_token = str(getattr(request.state, "csrf_token", "") or "")
    if not isinstance(getattr(request.state, "csrf_form", None), str):
        request.state.csrf_form = (
            f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">'
            if csrf_token
            else ""
        )

    organization = None
    if auth and auth.is_authenticated and auth.organization_id:
        from app.models.finance.core_org.organization import Organization

        organization = db.get(Organization, auth.organization_id)
    context = {
        "request": request,
        "user": auth.user if auth and auth.is_authenticated else {},
        "page_title": page_title,
        "active_page": "settings",
        "brand": resolve_brand_context(
            db,
            organization,
            auth.organization_id if auth and auth.is_authenticated else None,
        ),
    }
    return context


@router.get("/settings/hub", response_class=HTMLResponse)
def admin_settings_hub(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Admin settings hub page."""
    context = _admin_base_context(request, auth, "Settings", db)
    context.update(
        admin_settings_web_service.get_hub_context(
            auth.organization_id if auth else None
        )
    )
    return templates.TemplateResponse(request, "admin/settings/index.html", context)


@router.get("/settings/integrations", response_class=HTMLResponse)
def admin_settings_integrations(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Unified integration control-plane hub."""
    if not auth or not auth.is_authenticated or not auth.organization_id:
        return RedirectResponse(
            url="/login?next=/admin/settings/integrations", status_code=303
        )
    context = _admin_base_context(request, auth, "Integrations", db)
    context.update(
        admin_settings_web_service.get_integrations_context(db, auth.organization_id)
    )
    return templates.TemplateResponse(
        request, "admin/settings/integrations.html", context
    )


@router.get("/settings/organization", response_class=HTMLResponse)
def admin_settings_organization(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Organization profile settings page."""
    if not auth.is_authenticated or not auth.organization_id:
        return RedirectResponse(
            url="/login?next=/admin/settings/organization", status_code=303
        )

    context = _admin_base_context(request, auth, "Organization Profile", db)
    context.update(
        admin_settings_web_service.get_organization_context(db, auth.organization_id)
    )
    return templates.TemplateResponse(
        request, "admin/settings/organization.html", context
    )


@router.post("/settings/organization", response_class=HTMLResponse)
async def admin_settings_organization_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update organization profile."""
    if not auth.is_authenticated or not auth.organization_id:
        return RedirectResponse(
            url="/login?next=/admin/settings/organization", status_code=303
        )

    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = _normalize_form(form)

    success, error = admin_settings_web_service.update_organization(
        db, auth.organization_id, data
    )
    if not success:
        context = _admin_base_context(request, auth, "Organization Profile", db)
        context.update(
            admin_settings_web_service.get_organization_context(
                db, auth.organization_id
            )
        )
        context["error"] = error
        return templates.TemplateResponse(
            request, "admin/settings/organization.html", context
        )

    return RedirectResponse(url="/admin/settings/organization?saved=1", status_code=303)


@router.get("/settings/branding", response_class=HTMLResponse)
def admin_settings_branding(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Branding settings page."""
    context = _admin_base_context(request, auth, "Branding", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_branding_context(db, auth.organization_id)
        )
    return templates.TemplateResponse(request, "admin/settings/branding.html", context)


@router.post("/settings/branding", response_class=HTMLResponse)
async def admin_settings_branding_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update branding settings."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = dict(form)

    if auth and auth.organization_id:
        logo_file = form.get("logo_file")
        favicon_file = form.get("favicon_file")
        logo_dark_file = form.get("logo_dark_file")
        remove_logo = str(form.get("remove_logo") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        remove_logo_dark = str(form.get("remove_logo_dark") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        remove_favicon = str(form.get("remove_favicon") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            context = admin_settings_web_service.get_branding_context(
                db, auth.organization_id
            )
            branding = context.get("branding")
            organization = context.get("organization")

            existing_logo = None
            if branding and getattr(branding, "logo_url", None):
                existing_logo = branding.logo_url
            elif organization and getattr(organization, "logo_url", None):
                existing_logo = organization.logo_url

            existing_logo_dark = None
            if branding and getattr(branding, "logo_dark_url", None):
                existing_logo_dark = branding.logo_dark_url

            existing_favicon = None
            if branding and getattr(branding, "favicon_url", None):
                existing_favicon = branding.favicon_url

            if isinstance(logo_file, UploadFile) and logo_file.filename:
                uploaded_url = await save_branding_asset(
                    logo_file, str(auth.organization_id), "logo"
                )
                data["logo_url"] = uploaded_url
                if existing_logo and existing_logo != uploaded_url:
                    delete_branding_asset(existing_logo)
            elif remove_logo:
                data["logo_url"] = ""
                if existing_logo:
                    delete_branding_asset(existing_logo)

            if isinstance(favicon_file, UploadFile) and favicon_file.filename:
                uploaded_favicon = await save_branding_asset(
                    favicon_file, str(auth.organization_id), "favicon"
                )
                data["favicon_url"] = uploaded_favicon
                if existing_favicon and existing_favicon != uploaded_favicon:
                    delete_branding_asset(existing_favicon)
            elif remove_favicon:
                data["favicon_url"] = ""
                if existing_favicon:
                    delete_branding_asset(existing_favicon)

            if isinstance(logo_dark_file, UploadFile) and logo_dark_file.filename:
                uploaded_dark = await save_branding_asset(
                    logo_dark_file, str(auth.organization_id), "logo_dark"
                )
                data["logo_dark_url"] = uploaded_dark
                if existing_logo_dark and existing_logo_dark != uploaded_dark:
                    delete_branding_asset(existing_logo_dark)
            elif remove_logo_dark:
                data["logo_dark_url"] = ""
                if existing_logo_dark:
                    delete_branding_asset(existing_logo_dark)
        except Exception as exc:
            context = _admin_base_context(request, auth, "Branding", db)
            context.update(
                admin_settings_web_service.get_branding_context(
                    db, auth.organization_id
                )
            )
            context["error"] = str(getattr(exc, "detail", exc))
            return templates.TemplateResponse(
                request, "admin/settings/branding.html", context
            )

    if auth and auth.organization_id:
        effective_logo = data.get("logo_url")
        if effective_logo is None:
            context = admin_settings_web_service.get_branding_context(
                db, auth.organization_id
            )
            branding = context.get("branding")
            organization = context.get("organization")
            if branding and getattr(branding, "logo_url", None):
                effective_logo = branding.logo_url
            elif organization and getattr(organization, "logo_url", None):
                effective_logo = organization.logo_url

        if effective_logo:
            data["email_logo_url"] = effective_logo
            data["report_logo_url"] = effective_logo
        elif "logo_url" in data and not data.get("logo_url"):
            data["email_logo_url"] = ""
            data["report_logo_url"] = ""
        success, error = admin_settings_web_service.update_branding(
            db, auth.organization_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Branding", db)
            context.update(
                admin_settings_web_service.get_branding_context(
                    db, auth.organization_id
                )
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/branding.html", context
            )

    return RedirectResponse(url="/admin/settings/branding?success=1", status_code=303)


@router.get("/settings/email", response_class=HTMLResponse)
def admin_settings_email(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Email configuration page."""
    context = _admin_base_context(request, auth, "Email Configuration", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_email_context(db, auth.organization_id)
        )
    return templates.TemplateResponse(request, "admin/settings/email.html", context)


@router.post("/settings/email", response_class=HTMLResponse)
async def admin_settings_email_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update email settings."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = dict(form)

    if auth and auth.organization_id:
        success, error = admin_settings_web_service.update_email(
            db, auth.organization_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Email Configuration", db)
            context.update(
                admin_settings_web_service.get_email_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/email.html", context
            )

    return RedirectResponse(url="/admin/settings/email?success=1", status_code=303)


@router.post("/settings/email/test", response_class=HTMLResponse)
async def admin_settings_email_test(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Test email settings without saving changes."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = dict(form)
    target = str(data.get("smtp_test_target") or "global")

    context = _admin_base_context(request, auth, "Email Configuration", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_email_context(db, auth.organization_id)
        )
        success, message = admin_settings_web_service.test_email(
            db, auth.organization_id, data, target
        )
        context["smtp_test"] = {
            "success": success,
            "message": message,
        }

    return templates.TemplateResponse(request, "admin/settings/email.html", context)


@router.get("/settings/features", response_class=HTMLResponse)
def admin_settings_features(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Feature flags page."""
    context = _admin_base_context(request, auth, "Feature Flags", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_features_context(db, auth.organization_id)
        )
    return templates.TemplateResponse(request, "admin/settings/features.html", context)


@router.post("/settings/features/{feature_key}/toggle", response_class=HTMLResponse)
async def admin_settings_feature_toggle(
    request: Request,
    feature_key: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Toggle a feature flag."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    enabled = str(form.get("enabled", "false")).lower() == "true"

    if auth and auth.organization_id:
        success, error = admin_settings_web_service.toggle_feature(
            db, auth.organization_id, feature_key, enabled
        )
        if not success:
            context = _admin_base_context(request, auth, "Feature Flags", db)
            context.update(
                admin_settings_web_service.get_features_context(
                    db, auth.organization_id
                )
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/features.html", context
            )

    return RedirectResponse(url="/admin/settings/features?saved=1", status_code=303)


@router.get("/settings/features/new", response_class=HTMLResponse)
def admin_settings_feature_new(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Form to register a new feature flag."""
    from app.models.feature_flag import FeatureFlagCategory

    context = _admin_base_context(request, auth, "New Feature Flag", db)
    context["categories"] = [c.value for c in FeatureFlagCategory]
    context["form_data"] = {}
    return templates.TemplateResponse(
        request, "admin/settings/feature_form.html", context
    )


@router.post("/settings/features/create", response_class=HTMLResponse)
async def admin_settings_feature_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Create a new feature flag from form submission."""
    from app.models.feature_flag import FeatureFlagCategory
    from app.services.feature_flag_service import FeatureFlagService

    form = await request.form()
    flag_key = str(form.get("flag_key", "")).strip()
    label = str(form.get("label", "")).strip()
    description = str(form.get("description", "")).strip()
    category = str(form.get("category", "MODULE"))
    default_enabled = str(form.get("default_enabled", "false")).lower() == "true"
    owner = str(form.get("owner", "")).strip() or None

    # Validation
    errors = []
    if not flag_key:
        errors.append("Flag key is required")
    if not label:
        errors.append("Label is required")

    if errors:
        context = _admin_base_context(request, auth, "New Feature Flag", db)
        context["categories"] = [c.value for c in FeatureFlagCategory]
        context["form_data"] = dict(form)
        context["error"] = "; ".join(errors)
        return templates.TemplateResponse(
            request, "admin/settings/feature_form.html", context
        )

    service = FeatureFlagService(db)
    try:
        cat = FeatureFlagCategory(category)
    except ValueError:
        cat = FeatureFlagCategory.MODULE

    person_id = auth.person_id if auth else None
    try:
        service.register_flag(
            flag_key=flag_key,
            label=label,
            description=description,
            category=cat,
            default_enabled=default_enabled,
            owner=owner,
            created_by_id=person_id,
        )
        db.commit()
    except Exception as e:
        context = _admin_base_context(request, auth, "New Feature Flag", db)
        context["categories"] = [c.value for c in FeatureFlagCategory]
        context["form_data"] = dict(form)
        context["error"] = str(e)
        return templates.TemplateResponse(
            request, "admin/settings/feature_form.html", context
        )

    return RedirectResponse(url="/admin/settings/features?success=1", status_code=303)


@router.post("/settings/features/{feature_key}/archive", response_class=HTMLResponse)
async def admin_settings_feature_archive(
    request: Request,
    feature_key: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Archive a feature flag."""
    from app.services.feature_flag_service import FeatureFlagService

    service = FeatureFlagService(db)
    service.archive_flag(feature_key)
    db.commit()
    return RedirectResponse(url="/admin/settings/features?success=1", status_code=303)


@router.get("/settings/service-hooks", response_class=HTMLResponse)
def admin_settings_service_hooks(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Service hook settings page."""
    context = _admin_base_context(request, auth, "Service Hooks", db)
    if auth and auth.organization_id:
        q = request.query_params.get("q")
        handler_type = request.query_params.get("handler_type")
        is_active = request.query_params.get("is_active")
        context.update(
            service_hook_web_service.settings_context_filtered(
                db,
                auth.organization_id,
                q=q,
                handler_type=handler_type,
                is_active=is_active,
            )
        )
        bulk_action = request.query_params.get("bulk_action")
        if bulk_action:
            try:
                requested = int(request.query_params.get("bulk_requested", "0"))
            except ValueError:
                requested = 0
            try:
                processed = int(request.query_params.get("bulk_processed", "0"))
            except ValueError:
                processed = 0
            try:
                skipped = int(request.query_params.get("bulk_skipped", "0"))
            except ValueError:
                skipped = max(0, requested - processed)
            context["bulk_result"] = {
                "action": bulk_action,
                "requested": requested,
                "processed": processed,
                "skipped": skipped,
            }
        detail_hook_id = request.query_params.get("detail_hook_id")
        detail_execution_id = request.query_params.get("detail_execution_id")
        if detail_hook_id and detail_execution_id:
            detail, detail_error = service_hook_web_service.execution_detail(
                db,
                auth.organization_id,
                detail_hook_id,
                detail_execution_id,
            )
            context["selected_execution"] = detail
            if detail_error:
                context["error"] = detail_error
    return templates.TemplateResponse(
        request, "admin/settings/service_hooks.html", context
    )


@router.post("/settings/service-hooks", response_class=HTMLResponse)
async def admin_settings_service_hooks_create(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Create a service hook."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = _normalize_form(form)

    if auth and auth.organization_id:
        success, error = service_hook_web_service.create_from_form(
            db,
            auth.organization_id,
            auth.person_id if auth else None,
            data,
        )
        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

    return RedirectResponse(
        url="/admin/settings/service-hooks?saved=1", status_code=303
    )


@router.post("/settings/service-hooks/{hook_id}/toggle", response_class=HTMLResponse)
async def admin_settings_service_hooks_toggle(
    request: Request,
    hook_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Enable or disable a service hook."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    enabled = str(form.get("enabled", "false")).lower() == "true"

    if auth and auth.organization_id:
        success, error = service_hook_web_service.toggle(
            db, auth.organization_id, hook_id, enabled
        )
        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

    return RedirectResponse(
        url="/admin/settings/service-hooks?saved=1", status_code=303
    )


@router.post("/settings/service-hooks/bulk-action", response_class=HTMLResponse)
async def admin_settings_service_hooks_bulk_action(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Apply a bulk action to selected service hooks."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    q = str(form.get("q") or "").strip()
    handler_type = str(form.get("handler_type") or "").strip()
    is_active = str(form.get("is_active") or "").strip()
    base_params: dict[str, str] = {}
    if q:
        base_params["q"] = q
    if handler_type:
        base_params["handler_type"] = handler_type
    if is_active:
        base_params["is_active"] = is_active
    action = str(form.get("bulk_action") or "").strip().lower()
    hook_ids = list(form.getlist("hook_ids")) if hasattr(form, "getlist") else []
    if not hook_ids:
        query = urlencode(base_params)
        return RedirectResponse(
            url=f"/admin/settings/service-hooks?{query}"
            if query
            else "/admin/settings/service-hooks",
            status_code=303,
        )

    if auth and auth.organization_id:
        if action == "enable":
            success, error, result = service_hook_web_service.bulk_toggle(
                db,
                auth.organization_id,
                hook_ids,
                enabled=True,
            )
        elif action == "disable":
            success, error, result = service_hook_web_service.bulk_toggle(
                db,
                auth.organization_id,
                hook_ids,
                enabled=False,
            )
        elif action == "delete":
            success, error, result = service_hook_web_service.bulk_delete(
                db,
                auth.organization_id,
                hook_ids,
            )
        else:
            success, error, result = False, "Invalid bulk action.", None

        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

        if result:
            requested = _coerce_int(result.get("requested"), len(hook_ids))
            processed_raw = result.get("updated", result.get("deleted", 0))
            processed = _coerce_int(processed_raw, 0)
            skipped = max(0, requested - processed)
            params = dict(base_params)
            params.update(
                {
                    "saved": "1",
                    "bulk_action": action,
                    "bulk_requested": str(requested),
                    "bulk_processed": str(processed),
                    "bulk_skipped": str(skipped),
                }
            )
            query = urlencode(params)
            return RedirectResponse(
                url=f"/admin/settings/service-hooks?{query}",
                status_code=303,
            )

    params = dict(base_params)
    params["saved"] = "1"
    query = urlencode(params)
    return RedirectResponse(
        url=f"/admin/settings/service-hooks?{query}", status_code=303
    )


@router.post("/settings/service-hooks/{hook_id}/delete", response_class=HTMLResponse)
async def admin_settings_service_hooks_delete(
    request: Request,
    hook_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a service hook."""
    if auth and auth.organization_id:
        success, error = service_hook_web_service.delete(
            db, auth.organization_id, hook_id
        )
        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

    return RedirectResponse(
        url="/admin/settings/service-hooks?saved=1", status_code=303
    )


@router.post("/settings/service-hooks/{hook_id}/edit", response_class=HTMLResponse)
async def admin_settings_service_hooks_edit(
    request: Request,
    hook_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update a service hook."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = _normalize_form(form)

    if auth and auth.organization_id:
        success, error = service_hook_web_service.update_from_form(
            db, auth.organization_id, hook_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

    return RedirectResponse(
        url="/admin/settings/service-hooks?saved=1", status_code=303
    )


@router.post(
    "/settings/service-hooks/{hook_id}/executions/{execution_id}/retry",
    response_class=HTMLResponse,
)
async def admin_settings_service_hooks_retry_execution(
    request: Request,
    hook_id: str,
    execution_id: str,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Retry a failed/dead service hook execution."""
    if auth and auth.organization_id:
        success, error = service_hook_web_service.retry_execution(
            db, auth.organization_id, hook_id, execution_id
        )
        if not success:
            context = _admin_base_context(request, auth, "Service Hooks", db)
            context.update(
                service_hook_web_service.settings_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/service_hooks.html", context
            )

    return RedirectResponse(
        url="/admin/settings/service-hooks?saved=1", status_code=303
    )


@router.get("/settings/payments", response_class=HTMLResponse)
def admin_settings_payments(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Payments settings hub page."""
    context = _admin_base_context(request, auth, "Payment Providers", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_payments_hub_context(
                db, auth.organization_id
            )
        )
    return templates.TemplateResponse(
        request, "admin/settings/payments_index.html", context
    )


@router.get("/settings/payments/paystack", response_class=HTMLResponse)
def admin_settings_paystack(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Paystack settings page."""
    context = _admin_base_context(request, auth, "Paystack Settings", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_paystack_context(db, auth.organization_id)
        )
    base_url = str(request.base_url).rstrip("/")
    context["paystack_webhook_url"] = f"{base_url}/api/v1/payments/webhook/paystack"
    context["paystack_relay_path"] = "/api/v1/payment-events/paystack"
    return templates.TemplateResponse(request, "admin/settings/paystack.html", context)


@router.post("/settings/payments/paystack", response_class=HTMLResponse)
async def admin_settings_paystack_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update Paystack settings."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = dict(form)

    if auth and auth.organization_id:
        success, error = admin_settings_web_service.update_paystack(
            db, auth.organization_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Paystack Settings", db)
            context.update(
                admin_settings_web_service.get_paystack_context(
                    db, auth.organization_id
                )
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/paystack.html", context
            )

    return RedirectResponse(
        url="/admin/settings/payments/paystack?saved=1", status_code=303
    )


@router.get("/settings/banking/mono", response_class=HTMLResponse)
def admin_settings_mono(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Mono Connect settings page."""
    context = _admin_base_context(request, auth, "Mono Connect Settings", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_mono_context(db, auth.organization_id)
        )
    return templates.TemplateResponse(request, "admin/settings/mono.html", context)


@router.post("/settings/banking/mono", response_class=HTMLResponse)
async def admin_settings_mono_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update Mono Connect settings."""
    form = await request.form()
    data = dict(form)

    if auth and auth.organization_id:
        success, error = admin_settings_web_service.update_mono(
            db, auth.organization_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Mono Connect Settings", db)
            context.update(
                admin_settings_web_service.get_mono_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/mono.html", context
            )

    return RedirectResponse(url="/admin/settings/banking/mono?saved=1", status_code=303)


@router.get("/settings/coach", response_class=HTMLResponse)
def admin_settings_coach(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Coach / AI settings page."""
    context = _admin_base_context(request, auth, "Coach / AI Settings", db)
    if auth and auth.organization_id:
        context.update(
            admin_settings_web_service.get_coach_context(db, auth.organization_id)
        )
    return templates.TemplateResponse(request, "admin/settings/coach.html", context)


@router.post("/settings/coach", response_class=HTMLResponse)
async def admin_settings_coach_update(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Update Coach / AI settings."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    data = dict(form)

    if auth and auth.organization_id:
        success, error = admin_settings_web_service.update_coach(
            db, auth.organization_id, data
        )
        if not success:
            context = _admin_base_context(request, auth, "Coach / AI Settings", db)
            context.update(
                admin_settings_web_service.get_coach_context(db, auth.organization_id)
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "admin/settings/coach.html", context
            )

    return RedirectResponse(url="/admin/settings/coach?saved=1", status_code=303)


@router.get("/settings/advanced", response_class=HTMLResponse)
def admin_settings_advanced(
    request: Request,
    page: int = Query(default=1, ge=1),
    search: str = Query(default=""),
    status: str = Query(default=""),
    domain: str = Query(default=""),
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Advanced settings (raw DomainSettings CRUD)."""
    return admin_web_service.settings_response(
        request, db, auth, page, search, status, domain
    )


@router.get("/settings/{setting_id}", response_class=HTMLResponse)
def admin_settings_view(
    request: Request,
    setting_id: UUID,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """View setting details (same as edit)."""
    return admin_web_service.settings_view_response(request, db, auth, str(setting_id))


@router.get("/settings/{setting_id}/edit", response_class=HTMLResponse)
def admin_settings_edit(
    request: Request,
    setting_id: UUID,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Show edit setting form."""
    return admin_web_service.settings_edit_response(request, db, auth, str(setting_id))


@router.post("/settings/{setting_id}/edit", response_class=HTMLResponse)
async def admin_settings_update(
    request: Request,
    setting_id: UUID,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Handle edit setting form submission."""
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)

    domain = (form.get("domain") or "").strip()
    key = (form.get("key") or "").strip()
    value_type = (form.get("value_type") or "").strip()
    value = form.get("value") or ""
    is_secret = form.get("is_secret") or ""
    is_active = form.get("is_active") or ""
    if not (domain and key and value_type):
        content_type = (request.headers.get("content-type") or "").lower()
        if content_type.startswith("application/json"):
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                domain = domain or str(payload.get("domain") or "").strip()
                key = key or str(payload.get("key") or "").strip()
                value_type = value_type or str(payload.get("value_type") or "").strip()
                if not value:
                    value = payload.get("value") or ""
                if not is_secret:
                    is_secret = payload.get("is_secret") or ""
                if not is_active:
                    is_active = payload.get("is_active") or ""

    return admin_web_service.settings_update_response(
        request,
        db,
        auth,
        str(setting_id),
        domain,
        key,
        value_type,
        value,
        is_secret,
        is_active,
    )


@router.post("/settings/{setting_id}/delete")
def admin_settings_delete(
    request: Request,
    setting_id: UUID,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """Delete a setting."""
    return admin_web_service.settings_delete_response(
        request, db, auth, str(setting_id)
    )


# ---------------------------------------------------------------------------
# License Status
# ---------------------------------------------------------------------------


@router.get("/license", response_class=HTMLResponse)
def admin_license(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    """License status page."""
    return admin_web_service.license_response(request, db, auth)
