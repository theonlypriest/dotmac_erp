"""Activate module-owned Employment Types and the one compatibility projector.

Revision ID: 20260828_people_et_activation
Revises: 20260828_route_permissions
Create Date: 2026-08-28

``mod_people.employment_types`` is authoritative at this application revision.
The retained ``hr.employment_type`` relation is a synchronous, derived foreign-
key projection.  The online role may select, insert, and update it through the
one assembly projector; destructive and relationship-changing privileges stay
absent.  The pre-activation source fence is removed with the reverse bootstrap.
"""

from __future__ import annotations

import os
from typing import Any

from alembic import op

revision = "20260828_people_et_activation"
down_revision = "20260828_route_permissions"
branch_labels = None
depends_on = None

_COLUMNS = (
    "employment_type_id",
    "organization_id",
    "type_code",
    "type_name",
    "description",
    "is_active",
    "created_at",
    "updated_at",
    "created_by_id",
    "updated_by_id",
    "erpnext_id",
    "last_synced_at",
)

_ACTIVATION_OPT_IN = "PEOPLE_EMPLOYMENT_TYPE_ACTIVATION"
_PARITY_SQL = """
WITH catalogue AS (
    SELECT
        legacy.employment_type_id AS legacy_id,
        authoritative.id AS authoritative_id,
        legacy.organization_id AS legacy_tenant_id,
        authoritative.tenant_id AS authoritative_tenant_id,
        legacy.type_code AS legacy_code,
        authoritative.code AS authoritative_code,
        legacy.type_name AS legacy_name,
        authoritative.name AS authoritative_name,
        legacy.description AS legacy_description,
        authoritative.description AS authoritative_description,
        legacy.is_active AS legacy_is_active,
        authoritative.is_active AS authoritative_is_active,
        legacy.created_at AS legacy_created_at,
        authoritative.created_at AS authoritative_created_at,
        legacy.updated_at AS legacy_updated_at,
        authoritative.updated_at AS authoritative_updated_at
    FROM hr.employment_type AS legacy
    FULL OUTER JOIN mod_people.employment_types AS authoritative
        ON authoritative.id = legacy.employment_type_id
)
SELECT
    count(*) FILTER (
        WHERE legacy_id IS NOT NULL AND authoritative_id IS NULL
    ) AS legacy_only,
    count(*) FILTER (
        WHERE legacy_id IS NULL AND authoritative_id IS NOT NULL
    ) AS authoritative_only,
    count(*) FILTER (
        WHERE legacy_id IS NOT NULL
          AND authoritative_id IS NOT NULL
          AND (
              legacy_tenant_id IS DISTINCT FROM authoritative_tenant_id
              OR legacy_code IS DISTINCT FROM authoritative_code
              OR legacy_name IS DISTINCT FROM authoritative_name
              OR legacy_description IS DISTINCT FROM authoritative_description
              OR legacy_is_active IS DISTINCT FROM authoritative_is_active
              OR legacy_created_at IS DISTINCT FROM authoritative_created_at
              OR legacy_updated_at IS DISTINCT FROM authoritative_updated_at
          )
    ) AS mismatched
FROM catalogue
"""

_PROJECTION_FENCE_FUNCTION = """
CREATE FUNCTION hr.enforce_employment_type_projection()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM mod_people.employment_types AS authoritative
        WHERE authoritative.id = NEW.employment_type_id
          AND authoritative.tenant_id IS NOT DISTINCT FROM NEW.organization_id
          AND authoritative.code IS NOT DISTINCT FROM NEW.type_code
          AND authoritative.name IS NOT DISTINCT FROM NEW.type_name
          AND authoritative.description IS NOT DISTINCT FROM NEW.description
          AND authoritative.is_active IS NOT DISTINCT FROM NEW.is_active
          AND authoritative.created_at IS NOT DISTINCT FROM NEW.created_at
          AND authoritative.updated_at IS NOT DISTINCT FROM NEW.updated_at
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'hr.employment_type is a derived compatibility projection; '
                'write mod_people.employment_types through its owning service',
            SCHEMA = 'hr',
            TABLE = 'employment_type';
    END IF;
    RETURN NEW;
END
$function$
"""

_PROJECTION_FENCE_TRIGGER = """
CREATE TRIGGER enforce_employment_type_projection
BEFORE INSERT OR UPDATE ON hr.employment_type
FOR EACH ROW
EXECUTE FUNCTION hr.enforce_employment_type_projection()
"""

# Frozen from scripts/seed_rbac.py at this activation revision. Roles are the
# existing global definitions; organization scope continues to come from the
# Person carrying a role membership, not from per-organization role variants.
EMPLOYMENT_TYPE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("hr:employment_types:read", "View employment types"),
    ("hr:employment_types:manage", "Manage employment types"),
)

ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full system administrator",
    "hr_director": "HR executive with full module access",
    "hr_manager": "HR management with approvals",
    "hr_officer": "Standard HR operations",
}

ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "admin": (
        "hr:employment_types:read",
        "hr:employment_types:manage",
    ),
    "hr_director": (
        "hr:employment_types:read",
        "hr:employment_types:manage",
    ),
    "hr_manager": ("hr:employment_types:read",),
    "hr_officer": ("hr:employment_types:read",),
}


