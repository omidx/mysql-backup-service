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