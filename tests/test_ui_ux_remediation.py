from __future__ import annotations

from pathlib import Path

import pytest


APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("DRAFT", False),
        ("SUBMITTED", False),
        ("PENDING_APPROVAL", False),
        ("APPROVED", True),
        ("POSTED", True),
        ("PARTIALLY_PAID", True),
        ("PAID", False),
        ("REJECTED", False),
        ("VOID", False),
    ],
)
def test_ap_payment_cta_eligibility_matches_payment_form_statuses(
    status: str, expected: bool
) -> None:
    base = (APP / "services" / "finance" / "ap" / "web" / "base.py").read_text(
        encoding="utf-8"
    )
    eligibility_block = base[
        base.index("AP_PAYMENT_ELIGIBLE_INVOICE_STATUSES") : base.index(
            "def parse_invoice_status"
        )
    ]

    if expected:
        assert f"SupplierInvoiceStatus.{status}" in eligibility_block
    else:
        assert f"SupplierInvoiceStatus.{status}" not in eligibility_block


def test_ap_invoice_templates_use_derived_payment_cta_flag() -> None:
    invoice_list = (TEMPLATES / "finance" / "ap" / "invoices.html").read_text(
        encoding="utf-8"
    )
    invoice_detail = (TEMPLATES / "finance" / "ap" / "invoice_detail.html").read_text(
        encoding="utf-8"
    )

    assert "invoice.can_record_payment" in invoice_list
    assert "invoice.can_record_payment" in invoice_detail
    assert "OVERDUE" not in invoice_detail.partition("Record Payment")[0]


def test_ap_payment_detail_does_not_expose_unsupported_edit_affordance() -> None:
    template = (TEMPLATES / "finance" / "ap" / "payment_detail.html").read_text(
        encoding="utf-8"
    )

    assert "/finance/ap/payments/{{ payment.payment_id }}/edit" not in template


def test_expense_claim_detail_covers_known_redirect_outcomes() -> None:
    template = (TEMPLATES / "expense" / "claim_detail.html").read_text(encoding="utf-8")

    for action in [
        "submitted",
        "resubmitted",
        "approval_recorded",
        "cancelled",
        "approved",
        "rejected",
    ]:
        assert f'"{action}"' in template

    for error in ["submit_failed", "cancel_failed", "resubmit_failed"]:
        assert f'"{error}"' in template


def test_procurement_dashboard_uses_total_pending_requisition_count() -> None:
    source = (
        APP / "services" / "procurement" / "web" / "procurement_web.py"
    ).read_text(encoding="utf-8")

    assert "pending_reqs, pending_req_count = req_service.list_requisitions" in source
    assert '"pending_req_count": pending_req_count' in source
    assert '"pending_req_count": len(pending_reqs)' not in source


def test_procurement_requisition_list_uses_shared_pagination_and_alpine_modals() -> (
    None
):
    template = (TEMPLATES / "procurement" / "requisitions" / "list.html").read_text(
        encoding="utf-8"
    )

    assert "pagination(" in template
    assert 'x-trap="importOpen"' in template
    assert 'x-trap="exportOpen"' in template
    assert "Previous</a>" not in template


def test_procurement_requisition_and_rfq_lists_expose_page_pagination() -> None:
    web_routes = (APP / "web" / "procurement.py").read_text(encoding="utf-8")
    web_service = (
        APP / "services" / "procurement" / "web" / "procurement_web.py"
    ).read_text(encoding="utf-8")

    assert "page: int | None = Query(None, ge=1)" in web_routes
    assert "offset = (page - 1) * limit" in web_routes
    assert '"page": (offset // limit) + 1 if limit else 1' in web_service
    assert (
        '"total_pages": max(1, (total + limit - 1) // limit) if limit else 1'
        in web_service
    )


def test_procurement_rfq_list_uses_shared_pagination_and_alpine_modals() -> None:
    template = (TEMPLATES / "procurement" / "rfqs" / "list.html").read_text(
        encoding="utf-8"
    )

    assert "pagination(" in template
    assert 'filters={"status": filter_status, "method": filter_method}' in template
    assert 'x-trap="importOpen"' in template
    assert 'x-trap="exportOpen"' in template
    assert "offset={{ offset" not in template
    assert "Previous</a>" not in template


def test_inventory_item_actions_are_permission_flagged() -> None:
    template = (TEMPLATES / "inventory" / "items.html").read_text(encoding="utf-8")

    for flag in [
        "can_create_item",
        "can_update_item",
        "can_delete_item",
        "can_export_item",
        "can_bulk_items",
    ]:
        assert flag in template


def test_procurement_forms_preserve_posted_values() -> None:
    contract = (TEMPLATES / "procurement" / "contracts" / "form.html").read_text(
        encoding="utf-8"
    )
    prequalification = (
        TEMPLATES / "procurement" / "vendors" / "prequalification_form.html"
    ).read_text(encoding="utf-8")

    assert "form_data" in contract
    assert "fd.contract_number" in contract
    assert '<select id="supplier_id"' in contract
    assert "form_data" in prequalification
    assert "fd.application_date" in prequalification


def test_people_leave_rejection_uses_modal_reason_not_prompt() -> None:
    detail = (TEMPLATES / "people" / "leave" / "application_detail.html").read_text(
        encoding="utf-8"
    )
    team = (TEMPLATES / "people" / "self" / "team_leave.html").read_text(
        encoding="utf-8"
    )

    assert "prompt(" not in detail
    assert 'x-trap="rejectOpen"' in detail
    assert 'value="Rejected"' not in team
    assert 'x-trap="rejectOpen"' in team


def test_inventory_transaction_related_empty_state_is_user_friendly() -> None:
    template = (TEMPLATES / "inventory" / "transaction_detail.html").read_text(
        encoding="utf-8"
    )

    assert "No related transactions" in template
    assert "related_transactions found" not in template
