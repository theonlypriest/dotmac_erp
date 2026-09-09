"""ERP -> dotmac_sub staff sync — sync_employee decision logic (hermetic)."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import settings
from app.models.people.hr.employee import EmployeeStatus
from app.services.dotmac_sub import staff_sync


class FakeClient:
    def __init__(self, existing: dict | None = None):
        self.existing = existing
        self.created = []
        self.active_calls = []
        self.role_calls = []
        self.department_calls = []

    def get_staff_account(self, email):
        return self.existing

    def create_staff_account(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "acc-123", "email": kwargs["email"], "created": True}

    def set_staff_account_active(self, account_id, *, is_active):
        self.active_calls.append((account_id, is_active))
        return {"id": account_id, "is_active": is_active}

    def set_staff_account_roles(self, account_id, *, roles):
        self.role_calls.append((account_id, roles))
        return {"id": account_id, "roles": roles}

    def sync_staff_account_erp_department(self, account_id, **kwargs):
        self.department_calls.append((account_id, kwargs))
        return {"id": account_id, **kwargs}

    def close(self):
        pass


def _employee(
    status,
    *,
    email="tech@dotmac.io",
    account_id=None,
    access_enabled=True,
    roles=None,
    department=None,
):
    department_id = getattr(department, "department_id", None)
    return SimpleNamespace(
        employee_id=uuid4(),
        organization_id=uuid4(),
        employee_code="EMP-1",
        status=status,
        department_id=department_id,
        department=department,
        work_email=email,
        personal_email=None,
        first_name="Field",
        last_name="Tech",
        dotmac_sub_account_id=account_id,
        dotmac_sub_staff_synced_at=None,
        dotmac_sub_access_enabled=access_enabled,
        dotmac_sub_roles=roles or ["staff"],
    )


@pytest.fixture(autouse=True)
def _enable_staff_sync(monkeypatch):
    monkeypatch.setattr(settings, "dotmac_sub_staff_sync_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "dotmac_sub_staff_default_role", "staff", raising=False
    )


def test_active_employee_without_account_is_created_and_invited():
    client = FakeClient(existing=None)
    emp = _employee(EmployeeStatus.ACTIVE)

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "created"
    assert emp.dotmac_sub_account_id == "acc-123"
    assert emp.dotmac_sub_staff_synced_at is not None
    assert client.created[0]["email"] == "tech@dotmac.io"
    assert client.created[0]["send_invite"] is True
    assert client.created[0]["role"] == "staff"
    assert client.created[0]["roles"] == ["staff"]
    assert client.department_calls == [
        (
            "acc-123",
            {
                "erp_employee_id": str(emp.employee_id),
                "employee_code": "EMP-1",
                "erp_organization_id": str(emp.organization_id),
                "department": None,
            },
        )
    ]


def test_active_employee_with_inactive_account_projects_reactivation():
    client = FakeClient(existing={"id": "acc-9", "is_active": False})
    emp = _employee(EmployeeStatus.ACTIVE)

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "reactivation_projected"
    assert client.active_calls == []
    assert client.role_calls == [("acc-9", ["staff"])]
    assert client.department_calls == [
        (
            "acc-9",
            {
                "erp_employee_id": str(emp.employee_id),
                "employee_code": "EMP-1",
                "erp_organization_id": str(emp.organization_id),
                "department": None,
            },
        )
    ]
    assert not client.created


def test_terminated_employee_account_is_disabled():
    client = FakeClient(existing={"id": "acc-9", "is_active": True})
    emp = _employee(EmployeeStatus.TERMINATED, account_id="acc-9")

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "disabled"
    assert client.active_calls == [("acc-9", False)]
    assert client.department_calls == [
        (
            "acc-9",
            {
                "erp_employee_id": str(emp.employee_id),
                "employee_code": "EMP-1",
                "erp_organization_id": str(emp.organization_id),
                "department": None,
            },
        )
    ]


def test_terminated_employee_without_account_is_skipped():
    client = FakeClient(existing=None)
    emp = _employee(EmployeeStatus.TERMINATED)

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "skipped"
    assert not client.active_calls


def test_active_employee_without_sub_access_is_disabled():
    client = FakeClient(existing={"id": "acc-9", "is_active": True})
    emp = _employee(
        EmployeeStatus.ACTIVE,
        account_id="acc-9",
        access_enabled=False,
    )

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "disabled"
    assert client.active_calls == [("acc-9", False)]
    assert not client.role_calls


def test_active_employee_without_account_or_access_is_not_created():
    client = FakeClient(existing=None)
    emp = _employee(EmployeeStatus.ACTIVE, access_enabled=False)

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result == {
        "action": "skipped",
        "reason": "dotmac_sub access not granted",
    }
    assert not client.created
    assert not client.department_calls


def test_draft_employee_with_revoked_access_and_linked_account_is_disabled():
    client = FakeClient(existing=None)
    emp = _employee(
        EmployeeStatus.DRAFT,
        email=None,
        account_id="acc-9",
        access_enabled=False,
    )

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "disabled"
    assert client.active_calls == [("acc-9", False)]
    assert client.department_calls == [
        (
            "acc-9",
            {
                "erp_employee_id": str(emp.employee_id),
                "employee_code": "EMP-1",
                "erp_organization_id": str(emp.organization_id),
                "department": None,
            },
        )
    ]


def test_existing_account_roles_are_synchronized():
    client = FakeClient(existing={"id": "acc-9", "is_active": True})
    emp = _employee(
        EmployeeStatus.ACTIVE,
        account_id="acc-9",
        roles=["field_technician", "support_agent"],
    )

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "noop"
    assert client.role_calls == [("acc-9", ["field_technician", "support_agent"])]


def test_existing_account_department_is_synchronized():
    department = SimpleNamespace(
        department_id=uuid4(),
        organization_id=None,
        department_code="OPS",
        department_name="Operations",
    )
    client = FakeClient(existing={"id": "acc-9", "is_active": True})
    emp = _employee(
        EmployeeStatus.ACTIVE,
        account_id="acc-9",
        department=department,
    )
    department.organization_id = emp.organization_id

    result = staff_sync.sync_employee(None, emp, client=client)

    assert result["action"] == "noop"
    assert client.department_calls == [
        (
            "acc-9",
            {
                "erp_employee_id": str(emp.employee_id),
                "employee_code": "EMP-1",
                "erp_organization_id": str(emp.organization_id),
                "department": {
                    "department_id": str(department.department_id),
                    "department_code": "OPS",
                    "department_name": "Operations",
                },
            },
        )
    ]


def test_draft_and_missing_email_are_skipped():
    client = FakeClient()
    assert (
        staff_sync.sync_employee(None, _employee(EmployeeStatus.DRAFT), client=client)[
            "action"
        ]
        == "skipped"
    )
    assert (
        staff_sync.sync_employee(
            None, _employee(EmployeeStatus.ACTIVE, email=None), client=client
        )["action"]
        == "skipped"
    )


def test_disabled_flag_skips_everything(monkeypatch):
    monkeypatch.setattr(settings, "dotmac_sub_staff_sync_enabled", False, raising=False)
    client = FakeClient()
    result = staff_sync.sync_employee(
        None, _employee(EmployeeStatus.ACTIVE), client=client
    )
    assert result["action"] == "skipped"
    assert not client.created


def test_reconcile_reprimes_per_employee_and_isolates_errors(monkeypatch):
    """Reconcile must re-prime tenant context every iteration (commit resets
    the SET LOCAL RLS context) and isolate a single employee's failure — the
    ObjectDeletedError bug that made the first prod backfill create 0 of 102.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr(settings, "dotmac_sub_staff_sync_enabled", True, raising=False)

    org_id = uuid4()
    ids = [uuid4(), uuid4(), uuid4()]
    codes = ["EMP-1", "EMP-2", "EMP-3"]

    # DB: execute(...).all() returns the id/code snapshot; get() returns a stub
    # employee; commit/rollback are counted.
    db = MagicMock()
    db.execute.return_value.all.return_value = list(zip(ids, codes, strict=False))
    db.get.side_effect = lambda model, pk: SimpleNamespace(employee_id=pk)

    primed = []
    monkeypatch.setattr(
        staff_sync, "prime_tenant_context", lambda s, o: primed.append(o)
    )

    cfg = MagicMock()
    cfg.is_configured.return_value = True
    monkeypatch.setattr(
        staff_sync.DotmacSubConfig, "for_org", classmethod(lambda cls, d, o: cfg)
    )
    monkeypatch.setattr(
        staff_sync,
        "DotmacSubClient",
        lambda c: MagicMock(),  # MagicMock supports the context-manager protocol
    )

    # EMP-2 raises; EMP-1 and EMP-3 succeed.
    def fake_sync(d, employee, *, client):
        if employee.employee_id == ids[1]:
            raise RuntimeError("boom")
        return {"action": "created"}

    monkeypatch.setattr(staff_sync, "sync_employee", fake_sync)

    result = staff_sync.reconcile_staff_accounts(db, org_id)

    # Re-primed once per employee (not once total).
    assert primed == [org_id, org_id, org_id]
    # The failure was isolated: 2 created, 1 error, still "success=False".
    assert result["counts"] == {"created": 2}
    assert result["errors"] == ["EMP-2: boom"]
    assert result["success"] is False
    assert db.rollback.call_count == 1
    assert db.commit.call_count == 2
