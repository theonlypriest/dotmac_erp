#!/bin/bash
# Deploy DotMac ERP — hardened: backup -> pull -> migrate -> recreate ->
# health gate -> ordinary auto-rollback or cutover forward repair.
#
# Usage:
#   MIGRATION_DATABASE_URL=<app_admin DSN> ./scripts/deploy.sh
#   MIGRATION_DATABASE_URL=<app_admin DSN> ./scripts/deploy.sh --quick
#   MIGRATION_DATABASE_URL=<app_admin DSN> ./scripts/deploy.sh \
#       --people-employment-type-activation
#   MIGRATION_DATABASE_URL=<app_admin DSN> ./scripts/deploy.sh sha256:<64 hex>
#   SKIP_BACKUP=1 ./scripts/deploy.sh   # skip the pre-migration DB backup (NOT recommended)
#
# `MIGRATION_DATABASE_URL` comes from the approved secret source and is passed
# only to one-off preflight/migration containers. Runtime services keep only
# `DATABASE_URL`; Alembic never falls back to it.
#
# Notes on erp's deploy model: application code, migrations and dependencies all
# ship inside the tested image. The checkout supplies Compose configuration,
# operator scripts and the existing static/template/Gunicorn read-only overlays;
# it does not replace application Python or migrations. A real deploy therefore
# pulls the immutable image before running its migrations.
#
# Image pinning: app/worker/beat run ${APP_IMAGE}, which docker-compose.yml
# declares with NO default, so an unset value refuses to start.
#
# APP_IMAGE is resolved from `deploy/rendered/docker-compose.yml` — the asset
# the pinned dotmac-deployment-foundation renders from deploy/product.toml,
# whose image digest is the one protected-main CI resolved for the image it
# built and tested. `scripts/resolve_deploy_image.sh` reads it and REFUSES
# anything that is not `sha256:<64 hex>`, so this deploy path structurally
# cannot consume a mutable tag.
#
# This replaced ERP_IMAGE_TAG, which was pinned to `sha-<short 7>` of the
# deployed commit. That looked reproducible and was not: a tag is a registry
# pointer that can be repushed after it was verified, and nothing compared it
# to the bytes CI tested. ERP's publish lane had been resolving and recording
# the real OCI digest the whole time; production simply never consumed it.
#
# An explicit `sha256:<64 hex>` argument overrides the rendered file, for
# redeploying an exact earlier release. It is held to the identical gate.
# --quick keeps the image the app container is already running.
#
# On an ordinary failed health gate the code is reset to the previous commit AND
# the image tag is restored to the previously-running one, then the containers
# are recreated. Migrations are NOT auto-reverted, so ordinary revisions must be
# backward-compatible with the previous release.
#
# Employment Type authority activation is deliberately different. Its explicit
# mode drains every old application writer before the forward-only migration.
# Once that migration commits the previous image is no longer a valid rollback
# target, so failures stop for an operator-led forward fix instead of restoring
# split ownership.

set -euo pipefail

quick_deploy=0
people_employment_type_activation=0
requested_image_selector=""
for argument in "$@"; do
    case "$argument" in
        --quick)
            quick_deploy=1
            ;;
        --people-employment-type-activation)
            people_employment_type_activation=1
            ;;
        sha256:*)
            # An exact digest, for redeploying a known earlier release without
            # editing the descriptor. Deliberately the ONLY positional form
            # accepted: `sha-abc1234` and `latest` fall through to the error
            # below rather than being resolved as tags, because a selector this
            # script accepts is a selector production can run.
            requested_image_selector="$argument"
            ;;
        *)
            echo "ERROR: unknown deploy argument: $argument" >&2
            echo "       An explicit image selector must be sha256:<64 hex>." >&2
            exit 2
            ;;
    esac
done
if [[ "$quick_deploy" == "1" && "$people_employment_type_activation" == "1" ]]; then
    echo "ERROR: Employment Type activation cannot use --quick; the new image is required." >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-150}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8003/health/ready}"
