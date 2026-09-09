"""
Payment Service.

Handles payment intent creation and processing for Paystack integration.
"""

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.finance.ar.customer import Customer
from app.models.finance.ar.customer_payment import PaymentMethod
from app.models.finance.ar.invoice import Invoice, InvoiceStatus
from app.models.finance.payments.payment_intent import (
    PaymentDirection,
    PaymentIntent,
    PaymentIntentStatus,
)
from app.models.finance.payments.transfer_batch import (
    TransferBatchItem,
    TransferBatchItemStatus,
    TransferBatchStatus,
)
from app.metrics import observe_transfer_unresolved
from app.services.common import coerce_uuid
from app.services.finance.payments.paystack_client import (
    PaystackClient,
    PaystackConfig,
    PaystackError,
    PaystackUnreachable,
)
from app.services.finance.platform.org_context import org_context_service
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


TRANSFER_POLL_MIN_AGE = timedelta(minutes=2)
"""How old an in-flight transfer must be before the reconciler will poll it.

Paystack webhooks normally land within seconds; anything older than this has
plausibly lost its webhook.
"""

TRANSFER_POLL_MAX_ATTEMPTS = 10
"""Circuit breaker. After this many failed poll attempts the fast poller stops
asking. At the scheduled cadence of one pass every two minutes that is roughly
twenty minutes of retrying.

Spending the budget is NOT a verdict. It ends the fast loop and nothing more:
what the intent becomes then depends on whether Paystack ever answered — see
:meth:`PaymentService._record_transfer_poll_failure`.

This lived in ``app.tasks.expense`` while the worker still made the decision
itself; it belongs with the owner of the column that decision writes.
"""

_PAYSTACK_IN_FLIGHT_STATUSES = frozenset({"pending", "otp", "processing", "receipt"})
"""Paystack transfer statuses that genuinely mean "still moving".

Enumerated rather than assumed. Everything Paystack's transfer API documents is
either terminal (``success``/``failed``/``reversed``, handled above) or one of
these; a status outside BOTH sets is a word this system has never seen, and the
honest thing to do with it is admit we do not know what it means rather than
guess the reassuring interpretation.
"""

INDETERMINATE_RECHECK_INTERVAL = timedelta(hours=1)
"""How long an INDETERMINATE intent rests between slow-reconciler attempts.

The fast poller runs every two minutes because a webhook is merely late. An
INDETERMINATE intent is a different animal: the fast loop already exhausted
itself against it, so re-asking at that cadence is just load. It is re-asked
hourly, forever, because the money is real and no amount of elapsed time turns
"we do not know" into "it did not happen".
"""

TRANSFER_UNRESOLVED_ALERT_HOURS_DEFAULT = 6
"""Fallback for ``paystack_transfer_unresolved_alert_hours`` (see the spec in
``app.services.settings_spec`` for why six). Duplicated as a constant only so
the reconciler still has a documented threshold if the settings row and the
spec are both unreachable; the spec default is the real answer."""


class TransferPollOutcome(str, enum.Enum):
    """What one reconciliation pass concluded about one transfer intent."""

    COMPLETED = "completed"
    FAILED = "failed"
    STILL_PENDING = "still_pending"
    ABANDONED = "abandoned"
    ERRORED = "errored"
    SKIPPED = "skipped"
    #: The fast loop gave up without ever getting an answer out of Paystack.
    #: Distinct from ABANDONED, which is reserved for the case where Paystack
    #: DID answer and the answer was a refusal.
    INDETERMINATE = "indeterminate"
    #: The slow reconciler asked again and still could not observe an outcome.
    STILL_UNRESOLVED = "still_unresolved"
    #: Paystack said the money moved and came back.
    REVERSED = "reversed"


class TransferOutcomeUnknown(Exception):
    """Initiation neither confirmed started nor confirmed refused.

    Raised out of :meth:`PaymentService.initiate_expense_transfer` when the
    request left this process and Paystack never told us what became of it.
    The intent is INDETERMINATE by the time this is raised, and the caller's
    only correct response is to STOP — not to retry, not to report a failure.
    """

    def __init__(self, intent_id: UUID, reason: str):
        super().__init__(reason)
        self.intent_id = intent_id
        self.reason = reason


def is_unobserved(error: BaseException) -> bool:
    """Whether this exception leaves the transfer's outcome unknown.

    Inverted on purpose. The question is not "is this a non-observation?" but
    "did Paystack actually answer?", and only one exception type can say yes: a
    ``PaystackError`` that is not a ``PaystackUnreachable`` — a parsed
    ``status: false`` body or a 4xx refusal.

    Everything else — a timeout, a 5xx, a bug in our own posting code, an
    exception type that does not exist yet — is unobserved. Getting the default
    the other way round is exactly the defect ADR-0007 fixes: the safe answer
    must be the one you fall into, not the one you remember to write.
    """
    if isinstance(error, PaystackUnreachable):
        return True
    return not isinstance(error, PaystackError)


@dataclass(frozen=True)
class TransferPollResult:
    """Result of reconciling one transfer intent.

    Returned instead of leaving the caller to read ``intent.status`` back:
    a scheduled worker that reads the status column to decide what happened is
    one refactor away from writing it.
    """

    intent_id: UUID
    outcome: TransferPollOutcome
    poll_count: int
    error: str | None = None


