"""
HR Module Background Tasks - Celery tasks for HR workflows.

Handles:
- Probation period ending notifications
- Contract expiry notifications
- Work anniversary notifications
- Employee birthday notifications
- Performance review due reminders
- Certification expiry warnings
"""

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from celery import shared_task
from sqlalchemy import extract, func, select

from app.db.session_context import cross_org_session, session_for_org
from app.models.finance.core_org.organization import Organization
from app.models.notification import EntityType, NotificationChannel, NotificationType
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.models.person import Person, PersonStatus
from app.models.rbac import PersonRole, Role
from app.services.notification import NotificationService
from app.services.people.hr.org_resolver import OrgResolver
from app.tenant_catalog import organization_ids

logger = logging.getLogger(__name__)


def _resolve_manager(db, employee: Employee, organization_id) -> Employee | None:
    return OrgResolver(db).get_manager(employee.employee_id, organization_id)


def _list_organization_ids() -> list[uuid.UUID]:
    return organization_ids(include_inactive=True)


def _get_hr_manager_recipients(db, org_id: uuid.UUID) -> list[Person]:
    """Return active HR managers for birthday notifications."""
    recipient_ids = list(
        db.scalars(
            select(Person.id)
            .join(PersonRole, PersonRole.person_id == Person.id)
            .join(Role, Role.id == PersonRole.role_id)
            .where(
                Person.organization_id == org_id,
                Person.is_active.is_(True),
                Person.status == PersonStatus.active,
                Role.name == "hr_manager",
                Role.is_active.is_(True),
            )
            .distinct()
        ).all()
    )
    if not recipient_ids:
        return []

    return list(
        db.scalars(
            select(Person)
            .where(Person.id.in_(recipient_ids))
            .order_by(Person.first_name, Person.last_name, Person.id)
        ).all()
    )


