"""
core.property_live — on-demand lookups against NYC's public ACRIS datasets.

Used only when Nick asks a follow-up ("who was the lender?", "what else
happened at this building?"). Public records, queried by document id or
borough/block/lot; nothing personal leaves the box.

  parties  636b-3b5g  ACRIS Real Property Parties   (party_type 1 / 2)
  legals   8h5j-fqxa  ACRIS Real Property Legals    (document -> lot)
  master   bnx9-e6tj  ACRIS Real Property Master    (type, dates, amount)
  codes    7isb-wh4c  Document Control Codes        (party role names, retrieved 2026-10-06)

Party roles depend on the document type, so they are named from the codes
dataset rather than assumed: for MTGE party 1 is the borrower and party 2
the lender; for DEED party 1 is the grantor/seller and party 2 the grantee/buyer.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

BASE = "https://data.cityofnewyork.us/resource"
TIMEOUT_S = 15

_CODES_PATH = os.path.join(os.path.dirname(__file__), "acris_codes.json")
try:
    with open(_CODES_PATH, encoding="utf-8") as _fh:
        CODES = json.load(_fh)["codes"]       # code -> [description, party1 role, party2 role]
except OSError:
    CODES = {}


def roles(doc_type: str) -> tuple[str, str]:
    c = CODES.get(doc_type)
    if not c:
        return ("party 1", "party 2")
    return ((c[1] or "party 1").lower(), (c[2] or "party 2").lower())


def type_name(t) -> str:
    c = CODES.get(t or "")
    return c[0].lower() if c and c[0] else (t or "?")


def _get(dataset, params, opener=None):
    url = f"{BASE}/{dataset}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "spine-assist/1"})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def parties(doc_id: str, doc_type: str, opener=None) -> dict:
    rows = _get("636b-3b5g", {"document_id": doc_id, "$limit": 50}, opener)
    r1, r2 = roles(doc_type)
    out = {"doc_id": doc_id, "roles": (r1, r2), "party1": [], "party2": [],
           "source": f"https://data.cityofnewyork.us/resource/636b-3b5g.json?document_id={doc_id}"}
    for r in rows:
        name = (r.get("name") or "").strip()
        city = " ".join(x for x in ((r.get("city") or "").title(), r.get("state") or "") if x)
        entry = {"name": name, "city": city}
        if str(r.get("party_type")) == "1":
            out["party1"].append(entry)
        elif str(r.get("party_type")) == "2":
            out["party2"].append(entry)
    return out


def lot_history(bbl: str, limit: int = 12, opener=None) -> dict:
    """Recorded documents on one lot, newest first (public master + legals)."""
    boro, block, lot = int(bbl[0]), int(bbl[1:6]), int(bbl[6:])
    legals = _get("8h5j-fqxa", {"$where": f"borough={boro} AND block={block} AND lot={lot}",
                                "$select": "document_id", "$limit": 500}, opener)
    ids = sorted({r["document_id"] for r in legals if r.get("document_id")})
    docs = []
    for i in range(0, len(ids), 80):
        chunk = ",".join(f"'{d}'" for d in ids[i:i + 80])
        docs += _get("bnx9-e6tj", {"$where": f"document_id in({chunk})",
                                   "$select": "document_id,doc_type,document_date,recorded_datetime,"
                                              "document_amt",
                                   "$limit": 200}, opener)
    docs.sort(key=lambda d: d.get("recorded_datetime") or "", reverse=True)
    counts = {}
    for d in docs:
        counts[d.get("doc_type")] = counts.get(d.get("doc_type"), 0) + 1
    return {"bbl": bbl, "total": len(docs), "counts": counts, "recent": docs[:limit],
            "deeds": [d for d in docs if d.get("doc_type") in ("DEED", "DEEDO")],
            "first": docs[-1].get("recorded_datetime", "")[:10] if docs else None,
            "source": "https://data.cityofnewyork.us/d/8h5j-fqxa"}
