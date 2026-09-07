"""
collectors/eventbot — paper-trading positions, into the item store.

Reads `eventbot.db`, which darkweb-jobs fills every five minutes: headlines
and price jumps become signals, signals become ideas, some ideas become
paper positions with a stop and a target.

## Scope: positions only. Signals are deliberately NOT bridged.

`signals` holds 3,588 rows and `ideas` 1,070. Both are eventbot's internal
machinery — the funnel that produces a position — and eventbot's own 07:00
Telegram digest already reports "latest matched signals" from them.

Nick decided on Sept 6 that eventbot's digest and Spine's brief run **side
by side**, with nothing absorbed. Bridging signals would quietly break that
decision: the same headlines would appear in two places every morning, and
the one that got read would be whichever arrived on Telegram. So this
collector takes only the thing eventbot's digest under-serves and that has
money attached — the positions themselves.

If the brief later absorbs the digest (Phase 4 territory, when Spine gets
Telegram delivery), signals come across then, as one deliberate change.

## The key

`position:<id>` — the row's primary key. A position is a single object with
a life cycle: it opens, it may hit a stop or a target, it closes. Because
`put()` updates a row in place on key conflict, the same item follows the
position from open to closed, and `ts` keeps meaning when it opened.

## Why `data: "private"`

This is the one bridged collector that is not public data. Polymarket prices
and ACRIS filings are the world's; a position is a record of what Nick's
system decided to do with his money, even in paper form. `private` is also
the framework default, and CLAUDE.md is explicit that the failure being
designed against is omission — so the burden is on marking something public,
not on marking it private.

**Consequence under Phase 3's option C:** positions are listed
deterministically in the brief and never enter a prompt to the free tier.
That is the correct outcome, and it is worth checking that it still feels
correct once the brief is real — flagged for review rather than assumed.
"""

from __future__ import annotations

from core import bridge

META = {
    "id": "eventbot",
    # 06:20 UTC, ahead of acris at 06:30. eventbot's tick runs every five
    # minutes so there is no quiet moment to aim for; the read is read-only
    # and takes milliseconds against 18 rows.
    "schedule": "20 6 * * *",
    "timeout": 120,
    "ram_mb": 80,
    "window": "any",
    "weight": "light",
    "tier": None,
    "data": "private",
    "description": "eventbot paper positions, open and recently closed",
}

# Positions opened or closed in this window, plus anything still open
# regardless of age. 48h so a Monday brief still shows Friday's activity.
LOOKBACK_HOURS = 48

MAX_ROWS = 100

SQL = """
SELECT id, asset, rule, idea_id, executor, status, qty, notional,
       entry_price, entry_ts, stop, target, max_exit_ts,
       exit_price, exit_ts, pnl, exit_reason
  FROM positions
 WHERE status = 'open'
    OR entry_ts >= ?
    OR (exit_ts IS NOT NULL AND exit_ts >= ?)
 ORDER BY COALESCE(exit_ts, entry_ts) DESC
 LIMIT ?
"""


def importance(row) -> int:
    """An open position is a live obligation; a closed one is a result.

    Losses rank above wins on purpose. A stopped-out trade is the one worth
    looking at — it is where the rule was wrong, and eventbot's whole
    learning loop runs on that.
    """
    if row["status"] == "open":
        return 65
    pnl = row["pnl"] or 0
    if pnl < 0:
        return 60
    return 50


def title_of(row) -> str:
    asset = row["asset"] or "?"
    if row["status"] == "open":
        notional = row["notional"] or 0
        return f"open {asset} ${notional:,.0f} via {row['rule'] or '?'}"
    pnl = row["pnl"]
    money = f"{pnl:+,.2f}" if pnl is not None else "n/a"
    reason = row["exit_reason"] or "closed"
    return f"closed {asset} {money} ({reason})"


def body_of(row) -> str:
    bits = []
    if row["entry_price"] is not None:
        bits.append(f"entry {row['entry_price']:.4g}")
    if row["exit_price"] is not None:
        bits.append(f"exit {row['exit_price']:.4g}")
    if row["stop"] is not None:
        bits.append(f"stop {row['stop']:.4g}")
    if row["target"] is not None:
        bits.append(f"target {row['target']:.4g}")
    if row["entry_ts"]:
        bits.append(f"opened {str(row['entry_ts'])[:16].replace('T', ' ')}")
    if row["executor"]:
        bits.append(f"via {row['executor']}")
    return " · ".join(bits)


def select(conn, since: str, limit: int = MAX_ROWS):
    return conn.execute(SQL, (since, since, limit)).fetchall()


def build_items(rows) -> list[dict]:
    items = []
    for row in rows:
        items.append({
            # A position is a state of the world, not a call to action —
            # "fact", not "task". Nick does not act on these; eventbot does.
            "kind": "fact",
            "key": f"position:{row['id']}",
            "title": title_of(row)[:500],
            "body": body_of(row),
            "importance": importance(row),
            "data": {
                "position_id": row["id"],
                "asset": row["asset"],
                "rule": row["rule"],
                "status": row["status"],
                "notional": row["notional"],
                "entry_price": row["entry_price"],
                "entry_ts": row["entry_ts"],
                "exit_price": row["exit_price"],
                "exit_ts": row["exit_ts"],
                "pnl": row["pnl"],
                "exit_reason": row["exit_reason"],
                "executor": row["executor"],
            },
        })
    return items


def run(ctx):
    since = bridge.since_iso(LOOKBACK_HOURS, now=ctx.now)
    ctx.log(f"eventbot: reading positions open or touched since {since}")

    conn = bridge.connect("eventbot")
    try:
        rows = select(conn, since)
    finally:
        conn.close()

    items = build_items(rows)
    n_open = sum(1 for r in rows if r["status"] == "open")
    ctx.log(f"eventbot: {len(items)} position(s), {n_open} open")

    if ctx.dry_run:
        for it in items[:10]:
            ctx.log(f"  would emit [{it['importance']}] {it['title']}")
        return {"items": items, "stats": {"dry_run": True,
                                          "would_emit": len(items)}}

    result = ctx.db.put(items) if items else {"new": 0, "updated": 0,
                                              "total": 0}
    ctx.log(f"item store: {result['new']} new, {result['updated']} updated")
    return {"items": items, "stats": {"open": n_open, **result}}
