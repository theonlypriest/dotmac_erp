from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeStatus,
    Position,
    PositionAssignment,
    PositionAssignmentType,
    PositionVacancyRoutingPolicy,
)
from app.models.person import Person
from app.services.common import ConflictError, PaginationParams
from app.services.people.hr import EmployeeFilters, EmployeeService, EmployeeUpdateData
from app.services.people.hr.errors import InvalidManagerError
from app.services.people.hr.employee_filter_contract import FilterExpression
from app.services.people.hr.org_resolver import OrgResolver
from app.services.people.hr.positions import (
    OrgChartNode,
    PositionAssignmentCreateData,
    PositionCreateData,
    PositionUpdateData,
    PositionService,
    ReconcileResult,
)
from app.services.people.leave.web import LeaveWebService


def _ensure_hr_position_tables(engine) -> None:
    tables = (
        Department.__table__,
        Designation.__table__,
        Employee.__table__,
        Position.__table__,
        PositionAssignment.__table__,
    )
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            default_text = str(getattr(default, "arg", default)).lower()
            if "gen_random_uuid" in default_text or "uuid_generate" in default_text:
                column.server_default = None
        table.create(engine, checkfirst=True)


def _make_employee(db_session, org_id: uuid.UUID, code: str) -> Employee:
    person = Person(
        id=uuid.uuid4(),
        organization_id=org_id,
        first_name=code,
        last_name="Employee",
        email=f"{uuid.uuid4().hex}@example.com",
    )
    employee = Employee(
        employee_id=uuid.uuid4(),
        organization_id=org_id,
        person_id=person.id,
        employee_code=code,
        date_of_joining=date(2026, 1, 1),
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add_all([person, employee])
    db_session.flush()
    return employee


def _make_position(
    db_session,
    org_id: uuid.UUID,
    *,
    position_code: str | None = None,
    position_name: str | None = None,
    department_id: uuid.UUID | None = None,
    parent_position_id: uuid.UUID | None = None,
    is_vacant: bool = True,
    vacancy_routing_policy: PositionVacancyRoutingPolicy = (
        PositionVacancyRoutingPolicy.SKIP_UP
    ),
) -> Position:
    short_id = uuid.uuid4().hex[:8].upper()
    position = Position(
        position_id=uuid.uuid4(),
        organization_id=org_id,
        position_code=position_code or f"POS-{short_id}",
        position_name=position_name or f"Position {short_id}",
        department_id=department_id,
        parent_position_id=parent_position_id,
        vacancy_routing_policy=vacancy_routing_policy,
        is_vacant=is_vacant,
    )
    db_session.add(position)
    db_session.flush()
    return position


def _assignment_data(
    employee: Employee,
    *,
    assignment_type: PositionAssignmentType = PositionAssignmentType.PRIMARY,
    start_date: date = date(2026, 5, 1),
    end_date: date | None = None,
) -> PositionAssignmentCreateData:
    return PositionAssignmentCreateData(
        employee_id=employee.employee_id,
        assignment_type=assignment_type,
        start_date=start_date,
        end_date=end_date,
    )


def test_create_primary_assignment_marks_position_not_vacant(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    position = _make_position(db_session, org_id)

    assignment = PositionService(db_session, org_id).create_assignment(
        position.position_id,
        _assignment_data(employee),
    )

    assert assignment.position_id == position.position_id
    assert assignment.employee_id == employee.employee_id
    assert position.is_vacant is False


def test_future_assignment_keeps_position_vacant_until_start_date(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    position = _make_position(db_session, org_id)

    PositionService(db_session, org_id).create_assignment(
        position.position_id,
        _assignment_data(employee, start_date=date(2099, 1, 1)),
    )

    assert position.is_vacant is True


def test_position_update_recalculates_vacancy_from_assignments(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    position = _make_position(db_session, org_id, is_vacant=False)

    PositionService(db_session, org_id).update_position(
        position.position_id,
        PositionUpdateData(),
    )

    assert position.is_vacant is True


def test_create_position_stores_position_identity(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()

    position = PositionService(db_session, org_id).create_position(
        PositionCreateData(
            position_code="fin-mgr-01",
            position_name="Finance Manager",
        )
    )

    assert position.position_code == "FIN-MGR-01"
    assert position.position_name == "Finance Manager"
    assert position.is_vacant is True


def test_create_position_rejects_duplicate_position_code(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    service = PositionService(db_session, org_id)
    service.create_position(
        PositionCreateData(
            position_code="OPS-001",
            position_name="Operations Lead",
        )
    )

    with pytest.raises(ConflictError, match="Position code already exists"):
        service.create_position(
            PositionCreateData(
                position_code="ops-001",
                position_name="Operations Lead Backup",
            )
        )


def test_primary_assignment_rejects_active_primary_for_same_position(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    first_employee = _make_employee(db_session, org_id, "EMP-001")
    second_employee = _make_employee(db_session, org_id, "EMP-002")
    position = _make_position(db_session, org_id)
    service = PositionService(db_session, org_id)
    service.create_assignment(position.position_id, _assignment_data(first_employee))

    with pytest.raises(
        ConflictError,
        match="position already has an active primary assignment",
    ):
        service.create_assignment(
            position.position_id, _assignment_data(second_employee)
        )


def test_primary_assignment_rejects_active_primary_for_same_employee(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    first_position = _make_position(db_session, org_id)
    second_position = _make_position(db_session, org_id)
    service = PositionService(db_session, org_id)
    service.create_assignment(first_position.position_id, _assignment_data(employee))

    with pytest.raises(
        ConflictError,
        match="employee already has an active primary position",
    ):
        service.create_assignment(
            second_position.position_id, _assignment_data(employee)
        )


def test_acting_assignment_allowed_alongside_primary(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    primary_employee = _make_employee(db_session, org_id, "EMP-001")
    acting_employee = _make_employee(db_session, org_id, "EMP-002")
    position = _make_position(db_session, org_id)
    service = PositionService(db_session, org_id)
    service.create_assignment(position.position_id, _assignment_data(primary_employee))

    acting = service.create_assignment(
        position.position_id,
        _assignment_data(
            acting_employee,
            assignment_type=PositionAssignmentType.ACTING,
        ),
    )

    assert acting.assignment_type == PositionAssignmentType.ACTING
    assert position.is_vacant is False


def test_end_last_active_assignment_marks_position_vacant(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    position = _make_position(db_session, org_id)
    service = PositionService(db_session, org_id)
    assignment = service.create_assignment(
        position.position_id, _assignment_data(employee)
    )

    service.end_assignment(
        position.position_id,
        assignment.position_assignment_id,
        end_date=date(2026, 5, 2),
    )

    assert assignment.end_date == date(2026, 5, 2)
    assert position.is_vacant is True


def test_org_resolver_direct_reports_rolls_up_vacant_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=True,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(
        manager_position.position_id,
        _assignment_data(manager),
    )
    service.create_assignment(
        employee_position.position_id,
        _assignment_data(employee),
    )

    reports = OrgResolver(db_session).get_direct_reports(
        manager.employee_id,
        org_id,
    )

    assert [report.employee_id for report in reports] == [employee.employee_id]


def test_block_vacancy_policy_stops_manager_rollup(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    director = _make_employee(db_session, org_id, "DIR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    director_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=director_position.position_id,
        is_vacant=True,
        vacancy_routing_policy=PositionVacancyRoutingPolicy.BLOCK,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    manager = OrgResolver(db_session).get_manager(employee.employee_id, org_id)
    chain = OrgResolver(db_session).get_approval_chain(employee.employee_id, org_id)

    assert manager is None
    assert chain == []


def test_notify_hr_vacancy_policy_skips_and_records_alert(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    director = _make_employee(db_session, org_id, "DIR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    director_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        position_code="MGR-VACANT",
        position_name="Vacant Manager",
        parent_position_id=director_position.position_id,
        is_vacant=True,
        vacancy_routing_policy=PositionVacancyRoutingPolicy.NOTIFY_HR_THEN_SKIP,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))
    resolver = OrgResolver(db_session)

    manager = resolver.get_manager(employee.employee_id, org_id)
    alerts = resolver.drain_vacancy_routing_alerts()

    assert manager is not None
    assert manager.employee_id == director.employee_id
    assert len(alerts) == 1
    assert alerts[0].position_id == vacant_position.position_id
    assert alerts[0].position_code == "MGR-VACANT"


def test_block_vacancy_policy_stops_direct_report_roll_down(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    director = _make_employee(db_session, org_id, "DIR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    director_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=director_position.position_id,
        is_vacant=True,
        vacancy_routing_policy=PositionVacancyRoutingPolicy.BLOCK,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    reports = OrgResolver(db_session).get_direct_reports(director.employee_id, org_id)

    assert reports == []


def test_employee_service_direct_reports_uses_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    legacy_report = _make_employee(db_session, org_id, "EMP-LEGACY")
    legacy_report.reports_to_id = manager.employee_id
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    reports = EmployeeService(db_session, org_id).get_direct_reports(
        manager.employee_id
    )

    assert [report.employee_id for report in reports] == [employee.employee_id]
    assert legacy_report.employee_id not in {report.employee_id for report in reports}


def test_employee_list_reports_to_filter_uses_positions_not_legacy_column(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    legacy_report = _make_employee(db_session, org_id, "EMP-LEGACY")
    legacy_report.reports_to_id = manager.employee_id
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    result = EmployeeService(db_session, org_id).list_employees(
        EmployeeFilters(reports_to_id=manager.employee_id),
        PaginationParams(limit=10),
    )

    assert [item.employee_id for item in result.items] == [employee.employee_id]
    assert legacy_report.employee_id not in {item.employee_id for item in result.items}


def test_employee_list_reports_to_filter_rolls_down_vacant_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=True,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    result = EmployeeService(db_session, org_id).list_employees(
        EmployeeFilters(reports_to_id=manager.employee_id),
        PaginationParams(limit=10),
    )

    assert [item.employee_id for item in result.items] == [employee.employee_id]


def test_employee_advanced_reports_to_filter_uses_positions_not_legacy_column(
    db_session,
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    legacy_report = _make_employee(db_session, org_id, "EMP-LEGACY")
    legacy_report.reports_to_id = manager.employee_id
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    expression = FilterExpression.parse_payload(
        [["Employee", "reports_to_id", "=", str(manager.employee_id)]]
    )
    result = EmployeeService(db_session, org_id).list_employees(
        advanced_filter_expression=expression,
        pagination=PaginationParams(limit=10),
    )

    assert [item.employee_id for item in result.items] == [employee.employee_id]
    assert legacy_report.employee_id not in {item.employee_id for item in result.items}


def test_employee_advanced_reports_to_filter_rolls_down_vacant_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=True,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    expression = FilterExpression.parse_payload(
        [["Employee", "reports_to_id", "=", str(manager.employee_id)]]
    )
    result = EmployeeService(db_session, org_id).list_employees(
        advanced_filter_expression=expression,
        pagination=PaginationParams(limit=10),
    )

    assert [item.employee_id for item in result.items] == [employee.employee_id]


def test_leave_employee_options_use_position_manager_not_legacy_column(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    legacy_manager = _make_employee(db_session, org_id, "MGR-LEGACY")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))
    employee.reports_to_id = legacy_manager.employee_id
    db_session.flush()

    options = LeaveWebService._get_employees(db_session, org_id)
    option = next(item for item in options if item.employee_id == employee.employee_id)

    assert option.resolved_manager_id == manager.employee_id
    assert option.resolved_manager_name == manager.full_name


def test_employee_initial_position_assignment_uses_existing_position(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    position = _make_position(db_session, org_id, is_vacant=True)

    EmployeeService(db_session, org_id)._assign_initial_position(
        employee,
        position.position_id,
    )

    assignment = OrgResolver(db_session).get_active_assignment(
        employee.employee_id,
        org_id,
    )
    assert assignment is not None
    assert assignment.position_id == position.position_id
    assert position.is_vacant is False


def test_employee_reports_to_update_syncs_position_parent(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(db_session, org_id, is_vacant=False)
    position_service = PositionService(db_session, org_id)
    position_service.create_assignment(
        manager_position.position_id,
        _assignment_data(manager),
    )
    position_service.create_assignment(
        employee_position.position_id,
        _assignment_data(employee),
    )

    EmployeeService(db_session, org_id).update_employee(
        employee.employee_id,
        EmployeeUpdateData(reports_to_id=manager.employee_id),
    )

    assert employee.reports_to_id == manager.employee_id
    assert employee_position.parent_position_id == manager_position.position_id
    resolved = OrgResolver(db_session).get_manager(employee.employee_id, org_id)
    assert resolved is not None and resolved.employee_id == manager.employee_id


def test_employee_department_update_syncs_active_primary_position(
    db_session, monkeypatch
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    previous_department = Department(
        department_id=uuid.uuid4(),
        organization_id=org_id,
        department_code="OLD",
        department_name="Old Department",
    )
    new_department = Department(
        department_id=uuid.uuid4(),
        organization_id=org_id,
        department_code="NEW",
        department_name="New Department",
    )
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.department_id = previous_department.department_id
    position = _make_position(
        db_session,
        org_id,
        department_id=previous_department.department_id,
        is_vacant=False,
    )
    db_session.add_all([previous_department, new_department])
    db_session.flush()
    monkeypatch.setattr(
        "app.services.people.scheduling.SchedulingService.sync_employee_department",
        lambda self, **kwargs: {"assignments_updated": 0, "assignments_ended": 0},
    )
    PositionService(db_session, org_id).create_assignment(
        position.position_id,
        _assignment_data(employee),
    )

    EmployeeService(db_session, org_id).update_employee(
        employee.employee_id,
        EmployeeUpdateData(department_id=new_department.department_id),
    )

    assert employee.department_id == new_department.department_id
    assert position.department_id == new_department.department_id


def test_employee_manager_clear_syncs_position_parent_and_legacy_cache(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(db_session, org_id, is_vacant=False)
    position_service = PositionService(db_session, org_id)
    position_service.create_assignment(
        manager_position.position_id,
        _assignment_data(manager),
    )
    position_service.create_assignment(
        employee_position.position_id,
        _assignment_data(employee),
    )
    employee.reports_to_id = manager.employee_id
    employee_position.parent_position_id = manager_position.position_id
    db_session.flush()

    data = EmployeeUpdateData(reports_to_id=None)
    data.provided_fields = {"reports_to_id"}
    EmployeeService(db_session, org_id).update_employee(employee.employee_id, data)

    assert employee.reports_to_id is None
    assert employee_position.parent_position_id is None
    assert OrgResolver(db_session).get_manager(employee.employee_id, org_id) is None


def test_employee_update_retains_unchanged_inactive_employment_type(
    db_session, monkeypatch
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    current_employment_type_id = uuid.uuid4()
    employee.employment_type_id = current_employment_type_id
    service = EmployeeService(db_session, org_id)
    monkeypatch.setattr(
        service,
        "get_employee",
        lambda _employee_id: employee,
    )

    class _UnexpectedOwner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("unchanged current assignment must not be revalidated")

    monkeypatch.setattr(
        "app.services.people.hr.employees.EmploymentTypeService",
        _UnexpectedOwner,
    )

    data = EmployeeUpdateData(
        employment_type_id=current_employment_type_id,
        provided_fields={"employment_type_id"},
    )
    updated = service.update_employee(employee.employee_id, data)

    assert updated.employment_type_id == current_employment_type_id


def test_employee_update_requires_active_when_employment_type_changes(
    db_session, monkeypatch
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.employment_type_id = uuid.uuid4()
    requested_employment_type_id = uuid.uuid4()
    required: list[uuid.UUID] = []
    service = EmployeeService(db_session, org_id)
    monkeypatch.setattr(
        service,
        "get_employee",
        lambda _employee_id: employee,
    )

    class _Owner:
        def __init__(self, db, organization_id):
            assert db is db_session
            assert organization_id == org_id

        def require_active(self, employment_type_id):
            required.append(employment_type_id)

    monkeypatch.setattr(
        "app.services.people.hr.employees.EmploymentTypeService",
        _Owner,
    )

    data = EmployeeUpdateData(
        employment_type_id=requested_employment_type_id,
        provided_fields={"employment_type_id"},
    )
    updated = service.update_employee(employee.employee_id, data)

    assert required == [requested_employment_type_id]
    assert updated.employment_type_id == requested_employment_type_id


def test_employee_update_omitted_employment_type_preserves_current_assignment(
    db_session, monkeypatch
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    current_employment_type_id = uuid.uuid4()
    employee.employment_type_id = current_employment_type_id
    service = EmployeeService(db_session, org_id)
    monkeypatch.setattr(
        service,
        "get_employee",
        lambda _employee_id: employee,
    )

    class _UnexpectedOwner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("an omitted assignment must not validate a type")

    monkeypatch.setattr(
        "app.services.people.hr.employees.EmploymentTypeService",
        _UnexpectedOwner,
    )

    updated = service.update_employee(employee.employee_id, EmployeeUpdateData())

    assert updated.employment_type_id == current_employment_type_id


def test_employee_update_explicit_blank_clears_employment_type(db_session, monkeypatch):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.employment_type_id = uuid.uuid4()
    service = EmployeeService(db_session, org_id)
    monkeypatch.setattr(
        service,
        "get_employee",
        lambda _employee_id: employee,
    )

    class _UnexpectedOwner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("clearing an assignment must not validate a type")

    monkeypatch.setattr(
        "app.services.people.hr.employees.EmploymentTypeService",
        _UnexpectedOwner,
    )

    data = EmployeeUpdateData(
        employment_type_id=None,
        provided_fields={"employment_type_id"},
    )
    updated = service.update_employee(employee.employee_id, data)

    assert updated.employment_type_id is None


def test_primary_assignment_syncs_existing_legacy_manager_to_position_parent(
    db_session,
):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.reports_to_id = manager.employee_id
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(db_session, org_id, is_vacant=True)
    position_service = PositionService(db_session, org_id)
    position_service.create_assignment(
        manager_position.position_id,
        _assignment_data(manager),
    )

    position_service.create_assignment(
        employee_position.position_id,
        _assignment_data(employee),
    )

    assert employee_position.parent_position_id == manager_position.position_id


def test_reconcile_provisions_positions_for_employees_missing_assignments(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.reports_to_id = manager.employee_id
    db_session.flush()

    result = PositionService(db_session, org_id).reconcile_from_reports_to_id()

    assert isinstance(result, ReconcileResult)
    assert result.positions_created == 2
    assert result.assignments_created == 2
    manager_assignment = db_session.scalar(
        select(PositionAssignment).where(
            PositionAssignment.employee_id == manager.employee_id,
            PositionAssignment.end_date.is_(None),
        )
    )
    employee_assignment = db_session.scalar(
        select(PositionAssignment).where(
            PositionAssignment.employee_id == employee.employee_id,
            PositionAssignment.end_date.is_(None),
        )
    )
    assert manager_assignment is not None
    assert employee_assignment is not None
    employee_position = db_session.get(Position, employee_assignment.position_id)
    assert employee_position.parent_position_id == manager_assignment.position_id


def test_reconcile_syncs_parents_for_existing_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(db_session, org_id, is_vacant=False)
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    employee.reports_to_id = manager.employee_id
    db_session.flush()
    employee_position.parent_position_id = None
    db_session.flush()

    result = service.reconcile_from_reports_to_id()

    assert result.positions_created == 0
    assert result.assignments_created == 0
    assert result.parents_synced == 1
    assert employee_position.parent_position_id == manager_position.position_id


def test_reconcile_is_idempotent(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    employee.reports_to_id = manager.employee_id
    db_session.flush()
    service = PositionService(db_session, org_id)
    service.reconcile_from_reports_to_id()

    second = service.reconcile_from_reports_to_id()

    assert second.positions_created == 0
    assert second.assignments_created == 0
    assert second.parents_synced == 0


def test_reconcile_scopes_to_employee_id_subset(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    in_scope = _make_employee(db_session, org_id, "IN-001")
    out_of_scope = _make_employee(db_session, org_id, "OUT-001")
    db_session.flush()
    service = PositionService(db_session, org_id)

    result = service.reconcile_from_reports_to_id(
        employee_ids=[in_scope.employee_id],
    )

    assert result.positions_created == 1
    assert result.assignments_created == 1
    out_assignment = db_session.scalar(
        select(PositionAssignment).where(
            PositionAssignment.employee_id == out_of_scope.employee_id,
        )
    )
    assert out_assignment is None


def test_reconcile_preserves_parent_when_reports_to_id_is_none(db_session):
    """
    A position parent set via the EmployeeService.set_manager chokepoint
    must NOT be overwritten by a bulk reconcile when the employee's
    reports_to_id is None. Bulk callers that need to clear a manager should
    use set_manager(employee, None) explicitly.
    """
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))
    employee.reports_to_id = None
    db_session.flush()

    result = service.reconcile_from_reports_to_id(
        employee_ids=[employee.employee_id],
    )

    assert result.parents_synced == 0
    assert employee_position.parent_position_id == manager_position.position_id


def test_position_chain_starts_with_employee_and_walks_up(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    director = _make_employee(db_session, org_id, "DIR-001")
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    director_position = _make_position(db_session, org_id, is_vacant=False)
    manager_position = _make_position(
        db_session,
        org_id,
        parent_position_id=director_position.position_id,
        is_vacant=False,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(manager_position.position_id, _assignment_data(manager))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    chain = OrgResolver(db_session).get_position_chain(employee.employee_id, org_id)

    assert [position.position_id for position, _ in chain] == [
        employee_position.position_id,
        manager_position.position_id,
        director_position.position_id,
    ]
    assert [incumbent.employee_id if incumbent else None for _, incumbent in chain] == [
        employee.employee_id,
        manager.employee_id,
        director.employee_id,
    ]


def test_position_chain_includes_vacant_ancestor(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    director = _make_employee(db_session, org_id, "DIR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    director_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=director_position.position_id,
        is_vacant=True,
    )
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=vacant_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(employee_position.position_id, _assignment_data(employee))

    chain = OrgResolver(db_session).get_position_chain(employee.employee_id, org_id)

    assert [position.position_id for position, _ in chain] == [
        employee_position.position_id,
        vacant_position.position_id,
        director_position.position_id,
    ]
    incumbents = [incumbent for _, incumbent in chain]
    assert incumbents[0].employee_id == employee.employee_id
    assert incumbents[1] is None
    assert incumbents[2].employee_id == director.employee_id


def test_position_chain_empty_for_employee_without_assignment(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    employee = _make_employee(db_session, org_id, "EMP-001")

    chain = OrgResolver(db_session).get_position_chain(employee.employee_id, org_id)

    assert chain == []


def test_build_org_chart_returns_roots_with_children(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    ceo = _make_employee(db_session, org_id, "CEO-001")
    director = _make_employee(db_session, org_id, "DIR-001")
    engineer = _make_employee(db_session, org_id, "ENG-001")
    ceo_position = _make_position(db_session, org_id, is_vacant=False)
    director_position = _make_position(
        db_session,
        org_id,
        parent_position_id=ceo_position.position_id,
        is_vacant=False,
    )
    engineer_position = _make_position(
        db_session,
        org_id,
        parent_position_id=director_position.position_id,
        is_vacant=False,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(ceo_position.position_id, _assignment_data(ceo))
    service.create_assignment(director_position.position_id, _assignment_data(director))
    service.create_assignment(engineer_position.position_id, _assignment_data(engineer))

    roots = service.build_org_chart()

    assert len(roots) == 1
    assert isinstance(roots[0], OrgChartNode)
    assert roots[0].position_id == ceo_position.position_id
    assert roots[0].incumbent_employee_id == ceo.employee_id
    assert len(roots[0].children) == 1
    director_node = roots[0].children[0]
    assert director_node.position_id == director_position.position_id
    assert len(director_node.children) == 1
    assert director_node.children[0].position_id == engineer_position.position_id


def test_build_org_chart_marks_vacant_positions(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    ceo = _make_employee(db_session, org_id, "CEO-001")
    ceo_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=ceo_position.position_id,
        is_vacant=True,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(ceo_position.position_id, _assignment_data(ceo))

    roots = service.build_org_chart()

    assert len(roots) == 1
    assert roots[0].is_vacant is False
    vacant_node = roots[0].children[0]
    assert vacant_node.position_id == vacant_position.position_id
    assert vacant_node.is_vacant is True
    assert vacant_node.incumbent_employee_id is None
    assert vacant_node.incumbent_name == ""


def test_build_org_chart_includes_acting_coverage_for_vacant_position(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    ceo = _make_employee(db_session, org_id, "CEO-001")
    acting_manager = _make_employee(db_session, org_id, "ACT-001")
    ceo_position = _make_position(db_session, org_id, is_vacant=False)
    vacant_position = _make_position(
        db_session,
        org_id,
        parent_position_id=ceo_position.position_id,
        is_vacant=True,
    )
    service = PositionService(db_session, org_id)
    service.create_assignment(ceo_position.position_id, _assignment_data(ceo))
    service.create_assignment(
        vacant_position.position_id,
        _assignment_data(
            acting_manager,
            assignment_type=PositionAssignmentType.ACTING,
        ),
    )

    roots = service.build_org_chart()

    vacant_node = roots[0].children[0]
    assert vacant_node.is_vacant is True
    assert vacant_node.incumbent_employee_id is None
    assert len(vacant_node.covering_assignments) == 1
    coverage = vacant_node.covering_assignments[0]
    assert coverage.employee_id == acting_manager.employee_id
    assert coverage.employee_name == "ACT-001 Employee"
    assert coverage.assignment_type == PositionAssignmentType.ACTING


def test_build_org_chart_empty_org_returns_empty(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()

    roots = PositionService(db_session, org_id).build_org_chart()

    assert roots == []


def test_employee_reports_to_update_rejects_position_cycle(db_session):
    _ensure_hr_position_tables(db_session.bind)
    org_id = uuid.uuid4()
    manager = _make_employee(db_session, org_id, "MGR-001")
    employee = _make_employee(db_session, org_id, "EMP-001")
    manager_position = _make_position(db_session, org_id, is_vacant=False)
    employee_position = _make_position(
        db_session,
        org_id,
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    position_service = PositionService(db_session, org_id)
    position_service.create_assignment(
        manager_position.position_id,
        _assignment_data(manager),
    )
    position_service.create_assignment(
        employee_position.position_id,
        _assignment_data(employee),
    )

    with pytest.raises(InvalidManagerError):
        EmployeeService(db_session, org_id).update_employee(
            manager.employee_id,
            EmployeeUpdateData(reports_to_id=employee.employee_id),
        )
