"""
core.health_facts — deterministic, tested health facts for the assistant.

Everything a model or a Telegram message says about Nick's health numbers
comes from here. Nothing in this module calls a model.

## What the data actually is (verified Oct 6 2026)

Health Auto Export (iOS) POSTs to collectors/health_receiver with
"Aggregate data: by day": one row per (metric, calendar day, source) in
health_metric_samples, ts at local midnight with the phone's offset. The
app merges devices itself and reports the merged source as e.g.
"Nicholas's Apple Watch|iPhone (2)". So:

  - a day's value is an aggregate the phone computed, not raw samples;
  - the same day can be re-sent many times with a growing value until the
    day is over (the upsert replaces it, so nothing double-counts);
  - if two DIFFERENT source strings ever report the same summed metric
    for one day (e.g. "iPhone" and "Apple Watch" separately), adding them
    would double-count the same steps. We take the larger and say so.

## Rules this module enforces

  - Missing is not zero. A metric with no row for a day is None.
  - A day is COMPLETE only if an import arrived after that local day ended.
    Otherwise it is PARTIAL and must never be read as "a whole day of X".
  - A baseline exists only with enough complete prior days (MIN_N).
  - Calendar days are America/New_York (DST-aware via zoneinfo).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

# metric -> (label, unit shown, daily aggregation across sources, note)
METRICS = {
    "step_count": ("Steps", "steps", "sum",
                   "Phone/watch step count for the calendar day."),
    "walking_running_distance": ("Walking + running distance", "mi", "sum", ""),
    "active_energy": ("Active energy", "kcal", "sum",
                      "Estimated by Apple from motion and heart rate; an estimate, not a measurement."),
    "apple_exercise_time": ("Exercise minutes", "min", "sum",
                            "Minutes Apple counted as brisk activity."),
    "apple_stand_hour": ("Stand hours", "h", "sum", ""),
    "resting_heart_rate": ("Resting heart rate", "bpm", "mean",
                           "Apple's daily resting heart-rate estimate."),
    "walking_heart_rate_average": ("Walking heart rate", "bpm", "mean", ""),
    "heart_rate_variability": ("HRV (SDNN)", "ms", "mean",
                               "Apple Watch records SDNN from short, irregularly timed readings; "
                               "this is the mean of that day's readings. Not comparable with "
                               "RMSSD numbers from other devices."),
    "blood_oxygen_saturation": ("Blood oxygen", "%", "mean",
                                "Spot readings; wellness feature, not a medical oximeter."),
    "respiratory_rate": ("Breathing rate (sleep)", "br/min", "mean", ""),
}
SUMMED = {m for m, v in METRICS.items() if v[2] == "sum"}

BASELINES = {7: 5, 30: 14}   # window days -> minimum complete prior days


@dataclass
class DayValue:
    metric: str
    day: str
    value: float | None
    complete: bool
    sources: list = field(default_factory=list)
    reconciled: str = ""          # how overlapping sources were combined


@dataclass
class Baseline:
    window: int
    n: int
    need: int
    mean: float | None            # None when n < need

    @property
    def ok(self) -> bool:
        return self.mean is not None


def local_day(ts: datetime) -> date:
    return ts.astimezone(TZ).date()


def day_end_utc(d: date) -> datetime:
    """The instant local day `d` ends, in UTC (DST-correct)."""
    nxt = datetime.combine(d + timedelta(days=1), time(0), tzinfo=TZ)
    return nxt.astimezone(timezone.utc)


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_import(conn) -> datetime | None:
    r = conn.execute("SELECT MAX(received_at) FROM raw_payloads").fetchone()
    return _parse_utc(r[0]) if r else None


def is_complete(d: date, imported: datetime | None) -> bool:
    return imported is not None and imported >= day_end_utc(d)


def day_values(conn, metric: str, start: date, end: date,
               imported: datetime | None = None) -> dict[str, DayValue]:
    """{iso day: DayValue} for days with data. Days with no row are absent."""
    if imported is None:
        imported = last_import(conn)
    agg = METRICS.get(metric, ("", "", "mean", ""))[2]
    rows = conn.execute(
        "SELECT date, source, qty FROM health_metric_samples WHERE metric=? "
        "AND date BETWEEN ? AND ? AND qty IS NOT NULL ORDER BY date",
        (metric, start.isoformat(), end.isoformat())).fetchall()
    by_day: dict[str, list] = {}
    for d, src, q in rows:
        by_day.setdefault(d, []).append((src or "", float(q)))
    out = {}
    for d, vals in by_day.items():
        how = ""
        if len(vals) == 1:
            v = vals[0][1]
        elif agg == "sum":
            # Different source strings for one day overlap in reality; adding
            # them double-counts. The larger is the conservative choice.
            v = max(q for _, q in vals)
            how = "max of overlapping sources (not summed)"
        else:
            v = sum(q for _, q in vals) / len(vals)
            how = "mean of sources"
        out[d] = DayValue(metric, d, v, is_complete(date.fromisoformat(d), imported),
                          sorted({s for s, _ in vals}), how)
    return out


def baseline(series: dict[str, DayValue], target: date, window: int) -> Baseline:
    need = BASELINES.get(window, max(3, window // 2))
    lo = target - timedelta(days=window)
    vals = [dv.value for k, dv in series.items()
            if dv.complete and dv.value is not None
            and lo <= date.fromisoformat(k) < target]
    n = len(vals)
    return Baseline(window, n, need, (sum(vals) / n) if n >= need else None)


@dataclass
class MetricFact:
    metric: str
    label: str
    unit: str
    day: str
    value: float | None
    complete: bool
    b7: Baseline
    b30: Baseline
    note: str
    sources: list
    reconciled: str


def fact(conn, metric: str, target: date, imported=None) -> MetricFact:
    imported = imported if imported is not None else last_import(conn)
    series = day_values(conn, metric, target - timedelta(days=31), target, imported)
    dv = series.get(target.isoformat())
    label, unit, _, note = METRICS.get(metric, (metric, "", "mean", ""))
    return MetricFact(metric, label, unit, target.isoformat(),
                      dv.value if dv else None,
                      dv.complete if dv else is_complete(target, imported),
                      baseline(series, target, 7), baseline(series, target, 30),
                      note, dv.sources if dv else [], dv.reconciled if dv else "")


def coverage(conn, now: datetime | None = None) -> dict:
    """What the store holds: import time, latest measurement day, range,
    per-metric day counts, sleep/workout availability."""
    now = now or datetime.now(timezone.utc)
    imp = last_import(conn)
    r = conn.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) "
                     "FROM health_metric_samples").fetchone()
    per = {m: n for m, n in conn.execute(
        "SELECT metric, COUNT(DISTINCT date) FROM health_metric_samples GROUP BY metric")}
    sleep_n = conn.execute("SELECT COUNT(*) FROM sleep_sessions").fetchone()[0]
    sleep_n += conn.execute("SELECT COUNT(*) FROM health_metric_samples "
                            "WHERE metric LIKE 'sleep%'").fetchone()[0]
    work_n = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
    srcs = sorted({s for (s,) in conn.execute(
        "SELECT DISTINCT source FROM health_metric_samples") for s in (s or "").split("|") if s})
    age_h = (now - imp).total_seconds() / 3600 if imp else None
    return {"last_import": imp, "import_age_h": age_h,
            "first_day": r[0], "last_day": r[1], "days": r[2] or 0,
            "per_metric_days": per, "sleep_records": sleep_n,
            "workouts": work_n, "devices": srcs, "today": local_day(now).isoformat()}


def fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return "no data"
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}"
    return f"{s} {unit}".strip()


def describe(f: MetricFact, show_missing_baseline: bool = True) -> str:
    """One honest line: value, completeness, baseline (or why there is none)."""
    if f.value is None:
        return f"{f.label}: no data for {f.day}"
    s = f"{f.label}: {fmt(f.value, f.unit)}"
    if not f.complete and f.metric in SUMMED:
        s += " (day not finished or not fully synced: a running total, not a daily total)"
    elif not f.complete:
        s += " (partial day)"
    b = f.b7 if f.b7.ok else f.b30
    if b.ok:
        pct = 100 * (f.value - b.mean) / b.mean if b.mean else 0
        s += f"; {b.window}-day average {fmt(b.mean, f.unit)} ({pct:+.0f}%, n={b.n} days)"
    elif show_missing_baseline:
        s += f"; no personal baseline yet ({f.b7.n} complete prior days, need {f.b7.need})"
    return s


def series_for_chart(conn, metric: str, end: date, days: int) -> list[tuple]:
    """[(iso day, value|None, complete)] for every day in range, gaps kept."""
    imp = last_import(conn)
    start = end - timedelta(days=days - 1)
    s = day_values(conn, metric, start, end, imp)
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        dv = s.get(d)
        out.append((d, dv.value if dv else None, dv.complete if dv else False))
    return out
