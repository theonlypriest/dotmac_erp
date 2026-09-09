"""Pure contract for the RUNTIME database identity, scoped to ACTIVE modules.

`app.migration_database_roles` answers "may this connection run DDL?".  This
module answers the other half — "is the connection the application will serve
requests on the canonical, least-privilege runtime identity, for the module
storage it is actually going to use?" — and it is deliberately a separate
decision with a separate credential.

## The failure this prevents

A composed module's tables are created by `alembic upgrade heads` as
`app_admin`, with `GRANT`s addressed to `app_user` and `FORCE`d row-level
security whose policies read `app.current_tenant`.  Every one of those
protections is addressed to a NAMED role.  A deployment whose application
connects as some other login — a legacy account that predates the
least-privilege programme, say — reaches those tables through whatever ACL that
other login happens to carry, and the RLS policies written for `app_user` are
simply not the policies it is evaluated against.  The migration gate cannot see
this: it inspects the schema, and the schema is correct.  Only a connection made
with the RUNTIME credential can observe it, which is why this check exists and
why it is the one step in `scripts/deploy.sh` that must NOT be handed
`MIGRATION_DATABASE_URL`.

## Why it is scoped to active modules, and why that is a ratchet

ERP production connects as the legacy login `dotmac_erp_app`, not `app_user`.
An unconditional assertion would therefore refuse the next production deploy,
which is not a security improvement — it is an outage.  So the assertion is
demanded per module, and only for a module a deployment has DECLARED active.
Today no composed module declares itself active, so this check passes and says
so loudly.  Activating one is what turns the assertion on, in the same change
that takes on the obligation.

## Why "active" is a declaration and never an inference

`app.bill_of_materials.SelectedModule.state` is `"composed"` or `"selected"`.
That is MEMBERSHIP — which modules the product installs — and it says nothing
about whether a deployment has been told to use one.  Inferring activity from
membership, from a schema existing, or from a table having rows would make the
gate fire on storage that ADR-0003 deliberately created ahead of cutover.  So
activity is exactly one thing: an operator-set environment flag, declared below
next to the module it governs, defaulting off.

## Why silence is not a pass (ADR-0018)

A gate scoped to an empty set passes for the wrong reason, and a passing gate
that prints nothing is an unmonitored region rather than evidence.  So the
entrypoint prints every module it considered, the flag that would activate it,
every check it ran and every check it skipped — and when the active set is
empty it prints `VACUOUS_ADMISSION_NOTICE`, which states in as many words that
nothing was proven about the runtime role.  `runtime_admission_report` builds
that transcript from the same snapshot the decision reads, so the transcript
cannot describe a different run from the one that was judged.

## Shape

The split mirrors `dotmac_kernel.migrations.catalog` and the `--verify-only`
path of `scripts/bootstrap_database_roles.py`:

* `RuntimeSnapshot` and friends — plain frozen dataclasses of observations.
* `runtime_admission_violations` — a PURE function over one snapshot, unit
  tested against synthetic inputs including the ones that must make it fire.
* `scripts/verify_runtime_admission.py` — the thin `fetch_snapshot` seam that
  runs the SQL and prints the transcript.  It reads; it never writes.

No SQL in this programme interpolates a schema, table or role name.  Names
reach PostgreSQL as bound parameters, or — where a relation must be named in a
`FROM` clause for the RLS probes — through `psycopg.sql.Identifier`, which is
composition by the driver rather than string building by us.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.migration_database_roles import RolePosture, posture

#: The one login the composed modules' grants and RLS policies are addressed to.
#: Stated here rather than imported from `ROLE_CONTRACT` because that mapping
#: answers a different question (which roles must EXIST and how they are
#: shaped); this constant answers "who must this connection BE".
RUNTIME_ROLE: Final[str] = "app_user"

#: The transaction-local GUC every module RLS policy reads, primed by
#: `app.rls.set_current_organization`.  The probes below set it with
#: `set_config(..., true)` so the value dies with the read-only transaction.
TENANT_GUC: Final[str] = "app.current_tenant"

#: What an active module's runtime must be able to do to its own tables.  A
#: module that can read but not write is not "mostly admitted": its first write
#: fails in production, at request time, on a tenant's data.
REQUIRED_TABLE_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
)

#: The privileges PostgreSQL can also grant at COLUMN granularity.  A
#: column-level grant is invisible to `has_table_privilege`, so a probe that
#: asked only that question would report a reachable table as unreachable.
#: `DELETE` has no column form, and asking `has_any_column_privilege` for it
#: raises rather than returning false — hence the explicit split.
COLUMN_GRANTABLE_PRIVILEGES: Final[frozenset[str]] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "REFERENCES"}
)


@dataclass(frozen=True)
class ComposedModule:
    """One module whose storage this assembly composes, and how it activates.

    `tenant_tables` and `platform_tables` are the module's own manifest-derived
    relations, recorded here so the admission check knows what an active module
    needs WITHOUT importing the module (which would make an inert composition
    live).  `tests/architecture/test_runtime_admission_is_read_only.py` proves
    both lists against `tests/integration/tenant_table_inventory.tsv`, so the
    declaration cannot drift from the schema the migrations actually build.

    `platform_tables` exist so the control-plane half of a dual-plane module is
    named and then deliberately NOT demanded: ADR-0023 makes those tables
    REVOKEd from the tenant application role, so requiring `app_user` to reach
    them would be requiring the isolation to be broken.
    """

    module_code: str
    schema: str
    tenant_tables: tuple[str, ...]
    platform_tables: tuple[str, ...] = ()
    #: The environment flag that makes this module ACTIVE. Default-off, read
    #: from a passed-in mapping so the decision stays pure and testable.
    activation_env_var: str = ""
    #: Where that flag's meaning is owned. Two owners exist and the difference
    #: matters to a reviewer: a module with its own composition gate is
    #: activated by the gate the adoption boundary already defined, while a
    #: module without one is opted in here, explicitly, by an operator.
    activation_owner: str = ""
    #: The single tenant table the RLS probes drive. One is enough — the probe
    #: proves the policy path and the tenant GUC are in force for this schema —
    #: and probing all of them would multiply a read-only check's cost against
    #: production for no additional evidence.
    probe_table: str = ""
    note: str = ""


#: Every module whose lineage `alembic.ini` composes, with its activation flag.
#:
#: The order is the order the transcript prints in, so it is alphabetical by
#: module code rather than by adoption date.
COMPOSED_MODULES: Final[tuple[ComposedModule, ...]] = (
    ComposedModule(
        module_code="accounting",
        schema="mod_accounting",
        tenant_tables=(
            "account_categories",
            "accounting_dimension_values",
            "accounting_dimensions",
            "accounts",
            "fiscal_periods",
            "fiscal_years",
            "journal_entries",
            "journal_line_dimensions",
            "journal_lines",
            "period_events",
            "posted_ledger_dimensions",
            "posted_ledger_lines",
        ),
        activation_env_var="ACCOUNTING_COMPOSITION_ENABLED",
        activation_owner="composition gate (app/accounting_adoption.py)",
        probe_table="journal_entries",
        note=(
            "Composed and disabled at gate C: storage exists, ERP remains the "
            "live posting authority. See docs/architecture/"
            "accounting-adoption-boundary.md."
        ),
    ),
    ComposedModule(
        module_code="files",
        schema="mod_files",
        tenant_tables=("stored_files",),
        platform_tables=("platform_stored_files",),
        activation_env_var="FILES_RUNTIME_ADMISSION_ENABLED",
        activation_owner="runtime-admission opt-in (no composition gate exists)",
        probe_table="stored_files",
        note=(
            "ERP consumes dotmac-files as an object-storage contract over its "
            "one MinIO adapter; nothing under app/ writes mod_files. This flag "
            "is what puts that storage under the runtime-identity ratchet when "
            "a deployment starts using it."
        ),
    ),
    ComposedModule(
        module_code="imports",
        schema="mod_imports",
        tenant_tables=("import_partitions", "import_run_rows", "import_runs"),
        activation_env_var="IMPORTS_RUNTIME_ADMISSION_ENABLED",
        activation_owner="runtime-admission opt-in (no composition gate exists)",
        probe_table="import_runs",
        note=(
            "The durable-customer slice is a SHADOW: its routes exist under "
            "app/api/finance/import_export.py, and the retiring CustomerImporter "
            "is still the compared verdict. Turning this flag on is the "
            "deployment declaring it depends on mod_imports for real, and is "
            "the only honest trigger for demanding app_user here — inferring it "
            "from the routes being mounted would fire on every deployment that "
            "has never run an import."
        ),
    ),
    ComposedModule(
        module_code="numbering",
        schema="mod_numbering",
        tenant_tables=(
            "allocation_receipts",
            "number_series",
            "series_counters",
            "series_repairs",
        ),
        activation_env_var="NUMBERING_RUNTIME_ADMISSION_ENABLED",
        activation_owner="runtime-admission opt-in (no composition gate exists)",
        probe_table="number_series",
        note=(
            "Tenant plane only (app/migration_planes.py). Storage, not numbering "
            "authority: no series is configured and no legacy allocator is "
            "repointed, so nothing reads or writes these tables yet."
        ),
    ),
    ComposedModule(
        module_code="people",
        schema="mod_people",
        tenant_tables=(
            "departments",
            "designations",
            "employees",
            "employment_types",
            "position_assignments",
            "positions",
        ),
        activation_env_var="PEOPLE_RUNTIME_ADMISSION_ENABLED",
        activation_owner="runtime-admission opt-in (no composition gate exists)",
        probe_table="employees",
        note=(
            "Composed with no runtime caller and no authority transfer "
            "(app/bill_of_materials.py): the six-table employment-identity "
            "lineage exists while the legacy People writers remain the only "
            "authority. Nothing under app/ reads or writes mod_people, so this "
            "flag is what puts the storage under the runtime-identity ratchet "
            "when a domain-by-domain cutover actually starts using it."
        ),
    ),
    ComposedModule(
        module_code="tax",
        schema="mod_tax",
        tenant_tables=(
            "statutory_report_boxes",
            "statutory_report_definitions",
            "statutory_report_values",
            "statutory_reports",
            "tax_authorities",
            "tax_codes",
            "tax_determination_lines",
            "tax_determination_sets",
            "tax_determinations",
            "tax_filing_obligations",
            "tax_jurisdictions",
            "tax_return_events",
            "tax_returns",
            "tax_rule_bands",
            "tax_rules",
            "tax_subject_classifications",
        ),
        activation_env_var="TAX_COMPOSITION_ENABLED",
        activation_owner=(
            "composition gate (app/services/finance/tax/adoption/composition.py)"
        ),
        probe_table="tax_determinations",
        note=(
            "Composed and disabled at C2: released contract and storage, "
            "authority unchanged. See docs/architecture/"
            "dotmac-tax-adoption-boundary.md."
        ),
    ),
)

MODULES_BY_CODE: Final[dict[str, ComposedModule]] = {
    module.module_code: module for module in COMPOSED_MODULES
}


def _flag_is_on(environ: Mapping[str, str], name: str) -> bool:
    """The one truth test for an activation flag.

    Matches `app.accounting_adoption` and the tax composition module exactly
    (`.lower() == "true"`), so a deployment cannot find that the same `.env`
    line means "on" to one reader and "off" to another.
    """
    return environ.get(name, "false").strip().lower() == "true"


def active_modules(environ: Mapping[str, str]) -> tuple[ComposedModule, ...]:
    """The modules a deployment has DECLARED active, in declaration order."""
    return tuple(
        module
        for module in COMPOSED_MODULES
        if _flag_is_on(environ, module.activation_env_var)
    )


@dataclass(frozen=True)
class SchemaUsage:
    """Whether the runtime role can enter one module schema at all."""

    schema: str
    present: bool
    usable: bool


@dataclass(frozen=True)
class TablePrivilege:
    """One `(relation, privilege)` reachability answer for the runtime role.

    `held` is the OR of the table-level and column-level probes; see
    `COLUMN_GRANTABLE_PRIVILEGES` for why both are needed.
    """

    schema: str
    table: str
    privilege: str
    present: bool
    held: bool


@dataclass(frozen=True)
class RlsProbe:
    """The result of really reading one module table under two tenant contexts.

    A catalog inspection can tell you a policy exists.  It cannot tell you the
    policy is the one this connection is evaluated against, that the GUC the
    policy reads is the GUC the application sets, or that the role is not
    quietly bypassing the whole mechanism.  Reading is the only way to know, so
    these three counts come from three real `SELECT count(*)` statements.

    `own_rows` is REPORTED and never required: a module activated on a fresh
    database legitimately has none, and demanding rows would make the check
    refuse the very first deploy of a cutover.  The two counts that must be
    zero are the ones that can only be non-zero if isolation has failed.
    """

    schema: str
    table: str
    #: False when the statements could not run at all — the grant is missing,
    #: the relation is absent, or the connection refused. `error` says which.
    executed: bool
    error: str | None = None
    own_tenant: str | None = None
    other_tenant: str | None = None
    #: Rows visible for the OWN tenant while the GUC names the own tenant.
    own_rows: int = 0
    #: Rows visible for any OTHER tenant while the GUC names the own tenant.
    #: Must be zero.
    foreign_rows_under_own_context: int = 0
    #: Rows visible for the OWN tenant while the GUC names the other tenant.
    #: Must be zero. This is the direction that catches a policy which ignores
    #: the GUC entirely, because the first count alone would still read zero.
    own_rows_under_other_context: int = 0


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Everything `fetch_snapshot` observed, and nothing it decided."""

    #: `SELECT current_user` — who the connection is acting as right now.
    current_user: str
    #: `SELECT session_user` — who it authenticated as. Equal to `current_user`
    #: unless something issued `SET ROLE`, which is exactly the "reached
    #: `app_user` indirectly" shape this check refuses.
    session_user: str
    #: `(rolbypassrls, rolsuper)` for every role worth reporting, keyed by name.
    role_posture: Mapping[str, RolePosture] = field(default_factory=dict)
    #: Roles the runtime role can assume that are SUPERUSER or BYPASSRLS. A
    #: `NOBYPASSRLS` role one `SET ROLE` away from a `BYPASSRLS` role has the
    #: attribute and not the property.
    escalation_memberships: tuple[str, ...] = ()
    schema_usage: tuple[SchemaUsage, ...] = ()
    table_privileges: tuple[TablePrivilege, ...] = ()
    rls_probes: tuple[RlsProbe, ...] = ()
    #: The module codes `active_modules` returned for this run.
    active_modules: tuple[str, ...] = ()


