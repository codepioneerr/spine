"""
surfaces.research — live source retrieval + deterministic, cited synthesis.

What is implemented, precisely (no more, no less):

  LIVE SOURCE RETRIEVAL (yes)
    - NIH MedlinePlus health-topic search (consumer guidance pages)
    - PubMed (NCBI E-utilities): systematic reviews / meta-analyses only,
      abstracts fetched. These are research summaries, not guidance.
  BROADER WEB SEARCH (no)
    No search-engine API is configured on this box. Nothing here crawls or
    searches the open web.
  MODEL REASONING (no)
    No language model writes these answers. "Synthesis" means: candidate
    sentences from all retrieved sources are scored by fixed rules
    (keyword overlap, the aspect asked about, a study's Results/Conclusion
    sections), de-duplicated, ordered, and QUOTED with a [n] citation.
    Nothing is paraphrased, so nothing can be misattributed; what the
    rules cannot do is weigh evidence the way an expert would.

What leaves the box: up to 3 topic keywords per source query (e.g.
"caffeine sleep"). Never the question text, health values or identifiers.
NLM/NCBI see an anonymous keyword query from this machine's IP.
/settings research off disables all of it; cached results still answer.

Aspects ("how much", "is it safe", "when", "does it work") steer which
sentences are chosen, so a follow-up such as "how much is too much?" on a
caffeine answer re-ranks the same topic instead of starting over.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

MEDLINE = "https://wsearch.nlm.nih.gov/ws/query"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
UA = {"User-Agent": "spine-assist/1 (personal research assistant)"}
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
all worth try trying start stop doing done normal enough okay fine bad too there their much
about anything something someone ever really actually lot lots kind sort
bed bedtime night nights evening morning afternoon late early prevent prevents
timing dose doses amount safe safety risk risks side effective efficacy limit
one two three four five six seven eight nine ten around about approx pm am tonight tomorrow
going gonna wreck mess ruin hurt bad okay ok like only just had have having drink drank
question wondering wonder think maybe probably usually sometimes always never often
practice feel feels feeling get gets getting matter matters
building build builds built gain gaining lose losing""".split())

ASPECTS = {
    "dose": (r"\bhow much\b|\bhow many\b|\bdose\b|\bamount\b|\btoo much\b|\blimit\b|\bmg\b|\bper day\b",
             r"\b\d+\s?(mg|g|grams|cups?|hours?|minutes?|servings?)\b|\bup to\b|\blimit\b|\bdaily\b|\bper day\b|\bmoderate\b"),
    "safety": (r"\bsafe\b|\brisks?\b|\bside effects?\b|\bdanger|\bharm|\bbad for\b",
               r"\brisk|\bside effect|\bharm|\bsafe|\bavoid|\bcaution|\badverse|\btoo much\b|\bproblem"),
    "timing": (r"\bwhen\b|\btiming\b|\bbefore bed\b|\bat night\b|\bin the evening\b|\bhow long before\b|"
               r"\b\d{1,2}\s?(am|pm)\b|\btonight\b|\blate in the day\b|\bafternoon\b|\bbefore or after\b",
               r"\bhours? before\b|\bbedtime\b|\bevening\b|\bafternoon\b|\btiming\b|\bbefore sleep\b|\bat night\b"),
    "efficacy": (r"\bdoes it work\b|\bworks?\b|\beffective\b|\bhelp(s)? with\b|\bimprove\b|\bbenefit",
                 r"\beffective|\bimprove|\bbenefit|\breduc|\bincreas|\bsignificant|\bassociated with\b"),
}
UNCERTAIN = re.compile(r"\b(limited|low[- ]certainty|low[- ]quality|very low|heterogene|mixed|"
                       r"inconsistent|insufficient (evidence|data)|(further|future) (research|studies)|more (research|studies)|"
                       r"small (sample|number)|unclear|uncertain|may not|bias|(are|is) needed)\b", re.I)
