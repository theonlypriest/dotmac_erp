from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.models.person import Person
from app.services.common import ValidationError
from app.services.people.hr.web.employee_web import HRWebService
from app.web.deps import WebAuthContext


def _make_request(form: dict[str, str]) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/people/hr/employees/test/edit",
            "headers": [],
        }
    )
    request.state.csrf_form = form
    return request


def _make_new_employee_request(form: dict[str, str]) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/people/hr/employees/new",
            "headers": [],
        }
    )
    request.state.csrf_form = form
    return request


def _make_auth(person_id, organization_id, scopes: list[str]) -> WebAuthContext:
    return WebAuthContext(
        is_authenticated=True,
        person_id=person_id,
        organization_id=organization_id,
        roles=["hr_manager"],
        scopes=["hr:access", *scopes],
    )


def _stub_new_employee_form_dependencies(monkeypatch, db_session) -> None:
    empty_result = SimpleNamespace(items=[])
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrganizationService.list_departments",
        lambda self, filters, pagination: empty_result,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrganizationService.list_designations",
        lambda self, filters, pagination: empty_result,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrganizationService.list_employment_types",
        lambda self, filters, pagination: empty_result,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrganizationService.list_employee_grades",
        lambda self, filters, pagination: empty_result,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.list_employees",
        lambda self, filters, pagination, eager_load=False: empty_result,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.AttendanceService.list_shift_types",
        lambda self, organization_id, is_active, pagination: empty_result,
    )
    monkeypatch.setattr(
        HRWebService, "_load_manager_position_titles", lambda self, db, org_id, ids: {}
    )
    monkeypatch.setattr(HRWebService, "_list_pfas", lambda self, db: [])
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.base_context",
        lambda request, auth, title, active: {"title": title, "active_page": active},
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.templates.TemplateResponse",
        lambda request, template_name, context: SimpleNamespace(
            status_code=200,
            context=context,
        ),
    )
    monkeypatch.setattr(
        db_session, "scalars", lambda stmt: SimpleNamespace(all=lambda: [])
    )
    monkeypatch.setattr(
        db_session, "execute", lambda stmt: SimpleNamespace(all=lambda: [])
    )


def _stub_salary_structure_lookup(
    db_session, monkeypatch, organization_id, structure_id
):
    original_scalar = db_session.scalar
    salary_structure = SimpleNamespace(
        structure_id=structure_id,
        organization_id=organization_id,
        is_active=True,
    )

    def _scalar(stmt):
        if "salary_structure" in str(stmt):
            return salary_structure
        return original_scalar(stmt)

    monkeypatch.setattr(db_session, "scalar", _scalar)
    return salary_structure


