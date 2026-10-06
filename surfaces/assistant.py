"""
surfaces.assistant — what the Telegram front door says, as pure-ish functions.

Each handler reads source data READ-ONLY through typed queries
(core.health_facts, core.property_facts, core.quant_facts), and returns a
Reply. Numbers come only from those modules; wording is templates. No model
call is needed for anything here, so every answer still works when the local
model cannot load or no hosted provider is configured.

Three kinds of statement are kept visibly separate in health answers:
"Your data" (measured, with dates), "General evidence" (vetted sources in
surfaces.evidence), and "Interpretation" (ours, labelled).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core import bridge, health_facts as hf, property_facts as pf, quant_facts as qf
from core import market_filter, paths
from surfaces import charts, evidence, workouts
from surfaces.tg import esc


@dataclass
class Reply:
    text: str
    actions: list = field(default_factory=list)    # [(label, action, arg)]
    links: list = field(default_factory=list)      # [(label, url)]
    domain: str = "general"
    ref: dict = field(default_factory=dict)
    snapshot: dict | None = None
    photo: bytes | None = None
    sensitive: bool = False


# ── data access (read-only) ─────────────────────────────────────────

def _ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def health_conn():
    return _ro(os.environ.get("SPINE_HEALTH_DB") or paths.var("health.db"))


def spine_conn():
    return _ro(paths.db_path())


def eventbot_conn():
    return bridge.connect("eventbot")


def prediction_conn():
    return bridge.connect("prediction")


def _now():
    return datetime.now(timezone.utc)


def _et(dt):
    return dt.astimezone(hf.TZ).strftime("%a %b %d, %-I:%M %p ET") if dt else "never"


def snap_health(cov) -> str:
    return f"Health data as of the {_et(cov['last_import'])} export" if cov.get("last_import") \
        else "No health export yet"


def snap_property(conn) -> str:
    try:
        newest = pf.feed_newest(conn)
        r = conn.execute("SELECT MAX(updated_ts) FROM items WHERE source='acris'").fetchone()
        seen = datetime.fromisoformat(r[0].replace("Z", "+00:00")) if r and r[0] else None
    except Exception:
        return "Property data time unknown"
    return (f"Property records as of NYC's newest recording {str(newest)[:10]}; "
            f"last checked by us {_et(seen)}")


def snap_quant(a) -> str:
    try:
        t = datetime.fromisoformat(a["as_of"].replace("Z", "+00:00"))
        return f"Simulation as of {_et(t)}"
    except Exception:
        return "Simulation time unknown"


def et_iso(s) -> str:
    """'2026-10-06T16:05:06Z' -> 'Tue Oct 06, 12:05 PM ET' (same format as every footer)."""
    try:
        return _et(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except Exception:
        return str(s or "?")


def footer(text):
    return f"\n<i>🕒 {esc(text)}</i>"


def source_links(ids):
    return [(s["title"].split(" — ")[-1][:40], s["url"]) for s in evidence.cite(ids)]


def _sources(ids):
    return [(s["title"].split(" — ")[0][:28] + ": " + s["title"].split(" — ")[-1][:30], s["url"])
            for s in evidence.cite(ids)]


def sources_text(ids) -> str:
    out = []
    for s in evidence.cite(ids):
        upd = f", updated {s['updated']}" if s.get("updated") else ""
        out.append(f"• <a href=\"{esc(s['url'])}\">{esc(s['title'])}</a>{esc(upd)}; "
                   f"retrieved {evidence.RETRIEVED}.\n  Supports: {esc(s['claim'])}\n"
                   f"  Limits: {esc(s['limits'])}")
    return "\n".join(out)


# ── help / privacy ──────────────────────────────────────────────────

HELP = """<b>Dell assistant</b> (this bot). One front door to your own data.
Domains: <b>Health</b> (your Apple Health export), <b>Property</b> (NYC recorded documents), <b>Quant</b> (the trading <i>simulation</i>).

Ask in normal words, e.g. “Does caffeine at 4pm affect sleep?”. Short follow-ups (“how much is too much?”, “who was the lender?”) continue the last card; or reply to any card directly.
Research answers quote NIH MedlinePlus and PubMed reviews with numbered sources; no AI model writes them.

/brief – today's short overview
/health – your data, coverage and trends
/workout – quiet dorm-mat routine · /mobility – ankles, knees, hips
/property – recent recorded property documents
/quant – simulation summary
/focus – what to include in briefs · /settings – time, quiet hours
/profile – what I know about you (confirm or remove)
/glossary deed – plain-English terms
/sources · /status (operator view) · /privacy · /forget · /cancel

Hermes (@hermnick_bot) stays your general chat agent. This bot only reads your data and answers; it never trades, posts or deletes source data."""

PRIVACY = """<b>Privacy, plainly</b>
• Your health data stays in a local database on the Dell (var/health.db, owner-only).
• To answer you, a few computed numbers are sent to you through Telegram. Telegram bot chats are <b>not end-to-end encrypted</b>: Telegram's servers can see these messages.
• This bot uses no hosted AI model for your health data. Answers are computed on the Dell from fixed rules and vetted sources.
• Only your numeric Telegram account, in a private chat, is answered. Anyone else is ignored.
• Logs record timing and errors, not your messages or health values.
• For questions outside the vetted list, up to 3 topic keywords (e.g. “caffeine sleep”; never your numbers, profile or the full question) are sent to NIH's MedlinePlus and PubMed search services. Property follow-ups query NYC Open Data by document or lot number (public records). Turn the health lookups off with <code>/settings research off</code>.
• No AI model writes answers. Hermes's Gemini model is never used for your questions here.
• /forget deletes this assistant's local data (preferences, focus list, card history, saved questions). It does not delete your phone's Health data, the Dell's imported health.db, or messages already in Telegram (delete those in the app)."""


def help_reply():
    return Reply(HELP)


def privacy_reply():
    return Reply(PRIVACY, actions=[("Forget my assistant data…", "forget_ask", "")])


# ── health ──────────────────────────────────────────────────────────

KEY_METRICS = ["step_count", "apple_exercise_time", "active_energy",
               "resting_heart_rate", "heart_rate_variability", "blood_oxygen_saturation"]


def _target_days(cov):
    """(last complete day or None, today-partial day or None)."""
    if not cov["last_day"]:
        return None, None
    imp = cov["last_import"]
    last = datetime.fromisoformat(cov["last_day"]).date()
    if hf.is_complete(last, imp):
        return last, None
    prev = last - timedelta(days=1)
    return (prev if cov["days"] > 1 else None), last


def coverage_text(cov) -> str:
    if not cov["last_import"]:
        return "No health export has reached the Dell yet."
    age = cov["import_age_h"]
    stale = " — <b>over a day old; open Health Auto Export to sync</b>" if age and age > 26 else ""
    return f"Latest export: {_et(cov['last_import'])} ({age:.0f} h ago){stale}"


