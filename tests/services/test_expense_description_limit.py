from __future__ import annotations

import pytest

from app.services.common import ValidationError
from app.services.expense.service_claims import ExpenseClaimMixin


def test_expense_item_description_accepts_the_database_limit() -> None:
    description = "x" * 500

    assert ExpenseClaimMixin._validate_item_description(description) == description


@pytest.mark.parametrize("description", ["x" * 501, "   ", None])
def test_expense_item_description_rejects_invalid_values_before_flush(
    description: object,
) -> None:
    with pytest.raises(ValidationError):
        ExpenseClaimMixin._validate_item_description(description)