#: Printed verbatim when nothing was active. Worded as a refusal to claim
#: evidence rather than as a success, because an operator skimming a green
#: deploy log must not read this step as "the runtime identity is canonical".
VACUOUS_ADMISSION_NOTICE: Final[str] = (
    "NO ACTIVE MODULE DEMANDED A RUNTIME IDENTITY ASSERTION, SO NOTHING WAS "
    "PROVEN ABOUT THE RUNTIME ROLE. Every composed module is storage-only: its "
    "activation flag is unset, so its grants and RLS reachability were NOT "
    "required and were NOT checked. This step passed because it had nothing to "
    "assert, not because the runtime identity was found to be canonical."
)


def _identity_violations(snapshot: RuntimeSnapshot) -> list[str]:
    """The runtime must BE `app_user`, directly, with no escalation path."""
    violations: list[str] = []
    if snapshot.current_user != RUNTIME_ROLE:
        violations.append(
            f"RUNTIME IDENTITY: the application connection is "
            f"{snapshot.current_user!r}, required {RUNTIME_ROLE!r}. An active "
            "module's GRANTs and RLS policies are addressed to that role by "
            "name; another login reaches its tables through a different ACL "
            "and is not evaluated against those policies at all."
        )
    if snapshot.session_user != snapshot.current_user:
        violations.append(
            f"RUNTIME IDENTITY: the connection authenticated as "
            f"{snapshot.session_user!r} and is acting as "
            f"{snapshot.current_user!r}. {RUNTIME_ROLE!r} must be the LOGIN "
            "role, not a role reached with SET ROLE — a session that can set "
            "the role can reset it, so the restriction is advisory."
        )
    elif snapshot.session_user != RUNTIME_ROLE:
        violations.append(
            f"RUNTIME IDENTITY: the connection authenticated as "
            f"{snapshot.session_user!r}, required {RUNTIME_ROLE!r} directly."
        )
    for role in snapshot.escalation_memberships:
        violations.append(
            f"RUNTIME IDENTITY: {RUNTIME_ROLE!r} is a member of {role!r}, "
            "which is SUPERUSER or BYPASSRLS. Row-level security a role can "
            "step out of is decoration; the membership must be removed."
        )
    return violations