def _day_status(d, cov):
    """Finished-day status and recording coverage are different facts.
    'Finished' = the calendar day ended and an export arrived afterwards.
    Coverage (how much of the day the watch was worn/recording) is not in a
    daily-aggregate export, so it is reported as unknown, never assumed."""
    bits = ["day finished and exported after midnight"]
    if d.isoformat() == cov["first_day"]:
        bits.append("first day on record, so it may start partway through the day")
    bits.append("wear/recording coverage unknown (export has daily totals only)")
    return "; ".join(bits)


SHORT = [("step_count", "steps"), ("resting_heart_rate", "resting HR"),
         ("heart_rate_variability", "HRV")]


def _short_line(conn, d, imp):
    parts = []
    for m, name in SHORT:
        f = hf.fact(conn, m, d, imp)
        if f.value is not None:
            parts.append(f"{name} {hf.fmt(f.value, '' if m == 'step_count' else f.unit)}")
    return " · ".join(parts)


def health_summary(conn=None, now=None) -> Reply:
    """Short card. Full measurements live behind 'Explain numbers'."""
    conn = conn or health_conn()
    now = now or _now()
    cov = hf.coverage(conn, now)
    full, partial = _target_days(cov)
    imp = cov["last_import"]
    lines = ["<b>Health</b>", coverage_text(cov)]
    if full:
        lines.append(f"<b>{full.strftime('%a %b %d')}</b> (finished day): {esc(_short_line(conn, full, imp))}")
    if partial:
        lines.append(f"<b>{partial.strftime('%a %b %d')}</b>, as of the {_et(imp)} export (not live): "
                     f"{esc(_short_line(conn, partial, imp))} so far")
    if not full and not partial:
        lines.append("No measurements to show.")
    gaps = []
    if cov["days"] < 7:
        gaps.append(f"{cov['days']} day(s) of history: no baseline yet")
    if not cov["sleep_records"]:
        gaps.append("no sleep data")
    if not cov["workouts"]:
        gaps.append("no workouts")
    if gaps:
        lines.append("<i>" + esc("; ".join(gaps)) + ".</i>")
    snap = {"coverage": {k: (str(v) if k == "last_import" else v) for k, v in cov.items()},
            "full": full.isoformat() if full else None,
            "partial": partial.isoformat() if partial else None}
    lines.append(footer(snap_health(cov)))
    return Reply("\n".join(lines), domain="health", ref={"kind": "summary"}, snapshot=snap,
                 actions=[("Explain numbers", "explain_health", ""),
                          ("Steps 7d", "trend", "step_count:7"),
                          ("Two things to improve", "improve", ""),
                          ("Sync help", "sync_help", "")])


EXPLAIN_HEALTH = """<b>What these numbers mean</b>
• <b>Steps / exercise minutes / active energy</b> are totals for a New York calendar day. Before the day ends, or before the next export, they are running totals: a low number in the morning is not a low day.
• <b>Active energy</b> is Apple's estimate from motion and heart rate, not a direct measurement.
• <b>Resting heart rate</b> is Apple's daily estimate.
• <b>HRV</b> on Apple Watch is <b>SDNN</b> (ms) from short readings at irregular times; shown as that day's average. Not comparable with RMSSD from other devices.
• <b>Blood oxygen</b> is a wellness spot reading, not a medical oximeter.
• A <b>baseline</b> appears only with ≥5 complete prior days (7-day) or ≥14 (30-day); the count n is shown.
<i>Interpretation, not from a cited source: single-day HRV and resting-HR changes are noisy; only multi-week trends of your own numbers are worth reading.</i>"""


def explain_health(conn=None, now=None) -> Reply:
    """Every stored measurement for the days on the card, with status, then meanings."""
    try:
        conn = conn or health_conn()
    except Exception:
        return Reply(EXPLAIN_HEALTH, domain="health")
    now = now or _now()
    cov = hf.coverage(conn, now)
    full, partial = _target_days(cov)
    imp = cov["last_import"]
    lines = ["<b>All measurements</b>", coverage_text(cov),
             f"History: {cov['days']} day(s), {cov['first_day']} → {cov['last_day']}; "
             f"devices: {esc(', '.join(cov['devices']) or 'unknown')}", ""]
    for d, head in ((full, "finished day"), (partial, f"as of the {_et(imp)} export")):
        if not d:
            continue
        lines.append(f"<b>{d.strftime('%a %b %d')}</b> ({esc(head)})")
        if d == full:
            lines.append("<i>" + esc(_day_status(d, cov)) + "</i>")
        for m in KEY_METRICS:
            f = hf.fact(conn, m, d, imp)
            if f.value is not None:
                lines.append("• " + esc(hf.describe(f, True)))
        lines.append("")
    return Reply("\n".join(lines) + EXPLAIN_HEALTH + footer(snap_health(cov)), domain="health",
                 ref={"kind": "explain"})


SYNC_HELP = """<b>Turn on sleep and workout exports</b>
Data reaches the Dell only when the iPhone app <b>Health Auto Export</b> sends it (over Tailscale). Nothing is live. Labels below are from the app's help pages (updated Aug 23, 2026); your app version may word them slightly differently.

<b>Sleep</b>
1. Set a sleep schedule (Health app → Browse → Sleep) and turn on <b>Track Sleep with Apple Watch</b> (Watch app on iPhone → Sleep). Wear the watch to bed.
2. Health Auto Export → <b>Automations</b> → your REST API automation (Data Type: <b>Health Metrics</b>) → <b>Select Health Metrics</b> → add <b>Sleep Analysis</b>. Keep the metrics you already send.
3. Keep <b>Summarize Data</b> ON with <b>Time Grouping</b> = Day (that is how your current export is set).

<b>Workouts</b> (one data type per automation)
4. <b>New Automation</b> → REST API → Data Type <b>Workouts</b>. Use the same URL and the same <b>Authorization</b> header (Add Headers) as the Health Metrics one. Export Version 2 is fine (the Dell reads v1 and v2).
5. Record workouts on the watch (Workout app → e.g. Kickboxing, Functional Strength Training).

<b>Check it worked</b>
6. In each automation tap <b>Manual Export</b> for the last 7 days.
7. Send /health here. “Latest export” should show that time, and “no sleep data” / “no workouts” should disappear. /status shows counts from the Dell's side. If it doesn't change within a minute, tell me: I can check the receiver log for the request."""


def sync_help() -> Reply:
    return Reply(SYNC_HELP, domain="health")


METRIC_NAMES = {"steps": "step_count", "step": "step_count", "hrv": "heart_rate_variability",
                "resting": "resting_heart_rate", "heart": "resting_heart_rate",
                "exercise": "apple_exercise_time", "energy": "active_energy",
                "calories": "active_energy", "oxygen": "blood_oxygen_saturation"}


