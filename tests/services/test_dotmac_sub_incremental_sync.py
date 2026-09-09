"""Tests for the dotmac_sub AR incremental-sync watermark.

Incident: the AR pull re-listed every invoice each cycle via OFFSET pagination
over an unindexed ``created_at`` sort, starving dotmac_sub's DB pool. The fix
pulls only rows with ``updated_at >= watermark`` and advances a per-entity
high-watermark. These tests cover:

- the pure advance logic (first-run full pull, advance to max, freeze at the
  first failed row, advance-only),
- the client forwarding ``updated_since`` + ascending order to the API,
- records parsing ``updated_at``,
- the DB-backed watermark get/advance helpers.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.models.finance.ar.external_sync import EntityType
from app.services.dotmac_sub.client import (
    DotmacSubClient,
    DotmacSubConfig,
    InvoiceLineRecord,
    InvoiceRecord,
    ResellerRecord,
    SubscriberRecord,
    _watermark_params,
)
from app.services.dotmac_sub.sync._base import BaseSyncMixin, next_watermark
from app.services.dotmac_sub.sync._invoices import InvoiceSyncMixin
from app.services.dotmac_sub.sync._invoices import _invoice_hash_payload
from app.services.dotmac_sub.sync._resellers import ResellerSyncMixin
from app.services.dotmac_sub.sync._subscribers import SubscriberSyncMixin

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _norm(dt):
    """Normalize to UTC-naive so tz-aware (Postgres) and tz-naive (SQLite test
    backend) reads of a ``DateTime(timezone=True)`` column compare equal."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Pure watermark-advance logic
# ---------------------------------------------------------------------------


def test_next_watermark_first_run_advances_to_max() -> None:
    # No prior watermark, no errors → advance to the highest processed row.
    assert next_watermark(None, _T0 + timedelta(days=2), None) == _T0 + timedelta(
        days=2
    )


def test_next_watermark_empty_pull_keeps_current() -> None:
    # Nothing seen (delta empty) → leave the cursor where it was.
    assert next_watermark(_T0, None, None) == _T0
    assert next_watermark(None, None, None) is None


def test_next_watermark_freezes_at_first_error() -> None:
    # A failed row must not be skipped: park the cursor at the earliest failure
    # (inclusive >= re-pulls it next cycle), even if later rows succeeded.
    max_ok = _T0 + timedelta(days=5)
    min_error = _T0 + timedelta(days=2)
    assert next_watermark(_T0, max_ok, min_error) == min_error


def test_next_watermark_is_advance_only() -> None:
    # Never move backward below the current cursor.
    assert next_watermark(_T0 + timedelta(days=10), _T0, None) == _T0 + timedelta(
        days=10
    )


# ---------------------------------------------------------------------------
# Client param building + forwarding
# ---------------------------------------------------------------------------


def test_watermark_params_only_sends_feed_filters() -> None:
    params = _watermark_params(None, None, "2026-01-01T12:00:00+00:00")
    assert params == {
        "updated_since": "2026-01-01T12:00:00+00:00",
    }


def test_watermark_params_omits_order_without_watermark() -> None:
    # Full pull (no watermark) keeps the API default ordering.
    assert _watermark_params("acct-1", "paid", None) == {
        "account_id": "acct-1",
        "status": "paid",
    }


def _client_capturing_params() -> tuple[DotmacSubClient, list[dict]]:
    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="t"))
    captured: list[dict] = []

    def _fake_request(method, endpoint, params=None, **kwargs):
        captured.append(dict(params or {}))
        return {"items": []}  # empty page → generator stops immediately

    client._request = MagicMock(side_effect=_fake_request)  # type: ignore[method-assign]
    return client, captured


def test_get_invoices_forwards_updated_since() -> None:
    client, captured = _client_capturing_params()
    list(client.get_invoices(updated_since="2026-05-01T00:00:00+00:00"))
    assert captured[0]["updated_since"] == "2026-05-01T00:00:00+00:00"
    assert captured[0]["limit"] == 500


def test_get_invoices_uses_lightweight_sync_feed() -> None:
    client, _ = _client_capturing_params()
    list(client.get_invoices())
    client._request.assert_called_once()
    assert client._request.call_args.args[:2] == ("GET", "/invoices/sync")


def test_get_invoices_without_watermark_sends_no_updated_since() -> None:
    client, captured = _client_capturing_params()
    list(client.get_invoices())
    assert "updated_since" not in captured[0]


