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

import os
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

Ask in normal words, e.g. “How has my activity changed this week?”, or reply to any card with “Explain this”.

/brief – today's short overview
/health – your data, coverage and trends
/workout – quiet dorm-mat routine · /mobility – ankles, knees, hips
/property – recent recorded property documents
/quant – simulation summary
/focus – what to include in briefs · /settings – time, quiet hours
/glossary deed – plain-English terms
/sources · /status (operator view) · /privacy · /forget · /cancel

Hermes (@hermnick_bot) stays your general chat agent. This bot only reads your data and answers; it never trades, posts or deletes source data."""

PRIVACY = """<b>Privacy, plainly</b>
• Your health data stays in a local database on the Dell (var/health.db, owner-only).
• To answer you, a few computed numbers are sent to you through Telegram. Telegram bot chats are <b>not end-to-end encrypted</b>: Telegram's servers can see these messages.
• This bot uses no hosted AI model for your health data. Answers are computed on the Dell from fixed rules and vetted sources.
• Only your numeric Telegram account, in a private chat, is answered. Anyone else is ignored.
• Logs record timing and errors, not your messages or health values.
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
    stale = " — <b>older than a day; open Health Auto Export to sync</b>" if age and age > 26 else ""
    sleep = ("sleep records: none exported yet" if not cov["sleep_records"]
             else f"sleep records: {cov['sleep_records']}")
    return (f"Last import: {_et(cov['last_import'])} ({age:.0f} h ago){stale}\n"
            f"History: {cov['days']} day(s), {cov['first_day']} → {cov['last_day']}; "
            f"{sleep}; workouts: {cov['workouts']}\n"
            f"Devices: {esc(', '.join(cov['devices']) or 'unknown')}")


def health_summary(conn=None, now=None) -> Reply:
    conn = conn or health_conn()
    now = now or _now()
    cov = hf.coverage(conn, now)
    full, partial = _target_days(cov)
    lines = ["<b>Health — your recorded data</b>", coverage_text(cov), ""]
    snap = {"coverage": {k: (str(v) if k == "last_import" else v) for k, v in cov.items()}}
    imp = cov["last_import"]
    for label, d in (("Last complete day", full), ("Today so far", partial)):
        if not d:
            continue
        lines.append(f"<b>{label} ({d.strftime('%a %b %d')})</b>")
        if d.isoformat() == cov["first_day"]:
            lines.append("<i>First day on record: it may not cover the whole day of wear.</i>")
        for m in KEY_METRICS:
            f = hf.fact(conn, m, d, imp)
            if f.value is not None:
                lines.append("• " + esc(hf.describe(f, cov["days"] >= 7)))
                snap[f"{d}:{m}"] = f.value
        lines.append("")
    if cov["days"] < 7:
        lines.append(f"<i>Only {cov['days']} day(s) of history, so there is no personal baseline "
                     "yet. Trends and comparisons start after ~5 complete days.</i>")
    if full is None and partial is None:
        lines.append("No measurements to show.")
    return Reply("\n".join(lines).strip(), domain="health", ref={"kind": "summary"},
                 snapshot=snap,
                 actions=[("Steps 7d", "trend", "step_count:7"),
                          ("Steps 30d", "trend", "step_count:30"),
                          ("Two things to improve", "improve", ""),
                          ("Explain numbers", "explain_health", ""),
                          ("Refresh / sync help", "sync_help", "")])


EXPLAIN_HEALTH = """<b>What these numbers mean</b>
• <b>Steps / exercise minutes / active energy</b> are totals for a calendar day (New York time). A day that hasn't finished, or hasn't fully synced, is a running total — a low number at 9 am is not a lazy day.
• <b>Active energy</b> is Apple's estimate from motion and heart rate, not a measurement.
• <b>Resting heart rate</b> is Apple's daily estimate. Lower usually tracks fitness, but day-to-day swings of a few bpm are normal.
• <b>HRV</b> on Apple Watch is <b>SDNN</b> in milliseconds, from short readings taken at irregular times; we show that day's average. It is noisy, varies a lot between people, and can't be compared with other devices' RMSSD numbers. Only your own multi-week trend is meaningful.
• <b>Blood oxygen</b> is a wellness spot reading, not a medical oximeter.
• A <b>baseline</b> appears only once there are ≥5 complete prior days (7-day) or ≥14 (30-day); the sample count is always shown."""


