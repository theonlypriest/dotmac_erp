"""
dotmac_sub API Client.

HTTP client for the dotmac_sub subscriber-management system at
``selfcare.dotmac.io``. Replaces the legacy Splynx ISP-billing feed.

- **Auth**: scoped dotmac_sub API key sent as ``X-Api-Key``. dotmac_sub's
  ``Authorization: Bearer`` path only accepts session-bound login JWTs, so a
  long-lived service key MUST go in the ``X-Api-Key`` header.
- **API**: REST under ``/api/v1`` with ``?limit=&offset=`` pagination wrapped as
  ``{"items": [...], "count", "limit", "offset"}``.
- **Domain**: ``Reseller -> Subscriber -> BillingAccount`` with
  invoices/payments/credit-notes keyed on the *billing account* (``account_id``).
- **Allocations are inline** on invoices and payments — no ledger re-fetch.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx
from dotmac_integration_client import (
    IntegrationHttpClient,
    ReachabilityCircuit,
    exponential_backoff,
)
from dotmac_kernel.money import Money

from app.config import settings
from app.services.finance.money_boundary import (
    SUPPORTED_CURRENCIES,
    MoneyBoundaryError,
    check_canonical_money_lexeme,
    check_canonical_money_string,
    to_boundary_money,
)
from app.metrics import observe_integration_request, observe_paystack_selfcare_relay
from app.observability import get_request_id

logger = logging.getLogger(__name__)

_INTEGRATION_CLIENT_HEADER = "X-Dotmac-Integration-Client"
_INTEGRATION_CLIENT_NAME = "dotmac-erp"

# Reachability-circuit cooldown (seconds); <= 0 disables the breaker.
_CIRCUIT_COOLDOWN_ENV = "DOTMAC_SUB_CIRCUIT_SECONDS"
_CIRCUIT_COOLDOWN_DEFAULT = 30.0
_MAX_INFLIGHT_ENV = "DOTMAC_SUB_MAX_INFLIGHT_REQUESTS"
_MAX_INFLIGHT_DEFAULT = 4


def _max_inflight_requests() -> int:
    raw = os.getenv(_MAX_INFLIGHT_ENV, "")
    try:
        value = int(raw) if raw else _MAX_INFLIGHT_DEFAULT
    except ValueError:
        value = _MAX_INFLIGHT_DEFAULT
        logger.warning(
            "Invalid %s=%r; using default %d",
            _MAX_INFLIGHT_ENV,
            raw,
            _MAX_INFLIGHT_DEFAULT,
        )
    return max(1, min(value, 32))


# One admission pool per ERP worker keeps concurrent bulk-sync calls bounded.
# The signed Paystack relay intentionally does not use this pool: payment
# notification delivery must not wait behind a large reconciliation run.
_REQUEST_SLOTS = threading.BoundedSemaphore(_max_inflight_requests())


def _circuit_cooldown_seconds() -> float:
    raw = os.getenv(_CIRCUIT_COOLDOWN_ENV, "")
    if not raw:
        return _CIRCUIT_COOLDOWN_DEFAULT
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %.0fs",
            _CIRCUIT_COOLDOWN_ENV,
            raw,
            _CIRCUIT_COOLDOWN_DEFAULT,
        )
        return _CIRCUIT_COOLDOWN_DEFAULT


def _request_id_provider() -> str | None:
    """Propagate ERP's x-request-id contextvar onto outbound sub calls."""
    request_id = get_request_id()
    return request_id or None


def _response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("message") or data.get("error")
        if isinstance(detail, str):
            return detail.strip()
        if detail is not None:
            return str(detail)
    return ""


class DotmacSubError(Exception):
    """dotmac_sub API error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class DotmacSubAuthenticationError(DotmacSubError):
    """Authentication failed (bad or missing bearer token)."""


class DotmacSubNotFoundError(DotmacSubError):
    """Resource not found."""


class DotmacSubRateLimitError(DotmacSubError):
    """Rate limit exceeded.

    ``retry_after`` (seconds, already capped at the client's
    ``_RETRY_AFTER_CAP``) carries a parsed ``Retry-After`` header; the retry
    engine honours it over exponential backoff when set.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


class DotmacSubPermanentSyncError(DotmacSubError):
    """A dotmac_sub rejection that needs data/configuration to be fixed."""


class _TransientServerError(DotmacSubError):
    """Internal: a 5xx response, distinguishable so the engine retries it.

    Never escapes ``DotmacSubClient._request`` — exhausted retries are
    re-wrapped as a plain :class:`DotmacSubError`, exactly like the old loop.
    """


