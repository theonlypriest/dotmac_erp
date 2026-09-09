# ERP deployment entrypoint census — every way production state can change

Step 1 of the executor retirement is *freeze the legacy executor and inventory
every trigger*. This is that inventory.

It exists because a shallow census misses the dangerous things. The preflight
found `dotmac-books.service` installed-but-disabled and an `app-dev` compose
service carrying the production `DATABASE_URL` — neither is in `scripts/`, and
neither would appear in a census that looked there. So this walks **families of
entrypoint**, not directories.

- **Measured:** 2026-08-30, host `149.102.158.167` (`vmi2988431`), read-only.
- **Scope:** anything that can change what runs in production, or the data it
  runs against. Application-plane writes (a user editing an invoice) are out of
  scope; control-plane changes are in.
- **Not in this document:** the zero-surface ratchets and the retirement receipt
  schema. Those are owned by a separate lane and this census is an input to
  them, not a substitute.

**Nothing here is frozen, disabled or deleted yet.** The legacy path first loses
ordinary authority (step 3), survives briefly as explicit break-glass rollback,
and is deleted only after two proven controller cycles (step 6).

---

## A. Deployment executors

| # | entrypoint | mutates | invoked by | disposition |
|---|---|---|---|---|
| A1 | `/root/dotmac/scripts/deploy.sh` (486 lines) | git worktree, `.env`, images, containers, DB schema | operator over SSH with `MIGRATION_DATABASE_URL` | **The** retirement target. Step 3: lose ordinary authority. Step 6: delete. |
| A2 | `SKIP_BACKUP=1` on A1 | disables the pre-migration backup gate | same | Remove with A1; a documented way to skip the backup is not a break-glass feature. |
| A3 | `--people-employment-type-activation` mode of A1 | forward-only migration, drains writers | same | Dies with A1; the controller must carry the forward-fix-only semantics. |
| A4 | `git pull --rebase` / `git reset --hard` inside A1 **and by hand** | the deployed source tree | A1, or any shell | The reflog shows `pull --ff-only` (which A1 never issues) and two `reset` operations — so hand-driven deploys already happen alongside the script. |

## B. Scheduled triggers

| # | entrypoint | mutates | disposition |
|---|---|---|---|
| B1 | `/etc/cron.d/dotmac_erp_db_backup` — `0 18 * * *` root | runs `scripts/backup_erp_db.sh` from the **mutable checkout**; writes `/var/backups/db/`, uploads via rclone, prunes remote | Must become controller- or Foundation-declared. Executing a script out of a git worktree on a cron is the same defect class as the removed bind mounts. |
| B2 | `/etc/cron.d/staticroute` — `@reboot` root | rewrites the eth0 link route | Host networking, not ERP. Retain deliberately or retire deliberately — but name an owner. |
| B3 | `/etc/cron.d/certbot`, `certbot.timer` | TLS material under `/etc/letsencrypt`, reloads nginx | Out of scope for the executor retirement; in scope for "who can change what nginx serves". |
| B4 | `e2scrub_all`, `sysstat`, `man-db`, `apt-daily*`, `logrotate`, `fstrim`, `chkrootkit`, `motd-news`, `fwupd`, `dpkg-db-backup`, `update-notifier`, `snapd`, `ua-timer`, `apport` | OS maintenance | Stock Ubuntu. Named here so the census is exhaustive, not because they are ERP's. |

**No timer or cron deploys the application.** Verified: nothing outside B1 touches Docker, the checkout or the database.

## C. Host service units

| # | entrypoint | risk | disposition |
|---|---|---|---|
| C1 | **`dotmac-books.service`** — installed at `/etc/systemd/system/`, `disabled` + `inactive` | Runs `poetry run gunicorn app.main:app` **as root** from `/root/dotmac` with `EnvironmentFile=/root/dotmac/.env` and `Restart=always`. One `systemctl start` puts a second, uncontainerised ERP writer on the production database. `disabled` prevents boot activation, **not** a manual or dependency-driven start. | **Mask and remove.** Commands in `docs/runbooks/retire-latent-writers.md`. |
| C2 | `postgresql@16-main.service`, `redis-server.service`, `mariadb.service` | Loopback-only host services; `Requires=` of C1 is why the first two exist | Decide with C1. Not ERP's data plane — that is the `dotmac_pg_local` container. |
| C3 | `docker.service` | Every container's lifecycle | Retained; the controller runs through it. |
| C4 | `nginx` (host package) | What the public actually receives on 443 | The `/static/` location must move to `proxy_pass` before the next deploy; `deploy.sh` now refuses otherwise. |

## D. Container-level triggers

| # | entrypoint | note |
|---|---|---|
| D1 | `restart: unless-stopped` on app/worker/beat/redis | Docker restarts these across reboot with **no operator and no controller involved**. A controller that believes it owns the runtime must account for a restart it did not order. |
| D2 | `docker start dotmac_erp_app_dev` | The exited container object still exists and retains the production `DATABASE_URL`, the whole-checkout mount and a short-syntax `8002:8002` publish. Removing the compose service did **not** remove the object. |
| D3 | `dotmac_pg_local` | Hand-run, no compose project, 7.9 GB on a host bind mount. Nothing declarative manages the database. |
| D4 | `docker compose up/restart/exec/cp` by hand | Evidenced ~30 times in shell history, including code copied **into** the running container and ad-hoc Python using `allow_cross_org` to bypass tenant isolation. |

