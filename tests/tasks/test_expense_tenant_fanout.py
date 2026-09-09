"""The expense sweeps fan out over tenants instead of scanning across them.

Both jobs had the same shape: one ``cross_org_session`` read every tenant's rows
at once to find out who had work, and a tenant session was then opened per
answer. ``cross_org_session`` lifts only the SQLAlchemy listener and never
PostgreSQL RLS, so under ``app_user`` that first read returns **zero rows** — no
approval reminder is ever sent, no stale payout intent ever expires, no stuck
transfer is ever polled, and both jobs report a clean run every time they fire.

The reminders job's grouping dict and id re-fetch are deleted rather than
adapted: the same predicate evaluated inside a tenant session already returns
only that tenant's claims, which is the point of the session.

``poll_stuck_expense_transfers`` keeps its selections in ``PaymentService``,
which is the sole writer of ``PaymentIntent.status``
(``tests/architecture/test_payment_intent_status_single_owner.py``); only the
session they run on changed. They still return organization -> intent ids, so
inside a tenant session that mapping has at most one key and reading it back by
``org_id`` is the isolation assertion.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.db import session_context
from app import tenant_catalog
from app.tasks import expense

ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def tenant_sessions(monkeypatch):
    """Two organizations. Each session open is recorded and scripted.

    ``script`` is consumed one list per session open, in order — which is how a
    test says "this tenant's query returns these rows" for a job that opens more
    than one session per tenant.

    The seam patched is ``for_each_organization``'s own — the catalogue definer
    and ``session_for_org`` in :mod:`app.db.session_context` — not a name bound
    in :mod:`app.tasks.expense`. Patching the helper itself would let a fan-out
    that never opened a tenant session pass, which is the shape this file
    exists to refuse. ``discovery`` records the kwargs it enumerated with.
    """
    opened: list[uuid.UUID] = []
    sessions: list[MagicMock] = []
    scripted: list[list[object]] = []
    discovery: dict[str, object] = {}

    @contextmanager
    def session_for_org(organization_id):
        db = MagicMock(name=f"db:{organization_id}")
        db.scalars.return_value.all.return_value = scripted.pop(0) if scripted else []
        opened.append(organization_id)
        sessions.append(db)
        yield db

    def organization_ids(**kwargs):
        discovery.update(kwargs)
        return [ORG_A, ORG_B]

    monkeypatch.setattr(session_context, "session_for_org", session_for_org)
    monkeypatch.setattr(tenant_catalog, "organization_ids", organization_ids)
    return SimpleNamespace(
        opened=opened, sessions=sessions, script=scripted, discovery=discovery
    )


def _claim(number: str, *, days_pending: int = 0) -> SimpleNamespace:
    """A submitted claim, pending for ``days_pending`` days.

    Default 0: too young for any reminder, so the job walks the tenants and
    stops at the age guard — which is all most of these tests need.
    """
    today = date.today()
    pending_since = today - timedelta(days=days_pending)
    return SimpleNamespace(
        claim_id=uuid.uuid4(),
        claim_number=number,
        organization_id=uuid.uuid4(),
        claim_date=pending_since,
        updated_at=datetime.combine(pending_since, datetime.min.time()),
        approver_id=None,
        employee=SimpleNamespace(
            employee_id=uuid.uuid4(),
            expense_approver_id=uuid.uuid4(),
        ),
    )


# ── process_expense_approval_reminders ───────────────────────────────


def test_pending_claims_are_read_inside_each_tenants_session(tenant_sessions) -> None:
    """The notification service is built from the tenant session, per tenant."""
    built_from: list[object] = []

    def service_factory(db):
        built_from.append(db)
        return MagicMock()

    with patch(
        "app.services.expense.expense_notifications.ExpenseNotificationService",
        side_effect=service_factory,
    ):
        expense.process_expense_approval_reminders()

    assert tenant_sessions.opened == [ORG_A, ORG_B]
    assert built_from == tenant_sessions.sessions, (
        "the service must be built from the tenant-scoped session; building it "
        "from anything else is how the cross-tenant scan came back"
    )


def test_a_tenants_claims_are_only_seen_in_that_tenants_session(
    tenant_sessions,
) -> None:
    """Isolation: org A's claim never reaches org B's session.

    The old code fetched both tenants' claims in one bypassed session and relied
    on correct grouping to keep them apart. Here the session keeps them apart,
    and this asserts the code does not undo that.
    """
    claim_a = _claim("EXP-A-1", days_pending=7)
    claim_b = _claim("EXP-B-1", days_pending=7)
    tenant_sessions.script.extend([[claim_a], [claim_b]])
    reminded: list[tuple[object, str]] = []

    def service_factory(db):
        service = MagicMock()
        service.send_pending_approval_reminder.side_effect = (
            lambda claim, approver, **_: (
                reminded.append((db, claim.claim_number)) or True
            )
        )
        return service

    with (
        patch(
            "app.services.expense.expense_notifications.ExpenseNotificationService",
            side_effect=service_factory,
        ),
        patch.object(expense, "NotificationService") as notifications,
    ):
        notifications.return_value.was_sent_since.return_value = False
        expense.process_expense_approval_reminders()

    db_a, db_b = tenant_sessions.sessions
    assert tenant_sessions.opened == [ORG_A, ORG_B]
    assert reminded == [(db_a, "EXP-A-1"), (db_b, "EXP-B-1")], (
        "each claim must be reminded through the session opened for its own "
        "tenant — the isolation the grouping dict used to maintain by hand"
    )


def test_approval_reminders_reach_deactivated_tenants(tenant_sessions) -> None:
    """The scan this replaced had no ``Organization`` predicate."""
    with patch(
        "app.services.expense.expense_notifications.ExpenseNotificationService",
        return_value=MagicMock(),
    ):
        expense.process_expense_approval_reminders()

    assert tenant_sessions.discovery == {"include_inactive": True, "only": None}


# ── poll_stuck_expense_transfers ─────────────────────────────────────


def _poll_patches(*, stale=None, stuck=None, config=object(), reconcile=None):
    """Patch the four PaymentService seams the worker delegates to.

    The selections stay static methods on the service — the worker is a thin
    adapter over them and must not grow its own SELECT — so they are patched
    where they live rather than replaced by a fake session.
    """
    from app.services.finance.payments import payment_service as module

    return (
        patch.object(
            module.PaymentService,
            "find_stale_pending_transfer_intents",
            staticmethod(lambda db, **_: dict(stale or {})),
        ),
        patch.object(
            module.PaymentService,
            "find_stuck_transfer_intents",
            staticmethod(lambda db, **_: dict(stuck or {})),
        ),
        patch.object(
            module.PaymentService,
            "resolve_transfer_polling_config",
            MagicMock(return_value=config),
        ),
        patch.object(
            module.PaymentService,
            "reconcile_stuck_transfer",
            reconcile or MagicMock(),
        ),
    )


def test_stale_payout_intents_expire_in_their_own_tenants_session(
    tenant_sessions,
) -> None:
    """Phase one visits every tenant, expiring only that tenant's intents.

    ``find_stale_pending_transfer_intents`` answers with org A's grouping in
    BOTH tenants' sessions. Only org A's pass may act on it: the worker reads
    the mapping back by the organization whose session it is holding, so a row
    grouped under another tenant is not this session's to touch. That is the
    isolation the database enforces and this asserts the code does not undo.
    """
    from app.services.finance.payments import payment_service as module

    intent_id = uuid.uuid4()
    expired: list[tuple[object, uuid.UUID, uuid.UUID]] = []

    def expire(self, iid, **_):
        expired.append((self.db, self.organization_id, iid))
        return True

    stale_patch, stuck_patch, config_patch, reconcile_patch = _poll_patches(
        stale={ORG_A: [intent_id]}
    )
    with (
        stale_patch,
        stuck_patch,
        config_patch,
        reconcile_patch,
        patch.object(module.PaymentService, "expire_stale_pending_transfer", expire),
    ):
        result = expense.poll_stuck_expense_transfers()

    assert tenant_sessions.opened == [ORG_A, ORG_B, ORG_A, ORG_B]
    assert expired == [(tenant_sessions.sessions[0], ORG_A, intent_id)]
    assert result["expired"] == 1
    # Only the tenant that had work commits; org B is skipped before its commit.
    assert tenant_sessions.sessions[0].commit.call_count == 1
    assert tenant_sessions.sessions[1].commit.call_count == 0


def test_stuck_transfers_are_polled_with_their_own_tenants_config(
    tenant_sessions,
) -> None:
    """Phase two resolves the Paystack config from the tenant's own session.

    Keys are ``SettingDomain.payments`` values resolved through the session, so
    a tenant's transfer can only ever be polled with that tenant's credentials.
    """
    from app.services.finance.payments.payment_service import (
        TransferPollOutcome,
        TransferPollResult,
    )

    intent_id = uuid.uuid4()
    reconciled: list[tuple[object, uuid.UUID, uuid.UUID]] = []

    def reconcile(self, iid, config, **_):
        reconciled.append((self.db, self.organization_id, iid))
        return TransferPollResult(
            intent_id=iid,
            outcome=TransferPollOutcome.STILL_PENDING,
            poll_count=1,
        )

    patches = _poll_patches(stuck={ORG_A: [intent_id]}, reconcile=reconcile)
    with patches[0], patches[1], patches[2], patches[3]:
        result = expense.poll_stuck_expense_transfers()

    # Session index 2 is org A's phase-two session.
    assert reconciled == [(tenant_sessions.sessions[2], ORG_A, intent_id)]
    assert result["intents_checked"] == 1
    assert result["still_pending"] == 1


def test_a_tenant_without_stuck_transfers_is_not_asked_for_paystack_keys(
    tenant_sessions,
) -> None:
    """Skipping before the config read keeps the warning meaningful.

    The old grouping only ever built a session for a tenant that had a stuck
    intent, so "No Paystack keys for org X" meant a transfer was actually
    waiting on those keys. Enumerating every tenant would turn that into noise
    for every unconfigured tenant on the fleet if the order were reversed.
    """
    stale_patch, stuck_patch, config_patch, reconcile_patch = _poll_patches()
    with stale_patch, stuck_patch, config_patch as config, reconcile_patch:
        result = expense.poll_stuck_expense_transfers()

    config.assert_not_called()
    assert result["intents_checked"] == 0


def test_transfer_polling_reaches_deactivated_tenants(tenant_sessions) -> None:
    """Neither scan had an ``Organization`` predicate, and an in-flight payout
    for a deactivated tenant still has to settle."""
    stale_patch, stuck_patch, config_patch, reconcile_patch = _poll_patches()
    with stale_patch, stuck_patch, config_patch, reconcile_patch:
        expense.poll_stuck_expense_transfers()

    assert tenant_sessions.discovery == {"include_inactive": True, "only": None}


# ── The retired seam ─────────────────────────────────────────────────


def test_the_module_no_longer_reaches_across_tenants() -> None:
    """The bypass is gone from this module, not merely unused.

    ``cross_org_session`` and the two id-grouping dicts it fed were the whole
    cross-tenant path here, and the architecture guard only sees the catalogue
    enumeration shape, not this one.
    """
    source = (expense.__file__ or "").replace(".pyc", ".py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "cross_org_session(" not in text
    assert "import cross_org_session" not in text
    assert "claims_by_org" not in text
    assert "stale_by_org" not in text
    assert "stuck_intent_meta" not in text
    assert "for_each_organization(include_inactive=True)" in text
