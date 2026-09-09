"""
Data Completeness Report Service.

Identifies employees with missing data required for statutory exports
(PAYE, Pension, NHF, Bank Upload). Helps payroll admins ensure compliance
before generating export files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.people.hr.employee import Employee, EmployeeStatus
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.salary_assignment import (
    SalaryStructureAssignment,
)
from app.models.person import Person
from app.services.finance.banking.bank_directory import BankDirectoryService
from app.services.people.payroll.eligibility import payroll_employee_eligibility_clause
from app.services.people.payroll.employment_type_classification import (
    classify_payroll_employment_type,
)

logger = logging.getLogger(__name__)


class ExportType(str, Enum):
    """Types of statutory exports."""

    PAYE = "paye"
    PENSION = "pension"
    NHF = "nhf"
    BANK_UPLOAD = "bank_upload"


class PayrollIssueType(str, Enum):
    """Types of issues that flag a slip for review."""

    MISSING_BANK_DETAILS = "missing_bank_details"
    MISSING_TAX_PROFILE = "missing_tax_profile"
    MISSING_TIN = "missing_tin"
    MISSING_SALARY_ASSIGNMENT = "missing_salary_assignment"
    EXPIRED_SALARY_ASSIGNMENT = "expired_salary_assignment"
    NO_ATTENDANCE_RECORDS = "no_attendance_records"
    ATTENDANCE_GAPS = "attendance_gaps"
    NEW_HIRE_PRORATION = "new_hire_proration"
    EXIT_PRORATION = "exit_proration"


class CompletenessStatus(str, Enum):
    """Data completeness status."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING_PROFILE = "missing_profile"


@dataclass
class MissingField:
    """A missing field for an employee."""

    field_name: str
    field_label: str
    export_types: list[ExportType]
    severity: str = "required"  # required, recommended


@dataclass
class EmployeeCompleteness:
    """Data completeness for a single employee."""

    employee_id: UUID
    employee_number: str
    full_name: str
    department_name: str | None
    status: CompletenessStatus
    missing_fields: list[MissingField] = field(default_factory=list)
    has_tax_profile: bool = False
    has_bank_details: bool = False
    bank_code_resolvable: bool = False

    @property
    def is_paye_ready(self) -> bool:
        """Check if employee has all PAYE required fields."""
        return not any(
            ExportType.PAYE in f.export_types and f.severity == "required"
            for f in self.missing_fields
        )

    @property
    def is_pension_ready(self) -> bool:
        """Check if employee has all Pension required fields."""
        return not any(
            ExportType.PENSION in f.export_types and f.severity == "required"
            for f in self.missing_fields
        )

    @property
    def is_nhf_ready(self) -> bool:
        """Check if employee has all NHF required fields."""
        return not any(
            ExportType.NHF in f.export_types and f.severity == "required"
            for f in self.missing_fields
        )

    @property
    def is_bank_ready(self) -> bool:
        """Check if employee has all Bank Upload required fields."""
        return not any(
            ExportType.BANK_UPLOAD in f.export_types and f.severity == "required"
            for f in self.missing_fields
        )


@dataclass
class CompletenessReportResult:
    """Result of data completeness report generation."""

    organization_id: UUID
    report_date: date
    total_employees: int
    complete_count: int
    incomplete_count: int
    missing_profile_count: int
    employees: list[EmployeeCompleteness]

    # Aggregate counts by export type
    paye_ready_count: int = 0
    pension_ready_count: int = 0
    nhf_ready_count: int = 0
    bank_ready_count: int = 0

    @property
    def completeness_rate(self) -> float:
        """Overall completeness percentage."""
        if self.total_employees == 0:
            return 100.0
        return (self.complete_count / self.total_employees) * 100

    @property
    def incomplete_employees(self) -> list[EmployeeCompleteness]:
        """Get only employees with incomplete data."""
        return [e for e in self.employees if e.status != CompletenessStatus.COMPLETE]


