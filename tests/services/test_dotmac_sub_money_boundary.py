"""E4 money-boundary behavior of the Sub-facing schemas.

Golden-style pins for the typed Money boundary on the dotmac_sub connector
records (invoice / credit-note / payment WHT evidence) and the Sub
payables command schema: exact NGN round-trips and byte-exact serialization
where the touched schemas carry money, plus every fail-closed rejection, and
proof that a rejected row fails ITS savepoint (raises) without any DB work.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.sync.sub_operational import SubPurchaseInvoicePayload
from app.services.dotmac_sub.client import (
    AllocationRecord,
    CreditNoteRecord,
    DotmacSubClient,
    DotmacSubConfig,
    DotmacSubParseError,
    InvoiceLineRecord,
    InvoiceRecord,
    PaymentRecord,
    TaxApplication,
)
from app.services.dotmac_sub.sync._credit_notes import CreditNoteSyncMixin
from app.services.dotmac_sub.sync._base import SyncWatermarkPosition
from app.services.dotmac_sub.sync._invoices import InvoiceSyncMixin
from app.services.dotmac_sub.sync._payments import PaymentSyncMixin
from app.services.dotmac_sub.sync._types import SyncResult
from app.services.finance.money_boundary import (
    MoneyBoundaryError,
    from_money,
    serialize_money,
)


def _invoice(**overrides: object) -> InvoiceRecord:
    defaults: dict = {
        "id": "inv-1",
        "account_id": "acct-1",
        "invoice_number": "SUB-INV-1",
        "status": "posted",
        "currency": "NGN",
        "subtotal": Decimal("45000.00"),
        "tax_total": Decimal("3375.00"),
        "total": Decimal("48375.00"),
        "balance_due": Decimal("48375.00"),
        "lines": (
            InvoiceLineRecord(
                id="l1",
                description="Service",
                quantity=Decimal("1"),
                unit_price=Decimal("45000.00"),
                amount=Decimal("45000.00"),
            ),
        ),
    }
    defaults.update(overrides)
    return InvoiceRecord(**defaults)


def _payment(**overrides: object) -> PaymentRecord:
    defaults: dict = {
        "id": "pay-1",
        "account_id": "acct-1",
        "billing_account_id": "acct-1",
        "amount": Decimal("107.50"),
        "currency": "NGN",
        "status": "succeeded",
        "gross_amount": Decimal("107.50"),
        "net_amount": Decimal("100.00"),
        "wht_amount": Decimal("7.50"),
        "wht_rate": Decimal("7.5"),
    }
    defaults.update(overrides)
    return PaymentRecord(**defaults)


# ---------------------------------------------------------------------------
# Invoice / credit-note headers: typed Money, exact NGN round-trip
# ---------------------------------------------------------------------------


def test_invoice_boundary_money_round_trips_exactly() -> None:
    money = _invoice().boundary_money()
    assert from_money(money.subtotal) == (Decimal("45000.00"), "NGN")
    assert from_money(money.tax_total) == (Decimal("3375.00"), "NGN")
    assert from_money(money.total) == (Decimal("48375.00"), "NGN")
    assert money.balance_due is not None
    assert from_money(money.balance_due) == (Decimal("48375.00"), "NGN")
    # Byte-exact connector serialization for the header facts.
    assert serialize_money(money.total) == {"amount": "48375.00", "currency": "NGN"}


def test_invoice_boundary_money_rejects_missing_currency() -> None:
    with pytest.raises(MoneyBoundaryError, match="currency code is required"):
        _invoice(currency="").boundary_money()


def test_invoice_boundary_money_rejects_excess_precision() -> None:
    with pytest.raises(MoneyBoundaryError, match="subtotal.*minor-unit precision"):
        _invoice(subtotal=Decimal("45000.005")).boundary_money()


def test_invoice_boundary_money_validates_allocations() -> None:
    bad = _invoice(
        allocations=(
            AllocationRecord(
                id="a1",
                payment_id="p1",
                invoice_id="inv-1",
                amount=Decimal("10.005"),
            ),
        )
    )
    with pytest.raises(MoneyBoundaryError, match="allocation a1"):
        bad.boundary_money()


def test_credit_note_boundary_money_round_trips_exactly() -> None:
    cn = CreditNoteRecord(
        id="cn-1",
        account_id="acct-1",
        invoice_id="inv-1",
        credit_number="SUB-CN-1",
        status="applied",
        currency="NGN",
        subtotal=Decimal("1000.00"),
        tax_total=Decimal("75.00"),
        total=Decimal("1075.00"),
        applied_total=Decimal("1075.00"),
    )
    money = cn.boundary_money()
    assert from_money(money.total) == (Decimal("1075.00"), "NGN")
    assert money.applied_total is not None
    assert serialize_money(money.applied_total) == {
        "amount": "1075.00",
        "currency": "NGN",
    }


# ---------------------------------------------------------------------------
# Payment WHT evidence: typed Money, unchanged NGN facts
# ---------------------------------------------------------------------------


def test_payment_wht_evidence_round_trips_exactly() -> None:
    money = _payment().boundary_money()
    assert from_money(money.amount) == (Decimal("107.50"), "NGN")
    assert money.gross_amount is not None and money.net_amount is not None
    assert serialize_money(money.gross_amount) == {
        "amount": "107.50",
        "currency": "NGN",
    }
    assert serialize_money(money.net_amount) == {
        "amount": "100.00",
        "currency": "NGN",
    }
    assert serialize_money(money.wht_amount) == {"amount": "7.50", "currency": "NGN"}


def test_payment_boundary_money_rejects_excess_precision() -> None:
    with pytest.raises(MoneyBoundaryError, match="wht_amount.*minor-unit precision"):
        _payment(wht_amount=Decimal("7.505")).boundary_money()


def test_payment_boundary_money_optional_gross_net() -> None:
    money = _payment(gross_amount=None, net_amount=None).boundary_money()
    assert money.gross_amount is None
    assert money.net_amount is None


# ---------------------------------------------------------------------------
# The sync mixins fail the ROW (savepoint), before any DB/session work
# ---------------------------------------------------------------------------


class _InvoiceStub(InvoiceSyncMixin):
    def __init__(self) -> None:
        self._compute_hash = lambda payload: "hash"
        self._has_changed = lambda *args: True  # "changed" → proceed to admit


def test_invoice_sync_rejects_bad_money_before_any_db_work() -> None:
    result = SyncResult(success=True, entity_type="invoices")
    bad = _invoice(total=Decimal("48375.005"))
    with pytest.raises(MoneyBoundaryError, match="total.*minor-unit precision"):
        _InvoiceStub()._sync_single_invoice(bad, None, result, True)


class _PaymentStub(PaymentSyncMixin):
    pass


def test_payment_sync_rejects_bad_money_before_any_db_work() -> None:
    result = SyncResult(success=True, entity_type="payments")
    bad = _payment(net_amount=Decimal("100.005"))
    with pytest.raises(MoneyBoundaryError, match="net_amount.*minor-unit precision"):
        _PaymentStub()._sync_single_payment(bad, result, None, True)


# ---------------------------------------------------------------------------
# Admission runs on EVERY pass: an UNCHANGED record with invalid money facts
# fails its row — it cannot ride the unchanged-skip (or a status branch)
# around the boundary forever
# ---------------------------------------------------------------------------


class _UnchangedInvoiceStub(InvoiceSyncMixin):
    def __init__(self) -> None:
        self._compute_hash = lambda payload: "hash"
        self._has_changed = lambda *args: False  # "unchanged" → would skip


def test_unchanged_invoice_with_invalid_money_still_fails_admission() -> None:
    result = SyncResult(success=True, entity_type="invoices")
    bad = _invoice(total=Decimal("48375.005"))
    with pytest.raises(MoneyBoundaryError, match="total.*minor-unit precision"):
        _UnchangedInvoiceStub()._sync_single_invoice(bad, None, result, True)
    assert result.skipped == 0  # rejected, not silently skipped


def test_unchanged_invoice_with_fake_currency_still_fails_admission() -> None:
    result = SyncResult(success=True, entity_type="invoices")
    bad = _invoice(currency="ZZZ")
    with pytest.raises(MoneyBoundaryError, match="not provisioned"):
        _UnchangedInvoiceStub()._sync_single_invoice(bad, None, result, True)


class _UnchangedCreditNoteStub(CreditNoteSyncMixin):
    def __init__(self) -> None:
        self._compute_hash = lambda payload: "hash"
        self._has_changed = lambda *args: False


def test_unchanged_credit_note_with_invalid_money_still_fails_admission() -> None:
    result = SyncResult(success=True, entity_type="credit_notes")
    bad = CreditNoteRecord(
        id="cn-1",
        account_id="acct-1",
        invoice_id="inv-1",
        credit_number="SUB-CN-1",
        status="applied",
        currency="NGN",
        subtotal=Decimal("1000.005"),
        tax_total=Decimal("75.00"),
        total=Decimal("1075.00"),
    )
    with pytest.raises(MoneyBoundaryError, match="subtotal.*minor-unit precision"):
        _UnchangedCreditNoteStub()._sync_single_credit_note(bad, None, result, True)
    assert result.skipped == 0


def test_unsettled_payment_with_invalid_money_still_fails_admission() -> None:
    # A refunded/voided payment's amounts are still consumed (status hash,
    # reversal path) — admission runs before the settled-status branch.
    result = SyncResult(success=True, entity_type="payments")
    bad = _payment(status="refunded", wht_amount=Decimal("7.505"))
    with pytest.raises(MoneyBoundaryError, match="wht_amount.*minor-unit precision"):
        _PaymentStub()._sync_single_payment(bad, result, None, True)


# ---------------------------------------------------------------------------
# Sub payables command schema (vendor invoice)
# ---------------------------------------------------------------------------


def _payables_payload(**overrides: object) -> dict:
    payload: dict = {
        "source_invoice_id": "src-inv-1",
        "source_invoice_number": "VND-1",
        "source_project_id": "proj-1",
        "installation_project_id": "inst-1",
        "erp_purchase_order_id": "PO-0001",
        "vendor_name": "Vendor Ltd",
        "currency": "NGN",
        "tax_rate_percent": Decimal("7.5"),
        "subtotal": Decimal("1000.00"),
        "tax_total": Decimal("75.00"),
        "total": Decimal("1075.00"),
        "items": [
            {
                "description": "Fibre splice",
                "quantity": Decimal("2"),
                "unit_price": Decimal("500.00"),
                "amount": Decimal("1000.00"),
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_payables_payload_accepts_exact_money() -> None:
    payload = SubPurchaseInvoicePayload(**_payables_payload())
    assert payload.total == Decimal("1075.00")


def test_payables_payload_rejects_excess_header_precision() -> None:
    with pytest.raises(ValidationError, match="minor-unit precision"):
        SubPurchaseInvoicePayload(**_payables_payload(total=Decimal("1075.005")))


def test_payables_payload_rejects_excess_line_precision() -> None:
    bad_items = [
        {
            "description": "Fibre splice",
            "quantity": Decimal("2"),
            "unit_price": Decimal("500.00"),
            "amount": Decimal("1000.005"),
        }
    ]
    with pytest.raises(ValidationError, match="line 1 amount"):
        SubPurchaseInvoicePayload(**_payables_payload(items=bad_items))


def test_payables_payload_rejects_invalid_currency() -> None:
    with pytest.raises(ValidationError, match="invalid ISO-4217"):
        SubPurchaseInvoicePayload(**_payables_payload(currency="N1N"))


def test_payables_payload_unit_price_keeps_erp_decimal_scale() -> None:
    # unit_price is a rate, not boundary money — extra scale stays legal.
    items = [
        {
            "description": "Cable per metre",
            "quantity": Decimal("3"),
            "unit_price": Decimal("333.3333"),
            "amount": Decimal("1000.00"),
        }
    ]
    payload = SubPurchaseInvoicePayload(**_payables_payload(items=items))
    assert payload.items[0].unit_price == Decimal("333.3333")


# ---------------------------------------------------------------------------
# Payables ingress: floats rejected BEFORE pydantic's Decimal coercion
# ---------------------------------------------------------------------------
# Policy (matches the connector's E4 outbound {"amount": "48375.00"} shape):
# money arrives as a string (canonical) or exact int/Decimal; float is
# rejected on the raw value in mode="before" validators — pydantic v2
# otherwise coerces float→Decimal before after-validators can see it.


def _payables_json(**overrides: object) -> str:
    payload: dict = {
        "source_invoice_id": "src-inv-1",
        "source_invoice_number": "VND-1",
        "source_project_id": "proj-1",
        "installation_project_id": "inst-1",
        "erp_purchase_order_id": "PO-0001",
        "vendor_name": "Vendor Ltd",
        "currency": "NGN",
        "tax_rate_percent": "7.5",
        "subtotal": "1000.00",
        "tax_total": "75.00",
        "total": "1075.00",
        "items": [
            {
                "description": "Fibre splice",
                "quantity": "2",
                "unit_price": "500.00",
                "amount": "1000.00",
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_payables_json_ingress_accepts_string_money_exactly() -> None:
    payload = SubPurchaseInvoicePayload.model_validate_json(_payables_json())
    assert payload.total == Decimal("1075.00")
    assert payload.items[0].amount == Decimal("1000.00")


def test_payables_json_ingress_rejects_bare_json_float_header() -> None:
    # A bare fractional JSON number materializes as float — refused pre-coercion.
    raw = _payables_json().replace('"total": "1075.00"', '"total": 1075.005')
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate_json(raw)
    # Even an exactly-representable JSON float is refused — policy is by
    # type, not by luck of representability.
    raw = _payables_json().replace('"total": "1075.00"', '"total": 1075.5')
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate_json(raw)


def test_payables_json_ingress_rejects_bare_json_float_line_amount() -> None:
    raw = _payables_json().replace('"amount": "1000.00"', '"amount": 1000.25')
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate_json(raw)


def test_payables_json_ingress_rejects_integer_money_tokens() -> None:
    # Wire policy: external money is a canonical decimal STRING only —
    # EVERY JSON number token is rejected, integers included.
    raw = _payables_json().replace('"subtotal": "1000.00"', '"subtotal": 1000')
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate_json(raw)
    raw = _payables_json().replace('"amount": "1000.00"', '"amount": 1000')
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate_json(raw)


def test_payables_dict_ingress_rejects_python_float_and_int() -> None:
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate(_payables_payload(total=1075.00))
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate(_payables_payload(subtotal=1000))
    bad_items = [
        {
            "description": "Fibre splice",
            "quantity": Decimal("2"),
            "unit_price": Decimal("500.00"),
            "amount": 1000.25,
        }
    ]
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate(_payables_payload(items=bad_items))


def test_payables_ingress_rejects_non_finite_values() -> None:
    # NaN/Infinity as strings parse as Decimals — refused explicitly at the
    # ingress layer with the typed message, at header and line level.
    for bad in ("NaN", "Infinity", "-Infinity"):
        raw = _payables_json().replace('"total": "1075.00"', f'"total": "{bad}"')
        with pytest.raises(ValidationError, match="non-finite"):
            SubPurchaseInvoicePayload.model_validate_json(raw)
        raw = _payables_json().replace('"amount": "1000.00"', f'"amount": "{bad}"')
        with pytest.raises(ValidationError, match="non-finite"):
            SubPurchaseInvoicePayload.model_validate_json(raw)
    # As Python floats they are refused by type (floats never enter).
    with pytest.raises(ValidationError, match="money at ingress"):
        SubPurchaseInvoicePayload.model_validate(_payables_payload(total=float("inf")))
    with pytest.raises(ValidationError, match="non-finite"):
        SubPurchaseInvoicePayload.model_validate(
            _payables_payload(total=Decimal("NaN"))
        )


def test_payables_dict_ingress_still_accepts_internal_decimal() -> None:
    # Internal Python callers may pass Decimal (finite, canonical scale) —
    # the strings-only rule is a WIRE rule.
    payload = SubPurchaseInvoicePayload.model_validate(_payables_payload())
    assert payload.total == Decimal("1075.00")


@pytest.mark.parametrize(
    "literal", ["1e3", " 100.00 ", "+100.00", "01.00", ".50", "100."]
)
def test_payables_ingress_rejects_every_non_canonical_spelling(
    literal: str,
) -> None:
    # Field-level (currency-independent) half of the canonical grammar: the
    # ONE lexical wire representation, enforced on the raw string before any
    # coercion — at header and line level.
    raw = _payables_json().replace('"total": "1075.00"', f'"total": "{literal}"')
    with pytest.raises(ValidationError, match="non-canonical money string"):
        SubPurchaseInvoicePayload.model_validate_json(raw)
    raw = _payables_json().replace('"amount": "1000.00"', f'"amount": "{literal}"')
    with pytest.raises(ValidationError, match="non-canonical money string"):
        SubPurchaseInvoicePayload.model_validate_json(raw)


@pytest.mark.parametrize("literal", ["1075", "1075.0", "1075.000"])
def test_payables_ingress_requires_exact_minor_unit_digits(literal: str) -> None:
    # Model-level (currency-aware) half: with the sibling currency known,
    # every money value must carry EXACTLY the currency's minor-unit digit
    # count — "1075" and "1075.0" are valid lexemes but not the canonical
    # NGN form.
    raw = _payables_json().replace('"total": "1075.00"', f'"total": "{literal}"')
    with pytest.raises(ValidationError, match="fractional digits"):
        SubPurchaseInvoicePayload.model_validate_json(raw)
    # Internal Decimal callers are held to the same exact scale.
    with pytest.raises(ValidationError, match="fractional digits"):
        SubPurchaseInvoicePayload.model_validate(
            _payables_payload(total=Decimal(literal))
        )


# ---------------------------------------------------------------------------
# Canaries through the REAL DotmacSubClient parsers (raw wire payloads)
# ---------------------------------------------------------------------------


def _client() -> DotmacSubClient:
    return DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="t"))


def _raw_invoice(**overrides: object) -> dict:
    payload: dict = {
        "id": "inv-raw-1",
        "account_id": "acct-1",
        "invoice_number": "SUB-INV-9",
        "status": "posted",
        "currency": "NGN",
        "subtotal": "45000.00",
        "tax_total": "3375.00",
        "total": "48375.00",
        "balance_due": "48375.00",
        "updated_at": "2026-07-01T10:00:00+00:00",
        "lines": [
            {
                "id": "l1",
                "description": "Service",
                "quantity": "1",
                "unit_price": "45000.00",
                "amount": "45000.00",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _raw_payment(**overrides: object) -> dict:
    payload: dict = {
        "id": "pay-raw-1",
        "account_id": "acct-1",
        "billing_account_id": "acct-1",
        "amount": "107.50",
        "currency": "NGN",
        "status": "succeeded",
        "updated_at": "2026-07-01T11:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _raw_credit_note(**overrides: object) -> dict:
    payload: dict = {
        "id": "cn-raw-1",
        "account_id": "acct-1",
        "credit_number": "SUB-CN-9",
        "status": "applied",
        "currency": "NGN",
        "subtotal": "1000.00",
        "tax_total": "75.00",
        "total": "1075.00",
        "applied_total": "1075.00",
        "updated_at": "2026-07-01T12:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_parse_invoice_happy_path_is_exact() -> None:
    inv = _client()._parse_invoice(_raw_invoice())
    assert inv.total == Decimal("48375.00")
    assert inv.currency == "NGN"
    assert inv.lines[0].amount == Decimal("45000.00")
    money = inv.boundary_money()
    assert serialize_money(money.total) == {"amount": "48375.00", "currency": "NGN"}


def test_parse_invoice_rejects_malformed_amount() -> None:
    with pytest.raises(DotmacSubParseError, match="malformed money fact") as exc_info:
        _client()._parse_invoice(_raw_invoice(total="not-money"))
    # The typed error carries row identity + updated_at so the sync loop can
    # fail the row and hold the watermark at it.
    assert exc_info.value.record == "Sub invoice inv-raw-1"
    # Positioned at the row's PARSED instant (not the raw wire string): the
    # collector hands it straight to WatermarkProgress.
    assert exc_info.value.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Admitted evidence is immutable IN DEPTH, not just at its top level
# ---------------------------------------------------------------------------


def test_admitted_collections_are_tuples_not_lists() -> None:
    """A frozen dataclass with a ``list`` field is only SHALLOWLY immutable.

    Every collection an admitted record carries is a tuple, so the record
    cannot be edited in place through its collections either.
    """
    inv = _client()._parse_invoice(
        _raw_invoice(
            payment_allocations=[
                {
                    "id": "a1",
                    "payment_id": "p1",
                    "invoice_id": "inv-raw-1",
                    "amount": "10.00",
                }
            ]
        )
    )
    assert isinstance(inv.lines, tuple)
    assert isinstance(inv.allocations, tuple)

    pay = _client()._parse_payment(
        _raw_payment(
            allocations=[
                {
                    "id": "a1",
                    "payment_id": "pay-raw-1",
                    "invoice_id": "inv-1",
                    "amount": "10.00",
                }
            ]
        )
    )
    assert isinstance(pay.allocations, tuple)

    cn = _client()._parse_credit_note(
        _raw_credit_note(
            lines=[
                {
                    "id": "cl1",
                    "description": "Refund",
                    "quantity": "1",
                    "unit_price": "1000.00",
                    "amount": "1000.00",
                }
            ]
        )
    )
    assert isinstance(cn.lines, tuple)

    # No admitted record may carry a bare mutable container of any kind.
    for record in (inv, inv.lines[0], inv.allocations[0], pay, cn, cn.lines[0]):
        for name, value in vars(record).items():
            assert not isinstance(value, (list, dict, set)), (
                f"{type(record).__name__}.{name} is a mutable "
                f"{type(value).__name__}; admitted evidence must be immutable"
            )


def test_admitted_collection_cannot_be_mutated_in_place() -> None:
    inv = _client()._parse_invoice(_raw_invoice())
    extra = InvoiceLineRecord(
        id="l2",
        description="Smuggled",
        quantity=Decimal("1"),
        unit_price=Decimal("1.00"),
        amount=Decimal("1.00"),
    )
    with pytest.raises(AttributeError):
        inv.lines.append(extra)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        inv.lines[0] = extra  # type: ignore[index]
    with pytest.raises(AttributeError):
        inv.lines.sort()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        del inv.lines[0]  # type: ignore[attr-defined]
    # Reassigning the field itself is refused by the frozen dataclass.
    with pytest.raises(FrozenInstanceError):
        inv.lines = (extra,)  # type: ignore[misc]
    assert [line.id for line in inv.lines] == ["l1"]


def test_nested_admitted_line_record_is_itself_frozen() -> None:
    inv = _client()._parse_invoice(_raw_invoice())
    with pytest.raises(FrozenInstanceError):
        inv.lines[0].amount = Decimal("1.00")  # type: ignore[misc]
    assert inv.lines[0].amount == Decimal("45000.00")


def test_nested_admitted_allocation_record_is_itself_frozen() -> None:
    pay = _client()._parse_payment(
        _raw_payment(
            allocations=[
                {
                    "id": "a1",
                    "payment_id": "pay-raw-1",
                    "invoice_id": "inv-1",
                    "amount": "10.00",
                }
            ]
        )
    )
    with pytest.raises(FrozenInstanceError):
        pay.allocations[0].amount = Decimal("999.00")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pay.allocations.append(pay.allocations[0])  # type: ignore[attr-defined]
    assert pay.allocations[0].amount == Decimal("10.00")


def test_a_changed_payload_is_a_new_record_not_an_edit() -> None:
    """The sanctioned way to represent a Sub-side change."""
    inv = _client()._parse_invoice(_raw_invoice())
    changed = replace(inv, lines=(*inv.lines, inv.lines[0]))
    assert len(inv.lines) == 1  # the admitted record is untouched
    assert len(changed.lines) == 2
    assert isinstance(changed.lines, tuple)


# ---------------------------------------------------------------------------
# Admission is TYPED: timestamps are datetimes, closed statuses are enums
# ---------------------------------------------------------------------------


def test_admitted_timestamps_are_datetimes_not_wire_strings() -> None:
    inv = _client()._parse_invoice(
        _raw_invoice(
            issued_at="2026-07-01",  # date-only spelling Sub also emits
            due_at="2026-07-31T00:00:00Z",
            paid_at="2026-07-15T08:30:00+00:00",
        )
    )
    assert inv.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)
    assert inv.issued_at == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert inv.due_at == datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert inv.paid_at == datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc)

    pay = _client()._parse_payment(
        _raw_payment(paid_at="2026-07-02T09:00:00Z", wht_resolved_at="2026-07-03")
    )
    assert pay.paid_at == datetime(2026, 7, 2, 9, tzinfo=timezone.utc)
    assert pay.wht_resolved_at == datetime(2026, 7, 3, tzinfo=timezone.utc)

    cn = _client()._parse_credit_note(_raw_credit_note(issued_at="2026-07-04"))
    assert cn.issued_at == datetime(2026, 7, 4, tzinfo=timezone.utc)

    sub = _client()._parse_subscriber(
        {
            "id": "sub-1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-07-01T10:00:00+00:00",
        }
    )
    assert sub.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert sub.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)

    res = _client()._parse_reseller(
        {"id": "r-1", "name": "Wholesale", "updated_at": "2026-07-01T10:00:00+00:00"}
    )
    assert res.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)


def test_naive_wire_instant_is_admitted_as_utc() -> None:
    inv = _client()._parse_invoice(_raw_invoice(updated_at="2026-07-01T10:00:00"))
    assert inv.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)


def test_malformed_non_position_timestamp_is_a_POSITIONED_row_failure() -> None:
    """``updated_at`` parses first, so a bad ``issued_at`` still knows where
    it is — it parks the cursor rather than freezing it."""
    with pytest.raises(DotmacSubParseError, match="issued_at.*ISO8601") as exc_info:
        _client()._parse_invoice(_raw_invoice(issued_at="not-a-timestamp"))
    assert exc_info.value.updated_at == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["not-a-timestamp", 1730000000, {"t": 1}])
def test_malformed_updated_at_is_an_UNPOSITIONED_row_failure(bad: object) -> None:
    """The cursor-freeze contract (round 2/3) is unchanged by typing."""
    with pytest.raises(DotmacSubParseError, match="updated_at") as exc_info:
        _client()._parse_invoice(_raw_invoice(updated_at=bad))
    assert exc_info.value.updated_at is None


def test_closed_tax_application_set_is_admitted_as_a_typed_member() -> None:
    inv = _client()._parse_invoice(
        _raw_invoice(
            lines=[
                {"id": "l1", "amount": "1.00", "tax_application": "INCLUSIVE"},
                {"id": "l2", "amount": "1.00", "tax_application": "exempt"},
                {"id": "l3", "amount": "1.00"},  # absent -> documented default
            ]
        )
    )
    assert [line.tax_application for line in inv.lines] == [
        TaxApplication.INCLUSIVE,
        TaxApplication.EXEMPT,
        TaxApplication.EXCLUSIVE,
    ]


def test_unknown_tax_application_is_rejected_at_parse() -> None:
    """Previously a bogus application rode through silently whenever the line
    carried no ``tax_rate_id``; the closed set now fails the row."""
    with pytest.raises(DotmacSubParseError, match="unsupported tax_application"):
        _client()._parse_invoice(
            _raw_invoice(
                lines=[{"id": "l1", "amount": "1.00", "tax_application": "reverse"}]
            )
        )
    with pytest.raises(DotmacSubParseError, match="unsupported tax_application"):
        _client()._parse_credit_note(
            _raw_credit_note(
                lines=[{"id": "cl1", "amount": "1.00", "tax_application": "vat-free"}]
            )
        )


def test_open_status_vocabularies_stay_data_not_errors() -> None:
    """Documented as ``str`` on the records: Sub owns and extends these, and
    ERP's mappings have catch-alls. An unknown member must NOT fail a row."""
    inv = _client()._parse_invoice(_raw_invoice(status="awaiting_dunning_review"))
    assert inv.status == "awaiting_dunning_review"
    pay = _client()._parse_payment(
        _raw_payment(status="chargeback_pending", wht_status="under_appeal")
    )
    assert pay.status == "chargeback_pending"
    assert pay.wht_status == "under_appeal"
    cn = _client()._parse_credit_note(_raw_credit_note(status="pending_review"))
    assert cn.status == "pending_review"


