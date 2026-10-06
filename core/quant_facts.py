"""
core.quant_facts — readable, reconciled facts about eventbot's simulation.

Reads darkweb-jobs/data/eventbot.db read-only (core.bridge). Never writes,
never places orders, never changes EXECUTOR. The mode shown is whatever
eventbot's .env says, read from its positions' `executor` column and the
EXECUTOR setting; "sim" means no broker was involved at all.

## Accounting (stated so the numbers can be checked by hand)

  start          = kv.start_cash (initial simulated equity)
  realized       = SUM(pnl) over closed positions
  open_cost      = SUM(notional) over open positions (what was "paid")
  open_value     = equity_log.positions_value (marked at last tick price)
  unrealized     = open_value - open_cost
  equity         = equity_log.equity = cash + open_value
  expected_cash  = start + realized - open_cost
  residual       = cash - expected_cash   (fees/rounding; shown if non-zero)
  total return   = (equity - start) / start
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

RULE_EXPLAIN = {
    "crypto_mention": "a headline or market move mentioning crypto triggered a long position "
                      "in BTC/ETH",
    "tariff_generic": "a tariff-related headline triggered a long position in a tariff-hedge "
                      "asset (e.g. gold ETF GLD)",
}


def mode(conn) -> str:
    r = conn.execute("SELECT executor, COUNT(*) FROM positions GROUP BY executor "
                     "ORDER BY COUNT(*) DESC").fetchall()
    ex = {e for e, _ in r}
    if ex <= {"sim"}:
        return "simulation"
    if "alpaca" in ex:
        return "paper or live broker (check ALPACA_ENDPOINT)"
    return ",".join(sorted(e or "?" for e in ex))


def accounting(conn) -> dict:
    kv = {k: v for k, v in conn.execute("SELECT k, v FROM kv")}
    start = float(kv.get("start_cash", 0) or 0)
    eq = conn.execute("SELECT ts, equity, cash, positions_value, n_open FROM equity_log "
                      "ORDER BY ts DESC LIMIT 1").fetchone()
    realized, n_closed = conn.execute(
        "SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM positions WHERE status='closed'").fetchone()
    open_cost, n_open = conn.execute(
        "SELECT COALESCE(SUM(notional),0), COUNT(*) FROM positions WHERE status='open'").fetchone()
    first = conn.execute("SELECT MIN(ts) FROM equity_log").fetchone()[0]
    if not eq:
        return {"start": start, "available": False}
    ts, equity, cash, open_value, _ = eq
    unreal = open_value - open_cost
    expected_cash = start + realized - open_cost
    return {
        "available": True, "as_of": ts, "since": first, "start": start,
        "equity": equity, "cash": cash, "open_value": open_value, "open_cost": open_cost,
        "realized": realized, "unrealized": unreal, "n_closed": n_closed, "n_open": n_open,
        "residual": cash - expected_cash,
        "total_return_pct": 100 * (equity - start) / start if start else None,
        "reconciles": abs((equity - start) - (realized + unreal + (cash - expected_cash))) < 0.01,
    }


def change_24h(conn, now=None) -> float | None:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    a = conn.execute("SELECT equity FROM equity_log WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                     (since,)).fetchone()
    b = conn.execute("SELECT equity FROM equity_log ORDER BY ts DESC LIMIT 1").fetchone()
    return (b[0] - a[0]) if a and b else None


def open_positions(conn) -> list[dict]:
    cols = ("id", "asset", "rule", "notional", "entry_price", "entry_ts", "stop", "target",
            "max_exit_ts")
    return [dict(zip(cols, r)) for r in conn.execute(
        "SELECT id, asset, rule, notional, entry_price, entry_ts, stop, target, max_exit_ts "
        "FROM positions WHERE status='open' ORDER BY entry_ts DESC")]


def why_opened(conn, position_id: int) -> dict:
    """The decision record chain position -> idea -> signal, or what is missing."""
    p = conn.execute("SELECT id, asset, rule, idea_id, executor, status, notional, entry_price, "
                     "entry_ts, stop, target, max_exit_ts, exit_price, exit_ts, pnl, exit_reason "
                     "FROM positions WHERE id=?", (position_id,)).fetchone()
    if not p:
        return {"found": False}
    keys = ("id", "asset", "rule", "idea_id", "executor", "status", "notional", "entry_price",
            "entry_ts", "stop", "target", "max_exit_ts", "exit_price", "exit_ts", "pnl",
            "exit_reason")
    pos = dict(zip(keys, p))
    out = {"found": True, "position": pos, "idea": None, "signal": None, "missing": [],
           "rule_meaning": RULE_EXPLAIN.get(pos["rule"])}
    if pos["idea_id"]:
        i = conn.execute("SELECT id, signal_id, rule, asset, sentiment, blocked, entry_price, "
                         "entry_ts, price_source, weight FROM ideas WHERE id=?",
                         (pos["idea_id"],)).fetchone()
        if i:
            out["idea"] = dict(zip(("id", "signal_id", "rule", "asset", "sentiment", "blocked",
                                    "entry_price", "entry_ts", "price_source", "weight"), i))
            s = conn.execute("SELECT id, ts, seen_ts, source, text, url FROM signals WHERE id=?",
                             (i[1],)).fetchone()
            if s:
                out["signal"] = dict(zip(("id", "ts", "seen_ts", "source", "text", "url"), s))
            else:
                out["missing"].append("the triggering signal row")
        else:
            out["missing"].append("the idea record")
    else:
        out["missing"].append("an idea link (position has no idea_id)")
    sig = out["signal"]
    if sig and sig["source"] == "polymarket" and ("0.01" in (sig["text"] or "")
                                                  or "0.99" in (sig["text"] or "")):
        out["warning"] = ("The trigger was a prediction market moving to ~1% or ~99%. "
                          "That is usually the market settling at its end time, not new "
                          "information, so this rule may be reacting to expiry.")
    return out
