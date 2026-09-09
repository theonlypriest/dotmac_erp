"""
Notification delivery background tasks.

Handles:
- Delivery of pending email notifications stored in public.notification
- Delivery of pending Nextcloud Talk notifications
"""

import html
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from typing import TypedDict

from celery import shared_task
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db.session_context import cross_org_session, session_for_org
from app.models.email_profile import EmailModule
from app.models.notification import EntityType, Notification, NotificationChannel
from app.services.email import person_can_receive_email, send_email
from app.tenant_catalog import active_organization_ids

logger = logging.getLogger(__name__)

_DB_RETRYABLE_ERRORS = (OperationalError, ProgrammingError)


def _task_db_session():
    """Yield a task DB session and ensure it is rolled back/closed on any error."""
    return cross_org_session()


# Notifications older than this are considered permanently failed and will not
# be retried.  They remain in the database with email_sent=False for operator
# inspection but are excluded from the processing query.
_DEAD_LETTER_AGE = timedelta(days=3)
_EMAIL_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
)
_EMAIL_MAX_ATTEMPTS = len(_EMAIL_RETRY_DELAYS)


class NotificationEmailDispatchResults(TypedDict):
    processed: int
    sent: int
    skipped: int
    failed: int
    dead_letter: int


def _email_module_for_notification(notification: Notification) -> EmailModule:
    """Route notification emails through the appropriate email profile."""
    if notification.entity_type == EntityType.TICKET:
        return EmailModule.SUPPORT
    if notification.entity_type == EntityType.EXPENSE:
        return EmailModule.EXPENSE
    if notification.entity_type in {
        EntityType.LEAVE,
        EntityType.ATTENDANCE,
        EntityType.PAYROLL,
        EntityType.EMPLOYEE,
        EntityType.DISCIPLINE,
    }:
        return EmailModule.PEOPLE_PAYROLL
    if notification.entity_type in {
        EntityType.FISCAL_PERIOD,
        EntityType.TAX_PERIOD,
        EntityType.BANK_RECONCILIATION,
        EntityType.INVOICE,
        EntityType.SUBLEDGER,
    }:
        return EmailModule.FINANCE

    # Several older producers use SYSTEM for module-owned notifications.
    # Preserve their schema while still honoring module-specific SMTP routes.
    path = urlparse(notification.action_url or "").path.lower()
    path_routes = (
        (("/people", "/payroll"), EmailModule.PEOPLE_PAYROLL),
        (("/finance",), EmailModule.FINANCE),
        (("/expense",), EmailModule.EXPENSE),
        (("/support",), EmailModule.SUPPORT),
        (("/inventory", "/fleet"), EmailModule.INVENTORY_FLEET),
        (("/procurement",), EmailModule.PROCUREMENT),
        (("/pm", "/projects"), EmailModule.OPERATIONS),
    )
    for prefixes, module in path_routes:
        if path.startswith(prefixes):
            return module
    return EmailModule.ADMIN


def _email_action_label_for_notification(notification: Notification) -> str:
    """Return the email CTA label for a notification."""
    if notification.entity_type == EntityType.LEAVE:
        return "Review leave"
    return "Open notification"


def _record_email_delivery_failure(notification: Notification, now: datetime) -> bool:
    """Schedule a bounded retry and return whether delivery is terminal."""
    notification.email_retry_count += 1
    if notification.email_retry_count >= _EMAIL_MAX_ATTEMPTS:
        notification.email_dead_lettered = True
        notification.email_next_retry_at = None
        return True

    notification.email_next_retry_at = (
        now + _EMAIL_RETRY_DELAYS[notification.email_retry_count - 1]
    )
    return False