def explain_health() -> Reply:
    return Reply(EXPLAIN_HEALTH, domain="health")


SYNC_HELP = """<b>Keeping your data current</b>
Your data arrives from the iPhone app <b>Health Auto Export</b>, which POSTs to the Dell over Tailscale. Nothing is real-time.
1. Open Health Auto Export → Automations → your REST API automation.
2. Make sure it includes the metrics you care about. <b>Sleep Analysis is not arriving yet</b>; add it if you want sleep answers.
3. Keep “Aggregate data: by day”.
4. Tap “Export now” (or “Manual export”) to send immediately. Background exports only run when iOS allows; opening the app helps.
Then ask /health again: the “Last import” time should update."""


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
            "more hours a night (CDC). Consistent bed/wake times are the most practical lever in a "
            "dorm: same wake time daily, dim screens 30 min before bed, earplugs/eye mask.")
    return Reply(text, domain="health", ref={"kind": "sleep"},
                 actions=[("Sources", "sources", "sleep"), ("Sync help", "sync_help", ""),
                          ("Add sleep to brief", "focus_propose", "sleep consistency")])


def sexual_health() -> Reply:
    text = """<b>Sexual health — what your data can and can't tell</b>
<b>Can't tell:</b> your watch data cannot measure hormones, sexual function, fertility or STI status. I won't infer any of those from steps or heart rate.

<b>Can loosely reflect:</b> the general cardiovascular and lifestyle factors that matter for sexual health — regular activity, resting heart rate trend, and (once exported) sleep.

<b>General evidence (NIDDK):</b> inactivity, smoking, heavy drinking, drug use and blood-vessel problems such as high blood pressure are linked to erectile problems; heart-healthy habits support blood-vessel health.

<b>Practical, for anyone:</b>
• Regular activity (150 min/week) and 7+ hours of sleep.
• Keep alcohol moderate; don't smoke or vape nicotine.
• Consent and condoms; STI testing on a schedule that fits your situation (CDC's page lists schedules by group — a campus health center can tell you which applies).
• Kegels/pelvic-floor routines aren't a default for everyone; NIDDK advises checking with a clinician first.

<b>See a clinician</b> for any persistent change in erections or libido, pain, discharge, sores, or blood in urine or semen. Campus health is a fine first stop.

This topic won't appear in your daily briefs unless you add it yourself."""
    return Reply(text, domain="health", ref={"kind": "sexual"}, sensitive=True,
                 actions=[("Sources", "sources", "sexual"),
                          ("Pelvic floor: evidence", "sources", "pelvic")])


def mobility(profile_text="") -> Reply:
    intro = ("<b>Comfortable walking + ankle, knee and hip mobility</b>\n"
             "I read “goota” as <b>GOATA</b>, the gait/movement coaching system. If you meant "
             "something else, tell me.\n\n"
             "<b>Evidence check:</b> GOATA is a commercial method; I found no peer-reviewed "
             "controlled trials of GOATA itself (searched Oct 6, 2026). General strength, balance "
             "and mobility work has much better support. There's no single correct foot angle or "
             "gait for everyone, and no routine will “realign” bones.\n\n")
    plan = workouts.render("mobility10", workouts.profile_notes(profile_text))
    return Reply(intro + plan, domain="health", ref={"kind": "workout", "plan": "mobility10"},
                 actions=[("Done", "fb", "done"), ("Too easy", "fb", "easy"),
                          ("Too hard", "fb", "hard"), ("Skip", "fb", "skip"),
                          ("Sources", "sources", "mobility"),
                          ("Add mobility to brief", "focus_propose", "mobility")])


