"""
Staff sync: mirror employee lifecycle into dotmac_sub staff accounts.

ERP is the HR system of record. On hire (ACTIVE) the employee gets a
dotmac_sub SystemUser (created + invited, or re-enabled); on exit
(TERMINATED / RESIGNED / RETIRED / SUSPENDED) the account is disabled —
dotmac_sub revokes live sessions on deactivation.

``sync_employee`` is a one-employee idempotent push, safe to call from
lifecycle events and from the nightly reconcile sweep alike.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session_context import prime_tenant_context
from app.models.people.hr.department import Department
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.services.dotmac_sub.client import DotmacSubClient, DotmacSubConfig

logger = logging.getLogger(__name__)

# Statuses whose dotmac_sub account must be enabled / disabled.
_ENABLED_STATUSES = {EmployeeStatus.ACTIVE}
_DISABLED_STATUSES = {
    EmployeeStatus.SUSPENDED,
    EmployeeStatus.RESIGNED,
    EmployeeStatus.TERMINATED,
    EmployeeStatus.RETIRED,
}


def _staff_email(employee: Employee) -> str | None:
    return employee.work_email or employee.personal_email


def _staff_roles(employee: Employee) -> list[str]:
    configured = getattr(employee, "dotmac_sub_roles", None) or []
    normalized = list(
        dict.fromkeys(str(role).strip() for role in configured if str(role).strip())
    )
    return normalized or [settings.dotmac_sub_staff_default_role]


def _department_payload(
    db: Session | None, employee: Employee
) -> dict[str, str | None] | None:
    department_id = getattr(employee, "department_id", None)
    if department_id is None:
        return None

    department = getattr(employee, "department", None)
    if department is None and db is not None:
        department = db.get(Department, department_id)
    if department is None:
        return None
    if (
        getattr(department, "organization_id", employee.organization_id)
        != employee.organization_id
    ):
        return None

    return {
        "department_id": str(department.department_id),
        "department_code": department.department_code,
        "department_name": department.department_name,
    }


def _sync_erp_department_membership(
    db: Session | None,
    employee: Employee,
    account_id: str,
    client: DotmacSubClient,
    *,
    remove: bool = False,
) -> None:
    department = None
    if not remove:
        department = _department_payload(db, employee)
        if department is None and getattr(employee, "department_id", None) is not None:
            raise ValueError("employee department could not be resolved for staff sync")
    client.sync_staff_account_erp_department(
        account_id,
        erp_employee_id=str(employee.employee_id),
        employee_code=getattr(employee, "employee_code", None),
        erp_organization_id=str(employee.organization_id),
        department=department,
    )


def sync_employee(
    db: Session | None,
    employee: Employee,
    *,
    client: DotmacSubClient | None = None,
) -> dict[str, Any]:
    """Push one employee's lifecycle state to dotmac_sub. Idempotent.

    Returns a result dict with action created, reactivation_projected, disabled,
    skipped, or noop.
    Never raises on business-state gaps (missing email, draft status) — those
    are 'skipped' with a reason; transport/auth errors do raise so callers
    (Celery retry / reconcile error counters) see them.
    """
    if not settings.dotmac_sub_staff_sync_enabled:
        return {"action": "skipped", "reason": "staff sync disabled"}

    status = employee.status
    access_enabled = bool(getattr(employee, "dotmac_sub_access_enabled", False))
    if access_enabled and status not in _ENABLED_STATUSES | _DISABLED_STATUSES:
        return {"action": "skipped", "reason": f"status {status.value} not synced"}

    email = _staff_email(employee)
    account_id = employee.dotmac_sub_account_id
    if not email and not account_id:
        return {"action": "skipped", "reason": "employee has no email"}

    owns_client = client is None
    if client is None:
        config = DotmacSubConfig.for_org(db, employee.organization_id)
        if not config.is_configured():
            return {"action": "skipped", "reason": "dotmac_sub not configured"}
        client = DotmacSubClient(config)

    try:
        account: dict[str, Any] | None = None
        if not account_id and email:
            account = client.get_staff_account(email)
            if account:
                account_id = str(account.get("id"))

        if status in _ENABLED_STATUSES and access_enabled:
            roles = _staff_roles(employee)
            if not account_id:
                if not email:
                    return {"action": "skipped", "reason": "employee has no email"}
                created = client.create_staff_account(
                    email=email,
                    first_name=employee.first_name or "",
                    last_name=employee.last_name or "",
                    role=settings.dotmac_sub_staff_default_role,
                    roles=roles,
                    send_invite=True,
                )
                employee.dotmac_sub_account_id = str(created.get("id"))
                _sync_erp_department_membership(
                    db, employee, employee.dotmac_sub_account_id, client
                )
                _mark_synced(employee)
                _refresh_staff_access_projection(db, employee)
                return {
                    "action": "created",
                    "account_id": employee.dotmac_sub_account_id,
                }
            if account is None and email:
                account = client.get_staff_account(email)
            employee.dotmac_sub_account_id = account_id
            client.set_staff_account_roles(account_id, roles=roles)
            if account and not account.get("is_active", True):
                _sync_erp_department_membership(db, employee, account_id, client)
                _mark_synced(employee)
                _refresh_staff_access_projection(db, employee)
                return {"action": "reactivation_projected", "account_id": account_id}
            _sync_erp_department_membership(db, employee, account_id, client)
            _mark_synced(employee)
            _refresh_staff_access_projection(db, employee)
            return {"action": "noop", "account_id": account_id}

        # Disabled lifecycle statuses or an explicit HR access revocation.
        if not account_id:
            reason = (
                "dotmac_sub access not granted"
                if status in _ENABLED_STATUSES
                else "no dotmac_sub account"
            )
            return {"action": "skipped", "reason": reason}
        employee.dotmac_sub_account_id = account_id
        if account is None and email:
            account = client.get_staff_account(email)
        if account and not account.get("is_active", True):
            _sync_erp_department_membership(
                db, employee, account_id, client, remove=True
            )
            _mark_synced(employee)
            _refresh_staff_access_projection(db, employee)
            return {"action": "noop", "account_id": account_id}
        _sync_erp_department_membership(db, employee, account_id, client, remove=True)
        client.set_staff_account_active(account_id, is_active=False)
        _mark_synced(employee)
        _refresh_staff_access_projection(db, employee)
        return {"action": "disabled", "account_id": account_id}
    finally:
        if owns_client:
            client.close()


def _mark_synced(employee: Employee) -> None:
    employee.dotmac_sub_staff_synced_at = datetime.now(timezone.utc)


def _refresh_staff_access_projection(db: Session | None, employee: Employee) -> None:
    if db is None:
        return

    from app.services.people.hr.staff_access_projection import (
        StaffAccessProjectionService,
    )

    StaffAccessProjectionService(db).refresh_employee_projections(employee)


def reconcile_staff_accounts(db: Session, organization_id: UUID) -> dict[str, Any]:
    """Nightly sweep: push every syncable employee's state. Error-isolated."""
    if not settings.dotmac_sub_staff_sync_enabled:
        return {"success": True, "skipped": "staff sync disabled"}

    config = DotmacSubConfig.for_org(db, organization_id)
    if not config.is_configured():
        return {"success": True, "skipped": "dotmac_sub not configured"}

    syncable = list(_ENABLED_STATUSES | _DISABLED_STATUSES)
    # Snapshot (id, code) up front. We commit per employee for error
    # isolation, but the tenant RLS context is a transaction-scoped
    # ``SET LOCAL`` that every commit resets — so carrying ORM instances
    # across commits makes their next attribute access reload under no org
    # context, RLS filters the row out, and SQLAlchemy raises
    # ObjectDeletedError. Re-prime and re-fetch each iteration instead; the
    # code is kept as a plain string so the error branch never touches an
    # expired instance either.
    rows = db.execute(
        select(Employee.employee_id, Employee.employee_code).where(
            Employee.organization_id == organization_id,
            or_(
                Employee.status.in_(syncable),
                Employee.dotmac_sub_account_id.is_not(None),
                Employee.dotmac_sub_access_enabled.is_(True),
            ),
        )
    ).all()

    counts: dict[str, int] = {}
    errors: list[str] = []
    with DotmacSubClient(config) as client:
        for emp_id, emp_code in rows:
            try:
                prime_tenant_context(db, organization_id)
                employee = db.get(Employee, emp_id)
                if employee is None:
                    continue
                result = sync_employee(db, employee, client=client)
                counts[result["action"]] = counts.get(result["action"], 0) + 1
                db.commit()
            except Exception as e:  # noqa: BLE001 — isolate per-employee failures
                db.rollback()
                errors.append(f"{emp_code}: {e}")
                logger.exception("Staff sync failed for employee %s", emp_id)

    return {"success": not errors, "counts": counts, "errors": errors[:20]}
