#!/usr/bin/env python3
"""
eval/build_benchmark.py — builds the frozen residential AVM benchmark that
eval/score_avm.py grades against. Run once by a human; never by a worker.

    python3 eval/build_benchmark.py            # fetch (resumable) + build
    python3 eval/build_benchmark.py --rebuild  # replace an existing benchmark

## Sources, and what each is trusted for

- **Price (y):** NYC DOF sales — the Rolling Sales workbooks (last 12
  months) and the Annualized Calendar Sales dataset (w2pb-icbu) for history.
  ACRIS document_amt is never the target.
- **Hygiene only:** ACRIS Master (deed type, percent_trans, recorded
  datetime), Legals (parcels per deed, encumbrance flags) and Parties
  (related-party heuristics). A sale with no matching ACRIS deed is dropped:
  its hygiene cannot be checked and its recording time is unknown.

## Scope (V1)

Manhattan, Bronx, Brooklyn, Queens — Staten Island deeds are recorded by
the Richmond County Clerk, not ACRIS. Residential condo units and 1–3 family
homes only (CLASSES); co-ops are share transfers and never appear as deeds.

## Labels

Every sale gets one label: CLEAN_MARKET, or the first exclusion it hits
(see `classify`). CLEAN_MARKET is a benchmark policy, not a legal finding
that a sale was arm's length.

## Split

Chronological, never random:

    test_end   = max ACRIS recorded date − DATA_LAG_DAYS (45)
    test_start = test_end − 89 days                     (90-day window)

Test rows are CLEAN_MARKET sales dated in the window. The comp pool
(benchmark_train.csv, with prices) is CLEAN_MARKET sales with
sale_date < test_start AND recorded_datetime < test_start. Every test
subject's as-of timestamp is its sale date ≥ test_start, so every comp in
the pool satisfies the point-in-time rule for every subject by construction;
`pit_ok` states the rule for anyone sourcing comps elsewhere.

## Output (var/avm_benchmark/)

    benchmark_test_X.csv   features known before subject_as_of_timestamp
    benchmark_test_y.csv   sale_id, bbl, sale_price            (mode 0400)
    benchmark_train.csv    PIT-safe comp pool with prices
    manifest.json          window, counts, label counts, SHA-256 per file

CSV, not Parquet: Spine is stdlib-only (CLAUDE.md §5) and pyarrow is not
installed. sale_id is an opaque hash, not the ACRIS document id, so the
answer cannot be looked up from X by id alone.

Stdlib only. Downloads stream into raw.db (SQLite) beside the output, so a
failed run resumes and peak RAM stays in collector territory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_avm                                              # noqa: E402

VERSION = 1
BOROUGHS = ("1", "2", "3", "4")
BORO_NAMES = {"1": "manhattan", "2": "bronx", "3": "brooklyn", "4": "queens"}
DATA_LAG_DAYS = 45
WINDOW_DAYS = 90
MIN_PRICE = 150_000
HISTORY_START = "2024-01-01"
MATCH_DAYS = 7          # DOF sale date vs ACRIS document date
DEED_TYPES = ("DEED",)  # not DEED, TS / LE / RC / COR, not DEEDO

# Building class at time of sale. 1–3 family: A* (not A8, co-op bungalow
# colonies), B*, C0. Condo units: R1 R2 R3 R4 R6. Not R9 (co-op in condo).
CLASSES = {"A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A9",
           "B1", "B2", "B3", "B9", "C0",
           "R1", "R2", "R3", "R4", "R6"}
CONDO = {"R1", "R2", "R3", "R4", "R6"}

SOCRATA = "https://data.cityofnewyork.us/resource"
DS_ANNUAL, DS_MASTER, DS_LEGALS, DS_PARTIES = (
    "w2pb-icbu", "bnx9-e6tj", "8h5j-fqxa", "636b-3b5g")
ROLLING_URL = ("https://www.nyc.gov/assets/finance/downloads/pdf/"
               "rolling_sales/rollingsales_{}.xlsx")
# nyc.gov's CDN answers 403 to non-browser user agents.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
SPINE_UA = "spine/0.1 (+https://github.com/codepioneerr/spine)"

X_COLUMNS = ("sale_id", "bbl", "borough", "block", "lot", "unit", "bldgclass",
             "zipcode", "neighborhood", "address", "residential_units",
             "gross_sqft", "land_sqft", "year_built", "subject_as_of_timestamp")
TRAIN_COLUMNS = (*X_COLUMNS[:-1], "sale_date", "recorded_datetime", "sale_price")

# ─────────────────────────────────────────────────────────────────────────────
# pure rules
# ─────────────────────────────────────────────────────────────────────────────

ENTITY = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|LP|LLP|LTD|TRUST|"
                    r"TRUSTEE|TRUSTEES|BANK|ASSOCIATION|ASSN|HOLDINGS?|"
                    r"PARTNERS|REALTY|PROPERTIES|DEVELOPMENT|GROUP|FUND|"
                    r"ESTATE|FOUNDATION|CHURCH|CITY OF|AUTHORITY|N A)\b")
NOISE = re.compile(r"\b(THE|AS|OF|A|AN|AND|ET AL|ETAL|JR|SR|II|III|IV|"
                   r"INDIVIDUALLY|EXECUTOR|EXECUTRIX|ADMINISTRATOR|REFEREE)\b")


def norm_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    return " ".join(NOISE.sub(" ", s).split())


def is_entity(name: str) -> bool:
    return bool(ENTITY.search(norm_name(name)))


def surname(name: str) -> str | None:
    """ACRIS writes people as 'LAST, FIRST M'."""
    raw = str(name or "").upper()
    if is_entity(raw) or "," not in raw:
        return None
    last = norm_name(raw.split(",", 1)[0])
    return last if len(last) >= 2 else None


def _core(name: str) -> str:
    """An entity's distinguishing words: '123 MAIN ST HOLDINGS LLC' -> '123 MAIN ST'."""
    return " ".join(ENTITY.sub(" ", norm_name(name)).split())


