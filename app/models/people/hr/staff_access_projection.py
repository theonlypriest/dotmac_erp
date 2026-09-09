"""ERP-owned staff access projections for Selfcare replication."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.people.hr.employee import Employee


class StaffLeaveRestrictionStatus(str, enum.Enum):
    """Replicated status for a leave-sourced read-only restriction."""

    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class StaffAccountStatusState(str, enum.Enum):
    """ERP-controlled account state replicated to Selfcare."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class StaffLeaveAccessRestriction(Base):
    """Versioned leave restriction projection owned by ERP."""

    __tablename__ = "staff_leave_access_restriction"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "leave_application_id",
            name="uq_staff_leave_restriction_leave",
        ),
        Index(
            "idx_staff_leave_restriction_active_person",
            "organization_id",
            "person_id",
            "status",
            "effective_from",
            "effective_until",
        ),
        Index(
            "idx_staff_leave_restriction_active_employee",
            "organization_id",
            "employee_id",
            "status",
            "effective_from",
            "effective_until",
        ),
        Index(
            "idx_staff_leave_restriction_updated",
            "organization_id",
            "updated_at",
            "restriction_id",
        ),
        {"schema": "hr"},
    )

    restriction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("core_org.organization.organization_id"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hr.employee.employee_id"),
        nullable=False,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("people.id"),
        nullable=False,
    )
    selfcare_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    leave_application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[StaffLeaveRestrictionStatus] = mapped_column(
        Enum(
            StaffLeaveRestrictionStatus,
            name="staff_leave_restriction_status",
        ),
        nullable=False,
        default=StaffLeaveRestrictionStatus.ACTIVE,
    )
    source_leave_status: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    employee: Mapped[Employee] = relationship("Employee")


class StaffAccountStatusProjection(Base):
    """ERP-owned account-status projection for a mapped Selfcare user."""

    __tablename__ = "staff_account_status_projection"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "employee_id",
            name="uq_staff_account_status_employee",
        ),
        Index(
            "idx_staff_account_status_updated",
            "organization_id",
            "updated_at",
            "projection_id",
        ),
        Index(
            "idx_staff_account_status_selfcare",
            "organization_id",
            "selfcare_user_id",
        ),
        {"schema": "hr"},
    )

    projection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("core_org.organization.organization_id"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hr.employee.employee_id"),
        nullable=False,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("people.id"),
        nullable=False,
    )
    selfcare_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    erp_employee_status: Mapped[str] = mapped_column(String(30), nullable=False)
    state: Mapped[StaffAccountStatusState] = mapped_column(
        Enum(
            StaffAccountStatusState,
            name="staff_account_status_state",
        ),
        nullable=False,
    )
    source_reason: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="employee_status",
        server_default="employee_status",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    employee: Mapped[Employee] = relationship("Employee")


__all__ = [
    "StaffAccountStatusProjection",
    "StaffAccountStatusState",
    "StaffLeaveAccessRestriction",
    "StaffLeaveRestrictionStatus",
]
