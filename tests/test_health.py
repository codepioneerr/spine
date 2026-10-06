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
        self.assertEqual({k: result[k] for k in ("daily_vitals", "sleep_sessions", "workouts")},
                         {"daily_vitals": 2, "sleep_sessions": 1, "workouts": 1})
        self.assertEqual(result["errors"], 0)
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


# ── five streams ─────────────────────────────────────────────────────

STREAMS = {"data": {
    "metrics": [
        {"name": "walking_asymmetry_percentage", "units": "%", "data": [
            {"date": "2026-10-05 08:00:00 -0400", "qty": 2.5, "source": "iPhone"},
            {"date": "2026-10-05 18:00:00 -0400", "qty": 3.5, "source": "iPhone"}]},
        {"name": "dietary_magnesium", "units": "mg", "data": [
            {"date": "2026-10-05 00:00:00 -0400", "qty": 310}]},
        {"name": "heart_rate", "units": "count/min", "data": [
            {"date": "2026-10-05 09:00:00 -0400", "Min": 52, "Avg": 70, "Max": 140}]},
        {"name": "blood_pressure", "units": "mmHg", "data": [
            {"date": "2026-10-05 09:00:00 -0400", "systolic": 118, "diastolic": 76}]},
        {"name": "sleep_analysis", "units": "hr", "data": [
            {"startDate": "2026-10-04 23:00:00 -0400", "endDate": "2026-10-05 01:00:00 -0400", "value": "Core"},
            {"startDate": "2026-10-05 01:00:00 -0400", "endDate": "2026-10-05 02:00:00 -0400", "value": "Deep"},
            {"startDate": "2026-10-05 02:00:00 -0400", "endDate": "2026-10-05 02:30:00 -0400", "value": "Awake"},
            {"startDate": "2026-10-05 02:30:00 -0400", "endDate": "2026-10-05 04:00:00 -0400", "value": "REM"}]},
        {"name": "broken", "data": [{"qty": 1}]},
    ],
    "symptoms": [{"name": "Headache", "start": "2026-10-05 14:00:00 -0400",
                  "end": "2026-10-05 16:00:00 -0400", "severity": "Mild"}],
    "ecg": [{"start": "2026-10-05 10:00:00 -0400", "end": "2026-10-05 10:00:30 -0400",
             "classification": "Sinus Rhythm", "averageHeartRate": 64,
             "samplingFrequency": 512, "numberOfVoltageMeasurements": 3,
             "voltageMeasurements": [{"date": "x", "voltage": 1.0}, {"voltage": 2.0}, {"voltage": 3.0}]}],
    "stateOfMind": [{"id": "SOM-1", "start": "2026-10-05 20:00:00 -0400", "kind": "dailyMood",
                     "valence": 0.4, "valenceClassification": "Pleasant", "labels": ["Calm"],
                     "associations": ["Fitness"]}],
    "medications": [{"name": "whatever"}],
}}