@shared_task(
    bind=True,
    max_retries=3,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_employee_mailcow_offboarding(
    self,
    employee_id: str,
    organization_id: str,
    status: str,
) -> dict[str, Any]:
    """Run ERP/Mailcow offboarding for an exited employee."""
    from app.services.people.hr.offboarding import EmployeeOffboardingService

    org_uuid = uuid.UUID(organization_id)
    employee_uuid = uuid.UUID(employee_id)
    logger.info(
        "Running Mailcow offboarding for employee %s status=%s",
        employee_id,
        status,
    )
    with session_for_org(org_uuid) as db:
        result = EmployeeOffboardingService(db).offboard_employee(
            org_uuid,
            employee_uuid,
        )
        db.commit()
        return {
            "employee_id": result.employee_id,
            "email": result.email,
            "status": result.status,
            "erp_credentials_disabled": result.erp_credentials_disabled,
            "erp_sessions_revoked": result.erp_sessions_revoked,
            "person_deactivated": result.person_deactivated,
            "mailcow_enabled": result.mailcow_enabled,
            "mailcow_mailbox_found": result.mailcow_mailbox_found,
            "mailcow_password_reset": result.mailcow_password_reset,
            "sieve_offboarding_script_updated": (
                result.sieve_offboarding_script_updated
            ),
            "sogo_inactive_forward_updated": result.sogo_inactive_forward_updated,
            "shared_profiles_cleaned": result.shared_profiles_cleaned,
            "shared_sieve_scripts_cleaned": result.shared_sieve_scripts_cleaned,
            "skipped": result.skipped,
            "errors": result.errors,
        }


@shared_task
def process_probation_ending_notifications() -> dict:
    """
    Send notifications for employees whose probation period is ending soon.

    Sends notifications:
    - 14 days before probation ends
    - 7 days before probation ends
    - On the day probation ends

    Returns:
        Dict with notification statistics
    """
    from app.services.hr_notifications import HRNotificationService

    FIRST_NOTICE_DAYS = 14
    SECOND_NOTICE_DAYS = 7
    FINAL_NOTICE_DAYS = 0

    logger.info("Processing probation ending notifications")

    results: dict[str, Any] = {
        "first_notices_sent": 0,
        "second_notices_sent": 0,
        "final_notices_sent": 0,
        "errors": [],
    }

    today = date.today()
    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)

            # Find employees on probation with probation end dates
            probation_employees = db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.probation_end_date.isnot(None),
                )
            ).all()

            for employee in probation_employees:
                try:
                    probation_end = employee.probation_end_date
                    if probation_end is None:
                        continue
                    days_remaining = (probation_end - today).days

                    # Skip if probation already ended or too far away
                    if days_remaining < 0 or days_remaining > FIRST_NOTICE_DAYS:
                        continue

                    # Determine notice type
                    if days_remaining == FINAL_NOTICE_DAYS:
                        notice_type = "final"
                    elif days_remaining <= SECOND_NOTICE_DAYS:
                        notice_type = "second"
                    elif days_remaining <= FIRST_NOTICE_DAYS:
                        notice_type = "first"
                    else:
                        continue

                    # Get manager
                    manager = _resolve_manager(db, employee, org_id)

                    # Send notification to manager
                    if manager:
                        success = (
                            notification_service.send_probation_ending_notification(
                                employee,
                                manager,
                                days_remaining=days_remaining,
                            )
                        )

                        if success:
                            if notice_type == "first":
                                results["first_notices_sent"] += 1
                            elif notice_type == "second":
                                results["second_notices_sent"] += 1
                            else:
                                results["final_notices_sent"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process probation notification for employee %s: %s",
                        employee.employee_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "employee_id": str(employee.employee_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    total_sent = (
        results["first_notices_sent"]
        + results["second_notices_sent"]
        + results["final_notices_sent"]
    )
    logger.info("Probation notifications complete: %d sent", total_sent)

    return results


@shared_task
def process_contract_expiry_notifications() -> dict:
    """
    Send notifications for employees whose contracts are expiring soon.

    Sends notifications:
    - 30 days before contract expires
    - 14 days before contract expires
    - 7 days before contract expires

    Returns:
        Dict with notification statistics
    """
    from app.services.hr_notifications import HRNotificationService

    FIRST_NOTICE_DAYS = 30
    SECOND_NOTICE_DAYS = 14
    FINAL_NOTICE_DAYS = 7

    logger.info("Processing contract expiry notifications")

    results: dict[str, Any] = {
        "notifications_sent": 0,
        "errors": [],
    }

    today = date.today()

    contract_end_attr = getattr(Employee, "contract_end_date", None)
    if contract_end_attr is None:
        logger.info(
            "Employee.contract_end_date not available; skipping contract expiry notifications"
        )
        return results

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)
            # Find employees with contract end dates
            contract_employees = db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    contract_end_attr.isnot(None),
                )
            ).all()

            for employee in contract_employees:
                try:
                    contract_end = getattr(employee, "contract_end_date", None)
                    if contract_end is None:
                        continue
                    days_remaining = (contract_end - today).days

                    # Skip if contract already ended or too far away
                    if days_remaining < 0 or days_remaining > FIRST_NOTICE_DAYS:
                        continue

                    # Only send on specific days
                    if days_remaining not in [
                        FIRST_NOTICE_DAYS,
                        SECOND_NOTICE_DAYS,
                        FINAL_NOTICE_DAYS,
                    ]:
                        continue

                    # Get manager
                    manager = _resolve_manager(db, employee, org_id)

                    # Send notification to manager and HR
                    if manager:
                        success = (
                            notification_service.send_contract_expiry_notification(
                                employee,
                                manager,
                                days_remaining=days_remaining,
                            )
                        )

                        if success:
                            results["notifications_sent"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process contract expiry notification for employee %s: %s",
                        employee.employee_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "employee_id": str(employee.employee_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    logger.info(
        "Contract expiry notifications complete: %d sent", results["notifications_sent"]
    )

    return results


@shared_task
def process_work_anniversary_notifications() -> dict:
    """
    Send notifications for employee work anniversaries.

    Sends notifications for employees with work anniversaries this week.

    Returns:
        Dict with notification statistics
    """
    from app.services.hr_notifications import HRNotificationService

    logger.info("Processing work anniversary notifications")

    results: dict[str, Any] = {
        "notifications_sent": 0,
        "milestone_notifications": 0,
        "errors": [],
    }

    today = date.today()
    week_end = today + timedelta(days=7)

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)
            # Find active employees
            active_employees = db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.date_of_joining.isnot(None),
                )
            ).all()

            for employee in active_employees:
                try:
                    joining_date = employee.date_of_joining

                    # Calculate this year's anniversary
                    this_year_anniversary = joining_date.replace(year=today.year)

                    # Check if anniversary is within this week
                    if not (today <= this_year_anniversary <= week_end):
                        continue

                    # Calculate years of service
                    years_of_service = today.year - joining_date.year

                    # Determine if it's a milestone year (5, 10, 15, 20, 25, etc.)
                    is_milestone = years_of_service > 0 and years_of_service % 5 == 0

                    # Get manager
                    manager = _resolve_manager(db, employee, org_id)

                    # Send notification
                    success = notification_service.send_work_anniversary_notification(
                        employee,
                        manager,
                        years_of_service=years_of_service,
                        is_milestone=is_milestone,
                    )

                    if success:
                        results["notifications_sent"] += 1
                        if is_milestone:
                            results["milestone_notifications"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process anniversary notification for employee %s: %s",
                        employee.employee_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "employee_id": str(employee.employee_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    logger.info(
        "Work anniversary notifications complete: %d sent (%d milestones)",
        results["notifications_sent"],
        results["milestone_notifications"],
    )

    return results


@shared_task
def process_birthday_notifications() -> dict:
    """
    Send notifications for employee birthdays.

    Sends notifications for employees with birthdays today or tomorrow.

    Returns:
        Dict with notification statistics
    """
    from app.services.hr_notifications import HRNotificationService

    logger.info("Processing birthday notifications")

    results: dict[str, Any] = {
        "notifications_sent": 0,
        "errors": [],
    }

    today = date.today()
    tomorrow = today + timedelta(days=1)

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)
            # Find active employees
            active_employees = db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.date_of_birth.isnot(None),
                )
            ).all()

            for employee in active_employees:
                try:
                    birthday = employee.date_of_birth
                    if birthday is None:
                        continue

                    # Check if birthday is today or tomorrow
                    this_year_birthday = birthday.replace(year=today.year)

                    if this_year_birthday == today:
                        notification_type = "today"
                    elif this_year_birthday == tomorrow:
                        notification_type = "tomorrow"
                    else:
                        continue

                    # Get manager
                    manager = _resolve_manager(db, employee, org_id)

                    # Send notification to manager
                    if manager and notification_type == "tomorrow":
                        success = notification_service.send_birthday_notification(
                            employee,
                            manager,
                            is_advance_notice=True,
                        )

                        if success:
                            results["notifications_sent"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process birthday notification for employee %s: %s",
                        employee.employee_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "employee_id": str(employee.employee_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    logger.info(
        "Birthday notifications complete: %d sent", results["notifications_sent"]
    )

    return results


@shared_task
def send_hr_birthday_morning_email() -> dict:
    """Create same-day birthday notifications for HR managers."""
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    results: dict[str, Any] = {
        "notifications_created": 0,
        "birthdays_found": 0,
        "recipients_notified": 0,
        "errors": [],
    }

    logger.info("Processing HR birthday morning emails")

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            try:
                birthday_rows = db.execute(
                    select(
                        Employee.employee_id,
                        Person.name_expr().label("employee_name"),
                    )
                    .join(Employee, Employee.person_id == Person.id)
                    .where(
                        Employee.organization_id == org_id,
                        Employee.status == EmployeeStatus.ACTIVE,
                        Person.date_of_birth.isnot(None),
                        extract("month", Person.date_of_birth) == today.month,
                        extract("day", Person.date_of_birth) == today.day,
                    )
                    .order_by(Person.first_name, Person.last_name)
                ).all()

                birthdays = [
                    (employee_id, name)
                    for employee_id, name in birthday_rows
                    if employee_id and isinstance(name, str) and name
                ]
                if not birthdays:
                    continue

                results["birthdays_found"] += len(birthdays)

                body_lines = [
                    f"Today is {employee_name}'s birthday."
                    for _, employee_name in birthdays
                ]
                message = "\n".join(body_lines)
                entity_id = birthdays[0][0]
                recipients = _get_hr_manager_recipients(db, org_id)
                if not recipients:
                    db.commit()
                    continue

                notification_service = NotificationService()
                for recipient in recipients:
                    notification = notification_service.create_if_not_sent_since(
                        db,
                        organization_id=org_id,
                        recipient_id=recipient.id,
                        entity_type=EntityType.EMPLOYEE,
                        entity_id=entity_id,
                        notification_type=NotificationType.REMINDER,
                        title="Staff Birthday Reminder",
                        message=message,
                        since=today_start,
                        channel=NotificationChannel.BOTH,
                        action_url="/people",
                    )
                    if notification is not None:
                        results["notifications_created"] += 1
                        results["recipients_notified"] += 1
                db.commit()
            except Exception as exc:
                logger.error(
                    "Failed to create HR birthday notifications for organization %s: %s",
                    org_id,
                    exc,
                )
                results["errors"].append(
                    {
                        "organization_id": str(org_id),
                        "error": str(exc),
                    }
                )

    logger.info(
        "HR birthday morning notifications complete: %d created (%d birthdays)",
        results["notifications_created"],
        results["birthdays_found"],
    )
    return results


@shared_task
def process_performance_review_reminders() -> dict:
    """
    Send reminders for upcoming performance reviews.

    Checks appraisal cycles and sends reminders:
    - Self-assessment due reminders
    - Manager review due reminders
    - Calibration deadline reminders

    Returns:
        Dict with reminder statistics
    """
    from app.models.people.perf.appraisal import Appraisal, AppraisalStatus
    from app.models.people.perf.appraisal_cycle import (
        AppraisalCycle,
        AppraisalCycleStatus,
    )
    from app.services.hr_notifications import HRNotificationService

    logger.info("Processing performance review reminders")

    results: dict[str, Any] = {
        "self_assessment_reminders": 0,
        "manager_review_reminders": 0,
        "calibration_reminders": 0,
        "errors": [],
    }

    today = date.today()

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)
            # Find active cycles for this organization
            active_cycles = db.scalars(
                select(AppraisalCycle).where(
                    AppraisalCycle.organization_id == org_id,
                    AppraisalCycle.status.in_(
                        [
                            AppraisalCycleStatus.ACTIVE,
                            AppraisalCycleStatus.REVIEW,
                            AppraisalCycleStatus.CALIBRATION,
                        ]
                    ),
                )
            ).all()

            for cycle in active_cycles:
                try:
                    # Check self-assessment deadline
                    if cycle.self_assessment_deadline:
                        days_to_deadline = (cycle.self_assessment_deadline - today).days
                        if 0 <= days_to_deadline <= 7:
                            # Find employees with pending self-assessment
                            pending_appraisals = db.scalars(
                                select(Appraisal).where(
                                    Appraisal.cycle_id == cycle.cycle_id,
                                    Appraisal.status.in_(
                                        [
                                            AppraisalStatus.DRAFT,
                                            AppraisalStatus.SELF_ASSESSMENT,
                                        ]
                                    ),
                                )
                            ).all()

                            for appraisal in pending_appraisals:
                                employee = db.get(Employee, appraisal.employee_id)
                                if employee:
                                    success = notification_service.send_self_assessment_reminder(
                                        employee,
                                        cycle,
                                        days_remaining=days_to_deadline,
                                    )
                                    if success:
                                        results["self_assessment_reminders"] += 1

                    # Check manager review deadline
                    if cycle.manager_review_deadline:
                        days_to_deadline = (cycle.manager_review_deadline - today).days
                        if 0 <= days_to_deadline <= 7:
                            # Find appraisals pending manager review
                            pending_reviews = db.scalars(
                                select(Appraisal).where(
                                    Appraisal.cycle_id == cycle.cycle_id,
                                    Appraisal.status.in_(
                                        [
                                            AppraisalStatus.PENDING_REVIEW,
                                            AppraisalStatus.UNDER_REVIEW,
                                        ]
                                    ),
                                )
                            ).all()

                            for appraisal in pending_reviews:
                                manager = db.get(Employee, appraisal.manager_id)
                                employee = db.get(Employee, appraisal.employee_id)
                                if manager and employee:
                                    success = notification_service.send_manager_review_reminder(
                                        manager,
                                        employee,
                                        cycle,
                                        days_remaining=days_to_deadline,
                                    )
                                    if success:
                                        results["manager_review_reminders"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process review reminders for cycle %s: %s",
                        cycle.cycle_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "cycle_id": str(cycle.cycle_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    total_sent = (
        results["self_assessment_reminders"]
        + results["manager_review_reminders"]
        + results["calibration_reminders"]
    )
    logger.info("Performance review reminders complete: %d sent", total_sent)

    return results


@shared_task
def process_certification_expiry_notifications() -> dict:
    """
    Send notifications for expiring employee certifications.

    Sends notifications:
    - 60 days before expiry
    - 30 days before expiry
    - 7 days before expiry

    Returns:
        Dict with notification statistics
    """
    from app.models.people.hr.employee_extended import EmployeeCertification
    from app.services.hr_notifications import HRNotificationService

    FIRST_NOTICE_DAYS = 60
    SECOND_NOTICE_DAYS = 30
    FINAL_NOTICE_DAYS = 7

    logger.info("Processing certification expiry notifications")

    results: dict[str, Any] = {
        "notifications_sent": 0,
        "errors": [],
    }

    today = date.today()

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = HRNotificationService(db)
            # Find certifications with expiry dates for this organization
            expiring_certs = db.scalars(
                select(EmployeeCertification).where(
                    EmployeeCertification.organization_id == org_id,
                    EmployeeCertification.expiry_date.isnot(None),
                    EmployeeCertification.expiry_date >= today,
                    EmployeeCertification.expiry_date
                    <= today + timedelta(days=FIRST_NOTICE_DAYS),
                )
            ).all()

            for cert in expiring_certs:
                try:
                    expiry = cert.expiry_date
                    if expiry is None:
                        continue
                    days_remaining = (expiry - today).days

                    # Only send on specific days
                    if days_remaining not in [
                        FIRST_NOTICE_DAYS,
                        SECOND_NOTICE_DAYS,
                        FINAL_NOTICE_DAYS,
                    ]:
                        continue

                    employee = db.get(Employee, cert.employee_id)
                    if not employee:
                        continue

                    # Send notification to employee
                    success = (
                        notification_service.send_certification_expiry_notification(
                            employee,
                            cert,
                            days_remaining=days_remaining,
                        )
                    )

                    if success:
                        results["notifications_sent"] += 1

                except Exception as e:
                    logger.error(
                        "Failed to process certification expiry notification for cert %s: %s",
                        cert.certification_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "certification_id": str(cert.certification_id),
                            "error": str(e),
                        }
                    )

            db.commit()

    logger.info(
        "Certification expiry notifications complete: %d sent",
        results["notifications_sent"],
    )

    return results


@shared_task
def calculate_hr_analytics(organization_id: str) -> dict:
    """
    Calculate HR analytics for reporting dashboards.

    Generates aggregate statistics for:
    - Headcount by department
    - Attrition rates
    - Average tenure
    - Upcoming reviews and expirations

    Args:
        organization_id: UUID of the organization

    Returns:
        Dict with calculated analytics
    """
    logger.info("Calculating HR analytics for org %s", organization_id)

    org_id = uuid.UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            today = date.today()

            # Get active employee count
            active_count = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        Employee.organization_id == org_id,
                        Employee.status == EmployeeStatus.ACTIVE,
                    )
                )
                or 0
            )

            # Get employees on probation
            on_probation = (
                db.scalar(
                    select(func.count(Employee.employee_id)).where(
                        Employee.organization_id == org_id,
                        Employee.status == EmployeeStatus.ACTIVE,
                        Employee.probation_end_date.isnot(None),
                        Employee.probation_end_date >= today,
                    )
                )
                or 0
            )

            # Get employees with expiring contracts (next 90 days)
            expiring_contracts = 0
            contract_end_attr = getattr(Employee, "contract_end_date", None)
            if contract_end_attr is not None:
                expiring_contracts = (
                    db.scalar(
                        select(func.count(Employee.employee_id)).where(
                            Employee.organization_id == org_id,
                            Employee.status == EmployeeStatus.ACTIVE,
                            contract_end_attr.isnot(None),
                            contract_end_attr >= today,
                            contract_end_attr <= today + timedelta(days=90),
                        )
                    )
                    or 0
                )

            # Calculate average tenure
            employees_with_joining = db.scalars(
                select(Employee).where(
                    Employee.organization_id == org_id,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.date_of_joining.isnot(None),
                )
            ).all()

            total_tenure_days = 0
            for emp in employees_with_joining:
                if emp.date_of_joining:
                    total_tenure_days += (today - emp.date_of_joining).days

            avg_tenure_years = (
                (total_tenure_days / len(employees_with_joining) / 365)
                if employees_with_joining
                else 0
            )

            return {
                "success": True,
                "organization_id": organization_id,
                "date": str(today),
                "headcount": {
                    "active_employees": active_count,
                    "on_probation": on_probation,
                    "expiring_contracts_90d": expiring_contracts,
                },
                "tenure": {
                    "avg_years": round(avg_tenure_years, 1),
                    "employees_counted": len(employees_with_joining),
                },
            }

        except Exception as e:
            logger.exception("HR analytics calculation failed: %s", e)
            return {
                "success": False,
                "error": str(e),
            }


