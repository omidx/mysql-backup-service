# MySQL Backup Service

A production-oriented Linux backup service for MySQL that supports **per-database full backups**, **real differential backups based on MySQL binary logs**, flexible cron scheduling, automatic startup with `systemd`, retention, verification, checksums, restore assistance, Docker mode, and health checks.

> Version 2 replaces the old pseudo-differential dump (`LIMIT 1000`) with a real recovery chain. A differential backup contains all binary-log changes from the latest full backup to the selected point in time.

## Features

- Full backup of every configured MySQL database using `mysqldump`
- Differential backup using `mysqlbinlog`
  - Base = latest full backup
  - Restore chain = **one Full + one Diff**
- Independent cron schedules for Full and Diff jobs
- Automatic database discovery or explicit include/exclude lists
- Native MySQL client mode or Docker-container mode
- `systemd` service with automatic startup on boot
- Configurable backup root directory
- Per-database directory layout
- Database name + backup type + date + time in every backup filename
- Gzip compression with configurable level
- SHA-256 checksum sidecar files
- JSON manifest for every backup
- Atomic `.partial` files to avoid treating interrupted backups as valid
- Post-backup gzip verification
- Pre-restore gzip, manifest, database, chain, and SHA-256 verification
- Configurable free-space safety thresholds
- Retention policy for Full and Diff backups
- Protects Full backups still referenced by retained differential backups
- Keeps a minimum number of Full backups even when age-based retention expires
- Automatic fallback to a new Full backup when required binlogs have expired
- Startup safety: automatically creates missing Full backups
- Optional max-age safety net for missed schedules after downtime
- Persistent state/status file
- Rotating application log + journald output
- Generic webhook notification support
- Manual `check`, `backup`, `list`, `status`, `cleanup`, and `restore` commands
- No third-party Python packages required

## Backup layout

If:

```ini
backup_root = /backup
backup_namespace = mysql_backup
```

and the server contains databases `shop` and `reporting`, the service creates:

```text
/backup/
└── mysql_backup/
    ├── shop/
    │   ├── full/
    │   │   ├── shop__full__2026-09-20_02-00-00.sql.gz
    │   │   ├── shop__full__2026-09-20_02-00-00.sql.gz.json
    │   │   └── shop__full__2026-09-20_02-00-00.sql.gz.sha256
    │   └── diff/
    │       ├── shop__diff__2026-09-20_03-00-00__base-shop__full__2026-09-20_02-00-00.sql.gz
    │       ├── ...sql.gz.json
    │       └── ...sql.gz.sha256
    └── reporting/
        ├── full/
        └── diff/
```

## How differential backup works

A MySQL logical dump by itself does not provide a generic open-source "differential dump" mode. Version 2 therefore uses the MySQL binary log for differential recovery:

1. A Full backup is created with `mysqldump --single-transaction`.
2. The exact binary-log file and position corresponding to that Full backup are embedded by `--source-data=2` (or `--master-data=2` on older clients).
3. A Diff backup uses `mysqlbinlog` to collect changes from that Full-backup coordinate up to the current binary-log coordinate.
4. Each database gets its own filtered differential stream.
5. To restore, apply the Full backup and then one compatible Diff backup.

For reliable per-database filtering, the default configuration requires:

```ini
binlog_format = ROW
```

The `check` command rejects Diff mode when binary logging is disabled or, by default, when the server is not using ROW format.

## Requirements

### Linux

- Python 3.8+
- `systemd`
- `gzip` support through Python's standard library

### Native mode

Install MySQL client utilities containing:

```text
mysql
mysqldump
mysqlbinlog
```

### Docker mode

- Docker installed on the host
- `mysql`, `mysqldump`, and `mysqlbinlog` available inside the configured MySQL container

## MySQL requirements

Differential backups require MySQL binary logging.

Check:

```sql
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'binlog_format';
```

Recommended:

```text
log_bin = ON
binlog_format = ROW
```

MySQL 8.x normally uses ROW by default, but you should verify the actual server configuration.

### Backup user

Use a dedicated account instead of `root`.

A typical MySQL 8.x starting point is:

```sql
CREATE USER 'backup'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';

GRANT SELECT, SHOW VIEW, TRIGGER, EVENT,
      RELOAD, PROCESS,
      REPLICATION CLIENT, REPLICATION SLAVE
ON *.* TO 'backup'@'127.0.0.1';
```

Depending on your exact MySQL version, security policy, object definitions, GTID settings, and restore workflow, additional privileges can be required. The installer does **not** create or modify MySQL accounts automatically.

For restore, use a separate privileged restore account. Replaying row-based `mysqlbinlog` output can require privileges such as `BINLOG_ADMIN`, or `REPLICATION_APPLIER` plus privileges required by the replayed operations.

## Installation

Clone the repository:

```bash
git clone https://github.com/omidx/mysql-backup-service.git
cd mysql-backup-service
```

Run the installer:

```bash
sudo ./install.sh
```