# ---------------------------------------------------------------------------
# Negative zero is refused at BOTH ingress paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["-0.00", "-0", "-0.0000"])
def test_sub_parser_rejects_negative_zero_money(literal: str) -> None:
    with pytest.raises(DotmacSubParseError, match="negative zero"):
        _client()._parse_invoice(_raw_invoice(balance_due=literal))
    with pytest.raises(DotmacSubParseError, match="negative zero"):
        _client()._parse_payment(_raw_payment(wht_amount=literal))
    with pytest.raises(DotmacSubParseError, match="negative zero"):
        _client()._parse_credit_note(_raw_credit_note(applied_total=literal))


def test_sub_parser_accepts_positive_zero() -> None:
    inv = _client()._parse_invoice(_raw_invoice(balance_due="0.00"))
    assert inv.balance_due == Decimal("0.00")
    assert not inv.balance_due.is_signed()


@pytest.mark.parametrize("literal", ["-0.00", "-0", "-0.0000"])
def test_pydantic_ingress_rejects_negative_zero_money(literal: str) -> None:
    """The other ingress path: the Sub payables command schema."""
    with pytest.raises(ValidationError, match="negative zero"):
        SubPurchaseInvoicePayload(**_payables_payload(total=literal))
    bad_items = [
        {
            "description": "Fibre splice",
            "quantity": Decimal("2"),
            "unit_price": Decimal("500.00"),
            "amount": literal,
        }
    ]
    with pytest.raises(ValidationError, match="negative zero"):
        SubPurchaseInvoicePayload(**_payables_payload(items=bad_items))