def related_party(grantors: list[str], grantees: list[str]) -> str | None:
    """Why the parties look related, or None. Heuristics, deliberately
    conservative: identical names, a shared family surname, entities sharing
    a core name, or a person whose surname is in the selling entity's name."""
    gs = [norm_name(n) for n in grantors if norm_name(n)]
    ge = [norm_name(n) for n in grantees if norm_name(n)]
    if set(gs) & set(ge):
        return "same_name"
    s_sur = {surname(n) for n in grantors} - {None}
    e_sur = {surname(n) for n in grantees} - {None}
    if s_sur & e_sur:
        return "shared_surname"
    s_core = {_core(n) for n in grantors if is_entity(n)} - {""}
    e_core = {_core(n) for n in grantees if is_entity(n)} - {""}
    if s_core & e_core:
        return "affiliate_entity"
    for c in s_core:
        words = set(c.split())
        if any(len(x) >= 3 and x in words for x in e_sur):
            return "entity_to_officer"
    return None


def bbl_of(boro, block, lot) -> str | None:
    try:
        b, bl, lt = str(boro).strip(), int(float(block)), int(float(lot))
    except (TypeError, ValueError):
        return None
    if b not in BOROUGHS or not (0 < bl <= 99999) or not (0 < lt <= 9999):
        return None
    return f"{b}{bl:05d}{lt:04d}"


def is_unit_lot(bbl: str) -> bool:
    return 1001 <= int(bbl[6:]) <= 6999


def classify(sale: dict, deed: dict | None, legals: list[dict],
             grantors: list[str], grantees: list[str]) -> str:
    """CLEAN_MARKET or the first exclusion reason. Pure.

    sale:   normalised DOF row (bbl, bldgclass, price, easement, package)
    deed:   matched ACRIS master row or None
    legals: every ACRIS legals row of that deed"""
    if sale["bldgclass"] not in CLASSES:
        return "out_of_scope_class"
    if not sale["bbl"]:
        return "invalid_bbl"
    if sale["bldgclass"] in CONDO and not is_unit_lot(sale["bbl"]):
        return "condo_without_unit_lot"
    if (sale["price"] or 0) < MIN_PRICE:
        return "nominal_price"
    if sale.get("easement"):
        return "encumbrance"
    if sale.get("package"):
        return "multi_parcel"
    if deed is None:
        return "no_acris_deed"
    pct = deed.get("percent_trans")
    if pct not in (None, "") and 0 < float(pct) < 100:
        return "partial_interest"
    if len({lg["bbl"] for lg in legals}) != 1:
        return "multi_parcel"
    for lg in legals:
        if (lg.get("easement") == "Y" or lg.get("partial_lot") == "P"
                or lg.get("air_rights") == "Y" or lg.get("subterranean_rights") == "Y"):
            return "encumbrance"
    if related_party(grantors, grantees):
        return "related_party"
    return "CLEAN_MARKET"


