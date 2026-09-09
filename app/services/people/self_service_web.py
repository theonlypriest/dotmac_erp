"""
Self-service web view service for employees and managers.
"""

from __future__ import annotations

import calendar
import json
import logging
from collections.abc import AsyncIterable, Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import UploadFile

from app.db.session_context import prime_tenant_context
from app.models.people.attendance import Attendance, AttendanceStatus
from app.models.people.exp import (
    ExpenseClaimStatus,
)
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.models.people.hr.employee_extended import (
    DocumentType,
    EmployeeCertification,
    EmployeeDependent,
    EmployeeQualification,
    EmployeeSkill,
    QualificationType,
    RelationshipType,
)
from app.models.people.hr.info_change_request import (
    InfoChangeOperation,
    InfoChangeStatus,
    InfoChangeType,
)
from app.models.people.leave import (
    Holiday,
    HolidayList,
    LeaveApplication,
    LeaveApplicationStatus,
)
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.salary_slip import SalarySlip, SalarySlipStatus
from app.models.people.scheduling import ScheduleStatus, SwapRequestStatus
from app.models.people.scheduling.shift_schedule import ShiftSchedule
from app.models.person import Gender as PersonGender
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.services.common import PaginationParams, ValidationError, coerce_uuid
from app.services.common_filters import build_active_filters
from app.services.expense.limit_service import (
    ExpenseLimitService,
    ExpenseLimitServiceError,
)
from app.services.file_upload import FileUploadError, get_employee_document_upload
from app.services.finance.banking.bank_directory import BankDirectoryService
from app.services.people.attendance import AttendanceService
from app.services.people.attendance.attendance_service import AttendanceServiceError
from app.services.people.expense import (
    ApproverAuthorityError,
    ExpenseClaimStatusError,
    ExpenseService,
    ExpenseServiceError,
)
from app.services.people.hr.employees import EmployeeService
from app.services.people.hr.employee_extended import (
    EmployeeExtendedDataError,
    EmployeeDocumentService,
    EmployeeExtendedSelfServiceService,
)
from app.services.people.hr.employee_types import EmployeeFilters
from app.services.people.hr.info_change_service import (
    DocumentBatchItemInput,
    ExtendedBatchItemInput,
    InfoChangeService,
    PendingEvidence,
)
from app.services.people.hr.org_resolver import OrgResolver
from app.services.people.leave import LeaveService
from app.services.people.leave.leave_service import LeaveServiceError
from app.services.people.payroll.paye_calculator import PAYECalculator
from app.services.people.payroll.pfa_directory import PFADirectoryService
from app.services.people.scheduling import SchedulingService, SwapService
from app.services.settings.bank_directory import OrgBankDirectoryService
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

logger = logging.getLogger(__name__)

DEPARTMENT_DISCIPLINE_READ_PERMISSION = "discipline:department:read"

# Keys carried on a collected form row purely to move request data into the
# submit path. They never belong in template context — see
# SelfServiceWebService._renderable_form_rows.
TRANSPORT_ROW_KEYS = frozenset({"_upload"})


