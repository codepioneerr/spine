"""
core.costs — every model call, what it cost, and the cap that stops it.

The failure this module exists to prevent is not one expensive call. It is a
cheap one nobody counted. Nick's own budget audit found a Perplexity key live
since May, called twice a day, that had never been measured — against a
~$9/month uncommitted budget, unmeasured and dangerous are the same word.

So: every call writes a row. Every row carries dollars. And the cap is
checked **before** the request goes out, not reported after it comes back —
the same discipline as the RAM guard in core.governor. Refuse cheaply rather
than discover expensively.

    python3 -m core.costs --month        month-to-date, by job and tier
    python3 -m core.costs --tail 20      the last 20 calls
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Two different numbers doing two different jobs.
#
# CAP is the wall: a call that would cross it is refused before sending. It
# exists so a runaway loop cannot bill you, and it should sit well above
# normal usage — a cap you brush against monthly is a cap you will raise
# reflexively, and then it protects nothing.
#
# TARGET is the benchmark: what the stack SHOULD cost. Crossing it is
# information, not an emergency. The SPEND panel measures against this, so
# the bar reads as "how am I doing" rather than "how close to disaster".
DEFAULT_CAP_USD = 25.00
DEFAULT_TARGET_USD = 5.00

SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    job        TEXT    NOT NULL,
    tier       TEXT    NOT NULL,
    provider   TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    usd        REAL    NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    outcome    TEXT    NOT NULL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_costs_ts  ON costs(ts);
CREATE INDEX IF NOT EXISTS idx_costs_job ON costs(job);
"""

# USD per MILLION tokens, (input, output).
#
# Figures from DECISION-30-dollar-budget-2026-09-03. Sonnet's introductory
# $2/$10 expired Aug 31 2026 — it is $3/$15 now. Keep this table honest; it
# is the only place dollars are computed, and a stale number here makes every
# downstream figure quietly wrong.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash":      (0.30, 2.50),
    "claude-haiku-4.5":      (1.00, 5.00),
    "claude-sonnet-5":       (3.00, 15.00),
    "claude-opus-5":         (5.00, 25.00),
    # free / local — priced at zero, still logged, because "how many calls"
    # matters even when "how many dollars" is zero.
    "mock":                  (0.00, 0.00),
    "nvidia-nim-free":       (0.00, 0.00),
}


class UnknownModel(KeyError):
    """A model with no price. Refused rather than silently costed at $0 —
    an uncounted call is exactly what this module exists to prevent."""


# ─────────────────────────────────────────────────────────────────────────────

