"""
Health ingestion: payload parsing, bearer auth, idempotent upserts.

    python3 -m pytest tests/test_health.py -q
"""
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import health  # noqa: E402
from collectors import health_receiver  # noqa: E402

TOKEN = "test-token-not-a-secret"

PAYLOAD = {"data": {
    "metrics": [
        {"name": "step_count", "units": "count", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 4000},
            {"date": "2026-10-05 12:00:00 -0400", "qty": 3500},
            {"date": "2026-10-06 00:00:00 -0400", "qty": 9000}]},
        {"name": "heart_rate_variability", "units": "ms", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 40},
            {"date": "2026-10-05 06:00:00 -0400", "qty": 60}]},
        {"name": "resting_heart_rate", "units": "count/min", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 55}]},
        {"name": "blood_oxygen_saturation", "units": "%", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 97.5}]},
        {"name": "not_a_metric_we_know", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 1}]},
        {"name": "sleep_analysis", "units": "hr", "data": [
            {"date": "2026-10-05 00:00:00 -0400",
             "sleepStart": "2026-10-04 23:10:00 -0400",
             "sleepEnd": "2026-10-05 06:40:00 -0400",
             "totalSleep": 7.0, "deep": 1.0, "rem": 1.5, "core": 4.5,
             "awake": 0.5, "inBed": 7.5}]},
    ],
    "workouts": [
        {"id": "W-1", "name": "Outdoor Run",
         "start": "2026-10-05 07:00:00 -0400", "end": "2026-10-05 07:30:00 -0400",
         "duration": 1800,
         "activeEnergyBurned": {"qty": 320.5, "units": "kcal"},
         "avgHeartRate": {"qty": 150, "units": "bpm"},
         "maxHeartRate": {"qty": 172, "units": "bpm"}}],
}}


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("daily_vitals", "sleep_sessions", "workouts")}


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = health.connect(os.path.join(self.tmp.name, "health.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_tables_exist(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"daily_vitals", "sleep_sessions", "workouts"} <= names)

    def test_valid_payload(self):
        result = health.ingest(PAYLOAD, self.conn)
        self.assertEqual(result, {"daily_vitals": 2, "sleep_sessions": 1, "workouts": 1})
        row = self.conn.execute(
            "SELECT step_count, hrv_avg, resting_hr_avg, oxygen_saturation_avg "
            "FROM daily_vitals WHERE date='2026-10-05'").fetchone()
        self.assertEqual(row, (7500, 50.0, 55.0, 97.5))
        sleep = self.conn.execute(
            "SELECT total_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes, "
            "core_sleep_minutes, awake_minutes, sleep_efficiency_pct "
            "FROM sleep_sessions").fetchone()
        self.assertEqual(sleep, (420.0, 60.0, 90.0, 270.0, 30.0, 93.3))
        w = self.conn.execute("SELECT * FROM workouts").fetchone()
        self.assertEqual(w[0:2], ("W-1", "Outdoor Run"))
        self.assertEqual(w[3:], (30.0, 320.5, 150.0, 172.0))

    def test_idempotent_on_repeat(self):
        for _ in range(3):
            health.ingest(PAYLOAD, self.conn)
        self.assertEqual(counts(self.conn),
                         {"daily_vitals": 2, "sleep_sessions": 1, "workouts": 1})
        steps = self.conn.execute(
            "SELECT step_count FROM daily_vitals WHERE date='2026-10-05'").fetchone()[0]
        self.assertEqual(steps, 7500)  # replaced, not accumulated

    def test_partial_payload_keeps_other_columns(self):
        health.ingest(PAYLOAD, self.conn)
        health.ingest({"data": {"metrics": [{"name": "step_count", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 8000}]}]}}, self.conn)
        row = self.conn.execute(
            "SELECT step_count, hrv_avg FROM daily_vitals WHERE date='2026-10-05'").fetchone()
        self.assertEqual(row, (8000, 50.0))

    def test_rejects_malformed(self):
        with self.assertRaises(ValueError):
            health.ingest([], self.conn)
        with self.assertRaises(ValueError):
            health.ingest({"data": {"metrics": "nope"}}, self.conn)


class TestReceiver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "health.db")
        cls.httpd = HTTPServer(("127.0.0.1", 0),
                               health_receiver.make_handler(TOKEN, cls.db))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def post(self, body, token=TOKEN, path=health_receiver.ROUTE):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_auth_rejected(self):
        self.assertEqual(self.post(PAYLOAD, token="wrong")[0], 401)
        self.assertEqual(self.post(PAYLOAD, token=None)[0], 401)

    def test_sync_and_repeat_is_idempotent(self):
        for _ in range(2):
            status, body = self.post(PAYLOAD)
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(counts(conn),
                             {"daily_vitals": 2, "sleep_sessions": 1, "workouts": 1})

    def test_registered_but_never_scheduled(self):
        from core import registry
        jobs, errors = registry.discover()
        self.assertEqual(errors, [])
        job = next(j for j in jobs if j.id == "health_receiver")
        self.assertFalse(job.enabled)

    def test_bad_payload_and_route(self):
        self.assertEqual(self.post({"data": {"metrics": 5}})[0], 400)
        self.assertEqual(self.post(PAYLOAD, path="/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
