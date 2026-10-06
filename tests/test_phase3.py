"""
Phase 3 tests — property history, PLUTO depth, repeat-sale signals.

Same honesty note as Phase 2b: the darkweb-jobs databases here are synthetic
(built from the verbatim schemas in test_phase2b) and PLUTO is a fake HTTP
client returning records in the shape the live API returned on 2026-10-06
("bbl": "1008010001.00000000", every number a string). That proves the code
is self-consistent; the first real 06:45 run is what checks it against the
world.

What is actually being tested:

  1. the two thresholds: $1M kept, $5M shown, independently
  2. history accumulates and first-seen survives re-runs
  3. repeat sales compare like with like — no multi-lot or partial deeds
  4. "could not ask PLUTO" is never recorded as "PLUTO does not have it"
  5. acris uses the parcels proptech filled, and the slice still wins
  6. a dry run writes nothing
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from core import job, proptech                              # noqa: E402
from core.http import HttpError                             # noqa: E402
from core.store import Store                                # noqa: E402
from collectors import acris                                # noqa: E402
from collectors import proptech as collector                # noqa: E402
from test_phase2b import PROPTECH_SCHEMA, BridgeCase        # noqa: E402

UTC = timezone.utc


class FakeHttp:
    """PLUTO stand-in. `parcels` is bbl -> record; `fail` is a set of batch
    indexes that raise, or True for every batch."""

    def __init__(self, parcels=None, fail=()):
        self.parcels = parcels or {}
        self.fail = fail
        self.calls = []

    def get_json(self, url, params=None, headers=None, timeout=None):
        idx = len(self.calls)
        self.calls.append(params)
        if self.fail is True or idx in self.fail:
            raise HttpError("HTTP 503", status=503)
        where = params["$where"]
        keys = where[where.index("(") + 1:where.index(")")].split(",")
        return [self.parcels[k] for k in keys if k in self.parcels]


def pluto_rec(bbl, address="1 TEST STREET", **kw):
    rec = {"bbl": f"{bbl}.00000000", "borough": "QN", "address": address,
           "zipcode": "11101", "bldgclass": "D1", "unitsres": "40",
           "unitstotal": "42", "yearbuilt": "1931", "numfloors": "6.0000000",
           "bldgarea": "40000", "assesstot": "3000000.00000",
           "ownername": "TEST OWNER LLC"}
    rec.update(kw)
    return rec


class Ctx:
    def __init__(self, now, http=None, dry_run=False):
        self.now = now
        self.http = http or FakeHttp()
        self.dry_run = dry_run
        self.secrets = None
        self.db = Store(source="proptech")
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)


class Phase3Case(BridgeCase):

    def setUp(self):
        super().setUp()
        self.dwj = self.make_db("proptech", PROPTECH_SCHEMA)
        self._n = 0

    def tearDown(self):
        self.dwj.close()
        super().tearDown()

    def doc(self, amt, bbls=("4000010001",), date="2026-08-01",
            doc_type="DEED", pct=100.0, doc=None):
        self._n += 1
        doc = doc or f"D{self._n:06d}"
        self.dwj.execute(
            "INSERT INTO acris_master VALUES (?,?,?,?,?,?,?)",
            (doc, doc_type, date, date + "T00:00:00", amt, pct,
             self.hours_ago(2)))
        for b in bbls:
            self.dwj.execute(
                "INSERT INTO acris_legals VALUES (?,?,?,?,?,?,?,?,?)",
                (doc, b[0], int(b[1:6]), int(b[6:]), b, "1", "TEST STREET",
                 "", "D1"))
        self.dwj.commit()
        return doc

    def slice_parcel(self, bbl, address="SLICE ADDRESS"):
        self.dwj.execute(
            "INSERT INTO pluto VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bbl, "MN", address, "10001", "C6-4", "D4", "03", 24, 26, 1928,
             12.0, 90000.0, 8000.0, 1e6, 9e6, "SLICE OWNER", 40.7, -74.0,
             self.hours_ago(2)))
        self.dwj.commit()

    def run_collector(self, http=None, dry_run=False, now=None):
        ctx = Ctx(now or self.now, http, dry_run)
        try:
            return collector.run(ctx), ctx
        finally:
            ctx.db.close()

    def spine(self):
        return proptech.connect()


# ─────────────────────────────────────────────────────────────────────────────

class TestContract(unittest.TestCase):

    def test_meta_validates_and_is_light_public(self):
        j = job.validate(collector.META, "collectors.proptech")
        self.assertEqual(j.weight, "light")
        self.assertEqual(j.data, "public")
        self.assertLessEqual(j.ram_mb, job.LIGHT_MAX_MB)

    def test_runs_after_acris(self):
        """acris reads the parcels this fills; the clocks must not overlap."""
        self.assertEqual(acris.META["schedule"], "30 6 * * *")
        self.assertEqual(collector.META["schedule"], "45 6 * * *")
        self.assertLessEqual(acris.META["timeout"], 15 * 60)

    def test_normalize_bbl(self):
        n = proptech.normalize_bbl
        self.assertEqual(n("1008010001.00000000"), "1008010001")
        self.assertEqual(n("1008010001"), "1008010001")
        self.assertEqual(n(1008010001), "1008010001")
        for bad in (None, "", "N/A", "9000010001", "0", "12345678901"):
            self.assertIsNone(n(bad), bad)


class TestThresholds(Phase3Case):

    def test_store_min_is_one_million_by_default(self):
        self.doc(1_000_000)
        self.doc(999_999)
        self.run_collector()
        c = self.spine()
        try:
            self.assertEqual(proptech.counts(c)["docs"], 1)
        finally:
            c.close()

    def test_store_min_is_independent_of_display_min(self):
        """SPINE_PROPTECH_MIN_AMOUNT is the brief's; it must not starve history."""
        os.environ["SPINE_PROPTECH_MIN_AMOUNT"] = "5000000"
        os.environ["SPINE_PROPTECH_STORE_MIN"] = "2000000"
        self.doc(1_500_000)
        self.doc(3_000_000)
        self.doc(6_000_000)
        self.run_collector()
        c = self.spine()
        try:
            self.assertEqual(proptech.counts(c)["docs"], 2)
        finally:
            c.close()

    def test_sat_and_asst_are_not_kept(self):
        self.doc(9_000_000, doc_type="SAT")
        self.doc(9_000_000, doc_type="ASST")
        self.doc(9_000_000, doc_type="MTGE")
        self.run_collector()
        c = self.spine()
        try:
            self.assertEqual(proptech.counts(c)["docs"], 1)
        finally:
            c.close()