RUNTIME_ADMISSION_TIMEOUT="${RUNTIME_ADMISSION_TIMEOUT:-90}"
DEPLOY_COMPOSE_PROJECT_NAME="dotmac"
# One knob for the image repository, shared by the digest gate, the rollback
# target resolution and the retention pass below, so a fork or a registry move
# is a single edit rather than three that can disagree.
IMAGE_REPOSITORY="${DOCKER_IMAGE_REPOSITORY:-ghcr.io/michaelayoade/dotmac_erp}"
export DOCKER_IMAGE_REPOSITORY="$IMAGE_REPOSITORY"
# The rendered Compose project is the authority for WHICH image this deploy
# runs. It is rendered from deploy/product.toml by the exact-pinned
# dotmac-deployment-foundation and byte-checked in CI, and it carries the
# digest protected-main resolved for the image it built and tested.
RENDERED_COMPOSE="${RENDERED_COMPOSE:-deploy/rendered/docker-compose.yml}"

# Production uses fixed container names (dotmac_erp_app, dotmac_erp_redis, etc.).
# Compose otherwise derives its project name from the current directory, which
# changes for revision-named release worktrees and causes container-name
# conflicts before migrations can run. Fail closed on a conflicting caller
# value, then export the stable name for every compose command and rollback.
if [[ -n "${COMPOSE_PROJECT_NAME:-}" && \
      "$COMPOSE_PROJECT_NAME" != "$DEPLOY_COMPOSE_PROJECT_NAME" ]]; then
    echo "ERROR: COMPOSE_PROJECT_NAME must be '$DEPLOY_COMPOSE_PROJECT_NAME' for production deploys (got '$COMPOSE_PROJECT_NAME')." >&2
    exit 2
fi
export COMPOSE_PROJECT_NAME="$DEPLOY_COMPOSE_PROJECT_NAME"

running_compose_service_containers() {
    local service="$1"
    docker ps \
        --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
        --filter "label=com.docker.compose.service=${service}" \
        --format '{{.Names}}'
}

cd "$PROJECT_DIR"
PREV_SHA="$(git rev-parse HEAD)"

if [[ -z "${MIGRATION_DATABASE_URL:-}" ]]; then
    echo "ERROR: MIGRATION_DATABASE_URL is required and must connect as the" >&2
    echo "non-superuser app_admin role. Alembic never uses DATABASE_URL." >&2
    exit 2
fi

# .env carries two values that merely RESTATE facts owned by the deployed
# release: APP_IMAGE (the immutable image) and APP_VERSION (pyproject's
# version, which docker-compose.yml already defaults to). Nothing kept them in
# step, so both drifted — and .env wins over the compose default, so the drift
# is what actually runs:
#
#   - APP_IMAGE's predecessor, ERP_IMAGE_TAG, sat 5 weeks behind the running
#     image, so a bare `docker compose up -d` by anyone not using this script
#     would have silently DOWNGRADED production. Worse, it defaulted to
#     `latest`, so an ABSENT key floated instead of failing. APP_IMAGE has no
#     default: absence now refuses.
#   - APP_VERSION sat two releases behind, so the app misreported its own
#     version — which is how a deploy gap got mis-sized from a stale note.
#
# This script pins APP_IMAGE for its own compose calls via `export`, which is
# why the drift stayed invisible to the deploy path. Making the deploy the
# single writer of both keys is what stops it recurring.
ENV_FILE="$PROJECT_DIR/.env"
ENV_BACKUP="${ENV_FILE}.deploy-bak"
env_synced=0

# Set key=value in .env, replacing the first existing occurrence or appending.
# Writes via a temp file so an interrupted deploy cannot leave a half-written
# .env, and preserves the original mode (it is 0600 and holds secrets).
set_env_var() {
    local key="$1" value="$2" tmp
    [[ -f "$ENV_FILE" ]] || return 0
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        awk -v k="$key" -v v="$value" '
            $0 ~ "^" k "=" && !seen { print k "=" v; seen = 1; next }
            { print }
        ' "$ENV_FILE" > "$tmp"
    else
        cat "$ENV_FILE" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
}