def pit_ok(comp: dict, subject_as_of: str) -> bool:
    """The leakage rule: a comp may inform a subject only if it both sold
    and was recorded strictly before the subject's as-of timestamp. Dates
    and datetimes are compared as full timestamps: a bare '2026-04-19' sorts
    before '2026-04-19T00:00:00' as a string, which would admit a same-day
    comp."""
    asof = _ts(subject_as_of)
    return _ts(comp["sale_date"]) < asof and _ts(comp["recorded_datetime"]) < asof


def _ts(v: str) -> str:
    s = str(v).replace(" ", "T")
    return s if len(s) > 10 else f"{s[:10]}T00:00:00"


def window(max_ingested: str) -> tuple[str, str]:
    end = date.fromisoformat(max_ingested[:10]) - timedelta(days=DATA_LAG_DAYS)
    return (end - timedelta(days=WINDOW_DAYS - 1)).isoformat(), end.isoformat()


def sale_id(document_id: str, bbl: str) -> str:
    return hashlib.sha256(f"avm-v{VERSION}:{document_id}:{bbl}".encode()).hexdigest()[:16]


def flag_packages(sales: list[dict]) -> None:
    """DOF lists a multi-lot sale once per lot at the full price. Mark every
    row sharing (borough, block, sale_date, price) with another lot."""
    seen: dict[tuple, set] = {}
    for s in sales:
        seen.setdefault((s["bbl"][:6] if s["bbl"] else None, s["sale_date"],
                         s["price"]), set()).add(s["bbl"])
    for s in sales:
        k = (s["bbl"][:6] if s["bbl"] else None, s["sale_date"], s["price"])
        s["package"] = bool(s["bbl"]) and len(seen[k]) > 1 and (s["price"] or 0) > 0


# ─────────────────────────────────────────────────────────────────────────────
# fetching — resumable, into raw.db
# ─────────────────────────────────────────────────────────────────────────────

RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    src TEXT, bbl TEXT, borough TEXT, block TEXT, lot TEXT, unit TEXT,
    bldgclass TEXT, zipcode TEXT, neighborhood TEXT, address TEXT,
    residential_units TEXT, gross_sqft TEXT, land_sqft TEXT, year_built TEXT,
    easement TEXT, price REAL, sale_date TEXT);
CREATE TABLE IF NOT EXISTS master (
    document_id TEXT PRIMARY KEY, doc_type TEXT, document_date TEXT,
    recorded_datetime TEXT, percent_trans TEXT, document_amt TEXT);
CREATE TABLE IF NOT EXISTS legals (
    document_id TEXT, bbl TEXT, easement TEXT, partial_lot TEXT,
    air_rights TEXT, subterranean_rights TEXT, property_type TEXT, unit TEXT);
CREATE INDEX IF NOT EXISTS ix_legals_doc ON legals(document_id);
CREATE INDEX IF NOT EXISTS ix_legals_bbl ON legals(bbl);
CREATE TABLE IF NOT EXISTS parties (document_id TEXT, party_type TEXT, name TEXT);
CREATE INDEX IF NOT EXISTS ix_parties_doc ON parties(document_id);
CREATE TABLE IF NOT EXISTS done (step TEXT PRIMARY KEY, ts TEXT);
"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fetch(url: str, ua: str = SPINE_UA, tries: int = 5) -> bytes:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if i == tries - 1:
                raise
            log(f"retry {i + 1} after {exc}")
            time.sleep(5 * (i + 1))
    raise AssertionError


def soql(dataset: str, **params) -> list[dict]:
    q = urllib.parse.urlencode({f"${k}": v for k, v in params.items()})
    return json.loads(fetch(f"{SOCRATA}/{dataset}.json?{q}"))


def soql_pages(dataset: str, where: str, select: str, page=50_000):
    off = 0
    while True:
        rows = soql(dataset, select=select, where=where, order=":id",
                    limit=page, offset=off)
        yield rows
        if len(rows) < page:
            return
        off += page


def _step_done(db, step):
    return db.execute("SELECT 1 FROM done WHERE step=?", (step,)).fetchone()


def _mark(db, step):
    db.execute("INSERT OR REPLACE INTO done VALUES (?, ?)", (step, datetime.now().isoformat()))
    db.commit()


