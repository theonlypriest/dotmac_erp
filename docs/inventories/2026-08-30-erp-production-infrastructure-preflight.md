# ERP production infrastructure preflight — measured inventory and mutation paths

**Nothing in this document constitutes adoption.** ERP has NOT adopted
Deployment Foundation 0.3 or Deployment Control a4. Neither is available to
adopt: Foundation 0.3 does not exist, and Control a4's verification is under
independent re-verification. This document records what production *is*, so
that a later cutover can be planned against measurements rather than memory.

- **Measured:** 2026-08-30, 12:04Z–12:15Z.
- **Host:** `149.102.158.167` (`vmi2988431`), named explicitly by Michael for
  this lane. Docker 29.1.3, Compose 2.37.1, uptime 209 days.
- **Method:** read-only inspection only. No container was started, stopped,
  recreated or removed; no file was written; no migration or deploy script was
  run; no firewall state was changed. No test was executed anywhere.
- **Secrets:** this document records variable *names* and whether they are set.
  No secret value appears here. Where a value is characterised, only its shape
  or location is given.

---

## 1. Service roster

Identity is the **registry digest**, not the tag. A tag is a mutable pointer.

### 1.1 Compose-managed — project `dotmac`, `/root/dotmac/docker-compose.yml`

| container | service | image (tag) | registry digest | local image id | state |
|---|---|---|---|---|---|
| `dotmac_erp_app` | `app` | `ghcr.io/michaelayoade/dotmac_erp:sha-34a7e9b` | `sha256:c7f4d7ab306f300806043c2f1c15692779cdecb2aa0a7525135b57572ea4cac9` | `sha256:e733dcce1f52f2e18a355daff67d2e4acfc6873dd2a0c0e3d2ca91efaf6c19f6` | running, **healthy**, 0 restarts, started 2026-08-29T20:15:29Z |
| `dotmac_erp_worker` | `worker` | same | same | same | running, **no healthcheck**, 0 restarts |
| `dotmac_erp_beat` | `beat` | same | same | same | running, **no healthcheck**, 0 restarts |
| `dotmac_erp_redis` | `redis` | `redis:7` | `redis@sha256:ba125ee995db4c9cf937bb5a771722f443ac96176c7aa5cd03711485ab77c852` | `sha256:6b8be92f31f3110b627cf93d5c9704f154eac7be88b7a57ea3c3a8c67b2b2727` | running 2 months, no healthcheck |
| `dotmac_erp_app_dev` | `app-dev` (profile `dev`) | `dotmac-app-dev` (host-built) | — none — | — | **exited**, container object still present |

All three ERP roles run the same image as uid/gid `10001:10001` with
`restart: unless-stopped` on network `dotmac_default`.

**Application image provenance labels** (read from the running image):

| label | value |
|---|---|
| `org.opencontainers.image.revision` | `34a7e9b45304d28709625fd880f2cd9ace49e8ec` |
| `io.dotmac.product-manifest.digest` | `sha256:9c3547745e453ffbd9339ce0d662af64a5071067087f16008f4630aae8b469b9` |
| `org.opencontainers.image.created` | `2026-08-29T18:35:06Z` |
| `org.opencontainers.image.version` | `main` — a **mutable branch name**, not a version |

The revision label equals `origin/main` at the time of measurement, and the
production checkout's `HEAD` is the same commit. Code identity is consistent.

### 1.2 Unmanaged — no compose project, no declarative owner

| container | image | registry digest | state |
|---|---|---|---|
| `dotmac_pg_local` | `postgis/postgis:16-3.4-alpine` | `postgis/postgis@sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f` | running 2 months, **no healthcheck** |
| `dotmac_demo_redis` | `redis:7-alpine` | `redis@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99` | **exited** 2026-08-29T16:59:06Z (quarantined) |

`dotmac_pg_local` is **the production PostgreSQL primary holding all ERP
data** — 7.9 GB on a host bind mount `/var/lib/dotmac-pg-local` — and it has
`com.docker.compose.project=` empty. It is a hand-run `docker run` container.
It is attached to both `bridge` and `dotmac_default`. It appears in no compose
file and in no descriptor. The ERP compose still declares a volume
`dotmac_erp_db_data` that **does not exist**, a dead declaration left over from
when the database was compose-managed.