class DataCompletenessService:
    """
    Service for checking employee data completeness for statutory exports.

    Validates that employees have all required fields for:
    - PAYE: TIN
    - Pension: RSA PIN, PFA Code
    - NHF: NHF Number
    - Bank Upload: Bank account number, Bank name (resolvable to code)
    """

    def __init__(self, db: Session):
        self.db = db
        self.bank_directory = BankDirectoryService(db)

    def generate_report(
        self,
        organization_id: UUID,
        *,
        department_id: UUID | None = None,
        designation_id: UUID | None = None,
        include_complete: bool = True,
        export_types: list[ExportType] | None = None,
    ) -> CompletenessReportResult:
        """
        Generate data completeness report for employees.

        Args:
            organization_id: Organization ID
            department_id: Optional department filter
            designation_id: Optional designation filter
            include_complete: Whether to include employees with complete data
            export_types: Specific export types to check (default: all)

        Returns:
            CompletenessReportResult with employee-level details
        """
        today = date.today()
        check_types = export_types or list(ExportType)

        # Get employees with active salary structure assignments
        employees = self._get_assigned_employees(
            organization_id, department_id, designation_id
        )

        # Get current tax profiles for all employees
        tax_profiles = self._get_current_tax_profiles(
            organization_id, [e.employee_id for e in employees], today
        )

        # Build lookup dict
        profile_by_employee: dict[UUID, EmployeeTaxProfile] = {
            p.employee_id: p for p in tax_profiles
        }

        results: list[EmployeeCompleteness] = []
        complete_count = 0
        incomplete_count = 0
        missing_profile_count = 0
        paye_ready = 0
        pension_ready = 0
        nhf_ready = 0
        bank_ready = 0

        for employee in employees:
            completeness = self._check_employee_completeness(
                employee, profile_by_employee.get(employee.employee_id), check_types
            )
            results.append(completeness)

            # Count by status
            if completeness.status == CompletenessStatus.COMPLETE:
                complete_count += 1
            elif completeness.status == CompletenessStatus.MISSING_PROFILE:
                missing_profile_count += 1
                incomplete_count += 1
            else:
                incomplete_count += 1

            # Count by export readiness
            if completeness.is_paye_ready:
                paye_ready += 1
            if completeness.is_pension_ready:
                pension_ready += 1
            if completeness.is_nhf_ready:
                nhf_ready += 1
            if completeness.is_bank_ready:
                bank_ready += 1

        # Filter out complete if not requested
        if not include_complete:
            results = [e for e in results if e.status != CompletenessStatus.COMPLETE]

        return CompletenessReportResult(
            organization_id=organization_id,
            report_date=today,
            total_employees=len(employees),
            complete_count=complete_count,
            incomplete_count=incomplete_count,
            missing_profile_count=missing_profile_count,
            employees=results,
            paye_ready_count=paye_ready,
            pension_ready_count=pension_ready,
            nhf_ready_count=nhf_ready,
            bank_ready_count=bank_ready,
        )

    def _get_assigned_employees(
        self,
        organization_id: UUID,
        department_id: UUID | None,
        designation_id: UUID | None,
    ) -> list[Employee]:
        """Get employees with active salary structure assignments."""
        today = date.today()

        # Subquery for employees with active assignments
        assignment_subq = (
            select(SalaryStructureAssignment.employee_id)
            .where(
                SalaryStructureAssignment.organization_id == organization_id,
                SalaryStructureAssignment.from_date <= today,
                or_(
                    SalaryStructureAssignment.to_date.is_(None),
                    SalaryStructureAssignment.to_date >= today,
                ),
            )
            .distinct()
        )

        stmt = (
            select(Employee)
            .options(joinedload(Employee.department), joinedload(Employee.designation))
            .where(
                Employee.organization_id == organization_id,
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.employee_id.in_(assignment_subq),
            )
            .join(Person, Employee.person_id == Person.id)
            .order_by(Person.display_name, Person.last_name, Person.first_name)
        )

        if department_id:
            stmt = stmt.where(Employee.department_id == department_id)
        if designation_id:
            stmt = stmt.where(Employee.designation_id == designation_id)

        return list(self.db.scalars(stmt).unique().all())

    def _get_current_tax_profiles(
        self,
        organization_id: UUID,
        employee_ids: list[UUID],
        as_of: date,
    ) -> list[EmployeeTaxProfile]:
        """Get current tax profiles for employees."""
        if not employee_ids:
            return []

        stmt = select(EmployeeTaxProfile).where(
            EmployeeTaxProfile.organization_id == organization_id,
            EmployeeTaxProfile.employee_id.in_(employee_ids),
            EmployeeTaxProfile.effective_from <= as_of,
            or_(
                EmployeeTaxProfile.effective_to.is_(None),
                EmployeeTaxProfile.effective_to >= as_of,
            ),
        )

        return list(self.db.scalars(stmt).all())

    def _check_employee_completeness(
        self,
        employee: Employee,
        tax_profile: EmployeeTaxProfile | None,
        export_types: list[ExportType],
    ) -> EmployeeCompleteness:
        """Check data completeness for a single employee."""
        missing_fields: list[MissingField] = []

        # Check if tax profile exists
        has_tax_profile = tax_profile is not None

        # Check bank details
        has_bank_details = bool(employee.bank_account_number and employee.bank_name)
        bank_code_resolvable = False
        if employee.bank_name:
            code = self.bank_directory.lookup_bank_code(employee.bank_name)
            bank_code_resolvable = code is not None

        # PAYE requirements
        if ExportType.PAYE in export_types:
            if not tax_profile or not tax_profile.tin:
                missing_fields.append(
                    MissingField(
                        field_name="tin",
                        field_label="Tax Identification Number (TIN)",
                        export_types=[ExportType.PAYE],
                        severity="required",
                    )
                )

        # Pension requirements
        if ExportType.PENSION in export_types:
            if not tax_profile or not tax_profile.rsa_pin:
                missing_fields.append(
                    MissingField(
                        field_name="rsa_pin",
                        field_label="RSA PIN",
                        export_types=[ExportType.PENSION],
                        severity="required",
                    )
                )
            if not tax_profile or not tax_profile.pfa_code:
                missing_fields.append(
                    MissingField(
                        field_name="pfa_code",
                        field_label="PFA Code",
                        export_types=[ExportType.PENSION],
                        severity="required",
                    )
                )

        # NHF requirements
        if ExportType.NHF in export_types:
            if not tax_profile or not tax_profile.nhf_number:
                missing_fields.append(
                    MissingField(
                        field_name="nhf_number",
                        field_label="NHF Number",
                        export_types=[ExportType.NHF],
                        severity="required",
                    )
                )

        # Bank Upload requirements
        if ExportType.BANK_UPLOAD in export_types:
            if not employee.bank_account_number:
                missing_fields.append(
                    MissingField(
                        field_name="bank_account_number",
                        field_label="Bank Account Number",
                        export_types=[ExportType.BANK_UPLOAD],
                        severity="required",
                    )
                )
            if not employee.bank_name:
                missing_fields.append(
                    MissingField(
                        field_name="bank_name",
                        field_label="Bank Name",
                        export_types=[ExportType.BANK_UPLOAD],
                        severity="required",
                    )
                )
            elif not bank_code_resolvable:
                missing_fields.append(
                    MissingField(
                        field_name="bank_code",
                        field_label="Bank Code (unrecognized bank name)",
                        export_types=[ExportType.BANK_UPLOAD],
                        severity="required",
                    )
                )

        # Determine status
        if not has_tax_profile and any(
            et in export_types
            for et in [ExportType.PAYE, ExportType.PENSION, ExportType.NHF]
        ):
            status = CompletenessStatus.MISSING_PROFILE
        elif missing_fields:
            status = CompletenessStatus.INCOMPLETE
        else:
            status = CompletenessStatus.COMPLETE

        dept_name = None
        if employee.department:
            dept_name = employee.department.department_name

        return EmployeeCompleteness(
            employee_id=employee.employee_id,
            employee_number=employee.employee_code,
            full_name=employee.full_name,
            department_name=dept_name,
            status=status,
            missing_fields=missing_fields,
            has_tax_profile=has_tax_profile,
            has_bank_details=has_bank_details,
            bank_code_resolvable=bank_code_resolvable,
        )

    def get_summary_stats(self, organization_id: UUID) -> dict:
        """
        Get quick summary statistics for data completeness.

        Returns dict with counts without full employee details.
        """
        today = date.today()

        # Count assigned employees
        assignment_subq = (
            select(SalaryStructureAssignment.employee_id)
            .where(
                SalaryStructureAssignment.organization_id == organization_id,
                SalaryStructureAssignment.from_date <= today,
                or_(
                    SalaryStructureAssignment.to_date.is_(None),
                    SalaryStructureAssignment.to_date >= today,
                ),
            )
            .distinct()
        )

        total_assigned = (
            self.db.scalar(
                select(func.count())
                .select_from(Employee)
                .where(
                    Employee.organization_id == organization_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.employee_id.in_(assignment_subq),
                )
            )
            or 0
        )

        # Count with tax profiles
        profile_subq = (
            select(EmployeeTaxProfile.employee_id)
            .where(
                EmployeeTaxProfile.organization_id == organization_id,
                EmployeeTaxProfile.effective_from <= today,
                or_(
                    EmployeeTaxProfile.effective_to.is_(None),
                    EmployeeTaxProfile.effective_to >= today,
                ),
            )
            .distinct()
        )

        with_profile = (
            self.db.scalar(
                select(func.count())
                .select_from(Employee)
                .where(
                    Employee.organization_id == organization_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.employee_id.in_(assignment_subq),
                    Employee.employee_id.in_(profile_subq),
                )
            )
            or 0
        )

        # Count with bank details
        with_bank = (
            self.db.scalar(
                select(func.count())
                .select_from(Employee)
                .where(
                    Employee.organization_id == organization_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.employee_id.in_(assignment_subq),
                    Employee.bank_account_number.isnot(None),
                    Employee.bank_name.isnot(None),
                )
            )
            or 0
        )

        return {
            "total_assigned_employees": total_assigned,
            "with_tax_profile": with_profile,
            "without_tax_profile": total_assigned - with_profile,
            "with_bank_details": with_bank,
            "without_bank_details": total_assigned - with_bank,
        }


