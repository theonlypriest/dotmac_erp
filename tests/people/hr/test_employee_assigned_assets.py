from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.people.hr.web.employee_web import HRWebService
from app.web.deps import WebAuthContext


class _ScalarResult:
    def __iter__(self):
        return iter([])

    def all(self):
        return []


class _DB:
    def __init__(self, person):
        self.person = person

    def get(self, _model, _id):
        return self.person

    def scalar(self, _query):
        return None

    def scalars(self, _query):
        return _ScalarResult()


def _auth(org_id):
    auth = WebAuthContext(
        is_authenticated=True,
        person_id=uuid4(),
        organization_id=org_id,
        roles=["hr_manager"],
        scopes=["hr:access"],
    )
    return auth


def _employee(org_id, person_id):
    return SimpleNamespace(
        employee_id=uuid4(),
        organization_id=org_id,
        person_id=person_id,
        department_id=None,
        designation_id=None,
        grade_id=None,
        employment_type_id=None,
        expense_approver_id=None,
    )


def test_employee_detail_context_includes_assigned_assets_when_allowed(monkeypatch):
    org_id = uuid4()
    person_id = uuid4()
    employee = _employee(org_id, person_id)
    employee.employment_type_id = uuid4()
    employment_type = SimpleNamespace(
        employment_type_id=employee.employment_type_id,
        type_name="Full Time",
    )
    auth = _auth(org_id)
    auth.has_module_access = lambda module: module == "fixed_assets"
    assigned_asset = SimpleNamespace(asset_id=uuid4(), asset_name="Laptop")
    captured = {}

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.base_context",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.get_recent_activity_for_record",
        lambda *_args, **_kwargs: [],
    )

    class _EmploymentTypeOwner:
        def __init__(self, _db, organization_id):
            assert organization_id == org_id

        def get_employment_type(self, employment_type_id):
            assert employment_type_id == employee.employment_type_id
            return employment_type

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.EmploymentTypeService",
        _EmploymentTypeOwner,
    )

    class _Resolver:
        def __init__(self, _db):
            pass

        def get_manager(self, _employee_id, _org_id):
            return None

        def get_position_chain(self, _employee_id, _org_id):
            return []

    class _LifecycleService:
        def __init__(self, _db):
            pass

        def get_onboarding_for_employee(self, _org_id, _employee_id):
            return None

    def _list_assigned_assets(_db, organization_id, employee_id):
        captured["organization_id"] = organization_id
        captured["employee_id"] = employee_id
        return [assigned_asset]

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrgResolver", _Resolver
    )
    monkeypatch.setattr(
        "app.services.people.hr.lifecycle.LifecycleService",
        _LifecycleService,
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.list_employee_assigned_assets",
        _list_assigned_assets,
    )

    context = HRWebService()._employee_detail_context(
        request=SimpleNamespace(),
        auth=auth,
        db=_DB(SimpleNamespace(id=person_id, name="Ada Lovelace")),
        employee=employee,
    )

    assert context["can_view_assigned_assets"] is True
    assert context["assigned_assets"] == [assigned_asset]
    assert context["employment_type"] is employment_type
    assert captured == {
        "organization_id": org_id,
        "employee_id": employee.employee_id,
    }


def test_employee_detail_context_skips_assigned_assets_without_access(monkeypatch):
    org_id = uuid4()
    person_id = uuid4()
    employee = _employee(org_id, person_id)
    auth = _auth(org_id)
    auth.has_module_access = lambda _module: False

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.base_context",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.get_recent_activity_for_record",
        lambda *_args, **_kwargs: [],
    )

    class _Resolver:
        def __init__(self, _db):
            pass

        def get_manager(self, _employee_id, _org_id):
            return None

        def get_position_chain(self, _employee_id, _org_id):
            return []

    class _LifecycleService:
        def __init__(self, _db):
            pass

        def get_onboarding_for_employee(self, _org_id, _employee_id):
            return None

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.OrgResolver", _Resolver
    )
    monkeypatch.setattr(
        "app.services.people.hr.lifecycle.LifecycleService",
        _LifecycleService,
    )

    def _list_assigned_assets(*_args):
        raise AssertionError("assigned assets should not be loaded without access")

    monkeypatch.setattr(
        "app.services.people.hr.web.employee_web.list_employee_assigned_assets",
        _list_assigned_assets,
    )

    context = HRWebService()._employee_detail_context(
        request=SimpleNamespace(),
        auth=auth,
        db=_DB(SimpleNamespace(id=person_id, name="Ada Lovelace")),
        employee=employee,
    )

    assert context["can_view_assigned_assets"] is False
    assert context["assigned_assets"] == []
