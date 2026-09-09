from __future__ import annotations

import uuid
from datetime import date, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from app.models.finance.core_org.organization import Organization
from app.models.finance.platform.event_outbox import EventOutbox
from app.models.people.hr import (
    Employee,
    EmployeeStatus,
    StaffAccountStatusProjection,
    StaffAccountStatusState,
    StaffLeaveAccessRestriction,
    StaffLeaveRestrictionStatus,
)
from app.models.people.leave.leave_application import (
    LeaveApplication,
    LeaveApplicationStatus,
)
from app.models.people.leave.leave_type import LeaveType
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.services.auth_dependencies import _enforce_staff_leave_write_restriction
from app.services.people.hr.staff_access_projection import (
    STAFF_ACCOUNT_STATUS_CHANGED,
    STAFF_LEAVE_RESTRICTION_CHANGED,
    StaffAccessProjectionService,
    is_mutating_permission_key,
)
from app.web.deps import WebAuthContext


def _strip_server_defaults(*tables) -> None:
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            default_text = str(getattr(default, "arg", default)).lower()
            if "gen_random_uuid" in default_text or "uuid_generate" in default_text:
                column.server_default = None


def _ensure_tables(engine) -> None:
    tables = (
        Organization.__table__,
        Person.__table__,
        Employee.__table__,
        LeaveType.__table__,
        LeaveApplication.__table__,
        StaffLeaveAccessRestriction.__table__,
        StaffAccountStatusProjection.__table__,
        EventOutbox.__table__,
        Role.__table__,
        Permission.__table__,
        PersonRole.__table__,
        RolePermission.__table__,
    )
    _strip_server_defaults(*tables)
    for table in tables:
        table.create(engine, checkfirst=True)


@pytest.fixture(autouse=True)
def staff_access_tables(db_session):
    _ensure_tables(db_session.bind)


def _org(db_session, *, timezone_name: str = "UTC") -> Organization:
    organization = Organization(
        organization_id=uuid.uuid4(),
        organization_code=f"ORG-{uuid.uuid4().hex[:8]}",
        legal_name="Projection Test Org",
        functional_currency_code="NGN",
        presentation_currency_code="NGN",
        fiscal_year_end_month=12,
        fiscal_year_end_day=31,
        timezone=timezone_name,
    )
    db_session.add(organization)
    return organization


def _employee(
    db_session,
    organization_id: uuid.UUID,
    *,
    status: EmployeeStatus = EmployeeStatus.ACTIVE,
    selfcare_user_id: str | None = None,
) -> tuple[Person, Employee]:
    person = Person(
        id=uuid.uuid4(),
        organization_id=organization_id,
        first_name="Staff",
        last_name="User",
        email=f"staff-{uuid.uuid4().hex}@example.com",
    )
    employee = Employee(
        employee_id=uuid.uuid4(),
        organization_id=organization_id,
        person_id=person.id,
        employee_code=f"EMP-{uuid.uuid4().hex[:8]}",
        date_of_joining=date(2025, 1, 1),
        status=status,
        dotmac_sub_account_id=selfcare_user_id,
        dotmac_sub_access_enabled=selfcare_user_id is not None,
        dotmac_sub_roles=["staff"],
    )
    db_session.add_all([person, employee])
    return person, employee


def _leave(
    db_session,
    organization_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    from_date: date = date(2026, 1, 10),
    to_date: date = date(2026, 1, 20),
    status: LeaveApplicationStatus = LeaveApplicationStatus.APPROVED,
) -> LeaveApplication:
    application = LeaveApplication(
        application_id=uuid.uuid4(),
        organization_id=organization_id,
        application_number=f"LVE-{uuid.uuid4().hex[:8]}",
        employee_id=employee_id,
        leave_type_id=uuid.uuid4(),
        from_date=from_date,
        to_date=to_date,
        total_leave_days=Decimal("1.0"),
        status=status,
    )
    db_session.add(application)
    return application


def _outbox_events(db_session, event_name: str) -> list[EventOutbox]:
    events = list(
        db_session.scalars(
            select(EventOutbox).where(EventOutbox.event_name == event_name)
        ).all()
    )
    return sorted(
        events,
        key=lambda event: (event.payload.get("version", 0), str(event.event_id)),
    )


