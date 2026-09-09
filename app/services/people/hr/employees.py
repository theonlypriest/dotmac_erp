"""Employee service - business logic for employee management.

This service encapsulates employee-related business logic:
- Employee CRUD operations
- Org chart / reporting hierarchy
- Status management (activate, terminate, etc.)
- Bulk operations

Routes should call this service and control the transaction boundary.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, cast

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from sqlalchemy import func, inspect as inspect_db, literal, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, selectinload

from app.db.session_context import session_for_org
from app.models.auth import AuthProvider, UserCredential
from app.models.finance.audit.audit_log import AuditAction
from app.models.finance.core_org.cost_center import CostCenter
from app.models.finance.core_org.location import Location
from app.models.people.attendance.shift_type import ShiftType
from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeGrade,
    EmployeeStatus,
    Position,
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.person import Person
from app.models.rbac import PersonRole, Role
from app.services.audit_dispatcher import fire_audit_event
from app.services.auth_flow import hash_password, request_password_reset
from app.services.common import PaginatedResult, PaginationParams, paginate
from app.services.email import send_password_reset_email
from app.services.people.hr.employment_types import EmploymentTypeService
from app.services.people.hr.invite_email import (
    EMPLOYEE_INVITE_NEXT_URL,
    get_employee_invite_email_template,
)
from app.services.people.hr.invite_attachment import load_default_invite_attachment
from app.services.people.hr.org_resolver import OrgResolver

from .employee_filter_contract import FilterExpression
from .employee_filter_engine import apply_employee_filter_expression
from .employee_types import (
    BulkResult,
    BulkUpdateData,
    EmployeeCreateData,
    EmployeeFilters,
    EmployeeSummary,
    EmployeeUpdateData,
    TerminationData,
)
from .errors import (
    EmployeeAlreadyExistsError,
    EmployeeNotFoundError,
    EmployeeStatusError,
    InvalidManagerError,
    ValidationError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmployeeInviteResult:
    sent: bool
    recipient_email: str
    recipient_kind: str
    attempted_recipients: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.sent


def send_employee_access_invite_background(
    organization_id: uuid.UUID,
    employee_id: uuid.UUID,
    app_url: str | None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send an employee access invite outside the create request."""
    try:
        with session_for_org(organization_id) as db:
            sent = EmployeeService(db, organization_id).send_employee_access_invite(
                employee_id,
                app_url=app_url,
                attachments=attachments,
            )
            if not sent:
                logger.warning(
                    "Employee access invite was not sent for %s", employee_id
                )
    except Exception:
        logger.exception("Employee access invite failed for %s", employee_id)


DEFAULT_NEW_LOCAL_PASSWORD = "Dotmac@123"  # noqa: S105  # nosec B105

if TYPE_CHECKING:
    from app.auth import Principal

__all__ = ["EmployeeInviteResult", "EmployeeService"]


def _employee_search_predicate(search_term: str):
    """Build the shared employee search predicate."""
    full_name = func.trim(
        func.coalesce(Person.first_name, "")
        + literal(" ")
        + func.coalesce(Person.last_name, "")
    )
    display_or_full_name = func.coalesce(Person.display_name, full_name)
    return or_(
        Employee.employee_code.ilike(search_term),
        Person.first_name.ilike(search_term),
        Person.last_name.ilike(search_term),
        display_or_full_name.ilike(search_term),
        full_name.ilike(search_term),
        Person.email.ilike(search_term),
    )


