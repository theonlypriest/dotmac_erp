"""The role contract has one runtime owner and one migration snapshot.

`app.migration_database_roles` is the runtime decision used by the elevated
bootstrap, deploy preflight and Alembic environment. The migration copies that
contract as a point-in-time snapshot. They must agree exactly, or the bootstrap
builds something the migration then refuses.

The migration does not import mutable runtime code. This test is what makes the
deliberate copy safe.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bootstrap_database_roles.py"
MIGRATION = REPO_ROOT / "alembic" / "versions" / "20260814_database_roles.py"
RUNTIME_CONTRACT = REPO_ROOT / "app" / "migration_database_roles.py"
ALEMBIC_ENV = REPO_ROOT / "alembic" / "env.py"
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"
RUNTIME_ADMISSION_SCRIPT = REPO_ROOT / "scripts" / "verify_runtime_admission.py"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HARDENED_CI = REPO_ROOT / ".github" / "workflows" / "release-hardened.yml"
MAKEFILE = REPO_ROOT / "Makefile"
HARDENED_DOCKERFILE = REPO_ROOT / "Dockerfile.hardened"

#: The accepted contract, as `(rolbypassrls, rolsuper)`. Stated a third time,
#: here, so the test cannot pass by both sources drifting together.
EXPECTED = {
    "app_admin": (True, False),
    "app_user": (False, False),
    "platform_api": (False, False),
}
EXPECTED_EXECUTOR = "app_admin"


def _contract(path: Path) -> dict[str, tuple[bool, bool]]:
    """Read `ROLE_CONTRACT` statically — importing the migration would run it."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "ROLE_CONTRACT":
                value = node.value if isinstance(node, ast.Assign) else node.value
                assert value is not None
                return ast.literal_eval(value)
    raise AssertionError(f"{path} declares no ROLE_CONTRACT")


def _constant(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} declares no {name}")


def _unbound_migration_steps(source: str) -> list[str]:
    workflow: dict[str, Any] = yaml.safe_load(source)
    failures: list[str] = []
    for job_name, job in workflow.get("jobs", {}).items():
        job_env = job.get("env", {})
        for step in job.get("steps", []):
            run = str(step.get("run", ""))
            if "alembic upgrade" not in run:
                continue
            evidence = f"{job_env}\n{step.get('env', {})}\n{run}"
            if (
                "MIGRATION_DATABASE_URL" not in evidence
                or "postgresql+psycopg://app_admin@" not in evidence
            ):
                failures.append(f"{job_name}: {step.get('name', '<unnamed>')}")
    return failures


def test_the_runtime_verifier_states_the_accepted_contract() -> None:
    assert _contract(RUNTIME_CONTRACT) == EXPECTED


def test_the_migration_states_the_same_contract() -> None:
    assert _contract(MIGRATION) == EXPECTED
    assert _constant(RUNTIME_CONTRACT, "MIGRATION_EXECUTOR") == EXPECTED_EXECUTOR
    assert _constant(MIGRATION, "MIGRATION_EXECUTOR") == EXPECTED_EXECUTOR


def test_bootstrap_and_deploy_reuse_the_runtime_verifier() -> None:
    """Deploy delegates to the shared contracts; it never restates one.

    Both verifiers are pinned, not just the first: dropping either step from
    `scripts/deploy.sh` fails here. They answer DIFFERENT questions on
    DIFFERENT credentials — `--verify-only` asks whether `app_admin` may run
    the DDL, and the admission check asks whether the connection the
    application will serve requests on is the canonical runtime identity for
    the modules that are actually active. Neither substitutes for the other,
    so neither may be silently removed.
    """
    bootstrap = SCRIPT.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    admission = RUNTIME_ADMISSION_SCRIPT.read_text(encoding="utf-8")

    assert "from app.migration_database_roles import" in bootstrap
    assert "MIGRATION_OWNERSHIP_SQL" in bootstrap
    assert "--verify-only" in deploy
    assert "REQUIRED =" not in deploy

    assert "from app.runtime_admission import" in admission
    assert "runtime_admission_violations" in admission
    assert "scripts/verify_runtime_admission.py" in deploy
    assert "RUNTIME_ROLE =" not in deploy


def compose_run_invocation(deploy: str, command: str) -> str:
    """The `docker compose run ...` that actually EXECUTES `command`.

    Anchored on the executed command and then walked BACKWARDS to the compose
    invocation that carries it, rather than splitting on the first textual
    mention of a script name. `scripts/deploy.sh` explains each step in a
    comment above it, so a name-first split measures the prose and reports on
    whichever command happens to precede it — which is how a check like this
    silently starts asserting nothing.
    """
    index = deploy.index(command)
    start = deploy.rindex("docker compose run", 0, index)
    return deploy[start : index + len(command)]


def test_the_two_deploy_verifiers_use_different_credentials() -> None:
    """The one deliberate asymmetry in `scripts/deploy.sh`, pinned.

    Every other one-off in that script is handed `-e MIGRATION_DATABASE_URL`.
    The admission step must NOT be: it exists to observe the runtime
    connection, and `app_admin` is BYPASSRLS by contract, so re-verifying it
    there would certify a role that can read past every tenant policy.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")

    admission = compose_run_invocation(
        deploy, "python scripts/verify_runtime_admission.py"
    )
    assert "-e MIGRATION_DATABASE_URL" not in admission, (
        "the runtime admission step was handed the migration credential"
    )

    for executor_command in (
        "python scripts/bootstrap_database_roles.py --verify-only",
        "alembic upgrade heads\n",
    ):
        invocation = compose_run_invocation(deploy, executor_command)
        assert "-e MIGRATION_DATABASE_URL" in invocation, executor_command


def test_the_credential_asymmetry_detector_is_sensitive() -> None:
    """Sensitivity proof (ADR-0018): the split above must be able to fail.

    Planted defect — hand the admission step the migration credential — and the
    helper must report it. A detector anchored on a comment would pass this.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    tampered = deploy.replace(
        "docker compose run --rm app \\\n    python "
        "scripts/verify_runtime_admission.py",
        "docker compose run --rm -e MIGRATION_DATABASE_URL app \\\n    python "
        "scripts/verify_runtime_admission.py",
        1,
    )
    assert tampered != deploy, "the tamper target moved; update this proof"
    assert "-e MIGRATION_DATABASE_URL" in compose_run_invocation(
        tampered, "python scripts/verify_runtime_admission.py"
    )