# Read one key back out of .env. The counterpart of set_env_var above, and the
# fallback source for the rollback target when the app container is not running.
env_value() {
    local key="$1"
    [[ -f "$ENV_FILE" ]] || return 0
    awk -F= -v k="$key" '$1 == k { sub("^" k "=", ""); print; exit }' "$ENV_FILE"
}

# The IMMUTABLE name of the image the app container is currently running.
#
# Resolved from the container's image ID via RepoDigests, NOT from its
# `Config.Image` string. That string is whatever reference started the
# container — historically a mutable `sha-<short>` tag — and restoring it on
# rollback would put production back on a tag, quietly reopening the hole this
# script now closes. RepoDigests is the immutable name of the same bytes, so
# rollback stays exact AND stays digest-only.
running_app_image_digest() {
    local image_id
    image_id="$(docker inspect --format '{{.Image}}' dotmac_erp_app 2>/dev/null || true)"
    [[ -n "$image_id" ]] || return 0
    docker image inspect "$image_id" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null |
        grep -E "^${IMAGE_REPOSITORY}@sha256:[0-9a-f]{64}$" |
        head -n 1 || true
}

PREV_IMAGE="$(running_app_image_digest)"
if [[ -z "$PREV_IMAGE" ]]; then
    # No running container, or an image with no registry digest (a local
    # build). Fall back to the pin this script itself wrote last time.
    PREV_IMAGE="$(env_value APP_IMAGE)"
fi
if [[ -n "$PREV_IMAGE" ]] && \
   ! "$SCRIPT_DIR/resolve_deploy_image.sh" --reference "$PREV_IMAGE" >/dev/null 2>&1
then
    # A rollback target that is not digest-shaped is not a rollback target.
    # Said out loud and discarded, rather than carried silently to the point
    # where a failed deploy would restore a mutable reference.
    echo "NOTE: the previous image reference '${PREV_IMAGE}' is not an immutable" >&2
    echo "      digest, so automatic IMAGE rollback is unavailable for this run." >&2
    echo "      Code rollback is unaffected. This is expected exactly once, on" >&2
    echo "      the first deploy after the ERP_IMAGE_TAG retirement." >&2
    PREV_IMAGE=""
fi
if [[ -n "$PREV_IMAGE" ]]; then
    export APP_IMAGE="$PREV_IMAGE"
elif [[ "$quick_deploy" == "1" ]]; then
    # --quick never resolves a new image, so with no previous one there is
    # nothing to run. Refuse here rather than letting compose fail on
    # ${APP_IMAGE:?} several irreversible steps later.
    echo "ERROR: --quick has no image to keep: no running app container carries" >&2
    echo "       a registry digest and .env has no immutable APP_IMAGE pin." >&2
    echo "       Run a full deploy, or pass an explicit sha256:<64 hex>." >&2
    exit 2
fi

echo "=== DotMac ERP Deploy ==="
echo "Project: $PROJECT_DIR   (compose: ${COMPOSE_PROJECT_NAME}, current: ${PREV_SHA:0:12})"
echo "Running image: ${PREV_IMAGE:-<none pinned>}"
echo ""

# `app-dev` is excluded from the production topology only by a Compose profile.
# Turn that stated premise into a live check before backup, pull, or any runtime
# drain: its bind-mounted old application can write the same database.
if [[ "$people_employment_type_activation" == "1" ]]; then
    if ! running_app_dev="$(running_compose_service_containers app-dev)"; then
        echo "ERROR: could not verify the app-dev cutover exclusion." >&2
        exit 2
    fi
    if [[ -n "$running_app_dev" ]]; then
        echo "ERROR: Employment Type activation refuses while app-dev is running:" >&2
        echo "$running_app_dev" >&2
        exit 2
    fi
fi

