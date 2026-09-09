"""
Payment web view service.

Provides view-focused data for payment-related web routes.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.services.common import (
    PaginationParams,
    apply_search,
    coerce_uuid,
    paginate,
    pagination_context,
)
from app.services.common_filters import build_active_filters
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


class PaymentWebService:
    """View service for payment web routes."""

    @staticmethod
    def payment_callback_context(
        db: Session,
        reference: str,
        trxref: str | None = None,
    ) -> dict:
        from app.models.finance.payments import PaymentIntent, PaymentIntentStatus

        ref = reference or trxref or ""
        intent = db.scalar(
            select(PaymentIntent).where(PaymentIntent.paystack_reference == ref)
        )

        status = "unknown"
        message = "Payment status unknown. Please contact support."
        invoice_number = None
        customer_payment_id = None
        amount = None
        currency = None

        if intent:
            amount = float(intent.amount)
            currency = intent.currency_code
            invoice_number = (
                intent.intent_metadata.get("invoice_number")
                if intent.intent_metadata
                else None
            )
            customer_payment_id = intent.customer_payment_id

            if intent.status == PaymentIntentStatus.COMPLETED:
                status = "success"
                message = "Payment successful! Your invoice has been paid."
            elif intent.status == PaymentIntentStatus.FAILED:
                status = "failed"
                error = (
                    intent.gateway_response.get("error", "Payment failed")
                    if intent.gateway_response
                    else "Payment failed"
                )
                message = f"Payment failed: {error}. Please try again."
            elif intent.status == PaymentIntentStatus.ABANDONED:
                status = "abandoned"
                message = "Payment was cancelled. Please try again if you wish to complete the payment."
            elif intent.status in [
                PaymentIntentStatus.PENDING,
                PaymentIntentStatus.PROCESSING,
            ]:
                status = "pending"
                message = (
                    "Payment is being processed. You will receive confirmation shortly."
                )
            elif intent.status == PaymentIntentStatus.EXPIRED:
                status = "expired"
                message = "Payment session expired. Please initiate a new payment."
            elif intent.status == PaymentIntentStatus.INDETERMINATE:
                # The default below already says "unknown", but by accident.
                # Stated explicitly so the page cannot silently start telling
                # somebody their payment failed if the default ever changes.
                status = "unknown"
                message = (
                    "We could not confirm what happened to this payment. Do "
                    "not pay again - support is reconciling it."
                )

        return {
            "title": f"Payment {status.title()}",
            "status": status,
            "message": message,
            "reference": ref,
            "invoice_number": invoice_number,
            "amount": amount,
            "currency": currency,
            "customer_payment_id": str(customer_payment_id)
            if customer_payment_id
            else None,
        }

    @staticmethod
    def pay_invoice_context(db: Session, organization_id, invoice_id: str) -> dict:
        from app.models.finance.ar.customer import Customer
        from app.models.finance.ar.invoice import Invoice, InvoiceStatus

        invoice = db.get(Invoice, coerce_uuid(invoice_id))
        if not invoice or invoice.organization_id != organization_id:
            return {"redirect_url": "/finance/ar/invoices"}

        payable_statuses = [
            InvoiceStatus.POSTED,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.OVERDUE,
        ]
        is_payable = invoice.status in payable_statuses and invoice.balance_due > 0

        customer = (
            db.get(Customer, invoice.customer_id) if invoice.customer_id else None
        )

        paystack_enabled = resolve_value(
            db,
            SettingDomain.payments,
            "paystack_enabled",
            organization_id=organization_id,
        )
        paystack_public_key = resolve_value(
            db,
            SettingDomain.payments,
            "paystack_public_key",
            organization_id=organization_id,
        )

        contact_email = None
        if customer and isinstance(customer.primary_contact, dict):
            contact_email = customer.primary_contact.get("email")

        return {
            "context": {
                "page_title": f"Pay Invoice {invoice.invoice_number}",
                "invoice": invoice,
                "customer": customer,
                "is_payable": is_payable,
                "paystack_enabled": bool(paystack_enabled),
                "paystack_public_key": str(paystack_public_key)
                if paystack_public_key
                else None,
                "has_email": bool(contact_email),
            }
        }

    @staticmethod
    def reimburse_expense_context(
        db: Session, organization_id, expense_claim_id: str
    ) -> dict:
        from app.models.expense.expense_claim import ExpenseClaim, ExpenseClaimStatus
        from app.models.finance.payments.payment_intent import (
            PaymentIntent,
            PaymentIntentStatus,
        )
        from app.models.people.hr.employee import Employee

        expense_claim = db.get(ExpenseClaim, coerce_uuid(expense_claim_id))
        if not expense_claim or expense_claim.organization_id != organization_id:
            return {"redirect_url": "/expense/claims"}

        # Check for existing active payment intent to prevent duplicate payments.
        #
        # INDETERMINATE counts as active. It is not "in progress" in any
        # ordinary sense - nothing is being processed and nobody is waiting on
        # a webhook - but it has the one property that matters here: the money
        # may already have left. Omitting it would leave `active_intent` None,
        # `can_reimburse` True, and a Reimburse button on screen for a claim
        # whose payout is unaccounted for (ADR-0007).
        active_statuses = [
            PaymentIntentStatus.PENDING,
            PaymentIntentStatus.PROCESSING,
            PaymentIntentStatus.INDETERMINATE,
        ]
        active_intent = db.scalar(
            select(PaymentIntent)
            .where(
                PaymentIntent.organization_id == organization_id,
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.source_id == expense_claim.claim_id,
                PaymentIntent.status.in_(active_statuses),
            )
            .order_by(PaymentIntent.created_at.desc())
            .limit(1)
        )

        latest_intent = db.scalar(
            select(PaymentIntent)
            .where(
                PaymentIntent.organization_id == organization_id,
                PaymentIntent.source_type == "EXPENSE_CLAIM",
                PaymentIntent.source_id == expense_claim.claim_id,
            )
            .order_by(PaymentIntent.created_at.desc())
            .limit(1)
        )

        # Every member here asserts the money did NOT move, which is what makes
        # a retry safe. INDETERMINATE asserts nothing and is deliberately
        # absent - `reset_expense_payment_intent` refuses it outright, force or
        # not, so offering the button would only produce a 409.
        resettable_statuses = {
            PaymentIntentStatus.FAILED,
            PaymentIntentStatus.ABANDONED,
            PaymentIntentStatus.EXPIRED,
        }

        can_reimburse = (
            expense_claim.status == ExpenseClaimStatus.APPROVED
            and active_intent is None
        )
        employee = (
            db.get(Employee, expense_claim.employee_id)
            if expense_claim.employee_id
            else None
        )

        paystack_enabled = resolve_value(
            db,
            SettingDomain.payments,
            "paystack_enabled",
            organization_id=organization_id,
        )
        transfers_enabled = resolve_value(
            db,
            SettingDomain.payments,
            "paystack_transfers_enabled",
            organization_id=organization_id,
        )

        # A PENDING intent with no transfer_code means step 2 (initiate)
        # was never completed — allow the user to retry.
        can_retry_transfer = (
            active_intent is not None
            and active_intent.status == PaymentIntentStatus.PENDING
            and not active_intent.transfer_code
        )

        can_reset_intent = (
            latest_intent is not None and latest_intent.status in resettable_statuses
        )
        latest_intent_status = latest_intent.status.value if latest_intent else None
        latest_intent_error = None
        if latest_intent and isinstance(latest_intent.gateway_response, dict):
            raw_error = latest_intent.gateway_response.get("error")
            if raw_error:
                latest_intent_error = str(raw_error)

        return {
            "context": {
                "page_title": f"Reimburse {expense_claim.claim_number}",
                "expense_claim": expense_claim,
                "employee": employee,
                "claim_bank_code": expense_claim.recipient_bank_code or "",
                "claim_bank_name": expense_claim.recipient_bank_name or "",
                "claim_account_number": expense_claim.recipient_account_number or "",
                "claim_recipient_name": expense_claim.recipient_name or "",
                "can_reimburse": can_reimburse,
                "paystack_enabled": bool(paystack_enabled),
                "transfers_enabled": bool(transfers_enabled),
                "has_active_payment": active_intent is not None,
                "active_intent_status": active_intent.status.value
                if active_intent
                else None,
                "active_intent_id": str(active_intent.intent_id)
                if active_intent
                else None,
                "can_retry_transfer": can_retry_transfer,
                "can_reset_intent": can_reset_intent,
                "latest_intent_status": latest_intent_status,
                "latest_intent_error": latest_intent_error,
                "latest_intent_id": str(latest_intent.intent_id)
                if latest_intent
                else None,
            }
        }

    @staticmethod
    def transfer_list_context(
        db: Session,
        organization_id,
        search: str | None,
        status: str | None,
        page: int,
        per_page: int = 25,
    ) -> dict:
        from app.models.finance.payments import (
            PaymentDirection,
            PaymentIntent,
            PaymentIntentStatus,
        )

        # Base filter: org + outbound direction
        base_filter = [
            PaymentIntent.organization_id == organization_id,
            PaymentIntent.direction == PaymentDirection.OUTBOUND,
        ]

        # Stat card counts (unfiltered by search/status)
        total_count = (
            db.scalar(
                select(func.count()).select_from(PaymentIntent).where(*base_filter)
            )
            or 0
        )
        total_amount = db.scalar(
            select(func.coalesce(func.sum(PaymentIntent.amount), 0)).where(*base_filter)
        )
        pending_count = (
            db.scalar(
                select(func.count())
                .select_from(PaymentIntent)
                .where(
                    *base_filter, PaymentIntent.status == PaymentIntentStatus.PENDING
                )
            )
            or 0
        )
        processing_count = (
            db.scalar(
                select(func.count())
                .select_from(PaymentIntent)
                .where(
                    *base_filter, PaymentIntent.status == PaymentIntentStatus.PROCESSING
                )
            )
            or 0
        )
        completed_count = (
            db.scalar(
                select(func.count())
                .select_from(PaymentIntent)
                .where(
                    *base_filter, PaymentIntent.status == PaymentIntentStatus.COMPLETED
                )
            )
            or 0
        )
        indeterminate_count = (
            db.scalar(
                select(func.count())
                .select_from(PaymentIntent)
                .where(
                    *base_filter,
                    PaymentIntent.status == PaymentIntentStatus.INDETERMINATE,
                )
            )
            or 0
        )
        failed_count = (
            db.scalar(
                select(func.count())
                .select_from(PaymentIntent)
                .where(*base_filter, PaymentIntent.status == PaymentIntentStatus.FAILED)
            )
            or 0
        )

        # Filtered query for listing
        stmt = select(PaymentIntent).where(*base_filter)
        stmt = apply_search(
            stmt,
            search,
            PaymentIntent.paystack_reference,
            PaymentIntent.recipient_account_name,
            PaymentIntent.recipient_account_number,
            PaymentIntent.transfer_code,
        )

        if status:
            try:
                status_enum = PaymentIntentStatus(status.upper())
                stmt = stmt.where(PaymentIntent.status == status_enum)
            except ValueError:
                pass

        result = paginate(
            db,
            stmt.order_by(PaymentIntent.created_at.desc()),
            PaginationParams.from_page(page, per_page),
        )

        from app.services.formatters import format_currency

        active_filters = build_active_filters(
            params={"status": status, "search": search},
            labels={"status": "Status", "search": "Search"},
            options={"status": {s.value: s.value.title() for s in PaymentIntentStatus}},
        )

        return {
            "intents": result.items,
            "search": search or "",
            "status_filter": status,
            "statuses": [s.value for s in PaymentIntentStatus],
            "active_filters": active_filters,
            **pagination_context(result),
            # Stat card data
            "stat_total_count": total_count,
            "stat_total_amount": format_currency(
                total_amount, settings.default_functional_currency_code
            ),
            "stat_pending": pending_count,
            "stat_processing": processing_count,
            "stat_completed": completed_count,
            "stat_failed": failed_count,
            "stat_indeterminate": indeterminate_count,
        }

    @staticmethod
    def payment_history_context(
        db: Session,
        organization_id,
        status: str | None,
        page: int,
        per_page: int = 20,
    ) -> dict:
        from app.models.finance.payments import PaymentIntent, PaymentIntentStatus

        stmt = select(PaymentIntent).where(
            PaymentIntent.organization_id == organization_id
        )

        if status:
            try:
                status_enum = PaymentIntentStatus(status.upper())
                stmt = stmt.where(PaymentIntent.status == status_enum)
            except ValueError:
                pass

        # paginate() for the count/fetch mechanics; this view keeps its bespoke
        # context keys (current_page/total) and unclamped total_pages.
        result = paginate(
            db,
            stmt.order_by(PaymentIntent.created_at.desc()),
            PaginationParams.from_page(page, per_page),
        )

        return {
            "intents": result.items,
            "current_page": page,
            "total_pages": (result.total + per_page - 1) // per_page,
            "total": result.total,
            "status_filter": status,
            "statuses": [s.value for s in PaymentIntentStatus],
        }


payment_web_service = PaymentWebService()
