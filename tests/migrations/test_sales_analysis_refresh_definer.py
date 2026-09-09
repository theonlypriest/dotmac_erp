from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    PROJECT_ROOT / "alembic" / "versions" / "20260828_sales_analysis_refresh_definer.py"
)


def test_refresh_definer_is_fixed_purpose_and_owned_by_app_admin() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert source.count("CREATE OR REPLACE FUNCTION") == 1
    assert "SECURITY DEFINER" in source
    assert "SET search_path = ''" in source
    assert source.count("rpt.sales_analysis_mv") >= 2
    assert "ALTER FUNCTION {SIGNATURE} OWNER TO app_admin" in source
    assert "REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION {SIGNATURE} TO app_user" in source


def test_refresh_definer_cannot_accept_a_relation_name() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    function_sql = source.split('_CREATE_FUNCTION = """', 1)[1].split('"""', 1)[0]

    assert "view_name" not in function_sql
    assert "regclass" not in function_sql
