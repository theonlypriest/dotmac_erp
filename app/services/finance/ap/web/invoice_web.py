"""
AP Invoice Web Service - Invoice-related web view methods.

Provides view-focused data and operations for AP invoice web routes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.finance.ap.ap_payment_allocation import APPaymentAllocation
from app.models.finance.ap.purchase_order import PurchaseOrder
from app.models.finance.ap.purchase_order_line import PurchaseOrderLine
from app.models.finance.ap.invoice_inventory_receipt_approval import (
    InvoiceInventoryReceiptApproval,
)
from app.models.finance.ap.supplier import Supplier
from app.models.finance.ap.supplier_invoice import (
    SupplierInvoice,
    SupplierInvoiceStatus,
    SupplierInvoiceType,
)
from app.models.finance.ap.supplier_invoice_line import SupplierInvoiceLine
from app.models.finance.ap.supplier_invoice_line_tax import SupplierInvoiceLineTax
from app.models.finance.ap.supplier_payment import SupplierPayment
from app.models.finance.common.attachment import AttachmentCategory
from app.models.finance.gl.account import Account
from app.models.finance.gl.account_category import IFRSCategory
from app.models.finance.tax.tax_code import TaxCode, TaxType
from app.models.notification import EntityType, NotificationChannel, NotificationType
from app.models.person import Person
from app.services.common import coerce_uuid
from app.services.common_filters import build_active_filters
from app.services.finance.ap.supplier import supplier_service
from app.services.finance.ap.supplier_invoice import (
    SupplierInvoiceInput,
    supplier_invoice_service,
)
from app.services.finance.ap.web.base import (
    InvoiceStats,
    format_currency,
    format_date,
    format_file_size,
    get_accounts,
    invoice_detail_view,
    invoice_line_view,
    invoice_status_label,
    logger,
    recent_activity_view,
    supplier_display_name,
    supplier_form_view,
    supplier_option_view,
)
from app.services.finance.common.attachment import AttachmentInput, attachment_service
from app.services.finance.common.sorting import apply_sort
from app.services.finance.platform.currency_context import get_currency_context
from app.services.notification import notification_service
from app.templates import templates
from app.web.deps import WebAuthContext, base_context


class InvoiceWebService:
    """Web service methods for AP invoices."""

    @staticmethod
    def build_invoice_input(
        db: Session, data: dict, organization_id: UUID
    ) -> SupplierInvoiceInput:
        """Build SupplierInvoiceInput from form data."""
        logger.debug("build_invoice_input: building input from form data")
        payload = dict(data)
        return supplier_invoice_service.build_input_from_payload(
            db=db,
            organization_id=organization_id,
            payload=payload,
        )

    @staticmethod
    def list_invoices_context(
        db: Session,
        organization_id: str,
        search: str | None,
        supplier_id: str | None,
        status: str | None,
        start_date: str | None,
        end_date: str | None,
        page: int,
        limit: int = 50,
        sort: str | None = None,
        sort_dir: str | None = None,
        invoice_type: SupplierInvoiceType | None = None,
    ) -> dict:
        """Get context for invoice listing page."""
        logger.debug(
            "list_invoices_context: org=%s search=%r supplier_id=%s status=%s page=%d",
            organization_id,
            search,
            supplier_id,
            status,
            page,
        )
        offset = (page - 1) * limit
        today = date.today()
        org_id = coerce_uuid(organization_id)

        from app.services.finance.ap.invoice_query import build_invoice_query

        base_stmt = build_invoice_query(
            db=db,
            organization_id=organization_id,
            search=search,
            supplier_id=supplier_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )

        if invoice_type is not None:
            base_stmt = base_stmt.where(SupplierInvoice.invoice_type == invoice_type)

        total_count = (
            db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0
        )

        invoice_sort_map = {
            "created_at": SupplierInvoice.created_at,
            "invoice_date": SupplierInvoice.invoice_date,
            "invoice_number": SupplierInvoice.invoice_number,
            "supplier_name": Supplier.legal_name,
            "total_amount": SupplierInvoice.total_amount,
            "status": SupplierInvoice.status,
        }
        sorted_stmt = apply_sort(
            base_stmt,
            sort,
            sort_dir,
            invoice_sort_map,
            default=SupplierInvoice.created_at.desc(),
        )

        # Add Supplier to the result columns so we get (SupplierInvoice, Supplier) tuples
        invoices = db.execute(
            sorted_stmt.add_columns(Supplier).limit(limit).offset(offset)
        ).all()

        open_statuses = [
            SupplierInvoiceStatus.POSTED,
            SupplierInvoiceStatus.PARTIALLY_PAID,
        ]
        # Build stats using the same base filters as a subquery source
        outstanding_base = base_stmt.where(SupplierInvoice.status.in_(open_statuses))
        outstanding_subquery = outstanding_base.subquery()

        balance_expr = func.coalesce(func.sum(outstanding_subquery.c.balance_due), 0)

        total_outstanding = db.scalar(
            select(balance_expr).select_from(outstanding_subquery)
        ) or Decimal("0")

        past_due = db.scalar(
            select(balance_expr)
            .select_from(outstanding_subquery)
            .where(outstanding_subquery.c.due_date < today)
        ) or Decimal("0")

        due_this_week_end = today + timedelta(days=7)
        due_this_week = db.scalar(
            select(balance_expr)
            .select_from(outstanding_subquery)
            .where(
                outstanding_subquery.c.due_date >= today,
                outstanding_subquery.c.due_date <= due_this_week_end,
            )
        ) or Decimal("0")

        pending_count = (
            db.scalar(
                select(func.count()).select_from(
                    base_stmt.where(
                        SupplierInvoice.status == SupplierInvoiceStatus.PENDING_APPROVAL
                    ).subquery()
                )
            )
            or 0
        )

        invoices_view = []
        for invoice, supplier in invoices:
            balance = invoice.balance_due
            invoices_view.append(
                {
                    "invoice_id": invoice.invoice_id,
                    "invoice_number": invoice.invoice_number,
                    "supplier_name": supplier_display_name(supplier),
                    "created_at": format_date(invoice.created_at),
                    "invoice_date": format_date(invoice.invoice_date),
                    "total_amount": format_currency(
                        invoice.total_amount, invoice.currency_code
                    ),
                    "purpose": invoice.purpose or "",
                    "tax_amount": format_currency(
                        invoice.tax_amount, invoice.currency_code
                    ),
                    "balance": format_currency(balance, invoice.currency_code),
                    "status": invoice_status_label(invoice.status),
                    "is_overdue": (
                        invoice.due_date < today
                        and invoice.status
                        not in {SupplierInvoiceStatus.PAID, SupplierInvoiceStatus.VOID}
                    ),
                }
            )

        selected_supplier = None
        if supplier_id:
            try:
                selected_supplier = supplier_option_view(
                    supplier_service.get(db, org_id, supplier_id)
                )
            except (ValueError, LookupError):
                selected_supplier = None

        total_pages = max(1, (total_count + limit - 1) // limit)

        stats = InvoiceStats(
            total_outstanding=format_currency(total_outstanding) or "$0.00",
            past_due=format_currency(past_due) or "$0.00",
            due_this_week=format_currency(due_this_week) or "$0.00",
            pending_count=pending_count,
        )

        logger.debug("list_invoices_context: found %d invoices", total_count)

        active_filters = build_active_filters(
            params={
                "status": status,
                "supplier_id": supplier_id,
                "start_date": start_date,
                "end_date": end_date,
            },
            labels={"start_date": "From", "end_date": "To"},
            options={
                "supplier_id": (
                    {
                        str(selected_supplier["supplier_id"]): selected_supplier[
                            "supplier_name"
                        ]
                    }
                    if selected_supplier
                    else {}
                )
            },
        )

        return {
            "invoices": invoices_view,
            "selected_supplier": selected_supplier,
            "stats": stats.__dict__,
            "search": search,
            "supplier_id": supplier_id,
            "status": status,
            "start_date": start_date,
            "end_date": end_date,
            "sort": sort,
            "sort_dir": sort_dir,
            "page": page,
            "limit": limit,
            "offset": offset,
            "total_count": total_count,
            "total_pages": total_pages,
            "active_filters": active_filters,
        }

    @staticmethod
    def invoice_form_context(
        db: Session,
        organization_id: str,
        supplier_id: str | None = None,
        po_id: str | None = None,
    ) -> dict:
        """Get context for invoice create form."""
        from app.models.finance.tax.tax_code import TaxCode
        from app.models.fixed_assets.asset_category import AssetCategory
        from app.models.inventory.item import Item
        from app.models.inventory.warehouse import Warehouse

        logger.debug(
            "invoice_form_context: org=%s supplier_id=%s po_id=%s",
            organization_id,
            supplier_id,
            po_id,
        )
        org_id = coerce_uuid(organization_id)
        from app.services.common import PaginationParams
        from app.services.fleet.vehicle_service import VehicleService

        fleet_vehicles = (
            VehicleService(db, org_id)
            .list_vehicles(params=PaginationParams(offset=0, limit=500))
            .items
        )
        suppliers_list = [
            supplier_option_view(supplier)
            for supplier in supplier_service.list(
                db,
                organization_id=org_id,
                is_active=True,
                limit=200,
            )
        ]

        expense_accounts = get_accounts(db, org_id, IFRSCategory.EXPENSES)

        # Get tax codes that apply to purchases
        tax_codes = [
            {
                "tax_code_id": str(tax.tax_code_id),
                "tax_code": tax.tax_code,
                "tax_name": tax.tax_name,
                "tax_rate": float(tax.tax_rate),
                "rate_display": float((tax.tax_rate * 100).quantize(Decimal("0.01")))
                if tax.tax_rate < 1
                else float(tax.tax_rate),
                "is_inclusive": tax.is_inclusive,
                "is_compound": tax.is_compound,
                "is_recoverable": getattr(tax, "is_recoverable", True),
            }
            for tax in db.scalars(
                select(TaxCode).where(
                    TaxCode.organization_id == org_id,
                    TaxCode.is_active == True,
                    TaxCode.applies_to_purchases == True,
                    TaxCode.tax_type.notin_([TaxType.WITHHOLDING, TaxType.STAMP_DUTY]),
                )
            ).all()
        ]

        # Pre-populated data from PO
        selected_supplier = None
        selected_po = None
        po_lines = []

        if supplier_id:
            supplier_uuid = coerce_uuid(supplier_id)
            supplier = db.get(Supplier, supplier_uuid)
            if supplier and supplier.organization_id == org_id:
                selected_supplier = supplier_option_view(supplier)

        if po_id:
            po_uuid = coerce_uuid(po_id)
            po = db.get(PurchaseOrder, po_uuid)
            if po and po.organization_id == org_id:
                supplier = db.get(Supplier, po.supplier_id)
                selected_po = {
                    "po_id": str(po.po_id),
                    "po_number": po.po_number,
                    "supplier_id": str(po.supplier_id),
                    "supplier_name": supplier_display_name(supplier)
                    if supplier
                    else "",
                    "currency_code": po.currency_code,
                    "total_amount": float(po.total_amount) if po.total_amount else 0,
                }
                # If supplier not already set, use PO's supplier
                if not selected_supplier and supplier:
                    selected_supplier = supplier_option_view(supplier)

                # Get PO lines for pre-populating invoice lines
                lines = db.scalars(
                    select(PurchaseOrderLine)
                    .where(PurchaseOrderLine.po_id == po_uuid)
                    .order_by(PurchaseOrderLine.line_number)
                ).all()
                for line in lines:
                    po_lines.append(
                        {
                            "line_id": str(line.line_id),
                            "line_number": line.line_number,
                            "item_id": str(line.item_id) if line.item_id else "",
                            "description": line.description,
                            "quantity": float(line.quantity_ordered),
                            "unit_price": float(line.unit_price),
                            "amount": float(line.quantity_ordered * line.unit_price),
                            "expense_account_id": str(line.expense_account_id)
                            if line.expense_account_id
                            else "",
                        }
                    )

        # Get inventory items for AP -> INV integration
        items_list = [
            {
                "item_id": str(item.item_id),
                "item_code": item.item_code,
                "item_name": item.item_name,
                "unit_price": float(item.last_purchase_cost)
                if item.last_purchase_cost
                else 0,
                "uom": item.base_uom,
                "track_inventory": bool(item.track_inventory),
                "track_lots": bool(item.track_lots),
                "track_serial_numbers": bool(item.track_serial_numbers),
            }
            for item in db.scalars(
                select(Item)
                .where(
                    Item.organization_id == org_id,
                    Item.is_active.is_(True),
                    Item.is_purchaseable.is_(True),
                )
                .order_by(Item.item_code)
            ).all()
        ]

        warehouses_list = [
            {
                "warehouse_id": str(warehouse.warehouse_id),
                "warehouse_code": warehouse.warehouse_code,
                "warehouse_name": warehouse.warehouse_name,
            }
            for warehouse in db.scalars(
                select(Warehouse)
                .where(
                    Warehouse.organization_id == org_id,
                    Warehouse.is_active.is_(True),
                    Warehouse.is_receiving.is_(True),
                )
                .order_by(Warehouse.warehouse_code)
            ).all()
        ]

        # Get asset accounts for capitalization (AP -> FA integration)
        asset_accounts = get_accounts(db, org_id, IFRSCategory.ASSETS)

        # Get asset categories for capitalization
        asset_categories = [
            {
                "category_id": str(cat.category_id),
                "category_code": cat.category_code,
                "category_name": cat.category_name,
                "threshold": float(cat.capitalization_threshold),
            }
            for cat in db.scalars(
                select(AssetCategory)
                .where(
                    AssetCategory.organization_id == org_id,
                    AssetCategory.is_active == True,
                )
                .order_by(AssetCategory.category_code)
            ).all()
        ]

        # Get WHT codes for withholding tax selection
        wht_codes = [
            {
                "tax_code_id": str(wht.tax_code_id),
                "tax_code": wht.tax_code,
                "tax_name": wht.tax_name,
                "tax_rate": float(wht.tax_rate),
                "rate_display": float((wht.tax_rate * 100).quantize(Decimal("0.01")))
                if wht.tax_rate < 1
                else float(wht.tax_rate),
            }
            for wht in db.scalars(
                select(TaxCode).where(
                    TaxCode.organization_id == org_id,
                    TaxCode.is_active == True,
                    TaxCode.tax_type == TaxType.WITHHOLDING,
                )
            ).all()
        ]

        context = {
            "suppliers_list": suppliers_list,
            "expense_accounts": expense_accounts,
            "asset_accounts": asset_accounts,
            "items_list": items_list,
            "warehouses_list": warehouses_list,
            "tax_codes": tax_codes,
            "wht_codes": wht_codes,
            "asset_categories": asset_categories,
            "fleet_vehicles": fleet_vehicles,
            "organization_id": organization_id,
            "selected_supplier": selected_supplier,
            "selected_po": selected_po,
            "po_lines": po_lines,
        }
        context.update(get_currency_context(db, organization_id))
        return context

    @staticmethod
    def invoice_detail_context(
        db: Session,
        organization_id: str,
        invoice_id: str,
    ) -> dict:
        """Get context for invoice detail page."""
        logger.debug(
            "invoice_detail_context: org=%s invoice_id=%s", organization_id, invoice_id
        )
        org_id = coerce_uuid(organization_id)
        invoice = None
        try:
            invoice = supplier_invoice_service.get(db, invoice_id)
        except (ValueError, LookupError) as e:
            logger.warning("Failed to load entity: %s", e)
            invoice = None

        if not invoice or invoice.organization_id != org_id:
            return {"invoice": None, "supplier": None, "lines": []}

        supplier = None
        try:
            supplier = supplier_service.get(db, org_id, str(invoice.supplier_id))
        except (ValueError, LookupError) as e:
            logger.warning("Failed to load entity: %s", e)
            supplier = None

        lines = supplier_invoice_service.get_invoice_lines(
            db,
            organization_id=org_id,
            invoice_id=invoice.invoice_id,
        )
        lines_view = [invoice_line_view(line, invoice.currency_code) for line in lines]
        account_ids = {
            account_id
            for line in lines
            for account_id in (line.asset_account_id, line.expense_account_id)
            if account_id
        }
        account_map = {}
        if account_ids:
            accounts = db.scalars(
                select(Account).where(
                    Account.organization_id == org_id,
                    Account.account_id.in_(account_ids),
                )
            ).all()
            account_map = {account.account_id: account for account in accounts}

        # Enrich lines with tax metadata and VAT-per-line display values.
        primary_tax_ids = {line.tax_code_id for line in lines if line.tax_code_id}
        tax_map: dict[UUID, TaxCode] = {}
        if primary_tax_ids:
            tax_codes = db.scalars(
                select(TaxCode).where(
                    TaxCode.organization_id == org_id,
                    TaxCode.tax_code_id.in_(primary_tax_ids),
                )
            ).all()
            tax_map = {tax.tax_code_id: tax for tax in tax_codes}

        vat_by_line: dict[UUID, Decimal] = {}
        inclusive_tax_by_line: dict[UUID, Decimal] = {}
        vat_labels_by_line: dict[UUID, set[str]] = {}
        line_ids = [line.line_id for line in lines]
        if line_ids:
            receipt_approvals = db.scalars(
                select(InvoiceInventoryReceiptApproval).where(
                    InvoiceInventoryReceiptApproval.organization_id == org_id,
                    InvoiceInventoryReceiptApproval.supplier_invoice_line_id.in_(
                        line_ids
                    ),
                )
            ).all()
            receipt_approval_by_line = {
                approval.supplier_invoice_line_id: approval
                for approval in receipt_approvals
            }
            line_taxes = db.execute(
                select(SupplierInvoiceLineTax, TaxCode)
                .join(
                    TaxCode, TaxCode.tax_code_id == SupplierInvoiceLineTax.tax_code_id
                )
                .where(
                    SupplierInvoiceLineTax.line_id.in_(line_ids),
                    TaxCode.organization_id == org_id,
                )
            ).all()
            for line_tax, tax_code in line_taxes:
                tax_amount = line_tax.tax_amount or Decimal("0")
                tax_rate = line_tax.tax_rate
                if not isinstance(tax_rate, Decimal):
                    try:
                        tax_rate = Decimal(str(tax_rate))
                    except Exception:
                        tax_rate = Decimal("0")

                vat_by_line[line_tax.line_id] = (
                    vat_by_line.get(line_tax.line_id, Decimal("0")) + tax_amount
                )
                if line_tax.is_inclusive:
                    inclusive_tax_by_line[line_tax.line_id] = (
                        inclusive_tax_by_line.get(line_tax.line_id, Decimal("0"))
                        + tax_amount
                    )
                if tax_code.tax_type in {TaxType.VAT, TaxType.GST}:
                    rate_label = (
                        f"{(tax_rate * 100).quantize(Decimal('0.01'))}%"
                        if tax_rate < 1
                        else f"{tax_rate}%"
                    )
                    incl_suffix = " Incl." if line_tax.is_inclusive else ""
                    vat_labels_by_line.setdefault(line_tax.line_id, set()).add(
                        f"{tax_code.tax_code} {rate_label}{incl_suffix}"
                    )

        for idx, line in enumerate(lines):
            line_view = lines_view[idx]
            receipt_approval = (
                receipt_approval_by_line.get(line.line_id) if line_ids else None
            )
            if receipt_approval:
                line_view["receipt_approval"] = {
                    "approval_id": str(receipt_approval.approval_id),
                    "status": receipt_approval.status.value,
                    "url": f"/inventory/receipt-approvals/{receipt_approval.approval_id}",
                }
            else:
                line_view["receipt_approval"] = None
            line_view["auto_receipt_transaction_id"] = (
                str(line.auto_receipt_transaction_id)
                if getattr(line, "auto_receipt_transaction_id", None)
                else ""
            )
            account_id = (
                line.asset_account_id
                if getattr(line, "capitalize_flag", False) and line.asset_account_id
                else line.expense_account_id or line.asset_account_id
            )
            account = account_map.get(account_id) if account_id else None
            if account:
                line_view["account"] = {
                    "account_id": account.account_id,
                    "account_code": account.account_code,
                    "account_name": account.account_name,
                    "account_url": f"/finance/gl/accounts/{account.account_id}",
                }
            else:
                line_view["account"] = None
            if getattr(line, "capitalize_flag", False) and line.asset_account_id:
                line_view["accounting_treatment"] = "Capitalize"
            elif line.asset_account_id and not line.expense_account_id:
                line_view["accounting_treatment"] = "Asset"
            else:
                line_view["accounting_treatment"] = "Expense"
            tax = tax_map.get(line.tax_code_id) if line.tax_code_id else None
            if tax:
                line_view["tax_code"] = tax.tax_code
                line_view["tax_name"] = tax.tax_name
                line_view["tax_type"] = tax.tax_type.value

            inclusive_tax = inclusive_tax_by_line.get(line.line_id, Decimal("0"))
            if inclusive_tax > 0:
                display_amount = (
                    (line.line_amount or Decimal("0"))
                    + (line.tax_amount or Decimal("0"))
                    - inclusive_tax
                )
                line_view["display_line_amount_raw"] = float(display_amount)
                line_view["display_line_amount"] = format_currency(
                    display_amount, invoice.currency_code
                )
                line_view["gross_amount_raw"] = float(display_amount)
                line_view["gross_amount"] = format_currency(
                    display_amount, invoice.currency_code
                )

            vat_amount = vat_by_line.get(line.line_id, Decimal("0"))
            if vat_amount == 0 and tax and tax.tax_type in {TaxType.VAT, TaxType.GST}:
                vat_amount = line.tax_amount
                rate_label = (
                    f"{(tax.tax_rate * 100).quantize(Decimal('0.01'))}%"
                    if tax.tax_rate < 1
                    else f"{tax.tax_rate}%"
                )
                vat_labels_by_line.setdefault(line.line_id, set()).add(
                    f"{tax.tax_code} {rate_label}"
                )

            if vat_amount > 0:
                line_view["vat_amount_raw"] = float(vat_amount)
                line_view["vat_amount"] = format_currency(
                    vat_amount, invoice.currency_code
                )
                labels = vat_labels_by_line.get(line.line_id, set())
                line_view["vat_label"] = ", ".join(sorted(labels)) if labels else None
            else:
                line_view["vat_amount_raw"] = 0.0
                line_view["vat_amount"] = None
                line_view["vat_label"] = None

        # Get attachments
        attachments = attachment_service.list_for_entity(
            db,
            organization_id=org_id,
            entity_type="SUPPLIER_INVOICE",
            entity_id=invoice.invoice_id,
        )
        attachments_view = [
            {
                "attachment_id": str(att.attachment_id),
                "file_name": att.file_name,
                "file_size": att.file_size,
                "file_size_display": format_file_size(att.file_size),
                "content_type": att.content_type,
                "category": att.category.value,
                "description": att.description,
                "uploaded_at": att.uploaded_at,
                "download_url": f"/finance/ap/attachments/{att.attachment_id}/download",
            }
            for att in attachments
        ]

        logger.debug("invoice_detail_context: found %d lines", len(lines_view))
        recent_activity = recent_activity_view(
            db,
            org_id,
            table_schema="ap",
            table_name="supplier_invoice",
            record_id=str(invoice.invoice_id),
            limit=10,
        )

        invoice_view = invoice_detail_view(invoice, supplier)
        inclusive_vat_total = sum(inclusive_tax_by_line.values(), Decimal("0"))
        if inclusive_vat_total > 0:
            display_subtotal = (invoice.subtotal or Decimal("0")) + inclusive_vat_total
            display_tax_added = (
                invoice.tax_amount or Decimal("0")
            ) - inclusive_vat_total
            if display_tax_added < 0:
                display_tax_added = Decimal("0")
            invoice_view["display_subtotal_raw"] = float(display_subtotal)
            invoice_view["display_subtotal"] = format_currency(
                display_subtotal, invoice.currency_code
            )
            invoice_view["display_tax_amount_raw"] = float(display_tax_added)
            invoice_view["display_tax_amount"] = format_currency(
                display_tax_added, invoice.currency_code
            )
            invoice_view["display_tax_added_raw"] = float(display_tax_added)
            invoice_view["display_tax_added"] = format_currency(
                display_tax_added, invoice.currency_code
            )
            invoice_view["display_tax_included_raw"] = float(inclusive_vat_total)
            invoice_view["display_tax_included"] = format_currency(
                inclusive_vat_total, invoice.currency_code
            )

        payment_rows = db.execute(
            select(SupplierPayment, APPaymentAllocation)
            .join(
                APPaymentAllocation,
                APPaymentAllocation.payment_id == SupplierPayment.payment_id,
            )
            .where(
                APPaymentAllocation.invoice_id == invoice.invoice_id,
                SupplierPayment.organization_id == org_id,
            )
            .order_by(
                SupplierPayment.payment_date.desc(),
                SupplierPayment.payment_number.desc(),
            )
        ).all()
        payments_view = [
            {
                "payment_id": payment.payment_id,
                "payment_number": payment.payment_number,
                "payment_date": format_date(payment.payment_date, format="%d %b %Y"),
                "payment_method": payment.payment_method.value,
                "amount": format_currency(
                    allocation.allocated_amount,
                    payment.currency_code,
                ),
            }
            for payment, allocation in payment_rows
        ]

        # Fetch org TIN for display on invoice document
        org_tin: str | None = None
        try:
            from app.models.finance.core_org.organization import Organization

            org = db.get(Organization, org_id)
            if org:
                org_tin = org.tax_identification_number
        except (AttributeError, TypeError):
            pass  # org TIN lookup is best-effort

        return {
            "invoice": invoice_view,
            "supplier": supplier_form_view(supplier) if supplier else None,
            "lines": lines_view,
            "attachments": attachments_view,
            "payments": payments_view,
            "org_tin": org_tin,
            "recent_activity": recent_activity,
        }

    @staticmethod
    def delete_invoice(
        db: Session,
        organization_id: str,
        invoice_id: str,
    ) -> str | None:
        """Delete an invoice. Returns error message or None on success."""
        logger.debug(
            "delete_invoice: org=%s invoice_id=%s", organization_id, invoice_id
        )
        org_id = coerce_uuid(organization_id)
        inv_id = coerce_uuid(invoice_id)

        try:
            supplier_invoice_service.delete_invoice(db, org_id, inv_id)
            logger.info("delete_invoice: deleted invoice %s for org %s", inv_id, org_id)
            return None
        except HTTPException as exc:
            return exc.detail
        except Exception as e:
            logger.exception("delete_invoice: failed for org %s", org_id)
            return f"Failed to delete invoice: {str(e)}"

    # =====================================================================
    # HTTP Response Methods
    # =====================================================================

    def list_invoices_response(
        self,
        request: Request,
        auth: WebAuthContext,
        search: str | None,
        supplier_id: str | None,
        status: str | None,
        start_date: str | None,
        end_date: str | None,
        page: int,
        db: Session,
        limit: int = 50,
        sort: str | None = None,
        sort_dir: str | None = None,
    ) -> HTMLResponse:
        """Render invoice list page."""
        context = base_context(request, auth, "AP Invoices", "ap")
        context.update(
            self.list_invoices_context(
                db,
                str(auth.organization_id),
                search=search,
                supplier_id=supplier_id,
                status=status,
                start_date=start_date,
                end_date=end_date,
                page=page,
                limit=limit,
                sort=sort,
                sort_dir=sort_dir,
            )
        )
        return templates.TemplateResponse(request, "finance/ap/invoices.html", context)

    def invoice_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        supplier_id: str | None,
        po_id: str | None,
        db: Session,
        duplicate_from: str | None = None,
    ) -> HTMLResponse:
        """Render new invoice form, optionally pre-filled from a source invoice."""
        context = base_context(request, auth, "New AP Invoice", "ap")
        context.update(
            self.invoice_form_context(
                db,
                str(auth.organization_id),
                supplier_id=supplier_id,
                po_id=po_id,
            )
        )

        if duplicate_from:
            dup_ctx = self._duplicate_invoice_context(
                db, str(auth.organization_id), duplicate_from
            )
            if dup_ctx:
                context["duplicate_source"] = dup_ctx
                # Re-build form context with source supplier pre-selected
                if not supplier_id:
                    supplier_obj = db.get(Supplier, coerce_uuid(dup_ctx["supplier_id"]))
                    if supplier_obj:
                        context["selected_supplier"] = supplier_option_view(
                            supplier_obj
                        )

        return templates.TemplateResponse(
            request, "finance/ap/invoice_form.html", context
        )

    def list_credit_notes_response(
        self,
        request: Request,
        auth: WebAuthContext,
        search: str | None,
        supplier_id: str | None,
        status: str | None,
        start_date: str | None,
        end_date: str | None,
        page: int,
        db: Session,
        limit: int = 50,
        sort: str | None = None,
        sort_dir: str | None = None,
    ) -> HTMLResponse:
        """Render the supplier credit notes list (AP credit notes)."""
        context = base_context(request, auth, "Supplier Credit Notes", "ap")
        context.update(
            self.list_invoices_context(
                db,
                str(auth.organization_id),
                search=search,
                supplier_id=supplier_id,
                status=status,
                start_date=start_date,
                end_date=end_date,
                page=page,
                limit=limit,
                sort=sort,
                sort_dir=sort_dir,
                invoice_type=SupplierInvoiceType.CREDIT_NOTE,
            )
        )
        context.update(
            {
                "list_title": "Supplier Credit Notes",
                "new_url": "/finance/ap/credit-notes/new",
                "new_label": "New Credit Note",
                "export_url": "/finance/ap/invoices/export",
                "empty_title": "No supplier credit notes",
                "empty_desc": "Raise a supplier credit note to record a purchase return.",
            }
        )
        return templates.TemplateResponse(request, "finance/ap/invoices.html", context)

    def credit_note_new_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        supplier_id: str | None,
        db: Session,
    ) -> HTMLResponse:
        """Render the new supplier credit note form (reuses the invoice form)."""
        context = base_context(request, auth, "New Supplier Credit Note", "ap")
        context.update(
            self.invoice_form_context(
                db,
                str(auth.organization_id),
                supplier_id=supplier_id,
            )
        )
        context["is_credit_note"] = True
        context["default_invoice_type"] = SupplierInvoiceType.CREDIT_NOTE.value
        return templates.TemplateResponse(
            request, "finance/ap/invoice_form.html", context
        )

    @staticmethod
    def _duplicate_invoice_context(
        db: Session,
        organization_id: str,
        invoice_id: str,
    ) -> dict | None:
        """Build pre-fill context from an existing AP invoice for duplication.

        Returns a dict with supplier, line items, and metadata from the
        source invoice — but no invoice_id/invoice_number so the form
        creates a new invoice.
        """
        org_id = coerce_uuid(organization_id)
        inv_id = coerce_uuid(invoice_id)

        invoice = db.get(SupplierInvoice, inv_id)
        if not invoice or invoice.organization_id != org_id:
            return None

        lines = db.scalars(
            select(SupplierInvoiceLine)
            .where(SupplierInvoiceLine.invoice_id == inv_id)
            .order_by(SupplierInvoiceLine.line_number)
        ).all()

        return {
            "source_number": invoice.invoice_number,
            "supplier_id": str(invoice.supplier_id),
            "currency_code": invoice.currency_code or "",
            "supplier_invoice_number": invoice.supplier_invoice_number or "",
            "purpose": invoice.purpose or "",
            "comments": invoice.comments or "",
            "exchange_rate": str(invoice.exchange_rate)
            if invoice.exchange_rate
            else "1",
            "lines": [
                {
                    "item_id": str(line.item_id) if line.item_id else "",
                    "expense_account_id": str(line.expense_account_id)
                    if line.expense_account_id
                    else "",
                    "description": line.description or "",
                    "quantity": float(line.quantity)
                    if line.quantity is not None
                    else 0,
                    "unit_price": float(line.unit_price)
                    if line.unit_price is not None
                    else 0,
                    "tax_code_id": str(line.tax_code_id) if line.tax_code_id else "",
                    "tax_amount": float(line.tax_amount)
                    if line.tax_amount is not None
                    else 0,
                }
                for line in lines
            ],
        }

    def invoice_edit_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> HTMLResponse | RedirectResponse:
        """Render edit invoice form with existing invoice data."""
        org_id = coerce_uuid(auth.organization_id)
        inv_id = coerce_uuid(invoice_id)

        invoice = db.get(SupplierInvoice, inv_id)
        if not invoice or invoice.organization_id != org_id:
            return RedirectResponse(
                url="/finance/ap/invoices?error=Invoice+not+found",
                status_code=303,
            )

        if invoice.status != SupplierInvoiceStatus.DRAFT:
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error=Only+draft+invoices+can+be+edited",
                status_code=303,
            )

        context = base_context(request, auth, "Edit AP Invoice", "ap")
        context.update(
            self.invoice_form_context(
                db,
                str(auth.organization_id),
                supplier_id=str(invoice.supplier_id),
            )
        )

        lines = db.scalars(
            select(SupplierInvoiceLine)
            .where(SupplierInvoiceLine.invoice_id == inv_id)
            .order_by(SupplierInvoiceLine.line_number)
        ).all()

        context["invoice"] = {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "invoice_type": invoice.invoice_type,
            "supplier_id": invoice.supplier_id,
            "invoice_date": invoice.invoice_date,
            "received_date": invoice.received_date,
            "due_date": invoice.due_date,
            "vehicle_id": getattr(invoice, "vehicle_id", None),
            "currency_code": invoice.currency_code,
            "purpose": invoice.purpose or "",
            "exchange_rate": str(invoice.exchange_rate)
            if invoice.exchange_rate
            else "",
            "lines": [
                {
                    "line_id": line.line_id,
                    "item_id": line.item_id,
                    "expense_account_id": line.expense_account_id,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "tax_code_id": line.tax_code_id,
                    "tax_amount": line.tax_amount,
                    "receipt_warehouse_id": line.receipt_warehouse_id,
                    "receipt_reference": line.receipt_reference,
                    "receipt_serial_numbers": line.receipt_serial_numbers,
                    "receipt_auto_generate_serials": line.receipt_auto_generate_serials,
                    "cost_center_id": line.cost_center_id,
                    "project_id": line.project_id,
                }
                for line in lines
            ],
            "auto_create_inventory_receipt": invoice.auto_create_inventory_receipt,
            "inventory_receipt_mode": invoice.inventory_receipt_mode,
            "withholding_tax_amount": invoice.withholding_tax_amount,
            "withholding_tax_code_id": invoice.withholding_tax_code_id,
        }

        return templates.TemplateResponse(
            request, "finance/ap/invoice_form.html", context
        )

    async def create_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | JSONResponse | RedirectResponse | dict:
        """Handle invoice creation form submission."""
        content_type = request.headers.get("content-type", "")
        org_id = auth.organization_id
        user_id = auth.person_id
        if org_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        if "application/json" in content_type:
            data = await request.json()
        else:
            form_data = await request.form()
            data = dict(form_data)

        try:
            input_data = self.build_invoice_input(db, data, org_id)

            invoice = supplier_invoice_service.create_invoice(
                db=db,
                organization_id=org_id,
                input=input_data,
                created_by_user_id=user_id,
            )
            invoice_id = invoice.invoice_id
            db.commit()

            if "application/json" in content_type:
                return {
                    "success": True,
                    "invoice_id": str(invoice_id),
                    "redirect_url": f"/finance/ap/invoices/{invoice_id}",
                }

            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+created+successfully",
                status_code=303,
            )

        except Exception as e:
            db.rollback()
            logger.exception(
                "create_invoice_response failed for org %s", auth.organization_id
            )
            if "application/json" in content_type:
                return JSONResponse(
                    status_code=400,
                    content={"detail": str(e)},
                )

            context = base_context(request, auth, "New AP Invoice", "ap")
            context.update(self.invoice_form_context(db, str(auth.organization_id)))
            context["error"] = str(e)
            context["form_data"] = data
            return templates.TemplateResponse(
                request, "finance/ap/invoice_form.html", context
            )

    async def update_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> HTMLResponse | JSONResponse | RedirectResponse | dict:
        """Handle invoice update form submission."""
        content_type = request.headers.get("content-type", "")
        org_id = auth.organization_id
        if org_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        if "application/json" in content_type:
            data = await request.json()
        else:
            form_data = await request.form()
            data = dict(form_data)

        try:
            input_data = self.build_invoice_input(db, data, org_id)

            invoice = supplier_invoice_service.update_invoice(
                db=db,
                organization_id=org_id,
                invoice_id=coerce_uuid(invoice_id),
                input=input_data,
            )
            updated_invoice_id = invoice.invoice_id
            db.commit()

            if "application/json" in content_type:
                return JSONResponse(
                    content={
                        "success": True,
                        "invoice_id": str(updated_invoice_id),
                        "redirect_url": f"/finance/ap/invoices/{updated_invoice_id}",
                    }
                )

            return RedirectResponse(
                url=f"/finance/ap/invoices/{updated_invoice_id}?success=Invoice+updated+successfully",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            logger.exception("update_invoice_response: failed")
            if "application/json" in content_type:
                return JSONResponse(status_code=400, content={"detail": str(e)})

            context = base_context(request, auth, "Edit AP Invoice", "ap")
            context.update(self.invoice_form_context(db, str(auth.organization_id)))
            context["error"] = str(e)
            context["form_data"] = data
            return templates.TemplateResponse(
                request, "finance/ap/invoice_form.html", context
            )

    def invoice_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> HTMLResponse:
        """Render invoice detail page."""
        context = base_context(request, auth, "AP Invoice Details", "ap")
        context.update(
            self.invoice_detail_context(
                db,
                str(auth.organization_id),
                invoice_id,
            )
        )
        return templates.TemplateResponse(
            request, "finance/ap/invoice_detail.html", context
        )

    def delete_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> HTMLResponse | RedirectResponse:
        """Handle invoice deletion."""
        try:
            error = self.delete_invoice(db, str(auth.organization_id), invoice_id)
            if not error:
                db.commit()
        except Exception:
            db.rollback()
            raise

        if error:
            context = base_context(request, auth, "AP Invoice Details", "ap")
            context.update(
                self.invoice_detail_context(
                    db,
                    str(auth.organization_id),
                    invoice_id,
                )
            )
            context["error"] = error
            return templates.TemplateResponse(
                request, "finance/ap/invoice_detail.html", context
            )

        return RedirectResponse(
            url="/finance/ap/invoices?success=Record+deleted+successfully",
            status_code=303,
        )

    def submit_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Submit invoice for approval."""
        try:
            supplier_invoice_service.submit_invoice(
                db=db,
                organization_id=auth.organization_id,
                invoice_id=coerce_uuid(invoice_id),
                submitted_by_user_id=auth.user_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+submitted+for+approval",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )

    def approve_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Approve a submitted invoice."""
        try:
            supplier_invoice_service.approve_invoice(
                db=db,
                organization_id=auth.organization_id,
                invoice_id=coerce_uuid(invoice_id),
                approved_by_user_id=auth.user_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+approved",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )

    def reject_invoice_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> HTMLResponse:
        """Render AP invoice rejection confirmation form."""
        context = base_context(request, auth, "Reject AP Invoice", "ap")
        context.update(
            self.invoice_detail_context(
                db,
                str(auth.organization_id),
                invoice_id,
            )
        )
        return templates.TemplateResponse(
            request, "finance/ap/invoice_reject.html", context
        )

    async def reject_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Reject a submitted invoice."""
        form = await request.form()
        reason = str(form.get("rejection_reason") or "").strip()
        try:
            supplier_invoice_service.reject_invoice(
                db=db,
                organization_id=auth.organization_id,
                invoice_id=coerce_uuid(invoice_id),
                rejected_by_user_id=auth.user_id,
                reason=reason,
            )
            db.commit()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+rejected",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )

    def post_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Post invoice to general ledger."""
        try:
            supplier_invoice_service.post_invoice(
                db=db,
                organization_id=auth.organization_id,
                invoice_id=coerce_uuid(invoice_id),
                posted_by_user_id=auth.user_id,
            )
            db.commit()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+posted+to+ledger",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )

    def void_invoice_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Void an invoice."""
        try:
            supplier_invoice_service.void_invoice(
                db=db,
                organization_id=auth.organization_id,
                invoice_id=coerce_uuid(invoice_id),
                voided_by_user_id=auth.user_id,
                reason="Voided via web interface",
            )
            db.commit()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Invoice+voided",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )

    async def add_invoice_comment_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        invoice_id: str,
    ) -> RedirectResponse:
        """Append an internal comment to an AP invoice."""
        org_id = auth.organization_id
        user_id = auth.person_id or auth.user_id
        if org_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            invoice = supplier_invoice_service.get(
                db, invoice_id, organization_id=org_id
            )
        except Exception:
            return RedirectResponse(
                url="/finance/ap/invoices?error=Invoice+not+found",
                status_code=303,
            )

        form = await request.form()
        comment_text = str(form.get("comment", "")).strip()
        if not comment_text:
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error=Comment+is+required#comments",
                status_code=303,
            )

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"[{timestamp}] {user_id}: {comment_text}"
        invoice.comments = f"{invoice.comments}\n{entry}" if invoice.comments else entry

        mentioned_person_ids: set[UUID] = set()

        # Support @email mentions.
        mention_pattern = re.compile(
            r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
        )
        mentioned_emails = {m.lower() for m in mention_pattern.findall(comment_text)}
        if mentioned_emails:
            email_matches = db.scalars(
                select(Person).where(
                    Person.organization_id == org_id,
                    func.lower(Person.email).in_(mentioned_emails),
                )
            ).all()
            mentioned_person_ids.update(person.id for person in email_matches)

        # Support @Display Name mentions (for inline tagging in comment text).
        if "@" in comment_text:
            normalized_comment = " ".join(comment_text.lower().split())
            org_people = db.scalars(
                select(Person).where(Person.organization_id == org_id)
            ).all()
            for person in org_people:
                candidate_names = {
                    (person.display_name or "").strip(),
                    f"{person.first_name} {person.last_name}".strip(),
                }
                for candidate in candidate_names:
                    if not candidate:
                        continue
                    candidate_norm = " ".join(candidate.lower().split())
                    needle = f"@{candidate_norm}"
                    pos = normalized_comment.find(needle)
                    while pos != -1:
                        end_pos = pos + len(needle)
                        if (
                            end_pos == len(normalized_comment)
                            or not normalized_comment[end_pos].isalnum()
                        ):
                            mentioned_person_ids.add(person.id)
                            break
                        pos = normalized_comment.find(needle, pos + 1)

        mention_notification_created = False
        if mentioned_person_ids:
            actor_id = coerce_uuid(user_id)
            actor = db.get(Person, actor_id)
            actor_name = actor.name if actor else "A teammate"
            mentioned_people = db.scalars(
                select(Person).where(Person.id.in_(mentioned_person_ids))
            ).all()
            for person in mentioned_people:
                if person.id == actor_id:
                    continue
                notification_service.create(
                    db=db,
                    organization_id=coerce_uuid(org_id),
                    recipient_id=person.id,
                    entity_type=EntityType.INVOICE,
                    entity_id=coerce_uuid(invoice.invoice_id),
                    notification_type=NotificationType.MENTION,
                    title=f"Mentioned on AP invoice {invoice.invoice_number}",
                    message=(
                        f"{actor_name} mentioned you in a comment on invoice "
                        f"{invoice.invoice_number}."
                    ),
                    channel=NotificationChannel.BOTH,
                    action_url=f"/finance/ap/invoices/{invoice_id}#comments",
                    actor_id=actor_id,
                )
                mention_notification_created = True
        db.commit()

        if mention_notification_created:
            try:
                from app.tasks.notifications import process_pending_notification_emails

                process_pending_notification_emails.delay()
            except Exception:
                # The minute-based scheduler remains the delivery fallback when
                # the broker is temporarily unavailable.
                logger.exception(
                    "Failed to queue AP invoice mention email dispatch for %s",
                    invoice_id,
                )

        return RedirectResponse(
            url=f"/finance/ap/invoices/{invoice_id}?success=Comment+added#comments",
            status_code=303,
        )

    async def upload_invoice_attachment_response(
        self,
        request: Request,
        invoice_id: str,
        file: UploadFile,
        description: str | None,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse | JSONResponse:
        """Handle invoice attachment upload."""
        wants_json = request.headers.get(
            "x-requested-with"
        ) == "fetch" or "application/json" in request.headers.get("accept", "")
        try:
            org_id = auth.organization_id
            user_id = auth.person_id
            if org_id is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            if user_id is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            invoice = supplier_invoice_service.get(db, invoice_id)
            if not invoice or invoice.organization_id != auth.organization_id:
                if wants_json:
                    return JSONResponse(
                        status_code=404, content={"detail": "Invoice not found"}
                    )
                return RedirectResponse(
                    url=f"/finance/ap/invoices/{invoice_id}?error=Invoice+not+found",
                    status_code=303,
                )

            input_data = AttachmentInput(
                entity_type="SUPPLIER_INVOICE",
                entity_id=invoice_id,
                file_name=file.filename or "unnamed",
                content_type=file.content_type or "application/octet-stream",
                category=AttachmentCategory.INVOICE,
                description=description,
            )

            attachment_service.save_file(
                db=db,
                organization_id=org_id,
                input=input_data,
                file_content=file.file,
                uploaded_by=user_id,
            )

            if wants_json:
                return JSONResponse(content={"success": True})
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?success=Attachment+uploaded",
                status_code=303,
            )

        except ValueError as e:
            if wants_json:
                return JSONResponse(status_code=400, content={"detail": str(e)})
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error={str(e)}",
                status_code=303,
            )
        except Exception:
            logger.exception("upload_invoice_attachment_response: failed")
            if wants_json:
                return JSONResponse(
                    status_code=500, content={"detail": "Upload failed"}
                )
            return RedirectResponse(
                url=f"/finance/ap/invoices/{invoice_id}?error=Upload+failed",
                status_code=303,
            )


# Singleton instance
invoice_web_service = InvoiceWebService()
