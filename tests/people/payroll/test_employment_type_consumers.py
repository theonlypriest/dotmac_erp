from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from app.services.people.payroll.payroll_service import PayrollService
from app.services.people.payroll.web.run_web import RunWebService


def test_run_filter_uses_complete_active_people_catalogue(monkeypatch):
    organization_id = uuid4()
    records = [
        SimpleNamespace(
            employment_type_id=uuid4(),
            type_name=f"Type {index:03d}",
        )
        for index in range(500)
    ]

    class FakeEmploymentTypeService:
        def __init__(self, db, requested_organization_id):
            assert db is session
            assert requested_organization_id == organization_id

        def iter_all(self, active=None):
            assert active is True
            return tuple(records)

    monkeypatch.setattr(
        "app.services.people.payroll.web.run_web.EmploymentTypeService",
        FakeEmploymentTypeService,
    )
    session = MagicMock()

    result = RunWebService._list_employment_types_for_filter(session, organization_id)

    assert result == records
    assert len(result) == 500


def test_payroll_entry_create_and_update_require_active_people_ids(monkeypatch):
    organization_id = uuid4()
    create_type_id = uuid4()
    update_type_id = uuid4()
    required: list[UUID] = []

    class FakeEmploymentTypeService:
        def __init__(self, db, requested_organization_id):
            assert db is session
            assert requested_organization_id == organization_id

        def require_active(self, employment_type_id):
            required.append(employment_type_id)
            return SimpleNamespace(employment_type_id=employment_type_id)

    monkeypatch.setattr(
        "app.services.people.payroll.payroll_service.EmploymentTypeService",
        FakeEmploymentTypeService,
    )
    monkeypatch.setattr(
        "app.services.finance.common.numbering.SyncNumberingService.generate_next_number",
        lambda *_args, **_kwargs: "PAY-0001",
    )
    session = MagicMock()
    service = PayrollService(session)
    monkeypatch.setattr(
        service,
        "_resolve_currency_code",
        lambda *_args, **_kwargs: "NGN",
    )

    entry = service.create_payroll_entry(
        organization_id,
        posting_date=date(2026, 8, 28),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        employment_type_id=create_type_id,
    )
    monkeypatch.setattr(service, "get_payroll_entry", lambda *_args: entry)
    service.update_payroll_entry(
        organization_id,
        entry.entry_id,
        employment_type_id=update_type_id,
    )

    assert required == [create_type_id, update_type_id]
    assert entry.employment_type_id == update_type_id