def test_parse_invoice_rejects_missing_money_fact_instead_of_zero() -> None:
    payload = _raw_invoice()
    del payload["total"]
    with pytest.raises(DotmacSubParseError, match="required money fact 'total'"):
        _client()._parse_invoice(payload)


def test_parse_invoice_rejects_missing_currency_no_functional_default() -> None:
    payload = _raw_invoice()
    del payload["currency"]
    with pytest.raises(DotmacSubParseError, match="currency is required"):
        _client()._parse_invoice(payload)
    with pytest.raises(DotmacSubParseError, match="currency is required"):
        _client()._parse_invoice(_raw_invoice(currency="   "))


def test_parse_invoice_rejects_float_typed_amount() -> None:
    with pytest.raises(DotmacSubParseError, match="refusing float"):
        _client()._parse_invoice(_raw_invoice(subtotal=45000.005))


def test_parse_invoice_rejects_malformed_allocation_amount() -> None:
    bad = _raw_invoice(
        payment_allocations=[
            {"id": "a1", "payment_id": "p1", "invoice_id": "inv-raw-1", "amount": ""}
        ]
    )
    with pytest.raises(DotmacSubParseError, match="allocation a1"):
        _client()._parse_invoice(bad)


def test_parse_invoice_excess_precision_fails_at_parse_for_provisioned() -> None:
    # For a provisioned currency the parser applies the currency-aware
    # canonical grammar: "48375.005" (3 fractional digits for NGN) fails the
    # ROW at parse — it is never admitted, never rounded. (The boundary
    # adapter's own excess-precision rejection stays covered by the
    # boundary_money tests above, which drive it with Decimal facts.)
    with pytest.raises(DotmacSubParseError, match="fractional digits"):
        _client()._parse_invoice(_raw_invoice(total="48375.005"))


