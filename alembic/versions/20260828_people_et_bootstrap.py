"""Grant the app role read-only access to the legacy Employment Type source.

Revision ID: 20260828_people_et_bootstrap
Revises: 20260828_merge_consolidated_heads
Create Date: 2026-08-28

This is a pre-activation bootstrap prerequisite.  It does not alter a table,
move authority, or grant any legacy write privilege.  The explicit operator
CLI gets the ability to read and transaction-fence the still-authoritative
source while the ``dotmac-people`` lineage owns target writes.
"""

from __future__ import annotations

from alembic import op

revision = "20260828_people_et_bootstrap"
down_revision = "20260828_merge_consolidated_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("GRANT USAGE ON SCHEMA hr TO app_user")
    op.execute("GRANT SELECT ON TABLE hr.employment_type TO app_user")
    # PostgreSQL permits a SELECT-only role to acquire ACCESS SHARE only. The
    # bootstrap needs SHARE to exclude INSERT/UPDATE/DELETE for the duration of
    # both scans, but granting a DML privilege merely to obtain that lock would
    # violate the legacy read-only boundary. Expose exactly the lock instead.
    op.execute(
        """
        CREATE FUNCTION hr.lock_employment_type_bootstrap()
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            LOCK TABLE hr.employment_type IN SHARE MODE NOWAIT;
        END
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION hr.lock_employment_type_bootstrap() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() TO app_user"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "REVOKE EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() FROM app_user"
    )
    op.execute("DROP FUNCTION hr.lock_employment_type_bootstrap()")
    op.execute("REVOKE SELECT ON TABLE hr.employment_type FROM app_user")
    # Do not revoke schema USAGE: this revision did not create the schema and
    # another independently deployed ERP path may still need that shared grant.
