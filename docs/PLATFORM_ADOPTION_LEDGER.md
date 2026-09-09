# Platform Adoption Ledger — dotmac_erp

**Status:** UI consumption and three ERP-hosted kernel prerequisites are
composed; runtime idempotency cutover remains blocked by the non-superuser role
gate and the per-operation retirement plan. Supersedes the
Phase-0 draft (recon pin 318a6e0d, surveyed 2026-07-19), which predated this
repo's checked-in SOT map, the executable SOT registry, and the released kernel.
This ledger records boundaries and evidence; it does not authorize a production
deployment.

**Evidence pins:**

- This composition slice starts from `dotmac_erp` `origin/main` at
  `325e422a3ef22634c513b50c2820b74d37e09491`; its final evidence is the
  reviewed commit and CI result, not this prose.
- Current declared pin: `dotmac-kernel==0.1.0a98` (source of record:
  `dotmac_starter_mt/packages/dotmac-kernel`, import name `dotmac_kernel`).
  The lock must resolve this exact published artifact; dependency, installed
  wheel, prerequisite vocabulary and verifier are checked dynamically.
- UI slice baseline: `dotmac_erp` `origin/main` at
  `c5f933d9be758c1ae70167cc030326877d9b27f7`; exact target
  `dotmac-ui==0.1.0a7`, UI contract `1` (source of record:
  `dotmac_starter_mt/packages/dotmac-ui`, import name `dotmac_ui`).
- E1's historical inventory baseline remains ERP commit `96928fa1`; sections
  that cite that hash describe that earlier measurement, not the E8 baseline.

## Imports I1 — first durable customer adopter

ERP composes `dotmac-imports==0.1.0a2` as the first real consumer of its
tenant-scoped `im` lineage. The module owns the run, immutable partition plan,
atomic claim/checkpoint lifecycle and minimised row outcomes. ERP remains the
sole owner of customer vocabulary, validation, duplicate policy and mutation;
`app.services.finance.import_export.durable_customers` delegates accepted rows
to `customer_service.create_customer` and never commits, rolls back or creates
a session.

Source and derived partition bytes are held by the already composed
`dotmac-files` contract through ERP's one MinIO adapter. Upload admission is
streamed into a spooled file under a configured hard ceiling; partition size,
row count and validation-worker count are deployment settings. Workers use
three phases: commit an atomic claim, read and verify the authorized stored
object with no session, then settle domain effects and outcomes in one new
tenant transaction. Apply stays serialized until customer legal-name
uniqueness is enforced by the database; dry-run claims may run concurrently.

This is a shadow slice, not immediate retirement. Every durable validation row
is compared with the retiring `CustomerImporter` verdict first and a mismatch
refuses settlement. The legacy endpoint stays available until CI/PostgreSQL
evidence and real shadow comparisons are clean, after which its decoding,
mapping and run-loop ownership is removed and the two-directional caller
ratchet is lowered in the same change. The exact contract is
`docs/architecture/imports-adoption-boundary.md`.

## Tax C2 — released contract and storage composed, authority unchanged

ERP pins `dotmac-tax==0.1.0a3` from the private index and composes the module's
tenant-only `tx` lineage at `tx_0003_result_fingerprint`. The external release
oracle is Starter run `32898397980`; annotated tag
`dotmac-tax-v0.1.0a3` peels to
`531f7f8c37ce2fdf41ecbf2f9a7a9940264a18f9`, and generated release-record PR
#443 merged at `fca290ac7a32755e9ab000661e8bc6a35c138173` with post-merge CI and
Engineering Standards green.

C2 deletes ERP's temporary determination-result mirror and consumes the
released public `TaxDeterminationSetV1`/component/line contract directly. ERP
retains only its typed source observation and accounting-application context,
including explicit posting target, account consequence and functional-currency
identity. The exact submitted source fact owns document, line, correlation and
reversal identity; a result cannot be rebound through the application context.
Source and result fingerprints stay distinct. Reportable zero outcomes remain
reachable without creating a zero journal, and an ERP consequence that
contradicts the module transaction side is refused. Foreign-currency posting is
structurally sealed rather than admitted with a free exchange-rate scalar.

This is supply and storage, not cutover. `TAX_COMPOSITION_ENABLED` defaults to
false; no calculator, source writer, reader or filing path calls the module.
C3 must backfill policy/classification/account mappings and compare every field
and proposed balanced consequence before any cohort moves. A foreign-source-
currency cohort remains `ADJUDICATION_REQUIRED` until Finance/Tax approves the
legal tax-base currency and rate evidence; an invoice-header rate is not
silently treated as that policy.

## E8 slice 3 — Organization identity is module tenant identity

The measured RLS audit/ratchet (E8 slices 1 and 2) is followed by the first
runtime compatibility slice. `core_org.organization` remains the tenancy
authority, and `app.tenancy.OrganizationTenantContext` maps it to shared-module
tenant scope without a second id or mapping-table writer:

```text
tenant_id = organization_id
```

ERP retains its existing engine, `SessionLocal`, transaction boundaries, ORM
listener and `app.current_organization_id` policies. Canonical session primers
now set `app.current_organization_id` and `app.current_tenant` atomically and
re-arm both after every transaction boundary. The latter is the scope consumed
by stateful shared-module RLS. Runtime code has no PostgreSQL bypass writer;
that removal is fail-closed against ERP's migrated policy set, not a no-op.
Cross-organization module work iterates Organizations under separate
`session_for_org` sessions, and the future `app_user` cutover remains blocked
until every residual database-wide caller has a narrow reviewed contract.

That caller disposition is exact and checked in at
`docs/inventories/rls-cross-org-callers.tsv`: one row per runtime
cross-organization caller, carrying its mechanism, the schema-qualified
relations it reaches, its reviewed contract, and whether it is ready for
ordinary `app_user` or still blocked.

The counts are deliberately NOT repeated here. Every converted slice moves
them, and a number copied into prose is stale by the next merge — this
paragraph carried "198 owning functions / ninety ready / 108 blocked / 71 jobs
and scripts" for four slices after all four had stopped being true. The TSV is
the count of record; `tests/architecture/test_cross_org_caller_dispositions.py`
is the two-directional ratchet and sensitivity proof that keeps it exact.

The complete contract and remaining gate are
`docs/architecture/organization-tenant-boundary.md`. This slice creates no
`tenants` table, runs no kernel or module lineage, admits no kernel persistence
import, and does not make `dotmac-files` a consumer. Kernel revision 0001's
atomic identity/RBAC/audit collisions and the truthful Organization→Tenant
projection remain the next lineage gate.

## E8 slice 4 — Tenant catalogue is an Organization projection

ERP migration `20260813_tenant_projection` now hosts kernel-compatible
`public.tenants` and `public.tenant_domains` tables in ERP's own lineage. Each
Organization projects to a Tenant with the same UUID through the single runtime
writer `app.services.tenant_projection`; create/update/retirement and projection
mutation share the caller's ERP transaction. The only newly admitted persisted
kernel symbol is `dotmac_kernel.models.Tenant` in that exact file. The
symbol-level import guard continues to reject every Party, identity, RBAC and
session model.

The web settings paths, `create_org.py`, E2E organization seeding and generated
instance bootstrap all call the same writer before their transaction commits.
An exact source inventory test admits only those paths, the machine-verified
shadowed legacy facade and one explicitly archived rename script.

The migration fails closed on incompatible catalogues or data, inserts only
missing truthful rows and hosts the exact `app_current_tenant_id()` RLS helper.
It does **not** compose or stamp the kernel lineage and does not adopt
`dotmac-files`. The complete unresolved 0001 matrix is
`docs/architecture/kernel-0001-dispositions.md`.

Read-only Seabone production preflight on 2026-08-13 found one Organization,
zero invalid/oversized projected names, no tenant catalogue/function and none
of kernel 0001's three database roles. No production write or migration ran.

## E8 slice 5 — Kernel lineage failure is executable

ERP now reuses Sub's installed-lineage PostgreSQL rehearsal instead of treating
the revision-0001 inventory as sufficient evidence. The canary creates a
disposable database, runs ERP's real Alembic chain, and attempts the installed
Kernel lineage in its own `public.dotmac_kernel_alembic_version` table. It
rehearses both fresh installation and the real predecessor-to-head upgrade.

At the pinned `dotmac-kernel==0.1.0a24`, both paths fail exactly at
`0001_initial_tenant_schema` on `public.tenants`. The failed transaction leaves
no Kernel version row and no net creation of `app_admin`, `app_user` or
`platform_api`. That exact failure is a forward-only ratchet: every later
disposition must move it deliberately, and an earlier or different failure is
a regression.

This slice composes no external lineage and does not make `dotmac-files`
admissible. Revision 0001 is still atomic, and its incompatible ERP identity,
RBAC and audit tables plus RLS/grant effects remain blocked by
`docs/architecture/kernel-0001-dispositions.md`.

## UI slice U1 — packaged empty-state consumer