rollback() {
    echo "!! Rolling back code to ${PREV_SHA:0:12} and image to ${PREV_IMAGE:-<unavailable>}..."
    git reset --hard "$PREV_SHA" || true
    # Undo the .env pins written for the failed deploy, then point APP_IMAGE at
    # the image actually being restored. Restoring the backup alone would
    # reinstate whatever drift was there before, which is the very landmine
    # this change exists to remove.
    if [[ -n "$PREV_IMAGE" ]]; then
        export APP_IMAGE="$PREV_IMAGE"
        if [[ "$env_synced" == "1" && -f "$ENV_BACKUP" ]]; then
            cp -a "$ENV_BACKUP" "$ENV_FILE"
            set_env_var APP_IMAGE "$PREV_IMAGE"
            echo "!! Reverted .env pins to the restored image (${PREV_IMAGE})."
        fi
    else
        # No digest-shaped rollback target. The compose path below will refuse
        # on ${APP_IMAGE:?} rather than guess, and the container-object restart
        # fallback brings the previously-running container back untouched —
        # which is the honest outcome, not a silent downgrade to a floating tag.
        echo "!! No immutable previous image is known; the running container is" >&2
        echo "!! restarted in place and the image pin is left alone." >&2
        if [[ "$env_synced" == "1" && -f "$ENV_BACKUP" ]]; then
            cp -a "$ENV_BACKUP" "$ENV_FILE"
        fi
    fi
    docker compose up -d app worker beat || { docker stop dotmac_erp_app || true; docker start dotmac_erp_app || true; }
    echo "!! Rolled back. NOTE: DB migrations were NOT reverted — restore from the"
    echo "!! pre-migration backup if the new revisions are not backward-compatible."
}

forward_fix_only=0
handle_deploy_failure() {
    if [[ "$forward_fix_only" == "1" ]]; then
        echo "!! FORWARD-FIX-ONLY: Employment Type activation committed or its outcome is ambiguous." >&2
        echo "!! The previous image remains stopped because it contains legacy writers." >&2
        echo "!! Repair the new release and resume it; do not restore the old image." >&2
        return
    fi
    rollback
}

resolve_ambiguous_activation_failure() {
    local probe_status

    # Alembic process failure is not proof that its transaction rolled back: a
    # container/transport failure may be reported after PostgreSQL committed.
    # Default to the safe direction. Rollback is permitted only when a fresh
    # read positively finds the pre-activation fence and no activation revision.
    forward_fix_only=1
    if docker compose run --rm -e MIGRATION_DATABASE_URL app \
        python scripts/probe_people_employment_type_activation.py
    then
        echo "!! Activation committed despite the migration process failure." >&2
        return
    else
        probe_status=$?
    fi

    if [[ "$probe_status" == "3" ]]; then
        forward_fix_only=0
        echo "!! Activation is positively absent; the previous image is rollback-safe." >&2
    else
        echo "!! Activation outcome is ambiguous; legacy writers remain stopped." >&2
    fi
}

wait_for_worker_admission() {
    local attempt=0 deadline=$((SECONDS + RUNTIME_ADMISSION_TIMEOUT))
    while (( SECONDS < deadline )); do
        attempt=$((attempt + 1))
        if [[ "$(docker inspect --format '{{.State.Running}}' dotmac_erp_worker 2>/dev/null || true)" == "true" ]] && \
           docker compose exec -T worker sh -c \
               'celery -A app.celery_app inspect ping --timeout=5 --destination "celery@$(hostname)"' \
               >/dev/null 2>&1
        then
            echo "  Worker admitted after ${attempt}s"
            return 0
        fi
        sleep 1
    done
    echo "  ERROR: Worker did not remain operational within ${RUNTIME_ADMISSION_TIMEOUT}s." >&2
    return 1
}

