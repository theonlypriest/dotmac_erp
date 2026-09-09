from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.models.people.payroll.salary_slip import SalarySlipStatus
from app.services.people.payroll.payroll_service import (
    PayrollService,
    PayrollServiceError,
)
from app.services.people.payroll.web import report_web


def _make_execute_result(return_rows):
    """Mock db.execute() result — .all() returns list of rows."""
    result = MagicMock()
    result.all.return_value = return_rows
    return result


def test_get_payroll_ytd_report_aggregates_totals_and_names():
    org_id = uuid4()

    base_rows = [
        SimpleNamespace(
            employee_id="emp-1",
            employee_code="EMP001",
            employee_name="Ada Lovelace",
            department_name="Engineering",
            slip_count=1,
            total_gross=Decimal("1000.00"),
            total_deductions=Decimal("100.00"),
            total_net=Decimal("900.00"),
        ),
        SimpleNamespace(
            employee_id="emp-2",
            employee_code="EMP002",
            employee_name="Grace Hopper",
            department_name=None,
            slip_count=2,
            total_gross=Decimal("2000.00"),
            total_deductions=Decimal("250.00"),
            total_net=Decimal("1750.00"),
        ),
    ]

    deduction_rows = [
        SimpleNamespace(
            employee_id="emp-1", component_code="PAYE", total_amount=Decimal("50.00")
        ),
        SimpleNamespace(
            employee_id="emp-1", component_code="PENSION", total_amount=Decimal("30.00")
        ),
        SimpleNamespace(
            employee_id="emp-2", component_code="NHF", total_amount=Decimal("20.00")
        ),
    ]

    db = MagicMock()
    db.execute.side_effect = [
        _make_execute_result(base_rows),
        _make_execute_result(deduction_rows),
    ]

    service = PayrollService(db)
    result = service.get_payroll_ytd_report(str(org_id), year=2026)

    assert result["totals"]["total_gross"] == Decimal("3000.00")
    assert result["totals"]["total_deductions"] == Decimal("350.00")
    assert result["totals"]["total_net"] == Decimal("2650.00")
    assert result["totals"]["total_paye"] == Decimal("50.00")
    assert result["totals"]["total_pension"] == Decimal("30.00")
    assert result["totals"]["total_nhf"] == Decimal("20.00")
    assert result["totals"]["slip_count"] == 3

    assert result["rows"][0]["employee_name"] == "Ada Lovelace"
    assert result["rows"][1]["employee_name"] == "Grace Hopper"


def test_get_payroll_tax_summary_groups_deduction_lines_by_component():
    org_id = uuid4()
    rows = [
        SimpleNamespace(
            component_id=uuid4(),
            component_name="PAYE Tax",
            component_code="PAYE",
            is_statutory=True,
            deduction_count=2,
            total_amount=Decimal("250.00"),
        ),
        SimpleNamespace(
            component_id=uuid4(),
            component_name="Staff Loan",
            component_code="LOAN",
            is_statutory=False,
            deduction_count=1,
            total_amount=Decimal("75.00"),
        ),
    ]
    db = MagicMock()
    db.execute.return_value = _make_execute_result(rows)

    result = PayrollService(db).get_payroll_tax_summary_report(
        org_id,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
    )

    assert result["statutory_total"] == Decimal("250.00")
    assert result["non_statutory_total"] == Decimal("75.00")
    assert result["total_deductions"] == Decimal("325.00")
    assert result["deductions"] == [
        {
            "component_id": str(rows[0].component_id),
            "component_name": "PAYE Tax",
            "component_code": "PAYE",
            "is_statutory": True,
            "deduction_count": 2,
            "total_amount": Decimal("250.00"),
            "percentage": 76.9,
        },
        {
            "component_id": str(rows[1].component_id),
            "component_name": "Staff Loan",
            "component_code": "LOAN",
            "is_statutory": False,
            "deduction_count": 1,
            "total_amount": Decimal("75.00"),
            "percentage": 23.1,
        },
    ]


def test_tax_summary_report_uses_the_submitted_date_range(monkeypatch):
    org_id = uuid4()
    report = {
        "start_date": date(2026, 3, 1),
        "end_date": date(2026, 3, 31),
        "deductions": [],
        "statutory_total": Decimal("0"),
        "non_statutory_total": Decimal("0"),
        "total_deductions": Decimal("0"),
    }
    captured: dict = {}

    def _tax_summary(_self, received_org_id, **kwargs):
        captured["organization_id"] = received_org_id
        captured.update(kwargs)
        return report

    monkeypatch.setattr(PayrollService, "get_payroll_tax_summary_report", _tax_summary)
    monkeypatch.setattr(report_web, "base_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        report_web.templates,
        "TemplateResponse",
        lambda _request, _name, context: context,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/people/payroll/reports/tax-summary",
            "query_string": b"start_date=2026-03-01&end_date=2026-03-31",
            "headers": [],
        }
    )

    context = report_web.ReportWebService().tax_summary_report_response(
        request,
        SimpleNamespace(organization_id=str(org_id)),
        MagicMock(),
    )

    assert captured == {
        "organization_id": org_id,
        "start_date": date(2026, 3, 1),
        "end_date": date(2026, 3, 31),
    }
    assert context["report"] is report


def test_approve_payroll_entry_fails_when_loan_posting_fails(monkeypatch):
    db = MagicMock()
    svc = PayrollService(db)

    creator_id = uuid4()
    approver_id = uuid4()
    slip = SimpleNamespace(
        slip_id="slip-1",
        slip_number="SLIP-001",
        status=SalarySlipStatus.SUBMITTED,
        created_by_id=creator_id,
        employee=None,
        employee_id="emp-1",
    )
    entry = SimpleNamespace(
        salary_slips=[slip],
        posting_date=None,
        status=None,
        entry_id="entry-1",
    )
    svc.get_payroll_entry = MagicMock(return_value=entry)

    pending_link = SimpleNamespace(
        loan_id="loan-1",
        amount=Decimal("100.00"),
        principal_portion=Decimal("100.00"),
        interest_portion=Decimal("0.00"),
        repayment_id=None,
    )
    db.scalars.return_value = SimpleNamespace(all=lambda: [pending_link])

    class _FailingLoanService:
        def __init__(self, _db):
            pass

        def record_payroll_deduction(self, **_kwargs):
            raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        "app.services.people.payroll.loan_service.LoanService",
        _FailingLoanService,
    )

    with pytest.raises(PayrollServiceError, match="Failed to process loan deduction"):
        svc.approve_payroll_entry(uuid4(), "entry-1", approved_by=approver_id)
