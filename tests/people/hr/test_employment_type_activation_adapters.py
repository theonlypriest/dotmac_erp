from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jinja2 import Environment
from starlette.requests import Request

from app.api.people import hr as api_routes
from app.schemas.people.hr import EmploymentTypeUpdate
from app.services.people.hr.employment_types import EmploymentTypeView
from app.services.people.hr.web.employee_web import HRWebService
from app.web.deps import WebAuthContext
from app.web.people.hr import organization as web_routes

ROOT = Path(__file__).resolve().parents[3]


def _permission_for(route, parameter: str) -> str:
    dependency = inspect.signature(route).parameters[parameter].default.dependency
    closure = inspect.getclosurevars(dependency)
    permission = closure.nonlocals.get("permission_key")
    return str(permission or closure.nonlocals["permission"])


def test_employment_type_api_routes_have_exact_permissions() -> None:
    assert {
        route.__name__: _permission_for(route, "_auth")
        for route in (
            api_routes.list_employment_types,
            api_routes.create_employment_type,
            api_routes.get_employment_type,
            api_routes.update_employment_type,
            api_routes.delete_employment_type,
        )
    } == {
        "list_employment_types": "hr:employment_types:read",
        "create_employment_type": "hr:employment_types:manage",
        "get_employment_type": "hr:employment_types:read",
        "update_employment_type": "hr:employment_types:manage",
        "delete_employment_type": "hr:employment_types:manage",
    }


def test_employment_type_web_routes_have_exact_permissions() -> None:
    assert {
        route.__name__: _permission_for(route, "auth")
        for route in (
            web_routes.list_employment_types,
            web_routes.new_employment_type_form,
            web_routes.edit_employment_type_form,
            web_routes.create_employment_type,
            web_routes.update_employment_type,
        )
    } == {
        "list_employment_types": "hr:employment_types:read",
        "new_employment_type_form": "hr:employment_types:manage",
        "edit_employment_type_form": "hr:employment_types:manage",
        "create_employment_type": "hr:employment_types:manage",
        "update_employment_type": "hr:employment_types:manage",
    }


def test_read_only_employment_type_list_hides_create_and_edit_affordances() -> None:
    source = ROOT / "templates/people/hr/employment_types.html"
    template = source.read_text(encoding="utf-8")

    assert template.count("{% if can_manage_employment_types %}") == 3
    assert 'href="/people/hr/employment-types/new"' in template
    assert 'aria-label="Edit {{ emp_type.type_name }}"' in template


def test_api_patch_preserves_explicit_description_clear(monkeypatch) -> None:
    organization_id = uuid4()
    employment_type_id = uuid4()
    captured = []
    now = datetime.now(UTC)
    view = SimpleNamespace(
        employment_type_id=employment_type_id,
        organization_id=organization_id,
        type_code="FULL_TIME",
        type_name="Full Time",
        description=None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    class _Owner:
        def __init__(self, db, scoped_organization_id, principal):
            assert db is not None
            assert scoped_organization_id == organization_id
            assert principal is None

        def update_employment_type(self, row_id, payload):
            assert row_id == employment_type_id
            captured.append(payload)
            return view

    monkeypatch.setattr(api_routes, "EmploymentTypeService", _Owner)

    result = api_routes.update_employment_type(
        employment_type_id,
        EmploymentTypeUpdate(description=None),
        organization_id,
        {},
        SimpleNamespace(),
    )

    assert result.description is None
    assert captured[0].description is None
    assert captured[0].description_is_set is True


def test_api_delete_is_a_204_deactivation(monkeypatch) -> None:
    organization_id = uuid4()
    employment_type_id = uuid4()
    person_id = uuid4()
    deactivated = []

    class _Owner:
        def __init__(self, db, scoped_organization_id, principal):
            assert db is not None
            assert scoped_organization_id == organization_id
            assert principal.id == person_id

        def deactivate_employment_type(self, row_id):
            deactivated.append(row_id)

    monkeypatch.setattr(api_routes, "EmploymentTypeService", _Owner)

    result = api_routes.delete_employment_type(
        employment_type_id,
        organization_id,
        {"person_id": str(person_id)},
        SimpleNamespace(),
    )
    route = next(
        route
        for route in api_routes.router.routes
        if route.endpoint is api_routes.delete_employment_type
    )

    assert result is None
    assert deactivated == [employment_type_id]
    assert route.status_code == 204


@pytest.mark.asyncio
async def test_web_edit_preserves_clear_and_unchecked_active_box(monkeypatch) -> None:
    organization_id = uuid4()
    employment_type_id = uuid4()
    captured = []
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/people/hr/employment-types/{employment_type_id}/edit",
            "headers": [],
        }
    )
    request.state.csrf_form = {
        "type_code": "FULL_TIME",
        "type_name": "Full Time",
        "description": "",
    }
    auth = WebAuthContext(
        is_authenticated=True,
        person_id=uuid4(),
        organization_id=organization_id,
        roles=[],
        scopes=["hr:employment_types:manage"],
    )

    class _Owner:
        def __init__(self, db, scoped_organization_id, principal):
            assert db is not None
            assert scoped_organization_id == organization_id
            assert principal.id == auth.person_id

        def update_employment_type(self, row_id, payload):
            assert row_id == employment_type_id
            captured.append(payload)

    monkeypatch.setattr(web_routes, "EmploymentTypeService", _Owner)

    response = await web_routes.update_employment_type(
        request, str(employment_type_id), auth, SimpleNamespace()
    )

    assert response.status_code == 303
    assert captured[0].description is None
    assert captured[0].description_is_set is True
    assert captured[0].is_active is False


