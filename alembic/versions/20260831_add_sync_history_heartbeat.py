"""Add a progress heartbeat to sync history.

Revision ID: 20260831_sync_history_heartbeat
Revises: 20260828_people_et_activation
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa

revision = "20260831_sync_history_heartbeat"
down_revision = "20260828_people_et_activation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_history",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        schema="sync",
    )
    op.execute(
        "UPDATE sync.sync_history "
        "SET last_activity_at = started_at "
        "WHERE status = 'RUNNING' AND last_activity_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("sync_history", "last_activity_at", schema="sync")
