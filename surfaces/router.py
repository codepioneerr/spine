"""
surfaces.router — deterministic intent routing for free text.

Keyword rules, not a model: they run in microseconds, never leave the box,
and are testable. Anything unmatched gets an honest "not covered yet" plus a
saved research request — never a guessed answer.
"""

from __future__ import annotations

import re

RULES = [
    ("focus", r"(\b(include|add|put)\b.*\b(brief|morning|daily)\b|\bremind me\b|\bsubscribe\b|^/?focus\b)"),
    ("sexual", r"\b(sex\w*|erections?|erectile|libido|ed|stis?|stds?|testosterone|condoms?|kegels?|pelvic floor)\b"),
    ("mobility", r"\b(goota|goata|mobility|ankle|knee|hip|gait|walking comfort|stiff)"),
    ("workout", r"\b(workout|exercise routine|yoga mat|mat\b|strength|push-?up|dorm workout|train)"),
    ("sleep", r"\b(sleep|slept|insomnia|bedtime|nap)"),
    ("improve", r"\b(improve|two (most )?useful|what should i (do|work on)|priorit)"),
    ("activity", r"\b(activity|guideline|guidance|150 min|active enough|compare)"),
    ("quant", r"\b(quant|position|trade|equity|return|simulation|signal|portfolio|p&l|pnl)"),
    ("trend", r"\b(trend|chart|graph|changed|over time|this week|last week|30 days|7 days)"),
    ("health", r"\b(health|steps?|hrv|heart|resting|oxygen|calorie|kcal)"),
    ("glossary", r"\b(what is (a |an )?|what's (a |an )?|define |explain )(deed|mortgage|mtge|bbl|acris|pluto|grantor|grantee|assessed value|avm|condo|valuation)"),
    ("property", r"\b(property|deed|mortgage|acris|pluto|building(?! (muscle|strength|endurance))|real estate|parcel)"),
    ("brief", r"\b(brief|today|summary)"),
    ("privacy", r"\b(privacy|private|who can see)"),
]

CARD_FOLLOWUPS = [
    ("p_saleloan", r"(sale|loan|sold|refinanc)"),
    ("p_score", r"(score|85|ranking|why .*(flag|send|sent|select))"),
    ("p_building", r"(building|lot|units|what do we know)"),
    ("next", r"(what should i do|next step|what now)"),
    ("explain", r"(explain|what does this mean|what is this|why|\?)"),
]


def route(text: str) -> str:
    t = (text or "").lower()
    for name, pat in RULES:
        if re.search(pat, t):
            return name
    return "unknown"


def followup(text: str) -> str:
    t = (text or "").lower()
    for name, pat in CARD_FOLLOWUPS:
        if re.search(pat, t):
            return name
    return "explain"


def glossary_term(text: str) -> str | None:
    m = re.search(r"(deed|mortgage|mtge|bbl|acris|pluto|grantor|grantee|assessed value|avm|condo|valuation)",
                  (text or "").lower())
    return m.group(1) if m else None


def focus_topic(text: str) -> str | None:
    t = (text or "").lower()
    for key, pat in (("mobility", "mobil|stretch|ankle|hip"), ("walking", "walk"),
                     ("sleep consistency", "sleep"), ("dorm strength", "strength|workout"),
                     ("real-estate basics", "real.?estate|property|deed")):
        if re.search(pat, t):
            return key
    return None


def trend_metric(text: str):
    t = (text or "").lower()
    days = 30 if re.search(r"30|month", t) else 7
    for word, m in (("hrv", "heart_rate_variability"), ("resting", "resting_heart_rate"),
                    ("heart", "resting_heart_rate"), ("exercise", "apple_exercise_time"),
                    ("energy", "active_energy"), ("calor", "active_energy"),
                    ("oxygen", "blood_oxygen_saturation")):
        if word in t:
            return m, days
    return "step_count", days


# Words the canned health answers already cover. If a health-ish question
# names anything else (caffeine, creatine, posture...), it is a novel
# question and goes to research instead of a canned reply.
COVERED = set("""sleep slept sleeping changed change trend chart graph steps step hrv heart rate resting
oxygen calories kcal health improve two most useful things activity active guidance general guideline
guidelines compare minutes week weeks month time over this last show see insomnia bedtime
mobility ankle ankles knee knees hip hips gait goota goata comfortable stiff quiet minute yoga mat
dorm give routine workout workouts strength dorm-mat plan""".split())
NOVEL_OK = {"sleep", "health", "activity", "improve", "trend", "unknown", "mobility", "workout"}


