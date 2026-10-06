"""
surfaces.store — the assistant's own small state: var/assist.db (mode 600).

Separate from spine.db (items) and health.db (sensor data) on purpose:
preferences and self-reports are written here, source data is only ever read.

  prefs       key -> JSON value (brief time, quiet hours, mode, pause)
  focus       structured subscriptions (topic, cadence, status, sensitive)
  cards       every outgoing message with buttons: opaque id, domain, the
              exact referenced object, a snapshot of the facts shown, and
              Telegram message_id -> replies resolve to the real object
  callbacks   short random tokens -> (card, action); expiry, single-use for
              side effects
  updates     processed Telegram update_ids (duplicate/replay protection)
  feedback    Done / Easier / Skip / Later taps (self-report, not sensor data)
  telemetry   latency, sizes, outcomes. Never message text or health values.
  brief_log   which brief went out for which local day (no duplicates)
  research    questions the registry could not answer yet (local only)
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time

from core import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS prefs (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS focus (
  id INTEGER PRIMARY KEY, topic TEXT NOT NULL, cadence TEXT NOT NULL DEFAULT 'daily',
  status TEXT NOT NULL DEFAULT 'active', sensitive INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL, max_per_day INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, msg_id INTEGER, domain TEXT NOT NULL,
  ref TEXT NOT NULL, snapshot TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS cards_by_msg ON cards(chat_id, msg_id);
CREATE TABLE IF NOT EXISTS callbacks (
  token TEXT PRIMARY KEY, card_id TEXT NOT NULL, action TEXT NOT NULL, arg TEXT,
  created REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, card_id TEXT, kind TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telemetry (
  ts REAL NOT NULL, event TEXT NOT NULL, ms INTEGER, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS brief_log (day TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (day, kind));
CREATE TABLE IF NOT EXISTS research (id INTEGER PRIMARY KEY, ts REAL NOT NULL, question TEXT);
"""

DEFAULTS = {
    "brief_time": "07:30",         # local America/New_York
    "brief_enabled": False,        # off until Nick turns it on (no surprise sends)
    "weekly_enabled": False,
    "weekly_day": "sun",
    "quiet_start": "22:30",
    "quiet_end": "07:00",
    "mode": "compact",             # compact | detailed
    "domains": ["health", "property", "quant"],
    "paused": False,
    "research_online": True,       # keyword-only MedlinePlus lookups (see /privacy)
}

CALLBACK_TTL_S = 7 * 24 * 3600
CARD_TTL_S = 30 * 24 * 3600


def db_path():
    return os.environ.get("SPINE_ASSIST_DB") or paths.var("assist.db")


