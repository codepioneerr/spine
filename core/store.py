"""
core.store — the item store. One table everything feeds.

This is the idea that turns a pile of cron jobs into an operating system. A
recorded deed, a prediction-market move, an unread dean email, an assignment
due, a mispriced listing — all the same shape, in one table. Deduplication,
retention, and "what is new since I last looked" get solved once, for
everything, instead of once per silo.

    items(id, ts, source, kind, key, title, body, url, data_json,
          importance, status, acted_at)

## The two columns that do the real work

**`UNIQUE(source, key)`** makes deduplication free. A collector re-runs,
re-emits the same deed, and nothing duplicates — so collectors can be dumb.
They emit everything they see on every run and let the store sort it out.
That is what makes a collector a one-file job instead of a state machine.

Choosing `key` is therefore the only hard part of writing a collector. It
must be stable across runs for the same real-world thing, and different for
different things. A document number is a good key. A row index is not.

**`status`** is what makes the assistant possible. `new` is the queue;
`acted` is the receipt. "What should I look at" is one indexed query, not a
diff against a remembered high-water mark.

## Upsert semantics, stated because they are a decision

On conflict the mutable facts (title, body, url, data_json, importance)
are updated, and **`ts`, `status` and `acted_at` are preserved**. So:

  - `ts` means FIRST seen, and stays put. A Polymarket contract that moves
    every 30 minutes does not keep resetting its own age.
  - Something you already acted on does not silently return to the queue
    because a collector re-emitted it.

`updated_ts` (an addition to the PRD schema) records the last time the row
changed, which is what "this signal is still live" actually needs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core import costs
from core.paths import db_path, root  # noqa: F401

KINDS = ("signal", "deal", "task", "alert", "fact")
STATUSES = ("new", "seen", "acted", "dismissed")

# How long a kind is worth keeping, in days. A prediction-market snapshot is
# stale in a week; a recorded deed is a permanent fact about a building.
RETENTION_DAYS = {
    "signal": 90,
    "deal": 0,      # 0 = keep forever
    "task": 365,
    "alert": 180,
    "fact": 0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    updated_ts TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    body       TEXT    NOT NULL DEFAULT '',
    url        TEXT,
    data_json  TEXT    NOT NULL DEFAULT '{}',
    importance INTEGER NOT NULL DEFAULT 50,
    status     TEXT    NOT NULL DEFAULT 'new',
    acted_at   TEXT,
    UNIQUE(source, key)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, importance DESC);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source, ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_kind   ON items(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_ts     ON items(ts DESC);
"""


class ItemError(ValueError):
    """An item that would corrupt the store if written.

    Validated on the way IN, for the same reason META is validated at load
    time: a bad row discovered at read time is discovered by the assistant,
    at 07:00, in front of Nick.
    """


