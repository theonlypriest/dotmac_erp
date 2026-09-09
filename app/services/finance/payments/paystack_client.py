"""
Paystack API Client.

Handles all HTTP communication with Paystack API.
"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx

from app.config import settings
from app.metrics import categorize_http_status, observe_integration_request

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    """Paystack API error.

    Raised when Paystack ANSWERED and the answer was a refusal: a parsed
    ``status: false`` body, or a 4xx that says the request was understood and
    rejected. A caller may treat this as a statement about the thing it asked
    about.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PaystackUnreachable(PaystackError):
    """Paystack did NOT answer, so nothing was learned about the money.

    Connection refused, DNS failure, read/connect timeout, TLS error, a 5xx
    from the edge — every case where the request may or may not have taken
    effect upstream and we have no way to tell from here.

    It subclasses ``PaystackError`` so existing ``except PaystackError``
    handlers keep working unchanged; what the subclass buys is that a handler
    which is about to write a VERDICT can ask whether it actually has one.
    Nothing about this exception licenses a caller to record FAILED: "we could
    not reach Paystack" and "Paystack told us this failed" are different claims
    (ADR-0007; fleet rule in `dotmac_starter_mt` ADR-0032).
    """


def _from_http_status(message: str, error: httpx.HTTPStatusError) -> PaystackError:
    """Classify an HTTP error response into verdict vs non-observation.

    5xx is the provider failing to answer — the request may well have been
    applied on their side, which is precisely why it is not evidence of
    anything. 4xx is an answer: Paystack parsed the request and refused it.

    Deliberately narrow. A 401 from a rotated key is technically also "we
    learned nothing about the transfer", but it is a configuration fault with
    its own loud failure mode, and widening the unreachable class to cover it
    would park every mis-keyed deployment's payouts in INDETERMINATE. The
    give-up path in ``PaymentService`` defends the money-critical direction
    separately: there, anything that is not an affirmative provider verdict is
    treated as unobserved.
    """
    status_code = error.response.status_code
    factory = PaystackUnreachable if status_code >= 500 else PaystackError
    return factory(message, status_code=status_code)


@dataclass
class PaystackConfig:
    """Configuration for Paystack API."""

    secret_key: str
    public_key: str
    webhook_secret: str


@dataclass
class InitializeResponse:
    """Response from transaction initialization."""

    authorization_url: str
    access_code: str
    reference: str


@dataclass
class VerifyResponse:
    """Response from transaction verification."""

    status: str  # success, failed, abandoned
    reference: str
    amount: int  # in kobo (smallest currency unit)
    currency: str
    transaction_id: str
    paid_at: str | None
    channel: str  # card, bank, ussd, etc.
    gateway_response: str
    customer_email: str
    metadata: dict


@dataclass
class ResolveAccountResponse:
    """Response from account number resolution."""

    account_number: str
    account_name: str
    bank_id: int


@dataclass
class CreateRecipientResponse:
    """Response from transfer recipient creation."""

    recipient_code: str
    name: str
    type: str  # nuban, mobile_money, basa
    bank_code: str
    account_number: str


@dataclass
class InitiateTransferResponse:
    """Response from transfer initiation."""

    transfer_code: str
    reference: str
    status: str  # pending, success, failed
    amount: int
    currency: str


@dataclass
class VerifyTransferResponse:
    """Response from transfer verification."""

    status: str  # pending, success, failed, reversed
    reference: str
    amount: int
    currency: str
    transfer_code: str
    recipient_code: str
    completed_at: str | None = None
    reason: str | None = None
    fee: int | None = None


@dataclass
class TransactionRecord:
    """A single transaction record from Paystack."""

    id: int
    reference: str
    amount: int  # in kobo
    currency: str
    status: str
    paid_at: str | None
    created_at: str
    channel: str
    customer_email: str
    fees: int
    metadata: dict


