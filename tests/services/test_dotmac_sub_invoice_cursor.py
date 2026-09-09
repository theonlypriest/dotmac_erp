from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlalchemy import func, select

from app.models.finance.ar.dotmac_sub_sync_watermark import DotmacSubSyncWatermark
from app.models.finance.ar.external_sync import EntityType
from app.services.dotmac_sub.client import InvoiceRecord
from app.services.dotmac_sub.sync._base import BaseSyncMixin, SyncWatermarkPosition
from app.services.dotmac_sub.sync._invoices import InvoiceSyncMixin

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _norm(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


class _InvoiceSyncHarness(InvoiceSyncMixin):
    pass


class _WatermarkHarness(BaseSyncMixin):
    def __init__(self, db, organization_id):
        self.db = db
        self.organization_id = organization_id


def _invoice_record(external_id: str, updated_at: datetime) -> InvoiceRecord:
    return InvoiceRecord(
        id=external_id,
        account_id="acct-1",
        invoice_number=f"INV-{external_id}",
        status="issued",
        currency="NGN",
        subtotal=100,
        tax_total=0,
        total=100,
        balance_due=100,
        updated_at=updated_at,
    )


def _invoice_harness_for_cursor(
    rows: list[InvoiceRecord],
    position: SyncWatermarkPosition,
) -> _InvoiceSyncHarness:
    harness = _InvoiceSyncHarness()
    harness.db = MagicMock()
    harness.client = MagicMock()
    harness.client.get_invoices.return_value = rows
    harness._get_sync_watermark_position = MagicMock(return_value=position)
    harness._advance_sync_watermark_position = MagicMock()
    harness._sync_single_invoice = MagicMock()
    harness._reprime_tenant_context = MagicMock()
    return harness


def test_invoice_sync_bounds_work_after_compound_cursor() -> None:
    shared_at = datetime(2026, 1, 2, 12, tzinfo=UTC)
    rows = [
        _invoice_record("inv-001", shared_at),
        _invoice_record("inv-002", shared_at),
        _invoice_record("inv-003", shared_at),
        _invoice_record("inv-004", shared_at + timedelta(minutes=1)),
    ]
    harness = _invoice_harness_for_cursor(
        rows,
        SyncWatermarkPosition(shared_at, "inv-002"),
    )

    result = harness.sync_invoices(batch_size=1)

    assert result.success
    assert "invoice work limit (1) reached" in result.message
    synced_ids = [
        call.args[0].id for call in harness._sync_single_invoice.call_args_list
    ]
    assert synced_ids == ["inv-003"]
    advanced = harness._advance_sync_watermark_position.call_args.args[1]
    assert advanced == SyncWatermarkPosition(shared_at, "inv-003")


def test_invoice_sync_continues_after_same_timestamp_boundary() -> None:
    shared_at = datetime(2026, 1, 2, 12, tzinfo=UTC)
    next_at = shared_at + timedelta(minutes=1)
    rows = [
        _invoice_record("inv-001", shared_at),
        _invoice_record("inv-002", shared_at),
        _invoice_record("inv-003", shared_at),
        _invoice_record("inv-004", next_at),
    ]
    harness = _invoice_harness_for_cursor(
        rows,
        SyncWatermarkPosition(shared_at, "inv-003"),
    )

    result = harness.sync_invoices(batch_size=1)

    assert result.success
    synced_ids = [
        call.args[0].id for call in harness._sync_single_invoice.call_args_list
    ]
    assert synced_ids == ["inv-004"]
    advanced = harness._advance_sync_watermark_position.call_args.args[1]
    assert advanced == SyncWatermarkPosition(next_at, "inv-004")


def test_invoice_sync_does_not_advance_past_failed_compound_position() -> None:
    shared_at = datetime(2026, 1, 2, 12, tzinfo=UTC)
    next_at = shared_at + timedelta(minutes=1)
    rows = [
        _invoice_record("inv-002", shared_at),
        _invoice_record("inv-003", shared_at),
        _invoice_record("inv-004", next_at),
    ]
    harness = _invoice_harness_for_cursor(
        rows,
        SyncWatermarkPosition(shared_at, "inv-001"),
    )
    harness._sync_single_invoice.side_effect = [
        None,
        ValueError("posting failed"),
        None,
    ]

    result = harness.sync_invoices(batch_size=10)

    assert result.success
    assert len(result.errors) == 1
    synced_ids = [
        call.args[0].id for call in harness._sync_single_invoice.call_args_list
    ]
    assert synced_ids == ["inv-002", "inv-003", "inv-004"]
    advanced = harness._advance_sync_watermark_position.call_args.args[1]
    assert advanced == SyncWatermarkPosition(shared_at, "inv-002")


def test_compound_watermark_position_roundtrip(db_session) -> None:
    org = uuid.uuid4()
    h = _WatermarkHarness(db_session, org)
    first = SyncWatermarkPosition(_T0 + timedelta(days=1), "inv-001")
    second = SyncWatermarkPosition(_T0 + timedelta(days=1), "inv-002")

    assert h._get_sync_watermark_position(EntityType.INVOICE) == SyncWatermarkPosition(
        None, None
    )

    h._advance_sync_watermark_position(EntityType.INVOICE, first)
    db_session.flush()
    stored = h._get_sync_watermark_position(EntityType.INVOICE)
    assert _norm(stored.watermark_at) == _norm(first.watermark_at)
    assert stored.external_id == "inv-001"

    h._advance_sync_watermark_position(EntityType.INVOICE, second)
    db_session.flush()
    stored = h._get_sync_watermark_position(EntityType.INVOICE)
    assert _norm(stored.watermark_at) == _norm(second.watermark_at)
    assert stored.external_id == "inv-002"


def test_compound_watermark_position_is_advance_only(db_session) -> None:
    org = uuid.uuid4()
    h = _WatermarkHarness(db_session, org)

    h._advance_sync_watermark_position(
        EntityType.INVOICE,
        SyncWatermarkPosition(_T0 + timedelta(days=3), "inv-003"),
    )
    h._advance_sync_watermark_position(
        EntityType.INVOICE,
        SyncWatermarkPosition(_T0 + timedelta(days=2), "inv-999"),
    )
    h._advance_sync_watermark_position(
        EntityType.INVOICE,
        SyncWatermarkPosition(_T0 + timedelta(days=3), "inv-002"),
    )
    h._advance_sync_watermark_position(
        EntityType.INVOICE,
        SyncWatermarkPosition(_T0 + timedelta(days=3), "inv-004"),
    )
    db_session.flush()

    stored = h._get_sync_watermark_position(EntityType.INVOICE)
    assert _norm(stored.watermark_at) == _norm(_T0 + timedelta(days=3))
    assert stored.external_id == "inv-004"
    row_count = db_session.scalar(
        select(func.count())
        .select_from(DotmacSubSyncWatermark)
        .where(
            DotmacSubSyncWatermark.organization_id == org,
            DotmacSubSyncWatermark.entity_type == EntityType.INVOICE.value,
        )
    )
    assert row_count == 1
