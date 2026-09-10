#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

BACKUP_DIR="${ROOT_DIR}/backups"
DATA_DIR="${ROOT_DIR}/data"
DB_FILE="${DATA_DIR}/database/images.db"
STAGING_DIR=""
ARCHIVE_TMP=""

cleanup() {
  [[ -z "${STAGING_DIR}" ]] || rm -rf -- "${STAGING_DIR}"
  [[ -z "${ARCHIVE_TMP}" ]] || rm -f -- "${ARCHIVE_TMP}"
}
trap cleanup EXIT

mkdir -p "${BACKUP_DIR}"
exec 9>"${BACKUP_DIR}/.data-maintenance.lock"
if ! flock -n 9; then
  echo "Another backup or restore operation is already running." >&2
  exit 5
fi

check_api_offline() {
  local running=""
  if command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1 \
    && running="$(docker compose ps --status running -q api 2>/dev/null)"; then
    if [[ -n "${running//[[:space:]]/}" ]]; then
      echo "Refusing online backup: stop the api service first with 'docker compose stop api'." >&2
      exit 4
    fi
    return 0
  fi

  if [[ "${BACKUP_OFFLINE_CONFIRMED:-0}" == "1" ]]; then
    echo "Warning: Compose state could not be checked; proceeding because BACKUP_OFFLINE_CONFIRMED=1." >&2
    return 0
  fi

  echo "Cannot confirm that Compose api is stopped. Run 'docker compose stop api', verify with 'docker compose ps --status running -q api', or set BACKUP_OFFLINE_CONFIRMED=1 only after independently confirming writes are stopped." >&2
  exit 4
}

check_api_offline

if [[ ! -d "${DATA_DIR}/images" ]]; then
  echo "Backup requires ${DATA_DIR}/images." >&2
  exit 3
fi
if [[ ! -f "${DB_FILE}" ]]; then
  echo "Backup requires ${DB_FILE}." >&2
  exit 3
fi

TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
STAGING_DIR="$(mktemp -d "${BACKUP_DIR}/.backup-staging.XXXXXXXX")"
ARCHIVE_TMP="$(mktemp "${BACKUP_DIR}/.backup-archive.XXXXXXXX.tar.gz")"
ARCHIVE="${BACKUP_DIR}/backup-${TIMESTAMP}.tar.gz"
mkdir -p "${STAGING_DIR}/data/images" "${STAGING_DIR}/data/database" "${STAGING_DIR}/config"

cp -a "${DATA_DIR}/images/." "${STAGING_DIR}/data/images/"
python3 - "${DB_FILE}" "${STAGING_DIR}/data/database/images.db" <<'PY'
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1])
dest = Path(sys.argv[2])
with sqlite3.connect(str(source), timeout=30) as source_conn:
    source_conn.execute("PRAGMA busy_timeout=5000")
    with sqlite3.connect(str(dest), timeout=30) as dest_conn:
        source_conn.backup(dest_conn)
        result = dest_conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise SystemExit("Backup SQLite snapshot failed integrity_check")
PY

if [[ -f "${ROOT_DIR}/.env.example" ]]; then
  cp -a "${ROOT_DIR}/.env.example" "${STAGING_DIR}/config/.env.example"
fi

if [[ -f "${ROOT_DIR}/.env" ]]; then
  python3 - "${ROOT_DIR}/.env" "${STAGING_DIR}/config/env.sanitized" <<'PY'
from pathlib import Path
import sys

sensitive = ("TOKEN", "PASSWORD", "USERNAME", "SECRET", "API_KEY", "KEY")
source = Path(sys.argv[1])
dest = Path(sys.argv[2])
lines = []
for raw in source.read_text(encoding="utf-8").splitlines():
    if not raw or raw.lstrip().startswith("#") or "=" not in raw:
        lines.append(raw)
        continue
    key, value = raw.split("=", 1)
    lines.append(f"{key}=" if any(token in key.upper() for token in sensitive) and value.strip() else raw)
dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

cat > "${STAGING_DIR}/MANIFEST.txt" <<EOF_MANIFEST
created_at=${TIMESTAMP}
source=random-image-api
includes=data/images,data/database/images.db,config/.env.example,config/env.sanitized
note=Secrets, data/cache, and temporary chunk upload files are excluded. Restore invalidates upload_tasks because upload.bin is intentionally absent.
EOF_MANIFEST

tar -C "${STAGING_DIR}" -czf "${ARCHIVE_TMP}" .
if [[ -e "${ARCHIVE}" ]]; then
  ARCHIVE="${BACKUP_DIR}/backup-${TIMESTAMP}-$$.tar.gz"
fi
mv -- "${ARCHIVE_TMP}" "${ARCHIVE}"
ARCHIVE_TMP=""
rm -rf -- "${STAGING_DIR}"
STAGING_DIR=""
trap - EXIT

echo "Backup created: ${ARCHIVE}"
