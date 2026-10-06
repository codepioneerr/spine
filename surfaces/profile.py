"""
surfaces.profile — self-reported profile items, used only once confirmed.

var/health_profile.md (written before the assistant existed) is read once
to SUGGEST items. Each suggestion starts as `unconfirmed` and is shown in
/profile with Confirm / Remove buttons; Nick can also add his own notes.
Personalisation (surfaces.workouts.profile_notes) reads CONFIRMED items
only. The file itself is never edited, and nothing is inferred from
questions he asks: an item exists only because he wrote or confirmed it.
"""

from __future__ import annotations

import re
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS profile (
  key TEXT PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL,   -- unconfirmed|confirmed|removed
  origin TEXT NOT NULL, updated REAL NOT NULL)"""

SUGGEST = [
    ("knee_valgus", r"valgus|knock-?knee", "Knees tend to move inward (knock-knees / valgus)"),
    ("flat_feet", r"flat feet|flat foot|fallen arch", "Flat feet"),
    ("scoliosis", r"scoliosis", "Slight scoliosis"),
    ("pelvic_tilt", r"pelvic tilt", "Pelvic tilt"),
    ("combat_training", r"boxing|muay thai|kickbox", "Trains boxing / Muay Thai"),
]


def ensure(conn):
    conn.execute(SCHEMA)


def seed_from_file(conn, text: str) -> int:
    """Add unconfirmed suggestions for items the file mentions. Never
    overwrites an item Nick already confirmed or removed."""
    ensure(conn)
    n = 0
    for key, pat, label in SUGGEST:
        if re.search(pat, text or "", re.I):
            with conn:
                cur = conn.execute("INSERT OR IGNORE INTO profile VALUES(?,?,?,?,?)",
                                   (key, label, "unconfirmed", "health_profile.md", time.time()))
            n += cur.rowcount
    return n


def items(conn, include_removed=False):
    ensure(conn)
    q = "SELECT key, label, status, origin FROM profile"
    if not include_removed:
        q += " WHERE status != 'removed'"
    return [dict(zip(("key", "label", "status", "origin"), r)) for r in conn.execute(q + " ORDER BY rowid")]


def set_status(conn, key, status):
    assert status in ("confirmed", "removed", "unconfirmed")
    ensure(conn)
    with conn:
        return conn.execute("UPDATE profile SET status=?, updated=? WHERE key=?",
                            (status, time.time(), key)).rowcount


def add_note(conn, text: str) -> str:
    ensure(conn)
    key = "note:" + str(int(time.time() * 1000))
    with conn:
        conn.execute("INSERT INTO profile VALUES(?,?,?,?,?)",
                     (key, text.strip()[:200], "confirmed", "you (Telegram)", time.time()))
    return key


def confirmed(conn) -> dict:
    ensure(conn)
    return {k: True for (k,) in conn.execute("SELECT key FROM profile WHERE status='confirmed'")}
