"""Regression coverage for the global People model registry."""

from app.db import Base
from app.models.people.attendance import Attendance


def test_attendance_scheduling_foreign_keys_resolve_from_people_registry() -> None:
    target_tables = {
        foreign_key.column.table.fullname
        for foreign_key in Attendance.__table__.foreign_keys
    }

    assert "scheduling.shift_schedule" in Base.metadata.tables
    assert "scheduling.work_schedule" in Base.metadata.tables
    assert "scheduling.shift_schedule" in target_tables
    assert "scheduling.work_schedule" in target_tables
