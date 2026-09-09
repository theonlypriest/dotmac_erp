"""
Payment Intent Model - Payments Schema.

Tracks Paystack payment initialization and completion.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base


class PaymentIntentStatus(str, enum.Enum):
    """Status of a payment intent.

    Every member except ``INDETERMINATE`` asserts a FACT about the money: it
    moved, it did not move, it moved and came back, nobody tried. ``FAILED`` in
    particular is a claim that the payout did not happen, and downstream readers
    treat it as one — the claim goes back to APPROVED, the intent becomes
    resettable, the operator is told to try again.

    ``INDETERMINATE`` is the one member that asserts nothing about the money. It
    means we could not OBSERVE the outcome: the provider was unreachable, it
    answered 5xx, the poll budget ran out with no verdict, or it reported a
    status this system does not recognise. It is not a weaker FAILED, and it
    must never be written where FAILED would do — see ADR-0007, adopting the
    fleet rule in `dotmac_starter_mt` ADR-0032 (unobserved is UNKNOWN, never
    ABSENT).
    """

    PENDING = "PENDING"  # Created, awaiting payment
    PROCESSING = "PROCESSING"  # Payment in progress
    COMPLETED = "COMPLETED"  # Successfully paid
    FAILED = "FAILED"  # Provider said the payment did not happen
    REVERSED = "REVERSED"  # Completed but later reversed/refunded
    ABANDONED = "ABANDONED"  # User didn't complete
    EXPIRED = "EXPIRED"  # Timed out
    INDETERMINATE = "INDETERMINATE"  # Outcome NOT OBSERVED - not a verdict


class PaymentDirection(str, enum.Enum):
    """Direction of payment flow."""

    INBOUND = "INBOUND"  # Collection - money coming in (customer payments)
    OUTBOUND = "OUTBOUND"  # Transfer - money going out (expense reimbursements)


class PaymentIntent(Base):
    """
    Payment Intent - tracks a payment from initialization to completion.

    A PaymentIntent is created when a user initiates a payment, and is updated
    as the payment progresses through the Paystack flow.
    """

    __tablename__ = "payment_intent"
    __table_args__ = {"schema": "payments"}

    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Paystack reference - our unique reference sent to Paystack
    paystack_reference: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
    )
    # Access code returned by Paystack
    paystack_access_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    # URL to redirect user for payment
    authorization_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    # Payment details
    amount: Mapped[Decimal] = mapped_column(
        Numeric(19, 4),
        nullable=False,
        comment="Amount in currency units (e.g., Naira, not kobo)",
    )
    currency_code: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=settings.default_functional_currency_code,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Customer email for Paystack",
    )

    # Payment direction - inbound (collection) or outbound (transfer)
    direction: Mapped[PaymentDirection] = mapped_column(
        Enum(
            PaymentDirection,
            name="payment_direction",
            schema="payments",
            create_type=False,
        ),
        nullable=False,
        default=PaymentDirection.INBOUND,
        comment="INBOUND for collections, OUTBOUND for transfers/payouts",
    )

    # Bank account linkage for reconciliation
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Bank account for settlement/source of funds",
    )

    # Source reference - what is being paid
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="INVOICE, EXPENSE_CLAIM, GENERAL",
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="ID of the document being paid",
    )

    # Transfer-specific fields (for OUTBOUND payments)
    transfer_recipient_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Paystack transfer recipient code for payouts",
    )
    transfer_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Paystack transfer code after initiation",
    )
    recipient_bank_code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Recipient bank code for transfers",
    )
    recipient_account_number: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Recipient account number for transfers",
    )
    recipient_account_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Recipient account name (verified by Paystack)",
    )

    # Status
    status: Mapped[PaymentIntentStatus] = mapped_column(
        Enum(
            PaymentIntentStatus,
            name="payment_intent_status",
            schema="payments",
            create_type=False,
        ),
        nullable=False,
        default=PaymentIntentStatus.PENDING,
    )

    # Result after completion
    customer_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Links to AR customer_payment after successful payment",
    )
    paystack_transaction_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Paystack transaction ID",
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    gateway_response: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Full response from Paystack",
    )

    # Fee tracking
    fee_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(19, 4),
        nullable=True,
        comment="Gateway fee charged (in currency units, not kobo)",
    )
    fee_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="GL journal entry for fee posting",
    )

    # Custom data
    intent_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Custom metadata (invoice_number, customer_name, etc.)",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this intent expires",
    )

    # Transfer polling circuit breaker
    poll_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Number of times this transfer has been polled for status",
    )
    last_poll_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Last error message from transfer status polling",
    )
    unresolved_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "When this intent became INDETERMINATE (outcome unobserved). "
            "NULL for every other status; the age of this timestamp is what "
            "raises the operator signal and what the slow reconciler orders by."
        ),
    )

    # Timestamps
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
