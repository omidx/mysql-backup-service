# MySQL Backup Service

Production-oriented MySQL backup service for Linux with **Full + binary-log differential backups**, per-database schedules, encryption, off-host replication, point-in-time recovery, automated restore testing, GFS retention, throttling, and systemd startup.

Current version: **2.1.0**

## What v2.1 adds

- Remote/off-host backup through **rclone**
  - AWS S3
  - MinIO / any S3-compatible endpoint
  - Backblaze B2 / B2 S3 API
  - SFTP
  - FTP
  - any other rclone-supported backend
- Staged remote publication using temporary `.partial.<id>` objects and a manifest-last completion marker
- Remote size verification before publish
- Remote manifest is finalized last and acts as the completion marker
- Optional S3-compatible **Object Lock** retention through AWS CLI
- Encryption at rest before remote upload
  - `age`
  - GPG
  - OpenSSL AES-256-CBC + PBKDF2
- Exact point-in-time restore: `restore --to-time "YYYY-MM-DD HH:MM:SS"`
- Separate backup Source and Restore Target connections for DR/PITR to a replacement server
- Scheduled disposable Docker restore tests
- CPU/I/O/local-stream/remote-bandwidth throttling
- Per-database schedules and retention policies
- Excluded tables and schema-only tables for Full backups
- GFS smart retention: daily / weekly / monthly restore points
- Expanded CLI:
  - `backup`
  - `restore`
  - `verify`
  - `list`
  - `status`
  - `prune`
  - `doctor`
  - `config-test`
  - `schedule`
  - `restore-test`
  - `version`

## Backup layout

With:

```ini
[general]
backup_root = /backup
backup_namespace = mysql_backup
```

the service creates one directory per database:

```text
/backup/mysql_backup/
├── appdb/
│   ├── full/
│   │   ├── appdb__full__2026-09-20_02-00-00.sql.gz.age
│   │   ├── appdb__full__2026-09-20_02-00-00.sql.gz.age.json
│   │   └── appdb__full__2026-09-20_02-00-00.sql.gz.age.sha256
│   └── diff/
│       ├── appdb__diff__2026-09-20_03-00-00__base-appdb__full__2026-09-20_02-00-00.sql.gz.age
│       ├── ...json
│       └── ...sha256
└── reporting/
    ├── full/
    └── diff/
```

When encryption is disabled, the `.age`, `.gpg`, or `.enc` suffix is absent.

## How Full and Diff work

A Full backup uses `mysqldump --single-transaction` and captures the exact binary-log coordinates using `--source-data=2` (or `--master-data=2` on older clients). New installations default to `add_drop_database = true` so a Full restore into a non-empty target replaces the database cleanly instead of leaving stale objects that no longer exist in the backup.

`--single-transaction` provides the strongest consistency for transactional engines such as InnoDB. If a selected database contains non-transactional tables (for example MyISAM), plan a maintenance/locking strategy or migrate those tables; a lock-free logical snapshot cannot guarantee the same cross-table consistency for non-transactional engines.

A Diff backup uses **one mysqlbinlog process across the complete binary-log range** from the selected Full coordinate to the current coordinate. This produces a simple restore chain:

```text
FULL + one compatible DIFF
```

For reliable database-level row-event filtering, Diff mode defaults to requiring:

```text
log_bin = ON
binlog_format = ROW
```

Run `mysql-backup-service doctor` to validate this.

By default, auto-discovery excludes `information_schema`, `performance_schema`, `sys`, and the MySQL system schema `mysql`. You can explicitly include `mysql` if you have a tested reason to back up grant/system tables, but MySQL 8.x does not permit `DROP DATABASE mysql`; the service therefore never emits `--add-drop-database` for that schema and `doctor` warns when it is selected.

## Exact Point-in-Time Recovery

Restore to an exact time:

```bash
sudo mysql-backup-service restore \
  --database appdb \
  --latest \
  --to-time "2026-09-20 14:37:12" \
  --yes
```

With `--latest`, the service selects the newest Full backup whose completion time is at or before the requested target. It reads a fixed binary-log snapshot from the Source configured in `[mysql]`, while SQL is applied to the optional independent `[restore_target]`. This allows DR/PITR into a replacement MySQL server without writing back into the production Source.

