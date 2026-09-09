"""
Purchase Order Model - AP Schema.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class POStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"
    SUPERSEDED = "SUPERSEDED"


class PurchaseOrder(Base):
    """
    Purchase order header.
    """

    __tablename__ = "purchase_order"
    __table_args__ = (
        UniqueConstraint("organization_id", "po_number", name="uq_po_number"),
        Index("idx_po_supplier", "supplier_id"),
        Index("idx_po_status", "organization_id", "status"),
        Index(
            "uq_po_variation_id",
            "organization_id",
            "variation_id",
            unique=True,
            postgresql_where=text("variation_id IS NOT NULL"),
        ),
        {"schema": "ap"},
    )

    po_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("core_org.organization.organization_id"),
        nullable=False,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ap.supplier.supplier_id"),
        nullable=False,
    )

    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    po_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Currency
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )

    # Amounts
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, default=0
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)

    # `amount_invoiced` and `amount_received` are DELIBERATELY absent.
    #
    # They were stored columns on this row with split authority: `amount_received`
    # had one absolute writer and one incremental writer that disagreed, and
    # `amount_invoiced` had no writer at all and was permanently zero while the
    # detail screen rendered it as a financial fact.  Both are derivable from the
    # authoritative receipt and invoice records, so they are now derived, by one
    # named owner: `app.services.finance.ap.purchase_order_amounts`.
    #
    # Do not add either column back.  A row whose columns are owned by different
    # authorities cannot be transferred as a unit, which is why the storage is
    # separated rather than the writers merely coordinated.

    # Status
    status: Mapped[POStatus] = mapped_column(
        Enum(POStatus, name="po_status"),
        nullable=False,
        default=POStatus.DRAFT,
    )

    shipping_address: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    terms_and_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Budget / Encumbrance
    budget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    commitment_journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    # SoD tracking
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Amendment / Variation tracking
    is_amendment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    original_po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ap.purchase_order.po_id"),
        nullable=True,
        comment="Links to the baseline PO being amended",
    )
    amendment_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
        comment="Version counter: baseline=1, first amendment=2, etc.",
    )
    amendment_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Reason for the amendment / variation"
    )
    variation_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="Source amendment identifier for traceability",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )

    # Relationships
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        "PurchaseOrderLine",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
    )
    original_po: Mapped["PurchaseOrder | None"] = relationship(
        "PurchaseOrder",
        remote_side="PurchaseOrder.po_id",
        foreign_keys=[original_po_id],
        uselist=False,
    )


# Forward reference
from app.models.finance.ap.purchase_order_line import PurchaseOrderLine  # noqa: E402
