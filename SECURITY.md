# Security policy

## Supported versions

Security fixes are made against the current `main` branch and included in subsequent releases. The current code identifies itself as **2.1.0**. Older releases and the original shell script do not have a committed security maintenance schedule. Check the release notes and use a maintained revision; do not assume that an older tag receives backports.

| Version | Security maintenance |
| --- | --- |
| Current `main` / 2.1.x | Report issues; fixes are evaluated for the current code |
| 2.0.x and earlier | No promised backports |

## Privately report a vulnerability

Use **Security → Report a vulnerability** on this repository if GitHub's private vulnerability reporting is available. Otherwise, contact the maintainer through a private contact method listed on the [maintainer's GitHub profile](https://github.com/omidx). Do not put an exploit, a live credential, a production backup, or sensitive logs in a public issue or pull request. Public issues are suitable for general hardening suggestions that contain no exploit details.

Include the affected commit or version, deployment mode (`native` or `docker`), relevant non-secret configuration, precise reproduction steps in a disposable environment, expected and observed behavior, and potential impact. Redact database names, addresses, credentials, backup contents, webhook URLs and keys. We will acknowledge and triage reports as capacity allows; no response-time or embargo promise is made here. Coordinate disclosure after a fix is available.

If a credential or backup has already leaked, revoke or rotate the credential, restrict access to the affected storage, and follow your incident process immediately. Do not wait for an upstream response.

## Operational security model

The service runs with the privileges of its systemd user (the supplied unit uses `root`). The database source, backup directory, state directory, remote endpoint, rclone configuration, and optional Docker socket are inside that trust boundary. Backups may contain passwords, tokens, personal data and Zabbix information in databases. Treat manifests and log output as sensitive metadata too.

- Keep `/etc/mysql-backup-service/mysql-backup.conf`, MySQL option files, `rclone.conf`, GPG/age identities and OpenSSL passphrase files accessible only to the service account (`0600` for secret files). Restrict parent directories and backup storage. The process sets umask `0077`; verify permissions on mounts and remote storage separately.
- Prefer `defaults_extra_file` and a separate backup account with the minimum required read and binlog privileges. Do not put production passwords in `[mysql].password`, arguments, an image, a CI log, or Git. In Docker source mode, an inline password can appear in `docker exec -e MYSQL_PWD=...` process arguments; protect the Docker host and prefer an option-file arrangement that you have tested inside the container.
- Configure `[restore_target] enabled = true` for recovery drills and point it at an isolated server. If disabled, `restore --yes` falls back to the source connection and can replace its database. Check the selected target and take an independent snapshot before restoring.
- Backups are first written as **plaintext compressed temporary files** in the local backup directory, then encrypted if encryption is enabled. Restrict that volume, its snapshots, and access to deleted blocks. Encryption at rest protects the final payload and remote copy; it does not eliminate this transient plaintext.
- Prefer age or GPG recipient encryption with private keys held outside backup storage. OpenSSL mode uses AES-256-CBC with PBKDF2; it is not authenticated encryption. SHA-256 manifests and sidecars detect accidental corruption but are not a digital signature or protection from an attacker who can rewrite the files and checksums.
- Use a protected remote connection and limit the remote principal to the necessary bucket/prefix. FTP does not provide transport encryption; prefer SFTP or HTTPS-based object storage. Remote publication checks object sizes and publishes the manifest last, but size alone is not an independent remote content-integrity proof. Periodically download and deep-verify remote copies.
- Optional S3 Object Lock depends on bucket versioning, provider support, retention mode, and credentials. Test immutability at the destination. Remote retention is separate from local retention unless `prune_with_local = true`; avoid giving the backup service delete/bypass rights when not needed.
- Restrict Docker socket access. `restore-test` starts a disposable MySQL container and supplies a generated test password via Docker arguments; Docker access normally gives broad host control. Review and pin the selected `docker_image` in environments that require reproducible dependencies.
- Keep MySQL binary logs for at least as long as the desired PITR window. The current implementation does not archive raw binary logs independently, so second-level PITR is unavailable after source binlog loss even if Full/Diff backups survive.
- Restrict `dump_extra_args`, `mysql_extra_args`, `mysqlbinlog_extra_args` and `validation_queries` to trusted administrators: they are passed to database tools or executed against a restore-test database.

## Verification and recovery

Run `config-test` and `doctor` after configuration changes; use `verify` for local integrity/decryption checks and periodically perform an isolated restore with application-specific validation. `restore-test` is opt-in and checks the database in a disposable container; it is not a substitute for a full disaster recovery exercise. A `.json` manifest indicates a completed local or remotely published bundle, not that a backup is immutable, independently authenticated, or restorable without testing.

For usage, configuration and recovery steps, see the [README](README.md) and the [Wiki](https://github.com/omidx/mysql-backup-service/wiki).