This is the central structural fact for any Foundation cutover: the stateful
heart of ERP sits entirely outside the declarative deployment surface.

### 1.3 Separate compose project — `promtail`, `/opt/promtail/docker-compose.yml`

`grafana/promtail:latest` (**mutable tag**), digest
`grafana/promtail@sha256:6cfa64ec432b24a912d640e2edb940eeae2666f61861a66c121d763dd7241381`.

This is *not* the promtail the ERP compose declares — see § 4 F1.

### 1.4 Host services, not containerised, in no compose file

`nginx` (80/443), `postgresql@16-main.service` (a **second** PostgreSQL, on
loopback 5432), `redis-server.service` (a **third** Redis, loopback 6379),
`mariadb.service` (loopback 3306), `postfix` (25), and one
`python3 -m http.server` (8888 — see § 4 E1).

---

## 2. Published sockets

Address families are listed separately because they take different paths. An
IPv4 publish is DNAT'd and traverses `FORWARD` → `DOCKER-USER`. An IPv6 publish
with no IPv6 docker network has no DNAT: `docker-proxy` accepts it as a local
process and it traverses `INPUT`, where `DOCKER-USER` is never consulted. The
ERP compose file documents this mechanism in its own comments.

| port | family | bind | path | effective reachability |
|---|---|---|---|---|
| 8003 | v4 | `127.0.0.1` | DNAT → `172.18.0.4:8002` | loopback only |
| 8003 | v6 | `[::1]` | `docker-proxy` → INPUT | loopback only |
| 6382 | v4 | `127.0.0.1` | DNAT → `172.18.0.3:6379` | loopback only |
| 6382 | v6 | — | **no v6 publish** | n/a — asymmetric with `app` |
| **9001** | v4 | `0.0.0.0` | DNAT → `172.17.0.2:5432` (PostgreSQL) | **`DOCKER-USER` allowlist, then DROP** |
| **9001** | v6 | `[::]` | `docker-proxy` → INPUT | blocked by UFW default-deny |
| 8888 | v4+v6 | `0.0.0.0` / `[::]` | host process → INPUT | **UFW `ALLOW IN Anywhere`, both families** |
| 80, 443 | v4+v6 | `0.0.0.0` / `[::]` | nginx | public, intended |
| 22 | v4+v6 | | sshd | public |
| 25 | v4+v6 | | postfix | no UFW allow → INPUT-denied |
| 6443 | — | **nothing listening** | | UFW `ALLOW IN Anywhere` — stale rule |

### 2.1 PostgreSQL on 9001 — contained on IPv4, incidentally closed on IPv6

`DOCKER-USER` allowlists `160.119.124.0/22` plus fifteen `/32` addresses for
port 9001, then `DROP`s everything else. A TCP connect from
`160.119.127.229` — inside that `/22` — **succeeded**, which proves the ACCEPT
path is live rather than merely present. The rule set is working as designed
for IPv4; a database port is reachable from Dotmac's own network and nowhere
else.

IPv6 is a different story. `ip6tables DOCKER-USER` carries six DROP rules for
ports 9001, 6390 and 6391 — and **every one of them is structurally inert**,
because the `[::]:9001` listener is a userland `docker-proxy` socket whose
traffic reaches `INPUT`, never `FORWARD`. What actually closes IPv6 9001 is
UFW's `deny (incoming)` default and the absence of a 9001 allow. The intended
control is not the control that is working. That is worth fixing deliberately,
because the next person to add a UFW allow for an unrelated port on that host
will not know the v6 database path is protected only by a default.

### 2.2 Orphaned and duplicated firewall state, with no known producer

- Ports **6390 and 6391** carry complete `DOCKER-USER` allowlists but have no
  listener and no DNAT rule. Dead.
- Port **5432** has a `RETURN` for one `/32` then a `DROP` in `DOCKER-USER`,
  but nothing DNATs to 5432. Inert.
- The 9001 allowlist exists **twice**, once in `--ctorigdstport` conntrack form
  and once in plain `--dport` form — two rule producers wrote overlapping sets.
