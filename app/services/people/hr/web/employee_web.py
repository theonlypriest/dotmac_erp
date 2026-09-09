"""HR Web Service - Employee and organization web view methods.

Provides view-focused data and operations for HR web routes.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from html import escape
from io import StringIO
from urllib.parse import urlencode

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import UploadFile

from app.config import settings
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus, UserCredential
from app.models.finance.core_org.cost_center import CostCenter
from app.models.finance.core_org.location import Location
from app.models.finance.core_org.pfa_directory import PFADirectory
from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeGrade,
    EmployeeStatus,
    Position,
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.people.hr.employee import SalaryMode
from app.models.people.payroll.employee_tax_profile import EmployeeTaxProfile
from app.models.people.payroll.salary_assignment import SalaryStructureAssignment
from app.models.people.payroll.salary_structure import SalaryStructure
from app.models.person import Gender, Person
from app.net import get_request_host, get_request_scheme
from app.schemas.person import PersonUpdate
from app.services.common import PaginationParams, ServiceError, coerce_uuid
from app.services.common_filters import build_active_filters
from app.services.fixed_assets.asset_query import list_employee_assigned_assets
from app.services.formatters import parse_bool
from app.services.people.attendance.attendance_service import AttendanceService
from app.services.people.hr import (
    DepartmentFilters,
    DesignationFilters,
    EmployeeCreateData,
    EmployeeFilters,
    EmployeeGradeFilters,
    EmployeeNotFoundError,
    EmployeeService,
    EmployeeUpdateData,
    EmploymentTypeFilters,
    OrganizationService,
    TerminationData,
)
from app.services.people.hr.employee_filter_engine import (
    parse_employee_filter_payload_json,
)
from app.services.people.hr.employment_types import (
    EmploymentTypeService,
    EmploymentTypeView,
)
from app.services.people.hr.org_resolver import OrgResolver
from app.services.people.hr.offboarding import (
    queue_employee_mailcow_offboarding,
    should_offboard_status,
)
from app.services.people.hr.web.constants import DEFAULT_PAGE_SIZE, DROPDOWN_LIMIT
from app.services.recent_activity import get_recent_activity_for_record
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

logger = logging.getLogger(__name__)

DEPARTMENT_MANAGE_ROLES = frozenset({"admin", "hr_director", "hr_manager"})


def _can_manage_departments(auth: WebAuthContext) -> bool:
    """Return whether the current HR user can manage department records."""
    if auth.has_permission("hr:departments:manage"):
        return True
    return bool({role.strip().lower() for role in auth.roles} & DEPARTMENT_MANAGE_ROLES)


NIGERIA_STATES = [
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


class HRWebService:
    """Service for HR web views."""

    FINAL_PAYROLL_EDITOR_ROLES = frozenset({"admin", "hr_director", "hr_manager"})

    EMPLOYEE_EXPORT_FIELDS: dict[
        str, tuple[str, Callable[[Employee], object] | None]
    ] = {
        "employee_code": ("Employee Code", lambda emp: emp.employee_code),
        "full_name": ("Full Name", lambda emp: emp.person.name if emp.person else ""),
        "work_email": (
            "Work Email",
            lambda emp: emp.person.email if emp.person else "",
        ),
        "work_phone": (
            "Work Phone",
            lambda emp: emp.person.phone if emp.person else "",
        ),
        "personal_email": ("Personal Email", lambda emp: emp.personal_email),
        "personal_phone": ("Personal Phone", lambda emp: emp.personal_phone),
        "department": (
            "Department",
            lambda emp: emp.department.department_name if emp.department else "",
        ),
        "designation": (
            "Designation",
            lambda emp: emp.designation.designation_name if emp.designation else "",
        ),
        "employment_type": (
            "Employment Type",
            None,
        ),
        "status": ("Status", lambda emp: emp.status.value if emp.status else ""),
        "date_of_joining": ("Date of Joining", lambda emp: emp.date_of_joining),
        "date_of_leaving": ("Date of Leaving", lambda emp: emp.date_of_leaving),
        "probation_end_date": (
            "Probation End Date",
            lambda emp: emp.probation_end_date,
        ),
        "confirmation_date": ("Confirmation Date", lambda emp: emp.confirmation_date),
    }
    DEFAULT_EMPLOYEE_EXPORT_FIELDS = (
        "employee_code",
        "full_name",
        "work_email",
        "department",
        "designation",
        "employment_type",
        "status",
        "date_of_joining",
    )

    # =========================================================================
    # Employees
    # =========================================================================

    @staticmethod
    def _resolve_app_url(request: Request) -> str:
        scheme = get_request_scheme(request)
        host = get_request_host(request) or request.url.netloc
        return f"{scheme}://{host}".rstrip("/")

    @staticmethod
    async def _request_form(request: Request) -> Any:
        """Return parsed POST form data, ignoring template-only CSRF HTML."""
        form = getattr(request.state, "csrf_form", None)
        if form is None or isinstance(form, str):
            return await request.form()
        return form

    @staticmethod
    def _can_manage_final_payroll(auth: WebAuthContext) -> bool:
        roles = {role.strip().lower() for role in auth.roles if role and role.strip()}
        return bool(roles.intersection(HRWebService.FINAL_PAYROLL_EDITOR_ROLES))

    @staticmethod
    def _clean_person_text(value: str | None) -> str | None:
        cleaned = (value or "").strip()
        if not cleaned or cleaned.lower() in {"none", "null"}:
            return None
        return cleaned

    @staticmethod
    def _clean_optional_text(value: str | None) -> str | None:
        """Normalize optional string values coming from forms or persisted text."""
        cleaned = (value or "").strip()
        if not cleaned or cleaned.lower() in {"none", "null"}:
            return None
        return cleaned

    @staticmethod
    def _load_manager_position_titles(
        db: Session,
        org_id: UUID,
        employee_ids: list[UUID],
    ) -> dict[UUID, str]:
        """
        Return a ``{employee_id: designation_name}`` map for the active PRIMARY
        position of each given employee.

        Used to enrich manager-picker dropdowns so users see "Sarah Adeyemi -
        Field Operations Manager" rather than a bare name. Employees without
        an active PRIMARY position assignment, or whose position lacks a
        designation, are omitted from the map. Two queries regardless of
        input size — safe to call on full dropdown sets.
        """
        if not employee_ids:
            return {}

        assignment_rows = db.execute(
            select(
                PositionAssignment.employee_id,
                PositionAssignment.position_id,
            ).where(
                PositionAssignment.organization_id == org_id,
                PositionAssignment.employee_id.in_(employee_ids),
                PositionAssignment.assignment_type == PositionAssignmentType.PRIMARY,
                PositionAssignment.end_date.is_(None),
            )
        ).all()
        if not assignment_rows:
            return {}

        position_to_employees: dict[UUID, list[UUID]] = {}
        for row in assignment_rows:
            position_to_employees.setdefault(row.position_id, []).append(
                row.employee_id
            )

        designation_rows = db.execute(
            select(
                Position.position_id,
                Designation.designation_name,
            )
            .join(Designation, Position.designation_id == Designation.designation_id)
            .where(Position.position_id.in_(position_to_employees.keys()))
        ).all()

        titles: dict[UUID, str] = {}
        for desig_row in designation_rows:
            for emp_id in position_to_employees.get(desig_row.position_id, ()):
                titles[emp_id] = desig_row.designation_name
        return titles

    @staticmethod
    def _employee_position_context(
        db: Session,
        org_id: UUID,
        employee_id: UUID,
    ) -> dict[str, Any]:
        """Return current position/reporting-line display data for an employee."""
        resolver = OrgResolver(db)
        assignment = resolver.get_active_assignment(employee_id, org_id)
        position = None
        parent_position = None
        if assignment:
            position = assignment.position or db.get(Position, assignment.position_id)
            if position and position.parent_position_id:
                parent_position = db.get(Position, position.parent_position_id)

        manager = resolver.get_manager(employee_id, org_id)
        return {
            "position": position,
            "parent_position": parent_position,
            "manager": manager,
        }

    @staticmethod
    def _list_vacant_position_options(db: Session, org_id: UUID) -> list[Any]:
        """Return vacant positions that can receive a new employee assignment."""
        today = date.today()
        current_active_incumbent = (
            select(PositionAssignment.position_assignment_id)
            .join(
                Employee,
                Employee.employee_id == PositionAssignment.employee_id,
            )
            .where(
                Employee.organization_id == org_id,
                Employee.status == EmployeeStatus.ACTIVE,
                PositionAssignment.organization_id == org_id,
                PositionAssignment.position_id == Position.position_id,
                PositionAssignment.start_date <= today,
                (
                    PositionAssignment.end_date.is_(None)
                    | (PositionAssignment.end_date >= today)
                ),
            )
            .exists()
        )
        positions = db.scalars(
            select(Position)
            .where(
                Position.organization_id == org_id,
                Position.is_active.is_(True),
                ~current_active_incumbent,
            )
            .order_by(Position.position_code)
            .limit(DROPDOWN_LIMIT)
        ).all()
        return [{"position": position} for position in positions]

    def employee_position_options_response(
        self,
        auth: WebAuthContext,
        db: Session,
        selected_position_id: str | None = None,
    ) -> str:
        """Return the position-seat select for lazy employee-form loading."""
        org_id = coerce_uuid(auth.organization_id)
        try:
            options = self._list_vacant_position_options(db, org_id)
        except Exception:
            logger.exception("Failed to lazy-load vacant employee positions")
            options = []
        selected = (
            str(coerce_uuid(selected_position_id)) if selected_position_id else ""
        )

        rows = [
            '<select name="position_id" id="position_id" class="form-input w-full">',
            '<option value="">Create a new position from employee details</option>',
        ]
        if not options:
            rows.append(
                '<option value="" disabled>No vacant position seats available</option>'
            )
        for summary in options:
            position = summary["position"]
            position_id = str(position.position_id)
            is_selected = " selected" if position_id == selected else ""
            label = escape(f"{position.position_code} - {position.position_name}")
            rows.append(f'<option value="{position_id}"{is_selected}>{label}</option>')
        rows.append("</select>")
        return "".join(rows)

    @staticmethod
    def _designation_is_nysc(designation_name: str | None) -> bool:
        """Return True when the designation is an NYSC tenure role."""
        return (designation_name or "").strip().upper().endswith("(NYSC)")

    def _designation_requires_nysc_dates(
        self,
        db: Session,
        organization_id: UUID,
        designation_id: str,
    ) -> bool:
        """Check if the selected designation requires NYSC date tracking."""
        if not designation_id:
            return False

        designation = db.scalar(
            select(Designation).where(
                Designation.designation_id == coerce_uuid(designation_id),
                Designation.organization_id == organization_id,
            )
        )
        if not designation:
            return False

        return self._designation_is_nysc(designation.designation_name)

    def _validate_nysc_dates(
        self,
        *,
        db: Session,
        organization_id: UUID,
        designation_id: str,
        nysc_start_date: str,
        nysc_end_date: str,
    ) -> tuple[dict[str, str], date | None, date | None]:
        """Validate NYSC date requirements for temporary designations."""
        requires_nysc_dates = self._designation_requires_nysc_dates(
            db,
            organization_id,
            designation_id,
        )
        start_date_value = self._parse_date(nysc_start_date)
        end_date_value = self._parse_date(nysc_end_date)
        errors: dict[str, str] = {}

        if not requires_nysc_dates:
            return errors, None, None

        if not nysc_start_date:
            errors["nysc_start_date"] = "Required for NYSC designation"
        elif start_date_value is None:
            errors["nysc_start_date"] = "Enter a valid date"

        if not nysc_end_date:
            errors["nysc_end_date"] = "Required for NYSC designation"
        elif end_date_value is None:
            errors["nysc_end_date"] = "Enter a valid date"

        if (
            start_date_value is not None
            and end_date_value is not None
            and end_date_value < start_date_value
        ):
            errors["nysc_end_date"] = "End date must be on or after start date"

        return errors, start_date_value, end_date_value

    def _update_linked_person(
        self,
        *,
        auth: WebAuthContext,
        db: Session,
        employee: Employee,
        form: Any,
    ) -> None:
        """Apply linked Person updates when the caller has people:write."""
        if not auth.has_permission("people:write") or not employee.person_id:
            return

        person = db.get(Person, employee.person_id)
        org_id = coerce_uuid(auth.organization_id)
        if not person or person.organization_id != org_id:
            return

        gender_value = self._clean_person_text(self._form_str(form, "gender"))
        gender: str | None = None
        if gender_value:
            try:
                gender = Gender(gender_value).value
            except ValueError:
                gender = person.gender.value if person.gender else None
        else:
            gender = person.gender.value if person.gender else None

        payload_data: dict[str, Any] = {
            "first_name": self._clean_person_text(self._form_str(form, "first_name")),
            "last_name": self._clean_person_text(self._form_str(form, "last_name")),
            "email": self._clean_person_text(self._form_str(form, "email")),
            "phone": self._clean_person_text(self._form_str(form, "phone")),
            "date_of_birth": self._parse_date(self._form_str(form, "date_of_birth")),
            "gender": gender,
            "address_line1": self._clean_person_text(
                self._form_str(form, "address_line1")
            ),
            "address_line2": self._clean_person_text(
                self._form_str(form, "address_line2")
            ),
            "city": self._clean_person_text(self._form_str(form, "city")),
            "region": self._clean_person_text(self._form_str(form, "region")),
            "postal_code": self._clean_person_text(self._form_str(form, "postal_code")),
            "country_code": (
                self._clean_person_text(self._form_str(form, "country_code")) or ""
            ).upper()
            or None,
        }

        try:
            payload = PersonUpdate.model_validate(payload_data)
        except PydanticValidationError as exc:
            invalid_fields = {err["loc"][0] for err in exc.errors() if err.get("loc")}
            if "email" in invalid_fields:
                logger.warning(
                    "Skipping linked person email update because email failed validation",
                    extra={
                        "employee_id": str(employee.employee_id),
                        "person_id": str(person.id),
                        "submitted_email": payload_data.get("email"),
                    },
                )
                payload_data.pop("email", None)
                payload = PersonUpdate.model_validate(payload_data)
            else:
                raise

        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(person, key, value)

    def _update_tax_profile(
        self,
        *,
        auth: WebAuthContext,
        db: Session,
        employee: Employee,
        form: Any,
    ) -> None:
        if not auth.has_permission("people:write"):
            return
        org_id = coerce_uuid(auth.organization_id)
        profile = db.scalar(
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.organization_id == org_id,
                EmployeeTaxProfile.employee_id == employee.employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
            .limit(1)
        )

        def _value(name: str) -> str | None:
            return self._clean_person_text(self._form_str(form, name))

        tin = _value("tin")
        rsa_pin = _value("rsa_pin")
        pfa_code = _value("pfa_code")
        nhf_number = _value("nhf_number")
        pension_rate = self._parse_decimal(self._form_str(form, "pension_rate"))

        if profile is None:
            profile = EmployeeTaxProfile(
                employee_id=employee.employee_id,
                organization_id=org_id,
                effective_from=datetime.now(UTC),
                tax_state=None,
                annual_rent=Decimal("0"),
            )
            db.add(profile)

        profile.tin = tin or profile.tin
        profile.rsa_pin = rsa_pin or profile.rsa_pin
        profile.pfa_code = pfa_code or profile.pfa_code
        profile.nhf_number = nhf_number or profile.nhf_number
        if pension_rate is not None:
            profile.pension_rate = pension_rate

    def list_employees_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        status: str | None = None,
        department_id: str | None = None,
        designation_id: str | None = None,
        date_of_joining_from: str | None = None,
        date_of_joining_to: str | None = None,
        date_of_leaving_from: str | None = None,
        date_of_leaving_to: str | None = None,
        filters_json: str | None = None,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
        success: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        """Render employee list page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        org_svc = OrganizationService(db, org_id)
        page_size = limit if limit in {25, 50, 100, 200} else DEFAULT_PAGE_SIZE

        # Parse status filter
        status_filter = None
        status_filters = None
        archive_only = False
        if status:
            status_value = status.strip().lower()
            if status_value in {"archive", "exit_archive"}:
                archive_only = True
            elif status_value == "inactive":
                status_filters = [
                    EmployeeStatus.SUSPENDED,
                    EmployeeStatus.RESIGNED,
                    EmployeeStatus.TERMINATED,
                    EmployeeStatus.RETIRED,
                ]
            else:
                try:
                    status_filter = EmployeeStatus(status.upper())
                except ValueError:
                    pass

        leaving_from = self._parse_date(date_of_leaving_from or "")
        leaving_to = self._parse_date(date_of_leaving_to or "")
        has_exit_date_filter = bool(leaving_from or leaving_to)
        show_exit_date = status_filter == EmployeeStatus.RESIGNED
        include_exit_history = archive_only or show_exit_date or has_exit_date_filter

        employee_filters = EmployeeFilters(
            search=search,
            status=status_filter,
            statuses=status_filters,
            include_archived=(include_exit_history or status_filters is not None),
            archive_only=archive_only,
            include_deleted=(
                include_exit_history
                or status_filters is not None
                or status_filter == EmployeeStatus.TERMINATED
            ),
            department_id=coerce_uuid(department_id) if department_id else None,
            designation_id=coerce_uuid(designation_id) if designation_id else None,
            date_of_joining_from=self._parse_date(date_of_joining_from or ""),
            date_of_joining_to=self._parse_date(date_of_joining_to or ""),
            date_of_leaving_from=leaving_from,
            date_of_leaving_to=leaving_to,
        )
        pagination = PaginationParams.from_page(page, page_size)
        try:
            advanced_expression = parse_employee_filter_payload_json(filters_json)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Use eager_load=True to avoid N+1 queries (loads person, dept, desig in bulk)
        result = svc.list_employees(
            employee_filters,
            pagination,
            eager_load=True,
            advanced_filter_expression=advanced_expression,
        )
        stats = svc.get_employee_stats()

        # Get departments for filter dropdown
        dept_result = org_svc.list_departments(
            DepartmentFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        )
        departments = dept_result.items

        desig_result = org_svc.list_designations(
            DesignationFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        )
        designations = desig_result.items

        employment_types = self._list_employee_employment_types(db, org_id)

        location_result = org_svc.list_locations(
            is_active=True,
            pagination=PaginationParams(limit=DROPDOWN_LIMIT),
        )
        locations = location_result.items

        manager_result = svc.list_employees(
            EmployeeFilters(include_deleted=False),
            PaginationParams(limit=DROPDOWN_LIMIT),
            eager_load=True,
        )
        managers = []
        for mgr in manager_result.items:
            person = mgr.person
            full_name = person.name if person else ""
            managers.append(
                {
                    "employee_id": mgr.employee_id,
                    "employee_code": mgr.employee_code,
                    "full_name": full_name,
                }
            )

        # Build employee view data - relationships already loaded via eager_load
        employees_view = []
        for emp in result.items:
            person = emp.person
            dept = emp.department
            desig = emp.designation
            status_value = emp.status.value if emp.status else "UNKNOWN"

            employees_view.append(
                {
                    "employee_id": emp.employee_id,
                    "employee_code": emp.employee_code,
                    "person_name": person.name if person else "",
                    "email": person.email if person else "",
                    "department_name": dept.department_name if dept else "",
                    "designation_name": desig.designation_name if desig else "",
                    "date_of_joining": emp.date_of_joining,
                    "date_of_leaving": emp.date_of_leaving,
                    "status": status_value,
                    "status_class": self._status_class(emp.status),
                }
            )

        # Build option lookups for active filter chips
        dept_options = {str(d.department_id): d.department_name for d in departments}
        desig_options = {
            str(d.designation_id): d.designation_name for d in designations
        }
        employment_type_options = {
            str(item.employment_type_id): item.type_name for item in employment_types
        }
        location_options = {
            str(item.location_id): item.location_name for item in locations
        }
        active_filters = build_active_filters(
            params={
                "status": status,
                "department_id": department_id,
                "designation_id": designation_id,
                "date_of_joining_from": date_of_joining_from,
                "date_of_joining_to": date_of_joining_to,
                "date_of_leaving_from": date_of_leaving_from,
                "date_of_leaving_to": date_of_leaving_to,
            },
            labels={
                "status": "Status",
                "department_id": "Department",
                "designation_id": "Designation",
                "date_of_joining_from": "Joined From",
                "date_of_joining_to": "Joined To",
                "date_of_leaving_from": "Exit From",
                "date_of_leaving_to": "Exit To",
            },
            options={
                "department_id": dept_options,
                "designation_id": desig_options,
                "employment_type_id": employment_type_options,
                "assigned_location_id": location_options,
            },
        )
        if filters_json:
            active_filters.append(
                {
                    "name": "filters",
                    "value": filters_json,
                    "display_value": "Advanced: Custom rules",
                }
            )
        context = {
            **base_context(request, auth, "Employees", "employees", db=db),
            "employees": employees_view,
            "stats": stats,
            "departments": departments,
            "designations": designations,
            "employment_types": employment_types,
            "locations": locations,
            "managers": managers,
            "search": search or "",
            "status": status or "",
            "department_id": department_id or "",
            "designation_id": designation_id or "",
            "date_of_joining_from": date_of_joining_from or "",
            "date_of_joining_to": date_of_joining_to or "",
            "date_of_leaving_from": date_of_leaving_from or "",
            "date_of_leaving_to": date_of_leaving_to or "",
            "show_exit_date": show_exit_date,
            "filters_json": filters_json or "",
            "page": page,
            "total_pages": result.total_pages,
            "total_count": result.total,
            "total": result.total,
            "limit": pagination.limit,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
            "success": success,
            "error": error,
            "active_filters": active_filters,
            "employee_export_fields": [
                {"key": key, "label": label}
                for key, (label, _) in self.EMPLOYEE_EXPORT_FIELDS.items()
            ],
            "default_employee_export_fields": self.DEFAULT_EMPLOYEE_EXPORT_FIELDS,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/employees.html",
            context,
        )

    @staticmethod
    def _csv_safe_cell(value: object) -> str:
        """Prevent spreadsheet applications from evaluating exported values."""
        if value is None:
            return ""
        text = str(value)
        return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text

    def export_employees_csv_response(
        self,
        *,
        auth: WebAuthContext,
        db: Session,
        fields: list[str],
        status: str | None = None,
        department_id: str | None = None,
        designation_id: str | None = None,
        date_of_joining_from: str | None = None,
        date_of_joining_to: str | None = None,
        include_archived: bool = False,
    ) -> Response:
        """Build a CSV from explicitly selected, allowlisted employee fields."""
        selected_fields = [
            field for field in fields if field in self.EMPLOYEE_EXPORT_FIELDS
        ]
        if not selected_fields:
            raise HTTPException(
                status_code=422, detail="Select at least one export field"
            )

        status_filter = None
        status_filters = None
        if status:
            if status.strip().lower() == "inactive":
                status_filters = [
                    EmployeeStatus.SUSPENDED,
                    EmployeeStatus.RESIGNED,
                    EmployeeStatus.TERMINATED,
                    EmployeeStatus.RETIRED,
                ]
            else:
                try:
                    status_filter = EmployeeStatus(status.upper())
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail="Invalid employee status"
                    ) from exc

        org_id = coerce_uuid(auth.organization_id)
        filters = EmployeeFilters(
            status=status_filter,
            statuses=status_filters,
            include_archived=(
                include_archived
                or status_filters is not None
                or status_filter == EmployeeStatus.RESIGNED
            ),
            include_deleted=(
                include_archived
                or status_filters is not None
                or status_filter == EmployeeStatus.TERMINATED
            ),
            department_id=coerce_uuid(department_id) if department_id else None,
            designation_id=coerce_uuid(designation_id) if designation_id else None,
            date_of_joining_from=self._parse_date(date_of_joining_from or ""),
            date_of_joining_to=self._parse_date(date_of_joining_to or ""),
        )
        employees = (
            EmployeeService(db, org_id)
            .list_employees(
                filters,
                PaginationParams(limit=100_000),
                eager_load=True,
            )
            .items
        )
        employment_type_names = (
            {
                row.employment_type_id: row.type_name
                for row in EmploymentTypeService(db, org_id).iter_all(active=None)
            }
            if "employment_type" in selected_fields
            else {}
        )

        def _field_value(field: str, employee: Employee) -> object:
            _, extractor = self.EMPLOYEE_EXPORT_FIELDS[field]
            if extractor is not None:
                return extractor(employee)
            if field != "employment_type":
                raise RuntimeError(f"missing employee export extractor: {field}")
            if employee.employment_type_id is None:
                return ""
            return employment_type_names.get(employee.employment_type_id, "")

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [self.EMPLOYEE_EXPORT_FIELDS[field][0] for field in selected_fields]
        )
        for employee in employees:
            writer.writerow(
                [
                    self._csv_safe_cell(_field_value(field, employee))
                    for field in selected_fields
                ]
            )

        filename = f"employees_{date.today():%Y%m%d}.csv"
        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def employee_stats_response(
        self,
        auth: WebAuthContext,
        db: Session,
    ) -> dict:
        """Return employee stats for dashboard widgets."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        return dict(svc.get_employee_stats())

    async def create_employee_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        background_tasks: BackgroundTasks | None = None,
    ) -> RedirectResponse | HTMLResponse:
        """Handle new employee form submission."""
        form = await self._request_form(request)

        # Person fields
        first_name = self._form_str(form, "first_name")
        last_name = self._form_str(form, "last_name")
        email = self._form_str(form, "email")
        phone = self._form_str(form, "phone")
        date_of_birth = self._form_str(form, "date_of_birth")
        gender = self._form_str(form, "gender")
        address_line1 = self._form_str(form, "address_line1")
        address_line2 = self._form_str(form, "address_line2")
        city = self._form_str(form, "city")
        region = self._form_str(form, "region")
        postal_code = self._form_str(form, "postal_code")
        country_code = self._form_str(form, "country_code")
        # Employee fields
        employee_code = self._form_str(form, "employee_code")
        department_id = self._form_str(form, "department_id")
        designation_id = self._form_str(form, "designation_id")
        employment_type_id = self._form_str(form, "employment_type_id")
        grade_id = self._form_str(form, "grade_id")
        position_id = self._form_str(form, "position_id")
        reports_to_id = self._form_str(form, "reports_to_id")
        expense_approver_id = self._form_str(form, "expense_approver_id")
        assigned_location_id = self._form_str(form, "assigned_location_id")
        default_shift_type_id = self._form_str(form, "default_shift_type_id")
        linked_person_id = self._form_str(form, "linked_person_id")
        cost_center_id = self._form_str(form, "cost_center_id")
        current_tab = self._form_str(form, "current_tab")
        date_of_joining = self._form_str(form, "date_of_joining")
        probation_end_date = self._form_str(form, "probation_end_date")
        confirmation_date = self._form_str(form, "confirmation_date")
        nysc_start_date = self._form_str(form, "nysc_start_date")
        nysc_end_date = self._form_str(form, "nysc_end_date")
        notes = self._form_str(form, "notes")
        status = self._form_str(form, "status") or "DRAFT"
        dotmac_sub_access_enabled = "dotmac_sub_access_enabled" in form
        dotmac_sub_roles = self._parse_role_names(
            self._form_str(form, "dotmac_sub_roles")
        )
        # Personal contact & emergency
        personal_email = self._clean_optional_text(
            self._form_str(form, "personal_email")
        )
        personal_phone = self._clean_optional_text(
            self._form_str(form, "personal_phone")
        )
        emergency_contact_name = self._clean_optional_text(
            self._form_str(form, "emergency_contact_name")
        )
        emergency_contact_phone = self._clean_optional_text(
            self._form_str(form, "emergency_contact_phone")
        )
        # Bank details
        bank_name = self._clean_optional_text(self._form_str(form, "bank_name"))
        bank_account_name = self._clean_optional_text(
            self._form_str(form, "bank_account_name")
        )
        bank_account_number = self._clean_optional_text(
            self._form_str(form, "bank_account_number")
        )
        bank_branch_code = self._clean_optional_text(
            self._form_str(form, "bank_branch_code")
        )
        ctc_raw = self._form_str(form, "ctc")
        salary_mode_raw = self._form_str(form, "salary_mode")
        ctc = self._parse_decimal(ctc_raw)
        salary_mode = self._parse_salary_mode(salary_mode_raw)
        salary_structure_id = self._form_str(form, "salary_structure_id")

        tin = self._form_str(form, "tin")
        tax_state = self._form_str(form, "tax_state")
        rsa_pin = self._form_str(form, "rsa_pin")
        pfa_code = self._form_str(form, "pfa_code")
        pension_rate_raw = self._form_str(form, "pension_rate")
        nhf_number = self._form_str(form, "nhf_number")

        required_employment_errors = {}
        if not employment_type_id:
            required_employment_errors["employment_type_id"] = "Required"
        if not salary_mode_raw:
            required_employment_errors["salary_mode"] = "Required"
        elif not salary_mode:
            required_employment_errors["salary_mode"] = "Select a valid salary mode."

        if (
            not linked_person_id and (not first_name or not last_name or not email)
        ) or not date_of_joining:
            self._log_employee_create_validation(
                request,
                reason="missing_required_identity_or_joining_date",
                current_tab=current_tab,
                has_salary_structure=bool(salary_structure_id),
            )
            errors = {
                "first_name": "Required" if not first_name else "",
                "last_name": "Required" if not last_name else "",
                "email": "Required" if not email else "",
                "date_of_joining": "Required" if not date_of_joining else "",
            }
            return self.employee_new_form_response(
                request,
                auth,
                db,
                error="First name, last name, email, and date of joining are required.",
                form_data={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "date_of_birth": date_of_birth,
                    "gender": gender,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country_code": country_code,
                    "employee_code": employee_code,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "grade_id": grade_id,
                    "position_id": position_id,
                    "reports_to_id": reports_to_id,
                    "expense_approver_id": expense_approver_id,
                    "assigned_location_id": assigned_location_id,
                    "default_shift_type_id": default_shift_type_id,
                    "linked_person_id": linked_person_id,
                    "cost_center_id": cost_center_id,
                    "current_tab": current_tab,
                    "date_of_joining": date_of_joining,
                    "probation_end_date": probation_end_date,
                    "confirmation_date": confirmation_date,
                    "nysc_start_date": nysc_start_date,
                    "nysc_end_date": nysc_end_date,
                    "status": status,
                    "bank_name": bank_name,
                    "bank_account_name": bank_account_name,
                    "bank_account_number": bank_account_number,
                    "bank_branch_code": bank_branch_code,
                    "ctc": ctc_raw,
                    "salary_mode": salary_mode_raw,
                    "salary_structure_id": salary_structure_id,
                    "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                    "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                    "notes": notes,
                    "tin": tin,
                    "tax_state": tax_state,
                    "rsa_pin": rsa_pin,
                    "pfa_code": pfa_code,
                    "pension_rate": pension_rate_raw,
                    "nhf_number": nhf_number,
                },
                errors=errors,
            )

        if required_employment_errors:
            self._log_employee_create_validation(
                request,
                reason="missing_required_contract_type_or_salary_mode",
                current_tab=current_tab,
                has_salary_structure=bool(salary_structure_id),
            )
            return self.employee_new_form_response(
                request,
                auth,
                db,
                error=(
                    "Contract type and salary mode must be selected for "
                    "employee creation."
                ),
                form_data={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "date_of_birth": date_of_birth,
                    "gender": gender,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country_code": country_code,
                    "employee_code": employee_code,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "grade_id": grade_id,
                    "position_id": position_id,
                    "reports_to_id": reports_to_id,
                    "expense_approver_id": expense_approver_id,
                    "assigned_location_id": assigned_location_id,
                    "default_shift_type_id": default_shift_type_id,
                    "linked_person_id": linked_person_id,
                    "cost_center_id": cost_center_id,
                    "current_tab": "employment",
                    "date_of_joining": date_of_joining,
                    "probation_end_date": probation_end_date,
                    "confirmation_date": confirmation_date,
                    "nysc_start_date": nysc_start_date,
                    "nysc_end_date": nysc_end_date,
                    "status": status,
                    "personal_email": personal_email,
                    "personal_phone": personal_phone,
                    "emergency_contact_name": emergency_contact_name,
                    "emergency_contact_phone": emergency_contact_phone,
                    "bank_name": bank_name,
                    "bank_account_name": bank_account_name,
                    "bank_account_number": bank_account_number,
                    "bank_branch_code": bank_branch_code,
                    "ctc": ctc_raw,
                    "salary_mode": salary_mode_raw,
                    "salary_structure_id": salary_structure_id,
                    "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                    "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                    "notes": notes,
                    "tin": tin,
                    "tax_state": tax_state,
                    "rsa_pin": rsa_pin,
                    "pfa_code": pfa_code,
                    "pension_rate": pension_rate_raw,
                    "nhf_number": nhf_number,
                },
                errors=required_employment_errors,
            )

        org_id = coerce_uuid(auth.organization_id)
        normalized_country_code = country_code.upper() if country_code else ""
        if normalized_country_code and len(normalized_country_code) != 2:
            self._log_employee_create_validation(
                request,
                reason="invalid_country_code",
                current_tab=current_tab,
                has_salary_structure=bool(salary_structure_id),
            )
            return self.employee_new_form_response(
                request,
                auth,
                db,
                error=(
                    "Country Code must be a 2-letter code like NI, "
                    "not the country name."
                ),
                form_data={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "date_of_birth": date_of_birth,
                    "gender": gender,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country_code": country_code,
                    "employee_code": employee_code,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "grade_id": grade_id,
                    "position_id": position_id,
                    "reports_to_id": reports_to_id,
                    "expense_approver_id": expense_approver_id,
                    "assigned_location_id": assigned_location_id,
                    "default_shift_type_id": default_shift_type_id,
                    "linked_person_id": linked_person_id,
                    "cost_center_id": cost_center_id,
                    "current_tab": "personal",
                    "date_of_joining": date_of_joining,
                    "probation_end_date": probation_end_date,
                    "confirmation_date": confirmation_date,
                    "nysc_start_date": nysc_start_date,
                    "nysc_end_date": nysc_end_date,
                    "status": status,
                    "personal_email": personal_email,
                    "personal_phone": personal_phone,
                    "emergency_contact_name": emergency_contact_name,
                    "emergency_contact_phone": emergency_contact_phone,
                    "bank_name": bank_name,
                    "bank_account_name": bank_account_name,
                    "bank_account_number": bank_account_number,
                    "bank_branch_code": bank_branch_code,
                    "ctc": ctc_raw,
                    "salary_mode": salary_mode_raw,
                    "salary_structure_id": salary_structure_id,
                    "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                    "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                    "notes": notes,
                    "tin": tin,
                    "tax_state": tax_state,
                    "rsa_pin": rsa_pin,
                    "pfa_code": pfa_code,
                    "pension_rate": pension_rate_raw,
                    "nhf_number": nhf_number,
                },
                errors={
                    "country_code": (
                        "Use a 2-letter country code like NI, not the country name."
                    )
                },
            )

        nysc_errors, nysc_start_value, nysc_end_value = self._validate_nysc_dates(
            db=db,
            organization_id=org_id,
            designation_id=designation_id,
            nysc_start_date=nysc_start_date,
            nysc_end_date=nysc_end_date,
        )
        if nysc_errors:
            self._log_employee_create_validation(
                request,
                reason="missing_required_nysc_dates",
                current_tab=current_tab,
                has_salary_structure=bool(salary_structure_id),
            )
            return self.employee_new_form_response(
                request,
                auth,
                db,
                error="NYSC start and end dates are required for NYSC designations.",
                form_data={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "date_of_birth": date_of_birth,
                    "gender": gender,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country_code": country_code,
                    "employee_code": employee_code,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "grade_id": grade_id,
                    "position_id": position_id,
                    "reports_to_id": reports_to_id,
                    "expense_approver_id": expense_approver_id,
                    "assigned_location_id": assigned_location_id,
                    "default_shift_type_id": default_shift_type_id,
                    "linked_person_id": linked_person_id,
                    "cost_center_id": cost_center_id,
                    "date_of_joining": date_of_joining,
                    "probation_end_date": probation_end_date,
                    "confirmation_date": confirmation_date,
                    "nysc_start_date": nysc_start_date,
                    "nysc_end_date": nysc_end_date,
                    "status": status,
                    "personal_email": personal_email,
                    "personal_phone": personal_phone,
                    "emergency_contact_name": emergency_contact_name,
                    "emergency_contact_phone": emergency_contact_phone,
                    "bank_name": bank_name,
                    "bank_account_name": bank_account_name,
                    "bank_account_number": bank_account_number,
                    "bank_branch_code": bank_branch_code,
                    "ctc": ctc_raw,
                    "salary_mode": salary_mode_raw,
                    "salary_structure_id": salary_structure_id,
                    "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                    "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                    "notes": notes,
                    "tin": tin,
                    "tax_state": tax_state,
                    "rsa_pin": rsa_pin,
                    "pfa_code": pfa_code,
                    "pension_rate": pension_rate_raw,
                    "nhf_number": nhf_number,
                },
                errors=nysc_errors,
            )

        selected_salary_structure: SalaryStructure | None = None
        if not salary_structure_id:
            self._log_employee_create_validation(
                request,
                reason="missing_salary_structure",
                current_tab=current_tab,
                has_salary_structure=False,
            )
            return self.employee_new_form_response(
                request,
                auth,
                db,
                error="Salary structure is required for employee creation.",
                form_data={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "date_of_birth": date_of_birth,
                    "gender": gender,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "city": city,
                    "region": region,
                    "postal_code": postal_code,
                    "country_code": country_code,
                    "employee_code": employee_code,
                    "department_id": department_id,
                    "designation_id": designation_id,
                    "employment_type_id": employment_type_id,
                    "grade_id": grade_id,
                    "position_id": position_id,
                    "reports_to_id": reports_to_id,
                    "expense_approver_id": expense_approver_id,
                    "assigned_location_id": assigned_location_id,
                    "default_shift_type_id": default_shift_type_id,
                    "linked_person_id": linked_person_id,
                    "cost_center_id": cost_center_id,
                    "current_tab": "employment",
                    "date_of_joining": date_of_joining,
                    "probation_end_date": probation_end_date,
                    "confirmation_date": confirmation_date,
                    "nysc_start_date": nysc_start_date,
                    "nysc_end_date": nysc_end_date,
                    "status": status,
                    "personal_email": personal_email,
                    "personal_phone": personal_phone,
                    "emergency_contact_name": emergency_contact_name,
                    "emergency_contact_phone": emergency_contact_phone,
                    "bank_name": bank_name,
                    "bank_account_name": bank_account_name,
                    "bank_account_number": bank_account_number,
                    "bank_branch_code": bank_branch_code,
                    "ctc": ctc_raw,
                    "salary_mode": salary_mode_raw,
                    "salary_structure_id": salary_structure_id,
                    "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                    "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                    "notes": notes,
                    "tin": tin,
                    "tax_state": tax_state,
                    "rsa_pin": rsa_pin,
                    "pfa_code": pfa_code,
                    "pension_rate": pension_rate_raw,
                    "nhf_number": nhf_number,
                },
            )

        if salary_structure_id:
            try:
                structure_uuid = coerce_uuid(salary_structure_id, raise_http=False)
            except (TypeError, ValueError):
                structure_uuid = None

            if structure_uuid:
                selected_salary_structure = db.scalar(
                    select(SalaryStructure).where(
                        SalaryStructure.organization_id == org_id,
                        SalaryStructure.structure_id == structure_uuid,
                        SalaryStructure.is_active.is_(True),
                    )
                )

            if not selected_salary_structure:
                self._log_employee_create_validation(
                    request,
                    reason="invalid_salary_structure",
                    current_tab=current_tab,
                    has_salary_structure=True,
                )
                return self.employee_new_form_response(
                    request,
                    auth,
                    db,
                    error="Select a valid active salary structure for this organization.",
                    form_data={
                        "first_name": first_name,
                        "last_name": last_name,
                        "email": email,
                        "phone": phone,
                        "date_of_birth": date_of_birth,
                        "gender": gender,
                        "address_line1": address_line1,
                        "address_line2": address_line2,
                        "city": city,
                        "region": region,
                        "postal_code": postal_code,
                        "country_code": country_code,
                        "employee_code": employee_code,
                        "department_id": department_id,
                        "designation_id": designation_id,
                        "employment_type_id": employment_type_id,
                        "grade_id": grade_id,
                        "position_id": position_id,
                        "reports_to_id": reports_to_id,
                        "expense_approver_id": expense_approver_id,
                        "assigned_location_id": assigned_location_id,
                        "default_shift_type_id": default_shift_type_id,
                        "linked_person_id": linked_person_id,
                        "cost_center_id": cost_center_id,
                        "current_tab": "employment",
                        "date_of_joining": date_of_joining,
                        "probation_end_date": probation_end_date,
                        "confirmation_date": confirmation_date,
                        "nysc_start_date": nysc_start_date,
                        "nysc_end_date": nysc_end_date,
                        "status": status,
                        "personal_email": personal_email,
                        "personal_phone": personal_phone,
                        "emergency_contact_name": emergency_contact_name,
                        "emergency_contact_phone": emergency_contact_phone,
                        "bank_name": bank_name,
                        "bank_account_name": bank_account_name,
                        "bank_account_number": bank_account_number,
                        "bank_branch_code": bank_branch_code,
                        "ctc": ctc_raw,
                        "salary_mode": salary_mode_raw,
                        "salary_structure_id": salary_structure_id,
                        "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                        "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                        "notes": notes,
                        "tin": tin,
                        "tax_state": tax_state,
                        "rsa_pin": rsa_pin,
                        "pfa_code": pfa_code,
                        "pension_rate": pension_rate_raw,
                        "nhf_number": nhf_number,
                    },
                )

        joining_date = self._parse_date(date_of_joining)
        dob = self._parse_date(date_of_birth)
        probation_date = self._parse_date(probation_end_date)
        confirm_date = self._parse_date(confirmation_date)

        # Parse status
        status_enum = EmployeeStatus.DRAFT
        if status:
            try:
                status_enum = EmployeeStatus(status.upper())
            except ValueError:
                pass

        person: Person | None = None

        # Check if person with this email already exists
        existing_person = db.scalar(
            select(Person).where(
                Person.email == email,
                Person.organization_id == org_id,
            )
        )

        if existing_person:
            # Check if they already have an employee record
            svc = EmployeeService(db, org_id)
            existing_emp = svc.get_employee_by_person(existing_person.id)
            if existing_emp:
                self._log_employee_create_validation(
                    request,
                    reason="person_already_has_employee",
                    current_tab=current_tab,
                    has_salary_structure=bool(salary_structure_id),
                )
                return self.employee_new_form_response(
                    request,
                    auth,
                    db,
                    error=f"A person with email '{email}' already has an employee record.",
                    form_data={
                        "first_name": first_name,
                        "last_name": last_name,
                        "email": email,
                        "employee_code": employee_code,
                        "department_id": department_id,
                        "designation_id": designation_id,
                        "assigned_location_id": assigned_location_id,
                        "default_shift_type_id": default_shift_type_id,
                        "expense_approver_id": expense_approver_id,
                        "linked_person_id": linked_person_id,
                        "date_of_joining": date_of_joining,
                        "nysc_start_date": nysc_start_date,
                        "nysc_end_date": nysc_end_date,
                        "status": status,
                        "bank_name": bank_name,
                        "bank_account_name": bank_account_name,
                        "bank_account_number": bank_account_number,
                        "bank_branch_code": bank_branch_code,
                        "ctc": ctc_raw,
                        "salary_mode": salary_mode_raw,
                        "salary_structure_id": salary_structure_id,
                        "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                        "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                        "tin": tin,
                        "tax_state": tax_state,
                        "rsa_pin": rsa_pin,
                        "pfa_code": pfa_code,
                        "pension_rate": pension_rate_raw,
                        "nhf_number": nhf_number,
                    },
                )
            person = existing_person
        else:
            if linked_person_id:
                person = db.get(Person, coerce_uuid(linked_person_id))
                if not person or person.organization_id != org_id:
                    self._log_employee_create_validation(
                        request,
                        reason="linked_person_not_found",
                        current_tab=current_tab,
                        has_salary_structure=bool(salary_structure_id),
                    )
                    return self.employee_new_form_response(
                        request,
                        auth,
                        db,
                        error="Selected user account not found for this organization.",
                        form_data={
                            "first_name": first_name,
                            "last_name": last_name,
                            "email": email,
                            "phone": phone,
                            "date_of_birth": date_of_birth,
                            "gender": gender,
                            "address_line1": address_line1,
                            "address_line2": address_line2,
                            "city": city,
                            "region": region,
                            "postal_code": postal_code,
                            "country_code": country_code,
                            "employee_code": employee_code,
                            "department_id": department_id,
                            "designation_id": designation_id,
                            "employment_type_id": employment_type_id,
                            "grade_id": grade_id,
                            "position_id": position_id,
                            "reports_to_id": reports_to_id,
                            "expense_approver_id": expense_approver_id,
                            "assigned_location_id": assigned_location_id,
                            "default_shift_type_id": default_shift_type_id,
                            "linked_person_id": linked_person_id,
                            "cost_center_id": cost_center_id,
                            "date_of_joining": date_of_joining,
                            "probation_end_date": probation_end_date,
                            "confirmation_date": confirmation_date,
                            "nysc_start_date": nysc_start_date,
                            "nysc_end_date": nysc_end_date,
                            "status": status,
                            "bank_name": bank_name,
                            "bank_account_name": bank_account_name,
                            "bank_account_number": bank_account_number,
                            "bank_branch_code": bank_branch_code,
                            "ctc": ctc_raw,
                            "salary_mode": salary_mode_raw,
                            "salary_structure_id": salary_structure_id,
                            "dotmac_sub_access_enabled": dotmac_sub_access_enabled,
                            "dotmac_sub_roles": ", ".join(dotmac_sub_roles),
                            "notes": notes,
                            "tin": tin,
                            "tax_state": tax_state,
                            "rsa_pin": rsa_pin,
                            "pfa_code": pfa_code,
                            "pension_rate": pension_rate_raw,
                            "nhf_number": nhf_number,
                        },
                    )
            else:
                # Create new Person
                person = Person(
                    organization_id=org_id,
                    first_name=first_name,
                    last_name=last_name,
                    email=email.lower(),
                    phone=phone or None,
                    date_of_birth=dob,
                    gender=Gender(gender) if gender else Gender.unknown,
                    address_line1=address_line1 or None,
                    address_line2=address_line2 or None,
                    city=city or None,
                    region=region or None,
                    postal_code=postal_code or None,
                    country_code=normalized_country_code or None,
                )
                db.add(person)
                db.flush()

        # Create Employee linked to Person
        svc = EmployeeService(db, org_id)
        data = EmployeeCreateData(
            employee_number=employee_code if employee_code else None,
            department_id=coerce_uuid(department_id) if department_id else None,
            designation_id=coerce_uuid(designation_id) if designation_id else None,
            employment_type_id=coerce_uuid(employment_type_id)
            if employment_type_id
            else None,
            grade_id=coerce_uuid(grade_id) if grade_id else None,
            position_id=coerce_uuid(position_id) if position_id else None,
            reports_to_id=coerce_uuid(reports_to_id) if reports_to_id else None,
            expense_approver_id=coerce_uuid(expense_approver_id)
            if expense_approver_id
            else None,
            assigned_location_id=coerce_uuid(assigned_location_id)
            if assigned_location_id
            else None,
            default_shift_type_id=coerce_uuid(default_shift_type_id)
            if default_shift_type_id
            else None,
            cost_center_id=coerce_uuid(cost_center_id) if cost_center_id else None,
            date_of_joining=joining_date,
            probation_end_date=probation_date,
            confirmation_date=confirm_date,
            nysc_start_date=nysc_start_value,
            nysc_end_date=nysc_end_value,
            status=status_enum,
            personal_email=personal_email,
            personal_phone=personal_phone,
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
            bank_name=bank_name,
            bank_account_name=bank_account_name,
            bank_account_number=bank_account_number,
            bank_sort_code=bank_branch_code,
            ctc=ctc,
            salary_mode=salary_mode,
            notes=notes or None,
            dotmac_sub_access_enabled=dotmac_sub_access_enabled,
            dotmac_sub_roles=dotmac_sub_roles,
        )

        if person is None:
            raise HTTPException(status_code=400, detail="Person not found")
        employee = svc.create_employee(person.id, data)
        self._update_tax_profile(auth=auth, db=db, employee=employee, form=form)
        if selected_salary_structure:
            self._create_initial_salary_assignment(
                db=db,
                organization_id=org_id,
                employee=employee,
                salary_structure=selected_salary_structure,
                base=ctc,
            )
        employee_id = employee.employee_id
        app_url = self._resolve_app_url(request)
        db.commit()
        invite_status = "sent"
        invite_recipient_kind = ""
        invite_recipient_email = ""
        try:
            invite_result = svc.send_employee_access_invite(
                employee_id,
                app_url=app_url,
            )
            if not invite_result:
                invite_status = "failed"
                logger.warning(
                    "Employee access invite was not sent for %s", employee_id
                )
            else:
                invite_recipient_kind = getattr(invite_result, "recipient_kind", "")
                invite_recipient_email = getattr(invite_result, "recipient_email", "")
        except ServiceError:
            invite_status = "failed"
            logger.exception("Employee access invite failed for %s", employee_id)

        query = urlencode(
            {
                key: value
                for key, value in {
                    "saved": "1",
                    "invite_status": invite_status,
                    "invite_recipient_kind": invite_recipient_kind,
                    "invite_recipient_email": invite_recipient_email,
                }.items()
                if value
            }
        )
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?{query}",
            status_code=303,
        )

    async def update_employee_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Handle employee update form submission."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        employee = svc.get_employee(coerce_uuid(employee_id))

        form = await self._request_form(request)

        employee_code = self._form_str(form, "employee_code")
        department_id = self._form_str(form, "department_id")
        designation_id = self._form_str(form, "designation_id")
        employment_type_id = self._form_str(form, "employment_type_id")
        grade_id = self._form_str(form, "grade_id")
        position_id = self._form_str(form, "position_id")
        reports_to_id = self._form_str(form, "reports_to_id")
        expense_approver_id = self._form_str(form, "expense_approver_id")
        assigned_location_id = self._form_str(form, "assigned_location_id")
        default_shift_type_id = self._form_str(form, "default_shift_type_id")
        linked_person_id = self._form_str(form, "linked_person_id")
        cost_center_id = self._form_str(form, "cost_center_id")
        current_tab = self._form_str(form, "current_tab")
        date_of_joining = self._form_str(form, "date_of_joining")
        probation_end_date = self._form_str(form, "probation_end_date")
        confirmation_date = self._form_str(form, "confirmation_date")
        nysc_start_date = self._form_str(form, "nysc_start_date")
        nysc_end_date = self._form_str(form, "nysc_end_date")
        notes = self._form_str(form, "notes")
        status = self._form_str(form, "status")
        dotmac_sub_access_submitted = "dotmac_sub_access_present" in form
        dotmac_sub_access_enabled = (
            "dotmac_sub_access_enabled" in form if dotmac_sub_access_submitted else None
        )
        dotmac_sub_roles = (
            self._parse_role_names(self._form_str(form, "dotmac_sub_roles"))
            if dotmac_sub_access_submitted
            else None
        )
        # Personal contact & emergency
        personal_email = self._clean_optional_text(
            self._form_str(form, "personal_email")
        )
        personal_phone = self._clean_optional_text(
            self._form_str(form, "personal_phone")
        )
        emergency_contact_name = self._clean_optional_text(
            self._form_str(form, "emergency_contact_name")
        )
        emergency_contact_phone = self._clean_optional_text(
            self._form_str(form, "emergency_contact_phone")
        )
        # Bank details
        bank_name = self._clean_optional_text(self._form_str(form, "bank_name"))
        bank_account_name = self._clean_optional_text(
            self._form_str(form, "bank_account_name")
        )
        bank_account_number = self._clean_optional_text(
            self._form_str(form, "bank_account_number")
        )
        bank_branch_code = self._clean_optional_text(
            self._form_str(form, "bank_branch_code")
        )
        ctc_raw = self._form_str(form, "ctc")
        salary_mode_raw = self._form_str(form, "salary_mode")
        ctc = self._parse_decimal(ctc_raw)
        salary_mode = self._parse_salary_mode(salary_mode_raw)

        status_enum = None
        if status:
            try:
                status_enum = EmployeeStatus(status.upper())
            except ValueError:
                pass
        if status_enum == EmployeeStatus.TERMINATED:
            status_enum = None

        joining_date = self._parse_date(date_of_joining)
        probation_date = self._parse_date(probation_end_date)
        confirm_date = self._parse_date(confirmation_date)
        nysc_errors, nysc_start_value, nysc_end_value = self._validate_nysc_dates(
            db=db,
            organization_id=org_id,
            designation_id=designation_id,
            nysc_start_date=nysc_start_date,
            nysc_end_date=nysc_end_date,
        )
        if nysc_errors:
            form_data_payload = {
                "employee_code": employee_code,
                "department_id": department_id,
                "designation_id": designation_id,
                "employment_type_id": employment_type_id,
                "grade_id": grade_id,
                "position_id": position_id,
                "reports_to_id": reports_to_id,
                "expense_approver_id": expense_approver_id,
                "assigned_location_id": assigned_location_id,
                "default_shift_type_id": default_shift_type_id,
                "linked_person_id": linked_person_id,
                "cost_center_id": cost_center_id,
                "current_tab": current_tab,
                "date_of_joining": date_of_joining,
                "probation_end_date": probation_end_date,
                "confirmation_date": confirmation_date,
                "nysc_start_date": nysc_start_date,
                "nysc_end_date": nysc_end_date,
                "notes": notes,
                "status": status,
                "personal_email": personal_email,
                "personal_phone": personal_phone,
                "emergency_contact_name": emergency_contact_name,
                "emergency_contact_phone": emergency_contact_phone,
                "bank_name": bank_name,
                "bank_account_name": bank_account_name,
                "bank_account_number": bank_account_number,
                "bank_branch_code": bank_branch_code,
                "ctc": ctc_raw,
                "salary_mode": salary_mode_raw,
                "dotmac_sub_access_enabled": (
                    dotmac_sub_access_enabled
                    if dotmac_sub_access_enabled is not None
                    else bool(getattr(employee, "dotmac_sub_access_enabled", False))
                ),
                "dotmac_sub_roles": ", ".join(
                    dotmac_sub_roles
                    or getattr(employee, "dotmac_sub_roles", None)
                    or [getattr(settings, "dotmac_sub_staff_default_role", "staff")]
                ),
            }

            # Preserve linked Person + statutory inputs when present in the submitted form.
            for key in (
                "first_name",
                "last_name",
                "email",
                "phone",
                "date_of_birth",
                "gender",
                "address_line1",
                "address_line2",
                "city",
                "region",
                "postal_code",
                "country_code",
                "tin",
                "tax_state",
                "rsa_pin",
                "pfa_code",
                "pension_rate",
                "nhf_number",
            ):
                if key in form:
                    form_data_payload[key] = self._form_str(form, key)

            return self.employee_edit_form_response(
                request,
                auth,
                db,
                str(employee_id),
                error="NYSC start and end dates are required for NYSC designations.",
                form_data=form_data_payload,
                errors=nysc_errors,
            )

        logger.info(
            "Employee edit submit received",
            extra={
                "employee_id": str(employee_id),
                "assigned_location_id": assigned_location_id or None,
                "current_tab": current_tab or None,
            },
        )

        provided_fields = {
            "employee_number",
            "department_id",
            "designation_id",
            "grade_id",
            "expense_approver_id",
            "cost_center_id",
            "assigned_location_id",
            "default_shift_type_id",
            "date_of_joining",
            "probation_end_date",
            "confirmation_date",
            "nysc_start_date",
            "nysc_end_date",
            "status",
            "personal_email",
            "personal_phone",
            "emergency_contact_name",
            "emergency_contact_phone",
            "bank_name",
            "bank_account_name",
            "bank_account_number",
            "bank_sort_code",
            "ctc",
            "salary_mode",
            "notes",
        }
        if dotmac_sub_access_submitted:
            provided_fields.update({"dotmac_sub_access_enabled", "dotmac_sub_roles"})
        if "reports_to_id" in form:
            provided_fields.add("reports_to_id")
        if "employment_type_id" in form:
            provided_fields.add("employment_type_id")

        data = EmployeeUpdateData(
            employee_number=employee_code if employee_code else None,
            department_id=coerce_uuid(department_id) if department_id else None,
            designation_id=coerce_uuid(designation_id) if designation_id else None,
            employment_type_id=coerce_uuid(employment_type_id)
            if employment_type_id
            else None,
            grade_id=coerce_uuid(grade_id) if grade_id else None,
            reports_to_id=coerce_uuid(reports_to_id) if reports_to_id else None,
            expense_approver_id=coerce_uuid(expense_approver_id)
            if expense_approver_id
            else None,
            assigned_location_id=coerce_uuid(assigned_location_id)
            if assigned_location_id
            else None,
            default_shift_type_id=coerce_uuid(default_shift_type_id)
            if default_shift_type_id
            else None,
            cost_center_id=coerce_uuid(cost_center_id) if cost_center_id else None,
            date_of_joining=joining_date,
            probation_end_date=probation_date,
            confirmation_date=confirm_date,
            nysc_start_date=nysc_start_value,
            nysc_end_date=nysc_end_value,
            status=status_enum,
            personal_email=personal_email,
            personal_phone=personal_phone,
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
            bank_name=bank_name,
            bank_account_name=bank_account_name,
            bank_account_number=bank_account_number,
            bank_sort_code=bank_branch_code,
            ctc=ctc,
            salary_mode=salary_mode,
            notes=notes or None,
            dotmac_sub_access_enabled=dotmac_sub_access_enabled,
            dotmac_sub_roles=dotmac_sub_roles,
            provided_fields=provided_fields,
        )

        if linked_person_id:
            svc.link_employee_to_person(
                coerce_uuid(employee_id),
                coerce_uuid(linked_person_id),
            )
            employee = svc.get_employee(coerce_uuid(employee_id))

        self._update_linked_person(auth=auth, db=db, employee=employee, form=form)

        prior_status = getattr(employee, "status", None)
        updated_employee = svc.update_employee(coerce_uuid(employee_id), data)
        self._update_tax_profile(auth=auth, db=db, employee=employee, form=form)
        assigned_location_log = (
            str(getattr(updated_employee, "assigned_location_id", None))
            if getattr(updated_employee, "assigned_location_id", None)
            else None
        )
        db.commit()
        if getattr(
            updated_employee, "status", None
        ) != prior_status and should_offboard_status(
            getattr(updated_employee, "status", None)
        ):
            queue_employee_mailcow_offboarding(
                updated_employee.employee_id,
                updated_employee.organization_id,
                updated_employee.status,
            )

        logger.info(
            "Employee edit persisted",
            extra={
                "employee_id": str(employee_id),
                "assigned_location_id": assigned_location_log,
            },
        )

        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}/edit?success=Saved%20successfully.",
            status_code=303,
        )

    def activate_employee_response(
        self,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Activate an employee."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        svc.activate_employee(employee_id)
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
        )

    def resend_employee_invite_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Resend the employee access invite and report the delivery attempt."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        app_url = self._resolve_app_url(request)
        invite_status = "sent"
        invite_recipient_kind = ""
        invite_recipient_email = ""
        try:
            invite_result = svc.send_employee_access_invite(
                employee_id, app_url=app_url
            )
            if not invite_result:
                invite_status = "failed"
            else:
                invite_recipient_kind = getattr(invite_result, "recipient_kind", "")
                invite_recipient_email = getattr(invite_result, "recipient_email", "")
        except ServiceError:
            invite_status = "failed"
            logger.exception("Employee access invite resend failed for %s", employee_id)

        query = urlencode(
            {
                key: value
                for key, value in {
                    "saved": "1",
                    "invite_status": invite_status,
                    "invite_recipient_kind": invite_recipient_kind,
                    "invite_recipient_email": invite_recipient_email,
                }.items()
                if value
            }
        )
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?{query}",
            status_code=303,
        )

    async def suspend_employee_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Suspend an employee."""
        form = await self._request_form(request)
        reason = self._form_str(form, "reason")

        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        svc.suspend_employee(employee_id, reason=reason or None)
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
        )

    def set_employee_on_leave_response(
        self,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Set an employee on leave."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        svc.set_on_leave(employee_id)
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
        )

    async def resign_employee_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Record employee resignation."""
        form = await self._request_form(request)
        date_of_leaving = self._form_str(form, "date_of_leaving")
        eligible_for_final_payroll = parse_bool(
            self._form_str(form, "eligible_for_final_payroll")
        )
        final_payroll_cutoff_date = self._parse_date(
            self._form_str(form, "final_payroll_cutoff_date")
        )

        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)

        if eligible_for_final_payroll and not self._can_manage_final_payroll(auth):
            employee = svc.get_employee(employee_id)
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "Only admin, HR Director, or HR Manager can enable final payroll.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        leaving_date = self._parse_date(date_of_leaving)

        if leaving_date:
            svc.resign_employee(
                employee_id,
                leaving_date,
                eligible_for_final_payroll=eligible_for_final_payroll,
                final_payroll_cutoff_date=final_payroll_cutoff_date,
            )
            db.commit()
            queue_employee_mailcow_offboarding(
                employee_id,
                org_id,
                EmployeeStatus.RESIGNED,
            )
            return RedirectResponse(
                url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
            )

        employee = svc.get_employee(employee_id)
        context = self._employee_detail_context(request, auth, db, employee)
        context.update(
            {
                "employee": employee,
                "error": "Please provide a valid resignation date.",
            }
        )
        return templates.TemplateResponse(
            request, "people/hr/employee_detail.html", context
        )

    async def rehire_employee_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Rehire a previously separated employee."""
        form = await self._request_form(request)
        date_of_rejoining = self._form_str(form, "date_of_rejoining")
        notes = self._form_str(form, "notes")

        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)

        rejoining_date = self._parse_date(date_of_rejoining)

        if rejoining_date:
            svc.rehire_employee(employee_id, rejoining_date, notes=notes or None)
            db.commit()
            return RedirectResponse(
                url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
            )

        employee = svc.get_employee(employee_id)
        context = self._employee_detail_context(request, auth, db, employee)
        context.update(
            {
                "employee": employee,
                "error": "Please provide a valid rehire date.",
            }
        )
        return templates.TemplateResponse(
            request, "people/hr/employee_detail.html", context
        )

    async def terminate_employee_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Terminate an employee."""
        form = await self._request_form(request)
        date_of_leaving = self._form_str(form, "date_of_leaving")
        reason = self._form_str(form, "reason")
        eligible_for_final_payroll = parse_bool(
            self._form_str(form, "eligible_for_final_payroll")
        )
        final_payroll_cutoff_date = self._parse_date(
            self._form_str(form, "final_payroll_cutoff_date")
        )

        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)

        if eligible_for_final_payroll and not self._can_manage_final_payroll(auth):
            employee = svc.get_employee(employee_id)
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "Only admin, HR Director, or HR Manager can enable final payroll.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        leaving_date = self._parse_date(date_of_leaving)

        if leaving_date:
            svc.terminate_employee(
                employee_id,
                TerminationData(
                    date_of_leaving=leaving_date,
                    reason=reason or None,
                    eligible_for_final_payroll=eligible_for_final_payroll,
                    final_payroll_cutoff_date=final_payroll_cutoff_date,
                ),
            )
            db.commit()
            queue_employee_mailcow_offboarding(
                employee_id,
                org_id,
                EmployeeStatus.TERMINATED,
            )
            return RedirectResponse(
                url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
            )

        employee = svc.get_employee(employee_id)
        context = self._employee_detail_context(request, auth, db, employee)
        context.update(
            {
                "employee": employee,
                "error": "Please provide a valid termination date.",
            }
        )
        return templates.TemplateResponse(
            request, "people/hr/employee_detail.html", context
        )

    async def update_final_payroll_response(
        self,
        request: Request,
        employee_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Update final payroll settings for an exited employee."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        employee = svc.get_employee(employee_id, include_deleted=True)

        if not self._can_manage_final_payroll(auth):
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "Only admin, HR Director, or HR Manager can enable final payroll.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        if employee.status not in {
            EmployeeStatus.RESIGNED,
            EmployeeStatus.TERMINATED,
            EmployeeStatus.RETIRED,
        }:
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "Final payroll can only be managed for exited employees.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        form = await self._request_form(request)
        eligible_for_final_payroll = parse_bool(
            self._form_str(form, "eligible_for_final_payroll")
        )
        cutoff_date = self._parse_date(
            self._form_str(form, "final_payroll_cutoff_date")
        )

        if eligible_for_final_payroll and cutoff_date is None:
            cutoff_date = employee.date_of_leaving
        elif not eligible_for_final_payroll:
            cutoff_date = None

        svc.update_final_payroll_settings(
            employee_id,
            eligible_for_final_payroll=eligible_for_final_payroll,
            final_payroll_cutoff_date=cutoff_date,
        )
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?saved=1", status_code=303
        )

    async def toggle_user_credential_response(
        self,
        request: Request,
        employee_id: UUID,
        credential_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | HTMLResponse:
        """Enable/disable a user credential linked to an employee."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        employee = svc.get_employee(employee_id)

        if not employee.person_id:
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "This employee is not linked to a user account.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        credential = db.scalar(
            select(UserCredential).where(
                UserCredential.id == credential_id,
                UserCredential.person_id == employee.person_id,
            )
        )
        if not credential:
            context = self._employee_detail_context(request, auth, db, employee)
            context.update(
                {
                    "employee": employee,
                    "error": "User credential not found for this employee.",
                }
            )
            return templates.TemplateResponse(
                request, "people/hr/employee_detail.html", context
            )

        credential.is_active = not bool(credential.is_active)

        # If disabling, revoke active sessions for immediate lockout.
        if not credential.is_active:
            now = datetime.now(UTC)
            active_sessions = db.scalars(
                select(AuthSession).where(
                    AuthSession.person_id == employee.person_id,
                    AuthSession.status == SessionStatus.active,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            ).all()
            session_ids = [s.id for s in active_sessions]
            for session in active_sessions:
                session.status = SessionStatus.revoked
                session.revoked_at = now

            db.commit()

            if session_ids:
                from app.services.auth_dependencies import invalidate_session_cache

                for session_id in session_ids:
                    invalidate_session_cache(session_id)
        else:
            db.commit()

        return RedirectResponse(
            url=f"/people/hr/employees/{employee_id}?saved=1",
            status_code=303,
        )

    def _employee_detail_context(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        employee: Employee,
    ) -> dict:
        """Build employee detail page context."""
        org_id = coerce_uuid(auth.organization_id)

        person = db.get(Person, employee.person_id)
        dept = (
            db.get(Department, employee.department_id)
            if employee.department_id
            else None
        )
        desig = (
            db.get(Designation, employee.designation_id)
            if employee.designation_id
            else None
        )
        grade = db.get(EmployeeGrade, employee.grade_id) if employee.grade_id else None
        emp_type = (
            EmploymentTypeService(db, org_id).get_employment_type(
                employee.employment_type_id
            )
            if employee.employment_type_id
            else None
        )
        resolver = OrgResolver(db)
        manager = None
        manager_emp = resolver.get_manager(employee.employee_id, org_id)
        if manager_emp:
            manager_person = db.get(Person, manager_emp.person_id)
            manager = {
                "employee_id": manager_emp.employee_id,
                "name": manager_person.name if manager_person else "",
            }

        chain_entries = resolver.get_position_chain(employee.employee_id, org_id)
        designation_ids = {
            position.designation_id
            for position, _ in chain_entries
            if position.designation_id
        }
        department_ids = {
            position.department_id
            for position, _ in chain_entries
            if position.department_id
        }
        person_ids = {
            incumbent.person_id
            for _, incumbent in chain_entries
            if incumbent and incumbent.person_id
        }
        designations_by_id = (
            {
                d.designation_id: d
                for d in db.scalars(
                    select(Designation).where(
                        Designation.designation_id.in_(designation_ids)
                    )
                ).all()
            }
            if designation_ids
            else {}
        )
        departments_by_id = (
            {
                d.department_id: d
                for d in db.scalars(
                    select(Department).where(
                        Department.department_id.in_(department_ids)
                    )
                ).all()
            }
            if department_ids
            else {}
        )
        persons_by_id = (
            {
                p.id: p
                for p in db.scalars(
                    select(Person).where(Person.id.in_(person_ids))
                ).all()
            }
            if person_ids
            else {}
        )
        position_chain = []
        for position, incumbent in chain_entries:
            chain_designation = (
                designations_by_id.get(position.designation_id)
                if position.designation_id
                else None
            )
            chain_department = (
                departments_by_id.get(position.department_id)
                if position.department_id
                else None
            )
            incumbent_name = ""
            incumbent_id = None
            if incumbent:
                incumbent_id = incumbent.employee_id
                incumbent_person = persons_by_id.get(incumbent.person_id)
                if incumbent_person:
                    incumbent_name = incumbent_person.name
            position_chain.append(
                {
                    "position_id": position.position_id,
                    "designation_name": chain_designation.designation_name
                    if chain_designation
                    else "",
                    "department_name": chain_department.department_name
                    if chain_department
                    else "",
                    "incumbent_name": incumbent_name,
                    "incumbent_employee_id": incumbent_id,
                    "is_self": incumbent_id == employee.employee_id
                    if incumbent_id
                    else False,
                    "is_vacant": incumbent is None,
                }
            )
        expense_approver = None
        if employee.expense_approver_id:
            approver_emp = db.scalar(
                select(Employee).where(
                    Employee.employee_id == employee.expense_approver_id,
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            if approver_emp:
                approver_person = db.get(Person, approver_emp.person_id)
                expense_approver = {
                    "employee_id": approver_emp.employee_id,
                    "name": approver_person.name if approver_person else "",
                }

        credentials: list[UserCredential] = []
        if employee.person_id:
            credentials = list(
                db.scalars(
                    select(UserCredential)
                    .where(UserCredential.person_id == employee.person_id)
                    .order_by(UserCredential.created_at.asc())
                )
            )

        # Fetch salary structure assignments for this employee (eager load structure)
        salary_assignments = db.scalars(
            select(SalaryStructureAssignment)
            .options(joinedload(SalaryStructureAssignment.salary_structure))
            .where(
                SalaryStructureAssignment.organization_id == org_id,
                SalaryStructureAssignment.employee_id == employee.employee_id,
            )
            .order_by(SalaryStructureAssignment.from_date.desc())
        ).all()

        # Fetch tax profile for this employee
        tax_profile = db.scalar(
            select(EmployeeTaxProfile).where(
                EmployeeTaxProfile.organization_id == org_id,
                EmployeeTaxProfile.employee_id == employee.employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
        )

        # Fetch onboarding record for this employee
        from app.services.people.hr.lifecycle import LifecycleService

        lifecycle_svc = LifecycleService(db)
        onboarding = lifecycle_svc.get_onboarding_for_employee(
            org_id, employee.employee_id
        )

        can_view_assigned_assets = auth.has_module_access("fixed_assets")
        assigned_assets = (
            list_employee_assigned_assets(db, org_id, employee.employee_id)
            if can_view_assigned_assets
            else []
        )

        return {
            **base_context(request, auth, "Employee Details", "employees"),
            "employee": employee,
            "final_payroll_pending": bool(
                getattr(employee, "eligible_for_final_payroll", False)
                and getattr(employee, "final_payroll_processed_at", None) is None
            ),
            "recent_activity": get_recent_activity_for_record(
                db,
                org_id,
                record=employee,
                limit=10,
            ),
            "person": person,
            "department": dept,
            "designation": desig,
            "grade": grade,
            "employment_type": emp_type,
            "manager": manager,
            "expense_approver": expense_approver,
            "position_chain": position_chain,
            "credentials": credentials,
            "salary_assignments": salary_assignments,
            "tax_profile": tax_profile,
            "onboarding": onboarding,
            "assigned_assets": assigned_assets,
            "can_view_assigned_assets": can_view_assigned_assets,
            "can_manage_final_payroll": self._can_manage_final_payroll(auth),
        }

    def employee_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        employee_id: str,
        saved: bool = False,
        invite_status: str | None = None,
        invite_recipient_kind: str | None = None,
        invite_recipient_email: str | None = None,
    ) -> HTMLResponse:
        """Render employee detail page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)

        try:
            parsed_employee_id = coerce_uuid(employee_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Employee not found") from exc

        try:
            employee = svc.get_employee(parsed_employee_id)
        except EmployeeNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Employee not found") from exc

        context = self._employee_detail_context(request, auth, db, employee)
        context["saved"] = saved
        context["invite_status"] = invite_status
        context["invite_recipient_kind"] = invite_recipient_kind
        context["invite_recipient_email"] = invite_recipient_email

        return templates.TemplateResponse(
            request,
            "people/hr/employee_detail.html",
            context,
        )

    def employee_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        error: str | None = None,
        form_data: dict | None = None,
        errors: dict | None = None,
    ) -> HTMLResponse:
        """Render new employee form.

        Args:
            request: FastAPI request.
            auth: Authentication context.
            db: Database session.
            error: Top-level error message to display.
            form_data: Previously submitted form data (for re-populating on error).
            errors: Field-level validation errors.
        """
        org_id = coerce_uuid(auth.organization_id)
        org_svc = OrganizationService(db, org_id)

        # Get dropdown options
        departments = org_svc.list_departments(
            DepartmentFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        designations = org_svc.list_designations(
            DesignationFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        employment_types = self._list_employee_employment_types(db, org_id)
        grades = org_svc.list_employee_grades(
            EmployeeGradeFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        managers = (
            EmployeeService(db, org_id)
            .list_employees(
                EmployeeFilters(status=EmployeeStatus.ACTIVE),
                PaginationParams(limit=DROPDOWN_LIMIT),
                eager_load=True,
            )
            .items
        )
        manager_position_titles = {
            str(emp_id): title
            for emp_id, title in self._load_manager_position_titles(
                db, org_id, [m.employee_id for m in managers]
            ).items()
        }
        current_manager_id: UUID | None = None
        cost_centers = db.scalars(
            select(CostCenter)
            .where(
                CostCenter.organization_id == org_id,
                CostCenter.is_active.is_(True),
            )
            .order_by(CostCenter.cost_center_code)
        ).all()
        locations = db.scalars(
            select(Location)
            .where(
                Location.organization_id == org_id,
                Location.is_active.is_(True),
            )
            .order_by(Location.location_name)
        ).all()
        shift_types = (
            AttendanceService(db)
            .list_shift_types(
                org_id,
                is_active=True,
                pagination=PaginationParams(limit=DROPDOWN_LIMIT),
            )
            .items
        )
        salary_structures = self._list_active_salary_structures(db, org_id)
        pfas = self._list_pfas(db)
        user_rows = db.execute(
            select(UserCredential, Person)
            .join(Person, UserCredential.person_id == Person.id)
            .where(Person.organization_id == org_id)
            .order_by(Person.first_name, Person.last_name)
        ).all()
        user_options = {}
        for cred, user_person in user_rows:
            label = f"{user_person.name} ({user_person.email})"
            if cred.username:
                label = f"{label} - {cred.username}"
            user_options[str(user_person.id)] = {
                "person_id": user_person.id,
                "label": label,
            }
        user_accounts = list(user_options.values())

        context = {
            **base_context(request, auth, "New Employee", "employees"),
            "employee": None,
            "person": None,
            "departments": departments,
            "designations": designations,
            "employment_types": employment_types,
            "grades": grades,
            "managers": managers,
            "manager_position_titles": manager_position_titles,
            "position_options": [],
            "current_manager_id": current_manager_id,
            "employee_position": None,
            "employee_parent_position": None,
            "employee_position_manager": None,
            "cost_centers": cost_centers,
            "locations": locations,
            "shift_types": shift_types,
            "salary_structures": salary_structures,
            "user_accounts": user_accounts,
            "statuses": [
                s.value for s in EmployeeStatus if s != EmployeeStatus.TERMINATED
            ],
            "salary_modes": [m.value for m in SalaryMode],
            "genders": self._gender_options(),
            "error": error,
            "errors": errors or {},
            "form_data": form_data or {},
            "pfas": pfas,
            "tax_profile": None,
            "nigeria_states": NIGERIA_STATES,
            "can_edit_tax": auth.has_permission("people:write"),
        }

        return templates.TemplateResponse(
            request,
            "people/hr/employee_form.html",
            context,
        )

    @staticmethod
    def _list_active_salary_structures(
        db: Session, organization_id: UUID
    ) -> list[SalaryStructure]:
        return list(
            db.scalars(
                select(SalaryStructure)
                .where(
                    SalaryStructure.organization_id == organization_id,
                    SalaryStructure.is_active.is_(True),
                )
                .order_by(SalaryStructure.structure_name)
            ).all()
        )

    @staticmethod
    def _create_initial_salary_assignment(
        *,
        db: Session,
        organization_id: UUID,
        employee: Employee,
        salary_structure: SalaryStructure,
        base: Decimal | None,
    ) -> SalaryStructureAssignment:
        assignment = SalaryStructureAssignment(
            organization_id=organization_id,
            employee_id=employee.employee_id,
            structure_id=salary_structure.structure_id,
            from_date=employee.date_of_joining or date.today(),
            base=base or Decimal("0"),
            variable=Decimal("0"),
        )
        db.add(assignment)
        db.flush()
        return assignment

    @staticmethod
    def _log_employee_create_validation(
        request: Request,
        *,
        reason: str,
        current_tab: str,
        has_salary_structure: bool,
    ) -> None:
        logger.info(
            "Employee create form re-rendered after validation failure",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "reason": reason,
                "current_tab": current_tab or None,
                "has_salary_structure": has_salary_structure,
            },
        )

    @staticmethod
    def _form_str(form: Any, key: str) -> str:
        """Normalize form value to a trimmed string."""
        value = form.get(key) if form is not None else None
        if value is None or isinstance(value, UploadFile):
            return ""
        return str(value).strip()

    @staticmethod
    def _parse_date(value: str) -> date | None:
        """Parse a date string in YYYY-MM-DD format."""
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _parse_decimal(value: str) -> Decimal | None:
        """Parse a decimal value from form input."""
        if not value:
            return None
        try:
            return Decimal(value.replace(",", ""))
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _parse_salary_mode(value: str) -> SalaryMode | None:
        """Parse salary mode enum from form input."""
        if not value:
            return None
        try:
            return SalaryMode(value.upper())
        except ValueError:
            return None

    @staticmethod
    def _parse_role_names(value: str) -> list[str]:
        roles = list(
            dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
        )
        return roles or [getattr(settings, "dotmac_sub_staff_default_role", "staff")]

    @staticmethod
    def _gender_options() -> list[str]:
        """Return selectable gender values for employee forms."""
        return [g.value for g in Gender if g != Gender.unknown]

    @staticmethod
    def _list_employee_employment_types(
        db: Session,
        organization_id: UUID,
        current_employment_type_id: UUID | None = None,
    ) -> list[EmploymentTypeView]:
        """Return every active option plus a missing current assignment."""
        owner = EmploymentTypeService(db, organization_id)
        options = list(owner.iter_all(active=True))
        option_ids = {row.employment_type_id for row in options}
        if (
            current_employment_type_id is not None
            and current_employment_type_id not in option_ids
        ):
            options.append(owner.get_employment_type(current_employment_type_id))
        options.sort(
            key=lambda row: (
                row.type_name.casefold(),
                row.type_name,
                row.employment_type_id,
            )
        )
        return options

    @staticmethod
    def _list_pfas(db: Session) -> list[PFADirectory]:
        """Return the PFA dictionary ordered by name."""
        rows = db.scalars(
            select(PFADirectory)
            .where(PFADirectory.is_active.is_(True))
            .order_by(PFADirectory.pfa_name)
        ).all()
        return list(rows)

    def employee_edit_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        employee_id: str,
        error: str | None = None,
        form_data: dict | None = None,
        errors: dict | None = None,
    ) -> HTMLResponse:
        """Render edit employee form."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmployeeService(db, org_id)
        org_svc = OrganizationService(db, org_id)

        employee = svc.get_employee(coerce_uuid(employee_id))
        person = db.get(Person, employee.person_id)

        # Get dropdown options
        departments = org_svc.list_departments(
            DepartmentFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        designations = org_svc.list_designations(
            DesignationFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        employment_types = self._list_employee_employment_types(
            db,
            org_id,
            employee.employment_type_id,
        )
        grades = org_svc.list_employee_grades(
            EmployeeGradeFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        managers = (
            EmployeeService(db, org_id)
            .list_employees(
                EmployeeFilters(status=EmployeeStatus.ACTIVE),
                PaginationParams(limit=DROPDOWN_LIMIT),
                eager_load=True,
            )
            .items
        )
        manager_position_titles = {
            str(emp_id): title
            for emp_id, title in self._load_manager_position_titles(
                db, org_id, [m.employee_id for m in managers]
            ).items()
        }
        position_options = self._list_vacant_position_options(db, org_id)
        current_manager_emp = OrgResolver(db).get_manager(employee.employee_id, org_id)
        current_manager_id: UUID | None = (
            current_manager_emp.employee_id if current_manager_emp else None
        )
        position_context = self._employee_position_context(
            db,
            org_id,
            employee.employee_id,
        )
        cost_centers = db.scalars(
            select(CostCenter)
            .where(
                CostCenter.organization_id == org_id,
                CostCenter.is_active.is_(True),
            )
            .order_by(CostCenter.cost_center_code)
        ).all()
        locations = db.scalars(
            select(Location)
            .where(
                Location.organization_id == org_id,
                Location.is_active.is_(True),
            )
            .order_by(Location.location_name)
        ).all()
        shift_types = (
            AttendanceService(db)
            .list_shift_types(
                org_id,
                is_active=True,
                pagination=PaginationParams(limit=DROPDOWN_LIMIT),
            )
            .items
        )
        pfas = self._list_pfas(db)
        user_rows = db.execute(
            select(UserCredential, Person)
            .join(Person, UserCredential.person_id == Person.id)
            .where(Person.organization_id == org_id)
            .order_by(Person.first_name, Person.last_name)
        ).all()
        user_options = {}
        for cred, user_person in user_rows:
            label = f"{user_person.name} ({user_person.email})"
            if cred.username:
                label = f"{label} - {cred.username}"
            user_options[str(user_person.id)] = {
                "person_id": user_person.id,
                "label": label,
            }
        if person and str(person.id) not in user_options:
            user_options[str(person.id)] = {
                "person_id": person.id,
                "label": f"{person.name} ({person.email})",
            }
        user_accounts = list(user_options.values())

        tax_profile = db.scalar(
            select(EmployeeTaxProfile)
            .where(
                EmployeeTaxProfile.organization_id == org_id,
                EmployeeTaxProfile.employee_id == employee.employee_id,
                EmployeeTaxProfile.effective_to.is_(None),
            )
            .order_by(EmployeeTaxProfile.effective_from.desc())
            .limit(1)
        )

        context = {
            **base_context(request, auth, "Edit Employee", "employees"),
            "employee": employee,
            "person": person,
            "can_edit_person": auth.has_permission("people:write"),
            "can_edit_tax": auth.has_permission("people:write"),
            "departments": departments,
            "designations": designations,
            "employment_types": employment_types,
            "grades": grades,
            "managers": managers,
            "manager_position_titles": manager_position_titles,
            "position_options": position_options,
            "current_manager_id": current_manager_id,
            "employee_position": position_context["position"],
            "employee_parent_position": position_context["parent_position"],
            "employee_position_manager": position_context["manager"],
            "cost_centers": cost_centers,
            "locations": locations,
            "shift_types": shift_types,
            "user_accounts": user_accounts,
            "pfas": pfas,
            "tax_profile": tax_profile,
            "statuses": [
                s.value for s in EmployeeStatus if s != EmployeeStatus.TERMINATED
            ],
            "salary_modes": [m.value for m in SalaryMode],
            "genders": self._gender_options(),
            "error": error,
            "errors": errors or {},
            "form_data": form_data or {},
            "nigeria_states": NIGERIA_STATES,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/employee_form.html",
            context,
        )

    # =========================================================================
    # Departments
    # =========================================================================

    def list_departments_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        page: int = 1,
        is_active: bool | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> HTMLResponse:
        """Render department list page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)
        page_size = limit if limit in {10, 25, 50, 100, 200} else DEFAULT_PAGE_SIZE

        filters = DepartmentFilters(search=search, is_active=is_active)
        pagination = PaginationParams.from_page(page, page_size)
        result = svc.list_departments(filters, pagination)

        # Count employees per department in bulk
        dept_employee_counts = svc.get_department_headcounts_bulk(
            [dept.department_id for dept in result.items]
        )
        is_active_value = (
            "true" if is_active is True else "false" if is_active is False else ""
        )

        context = {
            **base_context(request, auth, "Departments", "departments"),
            "departments": result.items,
            "employee_counts": dept_employee_counts,
            "search": search or "",
            "is_active": is_active_value,
            "page": page,
            "total_pages": result.total_pages,
            "total_count": result.total,
            "total": result.total,
            "limit": pagination.limit,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
            "pagination_filters": {"is_active": is_active_value}
            if is_active_value
            else {},
        }

        return templates.TemplateResponse(
            request,
            "people/hr/departments.html",
            context,
        )

    def department_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        department_id: str | None = None,
    ) -> HTMLResponse:
        """Render department form (new or edit)."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)
        emp_svc = EmployeeService(db, org_id)

        department = None
        if department_id:
            department = svc.get_department(coerce_uuid(department_id))

        # Get parent department options (exclude current dept to prevent cycles)
        all_depts = svc.list_departments(
            DepartmentFilters(is_active=True),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items
        parent_options = [
            d
            for d in all_depts
            if not department or d.department_id != department.department_id
        ]

        # Get active employees for department head dropdown
        employee_options = emp_svc.list_employees(
            EmployeeFilters(status=EmployeeStatus.ACTIVE),
            PaginationParams(limit=DROPDOWN_LIMIT),
        ).items

        title = "Edit Department" if department else "New Department"
        context = {
            **base_context(request, auth, title, "departments"),
            "department": department,
            "parent_options": parent_options,
            "employee_options": employee_options,
            "errors": {},
        }

        return templates.TemplateResponse(
            request,
            "people/hr/department_form.html",
            context,
        )

    def department_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        department_id: str,
        page: int = 1,
    ) -> HTMLResponse:
        """Render department detail page."""
        org_id = coerce_uuid(auth.organization_id)
        org_svc = OrganizationService(db, org_id)
        emp_svc = EmployeeService(db, org_id)

        department = org_svc.get_department(coerce_uuid(department_id))
        headcount = org_svc.get_department_headcount(department.department_id)

        filters = EmployeeFilters(department_id=department.department_id)
        pagination = PaginationParams.from_page(page, DEFAULT_PAGE_SIZE)
        result = emp_svc.list_employees(filters, pagination, eager_load=True)
        employment_types_by_id = {
            row.employment_type_id: row
            for row in EmploymentTypeService(db, org_id).iter_all(active=None)
        }

        context = {
            **base_context(request, auth, department.department_name, "departments"),
            "department": department,
            "headcount": headcount,
            "employees": result.items,
            "employment_types_by_id": employment_types_by_id,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "can_manage_departments": _can_manage_departments(auth),
            "page": page,
            "total_pages": result.total_pages,
            "total": result.total,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/department_detail.html",
            context,
        )

    # =========================================================================
    # Designations
    # =========================================================================

    def list_designations_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> HTMLResponse:
        """Render designation list page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)
        page_size = limit if limit in {25, 50, 100, 200} else DEFAULT_PAGE_SIZE

        filters = DesignationFilters(search=search)
        pagination = PaginationParams.from_page(page, page_size)
        result = svc.list_designations(filters, pagination)

        context = {
            **base_context(request, auth, "Designations", "designations"),
            "designations": result.items,
            "search": search or "",
            "page": page,
            "total_pages": result.total_pages,
            "total_count": result.total,
            "total": result.total,
            "limit": pagination.limit,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/designations.html",
            context,
        )

    def designation_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        designation_id: str | None = None,
    ) -> HTMLResponse:
        """Render designation form (new or edit)."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)

        designation = None
        if designation_id:
            designation = svc.get_designation(coerce_uuid(designation_id))

        title = "Edit Designation" if designation else "New Designation"
        context = {
            **base_context(request, auth, title, "designations"),
            "designation": designation,
            "errors": {},
        }

        return templates.TemplateResponse(
            request,
            "people/hr/designation_form.html",
            context,
        )

    # =========================================================================
    # Employment Types
    # =========================================================================

    def list_employment_types_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """Render employment types list page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmploymentTypeService(db, org_id)

        filters = EmploymentTypeFilters(search=search)
        pagination = PaginationParams.from_page(page, DEFAULT_PAGE_SIZE)
        result = svc.list_employment_types(filters, pagination)

        context = {
            **base_context(request, auth, "Employment Types", "employment-types"),
            "employment_types": result.items,
            "can_manage_employment_types": auth.has_permission(
                "hr:employment_types:manage"
            ),
            "search": search or "",
            "page": page,
            "total_pages": result.total_pages,
            "total_count": result.total,
            "total": result.total,
            "limit": pagination.limit,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/employment_types.html",
            context,
        )

    def employment_type_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        employment_type_id: str | None = None,
    ) -> HTMLResponse:
        """Render employment type form (new or edit)."""
        org_id = coerce_uuid(auth.organization_id)
        svc = EmploymentTypeService(db, org_id)

        employment_type = None
        if employment_type_id:
            employment_type = svc.get_employment_type(coerce_uuid(employment_type_id))

        title = "Edit Employment Type" if employment_type else "New Employment Type"
        context = {
            **base_context(request, auth, title, "employment-types"),
            "employment_type": employment_type,
            "errors": {},
        }

        return templates.TemplateResponse(
            request,
            "people/hr/employment_type_form.html",
            context,
        )

    # =========================================================================
    # Employee Grades
    # =========================================================================

    def list_grades_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        search: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """Render employee grades list page."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)

        filters = EmployeeGradeFilters(search=search)
        pagination = PaginationParams.from_page(page, DEFAULT_PAGE_SIZE)
        result = svc.list_employee_grades(filters, pagination)

        context = {
            **base_context(request, auth, "Employee Grades", "grades"),
            "grades": result.items,
            "search": search or "",
            "page": page,
            "total_pages": result.total_pages,
            "total_count": result.total,
            "total": result.total,
            "limit": pagination.limit,
            "has_prev": result.has_prev,
            "has_next": result.has_next,
        }

        return templates.TemplateResponse(
            request,
            "people/hr/grades.html",
            context,
        )

    def grade_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        grade_id: str | None = None,
    ) -> HTMLResponse:
        """Render employee grade form (new or edit)."""
        org_id = coerce_uuid(auth.organization_id)
        svc = OrganizationService(db, org_id)

        grade = None
        if grade_id:
            grade = svc.get_employee_grade(coerce_uuid(grade_id))

        title = "Edit Employee Grade" if grade else "New Employee Grade"
        context = {
            **base_context(request, auth, title, "grades"),
            "grade": grade,
            "errors": {},
        }

        return templates.TemplateResponse(
            request,
            "people/hr/grade_form.html",
            context,
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _status_class(self, status: EmployeeStatus) -> str:
        """Get CSS class for employee status badge."""
        return {
            EmployeeStatus.DRAFT: "bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300",
            EmployeeStatus.ACTIVE: "bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300",
            EmployeeStatus.ON_LEAVE: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300",
            EmployeeStatus.SUSPENDED: "bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300",
            EmployeeStatus.RESIGNED: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
            EmployeeStatus.TERMINATED: "bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300",
            EmployeeStatus.RETIRED: "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300",
        }.get(status, "bg-slate-100 text-slate-700")


# Singleton instance
hr_web_service = HRWebService()