AIMS = re.compile(r"^to (investigate|examine|evaluate|assess|determine|explore|review|compare|summari[sz]e|identify|synthesi[sz]e)\b|^(this|the present|our|we)\b.{0,40}\b(review|meta-analysis|study|paper|aim|aimed|"
                  r"investigat|examin|explor|evaluat|assess)|\b(purpose|objective|aim) (of|was)\b", re.I)
FINDING = re.compile(r"\d|\b(reduc|increas|improv|decreas|associated|no (significant )?(effect|difference)|"
                     r"did not|found|showed|suggest)", re.I)
METHODS = re.compile(r"\b(we searched|databases?|PRISMA|search strategy|were searched|registered|"
                     r"PROSPERO|eligible|inclusion criteria|risk of bias tool|data extraction|"
                     r"random[- ]effects model)\b", re.I)

POPULATIONS = [
    (r"knee replacement|arthroplasty|\bTKA\b|\bTHA\b", "people after joint replacement"),
    (r"osteoarthritis", "people with osteoarthritis"),
    (r"older adults|elderly|aged \d{2}|postmenopausal|\bolder\b", "older adults"),
    (r"pregnan", "pregnant people"),
    (r"child|adolescen|pediatric|infant", "children or adolescents"),
    (r"athlete|soccer|football players|runners|military|soldiers", "athletes or military"),
    (r"diabet|cancer|stroke|heart failure|kidney disease|parkinson|dementia|multiple sclerosis|"
     r"patients", "people with a medical condition"),
]


def population(doc: dict) -> str | None:
    """Who a study was about: from its TITLE, or an explicit "in/among <group>"
    phrase in the abstract. A passing mention of a group is not the population
    ("...in healthy knees" is not an osteoarthritis study)."""
    title = doc.get("title", "")
    if re.search(r"\bhealthy\b", title, re.I):
        return None
    for pat, label in POPULATIONS:
        if re.search(pat, title, re.I):
            return label
    body = doc.get("summary", "")[:800]
    for pat, label in POPULATIONS:
        if re.search(r"\b(in|among|of) (\w+[ -]){0,3}(" + pat + ")", body, re.I):
            return label
    return None


GENERIC = {"train", "training", "exercise", "exercises", "workout", "workouts", "sport", "sports",
           "health", "healthy", "body", "fitness", "practice", "activity", "people", "adults"}

SCHEMA = """CREATE TABLE IF NOT EXISTS research_cache (
  q TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL)"""


# ── query construction ──────────────────────────────────────────────

# Everyday words -> the term the sources index. Keeps "two coffees" from
# becoming a genetics query.
SYNONYMS = {"coffee": "caffeine", "coffees": "caffeine", "espresso": "caffeine", "latte": "caffeine",
            "energy drink": "caffeine", "energy drinks": "caffeine", "preworkout": "caffeine",
            "pre-workout": "caffeine", "slept": "sleep", "sleeping": "sleep", "asleep": "sleep",
            "squats": "squat", "stretch": "stretching", "stretches": "stretching",
            "sit": "sedentary", "sitting": "sedentary", "inactive": "sedentary", "lectures": "",
            "lecture": "", "class": "", "classes": "", "lifting": "weight training", "weights": "weight training",
            "sore": "muscle soreness", "soreness": "muscle soreness", "booze": "alcohol",
            "drinking": "alcohol", "beer": "alcohol", "vape": "vaping", "weed": "marijuana",
            "cannabis": "marijuana"}


def keywords(question: str, limit: int = 3) -> list[str]:
    t = (question or "").lower()
    for phrase in ("energy drinks", "energy drink"):
        t = t.replace(phrase, SYNONYMS[phrase].replace(" ", "_"))
    words = re.findall(r"[a-z][a-z_\-]{2,}", t)
    out = []
    for w in words:
        w = SYNONYMS.get(w, w).replace("_", " ")
        if w and w not in STOP and w not in out:
            out.append(w)
    return out[:limit]


def aspect(question: str) -> str | None:
    t = (question or "").lower()
    for name, (ask, _) in ASPECTS.items():
        if re.search(ask, t):
            return name
    return None


