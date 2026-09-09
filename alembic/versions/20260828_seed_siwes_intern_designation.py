"""Seed SIWES Intern designation.

Revision ID: 20260828_seed_siwes_designation
Revises: 20260826_hr_shift_scheduler
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "20260828_seed_siwes_designation"
down_revision = "20260826_hr_shift_scheduler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DESIGNATION_DESCRIPTION = "Student Industrial Work Experience Scheme intern"


def upgrade() -> None:
    """Ensure every organization has one active SIWES Intern designation."""
    op.execute(
        f"""
        UPDATE hr.designation AS designation
        SET designation_name = 'SIWES Intern',
            description = COALESCE(
                NULLIF(designation.description, ''),
                '{DESIGNATION_DESCRIPTION}'
            ),
            is_active = TRUE,
            updated_at = NOW()
        WHERE designation.designation_code IN ('SIWES-INTERN', 'SIWES_INTERN')
           OR LOWER(designation.designation_name) = 'siwes intern'
        """
    )

    op.execute(
        f"""
        UPDATE hr.designation AS designation
        SET designation_name = 'SIWES Intern',
            description = COALESCE(
                NULLIF(designation.description, ''),
                '{DESIGNATION_DESCRIPTION}'
            ),
            is_active = TRUE,
            updated_at = NOW()
        WHERE (
                designation.designation_code = 'SIWES'
                OR LOWER(designation.designation_name) = 'siwes'
            )
          AND NOT EXISTS (
                SELECT 1
                FROM hr.designation AS existing
                WHERE existing.organization_id = designation.organization_id
                  AND existing.designation_id != designation.designation_id
                  AND (
                        existing.designation_code IN (
                            'SIWES-INTERN',
                            'SIWES_INTERN'
                        )
                        OR LOWER(existing.designation_name) = 'siwes intern'
                  )
            )
        """
    )

    op.execute(
        f"""
        INSERT INTO hr.designation (
            organization_id,
            designation_code,
            designation_name,
            description,
            is_active,
            created_at
        )
        SELECT organization.organization_id,
               'SIWES-INTERN',
               'SIWES Intern',
               '{DESIGNATION_DESCRIPTION}',
               TRUE,
               NOW()
        FROM core_org.organization AS organization
        WHERE NOT EXISTS (
            SELECT 1
            FROM hr.designation AS existing
            WHERE existing.organization_id = organization.organization_id
              AND (
                    existing.designation_code IN (
                        'SIWES',
                        'SIWES-INTERN',
                        'SIWES_INTERN'
                    )
                    OR LOWER(existing.designation_name) IN (
                        'siwes',
                        'siwes intern'
                    )
              )
        )
        """
    )


def downgrade() -> None:
    """Preserve seeded designation rows that may already be referenced."""
