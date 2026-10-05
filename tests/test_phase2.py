"""
Phase 2 tests — the item store.

The store's whole promise is that collectors can be dumb: emit everything
you see, every run, and let the table sort it out. Every test here is really
testing one of the three things that makes that promise hold — dedup,
status preservation, and validation on the way in.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import store                                    # noqa: E402
from core.store import ItemError, Store                   # noqa: E402

UTC = timezone.utc


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old = dict(os.environ)
        os.environ["SPINE_DB"] = os.path.join(self._dir.name, "spine.db")
        self.s = Store(source="test")

    def tearDown(self):
        self.s.close()
        os.environ.clear()
        os.environ.update(self._old)
        self._dir.cleanup()

    def item(self, **over):
        base = {"key": "k1", "kind": "signal", "title": "a thing",
                "body": "details", "data": {"n": 1}}
        base.update(over)
        return base


class TestDedup(StoreCase):

    def test_same_key_twice_is_one_row(self):
        """The promise: a collector re-runs, re-emits, nothing duplicates."""
        self.assertEqual(self.s.put(self.item())["new"], 1)
        r = self.s.put(self.item())
        self.assertEqual((r["new"], r["updated"]), (0, 1))
        self.assertEqual(self.s.total(), 1)

    def test_different_sources_may_share_a_key(self):
        """Uniqueness is (source, key). Two collectors both keying on '2026'
        must not collide."""
        self.s.put(self.item(), source="a")
        self.s.put(self.item(), source="b")
        self.assertEqual(self.s.total(), 2)

    def test_update_refreshes_mutable_facts(self):
        self.s.put(self.item(title="old", data={"price": 1}))
        self.s.put(self.item(title="new", data={"price": 2}))
        row = self.s.query()[0]
        self.assertEqual(row["title"], "new")
        self.assertIn('"price": 2', row["data_json"])

    def test_first_seen_timestamp_survives_updates(self):
        """A Polymarket contract that moves every 30 minutes must not keep
        resetting its own age, or 'what is new today' becomes meaningless."""
        self.s.put(self.item())
        first = self.s.query()[0]["ts"]
        self.s.conn.execute("UPDATE items SET ts='2020-01-01T00:00:00Z'")
        self.s.conn.commit()
        self.s.put(self.item(title="changed"))
        row = self.s.query()[0]
        self.assertEqual(row["ts"], "2020-01-01T00:00:00Z")
        self.assertNotEqual(row["updated_ts"], "2020-01-01T00:00:00Z")
        self.assertNotEqual(first, "")

    def test_acted_items_do_not_return_to_the_queue(self):
        """THE important one. If a re-emit reset status, everything Nick had
        already dealt with would reappear in tomorrow's brief, and he would
        stop reading the brief."""
        self.s.put(self.item())
        rid = self.s.query()[0]["id"]
        self.s.mark(rid, "acted")
        self.s.put(self.item(title="re-emitted by a re-run"))
        row = self.s.query()[0]
        self.assertEqual(row["status"], "acted")
        self.assertIsNotNone(row["acted_at"])
        self.assertEqual(row["title"], "re-emitted by a re-run")
        self.assertEqual(self.s.unacted(), [])


class TestValidation(StoreCase):

    def test_key_is_required(self):
        with self.assertRaises(ItemError) as e:
            self.s.put({"title": "no key"})
        self.assertIn("stable string 'key'", str(e.exception))

    def test_kind_and_status_are_closed_sets(self):
        for bad in ({"key": "k", "kind": "thought"},
                    {"key": "k", "status": "maybe"}):
            with self.assertRaises(ItemError):
                self.s.put(bad)

    def test_importance_is_bounded(self):
        for bad in (-1, 101, "high", True):
            with self.assertRaises(ItemError):
                self.s.put(self.item(importance=bad))
        self.s.put(self.item(importance=95))

    def test_unserialisable_data_is_caught_on_the_way_in(self):
        """A bad row found at read time is found by the assistant, at 07:00,
        in front of Nick."""
        with self.assertRaises(ItemError):
            self.s.put(self.item(data={"conn": object()}))

    def test_data_accepts_a_json_string_too(self):
        self.s.put(self.item(data='{"already": "json"}'))
        self.assertIn("already", self.s.query()[0]["data_json"])

    def test_a_bad_item_in_a_batch_writes_nothing(self):
        """Validate the whole batch before writing any of it, or a collector
        fails halfway and leaves the store in a state nobody designed."""
        with self.assertRaises(ItemError):
            self.s.put([self.item(key="ok1"), {"no": "key"},
                        self.item(key="ok2")])
        self.assertEqual(self.s.total(), 0)


class TestQuerying(StoreCase):

    def seed(self):
        self.s.put([
            self.item(key="a", kind="deal", importance=90, title="big"),
            self.item(key="b", kind="signal", importance=40, title="small"),
            self.item(key="c", kind="alert", importance=95, title="urgent"),
        ])

    def test_orders_by_importance(self):
        self.seed()
        self.assertEqual([r["title"] for r in self.s.query()][:2],
                         ["urgent", "big"])

    def test_filters_compose(self):
        self.seed()
        self.assertEqual(len(self.s.query(kind="deal")), 1)
        self.assertEqual(len(self.s.query(kind=("deal", "alert"))), 2)
        self.assertEqual(len(self.s.query(min_importance=90)), 2)

    def test_unacted_is_the_assistants_inbox(self):
        self.seed()
        self.assertEqual(len(self.s.unacted()), 3)
        self.s.mark([r["id"] for r in self.s.query(kind="deal")], "acted")
        self.assertEqual(len(self.s.unacted()), 2)

    def test_counts_and_sources(self):
        self.seed()
        self.assertEqual(self.s.counts()["new"], 3)
        self.assertEqual(self.s.by_source()[0][0], "test")


class TestRetention(StoreCase):

    def test_prunes_stale_signals_but_never_acted_items(self):
        """Acted rows are the record of what Nick actually did. They cost
        nothing to keep and would hurt to lose."""
        old = (datetime.now(UTC) - timedelta(days=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.s.put([self.item(key="stale", kind="signal"),
                    self.item(key="done", kind="signal"),
                    self.item(key="deed", kind="deal")])
        self.s.conn.execute("UPDATE items SET ts=?", (old,))
        self.s.conn.commit()
        self.s.mark([r["id"] for r in self.s.query(kind="signal")
                     if r["key"] == "done"], "acted")

        removed = self.s.prune()
        self.assertEqual(removed.get("signal"), 1)
        keys = {r["key"] for r in self.s.query()}
        self.assertEqual(keys, {"done", "deed"})

    def test_deals_and_facts_are_kept_forever(self):
        """A recorded deed is a permanent fact about a building."""
        self.assertEqual(store.RETENTION_DAYS["deal"], 0)
        self.assertEqual(store.RETENTION_DAYS["fact"], 0)

    def test_dry_run_reports_without_deleting(self):
        old = (datetime.now(UTC) - timedelta(days=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.s.put(self.item(key="stale", kind="signal"))
        self.s.conn.execute("UPDATE items SET ts=?", (old,))
        self.s.conn.commit()
        self.assertEqual(self.s.prune(dry_run=True).get("signal"), 1)
        self.assertEqual(self.s.total(), 1)


class TestSharedDatabase(StoreCase):

    def test_items_and_costs_live_in_one_file(self):
        """One database, several tables. The assistant will want to join
        'what happened' against 'what it cost to find out'."""
        from core import costs
        costs.record("j", "bulk", "mock", "mock")
        self.s.put(self.item())
        tables = {r[0] for r in self.s.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("items", tables)
        self.assertIn("costs", tables)


class TestCollectorIntegration(unittest.TestCase):
    """The end-to-end proof: registry -> runner -> ctx.db -> items.

    This used to run against `heartbeat`, which was the only collector that
    existed. As of Phase 2b heartbeat deliberately writes no items (see its
    docstring — 97 telemetry rows were 100% of the store), so the chain is
    now proved with `acris` against a synthetic proptech.db instead.

    Re-pointed rather than deleted: the thing under test is the framework
    plumbing, not the collector. Losing the coverage because its subject
    changed would be the wrong trade.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old = dict(os.environ)
        os.environ["SPINE_DB"] = os.path.join(self._dir.name, "spine.db")

        data = os.path.join(self._dir.name, "dwj", "data")
        os.makedirs(data)
        os.environ["SPINE_DWJ_ROOT"] = os.path.dirname(data)

        import sqlite3
        from datetime import datetime, timedelta, timezone
        recorded = (datetime.now(timezone.utc) - timedelta(hours=2)
                    ).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(os.path.join(data, "proptech.db"))
        conn.executescript("""
            CREATE TABLE acris_legals (
              document_id TEXT, borough TEXT, block INTEGER, lot INTEGER,
              bbl TEXT, street_number TEXT, street_name TEXT, unit TEXT,
              property_type TEXT,
              PRIMARY KEY (document_id, bbl, unit));
            CREATE TABLE acris_master (
              document_id TEXT PRIMARY KEY, doc_type TEXT, document_date TEXT,
              recorded_datetime TEXT, document_amt REAL, percent_trans REAL,
              fetched_ts TEXT);
            CREATE TABLE pluto (
              bbl TEXT PRIMARY KEY, borough TEXT, address TEXT, zipcode TEXT,
              zonedist1 TEXT, bldgclass TEXT, landuse TEXT, unitsres INTEGER,
              unitstotal INTEGER, yearbuilt INTEGER, numfloors REAL,
              bldgarea REAL, lotarea REAL, assessland REAL, assesstot REAL,
              ownername TEXT, latitude REAL, longitude REAL, fetched_ts TEXT);
        """)
        conn.execute("INSERT INTO acris_master VALUES (?,?,?,?,?,?,?)",
                     ("2026000999001", "DEED", "2026-09-01", recorded,
                      7_500_000.0, 100.0, recorded))
        conn.execute("INSERT INTO acris_legals VALUES (?,?,?,?,?,?,?,?,?)",
                     ("2026000999001", "1", 560, 42, "1005600042", "1",
                      "TEST STREET", "", "CONDO"))
        conn.commit()
        conn.close()

        # run_job writes var/state/<job_id>.json, and these tests run the REAL
        # acris and heartbeat jobs — so without this the suite overwrites the
        # operational state that bin/status and bin/darkweb display, and the
        # console shows a synthetic run from whenever the tests last ran.
        # state_dir() is derived from the checkout and deliberately not
        # configurable (core.paths), which is the right call for production and
        # exactly why the redirect belongs here instead of in an env var.
        from core import registry
        self._write_state = registry.write_state
        registry.write_state = lambda job_id, data: None

    def tearDown(self):
        from core import registry
        registry.write_state = self._write_state
        os.environ.clear()
        os.environ.update(self._old)
        self._dir.cleanup()

    def test_a_collector_writes_a_real_item_through_the_runner(self):
        """registry -> runner -> ctx.db -> items, with a real collector."""
        from core import registry, runner
        jobs, errors = registry.discover()
        self.assertEqual(errors, [])
        job = next(j for j in jobs if j.id == "acris")

        code, state = runner.run_job(job, log=lambda *a, **k: None)
        self.assertEqual(code, runner.EXIT_OK, state.get("error"))

        s = Store()
        rows = s.query(source="acris")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "deal")
        self.assertEqual(rows[0]["status"], "new")
        s.close()

    def test_rerunning_a_collector_does_not_duplicate(self):
        from core import registry, runner
        jobs, _ = registry.discover()
        job = next(j for j in jobs if j.id == "acris")
        for _ in range(3):
            runner.run_job(job, log=lambda *a, **k: None)
        s = Store()
        self.assertEqual(len(s.query(source="acris")), 1)
        s.close()

    def test_heartbeat_runs_green_and_writes_no_items(self):
        """Phase 2b: telemetry left the store, but the chain must still run.

        A collector returning zero items is a healthy outcome, not a
        failure — the runner has to treat it that way or every quiet
        morning looks like a broken box.
        """
        from core import registry, runner
        jobs, _ = registry.discover()
        hb = next(j for j in jobs if j.id == "heartbeat")

        code, state = runner.run_job(hb, log=lambda *a, **k: None)
        self.assertEqual(code, runner.EXIT_OK, state.get("error"))

        s = Store()
        self.assertEqual(s.query(source="heartbeat"), [])
        s.close()


if __name__ == "__main__":
    unittest.main()


class TestDataCoercion(StoreCase):
    """`default=str` would have made every test above pass while quietly
    storing "<socket object at 0x7f...>" as data. Accept the types a
    collector legitimately has; raise on the rest."""

    def test_accepts_the_types_collectors_actually_produce(self):
        from decimal import Decimal
        self.s.put(self.item(data={
            "when": datetime(2026, 9, 4, tzinfo=UTC),
            "price": Decimal("1234.56"),
            "tags": {"b", "a"},
            "raw": b"bytes",
        }))
        blob = self.s.query()[0]["data_json"]
        for expected in ("2026-09-04", "1234.56", '"a"', "bytes"):
            self.assertIn(expected, blob)

    def test_refuses_a_live_object(self):
        with self.assertRaises(ItemError) as e:
            self.s.put(self.item(data={"conn": self.s.conn}))
        self.assertIn("not storable", str(e.exception))