def data_completeness_service(db: Session) -> DataCompletenessService:
    """Create a DataCompletenessService instance."""
    return DataCompletenessService(db)


# =============================================================================
# Payroll Auto-Generation Readiness
# =============================================================================


@dataclass
class PayrollReadinessIssue:
    """An issue that flags an employee for payroll review."""

    issue_type: PayrollIssueType
    message: str
    severity: str = "warning"  # "warning" or "critical"


@dataclass
class EmployeePayrollReadiness:
    """Payroll readiness for a single employee."""

    employee_id: UUID
    employee_code: str
    employee_name: str
    department_name: str | None
    is_ready: bool
    needs_review: bool
    issues: list[PayrollReadinessIssue] = field(default_factory=list)

    # Specific checks
    has_salary_assignment: bool = True
    has_bank_details: bool = True
    has_tax_profile: bool = True
    has_attendance: bool = True
    attendance_gap_days: int = 0
    is_prorated: bool = False
    proration_reason: str | None = None
    is_contract_staff: bool = False

    @property
    def review_reasons(self) -> list[str]:
        """Get human-readable review reasons."""
        return [issue.message for issue in self.issues if issue.severity == "warning"]

    @property
    def blocking_issues(self) -> list[str]:
        """Get critical issues that block payroll."""
        return [issue.message for issue in self.issues if issue.severity == "critical"]