@dataclass
class TransferRecord:
    """A single transfer record from Paystack."""

    id: int
    transfer_code: str
    reference: str
    amount: int  # in kobo
    currency: str
    status: str
    reason: str | None
    created_at: str
    updated_at: str | None
    recipient_name: str
    recipient_account_number: str
    recipient_bank_code: str
    fees: int | None = None


@dataclass
class SettlementRecord:
    """A settlement record from Paystack (payout to merchant bank)."""

    id: int
    settlement_date: str
    status: str  # pending, success
    total_amount: int  # in kobo - gross amount settled
    total_fees: int  # in kobo - fees deducted
    net_amount: int  # in kobo - amount actually sent to bank
    currency: str
    created_at: str


@dataclass
class SettlementTransaction:
    """A transaction within a settlement."""

    id: int
    reference: str
    amount: int  # in kobo (gross)
    fees: int  # in kobo
    net_amount: int  # in kobo (amount - fees)
    currency: str
    status: str
    paid_at: str | None
    created_at: str
    customer_email: str
    channel: str


@dataclass
class Bank:
    """Bank information."""

    name: str
    code: str
    country: str
    currency: str
    type: str  # nuban, mobile_money


@dataclass
class PaystackCustomer:
    """A customer in Paystack."""

    id: int
    customer_code: str
    email: str
    first_name: str | None
    last_name: str | None
    phone: str | None
    metadata: dict
    created_at: str