def trend(metric, days, conn=None, now=None) -> Reply:
    conn = conn or health_conn()
    now = now or _now()
    end = hf.local_day(now)
    pts = hf.series_for_chart(conn, metric, end, days)
    label, unit, agg, _ = hf.METRICS.get(metric, (metric, "", "mean", ""))
    have = [p for p in pts if p[1] is not None]
    gaps = len(pts) - len(have)
    partial = sum(1 for p in have if not p[2])
    png = charts.bar_chart(pts, zero_based=(agg == "sum")) if have else None
    cap = (f"<b>{esc(label)}</b>{'' if unit.lower() in label.lower() else f' ({esc(unit)})'}, {pts[0][0]} → {pts[-1][0]}, New York days.\n"
           f"{len(have)} day(s) with data, {gaps} with none (gray ×, not zero)"
           + (f", {partial} partial (light bar)" if partial else "") + ".")
    if not have:
        cap += "\nNothing to chart yet."
    cap += footer(snap_health(hf.coverage(conn, now)))
    other = 30 if days == 7 else 7
    return Reply(cap, photo=png, domain="health", ref={"kind": "trend", "metric": metric,
                                                       "days": days},
                 snapshot={"points": pts},
                 actions=[(f"{other} days", "trend", f"{metric}:{other}"),
                          ("Resting HR", "trend", f"resting_heart_rate:{days}"),
                          ("HRV", "trend", f"heart_rate_variability:{days}")])


def _week_sum(conn, metric, end, imp):
    s = hf.day_values(conn, metric, end - timedelta(days=6), end, imp)
    vals = [dv.value for dv in s.values() if dv.value is not None]
    return (sum(vals) if vals else None), len(vals), sum(1 for dv in s.values() if dv.complete)


def improve(conn=None, now=None, store=None) -> Reply:
    """The two most useful things this week, chosen by fixed rules from data."""
    conn = conn or health_conn()
    now = now or _now()
    cov = hf.coverage(conn, now)
    end = hf.local_day(now)
    imp = cov["last_import"]
    picks, facts = [], []
    ex, n_ex, n_full = _week_sum(conn, "apple_exercise_time", end, imp)
    steps = hf.day_values(conn, "step_count", end - timedelta(days=6), end, imp)
    if ex is not None:
        facts.append(f"Exercise minutes recorded over the last 7 days: {ex:.0f} min across "
                     f"{n_ex} day(s) with data ({n_full} complete).")
    if steps:
        facts.append("Steps by day: " + ", ".join(
            f"{d[5:]} {dv.value:,.0f}{'' if dv.complete else ' (partial)'}"
            for d, dv in sorted(steps.items())))
    if cov["days"] < 7 or not cov["sleep_records"]:
        picks.append("<b>Make the data usable.</b> Turn on Sleep Analysis in Health Auto Export and "
                     "let it sync daily for a week. Without that, I can't compare your days or say "
                     "anything about sleep. (/health → Refresh / sync help)")
    picks.append("<b>Move toward 150 minutes a week.</b> CDC guidance for adults is 150 min of "
                 "moderate activity a week. A practical start: a 20-minute brisk walk on 5 days. "
                 + ("Your recorded exercise minutes so far are below that pace."
                    if ex is not None and n_full and ex < 150 * max(1, n_ex) / 7 else
                    "With so few days recorded, treat this as general guidance, not a verdict."))
    picks.append("<b>Two short strength sessions.</b> CDC also recommends muscle-strengthening on "
                  "2 days a week. Your watch can't see this; use /workout and tap Done so it's tracked.")
    text = ["<b>Two things worth doing this week</b>", ""]
    text += [f"{i}. {p}" for i, p in enumerate(picks[:2], 1)]
    text += ["", "<b>Your data</b>"] + [esc(f) for f in facts or ["No activity data yet."]]
    text += ["", "<i>General guidance applied to limited data, not a personal assessment.</i>"]
    return Reply("\n".join(text), domain="health", ref={"kind": "improve"},
                 actions=[("Sources", "sources", "activity"), ("Dorm workout", "workout", "strength15"),
                          ("Add walking to brief", "focus_propose", "walking")])


def activity_vs_guidance(conn=None, now=None) -> Reply:
    conn = conn or health_conn()
    now = now or _now()
    end = hf.local_day(now)
    imp = hf.last_import(conn)
    ex, n, n_full = _week_sum(conn, "apple_exercise_time", end, imp)
    lines = ["<b>Your activity vs. general guidance</b>",
             "<b>General evidence:</b> 150 min/week moderate activity (or 75 vigorous) plus "
             "2 days of muscle-strengthening (CDC).", ""]
    if ex is None:
        lines.append("<b>Your data:</b> no exercise-minute records in the last 7 days.")
    else:
        lines.append(f"<b>Your data:</b> {ex:.0f} exercise minutes recorded in the last 7 days, "
                     f"from {n} day(s) with data ({n_full} complete). Days with no data are not "
                     "counted as zero; with fewer than 7 days the weekly total is incomplete.")
    lines += ["", "<b>Interpretation:</b> Apple's exercise minutes count brisk movement it detects. "
              "Boxing/Muay Thai sessions only count if the watch was worn. Strength work isn't measured."]
    return Reply("\n".join(lines), domain="health", ref={"kind": "activity"},
                 actions=[("Sources", "sources", "activity"), ("Steps 7d", "trend", "step_count:7")])


def sleep_answer(conn=None, now=None) -> Reply:
    conn = conn or health_conn()
    cov = hf.coverage(conn, now or _now())
    if not cov["sleep_records"]:
        body = ("<b>Your data:</b> no sleep records have reached the Dell, so I can't say how your "
                "sleep changed. Enable <i>Sleep Analysis</i> in Health Auto Export (and wear the "
                "watch overnight) and this will start working after a few nights.")
    else:
        body = (f"<b>Your data:</b> {cov['sleep_records']} sleep record(s) stored. "
                "Weekly comparisons need at least 5 nights.")
    text = ("<b>Sleep</b>\n" + body + "\n\n<b>General evidence:</b> adults 18–60 should get 7 or "
            "more hours a night (CDC).\n<i>Interpretation, not from a cited source: in a dorm, a fixed wake "
            "time and earplugs/eye mask are the easiest things to control.</i>")
    return Reply(text, domain="health", ref={"kind": "sleep"},
                 actions=[("Sources", "sources", "sleep"), ("Sync help", "sync_help", ""),
                          ("Add sleep to brief", "focus_propose", "sleep consistency")])


