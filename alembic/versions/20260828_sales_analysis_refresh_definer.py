"""Expose one owner-executed refresh path for the sales analysis view.

The application role must not own reporting relations, but PostgreSQL permits
only a materialized view's owner to refresh it. This migration keeps ownership
with ``app_admin`` and grants ``app_user`` one fixed-purpose SECURITY DEFINER
function that cannot name or refresh any other relation.

Revision ID: 20260828_sales_mv_refresh
Revises: 20260825_pi_indeterminate
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260828_sales_mv_refresh"
down_revision = "20260825_pi_indeterminate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "rpt.refresh_sales_analysis_mv(boolean)"

_CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION rpt.refresh_sales_analysis_mv(
    use_concurrently boolean DEFAULT true
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
    IF use_concurrently THEN
        EXECUTE 'REFRESH MATERIALIZED VIEW CONCURRENTLY rpt.sales_analysis_mv';
    ELSE
        EXECUTE 'REFRESH MATERIALIZED VIEW rpt.sales_analysis_mv';
    END IF;
END;
$function$
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    owner = str(bind.scalar(sa.text("SELECT current_user")))
    if owner != "app_admin":
        raise RuntimeError(
            f"sales analysis refresh definer would be owned by {owner!r}; "
            "expected 'app_admin'"
        )
    exists = bool(
        bind.scalar(sa.text("SELECT to_regclass('rpt.sales_analysis_mv') IS NOT NULL"))
    )
    if not exists:
        raise RuntimeError(
            "rpt.sales_analysis_mv must exist before its refresh definer"
        )

    op.execute(_CREATE_FUNCTION)
    op.execute(f"ALTER FUNCTION {SIGNATURE} OWNER TO app_admin")
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA rpt TO app_user")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO app_user")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
