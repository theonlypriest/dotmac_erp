"""
Payroll Web Service - Payroll Run/Entry operations.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, timedelta
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.finance.banking.bank_account import BankAccount
from app.models.finance.core_org import Organization
from app.models.finance.gl.account import Account
from app.models.people.hr.department import Department
from app.models.people.hr.designation import Designation
from app.models.people.hr.employee import Employee
from app.models.people.payroll.payroll_entry import PayrollEntry, PayrollEntryStatus
from app.models.people.payroll.salary_assignment import SalaryStructureAssignment
from app.models.people.payroll.salary_slip import SalarySlip, SalarySlipStatus
from app.models.people.payroll.salary_structure import PayrollFrequency, SalaryStructure
from app.services.common import PaginationParams, coerce_uuid, paginate
from app.services.finance.platform.org_context import org_context_service
from app.services.people.hr.employment_types import (
    EmploymentTypeService,
    EmploymentTypeView,
)
from app.services.people.payroll.eligibility import payroll_employee_eligibility_clause
from app.services.people.payroll.payroll_service import (
    PayrollService,
)
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

from .base import (
    DEFAULT_PAGE_SIZE,
    ENTRY_STATUSES,
    PAYROLL_FREQUENCIES,
    SLIP_STATUSES,
    parse_date,
    parse_entry_status,
    parse_payroll_frequency,
    parse_slip_status,
    parse_uuid,
)

logger = logging.getLogger(__name__)


class RunWebService:
    """Service for payroll run web views."""

    @staticmethod
    def _form_text(value: object | None, default: str = "") -> str:
        if isinstance(value, str):
            return value.strip()
        return default

    def _get_default_frequency(self, db: Session, org_id) -> PayrollFrequency:
        org = db.get(Organization, org_id) if org_id else None
        if org and org.hr_payroll_frequency:
            parsed = parse_payroll_frequency(org.hr_payroll_frequency)
            if parsed:
                return parsed
        return PayrollFrequency.MONTHLY

    @staticmethod
    def _get_next_period(
        frequency: PayrollFrequency,
        start: date,
        end: date,
    ) -> tuple[date, date]:
        """Calculate the next pay period following the given source period."""
        if frequency == PayrollFrequency.MONTHLY:
            if start.month == 12:
                next_start = start.replace(year=start.year + 1, month=1, day=1)
            else:
                next_start = start.replace(month=start.month + 1, day=1)
            last_day = monthrange(next_start.year, next_start.month)[1]
            next_end = next_start.replace(day=last_day)
        elif frequency == PayrollFrequency.SEMIMONTHLY:
            if start.day <= 15:
                next_start = start.replace(day=16)
                last_day = monthrange(start.year, start.month)[1]
                next_end = start.replace(day=last_day)
            else:
                if start.month == 12:
                    next_start = start.replace(year=start.year + 1, month=1, day=1)
                else:
                    next_start = start.replace(month=start.month + 1, day=1)
                next_end = next_start.replace(day=15)
        elif frequency == PayrollFrequency.BIWEEKLY:
            next_start = start + timedelta(days=14)
            next_end = end + timedelta(days=14)
        else:  # WEEKLY
            next_start = start + timedelta(days=7)
            next_end = end + timedelta(days=7)
        return next_start, next_end

    @staticmethod
    def _get_default_period(
        frequency: PayrollFrequency,
        today: date,
    ) -> tuple[date, date]:
        if frequency == PayrollFrequency.WEEKLY:
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
        elif frequency == PayrollFrequency.BIWEEKLY:
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=13)
        elif frequency == PayrollFrequency.SEMIMONTHLY:
            if today.day <= 15:
                start = today.replace(day=1)
                end = today.replace(day=15)
            else:
                start = today.replace(day=16)
                last_day = monthrange(today.year, today.month)[1]
                end = today.replace(day=last_day)
        else:
            start = today.replace(day=1)
            last_day = monthrange(today.year, today.month)[1]
            end = today.replace(day=last_day)
        return start, end

    def _count_active_assignments(
        self,
        db: Session,
        org_id,
        effective_date: date,
    ) -> int:
        return (
            db.scalar(
                select(func.count(SalaryStructureAssignment.assignment_id))
                .join(
                    Employee,
                    SalaryStructureAssignment.employee_id == Employee.employee_id,
                )
                .where(SalaryStructureAssignment.organization_id == org_id)
                .where(SalaryStructureAssignment.from_date <= effective_date)
                .where(
                    or_(
                        SalaryStructureAssignment.to_date.is_(None),
                        SalaryStructureAssignment.to_date >= effective_date,
                    )
                )
                .where(
                    payroll_employee_eligibility_clause(
                        period_start=effective_date,
                        period_end=effective_date,
                    )
                )
            )
            or 0
        )

    @staticmethod
    def _list_employment_types_for_filter(
        db: Session,
        org_id: UUID,
    ) -> list[EmploymentTypeView]:
        """Load the complete, name-ordered active People-owned catalogue."""
        return list(EmploymentTypeService(db, org_id).iter_all(active=True))

    def list_runs_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        status: str | None = None,
        year: int | None = None,
        month: int | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """Render payroll runs list page."""
        org_id = coerce_uuid(auth.organization_id)
        per_page = DEFAULT_PAGE_SIZE
        query = select(PayrollEntry).where(PayrollEntry.organization_id == org_id)

        status_enum = parse_entry_status(status)
        if status_enum:
            query = query.where(PayrollEntry.status == status_enum)

        if year:
            query = query.where(PayrollEntry.payroll_year == year)

        if month:
            query = query.where(PayrollEntry.payroll_month == month)

        result = paginate(
            db,
            query.order_by(PayrollEntry.created_at.desc()),
            PaginationParams.from_page(page, per_page),
        )
        entries = result.items

        # Get statistics
        draft_count = (
            db.scalar(
                select(func.count(PayrollEntry.entry_id)).where(
                    PayrollEntry.organization_id == org_id,
                    PayrollEntry.status == PayrollEntryStatus.DRAFT,
                )
            )
            or 0
        )
        pending_count = (
            db.scalar(
                select(func.count(PayrollEntry.entry_id)).where(
                    PayrollEntry.organization_id == org_id,
                    PayrollEntry.status == PayrollEntryStatus.PENDING,
                )
            )
            or 0
        )
        status_counts = {}
        for entry_status in ENTRY_STATUSES:
            try:
                status_enum = parse_entry_status(entry_status)
                if status_enum:
                    status_counts[entry_status] = (
                        db.scalar(
                            select(func.count(PayrollEntry.entry_id)).where(
                                PayrollEntry.organization_id == org_id,
                                PayrollEntry.status == status_enum,
                            )
                        )
                        or 0
                    )
            except Exception:
                status_counts[entry_status] = 0

        context = base_context(request, auth, "Payroll Runs", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "runs": entries,
                "status": status,
                "year": year,
                "month": month,
                "page": page,
                "total_pages": result.total_pages,
                "total": result.total,
                "has_prev": result.has_prev,
                "has_next": result.has_next,
                "statuses": ENTRY_STATUSES,
                "draft_count": draft_count,
                "pending_count": pending_count,
                "status_counts": status_counts,
            }
        )
        return templates.TemplateResponse(request, "people/payroll/runs.html", context)

    def run_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """Render new payroll run form."""
        org_id = coerce_uuid(auth.organization_id)

        departments = db.scalars(
            select(Department)
            .where(Department.organization_id == org_id, Department.is_active.is_(True))
            .order_by(Department.department_name)
        ).all()

        designations = db.scalars(
            select(Designation)
            .where(
                Designation.organization_id == org_id,
                Designation.is_active.is_(True),
            )
            .order_by(Designation.designation_name)
        ).all()
        employment_types = self._list_employment_types_for_filter(db, org_id)

        structures = db.scalars(
            select(SalaryStructure)
            .where(
                SalaryStructure.organization_id == org_id,
                SalaryStructure.is_active.is_(True),
            )
            .order_by(SalaryStructure.structure_name)
        ).all()
        bank_accounts = db.scalars(
            select(BankAccount)
            .where(BankAccount.organization_id == org_id)
            .order_by(BankAccount.bank_name, BankAccount.account_name)
        ).all()

        # Expense accounts for GL posting (account codes starting with 6)
        expense_accounts = db.scalars(
            select(Account)
            .where(
                Account.organization_id == org_id,
                Account.is_active.is_(True),
                Account.account_code.like("6%"),
            )
            .order_by(Account.account_code)
        ).all()

        today = date.today()
        frequency = self._get_default_frequency(db, org_id)
        default_start, default_end = self._get_default_period(frequency, today)
        assigned_count = self._count_active_assignments(db, org_id, default_start)

        context = base_context(request, auth, "New Payroll Run", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "entry": None,
                "run": None,
                "departments": departments,
                "designations": designations,
                "employment_types": employment_types,
                "structures": structures,
                "bank_accounts": bank_accounts,
                "expense_accounts": expense_accounts,
                "current_year": today.year,
                "current_month": today.month,
                "frequencies": PAYROLL_FREQUENCIES,
                "assigned_count": assigned_count,
                "default_start": default_start.isoformat(),
                "default_end": default_end.isoformat(),
                "default_posting": today.isoformat(),
                "form_data": {
                    "payroll_year": today.year,
                    "payroll_month": today.month,
                },
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/run_form.html", context
        )

    def copy_run_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> HTMLResponse | RedirectResponse:
        """Render new payroll run form pre-populated from an existing run."""
        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        source = db.get(PayrollEntry, e_id)
        if not source or source.organization_id != org_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        # Calculate next period from source run
        next_start, next_end = self._get_next_period(
            source.payroll_frequency, source.start_date, source.end_date
        )

        # Load dropdowns (same as run_new_form_response)
        departments = db.scalars(
            select(Department)
            .where(Department.organization_id == org_id, Department.is_active.is_(True))
            .order_by(Department.department_name)
        ).all()

        designations = db.scalars(
            select(Designation)
            .where(
                Designation.organization_id == org_id,
                Designation.is_active.is_(True),
            )
            .order_by(Designation.designation_name)
        ).all()
        employment_types = self._list_employment_types_for_filter(db, org_id)
        copy_employment_type = (
            EmploymentTypeService(db, org_id).get_employment_type(
                source.employment_type_id
            )
            if source.employment_type_id
            else None
        )

        structures = db.scalars(
            select(SalaryStructure)
            .where(
                SalaryStructure.organization_id == org_id,
                SalaryStructure.is_active.is_(True),
            )
            .order_by(SalaryStructure.structure_name)
        ).all()
        bank_accounts = db.scalars(
            select(BankAccount)
            .where(BankAccount.organization_id == org_id)
            .order_by(BankAccount.bank_name, BankAccount.account_name)
        ).all()
        expense_accounts = db.scalars(
            select(Account)
            .where(
                Account.organization_id == org_id,
                Account.is_active.is_(True),
                Account.account_code.like("6%"),
            )
            .order_by(Account.account_code)
        ).all()

        assigned_count = self._count_active_assignments(db, org_id, next_start)

        # Pre-populate form_data from source run
        form_data: dict[str, str | int | None] = {
            "department_id": str(source.department_id) if source.department_id else "",
            "designation_id": str(source.designation_id)
            if source.designation_id
            else "",
            "employment_type_id": str(source.employment_type_id)
            if source.employment_type_id
            else "",
            "bank_account_id": str(source.source_bank_account_id)
            if source.source_bank_account_id
            else "",
            "expense_account_id": str(source.expense_account_id)
            if source.expense_account_id
            else "",
            "payroll_frequency": source.payroll_frequency.value,
            "currency_code": source.currency_code,
            "notes": source.notes or "",
        }

        context = base_context(request, auth, "New Payroll Run", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "entry": None,
                "run": None,
                "departments": departments,
                "designations": designations,
                "employment_types": employment_types,
                "structures": structures,
                "bank_accounts": bank_accounts,
                "expense_accounts": expense_accounts,
                "current_year": next_start.year,
                "current_month": next_start.month,
                "frequencies": PAYROLL_FREQUENCIES,
                "assigned_count": assigned_count,
                "default_start": next_start.isoformat(),
                "default_end": next_end.isoformat(),
                "default_posting": next_end.isoformat(),
                "form_data": form_data,
                "copy_from": source,
                "copy_employment_type": copy_employment_type,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/run_form.html", context
        )

    async def create_run_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Create new payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        coerce_uuid(auth.user_id)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        entry_name = self._form_text(form.get("entry_name"))
        department_id = self._form_text(form.get("department_id"))
        designation_id = self._form_text(form.get("designation_id"))
        employment_type_id = self._form_text(form.get("employment_type_id"))
        structure_id = self._form_text(form.get("structure_id"))
        bank_account_id = self._form_text(form.get("bank_account_id"))
        expense_account_id = self._form_text(form.get("expense_account_id"))
        payroll_frequency = self._form_text(form.get("payroll_frequency"))
        currency_code = self._form_text(form.get("currency_code"))
        start_date_str = self._form_text(form.get("start_date"))
        end_date_str = self._form_text(form.get("end_date"))
        posting_date_str = self._form_text(form.get("posting_date"))
        notes = self._form_text(form.get("notes"))

        try:
            svc = PayrollService(db)
            start_date = parse_date(start_date_str)
            end_date = parse_date(end_date_str)
            posting_date = parse_date(posting_date_str) or date.today()
            if not start_date or not end_date:
                raise ValueError("Start date and end date are required")

            parsed_frequency = parse_payroll_frequency(payroll_frequency)
            entry = svc.create_payroll_entry(
                org_id,
                posting_date=posting_date,
                start_date=start_date,
                end_date=end_date,
                source_bank_account_id=parse_uuid(bank_account_id)
                if bank_account_id
                else None,
                expense_account_id=parse_uuid(expense_account_id)
                if expense_account_id
                else None,
                department_id=parse_uuid(department_id) if department_id else None,
                designation_id=parse_uuid(designation_id) if designation_id else None,
                employment_type_id=parse_uuid(employment_type_id)
                if employment_type_id
                else None,
                payroll_frequency=parsed_frequency or PayrollFrequency.MONTHLY,
                currency_code=currency_code
                or org_context_service.get_functional_currency(db, org_id),
                notes=notes or entry_name or None,
            )
            db.commit()
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry.entry_id}?saved=1", status_code=303
            )

        except Exception as e:
            db.rollback()
            return self._render_run_form_with_error(
                request,
                auth,
                db,
                str(e),
                {
                    "entry_name": entry_name,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "structure_id": structure_id,
                    "bank_account_id": bank_account_id,
                    "expense_account_id": expense_account_id,
                    "payroll_frequency": payroll_frequency,
                    "currency_code": currency_code,
                    "start_date": start_date_str,
                    "end_date": end_date_str,
                    "posting_date": posting_date_str,
                    "notes": notes or entry_name,
                },
            )

    def run_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        success: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse | RedirectResponse:
        """Render payroll run detail page."""
        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return RedirectResponse(
                url="/people/payroll/runs?success=Record+saved+successfully",
                status_code=303,
            )

        entry = db.get(PayrollEntry, e_id)
        if not entry or entry.organization_id != org_id:
            return RedirectResponse(
                url="/people/payroll/runs?success=Record+saved+successfully",
                status_code=303,
            )

        # Get associated slips
        search = self._form_text(request.query_params.get("search"))
        status_value = self._form_text(request.query_params.get("status"))
        status_filter = parse_slip_status(status_value)
        try:
            page = int(request.query_params.get("page", "1"))
        except (TypeError, ValueError):
            page = 1
        page = max(1, page)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except (TypeError, ValueError):
            limit = 50
        if limit not in {25, 50, 100, 200}:
            limit = 50

        slip_conditions = [SalarySlip.payroll_entry_id == e_id]
        if search:
            like = f"%{search}%"
            slip_conditions.append(
                or_(
                    SalarySlip.employee_name.ilike(like),
                    SalarySlip.slip_number.ilike(like),
                )
            )
        if status_filter:
            slip_conditions.append(SalarySlip.status == status_filter)

        total_count = (
            db.scalar(select(func.count(SalarySlip.slip_id)).where(*slip_conditions))
            or 0
        )
        total_pages = max(1, (total_count + limit - 1) // limit)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * limit

        slips = db.scalars(
            select(SalarySlip)
            .where(*slip_conditions)
            .order_by(SalarySlip.employee_name)
            .offset(offset)
            .limit(limit)
        ).all()

        slip_status_counts = {}
        for status, count in db.execute(
            select(SalarySlip.status, func.count())
            .where(SalarySlip.payroll_entry_id == e_id)
            .group_by(SalarySlip.status)
        ).all():
            slip_status_counts[status.value] = count

        total_slips = sum(slip_status_counts.values())

        # Get active bank accounts for bank upload dropdown
        bank_accounts = db.scalars(
            select(BankAccount)
            .where(BankAccount.organization_id == org_id)
            .order_by(BankAccount.bank_name, BankAccount.account_name)
        ).all()

        # Check readiness for DRAFT runs
        readiness_report = None
        if (
            entry.status == PayrollEntryStatus.DRAFT
            and entry.start_date
            and entry.end_date
        ):
            try:
                from app.services.people.payroll.data_completeness import (
                    PayrollReadinessService,
                )

                readiness_svc = PayrollReadinessService(db)
                readiness_report = readiness_svc.check_readiness(
                    org_id,
                    entry.start_date,
                    entry.end_date,
                    department_id=entry.department_id,
                    check_attendance=False,
                )
            except Exception:
                logger.exception("Failed to check payroll readiness")

        context = base_context(
            request, auth, entry.entry_name or "Payroll Run", "payroll", db=db
        )
        context["request"] = request
        context.update(
            {
                "entry": entry,
                "slips": slips,
                "bank_accounts": bank_accounts,
                "success": success,
                "error": error,
                "slip_search": search,
                "slip_status": status_value if status_filter else "",
                "slip_statuses": SLIP_STATUSES,
                "slip_status_counts": slip_status_counts,
                "filtered_slip_count": total_count,
                "total_slip_count": total_slips,
                "search": search,
                "page": page,
                "total_pages": total_pages,
                "total_count": total_count,
                "limit": limit,
                "readiness_report": readiness_report,
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/run_detail.html", context
        )

    def generate_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse:
        """Generate salary slips for payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            try:
                svc = PayrollService(db)
                svc.generate_salary_slips(
                    org_id,
                    e_id,
                    created_by_id=user_id,
                )
                db.commit()
            except Exception:
                db.rollback()

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def regenerate_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse:
        """Regenerate salary slips for payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            try:
                svc = PayrollService(db)
                svc.regenerate_salary_slips(
                    org_id,
                    e_id,
                    created_by_id=user_id,
                )
                db.commit()
            except Exception:
                db.rollback()

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def submit_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse:
        """Submit payroll run for approval."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            try:
                svc = PayrollService(db)
                svc.submit_payroll_entry(
                    org_id,
                    e_id,
                    submitted_by=user_id,
                )
                db.commit()
            except Exception as e:
                db.rollback()
                return RedirectResponse(
                    url=f"/people/payroll/runs/{entry_id}?error={quote(str(e))}",
                    status_code=303,
                )

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def approve_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse:
        """Approve payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            try:
                svc = PayrollService(db)
                svc.approve_payroll_entry(
                    org_id,
                    e_id,
                    approved_by=user_id,
                )
                db.commit()
            except Exception as e:
                db.rollback()
                return RedirectResponse(
                    url=f"/people/payroll/runs/{entry_id}?error={quote(str(e))}",
                    status_code=303,
                )

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def post_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        posting_date: str | None = None,
    ) -> RedirectResponse:
        """Post payroll run to GL."""
        org_id = coerce_uuid(auth.organization_id)
        user_id = coerce_uuid(auth.user_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            try:
                svc = PayrollService(db)
                svc.handoff_payroll_to_books(
                    org_id,
                    e_id,
                    posting_date=parse_date(posting_date) or date.today(),
                    user_id=user_id,
                )
                db.commit()
            except Exception as e:
                db.rollback()
                return RedirectResponse(
                    url=f"/people/payroll/runs/{entry_id}?error={quote(str(e))}",
                    status_code=303,
                )

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def delete_run_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse:
        """Delete payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if e_id:
            entry = db.get(PayrollEntry, e_id)
            if (
                entry
                and entry.organization_id == org_id
                and entry.status
                in (PayrollEntryStatus.DRAFT, PayrollEntryStatus.SLIPS_CREATED)
            ):
                # Delete associated slips first
                db.execute(
                    delete(SalarySlip).where(SalarySlip.payroll_entry_id == e_id)
                )
                db.delete(entry)
                db.commit()
                return RedirectResponse(
                    url="/people/payroll/runs?success=Record+deleted+successfully",
                    status_code=303,
                )

        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?saved=1", status_code=303
        )

    def _render_run_form_with_error(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        error: str,
        form_data: dict,
    ) -> HTMLResponse:
        """Render run form with error."""
        org_id = coerce_uuid(auth.organization_id)

        departments = db.scalars(
            select(Department)
            .where(Department.organization_id == org_id, Department.is_active.is_(True))
            .order_by(Department.department_name)
        ).all()

        designations = db.scalars(
            select(Designation)
            .where(
                Designation.organization_id == org_id,
                Designation.is_active.is_(True),
            )
            .order_by(Designation.designation_name)
        ).all()
        employment_types = self._list_employment_types_for_filter(db, org_id)

        structures = db.scalars(
            select(SalaryStructure)
            .where(
                SalaryStructure.organization_id == org_id,
                SalaryStructure.is_active.is_(True),
            )
            .order_by(SalaryStructure.structure_name)
        ).all()

        bank_accounts = db.scalars(
            select(BankAccount)
            .where(BankAccount.organization_id == org_id)
            .order_by(BankAccount.bank_name, BankAccount.account_name)
        ).all()

        expense_accounts = db.scalars(
            select(Account)
            .where(
                Account.organization_id == org_id,
                Account.is_active.is_(True),
                Account.account_code.like("6%"),
            )
            .order_by(Account.account_code)
        ).all()

        today = date.today()
        frequency = self._get_default_frequency(db, org_id)
        default_start, default_end = self._get_default_period(frequency, today)
        start_date = parse_date(form_data.get("start_date")) if form_data else None
        end_date = parse_date(form_data.get("end_date")) if form_data else None
        posting_date = parse_date(form_data.get("posting_date")) if form_data else None
        effective_start = start_date or default_start
        assigned_count = self._count_active_assignments(db, org_id, effective_start)

        context = base_context(request, auth, "New Payroll Run", "payroll", db=db)
        context["request"] = request
        context.update(
            {
                "entry": None,
                "run": None,
                "departments": departments,
                "designations": designations,
                "employment_types": employment_types,
                "structures": structures,
                "bank_accounts": bank_accounts,
                "expense_accounts": expense_accounts,
                "current_year": today.year,
                "current_month": today.month,
                "frequencies": PAYROLL_FREQUENCIES,
                "assigned_count": assigned_count,
                "default_start": (start_date or default_start).isoformat(),
                "default_end": (end_date or default_end).isoformat(),
                "default_posting": (posting_date or today).isoformat(),
                "form_data": form_data,
                "error": error,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "people/payroll/run_form.html", context
        )

    def bank_upload_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        source_account_id: str | None = None,
    ):
        """
        Generate bank upload file for payroll run (Zenith Bank format).

        Args:
            auth: Web auth context
            db: Database session
            entry_id: Payroll entry ID
            source_account_id: Bank account ID for the debit account

        Returns:
            StreamingResponse with CSV file
        """
        import io

        from app.services.finance.banking.bank_upload import (
            BankUploadService,
        )
        from app.services.people.payroll.bank_payment_file import (
            PayrollBankFileIncomplete,
            PayrollBankFileUnresolved,
            payment_items_for_slips,
            require_resolved_bank_codes,
        )

        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return RedirectResponse(
                url="/people/payroll/runs?success=Record+saved+successfully",
                status_code=303,
            )

        entry = db.get(PayrollEntry, e_id)
        if not entry or entry.organization_id != org_id:
            return RedirectResponse(
                url="/people/payroll/runs?success=Record+saved+successfully",
                status_code=303,
            )

        # Resolve source bank account (prefer run selection, fallback to query param)
        resolved_source_id = source_account_id or (
            str(entry.source_bank_account_id) if entry.source_bank_account_id else None
        )
        source_account_number = ""
        if resolved_source_id:
            sa_id = parse_uuid(resolved_source_id)
            if sa_id:
                source_bank = db.get(BankAccount, sa_id)
                if source_bank and source_bank.organization_id == org_id:
                    source_account_number = source_bank.account_number or ""
        if not source_account_number:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error=Select a payment bank account to download the file",
                status_code=303,
            )

        # Get all salary slips for this entry
        slips = db.scalars(
            select(SalarySlip)
            .where(SalarySlip.payroll_entry_id == e_id)
            .where(SalarySlip.net_pay > 0)
            .order_by(SalarySlip.employee_name)
        ).all()

        if not slips:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error=No salary slips found",
                status_code=303,
            )

        try:
            payment_items = payment_items_for_slips(
                slips,
                payroll_month=entry.payroll_month,
                payroll_year=entry.payroll_year,
            )
        except PayrollBankFileIncomplete as exc:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote(str(exc))}",
                status_code=303,
            )

        # Generate bank upload (Zenith format only)
        bank_service = BankUploadService(db)
        payment_date = entry.posting_date or date.today()

        result = bank_service.generate_upload(
            items=payment_items,
            source_account_number=source_account_number,
            payment_date=payment_date,
            bank_format="zenith",
            batch_reference=entry.entry_number,
        )
        try:
            require_resolved_bank_codes(result)
        except PayrollBankFileUnresolved as exc:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote(str(exc))}",
                status_code=303,
            )

        # Generate filename with entry info
        entry_suffix = (
            entry.entry_name.lower().replace(" ", "_")
            if entry.entry_name
            else entry.entry_number
        )
        filename = (
            f"bank_upload_zenith_{entry_suffix}_{payment_date.strftime('%Y%m%d')}.xlsx"
        )

        # Return as downloadable file
        return StreamingResponse(
            io.BytesIO(result.content),
            media_type=result.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Row-Count": str(result.row_count),
                "X-Total-Amount": str(result.total_amount),
            },
        )

    def send_payslips_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        force: bool = False,
    ) -> RedirectResponse:
        """Queue payslip emails for all posted slips in a payroll run."""
        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        entry = db.get(PayrollEntry, e_id)
        if not entry or entry.organization_id != org_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        if entry.status != PayrollEntryStatus.POSTED:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote('Payroll must be posted before sending payslips')}",
                status_code=303,
            )

        if entry.payslips_email_status and not force:
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote('Payslips have already been queued')}",
                status_code=303,
            )

        try:
            from datetime import datetime

            from app.tasks.payroll import process_payroll_entry_notifications

            process_payroll_entry_notifications.delay(str(e_id), str(org_id))
            entry.payslips_email_status = "REQUEUED" if force else "QUEUED"
            entry.payslips_email_queued_at = datetime.utcnow()
            if auth.person_id:
                entry.payslips_email_queued_by_id = auth.person_id
            db.commit()
            logger.info(
                "Queued payslip emails for entry %s (force=%s)",
                entry.entry_number,
                force,
            )
        except Exception as e:
            db.rollback()
            logger.exception("Failed to queue payslip emails")
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote(str(e))}",
                status_code=303,
            )

        msg = "Payslip emails re-queued" if force else "Payslip emails queued"
        return RedirectResponse(
            url=f"/people/payroll/runs/{entry_id}?success={quote(msg)}",
            status_code=303,
        )

    def email_status_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> JSONResponse:
        """Return email sending progress for a payroll run as JSON."""
        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return JSONResponse({"error": "Invalid entry ID"}, status_code=400)

        entry = db.get(PayrollEntry, e_id)
        if not entry or entry.organization_id != org_id:
            return JSONResponse({"error": "Not found"}, status_code=404)

        # Count total posted slips
        total = (
            db.scalar(
                select(func.count(SalarySlip.slip_id)).where(
                    SalarySlip.payroll_entry_id == e_id,
                    SalarySlip.status == SalarySlipStatus.POSTED,
                )
            )
            or 0
        )

        # Derive processed count from entry-level status
        status = entry.payslips_email_status or "NOT_QUEUED"
        processed = total if status in ("SENT", "PARTIAL") else 0

        return JSONResponse(
            {
                "total": total,
                "processed": processed,
                "status": status,
            }
        )

    def export_paye_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        paye_format: str = "lirs",
    ) -> RedirectResponse | StreamingResponse:
        """Export PAYE (income tax) data for a payroll run."""
        return self._export_statutory(auth, db, entry_id, "paye", paye_format)

    def export_pension_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse | StreamingResponse:
        """Export pension contribution data for a payroll run."""
        return self._export_statutory(auth, db, entry_id, "pension")

    def export_nhf_response(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
    ) -> RedirectResponse | StreamingResponse:
        """Export NHF contribution data for a payroll run."""
        return self._export_statutory(auth, db, entry_id, "nhf")

    def _export_statutory(
        self,
        auth: WebAuthContext,
        db: Session,
        entry_id: str,
        export_type: str,
        export_format: str | None = None,
    ) -> RedirectResponse | StreamingResponse:
        """Common handler for statutory exports (PAYE, Pension, NHF)."""
        import io

        org_id = coerce_uuid(auth.organization_id)
        e_id = parse_uuid(entry_id)

        if not e_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        entry = db.get(PayrollEntry, e_id)
        if not entry or entry.organization_id != org_id:
            return RedirectResponse(url="/people/payroll/runs", status_code=303)

        if entry.status not in (
            PayrollEntryStatus.POSTED,
            PayrollEntryStatus.APPROVED,
        ):
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote('Run must be approved or posted to export')}",
                status_code=303,
            )

        year = entry.payroll_year or (
            entry.start_date.year if entry.start_date else date.today().year
        )
        month = entry.payroll_month or (
            entry.start_date.month if entry.start_date else date.today().month
        )

        content: bytes
        filename: str
        content_type: str
        selected_format = (export_format or "").strip().lower()

        try:
            if export_type == "paye":
                from app.services.people.payroll.paye_export import (
                    PAYEExportService,
                )

                if selected_format not in {"", "lirs", "fctirs"}:
                    return RedirectResponse(
                        url=f"/people/payroll/runs/{entry_id}?error={quote('Unknown PAYE export format')}",
                        status_code=303,
                    )
                paye_fmt: Literal["lirs", "fctirs"] = (
                    "fctirs" if selected_format == "fctirs" else "lirs"
                )
                paye_r = PAYEExportService(db).generate_export(
                    org_id,
                    year,
                    month,
                    paye_format=paye_fmt,
                    entry_id=e_id,
                )
                content = paye_r.content
                filename = paye_r.filename
                content_type = paye_r.content_type
            elif export_type == "pension":
                from app.services.people.payroll.pension_export import (
                    PensionExportService,
                )

                pen_r = PensionExportService(db).generate_export(
                    org_id, year, month, entry_id=e_id
                )
                content = pen_r.content
                filename = pen_r.filename
                content_type = pen_r.content_type
            elif export_type == "nhf":
                from app.services.people.payroll.nhf_export import (
                    NHFExportService,
                )

                nhf_r = NHFExportService(db).generate_export(
                    org_id, year, month, entry_id=e_id
                )
                content = nhf_r.content
                filename = nhf_r.filename
                content_type = nhf_r.content_type
            else:
                return RedirectResponse(
                    url=f"/people/payroll/runs/{entry_id}?error={quote('Unknown export type')}",
                    status_code=303,
                )
        except Exception as e:
            logger.exception("Failed to generate %s export", export_type)
            return RedirectResponse(
                url=f"/people/payroll/runs/{entry_id}?error={quote(str(e))}",
                status_code=303,
            )

        return StreamingResponse(
            io.BytesIO(content),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