class PaystackClient:
    """
    HTTP client for Paystack API.

    Handles transaction initialization, verification, and webhook signature
    verification.
    """

    def __init__(self, config: PaystackConfig, timeout: float = 30.0):
        self.config = config
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=PAYSTACK_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.config.secret_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        metric_status = "unknown"
        try:
            response = self._get_client().request(
                method=method,
                url=path,
                params=params,
                json=json,
            )
            metric_status = categorize_http_status(response.status_code)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
        except httpx.HTTPStatusError:
            observe_integration_request(
                "paystack",
                operation,
                metric_status,
                max(time.perf_counter() - started_at, 0.0),
            )
            raise
        except httpx.RequestError:
            observe_integration_request(
                "paystack",
                operation,
                "request_error",
                max(time.perf_counter() - started_at, 0.0),
            )
            raise
        finally:
            if metric_status == "success":
                observe_integration_request(
                    "paystack",
                    operation,
                    metric_status,
                    max(time.perf_counter() - started_at, 0.0),
                )

    def initialize_transaction(
        self,
        email: str,
        amount: int,  # Amount in the smallest currency unit
        reference: str,
        callback_url: str,
        metadata: dict | None = None,
        currency: str = settings.default_functional_currency_code,
    ) -> InitializeResponse:
        """
        Initialize a payment transaction.

        Args:
            email: Customer email address
            amount: Amount in kobo (smallest currency unit)
            reference: Unique transaction reference
            callback_url: URL to redirect after payment
            metadata: Optional custom metadata
            currency: Currency code (defaults to configured application currency)

        Returns:
            InitializeResponse with authorization URL for customer redirect

        Raises:
            PaystackError: If initialization fails
        """
        payload: dict = {
            "email": email,
            "amount": amount,
            "reference": reference,
            "callback_url": callback_url,
            "currency": currency,
        }
        if metadata:
            payload["metadata"] = metadata

        try:
            data = self._request(
                "POST",
                "/transaction/initialize",
                operation="initialize_transaction",
                json=payload,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack initialize failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to initialize transaction: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")

        if not data.get("status"):
            raise PaystackError(data.get("message", "Initialize failed"))

        return InitializeResponse(
            authorization_url=data["data"]["authorization_url"],
            access_code=data["data"]["access_code"],
            reference=data["data"]["reference"],
        )

    def verify_transaction(self, reference: str) -> VerifyResponse:
        """
        Verify a transaction by reference.

        Args:
            reference: Transaction reference

        Returns:
            VerifyResponse with transaction details

        Raises:
            PaystackError: If verification fails
        """
        try:
            data = self._request(
                "GET",
                f"/transaction/verify/{reference}",
                operation="verify_transaction",
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack verify failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to verify transaction: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")

        if not data.get("status"):
            raise PaystackError(data.get("message", "Verify failed"))

        d = data["data"]
        return VerifyResponse(
            status=d["status"],
            reference=d["reference"],
            amount=d["amount"],
            currency=d["currency"],
            transaction_id=str(d["id"]),
            paid_at=d.get("paid_at"),
            channel=d.get("channel", "unknown"),
            gateway_response=d.get("gateway_response", ""),
            customer_email=d["customer"]["email"],
            metadata=d.get("metadata") or {},
        )

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verify Paystack webhook signature.

        Paystack signs webhooks with HMAC-SHA512 using the secret key.

        Args:
            payload: Raw request body bytes
            signature: X-Paystack-Signature header value

        Returns:
            True if signature is valid, False otherwise

        Raises:
            ValueError: If webhook secret is not configured (security requirement)
        """
        if not self.config.webhook_secret:
            logger.error(
                "SECURITY: Webhook secret not configured - rejecting webhook. "
                "Set paystack_webhook_secret in payment settings."
            )
            raise ValueError(
                "Webhook secret not configured. Cannot verify webhook authenticity."
            )

        if not signature:
            logger.warning("SECURITY: Webhook received without signature header")
            return False

        expected = hmac.new(
            self.config.webhook_secret.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()

        is_valid = hmac.compare_digest(expected, signature)
        if not is_valid:
            logger.warning(
                "SECURITY: Webhook signature mismatch - possible spoofing attempt"
            )
        return is_valid

    # =========================================================================
    # Transfer API (for expense reimbursements / payouts)
    # =========================================================================

    def list_banks(
        self,
        country: str = "nigeria",
        currency: str = settings.default_functional_currency_code,
    ) -> list[Bank]:
        """
        Get list of banks supported by Paystack.

        Args:
            country: Country to get banks for (default: nigeria)
            currency: Currency code (defaults to configured application currency)

        Returns:
            List of Bank objects
        """
        try:
            data = self._request(
                "GET",
                "/bank",
                operation="list_banks",
                params={"country": country, "currency": currency},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack list banks failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to list banks: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "List banks failed"))

        return [
            Bank(
                name=b["name"],
                code=b["code"],
                country=b.get("country", country),
                currency=b.get("currency", currency),
                type=b.get("type", "nuban"),
            )
            for b in data["data"]
        ]

    def resolve_account(
        self,
        account_number: str,
        bank_code: str,
    ) -> ResolveAccountResponse:
        """
        Verify an account number and get the account name.

        Args:
            account_number: Bank account number
            bank_code: Bank code (from list_banks)

        Returns:
            ResolveAccountResponse with verified account details
        """
        try:
            data = self._request(
                "GET",
                "/bank/resolve",
                operation="resolve_account",
                params={
                    "account_number": account_number,
                    "bank_code": bank_code,
                },
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack resolve account failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to resolve account: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Resolve account failed"))

        d = data["data"]
        return ResolveAccountResponse(
            account_number=d["account_number"],
            account_name=d["account_name"],
            bank_id=d.get("bank_id", 0),
        )

    def create_transfer_recipient(
        self,
        name: str,
        account_number: str,
        bank_code: str,
        currency: str = settings.default_functional_currency_code,
        recipient_type: str = "nuban",
        description: str | None = None,
        metadata: dict | None = None,
    ) -> CreateRecipientResponse:
        """
        Create a transfer recipient for payouts.

        Args:
            name: Recipient name (should match bank account name)
            account_number: Recipient bank account number
            bank_code: Bank code (from list_banks)
            currency: Currency code (defaults to configured application currency)
            recipient_type: Type of recipient (nuban, mobile_money, basa)
            description: Optional description
            metadata: Optional metadata

        Returns:
            CreateRecipientResponse with recipient_code for transfers
        """
        payload: dict = {
            "type": recipient_type,
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": currency,
        }
        if description:
            payload["description"] = description
        if metadata:
            payload["metadata"] = metadata

        try:
            data = self._request(
                "POST",
                "/transferrecipient",
                operation="create_transfer_recipient",
                json=payload,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack create recipient failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to create recipient: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Create recipient failed"))

        d = data["data"]
        return CreateRecipientResponse(
            recipient_code=d["recipient_code"],
            name=d["name"],
            type=d["type"],
            bank_code=d["details"]["bank_code"],
            account_number=d["details"]["account_number"],
        )

    def initiate_transfer(
        self,
        amount: int,  # Amount in kobo
        recipient_code: str,
        reference: str,
        reason: str | None = None,
        currency: str = settings.default_functional_currency_code,
    ) -> InitiateTransferResponse:
        """
        Initiate a transfer to a recipient.

        Args:
            amount: Amount in kobo (smallest currency unit)
            recipient_code: Recipient code from create_transfer_recipient
            reference: Unique transfer reference
            reason: Optional reason for transfer
            currency: Currency code (defaults to configured application currency)

        Returns:
            InitiateTransferResponse with transfer_code
        """
        payload: dict = {
            "source": "balance",  # Transfer from Paystack balance
            "amount": amount,
            "recipient": recipient_code,
            "reference": reference,
            "currency": currency,
        }
        if reason:
            payload["reason"] = reason

        try:
            data = self._request(
                "POST",
                "/transfer",
                operation="initiate_transfer",
                json=payload,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack initiate transfer failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to initiate transfer: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Initiate transfer failed"))

        d = data["data"]
        return InitiateTransferResponse(
            transfer_code=d["transfer_code"],
            reference=d["reference"],
            status=d["status"],
            amount=d["amount"],
            currency=d["currency"],
        )

    def verify_transfer(self, reference: str) -> VerifyTransferResponse:
        """
        Verify a transfer by reference.

        Args:
            reference: Transfer reference

        Returns:
            VerifyTransferResponse with transfer details
        """
        try:
            data = self._request(
                "GET",
                f"/transfer/verify/{reference}",
                operation="verify_transfer",
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack verify transfer failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to verify transfer: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Verify transfer failed"))

        d = data["data"]
        return VerifyTransferResponse(
            status=d["status"],
            reference=d["reference"],
            amount=d["amount"],
            currency=d["currency"],
            transfer_code=d["transfer_code"],
            recipient_code=d["recipient"]["recipient_code"],
            completed_at=d.get("completed_at"),
            reason=d.get("reason"),
            fee=d.get("fee") or d.get("fees"),  # Paystack uses 'fee' or 'fees'
        )

    def list_transactions(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        per_page: int = 100,
        page: int = 1,
    ) -> list[TransactionRecord]:
        """
        List transactions (inbound payments/collections).

        Args:
            from_date: Start date (ISO format: 2026-01-01T00:00:00.000Z)
            to_date: End date (ISO format)
            status: Filter by status (success, failed, abandoned)
            per_page: Number of records per page (max 100)
            page: Page number

        Returns:
            List of TransactionRecord
        """
        params: dict = {"perPage": per_page, "page": page}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if status:
            params["status"] = status

        try:
            data = self._request(
                "GET",
                "/transaction",
                operation="list_transactions",
                params=params,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack list transactions failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to list transactions: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "List transactions failed"))

        records = []
        for t in data.get("data", []):
            records.append(
                TransactionRecord(
                    id=t["id"],
                    reference=t["reference"],
                    amount=t["amount"],
                    currency=t["currency"],
                    status=t["status"],
                    paid_at=t.get("paid_at"),
                    created_at=t["createdAt"],
                    channel=t.get("channel", ""),
                    customer_email=t.get("customer", {}).get("email", ""),
                    fees=t.get("fees", 0),
                    metadata=t.get("metadata") or {},
                )
            )
        return records

    def list_transfers(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        per_page: int = 100,
        page: int = 1,
    ) -> list[TransferRecord]:
        """
        List transfers (outbound payments).

        Args:
            from_date: Start date (ISO format: 2026-01-01T00:00:00.000Z)
            to_date: End date (ISO format)
            status: Filter by status (pending, success, failed)
            per_page: Number of records per page (max 100)
            page: Page number

        Returns:
            List of TransferRecord
        """
        params: dict = {"perPage": per_page, "page": page}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if status:
            params["status"] = status

        try:
            data = self._request(
                "GET",
                "/transfer",
                operation="list_transfers",
                params=params,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack list transfers failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to list transfers: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "List transfers failed"))

        records = []
        for t in data.get("data", []):
            recipient = t.get("recipient", {})
            details = recipient.get("details", {})
            records.append(
                TransferRecord(
                    id=t["id"],
                    transfer_code=t["transfer_code"],
                    reference=t["reference"],
                    amount=t["amount"],
                    currency=t["currency"],
                    status=t["status"],
                    reason=t.get("reason"),
                    created_at=t["createdAt"],
                    updated_at=t.get("updatedAt"),
                    recipient_name=recipient.get("name", ""),
                    recipient_account_number=details.get("account_number", ""),
                    recipient_bank_code=details.get("bank_code", ""),
                    fees=t.get("fee") or t.get("fees"),
                )
            )
        return records

    def get_balance(self) -> list[dict[str, Any]]:
        """
        Get Paystack account balance.

        Returns:
            Dict with balance info per currency
        """
        try:
            data = self._request(
                "GET",
                "/balance",
                operation="get_balance",
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack get balance failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to get balance: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Get balance failed"))

        return cast(list[dict[str, Any]], data.get("data", []))

    def list_settlements(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        per_page: int = 100,
        page: int = 1,
    ) -> list[SettlementRecord]:
        """
        List settlements (payouts to merchant bank account).

        Settlements are automatic payouts from Paystack to the merchant's
        registered bank account (e.g., UBA).

        Args:
            from_date: Start date (ISO format: 2026-01-01T00:00:00.000Z)
            to_date: End date (ISO format)
            status: Filter by status (pending, success)
            per_page: Number of records per page (max 100)
            page: Page number

        Returns:
            List of SettlementRecord
        """
        params: dict = {"perPage": per_page, "page": page}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if status:
            params["status"] = status

        try:
            data = self._request(
                "GET",
                "/settlement",
                operation="list_settlements",
                params=params,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack list settlements failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to list settlements: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "List settlements failed"))

        records = []
        for s in data.get("data", []):
            records.append(
                SettlementRecord(
                    id=s["id"],
                    settlement_date=s.get("settlement_date") or s.get("settled_at", ""),
                    status=s["status"],
                    total_amount=s.get("total_amount", 0),
                    total_fees=s.get("total_fees", 0),
                    net_amount=s.get("total_amount", 0) - s.get("total_fees", 0),
                    currency=s.get(
                        "currency", settings.default_functional_currency_code
                    ),
                    created_at=s.get("createdAt", ""),
                )
            )
        return records

    def get_settlement_transactions(
        self,
        settlement_id: int,
        per_page: int = 100,
        page: int = 1,
    ) -> list[SettlementTransaction]:
        """
        Get transactions that make up a specific settlement.

        Args:
            settlement_id: The settlement ID
            per_page: Number of records per page (max 100)
            page: Page number

        Returns:
            List of SettlementTransaction
        """
        params: dict = {"perPage": per_page, "page": page}

        try:
            data = self._request(
                "GET",
                f"/settlement/{settlement_id}/transactions",
                operation="get_settlement_transactions",
                params=params,
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Paystack get settlement transactions failed: {e.response.text}"
            )
            raise _from_http_status(
                f"Failed to get settlement transactions: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(
                data.get("message", "Get settlement transactions failed")
            )

        records = []
        for t in data.get("data", []):
            amount = t.get("amount", 0)
            fees = t.get("fees", 0)
            records.append(
                SettlementTransaction(
                    id=t["id"],
                    reference=t.get("reference", ""),
                    amount=amount,
                    fees=fees,
                    net_amount=amount - fees,
                    currency=t.get(
                        "currency", settings.default_functional_currency_code
                    ),
                    status=t.get("status", ""),
                    paid_at=t.get("paid_at"),
                    created_at=t.get("createdAt", ""),
                    customer_email=t.get("customer", {}).get("email", ""),
                    channel=t.get("channel", ""),
                )
            )
        return records

    # =========================================================================
    # Customer API (for better payment tracking)
    # =========================================================================

    def create_customer(
        self,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        metadata: dict | None = None,
    ) -> PaystackCustomer:
        """
        Create a customer in Paystack.

        Args:
            email: Customer email (required, used as identifier)
            first_name: Customer first name
            last_name: Customer last name
            phone: Customer phone number
            metadata: Custom metadata (e.g., customer_id, customer_code)

        Returns:
            PaystackCustomer with customer_code
        """
        payload: dict = {"email": email}
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if phone:
            payload["phone"] = phone
        if metadata:
            payload["metadata"] = metadata

        try:
            data = self._request(
                "POST",
                "/customer",
                operation="create_customer",
                json=payload,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack create customer failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to create customer: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Create customer failed"))

        d = data["data"]
        return PaystackCustomer(
            id=d["id"],
            customer_code=d["customer_code"],
            email=d["email"],
            first_name=d.get("first_name"),
            last_name=d.get("last_name"),
            phone=d.get("phone"),
            metadata=d.get("metadata") or {},
            created_at=d.get("createdAt", ""),
        )

    def update_customer(
        self,
        customer_code: str,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        metadata: dict | None = None,
    ) -> PaystackCustomer:
        """
        Update an existing customer in Paystack.

        Args:
            customer_code: Paystack customer code
            first_name: Customer first name
            last_name: Customer last name
            phone: Customer phone number
            metadata: Custom metadata to update

        Returns:
            Updated PaystackCustomer
        """
        payload: dict = {}
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if phone:
            payload["phone"] = phone
        if metadata:
            payload["metadata"] = metadata

        try:
            data = self._request(
                "PUT",
                f"/customer/{customer_code}",
                operation="update_customer",
                json=payload,
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack update customer failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to update customer: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            raise PaystackError(data.get("message", "Update customer failed"))

        d = data["data"]
        return PaystackCustomer(
            id=d["id"],
            customer_code=d["customer_code"],
            email=d["email"],
            first_name=d.get("first_name"),
            last_name=d.get("last_name"),
            phone=d.get("phone"),
            metadata=d.get("metadata") or {},
            created_at=d.get("createdAt", ""),
        )

    def get_customer(self, email_or_code: str) -> PaystackCustomer | None:
        """
        Get a customer by email or customer_code.

        Args:
            email_or_code: Customer email or Paystack customer_code

        Returns:
            PaystackCustomer if found, None if not found
        """
        try:
            data = self._request(
                "GET",
                f"/customer/{email_or_code}",
                operation="get_customer",
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            logger.error(f"Paystack get customer failed: {e.response.text}")
            raise _from_http_status(
                f"Failed to get customer: {e.response.text}",
                e,
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackUnreachable(f"Request failed: {str(e)}")
        if not data.get("status"):
            return None

        d = data["data"]
        return PaystackCustomer(
            id=d["id"],
            customer_code=d["customer_code"],
            email=d["email"],
            first_name=d.get("first_name"),
            last_name=d.get("last_name"),
            phone=d.get("phone"),
            metadata=d.get("metadata") or {},
            created_at=d.get("createdAt", ""),
        )

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