class TestStreams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = health.connect(os.path.join(self.tmp.name, "health.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_all_streams_land(self):
        r = health.ingest(STREAMS, self.conn)
        self.assertEqual(r["health_metric_samples"], 9)  # 2+1+1+1+4 sleep segments
        self.assertEqual((r["symptoms"], r["ecg_recordings"], r["state_of_mind"]), (1, 1, 1))
        self.assertEqual(r["errors"], 1)            # the dateless "broken" sample
        self.assertEqual(r["unhandled"], ["medications"])
        bp = self.conn.execute("SELECT extra_json FROM health_metric_samples "
                               "WHERE metric='blood_pressure'").fetchone()[0]
        self.assertEqual(json.loads(bp), {"diastolic": 76, "systolic": 118})
        hr = self.conn.execute("SELECT qty, min, max FROM health_metric_samples "
                               "WHERE metric='heart_rate'").fetchone()
        self.assertEqual(hr, (70.0, 52.0, 140.0))
        sleep = self.conn.execute("SELECT total_sleep_minutes, deep_sleep_minutes, "
                                  "rem_sleep_minutes, awake_minutes FROM sleep_sessions").fetchone()
        self.assertEqual(sleep, (270.0, 60.0, 90.0, 30.0))
        n = self.conn.execute("SELECT sample_count FROM ecg_recordings").fetchone()[0]
        self.assertEqual(n, 3)

    def test_streams_idempotent(self):
        for _ in range(3):
            health.ingest(STREAMS, self.conn)
        for t, n in (("health_metric_samples", 9), ("symptoms", 1), ("ecg_recordings", 1),
                     ("state_of_mind", 1), ("sleep_sessions", 1)):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], n, t)

    def test_stage_segments_crossing_midnight_are_one_night(self):
        segs = [("2026-10-05 23:00:00 -0400", "2026-10-05 23:50:00 -0400", "Core"),
                ("2026-10-05 23:50:00 -0400", "2026-10-06 00:30:00 -0400", "Deep"),
                ("2026-10-06 00:30:00 -0400", "2026-10-06 01:10:00 -0400", "REM"),
                ("2026-10-06 01:10:00 -0400", "2026-10-06 01:20:00 -0400", "Awake")]
        p = {"data": {"metrics": [{"name": "sleep_analysis", "data": [
            {"startDate": s, "endDate": t, "value": v} for s, t, v in segs]}]}}
        for _ in range(2):
            health.ingest(p, self.conn)
        rows = self.conn.execute("SELECT start_time, total_sleep_minutes, awake_minutes "
                                 "FROM sleep_sessions").fetchall()
        self.assertEqual(rows, [("2026-10-05T23:00:00-04:00", 130.0, 10.0)])

    def test_garbage_records_never_raise(self):
        junk = {"data": {"metrics": [None, 5, {"name": "x", "data": "no"},
                                     {"name": "y", "data": [None, {"date": "bad"}]}],
                         "workouts": [None, {"name": "Run"}], "symptoms": [{}],
                         "ecg": ["x"], "stateOfMind": [{"valence": "?"}]}}
        r = health.ingest(junk, self.conn)
        self.assertGreater(r["errors"], 0)

    def test_raw_kept_and_reprocessable(self):
        body = json.dumps(STREAMS).encode()
        health.store_raw(body, self.conn)
        health.store_raw(body, self.conn)  # same bytes, one row
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0], 1)
        r = health.reprocess(self.conn)
        self.assertEqual(r["symptoms"], 1)


class TestReceiverStreams(TestReceiver):
    # Own server and db (setUpClass runs per class); only the test below.
    test_auth_rejected = test_sync_and_repeat_is_idempotent = None
    test_bad_payload_and_route = test_registered_but_never_scheduled = None

    def test_streams_over_http_and_oversize(self):
        status, body = self.post(STREAMS)
        self.assertEqual(status, 200)
        self.assertEqual(body["upserted"]["state_of_mind"], 1)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", health_receiver.ROUTE)
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Length", str(health_receiver.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()


# ── morning health brief ─────────────────────────────────────────────

from datetime import date, datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from core import health_brief as hb  # noqa: E402