def _excel_date(v) -> str | None:
    if v in (None, ""):
        return None
    try:
        return (date(1899, 12, 30) + timedelta(days=int(float(v)))).isoformat()
    except ValueError:
        return str(v)[:10]


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_rows(data: bytes):
    """Rows of the first sheet as lists, streamed. Stdlib only."""
    import io
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(data))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
            if el.tag == ns + "si":
                strings.append("".join(t.text or "" for t in el.iter(ns + "t")))
                el.clear()
    for _, el in ET.iterparse(z.open("xl/worksheets/sheet1.xml")):
        if el.tag != ns + "row":
            continue
        row: dict[int, str] = {}
        for c in el.findall(ns + "c"):
            t, v = c.get("t"), c.find(ns + "v")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(ns + "t"))
            elif v is None:
                continue
            else:
                val = strings[int(v.text)] if t == "s" else v.text
            row[_col(c.get("r"))] = val
        el.clear()
        yield [row.get(i) for i in range(max(row) + 1)] if row else []


def _sale_row(src, boro, block, lot, unit, cls, zipc, nbhd, addr, res, gross,
              land, yr, ease, price, sdate):
    def s(v):
        return str(v).strip() if v not in (None, "") else None
    try:
        p = float(str(price).replace(",", "").replace("$", "")) if price not in (None, "") else None
    except ValueError:
        p = None
    return (src, bbl_of(boro, block, lot), s(boro), s(block), s(lot), s(unit),
            (s(cls) or "").upper() or None, s(zipc), s(nbhd), s(addr), s(res),
            s(gross), s(land), s(yr), s(ease), p, sdate)


def fetch_rolling(db):
    for boro, name in BORO_NAMES.items():
        step = f"rolling:{name}"
        if _step_done(db, step):
            continue
        log(f"rolling sales: {name}")
        header, n = None, 0
        for r in xlsx_rows(fetch(ROLLING_URL.format(name), ua=BROWSER_UA)):
            if header is None:
                if r and str(r[0] or "").strip().upper() == "BOROUGH":
                    header = {str(h).strip().upper(): i for i, h in enumerate(r) if h}
                continue
            g = lambda k: r[header[k]] if header.get(k) is not None and header[k] < len(r) else None  # noqa: E731
            if g("BOROUGH") is None:
                continue
            db.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _sale_row(
                "rolling", g("BOROUGH"), g("BLOCK"), g("LOT"), g("APARTMENT NUMBER"),
                g("BUILDING CLASS AT TIME OF SALE"), g("ZIP CODE"), g("NEIGHBORHOOD"),
                g("ADDRESS"), g("RESIDENTIAL UNITS"), g("GROSS SQUARE FEET"),
                g("LAND SQUARE FEET"), g("YEAR BUILT"), g("EASEMENT"), g("SALE PRICE"),
                _excel_date(g("SALE DATE"))))
            n += 1
        log(f"  {n} rows")
        _mark(db, step)


def fetch_annualized(db):
    if _step_done(db, "annualized"):
        return
    log("annualized sales")
    db.execute("DELETE FROM sales WHERE src='annual'")
    boros = ",".join(f"'{b}'" for b in BOROUGHS)
    where = f"sale_date >= '{HISTORY_START}' AND borough in ({boros})"
    sel = ("borough,block,lot,apartment_number,building_class_at_time_of,zip_code,"
           "neighborhood,address,residential_units,gross_square_feet,"
           "land_square_feet,year_built,ease_ment,sale_price,sale_date")
    for rows in soql_pages(DS_ANNUAL, where, sel):
        db.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            _sale_row("annual", r.get("borough"), r.get("block"), r.get("lot"),
                      r.get("apartment_number"), r.get("building_class_at_time_of"),
                      r.get("zip_code"), r.get("neighborhood"), r.get("address"),
                      r.get("residential_units"), r.get("gross_square_feet"),
                      r.get("land_square_feet"), r.get("year_built"),
                      r.get("ease_ment"), r.get("sale_price"),
                      (r.get("sale_date") or "")[:10] or None) for r in rows])
        log(f"  +{len(rows)}")
    _mark(db, "annualized")


