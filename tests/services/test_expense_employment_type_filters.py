from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.expense.approval_service import ExpenseApprovalService


def test_approval_dimension_filters_match_canonical_people_code(monkeypatch):
    organization_id = uuid4()
    employment_type_id = uuid4()
    unfiltered = SimpleNamespace(dimension_filters={})
    contract = SimpleNamespace(
        dimension_filters={"employment_types": [" contract ", "NYSC"]}
    )
    permanent = SimpleNamespace(dimension_filters={"employment_types": ["PERMANENT"]})
    db = MagicMock()
    db.scalars.return_value.all.return_value = [unfiltered, contract, permanent]

    class FakeEmploymentTypeService:
        def __init__(self, requested_db, requested_organization_id):
            assert requested_db is db
            assert requested_organization_id == organization_id

        def get_employment_type(self, requested_id):
            assert requested_id == employment_type_id
            return SimpleNamespace(type_code="CONTRACT")

    monkeypatch.setattr(
        "app.services.expense.approval_service.EmploymentTypeService",
        FakeEmploymentTypeService,
    )

    rules = ExpenseApprovalService(db).filter_limits_by_employment_type(
        organization_id,
        SimpleNamespace(employment_type_id=employment_type_id),
    )

    assert rules == [unfiltered, contract]


def test_approval_dimension_filter_does_not_admit_unclassified_employee():
    organization_id = uuid4()
    unfiltered = SimpleNamespace(dimension_filters={})
    filtered = SimpleNamespace(dimension_filters={"employment_types": ["CONTRACT"]})
    db = MagicMock()
    db.scalars.return_value.all.return_value = [unfiltered, filtered]

    rules = ExpenseApprovalService(db).filter_limits_by_employment_type(
        organization_id,
        SimpleNamespace(employment_type_id=None),
    )

    assert rules == [unfiltered]
