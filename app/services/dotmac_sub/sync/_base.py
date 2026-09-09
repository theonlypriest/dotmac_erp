from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc  # type: ignore[assignment]

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.db.session_context import prime_tenant_context
from app.models.finance.ar.customer import Customer
from app.models.finance.ar.dotmac_sub_sync_watermark import DotmacSubSyncWatermark
from app.models.finance.ar.external_sync import (
    EntityType,
    ExternalSource,
    ExternalSync,
)
from app.models.finance.ar.invoice import Invoice
from app.models.finance.tax.tax_code import TaxCode, TaxType
from app.services.dotmac_sub.client import (
    DotmacSubClient,
    DotmacSubConfig,
    SubscriberRecord,
    TaxRateRecord,
)

from ._constants import DEFAULT_BANK_NAME_MAPPING

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncWatermarkPosition:
    """Last source row safely consumed by a deterministic sync feed."""

    watermark_at: datetime | None
    external_id: str | None = None

    def includes(self, row_updated_at: datetime | None, row_external_id: str) -> bool:
        """Return True when a row is at or before this consumed position."""
        if self.watermark_at is None or row_updated_at is None:
            return False
        cursor_at = _aware_utc(self.watermark_at)
        row_at = _aware_utc(row_updated_at)
        if row_at < cursor_at:
            return True
        if row_at > cursor_at:
            return False
        if self.external_id is None:
            return False
        return str(row_external_id) <= self.external_id


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_watermark(
    current: datetime | None,
    max_ok: datetime | None,
    min_error: datetime | None,
) -> datetime | None:
    """Compute the watermark to persist after a batch (advance-only).

    Rows are pulled in ascending ``updated_at`` order. ``max_ok`` is the highest
    ``updated_at`` processed without error; ``min_error`` is the lowest
    ``updated_at`` of a row that failed. We never advance past the earliest
    failure — the watermark filter is inclusive (``updated_at >= wm``), so
    parking it at ``min_error`` re-pulls (and retries) that row next cycle while
    re-processing the successful rows at/after it idempotently. With no failures
    we advance to ``max_ok``. Returns ``current`` unchanged when there is nothing
    to advance to, or when the candidate would move the watermark backward.
    """
    candidate = min_error if min_error is not None else max_ok
    if candidate is None:
        return current
    if current is not None and candidate <= current:
        return current
    return candidate


# Bounded non-blocking acquire for the per-subscriber customer lock. Batch
# transactions hold every acquired xact lock until the outer commit (up to 500
# entities between commits; savepoints release nothing), so total worst-case
# wait stays short of the batch cadence while never joining a blocking cycle.
_CUSTOMER_LOCK_ATTEMPTS = 20
_CUSTOMER_LOCK_RETRY_DELAY_SECONDS = 0.25


class CustomerLockContentionError(RuntimeError):
    """Another sync stream holds this subscriber's customer lock.

    Raised after the bounded try-lock poll gives up. Callers' per-entity
    error handling defers just the contended entity; the next sync run
    retries it once the competing batch transaction has committed.
    """


