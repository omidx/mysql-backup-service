#!/usr/bin/env python3
"""MySQL Backup Service.

Production-oriented MySQL full + differential backup daemon using only the
Python standard library and MySQL client utilities.

Differential backups are reconstructed from binary logs beginning at the
binary-log coordinates captured in the most recent full backup. This makes a
restore chain simple: one full backup + one selected differential backup.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import request as urllib_request

VERSION = "2.0.0"
DEFAULT_CONFIG = "/etc/mysql-backup-service/mysql-backup.conf"
SYSTEM_DATABASES = {"information_schema", "performance_schema"}
LOG = logging.getLogger("mysql-backup-service")
STOP_REQUESTED = False


class BackupError(RuntimeError):
    pass


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def timestamp_for_file(value: Optional[dt.datetime] = None) -> str:
    value = value or now_local()
    return value.strftime("%Y-%m-%d_%H-%M-%S")


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def csv_list(value: str) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def safe_db_dir(name: str) -> str:
    # Preserve normal MySQL names while preventing path traversal.
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip(".")
    if not cleaned:
        cleaned = "database"
    if cleaned != name:
        suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}_{suffix}"
    return cleaned


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_gzip(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as fh:
            for _ in iter(lambda: fh.read(1024 * 1024), b""):
                pass
    except Exception as exc:
        raise BackupError(f"gzip verification failed for {path}: {exc}") from exc


def atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


class Settings:
    def __init__(self, path: str):
        self.path = Path(path)
        parser = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#", ";"),
        )
        if not self.path.exists():
            raise BackupError(f"config file not found: {self.path}")
        parser.read(self.path, encoding="utf-8")
        self.p = parser

    def get(self, section: str, key: str, default: str = "") -> str:
        return self.p.get(section, key, fallback=default).strip()

    def getint(self, section: str, key: str, default: int) -> int:
        return self.p.getint(section, key, fallback=default)

    def getfloat(self, section: str, key: str, default: float) -> float:
        return self.p.getfloat(section, key, fallback=default)

    def getbool(self, section: str, key: str, default: bool) -> bool:
        return parse_bool(self.get(section, key, str(default)), default)

    @property
    def backup_root(self) -> Path:
        root = Path(self.get("general", "backup_root", "/backup"))
        namespace = self.get("general", "backup_namespace", "mysql_backup")
        return root / namespace

    @property
    def state_dir(self) -> Path:
        return Path(self.get("general", "state_dir", "/var/lib/mysql-backup-service"))

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return Path(self.get("general", "lock_file", "/run/mysql-backup-service.lock"))


class StateStore:
    def __init__(self, settings: Settings):
        self.path = settings.state_file
        self.data = {
            "version": VERSION,
            "scheduler": {},
            "databases": {},
            "last_error": None,
            "last_success": None,
        }
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                if isinstance(existing, dict):
                    self.data.update(existing)
            except Exception as exc:
                LOG.warning("Could not read state file %s: %s", self.path, exc)
        self.data["version"] = VERSION

    def save(self) -> None:
        atomic_json_write(self.path, self.data)

    def db(self, name: str) -> dict:
        return self.data.setdefault("databases", {}).setdefault(name, {})

    def set_error(self, message: str) -> None:
        self.data["last_error"] = {"time": iso_now(), "message": message}
        self.save()

    def set_success(self) -> None:
        self.data["last_success"] = iso_now()
        self.data["last_error"] = None
        self.save()


class BackupLock:
    def __init__(self, path: Path, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.fh.fileno(), flags)
        except BlockingIOError as exc:
            self.fh.close()
            raise BackupError("another backup operation is already running") from exc
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fh:
            with contextlib.suppress(Exception):
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


class CronField:
    def __init__(self, expr: str, minimum: int, maximum: int, dow: bool = False):
        self.expr = expr.strip()
        self.minimum = minimum
        self.maximum = maximum
        self.dow = dow
        self.is_wildcard = self.expr == "*"
        self.values = self._parse(self.expr)

    def _normalize(self, n: int) -> int:
        if self.dow and n == 7:
            return 0
        return n

    def _range(self, start: int, end: int, step: int) -> Iterable[int]:
        if step <= 0:
            raise BackupError(f"invalid cron step: {step}")
        if start > end:
            raise BackupError("cron ranges may not wrap around")
        return range(start, end + 1, step)

    def _parse(self, expr: str) -> set:
        result = set()
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise BackupError(f"invalid cron field: {expr!r}")
            base, sep, step_text = part.partition("/")
            step = int(step_text) if sep else 1
            if base == "*":
                start, end = self.minimum, self.maximum
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                value = int(base)
                start = end = value
            for n in self._range(start, end, step):
                normalized = self._normalize(n)
                if not (self.minimum <= normalized <= self.maximum):
                    raise BackupError(f"cron value out of range: {n} in {expr!r}")
                result.add(normalized)
        return result

    def matches(self, value: int) -> bool:
        return self._normalize(value) in self.values


class CronSchedule:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise BackupError(
                f"cron expression must have 5 fields (minute hour day month weekday): {expr!r}"
            )
        self.expr = expr
        self.minute = CronField(parts[0], 0, 59)
        self.hour = CronField(parts[1], 0, 23)
        self.dom = CronField(parts[2], 1, 31)
        self.month = CronField(parts[3], 1, 12)
        self.dow = CronField(parts[4], 0, 6, dow=True)

    def matches(self, when: dt.datetime) -> bool:
        cron_dow = (when.weekday() + 1) % 7  # Python Mon=0; cron Sun=0.
        if not self.minute.matches(when.minute):
            return False
        if not self.hour.matches(when.hour):
            return False
        if not self.month.matches(when.month):
            return False

        dom_match = self.dom.matches(when.day)
        dow_match = self.dow.matches(cron_dow)
        if self.dom.is_wildcard and self.dow.is_wildcard:
            day_match = True
        elif self.dom.is_wildcard:
            day_match = dow_match
        elif self.dow.is_wildcard:
            day_match = dom_match
        else:
            # Vixie cron semantics: when both are restricted, either may match.
            day_match = dom_match or dow_match
        return day_match


class MySQLClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mode = self.s.get("mysql", "mode", "native").lower()
        if self.mode not in {"native", "docker"}:
            raise BackupError("mysql.mode must be 'native' or 'docker'")
        self.host = self.s.get("mysql", "host", "127.0.0.1")
        self.port = self.s.getint("mysql", "port", 3306)
        self.socket = self.s.get("mysql", "socket", "")
        self.user = self.s.get("mysql", "user", "backup")
        self.password = self.s.get("mysql", "password", "")
        self.defaults_file = self.s.get("mysql", "defaults_extra_file", "")
        self.container = self.s.get("mysql", "container", "mysql")
        self.container_host = self.s.get("mysql", "container_host", "127.0.0.1")
        self.container_port = self.s.getint("mysql", "container_port", 3306)
        self._dump_help: Optional[str] = None

    def _base_tool(self, tool: str, *, for_binlog: bool = False) -> Tuple[List[str], dict]:
        env = os.environ.copy()
        if self.password:
            env["MYSQL_PWD"] = self.password

        args: List[str] = []
        if self.mode == "docker":
            args = ["docker", "exec", "-i"]
            if self.password:
                args += ["-e", f"MYSQL_PWD={self.password}"]
            args += [self.container, tool]
            host = self.container_host
            port = self.container_port
        else:
            args = [tool]
            host = self.host
            port = self.port

        # mysql client options must follow the executable. --defaults-extra-file
        # must appear before most other options for MySQL utilities.
        if self.defaults_file:
            args.append(f"--defaults-extra-file={self.defaults_file}")
        args += [f"--user={self.user}"]
        if self.socket and not for_binlog and self.mode == "native":
            args += [f"--socket={self.socket}"]
        else:
            args += [f"--host={host}", f"--port={port}"]
        return args, env

    def _run(self, args: Sequence[str], env: dict, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            list(args),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            raise BackupError(f"command failed ({proc.returncode}): {stderr}")
        return proc

    def query(self, sql: str, check: bool = True) -> List[List[str]]:
        args, env = self._base_tool("mysql")
        args += ["--batch", "--skip-column-names", "--raw", "--execute", sql]
        proc = self._run(args, env, check=check)
        if proc.returncode != 0:
            return []
        rows = []
        for line in proc.stdout.splitlines():
            rows.append(line.split("\t"))
        return rows

    def server_version(self) -> str:
        rows = self.query("SELECT VERSION()")
        return rows[0][0] if rows else "unknown"

    def variable(self, name: str) -> str:
        rows = self.query(f"SHOW VARIABLES LIKE '{name}'")
        if rows and len(rows[0]) >= 2:
            return rows[0][1]
        return ""

    def databases(self) -> List[str]:
        rows = self.query("SHOW DATABASES")
        include = csv_list(self.s.get("mysql", "include_databases", "*")) or ["*"]
        exclude = set(csv_list(self.s.get("mysql", "exclude_databases", "information_schema,performance_schema,sys")))
        found = [r[0] for r in rows if r and r[0] not in SYSTEM_DATABASES and r[0] not in exclude]
        if include != ["*"]:
            allowed = set(include)
            found = [db for db in found if db in allowed]
        return sorted(found)

    def binary_log_status(self) -> Tuple[str, int]:
        # New spelling first (MySQL 8.4+), then compatibility spelling.
        rows = self.query("SHOW BINARY LOG STATUS", check=False)
        if not rows:
            rows = self.query("SHOW MASTER STATUS", check=False)
        if not rows or len(rows[0]) < 2:
            raise BackupError("binary logging is unavailable or backup user lacks permission to read binary log status")
        return rows[0][0], int(rows[0][1])

    def binary_logs(self) -> List[str]:
        rows = self.query("SHOW BINARY LOGS")
        return [r[0] for r in rows if r]

    def dump_help(self) -> str:
        if self._dump_help is None:
            if self.mode == "docker":
                args = ["docker", "exec", "-i", self.container, "mysqldump", "--help"]
                env = os.environ.copy()
            else:
                args, env = ["mysqldump", "--help"], os.environ.copy()
            proc = self._run(args, env, check=False)
            self._dump_help = proc.stdout + proc.stderr
        return self._dump_help

    def source_data_option(self) -> str:
        help_text = self.dump_help()
        if "--source-data" in help_text:
            return "--source-data=2"
        if "--master-data" in help_text:
            return "--master-data=2"
        raise BackupError("mysqldump does not support --source-data/--master-data required for differential backups")

    def dump_command(self, database: str, need_coordinates: bool) -> Tuple[List[str], dict]:
        args, env = self._base_tool("mysqldump")
        help_text = self.dump_help()
        args += [
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--default-character-set=utf8mb4",
        ]
        if "--set-gtid-purged" in help_text and self.s.getbool("mysql", "set_gtid_purged_off", True):
            args.append("--set-gtid-purged=OFF")
        if "--no-tablespaces" in help_text and self.s.getbool("mysql", "no_tablespaces", True):
            args.append("--no-tablespaces")
        if need_coordinates:
            args.append(self.source_data_option())
        if self.s.getbool("mysql", "add_drop_database", False):
            args.append("--add-drop-database")
        extra = self.s.get("mysql", "dump_extra_args", "")
        if extra:
            args += shlex.split(extra)
        args += ["--databases", database]
        return args, env

    def mysqlbinlog_command(
        self,
        database: str,
        log_name: str,
        start_position: Optional[int] = None,
        stop_position: Optional[int] = None,
    ) -> Tuple[List[str], dict]:
        args, env = self._base_tool("mysqlbinlog", for_binlog=True)
        args += ["--read-from-remote-server", "--verify-binlog-checksum"]
        if self.s.getbool("diff", "filter_by_database", True):
            args.append(f"--database={database}")
        if start_position is not None:
            args.append(f"--start-position={start_position}")
        if stop_position is not None:
            args.append(f"--stop-position={stop_position}")
        extra = self.s.get("mysql", "mysqlbinlog_extra_args", "")
        if extra:
            args += shlex.split(extra)
        args.append(log_name)
        return args, env

    def mysql_restore_command(self) -> Tuple[List[str], dict]:
        args, env = self._base_tool("mysql")
        # Required when replaying mysqlbinlog output containing binary/BLOB data.
        args.append("--binary-mode")
        extra = self.s.get("mysql", "mysql_extra_args", "")
        if extra:
            args += shlex.split(extra)
        return args, env


COORD_PATTERNS = [
    re.compile(r"SOURCE_LOG_FILE='([^']+)'.*SOURCE_LOG_POS=(\d+)", re.I),
    re.compile(r"MASTER_LOG_FILE='([^']+)'.*MASTER_LOG_POS=(\d+)", re.I),
]


def parse_dump_coordinates(path: Path) -> Optional[Tuple[str, int]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                for pattern in COORD_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        return match.group(1), int(match.group(2))
    except Exception as exc:
        raise BackupError(f"could not inspect dump coordinates in {path}: {exc}") from exc
    return None


class BackupManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mysql = MySQLClient(settings)
        self.state = StateStore(settings)
        self.root = settings.backup_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.s.state_dir.mkdir(parents=True, exist_ok=True)

    def db_paths(self, db: str) -> Tuple[Path, Path, Path]:
        base = self.root / safe_db_dir(db)
        return base, base / "full", base / "diff"

    def check_free_space(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        min_mb = self.s.getint("general", "min_free_space_mb", 1024)
        min_pct = self.s.getfloat("general", "min_free_space_percent", 5.0)
        free_pct = usage.free * 100.0 / usage.total if usage.total else 0
        if usage.free < min_mb * 1024 * 1024:
            raise BackupError(
                f"insufficient free space: {human_bytes(usage.free)} available; minimum is {min_mb} MiB"
            )
        if free_pct < min_pct:
            raise BackupError(
                f"insufficient free space: {free_pct:.1f}% available; minimum is {min_pct:.1f}%"
            )

    def selected_databases(self, requested: Optional[List[str]] = None) -> List[str]:
        available = self.mysql.databases()
        if not requested or requested == ["all"]:
            return available
        missing = [db for db in requested if db not in available]
        if missing:
            raise BackupError(f"requested database(s) not found or excluded: {', '.join(missing)}")
        return requested

    def _stream_command_to_gzip(self, args: Sequence[str], env: dict, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        level = max(1, min(9, self.s.getint("general", "gzip_level", 6)))
        partial = dest.with_name(dest.name + ".partial")
        stderr_fd, stderr_name = tempfile.mkstemp(prefix="mysql-backup-stderr-", text=True)
        os.close(stderr_fd)
        try:
            with open(stderr_name, "wb") as stderr_fh, gzip.open(partial, "wb", compresslevel=level) as out_fh:
                proc = subprocess.Popen(
                    list(args),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=stderr_fh,
                )
                assert proc.stdout is not None
                try:
                    shutil.copyfileobj(proc.stdout, out_fh, length=1024 * 1024)
                finally:
                    proc.stdout.close()
                rc = proc.wait()
            if rc != 0:
                err = Path(stderr_name).read_text(encoding="utf-8", errors="replace").strip()
                raise BackupError(f"backup command failed ({rc}): {err}")
            if partial.stat().st_size == 0:
                raise BackupError("backup command produced an empty file")
            os.replace(partial, dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stderr_name)

    def _append_command_to_gzip(self, args: Sequence[str], env: dict, gz_fh) -> None:
        stderr_fd, stderr_name = tempfile.mkstemp(prefix="mysql-binlog-stderr-", text=True)
        os.close(stderr_fd)
        try:
            with open(stderr_name, "wb") as stderr_fh:
                proc = subprocess.Popen(
                    list(args),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=stderr_fh,
                )
                assert proc.stdout is not None
                try:
                    shutil.copyfileobj(proc.stdout, gz_fh, length=1024 * 1024)
                finally:
                    proc.stdout.close()
                rc = proc.wait()
            if rc != 0:
                err = Path(stderr_name).read_text(encoding="utf-8", errors="replace").strip()
                raise BackupError(f"mysqlbinlog failed ({rc}): {err}")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(stderr_name)

    def _write_manifest(self, backup_file: Path, manifest: dict) -> Path:
        manifest_path = backup_file.with_suffix(backup_file.suffix + ".json")
        manifest["file"] = backup_file.name
        manifest["size_bytes"] = backup_file.stat().st_size
        manifest["sha256"] = sha256_file(backup_file)
        atomic_json_write(manifest_path, manifest)
        if self.s.getbool("general", "write_sha256_file", True):
            checksum_path = backup_file.with_suffix(backup_file.suffix + ".sha256")
            checksum_path.write_text(f"{manifest['sha256']}  {backup_file.name}\n", encoding="utf-8")
        return manifest_path

    def _verify_if_enabled(self, backup_file: Path) -> None:
        if self.s.getbool("general", "verify_after_backup", True):
            verify_gzip(backup_file)

    def full_backup_one(self, db: str) -> dict:
        self.check_free_space()
        _, full_dir, _ = self.db_paths(db)
        full_dir.mkdir(parents=True, exist_ok=True)
        started = now_local()
        stamp = timestamp_for_file(started)
        file_name = f"{safe_db_dir(db)}__full__{stamp}.sql.gz"
        dest = full_dir / file_name
        LOG.info("FULL start database=%s destination=%s", db, dest)

        need_coords = self.s.getbool("diff", "enabled", True)
        args, env = self.mysql.dump_command(db, need_coordinates=need_coords)
        self._stream_command_to_gzip(args, env, dest)
        self._verify_if_enabled(dest)

        coords = parse_dump_coordinates(dest) if need_coords else None
        if need_coords and not coords:
            dest.unlink(missing_ok=True)
            raise BackupError(
                f"full backup for {db} completed but no binary-log coordinates were found; differential backup chain would be unsafe"
            )

        finished = now_local()
        manifest = {
            "schema": 1,
            "type": "full",
            "database": db,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "server_version": self.mysql.server_version(),
            "binlog_start": ({"file": coords[0], "position": coords[1]} if coords else None),
        }
        manifest_path = self._write_manifest(dest, manifest)
        db_state = self.state.db(db)
        db_state["last_full"] = {
            "time": finished.isoformat(timespec="seconds"),
            "file": str(dest),
            "manifest": str(manifest_path),
            "binlog_file": coords[0] if coords else None,
            "binlog_position": coords[1] if coords else None,
        }
        self.state.set_success()
        LOG.info("FULL complete database=%s size=%s", db, human_bytes(dest.stat().st_size))
        self.notify("success", "full", db, str(dest))
        return manifest

    def latest_full_manifest(self, db: str) -> Optional[Tuple[Path, dict]]:
        _, full_dir, _ = self.db_paths(db)
        if not full_dir.exists():
            return None
        candidates = sorted(full_dir.glob("*.sql.gz.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("type") == "full" and data.get("database") == db:
                    data_file = full_dir / data.get("file", "")
                    if data_file.exists():
                        return path, data
            except Exception:
                continue
        return None

    def diff_backup_one(self, db: str) -> dict:
        if not self.s.getbool("diff", "enabled", True):
            raise BackupError("differential backups are disabled in config")
        self.check_free_space()
        latest = self.latest_full_manifest(db)
        if not latest:
            policy = self.s.get("diff", "missing_full_policy", "full").lower()
            if policy == "full":
                LOG.warning("No full backup exists for %s; creating one instead of differential", db)
                return self.full_backup_one(db)
            raise BackupError(f"cannot create differential backup for {db}: no full backup exists")

        full_manifest_path, full_manifest = latest
        start = full_manifest.get("binlog_start") or {}
        start_file = start.get("file")
        start_pos = start.get("position")
        if not start_file or start_pos is None:
            raise BackupError(f"latest full backup for {db} has no binary-log coordinates")

        end_file, end_pos = self.mysql.binary_log_status()
        logs = self.mysql.binary_logs()
        if start_file not in logs:
            policy = self.s.get("diff", "gap_policy", "full").lower()
            if policy == "full":
                LOG.warning(
                    "Binlog %s required by %s has expired; creating a new full backup",
                    start_file,
                    db,
                )
                return self.full_backup_one(db)
            raise BackupError(
                f"binlog gap for {db}: {start_file} is no longer available; a new full backup is required"
            )
        if end_file not in logs:
            raise BackupError(f"current binary log {end_file} was not returned by SHOW BINARY LOGS")

        start_index = logs.index(start_file)
        end_index = logs.index(end_file)
        if end_index < start_index:
            raise BackupError("binary log ordering is inconsistent")
        selected_logs = logs[start_index : end_index + 1]

        _, _, diff_dir = self.db_paths(db)
        diff_dir.mkdir(parents=True, exist_ok=True)
        started = now_local()
        stamp = timestamp_for_file(started)
        base_stamp = Path(full_manifest.get("file", "full")).name.replace(".sql.gz", "")
        file_name = f"{safe_db_dir(db)}__diff__{stamp}__base-{base_stamp}.sql.gz"
        dest = diff_dir / file_name
        partial = dest.with_name(dest.name + ".partial")
        level = max(1, min(9, self.s.getint("general", "gzip_level", 6)))
        LOG.info(
            "DIFF start database=%s from=%s:%s to=%s:%s logs=%d",
            db,
            start_file,
            start_pos,
            end_file,
            end_pos,
            len(selected_logs),
        )

        try:
            with gzip.open(partial, "wb", compresslevel=level) as out_fh:
                header = (
                    f"-- mysql-backup-service differential backup\n"
                    f"-- database: {db}\n"
                    f"-- base full: {full_manifest.get('file')}\n"
                    f"-- range: {start_file}:{start_pos} -> {end_file}:{end_pos}\n"
                ).encode("utf-8")
                out_fh.write(header)
                for idx, log_name in enumerate(selected_logs):
                    first = idx == 0
                    last = idx == len(selected_logs) - 1
                    cmd, env = self.mysql.mysqlbinlog_command(
                        db,
                        log_name,
                        start_position=int(start_pos) if first else None,
                        stop_position=int(end_pos) if last else None,
                    )
                    self._append_command_to_gzip(cmd, env, out_fh)
            os.replace(partial, dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()

        self._verify_if_enabled(dest)
        finished = now_local()
        manifest = {
            "schema": 1,
            "type": "diff",
            "mode": "differential-from-latest-full",
            "database": db,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "server_version": self.mysql.server_version(),
            "base_full_file": full_manifest.get("file"),
            "base_full_manifest": full_manifest_path.name,
            "binlog_start": {"file": start_file, "position": int(start_pos)},
            "binlog_end": {"file": end_file, "position": int(end_pos)},
            "binlog_files": selected_logs,
        }
        manifest_path = self._write_manifest(dest, manifest)
        db_state = self.state.db(db)
        db_state["last_diff"] = {
            "time": finished.isoformat(timespec="seconds"),
            "file": str(dest),
            "manifest": str(manifest_path),
            "base_full_file": full_manifest.get("file"),
            "binlog_end_file": end_file,
            "binlog_end_position": end_pos,
        }
        self.state.set_success()
        LOG.info("DIFF complete database=%s size=%s", db, human_bytes(dest.stat().st_size))
        self.notify("success", "diff", db, str(dest))
        return manifest

    def backup(self, kind: str, databases: Optional[List[str]] = None) -> None:
        dbs = self.selected_databases(databases)
        if not dbs:
            raise BackupError("no databases matched the current include/exclude configuration")
        errors = []
        for db in dbs:
            try:
                if kind == "full":
                    self.full_backup_one(db)
                elif kind == "diff":
                    self.diff_backup_one(db)
                else:
                    raise BackupError(f"unknown backup type: {kind}")
            except Exception as exc:
                errors.append(f"{db}: {exc}")
                LOG.exception("%s backup failed for database=%s", kind.upper(), db)
                self.notify("failure", kind, db, str(exc))
                if self.s.getbool("general", "stop_on_database_error", False):
                    break
        if errors:
            message = "; ".join(errors)
            self.state.set_error(message)
            raise BackupError(message)
        self.cleanup()

    def _manifest_for_file(self, backup_file: Path) -> Optional[dict]:
        m = backup_file.with_suffix(backup_file.suffix + ".json")
        if not m.exists():
            return None
        try:
            return json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            return None

    def cleanup(self) -> None:
        full_days = self.s.getint("retention", "full_days", 30)
        diff_days = self.s.getint("retention", "diff_days", 14)
        min_full = max(1, self.s.getint("retention", "minimum_full_backups", 2))
        now = time.time()

        for db_dir in [p for p in self.root.iterdir() if p.is_dir()] if self.root.exists() else []:
            full_dir = db_dir / "full"
            diff_dir = db_dir / "diff"
            diffs = sorted(diff_dir.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True) if diff_dir.exists() else []

            # Determine full backups referenced by differential backups that will remain.
            protected_fulls = set()
            for diff_file in diffs:
                age_days = (now - diff_file.stat().st_mtime) / 86400
                if diff_days <= 0 or age_days <= diff_days:
                    manifest = self._manifest_for_file(diff_file)
                    if manifest and manifest.get("base_full_file"):
                        protected_fulls.add(manifest["base_full_file"])

            if full_dir.exists():
                fulls = sorted(full_dir.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
                keep_by_count = {p.name for p in fulls[:min_full]}
                for file in fulls:
                    age_days = (now - file.stat().st_mtime) / 86400
                    if full_days > 0 and age_days > full_days and file.name not in keep_by_count and file.name not in protected_fulls:
                        self._delete_backup_set(file)
                        LOG.info("Retention deleted full backup %s", file)

            if diff_dir.exists() and diff_days > 0:
                for file in diffs:
                    age_days = (now - file.stat().st_mtime) / 86400
                    if age_days > diff_days:
                        self._delete_backup_set(file)
                        LOG.info("Retention deleted differential backup %s", file)

        # Clean abandoned partial files older than one day.
        cutoff = now - 86400
        if self.root.exists():
            for partial in self.root.rglob("*.partial"):
                with contextlib.suppress(OSError):
                    if partial.stat().st_mtime < cutoff:
                        partial.unlink()

    def _delete_backup_set(self, file: Path) -> None:
        candidates = [
            file,
            file.with_suffix(file.suffix + ".json"),
            file.with_suffix(file.suffix + ".sha256"),
        ]
        for path in candidates:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def check_environment(self) -> dict:
        result = {
            "version": VERSION,
            "config": str(self.s.path),
            "backup_root": str(self.root),
            "mode": self.mysql.mode,
            "checks": {},
        }
        if self.mysql.mode == "docker":
            result["checks"]["docker"] = bool(shutil.which("docker"))
            if not result["checks"]["docker"]:
                raise BackupError("docker executable was not found")
        else:
            missing_tools = []
            for tool in ("mysql", "mysqldump", "mysqlbinlog"):
                pres