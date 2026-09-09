"""What ``app_user`` can actually reach, measured through a real ``app_user`` session.

``tests/architecture/test_cross_org_caller_dispositions.py`` is a STATIC gate.
It parses the ledger and never opens a database, so each of its rows is a claim
about behaviour that nothing has executed.  This module executes it: for every
relation the ledger's
``target_relations`` column names, it opens a session whose ``current_user`` is
``app_user`` and attempts the read.

## WHAT THIS TEST CANNOT PROVE  (design §7.4 — read this before trusting a green run)

1. **An unprotected row is not proved reachable.**  ``app_user`` now holds the
   composed modules' table grants, hosted prerequisite grants, and one reviewed
   legacy ``SELECT`` on ``hr.employment_type``. None is a target of a
   ``protected_access == no`` cross-organization ledger row, so those targets
   still measure ``denied-no-grant`` today. Asserting reachability now would be
   91 red rows on the first run, which is how a gate gets deleted rather than
   fixed, so
   :func:`test_unprotected_rows_have_no_reachable_target_yet` records the
   number as a two-directional ratchet.  It becomes a real assertion in the
   change that adds the grants.
2. **A protected-target row is not proved refused.**  157 of the 223 relations in
   ``docs/rls-coverage-baseline.json`` carry no RLS at all.  Once grants land,
   those return EVERY organization's rows to ``app_user``; ``denied-no-grant``
   is the absence of a privilege, not the presence of a boundary.
3. **RLS reachability is NOT isolation, because ERP's policies consult a
   session GUC that any role may set.**  Every policy is
   ``USING (should_bypass_rls() OR organization_id = get_current_organization_id())``
   and ``app.bypass_rls`` is ``PGC_USERSET``: ``app_user`` can set it to
   ``'true'`` itself and every one of those policies then passes.  ``REVOKE SET
   ON PARAMETER`` does not restrict a custom GUC.  So everything measured here
   is CONDITIONAL on that GUC being unset — which
   :func:`test_the_proof_actually_runs_as_an_unprivileged_role` asserts rather
   than assumes.  The claim that the runtime never sets it belongs to the
   static check ``tests/architecture/test_no_runtime_rls_bypass.py``, not here.
4. **It proves things about relations, not about callers.**  Executing
   ``warn_unconfigured_webhook_allowlist`` as ``app_user`` would need grants
   that do not exist.  This module certifies the ledger's TARGETS; nothing yet
   certifies a caller end to end.
5. **It says nothing about production.**  CI measures ``alembic upgrade
   heads``.  The 2026-08-15 production snapshot had 16 RLS-enabled relations
   against this design's 160.

## Two deviations from design §7.3, stated rather than hidden

* ``scoped-to-org`` is not in the measured vocabulary.  Distinguishing "only
  org A's rows" from "no rows" requires seeding rows as ``app_user``, which
  needs ``INSERT`` privileges that do not exist.  A granted, RLS-protected
  relation that returns rows with NO organization GUC set is recorded as
  ``leaks-without-org-context`` — the only honest label for that measurement.
* ``guc-bypassable`` is recorded as its own column rather than as an outcome:
  it is orthogonal to whether the read is permitted, and collapsing the two
  would lose one of them.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTATION = Path(__file__).parent / "app_user_reachability.tsv"
DESIGN_CATALOG = Path(__file__).parent / "tenant_table_inventory.tsv"


# ---------------------------------------------------------------------------
# The static gate, loaded by path
# ---------------------------------------------------------------------------
# `tests/architecture/` has no `__init__.py`, and the integration lane runs with
# `confcutdir=tests/integration`, so `import tests.architecture...` is not
# available here.  Loading by path keeps ONE definition of the ledger's columns,
# its target-relation parser and the ORM-derived nullability floors: duplicating
# them would let the two lanes disagree about what the ledger says, which is the
# failure this whole programme exists to prevent.  The module imports only the
# standard library and pytest.
_GATE_PATH = (
    REPO_ROOT / "tests" / "architecture" / "test_cross_org_caller_dispositions.py"
)


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "erp_cross_org_caller_gate", _GATE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"cannot load the static gate from {_GATE_PATH} — this module derives "
        "the relations it measures from the ledger that gate owns"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because @dataclass resolves its own module
    # out of sys.modules while the class body is being processed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


# ---------------------------------------------------------------------------
# Becoming app_user
# ---------------------------------------------------------------------------
# `scripts/bootstrap_database_roles.py` deliberately never sets a password, so a
# direct `app_user` login is not generally available and the lane connects as the
# superuser and drops into the role.  `SET LOCAL ROLE` changes `current_user`,
# and PostgreSQL evaluates BOTH the superuser exemption and `rolbypassrls`
# against the current user — so RLS genuinely applies, which
# `test_the_proof_actually_runs_as_an_unprivileged_role` verifies rather than
# takes on trust.
APP_ROLE = os.getenv("RLS_PROOF_ROLE", "app_user")
APP_USER_URL_VAR = "APP_USER_DATABASE_URL"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema"})
"""Schemas the design catalog can never describe: its extraction query filters
``relkind='r'`` and excludes the system namespaces.  A relation here is measured
and recorded, but its expectation cannot be cross-checked against the catalog,
so :func:`test_the_expectation_is_derived_from_the_design_catalog` asserts the
uncheckable set is EXACTLY these — an uncatalogued application relation cannot
hide in the gap."""

INSUFFICIENT_PRIVILEGE = "42501"

COLUMNS = ("relation", "outcome", "rls_enabled", "guc_bypassable")

OUTCOMES = frozenset(
    {
        # `has_table_privilege` is false: the read never reaches RLS at all.
        "denied-no-grant",
        # Granted and `relrowsecurity` is false: every organization's rows are
        # visible.  NOT a pass.
        "visible-unprotected",
        # Granted, RLS on, no `app.current_organization_id`: zero rows.
        "empty-no-org-context",
        # Granted, RLS on, no organization GUC — and rows came back anyway.
        "leaks-without-org-context",
    }
)


def _quoted(name: str) -> str:
    assert _IDENTIFIER.match(name), (
        f"{name!r} is not a bare SQL identifier. Every relation in the ledger "
        "matches the static gate's RELATION pattern and every role name is "
        "configuration, so a name that needs escaping is a defect, not a case "
        "to handle."
    )
    return f'"{name}"'


@pytest.fixture(scope="module")
def proof_engine(engine):
    """The engine the proof connects through.

    Defaults to the lane's existing engine (superuser, then ``SET LOCAL ROLE``).
    ``APP_USER_DATABASE_URL`` selects a direct login where a deployment provides
    one; it changes nothing about the assertions, because the role is set
    explicitly either way.  The URL must carry NO password — credentials belong
    in ``PGPASSWORD``, which libpq reads either way and which cannot leak
    through a logged DSN, a ``ps`` listing or an error that echoes the URL.
    """
    url = os.getenv(APP_USER_URL_VAR)
    if not url:
        yield engine
        return
    assert make_url(url).password is None, (
        f"{APP_USER_URL_VAR} carries a password in the connection string. Put "
        "it in PGPASSWORD instead — see the bootstrap step in "
        ".github/workflows/ci.yml for the pattern this repo already uses."
    )
    direct = create_engine(url, pool_pre_ping=True)
    try:
        yield direct
    finally:
        direct.dispose()


@pytest.fixture(scope="module")
def app_role_connection(proof_engine):
    """A connection whose ``current_user`` is ``APP_ROLE``, rolled back at the end.

    Module-scoped and never committed: this proof reads, and every probe runs
    inside its own savepoint so a refused statement cannot poison the next one.
    """
    with proof_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text(f"SET LOCAL ROLE {_quoted(APP_ROLE)}"))
        try:
            yield connection
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# Reading the two checked artifacts
# ---------------------------------------------------------------------------


def _ledger_target_relations() -> frozenset[str]:
    with gate.INVENTORY.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert tuple(reader.fieldnames or ()) == gate.INVENTORY_FIELDS, (
            "the ledger's columns changed; this module measures the relations "
            f"named in {gate.INVENTORY_FIELDS.index('target_relations')}th column "
            "and must be updated in the same change"
        )
        rows = [dict(row) for row in reader]
    return frozenset(relation for row in rows for relation in gate.row_relations(row))


def _ledger_rows() -> list[dict[str, str]]:
    with gate.INVENTORY.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _recorded() -> dict[str, dict[str, str]]:
    recorded: dict[str, dict[str, str]] = {}
    for line in EXPECTATION.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        assert len(fields) == len(COLUMNS), f"malformed expectation row: {line!r}"
        row = dict(zip(COLUMNS, fields, strict=True))
        assert row["outcome"] in OUTCOMES, (
            f"{row['relation']}: {row['outcome']!r} is not a measured outcome. "
            f"The vocabulary is closed: {sorted(OUTCOMES)}"
        )
        recorded[row["relation"]] = row
    return recorded


def _design_catalog() -> dict[str, dict[str, str]]:
    columns = (
        "schema",
        "table",
        "tenant_class",
        "rls_enabled",
        "rls_forced",
        "owner",
        "policy_count",
        "policy_uses_settable_guc",
        "app_user_priv",
        "platform_api_priv",
    )
    catalog: dict[str, dict[str, str]] = {}
    for line in DESIGN_CATALOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        assert len(fields) == len(columns), f"malformed catalog row: {line!r}"
        row = dict(zip(columns, fields, strict=True))
        catalog[f"{row['schema']}.{row['table']}"] = row
    return catalog


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

RELATION_STATE_SQL = text(
    """