@dataclass
class PayrollReadinessReport:
    """Overall readiness report for payroll auto-generation."""

    organization_id: UUID
    period_start: date
    period_end: date
    total_employees: int
    ready_count: int
    needs_review_count: int
    blocked_count: int
    employees: list[EmployeePayrollReadiness]

    @property
    def can_proceed(self) -> bool:
        """Can auto-generation proceed? (no critical issues)."""
        return self.blocked_count == 0

    @property
    def employees_needing_review(self) -> list[EmployeePayrollReadiness]:
        """Get employees that need review."""
        return [e for e in self.employees if e.needs_review]

    @property
    def blocked_employees(self) -> list[EmployeePayrollReadiness]:
        """Get employees with critical issues."""
        return [e for e in self.employees if not e.is_ready]


class PayrollReadinessService:
    """
    Service for checking employee readiness for payroll auto-generation.

    Validates:
    - Active salary assignment for period
    - Bank details (if salary_mode = BANK)
    - Tax profile (for PAYE calculation)
    - Attendance records (flags gaps for review)
    - Proration (new hires/exits)
    """

    def __init__(self, db: Session):
        self.db = db

    def _is_contract_staff_employee(
        self,
        employee: Employee,
        assignment: SalaryStructureAssignment | None,
    ) -> bool:
        """Check whether payroll should skip statutory profile requirements."""
        if assignment is None:
            return False

        from app.models.people.payroll.salary_structure import SalaryStructure

        structure = assignment.salary_structure
        if structure is None and assignment.structure_id:
            structure = self.db.get(SalaryStructure, assignment.structure_id)

        classification = classify_payroll_employment_type(
            self.db,
            organization_id=employee.organization_id,
            employment_type_id=employee.employment_type_id,
        )
        return classification.is_contract_staff(
            structure_name=structure.structure_name if structure else None
        )

    def check_readiness(
        self,
        organization_id: UUID,
        period_start: date,
        period_end: date,
        *,
        department_id: UUID | None = None,
        check_attendance: bool = True,
    ) -> PayrollReadinessReport:
        """
        Check payroll readiness for all eligible employees.

        Args:
            organization_id: Organization ID
            period_start: Payroll period start date
            period_end: Payroll period end date
            department_id: Optional department filter
            check_attendance: Whether to check for attendance gaps

        Returns:
            PayrollReadinessReport with employee-level details
        """

        # Get eligible employees (active with salary assignment)
        employees = self._get_eligible_employees(
            organization_id, period_start, period_end, department_id
        )

        # Get tax profiles
        tax_profiles = self._get_tax_profiles(
            organization_id, [e.employee_id for e in employees], period_end
        )
        profile_by_emp: dict[UUID, EmployeeTaxProfile] = {
            p.employee_id: p for p in tax_profiles
        }

        # Get salary assignments
        assignments = self._get_salary_assignments(
            organization_id,
            [e.employee_id for e in employees],
            period_start,
            period_end,
        )
        assignment_by_emp: dict[UUID, SalaryStructureAssignment] = {
            a.employee_id: a for a in assignments
        }

        # Get attendance summaries if checking
        attendance_by_emp: dict[UUID, dict] = {}
        if check_attendance:
            attendance_by_emp = self._get_attendance_summaries(
                [e.employee_id for e in employees], period_start, period_end
            )

        results: list[EmployeePayrollReadiness] = []
        ready_count = 0
        review_count = 0
        blocked_count = 0

        for employee in employees:
            readiness = self._check_employee_readiness(
                employee=employee,
                assignment=assignment_by_emp.get(employee.employee_id),
                tax_profile=profile_by_emp.get(employee.employee_id),
                attendance=attendance_by_emp.get(employee.employee_id),
                period_start=period_start,
                period_end=period_end,
            )
            results.append(readiness)

            if readiness.is_ready and not readiness.needs_review:
                ready_count += 1
            elif readiness.needs_review:
                review_count += 1
            else:
                blocked_count += 1

        return PayrollReadinessReport(
            organization_id=organization_id,
            period_start=period_start,
            period_end=period_end,
            total_employees=len(employees),
            ready_count=ready_count,
            needs_review_count=review_count,
            blocked_count=blocked_count,
            employees=results,
        )

    def _get_eligible_employees(
        self,
        organization_id: UUID,
        period_start: date,
        period_end: date,
        department_id: UUID | None,
    ) -> list[Employee]:
        """Get employees eligible for payroll in this period."""
        stmt = (
            select(Employee)
            .options(
                joinedload(Employee.department),
                joinedload(Employee.person),
            )
            .where(
                Employee.organization_id == organization_id,
                payroll_employee_eligibility_clause(
                    period_start=period_start,
                    period_end=period_end,
                ),
                Employee.status != EmployeeStatus.TERMINATED,
                # Joined before or during period
                Employee.date_of_joining <= period_end,
                # Either no leaving date, or leaving date is within/after period start
                or_(
                    Employee.date_of_leaving.is_(None),
                    Employee.date_of_leaving >= period_start,
                ),
            )
        )

        if department_id:
            stmt = stmt.where(Employee.department_id == department_id)

        return list(self.db.scalars(stmt).unique().all())

    def _get_tax_profiles(
        self,
        organization_id: UUID,
        employee_ids: list[UUID],
        as_of: date,
    ) -> list[EmployeeTaxProfile]:
        """Get current tax profiles for employees."""
        if not employee_ids:
            return []

        stmt = select(EmployeeTaxProfile).where(
            EmployeeTaxProfile.organization_id == organization_id,
            EmployeeTaxProfile.employee_id.in_(employee_ids),
            EmployeeTaxProfile.effective_from <= as_of,
            or_(
                EmployeeTaxProfile.effective_to.is_(None),
                EmployeeTaxProfile.effective_to >= as_of,
            ),
        )

        return list(self.db.scalars(stmt).all())

    def _get_salary_assignments(
        self,
        organization_id: UUID,
        employee_ids: list[UUID],
        period_start: date,
        period_end: date,
    ) -> list[SalaryStructureAssignment]:
        """Get active salary assignments for period."""
        if not employee_ids:
            return []

        stmt = select(SalaryStructureAssignment).where(
            SalaryStructureAssignment.organization_id == organization_id,
            SalaryStructureAssignment.employee_id.in_(employee_ids),
            SalaryStructureAssignment.from_date <= period_end,
            or_(
                SalaryStructureAssignment.to_date.is_(None),
                SalaryStructureAssignment.to_date >= period_start,
            ),
        )

        return list(self.db.scalars(stmt).all())

    def _get_attendance_summaries(
        self,
        employee_ids: list[UUID],
        period_start: date,
        period_end: date,
    ) -> dict[UUID, dict]:
        """Get attendance summaries for employees."""
        from app.models.people.attendance import Attendance

        if not employee_ids:
            return {}

        # Count attendance records per employee
        stmt = (
            select(
                Attendance.employee_id,
                func.count(Attendance.attendance_id).label("record_count"),
            )
            .where(
                Attendance.employee_id.in_(employee_ids),
                Attendance.attendance_date >= period_start,
                Attendance.attendance_date <= period_end,
            )
            .group_by(Attendance.employee_id)
        )

        results = self.db.execute(stmt).all()

        # Calculate expected working days (simple: weekdays)
        total_days = (period_end - period_start).days + 1
        expected_days = sum(
            1
            for i in range(total_days)
            if (period_start + timedelta(days=i)).weekday() < 5
        )

        summaries: dict[UUID, dict] = {}
        emp_ids_with_attendance = set()

        for row in results:
            emp_ids_with_attendance.add(row.employee_id)
            gap_days = max(0, expected_days - row.record_count)
            summaries[row.employee_id] = {
                "record_count": row.record_count,
                "expected_days": expected_days,
                "gap_days": gap_days,
                "has_attendance": True,
            }

        # Mark employees with no attendance
        for emp_id in employee_ids:
            if emp_id not in emp_ids_with_attendance:
                summaries[emp_id] = {
                    "record_count": 0,
                    "expected_days": expected_days,
                    "gap_days": expected_days,
                    "has_attendance": False,
                }

        return summaries

    def _check_employee_readiness(
        self,
        employee: Employee,
        assignment: SalaryStructureAssignment | None,
        tax_profile: EmployeeTaxProfile | None,
        attendance: dict | None,
        period_start: date,
        period_end: date,
    ) -> EmployeePayrollReadiness:
        """Check readiness for a single employee."""
        from app.models.people.hr.employee import SalaryMode

        issues: list[PayrollReadinessIssue] = []
        is_ready = True
        needs_review = False
        is_contract_staff = self._is_contract_staff_employee(employee, assignment)

        # Check salary assignment
        has_assignment = assignment is not None
        if not has_assignment:
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.MISSING_SALARY_ASSIGNMENT,
                    message="No active salary assignment for this period",
                    severity="critical",
                )
            )
            is_ready = False

        # Check bank details (if bank payment mode)
        has_bank = bool(employee.bank_account_number and employee.bank_name)
        if employee.salary_mode == SalaryMode.BANK and not has_bank:
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.MISSING_BANK_DETAILS,
                    message="Missing bank account details for bank transfer",
                    severity="critical",
                )
            )
            is_ready = False

        # Check tax profile
        has_tax_profile = tax_profile is not None
        if not is_contract_staff and tax_profile is None:
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.MISSING_TAX_PROFILE,
                    message="No tax profile - PAYE may not calculate accurately",
                    severity="warning",
                )
            )
            needs_review = True
        elif not is_contract_staff and tax_profile is not None and not tax_profile.tin:
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.MISSING_TIN,
                    message="Missing TIN (Tax Identification Number)",
                    severity="warning",
                )
            )
            needs_review = True

        # Check attendance
        has_attendance = True
        attendance_gap_days = 0
        if attendance:
            has_attendance = attendance.get("has_attendance", False)
            attendance_gap_days = attendance.get("gap_days", 0)

            if not has_attendance:
                issues.append(
                    PayrollReadinessIssue(
                        issue_type=PayrollIssueType.NO_ATTENDANCE_RECORDS,
                        message="No attendance records - will assume full attendance",
                        severity="warning",
                    )
                )
                needs_review = True
            elif attendance_gap_days > 0:
                issues.append(
                    PayrollReadinessIssue(
                        issue_type=PayrollIssueType.ATTENDANCE_GAPS,
                        message=f"{attendance_gap_days} days without attendance records",
                        severity="warning",
                    )
                )
                needs_review = True

        # Check proration
        is_prorated = False
        proration_reason = None

        if employee.date_of_joining and employee.date_of_joining > period_start:
            is_prorated = True
            proration_reason = "new_hire"
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.NEW_HIRE_PRORATION,
                    message=f"New hire - joined {employee.date_of_joining}, salary will be prorated",
                    severity="warning",
                )
            )
            needs_review = True

        if (
            employee.date_of_leaving
            and period_start <= employee.date_of_leaving < period_end
        ):
            is_prorated = True
            proration_reason = "exit" if not proration_reason else "both"
            issues.append(
                PayrollReadinessIssue(
                    issue_type=PayrollIssueType.EXIT_PRORATION,
                    message=f"Exiting employee - leaving {employee.date_of_leaving}, salary will be prorated",
                    severity="warning",
                )
            )
            needs_review = True

        dept_name = None
        if employee.department:
            dept_name = employee.department.department_name

        return EmployeePayrollReadiness(
            employee_id=employee.employee_id,
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            department_name=dept_name,
            is_ready=is_ready,
            needs_review=needs_review,
            issues=issues,
            has_salary_assignment=has_assignment,
            has_bank_details=has_bank,
            has_tax_profile=has_tax_profile,
            has_attendance=has_attendance,
            attendance_gap_days=attendance_gap_days,
            is_prorated=is_prorated,
            proration_reason=proration_reason,
            is_contract_staff=is_contract_staff,
        )


def payroll_readiness_service(db: Session) -> PayrollReadinessService:
    """Create a PayrollReadinessService instance."""
    return PayrollReadinessService(db)
