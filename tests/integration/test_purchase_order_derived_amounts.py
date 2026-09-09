"""PostgreSQL proof that PO received/invoiced amounts derive from the real records.

`ap.purchase_order.amount_received` and `amount_invoiced` used to be stored
columns.  `amount_received` had two writers with different arithmetic;
`amount_invoiced` had none at all and was permanently zero while the UI rendered
it as a financial fact and the supersede interlock trusted it.

They are now derived by `app.services.finance.ap.purchase_order_amounts`.  These
tests run against real PostgreSQL because the derivation is SQL — correlated
scalar subqueries and a three-table join — and a mock session would prove only
that the Python around it is arranged tidily.

The load-bearing assertions:

* the invoiced amount is **non-zero** for a PO with a matched supplier invoice.
  The old column could never be non-zero, so this is the one that would have
  failed before and is the reason the interlock was dead.
* a `DRAFT` invoice does **not** count, and a `VOID` one stops counting.  The
  status vocabulary is what the guard depends on, so it is exercised rather than
  assumed.
* an unmatched invoice line (`po_line_id IS NULL`) contributes nothing — the
  join is on the match, not on the supplier.
* the batch form returns the same numbers as the single form and zero-fills a PO
  with no activity, so a list page cannot silently drop a row.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _make_po(db: Session, org_id: uuid.UUID, supplier, user_id: uuid.UUID, number: str):
    from app.models.finance.ap.purchase_order import POStatus, PurchaseOrder

    po = PurchaseOrder(
        organization_id=org_id,
        supplier_id=supplier.supplier_id,
        po_number=number,
        po_date=date(2024, 1, 10),
        currency_code="USD",
        exchange_rate=Decimal("1.0"),
        subtotal=Decimal("1000.00"),
        tax_amount=Decimal("0"),
        total_amount=Decimal("1000.00"),
        status=POStatus.APPROVED,
        created_by_user_id=user_id,
    )
    db.add(po)
    db.flush()
    return po


def _make_po_line(
    db: Session,
    po,
    line_number: int,
    ordered: str,
    received: str,
    unit_price: str,
):
    from app.models.finance.ap.purchase_order_line import PurchaseOrderLine

    line = PurchaseOrderLine(
        po_id=po.po_id,
        line_number=line_number,
        description=f"Line {line_number}",
        quantity_ordered=Decimal(ordered),
        quantity_received=Decimal(received),
        unit_price=Decimal(unit_price),
        line_amount=Decimal(ordered) * Decimal(unit_price),
    )
    db.add(line)
    db.flush()
    return line


def _make_invoice_line(db: Session, invoice, po_line, line_number: int, amount: str):
    from app.models.finance.ap.supplier_invoice_line import SupplierInvoiceLine

    line = SupplierInvoiceLine(
        invoice_id=invoice.invoice_id,
        line_number=line_number,
        po_line_id=po_line.line_id if po_line is not None else None,
        description=f"Invoice line {line_number}",
        quantity=Decimal("1"),
        unit_price=Decimal(amount),
        line_amount=Decimal(amount),
    )
    db.add(line)
    db.flush()
    return line


def test_the_migration_really_dropped_the_columns(db: Session) -> None:
    """The database, not just the model, is rid of them.

    The integration lane builds its schema with `alembic upgrade heads`, so this
    reads the outcome of the real migration rather than what the ORM believes.
    A model-only check would still pass if the migration were forgotten, leaving
    a live column with no owner and a stale value nobody updates.
    """
    present = (
        db.execute(
            text(
                """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'ap'
              AND table_name = 'purchase_order'
              AND column_name IN ('amount_received', 'amount_invoiced')
            ORDER BY column_name
            """
            )
        )
        .scalars()
        .all()
    )

    assert present == [], (
        f"ap.purchase_order still has {present}. These are derived facts owned "
        "by app.services.finance.ap.purchase_order_amounts; a stored column on "
        "this row splits authority over the row between ERP AP and whoever "
        "writes the commitment columns."
    )


def test_received_is_derived_from_the_line_quantities(
    db: Session, org_id: uuid.UUID, supplier, user_id: uuid.UUID
) -> None:
    from app.services.finance.ap.purchase_order_amounts import amounts_for

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-1")
    _make_po_line(db, po, 1, ordered="10", received="4", unit_price="100.00")
    _make_po_line(db, po, 2, ordered="5", received="5", unit_price="20.00")

    amounts = amounts_for(db, org_id, po.po_id)

    # 4 * 100 + 5 * 20
    assert amounts.amount_received == Decimal("500.000000")
    assert amounts.amount_invoiced == Decimal("0")


def test_invoiced_is_non_zero_once_an_invoice_line_matches_a_po_line(
    db: Session, org_id: uuid.UUID, supplier, supplier_invoice, user_id: uuid.UUID
) -> None:
    """The assertion the old stored column could never satisfy.

    `amount_invoiced` had no writer, so it was `0` for every PO that ever
    existed. A non-zero result here is the whole point of the cutover.
    """
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import amounts_for

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-2")
    line = _make_po_line(db, po, 1, ordered="10", received="0", unit_price="100.00")

    supplier_invoice.status = SupplierInvoiceStatus.POSTED
    _make_invoice_line(db, supplier_invoice, line, 1, "300.00")
    db.flush()

    amounts = amounts_for(db, org_id, po.po_id)
    assert amounts.amount_invoiced == Decimal("300.000000")
    assert amounts.amount_received == Decimal("0")


def test_a_draft_invoice_is_not_yet_a_claim_against_the_po(
    db: Session, org_id: uuid.UUID, supplier, supplier_invoice, user_id: uuid.UUID
) -> None:
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import amounts_for

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-3")
    line = _make_po_line(db, po, 1, ordered="10", received="0", unit_price="100.00")

    supplier_invoice.status = SupplierInvoiceStatus.DRAFT
    _make_invoice_line(db, supplier_invoice, line, 1, "300.00")
    db.flush()

    assert amounts_for(db, org_id, po.po_id).amount_invoiced == Decimal("0")

    # Approve it and the same line now counts.
    supplier_invoice.status = SupplierInvoiceStatus.APPROVED
    db.flush()
    assert amounts_for(db, org_id, po.po_id).amount_invoiced == Decimal("300.000000")

    # Void it and it stops counting, without anything having to rewrite a column.
    supplier_invoice.status = SupplierInvoiceStatus.VOID
    db.flush()
    assert amounts_for(db, org_id, po.po_id).amount_invoiced == Decimal("0")


def test_an_unmatched_invoice_line_does_not_count_against_the_po(
    db: Session, org_id: uuid.UUID, supplier, supplier_invoice, user_id: uuid.UUID
) -> None:
    """The join is on `po_line_id`, not on the supplier."""
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import amounts_for

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-4")
    _make_po_line(db, po, 1, ordered="10", received="0", unit_price="100.00")

    supplier_invoice.status = SupplierInvoiceStatus.POSTED
    _make_invoice_line(db, supplier_invoice, None, 1, "900.00")
    db.flush()

    assert amounts_for(db, org_id, po.po_id).amount_invoiced == Decimal("0")


def test_the_batch_form_matches_the_single_form_and_zero_fills(
    db: Session, org_id: uuid.UUID, supplier, user_id: uuid.UUID
) -> None:
    from app.services.finance.ap.purchase_order_amounts import (
        amounts_for,
        amounts_for_many,
    )

    active = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-5")
    _make_po_line(db, active, 1, ordered="3", received="2", unit_price="50.00")
    empty = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-6")

    batch = amounts_for_many(db, org_id, [active.po_id, empty.po_id, active.po_id])

    # Every requested id is present, including the one with no lines at all —
    # a list page must never have to tell "no rows" apart from "zero".
    assert set(batch) == {active.po_id, empty.po_id}
    assert batch[empty.po_id].amount_received == Decimal("0")
    assert batch[empty.po_id].amount_invoiced == Decimal("0")
    assert batch[active.po_id] == amounts_for(db, org_id, active.po_id)


def test_the_correlated_expressions_agree_with_the_batch_form(
    db: Session, org_id: uuid.UUID, supplier, supplier_invoice, user_id: uuid.UUID
) -> None:
    """`received_expr()`/`invoiced_expr()` are the SQL twins of `amounts_for`.

    Two shapes of one definition is exactly the drift risk this module exists to
    remove, so they are compared against each other rather than trusted.
    """
    from app.models.finance.ap.purchase_order import PurchaseOrder
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import (
        amounts_for,
        invoiced_expr,
        received_expr,
    )

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-7")
    line = _make_po_line(db, po, 1, ordered="8", received="3", unit_price="25.00")
    supplier_invoice.status = SupplierInvoiceStatus.PARTIALLY_PAID
    _make_invoice_line(db, supplier_invoice, line, 1, "60.00")
    db.flush()

    received, invoiced = db.execute(
        select(received_expr(), invoiced_expr()).where(PurchaseOrder.po_id == po.po_id)
    ).one()

    expected = amounts_for(db, org_id, po.po_id)
    assert Decimal(str(received)) == expected.amount_received
    assert Decimal(str(invoiced)) == expected.amount_invoiced
    assert expected.amount_received == Decimal("75.000000")
    assert expected.amount_invoiced == Decimal("60.000000")


def test_the_supersede_and_cancel_interlock_now_fires_on_an_invoice_alone(
    db: Session, org_id: uuid.UUID, supplier, supplier_invoice, user_id: uuid.UUID
) -> None:
    """A PO with an invoice but no receipt must read as having activity.

    The old interlock read `amount_invoiced` (never written) and
    `purchase_order_line.quantity_invoiced` (never written either). BOTH halves
    of the invoiced check were dead, so this exact situation — invoiced, not yet
    received — was treated as a clean PO and could be superseded or cancelled,
    orphaning the invoice.
    """
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import has_financial_activity

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-8")
    line = _make_po_line(db, po, 1, ordered="10", received="0", unit_price="100.00")

    assert has_financial_activity(db, org_id, po.po_id) is False

    supplier_invoice.status = SupplierInvoiceStatus.POSTED
    _make_invoice_line(db, supplier_invoice, line, 1, "1000.00")
    db.flush()

    assert has_financial_activity(db, org_id, po.po_id) is True


def test_another_tenants_purchase_order_derives_as_zero(
    db: Session, org_id: uuid.UUID, supplier, user_id: uuid.UUID
) -> None:
    """The derivation is scoped by argument, not by the caller's good manners.

    `ap.purchase_order_line` and `ap.supplier_invoice_line` carry no
    `organization_id` — they inherit isolation from their parents. A helper that
    took only a `po_id` would read straight across tenants the first time a
    caller handed it an id it had not already scoped. So the scope is a required
    argument and the query joins through `ap.purchase_order` to apply it, and
    this is the test that says so.
    """
    from app.services.finance.ap.purchase_order_amounts import (
        amounts_for,
        has_financial_activity,
    )

    po = _make_po(db, org_id, supplier, user_id, "PO-DERIVE-9")
    _make_po_line(db, po, 1, ordered="10", received="10", unit_price="100.00")

    assert amounts_for(db, org_id, po.po_id).amount_received == Decimal("1000.000000")

    other_org = uuid.uuid4()
    assert amounts_for(db, other_org, po.po_id).amount_received == Decimal("0")
    assert amounts_for(db, other_org, po.po_id).amount_invoiced == Decimal("0")
    assert has_financial_activity(db, other_org, po.po_id) is False