@pytest.mark.parametrize(
    "literal",
    ["1e3", " 100.00 ", "+100.00", "01.00", ".50", "100.", "48375", "48375.0"],
)
def test_parse_rejects_every_non_canonical_money_spelling(literal: str) -> None:
    # The ONE lexical wire representation (serialize_amount's fixed
    # minor-unit form) — every other spelling fails the row at parse, at all
    # three parsers.
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_invoice(_raw_invoice(total=literal))
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_payment(_raw_payment(amount=literal))
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_credit_note(_raw_credit_note(total=literal))


def test_parse_invoice_fake_currency_fails_at_the_boundary_layer() -> None:
    # An unprovisioned currency gets the currency-independent lexical
    # grammar at parse (canonical lexeme, unknown minor units) and is then
    # rejected outright by the registry at admission.
    inv = _client()._parse_invoice(_raw_invoice(currency="ZZZ"))
    with pytest.raises(MoneyBoundaryError, match="not provisioned"):
        inv.boundary_money()


def test_parse_invoice_missing_line_amount_stays_none_for_derivation() -> None:
    payload = _raw_invoice()
    del payload["lines"][0]["amount"]
    inv = _client()._parse_invoice(payload)
    assert inv.lines[0].amount is None  # ERP derives qty x unit_price itself