wait_for_beat_admission() {
    local attempt=0 deadline=$((SECONDS + RUNTIME_ADMISSION_TIMEOUT))
    while (( SECONDS < deadline )); do
        attempt=$((attempt + 1))
        if [[ "$(docker inspect --format '{{.State.Running}}' dotmac_erp_beat 2>/dev/null || true)" == "true" ]] && \
           docker compose exec -T beat sh -c '
               heartbeat=/tmp/dotmac-erp-beat-heartbeat
               test -f "$heartbeat" || exit 1
               now=$(date +%s)
               updated=$(stat -c %Y "$heartbeat")
               age=$((now - updated))
               test "$age" -ge 0 && test "$age" -le 120
           ' >/dev/null 2>&1
        then
            echo "  Beat admitted after ${attempt}s"
            return 0
        fi
        sleep 1
    done
    echo "  ERROR: Beat did not publish a fresh heartbeat within ${RUNTIME_ADMISSION_TIMEOUT}s." >&2
    return 1
}

# Preflight: nginx must PROXY /static/, not serve it from disk.
#
# The static-asset sync is GONE, deliberately. It rsynced /root/dotmac/static/
# into the nginx web root, and nginx serves /static/ ahead of the application --
# so the CHECKOUT, not the image, decided what browsers received. Production ran
# for an unknown period on a stylesheet 198 insertions behind its own image,
# missing dark-mode and accent utilities, while the image compiled the correct
# one that was never used. The retired unit's own README predicted the shape:
# "a missed sync can serve stale JS/CSS for up to 30 days" under immutable cache
# headers. The fix is to stop copying, not to copy more reliably.
#
# With the sync retired, a filesystem-served /static/ is now FROZEN and would go
# stale silently under those same 30-day immutable headers. Refuse here, before
# the backup, the pull or any container change, so a refusal costs nothing and
# leaves nothing half-done.
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/erp.dotmac.io}"
if [[ -r "$NGINX_SITE" ]]; then
    if grep -qE '^[[:space:]]*(alias|root)[[:space:]]+/var/www/dotmac/static' "$NGINX_SITE"; then
        echo "ERROR: $NGINX_SITE still serves /static/ from the filesystem." >&2
        echo "       The sync that kept that directory fresh has been retired, so" >&2
        echo "       it would now serve a frozen tree under 30-day immutable" >&2
        echo "       headers. Switch the /static/ location to proxy_pass to the" >&2
        echo "       app (see deploy/rendered/nginx/erp.dotmac.io.conf), reload" >&2
        echo "       nginx, then redeploy." >&2
        exit 2
    fi
    echo "-> nginx /static/ is proxied, not filesystem-served."
else
    echo "-> nginx site not readable at $NGINX_SITE; skipping the static check."
fi

# Step 1: pre-migration DB backup (SKIP_BACKUP=1 to skip)
if [[ "${SKIP_BACKUP:-0}" != "1" ]]; then
    echo "→ Backing up database (SKIP_BACKUP=1 to skip)..."
    bash "$SCRIPT_DIR/backup_erp_db.sh"
    echo ""
fi

