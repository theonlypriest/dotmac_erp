"""
PAYE Export Service.

Generates PAYE tax schedules for submission to state tax authorities.
Supports LIRS (Lagos) and FCTIRS (FCT/Abuja) formats.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.finance.core_org.organization import Organization
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.salary_slip import (
    SalarySlip,
    SalarySlipDeduction,
    SalarySlipEarning,
    SalarySlipStatus,
)
from app.services.people.payroll.employment_type_classification import (
    classify_payroll_employment_type,
)

logger = logging.getLogger(__name__)

PAYEFormat = Literal["lirs", "fctirs"]


def _round_currency(value: Decimal) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _spreadsheet_text(value: str | None) -> str:
    """Force spreadsheet apps to preserve long identifiers as text."""
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    return f'="{text_value}"'


@dataclass
class PAYEExportResult:
    """Result of PAYE export generation."""

    content: bytes
    filename: str
    content_type: str
    employee_count: int
    total_tax: Decimal
    errors: list[str]


class PAYEExportService:
    """
    Service for generating PAYE tax schedules.

    Supports:
    - LIRS (Lagos Internal Revenue Service) format
    - FCTIRS (FCT Internal Revenue Service) format
    """

    def __init__(self, db: Session):
        self.db = db

    def generate_export(
        self,
        organization_id: UUID,
        year: int,
        month: int,
        paye_format: PAYEFormat = "lirs",
        entry_id: UUID | None = None,
    ) -> PAYEExportResult:
        """
        Generate PAYE export for a period.

        Args:
            organization_id: Organization ID
            year: Tax year
            month: Tax month
            paye_format: Target format (lirs or fctirs)
            entry_id: Optional payroll entry ID to filter by

        Returns:
            PAYEExportResult with file content and metadata
        """
        slips = self._get_slips(organization_id, year, month, entry_id)
        profiles_by_employee = self._get_tax_profiles_by_employee(
            organization_id,
            slips,
        )
        organization_name = self._get_organization_name(organization_id)

        if paye_format == "lirs":
            return self._generate_lirs_format(
                slips,
                year,
                month,
                profiles_by_employee=profiles_by_employee,
            )
        else:
            return self._generate_fctirs_format(
                slips,
                year,
                month,
                organization_name=organization_name,
                profiles_by_employee=profiles_by_employee,
            )

    def _get_slips(
        self,
        organization_id: UUID,
        year: int,
        month: int,
        entry_id: UUID | None = None,
    ) -> list[SalarySlip]:
        """Get salary slips for the period."""
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1)
        else:
            end_date = date(year, month + 1, 1)

        stmt = (
            select(SalarySlip)
            .options(
                joinedload(SalarySlip.employee),
                joinedload(SalarySlip.earnings).joinedload(SalarySlipEarning.component),
                joinedload(SalarySlip.deductions).joinedload(
                    SalarySlipDeduction.component
                ),
            )
            .where(
                SalarySlip.organization_id == organization_id,
                SalarySlip.status.in_(SalarySlipStatus.gl_impacting()),
                SalarySlip.start_date >= start_date,
                SalarySlip.start_date < end_date,
            )
        )

        if entry_id:
            stmt = stmt.where(SalarySlip.payroll_entry_id == entry_id)

        return list(self.db.scalars(stmt).unique().all())

    def _generate_lirs_format(
        self,
        slips: list[SalarySlip],
        year: int,
        month: int,
        profiles_by_employee: dict[UUID, EmployeeTaxProfile] | None = None,
    ) -> PAYEExportResult:
        """
        Generate LIRS (Lagos) PAYE format.

        Columns based on LIRS template:
        name, tax_payer_number, nationality, designation, no_of_months,
        1-Basic Salary, 2-Housing, 3-Transport, 4-Furniture, 5-Education,
        6-Lunch, 7-Passage, 8-Leave, 9-Bonus, 10-13th Month, 11-Utility,
        12-Other Allowances, 13-NHF, 14-NHIS, 15-National Pension Scheme,
        16-Life Assurance, gross_income, tax_payable
        """
        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow(
            [
                "name",
                "tax_payer_number",
                "nationality",
                "designation",
                "no_of_months",
                "1-Basic Salary",
                "2-Housing",
                "3-Transport",
                "4-Furniture",
                "5-Education",
                "6-Lunch",
                "7-Passage",
                "8-Leave",
                "9-Bonus",
                "10-13th Month",
                "11-Utility",
                "12-Other Allowances",
                "13-NHF",
                "14-NHIS",
                "15-National Pension Scheme",
                "16-Life Assurance",
                "gross_income",
                "tax_payable",
            ]
        )

        errors: list[str] = []
        total_tax = Decimal("0")
        employee_count = 0

        for slip in slips:
            employee = slip.employee
            if not employee:
                errors.append(f"Slip {slip.slip_number}: No employee data")
                continue

            # Get employee tax profile
            tax_profile = self._get_tax_profile_for_employee(
                employee,
                profiles_by_employee,
            )
            if not self._should_include_employee(employee, tax_profile, "lirs"):
                continue
            tin = ""
            if tax_profile:
                tin = tax_profile.tin or ""

            # Get earnings breakdown
            earnings = self._extract_earnings_breakdown(slip)

            # Get deductions by component code
            deduction_map = self._extract_deduction_map(slip)
            pension = deduction_map.get("PENSION", Decimal("0"))
            nhf = deduction_map.get("NHF", Decimal("0"))
            nhis = deduction_map.get("NHIS", Decimal("0"))
            paye = deduction_map.get("PAYE", Decimal("0"))

            designation = ""
            if employee.designation:
                designation = employee.designation.designation_name or ""

            writer.writerow(
                [
                    employee.full_name,
                    _spreadsheet_text(tin),
                    "Nigerian",  # Default nationality
                    designation,
                    "1",  # Number of months (1 for monthly payroll)
                    str(_round_currency(earnings.get("basic", Decimal("0")))),
                    str(_round_currency(earnings.get("housing", Decimal("0")))),
                    str(_round_currency(earnings.get("transport", Decimal("0")))),
                    str(_round_currency(earnings.get("furniture", Decimal("0")))),
                    str(_round_currency(earnings.get("education", Decimal("0")))),
                    str(_round_currency(earnings.get("lunch", Decimal("0")))),
                    str(_round_currency(earnings.get("passage", Decimal("0")))),
                    str(_round_currency(earnings.get("leave", Decimal("0")))),
                    str(_round_currency(earnings.get("bonus", Decimal("0")))),
                    str(_round_currency(earnings.get("13th_month", Decimal("0")))),
                    str(_round_currency(earnings.get("utility", Decimal("0")))),
                    str(_round_currency(earnings.get("other", Decimal("0")))),
                    str(_round_currency(nhf)),
                    str(_round_currency(nhis)),
                    str(_round_currency(pension)),
                    "0",  # Life Assurance (not tracked separately)
                    str(_round_currency(slip.gross_pay)),
                    str(_round_currency(paye)),
                ]
            )

            total_tax += paye
            employee_count += 1

        content = output.getvalue().encode("utf-8")
        filename = f"LIRS_PAYE_{year}_{month:02d}.csv"

        return PAYEExportResult(
            content=content,
            filename=filename,
            content_type="text/csv",
            employee_count=employee_count,
            total_tax=_round_currency(total_tax),
            errors=errors,
        )

    def _generate_fctirs_format(
        self,
        slips: list[SalarySlip],
        year: int,
        month: int,
        organization_name: str | None = None,
        profiles_by_employee: dict[UUID, EmployeeTaxProfile] | None = None,
    ) -> PAYEExportResult:
        """
        Generate FCTIRS (FCT) PAYE format.
        """
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([(organization_name or "Company").upper()])
        writer.writerow([])
        writer.writerow(
            [
                f"PAY-AS-YOU-EARN COMPUTATION FOR {self._format_period_label(year, month)}"
            ]
        )
        writer.writerow(
            [
                "S/N",
                "NAME",
                "TIN ID",
                "BASIC",
                "HOUSING",
                "TRANSPORT",
                "UTILITY",
                "MEDICAL",
                "ENTERTAINMENT",
                "OTHER NON ALLOW",
                "MONTHLY GROSS INCOME",
                "ANNUAL GROSS INCOME",
                "EARNED INCOME",
                "CONSOLDATED ALLOWANCE",
                "NHF",
                "NHIS",
                "LIFE ASSURANCE",
                "PENSION",
                "TOTAL  ALLOWANCE",
                "CHARGEABLE INCOME",
                "ANNUAL TAX DUE",
                "ANNUAL MINIMUM TAX",
                "ANNUAL TAX PAYABLE",
                "MONTHLY TAX PAYABLE",
                "No. of Months Worked",
                "TAX PAID",
                "TAX OVER/(UNDER) DEDUC.",
            ]
        )

        errors: list[str] = []
        total_tax = Decimal("0")
        employee_count = 0
        serial_number = 1

        for slip in slips:
            employee = slip.employee
            if not employee:
                errors.append(f"Slip {slip.slip_number}: No employee data")
                continue

            # Get employee tax profile
            tax_profile = self._get_tax_profile_for_employee(
                employee,
                profiles_by_employee,
            )
            if not self._should_include_employee(employee, tax_profile, "fctirs"):
                continue
            tin = ""
            if tax_profile:
                tin = tax_profile.tin or ""

            # Get earnings breakdown
            earnings = self._extract_earnings_breakdown(slip)
            basic = earnings.get("basic", Decimal("0"))
            housing = earnings.get("housing", Decimal("0"))
            transport = earnings.get("transport", Decimal("0"))
            utility = earnings.get("utility", Decimal("0"))
            medical = earnings.get("medical", Decimal("0"))
            entertainment = earnings.get("entertainment", Decimal("0"))
            other = slip.gross_pay - (
                basic + housing + transport + utility + medical + entertainment
            )

            # Get deductions by component code
            deduction_map = self._extract_deduction_map(slip)
            pension = deduction_map.get("PENSION", Decimal("0"))
            nhf = deduction_map.get("NHF", Decimal("0"))
            nhis = deduction_map.get("NHIS", Decimal("0"))
            paye = deduction_map.get("PAYE", Decimal("0"))
            life_assurance = deduction_map.get("LIFE_ASSURANCE", Decimal("0"))

            annual_gross = slip.gross_pay * 12
            earned_income = annual_gross
            consolidated_allowance = (
                max(Decimal("200000"), Decimal("0.01") * annual_gross)
                + Decimal("0.20") * annual_gross
            )
            annual_nhf = nhf * 12
            annual_nhis = nhis * 12
            annual_life_assurance = life_assurance * 12
            annual_pension = pension * 12
            total_allowance = (
                consolidated_allowance
                + annual_nhf
                + annual_nhis
                + annual_life_assurance
                + annual_pension
            )
            chargeable_income = max(Decimal("0"), earned_income - total_allowance)
            bands = self._calculate_tax_bands(chargeable_income)
            annual_tax_due = sum(bands.values(), Decimal("0"))
            annual_minimum_tax = annual_gross * Decimal("0.01")
            annual_tax_payable = max(annual_tax_due, annual_minimum_tax)
            monthly_tax_payable = annual_tax_payable / Decimal("12")
            no_of_months_worked = Decimal("1")
            tax_paid = paye
            tax_over_under = tax_paid - monthly_tax_payable

            writer.writerow(
                [
                    serial_number,
                    employee.full_name,
                    _spreadsheet_text(tin),
                    str(_round_currency(basic)),
                    str(_round_currency(housing)),
                    str(_round_currency(transport)),
                    str(_round_currency(utility)),
                    str(_round_currency(medical)),
                    str(_round_currency(entertainment)),
                    str(_round_currency(other)),
                    str(_round_currency(slip.gross_pay)),
                    str(_round_currency(annual_gross)),
                    str(_round_currency(earned_income)),
                    str(_round_currency(consolidated_allowance)),
                    str(_round_currency(nhf)),
                    str(_round_currency(nhis)),
                    str(_round_currency(life_assurance)),
                    str(_round_currency(pension)),
                    str(_round_currency(total_allowance)),
                    str(_round_currency(chargeable_income)),
                    str(_round_currency(annual_tax_due)),
                    str(_round_currency(annual_minimum_tax)),
                    str(_round_currency(annual_tax_payable)),
                    str(_round_currency(monthly_tax_payable)),
                    str(_round_currency(no_of_months_worked)),
                    str(_round_currency(tax_paid)),
                    str(_round_currency(tax_over_under)),
                ]
            )

            total_tax += paye
            employee_count += 1
            serial_number += 1

        content = output.getvalue().encode("utf-8")
        filename = f"FCTIRS_PAYE_{year}_{month:02d}.csv"

        return PAYEExportResult(
            content=content,
            filename=filename,
            content_type="text/csv",
            employee_count=employee_count,
            total_tax=_round_currency(total_tax),
            errors=errors,
        )

    def _get_organization_name(self, organization_id: UUID) -> str:
        """Resolve organization display name for FCTIRS export headings."""
        if self.db is None:
            return "Company"
        org = self.db.get(Organization, organization_id)
        if not org:
            return "Company"
        return org.trading_name or org.legal_name or org.organization_code or "Company"

    @staticmethod
    def _format_period_label(year: int, month: int) -> str:
        """Render payroll period label like 'Nov, 2025'."""
        return date(year, month, 1).strftime("%b, %Y")

    @staticmethod
    def _normalize_tax_state(value: str | None) -> str:
        if not value:
            return ""
        return str(value).strip().lower()

    def _employee_matches_tax_jurisdiction(
        self,
        tax_profile: EmployeeTaxProfile | None,
        paye_format: PAYEFormat,
    ) -> bool:
        tax_state = self._normalize_tax_state(getattr(tax_profile, "tax_state", None))
        if not tax_state:
            return False
        if paye_format == "lirs":
            return tax_state in {"lagos", "lagos state"}
        return tax_state in {
            "fct",
            "abuja",
            "federal capital territory",
            "fct abuja",
        }

    def _should_include_employee(
        self,
        employee,
        tax_profile: EmployeeTaxProfile | None,
        paye_format: PAYEFormat,
    ) -> bool:
        classification = classify_payroll_employment_type(
            self.db,
            organization_id=employee.organization_id,
            employment_type_id=employee.employment_type_id,
        )
        return classification.is_permanent and self._employee_matches_tax_jurisdiction(
            tax_profile, paye_format
        )

    @staticmethod
    def _get_tax_profile_for_employee(
        employee,
        profiles_by_employee: dict[UUID, EmployeeTaxProfile] | None = None,
    ) -> EmployeeTaxProfile | None:
        """Resolve an employee tax profile from eager state or explicit lookup."""
        employee_id = getattr(employee, "employee_id", None)
        if employee_id and profiles_by_employee:
            profile = profiles_by_employee.get(employee_id)
            if profile is not None:
                return profile
        return getattr(employee, "current_tax_profile", None)

    def _get_tax_profiles_by_employee(
        self,
        organization_id: UUID,
        slips: list[SalarySlip],
    ) -> dict[UUID, EmployeeTaxProfile]:
        """Load current tax profiles for exported employees."""
        if self.db is None:
            return {}

        employee_ids = {
            slip.employee.employee_id
            for slip in slips
            if getattr(slip, "employee", None) is not None
            and getattr(slip.employee, "employee_id", None) is not None
        }
        if not employee_ids:
            return {}

        stmt = (
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.organization_id == organization_id,
                EmployeeTaxProfile.employee_id.in_(employee_ids),
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(
                EmployeeTaxProfile.employee_id,
                EmployeeTaxProfile.effective_from.desc(),
            )
        )

        profiles_by_employee: dict[UUID, EmployeeTaxProfile] = {}
        for profile in self.db.scalars(stmt).all():
            profiles_by_employee.setdefault(profile.employee_id, profile)
        return profiles_by_employee

    def _extract_earnings_breakdown(self, slip: SalarySlip) -> dict[str, Decimal]:
        """Extract earnings breakdown from salary slip."""
        breakdown: dict[str, Decimal] = {
            "basic": Decimal("0"),
            "housing": Decimal("0"),
            "transport": Decimal("0"),
            "furniture": Decimal("0"),
            "education": Decimal("0"),
            "lunch": Decimal("0"),
            "passage": Decimal("0"),
            "leave": Decimal("0"),
            "bonus": Decimal("0"),
            "13th_month": Decimal("0"),
            "utility": Decimal("0"),
            "medical": Decimal("0"),
            "entertainment": Decimal("0"),
            "other": Decimal("0"),
        }

        # Map component codes to breakdown keys
        code_mapping = {
            "BASIC": "basic",
            "HOUSING": "housing",
            "TRANSPORT": "transport",
            "FURNITURE": "furniture",
            "EDUCATION": "education",
            "LUNCH": "lunch",
            "MEAL": "lunch",
            "PASSAGE": "passage",
            "LEAVE": "leave",
            "LEAVE_ALLOWANCE": "leave",
            "BONUS": "bonus",
            "13TH_MONTH": "13th_month",
            "THIRTEENTH_MONTH": "13th_month",
            "UTILITY": "utility",
            "MEDICAL": "medical",
            "ENTERTAINMENT": "entertainment",
        }

        for earning in slip.earnings:
            component_code = (
                earning.component.component_code if earning.component else ""
            ).upper()
            key = code_mapping.get(component_code, "other")
            breakdown[key] += earning.amount or Decimal("0")

        return breakdown

    def _extract_deduction_map(self, slip: SalarySlip) -> dict[str, Decimal]:
        """Map deduction component codes to totals."""
        totals: dict[str, Decimal] = {}
        for deduction in slip.deductions:
            component_code = (
                deduction.component.component_code if deduction.component else ""
            ).upper()
            if not component_code:
                continue
            totals[component_code] = totals.get(component_code, Decimal("0")) + (
                deduction.amount or Decimal("0")
            )
        return totals

    def _calculate_tax_bands(self, taxable_income: Decimal) -> dict[str, Decimal]:
        """
        Calculate tax amounts per NTA 2025 bands.

        Returns tax amount (not income) in each band.
        """
        bands = {
            "band1": Decimal("0"),  # First 300K @ 0%
            "band2": Decimal("0"),  # Next 300K @ 15%
            "band3": Decimal("0"),  # Next 500K @ 18%
            "band4": Decimal("0"),  # Next 500K @ 21%
            "band5": Decimal("0"),  # Next 1.6M @ 23%
            "band6": Decimal("0"),  # Above 3.2M @ 25%
        }

        remaining = taxable_income

        # Band 1: First 300,000 @ 0%
        band1_limit = Decimal("300000")
        if remaining <= band1_limit:
            return bands
        remaining -= band1_limit

        # Band 2: Next 300,000 @ 15%
        band2_limit = Decimal("300000")
        band2_income = min(remaining, band2_limit)
        bands["band2"] = band2_income * Decimal("0.15")
        if remaining <= band2_limit:
            return bands
        remaining -= band2_limit

        # Band 3: Next 500,000 @ 18%
        band3_limit = Decimal("500000")
        band3_income = min(remaining, band3_limit)
        bands["band3"] = band3_income * Decimal("0.18")
        if remaining <= band3_limit:
            return bands
        remaining -= band3_limit

        # Band 4: Next 500,000 @ 21%
        band4_limit = Decimal("500000")
        band4_income = min(remaining, band4_limit)
        bands["band4"] = band4_income * Decimal("0.21")
        if remaining <= band4_limit:
            return bands
        remaining -= band4_limit

        # Band 5: Next 1,600,000 @ 23%
        band5_limit = Decimal("1600000")
        band5_income = min(remaining, band5_limit)
        bands["band5"] = band5_income * Decimal("0.23")
        if remaining <= band5_limit:
            return bands
        remaining -= band5_limit

        # Band 6: Above 3,200,000 @ 25%
        bands["band6"] = remaining * Decimal("0.25")

        return bands


def paye_export_service(db: Session) -> PAYEExportService:
    """Create a PAYEExportService instance."""
    return PAYEExportService(db)
