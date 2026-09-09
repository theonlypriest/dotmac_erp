"""
Sync History Model - Track sync job runs.

Records each migration/sync execution for audit and monitoring.
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc  # type: ignore[assignment]


class SyncType(str, enum.Enum):
    """Type of sync operation."""

    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class SyncJobStatus(str, enum.Enum):
    """Status of sync job."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SyncHistory(Base):
    """
    Track sync job execution history.

    Records each migration run with statistics and errors.
    """

    __tablename__ = "sync_history"
    __table_args__ = (
        Index("idx_sync_history_org", "organization_id"),
        Index("idx_sync_history_status", "status"),
        Index("idx_sync_history_started", "started_at"),
        {"schema": "sync"},
    )

    history_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("core_org.organization.organization_id"),
        nullable=False,
    )

    # Source system
    source_system: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # e.g., 'erpnext'

    # Sync configuration
    sync_type: Mapped[SyncType] = mapped_column(
        Enum(SyncType, name="sync_type"),
        nullable=False,
        default=SyncType.FULL,
    )
    entity_types: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True
    )  # ['items', 'assets', 'accounts']

    # Job status
    status: Mapped[SyncJobStatus] = mapped_column(
        Enum(SyncJobStatus, name="sync_job_status"),
        nullable=False,
        default=SyncJobStatus.PENDING,
    )

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Statistics
    total_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synced_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Error details (first N errors)
    errors: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )  # [{doctype, name, error}]

    # Audit
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def start(self) -> None:
        """Mark job as started."""
        now = datetime.now(UTC)
        self.status = SyncJobStatus.RUNNING
        self.started_at = now
        self.last_activity_at = now

    def touch(self) -> None:
        """Record progress so maintenance can distinguish live and abandoned jobs."""
        self.last_activity_at = datetime.now(UTC)

    def complete(self) -> None:
        """Mark job as completed."""
        self.completed_at = datetime.now(UTC)
        if self.error_count > 0:
            self.status = SyncJobStatus.COMPLETED_WITH_ERRORS
        else:
            self.status = SyncJobStatus.COMPLETED

    def fail(self, error: str) -> None:
        """Mark job as failed."""
        self.status = SyncJobStatus.FAILED
        self.completed_at = datetime.now(UTC)
        self.add_error("system", "system", error)

    def cancel(self) -> None:
        """Mark job as cancelled."""
        self.status = SyncJobStatus.CANCELLED
        self.completed_at = datetime.now(UTC)

    def add_error(self, doctype: str, name: str, error: str) -> None:
        """Add error to error list (capped at 100)."""
        if self.errors is None:
            self.errors = []
        if len(self.errors) < 100:
            self.errors.append(
                {
                    "doctype": doctype,
                    "name": name,
                    "error": error,
                }
            )
        self.error_count += 1

    def increment_synced(self) -> None:
        """Increment synced count."""
        self.synced_count += 1

    def increment_skipped(self) -> None:
        """Increment skipped count."""
        self.skipped_count += 1

    @property
    def duration_seconds(self) -> float | None:
        """Calculate duration in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.total_records == 0:
            return 0.0
        return (self.synced_count / self.total_records) * 100
