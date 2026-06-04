#!/usr/bin/env bash
set -euo pipefail

# Do not commit real RDS backup URLs. They may contain temporary authorization
# information or business-sensitive paths.
BACKUP_URL="${BACKUP_URL:-<RDS备份下载URL>}"
BACKUP_FILE="${BACKUP_FILE:-backup.tar.zst}"
EXTRACT_DIR="${EXTRACT_DIR:-/home/mysql/data}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
WORKERS="${WORKERS:-4}"

mkdir -p "${EXTRACT_DIR}"
wget -b -c -O "${BACKUP_FILE}" "${BACKUP_URL}"

echo "Wait for wget to finish, then run the extraction and restore steps below."

after_download() {
  zstd -d -c "${BACKUP_FILE}" | tar -xvf - -C "${EXTRACT_DIR}"

  python3 restore_sql_fast.py "${EXTRACT_DIR}" "${MYSQL_HOST}" "${MYSQL_PORT}" "${MYSQL_USER}" \
    --ask-password \
    --workers "${WORKERS}" \
    --log-file restore.log
}
