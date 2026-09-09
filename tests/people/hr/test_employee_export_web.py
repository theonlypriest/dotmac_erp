from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.people.hr.employee import EmployeeStatus
from app.services.people.hr.web.employee_web import HRWebService
from app.web.deps import WebAuthContext


def _auth() -> WebAuthContext:
    return WebAuthContext(
        is_authenticated=True,
        organization_id=uuid4(),
        roles=["hr_manager"],
        scopes=["hr:access"],
    )


def test_employee_export_uses_allowlisted_fields_and_csv_safe_cells(monkeypatch):
    employee = SimpleNamespace(
        employee_code="=SUM(A1:A2)",
        person=SimpleNamespace(
            name="Ada Lovelace", email="ada@example.com", phone=None
        ),
        department=SimpleNamespace(department_name="Engineering"),
        designation=None,
        employment_type=None,
        status=EmployeeStatus.ACTIVE,
        date_of_joining=None,
        date_of_leaving=None,
        probation_end_date=None,
        confirmation_date=None,
        personal_email=None,
        personal_phone=None,
    )
    captured = {}

    def _list(self, filters, pagination, *, eager_load=False):
        captured["filters"] = filters
        captured["pagination"] = pagination
        captured["eager_load"] = eager_load
        return SimpleNamespace(items=[employee])

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.list_employees",
        _list,
    )

    response = HRWebService().export_employees_csv_response(
        auth=_auth(),
        db=object(),
        fields=["employee_code", "full_name", "not_an_employee_field"],
        status="active",
    )

    assert (
        response.body.decode()
        == "Employee Code,Full Name\r\n'=SUM(A1:A2),Ada Lovelace\r\n"
    )
    assert captured["filters"].status == EmployeeStatus.ACTIVE
    assert captured["eager_load"] is True
    assert captured["pagination"].limit == 100_000


def test_employee_export_requires_a_selected_allowlisted_field():
    with pytest.raises(HTTPException, match="Select at least one export field"):
        HRWebService().export_employees_csv_response(
            auth=_auth(),
            db=object(),
            fields=["bank_account_number"],
        )


def test_employee_export_resolves_employment_type_from_complete_owner_catalogue(
    monkeypatch,
):
    organization_id = uuid4()
    employment_type_id = uuid4()
    employee = SimpleNamespace(employment_type_id=employment_type_id)
    employment_type = SimpleNamespace(
        employment_type_id=employment_type_id,
        type_name="Fixed Term",
    )
    auth = _auth()
    auth.organization_id = organization_id
    db = object()

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.list_employees",
        lambda self, filters, pagination, *, eager_load=False: SimpleNamespace(
            items=[employee]
        ),
    )

    class _Owner:
        def __init__(self, session, requested_organization_id):
            assert session is db
            assert requested_organization_id == organization_id

        def iter_all(self, active=None):
            assert active is None
            return (employment_type,)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmploymentTypeService", _Owner
    )

    response = HRWebService().export_employees_csv_response(
        auth=auth,
        db=db,
        fields=["employment_type"],
    )

    assert response.body.decode() == "Employment Type\r\nFixed Term\r\n"


def test_employee_export_inactive_filter_includes_all_inactive_lifecycle_statuses(
    monkeypatch,
):
    captured = {}

    def _list(self, filters, pagination, *, eager_load=False):
        captured["filters"] = filters
        return SimpleNamespace(items=[])

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.list_employees",
        _list,
    )

    HRWebService().export_employees_csv_response(
        auth=_auth(),
        db=object(),
        fields=["employee_code"],
        status="inactive",
    )

    assert captured["filters"].statuses == [
        EmployeeStatus.SUSPENDED,
        EmployeeStatus.RESIGNED,
        EmployeeStatus.TERMINATED,
        EmployeeStatus.RETIRED,
    ]
    assert captured["filters"].include_archived is True
    assert captured["filters"].include_deleted is True