def _stem(k):
    return k[:-1] if k.endswith("s") and len(k) > 4 else k


def _strip(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"</(p|li|h\d)>", ". ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"\.\s*\.", ".", s).strip()


# ── MedlinePlus ─────────────────────────────────────────────────────

def parse_medline(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for doc in root.iter("document"):
        d = {"url": doc.get("url"), "title": "", "alt": [], "summary": "", "raw": "",
             "org": "NIH MedlinePlus", "kind": "guidance"}
        for c in doc.findall("content"):
            n, txt = c.get("name"), "".join(c.itertext())
            if n == "title":
                d["title"] = _strip(txt)
            elif n == "altTitle":
                d["alt"].append(_strip(txt))
            elif n == "FullSummary":
                raw = re.sub(r"</?span[^>]*>", "", html.unescape(txt))   # search-term highlighting
                d["summary"], d["raw"] = _strip(raw), raw
        out.append(d)
    return out


def candidates_medline(raw: str) -> list[dict]:
    """Quotable units in source order. A list item keeps the sentence that
    introduces it ("...limit or avoid caffeine if you … have sleep disorders")."""
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
    return [{"text": x, "label": ""} for x in out if 15 < len(x) < 500]


def medline(kws, opener=None) -> list[dict]:
    q = urllib.parse.urlencode({"db": "healthTopics", "term": " ".join(kws), "retmax": 5})
    docs = parse_medline(_get(f"{MEDLINE}?{q}", opener))
    for d in docs:
        d["cands"] = candidates_medline(d["raw"]) or [
            {"text": x, "label": ""} for x in re.split(r"(?<=[.!?])\s+", d["summary"]) if len(x) > 20]
    return docs


# ── PubMed (reviews only) ───────────────────────────────────────────

REVIEW_FILTER = ('(systematic review[pt] OR meta-analysis[pt]) AND humans[mh] '
                 'AND ("2010"[dp] : "3000"[dp])')


def pubmed(kws, opener=None, n=3) -> list[dict]:
    # Main keyword must be in the title; the rest anywhere in title/abstract.
    parts = [f"{kws[0]}[ti]"] + [f"{k}[tiab]" for k in kws[1:]]
    term = " AND ".join(parts) + " AND " + REVIEW_FILTER
    q = urllib.parse.urlencode({"db": "pubmed", "retmode": "json", "retmax": n, "sort": "relevance",
                                "tool": "spine-assist", "term": term})
    ids = json.loads(_get(f"{EUTILS}/esearch.fcgi?{q}", opener))["esearchresult"]["idlist"]
    if not ids:
        return []
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
                                "tool": "spine-assist"})
    return parse_pubmed(_get(f"{EUTILS}/efetch.fcgi?{q}", opener))


def parse_pubmed(xml_text: str) -> list[dict]:
    out = []
    for a in ET.fromstring(xml_text).iter("PubmedArticle"):
        pmid = a.findtext(".//PMID")
        title = _strip("".join(a.find(".//ArticleTitle").itertext())) if a.find(".//ArticleTitle") is not None else ""
        year = a.findtext(".//PubDate/Year") or (a.findtext(".//PubDate/MedlineDate") or "")[:4]
        journal = a.findtext(".//Journal/Title") or ""
        types = [p.text for p in a.iter("PublicationType")]
        cands, summary = [], []
        for t in a.iter("AbstractText"):
            label = (t.get("Label") or t.get("NlmCategory") or "").upper()
            text = _strip("".join(t.itertext()))
            summary.append(text)
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
                if 25 < len(s) < 500:
                    cands.append({"text": s.strip(), "label": label})
        if not cands:
            continue
        kind = "meta-analysis" if "Meta-Analysis" in types else "systematic review"
        out.append({"url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "title": title,
                    "alt": [], "summary": " ".join(summary), "org": f"{journal} {year}".strip(),
                    "kind": kind, "cands": cands, "year": year})
    return out


