#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

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

if [[ "${RESTORE_CONFIRM:-}" != "YES" ]]; then
  echo "This will replace ${ROOT_DIR}/data/images and ${ROOT_DIR}/data/database, and clear data/tmp/admin/chunked."
  echo "Existing data will be copied to a timestamped pre-restore directory."
  echo "Re-run with RESTORE_CONFIRM=YES $0 ${ARCHIVE}"
  exit 2
fi

BACKUP_DIR="${ROOT_DIR}/backups"
STAGING_DIR=""
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
      echo "Refusing online restore: stop the api service first with 'docker compose stop api'." >&2
      exit 4
    fi
    return 0
  fi

  if [[ "${RESTORE_OFFLINE_CONFIRMED:-0}" == "1" ]]; then
    echo "Warning: Compose state could not be checked; proceeding because RESTORE_OFFLINE_CONFIRMED=1." >&2
    return 0
  fi

  echo "Cannot confirm that Compose api is stopped. Run 'docker compose stop api', verify with 'docker compose ps --status running -q api', or set RESTORE_OFFLINE_CONFIRMED=1 only after independently confirming writes are stopped." >&2
  exit 4
}

# Must precede extraction, snapshots, and every modification under live data/.
check_api_offline

TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
STAGING_DIR="$(mktemp -d "${BACKUP_DIR}/.restore-staging.XXXXXXXX")"
SAFETY_DIR="$(mktemp -d "${BACKUP_DIR}/pre-restore-${TIMESTAMP}.XXXXXXXX")"
cleanup_staging() { [[ -z "${STAGING_DIR}" ]] || rm -rf -- "${STAGING_DIR}"; }
trap cleanup_staging EXIT

export RESTORE_MAX_MEMBERS="${RESTORE_MAX_MEMBERS:-100000}"
export RESTORE_MAX_MEMBER_BYTES="${RESTORE_MAX_MEMBER_BYTES:-1073741824}"
export RESTORE_MAX_TOTAL_BYTES="${RESTORE_MAX_TOTAL_BYTES:-21474836480}"
export RESTORE_FREE_SPACE_MARGIN_BYTES="${RESTORE_FREE_SPACE_MARGIN_BYTES:-268435456}"
export RESTORE_EXPANDED_SPACE_FACTOR="${RESTORE_EXPANDED_SPACE_FACTOR:-2}"

# Validate and extract from one open archive handle to prevent archive swapping
# between validation and extraction.
python3 - "${ARCHIVE}" "${STAGING_DIR}" "${ROOT_DIR}/data" <<'PY_RESTORE'
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

archive_path, staging_arg, live_data_arg = sys.argv[1:]
staging = Path(staging_arg).resolve()
live_data = Path(live_data_arg)

def limit(name: str) -> int:
    raw = os.environ[name]
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")
    if value < 0:
        raise SystemExit(f"{name} must not be negative")
    return value

max_members = limit("RESTORE_MAX_MEMBERS")
max_member = limit("RESTORE_MAX_MEMBER_BYTES")
max_total = limit("RESTORE_MAX_TOTAL_BYTES")
margin = limit("RESTORE_FREE_SPACE_MARGIN_BYTES")
expanded_factor = limit("RESTORE_EXPANDED_SPACE_FACTOR")
if expanded_factor < 2:
    raise SystemExit("RESTORE_EXPANDED_SPACE_FACTOR must be at least 2")

def tree_size(path: Path) -> int:
    """Estimate bytes copied by cp -a without following symlink targets."""
    if not path.exists() or path.is_symlink():
        return 0
    total_size = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
        for name in files:
            candidate = root_path / name
            try:
                if not candidate.is_symlink():
                    total_size += candidate.stat().st_size
            except FileNotFoundError:
                raise SystemExit(f"Live data changed during restore preflight: {candidate}")
    return total_size

