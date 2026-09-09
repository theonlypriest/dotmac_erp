"""Provision the nine permission keys that routes enforce but nothing seeds.

``require_tenant_permission(key)`` (``app/services/auth_dependencies.py``)
resolves ``key`` against the ``permissions`` catalogue and raises 403 when no
active row exists.  A permission with no row is therefore not "open" and not
"closed by policy" — it is unreachable: no role can be granted it, and the
route it guards is admin-only by accident, because the guard short-circuits on
the ``admin`` role before it ever looks the key up.

Nine keys were in exactly that state.  They are enforced on real mounted
routes under ``app/`` yet appear in neither ``scripts/seed_rbac``'s
``DEFAULT_PERMISSIONS`` (577 codes) nor any earlier migration:

    banking:account:update      app/api/finance/banking.py
    banking:statement:create    app/api/finance/banking.py
    fa:assets:import:read       app/api/fixed_assets/import_export.py
    fa:assets:import:preview    app/api/fixed_assets/import_export.py
    fa:assets:import:execute    app/api/fixed_assets/import_export.py
    payments:invoice:initialize app/api/finance/payments.py
    payments:verify             app/api/finance/payments.py
    people:read                 app/api/persons.py
    people:write                app/api/persons.py

This migration materializes those rows and gives them a baseline set of role
grants, in the same shape as ``20260826_provision_expense_permissions``: the
production deployment path runs migrations rather than ``scripts/seed_rbac``,
so the seed script alone would not close the gap on a deployed database.

WHY EACH GRANT IS WHAT IT IS
============================

Baseline grants are DERIVED from the sibling code already carried by the same
family in ``scripts/seed_rbac``'s ``ROLE_PERMISSIONS``, never invented.  For
each new code the sibling is named, and the grant set is exactly the set of
roles that already hold that sibling.  ``admin`` is granted everything, which
matches ``ROLE_PERMISSIONS["admin"] = [perm for perm, _ in
DEFAULT_PERMISSIONS]`` in the seed script.

``banking:account:update``
    Guards Mono Connect link/unlink on a bank account — a configuration change
    to the account record.  Sibling: ``banking:accounts:update`` (the plural
    code the rest of that router uses), held by ``finance_director`` and
    ``finance_manager``.

``banking:statement:create``
    Guards Mono sync/refresh, which pulls statement lines onto the account.
    Sibling: ``banking:statements:import``, held by ``finance_director``,
    ``finance_manager`` and ``senior_accountant``.

``fa:assets:import:read``
    Guards ``GET /supported-types``, which returns the static description of
    what the asset importer accepts.  It reads nothing tenant-owned.  Sibling:
    ``fa:assets:read``, held by ``auditor``, ``finance_director``,
    ``finance_manager``, ``finance_viewer``, ``inventory_manager``,
    ``asset_manager``, ``asset_custodian`` and ``asset_viewer``.

``fa:assets:import:preview`` and ``fa:assets:import:execute``
    Preview is a dry run and writes nothing; execute creates assets.  Both are
    derived from ``fa:assets:create`` — held by ``finance_director``,
    ``finance_manager``, ``asset_manager`` and ``asset_custodian`` — rather
    than from ``fa:assets:read``.  Preview is deliberately graded with execute
    and not with read: it is the first step of a create workflow, it accepts an
    uploaded file, and a read-only role has nothing to do with the result.
    Grading it down to the eight ``fa:assets:read`` holders would widen the
    upload surface on the strength of a guess.

``payments:invoice:initialize`` and ``payments:verify``
    Initialize creates a Paystack transaction and a local ``PaymentIntent``;
    verify queries Paystack and reconciles that intent when a webhook was
    missed.  Siblings ``payments:intents:create`` and ``payments:intents:read``
    are both held by ``finance_director`` alone, so both new codes go to
    ``finance_director`` alone.  Note that neither of these is the
    disbursement authority: outbound transfer stays behind
    ``payments:transfer:initiate`` (see ``20260826_expense_permissions`` and
    ``tests/architecture/test_money_routes_reject_read_permissions.py``).

``people:read`` and ``people:write``
    NO SIBLING EXISTS.  There is no ``people:`` family anywhere in
    ``ROLE_PERMISSIONS``, so there is nothing to derive from.  Per the
    no-spray rule these go to the single most restrictive role that plausibly
    owns the ``Person`` identity record — ``hr_director``, the HR role that
    already holds ``hr:employees:read``, ``hr:employees:create`` and
    ``hr:employees:update`` — and to nobody else.  This is deliberately an
    UNDER-grant: ``hr:employees:read`` alone has eleven holders and
    ``hr:employees:create`` four, and picking any of those sets would be
    inventing an identity-write policy inside a migration.  Widening it is a
    role edit an operator makes with intent; a migration that had sprayed
    identity write authority across nine HR roles could not be walked back
    without destroying operator state (see ``downgrade`` below).

This migration is deliberately additive.  Existing roles, permissions,
descriptions, memberships, direct grants and role-permission links are
preserved.  An inactive desired role or permission is a CONFLICT: the
migration refuses to silently reactivate an operator-disabled record.

Revision ID: 20260828_route_permissions
Revises: 20260828_people_et_bootstrap
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from alembic import op

revision: str = "20260828_route_permissions"
down_revision: str | None = "20260828_people_et_bootstrap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen from the authored declarations at the revision that introduced this
# migration. A migration must remain executable after those declarations move.
ROUTE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (
        "banking:account:update",
        "Link, unlink, and configure a bank account's Mono Connect binding",
    ),
    (
        "banking:statement:create",
        "Trigger a Mono statement sync or refresh for a bank account",
    ),
    ("fa:assets:import:read", "View fixed asset import capabilities"),
    ("fa:assets:import:preview", "Preview a fixed asset import file (dry run)"),
    ("fa:assets:import:execute", "Execute a fixed asset import"),
    (
        "payments:invoice:initialize",
        "Initialize a Paystack payment for an invoice",
    ),
    (
        "payments:verify",
        "Verify a payment with Paystack and reconcile the payment intent",
    ),
    ("people:read", "View person identity records"),
    (
        "people:write",
        "Create, modify, and deactivate person identity records",
    ),
)

# Frozen copy of the descriptions these roles carry in the seed script's
# DEFAULT_ROLES. Only roles that actually receive a grant below appear here:
# this migration must not conjure a role it has no reason to touch.
ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full system administrator",
    "auditor": "Read-only audit access",
    "finance_director": "Finance executive with full control and approvals",
    "finance_manager": "Finance management with posting and approval rights",
    "senior_accountant": "Experienced accountant with limited approvals",
    "finance_viewer": "Read-only finance access",
    "inventory_manager": "Inventory control specialist",
    "asset_manager": "Asset management administrator",
    "asset_custodian": "Asset management operations",
    "asset_viewer": "Read-only asset management access",
    "hr_director": "HR executive with full module access",
}

_ALL_ROUTE_PERMISSION_CODES = tuple(code for code, _ in ROUTE_PERMISSIONS)

ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    # `admin` holds every code by construction in the seed script; keep the
    # migration in agreement rather than relying on the guard's admin
    # short-circuit, which is an authorization shortcut and not a grant.
    "admin": _ALL_ROUTE_PERMISSION_CODES,
    # Derived from `fa:assets:read`.
    "auditor": ("fa:assets:import:read",),
    "finance_director": (
        # from `banking:accounts:update`
        "banking:account:update",
        # from `banking:statements:import`
        "banking:statement:create",
        # from `fa:assets:read`
        "fa:assets:import:read",
        # from `fa:assets:create`
        "fa:assets:import:preview",
        "fa:assets:import:execute",
        # from `payments:intents:create` / `payments:intents:read`
        "payments:invoice:initialize",
        "payments:verify",
    ),
    "finance_manager": (
        # from `banking:accounts:update`
        "banking:account:update",
        # from `banking:statements:import`
        "banking:statement:create",
        # from `fa:assets:read`
        "fa:assets:import:read",
        # from `fa:assets:create`
        "fa:assets:import:preview",
        "fa:assets:import:execute",
    ),
    # from `banking:statements:import`
    "senior_accountant": ("banking:statement:create",),
    # Derived from `fa:assets:read`.
    "finance_viewer": ("fa:assets:import:read",),
    "inventory_manager": ("fa:assets:import:read",),
    "asset_viewer": ("fa:assets:import:read",),
    "asset_manager": (
        # from `fa:assets:read`
        "fa:assets:import:read",
        # from `fa:assets:create`
        "fa:assets:import:preview",
        "fa:assets:import:execute",
    ),
    "asset_custodian": (
        # from `fa:assets:read`
        "fa:assets:import:read",
        # from `fa:assets:create`
        "fa:assets:import:preview",
        "fa:assets:import:execute",
    ),
    # No sibling exists for the `people:` family; see the module docstring for
    # why this is one role and not the eleven that hold `hr:employees:read`.
    "hr_director": ("people:read", "people:write"),
}


def _ensure_active_role(conn: Any, role_name: str, description: str) -> Any:
    conn.exec_driver_sql(
        """
        INSERT INTO roles (id, name, description, is_active, created_at, updated_at)
        VALUES (gen_random_uuid(), %s, %s, true, NOW(), NOW())
        ON CONFLICT (name) DO NOTHING
        """,
        (role_name, description),
    )
    row = conn.exec_driver_sql(
        "SELECT id, is_active FROM roles WHERE name = %s",
        (role_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Route RBAC role was not materialized: {role_name}")
    if not row[1]:
        raise RuntimeError(f"Route RBAC role is inactive: {role_name}")
    return row[0]


def _ensure_active_permission(conn: Any, code: str, description: str) -> Any:
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
        raise RuntimeError(f"Route RBAC permission was not materialized: {code}")
    if not row[1]:
        raise RuntimeError(f"Route RBAC permission is inactive: {code}")
    return row[0]


def upgrade() -> None:
    conn = op.get_bind()
    role_ids = {
        role_name: _ensure_active_role(conn, role_name, description)
        for role_name, description in ROLE_DESCRIPTIONS.items()
    }
    permission_ids = {
        code: _ensure_active_permission(conn, code, description)
        for code, description in ROUTE_PERMISSIONS
    }

    for role_name, permission_codes in ROLE_GRANTS.items():
        for permission_code in permission_codes:
            conn.exec_driver_sql(
                """
                INSERT INTO role_permissions (id, role_id, permission_id)
                VALUES (gen_random_uuid(), %s, %s)
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """,
                (role_ids[role_name], permission_ids[permission_code]),
            )


def downgrade() -> None:
    # Additive baseline grants cannot be distinguished from operator grants in
    # the current schema.  Removing them would therefore be destructive.
    pass
