"""Selfcare-facing staff access projection contracts."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StaffAccessProjectionEntity(str, Enum):
    LEAVE_RESTRICTION = "leave_restriction"
    ACCOUNT_STATUS = "account_status"


class StaffLeaveRestrictionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Literal["leave_restriction"] = "leave_restriction"
    restriction_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: str | None = None
    leave_application_id: UUID
    effective_from: date
    effective_until: date
    status: str
    source_leave_status: str
    version: int
    updated_at: datetime
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None


class StaffAccountStatusProjectionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Literal["account_status"] = "account_status"
    projection_id: UUID
    organization_id: UUID
    employee_id: UUID
    person_id: UUID
    selfcare_user_id: str | None = None
    erp_employee_status: str
    state: str
    source_reason: str
    ownership: Literal["erp_employee_status"] = "erp_employee_status"
    version: int
    updated_at: datetime


StaffAccessProjectionRecord = Annotated[
    StaffLeaveRestrictionProjection | StaffAccountStatusProjectionRead,
    Field(discriminator="entity"),
]


class StaffAccessProjectionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["staff.access.projection.v1"] = (
        "staff.access.projection.v1"
    )
    entity: StaffAccessProjectionEntity
    items: list[StaffAccessProjectionRecord]


__all__ = [
    "StaffAccessProjectionEntity",
    "StaffAccessProjectionPage",
    "StaffAccessProjectionRecord",
    "StaffAccountStatusProjectionRead",
    "StaffLeaveRestrictionProjection",
]
