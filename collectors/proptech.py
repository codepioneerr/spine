"""
collectors/proptech — Phase 3. Property history and the repeat-sale signal.

Three steps, every night:

  1. **Keep.** Copy every ACRIS deed/mortgage at or above
     SPINE_PROPTECH_STORE_MIN ($1M) out of darkweb-jobs (read-only) into
     Spine's own tables (core.proptech). darkweb-jobs holds ~6 weeks; Spine
     keeps everything, because a repeat sale compares a parcel against a
     sale that may be years old.

  2. **Fill.** Give each parcel its PLUTO facts: first from darkweb-jobs'
     slice, then — for the ~94% that slice does not cover — by BBL lookup
     against NYC's public PLUTO dataset, capped per run. See core.proptech
     for why this one fetch lives in Spine.

  3. **Signal.** Emit one item per newly seen repeat sale: the same parcel
     sold twice, single-parcel whole-interest deeds only, with the price
     change and its annualised rate.

The $5M deal feed stays in collectors/acris, which now also reads the
parcels filled here, so a $6M Queens deed in the brief has a building class
and an owner instead of "not in the PLUTO slice".

## The key

`{bbl}:{document_id}` of the resale. A parcel can resell many times; each
resale is its own event, and the document number makes it stable.

## Why `data: "public"`

ACRIS and PLUTO are NYC open data. Nothing in this collector is Nick's.
"""

from __future__ import annotations

from datetime import timedelta

from core import bridge, proptech
from core.http import HttpError

META = {
    "id": "proptech",
    # 06:45 UTC: after darkweb-jobs' 06:00 ACRIS fetch and after acris at
    # 06:30 (timeout 300), so the three never contend. acris therefore reads
    # parcels filled the night before; it re-emits a 72h window, so a deed
    # picks up its PLUTO facts on the next run.
    "schedule": "45 6 * * *",
    "timeout": 900,
    "ram_mb": 120,
    "window": "any",
    "weight": "light",
    "tier": None,
    "data": "public",
    "description": "ACRIS history, PLUTO depth, repeat-sale signals",
}

PLUTO_URL = "https://data.cityofnewyork.us/resource/64uk-42ks.json"

# Socrata accepts long $where clauses, but 50 keys per request keeps each URL
# well under proxy limits and makes one failed request cost little.
PLUTO_BATCH = 50

# Per-run lookup budget. ~1,000 parcels is 20 requests, under a minute.
# The first nights backfill; after that the nightly inflow is ~85 documents.
DEFAULT_PLUTO_MAX = 1000

# Stop asking after this many consecutive failed requests. One timeout is
# weather; three is an outage, and hammering an outage helps nobody.
MAX_CONSECUTIVE_FAILURES = 3

# A resale is "new" if Spine stored it within this window. Wider than a day
# so a missed run or two does not lose a signal; the store dedups the rest.
SIGNAL_WINDOW_DAYS = 7

DWJ_SQL = """
SELECT m.document_id, m.doc_type, m.document_date, m.recorded_datetime,
       m.document_amt, m.percent_trans, l.bbl
  FROM acris_master m
  LEFT JOIN acris_legals l ON l.document_id = m.document_id
 WHERE m.document_amt >= ?
   AND m.doc_type IN ({types})
""".format(types=",".join("?" * len(proptech.KEPT_TYPES)))

DWJ_PLUTO_SQL = "SELECT * FROM pluto WHERE bbl IN ({})"

PLUTO_SELECT = ",".join(("bbl",) + proptech.PARCEL_FIELDS)

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

INTERRUPT_AT = 80


# ─────────────────────────────────────────────────────────────────────────────
# step 1 + 2a: read darkweb-jobs
# ─────────────────────────────────────────────────────────────────────────────

def read_dwj(store_min: int):
    """All kept documents and the PLUTO slice rows for their parcels.

    The whole table every night, not a lookback window. It is ~22k rows, the
    upsert is idempotent, and it means Spine's copy can never miss a document
    because a run was skipped — the property the lookback windows elsewhere
    in Phase 2b trade away to avoid flooding the item store, which this does
    not write to.
    """
    conn = bridge.connect("proptech")
    try:
        docs = conn.execute(DWJ_SQL,
                            (store_min, *proptech.KEPT_TYPES)).fetchall()
        bbls = sorted({r["bbl"] for r in docs if r["bbl"]})
        slice_rows = []
        for i in range(0, len(bbls), 500):
            chunk = bbls[i:i + 500]
            slice_rows += [dict(r) for r in conn.execute(
                DWJ_PLUTO_SQL.format(",".join("?" * len(chunk))), chunk)]
    finally:
        conn.close()
    return docs, slice_rows


