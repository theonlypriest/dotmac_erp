from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.dotmac_sub import DotmacSubPermanentSyncError
from app.tasks import staff_sync as task_module


def test_staff_sync_permanent_error_does_not_retry() -> None:
    employee_id = uuid4()
    org_id = uuid4()
    db = MagicMock()
    db.get.return_value = SimpleNamespace(employee_id=employee_id)

    with (
        patch.object(task_module, "_resolve_org_id", return_value=org_id),
        patch.object(task_module, "session_for_org") as mock_session_for_org,
        patch.object(
            task_module.staff_sync,
            "sync_employee",
            side_effect=DotmacSubPermanentSyncError("Department is not mapped"),
        ),
        patch.object(task_module.sync_employee_staff_account, "retry") as mock_retry,
    ):
        mock_session_for_org.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_for_org.return_value.__exit__ = MagicMock(return_value=False)

        result = task_module.sync_employee_staff_account.run(
            str(employee_id), str(org_id)
        )

    assert result == {
        "success": False,
        "retryable": False,
        "error": "Department is not mapped",
    }
    mock_retry.assert_not_called()
