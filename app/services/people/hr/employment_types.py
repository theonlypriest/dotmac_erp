"""ERP assembly owner for the composed People Employment Type slice.

``mod_people.employment_types`` owns the catalogue lifecycle.  The retained
``hr.employment_type`` relation is only a synchronous compatibility projection
for ERP foreign keys that have not moved yet.  :class:`_EmploymentTypeProjector`
is the only writer of that projection after activation.

Every method uses the caller's canonically primed :class:`~sqlalchemy.orm.Session`
and flushes only.  Request, task, and CLI adapters retain transaction authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol
from uuid import UUID

from dotmac_people import (
    ActivateEmploymentType,
    Conflict,
    CreateEmploymentType,
    DeactivateEmploymentType,
    EmploymentTypeQuery,
    EmploymentTypeRecord,
    InvalidLifecycle,
    NotFound,
    ReviseEmploymentType,
    activate_employment_type,
    deactivate_employment_type,
    list_employment_types,
    read_employment_type,
    register_employment_type,
    require_active_employment_type,
    revise_employment_type,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.people.hr.employment_type import EmploymentType
from app.services.common import PaginatedResult, PaginationParams, ValidationError
from app.services.people.hr.errors import EmploymentTypeNotFoundError
from app.services.people.hr.organization_types import (
    EmploymentTypeCreateData,
    EmploymentTypeFilters,
    EmploymentTypeUpdateData,
)
from app.tenancy import OrganizationTenantContext

_MODULE_PAGE_SIZE = 200


class _Principal(Protocol):
    @property
    def id(self) -> UUID: ...


@dataclass(frozen=True, slots=True)
class EmploymentTypeView:
    """Immutable legacy-shaped DTO backed by the module-owned record."""

    employment_type_id: UUID
    organization_id: UUID
    type_code: str
    type_name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None
    created_by_id: UUID | None = None
    updated_by_id: UUID | None = None

    @property
    def id(self) -> UUID:
        return self.employment_type_id

    @property
    def tenant_id(self) -> UUID:
        return self.organization_id

    @property
    def code(self) -> str:
        return self.type_code

    @property
    def name(self) -> str:
        return self.type_name

    @property
    def description_is_set(self) -> bool:
        """A materialized authoritative record always carries this decision."""
        return True


@dataclass(frozen=True, slots=True)
class _LegacyEmploymentTypeSnapshot:
    employment_type_id: UUID
    organization_id: UUID
    type_code: str
    type_name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None
    created_by_id: UUID | None
    updated_by_id: UUID | None


def _view(
    organization_id: UUID,
    record: EmploymentTypeRecord,
    *,
    created_by_id: UUID | None = None,
    updated_by_id: UUID | None = None,
) -> EmploymentTypeView:
    if record.tenant_id != organization_id:
        raise RuntimeError("Employment Type record belongs to another tenant")
    return EmploymentTypeView(
        employment_type_id=record.id,
        organization_id=organization_id,
        type_code=record.code,
        type_name=record.name,
        description=record.description,
        is_active=record.is_active,
        created_at=record.created_at,
        updated_at=record.updated_at,
        created_by_id=created_by_id,
        updated_by_id=updated_by_id,
    )


def _legacy_snapshot(row: EmploymentType) -> _LegacyEmploymentTypeSnapshot:
    return _LegacyEmploymentTypeSnapshot(
        employment_type_id=row.employment_type_id,
        organization_id=row.organization_id,
        type_code=row.type_code,
        type_name=row.type_name,
        description=row.description,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by_id=row.created_by_id,
        updated_by_id=row.updated_by_id,
    )


def _projection_matches(
    legacy: _LegacyEmploymentTypeSnapshot, record: EmploymentTypeRecord
) -> bool:
    return (
        legacy.organization_id == record.tenant_id
        and legacy.type_code == record.code
        and legacy.type_name == record.name
        and legacy.description == record.description
        and legacy.is_active == record.is_active
        and legacy.created_at == record.created_at
        and legacy.updated_at == record.updated_at
    )


class _EmploymentTypeProjector:
    """The sole writer of ERP's retained Employment Type projection."""

    def __init__(
        self,
        db: Session,
        organization_id: UUID,
        principal: _Principal | None = None,
    ) -> None:
        # Keep the conventional ``db`` receiver name: the repository's writer
        # inventory deliberately recognizes ``db.add`` as evidence that this
        # projector is the sole compatibility-table writer.
        self.db = db
        self._organization_id = organization_id
        self._principal = principal

    def project(self, record: EmploymentTypeRecord) -> EmploymentTypeView:
        if record.tenant_id != self._organization_id:
            raise RuntimeError("refusing to project a foreign-tenant Employment Type")

        row = self.db.scalar(
            select(EmploymentType).where(EmploymentType.employment_type_id == record.id)
        )
        actor_id = self._principal.id if self._principal is not None else None
        if row is None:
            row = EmploymentType(
                employment_type_id=record.id,
                organization_id=self._organization_id,
                type_code=record.code,
                type_name=record.name,
                description=record.description,
                is_active=record.is_active,
                created_at=record.created_at,
                updated_at=record.updated_at,
                created_by_id=actor_id,
            )
            self.db.add(row)
            self.db.flush()
        else:
            if row.organization_id != self._organization_id:
                raise RuntimeError(
                    "refusing to overwrite a foreign-organization Employment Type "
                    f"projection with id {record.id}"
                )
            before = _legacy_snapshot(row)
            if not _projection_matches(before, record):
                row.type_code = record.code
                row.type_name = record.name
                row.description = record.description
                row.is_active = record.is_active
                row.created_at = record.created_at
                row.updated_at = record.updated_at
                row.updated_by_id = actor_id
                self.db.flush()

        return _view(
            self._organization_id,
            record,
            created_by_id=row.created_by_id,
            updated_by_id=row.updated_by_id,
        )


