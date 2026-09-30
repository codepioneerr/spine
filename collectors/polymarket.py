"""
collectors/polymarket — prediction-market moves, into the item store.

Reads `prediction.db`, which darkweb-jobs fills every 30 minutes from the
Gamma API. It logs one snapshot row per market per run and separately flags
"jumps": markets whose YES price moved at least 8 probability points between
consecutive snapshots, filtered to markets with real 24h volume.

**This collector reads jumps, not snapshots.** `snapshots` holds 57,216 rows
and grows by ~300 every half hour; it is a time series, and a time series
does not belong in a queue of things to look at. `jumps` is already the
"something happened" table — 3,022 rows over the same period — and that is
what an item store is for.

## The key

`jump:<market_id>:<ts>` — the primary key of the source row. A jump is a
discrete event at a moment in time, so the same market jumping again
tomorrow is correctly a different item. Using bare `market_id` would make
every subsequent move overwrite the last one and quietly reset its own age.

## Why `data: "public"`

Polymarket prices are public market data. Nothing here is Nick's.
"""

from __future__ import annotations

from core import bridge

META = {
    "id": "polymarket",
    # :12 and :42 UTC. darkweb-jobs snapshots on */30 (:00/:30) and
    # heartbeat fires at :07/:37, so this sits in the gap after the writer
    # has finished and away from Spine's own job.
    "schedule": "12-59/30 * * * *",
    "timeout": 120,
    "ram_mb": 90,
    "window": "any",
    "weight": "light",
    "tier": None,
    "data": "public",
    "description": "Polymarket price jumps worth a look",
}

# 6 hours. The source writes every 30 minutes, so this covers eleven missed
# runs. Without a window the first execution would emit all 3,022 historical
# jumps at once.
LOOKBACK_HOURS = 6

# Below this the move is real but the market is too thin to mean anything.
# darkweb-jobs already filters at $20k when detecting jumps; this is a
# second, higher bar for "worth Nick's attention" as opposed to "worth
# recording".
DEFAULT_MIN_VOLUME = 250_000

MAX_ROWS = 200

SQL = """
SELECT ts, market_id, question, prev_price, new_price, delta,
       volume24h, political
  FROM jumps
 WHERE ts >= ?
   AND volume24h >= ?
   AND political = 1
 ORDER BY ABS(delta) DESC, ts DESC
 LIMIT ?
"""


def importance(delta: float | None, volume: float | None) -> int:
    """Size of the move, weighted by whether the market is liquid enough to
    believe. A 30-point move on a $60k market is noise; the same move on a
    $2M market is information.
    """
    d = abs(delta or 0)
    if d >= 0.30:
        base = 78
    elif d >= 0.20:
        base = 70
    elif d >= 0.15:
        base = 62
    elif d >= 0.10:
        base = 55
    else:
        base = 48
    v = volume or 0
    if v >= 1_000_000:
        base += 6
    elif v >= 250_000:
        base += 3
    return max(0, min(100, base))


def title_of(row) -> str:
    prev, new = row["prev_price"], row["new_price"]
    arrow = "up" if (row["delta"] or 0) > 0 else "down"
    pts = abs(row["delta"] or 0) * 100
    q = (row["question"] or "").strip() or row["market_id"]
    return f"{arrow} {pts:.0f}pt ({prev:.0%} -> {new:.0%}) — {q}"


def body_of(row) -> str:
    bits = [f"24h volume ${(row['volume24h'] or 0):,.0f}"]
    if row["political"]:
        bits.append("political")
    bits.append(f"jumped {str(row['ts'])[:16].replace('T', ' ')} UTC")
    return " · ".join(bits)


def select(conn, since: str, min_volume: int, limit: int = MAX_ROWS):
    return conn.execute(SQL, (since, min_volume, limit)).fetchall()


def build_items(rows) -> list[dict]:
    items = []
    for row in rows:
        items.append({
            "kind": "signal",
            "key": f"jump:{row['market_id']}:{row['ts']}",
            "title": title_of(row)[:500],
            "body": body_of(row),
            "url": f"https://polymarket.com/market/{row['market_id']}",
            "importance": importance(row["delta"], row["volume24h"]),
            "data": {
                "market_id": row["market_id"],
                "question": row["question"],
                "prev_price": row["prev_price"],
                "new_price": row["new_price"],
                "delta": row["delta"],
                "volume24h": row["volume24h"],
                "political": bool(row["political"]),
                "jumped_at": row["ts"],
            },
        })
    return items


def run(ctx):
    min_volume = bridge.env_int("SPINE_POLYMARKET_MIN_VOLUME",
                                DEFAULT_MIN_VOLUME)
    since = bridge.since_iso(LOOKBACK_HOURS, now=ctx.now)
    ctx.log(f"polymarket: reading prediction.db jumps since {since}, "
            f"min 24h volume ${min_volume:,}")

    conn = bridge.connect("prediction")
    try:
        rows = select(conn, since, min_volume)
    finally:
        conn.close()

    items = build_items(rows)
    ctx.log(f"polymarket: {len(items)} jump(s)")

    if ctx.dry_run:
        for it in items[:10]:
            ctx.log(f"  would emit [{it['importance']}] {it['title']}")
        return {"items": items, "stats": {"dry_run": True,
                                          "would_emit": len(items)}}

    result = ctx.db.put(items) if items else {"new": 0, "updated": 0,
                                              "total": 0}
    ctx.log(f"item store: {result['new']} new, {result['updated']} updated")
    return {"items": items, "stats": result}
