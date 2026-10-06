"""
core.proptech — Spine's own property history: ACRIS documents, the parcels
they touch, and PLUTO facts about those parcels.

## Why Spine keeps a copy at all

The item store holds things Nick might act on (CLAUDE.md 7a). A $1.4M deed
in Queens is not that: at ~85 documents a day above $1M, putting them in
`items` would bury the brief. But the same deed is exactly what a repeat-sale
signal needs eighteen months from now, when that parcel sells again.

darkweb-jobs holds about six weeks of ACRIS and makes no promise to hold
more. A repeat-sale query "gets better by accumulating history and by
nothing else" (queries/spine_acris_price_jumps.sql), so the history has to
live somewhere Spine controls. These tables are that place. They are
reference data, not items, which is why they sit beside `items` rather
than in it.

## Two thresholds, decided 2026-10-06

    SPINE_PROPTECH_STORE_MIN   $1M   what this module keeps (~85 docs/day)
    SPINE_PROPTECH_MIN_AMOUNT  $5M   what collectors/acris puts in the brief

Measured by `bin/compare-migration --preview` over the 30 days to Oct 5:
$1M was 85.3 docs/day and $5M 12.5/day, both during the city's backlog
catch-up. The $5M cut is about attention; the $1M cut is about not throwing
away the earlier sale a future repeat-sale comparison needs. Storage at
$1M is roughly 30k rows a year, which SQLite does not notice.

## PLUTO coverage, and the one place Spine fetches

darkweb-jobs keeps a PLUTO slice — Manhattan and Brooklyn, six or more
residential units — and only ~6% of ACRIS legals join to it. Widening that
slice would mean editing darkweb-jobs, which CLAUDE.md 6 forbids. So
collectors/proptech looks up the missing parcels itself, by BBL, against
NYC's public PLUTO dataset. That is a deliberate, narrow exception to
"darkweb-jobs is the fetch layer": a by-key lookup of public reference
data, budgeted per run, with no cursor to keep and nothing to retire.

A BBL PLUTO does not have is remembered as `absent` and not asked about
again for ABSENT_RETRY_DAYS. Most of those are condominium unit lots
(lot 1001+), which ACRIS records per unit and PLUTO rolls up under a
billing lot; mapping them needs a different dataset and is not done here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from core import store

DEFAULT_STORE_MIN = 1_000_000
ABSENT_RETRY_DAYS = 30

# The deed types that represent a transfer of the parcel. MTGE is kept in
# acris_docs (a large new mortgage is its own signal) but never priced.
SALE_TYPES = ("DEED", "DEEDO")
KEPT_TYPES = ("DEED", "DEEDO", "MTGE")

# A hold shorter than this is usually a same-day assignment or a correction
# deed, not a resale. Same cut as the hand-written query.
MIN_HOLD_DAYS = 30

PARCEL_FIELDS = ("borough", "address", "zipcode", "zonedist1", "bldgclass",
                 "landuse", "unitsres", "unitstotal", "yearbuilt", "numfloors",
                 "bldgarea", "lotarea", "assessland", "assesstot", "ownername",
                 "latitude", "longitude")

SCHEMA = """
CREATE TABLE IF NOT EXISTS acris_docs (
    document_id   TEXT PRIMARY KEY,
    doc_type      TEXT NOT NULL,
    document_date TEXT,
    recorded      TEXT,
    amount        REAL,
    percent_trans REAL,
    n_parcels     INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_acris_docs_recorded ON acris_docs(recorded);

CREATE TABLE IF NOT EXISTS acris_doc_parcels (
    document_id TEXT NOT NULL,
    bbl         TEXT NOT NULL,
    PRIMARY KEY (document_id, bbl)
);
CREATE INDEX IF NOT EXISTS ix_acris_doc_parcels_bbl ON acris_doc_parcels(bbl);

CREATE TABLE IF NOT EXISTS parcels (
    bbl        TEXT PRIMARY KEY,
    origin     TEXT NOT NULL,      -- dwj | pluto | absent
    borough TEXT, address TEXT, zipcode TEXT, zonedist1 TEXT, bldgclass TEXT,
    landuse TEXT, unitsres INTEGER, unitstotal INTEGER, yearbuilt INTEGER,
    numfloors REAL, bldgarea REAL, lotarea REAL, assessland REAL,
    assesstot REAL, ownername TEXT, latitude REAL, longitude REAL,
    fetched_ts TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    """spine.db, with the proptech tables present. ":memory:" gives a
    throwaway database, which is what a dry run writes into."""
    if path == ":memory:":
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    else:
        conn = store.connect(path)
    conn.executescript(SCHEMA)
    return conn


def normalize_bbl(raw) -> str | None:
    """10-digit BBL string, the format ACRIS legals use.

    PLUTO's API returns "1008010001.00000000"; ACRIS returns "1008010001".
    Anything that is not a plausible borough-block-lot is None rather than
    a guess, because a wrong BBL joins to the wrong building silently.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        n = int(float(s)) if s else 0
    except ValueError:
        return None
    out = f"{n:010d}"
    return out if len(out) == 10 and out[0] in "12345" else None


# ─────────────────────────────────────────────────────────────────────────────
# writes
# ─────────────────────────────────────────────────────────────────────────────

def upsert_docs(conn, rows) -> dict:
    """rows: dicts/Rows with document_id, doc_type, document_date,
    recorded_datetime, document_amt, percent_trans, bbl (one row per legal).

    Returns {"docs_new": n, "docs_seen": n, "links": n}. first_seen is
    preserved on conflict, same reasoning as items.ts.
    """
    docs: dict[str, dict] = {}
    links: set[tuple[str, str]] = set()
    for r in rows:
        doc = r["document_id"]
        docs.setdefault(doc, r)
        bbl = normalize_bbl(r["bbl"])
        if bbl:
            links.add((doc, bbl))

    before = conn.execute("SELECT COUNT(*) FROM acris_docs").fetchone()[0]
    now = _now()
    with conn:
        conn.executemany(
            """INSERT INTO acris_docs (document_id, doc_type, document_date,
                   recorded, amount, percent_trans, first_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET
                   doc_type=excluded.doc_type,
                   document_date=excluded.document_date,
                   recorded=excluded.recorded,
                   amount=excluded.amount,
                   percent_trans=excluded.percent_trans""",
            [(d, r["doc_type"], r["document_date"], r["recorded_datetime"],
              r["document_amt"], r["percent_trans"], now)
             for d, r in docs.items()])
        conn.executemany(
            "INSERT OR IGNORE INTO acris_doc_parcels VALUES (?,?)",
            sorted(links))
        conn.execute(
            """UPDATE acris_docs SET n_parcels = (
                   SELECT COUNT(*) FROM acris_doc_parcels p
                    WHERE p.document_id = acris_docs.document_id)""")
    after = conn.execute("SELECT COUNT(*) FROM acris_docs").fetchone()[0]
    return {"docs_new": after - before, "docs_seen": len(docs),
            "links": len(links)}


def upsert_parcels(conn, rows, origin: str) -> int:
    """rows: dicts with bbl plus any of PARCEL_FIELDS."""
    now = _now()
    vals = []
    for r in rows:
        bbl = normalize_bbl(r.get("bbl"))
        if bbl:
            vals.append((bbl, origin, *(r.get(f) for f in PARCEL_FIELDS), now))
    cols = ("bbl", "origin", *PARCEL_FIELDS, "fetched_ts")
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO parcels ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)
    return len(vals)


def mark_absent(conn, bbls) -> int:
    now = _now()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO parcels (bbl, origin, fetched_ts) "
            "VALUES (?, 'absent', ?)", [(b, now) for b in bbls])
    return len(bbls)


# ─────────────────────────────────────────────────────────────────────────────
# reads
# ─────────────────────────────────────────────────────────────────────────────

def bbls_needing_pluto(conn, limit: int, now: datetime | None = None) -> list[str]:
    """Parcels on a kept document with no PLUTO facts yet, largest deal first,
    so a capped run spends its budget where the brief will look."""
    now = now or datetime.now(timezone.utc)
    retry = (now - timedelta(days=ABSENT_RETRY_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT l.bbl, MAX(d.amount) AS top
             FROM acris_doc_parcels l
             JOIN acris_docs d USING (document_id)
             LEFT JOIN parcels p ON p.bbl = l.bbl
            WHERE p.bbl IS NULL
               OR (p.origin = 'absent' AND p.fetched_ts < ?)
            GROUP BY l.bbl
            ORDER BY top DESC
            LIMIT ?""", (retry, limit)).fetchall()
    return [r["bbl"] for r in rows]


def parcels_for(conn, bbls) -> dict[str, dict]:
    """bbl -> parcel facts, for the BBLs that have real ones."""
    bbls = [b for b in {normalize_bbl(x) for x in bbls} if b]
    out: dict[str, dict] = {}
    for i in range(0, len(bbls), 500):
        chunk = bbls[i:i + 500]
        for r in conn.execute(
                f"SELECT * FROM parcels WHERE origin != 'absent' AND bbl IN "
                f"({','.join('?' * len(chunk))})", chunk):
            out[r["bbl"]] = dict(r)
    return out


def repeat_sales(conn, since_first_seen: str | None = None) -> list[dict]:
    """Consecutive deeds on the same parcel, with the price change.

    Only single-parcel, whole-parcel deeds are priced. A deed covering five
    lots records one amount for all five, so comparing it against a later
    sale of one of them reads as an 80% crash; a 50% interest transfer reads
    as halving. Both are excluded rather than adjusted, because an adjusted
    number looks exactly as authoritative as a real one.

    since_first_seen limits results to resales Spine first stored on or
    after that time. Not the recorded date: the city publishes in batches
    (nothing after Aug 31 had appeared by Oct 6), so a document recorded
    six weeks ago can be news tonight.
    """
    rows = conn.execute(
        f"""WITH sales AS (
              SELECT l.bbl, d.document_id, d.document_date, d.recorded,
                     d.amount, d.first_seen
                FROM acris_docs d
                JOIN acris_doc_parcels l USING (document_id)
               WHERE d.doc_type IN ({','.join('?' * len(SALE_TYPES))})
                 AND d.n_parcels = 1
                 AND d.amount > 1000
                 AND d.document_date IS NOT NULL
                 AND COALESCE(d.percent_trans, 100) >= 95
            ),
            seq AS (
              SELECT *, LAG(amount) OVER w AS prev_amount,
                        LAG(document_date) OVER w AS prev_date,
                        LAG(document_id) OVER w AS prev_doc
                FROM sales
              WINDOW w AS (PARTITION BY bbl ORDER BY document_date, recorded)
            )
            SELECT * FROM seq
             WHERE prev_amount IS NOT NULL
               AND julianday(document_date) - julianday(prev_date) >= ?
               AND (? IS NULL OR first_seen >= ?)
             ORDER BY recorded DESC""",
        (*SALE_TYPES, MIN_HOLD_DAYS, since_first_seen, since_first_seen)).fetchall()
    out = []
    for r in rows:
        days = (datetime.fromisoformat(r["document_date"][:10])
                - datetime.fromisoformat(r["prev_date"][:10])).days
        ratio = r["amount"] / r["prev_amount"]
        out.append({
            "bbl": r["bbl"], "document_id": r["document_id"],
            "prev_document_id": r["prev_doc"],
            "sold_on": r["prev_date"][:10], "sold_for": r["prev_amount"],
            "resold_on": r["document_date"][:10], "resold_for": r["amount"],
            "recorded": r["recorded"], "days_held": days,
            "pct_change": round(100 * (ratio - 1), 1),
            "pct_annualised": round(100 * (ratio ** (365 / max(days, 1)) - 1), 1),
        })
    return out


def counts(conn) -> dict:
    c = {"docs": conn.execute("SELECT COUNT(*) FROM acris_docs").fetchone()[0]}
    for origin, n in conn.execute(
            "SELECT origin, COUNT(*) FROM parcels GROUP BY origin"):
        c[f"parcels_{origin}"] = n
    linked = conn.execute(
        "SELECT COUNT(DISTINCT bbl) FROM acris_doc_parcels").fetchone()[0]
    known = conn.execute(
        """SELECT COUNT(DISTINCT l.bbl) FROM acris_doc_parcels l
             JOIN parcels p ON p.bbl = l.bbl AND p.origin != 'absent'"""
    ).fetchone()[0]
    c["parcels_linked"] = linked
    c["pluto_coverage_pct"] = round(100 * known / linked, 1) if linked else 0.0
    return c