def test_get_payments_and_credit_notes_forward_updated_since() -> None:
    client, captured = _client_capturing_params()
    list(client.get_payments(updated_since="2026-05-01T00:00:00+00:00"))
    list(client.get_credit_notes(updated_since="2026-05-01T00:00:00+00:00"))
    assert all(c.get("updated_since") == "2026-05-01T00:00:00+00:00" for c in captured)


def test_all_bulk_collections_use_bounded_sync_feeds() -> None:
    client, _ = _client_capturing_params()
    pulls = (
        (client.get_resellers, "/resellers/sync"),
        (client.get_subscribers, "/subscribers/sync"),
        (client.get_billing_accounts, "/billing-accounts/sync"),
        (client.get_invoices, "/invoices/sync"),
        (client.get_payments, "/payments/sync"),
        (client.get_credit_notes, "/credit-notes/sync"),
        (client.get_tax_rates, "/tax-rates/sync"),
        (client.get_payment_channels, "/payment-channels/sync"),
    )

    for pull, endpoint in pulls:
        client._request.reset_mock()
        list(pull())
        assert client._request.call_args.args[:2] == ("GET", endpoint)
        assert client._request.call_args.kwargs["params"]["limit"] == 500


def test_customer_feeds_forward_their_watermarks() -> None:
    client, captured = _client_capturing_params()
    watermark = "2026-05-01T00:00:00+00:00"

    list(client.get_resellers(updated_since=watermark))
    list(client.get_subscribers(updated_since=watermark))

    assert [params["updated_since"] for params in captured] == [
        watermark,
        watermark,
    ]


class _SubscriberSyncHarness(SubscriberSyncMixin):
    pass


class _ResellerSyncHarness(ResellerSyncMixin):
    pass


class _InvoiceSyncHarness(InvoiceSyncMixin):
    pass


def test_subscriber_sync_advances_customer_watermark() -> None:
    harness = _SubscriberSyncHarness()
    harness.db = MagicMock()
    harness.client = MagicMock()
    harness.client.get_subscribers.return_value = [
        SubscriberRecord(id="sub-1", updated_at=datetime(2026, 1, 2, 12, tzinfo=UTC))
    ]
    harness._get_sync_watermark = MagicMock(return_value=_T0)
    harness._advance_sync_watermark = MagicMock()
    harness._sync_single_subscriber = MagicMock()
    harness._reprime_tenant_context = MagicMock()

    result = harness.sync_subscribers()

    assert result.success
    assert harness.client.get_subscribers.call_count == 1
    assert harness.client.get_subscribers.call_args.kwargs["updated_since"] == (
        _T0.isoformat()
    )
    harness._advance_sync_watermark.assert_called_once_with(
        EntityType.CUSTOMER,
        datetime(2026, 1, 2, 12, tzinfo=UTC),
    )


def test_reseller_sync_advances_reseller_watermark() -> None:
    harness = _ResellerSyncHarness()
    harness.db = MagicMock()
    harness.client = MagicMock()
    harness.client.get_resellers.return_value = [
        ResellerRecord(
            id="reseller-1",
            name="Wholesale",
            updated_at=datetime(2026, 1, 3, 12, tzinfo=UTC),
        )
    ]
    harness._get_sync_watermark = MagicMock(return_value=_T0)
    harness._advance_sync_watermark = MagicMock()
    harness._sync_single_reseller = MagicMock()
    harness._reprime_tenant_context = MagicMock()

    result = harness.sync_resellers()

    assert result.success
    assert harness.client.get_resellers.call_count == 1
    assert harness.client.get_resellers.call_args.kwargs["updated_since"] == (
        _T0.isoformat()
    )
    harness._advance_sync_watermark.assert_called_once_with(
        EntityType.RESELLER,
        datetime(2026, 1, 3, 12, tzinfo=UTC),
    )


