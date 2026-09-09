# ERP Foundation 0.3 / Control a4 adoption preflight — drafts, procedures, and the exact diff

**Nothing in this document constitutes adoption.** ERP has NOT adopted
Deployment Foundation 0.3 or Deployment Control a4, and cannot: **Foundation
0.3 does not exist**, and Control a4's verification was performed by a
defective verifier and is under independent re-verification. A pin, a render, a
CI run and a rehearsal are each *not* runtime adoption. Every version
coordinate below is either a measurement or a **named placeholder**. None is a
guess.

Companion to `2026-08-30-erp-production-infrastructure-preflight.md`, which
holds the measured production state this document plans against.

---

## 1. Where ERP is pinned today

| pin | current value | source |
|---|---|---|
| `dotmac-deployment-foundation` | `0.2.0a2` | `pyproject.toml:135`, `poetry.lock:1078` |
| `dotmac-deployment-control` | **not pinned at all** | ERP does not consume Control today |
| deployment-conformance reusable workflow | `michaelayoade/dotmac_starter_mt/.github/workflows/deployment-conformance.yml@55750e104df3dd94b6f9f70bf8c8db53986394c7` | `.github/workflows/deployment-conformance.yml:25` |
| `dotmac-kernel` | `0.1.0a98` | `pyproject.toml:60` |
| `dotmac-ui` | `0.1.0a7` | `pyproject.toml:64` |

### 1.1 Measured release coordinates for the two gating artifacts

**Foundation 0.3 — DOES NOT EXIST.** After fetching tags, the newest
Foundation tag in `michaelayoade/dotmac_starter_mt` is
`dotmac-deployment-foundation-v0.2.0a2`. There is no 0.3 tag of any kind.

**Control a4 — tagged, coordinate recorded, NOT verified:**

| field | measured value |
|---|---|
| repository | `michaelayoade/dotmac_deployment_control` |
| tag | `dotmac-deployment-control-v0.1.0a4` |
| tag object (annotated) | `3bc4ab0000c3a3dc8a4cf495d9cfec56ded6ed6a` |
| **peeled commit** | **`2c61540f74018b7e19d7c5add893e0653cfcdb17`** |
| tagged | 2026-08-30T06:41:29Z |

Recorded as a measurement only. A tag existing is not evidence it is
publishable, pinnable, or verified — and this one's verification is explicitly
in doubt. Do not pin against it until the independent re-verification returns.

---

## 2. Foundation descriptor — draft state and named holes

`deploy/product.toml` (`ProductDeploymentSpec.v1`) is already substantially and
honestly authored: it declares three roles (`app`, `worker`, `beat`) with
commands, replicas, stop-grace, resources and security; a typed `[migration]`
block with `expected_heads`, an owner material and a lock timeout; typed
`[roles.worker] ping_command` and `[roles.scheduler] tick_command` with
`last_tick_max_age_seconds = 120`; and an `[ingress]` block. The holes below are
gaps in the *facility* or the *runtime*, not sloppiness in the descriptor.

### 2.1 Stale bindings — refresh before any adoption

| field | descriptor holds | production runs |
|---|---|---|
| `[image].reference` | `…@sha256:d33c172a6d93449e4815f04182f79fbf517e955f8efa1d61bd2a74f19bc9586c` | `…@sha256:c7f4d7ab306f300806043c2f1c15692779cdecb2aa0a7525135b57572ea4cac9` |
| `source_revision` | `9b3fb250ac9b0a8ed47cf60060d0eae737f0d4fd` | `34a7e9b45304d28709625fd880f2cd9ace49e8ec` |
| `[assembly].manifest_digest` | `sha256:9c3547745e453ffbd9339ce0d662af64a5071067087f16008f4630aae8b469b9` | **matches** the running image's `io.dotmac.product-manifest.digest` — no change needed |

