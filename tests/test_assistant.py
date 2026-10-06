"""
Telegram assistant: data services, routing, authorization, cards, callbacks,
scheduling and privacy. Synthetic fixtures only; Telegram is a fake.

    python3 -m unittest tests.test_assistant
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import health, health_facts as hf, market_filter, property_facts as pf  # noqa: E402
from core import quant_facts as qf  # noqa: E402
from collectors import acris, polymarket  # noqa: E402

U = 5567513606


def _iso(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc).isoformat()


class Env:
    """Temp databases wired through the same env vars the service uses."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.old = {k: os.environ.get(k) for k in
                    ("SPINE_DB", "SPINE_HEALTH_DB", "SPINE_ASSIST_DB", "SPINE_DWJ_ROOT",
                     "SPINE_HEALTH_PROFILE")}
        os.environ["SPINE_DB"] = os.path.join(self.dir, "spine.db")
        os.environ["SPINE_HEALTH_DB"] = os.path.join(self.dir, "health.db")
        os.environ["SPINE_ASSIST_DB"] = os.path.join(self.dir, "assist.db")
        os.environ["SPINE_DWJ_ROOT"] = self.dir
        os.environ["SPINE_HEALTH_PROFILE"] = os.path.join(self.dir, "profile.md")
        os.makedirs(os.path.join(self.dir, "data"))
        self.health = health.connect(os.environ["SPINE_HEALTH_DB"])
        self._spine()
        self._eventbot()
        self._prediction()

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def sample(self, metric, day, qty, source="Watch", units="count"):
        self.health.execute("INSERT OR REPLACE INTO health_metric_samples(metric, ts, date, source,"
                            " units, qty) VALUES(?,?,?,?,?,?)",
                            (metric, f"{day}T00:00:00-04:00", day, source, units, qty))
        self.health.commit()

    def imported(self, when_iso):
        self.health.execute("INSERT OR REPLACE INTO raw_payloads(sha256, received_at, bytes, keys)"
                            " VALUES(?,?,0,'')", (when_iso, when_iso))
        self.health.commit()

    def _spine(self):
        from core import store
        st = store.Store(path=os.environ["SPINE_DB"])
        st.put([{"kind": "deal", "key": "D1", "title": "Deed (ownership transfer) $50,000,000",
                 "url": "https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView?doc_id=D1",
                 "importance": 85, "data": {"doc_type": "DEED", "amount": 5e7, "document_id": "D1",
                                            "address": "1 TEST ST <b>", "borough": "Manhattan",
                                            "bbl": "1008000071", "in_pluto": False,
                                            "recorded": "2026-08-31T00:00:00.000"}},
                {"kind": "deal", "key": "M1", "title": "Mortgage (loan, not a sale) $42,648,000",
                 "url": "https://a836-acris.nyc.gov/x?doc_id=M1", "importance": 80,
                 "data": {"doc_type": "MTGE", "amount": 42648000.0, "document_id": "M1",
                          "address": "2 LOAN AVE", "borough": "Brooklyn", "bbl": "3015880001",
                          "in_pluto": True, "unitsres": 267, "yearbuilt": 1976,
                          "recorded": "2026-08-31T00:00:00.000"}}], source="acris")
        c = sqlite3.connect(os.environ["SPINE_DB"])
        c.executescript("""
        CREATE TABLE acris_docs (document_id TEXT PRIMARY KEY, doc_type TEXT, document_date TEXT,
          recorded TEXT, amount REAL, percent_trans REAL, n_parcels INTEGER, first_seen TEXT);
        INSERT INTO acris_docs VALUES ('D1','DEED','2026-06-01','2026-08-31T00:00:00',5e7,100,3,'x');
        INSERT INTO acris_docs VALUES ('M1','MTGE','2026-08-19','2026-08-31T00:00:00',42648000,0,1,'x');
        """)
        c.commit()
        c.close()

    def _eventbot(self):
        c = sqlite3.connect(os.path.join(self.dir, "data", "eventbot.db"))
        c.executescript("""
        CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
        INSERT INTO kv VALUES ('start_cash','10000');
        CREATE TABLE equity_log (ts TEXT PRIMARY KEY, equity REAL, cash REAL, positions_value REAL, n_open INTEGER);
        INSERT INTO equity_log VALUES ('2026-09-03T00:00:00Z',10000,10000,0,0);
        INSERT INTO equity_log VALUES ('2026-10-06T16:00:00Z',10090,9600,490,1);
        CREATE TABLE positions (id INTEGER PRIMARY KEY, asset TEXT, rule TEXT, idea_id INTEGER, executor TEXT,
          status TEXT, qty REAL, notional REAL, entry_price REAL, entry_ts TEXT, stop REAL, target REAL,
          max_exit_ts TEXT, exit_price REAL, exit_ts TEXT, pnl REAL, exit_reason TEXT, broker_order_id TEXT);
        INSERT INTO positions VALUES (1,'GLD','tariff_generic',1,'sim','closed',1,400,400,'2026-09-10T00:00:00Z',
          null,null,null,500,'x',100,'target',null);
        INSERT INTO positions VALUES (2,'BTC-USD','crypto_mention',2,'sim','open',1,500,500,'2026-10-06T15:00:00Z',
          480,530,'2026-10-06T21:00:00Z',null,null,null,null,null);
        INSERT INTO positions VALUES (3,'ETH-USD','crypto_mention',99,'sim','closed',1,0,0,'x',null,null,null,0,'x',0,'x',null);
        CREATE TABLE ideas (id INTEGER PRIMARY KEY, signal_id INTEGER, rule TEXT, asset TEXT, sentiment INTEGER,
          blocked TEXT, entry_price REAL, entry_ts TEXT, price_source TEXT, traded INTEGER, weight REAL);
        INSERT INTO ideas VALUES (2,7,'crypto_mention','BTC-USD',1,null,500,'x','coinbase',1,0.5);
        CREATE TABLE signals (id INTEGER PRIMARY KEY, ts TEXT, seen_ts TEXT, source TEXT, source_id TEXT,
          text TEXT, url TEXT, sentiment INTEGER, n_ideas INTEGER);
        INSERT INTO signals VALUES (7,'t','2026-10-06T15:00:00Z','polymarket','s',
          '[polymarket down -0.71 -> 0.01] Bitcoin Up or Down - Oct 6 <script>','',1,1);
        """)
        c.commit()
        c.close()

    def _prediction(self):
        c = sqlite3.connect(os.path.join(self.dir, "data", "prediction.db"))
        c.executescript("""CREATE TABLE snapshots (ts TEXT, market_id TEXT, slug TEXT, question TEXT,
          event_title TEXT, yes_price REAL, best_bid REAL, best_ask REAL, volume24h REAL, liquidity REAL,
          end_date TEXT, political INTEGER, PRIMARY KEY (ts, market_id));
          INSERT INTO snapshots VALUES ('t','5239514','','q','',0,0,0,0,0,'2026-10-05T16:00:00Z',1);""")
        c.commit()
        c.close()


