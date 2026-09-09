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

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.models.finance.ar.external_sync import EntityType
from app.models.finance.ar.invoice import Invoice, InvoiceType
from app.models.finance.ar.invoice_line import InvoiceLine
from app.models.finance.ar.invoice_line_tax import InvoiceLineTax
from app.services.dotmac_sub.client import (
    CreditNoteRecord,
    DotmacSubError,
    InvoiceRecord,
    TaxApplication,
)
from app.services.finance.money_boundary import round_to_minor_units, to_boundary_money

from ._base import SyncWatermarkPosition
from ._constants import DOTMAC_SUB_SYNC_MIN_DATE, SYSTEM_USER_ID, _PRE_CUTOFF_SENTINEL
from ._progress import WatermarkPositionProgress
from ._types import SyncResult

logger = logging.getLogger(__name__)


def _invoice_hash_payload(inv: InvoiceRecord) -> dict[str, Any]:
    """Return every source field that can change the mirrored AR invoice."""
    return {
        "account_id": inv.account_id,
        "number": inv.invoice_number,
        "status": inv.status,
        "currency": inv.currency,
        "subtotal": str(inv.subtotal),
        "tax_total": str(inv.tax_total),
        "total": str(inv.total),
        "balance_due": str(inv.balance_due),
        # Typed instants are formatted back to the canonical wire text for the
        # change hash — the record no longer carries the raw string.
        "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
        "due_at": inv.due_at.isoformat() if inv.due_at else None,
        "memo": inv.memo,
        "is_proforma": inv.is_proforma,
        "lines": [
            {
                "id": line.id,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "amount": str(line.amount),
                "tax_rate_id": line.tax_rate_id,
                "tax_application": line.tax_application.value,
            }
            for line in sorted(inv.lines, key=lambda item: item.id)
        ],
    }


