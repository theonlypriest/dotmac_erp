"""Tests for notification Celery tasks."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import OperationalError

from app.models.email_profile import EmailModule
from app.models.notification import EntityType

ORG_ID = uuid4()


def _operational_error() -> OperationalError:
    return OperationalError("SELECT", {}, Exception("db gone"))


def _build_notification(
    *,
    email: str | None = "user@example.com",
    entity_type: EntityType = EntityType.LEAVE,
    action_url: str | None = "/self",
) -> SimpleNamespace:
    return SimpleNamespace(
        notification_id="notif-1",
        recipient=SimpleNamespace(email=email),
        title="Leave update",
        message="Your request",
        action_url=action_url,
        organization_id=ORG_ID,
        channel=None,
        entity_type=entity_type,
        email_retry_count=0,
        email_next_retry_at=None,
        email_dead_lettered=False,
    )


def test_email_module_routing_covers_module_specific_profiles() -> None:
    from app.tasks.notifications import _email_module_for_notification

    cases = [
        (EntityType.TICKET, None, EmailModule.SUPPORT),
        (EntityType.EXPENSE, None, EmailModule.EXPENSE),
        (EntityType.PAYROLL, None, EmailModule.PEOPLE_PAYROLL),
        (EntityType.DISCIPLINE, None, EmailModule.PEOPLE_PAYROLL),
        (EntityType.TAX_PERIOD, None, EmailModule.FINANCE),
        (EntityType.SYSTEM, "/fleet/maintenance", EmailModule.INVENTORY_FLEET),
        (EntityType.SYSTEM, "/procurement/requests/1", EmailModule.PROCUREMENT),
        (EntityType.SYSTEM, "/pm/tasks/1", EmailModule.OPERATIONS),
        (EntityType.SYSTEM, None, EmailModule.ADMIN),
    ]

    for entity_type, action_url, expected in cases:
        notification = _build_notification(
            entity_type=entity_type,
            action_url=action_url,
        )
        assert _email_module_for_notification(notification) == expected


def test_process_pending_notification_emails_retries_on_operational_error() -> None:
    class RetryCalled(Exception):
        pass

    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch(
            "app.tasks.notifications.session_for_org",
            side_effect=_operational_error(),
        ),
        patch(
            "app.tasks.notifications.process_pending_notification_emails.retry"
        ) as mock_retry,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        mock_retry.side_effect = RetryCalled("retry")

        try:
            process_pending_notification_emails()
        except RetryCalled:
            pass

    mock_retry.assert_called_once()
    assert isinstance(
        mock_retry.call_args.kwargs["exc"],
        OperationalError,
    )


def test_process_pending_notification_emails_sends_active_notification() -> None:
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch(
            "app.tasks.notifications.send_email", return_value=True
        ) as mock_send_email,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [_build_notification()]
        db.execute.return_value = execute_result

        result = process_pending_notification_emails(batch_size=1)

        assert result["processed"] == 1
        assert result["sent"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert result["dead_letter"] == 0
        assert mock_send_email.call_args.kwargs["module"] == EmailModule.PEOPLE_PAYROLL
        assert "Review leave" in mock_send_email.call_args.kwargs["body_html"]


def test_process_pending_notification_emails_routes_ticket_to_support() -> None:
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch(
            "app.tasks.notifications.send_email", return_value=True
        ) as mock_send_email,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [
            _build_notification(entity_type=EntityType.TICKET)
        ]
        db.execute.return_value = execute_result

        result = process_pending_notification_emails(batch_size=1)

        assert result["processed"] == 1
        assert result["sent"] == 1
        assert mock_send_email.call_args.kwargs["module"] == EmailModule.SUPPORT
        assert "Open notification" in mock_send_email.call_args.kwargs["body_html"]


def test_process_pending_invoice_mention_email_uses_finance_profile() -> None:
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch(
            "app.tasks.notifications.send_email", return_value=True
        ) as mock_send_email,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [
            _build_notification(entity_type=EntityType.INVOICE)
        ]
        db.execute.return_value = execute_result

        result = process_pending_notification_emails(batch_size=1)

        assert result["sent"] == 1
        assert mock_send_email.call_args.kwargs["module"] == EmailModule.FINANCE


def test_process_pending_notification_emails_routes_employee_to_people_payroll() -> (
    None
):
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch(
            "app.tasks.notifications.send_email", return_value=True
        ) as mock_send_email,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [
            _build_notification(entity_type=EntityType.EMPLOYEE)
        ]
        db.execute.return_value = execute_result

        result = process_pending_notification_emails(batch_size=1)

        assert result["processed"] == 1
        assert result["sent"] == 1
        assert mock_send_email.call_args.kwargs["module"] == EmailModule.PEOPLE_PAYROLL


def test_process_pending_notification_emails_skips_when_email_missing() -> None:
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch("app.tasks.notifications.send_email") as mock_send_email,
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [
            _build_notification(email=None)
        ]
        db.execute.return_value = execute_result

        result = process_pending_notification_emails(batch_size=1)

        assert result["processed"] == 1
        assert result["sent"] == 0
        assert result["skipped"] == 1
        mock_send_email.assert_not_called()


def test_process_pending_notification_emails_backs_off_after_send_failure() -> None:
    with (
        patch("app.tasks.notifications.active_organization_ids", return_value=[ORG_ID]),
        patch("app.tasks.notifications.session_for_org") as mock_session_local,
        patch("app.tasks.notifications.person_can_receive_email", return_value=True),
        patch("app.tasks.notifications.send_email", return_value=False),
    ):
        from app.tasks.notifications import process_pending_notification_emails

        db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = False
        notification = _build_notification()
        db.execute.return_value.scalars.return_value.all.return_value = [notification]

        result = process_pending_notification_emails(batch_size=1)

    assert result["failed"] == 1
    assert notification.email_retry_count == 1
    assert notification.email_next_retry_at is not None
    assert notification.email_dead_lettered is False