The installer:

- installs the application under `/usr/local/lib/mysql-backup-service/`
- installs `/usr/local/bin/mysql-backup-service`
- creates `/etc/mysql-backup-service/mysql-backup.conf` if missing
- creates `/etc/mysql-backup-service/mysql-client.cnf` if missing
- installs the systemd unit
- enables the service for automatic startup at boot
- does **not** start the service until configuration is reviewed

Edit the configuration:

```bash
sudo nano /etc/mysql-backup-service/mysql-backup.conf
```

Edit credentials:

```bash
sudo nano /etc/mysql-backup-service/mysql-client.cnf
sudo chmod 600 /etc/mysql-backup-service/mysql-client.cnf
```

Validate everything before starting:

```bash
sudo mysql-backup-service check
```

Start the service:

```bash
sudo systemctl start mysql-backup-service
```

Check status:

```bash
sudo systemctl status mysql-backup-service
```

Follow logs:

```bash
sudo journalctl -u mysql-backup-service -f
```

The default rotating application log is also written to:

```text
/var/log/mysql-backup-service/backup.log
```

## Configuration

The sample configuration is `mysql-backup.conf.example`.

### Native MySQL

```ini
[mysql]
mode = native
host = 127.0.0.1
port = 3306
user = backup
defaults_extra_file = /etc/mysql-backup-service/mysql-client.cnf
password =
```

Recommended credential file:

```ini
[client]
user=backup
password=CHANGE_ME
host=127.0.0.1
port=3306
```

Keep it private:

```bash
sudo chmod 600 /etc/mysql-backup-service/mysql-client.cnf
```

### Docker MySQL

Example:

```ini
[mysql]
mode = docker
container = mysql
container_host = 127.0.0.1
container_port = 3306
user = backup
password = CHANGE_ME
# Or use a defaults file that exists INSIDE the container.
defaults_extra_file =
```

The host account running the service must be able to execute `docker exec`.

## Database selection

Backup every non-excluded database:

```ini
include_databases = *
exclude_databases = information_schema,performance_schema,sys
```

Backup only selected databases:

```ini
include_databases = appdb,reporting,mysql
exclude_databases = information_schema,performance_schema,sys
```

`mysql` is intentionally not excluded by default. If you do not want MySQL system tables included, add it to `exclude_databases`.

## Scheduling

Schedules use standard **5-field cron** syntax:

```text
minute hour day-of-month month day-of-week
```

### Daily Full + hourly Diff

```ini
[schedule]
full = 0 2 * * *
diff = 0 * * * *
```

Full: every day at 02:00  
Diff: every hour at minute 00

### Weekly Full + daily Diff

```ini
[schedule]
full = 0 3 * * 0
diff = 30 3 * * *
```

Full: Sunday at 03:00  
Diff: every day at 03:30

### Full every 6 hours + Diff every 30 minutes

```ini
[schedule]
full = 0 */6 * * *
diff = */30 * * * *
```

### Disable one schedule

```ini
[schedule]
full = 0 2 * * *
diff = off
```

The service polls the schedule internally and records each executed minute, so one scheduled job is not repeated multiple times during the same minute.

### Missed-schedule safety

A server can be off during a scheduled time. Optional maximum-age checks can compensate:

```ini
[schedule]
full_max_age_hours = 36
diff_max_age_minutes = 90
```

Set either value to `0` to disable that safety net.

## Retention

Example:

```ini
[retention]
full_days = 30
diff_days = 14
minimum_full_backups = 2
```

The cleanup logic:

- removes expired Diff backups
- removes expired Full backups
- never removes a Full backup still referenced by a retained Diff
- always preserves at least `minimum_full_backups`
- removes abandoned `.partial` files older than one day

Set `full_days = 0` or `diff_days = 0` to disable age-based deletion for that backup type.

## Manual operations

### Check environment

```bash
sudo mysql-backup-service check
```

This validates:

- configuration
- required client utilities
- MySQL connectivity
- database discovery
- `log_bin`
- `binlog_format`
- current binary-log coordinates
- free disk space

### Run an immediate Full backup

All configured databases:

```bash
sudo mysql-backup-service backup --type full
```

One database:

```bash
sudo mysql-backup-service backup --type full --database appdb
```

Multiple selected databases:

```bash
sudo mysql-backup-service backup --type full --database appdb --database reporting
```

### Run an immediate Diff backup

```bash
sudo mysql-backup-service backup --type diff
```

### List backups

```bash
sudo mysql-backup-service list
sudo mysql-backup-service list --database appdb
```

### Service state

```bash
sudo mysql-backup-service status
```

### Run retention now

```bash
sudo mysql-backup-service cleanup
```

## Restore

> Test restores regularly. A backup is not proven until it has been restored successfully in a controlled environment.

### Automatically select latest compatible chain

```bash
sudo mysql-backup-service restore \
  --database appdb \
  --latest \
  --yes
```

The restore command:

