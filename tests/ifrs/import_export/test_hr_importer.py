from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.people.hr import PositionAssignment
from app.services.finance.import_export.base import build_alias_map
from app.services.people.hr.import_export import (
    EmployeeImporter,
    EmploymentTypeImporter,
)
from app.services.people.hr.errors import EmploymentTypeNotFoundError
from app.services.people.hr.web.import_web import HrImportWebService


def test_employee_importer_creates_person(import_config, mock_db):
    mock_db.scalar.return_value = None

    importer = EmployeeImporter(mock_db, import_config)
    row = {
        "employee_code": "EMP-001",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "work_email": "ada@example.com",
        "date_of_joining": date(2024, 1, 15),
    }

    employee = importer.create_entity(row)

    assert employee.employee_code == "EMP-001"
    assert employee.person_id is not None
    assert employee.date_of_joining == date(2024, 1, 15)
    mock_db.add.assert_called_once()
    mock_db.flush.assert_called_once()


def test_employee_importer_assigns_position_code(import_config, mock_db):
    position_id = uuid4()
    mock_db.scalar.side_effect = [
        None,  # person lookup by email
        SimpleNamespace(position_id=position_id),  # position
        None,  # existing position assignment
    ]

    importer = EmployeeImporter(mock_db, import_config)
    employee = importer.create_entity(
        {
            "employee_code": "EMP-001",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "work_email": "ada@example.com",
            "date_of_joining": date(2024, 1, 15),
            "position_code": "ENG-001",
        }
    )

    assignment = next(
        call.args[0]
        for call in mock_db.add.call_args_list
        if isinstance(call.args[0], PositionAssignment)
    )
    assert assignment.employee_id == employee.employee_id
    assert assignment.position_id == position_id
    assert assignment.start_date == date(2024, 1, 15)


def test_employee_importer_rejects_unknown_position_code(import_config, mock_db):
    mock_db.scalar.side_effect = [
        None,  # person lookup by email
        None,  # position
    ]

    importer = EmployeeImporter(mock_db, import_config)

    with pytest.raises(ValueError, match="Position not found: ENG-404"):
        importer.create_entity(
            {
                "employee_code": "EMP-001",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "work_email": "ada@example.com",
                "date_of_joining": date(2024, 1, 15),
                "position_code": "ENG-404",
            }
        )


def test_employee_import_wizard_exposes_position_fields():
    columns = HrImportWebService.get_entity_columns("employees")

    assert "Position Code" in columns["optional"]
    assert "Reports To Code" in columns["optional"]
    assert "Expense Approver Code" in columns["optional"]
    assert "Cost Center Code" in columns["optional"]


def test_employee_import_template_includes_position_columns():
    csv_content = HrImportWebService.build_csv_template("employees")
    header, sample = csv_content.splitlines()[:2]

    assert "Position Code" in header
    assert "Reports To Code" in header
    assert "Expense Approver Code" in header
    assert "ENG-SWE-001" in sample


def test_employee_import_alias_map_supports_position_fields():
    alias_map = build_alias_map()

    assert alias_map["position_code"] == "position_code"
    assert alias_map["position_id"] == "position_code"
    assert alias_map["manager_code"] == "reports_to_code"
    assert alias_map["expense_approver_code"] == "expense_approver_code"


def test_employment_type_importer_delegates_writes_to_owner(
    import_config, mock_db, monkeypatch
):
    employment_type_id = uuid4()
    view = SimpleNamespace(employment_type_id=employment_type_id)
    create_calls = []

    class _Owner:
        def __init__(self, db, organization_id, principal):
            assert db is mock_db
            assert organization_id == import_config.organization_id
            assert principal.id == import_config.user_id

        def create_employment_type(self, payload):
            create_calls.append(payload)
            return view

    monkeypatch.setattr(
        "app.services.people.hr.import_export.EmploymentTypeService", _Owner
    )
    importer = EmploymentTypeImporter(mock_db, import_config)

    created = importer.create_entity(
        {
            "type_code": "CONTRACT",
            "type_name": "Contract",
            "description": "Fixed term",
            "is_active": True,
        }
    )
    importer._commit_batch([created])

    assert created is view
    assert create_calls[0].type_code == "CONTRACT"
    assert importer.result.imported_ids == [employment_type_id]
    mock_db.add.assert_not_called()


def test_employee_importer_resolves_active_employment_type_code(
    import_config, mock_db, monkeypatch
):
    employment_type_id = uuid4()

    class _Owner:
        def __init__(self, db, organization_id):
            assert db is mock_db
            assert organization_id == import_config.organization_id

        def require_active_by_code(self, code):
            assert code == "FULL_TIME"
            return SimpleNamespace(employment_type_id=employment_type_id)

    monkeypatch.setattr(
        "app.services.people.hr.import_export.EmploymentTypeService", _Owner
    )

    importer = EmployeeImporter(mock_db, import_config)

    assert importer._resolve_employment_type_id("FULL_TIME") == employment_type_id


def test_employee_importer_treats_inactive_employment_type_as_unresolved(
    import_config, mock_db, monkeypatch
):
    class _Owner:
        def __init__(self, db, organization_id):
            assert db is mock_db
            assert organization_id == import_config.organization_id

        def require_active_by_code(self, code):
            raise EmploymentTypeNotFoundError(message=f"Inactive: {code}")

    monkeypatch.setattr(
        "app.services.people.hr.import_export.EmploymentTypeService", _Owner
    )

    importer = EmployeeImporter(mock_db, import_config)

    assert importer._resolve_employment_type_id("CONTRACT") is None