`dotmac-ui` owns the fleet presentation contract; ERP owns its FastAPI/Jinja
composition and its temporary compatibility adapter. ERP pins the released
`0.1.0a7` artifact exactly and consumes only published paths:

- `app/ui.py` resolves the installed static and template directories;
- `app/main.py` mounts package assets ahead of ERP's broad `/static` mount;
- `app/templates.py` layers the namespaced package directory into ERP's one
  shared Jinja environment; and
- browser document shells link the package stylesheet after ERP product CSS.

The former ERP **macro implementation** is retired. The macro at
`templates/components/macros.html` preserves the legacy positional and keyword
call shape, maps `description` and the CTA aliases onto the published
`title/message/action_label/action_url` contract, and delegates all emitted
markup to `dotmac_ui/components/empty_state.html`. Legacy `icon` and
`illustration` arguments remain accepted only during caller migration; they no
longer select product SVG or `/static/img/illustrations` paths. This is one
presentation owner for those callers, not a second renderer. At the slice
baseline the adapter cuts over 351 macro invocations across 234 ERP templates;
this is broad checked-in product adoption, not an isolated demonstration
template.

This is not yet fleet-wide retirement of ERP's old `.empty-state*` CSS. A
separate measured set of 25 hand-written surfaces across 22 legacy templates
still emits those classes, so deleting the styles here would break live product
markup. Those screens must move through focused caller slices; they are not
counted as consumers of the packaged component until they do. Both class
families remain in E2E empty-state detection during that migration.
The architecture guard freezes both the 25-surface and 22-file counts in both
directions, with a sensitivity proof, so new inline markup cannot hide inside
the retirement backlog and removals must lower the recorded baseline.

`tests/architecture/test_dotmac_ui_adoption.py` pins the exact release,
component signature, real shared-loader resolution, static mount ordering,
browser stylesheet composition and no-vendoring boundary. The existing macro
tests prove legacy calls reach the published `.dmui-empty-state*` markup.

This is ERP's first independent component consumption evidence. It does not by
itself promote the component slice to reuse-proven; that requires the planned
independent Sub cutover on an exact released pin. It changes no accounting,
tenancy, permissions, settings, database schema or migration lineage.

## Adoption slices

**2026-08-28 — Deployment release identity and reference adapter.** ERP declares one
release-time `ProductAssemblySpec` for the canonical `dotmac-erp` product over
the six module manifests already
present in `COMPOSED_MODULE_LINEAGES`: Accounting, Files, Imports, Numbering,
People and Tax. Its canonical `deploy/product-manifest.json` preserves exact module
codes, distribution versions and explicit/effective persistence planes; it
does not use the capability-only `ProductManifestSnapshot` as a substitute.

The Docker context excludes `deploy/`, breaking the product-manifest/image
digest self-reference. CI builds and tests one image, then transfers that exact
image between jobs; the publication job is structurally forbidden from
rebuilding it. Only after every gate in the CI workflow succeeds — including an
exact pinned re-run of the independently required Engineering standards check
— does the registry push return an immutable image digest; CI uploads a
non-secret `image-release.json` binding that exact reference to `GITHUB_SHA`.
ERP exact-pins the published `dotmac-deployment-foundation==0.2.0a2` build-time
facility released from commit
`55750e104df3dd94b6f9f70bf8c8db53986394c7`; its reusable conformance workflow
is pinned to that SAME immutable commit, because a2's `image/audit.py` change
and that revision's workflow change are two halves of one non-root
image-inspection fix and are not independently pinnable. `deploy/product.toml`
uses the same `dotmac-erp` product identity and binds the real canonical
manifest digest. Its four deterministic rendered assets are checked
byte-for-byte and parsed by a real Compose engine.

The image digest is no longer a sentinel. Protected-main CI published the
tested image for `9b3fb250ac9b0a8ed47cf60060d0eae737f0d4fd`, and the descriptor
binds the digest that run resolved for it,
`sha256:d33c172a6d93449e4815f04182f79fbf517e955f8efa1d61bd2a74f19bc9586c`, whose
`org.opencontainers.image.revision` annotation equals that same revision. With
the sentinel gone, `check_all` returns zero findings and the architecture
test's `KNOWN_UNRESOLVED` ratchet is empty.

This is a checked reference adapter, not host execution: no live deploy script,
runtime factory, module authority, data or production environment moves in this
slice. The detailed contracts are
`docs/architecture/deployment-foundation-prerequisites.md` and
`deploy/README.md`.

**2026-08-28 — Audited ERP runtime-image preparation.** The protected-main
candidate is built as a numeric-non-root runtime image with Poetry and Node
confined to builders. CI runs the web process, worker, Beat, role preflight and
migrations from the same local image under a read-only/capability-dropped
envelope, then the exact-pinned Deployment Foundation `0.2.0a2` collector
audits those same bytes before H3 export. The live Compose/deployer changes in
this slice are compatibility-only: they retire the boot-time installer and use
direct runtime executables now that Poetry is absent from the image.

This is not the host cutover. Production read-only settings, digest-only H3
evidence consumption, mutable-overlay retirement, signed-license material and
resolved-Compose execution evidence remain one subsequent deployment-adapter
slice. Keeping that boundary explicit prevents a CI-only hardening envelope
from being reported as live runtime truth.

**2026-08-28 — People Employment Type authority slice.** ERP pins
`dotmac-people==0.1.0a2` and composes its independent
`people` lineage at
`pe_0001_people_directory`. The six manifest-declared tenant tables are
created in `mod_people` with non-null tenant scope and ENABLEd/FORCEd RLS. ERP
supplies and re-verifies `tenant_scope_catalog.v1`,
`module_database_roles.v1` and `party_person_catalog.v1` from its own lineage;
it does not run or stamp the kernel lineage.

One ERP assembly owner imports the reviewed public a2 runtime surface. Web,
API, HR, Payroll, projection and operator callers delegate to it, and all six
legacy Employment Type decision writers are retired. Every module command
synchronously invokes one private compatibility projector in the same Session;
that exact-one writer is held by a two-directional architecture ratchet because
retained Employee and Payroll foreign keys still require the legacy UUID rows.

The activation migration removes the predecessor's reverse-source lock helper,
grants the online role only read/insert/update projection privileges, and
explicitly revokes destructive and table/column relationship privileges. It
also makes deployment independent of the optional full RBAC seed by
idempotently materializing the existing ERP permission contract: `admin` and
`hr_director` receive Employment Type read/manage, while `hr_manager` and
`hr_officer` receive read only. Existing active definitions and operator text
are preserved; inactive definitions fail the migration. RBAC remains an ERP
owner outside the People module. The predecessor bootstrap service and CLI are
removed. The remaining operator CLI repairs module-to-compatibility drift
only, refuses every legacy-only ID before writing, deletes nothing, and is
idempotent. This records repository authority; it does not claim a production
deployment or data cutover.

**2026-08-25 — Numbering composition: tenant storage only.** ERP pins
`dotmac-numbering==0.1.0a2` and composes its independent `numbering` lineage at
`nu_0001_numbering`. The assembly explicitly selects only
`ModulePlane.TENANT`; the four tenant tables are created in `mod_numbering`,
and the four platform tables are deliberately absent. ERP supplies and
re-verifies all three required effects from its own lineage:
`tenant_scope_catalog.v1`, `module_database_roles.v1` and
`idempotency_ledger.v1`. It still never composes or stamps the kernel lineage.

**This is storage, not numbering authority.** No module runtime service is
imported by `app/`, no series is configured or seeded, no historical receipt is
written, and no legacy allocator or caller is repointed. In particular,
`app/services/finance/banking/bank_statement.py` remains unchanged. Authority
moves only in a later series-family slice after explicit business-date,
historical reconciliation, shadow-divergence and runtime-role gates pass.

The release coordinate is registry release run `31908774569`; annotated tag
`dotmac-numbering-v0.1.0a2` peels to Starter commit
`4c282228b53405442effc4159f5db9d50446d335`. The prepared repository-local
evidence is the exact private-index pin declaration, the `alembic.ini` resource
location, the expected lineage head, the tenant-only plane declaration, the
composed migration gate and the disposable-PostgreSQL rehearsal. Composition
uses the matching `poetry.lock` generated by the repository's pinned Poetry
2.4.1 toolchain against the private release; its only package delta is the
`dotmac-numbering 0.1.0a2` entry, plus the derived content hash. The dedicated
lock assertion, `poetry check --lock`, and the full disposable-PostgreSQL
composition rehearsal pass. Registry authentication used the least-privilege
read identity through the approved ephemeral credential lane; no credential is
stored in the repository or Poetry configuration.

**2026-08-24 — Clean-instance composition direction.** ADR-0003 supersedes the
planned replay of 190,179 legacy posted journals. ERP will finish module
composition against fresh databases, import only reviewed masters, live open
items, continuity identities and a Finance-approved opening accounting state,
then retain the legacy ERP as the read-only owner of pre-cutover history. Known
legacy accounting defects remain evidence in that archive; they no longer block
software composition and are never copied into `mod_accounting`.

