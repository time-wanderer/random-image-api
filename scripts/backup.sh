#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
BACKUP_DIR="${ROOT_DIR}/backups"
STAGING_DIR="${BACKUP_DIR}/.staging-${TIMESTAMP}"
ARCHIVE="${BACKUP_DIR}/backup-${TIMESTAMP}.tar.gz"
DATA_DIR="${ROOT_DIR}/data"
DB_FILE="${DATA_DIR}/database/images.db"

mkdir -p "${BACKUP_DIR}" "${STAGING_DIR}/data" "${STAGING_DIR}/config"

if [[ -d "${DATA_DIR}/images" ]]; then
  mkdir -p "${STAGING_DIR}/data/images"
  cp -a "${DATA_DIR}/images/." "${STAGING_DIR}/data/images/"
fi

if [[ -f "${DB_FILE}" ]]; then
  mkdir -p "${STAGING_DIR}/data/database"
  python3 - "${DB_FILE}" "${STAGING_DIR}/data/database/images.db" <<'PY'
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1])
dest = Path(sys.argv[2])
if dest.exists():
    dest.unlink()
with sqlite3.connect(str(source), timeout=30) as conn:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("VACUUM INTO ?", (str(dest),))
PY
fi

if [[ -f "${ROOT_DIR}/.env.example" ]]; then
  cp -a "${ROOT_DIR}/.env.example" "${STAGING_DIR}/config/.env.example"
fi

if [[ -f "${ROOT_DIR}/.env" ]]; then
  python3 - "${ROOT_DIR}/.env" "${STAGING_DIR}/config/env.sanitized" <<'PY'
from pathlib import Path
import sys

sensitive = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "KEY")
source = Path(sys.argv[1])
dest = Path(sys.argv[2])
lines = []
for raw in source.read_text(encoding="utf-8").splitlines():
    if not raw or raw.lstrip().startswith("#") or "=" not in raw:
        lines.append(raw)
        continue
    key, value = raw.split("=", 1)
    if any(token in key.upper() for token in sensitive) and value.strip():
        lines.append(f"{key}=")
    else:
        lines.append(raw)
dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

cat > "${STAGING_DIR}/MANIFEST.txt" <<EOF
created_at=${TIMESTAMP}
source=${ROOT_DIR}
includes=data/images,data/database/images.db,config/.env.example,config/env.sanitized
note=Secrets in ADMIN_TOKEN are not stored in this archive.
EOF

tar -C "${STAGING_DIR}" -czf "${ARCHIVE}" .
rm -rf "${STAGING_DIR}"

echo "Backup created: ${ARCHIVE}"
