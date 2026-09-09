"""An outcome nobody observed is not a failed payout.

ADR-0005 gave ``payments.payment_intent.status`` one writer and left open what
that writer should SAY when it could not find out. ADR-0007 answers it: FAILED
is a claim that the money did not move, and this system may only make that
claim when Paystack made it first.

These tests are the behavioural half of that rule. They drive the real
``PaymentService`` with a stubbed transport and assert on the two things that
distinguish a verdict from a non-observation:

1. **What is written.** ``httpx.ConnectError`` on every call, run past the
   attempt cap, must produce INDETERMINATE with ``unresolved_since`` set — not
   FAILED. A single ``{"status": "failed"}`` must produce FAILED on the first
   attempt, so the test above cannot pass merely because FAILED stopped being
   reachable.

2. **What the rest of the system then does.** This is where the defect actually
   bit: FAILED is not a label, it is an instruction. The expense claim must stay
   APPROVED and unpaid, ``reset_expense_payment_intent`` must refuse (``force``
   included, because force is exactly what a hurried operator reaches for), and
   no GL journal may be posted. Each of those is a step on the path to paying
   the same claim twice.

The transport is stubbed at ``httpx.Client.request``, one level below
``PaystackClient``, deliberately: the classification of a connect error into
``PaystackUnreachable`` is part of what is under test, so stubbing the client
itself would assume the answer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

import httpx
import pytest
from fastapi import HTTPException

from app.models.expense.expense_claim import ExpenseClaimStatus
from app.models.finance.payments.payment_intent import (
    PaymentDirection,
    PaymentIntentStatus,
)
from app.services.finance.payments.payment_service import (
    PaymentService,
    TransferPollOutcome,
)
from app.services.finance.payments.paystack_client import PaystackConfig

_CFG = PaystackConfig(
    secret_key="sk_test", public_key="pk_test", webhook_secret="wh_test"
)


@pytest.fixture(autouse=True)
def _payments_settings():
    """The service reads its alert threshold from settings; a MagicMock session
    is not a settings store, so answer the one key that matters and let every
    other lookup come back None (which is what an unconfigured deployment
    looks like anyway)."""

    def _resolve(_db, _domain, key, *args, **kwargs):
        if key == "paystack_transfer_unresolved_alert_hours":
            return 6
        return None

    with patch(
        "app.services.finance.payments.payment_service.resolve_value",
        side_effect=_resolve,
    ):
        yield


def _org_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_intent(
    *,
    org_id: uuid.UUID,
    claim_id: uuid.UUID,
    status: PaymentIntentStatus = PaymentIntentStatus.PROCESSING,
    poll_count: int = 0,
) -> Any:
    return SimpleNamespace(
        intent_id=uuid.uuid4(),
        organization_id=org_id,
        paystack_reference=f"EXP-CLM-{uuid.uuid4().hex[:8]}",
        amount=Decimal("50000.00"),
        currency_code="NGN",
        email="employee@example.com",
        direction=PaymentDirection.OUTBOUND,
        # Deliberately SET. With no settlement account the GL branch is skipped
        # for reasons unrelated to this rule, and "no journal posted" would pass
        # for the wrong reason. Configured here, so the only thing keeping the
        # ledger clean is that the unobserved path never reaches posting.
        bank_account_id=uuid.uuid4(),
        source_type="EXPENSE_CLAIM",
        source_id=claim_id,
        transfer_recipient_code="RCP_test",
        transfer_code="TRF_in_flight",
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
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        created_at=datetime.now(UTC) - timedelta(minutes=30),
        updated_at=None,
        poll_count=poll_count,
        last_poll_error=None,
        unresolved_since=None,
    )


def _make_claim(claim_id: uuid.UUID, org_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        claim_id=claim_id,
        organization_id=org_id,
        claim_number="EXP-001",
        status=ExpenseClaimStatus.APPROVED,
        net_payable_amount=Decimal("50000.00"),
        paid_on=None,
        payment_reference=None,
        created_by_id=uuid.uuid4(),
        reimbursement_journal_id=None,
        employee_id=uuid.uuid4(),
    )


def _service(db: MagicMock, org_id: uuid.UUID) -> PaymentService:
    svc = PaymentService.__new__(PaymentService)
    svc.db = db
    svc.organization_id = org_id
    return svc


def _db_for(intent: Any, claim: Any) -> MagicMock:
    db = MagicMock()
    db.scalars.return_value.one_or_none.return_value = intent
    db.execute.return_value.scalar_one_or_none.return_value = intent
    db.get.return_value = claim
    # No batch item for this intent.
    db.scalar.return_value = None
    return db


def _unreachable_transport():
    """Stub httpx at the socket, not at PaystackClient.

    The client's job of turning a ConnectError into PaystackUnreachable is part
    of what these tests assert, so it must run for real.
    """
    return patch.object(
        httpx.Client,
        "request",
        side_effect=httpx.ConnectError("[Errno 61] Connection refused"),
    )


def _provider_says(payload: dict[str, Any]):
    """Paystack answered. Whatever it says is a real answer about the money."""
    reason = None
    if "reason" in payload:
        reason = payload["reason"]
    response = httpx.Response(
        200,
        json={
            "status": True,
            "message": "Transfer retrieved",
            "data": {
                "status": payload["status"],
                "reference": "EXP-CLM-ref",
                "transfer_code": "TRF_in_flight",
                "amount": 5000000,
                "currency": "NGN",
                "recipient": {"recipient_code": "RCP_test"},
                "reason": reason,
                "failures": None,
                "fee_charged": 5000,
                "created_at": "2026-08-25T10:00:00.000Z",
                "updated_at": "2026-08-25T10:00:00.000Z",
            },
        },
        request=httpx.Request("GET", "https://api.paystack.co/transfer/verify/x"),
    )
    return patch.object(httpx.Client, "request", return_value=response)


# ===========================================================================
# 1. Unreachable, past the cap: INDETERMINATE, and nothing downstream moves
# ===========================================================================


class TestUnreachableProviderIsNotAVerdict:
    def _run_to_the_cap(self, svc: PaymentService, intent: Any):
        """Drive the reconciler until the attempt budget is spent."""
        last = None
        for _ in range(10):
            last = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)
            if intent.status is not PaymentIntentStatus.PROCESSING:
                break
        return last

    def test_ten_unanswered_attempts_yield_indeterminate(self) -> None:
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _unreachable_transport():
            result = self._run_to_the_cap(svc, intent)

        assert intent.status is PaymentIntentStatus.INDETERMINATE
        assert intent.poll_count == 10
        assert result is not None
        assert result.outcome is TransferPollOutcome.INDETERMINATE

    def test_unresolved_since_is_set_and_starts_the_clock(self) -> None:
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        before = datetime.now(UTC)
        with _unreachable_transport():
            self._run_to_the_cap(svc, intent)

        assert intent.unresolved_since is not None
        assert intent.unresolved_since >= before
        assert intent.gateway_response["outcome_observed"] is False

    def test_the_expense_claim_is_untouched(self) -> None:
        """Not reverted, not paid, not annotated. The claim was APPROVED before
        the payout and it is APPROVED after — which is the honest position when
        nobody knows whether the money left."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _unreachable_transport():
            self._run_to_the_cap(svc, intent)

        assert claim.status is ExpenseClaimStatus.APPROVED
        assert claim.paid_on is None
        assert claim.payment_reference is None

    def test_the_claim_is_not_re_reimbursable(self) -> None:
        """`reset_expense_payment_intent` is how an operator earns permission to
        pay the claim again. It must refuse — and `force` must not help, because
        force is exactly what somebody in a hurry reaches for."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        db = _db_for(intent, claim)
        svc = _service(db, org_id)

        with _unreachable_transport():
            self._run_to_the_cap(svc, intent)

        assert intent.status is PaymentIntentStatus.INDETERMINATE

        # `reset_expense_payment_intent` looks the intent up itself.
        db.scalar.return_value = intent

        for force in (False, True):
            with pytest.raises(HTTPException) as excinfo:
                svc.reset_expense_payment_intent(claim_id, force=force)
            assert excinfo.value.status_code == 409
            assert "unknown" in str(excinfo.value.detail).lower()

        # ...and the refusal did not quietly change the status on its way out.
        assert intent.status is PaymentIntentStatus.INDETERMINATE

    def test_no_gl_journal_is_posted(self) -> None:
        """Posting happens only inside `process_successful_transfer`, which an
        unobserved outcome never reaches. Asserted on the adapter rather than on
        the absence of a journal id, so a refactor that posts from somewhere
        else is still caught."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with (
            patch(
                "app.services.expense.expense_posting_adapter.ExpensePostingAdapter"
            ) as adapter,
            _unreachable_transport(),
        ):
            self._run_to_the_cap(svc, intent)

        adapter.post_expense_reimbursement.assert_not_called()
        adapter.post_transfer_fee.assert_not_called()
        assert intent.paid_at is None
        assert intent.fee_journal_id is None