def _posture_violations(snapshot: RuntimeSnapshot) -> list[str]:
    """`NOSUPERUSER` AND `NOBYPASSRLS`, checked as two independent facts.

    Checking only `rolbypassrls` would certify `app_user SUPERUSER
    NOBYPASSRLS`, which bypasses RLS regardless of the flag. This is the exact
    distinction `tests/architecture/test_database_role_contract.py
    ::test_no_online_role_can_bypass_row_level_security` pins for the schema
    side, asserted here against the live connection.
    """
    observed = snapshot.role_posture.get(RUNTIME_ROLE)
    if observed is None:
        return [
            f"RUNTIME POSTURE: {RUNTIME_ROLE!r} was not found in pg_roles, so "
            "its BYPASSRLS/SUPERUSER attributes could not be read. Run "
            "scripts/bootstrap_database_roles.py once, as an operator."
        ]
    bypassrls, superuser = observed
    violations: list[str] = []
    if bypassrls:
        violations.append(
            f"RUNTIME POSTURE: {RUNTIME_ROLE!r} is {posture(*observed)}; "
            "BYPASSRLS makes every tenant policy on every active module's "
            "tables unenforced for this connection."
        )
    if superuser:
        violations.append(
            f"RUNTIME POSTURE: {RUNTIME_ROLE!r} is {posture(*observed)}; a "
            "superuser bypasses row-level security whether or not "
            "rolbypassrls is set."
        )
    return violations