class FakeBot:
    def __init__(self):
        self.sent, self.photos, self.answers, self.n = [], [], [], 100

    def send(self, chat, text, buttons=None, reply_to=None, silent=False):
        self.n += 1
        self.sent.append({"chat": chat, "text": text, "buttons": buttons, "reply_to": reply_to,
                          "id": self.n})
        return {"message_id": self.n}

    def photo(self, chat, png, caption, buttons=None, silent=False):
        self.n += 1
        self.photos.append({"chat": chat, "png": png, "caption": caption, "id": self.n})
        return {"message_id": self.n}

    def answer(self, cb_id, text="", alert=False):
        self.answers.append(text)

    def clear_buttons(self, *a):
        pass


class Base(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.env = Env()
        from surfaces.bot import Assistant
        from surfaces.store import Store
        self.store = Store()
        self.bot = FakeBot()
        self.app = Assistant(self.bot, self.store, U, now=lambda: self.NOW)
        self.uid = 0

    def tearDown(self):
        self.store.conn.close()
        self.env.health.close()
        self.env.close()

    def msg(self, text, reply=None, user=U, chat=U, ctype="private"):
        self.uid += 1
        m = {"message_id": 1000 + self.uid, "from": {"id": user},
             "chat": {"id": chat, "type": ctype}, "text": text}
        if reply:
            m["reply_to_message"] = {"message_id": reply}
        self.app.handle({"update_id": self.uid, "message": m})

    def tokens(self, msg_id):
        card = self.store.card_for_msg(U, msg_id)
        out = {}
        for t, a in self.store.conn.execute(
                "SELECT token, action FROM callbacks WHERE card_id=? ORDER BY rowid", (card["id"],)):
            out.setdefault(a, t)             # first button for each action
        return out

    def press(self, token, msg_id, user=U, update_id=None):
        if update_id is None:
            self.uid += 1
            update_id = self.uid
        self.app.handle({"update_id": update_id, "callback_query": {
            "id": "cb", "from": {"id": user}, "data": token,
            "message": {"message_id": msg_id, "chat": {"id": U, "type": "private"}}}})

    def last(self):
        return self.bot.sent[-1]["text"]


# ── health facts ─────────────────────────────────────────────────────

class TestHealthFacts(Base):
    def test_partial_day_is_not_a_daily_total(self):
        self.env.sample("step_count", "2026-10-06", 185)
        self.env.imported(_iso(2026, 10, 6, 12))
        f = hf.fact(self.env.health, "step_count", date(2026, 10, 6))
        self.assertFalse(f.complete)
        self.assertIn("running total", hf.describe(f))

    def test_day_complete_only_after_local_midnight_dst(self):
        # Oct 6 EDT ends 04:00 UTC Oct 7; Nov 2 (EST after DST ends) ends 05:00 UTC Nov 3.
        self.assertEqual(hf.day_end_utc(date(2026, 10, 6)).hour, 4)
        self.assertEqual(hf.day_end_utc(date(2026, 11, 1)).hour, 5)
        self.assertFalse(hf.is_complete(date(2026, 10, 6),
                                        datetime(2026, 10, 7, 3, 59, tzinfo=timezone.utc)))
        self.assertTrue(hf.is_complete(date(2026, 10, 6),
                                       datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)))

    def test_missing_is_not_zero(self):
        self.env.sample("step_count", "2026-10-01", 5000)
        self.env.sample("step_count", "2026-10-03", 7000)
        self.env.imported(_iso(2026, 10, 6, 12))
        pts = hf.series_for_chart(self.env.health, "step_count", date(2026, 10, 3), 3)
        self.assertEqual([p[1] for p in pts], [5000, None, 7000])

    def test_overlapping_sources_not_summed(self):
        self.env.sample("step_count", "2026-10-02", 6000, source="iPhone")
        self.env.sample("step_count", "2026-10-02", 5800, source="Watch")
        self.env.imported(_iso(2026, 10, 6, 12))
        dv = hf.day_values(self.env.health, "step_count", date(2026, 10, 2), date(2026, 10, 2))
        self.assertEqual(dv["2026-10-02"].value, 6000)
        self.assertIn("not summed", dv["2026-10-02"].reconciled)

    def test_reimport_idempotent(self):
        payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [
            {"date": "2026-10-02 00:00:00 -0400", "qty": 4000, "source": "Watch"}]}]}}
        for _ in range(3):
            health.ingest(payload, self.env.health)
        n = self.env.health.execute("SELECT COUNT(*) FROM health_metric_samples "
                                    "WHERE metric='step_count'").fetchone()[0]
        self.assertLessEqual(n, 1)

    def test_baseline_requires_enough_complete_days(self):
        for d in range(1, 4):
            self.env.sample("resting_heart_rate", f"2026-10-0{d}", 60)
        self.env.sample("resting_heart_rate", "2026-10-05", 70)
        self.env.imported(_iso(2026, 10, 6, 12))
        f = hf.fact(self.env.health, "resting_heart_rate", date(2026, 10, 5))
        self.assertFalse(f.b7.ok)
        self.assertEqual(f.b7.n, 3)
        self.assertIn("no personal baseline", hf.describe(f))
        for d in (28, 29):
            self.env.sample("resting_heart_rate", f"2026-09-{d}", 60)
        f = hf.fact(self.env.health, "resting_heart_rate", date(2026, 10, 5))
        self.assertTrue(f.b7.ok)
        self.assertEqual(f.b7.n, 5)
        self.assertFalse(f.b30.ok)
        self.assertIn("n=", hf.describe(f))


