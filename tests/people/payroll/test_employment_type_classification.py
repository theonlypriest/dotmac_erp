from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.people.payroll.employment_type_classification import (
    classify_payroll_employment_type,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CLASSIFICATION_CONSUMERS = (
    REPO_ROOT / "app/services/people/payroll/salary_slip_service.py",
    REPO_ROOT / "app/services/people/payroll/data_completeness.py",
    REPO_ROOT / "app/services/people/payroll/web/slip_web.py",
    REPO_ROOT / "app/services/people/payroll/paye_export.py",
)


def test_classification_uses_people_code_and_never_legacy_relationship(monkeypatch):
    organization_id = uuid4()
    employment_type_id = uuid4()
    calls: list[tuple[object, object]] = []

    class FakeEmploymentTypeService:
        def __init__(self, db, requested_organization_id):
            calls.append((db, requested_organization_id))

        def get_employment_type(self, requested_id):
            assert requested_id == employment_type_id
            return SimpleNamespace(type_code=" contract ")

    monkeypatch.setattr(
        "app.services.people.payroll.employment_type_classification.EmploymentTypeService",
        FakeEmploymentTypeService,
    )
    db = MagicMock()

    classification = classify_payroll_employment_type(
        db,
        organization_id=organization_id,
        employment_type_id=employment_type_id,
    )

    assert calls == [(db, organization_id)]
    assert classification.code == "CONTRACT"
    assert classification.is_contract is True
    assert classification.is_contract_staff(structure_name="General Staff") is True
    assert classification.is_permanent is False


def test_structure_override_and_permanent_codes_are_payroll_owned(monkeypatch):
    class FakeEmploymentTypeService:
        def __init__(self, _db, _organization_id):
            pass

        def get_employment_type(self, _employment_type_id):
            return SimpleNamespace(type_code="FULL_TIME")

    monkeypatch.setattr(
        "app.services.people.payroll.employment_type_classification.EmploymentTypeService",
        FakeEmploymentTypeService,
    )
    classification = classify_payroll_employment_type(
        MagicMock(),
        organization_id=uuid4(),
        employment_type_id=uuid4(),
    )

    assert classification.is_permanent is True
    assert classification.is_contract is False
    assert classification.is_contract_staff(structure_name="Contract Staff") is True


def test_payroll_classification_consumers_do_not_read_legacy_relationship():
    for path in CLASSIFICATION_CONSUMERS:
        source = path.read_text()
        assert "app.models.people.hr.employment_type" not in source
        assert re.search(r"employee\.employment_type(?!_id)", source) is None
        assert re.search(r"Employee\.employment_type\b", source) is None