# ==============================================================================
# Onboarding Tasks
# ==============================================================================


@shared_task
def process_onboarding_overdue_activities() -> dict:
    """
    Update overdue flags for onboarding activities across all organizations.

    Scans all pending onboarding activities and marks those past their due date
    as overdue. This task should run daily.

    Returns:
        Dict with processing statistics
    """
    from app.services.people.hr.onboarding import OnboardingService

    logger.info("Processing onboarding overdue activities")

    results: dict[str, Any] = {
        "organizations_processed": 0,
        "activities_marked_overdue": 0,
        "errors": [],
    }

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            try:
                service = OnboardingService(db)
                count = service.update_overdue_flags(org_id)

                results["organizations_processed"] += 1
                results["activities_marked_overdue"] += count

            except Exception as e:
                logger.error(
                    "Failed to process overdue activities for org %s: %s",
                    org_id,
                    e,
                )
                results["errors"].append(
                    {
                        "organization_id": str(org_id),
                        "error": str(e),
                    }
                )

            db.commit()

    logger.info(
        "Onboarding overdue processing complete: %d activities marked in %d orgs",
        results["activities_marked_overdue"],
        results["organizations_processed"],
    )

    return results


@shared_task
def process_onboarding_reminders() -> dict:
    """
    Send reminder notifications for onboarding activities.

    Sends notifications for:
    - Activities due within 2 days
    - Overdue activities (daily reminder until completed)

    Avoids duplicate reminders within 24 hours.

    Returns:
        Dict with notification statistics
    """
    from app.models.notification import (
        EntityType,
        NotificationChannel,
        NotificationType,
    )
    from app.models.people.hr.lifecycle import EmployeeOnboarding
    from app.services.notification import NotificationService
    from app.services.people.hr.onboarding import OnboardingService

    logger.info("Processing onboarding reminders")

    results: dict[str, Any] = {
        "due_soon_reminders": 0,
        "overdue_reminders": 0,
        "errors": [],
    }

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            notification_service = NotificationService()
            try:
                onboarding_service = OnboardingService(db)

                # Get activities needing reminders
                activities = onboarding_service.get_activities_needing_reminder(
                    org_id,
                    days_before_due=2,
                    remind_if_overdue=True,
                    hours_since_last_reminder=24,
                )

                for activity in activities:
                    try:
                        # Determine recipient
                        recipient_id = activity.assignee_id

                        # If no specific assignee, get from onboarding record
                        if not recipient_id:
                            onboarding = db.get(
                                EmployeeOnboarding, activity.onboarding_id
                            )
                            if onboarding:
                                # For self-service tasks, notify the employee via their person_id
                                if activity.assigned_to_employee:
                                    employee = db.get(Employee, onboarding.employee_id)
                                    if employee:
                                        recipient_id = employee.person_id
                                # For manager tasks
                                elif (
                                    activity.assignee_role == "MANAGER"
                                    and onboarding.manager_id
                                ):
                                    manager = db.get(Employee, onboarding.manager_id)
                                    if manager:
                                        recipient_id = manager.person_id
                                # For buddy tasks
                                elif (
                                    activity.assignee_role == "BUDDY"
                                    and onboarding.buddy_employee_id
                                ):
                                    buddy = db.get(
                                        Employee, onboarding.buddy_employee_id
                                    )
                                    if buddy:
                                        recipient_id = buddy.person_id

                        if not recipient_id:
                            logger.warning(
                                "No recipient found for activity %s",
                                activity.activity_id,
                            )
                            continue

                        # Determine notification type
                        is_overdue = activity.is_overdue
                        notif_type = (
                            NotificationType.OVERDUE
                            if is_overdue
                            else NotificationType.DUE_SOON
                        )

                        # Build notification message
                        if is_overdue:
                            title = f"Overdue: {activity.activity_name}"
                            message = f"The onboarding task '{activity.activity_name}' is overdue. Please complete it as soon as possible."
                        else:
                            days_remaining = (
                                (activity.due_date - date.today()).days
                                if activity.due_date
                                else 0
                            )
                            title = f"Task Due Soon: {activity.activity_name}"
                            message = f"The onboarding task '{activity.activity_name}' is due in {days_remaining} day{'s' if days_remaining != 1 else ''}."

                        # Send notification
                        notification_service.create(
                            db,
                            organization_id=org_id,
                            recipient_id=recipient_id,
                            entity_type=EntityType.SYSTEM,
                            entity_id=activity.activity_id,
                            notification_type=notif_type,
                            title=title,
                            message=message,
                            channel=NotificationChannel.BOTH,
                            action_url="/people/hr/onboarding",
                        )

                        # Mark reminder as sent
                        onboarding_service.mark_reminder_sent(activity.activity_id)

                        if is_overdue:
                            results["overdue_reminders"] += 1
                        else:
                            results["due_soon_reminders"] += 1

                    except Exception as e:
                        logger.error(
                            "Failed to send reminder for activity %s: %s",
                            activity.activity_id,
                            e,
                        )
                        results["errors"].append(
                            {
                                "activity_id": str(activity.activity_id),
                                "error": str(e),
                            }
                        )

            except Exception as e:
                logger.error(
                    "Failed to process reminders for org %s: %s",
                    org_id,
                    e,
                )
                results["errors"].append(
                    {
                        "organization_id": str(org_id),
                        "error": str(e),
                    }
                )

            db.commit()

    total_sent = results["due_soon_reminders"] + results["overdue_reminders"]
    logger.info(
        "Onboarding reminders complete: %d sent (%d due soon, %d overdue)",
        total_sent,
        results["due_soon_reminders"],
        results["overdue_reminders"],
    )

    return results