def _process_notification_email_batch(
    db,
    organization_id: UUID,
    batch_size: int,
    results: NotificationEmailDispatchResults,
) -> None:
    now = datetime.now(UTC)
    cutoff = now - _DEAD_LETTER_AGE

    dead_letter_result = db.execute(
        update(Notification)
        .where(Notification.organization_id == organization_id)
        .where(Notification.email_sent == False)  # noqa: E712
        .where(Notification.email_dead_lettered == False)  # noqa: E712
        .where(
            Notification.channel.in_(
                [
                    NotificationChannel.EMAIL,
                    NotificationChannel.BOTH,
                    NotificationChannel.ALL,
                ]
            )
        )
        .where(Notification.created_at < cutoff)
        .values(email_dead_lettered=True, email_next_retry_at=None)
    )
    rowcount = dead_letter_result.rowcount
    dead_letter_count = rowcount if isinstance(rowcount, int) else 0
    results["dead_letter"] += dead_letter_count
    if dead_letter_count:
        logger.warning(
            "Dead-letter: %d notification emails older than %s days will not "
            "be retried for org %s",
            dead_letter_count,
            _DEAD_LETTER_AGE.days,
            organization_id,
        )

    stmt = (
        select(Notification)
        .where(Notification.organization_id == organization_id)
        .where(Notification.email_sent == False)  # noqa: E712
        .where(Notification.email_dead_lettered == False)  # noqa: E712
        .where(
            (Notification.email_next_retry_at.is_(None))
            | (Notification.email_next_retry_at <= now)
        )
        .where(
            Notification.channel.in_(
                [
                    NotificationChannel.EMAIL,
                    NotificationChannel.BOTH,
                    NotificationChannel.ALL,
                ]
            )
        )
        .where(Notification.created_at >= cutoff)
        .order_by(Notification.created_at.asc())
        .limit(batch_size)
        .with_for_update(of=Notification, skip_locked=True)
    )
    notifications = list(db.execute(stmt).scalars().all())

    for notification in notifications:
        results["processed"] += 1

        if not person_can_receive_email(notification.recipient):
            logger.info(
                "Suppressing notification email %s: recipient is inactive",
                notification.notification_id,
            )
            notification.email_sent = True
            notification.email_sent_at = datetime.now(UTC)
            results["skipped"] += 1
            continue

        recipient_email = None
        if notification.recipient and notification.recipient.email:
            recipient_email = notification.recipient.email.strip()

        if not recipient_email:
            logger.warning(
                "Skipping notification %s: recipient email missing",
                notification.notification_id,
            )
            results["skipped"] += 1
            continue

        try:
            body_text = notification.message
            safe_message = (
                html.escape(notification.message) if notification.message else None
            )
            body_html = (
                f"<p>{safe_message}</p>"
                if safe_message
                else "<p>You have a new notification in Dotmac ERP.</p>"
            )
            if notification.action_url:
                url = notification.action_url
                if url.startswith("/") or url.startswith("http"):
                    safe_url = html.escape(url)
                    action_label = _email_action_label_for_notification(notification)
                    body_html += f'<p><a href="{safe_url}">{action_label}</a></p>'

            ok = send_email(
                db=db,
                to_email=recipient_email,
                subject=notification.title,
                body_html=body_html,
                body_text=body_text,
                module=_email_module_for_notification(notification),
                organization_id=notification.organization_id,
            )
            if ok:
                notification.email_sent = True
                notification.email_sent_at = datetime.now(UTC)
                results["sent"] += 1
            else:
                results["failed"] += 1
                if _record_email_delivery_failure(notification, now):
                    results["dead_letter"] += 1
                    logger.error(
                        "Dead-lettered notification email %s after %d attempts",
                        notification.notification_id,
                        notification.email_retry_count,
                    )
        except Exception:
            logger.exception(
                "Failed sending notification email %s",
                notification.notification_id,
            )
            results["failed"] += 1
            if _record_email_delivery_failure(notification, now):
                results["dead_letter"] += 1
                logger.error(
                    "Dead-lettered notification email %s after %d attempts",
                    notification.notification_id,
                    notification.email_retry_count,
                )

    db.commit()


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    retry_jitter=True,
)
def process_pending_notification_emails(
    self,
    batch_size: int = 100,
) -> NotificationEmailDispatchResults:
    """Send pending emails in fully tenant-scoped batches."""
    results: NotificationEmailDispatchResults = {
        "processed": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "dead_letter": 0,
    }

    try:
        for organization_id in active_organization_ids():
            with session_for_org(organization_id) as db:
                _process_notification_email_batch(
                    db,
                    organization_id,
                    batch_size,
                    results,
                )
    except _DB_RETRYABLE_ERRORS as exc:
        logger.exception(
            "Retrying notification email task after database error (attempt %d)",
            self.request.retries + 1,
        )
        raise self.retry(exc=exc)

    if results["processed"] > 0 or results["dead_letter"] > 0:
        logger.info(
            "Notification email dispatch: processed=%d sent=%d skipped=%d "
            "failed=%d dead_letter=%d",
            results["processed"],
            results["sent"],
            results["skipped"],
            results["failed"],
            results["dead_letter"],
        )

    return results