def sexual_health() -> Reply:
    text = """<b>Sexual health — what your data can and can't tell</b>
<b>Can't tell:</b> your watch data cannot measure hormones, sexual function, fertility or STI status. I won't infer any of those from steps or heart rate.

<b>Can loosely reflect</b> <i>(interpretation)</i>: lifestyle factors the sources below link to sexual health, namely activity level and (once exported) sleep.

<b>General evidence (NIDDK):</b> inactivity, smoking, heavy drinking, drug use and blood-vessel problems such as high blood pressure are linked to erectile problems; heart-healthy habits support blood-vessel health.

<b>Practical, for anyone:</b>
• Regular activity (CDC: 150 min/week) and sleep (CDC: 7+ hours for adults).
• Avoid smoking and heavy drinking (NIDDK lists both as factors).
• STI testing on a schedule that fits your situation: CDC lists schedules by group, and a campus health center can tell you which applies.
• Kegels/pelvic-floor routines aren't a default for everyone; NIDDK says to check with a clinician first.

<b>See a clinician</b> if you notice a persistent change in sexual function; NIDDK notes erectile problems can be a sign of another health problem. Any symptom that worries you, such as pain or sores, is also worth a campus-health visit.

This topic won't appear in your daily briefs unless you add it yourself."""
    return Reply(text, domain="health", ref={"kind": "sexual"}, sensitive=True,
                 actions=[("Sources", "sources", "sexual"),
                          ("Pelvic floor: evidence", "sources", "pelvic")])


def mobility(confirmed=None) -> Reply:
    intro = ("<b>Comfortable walking + ankle, knee and hip mobility</b>\n"
             "I read “goota” as <b>GOATA</b>, the gait/movement coaching system. If you meant "
             "something else, tell me.\n\n"
             "<b>Evidence check:</b> GOATA is a commercial method; I found no peer-reviewed "
             "controlled trials of GOATA itself (searched Oct 6, 2026). General strength, balance "
             "and mobility work has much better support. There's no single correct foot angle or "
             "gait for everyone, and I found no evidence that any routine “realigns” bones.\n\n")
    plan = workouts.render("mobility10", workouts.profile_notes(confirmed or {}, "mobility10"))
    return Reply(intro + plan, domain="health", ref={"kind": "workout", "plan": "mobility10"},
                 actions=[("Done", "fb", "done"), ("Too easy", "fb", "easy"),
                          ("Too hard", "fb", "hard"), ("Skip", "fb", "skip"),
                          ("Sources", "sources", "mobility"),
                          ("Add mobility to brief", "focus_propose", "mobility")])


def workout(plan_id="strength15", confirmed=None, minutes=None) -> Reply:
    text = workouts.render(plan_id, workouts.profile_notes(confirmed or {}, plan_id), minutes)
    ref = {"kind": "workout", "plan": plan_id}
    if minutes:
        ref["minutes"] = minutes
    return Reply(text, domain="health", ref=ref,
                 actions=[("Done", "fb", "done"), ("Too easy", "fb", "easy"),
                          ("Too hard", "fb", "hard"), ("Skip", "fb", "skip"),
                          ("Shorter version", "w_short", plan_id), ("My profile", "profile", "")])


def load_profile():
    p = os.environ.get("SPINE_HEALTH_PROFILE") or paths.var("health_profile.md")
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# ── property ────────────────────────────────────────────────────────

