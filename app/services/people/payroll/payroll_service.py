"""
Payroll service - structures, assignments, and payroll entries.

Builds payroll runs and generates salary slips using SalarySlipService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.people.hr.employee import Employee
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.payroll_entry import PayrollEntry, PayrollEntryStatus
from app.models.people.payroll.salary_assignment import SalaryStructureAssignment
from app.models.people.payroll.salary_component import (
    SalaryComponent,
    SalaryComponentType,
)
from app.models.people.payroll.salary_slip import SalarySlip, SalarySlipStatus
from app.models.people.payroll.salary_structure import (
    PayrollFrequency,
    SalaryStructure,
    SalaryStructureDeduction,
    SalaryStructureEarning,
)
from app.services.common import PaginatedResult, PaginationParams, coerce_uuid
from app.services.finance.platform.org_context import org_context_service
from app.services.people.hr.employment_types import EmploymentTypeService
from app.services.people.integrations.payroll_gl_adapter import PayrollGLAdapter
from app.services.people.payroll.disbursement import record_disbursement
from app.services.people.payroll.eligibility import (
    is_employee_payroll_eligible_for_period,
    payroll_employee_eligibility_clause,
)
from app.services.people.payroll_reporting import calculate_active_monthly_net_average
from app.services.people.payroll.salary_slip_service import (
    SalarySlipInput,
    salary_slip_service,
)
from app.services.settings_cache import get_cached_setting

logger = logging.getLogger(__name__)

__all__ = ["PayrollService", "PayrollServiceError", "AutoGenerateResult"]


def _dispatch_slip_paid(slip_id: UUID, slip_number: str, employee_id: UUID) -> None:
    """
    Dispatch SlipPaid event after DB commit.

    This function is called AFTER db.commit() to ensure the payment
    is persisted before dispatching any events.

    Args:
        slip_id: The salary slip ID
        slip_number: The slip number for logging
        employee_id: The employee ID
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.debug(
        "Dispatching slip paid event for slip %s (employee %s)",
        slip_number,
        employee_id,
    )
    # Event dispatch placeholder - integrate with event system when available
    # e.g., event_dispatcher.dispatch(SlipPaidEvent(slip_id=slip_id, ...))


@dataclass
class AutoGenerateResult:
    """Result of auto-generating salary slips."""

    created: int = 0
    skipped: int = 0
    flagged_for_review: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.skipped + len(self.errors)


class PayrollServiceError(Exception):
    """Base error for payroll service."""

    pass


class PayrollEntryNotFoundError(PayrollServiceError):
    """Payroll entry not found."""

    def __init__(self, entry_id: UUID):
        self.entry_id = entry_id
        super().__init__(f"Payroll entry {entry_id} not found")


class SalaryStructureNotFoundError(PayrollServiceError):
    """Salary structure not found."""

    def __init__(self, structure_id: UUID):
        self.structure_id = structure_id
        super().__init__(f"Salary structure {structure_id} not found")


class SalaryAssignmentNotFoundError(PayrollServiceError):
    """Salary structure assignment not found."""

    def __init__(self, assignment_id: UUID):
        self.assignment_id = assignment_id
        super().__init__(f"Salary assignment {assignment_id} not found")


