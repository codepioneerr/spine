"""
collectors/acris — NYC recorded deeds and mortgages, into the item store.

Reads `proptech.db`, which darkweb-jobs fills nightly from ACRIS via
Socrata. Joins the document to its legal record (which gives the BBL) and
then to the PLUTO slice (which gives the building). Emits one item per
document above a dollar threshold.

## The key

`document_id` — the ACRIS document number. Stable forever, unique per
recorded instrument, and exactly the kind of thing CLAUDE.md means by "a
document number is a good key. A row index is not."

## The PLUTO join is LEFT, deliberately

darkweb-jobs only keeps a PLUTO slice: Manhattan and Brooklyn, six or more
residential units. An INNER join would silently drop every Queens deed and
every small building — and it would look like a quiet week rather than a
filter. LEFT keeps the document and leaves the building fields null, which
is honest about what is and is not known.

## Why `data: "public"`

ACRIS and PLUTO are NYC open data. Nothing here is Nick's. This is one of
the few collectors that can legitimately claim `public`, and Phase 3's
option C depends on that claim being accurate — a public item gets
summarised by the free tier, so mislabelling something here is how a private
fact reaches a logging endpoint.
"""

from __future__ import annotations

from core import bridge

META = {
    "id": "acris",
    # 06:30 UTC. darkweb-jobs runs its own ACRIS fetch at 06:00 UTC
    # (crontab.txt: "0 6 * * * proptech.collect"), so this reads thirty
    # minutes after the writer finishes. Both are serialized by their own
    # flocks, which are separate domains — we stay out of the way by clock,
    # the same trick heartbeat uses against the polymarket snapshot.
    "schedule": "30 6 * * *",
    "timeout": 300,
    "ram_mb": 120,
    "window": "any",
    "weight": "light",
    "tier": None,
    "data": "public",
    "description": "NYC recorded deeds/mortgages above a threshold",
}

# Documents below this are noise: satisfactions, $0 transfers between
# related parties, and the long tail of small residential closings. The
# default is a guess and is meant to be tuned once the burn-in shows real
# daily volume — see bin/compare-migration --preview.
DEFAULT_MIN_AMOUNT = 1_000_000

# How far back to look on each run. The nightly fetch pulls ~30 days on
# backfill and a day or two incrementally, so 72h covers a missed run or
# two without re-emitting the entire backfill on first execution.
LOOKBACK_HOURS = 72

# The document types worth surfacing. darkweb-jobs collects six; SAT
# (satisfaction of mortgage) and ASST (assignment) are administrative and
# would triple the volume for very little signal.
INTERESTING_TYPES = ("DEED", "DEEDO", "MTGE")

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

SQL = """
SELECT m.document_id, m.doc_type, m.document_date, m.recorded_datetime,
       m.document_amt, m.percent_trans,
       l.bbl, l.borough, l.street_number, l.street_name, l.unit,
       l.property_type,
       p.address, p.zipcode, p.bldgclass, p.zonedist1, p.unitsres,
       p.unitstotal, p.yearbuilt, p.bldgarea, p.assesstot, p.ownername
  FROM acris_master m
  LEFT JOIN acris_legals l ON l.document_id = m.document_id
  LEFT JOIN pluto       p ON p.bbl = l.bbl
 WHERE m.fetched_ts >= ?
   AND m.document_amt >= ?
   AND m.doc_type IN ({types})
 ORDER BY m.recorded_datetime DESC
 LIMIT ?
""".format(types=",".join("?" * len(INTERESTING_TYPES)))

# A single deed can have several legal records (a building sold with three
# lots). The join multiplies those out, so cap generously and dedup by
# document_id in Python — the store would dedup anyway, but emitting the
# same document five times makes the run stats lie.
MAX_ROWS = 2000


def importance(amount: float | None, unitsres: int | None) -> int:
    """0-100. Above 80 is reserved for "worth interrupting Nick about".

    Money is the primary axis because this is a deal feed. Unit count nudges
    it up: a $5M twenty-unit building is a more interesting comp than a $5M
    townhouse, for someone whose differentiator is rental time series.
    """
    if not amount:
        return 40
    if amount >= 50_000_000:
        base = 85
    elif amount >= 20_000_000:
        base = 75
    elif amount >= 10_000_000:
        base = 68
    elif amount >= 5_000_000:
        base = 60
    elif amount >= 2_000_000:
        base = 52
    else:
        base = 45
    if unitsres and unitsres >= 20:
        base += 5
    elif unitsres and unitsres >= 6:
        base += 2
    return max(0, min(100, base))


