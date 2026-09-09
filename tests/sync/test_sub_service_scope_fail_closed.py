"""Every Sub service-principal scope family refuses an unscoped key."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.sync.dotmac_sub import (
    require_sub_ap_read_scope,
    require_sub_ap_scope,
    require_sub_domain_scope,
    require_sub_expense_scope,
    require_sub_inventory_read_scope,
    require_sub_material_read_scope,
    require_sub_material_scope,
    require_sub_po_scope,
)


@pytest.mark.parametrize(
    "guard",
    (
        require_sub_ap_scope,
        require_sub_ap_read_scope,
        require_sub_domain_scope,
        require_sub_material_scope,
        require_sub_material_read_scope,
        require_sub_inventory_read_scope,
        require_sub_expense_scope,
        require_sub_po_scope,
    ),
)
@pytest.mark.parametrize("scopes", (None, []))
def test_unscoped_service_keys_are_refused_by_every_sub_guard(guard, scopes):
    with pytest.raises(HTTPException) as exc:
        guard({"scopes": scopes})

    assert exc.value.status_code == 403


def test_sub_guard_allows_only_an_explicit_accepted_scope():
    auth = {"scopes": ["sub:material:write"]}
    assert require_sub_material_scope(auth) is auth


def test_material_bootstrap_key_includes_operational_domain_scope():
    from scripts.one_off.bootstrap_sub_material_integration import SCOPES

    assert "sub:domain:write" in SCOPES