class NextcloudDispatchResults(TypedDict):
    processed: int
    sent: int
    skipped: int
    failed: int
    dead_letter: int


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    retry_jitter=True,
)
def process_pending_nextcloud_notifications(
    self,
    batch_size: int = 100,
) -> NextcloudDispatchResults:
    """
    Send pending Nextcloud Talk notifications and mark delivery status.

    Notes:
    - Only notifications with channel NEXTCLOUD/ALL and nextcloud_sent=False are selected.
    - Uses FOR UPDATE SKIP LOCKED to prevent duplicate processing across workers.
    - Requires nextcloud_server_url, nextcloud_username, nextcloud_password in
      domain settings (notifications domain).
    """
    from app.services.nextcloud.client import (
        NextcloudError,
        NextcloudTalkClient,
        is_configured,
    )

    results: NextcloudDispatchResults = {
        "processed": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "dead_letter": 0,
    }

    try:
        with _task_db_session() as db:
            cutoff = datetime.now(UTC) - _DEAD_LETTER_AGE

            # Count dead-lettered Nextcloud notifications for observability.
            dead_nc_stmt = (
                select(Notification.notification_id)
                .where(Notification.nextcloud_sent == False)  # noqa: E712
                .where(
                    Notification.channel.in_(
                        [
                            NotificationChannel.NEXTCLOUD,
                            NotificationChannel.ALL,
                        ]
                    )
                )
                .where(Notification.created_at < cutoff)
            )
            dead_nc_ids = list(db.execute(dead_nc_stmt).scalars().all())
            results["dead_letter"] = len(dead_nc_ids)
            if dead_nc_ids:
                logger.warning(
                    "Dead-letter: %d Nextcloud notifications older than %s days will not be retried",
                    len(dead_nc_ids),
                    _DEAD_LETTER_AGE.days,
                )

            stmt = (
                select(Notification)
                .where(Notification.nextcloud_sent == False)  # noqa: E712
                .where(
                    Notification.channel.in_(
                        [
                            NotificationChannel.NEXTCLOUD,
                            NotificationChannel.ALL,
                        ]
                    )
                )
                .where(Notification.created_at >= cutoff)
                .order_by(Notification.created_at.asc())
                .limit(batch_size)
                .with_for_update(of=Notification, skip_locked=True)
            )
            notifications = list(db.execute(stmt).scalars().all())

            if not notifications:
                return results

            notifications_by_org: dict[UUID, list[Notification]] = {}
            for notification in notifications:
                notifications_by_org.setdefault(
                    notification.organization_id, []
                ).append(notification)

            for organization_id, org_notifications in notifications_by_org.items():
                if not is_configured(db, organization_id=organization_id):
                    results["processed"] += len(org_notifications)
                    results["skipped"] += len(org_notifications)
                    continue

                client = NextcloudTalkClient.from_db(
                    db, organization_id=organization_id
                )

                for notification in org_notifications:
                    results["processed"] += 1

                    nc_user_id = None
                    if notification.recipient:
                        nc_user_id = notification.recipient.nextcloud_user_id

                    if not nc_user_id:
                        logger.warning(
                            "Skipping notification %s: recipient nextcloud_user_id missing",
                            notification.notification_id,
                        )
                        results["skipped"] += 1
                        continue

                    try:
                        message = f"**{notification.title}**\n{notification.message}"
                        if notification.action_url:
                            message += f"\n\n{notification.action_url}"

                        client.send_to_user(nc_user_id, message)
                        notification.nextcloud_sent = True
                        notification.nextcloud_sent_at = datetime.now(UTC)
                        results["sent"] += 1
                    except NextcloudError:
                        logger.exception(
                            "Failed sending Nextcloud notification %s",
                            notification.notification_id,
                        )
                        results["failed"] += 1
                    except Exception:
                        logger.exception(
                            "Unexpected error sending Nextcloud notification %s",
                            notification.notification_id,
                        )
                        results["failed"] += 1

            db.commit()
    except _DB_RETRYABLE_ERRORS as exc:
        logger.exception(
            "Retrying nextcloud notification task after database error (attempt %d)",
            self.request.retries + 1,
        )
        raise self.retry(exc=exc)

    if results["processed"] > 0 or results["dead_letter"] > 0:
        logger.info(
            "Nextcloud dispatch: processed=%d sent=%d skipped=%d failed=%d dead_letter=%d",
            results["processed"],
            results["sent"],
            results["skipped"],
            results["failed"],
            results["dead_letter"],
        )

    return results


