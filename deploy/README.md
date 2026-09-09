# ERP deployment descriptor — the deploy path's image authority

`deploy/product.toml` is ERP's `ProductDeploymentSpec.v1`, implemented by the
published `dotmac-deployment-foundation==0.2.0a2` facility (Starter ADR-0070).
The package is exact-pinned as a dev dependency from Dotmac's private Forgejo
index. Its annotated release tag peels to Starter commit
`55750e104df3dd94b6f9f70bf8c8db53986394c7`. ERP's reusable conformance workflow
is pinned to that SAME immutable Starter commit, and deliberately so: 0.2.0a2's
only code change and that revision's workflow change are two halves of one fix
(complete non-root image inspection). The package's `image/audit.py` evaluates
`Config.User` from inspect evidence, and the workflow supplies that evidence by
running the filesystem collector as an inspection-only uid/gid 0 and refusing
the gate on a partial walk. Pinning a2 against the previous workflow revision
would pair a package expecting the new collector with a caller that does not
provide it, so these two pins move together.

The descriptor and `app/product_assembly.py` share the product identity
`dotmac-erp`. The canonical `deploy/product-manifest.json` binds the exact
composed module versions and persistence-plane selections, and the descriptor
pins the digest of those bytes. The published CLI deterministically renders:

- `deploy/rendered/docker-compose.yml`;
- `deploy/rendered/nginx/erp.dotmac.io.conf`;
- `deploy/rendered/otel-collector.yaml`;
- `deploy/rendered/alerts.rules.yml`.

## What CI proves

The normal CI workflow checks the canonical product manifest, builds the image
once, runs migrations and health checks against that image, and only on a
successful protected-main push transfers those same bytes to the publication
job. The publisher records a non-secret `image-release.json` containing the
source SHA and registry digest; it has no rebuild step.

`.github/workflows/deployment-conformance.yml` installs the exact facility
release with the repository's read-only `FORGEJO_READ_TOKEN`. It validates the
descriptor, runs the conformance kit, checks rendered bytes, and asks a real
Docker Compose engine to parse the rendered project. The architecture tests
also refuse a wrong package pin, mutable workflow coordinate, product identity
drift, product-manifest drift, Alembic-head drift, migration-owner material in
a runtime role, or an unrecorded conformance finding.

Run the same adapter checks with:

```bash
poetry run dotmac-deploy -f deploy/product.toml validate
poetry run dotmac-deploy -f deploy/product.toml render \
  --thresholds deploy/alerts/thresholds.json --check -o deploy/rendered
poetry run dotmac-deploy -f deploy/product.toml plan
poetry run dotmac-deploy -f deploy/product.toml preflight
poetry run dotmac-deploy -f deploy/product.toml backup
poetry run pytest tests/architecture/test_deployment_descriptor.py -q
```

Regenerate only with the exact pinned release:

```bash
poetry run dotmac-deploy -f deploy/product.toml render \
  --thresholds deploy/alerts/thresholds.json \
  -o deploy/rendered
```

Never hand-edit a rendered file.

## Current boundary

Both digests are real, the regression gate is armed, and the production deploy
path now consumes the image this descriptor declares.

**What this descriptor now owns.** `deploy/rendered/docker-compose.yml` is the
authority for WHICH image production runs. `scripts/deploy.sh` reads the image
reference out of that rendered file through `scripts/resolve_deploy_image.sh`,
which refuses anything that is not `sha256:<64 hex>`, exports it as
`APP_IMAGE`, and writes it into `.env`. The root `docker-compose.yml` declares
`${APP_IMAGE:?...}` with **no default**, so a bare `docker compose up -d` on a
host with no pin refuses to start rather than floating onto a mutable tag.

That closes a real gap rather than a theoretical one. ERP's publish lane has
long been the strongest in the fleet — it tags and pushes the exact tested
bytes, re-derives the OCI digest from `imagetools inspect --raw | sha256sum`
instead of trusting buildx's display, and persists `image-release.json`
binding digest to source SHA to manifest digest. None of it reached
production: the root compose read
`ghcr.io/michaelayoade/dotmac_erp:${ERP_IMAGE_TAG:-latest}` and the deploy
script pinned that variable to `sha-$(git rev-parse --short=7 HEAD)`. A
`sha-<short>` tag is reproducible-LOOKING and mutable in fact — a registry
pointer that can be repushed after verification. `ERP_IMAGE_TAG` is retired.

**What is still NOT the live path.** The rendered project is not yet the
production runtime topology, and this is a measured refusal rather than an
omission — see "Why the rendered topology is not yet the runtime" below.
`scripts/deploy.sh`, the root `docker-compose.yml`, the `Dockerfile` and the
backup scripts remain the executing deployment; only the image identity has
moved. The shared Foundation executor is likewise not in the path.