def property_items(conn, limit=3):
    rows = conn.execute(
        "SELECT * FROM items WHERE source='acris' AND kind='deal' AND status IN ('new','seen') "
        "ORDER BY importance DESC, ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        it = pf.load_item(conn, r["id"])
        out.append(it)
    return out


def property_card(item, conn=None, now=None) -> Reply:
    conn = conn or spine_conn()
    doc = pf.load_doc(conn, (item.get("data") or {}).get("document_id") or item["key"])
    f = pf.card_facts(item, doc, now=now, feed_newest=pf.feed_newest(conn))
    b = f["building"]
    where = ", ".join(x for x in (f["address"], f["borough"]) if x) or "address not recorded"
    lines = [f"🏠 <b>{esc(f['label'])}</b>", esc(where), "",
             f"<b>What happened:</b> {esc(f['what'])}",
             f"<b>Amount:</b> {pf.money(f['amount'])} — {esc(f['amount_means'])}",
             f"<b>Dates:</b> " + "; ".join(x for x in (
                 f"signed {f['document_date']}" if f["document_date"] else "",
                 f"recorded {f['recorded']}" if f["recorded"] else "",
                 f"we first saw it {str(f['first_seen'])[:10]}" if f["first_seen"] else "") if x)]
    if b:
        bits = []
        if b.get("unitsres"):
            bits.append(f"{b['unitsres']} apartments")
        if b.get("yearbuilt"):
            bits.append(f"built {b['yearbuilt']}")
        if b.get("bldgclass"):
            bits.append(f"building class {b['bldgclass']}")
        if bits:
            lines.append("<b>Building (PLUTO):</b> " + esc(", ".join(bits)))
    lines.append(f"<b>Why you got this:</b> amount above the $5M brief threshold; priority "
                 f"{f['importance']} ranks by dollar size only (not a probability or a deal rating).")
    for c in f["caveats"]:
        lines.append("⚠️ " + esc(c))
    lines.append("<b>Unknown:</b> " + esc("; ".join(f["unknown"])) + ".")
    lines.append(footer(snap_property(conn)))
    links = [("Official ACRIS record", f["acris_url"])] if f["acris_url"] else []
    if f["zola_url"]:
        links.append(("Lot on ZoLa map", f["zola_url"]))
    return Reply("\n".join(lines), domain="property",
                 ref={"kind": "acris_item", "item_id": item["id"], "doc_id": f["doc_id"]},
                 snapshot=f, links=links,
                 actions=[("Explain this", "p_explain", ""), ("Who are the parties?", "p_parties", ""),
                          ("Lot history", "p_history", ""), ("Building facts", "p_building", ""),
                          ("Score?", "p_score", ""), ("Save", "p_save", ""), ("Dismiss", "p_dismiss", "")])


def property_explain(f: dict) -> Reply:
    dt = f["doc_type"]
    if dt == "MTGE":
        core_ = ("This record is a <b>mortgage</b>: the borrower (mortgagor) recorded a loan from a "
                 "lender (mortgagee), with the property pledged as security. "
                 f"The {pf.money(f['amount'])} is the loan amount stated on the document. It is not a "
                 "price or a valuation. A mortgage is not a transfer document, so this record alone "
                 "doesn't tell us whether ownership changed; a sale would be a separate deed.")
    elif dt in ("DEED", "DEEDO"):
        core_ = ("This record is a <b>deed</b>: ownership passed from the grantor (seller) to the "
                 f"grantee (buyer). The {pf.money(f['amount'])} is the stated consideration, usually "
                 "the price. It shows a past transfer, not that the property is for sale now, and "
                 "it doesn't prove the title is clean.")
    else:
        core_ = esc(f["what"])
    rec = f.get("recorded")
    lag = (f"\n\n<b>Timing:</b> recorded {rec}. The city's open-data feed's newest recording is "
           f"{f.get('feed_newest') or 'unknown'}: NYC paused publishing after Aug 31 (dataset last "
           "updated Sep 8), so newer filings exist that we cannot see yet. Our download is current.")
    return Reply(core_ + lag + "\n\nTerms: /glossary deed · /glossary mortgage · /glossary bbl",
                 domain="property")


def property_saleloan(f: dict) -> Reply:
    dt = f["doc_type"]
    if dt == "MTGE":
        t = ("<b>A loan record.</b> MTGE = mortgage: financing secured by the property. This document "
             "doesn't transfer ownership; whether a sale also happened would show up as a separate deed.")
    elif dt == "DEED":
        t = ("<b>An ownership transfer</b> (a deed). Usually a sale; nominal amounts, related-party "
             "transfers and partial interests are exceptions we can't rule out from this record alone.")
    elif dt == "DEEDO":
        t = "<b>A non-standard transfer</b> (deed, other). Often not a market sale."
    else:
        t = f"<b>Neither clearly:</b> {esc(f['label'])}."
    return Reply(t, domain="property")


def property_building(f: dict) -> Reply:
    b = f["building"]
    if not b:
        return Reply("No PLUTO facts are stored for this lot (BBL "
                     f"{esc(f.get('bbl') or '?')}). Open the ZoLa link on the card for the city's map.",
                     domain="property")
    names = {"unitsres": "residential units", "unitstotal": "total units", "yearbuilt": "year built",
             "bldgarea": "building floor area (sq ft)", "bldgclass": "building class",
             "assesstot": "assessed total value (tax figure, not market value)",
             "ownername": "owner of record in PLUTO (may predate this filing)"}
    lines = [f"<b>What we know about this lot</b> (BBL {esc(f.get('bbl'))}, PLUTO)"]
    for k, v in b.items():
        vv = f"${v:,.0f}" if k == "assesstot" else (f"{v:,.0f}" if isinstance(v, float) else v)
        lines.append(f"• {names.get(k, k)}: {esc(vv)}")
    return Reply("\n".join(lines), domain="property")


def acris_code_reply(term: str):
    """Answer 'what is a <document type>' from the official ACRIS code table."""
    from core import property_live as L
    t = (term or "").strip().lower()
    for code, (desc, p1, p2) in L.CODES.items():
        if t and (t == code.lower() or t == (desc or "").lower()):
            generic = {"party 1", "party one", "party 2", "party two", ""}
            roles = (f" Party 1 is the {p1.lower()}; party 2 is the {p2.lower()}."
                     if (p1 or "").lower() not in generic and (p2 or "").lower() not in generic else
                     " Its parties are just labelled “party 1” and “party 2”.")
            return Reply(f"<b>{esc(desc.capitalize())}</b> (ACRIS code {esc(code)})\n"
                         f"This is one of NYC's official categories for a recorded document.{esc(roles)} "
                         "The category name is all the index says: what the document actually covers is "
                         "only in the document image (the “Official ACRIS record” link on a card).",
                         domain="property", links=[("ACRIS document codes (official)",
                                                    "https://data.cityofnewyork.us/d/7isb-wh4c")])
    return None


def glossary_reply(term: str) -> Reply:
    code = acris_code_reply(term)
    if code and not pf.glossary(term or ""):
        return code
    t = pf.glossary(term or "")
    if not t:
        return Reply("Terms I can explain: " + ", ".join(sorted(pf.GLOSSARY)) +
                     "\nTry /glossary deed", domain="property")
    return Reply(f"<b>{esc(term.strip().capitalize())}</b>\n{esc(t)}", domain="property",
                 actions=[("Sources", "sources", "property")])


def market_moves(now=None, limit=3) -> list[dict]:
    """Live (not expired/settling) market signals from the item store."""
    now = now or _now()
    conn = spine_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, title, data_json, ts FROM items WHERE source='polymarket' AND kind='signal' "
        "AND status IN ('new','seen') ORDER BY importance DESC, ts DESC LIMIT 50")]
    import json as _j
    try:
        ends = market_filter.end_dates(prediction_conn(),
                                       [_j.loads(r["data_json"]).get("market_id") for r in rows])
    except Exception:
        ends = {}
    out = []
    for r in rows:
        d = _j.loads(r["data_json"] or "{}")
        st = market_filter.status(ends.get(str(d.get("market_id"))) or d.get("end_date"),
                                  d.get("jumped_at"), now, d.get("new_price"))
        if st in ("live",):
            out.append(dict(r, state=st))
    return out[:limit]


def sd(v) -> str:
    """Signed dollars: +$1,234.50 / -$9.08."""
    return ("-" if v < 0 else "+") + f"${abs(v):,.2f}"


# ── quant ───────────────────────────────────────────────────────────

def quant_summary(conn=None, now=None) -> Reply:
    conn = conn or eventbot_conn()
    a = qf.accounting(conn)
    mode = qf.mode(conn)
    if not a.get("available"):
        return Reply("No simulation equity has been recorded yet.", domain="quant")
    ch = qf.change_24h(conn, now)
    lines = [f"📈 <b>Quant — {esc(mode.upper())}</b> (no broker, no real money)" if mode == "simulation"
             else f"📈 <b>Quant — {esc(mode)}</b>",
             f"Simulated account since {a['since'][:10]}: start ${a['start']:,.2f} → equity "
             f"${a['equity']:,.2f} ({a['total_return_pct']:+.2f}%).",
             f"= realized {sd(a['realized'])} ({a['n_closed']} closed) "
             f"+ unrealized {sd(a['unrealized'])} ({a['n_open']} open)"
             + (f" {sd(a['residual'])} unexplained cash difference" if abs(a['residual']) >= 0.01 else ""),
             ]
    if ch is not None:
        lines.append(f"Last 24 h: {sd(ch)} equity change.")
    lines.append("<i>Simulation limits: fills at the last quoted price; fees, spreads and slippage "
                 "are not modelled. Treat this as a test of the rules, not an estimate of live results.</i>")
    lines.append(footer(snap_quant(a)))
    return Reply("\n".join(lines), domain="quant", ref={"kind": "summary"}, snapshot=a,
                 actions=[("Open positions", "q_positions", ""), ("What changed (24 h)", "q_changes", ""),
                          ("How return is calculated", "q_calc", ""),
                          ("Risks & missing assumptions", "q_risks", "")])


def quant_calc(a: dict) -> Reply:
    t = (f"<b>How the numbers are calculated</b>\n"
         f"• Start = ${a['start']:,.2f} (simulated cash on {a['since'][:10]}).\n"
         f"• Equity = cash ${a['cash']:,.2f} + open positions marked at last price ${a['open_value']:,.2f} = ${a['equity']:,.2f}.\n"
         f"• Total return = (equity − start) / start = {a['total_return_pct']:+.2f}%. No deposits or withdrawals exist in a simulation, so no cash-flow adjustment is needed.\n"
         f"• Realized = sum of P&L on closed positions = {sd(a['realized'])}.\n"
         f"• Unrealized = current value − cost of open positions = ${a['open_value']:,.2f} − ${a['open_cost']:,.2f} = {sd(a['unrealized'])}.\n"
         f"• Check: start + realized − open cost should equal cash; the difference is {sd(a['residual'])} "
         + ("(reconciles)." if abs(a['residual']) < 0.01 else "(small, unresolved: probably rounding in the simulator; flagged, not hidden).")
         + "\nThe daily eventbot report's “24h” figure is equity now minus equity 24 h ago, a different period from the all-time return.")
    return Reply(t, domain="quant")