class TestHistory(Phase3Case):

    def test_rerun_is_idempotent_and_first_seen_sticks(self):
        self.doc(2_000_000, bbls=("4000010001", "4000010002"))
        self.run_collector()
        c = self.spine()
        first = c.execute("SELECT first_seen, n_parcels FROM acris_docs").fetchone()
        c.close()
        self.run_collector(now=self.now + timedelta(days=1))
        c = self.spine()
        try:
            rows = c.execute("SELECT first_seen, n_parcels FROM acris_docs").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["first_seen"], first["first_seen"])
            self.assertEqual(rows[0]["n_parcels"], 2)
        finally:
            c.close()

    def test_history_outlives_darkweb_jobs(self):
        """The point of keeping a copy: darkweb-jobs may forget, Spine does not."""
        d = self.doc(2_000_000)
        self.run_collector()
        self.dwj.execute("DELETE FROM acris_master WHERE document_id=?", (d,))
        self.dwj.commit()
        self.run_collector()
        c = self.spine()
        try:
            self.assertEqual(proptech.counts(c)["docs"], 1)
        finally:
            c.close()


class TestRepeatSales(Phase3Case):

    def sales(self, since=None):
        self.run_collector()
        c = self.spine()
        try:
            return proptech.repeat_sales(c, since)
        finally:
            c.close()

    def test_a_resale_is_found_with_its_change(self):
        self.doc(2_000_000, date="2025-08-01")
        self.doc(3_000_000, date="2026-08-01")
        s = self.sales()
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["pct_change"], 50.0)
        self.assertEqual(s[0]["days_held"], 365)
        self.assertAlmostEqual(s[0]["pct_annualised"], 50.0, delta=0.2)

    def test_multi_parcel_deeds_are_not_priced(self):
        """One amount for five lots compared against one lot is a fake crash."""
        self.doc(10_000_000, bbls=("4000010001", "4000010002"), date="2025-01-01")
        self.doc(2_000_000, date="2026-08-01")
        self.assertEqual(self.sales(), [])

    def test_partial_interest_is_not_priced(self):
        self.doc(2_000_000, date="2025-01-01")
        self.doc(1_100_000, date="2026-08-01", pct=50.0)
        self.assertEqual(self.sales(), [])

    def test_short_hold_is_a_correction_not_a_resale(self):
        self.doc(2_000_000, date="2026-07-20")
        self.doc(2_100_000, date="2026-08-01")
        self.assertEqual(self.sales(), [])

    def test_mortgages_are_never_priced(self):
        self.doc(2_000_000, date="2025-01-01")
        self.doc(9_000_000, date="2026-08-01", doc_type="MTGE")
        self.assertEqual(self.sales(), [])

    def test_window_is_by_first_seen(self):
        self.doc(2_000_000, date="2025-08-01")
        self.doc(3_000_000, date="2026-08-01")
        future = (self.now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.sales(since=future), [])


