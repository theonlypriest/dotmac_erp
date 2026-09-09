"""Add INDETERMINATE to payment_intent_status and unresolved_since column.

An outbound transfer whose outcome the system could not OBSERVE was written
FAILED — the same value Paystack's own "this payout did not happen" verdict
produces. INDETERMINATE is the vocabulary for "we do not know", and
``unresolved_since`` is when we stopped knowing (ADR-0007).

Revision ID: 20260825_pi_indeterminate
Revises: 20260826_hr_shift_scheduler
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260825_pi_indeterminate"
down_revision = "20260826_hr_shift_scheduler"
branch_labels = None
depends_on = None

TABLE_NAME = "payment_intent"
SCHEMA_NAME = "payments"
ENUM_NAME = "payment_intent_status"
NEW_VALUE = "INDETERMINATE"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(TABLE_NAME, schema=SCHEMA_NAME):
        return

    # Idempotent, and schema-qualified: the enum type lives in `payments`,
    # not in `public`, so pg_type must be joined to its namespace or this
    # would silently miss it and try to add the label twice.
    exists = bind.exec_driver_sql(
        "SELECT 1 FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        f"WHERE e.enumlabel = '{NEW_VALUE}' "
        f"AND t.typname = '{ENUM_NAME}' AND n.nspname = '{SCHEMA_NAME}'"
    ).fetchone()
    if not exists:
        op.execute(
            f'ALTER TYPE "{SCHEMA_NAME}"."{ENUM_NAME}" ADD VALUE \'{NEW_VALUE}\''
        )

    existing = {
        c["name"] for c in inspector.get_columns(TABLE_NAME, schema=SCHEMA_NAME)
    }
    if "unresolved_since" not in existing:
        op.add_column(
            TABLE_NAME,
            sa.Column(
                "unresolved_since",
                sa.DateTime(timezone=True),
                nullable=True,
                comment=("When this intent became INDETERMINATE (outcome unobserved)"),
            ),
            schema=SCHEMA_NAME,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(TABLE_NAME, schema=SCHEMA_NAME):
        return

    existing = {
        c["name"] for c in inspector.get_columns(TABLE_NAME, schema=SCHEMA_NAME)
    }
    if "unresolved_since" in existing:
        op.drop_column(TABLE_NAME, "unresolved_since", schema=SCHEMA_NAME)

    # PostgreSQL cannot remove an enum value safely, and rows may already hold
    # it. Deliberately left in place — see the same decision in
    # 20260224_add_settingdomain_banking.
