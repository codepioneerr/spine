"""
core.health_brief — the morning health brief, minus the model.

Same two-layer shape as core.brief (CLAUDE.md 8, collectors/brief.py):

  Layer 1 (here, deterministic, always sent): yesterday's numbers against a
  14-day baseline, plain-language flags from fixed rules, and the strongest
  60-day correlations between diet, sleep, training, recovery and gait.

  Layer 2 (collectors/health_brief.py): a local model turns layer 1 plus the
  personal profile into a Gokhale-informed plan. It is a 4B model on a CPU,
  so it gets the numbers already computed and is told not to invent any.

## Privacy

Health data is the most private thing on the box. The job is `data:
private`, so core.models will only route it to the local Ollama provider;
nothing leaves the machine. That is also why the model cannot "search the
web for Gokhale applications": a web search is a prompt leaving the box.
The Gokhale principles it needs are written down here instead (GOKHALE),
from the method's published material, so its advice is grounded in a fixed
text rather than in whatever a search returns.

The personal profile (conditions, training) is NOT in this file: the repo
is public. It lives in var/health_profile.md, which is gitignored.
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import date, timedelta

from core import paths

BASELINE_DAYS = 14
CORR_DAYS = 60
CORR_MIN_N = 10
CORR_MIN_R = 0.4

# Metrics summed per day; everything else is averaged.
SUMMED = {"step_count", "active_energy", "basal_energy_burned",
          "walking_running_distance", "flights_climbed", "apple_exercise_time",
          "apple_stand_time", "time_in_daylight"}
NUTRITION_PREFIXES = ("dietary_",)
NUTRITION = {"protein", "carbohydrates", "total_fat", "fiber", "sodium",
             "potassium", "magnesium", "calcium", "iron", "zinc", "vitamin_d",
             "vitamin_c", "vitamin_b12", "folate", "omega_3", "caffeine",
             "saturated_fat", "cholesterol", "selenium", "vitamin_a",
             "vitamin_e", "vitamin_k", "water"}

GAIT = {
    "walking_asymmetry_percentage": "walking asymmetry",
    "walking_double_support_percentage": "double support",
    "walking_step_length": "step length",
    "walking_speed": "walking speed",
    "apple_walking_steadiness": "walking steadiness",
    "stair_speed_up": "stair speed up",
    "stair_speed_down": "stair speed down",
    "running_ground_contact_time": "run ground contact",
    "running_vertical_oscillation": "run vertical bounce",
    "running_stride_length": "run stride length",
}
RECOVERY = {
    "heart_rate_variability": "HRV",
    "resting_heart_rate": "resting HR",
    "respiratory_rate": "breathing rate",
    "apple_sleeping_wrist_temperature": "wrist temp",
    "blood_oxygen_saturation": "blood oxygen",
    "vo2_max": "VO2 max",
}

GOKHALE = """\
Gokhale Method principles (Esther Gokhale, "8 Steps to a Pain-Free Back"):
- Pelvis: a gently anteverted pelvis (sit bones back) under a J-shaped spine:
  long and fairly flat lower back, curve only at the base. Not a swayback:
  ribs stay down, low back is not arched.
- Inner corset: lightly engage deep abdominal/back muscles to lengthen and
  decompress the spine, especially when lifting, twisting or striking.
- Stretchsitting / stacksitting: lengthen the back against a backrest, or
  stack vertebrae over an anteverted pelvis; no slumping or tucking.
- Glidewalking: the back leg drives (squeeze glute of the rear leg, push off
  through the ball of the foot), the front foot lands softly under the body,
  no reaching or heel-striking. Torso stays stacked over the hips.
- Feet: weight toward the heels and outer edge, toes pointing slightly out
  (~10-15 deg), arch lifted by rolling the foot slightly outward. Knees track
  over the 2nd-3rd toe, never collapsing inward.
- Shoulder roll: shoulders gently rolled back and down, arms hang beside the
  body, not in front.
- Hip hinge: bend from the hip joints with a long spine; no rounding.
- Neck: lengthen the back of the neck, chin slightly down, head over spine.
- Sleep: stretchlying on back or side with a long spine; pillow supports the
  neck in line with the spine.
