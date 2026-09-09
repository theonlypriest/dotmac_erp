"""Organization service for departments, designations, and related entities.

This service encapsulates organization structure business logic:
- Department CRUD and hierarchy
- Designation CRUD
- Employee Grade CRUD

Routes should call this service and control the transaction boundary.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.finance.core_org.cost_center import CostCenter
from app.models.finance.core_org.location import Location, LocationType
from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeGrade,
    EmployeeStatus,
)
from app.services.common import PaginatedResult, PaginationParams, paginate

from .employee_types import EmployeeFilters
from .employees import EmployeeService
from .errors import (
    CircularDepartmentError,
    DepartmentNotFoundError,
    DesignationNotFoundError,
    EmployeeGradeNotFoundError,
    LocationNotFoundError,
    ValidationError,
)
from .organization_types import (
    DepartmentCreateData,
    DepartmentFilters,
    DepartmentHeadcount,
    DepartmentNode,
    DepartmentUpdateData,
    DesignationCreateData,
    DesignationFilters,
    DesignationHeadcount,
    DesignationUpdateData,
    EmployeeGradeCreateData,
    EmployeeGradeFilters,
    EmployeeGradeUpdateData,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.auth import Principal

__all__ = ["OrganizationService"]


class OrganizationService:
    """Service for organization structure business logic.

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
    # Hierarchy Validation Helpers
    # =========================================================================

    def _validate_department_parent(
        self,
        department_id: uuid.UUID,
        parent_id: uuid.UUID,
        visited: set | None = None,
    ) -> bool:
        """Check if setting parent_id would create a circular reference.

        Traverses the hierarchy from parent_id upward to ensure department_id
        is not an ancestor of parent_id, which would create a cycle.

        Args:
            department_id: The department being modified.
            parent_id: The proposed parent department ID.
            visited: Set of already visited department IDs (for cycle detection).

        Returns:
            True if the parent assignment is valid, False if it creates a cycle.
        """
        if visited is None:
            visited = set()

        # Can't be parent of self
        if department_id == parent_id:
            return False

        # Load the proposed parent
        parent = self.db.scalar(
            select(Department).where(
                Department.department_id == parent_id,
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )
        if not parent:
            return True  # Parent doesn't exist, will fail separately

        visited.add(parent_id)

        # If parent has no grandparent, we're at root - no cycle
        if parent.parent_department_id is None:
            return True

        # If parent's parent is the department we're modifying, that's a cycle
        if parent.parent_department_id == department_id:
            return False

        # Avoid infinite loops in case of existing bad data
        if parent.parent_department_id in visited:
            return False

        # Recurse up the tree
        return self._validate_department_parent(
            department_id, parent.parent_department_id, visited
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
    # Department Methods
    # =========================================================================

    def list_employees(
        self,
        filters: EmployeeFilters | None = None,
        pagination: PaginationParams | None = None,
        *,
        eager_load: bool = False,
    ) -> PaginatedResult[Employee]:
        """List employees via EmployeeService for organization-scoped lookups."""
        employee_service = EmployeeService(self.db, self.organization_id)
        if filters is None:
            filters = EmployeeFilters()
        if pagination is None:
            pagination = PaginationParams()
        return employee_service.list_employees(
            filters,
            pagination,
            eager_load=eager_load,
        )

    def list_departments(
        self,
        filters: DepartmentFilters | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[Department]:
        """List departments with filters and pagination.

        Args:
            filters: Optional filter criteria.
            pagination: Pagination parameters (offset, limit).

        Returns:
            PaginatedResult containing departments and total count.
        """
        if filters is None:
            filters = DepartmentFilters()
        if pagination is None:
            pagination = PaginationParams()

        stmt = (
            select(Department)
            .options(joinedload(Department.head))
            .where(
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )

        if filters.is_active is not None:
            stmt = stmt.where(Department.is_active == filters.is_active)

        if filters.parent_department_id is not None:
            stmt = stmt.where(
                Department.parent_department_id == filters.parent_department_id
            )

        if filters.cost_center_id is not None:
            stmt = stmt.where(Department.cost_center_id == filters.cost_center_id)

        if filters.search:
            search_term = f"%{filters.search}%"
            stmt = stmt.where(
                or_(
                    Department.department_name.ilike(search_term),
                    Department.department_code.ilike(search_term),
                )
            )

        # Order by name
        stmt = stmt.order_by(Department.department_name.asc())

        return paginate(self.db, stmt, pagination)

    def get_department(self, department_id: uuid.UUID) -> Department:
        """Get a department by ID.

        Args:
            department_id: The department ID.

        Returns:
            The Department object.

        Raises:
            DepartmentNotFoundError: If department not found.
        """
        stmt = (
            select(Department)
            .options(joinedload(Department.head))
            .where(
                Department.department_id == department_id,
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )
        department = self.db.scalar(stmt)

        if not department:
            raise DepartmentNotFoundError(department_id)

        return department

    def get_department_by_code(self, code: str) -> Department | None:
        """Get a department by code.

        Args:
            code: The department code.

        Returns:
            The Department object or None if not found.
        """
        return self.db.scalar(
            select(Department).where(
                Department.department_code == code,
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )

    def create_department(self, data: DepartmentCreateData) -> Department:
        """Create a new department.

        Args:
            data: Department creation data.

        Returns:
            The created Department (not yet committed).

        Raises:
            ValidationError: If department code already exists.
        """
        # Check for duplicate code
        existing = self.get_department_by_code(data.department_code)
        if existing:
            raise ValidationError(
                f"Department with code '{data.department_code}' already exists"
            )

        self._validate_org_reference(
            Department, data.parent_department_id, "Parent department"
        )
        self._validate_org_reference(CostCenter, data.cost_center_id, "Cost center")
        self._validate_org_reference(Employee, data.head_id, "Department head")

        department = Department(
            organization_id=self.organization_id,
            department_code=data.department_code,
            department_name=data.department_name,
            description=data.description,
            parent_department_id=data.parent_department_id,
            cost_center_id=data.cost_center_id,
            head_id=data.head_id,
            is_active=data.is_active,
            created_by_id=self.principal.id if self.principal else None,
        )

        self.db.add(department)
        self.db.flush()

        return department

    def update_department(
        self, department_id: uuid.UUID, data: DepartmentUpdateData
    ) -> Department:
        """Update an existing department.

        Args:
            department_id: The department ID.
            data: Fields to update.

        Returns:
            The updated Department (not yet committed).

        Raises:
            DepartmentNotFoundError: If department not found.
            ValidationError: If department code already exists.
            CircularDepartmentError: If update would create a cycle.
        """
        department = self.get_department(department_id)

        if data.department_code is not None:
            # Check for duplicate code (excluding self)
            existing = self.get_department_by_code(data.department_code)
            if existing and existing.department_id != department_id:
                raise ValidationError(
                    f"Department with code '{data.department_code}' already exists"
                )
            department.department_code = data.department_code

        if data.department_name is not None:
            department.department_name = data.department_name

        if data.description is not None:
            department.description = data.description

        if data.parent_department_id is not None:
            self._validate_org_reference(
                Department, data.parent_department_id, "Parent department"
            )
            # Check for circular reference (including transitive cycles)
            if not self._validate_department_parent(
                department_id, data.parent_department_id
            ):
                raise CircularDepartmentError()
            department.parent_department_id = data.parent_department_id

        if data.cost_center_id is not None:
            self._validate_org_reference(CostCenter, data.cost_center_id, "Cost center")
            department.cost_center_id = data.cost_center_id

        if data.head_id is not None:
            self._validate_org_reference(Employee, data.head_id, "Department head")
            department.head_id = data.head_id

        if data.is_active is not None:
            department.is_active = data.is_active

        department.updated_at = datetime.now(UTC)
        department.updated_by_id = self.principal.id if self.principal else None

        return department

    def delete_department(self, department_id: uuid.UUID) -> None:
        """Soft delete a department.

        Args:
            department_id: The department ID.

        Raises:
            DepartmentNotFoundError: If department not found.
            ValidationError: If department has employees assigned.
        """
        department = self.get_department(department_id)

        # Check if any employees are in this department
        employee_count = self.db.scalar(
            select(func.count(Employee.employee_id)).where(
                Employee.department_id == department_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if employee_count and employee_count > 0:
            raise ValidationError(
                f"Cannot delete department with {employee_count} employees assigned"
            )

        child_count = self.db.scalar(
            select(func.count(Department.department_id)).where(
                Department.parent_department_id == department_id,
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )
        if child_count and child_count > 0:
            raise ValidationError(
                f"Cannot delete department with {child_count} child departments"
            )

        department.is_active = False
        department.updated_at = datetime.now(UTC)
        department.updated_by_id = self.principal.id if self.principal else None

    def get_department_tree(self) -> list[DepartmentNode]:
        """Get the department hierarchy as a tree.

        Returns:
            List of root DepartmentNode objects with nested children.
        """
        from sqlalchemy.orm import joinedload

        stmt = (
            select(Department)
            .options(joinedload(Department.head))
            .where(
                Department.organization_id == self.organization_id,
                Department.is_active.is_(True),
            )
        )

        departments = self.db.scalars(stmt).unique().all()

        # Build lookup dict by ID
        {d.department_id: d for d in departments}
        nodes: dict[uuid.UUID, DepartmentNode] = {}

        # Create nodes
        for dept in departments:
            head_name = None
            if dept.head:
                head_name = getattr(dept.head, "full_name", None)

            nodes[dept.department_id] = DepartmentNode(
                department_id=dept.department_id,
                department_code=dept.department_code,
                department_name=dept.department_name,
                parent_department_id=dept.parent_department_id,
                cost_center_id=dept.cost_center_id,
                head_id=dept.head_id,
                head_name=head_name,
                is_active=dept.is_active,
                children=[],
            )

        # Build tree
        roots: list[DepartmentNode] = []
        for node in nodes.values():
            if node.parent_department_id and node.parent_department_id in nodes:
                nodes[node.parent_department_id].children.append(node)
            else:
                roots.append(node)

        return roots

    def get_department_headcount(self, department_id: uuid.UUID) -> DepartmentHeadcount:
        """Get employee headcount for a department.

        Args:
            department_id: The department ID.

        Returns:
            DepartmentHeadcount with employee counts.

        Raises:
            DepartmentNotFoundError: If department not found.
        """
        department = self.get_department(department_id)

        # Get counts by status
        stmt = (
            select(Employee.status, func.count(Employee.employee_id))
            .where(
                Employee.department_id == department_id,
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
            .group_by(Employee.status)
        )
        counts = self.db.execute(stmt).all()

        status_counts = {status: count for status, count in counts}
        total = sum(status_counts.values())

        return DepartmentHeadcount(
            department_id=department_id,
            department_name=department.department_name,
            total_employees=total,
            active_employees=status_counts.get(EmployeeStatus.ACTIVE, 0),
            on_leave=status_counts.get(EmployeeStatus.ON_LEAVE, 0),
            terminated=status_counts.get(EmployeeStatus.TERMINATED, 0),
        )

    def get_department_headcounts_bulk(
        self, department_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Get employee headcounts for multiple departments in a single query.

        Args:
            department_ids: List of department IDs.

        Returns:
            Dictionary mapping department_id to total employee count.
        """
        if not department_ids:
            return {}

        stmt = (
            select(Employee.department_id, func.count(Employee.employee_id))
            .where(
                Employee.department_id.in_(department_ids),
                Employee.organization_id == self.organization_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
            .group_by(Employee.department_id)
        )
        results = self.db.execute(stmt).all()

        counts = {dept_id: count for dept_id, count in results}
        return {dept_id: counts.get(dept_id, 0) for dept_id in department_ids}

    # =========================================================================
    # Designation Methods
    # =========================================================================

    def list_designations(
        self,
        filters: DesignationFilters | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[Designation]:
        """List designations with filters and pagination.

        Args:
            filters: Optional filter criteria.
            pagination: Pagination parameters (offset, limit).

        Returns:
            PaginatedResult containing designations and total count.
        """
        if filters is None:
            filters = DesignationFilters()
        if pagination is None:
            pagination = PaginationParams()

        stmt = select(Designation).where(
            Designation.organization_id == self.organization_id,
            Designation.is_active.is_(True),
        )

        if filters.search:
            search_term = f"%{filters.search}%"
            stmt = stmt.where(
                or_(
                    Designation.designation_name.ilike(search_term),
                    Designation.designation_code.ilike(search_term),
                    Designation.description.ilike(search_term),
                )
            )

        if filters.is_active is not None:
            stmt = stmt.where(Designation.is_active == filters.is_active)

        # Order by name
        stmt = stmt.order_by(Designation.designation_name.asc())

        return paginate(self.db, stmt, pagination)

    def get_designation(self, designation_id: uuid.UUID) -> Designation:
        """Get a designation by ID.

        Args:
            designation_id: The designation ID.

        Returns:
            The Designation object.

        Raises:
            DesignationNotFoundError: If designation not found.
        """
        stmt = select(Designation).where(
            Designation.designation_id == designation_id,
            Designation.organization_id == self.organization_id,
            Designation.is_active.is_(True),
        )
        designation = self.db.scalar(stmt)

        if not designation:
            raise DesignationNotFoundError(designation_id)

        return designation

    def get_designation_by_code(self, code: str) -> Designation | None:
        """Get a designation by code.

        Args:
            code: The designation code.

        Returns:
            The Designation object or None if not found.
        """
        return self.db.scalar(
            select(Designation).where(
                Designation.designation_code == code,
                Designation.organization_id == self.organization_id,
                Designation.is_active.is_(True),
            )
        )

    def create_designation(self, data: DesignationCreateData) -> Designation:
        """Create a new designation.

        Args:
            data: Designation creation data.

        Returns:
            The created Designation (not yet committed).

        Raises:
            ValidationError: If designation code already exists.
        """
        existing = self.get_designation_by_code(data.designation_code)
        if existing:
            raise ValidationError(
                f"Designation with code '{data.designation_code}' already exists"
            )

        designation = Designation(
            organization_id=self.organization_id,
            designation_code=data.designation_code,
            designation_name=data.designation_name,
            description=data.description,
            is_active=data.is_active,
            created_by_id=self.principal.id if self.principal else None,
        )

        self.db.add(designation)
        self.db.flush()

        return designation

    def update_designation(
        self, designation_id: uuid.UUID, data: DesignationUpdateData
    ) -> Designation:
        """Update an existing designation.

        Args:
            designation_id: The designation ID.
            data: Fields to update.

        Returns:
            The updated Designation (not yet committed).

        Raises:
            DesignationNotFoundError: If designation not found.
            ValidationError: If designation code already exists.
        """
        designation = self.get_designation(designation_id)

        if data.designation_code is not None:
            existing = self.get_designation_by_code(data.designation_code)
            if existing and existing.designation_id != designation_id:
                raise ValidationError(
                    f"Designation with code '{data.designation_code}' already exists"
                )
            designation.designation_code = data.designation_code

        if data.designation_name is not None:
            designation.designation_name = data.designation_name

        if data.description is not None:
            designation.description = data.description

        if data.is_active is not None:
            designation.is_active = data.is_active

        designation.updated_at = datetime.now(UTC)
        designation.updated_by_id = self.principal.id if self.principal else None

        return designation

    def delete_designation(self, designation_id: uuid.UUID) -> None:
        """Soft delete a designation.

        Args:
            designation_id: The designation ID.

        Raises:
            DesignationNotFoundError: If designation not found.
            ValidationError: If designation has employees assigned.
        """
        designation = self.get_designation(designation_id)

        employee_count = self.db.scalar(
            select(func.count(Employee.employee_id)).where(
                Employee.designation_id == designation_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if employee_count and employee_count > 0:
            raise ValidationError(
                f"Cannot delete designation with {employee_count} employees assigned"
            )

        designation.is_active = False
        designation.updated_at = datetime.now(UTC)
        designation.updated_by_id = self.principal.id if self.principal else None

    def get_designation_headcount(
        self, designation_id: uuid.UUID
    ) -> DesignationHeadcount:
        """Get employee headcount for a designation.

        Args:
            designation_id: The designation ID.

        Returns:
            DesignationHeadcount with employee counts.

        Raises:
            DesignationNotFoundError: If designation not found.
        """
        designation = self.get_designation(designation_id)

        stmt = (
            select(Employee.status, func.count(Employee.employee_id))
            .where(
                Employee.designation_id == designation_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
            .group_by(Employee.status)
        )
        counts = self.db.execute(stmt).all()

        status_counts = {status: count for status, count in counts}
        total = sum(status_counts.values())

        return DesignationHeadcount(
            designation_id=designation_id,
            designation_name=designation.designation_name,
            total_employees=total,
            active_employees=status_counts.get(EmployeeStatus.ACTIVE, 0),
            on_leave=status_counts.get(EmployeeStatus.ON_LEAVE, 0),
            terminated=status_counts.get(EmployeeStatus.TERMINATED, 0),
        )

    # =========================================================================
    # Employee Grade Methods
    # =========================================================================

    def list_employee_grades(
        self,
        filters: EmployeeGradeFilters | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[EmployeeGrade]:
        """List employee grades with filters and pagination."""
        if filters is None:
            filters = EmployeeGradeFilters()
        if pagination is None:
            pagination = PaginationParams()

        stmt = select(EmployeeGrade).where(
            EmployeeGrade.organization_id == self.organization_id,
        )

        if filters.is_active is not None:
            stmt = stmt.where(EmployeeGrade.is_active == filters.is_active)

        if filters.search:
            search_term = f"%{filters.search}%"
            stmt = stmt.where(
                or_(
                    EmployeeGrade.grade_name.ilike(search_term),
                    EmployeeGrade.grade_code.ilike(search_term),
                )
            )

        stmt = stmt.order_by(EmployeeGrade.rank.asc(), EmployeeGrade.grade_name.asc())

        return paginate(self.db, stmt, pagination)

    def get_employee_grade(self, grade_id: uuid.UUID) -> EmployeeGrade:
        """Get an employee grade by ID."""
        stmt = select(EmployeeGrade).where(
            EmployeeGrade.grade_id == grade_id,
            EmployeeGrade.organization_id == self.organization_id,
        )
        grade = self.db.scalar(stmt)

        if not grade:
            raise EmployeeGradeNotFoundError(grade_id)

        return grade

    def get_employee_grade_by_code(self, code: str) -> EmployeeGrade | None:
        """Get an employee grade by code."""
        return self.db.scalar(
            select(EmployeeGrade).where(
                EmployeeGrade.grade_code == code,
                EmployeeGrade.organization_id == self.organization_id,
            )
        )

    def create_employee_grade(self, data: EmployeeGradeCreateData) -> EmployeeGrade:
        """Create a new employee grade."""
        existing = self.get_employee_grade_by_code(data.grade_code)
        if existing:
            raise ValidationError(
                f"Employee grade with code '{data.grade_code}' already exists"
            )

        grade = EmployeeGrade(
            organization_id=self.organization_id,
            grade_code=data.grade_code,
            grade_name=data.grade_name,
            description=data.description,
            rank=data.rank,
            min_salary=data.min_salary,
            max_salary=data.max_salary,
            is_active=data.is_active,
            created_by_id=self.principal.id if self.principal else None,
        )

        self.db.add(grade)
        self.db.flush()

        return grade

    def update_employee_grade(
        self, grade_id: uuid.UUID, data: EmployeeGradeUpdateData
    ) -> EmployeeGrade:
        """Update an existing employee grade."""
        grade = self.get_employee_grade(grade_id)

        if data.grade_code is not None:
            existing = self.get_employee_grade_by_code(data.grade_code)
            if existing and existing.grade_id != grade_id:
                raise ValidationError(
                    f"Employee grade with code '{data.grade_code}' already exists"
                )
            grade.grade_code = data.grade_code

        if data.grade_name is not None:
            grade.grade_name = data.grade_name

        if data.description is not None:
            grade.description = data.description

        if data.rank is not None:
            grade.rank = data.rank

        if data.min_salary is not None:
            grade.min_salary = data.min_salary

        if data.max_salary is not None:
            grade.max_salary = data.max_salary

        if data.is_active is not None:
            grade.is_active = data.is_active

        grade.updated_at = datetime.now(UTC)
        grade.updated_by_id = self.principal.id if self.principal else None

        return grade

    def delete_employee_grade(self, grade_id: uuid.UUID) -> None:
        """Delete an employee grade (hard delete if no employees assigned)."""
        grade = self.get_employee_grade(grade_id)

        employee_count = self.db.scalar(
            select(func.count(Employee.employee_id)).where(
                Employee.grade_id == grade_id,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if employee_count and employee_count > 0:
            raise ValidationError(
                f"Cannot delete employee grade with {employee_count} employees assigned"
            )

        self.db.delete(grade)

    # =========================================================================
    # Locations
    # =========================================================================

    def list_locations(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[Location]:
        if pagination is None:
            pagination = PaginationParams()

        stmt = select(Location).where(Location.organization_id == self.organization_id)

        if search:
            search_term = f"%{search}%"
            stmt = stmt.where(
                or_(
                    Location.location_code.ilike(search_term),
                    Location.location_name.ilike(search_term),
                )
            )

        if is_active is not None:
            stmt = stmt.where(Location.is_active == is_active)

        stmt = stmt.order_by(Location.location_name.asc())

        return paginate(self.db, stmt, pagination)

    def get_location(self, location_id: uuid.UUID) -> Location:
        location = self.db.scalar(
            select(Location).where(
                Location.location_id == location_id,
                Location.organization_id == self.organization_id,
            )
        )
        if not location:
            raise LocationNotFoundError(location_id)
        return location

    def create_location(
        self,
        *,
        location_code: str,
        location_name: str,
        location_type: LocationType | None,
        address_line_1: str | None = None,
        address_line_2: str | None = None,
        city: str | None = None,
        state_province: str | None = None,
        postal_code: str | None = None,
        country_code: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        geofence_radius_m: float | None = None,
        geofence_enabled: bool | None = None,
        geofence_polygon: str | None = None,
        is_active: bool | None = None,
    ) -> Location:
        location = Location(
            organization_id=self.organization_id,
            location_code=location_code,
            location_name=location_name,
            location_type=location_type,
            address_line_1=address_line_1,
            address_line_2=address_line_2,
            city=city,
            state_province=state_province,
            postal_code=postal_code,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            geofence_radius_m=geofence_radius_m,
            geofence_enabled=geofence_enabled,
            geofence_polygon=geofence_polygon,
            is_active=is_active,
            created_by_id=self.principal.id if self.principal else None,
        )
        self.db.add(location)
        self.db.flush()
        self.db.refresh(location)
        return location

    def update_location(
        self,
        location_id: uuid.UUID,
        update_data: dict,
    ) -> Location:
        location = self.get_location(location_id)
        for key, value in update_data.items():
            if hasattr(location, key):
                setattr(location, key, value)
        if hasattr(location, "updated_at"):
            location.updated_at = datetime.now(UTC)
        if hasattr(location, "updated_by_id"):
            location.updated_by_id = self.principal.id if self.principal else None
        self.db.refresh(location)
        return location

    def delete_location(self, location_id: uuid.UUID) -> None:
        location = self.get_location(location_id)
        self.db.delete(location)
