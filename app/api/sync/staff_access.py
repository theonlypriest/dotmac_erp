"""Read-only ERP staff access projections for Selfcare reconciliation."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.service_principal import (
    get_db_with_service_org,
    require_explicit_service_scope,
)
from app.schemas.sync.staff_access import (
    StaffAccessProjectionEntity,
    StaffAccessProjectionPage,
    StaffAccessProjectionRecord,
    StaffAccountStatusProjectionRead,
    StaffLeaveRestrictionProjection,
)
from app.services.people.hr.staff_access_projection import StaffAccessProjectionService

router = APIRouter(prefix="/sync/sub/staff-access", tags=["sub-staff-access"])


@router.get("/projection", response_model=StaffAccessProjectionPage)
def read_staff_access_projection(
    entity: StaffAccessProjectionEntity,
    updated_after: datetime | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    auth: dict = Depends(require_explicit_service_scope("sub:staff_access:read")),
    db: Session = Depends(get_db_with_service_org),
) -> StaffAccessProjectionPage:
    """Read tenant-bound staff access projection rows for reconciliation."""
    organization_id = auth["organization_id"]
    if not isinstance(organization_id, UUID):
        organization_id = UUID(str(organization_id))

    service = StaffAccessProjectionService(db)
    items: list[StaffAccessProjectionRecord]
    if entity == StaffAccessProjectionEntity.LEAVE_RESTRICTION:
        items = [
            StaffLeaveRestrictionProjection(
                restriction_id=row.restriction_id,
                organization_id=row.organization_id,
                employee_id=row.employee_id,
                person_id=row.person_id,
                selfcare_user_id=row.selfcare_user_id,
                leave_application_id=row.leave_application_id,
                effective_from=row.effective_from,
                effective_until=row.effective_until,
                status=row.status.value,
                source_leave_status=row.source_leave_status,
                version=row.version,
                updated_at=row.updated_at,
                cancelled_at=row.cancelled_at,
                cancellation_reason=row.cancellation_reason,
            )
            for row in service.list_leave_restrictions(
                organization_id,
                updated_after=updated_after,
                limit=limit,
            )
        ]
    else:
        items = [
            StaffAccountStatusProjectionRead(
                projection_id=row.projection_id,
                organization_id=row.organization_id,
                employee_id=row.employee_id,
                person_id=row.person_id,
                selfcare_user_id=row.selfcare_user_id,
                erp_employee_status=row.erp_employee_status,
                state=row.state.value,
                source_reason=row.source_reason,
                version=row.version,
                updated_at=row.updated_at,
            )
            for row in service.list_account_statuses(
                organization_id,
                updated_after=updated_after,
                limit=limit,
            )
        ]

    return StaffAccessProjectionPage(entity=entity, items=items)


__all__ = ["router"]