# ─────────────────────────────────────────────────────────────────────────────
# step 2b: PLUTO lookups
# ─────────────────────────────────────────────────────────────────────────────

def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_pluto(rec: dict) -> dict:
    """One Socrata record to a parcels row. Socrata returns every number as a
    string ("27.0000000"); integers that are really integers become ints."""
    out = {"bbl": proptech.normalize_bbl(rec.get("bbl"))}
    for f in proptech.PARCEL_FIELDS:
        v = rec.get(f)
        if f in ("unitsres", "unitstotal", "yearbuilt"):
            n = _num(v)
            out[f] = int(n) if n is not None else None
        elif f in ("numfloors", "bldgarea", "lotarea", "assessland",
                   "assesstot", "latitude", "longitude"):
            out[f] = _num(v)
        else:
            out[f] = str(v).strip() if v not in (None, "") else None
    # PLUTO writes yearbuilt 0 for "unknown". Unknown is None.
    if out.get("yearbuilt") == 0:
        out["yearbuilt"] = None
    return out


def fetch_pluto(http, bbls: list[str], app_token: str | None = None,
                log=lambda *_: None) -> tuple[list[dict], list[str], int]:
    """Look up BBLs in PLUTO. Returns (found rows, absent bbls, failures).

    A BBL is only marked absent when its batch succeeded and did not contain
    it. A failed batch marks nothing — "we could not ask" is not "PLUTO
    does not have it", and conflating them would hide a parcel for a month.
    """
    headers = {"X-App-Token": app_token} if app_token else None
    found: list[dict] = []
    absent: list[str] = []
    failures = streak = 0
    for i in range(0, len(bbls), PLUTO_BATCH):
        batch = bbls[i:i + PLUTO_BATCH]
        params = {"$select": PLUTO_SELECT,
                  "$where": f"bbl in ({','.join(str(int(b)) for b in batch)})",
                  "$limit": str(len(batch) * 2)}
        try:
            recs = http.get_json(PLUTO_URL, params=params, headers=headers)
        except HttpError as exc:
            failures += 1
            streak += 1
            log(f"proptech: PLUTO batch {i // PLUTO_BATCH} failed: {exc}")
            if streak >= MAX_CONSECUTIVE_FAILURES:
                log("proptech: PLUTO looks down; stopping lookups for this run")
                break
            continue
        streak = 0
        rows = [parse_pluto(r) for r in recs if isinstance(r, dict)]
        rows = [r for r in rows if r["bbl"]]
        got = {r["bbl"] for r in rows}
        found += rows
        absent += [b for b in batch if b not in got]
    return found, absent, failures


# ─────────────────────────────────────────────────────────────────────────────
# step 3: repeat-sale items
# ─────────────────────────────────────────────────────────────────────────────

def importance(sale: dict) -> int:
    """0-100, below the 80 interrupt line unless the deal is both large and a
    big move. A repeat sale is context for a decision, rarely the decision."""
    amt = sale["resold_for"] or 0
    pct = sale["pct_change"]
    base = 50
    if amt >= 20_000_000:
        base += 15
    elif amt >= 5_000_000:
        base += 10
    elif amt >= 2_000_000:
        base += 5
    if pct >= 50 or pct <= -30:
        base += 12
    elif pct >= 20 or pct <= -15:
        base += 6
    if amt >= 50_000_000 and (pct >= 50 or pct <= -30):
        base = max(base, 82)
    return max(0, min(100, base))


def _money(v) -> str:
    return f"${v:,.0f}" if v else "n/a"


def address_of(sale: dict, parcel: dict | None) -> str:
    if parcel and parcel.get("address"):
        return parcel["address"]
    return f"BBL {sale['bbl']}"