class SelfServiceWebService:
    """View service for employee self-service pages."""

    @staticmethod
    def _rollback_and_reprime(db: Session, organization_id: UUID) -> None:
        """Rollback failed form work and restore both tenant-context layers."""
        db.rollback()
        prime_tenant_context(db, organization_id)

    def index_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """Render self-service landing page with employee/team capabilities."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        context = base_context(request, auth, "Self Service", "self", db=db)

        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException:
            employee_id = None

        has_direct_reports = False
        if employee_id is not None:
            has_direct_reports = bool(self._get_direct_reports(db, org_id, employee_id))

        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_discipline"] = (
            auth.is_admin
            or auth.has_any_permission(
                [
                    "discipline:access",
                    "discipline:cases:read",
                    "discipline:cases:create",
                    "discipline:cases:update",
                    DEPARTMENT_DISCIPLINE_READ_PERMISSION,
                    "discipline:workflow:manage",
                ]
            )
            or has_direct_reports
        )
        return templates.TemplateResponse(request, "people/self/index.html", context)

    @staticmethod
    def _expense_approver_employee_statuses() -> tuple[EmployeeStatus, ...]:
        """Statuses that represent employed staff eligible for expense approval."""
        return (EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE)

    @staticmethod
    def _match_org_bank(
        allowed_banks: list,
        *,
        bank_name: str | None = None,
        bank_code: str | None = None,
    ):
        normalized_name = (bank_name or "").strip().lower()
        normalized_code = (bank_code or "").strip()

        if normalized_code:
            for bank in allowed_banks:
                if (bank.bank_sort_code or "").strip() == normalized_code:
                    return bank

        if normalized_name:
            for bank in allowed_banks:
                if bank.bank_name.strip().lower() == normalized_name:
                    return bank

        return None

    def _resolve_expense_bank_selection(
        self,
        db: Session,
        org_id: UUID,
        *,
        bank_name: str | None = None,
        bank_code: str | None = None,
        required: bool = False,
    ) -> tuple[str, str]:
        allowed_banks = OrgBankDirectoryService(db).list_active_banks(org_id)
        matched_bank = self._match_org_bank(
            allowed_banks,
            bank_name=bank_name,
            bank_code=bank_code,
        )

        if matched_bank:
            return matched_bank.bank_name, matched_bank.bank_sort_code

        if required:
            raise HTTPException(
                status_code=400,
                detail="Select a bank from the approved bank list",
            )

        return "", ""

    @staticmethod
    def _resolve_month_range(
        month_value: str | None, fallback_date: date
    ) -> tuple[date, date, str, str, str]:
        """Resolve the selected month and adjacent month navigation values."""
        if month_value:
            try:
                year, month_num = [int(part) for part in month_value.split("-", 1)]
                start_date = date(year, month_num, 1)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid month format"
                ) from exc
        else:
            start_date = fallback_date.replace(day=1)

        _, last_day = calendar.monthrange(start_date.year, start_date.month)
        end_date = start_date.replace(day=last_day)
        selected_month = start_date.strftime("%Y-%m")

        previous_month = (
            (start_date - timedelta(days=1)).replace(day=1).strftime("%Y-%m")
        )
        next_month = (end_date + timedelta(days=1)).replace(day=1).strftime("%Y-%m")

        return start_date, end_date, selected_month, previous_month, next_month

    def _get_holiday_map(
        self,
        db: Session,
        org_id: UUID,
        start_date: date,
        end_date: date,
    ) -> dict[date, str]:
        """Return holiday dates and names for the selected month."""
        holiday_rows = db.execute(
            select(Holiday.holiday_date, Holiday.holiday_name)
            .join(HolidayList, Holiday.holiday_list_id == HolidayList.holiday_list_id)
            .where(
                HolidayList.organization_id == org_id,
                HolidayList.is_active == True,  # noqa: E712
                HolidayList.from_date <= end_date,
                HolidayList.to_date >= start_date,
                Holiday.holiday_date >= start_date,
                Holiday.holiday_date <= end_date,
            )
            .order_by(Holiday.holiday_date)
        ).all()

        return {
            holiday_date: holiday_name for holiday_date, holiday_name in holiday_rows
        }

    @staticmethod
    def _build_attendance_calendar(
        *,
        month_start: date,
        month_end: date,
        today: date,
        holiday_map: dict[date, str],
        record_by_date: dict[date, Attendance],
        org_tzinfo,
    ) -> tuple[list[list[dict]], dict[str, int]]:
        """Build a month calendar grid with normalized per-day statuses."""

        def _format_time(value: datetime | None) -> str:
            if not value:
                return "-"
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(org_tzinfo).strftime("%H:%M")

        def _format_hours(value: Decimal | None) -> str:
            if value is None:
                return "-"
            return str(value)

        status_meta = {
            "present": (
                "Present",
                "bg-emerald-50 text-emerald-800 border-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:border-emerald-800",
            ),
            "absent": (
                "Absent",
                "bg-rose-50 text-rose-800 border-rose-200 dark:bg-rose-900/20 dark:text-rose-300 dark:border-rose-800",
            ),
            "half_day": (
                "Half Day",
                "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-800",
            ),
            "on_leave": (
                "On Leave",
                "bg-blue-50 text-blue-800 border-blue-200 dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800",
            ),
            "holiday": (
                "Holiday",
                "bg-violet-50 text-violet-800 border-violet-200 dark:bg-violet-900/20 dark:text-violet-300 dark:border-violet-800",
            ),
            "work_from_home": (
                "Work From Home",
                "bg-cyan-50 text-cyan-800 border-cyan-200 dark:bg-cyan-900/20 dark:text-cyan-300 dark:border-cyan-800",
            ),
            "missed": (
                "Missed",
                "bg-slate-100 text-slate-800 border-slate-300 dark:bg-slate-800 dark:text-slate-200 dark:border-slate-700",
            ),
            "future": (
                "Upcoming",
                "bg-white text-slate-500 border-slate-200 dark:bg-slate-900/40 dark:text-slate-400 dark:border-slate-800",
            ),
        }

        calendar_weeks: list[list[dict]] = []
        calendar_counts = {
            "present": 0,
            "missed": 0,
            "absent": 0,
            "half_day": 0,
            "on_leave": 0,
            "holiday": 0,
        }
        month_matrix = calendar.Calendar(firstweekday=0).monthdatescalendar(
            month_start.year, month_start.month
        )

        for week in month_matrix:
            week_days: list[dict] = []
            for day_date in week:
                record = record_by_date.get(day_date)
                holiday_name = holiday_map.get(day_date)

                if record:
                    status_key = {
                        AttendanceStatus.PRESENT: "present",
                        AttendanceStatus.ABSENT: "absent",
                        AttendanceStatus.HALF_DAY: "half_day",
                        AttendanceStatus.ON_LEAVE: "on_leave",
                        AttendanceStatus.HOLIDAY: "holiday",
                        AttendanceStatus.WORK_FROM_HOME: "work_from_home",
                    }.get(record.status, "missed")
                    note = holiday_name or (
                        record.remarks.strip() if record.remarks else None
                    )
                elif holiday_name:
                    status_key = "holiday"
                    note = holiday_name
                elif day_date > today:
                    status_key = "future"
                    note = None
                else:
                    status_key = "missed"
                    note = "Attendance not marked"

                if day_date.month == month_start.month:
                    if status_key == "present":
                        calendar_counts["present"] += 1
                    elif status_key == "missed":
                        calendar_counts["missed"] += 1
                    elif status_key == "absent":
                        calendar_counts["absent"] += 1
                    elif status_key == "half_day":
                        calendar_counts["half_day"] += 1
                    elif status_key == "on_leave":
                        calendar_counts["on_leave"] += 1
                    elif status_key == "holiday":
                        calendar_counts["holiday"] += 1

                label, classes = status_meta[status_key]
                week_days.append(
                    {
                        "date": day_date,
                        "day_number": day_date.day,
                        "is_current_month": day_date.month == month_start.month,
                        "is_today": day_date == today,
                        "is_weekend": day_date.weekday() >= 5,
                        "status_key": status_key,
                        "status_label": label,
                        "status_classes": classes,
                        "holiday_name": holiday_name,
                        "note": note,
                        "check_in_display": _format_time(record.check_in)
                        if record
                        else "-",
                        "check_out_display": _format_time(record.check_out)
                        if record
                        else "-",
                        "working_hours_display": _format_hours(record.working_hours)
                        if record
                        else "-",
                    }
                )
            calendar_weeks.append(week_days)

        return calendar_weeks, calendar_counts

    @staticmethod
    def _has_named_role(db: Session, person_id: UUID, role_names: set[str]) -> bool:
        normalized_names = {name.strip().lower() for name in role_names if name}
        if not normalized_names:
            return False
        rows = db.execute(
            select(Role.name)
            .join(PersonRole, PersonRole.role_id == Role.id)
            .where(
                PersonRole.person_id == person_id,
                Role.is_active == True,  # noqa: E712
            )
        ).all()
        for (raw_name,) in rows:
            if not raw_name:
                continue
            n = raw_name.strip().lower()
            if n in normalized_names or n.replace(" ", "_") in normalized_names:
                return True
        return False

    @staticmethod
    def _nigeria_states() -> list[str]:
        return [
            "Abia",
            "Adamawa",
            "Akwa Ibom",
            "Anambra",
            "Bauchi",
            "Bayelsa",
            "Benue",
            "Borno",
            "Cross River",
            "Delta",
            "Ebonyi",
            "Edo",
            "Ekiti",
            "Enugu",
            "Gombe",
            "Imo",
            "Jigawa",
            "Kaduna",
            "Kano",
            "Katsina",
            "Kebbi",
            "Kogi",
            "Kwara",
            "Lagos",
            "Nasarawa",
            "Niger",
            "Ogun",
            "Ondo",
            "Osun",
            "Oyo",
            "Plateau",
            "Rivers",
            "Sokoto",
            "Taraba",
            "Yobe",
            "Zamfara",
            "FCT",
        ]

    @staticmethod
    def _get_employee_id(
        db: Session, org_id: UUID | None, person_id: UUID | None
    ) -> UUID:
        if org_id is None or person_id is None:
            raise HTTPException(
                status_code=403, detail="Missing organization or person context"
            )
        employee = db.scalar(
            select(Employee).where(
                Employee.organization_id == org_id,
                Employee.person_id == person_id,
            )
        )
        if not employee:
            raise HTTPException(status_code=404, detail="Employee profile not found")
        return employee.employee_id

    @staticmethod
    def _employee_required_response(
        request: Request,
        auth: WebAuthContext,
        db: Session,
        page_title: str,
        active_module: str,
        *,
        detail: str | None = None,
    ) -> HTMLResponse:
        context = base_context(request, auth, page_title, active_module, db=db)
        context["has_team_approvals"] = False
        context["can_team_leave"] = False
        context["can_team_expenses"] = False
        context["error"] = detail or "Employee profile not found."
        return templates.TemplateResponse(
            request, "people/self/employee_required.html", context
        )

    @staticmethod
    def _require_permission(auth: WebAuthContext, permission: str) -> None:
        if not auth.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission '{permission}' required",
            )

    @staticmethod
    def _streaming_download_response(
        chunks: AsyncIterable[str | bytes] | Iterable[str | bytes],
        *,
        content_type: str,
        content_length: int | None,
        filename: str,
    ):
        from fastapi.responses import StreamingResponse

        quoted = quote(filename)
        headers = {
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}"
            )
        }
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        return StreamingResponse(
            chunks,
            media_type=content_type,
            headers=headers,
        )

    @staticmethod
    def _get_expense_approver_options(
        db: Session,
        org_id: UUID,
        employee_id: UUID | None = None,
        selected_approver_id: UUID | None = None,
    ) -> list[dict]:
        """Return valid expense approver options for the given employee.

        Options include all active employees whose roles grant expense claim
        approval permission. For new claims, the employee's reporting manager is
        selected by default only when that manager is also an active expense
        approver. Existing claims can pass selected_approver_id to preserve the
        saved approver.
        """

        def _build_option(emp: Employee, person: Person | None) -> dict[str, object]:
            label = ""
            if person:
                label = (
                    person.name
                    or f"{person.first_name or ''} {person.last_name or ''}".strip()
                )
            if emp.employee_code:
                label = f"{label} ({emp.employee_code})" if label else emp.employee_code
            return {"id": str(emp.employee_id), "label": label or "Unnamed"}

        default_approver_id = selected_approver_id
        if employee_id:
            employee = db.get(Employee, employee_id)
            if employee and default_approver_id is None:
                resolver = OrgResolver(db)
                manager = resolver.get_manager(employee.employee_id, org_id)
                resolver.notify_hr_for_vacancy_routing_alerts(org_id)
                default_approver_id = manager.employee_id if manager else None

        rows = db.execute(
            select(Employee, Person)
            .join(PersonRole, PersonRole.person_id == Employee.person_id)
            .join(Role, Role.id == PersonRole.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .join(Person, Person.id == Employee.person_id)
            .where(
                Employee.organization_id == org_id,
                Employee.status.in_(
                    SelfServiceWebService._expense_approver_employee_statuses()
                ),
                Role.is_active == True,
                or_(
                    func.lower(Role.name) == "admin",
                    and_(
                        Permission.is_active == True,
                        Permission.key.in_(
                            [
                                "expense:claims:approve:tier1",
                                "expense:claims:approve:tier2",
                                "expense:claims:approve:tier3",
                            ]
                        ),
                    ),
                ),
            )
            .order_by(Person.first_name, Person.last_name)
        ).all()

        options: dict[str, dict[str, object]] = {}
        for emp, person in rows:
            option = _build_option(emp, person)
            option["selected"] = str(emp.employee_id) == str(default_approver_id)
            options[str(emp.employee_id)] = option

        return list(options.values())

    @staticmethod
    def _is_active_expense_approver(
        db: Session,
        org_id: UUID,
        approver_id: UUID | None,
    ) -> bool:
        if approver_id is None:
            return False
        return (
            db.scalar(
                select(Employee.employee_id)
                .join(PersonRole, PersonRole.person_id == Employee.person_id)
                .join(Role, Role.id == PersonRole.role_id)
                .outerjoin(RolePermission, RolePermission.role_id == Role.id)
                .outerjoin(Permission, Permission.id == RolePermission.permission_id)
                .where(
                    Employee.organization_id == org_id,
                    Employee.status.in_(
                        SelfServiceWebService._expense_approver_employee_statuses()
                    ),
                    Employee.employee_id == approver_id,
                    Role.is_active == True,
                    or_(
                        func.lower(Role.name) == "admin",
                        and_(
                            Permission.is_active == True,
                            Permission.key.in_(
                                [
                                    "expense:claims:approve:tier1",
                                    "expense:claims:approve:tier2",
                                    "expense:claims:approve:tier3",
                                ]
                            ),
                        ),
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def _validate_expense_approver_selection(
        self,
        db: Session,
        org_id: UUID,
        approver_id: UUID | None,
    ) -> None:
        if not self._is_active_expense_approver(db, org_id, approver_id):
            raise HTTPException(
                status_code=400,
                detail="Select an active expense approver",
            )

    @staticmethod
    def _has_team_approvals(
        db: Session,
        org_id: UUID | None,
        person_id: UUID | None,
        *,
        employee_id: UUID | None = None,
    ) -> bool:
        if org_id is None or person_id is None:
            return False
        has_leave_role = SelfServiceWebService._has_named_role(
            db,
            person_id,
            {"admin", "leave_approver", "Leave approver"},
        )
        if has_leave_role:
            return True
        try:
            manager_employee_id = employee_id or SelfServiceWebService._get_employee_id(
                db, org_id, person_id
            )
        except HTTPException:
            return False

        return bool(
            SelfServiceWebService._get_direct_reports(
                db,
                org_id,
                manager_employee_id,
            )
        )

    @staticmethod
    def _get_direct_reports(
        db: Session,
        org_id: UUID,
        manager_employee_id: UUID,
    ) -> list[Employee]:
        return OrgResolver(db).get_direct_reports(manager_employee_id, org_id)

    @staticmethod
    def _get_direct_report_ids(
        db: Session,
        org_id: UUID,
        manager_employee_id: UUID,
    ) -> set[UUID]:
        return {
            employee.employee_id
            for employee in SelfServiceWebService._get_direct_reports(
                db, org_id, manager_employee_id
            )
        }

    @staticmethod
    def _get_department_discipline_employee_ids(
        db: Session,
        org_id: UUID,
        manager_employee_id: UUID,
    ) -> set[UUID]:
        department_id = db.scalar(
            select(Employee.department_id).where(
                Employee.organization_id == org_id,
                Employee.employee_id == manager_employee_id,
            )
        )
        if department_id is None:
            return set()
        return set(
            db.scalars(
                select(Employee.employee_id).where(
                    Employee.organization_id == org_id,
                    Employee.department_id == department_id,
                )
            ).all()
        )

    def _get_team_discipline_employee_ids(
        self,
        db: Session,
        org_id: UUID,
        manager_employee_id: UUID,
        auth: WebAuthContext,
    ) -> set[UUID]:
        employee_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)
        if auth.has_permission(DEPARTMENT_DISCIPLINE_READ_PERMISSION):
            employee_ids.update(
                self._get_department_discipline_employee_ids(
                    db,
                    org_id,
                    manager_employee_id,
                )
            )
        return employee_ids

    @staticmethod
    def _has_team_expense_approvals(
        db: Session,
        org_id: UUID | None,
        person_id: UUID | None,
        *,
        employee_id: UUID | None = None,
    ) -> bool:
        if org_id is None or person_id is None:
            return False
        has_expense_role = SelfServiceWebService._has_named_role(
            db,
            person_id,
            {"admin", "expense_approver", "Expense approver"},
        )
        if has_expense_role:
            return True
        try:
            approver_employee_id = (
                employee_id
                or SelfServiceWebService._get_employee_id(db, org_id, person_id)
            )
        except HTTPException:
            return False

        employee_svc = EmployeeService(db, org_id)
        reports = employee_svc.list_employees(
            filters=EmployeeFilters(expense_approver_id=approver_employee_id),
            pagination=PaginationParams(offset=0, limit=1),
        )
        return bool(reports.items)

    @staticmethod
    def _save_receipt_file(org_id: UUID, receipt_file: UploadFile) -> str:
        from app.services.file_upload import FileUploadError, get_expense_receipt_upload

        svc = get_expense_receipt_upload()
        file_data = receipt_file.file.read()
        try:
            result = svc.save(
                file_data=file_data,
                content_type=receipt_file.content_type,
                subdirs=(str(org_id),),
                original_filename=receipt_file.filename,
            )
        except FileUploadError:
            raise
        return result.relative_path

    @staticmethod
    def _get_tickets_for_dropdown(db: Session, org_id: UUID) -> list[dict]:
        """Get open/active support tickets for expense linking."""
        from app.models.support.ticket import Ticket, TicketStatus

        tickets = (
            db.execute(
                select(Ticket)
                .where(
                    Ticket.organization_id == org_id,
                    Ticket.status.in_(
                        [TicketStatus.OPEN, TicketStatus.REPLIED, TicketStatus.ON_HOLD]
                    ),
                )
                .order_by(Ticket.opening_date.desc())
                .limit(100)
            )
            .scalars()
            .all()
        )

        return [
            {
                "ticket_id": str(t.ticket_id),
                "ticket_number": t.ticket_number,
                "subject": t.subject,
            }
            for t in tickets
        ]

    @staticmethod
    def _get_projects_for_dropdown(db: Session, org_id: UUID) -> list[dict]:
        """Get active projects for expense linking."""
        try:
            from app.models.finance.core_org.project import Project, ProjectStatus

            projects = (
                db.execute(
                    select(Project)
                    .where(
                        Project.organization_id == org_id,
                        Project.status == ProjectStatus.ACTIVE,
                    )
                    .order_by(Project.project_code)
                )
                .scalars()
                .all()
            )

            return [
                {
                    "project_id": str(p.project_id),
                    "project_code": p.project_code,
                    "project_name": p.project_name,
                }
                for p in projects
            ]
        except Exception:
            # Project model may not exist
            return []

    def tax_info_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        success: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        """Self-service tax, bank, and personal info page."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Tax & Bank Info",
                    "self-tax-info",
                    detail=exc.detail,
                )
            raise

        employee = db.scalar(
            select(Employee)
            .options(joinedload(Employee.person))
            .where(
                Employee.organization_id == org_id,
                Employee.employee_id == employee_id,
            )
        )

        tax_profile = db.scalar(
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.employee_id == employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
            .limit(1)
        )

        banks = BankDirectoryService(db).list_active_banks()
        pfas = PFADirectoryService(db).list_active_pfas()

        info_change_service = InfoChangeService(db)
        has_pending = info_change_service.has_pending_request(org_id, employee_id)
        recent_requests = info_change_service.get_employee_requests(
            org_id,
            employee_id,
            include_resolved=True,
            limit=10,
        )

        context = base_context(request, auth, "Tax & Bank Info", "self-tax-info", db=db)
        context.update(
            {
                "employee": employee,
                "person": employee.person if employee else None,
                "tax_profile": tax_profile,
                "banks": banks,
                "pfas": pfas,
                "nigeria_states": self._nigeria_states(),
                "has_pending": has_pending,
                "recent_requests": recent_requests,
                "success": success,
                "error": error,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/tax_info.html", context)

    def tax_info_submit_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        payload: dict[str, object | None],
    ) -> RedirectResponse:
        """Submit a change request for tax, bank, and personal info."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        info_change_service = InfoChangeService(db)
        if info_change_service.has_pending_request(org_id, employee_id):
            return RedirectResponse(
                url="/people/self/tax-info?error=You+already+have+a+pending+request",
                status_code=303,
            )

        employee = db.scalar(
            select(Employee)
            .options(joinedload(Employee.person))
            .where(
                Employee.organization_id == org_id,
                Employee.employee_id == employee_id,
            )
        )
        if not employee:
            return RedirectResponse(
                url="/people/self/tax-info?error=Employee+profile+not+found",
                status_code=303,
            )

        person = employee.person
        tax_profile = db.scalar(
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.employee_id == employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
            .limit(1)
        )

        def _normalize(value: object | None) -> str | None:
            if value is None:
                return None
            value = str(value).strip()
            if not value:
                return None
            if value.lower() in {"none", "null"}:
                return None
            return value

        proposed_changes: dict[str, object] = {}

        # Person fields
        if person:
            phone = _normalize(payload.get("phone"))
            if phone != (person.phone or None):
                proposed_changes["phone"] = phone

            dob = payload.get("date_of_birth")
            current_dob = person.date_of_birth
            if dob != current_dob:
                proposed_changes["date_of_birth"] = (
                    dob.isoformat() if isinstance(dob, date) else None
                )

            gender_value = _normalize(payload.get("gender"))
            if gender_value:
                try:
                    gender = PersonGender(gender_value)
                except Exception:
                    return RedirectResponse(
                        url="/people/self/tax-info?error=Invalid+gender+value",
                        status_code=303,
                    )
            else:
                gender = None
            if (person.gender or None) != gender:
                proposed_changes["gender"] = gender.value if gender else None

            for field in [
                "address_line1",
                "address_line2",
                "city",
                "region",
                "postal_code",
                "country_code",
            ]:
                new_val = _normalize(payload.get(field))
                if field == "country_code" and new_val:
                    new_val = new_val.upper()
                    if len(new_val) != 2:
                        return RedirectResponse(
                            url="/people/self/tax-info?error=Country+code+must+be+2+letters",
                            status_code=303,
                        )
                current_val = getattr(person, field)
                if new_val != (current_val or None):
                    proposed_changes[field] = new_val

        # Employee contact fields
        for field in [
            "personal_email",
            "personal_phone",
            "emergency_contact_name",
            "emergency_contact_phone",
        ]:
            new_val = _normalize(payload.get(field))
            current_val = getattr(employee, field)
            if new_val != (current_val or None):
                proposed_changes[field] = new_val

        # Bank fields
        for field in [
            "bank_name",
            "bank_account_number",
            "bank_account_name",
            "bank_branch_code",
        ]:
            new_val = _normalize(payload.get(field))
            current_val = getattr(employee, field)
            if new_val != (current_val or None):
                proposed_changes[field] = new_val

        # Tax/pension fields
        for field in ["tin", "tax_state", "rsa_pin", "pfa_code", "nhf_number"]:
            new_val = _normalize(payload.get(field))
            current_val = getattr(tax_profile, field) if tax_profile else None
            if new_val != (current_val or None):
                proposed_changes[field] = new_val

        if not proposed_changes:
            return RedirectResponse(
                url="/people/self/tax-info?error=No+changes+detected",
                status_code=303,
            )

        info_change_service.submit_change_request(
            organization_id=org_id,
            employee_id=employee_id,
            proposed_changes=proposed_changes,
        )
        db.commit()
        return RedirectResponse(
            url="/people/self/tax-info?success=Change+request+submitted",
            status_code=303,
        )

    @staticmethod
    def _build_extended_query(
        base_path: str,
        *,
        success: str | None = None,
        error: str | None = None,
        edit_id: UUID | None = None,
    ) -> str:
        params: dict[str, str] = {}
        if success:
            params["success"] = success
        if error:
            params["error"] = error
        if edit_id:
            params["edit_id"] = str(edit_id)
        query = urlencode(params)
        return f"{base_path}?{query}" if query else base_path

    @staticmethod
    def _normalize_text(value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text

    def _upload_pending_evidence(
        self,
        *,
        org_id: UUID,
        employee_id: UUID,
        upload,
    ) -> PendingEvidence | None:
        if upload is None or not getattr(upload, "filename", None):
            return None
        file_name = str(upload.filename or "").strip()
        if not file_name:
            return None
        file_bytes = upload.file.read()
        if not file_bytes:
            raise EmployeeExtendedDataError("The selected file is empty")
        upload_service = get_employee_document_upload()
        try:
            result = upload_service.save(
                file_data=file_bytes,
                content_type=upload.content_type or "application/octet-stream",
                subdirs=(str(org_id), str(employee_id)),
                original_filename=file_name,
            )
        except FileUploadError as exc:
            raise EmployeeExtendedDataError(str(exc)) from exc
        return PendingEvidence(
            path=result.relative_path,
            file_name=file_name,
            file_size=result.file_size,
            mime_type=upload.content_type,
            checksum=result.checksum,
        )

    @staticmethod
    def _extended_record_snapshot(record: object, section: str) -> dict[str, Any]:
        if section == "qualifications" and isinstance(record, EmployeeQualification):
            return {
                "qualification_type": record.qualification_type.value,
                "qualification_name": record.qualification_name,
                "field_of_study": record.field_of_study,
                "institution_name": record.institution_name,
                "institution_location": record.institution_location,
                "start_date": record.start_date.isoformat()
                if record.start_date
                else None,
                "end_date": record.end_date.isoformat() if record.end_date else None,
                "is_ongoing": bool(record.is_ongoing),
                "grade": record.grade,
                "score": str(record.score) if record.score is not None else None,
                "max_score": str(record.max_score)
                if record.max_score is not None
                else None,
                "notes": record.notes,
                "document_id": str(record.document_id) if record.document_id else None,
            }
        if section == "certifications" and isinstance(record, EmployeeCertification):
            return {
                "certification_name": record.certification_name,
                "issuing_authority": record.issuing_authority,
                "issue_date": record.issue_date.isoformat()
                if record.issue_date
                else None,
                "expiry_date": record.expiry_date.isoformat()
                if record.expiry_date
                else None,
                "does_not_expire": bool(record.does_not_expire),
                "credential_id": record.credential_id,
                "credential_url": record.credential_url,
                "notes": record.notes,
                "document_id": str(record.document_id) if record.document_id else None,
            }
        if section == "skills" and isinstance(record, EmployeeSkill):
            return {
                "skill_id": str(record.skill_id),
                "proficiency_level": record.proficiency_level,
                "years_experience": str(record.years_experience)
                if record.years_experience is not None
                else None,
                "last_used_date": record.last_used_date.isoformat()
                if record.last_used_date
                else None,
                "is_primary": bool(record.is_primary),
                "notes": record.notes,
            }
        if section == "dependents" and isinstance(record, EmployeeDependent):
            return {
                "full_name": record.full_name,
                "relationship": record.relation_type.value,
                "date_of_birth": record.date_of_birth.isoformat()
                if record.date_of_birth
                else None,
                "gender": record.gender.value if record.gender else None,
                "phone": record.phone,
                "email": record.email,
                "address": record.address,
                "is_emergency_contact": bool(record.is_emergency_contact),
                "emergency_contact_priority": record.emergency_contact_priority,
                "is_beneficiary": bool(record.is_beneficiary),
                "beneficiary_percentage": str(record.beneficiary_percentage)
                if record.beneficiary_percentage is not None
                else None,
                "notes": record.notes,
            }
        raise EmployeeExtendedDataError("Unsupported record snapshot")

    def _extended_section_config(self, section: str) -> tuple[str, str, InfoChangeType]:
        mapping = {
            "qualifications": (
                "Qualifications",
                "self-qualifications",
                InfoChangeType.QUALIFICATION,
            ),
            "certifications": (
                "Certifications",
                "self-certifications",
                InfoChangeType.CERTIFICATION,
            ),
            "skills": ("Skills", "self-skills", InfoChangeType.SKILL),
            "dependents": (
                "Dependants",
                "self-dependents",
                InfoChangeType.DEPENDENT,
            ),
        }
        if section not in mapping:
            raise EmployeeExtendedDataError("Unsupported self-service section")
        return mapping[section]

    @staticmethod
    def _default_section_row(section: str) -> dict[str, Any]:
        defaults: dict[str, dict[str, Any]] = {
            "qualifications": {
                "qualification_type": "",
                "qualification_name": "",
                "field_of_study": "",
                "institution_name": "",
                "institution_location": "",
                "start_date": "",
                "end_date": "",
                "is_ongoing": False,
                "grade": "",
                "score": "",
                "max_score": "",
                "notes": "",
                "_errors": {},
            },
            "certifications": {
                "certification_name": "",
                "issuing_authority": "",
                "issue_date": "",
                "expiry_date": "",
                "does_not_expire": False,
                "credential_id": "",
                "credential_url": "",
                "notes": "",
                "_errors": {},
            },
            "skills": {
                "skill_id": "",
                "proficiency_level": "",
                "years_experience": "",
                "last_used_date": "",
                "is_primary": False,
                "notes": "",
                "_errors": {},
            },
            "dependents": {
                "full_name": "",
                "relationship": "",
                "date_of_birth": "",
                "gender": "",
                "phone": "",
                "email": "",
                "address": "",
                "is_emergency_contact": False,
                "emergency_contact_priority": "",
                "is_beneficiary": False,
                "beneficiary_percentage": "",
                "notes": "",
                "_errors": {},
            },
        }
        return defaults[section].copy()

    @staticmethod
    def _default_document_row() -> dict[str, Any]:
        return {
            "document_type": "",
            "document_name": "",
            "description": "",
            "issue_date": "",
            "expiry_date": "",
            "_errors": {},
        }

    @staticmethod
    def _assign_row_error(row: dict[str, Any], message: str) -> None:
        lowered = message.lower()
        matched_fields: list[str] = []
        for field_name in row:
            if field_name.startswith("_"):
                continue
            if field_name in lowered or field_name.replace("_", " ") in lowered:
                matched_fields.append(field_name)
        if not matched_fields:
            row.setdefault("_errors", {})["row"] = message
            return
        for field_name in matched_fields:
            row.setdefault("_errors", {})[field_name] = message

    @staticmethod
    def _renderable_form_rows(
        rows: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Project collected rows into JSON-safe form state for the templates.

        Collected rows carry the raw ``UploadFile`` under ``_upload`` so the
        submit path can read it. The templates hand rows to ``| tojson``, which
        cannot serialise an ``UploadFile``. This is the single point where rows
        become template context, so the substitution happens here: the file
        object is replaced by its name under ``_upload_filename``.

        The name is kept because a browser never repopulates a file input — on a
        validation failure the user has to pick the file again, and the template
        can only say which one if the name survives.
        """
        if rows is None:
            return None
        renderable: list[dict[str, Any]] = []
        for row in rows:
            projected = {
                key: value
                for key, value in row.items()
                if key not in TRANSPORT_ROW_KEYS
            }
            projected["_upload_filename"] = (
                getattr(row.get("_upload"), "filename", None) or ""
            )
            renderable.append(projected)
        return renderable

    def extended_profile_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        section: str,
        success: str | None = None,
        error: str | None = None,
        edit_id: UUID | None = None,
        form_data: dict[str, Any] | None = None,
        form_rows: list[dict[str, Any]] | None = None,
    ) -> HTMLResponse:
        title, active_module, change_type = self._extended_section_config(section)
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    title,
                    active_module,
                    detail=exc.detail,
                )
            raise

        extended_service = EmployeeExtendedSelfServiceService(db, org_id)
        employee = extended_service.get_employee_for_person(person_id)
        profile = extended_service.list_profile(employee_id)
        info_change_service = InfoChangeService(db)
        pending_request = None
        pending_requests = info_change_service.get_pending_requests(
            org_id,
            employee_id=employee_id,
            change_type=change_type,
            limit=50,
        )
        if pending_requests:
            pending_request = pending_requests[0]
        create_pending_requests = [
            item
            for item in pending_requests
            if item.operation == InfoChangeOperation.CREATE
        ]
        recent_requests = info_change_service.get_employee_requests(
            org_id,
            employee_id,
            include_resolved=True,
            limit=10,
        )

        edit_record = None
        if edit_id:
            getter_map = {
                "qualifications": extended_service.get_owned_qualification,
                "certifications": extended_service.get_owned_certification,
                "skills": extended_service.get_owned_employee_skill,
                "dependents": extended_service.get_owned_dependent,
            }
            try:
                edit_record = getter_map[section](employee_id, edit_id)
            except EmployeeExtendedDataError:
                error = "Record not found"

        context = base_context(request, auth, title, active_module, db=db)
        context.update(
            {
                "employee": employee,
                "records": profile[section],
                "skill_catalog": profile["skill_catalog"],
                "pending_request": pending_request,
                "recent_requests": recent_requests,
                "success": success,
                "error": error,
                "section": section,
                "section_title": title,
                "section_change_type": change_type,
                "qualification_types": list(QualificationType),
                "relationship_types": list(RelationshipType),
                "edit_record": edit_record,
                "edit_id": edit_id,
                "form_data": form_data or {},
                "form_rows": self._renderable_form_rows(form_rows)
                or [self._default_section_row(section)],
                "pending_requests": pending_requests,
                "create_pending_requests": create_pending_requests,
                "max_batch_items": InfoChangeService.MAX_BATCH_ITEMS,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request,
            f"people/self/{section}.html",
            context,
        )

    @staticmethod
    def _employee_document_type_options() -> list[DocumentType]:
        return sorted(
            list(InfoChangeService.SELF_SERVICE_DOCUMENT_TYPES),
            key=lambda item: item.value,
        )

    def documents_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        success: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        self._require_permission(auth, "selfservice:documents:read")
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Documents",
                    "self-documents",
                    detail=exc.detail,
                )
            raise

        extended_service = EmployeeExtendedSelfServiceService(db, org_id)
        employee = extended_service.get_employee_for_person(person_id)
        document_service = EmployeeDocumentService(db, org_id)
        approved_documents = document_service.list_documents(employee_id)
        all_requests = InfoChangeService(db).get_employee_requests(
            org_id,
            employee_id,
            include_resolved=True,
            limit=50,
        )
        document_requests = [
            item for item in all_requests if item.change_type == InfoChangeType.DOCUMENT
        ]
        pending_requests = [
            item
            for item in document_requests
            if item.status == InfoChangeStatus.PENDING
        ]
        recent_history = [
            item
            for item in document_requests
            if item.status in {InfoChangeStatus.REJECTED, InfoChangeStatus.EXPIRED}
        ][:10]

        context = base_context(request, auth, "My Documents", "self-documents", db=db)
        context.update(
            {
                "employee": employee,
                "approved_documents": approved_documents,
                "pending_requests": pending_requests,
                "recent_history": recent_history,
                "success": success,
                "error": error,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/documents.html", context
        )

    def document_upload_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        error: str | None = None,
        form_data: dict[str, Any] | None = None,
        form_rows: list[dict[str, Any]] | None = None,
    ) -> HTMLResponse:
        self._require_permission(auth, "selfservice:documents:upload")
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Upload Document",
                    "self-documents",
                    detail=exc.detail,
                )
            raise

        employee = EmployeeExtendedSelfServiceService(
            db, org_id
        ).get_employee_for_person(person_id)
        context = base_context(
            request, auth, "Upload Document", "self-documents", db=db
        )
        context.update(
            {
                "employee": employee,
                "document_types": self._employee_document_type_options(),
                "form_data": form_data or {},
                "form_rows": self._renderable_form_rows(form_rows)
                or [self._default_document_row()],
                "error": error,
                "max_batch_items": InfoChangeService.MAX_BATCH_ITEMS,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request,
            "people/self/document_form.html",
            context,
        )

    def submit_document_upload_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        payload: dict[str, Any],
        upload,
    ) -> RedirectResponse | HTMLResponse:
        self._require_permission(auth, "selfservice:documents:upload")
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        pending_evidence = None
        upload_service = get_employee_document_upload()
        info_change_service = InfoChangeService(db)
        try:
            pending_evidence = self._upload_pending_evidence(
                org_id=org_id,
                employee_id=employee_id,
                upload=upload,
            )
            if pending_evidence is None:
                raise EmployeeExtendedDataError("Select a file to upload")
            normalized = info_change_service._validate_document_payload(payload)
            info_change_service.submit_document_change_request(
                organization_id=org_id,
                employee_id=employee_id,
                proposed_changes=normalized,
                pending_evidence=pending_evidence,
            )
            db.commit()
            return RedirectResponse(
                url="/people/self/documents?success=Document+submitted+for+HR+approval",
                status_code=303,
            )
        except (EmployeeExtendedDataError, ValueError) as exc:
            self._rollback_and_reprime(db, org_id)
            if pending_evidence:
                try:
                    upload_service.delete(pending_evidence.path)
                except Exception:
                    logger.exception("Failed to clean up pending self-service document")
            return self.document_upload_form_response(
                request,
                auth,
                db,
                error=str(exc),
                form_data=payload,
            )

    def submit_document_upload_batch_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        rows: list[dict[str, Any]],
    ) -> RedirectResponse | HTMLResponse:
        self._require_permission(auth, "selfservice:documents:upload")
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        info_change_service = InfoChangeService(db)
        normalized_rows: list[DocumentBatchItemInput] = []
        row_errors = False
        upload_service = get_employee_document_upload()
        uploaded: list[PendingEvidence] = []
        try:
            for row in rows:
                row.setdefault("_errors", {})
                try:
                    normalized = info_change_service._validate_document_payload(row)
                except ValueError as exc:
                    self._assign_row_error(row, str(exc))
                    row_errors = True
                    continue
                upload = row.get("_upload")
                try:
                    pending = self._upload_pending_evidence(
                        org_id=org_id,
                        employee_id=employee_id,
                        upload=upload,
                    )
                except EmployeeExtendedDataError as exc:
                    row["_errors"]["file"] = str(exc)
                    row_errors = True
                    continue
                if pending is None:
                    row["_errors"]["file"] = "Select a file to upload"
                    row_errors = True
                    continue
                uploaded.append(pending)
                normalized_rows.append(
                    DocumentBatchItemInput(
                        proposed_changes=normalized,
                        pending_evidence=pending,
                    )
                )
            if row_errors:
                for item in uploaded:
                    upload_service.delete(item.path)
                return self.document_upload_form_response(
                    request,
                    auth,
                    db,
                    error="Correct the highlighted rows and try again",
                    form_rows=rows,
                )
            info_change_service.submit_document_change_batch(
                organization_id=org_id,
                employee_id=employee_id,
                items=normalized_rows,
            )
            db.commit()
            return RedirectResponse(
                url="/people/self/documents?success=Documents+submitted+for+HR+approval",
                status_code=303,
            )
        except (EmployeeExtendedDataError, ValueError) as exc:
            self._rollback_and_reprime(db, org_id)
            for item in uploaded:
                try:
                    upload_service.delete(item.path)
                except Exception:
                    logger.exception("Failed to clean up pending self-service document")
            return self.document_upload_form_response(
                request,
                auth,
                db,
                error=str(exc),
                form_rows=rows,
            )

    def download_document_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        document_id: UUID,
    ):
        self._require_permission(auth, "selfservice:documents:read")
        org_id = coerce_uuid(auth.organization_id)
        employee_id = self._get_employee_id(db, org_id, coerce_uuid(auth.person_id))
        try:
            resolved = EmployeeDocumentService(
                db, org_id
            ).resolve_owned_document_download(
                employee_id,
                document_id,
            )
        except EmployeeExtendedDataError as exc:
            raise HTTPException(status_code=404, detail="Document not found") from exc
        return self._streaming_download_response(
            resolved.chunks,
            content_type=resolved.content_type,
            content_length=resolved.content_length,
            filename=resolved.filename,
        )

    def submit_extended_profile_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        section: str,
        payload: dict[str, Any],
        upload=None,
        record_id: UUID | None = None,
    ) -> RedirectResponse | HTMLResponse:
        title, _, change_type = self._extended_section_config(section)
        del title
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        info_change_service = InfoChangeService(db)
        extended_service = EmployeeExtendedSelfServiceService(db, org_id)
        operation = (
            InfoChangeOperation.UPDATE
            if record_id is not None
            else InfoChangeOperation.CREATE
        )
        pending_evidence = None
        upload_service = get_employee_document_upload()
        try:
            if section in {"qualifications", "certifications"}:
                pending_evidence = self._upload_pending_evidence(
                    org_id=org_id,
                    employee_id=employee_id,
                    upload=upload,
                )

            validator_map = {
                "qualifications": info_change_service._validate_qualification_payload,
                "certifications": info_change_service._validate_certification_payload,
                "skills": lambda data: info_change_service._validate_skill_payload(
                    org_id,
                    employee_id,
                    data,
                    target_employee_skill_id=record_id,
                ),
                "dependents": info_change_service._validate_dependent_payload,
            }
            validator_map[section](payload)

            previous_values: dict[str, Any] = {}
            if record_id:
                getter_map = {
                    "qualifications": extended_service.get_owned_qualification,
                    "certifications": extended_service.get_owned_certification,
                    "skills": extended_service.get_owned_employee_skill,
                    "dependents": extended_service.get_owned_dependent,
                }
                record = getter_map[section](employee_id, record_id)
                previous_values = self._extended_record_snapshot(record, section)

            info_change_service.submit_extended_change_request(
                organization_id=org_id,
                employee_id=employee_id,
                change_type=change_type,
                operation=operation,
                proposed_changes=payload,
                previous_values=previous_values,
                target_record_id=record_id,
                pending_evidence=pending_evidence,
            )
            db.commit()
            return RedirectResponse(
                url=self._build_extended_query(
                    f"/people/self/{section}",
                    success="Change request submitted for HR approval",
                ),
                status_code=303,
            )
        except (EmployeeExtendedDataError, ValueError) as exc:
            self._rollback_and_reprime(db, org_id)
            if pending_evidence:
                try:
                    upload_service.delete(pending_evidence.path)
                except Exception:
                    logger.exception("Failed to clean up pending self-service evidence")
            return self.extended_profile_response(
                request,
                auth,
                db,
                section=section,
                error=str(exc),
                edit_id=record_id,
                form_data=payload,
            )

    def submit_extended_profile_batch_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        section: str,
        rows: list[dict[str, Any]],
    ) -> RedirectResponse | HTMLResponse:
        _, _, change_type = self._extended_section_config(section)
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        info_change_service = InfoChangeService(db)
        upload_service = get_employee_document_upload()
        prepared: list[ExtendedBatchItemInput] = []
        uploaded: list[PendingEvidence] = []
        row_errors = False
        try:
            for row in rows:
                row.setdefault("_errors", {})
                try:
                    normalized = info_change_service._validate_extended_payload(
                        org_id,
                        employee_id,
                        change_type=change_type,
                        payload=row,
                        target_record_id=None,
                    )
                except ValueError as exc:
                    self._assign_row_error(row, str(exc))
                    row_errors = True
                    continue
                pending_evidence = None
                if section in {"qualifications", "certifications"}:
                    try:
                        pending_evidence = self._upload_pending_evidence(
                            org_id=org_id,
                            employee_id=employee_id,
                            upload=row.get("_upload"),
                        )
                    except EmployeeExtendedDataError as exc:
                        row["_errors"]["supporting_file"] = str(exc)
                        row_errors = True
                        continue
                    if pending_evidence:
                        uploaded.append(pending_evidence)
                prepared.append(
                    ExtendedBatchItemInput(
                        proposed_changes=normalized,
                        previous_values={},
                        operation=InfoChangeOperation.CREATE,
                        pending_evidence=pending_evidence,
                    )
                )
            if row_errors:
                for item in uploaded:
                    upload_service.delete(item.path)
                return self.extended_profile_response(
                    request,
                    auth,
                    db,
                    section=section,
                    error="Correct the highlighted rows and try again",
                    form_rows=rows,
                )
            info_change_service.submit_extended_change_batch(
                organization_id=org_id,
                employee_id=employee_id,
                change_type=change_type,
                items=prepared,
            )
            db.commit()
            return RedirectResponse(
                url=self._build_extended_query(
                    f"/people/self/{section}",
                    success="Changes submitted for HR approval",
                ),
                status_code=303,
            )
        except (EmployeeExtendedDataError, ValueError) as exc:
            self._rollback_and_reprime(db, org_id)
            for item in uploaded:
                try:
                    upload_service.delete(item.path)
                except Exception:
                    logger.exception("Failed to clean up pending self-service evidence")
            return self.extended_profile_response(
                request,
                auth,
                db,
                section=section,
                error=str(exc),
                form_rows=rows,
            )

    def download_pending_info_change_evidence_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        request_id: UUID,
        require_owner_only: bool = False,
    ):
        org_id = coerce_uuid(auth.organization_id)
        employee_id = None
        if require_owner_only:
            employee_id = self._get_employee_id(db, org_id, coerce_uuid(auth.person_id))
        try:
            chunks, content_type, content_length, filename = InfoChangeService(
                db
            ).resolve_pending_evidence_download(
                org_id,
                request_id,
                employee_id=employee_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Evidence not found") from exc
        return self._streaming_download_response(
            chunks,
            content_type=content_type,
            content_length=content_length,
            filename=filename,
        )

    @staticmethod
    def _get_tasks_for_dropdown(
        db: Session, org_id: UUID, project_id: str | None = None
    ) -> list[dict]:
        """Get tasks for expense linking."""
        try:
            from app.models.pm.task import Task, TaskStatus

            stmt = select(Task).where(
                Task.organization_id == org_id,
                Task.status != TaskStatus.CANCELLED,
            )
            if project_id:
                stmt = stmt.where(Task.project_id == coerce_uuid(project_id))
            tasks = db.execute(stmt.order_by(Task.task_code)).scalars().all()

            return [
                {
                    "task_id": str(t.task_id),
                    "task_code": t.task_code,
                    "task_name": t.task_name,
                    "project_id": str(t.project_id),
                }
                for t in tasks
            ]
        except Exception:
            return []

    def tickets_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        page: int = 1,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Tickets",
                    "self-tickets",
                    detail=exc.detail,
                )
            raise

        from app.models.support.ticket import Ticket, TicketPriority
        from app.services.support.category import category_service

        per_page = 20
        filters = [
            Ticket.organization_id == org_id,
            or_(
                Ticket.raised_by_id == employee_id,
                Ticket.assigned_to_id == employee_id,
            ),
        ]
        total = db.scalar(select(func.count()).select_from(Ticket).where(*filters)) or 0
        tickets = list(
            db.execute(
                select(Ticket)
                .where(*filters)
                .options(
                    joinedload(Ticket.raised_by),
                    joinedload(Ticket.assigned_to),
                )
                .order_by(Ticket.opening_date.desc(), Ticket.created_at.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            .scalars()
            .unique()
            .all()
        )
        total_pages = (total + per_page - 1) // per_page
        categories = category_service.list_categories(db, org_id)

        context = base_context(request, auth, "My Tickets", "self-tickets", db=db)
        context.update(
            {
                "tickets": tickets,
                "categories": [
                    {
                        "value": str(category.category_id),
                        "label": category.category_name,
                    }
                    for category in categories
                ],
                "priorities": [priority.value for priority in TicketPriority],
                "page": page,
                "total": total,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/tickets.html", context)

    def ticket_create_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        subject: str,
        description: str | None = None,
        priority: str = "MEDIUM",
        category_id: str | None = None,
    ) -> RedirectResponse:
        """Create a support ticket from self-service and return to My Tickets."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        user_id = coerce_uuid(auth.user_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return RedirectResponse(
                    url=f"/people/self/tickets?error={quote(str(exc.detail))}",
                    status_code=303,
                )
            raise

        if not subject.strip():
            return RedirectResponse(
                url="/people/self/tickets?error=Subject+is+required",
                status_code=303,
            )

        from app.services.support.ticket import ticket_service

        try:
            ticket_service.create_ticket(
                db,
                org_id,
                user_id,
                subject=subject.strip(),
                description=description.strip() if description else None,
                priority=priority,
                raised_by_id=employee_id,
                category_id=coerce_uuid(category_id) if category_id else None,
            )
            db.commit()
            return RedirectResponse(
                url="/people/self/tickets?saved=1",
                status_code=303,
            )
        except Exception as exc:
            db.rollback()
            logger.exception("Failed to create self-service ticket")
            message = (
                getattr(exc, "detail", None)
                if isinstance(exc, HTTPException)
                else "Unable to create ticket"
            )
            return RedirectResponse(
                url=f"/people/self/tickets?error={quote(str(message))}",
                status_code=303,
            )

    def tasks_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        page: int = 1,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Tasks",
                    "self-tasks",
                    detail=exc.detail,
                )
            raise

        from app.services.pm.task_service import TaskService

        per_page = 20
        svc = TaskService(db, org_id)
        result = svc.list_tasks(
            assigned_to_id=employee_id,
            params=PaginationParams(offset=(page - 1) * per_page, limit=per_page),
        )
        total = result.total
        total_pages = (total + per_page - 1) // per_page

        context = base_context(request, auth, "My Tasks", "self-tasks", db=db)
        context.update(
            {
                "tasks": result.items,
                "page": page,
                "total": total,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/tasks.html", context)

    def attendance_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        month: str | None = None,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Attendance",
                    "self-attendance",
                    detail=exc.detail,
                )
            raise

        svc = AttendanceService(db)
        today = svc.get_org_today(org_id)
        today_record = svc.get_attendance_by_date(org_id, employee_id, today)
        org_tzinfo = svc.get_org_tzinfo(org_id)

        def _format_time(value: datetime | None) -> str:
            if not value:
                return "-"
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(org_tzinfo).strftime("%H:%M")

        month_start, month_end, selected_month, previous_month, next_month = (
            self._resolve_month_range(month, today)
        )
        summary = svc.get_employee_monthly_summary(
            org_id, employee_id, month_start.year, month_start.month
        )

        month_records = list(
            db.scalars(
                select(Attendance)
                .where(
                    Attendance.organization_id == org_id,
                    Attendance.employee_id == employee_id,
                    Attendance.attendance_date >= month_start,
                    Attendance.attendance_date <= month_end,
                )
                .order_by(Attendance.attendance_date)
            )
        )
        holiday_map = self._get_holiday_map(db, org_id, month_start, month_end)
        calendar_weeks, calendar_counts = self._build_attendance_calendar(
            month_start=month_start,
            month_end=month_end,
            today=today,
            holiday_map=holiday_map,
            record_by_date={record.attendance_date: record for record in month_records},
            org_tzinfo=org_tzinfo,
        )

        recent = svc.list_attendance(
            org_id,
            employee_id=employee_id,
            from_date=month_start,
            to_date=month_end,
            pagination=PaginationParams(offset=0, limit=10),
        )

        context = base_context(request, auth, "My Attendance", "self-attendance", db=db)
        context.update(
            {
                "today_record": today_record,
                "today_check_in_display": _format_time(
                    today_record.check_in if today_record else None
                ),
                "today_check_out_display": _format_time(
                    today_record.check_out if today_record else None
                ),
                "summary": summary,
                "calendar_weeks": calendar_weeks,
                "calendar_counts": calendar_counts,
                "selected_month": selected_month,
                "selected_month_label": month_start.strftime("%B %Y"),
                "previous_month": previous_month,
                "next_month": next_month,
                "recent_records": [
                    {
                        "attendance_date": rec.attendance_date,
                        "status": rec.status,
                        "check_in_display": _format_time(rec.check_in),
                        "check_out_display": _format_time(rec.check_out),
                        "working_hours": rec.working_hours,
                    }
                    for rec in recent.items
                ],
                "month": selected_month,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/attendance.html", context
        )

    def check_in_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        notes: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        try:
            AttendanceService(db).check_in(
                org_id,
                employee_id,
                check_in_time=None,
                notes=notes,
                latitude=latitude,
                longitude=longitude,
            )
        except ValidationError as exc:
            return RedirectResponse(
                url=f"/people/self/attendance?{urlencode({'error': exc.message})}",
                status_code=303,
            )
        except AttendanceServiceError as exc:
            return RedirectResponse(
                url=f"/people/self/attendance?{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        db.commit()
        return RedirectResponse(url="/people/self/attendance", status_code=302)

    def check_out_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        notes: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        try:
            AttendanceService(db).check_out(
                org_id,
                employee_id,
                check_out_time=None,
                notes=notes,
                latitude=latitude,
                longitude=longitude,
            )
        except ValidationError as exc:
            return RedirectResponse(
                url=f"/people/self/attendance?{urlencode({'error': exc.message})}",
                status_code=303,
            )
        except AttendanceServiceError as exc:
            return RedirectResponse(
                url=f"/people/self/attendance?{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        db.commit()
        return RedirectResponse(url="/people/self/attendance", status_code=302)

    def scheduling_schedules_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        year_month: str | None = None,
    ) -> HTMLResponse:
        """Self-service monthly schedule view."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Schedule",
                    "self-attendance",
                    detail=exc.detail,
                )
            raise

        resolved_month = year_month or date.today().strftime("%Y-%m")
        svc = SchedulingService(db)
        schedules = svc.list_schedules(
            org_id=org_id,
            employee_id=employee_id,
            schedule_month=resolved_month,
            pagination=PaginationParams(offset=0, limit=200),
        )

        context = base_context(request, auth, "My Schedule", "self-attendance", db=db)
        context.update(
            {
                "year_month": resolved_month,
                "schedules": schedules.items,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/scheduling_schedules.html", context
        )

    def scheduling_swaps_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        year_month: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """Self-service swap requests page."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Shift Swaps",
                    "self-attendance",
                    detail=exc.detail,
                )
            raise

        resolved_month = year_month or date.today().strftime("%Y-%m")
        pager = PaginationParams.from_page(page, per_page=20)
        swap_svc = SwapService(db)
        my_requests = swap_svc.get_my_requests(
            org_id=org_id,
            employee_id=employee_id,
            pagination=pager,
        )
        pending_acceptance = swap_svc.get_pending_acceptance(
            org_id=org_id,
            employee_id=employee_id,
            pagination=PaginationParams(offset=0, limit=50),
        )

        my_schedules = list(
            db.scalars(
                select(ShiftSchedule).where(
                    ShiftSchedule.organization_id == org_id,
                    ShiftSchedule.employee_id == employee_id,
                    ShiftSchedule.schedule_month == resolved_month,
                    ShiftSchedule.status == ScheduleStatus.PUBLISHED,
                )
            ).all()
        )
        my_schedule_ids = {s.shift_schedule_id for s in my_schedules}

        coworker_schedules = list(
            db.scalars(
                select(ShiftSchedule)
                .where(
                    ShiftSchedule.organization_id == org_id,
                    ShiftSchedule.schedule_month == resolved_month,
                    ShiftSchedule.status == ScheduleStatus.PUBLISHED,
                    ShiftSchedule.employee_id != employee_id,
                )
                .order_by(ShiftSchedule.shift_date, ShiftSchedule.employee_id)
                .limit(400)
            ).all()
        )
        employee_ids = {s.employee_id for s in coworker_schedules}
        employees = list(
            db.scalars(
                select(Employee).where(Employee.employee_id.in_(employee_ids))
            ).all()
        )
        employee_map = {
            emp.employee_id: (
                emp.full_name or emp.employee_code or str(emp.employee_id)
            )
            for emp in employees
        }

        target_options = [
            {
                "id": str(s.shift_schedule_id),
                "label": f"{employee_map.get(s.employee_id, str(s.employee_id))} - {s.shift_date.isoformat()}",
            }
            for s in coworker_schedules
        ]

        context = base_context(
            request, auth, "My Shift Swaps", "self-attendance", db=db
        )
        context.update(
            {
                "year_month": resolved_month,
                "my_requests": my_requests.items,
                "pending_acceptance": pending_acceptance.items,
                "my_schedule_options": my_schedules,
                "target_schedule_options": target_options,
                "my_schedule_ids": {str(sid) for sid in my_schedule_ids},
                "swap_statuses": [s.value for s in SwapRequestStatus],
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/scheduling_swaps.html", context
        )

    def scheduling_create_swap_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        form: dict,
    ) -> RedirectResponse:
        """Create swap request from self-service page."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        year_month = str(form.get("year_month") or date.today().strftime("%Y-%m"))
        try:
            requester_schedule_id = coerce_uuid(
                str(form.get("requester_schedule_id", ""))
            )
            target_schedule_id = coerce_uuid(str(form.get("target_schedule_id", "")))
        except Exception:
            return RedirectResponse(
                f"/people/self/scheduling/swaps?year_month={year_month}&error={quote('Invalid schedule selection')}",
                status_code=303,
            )
        reason_raw = form.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else None
        if not requester_schedule_id or not target_schedule_id:
            return RedirectResponse(
                f"/people/self/scheduling/swaps?year_month={year_month}&error={quote('Both schedules are required')}",
                status_code=303,
            )
        try:
            SwapService(db).create_swap_request(
                org_id=org_id,
                requester_id=employee_id,
                requester_schedule_id=requester_schedule_id,
                target_schedule_id=target_schedule_id,
                reason=reason,
            )
            db.commit()
            return RedirectResponse(
                f"/people/self/scheduling/swaps?year_month={year_month}&success={quote('Swap request submitted')}",
                status_code=303,
            )
        except Exception as exc:
            db.rollback()
            return RedirectResponse(
                f"/people/self/scheduling/swaps?year_month={year_month}&error={quote(str(exc))}",
                status_code=303,
            )

    def scheduling_accept_swap_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        request_id: UUID,
    ) -> RedirectResponse:
        """Accept a pending swap request as target employee."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        try:
            SwapService(db).accept_swap_request(
                org_id=org_id,
                request_id=request_id,
                accepting_employee_id=employee_id,
            )
            db.commit()
            return RedirectResponse(
                "/people/self/scheduling/swaps?success=accepted",
                status_code=303,
            )
        except Exception as exc:
            db.rollback()
            return RedirectResponse(
                f"/people/self/scheduling/swaps?error={quote(str(exc))}",
                status_code=303,
            )

    def scheduling_decline_swap_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        request_id: UUID,
        form: dict,
    ) -> RedirectResponse:
        """Decline a pending swap request as target employee."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        reason_raw = form.get("reason")
        reason = str(reason_raw).strip() if isinstance(reason_raw, str) else None
        try:
            SwapService(db).decline_swap_request(
                org_id=org_id,
                request_id=request_id,
                declining_employee_id=employee_id,
                reason=reason,
            )
            db.commit()
            return RedirectResponse(
                "/people/self/scheduling/swaps?success=declined",
                status_code=303,
            )
        except Exception as exc:
            db.rollback()
            return RedirectResponse(
                f"/people/self/scheduling/swaps?error={quote(str(exc))}",
                status_code=303,
            )

    def scheduling_cancel_swap_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        request_id: UUID,
    ) -> RedirectResponse:
        """Cancel own swap request."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)
        try:
            SwapService(db).cancel_swap_request(
                org_id=org_id,
                request_id=request_id,
                requester_id=employee_id,
            )
            db.commit()
            return RedirectResponse(
                "/people/self/scheduling/swaps?success=cancelled",
                status_code=303,
            )
        except Exception as exc:
            db.rollback()
            return RedirectResponse(
                f"/people/self/scheduling/swaps?error={quote(str(exc))}",
                status_code=303,
            )

    def leave_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Leave",
                    "self-leave",
                    detail=exc.detail,
                )
            raise

        svc = LeaveService(db, auth)
        balances = svc.get_employee_balances(org_id, employee_id)
        applications = svc.list_applications(
            org_id,
            employee_id=employee_id,
            pagination=PaginationParams(offset=0, limit=15),
        )
        leave_types = svc.list_leave_types(
            org_id, is_active=True, pagination=None
        ).items

        context = base_context(request, auth, "My Leave", "self-leave", db=db)
        context.update(
            {
                "balances": balances,
                "applications": applications.items,
                "leave_types": leave_types,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/leave.html", context)

    def leave_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        application_id: UUID,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Leave Application",
                    "self-leave",
                    detail=exc.detail,
                )
            raise

        svc = LeaveService(db, auth)
        application = svc.get_application(org_id, application_id)
        if application.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        context = base_context(request, auth, "Leave Application", "self-leave", db=db)
        context["application"] = application
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/leave_detail.html", context
        )

    def leave_cancel_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        application_id: UUID,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        svc = LeaveService(db, auth)
        application = svc.get_application(org_id, application_id)
        if application.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        svc.cancel_application(org_id, application_id)
        db.commit()
        return RedirectResponse(url="/people/self/leave", status_code=302)

    def leave_apply_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        leave_type_id: str,
        from_date: date,
        to_date: date,
        half_day: str | None = None,
        reason: str | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        try:
            LeaveService(db, auth).create_application(
                org_id,
                employee_id=employee_id,
                leave_type_id=coerce_uuid(leave_type_id),
                from_date=from_date,
                to_date=to_date,
                half_day=half_day is not None,
                half_day_date=from_date if half_day else None,
                reason=reason,
            )
            db.commit()
        except LeaveServiceError as exc:
            db.rollback()
            return RedirectResponse(
                f"/people/self/leave?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(url="/people/self/leave", status_code=302)

    def expenses_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Expenses",
                    "self-expenses",
                    detail=exc.detail,
                )
            raise

        employee = db.get(Employee, employee_id)
        svc = ExpenseService(db, auth)
        claims = svc.list_claims(
            org_id,
            employee_id=employee_id,
            pagination=PaginationParams(offset=0, limit=20),
        )
        categories = svc.list_categories(org_id, is_active=True, pagination=None).items

        # Get open/active tickets for dropdown
        tickets = self._get_tickets_for_dropdown(db, org_id)

        # Get projects for dropdown
        projects = self._get_projects_for_dropdown(db, org_id)

        # Get cost centers for dropdown
        from app.models.finance.core_org.cost_center import CostCenter

        cost_centers_stmt = (
            select(CostCenter)
            .where(
                CostCenter.organization_id == org_id,
                CostCenter.is_active.is_(True),
            )
            .order_by(CostCenter.cost_center_code)
        )
        cost_centers = list(db.scalars(cost_centers_stmt).all())

        allowed_banks = OrgBankDirectoryService(db).list_active_banks(org_id)
        selected_employee_bank = self._match_org_bank(
            allowed_banks,
            bank_name=employee.bank_name if employee else None,
            bank_code=employee.bank_branch_code if employee else None,
        )

        selected_ticket_id = request.query_params.get("ticket_id")
        selected_project_id = request.query_params.get("project_id")
        selected_task_id = request.query_params.get("task_id")
        tasks = self._get_tasks_for_dropdown(db, org_id, selected_project_id)
        from app.services.fleet.vehicle_service import VehicleService

        vehicles = (
            VehicleService(db, org_id)
            .list_vehicles(params=PaginationParams(offset=0, limit=500))
            .items
        )
        context = base_context(request, auth, "My Expenses", "self-expenses", db=db)
        context.update(
            {
                "claims": claims.items,
                "categories": categories,
                "tickets": tickets,
                "projects": projects,
                "cost_centers": cost_centers,
                "tasks": tasks,
                "vehicles": vehicles,
                "selected_ticket_id": selected_ticket_id,
                "selected_project_id": selected_project_id,
                "selected_task_id": selected_task_id,
                "employee_bank_code": (
                    selected_employee_bank.bank_sort_code
                    if selected_employee_bank
                    else ""
                ),
                "employee_bank_name": (
                    selected_employee_bank.bank_name if selected_employee_bank else ""
                ),
                "employee_bank_account_number": (
                    employee.bank_account_number if employee else ""
                )
                or "",
                "employee_recipient_name": (employee.full_name if employee else "")
                or "",
                "allowed_banks": allowed_banks,
                "expense_approver_options": self._get_expense_approver_options(
                    db, org_id, employee_id=employee_id
                ),
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/expenses.html", context)

    def expense_claim_create_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_date: date,
        purpose: str,
        expense_date: date,
        items: list[dict],
        recipient_bank_code: str | None = None,
        recipient_bank_name: str | None = None,
        recipient_account_number: str | None = None,
        recipient_name: str | None = None,
        requested_approver_id: str | None = None,
        receipt_url: str | None = None,
        receipt_number: str | None = None,
        receipt_files: list[UploadFile] | None = None,
        submit_now: str | None = None,
        project_id: str | None = None,
        ticket_id: str | None = None,
        task_id: str | None = None,
        vehicle_id: str | None = None,
        cost_center_id: str | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        if not items:
            raise HTTPException(
                status_code=400, detail="At least one expense item is required"
            )

        resolved_receipt_urls: list[str] = []
        if receipt_url and str(receipt_url).strip():
            resolved_receipt_urls.append(str(receipt_url).strip())

        upload_files: list[UploadFile] = []
        raw_receipt_files = receipt_files or []
        upload_files.extend(
            f
            for f in raw_receipt_files
            if isinstance(f, UploadFile) and getattr(f, "filename", None)
        )

        if upload_files:
            # File uploads can fail validation (e.g. unsupported MIME type).
            # For self-service web flows, redirect back with a user-visible error
            # toast instead of raising an unhandled exception (500).
            from app.services.file_upload import (
                FileUploadError,
                get_expense_receipt_upload,
            )

            upload_svc = get_expense_receipt_upload()
            uploaded_paths: list[str] = []
            try:
                for upload in upload_files:
                    file_data = upload.file.read()
                    result = upload_svc.save(
                        file_data=file_data,
                        content_type=upload.content_type,
                        subdirs=(str(org_id),),
                        original_filename=upload.filename,
                    )
                    receipt_key = result.s3_key.replace("\\", "/")
                    uploaded_paths.append(receipt_key)
                    resolved_receipt_urls.append(receipt_key)
            except FileUploadError as exc:
                for path in uploaded_paths:
                    try:
                        upload_svc.delete(path)
                    except Exception:
                        logger.exception(
                            "Failed to cleanup orphaned receipt upload",
                            extra={
                                "organization_id": str(org_id),
                                "path": path,
                            },
                        )
                return RedirectResponse(
                    url=f"/people/self/expenses?error={quote(str(exc))}",
                    status_code=303,
                )

        resolved_receipt_url: str | None
        if not resolved_receipt_urls:
            resolved_receipt_url = None
        elif len(resolved_receipt_urls) == 1:
            resolved_receipt_url = resolved_receipt_urls[0]
        else:
            resolved_receipt_url = json.dumps(resolved_receipt_urls)

        resolved_items: list[dict] = []
        for item in items:
            try:
                amount = Decimal(str(item["claimed_amount"]))
            except (InvalidOperation, TypeError, KeyError) as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid claimed amount"
                ) from exc
            resolved_items.append(
                {
                    "expense_date": expense_date,
                    "category_id": coerce_uuid(item["category_id"]),
                    "description": str(item["description"]).strip(),
                    "claimed_amount": amount,
                    "receipt_url": resolved_receipt_url,
                    "receipt_number": receipt_number.strip()
                    if receipt_number
                    else None,
                }
            )

        svc = ExpenseService(db, auth)
        resolved_approver_id = (
            coerce_uuid(requested_approver_id) if requested_approver_id else None
        )
        self._validate_expense_approver_selection(db, org_id, resolved_approver_id)
        claim = svc.create_claim(
            org_id,
            employee_id=employee_id,
            claim_date=claim_date,
            purpose=purpose.strip(),
            project_id=coerce_uuid(project_id) if project_id else None,
            ticket_id=coerce_uuid(ticket_id) if ticket_id else None,
            task_id=coerce_uuid(task_id) if task_id else None,
            vehicle_id=coerce_uuid(vehicle_id) if vehicle_id else None,
            cost_center_id=coerce_uuid(cost_center_id) if cost_center_id else None,
            recipient_bank_code=recipient_bank_code,
            recipient_bank_name=recipient_bank_name,
            recipient_account_number=recipient_account_number,
            recipient_name=recipient_name,
            requested_approver_id=resolved_approver_id,
            items=resolved_items,
            created_by_id=person_id,
        )
        if submit_now:
            svc.submit_claim(
                org_id,
                claim.claim_id,
                skip_receipt_validation=True,
                actor_id=person_id,
            )
        db.commit()
        return RedirectResponse(url="/people/self/expenses", status_code=303)

    def expense_claim_edit_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        svc = ExpenseService(db, auth)
        claim = svc.get_claim(org_id, claim_id)
        if claim.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        can_edit = claim.status == ExpenseClaimStatus.DRAFT
        can_submit = claim.status == ExpenseClaimStatus.DRAFT
        categories = svc.list_categories(org_id, is_active=True, pagination=None).items

        # Get projects, tickets, tasks for dropdowns (same as create form)
        projects = self._get_projects_for_dropdown(db, org_id)
        tickets = self._get_tickets_for_dropdown(db, org_id)
        tasks = self._get_tasks_for_dropdown(
            db, org_id, str(claim.project_id) if claim.project_id else None
        )
        from app.services.fleet.vehicle_service import VehicleService

        vehicles = (
            VehicleService(db, org_id)
            .list_vehicles(params=PaginationParams(offset=0, limit=500))
            .items
        )

        # Get cost centers for dropdown
        from app.models.finance.core_org.cost_center import CostCenter

        cost_centers_stmt = (
            select(CostCenter)
            .where(
                CostCenter.organization_id == org_id,
                CostCenter.is_active.is_(True),
            )
            .order_by(CostCenter.cost_center_code)
        )
        cost_centers = list(db.scalars(cost_centers_stmt).all())

        allowed_banks = OrgBankDirectoryService(db).list_active_banks(org_id)
        selected_claim_bank = self._match_org_bank(
            allowed_banks,
            bank_name=claim.recipient_bank_name,
            bank_code=claim.recipient_bank_code,
        )

        context = base_context(
            request, auth, "Edit Expense Claim", "self-expenses", db=db
        )
        context.update(
            {
                "claim": claim,
                "categories": categories,
                "can_edit": can_edit,
                "can_submit": can_submit,
                "projects": projects,
                "tickets": tickets,
                "tasks": tasks,
                "vehicles": vehicles,
                "cost_centers": cost_centers,
                "allowed_banks": allowed_banks,
                "selected_claim_bank_name": (
                    selected_claim_bank.bank_name if selected_claim_bank else ""
                ),
                "selected_claim_bank_code": (
                    selected_claim_bank.bank_sort_code if selected_claim_bank else ""
                ),
                "expense_approver_options": self._get_expense_approver_options(
                    db,
                    org_id,
                    employee_id=employee_id,
                    selected_approver_id=claim.requested_approver_id,
                ),
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/expense_claim_edit.html", context
        )

    def expense_claim_submit_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        svc = ExpenseService(db, auth)
        claim = svc.get_claim(org_id, claim_id)
        if claim.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if claim.status != ExpenseClaimStatus.DRAFT:
            raise HTTPException(
                status_code=400, detail="Only draft claims can be submitted"
            )

        svc.submit_claim(
            org_id,
            claim_id,
            skip_receipt_validation=True,
            actor_id=person_id,
        )
        db.commit()
        return RedirectResponse(url="/people/self/expenses", status_code=302)

    def expense_claim_delete_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        svc = ExpenseService(db, auth)
        claim = svc.get_claim(org_id, claim_id)
        if claim.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if claim.status != ExpenseClaimStatus.DRAFT:
            raise HTTPException(
                status_code=400, detail="Only draft claims can be deleted"
            )

        svc.delete_claim(org_id, claim_id)
        db.commit()
        return RedirectResponse(url="/people/self/expenses", status_code=302)

    def expense_claim_update_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
        items: list[dict],
        recipient_bank_code: str | None = None,
        recipient_bank_name: str | None = None,
        recipient_account_number: str | None = None,
        recipient_name: str | None = None,
        requested_approver_id: UUID | None = None,
        project_id: UUID | None = None,
        ticket_id: UUID | None = None,
        task_id: UUID | None = None,
        vehicle_id: UUID | None = None,
        cost_center_id: UUID | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        employee_id = self._get_employee_id(db, org_id, person_id)

        svc = ExpenseService(db, auth)
        claim = svc.get_claim(org_id, claim_id)
        if claim.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if claim.status != ExpenseClaimStatus.DRAFT:
            raise HTTPException(
                status_code=400, detail="Only draft claims can be edited"
            )

        if recipient_bank_name or recipient_bank_code:
            recipient_bank_name, recipient_bank_code = (
                self._resolve_expense_bank_selection(
                    db,
                    org_id,
                    bank_name=recipient_bank_name or claim.recipient_bank_name,
                    bank_code=recipient_bank_code or claim.recipient_bank_code,
                    required=True,
                )
            )

        self._validate_expense_approver_selection(db, org_id, requested_approver_id)

        try:
            svc.update_claim(
                org_id,
                claim_id,
                updated_by_id=person_id,
                recipient_bank_code=recipient_bank_code,
                recipient_bank_name=recipient_bank_name,
                recipient_account_number=recipient_account_number,
                recipient_name=recipient_name,
                requested_approver_id=requested_approver_id,
                project_id=project_id,
                ticket_id=ticket_id,
                task_id=task_id,
                vehicle_id=vehicle_id,
                cost_center_id=cost_center_id,
            )
        except ExpenseServiceError as exc:
            from urllib.parse import quote_plus

            return RedirectResponse(
                url=f"/people/self/expenses/claims/{claim_id}/edit?error={quote_plus(str(exc))}",
                status_code=302,
            )

        for item in items:
            if item.get("remove"):
                svc.remove_claim_item(
                    org_id,
                    claim_id=claim_id,
                    item_id=coerce_uuid(item["item_id"]),
                )
                continue

            if not item.get("item_id"):
                svc.add_claim_item(
                    org_id,
                    claim_id=claim_id,
                    expense_date=item["expense_date"],
                    category_id=coerce_uuid(item["category_id"]),
                    description=item["description"],
                    claimed_amount=item["claimed_amount"],
                    receipt_number=item.get("receipt_number"),
                    receipt_url=item.get("receipt_url"),
                )
                continue

            svc.update_claim_item(
                org_id,
                claim_id=claim_id,
                item_id=coerce_uuid(item["item_id"]),
                expense_date=item["expense_date"],
                category_id=coerce_uuid(item["category_id"]),
                description=item["description"],
                claimed_amount=item["claimed_amount"],
                receipt_number=item.get("receipt_number"),
                receipt_url=item.get("receipt_url"),
            )

        db.commit()
        return RedirectResponse(url="/people/self/expenses", status_code=302)

    def team_leave_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        status: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            manager_employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Team Leave",
                    "self-team-leave",
                    detail=exc.detail,
                )
            raise

        reports = self._get_direct_reports(db, org_id, manager_employee_id)
        report_ids = [emp.employee_id for emp in reports]
        items = []
        total = 0
        pagination = PaginationParams.from_page(page, per_page=20)

        scope_filters = [LeaveApplication.leave_approver_id == manager_employee_id]
        if report_ids:
            scope_filters.append(LeaveApplication.employee_id.in_(report_ids))

        query = (
            select(LeaveApplication)
            .options(
                joinedload(LeaveApplication.employee).joinedload(Employee.person),
                joinedload(LeaveApplication.leave_type),
            )
            .where(
                LeaveApplication.organization_id == org_id,
                or_(*scope_filters),
            )
        )
        if status:
            try:
                status_value = LeaveApplicationStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
            query = query.where(LeaveApplication.status == status_value)
        query = query.order_by(LeaveApplication.from_date.desc())
        count_query = select(func.count()).select_from(query.subquery())
        total = db.scalar(count_query) or 0
        items = list(
            db.scalars(query.offset(pagination.offset).limit(pagination.limit)).all()
        )

        total_pages = (total + pagination.limit - 1) // pagination.limit if total else 1
        active_filters = build_active_filters(
            params={"status": status},
            labels={"status": "Status"},
        )
        context = base_context(request, auth, "Team Leave", "self-team-leave", db=db)
        context.update(
            {
                "applications": items,
                "status": status,
                "statuses": [s.value for s in LeaveApplicationStatus],
                "active_filters": active_filters,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "total_count": total,
                "limit": pagination.limit,
                "has_prev": page > 1,
                "has_next": pagination.offset + pagination.limit < total,
            }
        )
        context["has_team_approvals"] = True
        context["can_team_leave"] = True
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/team_leave.html", context
        )

    def team_leave_approve_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        application_id: UUID,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        application = LeaveService(db, auth).get_application(org_id, application_id)
        if application.employee_id == manager_employee_id:
            raise HTTPException(status_code=400, detail="Cannot approve own leave")

        report_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)
        if (
            application.employee_id not in report_ids
            and application.leave_approver_id != manager_employee_id
        ):
            raise HTTPException(status_code=403, detail="Forbidden")

        LeaveService(db, auth).approve_application(
            org_id=org_id,
            application_id=application_id,
            approver_id=person_id,
        )
        db.commit()
        return RedirectResponse(url="/people/self/team/leave", status_code=302)

    def team_leave_reject_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        application_id: UUID,
        reason: str | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        application = LeaveService(db, auth).get_application(org_id, application_id)
        report_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)
        if (
            application.employee_id not in report_ids
            and application.leave_approver_id != manager_employee_id
        ):
            raise HTTPException(status_code=403, detail="Forbidden")

        LeaveService(db, auth).reject_application(
            org_id=org_id,
            application_id=application_id,
            approver_id=person_id,
            reason=reason or "Rejected",
        )
        db.commit()
        return RedirectResponse(url="/people/self/team/leave", status_code=302)

    def team_expenses_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        status: str | None = None,
        decision: str | None = None,
        employee_id: UUID | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            approver_employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Team Expenses",
                    "self-team-expenses",
                    detail=exc.detail,
                )
            raise

        normalized_status = status.upper() if status else None
        normalized_decision = decision.upper() if decision else None
        try:
            report_data = ExpenseService(db, auth).get_my_approvals_report(
                org_id,
                approver_id=approver_employee_id,
                start_date=start_date,
                end_date=end_date,
                decision=normalized_decision,
                claim_status=normalized_status,
                employee_id=employee_id,
                include_pending=True,
                filter_by_claim_date=True,
                use_default_date_range=False,
                limit=1000,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        decisions = report_data["decisions"]
        if status:
            normalized_status = status.upper()
        if decision:
            normalized_decision = decision.upper()

        pagination = PaginationParams.from_page(page, per_page=20)
        total = len(decisions)
        items = decisions[pagination.offset : pagination.offset + pagination.limit]
        total_pages = (total + pagination.limit - 1) // pagination.limit if total else 1

        weekly_balance = None
        budget_balance = ExpenseLimitService(db).get_approver_weekly_budget_balance(
            org_id,
            approver_employee_id,
        )
        if budget_balance is not None:
            weekly_balance = {
                "usage_label": budget_balance.usage_label,
                "budget": budget_balance.budget,
                "used": budget_balance.used,
                "remaining": budget_balance.remaining,
                "last_reset_at": budget_balance.last_reset_at,
            }

        context = base_context(
            request, auth, "Team Expenses", "self-team-expenses", db=db
        )
        status_options = [
            ExpenseClaimStatus.SUBMITTED.value,
            ExpenseClaimStatus.PENDING_APPROVAL.value,
            ExpenseClaimStatus.APPROVED.value,
            ExpenseClaimStatus.REJECTED.value,
            ExpenseClaimStatus.PAID.value,
            ExpenseClaimStatus.CANCELLED.value,
        ]
        employee_options = report_data["employee_options"]
        active_filters = build_active_filters(
            params={
                "decision": normalized_decision,
                "status": normalized_status,
                "employee_id": str(employee_id) if employee_id else None,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
            },
            labels={
                "decision": "Decision",
                "status": "Status",
                "employee_id": "Employee",
                "start_date": "From",
                "end_date": "To",
            },
            options={
                "employee_id": {
                    option["employee_id"]: option["name"] for option in employee_options
                }
            },
        )
        context.update(
            {
                "approvals": items,
                "status": normalized_status,
                "decision": normalized_decision,
                "employee_id": employee_id,
                "selected_employee_id": str(employee_id) if employee_id else "",
                "start_date": start_date,
                "end_date": end_date,
                "statuses": status_options,
                "decisions": ["PENDING", "APPROVED", "REJECTED"],
                "employee_options": employee_options,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": pagination.offset + pagination.limit < total,
                "weekly_balance": weekly_balance,
                "summary": {
                    "approved_count": report_data["approved_count"],
                    "rejected_count": report_data["rejected_count"],
                    "pending_count": report_data["pending_count"],
                    "paid_count": report_data["paid_count"],
                    "approved_total": report_data["approved_total"],
                    "rejected_total": report_data["rejected_total"],
                    "pending_total": report_data["pending_total"],
                    "paid_total": report_data["paid_total"],
                },
                "active_filters": active_filters,
                "success": request.query_params.get("success"),
                "error": request.query_params.get("error"),
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=approver_employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=approver_employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/team_expenses.html", context
        )

    def team_expense_approve_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        claim = ExpenseService(db, auth).get_claim(org_id, claim_id)
        employee_svc = EmployeeService(db, org_id)
        reports = employee_svc.list_employees(
            filters=EmployeeFilters(expense_approver_id=manager_employee_id),
            pagination=PaginationParams(offset=0, limit=1000),
        ).items
        report_ids = {emp.employee_id for emp in reports}
        if claim.employee_id not in report_ids:
            raise HTTPException(status_code=403, detail="Forbidden")

        try:
            ExpenseService(db, auth).approve_claim(
                org_id=org_id,
                claim_id=claim_id,
                approver_id=manager_employee_id,
                actor_id=person_id,
            )
            db.commit()
        except (ApproverAuthorityError, ExpenseLimitServiceError) as exc:
            db.rollback()
            return RedirectResponse(
                url=f"/people/self/my-approvals?error={quote(str(exc))}",
                status_code=303,
            )
        except ExpenseClaimStatusError:
            db.rollback()
            return RedirectResponse(
                url="/people/self/my-approvals?error=This+claim+can+no+longer+be+approved+in+its+current+status.",
                status_code=303,
            )
        except ValueError as exc:
            db.rollback()
            message = str(exc).strip() or "Approval could not be completed."
            if message == "Approver is not assigned to the current approval step":
                message = (
                    "You cannot approve this claim yet because it is assigned "
                    "to a different approval step."
                )
            elif message == "Claim has no pending approval steps":
                message = "This claim has no pending approval step."
            return RedirectResponse(
                url=f"/people/self/my-approvals?error={quote(message)}",
                status_code=303,
            )
        except ExpenseServiceError as exc:
            db.rollback()
            return RedirectResponse(
                url=f"/people/self/my-approvals?error={quote(str(exc))}",
                status_code=303,
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Team expense approval failed", extra={"claim_id": claim_id}
            )
            return RedirectResponse(
                url="/people/self/my-approvals?error=Approval+failed.+Please+refresh+and+try+again.",
                status_code=303,
            )

        return RedirectResponse(
            url="/people/self/my-approvals?success=Claim+approved", status_code=303
        )

    def team_expense_reject_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        claim_id: UUID,
        reason: str | None = None,
    ) -> RedirectResponse:
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        claim = ExpenseService(db, auth).get_claim(org_id, claim_id)
        employee_svc = EmployeeService(db, org_id)
        reports = employee_svc.list_employees(
            filters=EmployeeFilters(expense_approver_id=manager_employee_id),
            pagination=PaginationParams(offset=0, limit=1000),
        ).items
        report_ids = {emp.employee_id for emp in reports}
        if claim.employee_id not in report_ids:
            raise HTTPException(status_code=403, detail="Forbidden")

        ExpenseService(db, auth).reject_claim(
            org_id=org_id,
            claim_id=claim_id,
            approver_id=manager_employee_id,
            reason=reason or "Rejected",
            actor_id=person_id,
        )
        db.commit()
        return RedirectResponse(url="/people/self/my-approvals", status_code=302)

    # ============ Payslips Self-Service ============

    def payslips_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        year: int | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """Self-service payslips list page."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "My Payslips",
                    "self-payslips",
                    detail=exc.detail,
                )
            raise

        pagination = PaginationParams.from_page(page, per_page=12)

        # Query salary slips for this employee
        query = select(SalarySlip).where(
            SalarySlip.organization_id == org_id,
            SalarySlip.employee_id == employee_id,
            SalarySlip.status.in_(SalarySlipStatus.gl_impacting()),
        )

        if year:
            query = query.where(func.extract("year", SalarySlip.start_date) == year)

        query = query.order_by(SalarySlip.start_date.desc())

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total = db.scalar(count_query) or 0

        # Get paginated items
        slips = db.scalars(
            query.offset(pagination.offset).limit(pagination.limit)
        ).all()

        # Get available years for filtering
        years_query = (
            select(func.distinct(func.extract("year", SalarySlip.start_date)))
            .where(
                SalarySlip.organization_id == org_id,
                SalarySlip.employee_id == employee_id,
                SalarySlip.status.in_(SalarySlipStatus.gl_impacting()),
            )
            .order_by(func.extract("year", SalarySlip.start_date).desc())
        )
        available_years = [int(y[0]) for y in db.execute(years_query).all() if y[0]]

        total_pages = (total + pagination.limit - 1) // pagination.limit if total else 1

        context = base_context(request, auth, "My Payslips", "self-payslips", db=db)
        active_filters = build_active_filters(
            params={"year": str(year) if year else None},
            labels={"year": "Year"},
        )
        context.update(
            {
                "slips": slips,
                "year": year,
                "available_years": available_years,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": pagination.offset + pagination.limit < total,
                "active_filters": active_filters,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(request, "people/self/payslips.html", context)

    def payslip_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        slip_id: UUID,
    ) -> HTMLResponse:
        """Self-service payslip detail page with PAYE breakdown."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Payslip Detail",
                    "self-payslips",
                    detail=exc.detail,
                )
            raise

        # Get the salary slip
        slip = db.get(SalarySlip, slip_id)
        if not slip or slip.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Payslip not found")

        # Ensure employee can only view their own slips
        if slip.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Only show posted/paid slips in self-service
        if slip.status not in SalarySlipStatus.gl_impacting():
            raise HTTPException(status_code=403, detail="Payslip not yet available")

        # Calculate PAYE breakdown for display
        paye_breakdown = None
        if slip.gross_pay and slip.gross_pay > 0:
            # Find basic pay from earnings
            basic_pay = Decimal("0")
            for earning in slip.earnings:
                if earning.abbr and earning.abbr.upper() in ("BAS", "BASIC"):
                    basic_pay = earning.amount
                    break

            # If no BASIC found, estimate from gross (60% assumption)
            if basic_pay == 0:
                basic_pay = slip.gross_pay * Decimal("0.6")

            calculator = PAYECalculator(db)
            paye_breakdown = calculator.calculate(
                organization_id=org_id,
                gross_monthly=slip.gross_pay,
                basic_monthly=basic_pay,
                employee_id=employee_id,
                as_of_date=slip.start_date,
            )

        context = base_context(
            request, auth, f"Payslip {slip.slip_number}", "self-payslips", db=db
        )
        context.update(
            {
                "slip": slip,
                "paye_breakdown": paye_breakdown,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/payslip_detail.html", context
        )

    # =========================================================================
    # Discipline Self-Service
    # =========================================================================

    def discipline_cases_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        include_closed: bool = False,
    ) -> HTMLResponse:
        """Self-service disciplinary cases list."""
        org_id = coerce_uuid(auth.organization_id)
        person_id = auth.person_id

        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException:
            return self._employee_required_response(
                request, auth, db, "Discipline", "self-discipline"
            )

        from app.services.people.discipline import DisciplineService

        discipline_service = DisciplineService(db)
        cases, _total = discipline_service.list_employee_cases(
            org_id, employee_id, include_closed=include_closed
        )

        # Mark cases that need response
        for case in cases:
            case.has_pending_response = (  # type: ignore[attr-defined]
                case.status.value == "QUERY_ISSUED"
                and case.response_due_date is not None
            )

        context = base_context(request, auth, "Discipline", "self-discipline", db=db)
        context.update(
            {
                "cases": cases,
                "include_closed": include_closed,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/discipline.html", context
        )

    def discipline_case_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        case_id: UUID,
    ) -> HTMLResponse:
        """Self-service disciplinary case detail view."""
        org_id = auth.organization_id
        person_id = auth.person_id

        try:
            employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException:
            return self._employee_required_response(
                request, auth, db, "Discipline", "self-discipline"
            )

        from app.models.people.discipline import CaseStatus
        from app.services.people.discipline import DisciplineService

        discipline_service = DisciplineService(db)
        try:
            case = discipline_service.get_case_detail(case_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Case not found")

        # Verify this is the employee's own case
        if case.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Determine what actions are available
        can_respond = case.status == CaseStatus.QUERY_ISSUED
        can_appeal = (
            case.status == CaseStatus.DECISION_MADE
            and case.appeal_deadline is not None
            and date.today() <= case.appeal_deadline
        )

        context = base_context(
            request, auth, f"Case {case.case_number}", "self-discipline", db=db
        )
        context.update(
            {
                "case": case,
                "can_respond": can_respond,
                "can_appeal": can_appeal,
            }
        )
        context["has_team_approvals"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        context["can_team_leave"] = context["has_team_approvals"]
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=employee_id
        )
        return templates.TemplateResponse(
            request, "people/self/discipline_detail.html", context
        )

    def discipline_submit_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        case_id: UUID,
        response_text: str,
    ) -> RedirectResponse:
        """Submit employee response to disciplinary query."""
        org_id = auth.organization_id
        person_id = auth.person_id

        employee_id = self._get_employee_id(db, org_id, person_id)

        from app.schemas.people.discipline import CaseResponseCreate
        from app.services.people.discipline import DisciplineService

        discipline_service = DisciplineService(db)
        case = discipline_service.get_case_or_404(
            case_id, organization_id=coerce_uuid(org_id)
        )

        # Verify this is the employee's own case
        if case.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        response_data = CaseResponseCreate(response_text=response_text)
        discipline_service.record_response(case_id, response_data)
        db.commit()

        return RedirectResponse(
            url=f"/people/self/discipline/{case_id}?success=response_submitted",
            status_code=303,
        )

    def discipline_file_appeal_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        case_id: UUID,
        appeal_reason: str,
    ) -> RedirectResponse:
        """File an appeal against disciplinary decision."""
        org_id = auth.organization_id
        person_id = auth.person_id

        employee_id = self._get_employee_id(db, org_id, person_id)

        from app.schemas.people.discipline import FileAppealRequest
        from app.services.people.discipline import DisciplineService

        discipline_service = DisciplineService(db)
        case = discipline_service.get_case_or_404(
            case_id, organization_id=coerce_uuid(org_id)
        )

        # Verify this is the employee's own case
        if case.employee_id != employee_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        appeal_data = FileAppealRequest(appeal_reason=appeal_reason)
        discipline_service.file_appeal(case_id, appeal_data)
        db.commit()

        return RedirectResponse(
            url=f"/people/self/discipline/{case_id}?success=appeal_filed",
            status_code=303,
        )

    def team_discipline_cases_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        include_closed: bool = False,
        page: int = 1,
    ) -> HTMLResponse:
        """List discipline cases for permitted team scope."""
        from app.models.people.discipline import CaseStatus, DisciplinaryCase

        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            manager_employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Team Discipline",
                    "self-team-discipline",
                    detail=exc.detail,
                )
            raise

        reports = self._get_direct_reports(db, org_id, manager_employee_id)
        report_ids = [emp.employee_id for emp in reports]
        has_direct_reports = bool(report_ids)
        team_employee_ids = self._get_team_discipline_employee_ids(
            db,
            org_id,
            manager_employee_id,
            auth,
        )
        has_team_scope = bool(team_employee_ids)
        can_create_team_discipline = has_direct_reports

        pagination = PaginationParams.from_page(page, per_page=20)
        total = 0
        cases = []
        if team_employee_ids:
            query = select(DisciplinaryCase).where(
                DisciplinaryCase.organization_id == org_id,
                DisciplinaryCase.employee_id.in_(team_employee_ids),
                DisciplinaryCase.status != CaseStatus.WITHDRAWN,
            )
            if not include_closed:
                query = query.where(
                    DisciplinaryCase.status.notin_(
                        [CaseStatus.CLOSED, CaseStatus.WITHDRAWN]
                    )
                )
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            cases = list(
                db.scalars(
                    query.order_by(DisciplinaryCase.created_at.desc())
                    .offset(pagination.offset)
                    .limit(pagination.limit)
                ).all()
            )

        total_pages = (total + pagination.limit - 1) // pagination.limit if total else 1
        context = base_context(
            request, auth, "Team Discipline", "self-team-discipline", db=db
        )
        context.update(
            {
                "cases": cases,
                "include_closed": include_closed,
                "has_direct_reports": has_direct_reports,
                "has_team_scope": has_team_scope,
                "can_create_team_discipline": can_create_team_discipline,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": pagination.offset + pagination.limit < total,
            }
        )
        context["has_team_approvals"] = (
            self._has_team_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or self._has_team_expense_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or has_team_scope
        )
        context["can_team_leave"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_discipline"] = True
        return templates.TemplateResponse(
            request, "people/self/team_discipline.html", context
        )

    def team_discipline_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        error: str | None = None,
        form_data: dict[str, str] | None = None,
    ) -> HTMLResponse:
        """Render form for creating team discipline case."""
        from app.models.people.discipline import SeverityLevel, ViolationType

        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            manager_employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Team Discipline",
                    "self-team-discipline",
                    detail=exc.detail,
                )
            raise

        reports = self._get_direct_reports(db, org_id, manager_employee_id)

        context = base_context(
            request, auth, "New Team Discipline Case", "self-team-discipline", db=db
        )
        context.update(
            {
                "error": error,
                "form_data": form_data or {},
                "reports": reports,
                "has_direct_reports": bool(reports),
                "violation_types": [v.value for v in ViolationType],
                "severities": [s.value for s in SeverityLevel],
            }
        )
        context["has_team_approvals"] = (
            self._has_team_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or self._has_team_expense_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or bool(reports)
        )
        context["can_team_leave"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_discipline"] = True
        return templates.TemplateResponse(
            request, "people/self/team_discipline_new.html", context
        )

    def team_discipline_create_case_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        employee_id: str,
        violation_type: str,
        severity: str,
        subject: str,
        description: str | None = None,
        incident_date: str | None = None,
        query_text: str,
        response_due_date: str,
    ) -> RedirectResponse | HTMLResponse:
        """Create a team discipline case and immediately issue a query."""
        from app.models.people.discipline import SeverityLevel, ViolationType
        from app.schemas.people.discipline import (
            DisciplinaryCaseCreate,
            IssueQueryRequest,
        )
        from app.services.people.discipline import DisciplineService

        required = [
            employee_id,
            violation_type,
            severity,
            subject,
            query_text,
            response_due_date,
        ]
        form_data = {
            "employee_id": employee_id,
            "violation_type": violation_type,
            "severity": severity,
            "subject": subject,
            "description": description or "",
            "incident_date": incident_date or "",
            "query_text": query_text,
            "response_due_date": response_due_date,
        }
        if any(not str(value or "").strip() for value in required):
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error="Employee, violation type, severity, subject, query text, and response due date are required.",
                form_data=form_data,
            )

        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        employee_uuid = coerce_uuid(employee_id)
        if employee_uuid is None:
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error="Invalid employee selected.",
                form_data=form_data,
            )

        report_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)
        if employee_uuid not in report_ids:
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error="You can only create cases for your direct reports.",
                form_data=form_data,
            )

        try:
            violation = ViolationType(violation_type)
            severity_level = SeverityLevel(severity)
        except ValueError:
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error="Invalid violation type or severity.",
                form_data=form_data,
            )

        try:
            due_date = date.fromisoformat(response_due_date)
            incident = date.fromisoformat(incident_date) if incident_date else None
        except ValueError:
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error="Dates must be in YYYY-MM-DD format.",
                form_data=form_data,
            )

        service = DisciplineService(db)
        try:
            case = service.create_case(
                org_id,
                DisciplinaryCaseCreate(
                    employee_id=employee_uuid,
                    violation_type=violation,
                    severity=severity_level,
                    subject=subject,
                    description=description,
                    incident_date=incident,
                    reported_date=date.today(),
                    reported_by_id=manager_employee_id,
                ),
                created_by_id=person_id,
            )
            service.issue_query(
                case.case_id,
                IssueQueryRequest(query_text=query_text, response_due_date=due_date),
                issued_by_id=person_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/people/self/team/discipline/{case.case_id}?success=case_created",
                status_code=303,
            )
        except (ValidationError, HTTPException) as exc:
            db.rollback()
            message = getattr(exc, "detail", None) or str(exc)
            return self.team_discipline_new_form_response(
                request,
                auth,
                db,
                error=message,
                form_data=form_data,
            )

    def team_discipline_case_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        case_id: UUID,
    ) -> HTMLResponse:
        """View team discipline case detail within permitted team scope."""
        from app.models.people.discipline import CaseStatus
        from app.services.people.discipline import DisciplineService

        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        try:
            manager_employee_id = self._get_employee_id(db, org_id, person_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return self._employee_required_response(
                    request,
                    auth,
                    db,
                    "Team Discipline",
                    "self-team-discipline",
                    detail=exc.detail,
                )
            raise

        report_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)
        team_employee_ids = self._get_team_discipline_employee_ids(
            db,
            org_id,
            manager_employee_id,
            auth,
        )

        try:
            case = DisciplineService(db).get_case_detail(case_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Case not found")

        if case.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Case not found")
        if case.employee_id not in team_employee_ids:
            raise HTTPException(status_code=403, detail="Forbidden")

        can_issue_query = (
            case.status == CaseStatus.DRAFT and case.employee_id in report_ids
        )
        context = base_context(
            request,
            auth,
            f"Team Case {case.case_number}",
            "self-team-discipline",
            db=db,
        )
        context.update(
            {
                "case": case,
                "can_issue_query": can_issue_query,
            }
        )
        context["has_team_approvals"] = (
            self._has_team_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or self._has_team_expense_approvals(
                db, org_id, person_id, employee_id=manager_employee_id
            )
            or bool(team_employee_ids)
        )
        context["can_team_leave"] = self._has_team_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_expenses"] = self._has_team_expense_approvals(
            db, org_id, person_id, employee_id=manager_employee_id
        )
        context["can_team_discipline"] = True
        return templates.TemplateResponse(
            request, "people/self/team_discipline_detail.html", context
        )

    def team_discipline_issue_query_response(
        self,
        auth: WebAuthContext,
        db: Session,
        *,
        case_id: UUID,
        query_text: str,
        response_due_date: str,
    ) -> RedirectResponse:
        """Issue query to employee for team discipline case."""
        from app.schemas.people.discipline import IssueQueryRequest
        from app.services.people.discipline import DisciplineService

        if not query_text or not response_due_date:
            return RedirectResponse(
                url=f"/people/self/team/discipline/{case_id}?error={quote('Query text and response due date are required.')}",
                status_code=303,
            )

        org_id = coerce_uuid(auth.organization_id)
        person_id = coerce_uuid(auth.person_id)
        manager_employee_id = self._get_employee_id(db, org_id, person_id)

        report_ids = self._get_direct_report_ids(db, org_id, manager_employee_id)

        service = DisciplineService(db)
        case = service.get_case_or_404(case_id, organization_id=coerce_uuid(org_id))
        if case.employee_id not in report_ids:
            raise HTTPException(status_code=403, detail="Forbidden")

        try:
            due_date = date.fromisoformat(response_due_date)
        except ValueError:
            return RedirectResponse(
                url=f"/people/self/team/discipline/{case_id}?error={quote('Response due date must be in YYYY-MM-DD format.')}",
                status_code=303,
            )

        try:
            service.issue_query(
                case_id=case_id,
                data=IssueQueryRequest(
                    query_text=query_text, response_due_date=due_date
                ),
                issued_by_id=person_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/people/self/team/discipline/{case_id}?success=query_issued",
                status_code=303,
            )
        except (ValidationError, HTTPException) as exc:
            db.rollback()
            message = quote(getattr(exc, "detail", None) or str(exc))
            return RedirectResponse(
                url=f"/people/self/team/discipline/{case_id}?error={message}",
                status_code=303,
            )


self_service_web_service = SelfServiceWebService()
