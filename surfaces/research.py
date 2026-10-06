"""
surfaces.research — retrieved, cited answers to questions the vetted
registry (surfaces.evidence) does not cover. No model.

Source: MedlinePlus health-topic search (U.S. National Library of
Medicine), https://wsearch.nlm.nih.gov/ws/query. Public, no key.

## What leaves the box

Only 1-3 extracted topic keywords (e.g. "creatine"), never the question,
never any health value, never an identifier. Not Gemini, not Hermes.
NLM sees an anonymous keyword query from this machine's IP. /settings
research off disables it; cached results still answer.

## How an answer is built (deterministic)

1. Keywords: lowercase words minus stopwords/personal words, max 3.
2. Retrieve up to 5 topics. A topic is RELEVANT only if a keyword appears
   in its title/alt-titles, or at least twice in its summary. MedlinePlus
   ranks loosely ("creatine" -> "Heart Attack" first), so ranking alone
   is not evidence of relevance.
3. Quote up to 3 of the source's own sentences that contain a keyword.
   Extractive, so nothing is paraphrased or invented; the reader sees the
   source's words and the link.
4. Cache by keyword set for CACHE_DAYS; offline uses the cache, else says
   so plainly.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

URL = "https://wsearch.nlm.nih.gov/ws/query"
CACHE_DAYS = 30
TIMEOUT_S = 12

STOP = set("""a an the and or but if of to in on for with without at by from is are was were be
been being do does did doing have has had i me my mine we our you your he she they them it its this
that these those what which who whom how why when where can could should would will shall may might
must about into over under than then so very just also more most less much many some any each every
good bad best better worse tell explain help please know want need like get make take give use using
used really thing things way ok okay day days week weeks today yesterday daily recent recently my
data based compare compared vs versus is it safe am im i'm dont don't does doesn't should i
help helps helped affect affects effect effects useful before after take taking took work works
all worth try trying start stop doing done normal enough okay fine bad""".split())

SCHEMA = """CREATE TABLE IF NOT EXISTS research_cache (
  q TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL)"""


def keywords(question: str, limit: int = 3) -> list[str]:
    words = re.findall(r"[a-z][a-z\-]{2,}", (question or "").lower())
    out = []
    for w in words:
        if w not in STOP and w not in out:
            out.append(w)
    return out[:limit]


def _strip(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"</(p|li|h\d)>", ". ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"\.\s*\.", ".", s).strip()


def parse(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for doc in root.iter("document"):
        d = {"url": doc.get("url"), "title": "", "alt": [], "summary": "", "org": "", "raw": ""}
        for c in doc.findall("content"):
            n = c.get("name")
            txt = "".join(c.itertext())
            if n == "title":
                d["title"] = _strip(txt)
            elif n == "altTitle":
                d["alt"].append(_strip(txt))
            elif n == "FullSummary":
                d["summary"] = _strip(txt)
                d["raw"] = html.unescape(txt)
            elif n == "organizationName":
                d["org"] = _strip(txt)
        out.append(d)
    return out


def _stem(k):
    return k[:-1] if k.endswith("s") and len(k) > 4 else k


def score(doc: dict, kws: list[str]) -> int:
    """0 = not relevant. A title/alt-title hit is worth far more than body
    mentions; with no title hit, EVERY keyword must appear in the body."""
    head = " ".join([doc["title"]] + doc["alt"]).lower()
    body = doc["summary"].lower()
    stems = [_stem(k) for k in kws]
    in_head = sum(st in head for st in stems)
    in_body = [body.count(st) for st in stems]
    if in_head:
        return 100 * in_head + sum(min(c, 5) for c in in_body)
    if all(c >= 1 for c in in_body) and sum(in_body) >= 2:
        return sum(min(c, 5) for c in in_body)
    return 0


def relevant(doc: dict, kws: list[str]) -> bool:
    return score(doc, kws) > 0


def candidates(raw: str) -> list[str]:
    """Quotable units in source order. A list item is quoted together with
    the sentence that introduces it, so "Have sleep disorders" keeps its
    meaning ("...limit or avoid caffeine if you: ... have sleep disorders")."""
    out, lead = [], ""
    for m in re.finditer(r"<ul>(.*?)</ul>|<p>(.*?)</p>|([^<]+)", raw or "", re.S):
        if m.group(1) is not None:
            for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S):
                item = _strip(li).rstrip(".")
                if item:
                    out.append(f"{lead} … {item}." if lead else item + ".")
        else:
            text = _strip(m.group(2) if m.group(2) is not None else m.group(3))
            sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
            out += [x for x in sents if not x.endswith(":")]
            lead = sents[-1].rstrip(":") if sents and sents[-1].endswith(":") else ""
    return [x for x in out if 15 < len(x) < 500]


def quotes(doc: dict, kws: list[str], n: int = 3) -> list[str]:
    """The source's own words, most keywords first, original order kept."""
    sents = candidates(doc.get("raw") or "") or [
        x.strip() for x in re.split(r"(?<=[.!?])\s+", doc["summary"]) if 20 < len(x.strip()) < 400]
    stems = [_stem(k) for k in kws]
    scored = [(sum(st in x.lower() for st in stems), i, x) for i, x in enumerate(sents)]
    best = max((t[0] for t in scored), default=0)
    top = [t for t in scored if t[0] and t[0] >= best][:n] or \
        sorted([t for t in scored if t[0]], key=lambda t: (-t[0], t[1]))[:n]
    if not top:
        return sents[:2]
    return [x for _, _, x in sorted(top, key=lambda t: t[1])]


def fetch(kws: list[str], opener=None) -> str:
    q = urllib.parse.urlencode({"db": "healthTopics", "term": " ".join(kws), "retmax": 5})
    req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": "spine-assist/1"})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as r:
        return r.read().decode("utf-8", errors="replace")


def lookup(question: str, conn=None, online: bool = True, opener=None, now=None) -> dict:
    """{status: ok|none|offline|disabled, keywords, sources:[{title,url,org,quotes}], cached}"""
    now = now or time.time()
    kws = keywords(question)
    res = {"keywords": kws, "sources": [], "cached": False, "retrieved": None}
    if not kws:
        return dict(res, status="none")
    key = " ".join(sorted(kws))
    if conn is not None:
        conn.execute(SCHEMA)
        row = conn.execute("SELECT fetched, body FROM research_cache WHERE q=?", (key,)).fetchone()
        if row and (now - row[0] < CACHE_DAYS * 86400 or not online):
            res.update(json.loads(row[1]), cached=True)
            return res
    if not online:
        return dict(res, status="disabled")
    try:
        docs = parse(fetch(kws, opener))
    except Exception:
        if conn is not None:
            row = conn.execute("SELECT body FROM research_cache WHERE q=?", (key,)).fetchone()
            if row:
                res.update(json.loads(row[0]), cached=True)
                return res
        return dict(res, status="offline")
    ranked = sorted(((score(d, kws), d) for d in docs), key=lambda x: -x[0])
    best = ranked[0][0] if ranked else 0
    srcs = [{"title": d["title"], "url": d["url"], "org": d["org"] or "MedlinePlus",
             "quotes": quotes(d, kws)} for sc, d in ranked if sc and sc >= best / 2][:2]
    body = {"status": "ok" if srcs else "none", "sources": srcs,
            "retrieved": time.strftime("%Y-%m-%d", time.gmtime(now))}
    if conn is not None:
        with conn:
            conn.execute("INSERT OR REPLACE INTO research_cache VALUES(?,?,?)",
                         (key, now, json.dumps(body)))
    res.update(body)
    return res
