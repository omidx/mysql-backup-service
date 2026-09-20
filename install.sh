#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/usr/local/lib/mysql-backup-service"
BIN_PATH="/usr/local/bin/mysql-backup-service"
CONF_DIR="/etc/mysql-backup-service"
STATE_DIR="/var/lib/mysql-backup-service"
LOG_DIR="/var/log/mysql-backup-service"
SERVICE_PATH="/etc/systemd/system/mysql-backup-service.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root (sudo ./install.sh)." >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

install -d -m 0755 "$APP_DIR" "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"
install -m 0755 "$REPO_DIR/mysql_backup_service.py" "$APP_DIR/mysql_backup_service.py"
install -m 0755 "$REPO_DIR/backup_script.sh" "$BIN_PATH"
install -m 0644 "$REPO_DIR/backup_service.service" "$SERVICE_PATH"

if [[ ! -f "$CONF_DIR/mysql-backup.conf" ]]; then
  install -m 0600 "$REPO_DIR/mysql-backup.conf.example" "$CONF_DIR/mysql-backup.conf"
  echo "Created $CONF_DIR/mysql-backup.conf"
else
  echo "Keeping existing $CONF_DIR/mysql-backup.conf"
fi

if [[ ! -f "$CONF_DIR/mysql-client.cnf" ]]; then
  install -m 0600 "$REPO_DIR/mysql-client.cnf.example" "$CONF_DIR/mysql-client.cnf"
  echo "Created $CONF_DIR/mysql-client.cnf (edit credentials before starting the service)"
else
  echo "Keeping existing $CONF_DIR/mysql-client.cnf"
fi

systemctl daemon-reload
systemctl enable mysql-backup-service.service

cat <<EOF

Installed MySQL Backup Service.

Next steps:
  1. Edit $CONF_DIR/mysql-backup.conf
  2. Edit $CONF_DIR/mysql-client.cnf and keep it chmod 600
  3. Validate: $BIN_PATH --config $CONF_DIR/mysql-backup.conf check
  4. Start:    systemctl start mysql-backup-service
  5. Logs:     journalctl -u mysql-backup-service -f

The installer enables the service at boot but does not start it until you have
reviewed the configuration and credentials.
EOF