QUANT_RISKS = """<b>Risks and missing assumptions</b>
• <b>Costs not modelled:</b> the simulator fills at the last price with no fees, spreads or slippage. These are limitations of the simulation; its results are not an estimate of live performance.
• <b>Short history:</b> a few weeks of simulation says little about future results.
• <b>Rule triggers are keyword matches</b> on headlines or market moves. A headline is evidence that something was reported, not that it is true or that it moved prices.
• <b>Expiry artefacts:</b> some crypto entries were triggered by short-dated prediction markets dropping to ~1% at their end time — that's a market settling, not news. Flagged for review in eventbot's rules.
• Nothing here places orders; switching to paper or live trading is outside this bot."""


def quant_positions(conn=None) -> list[Reply]:
    conn = conn or eventbot_conn()
    out = []
    for p in qf.open_positions(conn)[:6]:
        t = (f"<b>{esc(p['asset'])}</b> simulated long, ${p['notional']:,.0f} at {p['entry_price']:,.2f} "
             f"({et_iso(p['entry_ts'])})\nRule: {esc(p['rule'])}"
             + (f"; stop {p['stop']:,.2f}, target {p['target']:,.2f}" if p["stop"] and p["target"] else "")
             + (f"; closes by {et_iso(p['max_exit_ts'])}" if p["max_exit_ts"] else ""))
        out.append(Reply(t, domain="quant", ref={"kind": "position", "position_id": p["id"]},
                         snapshot=p, actions=[("Why opened?", "q_why", ""),
                                              ("How has this rule done?", "q_rule", p["rule"])]))
    if not out:
        out.append(Reply("No open simulated positions.", domain="quant"))
    return out


def quant_why(position_id: int, conn=None) -> Reply:
    conn = conn or eventbot_conn()
    w = qf.why_opened(conn, position_id)
    if not w["found"]:
        return Reply(f"No record of position {position_id}.", domain="quant")
    p = w["position"]
    lines = [f"<b>Why simulated position {p['id']} ({esc(p['asset'])}) was opened</b>",
             f"<b>Observed (decision record):</b> rule <code>{esc(p['rule'])}</code> fired"]
    if w["signal"]:
        s = w["signal"]
        lines.append(f"on a {esc(s['source'])} signal seen {esc(et_iso(s['seen_ts'] or s['ts']))}:")
        lines.append(f"“{esc((s['text'] or '')[:220])}”")
    if w["rule_meaning"]:
        lines.append(f"<b>Rule hypothesis:</b> {esc(w['rule_meaning'])}.")
    if w.get("idea"):
        i = w["idea"]
        lines.append(f"Idea #{i['id']}: sentiment {i['sentiment']}, weight {i['weight']}, "
                     f"price source {esc(i['price_source'])}.")
    if w["missing"]:
        lines.append("<b>Missing evidence:</b> " + esc("; ".join(w["missing"])) + ".")
    if w.get("warning"):
        lines.append("⚠️ " + esc(w["warning"]))
    lines.append("<i>A headline shows that something was reported, not that it's true or that it moved the price.</i>")
    links = [("Signal source", w["signal"]["url"])] if w["signal"] and w["signal"].get("url") else []
    return Reply("\n".join(lines), domain="quant", links=links,
                 ref={"kind": "position", "position_id": p["id"]}, snapshot={"rule": p["rule"], "id": p["id"]},
                 actions=[("How has this rule done?", "q_rule", p["rule"])])


# ── researched answers (novel questions) ──────────────────────────────

PERSONAL_HOOKS = [
    (("sleep", "insomnia", "caffeine", "nap", "tired", "melatonin"), "sleep"),
    (("sit", "sitting", "inactive", "walk", "walking", "steps", "exercise", "exercises", "activity",
      "protein", "creatine", "muscle", "cardio", "stretching", "strength"), "activity"),
    (("heart", "pulse", "hrv", "blood", "pressure", "stress", "anxiety"), "heart"),
]

# What would be needed to make a general answer personal, by topic word.
MISSING = [
    (("caffeine", "coffee", "energy"), "how much caffeine you have and when (not tracked anywhere)"),
    (("protein", "creatine", "supplement", "supplements", "vitamin", "diet"),
     "your diet and supplement use (not tracked), and any medicines or conditions"),
    (("knee", "ankle", "hip", "back", "pain", "injury", "joint", "joints"),
     "whether there is pain, swelling or a past injury (tell me, or see a clinician if there is)"),
    (("blood", "pressure"), "blood-pressure readings (none in your export)"),
    (("sleep", "insomnia", "melatonin", "nap"), "sleep records (enable Sleep Analysis; /health → Sync help)"),
]


def _personal_lines(kws, conn, now):
    want = {tag for words, tag in PERSONAL_HOOKS for k in kws if k in words}
    if not want:
        return []
    cov = hf.coverage(conn, now)
    full, _ = _target_days(cov)
    out = []
    if "sleep" in want:
        out.append("Sleep: no sleep records exported yet, so I can't relate this to your nights."
                   if not cov["sleep_records"] else f"Sleep records stored: {cov['sleep_records']}.")
    if full and "activity" in want:
        for m in ("step_count", "apple_exercise_time"):
            f = hf.fact(conn, m, full, cov["last_import"])
            if f.value is not None:
                out.append(f"{full.strftime('%a %b %d')}: " + hf.describe(f, cov["days"] >= 7))
    if full and "heart" in want:
        for m in ("resting_heart_rate", "heart_rate_variability"):
            f = hf.fact(conn, m, full, cov["last_import"])
            if f.value is not None:
                out.append(f"{full.strftime('%a %b %d')}: " + hf.describe(f, cov["days"] >= 7))
    if out and cov["days"] < 7:
        out.append(f"Only {cov['days']} day(s) of history, so this can't show a personal pattern.")
    if out:
        out.append(snap_health(cov))
    return out


def _missing(kws):
    return [txt for words, txt in MISSING if any(k in words for k in kws)][:2]


ASPECT_WORDS = {"dose": "how much", "safety": "safety", "timing": "timing", "efficacy": "whether it works"}


