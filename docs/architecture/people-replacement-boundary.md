# People replacement boundary

Status: **Employment Type authority is cut over in repository behavior;
deployment activation and production data movement are not claimed**.

> **Destination naming.** The replacement target is the commercial **Dotmac
> ERP** product assembly, not an internally framed `dotmac_backoffice`
> application — Michael corrected that on 2026-08-19
> (`dotmac-erp-recomposition-into-domain-modules`; the earlier
> `erp-hardening-is-containment-backoffice-is-the-destination` entry is
> superseded on naming and retained only for its cutover mechanics).
>
> **Four names, and only four** — do not introduce a fifth:
>
> | Role | Name |
> | --- | --- |
> | Product / repository slug | `dotmac-erp` |
> | Authority / assembly id | `asm-dotmac-erp` |
> | Historical source repository | `dotmac_erp` |
> | Human name | Dotmac ERP |
>
> The writer ledger's `next_owner` is an AUTHORITY field, so it reads
> `asm-dotmac-erp/dotmac-people`. Starter dossier `candidate_consumers` name
> the product slug `dotmac-erp`. The legacy `dotmac_erp` repository is the
> extraction SOURCE and is archived or renamed at final retirement.
>
> The `backoffice:people:read` scope, the
> `/api/v1/sync/backoffice/people/projection` route and the
> `backoffice.people.projection.v1` contract keep their names deliberately.
> They are shipped wire identifiers held by a live consumer; renaming them is a
> breaking API change, not a documentation correction, and it is out of scope
> here.

## Ownership

| Concern | Current owner | Intended owner after an authorized cutover |
| --- | --- | --- |
| Person identity used by employment | `public.people` in Dotmac ERP | kernel `Party` + `PartyPerson` in the composed Dotmac ERP product |
| Employment Type catalogue | `dotmac-people==0.1.0a2`, composed behind `app.services.people.hr.employment_types` | Cut over in this first thin slice |
| Remaining employment directory, positions and assignments | Dotmac ERP HR services and `hr.*` tables | Later released `dotmac-people` slices composed by Dotmac ERP |
| Credentials, sessions, roles and permissions | Dotmac ERP | Not part of this slice |
| Payroll, bank, compensation, attendance and location data | Their existing ERP domains | Not part of `dotmac-people` |
| Personal profile data that supplements `public.people` | `hr.employee` | **Unresolved**; it is neither a `dotmac-people` field nor approved kernel `PartyPerson` data |
| Employee documents, qualifications, certifications and dependants | `hr.employee_*` tables | **Unresolved**; file storage alone is not business ownership |
| Skill catalogue and employee proficiency | `hr.skill` and `hr.employee_skill` | selected `dotmac-workforce` capability, but blocked until that distribution is released and its contract is accepted |

ERP pins the immutable `dotmac-people==0.1.0a2` release and composes its
independent `people` lineage at `pe_0001_people_directory`. One ERP assembly
owner imports its public Employment Type surface. All runtime reads and
commands reach that owner; it validates the already-primed Organization/Tenant
identity and never opens or ends a transaction. Every command synchronously
projects the same record into `hr.employment_type` before returning, using the
same Session and flush boundary.

The legacy relation is retained only because Employee and Payroll foreign keys
still reference its stable UUIDs. `_EmploymentTypeProjector.project` is its
exact-one writer. It preserves identity, tenant, owned fields, timestamps and
the available ERP audit actor, deletes nothing, and is checked by a two-way
architecture ratchet. The other People entities remain ERP-owned.

The complete disposition of the legacy `hr.employee` columns and its extended
tables is recorded in
`docs/inventories/people-employee-field-ownership.tsv`. An `unresolved` row is
a cutover blocker, not permission to place the field in a JSON escape hatch or
to copy it into `dotmac-people`.
`tests/integration/test_people_employee_ownership_catalog.py` proves that the
ledger covers every migrated `hr.employee` column and that every classified
extended entity exists after `alembic upgrade heads`; the architecture test
separately keeps the ledger synchronized with mapped model intent.

## Versioned source read projection

The composed product may read one tenant at a time through:

`GET /api/v1/sync/backoffice/people/projection`

The API requires an API key with the explicit
`backoffice:people:read` scope. Legacy keys with an empty scope set are
rejected. The organization comes from the authenticated service principal;
the request cannot supply or override it. The database session is tenant
primed and every projection query also carries an explicit
`organization_id` predicate.

Required parameters and continuation:

- `entity` is one of `party_person`, `department`, `designation`,
  `employment_type`, `employee`, `position`, `position_assignment`.
- `after` is the last source UUID from the previous page.
- `limit` is 1–500 and defaults to 200.
- `next_after` is present only when another page exists.

The response contract is `backoffice.people.projection.v1`. Employment Type is
now read from the module owner, then UUID-ordered and paged without consulting
the compatibility table. Every other entity remains sourced from its legacy
ERP owner. Every item carries
the preserved ERP UUID, the source timestamp when one exists, and a SHA-256
fingerprint over exactly the projected target fields. The fingerprint—not an
ERP `updated_at` value—is the comparison authority because legacy bulk writers
do not all advance timestamps.

