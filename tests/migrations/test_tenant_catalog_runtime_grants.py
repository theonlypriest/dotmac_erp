from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "20260828_repair_tenant_catalog_runtime_grants.py"
)
spec = importlib.util.spec_from_file_location(
    "tenant_catalog_grant_repair", MIGRATION_PATH
)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_upgrade_reasserts_only_the_narrow_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind = MagicMock()
    bind.dialect = SimpleNamespace(name="postgresql")
    bind.scalar.side_effect = ["app_admin", "app_admin", True, True]
    executed: list[str] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()

    assert executed == [
        "REVOKE ALL ON SCHEMA tenant_catalog FROM PUBLIC",
        "REVOKE ALL ON FUNCTION tenant_catalog.organization_ids(boolean) FROM PUBLIC",
        "GRANT USAGE ON SCHEMA tenant_catalog TO app_user",
        "GRANT EXECUTE ON FUNCTION tenant_catalog.organization_ids(boolean) TO app_user",
    ]


def test_upgrade_refuses_a_wrong_function_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind = MagicMock()
    bind.dialect = SimpleNamespace(name="postgresql")
    bind.scalar.side_effect = ["app_admin", "postgres"]
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="owned by"):
        migration.upgrade()