SELECT c.relrowsecurity,
       COALESCE((
         SELECT bool_or(
                  COALESCE(pg_get_expr(p.polqual, p.polrelid), '')
                    LIKE '%should_bypass_rls%'
                  OR COALESCE(pg_get_expr(p.polwithcheck, p.polrelid), '')
                    LIKE '%should_bypass_rls%')
         FROM pg_policy p WHERE p.polrelid = c.oid), false)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema AND c.relname = :table
"""
)


def _sqlstate(error: DBAPIError) -> str | None:
    original = error.orig
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def _probe(connection: Connection, relation: str) -> tuple[str, str, str]:
    """Measure one relation AS the current role.  Returns (outcome, rls, guc)."""
    schema, _, table = relation.partition(".")
    qualified = f"{_quoted(schema)}.{_quoted(table)}"

    state = connection.execute(
        RELATION_STATE_SQL, {"schema": schema, "table": table}
    ).first()
    assert state is not None, (
        f"{relation} is named by the ledger but no such relation is visible in "
        "pg_class. Either the database is not migrated to heads or the ledger "
        "names a relation that does not exist."
    )
    rls = "true" if state[0] else "false"
    guc = "true" if state[1] else "false"

    # The read is ATTEMPTED, not inferred from has_table_privilege: a refusal is
    # observed as PostgreSQL actually refusing it.  Own savepoint per probe, so
    # one refusal does not abort the rest.
    try:
        with connection.begin_nested():
            visible = connection.execute(
                # A relation name cannot be a bind parameter. Both halves came
                # from `_quoted`, which refuses anything that is not a bare
                # identifier, and the names originate in a TSV the static gate
                # validates against its RELATION pattern.
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM {qualified} LIMIT 1) probe"  # noqa: S608
                )
            ).scalar_one()
    except DBAPIError as error:
        if _sqlstate(error) != INSUFFICIENT_PRIVILEGE:
            raise
        return "denied-no-grant", rls, guc

    if rls != "true":
        return "visible-unprotected", rls, guc
    return (
        ("empty-no-org-context" if visible == 0 else "leaks-without-org-context"),
        rls,
        guc,
    )


@pytest.fixture(scope="module")
def measured(app_role_connection) -> dict[str, dict[str, str]]:
    return {
        relation: dict(
            zip(
                COLUMNS, (relation, *_probe(app_role_connection, relation)), strict=True
            )
        )
        for relation in sorted(_ledger_target_relations())
    }


# ---------------------------------------------------------------------------
# The preconditions that make everything below non-vacuous
# ---------------------------------------------------------------------------


def test_the_proof_actually_runs_as_an_unprivileged_role(app_role_connection) -> None:
    """Design §7.2.

    A proof that silently ran as ``postgres`` would certify the exact defect it
    exists to catch: a superuser is exempt from RLS, so every policy passes and
    every relation looks reachable.  Three separate facts are asserted, none
    assumed — the identity, the role's attributes, and the bypass GUC every ERP
    policy short-circuits on.
    """
    connection = app_role_connection

    assert connection.scalar(text("SELECT current_user")) == APP_ROLE, (
        f"the proof is not running as {APP_ROLE!r}. SET LOCAL ROLE did not take "
        "effect, and every measurement below is about some other role."
    )

    attributes = connection.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).first()
    assert attributes is not None, (
        f"{APP_ROLE!r} does not exist in pg_roles — has "
        "scripts/bootstrap_database_roles.py run?"
    )
    assert attributes == (False, False), (
        f"{APP_ROLE!r} is (rolsuper, rolbypassrls) = {tuple(attributes)}. Either "
        "attribute exempts it from RLS entirely, so nothing below measures "
        "isolation."
    )

    # `should_bypass_rls()` is the first disjunct of EVERY ERP policy. If it is
    # true, the whole module proves nothing. Located rather than assumed to be
    # in `public`, so a schema move fails loudly instead of erroring obscurely.
    schemas = (
        connection.execute(
            text(
                "SELECT n.nspname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE p.proname = 'should_bypass_rls' AND p.pronargs = 0"
            )
        )
        .scalars()
        .all()
    )
    assert len(schemas) == 1, (
        "expected exactly one zero-argument should_bypass_rls(); found "
        f"{list(schemas)}. Every RLS policy in this database calls it "
        "unqualified, so ambiguity here is a security defect."
    )
    # `_quoted` refuses a schema name that is not a bare identifier, and the
    # name came from pg_namespace, not from input.
    bypass_sql = text(f"SELECT {_quoted(schemas[0])}.should_bypass_rls()")  # noqa: S608
    assert connection.scalar(bypass_sql) is False, (
        "app.bypass_rls is set on this session, so every policy passes "
        "unconditionally and no measurement below means anything."
    )
    assert connection.scalar(
        text("SELECT current_setting('app.current_organization_id', true)")
    ) in (None, ""), (
        "an organization GUC is already set, so 'no org context' is not what is "
        "being measured."
    )


def test_the_probe_can_observe_both_a_permitted_and_a_refused_read(
    app_role_connection, measured
) -> None:
    """Sensitivity proof (ADR-0018).

    A probe that returned ``denied-no-grant`` unconditionally — a typo in the
    SQL, a savepoint left aborted — would make every expectation below pass for
    the wrong reason.  The positive control is ``pg_catalog.pg_class``, readable
    by PUBLIC on any PostgreSQL and deliberately NOT sourced from the ledger, so
    editing the ledger cannot delete this control.
    """
    outcome, _, _ = _probe(app_role_connection, "pg_catalog.pg_class")
    assert outcome != "denied-no-grant", (
        "the probe reports a refusal on pg_catalog.pg_class, which PUBLIC can "
        "always read. The probe is broken and every 'denied-no-grant' below is "
        "an artefact."
    )
    assert "denied-no-grant" in {row["outcome"] for row in measured.values()}, (
        "no ledger relation measured a refusal. Either grants landed (in which "
        "case §7.4.1's ratchet should be moving) or the role drop failed."
    )


def test_the_ledger_names_relations_to_measure(measured) -> None:
    """A green run over an empty relation set is the failure where the suite
    stopped looking."""
    assert measured, (
        "the ledger's target_relations column yielded no relations, so nothing "
        "below is measuring anything."
    )
    assert set(measured) == set(_recorded()), (
        "the measured relation set and "
        f"{EXPECTATION.name} disagree: "
        f"measured-only={sorted(set(measured) - set(_recorded()))}, "
        f"recorded-only={sorted(set(_recorded()) - set(measured))}. The "
        "expectation file is regenerated in the same change that moves a "
        "target_relations entry."
    )


# ---------------------------------------------------------------------------
# The measurement, asserted as an exact set
# ---------------------------------------------------------------------------


def test_measured_reachability_matches_the_recorded_expectation(measured) -> None:
    """Exact rows, not counts — the same shape as
    ``test_tenant_table_inventory.py``.  "34 relations are denied" stays true
    when one gains a grant and another loses one."""
    recorded = _recorded()
    drift = [
        f"{relation}.{field}: recorded={recorded[relation][field]!r} "
        f"measured={row[field]!r}"
        for relation, row in sorted(measured.items())
        if relation in recorded
        for field in COLUMNS[1:]
        if recorded[relation][field] != row[field]
    ]
    assert not drift, (
        "what app_user can reach drifted from the recorded expectation:\n"
        + "\n".join(drift)
        + "\n\nA relation moving OFF denied-no-grant is a new grant and must be "
        "reviewed against the ledger rows that target it."
    )


def test_the_expectation_is_derived_from_the_design_catalog() -> None:
    """The expectation file is a DERIVATION, not a typed claim.

    This is the defect that sank the first attempt at a scope gate: prose
    asserting a fact that nothing compared to anything.  Every recorded row here
    is reproducible from ``tenant_table_inventory.tsv`` — ``app_user_priv``
    decides granted-or-not, ``rls_enabled`` decides the rest — so the file
    cannot be hand-edited to match whatever a run happens to produce without
    this test noticing.
    """
    catalog = _design_catalog()
    recorded = _recorded()

    uncheckable = {
        relation for relation in recorded if relation.split(".", 1)[0] in SYSTEM_SCHEMAS
    }
    assert uncheckable == set(recorded) - set(catalog), (
        "a recorded relation is absent from the design catalog without being a "
        "system-catalog relation: "
        f"{sorted((set(recorded) - set(catalog)) - uncheckable)}. The catalog "
        "describes every application relation, so this is a typo or an "
        "unreviewed table."
    )

    mismatches = []
    for relation, row in sorted(recorded.items()):
        entry = catalog.get(relation)
        if entry is None:
            continue
        readable = entry["app_user_priv"].startswith("r")
        if not readable:
            expected = "denied-no-grant"
        elif entry["rls_enabled"] != "true":
            expected = "visible-unprotected"
        else:
            expected = "empty-no-org-context"
        if row["outcome"] != expected:
            mismatches.append(
                f"{relation}: recorded={row['outcome']!r} "
                f"derived={expected!r} (app_user_priv={entry['app_user_priv']!r}, "
                f"rls_enabled={entry['rls_enabled']!r})"
            )
        if row["rls_enabled"] != entry["rls_enabled"]:
            mismatches.append(
                f"{relation}.rls_enabled: recorded={row['rls_enabled']!r} "
                f"catalog={entry['rls_enabled']!r}"
            )
        if row["guc_bypassable"] != entry["policy_uses_settable_guc"]:
            mismatches.append(
                f"{relation}.guc_bypassable: recorded={row['guc_bypassable']!r} "
                f"catalog={entry['policy_uses_settable_guc']!r}"
            )
    assert not mismatches, (
        f"{EXPECTATION.name} is not derivable from {DESIGN_CATALOG.name}:\n"
        + "\n".join(mismatches)
    )


# ---------------------------------------------------------------------------
# The two directional claims the ledger makes
# ---------------------------------------------------------------------------


def test_a_protected_rows_targets_are_not_freely_readable(measured) -> None:
    """A row that claims a protected target while every target is readable and
    unprotected is bookkeeping: it blocks a cutover against tables that protect
    nothing.

    Recorded as a shrink-only literal rather than asserted to zero, because the
    honest expectation for the 157 ``known_gaps`` relations with no RLS is
    exactly ``visible-unprotected`` once grants land (§7.4.2).
    """
    freely_readable = sorted(
        f"{row['path']}::{row['symbol']} -> {relation}"
        for row in _ledger_rows()
        if row["protected_access"] == "yes"
        for relation in gate.row_relations(row)
        if measured.get(relation, {}).get("outcome") == "visible-unprotected"
    )
    # Today: only `pg_catalog.pg_matviews`, reached by
    # app/tasks/finance.py::refresh_analysis_cubes. It is a system view that
    # can never carry a tenant scope, which is why that row is in the static
    # gate's UNBOUNDED_REACH rather than given an invented shape.
    baseline = 1
    assert len(freely_readable) == baseline, (
        f"{len(freely_readable)} protected-row targets are freely readable by "
        f"{APP_ROLE}; baseline is {baseline}. A rise means a block was recorded "
        "against a relation that protects nothing. A fall is progress that must "
        "lower the baseline in the same commit.\n" + "\n".join(freely_readable)
    )


def test_unprotected_rows_have_no_reachable_target_yet(measured) -> None:
    """Design §7.4.1 — a RECORDED MEASUREMENT, deliberately not a gate.

    A ``protected_access == no`` row claims its caller survives the
    ``app_user`` cutover.  That
    claim is unfalsifiable until ``app_user`` is granted those exact target
    reads. Existing module/host grants and the sealed Employment Type source
    read do not cover this ledger set, so every ``ready`` target measures
    ``denied-no-grant``. This ratchets the number DOWN as grants land and fails
    if it rises. It becomes
    ``assert unreachable == 0`` in the change that adds the grants — that change
    is where the reachability direction turns into a real assertion.
    """
    unreachable = sorted(
        f"{row['path']}::{row['symbol']}"
        for row in _ledger_rows()
        if row["protected_access"] == "no"
        and any(
            measured.get(relation, {}).get("outcome") == "denied-no-grant"
            for relation in gate.row_relations(row)
        )
    )
    # 91 unprotected rows, all of them. The new hr.employment_type grant is not a
    # target of any of these cross-organization caller rows.
    baseline = 91
    assert len(unreachable) == baseline, (
        f"{len(unreachable)} unprotected rows still have an unreachable "
        f"target; "
        f"baseline is {baseline}. A fall is the grants landing and must lower "
        "the baseline here in the same commit. A rise means a row was marked "
        "unprotected over a relation app_user cannot read.\n" + "\n".join(unreachable)
    )
