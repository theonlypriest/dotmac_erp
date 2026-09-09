from __future__ import annotations

import ast
from pathlib import Path

MIGRATION_PATH = Path("alembic/versions/20260828_seed_siwes_intern_designation.py")


def _module() -> ast.Module:
    return ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))


def _executed_sql(function_name: str) -> str:
    statements: list[str] = []
    for node in ast.walk(_module()):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute"
                    and call.args
                    and isinstance(call.args[0], ast.JoinedStr)
                ):
                    statements.append(
                        "".join(
                            part.value
                            for part in call.args[0].values
                            if isinstance(part, ast.Constant)
                            and isinstance(part.value, str)
                        )
                    )
    return "\n".join(statements)


def test_siwes_designation_migration_uses_expected_revision_chain():
    module = _module()
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert assignments["revision"] == "20260828_seed_siwes_designation"
    assert assignments["down_revision"] == "20260826_hr_shift_scheduler"
    assert len(assignments["revision"]) <= 32


def test_siwes_designation_migration_seeds_one_active_row_per_org():
    sql = _executed_sql("upgrade")

    assert "INSERT INTO hr.designation" in sql
    assert "FROM core_org.organization AS organization" in sql
    assert "'SIWES-INTERN'" in sql
    assert "'SIWES Intern'" in sql
    assert "is_active" in sql
    assert "NOT EXISTS" in sql
    assert "existing.organization_id = organization.organization_id" in sql


def test_siwes_designation_migration_reuses_existing_exact_matches():
    sql = _executed_sql("upgrade")

    assert "designation_code IN ('SIWES-INTERN', 'SIWES_INTERN')" in sql
    assert "LOWER(designation.designation_name) = 'siwes intern'" in sql
    assert "designation_name = 'SIWES Intern'" in sql


def test_siwes_designation_migration_reuses_existing_siwes_semantics():
    sql = _executed_sql("upgrade")

    assert "designation.designation_code = 'SIWES'" in sql
    assert "LOWER(designation.designation_name) = 'siwes'" in sql
    assert "designation_name = 'SIWES Intern'" in sql


def test_siwes_designation_migration_downgrade_is_non_destructive():
    sql = _executed_sql("downgrade").upper()

    assert "DELETE" not in sql
    assert "UPDATE" not in sql
    assert "DROP" not in sql
