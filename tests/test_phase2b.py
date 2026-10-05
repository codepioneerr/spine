"""
Phase 2b tests — the read-only bridge into darkweb-jobs.

Every database here is synthetic, built from the schemas dumped off the Dell
on Sept 7 2026. That is a real limit and worth stating: these tests prove
the code is self-consistent, not that it is right about the world. The tool
that checks the second thing is bin/compare-migration, and it needs a week
of real output before Phase 3 should be trusted.

What is actually being tested:

  1. read-only is enforced by SQLite, not by our good intentions
  2. keys are stable across runs, because dedup is the whole bargain
  3. the lookback window exists, because without it the first run floods
  4. the PLUTO join is LEFT, because INNER would look like a quiet week
  5. heartbeat writes no items, because that was the Phase 2a mistake
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import bridge                                    # noqa: E402
from core.store import Store                               # noqa: E402
from collectors import acris, eventbot, polymarket         # noqa: E402

UTC = timezone.utc

# Schemas copied verbatim from darkweb-jobs-schemas.txt (Sept 7 2026).
# Verbatim matters: a paraphrased schema tests a database that does not
# exist, which is exactly how the previous Phase 2b attempt went wrong.
PROPTECH_SCHEMA = """
CREATE TABLE acris_legals (
  document_id TEXT, borough TEXT, block INTEGER, lot INTEGER, bbl TEXT,
  street_number TEXT, street_name TEXT, unit TEXT, property_type TEXT,
  PRIMARY KEY (document_id, bbl, unit));
CREATE TABLE acris_master (
  document_id TEXT PRIMARY KEY, doc_type TEXT, document_date TEXT,
  recorded_datetime TEXT, document_amt REAL, percent_trans REAL,
  fetched_ts TEXT);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE pluto (
  bbl TEXT PRIMARY KEY, borough TEXT, address TEXT, zipcode TEXT,
  zonedist1 TEXT, bldgclass TEXT, landuse TEXT, unitsres INTEGER,
  unitstotal INTEGER, yearbuilt INTEGER, numfloors REAL, bldgarea REAL,
  lotarea REAL, assessland REAL, assesstot REAL, ownername TEXT,
  latitude REAL, longitude REAL, fetched_ts TEXT);
CREATE TABLE runs (ts TEXT PRIMARY KEY, kind TEXT, rows INTEGER, note TEXT);
"""

PREDICTION_SCHEMA = """
CREATE TABLE jumps (
  ts TEXT NOT NULL, market_id TEXT NOT NULL, question TEXT, prev_price REAL,
  new_price REAL, delta REAL, volume24h REAL, political INTEGER,
  consumed INTEGER DEFAULT 0, PRIMARY KEY (ts, market_id));
CREATE TABLE snapshots (
  ts TEXT NOT NULL, market_id TEXT NOT NULL, slug TEXT, question TEXT,
  event_title TEXT, yes_price REAL, best_bid REAL, best_ask REAL,
  volume24h REAL, liquidity REAL, end_date TEXT, political INTEGER,
  PRIMARY KEY (ts, market_id));
"""

EVENTBOT_SCHEMA = """
CREATE TABLE positions (
  id INTEGER PRIMARY KEY, asset TEXT, rule TEXT, idea_id INTEGER,
  executor TEXT, status TEXT, qty REAL, notional REAL, entry_price REAL,
  entry_ts TEXT, stop REAL, target REAL, max_exit_ts TEXT, exit_price REAL,
  exit_ts TEXT, pnl REAL, exit_reason TEXT, broker_order_id TEXT);
CREATE TABLE signals (
  id INTEGER PRIMARY KEY, ts TEXT, seen_ts TEXT, source TEXT,
  source_id TEXT, text TEXT, url TEXT, sentiment INTEGER, n_ideas INTEGER,
  UNIQUE(source, source_id));
