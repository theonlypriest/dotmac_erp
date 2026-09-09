"""Purchase-order received/invoiced amounts — derived, with ERP AP as sole owner.

`ap.purchase_order` used to carry two stored columns, `amount_received` and
`amount_invoiced`, and they were the textbook shape of split authority:

* `amount_received` had **two** writers that disagreed about arithmetic.
  `GoodsReceiptService._update_po_status` recomputed it ABSOLUTELY from the PO
  lines, while `PurchaseOrderService.update_received_amount` INCREMENTED it by a
  caller-supplied delta.  Run both over one PO and the column is whatever the
  last writer happened to be.
* `amount_invoiced` had **zero** writers.  Nothing in the application ever
  assigned it, so it was permanently `0` — and the purchase-order detail screen
  rendered that zero as a financial fact, and the CRM supersede guard trusted it
  as a safety interlock.
* `WorkflowActionExecutor._action_update_field` could `setattr` either of them to
  any value a workflow rule named, because `PURCHASE_ORDER` is in the automation
  entity registry.

Both facts are **derivable from the authoritative records that already exist**,
so per the accepted ruling they are derived here rather than stored anywhere:

* received  = SUM over `ap.purchase_order_line` of
              `quantity_received * unit_price`
* invoiced  = SUM over `ap.supplier_invoice_line.line_amount` for lines matched
              to one of the PO's lines, restricted to supplier invoices whose
              status counts as invoiced (see `COUNTS_AS_INVOICED`).

There is no projection table.  Nothing is stored, so nothing can go stale, and
the column-level authority rule is satisfied structurally rather than by
discipline: the columns do not exist on the `purchase_order` row, so ERP AP and
the procurement module cannot end up owning different columns of it.

**This module is the single definition.**  A caller that needs either number
calls into here — it never re-writes the SUM by hand, and it never writes the
number back onto a row.  `tests/architecture/test_po_amounts_single_owner.py`
fails the build if a second definition or a write path appears.

## Two shapes, because callers need two shapes

`received_expr()` / `invoiced_expr()` return correlated scalar subqueries for use
inside a SELECT — a list page's aggregate (`SUM(total_amount - received)`), an
ORDER BY, a WHERE.  `amounts_for()` / `amounts_for_many()` return `Decimal`s for
a loaded instance or a batch of them, and the batch form exists so a list page
does not issue one query per row.

Both go to the database.  Neither reads a cached attribute off the ORM instance,
because there is no longer an attribute to read.

## Tenant scope is a required argument, not an assumption

`ap.purchase_order_line` and `ap.supplier_invoice_line` carry no
`organization_id` — they inherit isolation from their parents.  A helper that
took only a `po_id` would therefore read across tenants if a caller ever handed
it an id it had not already scoped, and "the caller checked" is not a boundary.
So the instance-form functions REQUIRE `organization_id` and constrain through
`ap.purchase_order`; the invoiced query additionally constrains
`ap.supplier_invoice.organization_id`, so an invoice belonging to another tenant
contributes nothing even if a line somehow points at this PO.

A PO that does not exist, or belongs to another tenant, derives as zero rather
than raising: these are read helpers behind guards that have already loaded the
PO under tenant scope, and a zero cannot leak anything.

The `*_expr()` forms take no `organization_id` because they correlate on
`PurchaseOrder` and inherit whatever the enclosing query filters on — which must
therefore include the tenant predicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models.finance.ap.purchase_order import PurchaseOrder
from app.models.finance.ap.purchase_order_line import PurchaseOrderLine
from app.models.finance.ap.supplier_invoice import (
    SupplierInvoice,
    SupplierInvoiceStatus,
)
from app.models.finance.ap.supplier_invoice_line import SupplierInvoiceLine

ZERO = Decimal("0")

# A supplier invoice in one of these statuses is a real claim against the PO, so
# its matched lines count toward `amount_invoiced`.
#
# `PAID` counts: money already went out against this PO.  `ON_HOLD` and
# `DISPUTED` count: the claim exists and the goods are spoken for even though
# settlement is stalled — a supersede guard that ignored a disputed invoice
# would orphan it, which is the exact accident the guard is there to prevent.
COUNTS_AS_INVOICED: frozenset[SupplierInvoiceStatus] = frozenset(
    {
        SupplierInvoiceStatus.SUBMITTED,
        SupplierInvoiceStatus.PENDING_APPROVAL,
        SupplierInvoiceStatus.APPROVED,
        SupplierInvoiceStatus.POSTED,
        SupplierInvoiceStatus.PARTIALLY_PAID,
        SupplierInvoiceStatus.PAID,
        SupplierInvoiceStatus.ON_HOLD,
        SupplierInvoiceStatus.DISPUTED,
    }
)

# The complement, stated positively rather than left implicit.  A `DRAFT`
# invoice is not yet a claim; `REJECTED` and `VOID` never became one.
#
# The two sets must together cover `SupplierInvoiceStatus` exactly.  That is
# asserted in the architecture test, so adding a status to the enum without
# classifying it fails the build instead of silently defaulting one way.
NOT_YET_INVOICED: frozenset[SupplierInvoiceStatus] = frozenset(
    {
        SupplierInvoiceStatus.DRAFT,
        SupplierInvoiceStatus.REJECTED,
        SupplierInvoiceStatus.VOID,
    }
)


@dataclass(frozen=True)
class PurchaseOrderAmounts:
    """The two derived facts for one purchase order."""

    po_id: UUID
    amount_received: Decimal
    amount_invoiced: Decimal


def received_expr() -> ColumnElement[Decimal]:
    """Correlated scalar subquery for a PO's received amount.

    Use inside a SELECT over `PurchaseOrder` — an aggregate, a WHERE, an
    ORDER BY.  It correlates on `PurchaseOrder.po_id`, so the enclosing query
    must have `PurchaseOrder` in scope.
    """
    return (
        select(
            func.coalesce(
                func.sum(
                    PurchaseOrderLine.quantity_received * PurchaseOrderLine.unit_price
                ),
                0,
            )
        )
        .where(PurchaseOrderLine.po_id == PurchaseOrder.po_id)
        .correlate(PurchaseOrder)
        .scalar_subquery()
    )


def invoiced_expr() -> ColumnElement[Decimal]:
    """Correlated scalar subquery for a PO's invoiced amount.

    Same correlation contract as `received_expr`.
    """
    return (
        select(func.coalesce(func.sum(SupplierInvoiceLine.line_amount), 0))
        .select_from(SupplierInvoiceLine)
        .join(
            SupplierInvoice,
            SupplierInvoice.invoice_id == SupplierInvoiceLine.invoice_id,
        )
        .join(
            PurchaseOrderLine,
            PurchaseOrderLine.line_id == SupplierInvoiceLine.po_line_id,
        )
        .where(
            PurchaseOrderLine.po_id == PurchaseOrder.po_id,
            SupplierInvoice.status.in_(
                sorted(COUNTS_AS_INVOICED, key=lambda s: s.value)
            ),
        )
        .correlate(PurchaseOrder)
        .scalar_subquery()
    )


def _received_for_ids(
    db: Session, organization_id: UUID, po_ids: list[UUID]
) -> dict[UUID, Decimal]:
    rows = db.execute(
        select(
            PurchaseOrderLine.po_id,
            func.coalesce(
                func.sum(
                    PurchaseOrderLine.quantity_received * PurchaseOrderLine.unit_price
                ),
                0,
            ),
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == PurchaseOrderLine.po_id)
        .where(
            PurchaseOrderLine.po_id.in_(po_ids),
            PurchaseOrder.organization_id == organization_id,
        )
        .group_by(PurchaseOrderLine.po_id)
    ).all()
    return {po_id: Decimal(str(total)) for po_id, total in rows}


def _invoiced_for_ids(
    db: Session, organization_id: UUID, po_ids: list[UUID]
) -> dict[UUID, Decimal]:
    rows = db.execute(
        select(
            PurchaseOrderLine.po_id,
            func.coalesce(func.sum(SupplierInvoiceLine.line_amount), 0),
        )
        .select_from(SupplierInvoiceLine)
        .join(
            SupplierInvoice,
            SupplierInvoice.invoice_id == SupplierInvoiceLine.invoice_id,
        )
        .join(
            PurchaseOrderLine,
            PurchaseOrderLine.line_id == SupplierInvoiceLine.po_line_id,
        )
        .join(PurchaseOrder, PurchaseOrder.po_id == PurchaseOrderLine.po_id)
        .where(
            PurchaseOrderLine.po_id.in_(po_ids),
            # Both parents are constrained, not just the PO: an invoice from
            # another tenant must not contribute even if a line points here.
            PurchaseOrder.organization_id == organization_id,
            SupplierInvoice.organization_id == organization_id,
            SupplierInvoice.status.in_(
                sorted(COUNTS_AS_INVOICED, key=lambda s: s.value)
            ),
        )
        .group_by(PurchaseOrderLine.po_id)
    ).all()
    return {po_id: Decimal(str(total)) for po_id, total in rows}


def amounts_for_many(
    db: Session, organization_id: UUID, po_ids: list[UUID]
) -> dict[UUID, PurchaseOrderAmounts]:
    """Derive both amounts for a batch of POs in two queries, not two per PO.

    Every requested id appears in the result, zero-valued when the PO has no
    lines, no receipts and no matched invoice lines — and also when the id names
    a PO that does not exist or belongs to another tenant.  A caller therefore
    never has to distinguish "no rows" from "zero".
    """
    unique_ids = list(dict.fromkeys(po_ids))
    if not unique_ids:
        return {}

    received = _received_for_ids(db, organization_id, unique_ids)
    invoiced = _invoiced_for_ids(db, organization_id, unique_ids)

    return {
        po_id: PurchaseOrderAmounts(
            po_id=po_id,
            amount_received=received.get(po_id, ZERO),
            amount_invoiced=invoiced.get(po_id, ZERO),
        )
        for po_id in unique_ids
    }


def amounts_for(
    db: Session, organization_id: UUID, po_id: UUID
) -> PurchaseOrderAmounts:
    """Derive both amounts for one PO."""
    return amounts_for_many(db, organization_id, [po_id])[po_id]


def received_for(db: Session, organization_id: UUID, po_id: UUID) -> Decimal:
    """Derive just the received amount for one PO."""
    return amounts_for(db, organization_id, po_id).amount_received


def invoiced_for(db: Session, organization_id: UUID, po_id: UUID) -> Decimal:
    """Derive just the invoiced amount for one PO."""
    return amounts_for(db, organization_id, po_id).amount_invoiced


def has_financial_activity(db: Session, organization_id: UUID, po_id: UUID) -> bool:
    """True when a PO has been received against or invoiced against.

    This is the interlock the CRM supersede path and `cancel_po` need: a PO with
    financial activity cannot be superseded or cancelled without orphaning the
    receipt and invoice records that point at it.

    It replaces two guards that could not fire.  The old header check read
    `amount_invoiced`, which nothing ever wrote; the old line check read
    `purchase_order_line.quantity_invoiced`, which nothing ever wrote either.
    Both halves of the "invoiced" interlock were dead.  This one reads the
    supplier invoice lines themselves.
    """
    amounts = amounts_for(db, organization_id, po_id)
    return amounts.amount_received > ZERO or amounts.amount_invoiced > ZERO
