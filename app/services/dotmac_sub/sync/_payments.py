from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc  # type: ignore[assignment]

from sqlalchemy import select

from app.config import settings
from app.models.finance.ar.customer import Customer
from app.models.finance.ar.customer_payment import (
    CustomerPayment,
    PaymentMethod,
    PaymentStatus,
)
from app.models.finance.ar.external_sync import EntityType
from app.models.finance.ar.invoice import Invoice
from app.models.finance.ar.payment_allocation import PaymentAllocation
from app.services.dotmac_sub.client import DotmacSubError, PaymentRecord
from app.services.finance.money_boundary import (
    check_settlement_identity,
    to_boundary_money,
)

from ._constants import DOTMAC_SUB_SYNC_MIN_DATE, SYSTEM_USER_ID, _PRE_CUTOFF_SENTINEL
from ._progress import WatermarkProgress
from ._types import SyncResult

logger = logging.getLogger(__name__)

# dotmac_sub payment statuses that represent settled cash.
_SETTLED_STATUSES = {"succeeded", "partially_refunded"}


class PaymentSyncMixin:
    """Sync dotmac_sub payments → ERP AR receipts (wholesale GL-suppression)."""

    db: Any
    client: Any
    organization_id: UUID
    _account_cache: dict[str, UUID]

    _compute_hash: Any
    _has_changed: Any
    _record_sync: Any
    _get_synced_entity: Any
    _get_customer_for_account: Any
    _load_payment_channels: Any
    _get_bank_account_for_channel: Any
    _channel_name: Any
    _get_sync_watermark: Any
    _advance_sync_watermark: Any
    _generate_payment_number: Any
    _reprime_tenant_context: Any
    _functional_amount: Any
    _resolve_source_wht_code: Any

    def sync_payments(
        self,
        account_id: str | None = None,
        status: str | None = None,
        created_by_user_id: UUID | None = None,
        batch_size: int | None = None,
        skip_unchanged: bool = True,
    ) -> SyncResult:
        result = SyncResult(success=True, entity_type="payments")
        processed = 0
        self._load_payment_channels()
        use_watermark = account_id is None and status is None
        watermark = (
            self._get_sync_watermark(EntityType.PAYMENT) if use_watermark else None
        )
        updated_since = watermark.isoformat() if watermark else None
        # ONE shared owner for the park-vs-freeze cursor decision (see
        # _progress.WatermarkProgress); parse rejections and savepoint row
        # failures flow through the same accounting.
        progress = WatermarkProgress(watermark, label="payment")

        try:
            for pay in self.client.get_payments(
                account_id=account_id,
                status=status,
                updated_since=updated_since,
                on_parse_error=progress.parse_error_collector(result),
            ):
                if batch_size and processed >= batch_size:
                    result.message = f"Batch limit ({batch_size}) reached"
                    break
                row_updated_at = pay.updated_at
                try:
                    savepoint = self.db.begin_nested()
                    self._sync_single_payment(
                        pay, result, created_by_user_id, skip_unchanged
                    )
                    savepoint.commit()
                    processed += 1
                    progress.record_success(row_updated_at)
                    if processed % 500 == 0:
                        self.db.commit()
                        self._reprime_tenant_context()
                        self.db.expunge_all()
                        logger.info("Progress: %d payments processed", processed)
                except Exception as e:  # noqa: BLE001
                    try:
                        savepoint.rollback()
                    except Exception:  # noqa: BLE001
                        self.db.rollback()
                    result.errors.append(f"Payment {pay.id}: {e!s}")
                    logger.exception("Error syncing payment %s", pay.id)
                    progress.record_failure(row_updated_at)
            # Advance-or-park, or freeze on an unpositioned failure (see
            # WatermarkProgress).
            if use_watermark:
                progress.conclude(
                    lambda cursor: self._advance_sync_watermark(
                        EntityType.PAYMENT, cursor
                    )
                )
            self.db.flush()
            result.message = (
                f"Synced {result.created} new, {result.updated} updated, "
                f"{result.skipped} skipped payments"
            )
        except DotmacSubError as e:
            result.success = False
            result.message = f"dotmac_sub API error: {e.message}"
            result.errors.append(result.message)
            logger.error(result.message)
        return result

    def _sync_single_payment(
        self,
        pay: PaymentRecord,
        result: SyncResult,
        created_by_user_id: UUID | None,
        skip_unchanged: bool,
    ) -> None:
        external_id = pay.id

        # E4 money boundary FIRST — before the settled/unchanged branches —
        # so every consumed monetary fact is validated on EVERY sync pass
        # (rejects missing currency, excess minor-unit precision,
        # unprovisioned currency), including refund/void paths that consume
        # the amounts. A rejection fails this row's savepoint, never the run.
        pay.boundary_money()

        if (pay.status or "").lower() not in _SETTLED_STATUSES:
            # Not settled cash (e.g. refunded / voided). If a prior sync already
            # GL-posted this payment, its receipt journal must be reversed —
            # otherwise ERP keeps the cash on the books after a refund. Handled
            # idempotently; a never-posted payment is simply skipped.
            self._handle_unsettled_payment(pay, external_id, result, created_by_user_id)
            return

        data_hash = self._compute_hash(
            {
                "amount": str(pay.amount),
                "refunded": str(pay.refunded_amount),
                "gross_amount": str(pay.gross_amount),
                "net_amount": str(pay.net_amount),
                "wht_amount": str(pay.wht_amount),
                "wht_rate": str(pay.wht_rate),
                "wht_status": pay.wht_status,
                "wht_record_id": pay.wht_record_id,
                "wht_certificate_reference": pay.wht_certificate_reference,
                # Typed instants formatted back to canonical wire text (the
                # record no longer carries the raw strings).
                "wht_resolved_at": (
                    pay.wht_resolved_at.isoformat() if pay.wht_resolved_at else None
                ),
                "status": pay.status,
                "paid_at": pay.paid_at.isoformat() if pay.paid_at else None,
                "account_id": pay.effective_account_id,
                "channel": pay.payment_channel_id,
                "allocations": sorted(
                    (a.invoice_id, str(a.amount)) for a in pay.allocations
                ),
            }
        )
        if skip_unchanged and not self._has_changed(
            EntityType.PAYMENT, external_id, data_hash
        ):
            payment = self._find_local_payment(external_id)
            if payment is not None:
                # An unchanged source row can still repair a missing ERP
                # projection. Reconciliation is driven from canonical Sub
                # facts, not from the import hash alone.
                self._ensure_synced_payment_posted(payment, created_by_user_id)
                self._ensure_wht_terminal_consequence(
                    payment,
                    pay,
                    created_by_user_id=created_by_user_id,
                )
            result.skipped += 1
            return

        account_id = pay.effective_account_id
        if not account_id:
            result.skipped += 1
            result.errors.append(f"Payment {pay.id}: no billing account")
            return

        customer_id = self._get_customer_for_account(account_id)
        if not customer_id:
            result.skipped += 1
            result.errors.append(
                f"Payment {pay.id}: account {account_id} not resolved to a customer"
            )
            return

        payment_date = pay.paid_at.date() if pay.paid_at else date.today()
        if payment_date < DOTMAC_SUB_SYNC_MIN_DATE:
            self._record_sync(EntityType.PAYMENT, external_id, _PRE_CUTOFF_SENTINEL)
            result.skipped += 1
            return

        customer = self.db.get(Customer, customer_id)
        currency_code = (
            (customer.currency_code if customer else None)
            or pay.currency
            or settings.default_functional_currency_code
        )
        # Net cash actually received = source net cash minus refunds. For a WHT
        # settlement, Sub's payment amount is the gross customer credit while
        # the proof-backed WHT record supplies the smaller bank receipt.
        source_gross = pay.gross_amount if pay.gross_amount is not None else pay.amount
        source_net = pay.net_amount if pay.net_amount is not None else pay.amount
        gross_amount = source_gross - pay.refunded_amount
        net_amount = source_net - pay.refunded_amount
        wht_amount = pay.wht_amount or Decimal("0")
        if gross_amount < 0 or net_amount < 0:
            raise ValueError("Sub payment refund exceeds its source settlement amount")
        # WHT evidence identity (net + WHT = gross) via the E4 adapter, with the
        # pre-existing one-minor-unit sync tolerance for the booking currency.
        check_settlement_identity(
            gross=to_boundary_money(
                gross_amount, currency_code, field=f"Sub payment {pay.id} gross"
            ),
            net=to_boundary_money(
                net_amount, currency_code, field=f"Sub payment {pay.id} net"
            ),
            withheld=to_boundary_money(
                wht_amount, currency_code, field=f"Sub payment {pay.id} WHT"
            ),
            field=f"Sub payment {pay.id}",
        )
        wht_code_id = None
        if wht_amount > Decimal("0"):
            if pay.wht_rate is None:
                raise ValueError("Sub WHT payment is missing its source WHT rate")
            wht_code_id = self._resolve_source_wht_code(
                rate_percent=pay.wht_rate,
                effective_date=payment_date,
            ).tax_code_id

        # A partial refund reduces the cash/AR settlement but does not silently
        # erase an already-recognized WHT receivable.
        #
        # Net cash actually received = gross captured minus refunds. A partial
        # refund (partially_refunded is "settled") reduces this, so the receipt
        # GL and the C2 change-detection post the net — otherwise ERP overstates
        # cash by the refunded amount. A full refund flips status out of settled
        # and is reversed by _handle_unsettled_payment instead.
        exch_rate, functional_amount = self._functional_amount(
            net_amount, currency_code, payment_date
        )
        bank_account_id = self._get_bank_account_for_channel(
            pay.payment_channel_id, currency_code
        )
        method = self._map_payment_method(pay.payment_channel_id)
        channel_name = self._channel_name(pay.payment_channel_id) or "dotmac_sub"

        payment: CustomerPayment | None = self._find_local_payment(external_id)

        if payment is None:
            payment = CustomerPayment(
                organization_id=self.organization_id,
                customer_id=customer_id,
                payment_number=self._generate_payment_number(payment_date),
                payment_date=payment_date,
                payment_method=method,
                currency_code=currency_code,
                gross_amount=gross_amount,
                amount=net_amount,
                wht_code_id=wht_code_id,
                wht_amount=wht_amount,
                wht_certificate_number=pay.wht_certificate_reference,
                exchange_rate=exch_rate,
                functional_currency_amount=functional_amount,
                bank_account_id=bank_account_id,
                reference=pay.external_id,
                description=(
                    f"dotmac_sub payment via {channel_name}. {pay.memo or ''}"
                ).strip(),
                status=PaymentStatus.CLEARED,
                correlation_id=f"dotmac-sub-pmt-{pay.id}",
                created_by_user_id=created_by_user_id or SYSTEM_USER_ID,
                dotmac_sub_id=pay.id,
                dotmac_sub_receipt_number=pay.external_id,
                last_synced_at=datetime.now(UTC),
            )
            self.db.add(payment)
            self.db.flush()
            result.created += 1
        else:
            # If this payment is already GL-posted and a GL-relevant amount is
            # changing (e.g. succeeded -> partially_refunded), the old journal no
            # longer matches. Reverse it and drop the posting link so the
            # post_unposted_payments step re-posts at the new amount. Mutating
            # the amount in place would leave the GL at the old figure while the
            # subledger shows the new one. If the reversal can't be created,
            # leave the payment (and its GL) untouched rather than diverge — the
            # next sync retries.
            if self._posted_amount_changed(
                payment,
                new_amount=net_amount,
                new_gross_amount=gross_amount,
                new_wht_amount=wht_amount,
                new_wht_code_id=wht_code_id,
                new_functional=functional_amount,
            ):
                if not self._reverse_posted_payment_gl(payment, created_by_user_id):
                    result.errors.append(
                        f"Payment {external_id}: GL reversal failed on amount "
                        "change; left unchanged to avoid GL/subledger divergence"
                    )
                    return
            payment.customer_id = customer_id
            payment.payment_date = payment_date
            payment.payment_method = method
            payment.currency_code = currency_code
            payment.gross_amount = gross_amount
            payment.amount = net_amount
            payment.wht_code_id = wht_code_id
            payment.wht_amount = wht_amount
            payment.wht_certificate_number = pay.wht_certificate_reference
            payment.exchange_rate = exch_rate
            payment.functional_currency_amount = functional_amount
            payment.bank_account_id = bank_account_id
            payment.reference = pay.external_id
            payment.dotmac_sub_id = pay.id
            payment.dotmac_sub_receipt_number = pay.external_id
            payment.last_synced_at = datetime.now(UTC)
            result.updated += 1

        self._apply_allocations(payment, pay, payment_date)
        self._ensure_synced_payment_posted(payment, created_by_user_id)
        self._ensure_wht_terminal_consequence(
            payment,
            pay,
            created_by_user_id=created_by_user_id,
        )
        self._record_sync(
            EntityType.PAYMENT, external_id, payment.payment_id, data_hash
        )

    def _ensure_synced_payment_posted(
        self,
        payment: CustomerPayment,
        created_by_user_id: UUID | None,
    ) -> None:
        """Require the ERP-owned balanced receipt journal in the source savepoint."""
        from app.services.finance.ar.customer_payment import CustomerPaymentService

        CustomerPaymentService.ensure_gl_posted(
            self.db,
            payment,
            posted_by_user_id=created_by_user_id,
        )
        if payment.gross_amount != Decimal("0") and payment.journal_entry_id is None:
            raise ValueError(
                "ERP failed to post the synced payment to its canonical GL"
            )

    def _ensure_wht_terminal_consequence(
        self,
        payment: CustomerPayment,
        pay: PaymentRecord,
        *,
        created_by_user_id: UUID | None,
    ) -> None:
        """Post ERP's balanced consequence for a terminal Sub WHT decision.

        Sub owns the evidence-backed reclaimed/written-off decision. ERP owns
        the accounts and journal: reclaim applies the receivable against the
        mapped tax-liability account; write-off moves it to the mapped expense
        account. Missing configuration fails the entire source savepoint.
        """
        terminal_status = (pay.wht_status or "").lower()
        if terminal_status not in {"reclaimed", "written_off"}:
            return
        if payment.wht_amount <= Decimal("0") or payment.wht_code_id is None:
            raise ValueError("Terminal Sub WHT state has no ERP WHT amount/code")
        if not pay.wht_record_id:
            raise ValueError("Terminal Sub WHT state is missing its source record id")

        from app.models.finance.gl.journal_entry import JournalType
        from app.models.finance.tax.tax_code import TaxCode, TaxType
        from app.services.finance.gl.journal import JournalInput, JournalLineInput
        from app.services.finance.posting.base import BasePostingAdapter
        from app.services.finance.posting.idempotency import PostingIdempotencyService

        document_type = (
            "CUSTOMER_PAYMENT_WHT_RECLAIM"
            if terminal_status == "reclaimed"
            else "CUSTOMER_PAYMENT_WHT_WRITE_OFF"
        )
        if PostingIdempotencyService.source_journal_exists(
            self.db,
            source_module="AR",
            source_document_type=document_type,
            source_document_id=payment.payment_id,
            exclude_reversal_journals=True,
        ):
            return

        tax_code = self.db.get(TaxCode, payment.wht_code_id)
        if not (
            tax_code
            and tax_code.organization_id == self.organization_id
            and tax_code.tax_type == TaxType.WITHHOLDING
            and tax_code.tax_paid_account_id
        ):
            raise ValueError("ERP WHT receivable account is not configured")
        debit_account_id = (
            tax_code.tax_collected_account_id
            if terminal_status == "reclaimed"
            else tax_code.tax_expense_account_id
        )
        if debit_account_id is None:
            required = "tax-liability" if terminal_status == "reclaimed" else "expense"
            raise ValueError(
                f"ERP WHT {required} account is required for {terminal_status}"
            )

        resolved_at = pay.wht_resolved_at
        if resolved_at is None:
            raise ValueError(
                "Terminal Sub WHT state is missing its resolution timestamp"
            )
        posting_date = resolved_at.date()
        exchange_rate = payment.exchange_rate or Decimal("1")
        amount = payment.wht_amount
        functional_amount = amount * exchange_rate
        label = "reclaimed" if terminal_status == "reclaimed" else "written off"
        journal_input = JournalInput(
            journal_type=JournalType.STANDARD,
            entry_date=posting_date,
            posting_date=posting_date,
            description=(
                f"Sub WHT {label} for {payment.payment_number} ({pay.wht_record_id})"
            ),
            reference=pay.wht_certificate_reference or pay.wht_record_id,
            currency_code=payment.currency_code,
            exchange_rate=exchange_rate,
            lines=[
                JournalLineInput(
                    account_id=debit_account_id,
                    debit_amount=amount,
                    credit_amount=Decimal("0"),
                    debit_amount_functional=functional_amount,
                    credit_amount_functional=Decimal("0"),
                    description=f"WHT {label}",
                ),
                JournalLineInput(
                    account_id=tax_code.tax_paid_account_id,
                    debit_amount=Decimal("0"),
                    credit_amount=amount,
                    debit_amount_functional=Decimal("0"),
                    credit_amount_functional=functional_amount,
                    description="Clear WHT receivable",
                ),
            ],
            source_module="AR",
            source_document_type=document_type,
            source_document_id=payment.payment_id,
            correlation_id=payment.correlation_id,
        )
        user_id = created_by_user_id or payment.created_by_user_id or SYSTEM_USER_ID
        journal, result = BasePostingAdapter.create_approve_and_post_journal(
            self.db,
            self.organization_id,
            journal_input,
            user_id,
            posting_date=posting_date,
            idempotency_key=BasePostingAdapter.make_idempotency_key(
                self.organization_id,
                "AR:PAY:WHT",
                payment.payment_id,
                action=terminal_status,
            ),
            source_module="AR",
            correlation_id=payment.correlation_id,
            success_message=f"WHT {label} posted successfully",
        )
        if not result.success or journal is None:
            raise ValueError(result.message)

        if terminal_status == "written_off":
            self._record_wht_write_off_tax_reversal(
                payment,
                pay,
                tax_code=tax_code,
                posting_date=posting_date,
                journal_entry_id=journal.journal_entry_id,
            )

    def _record_wht_write_off_tax_reversal(
        self,
        payment: CustomerPayment,
        pay: PaymentRecord,
        *,
        tax_code: Any,
        posting_date: date,
        journal_entry_id: UUID,
    ) -> None:
        """Negate the original WHT tax credit without rewriting its history."""
        from app.models.finance.tax.tax_transaction import (
            TaxRecognitionBasis,
            TaxTransaction,
            TaxTransactionType,
        )
        from app.services.finance.gl.period_guard import PeriodGuardService
        from app.services.finance.tax.tax_transaction import (
            TaxTransactionInput,
            tax_transaction_service,
        )

        existing = self.db.scalar(
            select(TaxTransaction.transaction_id).where(
                TaxTransaction.organization_id == self.organization_id,
                TaxTransaction.source_document_type == "CUSTOMER_PAYMENT_WHT_WRITE_OFF",
                TaxTransaction.source_document_id == payment.payment_id,
            )
        )
        if existing is not None:
            return
        fiscal_period = PeriodGuardService.get_period_for_date(
            self.db, self.organization_id, posting_date
        )
        if fiscal_period is None:
            raise ValueError(
                f"ERP fiscal period is not configured for WHT write-off {posting_date}"
            )
        exchange_rate = payment.exchange_rate or Decimal("1")
        tax_transaction = tax_transaction_service.create_transaction(
            db=self.db,
            organization_id=self.organization_id,
            input=TaxTransactionInput(
                fiscal_period_id=fiscal_period.fiscal_period_id,
                tax_code_id=tax_code.tax_code_id,
                jurisdiction_id=tax_code.jurisdiction_id,
                transaction_type=TaxTransactionType.WITHHOLDING,
                recognition_basis=TaxRecognitionBasis.ACCRUAL,
                transaction_date=posting_date,
                source_document_type="CUSTOMER_PAYMENT_WHT_WRITE_OFF",
                source_document_id=payment.payment_id,
                source_document_reference=pay.wht_record_id,
                currency_code=payment.currency_code,
                base_amount=Decimal("0"),
                tax_rate=tax_code.tax_rate,
                tax_amount=-payment.wht_amount,
                functional_base_amount=Decimal("0"),
                functional_tax_amount=-(payment.wht_amount * exchange_rate),
                exchange_rate=exchange_rate,
            ),
        )
        tax_transaction.journal_entry_id = journal_entry_id

    @staticmethod
    def _posted_amount_changed(
        payment: CustomerPayment,
        *,
        new_amount: Decimal,
        new_gross_amount: Decimal,
        new_wht_amount: Decimal,
        new_wht_code_id: UUID | None,
        new_functional: Decimal,
    ) -> bool:
        """True when a GL-posted payment's cash amount is materially changing —
        the case that needs a GL reversal, not an in-place mutation."""
        return payment.journal_entry_id is not None and (
            payment.amount != new_amount
            or payment.gross_amount != new_gross_amount
            or payment.wht_amount != new_wht_amount
            or payment.wht_code_id != new_wht_code_id
            or payment.functional_currency_amount != new_functional
        )

    def _reverse_posted_payment_gl(
        self,
        payment: CustomerPayment,
        created_by_user_id: UUID | None,
        *,
        reason: str | None = None,
        idempotency_suffix: str = "resync-reversal",
    ) -> bool:
        """Reverse a posted payment's GL journal and clear its posting link.

        Used two ways: on an amount change (the caller then leaves status CLEARED
        so ``post_unposted_payments`` re-posts at the new amount), and on a refund
        (the caller then sets status REVERSED so it is NOT re-posted). ``reason``
        and ``idempotency_suffix`` distinguish the two in the GL + idempotency key.

        Returns True on success. On any failure the posting link is left intact
        so the caller can decline to mutate the payment (no GL/subledger drift).
        Reversal is idempotent on the original journal id.
        """
        from app.services.finance.gl.reversal import ReversalService

        journal_id = payment.journal_entry_id
        if journal_id is None:
            return False
        user_id = created_by_user_id or payment.created_by_user_id or SYSTEM_USER_ID
        try:
            result = ReversalService.create_reversal(
                db=self.db,
                organization_id=self.organization_id,
                original_journal_id=journal_id,
                reversal_date=date.today(),
                created_by_user_id=user_id,
                reason=reason
                or (
                    "dotmac_sub payment amount changed on resync "
                    f"({payment.dotmac_sub_id})"
                ),
                auto_post=True,
                idempotency_key=(
                    f"{self.organization_id}:AR:PAY:{payment.payment_id}"
                    f":{idempotency_suffix}:{journal_id}"
                ),
            )
        except Exception:
            logger.exception("GL reversal errored for payment %s", payment.payment_id)
            return False

        if not getattr(result, "success", False):
            logger.error(
                "GL reversal failed for payment %s: %s",
                payment.payment_id,
                getattr(result, "message", "unknown"),
            )
            return False

        payment.journal_entry_id = None
        payment.posting_batch_id = None
        logger.info(
            "Reversed GL journal %s for payment %s (%s)",
            journal_id,
            payment.payment_id,
            idempotency_suffix,
        )
        return True

    def _find_local_payment(self, external_id: str) -> CustomerPayment | None:
        """Locate a previously-synced ERP payment for a dotmac_sub payment id —
        by the sync-state link first, then by ``dotmac_sub_id``."""
        local_id = self._get_synced_entity(EntityType.PAYMENT, external_id)
        payment: CustomerPayment | None = None
        if local_id and local_id != _PRE_CUTOFF_SENTINEL:
            payment = self.db.get(CustomerPayment, local_id)
        if payment is None:
            payment = self.db.scalar(
                select(CustomerPayment)
                .where(
                    CustomerPayment.organization_id == self.organization_id,
                    CustomerPayment.dotmac_sub_id == external_id,
                )
                .order_by(CustomerPayment.created_at)
            )
        return payment

    def _handle_unsettled_payment(
        self,
        pay: PaymentRecord,
        external_id: str,
        result: SyncResult,
        created_by_user_id: UUID | None,
    ) -> None:
        """Handle a dotmac_sub payment that is no longer settled cash (refunded /
        voided / failed).

        If a prior sync GL-posted it, reverse that receipt journal and mark the
        payment REVERSED so ``post_unposted_payments`` (which requires CLEARED)
        won't re-post it — closing the gap where a full refund left ERP
        overstating cash forever. Idempotent: an already-reversed payment is
        recorded and skipped.
        """
        payment = self._find_local_payment(external_id)
        status_hash = self._compute_hash(
            {"status": (pay.status or "").lower(), "amount": str(pay.amount)}
        )
        if payment is None:
            # Never synced/posted here — nothing on the GL to reverse.
            result.skipped += 1
            return
        if (
            payment.status == PaymentStatus.REVERSED
            and payment.journal_entry_id is None
        ):
            self._record_sync(
                EntityType.PAYMENT, external_id, payment.payment_id, status_hash
            )
            result.skipped += 1
            return
        if payment.journal_entry_id is not None:
            if not self._reverse_posted_payment_gl(
                payment,
                created_by_user_id,
                reason=(
                    f"dotmac_sub payment {(pay.status or 'unsettled').lower()} "
                    f"({payment.dotmac_sub_id})"
                ),
                idempotency_suffix="refund-reversal",
            ):
                result.errors.append(
                    f"Payment {external_id}: GL reversal failed on "
                    f"{pay.status}; left unchanged to avoid GL/subledger divergence"
                )
                return
        payment.status = PaymentStatus.REVERSED
        payment.last_synced_at = datetime.now(UTC)
        self._record_sync(
            EntityType.PAYMENT, external_id, payment.payment_id, status_hash
        )
        result.updated += 1

    def _apply_allocations(
        self, payment: CustomerPayment, pay: PaymentRecord, payment_date: date
    ) -> None:
        from sqlalchemy import delete

        # Rebuild this payment's allocation records (payment -> invoice linkage,
        # used for AR aging / GL). Deleting is a no-op when none exist.
        self.db.execute(
            delete(PaymentAllocation).where(
                PaymentAllocation.payment_id == payment.payment_id
            )
        )

        for a in pay.allocations:
            inv = self.db.scalar(
                select(Invoice).where(
                    Invoice.organization_id == self.organization_id,
                    Invoice.dotmac_sub_id == a.invoice_id,
                )
            )
            if not inv:
                continue
            self.db.add(
                PaymentAllocation(
                    payment_id=payment.payment_id,
                    invoice_id=inv.invoice_id,
                    allocated_amount=a.amount,
                    allocation_date=payment_date,
                )
            )
            # NOTE: we deliberately do NOT touch inv.amount_paid / inv.status
            # here. The invoice sync owns them and sets them from dotmac_sub's
            # authoritative balance_due (_invoices.py: amount_paid = total -
            # balance_due), which already reflects this payment. Incrementing
            # amount_paid here double-counted the paid amount and flipped
            # partially-paid invoices to PAID on first import.

    def _map_payment_method(self, channel_id: str | None) -> PaymentMethod:
        name = (self._channel_name(channel_id) or "").lower()
        if "cash" in name:
            return PaymentMethod.CASH
        if "card" in name:
            return PaymentMethod.CARD
        return PaymentMethod.BANK_TRANSFER

    def post_unposted_payments(
        self,
        created_by_user_id: UUID | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Post CLEARED dotmac_sub payments to the GL — standard AR behaviour.

        The reseller→subscriber link is a customer-grouping dimension only (the main
        company "dotmac" is itself a reseller), so the sync applies NO special
        wholesale suppression: every CLEARED synced payment posts to the GL
        exactly like the rest of the ERP's AR payments. ``parent_customer_id``
        stays grouping-only, consistent with existing ERP convention — this sync
        must not mutate regular GL behaviour.
        """
        from app.services.finance.ar.customer_payment import CustomerPaymentService

        stats: dict[str, Any] = {"posted": 0, "errors": []}

        stmt = select(CustomerPayment).where(
            CustomerPayment.organization_id == self.organization_id,
            CustomerPayment.status == PaymentStatus.CLEARED,
            CustomerPayment.journal_entry_id.is_(None),
            CustomerPayment.dotmac_sub_id.is_not(None),
        )
        if limit is not None:
            stmt = stmt.limit(max(limit, 1))
        for payment in self.db.scalars(stmt).all():
            savepoint = self.db.begin_nested()
            try:
                if CustomerPaymentService.ensure_gl_posted(
                    self.db, payment, posted_by_user_id=created_by_user_id
                ):
                    stats["posted"] += 1
                if (
                    payment.gross_amount != Decimal("0")
                    and payment.journal_entry_id is None
                ):
                    raise ValueError(
                        "ERP failed to post the synced payment to its canonical GL"
                    )
                savepoint.commit()
            except Exception as e:  # noqa: BLE001
                savepoint.rollback()
                logger.exception("Failed to GL-post payment %s", payment.payment_id)
                stats["errors"].append(f"Payment {payment.payment_number}: {e}")
        return stats
