"""Offline release checks using synthetic records, never real credentials or data."""

import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "sync_cloud", Path(__file__).resolve().parents[1] / "sync_cloud.py"
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
TIMESTAMP = 1893459900  # Fictional 2030-01-01 09:05 UTC+8.
DAY = 1893456000  # Date label 2030-01-01 at UTC midnight.


class FakeClient:
    deleted = False

    def fetch_raw_by_time(self, key, start, end):
        values = {
            "heart_rate": {"bpm": 198},
            "steps": {"steps": 100, "distance": 60, "calories": 4},
            "spo2": {"spo2": 98},
            "stress": {"stress": 20},
            "weight": {"weight": 60, "bmi": 21},
            "headset": {"start_time": TIMESTAMP, "end_time": TIMESTAMP + 60, "high": True},
            "sleep": {
                "duration": 420, "bedtime": TIMESTAMP - 8 * 3600,
                "wake_up_time": TIMESTAMP - 3600, "sleep_deep_duration": 90,
            },
        }
        if key not in values:
            return []
        return [{
            "sid": "synthetic-device", "time": TIMESTAMP,
            "value": json.dumps(values[key]), "zone_offset": 28800,
            "deleted": self.deleted,
        }]

    def fetch_daily_report(self, key, start, end):
        values = {
            "steps": {"steps": 100, "distance": 60, "calories": 4},
            "calories": {"calories": 1600},
        }
        if key not in values:
            return []
        return [{"sid": "synthetic-device", "time": DAY,
                 "value": json.dumps(values[key]), "deleted": self.deleted}]

    def fetch_sport_records(self, watermark):
        return ([{
            "sid": "synthetic-device", "key": "synthetic-run", "time": TIMESTAMP,
            "value": json.dumps({"duration": 60, "distance": 100, "max_hrm": 198}),
            "deleted": self.deleted,
        }], 1)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.paths = patch.multiple(
            sync, DATA_DIR=base / "data", ANALYSIS_DIR=base / "analysis",
            FITNESS_DB=base / "data" / "fitness_data.db",
            SUMMARY_DB=base / "data" / "fitness_summary.db",
            TOKEN_FILE=base / "xiaomi_token.json",
        )
        self.paths.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.paths.stop)

    def read_csv(self, name):
        with (sync.ANALYSIS_DIR / name).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_clean_database_and_empty_exports(self):
        sync.init_local_dbs()
        sync.init_local_dbs()
        self.assertEqual(sync.export_all_csvs(), {})
        files = list(sync.ANALYSIS_DIR.glob("*.csv"))
        self.assertEqual(len(files), 8)
        for path in files:
            self.assertEqual(self.read_csv(path.name), [])

    def test_synthetic_sync_exports_all_eight_types(self):
        sync.sync_from_cloud(FakeClient())
        counts = sync.export_all_csvs()
        self.assertEqual(len(counts), 8)
        self.assertTrue(all(value == 1 for value in counts.values()))
        self.assertEqual(self.read_csv("hearing_health.csv")[0]["duration_sec"], "60")

    def test_utc_timestamp_is_not_shifted_by_sixteen_hours(self):
        sync.sync_from_cloud(FakeClient())
        sync.export_all_csvs()
        self.assertEqual(self.read_csv("hr_raw.csv")[0]["datetime"], "2030-01-01 09:05:00")
        self.assertEqual(self.read_csv("steps_daily.csv")[0]["date"], "2030-01-01")
        self.assertEqual(self.read_csv("sleep_daily.csv")[0]["wakeup"], "2030-01-01 08:05")

    def test_repeated_sync_does_not_duplicate_rows(self):
        client = FakeClient()
        sync.sync_from_cloud(client)
        sync.sync_from_cloud(client)
        with sqlite3.connect(sync.FITNESS_DB) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hr_record").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT watermark FROM server_request").fetchone()[0], 1)

    def test_deleted_records_do_not_reappear_in_exports(self):
        client = FakeClient()
        sync.sync_from_cloud(client)
        client.deleted = True
        sync.sync_from_cloud(client)
        self.assertEqual(sync.export_all_csvs(), {})
        self.assertEqual(self.read_csv("hr_raw.csv"), [])

    def test_login_preserves_password_and_protects_saved_token(self):
        import urllib.request
        from http.cookiejar import Cookie

        cookie_handler = urllib.request.HTTPCookieProcessor()
        cookie_handler.cookiejar.set_cookie(Cookie(
            0, "serviceToken", "synthetic-service-token", None, False,
            "hlth.io.mi.com", True, False, "/", True, True, None, True, None, None, {},
        ))

        class Response:
            def __init__(self, obj):
                self.obj = obj

            def read(self):
                return ("&&&START&&&" + json.dumps(self.obj)).encode()

        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = [
            Response({}),
            Response({"code": 0, "location": "https://hlth.io.mi.com/sts?test=1",
                      "ssecurity": "synthetic", "userId": "synthetic",
                      "cUserId": "synthetic", "nonce": 1}),
            Response({}),
        ]
        with patch("builtins.input", return_value="synthetic"), \
                patch.object(sync.getpass, "getpass", return_value=" password "), \
                patch.object(sync.urllib.request, "HTTPCookieProcessor", return_value=cookie_handler), \
                patch.object(sync.urllib.request, "build_opener", return_value=opener), \
                contextlib.redirect_stdout(io.StringIO()):
            sync.interactive_xiaomi_login()
        request = opener.open.call_args_list[1].args[0]
        params = sync.urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(params["hash"], [sync.hashlib.md5(b" password ").hexdigest().upper()])
        self.assertEqual(json.loads(sync.TOKEN_FILE.read_text())["serviceToken"], "synthetic-service-token")
        if os.name == "posix":
            self.assertEqual(sync.TOKEN_FILE.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