class BaseSyncMixin:
    """Core utilities shared by all dotmac_sub sync mixins."""

    SOURCE_PREFIX = "DSUB"

    # ar.customer.customer_code is VARCHAR(30); an untruncated
    # "DSUB-R-<uuid>" (43 chars) fails the INSERT outright.
    CUSTOMER_CODE_MAX = 30

    def _customer_code(self, marker: str, ref: str) -> str:
        """Deterministic ``customer_code`` within the column's 30-char limit.

        Short human refs (account numbers) pass through untouched; UUID-style
        refs are compacted to dash-less hex before truncating so the kept
        portion is maximally distinctive.
        """
        prefix = (
            f"{self.SOURCE_PREFIX}-{marker}-" if marker else f"{self.SOURCE_PREFIX}-"
        )
        if len(prefix) + len(ref) > self.CUSTOMER_CODE_MAX:
            ref = ref.replace("-", "")
        return (prefix + ref)[: self.CUSTOMER_CODE_MAX]

    # Provided by sibling mixins at runtime (combined in DotmacSubSyncService).
    _cache_reseller: Any
    _sync_single_subscriber: Any

    def __init__(
        self,
        db: Session,
        organization_id: UUID,
        ar_control_account_id: UUID,
        default_revenue_account_id: UUID | None = None,
        config: DotmacSubConfig | None = None,
        bank_name_mapping: dict[str, str | None] | None = None,
    ) -> None:
        self.db = db
        self.organization_id = organization_id
        self.ar_control_account_id = ar_control_account_id
        self.default_revenue_account_id = default_revenue_account_id
        self.config = config or DotmacSubConfig.for_org(db, organization_id)
        self._client: DotmacSubClient | None = None
        self._bank_name_mapping = bank_name_mapping or DEFAULT_BANK_NAME_MAPPING

        self._reseller_cache: dict[str, UUID] = {}
        self._subscriber_cache: dict[str, UUID] = {}
        self._account_cache: dict[str, UUID] = {}
        # account_ids that could not be resolved to an ERP customer this run
        # (orphaned in dotmac_sub, or the resolve call was rate-limited). Cached
        # so the same account is not re-fetched for every invoice/payment that
        # references it within one run. Reset per run, so it is retried next run.
        self._unresolvable_accounts: set[str] = set()
        self._payment_channel_names: dict[str, str] = {}
        self._bank_account_mapping: dict[str, UUID] = {}
        self._default_bank_account_cache: dict[str, UUID] = {}

        self._source_tax_rates: dict[str, TaxRateRecord] | None = None
        self._source_tax_code_cache: dict[tuple[str, str, date], TaxCode] = {}
        self._source_wht_code_cache: dict[tuple[Decimal, date], TaxCode] = {}

    def _reprime_tenant_context(self) -> None:
        prime_tenant_context(self.db, self.organization_id)

    def _functional_amount(
        self, amount: Decimal, currency_code: str, on_date: date
    ) -> tuple[Decimal, Decimal]:
        """Resolve ``(exchange_rate, functional_amount)`` for a synced document.

        ``exchange_rate`` is the foreign→functional rate (functional = amount *
        rate). Falls back to ``1.0`` (no conversion) when the document is already
        in functional currency or no SPOT rate is configured. Never raises — a
        missing rate degrades gracefully rather than failing the whole sync.
        Shared by payments, invoices, and credit notes so all three record the
        same functional amount and the AR subledger nets to zero.
        """
        from app.services.finance.platform.fx import FXService

        info = FXService.lookup_spot_rate(
            self.db, self.organization_id, currency_code, on_date
        )
        # In lookup_spot_rate, ``from`` is the org functional currency and ``to``
        # is currency_code, so ``inverse_rate`` is currency_code → functional.
        raw = info.get("inverse_rate")
        if raw in (None, ""):
            return Decimal("1"), amount
        try:
            rate = Decimal(str(raw))
        except (ValueError, ArithmeticError):
            return Decimal("1"), amount
        if rate <= 0:
            return Decimal("1"), amount
        return rate, (amount * rate).quantize(Decimal("0.000001"))

    @property
    def client(self) -> DotmacSubClient:
        if self._client is None:
            self._client = DotmacSubClient(self.config)
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def _get_source_tax_rate(self, source_tax_rate_id: str) -> TaxRateRecord:
        """Return a Sub tax-rate fact without making Sub's id an ERP account key."""
        if self._source_tax_rates is None:
            self._source_tax_rates = {
                rate.id: rate for rate in self.client.get_tax_rates()
            }
        rate = self._source_tax_rates.get(source_tax_rate_id)
        if rate is None:
            raise ValueError(
                f"dotmac_sub tax rate {source_tax_rate_id} is missing from the tax feed"
            )
        return rate

    @staticmethod
    def _source_rate_ratio(rate_percent: Decimal) -> Decimal:
        """Sub stores percentages (7.5); ERP TaxCode stores ratios (0.075)."""
        return (rate_percent / Decimal("100")).normalize()

    def _resolve_source_sales_tax_code(
        self,
        *,
        source_tax_rate_id: str,
        tax_application: str,
        effective_date: date,
    ) -> TaxCode:
        """Resolve a source tax fact to exactly one ERP-owned sales tax code.

        The source id is never treated as a chart/account identifier. ERP's own
        effective TaxCode records and their control-account mappings decide the
        posting. Missing or ambiguous configuration fails closed.
        """
        application = (tax_application or "exclusive").strip().lower()
        if application not in {"exclusive", "inclusive"}:
            raise ValueError(f"Unsupported taxable application: {tax_application}")
        key = (source_tax_rate_id, application, effective_date)
        cached = self._source_tax_code_cache.get(key)
        if cached is not None:
            return cached

        source_rate = self._get_source_tax_rate(source_tax_rate_id)
        ratio = self._source_rate_ratio(source_rate.rate)
        predicates = [
            TaxCode.organization_id == self.organization_id,
            TaxCode.tax_type.in_([TaxType.VAT, TaxType.GST, TaxType.SALES_TAX]),
            TaxCode.applies_to_sales.is_(True),
            TaxCode.tax_rate == ratio,
            TaxCode.is_inclusive.is_(application == "inclusive"),
            TaxCode.is_active.is_(True),
            TaxCode.effective_from <= effective_date,
            or_(
                TaxCode.effective_to.is_(None),
                TaxCode.effective_to >= effective_date,
            ),
            TaxCode.tax_collected_account_id.is_not(None),
        ]
        if source_rate.code:
            predicates.append(TaxCode.tax_code == source_rate.code)
        candidates = list(self.db.scalars(select(TaxCode).where(*predicates)).all())
        if len(candidates) != 1:
            raise ValueError(
                "ERP must have exactly one effective sales tax code with a "
                f"collected-tax account for Sub rate {source_rate.name} "
                f"({source_rate.rate}%, {application}); found {len(candidates)}"
            )
        self._source_tax_code_cache[key] = candidates[0]
        return candidates[0]

    def _resolve_source_wht_code(
        self, *, rate_percent: Decimal, effective_date: date
    ) -> TaxCode:
        """Resolve customer-deducted WHT to one ERP WHT-receivable tax code."""
        key = (rate_percent.normalize(), effective_date)
        cached = self._source_wht_code_cache.get(key)
        if cached is not None:
            return cached
        ratio = self._source_rate_ratio(rate_percent)
        candidates = list(
            self.db.scalars(
                select(TaxCode).where(
                    TaxCode.organization_id == self.organization_id,
                    TaxCode.tax_type == TaxType.WITHHOLDING,
                    TaxCode.tax_rate == ratio,
                    TaxCode.is_active.is_(True),
                    TaxCode.effective_from <= effective_date,
                    or_(
                        TaxCode.effective_to.is_(None),
                        TaxCode.effective_to >= effective_date,
                    ),
                    TaxCode.tax_paid_account_id.is_not(None),
                )
            ).all()
        )
        if len(candidates) != 1:
            raise ValueError(
                "ERP must have exactly one effective WHT code with a receivable "
                f"account for Sub rate {rate_percent}%; found {len(candidates)}"
            )
        self._source_wht_code_cache[key] = candidates[0]
        return candidates[0]

    def _generate_invoice_number(self, reference_date: date | None = None) -> str:
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        return SyncNumberingService(self.db).generate_next_number(
            self.organization_id, SequenceType.INVOICE, reference_date
        )

    def _generate_payment_number(self, reference_date: date | None = None) -> str:
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        return SyncNumberingService(self.db).generate_next_number(
            self.organization_id, SequenceType.PAYMENT, reference_date
        )

    def _generate_credit_note_number(self, reference_date: date | None = None) -> str:
        from app.models.finance.core_config.numbering_sequence import SequenceType
        from app.services.finance.common.numbering import SyncNumberingService

        return SyncNumberingService(self.db).generate_next_number(
            self.organization_id, SequenceType.CREDIT_NOTE, reference_date
        )

    # Wire timestamps are NOT parsed here. ``dotmac_sub.client`` admits every
    # record field as a typed ``datetime`` (``_parse_wire_instant``), so this
    # sync layer consumes real instants and there is exactly ONE owner of the
    # wire-text -> instant decision. A malformed timestamp is a typed row
    # rejection at parse time, routed through the same collector as a
    # malformed money fact, not a silent ``None`` discovered down here.

    # ---- Incremental-sync high-watermark ----

    def _get_sync_watermark_row(
        self, entity_type: EntityType
    ) -> DotmacSubSyncWatermark | None:
        """Return this entity's persisted or pending watermark row."""
        stmt = select(DotmacSubSyncWatermark).where(
            DotmacSubSyncWatermark.organization_id == self.organization_id,
            DotmacSubSyncWatermark.entity_type == entity_type.value,
        )
        row = self.db.scalar(stmt)
        if row is not None:
            return row

        for pending in getattr(self.db, "new", ()):
            if (
                isinstance(pending, DotmacSubSyncWatermark)
                and pending.organization_id == self.organization_id
                and pending.entity_type == entity_type.value
            ):
                return pending
        return None

    def _get_sync_watermark(self, entity_type: EntityType) -> datetime | None:
        """Highest ``updated_at`` already synced for this entity type, or None
        (never synced → the caller does a full pull)."""
        row = self._get_sync_watermark_row(entity_type)
        return None if row is None else row.watermark_at

    def _get_sync_watermark_position(
        self, entity_type: EntityType
    ) -> SyncWatermarkPosition:
        row = self._get_sync_watermark_row(entity_type)
        if row is None:
            return SyncWatermarkPosition(None, None)
        return SyncWatermarkPosition(row.watermark_at, row.watermark_external_id)

    def _advance_sync_watermark(
        self, entity_type: EntityType, new_value: datetime | None
    ) -> None:
        """Move the watermark forward to ``new_value`` (never backward)."""
        if new_value is None:
            return
        row = self._get_sync_watermark_row(entity_type)
        if row is None:
            self.db.add(
                DotmacSubSyncWatermark(
                    organization_id=self.organization_id,
                    entity_type=entity_type.value,
                    watermark_at=new_value,
                )
            )
            return
        current = row.watermark_at
        # A naive stored value only happens on the SQLite test backend
        # (DateTime(timezone=True) drops tzinfo there); treat it as UTC so the
        # advance-only comparison never mixes aware/naive. Postgres stores aware.
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current is None or new_value > current:
            row.watermark_at = new_value
            row.watermark_external_id = None

    def _advance_sync_watermark_position(
        self, entity_type: EntityType, position: SyncWatermarkPosition | None
    ) -> None:
        """Move the compound watermark forward to a consumed source row."""
        if position is None or position.watermark_at is None:
            return
        row = self._get_sync_watermark_row(entity_type)
        new_at = _aware_utc(position.watermark_at)
        if row is None:
            self.db.add(
                DotmacSubSyncWatermark(
                    organization_id=self.organization_id,
                    entity_type=entity_type.value,
                    watermark_at=position.watermark_at,
                    watermark_external_id=position.external_id,
                )
            )
            return

        current = row.watermark_at
        if current is None:
            row.watermark_at = position.watermark_at
            row.watermark_external_id = position.external_id
            return
        current_at = _aware_utc(current)
        current_id = row.watermark_external_id
        if new_at > current_at or (
            new_at == current_at
            and position.external_id is not None
            and (current_id is None or position.external_id > current_id)
        ):
            row.watermark_at = position.watermark_at
            row.watermark_external_id = position.external_id

    def _get_synced_entity(
        self, entity_type: EntityType, external_id: str
    ) -> UUID | None:
        stmt = select(ExternalSync.local_entity_id).where(
            ExternalSync.organization_id == self.organization_id,
            ExternalSync.source == ExternalSource.DOTMAC_SUB,
            ExternalSync.entity_type == entity_type,
            ExternalSync.external_id == external_id,
        )
        return self.db.scalar(stmt)

    def _record_sync(
        self,
        entity_type: EntityType,
        external_id: str,
        local_entity_id: UUID,
        data_hash: str | None = None,
        external_updated_at: datetime | None = None,
    ) -> None:
        existing = self._get_synced_entity(entity_type, external_id)
        if existing:
            stmt = select(ExternalSync).where(
                ExternalSync.organization_id == self.organization_id,
                ExternalSync.source == ExternalSource.DOTMAC_SUB,
                ExternalSync.entity_type == entity_type,
                ExternalSync.external_id == external_id,
            )
            sync_record = self.db.scalar(stmt)
            if sync_record:
                sync_record.synced_at = datetime.now(tz=UTC)
                sync_record.sync_hash = data_hash
                sync_record.local_entity_id = local_entity_id
                if external_updated_at:
                    sync_record.external_updated_at = external_updated_at
        else:
            self.db.add(
                ExternalSync(
                    organization_id=self.organization_id,
                    source=ExternalSource.DOTMAC_SUB,
                    entity_type=entity_type,
                    external_id=external_id,
                    local_entity_id=local_entity_id,
                    sync_hash=data_hash,
                    external_updated_at=external_updated_at,
                )
            )

    def _compute_hash(self, data: dict[str, Any]) -> str:
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode()).hexdigest()[:32]

    def _has_changed(
        self, entity_type: EntityType, external_id: str, new_hash: str
    ) -> bool:
        stmt = select(ExternalSync.sync_hash).where(
            ExternalSync.organization_id == self.organization_id,
            ExternalSync.source == ExternalSource.DOTMAC_SUB,
            ExternalSync.entity_type == entity_type,
            ExternalSync.external_id == external_id,
        )
        old_hash = self.db.scalar(stmt)
        return old_hash != new_hash

    def _get_existing_invoice(self, invoice_number: str) -> Invoice | None:
        stmt = select(Invoice).where(
            Invoice.organization_id == self.organization_id,
            Invoice.invoice_number == invoice_number,
        )
        return self.db.scalar(stmt)

    def _map_invoice_status(self, status: str, balance_due: Decimal) -> Any:
        from app.models.finance.ar.invoice import InvoiceStatus

        s = (status or "").lower()
        # Check terminal/explicit statuses BEFORE the zero-balance shortcut: a
        # voided invoice typically carries balance_due == 0, so the PAID branch
        # would otherwise mislabel it as PAID.
        if s == "void":
            return InvoiceStatus.VOID
        if s == "paid" or balance_due == Decimal("0"):
            return InvoiceStatus.PAID
        if s == "partially_paid":
            return InvoiceStatus.PARTIALLY_PAID
        return InvoiceStatus.POSTED

    def _get_customer_for_account(
        self,
        account_id: str,
        account: SubscriberRecord | None = None,
    ) -> UUID | None:
        if account_id in self._account_cache:
            return self._account_cache[account_id]
        if account_id in self._unresolvable_accounts:
            # Already determined unresolvable earlier this run. Don't re-hit
            # dotmac_sub for the same account again (it will stay unresolved
            # this run, and re-fetching only burns the rate limit); retried
            # fresh on the next sync run.
            return None
        mapped = self._get_synced_entity(EntityType.BILLING_ACCOUNT, account_id)
        if mapped:
            self._account_cache[account_id] = mapped
            return mapped
        customer_id = self._resolve_account_owner(account_id, account)
        if customer_id:
            self._account_cache[account_id] = customer_id
            self._record_sync(EntityType.BILLING_ACCOUNT, account_id, customer_id)
        else:
            self._unresolvable_accounts.add(account_id)
        return customer_id

    def _resolve_account_owner(
        self,
        account_id: str,
        account: SubscriberRecord | None = None,
    ) -> UUID | None:
        """Resolve an invoice/payment ``account_id`` to its owning ERP customer.

        Verified against the live dotmac_sub API (2026-06-16): for invoices,
        payments and credit notes ``account_id`` is the **subscriber id** (it
        resolves via ``GET /subscribers/{id}``, not ``/billing-accounts``). So we
        resolve subscriber-first to that subscriber's ERP customer (the reseller
        link is grouping only — invoices/payments post to GL normally).

        Fallback: the 34 reseller-named "billing accounts" are a separate
        consolidation concept; if an ``account_id`` is one of those instead, map
        it to the reseller's ERP *parent* customer (which posts to GL).
        """
        from app.services.dotmac_sub.client import (
            DotmacSubError,
            DotmacSubRateLimitError,
        )

        # 1) Already-synced subscriber → child customer.
        cust = self._get_customer_by_dotmac_sub_id(account_id)
        if cust:
            return cust.customer_id

        # 2) Subscriber not yet synced this run → fetch + upsert on demand so the
        #    invoice/payment can attach (subscribers are normally synced first,
        #    but batch limits or webhooks can arrive out of order).
        sub = account
        if sub is None:
            try:
                sub = self.client.get_subscriber(account_id)
            except DotmacSubRateLimitError:
                # Throttled, not missing. Skip the billing-account fallback (it
                # would be throttled too) and retry the account on the next run.
                return None
            except DotmacSubError:
                sub = None
        if sub is not None:
            if sub.reseller_id:
                self._cache_reseller(sub.reseller_id)
            from ._types import SyncResult

            self._sync_single_subscriber(
                sub, None, SyncResult(success=True, entity_type="subscribers"), False
            )
            resolved = self._get_customer_by_dotmac_sub_id(account_id)
            if resolved:
                return resolved.customer_id

        # 3) Fallback: a reseller-level billing account → reseller parent customer.
        try:
            account = self.client.get_billing_account(account_id)
        except DotmacSubRateLimitError:
            return None
        except DotmacSubError:
            account = None
        if account and account.reseller_id:
            return self._reseller_customer_id(account.reseller_id)
        return None

    def _lock_dotmac_sub_customer(self, dotmac_sub_id: str) -> None:
        """Serialize concurrent customer upserts for one dotmac_sub subscriber.

        ``Customer.dotmac_sub_id`` is non-unique, and the subscriber can be
        created from two paths that race: the batch subscriber sync and the
        on-demand ``_resolve_account_owner`` upsert (when an invoice/payment
        arrives before its subscriber). Without a guard both find "not found"
        and both insert a customer for one subscriber, fragmenting that
        subscriber's AR across two accounts. A transaction-level advisory lock
        keyed on (org, dotmac_sub_id) makes the second path wait until the
        first commits. No-op off PostgreSQL (the SQLite test harness).

        The acquire is a bounded ``pg_try_advisory_xact_lock`` poll, never a
        blocking wait: xact locks are held to the OUTER commit (up to 500
        entities between commits; per-entity savepoints release nothing), so
        two sync streams accumulating the same per-subscriber locks in
        different orders deadlocked in prod (2026-07-18, invoice sync vs
        subscriber sync). A poll cannot join a Postgres wait cycle; when the
        budget runs out, :class:`CustomerLockContentionError` defers just this
        entity to the caller's per-entity handling and the next run.
        """
        if not dotmac_sub_id or self.db.get_bind().dialect.name != "postgresql":
            return
        key = f"erp_customer:{self.organization_id}:{dotmac_sub_id}"
        for attempt in range(_CUSTOMER_LOCK_ATTEMPTS):
            acquired = self.db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": key},
            ).scalar()
            if acquired:
                return
            if attempt < _CUSTOMER_LOCK_ATTEMPTS - 1:
                time.sleep(_CUSTOMER_LOCK_RETRY_DELAY_SECONDS)
        raise CustomerLockContentionError(
            f"customer lock for dotmac_sub subscriber {dotmac_sub_id} still "
            f"held by a concurrent sync after {_CUSTOMER_LOCK_ATTEMPTS} "
            "attempts; entity deferred to the next run"
        )

    def _get_customer_by_dotmac_sub_id(self, dotmac_sub_id: str) -> Customer | None:
        stmt = select(Customer).where(
            Customer.organization_id == self.organization_id,
            Customer.dotmac_sub_id == dotmac_sub_id,
        )
        return self.db.scalar(stmt)

    def _reseller_customer_id(self, reseller_id: str) -> UUID | None:
        if reseller_id in self._reseller_cache:
            return self._reseller_cache[reseller_id]
        stmt = select(Customer).where(
            Customer.organization_id == self.organization_id,
            Customer.dotmac_sub_reseller_id == reseller_id,
            Customer.parent_customer_id.is_(None),
        )
        reseller = self.db.scalar(stmt)
        if reseller:
            self._reseller_cache[reseller_id] = reseller.customer_id
            return reseller.customer_id
        return None
