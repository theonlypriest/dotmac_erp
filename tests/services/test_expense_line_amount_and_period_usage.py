"""Unit tests for expense line-amount validation (F32) and dimension-scoped
period usage (F31)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.expense import (
    ExpenseClaimStatus,
    LimitPeriodType,
    LimitResultType,
    LimitScopeType,
)
from app.services.common import ValidationError
from app.services.expense.expense_service import ExpenseService
from app.services.expense.limit_service import EvaluationResult, ExpenseLimitService


# ---------------------------------------------------------------------------
# F32 — reject non-positive line amounts
# ---------------------------------------------------------------------------


def _draft_claim():
    claim = MagicMock()
    claim.status = ExpenseClaimStatus.DRAFT
    claim.total_claimed_amount = Decimal("0")
    return claim


def _category():
    category = MagicMock()
    category.max_amount_per_claim = None
    category.expense_account_id = uuid4()
    return category


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-5.00"), None])
def test_add_claim_item_rejects_non_positive_amount(bad_amount):
    svc = ExpenseService(MagicMock())
    org_id = uuid4()
    claim_id = uuid4()

    with (
        patch.object(svc, "get_claim", return_value=_draft_claim()),
        patch.object(svc, "get_category", return_value=_category()),
    ):
        with pytest.raises(ValidationError, match="greater than zero"):
            svc.add_claim_item(
                org_id,
                claim_id,
                expense_date=date(2026, 3, 1),
                category_id=uuid4(),
                description="Bad line",
                claimed_amount=bad_amount,
            )


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-1.00")])
def test_update_claim_item_rejects_non_positive_amount(bad_amount):
    svc = ExpenseService(MagicMock())
    org_id = uuid4()
    claim_id = uuid4()

    item = MagicMock()
    item.claimed_amount = Decimal("10.00")

    db = svc.db
    db.scalar.return_value = item

    with (
        patch.object(svc, "get_claim", return_value=_draft_claim()),
        patch.object(svc, "get_category", return_value=_category()),
    ):
        with pytest.raises(ValidationError, match="greater than zero"):
            svc.update_claim_item(
                org_id,
                claim_id=claim_id,
                item_id=uuid4(),
                expense_date=date(2026, 3, 1),
                category_id=uuid4(),
                description="Bad update",
                claimed_amount=bad_amount,
            )


def test_add_claim_item_accepts_positive_amount():
    svc = ExpenseService(MagicMock())
    org_id = uuid4()
    claim_id = uuid4()
    svc.db.scalar.return_value = 0  # max(sequence) query

    with (
        patch.object(svc, "get_claim", return_value=_draft_claim()),
        patch.object(svc, "get_category", return_value=_category()),
    ):
        item = svc.add_claim_item(
            org_id,
            claim_id,
            expense_date=date(2026, 3, 1),
            category_id=uuid4(),
            description="Good line",
            claimed_amount=Decimal("25.00"),
        )
    assert item.claimed_amount == Decimal("25.00")


def test_live_limit_query_includes_employee_employment_type_scope() -> None:
    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    svc = ExpenseLimitService(db)
    employment_type_id = uuid4()
    employee = MagicMock(
        employee_id=uuid4(),
        department_id=None,
        grade_id=None,
        designation_id=None,
        employment_type_id=employment_type_id,
    )

    assert svc.get_applicable_rules(uuid4(), employee, date(2026, 8, 28)) == []

    statement = db.scalars.call_args.args[0]
    rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "EMPLOYMENT_TYPE" in rendered
    assert str(employment_type_id) in rendered


def test_live_claim_evaluation_applies_employment_type_block_rule() -> None:
    db = MagicMock()
    svc = ExpenseLimitService(db)
    employment_type_id = uuid4()
    employee = MagicMock(
        employee_id=uuid4(),
        department_id=None,
        grade_id=None,
        designation_id=None,
        employment_type_id=employment_type_id,
    )
    rule = MagicMock(scope_type=LimitScopeType.EMPLOYMENT_TYPE)
    db.get.return_value = employee
    db.scalars.return_value.all.return_value = [rule]
    claim = MagicMock(
        organization_id=uuid4(),
        employee_id=employee.employee_id,
        claim_date=date(2026, 8, 28),
        total_claimed_amount=Decimal("1000.00"),
    )
    blocked = EvaluationResult(
        result=LimitResultType.BLOCKED,
        message="Employment Type limit exceeded",
        triggered_rule=rule,
    )

    with patch.object(svc, "_evaluate_rule", return_value=blocked) as evaluate:
        result = svc.evaluate_claim(claim, preview_only=True)

    assert result.result == LimitResultType.BLOCKED
    evaluate.assert_called_once_with(claim, rule, employee)


# ---------------------------------------------------------------------------
# F31 — dimension-scoped period usage
# ---------------------------------------------------------------------------


def test_calculate_period_usage_unscoped_sums_claim_totals():
    db = MagicMock()
    db.execute.return_value.one.return_value = (Decimal("300.00"), 3)
    svc = ExpenseLimitService(db)

    total, count = svc.calculate_period_usage(
        uuid4(),
        uuid4(),
        LimitPeriodType.MONTH,
        date(2026, 3, 1),
        date(2026, 3, 31),
    )

    assert total == Decimal("300.00")
    assert count == 3
    # Unscoped path must not join the item table.
    rendered = str(db.execute.call_args.args[0])
    assert "expense_claim_item" not in rendered


def test_calculate_period_usage_category_scoped_joins_items():
    db = MagicMock()
    db.execute.return_value.one.return_value = (Decimal("50.00"), 1)
    svc = ExpenseLimitService(db)
    category_id = uuid4()

    total, count = svc.calculate_period_usage(
        uuid4(),
        uuid4(),
        LimitPeriodType.MONTH,
        date(2026, 3, 1),
        date(2026, 3, 31),
        category_ids=[category_id],
    )

    assert total == Decimal("50.00")
    assert count == 1
    rendered = str(db.execute.call_args.args[0])
    # Scoped path sums line items and filters on category.
    assert "expense_claim_item" in rendered
    assert "category_id" in rendered


def test_calculate_period_usage_cost_center_scoped_joins_items():
    db = MagicMock()
    db.execute.return_value.one.return_value = (Decimal("0"), 0)
    svc = ExpenseLimitService(db)

    svc.calculate_period_usage(
        uuid4(),
        uuid4(),
        LimitPeriodType.MONTH,
        date(2026, 3, 1),
        date(2026, 3, 31),
        cost_center_ids=[uuid4()],
    )

    rendered = str(db.execute.call_args.args[0])
    assert "expense_claim_item" in rendered
    assert "cost_center_id" in rendered