@pytest.mark.asyncio
async def test_update_employee_response_updates_linked_person_with_people_write(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        lambda self, _employee_id, _data: employee,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request(
        {
            "first_name": "Updated",
            "last_name": "Person",
            "email": f"updated-{uuid4().hex[:8]}@example.com",
            "phone": "+2348000000000",
            "city": "Lagos",
            "country_code": "NG",
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    db_session.refresh(person)
    assert response.status_code == 303
    assert person.first_name == "Updated"
    assert person.last_name == "Person"
    assert person.phone == "+2348000000000"
    assert person.city == "Lagos"
    assert person.country_code == "NG"
    assert person.email.startswith("updated-")


@pytest.mark.asyncio
async def test_update_employee_response_uses_request_form_when_csrf_state_is_html(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        lambda self, _employee_id, _data: employee,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request({})
    request.state.csrf_form = '<input type="hidden" name="csrf_token" value="token">'

    async def _request_form():
        return {
            "first_name": "Fallback",
            "last_name": "Reader",
            "email": f"fallback-{uuid4().hex[:8]}@example.com",
            "city": "Ibadan",
            "country_code": "NG",
        }

    request.form = _request_form
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    db_session.refresh(person)
    assert response.status_code == 303
    assert person.first_name == "Fallback"
    assert person.last_name == "Reader"
    assert person.city == "Ibadan"
    assert person.country_code == "NG"


@pytest.mark.asyncio
async def test_update_employee_response_keeps_linked_person_read_only_without_people_write(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    original_email = person.email

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        lambda self, _employee_id, _data: employee,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request(
        {
            "first_name": "Blocked",
            "email": f"blocked-{uuid4().hex[:8]}@example.com",
            "city": "Abuja",
        }
    )
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    stored = db_session.get(Person, person.id)
    assert response.status_code == 303
    assert stored is not None
    assert stored.first_name == person.first_name
    assert stored.email == original_email
    assert stored.city is None


@pytest.mark.asyncio
async def test_update_employee_response_does_not_clear_manager_when_field_omitted(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )

    def _capture_update(self, _employee_id, data):
        captured["reports_to_id"] = data.reports_to_id
        captured["provided_fields"] = data.provided_fields
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        _capture_update,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request(
        {
            "first_name": "No",
            "last_name": "ManagerField",
            "email": f"no-manager-{uuid4().hex[:8]}@example.com",
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["reports_to_id"] is None
    assert "reports_to_id" not in captured["provided_fields"]


@pytest.mark.asyncio
async def test_update_employee_response_passes_sub_application_access(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )

    def _capture_update(self, _employee_id, data):
        captured["enabled"] = data.dotmac_sub_access_enabled
        captured["roles"] = data.dotmac_sub_roles
        captured["provided_fields"] = data.provided_fields
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        _capture_update,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request(
        {
            "dotmac_sub_access_present": "true",
            "dotmac_sub_access_enabled": "true",
            "dotmac_sub_roles": "staff, field_technician, staff",
        }
    )
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["enabled"] is True
    assert captured["roles"] == ["staff", "field_technician"]
    assert "dotmac_sub_access_enabled" in captured["provided_fields"]


@pytest.mark.asyncio
async def test_create_employee_response_passes_selected_position_id(
    db_session, person, monkeypatch
):
    service = HRWebService()
    position_id = uuid4()
    employee_id = uuid4()
    structure_id = uuid4()
    captured: dict[str, object] = {}
    _stub_salary_structure_lookup(
        db_session, monkeypatch, person.organization_id, structure_id
    )

    def _capture_create(self, person_id, data):
        captured["person_id"] = person_id
        captured["position_id"] = data.position_id
        return SimpleNamespace(employee_id=employee_id, person_id=person_id)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _capture_create,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.send_employee_access_invite",
        lambda self, employee_id, app_url, attachments=None: SimpleNamespace(
            sent=True,
            recipient_kind="work",
            recipient_email="user@example.com",
        ),
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_create_initial_salary_assignment",
        staticmethod(lambda **kwargs: None),
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "position_id": str(position_id),
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": str(structure_id),
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["person_id"] == person.id
    assert captured["position_id"] == position_id


@pytest.mark.asyncio
async def test_create_employee_response_creates_initial_salary_assignment(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    structure_id = uuid4()
    captured: dict[str, object] = {}
    salary_structure = _stub_salary_structure_lookup(
        db_session, monkeypatch, person.organization_id, structure_id
    )

    def _capture_assignment(**kwargs):
        captured["organization_id"] = kwargs["organization_id"]
        captured["employee_id"] = kwargs["employee"].employee_id
        captured["salary_structure"] = kwargs["salary_structure"]
        captured["base"] = kwargs["base"]

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        lambda self, person_id, data: SimpleNamespace(
            employee_id=employee_id,
            person_id=person_id,
            date_of_joining=date(2026, 1, 1),
        ),
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.send_employee_access_invite",
        lambda self, employee_id, app_url: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_create_initial_salary_assignment",
        staticmethod(_capture_assignment),
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": str(structure_id),
            "ctc": "1200000",
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["organization_id"] == person.organization_id
    assert captured["employee_id"] == employee_id
    assert captured["salary_structure"] is salary_structure
    assert str(captured["base"]) == "1200000"


@pytest.mark.asyncio
async def test_create_employee_response_requires_contract_and_salary_mode(
    db_session, person, monkeypatch
):
    service = HRWebService()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)

    def _fail_create(self, person_id, data):
        raise AssertionError("employee should not be created")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _fail_create,
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "salary_structure_id": str(uuid4()),
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert (
        response.context["error"]
        == "Contract type and salary mode must be selected for employee creation."
    )
    assert response.context["errors"] == {
        "employment_type_id": "Required",
        "salary_mode": "Required",
    }
    assert response.context["form_data"]["current_tab"] == "employment"


@pytest.mark.asyncio
async def test_create_employee_response_requires_salary_structure_before_create(
    db_session, person, monkeypatch
):
    service = HRWebService()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)

    def _fail_create(self, person_id, data):
        raise AssertionError("employee should not be created")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _fail_create,
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert (
        response.context["error"]
        == "Salary structure is required for employee creation."
    )
    assert response.context["errors"] == {}
    assert response.context["form_data"]["current_tab"] == "employment"


@pytest.mark.asyncio
async def test_create_employee_response_rejects_invalid_salary_structure_before_create(
    db_session, person, monkeypatch
):
    service = HRWebService()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)

    def _fail_create(self, person_id, data):
        raise AssertionError("employee should not be created")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _fail_create,
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": "not-a-uuid",
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert (
        response.context["error"]
        == "Select a valid active salary structure for this organization."
    )
    assert response.context["errors"] == {}
    assert response.context["form_data"]["salary_structure_id"] == "not-a-uuid"


@pytest.mark.asyncio
async def test_create_employee_response_rejects_country_name_before_create(
    db_session, person, monkeypatch
):
    service = HRWebService()
    structure_id = uuid4()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)
    _stub_salary_structure_lookup(
        db_session, monkeypatch, person.organization_id, structure_id
    )

    def _fail_create(self, person_id, data):
        raise AssertionError("employee should not be created")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _fail_create,
    )

    request = _make_new_employee_request(
        {
            "first_name": "Mir",
            "last_name": "David",
            "email": f"country-name-{uuid4().hex[:8]}@example.com",
            "country_code": "Nicaragua",
            "date_of_joining": "2026-01-01",
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": str(structure_id),
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert (
        response.context["error"]
        == "Country Code must be a 2-letter code like NI, not the country name."
    )
    assert response.context["errors"] == {
        "country_code": "Use a 2-letter country code like NI, not the country name."
    }
    assert response.context["form_data"]["country_code"] == "Nicaragua"
    assert response.context["form_data"]["current_tab"] == "personal"


@pytest.mark.asyncio
async def test_create_employee_response_does_not_fail_when_invite_fails(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    structure_id = uuid4()
    _stub_salary_structure_lookup(
        db_session, monkeypatch, person.organization_id, structure_id
    )

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        lambda self, person_id, data: SimpleNamespace(
            employee_id=employee_id,
            person_id=person_id,
        ),
    )

    def _raise_invite_error(self, employee_id, app_url, attachments=None):
        raise ValidationError("Employee user credentials are not ready for invite")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.send_employee_access_invite",
        _raise_invite_error,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_create_initial_salary_assignment",
        staticmethod(lambda **kwargs: None),
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": str(structure_id),
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/people/hr/employees/{employee_id}?saved=1&invite_status=failed"
    )


def test_employee_new_form_does_not_load_position_options_initially(
    db_session, person, monkeypatch
):
    service = HRWebService()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)

    def _fail_position_load(db, org_id):
        raise AssertionError("Position options should lazy-load after page render")

    monkeypatch.setattr(
        HRWebService, "_list_vacant_position_options", _fail_position_load
    )

    request = _make_new_employee_request({})
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = service.employee_new_form_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert response.context["position_options"] == []


def test_employee_new_form_exposes_siwes_intern_designation(
    db_session, person, monkeypatch
):
    service = HRWebService()
    _stub_new_employee_form_dependencies(monkeypatch, db_session)
    designation_id = uuid4()
    siwes_designation = SimpleNamespace(
        designation_id=designation_id,
        designation_code="SIWES-INTERN",
        designation_name="SIWES Intern",
    )
    designation_result = SimpleNamespace(items=[siwes_designation])

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrganizationService.list_designations",
        lambda self, filters, pagination: designation_result,
    )

    request = _make_new_employee_request({})
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = service.employee_new_form_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert response.context["designations"] == [siwes_designation]


@pytest.mark.asyncio
async def test_create_employee_response_passes_siwes_intern_designation_id(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    structure_id = uuid4()
    designation_id = uuid4()
    captured: dict[str, object] = {}
    _stub_salary_structure_lookup(
        db_session, monkeypatch, person.organization_id, structure_id
    )

    def _capture_create(self, person_id, data):
        captured["person_id"] = person_id
        captured["designation_id"] = data.designation_id
        return SimpleNamespace(employee_id=employee_id, person_id=person_id)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.create_employee",
        _capture_create,
    )
    invite_path = (
        "app.services.people.hr.web.employee_web."
        "EmployeeService.send_employee_access_invite"
    )
    monkeypatch.setattr(
        invite_path,
        lambda self, employee_id, app_url, attachments=None: SimpleNamespace(
            sent=True,
            recipient_kind="work",
            recipient_email="user@example.com",
        ),
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_create_initial_salary_assignment",
        staticmethod(lambda **kwargs: None),
    )

    request = _make_new_employee_request(
        {
            "linked_person_id": str(person.id),
            "date_of_joining": "2026-01-01",
            "designation_id": str(designation_id),
            "employment_type_id": str(uuid4()),
            "salary_mode": "BANK",
            "salary_structure_id": str(structure_id),
        }
    )
    auth = _make_auth(person.id, person.organization_id, ["people:write"])

    response = await service.create_employee_response(
        request=request,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["person_id"] == person.id
    assert captured["designation_id"] == designation_id


@pytest.mark.asyncio
async def test_update_employee_response_persists_nysc_dates(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    designation_id = str(uuid4())
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )

    def _capture_update(self, _employee_id, data):
        captured["nysc_start_date"] = data.nysc_start_date
        captured["nysc_end_date"] = data.nysc_end_date
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        _capture_update,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )
    monkeypatch.setattr(
        HRWebService,
        "_designation_requires_nysc_dates",
        lambda self, db, organization_id, designation_id: True,
    )

    request = _make_request(
        {
            "designation_id": designation_id,
            "nysc_start_date": "2026-01-10",
            "nysc_end_date": "2026-11-10",
        }
    )
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/people/hr/employees/{employee_id}/edit?success=Saved%20successfully."
    )
    assert str(captured["nysc_start_date"]) == "2026-01-10"
    assert str(captured["nysc_end_date"]) == "2026-11-10"


@pytest.mark.asyncio
async def test_update_employee_response_ignores_terminal_status_from_edit_form(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )

    def _capture_update(self, _employee_id, data):
        captured["status"] = data.status
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        _capture_update,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request({"status": "TERMINATED"})
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["status"] is None
    assert (
        response.headers["location"]
        == f"/people/hr/employees/{employee_id}/edit?success=Saved%20successfully."
    )


@pytest.mark.asyncio
async def test_update_employee_response_does_not_reload_employee_after_commit(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    calls = 0

    def _get_employee(self, _employee_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("Employee should not be reloaded after commit")
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        _get_employee,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        lambda self, _employee_id, data: employee,
    )
    monkeypatch.setattr(
        HRWebService,
        "_update_tax_profile",
        lambda self, *, auth, db, employee, form: None,
    )

    request = _make_request({})
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert calls == 1


@pytest.mark.asyncio
async def test_update_employee_response_requires_nysc_dates_for_nysc_designation(
    db_session, person, monkeypatch
):
    service = HRWebService()
    employee_id = uuid4()
    designation_id = str(uuid4())
    employee = SimpleNamespace(employee_id=employee_id, person_id=person.id)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id: employee,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_employee",
        lambda self, _employee_id, data: captured.setdefault("called", True),
    )
    monkeypatch.setattr(
        HRWebService,
        "_designation_requires_nysc_dates",
        lambda self, db, organization_id, designation_id: True,
    )

    def _capture_edit_form(self, request, auth, db, employee_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200, context=kwargs)

    monkeypatch.setattr(HRWebService, "employee_edit_form_response", _capture_edit_form)

    request = _make_request(
        {
            "designation_id": designation_id,
            "nysc_start_date": "",
            "nysc_end_date": "",
        }
    )
    auth = _make_auth(person.id, person.organization_id, [])

    response = await service.update_employee_response(
        request=request,
        employee_id=employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    assert captured["errors"]["nysc_start_date"] == "Required for NYSC designation"
    assert captured["errors"]["nysc_end_date"] == "Required for NYSC designation"
    assert "called" not in captured
