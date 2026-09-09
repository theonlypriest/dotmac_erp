"""ERP's read-only People replacement boundary.

The projection is a temporary migration surface: it gives Backoffice a
tenant-scoped, versioned source for backfill and reconciliation without
creating a second writer or exporting ERP-only HR/payroll data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import HTTPException
import pytest

from app.api.service_principal import require_explicit_service_scope
from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    EmployeeStatus,
    EmploymentType,
    Position,
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.person import Person
from app.schemas.sync.backoffice_people import (
    DepartmentProjection,
    DesignationProjection,
    EmployeeProjection,
    EmploymentTypeProjection,
    PartyPersonProjection,
    PositionAssignmentProjection,
    PositionProjection,
)
from app.services.people.hr.replacement_projection import (
    BackofficePeopleProjectionService,
    PeopleProjectionEntity,
)
from app.services.people.hr.employment_types import EmploymentTypeView


ORG_A = UUID("00000000-0000-0000-0000-00000000000a")
ORG_B = UUID("00000000-0000-0000-0000-00000000000b")
ID_1 = UUID("10000000-0000-0000-0000-000000000001")
ID_2 = UUID("20000000-0000-0000-0000-000000000002")


def _ensure_projection_tables(engine) -> None:
    for table in (
        Department.__table__,
        Designation.__table__,
        EmploymentType.__table__,
        Employee.__table__,
        Position.__table__,
        PositionAssignment.__table__,
    ):
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            default_text = str(getattr(default, "arg", default)).lower()
            if "gen_random_uuid" in default_text or "uuid_generate" in default_text:
                column.server_default = None
        table.create(engine, checkfirst=True)


def test_contract_exposes_only_released_people_fields() -> None:
    common = {"entity", "source_id", "source_fingerprint", "source_updated_at"}
    expected = {
        PartyPersonProjection: common
        | {"display_name", "email", "is_active", "first_name", "last_name"},
        DepartmentProjection: common
        | {"code", "name", "description", "parent_id", "is_active"},
        DesignationProjection: common | {"code", "name", "description", "is_active"},
        EmploymentTypeProjection: common | {"code", "name", "description", "is_active"},
        EmployeeProjection: common
        | {
            "party_id",
            "employee_code",
            "department_id",
            "designation_id",
            "employment_type_id",
            "date_of_joining",
            "date_of_leaving",
            "probation_end_date",
            "confirmation_date",
            "status",
        },
        PositionProjection: common
        | {
            "code",
            "name",
            "department_id",
            "designation_id",
            "parent_id",
            "is_department_head",
            "vacancy_routing_policy",
            "is_active",
        },
        PositionAssignmentProjection: common
        | {
            "employee_id",
            "position_id",
            "assignment_type",
            "start_date",
            "end_date",
        },
    }

    assert {model: set(model.model_fields) for model in expected} == expected

    exported = set().union(*(set(model.model_fields) for model in expected))
    assert not exported.intersection(
        {
            "bank_account_number",
            "bank_name",
            "ctc",
            "salary_mode",
            "reports_to_id",
            "head_id",
            "is_vacant",
            "cost_center_id",
            "assigned_location_id",
        }
    )


def test_party_projection_is_tenant_scoped_keyset_paginated_and_fingerprinted(
    db_session,
) -> None:
    first = Person(
        id=ID_1,
        organization_id=ORG_A,
        first_name="Ada",
        last_name="One",
        display_name="Ada One",
        email="ada.one@example.com",
    )
    second = Person(
        id=ID_2,
        organization_id=ORG_A,
        first_name="Ada",
        last_name="Two",
        display_name="Ada Two",
        email="ada.two@example.com",
    )
    other_tenant = Person(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        organization_id=ORG_B,
        first_name="Other",
        last_name="Tenant",
        email="other.tenant@example.com",
    )
    db_session.add_all([first, second, other_tenant])
    db_session.flush()
    service = BackofficePeopleProjectionService(db_session)

    page_one = service.page(
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.PARTY_PERSON,
        after=None,
        limit=1,
    )
    assert [item.source_id for item in page_one.items] == [ID_1]
    assert page_one.next_after == ID_1
    assert page_one.contract_version == "backoffice.people.projection.v1"

    fingerprint = page_one.items[0].source_fingerprint
    first.display_name = "Ada Changed"
    db_session.flush()
    changed = service.page(
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.PARTY_PERSON,
        after=None,
        limit=1,
    )
    assert changed.items[0].source_fingerprint != fingerprint

    page_two = service.page(
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.PARTY_PERSON,
        after=ID_1,
        limit=2,
    )
    assert [item.source_id for item in page_two.items] == [ID_2]
    assert page_two.next_after is None


def test_projection_maps_the_released_contract_and_derives_department_head(
    db_session,
    monkeypatch,
) -> None:
    _ensure_projection_tables(db_session.bind)
    person = Person(
        id=UUID("30000000-0000-0000-0000-000000000003"),
        organization_id=ORG_A,
        first_name="Head",
        last_name="Employee",
        email="department.head@example.com",
    )
    employee = Employee(
        employee_id=UUID("40000000-0000-0000-0000-000000000004"),
        organization_id=ORG_A,
        person_id=person.id,
        employee_code="EMP-HEAD",
        date_of_joining=date(2025, 1, 1),
        status=EmployeeStatus.ACTIVE,
    )
    department = Department(
        department_id=UUID("50000000-0000-0000-0000-000000000005"),
        organization_id=ORG_A,
        department_code="OPS",
        department_name="Operations",
        head_id=employee.employee_id,
    )
    designation = Designation(
        designation_id=UUID("51000000-0000-0000-0000-000000000005"),
        organization_id=ORG_A,
        designation_code="OPS-MGR",
        designation_name="Operations Manager",
        description="Owns field operations",
    )
    employment_type = EmploymentType(
        employment_type_id=UUID("52000000-0000-0000-0000-000000000005"),
        organization_id=ORG_A,
        type_code="FULL-TIME",
        type_name="Full time",
        description="Permanent employment",
    )
    employee.department_id = department.department_id
    employee.designation_id = designation.designation_id
    employee.employment_type_id = employment_type.employment_type_id
    employee.probation_end_date = date(2025, 6, 30)
    employee.confirmation_date = date(2025, 7, 1)
    position = Position(
        position_id=UUID("60000000-0000-0000-0000-000000000006"),
        organization_id=ORG_A,
        position_code="OPS-HEAD",
        position_name="Operations Head",
        department_id=department.department_id,
        designation_id=designation.designation_id,
    )
    assignment = PositionAssignment(
        position_assignment_id=UUID("70000000-0000-0000-0000-000000000007"),
        organization_id=ORG_A,
        employee_id=employee.employee_id,
        position_id=position.position_id,
        assignment_type=PositionAssignmentType.PRIMARY,
        start_date=date(2025, 1, 1),
    )
    db_session.add_all(
        [
            person,
            employee,
            department,
            designation,
            employment_type,
            position,
            assignment,
        ]
    )
    db_session.flush()

    authoritative_employment_type = EmploymentTypeView(
        employment_type_id=employment_type.employment_type_id,
        organization_id=ORG_A,
        type_code="FULL-TIME",
        type_name="Full time",
        description="Permanent employment",
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    class FakeEmploymentTypeService:
        def __init__(self, _db, organization_id):
            assert organization_id == ORG_A

        def iter_all(self):
            return (authoritative_employment_type,)

    monkeypatch.setattr(
        "app.services.people.hr.replacement_projection.EmploymentTypeService",
        FakeEmploymentTypeService,
    )

    service = BackofficePeopleProjectionService(db_session)

    def projected(entity: PeopleProjectionEntity, source_id: UUID):
        page = service.page(
            organization_id=ORG_A,
            entity=entity,
            after=None,
            limit=10,
        )
        return next(item for item in page.items if item.source_id == source_id)

    department_item = projected(
        PeopleProjectionEntity.DEPARTMENT, department.department_id
    )
    assert department_item.code == "OPS"
    assert department_item.name == "Operations"
    assert department_item.parent_id is None

    designation_item = projected(
        PeopleProjectionEntity.DESIGNATION, designation.designation_id
    )
    assert designation_item.code == "OPS-MGR"
    assert designation_item.name == "Operations Manager"

    employment_type_item = projected(
        PeopleProjectionEntity.EMPLOYMENT_TYPE,
        employment_type.employment_type_id,
    )
    assert employment_type_item.code == "FULL-TIME"
    assert employment_type_item.name == "Full time"

    employee_item = projected(PeopleProjectionEntity.EMPLOYEE, employee.employee_id)
    assert employee_item.party_id == person.id
    assert employee_item.department_id == department.department_id
    assert employee_item.designation_id == designation.designation_id
    assert employee_item.employment_type_id == employment_type.employment_type_id
    assert employee_item.status == "ACTIVE"
    assert employee_item.probation_end_date == date(2025, 6, 30)
    assert employee_item.confirmation_date == date(2025, 7, 1)

    position_item = projected(PeopleProjectionEntity.POSITION, position.position_id)
    assert position_item.department_id == department.department_id
    assert position_item.designation_id == designation.designation_id
    assert position_item.is_department_head is True
    assert position_item.vacancy_routing_policy == "SKIP_UP"

    assignment_item = projected(
        PeopleProjectionEntity.POSITION_ASSIGNMENT,
        assignment.position_assignment_id,
    )
    assert assignment_item.employee_id == employee.employee_id
    assert assignment_item.position_id == position.position_id
    assert assignment_item.assignment_type == "PRIMARY"
    assert assignment_item.start_date == date(2025, 1, 1)
    assert assignment_item.end_date is None


def test_employment_type_projection_pages_complete_module_set_by_uuid(
    monkeypatch,
) -> None:
    records = tuple(
        EmploymentTypeView(
            employment_type_id=UUID(int=index),
            organization_id=ORG_A,
            type_code=f"TYPE-{index:03d}",
            type_name=f"Type {index:03d}",
            description=None,
            is_active=True,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index in range(205, 0, -1)
    )

    class FakeEmploymentTypeService:
        def __init__(self, _db, organization_id):
            assert organization_id == ORG_A

        def iter_all(self):
            return records

    monkeypatch.setattr(
        "app.services.people.hr.replacement_projection.EmploymentTypeService",
        FakeEmploymentTypeService,
    )
    service = BackofficePeopleProjectionService(object())  # type: ignore[arg-type]

    first = service.page(
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.EMPLOYMENT_TYPE,
        after=None,
        limit=200,
    )
    assert len(first.items) == 200
    assert first.items[0].source_id == UUID(int=1)
    assert first.items[-1].source_id == UUID(int=200)
    assert first.next_after == UUID(int=200)

    second = service.page(
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.EMPLOYMENT_TYPE,
        after=first.next_after,
        limit=200,
    )
    assert [item.source_id for item in second.items] == [
        UUID(int=index) for index in range(201, 206)
    ]
    assert second.next_after is None


def test_employment_type_projection_preserves_created_timestamp_when_never_updated(
    monkeypatch,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    record = EmploymentTypeView(
        employment_type_id=UUID(int=1),
        organization_id=ORG_A,
        type_code="PERMANENT",
        type_name="Permanent",
        description=None,
        is_active=True,
        created_at=created_at,
        updated_at=None,
    )

    class FakeEmploymentTypeService:
        def __init__(self, _db, organization_id):
            assert organization_id == ORG_A

        def iter_all(self):
            return (record,)

    monkeypatch.setattr(
        "app.services.people.hr.replacement_projection.EmploymentTypeService",
        FakeEmploymentTypeService,
    )

    page = BackofficePeopleProjectionService(object()).page(  # type: ignore[arg-type]
        organization_id=ORG_A,
        entity=PeopleProjectionEntity.EMPLOYMENT_TYPE,
        after=None,
        limit=200,
    )

    assert page.items[0].source_updated_at == created_at


def test_projection_fingerprint_is_not_a_bootstrap_public_alias() -> None:
    from app.services.people.hr import replacement_projection

    assert not hasattr(replacement_projection, "people_projection_fingerprint")


def test_backoffice_projection_scope_fails_closed_for_unscoped_keys() -> None:
    dependency = require_explicit_service_scope("backoffice:people:read")

    with pytest.raises(HTTPException) as exc:
        dependency(auth={"scopes": []})
    assert exc.value.status_code == 403
    assert "backoffice:people:read" in exc.value.detail["message"]
    assert dependency(auth={"scopes": ["backoffice:people:read"]})["scopes"]