def test_organization_service_no_longer_owns_employment_type_decisions() -> None:
    source = (ROOT / "app/services/people/hr/organization.py").read_text(
        encoding="utf-8"
    )

    for method in (
        "list_employment_types",
        "get_employment_type",
        "get_employment_type_by_code",
        "create_employment_type",
        "update_employment_type",
        "delete_employment_type",
    ):
        assert f"def {method}(" not in source


def _employment_type_view(
    *,
    organization_id,
    name: str,
    is_active: bool,
    employment_type_id=None,
) -> EmploymentTypeView:
    now = datetime.now(UTC)
    return EmploymentTypeView(
        employment_type_id=employment_type_id or uuid4(),
        organization_id=organization_id,
        type_code=name.upper().replace(" ", "_"),
        type_name=name,
        description=None,
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )


def test_employee_options_include_complete_catalogue_and_inactive_current_once(
    monkeypatch,
) -> None:
    organization_id = uuid4()
    current = _employment_type_view(
        organization_id=organization_id,
        name="Alpha Current",
        is_active=False,
    )
    active_options = [
        _employment_type_view(
            organization_id=organization_id,
            name=f"Type {index:04d}",
            is_active=True,
        )
        for index in range(1001)
    ]
    beyond_legacy_cap = active_options[-1]
    captured: dict[str, object] = {"get_calls": []}

    class _Owner:
        def __init__(self, db, scoped_organization_id):
            assert db is not None
            assert scoped_organization_id == organization_id

        def iter_all(self, active=None):
            captured["active"] = active
            return tuple(reversed(active_options))

        def get_employment_type(self, employment_type_id):
            captured["get_calls"].append(employment_type_id)
            return current

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmploymentTypeService", _Owner
    )

    options = HRWebService._list_employee_employment_types(
        SimpleNamespace(), organization_id, current.employment_type_id
    )

    assert len(options) == 1002
    assert beyond_legacy_cap in options
    assert options[0] is current
    assert [option.type_name for option in options[1:]] == [
        f"Type {index:04d}" for index in range(1001)
    ]
    assert options.count(current) == 1
    assert captured == {
        "get_calls": [current.employment_type_id],
        "active": True,
    }


def test_every_employee_employment_type_surface_uses_the_complete_owner_helper() -> (
    None
):
    for method in (
        HRWebService.list_employees_response,
        HRWebService.employee_new_form_response,
        HRWebService.employee_edit_form_response,
    ):
        source = inspect.getsource(method)
        assert "self._list_employee_employment_types(" in source
        assert ".list_employment_types(" not in source


def test_employee_edit_marks_inactive_current_selected_and_enabled() -> None:
    organization_id = uuid4()
    current = _employment_type_view(
        organization_id=organization_id,
        name="Full Time",
        is_active=False,
    )
    source = (ROOT / "templates/people/hr/employee_form.html").read_text(
        encoding="utf-8"
    )
    option_loop = re.search(
        r"{% for emp_type in employment_types %}.*?{% endfor %}",
        source,
        flags=re.DOTALL,
    )
    assert option_loop is not None

    rendered = (
        Environment(autoescape=True)
        .from_string(option_loop.group(0))
        .render(
            employee=SimpleNamespace(
                employment_type_id=current.employment_type_id,
            ),
            employment_types=[current],
            form_data={},
        )
    )
    option = re.search(r"<option\b.*?</option>", rendered, flags=re.DOTALL)

    assert option is not None
    option_markup = option.group(0)
    assert "selected" in option_markup
    assert "Full Time (Inactive — current)" in " ".join(option_markup.split())
    assert "disabled" not in option_markup


def test_employee_service_does_not_eagerload_legacy_employment_type() -> None:
    source = (ROOT / "app/services/people/hr/employees.py").read_text(encoding="utf-8")

    assert "selectinload(Employee.employment_type)" not in source
    assert "joinedload(Employee.employment_type)" not in source