def _grant_violations(
    snapshot: RuntimeSnapshot,
    modules: Sequence[ComposedModule],
) -> list[str]:
    """Every manifest-derived relation an active module needs must be reachable."""
    usage = {entry.schema: entry for entry in snapshot.schema_usage}
    privileges = {
        (entry.schema, entry.table, entry.privilege): entry
        for entry in snapshot.table_privileges
    }
    violations: list[str] = []
    for module in modules:
        schema_entry = usage.get(module.schema)
        if schema_entry is None:
            violations.append(
                f"RUNTIME GRANT: {module.module_code} is active but schema "
                f"{module.schema!r} was not observed at all; the snapshot is "
                "incomplete, which is a defect in the check rather than a "
                "clean database."
            )
            continue
        if not schema_entry.present:
            violations.append(
                f"RUNTIME GRANT: {module.module_code} is active but schema "
                f"{module.schema!r} does not exist. Storage must be migrated "
                "before a module may be activated."
            )
            continue
        if not schema_entry.usable:
            violations.append(
                f"RUNTIME GRANT: {RUNTIME_ROLE!r} has no USAGE on schema "
                f"{module.schema!r}, so nothing inside it is reachable."
            )
        for table in module.tenant_tables:
            for privilege in REQUIRED_TABLE_PRIVILEGES:
                entry = privileges.get((module.schema, table, privilege))
                if entry is None:
                    violations.append(
                        f"RUNTIME GRANT: {module.schema}.{table} {privilege} "
                        "was not probed; the snapshot does not cover an active "
                        "module's declared relation."
                    )
                    continue
                if not entry.present:
                    violations.append(
                        f"RUNTIME GRANT: {module.schema}.{table} does not "
                        f"exist, but {module.module_code} is active and its "
                        "manifest declares it."
                    )
                    break
                if not entry.held:
                    violations.append(
                        f"RUNTIME GRANT: {RUNTIME_ROLE!r} lacks {privilege} on "
                        f"{module.schema}.{table}, which active module "
                        f"{module.module_code} writes. Neither a table-level "
                        "nor a column-level privilege was found."
                    )
    return violations