def fetch_master(db):
    if _step_done(db, "master"):
        return
    log("ACRIS master (deeds)")
    db.execute("DELETE FROM master")
    types = ",".join(f"'{t}'" for t in DEED_TYPES)
    boros = ",".join(f"'{b}'" for b in BOROUGHS)
    where = (f"doc_type in ({types}) AND document_date >= '{HISTORY_START}' "
             f"AND recorded_borough in ({boros})")
    sel = "document_id,doc_type,document_date,recorded_datetime,percent_trans,document_amt"
    for rows in soql_pages(DS_MASTER, where, sel):
        db.executemany("INSERT OR REPLACE INTO master VALUES (?,?,?,?,?,?)", [
            (r["document_id"], r.get("doc_type"), (r.get("document_date") or "")[:10],
             (r.get("recorded_datetime") or "")[:19], r.get("percent_trans"),
             r.get("document_amt")) for r in rows])
        log(f"  +{len(rows)}")
    _mark(db, "master")


def _by_ids(dataset, select, ids):
    inlist = ",".join(f"'{i}'" for i in ids)
    return soql(dataset, select=select, where=f"document_id in ({inlist})", limit=50_000)


def _fetch_children(db, step, dataset, select, table, ids, to_row, chunk=150):
    if _step_done(db, step):
        return
    have = {r[0] for r in db.execute(f"SELECT DISTINCT document_id FROM {table}")}
    todo = sorted(set(ids) - have)
    log(f"{step}: {len(todo)} documents to fetch")
    chunks = [todo[i:i + chunk] for i in range(0, len(todo), chunk)]
    with ThreadPoolExecutor(4) as pool:
        for k, rows in enumerate(pool.map(lambda c: _by_ids(dataset, select, c), chunks)):
            vals = [to_row(r) for r in rows]
            if vals:
                db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(vals[0]))})", vals)
            if k % 50 == 0:
                db.commit()
                log(f"  {k}/{len(chunks)}")
    db.commit()
    _mark(db, step)


def fetch_legals(db):
    ids = [r[0] for r in db.execute("SELECT document_id FROM master")]
    _fetch_children(
        db, "legals", DS_LEGALS,
        "document_id,borough,block,lot,easement,partial_lot,air_rights,"
        "subterranean_rights,property_type,unit", "legals", ids,
        lambda r: (r["document_id"], bbl_of(r.get("borough"), r.get("block"), r.get("lot")),
                   r.get("easement"), r.get("partial_lot"), r.get("air_rights"),
                   r.get("subterranean_rights"), r.get("property_type"), r.get("unit")))


def fetch_parties(db, doc_ids):
    _fetch_children(db, "parties", DS_PARTIES, "document_id,party_type,name",
                    "parties", doc_ids,
                    lambda r: (r["document_id"], r.get("party_type"), r.get("name")))


# ─────────────────────────────────────────────────────────────────────────────
# assembly
# ─────────────────────────────────────────────────────────────────────────────

def load_sales(db) -> list[dict]:
    """Every in-scope-borough sale, rolling preferred where the sources
    overlap (per borough, annualized rows from the rolling start onward are
    dropped)."""
    db.row_factory = sqlite3.Row
    roll_start = dict(db.execute(
        "SELECT borough, MIN(sale_date) FROM sales WHERE src='rolling' GROUP BY borough").fetchall())
    out = []
    for r in db.execute("SELECT * FROM sales WHERE sale_date IS NOT NULL"):
        s = dict(r)
        if s["src"] == "annual" and roll_start.get(s["borough"]) and \
                s["sale_date"] >= roll_start[s["borough"]]:
            continue
        out.append(s)
    return out


def match_deeds(db, sales: list[dict]) -> dict[int, dict]:
    """sale index -> ACRIS master row. Candidates are deeds whose legals name
    the sale's BBL within MATCH_DAYS; closest date wins, then equal amount."""
    by_bbl: dict[str, list[dict]] = {}
    for r in db.execute("""SELECT l.bbl, m.* FROM legals l JOIN master m USING (document_id)"""):
        by_bbl.setdefault(r["bbl"], []).append(dict(r))
    out = {}
    for i, s in enumerate(sales):
        if s["bldgclass"] not in CLASSES or not s["bbl"] or (s["price"] or 0) < MIN_PRICE:
            continue
        sd = date.fromisoformat(s["sale_date"])
        best = None
        for d in by_bbl.get(s["bbl"], ()):
            try:
                gap = abs((date.fromisoformat(d["document_date"]) - sd).days)
            except ValueError:
                continue
            if gap > MATCH_DAYS:
                continue
            try:
                amt_off = abs(float(d["document_amt"] or 0) - s["price"]) > 1
            except ValueError:
                amt_off = True
            key = (gap, amt_off)
            if best is None or key < best[0]:
                best = (key, d)
        if best:
            out[i] = best[1]
    return out