class InvoiceSyncMixin:
    """Sync dotmac_sub invoices into ERP's canonical AR and GL."""

    db: Any
    client: Any
    organization_id: UUID
    ar_control_account_id: UUID
    default_revenue_account_id: UUID | None
    _compute_hash: Any
    _has_changed: Any
    _record_sync: Any
    _get_synced_entity: Any
    _get_existing_invoice: Any
    _get_customer_for_account: Any
    _get_sync_watermark: Any
    _advance_sync_watermark: Any
    _generate_invoice_number: Any
    _generate_credit_note_number: Any
    _map_invoice_status: Any
    _get_source_tax_rate: Any
    _resolve_source_sales_tax_code: Any
    _reprime_tenant_context: Any
    _functional_amount: Any
    _get_sync_watermark_position: Any
    _advance_sync_watermark_position: Any

    def _invoice_watermark_position(self) -> SyncWatermarkPosition:
        getter = getattr(self, "_get_sync_watermark_position", None)
        if getter is not None:
            return getter(EntityType.INVOICE)
        return SyncWatermarkPosition(self._get_sync_watermark(EntityType.INVOICE), None)

    def _advance_invoice_watermark_position(
        self, position: SyncWatermarkPosition | None
    ) -> None:
        advancer = getattr(self, "_advance_sync_watermark_position", None)
        if advancer is not None:
            advancer(EntityType.INVOICE, position)
            return
        if position is not None:
            self._advance_sync_watermark(EntityType.INVOICE, position.watermark_at)

    def sync_invoices(
        self,
        account_id: str | None = None,
        status: str | None = None,
        created_by_user_id: UUID | None = None,
        batch_size: int | None = None,
        skip_unchanged: bool = True,
    ) -> SyncResult:
        result = SyncResult(success=True, entity_type="invoices")
        processed = 0
        # Incremental pull: only when this is the unfiltered full sync (a
        # targeted account_id/status pull must not touch the global cursor).
        use_watermark = account_id is None and status is None
        watermark_position = (
            self._invoice_watermark_position() if use_watermark else None
        )
        watermark = watermark_position.watermark_at if watermark_position else None
        updated_since = watermark.isoformat() if watermark else None
        # ONE shared owner for the compound cursor decision (see
        # _progress.WatermarkPositionProgress); parse rejections and savepoint
        # row failures flow through the same accounting.
        progress = WatermarkPositionProgress(
            watermark_position or SyncWatermarkPosition(None, None),
            label="invoice",
        )
        limit_reached = False

        try:
            for inv in self.client.get_invoices(
                account_id=account_id,
                status=status,
                updated_since=updated_since,
                on_parse_error=progress.parse_error_collector(result),
            ):
                if (
                    use_watermark
                    and watermark_position is not None
                    and watermark_position.includes(inv.updated_at, inv.id)
                ):
                    continue
                if batch_size and processed >= batch_size:
                    limit_reached = True
                    break
                row_updated_at = inv.updated_at
                savepoint = None
                try:
                    savepoint = self.db.begin_nested()
                    self._sync_single_invoice(
                        inv, created_by_user_id, result, skip_unchanged
                    )
                    savepoint.commit()
                    processed += 1
                    progress.record_success(row_updated_at, inv.id)
                    if processed % 500 == 0:
                        self.db.commit()
                        self._reprime_tenant_context()
                        self.db.expunge_all()
                        logger.info("Progress: %d invoices processed", processed)
                except OperationalError:
                    if savepoint is not None:
                        try:
                            savepoint.rollback()
                        except Exception:  # noqa: BLE001
                            self.db.rollback()
                    else:
                        self.db.rollback()
                    logger.exception(
                        "Database error syncing invoice %s; aborting dotmac_sub run",
                        inv.id,
                    )
                    raise
                except Exception as e:  # noqa: BLE001
                    try:
                        if savepoint is not None:
                            savepoint.rollback()
                        else:
                            self.db.rollback()
                    except Exception:  # noqa: BLE001
                        self.db.rollback()
                    result.errors.append(f"Invoice {inv.invoice_number}: {e!s}")
                    logger.exception("Error syncing invoice %s", inv.id)
                    progress.record_failure(row_updated_at, inv.id)
            # Advance the cursor only after the pull completed without an API
            # error. The upstream feed is ordered by updated_at,id; the stored
            # id lets the next inclusive timestamp pull skip only the consumed
            # same-timestamp prefix.
            if use_watermark:
                progress.conclude(self._advance_invoice_watermark_position)
            self.db.flush()
            suffix = (
                f"; invoice work limit ({batch_size}) reached"
                if limit_reached and batch_size
                else ""
            )
            result.message = (
                f"Synced {result.created} new, {result.updated} updated, "
                f"{result.skipped} skipped invoices{suffix}"
            )
        except DotmacSubError as e:
            result.success = False
            result.message = f"dotmac_sub API error: {e.message}"
            result.errors.append(result.message)
            logger.error(result.message)
        return result

    def _sync_single_invoice(
        self,
        inv: InvoiceRecord,
        created_by_user_id: UUID | None,
        result: SyncResult,
        skip_unchanged: bool,
    ) -> None:
        external_id = inv.id

        # E4 money boundary FIRST — before the unchanged/status branches —
        # so every consumed monetary fact is validated on EVERY sync pass
        # (rejects missing currency, excess minor-unit precision,
        # unprovisioned currency). A legacy row whose invalid money never
        # changes must fail its row here, not ride the unchanged-skip
        # forever. A rejection fails this row's savepoint, never the run.
        inv.boundary_money()

        data_hash = self._compute_hash(_invoice_hash_payload(inv))
        if skip_unchanged and not self._has_changed(
            EntityType.INVOICE, external_id, data_hash
        ):
            result.skipped += 1
            return

        if inv.is_proforma or (inv.status or "").lower() == "draft":
            result.skipped += 1
            return

        local_id = self._get_synced_entity(EntityType.INVOICE, external_id)
        existing: Invoice | None = None
        if local_id and local_id != _PRE_CUTOFF_SENTINEL:
            existing = self.db.get(Invoice, local_id)
        if not existing:
            existing = self.db.scalar(
                select(Invoice).where(
                    Invoice.organization_id == self.organization_id,
                    Invoice.dotmac_sub_id == external_id,
                )
            )

        customer_id = self._get_customer_for_account(inv.account_id, inv.account)
        if not customer_id:
            result.skipped += 1
            result.errors.append(
                f"Invoice {inv.invoice_number}: account {inv.account_id} "
                "not resolved to a customer"
            )
            return

        invoice_date = inv.issued_at.date() if inv.issued_at else date.today()
        due_date = inv.due_at.date() if inv.due_at else invoice_date

        if invoice_date < DOTMAC_SUB_SYNC_MIN_DATE:
            self._record_sync(
                EntityType.INVOICE,
                external_id,
                _PRE_CUTOFF_SENTINEL,
                external_updated_at=inv.updated_at,
            )
            result.skipped += 1
            return

        amount_paid = inv.total - inv.balance_due
        status = self._map_invoice_status(inv.status, inv.balance_due)
        subtotal = inv.subtotal
        tax_amount = inv.tax_total
        # Convert the receivable to functional currency (was booked at face value,
        # while payments convert — so multi-currency AR never netted to zero).
        exch_rate, functional_amount = self._functional_amount(
            inv.total, inv.currency, invoice_date
        )

        if existing:
            if existing.journal_entry_id is not None and (
                status.name == "VOID"
                or self._posted_invoice_accounting_changed(
                    existing,
                    inv,
                    invoice_date=invoice_date,
                    currency_code=inv.currency,
                    exchange_rate=exch_rate,
                    functional_amount=functional_amount,
                    is_credit_note=False,
                )
            ):
                if not self._reverse_posted_invoice_gl(
                    existing,
                    created_by_user_id,
                    reason=(f"dotmac_sub invoice {inv.status or 'changed'} ({inv.id})"),
                ):
                    raise ValueError(
                        "ERP could not reverse the existing invoice journal; "
                        "the source correction was not applied"
                    )
            existing.customer_id = customer_id
            existing.invoice_date = invoice_date
            existing.due_date = due_date
            existing.currency_code = inv.currency
            existing.subtotal = subtotal
            existing.tax_amount = tax_amount
            existing.total_amount = inv.total
            existing.functional_currency_amount = functional_amount
            existing.exchange_rate = exch_rate
            existing.amount_paid = amount_paid
            existing.status = status
            existing.notes = inv.memo
            existing.dotmac_sub_id = inv.id
            existing.dotmac_sub_number = inv.invoice_number
            existing.last_synced_at = datetime.now(UTC)
            self._replace_lines(existing.invoice_id, inv, is_credit_note=False)
            self._ensure_synced_invoice_posted(existing, created_by_user_id)
            result.updated += 1
            self._record_sync(
                EntityType.INVOICE,
                external_id,
                existing.invoice_id,
                data_hash,
                external_updated_at=inv.updated_at,
            )
            return

        invoice = Invoice(
            organization_id=self.organization_id,
            customer_id=customer_id,
            invoice_number=self._generate_invoice_number(invoice_date),
            invoice_type=InvoiceType.STANDARD,
            invoice_date=invoice_date,
            due_date=due_date,
            currency_code=inv.currency,
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=inv.total,
            amount_paid=amount_paid,
            functional_currency_amount=functional_amount,
            exchange_rate=exch_rate,
            status=status,
            ar_control_account_id=self.ar_control_account_id,
            source_document_type="dotmac_sub_invoice",
            correlation_id=f"dotmac-sub-inv-{inv.id}",
            notes=inv.memo,
            internal_notes=f"Imported from dotmac_sub. Original ID: {inv.id}",
            created_by_user_id=created_by_user_id or SYSTEM_USER_ID,
            dotmac_sub_id=inv.id,
            dotmac_sub_number=inv.invoice_number,
            last_synced_at=datetime.now(UTC),
        )
        self.db.add(invoice)
        self.db.flush()
        self._create_lines(invoice.invoice_id, inv, is_credit_note=False)
        self._ensure_synced_invoice_posted(invoice, created_by_user_id)
        result.created += 1
        self._record_sync(
            EntityType.INVOICE,
            external_id,
            invoice.invoice_id,
            data_hash,
            external_updated_at=inv.updated_at,
        )

    def _create_lines(
        self,
        invoice_id: UUID,
        doc: InvoiceRecord | CreditNoteRecord,
        *,
        is_credit_note: bool,
    ) -> None:
        if not self.default_revenue_account_id:
            raise ValueError("ERP default revenue account is required for Sub invoices")
        # InvoiceLineRecord and CreditNoteLineRecord are structurally identical;
        # type as Any so the union of the two list types doesn't collapse to
        # ``list[object]`` under mypy invariance.
        lines: list[Any] = list(doc.lines)
        label = "Credit Note" if is_credit_note else "Invoice"
        number = getattr(doc, "credit_number", None) or getattr(
            doc, "invoice_number", ""
        )

        sign = Decimal("-1") if is_credit_note else Decimal("1")
        projected = self._project_source_lines(doc, is_credit_note=is_credit_note)

        if lines:
            for seq, (item, line_subtotal, line_tax, tax_code) in enumerate(
                projected, 1
            ):
                self._add_line(
                    invoice_id,
                    seq,
                    item.description or f"dotmac_sub {label} {number} - line {seq}",
                    item.quantity,
                    item.unit_price,
                    sign * line_subtotal,
                    sign * line_tax,
                    tax_code,
                )
        else:
            if doc.tax_total != Decimal("0"):
                raise ValueError(
                    f"Sub {label} {number} has tax but no line-level tax contract"
                )
            self._add_line(
                invoice_id,
                1,
                f"dotmac_sub {label} {number}",
                Decimal("1"),
                sign * doc.subtotal,
                sign * doc.subtotal,
                Decimal("0"),
                None,
            )

    def _project_source_lines(
        self,
        doc: InvoiceRecord | CreditNoteRecord,
        *,
        is_credit_note: bool,
    ) -> list[tuple[Any, Decimal, Decimal, Any]]:
        """Validate Sub's line contract and resolve ERP-owned posting codes."""
        lines: list[Any] = list(doc.lines)
        label = "Credit Note" if is_credit_note else "Invoice"
        number = getattr(doc, "credit_number", None) or getattr(
            doc, "invoice_number", ""
        )
        effective_date = doc.issued_at.date() if doc.issued_at else date.today()
        currency_code = doc.currency
        projected: list[tuple[Any, Decimal, Decimal, Any]] = []
        for item in lines:
            line_subtotal, line_tax, tax_code = self._source_line_amounts(
                item, currency_code=currency_code, effective_date=effective_date
            )
            projected.append((item, line_subtotal, line_tax, tax_code))

        def _round(value: Decimal) -> Decimal:
            # Centralized E4 rounding decision (currency minor units, half-up).
            # ERP-DERIVED aggregates only — source header FACTS are compared
            # as transmitted, never rounded (a 10.005 fact must fail the row
            # via the boundary, not become 10.01 here).
            return round_to_minor_units(value, currency_code)

        projected_subtotal = sum((item[1] for item in projected), Decimal("0"))
        projected_tax = sum((item[2] for item in projected), Decimal("0"))
        line_mismatch = bool(lines) and (
            _round(projected_subtotal) != doc.subtotal
            or _round(projected_tax) != doc.tax_total
        )
        if line_mismatch or doc.subtotal + doc.tax_total != doc.total:
            raise ValueError(
                f"Sub {label} {number} tax lines do not reconcile to the "
                f"source header: lines={projected_subtotal}+{projected_tax}, "
                f"header={doc.subtotal}+{doc.tax_total}={doc.total}"
            )
        return projected

    def _source_line_amounts(
        self, item: Any, *, currency_code: str, effective_date: date
    ) -> tuple[Decimal, Decimal, Any]:
        """Mirror Sub's line tax facts; ERP only resolves the posting TaxCode.

        A SUPPLIED line amount is a source money FACT: it is validated exact
        via ``to_boundary_money`` (excess minor-unit precision is REJECTED,
        never rounded — 10.005 NGN fails the row instead of becoming 10.01).
        Only when Sub omits the line amount does ERP derive
        quantity x unit_price itself, and only that ERP-derived value (and
        the derived tax splits below) may round through the E4 adapter's
        centralized minor-unit rounding (half-up).
        """
        if item.amount is not None:
            amount = to_boundary_money(
                item.amount,
                currency_code,
                field=f"Sub line {item.id} amount",
            ).amount
        else:
            amount = round_to_minor_units(
                item.quantity * item.unit_price, currency_code
            )
        # Admitted as a typed TaxApplication member at parse time (closed set),
        # so nothing here has to re-normalize or re-validate wire text.
        application: TaxApplication = item.tax_application
        if application is TaxApplication.EXEMPT or not item.tax_rate_id:
            return amount, Decimal("0"), None

        source_rate = self._get_source_tax_rate(str(item.tax_rate_id))
        ratio = source_rate.rate / Decimal("100")
        if ratio <= Decimal("0"):
            return amount, Decimal("0"), None
        if application is TaxApplication.INCLUSIVE:
            tax_amount = round_to_minor_units(
                amount - (amount / (Decimal("1") + ratio)), currency_code
            )
            line_subtotal = amount - tax_amount
        else:
            line_subtotal = amount
            tax_amount = round_to_minor_units(amount * ratio, currency_code)
        tax_code = self._resolve_source_sales_tax_code(
            source_tax_rate_id=str(item.tax_rate_id),
            tax_application=application.value,
            effective_date=effective_date,
        )
        return line_subtotal, tax_amount, tax_code

    def _add_line(
        self,
        invoice_id: UUID,
        seq: int,
        description: str,
        quantity: Decimal,
        unit_price: Decimal,
        line_amount: Decimal,
        tax_amount: Decimal,
        tc: Any,
    ) -> None:
        line = InvoiceLine(
            invoice_id=invoice_id,
            line_number=seq,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            discount_percentage=Decimal("0"),
            discount_amount=Decimal("0"),
            line_amount=line_amount,
            tax_amount=tax_amount,
            tax_code_id=tc.tax_code_id if tc else None,
            revenue_account_id=self.default_revenue_account_id,
        )
        self.db.add(line)
        self.db.flush()
        if tc is not None and tax_amount != Decimal("0"):
            self.db.add(
                InvoiceLineTax(
                    line_id=line.line_id,
                    tax_code_id=tc.tax_code_id,
                    base_amount=line_amount,
                    tax_rate=tc.tax_rate,
                    tax_amount=tax_amount,
                    is_inclusive=tc.is_inclusive,
                    sequence=1,
                )
            )

    def _posted_invoice_accounting_changed(
        self,
        invoice: Invoice,
        doc: InvoiceRecord | CreditNoteRecord,
        *,
        invoice_date: date,
        currency_code: str,
        exchange_rate: Decimal,
        functional_amount: Decimal,
        is_credit_note: bool,
    ) -> bool:
        """Compare only fields consumed by ERP's invoice posting adapter."""
        sign = Decimal("-1") if is_credit_note else Decimal("1")
        projected = self._project_source_lines(doc, is_credit_note=is_credit_note)
        expected = [
            (
                sequence,
                sign * subtotal,
                sign * tax,
                tax_code.tax_code_id if tax_code else None,
                self.default_revenue_account_id,
            )
            for sequence, (_item, subtotal, tax, tax_code) in enumerate(projected, 1)
        ]
        if not expected:
            expected = [
                (
                    1,
                    sign * doc.subtotal,
                    Decimal("0"),
                    None,
                    self.default_revenue_account_id,
                )
            ]
        current = [
            (
                line.line_number,
                line.line_amount,
                line.tax_amount or Decimal("0"),
                line.tax_code_id,
                line.revenue_account_id,
            )
            for line in self.db.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice.invoice_id)
                .order_by(InvoiceLine.line_number)
            ).all()
        ]
        return (
            invoice.invoice_date != invoice_date
            or invoice.currency_code != currency_code
            or invoice.subtotal != sign * doc.subtotal
            or invoice.tax_amount != sign * doc.tax_total
            or invoice.total_amount != sign * doc.total
            or invoice.exchange_rate != exchange_rate
            or invoice.functional_currency_amount != functional_amount
            or current != expected
        )

    def _reverse_posted_invoice_gl(
        self,
        invoice: Invoice,
        created_by_user_id: UUID | None,
        *,
        reason: str,
    ) -> bool:
        from app.services.finance.gl.reversal import ReversalService

        journal_id = invoice.journal_entry_id
        if journal_id is None:
            return False
        user_id = created_by_user_id or invoice.created_by_user_id or SYSTEM_USER_ID
        try:
            reversal = ReversalService.create_reversal(
                db=self.db,
                organization_id=self.organization_id,
                original_journal_id=journal_id,
                reversal_date=date.today(),
                created_by_user_id=user_id,
                reason=reason,
                auto_post=True,
                idempotency_key=(
                    f"{self.organization_id}:AR:INV:{invoice.invoice_id}:"
                    f"sub-resync:{journal_id}"
                ),
            )
        except Exception:
            logger.exception("GL reversal errored for invoice %s", invoice.invoice_id)
            return False
        if not getattr(reversal, "success", False):
            return False
        invoice.journal_entry_id = None
        invoice.posting_batch_id = None
        invoice.posting_status = "NOT_POSTED"
        return True

    def _ensure_synced_invoice_posted(
        self, invoice: Invoice, created_by_user_id: UUID | None
    ) -> None:
        from app.models.finance.ar.invoice import InvoiceStatus
        from app.services.finance.ar.invoice import ARInvoiceService

        postable = {
            InvoiceStatus.POSTED,
            InvoiceStatus.PAID,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.OVERDUE,
        }
        if invoice.status not in postable or invoice.total_amount == Decimal("0"):
            return
        ARInvoiceService.ensure_gl_posted(
            self.db,
            invoice,
            posted_by_user_id=created_by_user_id,
        )
        if invoice.journal_entry_id is None:
            raise ValueError(
                "ERP failed to post the synced document to its canonical GL"
            )

    def post_unposted_invoices(
        self,
        created_by_user_id: UUID | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Repair legacy Sub documents that reached ERP before GL posting."""
        stats: dict[str, Any] = {"posted": 0, "errors": []}
        stmt = select(Invoice).where(
            Invoice.organization_id == self.organization_id,
            Invoice.source_document_type.in_(
                ["dotmac_sub_invoice", "dotmac_sub_credit_note"]
            ),
            Invoice.journal_entry_id.is_(None),
        )
        if limit is not None:
            stmt = stmt.limit(max(limit, 1))
        for invoice in self.db.scalars(stmt).all():
            savepoint = self.db.begin_nested()
            try:
                self._ensure_synced_invoice_posted(invoice, created_by_user_id)
                if invoice.journal_entry_id is not None:
                    stats["posted"] += 1
                savepoint.commit()
            except Exception as exc:  # noqa: BLE001
                savepoint.rollback()
                logger.exception("Failed to GL-post invoice %s", invoice.invoice_id)
                stats["errors"].append(f"Invoice {invoice.invoice_number}: {exc}")
        return stats

    def _replace_lines(
        self,
        invoice_id: UUID,
        doc: InvoiceRecord | CreditNoteRecord,
        *,
        is_credit_note: bool,
    ) -> None:
        self.db.execute(
            delete(InvoiceLineTax).where(
                InvoiceLineTax.line_id.in_(
                    select(InvoiceLine.line_id).where(
                        InvoiceLine.invoice_id == invoice_id
                    )
                )
            )
        )
        self.db.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id))
        self._create_lines(invoice_id, doc, is_credit_note=is_credit_note)