### Important PITR requirement

Exact PITR currently requires the original/source MySQL server's required binary logs to still be available. If the base binary log has expired, the command refuses to continue rather than silently producing an incomplete recovery.

`mysqlbinlog --stop-datetime` interprets timestamps in the timezone of the machine where `mysqlbinlog` runs. In Docker source mode the service asks the source container to render the requested epoch in the container timezone (including the target date's DST rules) before invoking `mysqlbinlog`. The CLI treats the requested second as inclusive: a target of `14:37:12` includes events timestamped `14:37:12` and excludes later seconds.

## Requirements

### Base requirements

- Linux
- Python 3.8+
- systemd
- MySQL client tools (`mysql`, `mysqldump`, `mysqlbinlog`) in native mode
- or Docker in `mysql.mode=docker`

### Optional feature dependencies

| Feature | Tool |
|---|---|
| Remote storage | `rclone` |
| age encryption | `age` |
| GPG encryption | `gpg` |
| OpenSSL encryption | `openssl` |
| S3 Object Lock | AWS CLI v2 (`aws`) |
| Automated restore test | Docker |
| I/O priority | `ionice` |
| CPU priority | `nice` |

Optional tools are checked by `doctor`; the base service does not require third-party Python packages.

## Installation

```bash
git clone https://github.com/omidx/mysql-backup-service.git
cd mysql-backup-service
sudo ./install.sh
```

Then edit:

```bash
sudo nano /etc/mysql-backup-service/mysql-backup.conf
sudo nano /etc/mysql-backup-service/mysql-client.cnf
sudo chmod 600 /etc/mysql-backup-service/mysql-client.cnf
# If restore_target is enabled:
sudo nano /etc/mysql-backup-service/mysql-restore-client.cnf
sudo chmod 600 /etc/mysql-backup-service/mysql-restore-client.cnf
```

Validate configuration only:

```bash
sudo mysql-backup-service config-test
```

Run runtime checks:

```bash
sudo mysql-backup-service doctor
```

Start the service:

```bash
sudo systemctl start mysql-backup-service
sudo systemctl status mysql-backup-service
```

Logs:

```bash
sudo journalctl -u mysql-backup-service -f
```

## MySQL backup account

Use a dedicated account instead of `root`. A typical MySQL 8.x starting point is:

```sql
CREATE USER 'backup'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';

GRANT SELECT, SHOW VIEW, TRIGGER, EVENT,
      RELOAD, REPLICATION CLIENT, REPLICATION SLAVE
ON *.* TO 'backup'@'127.0.0.1';
```

Privileges vary by MySQL version and environment. With the default `no_tablespaces=true`, `PROCESS` is not needed just for tablespace dumping; disabling that option can change the privilege requirements. Restore should use a separate controlled restore account. Row-based `mysqlbinlog` replay can require `BINLOG_ADMIN`, or `REPLICATION_APPLIER` plus the privileges required by the replayed events.

## Separate Restore Target

For production recovery, especially PITR, configure a destination independently from the backup Source:

```ini
[restore_target]
enabled = true
mode = native
host = 10.10.10.50
port = 3306
user = restore
defaults_extra_file = /etc/mysql-backup-service/mysql-restore-client.cnf
password =
```

`[mysql]` always remains the backup/binlog **Source**. When `restore_target.enabled=true`, Full/Diff/PITR SQL is written to `[restore_target]`. When it is false, Restore falls back to `[mysql]`; that is intentionally supported for backward compatibility but means `restore --yes` writes to the Source connection.

Keep restore credentials separate and restrictive:

```bash
sudo chmod 600 /etc/mysql-backup-service/mysql-restore-client.cnf
```

Docker targets are also supported with `mode=docker` and a separate `container=` name. `doctor` checks the Restore Target connection when it is enabled.

## Per-database policies

Global defaults:

```ini
[schedule]
full = 0 2 * * *
diff = 0 * * * *
retry_cooldown_seconds = 300

[retention]
full_days = 30
diff_days = 14
minimum_full_backups = 2
gfs_daily = 7
gfs_weekly = 4
gfs_monthly = 12
```

Override any database:

```ini
[database:appdb]
full_schedule = 0 2 * * *
diff_schedule = 0 * * * *
full_days = 30
diff_days = 14
gfs_daily = 7
gfs_weekly = 4
gfs_monthly = 12

[database:reporting]
full_schedule = 0 3 * * 0
diff_schedule = 30 3 * * *
full_days = 90
diff_days = 30
gfs_daily = 14
gfs_weekly = 8
gfs_monthly = 24
```

Show effective schedules and the next three runs:

```bash
sudo mysql-backup-service schedule
```

## Excluding tables / schema-only tables

Full-only example:

```ini
[database:analytics]
full_schedule = 0 4 * * *
diff_schedule = off
exclude_tables = audit_logs,cache_table
schema_only_tables = giant_history
```

Behavior:

- `exclude_tables`: table structure and data are not included.
- `schema_only_tables`: table definition/triggers are included, but table rows are not.

### Safety restriction

A database with table-level exclusions **must use `diff_schedule = off`**. `mysqlbinlog` database filtering can scope row-based DML replay to a database, but executable row-event replay does not provide a reliable general table-exclusion mechanism. Allowing a Full backup to omit a table and then replaying that table's events could create a broken recovery chain, so `config-test` rejects that configuration. `restore --to-time` also refuses PITR when the selected Full manifest shows table exclusions or schema-only tables.

MySQL always logs DDL as statements even when `binlog_format=ROW`; database filtering for those statement events follows MySQL's default-database rules. Avoid cross-database/fully-qualified DDL patterns that rely on a different default database if you require isolated per-database Diff recovery.

## Encryption at rest

Encryption happens **after gzip compression and before remote upload**. SHA-256 is calculated over the final stored/encrypted artifact.

### age

```ini
[encryption]
enabled = true
provider = age
age_recipient = age1...
age_identity_file = /etc/mysql-backup-service/age.key
```

Keep the private identity file off the backup volume when possible and restrict permissions:

```bash
sudo chmod 600 /etc/mysql-backup-service/age.key
```

### GPG

```ini
[encryption]
enabled = true
provider = gpg
gpg_recipient = backup@example.org
gpg_homedir = /root/.gnupg
```

### OpenSSL AES-256

```ini
[encryption]
enabled = true
provider = openssl
openssl_passphrase_file = /etc/mysql-backup-service/backup.passphrase
openssl_pbkdf2_iterations = 200000
```

The OpenSSL cipher/KDF parameters and PBKDF2 iteration count are stored in each backup manifest, so changing the current iteration setting later does not make older v2.1 backups undecipherable. Keep the corresponding passphrase available for the lifetime of those backups.

```bash
sudo chmod 600 /etc/mysql-backup-service/backup.passphrase
```

Older unencrypted v2.0 backup manifests remain readable even if encryption is later enabled; decryption behavior is selected from each backup manifest instead of assuming the current global setting.

## Remote/Object Storage

Remote replication uses `rclone`, so the service does not need cloud-provider SDKs or Python packages.

Example destinations:

```ini
[remote]
enabled = true
backend = rclone
rclone_config = /root/.config/rclone/rclone.conf
retries = 3
bwlimit = 50M

# One of these, depending on the configured rclone remote:
destination = minio:mysql-backups/server01
# destination = aws:my-bucket/server01
# destination = b2:my-bucket/server01
# destination = sftp:/backups/server01
# destination = ftp:/backups/server01
```

The rclone remote itself should be configured outside the Git repository:

```bash
rclone config
```

### Staged remote publication / completion marker

For every backup bundle the service:

1. uploads each local file as `<final>.partial.<random>`
2. checks the remote object's size against the local file
3. moves the temporary remote object to the final key
4. applies Object Lock when configured
5. publishes the JSON manifest **last**

Consumers can therefore treat the final `.json` manifest as the service-level completion marker. On object stores such as S3, `moveto` may be implemented as copy+delete rather than a backend-atomic rename; correctness comes from publishing the manifest last, not from assuming a POSIX-style atomic rename. Final remote object sizes are verified again after promotion. If a Full's earlier remote publication failed, the next Diff checks for the base Full's remote manifest and idempotently republishes that Full bundle before publishing the dependent Diff.

## S3 / MinIO Object Lock

Object Lock is optional and only applies to S3-compatible destinations that support the API.

```ini
[object_lock]
enabled = true
bucket = mysql-backups
prefix = server01
mode = COMPLIANCE
retention_days = 30
endpoint_url = https://minio.example.org
region = us-east-1
aws_profile = backup
```

AWS S3 example can leave `endpoint_url` blank.

Requirements:

- bucket versioning/Object Lock enabled
- AWS CLI configured for the same credentials/endpoint
- permission equivalent to `s3:PutObjectRetention`

Modes:

- `GOVERNANCE`: privileged administrators can bypass retention if explicitly authorized.
- `COMPLIANCE`: objects cannot be shortened/deleted before the retention date, including by root-level bucket administrators within the normal API model.

Test immutability on a non-production bucket before enabling it. A wrong compliance retention period can intentionally make deletion impossible until expiry.

## Remote retention behavior

By default, local pruning does **not** delete remote data:

```ini
[remote]
prune_with_local = false
```

To mirror local retention to remote storage:

```ini
prune_with_local = true
```

When Object Lock blocks a delete, local pruning continues and logs a warning for the remote object.

## GFS smart retention

Example:

```ini
[retention]
gfs_daily = 7
gfs_weekly = 4
gfs_monthly = 12
minimum_full_backups = 2
diff_days = 14
```

For Full backups, the service keeps the newest restore point in each selected calendar bucket:

- one daily Full for the last 7 days
- one weekly Full for the last 4 weeks
- one monthly Full for the last 12 months
- at least `minimum_full_backups`
- any Full still referenced by a retained Diff

This is **retention deduplication**, not block-level content deduplication.

If all GFS values are `0`, traditional `full_days` age-based retention is used.

Preview deletion without changing anything:

```bash
sudo mysql-backup-service prune --dry-run
```

Run it:

```bash
sudo mysql-backup-service prune
```

## Automated restore testing

Enable a weekly disposable Docker restore test:

```ini
[restore_test]
enabled = true
schedule = 0 5 * * 0
docker_image = mysql:8.4
startup_timeout_seconds = 120
databases = *
validation_queries = SELECT 1
```

The service:

1. creates an isolated disposable MySQL container with a random root password
2. selects the latest Full and compatible Diff for each selected database
3. verifies manifest, checksum, decryption, and gzip integrity
4. restores the backup chain into the disposable container
5. verifies the database is queryable
6. runs optional validation queries
7. records success/failure in service state
8. destroys the container

Run immediately:

```bash
sudo mysql-backup-service restore-test
```

Custom queries use `||` as the separator and support `{{database}}` substitution:

```ini
validation_queries = SELECT 1 || SELECT COUNT(*) FROM `{{database}}`.important_table
```

## Throttling

```ini
[throttle]
nice = 10
ionice_class = 2
ionice_level = 7
local_stream_mbps = 50

[remote]
bwlimit = 20M
```

- In native MySQL mode, `nice` lowers CPU scheduling priority of `mysqldump`/`mysqlbinlog` child processes.
- In native MySQL mode, `ionice` lowers child-process I/O priority when available. In Docker source mode the host-side wrapper cannot directly change the container process scheduler priority.
- `local_stream_mbps` works in both native and Docker source modes by throttling the dump/binlog byte stream before gzip writing and therefore back-pressuring the source process.
- `remote.bwlimit` is passed to rclone and limits network transfer bandwidth.

Set any throttle to `0`/blank to disable it.

## CLI

### Backup

```bash
sudo mysql-backup-service backup --type full
sudo mysql-backup-service backup --type diff
sudo mysql-backup-service backup --type full --database appdb
```

### Restore latest chain

For DR, enable `[restore_target]` first. If it is disabled, the command writes to the `[mysql]` Source connection. Restore remains destructive and requires `--yes`.

```bash
sudo mysql-backup-service restore --database appdb --latest --yes
```

### Restore explicit chain

```bash
sudo mysql-backup-service restore \
  --database appdb \
  --full /backup/mysql_backup/appdb/full/<file> \
  --diff /backup/mysql_backup/appdb/diff/<file> \
  --yes
```

### Exact PITR

```bash
sudo mysql-backup-service restore \
  --database appdb \
  --latest \
  --to-time "2026-09-20 14:37:12" \
  --yes
```

### Verify

Deep verification (manifest + SHA-256 + decrypt + gzip scan):

```bash
sudo mysql-backup-service verify
sudo mysql-backup-service verify --database appdb
```

Quick manifest/checksum verification:

```bash
sudo mysql-backup-service verify --quick
```

### List

```bash
sudo mysql-backup-service list
sudo mysql-backup-service list --database appdb
```

### Status

```bash
sudo mysql-backup-service status
sudo mysql-backup-service status --json
```

### Configuration / doctor

```bash
sudo mysql-backup-service config-test
sudo mysql-backup-service doctor
```

`check` remains as an alias for `doctor` for backward compatibility.

### Schedule

```bash
sudo mysql-backup-service schedule
```

### Version

```bash
mysql-backup-service version
```

## Backup metadata

Every backup has a JSON manifest containing data such as:

- service version
- backup type
- database
- start/completion time
- MySQL version
- filename/size
- SHA-256
- encryption provider
- Full binary-log coordinate
- Diff base Full filename
- Diff start/end coordinates
- binary-log files included
- Full table exclusion/schema-only policy

The manifest is also the remote completion marker when remote replication is enabled.

## Failure safety

The service is intentionally fail-closed in recovery-critical situations:

- local in-progress backups use `.partial`
- remote uploads use random `.partial.<id>` keys
- SHA-256 sidecar creation happens after the data file is durable, and the local JSON manifest is committed last as the local completion marker
- remote manifest is finalized last and acts as the completion marker
- failed scheduled jobs are isolated per database and retried after `schedule.retry_cooldown_seconds` instead of retrying every poll or starving the remaining queue
- failed command output never becomes a successful backup
- Diff refuses to run if its base binlog is unavailable unless configured to create a fresh Full
- table-filtered Full policies cannot be combined with Diff/PITR
- exact PITR refuses future targets, rejects `--diff` + `--to-time`, rejects table-filtered Fulls, and refuses to run if its source base binary log has expired
- restore validates the selected Full/Diff relationship
- PITR reads binlogs from the Source but can write to an independent Restore Target
- final backup/manifest files are fsync'd before success is reported, and manual CLI execution enforces a restrictive `077` umask
- one filesystem lock prevents overlapping backup/restore operations

## Security recommendations

- Do not commit database passwords, encryption keys, passphrases, rclone configs, or AWS credentials.
- Keep credential/key files `0600`; manual CLI runs also force `umask 077` for newly created backup/state/log files.
- Prefer a dedicated least-privilege MySQL backup user.
- Keep encryption private keys separate from the backup volume where possible.
- Keep at least one backup copy off-host.
- For ransomware-sensitive environments, use a tested immutable/Object-Locked destination.
- Treat Docker socket access as privileged access.
- Test restores regularly; automated restore testing complements, but does not replace, disaster-recovery exercises.

## systemd

```bash
sudo systemctl daemon-reload
sudo systemctl enable mysql-backup-service
sudo systemctl start mysql-backup-service
sudo systemctl restart mysql-backup-service
sudo systemctl status mysql-backup-service
sudo journalctl -u mysql-backup-service -f
```

## Upgrade from v2.0

```bash
git pull
sudo ./install.sh
```

The installer preserves existing configuration and credential files. Because v2.1 adds new config sections, compare your installed configuration with `mysql-backup.conf.example` and add the features you want.

Existing v2.0 unencrypted backups remain restorable even after v2.1 encryption is enabled; encryption metadata is interpreted per backup.

## Development / CI

```bash
python3 -m py_compile mysql_backup_service.py
python3 -m unittest discover -s tests -v
bash -n backup_script.sh
bash -n install.sh
python3 mysql_backup_service.py --config mysql-backup.conf.example config-test
python3 mysql_backup_service.py version
```

GitHub Actions tests Python 3.8, 3.11, and 3.13 and also runs an end-to-end Docker integration covering encrypted Full + Diff backup, rclone remote publication, restore into a separate MySQL target, and exact PITR.

## License

See [LICENSE](LICENSE).
