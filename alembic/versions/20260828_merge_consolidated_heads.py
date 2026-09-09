"""Merge the independent ERP migration heads included in the PR consolidation.

Revision ID: 20260828_merge_consolidated_heads
Revises: 20260828_sales_mv_refresh, 20260828_seed_siwes_designation,
20260828_tenant_catalog_grants
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence


revision = "20260828_merge_consolidated_heads"
down_revision: str | Sequence[str] | None = (
    "20260828_sales_mv_refresh",
    "20260828_seed_siwes_designation",
    "20260828_tenant_catalog_grants",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the consolidated feature lineages without changing schema."""


def downgrade() -> None:
    """The merge revision has no schema operations to reverse."""
