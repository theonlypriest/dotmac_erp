# Runbook — ERP database backup and restore

Until 2026-08-30 ERP had no restore procedure. It had a backup producer that
ran `pg_dump` on one database, and `scripts/restore_from_backup.py`, which
extracts `COPY` blocks and skips every `CREATE`, `ALTER`, `DROP`, `GRANT` and
`REVOKE`. Roles, `app_admin`, the least-privilege `dotmac_erp_app` login and
every GRANT the RLS policies depend on were captured by no artifact.

A restored cluster with correct rows and no roles cannot serve. That is the
failure mode this runbook exists to prevent, and the verification steps below
assert against it specifically.

Measured evidence:
`docs/inventories/2026-08-30-erp-production-infrastructure-preflight.md` § 6.

## What a backup consists of

Two artifacts per run. **They are useless apart.**

| artifact | contains |
|---|---|
| `dotmac_erp_<ts>.globals.sql.gz` | roles, role memberships, cluster-level GRANTs (`pg_dumpall --globals-only`) |
| `dotmac_erp_<ts>.dump` | the database, `pg_dump` custom format (`-Fc`) |

Custom format is used because it is the only format `pg_restore` can inspect
without executing. `pg_restore --list` walks the archive's table of contents
and fails on a truncated or corrupt file, which a byte count cannot detect.
`scripts/backup_erp_db.sh` runs that check before uploading and refuses a
globals dump containing no `CREATE ROLE`.

## Taking a backup

```
./scripts/backup_erp_db.sh
```

Reads `POSTGRES_*` from `.env`. It **refuses** if a key is duplicated there,
because Compose reads the last occurrence while a naive `sed` reads the first —
a live disagreement in production `.env` today. Deduplicate rather than guess.

Retention keeps the last `KEEP_LAST` **runs** (default 5), removing every file
belonging to an expired timestamp. It counts runs, not files, because each run
now writes two artifacts.

`SKIP_UPLOAD=1` skips rclone (used by the CI rehearsal).

## Restoring

**Order is not optional. Globals first, then the database.** `pg_restore`
emits `ALTER ... OWNER TO` and `GRANT ... TO`; if the role does not exist those
statements fail, and with `--exit-on-error` the restore stops rather than
producing a database whose ownership is quietly wrong.

```
./scripts/restore_erp_db.sh \
    --globals /var/backups/db/dotmac_erp_<ts>.globals.sql.gz \
    --dump    /var/backups/db/dotmac_erp_<ts>.dump \
    --target-container <disposable postgres container> \
    --target-db dotmac_erp_restore \
    --require-roles app_admin,dotmac_erp_app
```

The script:

1. verifies the archive parses **before touching the target**;
2. restores globals into the cluster;
3. drops and recreates the target database;
4. `pg_restore --exit-on-error`;
5. **asserts** each `--require-roles` role exists, that tables were created,
   and reports the Alembic heads found.

It exits non-zero if any required role is missing. A restore that produced rows
but no roles is reported as **incomplete**, not as success.

### It refuses production

`--target-container dotmac_pg_local` or `--target-db dotmac_erp` is refused.
Restoring onto the live cluster is an outage, and a rehearsal script is exactly
where that mistake gets made. A genuine disaster recovery onto the real name
requires setting `ALLOW_PRODUCTION_NAME=1` deliberately.

## Role capture carries no password material

`pg_dumpall --globals-only` alone emits
`CREATE ROLE … PASSWORD 'SCRAM-SHA-256$…'` for every login role, and this
artifact is uploaded offsite. The capture therefore passes
**`--no-role-passwords`**, and the script additionally **refuses to upload** a
globals dump in which a verifier appears — the flag is one edit away from being
dropped, so the content is checked as well as the argument.

Login material is not part of the bundle. It is reinstalled after a restore from
the approved secret source — for the cluster superuser that is
`secret/dotmac/postgres/erp-shared-primary/postgres`.

This corrects the procedure as first written, which omitted the flag. Production
never ran that version, so no verifier left the host.

## What this procedure does NOT yet prove

Stated here, in the receipt itself, because this document is what a future
reader will treat as the claim — not the pull request that introduced it.

This is a **first increment**, not a met standard. The CI rehearsal proves
globals, role memberships, ownership, grants, schema, data and Alembic heads
survive a dump/restore cycle. It does **not** prove any of:

- **tablespaces** — not exercised; the cluster uses the default today, so a
  restore onto a host with different tablespace layout is unproven;
- **extensions** — production runs PostGIS (`postgis/postgis:16-3.4-alpine`),
  and extension presence/version equivalence after restore is not asserted;
- **default privileges** (`ALTER DEFAULT PRIVILEGES`) — not compared, so objects
  created after a restore may not inherit the grants they do today;
- **row and column ACLs** — not compared;
- **production bytes** — the target is an ephemeral CI container, not a fresh
  isolated instance holding a real ~8 GB artifact.

Michael's standard is wider than what this meets: *"a `pg_dump` data file alone
is not a restore proof."* A restore must reproduce globals, role memberships,
credential bindings, tablespaces, extensions, schemas and migration heads,
ownership, grants and default privileges, row and column ACLs, and application
data — into a **fresh isolated instance**, proving schema/data/catalog
equivalence, application readiness, migration operation, tenant/platform role
separation, documented rollback, and no dependence on undocumented cluster state.

