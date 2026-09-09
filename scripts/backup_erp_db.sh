#!/usr/bin/env bash
#
# ERP database backup — globals FIRST, then a verifiable custom-format dump.
#
# WHY THIS SHAPE. The previous version ran a bare `pg_dump` of one database.
# That captures tables and rows and NOTHING ELSE: roles, the least-privilege
# `dotmac_erp_app` login, `app_admin`, and every GRANT the RLS policies depend
# on lived in no backup at all. A cluster restored from it has correct data and
# no way for the application to log in — which is not a backup, it is a data
# export with a reassuring filename. Measured and written up in
# docs/inventories/2026-08-30-erp-production-infrastructure-preflight.md § 6.
#
# So this script emits TWO artifacts per run, and they are useless apart:
#
#   dotmac_erp_<ts>.globals.sql.gz   roles, role memberships, cluster GRANTs
#   dotmac_erp_<ts>.dump             the database, pg_dump custom format
#
# Custom format (`-Fc`) rather than plain SQL because it is the only format
# `pg_restore` can inspect without executing: `pg_restore --list` walks the
# archive's table of contents and fails on a truncated or corrupt file. That
# turns "the upload had the right byte count" into "the archive parses", which
# is a materially stronger claim and is asserted below before anything is
# uploaded.
#
# SUPERSEDED-IN-WAITING. dotmac-deployment-foundation ships
# `PostgresRecoveryBundle.v1` (recovery.py), which defines thirteen required
# components -- role closure and membership closure, ownership, default
# privileges, fine-grained ACLs, row security, extensions, tablespaces,
# migration heads -- and makes the PROVED assurance level reachable. This script
# produces two of those components and therefore cannot reach PROVED; under that
# facility's vocabulary its output is a DATA_EXPORT plus a partial role capture,
# not a RECOVERY_BUNDLE. It is retained only until ERP can pin a PUBLISHED
# Foundation carrying that module (0.3.0a1 is source-only; the newest published
# tag is 0.2.0a2), because deleting it first would remove the only recovery
# capability ERP has. Do not extend it; adopt the facility.
#
# Restore is scripts/restore_erp_db.sh. Neither script is a backup on its own:
# a dump nobody has restored is an untested write path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"

# Read one key from the Compose dotenv file.
#
# The old implementation took `head -n 1` — the FIRST occurrence. Compose takes
# the LAST. Production `.env` currently carries duplicate OPENBAO_* keys, so
# that disagreement is live, not theoretical: the backup could authenticate
# with one value while the running application uses another.
#
# Rather than silently pick a side, refuse. A duplicated key in a secret-bearing
# file is a defect to surface, and a backup script is a bad place to guess.
load_env_default() {
  local key="$1" occurrences value
  if [[ -n "${!key:-}" || ! -f "${ENV_FILE}" ]]; then
    return
  fi

  occurrences="$(grep -cE "^${key}=" "${ENV_FILE}" || true)"
  if (( occurrences > 1 )); then
    echo "[backup] FATAL: ${key} appears ${occurrences} times in ${ENV_FILE}." >&2
    echo "[backup] Compose reads the last, this script would read the first." >&2
    echo "[backup] Deduplicate the key before backing up." >&2
    exit 2
  fi

  value="$(sed -n "s/^${key}=//p" "${ENV_FILE}")"
  if [[ -n "${value}" ]]; then
    export "${key}=${value}"
  fi
}

load_env_default "POSTGRES_USER"
load_env_default "POSTGRES_PASSWORD"
load_env_default "POSTGRES_DB"

REMOTE="${REMOTE:-Backup:db.backup}"
DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"
DB_NAME="${DB_NAME:-${POSTGRES_DB:-dotmac_erp}}"
DB_USER="${DB_USER:-${POSTGRES_USER:-postgres}}"
DB_OS_USER="${DB_OS_USER:-postgres}"
# EXPORTED deliberately. `docker exec -e PGPASSWORD` with no `=` forwards the
# value from this process's ENVIRONMENT rather than restating it on the command
# line, which is what keeps the credential out of the container's argv and out
# of the host process table. A bare shell assignment is not in the environment,
# so the flag would forward nothing and the dump would fail to authenticate.
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
LOCAL_DIR="${LOCAL_DIR:-/var/backups/db}"
REMOTE_DIR="${REMOTE_DIR:-${REMOTE}/dotmac_erp}"
# Retention counts RUNS, not files. Each run now writes two artifacts, so a
# file-counting retention would have silently kept 2.5 runs.
KEEP_LAST="${KEEP_LAST:-5}"
# Set to 1 to skip the rclone upload (the CI rehearsal has no remote).
SKIP_UPLOAD="${SKIP_UPLOAD:-0}"

timestamp="$(date -u +%Y%m%d_%H%M%S)"
base="dotmac_erp_${timestamp}"
globals_path="${LOCAL_DIR}/${base}.globals.sql.gz"
dump_path="${LOCAL_DIR}/${base}.dump"

mkdir -p "${LOCAL_DIR}"

# `docker exec -e PGPASSWORD` with no `=` passes the value through from this
# environment instead of restating it on the command line, keeping it out of
# the container's argv and therefore out of the host process table.
exec_db() {
  if [[ -n "${PGPASSWORD}" ]]; then
    docker exec -e PGPASSWORD -i "${DB_CONTAINER}" "$@"
  else
    docker exec -u "${DB_OS_USER}" -i "${DB_CONTAINER}" "$@"
  fi
}