This changes Gate D, not the as-built Gate C claim. Accounting remains composed
and disabled, no runtime owner has moved, and production is untouched. The
legacy extractor loses its load seam and remains a read-only forensic tool. The
new bootstrap must use module public contracts, stable source identities,
content fingerprints and exact control evidence, and must be reproducible on
two independently migrated clean databases before any caller is repointed.

**2026-08-21 — Accounting gate C: composed and disabled.** ERP pins
`dotmac-accounting==0.1.0a1` exactly and composes `mod_accounting` into its
Alembic graph. Kernel repinned `0.1.0a83` → `0.1.0a85` — **demanded by the
module, not bundled with it**: `dotmac-accounting 0.1.0a1` floors at
`dotmac-kernel >= 0.1.0a85`, so the lock cannot resolve at a83. `dotmac-files`
needs no repin and stays at `0.1.0a2`. The regenerated lock adds exactly one
package and moves exactly one version; there is no transitive churn.

**Gate B is complete**, settled by the annotated Starter tag
`dotmac-accounting-v0.1.0a1`, which peels to commit `20d24703`. Gate A's
`test_the_distribution_is_not_pinned` was an ABSENCE guard, never a tag oracle;
gate C replaced it and the three other absence assertions with their positive
counterparts — exact pin, installed version matches, lineage resolves,
`require_composition_ready()` now refuses for "not enabled" rather than "not
installed". The one gate-A assertion that survives unchanged is that nothing
under `app/` imports the package.

**This is storage, not authority.** `ACCOUNTING_COMPOSITION_ENABLED` stays
false, no ERP writer is repointed, no backfill has run, and no decision has
moved — the same rule ERP already applies to `idempotency_ledger.v1`, whose
tables have existed since `20260820_idempotency_ledger` while its operations
remain uncut-over.

Proven against PostgreSQL on a disposable database built the way a deploy builds
one (ERP's lineage to head, then `alembic upgrade heads` with the modules
composed): `ac_0001_accounting` applies and is stamped; `require_prerequisites`
passes against the live catalog for all three effects, with a sensitivity proof;
every declared `mod_accounting` table exists with a non-nullable `tenant_id` and
FORCEd RLS; `upgrade heads` is repeatable; and the kernel's composed migration
gate reports nothing against either module lineage.

**Two head-related corrections, both of which a draft got wrong.** First, ERP
does not have one global Alembic head and must not expect one — each composed
module lineage is an independent root with its own branch label, which is why
the deploy path has always been `alembic upgrade heads`, plural. Second, the
adoption run showed `alembic_version` held only TWO rows
(`ac_0001_accounting`, `fi_0001_stored_files`): the prerequisite provider
`20260820_idempotency_ledger` was subsumed because both modules carry a
`depends_on` edge onto it, and Alembic treats a depended-upon revision as an
ancestor. Later ERP revisions remain separately stamped effective heads, so
the current composed graph has 3 graph heads and 3 stamped rows. The expectation
is derived from the script directory, and a separate test distinguishes
"provider subsumed" from "provider never ran" — indistinguishable in the
version table alone. `app/migration_bindings.py` now declares
`COMPOSED_MODULE_LINEAGES` as the checked expectation.

The composed gate must also see the WHOLE composition: scoped to the module's
own directory it correctly reports each bound prerequisite as naming "a revision
this deployment never runs". Given every location, what remains is ERP's legacy
history, so the assertion is scoped by the gate's own attribution, and ERP's
long revision ids rest on an enforceable premise checked against the live
catalog — `extend_alembic_version` widened `alembic_version.version_num`, and if
anyone narrows it the build says so.

**2026-08-21 — Accounting readiness (gate A). No pin.** ERP prepares the five
artifacts a sealed `dotmac-accounting` cutover needs, and pins nothing: the
module has no release tag, and
`tests/architecture/test_accounting_composition_disabled.py` enforces its
absence four independent ways (no dependency, no import under `app/`, no
`version_locations` entry, `ACCOUNTING_COMPOSITION_ENABLED` defaulting off).

What landed, and what each is for:

- **The map.** `docs/inventories/accounting-gl-writers.tsv` (74 rows) and
  `docs/inventories/accounting-gl-callers.tsv` (168 rows), each an exact
  two-directional ratchet with a per-shape sensitivity proof
  (`tests/architecture/test_accounting_gl_boundary.py`). Every row carries both a
  disposition and a `final_state`, and each pair carries an invariant checked
  against the code rather than the label — a `keep_local` writer must touch only
  retained relations, a `tool_repointed` entry point must be Python, a
  `gl_internal` caller must really live in the GL owner. Writer final states:
  36 `writer_removed`, 20 `tool_repointed`, 12 `tool_archived`, 6
  `retained_erp_writer`. Caller final states: 106 `caller_repointed`, 58
  `retired_with_gl_owner`, 4 `retained_erp_caller`. The 106 repointed callers
  are the real size of this cutover.
- **The disabled composition, proven inert.** `app/accounting_adoption.py`
  states the composition as data — names, version location, required effects, and
  a THREE-way partition of every GL relation: seven migrate, four are retained
  ERP writers (`gl.account_balance`, `gl.balance_refresh_queue`, `gl.budget`,
  `gl.budget_line`), and `gl.posting_batch` ends with its writer, having neither
  a module table nor a surviving ERP writer once the poster is sealed. The
  two-way version of that partition mis-filed
  `LedgerPostingService._retire_superseded_batch_key` as `keep_local`; the
  writer ledger's invariant is what caught it. It imports nothing from the
  module, and `require_composition_ready()` refuses rather than degrading,
  distinguishing "not installed" from "not enabled".
  `tests/architecture/test_accounting_scaffold_is_inert.py` proves the scaffold
  changes no deployment: booting `app.main` imports none of it, it registers no
  route/task/beat entry/ORM table, it reads exactly one environment variable,
  and importing it with every outbound socket poisoned reaches no network. Both
  probes carry sensitivity proofs, since both pass by finding nothing.
- **Migrations: prerequisites bound and resolving; nothing further proven.** The
  module declares `tenant_scope_catalog.v1`, `module_database_roles.v1` and
  `idempotency_ledger.v1`; ERP binds all three to its own revisions, the last of
  them by PR #328, and resolution yields exactly those three and nothing
  beginning `0001_`. That is a check over declarations and bindings — the only
  one possible without the wheel. It does NOT prove the effects hold in a
  database (`require_prerequisites` verifies that against the live catalog at
  migration time, and has not run for Accounting), nor that `ac_0001` applies
  onto ERP's revision graph with a single head. The accurate claim is that no
  ERP migration is currently known to be outstanding; whether any is needed is a
  gate C question answered by running the lineage against a real non-production
  database.
- **The backfill.** `app/services/finance/gl/accounting_backfill.py` plus
  `scripts/backfill_accounting.py`. Masters row by row; transactions as a
  per-period work list carrying ERP's acceptance digest, so a divergence is
  attributable to a period rather than to "the backfill". Three shape changes
  are performed rather than copied — fixed dimension columns to a generic
  registry, a period status column to an event stream, and ERP's
  category-IFRS-class / account-type split onto the module's `account_class` /
  `kind`. Both classification tables are checked against the ERP enums they map,
  so a new enum member fails the build instead of the run.
- **The shadow comparison.**
  `app/services/finance/gl/accounting_shadow.py` — a pure comparator over exact
  `Decimal`s at three levels (control totals, per account, ordered line digest),
  because a missing document, a mis-mapped account and the same position reached
  by different entries fail differently and a single boolean hides which. Both
  sides produce the same `LedgerFact` values, so the comparison logic is
  unit-tested today and unchanged at cutover.

No writer moved, no authority moved, no production work was performed. The
ordered gates B–G are in `docs/architecture/accounting-adoption-boundary.md`;
each is a separate authorization.

## Pin history

**2026-08-20 — `0.1.0a56` → `0.1.0a83`.** This is the first DB-provider use
of the pinned kernel without composing its lineage. ERP revision
`20260820_idempotency_ledger` creates both planes required by
`idempotency_ledger.v1`, binds the effect to that ERP revision, and calls the
published verifier before the migration may succeed. The app still imports no
kernel idempotency model or execution service, and no legacy operation moves:
the exact caller ratchet and ADR-0001 keep runtime cutover separate.

The floor is a83 because it is the latest published kernel already validated
by Starter before this slice; Accounting's prepared package floor is a85 and
therefore remains uncomposable until a85 and Accounting a1 are separately
authorized and registry-verified. This pin supplies the prerequisite contract,
not authorization to publish either artifact.

**Historical E8 measurement — `0.1.0a24`.** The a24 dependency and lock were
already present at the E8 slice baseline; this slice does not claim to perform
that upgrade. That distribution had 17 kernel migration revisions and
E8's 0001 disposition test derives its table inventory from the installed
revision rather than copying an a13-era count.