# Step 2: pull the deployment checkout + the image carrying app, migrations and
# dependencies, then pin it to the new commit's immutable sha-<short> tag.
if [[ "$quick_deploy" != "1" ]]; then
    echo "→ Pulling latest code + image..."
    git pull --rebase

    # WHICH image this deploy runs is decided here, and it is the only place.
    #
    # The rendered Compose project is read AFTER the pull, deliberately: it is
    # a tracked file, so the freshly-pulled revision is what names the release
    # being deployed. `resolve_deploy_image.sh` refuses anything that is not
    # `sha256:<64 hex>`, so a regression that reintroduced a tag into the
    # rendered file — or into an operator's argument — stops the deploy before
    # the backup is even consulted for a pull.
    if [[ -n "$requested_image_selector" ]]; then
        NEW_IMAGE="$("$SCRIPT_DIR/resolve_deploy_image.sh" \
            --reference "$requested_image_selector")"
        echo "  Image selector: explicit operator digest"
    else
        NEW_IMAGE="$("$SCRIPT_DIR/resolve_deploy_image.sh" \
            --compose "$PROJECT_DIR/$RENDERED_COMPOSE")"
        echo "  Image selector: ${RENDERED_COMPOSE}"
    fi
    export APP_IMAGE="$NEW_IMAGE"
    echo "  Pinning image: ${APP_IMAGE}"
    echo "  Rollback target: ${PREV_IMAGE:-<none; image rollback unavailable>}"

    # Persist both pins into .env from the freshly-pulled commit. This runs
    # BEFORE migrate/recreate deliberately: APP_VERSION only reaches the app
    # container if .env is correct at `docker compose up` time, so writing it
    # after the health gate would leave the running container reporting the
    # previous release until the *next* deploy.
    if [[ -f "$ENV_FILE" ]]; then
        cp -a "$ENV_FILE" "$ENV_BACKUP"
        env_synced=1
        NEW_APP_VERSION="$(awk -F'"' '/^version = "/ { print $2; exit }' pyproject.toml)"
        set_env_var APP_IMAGE "$NEW_IMAGE"
        if [[ -n "$NEW_APP_VERSION" ]]; then
            set_env_var APP_VERSION "$NEW_APP_VERSION"
        fi
        echo "  .env synced: APP_IMAGE=${NEW_IMAGE} APP_VERSION=${NEW_APP_VERSION:-<unchanged>}"
    fi

    docker compose pull app worker beat

    # The digest names bytes; this proves those bytes are the release the
    # descriptor says they are. The image carries
    # `org.opencontainers.image.revision` as a real Config label, set at build
    # time and asserted by ci.yml's "Verify tested image provenance labels", so
    # comparing it with the descriptor's `source_revision` closes the pairing
    # locally, on the host, against the artifact actually pulled — rather than
    # trusting that two files in a checkout still describe each other.
    DESCRIPTOR_REVISION="$(awk -F'"' '/^source_revision = "/ { print $2; exit }' \
        deploy/product.toml)"
    PULLED_REVISION="$(docker image inspect "$APP_IMAGE" \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        2>/dev/null || true)"
    if [[ -z "$DESCRIPTOR_REVISION" || -z "$PULLED_REVISION" ]]; then
        echo "ERROR: could not read the image revision pairing" >&2
        echo "       (descriptor='${DESCRIPTOR_REVISION:-<empty>}'," >&2
        echo "        image='${PULLED_REVISION:-<empty>}')." >&2
        echo "       An unreadable pairing is an unverified one; refusing." >&2
        exit 2
    fi
    if [[ "$PULLED_REVISION" != "$DESCRIPTOR_REVISION" ]]; then
        echo "IMAGE INTEGRITY FAILURE: ${APP_IMAGE} was built from" >&2
        echo "  ${PULLED_REVISION}" >&2
        echo "but deploy/product.toml declares source_revision" >&2
        echo "  ${DESCRIPTOR_REVISION}" >&2
        echo "The digest and the revision are one pairing; a revision naming" >&2
        echo "bytes that are not the deployed bytes identifies nothing." >&2
        exit 2
    fi
    echo "  Image revision verified: ${PULLED_REVISION:0:12}"
    echo ""
fi

# From here a failure triggers an automatic rollback.
trap 'echo "Deploy FAILED"; handle_deploy_failure; exit 1' ERR

# Step 3a: PREFLIGHT migration identity, role posture and ownership before DDL.
#
# `20260814_database_roles` fails closed when `app_admin`, `app_user` or
# `platform_api` is missing or wrong-shaped. Discovering that mid-chain means a
# half-applied upgrade and an automatic rollback; discovering it here costs
# nothing. This deliberately does NOT create the roles: creation needs superuser
# or CREATEROLE, and the deploy path must never hold those. Run the explicitly
# privileged bootstrap once, as an operator, then re-run the deploy.
echo "→ Preflight: migration executor contract..."
if ! docker compose run --rm \
    -e MIGRATION_DATABASE_URL app \
    python scripts/bootstrap_database_roles.py --verify-only
