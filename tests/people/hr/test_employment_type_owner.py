from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from dotmac_people import EmploymentTypeRecord, NotFound

from app.services.common import PaginationParams, ValidationError
from app.services.people.hr import employment_types as owner
from app.services.people.hr.employment_types import (
    EmploymentTypeService,
    _EmploymentTypeProjector,
    _LegacyEmploymentTypeSnapshot,
)
from app.services.people.hr.errors import EmploymentTypeNotFoundError
from app.services.people.hr.organization_types import (
    EmploymentTypeCreateData,
    EmploymentTypeFilters,
    EmploymentTypeUpdateData,
)


def _db(organization_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        info={
            "organization_id": organization_id,
            "tenant_id": organization_id,
        }
    )


def _record(
    organization_id: UUID,
    *,
    row_id: UUID | None = None,
    code: str = "PERMANENT",
    name: str = "Permanent",
    description: str | None = "Open ended",
    active: bool = True,
) -> EmploymentTypeRecord:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    return EmploymentTypeRecord(
        id=row_id or uuid4(),
        tenant_id=organization_id,
        code=code,
        name=name,
        description=description,
        is_active=active,
        created_at=now,
        updated_at=now,
    )


def _snapshot(record: EmploymentTypeRecord) -> _LegacyEmploymentTypeSnapshot:
    return _LegacyEmploymentTypeSnapshot(
        employment_type_id=record.id,
        organization_id=record.tenant_id,
        type_code=record.code,
        type_name=record.name,
        description=record.description,
        is_active=record.is_active,
        created_at=record.created_at,
        updated_at=record.updated_at,
        created_by_id=None,
        updated_by_id=None,
    )


def test_service_refuses_a_partially_or_foreign_primed_session() -> None:
    organization_id = uuid4()
    with pytest.raises(RuntimeError, match="canonically primed"):
        EmploymentTypeService(SimpleNamespace(info={}), organization_id)
    with pytest.raises(RuntimeError, match="canonically primed"):
        EmploymentTypeService(
            SimpleNamespace(
                info={
                    "organization_id": organization_id,
                    "tenant_id": uuid4(),
                }
            ),
            organization_id,
        )


def test_list_completes_module_pages_before_legacy_name_order_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    records = [
        _record(organization_id, code=f"T{index:03}", name=f"Type {index:03}")
        for index in range(201)
    ]
    records[0] = _record(organization_id, code="Z", name="Zulu")
    records[200] = _record(organization_id, code="A", name="Alpha")
    calls: list[tuple[int, int]] = []

    def fake_list(db, *, scope, query):
        calls.append((query.offset, query.limit))
        return SimpleNamespace(
            items=tuple(records[query.offset : query.offset + query.limit]),
            total=len(records),
        )

    monkeypatch.setattr(owner, "list_employment_types", fake_list)
    result = EmploymentTypeService(
        _db(organization_id), organization_id
    ).list_employment_types(
        EmploymentTypeFilters(search="a"),
        PaginationParams(offset=0, limit=1000),
    )

    assert calls == [(0, 200), (200, 200)]
    assert result.items[0].type_name == "Alpha"
    assert result.limit == 1000
    assert all(item.organization_id == organization_id for item in result.items)


def test_module_not_found_is_translated_to_the_existing_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    row_id = uuid4()

    def absent(*args, **kwargs):
        raise NotFound("absent")

    monkeypatch.setattr(owner, "read_employment_type", absent)
    service = EmploymentTypeService(_db(organization_id), organization_id)
    with pytest.raises(EmploymentTypeNotFoundError) as refused:
        service.get_employment_type(row_id)
    assert refused.value.employment_type_id == row_id


