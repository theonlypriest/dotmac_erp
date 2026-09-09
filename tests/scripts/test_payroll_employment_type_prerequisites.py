from __future__ import annotations

from contextlib import nullcontext
import re
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import seed_payroll_from_excel as seed_payroll

REPO_ROOT = Path(__file__).resolve().parents[2]
ADD_MISSING = REPO_ROOT / "scripts" / "add_missing_contract_staff.py"
SEED_PAYROLL = REPO_ROOT / "scripts" / "seed_payroll_from_excel.py"


def test_payroll_scripts_require_explicit_tenant_sessions_and_never_create_types():
    for path in (ADD_MISSING, SEED_PAYROLL):
        source = path.read_text()
        assert (
            'parser.add_argument("--organization-id", type=UUID, required=True)'
            in source
        )
        assert "with session_for_org(" in source
        assert "SessionLocal" not in source
        assert "get_org_id" not in source
        assert "get_or_create_employment_type" not in source
        assert "app.models.people.hr.employment_type" not in source


def test_seed_resolves_every_required_active_type_before_destructive_clear():
    source = SEED_PAYROLL.read_text()
    preflight = source.index('require_active_by_code("PERMANENT")')
    assert source.index('require_active_by_code("CONTRACT")') > preflight
    assert source.index('require_active_by_code("NYSC")') > preflight
    assert source.index("clear_payroll_data(db, org_id)", preflight) > preflight
    assert source.count("db.commit()") == 1
    assert source.index("db.commit()") > source.index(
        "clear_payroll_data(db, org_id)", preflight
    )


def test_late_seed_failure_rolls_back_the_destructive_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    organization_id = uuid4()
    input_file = tmp_path / "payroll.xlsx"
    input_file.touch()

    class TransactionalDb:
        def __init__(self) -> None:
            self.committed_state = "payroll-intact"
            self.pending_state = self.committed_state
            self.commit_calls = 0
            self.rollback_calls = 0
            self.delete_statements: list[str] = []

        def execute(self, statement, _parameters=None):
            sql = str(statement)
            if "DELETE FROM" in sql.upper():
                self.delete_statements.append(sql)
                self.pending_state = "payroll-cleared"
            return SimpleNamespace()

        def add(self, _record) -> None:
            return None

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            self.commit_calls += 1
            self.committed_state = self.pending_state

        def rollback(self) -> None:
            self.rollback_calls += 1
            self.pending_state = self.committed_state

    db = TransactionalDb()

    class EmploymentTypes:
        def __init__(self, observed_db, scoped_organization_id) -> None:
            assert observed_db is db
            assert scoped_organization_id == organization_id

        def require_active_by_code(self, _code: str) -> SimpleNamespace:
            return SimpleNamespace(employment_type_id=uuid4())

    class Batch:
        def __init__(self, **_kwargs) -> None:
            self.id = uuid4()

    def fail_after_clear(*_args, **_kwargs) -> None:
        raise RuntimeError("late payroll rebuild failure")

    monkeypatch.setattr(seed_payroll, "EXCEL_PATH", input_file)
    monkeypatch.setattr(
        seed_payroll,
        "session_for_org",
        lambda observed: (
            nullcontext(db)
            if observed == organization_id
            else pytest.fail("wrong organization session")
        ),
    )
    monkeypatch.setattr(seed_payroll, "EmploymentTypeService", EmploymentTypes)
    monkeypatch.setattr(seed_payroll, "get_admin_user_id", lambda _db: uuid4())
    monkeypatch.setattr(seed_payroll, "parse_excel_data", lambda _path: ([], []))
    monkeypatch.setattr(seed_payroll, "get_file_checksum", lambda _path: "checksum")
    monkeypatch.setattr(seed_payroll, "BatchOperation", Batch)
    monkeypatch.setattr(seed_payroll, "create_components", fail_after_clear)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_payroll_from_excel.py",
            "--organization-id",
            str(organization_id),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        seed_payroll.main()

    assert raised.value.code == 1
    assert db.delete_statements
    assert db.commit_calls == 0
    assert db.rollback_calls == 1
    assert db.pending_state == "payroll-intact"
    assert db.committed_state == "payroll-intact"


def test_add_missing_contract_staff_preflights_contract_before_employee_creation():
    source = ADD_MISSING.read_text()
    match = re.search(r'require_active_by_code\(\s*"CONTRACT"\s*\)', source)
    assert match is not None
    preflight = match.start()
    mutation = source.index("create_employee(", preflight)
    assert preflight < mutation