then
    echo ""
    echo "DEPLOY STOPPED: the migration identity, role posture, or database" >&2
    echo "ownership contract is unsatisfied. No migration was attempted." >&2
    echo "" >&2
    echo "If roles are missing or wrong-shaped, run the explicitly privileged" >&2
    echo "bootstrap once with superuser or CREATEROLE credentials:" >&2
    echo "" >&2
    echo "  BOOTSTRAP_DATABASE_URL=postgresql://<superuser>@<host>/<db> \\" >&2
    echo "      python scripts/bootstrap_database_roles.py --dry-run" >&2
    echo "  # review, then drop --dry-run" >&2
    echo "" >&2
    echo "That script never sets passwords or transfers object ownership." >&2
    echo "For an existing database whose objects have another owner, complete" >&2
    echo "a separately reviewed ownership cutover before re-running deploy." >&2
    exit 1
fi
echo "  roles present and correctly shaped"
echo ""

# Step 3b: apply migrations on the freshly-pulled image (multi-head safe — erp has
# hit multi-head states, so `heads` (plural), never `head`).
activation_env=()
if [[ "$people_employment_type_activation" == "1" ]]; then
    echo "→ Draining old Employment Type writers before authority activation..."
    docker compose stop app worker beat
    remaining_legacy_runtimes=""
    for service in app app-dev worker beat; do
        if ! running_service="$(running_compose_service_containers "$service")"; then
            echo "ERROR: could not prove the ${service} runtime drain." >&2
            false
        fi
        if [[ -n "$running_service" ]]; then
            remaining_legacy_runtimes+="${service}: ${running_service}"$'\n'
        fi
    done
    if [[ -n "$remaining_legacy_runtimes" ]]; then
        echo "ERROR: legacy-capable Compose containers remain after the drain:" >&2
        echo "$remaining_legacy_runtimes" >&2
        false
    fi
    activation_env+=(-e PEOPLE_EMPLOYMENT_TYPE_ACTIVATION=1)
    echo "  old app, worker and beat stopped"
    echo ""
fi
echo "→ Applying migrations (alembic upgrade heads)..."
# `alembic upgrade heads` stays on its own line: the credential-asymmetry
# detector in tests/architecture/test_database_role_contract.py anchors on
# the executed command and walks BACK to its `docker compose run`, so an
# inlined command would make that check unable to find this step at all.
if docker compose run --rm -e MIGRATION_DATABASE_URL "${activation_env[@]+"${activation_env[@]}"}" app \
    alembic upgrade heads
then
    if [[ "$people_employment_type_activation" == "1" ]]; then
        # From this instant the previous image's legacy writers are incompatible
        # with the database authority boundary. Every later failure is repaired
        # forward; automatic code/image rollback would recreate split ownership.
        forward_fix_only=1
    fi
else
    if [[ "$people_employment_type_activation" == "1" ]]; then
        resolve_ambiguous_activation_failure
    fi
    trap - ERR
    handle_deploy_failure
    exit 1
fi
echo ""