def novel(text: str, intent: str) -> bool:
    if intent not in NOVEL_OK:
        return False
    from surfaces.research import keywords
    extra = [k for k in keywords(text, limit=6) if k not in COVERED]
    return bool(extra) or intent == "unknown"


FOLLOWUP = re.compile(
    r"^(and|but|so|also|ok(ay)?,?|what about|how about|how much|how many|how long|how often|"
    r"is (it|that|this|there)|are (they|those)|does (it|that|this)|do (they|those)|why|when|who|whose|"
    r"what if|what else|what changed|tell me more|more|any (risks?|downsides?)|side effects|should i|can i|which|"
    r"is that|was (it|that|this)|were they|explain (it|that|this)|and the)\b", re.I)
PRONOUN = re.compile(r"\b(it|that|this|they|them|those|these|the lender|the buyer|the seller|"
                     r"the building|the rule|the position)\b", re.I)


def is_followup(text: str) -> bool:
    """A short message that only makes sense with the previous card."""
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    t = re.sub(r"\b(this|last|next) (week|month|morning|evening|year|time)\b", "", t, flags=re.I)
    words = t.split()
    if len(words) > 12:
        return False
    if FOLLOWUP.search(t):
        return True
    # "my knee hurts during it": a pronoun pointing back makes it a follow-up
    # even when a topic word is present.
    return len(words) <= 8 and bool(PRONOUN.search(t))


# Only explicit topic intents count as switching away from the current card;
# generic ones ("changed", "improve", "compare") stay with the card.
INTENT_DOMAIN = {"sexual": "health", "mobility": "health", "workout": "health", "sleep": "health",
                 "health": "health",
                 "glossary": "property", "property": "property", "quant": "quant"}


def switches_domain(text: str, card_domain: str) -> bool:
    """True when the message clearly names a different domain than the card."""
    d = INTENT_DOMAIN.get(route(text))
    if d is None:
        return False
    cd = "health" if card_domain in ("health", "research") else card_domain
    return d != cd


# Words that, with a card of this kind open, make a message a follow-up even
# without "it/that" ("I only have 8 minutes", "who borrowed the money?").
CARD_HOOKS = {
    "workout": r"\b(short|shorter|quick|minutes?|min|easier|harder|too (easy|hard)|hurts?|pain|sore|"
               r"knees?|progress|instead)\b",
    "acris_item": r"\b(lender|borrow\w*|bank|loan|buyer|seller|bought|sold|owner|parties|party|"
                  r"history|before|previous|else|happened|other|records?|documents?|worth|value|price|building|units)\b",
    "position": r"\b(rule|close|exit|stop|target|sell|profit|loss|track)\b",
    "summary_quant": r"\b(rule|change\w*|position\w*|risk\w*|return|equity|fees|eth|btc|gld|gold|bitcoin|djt)\b",
    "research": r"\b(how much|safe|risks?|side effects?|when|timing|work|dose|limit|instead|alternatives?)\b",
}


def card_hook(text: str, card: dict) -> bool:
    kind = (card.get("ref") or {}).get("kind") or ""
    if kind == "summary" and card.get("domain") == "quant":
        kind = "summary_quant"
    pat = CARD_HOOKS.get(kind)
    return bool(pat and re.search(pat, (text or "").lower()))


def new_topic(text: str, card: dict) -> bool:
    """Generic follow-up phrasing ("is it bad that...", "what about...") that
    names a NEW subject is a new question, unless the card is a research card
    (where "what about X" deliberately extends the topic)."""
    if (card.get("ref") or {}).get("kind") == "research":
        return False
    from surfaces.research import keywords
    return any(k not in COVERED for k in keywords(text, limit=4))


def should_follow(text: str, card: dict) -> bool:
    """Does this message continue the card? Research cards: only generic
    follow-up phrasing ("how much is too much?", "what about X") or a hook
    with no new subject; a full new question ("does protein timing matter
    for muscle?") starts a fresh search. Other cards: hook words, or generic
    phrasing without a new subject."""
    from surfaces.research import keywords
    kind = (card.get("ref") or {}).get("kind")
    if kind == "research":
        old = set((card.get("ref") or {}).get("keywords") or [])
        fresh = [k for k in keywords(text, limit=4) if k not in old and k not in COVERED]
        if re.match(r"^\s*(what about|how about|and|also|what if)\b", text, re.I):
            return True                     # explicit extension of the topic
        return (is_followup(text) or card_hook(text, card)) and not fresh
    return card_hook(text, card) or (is_followup(text) and not new_topic(text, card))
