"""Repair runtime access to the narrow tenant discovery function.

Production showed ``app_user`` could execute neither the ``tenant_catalog``
schema lookup nor its only approved function. The provider migration already
declares these grants, but an applied revision is never replayed; this additive
forward repair reasserts and verifies the live privilege contract.

Revision ID: 20260828_tenant_catalog_grants
Revises: 20260825_pi_indeterminate
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "20260828_tenant_catalog_grants"
down_revision = "20260825_pi_indeterminate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "tenant_catalog"
SIGNATURE = "tenant_catalog.organization_ids(boolean)"
RUNTIME_ROLE = "app_user"
MIGRATION_ROLE = "app_admin"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    executor = str(bind.scalar(sa.text("SELECT current_user")))
    if executor != MIGRATION_ROLE:
        raise RuntimeError(
            f"tenant-catalog privilege repair is running as {executor!r}; "
            f"{MIGRATION_ROLE!r} is required"
        )

    function_owner = bind.scalar(
        sa.text(
            "SELECT pg_get_userbyid(proowner) "
            "FROM pg_proc WHERE oid = to_regprocedure(:signature)"
        ),
        {"signature": SIGNATURE},
    )
    if function_owner is None:
        raise RuntimeError(f"required function {SIGNATURE} does not exist")
    if str(function_owner) != MIGRATION_ROLE:
        raise RuntimeError(
            f"{SIGNATURE} is owned by {function_owner!r}; expected "
            f"{MIGRATION_ROLE!r} for its reviewed SECURITY DEFINER contract"
        )

    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {RUNTIME_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {RUNTIME_ROLE}")

    has_usage = bool(
        bind.scalar(
            sa.text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
            {"role": RUNTIME_ROLE, "schema": SCHEMA},
        )
    )
    has_execute = bool(
        bind.scalar(
            sa.text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
            {"role": RUNTIME_ROLE, "signature": SIGNATURE},
        )
    )
    if not (has_usage and has_execute):
        raise RuntimeError(
            f"failed to restore {RUNTIME_ROLE} access to {SIGNATURE}: "
            f"schema_usage={has_usage}, function_execute={has_execute}"
        )


def downgrade() -> None:
    raise RuntimeError(
        "20260828_tenant_catalog_grants is a forward-only production repair; "
        "revoking it would restore the scheduled-task outage"
    )