# ===========================================================================
# 2. Specificity: FAILED is still reachable, in one attempt, when earned
# ===========================================================================


class TestAnAnsweredRefusalIsStillFailed:
    def test_one_attempt_with_a_failed_verdict_yields_failed(self) -> None:
        """The rule above is not "stop writing FAILED". Paystack said the payout
        did not happen, so FAILED is the honest record — and it takes ONE
        attempt, not ten, because a verdict does not need a budget."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _provider_says({"status": "failed", "reason": "Insufficient funds"}):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status is PaymentIntentStatus.FAILED
        assert intent.poll_count == 1
        assert intent.unresolved_since is None
        assert result.outcome is TransferPollOutcome.FAILED

    def test_a_failed_verdict_makes_the_claim_payable_again(self) -> None:
        """The contrast that makes the whole distinction matter: this is what
        the unobserved case must NOT do."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        claim.status = ExpenseClaimStatus.PAID
        claim.paid_on = datetime.now(UTC).date()
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _provider_says({"status": "failed", "reason": "Insufficient funds"}):
            svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert claim.status is ExpenseClaimStatus.APPROVED
        assert claim.paid_on is None


# ===========================================================================
# 3. A status we cannot read is not "still pending"
# ===========================================================================


class TestUnrecognisedStatusIsNotStillPending:
    def test_an_unknown_status_word_yields_indeterminate(self) -> None:
        """`poll_transfer_status` had no `else`: anything that was not success,
        failed or reversed logged "still pending". A transfer Paystack has
        already settled under a word we do not parse would then sit in
        PROCESSING until the circuit breaker stamped a verdict on it — one
        manufactured out of a word we did not read."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _provider_says({"status": "quarantined"}):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status is PaymentIntentStatus.INDETERMINATE
        assert result.outcome is TransferPollOutcome.INDETERMINATE
        assert result.outcome is not TransferPollOutcome.STILL_PENDING
        assert intent.unresolved_since is not None
        assert "quarantined" in intent.gateway_response["last_error"]

    @pytest.mark.parametrize("in_flight", ["pending", "otp", "processing", "receipt"])
    def test_a_documented_in_flight_status_is_still_pending(
        self, in_flight: str
    ) -> None:
        """Specificity for the test above. The words Paystack actually uses for
        "still moving" must keep meaning that, or every in-flight transfer in
        the fleet would be escalated to an operator."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _provider_says({"status": in_flight}):
            result = svc.reconcile_stuck_transfer(intent.intent_id, _CFG)

        assert intent.status is PaymentIntentStatus.PROCESSING
        assert intent.unresolved_since is None
        assert result.outcome is TransferPollOutcome.STILL_PENDING