# Push notifications older than this are stale (the user has seen the in-app
# inbox by now); they are swept to push_sent without delivery so the queue
# drains instead of blasting old backlog after first deploy/downtime.
_PUSH_STALE_AGE = timedelta(hours=24)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    retry_jitter=True,
)
def process_pending_push_notifications(
    self,
    batch_size: int = 100,
) -> dict:
    """
    Deliver pending mobile pushes (FCM) and mark delivery status.

    Mirrors process_pending_notification_emails:
    - Only channel IN_APP/BOTH/ALL with push_sent=False are selected.
    - FOR UPDATE SKIP LOCKED prevents duplicate processing across workers.
    - No-op (cheap) when FCM is unconfigured.
    """
    from app.services.push import PushService

    results: dict = {"processed": 0, "sent": 0, "swept": 0, "failed": 0}

    if not PushService.is_configured():
        return results

    try:
        with _task_db_session() as db:
            cutoff = datetime.now(UTC) - _PUSH_STALE_AGE

            # Sweep stale backlog (e.g. rows pre-dating this feature) so the
            # pending set stays bounded.
            from sqlalchemy import update as sa_update

            swept = db.execute(
                sa_update(Notification)
                .where(Notification.push_sent == False)  # noqa: E712
                .where(Notification.created_at < cutoff)
                .values(push_sent=True)
            )
            results["swept"] = swept.rowcount or 0

            stmt = (
                select(Notification)
                .where(Notification.push_sent == False)  # noqa: E712
                .where(
                    Notification.channel.in_(
                        [
                            NotificationChannel.IN_APP,
                            NotificationChannel.BOTH,
                            NotificationChannel.ALL,
                        ]
                    )
                )
                .where(Notification.created_at >= cutoff)
                .order_by(Notification.created_at.asc())
                .limit(batch_size)
                .with_for_update(of=Notification, skip_locked=True)
            )
            notifications = list(db.execute(stmt).scalars().all())

            push_service = PushService(db)
            for notification in notifications:
                results["processed"] += 1
                try:
                    data = {"notification_id": str(notification.notification_id)}
                    if notification.action_url:
                        data["action_url"] = notification.action_url
                    sent = push_service.send_to_person(
                        notification.recipient_id,
                        title=notification.title,
                        body=notification.message,
                        data=data,
                    )
                    results["sent"] += sent
                except Exception:
                    # Delivery problems never poison the batch; the row stays
                    # marked sent — push is best-effort, in-app is canonical.
                    logger.exception(
                        "Push dispatch failed for notification %s",
                        notification.notification_id,
                    )
                    results["failed"] += 1
                notification.push_sent = True
                notification.push_sent_at = datetime.now(UTC)

            db.commit()
    except _DB_RETRYABLE_ERRORS as exc:
        logger.warning("Push dispatch DB error; retrying: %s", exc)
        raise self.retry(exc=exc)

    if results["processed"]:
        logger.info("Push dispatch: %s", results)
    return results
