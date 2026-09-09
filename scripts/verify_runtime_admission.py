#!/usr/bin/env python3
"""Admit — or refuse — the RUNTIME database connection. READ-ONLY, NO DDL.

This is the deploy step that runs AFTER `alembic upgrade heads` and BEFORE the
application containers are recreated, and it is the ONLY step in
`scripts/deploy.sh` that must not be handed `MIGRATION_DATABASE_URL`. It exists
to answer a question the migration gate structurally cannot: the migration gate
connects as `app_admin` and inspects the schema, so it certifies that the
GRANTs and RLS policies are correct — for the role they name. Whether the
application will actually connect AS that role is a property of the runtime
credential, and only a connection made with the runtime credential can observe
it.

## What it asserts, and only for ACTIVE modules

For every module `app.runtime_admission.active_modules` reports active:

1. `current_user` is `app_user`, DIRECTLY — equal to `session_user`, not
   reached with `SET ROLE`, and not one `SET ROLE` away from a SUPERUSER or
   BYPASSRLS role;
2. that role is `NOSUPERUSER` and `NOBYPASSRLS`, checked as two independent
   attributes because either one alone defeats row-level security;
3. every manifest-derived relation the module declares is reachable with
   SELECT/INSERT/UPDATE/DELETE, probed at BOTH table and column granularity
   because a column-level grant is invisible to `has_table_privilege`;
4. real isolation probes RUN: the tenant GUC is set, own rows are read, and
   then rows are read across the tenant boundary in both directions and
   required to be zero.

A composed-but-DISABLED module is deliberately admitted with none of that. Its
tables exist and the migration gate audits them; its `app_user` grants and RLS
reachability are not required, because ADR-0003 created that storage ahead of
any cutover and demanding runtime grants for it would refuse a deploy for
having done the right thing.

## Why it prints so much

The active set is empty on ERP today, so this check passes without asserting
anything — which is exactly the unmonitored region ADR-0018 forbids when it is
silent. So it always prints every module it considered, the flag that would
activate it, every check it ran and every check it skipped, and when the active
set is empty it prints `VACUOUS_ADMISSION_NOTICE` in as many words.

## Usage

    DATABASE_URL=postgresql+psycopg://app_user@host/db \\
        python scripts/verify_runtime_admission.py

Optional, and REQUIRED once any module is active — two distinct tenant UUIDs
the isolation probes run against:

    RUNTIME_ADMISSION_TENANT_ID=<uuid>
    RUNTIME_ADMISSION_OTHER_TENANT_ID=<uuid>

Exit codes: 0 admitted, 1 admission refused, 2 usage/connection error.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import uuid

# Direct execution sets ``sys.path[0]`` to ``scripts/`` rather than the
# repository root, and deploy intentionally uses the documented
# ``python scripts/verify_runtime_admission.py`` entrypoint. Same preamble as
# scripts/bootstrap_database_roles.py, for the same reason.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql

from app.runtime_admission import (
    COLUMN_GRANTABLE_PRIVILEGES,
    COMPOSED_MODULES,
    REQUIRED_TABLE_PRIVILEGES,
    RUNTIME_ROLE,
    TENANT_GUC,
    ComposedModule,
    RlsProbe,
    RuntimeSnapshot,
    SchemaUsage,
    TablePrivilege,
    active_modules,
    runtime_admission_report,
    runtime_admission_violations,
)

RUNTIME_URL_VAR = "DATABASE_URL"
TENANT_VAR = "RUNTIME_ADMISSION_TENANT_ID"
OTHER_TENANT_VAR = "RUNTIME_ADMISSION_OTHER_TENANT_ID"

#: `SELECT current_user` and `SELECT session_user` in one round trip. They
#: differ only after a `SET ROLE`, which is the "reached app_user indirectly"
#: shape the contract refuses.
IDENTITY_SQL = "SELECT current_user::text, session_user::text"

#: The runtime role's own attributes, plus whichever login actually connected,
#: so a refusal can report the posture of the role that IS in use as well as
#: the one that should be. Names are bound, never interpolated.
POSTURE_SQL = """
SELECT rolname::text, rolbypassrls, rolsuper
FROM pg_roles
WHERE rolname = ANY(%(roles)s::text[])
"""

#: Every SUPERUSER or BYPASSRLS role the runtime role can assume.
#:
#: `NOBYPASSRLS` is an attribute of a role, not a property of a session: a role
#: that is a member of a BYPASSRLS role can `SET ROLE` to it and read past every
#: policy, and `pg_roles` would still report the attribute this check wants.
#: `pg_has_role(..., 'USAGE')` is the membership question that closes that gap.
ESCALATION_SQL = """
SELECT escalated.rolname::text
FROM pg_roles AS escalated
WHERE (escalated.rolsuper OR escalated.rolbypassrls)
  AND escalated.rolname <> %(role)s::text
  AND pg_has_role(%(role)s::text, escalated.oid, 'USAGE')