def root() -> str:
    return os.environ.get("SPINE_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))


def db_path() -> str:
    return os.environ.get("SPINE_DB") or os.path.join(root(), "var", "spine.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL so bin/darkweb can read while a job is mid-write. The global flock
    # serializes jobs against each other, but not against the console.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.executescript(SCHEMA)
    return conn


def _money(env_key: str, fallback: float) -> float:
    try:
        return float(os.environ.get(env_key, fallback))
    except (TypeError, ValueError):
        return fallback


def cap_usd() -> float:
    """The hard refusal limit."""
    return _money("SPINE_MONTHLY_USD_CAP", DEFAULT_CAP_USD)


def target_usd() -> float:
    """The number the stack is supposed to cost. Never refuses anything.

    Clamped to the cap: a target above the wall would mean the bar shows
    plenty of room right up to the moment calls start being refused.
    """
    return min(_money("SPINE_MONTHLY_USD_TARGET", DEFAULT_TARGET_USD),
               cap_usd())


# ─────────────────────────────────────────────────────────────────────────────
# pricing
# ─────────────────────────────────────────────────────────────────────────────

def price(model: str, tokens_in: int, tokens_out: int) -> float:
    """Dollars for a call, computed locally.

    No provider API is consulted. Knowing the bill must not itself depend on
    the network, or the cap fails exactly when the network is misbehaving.
    """
    if model not in PRICING:
        raise UnknownModel(
            f"no price for model {model!r}. Add it to core.costs.PRICING — "
            "an unpriced call would be logged at $0 and the cap would not "
            "see it.")
    rate_in, rate_out = PRICING[model]
    usd = (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out
    return round(usd, 8)


def estimate(model: str, prompt: str, max_tokens: int) -> float:
    """Worst-case dollars for a call that has not happened yet.

    ~4 characters per token on input, and the full `max_tokens` on output —
    deliberately pessimistic, because this number gates the cap. Guessing low
    here is how a budget gets exceeded by the call that was 'probably fine'.
    """
    tokens_in = max(1, len(prompt) // 4)
    return price(model, tokens_in, max_tokens)


# ─────────────────────────────────────────────────────────────────────────────
# writing
# ─────────────────────────────────────────────────────────────────────────────

def record(job: str, tier: str, provider: str, model: str, *,
           tokens_in: int = 0, tokens_out: int = 0, usd: float | None = None,
           latency_ms: int = 0, outcome: str = "ok",
           error: str | None = None, conn: sqlite3.Connection | None = None,
           ts: datetime | None = None) -> float:
    """Write one row. Returns the dollars charged.

    Called for successes, failures, fallbacks and refusals alike. A provider
    that errored still consumed time and sometimes tokens, and a refusal is
    the most interesting row in the table.
    """
    own = conn is None
    conn = conn or connect()
    if usd is None:
        usd = price(model, tokens_in, tokens_out) if outcome == "ok" else 0.0
    stamp = (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute(
            "INSERT INTO costs (ts, job, tier, provider, model, tokens_in, "
            "tokens_out, usd, latency_ms, outcome, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, job, tier, provider, model, tokens_in, tokens_out,
             usd, latency_ms, outcome, error))
        conn.commit()
    finally:
        if own:
            conn.close()
    return usd


# ─────────────────────────────────────────────────────────────────────────────
# reading
# ─────────────────────────────────────────────────────────────────────────────

def _month_prefix(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def month_to_date(now: datetime | None = None,
                  conn: sqlite3.Connection | None = None) -> float:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(usd), 0.0) AS total FROM costs "
            "WHERE ts LIKE ?", (_month_prefix(now) + "%",)).fetchone()
        return round(row["total"], 6)
    finally:
        if own:
            conn.close()


def _group(column: str, now=None, conn=None) -> list[tuple[str, float, int]]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            f"SELECT {column} AS k, SUM(usd) AS usd, COUNT(*) AS n "
            f"FROM costs WHERE ts LIKE ? GROUP BY {column} "
            f"ORDER BY usd DESC", (_month_prefix(now) + "%",)).fetchall()
        return [(r["k"], round(r["usd"], 6), r["n"]) for r in rows]
    finally:
        if own:
            conn.close()


def by_job(now=None, conn=None):
    return _group("job", now, conn)


def by_tier(now=None, conn=None):
    return _group("tier", now, conn)


def by_outcome(now=None, conn=None):
    return _group("outcome", now, conn)


def recent(limit: int = 20, conn=None) -> list[sqlite3.Row]:
    own = conn is None
    conn = conn or connect()
    try:
        return conn.execute(
            "SELECT * FROM costs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        if own:
            conn.close()


def headroom(now=None, conn=None) -> dict:
    """Where the month stands against both numbers."""
    spent = month_to_date(now, conn)
    cap, target = cap_usd(), target_usd()
    return {
        "spent": spent,
        "cap": cap,
        "target": target,
        "left_to_cap": round(cap - spent, 6),
        "left_to_target": round(target - spent, 6),
        "pct_of_target": (spent / target * 100) if target else 0.0,
        "pct_of_cap": (spent / cap * 100) if cap else 0.0,
        "over_target": spent > target,
    }


# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.costs")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", action="store_true")
    g.add_argument("--tail", type=int, metavar="N")
    args = ap.parse_args(argv)

    if args.tail:
        for r in reversed(recent(args.tail)):
            print(f"{r['ts']}  {r['job']:<12} {r['tier']:<8} "
                  f"{r['provider']:<14} {r['outcome']:<8} "
                  f"{r['tokens_in']:>6}in {r['tokens_out']:>6}out "
                  f"${r['usd']:.6f}  {r['latency_ms']}ms"
                  + (f"  {r['error']}" if r["error"] else ""))
        return 0

    h = headroom()
    flag = "  OVER TARGET" if h["over_target"] else ""
    print(f"month-to-date  ${h['spent']:.4f}")
    print(f"  target       ${h['target']:.2f}   "
          f"({h['pct_of_target']:.1f}% used){flag}")
    print(f"  hard cap     ${h['cap']:.2f}   "
          f"(${h['left_to_cap']:.4f} before calls are refused)")
    jobs = by_job()
    if not jobs:
        print("no model calls recorded")
        return 0
    print("\nby job")
    for k, usd, n in jobs:
        print(f"  {k:<16} ${usd:.6f}  {n} call(s)")
    print("\nby tier")
    for k, usd, n in by_tier():
        print(f"  {k:<16} ${usd:.6f}  {n} call(s)")
    print("\nby outcome")
    for k, usd, n in by_outcome():
        print(f"  {k:<16} ${usd:.6f}  {n} call(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