- No `ufw`, `iptables`, `ip6tables` or `nft` command appears anywhere in
  `/root/.bash_history`. **The producer of these rules is unknown.**

---

## 3. Migrations and health

### 3.1 Alembic — exactly in step, seven lineages

`alembic current` (applied) and `alembic heads` (code) return the **same seven
revisions**. There is no extra head, no missing head, and no second head within
a single lineage:

`20260828_people_et_activation` (product), `fi_0001_stored_files`,
`ac_0001_accounting`, `im_0001_import_runs`, `nu_0001_numbering`,
`pe_0001_people_directory`, `tx_0003_result_fingerprint`.

Multiple heads here are the module-lineage model working correctly, not drift.
These match `deploy/product.toml`'s `expected_heads` exactly. (The prose comment
above that list says "these six heads" while the list correctly contains seven —
a comment/data drift, harmless but worth correcting.)

### 3.2 What the health checks actually assert

The controller will trust these, so what they assert matters more than that
they exist.

**`/health/live` returns 200 unconditionally.** Its own docstring says it
"should always return 200 if the Python process is running". It asserts process
existence and nothing else. It is not wired to any container healthcheck today,
but `deploy/product.toml` `[roles.health.live]` declares it for the `app` role,
so a Foundation controller *would* begin consulting it. It must never be read as
evidence that ERP works.

**`/health/ready` is the app container's healthcheck** and does assert real
things: a `SELECT 1` as the runtime role, a Redis `PING`, and the required
external dependencies. Measured live at 12:09Z:

| dependency | configured | healthy | required for ready |
|---|---|---|---|
| database (`SELECT 1`) | — | yes | yes |
| redis (`PING`) | — | yes | yes |
| **openbao** | yes | yes | **yes** |
| **storage** (object store) | yes | yes | **yes** |
| dotmac_sub | yes | yes | no |
| paystack | yes | yes | no |
| smtp | yes | yes | no |
| nextcloud | no | no | no |
| remita | no | no | no |

Overall status `ready`, HTTP 200. Three properties matter for the cutover:

1. **It covers the web process only.** Worker and beat have no healthcheck at
   all. Two of ERP's four roles are invisible to every probe a controller could
   gate on.
2. **`ready_with_degraded_dependencies` returns 200.** An optional dependency
   failing is invisible to Docker and to a controller reading only the status
   code.
3. **`openbao` and `storage` are `required`.** An OpenBao or object-store blip
   marks the app container *unhealthy*. A controller gating a deployment on
   container health would read a dependency outage as a failed release and could
   roll back a perfectly good one. See the observation-window ruling needed in
   the companion document.

`_check_redis` also fails open: it reports healthy when `REDIS_URL` is unset.

### 3.3 Dependencies

Required for readiness: PostgreSQL, Redis, OpenBao, object storage. Optional:
`dotmac_sub` API, Paystack, SMTP. Also present: Loki push, Sentry/GlitchTip,
and a central Prometheus scrape of `dotmac-erp-app`.

**OpenBao** is reached as an unauthenticated `GET /v1/sys/health` from this
host and is an enforced client in the OpenBao ACL. Retiring that probe is a
separate deliberate change and is out of scope here. Note that the compose file
sets `OPENBAO_ALLOW_INSECURE: 'true'` for every ERP role, and defaults
`OPENBAO_TOKEN` to the literal string `devtoken` if unset — a fail-open default
for a secret-bearing variable.

### 3.4 Environment

`/root/dotmac/.env` is mode 0600 and holds **102 keys**. Names only were read.
Nine are set-but-empty (`REFRESH_COOKIE_DOMAIN`, `BRAND_LOGO_URL`, `BRAND_MARK`,
`LANDING_CONTENT_JSON`, `OPENBAO_NAMESPACE`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`MAILCOW_SOGO_DB_HOST`, `MAILCOW_SOGO_DB_USER`, `MAILCOW_SOGO_DB_PASSWORD`).

