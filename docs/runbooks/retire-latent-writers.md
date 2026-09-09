# Runbook — retiring ERP's latent parallel writers

Two paths on `149.102.158.167` can put a **second ERP writer** on the production
database without going through the image, the migration gate, Compose, or any
deployment controller. Both were measured on 2026-08-30 and are recorded in
`docs/inventories/2026-08-30-erp-production-infrastructure-preflight.md` § 4 B.

The repository half of this work has landed. **The host half has not**, and is
listed here as exact commands so it can be executed by someone holding the
permission, and verified afterwards.

Do **not** remove `/root/dotmac/docker-compose.yml`. It is retained as rollback
evidence until the legacy executor is retired.

## 1. `dotmac-books.service` — host-only, still present

An installed systemd unit at `/etc/systemd/system/dotmac-books.service`,
currently `disabled` and `inactive`. It runs:

```
ExecStart=/root/.local/bin/poetry run gunicorn app.main:app -c gunicorn.conf.py
User=root
WorkingDirectory=/root/dotmac
EnvironmentFile=/root/dotmac/.env      # production DSN and every secret
Restart=always
Requires=postgresql.service redis-server.service
```

One `systemctl start dotmac-books` is all that stands between the current state
and an uncontainerised ERP application, running as root, writing to the
production database from whatever the checkout happens to contain. `disabled`
prevents boot activation; it does **not** prevent a manual or dependency-driven
start.

```
systemctl mask dotmac-books.service      # mask, not just disable: refuses manual start too
systemctl stop dotmac-books.service      # no-op if inactive, harmless
rm /etc/systemd/system/dotmac-books.service
systemctl daemon-reload
```

Verify:

```
systemctl status dotmac-books.service    # expect: could not be found / masked
```

Its `Requires=` is also the reason the host carries `postgresql.service` and
`redis-server.service` on loopback. Whether those host services are retired with
it is a separate decision — they are not ERP's data plane (that is the
`dotmac_pg_local` container) but removing them is out of scope here.

## 2. `app-dev` — Compose service removed; the container object remains

The service definition is gone from `docker-compose.yml`. The **exited container
object `dotmac_erp_app_dev` still exists on the host**, and it retains its
original configuration: the production `DATABASE_URL`, the whole checkout
mounted over `/app`, and a short-syntax `8002:8002` publish on both address
families that no `DOCKER-USER` rule covers.

Removing the service definition does not remove the object. A single
`docker start dotmac_erp_app_dev` would still revive it.

```
docker rm dotmac_erp_app_dev
```

Verify:

```
docker ps -a --filter name=dotmac_erp_app_dev   # expect: no rows
```

## 3. What the repository change already closed

- The four checkout bind mounts (`./static`, `./templates`, `./license`,
  `./gunicorn.conf.py`) are removed, so the image digest identifies the runtime.
- `scripts/sync-static.sh` now copies product static **out of the container**
  rather than out of `/root/dotmac/static/`. This mattered more than the mounts:
  nginx intercepts `/static/` before FastAPI and serves the rsynced copy, so the
  checkout — not the image — decided what browsers received.
- The `app-dev` service definition is gone, and with it the only remaining
  `build:` stanza — which closes the in-place host image build path
  (`docker compose up -d --build app`, run roughly twelve times historically).
- A `css-drift` CI job proves the committed stylesheet equals a fresh build, so
  serving the image's compiled asset is provably behaviour-preserving.

## 4. Ordering

The host steps above are safe in either order and are independent of a
deployment. They should be done **before** any controller cutover, because a
controller that owns deployment while these paths remain open does not actually
own the runtime.

---

## 5. Port 8888 — contained 2026-08-30, and now forbidden

### What it was