"""


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class BridgeCase(unittest.TestCase):
    """Builds a fake darkweb-jobs tree and points SPINE_DWJ_ROOT at it."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old = dict(os.environ)
        self.root = self._dir.name
        os.makedirs(os.path.join(self.root, "dwj", "data"))
        os.environ["SPINE_DWJ_ROOT"] = os.path.join(self.root, "dwj")
        os.environ["SPINE_DB"] = os.path.join(self.root, "spine.db")
        self.now = datetime.now(UTC)
        self.store = Store(source="test")

    def tearDown(self):
        self.store.close()
        os.environ.clear()
        os.environ.update(self._old)
        self._dir.cleanup()

    def make_db(self, name: str, schema: str) -> sqlite3.Connection:
        path = os.path.join(self.root, "dwj", "data", f"{name}.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(schema)
        conn.commit()
        return conn

    def hours_ago(self, h: float) -> str:
        return iso(self.now - timedelta(hours=h))


# ─────────────────────────────────────────────────────────────────────────────
# the bridge itself
# ─────────────────────────────────────────────────────────────────────────────

class TestBridge(BridgeCase):

    def test_missing_database_names_the_path(self):
        """An unavailable source must fail loudly, not emit zero items.

        Zero items and a missing file look identical in the brief — both are
        a quiet morning. Only one of them is.
        """
        with self.assertRaises(bridge.BridgeUnavailable) as cm:
            bridge.connect("proptech")
        self.assertIn("proptech.db", str(cm.exception))
        self.assertIn(self.root, str(cm.exception))

    def test_connection_is_read_only(self):
        """SQLite enforces this, not our discipline.

        eventbot writes to these files every five minutes. A read-write
        handle from Spine is a lock held at the wrong moment and a bug away
        from corrupting data Spine does not own.
        """
        self.make_db("prediction", PREDICTION_SCHEMA).close()
        conn = bridge.connect("prediction")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO jumps (ts, market_id) "
                             "VALUES ('x', 'y')")
        finally:
            conn.close()

    def test_since_iso_matches_source_timestamp_format(self):
        """The comparison is a string comparison, so the format must match
        what darkweb-jobs writes or the window silently matches nothing."""
        cutoff = bridge.since_iso(24, now=self.now)
        self.assertRegex(cutoff, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertLess(cutoff, iso(self.now))

    def test_env_int_falls_back_on_garbage(self):
        """A typo'd threshold in .env must not crash a 06:30 run."""
        os.environ["SPINE_TEST_INT"] = "not-a-number"
        self.assertEqual(bridge.env_int("SPINE_TEST_INT", 42), 42)
        os.environ["SPINE_TEST_INT"] = "1500000"
        self.assertEqual(bridge.env_int("SPINE_TEST_INT", 42), 1_500_000)


# ─────────────────────────────────────────────────────────────────────────────
# acris
# ─────────────────────────────────────────────────────────────────────────────

class TestAcris(BridgeCase):

    def seed(self, conn, *, doc="2026000123456", amt=5_000_000.0,
             hours=2, doc_type="DEED", with_legal=True, with_pluto=True,
             bbl="1005600042"):
        conn.execute(
            "INSERT INTO acris_master VALUES (?,?,?,?,?,?,?)",
            (doc, doc_type, "2026-09-01", self.hours_ago(hours), amt, 100.0,
             self.hours_ago(hours)))
        if with_legal:
            conn.execute(
                "INSERT INTO acris_legals VALUES (?,?,?,?,?,?,?,?,?)",
                (doc, "1", 560, 42, bbl, "123", "BROADWAY", "", "CONDO"))
        if with_pluto:
            conn.execute(
                "INSERT INTO pluto VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bbl, "MN", "123 BROADWAY", "10007", "C6-4", "D4", "03",
                 24, 26, 1928, 12.0, 90000.0, 8000.0, 1_000_000.0,
                 9_000_000.0, "BROADWAY OWNER LLC", 40.71, -74.01,
                 self.hours_ago(hours)))
        conn.commit()

    def read(self, min_amount=1_000_000, hours=None):
        conn = bridge.connect("proptech")
        try:
            since = bridge.since_iso(hours or acris.LOOKBACK_HOURS,
                                     now=self.now)
            return acris.build_items(acris.select(conn, since, min_amount))
        finally:
            conn.close()

    def test_document_id_is_the_key(self):
        """Stable across runs for the same real-world thing. CLAUDE.md 7a."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c)
        c.close()
        items = self.read()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], "2026000123456")

    def test_rerun_updates_rather_than_duplicates(self):
        """The bargain that lets collectors be dumb."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c)
        c.close()
        s = Store(source="acris")
        try:
            first = s.put(self.read())
            second = s.put(self.read())
            self.assertEqual(first["new"], 1)
            self.assertEqual(second["new"], 0)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(s.total(), 1)
        finally:
            s.close()

    def test_below_threshold_is_excluded(self):
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="cheap", amt=250_000.0)
        c.close()
        self.assertEqual(self.read(min_amount=1_000_000), [])
        self.assertEqual(len(self.read(min_amount=100_000)), 1)

    def test_outside_lookback_is_excluded(self):
        """No window means the first run emits the entire backfill."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="old", hours=24 * 20)
        c.close()
        self.assertEqual(self.read(), [])

    def test_pluto_join_is_left(self):
        """A Queens deed is outside the MN/BK PLUTO slice. It must still
        appear, with the building fields empty — an INNER join here would
        look like a quiet week rather than a filter."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="queens1", with_pluto=False, bbl="4001230045")
        c.close()
        items = self.read()
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["data"]["in_pluto"])
        self.assertIn("PLUTO slice", items[0]["body"])

    def test_document_with_no_legal_record_still_appears(self):
        """ACRIS master and legals are fetched separately and legals can lag.
        A deed without its BBL yet is still a deed."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="nolegal", with_legal=False, with_pluto=False)
        c.close()
        items = self.read()
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["data"]["bbl"])

    def test_multi_lot_deed_emits_once(self):
        """A building sold with three lots has three legal rows. The join
        multiplies them; the item must not."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="multi", bbl="1005600042")
        for lot, bbl in ((43, "1005600043"), (44, "1005600044")):
            c.execute("INSERT INTO acris_legals VALUES (?,?,?,?,?,?,?,?,?)",
                      ("multi", "1", 560, lot, bbl, "123", "BROADWAY", "",
                       "CONDO"))
        c.commit()
        c.close()
        items = self.read()
        self.assertEqual(len(items), 1)

    def test_administrative_doc_types_are_skipped(self):
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c, doc="sat1", doc_type="SAT")
        c.close()
        self.assertEqual(self.read(), [])

    def test_importance_tracks_money_and_stays_in_range(self):
        for amt in (0, 1_500_000, 6_000_000, 25_000_000, 80_000_000):
            for units in (None, 4, 30):
                v = acris.importance(amt, units)
                self.assertGreaterEqual(v, 0)
                self.assertLessEqual(v, 100)
        self.assertGreater(acris.importance(60_000_000, 40),
                           acris.importance(1_200_000, None))

    def test_only_the_biggest_deals_can_interrupt(self):
        """Above 80 is reserved for 'worth interrupting Nick about'. A
        routine $2M closing must not qualify."""
        self.assertLess(acris.importance(2_000_000, 8), 80)
        self.assertGreaterEqual(acris.importance(60_000_000, 40), 80)

    def test_items_validate_against_the_store(self):
        """Validation on the way in is the point; prove these pass it."""
        c = self.make_db("proptech", PROPTECH_SCHEMA)
        self.seed(c)
        c.close()
        s = Store(source="acris")
        try:
            s.put(self.read())
            row = s.conn.execute("SELECT * FROM items").fetchone()
            self.assertEqual(row["kind"], "deal")
            self.assertEqual(row["source"], "acris")
            self.assertIn("BROADWAY", row["title"])
        finally:
            s.close()


