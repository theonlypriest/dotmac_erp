"""
GoodsReceiptService - Goods receipt lifecycle management.

Manages goods receipt creation, inspection, and acceptance/rejection.
"""

from __future__ import annotations

import builtins
import logging
from dataclasses import dataclass, field
from datetime import date, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.finance.ap.goods_receipt import GoodsReceipt, ReceiptStatus
from app.models.finance.ap.goods_receipt_line import GoodsReceiptLine
from app.models.finance.ap.purchase_order import POStatus, PurchaseOrder
from app.models.finance.ap.purchase_order_line import PurchaseOrderLine
from app.models.finance.core_config.numbering_sequence import SequenceType
from app.models.inventory.inventory_transaction import TransactionType
from app.models.inventory.item import Item
from app.services.common import coerce_uuid
from app.services.finance.ap.input_utils import (
    parse_date_str,
    parse_decimal,
    parse_json_list,
    require_uuid,
)
from app.services.finance.gl.period_guard import PeriodGuardService
from app.services.finance.platform.sequence import SequenceService
from app.services.inventory.transaction import (
    InventoryTransactionService,
    TransactionInput,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


@dataclass
class GRLineInput:
    """Input for a goods receipt line."""

    po_line_id: UUID
    quantity_received: Decimal
    location_id: UUID | None = None
    lot_number: str | None = None
    serial_numbers: list[str] | None = None


@dataclass
class GoodsReceiptInput:
    """Input for creating a goods receipt."""

    po_id: UUID
    receipt_date: date
    lines: list[GRLineInput] = field(default_factory=list)
    warehouse_id: UUID | None = None
    notes: str | None = None


@dataclass
class InspectionResult:
    """Result of inspecting a goods receipt line."""

    line_id: UUID
    quantity_accepted: Decimal
    quantity_rejected: Decimal = Decimal("0")
    rejection_reason: str | None = None


class GoodsReceiptService(ListResponseMixin):
    """
    Service for goods receipt lifecycle management.
    """

    @staticmethod
    def build_receipt_input(
        po_id: UUID,
        receipt_date: date,
        lines_raw: list[dict[str, Any]],
        notes: str | None = None,
    ) -> GoodsReceiptInput:
        """Build GoodsReceiptInput from raw API params.

        Raises:
            ValueError: If any line is missing po_line_id.
        """
        lines: list[GRLineInput] = []
        for line in lines_raw:
            if not line.get("po_line_id"):
                raise ValueError("po_line_id required for each line")
            lines.append(
                GRLineInput(
                    po_line_id=line["po_line_id"],
                    quantity_received=line["quantity_received"],
                    location_id=line.get("warehouse_id"),
                )
            )
        return GoodsReceiptInput(
            po_id=po_id,
            receipt_date=receipt_date,
            notes=notes,
            lines=lines,
        )

    @staticmethod
    def build_input_from_payload(
        db: Session,
        organization_id: UUID,
        payload: dict,
    ) -> GoodsReceiptInput:
        """Build GoodsReceiptInput from raw payload (strings or JSON)."""
        _ = db
        _ = organization_id

        receipt_date = parse_date_str(payload.get("receipt_date"), "Receipt date", True)
        if receipt_date is None:
            raise ValueError("Receipt date is required")

        lines_data = parse_json_list(payload.get("lines"), "Lines")
        lines: list[GRLineInput] = []
        for line in lines_data:
            po_line_id = require_uuid(line.get("po_line_id"), "PO line")
            quantity_received = parse_decimal(
                line.get("quantity_received", line.get("quantity", 0)),
                "Quantity received",
            )
            serial_numbers = line.get("serial_numbers")
            if isinstance(serial_numbers, str):
                serial_numbers = [
                    s.strip() for s in serial_numbers.split(",") if s.strip()
                ]
            if serial_numbers is not None and not isinstance(serial_numbers, list):
                serial_numbers = None

            lines.append(
                GRLineInput(
                    po_line_id=po_line_id,
                    quantity_received=quantity_received,
                    location_id=coerce_uuid(line.get("location_id"))
                    if line.get("location_id")
                    else None,
                    lot_number=line.get("lot_number"),
                    serial_numbers=serial_numbers,
                )
            )

        po_id = require_uuid(payload.get("po_id"), "Purchase order")
        return GoodsReceiptInput(
            po_id=po_id,
            receipt_date=receipt_date,
            lines=lines,
            warehouse_id=coerce_uuid(payload.get("warehouse_id"))
            if payload.get("warehouse_id")
            else None,
            notes=payload.get("notes"),
        )

    @staticmethod
    def create_receipt(
        db: Session,
        organization_id: UUID,
        input: GoodsReceiptInput,
        received_by_user_id: UUID,
    ) -> GoodsReceipt:
        """
        Create a new goods receipt against a purchase order.

        Args:
            db: Database session
            organization_id: Organization scope
            input: Receipt input data
            received_by_user_id: User receiving the goods

        Returns:
            Created GoodsReceipt

        Raises:
            HTTPException(400): If validation fails
            HTTPException(404): If PO not found
        """
        org_id = coerce_uuid(organization_id)
        po_id = coerce_uuid(input.po_id)

        # Validate PO exists and is in receivable status
        po = db.scalars(
            select(PurchaseOrder).where(
                PurchaseOrder.po_id == po_id,
                PurchaseOrder.organization_id == org_id,
            )
        ).first()

        if not po:
            raise HTTPException(status_code=404, detail="Purchase order not found")

        if po.status not in [POStatus.APPROVED, POStatus.PARTIALLY_RECEIVED]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot receive goods for PO in {po.status.value} status",
            )

        # Validate lines
        if not input.lines:
            raise HTTPException(
                status_code=400, detail="Goods receipt must have at least one line"
            )

        # Validate all PO lines exist and belong to this PO
        po_line_ids = {line.line_id for line in po.lines}
        for line_input in input.lines:
            if coerce_uuid(line_input.po_line_id) not in po_line_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"PO line {line_input.po_line_id} not found in this purchase order",
                )

        # Generate receipt number
        receipt_number = SequenceService.get_next_number(
            db, org_id, SequenceType.GOODS_RECEIPT
        )

        # Create receipt
        receipt = GoodsReceipt(
            organization_id=org_id,
            supplier_id=po.supplier_id,
            po_id=po_id,
            receipt_number=receipt_number,
            receipt_date=input.receipt_date,
            status=ReceiptStatus.RECEIVED,
            received_by_user_id=received_by_user_id,
            warehouse_id=input.warehouse_id,
            notes=input.notes,
        )
        db.add(receipt)
        db.flush()

        # Create lines and update PO line quantities
        for idx, line_input in enumerate(input.lines, start=1):
            po_line_id = coerce_uuid(line_input.po_line_id)

            # Get the PO line
            po_line = db.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.line_id == po_line_id)
            ).first()
            if not po_line:
                raise HTTPException(
                    status_code=404,
                    detail=f"Purchase order line {po_line_id} not found",
                )

            # Reject non-positive received quantities. A negative quantity
            # would otherwise pass the over-receipt check below and silently
            # DECREMENT the PO line's received quantity.
            if line_input.quantity_received <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Quantity received must be greater than zero for line {po_line.line_number}",
                )

            # Validate quantity doesn't exceed remaining
            remaining_qty = po_line.quantity_ordered - po_line.quantity_received
            if line_input.quantity_received > remaining_qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Quantity received ({line_input.quantity_received}) exceeds remaining quantity ({remaining_qty}) for line {po_line.line_number}",
                )

            line = GoodsReceiptLine(
                receipt_id=receipt.receipt_id,
                po_line_id=po_line_id,
                line_number=idx,
                quantity_received=line_input.quantity_received,
                quantity_accepted=Decimal("0"),
                quantity_rejected=Decimal("0"),
                location_id=line_input.location_id,
                lot_number=line_input.lot_number,
                serial_numbers=line_input.serial_numbers,
            )
            db.add(line)

            # Update PO line quantity received
            po_line.quantity_received += line_input.quantity_received

        # Update PO received amount and status
        GoodsReceiptService._update_po_status(db, po)

        db.flush()

        return receipt

    @staticmethod
    def start_inspection(
        db: Session,
        organization_id: UUID,
        receipt_id: UUID,
    ) -> GoodsReceipt:
        """
        Move receipt to inspection status.

        Args:
            db: Database session
            organization_id: Organization scope
            receipt_id: Goods receipt ID

        Returns:
            Updated GoodsReceipt
        """
        org_id = coerce_uuid(organization_id)
        receipt_id = coerce_uuid(receipt_id)

        receipt = db.scalars(
            select(GoodsReceipt).where(
                GoodsReceipt.receipt_id == receipt_id,
                GoodsReceipt.organization_id == org_id,
            )
        ).first()

        if not receipt:
            raise HTTPException(status_code=404, detail="Goods receipt not found")

        if receipt.status != ReceiptStatus.RECEIVED:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start inspection for receipt in {receipt.status.value} status",
            )

        receipt.status = ReceiptStatus.INSPECTING
        db.flush()

        return receipt

    @staticmethod
    def complete_inspection(
        db: Session,
        organization_id: UUID,
        receipt_id: UUID,
        inspection_results: list[InspectionResult],
    ) -> GoodsReceipt:
        """
        Complete inspection with acceptance/rejection quantities.

        Args:
            db: Database session
            organization_id: Organization scope
            receipt_id: Goods receipt ID
            inspection_results: List of inspection results per line

        Returns:
            Updated GoodsReceipt
        """
        org_id = coerce_uuid(organization_id)
        receipt_id = coerce_uuid(receipt_id)

        receipt = db.scalars(
            select(GoodsReceipt).where(
                GoodsReceipt.receipt_id == receipt_id,
                GoodsReceipt.organization_id == org_id,
            )
        ).first()

        if not receipt:
            raise HTTPException(status_code=404, detail="Goods receipt not found")

        if receipt.status not in [ReceiptStatus.RECEIVED, ReceiptStatus.INSPECTING]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot complete inspection for receipt in {receipt.status.value} status",
            )

        # Process inspection results
        all_accepted = True
        all_rejected = True
        total_received = Decimal("0")
        total_accepted = Decimal("0")
        total_rejected = Decimal("0")

        for result in inspection_results:
            line = db.scalars(
                select(GoodsReceiptLine).where(
                    GoodsReceiptLine.line_id == coerce_uuid(result.line_id),
                    GoodsReceiptLine.receipt_id == receipt_id,
                )
            ).first()

            if not line:
                raise HTTPException(
                    status_code=400, detail=f"Receipt line {result.line_id} not found"
                )

            # Validate quantities
            if (
                result.quantity_accepted + result.quantity_rejected
                != line.quantity_received
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Accepted + rejected quantities must equal received quantity for line {line.line_number}",
                )

            line.quantity_accepted = result.quantity_accepted
            line.quantity_rejected = result.quantity_rejected
            line.rejection_reason = result.rejection_reason

            total_received += line.quantity_received
            total_accepted += result.quantity_accepted
            total_rejected += result.quantity_rejected

            if result.quantity_rejected > 0:
                all_accepted = False
            if result.quantity_accepted > 0:
                all_rejected = False

        # Determine final status
        if all_accepted:
            receipt.status = ReceiptStatus.ACCEPTED
        elif all_rejected:
            receipt.status = ReceiptStatus.REJECTED
        else:
            receipt.status = ReceiptStatus.PARTIAL

        # If rejected, reverse PO line quantities
        if receipt.status == ReceiptStatus.REJECTED:
            GoodsReceiptService._reverse_po_quantities(db, receipt)
        elif receipt.status == ReceiptStatus.PARTIAL:
            GoodsReceiptService._reverse_rejected_quantities(db, receipt)

        # Create inventory transactions for accepted lines (if any)
        if receipt.status in [ReceiptStatus.ACCEPTED, ReceiptStatus.PARTIAL]:
            if receipt.received_by_user_id is None:
                raise HTTPException(
                    status_code=400, detail="Receipt has no received_by_user_id"
                )
            GoodsReceiptService._create_inventory_transactions_for_receipt(
                db=db,
                organization_id=org_id,
                receipt=receipt,
                user_id=receipt.received_by_user_id,
            )

        db.flush()

        return receipt

    @staticmethod
    def accept_all(
        db: Session,
        organization_id: UUID,
        receipt_id: UUID,
    ) -> GoodsReceipt:
        """
        Accept all items on a goods receipt (skip inspection).

        Args:
            db: Database session
            organization_id: Organization scope
            receipt_id: Goods receipt ID

        Returns:
            Updated GoodsReceipt
        """
        org_id = coerce_uuid(organization_id)
        receipt_id = coerce_uuid(receipt_id)

        receipt = db.scalars(
            select(GoodsReceipt).where(
                GoodsReceipt.receipt_id == receipt_id,
                GoodsReceipt.organization_id == org_id,
            )
        ).first()

        if not receipt:
            raise HTTPException(status_code=404, detail="Goods receipt not found")

        if receipt.status not in [ReceiptStatus.RECEIVED, ReceiptStatus.INSPECTING]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot accept receipt in {receipt.status.value} status",
            )

        # Accept all lines
        for line in receipt.lines:
            line.quantity_accepted = line.quantity_received
            line.quantity_rejected = Decimal("0")

        receipt.status = ReceiptStatus.ACCEPTED

        # Create inventory transactions for accepted lines
        if receipt.received_by_user_id is None:
            raise HTTPException(
                status_code=400, detail="Receipt has no received_by_user_id"
            )
        GoodsReceiptService._create_inventory_transactions_for_receipt(
            db=db,
            organization_id=org_id,
            receipt=receipt,
            user_id=receipt.received_by_user_id,
        )

        db.flush()

        return receipt

    @staticmethod
    def _create_inventory_transactions_for_receipt(
        db: Session,
        organization_id: UUID,
        receipt: GoodsReceipt,
        user_id: UUID,
    ) -> list[UUID]:
        """
        Create inventory transactions for accepted goods receipt lines.

        For each line with an inventory item, creates a RECEIPT transaction
        that increases inventory at the specified warehouse.

        Args:
            db: Database session
            organization_id: Organization scope
            receipt: The goods receipt being accepted
            user_id: User performing the acceptance

        Returns:
            List of created inventory transaction IDs
        """
        transaction_ids: list[UUID] = []

        # Get fiscal period for the receipt date
        fiscal_period = PeriodGuardService.get_period_for_date(
            db,
            organization_id,
            receipt.receipt_date,
        )

        if not fiscal_period:
            # No fiscal period - skip inventory transactions
            return transaction_ids

        for line in receipt.lines:
            # Skip lines with no accepted quantity
            if line.quantity_accepted <= Decimal("0"):
                continue

            # Get the PO line to check for item_id
            po_line = db.scalars(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.line_id == line.po_line_id
                )
            ).first()

            if not po_line or not po_line.item_id:
                # Skip non-inventory lines
                continue

            # Get the item
            item = db.get(Item, po_line.item_id)
            if not item or not item.track_inventory:
                # Skip non-tracked items
                continue

            # Determine warehouse
            warehouse_id = receipt.warehouse_id
            if not warehouse_id:
                # No warehouse specified on receipt - skip
                continue

            # Create inventory receipt transaction
            try:
                # Convert date to datetime for transaction
                from datetime import datetime as dt

                transaction_datetime = dt.combine(
                    receipt.receipt_date,
                    dt.min.time(),
                    tzinfo=UTC,
                )

                txn_input = TransactionInput(
                    transaction_type=TransactionType.RECEIPT,
                    transaction_date=transaction_datetime,
                    fiscal_period_id=fiscal_period.fiscal_period_id,
                    item_id=po_line.item_id,
                    warehouse_id=warehouse_id,
                    quantity=line.quantity_accepted,
                    unit_cost=po_line.unit_price,
                    uom=item.base_uom,
                    currency_code=item.currency_code,
                    location_id=line.location_id,
                    lot_number=line.lot_number,
                    serial_numbers=line.serial_numbers,
                    source_document_type="GOODS_RECEIPT",
                    source_document_id=receipt.receipt_id,
                    source_document_line_id=line.line_id,
                    reference=receipt.receipt_number,
                )

                transaction = InventoryTransactionService.create_receipt(
                    db=db,
                    organization_id=organization_id,
                    input=txn_input,
                    created_by_user_id=user_id,
                )
                transaction_ids.append(transaction.transaction_id)

            except HTTPException:
                # Log error but continue with other lines
                pass

        return transaction_ids

    @staticmethod
    def _update_po_status(db: Session, po: PurchaseOrder) -> None:
        """Update PO status from the received quantities on its lines.

        This used to also write `po.amount_received`.  It no longer does, and
        must not again: the received amount is DERIVED by
        `app.services.finance.ap.purchase_order_amounts`, which is its sole
        owner.  `purchase_order_line.quantity_received` — written just above by
        the receipt itself — is the authoritative input, and this method reads it
        for the status decision only.
        """
        total_ordered = Decimal("0")
        total_received = Decimal("0")

        for line in po.lines:
            total_ordered += line.quantity_ordered * line.unit_price
            total_received += line.quantity_received * line.unit_price

        if total_received >= total_ordered:
            po.status = POStatus.RECEIVED
        elif total_received > 0:
            po.status = POStatus.PARTIALLY_RECEIVED

    @staticmethod
    def _reverse_po_quantities(db: Session, receipt: GoodsReceipt) -> None:
        """Reverse PO line quantities for a rejected receipt."""
        if not receipt.lines:
            return
        for line in receipt.lines:
            po_line = db.scalars(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.line_id == line.po_line_id
                )
            ).first()
            if po_line:
                po_line.quantity_received -= line.quantity_received

        # Recalculate PO status
        po = db.scalars(
            select(PurchaseOrder).where(
                PurchaseOrder.po_id == receipt.po_id,
                PurchaseOrder.organization_id == receipt.organization_id,
            )
        ).first()
        if po:
            GoodsReceiptService._update_po_status(db, po)

    @staticmethod
    def _reverse_rejected_quantities(db: Session, receipt: GoodsReceipt) -> None:
        """Reverse only rejected quantities on PO lines for a partial receipt."""
        if not receipt.lines:
            return
        for line in receipt.lines:
            if line.quantity_rejected > 0:
                po_line = db.scalars(
                    select(PurchaseOrderLine).where(
                        PurchaseOrderLine.line_id == line.po_line_id
                    )
                ).first()
                if po_line:
                    po_line.quantity_received -= line.quantity_rejected

        # Recalculate PO status
        po = db.scalars(
            select(PurchaseOrder).where(
                PurchaseOrder.po_id == receipt.po_id,
                PurchaseOrder.organization_id == receipt.organization_id,
            )
        ).first()
        if po:
            GoodsReceiptService._update_po_status(db, po)

    @staticmethod
    def get(
        db: Session,
        receipt_id: str,
        organization_id: UUID | None = None,
    ) -> GoodsReceipt | None:
        """Get a goods receipt by ID with optional org_id isolation."""
        receipt = db.get(GoodsReceipt, coerce_uuid(receipt_id))
        if receipt is None:
            return None
        if organization_id is not None and receipt.organization_id != organization_id:
            return None
        return receipt

    @staticmethod
    def get_by_number(
        db: Session,
        organization_id: UUID,
        receipt_number: str,
    ) -> GoodsReceipt | None:
        """Get a goods receipt by number."""
        return db.scalars(
            select(GoodsReceipt).where(
                GoodsReceipt.organization_id == coerce_uuid(organization_id),
                GoodsReceipt.receipt_number == receipt_number,
            )
        ).first()

    @staticmethod
    def get_receipt_lines(
        db: Session, receipt_id: str
    ) -> builtins.list[GoodsReceiptLine]:
        """Get all lines for a goods receipt."""
        return list(
            db.scalars(
                select(GoodsReceiptLine)
                .where(GoodsReceiptLine.receipt_id == coerce_uuid(receipt_id))
                .order_by(GoodsReceiptLine.line_number)
            ).all()
        )

    @staticmethod
    def list_by_po(
        db: Session,
        organization_id: UUID,
        po_id: UUID,
    ) -> builtins.list[GoodsReceipt]:
        """List all goods receipts for a purchase order."""
        return list(
            db.scalars(
                select(GoodsReceipt)
                .where(
                    GoodsReceipt.organization_id == coerce_uuid(organization_id),
                    GoodsReceipt.po_id == coerce_uuid(po_id),
                )
                .order_by(GoodsReceipt.receipt_date.desc())
            ).all()
        )

    @staticmethod
    def list(
        db: Session,
        organization_id: str | UUID | None = None,
        supplier_id: str | None = None,
        po_id: str | None = None,
        status: ReceiptStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[GoodsReceipt]:
        """
        List goods receipts with filters.

        Args:
            db: Database session
            organization_id: Filter by organization
            supplier_id: Filter by supplier
            status: Filter by status
            from_date: Filter by start date
            to_date: Filter by end date
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of GoodsReceipt objects
        """
        stmt = select(GoodsReceipt)
        if organization_id is not None:
            stmt = stmt.where(
                GoodsReceipt.organization_id == coerce_uuid(organization_id)
            )

        if supplier_id:
            stmt = stmt.where(GoodsReceipt.supplier_id == coerce_uuid(supplier_id))

        if po_id:
            stmt = stmt.where(GoodsReceipt.po_id == coerce_uuid(po_id))

        if status:
            stmt = stmt.where(GoodsReceipt.status == status)

        if from_date:
            stmt = stmt.where(GoodsReceipt.receipt_date >= from_date)

        if to_date:
            stmt = stmt.where(GoodsReceipt.receipt_date <= to_date)

        return list(
            db.scalars(
                stmt.order_by(GoodsReceipt.receipt_date.desc())
                .offset(offset)
                .limit(limit)
            ).all()
        )


# Module-level instance
goods_receipt_service = GoodsReceiptService()