def _rls_violations(
    snapshot: RuntimeSnapshot,
    modules: Sequence[ComposedModule],
) -> list[str]:
    """The probes must have RUN, and both cross-tenant counts must be zero."""
    probes = {(probe.schema, probe.table): probe for probe in snapshot.rls_probes}
    violations: list[str] = []
    for module in modules:
        key = (module.schema, module.probe_table)
        probe = probes.get(key)
        if probe is None:
            violations.append(
                f"RUNTIME RLS: no isolation probe was recorded for "
                f"{module.schema}.{module.probe_table}, so active module "
                f"{module.module_code} was admitted on a catalog reading "
                "alone. A policy that exists is not a policy that is enforced "
                "for this connection."
            )
            continue
        if not probe.executed:
            violations.append(
                f"RUNTIME RLS: the isolation probe for "
                f"{module.schema}.{module.probe_table} did not run "
                f"({probe.error or 'no reason recorded'}). An unprovable "
                "probe fails closed: activating a module is what takes on the "
                "obligation to make it provable."
            )
            continue
        if probe.foreign_rows_under_own_context:
            violations.append(
                f"RUNTIME RLS: {probe.foreign_rows_under_own_context} row(s) "
                f"belonging to another tenant were visible in "
                f"{module.schema}.{module.probe_table} while "
                f"{TENANT_GUC} named {probe.own_tenant!r}. Tenant isolation "
                "is not in force on this connection."
            )
        if probe.own_rows_under_other_context:
            violations.append(
                f"RUNTIME RLS: {probe.own_rows_under_other_context} row(s) of "
                f"tenant {probe.own_tenant!r} stayed visible in "
                f"{module.schema}.{module.probe_table} after {TENANT_GUC} was "
                f"switched to {probe.other_tenant!r}. The policy is not "
                "reading the tenant context the application sets."
            )
    return violations


