from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from app.api.people.hr import update_employee as update_employee_api
from app.models.people.hr.employee import EmployeeStatus
from app.schemas.people.hr import EmployeeUpdate
from app.services.people.hr.employee_types import TerminationData
from app.services.people.hr.employees import EmployeeService
from app.services.people.hr.web.employee_web import HRWebService
from app.web.deps import WebAuthContext


def _employee(status: EmployeeStatus, *, leaving: date | None = None):
    return SimpleNamespace(
        employee_id=uuid4(),
        organization_id=uuid4(),
        person_id=uuid4(),
        status=status,
        date_of_joining=date(2020, 1, 1),
        date_of_leaving=leaving,
        eligible_for_final_payroll=False,
        final_payroll_cutoff_date=None,
        final_payroll_processed_at=None,
        updated_at=None,
        updated_by_id=None,
        version=0,
    )


def _make_request(form: dict[str, str]) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/people/hr/employees/test/resign",
            "headers": [],
        }
    )
    request.state.csrf_form = form
    request.state.csrf_token = "token"
    request.state.csrf_form = form
    return request


def _make_auth(*, roles: list[str]) -> WebAuthContext:
    return WebAuthContext(
        is_authenticated=True,
        person_id=uuid4(),
        organization_id=uuid4(),
        roles=roles,
        scopes=["hr:access"],
    )


def test_resign_employee_sets_final_payroll_fields():
    db = SimpleNamespace()
    svc = EmployeeService(db, uuid4())
    emp = _employee(EmployeeStatus.ACTIVE)
    svc.get_employee = lambda _employee_id: emp

    result = svc.resign_employee(
        emp.employee_id,
        date(2026, 4, 25),
        eligible_for_final_payroll=True,
        final_payroll_cutoff_date=None,
    )

    assert result.status == EmployeeStatus.RESIGNED
    assert result.eligible_for_final_payroll is True
    assert result.final_payroll_cutoff_date == date(2026, 4, 25)
    assert result.final_payroll_processed_at is None


def test_terminate_employee_sets_explicit_final_payroll_cutoff():
    db = SimpleNamespace()
    svc = EmployeeService(db, uuid4())
    emp = _employee(EmployeeStatus.ACTIVE)
    svc.get_employee = lambda _employee_id: emp

    result = svc.terminate_employee(
        emp.employee_id,
        TerminationData(
            date_of_leaving=date(2026, 4, 25),
            eligible_for_final_payroll=True,
            final_payroll_cutoff_date=date(2026, 4, 30),
        ),
    )

    assert result.status == EmployeeStatus.TERMINATED
    assert result.eligible_for_final_payroll is True
    assert result.final_payroll_cutoff_date == date(2026, 4, 30)


def test_rehire_clears_final_payroll_fields(monkeypatch):
    db = SimpleNamespace()
    svc = EmployeeService(db, uuid4())
    emp = _employee(EmployeeStatus.RESIGNED, leaving=date(2026, 1, 31))
    emp.eligible_for_final_payroll = True
    emp.final_payroll_cutoff_date = date(2026, 1, 31)
    emp.final_payroll_processed_at = object()
    svc.get_employee = lambda _employee_id: emp

    monkeypatch.setattr(
        "app.services.people.hr.employees.fire_audit_event",
        lambda **_kwargs: None,
    )

    class _LifecycleService:
        def __init__(self, _db):
            pass

        def create_onboarding(self, _org_id, **_kwargs):
            return SimpleNamespace(status=None)

    monkeypatch.setattr(
        "app.services.people.hr.lifecycle.LifecycleService",
        _LifecycleService,
    )

    result = svc.rehire_employee(emp.employee_id, date(2026, 2, 1), notes="rehired")

    assert result.eligible_for_final_payroll is False
    assert result.final_payroll_cutoff_date is None
    assert result.final_payroll_processed_at is None


def test_update_final_payroll_settings_disables_without_clearing_processed_at():
    db = SimpleNamespace(flush=lambda: None)
    svc = EmployeeService(db, uuid4())
    emp = _employee(EmployeeStatus.RESIGNED, leaving=date(2026, 4, 25))
    processed_at = object()
    emp.eligible_for_final_payroll = True
    emp.final_payroll_cutoff_date = date(2026, 4, 30)
    emp.final_payroll_processed_at = processed_at
    svc.get_employee = lambda _employee_id, include_deleted=False: emp

    result = svc.update_final_payroll_settings(
        emp.employee_id,
        eligible_for_final_payroll=False,
    )

    assert result.eligible_for_final_payroll is False
    assert result.final_payroll_cutoff_date is None
    assert result.final_payroll_processed_at is processed_at


