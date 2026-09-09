"""
Employment Type Model - HR Schema.

Types of employment (Full-time, Part-time, Contract, etc.)
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.people.base import AuditMixin, ERPNextSyncMixin

if TYPE_CHECKING:
    from app.models.finance.core_org.organization import Organization
    from app.models.people.hr.employee import Employee


class EmploymentType(Base, AuditMixin, ERPNextSyncMixin):
    """
    Employment Type entity.

    Defines types of employment arrangements:
    - Full-time
    - Part-time
    - Contract
    - Intern
    - Consultant
    """

    __tablename__ = "employment_type"
    __table_args__ = {"schema": "hr"}

    employment_type_id: Mapped[uuid.UUID] = mapped_column(
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

    # Employment type identification
    type_code: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )
    type_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization",
        foreign_keys=[organization_id],
    )
    employees: Mapped[list["Employee"]] = relationship(
        "Employee",
        back_populates="employment_type",
        foreign_keys="Employee.employment_type_id",
    )