class PaymentService:
    """
    Service for payment operations.

    Manages payment intent lifecycle from creation through completion.
    """

    def __init__(self, db: Session, organization_id: UUID):
        self.db = db
        self.organization_id = coerce_uuid(organization_id)

    def _commit_and_refresh(self, intent: PaymentIntent) -> None:
        self.db.commit()
        self.db.refresh(intent)

    @staticmethod
    def get_intent_by_reference(
        db: Session,
        reference: str,
        organization_id: UUID | None = None,
    ) -> PaymentIntent | None:
        """Get a payment intent by reference (optionally scoped to org)."""
        stmt = select(PaymentIntent).where(
            PaymentIntent.paystack_reference == reference
        )
        if organization_id is not None:
            stmt = stmt.where(
                PaymentIntent.organization_id == coerce_uuid(organization_id)
            )
        return db.scalar(stmt)

    def create_invoice_payment_intent(
        self,
        invoice_id: UUID,
        callback_url: str,
        paystack_config: PaystackConfig,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentIntent:
        """
        Create a payment intent for an invoice.

        Args:
            invoice_id: The invoice to pay
            callback_url: URL to redirect after payment
            paystack_config: Paystack credentials
            metadata: Optional additional metadata

        Returns:
            PaymentIntent with authorization URL

        Raises:
            HTTPException: If invoice is not valid for payment
        """
        inv_id = coerce_uuid(invoice_id)

        # Get invoice
        invoice = self.db.get(Invoice, inv_id)
        if not invoice:
            raise HTTPException(
                status_code=404, detail=f"Invoice {invoice_id} not found"
            )
        if invoice.organization_id != self.organization_id:
            raise HTTPException(status_code=404, detail="Invoice not found")

        # Validate invoice is payable
        payable_statuses = [
            InvoiceStatus.POSTED,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.OVERDUE,
        ]
        if invoice.status not in payable_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invoice with status '{invoice.status.value}' cannot be paid online",
            )

        if invoice.balance_due <= Decimal("0"):
            raise HTTPException(status_code=400, detail="Invoice is already fully paid")

        # Check for existing active payment intent to prevent duplicate payments
        #
        # INDETERMINATE is deliberately absent from this list rather than
        # forgotten: it is only ever written on the OUTBOUND transfer path, and
        # this query is scoped to `source_type == "INVOICE"` (inbound
        # collections), so no INDETERMINATE row can reach it. If inbound
        # collection ever gains an unobserved outcome, it needs the same
        # unconditional refusal `create_expense_payment_intent` has below — NOT
        # an entry here, because the branch under this query would then stamp
        # EXPIRED over an intent whose money may have moved.
        active_statuses = [PaymentIntentStatus.PENDING, PaymentIntentStatus.PROCESSING]
        existing_intent = self.db.scalar(
            select(PaymentIntent).where(
                PaymentIntent.source_type == "INVOICE",
                PaymentIntent.source_id == inv_id,
                PaymentIntent.status.in_(active_statuses),
            )
        )
        if existing_intent:
            expires_at = existing_intent.expires_at
            if expires_at and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at and expires_at <= datetime.now(UTC):
                existing_intent.status = PaymentIntentStatus.EXPIRED
                self.db.flush()
            else:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A payment is already in progress for this invoice "
                        f"(status: {existing_intent.status.value}). "
                        "Please wait for it to complete or check the payment history."
                    ),
                )

        # Get customer and validate email
        customer = self.db.get(Customer, invoice.customer_id)
        if not customer:
            raise HTTPException(
                status_code=400, detail="Customer not found for invoice"
            )

        # Get email from primary_contact JSONB field
        email = None
        if customer.primary_contact and isinstance(customer.primary_contact, dict):
            email = customer.primary_contact.get("email")

        if not email:
            raise HTTPException(
                status_code=400,
                detail="Customer email is required for online payment. Add email to customer's primary contact.",
            )

        # Generate unique reference
        # Format: INV-{invoice_number}-{short_uuid}
        short_uuid = uuid4().hex[:8]
        reference = f"INV-{invoice.invoice_number}-{short_uuid}"

        # Amount in kobo (Naira * 100) - use round to avoid truncation
        amount_kobo = int(
            (Decimal(invoice.balance_due) * Decimal("100")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

        # Build metadata
        intent_metadata = {
            "invoice_number": invoice.invoice_number,
            "invoice_id": str(inv_id),
            "customer_name": customer.legal_name or customer.trading_name,
            "customer_id": str(customer.customer_id),
        }
        if metadata:
            intent_metadata.update(metadata)

        # Get collection bank account from settings
        collection_bank_account_id = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_collection_bank_account_id",
            organization_id=self.organization_id,
        )
        bank_account_uuid = None
        if collection_bank_account_id:
            try:
                bank_account_uuid = coerce_uuid(collection_bank_account_id)
            except ValueError:
                logger.warning(
                    f"Invalid collection bank account ID: {collection_bank_account_id}"
                )

        # Create payment intent
        intent = PaymentIntent(
            intent_id=uuid4(),
            organization_id=self.organization_id,
            paystack_reference=reference,
            amount=invoice.balance_due,
            currency_code=invoice.currency_code
            or org_context_service.get_functional_currency(
                self.db, self.organization_id
            ),
            email=email,
            direction=PaymentDirection.INBOUND,
            bank_account_id=bank_account_uuid,
            source_type="INVOICE",
            source_id=inv_id,
            status=PaymentIntentStatus.PENDING,
            intent_metadata=intent_metadata,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        # Initialize with Paystack
        with PaystackClient(paystack_config) as client:
            result = client.initialize_transaction(
                email=email,
                amount=amount_kobo,
                reference=reference,
                callback_url=callback_url,
                metadata=intent_metadata,
                currency=intent.currency_code,
            )

            intent.paystack_access_code = result.access_code
            intent.authorization_url = result.authorization_url

        self.db.add(intent)
        self.db.flush()

        logger.info(
            f"Created payment intent {intent.intent_id} for invoice {invoice.invoice_number}",
            extra={
                "intent_id": str(intent.intent_id),
                "invoice_id": str(inv_id),
                "amount": str(invoice.balance_due),
                "reference": reference,
            },
        )

        self._commit_and_refresh(intent)
        return intent

    def verify_payment_by_reference(
        self,
        reference: str,
        paystack_config: PaystackConfig,
    ) -> PaymentIntent:
        """Verify a payment by reference with Paystack and update intent status."""
        intent = PaymentService.get_intent_by_reference(
            self.db, reference, self.organization_id
        )
        if not intent:
            raise HTTPException(status_code=404, detail="Payment not found")

        if intent.status == PaymentIntentStatus.COMPLETED:
            return intent

        try:
            with PaystackClient(paystack_config) as client:
                result = client.verify_transaction(reference)

            if result.status == "success":
                try:
                    self._validate_amount_and_currency(
                        intent=intent,
                        amount_kobo=result.amount,
                        currency=result.currency,
                        context="verify",
                    )
                except ValueError as e:
                    self.mark_payment_failed(
                        intent,
                        str(e),
                        gateway_response={
                            "status": result.status,
                            "amount": result.amount,
                            "currency": result.currency,
                            "reference": result.reference,
                        },
                    )
                    self._commit_and_refresh(intent)
                    raise HTTPException(status_code=400, detail=str(e))

                if result.paid_at:
                    try:
                        paid_at = datetime.fromisoformat(
                            result.paid_at.replace("Z", "+00:00")
                        )
                    except ValueError:
                        paid_at = datetime.now(UTC)
                else:
                    paid_at = datetime.now(UTC)

                self.process_successful_payment(
                    intent=intent,
                    transaction_id=result.transaction_id,
                    paid_at=paid_at,
                    gateway_response={
                        "status": result.status,
                        "gateway_response": result.gateway_response,
                        "channel": result.channel,
                    },
                    channel=result.channel,
                )

            elif result.status == "failed":
                self.mark_payment_failed(
                    intent,
                    result.gateway_response or "Payment failed",
                )

            elif result.status == "abandoned":
                self.mark_payment_abandoned(intent)

        except PaystackError:
            raise

        self._commit_and_refresh(intent)
        return intent

    @staticmethod
    def _validate_amount_and_currency(
        intent: PaymentIntent,
        amount_kobo: int,
        currency: str,
        context: str,
    ) -> None:
        """Validate Paystack amount/currency against our intent."""
        expected_amount_kobo = int(
            (Decimal(intent.amount) * Decimal("100")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        expected_currency = intent.currency_code.upper()
        paystack_currency = (currency or intent.currency_code).upper()

        amount_diff = abs(int(amount_kobo) - expected_amount_kobo)
        if amount_diff > 1:
            logger.error(
                "SECURITY: Amount mismatch in %s! Expected %s kobo, got %s kobo. "
                "Intent: %s, Reference: %s",
                context,
                expected_amount_kobo,
                amount_kobo,
                intent.intent_id,
                intent.paystack_reference,
            )
            raise ValueError(
                f"Amount mismatch: expected {expected_amount_kobo} kobo, "
                f"received {amount_kobo} kobo"
            )

        if paystack_currency != expected_currency:
            logger.error(
                "SECURITY: Currency mismatch in %s! Expected %s, got %s. "
                "Intent: %s, Reference: %s",
                context,
                expected_currency,
                paystack_currency,
                intent.intent_id,
                intent.paystack_reference,
            )
            raise ValueError(
                f"Currency mismatch: expected {expected_currency}, "
                f"received {paystack_currency}"
            )

    def list_pending_transfers(self) -> list[PaymentIntent]:
        """List pending outbound transfers for the organization."""
        return list(
            self.db.scalars(
                select(PaymentIntent)
                .where(
                    PaymentIntent.organization_id == self.organization_id,
                    PaymentIntent.direction == PaymentDirection.OUTBOUND,
                    PaymentIntent.status.in_(
                        [
                            PaymentIntentStatus.PENDING,
                            PaymentIntentStatus.PROCESSING,
                        ]
                    ),
                )
                .order_by(PaymentIntent.created_at.desc())
            ).all()
        )

    def process_successful_payment(
        self,
        intent: PaymentIntent,
        transaction_id: str,
        paid_at: datetime,
        gateway_response: dict[str, Any],
        channel: str = "card",
    ) -> UUID:
        """
        Process a successful payment.

        Creates CustomerPayment, posts it, and updates the invoice.

        Args:
            intent: The payment intent
            transaction_id: Paystack transaction ID
            paid_at: When payment was made
            gateway_response: Full Paystack response
            channel: Payment channel (card, bank, ussd, etc.)

        Returns:
            customer_payment_id

        Raises:
            HTTPException: If processing fails
        """

        # Re-fetch intent with row-level lock to prevent race conditions
        # between webhook and manual verification
        locked_intent = self.db.execute(
            select(PaymentIntent)
            .where(PaymentIntent.intent_id == intent.intent_id)
            .with_for_update(nowait=False)
        ).scalar_one_or_none()

        if not locked_intent:
            raise HTTPException(
                status_code=404,
                detail="Payment intent not found",
            )

        # Check if already processed (idempotency) - using locked row
        if locked_intent.status == PaymentIntentStatus.COMPLETED:
            logger.info(f"Payment intent {locked_intent.intent_id} already completed")
            if locked_intent.customer_payment_id:
                return locked_intent.customer_payment_id
            raise HTTPException(
                status_code=400,
                detail="Payment already processed but customer_payment_id missing",
            )

        # Only process PENDING or PROCESSING intents
        if locked_intent.status not in [
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.PROCESSING,
        ]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot process payment with status '{locked_intent.status.value}'",
            )

        # Update status to PROCESSING using the locked intent
        locked_intent.status = PaymentIntentStatus.PROCESSING
        self.db.flush()

        # Use locked_intent from here on
        intent = locked_intent

        # Validate source
        if intent.source_type != "INVOICE":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported source type: {intent.source_type}",
            )

        invoice = self.db.get(Invoice, intent.source_id)
        if not invoice:
            raise HTTPException(
                status_code=400,
                detail=f"Invoice {intent.source_id} not found",
            )

        customer = self.db.get(Customer, invoice.customer_id)
        if not customer:
            raise HTTPException(
                status_code=400,
                detail="Customer not found",
            )

        # Map Paystack channel to PaymentMethod
        payment_method = self._map_channel_to_method(channel)

        # Create customer payment using the service
        from app.services.finance.ar.customer_payment import (
            CustomerPaymentInput,
            CustomerPaymentService,
            PaymentAllocationInput,
        )

        # We need a user ID for the payment creation
        # Use a system user or the customer's created_by_user_id
        system_user_id = invoice.created_by_user_id

        payment_input = CustomerPaymentInput(
            customer_id=customer.customer_id,
            payment_date=paid_at.date(),
            payment_method=payment_method,
            currency_code=intent.currency_code,
            amount=intent.amount,
            bank_account_id=intent.bank_account_id,  # Paystack settlement account
            reference=intent.paystack_reference,
            description=f"Paystack payment for {invoice.invoice_number}",
            correlation_id=str(intent.intent_id),
            allocations=[
                PaymentAllocationInput(
                    invoice_id=invoice.invoice_id,
                    amount=min(intent.amount, invoice.balance_due),
                )
            ],
        )

        try:
            payment = CustomerPaymentService.create_payment(
                db=self.db,
                organization_id=self.organization_id,
                input=payment_input,
                created_by_user_id=system_user_id,
            )

            # Auto-post if bank account is configured
            if intent.bank_account_id:
                try:
                    CustomerPaymentService.post_payment(
                        db=self.db,
                        organization_id=self.organization_id,
                        payment_id=payment.payment_id,
                        posted_by_user_id=system_user_id,
                        posting_date=paid_at.date(),
                    )
                    logger.info(
                        f"Auto-posted Paystack payment {payment.payment_id} to GL",
                        extra={"payment_id": str(payment.payment_id)},
                    )
                except Exception as post_error:
                    # Log but don't fail - payment is still recorded
                    logger.warning(
                        f"Failed to auto-post payment {payment.payment_id}: {post_error}",
                        extra={
                            "payment_id": str(payment.payment_id),
                            "error": str(post_error),
                        },
                    )
            else:
                logger.info(
                    f"Paystack payment {payment.payment_id} created but not posted - "
                    "no settlement bank account configured",
                )

            # Update intent
            intent.status = PaymentIntentStatus.COMPLETED
            intent.customer_payment_id = payment.payment_id
            intent.paystack_transaction_id = transaction_id
            intent.paid_at = paid_at
            intent.gateway_response = gateway_response

            self.db.flush()

            logger.info(
                f"Processed payment {payment.payment_id} for intent {intent.intent_id}",
                extra={
                    "payment_id": str(payment.payment_id),
                    "intent_id": str(intent.intent_id),
                    "invoice_id": str(invoice.invoice_id),
                    "amount": str(intent.amount),
                },
            )

            return payment.payment_id

        except Exception as e:
            logger.exception(f"Failed to process payment for intent {intent.intent_id}")
            intent.status = PaymentIntentStatus.FAILED
            intent.gateway_response = {
                "error": str(e),
                "original_response": gateway_response,
            }
            self.db.flush()
            raise

    def mark_payment_failed(
        self,
        intent: PaymentIntent,
        error_message: str,
        gateway_response: dict[str, Any] | None = None,
    ) -> None:
        """
        Mark a payment intent as failed.

        Args:
            intent: The payment intent
            error_message: Error description
            gateway_response: Optional Paystack response
        """
        intent.status = PaymentIntentStatus.FAILED
        intent.gateway_response = {
            "error": error_message,
            **(gateway_response or {}),
        }
        self.db.flush()

        logger.warning(
            f"Payment intent {intent.intent_id} failed: {error_message}",
            extra={
                "intent_id": str(intent.intent_id),
                "error": error_message,
            },
        )

    def mark_payment_abandoned(self, intent: PaymentIntent) -> None:
        """Mark a payment intent as abandoned (user didn't complete)."""
        intent.status = PaymentIntentStatus.ABANDONED
        self.db.flush()

        logger.info(f"Payment intent {intent.intent_id} abandoned")

    def get_intent_by_id(self, intent_id: UUID) -> PaymentIntent | None:
        """Get a payment intent by ID."""
        intent = self.db.get(PaymentIntent, coerce_uuid(intent_id))
        if intent and intent.organization_id != self.organization_id:
            return None
        return intent

    def reset_expense_payment_intent(
        self,
        expense_claim_id: UUID,
        reason: str | None = None,
        force: bool = False,
    ) -> PaymentIntent:
        """Reset a stale expense payment intent so it can be retried.

        This method is intended for manual recovery when a claim is still
        APPROVED but payout initiation could not complete. It marks the latest
        non-completed outbound expense intent as ABANDONED.
        """
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus

        claim_id = coerce_uuid(expense_claim_id)

        claim = self.db.get(ExpenseClaim, claim_id)
        if not claim:
            raise HTTPException(
                status_code=404,
                detail=f"Expense claim {expense_claim_id} not found",
            )
        if claim.organization_id != self.organization_id:
            raise HTTPException(status_code=404, detail="Expense claim not found")
        if claim.status != ExpenseClaimStatus.APPROVED:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Can only reset payment intent for claims in APPROVED state. "
                    f"Current status: {claim.status.value}"
                ),
            )

        intent = self.db.scalar(
            select(PaymentIntent)
            .where(
                PaymentIntent.direction == PaymentDirection.OUTBOUND,
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.source_id == claim_id,
            )
            .order_by(PaymentIntent.created_at.desc())
            .limit(1)
        )

        if not intent:
            raise HTTPException(
                status_code=404,
                detail="No payment intent found for this expense claim",
            )

        if intent.status == PaymentIntentStatus.COMPLETED:
            raise HTTPException(
                status_code=400,
                detail="Cannot reset a completed payout intent",
            )

        if intent.status == PaymentIntentStatus.REVERSED:
            raise HTTPException(
                status_code=400,
                detail="Cannot reset a reversed payout intent",
            )

        # Not resettable, and not overridable by `force` either. Resetting an
        # intent is how an operator gets permission to pay the claim AGAIN;
        # doing that while the first payout's outcome is unknown is how the
        # employee gets reimbursed twice. FAILED/EXPIRED/ABANDONED are all
        # safe to reset because each one asserts the money did not move.
        # INDETERMINATE asserts nothing (ADR-0007), so the only way out is
        # `resolve_indeterminate_transfer` obtaining a real verdict — after
        # which, if that verdict is FAILED, this method works normally.
        if intent.status == PaymentIntentStatus.INDETERMINATE:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot reset a payout whose outcome is unknown. This "
                    "transfer was recorded INDETERMINATE because Paystack "
                    "never confirmed what happened to it; it may still have "
                    "moved money. It must be resolved against Paystack "
                    "(automatically, or by an operator confirming the "
                    "outcome) before the claim can be paid again."
                ),
            )

        if not force and intent.status not in {
            PaymentIntentStatus.ABANDONED,
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.EXPIRED,
        }:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Intent is not in a retryable state (ABANDONED/FAILED/EXPIRED). "
                    "Use force=true only for active intents."
                ),
            )

        if force and intent.status in {
            PaymentIntentStatus.ABANDONED,
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.EXPIRED,
        }:
            # These can be safely retried once marked with a fresh intent.
            intent.status = PaymentIntentStatus.ABANDONED
        else:
            # Force mode allows operators to abandon active/edge intents.
            intent.status = PaymentIntentStatus.ABANDONED

        intent.expires_at = datetime.now(UTC)
        intent.gateway_response = {
            **(intent.gateway_response or {}),
            "manual_revert": True,
            "revert_reason": reason or "manual_retry_request",
            "reverted_at": datetime.now(UTC).isoformat(),
        }
        self.db.flush()

        logger.info(
            "Reset expense payment intent %s for claim %s (force=%s)",
            intent.intent_id,
            claim_id,
            force,
        )

        return intent

    @staticmethod
    def _map_channel_to_method(channel: str) -> PaymentMethod:
        """Map Paystack channel to AR PaymentMethod."""
        channel_map = {
            "card": PaymentMethod.CARD,
            "bank": PaymentMethod.BANK_TRANSFER,
            "ussd": PaymentMethod.MOBILE_MONEY,
            "mobile_money": PaymentMethod.MOBILE_MONEY,
            "bank_transfer": PaymentMethod.BANK_TRANSFER,
            "qr": PaymentMethod.MOBILE_MONEY,
        }
        return channel_map.get(channel.lower(), PaymentMethod.CARD)

    # =========================================================================
    # Expense Reimbursement (Outbound Transfer) Methods
    # =========================================================================

    def create_expense_payment_intent(
        self,
        expense_claim_id: UUID,
        paystack_config: PaystackConfig,
        recipient_bank_code: str | None = None,
        recipient_account_number: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentIntent:
        """
        Create a payment intent for expense reimbursement via Paystack Transfer.

        Args:
            expense_claim_id: The expense claim to reimburse
            paystack_config: Paystack credentials
            recipient_bank_code: Employee's bank code (extracted from claim if omitted)
            recipient_account_number: Employee's bank account number (extracted from claim if omitted)
            metadata: Optional additional metadata

        Returns:
            PaymentIntent with transfer details

        Raises:
            HTTPException: If expense claim is not valid for payment
        """
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus

        claim_id = coerce_uuid(expense_claim_id)
        should_commit = False

        # An unresolved payout blocks a new one, unconditionally and before
        # anything else. This is deliberately NOT folded into `active_statuses`
        # below: that branch expires a stale intent and lets a fresh one
        # through, and an INDETERMINATE intent is precisely the one that must
        # never be expired away — its money may already have left (ADR-0007).
        unresolved_intent = self.db.scalar(
            select(PaymentIntent).where(
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.source_id == claim_id,
                PaymentIntent.status == PaymentIntentStatus.INDETERMINATE,
            )
        )
        if unresolved_intent:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A previous payout for this claim has an unknown outcome "
                    f"(intent {unresolved_intent.intent_id}). It must be "
                    "resolved against Paystack before another payout can be "
                    "created, otherwise this claim may be paid twice."
                ),
            )

        # Check for existing active payment intent (idempotency check)
        active_statuses = [
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.PROCESSING,
        ]
        existing_intent = self.db.scalar(
            select(PaymentIntent).where(
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.source_id == claim_id,
                PaymentIntent.status.in_(active_statuses),
            )
        )

        if existing_intent:
            # Check if expired
            expires_at = existing_intent.expires_at
            if expires_at and expires_at <= datetime.now(UTC):
                # Mark as expired and allow new intent
                existing_intent.status = PaymentIntentStatus.EXPIRED
                self.db.flush()
                should_commit = True
                logger.info(
                    f"Expired stale payment intent {existing_intent.intent_id} for claim {claim_id}"
                )
            else:
                # Return existing active intent
                logger.info(
                    f"Returning existing payment intent {existing_intent.intent_id} for claim {claim_id}"
                )
                return existing_intent

        # Verify transfers are enabled
        transfers_enabled = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_transfers_enabled",
            organization_id=self.organization_id,
        )
        if not transfers_enabled:
            raise HTTPException(
                status_code=400,
                detail="Paystack transfers are not enabled",
            )

        # Get expense claim with row-level lock to prevent race conditions
        claim = self.db.scalar(
            select(ExpenseClaim)
            .where(ExpenseClaim.claim_id == claim_id)
            .with_for_update(nowait=False)
        )
        if not claim:
            raise HTTPException(
                status_code=404, detail=f"Expense claim {expense_claim_id} not found"
            )
        if claim.organization_id != self.organization_id:
            raise HTTPException(status_code=404, detail="Expense claim not found")

        # Validate claim is approved and ready for payment
        if claim.status != ExpenseClaimStatus.APPROVED:
            raise HTTPException(
                status_code=400,
                detail=f"Expense claim with status '{claim.status.value}' cannot be paid",
            )

        if claim.net_payable_amount is None or claim.net_payable_amount <= Decimal("0"):
            raise HTTPException(
                status_code=400, detail="No amount payable for this claim"
            )

        # Extract bank details from claim if not provided by caller
        if not recipient_bank_code:
            recipient_bank_code = claim.recipient_bank_code
        if not recipient_account_number:
            recipient_account_number = claim.recipient_account_number
        if not recipient_bank_code or not recipient_account_number:
            raise HTTPException(
                status_code=400,
                detail="Expense claim is missing bank details",
            )

        # Get employee for recipient details
        from app.models.people.hr.employee import Employee

        employee = self.db.get(Employee, claim.employee_id)
        if not employee:
            raise HTTPException(
                status_code=400, detail="Employee not found for expense claim"
            )

        email = employee.work_email or employee.personal_email
        if not email:
            raise HTTPException(
                status_code=400,
                detail="Employee email is required for transfer notification",
            )

        # Get transfer bank account from settings (source of funds)
        transfer_bank_account_id = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_transfer_bank_account_id",
            organization_id=self.organization_id,
        )
        bank_account_uuid = None
        if transfer_bank_account_id:
            try:
                bank_account_uuid = coerce_uuid(transfer_bank_account_id)
            except ValueError:
                logger.warning(
                    f"Invalid transfer bank account ID: {transfer_bank_account_id}"
                )

        # Generate unique reference
        short_uuid = uuid4().hex[:8]
        reference = f"EXP-{claim.claim_number}-{short_uuid}"

        # Amount in kobo (Naira * 100) - use round to avoid truncation
        int(
            (Decimal(claim.net_payable_amount) * Decimal("100")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

        # Build metadata
        intent_metadata = {
            "claim_number": claim.claim_number,
            "claim_id": str(claim_id),
            "employee_name": employee.full_name,
            "employee_id": str(employee.employee_id),
        }
        if metadata:
            intent_metadata.update(metadata)
        resolved_currency_code = claim.currency_code or (
            org_context_service.get_functional_currency(self.db, self.organization_id)
        )

        # Verify account and create transfer recipient with Paystack
        with PaystackClient(paystack_config) as client:
            # Resolve account to get verified name
            account_info = client.resolve_account(
                account_number=recipient_account_number,
                bank_code=recipient_bank_code,
            )

            # Create transfer recipient
            recipient = client.create_transfer_recipient(
                name=account_info.account_name,
                account_number=recipient_account_number,
                bank_code=recipient_bank_code,
                currency=resolved_currency_code,
                description=f"Expense reimbursement for {employee.full_name}",
                metadata=intent_metadata,
            )

        # Store verified account name on claim for audit trail
        claim.recipient_account_name = account_info.account_name
        self.db.flush()
        should_commit = True

        # Create payment intent
        intent = PaymentIntent(
            intent_id=uuid4(),
            organization_id=self.organization_id,
            paystack_reference=reference,
            amount=claim.net_payable_amount,
            currency_code=resolved_currency_code,
            email=email,
            direction=PaymentDirection.OUTBOUND,
            bank_account_id=bank_account_uuid,
            source_type="EXPENSE_CLAIM",
            source_id=claim_id,
            transfer_recipient_code=recipient.recipient_code,
            recipient_bank_code=recipient_bank_code,
            recipient_account_number=recipient_account_number,
            recipient_account_name=account_info.account_name,
            status=PaymentIntentStatus.PENDING,
            intent_metadata=intent_metadata,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        self.db.add(intent)
        self.db.flush()
        should_commit = True

        logger.info(
            f"Created expense payment intent {intent.intent_id} for claim {claim.claim_number}",
            extra={
                "intent_id": str(intent.intent_id),
                "claim_id": str(claim_id),
                "amount": str(claim.net_payable_amount),
                "reference": reference,
                "recipient_code": recipient.recipient_code,
            },
        )

        if should_commit:
            self._commit_and_refresh(intent)
        return intent

    def initiate_expense_transfer(
        self,
        intent: PaymentIntent,
        paystack_config: PaystackConfig,
    ) -> PaymentIntent:
        """
        Initiate the actual Paystack transfer for an expense reimbursement.

        This is called after the payment intent is created and approved.

        Args:
            intent: The payment intent with transfer details
            paystack_config: Paystack credentials

        Returns:
            Updated PaymentIntent with transfer_code

        Raises:
            HTTPException: If transfer initiation fails
        """
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus

        if intent.direction != PaymentDirection.OUTBOUND:
            raise HTTPException(
                status_code=400,
                detail="Can only initiate transfer for OUTBOUND payments",
            )

        if intent.status != PaymentIntentStatus.PENDING:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot initiate transfer with status '{intent.status.value}'",
            )

        if not intent.transfer_recipient_code:
            raise HTTPException(
                status_code=400,
                detail="Transfer recipient code is missing",
            )

        # Check intent expiration
        if intent.expires_at and intent.expires_at <= datetime.now(UTC):
            intent.status = PaymentIntentStatus.EXPIRED
            self.db.flush()
            self._commit_and_refresh(intent)
            raise HTTPException(
                status_code=400,
                detail="Payment intent has expired. Please create a new one.",
            )

        # Lock the expense claim to prevent concurrent modifications
        # This prevents race conditions where claim is cancelled while transfer is in progress
        if intent.source_type == "EXPENSE_CLAIM" and intent.source_id:
            locked_claim = self.db.scalar(
                select(ExpenseClaim)
                .where(ExpenseClaim.claim_id == intent.source_id)
                .with_for_update(nowait=False)
            )
            if not locked_claim:
                raise HTTPException(
                    status_code=404,
                    detail="Expense claim not found",
                )
            if locked_claim.status != ExpenseClaimStatus.APPROVED:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot initiate transfer - claim status is '{locked_claim.status.value}'",
                )

        # Amount in kobo - use round to avoid truncation
        amount_kobo = int(
            (Decimal(intent.amount) * Decimal("100")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

        with PaystackClient(paystack_config) as client:
            try:
                result = client.initiate_transfer(
                    amount=amount_kobo,
                    recipient_code=intent.transfer_recipient_code,
                    reference=intent.paystack_reference,
                    reason=f"Expense reimbursement: {(intent.intent_metadata or {}).get('claim_number', '')}",
                    currency=intent.currency_code,
                )
            except PaystackError as exc:
                result = self._recover_transfer_initiation(
                    intent=intent,
                    client=client,
                    error=exc,
                )
                if result is None:
                    # None means one thing only: Paystack ANSWERED and refused,
                    # so the money did not move and the intent stays PENDING and
                    # retryable. The unobserved case never reaches here — the
                    # recovery helper settles it INDETERMINATE and raises
                    # TransferOutcomeUnknown out of this block.
                    raise

        # Update intent with transfer code
        intent.transfer_code = result.transfer_code

        # Check immediate status from Paystack response
        # Some transfers complete instantly, no need to wait for webhook
        if result.status == "success":
            logger.info(
                f"Transfer {result.transfer_code} completed immediately for intent {intent.intent_id}",
                extra={
                    "intent_id": str(intent.intent_id),
                    "transfer_code": result.transfer_code,
                    "status": result.status,
                },
            )
            # Process as successful immediately
            intent.status = (
                PaymentIntentStatus.PROCESSING
            )  # Set first for the lock check
            self.db.flush()
            self.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={
                    "immediate": True,
                    "transfer_code": result.transfer_code,
                    "status": result.status,
                    "amount": result.amount,
                    "currency": result.currency,
                },
                fee_kobo=None,  # Fee comes in webhook or verify
            )
        elif result.status == "failed":
            logger.warning(
                f"Transfer {result.transfer_code} failed immediately for intent {intent.intent_id}",
                extra={
                    "intent_id": str(intent.intent_id),
                    "transfer_code": result.transfer_code,
                    "status": result.status,
                },
            )
            intent.status = PaymentIntentStatus.FAILED
            intent.gateway_response = {
                "immediate": True,
                "transfer_code": result.transfer_code,
                "status": result.status,
            }
            self.db.flush()
        else:
            # Status is "pending" or other - wait for webhook
            intent.status = PaymentIntentStatus.PROCESSING
            self.db.flush()
            logger.info(
                f"Initiated transfer {result.transfer_code} for intent {intent.intent_id} (status: {result.status})",
                extra={
                    "intent_id": str(intent.intent_id),
                    "transfer_code": result.transfer_code,
                    "amount": str(intent.amount),
                    "status": result.status,
                },
            )

        # CRITICAL: Commit after Paystack transfer is initiated.  The money
        # has already left (or is in-flight), so DB must reflect transfer_code
        # and updated status.  Without this commit the session closes without
        # persisting, leaving the intent PENDING with no transfer_code — which
        # causes webhooks to be rejected and the polling task to miss it.
        self._commit_and_refresh(intent)

        return intent

    def _recover_transfer_initiation(
        self,
        *,
        intent: PaymentIntent,
        client: PaystackClient,
        error: PaystackError,
    ) -> Any | None:
        """Recover an ambiguous initiate failure, or settle it as unresolved.

        Three outcomes, and the caller depends on the difference:

        * a ``VerifyTransferResponse`` — Paystack knows the reference and told
          us its state, so initiation did happen and the caller proceeds;
        * ``None`` — Paystack ANSWERED that nothing started. The caller
          re-raises the refusal and the intent stays PENDING and retryable;
        * ``TransferOutcomeUnknown`` raised — nobody could tell us what
          happened. The intent is INDETERMINATE and committed before this
          leaves; the caller must not retry (ADR-0007).

        The ambiguity test used to be two hard-coded strings, which is why a
        genuine connect timeout — the commonest ambiguous initiate there is —
        fell straight through to "return None" and left the row PENDING. It now
        keys on the exception TYPE (``is_unobserved``) and keeps the string
        matches for the cases where Paystack answered with an ambiguous BODY.
        """
        error_message = str(error)
        unobserved_attempt = is_unobserved(error)
        is_timeout = "Request timed out" in error_message
        is_duplicate_reference = "duplicate_transfer_reference" in error_message or (
            "Reference already exists on a transfer" in error_message
        )

        if not (unobserved_attempt or is_timeout or is_duplicate_reference):
            return None

        try:
            verified = client.verify_transfer(intent.paystack_reference)
        except PaystackError as verify_error:
            logger.warning(
                "Failed to recover transfer initiation for intent %s after error: %s",
                intent.intent_id,
                error_message,
            )
            # We asked twice. Whether "no verdict" is now permissible depends
            # on what the two answers were.
            #
            # If the FIRST attempt was answered (Paystack refused it, and only
            # a duplicate-reference string brought us here) and verification is
            # also answered — Paystack has no transfer under this reference —
            # then the two agree that nothing started. Retryable.
            #
            # Otherwise at least one leg told us nothing, and a "not found" on
            # a reference we may have created milliseconds ago is not evidence
            # of absence. Unobserved.
            if unobserved_attempt or is_unobserved(verify_error):
                raise self._record_unobserved_initiation(
                    intent=intent, error=error
                ) from verify_error
            return None

        logger.info(
            "Recovered transfer initiation for intent %s via verify_transfer after error: %s",
            intent.intent_id,
            error_message,
            extra={
                "intent_id": str(intent.intent_id),
                "transfer_code": verified.transfer_code,
                "verified_status": verified.status,
            },
        )
        return verified

    def _record_unobserved_initiation(
        self,
        *,
        intent: PaymentIntent,
        error: BaseException,
    ) -> TransferOutcomeUnknown:
        """Record an initiation whose outcome was never observed, and commit.

        Returns the exception for the caller to raise, rather than raising it,
        so the call site reads ``raise self._record_unobserved_initiation(...)``
        — one statement that visibly both writes and aborts.

        The commit is not optional. This runs on a path that is about to raise
        out of the request, and without it the session unwinds and the only
        record that a payout may be in flight is a log line.
        """
        message = str(error)
        self._settle_indeterminate(
            intent,
            reason=message,
            stage="initiation",
            marker="unobserved_initiation",
        )
        self._commit_and_refresh(intent)
        return TransferOutcomeUnknown(intent.intent_id, message)

    def _settle_indeterminate(
        self,
        intent: PaymentIntent,
        *,
        reason: str,
        stage: str,
        marker: str,
    ) -> None:
        """Record that this transfer's outcome could not be observed.

        The one place INDETERMINATE is written, so the three ways of arriving
        at it — an initiation nobody could confirm, a poll budget spent without
        an answer, a provider status this system cannot parse — cannot drift
        into three different records of the same fact.

        ``unresolved_since`` is set ONCE. Re-stamping it on every failed
        re-check would reset the clock the operator alert is measured against,
        and a transfer nobody can account for would never age.
        """
        already_unresolved = intent.status == PaymentIntentStatus.INDETERMINATE
        intent.status = PaymentIntentStatus.INDETERMINATE
        intent.unresolved_since = intent.unresolved_since or datetime.now(UTC)
        intent.last_poll_error = reason
        unresolved_since = intent.unresolved_since
        intent.gateway_response = {
            **(intent.gateway_response or {}),
            marker: True,
            "outcome_observed": False,
            "unresolved_since": unresolved_since.isoformat()
            if unresolved_since
            else None,
            "last_error": reason,
        }
        self.db.flush()
        self._hold_batch_item_unresolved(intent, reason=reason)

        if not already_unresolved:
            observe_transfer_unresolved(stage)

        logger.error(
            "Transfer intent %s is UNRESOLVED at %s - no outcome was observed "
            "and the payout may have moved money: %s",
            intent.intent_id,
            stage,
            reason,
            extra={
                "intent_id": str(intent.intent_id),
                "reference": intent.paystack_reference,
                "organization_id": str(self.organization_id),
                "stage": stage,
                "unresolved_since": unresolved_since.isoformat()
                if unresolved_since
                else None,
                "error": reason,
            },
        )

    def _record_unrecognised_transfer_status(
        self,
        intent: PaymentIntent,
        result: Any,
    ) -> None:
        """Paystack answered with a transfer status this system cannot read.

        Not treated as an error of Paystack's and not retried on the fast loop:
        asking again returns the same word. It is an observation we cannot
        interpret, which is a non-observation, and the slow reconciler will keep
        asking until the word becomes one we know or an operator decides.
        """
        reason = f"Unrecognised Paystack transfer status: {result.status!r}"
        self._settle_indeterminate(
            intent,
            reason=reason,
            stage="unrecognised_status",
            marker="unrecognised_transfer_status",
        )

    def build_transfer_result(self, intent: PaymentIntent) -> dict[str, Any]:
        """Build transfer result with claim status and user-facing message.

        Args:
            intent: The updated payment intent after transfer initiation.

        Returns:
            Dict with completed_immediately, claim_status, and message keys.
        """
        from app.models.expense.expense_claim import ExpenseClaim

        claim_status: str | None = None
        if intent.source_type == "EXPENSE_CLAIM" and intent.source_id:
            claim = self.db.get(ExpenseClaim, intent.source_id)
            if claim:
                claim_status = claim.status.value

        completed_immediately = intent.status == PaymentIntentStatus.COMPLETED
        if completed_immediately:
            message = "Transfer completed successfully! The expense claim has been marked as paid."
        elif intent.status == PaymentIntentStatus.FAILED:
            message = "Transfer failed. Please check the error and try again."
        elif intent.status == PaymentIntentStatus.INDETERMINATE:
            # Deliberately not "failed, try again": the money may have moved.
            # The one thing the operator must not do here is retry.
            message = (
                "Transfer outcome is UNKNOWN. Paystack did not confirm what "
                "happened, so this payout may or may not have gone out. Do "
                "NOT retry it — it is being reconciled automatically, and the "
                "claim stays approved and unpaid until a real outcome is "
                "obtained."
            )
        else:
            message = "Transfer initiated and is being processed. You will be notified when complete."

        return {
            "completed_immediately": completed_immediately,
            "claim_status": claim_status,
            "message": message,
        }

    def process_successful_transfer(
        self,
        intent: PaymentIntent,
        completed_at: datetime,
        gateway_response: dict[str, Any],
        fee_kobo: int | None = None,
    ) -> None:
        """
        Process a successful transfer (expense reimbursement).

        Updates the expense claim status to PAID, posts to GL, and records fees.

        Args:
            intent: The payment intent
            completed_at: When transfer completed
            gateway_response: Full Paystack response
            fee_kobo: Transfer fee in kobo (smallest currency unit)
        """

        from app.models.expense.expense_claim import ExpenseClaim

        # Re-fetch intent with row-level lock to prevent race conditions
        # between webhook and manual polling
        locked_intent = self.db.execute(
            select(PaymentIntent)
            .where(PaymentIntent.intent_id == intent.intent_id)
            .with_for_update(nowait=False)
        ).scalar_one_or_none()

        if not locked_intent:
            logger.warning(
                "Transfer intent %s not found during processing",
                intent.intent_id,
            )
            raise HTTPException(status_code=404, detail="Transfer intent not found")

        # Check if already processed (using locked row)
        if locked_intent.status == PaymentIntentStatus.COMPLETED:
            logger.info(f"Transfer intent {locked_intent.intent_id} already completed")
            return

        # Accept PROCESSING (normal), PENDING (defensive: webhook arrived
        # before the initiate route committed, or commit was lost) and
        # INDETERMINATE (the outcome was previously unobservable and has now
        # been observed — resolving one is the entire point of recording it).
        # Once Paystack confirms success we must honour it regardless.
        if locked_intent.status not in (
            PaymentIntentStatus.PROCESSING,
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.INDETERMINATE,
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot complete transfer with status '{locked_intent.status.value}'",
            )

        # Use locked_intent from here on
        intent = locked_intent

        # Update expense claim status.
        # mark_paid() must not prevent intent completion — the money has
        # already left the Paystack balance.  If it fails (e.g. budget
        # exhaustion race), log the error so it can be resolved manually
        # but still mark the intent COMPLETED below.
        claim = None
        if intent.source_type == "EXPENSE_CLAIM" and intent.source_id:
            claim = self.db.get(ExpenseClaim, intent.source_id)
            if claim:
                try:
                    from app.services.expense.expense_service import ExpenseService

                    claim = ExpenseService(self.db).mark_paid(
                        self.organization_id,
                        claim.claim_id,
                        payment_reference=intent.paystack_reference,
                        payment_date=completed_at.date(),
                        send_notification=False,
                        skip_budget_check=True,
                    )
                except Exception:
                    logger.exception(
                        "Failed to mark claim %s as PAID after successful "
                        "transfer %s — requires manual resolution",
                        intent.source_id,
                        intent.intent_id,
                    )
            else:
                logger.warning(
                    "Expense claim not found for transfer intent %s. "
                    "source_id=%s. Payment marked complete but claim not updated.",
                    intent.intent_id,
                    intent.source_id,
                )

        # Store fee amount (convert from kobo to Naira)
        fee_amount = None
        if fee_kobo and fee_kobo > 0:
            fee_amount = Decimal(fee_kobo) / Decimal("100")
            intent.fee_amount = fee_amount

        # Update intent
        intent.status = PaymentIntentStatus.COMPLETED
        intent.paid_at = completed_at
        intent.gateway_response = gateway_response

        self.db.flush()

        # Update batch item if this transfer is part of a batch
        self._update_batch_item_status(
            intent=intent,
            status=TransferBatchItemStatus.COMPLETED,
            completed_at=completed_at,
            fee_amount=fee_amount,
        )

        # Auto-post reimbursement to GL if bank account is configured
        system_user_id = None
        if claim and intent.bank_account_id:
            try:
                from app.services.expense.expense_posting_adapter import (
                    ExpensePostingAdapter,
                )

                # Get a system user ID for posting
                system_user_id = claim.created_by_id
                if not system_user_id:
                    logger.warning(
                        "Expense reimbursement not posted - missing user ID",
                        extra={"claim_id": str(claim.claim_id)},
                    )
                else:
                    posting_result = ExpensePostingAdapter.post_expense_reimbursement(
                        db=self.db,
                        organization_id=self.organization_id,
                        claim_id=claim.claim_id,
                        posting_date=completed_at.date(),
                        posted_by_user_id=system_user_id,
                        bank_account_id=intent.bank_account_id,
                        payment_reference=intent.paystack_reference,
                        correlation_id=str(intent.intent_id),
                    )

                    if posting_result.success:
                        logger.info(
                            f"Auto-posted expense reimbursement {claim.claim_number} to GL",
                            extra={
                                "claim_id": str(claim.claim_id),
                                "journal_entry_id": str(
                                    posting_result.journal_entry_id
                                ),
                            },
                        )
                    else:
                        logger.warning(
                            f"Failed to auto-post expense reimbursement: {posting_result.message}",
                            extra={"claim_id": str(claim.claim_id)},
                        )
            except Exception as post_error:
                # Log but don't fail - payment is still recorded
                logger.warning(
                    f"Failed to auto-post reimbursement for claim {claim.claim_id}: {post_error}",
                    extra={"claim_id": str(claim.claim_id), "error": str(post_error)},
                )
        elif claim and not intent.bank_account_id:
            logger.info(
                f"Expense reimbursement {claim.claim_number} not posted to GL - "
                "no transfer bank account configured in payment settings",
            )

        # Post transfer fee to GL if fee account is configured
        if fee_amount and fee_amount > Decimal("0") and intent.bank_account_id:
            self._post_transfer_fee(
                intent=intent,
                fee_amount=fee_amount,
                posting_date=completed_at.date(),
                system_user_id=system_user_id
                or (claim.created_by_id if claim else None),
            )

        logger.info(
            f"Processed successful transfer for intent {intent.intent_id}",
            extra={
                "intent_id": str(intent.intent_id),
                "source_type": intent.source_type,
                "source_id": str(intent.source_id) if intent.source_id else None,
                "fee_amount": str(fee_amount) if fee_amount else None,
            },
        )

    def _post_transfer_fee(
        self,
        intent: PaymentIntent,
        fee_amount: Decimal,
        posting_date,
        system_user_id: UUID | None,
    ) -> None:
        """
        Post transfer fee to GL if fee account is configured.

        Args:
            intent: Payment intent with fee details
            fee_amount: Fee amount in currency units
            posting_date: Date for posting
            system_user_id: User ID for audit trail
        """
        # Get fee expense account from settings
        fee_account_id = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_transfer_fee_account_id",
            organization_id=self.organization_id,
        )

        if not fee_account_id:
            logger.debug(
                "Transfer fee not posted - no fee account configured",
                extra={"intent_id": str(intent.intent_id), "fee": str(fee_amount)},
            )
            return

        if not system_user_id:
            logger.warning(
                "Transfer fee not posted - no user ID available",
                extra={"intent_id": str(intent.intent_id)},
            )
            return

        try:
            from app.services.expense.expense_posting_adapter import (
                ExpensePostingAdapter,
            )

            fee_account_uuid = coerce_uuid(fee_account_id)

            if intent.bank_account_id is None:
                logger.warning(
                    "Transfer fee not posted - missing bank account",
                    extra={"intent_id": str(intent.intent_id)},
                )
                return

            fee_result = ExpensePostingAdapter.post_transfer_fee(
                db=self.db,
                organization_id=self.organization_id,
                posting_date=posting_date,
                posted_by_user_id=system_user_id,
                fee_amount=fee_amount,
                bank_account_id=intent.bank_account_id,
                fee_expense_account_id=fee_account_uuid,
                reference=intent.paystack_reference,
                description=f"Paystack transfer fee: {intent.paystack_reference}",
                correlation_id=str(intent.intent_id),
            )

            if fee_result.success:
                intent.fee_journal_id = fee_result.journal_entry_id
                logger.info(
                    "Posted transfer fee to GL",
                    extra={
                        "intent_id": str(intent.intent_id),
                        "fee": str(fee_amount),
                        "journal_id": str(fee_result.journal_entry_id),
                    },
                )
            else:
                logger.warning(
                    f"Failed to post transfer fee: {fee_result.message}",
                    extra={"intent_id": str(intent.intent_id), "fee": str(fee_amount)},
                )

        except Exception as e:
            logger.warning(
                f"Error posting transfer fee: {e}",
                extra={"intent_id": str(intent.intent_id), "error": str(e)},
            )

    def _update_batch_item_status(
        self,
        intent: PaymentIntent,
        status: TransferBatchItemStatus,
        completed_at: datetime | None = None,
        fee_amount: Decimal | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Update batch item status if the intent is part of a batch.

        Also updates batch totals when items complete or fail.

        Args:
            intent: The payment intent
            status: New status for the batch item
            completed_at: When the transfer completed (for COMPLETED status)
            fee_amount: Transfer fee (for COMPLETED status)
            error_message: Error description (for FAILED status)
        """
        # Find batch item by payment intent
        batch_item = self.db.scalar(
            select(TransferBatchItem).where(
                TransferBatchItem.payment_intent_id == intent.intent_id
            )
        )

        if not batch_item:
            # Intent is not part of a batch
            return

        # Update batch item
        batch_item.status = status
        if completed_at:
            batch_item.completed_at = completed_at
        if fee_amount:
            batch_item.fee_amount = fee_amount
        if error_message:
            batch_item.error_message = error_message[:500] if error_message else None

        # Update batch totals
        batch = batch_item.batch
        if batch:
            batch.update_totals()

            # Update batch status based on item completion
            all_items = batch.items
            total = len(all_items)
            completed = batch.completed_count
            failed = batch.failed_count

            if completed + failed == total:
                # All items are finalized
                if failed == 0:
                    batch.status = TransferBatchStatus.COMPLETED
                elif completed == 0:
                    batch.status = TransferBatchStatus.FAILED
                else:
                    batch.status = TransferBatchStatus.PARTIALLY_COMPLETED

        self.db.flush()

        logger.info(
            f"Updated batch item for intent {intent.intent_id} to {status.value}",
            extra={
                "intent_id": str(intent.intent_id),
                "batch_item_id": str(batch_item.item_id),
                "batch_id": str(batch_item.batch_id),
            },
        )

    def _hold_batch_item_unresolved(
        self,
        intent: PaymentIntent,
        *,
        reason: str,
    ) -> None:
        """Note an unresolved outcome on a batch item WITHOUT settling it.

        `TransferBatchItemStatus` has four members and every one of them is a
        claim: PENDING, PROCESSING, COMPLETED, FAILED. There is no member for
        "unknown", and writing FAILED here would repeat the exact conflation
        this change exists to remove — one level up, where it would also
        finalize the parent batch, because `_update_batch_item_status` rolls a
        batch to COMPLETED/FAILED/PARTIALLY_COMPLETED as soon as
        ``completed + failed == total``.

        So the item's STATUS is deliberately left where it is. That keeps the
        batch un-finalized, which is true: a batch containing a payout nobody
        can account for is not finished. The reason is recorded on the item so
        the situation is visible rather than merely absent.

        No new enum member and no migration: the batch tables survive only as
        payout history (ADR-0005 §4), nothing creates batches any more, and
        adding a fifth status to a dormant vocabulary would be inventing a
        contract for a writer that does not exist.
        """
        batch_item = self.db.scalar(
            select(TransferBatchItem).where(
                TransferBatchItem.payment_intent_id == intent.intent_id
            )
        )
        if not batch_item:
            return

        batch_item.error_message = f"UNRESOLVED: {reason}"[:500]
        self.db.flush()
        logger.warning(
            "Batch item for intent %s held UNRESOLVED - its batch cannot be "
            "finalized until the payout outcome is known",
            intent.intent_id,
            extra={
                "intent_id": str(intent.intent_id),
                "batch_item_id": str(batch_item.item_id),
                "batch_id": str(batch_item.batch_id),
            },
        )

    def mark_transfer_failed(
        self,
        intent: PaymentIntent,
        error_message: str,
        gateway_response: dict[str, Any] | None = None,
    ) -> None:
        """
        Mark a transfer intent as failed.

        Also reverts the expense claim status back to APPROVED if needed.

        Args:
            intent: The payment intent
            error_message: Error description
            gateway_response: Optional Paystack response
        """
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus

        intent.status = PaymentIntentStatus.FAILED
        intent.gateway_response = {
            "error": error_message,
            **(gateway_response or {}),
        }

        # Revert expense claim status if it was somehow marked PAID
        if intent.source_type == "EXPENSE_CLAIM" and intent.source_id:
            claim = self.db.get(ExpenseClaim, intent.source_id)
            if claim and claim.status == ExpenseClaimStatus.PAID:
                claim.status = ExpenseClaimStatus.APPROVED
                claim.paid_on = None
                claim.payment_reference = None
                logger.info(
                    f"Reverted claim {claim.claim_number} to APPROVED due to failed transfer"
                )

        self.db.flush()

        # Update batch item if this transfer is part of a batch
        self._update_batch_item_status(
            intent=intent,
            status=TransferBatchItemStatus.FAILED,
            error_message=error_message,
        )

        logger.warning(
            f"Transfer intent {intent.intent_id} failed: {error_message}",
            extra={
                "intent_id": str(intent.intent_id),
                "error": error_message,
            },
        )

    def poll_transfer_status(
        self,
        intent: PaymentIntent,
        paystack_config: PaystackConfig,
    ) -> PaymentIntent:
        """
        Poll Paystack for transfer status (fallback for missed webhooks).

        Use this to check status of transfers stuck in PROCESSING state.

        Args:
            intent: The payment intent with transfer_code
            paystack_config: Paystack credentials

        Returns:
            Updated PaymentIntent
        """
        if intent.direction != PaymentDirection.OUTBOUND:
            raise ValueError("Can only poll transfer status for OUTBOUND payments")

        if intent.status not in (
            PaymentIntentStatus.PROCESSING,
            PaymentIntentStatus.INDETERMINATE,
        ):
            logger.debug(f"Intent {intent.intent_id} not in a pollable state")
            return intent

        # Paystack's /transfer/verify/{reference} looks up by the merchant's
        # reference, so the REFERENCE is what is actually required here.
        if not intent.paystack_reference:
            logger.warning(f"Intent {intent.intent_id} has no paystack_reference")
            return intent

        # A missing transfer_code still means something went wrong for a
        # PROCESSING intent, and it is left alone. For an INDETERMINATE one it
        # is EXPECTED — the initiate call never came back with a code, which is
        # precisely why nobody knows what happened — and refusing to verify
        # would leave the intents that most need asking about permanently
        # unasked (ADR-0007).
        if (
            not intent.transfer_code
            and intent.status != PaymentIntentStatus.INDETERMINATE
        ):
            logger.warning(f"Intent {intent.intent_id} has no transfer_code")
            return intent

        with PaystackClient(paystack_config) as client:
            result = client.verify_transfer(intent.paystack_reference)

        if result.status == "success":
            self.process_successful_transfer(
                intent,
                completed_at=datetime.now(UTC),
                gateway_response={"polled": True, "transfer_status": result.status},
                fee_kobo=result.fee,
            )
        elif result.status == "failed":
            self.mark_transfer_failed(
                intent,
                error_message=f"Transfer failed: {result.reason or 'Unknown'}",
                gateway_response={"polled": True, "transfer_status": result.status},
            )
        elif result.status == "reversed":
            self.process_transfer_reversal(
                intent,
                reversed_at=datetime.now(UTC),
                gateway_response={"polled": True, "transfer_status": result.status},
                reason=result.reason,
            )
        elif result.status in _PAYSTACK_IN_FLIGHT_STATUSES:
            logger.info(
                f"Transfer {intent.transfer_code} still pending: {result.status}",
            )
        else:
            # An unrecognised status is not "still pending". Treating it as one
            # is how a transfer Paystack has already settled sits in PROCESSING
            # for twenty minutes and then gets stamped FAILED by the circuit
            # breaker — a verdict manufactured out of a word we did not parse.
            # We did not observe an outcome, so we say so (ADR-0007).
            self._record_unrecognised_transfer_status(intent, result)

        return intent

    # ------------------------------------------------------------------
    # Scheduled reconciliation of in-flight transfers
    #
    # `app.tasks.expense.poll_stuck_expense_transfers` used to select these
    # intents, promote PENDING to PROCESSING, count the attempts and stamp
    # FAILED itself — a second writer of `PaymentIntent.status` with no tests
    # around it. The decisions live here now; the worker only supplies the
    # sessions and aggregates what it is told.
    #
    # Those sessions are TENANT sessions. The selections below first ran under
    # `cross_org_session`, which lifts only the SQLAlchemy listener and never
    # PostgreSQL RLS — so under `app_user` they matched nothing and the poller
    # reported a clean run forever. See ADR-0005's 2026-08-28 amendment.
    # ------------------------------------------------------------------

    @staticmethod
    def find_stale_pending_transfer_intents(
        db: Session,
        *,
        now: datetime | None = None,
    ) -> dict[UUID, list[UUID]]:
        """Outbound expense intents that expired before a transfer was started.

        Selection only — the caller runs this inside each tenant's own session
        (``app.tasks.expense.poll_stuck_expense_transfers`` fans out over
        ``for_each_organization``), and every decision about a selected row is
        re-taken, and re-proved under a lock, by
        :meth:`expire_stale_pending_transfer`. The re-proof is not redundant
        just because the selection now shares the session: a transfer initiated
        between the two has already moved the row.

        There is deliberately no ``organization_id`` predicate: the session is
        the organization, and adding one here would make this method look
        usable on an unscoped session. Returns organization id -> intent ids —
        at most one key inside a tenant session, which is what lets the caller
        read the mapping back by the organization it is holding and treat
        anything else as not its to touch.
        """
        moment = now or datetime.now(UTC)
        rows = db.execute(
            select(PaymentIntent.intent_id, PaymentIntent.organization_id).where(
                PaymentIntent.direction == PaymentDirection.OUTBOUND,
                PaymentIntent.status == PaymentIntentStatus.PENDING,
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.transfer_code.is_(None),
                PaymentIntent.expires_at.isnot(None),
                PaymentIntent.expires_at <= moment,
            )
        ).all()
        return PaymentService._group_by_organization(rows)

    @staticmethod
    def find_stuck_transfer_intents(
        db: Session,
        *,
        now: datetime | None = None,
        min_age: timedelta = TRANSFER_POLL_MIN_AGE,
        max_poll_attempts: int = TRANSFER_POLL_MAX_ATTEMPTS,
    ) -> dict[UUID, list[UUID]]:
        """Outbound expense transfers that have a transfer code but no verdict.

        Includes PENDING rows that already carry a ``transfer_code``: the money
        may have left even though the row never reached PROCESSING, so they are
        reconciled rather than assumed dead.

        Selection only, run inside the tenant's own session; see
        :meth:`find_stale_pending_transfer_intents`.

        Returns organization id -> intent ids.
        """
        cutoff = (now or datetime.now(UTC)) - min_age
        rows = db.execute(
            select(PaymentIntent.intent_id, PaymentIntent.organization_id).where(
                PaymentIntent.direction == PaymentDirection.OUTBOUND,
                PaymentIntent.status.in_(
                    [
                        PaymentIntentStatus.PROCESSING,
                        PaymentIntentStatus.PENDING,
                    ]
                ),
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.transfer_code.isnot(None),
                PaymentIntent.created_at < cutoff,
                PaymentIntent.poll_count < max_poll_attempts,
            )
        ).all()
        return PaymentService._group_by_organization(rows)

    @staticmethod
    def find_indeterminate_transfer_intents(
        db: Session,
        *,
        organization_id: UUID,
    ) -> list[UUID]:
        """Outbound expense transfers whose outcome was never observed.

        The slow lane. `find_stuck_transfer_intents` deliberately does not see
        these — its predicate is PENDING/PROCESSING and it carries an attempt
        cap, so an INDETERMINATE intent drops out of the two-minute loop by
        construction rather than by remembering to exclude it. This selector
        replaces the cap with a separate, uncapped predicate: an unresolved
        payout is asked about for as long as it stays unresolved, because no
        quantity of elapsed time converts "we do not know" into a verdict.

        Selection only; every decision is re-taken and re-proved under a lock in
        :meth:`resolve_indeterminate_transfer`. The organization is mandatory:
        the scheduler discovers tenant identifiers through the narrow tenant
        catalogue and performs this read inside that tenant's own RLS session.
        """
        return list(
            db.scalars(
                select(PaymentIntent.intent_id)
                .where(
                    PaymentIntent.organization_id == organization_id,
                    PaymentIntent.direction == PaymentDirection.OUTBOUND,
                    PaymentIntent.status == PaymentIntentStatus.INDETERMINATE,
                    PaymentIntent.source_type == "EXPENSE_CLAIM",
                )
                .order_by(PaymentIntent.unresolved_since.asc())
            ).all()
        )

    @staticmethod
    def oldest_unresolved_transfer_age(
        db: Session,
        *,
        organization_id: UUID,
        now: datetime | None = None,
    ) -> timedelta:
        """Age of this tenant's longest-unresolved payout, or zero.

        The caller aggregates the maximum across tenant-scoped reads. Keeping
        the organization argument mandatory prevents this metric helper from
        becoming a new cross-tenant read path.
        """
        moment = now or datetime.now(UTC)
        oldest = db.scalar(
            select(func.min(PaymentIntent.unresolved_since)).where(
                PaymentIntent.organization_id == organization_id,
                PaymentIntent.direction == PaymentDirection.OUTBOUND,
                PaymentIntent.status == PaymentIntentStatus.INDETERMINATE,
                PaymentIntent.unresolved_since.isnot(None),
            )
        )
        if oldest is None:
            return timedelta(0)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return max(moment - oldest, timedelta(0))

    @staticmethod
    def _group_by_organization(rows) -> dict[UUID, list[UUID]]:
        grouped: dict[UUID, list[UUID]] = {}
        for intent_id, organization_id in rows:
            grouped.setdefault(organization_id, []).append(intent_id)
        return grouped

    def resolve_transfer_polling_config(self) -> PaystackConfig | None:
        """Paystack credentials for this organization, or None if unconfigured.

        Deliberately distinct from the API layer's config builder: this one
        reads the separately stored ``paystack_webhook_secret`` and tolerates
        it being absent, because the polling path never verifies a signature.
        Preserved as it was in the worker rather than unified — unifying the
        webhook secret across paths is a signature-verification change and does
        not belong in a single-writer fix.
        """
        secret_key = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_secret_key",
            organization_id=self.organization_id,
        )
        public_key = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_public_key",
            organization_id=self.organization_id,
        )
        webhook_secret = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_webhook_secret",
            organization_id=self.organization_id,
        )
        if not secret_key or not public_key:
            return None
        return PaystackConfig(
            secret_key=str(secret_key),
            public_key=str(public_key),
            webhook_secret=str(webhook_secret or ""),
        )

    def _lock_transfer_intent(self, intent_id: UUID) -> PaymentIntent | None:
        """Load one of this organization's intents FOR UPDATE, refreshed.

        ``populate_existing`` matters as much as the lock. A worker holds one
        session across a whole tenant's batch and makes a network call per
        intent, so an intent loaded at the top of the batch can be minutes
        stale by the time it is acted on — and SQLAlchemy would hand back the
        identity-mapped copy, showing PENDING for a row a webhook has since
        moved to COMPLETED. Writing the worker's view back over that is how a
        settled transfer gets re-posted to the ledger.
        """
        return self.db.scalars(
            select(PaymentIntent)
            .where(
                PaymentIntent.intent_id == coerce_uuid(intent_id),
                PaymentIntent.organization_id == self.organization_id,
            )
            .with_for_update(nowait=False)
            .execution_options(populate_existing=True)
        ).one_or_none()

    @staticmethod
    def _is_stale_pending_transfer(intent: PaymentIntent, now: datetime) -> bool:
        """The premise :meth:`find_stale_pending_transfer_intents` selected on."""
        return (
            intent.direction == PaymentDirection.OUTBOUND
            and intent.status == PaymentIntentStatus.PENDING
            and intent.source_type == "EXPENSE_CLAIM"
            and intent.transfer_code is None
            and intent.expires_at is not None
            and intent.expires_at <= now
        )

    def expire_stale_pending_transfer(
        self,
        intent_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Expire one intent whose transfer was never initiated. True if expired.

        The premise is re-proved here, under a row lock, because it was
        established in a different session: a transfer initiated in that gap
        has already moved the row to PROCESSING and may have moved money.
        Stamping EXPIRED over that would hide an in-flight payout.
        """
        moment = now or datetime.now(UTC)
        intent = self._lock_transfer_intent(intent_id)
        if intent is None:
            logger.info(
                "Skipped expiring intent %s - not found in organization %s",
                intent_id,
                self.organization_id,
            )
            return False

        if not self._is_stale_pending_transfer(intent, moment):
            logger.info(
                "Skipped expiring intent %s - state moved (status=%s, transfer_code=%s)",
                intent.intent_id,
                intent.status.value,
                intent.transfer_code,
            )
            return False

        intent.status = PaymentIntentStatus.EXPIRED
        self.db.flush()
        logger.info(
            "Expired stale PENDING transfer intent %s (created %s)",
            intent.intent_id,
            intent.created_at,
        )
        return True

    def reconcile_stuck_transfer(
        self,
        intent_id: UUID,
        paystack_config: PaystackConfig,
        *,
        max_poll_attempts: int = TRANSFER_POLL_MAX_ATTEMPTS,
    ) -> TransferPollResult:
        """Ask Paystack what happened to one in-flight transfer and settle it.

        Owns every status decision the scheduled poller used to make inline:
        the promotion of a PENDING-but-initiated intent to PROCESSING, the
        attempt counting, and the circuit breaker that gives up.

        Never raises for a poll failure: the caller is a batch job and one
        unreachable transfer must not abandon the rest of the tenant's work.
        """
        intent = self._lock_transfer_intent(intent_id)
        if intent is None:
            return TransferPollResult(
                intent_id=coerce_uuid(intent_id),
                outcome=TransferPollOutcome.SKIPPED,
                poll_count=0,
                error="intent not found in this organization",
            )

        # Re-proved under the lock, for the reason in `_lock_transfer_intent`:
        # a webhook may have settled this intent since it was selected, and a
        # settled transfer is not the poller's to reopen.
        if intent.status not in (
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.PROCESSING,
        ):
            logger.info(
                "Skipped polling intent %s - already settled as %s",
                intent.intent_id,
                intent.status.value,
            )
            return TransferPollResult(
                intent_id=intent.intent_id,
                outcome=TransferPollOutcome.SKIPPED,
                poll_count=intent.poll_count,
                error=f"already settled as {intent.status.value}",
            )

        intent.poll_count += 1

        try:
            # An intent can hold a transfer_code while still PENDING when the
            # initiate call was ambiguous (timeout, duplicate reference).
            # poll_transfer_status only reconciles PROCESSING rows, so promote
            # it first: the money is in flight either way.
            if intent.status == PaymentIntentStatus.PENDING:
                intent.status = PaymentIntentStatus.PROCESSING
                self.db.flush()

            self.poll_transfer_status(intent, paystack_config)
        except Exception as exc:  # noqa: BLE001 - recorded, never re-raised
            return self._record_transfer_poll_failure(
                intent=intent,
                error=exc,
                max_poll_attempts=max_poll_attempts,
            )

        if intent.status == PaymentIntentStatus.COMPLETED:
            outcome = TransferPollOutcome.COMPLETED
        elif intent.status == PaymentIntentStatus.FAILED:
            outcome = TransferPollOutcome.FAILED
        elif intent.status == PaymentIntentStatus.REVERSED:
            outcome = TransferPollOutcome.REVERSED
        elif intent.status == PaymentIntentStatus.INDETERMINATE:
            # poll_transfer_status parsed a status it does not recognise.
            outcome = TransferPollOutcome.INDETERMINATE
        else:
            outcome = TransferPollOutcome.STILL_PENDING

        return TransferPollResult(
            intent_id=intent.intent_id,
            outcome=outcome,
            poll_count=intent.poll_count,
        )

    def resolve_transfer_unresolved_alert_threshold(self) -> timedelta:
        """How long a payout may stay unresolved before an operator is told.

        Config, not a constant: a deployment whose treasury desk works Lagos
        hours wants a different number from one that does not, and neither
        should have to patch code to say so. The spec's default (six hours) is
        the documented answer; the module constant is only the floor under a
        settings layer that cannot answer at all.
        """
        raw = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_transfer_unresolved_alert_hours",
            organization_id=self.organization_id,
        )
        try:
            hours = int(str(raw))
        except (TypeError, ValueError):
            hours = TRANSFER_UNRESOLVED_ALERT_HOURS_DEFAULT
        if hours < 1:
            hours = TRANSFER_UNRESOLVED_ALERT_HOURS_DEFAULT
        return timedelta(hours=hours)

    def resolve_indeterminate_transfer(
        self,
        intent_id: UUID,
        paystack_config: PaystackConfig,
        *,
        now: datetime | None = None,
    ) -> TransferPollResult:
        """Ask Paystack again about one payout whose outcome is unknown.

        **The only writer permitted to move an intent OUT of INDETERMINATE**,
        and it may only move it to a status Paystack itself justified —
        COMPLETED, FAILED or REVERSED — by delegating to the same three methods
        the webhook and the fast poller use. There is deliberately no "give up"
        branch and no attempt cap: a budget here would recreate the defect one
        level up, manufacturing a verdict out of repeated silence.

        Same shape as `reconcile_stuck_transfer` and for the same reason
        (ADR-0005 section 3): the premise was established by a selection in
        another session, so the row is taken by id, locked ``FOR UPDATE`` with
        ``populate_existing=True``, and re-proved to still be INDETERMINATE
        before anything is written. An intent a webhook resolved in the gap is
        reported, never overwritten.

        Never raises for a poll failure - the caller is a batch job.
        """
        moment = now or datetime.now(UTC)
        intent = self._lock_transfer_intent(intent_id)
        if intent is None:
            return TransferPollResult(
                intent_id=coerce_uuid(intent_id),
                outcome=TransferPollOutcome.SKIPPED,
                poll_count=0,
                error="intent not found in this organization",
            )

        if intent.status != PaymentIntentStatus.INDETERMINATE:
            logger.info(
                "Skipped resolving intent %s - no longer unresolved (status=%s)",
                intent.intent_id,
                intent.status.value,
            )
            return TransferPollResult(
                intent_id=intent.intent_id,
                outcome=TransferPollOutcome.SKIPPED,
                poll_count=intent.poll_count,
                error=f"already resolved as {intent.status.value}",
            )

        if not intent.paystack_reference:
            # Nothing to ask about. This should not be reachable - an intent
            # only becomes INDETERMINATE after a reference was sent - and it is
            # reported rather than settled, because "we have no way to ask" is
            # itself an unobserved outcome, not a verdict.
            return self._report_still_unresolved(
                intent, moment, "no reference to verify against"
            )

        intent.poll_count += 1

        try:
            self.poll_transfer_status(intent, paystack_config)
        except Exception as exc:  # noqa: BLE001 - recorded, never re-raised
            intent.last_poll_error = str(exc)
            self.db.flush()
            return self._report_still_unresolved(intent, moment, str(exc))

        if intent.status == PaymentIntentStatus.COMPLETED:
            outcome = TransferPollOutcome.COMPLETED
        elif intent.status == PaymentIntentStatus.FAILED:
            outcome = TransferPollOutcome.FAILED
        elif intent.status == PaymentIntentStatus.REVERSED:
            outcome = TransferPollOutcome.REVERSED
        else:
            # Still INDETERMINATE: Paystack was reached but said nothing this
            # system can turn into a verdict.
            return self._report_still_unresolved(
                intent, moment, intent.last_poll_error or "no verdict from Paystack"
            )

        # A resolved intent is no longer unresolved, and leaving the timestamp
        # behind would keep it in the alert query forever.
        intent.unresolved_since = None
        self.db.flush()
        logger.info(
            "Resolved previously unresolved transfer %s as %s after %d attempts",
            intent.intent_id,
            intent.status.value,
            intent.poll_count,
            extra={
                "intent_id": str(intent.intent_id),
                "resolved_as": intent.status.value,
            },
        )
        return TransferPollResult(
            intent_id=intent.intent_id,
            outcome=outcome,
            poll_count=intent.poll_count,
        )

    def _report_still_unresolved(
        self,
        intent: PaymentIntent,
        now: datetime,
        reason: str,
    ) -> TransferPollResult:
        """Still no verdict. Escalate once the intent is older than the threshold.

        Escalation is a log record at ERROR carrying the intent, the reference,
        the amount and how long it has been unknown - the fields an operator
        needs in order to go and look at Paystack themselves. It deliberately
        does not create a Notification: a notification needs a named recipient,
        and guessing one for a treasury exception would either spam every admin
        or silently reach nobody. The alerting rule keys on this log line and on
        ``payment_transfer_unresolved_oldest_age_seconds``.
        """
        threshold = self.resolve_transfer_unresolved_alert_threshold()
        unresolved_since = intent.unresolved_since
        if unresolved_since is not None and unresolved_since.tzinfo is None:
            unresolved_since = unresolved_since.replace(tzinfo=UTC)
        age = (now - unresolved_since) if unresolved_since else timedelta(0)

        if age >= threshold:
            logger.error(
                "ESCALATION: transfer %s has had an unknown outcome for %s "
                "(threshold %s). A human must confirm with Paystack whether "
                "%s %s left the account. Reference %s.",
                intent.intent_id,
                age,
                threshold,
                intent.currency_code,
                intent.amount,
                intent.paystack_reference,
                extra={
                    "intent_id": str(intent.intent_id),
                    "organization_id": str(self.organization_id),
                    "reference": intent.paystack_reference,
                    "amount": str(intent.amount),
                    "currency": intent.currency_code,
                    "unresolved_for_seconds": int(age.total_seconds()),
                    "threshold_seconds": int(threshold.total_seconds()),
                    "needs_operator": True,
                },
            )
        else:
            logger.warning(
                "Transfer %s still unresolved after %s: %s",
                intent.intent_id,
                age,
                reason,
            )

        return TransferPollResult(
            intent_id=intent.intent_id,
            outcome=TransferPollOutcome.STILL_UNRESOLVED,
            poll_count=intent.poll_count,
            error=reason,
        )

    def _record_transfer_poll_failure(
        self,
        *,
        intent: PaymentIntent,
        error: BaseException,
        max_poll_attempts: int,
    ) -> TransferPollResult:
        """Record a failed poll attempt, and stop the fast loop once spent.

        Spending the attempt budget ends the POLLING, and that is all it ends.
        What the intent becomes depends on what the last attempt actually
        learned, and there are only two possibilities:

        * **Paystack answered and refused** — a parsed ``status: false`` body,
          a 4xx. That is a verdict about this transfer, so FAILED is the honest
          record and the claim goes back to being payable.

        * **Nothing was learned** — a timeout, a connection error, a 5xx, an
          exception out of our own posting code. FAILED here would be a claim
          this system is not entitled to make: "we could not reach Paystack ten
          times" is not "Paystack told us this failed", and every downstream
          reader — the reset endpoint, the reimburse button, the operator
          reading a rose badge — acts on the difference. The intent becomes
          INDETERMINATE, keeps the money-may-have-moved semantics, and is
          handed to `resolve_indeterminate_transfer` (ADR-0007, adopting
          `dotmac_starter_mt` ADR-0032).

        The FAILED write stays here rather than going through
        `mark_transfer_failed`, unchanged from the single-writer fix: that
        method also reverts a PAID claim and replaces `gateway_response`.
        """
        error_message = str(error)
        intent.last_poll_error = error_message
        logger.error(
            "Failed to poll transfer %s (attempt %d/%d): %s",
            intent.intent_id,
            intent.poll_count,
            max_poll_attempts,
            error_message,
        )

        if intent.poll_count < max_poll_attempts:
            return TransferPollResult(
                intent_id=intent.intent_id,
                outcome=TransferPollOutcome.ERRORED,
                poll_count=intent.poll_count,
                error=error_message,
            )

        if is_unobserved(error):
            self._settle_indeterminate(
                intent,
                reason=error_message,
                stage="polling",
                marker="poll_abandoned_unobserved",
            )
            intent.gateway_response = {
                **(intent.gateway_response or {}),
                "poll_attempts": intent.poll_count,
            }
            self.db.flush()
            return TransferPollResult(
                intent_id=intent.intent_id,
                outcome=TransferPollOutcome.INDETERMINATE,
                poll_count=intent.poll_count,
                error=error_message,
            )

        intent.status = PaymentIntentStatus.FAILED
        intent.gateway_response = {
            **(intent.gateway_response or {}),
            "poll_abandoned": True,
            "poll_attempts": intent.poll_count,
            "last_error": error_message,
        }
        self.db.flush()
        logger.warning(
            "Transfer %s marked FAILED after %d poll attempts - Paystack "
            "answered and refused: %s",
            intent.intent_id,
            intent.poll_count,
            error_message,
        )
        return TransferPollResult(
            intent_id=intent.intent_id,
            outcome=TransferPollOutcome.ABANDONED,
            poll_count=intent.poll_count,
            error=error_message,
        )

    def process_transfer_reversal(
        self,
        intent: PaymentIntent,
        reversed_at: datetime,
        gateway_response: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        """
        Process a transfer reversal (funds returned).

        Updates status, reverts expense claim, and creates reversal journal entries.

        Args:
            intent: The payment intent
            reversed_at: When reversal occurred
            gateway_response: Full Paystack response
            reason: Reason for reversal
        """
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus

        # Check if already processed
        if intent.status == PaymentIntentStatus.REVERSED:
            logger.info(f"Transfer intent {intent.intent_id} already reversed")
            return

        # Can only reverse COMPLETED, PROCESSING or INDETERMINATE transfers.
        # INDETERMINATE belongs here for the same reason it belongs in
        # `process_successful_transfer`: a reversal notice IS the observation
        # that was missing, and it says the money moved and came back. Refusing
        # it would strand the intent unresolved forever.
        if intent.status not in [
            PaymentIntentStatus.COMPLETED,
            PaymentIntentStatus.PROCESSING,
            PaymentIntentStatus.INDETERMINATE,
        ]:
            logger.warning(
                f"Cannot reverse intent {intent.intent_id} with status '{intent.status.value}'"
            )
            return

        # Update intent status
        was_completed = intent.status == PaymentIntentStatus.COMPLETED
        intent.status = PaymentIntentStatus.REVERSED
        intent.gateway_response = {
            **(intent.gateway_response or {}),
            "reversal": gateway_response,
            "reversal_reason": reason,
            "reversed_at": reversed_at.isoformat(),
        }

        # Revert expense claim status back to APPROVED
        claim = None
        if intent.source_type == "EXPENSE_CLAIM" and intent.source_id:
            claim = self.db.get(ExpenseClaim, intent.source_id)
            if claim and claim.status == ExpenseClaimStatus.PAID:
                claim.status = ExpenseClaimStatus.APPROVED
                claim.paid_on = None
                claim.payment_reference = None
                # Clear reimbursement journal reference (will create reversal)
                # Note: We keep the original journal for audit trail

        self.db.flush()

        # Update batch item if this transfer is part of a batch
        # Reversals count as failures for batch tracking
        self._update_batch_item_status(
            intent=intent,
            status=TransferBatchItemStatus.FAILED,
            error_message=f"Transfer reversed: {reason or 'No reason provided'}",
        )

        # Create reversal journal entries if we had posted to GL
        if was_completed and intent.bank_account_id and claim:
            self._post_reversal_entries(intent, claim, reversed_at)

        logger.info(
            f"Processed transfer reversal for intent {intent.intent_id}",
            extra={
                "intent_id": str(intent.intent_id),
                "reason": reason,
                "was_completed": was_completed,
            },
        )

    def _post_reversal_entries(
        self,
        intent: PaymentIntent,
        claim,
        reversed_at: datetime,
    ) -> None:
        """
        Post reversal journal entries for a reversed transfer.

        Creates entries that reverse both the reimbursement and fee postings.
        """
        from app.services.expense.expense_posting_adapter import ExpensePostingAdapter

        system_user_id = claim.created_by_id
        if not system_user_id:
            logger.warning("Cannot post reversal entries - no user ID")
            return
        if intent.bank_account_id is None:
            logger.warning("Cannot post reversal entries - missing bank account")
            return

        # Reverse the reimbursement entry if it was posted
        if claim.reimbursement_journal_id:
            try:
                # Create a reversal entry (opposite of original)
                # Original was: Dr Employee Payable, Cr Bank
                # Reversal is: Dr Bank, Cr Employee Payable
                result = ExpensePostingAdapter.post_expense_reimbursement_reversal(
                    db=self.db,
                    organization_id=self.organization_id,
                    claim_id=claim.claim_id,
                    original_journal_id=claim.reimbursement_journal_id,
                    posting_date=reversed_at.date(),
                    posted_by_user_id=system_user_id,
                    bank_account_id=intent.bank_account_id,
                    reason=f"Transfer reversed: {intent.paystack_reference}",
                    correlation_id=str(intent.intent_id),
                )

                if result.success:
                    logger.info(
                        f"Posted reimbursement reversal for claim {claim.claim_number}",
                        extra={"journal_id": str(result.journal_entry_id)},
                    )
                else:
                    logger.warning(
                        f"Failed to post reimbursement reversal: {result.message}"
                    )

            except Exception as e:
                logger.warning(f"Error posting reversal: {e}")

        # Reverse the fee entry if it was posted
        if intent.fee_journal_id and intent.fee_amount:
            try:
                result = ExpensePostingAdapter.post_transfer_fee_reversal(
                    db=self.db,
                    organization_id=self.organization_id,
                    original_journal_id=intent.fee_journal_id,
                    posting_date=reversed_at.date(),
                    posted_by_user_id=system_user_id,
                    fee_amount=intent.fee_amount,
                    bank_account_id=intent.bank_account_id,
                    reference=intent.paystack_reference,
                    correlation_id=str(intent.intent_id),
                )

                if result.success:
                    logger.info(
                        f"Posted fee reversal for intent {intent.intent_id}",
                        extra={"journal_id": str(result.journal_entry_id)},
                    )
                else:
                    logger.warning(f"Failed to post fee reversal: {result.message}")

            except Exception as e:
                logger.warning(f"Error posting fee reversal: {e}")