UUID keyset order prevents offset drift. A scan is not a database snapshot
across HTTP requests: the backfill/reconciler must repeat an entity scan until
the source-ID/fingerprint set reaches a fixed point. Absence is meaningful only
after a complete scan; this contract makes no incremental tombstone claim.

For the remaining entities this endpoint is still a one-way extraction seam
from the historical ERP owner into a later module slice. Employment Type no
longer uses it as a reverse feed: its projection payload is built from module
state. The other entities still have no target-side importer, fingerprint
ledger or reconciler.

The shipped v1 projection is also **not deterministic enough for cutover**:
position fingerprints include `is_department_head`, which is evaluated against
the process date rather than an explicit `as_of`. A scan crossing an assignment
date boundary can therefore change without a source write. A successor
versioned projection must accept an explicit effective date before bootstrap or
fixed-point reconciliation is admissible; the published v1 contract must not be
redefined in place.

## Employment Type activation and repair

The predecessor revision `20260828_people_et_bootstrap` and its historical
migration test are retained as migration history. Before this application
revision is activated against an existing database, operators must have run
the predecessor image's sealed dry-run/commit/replay sequence to a quiescent
fixed point and retained its non-secret evidence. That predecessor service and
CLI are absent from the activated application, so no legacy-to-module decision
path survives the switch.

`20260828_people_et_activation` descends from that gate through
`20260828_route_permissions`, the revision that landed on `main` from the same
predecessor while this branch was open. The ERP lineage therefore keeps exactly
one head, which is the head `deploy/product.toml`'s `expected_heads` names. It
drops the bootstrap lock function and grants `app_user` only schema `USAGE` plus table
`SELECT`, `INSERT`, and `UPDATE` for the compatibility projector. `DELETE`,
`TRUNCATE`, table/column `REFERENCES`, and `TRIGGER` are explicitly revoked.
The same migration idempotently materializes the existing ERP-owned
`hr:employment_types:read` and `hr:employment_types:manage` permission
contract and the exact seed-profile grants: `admin` and `hr_director` receive
both, while `hr_manager` and `hr_officer` receive read only. It preserves
pre-existing active role/permission rows and operator descriptions, and
refuses inactive definitions rather than silently reactivating them. The
optional full RBAC seed is therefore not a deployment prerequisite, and this
does not move RBAC ownership into the People module.
The migration is forward-fix only: downgrading application authority while
leaving module state live would restore split ownership. It raises before any
downgrade SQL, because adopted permission/grant rows cannot be distinguished
from operator state safely.

The activation revision also enforces the cutover premise itself. It refuses
unless the migration process explicitly carries
`PEOPLE_EMPLOYMENT_TYPE_ACTIVATION=1`, then takes `ACCESS EXCLUSIVE ... NOWAIT`
locks on both the legacy and module catalogues before comparing them. Those
fail-fast fences exclude legacy DML, module DML, and the reverse bootstrap's
own source lock for the entire parity proof and authority switch. Legacy-only
rows, module-only rows, or any tenant/ID/name/active-state/timestamp mismatch
abort the migration before the fence, grants, or RBAC state change. Exact
both-empty catalogues are a valid fixed point for a clean installation or an
organization that legitimately has no Employment Types.

Before releasing those locks, activation installs
`hr.enforce_employment_type_projection()`. Its trigger accepts an INSERT or
UPDATE to `hr.employment_type` only when every decision field and timestamp
already matches the `mod_people.employment_types` row with the same ID. The
module-first same-transaction projector and repair path therefore pass, while
an accidentally resumed old image cannot make a legacy-only decision even
though the compatibility projector still needs INSERT/UPDATE privileges.

For an existing deployment, that opt-in is installed only by
`scripts/deploy.sh --people-employment-type-activation`. The mode refuses
`--quick`, stops app, worker, and Beat before DDL, and holds the database proof
and authority switch in the migration transaction. A migration failure may
restore the drained previous image only when a fresh read of the CURRENT
module-owned surface says so: the module-owned authority relation
`mod_people.employment_types` must be visible (the probe's positive control,
without which an absent revision proves nothing), and both the activation
revision and the activation's own `hr.enforce_employment_type_projection()`
fence must be absent. Those two share one transaction, so they cannot disagree
unless the state is torn. The probe never reads the retired bootstrap fence,
which this revision removes. A non-zero Alembic container exit alone is
ambiguous and defaults to the safe direction. Once
activation commits, the script becomes forward-fix-only: an app health, static
sync, worker-ping, Beat-heartbeat, or later deploy failure never restarts the
previous image's legacy writers. The deploy also refuses a running `app-dev`
and verifies no known Compose one-off remains after the drain. CI uses the same
opt-in only on disposable databases where no previous image is serving.

`scripts/repair_people_employment_types.py` is the only operator repair path.
For each canonically primed tenant Session it performs complete module and
legacy scans, refuses the full set of legacy-only IDs before its first write,
projects only missing or changed rows, then verifies exact IDs and fields. It
never deletes and reaches zero writes at a fixed point. `--dry-run` executes
the same checks and rolls back; ordinary execution commits in the CLI adapter.
Normal reads never invoke repair.

