"""
Unit tests for PurchaseOrderService.

Tests cover:
- PO creation with validation
- PO workflow (submit, approve, cancel, close)
- Segregation of duties enforcement
- Received amount updates
- Getter and list methods
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.finance.ap.purchase_order import (
    POLineInput,
    POStatus,
    PurchaseOrderInput,
    PurchaseOrderService,
)


# Mock enums
class MockPOStatus:
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    @property
    def value(self):
        return self


class MockSupplier:
    """Mock Supplier model."""

    def __init__(
        self,
        supplier_id=None,
        organization_id=None,
        name="Test Supplier",
        primary_contact=None,
    ):
        self.supplier_id = supplier_id or uuid4()
        self.organization_id = organization_id or uuid4()
        self.name = name
        self.legal_name = name
        self.trading_name = None
        self.primary_contact = primary_contact
        self.is_active = True


class MockPurchaseOrder:
    """Mock PurchaseOrder model."""

    def __init__(
        self,
        po_id=None,
        organization_id=None,
        supplier_id=None,
        po_number="PO-000001",
        po_date=None,
        expected_delivery_date=None,
        currency_code="USD",
        exchange_rate=Decimal("1.00"),
        subtotal=Decimal("1000.00"),
        tax_amount=Decimal("100.00"),
        total_amount=Decimal("1100.00"),
        status=None,
        shipping_address=None,
        terms_and_conditions=None,
        budget_id=None,
        created_by_user_id=None,
        approved_by_user_id=None,
        approved_at=None,
        correlation_id=None,
    ):
        self.po_id = po_id or uuid4()
        self.organization_id = organization_id or uuid4()
        self.supplier_id = supplier_id or uuid4()
        self.po_number = po_number
        self.po_date = po_date or date.today()
        self.expected_delivery_date = expected_delivery_date
        self.currency_code = currency_code
        self.exchange_rate = exchange_rate
        self.subtotal = subtotal
        self.tax_amount = tax_amount
        self.total_amount = total_amount
        self.status = status or MockPOStatus()
        self.shipping_address = shipping_address
        self.terms_and_conditions = terms_and_conditions
        self.budget_id = budget_id
        self.created_by_user_id = created_by_user_id or uuid4()
        self.approved_by_user_id = approved_by_user_id
        self.approved_at = approved_at
        self.correlation_id = correlation_id


class MockPurchaseOrderLine:
    """Mock PurchaseOrderLine model."""

    def __init__(
        self,
        line_id=None,
        po_id=None,
        line_number=1,
        item_id=None,
        description="Test Item",
        quantity_ordered=Decimal("10.00"),
        quantity_received=Decimal("0"),
        quantity_invoiced=Decimal("0"),
        unit_price=Decimal("100.00"),
        line_amount=Decimal("1000.00"),
        tax_code_id=None,
        tax_amount=Decimal("100.00"),
        expense_account_id=None,
        asset_account_id=None,
        cost_center_id=None,
        project_id=None,
        segment_id=None,
        delivery_date=None,
    ):
        self.line_id = line_id or uuid4()
        self.po_id = po_id or uuid4()
        self.line_number = line_number
        self.item_id = item_id
        self.description = description
        self.quantity_ordered = quantity_ordered
        self.quantity_received = quantity_received
        self.quantity_invoiced = quantity_invoiced
        self.unit_price = unit_price
        self.line_amount = line_amount
        self.tax_code_id = tax_code_id
        self.tax_amount = tax_amount
        self.expense_account_id = expense_account_id
        self.asset_account_id = asset_account_id
        self.cost_center_id = cost_center_id
        self.project_id = project_id
        self.segment_id = segment_id
        self.delivery_date = delivery_date


# ===================== CREATE PO TESTS =====================


class TestCreatePO:
    """Tests for purchase order creation."""

    @patch("app.services.finance.ap.purchase_order.SequenceService")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderLine")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrder")
    def test_create_po_success(self, mock_po_class, mock_line_class, mock_seq_service):
        """Test successful PO creation."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        # Mock supplier lookup
        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier

        # Mock sequence
        mock_seq_service.get_next_number.return_value = "PO-000001"

        # Mock PO creation
        mock_po = MockPurchaseOrder(
            organization_id=org_id,
            supplier_id=supplier_id,
        )
        mock_po_class.return_value = mock_po

        # Create input with lines
        line_input = POLineInput(
            description="Office Supplies",
            quantity_ordered=Decimal("10"),
            unit_price=Decimal("50.00"),
            tax_amount=Decimal("50.00"),
        )

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[line_input],
        )

        result = PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert result is not None
        db.add.assert_called()
        db.flush.assert_called()
        mock_seq_service.get_next_number.assert_called_once()

    def test_create_po_supplier_not_found(self):
        """Test PO creation with non-existent supplier."""
        db = MagicMock()
        org_id = uuid4()
        user_id = uuid4()

        # Supplier not found
        db.scalars.return_value.first.return_value = None

        input_data = PurchaseOrderInput(
            supplier_id=uuid4(),
            po_date=date.today(),
            currency_code="USD",
            lines=[
                POLineInput(
                    description="Test",
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("100.00"),
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert exc_info.value.status_code == 404
        assert "Supplier not found" in str(exc_info.value.detail)

    def test_create_po_no_lines(self):
        """Test PO creation without lines fails."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[],  # Empty lines
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert exc_info.value.status_code == 400
        assert "at least one line" in str(exc_info.value.detail).lower()

    def test_create_po_inactive_supplier(self):
        """F41: PO creation against an inactive supplier is rejected."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        mock_supplier.is_active = False
        db.scalars.return_value.first.return_value = mock_supplier

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[
                POLineInput(
                    description="Test",
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("100.00"),
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert exc_info.value.status_code == 400
        assert "not active" in str(exc_info.value.detail).lower()

    def test_create_po_non_positive_quantity(self):
        """F39: PO line with quantity_ordered <= 0 is rejected."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[
                POLineInput(
                    description="Test",
                    quantity_ordered=Decimal("0"),
                    unit_price=Decimal("100.00"),
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert exc_info.value.status_code == 400
        assert "quantity ordered" in str(exc_info.value.detail).lower()

    def test_create_po_negative_unit_price(self):
        """F39: PO line with negative unit_price is rejected."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[
                POLineInput(
                    description="Test",
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("-100.00"),
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert exc_info.value.status_code == 400
        assert "unit price" in str(exc_info.value.detail).lower()


class TestUpdatePO:
    """Tests for purchase order updates."""

    def test_update_po_success_replaces_lines(self):
        """Draft purchase orders can be updated and lines replaced."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        po_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            status=MockPOStatus(),
        )
        mock_po.status = POStatus.DRAFT
        existing_line = MockPurchaseOrderLine(po_id=po_id)

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        supplier_result = MagicMock()
        supplier_result.first.return_value = mock_supplier
        lines_result = MagicMock()
        lines_result.all.return_value = [existing_line]
        db.scalars.side_effect = [po_result, supplier_result, lines_result]

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            exchange_rate=Decimal("1.25"),
            terms_and_conditions="Net 30",
            lines=[
                POLineInput(
                    description="Updated line",
                    quantity_ordered=Decimal("2"),
                    unit_price=Decimal("50.00"),
                    tax_amount=Decimal("5.00"),
                )
            ],
        )

        result = PurchaseOrderService.update_po(db, org_id, po_id, input_data)

        assert result is mock_po
        assert mock_po.supplier_id == supplier_id
        assert mock_po.currency_code == "USD"
        assert mock_po.subtotal == Decimal("100.00")
        assert mock_po.tax_amount == Decimal("5.00")
        assert mock_po.total_amount == Decimal("105.00")
        db.delete.assert_called_once_with(existing_line)
        db.flush.assert_called()
        db.refresh.assert_called_once_with(mock_po)
        assert db.add.call_count == 1

    def test_update_po_rejects_non_draft(self):
        """Non-draft purchase orders cannot be edited."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        po_id = uuid4()

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            status=POStatus.APPROVED,
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        db.scalars.return_value = po_result

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=[
                POLineInput(
                    description="Updated line",
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("10.00"),
                )
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.update_po(db, org_id, po_id, input_data)

        assert exc_info.value.status_code == 400
        assert "Cannot edit PO in APPROVED status" in str(exc_info.value.detail)


class TestDeletePO:
    """Tests for purchase order deletion."""

    def test_delete_po_success_for_draft(self):
        """Draft purchase orders can be deleted."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        po_id = uuid4()

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            status=POStatus.DRAFT,
        )
        existing_line = MockPurchaseOrderLine(po_id=po_id)

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        lines_result = MagicMock()
        lines_result.all.return_value = [existing_line]
        db.scalars.side_effect = [po_result, lines_result]

        PurchaseOrderService.delete_po(db, org_id, po_id)

        db.delete.assert_called_once_with(mock_po)
        db.flush.assert_called()

    def test_delete_po_rejects_non_draft(self):
        """Non-draft purchase orders cannot be deleted."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        po_id = uuid4()

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            status=POStatus.APPROVED,
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        db.scalars.return_value = po_result

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.delete_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "Cannot delete PO in APPROVED status" in str(exc_info.value.detail)

    def test_delete_po_rejects_received_or_invoiced_lines(self):
        """Purchase orders with line activity cannot be deleted."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        po_id = uuid4()

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            status=POStatus.DRAFT,
        )
        existing_line = MockPurchaseOrderLine(
            po_id=po_id,
            quantity_received=Decimal("1"),
        )
        existing_line.quantity_invoiced = Decimal("0")

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        lines_result = MagicMock()
        lines_result.all.return_value = [existing_line]
        db.scalars.side_effect = [po_result, lines_result]

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.delete_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "received or invoiced quantities" in str(exc_info.value.detail)

    @patch("app.services.finance.ap.purchase_order.SequenceService")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderLine")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrder")
    def test_create_po_calculates_totals(
        self, mock_po_class, mock_line_class, mock_seq_service
    ):
        """Test PO creation calculates totals correctly."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier
        mock_seq_service.get_next_number.return_value = "PO-000001"

        # Track the actual values passed to PurchaseOrder constructor
        captured_values = {}

        def capture_po_init(**kwargs):
            captured_values.update(kwargs)
            return MockPurchaseOrder(**kwargs)

        mock_po_class.side_effect = capture_po_init

        # Multiple lines with different amounts
        lines = [
            POLineInput(
                description="Item 1",
                quantity_ordered=Decimal("5"),
                unit_price=Decimal("100.00"),  # 500
                tax_amount=Decimal("50.00"),
            ),
            POLineInput(
                description="Item 2",
                quantity_ordered=Decimal("10"),
                unit_price=Decimal("25.00"),  # 250
                tax_amount=Decimal("25.00"),
            ),
        ]

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="USD",
            lines=lines,
        )

        PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        # Subtotal: 500 + 250 = 750
        # Tax: 50 + 25 = 75
        # Total: 825
        assert captured_values.get("subtotal") == Decimal("750")
        assert captured_values.get("tax_amount") == Decimal("75")
        assert captured_values.get("total_amount") == Decimal("825")

    @patch("app.services.finance.ap.purchase_order.SequenceService")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderLine")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrder")
    def test_create_po_with_all_fields(
        self, mock_po_class, mock_line_class, mock_seq_service
    ):
        """Test PO creation with all optional fields."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()
        user_id = uuid4()
        budget_id = uuid4()

        mock_supplier = MockSupplier(supplier_id=supplier_id, organization_id=org_id)
        db.scalars.return_value.first.return_value = mock_supplier
        mock_seq_service.get_next_number.return_value = "PO-000001"

        mock_po = MockPurchaseOrder(organization_id=org_id, supplier_id=supplier_id)
        mock_po_class.return_value = mock_po

        input_data = PurchaseOrderInput(
            supplier_id=supplier_id,
            po_date=date.today(),
            currency_code="EUR",
            lines=[
                POLineInput(
                    description="Item",
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("100.00"),
                    item_id=uuid4(),
                    tax_code_id=uuid4(),
                    expense_account_id=uuid4(),
                    cost_center_id=uuid4(),
                    project_id=uuid4(),
                    delivery_date=date(2025, 6, 30),
                )
            ],
            expected_delivery_date=date(2025, 7, 1),
            exchange_rate=Decimal("1.10"),
            shipping_address={"address": "123 Main St"},
            terms_and_conditions="Net 30",
            budget_id=budget_id,
            correlation_id="REQ-123",
        )

        result = PurchaseOrderService.create_po(db, org_id, input_data, user_id)

        assert result is not None
        db.add.assert_called()


# ===================== SUBMIT FOR APPROVAL TESTS =====================


class TestSubmitForApproval:
    """Tests for PO submission for approval."""

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_submit_for_approval_success(self, mock_status_class):
        """Test successful PO submission."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        user_id = uuid4()

        # Setup status mocks
        mock_draft = MagicMock()
        mock_draft.value = "DRAFT"
        mock_pending = MagicMock()
        mock_status_class.DRAFT = mock_draft
        mock_status_class.PENDING_APPROVAL = mock_pending

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_draft

        db.scalars.return_value.first.return_value = mock_po

        result = PurchaseOrderService.submit_for_approval(db, org_id, po_id, user_id)

        assert result is not None
        assert mock_po.status == mock_pending
        db.flush.assert_called()

    def test_submit_for_approval_not_found(self):
        """Test submission of non-existent PO."""
        db = MagicMock()
        org_id = uuid4()
        user_id = uuid4()

        db.scalars.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.submit_for_approval(db, org_id, uuid4(), user_id)

        assert exc_info.value.status_code == 404

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_submit_for_approval_wrong_status(self, mock_status_class):
        """Test submission of PO not in DRAFT status."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        user_id = uuid4()

        mock_draft = MagicMock()
        mock_draft.value = "DRAFT"
        mock_approved = MagicMock()
        mock_approved.value = "APPROVED"
        mock_status_class.DRAFT = mock_draft

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_approved  # Wrong status

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.submit_for_approval(db, org_id, po_id, user_id)

        assert exc_info.value.status_code == 400
        assert "Cannot submit" in str(exc_info.value.detail)


# ===================== APPROVE PO TESTS =====================


class TestApprovePO:
    """Tests for PO approval."""

    @patch("app.services.finance.ap.purchase_order.queue_email")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderPDFService")
    @patch("app.services.finance.ap.purchase_order.render_branded_email")
    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_success(
        self,
        mock_status_class,
        mock_render_branded_email,
        mock_pdf_service,
        mock_queue_email,
    ):
        """Test successful PO approval."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        supplier_id = uuid4()
        creator_id = uuid4()
        approver_id = uuid4()  # Different from creator

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_approved = MagicMock()
        mock_status_class.PENDING_APPROVAL = mock_pending
        mock_status_class.APPROVED = mock_approved

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            created_by_user_id=creator_id,
        )
        mock_po.status = mock_pending
        mock_supplier = MockSupplier(
            supplier_id=supplier_id,
            organization_id=org_id,
            primary_contact={"email": "supplier@example.com"},
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        supplier_result = MagicMock()
        supplier_result.first.return_value = mock_supplier
        db.scalars.side_effect = [po_result, supplier_result]
        mock_render_branded_email.return_value = ("<p>approved</p>", "approved")
        mock_pdf_service.return_value.generate_pdf.return_value = b"%PDF-1.4"

        result = PurchaseOrderService.approve_po(db, org_id, po_id, approver_id)

        assert result is not None
        assert mock_po.status == mock_approved
        assert mock_po.approved_by_user_id == approver_id
        assert mock_po.approved_at is not None
        mock_render_branded_email.assert_called_once()
        mock_queue_email.assert_called_once_with(
            to_email="supplier@example.com",
            subject=f"Purchase Order Approved: {mock_po.po_number}",
            body_html="<p>approved</p>",
            body_text="approved",
            attachments=[(f"{mock_po.po_number}.pdf", b"%PDF-1.4", "application/pdf")],
            module="FINANCE",
            organization_id=str(org_id),
        )
        db.flush.assert_called()

    @patch("app.services.finance.ap.purchase_order.queue_email")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderPDFService")
    @patch("app.services.finance.ap.purchase_order.render_branded_email")
    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_skips_supplier_notification_without_email(
        self,
        mock_status_class,
        mock_render_branded_email,
        mock_pdf_service,
        mock_queue_email,
    ):
        """Approval should still succeed when supplier has no email."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        supplier_id = uuid4()
        creator_id = uuid4()
        approver_id = uuid4()

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_approved = MagicMock()
        mock_status_class.PENDING_APPROVAL = mock_pending
        mock_status_class.APPROVED = mock_approved

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            created_by_user_id=creator_id,
        )
        mock_po.status = mock_pending
        mock_supplier = MockSupplier(
            supplier_id=supplier_id,
            organization_id=org_id,
            primary_contact={},
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        supplier_result = MagicMock()
        supplier_result.first.return_value = mock_supplier
        db.scalars.side_effect = [po_result, supplier_result]

        result = PurchaseOrderService.approve_po(db, org_id, po_id, approver_id)

        assert result is mock_po
        mock_render_branded_email.assert_not_called()
        mock_pdf_service.assert_not_called()
        mock_queue_email.assert_not_called()
        db.flush.assert_called()

    @patch("app.services.finance.ap.purchase_order.queue_email")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderPDFService")
    @patch("app.services.finance.ap.purchase_order.render_branded_email")
    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_queues_email_without_attachment_when_pdf_generation_fails(
        self,
        mock_status_class,
        mock_render_branded_email,
        mock_pdf_service,
        mock_queue_email,
    ):
        """Approval should still queue email when PDF generation fails."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        supplier_id = uuid4()
        creator_id = uuid4()
        approver_id = uuid4()

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_approved = MagicMock()
        mock_status_class.PENDING_APPROVAL = mock_pending
        mock_status_class.APPROVED = mock_approved

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            created_by_user_id=creator_id,
        )
        mock_po.status = mock_pending
        mock_supplier = MockSupplier(
            supplier_id=supplier_id,
            organization_id=org_id,
            primary_contact={"email": "supplier@example.com"},
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        supplier_result = MagicMock()
        supplier_result.first.return_value = mock_supplier
        db.scalars.side_effect = [po_result, supplier_result]
        mock_render_branded_email.return_value = ("<p>approved</p>", "approved")
        mock_pdf_service.return_value.generate_pdf.side_effect = RuntimeError(
            "pdf unavailable"
        )

        result = PurchaseOrderService.approve_po(db, org_id, po_id, approver_id)

        assert result is mock_po
        mock_queue_email.assert_called_once_with(
            to_email="supplier@example.com",
            subject=f"Purchase Order Approved: {mock_po.po_number}",
            body_html="<p>approved</p>",
            body_text="approved",
            attachments=None,
            module="FINANCE",
            organization_id=str(org_id),
        )
        db.flush.assert_called()

    @patch("app.services.finance.ap.purchase_order.queue_email")
    @patch("app.services.finance.ap.purchase_order.PurchaseOrderPDFService")
    @patch("app.services.finance.ap.purchase_order.render_branded_email")
    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_continues_when_supplier_notification_rendering_fails(
        self,
        mock_status_class,
        mock_render_branded_email,
        mock_pdf_service,
        mock_queue_email,
    ):
        """Approval should not fail if email rendering raises."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        supplier_id = uuid4()
        creator_id = uuid4()
        approver_id = uuid4()

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_approved = MagicMock()
        mock_status_class.PENDING_APPROVAL = mock_pending
        mock_status_class.APPROVED = mock_approved

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            supplier_id=supplier_id,
            created_by_user_id=creator_id,
        )
        mock_po.status = mock_pending
        mock_supplier = MockSupplier(
            supplier_id=supplier_id,
            organization_id=org_id,
            primary_contact={"email": "supplier@example.com"},
        )

        po_result = MagicMock()
        po_result.first.return_value = mock_po
        supplier_result = MagicMock()
        supplier_result.first.return_value = mock_supplier
        db.scalars.side_effect = [po_result, supplier_result]
        mock_render_branded_email.side_effect = RuntimeError("smtp unavailable")

        result = PurchaseOrderService.approve_po(db, org_id, po_id, approver_id)

        assert result is mock_po
        mock_pdf_service.assert_not_called()
        mock_queue_email.assert_not_called()
        db.flush.assert_called()

    def test_approve_po_not_found(self):
        """Test approval of non-existent PO."""
        db = MagicMock()
        org_id = uuid4()

        db.scalars.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.approve_po(db, org_id, uuid4(), uuid4())

        assert exc_info.value.status_code == 404

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_wrong_status(self, mock_status_class):
        """Test approval of PO not in PENDING_APPROVAL status."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_draft = MagicMock()
        mock_draft.value = "DRAFT"
        mock_status_class.PENDING_APPROVAL = mock_pending

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_draft

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.approve_po(db, org_id, po_id, uuid4())

        assert exc_info.value.status_code == 400
        assert "Cannot approve" in str(exc_info.value.detail)

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_approve_po_segregation_of_duties(self, mock_status_class):
        """Test SoD enforcement - approver cannot be creator."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()
        user_id = uuid4()  # Same user creates and approves

        mock_pending = MagicMock()
        mock_pending.value = "PENDING_APPROVAL"
        mock_status_class.PENDING_APPROVAL = mock_pending

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
            created_by_user_id=user_id,  # Creator is the same
        )
        mock_po.status = mock_pending

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.approve_po(db, org_id, po_id, user_id)  # Same user

        assert exc_info.value.status_code == 400
        assert "Segregation of Duties" in str(exc_info.value.detail)


# ===================== CANCEL PO TESTS =====================


class TestCancelPO:
    """Tests for PO cancellation."""

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_cancel_po_success(self, mock_status_class):
        """Test successful PO cancellation."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_draft = MagicMock()
        mock_received = MagicMock()
        mock_closed = MagicMock()
        mock_cancelled = MagicMock()
        mock_status_class.RECEIVED = mock_received
        mock_status_class.CLOSED = mock_closed
        mock_status_class.CANCELLED = mock_cancelled

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
        )
        mock_po.status = mock_draft

        db.scalars.return_value.first.return_value = mock_po

        # `amount_received` is DERIVED now, not a column on the row, so the
        # guard reads it from its owner rather than off `mock_po`.
        with patch(
            "app.services.finance.ap.purchase_order_amounts.received_for",
            return_value=Decimal("0"),
        ):
            result = PurchaseOrderService.cancel_po(db, org_id, po_id)

        assert result is not None
        assert mock_po.status == mock_cancelled
        db.flush.assert_called()

    def test_cancel_po_not_found(self):
        """Test cancellation of non-existent PO."""
        db = MagicMock()
        org_id = uuid4()

        db.scalars.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.cancel_po(db, org_id, uuid4())

        assert exc_info.value.status_code == 404

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_cancel_po_received_status(self, mock_status_class):
        """Test cannot cancel PO in RECEIVED status."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_received = MagicMock()
        mock_received.value = "RECEIVED"
        mock_closed = MagicMock()
        mock_status_class.RECEIVED = mock_received
        mock_status_class.CLOSED = mock_closed

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_received

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.cancel_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "Cannot cancel" in str(exc_info.value.detail)

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_cancel_po_closed_status(self, mock_status_class):
        """Test cannot cancel PO in CLOSED status."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_received = MagicMock()
        mock_closed = MagicMock()
        mock_closed.value = "CLOSED"
        mock_status_class.RECEIVED = mock_received
        mock_status_class.CLOSED = mock_closed

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_closed

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.cancel_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_cancel_po_with_received_goods(self, mock_status_class):
        """Test cannot cancel PO with received goods."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_received = MagicMock()
        mock_closed = MagicMock()
        mock_draft = MagicMock()
        mock_status_class.RECEIVED = mock_received
        mock_status_class.CLOSED = mock_closed

        mock_po = MockPurchaseOrder(
            po_id=po_id,
            organization_id=org_id,
        )
        mock_po.status = mock_draft

        db.scalars.return_value.first.return_value = mock_po

        # The receipts are the authority here, not a stored header column: the
        # guard asks `purchase_order_amounts`, which sums the line quantities.
        with (
            patch(
                "app.services.finance.ap.purchase_order_amounts.received_for",
                return_value=Decimal("500.00"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            PurchaseOrderService.cancel_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "received goods" in str(exc_info.value.detail).lower()


# ===================== CLOSE PO TESTS =====================


class TestClosePO:
    """Tests for PO closing."""

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_close_po_success(self, mock_status_class):
        """Test successful PO closing."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_cancelled = MagicMock()
        mock_closed = MagicMock()
        mock_received = MagicMock()
        mock_status_class.CANCELLED = mock_cancelled
        mock_status_class.CLOSED = mock_closed
        mock_status_class.RECEIVED = mock_received

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_received

        db.scalars.return_value.first.return_value = mock_po

        result = PurchaseOrderService.close_po(db, org_id, po_id)

        assert result is not None
        assert mock_po.status == mock_closed
        db.flush.assert_called()

    def test_close_po_not_found(self):
        """Test closing non-existent PO."""
        db = MagicMock()
        org_id = uuid4()

        db.scalars.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.close_po(db, org_id, uuid4())

        assert exc_info.value.status_code == 404

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_close_po_cancelled(self, mock_status_class):
        """Test cannot close cancelled PO."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_cancelled = MagicMock()
        mock_cancelled.value = "CANCELLED"
        mock_status_class.CANCELLED = mock_cancelled

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_cancelled

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.close_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "cancelled" in str(exc_info.value.detail).lower()

    @patch("app.services.finance.ap.purchase_order.POStatus")
    def test_close_po_draft_rejected(self, mock_status_class):
        """F43: a DRAFT PO cannot be closed (only APPROVED/received)."""
        db = MagicMock()
        org_id = uuid4()
        po_id = uuid4()

        mock_draft = MagicMock()
        mock_draft.value = "DRAFT"
        mock_status_class.DRAFT = mock_draft

        mock_po = MockPurchaseOrder(po_id=po_id, organization_id=org_id)
        mock_po.status = mock_draft

        db.scalars.return_value.first.return_value = mock_po

        with pytest.raises(HTTPException) as exc_info:
            PurchaseOrderService.close_po(db, org_id, po_id)

        assert exc_info.value.status_code == 400
        assert "draft" in str(exc_info.value.detail).lower()


# ===================== UPDATE RECEIVED AMOUNT TESTS =====================


class TestTheReceivedAmountHasNoSetter:
    """`update_received_amount` is gone, and must not come back.

    It incremented `po.amount_received` by a caller-supplied delta while
    `GoodsReceiptService._update_po_status` recomputed the same column absolutely
    from the PO lines. Two writers, two different arithmetics, one column: the
    stored value was whichever ran last. Nothing in `app/` ever called it — only
    the tests that used to live here.

    The received amount is now derived by
    `app.services.finance.ap.purchase_order_amounts`, which stores nothing. The
    behaviour those deleted tests covered (partial, full and over-delivery
    totals) is proved end to end against real PostgreSQL in
    `tests/integration/test_purchase_order_derived_amounts.py`, where the sum
    can actually be exercised against real receipt rows.
    """

    def test_the_service_exposes_no_received_amount_setter(self):
        assert not hasattr(PurchaseOrderService, "update_received_amount"), (
            "PurchaseOrderService.update_received_amount is back. The received "
            "amount is derived by purchase_order_amounts and is stored nowhere; "
            "a setter here is a second authority over the fact."
        )

    def test_the_purchase_order_model_has_no_amount_columns(self):
        from app.models.finance.ap.purchase_order import PurchaseOrder

        mapped = set(PurchaseOrder.__mapper__.columns.keys())
        assert "amount_received" not in mapped
        assert "amount_invoiced" not in mapped


class TestGetters:
    """Tests for getter methods."""

    def test_get_po(self):
        """Test getting PO by ID."""
        db = MagicMock()
        po_id = uuid4()

        mock_po = MockPurchaseOrder(po_id=po_id)
        db.get.return_value = mock_po

        result = PurchaseOrderService.get(db, str(po_id))

        assert result is not None
        assert result.po_id == po_id

    def test_get_po_not_found(self):
        """Test getting non-existent PO."""
        db = MagicMock()

        db.get.return_value = None

        result = PurchaseOrderService.get(db, str(uuid4()))

        assert result is None

    def test_get_by_number(self):
        """Test getting PO by number."""
        db = MagicMock()
        org_id = uuid4()

        mock_po = MockPurchaseOrder(po_number="PO-000001")
        db.scalars.return_value.first.return_value = mock_po

        result = PurchaseOrderService.get_by_number(db, org_id, "PO-000001")

        assert result is not None
        assert result.po_number == "PO-000001"

    def test_get_po_lines(self):
        """Test getting PO lines."""
        db = MagicMock()
        po_id = uuid4()

        lines = [
            MockPurchaseOrderLine(line_number=1),
            MockPurchaseOrderLine(line_number=2),
        ]
        db.scalars.return_value.all.return_value = lines

        result = PurchaseOrderService.get_po_lines(db, str(po_id))

        assert len(result) == 2


# ===================== LIST TESTS =====================


class TestListPOs:
    """Tests for listing purchase orders."""

    def test_list_pos(self):
        """Test listing purchase orders."""
        db = MagicMock()
        org_id = uuid4()

        pos = [
            MockPurchaseOrder(po_number="PO-000001"),
            MockPurchaseOrder(po_number="PO-000002"),
        ]
        db.scalars.return_value.all.return_value = pos

        result = PurchaseOrderService.list(db, organization_id=str(org_id))

        assert len(result) == 2

    def test_list_pos_with_filters(self):
        """Test listing POs with filters."""
        db = MagicMock()
        org_id = uuid4()
        supplier_id = uuid4()

        pos = [MockPurchaseOrder()]
        db.scalars.return_value.all.return_value = pos

        result = PurchaseOrderService.list(
            db,
            organization_id=str(org_id),
            supplier_id=str(supplier_id),
            status=MockPOStatus.APPROVED,
            from_date=date(2024, 1, 1),
            to_date=date(2024, 12, 31),
            limit=10,
            offset=0,
        )

        assert len(result) == 1
        db.scalars.assert_called_once()

    def test_list_pos_empty(self):
        """Test listing returns empty when no POs."""
        db = MagicMock()
        org_id = uuid4()

        db.scalars.return_value.all.return_value = []

        result = PurchaseOrderService.list(db, organization_id=str(org_id))

        assert len(result) == 0

    def test_list_pos_pagination(self):
        """Test list respects pagination."""
        db = MagicMock()
        org_id = uuid4()

        pos = [MockPurchaseOrder()]
        db.scalars.return_value.all.return_value = pos

        result = PurchaseOrderService.list(
            db, organization_id=str(org_id), limit=5, offset=10
        )

        assert len(result) == 1
        # Verify offset and limit were called
        db.scalars.assert_called_once()