# ── markets / property / quant services ──────────────────────────────

class TestServices(Base):
    def test_expired_market_dropped(self):
        now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(market_filter.status("2026-10-05T16:00:00Z", "2026-10-05T18:30:01Z", now), "expired")
        self.assertEqual(market_filter.status("2026-10-07T16:00:00Z", "2026-10-07T09:00:00Z",
                                              datetime(2026, 10, 7, 10, tzinfo=timezone.utc)), "live")
        self.assertEqual(market_filter.status("2026-10-07T16:00:00Z", "2026-10-07T16:30:00Z",
                                              datetime(2026, 10, 7, 10, tzinfo=timezone.utc)), "settling")
        self.assertEqual(market_filter.status("2026-10-07T16:00:00Z", "2026-10-07T16:30:00Z",
                                              datetime(2026, 10, 7, 17, tzinfo=timezone.utc)), "expired")
        rows = [{"market_id": "5239514", "ts": "2026-10-05T18:30:01Z", "question": "q",
                 "prev_price": .42, "new_price": .99, "delta": .57, "volume24h": 5e5, "political": 1}]
        self.assertEqual(polymarket.build_items(rows, {"5239514": "2026-10-05T16:00:00Z"}, now=now), [])
        self.assertEqual(len(polymarket.build_items(rows, {}, now=now)), 1)  # unknown end: kept

    def test_deed_vs_mortgage(self):
        m = pf.card_facts({"data": {"doc_type": "MTGE", "amount": 1e7, "in_pluto": True}},
                          {"percent_trans": 0, "n_parcels": 1})
        self.assertIn("financing document, not a transfer", m["what"])
        self.assertIn("NOT the property's price", m["amount_means"])
        self.assertEqual(m["caveats"], [])          # 0% "transferred" is meaningless on a loan
        d = pf.card_facts({"data": {"doc_type": "DEED", "amount": 10}},
                          {"percent_trans": 50, "n_parcels": 4})
        self.assertTrue(any("4 parcels" in c for c in d["caveats"]))
        self.assertTrue(any("50%" in c for c in d["caveats"]))
        self.assertTrue(any("Nominal" in c for c in d["caveats"]))

    def test_acris_titles_say_loan(self):
        row = {"document_amt": 1e7, "doc_type": "MTGE", "borough": 1}
        acris_title = acris.title_of.__globals__["SHORT_TYPE"]["MTGE"]
        self.assertIn("not a sale", acris_title)

    def test_score_explanation_matches_collector(self):
        self.assertEqual(acris.importance(6e7, None), 85)
        self.assertEqual(acris.importance(3e6, 25), 57)
        self.assertIn("not opportunity", pf.SCORE_EXPLAIN.replace(", not", " not"))

    def test_glossary_terms(self):
        self.assertIn("ownership", pf.glossary("deed"))
        self.assertIn("loan", pf.glossary("MTGE"))
        self.assertIn("Borough-Block-Lot", pf.glossary("bbl"))
        self.assertIsNone(pf.glossary("xyz"))

    def test_quant_accounting_reconciles(self):
        from surfaces import assistant as A
        a = qf.accounting(A.eventbot_conn())
        self.assertAlmostEqual(a["realized"], 100)
        self.assertAlmostEqual(a["unrealized"], -10)
        self.assertAlmostEqual(a["residual"], 0)       # 10000+100-500 = 9600
        self.assertAlmostEqual(a["total_return_pct"], 0.9)
        self.assertTrue(a["reconciles"])
        self.assertEqual(qf.mode(A.eventbot_conn()), "simulation")

    def test_why_opened_links_record_or_says_missing(self):
        from surfaces import assistant as A
        w = qf.why_opened(A.eventbot_conn(), 2)
        self.assertEqual(w["signal"]["id"], 7)
        self.assertIn("settling", w["warning"])
        w = qf.why_opened(A.eventbot_conn(), 3)
        self.assertIn("the idea record", w["missing"])


