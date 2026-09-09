"""
Payroll Web Service - Report operations.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.people.hr.department import Department
from app.models.people.hr.employee import Employee
from app.models.people.payroll.salary_slip import SalarySlip, SalarySlipStatus
from app.services.common import coerce_uuid
from app.services.people.payroll.payroll_service import PayrollService
from app.services.people.payroll_reporting import (
    REPORTABLE_SLIP_STATUSES,
    calculate_active_monthly_net_average,
)
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

from .base import parse_date

logger = logging.getLogger(__name__)


class DepartmentSummaryRow(TypedDict):
    department: Department
    slip_count: int
    total_gross: Decimal
    total_net: Decimal


class MonthSummaryRow(TypedDict):
    month: int
    month_name: str
    month_label: str
    start_date: str
    end_date: str
    detail_url: str
    slip_count: int
    total_gross: Decimal
    total_net: Decimal
    total_deductions: Decimal


class ReportWebService:
    """Service for payroll report web views."""

    @staticmethod
    def _reportable_slip_statuses() -> tuple[SalarySlipStatus, ...]:
        """Statuses that represent finalized payroll values for reports."""
        return REPORTABLE_SLIP_STATUSES

    def summary_report_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        year: int | None = None,
        month: int | None = None,
    ) -> HTMLResponse:
        """Render payroll summary report."""
        org_id = coerce_uuid(auth.organization_id)

        start_date = parse_date(request.query_params.get("start_date"))
        end_date = parse_date(request.query_params.get("end_date"))

        if not start_date and not end_date and year and month:
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, month + 1, 1)

        svc = PayrollService(db)
        report = svc.get_payroll_summary_report(
            org_id,
            start_date=start_date,
            end_date=end_date,
        )

        context = base_context(request, auth, "Payroll Summary", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "start_date": report["start_date"],
                "end_date": report["end_date"],
                "report": report,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/reports/summary.html", context
        )

    def by_department_report_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        year: int | None = None,
        month: int | None = None,
    ) -> HTMLResponse:
        """Render payroll by department report."""
        org_id = coerce_uuid(auth.organization_id)

        today = date.today()
        year = year or today.year
        month = month or today.month

        start_date = parse_date(request.query_params.get("start_date"))
        end_date = parse_date(request.query_params.get("end_date"))
        if not start_date and not end_date:
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, month + 1, 1)

        # Get department breakdown
        departments = list(
            db.scalars(
                select(Department)
                .where(Department.organization_id == org_id)
                .order_by(Department.department_name)
            ).all()
        )

        dept_data: list[DepartmentSummaryRow] = []
        for dept in departments:
            result = db.execute(
                select(
                    func.count(SalarySlip.slip_id).label("slip_count"),
                    func.sum(SalarySlip.gross_pay).label("total_gross"),
                    func.sum(SalarySlip.net_pay).label("total_net"),
                )
                .join(Employee, SalarySlip.employee_id == Employee.employee_id)
                .where(
                    SalarySlip.organization_id == org_id,
                    Employee.department_id == dept.department_id,
                    SalarySlip.status.in_(self._reportable_slip_statuses()),
                    SalarySlip.start_date >= start_date,
                    SalarySlip.start_date < end_date,
                )
            ).first()
            if result and result.slip_count and result.slip_count > 0:
                dept_data.append(
                    {
                        "department": dept,
                        "slip_count": result.slip_count or 0,
                        "total_gross": result.total_gross or Decimal("0"),
                        "total_net": result.total_net or Decimal("0"),
                    }
                )

        total_gross = sum((row["total_gross"] for row in dept_data), Decimal("0"))
        total_net = sum((row["total_net"] for row in dept_data), Decimal("0"))
        total_deductions = total_gross - total_net
        dept_rows = []
        for row in dept_data:
            dept_gross = row["total_gross"]
            dept_net = row["total_net"]
            dept_deductions = dept_gross - dept_net
            percentage = (
                (dept_gross / total_gross * Decimal("100"))
                if total_gross
                else Decimal("0")
            )
            dept_rows.append(
                {
                    "department_name": row["department"].department_name,
                    "slip_count": row["slip_count"],
                    "total_gross": dept_gross,
                    "total_net": dept_net,
                    "total_deductions": dept_deductions,
                    "percentage": float(round(percentage, 2)),
                }
            )

        report = {
            "total_departments": len(dept_rows),
            "total_gross": total_gross,
            "total_net": total_net,
            "total_deductions": total_deductions,
            "departments": dept_rows,
        }

        context = base_context(request, auth, "Payroll by Department", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "year": year,
                "month": month,
                "start_date": start_date,
                "end_date": end_date,
                "report": report,
                "dept_data": dept_data,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/reports/by_department.html", context
        )

    def tax_summary_report_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        year: int | None = None,
        month: int | None = None,
    ) -> HTMLResponse:
        """Render tax summary report."""
        org_id = coerce_uuid(auth.organization_id)

        today = date.today()
        year = year or today.year
        month = month or today.month

        start_date = parse_date(request.query_params.get("start_date"))
        end_date = parse_date(request.query_params.get("end_date"))
        if not start_date and not end_date:
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = date(year, month + 1, 1) - timedelta(days=1)

        report = PayrollService(db).get_payroll_tax_summary_report(
            org_id,
            start_date=start_date,
            end_date=end_date,
        )

        context = base_context(request, auth, "Tax Summary Report", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "year": year,
                "month": month,
                "start_date": report["start_date"],
                "end_date": report["end_date"],
                "report": report,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/reports/tax_summary.html", context
        )

    def trends_report_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        year: int | None = None,
        months: int | None = 12,
    ) -> HTMLResponse:
        """Render payroll trends report."""
        org_id = coerce_uuid(auth.organization_id)

        today = date.today()
        months = months or 12

        if year:
            # Calendar year view
            month_starts = [date(year, m, 1) for m in range(1, 13)]
        else:
            # Rolling months view ending this month
            month_starts = []
            cursor_year = today.year
            cursor_month = today.month
            for _ in range(months):
                month_starts.append(date(cursor_year, cursor_month, 1))
                cursor_month -= 1
                if cursor_month == 0:
                    cursor_month = 12
                    cursor_year -= 1
            month_starts.reverse()

        months_data: list[MonthSummaryRow] = []
        for start_date in month_starts:
            if start_date.month == 12:
                end_date = date(start_date.year + 1, 1, 1)
            else:
                end_date = date(start_date.year, start_date.month + 1, 1)
            detail_end_date = end_date - timedelta(days=1)

            result = db.execute(
                select(
                    func.count(SalarySlip.slip_id).label("slip_count"),
                    func.sum(SalarySlip.gross_pay).label("total_gross"),
                    func.sum(SalarySlip.net_pay).label("total_net"),
                ).where(
                    SalarySlip.organization_id == org_id,
                    SalarySlip.status.in_(self._reportable_slip_statuses()),
                    SalarySlip.start_date >= start_date,
                    SalarySlip.start_date < end_date,
                )
            ).first()

            if result is None:
                slip_count = 0
                total_gross = Decimal("0")
                total_net = Decimal("0")
            else:
                slip_count = result.slip_count or 0
                total_gross = result.total_gross or Decimal("0")
                total_net = result.total_net or Decimal("0")

            months_data.append(
                {
                    "month": start_date.month,
                    "month_name": start_date.strftime("%B"),
                    "month_label": start_date.strftime("%b %Y"),
                    "start_date": start_date.isoformat(),
                    "end_date": detail_end_date.isoformat(),
                    "detail_url": (
                        "/people/payroll/slips?"
                        f"start_date={start_date.isoformat()}"
                        f"&end_date={detail_end_date.isoformat()}"
                        "&status_group=reportable"
                    ),
                    "slip_count": slip_count,
                    "total_gross": total_gross,
                    "total_net": total_net,
                    "total_deductions": total_gross - total_net,
                }
            )

        total_months = len(months_data)
        total_gross = sum((m["total_gross"] for m in months_data), Decimal("0"))
        total_net = sum((m["total_net"] for m in months_data), Decimal("0"))
        total_deductions = sum(
            (m["total_deductions"] for m in months_data), Decimal("0")
        )
        average_monthly = calculate_active_monthly_net_average(months_data)

        report = {
            "total_months": total_months,
            "total_gross": total_gross,
            "total_net": total_net,
            "total_deductions": total_deductions,
            "average_monthly": average_monthly,
            "months": months_data,
        }

        context = base_context(request, auth, "Payroll Trends", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "year": year,
                "months": months if not year else 12,
                "report": report,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/reports/trends.html", context
        )