def _history(days=20):
    """A synthetic month where magnesium tracks deep sleep the next night."""
    metrics = {"walking_asymmetry_percentage": [], "heart_rate_variability": [],
               "dietary_magnesium": [], "walking_double_support_percentage": []}
    sleep = []
    start = date(2026, 9, 16)
    for i in range(days):
        d = start + timedelta(days=i)
        last = i == days - 1
        metrics["walking_asymmetry_percentage"].append(
            {"date": f"{d} 12:00:00 -0400", "qty": 6.0 if last else 1.5 + (i % 3) * 0.1})
        metrics["heart_rate_variability"].append(
            {"date": f"{d} 03:00:00 -0400", "qty": 30 if last else 55 + i % 4})
        metrics["walking_double_support_percentage"].append(
            {"date": f"{d} 12:00:00 -0400", "qty": 28 + (i % 2)})
        metrics["dietary_magnesium"].append({"date": f"{d} 00:00:00 -0400", "qty": 200 + 20 * (i % 5)})
        n = d + timedelta(days=1)
        sleep.append({"sleepStart": f"{d} 23:00:00 -0400", "sleepEnd": f"{n} 06:30:00 -0400",
                      "totalSleep": 7, "deep": (40 + 4 * (i % 5)) / 60, "rem": 1.5,
                      "core": 4.5, "awake": 0.3})
    m = [{"name": k, "units": "%", "data": v} for k, v in metrics.items()]
    m.append({"name": "sleep_analysis", "data": sleep})
    return {"data": {"metrics": m, "workouts": [
        {"id": "MT-1", "name": "Kickboxing", "start": f"{start + timedelta(days=days - 1)} 18:00:00 -0400",
         "duration": 3600, "activeEnergyBurned": {"qty": 600}}]}}


class TestHealthBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = health.connect(os.path.join(self.tmp.name, "health.db"))
        health.ingest(_history(), self.conn)
        self.day = date(2026, 10, 5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_layer1_flags_and_sections(self):
        b = hb.build(self.conn, self.day)
        self.assertTrue(b["has_data"])
        text = b["text"]
        for s in ("Gait:", "Recovery:", "Training:", "Nutrition:", "Kickboxing"):
            self.assertIn(s, text)
        self.assertTrue(any("asymmetry" in f for f in b["flags"]))
        self.assertTrue(any("HRV" in f for f in b["flags"]))

    def test_correlation_found(self):
        b = hb.build(self.conn, self.day)
        self.assertTrue(any("magnesium intake" in p and "deep sleep" in p for p in b["patterns"]),
                        b["patterns"])

    def test_no_data_says_so(self):
        b = hb.build(self.conn, date(2025, 1, 1))
        self.assertFalse(b["has_data"])
        self.assertIn("No data synced", b["text"])

    def test_prompt_fits_context(self):
        p = hb.prompt(hb.build(self.conn, self.day)["text"], "x" * 5000)
        self.assertIn("Gokhale", p)
        self.assertLess(len(p) / 3.5, 4096 - 650)  # rough chars-per-token bound

    def test_job_runs_and_keeps_file(self):
        from collectors import health_brief as job
        from core import registry
        jobs, errors = registry.discover()
        self.assertEqual(errors, [])
        meta = next(j for j in jobs if j.id == "health_brief")
        self.assertEqual(meta.data, "private")
        prompts, sent = [], []
        models = SimpleNamespace(complete=lambda p, **k: prompts.append(p) or SimpleNamespace(
            text="Glide today.", provider="mock", tokens_out=3, latency_ms=1))
        ctx = SimpleNamespace(now=datetime(2026, 10, 6, 9, 10, tzinfo=timezone.utc),
                              log=lambda *a, **k: None, models=models, dry_run=False,
                              job=SimpleNamespace(tier="smart"),
                              notify=SimpleNamespace(send=sent.append))
        env = {"SPINE_HEALTH_DB": os.path.join(self.tmp.name, "health.db"),
               "SPINE_HEALTH_PROFILE": os.path.join(self.tmp.name, "none.md")}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        orig_ram, orig_keep = job.judgment_ram, job._keep
        kept = []
        job.judgment_ram = lambda log: (True, 9999, 1)
        job._keep = lambda day, text: kept.append((day, text)) or "x"
        try:
            out = job.run(ctx)
        finally:
            job.judgment_ram, job._keep = orig_ram, orig_keep
            for k, v in old.items():
                os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(out["stats"]["day"], "2026-10-05")
        self.assertTrue(out["stats"]["plan"])
        self.assertIn("Glide today.", kept[0][1])
        self.assertEqual(sent, [])  # Telegram is opt-in
        self.assertIn("Gokhale", prompts[0])