def _write_csv(path, cols, rows, mode=0o444):
    if os.path.exists(path):
        os.chmod(path, 0o600)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.chmod(path, mode)


def build_frozen_benchmark(bench_dir: str = score_avm.DEFAULT_BENCH,
                           rebuild: bool = False, offline: bool = False) -> dict:
    """Fetch (resumably) and write the frozen benchmark. Returns the manifest."""
    man_path = os.path.join(bench_dir, score_avm.MANIFEST)
    if os.path.exists(man_path) and not rebuild:
        raise SystemExit(f"benchmark exists at {bench_dir}; it is frozen. "
                         "--rebuild replaces it (a human decision).")
    os.makedirs(bench_dir, exist_ok=True)
    db = sqlite3.connect(os.path.join(bench_dir, "raw.db"))
    db.executescript(RAW_SCHEMA)
    db.row_factory = sqlite3.Row
    if not offline:
        fetch_rolling(db)
        fetch_annualized(db)
        fetch_master(db)
        fetch_legals(db)

    sales = load_sales(db)
    flag_packages(sales)
    matched = match_deeds(db, sales)
    if not offline:
        fetch_parties(db, sorted({d["document_id"] for d in matched.values()}))

    legals: dict[str, list[dict]] = {}
    for r in db.execute("SELECT * FROM legals WHERE document_id IN "
                        "(SELECT document_id FROM master)"):
        legals.setdefault(r["document_id"], []).append(dict(r))
    parties: dict[str, tuple[list, list]] = {}
    for r in db.execute("SELECT * FROM parties"):
        parties.setdefault(r["document_id"], ([], []))[0 if r["party_type"] == "1" else 1].append(r["name"])

    max_ingested = db.execute("SELECT MAX(recorded_datetime) FROM master").fetchone()[0]
    test_start, test_end = window(max_ingested)

    labels: dict[str, int] = {}
    test, train, used_docs = [], [], set()
    for i, s in enumerate(sales):
        deed = matched.get(i)
        doc = deed["document_id"] if deed else None
        g = parties.get(doc, ([], []))
        lab = classify(s, deed, legals.get(doc, []), g[0], g[1])
        if lab == "CLEAN_MARKET" and doc in used_docs:
            lab = "duplicate_sale"
        labels[lab] = labels.get(lab, 0) + 1
        if lab != "CLEAN_MARKET":
            continue
        used_docs.add(doc)
        row = {**s, "sale_id": sale_id(doc, s["bbl"]), "sale_price": s["price"],
               "recorded_datetime": deed["recorded_datetime"],
               "subject_as_of_timestamp": f"{s['sale_date']}T00:00:00"}
        if test_start <= s["sale_date"] <= test_end:
            test.append(row)
        elif pit_ok(row, f"{test_start}T00:00:00"):
            train.append(row)

    test.sort(key=lambda r: r["sale_id"])
    train.sort(key=lambda r: (r["sale_date"], r["sale_id"]))
    files = {
        score_avm.X_FILE: (X_COLUMNS, test, 0o444),
        score_avm.Y_FILE: (("sale_id", "bbl", "sale_price"), test, 0o400),
        "benchmark_train.csv": (TRAIN_COLUMNS, train, 0o444),
    }
    for name, (cols, rows, mode) in files.items():
        _write_csv(os.path.join(bench_dir, name), cols, rows, mode)

    manifest = {
        "version": VERSION, "built_at": datetime.now().isoformat(timespec="seconds"),
        "max_ingested_date": max_ingested[:10], "data_lag_days": DATA_LAG_DAYS,
        "test_start": test_start, "test_end": test_end,
        "n_test": len(test), "n_train": len(train),
        "test_by_borough": {b: sum(r["borough"] == b for r in test) for b in BOROUGHS},
        "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])),
        "sha256": {n: score_avm.sha256(os.path.join(bench_dir, n)) for n in files},
    }
    if os.path.exists(man_path):
        os.chmod(man_path, 0o600)
    with open(man_path, "w") as f:
        json.dump(manifest, f, indent=2)
    os.chmod(man_path, 0o444)
    db.close()
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the frozen AVM benchmark.")
    ap.add_argument("--bench-dir", default=score_avm.DEFAULT_BENCH)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="rebuild from raw.db without fetching")
    a = ap.parse_args(argv)
    m = build_frozen_benchmark(a.bench_dir, a.rebuild, a.offline)
    print(json.dumps({k: v for k, v in m.items() if k != "sha256"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