def _coerce(obj):
    """JSON fallback for the handful of types worth accepting.

    Deliberately NOT `default=str`. That would stringify anything at all —
    a socket becomes "<socket object at 0x7f...>" and lands in the store as
    data. Accept the types a collector legitimately has, and raise on the
    rest so the mistake is caught at write time.
    """
    from datetime import date
    from decimal import Decimal
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return sorted(obj, key=str)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"{type(obj).__name__} is not storable")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Same file as costs — one database, several tables."""
    conn = costs.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ─────────────────────────────────────────────────────────────────────────────

def validate(item: dict, source: str) -> dict:
    if not isinstance(item, dict):
        raise ItemError(f"{source}: item must be a dict, got "
                        f"{type(item).__name__}")

    key = item.get("key")
    if not key or not isinstance(key, str):
        raise ItemError(
            f"{source}: every item needs a stable string 'key'. It is what "
            "makes dedup work, so it must identify the same real-world thing "
            "across runs — a document number, not a row index.")

    kind = item.get("kind", "fact")
    if kind not in KINDS:
        raise ItemError(f"{source}: kind {kind!r} must be one of {KINDS}")

    status = item.get("status", "new")
    if status not in STATUSES:
        raise ItemError(f"{source}: status {status!r} must be one of {STATUSES}")

    importance = item.get("importance", 50)
    if not isinstance(importance, int) or isinstance(importance, bool) \
            or not 0 <= importance <= 100:
        raise ItemError(
            f"{source}: importance must be an int 0-100, got {importance!r}. "
            "50 is 'normal'; reserve above 80 for things worth interrupting "
            "Nick about.")

    data = item.get("data", item.get("data_json", {}))
    if isinstance(data, str):
        try:
            json.loads(data)
            data_json = data
        except json.JSONDecodeError as exc:
            raise ItemError(f"{source}: data_json is not valid JSON") from exc
    else:
        try:
            data_json = json.dumps(data, default=_coerce, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ItemError(
                f"{source}: data is not JSON-serialisable — {exc}. Put the "
                "value in a plain dict/list/str/number, or leave it out. "
                "Anything the store cannot round-trip is not data, it is a "
                "reference to something that will not exist tomorrow.") from exc

    return {
        "source": source,
        "kind": kind,
        "key": str(key)[:512],
        "title": str(item.get("title", ""))[:500],
        "body": str(item.get("body", "")),
        "url": item.get("url"),
        "data_json": data_json,
        "importance": importance,
        "status": status,
    }


class Store:
    """The item store, bound to one source.

    `source` comes from the job's id, not from the call site — the same
    reasoning as `ctx.models` taking its privacy class from META. A collector
    cannot mislabel where its items came from, because it never gets to say.
    """

    def __init__(self, source: str = "adhoc", conn=None, path=None):
        self.source = source
        self._conn = conn
        self._path = path
        self._owned = conn is None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = connect(self._path)
        return self._conn

    def close(self):
        if self._conn is not None and self._owned:
            self._conn.close()
            self._conn = None

    # ── writing ──────────────────────────────────────────────────────────

    def put(self, items, source: str | None = None) -> dict:
        """Upsert items. Returns {'new': n, 'updated': n, 'total': n}.

        Collectors emit everything they see on every run; the store decides
        what is actually new. That is the trade that keeps collectors dumb.
        """
        if isinstance(items, dict):
            items = [items]
        src = source or self.source
        rows = [validate(i, src) for i in items]
        now = _now()
        created = updated = 0

        with self.conn:
            for r in rows:
                cur = self.conn.execute(
                    "SELECT id FROM items WHERE source=? AND key=?",
                    (r["source"], r["key"]))
                exists = cur.fetchone() is not None
                self.conn.execute(
                    """
                    INSERT INTO items (ts, updated_ts, source, kind, key, title,
                                       body, url, data_json, importance, status)
                    VALUES (:ts, :ts, :source, :kind, :key, :title, :body, :url,
                            :data_json, :importance, :status)
                    ON CONFLICT(source, key) DO UPDATE SET
                        updated_ts = excluded.updated_ts,
                        title      = excluded.title,
                        body       = excluded.body,
                        url        = excluded.url,
                        data_json  = excluded.data_json,
                        importance = excluded.importance
                        -- ts, status and acted_at deliberately untouched:
                        -- first-seen stays first-seen, and something already
                        -- acted on does not return to the queue because a
                        -- collector re-emitted it.
                    """,
                    {**r, "ts": now})
                if exists:
                    updated += 1
                else:
                    created += 1

        return {"new": created, "updated": updated, "total": len(rows)}

    # ── reading ──────────────────────────────────────────────────────────

    def query(self, *, status=None, kind=None, source=None, since=None,
              min_importance=None, limit=200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM items WHERE 1=1"
        args: list = []
        for col, val in (("status", status), ("kind", kind),
                         ("source", source)):
            if val is not None:
                if isinstance(val, (list, tuple, set)):
                    sql += f" AND {col} IN ({','.join('?' * len(val))})"
                    args += list(val)
                else:
                    sql += f" AND {col}=?"
                    args.append(val)
        if since:
            sql += " AND ts >= ?"
            args.append(since if isinstance(since, str)
                        else since.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if min_importance is not None:
            sql += " AND importance >= ?"
            args.append(min_importance)
        sql += " ORDER BY importance DESC, ts DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def unacted(self, limit=200, min_importance=None):
        """The assistant's inbox: everything not yet dealt with."""
        return self.query(status=("new", "seen"), limit=limit,
                          min_importance=min_importance)

    def counts(self) -> dict:
        out = {}
        for row in self.conn.execute(
                "SELECT status, COUNT(*) n FROM items GROUP BY status"):
            out[row["status"]] = row["n"]
        return out

    def by_source(self) -> list[tuple[str, int, str]]:
        return [(r["source"], r["n"], r["last"]) for r in self.conn.execute(
            "SELECT source, COUNT(*) n, MAX(updated_ts) last "
            "FROM items GROUP BY source ORDER BY n DESC")]

    def total(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]

    # ── acting ───────────────────────────────────────────────────────────

    def mark(self, ids, status: str) -> int:
        """Move items through the queue. The only writer of `acted_at`."""
        if status not in STATUSES:
            raise ItemError(f"status must be one of {STATUSES}")
        ids = [ids] if isinstance(ids, int) else list(ids)
        if not ids:
            return 0
        stamp = _now() if status == "acted" else None
        with self.conn:
            self.conn.execute(
                f"UPDATE items SET status=?, "
                f"acted_at=COALESCE(?, acted_at) "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                [status, stamp, *ids])
        return len(ids)

    # ── housekeeping ─────────────────────────────────────────────────────

    def prune(self, now=None, dry_run=False) -> dict:
        """Drop rows past their kind's retention. Never drops `acted` items —
        those are the record of what Nick actually did, and they are the only
        rows that cost nothing to keep and would hurt to lose."""
        now = now or datetime.now(timezone.utc)
        removed = {}
        for kind, days in RETENTION_DAYS.items():
            if not days:
                continue
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            n = self.conn.execute(
                "SELECT COUNT(*) c FROM items WHERE kind=? AND ts < ? "
                "AND status != 'acted'", (kind, cutoff)).fetchone()["c"]
            if n and not dry_run:
                with self.conn:
                    self.conn.execute(
                        "DELETE FROM items WHERE kind=? AND ts < ? "
                        "AND status != 'acted'", (kind, cutoff))
            if n:
                removed[kind] = n
        return removed
