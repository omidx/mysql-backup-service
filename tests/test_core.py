import datetime as dt
import gzip
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mysql_backup_service", ROOT / "mysql_backup_service.py")
mod = importlib.util.module_from_spec(spec)
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


if __name__ == "__main__":
    unittest.main()
