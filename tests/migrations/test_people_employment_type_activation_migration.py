from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from scripts.seed_rbac import DEFAULT_ROLES, HR_PERMISSIONS, ROLE_PERMISSIONS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = PROJECT_ROOT / "alembic" / "versions" / "20260828_people_et_activation.py"
EMPLOYMENT_TYPE_KEYS = {
    "hr:employment_types:read",
    "hr:employment_types:manage",
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("people_et_activation", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RowResult:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeConnection:
    def __init__(self, *, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.roles: dict[str, tuple[str, bool, str]] = {}
        self.permissions: dict[str, tuple[str, bool, str]] = {}
        self.grants: set[tuple[str, str]] = set()
        self.parity: tuple[int, int, int] = (0, 0, 0)

    def exec_driver_sql(
        self,
        statement: str,
        parameters: tuple[Any, ...] | None = None,
    ) -> _RowResult:
        sql = " ".join(statement.split())
        params = parameters or ()

        if sql.startswith("WITH catalogue AS"):
            return _RowResult(self.parity)
        if sql.startswith("INSERT INTO roles"):
            name, description = params
            self.roles.setdefault(
                str(name),
                (f"role:{name}", True, str(description)),
            )
            return _RowResult()
        if sql.startswith("SELECT id, is_active FROM roles"):
            role = self.roles.get(str(params[0]))
            return _RowResult(None if role is None else role[:2])
        if sql.startswith("INSERT INTO permissions"):
            code, description = params
            self.permissions.setdefault(
                str(code),
                (f"permission:{code}", True, str(description)),
            )
            return _RowResult()
        if sql.startswith("SELECT id, is_active FROM permissions"):
            permission = self.permissions.get(str(params[0]))
            return _RowResult(None if permission is None else permission[:2])
        if sql.startswith("INSERT INTO role_permissions"):
            self.grants.add((str(params[0]), str(params[1])))
            return _RowResult()
        raise AssertionError(f"Unexpected migration SQL: {sql}")


@pytest.fixture(autouse=True)
def _operator_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEOPLE_EMPLOYMENT_TYPE_ACTIVATION", "1")


def _seed_permissions() -> tuple[tuple[str, str], ...]:
    return tuple(item for item in HR_PERMISSIONS if item[0] in EMPLOYMENT_TYPE_KEYS)


def _seed_role_grants() -> dict[str, tuple[str, ...]]:
    return {
        role: tuple(code for code in permission_codes if code in EMPLOYMENT_TYPE_KEYS)
        for role, permission_codes in ROLE_PERMISSIONS.items()
        if any(code in EMPLOYMENT_TYPE_KEYS for code in permission_codes)
    }


def _revision_parents() -> dict[str, tuple[str, ...]]:
    """Map every ERP-lineage revision id to its ``down_revision`` parents.

    Read from the migration sources with the AST rather than a text search, so
    a merge revision's tuple of parents is followed too, and the ancestry
    assertion below survives a rebase that inserts a revision between the
    bootstrap gate and this activation.
    """
    parents: dict[str, tuple[str, ...]] = {}
    for path in (PROJECT_ROOT / "alembic" / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision: str | None = None
        down: tuple[str, ...] = ()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            else:
                continue
            if value is None:
                continue
            try:
                literal = ast.literal_eval(value)
            except ValueError:
                continue
            if "revision" in names and revision is None and isinstance(literal, str):
                revision = literal
            if "down_revision" in names:
                if isinstance(literal, str):
                    down = (literal,)
                elif isinstance(literal, (tuple, list)):
                    down = tuple(str(item) for item in literal)
                else:
                    down = ()
        if revision is not None:
            parents[revision] = down
    return parents


def test_activation_descends_the_bootstrap_gate() -> None:
    module = _load()
    assert module.revision == "20260828_people_et_activation"

    parents = _revision_parents()
    # Positive control: the parser really does see both endpoints of the walk,
    # so an ancestry miss below would be a real absence and not a parse miss.
    assert "20260828_people_et_bootstrap" in parents
    assert parents["20260828_people_et_activation"] == (module.down_revision,)

    seen: set[str] = set()
    frontier = [module.down_revision]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(parents.get(current, ()))
    assert "20260828_people_et_bootstrap" in seen


def test_the_erp_lineage_stays_single_headed_after_this_activation() -> None:
    """The activation remains ancestral to the current descriptor-pinned head."""
    parents = _revision_parents()
    assert len(parents) > 100, len(parents)
    referenced = {parent for values in parents.values() for parent in values}
    heads = sorted(revision for revision in parents if revision not in referenced)
    assert heads == ["20260902_staff_access_projection"], heads


def test_rbac_contract_is_a_frozen_exact_copy_of_the_authored_seed() -> None:
    module = _load()
    assert _seed_permissions() == module.EMPLOYMENT_TYPE_PERMISSIONS
    assert _seed_role_grants() == module.ROLE_GRANTS
    assert {
        role: description
        for role, description in DEFAULT_ROLES
        if role in module.ROLE_GRANTS
    } == module.ROLE_DESCRIPTIONS
    assert sum(map(len, module.ROLE_GRANTS.values())) == 6


def test_upgrade_refuses_without_operator_opt_in_before_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.delenv("PEOPLE_EMPLOYMENT_TYPE_ACTIVATION")
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: pytest.fail("missing opt-in must refuse before opening the migration"),
    )
    monkeypatch.setattr(
        module.op,
        "execute",
        lambda _statement: pytest.fail("missing opt-in must emit no SQL"),
    )

    with pytest.raises(RuntimeError, match="explicit operator opt-in"):
        module.upgrade()


def test_parity_query_covers_identity_tenant_and_every_authoritative_field() -> None:
    module = _load()
    normalized = " ".join(module._PARITY_SQL.split())

    assert "FULL OUTER JOIN mod_people.employment_types" in normalized
    assert "authoritative.id = legacy.employment_type_id" in normalized
    for legacy_field, authoritative_field in (
        ("organization_id", "tenant_id"),
        ("type_code", "code"),
        ("type_name", "name"),
        ("description", "description"),
        ("is_active", "is_active"),
        ("created_at", "created_at"),
        ("updated_at", "updated_at"),
    ):
        assert f"legacy.{legacy_field}" in normalized
        assert f"authoritative.{authoritative_field}" in normalized


def test_projection_fence_is_hardened_and_covers_the_authoritative_image() -> None:
    module = _load()
    function_sql = " ".join(module._PROJECTION_FENCE_FUNCTION.split())
    trigger_sql = " ".join(module._PROJECTION_FENCE_TRIGGER.split())

    assert "SECURITY DEFINER SET search_path = pg_catalog" in function_sql
    assert "FROM mod_people.employment_types AS authoritative" in function_sql
    assert "authoritative.id = NEW.employment_type_id" in function_sql
    for authoritative_field, legacy_field in (
        ("tenant_id", "organization_id"),
        ("code", "type_code"),
        ("name", "type_name"),
        ("description", "description"),
        ("is_active", "is_active"),
        ("created_at", "created_at"),
        ("updated_at", "updated_at"),
    ):
        assert (
            f"authoritative.{authoritative_field} IS NOT DISTINCT FROM "
            f"NEW.{legacy_field}"
        ) in function_sql
    assert "ERRCODE = '23514'" in function_sql
    assert (
        "BEFORE INSERT OR UPDATE ON hr.employment_type FOR EACH ROW "
        "EXECUTE FUNCTION hr.enforce_employment_type_projection()"
    ) in trigger_sql


def test_exact_both_empty_catalogues_are_a_valid_fixed_point() -> None:
    module = _load()
    connection = _FakeConnection()

    module._require_exact_catalogue_parity(connection)

    assert connection.parity == (0, 0, 0)


@pytest.mark.parametrize(
    ("parity", "expected"),
    [
        ((1, 0, 0), "legacy_only=1 authoritative_only=0 mismatched=0"),
        ((0, 1, 0), "legacy_only=0 authoritative_only=1 mismatched=0"),
        ((0, 0, 1), "legacy_only=0 authoritative_only=0 mismatched=1"),
    ],
)
def test_upgrade_refuses_catalogue_drift_before_any_activation_effect(
    monkeypatch: pytest.MonkeyPatch,
    parity: tuple[int, int, int],
    expected: str,
) -> None:
    module = _load()
    connection = _FakeConnection()
    connection.parity = parity
    statements: list[str] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: connection)
    monkeypatch.setattr(module.op, "execute", statements.append)

    with pytest.raises(RuntimeError, match=expected):
        module.upgrade()

    assert statements == [
        "LOCK TABLE hr.employment_type IN ACCESS EXCLUSIVE MODE NOWAIT",
        "LOCK TABLE mod_people.employment_types IN ACCESS EXCLUSIVE MODE NOWAIT",
    ]
    assert connection.roles == {}
    assert connection.permissions == {}
    assert connection.grants == set()


def test_upgrade_installs_projection_fence_before_exposing_projector_dml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    connection = _FakeConnection()
    statements: list[str] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: connection)
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()

    rendered = "\n".join(statements)
    assert statements[:2] == [
        "LOCK TABLE hr.employment_type IN ACCESS EXCLUSIVE MODE NOWAIT",
        "LOCK TABLE mod_people.employment_types IN ACCESS EXCLUSIVE MODE NOWAIT",
    ]
    assert statements[2] == module._PROJECTION_FENCE_FUNCTION
    assert statements[3:7] == [
        "ALTER FUNCTION hr.enforce_employment_type_projection() OWNER TO app_admin",
        "REVOKE ALL ON FUNCTION hr.enforce_employment_type_projection() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION hr.enforce_employment_type_projection() FROM app_user",
        module._PROJECTION_FENCE_TRIGGER,
    ]
    assert statements[7:9] == [
        "REVOKE EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() FROM app_user",
        "DROP FUNCTION hr.lock_employment_type_bootstrap()",
    ]
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE hr.employment_type TO app_user"
        in rendered
    )
    assert "REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER" in rendered
    assert "REVOKE REFERENCES (" in rendered
    assert "employment_type_id" in rendered
    assert "last_synced_at" in rendered


