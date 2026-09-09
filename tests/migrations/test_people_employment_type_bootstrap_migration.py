from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = PROJECT_ROOT / "alembic" / "versions" / "20260828_people_et_bootstrap.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("people_et_bootstrap", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_extends_the_actual_current_assembly_head() -> None:
    module = _load()
    assert module.revision == "20260828_people_et_bootstrap"
    assert module.down_revision == "20260828_merge_consolidated_heads"


def test_upgrade_grants_only_the_reviewed_read_surface(monkeypatch) -> None:
    module = _load()
    statements: list[str] = []
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()

    assert statements[:2] == [
        "GRANT USAGE ON SCHEMA hr TO app_user",
        "GRANT SELECT ON TABLE hr.employment_type TO app_user",
    ]
    assert "SECURITY DEFINER" in statements[2]
    assert "SET search_path = pg_catalog" in statements[2]
    assert "LOCK TABLE hr.employment_type IN SHARE MODE NOWAIT" in statements[2]
    assert statements[3:] == [
        "REVOKE ALL ON FUNCTION hr.lock_employment_type_bootstrap() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() TO app_user",
    ]


def test_downgrade_never_revokes_shared_schema_usage(monkeypatch) -> None:
    module = _load()
    statements: list[str] = []
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.downgrade()

    assert statements == [
        "REVOKE EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() FROM app_user",
        "DROP FUNCTION hr.lock_employment_type_bootstrap()",
        "REVOKE SELECT ON TABLE hr.employment_type FROM app_user",
    ]