def build_items(sales: list[dict], parcels: dict[str, dict]) -> list[dict]:
    """Pure: repeat sales plus parcel facts to items."""
    items = []
    for s in sales:
        p = parcels.get(s["bbl"])
        boro = BOROUGHS.get(s["bbl"][0], "")
        sign = "+" if s["pct_change"] >= 0 else ""
        title = (f"RESALE {sign}{s['pct_change']:.0f}% — "
                 f"{address_of(s, p)}{', ' + boro if boro else ''}: "
                 f"{_money(s['sold_for'])} → {_money(s['resold_for'])}")
        years = s["days_held"] / 365
        held = f"{years:.1f}y" if years >= 1 else f"{s['days_held']}d"
        bits = [f"held {held}",
                f"{s['pct_annualised']:+.1f}%/yr annualised",
                f"sold {s['sold_on']}, resold {s['resold_on']}"]
        if p:
            if p.get("bldgclass"):
                bits.append(f"class {p['bldgclass']}")
            if p.get("unitstotal"):
                bits.append(f"{p['unitstotal']} units")
            if p.get("bldgarea") and s["resold_for"]:
                bits.append(f"${s['resold_for'] / p['bldgarea']:,.0f}/sqft")
            if p.get("ownername"):
                bits.append(f"owner {str(p['ownername'])[:60]}")
        items.append({
            "kind": "signal",
            "key": f"{s['bbl']}:{s['document_id']}",
            "title": title[:500],
            "body": " · ".join(bits),
            "url": ("https://a836-acris.nyc.gov/DS/DocumentSearch/"
                    f"DocumentImageView?doc_id={s['document_id']}"),
            "importance": importance(s),
            "data": {**s, "borough": boro or None,
                     "address": address_of(s, p),
                     "bldgclass": (p or {}).get("bldgclass"),
                     "unitstotal": (p or {}).get("unitstotal"),
                     "bldgarea": (p or {}).get("bldgarea")},
        })
    return items


# ─────────────────────────────────────────────────────────────────────────────

def run(ctx):
    store_min = bridge.env_int("SPINE_PROPTECH_STORE_MIN",
                               proptech.DEFAULT_STORE_MIN)
    pluto_max = bridge.env_int("SPINE_PLUTO_FETCH_MAX", DEFAULT_PLUTO_MAX)
    docs, slice_rows = read_dwj(store_min)
    ctx.log(f"proptech: {len(docs)} legal row(s) >= ${store_min:,} in "
            f"darkweb-jobs, {len(slice_rows)} in its PLUTO slice")

    if ctx.dry_run:
        conn = proptech.connect(":memory:")
    else:
        conn = proptech.connect()
    try:
        kept = proptech.upsert_docs(conn, docs)
        proptech.upsert_parcels(conn, slice_rows, origin="dwj")

        todo = proptech.bbls_needing_pluto(conn, pluto_max, now=ctx.now)
        found, absent, failures = [], [], 0
        if todo and not ctx.dry_run:
            token = ctx.secrets.get("SPINE_SOCRATA_APP_TOKEN") if ctx.secrets else None
            found, absent, failures = fetch_pluto(ctx.http, todo, token, ctx.log)
            proptech.upsert_parcels(conn, found, origin="pluto")
            proptech.mark_absent(conn, absent)
        ctx.log(f"proptech: PLUTO lookups {len(todo)} asked, {len(found)} "
                f"found, {len(absent)} absent, {failures} failed batch(es)")

        since = (ctx.now - timedelta(days=SIGNAL_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        sales = proptech.repeat_sales(conn, since_first_seen=since)
        parcels = proptech.parcels_for(conn, [s["bbl"] for s in sales])
        items = build_items(sales, parcels)
        stats = {**kept, "pluto_asked": len(todo), "pluto_found": len(found),
                 "pluto_absent": len(absent), "pluto_failed_batches": failures,
                 "repeat_sales": len(sales), **proptech.counts(conn)}
    finally:
        conn.close()

    ctx.log(f"proptech: {stats['docs']} documents kept, PLUTO coverage "
            f"{stats['pluto_coverage_pct']}%, {len(items)} repeat sale(s)")
    if ctx.dry_run:
        for it in items[:10]:
            ctx.log(f"  would emit [{it['importance']}] {it['title']}")
        return {"items": items, "stats": {"dry_run": True, **stats}}

    result = ctx.db.put(items) if items else {"new": 0, "updated": 0, "total": 0}
    ctx.log(f"item store: {result['new']} new, {result['updated']} updated")
    return {"items": items, "stats": {**stats, **result}}