## E. Credentials conferring deployment authority

| # | entrypoint | note |
|---|---|---|
| E1 | **Eight unrestricted root SSH keys** | `seabone@hp-server`, `dotmac@proxmox-10.120.120.20`, `michaelayoade@macboos-MacBook-Pro.local`, `server-158`, `root@dotmac-db-primary`, `erp-dotmac`, `kayamus29@gmail.com`, and one with **no comment**. All ED25519. **Zero carry `from=`, `command=` or `restrict`** — every one is unrestricted root shell, i.e. a full deployment authority. `PermitRootLogin without-password`, `PasswordAuthentication no`. |
| E2 | `MIGRATION_DATABASE_URL` in the root-only `.env` | The `app_admin` DSN; the only credential `deploy.sh` requires beyond shell access. |
| E3 | `/root/dotmac/.env` (102 keys, mode 0600) | Holds every runtime secret. Carries **duplicate `OPENBAO_ADDR` and `OPENBAO_TOKEN`** keys — Compose takes the last, a naive parser takes the first, and they differ. |
| E4 | `/root/dotmac/.env.deploy-bak` | Rollback copy. Still contains the **pre-rotation** `POSTGRES_PASSWORD`, so a rollback reinstates a stale value. Dead credential, so not an exposure — a latent inconsistency. |
| E5 | `ralph` account with `/bin/bash` | Unattributed shell account; `/root/dotmac/.ralph_state` exists. Needs an owner or removal. |

## F. CI/CD workflows

**No ERP workflow deploys, SSHes, or mutates the production host.** Verified across all six workflows: the only mutating authority is `packages: write` in `ci.yml` and `release-hardened.yml`, which publishes the image to GHCR.

That is a **build** authority, not a **deploy** authority — and it is the reason step 3's "disable direct deployment workflows" is a no-op for ERP. It also means **E1 is the entire deployment authority surface**: production changes only through an unrestricted root shell.

`deployment-conformance.yml` calls a Starter reusable workflow pinned to an immutable commit; it is read-only against the repository.

## G. Inbound network triggers

Application-plane only. Payment and `dotmac_sub` webhooks (`ERP_SUB_WEBHOOK_SECRET`, `DOTMAC_SUB_WEBHOOK_SECRET`, `DOTMAC_ACADEMY_WEBHOOK_SECRET`) mutate business records, not deployment state. **No inbound endpoint can change what code runs.** Listed so the census is complete and so a future reviewer does not have to re-derive it.

Published sockets are governed by `tests/architecture/test_published_ports_are_declared.py`; port 8888 was contained on 2026-08-30 and is permanently forbidden there.

## H. Documentation that instructs manual mutation

`docs/deployment.md` and `README.md` instruct `scripts/deploy.sh` and manual
`docker compose` operations. `docs/PLATFORM_ADOPTION_LEDGER.md` and two
architecture documents reference the deploy path.

At step 6 these must be updated in the same change that deletes the executor —
documentation implying a retired path is still supported is how a retired path
comes back.

## I. Latent and quarantined

| # | item | note |
|---|---|---|
| I1 | `dotmac_demo_redis` | Ownerless, exited 2026-08-29, `dump.rdb` 261,457 bytes of undrained broker state. Removal must go through the managed path. |
| I2 | Orphan volumes | `dotmac_dotmac_books_db_data` (the legacy Books database), `acme_*`, `dotmac_mkt_certbot_*`, `dotmac_uploads_data`, `production-low-stock-hotfix_dotmac_logs`, four anonymous. |
| I3 | The 166-entry `scripts/` tree in the production checkout | Backfills, `cutover_database_ownership.py`, `bootstrap_database_roles.py`, ~30 mutating `.sql`. Each is one command from production. A rendered bundle carries none of them. |
| I4 | Out-of-band promtail | Running from `/opt/promtail/docker-compose.yml` on the mutable `:latest` tag, while ERP's own pinned promtail/vmagent sit unused behind the `observability` profile. |

---

## What this census changes about the retirement

1. **Deleting `scripts/deploy.sh` retires one of four executor families.** A1 is
   the named target, but C1, D2 and I3 each independently restore the ability to
   mutate production. Retiring A1 alone would be nominal.
2. **The deployment authority is SSH, not CI.** There is no deployment workflow
   to disable. Step 3's meaningful action for ERP is reducing E1 — eight
   unrestricted root keys — not revoking a CI credential.
3. **Two entrypoints execute code out of the mutable checkout**: B1 (the backup
   cron) and A1 itself. Both must be re-pointed at declared artifacts, or the
   controller's digest binding does not describe what actually runs.
4. **`restart: unless-stopped` (D1) means the runtime can change with no
   invocation at all.** A "zero legacy invocations" observation window has to
   distinguish an unordered Docker restart from an operator action.
