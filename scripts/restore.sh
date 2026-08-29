#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

ARCHIVE="${1:-}"
if [[ -z "${ARCHIVE}" ]]; then
  ARCHIVE="$(ls -1t "${ROOT_DIR}/backups"/backup-*.tar.gz 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${ARCHIVE}" || ! -f "${ARCHIVE}" ]]; then
  echo "Usage: $0 /path/to/backup-YYYY-MM-DD-HHMMSS.tar.gz" >&2
  exit 1
fi

ARCHIVE="$(cd "$(dirname "${ARCHIVE}")" && pwd)/$(basename "${ARCHIVE}")"
CONFIRM="${RESTORE_CONFIRM:-}"

if [[ "${CONFIRM}" != "YES" ]]; then
  echo "This will replace ${ROOT_DIR}/data/images and ${ROOT_DIR}/data/database."
  echo "Existing data will be moved to a timestamped .pre-restore directory."
  echo "Re-run with RESTORE_CONFIRM=YES $0 ${ARCHIVE}"
  exit 2
fi

TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
STAGING_DIR="${ROOT_DIR}/backups/.restore-${TIMESTAMP}"
SAFETY_DIR="${ROOT_DIR}/backups/pre-restore-${TIMESTAMP}"

mkdir -p "${STAGING_DIR}" "${SAFETY_DIR}"
tar -xzf "${ARCHIVE}" -C "${STAGING_DIR}"

if [[ ! -d "${STAGING_DIR}/data/images" && ! -f "${STAGING_DIR}/data/database/images.db" ]]; then
  echo "Archive does not look like a valid backup." >&2
  rm -rf "${STAGING_DIR}"
  exit 3
fi

if [[ -d "${ROOT_DIR}/data/images" ]]; then
  mkdir -p "${SAFETY_DIR}/data"
  cp -a "${ROOT_DIR}/data/images" "${SAFETY_DIR}/data/images"
fi
if [[ -d "${ROOT_DIR}/data/database" ]]; then
  mkdir -p "${SAFETY_DIR}/data"
  cp -a "${ROOT_DIR}/data/database" "${SAFETY_DIR}/data/database"
fi

rm -rf "${ROOT_DIR}/data/images" "${ROOT_DIR}/data/database"
mkdir -p "${ROOT_DIR}/data/images/desktop" "${ROOT_DIR}/data/images/mobile" "${ROOT_DIR}/data/database" "${ROOT_DIR}/data/logs"

if [[ -d "${STAGING_DIR}/data/images" ]]; then
  cp -a "${STAGING_DIR}/data/images/." "${ROOT_DIR}/data/images/"
fi
if [[ -f "${STAGING_DIR}/data/database/images.db" ]]; then
  cp -a "${STAGING_DIR}/data/database/images.db" "${ROOT_DIR}/data/database/images.db"
  rm -f "${ROOT_DIR}/data/database/images.db-wal" "${ROOT_DIR}/data/database/images.db-shm"
fi

if [[ ! -f "${ROOT_DIR}/.env" && -f "${STAGING_DIR}/config/env.sanitized" ]]; then
  cp -a "${STAGING_DIR}/config/env.sanitized" "${ROOT_DIR}/.env"
  echo "Created .env from sanitized backup. Review ADMIN_TOKEN before starting."
fi

rm -rf "${STAGING_DIR}"

echo "Restore completed from ${ARCHIVE}"
echo "Previous data saved at ${SAFETY_DIR}"
echo "Next: docker compose up -d && docker compose ps"
