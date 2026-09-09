from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.people.hr.web.dashboard_web import PeopleDashboardService


def test_dashboard_omits_null_coalesces_names_and_orders_by_count(monkeypatch):
    organization_id = uuid4()
    contract_primary_id = uuid4()
    contract_secondary_id = uuid4()
    casual_id = uuid4()
    permanent_id = uuid4()
    db = MagicMock()
    db.execute.return_value.all.return_value = [
        (contract_primary_id, 7),
        (permanent_id, 8),
        (contract_secondary_id, 2),
        (casual_id, 8),
        (None, 2),
    ]

    class FakeEmploymentTypeService:
        def __init__(self, requested_db, requested_organization_id):
            assert requested_db is db
            assert requested_organization_id == organization_id

        def iter_all(self):
            return (
                SimpleNamespace(
                    employment_type_id=contract_primary_id,
                    type_name="Contract",
                ),
                SimpleNamespace(
                    employment_type_id=contract_secondary_id,
                    type_name="Contract",
                ),
                SimpleNamespace(employment_type_id=casual_id, type_name="Casual"),
                SimpleNamespace(
                    employment_type_id=permanent_id,
                    type_name="Permanent",
                ),
            )

    monkeypatch.setattr(
        "app.services.people.hr.web.dashboard_web.EmploymentTypeService",
        FakeEmploymentTypeService,
    )

    result = PeopleDashboardService()._get_employment_type_distribution(
        db, organization_id
    )

    assert result == [
        {"type": "Contract", "count": 9},
        {"type": "Casual", "count": 8},
        {"type": "Permanent", "count": 8},
    ]
    statement = db.execute.call_args.args[0]
    assert "employment_type_id" in str(statement)
    assert "IS NOT NULL" in str(statement)
    assert "JOIN hr.employment_type" not in str(statement)
