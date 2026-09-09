"""Drop ap.purchase_order.amount_invoiced and amount_received.

Both were stored copies of facts that the authoritative receipt and invoice
records already carry, and both had broken authority on the row:

* `amount_received` had two writers with different arithmetic — the goods-receipt
  path recomputed it absolutely from the PO lines while a PO-service method
  incremented it by a caller-supplied delta.
* `amount_invoiced` had no writer at all.  It was permanently `0` for every row
  ever created, and the purchase-order detail screen rendered that zero as a
  financial fact while the CRM supersede interlock trusted it as a safety check.

They are now derived by their sole owner,
`app.services.finance.ap.purchase_order_amounts`:

    received = SUM(purchase_order_line.quantity_received * unit_price)
    invoiced = SUM(supplier_invoice_line.line_amount) for lines matched to this
               PO's lines, on invoices in a status that counts as invoiced

Dropping the columns is what makes the ownership rule structural rather than a
convention: two authorities cannot write different columns of one row if the
columns are not there.

## Data loss on downgrade

`downgrade()` re-creates both columns and BACKFILLS `amount_received` from the PO
lines, which reproduces exactly what the absolute writer would have computed.
`amount_invoiced` is restored as `0` — which is what every row held, since
nothing ever wrote it.  No information is lost in either direction because
neither column ever held anything the line-level records did not.

Revision ID: 20260902_drop_po_derived_amounts
Revises: 20260901_dotmac_sub_invoice_cursor_id
"""

from alembic import op
import sqlalchemy as sa

revision = "20260902_drop_po_derived_amounts"
down_revision = "20260901_dotmac_sub_invoice_cursor_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("purchase_order", "amount_invoiced", schema="ap")
    op.drop_column("purchase_order", "amount_received", schema="ap")


def downgrade() -> None:
    op.add_column(
        "purchase_order",
        sa.Column(
            "amount_invoiced",
            sa.Numeric(20, 6),
            nullable=False,
            server_default="0",
        ),
        schema="ap",
    )
    op.add_column(
        "purchase_order",
        sa.Column(
            "amount_received",
            sa.Numeric(20, 6),
            nullable=False,
            server_default="0",
        ),
        schema="ap",
    )
    # Backfill the received amount from the authoritative line quantities, so a
    # downgraded database matches what the old absolute writer would have left.
    op.execute(
        """
        UPDATE ap.purchase_order po
        SET amount_received = COALESCE(agg.total, 0)
        FROM (
            SELECT po_id, SUM(quantity_received * unit_price) AS total
            FROM ap.purchase_order_line
            GROUP BY po_id
        ) AS agg
        WHERE agg.po_id = po.po_id
        """
    )
