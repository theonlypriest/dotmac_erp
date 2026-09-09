from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.person import PersonStatus
from app.tasks import notifications as notification_tasks
from tests._helpers.session_mocks import org_session_context


def test_process_pending_notification_emails_skips_inactive_people(monkeypatch):
    recipient = SimpleNamespace(
        email="inactive@example.com",
        is_active=False,
        status=PersonStatus.inactive,
    )
    notification = SimpleNamespace(
        notification_id=uuid4(),
        recipient=recipient,
        title="Inactive recipient",
        message="This should not be emailed.",
        action_url=None,
        organization_id=uuid4(),
        email_sent=False,
        email_sent_at=None,
    )

    mock_db = MagicMock()
    mock_db.execute.return_value.scalars.return_value.all.return_value = [notification]

    monkeypatch.setattr(
        notification_tasks,
        "active_organization_ids",
        lambda: [notification.organization_id],
    )
    monkeypatch.setattr(
        notification_tasks,
        "session_for_org",
        org_session_context(mock_db),
    )

    send_calls: list[str] = []

    def _fake_send_email(*args, **kwargs):
        send_calls.append(kwargs.get("to_email") or args[1])
        return True

    monkeypatch.setattr(notification_tasks, "send_email", _fake_send_email)

    result = notification_tasks.process_pending_notification_emails(batch_size=10)

    assert result["processed"] == 1
    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert send_calls == []
    assert notification.email_sent is True
    assert notification.email_sent_at is not None
    mock_db.commit.assert_called_once()