with open(archive_path, "rb") as raw_archive:
    with tarfile.open(fileobj=raw_archive, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > max_members:
            raise SystemExit("Backup archive exceeds RESTORE_MAX_MEMBERS")

        checked = []
        seen = set()
        total = 0
        for member in members:
            original = member.name
            path = PurePosixPath(original)
            parts = tuple(part for part in path.parts if part not in ("", "."))
            if not original or "\0" in original or path.is_absolute() or ".." in parts:
                raise SystemExit(f"Unsafe backup member path: {original!r}")
            if not parts:
                if not (member.isdir() and original in (".", "./")):
                    raise SystemExit(f"Unsafe backup member path: {original!r}")
                normalized = "."
            else:
                normalized = "/".join(parts)
            if normalized in seen:
                raise SystemExit(f"Duplicate backup member: {normalized!r}")
            seen.add(normalized)
            if normalized == ".":
                continue
            if not (member.isfile() or member.isdir()):
                raise SystemExit(f"Links and special backup members are forbidden: {original!r}")
            if member.size < 0 or member.size > max_member:
                raise SystemExit(f"Backup member exceeds RESTORE_MAX_MEMBER_BYTES: {normalized!r}")
            if member.isfile():
                total += member.size
                if total > max_total:
                    raise SystemExit("Backup archive exceeds RESTORE_MAX_TOTAL_BYTES")
            checked.append((member, parts))

        old_live = sum(
            tree_size(live_data / relative)
            for relative in ("images", "database", "tmp/admin/chunked")
        )
        # Peak usage keeps the extracted staging tree, an old-live safety copy,
        # and the newly copied live tree at the same time.
        required = total * expanded_factor + old_live + margin
        free = shutil.disk_usage(staging).free
        if free < required:
            raise SystemExit(
                f"Insufficient free space for restore: need {required} bytes "
                f"(expanded copies, old-data rollback copy, and safety margin), have {free} bytes"
            )

        for member, parts in checked:
            destination = staging.joinpath(*parts)
            if destination != staging and staging not in destination.parents:
                raise SystemExit(f"Unsafe extraction destination: {member.name!r}")
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"Cannot read backup member: {member.name!r}")
            with source, open(destination, "xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(destination, 0o600)
PY_RESTORE

if [[ ! -d "${STAGING_DIR}/data/images" ]]; then
  echo "Complete restore requires data/images." >&2
  exit 3
fi
if [[ ! -f "${STAGING_DIR}/data/database/images.db" ]]; then
  echo "Complete restore requires data/database/images.db." >&2
  exit 3
fi

# upload.bin is intentionally excluded. Invalidate its metadata and validate the
# replacement DB before touching live data.
python3 - "${STAGING_DIR}/data/database/images.db" <<'PY_PREPARE_DB'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1], timeout=30) as conn:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_tasks'"
    ).fetchone()
    if exists:
        conn.execute("DELETE FROM upload_tasks")
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit("Restored SQLite database failed integrity_check")
PY_PREPARE_DB

# Snapshot every live resource coupled to upload_tasks so rollback restores one
# consistent old state.
mkdir -p "${SAFETY_DIR}/data"
[[ ! -d "${ROOT_DIR}/data/images" ]] || cp -a "${ROOT_DIR}/data/images" "${SAFETY_DIR}/data/images"
[[ ! -d "${ROOT_DIR}/data/database" ]] || cp -a "${ROOT_DIR}/data/database" "${SAFETY_DIR}/data/database"
if [[ -d "${ROOT_DIR}/data/tmp/admin/chunked" ]]; then
  mkdir -p "${SAFETY_DIR}/data/tmp/admin"
  cp -a "${ROOT_DIR}/data/tmp/admin/chunked" "${SAFETY_DIR}/data/tmp/admin/chunked"
fi

SWITCH_STARTED=0
rollback_restore() {
  local rc="${1:-1}"
  trap - ERR
  if [[ "${SWITCH_STARTED}" == "1" ]]; then
    echo "Restore failed; rolling back previous images, database, and chunked uploads from ${SAFETY_DIR}." >&2
    rm -rf -- "${ROOT_DIR}/data/images" "${ROOT_DIR}/data/database" "${ROOT_DIR}/data/tmp/admin/chunked"
    mkdir -p "${ROOT_DIR}/data" "${ROOT_DIR}/data/tmp/admin"
    [[ ! -d "${SAFETY_DIR}/data/images" ]] || cp -a "${SAFETY_DIR}/data/images" "${ROOT_DIR}/data/images"
    [[ ! -d "${SAFETY_DIR}/data/database" ]] || cp -a "${SAFETY_DIR}/data/database" "${ROOT_DIR}/data/database"
    [[ ! -d "${SAFETY_DIR}/data/tmp/admin/chunked" ]] || cp -a "${SAFETY_DIR}/data/tmp/admin/chunked" "${ROOT_DIR}/data/tmp/admin/chunked"
  fi
  exit "${rc}"
}
trap 'rollback_restore $?' ERR

SWITCH_STARTED=1
rm -rf -- "${ROOT_DIR}/data/images" "${ROOT_DIR}/data/database"
mkdir -p "${ROOT_DIR}/data/images" "${ROOT_DIR}/data/database" "${ROOT_DIR}/data/logs"
cp -a "${STAGING_DIR}/data/images/." "${ROOT_DIR}/data/images/"
LIVE_IMAGES_DIR="${ROOT_DIR}/data/images"
mkdir -p -- "${LIVE_IMAGES_DIR}/desktop" "${LIVE_IMAGES_DIR}/mobile" "${LIVE_IMAGES_DIR}/square"
cp -a "${STAGING_DIR}/data/database/images.db" "${ROOT_DIR}/data/database/images.db"
rm -f -- "${ROOT_DIR}/data/database/images.db-wal" "${ROOT_DIR}/data/database/images.db-shm"

if [[ ! -f "${ROOT_DIR}/.env" && -f "${STAGING_DIR}/config/env.sanitized" ]]; then
  cp -a "${STAGING_DIR}/config/env.sanitized" "${ROOT_DIR}/.env"
  echo "Created .env from sanitized backup. Review ADMIN_TOKEN before starting."
fi

# Final consistency change: staged DB no longer references these files. Rollback
# protection remains active until this removal succeeds.
rm -rf -- "${ROOT_DIR}/data/tmp/admin/chunked"
SWITCH_STARTED=0
trap - ERR
cleanup_staging
STAGING_DIR=""
trap - EXIT

echo "Restore completed from ${ARCHIVE}"
echo "Previous data saved at ${SAFETY_DIR}"
echo "Next: docker compose up -d && docker compose ps"