# ─────────────────────────────────────────────────────────────────────────────
# polymarket
# ─────────────────────────────────────────────────────────────────────────────

class TestPolymarket(BridgeCase):

    def seed(self, conn, *, mid="0x1234", hours=1, delta=0.12,
             volume=250_000.0, question="Will X happen by 2027?"):
        ts = self.hours_ago(hours)
        conn.execute(
            "INSERT INTO jumps (ts, market_id, question, prev_price, "
            "new_price, delta, volume24h, political) VALUES (?,?,?,?,?,?,?,?)",
            (ts, mid, question, 0.40, 0.40 + delta, delta, volume, 1))
        conn.commit()
        return ts

    def read(self, min_volume=50_000):
        conn = bridge.connect("prediction")
        try:
            since = bridge.since_iso(polymarket.LOOKBACK_HOURS, now=self.now)
            return polymarket.build_items(
                polymarket.select(conn, since, min_volume))
        finally:
            conn.close()

    def test_key_includes_the_timestamp(self):
        """A jump is an event, not a market. Keying on market_id alone would
        make tomorrow's move overwrite today's and reset its own age."""
        c = self.make_db("prediction", PREDICTION_SCHEMA)
        ts = self.seed(c)
        c.close()
        items = self.read()
        self.assertEqual(items[0]["key"], f"jump:0x1234:{ts}")

    def test_same_market_jumping_twice_is_two_items(self):
        c = self.make_db("prediction", PREDICTION_SCHEMA)
        self.seed(c, hours=1)
        self.seed(c, hours=3)
        c.close()
        self.assertEqual(len(self.read()), 2)

    def test_thin_markets_are_excluded(self):
        c = self.make_db("prediction", PREDICTION_SCHEMA)
        self.seed(c, volume=20_000.0)
        c.close()
        self.assertEqual(self.read(), [])
        self.assertEqual(len(self.read(min_volume=10_000)), 1)

    def test_lookback_prevents_the_first_run_flood(self):
        """Three thousand historical jumps into a store holding 97 items is
        the failure this window exists to prevent."""
        c = self.make_db("prediction", PREDICTION_SCHEMA)
        for i in range(50):
            self.seed(c, mid=f"old{i}", hours=48 + i)
        self.seed(c, mid="recent", hours=1)
        c.close()
        items = self.read()
        self.assertEqual(len(items), 1)
        self.assertIn("recent", items[0]["key"])

    def test_snapshots_are_never_read(self):
        """57,216 rows of time series do not belong in a queue. If a future
        edit points this collector at snapshots, this fails."""
        self.assertNotIn("snapshots", polymarket.SQL.lower())

    def test_importance_rewards_size_and_liquidity(self):
        big = polymarket.importance(0.35, 2_000_000)
        small = polymarket.importance(0.09, 60_000)
        self.assertGreater(big, small)
        self.assertLessEqual(big, 100)
        self.assertGreaterEqual(small, 0)

    def test_items_validate_against_the_store(self):
        c = self.make_db("prediction", PREDICTION_SCHEMA)
        self.seed(c)
        c.close()
        s = Store(source="polymarket")
        try:
            s.put(self.read())
            row = s.conn.execute("SELECT * FROM items").fetchone()
            self.assertEqual(row["kind"], "signal")
        finally:
            s.close()