# Step 3c: ADMIT the RUNTIME connection, after the DDL and before the app runs.
#
# This is the one one-off in this script that is deliberately NOT given
# `-e MIGRATION_DATABASE_URL`, and the omission is the entire point. Every
# other step here verifies the migration executor; this one verifies the
# credential the APPLICATION will serve requests on, which the one-off inherits
# from the `app` service's own `environment:` block (`DATABASE_URL:
# ${DATABASE_URL}`, itself read from `env_file: - .env`). Passing the migration
# URL would re-verify `app_admin` — a role that is BYPASSRLS by contract — and
# prove nothing about the connection that reads tenant rows.
#
# Invoked in exactly the shape steps 3a and 3b use: the runtime image carries
# its own virtualenv on PATH and its own default command, so no builder tool
# and no command override belongs in this script.
# `scripts/verify_runtime_admission.py` is a NAMED runtime surface of that
# image (see the Dockerfile's explicit COPY) — the checkout is not mounted over
# /app, so a script absent from the image is not reachable from this step.
#
# It runs AFTER migrations because it inspects grants and row-level security
# those migrations have just (re)created, and BEFORE `up -d app` because
# refusing a runtime identity is only useful while the old container still
# serves.
#
# Read-only, and scoped to the modules a deployment has DECLARED active. With
# no module active it asserts nothing and says so loudly rather than passing in
# silence — see app/runtime_admission.py.
#
# NOTE FOR THE OPERATOR — what a failure here does and does not undo. The `if
# !` guard is the same shape step 3a uses, and a command in an `if` condition
# does NOT fire the ERR trap set above, so this step exits WITHOUT the
# automatic rollback. Nothing is reverted: the migrations applied by step 3b
# stay applied (that trap's rollback would not have reverted them either — see
# the rollback function's closing message), the working tree stays at the
# freshly pulled commit with the new .env pins, and the PREVIOUS app container
# keeps serving on the previous image because step 4 never ran. Repair the
# runtime credential or unset the module flag and re-run deploy; if the new
# revisions are not backward-compatible with the running release, restore the
# step-1 backup.
echo "→ Admission: runtime database identity (runtime credential, read-only)..."
if ! docker compose run --rm app \
    python scripts/verify_runtime_admission.py
then
    echo ""
    echo "DEPLOY STOPPED: the runtime database connection is not admissible" >&2
    echo "for at least one ACTIVE module. The app container was NOT recreated." >&2
    echo "" >&2
    echo "The refusal lines above name each failure. The usual causes are:" >&2
    echo "  - DATABASE_URL still connects as a legacy login rather than" >&2
    echo "    app_user, whose name the module GRANTs and RLS policies carry;" >&2
    echo "  - a module was activated before its app_user grants were applied;" >&2
    echo "  - RUNTIME_ADMISSION_TENANT_ID / RUNTIME_ADMISSION_OTHER_TENANT_ID" >&2
    echo "    are unset, so tenant isolation could not be proved." >&2
    echo "" >&2
    echo "Migrations from step 3b have ALREADY been applied and are NOT rolled" >&2
    echo "back. Repair the runtime credential, or unset the module's" >&2
    echo "activation flag, then re-run deploy." >&2
    exit 1
fi
echo ""

# Step 4: recreate the app container on the new image + code
echo "→ Recreating app container..."
docker compose up -d app

# Step 5: health gate
echo "→ Waiting for health check (up to ${HEALTH_TIMEOUT}s)..."
healthy=0
for i in $(seq 1 "$HEALTH_TIMEOUT"); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo "  App healthy after ${i}s"
        healthy=1
        break
    fi
    sleep 1
done

if [[ "$healthy" != "1" ]]; then
    trap - ERR
    echo "  ERROR: App not healthy after ${HEALTH_TIMEOUT}s!"
    docker logs dotmac_erp_app --tail 20 || true
    handle_deploy_failure
    exit 1
fi

# Step 6: restart worker/beat (only on a healthy deploy)
# Recreate (not just restart) so worker/beat pick up the newly-pinned image.
echo "→ Recreating worker and beat on the pinned image..."
docker compose up -d worker beat
echo "→ Admitting worker and Beat..."
wait_for_worker_admission
wait_for_beat_admission
# The complete new runtime is now admitted. Until this point any failure after
# an activation commit must remain forward-fix-only; a healthy HTTP process is
# not sufficient while its workers are still drained.
trap - ERR
echo "→ Enforcing Docker image retention (keep last ${DOCKER_IMAGE_KEEP_LAST:-5})..."
if ! KEEP_LAST="${DOCKER_IMAGE_KEEP_LAST:-5}" \
  IMAGE_REPOSITORY="$IMAGE_REPOSITORY" \
  "$SCRIPT_DIR/prune_docker_images.sh" --execute; then
    echo "  WARNING: Docker image retention failed; deploy remains healthy."
fi

echo ""
echo "=== Deploy complete ==="
echo "Verify: https://erp.dotmac.io/health/ready"
