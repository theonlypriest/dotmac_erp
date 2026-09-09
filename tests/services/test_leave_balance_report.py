from unittest.mock import MagicMock
from uuid import uuid4

from app.models.people.hr.employee import EmployeeStatus
from app.services.people.leave.leave_service import LeaveService


def test_leave_balance_report_queries_active_and_on_leave_employees():
    db = MagicMock()
    db.execute.return_value.all.return_value = []
    service = LeaveService(db)

    service.get_leave_balance_report(uuid4(), year=2026)

    statement = db.execute.call_args.args[0]
    eligible_statuses = next(
        value
        for value in statement.compile().params.values()
        if isinstance(value, list)
        and all(isinstance(status, EmployeeStatus) for status in value)
    )
    assert eligible_statuses == [EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE]
