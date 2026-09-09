from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.notification import NotificationChannel
from app.tools import requeue_notification_emails as replay
from tests._helpers.session_mocks import org_session_context


def test_requeue_requires_exact_eligible_notification_ids(monkeypatch) -> None:
    org_id = uuid4()
    ready_id = uuid4()
    inactive_id = uuid4()
    ready = SimpleNamespace(
        notification_id=ready_id,
        channel=NotificationChannel.BOTH,
        recipient=SimpleNamespace(email="ready@example.com"),
        email_sent=True,
        email_sent_at="previously-sent",
        email_retry_count=3,
        email_next_retry_at="later",
        email_dead_lettered=True,
    )
    inactive = SimpleNamespace(
        notification_id=inactive_id,
        channel=NotificationChannel.EMAIL,
        recipient=SimpleNamespace(email="inactive@example.com"),
        email_sent=True,
    )
    db = MagicMock()
    db.scalars.return_value.all.return_value = [ready, inactive]

    monkeypatch.setattr(
        replay,
        "session_for_org",
        org_session_context(db),
    )
    monkeypatch.setattr(
        replay,
        "person_can_receive_email",
        lambda person: person.email == "ready@example.com",
    )

    replayable, rejected = replay.requeue_notification_emails(
        org_id,
        [ready_id, inactive_id],
        execute=True,
    )

    assert replayable == [ready_id]
    assert rejected == {inactive_id: "recipient is not eligible for email"}
    assert ready.email_sent is False
    assert ready.email_sent_at is None
    assert ready.email_retry_count == 0
    assert ready.email_next_retry_at is None
    assert ready.email_dead_lettered is False
    db.commit.assert_called_once()
