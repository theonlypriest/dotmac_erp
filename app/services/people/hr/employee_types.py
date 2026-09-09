"""Type definitions for employee service.

These dataclasses define the contract for employee operations.
They are framework-agnostic (no Pydantic, no FastAPI).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Union

from app.models.people.hr import EmployeeStatus as EmploymentStatus
from app.models.people.hr.employee import SalaryMode

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


# =============================================================================
# Form Parsing Utilities
# =============================================================================


class FormParser:
    """Utility for parsing form data with type coercion.

    Provides safe extraction of typed values from form data, handling
    empty strings, type conversion errors, and file upload fields.

    Example:
        parser = FormParser(form_data)
        name = parser.get_str("name")
        age = parser.int("age")
        salary = parser.decimal("salary")
    """

    def __init__(self, form: Mapping[str, Any]) -> None:
        self._form = form

    def get_str(self, key: str, default: str = "") -> str:
        """Extract string value, stripping whitespace."""
        value = self._form.get(key, default)
        # Handle UploadFile objects (return default)
        if hasattr(value, "filename"):  # UploadFile check without import
            return default
        if value is None:
            return default
        return str(value).strip()

    def str_or_none(self, key: str) -> str | None:
        """Extract string value or None if empty."""
        value = self.get_str(key, "")
        return value if value else None

    def int(self, key: str) -> int | None:
        """Extract integer value or None if invalid/empty."""
        value = self.get_str(key, "")
        if not value:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def decimal(self, key: str) -> Decimal | None:
        """Extract Decimal value or None if invalid/empty."""
        value = self.get_str(key, "")
        if not value:
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    def date(self, key: str) -> date | None:
        """Extract date from ISO format (YYYY-MM-DD) or None if invalid."""
        value = self.get_str(key, "")
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def enum(
        self, key: str, enum_class: type, default: Any | None = None
    ) -> Any | None:
        """Extract enum value or default if invalid."""
        value = self.get_str(key, "")
        if not value:
            return default
        try:
            return enum_class(value)
        except ValueError:
            return default


@dataclass
class ValidationResult:
    """Result of data validation.

    Attributes:
        is_valid: Whether validation passed.
        errors: Dictionary of field -> error message.
    """

    is_valid: bool
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def success(cls) -> ValidationResult:
        """Create a successful validation result."""
        return cls(is_valid=True)

    @classmethod
    def failure(cls, errors: dict[str, str]) -> ValidationResult:
        """Create a failed validation result."""
        return cls(is_valid=False, errors=errors)


__all__ = [
    "FormParser",
    "ValidationResult",
    "EmployeeFilters",
    "EmployeeCreateData",
    "EmployeeUpdateData",
    "EmployeeSummary",
    "TerminationData",
    "BulkResult",
    "BulkUpdateData",
]


@dataclass
class EmployeeFilters:
    """Filters for listing employees."""

    status: Union[EmploymentStatus, str] | None = None
    statuses: list[EmploymentStatus] | None = None
    is_active: bool | None = None
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None
    expense_approver_id: uuid.UUID | None = None
    employment_type_id: uuid.UUID | None = None
    search: str | None = None  # Name, email, employee_code
    date_of_joining_from: date | None = None
    date_of_joining_to: date | None = None
    date_of_leaving_from: date | None = None
    date_of_leaving_to: date | None = None
    include_archived: bool = False
    archive_only: bool = False
    include_deleted: bool = False
    sort_key: str | None = None
    sort_dir: str | None = None


@dataclass
class EmployeeCreateData:
    """Data for creating an employee.

    Note: Personal info (name, email, phone, address) is stored in the Person model.
    The Employee model stores HR-specific data and links to Person via person_id.
    """

    # Employee code (auto-generated if not provided)
    employee_number: str | None = None
    # Organization structure (UUIDs)
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    position_id: uuid.UUID | None = None
    employment_type_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None
    expense_approver_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    assigned_location_id: uuid.UUID | None = None
    default_shift_type_id: uuid.UUID | None = None
    # Employment dates
    date_of_joining: date | None = None
    probation_end_date: date | None = None
    confirmation_date: date | None = None
    nysc_start_date: date | None = None
    nysc_end_date: date | None = None
    # Status
    status: EmploymentStatus | None = None
    # Compensation
    ctc: Decimal | None = None
    salary_mode: SalaryMode | None = None
    # Personal contact (separate from Person's work email/phone)
    personal_email: str | None = None
    personal_phone: str | None = None
    # Emergency contact
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    # Bank Details (for payroll)
    bank_name: str | None = None
    bank_account_number: str | None = None
    bank_sort_code: str | None = None
    bank_account_name: str | None = None
    # Notes
    notes: str | None = None
    dotmac_sub_access_enabled: bool = False
    dotmac_sub_roles: list[str] = field(default_factory=lambda: ["staff"])


@dataclass
class EmployeeUpdateData:
    """Data for updating an employee (all fields optional).

    Note: Personal info updates should be made to the Person record directly.
    """

    employee_number: str | None = None
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    employment_type_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    reports_to_id: uuid.UUID | None = None
    expense_approver_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    assigned_location_id: uuid.UUID | None = None
    default_shift_type_id: uuid.UUID | None = None
    date_of_joining: date | None = None
    date_of_leaving: date | None = None
    final_payroll_cutoff_date: date | None = None
    probation_end_date: date | None = None
    confirmation_date: date | None = None
    nysc_start_date: date | None = None
    nysc_end_date: date | None = None
    status: EmploymentStatus | None = None
    # Compensation
    ctc: Decimal | None = None
    salary_mode: SalaryMode | None = None
    # Personal contact (separate from Person's work email/phone)
    personal_email: str | None = None
    personal_phone: str | None = None
    # Emergency contact
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    # Bank Details
    bank_name: str | None = None
    bank_account_number: str | None = None
    bank_sort_code: str | None = None
    bank_account_name: str | None = None
    notes: str | None = None
    dotmac_sub_access_enabled: bool | None = None
    dotmac_sub_roles: list[str] | None = None
    eligible_for_final_payroll: bool | None = None
    # Tracks which fields were explicitly provided (for null handling)
    provided_fields: set[str] = field(default_factory=set, repr=False)


@dataclass
class EmployeeSummary:
    """Summary view of employee for search/autocomplete."""

    id: uuid.UUID
    name: str
    email: str | None
    employee_number: str | None
    department: str | None
    designation: str | None
    status: EmploymentStatus


@dataclass
class TerminationData:
    """Data for employee termination."""

    date_of_leaving: date
    reason: str | None = None
    exit_interview_notes: str | None = None
    eligible_for_final_payroll: bool = False
    final_payroll_cutoff_date: date | None = None


@dataclass
class BulkUpdateData:
    """Data for bulk updating employees."""

    ids: list[uuid.UUID] = field(default_factory=list)
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    status: EmploymentStatus | None = None
    reports_to_id: uuid.UUID | None = None


@dataclass
class BulkResult:
    """Result of a bulk operation."""

    updated_count: int = 0
    deleted_count: int = 0
    failed_ids: list[uuid.UUID] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
