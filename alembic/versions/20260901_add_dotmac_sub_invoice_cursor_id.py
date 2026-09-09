"""Add stable row cursor for bounded dotmac_sub invoice sync.

Revision ID: 20260901_dotmac_sub_invoice_cursor_id
Revises: 20260831_sync_history_heartbeat
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_dotmac_sub_invoice_cursor_id"
down_revision = "20260831_sync_history_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dotmac_sub_sync_watermark",
        sa.Column(
            "watermark_external_id",
            sa.String(length=100),
            nullable=True,
            comment=(
                "Stable source id for the last row processed at watermark_at; "
                "used by bounded invoice sync with upstream updated_at,id ordering"
            ),
        ),
        schema="ar",
    )


def downgrade() -> None:
    op.drop_column(
        "dotmac_sub_sync_watermark",
        "watermark_external_id",
        schema="ar",
    )
