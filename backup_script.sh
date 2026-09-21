#!/usr/bin/env bash
set -Eeuo pipefail

# Backward-compatible entrypoint for MySQL Backup Service v2.1.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${MYSQL_BACKUP_CONFIG:-/etc/mysql-backup-service/mysql-backup.conf}"

if [[ -x "${SCRIPT_DIR}/mysql_backup_service.py" ]]; then
  APP="${SCRIPT_DIR}/mysql_backup_service.py"
elif [[ -x "/usr/local/lib/mysql-backup-service/mysql_backup_service.py" ]]; then
  APP="/usr/local/lib/mysql-backup-service/mysql_backup_service.py"
else
  echo "mysql_backup_service.py not found" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  exec python3 "$APP" --config "$CONFIG" run
fi

for arg in "$@"; do
  if [[ "$arg" == "--config" || "$arg" == --config=* ]]; then
    exec python3 "$APP" "$@"
  fi
done

exec python3 "$APP" --config "$CONFIG" "$@"