def test_approved_leave_creates_active_versioned_restriction_and_outbox(db_session):
    org = _org(db_session)
    person, employee = _employee(
        db_session,
        org.organization_id,
        selfcare_user_id=str(uuid.uuid4()),
    )
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()

    restriction = StaffAccessProjectionService(db_session).project_leave_application(
        application
    )

    assert restriction.status == StaffLeaveRestrictionStatus.ACTIVE
    assert restriction.person_id == person.id
    assert restriction.selfcare_user_id == employee.dotmac_sub_account_id
    assert restriction.effective_from == date(2026, 1, 10)
    assert restriction.effective_until == date(2026, 1, 20)
    assert restriction.version == 1

    event = _outbox_events(db_session, STAFF_LEAVE_RESTRICTION_CHANGED)[-1]
    assert event.idempotency_key.endswith(f":v{restriction.version}")
    assert event.payload["restriction_id"] == str(restriction.restriction_id)
    assert event.payload["status"] == "ACTIVE"
    assert event.payload["version"] == 1
    assert event.payload["selfcare_user_id"] == employee.dotmac_sub_account_id


def test_non_approved_leave_projection_does_not_create_active_restriction(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    application = _leave(
        db_session,
        org.organization_id,
        employee.employee_id,
        status=LeaveApplicationStatus.SUBMITTED,
    )
    db_session.flush()

    restriction = StaffAccessProjectionService(db_session).project_leave_application(
        application
    )

    assert restriction is None
    assert (
        StaffAccessProjectionService(db_session).active_restriction_for_person(
            org.organization_id,
            person.id,
            as_of_date=date(2026, 1, 12),
        )
        is None
    )
    assert _outbox_events(db_session, STAFF_LEAVE_RESTRICTION_CHANGED) == []


def test_restriction_uses_date_range_and_expires_without_restore_job(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()
    service = StaffAccessProjectionService(db_session)
    service.project_leave_application(application)

    assert (
        service.active_restriction_for_person(
            org.organization_id,
            person.id,
            as_of_date=date(2026, 1, 10),
        )
        is not None
    )
    assert (
        service.active_restriction_for_person(
            org.organization_id,
            person.id,
            as_of_date=date(2026, 1, 21),
        )
        is None
    )


def test_cancellation_versions_and_deactivates_existing_restriction(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()
    service = StaffAccessProjectionService(db_session)
    service.project_leave_application(application)

    application.status = LeaveApplicationStatus.CANCELLED
    cancelled = service.project_leave_application(
        application,
        reason="employee_cancelled",
    )

    assert cancelled.version == 2
    assert cancelled.status == StaffLeaveRestrictionStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert cancelled.cancellation_reason == "employee_cancelled"
    assert (
        service.active_restriction_for_person(
            org.organization_id,
            person.id,
            as_of_date=date(2026, 1, 12),
        )
        is None
    )
    events = _outbox_events(db_session, STAFF_LEAVE_RESTRICTION_CHANGED)
    assert events[-1].payload["status"] == "CANCELLED"
    assert events[-1].payload["version"] == 2


def test_approved_leave_period_changes_advance_versions(db_session):
    org = _org(db_session)
    _, employee = _employee(db_session, org.organization_id)
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()
    service = StaffAccessProjectionService(db_session)

    restriction = service.project_leave_application(application)
    assert restriction.version == 1

    application.to_date = date(2026, 1, 15)
    shortened = service.project_leave_application(application)
    assert shortened.version == 2
    assert shortened.effective_until == date(2026, 1, 15)

    application.to_date = date(2026, 1, 25)
    extended = service.project_leave_application(application)
    assert extended.version == 3
    assert extended.effective_until == date(2026, 1, 25)

    application.from_date = date(2026, 1, 12)
    moved = service.project_leave_application(application)
    assert moved.version == 4
    assert moved.effective_from == date(2026, 1, 12)
    assert (
        _outbox_events(db_session, STAFF_LEAVE_RESTRICTION_CHANGED)[-1].payload[
            "version"
        ]
        == 4
    )


def test_overlapping_leaves_are_deterministic_and_independently_versioned(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    first = _leave(
        db_session,
        org.organization_id,
        employee.employee_id,
        from_date=date(2026, 1, 10),
        to_date=date(2026, 1, 15),
    )
    second = _leave(
        db_session,
        org.organization_id,
        employee.employee_id,
        from_date=date(2026, 1, 15),
        to_date=date(2026, 1, 25),
    )
    db_session.flush()
    service = StaffAccessProjectionService(db_session)
    first_restriction = service.project_leave_application(first)
    second_restriction = service.project_leave_application(second)

    active = service.active_restriction_for_person(
        org.organization_id,
        person.id,
        as_of_date=date(2026, 1, 15),
    )

    assert active is not None
    assert active.restriction_id == second_restriction.restriction_id
    assert first_restriction.restriction_id != second_restriction.restriction_id
    assert first_restriction.version == 1
    assert second_restriction.version == 1


def test_transaction_rollback_does_not_publish_committed_projection(db_session):
    org = _org(db_session)
    _, employee = _employee(db_session, org.organization_id)
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()

    StaffAccessProjectionService(db_session).project_leave_application(application)
    db_session.rollback()

    assert db_session.scalar(select(StaffLeaveAccessRestriction)) is None
    assert db_session.scalar(select(EventOutbox)) is None


def test_employee_account_status_projection_is_erp_owned_and_versioned(db_session):
    org = _org(db_session)
    _, employee = _employee(
        db_session,
        org.organization_id,
        status=EmployeeStatus.ACTIVE,
        selfcare_user_id=str(uuid.uuid4()),
    )
    db_session.flush()
    service = StaffAccessProjectionService(db_session)

    active = service.project_employee_account_status(employee)
    assert active.state == StaffAccountStatusState.ACTIVE
    assert active.version == 1

    employee.status = EmployeeStatus.SUSPENDED
    inactive = service.project_employee_account_status(employee)
    assert inactive.state == StaffAccountStatusState.INACTIVE
    assert inactive.version == 2

    employee.status = EmployeeStatus.ACTIVE
    reactivated = service.project_employee_account_status(employee)
    assert reactivated.state == StaffAccountStatusState.ACTIVE
    assert reactivated.version == 3

    event = _outbox_events(db_session, STAFF_ACCOUNT_STATUS_CHANGED)[-1]
    assert event.payload["ownership"] == "erp_employee_status"
    assert event.payload["state"] == "ACTIVE"
    assert "independent local suspensions" in event.payload["downstream_semantics"]


def test_selfcare_mapping_changes_are_projected_without_hardcoded_identity(db_session):
    org = _org(db_session)
    _, employee = _employee(db_session, org.organization_id, selfcare_user_id=None)
    db_session.flush()
    service = StaffAccessProjectionService(db_session)

    initial = service.project_employee_account_status(employee)
    assert initial.selfcare_user_id is None

    employee.dotmac_sub_account_id = str(uuid.uuid4())
    mapped = service.project_employee_account_status(employee)

    assert mapped.selfcare_user_id == employee.dotmac_sub_account_id
    assert mapped.version == 2


def test_permission_classifier_blocks_mutations_but_not_reads() -> None:
    assert is_mutating_permission_key("ap:invoices:create") is True
    assert is_mutating_permission_key("hr:leave:approve") is True
    assert is_mutating_permission_key("ap:invoices:*") is True
    assert is_mutating_permission_key("ap:invoices:read") is False
    assert is_mutating_permission_key("reports:export") is False
    assert is_mutating_permission_key("search") is False


def test_api_leave_guard_blocks_write_permissions_but_allows_reads(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    application = _leave(
        db_session,
        org.organization_id,
        employee.employee_id,
        from_date=date.today() - timedelta(days=1),
        to_date=date.today() + timedelta(days=1),
    )
    db_session.flush()
    StaffAccessProjectionService(db_session).project_leave_application(application)
    auth = {
        "organization_id": str(org.organization_id),
        "person_id": str(person.id),
    }

    _enforce_staff_leave_write_restriction(db_session, auth, "ap:invoices:read")
    with pytest.raises(HTTPException) as exc_info:
        _enforce_staff_leave_write_restriction(
            db_session,
            auth,
            "ap:invoices:create",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "staff_on_leave_read_only"


def test_web_auth_context_preserves_admin_reads_and_denies_writes_on_leave() -> None:
    restriction_id = uuid.uuid4()
    auth = WebAuthContext(
        is_authenticated=True,
        person_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        roles=["admin"],
        scopes=[],
        leave_write_restricted=True,
        leave_restriction_id=restriction_id,
    )

    assert auth.has_permission("ap:invoices:read") is True
    assert auth.has_permission("ap:invoices:create") is False
    assert auth.has_any_permission(["ap:invoices:create", "ap:invoices:read"]) is True
    assert auth.leave_restriction_id == restriction_id


def test_projecting_restrictions_does_not_mutate_role_assignments(db_session):
    org = _org(db_session)
    person, employee = _employee(db_session, org.organization_id)
    role = Role(id=uuid.uuid4(), name=f"role-{uuid.uuid4().hex[:8]}", is_active=True)
    permission = Permission(
        id=uuid.uuid4(),
        key=f"test:{uuid.uuid4().hex}:create",
        description="Mutation permission",
        is_active=True,
    )
    db_session.add_all(
        [
            role,
            permission,
            PersonRole(person_id=person.id, role_id=role.id),
            RolePermission(role_id=role.id, permission_id=permission.id),
        ]
    )
    application = _leave(db_session, org.organization_id, employee.employee_id)
    db_session.flush()

    StaffAccessProjectionService(db_session).project_leave_application(application)

    assert db_session.scalar(
        select(PersonRole).where(
            PersonRole.person_id == person.id,
            PersonRole.role_id == role.id,
        )
    )
    assert db_session.scalar(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission_id == permission.id,
        )
    )
