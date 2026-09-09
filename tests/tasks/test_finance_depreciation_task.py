"""Tests for monthly fixed-asset depreciation automation task.

The task used to open one ``cross_org_session``, ask it whether automation was
enabled, and list every active organization through it. ``cross_org_session``
lifts only the SQLAlchemy listener and never PostgreSQL RLS, so under
``app_user`` that listing returns zero organizations — and a depreciation job
that finds no organizations reports a clean run having posted nothing.

Discovery now goes through the tenant catalogue, and the automation switches
are read inside each tenant's own session, which is where a
``SettingDomain.automation`` value was always meant to resolve. These tests
patch that seam, not the retired one.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.db import session_context
from app import tenant_catalog
from app.tasks import finance

_SERVICE = "app.services.fixed_assets.depreciation.DepreciationService"


@pytest.fixture
def tenant_sessions(monkeypatch):
    """Enumerate three organizations, recording the session opened for each.

    The seam patched is ``for_each_organization``'s own — the catalogue definer
    and ``session_for_org`` in :mod:`app.db.session_context` — rather than a
    name bound in :mod:`app.tasks.finance`. Patching the helper itself would let
    the task keep working with a fan-out that never opened a tenant session at
    all, which is the whole property these tests exist to hold.
    """
    org_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    opened: list[uuid.UUID] = []
    sessions = {org_id: MagicMock(name=f"db:{org_id}") for org_id in org_ids}
    discovery: dict[str, object] = {}

    @contextmanager
    def session_for_org(organization_id):
        opened.append(organization_id)
        yield sessions[organization_id]

    def organization_ids(**kwargs):
        discovery.update(kwargs)
        return list(org_ids)

    monkeypatch.setattr(session_context, "session_for_org", session_for_org)
    monkeypatch.setattr(tenant_catalog, "organization_ids", organization_ids)
    return org_ids, opened, sessions


def test_the_depreciation_fan_out_is_active_tenants_only(tenant_sessions) -> None:
    """The scan this replaced filtered ``Organization.is_active``.

    ``DepreciationService.list_active_organization_ids`` narrowed to active
    organizations, so the default ``include_inactive=False`` is what preserves
    its reach. A deactivated tenant does not start new depreciation runs.
    """
    captured: dict[str, object] = {}
    real = tenant_catalog.organization_ids

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    with (
        patch.object(tenant_catalog, "organization_ids", spy),
        patch(f"{_SERVICE}.automation_enabled", return_value=False),
    ):
        finance.process_monthly_depreciation_runs()

    assert captured.get("include_inactive") is False


class TestProcessMonthlyDepreciationRuns:
    """Tests for the monthly fixed-assets depreciation automation task."""

    def test_skips_when_automation_disabled(self, tenant_sessions) -> None:
        """Task should no-op cleanly when automation is turned off.

        Every organization is still visited — the switch is per tenant — but
        none of them is counted or run.
        """
        org_ids, opened, _ = tenant_sessions

        with (
            patch(f"{_SERVICE}.automation_enabled", return_value=False),
            patch(f"{_SERVICE}.create_automated_monthly_run") as run_mock,
        ):
            result = finance.process_monthly_depreciation_runs()

        assert opened == org_ids
        run_mock.assert_not_called()
        assert result["automation_enabled"] is False
        assert result["organizations_checked"] == 0
        assert result["runs_calculated"] == 0
        assert result["runs_posted"] == 0

    def test_processes_active_organizations(self, tenant_sessions) -> None:
        """Task should create or post runs for each active organization."""
        with (
            patch(f"{_SERVICE}.automation_enabled", return_value=True),
            patch(f"{_SERVICE}.automation_auto_post_enabled", return_value=True),
            patch(
                f"{_SERVICE}.create_automated_monthly_run",
                side_effect=[
                    {"status": "posted"},
                    {"status": "calculated"},
                    {"status": "skipped"},
                ],
            ) as run_mock,
        ):
            result = finance.process_monthly_depreciation_runs()

        assert run_mock.call_count == 3
        assert result["automation_enabled"] is True
        assert result["auto_post"] is True
        assert result["organizations_checked"] == 3
        assert result["runs_posted"] == 1
        assert result["runs_calculated"] == 1
        assert result["skipped"] == 1

    def test_the_automation_switch_is_read_in_each_tenants_session(
        self, tenant_sessions
    ) -> None:
        """The switch is a per-tenant setting, so it is resolved per tenant.

        Read on the old unscoped session it answered for no particular tenant.
        Read here it answers for the one whose runs it is about to gate. The
        retired cross-tenant listing must not come back either — asserting it is
        never called is what stops a future edit reintroducing the scan that is
        invisible to RLS under ``app_user``.
        """
        org_ids, _, sessions = tenant_sessions

        with (
            patch(f"{_SERVICE}.automation_enabled", return_value=True) as enabled_mock,
            patch(f"{_SERVICE}.automation_auto_post_enabled", return_value=False),
            patch(
                f"{_SERVICE}.create_automated_monthly_run",
                return_value={"status": "skipped"},
            ),
            patch(f"{_SERVICE}.list_active_organization_ids") as legacy_listing,
        ):
            finance.process_monthly_depreciation_runs()

        assert [call.args[0] for call in enabled_mock.call_args_list] == [
            sessions[org_id] for org_id in org_ids
        ]
        legacy_listing.assert_not_called()

    def test_each_run_is_created_in_its_own_tenant_session(
        self, tenant_sessions
    ) -> None:
        """A tenant's depreciation run is never created on another's session."""
        org_ids, _, sessions = tenant_sessions
        seen: list[tuple[object, uuid.UUID]] = []

        def record(db, organization_id, **_kwargs):
            seen.append((db, organization_id))
            return {"status": "skipped"}

        with (
            patch(f"{_SERVICE}.automation_enabled", return_value=True),
            patch(f"{_SERVICE}.automation_auto_post_enabled", return_value=False),
            patch(f"{_SERVICE}.create_automated_monthly_run", side_effect=record),
        ):
            finance.process_monthly_depreciation_runs()

        assert seen == [(sessions[org_id], org_id) for org_id in org_ids]