## Field boundary

The projection is shaped to the released kernel and `dotmac-people` storage
contracts:

- ERP `Person` becomes kernel `Party(type=person)` + `PartyPerson`.
- Department, designation and employment-type codes/names map directly.
- Employee carries only party identity, directory references, employment
  dates and employment lifecycle status.
- Position carries its hierarchy, directory references, routing policy and
  active state.
- Position assignment carries employee, position, type and date interval.

ERP `Department.head_id` is not exported as a second head authority. The
projection derives `Position.is_department_head=true` only for the department's
position occupied by its named head through an active primary assignment on
the request date. ERP `Employee.reports_to_id` and cached `Position.is_vacant`
are not exported; hierarchy and vacancy are derived from positions and
assignments in the target.

Bank details, compensation, payroll state, cost centres, locations, shifts,
credentials, roles, permissions, Sub account metadata and all other wide ERP
employee fields are outside this contract.

## Dependency evidence

`docs/inventories/people-dependent-references.tsv` is the canonical static
**model-intent** ledger. One row is one unique intended-FK identity
`(source_schema, source_table, source_column, target_schema, target_table,
target_column)`. Repeated model declarations collapse into that identity;
`declaration_kind` and `declaration_paths` preserve whether it was explicit or
expanded from a reusable mixin and where it came from. Consequently, a count
of source declarations and a count of ledger rows answer different questions
and must never be presented as the same number. A model-intent row is evidence
that checked-in ORM code declares a dependency; it is not evidence that a
migration installed the corresponding PostgreSQL constraint.

`tests/integration/people_hub_fk_catalog.tsv` is the separately checked-in
baseline of constraints observed in the fully migrated PostgreSQL catalogue.
Each row records the six-column identity plus update/delete actions, match type
and deferrability. That is the migrated FK contract truth for this boundary.
`tests/integration/test_people_hub_fk_catalog.py` queries PostgreSQL and compares
it with the physical baseline without inferring the expected answer from
whatever ORM models happen to import during the test.

The current differences between model intent and migrated constraints are
explicit **model-only FK drift** and **physical-only FK drift**. They are
control debt under separate baselines in one two-directional ratchet, not an
assertion that either evidence source is a subset of the other. A change to
model intent must update the static ledger deliberately; a change to migrated
constraints must update the physical baseline deliberately; and the drift
ratchet prevents either gap changing silently in either direction. The tests
own all numeric baselines. Neither evidence source nor the debt baselines may
be replaced by copied prose counts in this document or the BOM.

## Writer retirement evidence

`docs/inventories/people-authority-writers.tsv` is the exact current census of
ERP mutations to projected source fields. Runtime writers are marked
`retire_with_domain_cutover`; operator/seed scripts are marked
`disable_before_cutover`. The architecture test scans application code,
tasks/workers, scripts and tools and compares in both directions: a new writer
fails, and a removed writer also fails until the ledger is lowered in the same
change. Its planted cases prove constructor, ORM assignment, `setattr`,
session add/delete, SQLAlchemy DML and raw SQL detection; a read-only negative
case prevents model references from being classified as writes.

The six Employment Type decision-writer rows were retired together:
`OrganizationService` create/update/delete,
`EmploymentTypeImporter.create_entity`, and the two payroll seed helpers.
The remaining derived writer is not hidden in that legacy-authority count: the
ratchet partitions the exact path, symbol, entity and evidence for
`_EmploymentTypeProjector.project`, and its sensitivity canaries fail on both
a missing projector and any peer writer.

## Cutover gates

Authority may move only when all of these are evidenced in checked-in cutover
artifacts and the irreversible switch is explicitly authorized:

1. The composed product pins the released kernel and `dotmac-people`
   distributions and migrates their lineages successfully. **Satisfied in
   repository behavior for the Employment Type slice** by
   `dotmac-people==0.1.0a2`; deployment evidence remains separate.
2. Backfill preserves source IDs and records deterministic source and target
   fingerprint-set evidence. **Implemented for Employment Type only; execution
   evidence is still required per organization.**
3. A successor projection with an explicit effective date removes v1's
   process-date-dependent fingerprint, and the target pins that date for each
   complete reconciliation pass.
4. Shadow reads and a repeatable reconciler show no unexplained row or field
   differences for every entity and tenant.
5. Every runtime row in the writer ledger is replaced by a composed-product
   command path or explicitly retired; every operator-script row is disabled.
   **Satisfied for Employment Type only.**
6. The composed product becomes the sole writer in one cutover. **Satisfied in
   code for Employment Type only; production activation is not claimed.** Its
   retained `hr.employment_type` relation is a local, rebuildable derived
   projection written only by the synchronous projector and explicit repair.
   A future `hr.employee` compatibility surface must likewise have one module-
   to-compatibility writer and no reverse feed.
   The checked-in deployment path enforces a drained, parity-gated,
   forward-fix-only switch; an operator must still explicitly invoke that mode
   for the target environment.
7. ERP tables remain until downstream foreign-key consumers have moved; table
   deletion is a later, separately authorized production operation.