**`OPENBAO_ADDR` and `OPENBAO_TOKEN` each appear TWICE.** Compose's dotenv
parser takes the last occurrence; `deploy.sh`'s `set_env_var` rewrites only the
*first*. The deploy script and the runtime can therefore disagree about a
secret-bearing key. This is a live defect, not a hypothetical.

---

## 4. Manual and legacy mutation paths

This is the finding that determines whether a cutover can ever complete. A path
not found here is a path that survives the cutover. Each entry states what it
mutates, who invokes it, and its disposition.

`/root/.bash_history` holds 2000 lines but was **last written 2026-08-10**,
while the most recent deploys were 2026-08-28/29. History therefore
**under-reports**: recent work happened in sessions whose history was never
flushed. Treat the command evidence below as a floor, not a ceiling.

### A. Sanctioned paths that must still be retired into the controller

| # | path | mutates | invoked by | disposition |
|---|---|---|---|---|
| A1 | `/root/dotmac/scripts/deploy.sh` | git worktree, `.env` (`ERP_IMAGE_TAG`, `APP_VERSION`), images, containers, DB schema | operator over SSH with `MIGRATION_DATABASE_URL` | **Retire** into the controller; preserve as rollback evidence through the shadow phase |
| A2 | `SKIP_BACKUP=1` escape on A1 | disables the pre-migration backup gate | operator | **Remove**, or make the controller refuse it |
| A3 | `/etc/cron.d/dotmac_erp_db_backup` — daily 18:00 root → `scripts/backup_erp_db.sh` | writes `/var/backups/db/`, uploads remotely; log last written 2026-08-29 18:04 | cron | **Retire** into a controller- or Foundation-declared job. It executes a script from the *mutable checkout*, so a `git checkout` silently changes the backup. |

### B. Legacy application writers — the highest risk in this inventory

| # | path | why it matters | disposition |
|---|---|---|---|
| **B1** | **`dotmac-books.service`**, installed at `/etc/systemd/system/`, currently `disabled` + `inactive` | Runs `poetry run gunicorn app.main:app` **as root** from `/root/dotmac` with `EnvironmentFile=/root/dotmac/.env` (full production DB and every secret) and `Restart=always`. One `systemctl start` launches a **second, uncontainerised ERP writer** against production — bypassing the image, the migration gate, compose, and any controller. `Requires=postgresql.service redis-server.service`, which is precisely why those host services exist. | **Mask and remove before cutover.** This is the single most dangerous latent path on the host. |
| **B2** | **`app-dev` compose service** (profile `dev`) | `build: context: .`; `volumes: - .:/app` (the whole checkout mounted over the application); `ports: 8002:8002` in **short syntax**, which publishes on `0.0.0.0` *and* `[::]`; `env_file: .env`; `DATABASE_URL: ${DATABASE_URL}` — **the production database**; `DOTMAC_DEV_MODE: 'true'`; `restart: unless-stopped`. `DOCKER-USER` drops 8003 and 8010 but has **no rule for 8002**, so an IPv4 publish would be internet-reachable. The container object `dotmac_erp_app_dev` still exists in exited state, so `docker start dotmac_erp_app_dev` alone revives it. `deploy.sh` checks for it **only** in `--people-employment-type-activation` mode; an ordinary deploy does not. | **Delete the service from the compose file and remove the container object, through the managed path.** |
| B3 | host `postgresql@16-main`, `redis-server`, `mariadb` services | The dependencies B1 needs in order to start. | Decide with B1. |

### C. Hand-run runtime mutation (evidenced in shell history)