# --- 1. Globals -------------------------------------------------------------
#
# FIRST, deliberately. If the cluster is unreachable this fails before a
# multi-gigabyte dump has been written, and an operator reading the log sees
# the roles artifact precede the data artifact in the same order the restore
# will need them.
# `--no-role-passwords` is NOT optional. Plain `pg_dumpall --globals-only` emits
# CREATE ROLE ... PASSWORD 'SCRAM-SHA-256$...' for every login role, and this
# artifact is uploaded offsite by rclone below -- so omitting the flag ships the
# password verifiers of all fourteen cluster roles to a backup bucket. This
# script previously omitted it. Production never ran that version (the host
# checkout predates it), so no verifier has left the host, but it would have at
# the next deploy.
#
# dotmac_deployment_foundation.recovery names this exact mistake and pins the
# remedy as REQUIRED_ROLE_CAPTURE_ARG = "--no-role-passwords": the bundle carries
# roles WITHOUT password material, and login material is reinstalled afterwards
# from the approved secret source as a separate restore step.
echo "[backup] dumping cluster globals (roles, memberships, GRANTs; no passwords)..."
if [[ -n "${PGPASSWORD}" ]]; then
  exec_db pg_dumpall -U "${DB_USER}" --globals-only --no-role-passwords | gzip -9 > "${globals_path}"
else
  exec_db pg_dumpall --globals-only --no-role-passwords | gzip -9 > "${globals_path}"
fi

# A globals dump that contains no CREATE ROLE captured nothing useful. Catching
# that here is the whole point of this script's existence.
if ! gzip -cd "${globals_path}" | grep -qE '^CREATE ROLE '; then
  echo "[backup] FATAL: globals dump contains no CREATE ROLE statement." >&2
  echo "[backup] Refusing to present a roleless backup as complete." >&2
  exit 1
fi

# Belt and braces on the flag above. The guard is about the NEXT edit to this
# capture, not this one: if a future change drops --no-role-passwords, the
# upload must not proceed. Refuse before anything leaves the host.
if gzip -cd "${globals_path}" | grep -qiE "PASSWORD '(SCRAM-SHA-256|md5)"; then
  echo "[backup] FATAL: the globals dump contains password verifiers." >&2
  echo "[backup] Refusing to upload credential material to the backup bucket." >&2
  rm -f "${globals_path}"
  exit 1
fi
globals_roles="$(gzip -cd "${globals_path}" | grep -cE '^CREATE ROLE ' || true)"
echo "[backup]   ${globals_roles} role(s) captured."

# --- 2. Database ------------------------------------------------------------
echo "[backup] dumping ${DB_NAME} from ${DB_CONTAINER} (custom format)..."
if [[ -n "${PGPASSWORD}" ]]; then
  exec_db pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc --no-password > "${dump_path}"
else
  exec_db pg_dump -d "${DB_NAME}" -Fc > "${dump_path}"
fi

# --- 3. Verify the archive parses -------------------------------------------
#
# `pg_restore --list` reads the archive's table of contents. It does not touch
# a database and it does not restore anything, but it DOES fail on a truncated,
# empty or corrupt file — which a byte count cannot detect. This is the check
# that separates "a file arrived" from "a restorable archive arrived".
echo "[backup] verifying archive integrity (pg_restore --list)..."
# `pg_restore` reads the archive from STDIN when given no filename. Passing
# /dev/stdin instead makes it open the path a second time, which for a piped
# stream yields no bytes and the misleading "did not find magic string in file
# header" -- an integrity error reported for a perfectly good archive.
toc_entries="$(exec_db pg_restore --list < "${dump_path}" | grep -cvE '^;|^$' || true)"
if (( toc_entries < 1 )); then
  echo "[backup] FATAL: archive has no readable table of contents." >&2
  exit 1
fi
echo "[backup]   ${toc_entries} archive entries."

chmod 600 "${globals_path}" "${dump_path}"

# --- 4. Upload --------------------------------------------------------------
if [[ "${SKIP_UPLOAD}" != "1" ]]; then
  echo "[backup] uploading to ${REMOTE_DIR}..."
  rclone copy "${globals_path}" "${REMOTE_DIR}" --log-level INFO
  rclone copy "${dump_path}" "${REMOTE_DIR}" --log-level INFO

  # --- 5. Retention, by run --------------------------------------------------
  echo "[backup] enforcing retention (keep last ${KEEP_LAST} runs)..."
  mapfile -t runs < <(
    rclone lsf "${REMOTE_DIR}" --files-only |
      sed -nE 's/^dotmac_erp_([0-9]{8}_[0-9]{6})\..*$/\1/p' |
      sort -ru
  )
  if (( ${#runs[@]} > KEEP_LAST )); then
    for stale in "${runs[@]:KEEP_LAST}"; do
      echo "[backup]   removing run ${stale}"
      rclone lsf "${REMOTE_DIR}" --files-only |
        grep -E "^dotmac_erp_${stale}\." |
        while read -r f; do rclone deletefile "${REMOTE_DIR}/${f}"; done
    done
  fi
else
  echo "[backup] SKIP_UPLOAD=1 — not uploading."
fi

echo "[backup] done:"
echo "[backup]   globals: ${globals_path}"
echo "[backup]   dump:    ${dump_path}"
echo "[backup] Restore both, globals first: scripts/restore_erp_db.sh"