_NCBI_GAP_S = 0.4          # NCBI allows 3 requests/s without an API key
_last_ncbi = [0.0]


def _get(url, opener=None):
    import urllib.error
    ncbi = "eutils.ncbi.nlm.nih.gov" in url
    for attempt in (1, 2):
        if ncbi and opener is None:
            wait = _NCBI_GAP_S - (time.time() - _last_ncbi[0])
            if wait > 0:
                time.sleep(wait)
            _last_ncbi[0] = time.time()
        req = urllib.request.Request(url, headers=UA)
        try:
            with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_S) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                time.sleep(1.2)
                continue
            raise


# ── relevance + synthesis (deterministic) ───────────────────────────

def score_doc(doc: dict, kws: list[str]) -> int:
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


def relevant(doc, kws):
    """Research abstracts must name a keyword in their TITLE (PubMed's own
    relevance ranking happily returns wastewater chemistry for 'caffeine')."""
    head = " ".join([doc["title"]] + doc.get("alt", [])).lower()
    if any(_stem(k) in head for k in kws):
        return score_doc(doc, kws) > 0
    if doc.get("kind") != "guidance":
        return False                       # research: title must name the topic
    body = doc["summary"].lower()
    counts = [body.count(_stem(k)) for k in kws]
    # guidance page without a title hit: every keyword present, the main one repeatedly
    return all(c >= 1 for c in counts) and counts[0] >= 3


def _words(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))


def synthesize(docs: list[dict], kws: list[str], asp: str | None, n_points: int = 5) -> dict:
    """Pick quoted points across sources. Returns {points:[{text, src}], uncertainty:[...]}"""
    stems = [_stem(k) for k in kws]
    cue = re.compile(ASPECTS[asp][1], re.I) if asp else None
    scored = []
    for si, d in enumerate(docs):
        for ci, c in enumerate(d["cands"]):
            t = c["text"]
            low = t.lower()
            if METHODS.search(t):
                continue
            hit = sum(st in low for st in stems)
            on_aspect = bool(cue and cue.search(t))
            # Multi-keyword questions need both words in one sentence, except a sentence
            # that answers the aspect asked (e.g. a dose line that names only "caffeine").
            if hit < min(2, len(stems)) and not (on_aspect and stems[0] in low):
                continue
            if AIMS.search(t) and d["kind"] != "guidance":
                continue                    # what a review set out to do is not what it found
            s = 3 * hit
            if cue and cue.search(t):
                s += 4
            if c["label"] in ("RESULTS", "CONCLUSIONS", "CONCLUSION", "FINDINGS"):
                s += 2
            if d["kind"] != "guidance" and FINDING.search(t):
                s += 2
            if c["label"] in ("BACKGROUND", "INTRODUCTION", "OBJECTIVE", "OBJECTIVES", "METHODS"):
                s -= 2
            if d["kind"] == "guidance":
                s += 1                      # consumer guidance first when tied
            scored.append((s, si, ci, t))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    points, per_src, seen = [], {}, []
    for s, si, ci, t in scored:
        if len(points) >= n_points:
            break
        if per_src.get(si, 0) >= 2:
            continue
        w = _words(t)
        if any(len(w & v) / max(1, len(w | v)) > 0.5 for v in seen):
            continue
        if UNCERTAIN.search(t) and docs[si]["kind"] != "guidance":
            continue                        # reported separately below
        points.append({"text": t, "src": si, "score": s})
        per_src[si] = per_src.get(si, 0) + 1
        seen.append(w)
    unc = []
    for si, d in enumerate(docs):
        if d["kind"] == "guidance":
            continue
        for c in d["cands"]:
            if UNCERTAIN.search(c["text"]) and not METHODS.search(c["text"]) \
                    and any(st in c["text"].lower() for st in stems + ["evidence", "studies", "trials"]):
                unc.append({"text": c["text"], "src": si})
                break
    gap = None
    if asp and cue and not any(cue.search(p["text"]) for p in points):
        gap = asp
    return {"points": points, "uncertainty": unc[:2], "gap": gap}


