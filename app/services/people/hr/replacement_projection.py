"""Tenant-scoped ERP source projection for the People replacement programme.

This service is intentionally read-only.  It exposes only the fields owned by
the released kernel Party and ``dotmac-people`` contracts, and every query
carries the service principal's organization explicitly in addition to the
session-level tenant boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import TypeVar
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.models.people.hr import (
    Department,
    Designation,
    Employee,
    Position,
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.person import Person
from app.schemas.sync.backoffice_people import (
    DepartmentProjection,
    DesignationProjection,
    EmployeeProjection,
    EmploymentTypeProjection,
    PartyPersonProjection,
    PeopleProjectionEntity,
    PeopleProjectionPage,
    PeopleProjectionRecord,
    PositionAssignmentProjection,
    PositionProjection,
)
from app.services.people.hr.employment_types import EmploymentTypeService

_Row = TypeVar("_Row")
_ProjectionSource = (
    Person | Department | Designation | Employee | Position | PositionAssignment
)


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"unsupported projection value: {type(value).__name__}")


def _fingerprint(payload: dict[str, object]) -> str:
    """Return the v1 projection fingerprint for one exact wire payload."""
    encoded = json.dumps(
        payload,
        default=_json_default,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _updated_at(row: _ProjectionSource) -> datetime | None:
    return row.updated_at or row.created_at


class BackofficePeopleProjectionService:
    """Read one stable, UUID-keyset page from ERP's current People authority."""

    def __init__(self, db: Session):
        self.db = db

    def _page_rows(
        self, statement: Select[tuple[_Row]], *, limit: int
    ) -> tuple[list[_Row], bool]:
        rows = list(self.db.scalars(statement.limit(limit + 1)))
        return rows[:limit], len(rows) > limit

    @staticmethod
    def _page(
        *,
        entity: PeopleProjectionEntity,
        items: list[PeopleProjectionRecord],
        has_more: bool,
    ) -> PeopleProjectionPage:
        return PeopleProjectionPage(
            entity=entity,
            items=items,
            next_after=items[-1].source_id if has_more and items else None,
        )

    def page(
        self,
        *,
        organization_id: UUID,
        entity: PeopleProjectionEntity,
        after: UUID | None,
        limit: int,
    ) -> PeopleProjectionPage:
        if not isinstance(organization_id, UUID):
            raise TypeError("People projection requires an explicit organization UUID")
        if not 1 <= limit <= 500:
            raise ValueError("People projection limit must be between 1 and 500")
        readers = {
            PeopleProjectionEntity.PARTY_PERSON: self._party_people,
            PeopleProjectionEntity.DEPARTMENT: self._departments,
            PeopleProjectionEntity.DESIGNATION: self._designations,
            PeopleProjectionEntity.EMPLOYMENT_TYPE: self._employment_types,
            PeopleProjectionEntity.EMPLOYEE: self._employees,
            PeopleProjectionEntity.POSITION: self._positions,
            PeopleProjectionEntity.POSITION_ASSIGNMENT: self._position_assignments,
        }
        return readers[entity](organization_id, after, limit)

    def _party_people(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(Person).where(Person.organization_id == organization_id)
        if after is not None:
            statement = statement.where(Person.id > after)
        rows, has_more = self._page_rows(statement.order_by(Person.id), limit=limit)
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "display_name": row.name,
                "email": row.email,
                "is_active": row.is_active,
                "first_name": row.first_name,
                "last_name": row.last_name,
            }
            items.append(
                PartyPersonProjection(
                    source_id=row.id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    display_name=row.name,
                    email=row.email,
                    is_active=row.is_active,
                    first_name=row.first_name,
                    last_name=row.last_name,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.PARTY_PERSON,
            items=items,
            has_more=has_more,
        )

    def _departments(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(Department).where(
            Department.organization_id == organization_id
        )
        if after is not None:
            statement = statement.where(Department.department_id > after)
        rows, has_more = self._page_rows(
            statement.order_by(Department.department_id), limit=limit
        )
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "code": row.department_code,
                "name": row.department_name,
                "description": row.description,
                "parent_id": row.parent_department_id,
                "is_active": row.is_active,
            }
            items.append(
                DepartmentProjection(
                    source_id=row.department_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    code=row.department_code,
                    name=row.department_name,
                    description=row.description,
                    parent_id=row.parent_department_id,
                    is_active=row.is_active,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.DEPARTMENT,
            items=items,
            has_more=has_more,
        )

    def _designations(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(Designation).where(
            Designation.organization_id == organization_id
        )
        if after is not None:
            statement = statement.where(Designation.designation_id > after)
        rows, has_more = self._page_rows(
            statement.order_by(Designation.designation_id), limit=limit
        )
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "code": row.designation_code,
                "name": row.designation_name,
                "description": row.description,
                "is_active": row.is_active,
            }
            items.append(
                DesignationProjection(
                    source_id=row.designation_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    code=row.designation_code,
                    name=row.designation_name,
                    description=row.description,
                    is_active=row.is_active,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.DESIGNATION,
            items=items,
            has_more=has_more,
        )

    def _employment_types(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        complete = EmploymentTypeService(self.db, organization_id).iter_all()
        ordered = sorted(
            (
                row
                for row in complete
                if after is None or row.employment_type_id > after
            ),
            key=lambda row: row.employment_type_id,
        )
        rows = ordered[: limit + 1]
        has_more = len(rows) > limit
        items: list[PeopleProjectionRecord] = []
        for row in rows[:limit]:
            payload: dict[str, object] = {
                "code": row.type_code,
                "name": row.type_name,
                "description": row.description,
                "is_active": row.is_active,
            }
            items.append(
                EmploymentTypeProjection(
                    source_id=row.employment_type_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=row.updated_at or row.created_at,
                    code=row.type_code,
                    name=row.type_name,
                    description=row.description,
                    is_active=row.is_active,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.EMPLOYMENT_TYPE,
            items=items,
            has_more=has_more,
        )

    def _employees(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(Employee).where(Employee.organization_id == organization_id)
        if after is not None:
            statement = statement.where(Employee.employee_id > after)
        rows, has_more = self._page_rows(
            statement.order_by(Employee.employee_id), limit=limit
        )
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "party_id": row.person_id,
                "employee_code": row.employee_code,
                "department_id": row.department_id,
                "designation_id": row.designation_id,
                "employment_type_id": row.employment_type_id,
                "date_of_joining": row.date_of_joining,
                "date_of_leaving": row.date_of_leaving,
                "probation_end_date": row.probation_end_date,
                "confirmation_date": row.confirmation_date,
                "status": row.status.value,
            }
            items.append(
                EmployeeProjection(
                    source_id=row.employee_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    party_id=row.person_id,
                    employee_code=row.employee_code,
                    department_id=row.department_id,
                    designation_id=row.designation_id,
                    employment_type_id=row.employment_type_id,
                    date_of_joining=row.date_of_joining,
                    date_of_leaving=row.date_of_leaving,
                    probation_end_date=row.probation_end_date,
                    confirmation_date=row.confirmation_date,
                    status=row.status.value,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.EMPLOYEE,
            items=items,
            has_more=has_more,
        )

    def _positions(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(Position).where(Position.organization_id == organization_id)
        if after is not None:
            statement = statement.where(Position.position_id > after)
        rows, has_more = self._page_rows(
            statement.order_by(Position.position_id), limit=limit
        )
        row_ids = [row.position_id for row in rows]
        head_position_ids: set[UUID] = set()
        if row_ids:
            today = date.today()
            head_position_ids = set(
                self.db.scalars(
                    select(PositionAssignment.position_id)
                    .join(
                        Position,
                        and_(
                            Position.organization_id
                            == PositionAssignment.organization_id,
                            Position.position_id == PositionAssignment.position_id,
                        ),
                    )
                    .join(
                        Department,
                        and_(
                            Department.organization_id
                            == PositionAssignment.organization_id,
                            Department.department_id == Position.department_id,
                            Department.head_id == PositionAssignment.employee_id,
                        ),
                    )
                    .where(
                        PositionAssignment.organization_id == organization_id,
                        PositionAssignment.position_id.in_(row_ids),
                        PositionAssignment.assignment_type
                        == PositionAssignmentType.PRIMARY,
                        PositionAssignment.start_date <= today,
                        or_(
                            PositionAssignment.end_date.is_(None),
                            PositionAssignment.end_date >= today,
                        ),
                    )
                )
            )
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "code": row.position_code,
                "name": row.position_name,
                "department_id": row.department_id,
                "designation_id": row.designation_id,
                "parent_id": row.parent_position_id,
                "is_department_head": row.position_id in head_position_ids,
                "vacancy_routing_policy": row.vacancy_routing_policy.value,
                "is_active": row.is_active,
            }
            items.append(
                PositionProjection(
                    source_id=row.position_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    code=row.position_code,
                    name=row.position_name,
                    department_id=row.department_id,
                    designation_id=row.designation_id,
                    parent_id=row.parent_position_id,
                    is_department_head=row.position_id in head_position_ids,
                    vacancy_routing_policy=row.vacancy_routing_policy.value,
                    is_active=row.is_active,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.POSITION,
            items=items,
            has_more=has_more,
        )

    def _position_assignments(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> PeopleProjectionPage:
        statement = select(PositionAssignment).where(
            PositionAssignment.organization_id == organization_id
        )
        if after is not None:
            statement = statement.where(
                PositionAssignment.position_assignment_id > after
            )
        rows, has_more = self._page_rows(
            statement.order_by(PositionAssignment.position_assignment_id), limit=limit
        )
        items: list[PeopleProjectionRecord] = []
        for row in rows:
            payload: dict[str, object] = {
                "employee_id": row.employee_id,
                "position_id": row.position_id,
                "assignment_type": row.assignment_type.value,
                "start_date": row.start_date,
                "end_date": row.end_date,
            }
            items.append(
                PositionAssignmentProjection(
                    source_id=row.position_assignment_id,
                    source_fingerprint=_fingerprint(payload),
                    source_updated_at=_updated_at(row),
                    employee_id=row.employee_id,
                    position_id=row.position_id,
                    assignment_type=row.assignment_type.value,
                    start_date=row.start_date,
                    end_date=row.end_date,
                )
            )
        return self._page(
            entity=PeopleProjectionEntity.POSITION_ASSIGNMENT,
            items=items,
            has_more=has_more,
        )


__all__ = [
    "BackofficePeopleProjectionService",
    "PeopleProjectionEntity",
]
