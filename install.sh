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
  echo "Compare it with mysql-backup.conf.example to enable new v2.1 features."
fi

if [[ ! -f "$CONF_DIR/mysql-client.cnf" ]]; then
  install -m 0600 "$REPO_DIR/mysql-client.cnf.example" "$CONF_DIR/mysql-client.cnf"
  echo "Created $CONF_DIR/mysql-client.cnf (edit backup-source credentials before starting the service)"
else
  echo "Keeping existing $CONF_DIR/mysql-client.cnf"
fi

if [[ ! -f "$CONF_DIR/mysql-restore-client.cnf" ]]; then
  install -m 0600 "$REPO_DIR/mysql-restore-client.cnf.example" "$CONF_DIR/mysql-restore-client.cnf"
  echo "Created $CONF_DIR/mysql-restore-client.cnf (used only when restore_target is enabled)"
else
  echo "Keeping existing $CONF_DIR/mysql-restore-client.cnf"
fi

systemctl daemon-reload
systemctl enable mysql-backup-service.service

cat <<EOF2

Installed MySQL Backup Service v2.1.

Next steps:
  1. Edit $CONF_DIR/mysql-backup.conf
  2. Edit $CONF_DIR/mysql-client.cnf and keep it chmod 600
  3. If restore_target is enabled, edit $CONF_DIR/mysql-restore-client.cnf (chmod 600)
  4. Config:   $BIN_PATH --config $CONF_DIR/mysql-backup.conf config-test
  5. Doctor:   $BIN_PATH --config $CONF_DIR/mysql-backup.conf doctor
  6. Start:    systemctl start mysql-backup-service
  7. Logs:     journalctl -u mysql-backup-service -f

Optional v2.1 features use external tools:
  rclone  -> remote S3/MinIO/B2/SFTP/FTP
  age/gpg/openssl -> encryption
  aws     -> S3-compatible Object Lock
  docker  -> Docker source/target mode and automated restore testing
  docker/mysql-tools/Dockerfile -> companion mysqlbinlog image for minimal MySQL containers

The installer enables the service at boot but does not start it until you have
reviewed the configuration and credentials.
EOF2
