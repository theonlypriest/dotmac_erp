"""
Banking Background Tasks - Celery tasks for banking reconciliation workflows.

Handles:
- Periodic auto-matching of unreconciled bank statement lines
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def auto_match_unreconciled_statements() -> dict[str, Any]:
    """Periodically auto-match unreconciled statement lines.

    Scans all statements with unmatched lines and runs deterministic
    matching via two strategies:

    1. **PaymentIntent** — DotMac-initiated Paystack transfers
    2. **Splynx CustomerPayment** — Splynx-originated payments

    This catches cases where a GL journal was posted *after* the
    statement was imported, as well as backfilling matches on
    historical statements.

    Session shape (two-step, the canonical batch pattern):

    1. List unmatched statements one tenant at a time via
       :func:`for_each_organization`, materialising ``(org, statement)``
       pairs; every listing session is closed before step 2 opens one.
    2. Process each statement under its own :func:`session_for_org` —
       per-statement session, single commit, isolated failure. This avoids
       (a) the SET LOCAL clearing that would silently de-prime a
       shared session after the first commit, and (b) identity-map
       contamination between tenants on the same session.

    Step 1 previously ran as a single cross-tenant SELECT under
    :func:`cross_org_session`, which lifts only the SQLAlchemy listener and
    never PostgreSQL RLS. Under ``app_user`` that SELECT returns zero rows, so
    the job matched nothing and logged a successful run — indistinguishable
    from a fleet with nothing to match. Read inside each tenant's own session
    the same predicate is filtered by the ORM listener *and* by RLS, and the
    ``organization_id`` column no longer has to be selected: the session
    already is the organization.

    ``include_inactive=True``: the old SELECT had no ``Organization`` predicate,
    so it saw deactivated tenants' statements. A deactivated tenant's bank
    statements still have to reconcile.

    Returns:
        Dict with processing statistics.
    """
    from sqlalchemy import select

    from app.db.session_context import for_each_organization, session_for_org
    from app.models.finance.banking.bank_statement import BankStatement
    from app.services.finance.banking.auto_reconciliation import (
        AutoReconciliationService,
    )

    logger.info("Starting periodic auto-match of unreconciled statements")

    results: dict[str, Any] = {
        "statements_processed": 0,
        "total_matched": 0,
        "errors": [],
    }

    # Step 1 — per-tenant listing. Materialise the (org, statement) pairs we
    # need and let each listing session close, so no listing session is still
    # open when the matcher gets one. `for_each_organization` is deliberately
    # NOT wrapped around step 2: that would hold this tenant's listing session
    # open across every per-statement session and commit below.
    statement_meta: list[tuple[UUID, UUID]] = []
    for org_id, listing_db in for_each_organization(include_inactive=True):
        statement_meta.extend(
            (org_id, stmt_id)
            for stmt_id in listing_db.scalars(
                select(BankStatement.statement_id).where(
                    BankStatement.unmatched_lines > 0
                )
            ).all()
        )

    auto_svc = AutoReconciliationService()

    # Step 2 — per-statement matching, fresh session + single commit.
    for org_id, stmt_id in statement_meta:
        try:
            with session_for_org(org_id) as db:
                match_result = auto_svc.auto_match_statement(db, org_id, stmt_id)
                db.commit()

                if match_result.matched > 0:
                    results["total_matched"] += match_result.matched
                    logger.info(
                        "Auto-matched %d lines for statement %s (org %s)",
                        match_result.matched,
                        stmt_id,
                        org_id,
                    )

                if match_result.errors:
                    for err in match_result.errors:
                        results["errors"].append(f"Statement {stmt_id}: {err}")

                results["statements_processed"] += 1

        except Exception as e:
            logger.exception("Failed to auto-match statement %s", stmt_id)
            results["errors"].append(f"Statement {stmt_id}: {e}")

    logger.info(
        "Periodic auto-match complete: %d statements, %d lines matched",
        results["statements_processed"],
        results["total_matched"],
    )

    return results