**The gate is armed.** `.github/workflows/deployment-conformance.yml` sets
`require-real-digests: true`. It should have been flipped the moment the
sentinel was replaced: an all-zero digest PARSES — it satisfies the
sha256 shape — so with the gate off, nothing at all prevented a silent
regression back to a placeholder. `deploy/product.toml` also declares
`environment = "production"`, which is what ERP is; it previously said
`reference` while serving live traffic at `https://erp.dotmac.io`, and that
value is projected into `deployment.environment` in the rendered collector
config, so the mislabel followed every span and metric.

The Employment Type authority revision is declared
`maintenance_required`, not online-compatible. An existing database may cross
that boundary only through:

```bash
MIGRATION_DATABASE_URL=<app_admin DSN> \
  ./scripts/deploy.sh --people-employment-type-activation
```

That explicit mode refuses `--quick`, proves `app-dev` is absent, drains every
known app/worker/Beat Compose container (including one-offs), passes the
one-revision activation opt-in, and never restores the previous legacy-writing
image after the migration commits. If the migration container fails after an
ambiguous transport boundary, rollback is allowed only when a fresh database
probe sees the module-owned authority relation `mod_people.employment_types`
(its positive control) and finds neither the activation revision nor the
activation's own `hr.enforce_employment_type_projection()` fence. The new
app, worker ping, and Beat heartbeat must all be admitted before the deployment
is complete. This path is independent of the Foundation executor: the
descriptor and rendered assets remain reference evidence only.

The descriptor makes several legacy defects executable rather than silently
accepted:

- app liveness is `/health/live`; dependency-aware readiness is
  `/health/ready`, never legacy always-200 `/health`;
- no runtime role may receive `MIGRATION_DATABASE_URL`;
- images are digest-shaped, never tags — and as of this slice that is
  enforced on the live deploy path, not only inside this descriptor;
- worker liveness uses a Celery ping and Beat freshness uses a declared tick
  command instead of invented HTTP probes;
- migration ordering, dependency waits, resource limits, immutable static
  delivery, verified backup policy and telemetry identity are rendered from
  one descriptor;
- unbacked alert definitions are omitted rather than emitted as rules that no
  producer can satisfy.

## Why the rendered topology is not yet the runtime

Moving the deploy path's IMAGE onto the rendered file is safe and done. Moving
the whole production topology onto it is not, and the blockers are specific
and checkable rather than a matter of caution:

- **The rendered project could not start.** Every role's `command` began with
  `/app/entrypoint-monitoring.sh`, a boot-time `pip install` wrapper that was
  DELETED with the audited runtime image (26753cde). The Dockerfile's runtime
  stage COPYs `scripts/` as a three-file allowlist that never included it, and
  the file no longer exists in the repository at all, so all three services
  would have failed to exec on first start. This has been fixed here — the
  descriptor now declares the Dockerfile's real `CMD` — and it is why the
  hardening guard was extended to cover the descriptor and `deploy/rendered`;
  it had been scoped to the root `docker-compose.yml` alone, which is why this
  survived. Removing the wrapper also dissolves the `read_only_root` tension
  the descriptor used to carry as a KNOWN TENSION.
- **No `env_file`.** The rendered services declare only the materials the
  descriptor names. The running app also reads `APP_URL`, `DOTMAC_DEV_MODE`,
  `DEFAULT_ORGANIZATION_ID`, `TRUSTED_PROXY_IPS`, `GUNICORN_WORKERS`,
  `APP_VERSION` and everything else `env_file: - .env` supplies today.
- **Port and vhost.** The rendered app publishes `127.0.0.1:8002`; production
  publishes `8003` on both loopback families and the live nginx upstream is
  `127.0.0.1:8003`. Switching the compose file without the vhost is an outage.
- **No container names.** `scripts/deploy.sh` inspects `dotmac_erp_app`,
  `dotmac_erp_worker` and `dotmac_erp_beat` by name; the rendered project
  declares none.
- **No observability agents.** `promtail` and `vmagent` exist only in the root
  compose, so a swap would silently stop shipping logs and metrics.

Each is a rendered-topology gap, not an image-identity gap, which is why the
image moved now and the topology did not.

## Gates before production execution

1. ~~Use protected-main's `image-release.json` to replace the image sentinel.~~
   Done: the descriptor binds the real registry digest and the deploy path
   consumes it.
2. Run the hardened image audit in strict mode against that exact image.
3. Census the production Docker/Compose versions and prove the rendered project
   on that supported engine.
4. Connect and prove the metrics/OTLP evaluator, alert routing, firing and
   recovery chain.
5. Prove migration-owner separation, backup restore, readiness, worker and
   scheduler health, warm ingress handoff, rollback and drift refusal on an ERP
   candidate.
6. Compare the shared executor with the current production path, switch only
   after parity, then retire the displaced local engine.

Until those gates pass, operators continue to use `scripts/deploy.sh`. The
rendered assets remain conformance evidence and are not a second production
command path — with one deliberate exception, which is the point of this
slice: `deploy/rendered/docker-compose.yml` is the authority for the image
reference `scripts/deploy.sh` deploys.
