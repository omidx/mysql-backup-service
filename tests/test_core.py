import datetime as dt
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys
import shutil

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mysql_backup_service", ROOT / "mysql_backup_service.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader
spec.loader.exec_module(mod)


class CronTests(unittest.TestCase):
    def test_hourly(self):
        c = mod.CronSchedule("0 * * * *")
        self.assertTrue(c.matches(dt.datetime(2026, 9, 20, 10, 0)))
        self.assertFalse(c.matches(dt.datetime(2026, 9, 20, 10, 1)))

    def test_steps(self):
        c = mod.CronSchedule("*/15 */6 * * *")
        self.assertTrue(c.matches(dt.datetime(2026, 9, 20, 12, 30)))
        self.assertFalse(c.matches(dt.datetime(2026, 9, 20, 13, 30)))

    def test_sunday_zero_and_seven(self):
        sunday = dt.datetime(2026, 9, 20, 3, 0)
        self.assertTrue(mod.CronSchedule("0 3 * * 0").matches(sunday))
        self.assertTrue(mod.CronSchedule("0 3 * * 7").matches(sunday))

    def test_next_runs(self):
        c = mod.CronSchedule("0 2 * * *")
        start = dt.datetime(2026, 9, 20, 1, 59, tzinfo=dt.timezone.utc)
        runs = c.next_runs(start, 2)
        self.assertEqual(runs[0].hour, 2)
        self.assertEqual(runs[0].day, 20)
        self.assertEqual(runs[1].day, 21)


class CoordinateTests(unittest.TestCase):
    def test_source_coordinates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "backup.sql.gz"
            with gzip.open(path, "wt") as fh:
                fh.write("-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='binlog.000123', SOURCE_LOG_POS=456;\n")
            self.assertEqual(mod.parse_dump_coordinates(path), ("binlog.000123", 456))

    def test_legacy_coordinates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "backup.sql.gz"
            with gzip.open(path, "wt") as fh:
                fh.write("-- CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.000002', MASTER_LOG_POS=789;\n")
            self.assertEqual(mod.parse_dump_coordinates(path), ("mysql-bin.000002", 789))


class ConfigTests(unittest.TestCase):
    def _config(self, text):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "test.ini"
        path.write_text(text)
        settings = mod.Settings(str(path))
        self.addCleanup(td.cleanup)
        return settings

    def test_per_database_policy_override(self):
        s = self._config("""
[general]
backup_root=/tmp
[schedule]
full=0 2 * * *
diff=0 * * * *
[retention]
full_days=30
diff_days=14
minimum_full_backups=2
gfs_daily=7
gfs_weekly=4
gfs_monthly=12
[database:app]
full_schedule=0 3 * * 0
diff_schedule=30 3 * * *
full_days=90
""")
        m = object.__new__(mod.BackupManager)
        m.s = s
        p = mod.BackupManager.policy(m, "app")
        self.assertEqual(p.full_schedule, "0 3 * * 0")
        self.assertEqual(p.full_days, 90)
        self.assertEqual(p.gfs_monthly, 12)

    def test_table_filters_require_diff_off(self):
        s = self._config("""
[schedule]
full=0 2 * * *
diff=0 * * * *
[database:app]
exclude_tables=audit_logs
""")
        with self.assertRaises(mod.BackupError):
            s.validate()

    def test_global_table_filters_require_global_diff_off(self):
        s = self._config("""
[schedule]
full=0 2 * * *
diff=0 * * * *
[tables]
exclude_tables=audit_logs
""")
        with self.assertRaises(mod.BackupError):
            s.validate()

    def test_table_filters_allowed_when_diff_off(self):
        s = self._config("""
[schedule]
full=0 2 * * *
diff=0 * * * *
[database:app]
diff_schedule=off
exclude_tables=audit_logs
schema_only_tables=history
""")
        s.validate()


