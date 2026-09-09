"""
Payroll Web Service - Salary Slip operations.
"""

from __future__ import annotations

import io
import logging
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.people.hr.employee import Employee
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.salary_slip import (
    SalarySlip,
    SalarySlipDeduction,
    SalarySlipEarning,
    SalarySlipStatus,
)
from app.models.people.payroll.salary_structure import SalaryStructure
from app.services.common import (
    PaginationParams,
    apply_search,
    coerce_uuid,
    paginate,
)
from app.services.people.hr.employment_types import EmploymentTypeService
from app.services.people.payroll import (
    PayrollGLAdapter,
    SalarySlipInput,
    salary_slip_service,
)
from app.services.people.payroll.eligibility import payroll_employee_eligibility_clause
from app.services.people.payroll.employment_type_classification import (
    classify_payroll_employment_type,
)
from app.services.people.payroll.paye_calculator import PAYECalculator
from app.services.people.payroll_reporting import REPORTABLE_SLIP_STATUSES
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

from .base import (
    DEFAULT_PAGE_SIZE,
    SLIP_STATUSES,
    parse_date,
    parse_decimal,
    parse_slip_status,
    parse_uuid,
)

logger = logging.getLogger(__name__)

# Standard component codes used for fixed CSV columns
_CSV_BASIC = "BASIC"
_CSV_HOUSING = "HOUSING"
_CSV_TRANSPORT = "TRANSPORT"
_CSV_PENSION = "PENSION"
_CSV_NHF = "NHF"
_CSV_PAYE = "PAYE"
_CSV_PENSION_EMPLOYER = "PENSION_EMPLOYER"