# ── public entry point ──────────────────────────────────────────────

def lookup(question: str, conn=None, online: bool = True, opener=None, now=None,
           kws: list[str] | None = None, asp: str | None = None) -> dict:
    """{status: ok|none|offline|disabled, keywords, aspect, sources, points, uncertainty,
       retrieved, cached, retrieval:[which services answered]}"""
    now = now or time.time()
    kws = kws or keywords(question)
    asp = asp if asp is not None else aspect(question)
    res = {"keywords": kws, "aspect": asp, "sources": [], "points": [], "uncertainty": [], "gap": None,
           "cached": False, "retrieved": None, "retrieval": []}
    if not kws:
        return dict(res, status="none")
    key = " ".join(sorted(kws))
    docs, cached = None, False
    if conn is not None:
        conn.execute(SCHEMA)
        row = conn.execute("SELECT fetched, body FROM research_cache WHERE q=?", (key,)).fetchone()
        if row and (now - row[0] < CACHE_DAYS * 86400 or not online):
            body = json.loads(row[1])
            if "docs" in body:
                docs, cached = body["docs"], True
                res["retrieved"], res["retrieval"] = body.get("retrieved"), body.get("retrieval", [])
                if body.get("keywords_used") and body["keywords_used"] != kws:
                    res["broadened_from"] = list(kws)
                    res["keywords"] = kws = body["keywords_used"]
    if docs is None:
        if not online:
            return dict(res, status="disabled")
        # Full keyword set first, then broaden by dropping the last keyword,
        # stopping at the first set with relevant sources. The answer says so.
        tried = []
        # Broaden by dropping generic words first, so the specific subject
        # survives ("train boxing sick" -> "boxing sick" -> "sick", never "train").
        order = sorted(kws, key=lambda k: (k in GENERIC, kws.index(k)))
        specific = [w for w in kws if w not in GENERIC]
        floor = max(1, len(specific) - 1)          # drop generic words + at most one specific word
        for k in range(len(order), 0, -1):
            sub = order[:k]
            if k < len(order) and (all(w in GENERIC for w in sub) or
                                   sum(w not in GENERIC for w in sub) < floor):
                break
            tried.append(sub)
            docs, used, failed = [], [], 0
            for name, fn in (("MedlinePlus", medline), ("PubMed", pubmed)):
                try:
                    got = [d for d in fn(sub, opener) if relevant(d, sub)]
                    got.sort(key=lambda d: -score_doc(d, sub))
                    docs += got[:2]
                    used.append(name)
                except Exception:
                    failed += 1
            if failed == 2:
                return dict(res, status="offline")
            if docs and synthesize(docs, sub, asp)["points"]:
                break
        if tried[-1] != kws:
            res["broadened_from"] = list(kws)
            res["keywords"] = kws = tried[-1]
        res["retrieved"] = time.strftime("%Y-%m-%d", time.gmtime(now))
        res["retrieval"] = used
        if conn is not None:
            with conn:
                conn.execute("INSERT OR REPLACE INTO research_cache VALUES(?,?,?)",
                             (key, now, json.dumps({"docs": docs, "retrieved": res["retrieved"],
                                                    "retrieval": used, "keywords_used": kws})))
    syn = synthesize(docs, kws, asp)
    if not syn["points"]:
        return dict(res, status="none", cached=cached)
    used_src = sorted({p["src"] for p in syn["points"]} | {u["src"] for u in syn["uncertainty"]})
    remap = {old: i for i, old in enumerate(used_src)}
    res.update(status="ok", cached=cached,
               sources=[dict({k: docs[i][k] for k in ("title", "url", "org", "kind")},
                             population=population(docs[i]) if docs[i]["kind"] != "guidance" else None)
                        for i in used_src],
               points=[{"text": p["text"], "n": remap[p["src"]] + 1} for p in syn["points"]],
               uncertainty=[{"text": u["text"], "n": remap[u["src"]] + 1} for u in syn["uncertainty"]],
               gap=syn.get("gap"))
    return res