def test_upgrade_materializes_rbac_idempotently_and_preserves_operator_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    connection = _FakeConnection()
    connection.roles["admin"] = (
        "existing-admin",
        True,
        "Operator-owned description",
    )
    connection.permissions["hr:employment_types:read"] = (
        "existing-read",
        True,
        "Operator-owned description",
    )
    connection.permissions["custom:permission"] = (
        "custom-permission",
        True,
        "Custom permission",
    )
    connection.grants.add(("existing-admin", "custom-permission"))
    statements: list[str] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: connection)
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()
    first_state = (
        dict(connection.roles),
        dict(connection.permissions),
        set(connection.grants),
    )
    module.upgrade()

    expected_grants = {
        (
            connection.roles[role][0],
            connection.permissions[code][0],
        )
        for role, codes in module.ROLE_GRANTS.items()
        for code in codes
    }
    assert connection.roles["admin"][2] == "Operator-owned description"
    assert (
        connection.permissions["hr:employment_types:read"][2]
        == "Operator-owned description"
    )
    assert ("existing-admin", "custom-permission") in connection.grants
    assert connection.grants == {
        *expected_grants,
        ("existing-admin", "custom-permission"),
    }
    assert len(connection.roles) == 4
    assert len(connection.permissions) == 3
    assert first_state == (
        connection.roles,
        connection.permissions,
        connection.grants,
    )