class EmployeeService:
    """Service for employee business logic.

    All methods that mutate data do NOT commit. The caller (route handler)
    is responsible for calling db.commit() after the operation succeeds.

    Args:
        db: SQLAlchemy database session.
        organization_id: The organization UUID for multi-tenant isolation.
        principal: Authenticated user/service token (for audit fields).
    """

    def __init__(
        self,
        db: Session,
        organization_id: uuid.UUID,
        principal: Principal | None = None,
    ) -> None:
        self.db = db
        self.organization_id = organization_id
        self.principal = principal

    # =========================================================================
    # Validation Helpers
    # =========================================================================

    def _validate_manager(
        self,
        employee_id: uuid.UUID,
        manager_id: uuid.UUID,
    ) -> bool:
        """
        Check that setting ``manager_id`` would not create a reporting cycle.

        Walks the position parent chain from the manager's active assignment
        upward; if the employee's own position appears, the assignment is a
        cycle. When either party has no active assignment, no cycle is
        possible (one of them is not in the tree at all), so the check
        returns True — caller paths that need a manager to actually be in
        the tree should validate that separately.
        """
        if employee_id == manager_id:
            return False

        resolver = OrgResolver(self.db)
        employee_assignment = resolver.get_active_assignment(
            employee_id,
            self.organization_id,
        )
        manager_assignment = resolver.get_active_assignment(
            manager_id,
            self.organization_id,
        )
        if not employee_assignment or not manager_assignment:
            return True

        current_position_id: uuid.UUID | None = manager_assignment.position_id
        visited_positions = {employee_assignment.position_id}
        while current_position_id:
            if current_position_id in visited_positions:
                return False
            visited_positions.add(current_position_id)
            position = self.db.scalar(
                select(Position).where(
                    Position.position_id == current_position_id,
                    Position.organization_id == self.organization_id,
                    Position.is_active.is_(True),
                )
            )
            if not position:
                return True
            current_position_id = position.parent_position_id
        return True

    def set_manager(
        self,
        employee_id: uuid.UUID,
        manager_employee_id: uuid.UUID | None,
        *,
        validate_cycle: bool = True,
    ) -> None:
        """
        Update an employee's manager through the canonical chokepoint.

        Updates the parent of the employee's active position so position
        reads (OrgResolver) reflect the new manager, and mirrors the value
        into the legacy ``reports_to_id`` cache for older response shapes and
        integrations. Direct writers (forms, API, lifecycle transitions,
        recruit, import) call this instead of touching the legacy column
        directly whenever they are changing one employee at a time.

        Skips ``validate_cycle`` for callers that have already validated, such
        as ``update_employee`` which checks before writing.
        """
        employee = self.db.scalar(
            select(Employee).where(
                Employee.employee_id == employee_id,
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if not employee:
            raise ValidationError(f"Employee with ID {employee_id} not found")

        from app.services.people.hr.positions import PositionService

        position_service = PositionService(self.db, self.organization_id)
        position_service.provision_positions_for_employees([employee_id])

        if manager_employee_id is not None:
            manager = self.db.scalar(
                select(Employee).where(
                    Employee.employee_id == manager_employee_id,
                    Employee.organization_id == self.organization_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if not manager:
                raise ValidationError(
                    f"Manager with ID {manager_employee_id} not found"
                )
            position_service.provision_positions_for_employees([manager_employee_id])
            if validate_cycle and not self._validate_manager(
                employee_id, manager_employee_id
            ):
                raise InvalidManagerError()

        employee.reports_to_id = manager_employee_id
        self._set_manager_position(employee_id, manager_employee_id)

    def _set_manager_position(
        self,
        employee_id: uuid.UUID,
        manager_employee_id: uuid.UUID | None,
    ) -> None:
        """Update the parent of the employee's active position."""
        from app.services.people.hr.positions import PositionService

        PositionService(self.db, self.organization_id).sync_employee_manager_position(
            employee_id,
            manager_employee_id,
        )

    def _generate_employee_code(self) -> str:
        """Generate a unique employee code.

        Uses sequence: EMP-YYYY-NNNN format.
        Delegates to SyncNumberingService for race-condition-safe generation.

        Returns:
            Generated employee code string.
        """
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        return SyncNumberingService(self.db).generate_next_number(
            self.organization_id, SequenceType.EMPLOYEE
        )

    def _validate_org_reference(
        self,
        model: type,
        entity_id: uuid.UUID | None,
        label: str,
    ) -> None:
        """Ensure a referenced entity exists within the organization."""
        if entity_id is None:
            return
        record = self.db.get(model, entity_id)
        if (
            not record
            or getattr(record, "organization_id", None) != self.organization_id
        ):
            raise ValidationError(f"{label} {entity_id} not found")
        # Lifecycle check: TERMINATED employee or inactive Department/Designation
        if (
            getattr(record, "status", None) == EmployeeStatus.TERMINATED
            or getattr(record, "is_active", True) is False
        ):
            raise ValidationError(f"{label} {entity_id} not found")

    # =========================================================================
    # Queries
    # =========================================================================

    def list_employees(
        self,
        filters: EmployeeFilters | None = None,
        pagination: PaginationParams | None = None,
        *,
        eager_load: bool = False,
        advanced_filter_expression: FilterExpression | None = None,
    ) -> PaginatedResult[Employee]:
        """List employees with filters and pagination.

        Args:
            filters: Optional filter criteria.
            pagination: Pagination parameters (offset, limit).
            eager_load: If True, eager load person, department, and designation
                relationships to avoid N+1 queries. Use for web views.

        Returns:
            PaginatedResult containing employees and total count.
        """
        if filters is None:
            filters = EmployeeFilters()
        if pagination is None:
            pagination = PaginationParams()

        # Normalize status filter
        if isinstance(filters.status, str):
            status_value = filters.status.strip()
            if status_value:
                try:
                    filters.status = EmployeeStatus(status_value.upper())
                except ValueError:
                    filters.status = None
            else:
                filters.status = None

        stmt = select(Employee).where(
            Employee.organization_id == self.organization_id,
        )
        joined_person = False

        if filters.archive_only:
            stmt = stmt.where(
                Employee.status.in_(
                    [EmployeeStatus.RESIGNED, EmployeeStatus.TERMINATED]
                )
            )
        elif not filters.include_deleted:
            stmt = stmt.where(Employee.status != EmployeeStatus.TERMINATED)
        if (
            not filters.archive_only
            and not filters.include_archived
            and filters.status != EmployeeStatus.RESIGNED
        ):
            stmt = stmt.where(Employee.status != EmployeeStatus.RESIGNED)

        # Advanced filters are always additive to base tenant/deletion constraints.
        stmt, joined_person = apply_employee_filter_expression(
            stmt,
            advanced_filter_expression,
            db=self.db,
            organization_id=self.organization_id,
        )

        if filters.is_active is not None and not filters.status:
            if filters.is_active:
                stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
            else:
                stmt = stmt.where(Employee.status != EmployeeStatus.ACTIVE)

        if filters.status:
            stmt = stmt.where(Employee.status == filters.status)

        if filters.department_id:
            stmt = stmt.where(Employee.department_id == filters.department_id)

        if filters.designation_id:
            stmt = stmt.where(Employee.designation_id == filters.designation_id)

        if filters.reports_to_id:
            report_ids = [
                employee.employee_id
                for employee in OrgResolver(self.db).get_direct_reports(
                    filters.reports_to_id,
                    self.organization_id,
                )
            ]
            if report_ids:
                stmt = stmt.where(Employee.employee_id.in_(report_ids))
            else:
                stmt = stmt.where(Employee.employee_id.is_(None))

        if filters.expense_approver_id:
            stmt = stmt.where(
                Employee.expense_approver_id == filters.expense_approver_id
            )

        if filters.search:
            search_term = f"%{filters.search}%"
            # Search by employee_code or join with Person for name/email
            if not joined_person:
                stmt = stmt.join(Person, Employee.person_id == Person.id)
                joined_person = True
            stmt = stmt.where(_employee_search_predicate(search_term))

        if filters.date_of_joining_from:
            stmt = stmt.where(Employee.date_of_joining >= filters.date_of_joining_from)

        if filters.date_of_joining_to:
            stmt = stmt.where(Employee.date_of_joining <= filters.date_of_joining_to)

        if filters.date_of_leaving_from:
            stmt = stmt.where(Employee.date_of_leaving >= filters.date_of_leaving_from)

        if filters.date_of_leaving_to:
            stmt = stmt.where(Employee.date_of_leaving <= filters.date_of_leaving_to)

        # Default ordering
        stmt = stmt.order_by(Employee.employee_code.asc())

        # Eager load relationships for web views
        if eager_load:
            stmt = stmt.options(
                selectinload(Employee.person),
                selectinload(Employee.department),
                selectinload(Employee.designation),
                selectinload(Employee.default_shift_type),
            )

        return paginate(
            self.db,
            stmt,
            pagination,
            count_column=Employee.employee_id,
        )

    def get_employee_stats(self) -> dict[str, int]:
        """Get employee count statistics by status.

        Returns:
            Dict with total and lifecycle status counts.
        """
        stmt = (
            select(Employee.status, func.count(Employee.employee_id))
            .where(
                Employee.organization_id == self.organization_id,
            )
            .group_by(Employee.status)
        )
        results = self.db.execute(stmt).all()

        status_counts = {status: count for status, count in results}
        terminated = status_counts.get(EmployeeStatus.TERMINATED, 0)
        resigned = status_counts.get(EmployeeStatus.RESIGNED, 0)
        total = sum(status_counts.values())

        return {
            "total": total,
            "current": total - terminated - resigned,
            "active": status_counts.get(EmployeeStatus.ACTIVE, 0),
            "on_leave": status_counts.get(EmployeeStatus.ON_LEAVE, 0),
            "resigned": resigned,
            "terminated": terminated,
            "exit_archive": resigned + terminated,
            "suspended": status_counts.get(EmployeeStatus.SUSPENDED, 0),
            "retired": status_counts.get(EmployeeStatus.RETIRED, 0),
            "inactive": (
                status_counts.get(EmployeeStatus.SUSPENDED, 0)
                + status_counts.get(EmployeeStatus.TERMINATED, 0)
                + status_counts.get(EmployeeStatus.RESIGNED, 0)
                + status_counts.get(EmployeeStatus.RETIRED, 0)
            ),
        }

    def get_employee(
        self,
        employee_id: uuid.UUID,
        include_deleted: bool = False,
        *,
        eager_load: bool = False,
    ) -> Employee:
        """Get an employee by ID.

        Args:
            employee_id: The employee ID.
            include_deleted: Whether to include soft-deleted employees.
            eager_load: If True, eager load person, department, and designation
                relationships to avoid N+1 queries. Use for detail views.

        Returns:
            The Employee object.

        Raises:
            EmployeeNotFoundError: If employee not found.
        """
        from sqlalchemy.orm import joinedload

        stmt = select(Employee).where(
            Employee.employee_id == employee_id,
            Employee.organization_id == self.organization_id,
        )

        if not include_deleted:
            stmt = stmt.where(Employee.status != EmployeeStatus.TERMINATED)

        if eager_load:
            stmt = stmt.options(
                joinedload(Employee.person),
                joinedload(Employee.department),
                joinedload(Employee.designation),
                joinedload(Employee.default_shift_type),
            )

        employee = self.db.scalar(stmt)

        if not employee:
            raise EmployeeNotFoundError(employee_id)

        return employee

    def get_employee_by_code(self, employee_code: str) -> Employee | None:
        """Get an employee by employee code.

        Args:
            employee_code: The employee code.

        Returns:
            The Employee object or None if not found.
        """
        return self.db.scalar(
            select(Employee).where(
                Employee.employee_code == employee_code,
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )

    def get_employee_by_person(self, person_id: uuid.UUID) -> Employee | None:
        """Get an employee by their linked Person ID.

        Args:
            person_id: The Person ID.

        Returns:
            The Employee object or None if not found.
        """
        return self.db.scalar(
            select(Employee).where(
                Employee.person_id == person_id,
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )

    def search_employees(self, query: str, limit: int = 20) -> list[EmployeeSummary]:
        """Search employees for autocomplete.

        Args:
            query: Search query (name, email, or employee code).
            limit: Maximum number of results.

        Returns:
            List of EmployeeSummary objects.
        """
        search_term = f"%{query}%"

        stmt = (
            select(Employee, Person)
            .join(Person, Employee.person_id == Person.id)
            .where(
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
                _employee_search_predicate(search_term),
            )
            .order_by(Person.first_name.asc())
            .limit(limit)
        )
        results = self.db.execute(stmt).all()

        return [
            EmployeeSummary(
                id=emp.employee_id,
                name=person.name,
                email=person.email,
                employee_number=emp.employee_code,
                department=None,  # Would need to join with Department
                designation=None,  # Would need to join with Designation
                status=emp.status,
            )
            for emp, person in results
        ]

    def get_direct_reports(self, manager_id: uuid.UUID) -> list[Employee]:
        """Get all direct reports of a manager through active positions.

        Args:
            manager_id: The manager's employee ID.

        Returns:
            List of Employee objects who report to this manager.
        """
        reports = OrgResolver(self.db).get_direct_reports(
            manager_id,
            self.organization_id,
        )
        return sorted(reports, key=lambda emp: emp.employee_code or "")

    # =========================================================================
    # CRUD
    # =========================================================================

    def create_employee(
        self,
        person_id: uuid.UUID,
        data: EmployeeCreateData,
    ) -> Employee:
        """Create a new employee.

        The employee is linked to an existing Person record. The Person
        contains contact information, while Employee contains HR-specific data.

        Args:
            person_id: The Person ID to link this employee to.
            data: Employee creation data.

        Returns:
            The created Employee (not yet committed).

        Raises:
            EmployeeAlreadyExistsError: If employee code already exists or
                                        person already has an employee record.
            ValidationError: If validation fails.
        """
        person = self.db.scalar(
            select(Person).where(
                Person.id == person_id,
                Person.organization_id == self.organization_id,
            )
        )
        if not person:
            raise ValidationError(
                f"Person {person_id} not found for organization {self.organization_id}"
            )

        # Check if person already has an employee record
        existing = self.get_employee_by_person(person_id)
        if existing:
            raise EmployeeAlreadyExistsError(
                str(person_id),
                f"Person {person_id} already has an employee record",
            )

        # Auto-generate employee code if not provided
        employee_code = data.employee_number
        if not employee_code:
            # Serialize code generation per org/year to avoid duplicates.
            lock_key = (self.organization_id.int ^ datetime.now(UTC).year) % (2**63)
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": lock_key},
            )
            employee_code = self._generate_employee_code()

        # Check for duplicate employee code
        existing = self.get_employee_by_code(employee_code)
        if existing:
            raise EmployeeAlreadyExistsError(
                employee_code,
                f"Employee with code '{employee_code}' already exists",
            )

        # Validate manager doesn't create cycle (not possible for new employee)
        if data.reports_to_id:
            manager = self.db.scalar(
                select(Employee).where(
                    Employee.employee_id == data.reports_to_id,
                    Employee.organization_id == self.organization_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if not manager:
                raise ValidationError(f"Manager with ID {data.reports_to_id} not found")

        if data.expense_approver_id:
            approver = self.db.scalar(
                select(Employee).where(
                    Employee.employee_id == data.expense_approver_id,
                    Employee.organization_id == self.organization_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if not approver:
                raise ValidationError(
                    f"Expense approver with ID {data.expense_approver_id} not found"
                )

        self._validate_org_reference(Department, data.department_id, "Department")
        self._validate_org_reference(Designation, data.designation_id, "Designation")
        if data.employment_type_id is not None:
            EmploymentTypeService(
                db=self.db, organization_id=self.organization_id
            ).require_active(data.employment_type_id)
        self._validate_org_reference(EmployeeGrade, data.grade_id, "Employee grade")
        self._validate_org_reference(CostCenter, data.cost_center_id, "Cost center")
        self._validate_org_reference(Location, data.assigned_location_id, "Location")
        self._validate_org_reference(
            ShiftType, data.default_shift_type_id, "Shift type"
        )

        employee = Employee(
            organization_id=self.organization_id,
            person_id=person_id,
            employee_code=employee_code,
            department_id=data.department_id,
            designation_id=data.designation_id,
            employment_type_id=data.employment_type_id,
            grade_id=data.grade_id,
            expense_approver_id=data.expense_approver_id,
            assigned_location_id=data.assigned_location_id,
            default_shift_type_id=data.default_shift_type_id,
            date_of_joining=data.date_of_joining or date.today(),
            probation_end_date=data.probation_end_date,
            confirmation_date=data.confirmation_date,
            nysc_start_date=data.nysc_start_date,
            nysc_end_date=data.nysc_end_date,
            status=data.status or EmployeeStatus.DRAFT,
            cost_center_id=data.cost_center_id,
            # Personal contact
            personal_email=data.personal_email,
            personal_phone=data.personal_phone,
            # Emergency contact
            emergency_contact_name=data.emergency_contact_name,
            emergency_contact_phone=data.emergency_contact_phone,
            # Bank details
            bank_name=data.bank_name,
            bank_account_number=data.bank_account_number,
            bank_account_name=data.bank_account_name,
            bank_branch_code=data.bank_sort_code,
            ctc=data.ctc,
            salary_mode=data.salary_mode,
            notes=data.notes,
            dotmac_sub_access_enabled=data.dotmac_sub_access_enabled,
            dotmac_sub_roles=data.dotmac_sub_roles,
            created_by_id=self.principal.id if self.principal else None,
        )

        self.db.add(employee)
        self.db.flush()
        if data.position_id:
            self._assign_initial_position(employee, data.position_id)
        elif data.reports_to_id:
            self.set_manager(
                employee.employee_id,
                data.reports_to_id,
                validate_cycle=False,
            )
        else:
            from app.services.people.hr.positions import PositionService

            PositionService(
                self.db, self.organization_id
            ).provision_positions_for_employees([employee.employee_id])
        self.ensure_local_user_credentials_for_employee(employee.employee_id)
        self._ensure_default_employee_role(person.id)

        fire_audit_event(
            db=self.db,
            organization_id=employee.organization_id,
            table_schema="hr",
            table_name="employee",
            record_id=str(employee.employee_id),
            action=AuditAction.INSERT,
            new_values={
                "employee_number": employee.employee_code,
                "person_id": str(employee.person_id),
            },
        )

        self._refresh_staff_access_projection(employee)
        if employee.dotmac_sub_access_enabled:
            self._enqueue_staff_sync(employee)

        return employee

    def _assign_initial_position(
        self,
        employee: Employee,
        position_id: uuid.UUID,
    ) -> None:
        """Assign a new employee to an existing position without auto-provisioning."""
        position = self.db.scalar(
            select(Position).where(
                Position.position_id == position_id,
                Position.organization_id == self.organization_id,
                Position.is_active.is_(True),
            )
        )
        if not position:
            raise ValidationError(f"Position with ID {position_id} not found")

        existing_position_assignment = self.db.scalar(
            select(PositionAssignment.position_assignment_id).where(
                PositionAssignment.organization_id == self.organization_id,
                PositionAssignment.position_id == position_id,
                PositionAssignment.assignment_type == PositionAssignmentType.PRIMARY,
                PositionAssignment.end_date.is_(None),
            )
        )
        if existing_position_assignment:
            raise ValidationError("Position already has an active primary assignment")

        existing_employee_assignment = self.db.scalar(
            select(PositionAssignment.position_assignment_id).where(
                PositionAssignment.organization_id == self.organization_id,
                PositionAssignment.employee_id == employee.employee_id,
                PositionAssignment.assignment_type == PositionAssignmentType.PRIMARY,
                PositionAssignment.end_date.is_(None),
            )
        )
        if existing_employee_assignment:
            raise ValidationError("Employee already has an active primary position")

        self.db.add(
            PositionAssignment(
                organization_id=self.organization_id,
                employee_id=employee.employee_id,
                position_id=position_id,
                assignment_type=PositionAssignmentType.PRIMARY,
                start_date=employee.date_of_joining or date.today(),
            )
        )
        position.is_vacant = False
        self.db.flush()

    def ensure_local_user_credentials_for_employee(
        self,
        employee_id: uuid.UUID,
    ) -> UserCredential:
        """Ensure the employee has a usable local credential.

        Newly created employees should always have a login-ready local account.
        If a local credential already exists but is incomplete, normalize it so
        the username matches the person's email and a temporary password exists.
        """
        employee = self.get_employee(employee_id)
        person = self.db.get(Person, employee.person_id)
        if not person or person.organization_id != self.organization_id:
            raise ValidationError("Employee is not linked to a valid user")

        normalized_email = (person.email or "").strip().lower() or None
        if not normalized_email:
            raise ValidationError("Employee user account requires a valid email")

        credential = self.db.scalar(
            select(UserCredential).where(
                UserCredential.person_id == person.id,
                UserCredential.provider == AuthProvider.local,
            )
        )
        if credential:
            if not credential.username:
                credential.username = normalized_email
            if not credential.password_hash:
                credential.password_hash = hash_password(DEFAULT_NEW_LOCAL_PASSWORD)
                credential.password_updated_at = datetime.now(UTC)
            credential.must_change_password = True
            self.db.flush()
            self.db.refresh(credential)
            return credential

        return self.create_user_credentials_for_employee(
            employee_id,
            username=normalized_email,
            password=None,
            provider=AuthProvider.local,
            must_change_password=True,
        )

    def send_employee_access_invite(
        self,
        employee_id: uuid.UUID,
        *,
        app_url: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> EmployeeInviteResult:
        """Send a password-setup invite using the standard reset-password flow."""
        employee = self.get_employee(employee_id)
        person = self.db.get(Person, employee.person_id)
        if not person or person.organization_id != self.organization_id:
            raise ValidationError("Employee is not linked to a valid user")

        email = (person.email or "").strip().lower()
        if not email:
            raise ValidationError("Employee user account requires a valid email")
        personal_email = (
            (getattr(employee, "personal_email", None) or "").strip().lower()
        )
        recipients = [email]
        if personal_email and personal_email not in recipients:
            recipients.append(personal_email)
        to_email = email
        recipient_kind = "work"

        invite = request_password_reset(self.db, email)
        if not invite:
            raise ValidationError("Employee user credentials are not ready for invite")

        if attachments is None:
            try:
                default_attachment = load_default_invite_attachment(
                    self.db,
                    self.organization_id,
                )
            except Exception as exc:
                logger.warning(
                    "Default employee invite attachment could not be loaded for org %s: %s",
                    self.organization_id,
                    exc,
                )
                default_attachment = None
            attachments = [default_attachment] if default_attachment else None

        email_template = get_employee_invite_email_template(
            self.db,
            self.organization_id,
        )
        sent = False
        for recipient in recipients:
            sent = (
                send_password_reset_email(
                    db=self.db,
                    to_email=recipient,
                    reset_token=invite["token"],
                    person_name=invite["person_name"],
                    app_url=app_url,
                    organization_id=invite.get("organization_id"),
                    next_url=EMPLOYEE_INVITE_NEXT_URL,
                    attachments=attachments,
                    email_template=email_template,
                )
                or sent
            )
        return EmployeeInviteResult(
            sent=sent,
            recipient_email=to_email,
            recipient_kind=recipient_kind,
            attempted_recipients=tuple(recipients),
        )

    def _ensure_default_employee_role(self, person_id: uuid.UUID) -> None:
        """Ensure newly created employees have the default employee role."""
        employee_role = self.db.scalar(
            select(Role).where(
                Role.name == "employee",
                Role.is_active.is_(True),
            )
        )
        if not employee_role:
            raise ValidationError("Default role 'employee' is missing or inactive")

        existing_assignment = self.db.scalar(
            select(PersonRole).where(
                PersonRole.person_id == person_id,
                PersonRole.role_id == employee_role.id,
            )
        )
        if existing_assignment:
            return

        self.db.add(PersonRole(person_id=person_id, role_id=employee_role.id))
        self.db.flush()

    def update_employee(
        self, employee_id: uuid.UUID, data: EmployeeUpdateData
    ) -> Employee:
        """Update an existing employee.

        Args:
            employee_id: The employee ID.
            data: Fields to update.

        Returns:
            The updated Employee (not yet committed).

        Raises:
            EmployeeNotFoundError: If employee not found.
            EmployeeAlreadyExistsError: If employee code conflicts.
            InvalidManagerError: If manager assignment creates cycle.
        """
        employee = self.get_employee(employee_id)
        prior_staff_access = (
            employee.status,
            employee.dotmac_sub_access_enabled,
            tuple(employee.dotmac_sub_roles or []),
            employee.department_id,
        )
        prior_department_id = employee.department_id

        provided_fields: set[str] = set(getattr(data, "provided_fields", set()))
        use_provided_fields = bool(provided_fields)

        # Validate and update employee code
        if (
            data.employee_number is not None
            and data.employee_number != employee.employee_code
        ):
            existing = self.get_employee_by_code(data.employee_number)
            if existing and existing.employee_id != employee_id:
                raise EmployeeAlreadyExistsError(
                    data.employee_number,
                    f"Employee with code '{data.employee_number}' already exists",
                )
            employee.employee_code = data.employee_number

        if data.reports_to_id is not None:
            self.set_manager(employee_id, data.reports_to_id)
        elif use_provided_fields and "reports_to_id" in provided_fields:
            self.set_manager(employee_id, None)

        if (
            data.expense_approver_id is not None
            and data.expense_approver_id != employee.expense_approver_id
        ):
            if data.expense_approver_id == employee_id:
                raise ValidationError("Expense approver cannot be the employee")
            if data.expense_approver_id:
                approver = self.db.scalar(
                    select(Employee).where(
                        Employee.employee_id == data.expense_approver_id,
                        Employee.organization_id == self.organization_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                    )
                )
                if not approver:
                    raise ValidationError(
                        f"Expense approver with ID {data.expense_approver_id} not found"
                    )
            employee.expense_approver_id = data.expense_approver_id
        elif use_provided_fields and "expense_approver_id" in provided_fields:
            employee.expense_approver_id = None

        # Update department
        if data.department_id is not None:
            self._validate_org_reference(Department, data.department_id, "Department")
            department_changed = employee.department_id != data.department_id
            employee.department_id = data.department_id
            if department_changed:
                self._sync_active_primary_position_department(
                    employee.employee_id,
                    data.department_id,
                )
        elif use_provided_fields and "department_id" in provided_fields:
            department_changed = employee.department_id is not None
            employee.department_id = None
            if department_changed:
                self._sync_active_primary_position_department(
                    employee.employee_id, None
                )

        if employee.department_id != prior_department_id:
            self._sync_scheduling_department(employee)

        # Update designation
        if data.designation_id is not None:
            self._validate_org_reference(
                Designation, data.designation_id, "Designation"
            )
            employee.designation_id = data.designation_id
        elif use_provided_fields and "designation_id" in provided_fields:
            employee.designation_id = None

        if data.employment_type_id is not None:
            if data.employment_type_id != employee.employment_type_id:
                EmploymentTypeService(
                    db=self.db, organization_id=self.organization_id
                ).require_active(data.employment_type_id)
            employee.employment_type_id = data.employment_type_id
        elif use_provided_fields and "employment_type_id" in provided_fields:
            employee.employment_type_id = None

        if data.grade_id is not None:
            self._validate_org_reference(EmployeeGrade, data.grade_id, "Employee grade")
            employee.grade_id = data.grade_id
        elif use_provided_fields and "grade_id" in provided_fields:
            employee.grade_id = None

        if data.cost_center_id is not None:
            self._validate_org_reference(CostCenter, data.cost_center_id, "Cost center")
            employee.cost_center_id = data.cost_center_id
        elif use_provided_fields and "cost_center_id" in provided_fields:
            employee.cost_center_id = None

        if data.assigned_location_id is not None:
            self._validate_org_reference(
                Location, data.assigned_location_id, "Location"
            )
            employee.assigned_location_id = data.assigned_location_id
        elif use_provided_fields and "assigned_location_id" in provided_fields:
            employee.assigned_location_id = None

        if data.default_shift_type_id is not None:
            self._validate_org_reference(
                ShiftType, data.default_shift_type_id, "Shift type"
            )
            employee.default_shift_type_id = data.default_shift_type_id
        elif use_provided_fields and "default_shift_type_id" in provided_fields:
            employee.default_shift_type_id = None

        # Update simple fields
        if data.date_of_joining is not None:
            employee.date_of_joining = data.date_of_joining

        if data.date_of_leaving is not None:
            employee.date_of_leaving = data.date_of_leaving
        elif use_provided_fields and "date_of_leaving" in provided_fields:
            employee.date_of_leaving = None

        if data.final_payroll_cutoff_date is not None:
            employee.final_payroll_cutoff_date = data.final_payroll_cutoff_date
        elif use_provided_fields and "final_payroll_cutoff_date" in provided_fields:
            employee.final_payroll_cutoff_date = None

        if data.status is not None:
            employee.status = data.status

        if data.eligible_for_final_payroll is not None:
            employee.eligible_for_final_payroll = data.eligible_for_final_payroll
            if not data.eligible_for_final_payroll:
                employee.final_payroll_cutoff_date = None
                employee.final_payroll_processed_at = None

        if data.probation_end_date is not None:
            employee.probation_end_date = data.probation_end_date
        elif use_provided_fields and "probation_end_date" in provided_fields:
            employee.probation_end_date = None

        if data.confirmation_date is not None:
            employee.confirmation_date = data.confirmation_date
        elif use_provided_fields and "confirmation_date" in provided_fields:
            employee.confirmation_date = None

        if data.nysc_start_date is not None:
            employee.nysc_start_date = data.nysc_start_date
        elif use_provided_fields and "nysc_start_date" in provided_fields:
            employee.nysc_start_date = None

        if data.nysc_end_date is not None:
            employee.nysc_end_date = data.nysc_end_date
        elif use_provided_fields and "nysc_end_date" in provided_fields:
            employee.nysc_end_date = None

        # Bank details
        if data.bank_name is not None:
            employee.bank_name = data.bank_name
        elif use_provided_fields and "bank_name" in provided_fields:
            employee.bank_name = None
        if data.bank_account_number is not None:
            employee.bank_account_number = data.bank_account_number
        elif use_provided_fields and "bank_account_number" in provided_fields:
            employee.bank_account_number = None
        if data.bank_account_name is not None:
            employee.bank_account_name = data.bank_account_name
        elif use_provided_fields and "bank_account_name" in provided_fields:
            employee.bank_account_name = None
        if data.bank_sort_code is not None:
            employee.bank_branch_code = data.bank_sort_code
        elif use_provided_fields and "bank_sort_code" in provided_fields:
            employee.bank_branch_code = None
        if data.ctc is not None:
            employee.ctc = data.ctc
        elif use_provided_fields and "ctc" in provided_fields:
            employee.ctc = None
        if data.salary_mode is not None:
            employee.salary_mode = data.salary_mode
        elif use_provided_fields and "salary_mode" in provided_fields:
            employee.salary_mode = None

        # Personal contact
        if data.personal_email is not None:
            employee.personal_email = data.personal_email
        elif use_provided_fields and "personal_email" in provided_fields:
            employee.personal_email = None
        if data.personal_phone is not None:
            employee.personal_phone = data.personal_phone
        elif use_provided_fields and "personal_phone" in provided_fields:
            employee.personal_phone = None

        # Emergency contact
        if data.emergency_contact_name is not None:
            employee.emergency_contact_name = data.emergency_contact_name
        elif use_provided_fields and "emergency_contact_name" in provided_fields:
            employee.emergency_contact_name = None
        if data.emergency_contact_phone is not None:
            employee.emergency_contact_phone = data.emergency_contact_phone
        elif use_provided_fields and "emergency_contact_phone" in provided_fields:
            employee.emergency_contact_phone = None

        if data.notes is not None:
            employee.notes = data.notes
        elif use_provided_fields and "notes" in provided_fields:
            employee.notes = None

        if data.dotmac_sub_access_enabled is not None:
            employee.dotmac_sub_access_enabled = data.dotmac_sub_access_enabled
        if data.dotmac_sub_roles is not None:
            employee.dotmac_sub_roles = list(dict.fromkeys(data.dotmac_sub_roles))

        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        employee.version += 1

        fire_audit_event(
            db=self.db,
            organization_id=employee.organization_id,
            table_schema="hr",
            table_name="employee",
            record_id=str(employee.employee_id),
            action=AuditAction.UPDATE,
            new_values={"updated_fields": "employee_data"},
        )

        current_staff_access = (
            employee.status,
            employee.dotmac_sub_access_enabled,
            tuple(employee.dotmac_sub_roles or []),
            employee.department_id,
        )
        self._refresh_staff_access_projection(employee)
        if current_staff_access != prior_staff_access:
            self._enqueue_staff_sync(employee)

        return employee

    def update_final_payroll_settings(
        self,
        employee_id: uuid.UUID,
        *,
        eligible_for_final_payroll: bool,
        final_payroll_cutoff_date: date | None = None,
    ) -> Employee:
        """Update final payroll eligibility for an exited employee."""
        employee = self.get_employee(employee_id, include_deleted=True)
        if employee.status not in {
            EmployeeStatus.RESIGNED,
            EmployeeStatus.TERMINATED,
            EmployeeStatus.RETIRED,
        }:
            raise EmployeeStatusError(
                employee.status.value,
                "Final payroll can only be managed for exited employees",
            )

        employee.eligible_for_final_payroll = eligible_for_final_payroll
        if eligible_for_final_payroll:
            employee.final_payroll_cutoff_date = (
                final_payroll_cutoff_date or employee.date_of_leaving
            )
            employee.final_payroll_processed_at = None
        else:
            employee.final_payroll_cutoff_date = None

        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        employee.version += 1
        self.db.flush()
        return employee

    def _sync_scheduling_department(self, employee: Employee) -> None:
        """Mirror HR department changes into active scheduling assignments."""
        from app.services.people.scheduling import SchedulingService

        result = SchedulingService(self.db).sync_employee_department(
            org_id=employee.organization_id,
            employee_id=employee.employee_id,
            new_department_id=employee.department_id,
        )
        if result["assignments_updated"] or result["assignments_ended"]:
            logger.info(
                "Employee %s department change synced to scheduling assignments: %s",
                employee.employee_id,
                result,
            )

    def _sync_active_primary_position_department(
        self,
        employee_id: uuid.UUID,
        department_id: uuid.UUID | None,
    ) -> None:
        """Keep an employee's active primary position aligned with their department."""
        position = self.db.scalar(
            select(Position)
            .join(
                PositionAssignment,
                PositionAssignment.position_id == Position.position_id,
            )
            .where(
                Position.organization_id == self.organization_id,
                PositionAssignment.organization_id == self.organization_id,
                PositionAssignment.employee_id == employee_id,
                PositionAssignment.assignment_type == PositionAssignmentType.PRIMARY,
                PositionAssignment.end_date.is_(None),
            )
        )
        if position and position.department_id != department_id:
            position.department_id = department_id
            logger.info(
                "Synced primary position department for employee %s",
                employee_id,
            )

    # =========================================================================
    # User Linking / Credentials
    # =========================================================================

    def link_employee_to_person(
        self,
        employee_id: uuid.UUID,
        person_id: uuid.UUID,
    ) -> Employee:
        """Link an employee record to an existing Person (user)."""
        employee = self.get_employee(employee_id)

        person = self.db.get(Person, person_id)
        if not person or person.organization_id != self.organization_id:
            raise ValidationError(f"Person {person_id} not found for organization")

        existing = self.get_employee_by_person(person_id)
        if existing and existing.employee_id != employee.employee_id:
            raise ValidationError("Person is already linked to another employee")

        employee.person_id = person_id
        self.db.flush()
        return employee

    def create_user_credentials_for_employee(
        self,
        employee_id: uuid.UUID,
        *,
        username: str | None,
        password: str | None,
        provider: AuthProvider = AuthProvider.local,
        must_change_password: bool = True,
    ) -> UserCredential:
        """Create user credentials for an employee's linked Person."""
        employee = self.get_employee(employee_id)
        person = self.db.get(Person, employee.person_id)
        if not person or person.organization_id != self.organization_id:
            raise ValidationError("Employee is not linked to a valid user")

        resolved_username = (username or "").strip() or None
        resolved_password = password

        if provider == AuthProvider.local:
            if not resolved_username:
                resolved_username = (person.email or "").strip().lower() or None
            if not resolved_password:
                resolved_password = DEFAULT_NEW_LOCAL_PASSWORD
            must_change_password = True
            if not resolved_username:
                raise ValidationError(
                    "Username and password are required for local auth"
                )

        existing = self.db.scalar(
            select(UserCredential).where(
                UserCredential.person_id == person.id,
                UserCredential.provider == provider,
            )
        )
        if existing:
            raise ValidationError("User credentials already exist for this employee")

        if resolved_username:
            username_in_use = self.db.scalar(
                select(UserCredential).where(
                    UserCredential.provider == provider,
                    UserCredential.username == resolved_username,
                )
            )
            if username_in_use:
                raise ValidationError("Username is already in use")

        password_hash = hash_password(resolved_password) if resolved_password else None
        credential = UserCredential(
            person_id=person.id,
            provider=provider,
            username=resolved_username,
            password_hash=password_hash,
            must_change_password=must_change_password,
            password_updated_at=datetime.now(UTC) if password_hash else None,
        )
        self.db.add(credential)
        self.db.flush()
        self.db.refresh(credential)
        return credential

    def delete_employee(self, employee_id: uuid.UUID) -> None:
        """Soft delete an employee.

        Args:
            employee_id: The employee ID.

        Raises:
            EmployeeNotFoundError: If employee not found.
        """
        employee = self.get_employee(employee_id)

        employee.status = EmployeeStatus.TERMINATED
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        self._refresh_staff_access_projection(employee)

    # =========================================================================
    # Status Management
    # =========================================================================

    def _enqueue_staff_sync(self, employee: Employee) -> None:
        """Queue the dotmac_sub staff-account push for a lifecycle change.

        Enqueued with a short countdown so the caller's transaction commits
        before the worker reads the row; the nightly reconcile sweep is the
        safety net for anything missed. Never lets queueing failures break
        the HR write path.
        """
        from app.config import settings as app_settings

        if not getattr(app_settings, "dotmac_sub_staff_sync_enabled", False):
            return
        try:
            from app.tasks.staff_sync import sync_employee_staff_account

            sync_employee_staff_account.apply_async(
                args=[str(employee.employee_id), str(employee.organization_id)],
                countdown=10,
            )
        except Exception:  # noqa: BLE001 — broker down must not block HR writes
            logger.warning(
                "Could not enqueue staff sync for employee %s",
                employee.employee_id,
                exc_info=True,
            )

    def _refresh_staff_access_projection(self, employee: Employee) -> None:
        """Refresh ERP-owned Selfcare-facing staff access projections."""
        required_attrs = ("organization_id", "employee_id", "person_id", "status")
        if any(getattr(employee, attr, None) is None for attr in required_attrs):
            return
        required_db_attrs = (
            "add",
            "connection",
            "flush",
            "get_bind",
            "scalar",
            "scalars",
        )
        if not all(hasattr(self.db, attr) for attr in required_db_attrs):
            return
        bind = self.db.get_bind()
        if getattr(bind.dialect, "name", "") == "sqlite":
            inspector = inspect_db(self.db.connection())
            has_projection_tables = inspector.has_table(
                "staff_account_status_projection"
            ) and inspector.has_table("staff_leave_access_restriction")
            if not has_projection_tables:
                return

        from app.services.people.hr.staff_access_projection import (
            StaffAccessProjectionService,
        )

        StaffAccessProjectionService(self.db).refresh_employee_projections(employee)

    def activate_employee(self, employee_id: uuid.UUID) -> Employee:
        """Activate an employee.

        Args:
            employee_id: The employee ID.

        Returns:
            The updated Employee.

        Raises:
            EmployeeNotFoundError: If employee not found.
        """
        employee = self.get_employee(employee_id)
        employee.status = EmployeeStatus.ACTIVE
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        self._refresh_staff_access_projection(employee)
        self._enqueue_staff_sync(employee)
        return employee

    def suspend_employee(
        self, employee_id: uuid.UUID, reason: str | None = None
    ) -> Employee:
        """Suspend an employee.

        Args:
            employee_id: The employee ID.
            reason: Optional reason for suspension.

        Returns:
            The updated Employee.

        Raises:
            EmployeeNotFoundError: If employee not found.
        """
        employee = self.get_employee(employee_id)
        employee.status = EmployeeStatus.SUSPENDED
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        # Note: reason could be stored in notes field or separate audit log
        self._refresh_staff_access_projection(employee)
        self._enqueue_staff_sync(employee)
        return employee

    def terminate_employee(
        self, employee_id: uuid.UUID, data: TerminationData
    ) -> Employee:
        """Terminate an employee.

        Args:
            employee_id: The employee ID.
            data: Termination data.

        Returns:
            The updated Employee.

        Raises:
            EmployeeNotFoundError: If employee not found.
            EmployeeStatusError: If employee is already terminated.
        """
        employee = self.get_employee(employee_id)

        if employee.status == EmployeeStatus.TERMINATED:
            raise EmployeeStatusError(
                employee.status.value,
                "Employee is already terminated",
            )

        old_status = employee.status.value if employee.status else None

        employee.status = EmployeeStatus.TERMINATED
        employee.date_of_leaving = data.date_of_leaving
        employee.eligible_for_final_payroll = data.eligible_for_final_payroll
        employee.final_payroll_cutoff_date = (
            data.final_payroll_cutoff_date or data.date_of_leaving
            if data.eligible_for_final_payroll
            else None
        )
        employee.final_payroll_processed_at = None
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None

        fire_audit_event(
            db=self.db,
            organization_id=employee.organization_id,
            table_schema="hr",
            table_name="employee",
            record_id=str(employee.employee_id),
            action=AuditAction.UPDATE,
            old_values={"status": old_status},
            new_values={"status": "TERMINATED"},
            reason=data.reason if hasattr(data, "reason") else None,
        )

        self._refresh_staff_access_projection(employee)
        self._enqueue_staff_sync(employee)
        return employee

    def resign_employee(
        self,
        employee_id: uuid.UUID,
        date_of_leaving: date,
        *,
        eligible_for_final_payroll: bool = False,
        final_payroll_cutoff_date: date | None = None,
    ) -> Employee:
        """Record employee resignation.

        Args:
            employee_id: The employee ID.
            date_of_leaving: The last working day.

        Returns:
            The updated Employee.

        Raises:
            EmployeeNotFoundError: If employee not found.
        """
        employee = self.get_employee(employee_id)
        employee.status = EmployeeStatus.RESIGNED
        employee.date_of_leaving = date_of_leaving
        employee.eligible_for_final_payroll = eligible_for_final_payroll
        employee.final_payroll_cutoff_date = (
            final_payroll_cutoff_date or date_of_leaving
            if eligible_for_final_payroll
            else None
        )
        employee.final_payroll_processed_at = None
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        self._refresh_staff_access_projection(employee)
        self._enqueue_staff_sync(employee)
        return employee

    def rehire_employee(
        self,
        employee_id: uuid.UUID,
        date_of_rejoining: date,
        notes: str | None = None,
    ) -> Employee:
        """Rehire a previously separated employee.

        Creates a completed onboarding record to preserve rehire history.
        """
        employee = self.get_employee(employee_id)

        if employee.status not in {
            EmployeeStatus.RESIGNED,
            EmployeeStatus.TERMINATED,
            EmployeeStatus.RETIRED,
        }:
            raise EmployeeStatusError(
                employee.status.value,
                "Only resigned, terminated, or retired employees can be rehired",
            )

        if employee.date_of_leaving and date_of_rejoining < employee.date_of_leaving:
            raise ValidationError("Rehire date cannot be before date of leaving")

        old_status = employee.status.value if employee.status else None

        employee.status = EmployeeStatus.ACTIVE
        employee.date_of_joining = date_of_rejoining
        employee.date_of_leaving = None
        employee.eligible_for_final_payroll = False
        employee.final_payroll_cutoff_date = None
        employee.final_payroll_processed_at = None
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None

        fire_audit_event(
            db=self.db,
            organization_id=employee.organization_id,
            table_schema="hr",
            table_name="employee",
            record_id=str(employee.employee_id),
            action=AuditAction.UPDATE,
            old_values={"status": old_status},
            new_values={
                "status": EmployeeStatus.ACTIVE.value,
                "date_of_joining": str(date_of_rejoining),
            },
            reason=notes,
        )

        try:
            from app.models.people.hr.lifecycle import BoardingStatus
            from app.services.people.hr.lifecycle import LifecycleService

            lifecycle = LifecycleService(self.db)
            onboarding = lifecycle.create_onboarding(
                employee.organization_id,
                employee_id=employee.employee_id,
                date_of_joining=date_of_rejoining,
                department_id=employee.department_id,
                designation_id=employee.designation_id,
                template_name="REHIRE",
                notes=notes or f"Rehired from {old_status or 'UNKNOWN'} status",
            )
            onboarding.status = BoardingStatus.COMPLETED
        except Exception:
            logger.exception(
                "Failed to create rehire lifecycle record for employee %s",
                employee.employee_id,
            )

        self._refresh_staff_access_projection(employee)
        return employee

    def set_on_leave(self, employee_id: uuid.UUID) -> Employee:
        """Set employee status to on leave.

        Args:
            employee_id: The employee ID.

        Returns:
            The updated Employee.

        Raises:
            EmployeeNotFoundError: If employee not found.
            EmployeeStatusError: If employee is terminated.
        """
        employee = self.get_employee(employee_id)

        if employee.status == EmployeeStatus.TERMINATED:
            raise EmployeeStatusError(
                employee.status.value,
                "Cannot set terminated employee on leave",
            )

        employee.status = EmployeeStatus.ON_LEAVE
        employee.updated_at = datetime.now(UTC)
        employee.updated_by_id = self.principal.id if self.principal else None
        self._refresh_staff_access_projection(employee)

        return employee

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    def bulk_update(self, data: BulkUpdateData) -> BulkResult:
        """Bulk update multiple employees.

        Args:
            data: Bulk update data containing IDs and fields to update.

        Returns:
            BulkResult with count of updated employees and any failures.
        """
        if not data.ids:
            return BulkResult()

        result = BulkResult()
        now = datetime.now(UTC)

        # Build update dict from non-None fields
        updates: dict = {}
        if data.department_id is not None:
            self._validate_org_reference(Department, data.department_id, "Department")
            updates["department_id"] = data.department_id
        if data.designation_id is not None:
            self._validate_org_reference(
                Designation, data.designation_id, "Designation"
            )
            updates["designation_id"] = data.designation_id
        if data.status is not None:
            updates["status"] = data.status
        if data.reports_to_id is not None:
            manager = self.db.scalar(
                select(Employee).where(
                    Employee.employee_id == data.reports_to_id,
                    Employee.organization_id == self.organization_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if not manager:
                raise ValidationError(f"Manager with ID {data.reports_to_id} not found")

            for employee_id in data.ids:
                if not self._validate_manager(employee_id, data.reports_to_id):
                    raise InvalidManagerError()
            updates["reports_to_id"] = data.reports_to_id

        if not updates:
            return result

        # Add audit fields
        updates["updated_at"] = now
        updates["version"] = Employee.version + 1
        if self.principal:
            updates["updated_by_id"] = self.principal.id

        # Perform bulk update
        stmt = (
            update(Employee)
            .where(
                Employee.employee_id.in_(data.ids),
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
            .values(**updates)
        )
        result_proxy = cast(CursorResult[Any], self.db.execute(stmt))
        result.updated_count = result_proxy.rowcount or 0
        if "reports_to_id" in updates:
            for updated_employee_id in data.ids:
                self._set_manager_position(
                    updated_employee_id,
                    data.reports_to_id,
                )
        if "status" in updates:
            employees = list(
                self.db.scalars(
                    select(Employee).where(
                        Employee.organization_id == self.organization_id,
                        Employee.employee_id.in_(data.ids),
                    )
                ).all()
            )
            for employee in employees:
                self._refresh_staff_access_projection(employee)

        return result

    def bulk_delete(self, ids: list[uuid.UUID]) -> BulkResult:
        """Bulk soft-delete multiple employees.

        Args:
            ids: List of employee IDs to delete.

        Returns:
            BulkResult with count of deleted employees.
        """
        if not ids:
            return BulkResult()

        result = BulkResult()
        now = datetime.now(UTC)
        user_id = self.principal.id if self.principal else None

        stmt = (
            update(Employee)
            .where(
                Employee.employee_id.in_(ids),
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
            .values(
                status=EmployeeStatus.TERMINATED,
                updated_at=now,
                updated_by_id=user_id,
            )
        )
        result_proxy = cast(CursorResult[Any], self.db.execute(stmt))
        result.deleted_count = result_proxy.rowcount or 0
        if result.deleted_count:
            employees = list(
                self.db.scalars(
                    select(Employee).where(
                        Employee.organization_id == self.organization_id,
                        Employee.employee_id.in_(ids),
                    )
                ).all()
            )
            for employee in employees:
                self._refresh_staff_access_projection(employee)

        return result