@pytest.mark.asyncio
async def test_resign_employee_response_blocks_unauthorized_final_payroll(
    db_session, monkeypatch
):
    service = HRWebService()
    employee = _employee(EmployeeStatus.ACTIVE)

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id, include_deleted=False: employee,
    )
    monkeypatch.setattr(
        HRWebService,
        "_employee_detail_context",
        lambda self, request, auth, db, employee: {"employee": employee},
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.templates.TemplateResponse",
        lambda request, template_name, context: HTMLResponse(context["error"]),
    )

    request = _make_request(
        {
            "date_of_leaving": "2026-04-25",
            "eligible_for_final_payroll": "1",
            "final_payroll_cutoff_date": "2026-04-25",
        }
    )
    auth = _make_auth(roles=["payroll_admin"])

    response = await service.resign_employee_response(
        request=request,
        employee_id=employee.employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert "Only admin, HR Director, or HR Manager can enable final payroll." in body


@pytest.mark.asyncio
async def test_update_final_payroll_response_updates_exited_employee(
    db_session, monkeypatch
):
    service = HRWebService()
    employee = _employee(EmployeeStatus.TERMINATED, leaving=date(2026, 4, 25))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id, include_deleted=False: employee,
    )

    def _capture_update(
        self,
        _employee_id,
        *,
        eligible_for_final_payroll,
        final_payroll_cutoff_date,
    ):
        captured["eligible_for_final_payroll"] = eligible_for_final_payroll
        captured["final_payroll_cutoff_date"] = final_payroll_cutoff_date
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_final_payroll_settings",
        _capture_update,
    )

    request = _make_request(
        {
            "eligible_for_final_payroll": "1",
            "final_payroll_cutoff_date": "2026-04-30",
        }
    )
    auth = _make_auth(roles=["hr_manager"])

    response = await service.update_final_payroll_response(
        request=request,
        employee_id=employee.employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["eligible_for_final_payroll"] is True
    assert captured["final_payroll_cutoff_date"] == date(2026, 4, 30)


@pytest.mark.asyncio
async def test_update_final_payroll_response_accepts_unchecked_checkbox(
    db_session, monkeypatch
):
    service = HRWebService()
    employee = _employee(EmployeeStatus.RESIGNED, leaving=date(2026, 4, 25))
    employee.eligible_for_final_payroll = True
    employee.final_payroll_cutoff_date = date(2026, 4, 30)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.get_employee",
        lambda self, _employee_id, include_deleted=False: employee,
    )

    def _capture_update(
        self,
        _employee_id,
        *,
        eligible_for_final_payroll,
        final_payroll_cutoff_date,
    ):
        captured["eligible_for_final_payroll"] = eligible_for_final_payroll
        captured["final_payroll_cutoff_date"] = final_payroll_cutoff_date
        return employee

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmployeeService.update_final_payroll_settings",
        _capture_update,
    )

    request = _make_request({"final_payroll_cutoff_date": "2026-04-30"})
    auth = _make_auth(roles=["hr_manager"])

    response = await service.update_final_payroll_response(
        request=request,
        employee_id=employee.employee_id,
        auth=auth,
        db=db_session,
    )

    assert response.status_code == 303
    assert captured["eligible_for_final_payroll"] is False
    assert captured["final_payroll_cutoff_date"] is None


def test_api_update_employee_blocks_unauthorized_final_payroll_cutoff_change(
    monkeypatch,
):
    employee = _employee(EmployeeStatus.TERMINATED, leaving=date(2026, 4, 25))

    monkeypatch.setattr(
        "app.api.people.hr.EmployeeService.update_employee",
        lambda self, _employee_id, _data: employee,
    )

    with pytest.raises(HTTPException) as excinfo:
        update_employee_api(
            employee_id=employee.employee_id,
            payload=EmployeeUpdate(final_payroll_cutoff_date=date(2026, 4, 30)),
            auth={"roles": ["payroll_admin"]},
            organization_id=employee.organization_id,
            db=SimpleNamespace(),
        )

    assert excinfo.value.status_code == 403
    assert "Only admin, HR Director, or HR Manager" in excinfo.value.detail