**2026-08-07 — `0.1.0a8` → `0.1.0a13`.** The kernel release carrying the
white-label foundation: the module registry and manifest declarations, D1's
per-module Postgres namespaces and migration lineages, tenant-entitlement
enforcement (`require_capability`), typed feature flags, and the platform
administration surface. (a13 is a single release covering five development
iterations; a9–a12 were published, a14–a17 never existed as artifacts.)

The upgrade changed **nothing in ERP but the pin, the lock entry, and this
ledger.** ERP consumes the kernel as CONTRACTS, not as a runtime: it has its own
`require_permission` and `_write_audit_event`, and `test_app_import_loads_no_
kernel_module` pins that the booted app loads no kernel module at all. The two
a9–a13 changes that enforce at runtime — `write_audit_event` rejecting
undeclared audit actions, and `require_permission` failing the boot on an
undeclared code — are kernel-side guards on kernel-side call paths ERP does not
use.

Compatibility held on every assertion E2 pins: bare-string `capabilities=(...)`
still resolves (a13 retyped the field as `str | CapabilitySpec` and coerces
strings), `ProductAssemblySpec`'s new fields all default, the consume-pure list
is a SUBSET check so a13's added modules (`cache`, `flags`) do not disturb it,
and `import dotmac_kernel` stays DB-free. No transitive dependency moved.

**Not delivered by this bump:** ERP declares no `ModuleManifest`, so D1's
namespace and migration-lineage rules govern nothing here yet. They become
relevant when ERP extracts stateful modules — its schema-bearing tables are much
of why D1 exists — and that is adoption work with its own slice.

**E2 status (2026-08-02): landed at pin `0.1.0a8` — install unblocked.** The
first E2 attempt targeted `0.1.0a7` and was blocked: the a7 wheel's floors
(`python >=3.12,<3.14`, `fastapi ^0.115`, `pydantic[email] ^2.9`) excluded
ERP's pins (`python >=3.11,<3.13`, `fastapi 0.111.0`, `pydantic 2.7.4`), so
the pin existed but poetry resolution failed and the wheel canaries had to
skip. The `0.1.0a8` release (kernel CHANGELOG, "0.1.0a8 — 2026-08-02")
resolved every blocker:

- **Floors widened** to `fastapi>=0.111,<0.116`, `pydantic>=2.7.4,<3.0`,
  `pydantic-settings>=2.2,<3.0`, `python>=3.11` — ERP's pins resolve cleanly
  with **no python marker** and no ERP pin loosened. Lock delta: adds only
  `dotmac-kernel 0.1.0a8`, `pydantic-settings 2.14.2`,
  `typing-inspection 0.4.2`.
- **Both a7 release defects fixed** (CHANGELOG "Fixed"):
  `dotmac_kernel.testing` no longer builds the DB engine at import (the deps
  import moved inside `assembly_test_client`), and `dotmac_kernel.profiles`
  is now in `SUPPORTED_MODULES`. The old red-sensitive defect pins are
  replaced by assertions of the FIXED behavior in
  `tests/architecture/test_kernel_compatibility.py`.
- **Extras split** (`[testing]` → httpx only; cryptography only via
  `[licensing]`): ERP installs the kernel with **no extras** — httpx 0.27.0
  and `cryptography>=44.0.1` are already ERP main dependencies, so the whole
  testing kit (including `FakeLicenceSigner`) works regardless.

Every E2 canary now RUNS — the skip machinery of the blocked attempt is
deleted; a missing or wrong-version kernel is a hard failure.

**E4 status (2026-08-02): exact Money/FX boundary adapter landed.** First
slice in which `app/` imports the kernel (`dotmac_kernel.money`, consume-pure
per the classification table below; the E1 import-boundary guard passes
unchanged). What shipped:

- **Adapter (one module):** `app/services/finance/money_boundary.py` —
  kernel `Money`/`Currency` ⇄ ERP `(Decimal, currency_code)` conversion
  (`to_boundary_money`/`from_money`), byte-exact connector serialization
  (`serialize_money`/`serialize_amount`), the slice's single centralized
  rounding decision (`BOUNDARY_ROUNDING` = half-up via
  `round_to_minor_units`), fail-closed validators (float/bool, missing or
  invalid currency, currency mismatch, excess minor-unit precision — a source
  FACT with more precision than the currency's minor units is rejected, never
  rounded), the WHT settlement identity (`check_settlement_identity`,
  net + WHT = gross, pre-existing one-minor-unit tolerance preserved), and
  FX ONLY from an immutable ERP-owned observation:
  `rate_snapshot_from_observation` builds a kernel `ExchangeRate` snapshot
  (pair, rate, effective time, `erp:core_fx:<source>[:<rate-type>]`,
  observation row id) from a persisted `core_fx.exchange_rate` row and
  refuses unpersisted/synthetic rows — `FXService` + `core_fx` remain the
  only FX owner; no posting path performs a live rate lookup.
- **Schemas converted (typed Money at the boundary):** the dotmac_sub
  connector records `InvoiceRecord`, `CreditNoteRecord`, `PaymentRecord`
  (`app/services/dotmac_sub/client.py`) each expose fail-closed
  `boundary_money()` (headers, WHT evidence, allocation amounts), enforced
  per-row inside the sync savepoints (`_invoices.py`, `_credit_notes.py`,
  `_payments.py` — a bad row fails ITS savepoint, never the run); the
  Sub payables command `SubPurchaseInvoicePayload`
  (`app/schemas/sync/sub_operational.py`) validates header totals and line
  amounts through the adapter at parse time. The `_CENTS`/`ROUND_HALF_UP`
  scattering in `_invoices.py` and the flat `0.01` WHT tolerance in
  `_payments.py` were replaced by the adapter's currency-aware equivalents
  (behavior-identical for 2-minor-unit currencies; all live data is NGN).
- **Deliberately still Decimal-internal (boundary 6):** `Numeric(20,6)`
  posting/functional amounts, `Numeric(20,10)` FX rates, tax ratios,
  quantities, unit prices (line `quantity`/`unit_price` and
  `tax_rate_percent`/`wht_rate` are rates, not money), posted-line
  snapshots, `_functional_amount`'s 6-decimal functional conversion, and
  account mappings. Line-level `amount` inputs remain part of the derived
  rounding path (they may legitimately arrive as quantity×unit_price
  products), now centralized through `round_to_minor_units`. Outbound
  Decimal fields (e.g. `SubPurchaseInvoiceStatusResponse`) already serialize
  as exact JSON strings under pydantic v2 (verified); re-quantizing that
  existing wire contract to minor units was deliberately NOT done.
- **No money at the material-support boundary:** `SubMaterialRequestPayload`
  carries quantities/serials only — valuation is ERP-internal at issue
  (moving-average cost). Nothing to convert; recorded here as the E4
  finding for that flow.
- **Float eliminations (finding 7 absorbed):**
  `app/models/fixed_assets/maintenance_work_order.py`
  `estimated_cost`/`actual_cost` are now `Mapped[Decimal]` (columns were
  already `Numeric(20,6)`; no schema change) and the two
  `float(updated_actual_cost)` writes in
  `app/services/people/assets/maintenance_service.py` are exact Decimal.
  The Sub-facing sync slice itself had no float money fields (verified:
  remaining floats there are HTTP timeouts/durations). `labor_hours` stays
  float (hours, not money).
- **Proof:** `tests/test_golden_money_pins.py` unchanged and green (row
  shapes identical); adapter unit tests in
  `tests/finance/test_money_boundary.py`; boundary/golden-serialization
  tests in `tests/services/test_dotmac_sub_money_boundary.py`. The E2
  app-unchanged canary evolved:
  `test_app_import_loads_only_pure_contract_kernel_modules` now snapshots
  the consume-pure import closure and asserts `app.main` loads nothing
  beyond it (kernel db/messaging/session surfaces still unimported).