def test_the_runtime_role_contract_is_stated_once_for_both_verifiers() -> None:
    """`app_user` is `(NOBYPASSRLS, NOSUPERUSER)` in exactly one place.

    The admission check refuses a runtime role that is SUPERUSER or BYPASSRLS,
    which is the same contract `ROLE_CONTRACT` states. It must not restate the
    pair — two copies is how a repaired bootstrap and a refusing admission
    check end up disagreeing about what correct looks like.
    """
    from app.migration_database_roles import ROLE_CONTRACT
    from app.runtime_admission import RUNTIME_ROLE

    assert RUNTIME_ROLE in ROLE_CONTRACT
    assert ROLE_CONTRACT[RUNTIME_ROLE] == EXPECTED["app_user"] == (False, False)


def test_alembic_requires_the_dedicated_migration_url_and_exact_executor() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert 'os.environ.get("MIGRATION_DATABASE_URL"' in source
    assert "verify_migration_connection" in source
    assert "MIGRATION_OWNERSHIP_SQL" in source
    assert "with connection.begin():\n            verify_migration_connection" in source
    assert "settings.database_url" not in source


def test_ci_runs_migrations_as_app_admin_not_postgres() -> None:
    for workflow in (CI, HARDENED_CI):
        source = workflow.read_text(encoding="utf-8")

        assert "alembic upgrade" in source
        assert _unbound_migration_steps(source) == []

        # Sensitivity proof: the entry-point-family detector must fail when the
        # dedicated executor channel is mechanically removed.
        broken = source.replace("MIGRATION_DATABASE_URL", "BROKEN_MIGRATION_URL")
        assert _unbound_migration_steps(broken)

    hardened_image = HARDENED_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY scripts/bootstrap_database_roles.py" in hardened_image


def test_operator_migration_entrypoints_do_not_reuse_the_running_app() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "-e MIGRATION_DATABASE_URL app" in deploy
    assert "docker compose run --rm -e MIGRATION_DATABASE_URL app" in makefile
    assert "--entrypoint" not in deploy
    assert "--entrypoint" not in makefile
    assert "docker exec dotmac_erp_app alembic" not in makefile


def test_app_admin_bypasses_rls_and_is_not_a_superuser() -> None:
    """The distinction that was got wrong twice upstream, pinned here.

    `app_admin` needs to read past RLS — that is BYPASSRLS. Accepting SUPERUSER
    as an alternative would certify cluster-wide authority (DDL on any database,
    role creation, COPY PROGRAM) to satisfy a requirement about reading rows.
    """
    assert EXPECTED["app_admin"] == (True, False)


def test_no_online_role_can_bypass_row_level_security() -> None:
    """Both attributes, not just the flag: a superuser bypasses RLS regardless
    of `rolbypassrls`, so checking only the flag would certify
    `app_user SUPERUSER NOBYPASSRLS` as isolated."""
    for role in ("app_user", "platform_api"):
        assert EXPECTED[role] == (False, False)


def _executable_strings(path: Path) -> list[str]:
    """Every string literal the module can EXECUTE — docstrings excluded.

    Asserted this way because the migration's docstring necessarily contains the
    phrase `CREATE ROLE` in order to explain why it never issues one. A check
    over raw file text matches that explanation and fails on correct code; the
    property under test is about what the module can run.
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


def test_the_migration_creates_no_role() -> None:
    """Creation is the privileged bootstrap's job. A migration that creates a
    role is a second authority over cluster access, and one that escalates to do
    so is worse."""
    executable = " ".join(_executable_strings(MIGRATION)).lower()
    for ddl in ("create role", "alter role", "drop role", "createrole"):
        assert ddl not in executable, f"migration must not issue {ddl!r}"


def test_the_guard_would_notice_role_ddl() -> None:
    """Sensitivity proof: the exclusion above must not swallow real DDL too."""
    executable = " ".join(_executable_strings(SCRIPT)).lower()
    assert "create role" in executable, (
        "the bootstrap script DOES issue CREATE ROLE, so a scanner that cannot "
        "see it here would not see it in a migration either"
    )


def test_the_migration_points_at_the_bootstrap_when_it_refuses() -> None:
    """A fail-closed check that does not name its remedy just stops a deploy."""
    source = MIGRATION.read_text(encoding="utf-8")
    assert "scripts/bootstrap_database_roles.py" in source


def test_the_bootstrap_never_sets_a_password() -> None:
    """Operators set passwords out of band. One in a repo, a log or a shell
    history is a credential leak.

    Asserted against the SQL the script can emit, not against the word
    "password" anywhere in the file — the docstring says it never sets one, and
    a check that forbade the word would forbid saying so.
    """
    lowered = SCRIPT.read_text(encoding="utf-8").lower()
    for forbidden in ("with password", "encrypted password", "unencrypted password"):
        assert forbidden not in lowered, f"bootstrap may not emit {forbidden!r}"