**The shared `PostgresRecoveryBundle.v1` facility
(`dotmac_deployment_foundation.recovery`) supersedes this procedure.** It defines
thirteen required components — database dump, role closure, role attributes,
memberships, object ownership, default privileges, schema privileges, object
privileges, fine-grained ACLs, row security, extensions, tablespaces and
migration heads — and is what makes the `PROVED` assurance level reachable. This
script produces **two** of the thirteen, so in that vocabulary its output is a
`DATA_EXPORT` plus a partial role capture, and it can never be a
`RECOVERY_BUNDLE`. ERP cannot adopt it yet: the module ships in Foundation
**0.3.0a1 source** while the newest **published** tag is 0.2.0a2, and a source
version is not a pinnable release. ERP rebuilds
against it when it lands. Until then, treat a green rehearsal here as evidence
that the mechanism is sound — not as evidence that ERP holds a proved recovery
bundle. **No ERP deployment may proceed on the strength of this document alone.**

## Rehearsal status — read this before claiming the backup works

| rehearsal | status |
|---|---|
| **Mechanism**, against a freshly migrated ERP cluster with real bootstrapped roles, restored into a second disposable PostgreSQL container, with a sensitivity proof that a roleless backup is rejected | **runs in CI** on every build, in the `Docker Build & Health Check` job |
| **Production bytes** — restoring an actual production artifact (~8 GB) onto a disposable host | **NOT DONE.** No disposable PostgreSQL target exists with the capacity and the clearance to hold production data. `85.190.246.211` is leased exclusively to Deployment Foundation and must not be used. This is an open blocker. |

The CI rehearsal proves the *mechanism* is correct — that globals are captured,
that the archive parses, that a restore reproduces roles, privileges and data,
and that the verification actually bites. It does **not** prove any particular
production artifact is restorable. Those are different claims and only the
first is currently evidenced.

## The sensitivity proof

CI strips `CREATE ROLE`/`ALTER ROLE` out of the globals artifact and re-runs the
restore, reproducing exactly the defect that was live in production. That run
**must fail**. If it ever succeeds, the role assertion has stopped detecting
anything and the gate is decorative — the step fails loudly in that case rather
than passing quietly.

## Disaster recovery outline

1. Provision a PostgreSQL 16 instance (production uses `postgis/postgis:16-3.4-alpine`;
   PostGIS is required — the schema uses spatial types).
2. Restore globals, then the database, per the command above with
   `ALLOW_PRODUCTION_NAME=1`.
3. Confirm `app_admin` and `dotmac_erp_app` exist and that
   `dotmac_erp_app` is `NOSUPERUSER`/`NOBYPASSRLS`.
4. Confirm the Alembic heads match the seven the deployed image expects.
5. Only then point the application at it.

Step 3 is the one that was impossible before this change.

---

## Measured: ERP's role closure is 8, and the declarations name 5

Taken read-only from the live `dotmac_erp` catalog on 2026-08-30, using the
reference-site model `PostgresRecoveryBundle.v1` defines — ownership, table
ACLs, schema ACLs, default privileges, policies, and role memberships.

| role | referenced by |
|---|---|
| `app_admin` | ownership, schema_acl, table_acl |
| `app_user` | schema_acl, table_acl |
| `dotmac_erp_app` | default_privilege, membership, schema_acl, table_acl |
| `outbox_dispatcher` | schema_acl |
| `platform_api` | schema_acl, table_acl |
| `platform_outbox_dispatcher` | schema_acl |
| `postgres` | ownership, schema_acl |
| `prom_exporter` | membership |

ERP's checked-in declarations total **five**: `ROLE_CONTRACT` names `app_admin`,
`app_user`, `platform_api`; `RELAY_DISPATCHER_CONTRACT` names
`outbox_dispatcher`, `platform_outbox_dispatcher`. The descriptor's `[[roles]]`
are *service* roles (app, worker, beat) and declare no database role at all.

**The closure is short by three**, and the omissions are the instructive part:

- **`dotmac_erp_app`** — the production runtime login, the role the application
  actually connects as. No contract names it. A restore rebuilt from the
  declarations would come back with every table present and **no role for the
  application to log in as**.
- **`prom_exporter`** — reachable only through `membership`. Any closure derived
  from table privileges misses it outright. This is the direct-grant trap the
  facility calls out: `information_schema.table_privileges` sees direct grants
  only, not membership, not `PUBLIC`, not column-level.
- **`postgres`** — owns objects in this database.

And note `outbox_dispatcher` and `platform_outbox_dispatcher` are referenced
**only via `schema_acl`** — they reach their tables through `SECURITY DEFINER`
routines and hold no table privilege a naive walk would see. ERP happens to
declare them, in a *separate* contract; a closure derived from table ACLs would
have missed both regardless.

This is the same shape as the measurement that motivated the facility: a
Vendor CP restore failing with 114 missing-role errors naming five roles, when
everything documented declared three.

**Consequence for adoption.** The role closure must be **derived from the
catalog**, never assembled from the declarations. The facility enforces exactly
that — `restore_plan` refuses a bundle with no `ROLE_CLOSURE` component *even
when the descriptor names every role*, and no function in the package emits role
DDL, so a validator can never manufacture the role it is checking for. These
eight names are recorded here as measured evidence, **not** as a declaration to
restore from.