class DotmacSubParseError(MoneyBoundaryError):
    """Fail-closed rejection of a Sub row's asserted money facts at parse time.

    Raised by the strict money-fact parsers when a billing document, payment
    or credit note asserts a malformed/missing monetary amount or omits its
    currency. Deliberately NOT a :class:`DotmacSubError` — the sync loops
    treat those as API/run failures, while this error must fail only the ONE
    source row (mirroring the per-row savepoint semantics).

    Carries the source row identity (``record``) and its server ``updated_at``
    so the sync mixins can hold the incremental watermark at the failed row —
    the row is re-pulled (and re-rejected, until Sub corrects it) instead of
    being silently skipped forever. ``updated_at`` is the PARSED instant, not
    the raw wire text: ``None`` means the row has no usable position and the
    cursor must FREEZE for the run (see ``sync/_progress.WatermarkProgress``).
    """

    def __init__(
        self,
        message: str,
        *,
        record: str,
        updated_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.record = record
        self.updated_at = updated_at


@dataclass
class DotmacSubConfig:
    """Configuration for the dotmac_sub API.

    Auth is a **scoped dotmac_sub API key** (``api_token``), sent as
    ``X-Api-Key``. Staff-credential login has been retired for security
    (audit S1) — the client no longer logs in with a username + password;
    ``api_token`` must be a dotmac_sub API key with read scopes for the
    synced domains.
    """

    api_url: str
    api_token: str = ""
    timeout: float = 60.0
    max_retries: int = 3

    @classmethod
    def from_settings(cls) -> DotmacSubConfig:
        """Create config from application settings (env fallback / bootstrap)."""
        from app.services.secrets import resolve_secret

        return cls(
            api_url=settings.dotmac_sub_api_url,
            api_token=resolve_secret(settings.dotmac_sub_api_token) or "",
            timeout=settings.dotmac_sub_request_timeout,
            max_retries=settings.dotmac_sub_max_retries,
        )

    @classmethod
    def for_org(cls, db: Any, organization_id: Any) -> DotmacSubConfig:
        """Resolve config for an org, preferring UI-managed IntegrationConfig.

        Looks up the org's ``DOTMAC_SUB`` row in ``integration_config`` (with
        decrypted, possibly OpenBao-backed credentials) and falls back to the
        env-based settings for any field not configured there.
        """
        from app.models.sync import IntegrationType
        from app.services.integration_config import IntegrationConfigService

        env = cls.from_settings()
        try:
            creds = IntegrationConfigService(db).get_decrypted_credentials(
                organization_id, IntegrationType.DOTMAC_SUB
            )
        except Exception:  # noqa: BLE001 — never let config lookup break sync
            logger.warning(
                "Could not load dotmac_sub IntegrationConfig for org %s; "
                "falling back to env settings",
                organization_id,
                exc_info=True,
            )
            creds = None

        if not creds:
            return env

        # IntegrationConfig column mapping for dotmac_sub:
        #   base_url   -> api_url
        #   api_key    -> dotmac_sub API key (encrypted)
        #   api_secret -> webhook secret (used by the inbound webhook route)
        return cls(
            api_url=creds.get("base_url") or env.api_url,
            api_token=creds.get("api_key") or env.api_token,
            timeout=env.timeout,
            max_retries=env.max_retries,
        )

    def is_configured(self) -> bool:
        """Configured when we have a base URL and an API key."""
        return bool(self.api_url and self.api_token)

    @property
    def auth_headers(self) -> dict[str, str]:
        """Auth headers for a dotmac_sub request (scoped API key)."""
        return {"X-Api-Key": self.api_token}


class TaxApplication(str, Enum):
    """How a Sub billing line's tax relates to its amount — a CLOSED set.

    ERP already fails closed on anything outside these three
    (``_source_line_amounts`` / ``_resolve_source_sales_tax_code`` raise), so
    admitting the value as a typed member at parse time is a tightening, not a
    new policy: it also closes the hole where a bogus application rode through
    silently when the line happened to carry no ``tax_rate_id``.
    """

    EXCLUSIVE = "exclusive"
    INCLUSIVE = "inclusive"
    EXEMPT = "exempt"


@dataclass(frozen=True)
class ResellerRecord:
    """Reseller (parent account) from dotmac_sub — ADMITTED evidence.

    Timestamps are typed at parse time; every collection field is a tuple, so
    the record is immutable in depth, not just at its top level.
    """

    id: str
    name: str
    code: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    is_active: bool = True
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SubscriberRecord:
    """Subscriber (end customer) from dotmac_sub — ADMITTED evidence.

    ``status`` and ``category`` stay ``str``: Sub's subscriber lifecycle and
    customer-category vocabularies are product-configurable and grow between
    Sub releases, and ERP consumes them as DATA (``category`` is membership-
    tested against ``_COMPANY_CATEGORIES``, ``status`` only feeds the change
    hash). An unknown member must not fail a customer row, so these are
    deliberately not enums.
    """

    id: str
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    company_name: str | None = None
    legal_name: str | None = None
    email: str | None = None
    phone: str | None = None
    status: str | None = None
    category: str | None = None
    is_active: bool = True
    reseller_id: str | None = None
    tax_id: str | None = None
    subscriber_number: str | None = None
    account_number: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    service_status: str | None = None
    recurring_subscription_count: int = 0
    next_renewal_at: datetime | None = None
    billing_cycle: str | None = None
    recurring_amount_monthly: Decimal | None = None
    annualized_recurring_revenue: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def full_name(self) -> str:
        if self.display_name:
            return self.display_name
        if self.company_name:
            return self.company_name
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.legal_name or self.account_number or self.id


@dataclass(frozen=True)
class BillingAccountRecord:
    """Billing account from dotmac_sub (reseller-scoped; invoices key on this).

    ``status`` stays ``str``: Sub's billing-account state vocabulary is
    product-configurable, ERP never branches on it (only ``is_active`` is
    consumed), and an unknown member must not fail account resolution.
    """

    id: str
    reseller_id: str
    name: str
    currency: str
    status: str
    balance: Decimal = Decimal("0")
    is_active: bool = True
    subscriber_id: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class DocumentBoundaryMoney:
    """Typed kernel ``Money`` for a Sub billing document's header money facts.

    Built fail-closed by ``InvoiceRecord.boundary_money`` /
    ``CreditNoteRecord.boundary_money`` via the E4 Money/FX boundary adapter
    (`app.services.finance.money_boundary`). ERP internals keep consuming the
    record's exact ``Decimal`` fields; these values exist so the boundary is
    validated and typed, not to replace ERP's decimal contracts.
    """

    subtotal: Money
    tax_total: Money
    total: Money
    balance_due: Money | None = None
    applied_total: Money | None = None


@dataclass(frozen=True)
class PaymentBoundaryMoney:
    """Typed kernel ``Money`` for a Sub payment's settlement money facts."""

    amount: Money
    refunded_amount: Money
    wht_amount: Money
    gross_amount: Money | None = None
    net_amount: Money | None = None


@dataclass(frozen=True)
class InvoiceLineRecord:
    """Invoice line item.

    ``amount`` is Sub's asserted line money FACT: ``None`` when Sub omits it
    (ERP then derives quantity x unit_price itself and may round that derived
    value); when present it is validated exact — never rounded, never
    defaulted to zero.

    ``tax_application`` is a typed :class:`TaxApplication` member — a CLOSED
    set ERP already fails closed on; an unknown value is rejected at parse.
    """

    id: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None
    tax_rate_id: str | None = None
    tax_application: TaxApplication = TaxApplication.EXCLUSIVE


@dataclass(frozen=True)
class AllocationRecord:
    """Payment-to-invoice allocation (inline on invoices and payments)."""

    id: str
    payment_id: str
    invoice_id: str
    amount: Decimal


@dataclass(frozen=True)
class InvoiceRecord:
    """Invoice from dotmac_sub — ADMITTED evidence, immutable in depth.

    ``status`` stays ``str``. Sub's invoice-status vocabulary is
    product-configurable and grows across Sub releases, and ERP's
    ``_map_invoice_status`` deliberately owns a documented CATCH-ALL (anything
    it does not recognize maps to ``POSTED``). Rejecting an unknown member at
    parse time would fail otherwise-valid AR rows on a Sub-side vocabulary
    addition, so the value is treated as DATA, not as an error.
    """

    id: str
    account_id: str
    invoice_number: str | None
    status: str
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    balance_due: Decimal
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool = False
    # Server-tracked last-modified instant, parsed to a tz-aware UTC datetime.
    # Drives the incremental sync watermark so we only pull the delta each
    # cycle; consumers needing the wire text format it back explicitly.
    updated_at: datetime | None = None
    # Subscriber identity is embedded by the sync feed so bulk invoice pulls
    # do not make a rate-limited detail request for every account.
    account: SubscriberRecord | None = None
    lines: tuple[InvoiceLineRecord, ...] = ()
    allocations: tuple[AllocationRecord, ...] = ()

    def boundary_money(self) -> DocumentBoundaryMoney:
        """Typed, fail-closed boundary money for this invoice's header facts.

        Rejects float/missing-currency/excess-minor-unit-precision via the E4
        adapter; allocation amounts are validated in the same pass. Line
        ``quantity``/``unit_price`` stay ERP decimals (rates, not money), and
        line amounts remain inputs to the centralized derived-value rounding
        in the sync mixins.
        """
        label = f"Sub invoice {self.id}"
        for alloc in self.allocations:
            to_boundary_money(
                alloc.amount,
                self.currency,
                field=f"{label} allocation {alloc.id} amount",
            )
        return DocumentBoundaryMoney(
            subtotal=to_boundary_money(
                self.subtotal, self.currency, field=f"{label} subtotal"
            ),
            tax_total=to_boundary_money(
                self.tax_total, self.currency, field=f"{label} tax_total"
            ),
            total=to_boundary_money(self.total, self.currency, field=f"{label} total"),
            balance_due=to_boundary_money(
                self.balance_due, self.currency, field=f"{label} balance_due"
            ),
        )


@dataclass(frozen=True)
class PaymentRecord:
    """Payment from dotmac_sub — ADMITTED evidence, immutable in depth.

    ``status`` and ``wht_status`` stay ``str``. Both vocabularies are owned by
    Sub and extended there (new PSP settlement states, new WHT lifecycle
    steps); ERP membership-tests the handful it acts on
    (``_SETTLED_STATUSES``, the terminal ``{reclaimed, written_off}`` pair) and
    treats everything else as "not that case". An unknown member is DATA — it
    must not fail a cash row — so neither is an enum.
    """

    id: str
    account_id: str | None
    billing_account_id: str | None
    amount: Decimal
    currency: str
    status: str
    # Total refunded so far (gross `amount` is unchanged). Net cash = amount -
    # refunded_amount. Defaults to 0 when dotmac_sub hasn't deployed the field
    # yet, so the two apps deploy in any order.
    refunded_amount: Decimal = Decimal("0")
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    wht_amount: Decimal = Decimal("0")
    wht_rate: Decimal | None = None
    wht_status: str | None = None
    wht_record_id: str | None = None
    wht_certificate_reference: str | None = None
    wht_resolved_at: datetime | None = None
    paid_at: datetime | None = None
    external_id: str | None = None
    memo: str | None = None
    payment_method_id: str | None = None
    payment_channel_id: str | None = None
    # Server-tracked last-modified instant; see InvoiceRecord.
    updated_at: datetime | None = None
    allocations: tuple[AllocationRecord, ...] = ()

    @property
    def effective_account_id(self) -> str | None:
        return self.billing_account_id or self.account_id

    def boundary_money(self) -> PaymentBoundaryMoney:
        """Typed, fail-closed boundary money for this payment's settlement
        facts (gross/net/WHT evidence, refunds, allocations).

        ``wht_rate`` is a percentage rate, not money — it stays a plain ERP
        ``Decimal``.
        """
        label = f"Sub payment {self.id}"
        for alloc in self.allocations:
            to_boundary_money(
                alloc.amount,
                self.currency,
                field=f"{label} allocation {alloc.id} amount",
            )
        return PaymentBoundaryMoney(
            amount=to_boundary_money(
                self.amount, self.currency, field=f"{label} amount"
            ),
            refunded_amount=to_boundary_money(
                self.refunded_amount, self.currency, field=f"{label} refunded_amount"
            ),
            wht_amount=to_boundary_money(
                self.wht_amount, self.currency, field=f"{label} wht_amount"
            ),
            gross_amount=(
                to_boundary_money(
                    self.gross_amount, self.currency, field=f"{label} gross_amount"
                )
                if self.gross_amount is not None
                else None
            ),
            net_amount=(
                to_boundary_money(
                    self.net_amount, self.currency, field=f"{label} net_amount"
                )
                if self.net_amount is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CreditNoteLineRecord:
    """Credit note line item (``amount`` and ``tax_application`` semantics as
    :class:`InvoiceLineRecord`)."""

    id: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None
    tax_rate_id: str | None = None
    tax_application: TaxApplication = TaxApplication.EXCLUSIVE


@dataclass(frozen=True)
class CreditNoteRecord:
    """Credit note from dotmac_sub — ADMITTED evidence, immutable in depth.

    ``status`` stays ``str`` for the same reason as
    :class:`InvoiceRecord`: Sub owns and extends the vocabulary, and the ERP
    mapping has a documented catch-all (``POSTED``) for members it does not
    recognize.
    """

    id: str
    account_id: str
    invoice_id: str | None
    credit_number: str | None
    status: str
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    applied_total: Decimal = Decimal("0")
    memo: str | None = None
    issued_at: datetime | None = None
    # Server-tracked last-modified instant; see InvoiceRecord.
    updated_at: datetime | None = None
    lines: tuple[CreditNoteLineRecord, ...] = ()

    def boundary_money(self) -> DocumentBoundaryMoney:
        """Typed, fail-closed boundary money for this credit note's header
        facts (see ``InvoiceRecord.boundary_money``)."""
        label = f"Sub credit note {self.id}"
        return DocumentBoundaryMoney(
            subtotal=to_boundary_money(
                self.subtotal, self.currency, field=f"{label} subtotal"
            ),
            tax_total=to_boundary_money(
                self.tax_total, self.currency, field=f"{label} tax_total"
            ),
            total=to_boundary_money(self.total, self.currency, field=f"{label} total"),
            applied_total=to_boundary_money(
                self.applied_total, self.currency, field=f"{label} applied_total"
            ),
        )


@dataclass(frozen=True)
class TaxRateRecord:
    """Tax rate from dotmac_sub."""

    id: str
    name: str
    rate: Decimal
    code: str | None = None
    is_active: bool = True


def _dec(value: Any, default: str = "0") -> Decimal:
    """Lenient decimal coercion for NON-money numerics (quantities, rates,
    informational balances). Money FACTS must never come through here — they
    parse via the strict ``_required_money``/``_optional_money``/
    ``_defaulted_money`` helpers below, which fail closed instead of
    defaulting to zero."""
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        logger.warning("Could not parse decimal: %r", value)
        return Decimal(default)


def _registry_minor_units(currency_code: str) -> int | None:
    """Minor units for a currency when it is provisioned, else ``None``.

    Parsing applies the currency-aware canonical grammar whenever the
    currency is provisioned; an unprovisioned currency still gets the
    currency-independent lexical grammar here and is then rejected outright
    by the boundary registry at admission (``boundary_money()``).
    """
    return SUPPORTED_CURRENCIES.by_code.get(currency_code.upper())


def _parse_money_value(
    value: Any,
    *,
    record: str,
    field: str,
    updated_at: datetime | None,
    minor_units: int | None,
) -> Decimal:
    """Strictly parse a PRESENT money fact asserted by Sub.

    Wire contract: external money is a canonical decimal STRING only, in the
    ONE fixed-minor-unit form ``serialize_amount`` emits (e.g. ``"48375.00"``
    for NGN). Every JSON number token — int and float alike — plus booleans,
    non-finite values (NaN/Infinity) and every non-canonical spelling
    (``"1e3"``, ``" 100.00 "``, ``"+100.00"``, ``"01.00"``, ``".50"``,
    ``"100."``) is refused; nothing is laundered into a clean-looking zero.
    ``Decimal`` instances are tolerated for internal (non-wire) callers only.
    """
    if isinstance(value, (bool, int, float)):
        raise DotmacSubParseError(
            f"{record}: refusing {type(value).__name__} {value!r} for money "
            f"fact {field!r}; Sub must send money as a canonical decimal "
            'string (e.g. "48375.00")',
            record=record,
            updated_at=updated_at,
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DotmacSubParseError(
                f"{record}: non-finite money fact {field}={value!r} is not money",
                record=record,
                updated_at=updated_at,
            )
        return value
    if not isinstance(value, str):
        raise DotmacSubParseError(
            f"{record}: unsupported type {type(value).__name__} for money "
            f"fact {field!r}",
            record=record,
            updated_at=updated_at,
        )
    try:
        if minor_units is None:
            check_canonical_money_lexeme(value, field=field)
        else:
            check_canonical_money_string(value, minor_units=minor_units, field=field)
    except MoneyBoundaryError as exc:
        raise DotmacSubParseError(
            f"{record}: malformed money fact {field}={value!r}: {exc}",
            record=record,
            updated_at=updated_at,
        ) from exc
    return Decimal(value)


def _required_money(
    item: dict[str, Any],
    key: str,
    *,
    record: str,
    updated_at: datetime | None,
    minor_units: int | None,
) -> Decimal:
    """A money fact the Sub contract always asserts. Missing/blank is a hard
    row failure — zero is a real accounting assertion Sub must make
    explicitly, never a default ERP invents."""
    value = item.get(key)
    if value is None or value == "":
        raise DotmacSubParseError(
            f"{record}: required money fact {key!r} is missing",
            record=record,
            updated_at=updated_at,
        )
    return _parse_money_value(
        value, record=record, field=key, updated_at=updated_at, minor_units=minor_units
    )


def _optional_money(
    item: dict[str, Any],
    key: str,
    *,
    record: str,
    updated_at: datetime | None,
    minor_units: int | None,
) -> Decimal | None:
    """A money fact the Sub contract marks nullable (e.g. ``gross_amount`` /
    ``net_amount`` before a WHT settlement exists). Absent stays ``None``;
    present must parse strictly."""
    value = item.get(key)
    if value is None or value == "":
        return None
    return _parse_money_value(
        value, record=record, field=key, updated_at=updated_at, minor_units=minor_units
    )


def _defaulted_money(
    item: dict[str, Any],
    key: str,
    default: str,
    *,
    record: str,
    updated_at: datetime | None,
    minor_units: int | None,
) -> Decimal:
    """A money fact with a DOCUMENTED absent-field default (deploy-order
    compatibility: ``refunded_amount``/``wht_amount`` default to 0 until Sub
    deploys the field, ``applied_total`` to 0 when nothing is applied).
    The default applies ONLY when the field is absent/blank — a present value
    must parse strictly."""
    value = item.get(key)
    if value is None or value == "":
        return Decimal(default)
    # Older Sub deployments serialize zero-default facts as ``"0"`` even
    # when the currency contract requires fixed minor units. Normalize only
    # that exact compatibility token here; required, optional and nonzero
    # money facts still pass through the strict parser unchanged.
    if value == "0" and minor_units is not None and minor_units > 0:
        value = f"0.{('0' * minor_units)}"
    return _parse_money_value(
        value, record=record, field=key, updated_at=updated_at, minor_units=minor_units
    )


def _parse_wire_instant(
    value: Any,
    *,
    record: str,
    field: str,
    updated_at: datetime | None,
) -> datetime | None:
    """Admit a wire timestamp as a tz-aware UTC ``datetime``.

    Absent/blank stays ``None`` (the field is genuinely optional in Sub's
    contract and downstream consumers own their documented fallback). Anything
    present must be a parseable ISO8601 STRING: a non-string (e.g. an integer
    epoch) or an unparseable spelling is a typed :class:`DotmacSubParseError`,
    so it routes through the SAME row-failure collector as a malformed money
    fact instead of silently degrading to ``None`` inside a consumer.

    Naive instants are read as UTC — Sub emits UTC and ERP's watermark
    comparisons must never mix aware and naive values.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DotmacSubParseError(
            f"{record}: {field} must be an ISO8601 string, got "
            f"{type(value).__name__} {value!r}",
            record=record,
            updated_at=updated_at,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DotmacSubParseError(
            f"{record}: {field}={value!r} is not a parseable ISO8601 instant",
            record=record,
            updated_at=updated_at,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _wire_updated_at(item: dict[str, Any], *, record: str) -> datetime | None:
    """The server-tracked ``updated_at`` watermark instant for a row.

    Parsed FIRST so every other rejection on the row can be POSITIONED at this
    instant. A missing, non-string or unparseable ``updated_at`` is itself an
    UNPOSITIONED parse failure (``updated_at=None``): it routes through the
    row-failure collector and FREEZES the cursor per the watermark contract,
    rather than exploding mid-run or letting a later good row advance the
    cursor past this one.
    """
    return _parse_wire_instant(
        item.get("updated_at"),
        record=record,
        field="updated_at",
        updated_at=None,  # unusable position -> cursor freeze semantics
    )


def _wire_tax_application(
    line: dict[str, Any], *, record: str, updated_at: datetime | None
) -> TaxApplication:
    """Admit a line's tax application as a typed :class:`TaxApplication`.

    A CLOSED set: an unknown member is a row failure at parse time rather than
    a value ERP silently treats as "exclusive" (or, when the line carried no
    ``tax_rate_id``, as untaxed).
    """
    value = line.get("tax_application")
    if value is None or value == "":
        return TaxApplication.EXCLUSIVE
    if isinstance(value, TaxApplication):
        return value
    if not isinstance(value, str):
        raise DotmacSubParseError(
            f"{record}: tax_application must be a string, got "
            f"{type(value).__name__} {value!r}",
            record=record,
            updated_at=updated_at,
        )
    try:
        return TaxApplication(value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in TaxApplication))
        raise DotmacSubParseError(
            f"{record}: unsupported tax_application {value!r}; ERP admits "
            f"exactly {{{allowed}}}",
            record=record,
            updated_at=updated_at,
        ) from exc


def _required_currency(
    item: dict[str, Any], *, record: str, updated_at: datetime | None
) -> str:
    """The mandatory currency tag for a document's money facts.

    Sub's billing documents and payments always carry their currency (the
    pull contract in ``docs/dotmac_sub_tax_accounting_contract.md`` lists it
    among the payment settlement facts, and every money fact is asserted in
    the document currency). A missing/blank currency is a hard row failure —
    ERP's functional currency is NEVER substituted for a fact asserted by the
    external system.
    """
    value = item.get("currency")
    if not isinstance(value, str) or not value.strip():
        raise DotmacSubParseError(
            f"{record}: currency is required for its money facts; refusing "
            "to default an externally asserted amount to ERP's functional "
            "currency",
            record=record,
            updated_at=updated_at,
        )
    return value.strip()


def _watermark_params(
    account_id: str | None,
    status: str | None,
    updated_since: str | None,
) -> dict[str, Any]:
    """Build filters for a deterministic sync feed.

    Sync endpoints always order by ``updated_at, id``; clients cannot override
    that ordering because doing so would make paging nondeterministic.
    """
    params: dict[str, Any] = {}
    if account_id:
        params["account_id"] = account_id
    if status:
        params["status"] = status
    if updated_since:
        params["updated_since"] = updated_since
    return params


def _allocations(
    items: list[dict[str, Any]] | None,
    *,
    record: str,
    updated_at: datetime | None,
    minor_units: int | None,
) -> tuple[AllocationRecord, ...]:
    """Admitted allocations as an immutable TUPLE.

    A frozen dataclass is only shallowly immutable: a ``list`` field on an
    admitted record can still be appended to, reordered or reassigned
    element-wise behind the boundary's back. Admitted evidence is a tuple so
    the record is immutable in depth — a changed payload is a NEW record
    (``dataclasses.replace``), never an in-place edit.
    """
    out: list[AllocationRecord] = []
    for a in items or []:
        alloc_id = str(a.get("id", ""))
        out.append(
            AllocationRecord(
                id=alloc_id,
                payment_id=str(a.get("payment_id", "")),
                invoice_id=str(a.get("invoice_id", "")),
                # An allocation's amount is a money fact — strict, no default.
                amount=_required_money(
                    a,
                    "amount",
                    record=f"{record} allocation {alloc_id}",
                    updated_at=updated_at,
                    minor_units=minor_units,
                ),
            )
        )
    return tuple(out)


class DotmacSubClient:
    """HTTP client for the dotmac_sub API (bearer auth, ListResponse paging)."""

    API_PREFIX = "/api/v1"

    # Resilience tuning.
    _RETRY_BACKOFF_BASE = 0.5  # seconds; exponential base for retry sleeps
    _RETRY_BACKOFF_CAP = 10.0  # seconds; max backoff between retries
    _RETRY_AFTER_CAP = 60.0  # seconds; max honoured Retry-After on a 429
    _MAX_PAGES = 100_000  # pagination safety bound (guards an API ignoring offset)
    _SYNC_PAGE_SIZE = 500
    _SYNC_PAGE_DELAY_SECONDS = 1.0

    def __init__(self, config: DotmacSubConfig | None = None) -> None:
        self.config = config or DotmacSubConfig.from_settings()
        self._client: httpx.Client | None = None
        # Shared retry/transport engine (dotmac-integration-client). Policy:
        #   - 429  -> DotmacSubRateLimitError with Retry-After honoured (capped)
        #   - 5xx  -> _TransientServerError, retried with jittered backoff
        #   - connect/timeout transport failures retried; trip the circuit
        #   - auth/404/other typed errors raise immediately
        self._engine = IntegrationHttpClient(
            client_factory=lambda: self.client,
            response_handler=self._handle_response,
            backoff=exponential_backoff(
                base=self._RETRY_BACKOFF_BASE, cap=self._RETRY_BACKOFF_CAP
            ),
            max_attempts=self.config.max_retries,
            rate_limit_exc=DotmacSubRateLimitError,
            retryable_excs=(_TransientServerError,),
            non_retryable_excs=(DotmacSubError,),
            loop_exhausted_factory=self._loop_exhausted_error,
            circuit=ReachabilityCircuit(cooldown_seconds=_circuit_cooldown_seconds()),
            auth_headers=lambda: {
                "X-Api-Key": self._api_key(),
                _INTEGRATION_CLIENT_HEADER: _INTEGRATION_CLIENT_NAME,
            },
            edge="dotmac_sub",
            request_id_provider=_request_id_provider,
        )

    def __enter__(self) -> DotmacSubClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @property
    def client(self) -> httpx.Client:
        if not self.config.is_configured():
            raise DotmacSubError(
                "dotmac_sub not configured. Set DOTMAC_SUB_API_URL and either "
                "DOTMAC_SUB_API_TOKEN or username+password."
            )
        if self._client is None:
            self._client = httpx.Client(
                base_url=f"{self.config.api_url.rstrip('/')}{self.API_PREFIX}",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self.config.timeout),
            )
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ---- Authentication ----

    def _api_key(self) -> str:
        """Return the configured dotmac_sub API key.

        Staff-credential login (username+password session scrape) was retired for
        security (audit S1). If no API key is configured we fail loudly rather
        than fall back to staff creds.
        """
        if not self.config.api_token:
            raise DotmacSubAuthenticationError(
                "dotmac_sub api_token is not configured. Staff-credential login "
                "has been retired — set DOTMAC_SUB_API_TOKEN (or the integration's "
                "api_key) to a scoped dotmac_sub API key."
            )
        return self.config.api_token

    def _parse_retry_after(self, header_value: str | None) -> float | None:
        """Parse a 429 ``Retry-After`` header, capped at ``_RETRY_AFTER_CAP``.

        Only the integer-seconds form is parsed (the common case); anything
        else returns ``None`` so the engine falls back to exponential backoff.
        """
        if header_value:
            try:
                return min(float(int(header_value)), self._RETRY_AFTER_CAP)
            except (ValueError, TypeError):
                pass
        return None

    def _handle_response(self, response: httpx.Response, *, endpoint: str) -> Any:
        """Map a dotmac_sub response to a parsed body or a typed error.

        Retry classification keys off the exception type: 429 raises the
        rate-limit error (with parsed ``retry_after``), 5xx raises the
        transient subclass; auth/404 raise immediately.
        """
        status = response.status_code
        if status == 403 and endpoint.endswith("/erp-department"):
            detail = _response_detail(response)
            message = (
                "Self-Care API key is missing the "
                "operations:service_team:membership scope required for ERP "
                "department membership sync."
            )
            if detail:
                message = f"{message} Self-Care detail: {detail}"
            raise DotmacSubPermanentSyncError(message, status_code=status)
        if status in (401, 403):
            raise DotmacSubAuthenticationError(
                "Authentication failed for dotmac_sub.", status_code=status
            )
        if status == 404:
            raise DotmacSubNotFoundError(
                f"Resource not found: {endpoint}", status_code=404
            )
        if status == 429:
            raise DotmacSubRateLimitError(
                "Rate limit exceeded.",
                status_code=429,
                retry_after=self._parse_retry_after(
                    response.headers.get("Retry-After")
                ),
            )
        if status in (409, 422):
            detail = _response_detail(response)
            if endpoint.endswith("/erp-department") and status == 422:
                message = (
                    "Department is not mapped in Self-Care. Map this ERP "
                    "department first."
                )
            elif endpoint.endswith("/erp-department") and status == 409:
                message = (
                    "ERP employee is already linked to a different Self-Care "
                    "user. Resolve the duplicate account link."
                )
            elif endpoint.endswith("/roles") and status == 422:
                message = (
                    "Self-Care rejected one or more ERP staff roles. Ensure "
                    "the employee dotmac_sub_roles exist and are active in "
                    "Self-Care."
                )
            else:
                message = "Self-Care rejected the request."
            if detail:
                message = f"{message} Self-Care detail: {detail}"
            raise DotmacSubPermanentSyncError(message, status_code=status)
        if status >= 500:
            raise _TransientServerError(f"Server error: {status}", status_code=status)
        response.raise_for_status()
        return response.json()

    def _loop_exhausted_error(self, exc: BaseException, retries: int) -> DotmacSubError:
        """Wrap engine give-up cases (429 exhaustion, circuit open)."""
        if isinstance(exc, DotmacSubRateLimitError):
            return DotmacSubRateLimitError(
                "Rate limit exceeded. Try again later.", status_code=429
            )
        return DotmacSubError(f"dotmac_sub temporarily unavailable: {exc}")

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Issue one logical request via the shared integration engine.

        The engine (``dotmac-integration-client``) owns the retry loop,
        jittered exponential backoff, 429 Retry-After honouring, the
        reachability circuit, X-Api-Key auth injection, and x-request-id
        propagation. This wrapper owns the metrics contract: exactly one
        ``observe_integration_request`` per logical call with the legacy
        status vocabulary, duration measured across all attempts.

        Behavioural deltas vs the old hand-rolled loop (accepted):
        - Backoff sleeps now carry up-to-0.25s of jitter.
        - A short reachability circuit (``DOTMAC_SUB_CIRCUIT_SECONDS``,
          default 30s) fails fast after a transport failure.
        - ``x-request-id`` is propagated from ERP's request context.
        - Transport retries cover connect + timeout failures; other
          ``httpx.RequestError`` subclasses (read/write/protocol errors) now
          fail fast instead of retrying, but still surface as the same
          ``DotmacSubError`` with a ``request_error`` metric.
        - Retried 429s emit one ``rate_limited`` metric per call rather than
          one per attempt, and 429 exhaustion sleeps once more before raising.
        """
        started_at = time.perf_counter()
        metric_status: str | None = None
        try:
            with _REQUEST_SLOTS:
                result = self._engine.request(
                    method,
                    endpoint,
                    params=params,
                    json_data=json,
                    handler_kwargs={"endpoint": endpoint},
                )
            metric_status = "success"
            return result
        except _TransientServerError as e:
            metric_status = "server_error"
            raise DotmacSubError(
                f"Request failed after {self.config.max_retries} attempts: {e}"
            ) from e
        except DotmacSubAuthenticationError as e:
            # A response-mapped 401/403 counts as auth_error; a locally raised
            # missing-api-key error (status None) never reached the wire and,
            # as before, emits no metric.
            if e.status_code is not None:
                metric_status = "auth_error"
            raise
        except DotmacSubNotFoundError:
            metric_status = "not_found"
            raise
        except DotmacSubRateLimitError:
            metric_status = "rate_limited"
            raise
        except DotmacSubPermanentSyncError:
            metric_status = "client_error"
            raise
        except httpx.TimeoutException as e:
            metric_status = "timeout"
            raise DotmacSubError(
                f"Request failed after {self.config.max_retries} attempts: {e}"
            ) from e
        except httpx.RequestError as e:
            metric_status = "request_error"
            raise DotmacSubError(
                f"Request failed after {self.config.max_retries} attempts: {e}"
            ) from e
        finally:
            if metric_status is not None:
                observe_integration_request(
                    "dotmac_sub",
                    f"{method.upper()} {endpoint}",
                    metric_status,
                    max(time.perf_counter() - started_at, 0.0),
                )

    def relay_paystack_webhook(
        self,
        *,
        raw_payload: bytes,
        signature: str,
    ) -> dict[str, Any]:
        """Relay exact signed bytes on a lane isolated from bulk-sync traffic."""

        if not raw_payload or not signature.strip():
            raise ValueError("Paystack payload and signature are required")
        started_at = time.perf_counter()
        outcome = "request_error"
        try:
            response = self.client.post(
                "/payment-events/paystack",
                content=raw_payload,
                headers={"X-Paystack-Signature": signature.strip()},
            )
            if response.status_code == 429:
                outcome = "rate_limited"
                raise DotmacSubRateLimitError(
                    "Selfcare rate-limited the Paystack relay",
                    status_code=429,
                    retry_after=self._parse_retry_after(
                        response.headers.get("Retry-After")
                    ),
                )
            if response.status_code >= 500:
                outcome = "server_error"
                raise DotmacSubError(
                    "Selfcare could not accept the Paystack relay",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                outcome = "client_error"
                raise DotmacSubError(
                    "Selfcare rejected the Paystack relay",
                    status_code=response.status_code,
                )
            result = response.json()
            if not isinstance(result, dict):
                outcome = "invalid_response"
                raise DotmacSubError(
                    "Selfcare returned an invalid Paystack relay result"
                )
            outcome = "success"
            return result
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            raise DotmacSubError("Selfcare Paystack relay timed out") from exc
        except httpx.RequestError as exc:
            outcome = "request_error"
            raise DotmacSubError("Selfcare Paystack relay was unreachable") from exc
        finally:
            observe_paystack_selfcare_relay(
                outcome, max(time.perf_counter() - started_at, 0.0)
            )

    def _paginate(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        page_delay: float = 0.0,
    ) -> Generator[dict[str, Any], None, None]:
        params = dict(params or {})
        offset = 0
        is_first_page = True
        page_count = 0
        while True:
            page_count += 1
            if page_count > self._MAX_PAGES:
                logger.error(
                    "dotmac_sub pagination exceeded %d pages for %s; aborting "
                    "(the API may be ignoring offset)",
                    self._MAX_PAGES,
                    endpoint,
                )
                break
            if not is_first_page and page_delay > 0:
                time.sleep(page_delay)
            is_first_page = False
            params["offset"] = offset
            params["limit"] = page_size
            data = self._request("GET", endpoint, params=params)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items", [])
            else:
                break
            if not items:
                break
            yield from items
            if len(items) < page_size:
                break
            offset += page_size

    def _sync_paginate(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Read one bounded integration feed without bursting the source API."""
        yield from self._paginate(
            endpoint,
            params=params,
            page_size=self._SYNC_PAGE_SIZE,
            page_delay=self._SYNC_PAGE_DELAY_SECONDS,
        )

    # ---- Staff accounts (ERP staff sync) ----

    def create_staff_account(
        self,
        *,
        email: str,
        first_name: str,
        last_name: str,
        role: str = "staff",
        roles: list[str] | None = None,
        send_invite: bool = True,
    ) -> dict[str, Any]:
        """Create (idempotently) + invite a dotmac_sub staff account.

        Requires the API key to carry ``rbac:assign``. Returns the endpoint's
        ``{id, email, is_active, created, invited}`` payload.
        """
        result = self._request(
            "POST",
            "/staff-accounts",
            json={
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "role": role,
                "roles": roles,
                "send_invite": send_invite,
            },
        )
        return dict(result) if isinstance(result, dict) else {}

    def get_staff_account(self, email: str) -> dict[str, Any] | None:
        """Look up a staff account by email; None when absent."""
        try:
            result = self._request("GET", "/staff-accounts", params={"email": email})
        except DotmacSubNotFoundError:
            return None
        return dict(result) if isinstance(result, dict) else None

    def set_staff_account_active(
        self, account_id: str, *, is_active: bool
    ) -> dict[str, Any]:
        """Activate/deactivate a staff account (deactivation revokes sessions)."""
        action = "activate" if is_active else "deactivate"
        result = self._request("POST", f"/staff-accounts/{account_id}/{action}")
        return dict(result) if isinstance(result, dict) else {}

    def set_staff_account_roles(
        self, account_id: str, *, roles: list[str]
    ) -> dict[str, Any]:
        """Replace only the role grants managed by ERP HR."""
        result = self._request(
            "PUT",
            f"/staff-accounts/{account_id}/roles",
            json={"roles": roles},
        )
        return dict(result) if isinstance(result, dict) else {}

    def sync_staff_account_erp_department(
        self,
        account_id: str,
        *,
        erp_employee_id: str,
        employee_code: str | None,
        erp_organization_id: str,
        department: dict[str, str | None] | None,
    ) -> dict[str, Any]:
        """Replace the ERP-managed service-team membership in dotmac_sub."""
        result = self._request(
            "PUT",
            f"/staff-accounts/{account_id}/erp-department",
            json={
                "erp_employee_id": erp_employee_id,
                "employee_code": employee_code,
                "erp_organization_id": erp_organization_id,
                "department": department,
            },
        )
        return dict(result) if isinstance(result, dict) else {}

    def _parse_reseller(self, item: dict[str, Any]) -> ResellerRecord:
        record = f"Sub reseller {item.get('id', '?')}"
        return ResellerRecord(
            id=str(item.get("id", "")),
            name=item.get("name", ""),
            code=item.get("code"),
            contact_email=item.get("contact_email"),
            contact_phone=item.get("contact_phone"),
            is_active=bool(item.get("is_active", True)),
            updated_at=_wire_updated_at(item, record=record),
        )

    def get_resellers(
        self,
        *,
        updated_since: str | None = None,
        on_parse_error: Callable[[DotmacSubParseError], None] | None = None,
    ) -> Generator[ResellerRecord, None, None]:
        logger.info("Fetching dotmac_sub resellers")
        params = {"updated_since": updated_since} if updated_since else None
        # See get_invoices for the on_parse_error row-failure contract: raising
        # out of this generator would terminate it (PEP 255) and silently drop
        # the rest of the feed.
        for item in self._sync_paginate("/resellers/sync", params=params):
            try:
                record = self._parse_reseller(item)
            except DotmacSubParseError as exc:
                if on_parse_error is None:
                    raise
                on_parse_error(exc)
                continue
            yield record

    def _parse_subscriber(self, item: dict[str, Any]) -> SubscriberRecord:
        record = f"Sub subscriber {item.get('id', '?')}"
        updated_at = _wire_updated_at(item, record=record)
        return SubscriberRecord(
            id=str(item.get("id", "")),
            first_name=item.get("first_name"),
            last_name=item.get("last_name"),
            display_name=item.get("display_name"),
            company_name=item.get("company_name"),
            legal_name=item.get("legal_name"),
            email=item.get("email"),
            phone=item.get("phone"),
            status=item.get("status"),
            category=item.get("category"),
            is_active=bool(item.get("is_active", True)),
            reseller_id=item.get("reseller_id"),
            tax_id=item.get("tax_id"),
            subscriber_number=item.get("subscriber_number"),
            account_number=item.get("account_number"),
            address_line1=item.get("address_line1"),
            address_line2=item.get("address_line2"),
            city=item.get("city"),
            region=item.get("region"),
            postal_code=item.get("postal_code"),
            country_code=item.get("country_code"),
            service_status=item.get("service_status"),
            recurring_subscription_count=int(
                item.get("recurring_subscription_count") or 0
            ),
            next_renewal_at=_parse_wire_instant(
                item.get("next_renewal_at"),
                record=record,
                field="next_renewal_at",
                updated_at=updated_at,
            ),
            billing_cycle=item.get("billing_cycle"),
            recurring_amount_monthly=_dec(item.get("recurring_amount_monthly"))
            if item.get("recurring_amount_monthly") is not None
            else None,
            annualized_recurring_revenue=_dec(item.get("annualized_recurring_revenue"))
            if item.get("annualized_recurring_revenue") is not None
            else None,
            created_at=_parse_wire_instant(
                item.get("created_at"),
                record=record,
                field="created_at",
                updated_at=updated_at,
            ),
            updated_at=updated_at,
        )

    def get_subscribers(
        self,
        subscriber_type: str | None = None,
        *,
        updated_since: str | None = None,
        on_parse_error: Callable[[DotmacSubParseError], None] | None = None,
    ) -> Generator[SubscriberRecord, None, None]:
        params: dict[str, Any] = {}
        if subscriber_type:
            params["subscriber_type"] = subscriber_type
        if updated_since:
            params["updated_since"] = updated_since
        logger.info("Fetching dotmac_sub subscribers with params: %s", params)
        # See get_invoices for the on_parse_error row-failure contract.
        for item in self._sync_paginate("/subscribers/sync", params=params):
            try:
                record = self._parse_subscriber(item)
            except DotmacSubParseError as exc:
                if on_parse_error is None:
                    raise
                on_parse_error(exc)
                continue
            yield record

    def get_subscriber(self, subscriber_id: str) -> SubscriberRecord:
        return self._parse_subscriber(
            self._request("GET", f"/subscribers/{subscriber_id}")
        )

    def _parse_billing_account(self, item: dict[str, Any]) -> BillingAccountRecord:
        record = f"Sub billing account {item.get('id', '?')}"
        return BillingAccountRecord(
            id=str(item.get("id", "")),
            reseller_id=str(item.get("reseller_id", "")),
            name=item.get("name", ""),
            currency=item.get("currency", settings.default_functional_currency_code),
            status=item.get("status", ""),
            balance=_dec(item.get("balance")),
            is_active=bool(item.get("is_active", True)),
            updated_at=_wire_updated_at(item, record=record),
        )

    def get_billing_accounts(
        self, reseller_id: str | None = None
    ) -> Generator[BillingAccountRecord, None, None]:
        params: dict[str, Any] = {}
        if reseller_id:
            params["reseller_id"] = reseller_id
        logger.info("Fetching dotmac_sub billing accounts with params: %s", params)
        for item in self._sync_paginate("/billing-accounts/sync", params=params):
            yield self._parse_billing_account(item)

    def get_billing_account(self, billing_account_id: str) -> BillingAccountRecord:
        """Fetch a single billing account by id (used to resolve its reseller)."""
        return self._parse_billing_account(
            self._request("GET", f"/billing-accounts/{billing_account_id}")
        )

    def _parse_invoice(self, item: dict[str, Any]) -> InvoiceRecord:
        """Parse a Sub invoice, failing closed on its money facts.

        Header subtotal/tax_total/total/balance_due, allocation amounts and
        supplied line amounts are money FACTS: malformed/missing values and a
        missing currency raise :class:`DotmacSubParseError` (the sync loops
        turn that into a single-row failure). Line ``quantity``/``unit_price``
        are non-money numerics and keep their lenient coercion.
        """
        record = f"Sub invoice {item.get('id', '?')}"
        updated_at = _wire_updated_at(item, record=record)
        currency = _required_currency(item, record=record, updated_at=updated_at)
        minor_units = _registry_minor_units(currency)
        lines = tuple(
            InvoiceLineRecord(
                id=str(line.get("id", "")),
                description=line.get("description", ""),
                quantity=_dec(line.get("quantity"), "1"),
                unit_price=_dec(line.get("unit_price")),
                amount=_optional_money(
                    line,
                    "amount",
                    record=f"{record} line {line.get('id', '?')}",
                    updated_at=updated_at,
                    minor_units=minor_units,
                ),
                tax_rate_id=line.get("tax_rate_id"),
                tax_application=_wire_tax_application(
                    line,
                    record=f"{record} line {line.get('id', '?')}",
                    updated_at=updated_at,
                ),
            )
            for line in item.get("lines", [])
        )
        return InvoiceRecord(
            id=str(item.get("id", "")),
            account_id=str(item.get("account_id", "")),
            invoice_number=item.get("invoice_number"),
            status=item.get("status", ""),
            currency=currency,
            subtotal=_required_money(
                item,
                "subtotal",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            tax_total=_required_money(
                item,
                "tax_total",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            total=_required_money(
                item,
                "total",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            balance_due=_required_money(
                item,
                "balance_due",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            issued_at=_parse_wire_instant(
                item.get("issued_at"),
                record=record,
                field="issued_at",
                updated_at=updated_at,
            ),
            due_at=_parse_wire_instant(
                item.get("due_at"), record=record, field="due_at", updated_at=updated_at
            ),
            paid_at=_parse_wire_instant(
                item.get("paid_at"),
                record=record,
                field="paid_at",
                updated_at=updated_at,
            ),
            memo=item.get("memo"),
            is_proforma=bool(item.get("is_proforma", False)),
            updated_at=updated_at,
            account=(
                self._parse_subscriber(item["account"])
                if isinstance(item.get("account"), dict)
                else None
            ),
            lines=lines,
            allocations=_allocations(
                item.get("payment_allocations"),
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
        )

    def get_invoices(
        self,
        account_id: str | None = None,
        status: str | None = None,
        *,
        updated_since: str | None = None,
        on_parse_error: Callable[[DotmacSubParseError], None] | None = None,
    ) -> Generator[InvoiceRecord, None, None]:
        params = _watermark_params(account_id, status, updated_since)
        logger.info("Fetching dotmac_sub invoices with params: %s", params)
        # Bulk AR pulls use Sub's sync-specific projection: it includes invoice
        # lines but omits payment allocations and UI/detail fields. A larger page
        # keeps the initial backfill efficient without making Sub hydrate the
        # expensive full InvoiceRead graph for every row.
        #
        # ``on_parse_error`` turns a strict money-fact rejection into a
        # single-row failure: raising out of this generator would terminate it
        # (PEP 255) and silently drop the rest of the feed, so the sync mixins
        # pass a collector and the pull continues with the next row. Without a
        # collector the typed error propagates (single-record semantics).
        for item in self._sync_paginate("/invoices/sync", params=params):
            try:
                record = self._parse_invoice(item)
            except DotmacSubParseError as exc:
                if on_parse_error is None:
                    raise
                on_parse_error(exc)
                continue
            yield record

    def get_invoice(self, invoice_id: str) -> InvoiceRecord:
        return self._parse_invoice(self._request("GET", f"/invoices/{invoice_id}"))

    def _parse_payment(self, item: dict[str, Any]) -> PaymentRecord:
        """Parse a Sub payment, failing closed on its settlement money facts.

        ``amount`` and the currency are mandatory (the pull contract lists
        currency among the payment settlement facts); ``gross_amount``/
        ``net_amount`` are nullable facts; ``refunded_amount``/``wht_amount``
        keep their DOCUMENTED absent-field default of 0 (deploy-order
        compatibility) but a present value must parse strictly. ``wht_rate``
        is a percentage rate, not money.
        """
        record = f"Sub payment {item.get('id', '?')}"
        updated_at = _wire_updated_at(item, record=record)
        currency = _required_currency(item, record=record, updated_at=updated_at)
        minor_units = _registry_minor_units(currency)
        return PaymentRecord(
            id=str(item.get("id", "")),
            account_id=item.get("account_id"),
            billing_account_id=item.get("billing_account_id"),
            amount=_required_money(
                item,
                "amount",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            refunded_amount=_defaulted_money(
                item,
                "refunded_amount",
                "0",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            gross_amount=_optional_money(
                item,
                "gross_amount",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            net_amount=_optional_money(
                item,
                "net_amount",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            wht_amount=_defaulted_money(
                item,
                "wht_amount",
                "0",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            wht_rate=_dec(item.get("wht_rate"))
            if item.get("wht_rate") is not None
            else None,
            wht_status=item.get("wht_status"),
            wht_record_id=item.get("wht_record_id"),
            wht_certificate_reference=item.get("wht_certificate_reference"),
            wht_resolved_at=_parse_wire_instant(
                item.get("wht_resolved_at"),
                record=record,
                field="wht_resolved_at",
                updated_at=updated_at,
            ),
            currency=currency,
            status=item.get("status", ""),
            paid_at=_parse_wire_instant(
                item.get("paid_at"),
                record=record,
                field="paid_at",
                updated_at=updated_at,
            ),
            external_id=item.get("external_id"),
            memo=item.get("memo"),
            payment_method_id=item.get("payment_method_id"),
            payment_channel_id=item.get("payment_channel_id"),
            updated_at=updated_at,
            allocations=_allocations(
                item.get("allocations"),
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
        )

    def get_payments(
        self,
        account_id: str | None = None,
        status: str | None = None,
        *,
        updated_since: str | None = None,
        on_parse_error: Callable[[DotmacSubParseError], None] | None = None,
    ) -> Generator[PaymentRecord, None, None]:
        params = _watermark_params(account_id, status, updated_since)
        logger.info("Fetching dotmac_sub payments with params: %s", params)
        # See get_invoices for the on_parse_error row-failure contract.
        for item in self._sync_paginate("/payments/sync", params=params):
            try:
                record = self._parse_payment(item)
            except DotmacSubParseError as exc:
                if on_parse_error is None:
                    raise
                on_parse_error(exc)
                continue
            yield record

    def get_payment(self, payment_id: str) -> PaymentRecord:
        return self._parse_payment(self._request("GET", f"/payments/{payment_id}"))

    def _parse_credit_note(self, item: dict[str, Any]) -> CreditNoteRecord:
        """Parse a Sub credit note, failing closed on its money facts (same
        rules as :meth:`_parse_invoice`; ``applied_total`` keeps a documented
        absent-field default of 0 — nothing applied yet)."""
        record = f"Sub credit note {item.get('id', '?')}"
        updated_at = _wire_updated_at(item, record=record)
        currency = _required_currency(item, record=record, updated_at=updated_at)
        minor_units = _registry_minor_units(currency)
        lines = tuple(
            CreditNoteLineRecord(
                id=str(line.get("id", "")),
                description=line.get("description", ""),
                quantity=_dec(line.get("quantity"), "1"),
                unit_price=_dec(line.get("unit_price")),
                amount=_optional_money(
                    line,
                    "amount",
                    record=f"{record} line {line.get('id', '?')}",
                    updated_at=updated_at,
                    minor_units=minor_units,
                ),
                tax_rate_id=line.get("tax_rate_id"),
                tax_application=_wire_tax_application(
                    line,
                    record=f"{record} line {line.get('id', '?')}",
                    updated_at=updated_at,
                ),
            )
            for line in item.get("lines", [])
        )
        return CreditNoteRecord(
            id=str(item.get("id", "")),
            account_id=str(item.get("account_id", "")),
            invoice_id=item.get("invoice_id"),
            credit_number=item.get("credit_number"),
            status=item.get("status", ""),
            currency=currency,
            subtotal=_required_money(
                item,
                "subtotal",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            tax_total=_required_money(
                item,
                "tax_total",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            total=_required_money(
                item,
                "total",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            applied_total=_defaulted_money(
                item,
                "applied_total",
                "0",
                record=record,
                updated_at=updated_at,
                minor_units=minor_units,
            ),
            memo=item.get("memo"),
            issued_at=_parse_wire_instant(
                item.get("issued_at") or item.get("created_at"),
                record=record,
                field="issued_at",
                updated_at=updated_at,
            ),
            updated_at=updated_at,
            lines=lines,
        )

    def get_credit_notes(
        self,
        account_id: str | None = None,
        status: str | None = None,
        *,
        updated_since: str | None = None,
        on_parse_error: Callable[[DotmacSubParseError], None] | None = None,
    ) -> Generator[CreditNoteRecord, None, None]:
        params = _watermark_params(account_id, status, updated_since)
        logger.info("Fetching dotmac_sub credit notes with params: %s", params)
        # See get_invoices for the on_parse_error row-failure contract.
        for item in self._sync_paginate("/credit-notes/sync", params=params):
            try:
                record = self._parse_credit_note(item)
            except DotmacSubParseError as exc:
                if on_parse_error is None:
                    raise
                on_parse_error(exc)
                continue
            yield record

    def get_tax_rates(self) -> list[TaxRateRecord]:
        rates: list[TaxRateRecord] = []
        for item in self._sync_paginate("/tax-rates/sync"):
            rates.append(
                TaxRateRecord(
                    id=str(item.get("id", "")),
                    name=item.get("name", ""),
                    rate=_dec(item.get("rate")),
                    code=item.get("code"),
                    is_active=bool(item.get("is_active", True)),
                )
            )
        return rates

    def get_payment_channels(self) -> Generator[dict[str, Any], None, None]:
        yield from self._sync_paginate("/payment-channels/sync")

    def test_connection(self) -> bool:
        try:
            self._request("GET", "/subscribers/sync", params={"limit": 1})
            return True
        except DotmacSubError as e:
            logger.error("dotmac_sub connection test failed: %s", e.message)
            return False
