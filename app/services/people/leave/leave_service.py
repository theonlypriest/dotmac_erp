"""Leave management service implementation.

Handles leave types, allocations, applications, and holiday lists.
Adapted from DotMac People for the unified ERP platform.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.models.finance.audit.audit_log import AuditAction
from app.models.finance.core_org import Organization
from app.models.people.hr import Employee, EmployeeStatus
from app.models.people.leave import (
    Holiday,
    HolidayList,
    LeaveAllocation,
    LeaveApplication,
    LeaveApplicationStatus,
    LeaveType,
    LeaveTypePolicy,
)
from app.models.notification import EntityType, NotificationChannel, NotificationType
from app.models.person import Person, PersonStatus
from app.models.rbac import PersonRole, Role
from app.services.audit_dispatcher import fire_audit_event
from app.services.common import (
    ConflictError,
    PaginatedResult,
    PaginationParams,
    ValidationError,
)
from app.services.notification import NotificationService
from app.services.state_machine import StateMachine

logger = logging.getLogger(__name__)

# Role names that should be notified when a leave application is submitted.
LEAVE_NOTIFICATION_ROLES = ("hr_manager",)


def _employee_search_predicate(pattern: str) -> ColumnElement[bool]:
    """Build the shared employee identity search predicate."""
    return or_(
        Employee.employee_code.ilike(pattern),
        Person.first_name.ilike(pattern),
        Person.last_name.ilike(pattern),
        Person.display_name.ilike(pattern),
        Person.email.ilike(pattern),
        func.concat(Person.first_name, " ", Person.last_name).ilike(pattern),
    )


if TYPE_CHECKING:
    from app.web.deps import WebAuthContext

__all__ = ["LeaveService"]


class LeaveServiceError(Exception):
    """Base error for leave service."""

    pass


class LeaveTypeNotFoundError(LeaveServiceError):
    """Leave type not found."""

    def __init__(self, leave_type_id: UUID):
        self.leave_type_id = leave_type_id
        super().__init__(f"Leave type {leave_type_id} not found")


class LeaveAllocationNotFoundError(LeaveServiceError):
    """Leave allocation not found."""

    def __init__(self, allocation_id: UUID):
        self.allocation_id = allocation_id
        super().__init__(f"Leave allocation {allocation_id} not found")


class LeaveBalanceEntry(TypedDict):
    leave_type_name: str
    allocated: Decimal
    used: Decimal
    balance: Decimal


class EmployeeLeaveSummary(TypedDict):
    employee_id: str
    employee_name: str
    department_name: str
    leave_balances: list[LeaveBalanceEntry]
    total_allocated: Decimal
    total_used: Decimal
    total_balance: Decimal


class LeaveAllocationExistsError(LeaveServiceError):
    """Leave allocation already exists for employee/type/period."""

    def __init__(self, employee_id: UUID, leave_type_id: UUID, from_date: date):
        self.employee_id = employee_id
        self.leave_type_id = leave_type_id
        self.from_date = from_date
        super().__init__(
            "Leave allocation already exists for this employee, leave type, and start date."
        )


class LeaveApplicationNotFoundError(LeaveServiceError):
    """Leave application not found."""

    def __init__(self, application_id: UUID):
        self.application_id = application_id
        super().__init__(f"Leave application {application_id} not found")


class HolidayListNotFoundError(LeaveServiceError):
    """Holiday list not found."""

    def __init__(self, holiday_list_id: UUID):
        self.holiday_list_id = holiday_list_id
        super().__init__(f"Holiday list {holiday_list_id} not found")


class InsufficientLeaveBalanceError(LeaveServiceError):
    """Insufficient leave balance."""

    def __init__(self, available: Decimal, requested: Decimal):
        self.available = available
        self.requested = requested
        super().__init__(
            f"Insufficient leave balance. Available: {available}, Requested: {requested}"
        )


class LeaveApplicationStatusError(LeaveServiceError):
    """Invalid leave application status transition."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition from {current} to {target}")


class LeaveEligibilityError(LeaveServiceError):
    """Raised when an employee is not eligible for a leave request."""


class OverlappingLeaveApplicationError(ConflictError, LeaveServiceError):
    """Raised when a leave request overlaps an existing submitted/approved request."""

    def __init__(self, from_date: date, to_date: date):
        self.from_date = from_date
        self.to_date = to_date
        ConflictError.__init__(
            self,
            f"Overlapping leave application exists for period {from_date} to {to_date}",
        )


# Valid status transitions for leave applications
STATUS_TRANSITIONS = {
    LeaveApplicationStatus.DRAFT: {
        LeaveApplicationStatus.SUBMITTED,
        LeaveApplicationStatus.CANCELLED,
    },
    LeaveApplicationStatus.SUBMITTED: {
        LeaveApplicationStatus.APPROVED,
        LeaveApplicationStatus.REJECTED,
        LeaveApplicationStatus.CANCELLED,
    },
    LeaveApplicationStatus.APPROVED: {
        LeaveApplicationStatus.CANCELLED,
    },
    LeaveApplicationStatus.REJECTED: set(),  # Terminal state
    LeaveApplicationStatus.CANCELLED: set(),  # Terminal state
}
_STATE_MACHINE = StateMachine(STATUS_TRANSITIONS)


