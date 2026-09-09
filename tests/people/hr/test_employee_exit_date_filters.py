from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from app.services.common import PaginationParams
from app.services.people.hr.employee_types import EmployeeFilters
from app.services.people.hr.employees import EmployeeService


def test_employee_exit_date_filters_apply_inclusive_boundaries(monkeypatch):
    captured: dict[str, object] = {}

    def _capture_paginate(db, stmt, pagination, count_column=None):
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        captured["sql"] = str(compiled)
        captured["params"] = compiled.params
        return SimpleNamespace(
            items=[],
            total=0,
            total_pages=0,
            has_prev=False,
            has_next=False,
        )

    monkeypatch.setattr("app.services.people.hr.employees.paginate", _capture_paginate)

    EmployeeService(SimpleNamespace(), uuid4()).list_employees(
        EmployeeFilters(
            include_archived=True,
            include_deleted=True,
            date_of_leaving_from=date(2026, 4, 1),
            date_of_leaving_to=date(2026, 4, 30),
        ),
        PaginationParams(limit=25),
    )

    sql = str(captured["sql"])
    params = captured["params"]

    assert "date_of_leaving >= :date_of_leaving_1" in sql
    assert "date_of_leaving <= :date_of_leaving_2" in sql
    assert params["date_of_leaving_1"] == date(2026, 4, 1)
    assert params["date_of_leaving_2"] == date(2026, 4, 30)
