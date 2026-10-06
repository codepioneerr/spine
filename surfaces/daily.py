"""
surfaces.daily — the daily brief and optional weekly review.

Target 120-200 words, at most three prioritised lines plus focus lines.
Empty or unchanged domains say nothing. System failures (RAM, model) are
never mixed into lifestyle content; at most one data-quality line, and only
when it changes what the reader can trust.

No model call: this brief cannot be skipped because RAM is short.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import health_facts as hf, property_facts as pf, quant_facts as qf
from surfaces import assistant as A, workouts
from surfaces.assistant import Reply
from surfaces.tg import esc

FOCUS_TOPICS = {
    "walking": ("Walking", False),
    "mobility": ("Mobility", False),
    "sleep consistency": ("Sleep consistency", False),
    "dorm strength": ("Dorm strength", False),
    "real-estate basics": ("Real-estate basics", False),
    "sexual health": ("Private focus", True),
}

_MOB = ["ankle_circles", "calf_raise", "single_leg_balance", "hip_9090", "hip_flexor",
        "hamstring_floss"]
_TERMS = ["deed", "mortgage", "bbl", "acris", "pluto", "grantor", "grantee", "assessed value",
          "recording date", "avm", "condo"]


def focus_line(topic: str, day_index: int) -> str:
    if topic == "mobility":
        mv = workouts.MOVES[_MOB[day_index % len(_MOB)]]
        return (f"🧘 Mobility idea: <b>{esc(mv.name)}</b>, {esc(mv.dose())}. "
                f"{esc(mv.how.split('.')[0])}.")
    if topic == "walking":
        return "🚶 Walking: one 20-minute brisk walk today (counts toward 150 min/week)."
    if topic == "sleep consistency":
        return "🌙 Sleep: keep tomorrow's wake time within 30 min of today's."
    if topic == "dorm strength":
        return "💪 Strength: /workout is 15 quiet minutes on the mat."
    if topic == "real-estate basics":
        t = _TERMS[day_index % len(_TERMS)]
        return f"📚 Term of the day: <b>{t}</b> — {esc(pf.glossary(t).split('.')[0])}."
    if FOCUS_TOPICS.get(topic, ("", False))[1]:
        return "🔒 Your private focus has an update: /focus"
    return f"• Focus: {esc(topic)}"


def health_line(now):
    try:
        conn = A.health_conn()
    except Exception:
        return None, "Health data unavailable on the Dell."
    cov = hf.coverage(conn, now)
    if not cov["last_import"]:
        return None, None
    if cov["import_age_h"] and cov["import_age_h"] > 26:
        return None, (f"Health data last synced {cov['import_age_h'] / 24:.0f} day(s) ago — "
                      "open Health Auto Export to sync.")
    full, _ = A._target_days(cov)
    if not full:
        return None, None
    f = hf.fact(conn, "step_count", full, cov["last_import"])
    if f.value is None:
        return None, None
    base = (f"; {f.b7.window}-day avg {f.b7.mean:,.0f} (n={f.b7.n})" if f.b7.ok
            else "; no baseline yet")
    first = " (first day on record; may be partial wear)" if full.isoformat() == cov["first_day"] else ""
    return f"❤️ Yesterday ({full.strftime('%a')}): {f.value:,.0f} steps{first}{base}.", None


def property_line(store, now):
    try:
        conn = A.spine_conn()
    except Exception:
        return None, None
    last = store.pref("last_brief_ts") or (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT data_json FROM items WHERE source='acris' AND kind='deal' "
                        "AND ts > ?", (last,)).fetchall()
    note = None
    newest = pf.feed_newest(conn)
    if newest and store.pref("stale_noted") != newest[:10]:
        age = (now.date() - datetime.strptime(newest[:10], "%Y-%m-%d").date()).days
        if age > 7:
            note = (f"NYC's property feed has published nothing recorded after {newest[:10]} "
                    "(city-side pause; our download is fine). Mentioned once.")
            store.set_pref("stale_noted", newest[:10])
    if not rows:
        return None, note
    import json
    types = [json.loads(r[0]).get("doc_type") for r in rows]
    deeds = sum(t in ("DEED", "DEEDO") for t in types)
    mtges = sum(t == "MTGE" for t in types)
    return (f"🏠 {len(rows)} new large recorded document(s): {deeds} deed(s) (ownership "
            f"transfers), {mtges} mortgage(s) (loans). /property"), note


def quant_line(now):
    try:
        conn = A.eventbot_conn()
        a = qf.accounting(conn)
        ch = qf.change_24h(conn, now)
    except Exception:
        return None
    if not a.get("available"):
        return None
    c = f", {A.sd(ch)} in 24 h" if ch is not None else ""
    return (f"📈 Simulation (no real money): {a['total_return_pct']:+.1f}% since "
            f"{a['since'][:10]}{c}. /quant")


def snapshot_times(doms, now) -> list[str]:
    """The same wording and source times the domain cards use (assistant.snap_*)."""
    out = []
    if "health" in doms:
        try:
            out.append(A.snap_health(hf.coverage(A.health_conn(), now)))
        except Exception:
            pass
    if "quant" in doms:
        try:
            out.append(A.snap_quant(qf.accounting(A.eventbot_conn())))
        except Exception:
            pass
    if "property" in doms:
        try:
            out.append(A.snap_property(A.spine_conn()))
        except Exception:
            pass
    return out or ["no data sources available"]


def build(store, now=None) -> Reply:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(hf.TZ)
    prefs = store.prefs()
    doms = set(prefs.get("domains") or [])
    lines, notes = [], []
    if "health" in doms:
        l, n = health_line(now)
        lines += [l] if l else []
        notes += [n] if n else []
    if "property" in doms:
        l, n = property_line(store, now)
        lines += [l] if l else []
        notes += [n] if n else []
    if "quant" in doms:
        l = quant_line(now)
        lines += [l] if l else []
    focus = [f for f in store.focuses() if f["status"] == "active"]
    flines = [focus_line(f["topic"], local.toordinal()) for f in focus]
    out = [f"<b>Today — {local.strftime('%a %b %d')}</b>"]
    out += lines[:3] or ["Nothing new worth your time from your data today."]
    if flines:
        out += [""] + flines[:3]
    if notes:
        out += ["", "<i>Data note: " + esc(notes[0]) + "</i>"]
    out.append(A.footer("; ".join(snapshot_times(doms, now))))
    store.set_pref("last_brief_ts", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return Reply("\n".join(out), domain="brief", ref={"kind": "daily", "day": local.date().isoformat()},
                 actions=[("Health", "open", "health"), ("Property", "open", "property"),
                          ("Quant", "open", "quant"), ("Workout", "workout", "strength15")])


def weekly(store, now=None) -> Reply:
    now = now or datetime.now(timezone.utc)
    rows = store.conn.execute("SELECT kind, COUNT(*) FROM feedback WHERE ts > ? GROUP BY kind",
                              ((now - timedelta(days=7)).timestamp(),)).fetchall()
    fb = {k: n for k, n in rows}
    lines = ["<b>Weekly review</b>",
             f"Workouts you marked done: {fb.get('done', 0)} (self-reported); skipped: {fb.get('skip', 0)}."]
    try:
        conn = A.health_conn()
        cov = hf.coverage(conn, now)
        lines.append(f"Health data: {cov['days']} day(s) on record; sleep records: {cov['sleep_records']}.")
    except Exception:
        pass
    focus = [f["topic"] for f in store.focuses() if f["status"] == "active"
             and not FOCUS_TOPICS.get(f["topic"], ("", False))[1]]
    lines.append("Next week's focus: " + (", ".join(focus) if focus else "none set — /focus"))
    return Reply("\n".join(lines), domain="brief", ref={"kind": "weekly"},
                 actions=[("Steps 7d", "trend", "step_count:7"), ("Focus", "open", "focus")])
