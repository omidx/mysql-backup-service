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
     