# ─────────────────────────────────────────────────────────────────────────────
# eventbot
# ─────────────────────────────────────────────────────────────────────────────

class TestEventbot(BridgeCase):

    def seed(self, conn, *, pid=1, status="open", hours=2, pnl=None,
             exit_ts=None, asset="BTC"):
        conn.execute(
            "INSERT INTO positions (id, asset, rule, idea_id, executor, "
            "status, qty, notional, entry_price, entry_ts, stop, target, "
            "max_exit_ts, exit_price, exit_ts, pnl, exit_reason, "
            "broker_order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, asset, "jump_follow", 10, "paper", status, 1.0, 500.0,
             100.0, self.hours_ago(hours), 95.0, 110.0, None,
             105.0 if exit_ts else None, exit_ts, pnl,
             "target" if exit_ts else None, None))
        conn.commit()

    def read(self):
        conn = bridge.connect("eventbot")
        try:
            since = bridge.since_iso(eventbot.LOOKBACK_HOURS, now=self.now)
            return eventbot.build_items(eventbot.select(conn, since))
        finally:
            conn.close()

    def test_position_key_survives_open_to_closed(self):
        """The same position must be one item through its whole life, so
        `ts` keeps meaning when it opened."""
        c = self.make_db("eventbot", EVENTBOT_SCHEMA)
        self.seed(c, pid=7, status="open")
        c.close()
        s = Store(source="eventbot")
        try:
            s.put(self.read())
            first = s.conn.execute("SELECT ts, title FROM items").fetchone()
            self.assertEqual(first["title"][:4], "open")

            c = sqlite3.connect(bridge.db_file("eventbot"))
            c.execute("UPDATE positions SET status='closed', pnl=42.0, "
                      "exit_ts=?, exit_reason='target' WHERE id=7",
                      (self.hours_ago(0.5),))
            c.commit()
            c.close()

            s.put(self.read())
            self.assertEqual(s.total(), 1)
            after = s.conn.execute("SELECT ts, title FROM items").fetchone()
            self.assertEqual(after["ts"], first["ts"])
            self.assertIn("closed", after["title"])
        finally:
            s.close()

    def test_open_positions_ignore_the_lookback(self):
        """A position opened three weeks ago and still live is still live."""
        c = self.make_db("eventbot", EVENTBOT_SCHEMA)
        self.seed(c, pid=1, status="open", hours=24 * 21)
        c.close()
        self.assertEqual(len(self.read()), 1)

    def test_old_closed_positions_are_excluded(self):
        c = self.make_db("eventbot", EVENTBOT_SCHEMA)
        self.seed(c, pid=2, status="closed", hours=24 * 21,
                  exit_ts=self.hours_ago(24 * 20), pnl=-11.0)
        c.close()
        self.assertEqual(self.read(), [])

    def test_losses_outrank_wins(self):
        """The stopped-out trade is where the rule was wrong."""
        c = self.make_db("eventbot", EVENTBOT_SCHEMA)
        self.seed(c, pid=1, status="closed", exit_ts=self.hours_ago(1),
                  pnl=-30.0, asset="ETH")
        self.seed(c, pid=2, status="closed", exit_ts=self.hours_ago(1),
                  pnl=30.0, asset="SOL")
        c.close()
        by_asset = {i["data"]["asset"]: i["importance"] for i in self.read()}
        self.assertGreater(by_asset["ETH"], by_asset["SOL"])

    def test_signals_table_is_not_bridged(self):
        """Nick decided eventbot's digest and Spine's brief run side by side.
        Bridging signals would put the same headlines in both."""
        self.assertNotIn("signals", eventbot.SQL.lower())
        self.assertNotIn("ideas", eventbot.SQL.lower())

    def test_positions_are_private(self):
        """The one bridged collector that is not public data."""
        self.assertEqual(eventbot.META["data"], "private")
        self.assertEqual(acris.META["data"], "public")
        self.assertEqual(polymarket.META["data"], "public")


