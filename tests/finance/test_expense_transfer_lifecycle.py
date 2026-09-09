"""
Tests for expense reimbursement transfer lifecycle.

Covers the full Paystack transfer flow for expense claims:
- Intent creation (step 1)
- Transfer initiation (step 2) — immediate success, pending, and failed paths
- Webhook processing (transfer.success, transfer.failed, transfer.reversed)
- Polling task for stuck transfers
- Edge cases: race conditions, idempotency, status gate logic
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.expense.expense_claim import ExpenseClaimStatus
from app.models.finance.payments.payment_intent import (
    PaymentDirection,
    PaymentIntentStatus,
)
from app.models.finance.payments.payment_webhook import WebhookStatus
from app.services.finance.payments.payment_service import (
    PaymentService,
    TransferOutcomeUnknown,
)
from app.services.finance.payments.paystack_client import (
    PaystackConfig,
    PaystackError,
    PaystackUnreachable,
)
from app.services.finance.payments.webhook_service import WebhookService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CFG = PaystackConfig(
    secret_key="sk_test", public_key="pk_test", webhook_secret="wh_test"
)


_SENTINEL: Any = object()


def _org_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_intent(
    *,
    org_id: uuid.UUID | None = None,
    status: PaymentIntentStatus = PaymentIntentStatus.PENDING,
    direction: PaymentDirection = PaymentDirection.OUTBOUND,
    transfer_code: str | None = None,
    amount: Decimal = Decimal("50000.00"),
    source_type: str = "EXPENSE_CLAIM",
    source_id: uuid.UUID | None = None,
    transfer_recipient_code: str = "RCP_test",
    bank_account_id: Any = _SENTINEL,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    poll_count: int = 0,
) -> Any:
    """Build a lightweight intent object for unit tests."""
    return SimpleNamespace(
        intent_id=uuid.uuid4(),
        organization_id=org_id or _org_id(),
        paystack_reference=f"EXP-CLM-{uuid.uuid4().hex[:8]}",
        amount=amount,
        currency_code="NGN",
        email="employee@example.com",
        direction=direction,
        bank_account_id=uuid.uuid4()
        if bank_account_id is _SENTINEL
        else bank_account_id,
        source_type=source_type,
        source_id=source_id or uuid.uuid4(),
        transfer_recipient_code=transfer_recipient_code,
        transfer_code=transfer_code,
        recipient_bank_code="058",
        recipient_account_number="0123456789",
        recipient_account_name="Jane Doe",
        status=status,
        customer_payment_id=None,
        paystack_transaction_id=None,
        paid_at=None,
        gateway_response=None,
        fee_amount=None,
        fee_journal_id=None,
        intent_metadata={"claim_number": "EXP-001"},
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=24)),
        created_at=created_at or datetime.now(UTC),
        updated_at=None,
        poll_count=poll_count,
        last_poll_error=None,
        unresolved_since=None,
    )


def _make_claim(
    claim_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    status: ExpenseClaimStatus = ExpenseClaimStatus.APPROVED,
    approver_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """Build a lightweight expense claim for unit tests."""
    return SimpleNamespace(
        claim_id=claim_id or uuid.uuid4(),
        organization_id=org_id or _org_id(),
        claim_number="EXP-001",
        status=status,
        net_payable_amount=Decimal("50000.00"),
        total_approved_amount=Decimal("50000.00"),
        total_claimed_amount=Decimal("50000.00"),
        claim_date=None,
        paid_on=None,
        payment_reference=None,
        created_by_id=uuid.uuid4(),
        reimbursement_journal_id=None,
        employee_id=uuid.uuid4(),
        approver_id=approver_id,
        recipient_bank_code="058",
        recipient_bank_name="GTBank",
        recipient_account_number="0123456789",
        recipient_account_name="Jane Doe",
        recipient_name="Jane Doe",
    )


def _make_paystack_transfer_result(
    *,
    status: str = "pending",
    transfer_code: str = "TRF_test123",
    amount: int = 5000000,
    currency: str = "NGN",
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        transfer_code=transfer_code,
        amount=amount,
        currency=currency,
    )


def _make_paystack_verify_result(
    *,
    status: str = "success",
    transfer_code: str = "TRF_verify123",
    reference: str = "EXP-verify-ref",
    amount: int = 5000000,
    currency: str = "NGN",
    recipient_code: str = "RCP_test",
    fee: int | None = 5000,
    reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        transfer_code=transfer_code,
        reference=reference,
        amount=amount,
        currency=currency,
        recipient_code=recipient_code,
        fee=fee,
        reason=reason,
        completed_at=None,
    )


def _paystack_client_context(
    initiate_result: SimpleNamespace | None = None,
    verify_result: SimpleNamespace | None = None,
    verify_error: Exception | None = None,
) -> MagicMock:
    """Return a context-manager mock for PaystackClient."""
    client = MagicMock()
    if initiate_result:
        client.initiate_transfer.return_value = initiate_result
    if verify_error is not None:
        client.verify_transfer.side_effect = verify_error
    elif verify_result:
        client.verify_transfer.return_value = verify_result
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _patch_expense_mark_paid(db: MagicMock):
    def _side_effect(
        _service,
        org_id: uuid.UUID,
        claim_id: uuid.UUID,
        *,
        payment_reference: str | None = None,
        payment_date=None,
        send_notification: bool = True,
        skip_budget_check: bool = False,
    ):
        claim = db.get.return_value
        if claim is not None:
            claim.status = ExpenseClaimStatus.PAID
            claim.paid_on = payment_date
            claim.payment_reference = payment_reference
        return claim

    return patch(
        "app.services.expense.expense_service.ExpenseService.mark_paid",
        autospec=True,
        side_effect=_side_effect,
    )


# ===========================================================================
# 1. initiate_expense_transfer — status transitions
# ===========================================================================


class TestInitiateExpenseTransfer:
    """Tests for PaymentService.initiate_expense_transfer()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    # -- happy path: Paystack returns "pending" --

    def test_pending_transfer_sets_processing_and_commits(self) -> None:
        """When Paystack returns pending, intent moves to PROCESSING and is committed."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim  # for the claim lock query

        transfer_result = _make_paystack_transfer_result(status="pending")
        client_cm = _paystack_client_context(initiate_result=transfer_result)

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            _patch_expense_mark_paid(db),
        ):
            result = svc.initiate_expense_transfer(intent, _CFG)

        assert result.status == PaymentIntentStatus.PROCESSING
        assert result.transfer_code == "TRF_test123"
        # Verify commit was called (via _commit_and_refresh)
        db.commit.assert_called()
        db.refresh.assert_called_with(intent)

    # -- happy path: Paystack returns "success" (immediate completion) --

    def test_immediate_success_sets_completed_and_commits(self) -> None:
        """When Paystack returns success immediately, intent moves to COMPLETED."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.side_effect = [claim, None]

        # process_successful_transfer re-fetches with FOR UPDATE
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result
        # db.get for expense claim inside process_successful_transfer
        db.get.return_value = claim

        transfer_result = _make_paystack_transfer_result(status="success")
        client_cm = _paystack_client_context(initiate_result=transfer_result)

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            _patch_expense_mark_paid(db),
        ):
            result = svc.initiate_expense_transfer(intent, _CFG)

        assert result.status == PaymentIntentStatus.COMPLETED
        assert result.transfer_code == "TRF_test123"
        # Claim should be PAID
        assert claim.status == ExpenseClaimStatus.PAID
        # Commit must have been called
        db.commit.assert_called()

    # -- Paystack returns "failed" --

    def test_immediate_failure_sets_failed_and_commits(self) -> None:
        """When Paystack returns failed immediately, intent is FAILED and committed."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim

        transfer_result = _make_paystack_transfer_result(status="failed")
        client_cm = _paystack_client_context(initiate_result=transfer_result)

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.initiate_expense_transfer(intent, _CFG)

        assert result.status == PaymentIntentStatus.FAILED
        assert result.gateway_response is not None
        db.commit.assert_called()

    # -- expired intent is rejected --

    def test_expired_intent_raises(self) -> None:
        """An expired intent cannot be initiated."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.initiate_expense_transfer(intent, _CFG)

        assert exc.value.status_code == 400
        assert "expired" in str(exc.value.detail).lower()
        assert intent.status == PaymentIntentStatus.EXPIRED

    # -- wrong direction rejected --

    def test_inbound_intent_rejected(self) -> None:
        """INBOUND intents cannot use initiate_expense_transfer."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, direction=PaymentDirection.INBOUND)

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.initiate_expense_transfer(intent, _CFG)

        assert exc.value.status_code == 400

    # -- wrong status rejected --

    def test_non_pending_intent_rejected(self) -> None:
        """Only PENDING intents can be initiated."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=PaymentIntentStatus.PROCESSING)

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.initiate_expense_transfer(intent, _CFG)

        assert exc.value.status_code == 400

    # -- missing recipient code --

    def test_missing_recipient_code_rejected(self) -> None:
        """Intent without transfer_recipient_code is rejected."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, transfer_recipient_code="")
        intent.transfer_recipient_code = None

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.initiate_expense_transfer(intent, _CFG)

        assert exc.value.status_code == 400
        assert "recipient" in str(exc.value.detail).lower()

    # -- cancelled claim rejected --

    def test_cancelled_claim_blocks_initiation(self) -> None:
        """If the expense claim was cancelled between steps 1 and 2, initiation is blocked."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id, status=ExpenseClaimStatus.CANCELLED)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.initiate_expense_transfer(intent, _CFG)

        assert exc.value.status_code == 400
        assert "CANCELLED" in str(exc.value.detail)

    def test_transfer_does_not_recheck_approver_budget(self) -> None:
        """Approved claims should not be blocked at payout by approver budget usage."""
        org_id = _org_id()
        db = MagicMock()
        approver_id = uuid.uuid4()
        claim = _make_claim(org_id=org_id, approver_id=approver_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim
        svc = self._svc(db, org_id)
        paystack_client_cm = MagicMock()
        paystack_client = paystack_client_cm.__enter__.return_value
        paystack_client.initiate_transfer.return_value = SimpleNamespace(
            transfer_code="TRF_test_ok",
            status="pending",
            amount=100000,
            currency="NGN",
        )

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=paystack_client_cm,
        ):
            updated = svc.initiate_expense_transfer(intent, _CFG)

        assert updated is intent
        assert intent.status == PaymentIntentStatus.PROCESSING
        assert intent.transfer_code == "TRF_test_ok"
        assert intent.gateway_response is None
        paystack_client.initiate_transfer.assert_called_once()
        db.commit.assert_called()
        db.refresh.assert_called_with(intent)

    def test_an_unobserved_initiation_is_recorded_indeterminate(self) -> None:
        """The request left this process and Paystack never said what became
        of it. Leaving the row PENDING was the old behaviour and it is a trap:
        the expiry pass eventually stamps it EXPIRED, and an expired-looking
        intent is one nobody chases (ADR-0007)."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        # The first lookup locks the claim; recording the unresolved outcome
        # then checks whether a legacy transfer-batch item exists.
        db.scalar.side_effect = [claim, None]
        client_cm = _paystack_client_context()
        client = client_cm.__enter__.return_value
        client.initiate_transfer.side_effect = PaystackUnreachable(
            "Request failed: connect timeout"
        )
        client.verify_transfer.side_effect = PaystackUnreachable(
            "Request failed: connect timeout"
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            with pytest.raises(TransferOutcomeUnknown) as excinfo:
                svc.initiate_expense_transfer(intent, _CFG)

        assert excinfo.value.intent_id == intent.intent_id
        assert intent.status == PaymentIntentStatus.INDETERMINATE
        assert intent.unresolved_since is not None
        assert intent.gateway_response["unobserved_initiation"] is True
        # Committed: this path is about to raise out of the request, and
        # without the commit the only record is a log line.
        db.commit.assert_called()
        # The claim is NOT marked paid and NOT reverted — it stays approved.
        assert claim.status == ExpenseClaimStatus.APPROVED

    def test_a_provider_refusal_at_initiation_stays_pending_and_reraises(
        self,
    ) -> None:
        """Specificity: Paystack answered, so nothing is in flight and the
        intent stays PENDING and legitimately retryable."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim
        client_cm = _paystack_client_context()
        client = client_cm.__enter__.return_value
        client.initiate_transfer.side_effect = PaystackError(
            "Failed to initiate transfer: insufficient balance"
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            with pytest.raises(PaystackError):
                svc.initiate_expense_transfer(intent, _CFG)

        assert intent.status == PaymentIntentStatus.PENDING
        assert intent.unresolved_since is None
        client.verify_transfer.assert_not_called()

    def test_timeout_recovers_by_verifying_reference(self) -> None:
        """A timeout during initiation should reconcile by transfer reference."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim
        verify_result = _make_paystack_verify_result(
            status="pending",
            transfer_code="TRF_recovered_timeout",
            reference=intent.paystack_reference,
        )
        client_cm = _paystack_client_context(verify_result=verify_result)
        client = client_cm.__enter__.return_value
        client.initiate_transfer.side_effect = PaystackError(
            'Failed to initiate transfer: { "status": false, "message": "Request timed out"}'
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.initiate_expense_transfer(intent, _CFG)

        assert result.status == PaymentIntentStatus.PROCESSING
        assert result.transfer_code == "TRF_recovered_timeout"
        client.verify_transfer.assert_called_once_with(intent.paystack_reference)
        db.commit.assert_called()
        db.refresh.assert_called_with(intent)

    def test_duplicate_reference_recovers_by_verifying_reference(self) -> None:
        """A duplicate transfer reference error should reconcile by verify_transfer."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(org_id=org_id, source_id=claim.claim_id)

        db.scalar.return_value = claim
        verify_result = _make_paystack_verify_result(
            status="pending",
            transfer_code="TRF_recovered_duplicate",
            reference=intent.paystack_reference,
        )
        client_cm = _paystack_client_context(verify_result=verify_result)
        client = client_cm.__enter__.return_value
        client.initiate_transfer.side_effect = PaystackError(
            '{"status":false,"message":"Please provide a unique reference. Reference already exists on a transfer","code":"duplicate_transfer_reference"}'
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.initiate_expense_transfer(intent, _CFG)

        assert result.status == PaymentIntentStatus.PROCESSING
        assert result.transfer_code == "TRF_recovered_duplicate"
        client.verify_transfer.assert_called_once_with(intent.paystack_reference)
        db.commit.assert_called()
        db.refresh.assert_called_with(intent)


# ===========================================================================
# 2. process_successful_transfer — status gate and claim update
# ===========================================================================


class TestProcessSuccessfulTransfer:
    """Tests for PaymentService.process_successful_transfer()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def _setup_db(
        self,
        db: MagicMock,
        locked_intent: SimpleNamespace,
        claim: SimpleNamespace | None = None,
    ) -> None:
        """Wire db.execute (FOR UPDATE) and db.get (claim lookup)."""
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = locked_intent
        db.execute.return_value = execute_result
        db.get.return_value = claim
        # _update_batch_item_status → no batch
        db.scalar.return_value = None

    # -- normal path: PROCESSING → COMPLETED --

    def test_processing_intent_completes(self) -> None:
        """A PROCESSING intent transitions to COMPLETED and claim becomes PAID."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
            transfer_code="TRF_abc",
        )
        self._setup_db(db, intent, claim)

        svc = self._svc(db, org_id)
        with _patch_expense_mark_paid(db):
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={"status": "success"},
            )

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert intent.paid_at is not None
        assert claim.status == ExpenseClaimStatus.PAID
        assert claim.paid_on is not None
        db.flush.assert_called()

    # -- defensive path: PENDING → COMPLETED (webhook race) --

    def test_pending_intent_also_accepted(self) -> None:
        """A PENDING intent is accepted (defensive: webhook before commit)."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PENDING,
            source_id=claim.claim_id,
        )
        self._setup_db(db, intent, claim)

        svc = self._svc(db, org_id)
        with _patch_expense_mark_paid(db):
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={"status": "success"},
            )

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert claim.status == ExpenseClaimStatus.PAID

    # -- idempotency: already COMPLETED is a no-op --

    def test_already_completed_is_noop(self) -> None:
        """Calling process_successful_transfer on a COMPLETED intent is idempotent."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.COMPLETED,
        )
        self._setup_db(db, intent)

        svc = self._svc(db, org_id)
        # Should not raise
        svc.process_successful_transfer(
            intent=intent,
            completed_at=datetime.now(UTC),
            gateway_response={"status": "success"},
        )

        # Status unchanged, no flush
        assert intent.status == PaymentIntentStatus.COMPLETED
        db.flush.assert_not_called()

    # -- rejected statuses --

    @pytest.mark.parametrize(
        "bad_status",
        [
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.EXPIRED,
            PaymentIntentStatus.REVERSED,
            PaymentIntentStatus.ABANDONED,
        ],
    )
    def test_invalid_statuses_rejected(self, bad_status: PaymentIntentStatus) -> None:
        """FAILED / EXPIRED / REVERSED / ABANDONED intents are rejected."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=bad_status)
        self._setup_db(db, intent)

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={"status": "success"},
            )

        assert exc.value.status_code == 400

    # -- intent not found --

    def test_missing_intent_raises_404(self) -> None:
        """If the intent disappears (deleted?), we get a 404."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=PaymentIntentStatus.PROCESSING)

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None  # gone
        db.execute.return_value = execute_result

        svc = self._svc(db, org_id)

        with pytest.raises(HTTPException) as exc:
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={},
            )

        assert exc.value.status_code == 404

    # -- claim not found (orphaned intent) --

    def test_claim_not_found_still_completes_intent(self) -> None:
        """If the claim was deleted, intent is still COMPLETED (money already sent)."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=uuid.uuid4(),
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result
        db.get.return_value = None  # claim missing

        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.process_successful_transfer(
            intent=intent,
            completed_at=datetime.now(UTC),
            gateway_response={"status": "success"},
        )

        assert intent.status == PaymentIntentStatus.COMPLETED

    # -- fee recording --

    def test_fee_recorded_in_naira(self) -> None:
        """Fee in kobo is converted to Naira and stored on intent."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
        )
        self._setup_db(db, intent, claim)

        svc = self._svc(db, org_id)
        with _patch_expense_mark_paid(db):
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={"status": "success"},
                fee_kobo=5375,  # ₦53.75
            )

        assert intent.fee_amount == Decimal("53.75")

    def test_zero_fee_not_recorded(self) -> None:
        """Zero or None fee leaves fee_amount as None."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
        )
        self._setup_db(db, intent, claim)

        svc = self._svc(db, org_id)
        with _patch_expense_mark_paid(db):
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={},
                fee_kobo=0,
            )

        assert intent.fee_amount is None

    # -- GL posting failure doesn't block completion --

    def test_gl_posting_failure_does_not_block_completion(self) -> None:
        """If GL posting raises, the intent is still COMPLETED (money already sent)."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
        )
        self._setup_db(db, intent, claim)

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.expense.expense_posting_adapter.ExpensePostingAdapter",
            ) as mock_adapter,
            _patch_expense_mark_paid(db),
        ):
            mock_adapter.post_expense_reimbursement.side_effect = RuntimeError(
                "GL boom"
            )
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={},
            )

        # Intent is COMPLETED despite GL failure
        assert intent.status == PaymentIntentStatus.COMPLETED
        assert claim.status == ExpenseClaimStatus.PAID