class TestPlutoLookups(Phase3Case):

    def test_slice_first_then_api_then_absent(self):
        self.doc(5_000_000, bbls=("1000010001",))   # in the slice
        self.doc(4_000_000, bbls=("4000010001",))   # PLUTO has it
        self.doc(3_000_000, bbls=("4000010002",))   # PLUTO does not
        self.slice_parcel("1000010001")
        http = FakeHttp({"4000010001": pluto_rec("4000010001")})
        out, _ = self.run_collector(http)
        st = out["stats"]
        self.assertEqual((st["pluto_asked"], st["pluto_found"],
                          st["pluto_absent"]), (2, 1, 1))
        self.assertEqual(st["pluto_coverage_pct"], 66.7)
        c = self.spine()
        try:
            p = proptech.parcels_for(c, ["4000010001"])["4000010001"]
            self.assertEqual(p["origin"], "pluto")
            self.assertEqual(p["unitsres"], 40)
            self.assertEqual(p["numfloors"], 6.0)
        finally:
            c.close()

    def test_absent_is_not_asked_again_until_retry(self):
        self.doc(3_000_000, bbls=("4000010002",))
        http = FakeHttp()
        self.run_collector(http)
        self.assertEqual(len(http.calls), 1)
        self.run_collector(http, now=self.now + timedelta(days=1))
        self.assertEqual(len(http.calls), 1)
        self.run_collector(http, now=self.now + timedelta(
            days=proptech.ABSENT_RETRY_DAYS + 1))
        self.assertEqual(len(http.calls), 2)

    def test_a_failed_batch_marks_nothing_absent(self):
        """'Could not ask' is not 'not there'."""
        self.doc(3_000_000, bbls=("4000010002",))
        out, _ = self.run_collector(FakeHttp(fail=True))
        self.assertEqual(out["stats"]["pluto_absent"], 0)
        self.assertEqual(out["stats"]["pluto_failed_batches"], 1)
        http = FakeHttp({"4000010002": pluto_rec("4000010002")})
        out, _ = self.run_collector(http)
        self.assertEqual(out["stats"]["pluto_found"], 1)

    def test_an_outage_stops_after_three_failures(self):
        bbls = [f"40000{i:05d}" for i in range(1, 251)]   # 5 batches
        http = FakeHttp(fail=True)
        found, absent, failures = collector.fetch_pluto(http, bbls)
        self.assertEqual(failures, collector.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(http.calls), collector.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(absent, [])

    def test_budget_spends_on_the_largest_deals_first(self):
        os.environ["SPINE_PLUTO_FETCH_MAX"] = "1"
        self.doc(2_000_000, bbls=("4000010001",))
        self.doc(9_000_000, bbls=("4000010009",))
        http = FakeHttp()
        self.run_collector(http)
        self.assertIn("4000010009", http.calls[0]["$where"])
        self.assertNotIn("4000010001", http.calls[0]["$where"])

    def test_parse_pluto_shapes(self):
        p = collector.parse_pluto(pluto_rec("4000010001", yearbuilt="0",
                                            ownername="  X LLC "))
        self.assertEqual(p["bbl"], "4000010001")
        self.assertIsNone(p["yearbuilt"])
        self.assertEqual(p["ownername"], "X LLC")
        self.assertEqual(p["assesstot"], 3_000_000.0)


class TestSignals(Phase3Case):

    def test_resale_becomes_one_signal_and_reruns_dedup(self):
        self.doc(2_000_000, date="2025-08-01")
        d = self.doc(3_000_000, date="2026-08-01")
        http = FakeHttp({"4000010001": pluto_rec("4000010001", "9 MAIN ST")})
        out, _ = self.run_collector(http)
        self.assertEqual(out["stats"]["new"], 1)
        it = out["items"][0]
        self.assertEqual(it["kind"], "signal")
        self.assertEqual(it["key"], f"4000010001:{d}")
        self.assertIn("9 MAIN ST, Queens", it["title"])
        self.assertIn("+50%", it["title"])
        self.assertIn("$75/sqft", it["body"])
        out, _ = self.run_collector(http)
        self.assertEqual(out["stats"]["new"], 0)

    def test_importance_stays_under_interrupt_except_big_and_large(self):
        base = {"resold_for": 3_000_000, "pct_change": 10.0}
        self.assertLess(collector.importance(base), collector.INTERRUPT_AT)
        big_move = {"resold_for": 30_000_000, "pct_change": 80.0}
        self.assertLess(collector.importance(big_move), collector.INTERRUPT_AT)
        huge = {"resold_for": 60_000_000, "pct_change": -40.0}
        self.assertGreaterEqual(collector.importance(huge), collector.INTERRUPT_AT)

    def test_dry_run_writes_nothing(self):
        self.doc(2_000_000, date="2025-08-01")
        self.doc(3_000_000, date="2026-08-01")
        http = FakeHttp()
        out, _ = self.run_collector(http, dry_run=True)
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(http.calls, [])
        c = sqlite3.connect(os.environ["SPINE_DB"])
        try:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("acris_docs", tables)
            if "items" in tables:
                self.assertEqual(c.execute(
                    "SELECT COUNT(*) FROM items").fetchone()[0], 0)
        finally:
            c.close()


class TestAcrisEnrichment(Phase3Case):

    def acris_items(self):
        conn = sqlite3.connect(
            f"file:{os.path.join(self.root, 'dwj/data/proptech.db')}?mode=ro",
            uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = acris.select(conn, self.hours_ago(72), 5_000_000)
        finally:
            conn.close()
        c = self.spine()
        try:
            parcels = proptech.parcels_for(c, [r["bbl"] for r in rows])
        finally:
            c.close()
        return acris.build_items(rows, parcels)

    def test_outer_borough_deed_gets_its_building(self):
        self.doc(6_000_000, bbls=("4000010001",))
        self.run_collector(FakeHttp({"4000010001": pluto_rec("4000010001")}))
        it = self.acris_items()[0]
        self.assertIn("1 TEST STREET, Queens", it["title"])
        self.assertEqual(it["data"]["unitsres"], 40)
        self.assertTrue(it["data"]["in_pluto"])
        self.assertIn("class D1", it["body"])

    def test_slice_value_wins_over_spine_copy(self):
        self.doc(6_000_000, bbls=("1000010001",))
        self.slice_parcel("1000010001", address="FROM THE SLICE")
        self.run_collector()
        c = self.spine()
        proptech.upsert_parcels(c, [{"bbl": "1000010001",
                                     "address": "STALE COPY"}], origin="pluto")
        c.close()
        self.assertIn("FROM THE SLICE", self.acris_items()[0]["title"])

    def test_without_parcels_behaviour_is_unchanged(self):
        self.doc(6_000_000, bbls=("4000010001",))
        conn = sqlite3.connect(
            f"file:{os.path.join(self.root, 'dwj/data/proptech.db')}?mode=ro",
            uri=True)
        conn.row_factory = sqlite3.Row
        try:
            items = acris.build_items(acris.select(conn, self.hours_ago(72), 0))
        finally:
            conn.close()
        self.assertFalse(items[0]["data"]["in_pluto"])
        self.assertIn("not yet looked up", items[0]["body"])


if __name__ == "__main__":
    unittest.main()
