"""Add ERP-owned staff access projection tables.

Revision ID: 20260902_staff_access_projection
Revises: 20260902_drop_po_derived_amounts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260902_staff_access_projection"
down_revision = "20260902_drop_po_derived_amounts"
branch_labels = None
depends_on = None


PROJECTED_TABLES = (
    "hr.staff_leave_access_restriction",
    "hr.staff_account_status_projection",
)


def _protect_projected_tables() -> None:
    for table in PROJECTED_TABLES:
        policy_name = table.replace(".", "_") + "_tenant_isolation"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {policy_name}
                ON {table}
                USING (organization_id = public.app_current_tenant_id())
                WITH CHECK (organization_id = public.app_current_tenant_id())
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO app_user")


def upgrade() -> None:
    staff_leave_restriction_status = postgresql.ENUM(
        "ACTIVE",
        "CANCELLED",
        name="staff_leave_restriction_status",
        create_type=False,
    )
    staff_account_status_state = postgresql.ENUM(
        "ACTIVE",
        "INACTIVE",
        name="staff_account_status_state",
        create_type=False,
    )
    staff_leave_restriction_status.create(op.get_bind(), checkfirst=True)
    staff_account_status_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "staff_leave_access_restriction",
        sa.Column(
            "restriction_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selfcare_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "leave_application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_until", sa.Date(), nullable=False),
        sa.Column(
            "status",
            staff_leave_restriction_status,
            nullable=False,
        ),
        sa.Column("source_leave_status", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["hr.employee.employee_id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["core_org.organization.organization_id"],
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
        sa.PrimaryKeyConstraint("restriction_id"),
        sa.UniqueConstraint(
            "organization_id",
            "leave_application_id",
            name="uq_staff_leave_restriction_leave",
        ),
        schema="hr",
    )
    op.create_index(
        "idx_staff_leave_restriction_active_employee",
        "staff_leave_access_restriction",
        [
            "organization_id",
            "employee_id",
            "status",
            "effective_from",
            "effective_until",
        ],
        schema="hr",
    )
    op.create_index(
        "idx_staff_leave_restriction_active_person",
        "staff_leave_access_restriction",
        [
            "organization_id",
            "person_id",
            "status",
            "effective_from",
            "effective_until",
        ],
        schema="hr",
    )
    op.create_index(
        "idx_staff_leave_restriction_updated",
        "staff_leave_access_restriction",
        ["organization_id", "updated_at", "restriction_id"],
        schema="hr",
    )

    op.create_table(
        "staff_account_status_projection",
        sa.Column(
            "projection_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selfcare_user_id", sa.String(length=36), nullable=True),
        sa.Column("erp_employee_status", sa.String(length=30), nullable=False),
        sa.Column("state", staff_account_status_state, nullable=False),
        sa.Column(
            "source_reason",
            sa.String(length=50),
            server_default="employee_status",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["hr.employee.employee_id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["core_org.organization.organization_id"],
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
        sa.PrimaryKeyConstraint("projection_id"),
        sa.UniqueConstraint(
            "organization_id",
            "employee_id",
            name="uq_staff_account_status_employee",
        ),
        schema="hr",
    )
    op.create_index(
        "idx_staff_account_status_selfcare",
        "staff_account_status_projection",
        ["organization_id", "selfcare_user_id"],
        schema="hr",
    )
    op.create_index(
        "idx_staff_account_status_updated",
        "staff_account_status_projection",
        ["organization_id", "updated_at", "projection_id"],
        schema="hr",
    )
    _protect_projected_tables()


def downgrade() -> None:
    op.drop_index(
        "idx_staff_account_status_updated",
        table_name="staff_account_status_projection",
        schema="hr",
    )
    op.drop_index(
        "idx_staff_account_status_selfcare",
        table_name="staff_account_status_projection",
        schema="hr",
    )
    op.drop_table("staff_account_status_projection", schema="hr")

    op.drop_index(
        "idx_staff_leave_restriction_updated",
        table_name="staff_leave_access_restriction",
        schema="hr",
    )
    op.drop_index(
        "idx_staff_leave_restriction_active_person",
        table_name="staff_leave_access_restriction",
        schema="hr",
    )
    op.drop_index(
        "idx_staff_leave_restriction_active_employee",
        table_name="staff_leave_access_restriction",
        schema="hr",
    )
    op.drop_table("staff_leave_access_restriction", schema="hr")

    postgresql.ENUM(name="staff_account_status_state").drop(
        op.get_bind(),
        checkfirst=True,
    )
    postgresql.ENUM(name="staff_leave_restriction_status").drop(
        op.get_bind(),
        checkfirst=True,
    )