@shared_task
def sync_leave_attendance() -> dict:
    """Mark attendance as ON_LEAVE for employees with approved leave today.

    Runs daily via Celery beat. For each organisation:
    1. Find LeaveApplications where status=APPROVED and date range covers today.
    2. For each employee on leave, check if an attendance record already exists.
    3. If none exists → create with status=ON_LEAVE and link leave_application_id.
    4. If a record exists with a different status → skip (manual override respected).

    Returns:
        Dict with processing statistics.
    """
    from app.models.people.attendance.attendance import Attendance, AttendanceStatus
    from app.models.people.leave.leave_application import (
        LeaveApplication,
        LeaveApplicationStatus,
    )

    logger.info("Starting daily leave → attendance/status sync")

    results: dict[str, Any] = {
        "synced": 0,
        "already_marked": 0,
        "status_set_on_leave": 0,
        "status_set_active": 0,
        "errors": [],
    }

    for org_id in _list_organization_ids():
        with session_for_org(org_id) as db:
            try:
                from app.services.people.leave import LeaveService

                leave_service = LeaveService(db)
                org_today = leave_service.get_org_today(org_id)

                # Find approved leave applications covering today
                approved_leaves = db.scalars(
                    select(LeaveApplication).where(
                        LeaveApplication.organization_id == org_id,
                        LeaveApplication.status == LeaveApplicationStatus.APPROVED,
                        LeaveApplication.from_date <= org_today,
                        LeaveApplication.to_date >= org_today,
                    )
                ).all()

                for leave in approved_leaves:
                    sync_outcome: str | None = None
                    savepoint_started = False
                    try:
                        with db.begin_nested():
                            savepoint_started = True
                            # Check if attendance record already exists
                            existing = db.scalar(
                                select(Attendance).where(
                                    Attendance.employee_id == leave.employee_id,
                                    Attendance.attendance_date == org_today,
                                )
                            )

                            if existing:
                                sync_outcome = "already_marked"
                            else:
                                # Create ON_LEAVE attendance record
                                attendance = Attendance(
                                    organization_id=org_id,
                                    employee_id=leave.employee_id,
                                    attendance_date=org_today,
                                    status=AttendanceStatus.ON_LEAVE,
                                    leave_application_id=leave.application_id,
                                    marked_by="SYSTEM",
                                )
                                db.add(attendance)
                                db.flush()
                                sync_outcome = "synced"
                        if sync_outcome == "already_marked":
                            results["already_marked"] += 1
                        elif sync_outcome == "synced":
                            results["synced"] += 1

                    except Exception as e:
                        logger.exception(
                            "Failed to sync attendance for employee %s, leave %s: %s",
                            leave.employee_id,
                            leave.application_id,
                            e,
                        )
                        results["errors"].append(
                            {
                                "employee_id": str(leave.employee_id),
                                "leave_id": str(leave.application_id),
                                "error": str(e),
                            }
                        )
                        if not savepoint_started:
                            raise

                status_savepoint_started = False
                try:
                    with db.begin_nested():
                        status_savepoint_started = True
                        status_result = leave_service.sync_employee_statuses_for_date(
                            org_id,
                            as_of_date=org_today,
                        )
                    results["status_set_on_leave"] += status_result["set_on_leave"]
                    results["status_set_active"] += status_result["set_active"]
                except Exception as e:
                    logger.exception(
                        "Failed to sync employee leave statuses for org %s: %s",
                        org_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "organization_id": str(org_id),
                            "phase": "employee_status",
                            "error": str(e),
                        }
                    )
                    if not status_savepoint_started:
                        raise

            except Exception as e:
                logger.exception(
                    "Failed to process leave sync for org %s: %s",
                    org_id,
                    e,
                )
                results["errors"].append(
                    {
                        "organization_id": str(org_id),
                        "error": str(e),
                    }
                )
                db.rollback()
            else:
                try:
                    db.commit()
                except Exception as e:
                    logger.exception(
                        "Failed to commit leave sync for org %s: %s",
                        org_id,
                        e,
                    )
                    results["errors"].append(
                        {
                            "organization_id": str(org_id),
                            "phase": "commit",
                            "error": str(e),
                        }
                    )
                    db.rollback()

    logger.info(
        "Leave → attendance/status sync complete: %d attendance synced, %d already marked, "
        "%d status->ON_LEAVE, %d status->ACTIVE, %d errors",
        results["synced"],
        results["already_marked"],
        results["status_set_on_leave"],
        results["status_set_active"],
        len(results["errors"]),
    )

    return results


