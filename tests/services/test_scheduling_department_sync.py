from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.people.hr.employee_types import EmployeeUpdateData
from app.services.people.hr.employees import EmployeeService
from app.services.people.scheduling.scheduling_service import SchedulingService


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def test_sync_employee_department_moves_active_assignment() -> None:
    old_department_id = uuid4()
    new_department_id = uuid4()
    assignment = SimpleNamespace(department_id=old_department_id)
    db = MagicMock()
    db.scalars.return_value = _ScalarResult([assignment])

    result = SchedulingService(db).sync_employee_department(
        org_id=uuid4(),
        employee_id=uuid4(),
        new_department_id=new_department_id,
    )

    assert assignment.department_id == new_department_id
    assert result == {"assignments_updated": 1, "assignments_ended": 0}
    db.flush.assert_called_once()


def test_sync_employee_department_ends_assignment_when_department_cleared() -> None:
    assignment = SimpleNamespace(is_active=True, effective_to=None)
    db = MagicMock()
    db.scalars.return_value = _ScalarResult([assignment])

    result = SchedulingService(db).sync_employee_department(
        org_id=uuid4(),
        employee_id=uuid4(),
        new_department_id=None,
    )

    assert assignment.is_active is False
    assert assignment.effective_to is not None
    assert result == {"assignments_updated": 0, "assignments_ended": 1}
    db.flush.assert_called_once()


def test_update_employee_syncs_scheduling_when_department_changes(monkeypatch) -> None:
    old_department_id = uuid4()
    new_department_id = uuid4()
    employee = SimpleNamespace(
        employee_id=uuid4(),
        organization_id=uuid4(),
        employee_code="EMP-001",
        department_id=old_department_id,
        status="ACTIVE",
        dotmac_sub_access_enabled=False,
        dotmac_sub_roles=["staff"],
        expense_approver_id=None,
        version=1,
    )
    service = EmployeeService.__new__(EmployeeService)
    service.db = MagicMock()
    service.organization_id = employee.organization_id
    service.principal = None
    monkeypatch.setattr(service, "get_employee", lambda employee_id: employee)
    monkeypatch.setattr(service, "_validate_org_reference", lambda *args: None)
    monkeypatch.setattr(
        "app.services.people.hr.employees.fire_audit_event", lambda **kwargs: None
    )
    synced = []
    monkeypatch.setattr(
        EmployeeService,
        "_sync_scheduling_department",
        lambda self, synced_employee: synced.append(synced_employee.department_id),
    )

    updated = service.update_employee(
        employee.employee_id,
        EmployeeUpdateData(department_id=new_department_id),
    )

    assert updated.department_id == new_department_id
    assert synced == [new_department_id]


def test_update_employee_enqueues_staff_sync_when_department_changes(
    monkeypatch,
) -> None:
    old_department_id = uuid4()
    new_department_id = uuid4()
    employee = SimpleNamespace(
        employee_id=uuid4(),
        organization_id=uuid4(),
        employee_code="EMP-001",
        department_id=old_department_id,
        status="ACTIVE",
        dotmac_sub_access_enabled=True,
        dotmac_sub_roles=["staff"],
        expense_approver_id=None,
        version=1,
    )
    service = EmployeeService.__new__(EmployeeService)
    service.db = MagicMock()
    service.organization_id = employee.organization_id
    service.principal = None
    monkeypatch.setattr(service, "get_employee", lambda employee_id: employee)
    monkeypatch.setattr(service, "_validate_org_reference", lambda *args: None)
    monkeypatch.setattr(
        "app.services.people.hr.employees.fire_audit_event", lambda **kwargs: None
    )
    monkeypatch.setattr(
        EmployeeService, "_sync_scheduling_department", lambda *args: None
    )
    enqueued = []
    monkeypatch.setattr(
        EmployeeService,
        "_enqueue_staff_sync",
        lambda self, synced_employee: enqueued.append(synced_employee.department_id),
    )

    service.update_employee(
        employee.employee_id,
        EmployeeUpdateData(department_id=new_department_id),
    )

    assert enqueued == [new_department_id]


def test_update_employee_does_not_sync_scheduling_when_department_unchanged(
    monkeypatch,
) -> None:
    department_id = uuid4()
    employee = SimpleNamespace(
        employee_id=uuid4(),
        organization_id=uuid4(),
        employee_code="EMP-001",
        department_id=department_id,
        status="ACTIVE",
        dotmac_sub_access_enabled=False,
        dotmac_sub_roles=["staff"],
        expense_approver_id=None,
        version=1,
    )
    service = EmployeeService.__new__(EmployeeService)
    service.db = MagicMock()
    service.organization_id = employee.organization_id
    service.principal = None
    monkeypatch.setattr(service, "get_employee", lambda employee_id: employee)
    monkeypatch.setattr(service, "_validate_org_reference", lambda *args: None)
    monkeypatch.setattr(
        "app.services.people.hr.employees.fire_audit_event", lambda **kwargs: None
    )
    sync = MagicMock()
    monkeypatch.setattr(EmployeeService, "_sync_scheduling_department", sync)

    service.update_employee(
        employee.employee_id,
        EmployeeUpdateData(department_id=department_id),
    )

    sync.assert_not_called()