# ── Telegram behaviour ───────────────────────────────────────────────

class TestBot(Base):
    def test_unauthorized_user_group_and_foreign_chat_ignored(self):
        self.msg("/health", user=1, chat=1)
        self.msg("/health", chat=-100, ctype="group")
        self.msg("/health", user=U, chat=999)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE event='unauthorized'").fetchone()[0], 3)

    def test_duplicate_update_processed_once(self):
        m = {"message_id": 1, "from": {"id": U}, "chat": {"id": U, "type": "private"}, "text": "/help"}
        self.app.handle({"update_id": 77, "message": m})
        self.app.handle({"update_id": 77, "message": m})
        self.assertEqual(len(self.bot.sent), 1)

    def test_reply_explain_resolves_exact_card(self):
        self.msg("/property")
        cards = [s for s in self.bot.sent if s["buttons"]]
        mort = [c for c in cards if "Mortgage" in c["text"]][0]
        deed = [c for c in cards if "Deed" in c["text"]][0]
        self.msg("Explain this", reply=mort["id"])
        self.assertIn("mortgage", self.last())
        self.assertIn("$42.6M", self.last())
        self.msg("Was this a sale or a loan?", reply=deed["id"])
        self.assertIn("ownership transfer", self.last())

    def test_reply_to_unknown_message(self):
        self.msg("Explain this", reply=55555)
        self.assertIn("don't have a record", self.last())

    def test_untrusted_text_is_escaped(self):
        self.msg("/property")
        txt = [s["text"] for s in self.bot.sent if "1 TEST ST" in s["text"]][0]
        self.assertIn("&lt;b&gt;", txt)
        cards = [s for s in self.bot.sent if s["buttons"]]
        self.msg("/quant")
        toks = self.tokens(self.bot.sent[-1]["id"])
        self.press(toks["q_positions"], self.bot.sent[-1]["id"])
        pos = [s for s in self.bot.sent if "BTC-USD" in s["text"]][0]
        self.msg("why was this opened?", reply=pos["id"])
        self.assertIn("&lt;script&gt;", self.last())
        self.assertNotIn("<script>", self.last())
        self.assertTrue(cards)

    def test_callback_ownership_expiry_and_single_use(self):
        self.msg("/workout")
        mid = self.bot.sent[-1]["id"]
        toks = self.tokens(mid)
        self.press("forged-token", mid)
        self.assertIn("isn't valid", self.bot.answers[-1])
        self.press(toks["fb"], mid)
        n = len(self.bot.sent)
        self.press(toks["fb"], mid)                   # double tap: no second record
        self.assertEqual(len(self.bot.sent), n)
        self.assertEqual(self.store.feedback_for(self.store.card_for_msg(U, mid)["id"]), ["done"])
        self.press(toks["fb"], mid, user=1)           # other user: ignored entirely
        self.assertEqual(len(self.bot.sent), n)
        self.store.conn.execute("UPDATE callbacks SET created=0")
        self.store.conn.commit()
        self.press(toks["fb"], mid)
        self.assertIn("expired", self.bot.answers[-1])

    def test_sensitive_question_stays_out_of_brief(self):
        self.msg("Help me support my sexual health")
        self.assertIn("can't tell", self.last())
        self.assertIn("won't appear in your daily briefs", self.last())
        self.assertEqual(self.store.focuses(), [])
        self.store.set_pref("brief_enabled", True)
        self.msg("/brief")
        self.assertNotRegex(self.last().lower(), r"sex|erect|sti")

    def test_focus_requires_explicit_confirmation_and_pause_stops_it(self):
        self.msg("Include a mobility idea in my morning brief")
        self.assertIn("Add <b>Mobility</b>", self.last())
        self.assertEqual(self.store.focuses(), [])
        mid = self.bot.sent[-1]["id"]
        self.press(self.tokens(mid)["focus_add"], mid)
        self.assertEqual(self.store.focuses()[0]["status"], "active")
        self.msg("/brief")
        self.assertIn("Mobility idea", self.last())
        self.store.set_focus_status(self.store.focuses()[0]["id"], "paused")
        self.msg("/brief")
        self.assertNotIn("Mobility idea", self.last())

    def test_scheduled_brief_once_per_day_and_survives_restart(self):
        from surfaces.bot import Assistant
        from surfaces.store import Store
        self.store.set_pref("brief_enabled", True)
        self.store.set_pref("brief_time", "07:30")
        early = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)   # 07:00 EDT
        self.app._now = lambda: early
        self.assertIsNone(self.app.tick())
        later = datetime(2026, 10, 6, 11, 31, tzinfo=timezone.utc)  # 07:31 EDT
        self.app._now = lambda: later
        self.assertEqual(self.app.tick(), "daily")
        app2 = Assistant(self.bot, Store(), U, now=lambda: later)   # "restart"
        self.assertIsNone(app2.tick())
        self.store.set_pref("paused", True)
        self.app._now = lambda: datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(self.app.tick())

    def test_brief_without_model_or_health_data_is_short_and_honest(self):
        self.msg("/brief")
        t = self.last()
        self.assertLess(len(re.sub("<[^>]+>", "", t).split()), 200)
        self.assertNotIn("MB free", t)
        self.assertIn("Simulation", t)

    def test_health_answer_shows_period_coverage_and_sources(self):
        self.env.sample("step_count", "2026-10-05", 343)
        self.env.sample("step_count", "2026-10-06", 185)
        self.env.imported(_iso(2026, 10, 6, 8))
        self.msg("/health")
        t = self.last()
        self.assertIn("Latest export", t)
        self.assertIn("as of the", t)                 # today's values tied to the export time
        self.assertIn("not live", t)
        self.assertNotIn("Today so far", t)
        self.assertLess(len(re.sub("<[^>]+>", "", t)), 450)   # short card
        mid = self.bot.sent[-1]["id"]
        self.press(self.tokens(mid)["explain_health"], mid)
        full = self.last()
        self.assertIn("running total", full)
        self.assertIn("wear/recording coverage unknown", full)
        self.assertIn("first day on record", full)
        self.msg("Based on my recent data, what are the two most useful things to improve this week?")
        mid = self.bot.sent[-1]["id"]
        self.press(self.tokens(mid)["sources"], mid)
        self.assertIn("cdc.gov", self.last())
        self.assertIn("retrieved", self.last())

    def test_chart_matches_snapshot_gaps(self):
        self.env.sample("step_count", "2026-10-04", 4000)
        self.env.sample("step_count", "2026-10-06", 185)
        self.env.imported(_iso(2026, 10, 6, 8))
        self.msg("show my steps trend")
        p = self.bot.photos[-1]
        self.assertTrue(p["png"].startswith(b"\x89PNG"))
        self.assertIn("2 day(s) with data, 5 with none", p["caption"])
        card = self.store.card_for_msg(U, p["id"])
        vals = [x[1] for x in card["snapshot"]["points"]]
        self.assertEqual(vals.count(None), 5)

    def _fake_fetch(self, xml, seen):
        class R:
            def __init__(s, b): s.b = b
            def read(s): return s.b
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def opener(req, timeout=None):
            seen.append(req.full_url)
            if xml is None:
                raise OSError("offline")
            return R(xml.encode())
        return opener

    XML = """<?xml version="1.0"?><nlmSearchResult><list>
      <document url="https://medlineplus.gov/heartattack.html"><content name="title">Heart Attack</content>
        <content name="FullSummary">&lt;p&gt;A heart attack happens when blood flow stops.&lt;/p&gt;</content></document>
      <document url="https://medlineplus.gov/caffeine.html"><content name="title">Caffeine</content>
        <content name="FullSummary">&lt;p&gt;Caffeine is a stimulant found in coffee and tea. Too much caffeine can cause trouble sleeping.&lt;/p&gt;</content></document>
      </list></nlmSearchResult>"""

    def test_unknown_question_not_guessed(self):
        seen = []
        self.app.research_opener = self._fake_fetch(self.XML, seen)
        self.msg("should I take creatine?")
        self.assertIn("won't guess", self.last())
        self.assertNotIn("Heart Attack", self.last())   # loose ranking is not relevance
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM research").fetchone()[0], 1)

    def test_novel_question_retrieves_quotes_and_personal_data(self):
        self.env.sample("step_count", "2026-10-05", 343)
        self.env.imported(_iso(2026, 10, 6, 8))
        seen = []
        self.app.research_opener = self._fake_fetch(self.XML, seen)
        self.msg("Does caffeine affect my sleep? I slept 5 hours and my HRV was 58")
        t = self.last()
        self.assertIn("Researched answer", t)
        self.assertIn("trouble sleeping", t)              # the source's own sentence
        self.assertNotIn("Heart Attack", t)
        self.assertIn("no sleep records", t)              # relevant personal data, honestly
        self.assertIn("medlineplus.gov/caffeine", json.dumps(self.bot.sent[-1]["buttons"]))
        q = seen[0].split("term=")[1]
        self.assertNotIn("58", q)                         # only keywords leave the box
        self.assertNotIn("5", q.split("&")[0])
        self.assertLessEqual(len(q.split("&")[0].split("+")), 3)
        # offline: answered from cache
        self.app.research_opener = self._fake_fetch(None, seen)
        self.msg("Does caffeine affect my sleep? I slept 5 hours and my HRV was 58")
        self.assertIn("(cached)", self.last())

    def test_research_offline_and_disabled(self):
        self.app.research_opener = self._fake_fetch(None, [])
        self.msg("is posture important")
        self.assertIn("couldn't be reached", self.last())
        self.msg("/settings research off")
        seen = []
        self.app.research_opener = self._fake_fetch(self.XML, seen)
        self.msg("tell me about melatonin")
        self.assertIn("research is off", self.last())
        self.assertEqual(seen, [])

    def test_wording_has_no_unsupported_claims(self):
        from surfaces import assistant as A
        f = pf.card_facts({"data": {"doc_type": "MTGE", "amount": 1e7}}, {"n_parcels": 1})
        for txt in (f["what"], A.property_explain(f).text, A.property_saleloan(f).text):
            self.assertNotIn("did not change", txt)
            self.assertNotIn("didn't change", txt)
            self.assertNotIn("routine", txt)
        self.msg("/quant")
        self.assertNotIn("worse", self.last())
        self.assertIn("not modelled", self.last())

    def test_forget_leaves_source_data(self):
        self.store.add_focus("walking")
        self.msg("/forget")
        mid = self.bot.sent[-1]["id"]
        self.press(self.tokens(mid)["forget"], mid)
        self.assertEqual(self.store.focuses(), [])
        self.assertIn("untouched", self.last())
        self.assertTrue(os.path.exists(os.environ["SPINE_HEALTH_DB"]))

    def test_no_health_values_in_telemetry(self):
        self.env.sample("resting_heart_rate", "2026-10-05", 68.0)
        self.env.imported(_iso(2026, 10, 6, 8))
        self.msg("/health")
        self.msg("Help me support my sexual health")
        rows = " ".join(str(tuple(r)[1:]) for r in self.store.conn.execute("SELECT * FROM telemetry"))
        self.assertNotIn("68", rows)
        self.assertNotIn("sexual health", rows)

    def test_token_scrubbed_from_errors(self):
        from surfaces.tg import Bot, TgError

        def boom(req, timeout=None):
            raise OSError("connect failed for " + req.full_url)
        b = Bot("123:SECRET", opener=boom)
        with self.assertRaises(TgError) as cm:
            b.send(1, "x")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_long_messages_split(self):
        from surfaces.tg import split
        parts = split("line\n" * 2000)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertGreater(len(parts), 1)


if __name__ == "__main__":
    unittest.main()