The descriptor is pinned to the a2-era image from PR #415. The composed module
set has not changed since, which is why the manifest digest still matches.

### 2.2 HOLE-1 — typed socket exposure cannot be expressed yet

Michael's requirement is exposure declared through **typed policy, with no
literal infrastructure addresses**. The descriptor cannot do that today:
`[ingress]` carries `host`, `trusted_proxies` and `[[ingress.routes]]`, but no
exposure classification and no address-family typing; and
`[[external_dependencies.ports]]` still uses raw `container = 6379` /
`host = 6382` integers.

The shape that would satisfy the requirement — exposure mandatory, per-family
bind material, free-form bind removed, loopback derived, `none` emitting no
publication, Compose using long syntax — is IngressPolicy.v1, which landed in
Starter **after** 0.2.0a2 and is not in the pinned facility.

**HOLE — pending Foundation 0.3.** When IngressPolicy.v1 is published, the
following must be declared, and each is stated here as intent, not as syntax I
have invented:

| surface | intended typed exposure |
|---|---|
| `app` | private / loopback, **both** address families (matching today's explicit `127.0.0.1` + `::1` publish) |
| `redis` | private / loopback, **v4 only today** — the v6 half must either be added or its absence declared deliberately, because the current asymmetry with `app` is accidental |
| PostgreSQL (9001) | a network-scoped exposure — **but see HOLE-2; it is not a declared role at all** |
| everything else | `none` |

I have deliberately not written a placeholder TOML block for this. Inventing a
shape that Foundation 0.3 then contradicts would be worse than a named hole.

### 2.3 HOLE-2 — the database is not in the descriptor

`dotmac_pg_local` holds 7.9 GB of production data on a host bind mount, belongs
to no compose project, and appears nowhere in `product.toml`. Foundation's
model is one release on one host. Whether the database becomes a declared
`external_dependency` with a socket policy, or stays outside the facility
entirely as host-owned infrastructure, is **an architecture decision for
Michael**, not one this lane should make. Until it is decided, the descriptor
cannot claim to describe ERP's deployment.

### 2.4 HOLE-3 — controller authorization envelope

Per the accepted invariant, a deployment carries two independent identities and
the envelope must bind both.

**Application release identity — measurable today:**

| axis | value |
|---|---|
| OCI repository digest | `ghcr.io/michaelayoade/dotmac_erp@sha256:c7f4d7ab306f300806043c2f1c15692779cdecb2aa0a7525135b57572ea4cac9` |
| source revision | `34a7e9b45304d28709625fd880f2cd9ace49e8ec` |
| product-manifest digest | `sha256:9c3547745e453ffbd9339ce0d662af64a5071067087f16008f4630aae8b469b9` |
| declared release-role roster | `app`, `worker`, `beat` (release) + `migrate` (migration) + `redis` (auxiliary) |
| exact descriptor digest | **derivable only once § 2.1 and HOLE-1 are resolved** |
| canonical Compose service hashes | **derivable only from a re-render** |

**Controller / authorization identity — four named holes, none fillable here:**

1. **Foundation 0.3 wheel coordinates and launcher** — the release does not
   exist.
2. **Release and authorization signing keys, key IDs and public keys** — not
   provisioned. No secret path was invented or accessed.
3. **Root-owned host trust policy** on `149.102.158.167` — not provisioned.
4. **The deployment-control authorization-finalizer lane** — the protected
   `workflow_run` finalizer, signing environment and publication contract are
   owned by `dotmac_deployment_control`, not by ERP and not by Starter.

Only the declared application roster joins the quorum; `redis` is auxiliary and
`migrate` is a migration role, so neither should.

### 2.5 HOLE-4 — the runtime is not identifiable by digest (a runtime defect, not a descriptor one)

The descriptor already declares `static = "image"` and states plainly that it
is *the corrected target*, not a claim about today. Today, four bind mounts
from the mutable checkout — `./static`, `./templates`, `./license`,
`./gunicorn.conf.py` — determine part of what production serves. **Until those
are removed, binding the image digest does not identify the runtime.** The
descriptor is right and the runtime is wrong; the cutover is what closes it.

### 2.6 HOLE-5 — worker and beat health

`[roles.worker].ping_command` and `[roles.scheduler].tick_command` are good
typed shapes. But the running worker and beat have **no healthcheck at all**,
and the known worker failures are invisible to every existing probe. Adopting
these declarations makes the controller start asserting something nothing
asserts today. Expect first-run failures — that is the controller working, and
it must be discovered in rehearsal, not at cutover.

### 2.7 HOLE-6 — `/health/live` is declared but asserts nothing

`[roles.health.live]` binds `/health/live`, which returns 200 unconditionally.
That is acceptable for a liveness role provided the controller never reads it
as evidence of function. The descriptor should say so in a comment, so the next
reader does not promote it.

### 2.8 Minor

`expected_heads`' prose comment says "these six heads"; the list correctly
contains seven, matching production exactly. Fix the comment.

---

## 3. The rendered bundle would cause an outage if adopted as-is

`deploy/rendered/` was generated against the a2-era digest and is stale. More
importantly, it is internally coherent but **incompatible with the live host in
three ways that must be handled atomically**.

### 3.1 The 8003 → 8002 port move

| | live | rendered |
|---|---|---|
| app publish | `127.0.0.1:8003` **and** `[::1]:8003` (long syntax, both families) | `127.0.0.1:8002:8002` (v4 only) |
| nginx upstream | `upstream dotmac_erp { server 127.0.0.1:8003; }` | `upstream dotmac-erp_app { server 127.0.0.1:8002 …; server 127.0.0.1:18001 backup …; }` |

The rendered pair agrees with itself. **Adopting the compose without the nginx
config, or the nginx config without the compose, black-holes all production
traffic.** They must land in one atomic step.

Two sub-points worth a deliberate decision rather than an accident:
- The rendered publish is **v4-only**, dropping today's explicit `::1` publish.
  That is a *reduction* in exposure and is fine — but it should be a declared
  intent under HOLE-1, not a side effect of short syntax.
- The rendered nginx introduces a **warm deployment candidate** at
  `127.0.0.1:18001` whose presence *is* the handoff mechanism. Nothing on the
  host provides it today.

### 3.2 The network move would sever the database

The rendered compose creates `dotmac-erp_net` and attaches all services to it.
`dotmac_pg_local` is attached to `dotmac_default` and is not in the compose
file at all. **The app would lose its route to PostgreSQL.** This must be
resolved — by joining the database to the new network, by changing the DSN, or
by resolving HOLE-2 — before any render is applied.

### 3.3 Container naming has a wide blast radius

The rendered compose sets **no `container_name`**, so services become
`dotmac-erp-app-1` and friends. That breaks the cron backup script,
`deploy.sh`'s `docker inspect dotmac_erp_app`, the `DOCKER-USER` rules that
operators reason about by container, and every operator habit. Not an outage,
but it must be planned.

### 3.4 What the rendered bundle gets right, and should be kept

`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `user 10001:10001`,
tmpfs `/tmp`, per-role CPU/memory/pids limits, log rotation, a typed `migrate`
role gated by `service_completed_successfully`, and a redis healthcheck that
actually authenticates rather than just opening the socket. All of these are
strictly better than the live compose. It also removes the four checkout bind
mounts (§ 2.5) — correct, and the change that must be verified hardest, since
it moves static assets and templates from the worktree into the image.

---

## 4. Backup, rollback and observation

### 4.1 Backup

Today: a daily 18:00 root cron plus `deploy.sh`'s pre-migration backup, written
to `/var/backups/db/` and uploaded remotely. The prior audit confirmed creation
and byte-equal upload of a 1,049,276,524-byte artifact. **That proves creation
and transfer. It does not prove restorability, and nothing on this host ever
has.**

Required before cutover, and not runnable from this lane:

1. One restore rehearsal onto a **disposable** target — never production, never
   this workstation. CI or a dedicated host owns it.
2. The rehearsal must assert the seven Alembic heads are present after restore,
   plus a row-count / aggregate parity check against the source.
3. The backup path must become controller-declared. A cron executing a script
   from a mutable checkout (§ A3 of the companion document) is not a backup
   guarantee.

### 4.2 Rollback — must be controller-owned

Today rollback is `git reset --hard` plus an `ERP_IMAGE_TAG` restore plus
`docker compose up -d`, inside `deploy.sh` — and, as the reflog shows, also by
hand. Migrations are never reverted.

A controller-owned rollback must bind, as one authorized envelope:

- the previous image **repository digest** (not tag). Prior image tags still
  resident on the host: `sha-c10e6fe`, `sha-63c5913`, `sha-04cdbc6`,
  `sha-713c072`;
- the previous descriptor digest and previous source revision;
- the pre-migration backup object identity.

And it must be able to **refuse**. ERP already declares
`compatibility = "maintenance_required"` and marks
`20260828_people_et_activation` forward-fix-only — past that migration the
previous image is not a valid rollback target and the controller must say so
rather than attempt one. A hand `git reset` must become detectable: the
controller's recorded state has to be compared against the host on every run,
or D3 simply survives the cutover.

### 4.3 Observation window — and what makes it fail

A procedure that cannot fail is not a procedure. Duration: **at least 30
minutes**, covering at least two Beat scheduled ticks and one half-hourly
`dotmac_sub` incremental sync.

| measurement | failure threshold |
|---|---|
| app container health | any transition to `unhealthy` → **FAIL** |
| restart counts (app, worker, beat) | any `> 0` → **FAIL** |
| `/health/ready` | non-200 → **FAIL**; `ready_with_degraded_dependencies` naming a **required** dependency → **FAIL** |
| `celery -A app.celery_app inspect ping` | fewer than 1 online node → **FAIL** |
| Beat tick age | `> 120s` (the descriptor's own `last_tick_max_age_seconds`) → **FAIL** |
| worker ERROR/CRITICAL/traceback count | **above the baseline captured immediately before cutover** → **FAIL** |
| applied Alembic heads | ≠ the seven declared `expected_heads` → **FAIL** |
| external `https://erp.dotmac.io/health/ready` | non-200 → **FAIL** |

**The worker baseline is not zero, and this is the trap.** ERP's worker is
known-degraded: tenant-catalog privilege failures recur on a five-minute cycle
and the `dotmac_sub` incremental sync hits its 25-minute soft and 28-minute
hard limits. An observation window that reports "no new errors" against an
assumed-clean baseline is measuring nothing. The baseline must be **captured
immediately before cutover and compared**, never assumed.

### 4.4 A hazard needing Michael's ruling

`openbao` and `storage` are `required_for = ["ready"]`. An OpenBao or
object-store blip during the observation window marks the app container
unhealthy, and a controller gating on container health will read that as a
**failed release** and may roll back a good one.

The controller must be able to distinguish *dependency* failure from *release*
failure — or the window must abort-and-retry rather than roll back. **This
needs a decision before Foundation 0.3 is given rollback authority over ERP.**

---

## 5. The exact adoption diff

**This diff cannot be finalised.** Foundation 0.3 is unpublished and Control a4
is unverified. Every version coordinate below is a named placeholder. No hash
or version number has been guessed.

### 5.1 `pyproject.toml`

```diff
-dotmac-deployment-foundation = {version = "0.2.0a2", source = "forgejo"}
+dotmac-deployment-foundation = {version = "<FOUNDATION_0_3_VERSION — UNPUBLISHED>", source = "forgejo"}
```

`poetry.lock` regenerated to match. **Blocked:** no 0.3 release exists.

### 5.2 `.github/workflows/deployment-conformance.yml`

```diff
-    uses: michaelayoade/dotmac_starter_mt/.github/workflows/deployment-conformance.yml@55750e104df3dd94b6f9f70bf8c8db53986394c7
+    uses: michaelayoade/dotmac_starter_mt/.github/workflows/deployment-conformance.yml@<FOUNDATION_0_3_PEELED_COMMIT — DOES NOT EXIST>
```

The package pin and the workflow ref move **together**: 0.2.0a2's collector and
that revision's workflow are two halves of one contract, and 0.3 will have the
same property. Pinning one without the other pairs a package with a caller that
does not satisfy it.

### 5.3 Control a4 — placement is a decision, not a default

ERP has **no** Control pin today. Whether ERP consumes Control as a package
dependency, or only through a workflow it does not itself pin, is **Michael's
decision**. The measured coordinate, recorded but not accepted:

```
repository   michaelayoade/dotmac_deployment_control
tag          dotmac-deployment-control-v0.1.0a4
tag object   3bc4ab0000c3a3dc8a4cf495d9cfec56ded6ed6a
peeled       2c61540f74018b7e19d7c5add893e0653cfcdb17
```

**Blocked:** verification performed by a defective verifier; independent
re-verification in flight.

### 5.4 `deploy/product.toml`

- refresh `[image].reference` → `sha256:c7f4d7ab…cac9` and `source_revision` →
  `34a7e9b4…` (§ 2.1) — or to whatever protected-main revision is current when
  the cutover PR is raised, measured then, never typed from here;
- add typed exposure once IngressPolicy.v1 exists (HOLE-1);
- resolve the database's place in the model (HOLE-2);
- fix the six/seven comment (§ 2.8);
- add the `/health/live` caveat comment (HOLE-6).

### 5.5 `deploy/rendered/**`

Re-render from the published Foundation 0.3 CLI. `render --check` is a **byte
comparison**, so it will fail until re-rendered. Rendering must happen only
from published artifacts — never from a working tree.

### 5.6 Host preparation, ordered, all outside this lane

1. Mask and remove `dotmac-books.service` (B1).
2. Delete the `app-dev` service and its container object (B2) — this also
   closes the in-place host-build class (C1).
3. Remove `dotmac_demo_redis` **through the managed path** (G).
4. Kill the port-8888 server and remove its UFW rule (E1); remove the stale
   6443 allow (E2).
5. Resolve the database's network and declaration (HOLE-2 / § 3.2).
6. Retire the backup cron into a declared job (A3).
7. Rotate the credential exposed in shell history and purge the file (C5).
8. Deduplicate `OPENBAO_ADDR` / `OPENBAO_TOKEN` in `.env` (§ 3.4 of the
   companion document).

### 5.7 Branch protection

ERP `main` protection is strict but requires **neither** Deployment Foundation
context. Before any claim of a required reference-adapter gate, add both:
`deployment / Deployment descriptor conformance` and
`deployment / Hardened image contract`.

---

## 6. Decisions needed from Michael

1. **The database (HOLE-2).** Declared `external_dependency` inside the
   facility, or host-owned infrastructure outside it? Everything else in the
   descriptor waits on this.
2. **Control a4's placement (§ 5.3).** Package pin in ERP, or workflow-only
   consumption?
3. **Dependency failure vs release failure (§ 4.4).** What should the
   controller do when OpenBao or object storage blips inside the observation
   window?
4. **Container renaming (§ 3.3).** Accept the rendered bundle's generated names
   and fix every consumer, or carry `container_name` forward?
5. **The eight root SSH keys (E6)**, the `ralph` account (E7), and the
   `staticroute` cron (E4) — retain, or retire?
6. **The `SKIP_BACKUP=1` escape (A2)** — remove entirely, or keep as a
   controller-refusable flag?
