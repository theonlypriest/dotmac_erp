"""PostgreSQL proof for the Employment Type activation privilege boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services.common import ValidationError
from app.services.people.hr.employment_types import EmploymentTypeService
from app.services.people.hr.organization_types import (
    EmploymentTypeCreateData,
    EmploymentTypeUpdateData,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVATION_MIGRATION = (
    PROJECT_ROOT / "alembic" / "versions" / "20260828_people_et_activation.py"
)


def _load_activation_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "people_et_activation_live",
        ACTIVATION_MIGRATION,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prime(db: Session, organization_id: UUID) -> None:
    db.info["organization_id"] = organization_id
    db.info["tenant_id"] = organization_id
    db.execute(
        text("SELECT set_config('app.current_organization_id', :value, true)"),
        {"value": str(organization_id)},
    )
    db.execute(
        text("SELECT set_config('app.current_tenant', :value, true)"),
        {"value": str(organization_id)},
    )


def _ensure_module_tenant(db: Session, organization_id: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO public.tenants "
            "(id, slug, name, is_active, created_at, updated_at) "
            "VALUES (:id, :slug, :name, true, now(), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": organization_id,
            "slug": f"erp-{organization_id}",
            "name": "Employment Type Activation Test",
        },
    )


def _create_exact_pair(db: Session, organization_id: UUID):
    _ensure_module_tenant(db, organization_id)
    _prime(db, organization_id)
    return EmploymentTypeService(db, organization_id).create_employment_type(
        EmploymentTypeCreateData(
            type_code=f"GATE-{uuid4().hex[:8]}",
            type_name="Activation Gate",
            description="Exact fixed point",
        )
    )


def _install_bootstrap_fence(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION hr.lock_employment_type_bootstrap()
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                LOCK TABLE hr.employment_type IN SHARE MODE NOWAIT;
            END
            $function$
            """
        )
    )
    db.execute(
        text("REVOKE ALL ON FUNCTION hr.lock_employment_type_bootstrap() FROM PUBLIC")
    )
    db.execute(
        text(
            "GRANT EXECUTE ON FUNCTION hr.lock_employment_type_bootstrap() TO app_user"
        )
    )


def _restore_pre_activation_state(db: Session) -> None:
    db.execute(
        text(
            "DROP TRIGGER IF EXISTS enforce_employment_type_projection "
            "ON hr.employment_type"
        )
    )
    db.execute(text("DROP FUNCTION IF EXISTS hr.enforce_employment_type_projection()"))
    _install_bootstrap_fence(db)
    db.execute(text("REVOKE INSERT, UPDATE ON hr.employment_type FROM app_user"))


def _activation_effect_state(db: Session) -> tuple[object, ...]:
    return (
        db.execute(
            text("SELECT to_regprocedure('hr.lock_employment_type_bootstrap()')")
        ).scalar_one(),
        db.execute(
            text("SELECT to_regprocedure('hr.enforce_employment_type_projection()')")
        ).scalar_one(),
        db.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_trigger "
                "WHERE tgrelid = 'hr.employment_type'::regclass "
                "AND tgname = 'enforce_employment_type_projection' "
                "AND NOT tgisinternal)"
            )
        ).scalar_one(),
        db.execute(
            text(
                "SELECT has_table_privilege('app_user', 'hr.employment_type', 'INSERT')"
            )
        ).scalar_one(),
        db.execute(
            text(
                "SELECT has_table_privilege('app_user', 'hr.employment_type', 'UPDATE')"
            )
        ).scalar_one(),
        tuple(
            db.execute(
                text(
                    "SELECT r.name, p.key FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE p.key IN ("
                    "'hr:employment_types:read', "
                    "'hr:employment_types:manage') "
                    "ORDER BY r.name, p.key"
                )
            ).all()
        ),
    )