# ===========================================================================
# 4. The way out has exactly one owner
# ===========================================================================


class TestOnlyTheReconcilerResolvesIt:
    def test_an_unresolved_intent_is_selectable_forever(self) -> None:
        """The fast poller caps attempts; the slow lane must not, or an
        unresolved payout would eventually stop being asked about and become
        permanently invisible — a worse outcome than the FAILED this replaces,
        because at least FAILED is on a dashboard."""
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        db = _db_for(intent, claim)
        svc = _service(db, org_id)

        with _unreachable_transport():
            for _ in range(10):
                svc.reconcile_stuck_transfer(intent.intent_id, _CFG)
            assert intent.status is PaymentIntentStatus.INDETERMINATE

            # Many more slow-lane passes, all unanswered.
            for _ in range(25):
                result = svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert intent.status is PaymentIntentStatus.INDETERMINATE
        assert result.outcome is TransferPollOutcome.STILL_UNRESOLVED

    def test_the_reconciler_resolves_it_once_paystack_answers(self) -> None:
        org_id = _org_id()
        claim_id = uuid.uuid4()
        claim = _make_claim(claim_id, org_id)
        intent = _make_intent(org_id=org_id, claim_id=claim_id)
        svc = _service(_db_for(intent, claim), org_id)

        with _unreachable_transport():
            for _ in range(10):
                svc.reconcile_stuck_transfer(intent.intent_id, _CFG)
        assert intent.status is PaymentIntentStatus.INDETERMINATE

        with _provider_says({"status": "failed", "reason": "Insufficient funds"}):
            result = svc.resolve_indeterminate_transfer(intent.intent_id, _CFG)

        assert intent.status is PaymentIntentStatus.FAILED
        assert intent.unresolved_since is None
        assert result.outcome is TransferPollOutcome.FAILED