# ===========================================================================
# 3. mark_transfer_failed
# ===========================================================================


class TestMarkTransferFailed:
    """Tests for PaymentService.mark_transfer_failed()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_intent_set_to_failed(self) -> None:
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=PaymentIntentStatus.PROCESSING)

        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.mark_transfer_failed(intent, "Insufficient balance")

        assert intent.status == PaymentIntentStatus.FAILED
        assert "Insufficient balance" in intent.gateway_response["error"]

    def test_paid_claim_reverted_to_approved(self) -> None:
        """If claim was somehow marked PAID before failure, revert it."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id, status=ExpenseClaimStatus.PAID)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
        )
        db.get.return_value = claim
        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.mark_transfer_failed(intent, "Bank rejected")

        assert claim.status == ExpenseClaimStatus.APPROVED
        assert claim.paid_on is None

    def test_approved_claim_not_touched(self) -> None:
        """If claim is still APPROVED, mark_transfer_failed doesn't change it."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id, status=ExpenseClaimStatus.APPROVED)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
        )
        db.get.return_value = claim
        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.mark_transfer_failed(intent, "Timeout")

        assert claim.status == ExpenseClaimStatus.APPROVED


# ===========================================================================
# 4. poll_transfer_status — fallback for missed webhooks
# ===========================================================================


class TestPollTransferStatus:
    """Tests for PaymentService.poll_transfer_status()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_success_from_paystack_completes_intent(self) -> None:
        """Polling finds success → process_successful_transfer called."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_abc",
            source_id=claim.claim_id,
        )

        # FOR UPDATE re-fetch
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result
        db.get.return_value = claim

        db.scalar.return_value = None

        verify_result = _make_paystack_verify_result(status="success", fee=5000)
        client_cm = _paystack_client_context(verify_result=verify_result)

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ) as mock_client_cls,
            _patch_expense_mark_paid(db),
        ):
            result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.COMPLETED
        assert claim.status == ExpenseClaimStatus.PAID
        # Verify we use paystack_reference (not transfer_code) for Paystack lookup
        client = mock_client_cls.return_value.__enter__.return_value
        client.verify_transfer.assert_called_once_with(intent.paystack_reference)

    def test_failed_from_paystack_marks_failed(self) -> None:
        """Polling finds failure → mark_transfer_failed called."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_abc",
        )
        db.get.return_value = None  # no claim lookup needed

        db.scalar.return_value = None

        verify_result = _make_paystack_verify_result(
            status="failed", reason="Insufficient funds"
        )
        client_cm = _paystack_client_context(verify_result=verify_result)

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.FAILED

    def test_still_pending_on_paystack_no_change(self) -> None:
        """If Paystack says still pending, intent stays PROCESSING."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_abc",
        )

        verify_result = _make_paystack_verify_result(status="pending")
        client_cm = _paystack_client_context(verify_result=verify_result)

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.PROCESSING

    def test_reversed_from_paystack(self) -> None:
        """Polling finds reversal → process_transfer_reversal called."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id, status=ExpenseClaimStatus.PAID)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_abc",
            source_id=claim.claim_id,
        )
        db.get.return_value = claim

        db.scalar.return_value = None

        verify_result = _make_paystack_verify_result(
            status="reversed", reason="Account closed"
        )
        client_cm = _paystack_client_context(verify_result=verify_result)

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.REVERSED

    def test_non_processing_intent_skipped(self) -> None:
        """Polling a COMPLETED intent is a no-op."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.COMPLETED,
            transfer_code="TRF_abc",
        )

        svc = self._svc(db, org_id)
        result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.COMPLETED

    def test_missing_transfer_code_skipped(self) -> None:
        """Polling an intent without transfer_code is a no-op."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code=None,
        )

        svc = self._svc(db, org_id)
        result = svc.poll_transfer_status(intent, _CFG)

        assert result.status == PaymentIntentStatus.PROCESSING

    def test_inbound_intent_rejected(self) -> None:
        """Poll rejects INBOUND intents."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            direction=PaymentDirection.INBOUND,
            transfer_code="TRF_abc",
        )

        svc = self._svc(db, org_id)

        with pytest.raises(ValueError, match="OUTBOUND"):
            svc.poll_transfer_status(intent, _CFG)


# ===========================================================================
# 4b. reconcile_stuck_transfer / expire_stale_pending_transfer
#
# The scheduled worker used to make these decisions inline. They are the
# owner's now, and the property that matters is that both re-prove, under a
# lock, the premise a *different* session established.
# ===========================================================================


class TestReconcileStuckTransfer:
    """Tests for PaymentService.reconcile_stuck_transfer()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_ambiguous_pending_intent_is_promoted_then_settled(self) -> None:
        """PENDING with a transfer_code means initiation was ambiguous and the
        money may already be moving. Polling only reconciles PROCESSING rows,
        so the owner promotes it first and drives it to a verdict."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PENDING,
            transfer_code="TRF_amb",
            source_id=claim.claim_id,
            bank_account_id=None,
        )

        db.scalars.return_value.one_or_none.return_value = intent
        db.execute.return_value.scalar_one_or_none.return_value = intent
        db.get.return_value = claim
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_result=_make_paystack_verify_result(status="success")
        )

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            _patch_expense_mark_paid(db),
        ):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert result.outcome.value == "completed"
        assert result.poll_count == 1

    def test_an_unreachable_provider_yields_indeterminate_not_failed(self) -> None:
        """The circuit breaker stops the polling; it does not decide the money.

        Ten unanswered attempts and one refusal are different facts, and this
        method used to record them identically (ADR-0007).
        """
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_amb",
            poll_count=9,
        )
        db.scalars.return_value.one_or_none.return_value = intent
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_error=PaystackUnreachable("Request failed: connect timeout")
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.INDETERMINATE
        assert intent.unresolved_since is not None
        assert result.outcome.value == "indeterminate"

    def test_a_provider_refusal_at_the_budget_yields_failed(self) -> None:
        """Specificity: FAILED did not simply stop being reachable."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_amb",
            poll_count=9,
        )
        db.scalars.return_value.one_or_none.return_value = intent
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_error=PaystackError("Transfer not found")
        )

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient",
            return_value=client_cm,
        ):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.FAILED
        assert intent.unresolved_since is None
        assert result.outcome.value == "abandoned"

    @pytest.mark.parametrize(
        "settled",
        [
            PaymentIntentStatus.COMPLETED,
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.REVERSED,
            PaymentIntentStatus.EXPIRED,
            # INDETERMINATE is not settled — it is the opposite of settled —
            # but the fast poller must leave it alone for the same reason: it
            # re-proves PENDING/PROCESSING under the lock, so an unresolved
            # intent drops out of the two-minute loop by construction and
            # stays selectable by `find_indeterminate_transfer_intents`
            # without burning an attempt (ADR-0007).
            PaymentIntentStatus.INDETERMINATE,
        ],
    )
    def test_an_already_settled_intent_is_left_alone(
        self, settled: PaymentIntentStatus
    ) -> None:
        """Selected while in flight, settled by a webhook before the poller got
        to it. The verdict is not the poller's to reopen, and the attempt
        budget is not its to spend."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=settled,
            transfer_code="TRF_done",
            poll_count=3,
        )
        db.scalars.return_value.one_or_none.return_value = intent

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient"
        ) as client_cls:
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status == settled
        assert intent.poll_count == 3
        assert result.outcome.value == "skipped"
        client_cls.assert_not_called()

    def test_a_vanished_intent_is_reported_not_raised(self) -> None:
        """A batch job must survive one row disappearing under it."""
        org_id = _org_id()
        db = MagicMock()
        db.scalars.return_value.one_or_none.return_value = None

        result = self._svc(db, org_id).reconcile_stuck_transfer(uuid.uuid4(), _CFG)

        assert result.outcome.value == "skipped"


class TestResolveIndeterminateTransfer:
    """The only writer permitted to move an intent out of INDETERMINATE."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def _unresolved(self, org_id: uuid.UUID, *, hours_ago: float = 1.0) -> Any:
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.INDETERMINATE,
            transfer_code="TRF_unknown",
            poll_count=10,
            bank_account_id=None,
        )
        intent.unresolved_since = datetime.now(UTC) - timedelta(hours=hours_ago)
        return intent

    def test_a_real_verdict_resolves_it_and_clears_the_clock(self) -> None:
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = self._unresolved(org_id)
        intent.source_id = claim.claim_id

        db.scalars.return_value.one_or_none.return_value = intent
        db.execute.return_value.scalar_one_or_none.return_value = intent
        db.get.return_value = claim
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_result=_make_paystack_verify_result(status="success")
        )

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            _patch_expense_mark_paid(db),
            patch.object(
                PaymentService,
                "resolve_transfer_unresolved_alert_threshold",
                return_value=timedelta(hours=6),
            ),
        ):
            result = svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert intent.unresolved_since is None
        assert result.outcome.value == "completed"

    def test_still_no_answer_keeps_it_unresolved_and_never_settles(self) -> None:
        """No attempt cap and no give-up branch: a budget here would rebuild
        the defect one level up, out of repeated silence rather than one
        silence."""
        org_id = _org_id()
        db = MagicMock()
        intent = self._unresolved(org_id)
        started_at = intent.unresolved_since

        db.scalars.return_value.one_or_none.return_value = intent
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_error=PaystackUnreachable("Request failed: connect timeout")
        )

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            patch.object(
                PaymentService,
                "resolve_transfer_unresolved_alert_threshold",
                return_value=timedelta(hours=6),
            ),
        ):
            result = svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.INDETERMINATE
        assert result.outcome.value == "still_unresolved"
        # The clock is NOT restarted, or nothing would ever age into an alert.
        assert intent.unresolved_since == started_at

    def test_an_intent_that_resolved_under_the_worker_is_skipped(self) -> None:
        """Selected in one session, settled by a webhook before this ran. The
        premise is re-proved under the lock (ADR-0005 section 3)."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.COMPLETED,
            transfer_code="TRF_unknown",
            poll_count=10,
        )
        db.scalars.return_value.one_or_none.return_value = intent

        svc = self._svc(db, org_id)

        with patch(
            "app.services.finance.payments.payment_service.PaystackClient"
        ) as client_cls:
            result = svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert result.outcome.value == "skipped"
        client_cls.assert_not_called()

    def test_past_the_threshold_it_escalates(self, caplog) -> None:
        org_id = _org_id()
        db = MagicMock()
        intent = self._unresolved(org_id, hours_ago=9)

        db.scalars.return_value.one_or_none.return_value = intent
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_error=PaystackUnreachable("Request failed: connect timeout")
        )

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            patch.object(
                PaymentService,
                "resolve_transfer_unresolved_alert_threshold",
                return_value=timedelta(hours=6),
            ),
            caplog.at_level("ERROR"),
        ):
            svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert any("ESCALATION" in record.message for record in caplog.records)

    def test_below_the_threshold_it_does_not_escalate(self, caplog) -> None:
        """Specificity for the test above — the alert is not simply always on."""
        org_id = _org_id()
        db = MagicMock()
        intent = self._unresolved(org_id, hours_ago=1)

        db.scalars.return_value.one_or_none.return_value = intent
        db.scalar.return_value = None

        client_cm = _paystack_client_context(
            verify_error=PaystackUnreachable("Request failed: connect timeout")
        )

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.finance.payments.payment_service.PaystackClient",
                return_value=client_cm,
            ),
            patch.object(
                PaymentService,
                "resolve_transfer_unresolved_alert_threshold",
                return_value=timedelta(hours=6),
            ),
            caplog.at_level("ERROR"),
        ):
            svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert not any("ESCALATION" in record.message for record in caplog.records)


class TestExpireStalePendingTransfer:
    """Tests for PaymentService.expire_stale_pending_transfer()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_a_genuinely_stalled_intent_is_expired(self) -> None:
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PENDING,
            transfer_code=None,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db.scalars.return_value.one_or_none.return_value = intent

        assert self._svc(db, org_id).expire_stale_pending_transfer(intent.intent_id)
        assert intent.status == PaymentIntentStatus.EXPIRED

    def test_a_transfer_started_in_the_gap_is_not_expired(self) -> None:
        """The premise was established in the cross-organization select; by the
        time this write happens the transfer may have been initiated, and
        EXPIRED over an in-flight payout is a payout nobody chases."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            transfer_code="TRF_started_in_the_gap",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db.scalars.return_value.one_or_none.return_value = intent

        assert not self._svc(db, org_id).expire_stale_pending_transfer(intent.intent_id)
        assert intent.status == PaymentIntentStatus.PROCESSING


# ===========================================================================
# 5. process_transfer_reversal
# ===========================================================================


class TestProcessTransferReversal:
    """Tests for PaymentService.process_transfer_reversal()."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_completed_intent_reversed_and_claim_reverted(self) -> None:
        """COMPLETED intent reversal reverts claim to APPROVED."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id, status=ExpenseClaimStatus.PAID)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.COMPLETED,
            source_id=claim.claim_id,
        )
        db.get.return_value = claim

        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.process_transfer_reversal(
            intent=intent,
            reversed_at=datetime.now(UTC),
            gateway_response={"status": "reversed"},
            reason="Account closed",
        )

        assert intent.status == PaymentIntentStatus.REVERSED
        assert claim.status == ExpenseClaimStatus.APPROVED
        assert claim.paid_on is None
        assert claim.payment_reference is None

    def test_already_reversed_is_noop(self) -> None:
        """Double reversal is idempotent."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=PaymentIntentStatus.REVERSED)

        svc = self._svc(db, org_id)
        svc.process_transfer_reversal(
            intent=intent,
            reversed_at=datetime.now(UTC),
            gateway_response={},
        )

        assert intent.status == PaymentIntentStatus.REVERSED
        db.flush.assert_not_called()

    @pytest.mark.parametrize(
        "bad_status",
        [
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.EXPIRED,
        ],
    )
    def test_invalid_status_for_reversal(self, bad_status: PaymentIntentStatus) -> None:
        """Only COMPLETED or PROCESSING intents can be reversed."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(org_id=org_id, status=bad_status)

        svc = self._svc(db, org_id)
        svc.process_transfer_reversal(
            intent=intent,
            reversed_at=datetime.now(UTC),
            gateway_response={},
        )

        # Should NOT change to REVERSED — early return
        assert intent.status == bad_status


# ===========================================================================
# 6. Webhook service — transfer event dispatching
# ===========================================================================


class TestWebhookTransferEvents:
    """Tests for WebhookService handling transfer events."""

    def test_transfer_success_validates_amount(self) -> None:
        """transfer.success webhook validates amount before processing."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(
            amount=Decimal("100.00"),
            status=PaymentIntentStatus.PROCESSING,
        )

        # Amount mismatch: 200.00 NGN (20000 kobo) vs intent's 100.00 (10000 kobo)
        with pytest.raises(ValueError, match="Amount mismatch"):
            svc._validate_amount_and_currency(
                intent=intent,
                data={"amount": 20000, "currency": "NGN"},
                event_type="transfer.success",
            )

    def test_transfer_success_validates_currency(self) -> None:
        """transfer.success webhook validates currency."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(amount=Decimal("100.00"))

        with pytest.raises(ValueError, match="Currency mismatch"):
            svc._validate_amount_and_currency(
                intent=intent,
                data={"amount": 10000, "currency": "USD"},
                event_type="transfer.success",
            )

    def test_amount_within_1_kobo_tolerance_accepted(self) -> None:
        """Rounding differences of 1 kobo are tolerated."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(amount=Decimal("100.00"))

        # 10001 kobo = 100.01 NGN → 1 kobo diff from 10000 → OK
        svc._validate_amount_and_currency(
            intent=intent,
            data={"amount": 10001, "currency": "NGN"},
            event_type="transfer.success",
        )

    def test_transfer_success_dispatches_to_payment_service(self) -> None:
        """_handle_transfer_success calls process_successful_transfer."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(
            amount=Decimal("500.00"),
            status=PaymentIntentStatus.PROCESSING,
        )

        data: dict[str, Any] = {
            "amount": 50000,
            "currency": "NGN",
            "reference": intent.paystack_reference,
            "completed_at": "2026-02-12T10:30:00.000Z",
            "fee": 2500,
        }

        with patch.object(
            PaymentService, "process_successful_transfer"
        ) as mock_process:
            svc._handle_transfer_success(intent, data)

        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args
        assert (
            call_kwargs.kwargs.get("fee_kobo") == 2500
            or call_kwargs[1].get("fee_kobo") == 2500
        )

    def test_transfer_failed_dispatches_to_mark_failed(self) -> None:
        """_handle_transfer_failed calls mark_transfer_failed."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(status=PaymentIntentStatus.PROCESSING)

        data: dict[str, Any] = {
            "reason": "Insufficient funds",
            "transfer_code": "TRF_x",
        }

        with patch.object(PaymentService, "mark_transfer_failed") as mock_fail:
            svc._handle_transfer_failed(intent, data)

        mock_fail.assert_called_once()

    def test_transfer_reversed_dispatches_to_reversal(self) -> None:
        """_handle_transfer_reversed calls process_transfer_reversal."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(status=PaymentIntentStatus.COMPLETED)

        data: dict[str, Any] = {
            "reason": "Account frozen",
            "reversed_at": "2026-02-12T12:00:00.000Z",
        }

        with patch.object(PaymentService, "process_transfer_reversal") as mock_reverse:
            svc._handle_transfer_reversed(intent, data)

        mock_reverse.assert_called_once()


# ===========================================================================
# 7. Webhook idempotency
# ===========================================================================


class TestWebhookIdempotency:
    """Tests for duplicate webhook handling."""

    def test_duplicate_event_id_returns_existing(self) -> None:
        """Second webhook with same event_id returns DUPLICATE status."""
        db = MagicMock()
        svc = WebhookService(db)

        existing_webhook = SimpleNamespace(
            webhook_id=uuid.uuid4(),
            status=WebhookStatus.PROCESSED,
            paystack_event_id="transfer.success:REF-1",
        )
        db.scalar.return_value = existing_webhook

        client_cls = MagicMock()
        client_cls.return_value.verify_webhook_signature.return_value = True

        with patch(
            "app.services.finance.payments.webhook_service.PaystackClient",
            client_cls,
        ):
            result = svc.process_webhook(
                event_type="transfer.success",
                event_data={"reference": "REF-1"},
                paystack_config=_CFG,
                raw_payload=b"{}",
                signature="sig",
            )

        assert result.status == WebhookStatus.DUPLICATE

    def test_event_id_built_from_type_and_reference(self) -> None:
        """Event ID is deterministic: {event_type}:{reference}."""
        svc = WebhookService(MagicMock())

        event_id = svc._build_event_id(
            "transfer.success", {"reference": "EXP-CLM-abc123"}
        )

        assert event_id == "transfer.success:EXP-CLM-abc123"

    def test_event_id_handles_missing_reference(self) -> None:
        svc = WebhookService(MagicMock())

        event_id = svc._build_event_id("charge.success", {})
        assert event_id == "charge.success:"


# ===========================================================================
# 8. Edge cases: concurrent / race / boundary
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def _svc(self, db: MagicMock, org_id: uuid.UUID) -> PaymentService:
        svc = PaymentService.__new__(PaymentService)
        svc.db = db
        svc.organization_id = org_id
        return svc

    def test_webhook_and_poll_cannot_double_complete(self) -> None:
        """If webhook sets COMPLETED, polling sees COMPLETED and skips."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.COMPLETED,
            transfer_code="TRF_done",
        )

        svc = self._svc(db, org_id)
        # poll_transfer_status checks status first
        result = svc.poll_transfer_status(intent, _CFG)
        assert result.status == PaymentIntentStatus.COMPLETED

    def test_kobo_tolerance_outbound_transfer(self) -> None:
        """OUTBOUND transfers allow up to 5 kobo tolerance for fee rounding."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(
            amount=Decimal("100.00"), direction=PaymentDirection.OUTBOUND
        )

        # 1 kobo diff → OK
        svc._validate_amount_and_currency(
            intent, {"amount": 10001, "currency": "NGN"}, "transfer.success"
        )

        # 5 kobo diff → OK (within OUTBOUND tolerance)
        svc._validate_amount_and_currency(
            intent, {"amount": 10005, "currency": "NGN"}, "transfer.success"
        )

        # 6 kobo diff → rejected (exceeds OUTBOUND tolerance)
        with pytest.raises(ValueError, match="Amount mismatch"):
            svc._validate_amount_and_currency(
                intent, {"amount": 10006, "currency": "NGN"}, "transfer.success"
            )

    def test_kobo_tolerance_inbound_collection(self) -> None:
        """INBOUND collections keep strict 1 kobo tolerance."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(
            amount=Decimal("100.00"), direction=PaymentDirection.INBOUND
        )

        # 1 kobo diff → OK
        svc._validate_amount_and_currency(
            intent, {"amount": 10001, "currency": "NGN"}, "charge.success"
        )

        # 2 kobo diff → rejected (strict for INBOUND)
        with pytest.raises(ValueError, match="Amount mismatch"):
            svc._validate_amount_and_currency(
                intent, {"amount": 10002, "currency": "NGN"}, "charge.success"
            )

    def test_fractional_amount_kobo_conversion(self) -> None:
        """Amounts like ₦12,345.67 convert correctly to kobo."""
        db = MagicMock()
        svc = WebhookService(db)
        intent = _make_intent(amount=Decimal("12345.67"))

        # 12345.67 * 100 = 1234567 kobo
        svc._validate_amount_and_currency(
            intent, {"amount": 1234567, "currency": "NGN"}, "transfer.success"
        )

    def test_non_expense_claim_source_type(self) -> None:
        """process_successful_transfer skips claim update for non-EXPENSE_CLAIM."""
        org_id = _org_id()
        db = MagicMock()
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_type="GENERAL",
            source_id=None,
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result

        db.scalar.return_value = None

        svc = self._svc(db, org_id)
        svc.process_successful_transfer(
            intent=intent,
            completed_at=datetime.now(UTC),
            gateway_response={},
        )

        assert intent.status == PaymentIntentStatus.COMPLETED
        # db.get not called for claim
        db.get.assert_not_called()

    def test_intent_with_no_bank_account_skips_gl_posting(self) -> None:
        """If bank_account_id is None, GL posting is skipped gracefully."""
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
            bank_account_id=None,
        )

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result
        db.get.return_value = claim

        db.scalar.return_value = None

        svc = self._svc(db, org_id)

        with (
            patch(
                "app.services.expense.expense_posting_adapter.ExpensePostingAdapter",
            ) as mock_adapter,
            _patch_expense_mark_paid(db),
        ):
            svc.process_successful_transfer(
                intent=intent,
                completed_at=datetime.now(UTC),
                gateway_response={},
            )

            # GL posting NOT attempted when no bank account
            mock_adapter.post_expense_reimbursement.assert_not_called()

        assert intent.status == PaymentIntentStatus.COMPLETED
        assert claim.status == ExpenseClaimStatus.PAID

    def test_process_successful_transfer_uses_expense_mark_paid(self) -> None:
        org_id = _org_id()
        db = MagicMock()
        claim = _make_claim(org_id=org_id)
        intent = _make_intent(
            org_id=org_id,
            status=PaymentIntentStatus.PROCESSING,
            source_id=claim.claim_id,
            transfer_code="TRF_abc",
        )
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = intent
        db.execute.return_value = execute_result
        db.get.return_value = claim
        db.scalar.return_value = None

        completed_at = datetime.now(UTC)
        svc = self._svc(db, org_id)

        with patch(
            "app.services.expense.expense_service.ExpenseService.mark_paid",
            autospec=True,
            return_value=claim,
        ) as mock_mark_paid:
            svc.process_successful_transfer(
                intent=intent,
                completed_at=completed_at,
                gateway_response={"status": "success"},
            )

        mock_mark_paid.assert_called_once_with(
            ANY,
            org_id,
            claim.claim_id,
            payment_reference=intent.paystack_reference,
            payment_date=completed_at.date(),
            send_notification=False,
            skip_budget_check=True,
        )