ORDER BY escalated.rolname
"""

#: Schema existence and USAGE for the runtime role.
#:
#: The `LEFT JOIN` onto `pg_namespace` rather than a bare
#: `has_schema_privilege(role, name, 'USAGE')` is deliberate: the by-name form
#: RAISES for a schema that does not exist, which would turn "this module was
#: never migrated" into a connection error instead of a named violation.
SCHEMA_USAGE_SQL = """
SELECT wanted.schema_name::text,
       namespace_catalog.oid IS NOT NULL AS schema_present,
       COALESCE(
           has_schema_privilege(%(role)s::text, namespace_catalog.oid, 'USAGE'),
           false
       ) AS usable
FROM unnest(%(schemas)s::text[]) AS wanted(schema_name)
LEFT JOIN pg_namespace AS namespace_catalog
       ON namespace_catalog.nspname = wanted.schema_name
ORDER BY wanted.schema_name
"""

#: Reachability for every declared `(schema, table, privilege)` triple.
#:
#: `CASE` rather than `OR` because SQL does not promise short-circuit
#: evaluation and `has_any_column_privilege` RAISES for `DELETE`, which has no
#: column-level form. The table-level answer is taken first; the column-level
#: probe runs only for the privileges PostgreSQL can grant per column. A check
#: that asked only `has_table_privilege` would report a column-granted table as
#: unreachable and refuse a correct deploy.
TABLE_PRIVILEGE_SQL = """
SELECT wanted.schema_name::text,
       wanted.table_name::text,
       privileges.privilege::text,
       relation_catalog.oid IS NOT NULL AS relation_present,
       CASE
           WHEN relation_catalog.oid IS NULL THEN false
           WHEN has_table_privilege(
                    %(role)s::text, relation_catalog.oid, privileges.privilege
                ) THEN true
           WHEN privileges.privilege = ANY(%(column_grantable)s::text[])
                THEN has_any_column_privilege(
                    %(role)s::text, relation_catalog.oid, privileges.privilege
                )
           ELSE false
       END AS held
FROM unnest(%(schemas)s::text[], %(tables)s::text[])
         AS wanted(schema_name, table_name)
CROSS JOIN unnest(%(privileges)s::text[]) AS privileges(privilege)
LEFT JOIN pg_namespace AS namespace_catalog
       ON namespace_catalog.nspname = wanted.schema_name
LEFT JOIN pg_class AS relation_catalog
       ON relation_catalog.relnamespace = namespace_catalog.oid
      AND relation_catalog.relname = wanted.table_name
      AND relation_catalog.relkind IN ('r', 'p')