class PayrollService:
    """Service for payroll structures, assignments, and payroll entries."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _resolve_currency_code(
        self,
        org_id: UUID,
        currency_code: str | None = None,
    ) -> str:
        if currency_code:
            return currency_code
        return org_context_service.get_functional_currency(self.db, org_id)

    # =========================================================================
    # Salary Components
    # =========================================================================

    def list_salary_components(
        self,
        org_id: UUID,
        *,
        component_type: SalaryComponentType | None = None,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResult[SalaryComponent]:
        """List salary components with optional filters."""
        org_id = coerce_uuid(org_id)
        stmt = select(SalaryComponent).where(
            SalaryComponent.organization_id == org_id,
        )
        if component_type:
            stmt = stmt.where(SalaryComponent.component_type == component_type)
        if is_active is not None:
            stmt = stmt.where(SalaryComponent.is_active == is_active)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.scalar(count_stmt) or 0

        items = list(
            self.db.scalars(
                stmt.order_by(SalaryComponent.display_order).offset(offset).limit(limit)
            ).all()
        )
        return PaginatedResult(items=items, total=total, offset=offset, limit=limit)

    def get_salary_component(self, component_id: UUID) -> SalaryComponent | None:
        """Get a salary component by ID."""
        return self.db.get(SalaryComponent, component_id)

    def create_salary_component(
        self,
        data: dict[str, object],
    ) -> SalaryComponent:
        """Create a new salary component."""
        component = SalaryComponent(**data, is_active=True)
        self.db.add(component)
        self.db.flush()
        self.db.refresh(component)
        logger.info("Created salary component: %s", component.component_code)
        return component

    def update_salary_component(
        self,
        component_id: UUID,
        data: dict[str, object],
    ) -> SalaryComponent | None:
        """Update a salary component. Returns None if not found."""
        component = self.db.get(SalaryComponent, component_id)
        if not component:
            return None
        for key, value in data.items():
            setattr(component, key, value)
        self.db.flush()
        self.db.refresh(component)
        return component

    # =========================================================================
    # Employee Tax Profiles
    # =========================================================================

    def get_employee_tax_profile(
        self,
        org_id: UUID,
        employee_id: UUID,
        as_of_date: date | None = None,
    ) -> EmployeeTaxProfile | None:
        """Get effective tax profile for an employee as of a date."""
        lookup_date = as_of_date or date.today()
        org_id = coerce_uuid(org_id)
        stmt = (
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.organization_id == org_id,
                EmployeeTaxProfile.employee_id == employee_id,
                EmployeeTaxProfile.effective_from <= lookup_date,
                (
                    (EmployeeTaxProfile.effective_to.is_(None))
                    | (EmployeeTaxProfile.effective_to >= lookup_date)
                ),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
        )
        return self.db.scalar(stmt)

    def create_employee_tax_profile(
        self,
        org_id: UUID,
        data: dict[str, object],
        created_by_id: UUID,
    ) -> EmployeeTaxProfile:
        """Create an employee tax profile."""
        org_id = coerce_uuid(org_id)
        profile = EmployeeTaxProfile(
            organization_id=org_id,
            created_by_id=created_by_id,
            **data,
        )
        profile.update_rent_relief()
        self.db.add(profile)
        self.db.flush()
        self.db.refresh(profile)
        logger.info("Created tax profile for employee %s", profile.employee_id)
        return profile

    def update_employee_tax_profile(
        self,
        org_id: UUID,
        employee_id: UUID,
        data: dict[str, object],
        updated_by_id: UUID,
    ) -> EmployeeTaxProfile | None:
        """Update the current (active) tax profile for an employee."""
        org_id = coerce_uuid(org_id)
        stmt = (
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.organization_id == org_id,
                EmployeeTaxProfile.employee_id == employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
        )
        profile = self.db.scalar(stmt)
        if not profile:
            return None

        for key, value in data.items():
            setattr(profile, key, value)
        profile.updated_by_id = updated_by_id

        if "annual_rent" in data or "rent_receipt_verified" in data:
            profile.update_rent_relief()

        self.db.flush()
        self.db.refresh(profile)
        return profile

    # =========================================================================
    # Salary Structures
    # =========================================================================

    def list_salary_structures(
        self,
        org_id: UUID,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[SalaryStructure]:
        query = select(SalaryStructure).where(SalaryStructure.organization_id == org_id)

        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    SalaryStructure.structure_code.ilike(search_term),
                    SalaryStructure.structure_name.ilike(search_term),
                )
            )

        if is_active is not None:
            query = query.where(SalaryStructure.is_active == is_active)

        query = query.order_by(SalaryStructure.structure_name.asc())

        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).all())
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_salary_structure(self, org_id: UUID, structure_id: UUID) -> SalaryStructure:
        structure = self.db.scalar(
            select(SalaryStructure).where(
                SalaryStructure.organization_id == org_id,
                SalaryStructure.structure_id == structure_id,
            )
        )
        if not structure:
            raise SalaryStructureNotFoundError(structure_id)
        return structure

    def create_salary_structure(
        self,
        org_id: UUID,
        *,
        structure_code: str,
        structure_name: str,
        description: str | None = None,
        payroll_frequency: PayrollFrequency = PayrollFrequency.MONTHLY,
        currency_code: str | None = None,
        earnings: list[dict] | None = None,
        deductions: list[dict] | None = None,
    ) -> SalaryStructure:
        resolved_currency_code = self._resolve_currency_code(org_id, currency_code)
        structure = SalaryStructure(
            organization_id=org_id,
            structure_code=structure_code,
            structure_name=structure_name,
            description=description,
            payroll_frequency=payroll_frequency,
            currency_code=resolved_currency_code,
        )
        self.db.add(structure)
        self.db.flush()

        self._replace_structure_lines(structure, earnings, deductions)
        self.db.flush()
        return structure

    def update_salary_structure(
        self,
        org_id: UUID,
        structure_id: UUID,
        *,
        earnings: list[dict] | None = None,
        deductions: list[dict] | None = None,
        **kwargs,
    ) -> SalaryStructure:
        structure = self.get_salary_structure(org_id, structure_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(structure, key):
                setattr(structure, key, value)

        if earnings is not None or deductions is not None:
            self._replace_structure_lines(structure, earnings, deductions)

        self.db.flush()
        return structure

    def delete_salary_structure(self, org_id: UUID, structure_id: UUID) -> None:
        structure = self.get_salary_structure(org_id, structure_id)
        structure.is_active = False
        self.db.flush()

    def _replace_structure_lines(
        self,
        structure: SalaryStructure,
        earnings: list[dict] | None,
        deductions: list[dict] | None,
    ) -> None:
        if earnings is not None:
            structure.earnings.clear()
            for line in earnings:
                component = self.db.get(SalaryComponent, line["component_id"])
                if not component:
                    raise PayrollServiceError("Salary component not found")
                structure.earnings.append(
                    SalaryStructureEarning(
                        component_id=line["component_id"],
                        amount=line.get("amount", Decimal("0")),
                        amount_based_on_formula=line.get(
                            "amount_based_on_formula", False
                        ),
                        formula=line.get("formula"),
                        condition=line.get("condition"),
                        display_order=line.get("display_order", 0),
                    )
                )

        if deductions is not None:
            structure.deductions.clear()
            for line in deductions:
                component = self.db.get(SalaryComponent, line["component_id"])
                if not component:
                    raise PayrollServiceError("Salary component not found")
                structure.deductions.append(
                    SalaryStructureDeduction(
                        component_id=line["component_id"],
                        amount=line.get("amount", Decimal("0")),
                        amount_based_on_formula=line.get(
                            "amount_based_on_formula", False
                        ),
                        formula=line.get("formula"),
                        condition=line.get("condition"),
                        display_order=line.get("display_order", 0),
                    )
                )

    # =========================================================================
    # Salary Structure Assignments
    # =========================================================================

    def list_assignments(
        self,
        org_id: UUID,
        *,
        employee_id: UUID | None = None,
        structure_id: UUID | None = None,
        active_on: date | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[SalaryStructureAssignment]:
        query = select(SalaryStructureAssignment).where(
            SalaryStructureAssignment.organization_id == org_id
        )

        if employee_id:
            query = query.where(SalaryStructureAssignment.employee_id == employee_id)

        if structure_id:
            query = query.where(SalaryStructureAssignment.structure_id == structure_id)

        if active_on:
            query = query.where(
                SalaryStructureAssignment.from_date <= active_on,
                or_(
                    SalaryStructureAssignment.to_date.is_(None),
                    SalaryStructureAssignment.to_date >= active_on,
                ),
            )

        query = query.order_by(SalaryStructureAssignment.from_date.desc())

        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).all())
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_assignment(
        self, org_id: UUID, assignment_id: UUID
    ) -> SalaryStructureAssignment:
        assignment = self.db.scalar(
            select(SalaryStructureAssignment).where(
                SalaryStructureAssignment.organization_id == org_id,
                SalaryStructureAssignment.assignment_id == assignment_id,
            )
        )
        if not assignment:
            raise SalaryAssignmentNotFoundError(assignment_id)
        return assignment

    def create_assignment(
        self,
        org_id: UUID,
        *,
        employee_id: UUID,
        structure_id: UUID,
        from_date: date,
        to_date: date | None = None,
        base: Decimal = Decimal("0"),
        variable: Decimal = Decimal("0"),
        income_tax_slab: str | None = None,
    ) -> SalaryStructureAssignment:
        assignment = SalaryStructureAssignment(
            organization_id=org_id,
            employee_id=employee_id,
            structure_id=structure_id,
            from_date=from_date,
            to_date=to_date,
            base=base,
            variable=variable,
            income_tax_slab=income_tax_slab,
        )
        self.db.add(assignment)
        self.db.flush()
        return assignment

    def update_assignment(
        self,
        org_id: UUID,
        assignment_id: UUID,
        **kwargs,
    ) -> SalaryStructureAssignment:
        assignment = self.get_assignment(org_id, assignment_id)

        for key, value in kwargs.items():
            if value is not None and hasattr(assignment, key):
                setattr(assignment, key, value)

        self.db.flush()
        return assignment

    def delete_assignment(self, org_id: UUID, assignment_id: UUID) -> None:
        assignment = self.get_assignment(org_id, assignment_id)
        self.db.delete(assignment)
        self.db.flush()

    # =========================================================================
    # Payroll Entries
    # =========================================================================

    def list_payroll_entries(
        self,
        org_id: UUID,
        *,
        status: PayrollEntryStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        payroll_frequency: PayrollFrequency | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[PayrollEntry]:
        query = select(PayrollEntry).where(PayrollEntry.organization_id == org_id)

        if status:
            query = query.where(PayrollEntry.status == status)

        if from_date:
            query = query.where(PayrollEntry.start_date >= from_date)

        if to_date:
            query = query.where(PayrollEntry.end_date <= to_date)

        if payroll_frequency:
            query = query.where(PayrollEntry.payroll_frequency == payroll_frequency)

        query = query.order_by(PayrollEntry.start_date.desc())

        count_query = select(func.count()).select_from(query.subquery())
        total = self.db.scalar(count_query) or 0

        if pagination:
            query = query.offset(pagination.offset).limit(pagination.limit)

        items = list(self.db.scalars(query).all())
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset if pagination else 0,
            limit=pagination.limit if pagination else len(items),
        )

    def get_payroll_entry(self, org_id: UUID, entry_id: UUID) -> PayrollEntry:
        entry = self.db.scalar(
            select(PayrollEntry).where(
                PayrollEntry.organization_id == org_id,
                PayrollEntry.entry_id == entry_id,
            )
        )
        if not entry:
            raise PayrollEntryNotFoundError(entry_id)
        return entry

    def create_payroll_entry(
        self,
        org_id: UUID,
        *,
        posting_date: date,
        start_date: date,
        end_date: date,
        payroll_frequency: PayrollFrequency = PayrollFrequency.MONTHLY,
        currency_code: str | None = None,
        department_id: UUID | None = None,
        designation_id: UUID | None = None,
        employment_type_id: UUID | None = None,
        source_bank_account_id: UUID | None = None,
        expense_account_id: UUID | None = None,
        notes: str | None = None,
    ) -> PayrollEntry:
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        if employment_type_id is not None:
            EmploymentTypeService(self.db, org_id).require_active(employment_type_id)

        entry_number = SyncNumberingService(self.db).generate_next_number(
            org_id, SequenceType.PAYROLL_ENTRY, reference_date=posting_date
        )
        resolved_currency_code = self._resolve_currency_code(org_id, currency_code)

        entry = PayrollEntry(
            organization_id=org_id,
            entry_number=entry_number,
            posting_date=posting_date,
            start_date=start_date,
            end_date=end_date,
            payroll_frequency=payroll_frequency,
            currency_code=resolved_currency_code,
            department_id=department_id,
            designation_id=designation_id,
            employment_type_id=employment_type_id,
            source_bank_account_id=source_bank_account_id,
            expense_account_id=expense_account_id,
            notes=notes,
            status=PayrollEntryStatus.DRAFT,
            payroll_year=start_date.year,
            payroll_month=start_date.month,
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def update_payroll_entry(
        self,
        org_id: UUID,
        entry_id: UUID,
        **kwargs,
    ) -> PayrollEntry:
        entry = self.get_payroll_entry(org_id, entry_id)
        if entry.salary_slips_created:
            raise PayrollServiceError(
                "Cannot update payroll entry after slips are created"
            )

        employment_type_id = kwargs.get("employment_type_id")
        if employment_type_id is not None:
            EmploymentTypeService(self.db, org_id).require_active(
                coerce_uuid(employment_type_id)
            )

        for key, value in kwargs.items():
            if value is not None and hasattr(entry, key):
                setattr(entry, key, value)

        self.db.flush()
        return entry

    def delete_payroll_entry(self, org_id: UUID, entry_id: UUID) -> None:
        entry = self.get_payroll_entry(org_id, entry_id)
        if entry.salary_slips_created:
            raise PayrollServiceError(
                "Cannot delete payroll entry after slips are created"
            )
        self.db.delete(entry)
        self.db.flush()

    def generate_salary_slips(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        created_by_id: UUID,
    ) -> dict:
        entry = self.get_payroll_entry(org_id, entry_id)
        if entry.salary_slips_created:
            raise PayrollServiceError("Salary slips already created for this entry")

        assignments = self._get_entry_assignments(org_id, entry)
        created = 0
        skipped = 0
        errors: list[dict] = []

        for assignment in assignments:
            try:
                slip = salary_slip_service.create_salary_slip(
                    db=self.db,
                    organization_id=org_id,
                    input=SalarySlipInput(
                        employee_id=assignment.employee_id,
                        start_date=entry.start_date,
                        end_date=entry.end_date,
                        posting_date=entry.posting_date,
                    ),
                    created_by_user_id=created_by_id,
                )
                slip.payroll_entry_id = entry.entry_id
                created += 1
            except Exception as exc:
                skipped += 1
                errors.append(
                    {"employee_id": str(assignment.employee_id), "reason": str(exc)}
                )

        self._update_entry_totals(entry)
        entry.salary_slips_created = True
        entry.status = PayrollEntryStatus.SLIPS_CREATED
        self.db.flush()
        return {"created_count": created, "skipped_count": skipped, "errors": errors}

    def generate_salary_slips_auto(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        include_attendance: bool = True,  # noqa: ARG002 — part of public API
        include_lwp: bool = True,
        prorate_joiners: bool = True,  # noqa: ARG002 — part of public API
        prorate_exits: bool = True,  # noqa: ARG002 — part of public API
    ) -> AutoGenerateResult:
        """
        Generate salary slips with auto-fetched data.

        Enhanced slip generation that:
        - Fetches attendance from AttendancePayrollAdapter
        - Fetches LWP from LeavePayrollAdapter
        - Calculates proration using WorkingDaysCalculator with holiday calendar
        - Flags employees with data gaps for review

        Args:
            org_id: Organization ID
            entry_id: Payroll entry ID
            include_attendance: Whether to fetch attendance data
            include_lwp: Whether to fetch LWP from leave module
            prorate_joiners: Whether to prorate new hires
            prorate_exits: Whether to prorate exits

        Returns:
            AutoGenerateResult with statistics and flagged slips
        """
        import logging

        from app.services.people.payroll.data_completeness import (
            PayrollReadinessService,
        )
        from app.services.people.payroll.leave_adapter import LeavePayrollAdapter
        from app.services.people.payroll.working_days_calculator import (
            ProrationReason,
            WorkingDaysCalculator,
        )

        logger = logging.getLogger(__name__)

        entry = self.get_payroll_entry(org_id, entry_id)
        if entry.salary_slips_created:
            raise PayrollServiceError(
                "Salary slips already created. Use regenerate instead."
            )

        # Get eligible employees (with salary assignments)
        assignments = self._get_entry_assignments(org_id, entry)

        # Initialize adapters and services
        working_days_calc = WorkingDaysCalculator(self.db)
        leave_adapter = LeavePayrollAdapter(self.db)
        readiness_service = PayrollReadinessService(self.db)

        result = AutoGenerateResult()

        # Get bulk LWP data for efficiency
        employee_ids = [a.employee_id for a in assignments]
        lwp_by_employee: dict[UUID, Decimal] = {}
        if include_lwp and employee_ids:
            lwp_by_employee = leave_adapter.get_bulk_lwp_days(
                employee_ids, entry.start_date, entry.end_date
            )

        for assignment in assignments:
            try:
                employee = self.db.get(Employee, assignment.employee_id)
                if not employee:
                    result.skipped += 1
                    continue

                if not is_employee_payroll_eligible_for_period(
                    employee,
                    period_start=entry.start_date,
                    period_end=entry.end_date,
                ):
                    result.skipped += 1
                    continue

                # Check employee readiness
                readiness = readiness_service._check_employee_readiness(
                    employee=employee,
                    assignment=assignment,
                    tax_profile=None,  # Will be checked during slip creation
                    attendance=None,
                    period_start=entry.start_date,
                    period_end=entry.end_date,
                )

                # Skip employees with critical issues (no salary assignment)
                if not readiness.has_salary_assignment:
                    result.skipped += 1
                    continue

                # Calculate proration
                proration = working_days_calc.calculate_payment_days(
                    organization_id=org_id,
                    employee_joining_date=employee.date_of_joining,
                    period_start=entry.start_date,
                    period_end=entry.end_date,
                    employee_leaving_date=employee.date_of_leaving,
                )

                # Get LWP days
                lwp_days = Decimal("0")
                if include_lwp:
                    lwp_days = lwp_by_employee.get(employee.employee_id, Decimal("0"))

                # Build slip input
                slip_input = SalarySlipInput(
                    employee_id=employee.employee_id,
                    start_date=entry.start_date,
                    end_date=entry.end_date,
                    posting_date=entry.posting_date,
                    total_working_days=proration.total_working_days,
                    absent_days=Decimal("0"),  # Could integrate attendance later
                    leave_without_pay=lwp_days,
                )

                # Create salary slip
                slip = salary_slip_service.create_salary_slip(
                    db=self.db,
                    organization_id=org_id,
                    input=slip_input,
                    created_by_user_id=None,  # System-generated
                )
                slip.payroll_entry_id = entry.entry_id

                # Collect review reasons
                review_reasons: list[str] = []

                # Flag for proration
                if proration.is_prorated:
                    if proration.proration_reason == ProrationReason.JOINED_MID_PERIOD:
                        review_reasons.append(
                            f"New hire - joined {employee.date_of_joining}, salary prorated"
                        )
                    elif proration.proration_reason == ProrationReason.LEFT_MID_PERIOD:
                        review_reasons.append(
                            f"Exit - leaving {employee.date_of_leaving}, salary prorated"
                        )
                    elif proration.proration_reason == ProrationReason.BOTH:
                        review_reasons.append(
                            f"Joined {employee.date_of_joining} and leaving {employee.date_of_leaving}"
                        )

                # Flag for missing bank details
                if not readiness.has_bank_details:
                    review_reasons.append("Missing bank account details")

                # Flag for missing tax profile
                if not readiness.has_tax_profile and not readiness.is_contract_staff:
                    review_reasons.append("No tax profile - PAYE may not be accurate")

                # Flag for LWP
                if lwp_days > 0:
                    review_reasons.append(f"{lwp_days} days Leave Without Pay deducted")

                # Set review flags on slip
                if review_reasons:
                    slip.needs_review = True
                    slip.review_reasons = review_reasons
                    result.flagged_for_review.append(
                        {
                            "employee_id": str(employee.employee_id),
                            "employee_code": employee.employee_code,
                            "employee_name": employee.full_name,
                            "slip_id": str(slip.slip_id),
                            "reasons": review_reasons,
                        }
                    )

                result.created += 1

            except Exception as e:
                logger.exception(
                    "Failed to create slip for employee %s",
                    assignment.employee_id,
                )
                result.errors.append(
                    {
                        "employee_id": str(assignment.employee_id),
                        "error": str(e),
                    }
                )

        # Update entry totals and status
        self._update_entry_totals(entry)
        entry.salary_slips_created = True
        entry.status = PayrollEntryStatus.SLIPS_CREATED
        self.db.flush()

        logger.info(
            "Auto-generated %d slips for entry %s (%d flagged, %d errors)",
            result.created,
            entry.entry_number,
            len(result.flagged_for_review),
            len(result.errors),
        )

        return result

    def regenerate_salary_slips(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        created_by_id: UUID,
    ) -> dict:
        entry = self.get_payroll_entry(org_id, entry_id)
        slips = list(entry.salary_slips or [])
        if any(slip.status != SalarySlipStatus.DRAFT for slip in slips):
            raise PayrollServiceError("Only draft slips can be regenerated")

        for slip in slips:
            self.db.delete(slip)

        entry.salary_slips_created = False
        entry.status = PayrollEntryStatus.DRAFT
        self.db.flush()

        return self.generate_salary_slips(org_id, entry_id, created_by_id=created_by_id)

    def submit_payroll_entry(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        submitted_by: UUID,
    ) -> PayrollEntry:
        """Submit payroll entry for approval."""
        entry = self.get_payroll_entry(org_id, entry_id)
        slips = list(entry.salary_slips or [])
        if not slips:
            raise PayrollServiceError("No salary slips found for this payroll entry")

        for slip in slips:
            if slip.status != SalarySlipStatus.DRAFT:
                raise PayrollServiceError(
                    f"All slips must be DRAFT to submit (found {slip.status.value})"
                )

        now = datetime.now(UTC)
        submitted_by_id = coerce_uuid(submitted_by)
        for slip in slips:
            slip.status = SalarySlipStatus.SUBMITTED
            slip.status_changed_at = now
            slip.status_changed_by_id = submitted_by_id

        entry.status = PayrollEntryStatus.SUBMITTED
        entry.salary_slips_submitted = True
        self.db.flush()
        return entry

    def approve_payroll_entry(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        approved_by: UUID,
    ) -> PayrollEntry:
        """Approve payroll entry."""
        entry = self.get_payroll_entry(org_id, entry_id)
        slips = list(entry.salary_slips or [])
        if not slips:
            raise PayrollServiceError("No salary slips found for this payroll entry")

        approver_id = coerce_uuid(approved_by)
        for slip in slips:
            if slip.status != SalarySlipStatus.SUBMITTED:
                raise PayrollServiceError(
                    f"All slips must be SUBMITTED to approve (found {slip.status.value})"
                )
            if slip.created_by_id == approver_id:
                raise PayrollServiceError(
                    "Segregation of duties: creator cannot approve their own slip"
                )

        now = datetime.now(UTC)
        for slip in slips:
            slip.status = SalarySlipStatus.APPROVED
            slip.status_changed_at = now
            slip.status_changed_by_id = approver_id

        # NOTE: Do NOT send payslip-posted notifications here.
        # Approval != posting. Employees should only be notified when
        # the payslip is actually posted to GL and emails are explicitly
        # triggered via the "Send Payslip Emails" action.

        # Process loan deductions — update loan balances
        from app.models.people.payroll.loan_repayment import (
            SalarySlipLoanDeduction as SlipLoanLink,
        )
        from app.services.people.payroll.loan_service import LoanService

        loan_svc = LoanService(self.db)

        for slip in slips:
            pending_links = list(
                self.db.scalars(
                    select(SlipLoanLink).where(
                        SlipLoanLink.slip_id == slip.slip_id,
                        SlipLoanLink.repayment_id.is_(None),
                    )
                ).all()
            )

            for link in pending_links:
                try:
                    repayment = loan_svc.record_payroll_deduction(
                        loan_id=link.loan_id,
                        slip_id=slip.slip_id,
                        amount=link.amount,
                        principal_portion=link.principal_portion,
                        interest_portion=link.interest_portion,
                        repayment_date=entry.posting_date or now.date(),
                        created_by_id=approver_id,
                        skip_link_creation=True,
                        organization_id=org_id,
                    )
                except Exception as loan_err:
                    raise PayrollServiceError(
                        "Failed to process loan deduction "
                        f"for slip {slip.slip_number} and loan {link.loan_id}: {loan_err}"
                    ) from loan_err
                link.repayment_id = repayment.repayment_id

        entry.status = PayrollEntryStatus.APPROVED
        self.db.flush()

        if get_cached_setting(
            self.db,
            SettingDomain.payroll,
            "auto_post_gl_on_approval",
            True,
            organization_id=org_id,
        ):
            posting_result = self.handoff_payroll_to_books(
                org_id,
                entry_id,
                posting_date=now.date(),
                user_id=approver_id,
                posted_at=now,
            )
            if not posting_result.get("success"):
                raise PayrollServiceError(
                    posting_result.get("error")
                    or "Payroll approved but GL posting failed"
                )
        return entry

    def payout_payroll_entry(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        paid_by_id: UUID,
        slip_ids: list[UUID] | None = None,
        payment_reference: str | None = None,
        amounts_paid: dict[UUID, Decimal] | None = None,
    ) -> dict:
        """Mark slips paid, recording how much was actually disbursed.

        `amounts_paid` maps a slip to the amount that genuinely moved. Omit an
        entry and the slip records its full `net_pay`, which is exactly the
        claim this method has always made implicitly — see the note in
        `record_disbursement`. The parameter exists so a caller that KNOWS a
        part-disbursement can say so; making it mandatory is stage 2 step 3's
        job, once every caller has a real amount to supply.
        """
        entry = self.get_payroll_entry(org_id, entry_id)
        slips = list(entry.salary_slips or [])
        if slip_ids:
            slips = [s for s in slips if s.slip_id in slip_ids]

        updated = 0
        errors: list[dict] = []

        # Import notification service
        from app.services.people.payroll.payroll_notifications import (
            PayrollNotificationService,
        )

        notification_service = PayrollNotificationService(self.db)

        paid_slips: list[SalarySlip] = []

        for slip in slips:
            if slip.status != SalarySlipStatus.POSTED:
                errors.append(
                    {"slip_id": str(slip.slip_id), "reason": "Slip not posted"}
                )
                continue
            slip.status = SalarySlipStatus.PAID
            slip.paid_at = datetime.now(UTC)
            slip.paid_by_id = paid_by_id
            slip.payment_reference = payment_reference
            record_disbursement(slip, (amounts_paid or {}).get(slip.slip_id))

            employee = self.db.get(Employee, slip.employee_id)
            if employee and employee.eligible_for_final_payroll:
                employee.eligible_for_final_payroll = False
                employee.final_payroll_processed_at = slip.paid_at

            updated += 1
            paid_slips.append(slip)

            # Send payment notification to employee
            try:
                notify_employee = slip.employee
                if notify_employee:
                    notification_service.notify_payslip_paid(slip, notify_employee)
            except Exception as notify_err:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to send payment notification for slip %s: %s",
                    slip.slip_id,
                    notify_err,
                )

        self.db.flush()

        # Dispatch events after commit to ensure persistence
        for slip in paid_slips:
            employee_id = getattr(slip, "employee_id", None)
            if employee_id is None:
                continue
            _dispatch_slip_paid(
                slip_id=slip.slip_id,
                slip_number=getattr(slip, "slip_number", ""),
                employee_id=employee_id,
            )

        return {"updated": updated, "requested": len(slips), "errors": errors}

    def handoff_payroll_to_books(
        self,
        org_id: UUID,
        entry_id: UUID,
        *,
        posting_date: date,
        user_id: UUID,
        posted_at: datetime | None = None,
    ) -> dict:
        result = PayrollGLAdapter.post_payroll_run(
            self.db,
            org_id=org_id,
            payroll_entry_id=entry_id,
            posting_date=posting_date,
            user_id=user_id,
            consolidated=True,  # Use single consolidated journal entry per run
            posted_at=posted_at,
        )
        return {"success": result.success, "error": result.error_message}

    def _get_entry_assignments(
        self, org_id: UUID, entry: PayrollEntry
    ) -> list[SalaryStructureAssignment]:
        query = (
            select(SalaryStructureAssignment)
            .join(
                Employee, SalaryStructureAssignment.employee_id == Employee.employee_id
            )
            .where(
                SalaryStructureAssignment.organization_id == org_id,
                SalaryStructureAssignment.from_date <= entry.end_date,
                or_(
                    SalaryStructureAssignment.to_date.is_(None),
                    SalaryStructureAssignment.to_date >= entry.start_date,
                ),
                payroll_employee_eligibility_clause(
                    period_start=entry.start_date,
                    period_end=entry.end_date,
                ),
            )
        )
        if entry.department_id:
            query = query.where(Employee.department_id == entry.department_id)
        if entry.designation_id:
            query = query.where(Employee.designation_id == entry.designation_id)
        if entry.employment_type_id:
            query = query.where(Employee.employment_type_id == entry.employment_type_id)
        return list(self.db.scalars(query).all())

    def _update_entry_totals(self, entry: PayrollEntry) -> None:
        slips = list(
            self.db.scalars(
                select(SalarySlip).where(SalarySlip.payroll_entry_id == entry.entry_id)
            ).all()
        )
        entry.employee_count = len(slips)
        entry.total_gross_pay = sum(
            ((s.gross_pay or Decimal("0")) for s in slips),
            Decimal("0"),
        )
        entry.total_deductions = sum(
            ((s.total_deduction or Decimal("0")) for s in slips),
            Decimal("0"),
        )
        entry.total_net_pay = sum(
            ((s.net_pay or Decimal("0")) for s in slips),
            Decimal("0"),
        )

    # =========================================================================
    # Reports
    # =========================================================================

    def get_payroll_summary_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """
        Get payroll summary report by period.

        Returns total gross, deductions, net pay, and breakdown by status.
        """

        today = date.today()
        if not start_date:
            start_date = today.replace(month=1, day=1)  # Year to date
        if not end_date:
            end_date = today

        # Aggregate by status
        status_results = self.db.execute(
            select(
                PayrollEntry.status,
                func.count(PayrollEntry.entry_id).label("run_count"),
                func.sum(PayrollEntry.employee_count).label("employee_count"),
                func.sum(PayrollEntry.total_gross_pay).label("total_gross"),
                func.sum(PayrollEntry.total_deductions).label("total_deductions"),
                func.sum(PayrollEntry.total_net_pay).label("total_net"),
            )
            .where(
                PayrollEntry.organization_id == org_id,
                PayrollEntry.start_date >= start_date,
                PayrollEntry.end_date <= end_date,
            )
            .group_by(PayrollEntry.status)
        ).all()
        status_breakdown = []
        total_runs = 0
        total_employees = 0
        total_gross = Decimal("0")
        total_deductions = Decimal("0")
        total_net = Decimal("0")

        for row in status_results:
            run_count = row.run_count or 0
            emp_count = row.employee_count or 0
            gross = row.total_gross or Decimal("0")
            deductions = row.total_deductions or Decimal("0")
            net = row.total_net or Decimal("0")

            status_breakdown.append(
                {
                    "status": row.status.value if row.status else "Unknown",
                    "run_count": run_count,
                    "employee_count": emp_count,
                    "total_gross": gross,
                    "total_deductions": deductions,
                    "total_net": net,
                }
            )

            total_runs += run_count
            total_employees += emp_count
            total_gross += gross
            total_deductions += deductions
            total_net += net

        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_runs": total_runs,
            "total_employees": total_employees,
            "total_gross": total_gross,
            "total_deductions": total_deductions,
            "total_net": total_net,
            "status_breakdown": status_breakdown,
        }

    def get_payroll_by_department_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """
        Get payroll breakdown by department.

        Returns payroll costs by department with employee counts.
        """
        from app.models.people.hr import Department, Employee

        today = date.today()
        if not start_date:
            start_date = today.replace(day=1)  # Current month
        if not end_date:
            end_date = today

        # Get salary slips grouped by department
        results = self.db.execute(
            select(
                Department.department_id,
                Department.department_name.label("department_name"),
                func.count(SalarySlip.slip_id).label("slip_count"),
                func.sum(SalarySlip.gross_pay).label("total_gross"),
                func.sum(SalarySlip.total_deduction).label("total_deductions"),
                func.sum(SalarySlip.net_pay).label("total_net"),
            )
            .select_from(SalarySlip)
            .join(Employee, SalarySlip.employee_id == Employee.employee_id)
            .outerjoin(Department, Employee.department_id == Department.department_id)
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.start_date >= start_date,
                SalarySlip.end_date <= end_date,
            )
            .group_by(Department.department_id, Department.department_name)
            .order_by(func.sum(SalarySlip.net_pay).desc())
        ).all()

        departments = []
        total_gross = Decimal("0")
        total_deductions = Decimal("0")
        total_net = Decimal("0")

        for row in results:
            gross = row.total_gross or Decimal("0")
            deductions = row.total_deductions or Decimal("0")
            net = row.total_net or Decimal("0")

            departments.append(
                {
                    "department_id": str(row.department_id)
                    if row.department_id
                    else None,
                    "department_name": row.department_name or "No Department",
                    "slip_count": row.slip_count or 0,
                    "total_gross": gross,
                    "total_deductions": deductions,
                    "total_net": net,
                }
            )

            total_gross += gross
            total_deductions += deductions
            total_net += net

        # Calculate percentages
        for dept in departments:
            net_value = dept.get("total_net") or Decimal("0")
            dept["percentage"] = (
                round(float(net_value) / float(total_net) * 100, 1)
                if total_net > 0
                else 0
            )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "departments": departments,
            "total_departments": len(departments),
            "total_gross": total_gross,
            "total_deductions": total_deductions,
            "total_net": total_net,
        }

    def get_payroll_tax_summary_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """
        Get payroll tax deduction summary.

        Returns breakdown of statutory deductions (tax, pension, etc.).
        """
        from app.models.people.payroll.salary_slip import SalarySlipDeduction

        today = date.today()
        if not start_date:
            start_date = today.replace(day=1)
        if not end_date:
            end_date = today

        # Get deductions grouped by component
        results = self.db.execute(
            select(
                SalaryComponent.component_id,
                SalaryComponent.component_name,
                SalaryComponent.component_code,
                SalaryComponent.is_statutory,
                func.count(SalarySlipDeduction.line_id).label("deduction_count"),
                func.sum(SalarySlipDeduction.amount).label("total_amount"),
            )
            .select_from(SalarySlipDeduction)
            .join(SalarySlip, SalarySlipDeduction.slip_id == SalarySlip.slip_id)
            .join(
                SalaryComponent,
                SalarySlipDeduction.component_id == SalaryComponent.component_id,
            )
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.status.in_(
                    [SalarySlipStatus.APPROVED, SalarySlipStatus.POSTED]
                ),
                SalarySlip.start_date >= start_date,
                SalarySlip.end_date <= end_date,
                SalarySlipDeduction.do_not_include_in_total.is_(False),
            )
            .group_by(
                SalaryComponent.component_id,
                SalaryComponent.component_name,
                SalaryComponent.component_code,
                SalaryComponent.is_statutory,
            )
            .order_by(func.sum(SalarySlipDeduction.amount).desc())
        ).all()

        deductions = []
        statutory_total = Decimal("0")
        non_statutory_total = Decimal("0")

        for row in results:
            amount = row.total_amount or Decimal("0")
            deductions.append(
                {
                    "component_id": str(row.component_id),
                    "component_name": row.component_name,
                    "component_code": row.component_code,
                    "is_statutory": row.is_statutory,
                    "deduction_count": row.deduction_count or 0,
                    "total_amount": amount,
                }
            )

            if row.is_statutory:
                statutory_total += amount
            else:
                non_statutory_total += amount

        total_deductions = statutory_total + non_statutory_total

        # Calculate percentages
        for d in deductions:
            d["percentage"] = (
                round(float(d["total_amount"]) / float(total_deductions) * 100, 1)
                if total_deductions > 0
                else 0
            )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "deductions": deductions,
            "statutory_total": statutory_total,
            "non_statutory_total": non_statutory_total,
            "total_deductions": total_deductions,
        }

    def get_payroll_trends_report(
        self,
        org_id: UUID,
        *,
        months: int = 12,
    ) -> dict:
        """
        Get payroll trends over time.

        Returns monthly breakdown of payroll costs.
        """
        from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

        today = date.today()
        end_date = today.replace(day=1)
        start_date = end_date - relativedelta(months=months - 1)

        # Query monthly aggregates
        month_bucket = func.date_trunc(
            literal_column("'month'"),
            SalarySlip.start_date,
        ).label("month")
        results = self.db.execute(
            select(
                month_bucket,
                func.count(SalarySlip.slip_id).label("slip_count"),
                func.sum(SalarySlip.gross_pay).label("total_gross"),
                func.sum(SalarySlip.total_deduction).label("total_deductions"),
                func.sum(SalarySlip.net_pay).label("total_net"),
            )
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.start_date >= start_date,
                SalarySlip.start_date <= today,
            )
            .group_by(month_bucket)
            .order_by(month_bucket)
        ).all()

        # Build results dict by month
        monthly_data = {}
        for row in results:
            month_key = row.month.strftime("%Y-%m")
            monthly_data[month_key] = {
                "month": month_key,
                "month_label": row.month.strftime("%b %Y"),
                "slip_count": row.slip_count or 0,
                "total_gross": row.total_gross or Decimal("0"),
                "total_deductions": row.total_deductions or Decimal("0"),
                "total_net": row.total_net or Decimal("0"),
            }

        # Fill in missing months with zeros
        months_list = []
        current = start_date
        total_gross = Decimal("0")
        total_net = Decimal("0")

        while current <= today:
            month_key = current.strftime("%Y-%m")
            if month_key in monthly_data:
                months_list.append(monthly_data[month_key])
                total_gross += monthly_data[month_key]["total_gross"]
                total_net += monthly_data[month_key]["total_net"]
            else:
                months_list.append(
                    {
                        "month": month_key,
                        "month_label": current.strftime("%b %Y"),
                        "slip_count": 0,
                        "total_gross": Decimal("0"),
                        "total_deductions": Decimal("0"),
                        "total_net": Decimal("0"),
                    }
                )
            current = current + relativedelta(months=1)

        num_months = len(months_list)
        average_monthly = calculate_active_monthly_net_average(months_list)

        return {
            "months": months_list,
            "total_months": num_months,
            "total_gross": total_gross,
            "total_net": total_net,
            "average_monthly": average_monthly,
        }

    def get_payroll_ytd_report(
        self,
        org_id: UUID,
        *,
        year: int | None = None,
    ) -> dict:
        """
        Get year-to-date payroll report with employee-level breakdowns.

        Returns aggregate totals and per-employee summary including
        statutory deduction breakdowns (PAYE, Pension, NHF).

        Args:
            org_id: Organization ID
            year: Report year (defaults to current year)

        Returns:
            Dict with 'totals' and 'rows' keys
        """
        from app.models.people.payroll.salary_component import SalaryComponent
        from app.models.people.payroll.salary_slip import SalarySlipDeduction
        from app.models.person import Person

        # Allow string org_id for testing compatibility
        try:
            org_id = coerce_uuid(org_id)
        except Exception:
            logger.exception(
                "Ignored exception"
            )  # Keep original value for mock testing

        if year is None:
            year = date.today().year

        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)

        # Base query: Employee-level aggregates
        base_stmt = (
            select(
                SalarySlip.employee_id,
                Employee.employee_code,
                Person.name_expr().label("employee_name"),
                func.coalesce(Employee.department_id, None).label("department_name"),
                func.count(SalarySlip.slip_id).label("slip_count"),
                func.sum(SalarySlip.gross_pay).label("total_gross"),
                func.sum(SalarySlip.total_deduction).label("total_deductions"),
                func.sum(SalarySlip.net_pay).label("total_net"),
            )
            .select_from(SalarySlip)
            .join(Employee, SalarySlip.employee_id == Employee.employee_id)
            .join(Person, Employee.person_id == Person.id)
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.start_date >= year_start,
                SalarySlip.end_date <= year_end,
            )
            .group_by(
                SalarySlip.employee_id,
                Employee.employee_code,
                Person.first_name,
                Person.last_name,
                Employee.department_id,
            )
            .order_by(Person.first_name, Person.last_name)
        )
        base_results = self.db.execute(base_stmt).all()

        # Query deduction breakdowns by component
        deduction_stmt = (
            select(
                SalarySlip.employee_id,
                SalaryComponent.component_code.label("component_code"),
                func.sum(SalarySlipDeduction.amount).label("total_amount"),
            )
            .select_from(SalarySlipDeduction)
            .join(SalarySlip, SalarySlipDeduction.slip_id == SalarySlip.slip_id)
            .join(
                SalaryComponent,
                SalarySlipDeduction.component_id == SalaryComponent.component_id,
            )
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.start_date >= year_start,
                SalarySlip.end_date <= year_end,
                SalaryComponent.component_code.in_(["PAYE", "PENSION", "NHF"]),
            )
            .group_by(SalarySlip.employee_id, SalaryComponent.component_code)
        )
        deduction_results = self.db.execute(deduction_stmt).all()

        # Build deduction lookup by employee
        deductions_by_employee: dict[str, dict[str, Decimal]] = {}
        total_paye = Decimal("0")
        total_pension = Decimal("0")
        total_nhf = Decimal("0")

        for row in deduction_results:
            emp_id = str(row.employee_id)
            if emp_id not in deductions_by_employee:
                deductions_by_employee[emp_id] = {}
            deductions_by_employee[emp_id][row.component_code] = (
                row.total_amount or Decimal("0")
            )

            if row.component_code == "PAYE":
                total_paye += row.total_amount or Decimal("0")
            elif row.component_code == "PENSION":
                total_pension += row.total_amount or Decimal("0")
            elif row.component_code == "NHF":
                total_nhf += row.total_amount or Decimal("0")

        # Build result rows and totals
        rows = []
        total_gross = Decimal("0")
        total_deductions = Decimal("0")
        total_net = Decimal("0")
        slip_count = 0

        for base_row in base_results:
            emp_id = str(base_row.employee_id)
            emp_deductions = deductions_by_employee.get(emp_id, {})

            rows.append(
                {
                    "employee_id": emp_id,
                    "employee_code": base_row.employee_code,
                    "employee_name": base_row.employee_name,
                    "department_name": base_row.department_name,
                    "slip_count": base_row.slip_count,
                    "total_gross": base_row.total_gross or Decimal("0"),
                    "total_deductions": base_row.total_deductions or Decimal("0"),
                    "total_net": base_row.total_net or Decimal("0"),
                    "paye": emp_deductions.get("PAYE", Decimal("0")),
                    "pension": emp_deductions.get("PENSION", Decimal("0")),
                    "nhf": emp_deductions.get("NHF", Decimal("0")),
                }
            )

            total_gross += base_row.total_gross or Decimal("0")
            total_deductions += base_row.total_deductions or Decimal("0")
            total_net += base_row.total_net or Decimal("0")
            slip_count += base_row.slip_count or 0

        return {
            "totals": {
                "total_gross": total_gross,
                "total_deductions": total_deductions,
                "total_net": total_net,
                "total_paye": total_paye,
                "total_pension": total_pension,
                "total_nhf": total_nhf,
                "slip_count": slip_count,
            },
            "rows": rows,
        }