@shared_task
def send_welcome_email(onboarding_id: str) -> dict:
    """
    Send welcome email to a new hire with self-service portal link.

    Args:
        onboarding_id: UUID of the onboarding record

    Returns:
        Dict with result status
    """
    import os

    from app.models.email_profile import EmailModule
    from app.models.people.hr.lifecycle import EmployeeOnboarding
    from app.services.email import employee_can_receive_email, send_email
    from app.services.people.hr.onboarding import OnboardingService

    logger.info("Sending welcome email for onboarding %s", onboarding_id)

    # Mode 3 — resolve the onboarding's owning org under cross-org bypass,
    # then re-fetch + send under a session primed for that org.
    onboarding_uuid = uuid.UUID(onboarding_id)
    with cross_org_session() as cross_db:
        onboarding = cross_db.get(EmployeeOnboarding, onboarding_uuid)
        if not onboarding:
            return {"success": False, "error": "Onboarding not found"}
        owning_org_id = onboarding.organization_id

    with session_for_org(owning_org_id) as db:
        try:
            onboarding = db.get(EmployeeOnboarding, onboarding_uuid)
            if not onboarding:
                return {"success": False, "error": "Onboarding not found"}

            if onboarding.self_service_email_sent:
                return {"success": True, "message": "Email already sent"}

            # Get employee details
            employee = db.get(Employee, onboarding.employee_id)
            if not employee or not employee.person:
                return {"success": False, "error": "Employee or person not found"}

            if not employee_can_receive_email(employee):
                return {
                    "success": False,
                    "error": f"Employee {employee.employee_id} is inactive",
                }

            person = employee.person
            if not person.email:
                return {"success": False, "error": "No email address"}

            # Get organization
            org = db.get(Organization, onboarding.organization_id)
            if not org:
                return {"success": False, "error": "Organization not found"}

            # Generate a fresh token for the URL
            # SECURITY: Token is stored as hash, so we regenerate to get the raw token
            service = OnboardingService(db)
            raw_token = service.regenerate_self_service_token(
                onboarding.organization_id, onboarding.onboarding_id
            )

            # Build portal URL with raw token
            app_url = os.getenv("APP_URL", "http://localhost:8000")
            portal_url = f"{app_url.rstrip('/')}/onboarding/start/{raw_token}"

            # Build email content
            employee_name = (
                person.display_name or f"{person.first_name} {person.last_name}"
            )
            org_name = org.legal_name
            start_date = (
                onboarding.date_of_joining.strftime("%B %d, %Y")
                if onboarding.date_of_joining
                else "TBD"
            )

            subject = f"Welcome to {org_name} - Complete Your Onboarding"
            body_html = f"""
            <p>Dear {employee_name},</p>

            <p>Welcome to <strong>{org_name}</strong>! We're excited to have you join our team.</p>

            <p>Your start date is <strong>{start_date}</strong>.</p>

            <p>To complete your onboarding tasks, please access your personal onboarding portal:</p>

            <p><a href="{portal_url}" style="background-color: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Access Onboarding Portal</a></p>

            <p>Through the portal, you'll be able to:</p>
            <ul>
                <li>Complete required forms and documents</li>
                <li>Upload necessary paperwork</li>
                <li>View your onboarding checklist and progress</li>
                <li>Access company information</li>
            </ul>

            <p>If you have any questions, please don't hesitate to reach out to HR.</p>

            <p>We look forward to seeing you soon!</p>

            <p>Best regards,<br>
            Human Resources<br>
            {org_name}</p>
            """

            body_text = f"""
Dear {employee_name},

Welcome to {org_name}! We're excited to have you join our team.

Your start date is {start_date}.

To complete your onboarding tasks, please access your personal onboarding portal:
{portal_url}

Through the portal, you'll be able to:
- Complete required forms and documents
- Upload necessary paperwork
- View your onboarding checklist and progress
- Access company information

If you have any questions, please don't hesitate to reach out to HR.

We look forward to seeing you soon!

Best regards,
Human Resources
{org_name}
            """

            # Send email
            success = send_email(
                db=db,
                to_email=person.email,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                module=EmailModule.PEOPLE_PAYROLL,
                organization_id=onboarding.organization_id,
            )

            if success:
                # Mark email as sent
                onboarding_service = OnboardingService(db)
                onboarding_service.mark_welcome_email_sent(
                    onboarding.organization_id,
                    onboarding.onboarding_id,
                )
                db.commit()

                logger.info(
                    "Welcome email sent to %s for onboarding %s",
                    person.email,
                    onboarding_id,
                )

                return {"success": True, "email": person.email}

            return {"success": False, "error": "Failed to send email"}

        except Exception as e:
            logger.exception("Failed to send welcome email: %s", e)
            return {"success": False, "error": str(e)}
