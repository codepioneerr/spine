"""
core.property_facts — beginner-readable facts about recorded property
documents. Deterministic; reads spine.db (acris_docs, parcels, items).

The single most important rule: a recorded document is a record that
something was FILED, with a recording date that can lag the signing date by
weeks. It does not mean a property is for sale, and a mortgage amount is a
loan amount, not a price.

Document-type meanings and party roles come from NYC's "ACRIS - Document
Control Codes" dataset (data.cityofnewyork.us 7isb-wh4c), retrieved
2026-10-06.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# doc_type -> (plain label, class, party1 role, party2 role, what happened)
DOC_TYPES = {
    "DEED": ("Deed (ownership transfer)", "Deeds and other conveyances",
             "grantor / seller", "grantee / buyer",
             "Ownership of the property (or a share of it) was transferred from "
             "the grantor to the grantee, and the deed was recorded with the City Register."),
    "DEEDO": ("Deed, other (non-standard transfer)", "Deeds and other conveyances",
              "grantor", "grantee",
              "A transfer recorded under 'deed, other' - often not an ordinary "
              "arm's-length sale (e.g. corrections, entity transfers). Treat the amount with caution."),
    "MTGE": ("Mortgage (loan secured by the property)", "Mortgages & instruments",
             "mortgagor / borrower", "mortgagee / lender",
             "A borrower pledged the property as security for a loan. This is "
             "financing, not a sale: ownership did not change because of this document."),
    "AGMT": ("Agreement (often mortgage-related)", "Mortgages & instruments",
             "party 1", "party 2", "An agreement filed against the property, frequently "
                                   "consolidating or modifying existing mortgages."),
    "SAT": ("Satisfaction of mortgage (loan paid off)", "Mortgages & instruments",
            "borrower", "lender", "The lender recorded that a mortgage was paid off."),
    "ASST": ("Assignment of mortgage", "Mortgages & instruments",
             "old lender", "new lender", "A mortgage was transferred from one lender to another."),
}

AMOUNT_MEANING = {
    "DEED": "the consideration stated on the deed (usually the price paid). "
            "Nominal amounts ($0, $10) usually mean a non-market transfer.",
    "DEEDO": "the stated consideration; often nominal or not a market price.",
    "MTGE": "the loan principal secured by this mortgage - NOT the property's price or value.",
    "AGMT": "the amount of the agreement, often a consolidated loan balance - not a price.",
}

# The acris collector's importance is a PRIORITY score: amount bands plus a
# unit-count nudge. Mirrors collectors/acris.importance; kept in sync by test.
SCORE_EXPLAIN = (
    "The number in brackets is a priority score (0-100) used only to sort the brief: "
    "45 under $2M, 52 for $2-5M, 60 for $5-10M, 68 for $10-20M, 75 for $20-50M, "
    "85 for $50M+, plus 2 for 6+ residential units or 5 for 20+. It measures dollar "
    "size, not opportunity, risk or probability of profit.")

GLOSSARY = {
    "deed": "A deed is the document that transfers ownership of real property from a "
            "grantor (seller) to a grantee (buyer). Recording it with the City Register makes "
            "the transfer public. It does not prove the title is free of problems.",
    "mortgage": "A mortgage (ACRIS code MTGE) is a document in which a borrower pledges the "
                "property as security for a loan from a lender. Its amount is the loan, not a price. "
                "Buildings refinance often, so a mortgage alone says nothing about a sale.",
    "mtge": "MTGE is ACRIS's code for a mortgage: a loan secured by the property. See /glossary mortgage.",
    "grantor": "The party giving up ownership in a deed (usually the seller).",
    "grantee": "The party receiving ownership in a deed (usually the buyer).",
    "borrower": "In a mortgage, the mortgagor: the owner who borrows and pledges the property.",
    "lender": "In a mortgage, the mortgagee: the bank or lender that receives the security interest.",
    "bbl": "BBL = Borough-Block-Lot, NYC's 10-digit parcel ID: 1 digit borough (1 Manhattan, "
           "2 Bronx, 3 Brooklyn, 4 Queens, 5 Staten Island), 5-digit block, 4-digit lot. "
           "Condo units get their own lots (1001+) that roll up to a billing lot (75xx).",
    "acris": "ACRIS (Automated City Register Information System) is the NYC Department of "
             "Finance system for recorded property documents in Manhattan, the Bronx, Brooklyn "
             "and Queens (1966-present). Staten Island records are kept by the Richmond County Clerk. "
             "We read its public open-data feed, which the city publishes in batches.",
    "pluto": "PLUTO (Primary Land Use Tax Lot Output) is NYC Planning's dataset of facts about "
             "each tax lot: address, zoning, building class, units, year built, floor area, "
             "assessed value. It describes the lot, not who just bought it or what it sold for.",
    "assessed value": "The Department of Finance's taxable assessed value. In NYC it is usually "
                      "far below market value by design (assessment ratios and caps), so it is not "
                      "a price estimate.",
    "avm": "An automated valuation model (AVM) estimates a value from comparable sales. Ours is "
           "experimental and is not shown until its backtested median error is under 25%.",
    "valuation": "A valuation estimate is a model's guess at market value, different from a "
                 "recorded price (what a deed states) and from assessed value (a tax figure).",
    "recording date": "When the City Register recorded the document. The document date (signing) "
                      "is usually earlier; the date we downloaded it is later still.",
    "condo": "Condo units each have their own BBL. Building facts in PLUTO are filed under the "
             "building's billing lot, so we map unit -> building before describing it.",
    "deedo": "DEEDO is ACRIS's 'deed, other' code: transfers that are not standard deeds. "
             "Amounts are often nominal.",
}
GLOSSARY_ALIASES = {"deeds": "deed", "mortgages": "mortgage", "assessed": "assessed value",
                    "assessment": "assessed value", "valuation estimate": "valuation",
                    "grantors": "grantor", "grantees": "grantee"}

SOURCE_URLS = {
    "acris": "https://www.nyc.gov/site/finance/property/acris.page",
    "doc_codes": "https://data.cityofnewyork.us/d/7isb-wh4c",
    "pluto": "https://data.cityofnewyork.us/d/64uk-42ks",
}

BOROS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}


def glossary(term: str) -> str | None:
    t = term.strip().lower()
    t = GLOSSARY_ALIASES.get(t, t)
    return GLOSSARY.get(t)


def zola_url(bbl: str) -> str | None:
    if not bbl or len(bbl) != 10 or not bbl.isdigit():
        return None
    return f"https://zola.planning.nyc.gov/l/lot/{bbl[0]}/{int(bbl[1:6])}/{int(bbl[6:])}"


def _day(s):
    return str(s)[:10] if s else None


def card_facts(item: dict, doc: dict | None = None, now: datetime | None = None,
               feed_newest: str | None = None) -> dict:
    """Pure. Structured facts for one acris item (row from items, data parsed)."""
    now = now or datetime.now(timezone.utc)
    d = item.get("data") or {}
    dt = d.get("doc_type") or (doc or {}).get("doc_type") or "?"
    label, klass, p1, p2, what = DOC_TYPES.get(
        dt, (f"Recorded document ({dt})", "", "party 1", "party 2",
             "A document of this type was recorded against the property."))
    n_parcels = (doc or {}).get("n_parcels") or 1
    pct = (doc or {}).get("percent_trans")
    amount = d.get("amount")
    unknown = []
    if not d.get("in_pluto"):
        unknown.append("building facts (not found in PLUTO for this lot)")
    if dt in ("DEED", "DEEDO"):
        unknown.append("whether this was an arm's-length market sale")
    if dt == "MTGE":
        unknown.append("the property's price or value (a loan amount is not a price)")
    unknown.append("whether the property is for sale now (a filing does not say)")
    caveats = []
    if n_parcels and n_parcels > 1:
        caveats.append(f"This document covers {n_parcels} parcels; the amount is for all of "
                       "them together, not this lot alone.")
    if dt in ("DEED", "DEEDO") and pct is not None and pct < 100:
        caveats.append(f"Only {pct:.0f}% of the interest was transferred.")
    if amount is not None and amount < 1000:
        caveats.append("Nominal amount: usually a non-market transfer.")
    rec = _day(d.get("recorded"))
    age = None
    if rec:
        try:
            age = (now.date() - datetime.strptime(rec, "%Y-%m-%d").date()).days
        except ValueError:
            pass
    return {
        "doc_id": d.get("document_id") or item.get("key"),
        "doc_type": dt, "label": label, "class": klass,
        "party1": p1, "party2": p2, "what": what,
        "amount": amount, "amount_means": AMOUNT_MEANING.get(dt, "the amount stated on the document."),
        "document_date": _day((doc or {}).get("document_date")),
        "recorded": rec, "recorded_age_days": age,
        "first_seen": item.get("ts"),
        "feed_newest": _day(feed_newest),
        "address": d.get("address"), "borough": d.get("borough"),
        "bbl": d.get("bbl"), "zipcode": d.get("zipcode"),
        "building": {k: d.get(k) for k in ("bldgclass", "unitsres", "unitstotal",
                                           "yearbuilt", "bldgarea", "assesstot", "ownername")
                     if d.get(k) is not None},
        "importance": item.get("importance"),
        "unknown": unknown, "caveats": caveats,
        "acris_url": item.get("url"), "zola_url": zola_url(d.get("bbl") or ""),
    }


def load_item(conn, item_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not r:
        return None
    it = dict(r) if hasattr(r, "keys") else None
    if it is None:
        cols = [c[0] for c in conn.execute("SELECT * FROM items LIMIT 0").description]
        it = dict(zip(cols, r))
    try:
        it["data"] = json.loads(it.get("data_json") or "{}")
    except ValueError:
        it["data"] = {}
    return it


def load_doc(conn, doc_id: str) -> dict | None:
    try:
        r = conn.execute("SELECT document_id, doc_type, document_date, recorded, amount, "
                         "percent_trans, n_parcels FROM acris_docs WHERE document_id=?",
                         (doc_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    return dict(zip(("document_id", "doc_type", "document_date", "recorded", "amount",
                     "percent_trans", "n_parcels"), r))


def feed_newest(conn) -> str | None:
    try:
        r = conn.execute("SELECT MAX(recorded) FROM acris_docs").fetchone()
        return r[0] if r else None
    except Exception:
        return None


def money(v) -> str:
    if v is None:
        return "no amount"
    if v >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v:,.0f}"
