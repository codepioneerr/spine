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
    ("property", r"\b(property|deed|mortgage|acris|pluto|building|real estate|parcel)"),
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
guidelines compare minutes week weeks month time over this last show see insomnia bedtime""".split())
NOVEL_OK = {"sleep", "health", "activity", "improve", "trend", "unknown"}


def novel(text: str, intent: str) -> bool:
    if intent not in NOVEL_OK:
        return False
    from surfaces.research import keywords
    extra = [k for k in keywords(text, limit=6) if k not in COVERED]
    return bool(extra) or intent == "unknown"