"""


def profile_path() -> str:
    return os.environ.get("SPINE_HEALTH_PROFILE") or paths.var("health_profile.md")


def load_profile() -> str:
    try:
        with open(profile_path(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "(no profile at var/health_profile.md)"


# ── queries ──────────────────────────────────────────────────────────

def _is_nutrition(m: str) -> bool:
    return m in NUTRITION or m.startswith(NUTRITION_PREFIXES)


def daily_metric(conn, metric: str, start: str, end: str) -> dict[str, float]:
    """{date: value} for start <= date <= end, summed or averaged per day."""
    agg = "SUM" if (metric in SUMMED or _is_nutrition(metric)) else "AVG"
    rows = conn.execute(
        f"SELECT date, {agg}(qty) FROM health_metric_samples "
        "WHERE metric=? AND date BETWEEN ? AND ? AND qty IS NOT NULL GROUP BY date",
        (metric, start, end)).fetchall()
    return {d: v for d, v in rows if v is not None}


def units_of(conn, metric: str) -> str:
    r = conn.execute("SELECT units FROM health_metric_samples WHERE metric=? "
                     "AND units IS NOT NULL LIMIT 1", (metric,)).fetchone()
    return r[0] if r and r[0] else ""


def sleep_by_wake_date(conn, start: str, end: str) -> dict[str, dict]:
    """Sleep sessions keyed by the date they ended (the morning after)."""
    out = {}
    for row in conn.execute(
            "SELECT end_time, total_sleep_minutes, deep_sleep_minutes, "
            "rem_sleep_minutes, core_sleep_minutes, awake_minutes, "
            "sleep_efficiency_pct FROM sleep_sessions WHERE end_time IS NOT NULL"):
        d = row[0][:10]
        if start <= d <= end:
            out[d] = dict(zip(("total", "deep", "rem", "core", "awake", "eff"), row[1:]))
    return out


def workouts_on(conn, day: str) -> list[tuple]:
    return conn.execute(
        "SELECT workout_type, duration_minutes, active_energy_kcal, avg_heart_rate, "
        "max_heart_rate FROM workouts WHERE substr(start_time,1,10)=? "
        "ORDER BY start_time", (day,)).fetchall()


# ── layer 1 ──────────────────────────────────────────────────────────

def _fmt(v, units=""):
    if v is None:
        return "—"
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}"
    return f"{s}{' ' + units if units and units not in ('count',) else ''}"


def _vs(today, base):
    if today is None or not base:
        return ""
    avg = sum(base) / len(base)
    if not avg:
        return ""
    pct = 100 * (today - avg) / abs(avg)
    return f" (14d avg {_fmt(avg)}, {pct:+.0f}%)"


def _pearson(xs, ys):
    n = len(xs)
    if n < CORR_MIN_N:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def build(conn: sqlite3.Connection, day: date) -> dict:
    """Everything the brief says about `day`. Returns {text, flags, facts, has_data}."""
    d = day.isoformat()
    b0 = (day - timedelta(days=BASELINE_DAYS)).isoformat()
    b1 = (day - timedelta(days=1)).isoformat()
    c0 = (day - timedelta(days=CORR_DAYS)).isoformat()

    present = [m for (m,) in conn.execute(
        "SELECT DISTINCT metric FROM health_metric_samples WHERE date BETWEEN ? AND ?",
        (c0, d))]
    sections: dict[str, list[str]] = {"Gait": [], "Recovery": [], "Sleep": [],
                                      "Training": [], "Nutrition": [], "Mind & body": []}
    flags: list[str] = []
    today: dict[str, float] = {}
    base: dict[str, list] = {}

    def line(section, label, metric):
        series = daily_metric(conn, metric, b0, d)
        v = series.get(d)
        if v is None:
            return
        today[metric] = v
        base[metric] = [series[k] for k in series if k != d]
        sections[section].append(f"- {label}: {_fmt(v, units_of(conn, metric))}{_vs(v, base[metric])}")

    for m, label in GAIT.items():
        if m in present:
            line("Gait", label, m)
    for m, label in RECOVERY.items():
        if m in present:
            line("Recovery", label, m)
    for m in ("step_count", "active_energy"):
        if m in present:
            line("Training", m.replace("_", " "), m)
    for m in sorted(x for x in present if _is_nutrition(x)):
        line("Nutrition", m.replace("dietary_", "").replace("_", " "), m)

    # Sleep: the night that ended on the morning of `day`.
    sleeps = sleep_by_wake_date(conn, b0, d)
    s = sleeps.get(d)
    if s:
        base_total = [v["total"] for k, v in sleeps.items() if k != d and v["total"]]
        sections["Sleep"].append(
            f"- total {_fmt((s['total'] or 0) / 60)} h{_vs((s['total'] or 0) / 60, [x / 60 for x in base_total])}; "
            f"deep {_fmt(s['deep'])} min, REM {_fmt(s['rem'])} min, core {_fmt(s['core'])} min, "
            f"awake {_fmt(s['awake'])} min, efficiency {_fmt(s['eff'])}%")

    for w in workouts_on(conn, d):
        sections["Training"].append(
            f"- {w[0]}: {_fmt(w[1])} min, {_fmt(w[2])} kcal, avg HR {_fmt(w[3])}, max HR {_fmt(w[4])}")

    for name, sev in conn.execute(
            "SELECT name, severity FROM symptoms WHERE substr(start_time,1,10)=?", (d,)):
        sections["Mind & body"].append(f"- symptom: {name}" + (f" ({sev})" if sev else ""))
    for kind, val, cls, labels in conn.execute(
            "SELECT kind, valence, valence_classification, labels_json FROM state_of_mind "
            "WHERE substr(start_time,1,10)=?", (d,)):
        lab = labels.strip("[]").replace('"', "") if labels else ""
        sections["Mind & body"].append(
            f"- mood ({kind or 'logged'}): {cls or _fmt(val)}" + (f" — {lab}" if lab else ""))
    for cls, hr in conn.execute(
            "SELECT classification, avg_heart_rate FROM ecg_recordings "
            "WHERE substr(start_time,1,10)=?", (d,)):
        sections["Mind & body"].append(f"- ECG: {cls or 'unclassified'}, avg HR {_fmt(hr)}")
        if cls and "sinus" not in cls.lower():
            flags.append(f"ECG on {d} read '{cls}'. Show it to a doctor; "
                         "the watch is a screening tool, not a diagnosis.")

    flags += _rules(today, base, s)
    patterns = correlations(conn, c0, d)

    has_data = any(sections.values())
    out = [f"HEALTH BRIEF — {day.strftime('%a %b %d')}"]
    if not has_data:
        out.append(f"No data synced for {d}. Open Health Auto Export on the phone "
                   "and check its automations ran.")
    if flags:
        out.append("\nWatch today:")
        out += [f"- {f}" for f in flags]
    for name, rows in sections.items():
        if rows:
            out.append(f"\n{name}:")
            out += rows
    if patterns:
        out.append(f"\nPatterns (last {CORR_DAYS} days — correlation, not proof):")
        out += [f"- {p}" for p in patterns]
    return {"text": "\n".join(out), "flags": flags, "patterns": patterns,
            "has_data": has_data}


def _rules(today, base, sleep) -> list[str]:
    """Fixed, conservative rules. Each fires only against his own baseline
    (>= 5 prior days) or a widely used absolute line."""
    out = []

    def avg(m):
        b = base.get(m) or []
        return sum(b) / len(b) if len(b) >= 5 else None

    a = today.get("walking_asymmetry_percentage")
    if a is not None and (a >= 5 or (avg("walking_asymmetry_percentage") and
                                     a > 1.5 * avg("walking_asymmetry_percentage") and a >= 2)):
        out.append(f"Walking asymmetry {a:.1f}% is high for you. Check one side "
                   "isn't guarding: even push-off from both rear legs (glidewalk).")
    ds = today.get("walking_double_support_percentage")
    if ds is not None and avg("walking_double_support_percentage") and \
            ds > avg("walking_double_support_percentage") * 1.08:
        out.append(f"Double support {ds:.1f}% is up: you spent more time on two feet, "
                   "often fatigue or caution. Lighter footwork day.")
    sl = today.get("walking_step_length")
    if sl is not None and avg("walking_step_length") and sl < avg("walking_step_length") * 0.95:
        out.append("Step length is down >5%. Often tight hips or tired legs: "
                   "hip mobility before training.")
    h = today.get("heart_rate_variability")
    if h is not None and avg("heart_rate_variability") and h < 0.85 * avg("heart_rate_variability"):
        out.append(f"HRV {h:.0f} ms is >15% under your norm: recovery is behind. "
                   "Technique over hard sparring today.")
    r = today.get("resting_heart_rate")
    if r is not None and avg("resting_heart_rate") and r > avg("resting_heart_rate") + 5:
        out.append(f"Resting HR {r:.0f} is 5+ above your norm: possible fatigue, "
                   "illness, alcohol or late food.")
    if sleep and sleep.get("total") and sleep["total"] < 360:
        out.append(f"Only {sleep['total'] / 60:.1f} h sleep. Expect slower reactions; "
                   "skip max-effort rounds.")
    if sleep and sleep.get("deep") is not None and sleep["total"] and sleep["deep"] < 40:
        out.append(f"Deep sleep {sleep['deep']:.0f} min was low; it is when tissue repairs.")
    return out


def _series(conn, c0, d) -> dict[str, dict[str, float]]:
    s = {}
    for m, label in {**{k: v for k, v in GAIT.items() if k.startswith("walking")},
                     "heart_rate_variability": "HRV", "resting_heart_rate": "resting HR",
                     "step_count": "steps"}.items():
        s[label] = daily_metric(conn, m, c0, d)
    for (m,) in conn.execute("SELECT DISTINCT metric FROM health_metric_samples "
                             "WHERE date BETWEEN ? AND ?", (c0, d)).fetchall():
        if _is_nutrition(m):
            s[m.replace("dietary_", "").replace("_", " ") + " intake"] = daily_metric(conn, m, c0, d)
    sl = sleep_by_wake_date(conn, c0, d)
    s["deep sleep"] = {k: v["deep"] for k, v in sl.items() if v["deep"] is not None}
    s["total sleep"] = {k: v["total"] for k, v in sl.items() if v["total"] is not None}
    tr = conn.execute("SELECT substr(start_time,1,10), SUM(duration_minutes) FROM workouts "
                      "WHERE substr(start_time,1,10) BETWEEN ? AND ? GROUP BY 1", (c0, d))
    s["training minutes"] = {k: v for k, v in tr if v}
    return {k: v for k, v in s.items() if len(v) >= CORR_MIN_N}


def correlations(conn, c0: str, d: str, top: int = 4) -> list[str]:
    """Same-day pairs across groups, plus "day X -> next morning Y" for
    intake and training. Only |r| >= CORR_MIN_R with n >= CORR_MIN_N."""
    s = _series(conn, c0, d)
    cause = [k for k in s if k.endswith("intake") or k == "training minutes"]
    effect = [k for k in s if k not in cause]
    found = []

    def nxt(day: str) -> str:
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    for a in cause:
        for b in effect:
            pairs = [(s[a][k], s[b][nxt(k)]) for k in s[a] if nxt(k) in s[b]]
            r = _pearson(*zip(*pairs)) if len(pairs) >= CORR_MIN_N else None
            if r is not None and abs(r) >= CORR_MIN_R:
                found.append((abs(r), f"more {a} → {'higher' if r > 0 else 'lower'} "
                                      f"{b} next day (r={r:+.2f}, {len(pairs)} days)"))
    for i, a in enumerate(effect):
        for b in effect[i + 1:]:
            ks = [k for k in s[a] if k in s[b]]
            r = _pearson([s[a][k] for k in ks], [s[b][k] for k in ks]) \
                if len(ks) >= CORR_MIN_N else None
            if r is not None and abs(r) >= CORR_MIN_R:
                found.append((abs(r), f"{a} and {b} move {'together' if r > 0 else 'oppositely'} "
                                      f"(r={r:+.2f}, {len(ks)} days)"))
    found.sort(reverse=True)
    return [t for _, t in found[:top]]


def prompt(layer1: str, profile: str) -> str:
    """Layer 2 prompt. Sized for a 4096-token context with ~650 out."""
    return (
        "You are a movement and recovery coach writing a short morning plan for "
        "one person. You are not a doctor and must not diagnose.\n\n"
        f"ABOUT THEM:\n{profile[:1500]}\n\n"
        f"{GOKHALE}\n"
        f"YESTERDAY'S DATA (already computed; do not invent numbers):\n{layer1[:5000]}\n\n"
        "Write the plan in plain words, no jargon, under 300 words, as:\n"
        "1. Today in one sentence.\n"
        "2. Alignment fix: 2-3 specific Gokhale cues tied to the gait numbers and "
        "their feet, knees and pelvis, and how to use them in stance, pivots and kicks.\n"
        "3. Recovery and training: how hard to go today, based on sleep, HRV and resting HR.\n"
        "4. Fuel: one or two food or timing changes suggested by the nutrition and "
        "patterns, aimed at energy, recovery and sexual health (sleep, zinc, "
        "vitamin D, magnesium, protein, hydration where relevant).\n"
        "5. One thing to watch.\n"
        "Only use numbers shown above. If data is missing, say what to log. If "
        "anything suggests pain, injury or a heart issue, say to see a clinician.\n"
    )