def research_answer(question, store_conn=None, online=True, now=None, opener=None,
                    kws=None, asp=None, context=None) -> Reply:
    from surfaces import research
    now = now or _now()
    r = research.lookup(question, store_conn, online=online, opener=opener, now=now.timestamp(),
                        kws=kws, asp=asp)
    kws = r["keywords"]
    try:
        mine = _personal_lines(kws, health_conn(), now)
    except Exception:
        mine = []
    ref = {"kind": "research", "status": r.get("status"), "keywords": kws, "aspect": r.get("aspect")}
    if r.get("status") != "ok":
        why = {"none": "I couldn't find a matching source (NIH MedlinePlus or a PubMed review) for",
               "offline": "The research sources couldn't be reached right now, and nothing is cached for",
               "disabled": "Online research is off (/settings research on), and nothing is cached for"
               }.get(r.get("status"), "I have nothing vetted on")
        text = (f"{why} “{esc(' '.join(kws) or question[:40])}”, so I won't guess. "
                "I saved the question locally for follow-up research.")
        if mine:
            text += "\n\n<b>Your data that might matter:</b>\n" + "\n".join("• " + esc(x) for x in mine)
        return Reply(text, domain="research", ref=ref)
    focus = f" — focusing on {ASPECT_WORDS[r['aspect']]}" if r.get("aspect") else ""
    ctx = f" (follow-up on “{esc(context)}”)" if context else ""
    broad = ""
    if r.get("broadened_from"):
        broad = (f"\n<i>Nothing matched “{esc(' '.join(r['broadened_from']))}” together, so I broadened "
                 f"the search to “{esc(' '.join(kws))}”. The sources below are about that, not the "
                 f"exact combination.</i>")
    lines = [f"<b>Researched answer: {esc(' + '.join(kws))}</b>{focus}{ctx}{broad}",
             "<b>What the sources say</b> <i>(quoted; [n] = source)</i>"]
    lines += [f"• “{esc(p['text'])}” [{p['n']}]" for p in r["points"]]
    if r.get("gap"):
        lines.append(f"<b>Gap:</b> none of the retrieved sources directly addresses "
                     f"{ASPECT_WORDS.get(r['gap'], r['gap'])}. I won't fill that in.")
    if r["uncertainty"]:
        lines.append("<b>Uncertainty the sources state</b>")
        lines += [f"• “{esc(u['text'])}” [{u['n']}]" for u in r["uncertainty"]]
    pops = [(i, s["population"]) for i, s in enumerate(r["sources"], 1) if s.get("population")]
    if pops:
        lines.append("<b>Who was studied:</b> " + "; ".join(f"[{i}] {esc(p)}" for i, p in pops)
                     + ". Results may not apply to a healthy college student.")
    if mine:
        lines += ["<b>Your data</b>"] + ["• " + esc(x) for x in mine]
    miss = _missing(kws)
    if miss:
        lines.append("<b>Missing to personalise:</b> " + esc("; ".join(miss)) + ".")
    lines.append("<b>Sources</b>")
    for i, s_ in enumerate(r["sources"], 1):
        kind = "consumer guidance" if s_["kind"] == "guidance" else s_["kind"]
        lines.append(f"[{i}] <a href=\"{esc(s_['url'])}\">{esc(s_['title'][:90])}</a> — "
                     f"{esc(s_['org'])}, {kind}")
    how = ("live retrieval" if not r.get("cached") else "cached retrieval") + \
        f" ({esc(', '.join(r.get('retrieval') or []))}, {esc(r.get('retrieved') or '')})"
    lines.append(f"<i>How this was made: {how}; sentences chosen and ordered by fixed rules, "
                 f"no AI model; no web search. Sent out: “{esc(' '.join(r.get('broadened_from') or kws))}”"
                 + (f", then “{esc(' '.join(kws))}”" if r.get("broadened_from") else "") + " only.</i>")
    links = [(f"[{i}] " + s_["title"][:34], s_["url"]) for i, s_ in enumerate(r["sources"], 1)]
    return Reply("\n".join(lines), domain="research", ref=ref,
                 snapshot={"keywords": kws, "sources": r["sources"]}, links=links,
                 actions=[("How much?", "r_aspect", "dose"), ("Is it safe?", "r_aspect", "safety"),
                          ("Timing?", "r_aspect", "timing"), ("Does it work?", "r_aspect", "efficacy")])


# ── property follow-ups (live public records) ─────────────────────────

def property_parties(f: dict, opener=None) -> Reply:
    from core import property_live as L
    try:
        p = L.parties(f["doc_id"], f["doc_type"], opener=opener)
    except Exception as exc:
        return Reply(f"I couldn't reach NYC's ACRIS parties dataset just now ({type(exc).__name__}). "
                     "The official record link on the card lists them too.", domain="property")
    r1, r2 = p["roles"]

    def names(lst):
        merged = []
        for x in lst:          # ACRIS splits long names across rows ("OF THE CITY OF ...", "F/K/A ...")
            if merged and re.match(r"^(OF |F/K/A|A/K/A|D/B/A|AS |AND |&|INC\b|LLC\b|L\.?P\.?\b)", x["name"]):
                merged[-1] = dict(merged[-1], name=merged[-1]["name"] + " " + x["name"])
            else:
                merged.append(x)
        lst = merged
        return "; ".join(f"{esc(x['name'])}" + (f" ({esc(x['city'])})" if x["city"] else "")
                         for x in lst) or "none listed"
    lines = [f"<b>Parties on this {esc(L.type_name(f['doc_type']))}</b> (official ACRIS index)",
             f"<b>{esc(r1.capitalize())}:</b> {names(p['party1'])}",
             f"<b>{esc(r2.capitalize())}:</b> {names(p['party2'])}"]
    if f["doc_type"] == "MTGE":
        lines.append("<i>Note: names are as filed. Borrowers are often single-purpose companies, and a "
                     "\"c/o\" address can be a manager rather than the owner.</i>")
    lines.append(footer("Live lookup of NYC Open Data dataset 636b-3b5g, just now"))
    return Reply("\n".join(lines), domain="property", ref={"kind": "acris_item", "doc_id": f["doc_id"]},
                 snapshot=f, links=[("Parties data (official)", p["source"])])