def _run_activation_upgrade(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_activation_migration()
    monkeypatch.setenv("PEOPLE_EMPLOYMENT_TYPE_ACTIVATION", "1")
    monkeypatch.setattr(module.op, "get_bind", db.connection)
    monkeypatch.setattr(
        module.op,
        "execute",
        lambda statement: db.execute(text(statement)),
    )
    module.upgrade()


def test_app_user_has_only_the_compatibility_projector_surface(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT has_schema_privilege('app_user', 'hr', 'USAGE')")
        )
        for privilege in ("SELECT", "INSERT", "UPDATE"):
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'app_user', 'hr.employment_type', :privilege)"
                ),
                {"privilege": privilege},
            )
        for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'app_user', 'hr.employment_type', :privilege)"
                ),
                {"privilege": privilege},
            ), f"app_user unexpectedly has {privilege} on hr.employment_type"
        assert not connection.scalar(
            text(
                "SELECT has_any_column_privilege("
                "'app_user', 'hr.employment_type', 'REFERENCES')"
            )
        )
        assert (
            connection.scalar(
                text("SELECT to_regprocedure('hr.lock_employment_type_bootstrap()')")
            )
            is None
        )
        function_contract = connection.execute(
            text(
                "SELECT owner.rolname, proc.prosecdef, proc.proconfig "
                "FROM pg_proc proc "
                "JOIN pg_namespace namespace ON namespace.oid = proc.pronamespace "
                "JOIN pg_roles owner ON owner.oid = proc.proowner "
                "WHERE namespace.nspname = 'hr' "
                "AND proc.proname = 'enforce_employment_type_projection'"
            )
        ).one()
        assert function_contract == (
            "app_admin",
            True,
            ["search_path=pg_catalog"],
        )
        assert not connection.scalar(
            text(
                "SELECT has_function_privilege("
                "'app_user', 'hr.enforce_employment_type_projection()', 'EXECUTE')"
            )
        )
        assert connection.scalar(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_trigger "
                "WHERE tgrelid = 'hr.employment_type'::regclass "
                "AND tgname = 'enforce_employment_type_projection' "
                "AND tgenabled = 'O' AND NOT tgisinternal)"
            )
        )