def test_parse_payment_happy_path_is_exact() -> None:
    pay = _client()._parse_payment(
        _raw_payment(
            gross_amount="107.50",
            net_amount="100.00",
            wht_amount="7.50",
            wht_rate="7.5",
        )
    )
    assert pay.amount == Decimal("107.50")
    assert pay.refunded_amount == Decimal("0")  # documented absent default
    money = pay.boundary_money()
    assert from_money(money.amount) == (Decimal("107.50"), "NGN")


def test_parse_payment_normalizes_legacy_zero_on_documented_default_fields() -> None:
    pay = _client()._parse_payment(_raw_payment(refunded_amount="0", wht_amount="0"))

    assert pay.refunded_amount == Decimal("0.00")
    assert pay.wht_amount == Decimal("0.00")
    assert pay.refunded_amount.as_tuple().exponent == -2
    assert pay.wht_amount.as_tuple().exponent == -2


def test_parse_payment_keeps_required_and_nonzero_money_strict() -> None:
    with pytest.raises(DotmacSubParseError, match="fractional digits"):
        _client()._parse_payment(_raw_payment(amount="0"))
    with pytest.raises(DotmacSubParseError, match="fractional digits"):
        _client()._parse_payment(_raw_payment(wht_amount="7"))


def test_parse_payment_rejects_missing_currency() -> None:
    payload = _raw_payment()
    del payload["currency"]
    with pytest.raises(DotmacSubParseError, match="currency is required"):
        _client()._parse_payment(payload)


