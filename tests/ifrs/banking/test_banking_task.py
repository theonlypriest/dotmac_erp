"""
Tests for banking Celery tasks.

The auto-match task is the canonical demo of tenant fan-out: discovery through
the narrow ``tenant_catalog`` definer, then one ``session_for_org`` per unit of
work. These tests lock that contract — they assert *which helpers are called
and how*, not row counts, because zero rows is a legitimate outcome.

They previously locked the older ``cross_org_session`` + ``session_for_org``
pair. That pair had a specific failure mode: ``cross_org_session`` lifts only
the SQLAlchemy listener and never PostgreSQL RLS, so the cross-tenant listing
returns zero statements under ``app_user`` and the task reports a successful run
having matched nothing. The listing now happens inside each tenant's own
session, where both isolation layers apply.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── Helpers ──────────────────────────────────────────────────────────


def _mock_match_result(
    matched: int = 0,
    skipped: int = 0,
    errors: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        matched=matched,
        skipped=skipped,
        errors=errors or [],
    )


def _fake_tenant_sessions(statements_by_org):
    """Build a ``session_for_org`` stand-in plus its two recorders.

    Every session it opens answers the listing query with that organization's
    statement ids — so a session opened for org A can never see org B's work,
    which is the property the database enforces and this fake must not undo.

    Returns ``(factory, opened, sessions)``: ``opened`` is the org id of each
    open in order, ``sessions`` the session objects in the same order. The task
    lists every tenant first and closes each listing session before it opens the
    first per-statement session, so the opens are all the listings in tenant
    order, then all the per-statement sessions in the same order.
    """
    opened: list[uuid.UUID] = []
    sessions: list[MagicMock] = []

    @contextmanager
    def factory(org_id):
        db = MagicMock(name=f"db:{org_id}")
        db.scalars.return_value.all.return_value = statements_by_org.get(org_id, [])
        opened.append(org_id)
        sessions.append(db)
        yield db

    return factory, opened, sessions


def _patch_fanout(org_ids, session_factory):
    """Patch both halves of the fan-out: catalogue discovery and the session."""
    return (
        patch("app.tenant_catalog.organization_ids", lambda **_: list(org_ids)),
        patch("app.db.session_context.session_for_org", session_factory),
    )


# ── Tests ────────────────────────────────────────────────────────────


class TestAutoMatchUnreconciledStatements:
    """Tests for the auto_match_unreconciled_statements Celery task."""

    def test_no_unmatched_statements(self) -> None:
        """Returns zero counts when no statements have unmatched lines.

        Each tenant is still visited — that is the only way to find out it has
        nothing to do — but no per-statement session is opened.
        """
        from app.tasks.banking import auto_match_unreconciled_statements

        org_id = uuid.uuid4()
        factory, opened, sessions = _fake_tenant_sessions({org_id: []})
        catalog_patch, session_patch = _patch_fanout([org_id], factory)

        with (
            catalog_patch,
            session_patch,
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ),
        ):
            result = auto_match_unreconciled_statements()

        assert result["statements_processed"] == 0
        assert result["total_matched"] == 0
        assert result["errors"] == []
        # One session: the listing. Nothing to match, so no work session.
        assert opened == [org_id]
        assert sessions[0].commit.call_count == 0

    def test_processes_multiple_statements(self) -> None:
        """Per-statement session; counts accumulate; commit fires once per
        statement (no shared commit across statements)."""
        from app.tasks.banking import auto_match_unreconciled_statements

        org_id = uuid.uuid4()
        stmt1, stmt2 = uuid.uuid4(), uuid.uuid4()
        factory, opened, sessions = _fake_tenant_sessions({org_id: [stmt1, stmt2]})
        catalog_patch, session_patch = _patch_fanout([org_id], factory)

        with (
            catalog_patch,
            session_patch,
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.auto_match_statement.side_effect = [
                _mock_match_result(matched=3, skipped=2),
                _mock_match_result(matched=1, skipped=4),
            ]
            result = auto_match_unreconciled_statements()

        assert result["statements_processed"] == 2
        assert result["total_matched"] == 4
        assert result["errors"] == []
        # sessions[0] listed; sessions[1:] did the work, one commit each.
        assert opened == [org_id, org_id, org_id]
        assert sessions[0].commit.call_count == 0
        assert sessions[1].commit.call_count == 1
        assert sessions[2].commit.call_count == 1

    def test_per_statement_failure_isolation(self) -> None:
        """A failure in one statement must not affect others. Isolation is
        structural — separate session, separate transaction."""
        from app.tasks.banking import auto_match_unreconciled_statements

        org_id = uuid.uuid4()
        stmt1, stmt2 = uuid.uuid4(), uuid.uuid4()
        factory, _, sessions = _fake_tenant_sessions({org_id: [stmt1, stmt2]})
        catalog_patch, session_patch = _patch_fanout([org_id], factory)

        with (
            catalog_patch,
            session_patch,
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.auto_match_statement.side_effect = [
                _mock_match_result(matched=2),
                RuntimeError("DB exploded"),
            ]
            result = auto_match_unreconciled_statements()

        # First statement committed successfully in its own session.
        assert result["statements_processed"] == 1
        assert result["total_matched"] == 2
        # Second statement's error is recorded but doesn't crash the task.
        assert len(result["errors"]) == 1
        assert "DB exploded" in result["errors"][0]
        # First-statement session committed once; second never reached commit.
        assert sessions[1].commit.call_count == 1
        assert sessions[2].commit.call_count == 0

    def test_every_query_runs_inside_a_tenant_session(self) -> None:
        """The tenant fan-out contract, asserted directly.

        Regression for the 2026-05-16 finding where the matcher ran on an
        un-primed session and every join silently returned zero, and for the
        ``app_user`` cutover failure where the cross-tenant listing returns zero
        statements because it never bypassed RLS in the first place. The
        canonical entry-point pattern is:

        1. Enumerate tenants through ``for_each_organization``, itself backed by
           ``app.tenant_catalog.organization_ids``.
        2. Do *all* work — including finding the work — inside
           ``session_for_org(org_id)``.

        Asserting the helpers themselves (not the underlying primitives) is what
        makes this regression-proof: if a future refactor moves the listing back
        outside a tenant session, this fails.
        """
        from app.tasks.banking import auto_match_unreconciled_statements

        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        stmt_a, stmt_b = uuid.uuid4(), uuid.uuid4()
        factory, opened, sessions = _fake_tenant_sessions(
            {org_a: [stmt_a], org_b: [stmt_b]}
        )
        catalog_patch, session_patch = _patch_fanout([org_a, org_b], factory)
        matched_in: list[tuple[object, uuid.UUID, uuid.UUID]] = []

        with (
            catalog_patch,
            session_patch,
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ) as mock_svc_cls,
        ):

            def auto_match(db, org_id, stmt_id):
                matched_in.append((db, org_id, stmt_id))
                return _mock_match_result(matched=1)

            mock_svc_cls.return_value.auto_match_statement.side_effect = auto_match
            auto_match_unreconciled_statements()

        # Both listing sessions, then both work sessions: every listing session
        # is closed before the matcher is handed one.
        assert opened == [org_a, org_b, org_a, org_b]
        # Each statement was matched on a session opened for its OWN org — the
        # isolation the cross-tenant listing used to have to maintain by hand.
        assert matched_in == [
            (sessions[2], org_a, stmt_a),
            (sessions[3], org_b, stmt_b),
        ]

    def test_deactivated_tenants_are_still_reconciled(self) -> None:
        """Discovery must include inactive organizations.

        The listing this replaced had no ``Organization`` predicate, so it saw
        deactivated tenants' statements. Their bank statements still have to
        reconcile, so narrowing to active tenants here would silently drop
        settlement work.
        """
        from app.tasks.banking import auto_match_unreconciled_statements

        seen_kwargs: dict[str, object] = {}

        def fake_organization_ids(**kwargs):
            seen_kwargs.update(kwargs)
            return []

        with (
            patch("app.tenant_catalog.organization_ids", fake_organization_ids),
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ),
        ):
            auto_match_unreconciled_statements()

        assert seen_kwargs.get("include_inactive") is True

    def test_match_errors_appended_to_results(self) -> None:
        """Per-line errors from auto_match are propagated to task results."""
        from app.tasks.banking import auto_match_unreconciled_statements

        org_id = uuid.uuid4()
        stmt = uuid.uuid4()
        factory, _, _ = _fake_tenant_sessions({org_id: [stmt]})
        catalog_patch, session_patch = _patch_fanout([org_id], factory)

        with (
            catalog_patch,
            session_patch,
            patch(
                "app.services.finance.banking.auto_reconciliation.AutoReconciliationService"
            ) as mock_svc_cls,
        ):
            mock_svc_cls.return_value.auto_match_statement.return_value = (
                _mock_match_result(matched=1, errors=["Line 3: amount mismatch"])
            )
            result = auto_match_unreconciled_statements()

        assert result["total_matched"] == 1
        assert len(result["errors"]) == 1
        assert "Line 3" in result["errors"][0]