class SlipWebService:
    """Service for salary slip web views."""

    @staticmethod
    def _form_str(form: Any, key: str) -> str:
        """Normalize form value to a trimmed string."""
        value = form.get(key)
        if value is None:
            return ""
        return str(value).strip()

    def list_slips_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        status: str | None = None,
        employment_type_id: str | None = None,
        page: int = 1,
    ) -> HTMLResponse | RedirectResponse:
        """Render salary slips list page."""
        org_id = coerce_uuid(auth.organization_id)
        start_date = parse_date(request.query_params.get("start_date"))
        end_date = parse_date(request.query_params.get("end_date"))
        status_group = request.query_params.get("status_group") or ""
        try:
            per_page = int(request.query_params.get("limit", str(DEFAULT_PAGE_SIZE)))
        except (TypeError, ValueError):
            per_page = DEFAULT_PAGE_SIZE
        if per_page not in {25, 50, 100, 200}:
            per_page = DEFAULT_PAGE_SIZE
        query = select(SalarySlip).where(SalarySlip.organization_id == org_id)
        query = apply_search(
            query,
            search,
            SalarySlip.slip_number,
            SalarySlip.employee_name,
        )

        status_enum = parse_slip_status(status)
        if status_enum:
            query = query.where(SalarySlip.status == status_enum)
        elif status_group == "reportable":
            query = query.where(SalarySlip.status.in_(REPORTABLE_SLIP_STATUSES))

        if start_date:
            query = query.where(SalarySlip.start_date >= start_date)
        if end_date:
            query = query.where(SalarySlip.start_date <= end_date)

        parsed_employment_type_id = parse_uuid(employment_type_id)
        if parsed_employment_type_id:
            query = query.join(
                Employee, SalarySlip.employee_id == Employee.employee_id
            ).where(
                Employee.organization_id == org_id,
                Employee.employment_type_id == parsed_employment_type_id,
            )

        query = query.order_by(SalarySlip.created_at.desc())
        result = paginate(db, query, PaginationParams.from_page(page, per_page))
        if page > result.total_pages:
            page = result.total_pages
            result = paginate(db, query, PaginationParams.from_page(page, per_page))
        slips = result.items
        total = result.total
        total_pages = result.total_pages

        # Get counts by status
        status_counts = {}
        for s in SalarySlipStatus:
            count = (
                db.scalar(
                    select(func.count())
                    .select_from(SalarySlip)
                    .where(
                        SalarySlip.organization_id == org_id,
                        SalarySlip.status == s,
                    )
                )
                or 0
            )
            status_counts[s.value] = count

        employment_types = list(EmploymentTypeService(db, org_id).iter_all(active=True))

        active_filters = []
        if status_enum:
            active_filters.append(
                {
                    "name": "status",
                    "value": status_enum.value,
                    "display_value": f"Status: {status_enum.value.title()}",
                }
            )
        elif status_group == "reportable":
            active_filters.append(
                {
                    "name": "status_group",
                    "value": "reportable",
                    "display_value": "Status: Finalized payroll",
                }
            )
        if start_date:
            active_filters.append(
                {
                    "name": "start_date",
                    "value": start_date.isoformat(),
                    "display_value": f"From: {start_date.strftime('%d %b %Y')}",
                }
            )
        if end_date:
            active_filters.append(
                {
                    "name": "end_date",
                    "value": end_date.isoformat(),
                    "display_value": f"To: {end_date.strftime('%d %b %Y')}",
                }
            )
        if parsed_employment_type_id:
            selected_employment_type = next(
                (
                    employment_type
                    for employment_type in employment_types
                    if employment_type.employment_type_id == parsed_employment_type_id
                ),
                None,
            )
            if selected_employment_type:
                active_filters.append(
                    {
                        "name": "employment_type_id",
                        "value": str(parsed_employment_type_id),
                        "display_value": (
                            f"Employment type: {selected_employment_type.type_name}"
                        ),
                    }
                )

        context = base_context(request, auth, "Salary Slips", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "slips": slips,
                "search": search or "",
                "status": status or "",
                "status_group": status_group,
                "start_date": start_date.isoformat() if start_date else "",
                "end_date": end_date.isoformat() if end_date else "",
                "employment_type_id": (
                    str(parsed_employment_type_id) if parsed_employment_type_id else ""
                ),
                "employment_types": employment_types,
                "page": page,
                "total_pages": total_pages,
                "total_count": total,
                "total": total,
                "limit": per_page,
                "has_prev": page > 1,
                "has_next": page < total_pages,
                "status_counts": status_counts,
                "statuses": SLIP_STATUSES,
                "active_filters": active_filters,
            }
        )
        return templates.TemplateResponse(request, "people/payroll/slips.html", context)

    def export_slips_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        status: str | None = None,
        employment_type_id: str | None = None,
    ) -> Response:
        """Export salary slips to CSV."""
        org_id = coerce_uuid(auth.organization_id)
        start_date = parse_date(request.query_params.get("start_date"))
        end_date = parse_date(request.query_params.get("end_date"))
        status_group = request.query_params.get("status_group") or ""

        query = (
            select(SalarySlip)
            .where(SalarySlip.organization_id == org_id)
            .options(
                selectinload(SalarySlip.earnings).selectinload(
                    SalarySlipEarning.component
                ),
                selectinload(SalarySlip.deductions).selectinload(
                    SalarySlipDeduction.component
                ),
            )
        )

        if search:
            query = query.where(
                SalarySlip.slip_number.ilike(f"%{search}%")
                | SalarySlip.employee_name.ilike(f"%{search}%")
            )

        status_enum = parse_slip_status(status)
        if status_enum:
            query = query.where(SalarySlip.status == status_enum)
        elif status_group == "reportable":
            query = query.where(SalarySlip.status.in_(REPORTABLE_SLIP_STATUSES))

        if start_date:
            query = query.where(SalarySlip.start_date >= start_date)
        if end_date:
            query = query.where(SalarySlip.start_date <= end_date)

        parsed_employment_type_id = parse_uuid(employment_type_id)
        if parsed_employment_type_id:
            query = query.join(
                Employee, SalarySlip.employee_id == Employee.employee_id
            ).where(
                Employee.organization_id == org_id,
                Employee.employment_type_id == parsed_employment_type_id,
            )

        slips = db.scalars(query.order_by(SalarySlip.created_at.desc())).all()

        headers = [
            "Slip #",
            "Employee",
            "Period",
            "Basic Salary (Basic)",
            "Housing Allowance (Hsg)",
            "Transport Allowance (Trsp)",
            "Other Allowances (Other)",
            "Gross",
            "Employee Pension (8% of Basic+Transport+Housing) (Pen)",
            "National Housing Fund (2.5%) (NHF)",
            "PAYE Tax (PAYE)",
            "Employer Pension Contribution (10% of Basic+Transport+Housing) (PEN-ER)",
            "Deductions",
            "Net Pay",
            "Status",
            "Bank Name",
            "Bank Account Number",
            "Bank Branch Code",
        ]

        def _fmt(amount: Decimal) -> str:
            return f"{amount:,.2f}"

        def _fmt_deduction(amount: Decimal) -> str:
            return f"({amount:,.2f})"

        rows: list[list[str]] = [headers]
        for slip in slips:
            period = f"{slip.start_date.strftime('%b %d')} - {slip.end_date.strftime('%b %d, %Y')}"

            basic = Decimal("0")
            housing = Decimal("0")
            transport = Decimal("0")
            other = Decimal("0")
            for earning in slip.earnings or []:
                if earning.statistical_component or earning.do_not_include_in_total:
                    continue
                code = (
                    (earning.component.component_code if earning.component else "")
                    or ""
                ).upper()
                amount = earning.amount or Decimal("0")
                if code == _CSV_BASIC:
                    basic += amount
                elif code == _CSV_HOUSING:
                    housing += amount
                elif code == _CSV_TRANSPORT:
                    transport += amount
                else:
                    other += amount

            employee_pension = Decimal("0")
            nhf = Decimal("0")
            paye = Decimal("0")
            employer_pension = Decimal("0")
            for deduction in slip.deductions or []:
                code = (
                    (deduction.component.component_code if deduction.component else "")
                    or ""
                ).upper()
                amount = deduction.amount or Decimal("0")
                if code == _CSV_PENSION and not deduction.do_not_include_in_total:
                    employee_pension += amount
                elif code == _CSV_NHF and not deduction.do_not_include_in_total:
                    nhf += amount
                elif code == _CSV_PAYE and not deduction.do_not_include_in_total:
                    paye += amount
                elif code == _CSV_PENSION_EMPLOYER:
                    # Employer pension is tracked as a statistical deduction line.
                    employer_pension += amount

            rows.append(
                [
                    slip.slip_number,
                    slip.employee_name or "",
                    period,
                    _fmt(basic),
                    _fmt(housing),
                    _fmt(transport),
                    _fmt(other),
                    _fmt(slip.gross_pay),
                    _fmt_deduction(employee_pension),
                    _fmt_deduction(nhf),
                    _fmt_deduction(paye),
                    _fmt_deduction(employer_pension),
                    _fmt_deduction(slip.total_deduction),
                    _fmt(slip.net_pay),
                    slip.status.value.title(),
                    slip.bank_name or "",
                    slip.bank_account_number or "",
                    slip.bank_branch_code or "",
                ]
            )

        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Salary Slips"

        # Header styling
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(
            start_color="0D9488", end_color="0D9488", fill_type="solid"
        )
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Write headers
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

        # Number format for currency columns
        currency_fmt = "#,##0.00"
        # Columns: 4-7 earnings, 8 gross, 9-12 deductions, 13 total ded, 14 net pay (1-indexed)
        earning_cols = {4, 5, 6, 7, 8, 14}  # Basic, Hsg, Trsp, Other, Gross, Net Pay
        deduction_cols = {9, 10, 11, 12, 13}  # Pen, NHF, PAYE, PEN-ER, Deductions

        # Write data rows
        for row_idx, row_data in enumerate(rows, 2):
            for col_idx, value in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                if col_idx in earning_cols or col_idx in deduction_cols:
                    # Store as number for Excel, strip formatting chars
                    raw = str(value).replace(",", "").replace("(", "").replace(")", "")
                    try:
                        num = Decimal(raw)
                        cell.value = float(num)
                        cell.number_format = currency_fmt
                        cell.alignment = Alignment(horizontal="right")
                    except (ValueError, ArithmeticError):
                        pass

        # Auto-width columns
        for col_idx in range(1, len(headers) + 1):
            col_letter = get_column_letter(col_idx)
            max_len = len(str(headers[col_idx - 1]))
            for row_idx in range(2, len(rows) + 2):
                cell_val = ws.cell(row=row_idx, column=col_idx).value
                if cell_val is not None:
                    max_len = max(max_len, len(str(cell_val)))
            ws.column_dimensions[col_letter].width = min(max_len + 3, 40)

        # Freeze header row
        ws.freeze_panes = "A2"

        # Auto-filter
        ws.auto_filter.ref = ws.dimensions

        buffer = io.BytesIO()
        wb.save(buffer)
        content = buffer.getvalue()
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="salary_slips.xlsx"'},
        )

    def slip_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Render new salary slip form."""
        org_id = coerce_uuid(auth.organization_id)

        employees = db.scalars(
            select(Employee)
            .where(
                Employee.organization_id == org_id,
                payroll_employee_eligibility_clause(
                    period_start=date.today(),
                    period_end=date.today(),
                ),
            )
            .order_by(Employee.employee_code)
        ).all()

        context = base_context(request, auth, "New Salary Slip", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "slip": None,
                "employees": employees,
                "form_data": {},
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/slip_form.html", context
        )

    def slip_edit_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> HTMLResponse | RedirectResponse:
        """Render edit salary slip form."""
        org_id = coerce_uuid(auth.organization_id)
        s_id = parse_uuid(slip_id)

        if not s_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+updated+successfully",
                status_code=303,
            )

        slip = db.get(SalarySlip, s_id)
        if not slip or slip.organization_id != org_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+updated+successfully",
                status_code=303,
            )

        if slip.status != SalarySlipStatus.DRAFT:
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip_id}?saved=1", status_code=303
            )

        employees = db.scalars(
            select(Employee)
            .where(
                Employee.organization_id == org_id,
                payroll_employee_eligibility_clause(
                    period_start=slip.start_date,
                    period_end=slip.end_date,
                ),
            )
            .order_by(Employee.employee_code)
        ).all()

        context = base_context(request, auth, "Edit Salary Slip", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "slip": slip,
                "employees": employees,
                "form_data": {
                    "employee_id": str(slip.employee_id),
                    "start_date": slip.start_date.isoformat(),
                    "end_date": slip.end_date.isoformat(),
                    "posting_date": slip.posting_date.isoformat()
                    if slip.posting_date
                    else "",
                    "total_working_days": str(slip.total_working_days or ""),
                    "absent_days": str(slip.absent_days or "0"),
                    "leave_without_pay": str(slip.leave_without_pay or "0"),
                },
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/slip_form.html", context
        )

    async def create_slip_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> Response:
        """Create new salary slip."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        employee_id = self._form_str(form, "employee_id")
        start_date = self._form_str(form, "start_date")
        end_date = self._form_str(form, "end_date")
        posting_date = self._form_str(form, "posting_date")
        total_working_days = self._form_str(form, "total_working_days")
        absent_days = self._form_str(form, "absent_days") or "0"
        leave_without_pay = self._form_str(form, "leave_without_pay") or "0"

        if not employee_id or not start_date or not end_date:
            employees = db.scalars(
                select(Employee)
                .where(
                    Employee.organization_id == org_id,
                    payroll_employee_eligibility_clause(
                        period_start=parse_date(start_date) or date.today(),
                        period_end=parse_date(end_date) or date.today(),
                    ),
                )
                .order_by(Employee.employee_code)
            ).all()
            context = base_context(request, auth, "New Salary Slip", "payroll", db=db)
            context["request"] = request
            context.update(
                {
                    "slip": None,
                    "employees": employees,
                    "error": "Employee, period start, and period end are required.",
                    "form_data": {
                        "employee_id": employee_id,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                    "errors": {},
                }
            )
            return templates.TemplateResponse(
                request, "people/payroll/slip_form.html", context
            )

        try:
            start = parse_date(start_date)
            end = parse_date(end_date)
            posting = parse_date(posting_date)
            if start is None or end is None:
                raise ValueError("Invalid start or end date")

            slip_input = SalarySlipInput(
                employee_id=coerce_uuid(employee_id),
                start_date=start,
                end_date=end,
                posting_date=posting,
                total_working_days=parse_decimal(total_working_days),
                absent_days=parse_decimal(absent_days) or Decimal("0"),
                leave_without_pay=parse_decimal(leave_without_pay) or Decimal("0"),
            )

            slip = salary_slip_service.create_salary_slip(
                db=db,
                organization_id=org_id,
                input=slip_input,
                created_by_user_id=user_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip.slip_id}?saved=1", status_code=303
            )

        except Exception as e:
            db.rollback()
            fallback_start = parse_date(start_date) or date.today()
            fallback_end = parse_date(end_date) or fallback_start
            employees = db.scalars(
                select(Employee)
                .where(
                    Employee.organization_id == org_id,
                    payroll_employee_eligibility_clause(
                        period_start=fallback_start,
                        period_end=fallback_end,
                    ),
                )
                .order_by(Employee.employee_code)
            ).all()

            context = base_context(request, auth, "New Salary Slip", "payroll", db=db)
            context["request"] = request
            context.update(
                {
                    "slip": None,
                    "employees": employees,
                    "error": str(e),
                    "form_data": {
                        "employee_id": employee_id,
                        "start_date": start_date,
                        "end_date": end_date,
                        "posting_date": posting_date,
                        "total_working_days": total_working_days,
                        "absent_days": absent_days,
                        "leave_without_pay": leave_without_pay,
                    },
                    "errors": {},
                }
            )
            return templates.TemplateResponse(
                request, "people/payroll/slip_form.html", context
            )

    async def update_slip_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> Response:
        """Update salary slip."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        employee_id = self._form_str(form, "employee_id")
        start_date = self._form_str(form, "start_date")
        end_date = self._form_str(form, "end_date")
        posting_date = self._form_str(form, "posting_date")
        total_working_days = self._form_str(form, "total_working_days")
        absent_days = self._form_str(form, "absent_days") or "0"
        leave_without_pay = self._form_str(form, "leave_without_pay") or "0"

        if not employee_id or not start_date or not end_date:
            employees = db.scalars(
                select(Employee)
                .where(
                    Employee.organization_id == org_id,
                    payroll_employee_eligibility_clause(
                        period_start=parse_date(start_date) or date.today(),
                        period_end=parse_date(end_date) or date.today(),
                    ),
                )
                .order_by(Employee.employee_code)
            ).all()
            context = base_context(request, auth, "Edit Salary Slip", "payroll", db=db)
            context["request"] = request
            context.update(
                {
                    "slip": db.get(SalarySlip, parse_uuid(slip_id))
                    if parse_uuid(slip_id)
                    else None,
                    "employees": employees,
                    "error": "Employee, period start, and period end are required.",
                    "form_data": {
                        "employee_id": employee_id,
                        "start_date": start_date,
                        "end_date": end_date,
                        "posting_date": posting_date,
                        "total_working_days": total_working_days,
                        "absent_days": absent_days,
                        "leave_without_pay": leave_without_pay,
                    },
                    "errors": {},
                }
            )
            return templates.TemplateResponse(
                request, "people/payroll/slip_form.html", context
            )

        try:
            start = parse_date(start_date)
            end = parse_date(end_date)
            posting = parse_date(posting_date)
            if start is None or end is None:
                raise ValueError("Invalid start or end date")

            slip_input = SalarySlipInput(
                employee_id=coerce_uuid(employee_id),
                start_date=start,
                end_date=end,
                posting_date=posting,
                total_working_days=parse_decimal(total_working_days),
                absent_days=parse_decimal(absent_days) or Decimal("0"),
                leave_without_pay=parse_decimal(leave_without_pay) or Decimal("0"),
            )

            slip = salary_slip_service.update_salary_slip(
                db=db,
                organization_id=org_id,
                slip_id=coerce_uuid(slip_id),
                input=slip_input,
                updated_by_user_id=user_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip.slip_id}?saved=1", status_code=303
            )

        except Exception as e:
            db.rollback()
            fallback_start = parse_date(start_date) or date.today()
            fallback_end = parse_date(end_date) or fallback_start
            employees = db.scalars(
                select(Employee)
                .where(
                    Employee.organization_id == org_id,
                    payroll_employee_eligibility_clause(
                        period_start=fallback_start,
                        period_end=fallback_end,
                    ),
                )
                .order_by(Employee.employee_code)
            ).all()

            context = base_context(request, auth, "Edit Salary Slip", "payroll", db=db)
            context["request"] = request
            context.update(
                {
                    "slip": db.get(SalarySlip, parse_uuid(slip_id))
                    if parse_uuid(slip_id)
                    else None,
                    "employees": employees,
                    "error": str(e),
                    "form_data": {
                        "employee_id": employee_id,
                        "start_date": start_date,
                        "end_date": end_date,
                        "posting_date": posting_date,
                        "total_working_days": total_working_days,
                        "absent_days": absent_days,
                        "leave_without_pay": leave_without_pay,
                    },
                    "errors": {},
                }
            )
            return templates.TemplateResponse(
                request, "people/payroll/slip_form.html", context
            )

    def slip_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> Response:
        """Render salary slip detail page with PAYE breakdown."""
        org_id = coerce_uuid(auth.organization_id)
        s_id = parse_uuid(slip_id)

        if not s_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+saved+successfully",
                status_code=303,
            )

        slip = db.get(SalarySlip, s_id)
        if not slip or slip.organization_id != org_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+saved+successfully",
                status_code=303,
            )

        # Calculate PAYE breakdown for display
        paye_breakdown = None
        tax_profile = None
        skip_deductions = False

        employee = db.get(Employee, slip.employee_id) if slip.employee_id else None
        structure = (
            db.get(SalaryStructure, slip.structure_id) if slip.structure_id else None
        )
        if employee and structure:
            classification = classify_payroll_employment_type(
                db,
                organization_id=org_id,
                employment_type_id=employee.employment_type_id,
            )
            skip_deductions = classification.is_contract_staff(
                structure_name=structure.structure_name
            )

        if slip.employee_id:
            tax_profile = db.scalar(
                select(EmployeeTaxProfile).where(
                    EmployeeTaxProfile.organization_id == org_id,
                    EmployeeTaxProfile.employee_id == slip.employee_id,
                    EmployeeTaxProfile.effective_to.is_(None),
                )
            )

            # Calculate PAYE breakdown if we have gross pay
            if slip.gross_pay > 0 and not skip_deductions:
                calculator = PAYECalculator(db)
                basic_estimate = slip.gross_pay * Decimal("0.6")

                paye_breakdown = calculator.calculate(
                    organization_id=org_id,
                    gross_monthly=slip.gross_pay,
                    basic_monthly=basic_estimate,
                    annual_rent=tax_profile.annual_rent
                    if tax_profile
                    else Decimal("0"),
                    rent_verified=tax_profile.rent_receipt_verified
                    if tax_profile
                    else False,
                    pension_rate=tax_profile.pension_rate
                    if tax_profile
                    else Decimal("0.08"),
                    nhf_rate=tax_profile.nhf_rate if tax_profile else Decimal("0.025"),
                    nhis_rate=tax_profile.nhis_rate if tax_profile else Decimal("0"),
                )

        context = base_context(request, auth, "Salary Slip", "payroll", db=db)
        context["request"] = request
        error = request.query_params.get("error")
        success = request.query_params.get("success")
        context.update(
            {
                "slip": slip,
                "paye_breakdown": paye_breakdown,
                "tax_profile": tax_profile,
                "error": error,
                "success": success,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/slip_detail.html", context
        )

    def submit_slip_response(
        self,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> RedirectResponse:
        """Submit salary slip for approval."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)

        try:
            salary_slip_service.submit_salary_slip(
                db=db,
                organization_id=org_id,
                slip_id=coerce_uuid(slip_id),
                submitted_by_user_id=user_id,
            )
            db.commit()
        except Exception as e:
            db.rollback()
            message = getattr(e, "detail", None) or str(e)
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip_id}?error={quote(message)}",
                status_code=303,
            )

        return RedirectResponse(
            url=f"/people/payroll/slips/{slip_id}?saved=1", status_code=303
        )

    def approve_slip_response(
        self,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> RedirectResponse:
        """Approve salary slip."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)

        try:
            salary_slip_service.approve_salary_slip(
                db=db,
                organization_id=org_id,
                slip_id=coerce_uuid(slip_id),
                approved_by_user_id=user_id,
            )
            db.commit()
        except Exception as e:
            db.rollback()
            message = getattr(e, "detail", None) or str(e)
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip_id}?error={quote(message)}",
                status_code=303,
            )

        return RedirectResponse(
            url=f"/people/payroll/slips/{slip_id}?saved=1", status_code=303
        )

    def post_slip_response(
        self,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
        posting_date: str | None = None,
    ) -> RedirectResponse:
        """Post salary slip to GL."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)

        post_date = parse_date(posting_date) or date.today()
        s_id = coerce_uuid(slip_id)

        try:
            PayrollGLAdapter.post_salary_slip(
                db=db,
                organization_id=org_id,
                slip_id=s_id,
                posting_date=post_date,
                posted_by_user_id=user_id,
            )

            posted_slip = db.get(SalarySlip, s_id)
            if (
                posted_slip
                and posted_slip.organization_id == org_id
                and posted_slip.employee
            ):
                from app.services.people.payroll.payroll_notifications import (
                    PayrollNotificationService,
                )

                PayrollNotificationService(db).notify_payslip_posted(
                    posted_slip,
                    posted_slip.employee,
                    queue_email=True,
                )

            db.commit()
        except Exception as e:
            db.rollback()
            message = getattr(e, "detail", None) or str(e)
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip_id}?error={quote(message)}",
                status_code=303,
            )

        return RedirectResponse(
            url=f"/people/payroll/slips/{slip_id}?saved=1", status_code=303
        )

    def delete_slip_response(
        self,
        auth: WebAuthContext,
        db: Session,
        slip_id: str,
    ) -> RedirectResponse:
        """Delete a draft salary slip."""
        org_id = coerce_uuid(auth.organization_id)
        s_id = parse_uuid(slip_id)

        if not s_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+deleted+successfully",
                status_code=303,
            )

        slip = db.get(SalarySlip, s_id)
        if not slip or slip.organization_id != org_id:
            return RedirectResponse(
                url="/people/payroll/slips?success=Record+deleted+successfully",
                status_code=303,
            )

        if slip.status != SalarySlipStatus.DRAFT:
            return RedirectResponse(
                url=f"/people/payroll/slips/{slip_id}?saved=1", status_code=303
            )

        try:
            db.delete(slip)
            db.commit()
        except Exception:
            db.rollback()

        return RedirectResponse(
            url="/people/payroll/slips?success=Record+deleted+successfully",
            status_code=303,
        )
