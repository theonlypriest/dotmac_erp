"""
dotmac_sub incremental-sync high-watermark - AR Schema.

Stores, per organization + entity type, the highest ``updated_at`` observed
from dotmac_sub that has been successfully synced. The AR pull passes this as
``updated_since`` so it fetches only the delta each cycle instead of re-listing
every invoice/payment/credit-note (the OFFSET-over-unindexed-``created_at`` scan
that starved dotmac_sub's DB pool).

This is a compact cursor (one row per org+entity), distinct from
``ExternalSync.external_updated_at`` which is a per-row audit field.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DotmacSubSyncWatermark(Base):
    """High-watermark cursor for the dotmac_sub AR incremental sync."""

    __tablename__ = "dotmac_sub_sync_watermark"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_type",
            name="uq_dotmac_sub_watermark_org_entity",
        ),
        {"schema": "ar"},
    )

    watermark_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    # EntityType value (INVOICE / PAYMENT / CREDIT_NOTE). Stored as a plain
    # string rather than the shared ``sync_entity_type`` enum so this table owns
    # no enum-type coupling (portable to the SQLite test suite).
    entity_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    # Highest dotmac_sub ``updated_at`` successfully synced for this entity type.
    # NULL means "never synced" → the next pull is a full pull.
    watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    watermark_external_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment=(
            "Stable source id for the last row processed at watermark_at; "
            "used by bounded invoice sync with upstream updated_at,id ordering"
        ),
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