# ─────────────────────────────────────────────────────────────────────────────
# the heartbeat fix
# ─────────────────────────────────────────────────────────────────────────────

class TestHeartbeatWritesNoItems(BridgeCase):

    def test_heartbeat_emits_nothing(self):
        """The Phase 2a mistake, now a test.

        97 telemetry rows were 100% of the item store. A brief selecting
        the top 25 unacted items would have been 25 heartbeats.
        """
        from collectors import heartbeat

        logged = []

        class FakeCtx:
            now = datetime.now(UTC)
            dry_run = True
            log = staticmethod(lambda *a: logged.append(a))

            @property
            def db(self):
                raise AssertionError(
                    "heartbeat touched ctx.db — telemetry is not a queue item")

        result = heartbeat.run(FakeCtx())
        self.assertEqual(result["items"], [])
        self.assertIn("load1", result["stats"])

    def test_vitals_are_still_collected(self):
        """Reverting the sink must not remove the signal — bin/darkweb
        still needs to show that the box is breathing."""
        from collectors import heartbeat
        v = heartbeat.vitals()
        for field in ("host", "kernel", "load", "cpus"):
            self.assertIn(field, v)


class TestPlaceholderAddressParts(unittest.TestCase):
    """ACRIS writes placeholders instead of leaving fields empty.

    265 of 28407 legals carry the literal string "N/A" as street_number. The
    old address_of treated any truthy value as real, so the brief showed
    "N/A 16 AVENUE, Brooklyn" on 2026-09-30. PLUTO could not cover for it:
    PLUTO_BOROS loads Manhattan and Brooklyn only, so every one of those rows
    joined to a NULL PLUTO address and fell through to the ACRIS parts.
    """

    def _row(self, **kw):
        base = {"address": None, "street_number": None, "street_name": None,
                "unit": None, "bbl": "3026390006"}
        base.update(kw)
        return base

    def test_the_literal_na_is_dropped_from_the_line(self):
        row = self._row(street_number="N/A", street_name="NORTH 14 STREET")
        self.assertEqual(acris.address_of(row), "NORTH 14 STREET")

    def test_placeholders_are_matched_case_folded(self):
        for probe in ("N/A", "n/a", "N/a", " NA "):
            row = self._row(street_number=probe, street_name="BATH AVENUE")
            self.assertEqual(acris.address_of(row), "BATH AVENUE", probe)

    def test_a_real_street_number_still_survives(self):
        """The fix must not eat legitimate values. This is the one that would
        make the change worse than the bug."""
        row = self._row(street_number="158-162", street_name="WEST 25TH STREET")
        self.assertEqual(acris.address_of(row), "158-162 WEST 25TH STREET")

    def test_a_placeholder_unit_adds_no_hash(self):
        row = self._row(street_number="12", street_name="POST COURT", unit="N/A")
        self.assertEqual(acris.address_of(row), "12 POST COURT")

    def test_a_real_unit_is_still_appended(self):
        row = self._row(street_number="12", street_name="POST COURT", unit="4B")
        self.assertEqual(acris.address_of(row), "12 POST COURT #4B")

    def test_a_placeholder_pluto_address_falls_through(self):
        """PLUTO is preferred, but not when what it holds is a placeholder."""
        row = self._row(address="N/A", street_number="12",
                        street_name="SCHENCK AVENUE")
        self.assertEqual(acris.address_of(row), "12 SCHENCK AVENUE")

    def test_all_placeholders_falls_back_to_the_bbl(self):
        """Never an empty title. The BBL is at least something to look up."""
        row = self._row(street_number="N/A", street_name="N/A")
        self.assertEqual(acris.address_of(row), "3026390006")

    def test_zero_is_a_placeholder_not_a_street_number(self):
        """Deliberate: no NYC address is number 0, and a bare 0 in this field
        is the same kind of filler as N/A."""
        self.assertEqual(acris._real("0"), "")