def runtime_admission_violations(snapshot: RuntimeSnapshot) -> tuple[str, ...]:
    """Every reason to refuse this runtime connection, or an empty tuple.

    PURE: it reads the snapshot and the declarations above and touches nothing
    else, which is what lets the sensitivity proof in
    `tests/unit/test_runtime_admission.py` drive it with synthetic inputs and
    require it to FIRE.

    An EMPTY active set yields no violations by construction. That is the
    intended production behaviour today and the reason the entrypoint prints
    `VACUOUS_ADMISSION_NOTICE` rather than a bare success line.
    """
    modules: list[ComposedModule] = []
    violations: list[str] = []
    for code in snapshot.active_modules:
        module = MODULES_BY_CODE.get(code)
        if module is None:
            violations.append(
                f"RUNTIME DECLARATION: {code!r} was reported active but is not "
                "a module declared in app/runtime_admission.py. An activation "
                "flag with no declaration would assert nothing at all."
            )
            continue
        modules.append(module)

    if not modules:
        return tuple(violations)

    violations.extend(_identity_violations(snapshot))
    violations.extend(_posture_violations(snapshot))
    violations.extend(_grant_violations(snapshot, modules))
    violations.extend(_rls_violations(snapshot, modules))
    return tuple(violations)


def runtime_admission_report(snapshot: RuntimeSnapshot) -> tuple[str, ...]:
    """The transcript: what was considered, what ran, what was skipped and why.

    Built from the SAME snapshot the decision reads, so a green step cannot
    print a description of a run that did not happen.
    """
    active = set(snapshot.active_modules)
    lines = [
        "runtime admission — read-only, runtime credential (no MIGRATION_DATABASE_URL)",
        f"  observed connection: current_user={snapshot.current_user!r} "
        f"session_user={snapshot.session_user!r}",
    ]
    observed_posture = snapshot.role_posture.get(RUNTIME_ROLE)
    if observed_posture is not None:
        lines.append(
            f"  observed {RUNTIME_ROLE!r} posture: {posture(*observed_posture)}"
        )
    lines.append("  composed modules considered:")
    for module in COMPOSED_MODULES:
        if module.module_code in active:
            lines.append(
                f"    ACTIVE   {module.module_code} "
                f"({module.activation_env_var}=true, {module.activation_owner}) "
                f"— required: direct {RUNTIME_ROLE} identity, NOSUPERUSER/"
                f"NOBYPASSRLS, USAGE on {module.schema}, "
                f"{'/'.join(REQUIRED_TABLE_PRIVILEGES)} on "
                f"{len(module.tenant_tables)} table(s), same-tenant and "
                f"cross-tenant RLS probes on {module.schema}."
                f"{module.probe_table}"
            )
        else:
            lines.append(
                f"    SKIPPED  {module.module_code} "
                f"({module.activation_env_var} is not 'true') — storage exists "
                "and is audited by the migration gate; its app_user grants and "
                "RLS reachability were NOT required and NOT checked"
            )
            if module.note:
                # The reviewed reason this module is storage-only, printed
                # rather than left in a source file: an operator reading a
                # deploy log must be able to tell a deliberate ratchet from a
                # forgotten flag without opening the repository.
                lines.append(f"             why: {module.note}")
        if module.platform_tables:
            lines.append(
                f"             platform plane NOT required for "
                f"{RUNTIME_ROLE}: "
                + ", ".join(
                    f"{module.schema}.{table}" for table in module.platform_tables
                )
                + " (ADR-0023 revokes it deliberately)"
            )
    for probe in snapshot.rls_probes:
        if not probe.executed:
            lines.append(
                f"  RLS probe {probe.schema}.{probe.table}: DID NOT RUN "
                f"({probe.error or 'no reason recorded'})"
            )
            continue
        emptiness = (
            ""
            if probe.own_rows
            else " — probe table held no rows for the probe "
            "tenant, so isolation was exercised structurally, not against data"
        )
        lines.append(
            f"  RLS probe {probe.schema}.{probe.table}: own_rows="
            f"{probe.own_rows} foreign_rows_under_own_context="
            f"{probe.foreign_rows_under_own_context} "
            f"own_rows_under_other_context="
            f"{probe.own_rows_under_other_context}{emptiness}"
        )
    if not active:
        lines.append(f"  {VACUOUS_ADMISSION_NOTICE}")
    return tuple(lines)


__all__ = [
    "COLUMN_GRANTABLE_PRIVILEGES",
    "COMPOSED_MODULES",
    "MODULES_BY_CODE",
    "REQUIRED_TABLE_PRIVILEGES",
    "RUNTIME_ROLE",
    "TENANT_GUC",
    "VACUOUS_ADMISSION_NOTICE",
    "ComposedModule",
    "RlsProbe",
    "RuntimeSnapshot",
    "SchemaUsage",
    "TablePrivilege",
    "active_modules",
    "runtime_admission_report",
    "runtime_admission_violations",
]
