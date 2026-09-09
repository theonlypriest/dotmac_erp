"""ERP-owned staff access projections and outbox events."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.people.hr import (
    Employee,
    EmployeeStatus,
    StaffAccountStatusProjection,
    StaffAccountStatusState,
    StaffLeaveAccessRestriction,
    StaffLeaveRestrictionStatus,
)
from app.models.people.leave import LeaveApplication, LeaveApplicationStatus
from app.services.finance.platform.outbox_publisher import OutboxPublisher

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc

STAFF_LEAVE_RESTRICTION_CHANGED = "hr.staff_leave_restriction.changed"
STAFF_ACCOUNT_STATUS_CHANGED = "hr.staff_account_status.changed"
STAFF_ACCESS_EVENT_SCHEMA_VERSION = 1

READ_PERMISSION_ACTIONS = {
    "access",
    "dashboard",
    "export",
    "list",
    "meta",
    "preview",
    "read",
    "report",
    "reports",
    "search",
    "status",
    "view",
}
MUTATION_PERMISSION_ACTIONS = {
    "activate",
    "approve",
    "archive",
    "assign",
    "cancel",
    "check-in",
    "check-out",
    "create",
    "delete",
    "deactivate",
    "disburse",
    "execute",
    "feedback",
    "generate",
    "import",
    "manage",
    "match",
    "post",
    "process",
    "publish",
    "reconcile",
    "reject",
    "reimburse",
    "reopen",
    "resolve",
    "settle",
    "submit",
    "toggle",
    "update",
    "upload",
    "void",
    "workflow",
    "write",
}
ACCOUNT_ENABLED_STATUSES = {EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE}
ACCOUNT_INACTIVE_STATUSES = {
    EmployeeStatus.DRAFT,
    EmployeeStatus.SUSPENDED,
    EmployeeStatus.RESIGNED,
    EmployeeStatus.TERMINATED,
    EmployeeStatus.RETIRED,
}


def is_mutating_permission_key(permission_key: str) -> bool:
    """Return whether a permission represents state-changing authority."""
    parts = [part for part in (permission_key or "").strip().lower().split(":") if part]
    if not parts:
        return False
    if "*" in parts:
        return True
    action = parts[-1]
    if action in READ_PERMISSION_ACTIONS:
        return False
    return action in MUTATION_PERMISSION_ACTIONS


class StaffAccessProjectionService:
    """Maintains ERP-owned staff leave/account-state projections."""

    def __init__(self, db: Session):
        self.db = db

    def project_leave_application(
        self,
        application: LeaveApplication,
        *,
        reason: str | None = None,
    ) -> StaffLeaveAccessRestriction | None:
        """Upsert the restriction projection for one leave application."""
        self.db.flush()
        employee = self._employee(
            application.organization_id,
            application.employee_id,
        )
        if employee is None:
            raise ValueError("Leave restriction projection requires an employee")

        desired_status = (
            StaffLeaveRestrictionStatus.ACTIVE
            if application.status == LeaveApplicationStatus.APPROVED
            else StaffLeaveRestrictionStatus.CANCELLED
        )
        now = datetime.now(UTC)
        restriction = self.db.scalar(
            select(StaffLeaveAccessRestriction)
            .where(
                StaffLeaveAccessRestriction.organization_id
                == application.organization_id,
                StaffLeaveAccessRestriction.leave_application_id
                == application.application_id,
            )
            .with_for_update()
        )
        if restriction is None:
            if desired_status == StaffLeaveRestrictionStatus.CANCELLED:
                return None
            restriction = StaffLeaveAccessRestriction(
                organization_id=application.organization_id,
                employee_id=application.employee_id,
                person_id=employee.person_id,
                selfcare_user_id=employee.dotmac_sub_account_id,
                leave_application_id=application.application_id,
                effective_from=application.from_date,
                effective_until=application.to_date,
                status=desired_status,
                source_leave_status=application.status.value,
                cancelled_at=now
                if desired_status == StaffLeaveRestrictionStatus.CANCELLED
                else None,
                cancellation_reason=reason
                if desired_status == StaffLeaveRestrictionStatus.CANCELLED
                else None,
                updated_at=now,
            )
            self.db.add(restriction)
            self.db.flush()
            self._publish_leave_event(restriction)
            return restriction

        changed = (
            restriction.employee_id != application.employee_id
            or restriction.person_id != employee.person_id
            or restriction.selfcare_user_id != employee.dotmac_sub_account_id
            or restriction.effective_from != application.from_date
            or restriction.effective_until != application.to_date
            or restriction.status != desired_status
            or restriction.source_leave_status != application.status.value
        )
        if not changed:
            return restriction

        restriction.employee_id = application.employee_id
        restriction.person_id = employee.person_id
        restriction.selfcare_user_id = employee.dotmac_sub_account_id
        restriction.effective_from = application.from_date
        restriction.effective_until = application.to_date
        restriction.status = desired_status
        restriction.source_leave_status = application.status.value
        restriction.updated_at = now
        restriction.version += 1
        if desired_status == StaffLeaveRestrictionStatus.CANCELLED:
            restriction.cancelled_at = restriction.cancelled_at or now
            restriction.cancellation_reason = reason
        else:
            restriction.cancelled_at = None
            restriction.cancellation_reason = None
        self.db.flush()
        self._publish_leave_event(restriction)
        return restriction

    def refresh_employee_leave_restrictions(
        self,
        employee: Employee,
    ) -> list[StaffLeaveAccessRestriction]:
        """Refresh all approved leave projections for an employee."""
        projected_leave_ids = select(
            StaffLeaveAccessRestriction.leave_application_id
        ).where(
            StaffLeaveAccessRestriction.organization_id == employee.organization_id,
            StaffLeaveAccessRestriction.employee_id == employee.employee_id,
        )
        applications = list(
            self.db.scalars(
                select(LeaveApplication).where(
                    LeaveApplication.organization_id == employee.organization_id,
                    LeaveApplication.employee_id == employee.employee_id,
                    or_(
                        LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                        LeaveApplication.application_id.in_(projected_leave_ids),
                    ),
                )
            ).all()
        )
        restrictions: list[StaffLeaveAccessRestriction] = []
        for application in applications:
            restriction = self.project_leave_application(application)
            if restriction is not None:
                restrictions.append(restriction)
        return restrictions

    def project_employee_account_status(
        self,
        employee: Employee,
    ) -> StaffAccountStatusProjection:
        """Upsert the ERP-controlled Selfcare account-state projection."""
        now = datetime.now(UTC)
        state = self._account_state(employee)
        projection = self.db.scalar(
            select(StaffAccountStatusProjection)
            .where(
                StaffAccountStatusProjection.organization_id
                == employee.organization_id,
                StaffAccountStatusProjection.employee_id == employee.employee_id,
            )
            .with_for_update()
        )
        if projection is None:
            projection = StaffAccountStatusProjection(
                organization_id=employee.organization_id,
                employee_id=employee.employee_id,
                person_id=employee.person_id,
                selfcare_user_id=employee.dotmac_sub_account_id,
                erp_employee_status=employee.status.value,
                state=state,
                source_reason="employee_status",
                updated_at=now,
            )
            self.db.add(projection)
            self.db.flush()
            self._publish_account_event(projection)
            return projection

        changed = (
            projection.person_id != employee.person_id
            or projection.selfcare_user_id != employee.dotmac_sub_account_id
            or projection.erp_employee_status != employee.status.value
            or projection.state != state
        )
        if not changed:
            return projection

        projection.person_id = employee.person_id
        projection.selfcare_user_id = employee.dotmac_sub_account_id
        projection.erp_employee_status = employee.status.value
        projection.state = state
        projection.updated_at = now
        projection.version += 1
        self.db.flush()
        self._publish_account_event(projection)
        return projection

    def refresh_employee_projections(
        self,
        employee: Employee,
    ) -> None:
        """Refresh every ERP-owned staff access projection for an employee."""
        self.project_employee_account_status(employee)
        self.refresh_employee_leave_restrictions(employee)

    def list_leave_restrictions(
        self,
        organization_id: UUID,
        *,
        updated_after: datetime | None = None,
        limit: int = 200,
    ) -> list[StaffLeaveAccessRestriction]:
        statement = select(StaffLeaveAccessRestriction).where(
            StaffLeaveAccessRestriction.organization_id == organization_id
        )
        if updated_after is not None:
            statement = statement.where(
                StaffLeaveAccessRestriction.updated_at > updated_after
            )
        return list(
            self.db.scalars(
                statement.order_by(
                    StaffLeaveAccessRestriction.updated_at,
                    StaffLeaveAccessRestriction.restriction_id,
                ).limit(limit)
            ).all()
        )

    def list_account_statuses(
        self,
        organization_id: UUID,
        *,
        updated_after: datetime | None = None,
        limit: int = 200,
    ) -> list[StaffAccountStatusProjection]:
        statement = select(StaffAccountStatusProjection).where(
            StaffAccountStatusProjection.organization_id == organization_id
        )
        if updated_after is not None:
            statement = statement.where(
                StaffAccountStatusProjection.updated_at > updated_after
            )
        return list(
            self.db.scalars(
                statement.order_by(
                    StaffAccountStatusProjection.updated_at,
                    StaffAccountStatusProjection.projection_id,
                ).limit(limit)
            ).all()
        )

    def active_restriction_for_person(
        self,
        organization_id: UUID,
        person_id: UUID,
        *,
        as_of_date: date | None = None,
    ) -> StaffLeaveAccessRestriction | None:
        check_date = as_of_date or self._org_today(organization_id)
        return self.db.scalar(
            select(StaffLeaveAccessRestriction)
            .where(
                StaffLeaveAccessRestriction.organization_id == organization_id,
                StaffLeaveAccessRestriction.person_id == person_id,
                StaffLeaveAccessRestriction.status
                == StaffLeaveRestrictionStatus.ACTIVE,
                StaffLeaveAccessRestriction.effective_from <= check_date,
                StaffLeaveAccessRestriction.effective_until >= check_date,
            )
            .order_by(StaffLeaveAccessRestriction.effective_until.desc())
            .limit(1)
        )

    def _employee(self, organization_id: UUID, employee_id: UUID) -> Employee | None:
        return self.db.scalar(
            select(Employee).where(
                Employee.organization_id == organization_id,
                Employee.employee_id == employee_id,
            )
        )

    def _org_today(self, organization_id: UUID) -> date:
        from app.services.people.leave.leave_service import LeaveService

        return LeaveService(self.db).get_org_today(organization_id)

    @staticmethod
    def _account_state(employee: Employee) -> StaffAccountStatusState:
        if employee.status in ACCOUNT_ENABLED_STATUSES:
            return StaffAccountStatusState.ACTIVE
        if employee.status in ACCOUNT_INACTIVE_STATUSES:
            return StaffAccountStatusState.INACTIVE
        return StaffAccountStatusState.INACTIVE

    @staticmethod
    def _headers(
        organization_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> dict[str, str | None]:
        return {
            "organization_id": str(organization_id),
            "user_id": str(user_id) if user_id else None,
            "request_id": None,
            "ip_address": None,
            "source": "erp.people",
        }

    def _publish_leave_event(self, restriction: StaffLeaveAccessRestriction) -> None:
        payload = {
            "contract_version": "staff.leave_restriction.v1",
            "event_type": STAFF_LEAVE_RESTRICTION_CHANGED,
            "restriction_id": str(restriction.restriction_id),
            "organization_id": str(restriction.organization_id),
            "employee_id": str(restriction.employee_id),
            "person_id": str(restriction.person_id),
            "selfcare_user_id": restriction.selfcare_user_id,
            "source": {
                "type": "leave_application",
                "id": str(restriction.leave_application_id),
                "status": restriction.source_leave_status,
            },
            "effective_from": restriction.effective_from.isoformat(),
            "effective_until": restriction.effective_until.isoformat(),
            "status": restriction.status.value,
            "version": restriction.version,
            "updated_at": restriction.updated_at.isoformat(),
            "cancelled_at": restriction.cancelled_at.isoformat()
            if restriction.cancelled_at
            else None,
            "cancellation_reason": restriction.cancellation_reason,
        }
        OutboxPublisher.publish_event(
            self.db,
            event_name=STAFF_LEAVE_RESTRICTION_CHANGED,
            event_version=STAFF_ACCESS_EVENT_SCHEMA_VERSION,
            aggregate_type="StaffLeaveAccessRestriction",
            aggregate_id=str(restriction.restriction_id),
            payload=payload,
            headers=self._headers(restriction.organization_id),
            producer_module="people",
            correlation_id=str(restriction.leave_application_id),
            idempotency_key=(
                f"staff-leave-restriction:{restriction.restriction_id}:"
                f"v{restriction.version}"
            ),
        )

    def _publish_account_event(self, projection: StaffAccountStatusProjection) -> None:
        payload = {
            "contract_version": "staff.account_status.v1",
            "event_type": STAFF_ACCOUNT_STATUS_CHANGED,
            "projection_id": str(projection.projection_id),
            "organization_id": str(projection.organization_id),
            "employee_id": str(projection.employee_id),
            "person_id": str(projection.person_id),
            "selfcare_user_id": projection.selfcare_user_id,
            "erp_employee_status": projection.erp_employee_status,
            "state": projection.state.value,
            "source_reason": projection.source_reason,
            "ownership": "erp_employee_status",
            "downstream_semantics": (
                "Selfcare must add or remove only the ERP-controlled employee-status "
                "hold; independent local suspensions remain authoritative."
            ),
            "version": projection.version,
            "updated_at": projection.updated_at.isoformat(),
        }
        OutboxPublisher.publish_event(
            self.db,
            event_name=STAFF_ACCOUNT_STATUS_CHANGED,
            event_version=STAFF_ACCESS_EVENT_SCHEMA_VERSION,
            aggregate_type="StaffAccountStatusProjection",
            aggregate_id=str(projection.projection_id),
            payload=payload,
            headers=self._headers(projection.organization_id),
            producer_module="people",
            correlation_id=str(projection.employee_id),
            idempotency_key=(
                f"staff-account-status:{projection.projection_id}:v{projection.version}"
            ),
        )


def has_active_staff_leave_restriction(
    db: Session,
    organization_id: UUID,
    person_id: UUID,
    *,
    as_of_date: date | None = None,
) -> bool:
    return (
        StaffAccessProjectionService(db).active_restriction_for_person(
            organization_id,
            person_id,
            as_of_date=as_of_date,
        )
        is not None
    )


def refresh_staff_access_projections_for_employees(
    db: Session,
    employees: Iterable[Employee],
) -> None:
    service = StaffAccessProjectionService(db)
    for employee in employees:
        service.refresh_employee_projections(employee)


__all__ = [
    "ACCOUNT_ENABLED_STATUSES",
    "ACCOUNT_INACTIVE_STATUSES",
    "STAFF_ACCOUNT_STATUS_CHANGED",
    "STAFF_ACCESS_EVENT_SCHEMA_VERSION",
    "STAFF_LEAVE_RESTRICTION_CHANGED",
    "StaffAccessProjectionService",
    "has_active_staff_leave_restriction",
    "is_mutating_permission_key",
    "refresh_staff_access_projections_for_employees",
]