ORDER BY wanted.schema_name, wanted.table_name, privileges.privilege
"""

#: Prime the transaction-local tenant GUC the module policies read.
#:
#: `set_config(..., true)` is transaction-local, so the value dies with the
#: read-only transaction this check opens; the GUC NAME and the tenant are both
#: bound parameters, the name coming from `runtime_admission.TENANT_GUC` so the
#: probe cannot drift from the constant the declaration states. This is the
#: only "write" the check performs and it writes no row — a read-only
#: transaction accepts it.
#:
#: `app/rls.py` remains the sole tenant-GUC writer for the APPLICATION, and
#: `tests/architecture/test_tenancy_mapping_boundary.py` enforces that over
#: `app/`. This is a deploy-time, read-only probe outside the request path — it
#: primes the context in order to OBSERVE the policy, exactly as
#: `tests/integration/test_party_person_catalog_prerequisite.py` already does;
#: it is not a second runtime writer of tenant scope.
SET_TENANT_SQL = "SELECT set_config(%(guc)s, %(tenant)s, true)"


def _module_relations(
    modules: tuple[ComposedModule, ...],
) -> tuple[list[str], list[str]]:
    """The declared `(schema, table)` pairs as two parallel bound arrays."""
    schemas: list[str] = []
    tables: list[str] = []
    for module in modules:
        for table in module.tenant_tables:
            schemas.append(module.schema)
            tables.append(table)
    return schemas, tables


def _probe_relation(
    conn: psycopg.Connection,
    module: ComposedModule,
    own_tenant: str,
    other_tenant: str,
) -> RlsProbe:
    """Really read one module table under two tenant contexts.

    Three counts, three statements, one relation. The relation must appear in a
    `FROM` clause, where a bound parameter cannot go, so it is composed with
    `psycopg.sql.Identifier` — the driver quotes the schema and table itself,
    exactly as `scripts/bootstrap_database_roles.py` composes a role name. No
    name is ever formatted into SQL text by this programme.

    A failure is CAPTURED, not raised: "the probe could not run" is a named
    violation with a reason, and losing that reason to a traceback would tell
    an operator less than the check already knows.
    """
    relation = sql.Identifier(module.schema, module.probe_table)
    own_rows_sql = sql.SQL(
        "SELECT count(*)::bigint FROM {} WHERE tenant_id = %(tenant)s::uuid"
    ).format(relation)
    foreign_rows_sql = sql.SQL(
        "SELECT count(*)::bigint FROM {} WHERE tenant_id <> %(tenant)s::uuid"
    ).format(relation)

    try:
        with conn.transaction():
            conn.execute(SET_TENANT_SQL, {"guc": TENANT_GUC, "tenant": own_tenant})
            own_rows = int(
                conn.execute(own_rows_sql, {"tenant": own_tenant}).fetchone()[0]
            )
            foreign_rows = int(
                conn.execute(foreign_rows_sql, {"tenant": own_tenant}).fetchone()[0]
            )
            # Switch the context and ask for the FIRST tenant's rows. This is
            # the direction that catches a policy which ignores the GUC
            # entirely: the count above would read zero on an empty table
            # whether or not isolation worked, while this one reads non-zero
            # exactly when the previous context's rows survived the switch.
            conn.execute(SET_TENANT_SQL, {"guc": TENANT_GUC, "tenant": other_tenant})
            own_rows_elsewhere = int(
                conn.execute(own_rows_sql, {"tenant": own_tenant}).fetchone()[0]
            )
    except psycopg.Error as exc:
        return RlsProbe(
            schema=module.schema,
            table=module.probe_table,
            executed=False,
            error=str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc),
            own_tenant=own_tenant,
            other_tenant=other_tenant,
        )

    return RlsProbe(
        schema=module.schema,
        table=module.probe_table,
        executed=True,
        own_tenant=own_tenant,
        other_tenant=other_tenant,
        own_rows=own_rows,
        foreign_rows_under_own_context=foreign_rows,
        own_rows_under_other_context=own_rows_elsewhere,
    )


def fetch_snapshot(
    conn: psycopg.Connection,
    *,
    active: tuple[ComposedModule, ...],
    own_tenant: str | None,
    other_tenant: str | None,
) -> RuntimeSnapshot:
    """Run the SQL. The thin seam — it observes and decides nothing.

    Every module is inventoried, not only the active ones, so the transcript
    can report a disabled module's real state instead of an assumption. What
    changes with activation is which observations are REQUIRED, and that
    decision lives entirely in `runtime_admission_violations`.
    """
    identity = conn.execute(IDENTITY_SQL).fetchone()
    current_user, session_user = str(identity[0]), str(identity[1])

    posture_rows = conn.execute(
        POSTURE_SQL, {"roles": sorted({RUNTIME_ROLE, current_user, session_user})}
    ).fetchall()
    role_posture = {str(row[0]): (bool(row[1]), bool(row[2])) for row in posture_rows}

    escalation = tuple(
        str(row[0])
        for row in conn.execute(ESCALATION_SQL, {"role": RUNTIME_ROLE}).fetchall()
    )

    schemas = [module.schema for module in COMPOSED_MODULES]
    usage_rows = conn.execute(
        SCHEMA_USAGE_SQL, {"role": RUNTIME_ROLE, "schemas": schemas}
    ).fetchall()
    schema_usage = tuple(
        SchemaUsage(schema=str(row[0]), present=bool(row[1]), usable=bool(row[2]))
        for row in usage_rows
    )

    relation_schemas, relation_tables = _module_relations(COMPOSED_MODULES)
    privilege_rows = conn.execute(
        TABLE_PRIVILEGE_SQL,
        {
            "role": RUNTIME_ROLE,
            "schemas": relation_schemas,
            "tables": relation_tables,
            "privileges": list(REQUIRED_TABLE_PRIVILEGES),
            "column_grantable": sorted(COLUMN_GRANTABLE_PRIVILEGES),
        },
    ).fetchall()
    table_privileges = tuple(
        TablePrivilege(
            schema=str(row[0]),
            table=str(row[1]),
            privilege=str(row[2]),
            present=bool(row[3]),
            held=bool(row[4]),
        )
        for row in privilege_rows
    )

    probes: list[RlsProbe] = []
    for module in active:
        if not own_tenant or not other_tenant or own_tenant == other_tenant:
            probes.append(
                RlsProbe(
                    schema=module.schema,
                    table=module.probe_table,
                    executed=False,
                    error=(
                        f"{TENANT_VAR} and {OTHER_TENANT_VAR} must both be set "
                        "to DISTINCT tenant UUIDs before a module may be "
                        "activated; isolation cannot be proved against one "
                        "tenant"
                    ),
                )
            )
            continue
        probes.append(_probe_relation(conn, module, own_tenant, other_tenant))

    return RuntimeSnapshot(
        current_user=current_user,
        session_user=session_user,
        role_posture=role_posture,
        escalation_memberships=escalation,
        schema_usage=schema_usage,
        table_privileges=table_privileges,
        rls_probes=tuple(probes),
        active_modules=tuple(module.module_code for module in active),
    )


def _tenant(var: str) -> str | None:
    """Read one probe tenant, refusing anything that is not a UUID.

    Validated in Python and then BOUND as a parameter — belt and braces, so the
    value can never be mistaken for SQL even if a future edit moves it.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        print(f"{var}={raw!r} is not a UUID", file=sys.stderr)
        return None


