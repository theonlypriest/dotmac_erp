"""Type definitions for organization service.

These dataclasses define the contract for department, designation, and team operations.
They are framework-agnostic (no Pydantic, no FastAPI).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

__all__ = [
    # Department types
    "DepartmentFilters",
    "DepartmentCreateData",
    "DepartmentUpdateData",
    "DepartmentNode",
    "DepartmentHeadcount",
    # Designation types
    "DesignationFilters",
    "DesignationCreateData",
    "DesignationUpdateData",
    "DesignationHeadcount",
    # Employment Type types
    "EmploymentTypeFilters",
    "EmploymentTypeCreateData",
    "EmploymentTypeUpdateData",
    # Employee Grade types
    "EmployeeGradeFilters",
    "EmployeeGradeCreateData",
    "EmployeeGradeUpdateData",
]


# ==============================================================================
# Department Types
# ==============================================================================


@dataclass
class DepartmentFilters:
    """Filters for listing departments."""

    organization_id: uuid.UUID | None = None
    is_active: bool | None = None
    parent_department_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    search: str | None = None


@dataclass
class DepartmentCreateData:
    """Data for creating a department."""

    department_code: str
    department_name: str
    description: str | None = None
    parent_department_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    head_id: uuid.UUID | None = None
    is_active: bool = True


@dataclass
class DepartmentUpdateData:
    """Data for updating a department (all fields optional)."""

    department_code: str | None = None
    department_name: str | None = None
    description: str | None = None
    parent_department_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    head_id: uuid.UUID | None = None
    is_active: bool | None = None


@dataclass
class DepartmentNode:
    """Node in department hierarchy tree."""

    department_id: uuid.UUID
    department_code: str
    department_name: str
    parent_department_id: uuid.UUID | None
    cost_center_id: uuid.UUID | None
    head_id: uuid.UUID | None
    head_name: str | None
    is_active: bool
    children: list[DepartmentNode] = field(default_factory=list)


@dataclass
class DepartmentHeadcount:
    """Headcount information for a department."""

    department_id: uuid.UUID
    department_name: str
    total_employees: int
    active_employees: int
    on_leave: int
    terminated: int


# ==============================================================================
# Designation Types
# ==============================================================================


@dataclass
class DesignationHeadcount:
    """Headcount information for a designation."""

    designation_id: uuid.UUID
    designation_name: str
    total_employees: int
    active_employees: int
    on_leave: int
    terminated: int


@dataclass
class DesignationFilters:
    """Filters for listing designations."""

    organization_id: uuid.UUID | None = None
    search: str | None = None
    is_active: bool | None = None


@dataclass
class DesignationCreateData:
    """Data for creating a designation."""

    designation_code: str
    designation_name: str
    description: str | None = None
    is_active: bool = True


@dataclass
class DesignationUpdateData:
    """Data for updating a designation (all fields optional)."""

    designation_code: str | None = None
    designation_name: str | None = None
    description: str | None = None
    is_active: bool | None = None


# ==============================================================================
# Employment Type Types
# ==============================================================================


@dataclass
class EmploymentTypeFilters:
    """Filters for listing employment types."""

    organization_id: uuid.UUID | None = None
    search: str | None = None
    is_active: bool | None = None


@dataclass
class EmploymentTypeCreateData:
    """Data for creating an employment type."""

    type_code: str
    type_name: str
    description: str | None = None
    is_active: bool = True


@dataclass
class EmploymentTypeUpdateData:
    """Data for updating an employment type (all fields optional)."""

    type_code: str | None = None
    type_name: str | None = None
    description: str | None = None
    description_is_set: bool = False
    is_active: bool | None = None


# ==============================================================================
# Employee Grade Types
# ==============================================================================


@dataclass
class EmployeeGradeFilters:
    """Filters for listing employee grades."""

    organization_id: uuid.UUID | None = None
    search: str | None = None
    is_active: bool | None = None


@dataclass
class EmployeeGradeCreateData:
    """Data for creating an employee grade."""

    grade_code: str
    grade_name: str
    description: str | None = None
    rank: int = 0
    min_salary: Decimal | None = None
    max_salary: Decimal | None = None
    is_active: bool = True


@dataclass
class EmployeeGradeUpdateData:
    """Data for updating an employee grade (all fields optional)."""

    grade_code: str | None = None
    grade_name: str | None = None
    description: str | None = None
    rank: int | None = None
    min_salary: Decimal | None = None
    max_salary: Decimal | None = None
    is_active: bool | None = None