class TestBurnInReadsTheSameConfig(unittest.TestCase):
    """bin/compare-migration must read the thresholds the collectors use.

    bin/run.sh sources .env before invoking core.runner; the burn-in is called
    straight from cron and does not. On 2026-10-01 that meant the comparison
    computed the ACRIS source at the $1,000,000 code default while the
    collector had been filling the store at the $5,000,000 in .env, and it
    passed only because the store happened to have been seeded at $1M and the
    $5M set is a subset of it. Rebuild the store and the same code reports 88
    phantom misses; lower the threshold in .env and it would hide real ones.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        path = os.path.join(ROOT, "bin", "compare-migration")
        spec = importlib.util.spec_from_loader(
            "cmig", importlib.machinery.SourceFileLoader("cmig", path))
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self._env = dict(os.environ)
        self._dir = tempfile.TemporaryDirectory()
        env_path = os.path.join(self._dir.name, ".env")
        with open(env_path, "w") as fh:
            fh.write(
                "SPINE_PROPTECH_MIN_AMOUNT=5000000\n"
                "SPINE_POLYMARKET_MIN_VOLUME=100000\n"
                "ANTHROPIC_API_KEY=sk-should-never-be-exported\n"
                "SPINE_BRIEF_WEBHOOK_SECRET=hmac-should-never-be-exported\n"
                "TELEGRAM_BOT_TOKEN=token-should-never-be-exported\n")
        os.chmod(env_path, 0o600)
        self._root = self.mod.paths.root
        self.mod.paths.root = lambda: self._dir.name
        for k in self.mod.DOTENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self.mod.paths.root = self._root
        os.environ.clear()
        os.environ.update(self._env)
        self._dir.cleanup()

    def test_thresholds_are_loaded(self):
        loaded = self.mod.load_tuning()
        self.assertEqual(loaded["SPINE_PROPTECH_MIN_AMOUNT"], "5000000")
        self.assertEqual(loaded["SPINE_POLYMARKET_MIN_VOLUME"], "100000")
        # And they are actually visible to the function the collectors use.
        self.assertEqual(
            bridge.env_int("SPINE_PROPTECH_MIN_AMOUNT", 1_000_000), 5_000_000)

    def test_secrets_are_never_exported(self):
        """The load is an allowlist, not a blanket source of .env. A tool that
        shells nothing and needs no credential should not be carrying an API
        key, an HMAC secret and a bot token in its environment."""
        self.mod.load_tuning()
        for leaked in ("ANTHROPIC_API_KEY", "SPINE_BRIEF_WEBHOOK_SECRET",
                       "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
            self.assertNotIn(leaked, os.environ, leaked)

    def test_a_real_environment_variable_still_wins(self):
        """setdefault semantics: an operator overriding on the command line
        must not be silently replaced by the file."""
        os.environ["SPINE_PROPTECH_MIN_AMOUNT"] = "250000"
        loaded = self.mod.load_tuning()
        self.assertNotIn("SPINE_PROPTECH_MIN_AMOUNT", loaded)
        self.assertEqual(os.environ["SPINE_PROPTECH_MIN_AMOUNT"], "250000")

    def test_a_world_readable_env_does_not_crash_the_burn_in(self):
        """_Secrets refuses a .env that is not 600. The burn-in must report
        that and carry on, not die -- it is evidence-gathering, not a job."""
        import contextlib
        import io as _io
        os.chmod(os.path.join(self._dir.name, ".env"), 0o644)
        err = _io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.mod.load_tuning(), {})
        self.assertIn("must be 600", err.getvalue(),
                      "it must SAY why it gave up, not fail silently")

    def test_the_allowlist_holds_no_secret_shaped_names(self):
        for key in self.mod.DOTENV_KEYS:
            for bad in ("KEY", "SECRET", "TOKEN", "PASSWORD"):
                self.assertNotIn(bad, key.upper(), key)


if __name__ == "__main__":
    unittest.main()