def test_parse_payment_rejects_malformed_and_number_typed_amounts() -> None:
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_payment(_raw_payment(amount="12,50"))
    # Wire policy: strings only — float AND int number tokens are refused.
    with pytest.raises(DotmacSubParseError, match="refusing float"):
        _client()._parse_payment(_raw_payment(amount=107.5))
    with pytest.raises(DotmacSubParseError, match="refusing int"):
        _client()._parse_payment(_raw_payment(amount=107))
    with pytest.raises(DotmacSubParseError, match="refusing float"):
        _client()._parse_payment(_raw_payment(amount=float("inf")))
    # A documented-default field must still parse strictly when present.
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_payment(_raw_payment(refunded_amount="oops"))


def test_parse_rejects_non_finite_money_strings() -> None:
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(DotmacSubParseError, match="non-finite"):
            _client()._parse_payment(_raw_payment(amount=bad))
        with pytest.raises(DotmacSubParseError, match="non-finite"):
            _client()._parse_invoice(_raw_invoice(total=bad))


def test_parse_invoice_rejects_integer_money_tokens() -> None:
    # Strings-only wire rule applies to the Sub feeds too.
    with pytest.raises(DotmacSubParseError, match="refusing int"):
        _client()._parse_invoice(_raw_invoice(subtotal=45000))
    with pytest.raises(DotmacSubParseError, match="refusing int"):
        _client()._parse_credit_note(_raw_credit_note(total=1075))


