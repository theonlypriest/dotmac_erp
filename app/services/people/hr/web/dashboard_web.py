"""People Dashboard Web Service.

Provides dashboard data and chart computations for the People module.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, extract, func, or_, select
from sqlalchemy.orm import Session

from app.models.people.attendance import Attendance
from app.models.people.hr import (
    Department,
    Employee,
    EmployeeStatus,
)
from app.models.people.leave import LeaveApplication
from app.models.people.payroll import SalarySlip
from app.models.people.recruit import Interview, JobApplicant, JobOffer, JobOpening
from app.models.person import Person
from app.services.common import coerce_uuid
from app.services.people.hr.employment_types import EmploymentTypeService
from app.templates import templates

if TYPE_CHECKING:
    from app.web.deps import WebAuthContext

logger = logging.getLogger(__name__)


class PeopleDashboardService:
    """Service for People module dashboard."""

    def dashboard_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """Render the People dashboard page."""
        from app.web.deps import base_context

        org_id = coerce_uuid(auth.organization_id)

        # Gather all dashboard data
        stats = self._get_dashboard_stats(db, org_id)
        chart_data = self._get_chart_data(db, org_id)
        recent_hires = self._get_recent_hires(db, org_id, limit=5)
        upcoming_birthdays = self._get_upcoming_birthdays(db, org_id)
        upcoming_anniversaries = self._get_upcoming_anniversaries(db, org_id)
        pending_approvals = self._get_pending_approvals(db, org_id)
        alerts = self._get_alerts(db, org_id)

        context = {
            **base_context(request, auth, "People Dashboard", "dashboard"),
            "stats": stats,
            "chart_data": chart_data,
            "recent_hires": recent_hires,
            "upcoming_birthdays": upcoming_birthdays,
            "upcoming_anniversaries": upcoming_anniversaries,
            "pending_approvals": pending_approvals,
            "alerts": alerts,
        }

        # Coach insight cards for People dashboards
        try:
            from app.services.coach.coach_service import CoachService

            coach_svc = CoachService(db)
            if coach_svc.is_enabled():
                context["coach_insights"] = coach_svc.top_insights_for_module(
                    org_id,
                    ["WORKFORCE", "EFFICIENCY", "DATA_QUALITY"],
                )
            else:
                context["coach_insights"] = []
        except Exception:
            logger.exception("Failed to load coach insights for people dashboard")
            context["coach_insights"] = []

        return templates.TemplateResponse(request, "people/dashboard.html", context)

    def _get_dashboard_stats(self, db: Session, org_id: UUID) -> dict[str, Any]:
        """Get aggregate statistics for the dashboard."""
        today = date.today()
        month_start = today.replace(day=1)

        # Employee counts
        total_employees = (
            db.scalar(
                select(func.count(Employee.employee_id)).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                    )
                )
            )
            or 0
        )

        active_employees = (
            db.scalar(
                select(func.count(Employee.employee_id)).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                        Employee.status == EmployeeStatus.ACTIVE,
                    )
                )
            )
            or 0
        )

        # New hires this month
        new_hires_this_month = (
            db.scalar(
                select(func.count(Employee.employee_id)).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                        Employee.date_of_joining >= month_start,
                    )
                )
            )
            or 0
        )

        # Leave stats
        on_leave_today = (
            db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    and_(
                        LeaveApplication.organization_id == org_id,
                        LeaveApplication.status == "APPROVED",
                        LeaveApplication.from_date <= today,
                        LeaveApplication.to_date >= today,
                    )
                )
            )
            or 0
        )

        pending_leave_requests = (
            db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    and_(
                        LeaveApplication.organization_id == org_id,
                        LeaveApplication.status == "SUBMITTED",
                    )
                )
            )
            or 0
        )

        approved_leave_this_month = (
            db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    and_(
                        LeaveApplication.organization_id == org_id,
                        LeaveApplication.status == "APPROVED",
                        LeaveApplication.from_date >= month_start,
                    )
                )
            )
            or 0
        )

        # Attendance stats for today
        checked_in_today = (
            db.scalar(
                select(func.count(Attendance.attendance_id)).where(
                    and_(
                        Attendance.organization_id == org_id,
                        Attendance.attendance_date == today,
                    )
                )
            )
            or 0
        )

        late_today = (
            db.scalar(
                select(func.count(Attendance.attendance_id)).where(
                    and_(
                        Attendance.organization_id == org_id,
                        Attendance.attendance_date == today,
                        Attendance.late_entry.is_(True),
                    )
                )
            )
            or 0
        )

        # Calculate attendance rate
        attendance_rate = 0
        late_rate = 0
        if active_employees > 0:
            attendance_rate = round((checked_in_today / active_employees) * 100)
            late_rate = (
                round((late_today / active_employees) * 100)
                if checked_in_today > 0
                else 0
            )

        absent_today = max(0, active_employees - checked_in_today - on_leave_today)

        # Recruitment stats
        open_positions = (
            db.scalar(
                select(func.count(JobOpening.job_opening_id)).where(
                    and_(
                        JobOpening.organization_id == org_id,
                        JobOpening.status == "OPEN",
                    )
                )
            )
            or 0
        )

        active_applicants = (
            db.scalar(
                select(func.count(JobApplicant.applicant_id)).where(
                    and_(
                        JobApplicant.organization_id == org_id,
                        JobApplicant.status.in_(["NEW", "SCREENING", "SHORTLISTED"]),
                    )
                )
            )
            or 0
        )

        scheduled_interviews = (
            db.scalar(
                select(func.count(Interview.interview_id)).where(
                    and_(
                        Interview.organization_id == org_id,
                        Interview.status == "SCHEDULED",
                        Interview.scheduled_to >= today,
                    )
                )
            )
            or 0
        )

        pending_offers = (
            db.scalar(
                select(func.count(JobOffer.offer_id)).where(
                    and_(
                        JobOffer.organization_id == org_id,
                        JobOffer.status == "PENDING_APPROVAL",
                    )
                )
            )
            or 0
        )

        return {
            "total_employees": total_employees,
            "active_employees": active_employees,
            "active_percentage": round((active_employees / total_employees) * 100)
            if total_employees > 0
            else 0,
            "new_hires_this_month": new_hires_this_month,
            "on_leave_today": on_leave_today,
            "pending_leave_requests": pending_leave_requests,
            "approved_leave_this_month": approved_leave_this_month,
            "checked_in_today": checked_in_today,
            "late_today": late_today,
            "absent_today": absent_today,
            "attendance_rate": attendance_rate,
            "late_rate": late_rate,
            "open_positions": open_positions,
            "active_applicants": active_applicants,
            "scheduled_interviews": scheduled_interviews,
            "pending_offers": pending_offers,
        }

    def _get_chart_data(self, db: Session, org_id: UUID) -> dict[str, Any]:
        """Get chart data for the dashboard."""
        chart_data = {}

        # Headcount trend (last 12 months)
        chart_data["headcount_trend"] = self._get_headcount_trend(db, org_id)

        # Department distribution
        chart_data["department_distribution"] = self._get_department_distribution(
            db, org_id
        )

        # Status breakdown
        chart_data["status_breakdown"] = self._get_status_breakdown(db, org_id)

        # Tenure distribution
        chart_data["tenure_distribution"] = self._get_tenure_distribution(db, org_id)

        # Age distribution
        chart_data["age_distribution"] = self._get_age_distribution(db, org_id)

        # Payroll trend (last 6 months)
        chart_data["payroll_trend"] = self._get_payroll_trend(db, org_id)

        # Gender distribution
        chart_data["gender_distribution"] = self._get_gender_distribution(db, org_id)

        # Employment type distribution
        chart_data["employment_type_distribution"] = (
            self._get_employment_type_distribution(db, org_id)
        )

        return chart_data

    def _get_headcount_trend(self, db: Session, org_id: UUID) -> list[dict[str, Any]]:
        """Get monthly headcount for the last 12 months."""
        today = date.today()
        trend = []

        for i in range(11, -1, -1):
            # Calculate month start/end
            month_date = today - timedelta(days=i * 30)
            month_name = month_date.strftime("%b")

            # Count employees active at end of that month
            month_end = month_date.replace(day=28)  # Simplified
            count = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        and_(
                            Employee.organization_id == org_id,
                            Employee.status != EmployeeStatus.TERMINATED,
                            Employee.date_of_joining <= month_end,
                            or_(
                                Employee.date_of_leaving.is_(None),
                                Employee.date_of_leaving > month_end,
                            ),
                        )
                    )
                )
                or 0
            )

            # Count hires that month
            hires = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        and_(
                            Employee.organization_id == org_id,
                            extract("year", Employee.date_of_joining)
                            == month_date.year,
                            extract("month", Employee.date_of_joining)
                            == month_date.month,
                        )
                    )
                )
                or 0
            )

            # Count exits that month
            exits = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        and_(
                            Employee.organization_id == org_id,
                            Employee.date_of_leaving.isnot(None),
                            extract("year", Employee.date_of_leaving)
                            == month_date.year,
                            extract("month", Employee.date_of_leaving)
                            == month_date.month,
                        )
                    )
                )
                or 0
            )

            trend.append(
                {
                    "month": month_name,
                    "count": count,
                    "hires": hires,
                    "exits": exits,
                }
            )

        return trend

    def _get_department_distribution(
        self, db: Session, org_id: UUID
    ) -> list[dict[str, Any]]:
        """Get employee count by department."""
        results = db.execute(
            select(Department.department_name, func.count(Employee.employee_id))
            .join(Employee, Employee.department_id == Department.department_id)
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                    Employee.status == EmployeeStatus.ACTIVE,
                )
            )
            .group_by(Department.department_name)
            .order_by(func.count(Employee.employee_id).desc())
            .limit(8)
        ).all()

        return [{"name": name, "count": count} for name, count in results]

    def _get_status_breakdown(self, db: Session, org_id: UUID) -> list[dict[str, Any]]:
        """Get employee count by status."""
        results = db.execute(
            select(Employee.status, func.count(Employee.employee_id))
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            .group_by(Employee.status)
        ).all()

        status_labels = {
            EmployeeStatus.DRAFT: "Draft",
            EmployeeStatus.ACTIVE: "Active",
            EmployeeStatus.ON_LEAVE: "On Leave",
            EmployeeStatus.SUSPENDED: "Suspended",
            EmployeeStatus.RESIGNED: "Resigned",
            EmployeeStatus.TERMINATED: "Terminated",
            EmployeeStatus.RETIRED: "Retired",
        }

        return [
            {
                "status": status_labels.get(
                    status, str(status.value) if status else "Unknown"
                ),
                "count": count,
            }
            for status, count in results
        ]

    def _get_tenure_distribution(
        self, db: Session, org_id: UUID
    ) -> list[dict[str, Any]]:
        """Get employee count by tenure range."""
        today = date.today()
        ranges = [
            ("< 1 year", 0, 1),
            ("1-2 years", 1, 2),
            ("2-5 years", 2, 5),
            ("5-10 years", 5, 10),
            ("10+ years", 10, 100),
        ]

        distribution = []
        for label, min_years, max_years in ranges:
            min_date = today - timedelta(days=max_years * 365)
            max_date = today - timedelta(days=min_years * 365)

            count = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        and_(
                            Employee.organization_id == org_id,
                            Employee.status != EmployeeStatus.TERMINATED,
                            Employee.status == EmployeeStatus.ACTIVE,
                            Employee.date_of_joining > min_date,
                            Employee.date_of_joining <= max_date,
                        )
                    )
                )
                or 0
            )

            distribution.append({"range": label, "count": count})

        return distribution

    def _get_age_distribution(self, db: Session, org_id: UUID) -> list[dict[str, Any]]:
        """Get employee count by age range."""
        today = date.today()
        ranges = [
            ("18-25", 18, 25),
            ("26-35", 26, 35),
            ("36-45", 36, 45),
            ("46-55", 46, 55),
            ("55+", 55, 100),
        ]

        distribution = []
        for label, min_age, max_age in ranges:
            min_dob = today - timedelta(days=(max_age + 1) * 365)
            max_dob = today - timedelta(days=min_age * 365)

            count = (
                db.scalar(
                    select(func.count(Employee.employee_id))
                    .join(Person, Person.id == Employee.person_id)
                    .where(
                        and_(
                            Employee.organization_id == org_id,
                            Employee.status != EmployeeStatus.TERMINATED,
                            Employee.status == EmployeeStatus.ACTIVE,
                            Person.date_of_birth.isnot(None),
                            Person.date_of_birth > min_dob,
                            Person.date_of_birth <= max_dob,
                        )
                    )
                )
                or 0
            )

            distribution.append({"range": label, "count": count})

        return distribution

    def _get_payroll_trend(self, db: Session, org_id: UUID) -> list[dict[str, Any]]:
        """Get monthly payroll totals for the last 6 months."""
        today = date.today()
        trend = []

        for i in range(5, -1, -1):
            month_date = today - timedelta(days=i * 30)
            month_name = month_date.strftime("%b")

            # Get gross and net for that month
            result = db.execute(
                select(
                    func.coalesce(func.sum(SalarySlip.gross_pay), 0),
                    func.coalesce(func.sum(SalarySlip.net_pay), 0),
                ).where(
                    and_(
                        SalarySlip.organization_id == org_id,
                        extract("year", SalarySlip.start_date) == month_date.year,
                        extract("month", SalarySlip.start_date) == month_date.month,
                    )
                )
            ).one()

            trend.append(
                {
                    "month": month_name,
                    "gross": float(result[0] or 0),
                    "net": float(result[1] or 0),
                }
            )

        return trend

    def _get_recent_hires(
        self, db: Session, org_id: UUID, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get most recently hired employees."""
        results = db.execute(
            select(Employee, Person, Department)
            .join(Person, Person.id == Employee.person_id)
            .outerjoin(Department, Department.department_id == Employee.department_id)
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
            .order_by(Employee.date_of_joining.desc())
            .limit(limit)
        ).all()

        hires = []
        for emp, person, dept in results:
            desig = emp.designation
            hires.append(
                {
                    "id": str(emp.employee_id),
                    "initials": (person.name[0] if person.name else "?").upper(),
                    "full_name": person.name,
                    "designation": desig.designation_name if desig else "",
                    "department": dept.department_name if dept else "",
                    "date_of_joining": emp.date_of_joining.strftime("%b %d, %Y")
                    if emp.date_of_joining
                    else "",
                }
            )

        return hires

    def _get_upcoming_birthdays(
        self, db: Session, org_id: UUID, days: int = 7
    ) -> list[dict[str, Any]]:
        """Get employees with birthdays in the next N days."""
        today = date.today()

        # This is simplified - in production you'd want proper date wrapping for year boundaries
        results = db.execute(
            select(Person.name_expr().label("employee_name"), Person.date_of_birth)
            .join(Employee, Employee.person_id == Person.id)
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Person.date_of_birth.isnot(None),
                    extract("month", Person.date_of_birth) == today.month,
                    extract("day", Person.date_of_birth) >= today.day,
                    extract("day", Person.date_of_birth) <= today.day + days,
                )
            )
            .order_by(extract("day", Person.date_of_birth))
            .limit(5)
        ).all()

        return [
            {
                "name": name or "",
                "date": dob.strftime("%b %d") if dob else "",
            }
            for name, dob in results
        ]

    def _get_upcoming_anniversaries(
        self, db: Session, org_id: UUID
    ) -> list[dict[str, Any]]:
        """Get employees with work anniversaries this month."""
        today = date.today()

        results = db.execute(
            select(Person.name_expr().label("employee_name"), Employee.date_of_joining)
            .join(Employee, Employee.person_id == Person.id)
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.date_of_joining.isnot(None),
                    extract("month", Employee.date_of_joining) == today.month,
                    extract("year", Employee.date_of_joining) < today.year,
                )
            )
            .order_by(extract("day", Employee.date_of_joining))
            .limit(5)
        ).all()

        return [
            {
                "name": name or "",
                "years": today.year - doj.year if doj else 0,
            }
            for name, doj in results
        ]

    def _get_gender_distribution(
        self, db: Session, org_id: UUID
    ) -> list[dict[str, Any]]:
        """Get employee count by gender."""
        from app.models.people.hr.employee import Gender

        results = db.execute(
            select(Employee.gender, func.count(Employee.employee_id))
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.gender.isnot(None),
                )
            )
            .group_by(Employee.gender)
        ).all()

        gender_labels = {
            Gender.MALE: "Male",
            Gender.FEMALE: "Female",
            Gender.OTHER: "Other",
            Gender.PREFER_NOT_TO_SAY: "Prefer not to say",
        }

        return [
            {
                "gender": gender_labels.get(
                    gender, str(gender.value) if gender else "Unknown"
                ),
                "count": count,
            }
            for gender, count in results
            if gender
        ]

    def _get_employment_type_distribution(
        self, db: Session, org_id: UUID
    ) -> list[dict[str, Any]]:
        """Get employee count by employment type."""
        results = db.execute(
            select(Employee.employment_type_id, func.count(Employee.employee_id))
            .where(
                and_(
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.employment_type_id.is_not(None),
                )
            )
            .group_by(Employee.employment_type_id)
        ).all()

        names_by_id = {
            employment_type.employment_type_id: employment_type.type_name
            for employment_type in EmploymentTypeService(db, org_id).iter_all()
        }
        counts_by_name: dict[str, int] = {}
        for employment_type_id, count in results:
            if employment_type_id is None:
                continue
            name = names_by_id[employment_type_id]
            counts_by_name[name] = counts_by_name.get(name, 0) + count

        return [
            {"type": name, "count": count}
            for name, count in sorted(
                counts_by_name.items(),
                key=lambda item: (-item[1], item[0].casefold(), item[0]),
            )
        ]

    def _get_pending_approvals(self, db: Session, org_id: UUID) -> dict[str, Any]:
        """Get counts of items pending approval."""
        # Pending leave requests
        pending_leave = (
            db.scalar(
                select(func.count(LeaveApplication.application_id)).where(
                    and_(
                        LeaveApplication.organization_id == org_id,
                        LeaveApplication.status == "SUBMITTED",
                    )
                )
            )
            or 0
        )

        # Pending job offers
        pending_offers = (
            db.scalar(
                select(func.count(JobOffer.offer_id)).where(
                    and_(
                        JobOffer.organization_id == org_id,
                        JobOffer.status == "PENDING_APPROVAL",
                    )
                )
            )
            or 0
        )

        # Pending interviews (scheduled for today or past due)
        today = date.today()
        pending_interviews = (
            db.scalar(
                select(func.count(Interview.interview_id)).where(
                    and_(
                        Interview.organization_id == org_id,
                        Interview.status == "SCHEDULED",
                        Interview.scheduled_to <= today,
                    )
                )
            )
            or 0
        )

        total = pending_leave + pending_offers + pending_interviews

        return {
            "total": total,
            "leave_requests": pending_leave,
            "job_offers": pending_offers,
            "interviews": pending_interviews,
        }

    def _get_alerts(self, db: Session, org_id: UUID) -> list[dict[str, Any]]:
        """Get alerts and notifications for the dashboard."""
        alerts = []
        today = date.today()

        # Employees with probation ending soon (within 30 days)
        probation_ending = (
            db.scalar(
                select(func.count(Employee.employee_id)).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                        Employee.status == EmployeeStatus.ACTIVE,
                        Employee.probation_end_date.isnot(None),
                        Employee.confirmation_date.is_(None),  # Not yet confirmed
                        Employee.probation_end_date >= today,
                        Employee.probation_end_date <= today + timedelta(days=30),
                    )
                )
            )
            or 0
        )

        if probation_ending > 0:
            alerts.append(
                {
                    "type": "warning",
                    "icon": "clock",
                    "title": "Probation Review Due",
                    "message": f"{probation_ending} employee(s) probation period ending soon",
                    "url": "/people/hr/employees?filter=probation_ending",
                }
            )

        # Employees who have resigned but are still active (in notice period)
        in_notice = (
            db.scalar(
                select(func.count(Employee.employee_id)).where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                        Employee.status == EmployeeStatus.RESIGNED,
                        Employee.date_of_leaving.isnot(None),
                        Employee.date_of_leaving >= today,
                    )
                )
            )
            or 0
        )

        if in_notice > 0:
            alerts.append(
                {
                    "type": "info",
                    "icon": "document",
                    "title": "Notice Period",
                    "message": f"{in_notice} employee(s) serving notice period",
                    "url": "/people/hr/employees?status=resigned",
                }
            )

        # Employees with missing critical info
        missing_info = (
            db.scalar(
                select(func.count(Employee.employee_id))
                .join(Person, Person.id == Employee.person_id)
                .where(
                    and_(
                        Employee.organization_id == org_id,
                        Employee.status != EmployeeStatus.TERMINATED,
                        Employee.status == EmployeeStatus.ACTIVE,
                        Person.date_of_birth.is_(None),
                    )
                )
            )
            or 0
        )

        if missing_info > 0:
            alerts.append(
                {
                    "type": "info",
                    "icon": "user",
                    "title": "Incomplete Profiles",
                    "message": f"{missing_info} employee(s) with missing date of birth",
                    "url": "/people/hr/employees?filter=incomplete",
                }
            )

        return alerts


# Singleton instance
people_dashboard_service = PeopleDashboardService()
