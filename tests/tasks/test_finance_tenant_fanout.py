"""The finance sweeps fan out over tenants instead of scanning across them.

Three finance jobs shared one shape: open a ``cross_org_session``, run a
cross-tenant ``SELECT ... DISTINCT organization_id`` over a work table to find
out who has work, then re-open a tenant session for each answer.

``cross_org_session`` lifts only the SQLAlchemy listener, never PostgreSQL RLS.
Under ``app_user`` that first SELECT returns **zero rows**, so:

* ``refresh_stale_balances`` refreshes nothing and every reporting balance
  silently goes stale;
* ``release_expired_stock_reservations`` releases nothing and expired
  reservations hold stock forever;
* ``sync_mono_transactions`` finds no linked accounts and logs "No Mono-linked
  bank accounts found" for a fleet full of them.

Each reports success. That is the whole failure mode: a job with no work and a
job that cannot see its work are indistinguishable from the outside.

The service calls take no ``organization_id`` — they scope through the session —
so inside ``session_for_org`` they already see exactly one tenant's work, and
the discovery query is not replaced, it is deleted.
"""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.db import session_context
from app import tenant_catalog
from app.tasks import finance

ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _function_source(module, name: str) -> str:
    """Return one function's source text, by name, from its module file.

    Read from the file rather than via ``inspect.getsource`` because these are
    Celery ``shared_task`` proxies, not plain functions.
    """
    path = (module.__file__ or "").replace(".pyc", ".py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{name} not found in {path}")


