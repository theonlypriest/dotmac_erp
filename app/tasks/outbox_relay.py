"""
Outbox Relay — Celery task that delivers finance outbox events with
applied-result semantics (claim / deliver / settle).

The transactional outbox pattern writes events atomically with business
data; this relay asynchronously applies their declared consequences:

1. **Claim** — a bounded batch is claimed atomically under
   ``SELECT ... FOR UPDATE SKIP LOCKED`` in a short cross-org transaction
   that stamps a claim token + lease and commits. Two workers can never
   claim the same event; expired leases are reclaimable, so a dead worker
   is repaired by redelivery.
2. **Deliver** — each event is delivered OUTSIDE the claim transaction in
   its own org-scoped session (``session_for_org`` primed from the event's
   ``headers.organization_id``), one event per transaction.
3. **Settle** — settlement is staged in the SAME transaction as the
   handler's mutations and requires the claim token, so:
   - a commit failure can never record success (the event stays claimed
     and is redelivered after lease expiry), and
   - a stale claimant (lease expired + reclaimed) cannot settle.

Unknown events fail closed: an event with no registered handler is
dead-lettered as UNSUPPORTED (alertable) unless its event name or producer
is explicitly declared no-consequence — a registered, documented decision
that settles as PUBLISHED with a recorded no-op note.

Handler success means the complete declared consequence committed. Handlers
must NOT commit and must NOT swallow per-item failures: any exception rolls
back the whole delivery transaction, so a partial application can never
persist (this whole-event atomicity is what keeps retries idempotent when
the underlying writer — e.g. AccountBalanceService.update_balance_for_posting,
which is additive — is not per-item idempotent).
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from celery import shared_task
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.db.session_context import cross_org_session, session_for_org
from app.metrics import (
    observe_outbox_outcome,
    observe_outbox_reconciliation,
    set_outbox_backlog_ages,
)
from app.services.finance.platform.outbox_publisher import (
    OutboxPublisher,
    StaleClaimError,
)

logger = logging.getLogger(__name__)
_DB_RETRYABLE_ERRORS = (OperationalError, ProgrammingError)


def _task_db_session():
    """Yield a task DB session and ensure it is rolled back/closed on any error."""
    return cross_org_session()


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

EventHandler = Any  # Callable[[Session, EventOutbox], None]

_HANDLERS: dict[str, EventHandler] = {}

# Event names that are DELIBERATELY observe-only: recording them in the
# outbox is their complete declared consequence, so they settle PUBLISHED
# with a recorded no-op note instead of dead-lettering as unsupported.
# Adding a name here is a registered, documented decision — do not add a
# name to silence an alert for an event that should have a handler.
DECLARED_NO_CONSEQUENCE: frozenset[str] = frozenset()

# Producers whose outbox entries are declared observe-only as a class.
# "hooks": the EVENT_OUTBOX service-hook handler copies admin-configured
# hook events into the outbox purely as a durable record — those event
# names are dynamic (per-hook configuration) and carry no relay-side
# consequence by design (app/services/hooks/registry.py).
DECLARED_NO_CONSEQUENCE_PRODUCERS: frozenset[str] = frozenset({"hooks"})


def register_handler(event_name: str, handler: EventHandler) -> None:
    """Register a handler for an event name."""
    _HANDLERS[event_name] = handler
    logger.debug("Registered handler for %s: %s", event_name, handler.__name__)


def _get_handler(event_name: str) -> EventHandler | None:
    """Get the handler for an event name (exact match)."""
    return _HANDLERS.get(event_name)


def _is_declared_no_consequence(event_name: str, producer_module: str) -> bool:
    """True when settling this event without a handler is a documented no-op."""
    return (
        event_name in DECLARED_NO_CONSEQUENCE
        or producer_module in DECLARED_NO_CONSEQUENCE_PRODUCERS
    )


class NonRetryableEventError(Exception):
    """Raised by handlers for events that can never succeed (malformed
    payload, missing required identifiers). Dead-letters immediately
    instead of burning the retry ladder."""


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def handle_ledger_posting_completed(db: Session, event: Any) -> None:
    """
    Handle ledger.posting.completed events.

    Reads posted ledger lines for the batch and incrementally updates
    account_balance rows. This provides near-real-time balance updates
    (the daily rebuild_account_balances task and the outbox reconciler
    serve as safety nets).

    Failure semantics: any per-line failure RAISES, which rolls back the
    whole delivery transaction. ``update_balance_for_posting`` is additive
    (re-applying a line double-counts), so partial progress must never be
    committed — whole-event atomicity is the idempotency mechanism. An
    unrecorded partial failure can therefore never settle as published.
    """
    from sqlalchemy import select

    from app.models.finance.gl.posted_ledger_line import PostedLedgerLine
    from app.services.finance.gl.account_balance import AccountBalanceService

    payload = event.payload
    batch_id = payload.get("batch_id")
    org_raw = payload.get("organization_id")

    if not batch_id or not org_raw:
        raise NonRetryableEventError(
            f"ledger.posting.completed event {event.event_id} payload is "
            "missing batch_id/organization_id"
        )
    org_id = UUID(str(org_raw))

    # Get all posted ledger lines for this batch
    lines = list(
        db.scalars(
            select(PostedLedgerLine).where(
                PostedLedgerLine.posting_batch_id == UUID(str(batch_id))
            )
        ).all()
    )

    if not lines:
        logger.info("No ledger lines for batch %s (may already be processed)", batch_id)
        return

    for line in lines:
        # No per-line exception handling: a failed line fails the event.
        AccountBalanceService.update_balance_for_posting(
            db,
            organization_id=org_id,
            account_id=line.account_id,
            fiscal_period_id=line.fiscal_period_id,
            debit_amount=line.debit_amount or Decimal("0"),
            credit_amount=line.credit_amount or Decimal("0"),
            business_unit_id=line.business_unit_id,
            cost_center_id=line.cost_center_id,
            project_id=line.project_id,
            segment_id=line.segment_id,
        )

    logger.info(
        "Updated account balances for batch %s (%d lines)",
        batch_id,
        len(lines),
    )


def handle_staff_access_projection_changed(db: Session, event: Any) -> None:
    """Bridge ERP staff access projection events into configured service hooks."""
    payload = event.payload or {}
    org_raw = payload.get("organization_id")
    aggregate_id = event.aggregate_id
    if not org_raw or not aggregate_id:
        raise NonRetryableEventError(
            f"{event.event_name} event {event.event_id} is missing organization "
            "or aggregate id"
        )

    from app.services.hooks.registry import emit_hook_event

    actor_raw = (event.headers or {}).get("user_id")
    actor_user_id = UUID(str(actor_raw)) if actor_raw else None
    emit_hook_event(
        db,
        event_name=event.event_name,
        organization_id=UUID(str(org_raw)),
        entity_type=event.aggregate_type,
        entity_id=UUID(str(aggregate_id)),
        actor_user_id=actor_user_id,
        payload=payload,
    )


# Register built-in handlers
register_handler("ledger.posting.completed", handle_ledger_posting_completed)
register_handler(
    "hr.staff_leave_restriction.changed",
    handle_staff_access_projection_changed,
)
register_handler(
    "hr.staff_account_status.changed",
    handle_staff_access_projection_changed,
)


# ---------------------------------------------------------------------------
# Relay task
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClaimedEvent:
    """Detached snapshot of a claimed event (taken before the claim
    transaction commits, so delivery never touches the claim session)."""

    event_id: UUID
    event_name: str
    producer_module: str
    organization_id: str | None


def _claim_batch(
    batch_size: int,
    max_retry_count: int,
    lease_seconds: int,
) -> tuple[UUID | None, list[_ClaimedEvent]]:
    """Claim a batch in a short committed transaction; also refresh
    backlog-age gauges from the same session."""
    from sqlalchemy import func, select

    from app.models.finance.platform.event_outbox import EventOutbox, EventStatus

    with _task_db_session() as db:
        claim_token, events = OutboxPublisher.claim_pending_events(
            db,
            worker_id=_worker_id(),
            batch_size=batch_size,
            max_retry_count=max_retry_count,
            lease_seconds=lease_seconds,
        )
        claimed = [
            _ClaimedEvent(
                event_id=event.event_id,
                event_name=event.event_name,
                producer_module=event.producer_module,
                organization_id=(event.headers or {}).get("organization_id"),
            )
            for event in events
        ]

        # Backlog gauges (read before commit; outbox is not org-scoped).
        now = datetime.now(UTC)
        oldest_pending = db.scalar(
            select(func.min(EventOutbox.occurred_at)).where(
                EventOutbox.status.in_([EventStatus.PENDING, EventStatus.FAILED])
            )
        )
        oldest_claim = db.scalar(
            select(func.min(EventOutbox.claimed_at)).where(
                EventOutbox.claim_token.isnot(None)
            )
        )
        pending_age = (now - oldest_pending).total_seconds() if oldest_pending else 0.0
        lease_age = (now - oldest_claim).total_seconds() if oldest_claim else 0.0
        set_outbox_backlog_ages(pending_age, lease_age)

        db.commit()

    return (claim_token if claimed else None), claimed


def _settle_without_handler(
    claimed: _ClaimedEvent,
    claim_token: UUID,
    counts: dict[str, int],
    errors: list[str],
) -> None:
    """Settle a claimed event that has no registered handler."""
    with _task_db_session() as db:
        try:
            if _is_declared_no_consequence(claimed.event_name, claimed.producer_module):
                from app.models.finance.platform.event_outbox import TerminalReason

                OutboxPublisher.mark_published(
                    db,
                    claimed.event_id,
                    claim_token=claim_token,
                    terminal_reason=TerminalReason.DECLARED_NO_CONSEQUENCE,
                )
                db.commit()
                counts["no_consequence"] += 1
                observe_outbox_outcome("no_consequence")
            else:
                # Fail closed: never PUBLISHED, alertable dead letter.
                logger.error(
                    "Unsupported outbox event %s (%s from %s): no registered "
                    "handler and not declared no-consequence — dead-lettering",
                    claimed.event_id,
                    claimed.event_name,
                    claimed.producer_module,
                )
                OutboxPublisher.mark_unsupported(
                    db, claimed.event_id, claim_token=claim_token
                )
                db.commit()
                counts["unsupported"] += 1
                observe_outbox_outcome("unsupported")
        except StaleClaimError:
            db.rollback()
            counts["stale_claims"] += 1
            observe_outbox_outcome("stale_claim")
        except (ValueError, SQLAlchemyError) as exc:
            db.rollback()
            counts["commit_failed"] += 1
            observe_outbox_outcome("commit_failed")
            errors.append(f"{claimed.event_id}: settlement failed: {exc}")


def _settle_failure(
    db: Session,
    claimed: _ClaimedEvent,
    claim_token: UUID,
    exc: Exception,
    counts: dict[str, int],
    errors: list[str],
) -> None:
    """Settle a failed delivery (retry ladder or dead-letter) in a fresh
    transaction on the same session (handler mutations already rolled
    back; the outbox table is not org-scoped, so the cleared RLS GUC after
    rollback does not affect this write)."""
    from app.models.finance.platform.event_outbox import EventStatus, TerminalReason

    try:
        if isinstance(exc, NonRetryableEventError):
            OutboxPublisher.mark_dead(
                db,
                claimed.event_id,
                error_message=str(exc),
                claim_token=claim_token,
                error_class=type(exc).__name__,
                terminal_reason=TerminalReason.INVALID_PAYLOAD,
            )
            db.commit()
            counts["dead"] += 1
            observe_outbox_outcome("dead")
        else:
            event = OutboxPublisher.handle_retry(
                db,
                claimed.event_id,
                claim_token=claim_token,
                error_message=str(exc),
                error_class=type(exc).__name__,
            )
            db.commit()
            if event.status == EventStatus.DEAD:
                counts["dead"] += 1
                observe_outbox_outcome("dead")
            else:
                counts["retried"] += 1
                observe_outbox_outcome("retried")
        errors.append(f"{claimed.event_id}: {exc}")
    except StaleClaimError:
        db.rollback()
        counts["stale_claims"] += 1
        observe_outbox_outcome("stale_claim")
    except (ValueError, SQLAlchemyError) as settle_exc:
        # Failure settlement itself failed — leave the event claimed; the
        # lease expiry makes it reclaimable, which is the safe outcome.
        db.rollback()
        counts["commit_failed"] += 1
        observe_outbox_outcome("commit_failed")
        errors.append(f"{claimed.event_id}: failure settlement failed: {settle_exc}")


def _deliver_one(
    claimed: _ClaimedEvent,
    claim_token: UUID,
    counts: dict[str, int],
    errors: list[str],
) -> None:
    """Deliver one claimed event in its own org-scoped transaction."""
    from app.models.finance.platform.event_outbox import EventOutbox, TerminalReason

    handler = _get_handler(claimed.event_name)
    if handler is None:
        _settle_without_handler(claimed, claim_token, counts, errors)
        return

    # Handlers touch org-scoped tables, so delivery MUST run under the
    # event's organization context (both tenant layers).
    try:
        org_id = UUID(str(claimed.organization_id)) if claimed.organization_id else None
    except ValueError:
        org_id = None
    if org_id is None:
        with _task_db_session() as db:
            try:
                OutboxPublisher.mark_dead(
                    db,
                    claimed.event_id,
                    error_message="Event headers carry no valid organization_id",
                    claim_token=claim_token,
                    error_class="MissingOrganizationContext",
                    terminal_reason=TerminalReason.MISSING_ORGANIZATION_CONTEXT,
                )
                db.commit()
                counts["dead"] += 1
                observe_outbox_outcome("missing_org")
            except (StaleClaimError, ValueError, SQLAlchemyError):
                db.rollback()
                counts["stale_claims"] += 1
                observe_outbox_outcome("stale_claim")
        errors.append(f"{claimed.event_id}: missing organization context")
        return

    with session_for_org(org_id) as db:
        event = db.get(EventOutbox, claimed.event_id)
        if event is None:
            logger.warning("Claimed event %s disappeared before delivery", claimed)
            return
        try:
            # Handler mutations + settlement in ONE transaction: a commit
            # failure can never record success.
            handler(db, event)
            OutboxPublisher.mark_published(
                db, claimed.event_id, claim_token=claim_token
            )
            db.commit()
            counts["published"] += 1
            observe_outbox_outcome("published")
        except StaleClaimError:
            db.rollback()
            counts["stale_claims"] += 1
            observe_outbox_outcome("stale_claim")
        except Exception as exc:  # noqa: BLE001 — handler boundary
            logger.exception(
                "Delivery failed for event %s (%s): %s",
                claimed.event_id,
                claimed.event_name,
                exc,
            )
            db.rollback()
            _settle_failure(db, claimed, claim_token, exc, counts, errors)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    retry_backoff=True,
    retry_jitter=True,
)
def relay_outbox_events(
    self,
    batch_size: int = 100,
    max_retry_count: int = 5,
    lease_seconds: int = 300,
) -> dict[str, Any]:
    """
    Claim a batch of outbox events, deliver each in its own org-scoped
    transaction, and settle with applied-result semantics.

    Args:
        batch_size: Max events to claim per run.
        max_retry_count: Max retries before marking dead.
        lease_seconds: Claim lease duration; a worker that dies mid-batch
            is repaired by reclaim + redelivery after this long.

    Returns:
        Dict with per-outcome counts.
    """
    logger.info("Relay: claiming outbox batch (batch=%d)", batch_size)

    counts: dict[str, int] = {
        "published": 0,
        "no_consequence": 0,
        "retried": 0,
        "dead": 0,
        "unsupported": 0,
        "stale_claims": 0,
        "commit_failed": 0,
    }
    errors: list[str] = []

    try:
        claim_token, claimed_events = _claim_batch(
            batch_size, max_retry_count, lease_seconds
        )
    except _DB_RETRYABLE_ERRORS as exc:
        logger.exception(
            "Retrying outbox relay task after database error (attempt %d)",
            self.request.retries + 1,
        )
        raise self.retry(exc=exc)

    if claim_token is None or not claimed_events:
        logger.debug("Relay: no pending events")
        return {**counts, "errors": errors}

    logger.info("Relay: delivering %d claimed events", len(claimed_events))
    for claimed in claimed_events:
        _deliver_one(claimed, claim_token, counts, errors)

    logger.info(
        "Relay complete: %(published)d published, %(no_consequence)d no-op, "
        "%(retried)d retried, %(dead)d dead, %(unsupported)d unsupported, "
        "%(stale_claims)d stale, %(commit_failed)d settlement failures",
        counts,
    )
    return {**counts, "errors": errors}


# ---------------------------------------------------------------------------
# Reconciliation task
# ---------------------------------------------------------------------------


@shared_task
def reconcile_outbox_balance_projection(lookback_hours: int = 26) -> dict[str, Any]:
    """
    Compare outbox delivery success against the authoritative consequence:
    for recent PUBLISHED ``ledger.posting.completed`` events, recompute the
    expected GL balance projection from posted_ledger_lines and repair any
    drift through the canonical writer (rebuild_balances_for_period).

    Idempotent — verification is read-only and repair rebuilds from
    authoritative inputs.

    Returns:
        Dict with scopes checked, drift found, and repairs performed.
    """
    from app.services.finance.platform.outbox_reconciler import (
        OutboxBalanceReconciler,
    )

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    logger.info("Outbox reconciler: verifying posting scopes since %s", since)

    with _task_db_session() as db:
        scopes = OutboxBalanceReconciler.collect_recent_posting_scopes(db, since)

    results: dict[str, Any] = {
        "scopes_checked": 0,
        "drift_found": 0,
        "repaired": 0,
        "records": [],
        "errors": [],
    }

    for org_id, period_id in scopes:
        try:
            with session_for_org(org_id) as db:
                record = OutboxBalanceReconciler.reconcile_period(db, org_id, period_id)
                db.commit()
            results["scopes_checked"] += 1
            if record["drifted_accounts"]:
                results["drift_found"] += 1
                results["records"].append(record)
                observe_outbox_reconciliation("drift_found", record["drifted_accounts"])
            if record["repaired"]:
                results["repaired"] += 1
                observe_outbox_reconciliation("repaired")
        except Exception as exc:  # noqa: BLE001 — per-scope isolation
            logger.exception(
                "Outbox reconciliation failed for org %s period %s",
                org_id,
                period_id,
            )
            results["errors"].append(f"{org_id}/{period_id}: {exc}")

    logger.info(
        "Outbox reconciler complete: %d scopes, %d drifted, %d repaired",
        results["scopes_checked"],
        results["drift_found"],
        results["repaired"],
    )
    return results


@shared_task
def cleanup_published_outbox_events(
    retention_days: int = 30,
    batch_size: int = 5000,
) -> dict[str, Any]:
    """
    Delete old PUBLISHED events to keep the outbox table lean.

    Args:
        retention_days: Keep published events for this many days.
        batch_size: Max events to delete per run.

    Returns:
        Dict with deletion count.
    """
    logger.info(
        "Cleaning up published outbox events older than %d days", retention_days
    )

    deleted = 0

    with _task_db_session() as db:
        from sqlalchemy import delete

        from app.models.finance.platform.event_outbox import EventOutbox, EventStatus

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)

        result = db.execute(
            delete(EventOutbox).where(
                EventOutbox.status == EventStatus.PUBLISHED,
                EventOutbox.published_at < cutoff,
            )
        )
        deleted = cast(CursorResult[Any], result).rowcount or 0
        db.commit()

    logger.info("Deleted %d old published outbox events", deleted)
    return {"deleted": deleted}