def address_of(row) -> str:
    """PLUTO's address if we have it, else reassemble ACRIS's parts."""
    if row["address"]:
        return str(row["address"]).strip()
    parts = [row["street_number"], row["street_name"]]
    line = " ".join(str(p).strip() for p in parts if p)
    if row["unit"]:
        line = f"{line} #{row['unit']}"
    return line.strip() or (row["bbl"] or "unknown address")


def title_of(row) -> str:
    amt = row["document_amt"]
    money = f"${amt:,.0f}" if amt else "amount n/a"
    boro = BOROUGHS.get(str(row["borough"]), "")
    where = address_of(row)
    return f"{row['doc_type']} {money} — {where}{', ' + boro if boro else ''}"


def body_of(row) -> str:
    bits = []
    if row["recorded_datetime"]:
        bits.append(f"recorded {str(row['recorded_datetime'])[:10]}")
    if row["unitsres"]:
        bits.append(f"{row['unitsres']} res units")
    if row["unitstotal"] and row["unitstotal"] != row["unitsres"]:
        bits.append(f"{row['unitstotal']} total")
    if row["yearbuilt"]:
        bits.append(f"built {row['yearbuilt']}")
    if row["bldgclass"]:
        bits.append(f"class {row['bldgclass']}")
    if row["zonedist1"]:
        bits.append(f"zoned {row['zonedist1']}")
    if row["assesstot"]:
        bits.append(f"assessed ${row['assesstot']:,.0f}")
    if row["ownername"]:
        bits.append(f"owner {str(row['ownername']).strip()[:60]}")
    if not row["bbl"]:
        bits.append("no legal record yet")
    elif row["address"] is None:
        bits.append("not in the PLUTO slice (MN/BK, 6+ units)")
    return " · ".join(bits)


def select(conn, since: str, min_amount: int, limit: int = MAX_ROWS):
    """The query, factored out so compare-migration can run it too."""
    return conn.execute(
        SQL, (since, min_amount, *INTERESTING_TYPES, limit)).fetchall()


def build_items(rows) -> list[dict]:
    """Rows to items. Pure — no I/O, so the tests can hit it directly."""
    seen: set[str] = set()
    items: list[dict] = []
    for row in rows:
        doc = row["document_id"]
        if doc in seen:
            continue
        seen.add(doc)
        items.append({
            "kind": "deal",
            "key": doc,
            "title": title_of(row)[:500],
            "body": body_of(row),
            "url": ("https://a836-acris.nyc.gov/DS/DocumentSearch/"
                    f"DocumentImageView?doc_id={doc}"),
            "importance": importance(row["document_amt"], row["unitsres"]),
            "data": {
                "document_id": doc,
                "doc_type": row["doc_type"],
                "amount": row["document_amt"],
                "recorded": row["recorded_datetime"],
                "bbl": row["bbl"],
                "borough": BOROUGHS.get(str(row["borough"])),
                "address": address_of(row),
                "zipcode": row["zipcode"],
                "bldgclass": row["bldgclass"],
                "unitsres": row["unitsres"],
                "unitstotal": row["unitstotal"],
                "yearbuilt": row["yearbuilt"],
                "bldgarea": row["bldgarea"],
                "assesstot": row["assesstot"],
                "ownername": row["ownername"],
                "in_pluto": row["address"] is not None,
            },
        })
    return items


def run(ctx):
    min_amount = bridge.env_int("SPINE_PROPTECH_MIN_AMOUNT",
                                DEFAULT_MIN_AMOUNT)
    since = bridge.since_iso(LOOKBACK_HOURS, now=ctx.now)
    ctx.log(f"acris: reading proptech.db since {since}, "
            f"min ${min_amount:,}")

    conn = bridge.connect("proptech")
    try:
        rows = select(conn, since, min_amount)
    finally:
        conn.close()

    items = build_items(rows)
    ctx.log(f"acris: {len(rows)} joined row(s) -> {len(items)} document(s)")

    if ctx.dry_run:
        for it in items[:10]:
            ctx.log(f"  would emit [{it['importance']}] {it['title']}")
        return {"items": items, "stats": {"dry_run": True,
                                          "would_emit": len(items)}}

    result = ctx.db.put(items) if items else {"new": 0, "updated": 0,
                                              "total": 0}
    ctx.log(f"item store: {result['new']} new, {result['updated']} updated")
    return {"items": items, "stats": {"rows": len(rows), **result}}