class LeaveService:
    """Service for leave management operations.

    Handles:
    - Leave types (annual, sick, maternity, etc.)
    - Holiday lists and holidays
    - Leave allocations per employee
    - Leave applications with approval workflow
    - Leave balance calculations
    """

    def _next_application_number(self, org_id: UUID) -> str:
        """Generate the next leave application number.

        Delegates to SyncNumberingService for race-condition-safe generation.
        """
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        return SyncNumberingService(self.db).generate_next_number(
            org_id, SequenceType.LEAVE_APPLICATION
        )

    def __init__(
        self,
        db: Session,
        ctx: WebAuthContext | None = None,
    ) -> None:
        self.db = db
        self.ctx = ctx

    def resolve_employee_filter(
        self,
        org_id: UUID,
        value: str | None,
    ) -> tuple[UUID | None, list[UUID] | None]:
        """Resolve an employee UUID, code, name, or email within one tenant."""
        raw = (value or "").strip()
        if not raw:
            return None, None

        try:
            parsed_uuid = UUID(raw)
        except ValueError:
            parsed_uuid = None

        if parsed_uuid:
            employee_id = self.db.scalar(
                select(Employee.employee_id).where(
                    Employee.organization_id == org_id,
                    Employee.employee_id == parsed_uuid,
                )
            )
            return (employee_id, None) if employee_id else (None, [])

        employee_id = self.db.scalar(
            select(Employee.employee_id).where(
                Employee.organization_id == org_id,
                Employee.employee_code.ilike(raw),
            )
        )
        if employee_id:
            return employee_id, None

        pattern = f"%{raw}%"
        employee_ids = list(
            self.db.scalars(
                select(Employee.employee_id)
                .join(Person, Person.id == Employee.person_id)
                .where(
                    Employee.organization_id == org_id,
                    Person.organization_id == org_id,
                    _employee_search_predicate(pattern),
                )
                .limit(500)
            ).all()
        )
        return None, employee_ids

    def get_org_today(self, org_id: UUID) -> date:
        """Return the current date in the organization's timezone."""
        org = self.db.get(Organization, org_id)
        tz_name = org.timezone if org and org.timezone else "UTC"
        try:
            return datetime.now(tz=ZoneInfo(tz_name)).date()
        except Exception:
            return datetime.now(tz=UTC).date()

    @staticmethod
    def _leave_type_tokens(leave_type: LeaveType) -> set[str]:
        """Return stable tokens for policy matching across code/name variants."""
        code = getattr(leave_type, "leave_type_code", "") or ""
        name = getattr(leave_type, "leave_type_name", "") or ""
        text = f"{code} {name}".upper()
        for separator in ("-", "_", "/"):
            text = text.replace(separator, " ")
        return set(text.split())

    @classmethod
    def _is_annual_leave_type(cls, leave_type: LeaveType) -> bool:
        return "ANNUAL" in cls._leave_type_tokens(leave_type)

    @classmethod
    def _is_sick_leave_type(cls, leave_type: LeaveType) -> bool:
        return "SICK" in cls._leave_type_tokens(leave_type)

    @staticmethod
    def _one_year_service_date(date_of_joining: date) -> date:
        """Return the date the employee completes one calendar year of service."""
        try:
            return date_of_joining.replace(year=date_of_joining.year + 1)
        except ValueError:
            return date(date_of_joining.year + 1, 3, 1)

    def _get_application_employee(self, org_id: UUID, employee_id: UUID) -> Employee:
        employee = self.db.scalar(
            select(Employee).where(
                Employee.organization_id == org_id,
                Employee.employee_id == employee_id,
            )
        )
        if not employee:
            raise LeaveServiceError("Employee not found")
        return employee

    def _validate_service_eligibility(
        self,
        org_id: UUID,
        *,
        employee_id: UUID,
        leave_type: LeaveType,
        total_days: Decimal,
    ) -> None:
        """Enforce HR Manual leave eligibility for staff under one year."""
        employee = self._get_application_employee(org_id, employee_id)
        service_date = self._one_year_service_date(employee.date_of_joining)
        if self.get_org_today(org_id) >= service_date:
            return

        if self._is_annual_leave_type(leave_type):
            raise LeaveEligibilityError(
                "Annual leave is only available after completing one year of service."
            )

        if self._is_sick_leave_type(leave_type) and total_days > Decimal("2"):
            raise LeaveEligibilityError(
                "Staff with less than one year of service may request up to 2 days of sick leave."
            )

    @staticmethod
    def _status_managed_by_leave(status: EmployeeStatus) -> bool:
        """Only ACTIVE/ON_LEAVE are auto-managed by leave workflow."""
        return status in {EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE}

    def sync_employee_leave_status(
        self,
        org_id: UUID,
        employee_id: UUID,
        *,
        as_of_date: date | None = None,
    ) -> str | None:
        """Sync one employee's status from approved leave coverage.

        Returns the new status string when changed, otherwise None.
        """
        check_date = as_of_date or self.get_org_today(org_id)
        employee = self.db.scalar(
            select(Employee).where(
                Employee.organization_id == org_id,
                Employee.employee_id == employee_id,
            )
        )
        if not employee or not self._status_managed_by_leave(employee.status):
            return None

        has_active_leave = (
            self.db.scalar(
                select(LeaveApplication.application_id).where(
                    LeaveApplication.organization_id == org_id,
                    LeaveApplication.employee_id == employee_id,
                    LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                    LeaveApplication.from_date <= check_date,
                    LeaveApplication.to_date >= check_date,
                )
            )
            is not None
        )

        if has_active_leave and employee.status == EmployeeStatus.ACTIVE:
            employee.status = EmployeeStatus.ON_LEAVE
            employee.updated_at = datetime.now(UTC)
            employee.updated_by_id = self.ctx.person_id if self.ctx else None
            self.db.flush()
            from app.services.people.hr.staff_access_projection import (
                StaffAccessProjectionService,
            )

            StaffAccessProjectionService(self.db).project_employee_account_status(
                employee
            )
            return EmployeeStatus.ON_LEAVE.value

        if not has_active_leave and employee.status == EmployeeStatus.ON_LEAVE:
            employee.status = EmployeeStatus.ACTIVE
            employee.updated_at = datetime.now(UTC)
            employee.updated_by_id = self.ctx.person_id if self.ctx else None
            self.db.flush()
            from app.services.people.hr.staff_access_projection import (
                StaffAccessProjectionService,
            )

            StaffAccessProjectionService(self.db).project_employee_account_status(
                employee
            )
            return EmployeeStatus.ACTIVE.value

        return None

    def sync_employee_statuses_for_date(
        self,
        org_id: UUID,
        *,
        as_of_date: date | None = None,
    ) -> dict[str, int]:
        """Reconcile ACTIVE/ON_LEAVE statuses from approved leave for a date."""
        check_date = as_of_date or self.get_org_today(org_id)
        approved_on_date = set(
            self.db.scalars(
                select(LeaveApplication.employee_id).where(
                    LeaveApplication.organization_id == org_id,
                    LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                    LeaveApplication.from_date <= check_date,
                    LeaveApplication.to_date >= check_date,
                )
            ).all()
        )
        candidates = list(
            self.db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status.in_(
                        [EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE]
                    ),
                )
            ).all()
        )

        set_on_leave = 0
        set_active = 0
        changed_employees: list[Employee] = []
        for employee in candidates:
            should_be_on_leave = employee.employee_id in approved_on_date
            if should_be_on_leave and employee.status == EmployeeStatus.ACTIVE:
                employee.status = EmployeeStatus.ON_LEAVE
                employee.updated_at = datetime.now(UTC)
                employee.updated_by_id = self.ctx.person_id if self.ctx else None
                set_on_leave += 1
                changed_employees.append(employee)
            elif not should_be_on_leave and employee.status == EmployeeStatus.ON_LEAVE:
                employee.status = EmployeeStatus.ACTIVE
                employee.updated_at = datetime.now(UTC)
                employee.updated_by_id = self.ctx.person_id if self.ctx else None
                set_active += 1
                changed_employees.append(employee)

        if set_on_leave or set_active:
            self.db.flush()
            from app.services.people.hr.staff_access_projection import (
                refresh_staff_access_projections_for_employees,
            )

            refresh_staff_access_projections_for_employees(self.db, changed_employees)

        return {
            "set_on_leave": set_on_leave,
            "set_active": set_active,
            "checked": len(candidates),
        }

    def _validate_transition(
        self,
        current_status: LeaveApplicationStatus,
        new_status: LeaveApplicationStatus,
    ) -> None:
        try:
            _STATE_MACHINE.validate(current_status, new_status)
        except ValidationError:
            raise LeaveApplicationStatusError(
                current_status.value, new_status.value
            ) from None

    def _get_hr_manager_recipients(self, org_id: UUID) -> list[Person]:
        """Return active people in the organization holding any leave-notification role."""
        recipient_ids = list(
            self.db.scalars(
                select(Person.id)
                .join(PersonRole, PersonRole.person_id == Person.id)
                .join(Role, Role.id == PersonRole.role_id)
                .where(
                    Person.organization_id == org_id,
                    Person.is_active.is_(True),
                    Person.status == PersonStatus.active,
                    Role.name.in_(LEAVE_NOTIFICATION_ROLES),
                    Role.is_active.is_(True),
                )
                .distinct()
            ).all()
        )
        if not recipient_ids:
            return []

        stmt = (
            select(Person)
            .where(Person.id.in_(recipient_ids))
            .order_by(Person.first_name, Person.last_name, Person.id)
        )
        return list(self.db.scalars(stmt).all())

    def _notify_leave_submitted(
        self,
        org_id: UUID,
        application: LeaveApplication,
    ) -> None:
        """Create HR manager notifications for a submitted leave application."""
        recipients = self._get_hr_manager_recipients(org_id)
        if not recipients:
            return

        employee = self.db.get(Employee, application.employee_id)
        employee_name = (
            employee.full_name
            if employee and getattr(employee, "full_name", None)
            else str(application.employee_id)
        )
        title = "Leave application submitted"
        message = (
            f"{employee_name} submitted leave request "
            f"{application.application_number} for "
            f"{application.from_date} to {application.to_date}."
        )
        action_url = f"/people/leave/applications/{application.application_id}"
        actor_id = self.ctx.person_id if self.ctx else None
        notification_service = NotificationService()

        for recipient in recipients:
            notification_service.create(
                self.db,
                organization_id=org_id,
                recipient_id=recipient.id,
                entity_type=EntityType.LEAVE,
                entity_id=application.application_id,
                notification_type=NotificationType.SUBMITTED,
                title=title,
                message=message,
                channel=NotificationChannel.BOTH,
                action_url=action_url,
                actor_id=actor_id,
            )

    def _project_leave_access_restriction(
        self,
        application: LeaveApplication,
        *,
        reason: str | None = None,
    ) -> None:
        """Refresh the ERP-owned leave access projection for this application."""
        from app.services.people.hr.staff_access_projection import (
            StaffAccessProjectionService,
        )

        StaffAccessProjectionService(self.db).project_leave_application(
            application,
            reason=reason,
        )

    # =========================================================================
    # Leave Types
    # =========================================================================

    def list_leave_types(
        self,
        org_id: UUID,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[LeaveType]:
        """List leave types for an organization."""
        query = select(LeaveType).where(LeaveType.organization_id == org_id)

        if is_active is not None:
            query = query.where(LeaveType.is_active == is_active)

        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    LeaveType.leave_type_code.ilike(search_term),
                    LeaveType.leave_type_name.ilike(search_term),
                )
            )

        query = query.order_by(LeaveType.leave_type_name)

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        # Apply pagination
        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).all())

        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_leave_type(self, org_id: UUID, leave_type_id: UUID) -> LeaveType:
        """Get a leave type by ID."""
        leave_type = self.db.scalar(
            select(LeaveType).where(
                LeaveType.leave_type_id == leave_type_id,
                LeaveType.organization_id == org_id,
            )
        )
        if not leave_type:
            raise LeaveTypeNotFoundError(leave_type_id)
        return leave_type

    def create_leave_type(
        self,
        org_id: UUID,
        *,
        leave_type_code: str,
        leave_type_name: str,
        allocation_policy: LeaveTypePolicy = LeaveTypePolicy.ANNUAL,
        max_days_per_year: Decimal | None = None,
        max_continuous_days: int | None = None,
        allow_carry_forward: bool = False,
        max_carry_forward_days: Decimal | None = None,
        carry_forward_expiry_months: int | None = None,
        allow_encashment: bool = False,
        encashment_threshold_days: Decimal | None = None,
        is_lwp: bool = False,
        is_optional: bool = False,
        is_compensatory: bool = False,
        include_holidays: bool = False,
        applicable_after_days: int = 0,
        max_optional_leaves: int | None = None,
        is_active: bool = True,
        description: str | None = None,
    ) -> LeaveType:
        """Create a new leave type."""
        leave_type = LeaveType(
            organization_id=org_id,
            leave_type_code=leave_type_code,
            leave_type_name=leave_type_name,
            description=description,
            allocation_policy=allocation_policy,
            max_days_per_year=max_days_per_year,
            max_continuous_days=max_continuous_days,
            allow_carry_forward=allow_carry_forward,
            max_carry_forward_days=max_carry_forward_days,
            carry_forward_expiry_months=carry_forward_expiry_months,
            allow_encashment=allow_encashment,
            encashment_threshold_days=encashment_threshold_days,
            is_lwp=is_lwp,
            is_compensatory=is_compensatory,
            include_holidays=include_holidays,
            applicable_after_days=applicable_after_days,
            is_optional=is_optional,
            max_optional_leaves=max_optional_leaves,
            is_active=is_active,
        )

        self.db.add(leave_type)
        self.db.flush()
        return leave_type

    def update_leave_type(
        self,
        org_id: UUID,
        leave_type_id: UUID,
        **kwargs,
    ) -> LeaveType:
        """Update a leave type."""
        leave_type = self.get_leave_type(org_id, leave_type_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(leave_type, key):
                setattr(leave_type, key, value)

        self.db.flush()
        return leave_type

    def delete_leave_type(self, org_id: UUID, leave_type_id: UUID) -> None:
        """Delete a leave type (soft delete by deactivating)."""
        leave_type = self.get_leave_type(org_id, leave_type_id)
        leave_type.is_active = False
        self.db.flush()

    # =========================================================================
    # Holiday Lists
    # =========================================================================

    def list_holiday_lists(
        self,
        org_id: UUID,
        *,
        year: int | None = None,
        is_active: bool | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[HolidayList]:
        """List holiday lists for an organization."""
        query = select(HolidayList).where(HolidayList.organization_id == org_id)

        if year is not None:
            year_start = date(year, 1, 1)
            year_end = date(year, 12, 31)
            query = query.where(
                and_(
                    HolidayList.from_date <= year_end,
                    HolidayList.to_date >= year_start,
                )
            )

        if is_active is not None:
            query = query.where(HolidayList.is_active == is_active)

        query = query.options(joinedload(HolidayList.holidays))
        query = query.order_by(HolidayList.from_date.desc())

        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).unique().all())

        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_holiday_list(self, org_id: UUID, holiday_list_id: UUID) -> HolidayList:
        """Get a holiday list by ID."""
        holiday_list = self.db.scalar(
            select(HolidayList)
            .options(joinedload(HolidayList.holidays))
            .where(
                HolidayList.holiday_list_id == holiday_list_id,
                HolidayList.organization_id == org_id,
            )
        )
        if not holiday_list:
            raise HolidayListNotFoundError(holiday_list_id)
        return holiday_list

    def create_holiday_list(
        self,
        org_id: UUID,
        *,
        list_code: str,
        list_name: str,
        year: int | None = None,
        from_date: date,
        to_date: date,
        description: str | None = None,
        weekly_off: str | None = None,
        is_default: bool | None = None,
        is_active: bool | None = None,
        holidays: list[dict] | None = None,
    ) -> HolidayList:
        """Create a new holiday list with holidays."""
        holiday_list_data = {
            "organization_id": org_id,
            "list_code": list_code,
            "list_name": list_name,
            "year": year if year is not None else from_date.year,
            "from_date": from_date,
            "to_date": to_date,
            "description": description,
        }
        if weekly_off is not None:
            holiday_list_data["weekly_off"] = weekly_off
        if is_default is not None:
            holiday_list_data["is_default"] = is_default
        if is_active is not None:
            holiday_list_data["is_active"] = is_active

        holiday_list = HolidayList(**holiday_list_data)

        self.db.add(holiday_list)
        self.db.flush()

        # Add holidays
        if holidays:
            for h in holidays:
                holiday = Holiday(
                    holiday_list_id=holiday_list.holiday_list_id,
                    holiday_date=h["holiday_date"],
                    holiday_name=h["holiday_name"],
                    description=h.get("description"),
                    is_optional=h.get("is_optional", False),
                )
                self.db.add(holiday)

        self.db.flush()
        return holiday_list

    def update_holiday_list(
        self,
        org_id: UUID,
        holiday_list_id: UUID,
        holidays: list[dict] | None = None,
        **kwargs,
    ) -> HolidayList:
        """Update a holiday list."""
        holiday_list = self.get_holiday_list(org_id, holiday_list_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(holiday_list, key):
                setattr(holiday_list, key, value)

        if holidays is not None:
            # Replace holidays to match the submitted list.
            self.db.execute(
                delete(Holiday).where(
                    Holiday.holiday_list_id == holiday_list.holiday_list_id
                )
            )
            # Keep relationship state consistent after bulk delete.
            holiday_list.holidays = []
            self.db.flush()
            for h in holidays:
                holiday = Holiday(
                    holiday_list_id=holiday_list.holiday_list_id,
                    holiday_date=h["holiday_date"],
                    holiday_name=h["holiday_name"],
                    description=h.get("description"),
                    is_optional=h.get("is_optional", False),
                )
                self.db.add(holiday)

        self.db.flush()
        return holiday_list

    def delete_holiday_list(self, org_id: UUID, holiday_list_id: UUID) -> None:
        """Delete a holiday list (soft delete by deactivating)."""
        holiday_list = self.get_holiday_list(org_id, holiday_list_id)
        holiday_list.is_active = False
        self.db.flush()

    def add_holiday(
        self,
        org_id: UUID,
        holiday_list_id: UUID,
        *,
        holiday_date: date,
        holiday_name: str,
        description: str | None = None,
        is_optional: bool = False,
    ) -> Holiday:
        """Add a holiday to a holiday list."""
        self.get_holiday_list(org_id, holiday_list_id)

        holiday = Holiday(
            holiday_list_id=holiday_list_id,
            holiday_date=holiday_date,
            holiday_name=holiday_name,
            description=description,
            is_optional=is_optional,
        )

        self.db.add(holiday)
        self.db.flush()
        return holiday

    def remove_holiday(
        self,
        org_id: UUID,
        holiday_list_id: UUID,
        holiday_id: UUID,
    ) -> None:
        """Remove a holiday from a holiday list."""
        # Verify holiday list exists
        self.get_holiday_list(org_id, holiday_list_id)

        holiday = self.db.scalar(
            select(Holiday).where(
                Holiday.holiday_id == holiday_id,
                Holiday.holiday_list_id == holiday_list_id,
            )
        )
        if not holiday:
            raise LeaveServiceError(
                f"Holiday {holiday_id} not found in list {holiday_list_id}"
            )

        self.db.delete(holiday)
        self.db.flush()

    def is_holiday(
        self,
        org_id: UUID,
        check_date: date,
        holiday_list_id: UUID | None = None,
    ) -> bool:
        """Check if a date is a holiday."""
        query = (
            select(Holiday)
            .join(HolidayList, Holiday.holiday_list_id == HolidayList.holiday_list_id)
            .where(
                HolidayList.organization_id == org_id,
                Holiday.holiday_date == check_date,
            )
        )

        if holiday_list_id:
            query = query.where(Holiday.holiday_list_id == holiday_list_id)

        return self.db.scalar(query) is not None

    # =========================================================================
    # Leave Allocations
    # =========================================================================

    def list_allocations(
        self,
        org_id: UUID,
        *,
        employee_id: UUID | None = None,
        employee_ids: Sequence[UUID] | None = None,
        leave_type_id: UUID | None = None,
        year: int | None = None,
        is_active: bool | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[LeaveAllocation]:
        """List leave allocations."""
        query = select(LeaveAllocation).where(LeaveAllocation.organization_id == org_id)

        if employee_id:
            query = query.where(LeaveAllocation.employee_id == employee_id)
        elif employee_ids is not None:
            if len(employee_ids) == 0:
                return PaginatedResult(
                    items=[],
                    total=0,
                    offset=pagination.offset if pagination else 0,
                    limit=pagination.limit if pagination else 0,
                )
            query = query.where(LeaveAllocation.employee_id.in_(employee_ids))

        if leave_type_id:
            query = query.where(LeaveAllocation.leave_type_id == leave_type_id)

        if year:
            year_start = date(year, 1, 1)
            year_end = date(year, 12, 31)
            query = query.where(
                and_(
                    LeaveAllocation.from_date <= year_end,
                    LeaveAllocation.to_date >= year_start,
                )
            )

        if is_active is not None:
            query = query.where(LeaveAllocation.is_active == is_active)

        query = query.options(
            joinedload(LeaveAllocation.employee).joinedload(Employee.person),
            joinedload(LeaveAllocation.leave_type),
        ).order_by(LeaveAllocation.from_date.desc())

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        # Apply pagination
        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).unique().all())

        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_allocation(self, org_id: UUID, allocation_id: UUID) -> LeaveAllocation:
        """Get a leave allocation by ID."""
        allocation = self.db.scalar(
            select(LeaveAllocation).where(
                LeaveAllocation.allocation_id == allocation_id,
                LeaveAllocation.organization_id == org_id,
            )
        )
        if not allocation:
            raise LeaveAllocationNotFoundError(allocation_id)
        return allocation

    def create_allocation(
        self,
        org_id: UUID,
        *,
        employee_id: UUID,
        leave_type_id: UUID,
        from_date: date,
        to_date: date,
        new_leaves_allocated: Decimal,
        carry_forward_leaves: Decimal = Decimal("0"),
        notes: str | None = None,
    ) -> LeaveAllocation:
        """Create a leave allocation for an employee."""
        # Verify leave type exists
        self.get_leave_type(org_id, leave_type_id)

        existing = self.db.scalar(
            select(LeaveAllocation).where(
                LeaveAllocation.organization_id == org_id,
                LeaveAllocation.employee_id == employee_id,
                LeaveAllocation.leave_type_id == leave_type_id,
                LeaveAllocation.from_date == from_date,
            )
        )
        if existing:
            raise LeaveAllocationExistsError(employee_id, leave_type_id, from_date)

        total_leaves = new_leaves_allocated + carry_forward_leaves

        allocation = LeaveAllocation(
            organization_id=org_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            from_date=from_date,
            to_date=to_date,
            new_leaves_allocated=new_leaves_allocated,
            carry_forward_leaves=carry_forward_leaves,
            total_leaves_allocated=total_leaves,
            leaves_used=Decimal("0"),
            leaves_encashed=Decimal("0"),
            notes=notes,
        )

        self.db.add(allocation)
        self.db.flush()
        return allocation

    def bulk_create_allocations(
        self,
        org_id: UUID,
        *,
        employee_ids: list[UUID],
        leave_type_id: UUID,
        from_date: date,
        to_date: date,
        new_leaves_allocated: Decimal,
        carry_forward_leaves: Decimal = Decimal("0"),
        notes: str | None = None,
    ) -> dict:
        """Bulk create leave allocations for employees."""
        success_count = 0
        failed_count = 0
        errors: list[dict] = []

        for employee_id in employee_ids:
            try:
                self.create_allocation(
                    org_id=org_id,
                    employee_id=employee_id,
                    leave_type_id=leave_type_id,
                    from_date=from_date,
                    to_date=to_date,
                    new_leaves_allocated=new_leaves_allocated,
                    carry_forward_leaves=carry_forward_leaves,
                    notes=notes,
                )
                success_count += 1
            except LeaveServiceError as exc:
                failed_count += 1
                errors.append(
                    {
                        "employee_id": str(employee_id),
                        "reason": str(exc),
                    }
                )

        self.db.flush()
        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors,
        }

    def update_allocation(
        self,
        org_id: UUID,
        allocation_id: UUID,
        **kwargs,
    ) -> LeaveAllocation:
        """Update a leave allocation."""
        allocation = self.get_allocation(org_id, allocation_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(allocation, key):
                setattr(allocation, key, value)

        # Recalculate total if components changed
        if "new_leaves_allocated" in kwargs or "carry_forward_leaves" in kwargs:
            allocation.total_leaves_allocated = (
                allocation.new_leaves_allocated + allocation.carry_forward_leaves
            )

        self.db.flush()
        return allocation

    def delete_allocation(self, org_id: UUID, allocation_id: UUID) -> None:
        """Delete a leave allocation.

        Can only delete allocations that have no leaves taken.
        """
        allocation = self.get_allocation(org_id, allocation_id)

        # Prevent deletion if leaves have been taken
        if allocation.leaves_used > Decimal("0"):
            raise LeaveServiceError(
                f"Cannot delete allocation with {allocation.leaves_used} days already taken. "
                "Consider deactivating it instead."
            )

        self.db.delete(allocation)
        self.db.flush()

    def get_employee_balance(
        self,
        org_id: UUID,
        employee_id: UUID,
        leave_type_id: UUID,
        as_of_date: date | None = None,
    ) -> Decimal:
        """Get employee's leave balance for a leave type."""
        check_date = as_of_date or date.today()

        # Get active allocation
        allocation = self.db.scalar(
            select(LeaveAllocation).where(
                LeaveAllocation.organization_id == org_id,
                LeaveAllocation.employee_id == employee_id,
                LeaveAllocation.leave_type_id == leave_type_id,
                LeaveAllocation.from_date <= check_date,
                LeaveAllocation.to_date >= check_date,
            )
        )

        if not allocation:
            return Decimal("0")

        return (
            allocation.total_leaves_allocated
            - allocation.leaves_used
            - allocation.leaves_encashed
            - allocation.leaves_expired
        )

    def get_employee_balances(
        self,
        org_id: UUID,
        employee_id: UUID,
        as_of_date: date | None = None,
    ) -> list[dict]:
        """Get all leave balances for an employee."""
        check_date = as_of_date or date.today()

        allocations = (
            self.db.scalars(
                select(LeaveAllocation)
                .options(joinedload(LeaveAllocation.leave_type))
                .where(
                    LeaveAllocation.organization_id == org_id,
                    LeaveAllocation.employee_id == employee_id,
                    LeaveAllocation.from_date <= check_date,
                    LeaveAllocation.to_date >= check_date,
                )
            )
            .unique()
            .all()
        )

        balances = []
        for alloc in allocations:
            balance = (
                alloc.total_leaves_allocated
                - alloc.leaves_used
                - alloc.leaves_encashed
                - alloc.leaves_expired
            )
            balances.append(
                {
                    "leave_type_id": alloc.leave_type_id,
                    "leave_type_code": alloc.leave_type.leave_type_code
                    if alloc.leave_type
                    else None,
                    "leave_type_name": alloc.leave_type.leave_type_name
                    if alloc.leave_type
                    else None,
                    "total_allocated": alloc.total_leaves_allocated,
                    "leaves_used": alloc.leaves_used,
                    "leaves_encashed": alloc.leaves_encashed,
                    "leaves_expired": alloc.leaves_expired,
                    "balance": balance,
                    "from_date": alloc.from_date,
                    "to_date": alloc.to_date,
                }
            )

        return balances

    # =========================================================================
    # Leave Applications
    # =========================================================================

    def list_applications(
        self,
        org_id: UUID,
        *,
        employee_id: UUID | None = None,
        employee_ids: Sequence[UUID] | None = None,
        leave_type_id: UUID | None = None,
        status: LeaveApplicationStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[LeaveApplication]:
        """List leave applications."""
        if employee_ids is not None and not employee_ids:
            return PaginatedResult(
                items=[],
                total=0,
                offset=pagination.offset if pagination else 0,
                limit=pagination.limit if pagination else 0,
            )

        filters: list[ColumnElement[bool]] = [
            LeaveApplication.organization_id == org_id
        ]
        if employee_id:
            filters.append(LeaveApplication.employee_id == employee_id)
        elif employee_ids is not None:
            filters.append(LeaveApplication.employee_id.in_(employee_ids))

        if leave_type_id:
            filters.append(LeaveApplication.leave_type_id == leave_type_id)

        if status:
            status_value = (
                LeaveApplicationStatus(status) if isinstance(status, str) else status
            )
            filters.append(LeaveApplication.status == status_value)

        if from_date:
            filters.append(LeaveApplication.from_date >= from_date)

        if to_date:
            filters.append(LeaveApplication.to_date <= to_date)

        query = (
            select(LeaveApplication)
            .options(
                joinedload(LeaveApplication.employee).joinedload(Employee.person),
                joinedload(LeaveApplication.leave_type),
            )
            .where(*filters)
            .order_by(LeaveApplication.from_date.desc())
        )

        count_query = select(func.count(LeaveApplication.application_id)).where(
            *filters
        )
        total = self.db.scalar(count_query) or 0

        # Apply pagination
        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).unique().all())

        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def list_team_applications(
        self,
        org_id: UUID,
        *,
        employee_ids: Sequence[UUID],
        status: LeaveApplicationStatus | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[LeaveApplication]:
        """List leave applications for a set of employees."""
        if not employee_ids:
            return PaginatedResult(
                items=[], total=0, offset=0, limit=pagination.limit if pagination else 0
            )

        query = (
            select(LeaveApplication)
            .options(
                joinedload(LeaveApplication.employee).joinedload(Employee.person),
                joinedload(LeaveApplication.leave_type),
            )
            .where(
                LeaveApplication.organization_id == org_id,
                LeaveApplication.employee_id.in_(employee_ids),
            )
        )

        if status:
            status_value = status
            if isinstance(status, str):
                status_value = LeaveApplicationStatus(status)
            query = query.where(LeaveApplication.status == status_value)

        query = query.order_by(LeaveApplication.from_date.desc())

        # Count total (separate query without joinedload)
        count_query = select(func.count(LeaveApplication.application_id)).where(
            LeaveApplication.organization_id == org_id,
            LeaveApplication.employee_id.in_(employee_ids),
        )
        if status:
            count_query = count_query.where(
                LeaveApplication.status
                == (
                    LeaveApplicationStatus(status)
                    if isinstance(status, str)
                    else status
                )
            )
        total = self.db.scalar(count_query) or 0

        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).unique().all())

        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_application(self, org_id: UUID, application_id: UUID) -> LeaveApplication:
        """Get a leave application by ID."""
        application = self.db.scalar(
            select(LeaveApplication).where(
                LeaveApplication.application_id == application_id,
                LeaveApplication.organization_id == org_id,
            )
        )
        if not application:
            raise LeaveApplicationNotFoundError(application_id)
        return application

    def calculate_leave_days(
        self,
        org_id: UUID,
        from_date: date,
        to_date: date,
        *,
        half_day: bool = False,
        include_holidays: bool = False,
        holiday_list_id: UUID | None = None,
    ) -> Decimal:
        """Calculate the number of leave days between two dates."""
        if from_date > to_date:
            raise LeaveServiceError("From date cannot be after to date")

        if half_day:
            return Decimal("0.5")

        total_days = Decimal("0")
        current = from_date

        while current <= to_date:
            # Skip weekends (Saturday=5, Sunday=6)
            if current.weekday() < 5:
                # Check if it's a holiday
                if include_holidays or not self.is_holiday(
                    org_id, current, holiday_list_id
                ):
                    total_days += Decimal("1")

            current += timedelta(days=1)

        return total_days

    def create_application(
        self,
        org_id: UUID,
        *,
        employee_id: UUID,
        leave_type_id: UUID,
        from_date: date,
        to_date: date,
        half_day: bool = False,
        half_day_date: date | None = None,
        reason: str | None = None,
        holiday_list_id: UUID | None = None,
        leave_approver_id: UUID | None = None,
    ) -> LeaveApplication:
        """Create a new leave application."""
        # Verify leave type
        leave_type = self.get_leave_type(org_id, leave_type_id)

        # Calculate leave days
        total_days = self.calculate_leave_days(
            org_id,
            from_date,
            to_date,
            half_day=half_day,
            include_holidays=leave_type.include_holidays,
            holiday_list_id=holiday_list_id,
        )

        self._validate_service_eligibility(
            org_id,
            employee_id=employee_id,
            leave_type=leave_type,
            total_days=total_days,
        )

        # Check balance (skip for LWP)
        if not leave_type.is_lwp:
            balance = self.get_employee_balance(org_id, employee_id, leave_type_id)
            if balance < total_days:
                raise InsufficientLeaveBalanceError(balance, total_days)

        # Check for overlapping applications
        overlapping = self.db.scalar(
            select(LeaveApplication).where(
                LeaveApplication.organization_id == org_id,
                LeaveApplication.employee_id == employee_id,
                LeaveApplication.status.in_(
                    [
                        LeaveApplicationStatus.SUBMITTED,
                        LeaveApplicationStatus.APPROVED,
                    ]
                ),
                or_(
                    and_(
                        LeaveApplication.from_date <= from_date,
                        LeaveApplication.to_date >= from_date,
                    ),
                    and_(
                        LeaveApplication.from_date <= to_date,
                        LeaveApplication.to_date >= to_date,
                    ),
                    and_(
                        LeaveApplication.from_date >= from_date,
                        LeaveApplication.to_date <= to_date,
                    ),
                ),
            )
        )
        if overlapping:
            raise OverlappingLeaveApplicationError(
                overlapping.from_date,
                overlapping.to_date,
            )

        # Check for active disciplinary investigation
        from app.services.people.discipline import DisciplineService

        discipline_service = DisciplineService(self.db)
        if discipline_service.has_active_investigation(org_id, employee_id):
            raise LeaveServiceError(
                "Leave applications are restricted during an active disciplinary investigation"
            )

        application_number = self._next_application_number(org_id)

        application = LeaveApplication(
            organization_id=org_id,
            application_number=application_number,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            from_date=from_date,
            to_date=to_date,
            total_leave_days=total_days,
            half_day=half_day,
            half_day_date=half_day_date,
            reason=reason,
            status=LeaveApplicationStatus.SUBMITTED,
            leave_approver_id=leave_approver_id,
        )

        self.db.add(application)
        self.db.flush()
        self._notify_leave_submitted(org_id, application)

        fire_audit_event(
            db=self.db,
            organization_id=org_id,
            table_schema="hr",
            table_name="leave_application",
            record_id=str(application.application_id),
            action=AuditAction.INSERT,
            new_values={
                "status": LeaveApplicationStatus.SUBMITTED.value,
                "employee_id": str(employee_id),
                "leave_type_id": str(leave_type_id),
                "from_date": str(from_date),
                "to_date": str(to_date),
                "total_leave_days": str(total_days),
            },
        )

        return application

    def approve_application(
        self,
        org_id: UUID,
        application_id: UUID,
        *,
        approver_id: UUID | None = None,
        notes: str | None = None,
    ) -> LeaveApplication:
        """Approve a leave application."""
        application = self.get_application(org_id, application_id)

        self._validate_transition(application.status, LeaveApplicationStatus.APPROVED)

        application.status = LeaveApplicationStatus.APPROVED
        application.approved_by_id = approver_id
        application.approved_at = datetime.now(UTC)

        # Update allocation
        allocation = self.db.scalar(
            select(LeaveAllocation).where(
                LeaveAllocation.organization_id == org_id,
                LeaveAllocation.employee_id == application.employee_id,
                LeaveAllocation.leave_type_id == application.leave_type_id,
                LeaveAllocation.from_date <= application.from_date,
                LeaveAllocation.to_date >= application.to_date,
            )
        )
        if allocation:
            allocation.leaves_used += application.total_leave_days

        today = self.get_org_today(org_id)
        if application.from_date <= today <= application.to_date:
            self.sync_employee_leave_status(
                org_id,
                application.employee_id,
                as_of_date=today,
            )

        self.db.flush()
        self._project_leave_access_restriction(application)

        fire_audit_event(
            db=self.db,
            organization_id=org_id,
            table_schema="hr",
            table_name="leave_application",
            record_id=str(application.application_id),
            action=AuditAction.UPDATE,
            old_values={"status": LeaveApplicationStatus.SUBMITTED.value},
            new_values={"status": LeaveApplicationStatus.APPROVED.value},
        )

        # Fire workflow automation event
        try:
            from app.services.finance.automation.event_dispatcher import (
                fire_workflow_event,
            )

            fire_workflow_event(
                db=self.db,
                organization_id=org_id,
                entity_type="LEAVE_REQUEST",
                entity_id=application.application_id,
                event="ON_APPROVAL",
                old_values={"status": "SUBMITTED"},
                new_values={"status": "APPROVED"},
                user_id=approver_id,
            )
        except Exception:
            logger.exception("Ignored exception")

        return application

    def bulk_approve_applications(
        self,
        org_id: UUID,
        application_ids: list[UUID],
        *,
        approver_id: UUID | None = None,
        notes: str | None = None,
    ) -> dict:
        """Bulk approve leave applications."""
        updated = 0
        for app_id in application_ids:
            try:
                self.approve_application(
                    org_id=org_id,
                    application_id=app_id,
                    approver_id=approver_id,
                    notes=notes,
                )
                updated += 1
            except LeaveServiceError:
                continue
        return {"updated": updated, "requested": len(application_ids)}

    def reject_application(
        self,
        org_id: UUID,
        application_id: UUID,
        *,
        approver_id: UUID | None = None,
        reason: str,
    ) -> LeaveApplication:
        """Reject a leave application."""
        application = self.get_application(org_id, application_id)

        self._validate_transition(application.status, LeaveApplicationStatus.REJECTED)

        application.status = LeaveApplicationStatus.REJECTED
        application.approved_by_id = approver_id
        application.approved_at = datetime.now(UTC)
        application.rejection_reason = reason

        self.db.flush()
        self._project_leave_access_restriction(application, reason=reason)

        fire_audit_event(
            db=self.db,
            organization_id=org_id,
            table_schema="hr",
            table_name="leave_application",
            record_id=str(application.application_id),
            action=AuditAction.UPDATE,
            old_values={"status": LeaveApplicationStatus.SUBMITTED.value},
            new_values={"status": LeaveApplicationStatus.REJECTED.value},
        )

        # Fire workflow automation event
        try:
            from app.services.finance.automation.event_dispatcher import (
                fire_workflow_event,
            )

            fire_workflow_event(
                db=self.db,
                organization_id=org_id,
                entity_type="LEAVE_REQUEST",
                entity_id=application.application_id,
                event="ON_REJECTION",
                old_values={"status": "SUBMITTED"},
                new_values={"status": "REJECTED", "rejection_reason": reason},
                user_id=approver_id,
            )
        except Exception:
            logger.exception("Ignored exception")

        return application

    def bulk_reject_applications(
        self,
        org_id: UUID,
        application_ids: list[UUID],
        *,
        approver_id: UUID | None = None,
        reason: str | None = None,
    ) -> dict:
        """Bulk reject leave applications."""
        updated = 0
        for app_id in application_ids:
            try:
                self.reject_application(
                    org_id=org_id,
                    application_id=app_id,
                    approver_id=approver_id,
                    reason=reason or "Rejected",
                )
                updated += 1
            except LeaveServiceError:
                continue
        return {"updated": updated, "requested": len(application_ids)}

    def cancel_application(
        self,
        org_id: UUID,
        application_id: UUID,
        *,
        reason: str | None = None,
    ) -> LeaveApplication:
        """Cancel a leave application."""
        application = self.get_application(org_id, application_id)

        self._validate_transition(application.status, LeaveApplicationStatus.CANCELLED)

        # If approved, restore balance
        if application.status == LeaveApplicationStatus.APPROVED:
            allocation = self.db.scalar(
                select(LeaveAllocation).where(
                    LeaveAllocation.organization_id == org_id,
                    LeaveAllocation.employee_id == application.employee_id,
                    LeaveAllocation.leave_type_id == application.leave_type_id,
                    LeaveAllocation.from_date <= application.from_date,
                    LeaveAllocation.to_date >= application.to_date,
                )
            )
            if allocation:
                allocation.leaves_used = max(
                    Decimal("0"),
                    allocation.leaves_used - application.total_leave_days,
                )

        old_status = application.status.value
        application.status = LeaveApplicationStatus.CANCELLED

        today = self.get_org_today(org_id)
        if application.from_date <= today <= application.to_date:
            self.sync_employee_leave_status(
                org_id,
                application.employee_id,
                as_of_date=today,
            )

        self.db.flush()
        self._project_leave_access_restriction(application, reason=reason)

        fire_audit_event(
            db=self.db,
            organization_id=org_id,
            table_schema="hr",
            table_name="leave_application",
            record_id=str(application.application_id),
            action=AuditAction.UPDATE,
            old_values={"status": old_status},
            new_values={"status": LeaveApplicationStatus.CANCELLED.value},
        )

        # Fire workflow automation event
        try:
            from app.services.finance.automation.event_dispatcher import (
                fire_workflow_event,
            )

            fire_workflow_event(
                db=self.db,
                organization_id=org_id,
                entity_type="LEAVE_REQUEST",
                entity_id=application.application_id,
                event="ON_STATUS_CHANGE",
                old_values={"status": old_status},
                new_values={"status": "CANCELLED"},
            )
        except Exception:
            logger.exception("Ignored exception")

        return application

    def update_application(
        self,
        org_id: UUID,
        application_id: UUID,
        **kwargs,
    ) -> LeaveApplication:
        """Update a leave application.

        Can only update applications in DRAFT or SUBMITTED status.
        """
        application = self.get_application(org_id, application_id)

        # Only allow updates to draft or submitted applications
        if application.status not in (
            LeaveApplicationStatus.DRAFT,
            LeaveApplicationStatus.SUBMITTED,
        ):
            raise LeaveApplicationStatusError(
                application.status.value,
                "updated (only DRAFT or SUBMITTED can be edited)",
            )

        # Track if dates changed for recalculation
        dates_changed = False

        for key, value in kwargs.items():
            if value is not None and hasattr(application, key):
                setattr(application, key, value)
                if key in ("from_date", "to_date", "half_day"):
                    dates_changed = True

        # Recalculate leave days if dates changed
        if dates_changed:
            leave_type = self.get_leave_type(org_id, application.leave_type_id)
            total_days = self.calculate_leave_days(
                org_id,
                application.from_date,
                application.to_date,
                half_day=application.half_day,
                include_holidays=leave_type.include_holidays,
            )
            application.total_leave_days = total_days

            # Re-check balance if not LWP
            if not leave_type.is_lwp:
                balance = self.get_employee_balance(
                    org_id, application.employee_id, application.leave_type_id
                )
                if balance < total_days:
                    raise InsufficientLeaveBalanceError(balance, total_days)

        self.db.flush()
        return application

    def delete_application(self, org_id: UUID, application_id: UUID) -> None:
        """Delete a leave application.

        Can only delete applications in DRAFT or CANCELLED status.
        """
        application = self.get_application(org_id, application_id)

        # Only allow deletion of draft or cancelled applications
        if application.status not in (
            LeaveApplicationStatus.DRAFT,
            LeaveApplicationStatus.CANCELLED,
        ):
            raise LeaveServiceError(
                f"Cannot delete application in {application.status.value} status. "
                "Only DRAFT or CANCELLED applications can be deleted."
            )

        self.db.delete(application)
        self.db.flush()

    # =========================================================================
    # Reporting
    # =========================================================================

    def get_leave_stats(self, org_id: UUID) -> dict:
        """Get leave statistics for dashboard."""
        today = date.today()

        # Pending applications
        pending_count = (
            self.db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    LeaveApplication.organization_id == org_id,
                    LeaveApplication.status == LeaveApplicationStatus.SUBMITTED,
                )
            )
            or 0
        )

        # On leave today
        on_leave_today = (
            self.db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    LeaveApplication.organization_id == org_id,
                    LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                    LeaveApplication.from_date <= today,
                    LeaveApplication.to_date >= today,
                )
            )
            or 0
        )

        # Applications this month
        month_start = datetime.combine(today.replace(day=1), datetime.min.time())
        applications_this_month = (
            self.db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    LeaveApplication.organization_id == org_id,
                    LeaveApplication.created_at >= month_start,
                )
            )
            or 0
        )

        return {
            "pending_applications": pending_count,
            "on_leave_today": on_leave_today,
            "applications_this_month": applications_this_month,
        }

    # =========================================================================
    # Report Methods
    # =========================================================================

    def get_leave_balance_report(
        self,
        org_id: UUID,
        *,
        department_id: UUID | None = None,
        year: int | None = None,
        employee_search: str | None = None,
    ) -> dict:
        """
        Get leave balance report by employee.

        Returns a list of employees with their leave balances across all leave types.
        """
        from app.models.people.hr import Department, Employee, EmployeeStatus
        from app.models.person import Person

        target_year = year or date.today().year

        # Get active allocations for the year
        alloc_query = (
            select(
                Employee.employee_id,
                Person.name_expr().label("employee_name"),
                Department.department_name.label("department_name"),
                LeaveType.leave_type_name,
                LeaveType.leave_type_id,
                func.sum(
                    LeaveAllocation.new_leaves_allocated
                    + LeaveAllocation.carry_forward_leaves
                ).label("total_allocated"),
                func.sum(LeaveAllocation.leaves_used).label("leaves_used"),
                func.sum(
                    LeaveAllocation.new_leaves_allocated
                    + LeaveAllocation.carry_forward_leaves
                    - LeaveAllocation.leaves_used
                    - LeaveAllocation.leaves_encashed
                    - LeaveAllocation.leaves_expired
                ).label("balance"),
            )
            .join(LeaveAllocation, LeaveAllocation.employee_id == Employee.employee_id)
            .join(Person, Person.id == Employee.person_id)
            .join(LeaveType, LeaveType.leave_type_id == LeaveAllocation.leave_type_id)
            .outerjoin(Department, Employee.department_id == Department.department_id)
            .where(
                Employee.organization_id == org_id,
                Person.organization_id == org_id,
                Employee.status.in_(
                    [
                        EmployeeStatus.ACTIVE,
                        EmployeeStatus.ON_LEAVE,
                    ]
                ),
                func.extract("year", LeaveAllocation.from_date) == target_year,
            )
        )

        if department_id:
            alloc_query = alloc_query.where(Employee.department_id == department_id)

        search_term = (employee_search or "").strip()
        if search_term:
            pattern = f"%{search_term}%"
            alloc_query = alloc_query.where(_employee_search_predicate(pattern))

        results = self.db.execute(
            alloc_query.group_by(
                Employee.employee_id,
                Person.display_name,
                Person.first_name,
                Person.last_name,
                Department.department_name,
                LeaveType.leave_type_name,
                LeaveType.leave_type_id,
            )
        ).all()

        # Organize by employee
        employees_dict: dict[str, EmployeeLeaveSummary] = {}
        for row in results:
            emp_id = str(row.employee_id)
            if emp_id not in employees_dict:
                employees_dict[emp_id] = {
                    "employee_id": emp_id,
                    "employee_name": row.employee_name,
                    "department_name": row.department_name or "No Department",
                    "leave_balances": [],
                    "total_allocated": Decimal("0"),
                    "total_used": Decimal("0"),
                    "total_balance": Decimal("0"),
                }
            employees_dict[emp_id]["leave_balances"].append(
                {
                    "leave_type_name": row.leave_type_name,
                    "allocated": row.total_allocated or Decimal("0"),
                    "used": row.leaves_used or Decimal("0"),
                    "balance": row.balance or Decimal("0"),
                }
            )
            employees_dict[emp_id]["total_allocated"] += row.total_allocated or Decimal(
                "0"
            )
            employees_dict[emp_id]["total_used"] += row.leaves_used or Decimal("0")
            employees_dict[emp_id]["total_balance"] += row.balance or Decimal("0")

        return {
            "year": target_year,
            "employees": list(employees_dict.values()),
            "total_employees": len(employees_dict),
        }

    def get_leave_usage_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """
        Get leave usage report by leave type.

        Returns breakdown of leave usage by type with counts and days.
        """
        today = date.today()
        if not start_date:
            start_date = today.replace(day=1)
        if not end_date:
            end_date = today

        # Query approved applications by leave type
        results = self.db.execute(
            select(
                LeaveType.leave_type_code,
                LeaveType.leave_type_name,
                func.count(LeaveApplication.application_id).label("application_count"),
                func.sum(LeaveApplication.total_leave_days).label("total_days"),
            )
            .join(
                LeaveApplication,
                LeaveApplication.leave_type_id == LeaveType.leave_type_id,
            )
            .where(
                LeaveApplication.organization_id == org_id,
                LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                LeaveApplication.from_date >= start_date,
                LeaveApplication.to_date <= end_date,
            )
            .group_by(
                LeaveType.leave_type_id,
                LeaveType.leave_type_code,
                LeaveType.leave_type_name,
            )
            .order_by(func.sum(LeaveApplication.total_leave_days).desc())
        ).all()

        leave_types = []
        total_applications = 0
        total_days = Decimal("0")

        for row in results:
            days = row.total_days or Decimal("0")
            leave_types.append(
                {
                    "leave_type_code": row.leave_type_code,
                    "leave_type_name": row.leave_type_name,
                    "application_count": row.application_count,
                    "total_days": days,
                }
            )
            total_applications += row.application_count
            total_days += days

        # Calculate percentages
        for lt in leave_types:
            if total_days > 0:
                lt["percentage"] = float(lt["total_days"] / total_days * 100)
            else:
                lt["percentage"] = 0.0

        return {
            "start_date": start_date,
            "end_date": end_date,
            "leave_types": leave_types,
            "total_applications": total_applications,
            "total_days": total_days,
        }

    def get_leave_calendar(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        department_id: UUID | None = None,
    ) -> dict:
        """
        Get leave calendar data.

        Returns a list of approved leave applications for the given period
        suitable for calendar display.
        """
        from app.models.people.hr import Department, Employee
        from app.models.person import Person

        today = date.today()
        if not start_date:
            start_date = today.replace(day=1)
        if not end_date:
            # Default to end of month
            next_month = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
            end_date = next_month - timedelta(days=1)

        # Query approved applications
        query = (
            select(
                LeaveApplication,
                Person.name_expr().label("employee_name"),
                Department.department_name.label("department_name"),
                LeaveType.leave_type_name,
            )
            .join(Employee, Employee.employee_id == LeaveApplication.employee_id)
            .join(Person, Person.id == Employee.person_id)
            .outerjoin(Department, Employee.department_id == Department.department_id)
            .join(LeaveType, LeaveType.leave_type_id == LeaveApplication.leave_type_id)
            .where(
                LeaveApplication.organization_id == org_id,
                LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                LeaveApplication.from_date <= end_date,
                LeaveApplication.to_date >= start_date,
            )
        )

        if department_id:
            query = query.where(Employee.department_id == department_id)

        results = self.db.execute(query.order_by(LeaveApplication.from_date)).all()

        leave_events = []
        for app, employee_name, dept_name, leave_type_name in results:
            leave_events.append(
                {
                    "application_id": str(app.application_id),
                    "employee_name": employee_name,
                    "department_name": dept_name or "No Department",
                    "leave_type_name": leave_type_name,
                    "from_date": app.from_date.isoformat(),
                    "to_date": app.to_date.isoformat(),
                    "total_days": float(app.total_leave_days),
                    "half_day": app.half_day,
                }
            )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "leave_events": leave_events,
            "total_events": len(leave_events),
        }

    def get_leave_trends_report(
        self,
        org_id: UUID,
        *,
        months: int = 12,
    ) -> dict:
        """
        Get leave application trends over time.

        Returns monthly breakdown of leave applications.
        """
        from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

        today = date.today()
        end_date = today.replace(day=1)
        start_date = end_date - relativedelta(months=months - 1)

        # Query monthly aggregates
        month_expr = func.date_trunc("month", LeaveApplication.from_date).label("month")
        results = self.db.execute(
            select(
                month_expr,
                func.count(LeaveApplication.application_id).label("application_count"),
                func.sum(LeaveApplication.total_leave_days).label("total_days"),
                func.count(
                    case(
                        (
                            LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                            LeaveApplication.application_id,
                        ),
                    )
                ).label("approved_count"),
                func.count(
                    case(
                        (
                            LeaveApplication.status == LeaveApplicationStatus.REJECTED,
                            LeaveApplication.application_id,
                        ),
                    )
                ).label("rejected_count"),
            )
            .where(
                LeaveApplication.organization_id == org_id,
                LeaveApplication.from_date >= start_date,
                LeaveApplication.from_date <= today,
            )
            .group_by(month_expr)
            .order_by(month_expr)
        ).all()

        # Build results dict by month
        monthly_data = {}
        for row in results:
            month_key = row.month.strftime("%Y-%m")
            monthly_data[month_key] = {
                "month": month_key,
                "month_label": row.month.strftime("%b %Y"),
                "application_count": row.application_count,
                "total_days": row.total_days or Decimal("0"),
                "approved_count": row.approved_count,
                "rejected_count": row.rejected_count,
            }

        # Fill in missing months with zeros
        months_list = []
        current = start_date
        total_applications = 0
        total_days = Decimal("0")

        while current <= today:
            month_key = current.strftime("%Y-%m")
            if month_key in monthly_data:
                months_list.append(monthly_data[month_key])
                total_applications += monthly_data[month_key]["application_count"]
                total_days += monthly_data[month_key]["total_days"]
            else:
                months_list.append(
                    {
                        "month": month_key,
                        "month_label": current.strftime("%b %Y"),
                        "application_count": 0,
                        "total_days": Decimal("0"),
                        "approved_count": 0,
                        "rejected_count": 0,
                    }
                )
            current = current + relativedelta(months=1)

        num_months = len(months_list)
        average_monthly_apps = total_applications / num_months if num_months > 0 else 0
        average_monthly_days = (
            total_days / num_months if num_months > 0 else Decimal("0")
        )

        return {
            "months": months_list,
            "total_months": num_months,
            "total_applications": total_applications,
            "total_days": total_days,
            "average_monthly_applications": average_monthly_apps,
            "average_monthly_days": average_monthly_days,
        }

    # =========================================================================
    # Payroll Integration
    # =========================================================================

    def calculate_lwp_days_in_period(
        self,
        leaves: list,
        period_start: date,
        period_end: date,
    ) -> Decimal:
        """
        Calculate total LWP days that overlap with a pay period.

        Only counts leave applications where the leave_type.is_lwp is True
        and the leave overlaps with the given period.

        Args:
            leaves: List of LeaveApplication objects
            period_start: Start of the pay period (inclusive)
            period_end: End of the pay period (inclusive)

        Returns:
            Total LWP days as Decimal (handles half-days as 0.5)
        """
        total_days = Decimal("0")

        for leave in leaves:
            # Get leave type to check if it's LWP
            leave_type = getattr(leave, "leave_type", None)
            if leave_type and not getattr(leave_type, "is_lwp", False):
                continue

            # Get leave dates
            leave_start = getattr(leave, "from_date", None)
            leave_end = getattr(leave, "to_date", None)

            if not leave_start or not leave_end:
                continue

            # Calculate overlap with period
            overlap_start = max(leave_start, period_start)
            overlap_end = min(leave_end, period_end)

            if overlap_start > overlap_end:
                # No overlap
                continue

            # Calculate days in overlap
            overlap_days = (overlap_end - overlap_start).days + 1

            # Handle half-day leaves
            is_half_day = getattr(leave, "half_day", False)
            half_day_date = getattr(leave, "half_day_date", None)

            if is_half_day and half_day_date:
                # Half-day leave only counts if the half-day date is in the overlap
                if period_start <= half_day_date <= period_end:
                    total_days += Decimal("0.5")
            else:
                total_days += Decimal(str(overlap_days))

        return total_days

    def mark_leave_posted_to_payroll(
        self,
        leaves: list,
        slip_id: UUID,
    ) -> None:
        """
        Mark leave applications as posted to payroll.

        Sets is_posted_to_payroll=True and salary_slip_id on each leave.

        Args:
            leaves: List of LeaveApplication objects
            slip_id: The salary slip ID that includes these leaves
        """
        for leave in leaves:
            leave.is_posted_to_payroll = True
            leave.salary_slip_id = slip_id

        self.db.flush()