def main() -> int:
    url = os.environ.get(RUNTIME_URL_VAR, "").strip()
    if not url:
        print(
            f"{RUNTIME_URL_VAR} is not set. This check deliberately uses the "
            "RUNTIME credential: verifying the migration identity again would "
            "prove nothing about the connection the application serves on.",
            file=sys.stderr,
        )
        return 2

    if os.environ.get("MIGRATION_DATABASE_URL", "").strip() == url:
        print(
            "DATABASE_URL and MIGRATION_DATABASE_URL are the same connection "
            "string. The application must not serve requests on the migration "
            "executor's credential; that role is BYPASSRLS by contract.",
            file=sys.stderr,
        )
        return 2

    active = active_modules(os.environ)
    own_tenant = _tenant(TENANT_VAR)
    other_tenant = _tenant(OTHER_TENANT_VAR)

    try:
        connect_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(connect_url, autocommit=False) as conn:
            # Belt and braces with the architecture test that forbids write
            # SQL in this programme: the SERVER now refuses a write on this
            # connection too, so a future edit cannot quietly mutate the
            # database this check was added to inspect.
            conn.read_only = True
            snapshot = fetch_snapshot(
                conn,
                active=active,
                own_tenant=own_tenant,
                other_tenant=other_tenant,
            )
            conn.rollback()
    except psycopg.Error as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    for line in runtime_admission_report(snapshot):
        print(line)

    violations = runtime_admission_violations(snapshot)
    if not violations:
        return 0
    # The violations already carry their own category prefix
    # (`RUNTIME IDENTITY:`, `RUNTIME POSTURE:`, `RUNTIME GRANT:`,
    # `RUNTIME RLS:`, `RUNTIME DECLARATION:`), so this header names the step
    # rather than repeating the category on every line.
    print("", file=sys.stderr)
    print(
        f"RUNTIME ADMISSION REFUSED for active module(s): "
        f"{', '.join(snapshot.active_modules)}",
        file=sys.stderr,
    )
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