class Store:
    def __init__(self, path=None):
        self.path = path or db_path()
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # prefs
    def pref(self, k):
        r = self.conn.execute("SELECT v FROM prefs WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else DEFAULTS.get(k)

    def set_pref(self, k, v):
        with self.conn:
            self.conn.execute("INSERT INTO prefs(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE "
                              "SET v=excluded.v", (k, json.dumps(v)))

    def prefs(self):
        out = dict(DEFAULTS)
        for r in self.conn.execute("SELECT k, v FROM prefs"):
            out[r[0]] = json.loads(r[1])
        return out

    # focus
    def add_focus(self, topic, cadence="daily", sensitive=False):
        now = time.time()
        r = self.conn.execute("SELECT id FROM focus WHERE topic=? AND status!='cancelled'",
                              (topic,)).fetchone()
        if r:
            with self.conn:
                self.conn.execute("UPDATE focus SET status='active', cadence=?, updated=? "
                                  "WHERE id=?", (cadence, now, r[0]))
            return r[0]
        with self.conn:
            cur = self.conn.execute("INSERT INTO focus(topic,cadence,sensitive,created,updated) "
                                    "VALUES(?,?,?,?,?)", (topic, cadence, int(sensitive), now, now))
        return cur.lastrowid

    def set_focus_status(self, fid, status):
        assert status in ("active", "paused", "cancelled")
        with self.conn:
            return self.conn.execute("UPDATE focus SET status=?, updated=? WHERE id=?",
                                     (status, time.time(), fid)).rowcount

    def focuses(self, include_cancelled=False):
        q = "SELECT * FROM focus" + ("" if include_cancelled else " WHERE status!='cancelled'")
        return [dict(r) for r in self.conn.execute(q + " ORDER BY id")]

    # cards + callbacks
    def new_card(self, chat_id, domain, ref, snapshot=None):
        cid = secrets.token_urlsafe(9)
        with self.conn:
            self.conn.execute("INSERT INTO cards(id,chat_id,domain,ref,snapshot,created) "
                              "VALUES(?,?,?,?,?,?)", (cid, chat_id, domain, json.dumps(ref),
                                                     json.dumps(snapshot, default=str), time.time()))
        return cid

    def attach_msg(self, card_id, msg_id):
        with self.conn:
            self.conn.execute("UPDATE cards SET msg_id=? WHERE id=?", (msg_id, card_id))

    def card(self, card_id):
        r = self.conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return self._card(r)

    def card_for_msg(self, chat_id, msg_id):
        r = self.conn.execute("SELECT * FROM cards WHERE chat_id=? AND msg_id=?",
                              (chat_id, msg_id)).fetchone()
        return self._card(r)

    @staticmethod
    def _card(r):
        if not r:
            return None
        d = dict(r)
        d["ref"] = json.loads(d["ref"])
        d["snapshot"] = json.loads(d["snapshot"]) if d["snapshot"] else None
        return d

    def token(self, card_id, action, arg=None):
        t = secrets.token_urlsafe(12)       # 16 chars; callback_data limit is 64 bytes
        with self.conn:
            self.conn.execute("INSERT INTO callbacks(token,card_id,action,arg,created) "
                              "VALUES(?,?,?,?,?)", (t, card_id, action, arg, time.time()))
        return t

    def resolve(self, token, chat_id, now=None):
        """(status, callback row, card). status: ok | unknown | expired | foreign | used."""
        now = now or time.time()
        r = self.conn.execute("SELECT * FROM callbacks WHERE token=?", (token,)).fetchone()
        if not r:
            return "unknown", None, None
        cb = dict(r)
        card = self.card(cb["card_id"])
        if not card or card["chat_id"] != chat_id:
            return "foreign", cb, None
        if now - cb["created"] > CALLBACK_TTL_S:
            return "expired", cb, card
        return ("used" if cb["used"] else "ok"), cb, card

    def mark_used(self, token):
        with self.conn:
            return self.conn.execute("UPDATE callbacks SET used=? WHERE token=? AND used IS NULL",
                                     (time.time(), token)).rowcount == 1

    # updates (dedupe)
    def seen_update(self, update_id) -> bool:
        """True if already processed; otherwise records it and returns False."""
        try:
            with self.conn:
                self.conn.execute("INSERT INTO updates(update_id, ts) VALUES(?,?)",
                                  (update_id, time.time()))
            return False
        except sqlite3.IntegrityError:
            return True

    def max_update(self):
        r = self.conn.execute("SELECT MAX(update_id) FROM updates").fetchone()
        return r[0]

    # feedback / telemetry / brief log / research
    def feedback(self, card_id, kind):
        with self.conn:
            self.conn.execute("INSERT INTO feedback(card_id,kind,ts) VALUES(?,?,?)",
                              (card_id, kind, time.time()))

    def feedback_for(self, card_id):
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM feedback WHERE card_id=? ORDER BY ts", (card_id,))]

    def tel(self, event, ms=None, ok=True, detail=None):
        with self.conn:
            self.conn.execute("INSERT INTO telemetry VALUES(?,?,?,?,?)",
                              (time.time(), event, ms, int(bool(ok)),
                               (detail or "")[:120]))

    def brief_sent(self, day, kind="daily"):
        return self.conn.execute("SELECT 1 FROM brief_log WHERE day=? AND kind=?",
                                 (day, kind)).fetchone() is not None

    def mark_brief(self, day, kind="daily"):
        try:
            with self.conn:
                self.conn.execute("INSERT INTO brief_log VALUES(?,?,?)", (day, kind, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def add_research(self, q):
        with self.conn:
            self.conn.execute("INSERT INTO research(ts,question) VALUES(?,?)",
                              (time.time(), q[:500]))

    def prune(self, now=None):
        now = now or time.time()
        with self.conn:
            self.conn.execute("DELETE FROM callbacks WHERE created < ?", (now - CALLBACK_TTL_S,))
            self.conn.execute("DELETE FROM cards WHERE created < ?", (now - CARD_TTL_S,))
            self.conn.execute("DELETE FROM updates WHERE ts < ?", (now - 7 * 86400,))
            self.conn.execute("DELETE FROM telemetry WHERE ts < ?", (now - 90 * 86400,))

    def forget(self, scope="all"):
        """Delete local assistant data. Never touches health.db, spine.db or Telegram."""
        tables = {"all": ["prefs", "focus", "cards", "callbacks", "feedback", "research",
                          "brief_log", "telemetry"],
                  "prefs": ["prefs", "focus"],
                  "history": ["cards", "callbacks", "feedback", "research"]}[scope]
        with self.conn:
            for t in tables:
                self.conn.execute(f"DELETE FROM {t}")
        return tables