def test_parse_payment_rejects_missing_amount() -> None:
    payload = _raw_payment()
    del payload["amount"]
    with pytest.raises(DotmacSubParseError, match="required money fact 'amount'"):
        _client()._parse_payment(payload)


def test_parse_credit_note_happy_path_and_rejections() -> None:
    cn = _client()._parse_credit_note(_raw_credit_note())
    assert cn.total == Decimal("1075.00")
    assert from_money(cn.boundary_money().total) == (Decimal("1075.00"), "NGN")

    payload = _raw_credit_note()
    del payload["currency"]
    with pytest.raises(DotmacSubParseError, match="currency is required"):
        _client()._parse_credit_note(payload)
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        _client()._parse_credit_note(_raw_credit_note(subtotal="NaN-ish"))
    with pytest.raises(DotmacSubParseError, match="refusing float"):
        _client()._parse_credit_note(_raw_credit_note(total=1075.5))


# ---------------------------------------------------------------------------
# A parse-rejected row fails THAT row: the feed continues, the run succeeds,
# and the watermark is held at the failed row for retry
# ---------------------------------------------------------------------------


def _feed_client(items: list[dict]) -> DotmacSubClient:
    client = _client()
    client._request = MagicMock(return_value={"items": items})  # type: ignore[method-assign]
    return client


def test_feed_generator_survives_a_bad_row_via_collector() -> None:
    client = _feed_client([_raw_invoice(total="oops"), _raw_invoice(id="inv-ok")])
    seen_errors: list[DotmacSubParseError] = []
    records = list(client.get_invoices(on_parse_error=seen_errors.append))
    assert [record.id for record in records] == ["inv-ok"]
    assert len(seen_errors) == 1
    assert seen_errors[0].record == "Sub invoice inv-raw-1"


_GOOD_ROW_AT = "2026-07-02T10:00:00+00:00"


def test_feed_generator_without_collector_raises_typed_error() -> None:
    client = _feed_client([_raw_invoice(total="oops")])
    with pytest.raises(DotmacSubParseError, match="malformed money fact"):
        list(client.get_invoices())


@pytest.mark.parametrize(
    ("feed", "bad", "good"),
    [
        pytest.param(
            "get_resellers",
            {"id": "r-bad", "name": "Bad", "updated_at": 1730000000},
            {"id": "r-ok", "name": "Ok", "updated_at": _GOOD_ROW_AT},
            id="reseller",
        ),
        pytest.param(
            "get_subscribers",
            {"id": "s-bad", "updated_at": "not-a-timestamp"},
            {"id": "s-ok", "updated_at": _GOOD_ROW_AT},
            id="subscriber",
        ),
    ],
)
def test_reseller_and_subscriber_feeds_survive_a_bad_row(
    feed: str, bad: dict, good: dict
) -> None:
    """Typing these records' timestamps must NOT let one bad row kill the feed.

    Raising out of a generator terminates it (PEP 255), so the reseller and
    subscriber pulls carry the same ``on_parse_error`` collector as the
    money-bearing feeds; the rejection is unpositioned, which freezes the
    cursor while every good row still syncs.
    """
    client = _feed_client([bad, good])
    seen: list[DotmacSubParseError] = []
    records = list(getattr(client, feed)(on_parse_error=seen.append))
    assert [record.id for record in records] == [good["id"]]
    assert len(seen) == 1
    assert seen[0].updated_at is None  # unpositioned -> cursor freeze


@dataclass(frozen=True)
class _EntityCase:
    """One sync path for the shared watermark canary matrix — all three
    mixins are proven by the SAME test matrix (they share the ONE
    WatermarkProgress tracker)."""

    mixin: type
    sync_method: str
    single_attr: str
    raw: Any  # Callable[..., dict]
    bad_money_field: str


_ENTITY_CASES = [
    pytest.param(
        _EntityCase(
            InvoiceSyncMixin,
            "sync_invoices",
            "_sync_single_invoice",
            _raw_invoice,
            "total",
        ),
        id="invoice",
    ),
    pytest.param(
        _EntityCase(
            PaymentSyncMixin,
            "sync_payments",
            "_sync_single_payment",
            _raw_payment,
            "amount",
        ),
        id="payment",
    ),
    pytest.param(
        _EntityCase(
            CreditNoteSyncMixin,
            "sync_credit_notes",
            "_sync_single_credit_note",
            _raw_credit_note,
            "total",
        ),
        id="credit-note",
    ),
]