| # | command class | count | what it defeats |
|---|---|---|---|
| C1 | `docker compose up -d --build app` | ~12 | Built the production image **on the host from the checkout**, bypassing CI, the registry and all provenance. Now closed for `app` — the current compose has no `build:` for it. The **only** remaining `build:` stanza is `app-dev`'s, so removing B2 closes this class completely. |
| C2 | `docker compose restart app`, `docker restart dotmac_erp_app` | ~8 | Hand restarts. Note the compose file's own warning: `restart` re-runs the *existing container object* with its existing `HostConfig.PortBindings`, so a hand restart silently preserves a stale socket binding that `up -d` would have corrected. |
| C3 | `docker compose cp scripts/… app:/app/scripts/…` | 1 | Copied code **into the running container**, mutating the runtime filesystem outside the image. Directly defeats digest identity. |
| C4 | `docker exec -i dotmac_erp_app python - <<'PY'`, `docker exec … python -c …` | ~8 | Ad-hoc Python against production holding an app DB session, several using `allow_cross_org` / `cross_org_session` to **deliberately bypass tenant isolation**. One wrote results out to `/root/offboarded_emails.txt`. |
| C5 | `psql` with an inline `PGPASSWORD=…`; `sudo -u postgres psql` | 2 | **A plaintext database credential is recorded in `/root/.bash_history`.** Treat that value as compromised: rotate it and purge the history file. (The value is deliberately not reproduced here.) |
| C6 | `docker compose exec -T db psql … -d dotmac_platform` reading SSH public keys | 2 | Cross-product database access from this host's shell. |

### D. Checkout-as-runtime — a standing mutation surface, not a command

| # | finding | consequence |
|---|---|---|
| **D1** | The production host holds a **full git checkout of the ERP source repository**, including all 166 entries in `scripts/` — backfills, bulk GL posting scripts, `cutover_database_ownership.py`, `bootstrap_database_roles.py`, `restore_from_backup.py`, and roughly thirty mutating `.sql` files. | Every one of them is one `python scripts/…` away from mutating production. A rendered deployment bundle would carry none of them. |
| **D2** | **Four bind mounts from the checkout into the running app**: `./static`, `./templates`, `./license`, `./gunicorn.conf.py`. Read-only inside the container; the **host side is a mutable git worktree**. | **The image digest does not determine runtime behaviour.** `git checkout <other-ref>` changes what production serves with no image change and, for static assets, no container recreate. This is the primary reason the current deployment cannot be identified by image digest alone. |
| D3 | Rollback by `git reset --hard` is reachable both from `deploy.sh`'s `rollback()` and by hand. Measured reflog, newest first: `34a7e9b4 reset` ← `34a7e9b4 pull --rebase` ← `63c59133 reset` ← `34a7e9b4 pull --rebase` ← `63c59133 pull --ff-only` ← `04cdbc64 checkout`. | That sequence contains a roll-back-then-roll-forward, and at least one `pull --ff-only` — which `deploy.sh` never issues (it uses `--rebase`), so it was **hand-run**. |
| D4 | `.env` is hand-edited; duplicate `OPENBAO_ADDR` / `OPENBAO_TOKEN` keys (§ 3.4); `.env.deploy-bak` is a hand-restorable copy. | Deploy script and runtime can disagree about a secret. |
| D5 | `docker-compose.yml.bak-20260829-loopback` — untracked hand-made backup of the compose file. | Evidence of a hand edit later regularised as commit `03e6d26b`. The live `docker-compose.yml` currently matches the committed one exactly — verified, no drift. |

### E. Host and network mutation with no known producer

| # | finding | disposition |
|---|---|---|
| **E1** | UFW `8888/tcp ALLOW IN Anywhere` (both families) fronting a root-run `python3 -m http.server 8888 --bind 0.0.0.0` serving `/root/.agent/diagrams`, started from an interactive SSH session (`session-5314.scope`), whose Python binary now reads `(deleted)`. Confirmed serving three HTML files (an ERP architecture diagram, an AR review dashboard, a mobile plan). The UFW rule carries **no source restriction**. | **P0.** Kill the process and remove the rule — through the managed path, not by hand from this lane. |
| E2 | UFW `6443/tcp ALLOW IN Anywhere` (both families), nothing listening. | Stale; remove. |
| E3 | Orphaned `DOCKER-USER` sets for 6390/6391 and 5432; duplicated 9001 set (§ 2.2). No firewall command in shell history. | Producer unknown — must be identified before the controller owns socket policy. |
| E4 | `/etc/cron.d/staticroute` — `@reboot` root, rewrites the eth0 link route. | Host networking mutation outside any product. Preserve deliberately or retire deliberately. |
| E5 | The `erp.dotmac.io` nginx vhost is a **regular file directly in `/etc/nginx/sites-enabled/`** — the host's other two vhosts are symlinks into `sites-available`. Hand-created, no version-controlled counterpart, last edited 2026-07-30. `certbot.timer` mutates its certificate material. | See companion document: the repo's rendered nginx config differs materially and is not deployed. |
| E6 | **Eight root SSH keys** with heterogeneous identities: `seabone@hp-server`, `dotmac@proxmox-10.120.120.20`, `michaelayoade@macboos-MacBook-Pro.local`, `server-158`, `root@dotmac-db-primary`, `erp-dotmac`, `kayamus29@gmail.com`, and one carrying **no identifying comment**. | Each is an independent, uncontrolled, full-root mutation path. Needs an owner-by-owner decision. |
| E7 | A `ralph` user with `/bin/bash` and a home directory; `/root/dotmac/.ralph_state`; the quarantined demo-redis volume is group-owned by `ralph`. | Identify and dispose. |