def workout(plan_id="strength15", profile_text="") -> Reply:
    text = workouts.render(plan_id, workouts.profile_notes(profile_text))
    return Reply(text, domain="health", ref={"kind": "workout", "plan": plan_id},
                 actions=[("Done", "fb", "done"), ("Too easy", "fb", "easy"),
                          ("Too hard", "fb", "hard"), ("Skip", "fb", "skip"),
                          ("Remind me later", "fb", "later")])


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
    links = [("Official ACRIS record", f["acris_url"])] if f["acris_url"] else []
    if f["zola_url"]:
        links.append(("Lot on ZoLa map", f["zola_url"]))
    return Reply("\n".join(lines), domain="property",
                 ref={"kind": "acris_item", "item_id": item["id"], "doc_id": f["doc_id"]},
                 snapshot=f, links=links,
                 actions=[("Explain this", "p_explain", ""), ("Sale or loan?", "p_saleloan", ""),
                          ("Building facts", "p_building", ""), ("Score?", "p_score", ""),
                          ("Save", "p_save", ""), ("Dismiss", "p_dismiss", "")])


def property_explain(f: dict) -> Reply:
    dt = f["doc_type"]
    if dt == "MTGE":
        core_ = ("This record is a <b>mortgage</b>: the owner borrowed money and pledged the "
                 "building as security. Think of it like a car loan where the car is the "
                 f"collateral. The {pf.money(f['amount'])} is how much was borrowed. It does not "
                 "tell you what the building is worth or that it was sold. Big buildings refinance "
                 "regularly, so this is often routine.")
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
        t = "<b>A loan.</b> MTGE = mortgage. Ownership didn't change because of this document."
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


def glossary_reply(term: str) -> Reply:
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
             f"${a['equity']:,.2f} ({a['total_return_pct']:+.2f}%) as of {a['as_of'][11:16]} UTC.",
             f"= realized {sd(a['realized'])} ({a['n_closed']} closed) "
             f"+ unrealized {sd(a['unrealized'])} ({a['n_open']} open)"
             + (f" {sd(a['residual'])} unexplained cash difference" if abs(a['residual']) >= 0.01 else ""),
             ]
    if ch is not None:
        lines.append(f"Last 24 h: {sd(ch)} equity change.")
    lines.append("<i>Returns are from a simulator that fills at the last quoted price with no "
                 "fees or slippage, so real trading would do worse.</i>")
    return Reply("\n".join(lines), domain="quant", ref={"kind": "summary"}, snapshot=a,
                 actions=[("Open positions", "q_positions", ""), ("How return is calculated", "q_calc", ""),
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
• <b>No costs modelled:</b> fills at last price, no fees, spreads or slippage.
• <b>Short history:</b> a few weeks of simulation says little about future results.
• <b>Rule triggers are keyword matches</b> on headlines or market moves. A headline is evidence that something was reported, not that it is true or that it moved prices.
• <b>Expiry artefacts:</b> some crypto entries were triggered by short-dated prediction markets dropping to ~1% at their end time — that's a market settling, not news. Flagged for review in eventbot's rules.
• Nothing here places orders; switching to paper or live trading is outside this bot."""


def quant_positions(conn=None) -> list[Reply]:
    conn = conn or eventbot_conn()
    out = []
    for p in qf.open_positions(conn)[:6]:
        t = (f"<b>{esc(p['asset'])}</b> simulated long, ${p['notional']:,.0f} at {p['entry_price']:,.2f} "
             f"({p['entry_ts'][:16].replace('T', ' ')} UTC)\nRule: {esc(p['rule'])}"
             + (f"; stop {p['stop']:,.2f}, target {p['target']:,.2f}" if p["stop"] and p["target"] else "")
             + (f"; closes by {p['max_exit_ts'][:16].replace('T', ' ')} UTC" if p["max_exit_ts"] else ""))
        out.append(Reply(t, domain="quant", ref={"kind": "position", "position_id": p["id"]},
                         snapshot=p, actions=[("Why opened?", "q_why", "")]))
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
        lines.append(f"on a {esc(s['source'])} signal seen {esc((s['seen_ts'] or s['ts'] or '')[:16])} UTC:")
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
    return Reply("\n".join(lines), domain="quant", links=links)