def test_create_runs_module_command_then_projects_in_the_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    record = _record(organization_id)
    db = _db(organization_id)
    captured: list[object] = []

    def register(observed_db, *, scope, command):
        assert observed_db is db
        captured.append(command)
        return record

    monkeypatch.setattr(owner, "register_employment_type", register)
    service = EmploymentTypeService(db, organization_id)
    service._projector = SimpleNamespace(
        project=lambda observed: captured.append(observed) or "projected"
    )

    result = service.create_employment_type(
        EmploymentTypeCreateData(
            type_code="permanent",
            type_name="Permanent",
            description=None,
        )
    )

    assert result == "projected"
    assert captured[-1] is record
    assert not hasattr(db, "commit")


def test_update_can_explicitly_clear_description_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    current = _record(organization_id)
    revised = _record(organization_id, row_id=current.id, description=None)
    captured: list[object] = []

    monkeypatch.setattr(owner, "read_employment_type", lambda *a, **k: current)

    def revise(db, *, scope, command):
        captured.append(command)
        return revised

    monkeypatch.setattr(owner, "revise_employment_type", revise)
    service = EmploymentTypeService(_db(organization_id), organization_id)
    service._projector = SimpleNamespace(project=lambda record: record)

    result = service.update_employment_type(
        current.id,
        EmploymentTypeUpdateData(description=None, description_is_set=True),
    )

    assert captured[0].description is None
    assert result is revised


def test_projector_preserves_identity_timestamps_and_audit_actor() -> None:
    organization_id = uuid4()
    actor_id = uuid4()
    record = _record(organization_id, code="CONTRACT", name="Contract")

    class FakeSession:
        def __init__(self) -> None:
            self.row = None
            self.flushes = 0

        def scalar(self, statement):
            return self.row

        def add(self, row) -> None:
            self.row = row

        def flush(self) -> None:
            self.flushes += 1

    db = FakeSession()
    view = _EmploymentTypeProjector(
        db,
        organization_id,
        SimpleNamespace(id=actor_id),
    ).project(record)

    assert db.flushes == 1
    assert db.row.employment_type_id == record.id
    assert db.row.organization_id == organization_id
    assert db.row.created_at == record.created_at
    assert db.row.updated_at == record.updated_at
    assert db.row.created_by_id == actor_id
    assert view.created_by_id == actor_id


def test_repair_refuses_every_legacy_only_id_before_projecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    module_record = _record(organization_id)
    legacy_only = _record(organization_id)
    service = EmploymentTypeService(_db(organization_id), organization_id)
    monkeypatch.setattr(service, "_scan_module_records", lambda: (module_record,))
    monkeypatch.setattr(
        service,
        "_scan_legacy_projection",
        lambda: {
            module_record.id: _snapshot(module_record),
            legacy_only.id: _snapshot(legacy_only),
        },
    )
    projected: list[EmploymentTypeRecord] = []
    service._projector = SimpleNamespace(project=projected.append)

    with pytest.raises(ValidationError, match="refusing every write") as refused:
        service.repair_compatibility_projection()
    assert str(module_record.id) not in str(refused.value)
    assert str(legacy_only.id) in str(refused.value)
    assert projected == []


def test_repair_projects_only_missing_or_changed_rows_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    unchanged = _record(organization_id, code="A", name="Alpha")
    changed = _record(organization_id, code="B", name="Beta")
    changed_before = replace(_snapshot(changed), type_name="Old beta")
    before = {
        unchanged.id: _snapshot(unchanged),
        changed.id: changed_before,
    }
    after = {
        unchanged.id: _snapshot(unchanged),
        changed.id: _snapshot(changed),
    }
    scans = iter((before, after))
    service = EmploymentTypeService(_db(organization_id), organization_id)
    monkeypatch.setattr(service, "_scan_module_records", lambda: (unchanged, changed))
    monkeypatch.setattr(service, "_scan_legacy_projection", lambda: next(scans))
    projected: list[EmploymentTypeRecord] = []
    service._projector = SimpleNamespace(project=projected.append)

    assert service.repair_compatibility_projection() == 1
    assert projected == [changed]

    stable_scans = iter((after, after))
    monkeypatch.setattr(service, "_scan_legacy_projection", lambda: next(stable_scans))
    projected.clear()
    assert service.repair_compatibility_projection() == 0
    assert projected == []