def _loop_harness(case: _EntityCase, client: DotmacSubClient) -> Any:
    harness = case.mixin()
    harness.client = client
    harness.db = MagicMock()
    harness._get_sync_watermark = lambda entity_type: None
    harness._advance_sync_watermark = MagicMock()
    harness._get_sync_watermark_position = MagicMock(
        return_value=SyncWatermarkPosition(None, None)
    )
    harness._advance_sync_watermark_position = MagicMock()
    setattr(harness, case.single_attr, MagicMock())
    harness._reprime_tenant_context = MagicMock()
    harness._load_payment_channels = MagicMock()  # payments only; harmless
    return harness


@pytest.mark.parametrize("case", _ENTITY_CASES)
def test_positioned_parse_failure_parks_watermark_at_failed_row(
    case: _EntityCase,
) -> None:
    bad = case.raw(**{case.bad_money_field: "oops"})  # rejected at parse
    good = case.raw(id="row-ok", updated_at=_GOOD_ROW_AT)
    harness = _loop_harness(case, _feed_client([bad, good]))

    result = getattr(harness, case.sync_method)()

    assert result.success is True  # the RUN did not fail
    assert len(result.errors) == 1
    assert getattr(harness, case.single_attr).call_count == 1  # good row synced
    if case.sync_method == "sync_invoices":
        # Invoice compound cursors require updated_at + id. Parse errors do not
        # expose a structured id, so the cursor freezes rather than advancing
        # to a timestamp-only position that could skip a same-timestamp row.
        harness._advance_sync_watermark_position.assert_not_called()
    else:
        # Watermark parks at the failed row (min_error), not advanced past it.
        advanced_to = harness._advance_sync_watermark.call_args.args[1]
        assert advanced_to == datetime.fromisoformat(bad["updated_at"])


@pytest.mark.parametrize("case", _ENTITY_CASES)
@pytest.mark.parametrize(
    "position_kind", ["missing", "malformed", "integer", "savepoint"]
)
def test_unpositioned_failure_freezes_the_watermark(
    case: _EntityCase, position_kind: str
) -> None:
    """An UNPOSITIONED failure — missing, malformed, or NON-STRING (integer
    epoch) updated_at, at parse OR inside the savepoint — cannot park the
    cursor at itself, so the cursor FREEZES at the pre-run position. Later
    good rows still sync; the watermark must not advance at all."""
    if position_kind == "savepoint":
        bad = case.raw(id="row-nopos")  # clean parse, row fails in savepoint
        del bad["updated_at"]
    elif position_kind == "integer":
        # A non-string updated_at must be a typed parse failure routed
        # through the collector — never an AttributeError that kills the run.
        bad = case.raw(updated_at=1730000000)
    else:
        bad = case.raw(**{case.bad_money_field: "oops"})
        if position_kind == "missing":
            del bad["updated_at"]
        else:  # malformed
            bad["updated_at"] = "not-a-timestamp"
    good = case.raw(id="row-ok", updated_at=_GOOD_ROW_AT)
    harness = _loop_harness(case, _feed_client([bad, good]))
    if position_kind == "savepoint":
        setattr(
            harness,
            case.single_attr,
            MagicMock(side_effect=[ValueError("posting failed"), None]),
        )

    result = getattr(harness, case.sync_method)()

    assert result.success is True  # run continues
    assert len(result.errors) == 1
    expected_calls = 2 if position_kind == "savepoint" else 1
    assert getattr(harness, case.single_attr).call_count == expected_calls
    # Frozen: the watermark was NOT advanced at all this run.
    if case.sync_method == "sync_invoices":
        harness._advance_sync_watermark_position.assert_not_called()
    else:
        harness._advance_sync_watermark.assert_not_called()


# ---------------------------------------------------------------------------
# Supplied line amounts are validated, never rounded; only derived values round
# ---------------------------------------------------------------------------


class _LineAmountHarness(InvoiceSyncMixin):
    def __init__(self) -> None:
        self._get_source_tax_rate = MagicMock()
        self._resolve_source_sales_tax_code = MagicMock()


def _line(amount: Decimal | None, **overrides: object) -> InvoiceLineRecord:
    defaults: dict = {
        "id": "l1",
        "description": "Service",
        "quantity": Decimal("3"),
        "unit_price": Decimal("3.335"),
        "amount": amount,
        "tax_rate_id": None,
    }
    defaults.update(overrides)
    return InvoiceLineRecord(**defaults)


def test_supplied_line_amount_with_excess_precision_is_rejected_not_rounded() -> None:
    # The review canary: 10.005 NGN from the wire must fail the row, never
    # become 10.01.
    harness = _LineAmountHarness()
    with pytest.raises(MoneyBoundaryError, match="minor-unit precision"):
        harness._source_line_amounts(
            _line(Decimal("10.005")),
            currency_code="NGN",
            effective_date=date(2026, 7, 1),
        )


def test_supplied_line_amount_is_admitted_exactly() -> None:
    harness = _LineAmountHarness()
    subtotal, tax, tax_code = harness._source_line_amounts(
        _line(Decimal("10.01")),
        currency_code="NGN",
        effective_date=date(2026, 7, 1),
    )
    assert (subtotal, tax, tax_code) == (Decimal("10.01"), Decimal("0"), None)


def test_omitted_line_amount_derives_and_rounds_qty_times_price() -> None:
    # Only the ERP-DERIVED quantity x unit_price may round (3 x 3.335 =
    # 10.005 → 10.01 half-up) — that value is ERP's own computation, not a
    # transmitted source fact.
    harness = _LineAmountHarness()
    subtotal, tax, tax_code = harness._source_line_amounts(
        _line(None),
        currency_code="NGN",
        effective_date=date(2026, 7, 1),
    )
    assert (subtotal, tax, tax_code) == (Decimal("10.01"), Decimal("0"), None)