class EmploymentTypeService:
    """The one ERP assembly owner for Employment Type reads and commands."""

    def __init__(
        self,
        db: Session,
        organization_id: UUID,
        principal: _Principal | None = None,
    ) -> None:
        context = OrganizationTenantContext.for_organization(organization_id)
        if (
            db.info.get("organization_id") != context.organization_id
            or db.info.get("tenant_id") != context.tenant_id
        ):
            raise RuntimeError(
                "EmploymentTypeService requires a canonically primed tenant Session"
            )
        self.db = db
        self.organization_id = context.organization_id
        self.scope = context.tenant_scope
        self._projector = _EmploymentTypeProjector(db, organization_id, principal)

    @staticmethod
    def _translate_error(
        exc: Conflict | InvalidLifecycle | NotFound | TypeError | ValueError,
        employment_type_id: UUID | None = None,
    ) -> NoReturn:
        if isinstance(exc, NotFound):
            raise EmploymentTypeNotFoundError(employment_type_id) from exc
        if isinstance(exc, InvalidLifecycle):
            raise ValidationError(str(exc)) from exc
        raise ValidationError(str(exc)) from exc

    def _scan_module_records(self) -> tuple[EmploymentTypeRecord, ...]:
        offset = 0
        expected_total: int | None = None
        records: list[EmploymentTypeRecord] = []
        seen: set[UUID] = set()
        while expected_total is None or offset < expected_total:
            try:
                page = list_employment_types(
                    self.db,
                    scope=self.scope,
                    query=EmploymentTypeQuery(
                        offset=offset,
                        limit=_MODULE_PAGE_SIZE,
                    ),
                )
            except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
                self._translate_error(exc)
            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                raise ValidationError(
                    "authoritative Employment Type set changed during a complete scan"
                )
            if not page.items and offset < expected_total:
                raise ValidationError(
                    "authoritative Employment Type scan ended before its reported total"
                )
            for record in page.items:
                if record.tenant_id != self.organization_id:
                    raise RuntimeError(
                        "authoritative Employment Type scan returned another tenant"
                    )
                if record.id in seen:
                    raise ValidationError(
                        f"authoritative Employment Type scan repeated id {record.id}"
                    )
                seen.add(record.id)
                records.append(record)
            offset += len(page.items)
        if expected_total != len(records):
            raise ValidationError(
                "authoritative Employment Type scan count changed during paging"
            )
        return tuple(records)

    def iter_all(self, active: bool | None = None) -> tuple[EmploymentTypeView, ...]:
        """Return one complete, stable module scan, optionally filtered by state."""
        records = [
            record
            for record in self._scan_module_records()
            if active is None or record.is_active is active
        ]
        records.sort(
            key=lambda record: (record.name.casefold(), record.name, record.id)
        )
        return tuple(_view(self.organization_id, record) for record in records)

    def list_employment_types(
        self,
        filters: EmploymentTypeFilters | None = None,
        pagination: PaginationParams | None = None,
    ) -> PaginatedResult[EmploymentTypeView]:
        """Apply the legacy list contract after a complete authoritative scan."""
        filters = filters or EmploymentTypeFilters()
        pagination = pagination or PaginationParams()
        if pagination.offset < 0:
            raise ValidationError("Employment Type offset must be non-negative")
        if pagination.limit < 1:
            raise ValidationError("Employment Type limit must be positive")

        records = list(self._scan_module_records())
        if filters.is_active is not None:
            records = [
                record for record in records if record.is_active is filters.is_active
            ]
        if filters.search and filters.search.strip():
            term = filters.search.strip().casefold()
            records = [
                record
                for record in records
                if term in record.name.casefold() or term in record.code.casefold()
            ]
        records.sort(
            key=lambda record: (record.name.casefold(), record.name, record.id)
        )
        total = len(records)
        selected = records[pagination.offset : pagination.offset + pagination.limit]
        return PaginatedResult(
            items=[_view(self.organization_id, record) for record in selected],
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )

    def get_employment_type(self, employment_type_id: UUID) -> EmploymentTypeView:
        try:
            record = read_employment_type(
                self.db,
                scope=self.scope,
                employment_type_id=employment_type_id,
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc, employment_type_id)
        return _view(self.organization_id, record)

    def get_by_code(self, code: str) -> EmploymentTypeView | None:
        try:
            page = list_employment_types(
                self.db,
                scope=self.scope,
                query=EmploymentTypeQuery(code=code, limit=1),
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc)
        if not page.items:
            return None
        return _view(self.organization_id, page.items[0])

    def require_active(self, employment_type_id: UUID) -> EmploymentTypeView:
        try:
            record = require_active_employment_type(
                self.db,
                scope=self.scope,
                employment_type_id=employment_type_id,
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc, employment_type_id)
        return _view(self.organization_id, record)

    def require_active_by_code(self, code: str) -> EmploymentTypeView:
        try:
            page = list_employment_types(
                self.db,
                scope=self.scope,
                query=EmploymentTypeQuery(code=code, active=True, limit=1),
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc)
        if not page.items:
            raise EmploymentTypeNotFoundError(
                message=f"Active employment type code not found: {code.strip().upper()}"
            )
        return _view(self.organization_id, page.items[0])

    def create_employment_type(
        self, data: EmploymentTypeCreateData
    ) -> EmploymentTypeView:
        try:
            record = register_employment_type(
                self.db,
                scope=self.scope,
                command=CreateEmploymentType(
                    code=data.type_code,
                    name=data.type_name,
                    description=data.description,
                ),
            )
            if not data.is_active:
                record = deactivate_employment_type(
                    self.db,
                    scope=self.scope,
                    command=DeactivateEmploymentType(record.id),
                )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc)
        return self._projector.project(record)

    def update_employment_type(
        self,
        employment_type_id: UUID,
        data: EmploymentTypeUpdateData,
    ) -> EmploymentTypeView:
        try:
            record = read_employment_type(
                self.db,
                scope=self.scope,
                employment_type_id=employment_type_id,
            )
            if (
                data.type_code is not None
                or data.type_name is not None
                or data.description_is_set
            ):
                record = revise_employment_type(
                    self.db,
                    scope=self.scope,
                    command=ReviseEmploymentType(
                        employment_type_id=employment_type_id,
                        code=data.type_code
                        if data.type_code is not None
                        else record.code,
                        name=data.type_name
                        if data.type_name is not None
                        else record.name,
                        description=(
                            data.description
                            if data.description_is_set
                            else record.description
                        ),
                    ),
                )
            if data.is_active is not None and data.is_active is not record.is_active:
                if data.is_active:
                    record = activate_employment_type(
                        self.db,
                        scope=self.scope,
                        command=ActivateEmploymentType(employment_type_id),
                    )
                else:
                    record = deactivate_employment_type(
                        self.db,
                        scope=self.scope,
                        command=DeactivateEmploymentType(employment_type_id),
                    )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc, employment_type_id)
        return self._projector.project(record)

    def activate_employment_type(self, employment_type_id: UUID) -> EmploymentTypeView:
        try:
            record = activate_employment_type(
                self.db,
                scope=self.scope,
                command=ActivateEmploymentType(employment_type_id),
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc, employment_type_id)
        return self._projector.project(record)

    def deactivate_employment_type(
        self, employment_type_id: UUID
    ) -> EmploymentTypeView:
        try:
            record = deactivate_employment_type(
                self.db,
                scope=self.scope,
                command=DeactivateEmploymentType(employment_type_id),
            )
        except (Conflict, InvalidLifecycle, NotFound, TypeError, ValueError) as exc:
            self._translate_error(exc, employment_type_id)
        return self._projector.project(record)

    def _scan_legacy_projection(
        self,
    ) -> dict[UUID, _LegacyEmploymentTypeSnapshot]:
        after: UUID | None = None
        snapshots: dict[UUID, _LegacyEmploymentTypeSnapshot] = {}
        while True:
            statement = (
                select(EmploymentType)
                .where(EmploymentType.organization_id == self.organization_id)
                .order_by(EmploymentType.employment_type_id)
                .limit(_MODULE_PAGE_SIZE)
            )
            if after is not None:
                statement = statement.where(EmploymentType.employment_type_id > after)
            rows = tuple(self.db.scalars(statement))
            for row in rows:
                snapshot = _legacy_snapshot(row)
                if snapshot.employment_type_id in snapshots:
                    raise ValidationError(
                        "legacy Employment Type scan repeated id "
                        f"{snapshot.employment_type_id}"
                    )
                snapshots[snapshot.employment_type_id] = snapshot
            if len(rows) < _MODULE_PAGE_SIZE:
                return snapshots
            after = rows[-1].employment_type_id

    def repair_compatibility_projection(self) -> int:
        """Idempotently repair the derived table from module authority only."""
        module_records = self._scan_module_records()
        module_by_id = {record.id: record for record in module_records}
        if len(module_by_id) != len(module_records):
            raise ValidationError("authoritative Employment Type scan repeated an id")

        legacy_before = self._scan_legacy_projection()
        legacy_only = sorted(set(legacy_before) - set(module_by_id), key=str)
        if legacy_only:
            raise ValidationError(
                "legacy compatibility projection contains ids absent from the "
                "complete authoritative module set; refusing every write: "
                + ", ".join(str(row_id) for row_id in legacy_only)
            )

        repaired = 0
        for record in module_records:
            legacy = legacy_before.get(record.id)
            if legacy is None or not _projection_matches(legacy, record):
                self._projector.project(record)
                repaired += 1

        legacy_after = self._scan_legacy_projection()
        if set(legacy_after) != set(module_by_id):
            missing = sorted(set(module_by_id) - set(legacy_after), key=str)
            extra = sorted(set(legacy_after) - set(module_by_id), key=str)
            raise ValidationError(
                "Employment Type compatibility id sets differ after repair: "
                f"missing={[str(value) for value in missing]} "
                f"extra={[str(value) for value in extra]}"
            )
        mismatched = sorted(
            (
                row_id
                for row_id, record in module_by_id.items()
                if not _projection_matches(legacy_after[row_id], record)
            ),
            key=str,
        )
        if mismatched:
            raise ValidationError(
                "Employment Type compatibility fields differ after repair: "
                + ", ".join(str(row_id) for row_id in mismatched)
            )
        return repaired


__all__ = [
    "EmploymentTypeService",
    "EmploymentTypeView",
]
