"""The runtime admission check READS. It may never write, and never DDL.

This step runs against production between `alembic upgrade heads` and the app
container being recreated, on the runtime credential, under an ERR trap that
rolls back code and image but NOT the database. A check placed there that could
mutate anything would be able to damage the estate it was added to inspect, at
the worst possible moment, with the migration already applied and no path back.

So the property is asserted twice over, in two independent ways:

1. **Statically, here.** Every SQL string the programme can EXECUTE is
   reconstructed and searched for a write verb. Docstrings are excluded,
   because this module and its subject both have to be able to SAY "INSERT" in
   order to explain that they never issue one.
2. **At runtime**, by `conn.read_only = True` in the entrypoint, which makes
   the SERVER refuse a write on that connection. Asserted here too, so the
   belt cannot be removed while the braces stay.

The second half of this module is the no-interpolated-identifier rule. A schema,
table or role name must never be formatted into SQL text: names travel as bound
parameters, or — for the RLS probes, where a relation has to appear in a `FROM`
clause and a parameter cannot — through `psycopg.sql.Identifier`, which is
composition performed by the driver. The detector carries its own sensitivity
proof, because a scanner that finds nothing in correct code proves nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISION = REPO_ROOT / "app" / "runtime_admission.py"
ENTRYPOINT = REPO_ROOT / "scripts" / "verify_runtime_admission.py"
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_database_roles.py"
INVENTORY = REPO_ROOT / "tests" / "integration" / "tenant_table_inventory.tsv"

#: Anything that changes the database. `GRANT`/`REVOKE` are here because this
#: check's whole job is to observe grants; a check that could also FIX one
#: would be a second, unreviewed writer of cluster access.
WRITE_VERBS = (
    "insert into",
    "update ",
    "delete from",
    "create ",
    "alter ",
    "drop ",
    "truncate",
    "grant ",
    "revoke ",
    "merge into",
    "copy ",
)


def _executable_strings(path: Path) -> list[str]:
    """Every string literal the module can EXECUTE — docstrings excluded.

    Same shape as `tests/architecture/test_database_role_contract.py`'s helper,
    and for the same reason: a check over raw file text matches the prose that
    explains why the code is safe, and so fails on correct code.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _dynamic_sql_literals(path: Path) -> list[str]:
    """f-strings and `+` concatenations that look like SQL.

    `<dynamic>` marks the interpolated part, so the literal fragments around it
    are still matched — `f"SELECT ... FROM {schema}.x"` is caught rather than
    dismissed as unreadable.
    """
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        template: str | None = None
        if isinstance(node, ast.JoinedStr):
            template = "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "<dynamic>"
                for part in node.values
            )
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = [
                child.value
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
                else "<dynamic>"
                for child in (node.left, node.right)
            ]
            if any(part != "<dynamic>" for part in parts):
                template = "".join(parts)
        if template is None or "<dynamic>" not in template:
            continue
        lowered = template.lower()
        if any(
            keyword in lowered
            for keyword in ("select ", "from ", "where ", "join ", "set_config(")
        ):
            found.append(template)
    return found


def _write_verbs_in(path: Path) -> list[str]:
    """Every write verb reachable from an executable literal, per literal.

    Scanned one literal at a time rather than over a joined blob: joining
    `("SELECT", "INSERT", "UPDATE", "DELETE")` — the privilege names this
    programme legitimately binds as parameters — manufactures the substring
    `"update "` out of two innocent neighbours, and a detector that fails on
    its own concatenation is one somebody will delete.
    """
    return [
        f"{verb!r} in {literal!r}"
        for literal in _executable_strings(path)
        for verb in WRITE_VERBS
        if verb in literal.lower()
    ]


def test_the_admission_check_executes_no_write_statement() -> None:
    for path in (DECISION, ENTRYPOINT):
        found = _write_verbs_in(path)
        assert not found, (
            f"{path.name} can execute a write: {found}. This step runs against "
            "production after the migration and before the app is recreated; "
            "it reads, and nothing else."
        )


def test_the_write_verb_detector_would_notice_real_ddl() -> None:
    """Sensitivity proof (ADR-0018).

    The exclusion of docstrings above is what makes the check usable, and it is
    also the way the check could be made blind. `scripts/bootstrap_database_roles.py`
    genuinely issues `CREATE ROLE` from an executable literal: a scanner that
    cannot see it there could not see one in the admission check either.
    """
    assert _write_verbs_in(BOOTSTRAP), (
        "the bootstrap DOES emit DDL, so a detector that reads it as clean is "
        "measuring nothing"
    )


def test_the_admission_connection_is_read_only_at_the_server() -> None:
    """The static check above is the belt; this is the braces.

    `conn.read_only = True` makes PostgreSQL itself refuse a write on this
    connection, so an edit that slipped a mutation past the AST scan would
    still fail rather than run.
    """
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "conn.read_only = True" in source
    assert "autocommit=False" in source


