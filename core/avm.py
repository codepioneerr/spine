"""
core.avm — comps-based valuation, v0. Price per unit and per square foot by
zip and building class, and how far a sale sits from its comps.

## What this is, honestly

Not a model. A median of the neighbours, with the subject left out. On six
weeks of ACRIS that is the most a number can claim without looking more
authoritative than it is; a regression fitted to a few hundred sales would
print three significant figures of noise.

## What gets priced

A sale is priceable when the amount means the building changed hands at that
price:

- DEED / DEEDO only (a mortgage amount is not a price),
- one parcel (a five-lot deed records one amount for all five),
- whole interest (percent_trans >= 95, or unrecorded),
- amount >= MIN_PRICE — about half of ACRIS masters are $0 or nominal
  transfers between related parties, and they would drag every median down,
- not a condo unit lot: its parcel facts are the whole building's, so
  $/unit and $/sqft would be off by the size of the building,
- with the building facts to divide by (unitsres or bldgarea > 0).

## Comps

Groups, most specific first; the first with MIN_COMPS other sales wins:

    zip+class   same zipcode, same building-class family (first letter:
                C walk-up, D elevator, R condo, ...)
    zip         same zipcode
    boro+class  same borough, same class family

$/sqft is preferred when the subject has a floor area, $/unit otherwise.
Leave-one-out always: a sale is never its own comp, or the backtest would
grade itself.

## Not in the brief yet, on purpose

First real backtest, 2026-10-06, darkweb-jobs' window (Jul 20 - Aug 31,
MN/BK 6+ units): 81 priced sales, median absolute error 50.9%, 8% within
20%. MIN_COMPS 3 or 8 and $/unit instead of $/sqft all land between 47% and
58% -- the limit is six weeks of data, not the method. A "1.4x comps" label
on a deal would look exactly as authoritative at 51% error as at 10%, so
`surfaceable()` holds every estimate back until its level backtests under
MAX_SURFACE_MDAPE. Spine's own history (collectors/proptech, $1M+, all
boroughs) is what should move that number; rerun the backtest as it grows.

    python3 -m core.avm --backtest              # spine.db history
    python3 -m core.avm --backtest --source dwj # darkweb-jobs' 6-week window
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict

SALE_TYPES = ("DEED", "DEEDO")
MIN_PRICE = 10_000
MIN_COMPS = 5
MAX_SURFACE_MDAPE = 25.0
LEVELS = ("zip+class", "zip", "boro+class")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _family(bldgclass) -> str | None:
    s = str(bldgclass or "").strip().upper()
    return s[:1] if s[:1].isalpha() else None


def _is_unit_lot(bbl: str) -> bool:
    return len(bbl) == 10 and 1001 <= int(bbl[6:]) <= 6999


def priceable(s: dict) -> dict | None:
    """The sale with ppu/ppsf and its group keys, or None if it cannot be
    priced. Pure."""
    bbl = str(s.get("bbl") or "")
    amount = _num(s.get("amount"))
    if (s.get("doc_type") not in SALE_TYPES or not amount
            or amount < MIN_PRICE or len(bbl) != 10 or not bbl.isdigit()
            or _is_unit_lot(bbl)):
        return None
    if (s.get("n_parcels") or 1) != 1:
        return None
    pct = s.get("percent_trans")
    if pct is not None and _num(pct) is not None and float(pct) < 95:
        return None
    units, area = _num(s.get("unitsres")), _num(s.get("bldgarea"))
    if not units and not area:
        return None
    zipc = str(s.get("zipcode") or "").strip()[:5] or None
    fam = _family(s.get("bldgclass"))
    return {
        **s, "amount": amount, "bbl": bbl, "unitsres": units, "bldgarea": area,
        "ppu": amount / units if units else None,
        "ppsf": amount / area if area else None,
        "keys": {
            "zip+class": (zipc, fam) if zipc and fam else None,
            "zip": zipc,
            "boro+class": (bbl[0], fam) if fam else None,
        },
    }


class Comps:
    """An index of priced sales by group, for leave-one-out medians."""

    def __init__(self, sales):
        self.sales = [p for p in map(priceable, sales) if p]
        self.groups: dict[tuple, list[dict]] = defaultdict(list)
        for p in self.sales:
            for level in LEVELS:
                if p["keys"][level] is not None:
                    self.groups[(level, p["keys"][level])].append(p)

    def estimate(self, subject: dict) -> dict | None:
        """{value, ratio, metric, level, n, median} for a sale, or None when
        no group has MIN_COMPS other sales. `subject` may be raw (it is run
        through priceable) or already priced."""
        p = subject if "keys" in subject else priceable(subject)
        if not p:
            return None
        doc = p.get("document_id")
        metrics = [("ppsf", "bldgarea"), ("ppu", "unitsres")]
        for level in LEVELS:
            key = p["keys"][level]
            if key is None:
                continue
            group = self.groups.get((level, key), ())
            for metric, size in metrics:
                if not p[size]:
                    continue
                vals = [c[metric] for c in group
                        if c[metric] and c.get("document_id") != doc]
                if len(vals) < MIN_COMPS:
                    continue
                med = statistics.median(vals)
                value = med * p[size]
                return {"value": round(value), "ratio": round(p["amount"] / value, 3),
                        "metric": metric, "level": level, "n": len(vals),
                        "median": round(med, 2)}
        return None


def backtest(sales, only_docs=None) -> dict:
    """Leave-one-out accuracy. only_docs restricts which sales are graded
    (e.g. the resales of repeat-sale pairs); every priced sale is still a
    potential comp."""
    comps = Comps(sales)
    errs, by_level = [], defaultdict(list)
    graded = [p for p in comps.sales
              if only_docs is None or p.get("document_id") in only_docs]
    for p in graded:
        e = comps.estimate(p)
        if e:
            err = abs(e["value"] / p["amount"] - 1)
            errs.append(err)
            by_level[e["level"]].append(err)

    def summary(xs):
        return {"n": len(xs),
                "mdape_pct": round(100 * statistics.median(xs), 1) if xs else None,
                "within_20_pct": round(100 * sum(x <= .2 for x in xs) / len(xs), 1) if xs else None}

    return {"priced": len(comps.sales), "graded": len(graded),
            "coverage_pct": round(100 * len(errs) / len(graded), 1) if graded else 0.0,
            **summary(errs),
            "by_level": {k: summary(v) for k, v in sorted(by_level.items())}}


def surfaceable(estimate: dict | None, report: dict) -> bool:
    """Whether an estimate is good enough to show anyone: its comp level must
    have backtested (report = backtest(...)) under MAX_SURFACE_MDAPE."""
    if not estimate:
        return False
    lvl = report.get("by_level", {}).get(estimate["level"]) or {}
    return lvl.get("mdape_pct") is not None and lvl["mdape_pct"] < MAX_SURFACE_MDAPE


# ─────────────────────────────────────────────────────────────────────────────
# loaders — rows in, sale dicts out
# ─────────────────────────────────────────────────────────────────────────────

def sales_from_spine(conn) -> list[dict]:
    """Spine's own history (core.proptech tables), with parcel facts."""
    from core import proptech
    rows = conn.execute(
        f"""SELECT d.document_id, d.doc_type, d.document_date AS date,
                   d.amount, d.percent_trans, d.n_parcels, l.bbl
              FROM acris_docs d JOIN acris_doc_parcels l USING (document_id)
             WHERE d.doc_type IN ({','.join('?' * len(SALE_TYPES))})
               AND d.n_parcels = 1 AND d.amount >= ?""",
        (*SALE_TYPES, MIN_PRICE)).fetchall()
    facts = proptech.parcels_for(conn, [r["bbl"] for r in rows])
    out = []
    for r in rows:
        f = facts.get(r["bbl"], {})
        out.append({**dict(r), **{k: f.get(k) for k in
                                  ("zipcode", "bldgclass", "unitsres", "bldgarea")}})
    return out