1. selects the latest Full backup
2. selects the newest Diff based on that exact Full, if one exists
3. verifies gzip integrity
4. validates backup type and database using the manifest
5. validates the Full/Diff relationship
6. verifies SHA-256 if configured
7. restores the Full
8. replays the Diff with `mysql --binary-mode`

### Restore explicit files

```bash
sudo mysql-backup-service restore \
  --database appdb \
  --full /backup/mysql_backup/appdb/full/appdb__full__2026-09-20_02-00-00.sql.gz \
  --diff /backup/mysql_backup/appdb/diff/appdb__diff__2026-09-20_08-00-00__base-appdb__full__2026-09-20_02-00-00.sql.gz \
  --yes
```

Without `--yes`, restore refuses to execute.

## Backup metadata

Every `.sql.gz` has a JSON manifest containing useful recovery metadata, including:

- backup type
- database
- start/end time
- MySQL server version
- file size
- SHA-256
- Full backup binary-log starting coordinate
- Diff base Full backup
- Diff start/end binary-log coordinates
- binary-log files used for the Diff

Example sidecars:

```text
appdb__full__2026-09-20_02-00-00.sql.gz
appdb__full__2026-09-20_02-00-00.sql.gz.json
appdb__full__2026-09-20_02-00-00.sql.gz.sha256
```

## Free-space protection

The service checks both absolute and percentage free space before backup:

```ini
[general]
min_free_space_mb = 1024
min_free_space_percent = 5
```

A backup is rejected when either threshold is violated.

## Notifications

A generic HTTP JSON webhook can receive backup success/failure events:

```ini
[notifications]
webhook_url = https://example.internal/hooks/mysql-backup
on_success = false
on_failure = true
```

The service sends JSON containing service name, version, timestamp, status, backup type, database, and detail.

## Failure handling

The service is designed to avoid silently producing unusable recovery chains:

- command failures do not become successful backup files
- in-progress files use a `.partial` suffix
- Full backups fail if Diff is enabled but binary-log coordinates cannot be captured
- Diff backups fail or automatically create a new Full when the base binlog has expired
- gzip integrity can be checked after every backup
- restore validates manifest/checksum/chain before applying data
- a process lock prevents overlapping backup/restore operations
- systemd restarts the daemon after unexpected process failure

## Security notes

- Do not place real passwords in the Git repository.
- Prefer `defaults_extra_file` in native mode and keep it `0600`.
- Use a dedicated least-privilege backup account.
- Use a separate restore account with only the privileges needed for controlled recovery.
- Protect the backup filesystem with restrictive Linux permissions.
- Backups contain production data. Encrypt the filesystem, backup volume, or off-host destination where required.
- Copy critical backups off-host. A local backup alone does not protect against host loss, ransomware, storage failure, or administrator error.
- Restrict Docker socket access; membership in the Docker group is effectively highly privileged.
- Test restore procedures on a non-production MySQL instance.

## Binary-log retention planning

A Diff needs every binary log from the base Full backup coordinate to the Diff endpoint.

Therefore, MySQL binary-log retention must be **longer than the maximum intended Full-to-Diff interval plus operational margin**.

For example, if Full backups are weekly, keeping only one day of binary logs is not sufficient. If the required base binlog has expired, the service's default behavior is to create a new Full backup instead of producing an unsafe Diff.

## Large databases

This project intentionally uses logical Full backups for portability and simple recovery.

For very large databases where logical dump time or restore time is unacceptable, consider a physical hot-backup tool such as Percona XtraBackup. Physical incremental backups have different operational requirements and are outside this project's current scope.

## systemd commands

```bash
sudo systemctl daemon-reload
sudo systemctl enable mysql-backup-service
sudo systemctl start mysql-backup-service
sudo systemctl restart mysql-backup-service
sudo systemctl stop mysql-backup-service
sudo systemctl status mysql-backup-service
sudo journalctl -u mysql-backup-service -f
```

## Upgrade

Pull the latest version and rerun the installer:

```bash
git pull
sudo ./install.sh
```

Existing configuration and credential files are preserved.

## Development / CI

Run local checks:

```bash
python3 -m py_compile mysql_backup_service.py
python3 -m unittest discover -s tests -v
bash -n backup_script.sh
bash -n install.sh
```

GitHub Actions runs the same syntax/unit checks on pushes and pull requests.

## Migration from the old script

The original version:

- stored Full and Diff backups in two global directories
- used fixed sleep loops
- hard-coded 12-hour Full / 1-hour Diff timing
- required Docker
- dumped all databases into one file
- used `--where="1 LIMIT 1000"` for a so-called differential backup

That last behavior was **not a valid MySQL differential backup** and could not represent all changed rows.

Version 2 replaces it with:

- one directory per database
- independent Full/Diff schedules
- native or Docker operation
- systemd startup
- real binary-log-based differential recovery
- checksums, manifests, validation, retention, health checks, and restore tooling

## License

See [LICENSE](LICENSE).