def _function_node(module, name: str) -> ast.FunctionDef:
    """The parsed function, by name, from its module file."""
    path = (module.__file__ or "").replace(".pyc", ".py")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _referenced_names(node: ast.AST) -> frozenset[str]:
    """Every name a function actually REFERENCES, from its AST.

    Deliberately not a substring scan of the source text. The retired seam is
    named in the explanatory comments of the very functions that retired it —
    that comment is the reason the conversion is legible to the next reader —
    and a text scan cannot tell an explanation from a call. It would force the
    documentation out to keep the guard quiet, which is the wrong trade.

    Catches every spelling that can actually reach the callable: a bare
    ``cross_org_session()``, an attribute ``session_context.cross_org_session``,
    and a ``from ... import cross_org_session`` binding, function-local imports
    included.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.ImportFrom):
            for alias in child.names:
                names.add(alias.asname or alias.name)
    return frozenset(names)


@pytest.fixture
def tenant_sessions(monkeypatch):
    """Two organizations, each with its own recorded session.

    ``for_each_organization``'s own seam is patched — the catalogue definer and
    ``session_for_org`` in :mod:`app.db.session_context` — not the helper
    itself. Patching the helper would let a fan-out that never opened a tenant
    session pass, which is exactly the shape these tests exist to refuse.
    ``discovery`` records the kwargs each task enumerated with.
    """
    opened: list[uuid.UUID] = []
    sessions = {ORG_A: MagicMock(name="db:org-a"), ORG_B: MagicMock(name="db:org-b")}
    discovery: dict[str, object] = {}

    @contextmanager
    def session_for_org(organization_id):
        opened.append(organization_id)
        yield sessions[organization_id]

    def organization_ids(**kwargs):
        discovery.update(kwargs)
        return [ORG_A, ORG_B]

    monkeypatch.setattr(session_context, "session_for_org", session_for_org)
    monkeypatch.setattr(tenant_catalog, "organization_ids", organization_ids)
    return opened, sessions, discovery


# ── The enumeration itself ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("task", "kwargs"),
    [
        ("refresh_stale_balances", {"batch_size": 10}),
        ("release_expired_stock_reservations", {"batch_size": 10}),
        ("sync_mono_transactions", {}),
    ],
)
def test_the_converted_sweeps_enumerate_inactive_tenants_too(
    tenant_sessions, monkeypatch, task: str, kwargs: dict
) -> None:
    """Deactivated tenants must stay in reach.

    The queries this replaced had no ``Organization`` predicate at all, so they
    saw deactivated tenants' rows. Settlement-shaped work has to keep seeing
    them: a tenant switched off yesterday still has balances to refresh,
    reservations to release and a linked account with settled lines to pull.
    The default ``for_each_organization()`` would narrow that silently, which is
    why every one of these sweeps passes ``include_inactive=True`` explicitly
    and every one of them is checked.
    """
    _, sessions, discovery = tenant_sessions
    for session in sessions.values():
        session.scalars.return_value.all.return_value = []
    monkeypatch.setattr(finance, "sync_mono_account", MagicMock())

    with (
        patch(
            "app.services.finance.gl.balance_refresh.BalanceRefreshService",
            return_value=MagicMock(
                **{
                    "process_queue.return_value": {
                        "processed": 0,
                        "refreshed": 0,
                        "errors": 0,
                    }
                }
            ),
        ),
        patch(
            "app.services.inventory.stock_reservation.StockReservationService",
            return_value=MagicMock(
                **{
                    "release_expired.return_value": {
                        "checked": 0,
                        "released": 0,
                        "errors": 0,
                    }
                }
            ),
        ),
    ):
        getattr(finance, task)(**kwargs)

    assert discovery == {"include_inactive": True, "only": None}


# ── refresh_stale_balances ───────────────────────────────────────────


def test_stale_balances_are_refreshed_inside_each_tenants_session(
    tenant_sessions,
) -> None:
    """The refresh service is built from the tenant session, once per tenant."""
    opened, sessions, _ = tenant_sessions
    built_from: list[object] = []

    def service_factory(db):
        built_from.append(db)
        service = MagicMock()
        service.process_queue.return_value = {
            "processed": 1,
            "refreshed": 1,
            "errors": 0,
        }
        return service

    with patch(
        "app.services.finance.gl.balance_refresh.BalanceRefreshService",
        side_effect=service_factory,
    ):
        result = finance.refresh_stale_balances(batch_size=10)

    assert opened == [ORG_A, ORG_B]
    assert built_from == [sessions[ORG_A], sessions[ORG_B]]
    assert result == {"processed": 2, "refreshed": 2, "errors": 0}


def test_stale_balance_refresh_stops_when_the_batch_budget_is_spent(
    tenant_sessions,
) -> None:
    """``batch_size`` still bounds the work, not the number of tenants.

    It used to double as a ``LIMIT`` on the cross-tenant org listing. Now it is
    only what it says: a budget of queue entries, spent across tenants in turn.
    """
    opened, _sessions, _ = tenant_sessions
    service = MagicMock()
    service.process_queue.return_value = {"processed": 5, "refreshed": 5, "errors": 0}

    with patch(
        "app.services.finance.gl.balance_refresh.BalanceRefreshService",
        return_value=service,
    ):
        result = finance.refresh_stale_balances(batch_size=5)

    assert opened == [ORG_A], "budget exhausted by the first tenant"
    service.process_queue.assert_called_once_with(batch_size=5)
    assert result["processed"] == 5


# ── release_expired_stock_reservations ───────────────────────────────


def test_expired_reservations_are_released_inside_each_tenants_session(
    tenant_sessions,
) -> None:
    opened, sessions, _ = tenant_sessions
    built_from: list[object] = []

    def service_factory(db):
        built_from.append(db)
        service = MagicMock()
        service.release_expired.return_value = {
            "checked": 2,
            "released": 1,
            "errors": 0,
        }
        return service

    with patch(
        "app.services.inventory.stock_reservation.StockReservationService",
        side_effect=service_factory,
    ):
        result = finance.release_expired_stock_reservations(batch_size=100)

    assert opened == [ORG_A, ORG_B]
    assert built_from == [sessions[ORG_A], sessions[ORG_B]]
    assert result == {"checked": 4, "released": 2, "errors": 0}


# ── sync_mono_transactions ───────────────────────────────────────────


def test_mono_accounts_are_listed_inside_each_tenants_session(
    tenant_sessions, monkeypatch
) -> None:
    """Every linked account is found, and each tenant's list comes from its own
    session — the sweep never reads a bank account through a bypass again."""
    opened, sessions, _ = tenant_sessions
    sessions[ORG_A].scalars.return_value.all.return_value = ["mono-a1", "mono-a2"]
    sessions[ORG_B].scalars.return_value.all.return_value = ["mono-b1"]

    synced: list[tuple[str, bool]] = []

    def fake_sync_account(mono_account_id, *, refresh_first=False, **_):
        synced.append((mono_account_id, refresh_first))
        return {"success": True, "transactions_synced": 3}

    monkeypatch.setattr(finance, "sync_mono_account", fake_sync_account)

    result = finance.sync_mono_transactions()

    assert opened == [ORG_A, ORG_B]
    assert synced == [
        ("mono-a1", True),
        ("mono-a2", True),
        ("mono-b1", True),
    ]
    assert result["accounts_synced"] == 3
    assert result["total_transactions"] == 9


def test_mono_sweep_reports_nothing_to_do_only_when_there_is_nothing(
    tenant_sessions, monkeypatch
) -> None:
    """The "no accounts" answer now means it, because the listing was scoped.

    Before, that same answer was what an RLS-blocked read looked like.
    """
    opened, sessions, _ = tenant_sessions
    for session in sessions.values():
        session.scalars.return_value.all.return_value = []
    monkeypatch.setattr(
        finance, "sync_mono_account", MagicMock(name="must-not-be-called")
    )

    result = finance.sync_mono_transactions()

    assert opened == [ORG_A, ORG_B], "every tenant was asked before concluding"
    assert result == {
        "success": True,
        "accounts_synced": 0,
        "message": "No Mono-linked bank accounts found",
    }
    finance.sync_mono_account.assert_not_called()


def test_one_bad_account_does_not_starve_the_rest_of_the_sweep(
    tenant_sessions, monkeypatch
) -> None:
    """Per-account isolation survives the conversion."""
    _, sessions, _ = tenant_sessions
    sessions[ORG_A].scalars.return_value.all.return_value = ["mono-a1"]
    sessions[ORG_B].scalars.return_value.all.return_value = ["mono-b1"]

    def fake_sync_account(mono_account_id, **_):
        if mono_account_id == "mono-a1":
            raise RuntimeError("Mono is down")
        return {"success": True, "transactions_synced": 1}

    monkeypatch.setattr(finance, "sync_mono_account", fake_sync_account)

    result = finance.sync_mono_transactions()

    assert result["accounts_synced"] == 1
    assert result["accounts_failed"] == 1
    assert result["errors"] == ["Mono is down"]


# ── The retired seam ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "task_name",
    [
        "process_monthly_depreciation_runs",
        "refresh_stale_balances",
        "release_expired_stock_reservations",
        "sync_mono_transactions",
    ],
)
def test_the_converted_sweeps_no_longer_reach_across_tenants(task_name: str) -> None:
    """The bypass is gone from each converted sweep, not merely unused.

    Asserted per function rather than per module on purpose: this module still
    has three deliberate ``cross_org_session`` callers, and they are a different
    shape. ``_resolve_report_instance_org`` and ``sync_mono_account`` resolve
    *which tenant owns one given row*, and ``refresh_analysis_cubes`` has a
    genuine org-less cube. Tenant resolution is not tenant enumeration, it has
    no contract yet, and forcing it into this one would be a guess.
    """
    referenced = _referenced_names(_function_node(finance, task_name))

    assert "cross_org_session" not in referenced, (
        f"{task_name} still references cross_org_session. Under app_user its "
        "discovery query returns zero rows, so the sweep silently processes "
        "nothing and exits 0."
    )
    assert "for_each_organization" in referenced, (
        f"{task_name} does not reach for_each_organization, so it is not "
        "enumerating tenants through the SECURITY DEFINER catalogue."
    )


@pytest.mark.parametrize(
    "task_name",
    ["sync_mono_account", "refresh_analysis_cubes", "_resolve_report_instance_org"],
)
def test_the_retired_seam_detector_still_fires(task_name: str) -> None:
    """The sensitivity proof ADR-0018 asks for, using the real deliberate callers.

    A guard that reports "no bypass here" is only worth reading if it would
    have said otherwise. These three keep ``cross_org_session`` on purpose —
    two resolve which tenant owns one given row, and ``refresh_analysis_cubes``
    has a genuine org-less cube — so they are the honest oracle: if the
    detector goes quiet on THEM, it has stopped working and the assertion above
    is passing for the wrong reason.
    """
    referenced = _referenced_names(_function_node(finance, task_name))

    assert "cross_org_session" in referenced, (
        f"{task_name} no longer references cross_org_session. If that was "
        "deliberate, move it into the converted list above; the detector must "
        "never be left with nothing it fires on."
    )