### F. Declared-versus-actual drift

| # | finding |
|---|---|
| **F1** | The running promtail is project `promtail` from `/opt/promtail/docker-compose.yml` on the **mutable** `grafana/promtail:latest`. The ERP compose declares its *own* promtail (pinned `grafana/promtail:3.0.0`) and a vmagent under the `observability` profile — **neither is running**. Two competing observability definitions; the undeclared one is the live one. |
| F2 | `deploy/systemd/dotmac-static-sync.service` and `.timer` exist in the repository but are **not installed** on the host. |
| F3 | `deploy/rendered/nginx/erp.dotmac.io.conf` differs materially from the live vhost and is **not deployed** (companion document § 3). |
| F4 | Orphan docker volumes: `dotmac_dotmac_books_db_data` (the legacy Books application's database), `acme_db_data`, `acme_dotmac_logs`, `acme_openbao_data`, `dotmac_mkt_certbot_conf`, `dotmac_mkt_certbot_www`, `dotmac_uploads_data`, `production-low-stock-hotfix_dotmac_logs`, plus four anonymous volumes. The compose declares `dotmac_erp_db_data`, which does not exist. |
| F5 | E2E and Playwright logs from June 2026 sit in the production checkout (`e2e-baseline*.log`, `e2e-final*.log`, `playwright-install.log`, `demo-app.log`) — evidence that tests were once executed **on the production host**, contrary to the CI-only rule. |

### G. The quarantined container — do not remove by hand

`dotmac_demo_redis`: ownerless (no compose project), exited
2026-08-29T16:59:06Z, holding an anonymous volume whose `dump.rdb` is
**261,457 bytes**, written at shutdown. The undrained Celery broker state is
**persisted, not lost**. It is *not* the ERP broker — that is
`dotmac_erp_redis` on 6382.

Per Michael's standing instruction it must be removed **through the managed
deployment path**, not by hand. It is recorded here precisely so the cutover
plan has to account for it.

---

## 5. What could not be measured, and why

| item | why not |
|---|---|
| Whether the pre-migration backups are **restorable** | Proving it requires a restore onto a disposable target. Not runnable from this lane, and never against production. Current evidence proves creation and byte-equal upload only. |
| The queue contents of `dotmac_demo_redis` | Reading them requires starting the container. Forbidden. Only the persisted `dump.rdb` size was observed. |
| Whether port 8888 is reachable from **outside** Dotmac's network | The probing workstation sits inside the allowlisted `/22`. The UFW rule carries no source restriction, so it is world-open *by rule*; that was not confirmed by an external connect. |
| The producer of the `DOCKER-USER` and UFW 8888/6443 rules | Absent from shell history entirely. |
| Any mutation performed between 2026-08-10 and now via an unflushed shell | `/root/.bash_history` was last written 2026-08-10. |
| Worker/beat runtime failure rates during this window | Would require log collection beyond read-only inspection scope; the prior audit's findings are cited rather than re-measured. |

---

## 6. Backup and restore mechanism

### 6.1 Backup producer — `scripts/backup_erp_db.sh`

Invoked by `/etc/cron.d/dotmac_erp_db_backup` daily at 18:00 as root, and by
`scripts/deploy.sh` before every migration.

```
docker exec [-e PGPASSWORD] dotmac_pg_local pg_dump -U <user> -d dotmac_erp \
  | gzip -9 > /var/backups/db/dotmac_erp_<UTC timestamp>.sql.gz
rclone copy <local_path> Backup:db.backup/dotmac_erp
# retention: keep the last 5 remote files, rclone deletefile the rest
```

Four properties matter for the cutover:

1. **It is `pg_dump`, not `pg_dumpall`.** Roles and globals are **never
   captured**. The least-privilege `dotmac_erp_app` login, `app_admin`, every
   `GRANT`, and the ownership that RLS policies depend on exist nowhere in any
   backup. A restore into a fresh cluster would produce a database no ERP role
   can log into.
2. **Plain format, not `--format=custom`.** No selective restore, no parallel
   restore, no `pg_restore --list` manifest to verify against.
3. **`PGPASSWORD` is passed via `docker exec -e`**, so it is visible in the host
   process table for the duration of every dump.
4. It reads `POSTGRES_*` from `.env` with `sed … | head -n 1` — the **first**
   match. Same first-versus-last hazard as `deploy.sh`'s `set_env_var` (§ 3.4).
   With duplicate keys present in `.env` today, the backup script and Compose
   can disagree about which value is authoritative.

There is no verification step: no checksum comparison, no manifest listing, no
test restore.

### 6.2 There is no restore procedure

`scripts/restore_from_backup.py` is **not a restore tool**, despite its name.
Its own docstring describes it accurately: it extracts only `COPY` data blocks
and `setval()` calls from a plain dump, writing them out with
`session_replication_role = 'replica'` to suppress foreign-key triggers, and it
**explicitly skips `CREATE`, `ALTER`, `DROP`, `COMMENT`, `GRANT` and
`REVOKE`**. It is a data-reload helper for an *already existing, already
migrated* database. It cannot rebuild one.

A repository-wide search found **no restore runbook, no `pg_restore`
invocation, no `psql < dump` procedure, and no `pg_dumpall` anywhere** in
`docs/` or `scripts/`.

**Finding: ERP has a backup producer and a data-reload helper, but no restore
procedure.** There is no documented, tested path from
`dotmac_erp_<ts>.sql.gz` back to a working ERP database. Prior audits
established that a backup was created and uploaded byte-identically; that is
evidence of *creation and transfer*, and it has never been evidence of
*restorability*.

This is a **hard prerequisite**, not a nice-to-have: a cutover step that says
"verify the backup and the restore command" cannot be satisfied, because the
restore command does not exist. Authoring and rehearsing one — on a disposable
target, never production — is human-gated work outside this read-only lane.

## 7. The exact legacy executor retirement target

Named precisely, so that "retired" is a checkable claim rather than a feeling.

**Primary target: `/root/dotmac/scripts/deploy.sh` on `149.102.158.167`** — 486
lines, invoked as `MIGRATION_DATABASE_URL=<value from the root-only host file>
./scripts/deploy.sh`. It is today the *only* sanctioned production migration
path. Retirement means it ceases to be the executor; it is not deleted while it
remains rollback evidence.

`/root/dotmac/docker-compose.yml` is **retained as rollback evidence** and is
explicitly *not* part of the retirement.

**Retirement gate:** two successful controller-owned deployments, per the
production sequence. One is not enough — a single success cannot distinguish a
working controller from a lucky one.

**Secondary executors that must retire in the same movement**, or the
retirement is nominal only — each is an independent path to mutate production
state without the controller:

| executor | why it must go with it |
|---|---|
| `/etc/cron.d/dotmac_erp_db_backup` | executes a script from the mutable checkout on a root cron |
| `dotmac-books.service` | can start a second uncontainerised ERP writer (§ 4 B1) |
| the `app-dev` compose service and its container object | production `DATABASE_URL`, host build, wide publish (§ 4 B2) |
| the checkout's 166 `scripts/` entries | every backfill, cutover and bulk-SQL script is one command from production (§ 4 D1) |
| the four checkout bind mounts | while they exist, the controller's digest binding does not identify the runtime (§ 4 D2) |

A retirement that removes only `deploy.sh` leaves all five standing.