def _ensure_active_role(conn: Any, name: str, description: str) -> Any:
    conn.exec_driver_sql(
        """
        INSERT INTO roles (id, name, description, is_active, created_at, updated_at)
        VALUES (gen_random_uuid(), %s, %s, true, NOW(), NOW())
        ON CONFLICT (name) DO NOTHING
        """,
        (name, description),
    )
    row = conn.exec_driver_sql(
        "SELECT id, is_active FROM roles WHERE name = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Employment Type RBAC role was not materialized: {name}")
    if not row[1]:
        raise RuntimeError(f"Employment Type RBAC role is inactive: {name}")
    return row[0]


def _ensure_active_permission(
    conn: Any,
    code: str,
    description: str,
) -> Any:
    conn.exec_driver_sql(
        """
        INSERT INTO permissions (
            id, key, description, is_active, created_at, updated_at
        )
        VALUES (gen_random_uuid(), %s, %s, true, NOW(), NOW())
        ON CONFLICT (key) DO NOTHING
        """,
        (code, description),
    )
    row = conn.exec_driver_sql(
        "SELECT id, is_active FROM permissions WHERE key = %s",
        (code,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Employment Type RBAC permission was not materialized: {code}"
        )
    if not row[1]:
        raise RuntimeError(f"Employment Type RBAC permission is inactive: {code}")
    return row[0]


def _provision_permissions(conn: Any) -> None:
    role_ids = {
        name: _ensure_active_role(conn, name, description)
        for name, description in ROLE_DESCRIPTIONS.items()
    }
    permission_ids = {
        code: _ensure_active_permission(conn, code, description)
        for code, description in EMPLOYMENT_TYPE_PERMISSIONS
    }
    for role_name, permission_codes in ROLE_GRANTS.items():
        for code in permission_codes:
            conn.exec_driver_sql(
                """
                INSERT INTO role_permissions (id, role_id, permission_id)
                VALUES (gen_random_uuid(), %s, %s)
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """,
                (role_ids[role_name], permission_ids[code]),
            )


def _require_exact_catalogue_parity(conn: Any) -> None:
    """Refuse activation unless legacy and module catalogues are identical."""
    row = conn.exec_driver_sql(_PARITY_SQL).fetchone()
    if row is None:
        raise RuntimeError("Employment Type activation parity query returned no result")
    legacy_only, authoritative_only, mismatched = (int(value) for value in row)
    if legacy_only or authoritative_only or mismatched:
        raise RuntimeError(
            "Employment Type activation requires exact all-tenant catalogue parity: "
            f"legacy_only={legacy_only} "
            f"authoritative_only={authoritative_only} "
            f"mismatched={mismatched}"
        )


def upgrade() -> None:
    if os.environ.get(_ACTIVATION_OPT_IN) != "1":
        raise RuntimeError(
            "Employment Type activation requires explicit operator opt-in: "
            f"{_ACTIVATION_OPT_IN}=1"
        )

    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    # The drained cutover owns this transaction. Exclusive, fail-fast fences on
    # both catalogues prove the drain rather than trusting the deploy caller's
    # assertion. In particular, the legacy bootstrap function takes SHARE on
    # the source before writing the target; a weaker SHARE fence here would let
    # a new bootstrap acquire its source lock, wait on the target, and resume
    # after this transaction committed the authority switch.
    op.execute("LOCK TABLE hr.employment_type IN ACCESS EXCLUSIVE MODE NOWAIT")
    op.execute("LOCK TABLE mod_people.employment_types IN ACCESS EXCLUSIVE MODE NOWAIT")
    _require_exact_catalogue_parity(conn)

    # Preserve the proven fixed point after the exclusive drain ends. The
    # module owner writes its row first and then synchronously projects that
    # exact image here in the same transaction. A legacy decision write has no
    # matching authoritative image and is rejected by the database itself.
    # SECURITY DEFINER is intentional: app_admin can compare across every
    # tenant despite target RLS, while the fixed pg_catalog search path and
    # fully-qualified target relation exclude caller-controlled resolution.
    op.execute(_PROJECTION_FENCE_FUNCTION)
    op.execute(
        "ALTER FUNCTION hr.enforce_employment_type_projection() OWNER TO app_admin"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION hr.enforce_employment_type_projection() FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION hr.enforce_employment_type_projection() FROM app_user"
    )
    op.execute(_PROJECTION_FENCE_TRIGGER)

    op.execute(
        "REVOKE EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() FROM app_user"
    )
    op.execute("DROP FUNCTION hr.lock_employment_type_bootstrap()")

    op.execute("GRANT USAGE ON SCHEMA hr TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE hr.employment_type TO app_user")
    op.execute(
        "REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON TABLE hr.employment_type FROM app_user"
    )
    op.execute(
        "REVOKE REFERENCES ("
        + ", ".join(_COLUMNS)
        + ") ON TABLE hr.employment_type FROM app_user"
    )
    _provision_permissions(conn)


def downgrade() -> None:
    raise RuntimeError(
        "20260828_people_et_activation is forward-fix only: authority, "
        "projection privileges, and additive RBAC rows cannot be safely "
        "distinguished from adopted operator state"
    )