@pytest.mark.parametrize(
    ("catalogue", "key", "message"),
    [
        ("roles", "hr_manager", "Employment Type RBAC role is inactive: hr_manager"),
        (
            "permissions",
            "hr:employment_types:manage",
            "Employment Type RBAC permission is inactive: hr:employment_types:manage",
        ),
    ],
)
def test_upgrade_refuses_to_reactivate_operator_disabled_state(
    monkeypatch: pytest.MonkeyPatch,
    catalogue: str,
    key: str,
    message: str,
) -> None:
    module = _load()
    connection = _FakeConnection()
    target = getattr(connection, catalogue)
    target[key] = (f"inactive:{key}", False, "Operator disabled")
    monkeypatch.setattr(module.op, "get_bind", lambda: connection)
    monkeypatch.setattr(module.op, "execute", lambda _statement: None)

    with pytest.raises(RuntimeError, match=message):
        module.upgrade()


def test_non_postgresql_upgrade_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    statements: list[str] = []
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: _FakeConnection(dialect="sqlite"),
    )
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()

    assert statements == []


def test_downgrade_is_forward_fix_only_and_emits_no_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setattr(
        module.op,
        "get_bind",
        lambda: pytest.fail("forward-fix downgrade must not inspect or mutate state"),
    )
    monkeypatch.setattr(
        module.op,
        "execute",
        lambda _statement: pytest.fail("forward-fix downgrade must emit no SQL"),
    )

    with pytest.raises(RuntimeError, match="forward-fix only"):
        module.downgrade()