def test_activation_materializes_the_exact_employment_type_rbac_profile(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        permissions = connection.execute(
            text(
                "SELECT key, description, is_active FROM permissions "
                "WHERE key IN ("
                "'hr:employment_types:read', 'hr:employment_types:manage'"
                ") ORDER BY key"
            )
        ).all()
        grants = connection.execute(
            text(
                "SELECT r.name, p.key FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE p.key IN ("
                "'hr:employment_types:read', 'hr:employment_types:manage'"
                ") ORDER BY r.name, p.key"
            )
        ).all()

    assert permissions == [
        (
            "hr:employment_types:manage",
            "Manage employment types",
            True,
        ),
        (
            "hr:employment_types:read",
            "View employment types",
            True,
        ),
    ]
    assert grants == [
        ("admin", "hr:employment_types:manage"),
        ("admin", "hr:employment_types:read"),
        ("hr_director", "hr:employment_types:manage"),
        ("hr_director", "hr:employment_types:read"),
        ("hr_manager", "hr:employment_types:read"),
        ("hr_officer", "hr:employment_types:read"),
    ]


@pytest.mark.parametrize(
    ("drift_sql", "expected"),
    [
        (
            "DELETE FROM mod_people.employment_types WHERE id = :id",
            "legacy_only=1 authoritative_only=0 mismatched=0",
        ),
        (
            "DELETE FROM hr.employment_type WHERE employment_type_id = :id",
            "legacy_only=0 authoritative_only=1 mismatched=0",
        ),
        (
            "UPDATE hr.employment_type SET type_name = 'Drifted' "
            "WHERE employment_type_id = :id",
            "legacy_only=0 authoritative_only=0 mismatched=1",
        ),
    ],
)
def test_live_activation_refuses_catalogue_drift_with_zero_activation_effects(
    db: Session,
    organization: object,
    monkeypatch: pytest.MonkeyPatch,
    drift_sql: str,
    expected: str,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    view = _create_exact_pair(db, organization_id)
    _restore_pre_activation_state(db)
    db.execute(text(drift_sql), {"id": view.employment_type_id})
    before = _activation_effect_state(db)

    with pytest.raises(RuntimeError, match=expected):
        _run_activation_upgrade(db, monkeypatch)

    assert _activation_effect_state(db) == before


def test_live_activation_permits_exact_all_tenant_parity(
    db: Session,
    organization: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    _create_exact_pair(db, organization_id)
    _restore_pre_activation_state(db)

    _run_activation_upgrade(db, monkeypatch)

    after = _activation_effect_state(db)
    assert after[0] is None
    assert after[1] is not None
    assert after[2]
    assert after[3:5] == (True, True)


def test_module_command_and_compatibility_projection_share_one_transaction(
    db: Session,
    organization: object,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    _ensure_module_tenant(db, organization_id)
    _prime(db, organization_id)
    db.flush()
    db.execute(text("SET LOCAL ROLE app_user"))

    service = EmploymentTypeService(db, organization_id)
    view = service.create_employment_type(
        EmploymentTypeCreateData(
            type_code="contract",
            type_name="Contract",
            description="Fixed term",
        )
    )
    module_row = db.execute(
        text(
            "SELECT id, tenant_id, code, name, description, is_active "
            "FROM mod_people.employment_types WHERE id = :id"
        ),
        {"id": view.employment_type_id},
    ).one()
    legacy_row = db.execute(
        text(
            "SELECT employment_type_id, organization_id, type_code, type_name, "
            "description, is_active FROM hr.employment_type "
            "WHERE employment_type_id = :id"
        ),
        {"id": view.employment_type_id},
    ).one()
    assert module_row == (
        view.employment_type_id,
        organization_id,
        "CONTRACT",
        "Contract",
        "Fixed term",
        True,
    )
    assert legacy_row == (
        view.employment_type_id,
        organization_id,
        "CONTRACT",
        "Contract",
        "Fixed term",
        True,
    )

    updated = service.update_employment_type(
        view.employment_type_id,
        EmploymentTypeUpdateData(
            type_code="consultant",
            type_name="Consultant",
            description=None,
            description_is_set=True,
            is_active=False,
        ),
    )
    module_after = db.execute(
        text(
            "SELECT id, tenant_id, code, name, description, is_active, "
            "created_at, updated_at FROM mod_people.employment_types WHERE id = :id"
        ),
        {"id": view.employment_type_id},
    ).one()
    legacy_after = db.execute(
        text(
            "SELECT employment_type_id, organization_id, type_code, type_name, "
            "description, is_active, created_at, updated_at "
            "FROM hr.employment_type WHERE employment_type_id = :id"
        ),
        {"id": view.employment_type_id},
    ).one()
    assert updated.type_code == "CONSULTANT"
    assert updated.type_name == "Consultant"
    assert updated.description is None
    assert not updated.is_active
    assert legacy_after == module_after

    db.rollback()
    assert (
        db.execute(
            text("SELECT count(*) FROM mod_people.employment_types WHERE id = :id"),
            {"id": view.employment_type_id},
        ).scalar_one()
        == 0
    )
    assert (
        db.execute(
            text(
                "SELECT count(*) FROM hr.employment_type WHERE employment_type_id = :id"
            ),
            {"id": view.employment_type_id},
        ).scalar_one()
        == 0
    )


def test_projection_fence_rejects_mismatching_raw_legacy_insert_and_update(
    db: Session,
    organization: object,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    _ensure_module_tenant(db, organization_id)
    _prime(db, organization_id)
    db.flush()
    db.execute(text("SET LOCAL ROLE app_user"))
    service = EmploymentTypeService(db, organization_id)
    authoritative = service.create_employment_type(
        EmploymentTypeCreateData(type_code="PERMANENT", type_name="Permanent")
    )

    legacy_only_id = uuid4()
    attempts = (
        (
            "UPDATE hr.employment_type SET type_name = 'Drifted' "
            "WHERE employment_type_id = :id",
            {"id": authoritative.employment_type_id},
        ),
        (
            "INSERT INTO hr.employment_type "
            "(employment_type_id, organization_id, type_code, type_name, is_active) "
            "VALUES (:id, :organization_id, 'ORPHAN', 'Orphan', true)",
            {"id": legacy_only_id, "organization_id": organization_id},
        ),
    )
    for statement, parameters in attempts:
        with pytest.raises(DBAPIError) as refused:
            with db.begin_nested():
                db.execute(text(statement), parameters)
        assert getattr(refused.value.orig, "sqlstate", None) == "23514"

    assert (
        db.execute(
            text(
                "SELECT type_name FROM hr.employment_type WHERE employment_type_id = :id"
            ),
            {"id": authoritative.employment_type_id},
        ).scalar_one()
        == "Permanent"
    )
    assert (
        db.execute(
            text(
                "SELECT count(*) FROM hr.employment_type WHERE employment_type_id = :id"
            ),
            {"id": legacy_only_id},
        ).scalar_one()
        == 0
    )


def test_repair_is_idempotent_through_projection_fence(
    db: Session,
    organization: object,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    _ensure_module_tenant(db, organization_id)
    _prime(db, organization_id)
    service = EmploymentTypeService(db, organization_id)
    authoritative = service.create_employment_type(
        EmploymentTypeCreateData(type_code="PERMANENT", type_name="Permanent")
    )

    db.execute(
        text(
            "ALTER TABLE hr.employment_type "
            "DISABLE TRIGGER enforce_employment_type_projection"
        )
    )
    db.execute(
        text(
            "UPDATE hr.employment_type SET type_name = 'Drifted' "
            "WHERE employment_type_id = :id"
        ),
        {"id": authoritative.employment_type_id},
    )
    db.execute(
        text(
            "ALTER TABLE hr.employment_type "
            "ENABLE TRIGGER enforce_employment_type_projection"
        )
    )
    db.execute(text("SET LOCAL ROLE app_user"))

    assert service.repair_compatibility_projection() == 1
    assert service.repair_compatibility_projection() == 0
    assert (
        db.execute(
            text(
                "SELECT type_name FROM hr.employment_type WHERE employment_type_id = :id"
            ),
            {"id": authoritative.employment_type_id},
        ).scalar_one()
        == "Permanent"
    )


def test_repair_still_refuses_legacy_only_rows_before_writes(
    db: Session,
    organization: object,
) -> None:
    organization_id = UUID(str(organization.organization_id))
    _ensure_module_tenant(db, organization_id)
    _prime(db, organization_id)
    service = EmploymentTypeService(db, organization_id)
    authoritative = service.create_employment_type(
        EmploymentTypeCreateData(type_code="PERMANENT", type_name="Permanent")
    )
    legacy_only_id = uuid4()

    db.execute(
        text(
            "ALTER TABLE hr.employment_type "
            "DISABLE TRIGGER enforce_employment_type_projection"
        )
    )
    db.execute(
        text(
            "UPDATE hr.employment_type SET type_name = 'Drifted' "
            "WHERE employment_type_id = :id"
        ),
        {"id": authoritative.employment_type_id},
    )
    db.execute(
        text(
            "INSERT INTO hr.employment_type "
            "(employment_type_id, organization_id, type_code, type_name, is_active) "
            "VALUES (:id, :organization_id, 'ORPHAN', 'Orphan', true)"
        ),
        {"id": legacy_only_id, "organization_id": organization_id},
    )
    db.execute(
        text(
            "ALTER TABLE hr.employment_type "
            "ENABLE TRIGGER enforce_employment_type_projection"
        )
    )
    db.execute(text("SET LOCAL ROLE app_user"))

    with pytest.raises(ValidationError, match=str(legacy_only_id)):
        service.repair_compatibility_projection()
    assert (
        db.execute(
            text(
                "SELECT type_name FROM hr.employment_type WHERE employment_type_id = :id"
            ),
            {"id": authoritative.employment_type_id},
        ).scalar_one()
        == "Drifted"
    )
