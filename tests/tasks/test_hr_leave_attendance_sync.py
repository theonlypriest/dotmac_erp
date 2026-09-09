from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, PendingRollbackError

from app.services.people import leave as leave_pkg
from app.tasks import hr as hr_tasks
from tests._helpers.session_mocks import org_session_context


ORG_ID = UUID("00000000-0000-0000-0000-00000000a771")
TODAY = date(2026, 9, 1)


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Savepoint:
    def __init__(self, db: _LeaveSyncSession):
        self.db = db

    def __enter__(self):
        self.db.in_savepoint = True
        self.db.savepoints_opened += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.db.in_savepoint = False
        if exc_type is not None:
            self.db.savepoint_rollbacks += 1
            self.db.pending_attendance.clear()
        else:
            self.db.savepoint_commits += 1
        return False


class _LeaveSyncSession:
    def __init__(
        self,
        leaves,
        *,
        fail_employee_id=None,
        fail_leave_query: bool = False,
    ):
        self.leaves = list(leaves)
        self.fail_employee_id = fail_employee_id
        self.fail_leave_query = fail_leave_query
        self.pending_attendance = []
        self.persisted_attendance = []
        self.in_savepoint = False
        self.poisoned = False
        self.commit_on_poisoned = False
        self.commits = 0
        self.rollbacks = 0
        self.savepoints_opened = 0
        self.savepoint_commits = 0
        self.savepoint_rollbacks = 0
        self.status_called_after_poison = None

    def scalars(self, _statement):
        if self.fail_leave_query:
            self.poisoned = True
            raise IntegrityError("select approved leaves", {}, Exception("boom"))
        if self.poisoned:
            raise PendingRollbackError("transaction is invalid", None)
        return _ScalarResult(self.leaves)

    def scalar(self, _statement):
        if self.poisoned:
            raise PendingRollbackError("transaction is invalid", None)
        return None

    def add(self, value):
        self.pending_attendance.append(value)

    def flush(self):
        if self.poisoned:
            raise PendingRollbackError("transaction is invalid", None)
        for attendance in self.pending_attendance:
            if attendance.employee_id == self.fail_employee_id:
                if not self.in_savepoint:
                    self.poisoned = True
                raise IntegrityError("insert attendance", {}, Exception("duplicate"))
        self.persisted_attendance.extend(self.pending_attendance)
        self.pending_attendance.clear()

    def begin_nested(self):
        if self.poisoned:
            raise PendingRollbackError("transaction is invalid", None)
        return _Savepoint(self)

    def commit(self):
        if self.poisoned:
            self.commit_on_poisoned = True
            raise PendingRollbackError("transaction is invalid", None)
        self.commits += 1

    def rollback(self):
        self.poisoned = False
        self.pending_attendance.clear()
        self.rollbacks += 1


def _leave(employee_id=None, application_id=None):
    return SimpleNamespace(
        employee_id=employee_id or uuid4(),
        application_id=application_id or uuid4(),
    )


class _SuccessfulLeaveService:
    def __init__(self, db):
        self.db = db

    def get_org_today(self, _org_id):
        return TODAY

    def sync_employee_statuses_for_date(self, _org_id, *, as_of_date):
        self.db.status_called_after_poison = self.db.poisoned
        assert as_of_date == TODAY
        return {"set_on_leave": 1, "set_active": 0, "checked": 2}


class _FailingStatusLeaveService(_SuccessfulLeaveService):
    def sync_employee_statuses_for_date(self, _org_id, *, as_of_date):
        self.db.status_called_after_poison = self.db.poisoned
        assert self.db.in_savepoint is True
        raise IntegrityError("update employee status", {}, Exception("boom"))


def test_leave_attendance_flush_failure_does_not_poison_later_work(monkeypatch):
    failing_employee = uuid4()
    successful_employee = uuid4()
    db = _LeaveSyncSession(
        [_leave(failing_employee), _leave(successful_employee)],
        fail_employee_id=failing_employee,
    )

    monkeypatch.setattr(hr_tasks, "_list_organization_ids", lambda: [ORG_ID])
    monkeypatch.setattr(hr_tasks, "session_for_org", org_session_context(db))
    monkeypatch.setattr(leave_pkg, "LeaveService", _SuccessfulLeaveService)

    result = hr_tasks.sync_leave_attendance()

    assert result["synced"] == 1
    assert result["already_marked"] == 0
    assert result["status_set_on_leave"] == 1
    assert result["status_set_active"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["employee_id"] == str(failing_employee)
    assert db.status_called_after_poison is False
    assert db.commit_on_poisoned is False
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.savepoint_rollbacks == 1
    assert len(db.persisted_attendance) == 1
    assert db.persisted_attendance[0].employee_id == successful_employee


def test_employee_status_failure_is_rolled_back_without_losing_attendance(
    monkeypatch,
):
    employee_id = uuid4()
    db = _LeaveSyncSession([_leave(employee_id)])

    monkeypatch.setattr(hr_tasks, "_list_organization_ids", lambda: [ORG_ID])
    monkeypatch.setattr(hr_tasks, "session_for_org", org_session_context(db))
    monkeypatch.setattr(leave_pkg, "LeaveService", _FailingStatusLeaveService)

    result = hr_tasks.sync_leave_attendance()

    assert result["synced"] == 1
    assert result["status_set_on_leave"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["phase"] == "employee_status"
    assert db.status_called_after_poison is False
    assert db.commit_on_poisoned is False
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.savepoint_rollbacks == 1
    assert len(db.persisted_attendance) == 1


def test_org_level_database_failure_rolls_back_instead_of_committing_poisoned_session(
    monkeypatch,
):
    db = _LeaveSyncSession([], fail_leave_query=True)

    monkeypatch.setattr(hr_tasks, "_list_organization_ids", lambda: [ORG_ID])
    monkeypatch.setattr(hr_tasks, "session_for_org", org_session_context(db))
    monkeypatch.setattr(leave_pkg, "LeaveService", _SuccessfulLeaveService)

    result = hr_tasks.sync_leave_attendance()

    assert result["synced"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["organization_id"] == str(ORG_ID)
    assert db.commit_on_poisoned is False
    assert db.commits == 0
    assert db.rollbacks == 1