def property_history(f: dict, opener=None) -> Reply:
    from core import property_live as L
    bbl = f.get("bbl")
    if not bbl or len(str(bbl)) != 10:
        return Reply("This record has no usable lot number (BBL), so I can't look up its history.",
                     domain="property")
    try:
        h = L.lot_history(bbl, opener=opener)
    except Exception as exc:
        return Reply(f"I couldn't reach NYC's ACRIS datasets just now ({type(exc).__name__}).",
                     domain="property")
    lines = [f"<b>Recorded history of this lot</b> (BBL {esc(bbl)})",
             f"{h['total']} documents since {esc(h['first'] or '?')}. Most common: " +
             esc(", ".join(f"{L.type_name(k)} ×{v}" for k, v in sorted(h['counts'].items(),
                                                                     key=lambda kv: -kv[1])[:5])),
             "<b>Most recent</b>"]
    same_day = {}
    for d in h["recent"][:8]:
        day = (d.get("recorded_datetime") or "")[:10]
        amt = float(d.get("document_amt") or 0)
        same_day.setdefault(day, set()).add(d.get("doc_type"))
        lines.append(f"• {esc(day)} — {esc(L.type_name(d.get('doc_type')))}"
                     + (f", {pf.money(amt)}" if amt else ""))
    rec = (f.get("recorded") or "")[:10]
    group = same_day.get(rec, set())
    if {"MTGE", "SAT"} <= group or {"MTGE", "ASST"} <= group:
        lines.append("<b>Interpretation (not proof):</b> a new mortgage recorded the same day as an "
                     "old-mortgage satisfaction/assignment usually indicates a refinancing. The "
                     "records alone don't show the reason or terms.")
    deeds = h.get("deeds") or [d for d in h["recent"] if d.get("doc_type") in ("DEED", "DEEDO")]
    earlier = [d for d in deeds if d.get("document_id") != f.get("doc_id")]
    if earlier:
        lines.append("Earlier ownership transfers (deeds): " + esc(", ".join(
            (d.get("recorded_datetime") or "")[:10] + (f" {pf.money(float(d.get('document_amt') or 0))}"
                                                     if float(d.get("document_amt") or 0) else "")
            for d in earlier[:4])) + ".")
    else:
        lines.append(f"No earlier deed on this lot in the city index (records start {esc(h['first'] or '?')}).")
    if f.get("doc_type") in ("DEED", "DEEDO") and len(f.get("caveats") or []) and \
            any("parcels" in c for c in f["caveats"]):
        lines.append("<i>This deed covers several lots; this history is for one of them.</i>")
    lines.append(footer("Live lookup of NYC Open Data (ACRIS legals + master), just now; "
                        "NYC's feed has published nothing recorded after its newest date"))
    return Reply("\n".join(lines), domain="property", ref={"kind": "acris_item", "doc_id": f.get("doc_id")},
                 snapshot=f, links=[("Lot documents (official)", h["source"])])


# ── quant follow-ups ──────────────────────────────────────────────────

def quant_changes(conn=None, now=None) -> Reply:
    conn = conn or eventbot_conn()
    now = now or _now()
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    opened = conn.execute("SELECT id, asset, rule, notional FROM positions WHERE entry_ts >= ? "
                          "ORDER BY entry_ts", (since,)).fetchall()
    closed = conn.execute("SELECT id, asset, rule, pnl, exit_reason FROM positions WHERE status='closed' "
                          "AND exit_ts >= ? ORDER BY exit_ts", (since,)).fetchall()
    a = qf.accounting(conn)
    ch = qf.change_24h(conn, now)
    lines = ["<b>What changed in the simulation (last 24 h)</b>",
             f"Equity change: {sd(ch) if ch is not None else 'unknown'}.",
             f"Opened {len(opened)}: " + (esc(", ".join(f"#{r[0]} {r[1]} ({r[2]})" for r in opened)) or "none"),
             f"Closed {len(closed)}: " + (esc(", ".join(f"#{r[0]} {r[1]} {sd(r[3] or 0)} ({r[4]})"
                                                       for r in closed)) or "none")]
    if closed:
        lines.append(f"Realized from those closes: {sd(sum(r[3] or 0 for r in closed))}.")
    lines.append(footer(snap_quant(a)))
    return Reply("\n".join(lines), domain="quant")


def quant_rule(rule: str, conn=None) -> Reply:
    conn = conn or eventbot_conn()
    rows = conn.execute("SELECT pnl, exit_reason FROM positions WHERE rule=? AND status='closed'",
                        (rule,)).fetchall()
    n = len(rows)
    lines = [f"<b>Rule <code>{esc(rule)}</code>: simulated track record</b>"]
    if not n:
        lines.append("No closed simulated trades for this rule yet, so there is no track record.")
    else:
        wins = sum(1 for p, _ in rows if (p or 0) > 0)
        total = sum(p or 0 for p, _ in rows)
        reasons = {}
        for _, r in rows:
            reasons[r] = reasons.get(r, 0) + 1
        lines += [f"Closed trades: {n}; profitable: {wins} ({100 * wins / n:.0f}%); total P&L {sd(total)}; "
                  f"average {sd(total / n)} per trade.",
                  "Exit reasons: " + esc(", ".join(f"{k} ×{v}" for k, v in reasons.items())) + "."]
        if n < 30:
            lines.append(f"<i>{n} trade{'' if n == 1 else 's'} is a small sample; this record says little about future results.</i>")
    lines.append("<i>Simulation only: no fees or slippage modelled.</i>")
    return Reply("\n".join(lines), domain="quant")


ASSET_ALIASES = {"gold": "GLD", "gld": "GLD", "bitcoin": "BTC-USD", "btc": "BTC-USD",
                 "ethereum": "ETH-USD", "eth": "ETH-USD", "ether": "ETH-USD", "djt": "DJT",
                 "trump media": "DJT", "sh": "SH", "short s&p": "SH", "inverse s&p": "SH"}


def find_position(text, conn=None):
    """Open simulated position named in free text (by symbol or common name), else None."""
    conn = conn or eventbot_conn()
    t = " " + text.lower() + " "
    opens = qf.open_positions(conn)
    for word, sym in ASSET_ALIASES.items():
        if f" {word} " in t or f" {word}?" in t or f" {word}," in t:
            for p in opens:
                if p["asset"] == sym:
                    return p
    for p in opens:
        if p["asset"].lower().split("-")[0] in re.findall(r"[a-z]+", t):
            return p
    return None


ADDRESS_RE = re.compile(r"\b(\d{1,5}[a-z]?(?:-\d{1,5})?\s+(?:[a-z]+\s){0,3}?"
                        r"(?:street|st|avenue|ave|place|pl|road|rd|boulevard|blvd|drive|dr|lane|ln|"
                        r"broadway|parkway|pkwy|terrace|court|ct))\b", re.I)
_ABBR = {"st": "street", "ave": "avenue", "pl": "place", "rd": "road", "blvd": "boulevard",
         "dr": "drive", "ln": "lane", "pkwy": "parkway", "ct": "court"}


def find_property(text, conn=None):
    """Item in the store whose address matches one named in the text."""
    m = ADDRESS_RE.search(text or "")
    if not m:
        return None
    words = m.group(1).lower().split()
    words = [_ABBR.get(w, w) for w in words]
    num, rest = words[0], words[1:]
    conn = conn or spine_conn()
    for r in conn.execute("SELECT id, data_json FROM items WHERE source='acris' AND kind='deal'"):
        try:
            addr = (json.loads(r["data_json"]).get("address") or "").lower()
        except Exception:
            continue
        aw = [_ABBR.get(w, w) for w in addr.replace(",", " ").split()]
        if aw and (aw[0] == num or aw[0].startswith(num + "-") or aw[0].endswith("-" + num)) \
                and all(w in aw for w in rest):
            return pf.load_item(conn, r["id"])
    return None