**E4 review hardening (2026-08-02, PR #219 review):** the adapter's
fail-closed claims now hold at the real ingress, superseding two statements
above:

- **Strict wire parsing:** `DotmacSubClient._parse_invoice` /
  `_parse_payment` / `_parse_credit_note` no longer default malformed
  amounts to 0 or a missing currency to ERP's functional currency — money
  facts raise a typed `DotmacSubParseError` that fails the ONE source row
  (the feeds' `on_parse_error` collector keeps the pull alive; a failure
  parks the incremental watermark at the row, and an UNPOSITIONED failure —
  no usable `updated_at` — freezes the cursor for the run so the row can
  never be skipped past).
- **Admission on every pass:** `boundary_money()` runs BEFORE the
  unchanged-hash and status branches in all three sync mixins, so a legacy
  row with invalid money facts cannot ride the unchanged-skip forever.
- **Supplied line amounts are FACTS:** a Sub-supplied line `amount`
  validates through `to_boundary_money` (excess precision rejected, never
  rounded); only an ERP-derived quantity×unit_price (when Sub omits the
  amount) and derived tax splits round. The earlier "line-level amount
  inputs remain part of the derived rounding path" statement is obsolete.
- **Minor-unit authority:** `SUPPORTED_CURRENCIES` (an explicit
  `CurrencyRegistry`) ships **NGN and USD only**; `kernel_currency()`'s
  2-minor-unit default for unknown codes is unreachable. EUR/GBP arrive
  later only behind checked-in provisioning plus a database consistency
  test against `core_fx.currency`; zero/three-minor-unit contracts (JPY,
  BHD) stay covered via test-scoped registry instances.
- **Strings-only money wire rule (pending contract artifact):** external
  money crosses the wire as a canonical decimal STRING
  (`{"amount": "48375.00"}`); every JSON number token — int and float —
  plus booleans and non-finite values (NaN/±Infinity) is rejected at
  ingress (`mode="before"` validators on `SubPurchaseInvoicePayload`, the
  strict Sub parsers). Per Michael's directive this rule is slated to
  become a checked-in cross-repository contract document when the E4 (ERP)
  and S5 (Sub) slices land — tracked here until that contract exists.

**E4 round-4 hardening (2026-08-03, PR #219 review):** what the boundary
*admits* is now typed and immutable in depth, not merely frozen at the top
level:

- **Admitted evidence is immutable IN DEPTH.** A frozen dataclass with a
  `list` field is only shallowly immutable — the collection can still be
  appended to or reassigned element-wise. Every collection field on an
  admitted wire record is a TUPLE (`InvoiceRecord.lines`,
  `InvoiceRecord.allocations`, `PaymentRecord.allocations`,
  `CreditNoteRecord.lines`); no admitted record carries a `list`, `dict` or
  `set`. A "changed" payload stays a NEW record (`dataclasses.replace`).
- **Zero has exactly ONE canonical spelling and it is positive.** Every
  negative-zero form (`"-0"`, `"-0.00"`, `"-0.0000"`) is rejected by the
  canonical grammar at both ingress paths, and `serialize_amount` normalizes
  a signed-zero result so it can never emit one. `Decimal("-0.00") ==
  Decimal("0.00")` is True, so this is enforced on the SIGN / literal text,
  never on equality.
- **Timestamps are admitted as `datetime`, not wire text.** `updated_at`,
  `issued_at`, `due_at`, `paid_at`, `wht_resolved_at` and `created_at` parse
  to tz-aware UTC instants in the client (`_parse_wire_instant`), which is
  the ONE owner of the wire-text→instant decision (`BaseSyncMixin._parse_date`
  / `_parse_datetime` are gone). A malformed timestamp is a typed row
  rejection routed through the SAME collector as a malformed money fact:
  `updated_at` parses first, so a bad non-position timestamp is POSITIONED
  (parks the cursor) while a bad `updated_at` is UNPOSITIONED (freezes it) —
  the round-2/3 cursor semantics are unchanged. Consumers needing the wire
  text (change hashes, `updated_since`) format it explicitly with
  `.isoformat()`.
- **Closed status sets are enums; open ones are documented `str`.**
  `tax_application` is a typed `TaxApplication` (`exclusive`/`inclusive`/
  `exempt`) — ERP already failed closed on anything else, and typing it also
  closes the hole where a bogus application rode through when the line
  carried no `tax_rate_id`. Invoice/credit-note/payment `status`,
  `wht_status`, subscriber `status`/`category` and billing-account `status`
  stay `str` **by decision**, documented in each record's docstring: Sub owns
  and extends those vocabularies, ERP's mappings carry documented catch-alls,
  and an unknown member must be DATA, never a failed financial row.
- **The last two copy-pasted watermark loops are gone.** The reseller and
  subscriber mixins now use the same `WatermarkProgress` owner (and carry
  `on_parse_error` collectors, so typing their timestamps cannot let one bad
  row terminate a feed generator). `WatermarkProgress.parse_error_collector`
  no longer takes a `parse_datetime` hook — `DotmacSubParseError.updated_at`
  is already the parsed instant.

**Authority order (highest wins):**

1. `app/services/sot_relationships.py` — the executable SOT registry
   (guarded by `tests/architecture/test_sot_registry_liveness.py`). It wins
   for current owners.
2. `docs/SOT_RELATIONSHIP_MAP.md` (prose map), `docs/gl_source_of_truth.md`,
   and the checked-in cross-product contracts
   (`docs/dotmac_sub_material_support_contract.md`,
   `docs/dotmac_sub_tax_accounting_contract.md`, `docs/oidc_identity_contract.md`,
   `docs/replaceable_application_boundary.md`).
3. This ledger — discovery facts and adoption classifications only.

**Enforcement:** the kernel import boundary declared below is enforced by
`tests/architecture/test_kernel_import_boundary.py` (static AST scan; proven
red-sensitive by a synthetic-tree negative control in the same file).

## Reconciliation with the SOT map and executable registry

The Phase-0 ledger's finding 10 ("no erp-wide SOT relationship map or
architecture-test suite") is **closed**: both now exist and win over this
ledger. Deltas the rebaseline absorbs:

| Phase-0 statement | Current state at 96928fa1 |
|---|---|
| No SOT map; creating one is a finding | `docs/SOT_RELATIONSHIP_MAP.md` + `app/services/sot_relationships.py` exist; 9 domains, liveness-guarded |
| `tests/architecture/` holds one metrics test | Full governance suite: SOT registry liveness, OpenAPI contract surface pin, identity protocol boundary, replaceable application boundary, webhook org attribution, metrics scrape safety, version-impact gate |
| Material-support flow had a provider-named compatibility owner; #118 class remains repair-first | `inventory.material_support` (module `app.services.inventory.material_support`) is the registered ERP owner of the Sub material-support slice; `sync.sub_procurement` is demoted to an explicit compatibility engine, still repair-first (registry notes) |
| Webhook org attribution in shadow retirement | Registry/`tests/architecture/test_webhook_org_attribution.py` govern it; per-org `IntegrationConfig(DOTMAC_SUB)` bindings are the authority |
| No OpenAPI pin (dual-mount aliasing risk) | `/api/v1` surface pinned in `tests/architecture/openapi_contract_surface.json` |

Registry owners this ledger defers to (do not restate or fork them here):
`tenancy.context`/`tenancy.orm_filter`/`tenancy.rls`, `auth.*`, `control.*`,
`audit.business_log` (fragmentation honestly recorded), `gl.*`,
`platform.sequences`, `fx.rates`, `tax.policy`, `events.outbox`,
`events.hooks`, `licensing.enforcement`, `sync.dotmac_sub`,
`inventory.material_support`, `sync.sub_procurement`, `platform.storage`,
`platform.secrets`, `platform.notifications`.

Still-open Phase-0 findings carried forward unchanged (verified present at
96928fa1): audit four-writers/three-tables (finding 1 — E6 prerequisite),
global-not-org-scoped RBAC tables (finding 2), licensing placeholder key
(finding 3 — `app/licensing/validator.py:32` still ships
`REPLACE_WITH_REAL_PUBLIC_KEY_BASE64`; E9 target), DomainSetting direct
writers (finding 5 — E6 prerequisite), plural approval engines (finding 6),
float annotations on money columns in
`app/models/fixed_assets/maintenance_work_order.py` (finding 7 — verified at
the E1 pin: `estimated_cost`/`actual_cost` were `Mapped[float]`; **RESOLVED
in E4** — annotations are now `Mapped[Decimal]` and the `float(...)` writes in
`app/services/people/assets/maintenance_service.py` are exact Decimal; see the
E4 status above).

### Tenant-administration containment (2026-08-11)

The People JSON API is tenant-owned: its session is primed from the authenticated
organization, every service query includes `Person.organization_id`, and create
ownership is derived from authentication rather than request data. A caller gets
404 for a person belonging to another organization, including get, update, and
delete operations.

The RBAC tables remain global pending the explicit identity/RBAC ownership
decision in E8. Until that decision lands, the global RBAC API is a system-admin
surface: `rbac:manage` on a tenant token is insufficient, and every endpoint
requires the explicit `require_admin_bypass` principal. This is containment, not
closure of finding 2; the absence of an organization key on the RBAC tables is
still recorded debt.

The same containment applies to global authentication records and platform
settings: tenant permission claims such as `auth:manage` or `settings:manage`
cannot authorize an RLS-bypass session. Those JSON APIs require the explicit
system-admin principal. Because JWT role claims are login-time snapshots,
`require_admin_bypass` revalidates the caller's live, active `admin` assignment
on every request; a removed assignment stops authorizing cross-tenant access
without waiting for token expiry. Tenant-scoped settings history retains its
organization ownership checks.

Licensing development mode is opt-in. An omitted `DOTMAC_DEV_MODE` now enters
normal fail-closed enforcement, while tests and development environments set
the bypass explicitly. This closes the silent-default half of finding 3; the
placeholder verification key and the E9 owner cutover remain open and must not
be guessed in this containment change.

No settings migration that adds `tenant_id` beside `organization_id` is approved
by this ledger. Boundary 2 remains authoritative until an explicit ADR changes
it. Any later settings-only rename or shadow phase must update this ledger in the
same change and must include writer migration, drift detection, PostgreSQL
migration tests, and a rollback/retirement plan.

## Non-negotiable adoption boundaries (from the accepted plan)

1. ERP stays authoritative for accounting, inventory, procurement, workforce,
   tax, asset, and backoffice records; the kernel never posts, moves stock, or
   decides employment outcomes.
2. `Organization` remains the ERP tenancy key; `organization_id` is never
   renamed. Kernel `Tenant` is its same-UUID projection through the E8 adapter
   and single writer.
3. Dual-layer tenancy (ORM listener + PostgreSQL RLS) must never be weakened
   or partially initialized (evidence section below).
4. ERP identity/sessions/RBAC stay local; kernel Party/auth/RBAC is
   prohibited in this program. (ERP's unshipped OIDC adapter was deleted on
   2026-08-15 — see `docs/oidc_identity_contract.md`.)
5. The existing finance outbox is improved in place; kernel messaging tables
   are not introduced beside it before the E8 ADR.
6. Kernel `Money` is a boundary value only; ERP's six-decimal posting, FX,
   tax, and functional-currency internals are untouched.
7. ERP and Sub stay independent apps/databases (versioned APIs/events only).
8. Licence entitlement is separate from RBAC and from data integrity.

## Kernel public-module classification (0.1.0a8)

Classes: **consume-pure** (DB-free contract, importable once the pin lands in
E2) · **adapt-existing** (kernel contract adapted behind an existing ERP
owner, no kernel table) · **hosted-db** (ERP's own lineage truthfully supplies
a named kernel database effect while runtime callers remain separately gated) ·
**defer-db** (kernel persistence/session/migration surface still awaiting its
named ownership and composition decision) ·
**prohibited** (out of scope for this program; never imported under `app/`).

Only **consume-pure** modules and the exact persisted symbols named below are
in the architecture-test import allowlist today. `adapt-existing` and
`defer-db` modules join only in the slice that adopts them, in the same change
that updates this table.

| Kernel module | Class | Timing | Rationale / constraints |
|---|---|---|---|
| `dotmac_kernel.money` | consume-pure | Early (E4) | `Money`/`Currency`/immutable `ExchangeRate` values at API/event boundaries only; ERP `Numeric(20,6)`, FX `(20,10)`, tax ratios, functional currency stay internal (boundary 6) |
| `dotmac_kernel.capabilities` | consume-pure | Early (E7) | `CapabilityCatalogue`; one domain owner per capability code |
| `dotmac_kernel.features` | consume-pure | Early (E7) | `FeatureManifest` as declared metadata ONLY — `mount_features` is never called; ERP routers keep mounting via `app/main.py` |
| `dotmac_kernel.profiles` | consume-pure | Early (E7) | `DeploymentProfileSpec`/registry for release preflight; never branch business logic on profile strings |
| `dotmac_kernel.assembly` | consume-pure | Early (E7) | `ProductAssemblySpec` as metadata/release validation; does not replace ERP app startup |
| `dotmac_kernel.providers` | consume-pure | Early (E7) | Provider seam interfaces consumed by profile preflight only |
| `dotmac_kernel.planes` | consume-pure | Numbering composition (kernel `0.1.0a94`) | `ModulePlane` and `ModulePlaneSelection` are DB-free assembly declarations; ERP selects Numbering's tenant plane in `app/migration_planes.py`, while validation and Alembic consume that one declaration. Importing the module performs no database, ORM or network work |
| `dotmac_kernel.prerequisites` | consume-pure | E8 (kernel `0.1.0a83`) | Vocabulary + `PrerequisiteBinding` + `resolve_depends_on` for ADR-0006 D1's logical migration prerequisites. Pure — no I/O, no ORM, no web framework — so it is consume-pure rather than an exact-symbol adoption like `models.Tenant`. ERP declares its bindings in `app/migration_bindings.py` and installs them from `alembic/env.py`; all three effects are supplied by ERP's OWN revisions (`20260813_tenant_projection`, `20260814_database_roles`, `20260820_idempotency_ledger`), never by a kernel revision, because ERP hosts `public.tenants` itself and can never run kernel `0001` |
| `dotmac_kernel.testing` | consume-pure | Early (E2+) | Pure fakes/clock/licence kit for compatibility tests. The a7 wheel defect (eager `harness` → `deps` → `db` import made the subtree DB-bound) is FIXED in 0.1.0a8; DB-free import of the full subtree is asserted by `tests/architecture/test_kernel_compatibility.py::test_every_consume_pure_module_imports_without_db`. `FakeLicenceSigner` works without kernel extras because cryptography is an ERP main dependency |
| `dotmac_kernel.licensing` | consume-pure (types/verifier); cutover deferred-but-required | E9 (after E8) | Value types + `verify_licence` are DB-free and importable; enforcement cutover replaces the placeholder-key path (`app/licensing/validator.py:32`) through one explicit shadow-compare + cutover. No second enforcement owner meanwhile |
| `dotmac_kernel.messaging` (behavior: envelope/outcome semantics) | adapt-existing | Early (E3) | Target semantics for the existing ERP outbox (`events.outbox` owner): claim/deliver/settle, fail-closed unknown events, no service-internal commits. Semantics are matched, not imported wholesale |
| `dotmac_kernel.messaging` (storage/relay/worker: `messaging.models`, `relay`, `worker`, `platform_*`, `inbox`) | defer-db | After E8 ADR | Kernel `outbox_events`/`inbox_records`/`platform_*` tables would stand beside `platform.event_outbox` — a prohibited second outbox until the ADR decides migration/tenancy compatibility |
| `dotmac_kernel.idempotency` (+ `.idempotency_models`) | hosted-db; runtime import/cutover deferred | Storage composed 2026-08-20 under [ADR-0001](adr/0001-kernel-idempotency-is-erps-only-at-most-once-owner.md) | ERP revision `20260820_idempotency_ledger` creates BOTH kernel-shaped tables in ERP's own lineage, binds `idempotency_ledger.v1`, and proves the exact columns, unique keys, retention indexes and RLS plane posture through the pinned verifier. It never runs or stamps the kernel root. `platform.idempotency_record` remains transitional legacy state under `tests/architecture/idempotency_legacy_callers.txt`; no operation is cut over or dual-written by this storage slice. Runtime adoption requires a disjoint operation-scope/replay-window mapping and stays blocked while production connects as `postgres`, because an RLS proof on a superuser proves nothing. Accounting also remains disabled until its exact kernel/package pins are published and its own requiring migration re-verifies this effect |
| `dotmac_kernel.config` | adapt-existing | Mid-program (E6+) | Typed settings contract adapted behind ERP's canonical settings owner only after direct `DomainSetting` writers are removed. Env-name collision: both define `DATABASE_URL`; kernel additionally wants `PLATFORM_DATABASE_URL`. Kernel builds a module-level `settings` singleton on import |
| `dotmac_kernel.settings_resolver` | adapt-existing | Mid-program (E6+) | Spec registry / tenant→platform→default resolution adapted behind `control.settings`; ERP DB remains runtime-authoritative |
| `dotmac_kernel.settings_models` | defer-db | After E8 | Kernel `DomainSetting` table name `domain_settings` **collides exactly** with ERP `public.domain_settings` (different columns: `tenant_id` vs `organization_id`, no ERP history table on the kernel side) |
| `dotmac_kernel.settings_admin` | defer-db | After E6+E8 | Admin write path over kernel settings storage |
| `dotmac_kernel.audit` | defer-db | After E6 consolidation | `write_audit_event` targets table `audit_events` — **collides exactly** with ERP `public.audit_events` (`app/models/audit.py:26`). No kernel audit table beside ERP's four unconsolidated writers |
| `dotmac_kernel.entitlements` | defer-db | After E8 | `tenant_entitlement_grants` table; local grants only after Organization→Tenant mapping + module catalogue |
| `dotmac_kernel.db` | defer-db | After E8 | Import constructs TWO engines + `SessionLocal`/`PlatformSessionLocal` from env at import time and primes RLS via GUC `app.current_tenant` — a different GUC than ERP's `app.current_organization_id`. Importing it violates the no-second-session-factory exclusion and would half-initialize tenancy |
| `dotmac_kernel.migrations` | defer-db (full lineage); exact verifier adopted by provider revision | After E8 | ERP imports `migrations.verify.require_prerequisites` only from its idempotency provider revision, as a proof over ERP-owned DDL. The kernel revision lineage itself remains absent and permanently unstampable: composing it into ERP's graph would collide on `public.tenants` and assert unrelated identity/RBAC/audit effects |
| `dotmac_kernel.models` | partial persisted adoption: exact `Tenant` symbol only; identity/RBAC prohibited | E8 slice 4 | `app.services.tenant_projection` is the only admitted importer and writer. `TenantDomain` has a hosted table but no runtime import. `Party`/`Role`/`UserCredential`/`AuthSession` remain prohibited; Party never replaces `Person` in this program |
| `dotmac_kernel.cache` | partial typed adoption: exact `TenantScope` symbol only | Imports I1 | `app.tenancy` is the only admitted importer and constructor. It maps the authoritative Organization UUID to the shared-module scope; services ask that adapter for the scope and cannot independently choose tenant or platform scope |
| `dotmac_kernel.models_platform` | prohibited | — | Platform actor identity (`platform_admins`/`platform_sessions`/`platform_audit_events`); ERP has no platform-actor concept and identity stays local |
| `dotmac_kernel.security` | prohibited | — | Kernel credential hashing/token machinery; ERP `auth.flow` owner stays |
| `dotmac_kernel.deps` | prohibited | — | Kernel route guards query kernel identity models |
| `dotmac_kernel.web_deps` | prohibited | — | Kernel portal auth (cookie + admin role) — ERP web auth stays local |
| `dotmac_kernel.platform_auth` | prohibited | — | Platform-admin auth surface |
| `dotmac_kernel.middleware` | prohibited | — | `TenantResolverMiddleware` resolves tenant from Host and pairs with kernel `get_db`'s `app.current_tenant` GUC — semantically collides with ERP's dependency-based org priming; CSRF/rate-limit/security-headers/observability duplicate existing ERP middleware owners |
| `dotmac_kernel.app_factory` | prohibited | — | `create_app` mounts kernel features, platform auth, and `/static`; ERP owns its FastAPI factory, `/admin`, and `/static` |
| `dotmac_kernel.crud` | prohibited | — | Reference-assembly CRUD services are not ERP domain services (plan matrix: out of scope) |
| `dotmac_kernel.templating` | prohibited | — | Kernel Jinja environment/brand globals; ERP has its own template system and UI standard |
| `dotmac_kernel.branding` | prohibited | — | Reference-assembly web surface |
| `dotmac_kernel.identity` | prohibited | — | Party-identity helpers |
| `dotmac_kernel.display` | prohibited | — | Kernel per-request display-settings seam tied to kernel settings + web auth |
| `dotmac_kernel.query` | prohibited (default-deny) | — | No documented ERP need; joins the allowlist only via a slice that documents one |
| `dotmac_kernel.errors` / `dotmac_kernel.exceptions` / `dotmac_kernel.logging` | prohibited (default-deny) | — | ERP has its own error taxonomy (`app/errors.py`) and logging owner; module-specific exceptions (e.g. `CurrencyMismatchError`) arrive via their allowed module |

Bare `import dotmac_kernel` (the curated top-level re-export surface) is also
disallowed under `app/`: it aggregates audit/entitlements/config/identity names
across classes, defeating per-module review. Import the classified submodule.

## Collision inventory (kernel 0.1.0a7 vs ERP at 96928fa1)

### Python packages

- Kernel installs as distribution `dotmac-kernel`, import package
  `dotmac_kernel`. No collision with ERP's `app` package; ERP `src/` contains
  CSS only. No namespace shadowing.

### Models and tables (kernel tables are all schema-less → `public`)

| Kernel table | ERP status | Severity |
|---|---|---|
| `domain_settings` | **EXACT COLLISION** — ERP `public.domain_settings` (`app/models/domain_settings.py:77`) with different columns (`organization_id`, spec-typed values, plus `domain_setting_history`) | Blocker for kernel settings storage until E8 |
| `audit_events` | **EXACT COLLISION** — ERP `public.audit_events` (`app/models/audit.py:26`), different shape | Blocker for kernel audit table until E6+E8 |
| `roles` | **EXACT COLLISION** — ERP `public.roles` (`app/models/rbac.py:17`, global RBAC) | Kernel RBAC prohibited anyway |
| `user_credentials` | **EXACT COLLISION** — ERP `public.user_credentials` (`app/models/auth.py:47`) | Kernel identity prohibited anyway |
| `auth_sessions` | Near-miss — ERP uses `public.sessions` (`app/models/auth.py:190`); no name clash but same concern | Prohibited surface |
| `tenants`, `tenant_domains` | Hosted kernel-compatible catalogue. `tenants` is a same-UUID projection of authoritative `core_org.organization`; `tenant_domains` has no ERP writer yet | E8 slice 4; full kernel lineage still gated |
| `idempotency_records`, `platform_idempotency_records` | Hosted at the a83 contract by ERP revision `20260820_idempotency_ledger`; the legacy response cache remains separately named `platform.idempotency_record` | Storage provider adopted; runtime operation cutover blocked by ADR-0001 gates |
| `parties`, `party_persons`, `party_organizations`, `party_roles` | No name clash with `public.people` (`app/models/person.py:50`) | Prohibited surface |
| `outbox_events`, `inbox_records`, `platform_outbox_events`, `platform_inbox_records` | No name clash — ERP outbox is `platform.event_outbox` (`app/models/finance/platform/event_outbox.py:42`, schema `platform`) — but a second outbox beside it is prohibited by boundary 5 | Defer-db |
| `platform_admins`, `platform_sessions`, `platform_audit_events` | No name clash; note ERP already uses a *schema* named `platform` — kernel `platform_*` tables in `public` would be confusable | Prohibited surface |
| `tenant_entitlement_grants` | No clash | Defer-db (E8) |

ERP schema inventory (for placement decisions): `public` plus domain schemas
incl. `hr`, `lease`, `core_org`, `tax`, `rpt`, `pm`, `fa`, `support`,
`payments`, `core_fx`, `ar`, `expense`, `payroll`, `recruit`, `training`,
`ipsas`, `audit`, `platform`, `procurement`.

### Alembic

- ERP: single `script_location = alembic` (`alembic.ini`), 372 revision files,
  one root (`down_revision = None` count: 1), version table
  `alembic_version` in schema `public`
  (`alembic/env.py` — `version_table_schema="public"`, `include_schemas=True`).
  Production migrates via `scripts/deploy.sh:93` →
  `poetry run alembic upgrade heads` (plural heads is a lived habit).
- Kernel a83: 26 revisions under `dotmac_kernel/migrations/versions/`
  (`…0001_initial_tenant_schema` … `…0026_platform_audit_log`), designed to be
  composed via the consuming app's `version_locations`. Composition would put
  a **second root** into ERP's revision graph sharing the same
  `public.alembic_version` table → guaranteed extra head, and `upgrade heads`
  would then execute kernel DDL implicitly. This is exactly what the E1
  acceptance forbids; blocked until the E8 ADR (which also decides schema
  placement and `FORCE ROW LEVEL SECURITY` handling for any kernel table).

### Middleware

- ERP stack (`app/main.py`): `ObservabilityMiddleware` (`app/observability.py`,
  added at `app/main.py:275`), `rate_limit_middleware`
  (`app/middleware/rate_limit.py`, main:278), `csrf_middleware`
  (`app/web/csrf.py`, main:279), `csp_middleware` (main:365),
  `redirect_error_template_middleware` (main:403), `audit_middleware`
  (main:536, dispatches to Celery), plus `app/middleware/request_cache.py`
  and security headers.
- Kernel middleware package: `csrf`, `observability`, `rate_limit`,
  `security_headers`, `tenant`. Every one duplicates an existing ERP owner;
  `middleware/tenant.py` additionally resolves tenancy from the Host header
  (ERP resolves org from authenticated identity, not Host). Prohibited.

### Routes

- ERP mounts every API router twice (bare legacy alias + `/api/v1`,
  `app/main.py:694-695`; retired CRM webhook removed), owns `/admin`, `/static`,
  and ~40 web routers. Kernel `app_factory.create_app` mounts platform auth,
  kernel `/static`, and feature routers incl. `/admin`. Never mounted;
  `/api/v1` remains pinned by
  `tests/architecture/openapi_contract_surface.json`.

### Settings

- Env collisions: `DATABASE_URL` read by both ERP (`app/config.py:35`) and
  kernel `config.Settings`; kernel also expects `PLATFORM_DATABASE_URL` in
  production validation. Kernel `config` constructs a `settings` singleton at
  import.
- Storage collision: `domain_settings` table (above). Contract overlap:
  kernel spec-registry/resolver vs ERP `app/services/settings_spec.py` +
  `app/services/domain_settings.py` (`control.settings` owner, with known
  direct-writer fragmentation — finding 5). Adapt only behind the ERP owner
  after E6.

### Audit

- ERP as-built: four writers, three tables — HTTP `audit_middleware`
  (async via Celery) → `public.audit_events`; manual dispatcher + ORM flush
  listener → `audit.audit_log`
  (`app/models/finance/audit/audit_log.py`, schema `audit`); field tracker →
  `audit.field_change_log` (`app/models/audit_field_tracking.py`); plus
  `domain_setting_history`. Kernel `audit.write_audit_event` would be a fifth
  writer into a colliding table name. Blocked until E6 names one writer.

### Identity

- Kernel `Party`/`UserCredential`/`AuthSession`/`platform_auth` vs ERP
  `Person` (`public.people`) + `user_credentials`/`sessions`/`mfa_methods`/
  `api_keys` (`app/models/auth.py`). Prohibited for this program (boundary 4);
  guarded already by `tests/architecture/test_identity_protocol_boundary.py`.
  There is no OIDC adapter to compare against: `app/services/sso/` was deleted
  on 2026-08-15 (never enabled, zero rows). `federated_identities` survives as
  an empty table with no reader, pending a separate retirement change.

### Outbox

- ERP owner (`events.outbox` in the registry):
  `app/services/finance/platform/outbox_publisher.py` (`OutboxPublisher`) over
  `platform.event_outbox`, relayed by `app/tasks/outbox_relay.py`, with
  saga/checkpoint companions (`app/models/finance/platform/…`). Kernel
  messaging BEHAVIOR is the E3 target semantics; kernel messaging STORAGE is
  deferred (see classification). Plan-claimed defects verified at this pin —
  see "Verified plan assumptions" below.

### Session factories

- ERP: one `SessionLocal` (`app/db/__init__.py`) plus the canonical context
  managers (`app/db/session_context.py`). Kernel `db.py`: `engine` +
  `platform_engine` created at import time, `SessionLocal` +
  `PlatformSessionLocal`, and a `get_db` that primes RLS with GUC
  `app.current_tenant` (transaction-scoped `set_config`). Two collisions:
  a second (and third) session factory, and a **different RLS GUC name** than
  ERP's `app.current_organization_id` — kernel sessions would satisfy neither
  ERP RLS policies nor the ORM listener. `dotmac_kernel.db` therefore stays
  defer-db behind E8.

## Organization context + PostgreSQL RLS initialization (as-built evidence)

ERP tenancy is dual-layer, and both layers must always be primed together
(registry rule for `organization_tenancy`):

- **Layer 1 — ORM listener:** `app/db/org_listener.py` (`do_orm_execute`
  handler `_add_org_filter`) reads `session.info["organization_id"]`,
  fail-closed via `MissingOrgContextError`
  (`app/db/multi_tenant.py`), gated by `ENFORCE_ORG_FILTER` (default on).
  SELECT-time only; UPDATE/DELETE isolation is RLS's job.
- **Layer 2 — PostgreSQL RLS:** policies created dynamically by migration
  `alembic/versions/add_rls_policies.py` (plus `add_hr_rls_policies.py` and
  schema-specific successors). A clean database at migration heads has 103
  tables whose policies still carry the legacy `should_bypass_rls()` predicate;
  the earlier 16-table count described stale production, not ERP's design.
  Runtime code sets only `app.current_organization_id` (and shared-module
  `app.current_tenant`); it has no writer for the user-settable bypass GUC.

Per execution context:

| Context | Priming path | Both layers? |
|---|---|---|
| HTTP JSON API | `app/api/deps.py::get_db_with_org` — `prime_session(db, org)` + `set_current_organization_sync(db, org)` (deps.py:106-107), org from `require_tenant_auth`; auto-commit at edge. Cross-tenant admin/auth bootstrap dependencies set only `info["allow_cross_org"]`; their historical `*_bypass` names refer to the ORM listener, not PostgreSQL RLS | Tenant paths: yes. Cross-org paths: ORM only |
| HTTP web (Jinja/HTMX) | `app/web/deps.py::get_db_for_org` (web/deps.py:1436; primes both at 1468-1475). Public-slug flows (careers/onboarding) open unprimed via `get_db` (web/deps.py:79), resolve the org under bypass, then `prime_tenant_context` (`app/db/session_context.py:119`) sets both layers retroactively | Yes — canonical dep or `prime_tenant_context` |
| **Not middleware** | ERP has **no tenant middleware**: org context is established by DB dependencies after authentication, never from Host headers. (Contrast: kernel `middleware/tenant.py` + kernel `get_db`.) | n/a |
| Celery tasks | `app/db/session_context.py::session_for_org` primes ORM and database tenant scope; `cross_org_session` bypasses only the ORM listener. `session_for_org` re-arms its transaction-local PostgreSQL scope GUCs on every `after_begin`, so commits do not silently lose scope. One session per org remains required to avoid identity-map contamination. `scripts/check_session_context.py` enforces the boundary in CI and pre-commit. Worker bootstrap (`app/celery_app.py`): `worker_process_init` re-registers audit listeners per process; beat uses `app.celery_scheduler.DbScheduler` | Tenant paths: yes. Cross-org discovery: ORM only |
| CLI / maintenance scripts | Same context managers (`session_for_org` / `cross_org_session`), e.g. `scripts/backfill_mailcow_offboarding.py`, `scripts/review_suspicious_bank_matches.py`. `scripts/check_session_context.py` now scans `scripts/` as well as `app/tasks/` and `app/tools/`; the exact legacy count is a shrinking ratchet in `scripts/session_context_legacy.txt`, with the fail-silent RLS subset independently ratcheted by `tests/architecture/test_script_rls_scope.py`. Provisioning entry points now separate global discovery/catalogue work from tenant-owned writes | Yes — enforced, with a shrinking legacy baseline |
| Reconciliation jobs | They are Celery tasks (e.g. `app/tasks/dotmac_sub.py` daily/full reconciliation, `app/tasks/outbox_relay.py`) → `session_for_org`/`cross_org_session` path above. The outbox relay processes per-event org context from the event row | Yes — same task path |
| Migrations | `alembic/env.py` builds its own engine from the migration URL. Historical revisions that set the legacy GUC remain immutable; new data migrations must use the migration executor's approved database authority rather than reintroducing a runtime GUC escape. Deploy path: `scripts/deploy.sh` → `alembic upgrade heads` inside the app container | Separate migration authority |

Any kernel adoption that opens sessions (db/messaging/entitlements/audit)
must reach RLS through these exact seams or atomically extend them — a kernel
session primed with `app.current_tenant` primes **neither** ERP layer.

## Verified plan assumptions (evidence for later slices — no changes made)

E3 (outbox hardening) claims, all **confirmed** at 96928fa1:

1. Settlement methods commit inside the service: `OutboxPublisher.mark_published`
   (`app/services/finance/platform/outbox_publisher.py:180`), `handle_retry`
   (:222), `mark_dead` (:250), `retry_dead_event` (:304) each call
   `db.commit()`.
2. Unregistered event types are counted as skipped but marked `PUBLISHED`:
   `app/tasks/outbox_relay.py:195-203` ("No handler … — marking published").
3. A ledger handler can swallow per-line errors and return normally, letting
   the relay mark the whole event published:
   `handle_ledger_posting_completed` (`app/tasks/outbox_relay.py:65-136`) —
   the per-line `except Exception: logger.exception(...)` continues the loop,
   then the relay calls `mark_published` unconditionally on normal return.

**E3 update (implemented in this branch):** all three behaviors above are
repaired. `OutboxPublisher` settlement methods flush only (the relay task
owns commit/rollback); the relay is claim/deliver/settle
(`FOR UPDATE SKIP LOCKED` claim + token-gated settlement over new
`claim_token`/`claimed_at`/`lease_expires_at` columns, migration
`20260802_add_outbox_claim_lease_columns`); unknown events dead-letter as
`terminal_reason="unsupported_event"` unless declared no-consequence
(`DECLARED_NO_CONSEQUENCE*` in `app/tasks/outbox_relay.py`); the ledger
handler re-raises per-line failures so the whole delivery transaction rolls
back; replay of DEAD events is audited; and
`reconcile_outbox_balance_projection` verifies the GL balance projection
against posted ledger lines, repairing drift via
`rebuild_balances_for_period`. Canaries:
`tests/integration/platform/test_outbox_applied_result_canaries.py` (PG) and
`tests/tasks/test_outbox_relay.py`.

E9 claim confirmed: `app/licensing/validator.py:32` still ships
`_PUBLIC_KEY_B64 = "REPLACE_WITH_REAL_PUBLIC_KEY_BASE64"`.

No plan-assumption contradictions were found at this pin.

## E1 acceptance restated against this evidence

Adding the `dotmac-kernel==0.1.0a8` pin alone (E2) cannot:

- **mount routes** — ERP never calls `create_app`/`mount_features`
  (`app_factory`/`features` mounting are prohibited/metadata-only above);
- **run kernel migrations** — ERP's `alembic.ini` has no `version_locations`
  entry for the kernel and `deploy.sh` migrates only ERP's graph;
- **create a second session factory** — `dotmac_kernel.db` (import-time
  engines) is defer-db and outside the import allowlist;
- **change owner/transaction behavior** — no registry owner moves; the import
  boundary test fails any `app/` import outside the consume-pure allowlist.

## Prior Phase-0 content

The Phase-0 authority inventory (identity/money/async ledger tables), the
kernel *extraction* verdicts (Money/FX conventions, tax schema, outbox
state machine, SequenceService, secrets), and the findings list remain
historically accurate for pin 318a6e0d and are superseded here only where
the reconciliation table above says so. For current owners, always read
`app/services/sot_relationships.py` first.
