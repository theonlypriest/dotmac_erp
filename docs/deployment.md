# Deployment

## Environment

Use `.env.example` as a starting point. Required values include:

- `DATABASE_URL`
- `JWT_SECRET`
- `TOTP_ENCRYPTION_KEY`

## Docker Compose

For local containerized setup using the published runtime image:

```bash
docker compose up -d app worker beat redis
```

To build the source-backed development profile, point Compose at an existing
local file containing the approved Forgejo read token. The file is a BuildKit
secret and its value never belongs in `.env`, Compose, or Git:

```bash
FORGEJO_TOKEN_FILE=/absolute/path/to/forgejo-read-token \
  docker compose --profile dev up -d --build app-dev redis
```

Apply migrations after containers are up:

```bash
docker compose exec app alembic upgrade heads
```

## Workers

Start Celery workers and scheduler in separate processes/containers:

```bash
poetry run celery -A app.celery_app worker -l info
poetry run celery -A app.celery_app beat -l info
```

## Quick Deploy

For production deployments, use the deploy script which handles everything:

```bash
./scripts/deploy.sh
```

This script will:
- Back up the database (pre-migration)
- Pull latest changes and the container image
- Enforce the stable Docker Compose project name `dotmac`, including when the
  script runs from a revision-named release worktree. A conflicting
  `COMPOSE_PROJECT_NAME` is rejected before backup or container changes.
- **Pin the image by digest.** `APP_IMAGE` is resolved from
  `deploy/rendered/docker-compose.yml` — rendered from `deploy/product.toml`,
  which carries the OCI digest protected-main CI resolved for the image it
  built and tested — and `scripts/resolve_deploy_image.sh` refuses anything
  that is not `sha256:<64 hex>`. `app`/`worker`/`beat` therefore all run one
  artifact that is identified by its bytes, not by a registry pointer
- Run database migrations (`alembic upgrade heads`) on the pulled image
- Recreate the containers on the pinned image
- Health-gate the app and admit the worker ping plus Beat heartbeat. Ordinary
  compatible deployments auto-roll back on failure — code resets to the
  previous commit and `APP_IMAGE` returns to the previously-running image's
  digest (read from its RepoDigests, never from the reference that started it).
  The explicit Employment Type authority activation is the exception: after
  its migration commits, every later failure is forward-fix-only because the
  previous image contains retired legacy writers. See `deploy/README.md`.
- Sync static files to nginx

The pinned image is controlled by `APP_IMAGE` (see `.env.example`); the deploy
script writes it automatically. `docker-compose.yml` declares it with no
default, so a bare `docker compose up -d` on a host whose `.env` has no pin
refuses to start instead of floating onto `:latest`.

To redeploy an exact earlier release, pass its digest to the deploy script —
which holds it to the same gate as the rendered file:

```bash
MIGRATION_DATABASE_URL=<app_admin DSN> ./scripts/deploy.sh sha256:<64 hex>
```

A tag is not an accepted selector anywhere on this path. `sha-abc1234` looks
reproducible but is a mutable registry pointer, and nothing binds it to the
bytes CI tested; the digest does.

## Static Assets

Rebuild CSS when templates or Tailwind config change:

```bash
npm run build:css
```

Static files are served **from the application image**. nginx proxies `/static/`
to the app rather than serving a filesystem copy.

This changed on 2026-08-30. nginx previously served `/var/www/dotmac/static/`,
populated by `scripts/sync-static.sh` from the checkout — so the working tree,
not the image, decided what browsers received. Production ran for an unknown
period on a stylesheet 198 insertions behind its own image, missing dark-mode
and accent utilities. The sync and its systemd timer are retired; `deploy.sh`
refuses to deploy while nginx still serves `/static/` from disk, because that
copy is now frozen and nginx sets 30-day `immutable` cache headers on it.

The expected digest of the served tree is `deploy/static-tree-digest.json`,
checked in CI against what the running container actually serves over HTTP.
To manually sync static files:

```bash
rsync -av --delete static/ /var/www/dotmac/static/
chown -R www-data:www-data /var/www/dotmac/static/
```