def sales_from_dwj(conn) -> list[dict]:
    """darkweb-jobs' proptech.db: its ACRIS window joined to its PLUTO slice.
    Only Manhattan/Brooklyn 6+ unit buildings have facts there."""
    rows = conn.execute(
        f"""SELECT m.document_id, m.doc_type, m.document_date AS date,
                   m.document_amt AS amount, m.percent_trans,
                   COUNT(*) AS n_parcels, MAX(l.bbl) AS bbl,
                   p.zipcode, p.bldgclass, p.unitsres, p.bldgarea
              FROM acris_master m
              JOIN acris_legals l ON l.document_id = m.document_id
              LEFT JOIN pluto p ON p.bbl = l.bbl
             WHERE m.doc_type IN ({','.join('?' * len(SALE_TYPES))})
               AND m.document_amt >= ?
             GROUP BY m.document_id""",
        (*SALE_TYPES, MIN_PRICE)).fetchall()
    return [dict(r) for r in rows]


def main(argv) -> int:
    if "--backtest" not in argv:
        print(__doc__)
        return 2
    if "--source" in argv and argv[argv.index("--source") + 1] == "dwj":
        from core import bridge
        conn = bridge.connect("proptech")
        sales, resales = sales_from_dwj(conn), None
    else:
        from core import proptech
        conn = proptech.connect()
        sales = sales_from_spine(conn)
        resales = {r["document_id"] for r in proptech.repeat_sales(conn)}
    conn.close()
    r = backtest(sales)
    print(f"all sales:   {r}")
    ok = [k for k, v in r["by_level"].items() if surfaceable({"level": k}, r)]
    print(f"surfaceable levels (< {MAX_SURFACE_MDAPE}% MdAPE): {ok or 'none'}")
    if resales:
        print(f"resales:     {backtest(sales, resales)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