def test_no_sql_interpolates_a_schema_table_or_role_name() -> None:
    """Names are bound, or composed by the driver. Never formatted.

    The relations in the RLS probes are the one place a name must appear in the
    SQL text at all, because a bound parameter cannot stand in a `FROM` clause.
    They go through `psycopg.sql.Identifier`, which quotes schema and table
    itself — the same mechanism `scripts/bootstrap_database_roles.py` already
    uses for a role name.
    """
    for path in (DECISION, ENTRYPOINT):
        dynamic = _dynamic_sql_literals(path)
        assert not dynamic, (
            f"{path.name} builds SQL by interpolation: {dynamic}. Bind the "
            "value, or compose the identifier with psycopg.sql.Identifier."
        )
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    assert "sql.Identifier(module.schema, module.probe_table)" in entrypoint


def test_the_interpolation_detector_is_sensitive(tmp_path: Path) -> None:
    """Sensitivity proof: mechanically reintroduce the defect and require a hit."""
    original = ENTRYPOINT.read_text(encoding="utf-8")
    marker = 'IDENTITY_SQL = "SELECT current_user::text, session_user::text"'
    assert marker in original, "the tamper target moved; update this proof"
    broken = original.replace(
        marker, 'IDENTITY_SQL = f"SELECT current_user FROM {schema}.probe"'
    )
    tampered = tmp_path / "tampered_entrypoint.py"
    tampered.write_text(broken, encoding="utf-8")
    assert _dynamic_sql_literals(tampered)


# ---------------------------------------------------------------------------
# The declaration must describe the schema the migrations actually build
# ---------------------------------------------------------------------------


def _inventory_relations() -> dict[str, set[str]]:
    relations: dict[str, set[str]] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        schema, table = fields[0], fields[1]
        if not schema.startswith("mod_"):
            continue
        relations.setdefault(schema, set()).add(table)
    return relations


def test_every_declared_module_relation_exists_in_the_migrated_catalog() -> None:
    """The declaration in `app/runtime_admission.py` is a claim about someone
    else's artifact, so it is checked against the recorded catalog rather than
    taken on trust. A module that gains or loses a table fails here until the
    declaration moves with it — which is what makes the update a reviewed diff.
    """
    from app.runtime_admission import COMPOSED_MODULES

    recorded = _inventory_relations()
    for module in COMPOSED_MODULES:
        assert module.schema in recorded, (
            f"{module.schema} is declared but the migrated catalog has no such "
            "module schema"
        )
        declared = set(module.tenant_tables) | set(module.platform_tables)
        assert declared == recorded[module.schema], (
            f"{module.schema}: declared {sorted(declared)} but the catalog has "
            f"{sorted(recorded[module.schema])}"
        )


def test_the_platform_plane_is_declared_separately_and_never_demanded() -> None:
    """ADR-0023: the control-plane half is REVOKEd from the tenant app role, so
    a check that demanded `app_user` reach it would demand the isolation be
    broken. The split exists so the transcript can NAME what it does not ask
    for, instead of silently omitting it."""
    from app.runtime_admission import COMPOSED_MODULES

    recorded_platform = {
        (row[0], row[1])
        for row in (
            line.split("\t")
            for line in INVENTORY.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if row[0].startswith("mod_") and row[2] == "platform"
    }
    declared_platform = {
        (module.schema, table)
        for module in COMPOSED_MODULES
        for table in module.platform_tables
    }
    assert declared_platform == recorded_platform
    for module in COMPOSED_MODULES:
        assert not set(module.tenant_tables) & set(module.platform_tables)


def test_the_activation_flags_are_the_gates_the_adoption_boundaries_own() -> None:
    """Two modules already have a composition gate. The admission check must
    read THOSE, not a second flag with the same intent — a deployment cannot be
    left with one file believing accounting is on and another believing it off.
    """
    from app.runtime_admission import MODULES_BY_CODE

    accounting = (REPO_ROOT / "app" / "accounting_adoption.py").read_text("utf-8")
    tax = (
        REPO_ROOT
        / "app"
        / "services"
        / "finance"
        / "tax"
        / "adoption"
        / "composition.py"
    ).read_text("utf-8")

    assert MODULES_BY_CODE["accounting"].activation_env_var == (
        "ACCOUNTING_COMPOSITION_ENABLED"
    )
    assert 'os.getenv("ACCOUNTING_COMPOSITION_ENABLED", "false")' in accounting
    assert MODULES_BY_CODE["tax"].activation_env_var == "TAX_COMPOSITION_ENABLED"
    assert 'os.getenv("TAX_COMPOSITION_ENABLED", "false")' in tax


def test_the_deploy_step_uses_the_runtime_credential_not_the_migration_one() -> None:
    """The single deliberate difference from every other one-off in deploy.sh.

    Re-verifying `app_admin` here would prove nothing: that role is BYPASSRLS by
    contract and is not the connection the application serves on.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    command = "python scripts/verify_runtime_admission.py"
    assert command in deploy
    # Anchored on the EXECUTED command and walked backwards to the compose
    # invocation carrying it. Splitting on the first textual mention of the
    # script would land in the comment above the step and then report on the
    # previous command instead.
    index = deploy.index(command)
    invocation = deploy[deploy.rindex("docker compose run", 0, index) : index]
    assert "-e MIGRATION_DATABASE_URL" not in invocation, (
        "the admission step was handed the migration credential, which defeats "
        "its entire purpose"
    )