def test_invoice_sync_aborts_on_database_connection_error() -> None:
    invoice = InvoiceRecord(
        id="inv-db-error",
        account_id="acct-1",
        invoice_number="INV-DB-ERROR",
        status="issued",
        currency="NGN",
        subtotal=100,
        tax_total=0,
        total=100,
        balance_due=100,
        updated_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    harness = _InvoiceSyncHarness()
    harness.db = MagicMock()
    harness.db.begin_nested.side_effect = OperationalError(
        "SAVEPOINT sa_savepoint_1",
        {},
        Exception("another command is already in progress"),
    )
    harness.client = MagicMock()
    harness.client.get_invoices.return_value = [invoice]
    harness._get_sync_watermark = MagicMock(return_value=None)
    harness._advance_sync_watermark = MagicMock()

    with pytest.raises(OperationalError):
        harness.sync_invoices()

    harness.db.rollback.assert_called_once_with()
    harness._advance_sync_watermark.assert_not_called()


def test_parse_invoice_reads_updated_at() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))
    # Money facts are mandatory (strict parser) — this pin is about updated_at.
    rec = client._parse_invoice(
        {
            "id": "1",
            "account_id": "a",
            "currency": "NGN",
            "subtotal": "100.00",
            "tax_total": "0.00",
            "total": "100.00",
            "balance_due": "100.00",
            "updated_at": "2026-06-01T10:00:00+00:00",
        }
    )
    # Admitted as a TYPED instant, not the raw wire string.
    assert rec.updated_at == datetime(2026, 6, 1, 10, tzinfo=UTC)


def test_invoice_hash_payload_tracks_line_and_header_changes() -> None:
    line = InvoiceLineRecord(
        id="line-1",
        description="Internet service",
        quantity=1,
        unit_price=100,
        amount=100,
    )
    invoice = InvoiceRecord(
        id="inv-1",
        account_id="acct-1",
        invoice_number="INV-1",
        status="issued",
        currency="NGN",
        subtotal=100,
        tax_total=0,
        total=100,
        balance_due=100,
        due_at=datetime(2026, 6, 30, tzinfo=UTC),
        memo="Original",
        lines=(line,),
    )
    original = _invoice_hash_payload(invoice)

    # Wire records are frozen contracts (typed + immutable): a "changed"
    # payload is a NEW record, exactly as a re-fetch from Sub would produce —
    # never an in-place edit of an already-admitted record.
    changed_line = replace(line, description="Corrected service")
    invoice = replace(invoice, memo="Corrected", lines=(changed_line,))
    changed = _invoice_hash_payload(invoice)

    assert changed != original
    assert changed["lines"][0]["description"] == "Corrected service"
    assert changed["memo"] == "Corrected"


# ---------------------------------------------------------------------------
# DB-backed watermark helpers
# ---------------------------------------------------------------------------


class _WatermarkHarness(BaseSyncMixin):
    """Minimal BaseSyncMixin that skips the config/DB lookup in __init__."""

    def __init__(self, db, organization_id):
        self.db = db
        self.organization_id = organization_id


def test_watermark_get_advance_roundtrip(db_session) -> None:
    org = uuid.uuid4()
    h = _WatermarkHarness(db_session, org)

    # Never synced → None (caller does a full pull).
    assert h._get_sync_watermark(EntityType.INVOICE) is None

    # First advance inserts the cursor.
    h._advance_sync_watermark(EntityType.INVOICE, _T0 + timedelta(days=1))
    db_session.flush()
    assert _norm(h._get_sync_watermark(EntityType.INVOICE)) == _norm(
        _T0 + timedelta(days=1)
    )

    # Advancing forward moves the cursor.
    h._advance_sync_watermark(EntityType.INVOICE, _T0 + timedelta(days=3))
    db_session.flush()
    assert _norm(h._get_sync_watermark(EntityType.INVOICE)) == _norm(
        _T0 + timedelta(days=3)
    )

    # A backward value is ignored (advance-only).
    h._advance_sync_watermark(EntityType.INVOICE, _T0)
    db_session.flush()
    assert _norm(h._get_sync_watermark(EntityType.INVOICE)) == _norm(
        _T0 + timedelta(days=3)
    )


def test_watermark_is_per_entity_type(db_session) -> None:
    org = uuid.uuid4()
    h = _WatermarkHarness(db_session, org)
    h._advance_sync_watermark(EntityType.INVOICE, _T0 + timedelta(days=1))
    h._advance_sync_watermark(EntityType.PAYMENT, _T0 + timedelta(days=2))
    db_session.flush()
    assert _norm(h._get_sync_watermark(EntityType.INVOICE)) == _norm(
        _T0 + timedelta(days=1)
    )
    assert _norm(h._get_sync_watermark(EntityType.PAYMENT)) == _norm(
        _T0 + timedelta(days=2)
    )
    assert h._get_sync_watermark(EntityType.CREDIT_NOTE) is None
