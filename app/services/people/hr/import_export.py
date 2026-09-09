"""
HR Importers.

CSV importers for HR master data and employees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.finance.core_org.cost_center import CostCenter
from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeStatus,
    Position,
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.person import Person
from app.services.finance.import_export.base import BaseImporter, FieldMapping
from app.services.people.hr.employment_types import (
    EmploymentTypeService,
    EmploymentTypeView,
)
from app.services.people.hr.errors import EmploymentTypeNotFoundError
from app.services.people.hr.organization_types import EmploymentTypeCreateData

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ImportActor:
    id: UUID


def _first_value(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


class DepartmentImporter(BaseImporter[Department]):
    """Importer for departments."""

    entity_name = "Department"
    model_class = Department

    def get_field_mappings(self) -> list[FieldMapping]:
        return [
            FieldMapping("Department Code", "department_code", required=False),
            FieldMapping("Department Name", "department_name", required=False),
            FieldMapping("Description", "description", required=False),
            FieldMapping(
                "Parent Department Code", "parent_department_code", required=False
            ),
            FieldMapping("Cost Center Code", "cost_center_code", required=False),
            FieldMapping("Head Employee Code", "head_employee_code", required=False),
            FieldMapping(
                "Is Active", "is_active", required=False, transformer=self.parse_boolean
            ),
        ]

    def get_unique_key(self, row: dict[str, Any]) -> str:
        return _first_value(row, "Department Code") or "unknown"

    def check_duplicate(self, row: dict[str, Any]) -> Department | None:
        code = _first_value(row, "Department Code")
        if not code:
            return None
        return self.db.scalar(
            select(Department).where(
                Department.organization_id == self.config.organization_id,
                Department.department_code == code,
                Department.is_active.is_(True),
            )
        )

    def _resolve_department_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"department:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        dept = self.db.scalar(
            select(Department).where(
                Department.organization_id == self.config.organization_id,
                Department.department_code == code,
                Department.is_active.is_(True),
            )
        )
        if dept:
            self._id_cache[cache_key] = dept.department_id
            return dept.department_id
        return None

    def _resolve_cost_center_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"cost_center:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        cc = self.db.scalar(
            select(CostCenter).where(
                CostCenter.organization_id == self.config.organization_id,
                CostCenter.cost_center_code == code,
            )
        )
        if cc:
            self._id_cache[cache_key] = cc.cost_center_id
            return cc.cost_center_id
        return None

    def _resolve_employee_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"employee:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        employee = self.db.scalar(
            select(Employee).where(
                Employee.organization_id == self.config.organization_id,
                Employee.employee_code == code,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if employee:
            self._id_cache[cache_key] = employee.employee_id
            return employee.employee_id
        return None

    def create_entity(self, row: dict[str, Any]) -> Department:
        department_code = _first_value(row, "department_code")
        department_name = _first_value(row, "department_name")

        if not department_code:
            raise ValueError("Department Code is required")
        if not department_name:
            raise ValueError("Department Name is required")

        parent_department_id = self._resolve_department_id(
            row.get("parent_department_code")
        )
        if row.get("parent_department_code") and not parent_department_id:
            raise ValueError(
                f"Parent Department not found: {row.get('parent_department_code')}"
            )

        cost_center_id = self._resolve_cost_center_id(row.get("cost_center_code"))
        if row.get("cost_center_code") and not cost_center_id:
            raise ValueError(f"Cost Center not found: {row.get('cost_center_code')}")

        head_id = self._resolve_employee_id(row.get("head_employee_code"))
        if row.get("head_employee_code") and not head_id:
            raise ValueError(
                f"Head Employee not found: {row.get('head_employee_code')}"
            )

        return Department(
            department_id=uuid4(),
            organization_id=self.config.organization_id,
            department_code=department_code[:20],
            department_name=department_name[:100],
            description=row.get("description"),
            parent_department_id=parent_department_id,
            head_id=head_id,
            cost_center_id=cost_center_id,
            is_active=row.get("is_active")
            if row.get("is_active") is not None
            else True,
        )


class DesignationImporter(BaseImporter[Designation]):
    """Importer for designations."""

    entity_name = "Designation"
    model_class = Designation

    def get_field_mappings(self) -> list[FieldMapping]:
        return [
            FieldMapping("Designation Code", "designation_code", required=False),
            FieldMapping("Designation Name", "designation_name", required=False),
            FieldMapping("Description", "description", required=False),
            FieldMapping(
                "Is Active", "is_active", required=False, transformer=self.parse_boolean
            ),
        ]

    def get_unique_key(self, row: dict[str, Any]) -> str:
        return _first_value(row, "Designation Code") or "unknown"

    def check_duplicate(self, row: dict[str, Any]) -> Designation | None:
        code = _first_value(row, "Designation Code")
        if not code:
            return None
        return self.db.scalar(
            select(Designation).where(
                Designation.organization_id == self.config.organization_id,
                Designation.designation_code == code,
                Designation.is_active.is_(True),
            )
        )

    def create_entity(self, row: dict[str, Any]) -> Designation:
        designation_code = _first_value(row, "designation_code")
        designation_name = _first_value(row, "designation_name")

        if not designation_code:
            raise ValueError("Designation Code is required")
        if not designation_name:
            raise ValueError("Designation Name is required")

        return Designation(
            designation_id=uuid4(),
            organization_id=self.config.organization_id,
            designation_code=designation_code[:20],
            designation_name=designation_name[:100],
            description=row.get("description"),
            is_active=row.get("is_active")
            if row.get("is_active") is not None
            else True,
        )


class EmploymentTypeImporter(BaseImporter[EmploymentTypeView]):
    """Importer for employment types."""

    entity_name = "Employment Type"
    model_class = EmploymentTypeView

    def get_field_mappings(self) -> list[FieldMapping]:
        return [
            FieldMapping("Employment Type Code", "type_code", required=False),
            FieldMapping("Employment Type Name", "type_name", required=False),
            FieldMapping("Description", "description", required=False),
            FieldMapping(
                "Is Active", "is_active", required=False, transformer=self.parse_boolean
            ),
        ]

    def get_unique_key(self, row: dict[str, Any]) -> str:
        return _first_value(row, "Employment Type Code") or "unknown"

    def check_duplicate(self, row: dict[str, Any]) -> EmploymentTypeView | None:
        code = _first_value(row, "Employment Type Code")
        if not code:
            return None
        return EmploymentTypeService(self.db, self.config.organization_id).get_by_code(
            code
        )

    def create_entity(self, row: dict[str, Any]) -> EmploymentTypeView:
        type_code = _first_value(row, "type_code")
        type_name = _first_value(row, "type_name")

        if not type_code:
            raise ValueError("Employment Type Code is required")
        if not type_name:
            raise ValueError("Employment Type Name is required")

        return EmploymentTypeService(
            self.db,
            self.config.organization_id,
            _ImportActor(self.config.user_id),
        ).create_employment_type(
            EmploymentTypeCreateData(
                type_code=type_code[:20],
                type_name=type_name[:100],
                description=row.get("description"),
                is_active=(
                    cast(bool, row.get("is_active"))
                    if row.get("is_active") is not None
                    else True
                ),
            )
        )

    def _commit_batch(self, batch: list[EmploymentTypeView]) -> None:
        """Record owner-created rows without adding immutable views to the ORM.

        ``EmploymentTypeService.create_employment_type`` owns the write and flushes
        each row. The base importer still batches result accounting, so this seam
        deliberately records those already-flushed IDs and performs no second write.
        """
        self.result.imported_ids.extend(row.employment_type_id for row in batch)


class EmployeeImporter(BaseImporter[Employee]):
    """Importer for employees."""

    entity_name = "Employee"
    model_class = Employee

    def __init__(self, db, config) -> None:
        super().__init__(db, config)
        self._imported_employee_ids: list[UUID] = []

    def get_field_mappings(self) -> list[FieldMapping]:
        return [
            FieldMapping("Employee Code", "employee_code", required=False),
            FieldMapping("Staff ID", "employee_code_alt", required=False),
            FieldMapping("First Name", "first_name", required=False),
            FieldMapping("Last Name", "last_name", required=False),
            FieldMapping("Work Email", "work_email", required=False),
            FieldMapping("Email", "work_email_alt", required=False),
            FieldMapping("Phone", "phone", required=False),
            FieldMapping(
                "Date of Joining",
                "date_of_joining",
                required=False,
                transformer=self.parse_date,
            ),
            FieldMapping(
                "Employee Status",
                "status",
                required=False,
                transformer=lambda v: self.parse_enum(
                    v, EmployeeStatus, EmployeeStatus.DRAFT
                ),
            ),
            FieldMapping("Department Code", "department_code", required=False),
            FieldMapping("Designation Code", "designation_code", required=False),
            FieldMapping(
                "Employment Type Code", "employment_type_code", required=False
            ),
            FieldMapping("Position Code", "position_code", required=False),
            FieldMapping("Reports To Code", "reports_to_code", required=False),
            FieldMapping(
                "Expense Approver Code", "expense_approver_code", required=False
            ),
            FieldMapping("Cost Center Code", "cost_center_code", required=False),
        ]

    def get_unique_key(self, row: dict[str, Any]) -> str:
        return (
            _first_value(row, "Employee Code", "Staff ID")
            or _first_value(row, "Work Email", "Email")
            or "unknown"
        )

    def check_duplicate(self, row: dict[str, Any]) -> Employee | None:
        employee_code = _first_value(row, "Employee Code", "Staff ID")
        if employee_code:
            existing = self.db.scalar(
                select(Employee).where(
                    Employee.organization_id == self.config.organization_id,
                    Employee.employee_code == employee_code,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if existing:
                return existing

        email = _first_value(row, "Work Email", "Email")
        if email:
            person = self.db.scalar(select(Person).where(Person.email == email))
            if person:
                return self.db.scalar(
                    select(Employee).where(
                        Employee.organization_id == self.config.organization_id,
                        Employee.person_id == person.id,
                        Employee.status != EmployeeStatus.TERMINATED,
                    )
                )
        return None

    def _resolve_department_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"department:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        dept = self.db.scalar(
            select(Department).where(
                Department.organization_id == self.config.organization_id,
                Department.department_code == code,
                Department.is_active.is_(True),
            )
        )
        if dept:
            self._id_cache[cache_key] = dept.department_id
            return dept.department_id
        return None

    def _resolve_designation_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"designation:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        designation = self.db.scalar(
            select(Designation).where(
                Designation.organization_id == self.config.organization_id,
                Designation.designation_code == code,
                Designation.is_active.is_(True),
            )
        )
        if designation:
            self._id_cache[cache_key] = designation.designation_id
            return designation.designation_id
        return None

    def _resolve_employment_type_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"employment_type:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        try:
            emp_type = EmploymentTypeService(
                self.db, self.config.organization_id
            ).require_active_by_code(code)
        except EmploymentTypeNotFoundError:
            return None
        self._id_cache[cache_key] = emp_type.employment_type_id
        return emp_type.employment_type_id

    def _resolve_employee_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"employee:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        employee = self.db.scalar(
            select(Employee).where(
                Employee.organization_id == self.config.organization_id,
                Employee.employee_code == code,
                Employee.status != EmployeeStatus.TERMINATED,
            )
        )
        if employee:
            self._id_cache[cache_key] = employee.employee_id
            return employee.employee_id
        return None

    def _resolve_position_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"position:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        position = self.db.scalar(
            select(Position).where(
                Position.organization_id == self.config.organization_id,
                Position.position_code == code,
                Position.is_active.is_(True),
            )
        )
        if position:
            self._id_cache[cache_key] = position.position_id
            return position.position_id
        return None

    def _resolve_cost_center_id(self, code: str | None) -> UUID | None:
        if not code:
            return None
        cache_key = f"cost_center:{code}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]
        cc = self.db.scalar(
            select(CostCenter).where(
                CostCenter.organization_id == self.config.organization_id,
                CostCenter.cost_center_code == code,
            )
        )
        if cc:
            self._id_cache[cache_key] = cc.cost_center_id
            return cc.cost_center_id
        return None

    def create_entity(self, row: dict[str, Any]) -> Employee:
        # Bulk import writes ``Employee.reports_to_id`` directly without
        # provisioning hr.position / hr.position_assignment. Each created
        # employee_id is tracked on ``self._imported_employee_ids`` so the
        # caller can finalize with ``reconcile_positions()`` after
        # ``import_file()`` returns. See ``EmployeeService.set_manager`` for
        # the per-row chokepoint used outside bulk paths.
        employee_code = _first_value(row, "employee_code", "employee_code_alt")
        first_name = _first_value(row, "first_name")
        last_name = _first_value(row, "last_name")
        work_email = _first_value(row, "work_email", "work_email_alt")
        date_of_joining = row.get("date_of_joining")

        if not employee_code:
            raise ValueError("Employee Code is required")
        if not first_name or not last_name:
            raise ValueError("First Name and Last Name are required")
        if not work_email:
            raise ValueError("Work Email is required")
        if not date_of_joining:
            raise ValueError("Date of Joining is required")

        person = self.db.scalar(select(Person).where(Person.email == work_email))
        if person and person.organization_id != self.config.organization_id:
            raise ValueError("Work Email belongs to another organization")

        if not person:
            person = Person(
                id=uuid4(),
                organization_id=self.config.organization_id,
                first_name=first_name[:80],
                last_name=last_name[:80],
                display_name=f"{first_name} {last_name}".strip()[:120],
                email=work_email[:255],
                phone=row.get("phone"),
            )
            self.db.add(person)
            self.db.flush()

        department_id = self._resolve_department_id(row.get("department_code"))
        designation_id = self._resolve_designation_id(row.get("designation_code"))
        employment_type_id = self._resolve_employment_type_id(
            row.get("employment_type_code")
        )
        position_id = self._resolve_position_id(row.get("position_code"))
        reports_to_id = self._resolve_employee_id(row.get("reports_to_code"))
        expense_approver_id = self._resolve_employee_id(
            row.get("expense_approver_code")
        )
        cost_center_id = self._resolve_cost_center_id(row.get("cost_center_code"))

        if row.get("department_code") and not department_id:
            raise ValueError(f"Department not found: {row.get('department_code')}")
        if row.get("designation_code") and not designation_id:
            raise ValueError(f"Designation not found: {row.get('designation_code')}")
        if row.get("employment_type_code") and not employment_type_id:
            raise ValueError(
                f"Employment Type not found: {row.get('employment_type_code')}"
            )
        if row.get("position_code") and not position_id:
            raise ValueError(f"Position not found: {row.get('position_code')}")
        if row.get("reports_to_code") and not reports_to_id:
            raise ValueError(f"Manager not found: {row.get('reports_to_code')}")
        if row.get("expense_approver_code") and not expense_approver_id:
            raise ValueError(
                f"Expense approver not found: {row.get('expense_approver_code')}"
            )
        if row.get("cost_center_code") and not cost_center_id:
            raise ValueError(f"Cost Center not found: {row.get('cost_center_code')}")

        employee = Employee(
            employee_id=uuid4(),
            organization_id=self.config.organization_id,
            person_id=person.id,
            employee_code=employee_code[:30],
            date_of_joining=date_of_joining,
            status=row.get("status") or EmployeeStatus.DRAFT,
            department_id=department_id,
            designation_id=designation_id,
            employment_type_id=employment_type_id,
            reports_to_id=reports_to_id,
            expense_approver_id=expense_approver_id,
            cost_center_id=cost_center_id,
            personal_phone=row.get("phone"),
            personal_email=None,
        )
        if position_id:
            self._assign_position(employee, position_id)
        self._imported_employee_ids.append(employee.employee_id)
        return employee

    def _assign_position(self, employee: Employee, position_id: UUID) -> None:
        existing_position_assignment = self.db.scalar(
            select(PositionAssignment.position_assignment_id).where(
                PositionAssignment.organization_id == self.config.organization_id,
                PositionAssignment.position_id == position_id,
                PositionAssignment.assignment_type == PositionAssignmentType.PRIMARY,
                PositionAssignment.end_date.is_(None),
            )
        )
        if existing_position_assignment:
            raise ValueError("Position already has an active primary assignment")

        self.db.add(
            PositionAssignment(
                position_assignment_id=uuid4(),
                organization_id=self.config.organization_id,
                employee_id=employee.employee_id,
                position_id=position_id,
                assignment_type=PositionAssignmentType.PRIMARY,
                start_date=employee.date_of_joining,
            )
        )

    def reconcile_positions(self):
        """
        Provision positions/assignments for employees created during this
        import batch.

        Invoked automatically by ``ImportService.run_import`` via the
        duck-typed post-import hook, inside the same transaction that
        commits the imported employees. Exposed publicly so tests and
        ad-hoc callers can drive it directly. Safe under partial-success
        state — IDs whose row never committed are silently skipped by the
        bulk reconcile.
        """
        from app.services.people.hr.positions import (
            PositionService,
            ReconcileResult,
        )

        if not self._imported_employee_ids:
            return ReconcileResult()
        return PositionService(
            self.db, self.config.organization_id
        ).reconcile_from_reports_to_id(employee_ids=self._imported_employee_ids)