A root-owned `python3 -m http.server 8888 --bind 0.0.0.0`, PID 910619, working
directory `/root/.agent/diagrams`, started **2026-03-01 15:35:15** from an
interactive agent shell one-liner inside transient systemd scope
`session-5314.scope` (by then `active (abandoned)` — the SSH session had ended
five months earlier). It had been listening for **182 days**, fronted by a UFW
`ALLOW IN Anywhere` rule on **both** address families, serving a directory index
to the public internet as root.

No systemd unit, timer, cron entry or supervisor owned it. That is exactly why
nothing flagged it: **there was no declared roster for it to be absent from.**

### Containment receipt

| | |
|---|---|
| snapshot / change / verify | 2026-08-30T15:07:24Z / 15:08:00Z / 15:09:58Z |
| before, local | `LISTEN 0.0.0.0:8888` pid 910619 (**IPv4 only** — no `[::]:8888` listener ever existed) |
| before, external | `HTTP/1.0 200 OK`, `Server: SimpleHTTP/0.6 Python/3.12.3`, `Content-Length: 432` |
| rollback armed at | `/root/8888-containment-rollback.sh` (mode 700), written **before** any change |
| process | stopped with `TERM`; exited without needing `KILL` |
| rules removed | UFW `[5] 8888/tcp ALLOW IN Anywhere` and `[10] 8888/tcp (v6) ALLOW IN Anywhere (v6)` — **only** these two |
| after, local | no `:8888` listener; UFW reduced from 10 rules to 8, every remaining rule byte-identical |
| after, external v4 | closed, confirmed by two independent methods (`curl`, raw TCP connect) |
| positive control | **same target** `149.102.158.167:443` → HTTP 200 and TCP open, in the same run |
| directory | `/root/.agent/diagrams` **preserved** — 3 entries, mode 755, mtime unchanged; contents never read or copied |
| app health | `/health/ready` → 200 after the change |

**IPv6, stated precisely.** `ss` showed only `0.0.0.0:8888`; there was never a v6
listener, so v6 was already closed before this work and remains closed. That is
a **confirmation of pre-existing state, not a change made here.** The external v6
probe from the vantage used was *inconclusive* — the v6 positive control against
the same host also failed, so that vantage has no IPv6 path and proves nothing
either way. The on-host absence of a v6 listener is the evidence that stands.

Ports `9001`, `6391`, `8002`, `8003` and the stale `6443` UFW allow were **not
touched**.

### Why it is forbidden, not merely absent

Containment success is "the listener is gone and the external path is closed".
Durable success is "the controller refuses to recreate it". The second half is
`tests/architecture/test_published_ports_are_declared.py`, which:

- requires **every** published host port to have a named owner in
  `AUTHORIZED_HOST_PORTS`, so an unowned socket fails the build instead of
  living for six months;
- names 8888 permanently in `FORBIDDEN_HOST_PORTS`, because a port that carried
  an incident deserves a stated refusal — silent absence is indistinguishable
  from nobody having looked;
- requires every publish to bind an explicit **loopback** `host_ip` per address
  family, which is the invariant the incidents actually shared. Short syntax
  with no `host_ip` binds `0.0.0.0` *and* `[::]`, and on this fleet the IPv6 half
  cannot be filtered with `DOCKER-USER` at all — an inbound v6 connection is
  accepted by `docker-proxy` as a local process and traverses `INPUT`, while
  `DOCKER-USER` is only jumped from `FORWARD`;
- **plants** both an 8888 publish and an all-interfaces `8002:8002` publish and
  requires each to be detected, and separately requires a legitimate loopback
  publish *not* to be flagged. A guard that cannot fail is decorative, and one
  that fires on everything gets disabled by the first person it inconveniences.

### The half this repository cannot close

These tests check what the **repository declares**. They cannot see a process
someone starts by hand on the host — which is precisely how 8888 arose. Closing
that requires Observer alerting on any listening socket outside the authorized
runtime roster. **That is a `dotmac_observability` change and is not done here.**
Until it exists, a green build here must not be read as "the host has no
undeclared listener".
