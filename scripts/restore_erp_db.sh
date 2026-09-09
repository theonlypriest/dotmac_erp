#!/usr/bin/env bash
#
# ERP database restore — the procedure that did not exist.
#
# Before this script, ERP had a backup producer and scripts/restore_from_backup.py,
# which is a data-only reload helper that explicitly skips CREATE, ALTER, DROP,
# GRANT and REVOKE. There was no pg_restore path, no runbook, and no way to
# rebuild a cluster from an artifact. See
# docs/inventories/2026-08-30-erp-production-infrastructure-preflight.md § 6.
#
# ORDER IS NOT OPTIONAL. Globals first, then the database. `pg_restore` emits
# `ALTER ... OWNER TO <role>` and `GRANT ... TO <role>`; if the role does not
# exist yet those statements fail, and with --exit-on-error the restore stops
# rather than producing a database whose ownership is quietly wrong.
#
#   ./scripts/restore_erp_db.sh \
#       --globals /var/backups/db/dotmac_erp_<ts>.globals.sql.gz \
#       --dump    /var/backups/db/dotmac_erp_<ts>.dump \
#       --target-container <disposable pg container> \
#       --target-db dotmac_erp_restore \
#       --require-roles app_admin,dotmac_erp_app
#
# REFUSES to run against the production database name on the production
# database container. Restoring onto a live ERP cluster is not a rehearsal, it
# is an outage, and a rehearsal script is exactly where that mistake gets made.
set -euo pipefail

GLOBALS=""
DUMP=""
TARGET_CONTAINER=""
TARGET_DB=""
TARGET_USER="${TARGET_USER:-postgres}"
# Roles whose presence after the restore is asserted. The default is the
# PRODUCTION pair: `app_admin` is the migration executor and `dotmac_erp_app`
# is the least-privilege runtime login. Both are created outside any dump, so
# a backup that omits globals restores rows and neither of these — the exact
# failure this script exists to catch. Overridable because a rehearsal cluster
# legitimately has a different role set (CI bootstraps app_admin/app_user).
REQUIRE_ROLES="${REQUIRE_ROLES:-app_admin,dotmac_erp_app}"
# Explicit, deliberate override for a genuine disaster recovery onto the real
# name. Absent, the guard below stands.
ALLOW_PRODUCTION_NAME="${ALLOW_PRODUCTION_NAME:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --globals)          GLOBALS="$2"; shift 2 ;;
    --dump)             DUMP="$2"; shift 2 ;;
    --target-container) TARGET_CONTAINER="$2"; shift 2 ;;
    --target-db)        TARGET_DB="$2"; shift 2 ;;
    --target-user)      TARGET_USER="$2"; shift 2 ;;
    --require-roles)    REQUIRE_ROLES="$2"; shift 2 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

for required in GLOBALS DUMP TARGET_CONTAINER TARGET_DB; do
  if [[ -z "${!required}" ]]; then
    echo "ERROR: --$(echo "${required}" | tr '[:upper:]_' '[:lower:]-') is required." >&2
    exit 2
  fi
done
# --- Refuse production ------------------------------------------------------
if [[ "${ALLOW_PRODUCTION_NAME}" != "1" ]]; then
  if [[ "${TARGET_CONTAINER}" == "dotmac_pg_local" || "${TARGET_DB}" == "dotmac_erp" ]]; then
    echo "ERROR: refusing to restore onto the production container/database." >&2
    echo "       target-container=${TARGET_CONTAINER} target-db=${TARGET_DB}" >&2
    echo "       Rehearse on a disposable target. For a real disaster recovery," >&2
    echo "       set ALLOW_PRODUCTION_NAME=1 deliberately and knowingly." >&2
    exit 2
  fi
fi

[[ -r "${GLOBALS}" ]] || { echo "ERROR: cannot read ${GLOBALS}" >&2; exit 2; }
[[ -r "${DUMP}"    ]] || { echo "ERROR: cannot read ${DUMP}" >&2; exit 2; }

psql_t() { docker exec -i "${TARGET_CONTAINER}" psql -X -v ON_ERROR_STOP=1 -U "${TARGET_USER}" "$@"; }

echo "=== ERP restore ==="
echo "  globals : ${GLOBALS}"
echo "  dump    : ${DUMP}"
echo "  target  : ${TARGET_CONTAINER} / ${TARGET_DB}"
echo ""

# --- 1. Verify the archive before touching the target -----------------------
echo "-> verifying archive integrity..."
entries="$(docker exec -i "${TARGET_CONTAINER}" pg_restore --list < "${DUMP}" | grep -cvE '^;|^$' || true)"
if (( entries < 1 )); then
  echo "ERROR: archive has no readable table of contents; aborting before any change." >&2
  exit 1
fi
echo "   ${entries} archive entries."

# --- 2. Globals -------------------------------------------------------------
#
# Roles are cluster-wide, so this runs against `postgres`, not the target
# database. Restoring globals into an existing cluster emits "role already
# exists" for roles that are present; that is expected and benign, so this step
# deliberately does NOT use ON_ERROR_STOP.
echo "-> restoring cluster globals (roles, memberships, GRANTs)..."
gzip -cd "${GLOBALS}" \
  | docker exec -i "${TARGET_CONTAINER}" psql -X -U "${TARGET_USER}" -d postgres \
  2>&1 | grep -vE 'already exists|^$' || true

# --- 3. Target database -----------------------------------------------------
echo "-> creating ${TARGET_DB}..."
psql_t -d postgres -c "DROP DATABASE IF EXISTS ${TARGET_DB}"
psql_t -d postgres -c "CREATE DATABASE ${TARGET_DB}"

echo "-> restoring database..."
docker exec -i "${TARGET_CONTAINER}" \
  pg_restore --exit-on-error --no-owner --role="${TARGET_USER}" \
             -U "${TARGET_USER}" -d "${TARGET_DB}" < "${DUMP}"

# --- 4. Prove the restore, do not assume it ---------------------------------
#
# The failure this whole change exists to prevent is a restore that produces
# rows but no roles. So assert roles explicitly; a row count alone would have
# passed against the old broken backup.
echo ""
echo "-> verifying restored cluster..."

missing=0
IFS=',' read -r -a required_roles <<< "${REQUIRE_ROLES}"
for role in "${required_roles[@]}"; do
  if psql_t -d postgres -Atc "SELECT 1 FROM pg_roles WHERE rolname='${role}'" | grep -q 1; then
    echo "   OK   role ${role} present"
  else
    echo "   FAIL role ${role} MISSING" >&2
    missing=1
  fi
done

tables="$(psql_t -d "${TARGET_DB}" -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")"
echo "   ${tables} table(s) restored"
if (( tables < 1 )); then
  echo "   FAIL no tables restored" >&2
  missing=1
fi

# Alembic's applied revisions are the schema's own statement of what it is.
if psql_t -d "${TARGET_DB}" -Atc \
     "SELECT to_regclass('public.alembic_version') IS NOT NULL" | grep -q t; then
  heads="$(psql_t -d "${TARGET_DB}" -Atc "SELECT count(*) FROM alembic_version")"
  echo "   ${heads} alembic head(s) present:"
  psql_t -d "${TARGET_DB}" -Atc "SELECT version_num FROM alembic_version ORDER BY 1" | sed 's/^/     /'
else
  echo "   note: no alembic_version table in this archive"
fi

if (( missing != 0 )); then
  echo ""
  echo "RESTORE INCOMPLETE — see FAIL lines above." >&2
  exit 1
fi

echo ""
echo "=== restore verified: roles present, schema populated ==="
