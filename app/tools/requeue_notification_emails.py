"""Safely requeue notification emails identified as suppressed in worker logs.

This tool deliberately requires exact notification ids. The historical worker
stored both successful sends and false RLS-based suppressions as
``email_sent=True``, so a date-range replay cannot distinguish them safely.

Examples::

    python -m app.tools.requeue_notification_emails \
      --organization-id <uuid> --notification-id <uuid> --dry-run

    python -m app.tools.requeue_notification_emails \
      --organization-id <uuid> --notification-id <uuid> --execute
"""

from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import select

from app.db.session_context import session_for_org
from app.models.notification import Notification, NotificationChannel
from app.services.email import person_can_receive_email

_EMAIL_CHANNELS = {
    NotificationChannel.EMAIL,
    NotificationChannel.BOTH,
    NotificationChannel.ALL,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=UUID, required=True)
    parser.add_argument(
        "--notification-id",
        type=UUID,
        action="append",
        required=True,
        dest="notification_ids",
        help="Exact id copied from a 'Suppressing notification email' log line",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def requeue_notification_emails(
    organization_id: UUID,
    notification_ids: list[UUID],
    *,
    execute: bool,
) -> tuple[list[UUID], dict[UUID, str]]:
    """Validate exact ids and optionally reset their email delivery state."""
    unique_ids = list(dict.fromkeys(notification_ids))
    replayable: list[UUID] = []
    rejected: dict[UUID, str] = {}

    with session_for_org(organization_id) as db:
        notifications = {
            row.notification_id: row
            for row in db.scalars(
                select(Notification).where(
                    Notification.organization_id == organization_id,
                    Notification.notification_id.in_(unique_ids),
                )
            ).all()
        }

        for notification_id in unique_ids:
            notification = notifications.get(notification_id)
            if notification is None:
                rejected[notification_id] = "not found in organization"
                continue
            if notification.channel not in _EMAIL_CHANNELS:
                rejected[notification_id] = "notification is not email-enabled"
                continue
            if not notification.email_sent:
                rejected[notification_id] = "email is already pending"
                continue
            if not person_can_receive_email(notification.recipient):
                rejected[notification_id] = "recipient is not eligible for email"
                continue

            replayable.append(notification_id)
            if execute:
                notification.email_sent = False
                notification.email_sent_at = None
                notification.email_retry_count = 0
                notification.email_next_retry_at = None
                notification.email_dead_lettered = False

        if execute:
            db.commit()
        else:
            db.rollback()

    return replayable, rejected


def main() -> None:
    args = _parse_args()
    replayable, rejected = requeue_notification_emails(
        args.organization_id,
        args.notification_ids,
        execute=args.execute,
    )
    mode = "REQUEUED" if args.execute else "DRY RUN"
    print(f"{mode}: {len(replayable)} notification email(s)")
    for notification_id in replayable:
        print(f"  ready: {notification_id}")
    for notification_id, reason in rejected.items():
        print(f"  skipped: {notification_id} ({reason})")


if __name__ == "__main__":
    main()