class MySQLBinlogCommandTests(unittest.TestCase):
    def test_pitr_command_contains_fixed_range_and_datetime(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c.ini"
            cfg.write_text("""
[mysql]
mode=native
host=127.0.0.1
port=3306
user=backup
[diff]
filter_by_database=true
""")
            client = mod.MySQLClient(mod.Settings(str(cfg)))
            args, _ = client.mysqlbinlog_command(
                "app", ["bin.000001", "bin.000002"], start_position=123, stop_position=999, stop_datetime="2026-09-20 14:37:12"
            )
            joined = " ".join(args)
            self.assertIn("--start-position=123", joined)
            self.assertIn("--stop-position=999", joined)
            self.assertIn("--stop-datetime=2026-09-20 14:37:12", joined)
            self.assertIn("--database=app", joined)
            self.assertTrue(joined.endswith("bin.000001 bin.000002"))


class EncryptionCompatibilityTests(unittest.TestCase):
    def test_unencrypted_old_manifest_works_when_new_config_has_encryption(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c.ini"
            cfg.write_text("""
[encryption]
enabled=true
provider=age
age_recipient=age1example
age_identity_file=/nonexistent
""")
            s = mod.Settings(str(cfg))
            e = mod.EncryptionManager(s)
            backup = Path(td) / "x.sql.gz"
            with gzip.open(backup, "wb") as fh:
                fh.write(b"SELECT 1;\n")
            e.verify_payload(backup, {"enabled": False, "provider": "none"})


class EncryptionRoundTripTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_openssl_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            passfile = Path(td) / "pass"
            passfile.write_text("correct horse battery staple\n")
            cfg = Path(td) / "c.ini"
            cfg.write_text(f"""
[encryption]
enabled=true
provider=openssl
openssl_passphrase_file={passfile}
openssl_pbkdf2_iterations=1000
""")
            enc = mod.EncryptionManager(mod.Settings(str(cfg)))
            plain = Path(td) / "data.sql.gz"
            with gzip.open(plain, "wb") as fh:
                fh.write(b"CREATE DATABASE test;\n")
            encrypted = Path(td) / "data.sql.gz.enc"
            enc.encrypt(plain, encrypted)
            self.assertTrue(encrypted.exists())
            enc.verify_payload(encrypted, {"enabled": True, "provider": "openssl"})


class RemoteObjectLockTests(unittest.TestCase):
    def test_object_lock_command(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c.ini"
            cfg.write_text("""
[remote]
enabled=true
backend=rclone
destination=minio:bucket/root
[object_lock]
enabled=true
bucket=bucket
prefix=root
mode=COMPLIANCE
retention_days=30
endpoint_url=https://minio.example
""")
            s = mod.Settings(str(cfg))
            r = mod.RemoteStore(s, Path("/backup/mysql_backup"))
            cmd = r._object_lock_command("app/full/file.sql.gz")
            self.assertIsNotNone(cmd)
            joined = " ".join(cmd)
            self.assertIn("put-object-retention", joined)
            self.assertIn("COMPLIANCE", joined)
            self.assertIn("root/app/full/file.sql.gz", joined)


class GFSTests(unittest.TestCase):
    def test_gfs_keeps_one_per_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c.ini"
            cfg.write_text("""
[general]
backup_root=/tmp
[retention]
minimum_full_backups=1
gfs_daily=7
gfs_weekly=4
gfs_monthly=12
""")
            s = mod.Settings(str(cfg))
            m = object.__new__(mod.BackupManager)
            m.s = s
            policy = mod.BackupManager.policy(m, "app")
            now = mod.now_local()
            items = []
            for hours in (1, 2, 25, 26):
                p = Path(td) / f"f{hours}.sql.gz"
                p.write_bytes(b"x")
                when = now - dt.timedelta(hours=hours)
                items.append((p, {"completed_at": when.isoformat()}))
            keep = mod.BackupManager._gfs_keep(m, items, policy)
            self.assertLessEqual(len(keep), 3)
            self.assertGreaterEqual(len(keep), 2)


if __name__ == "__main__":
    unittest.main()
