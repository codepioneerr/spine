"""
core.health — Apple Health data from the Health Auto Export iOS app.

Its own database, `var/health.db`, not spine.db. Health data is reference
data about Nick, not something he acts on, so per CLAUDE.md 7a it does not
belong in `items`. Keeping it in a separate file also means the most
sensitive data on the box can be backed up, wiped or permissioned on its
own.

    daily_vitals(date PK, ...)       one row per calendar day
    sleep_sessions(id PK, ...)       id = sleep start timestamp
    workouts(id PK, ...)             id = the app's workout UUID

## Idempotency, stated because it is the whole design

The app re-sends overlapping windows on every sync. So every write is an
upsert keyed on something stable across sends:

  - vitals key on the date. Within one payload, samples for a day are
    aggregated (sum for steps/energy, mean for the rest), and that aggregate
    REPLACES the stored value. Re-sending a payload never double-counts.
    Configure the app with "Aggregate data: by day" so each send carries
    the whole day, not just a window of it.
  - A metric absent from a payload leaves that column alone (COALESCE), so
    a send carrying only steps does not null out yesterday's HRV.
  - sleep keys on its start time; workouts on the app's id, falling back to
    a hash of type + start for exports that omit it.

Unknown metric names are ignored, not guessed at.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from core import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_vitals (
    date                   TEXT PRIMARY KEY,
    step_count             INTEGER,
    active_calories        REAL,
    resting_hr_avg         REAL,
    hrv_avg                REAL,
    wrist_temp_delta       REAL,
    respiratory_rate       REAL,
    oxygen_saturation_avg  REAL
);
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id                    TEXT PRIMARY KEY,
    start_time            TEXT,
    end_time              TEXT,
    total_sleep_minutes   REAL,
    deep_sleep_minutes    REAL,
    rem_sleep_minutes     REAL,
    core_sleep_minutes    REAL,
    awake_minutes         REAL,
    sleep_efficiency_pct  REAL
);
CREATE TABLE IF NOT EXISTS workouts (
    id                  TEXT PRIMARY KEY,
    workout_type        TEXT,
    start_time          TEXT,
    duration_minutes    REAL,
    active_energy_kcal  REAL,
    avg_heart_rate      REAL,
    max_heart_rate      REAL
);
"""

# Health Auto Export metric name -> (column, aggregation within a day)
METRICS = {
    "step_count":                       ("step_count", "sum"),
    "active_energy":                    ("active_calories", "sum"),
    "resting_heart_rate":               ("resting_hr_avg", "mean"),
    "heart_rate_variability":           ("hrv_avg", "mean"),
    "heart_rate_variability_sdnn":      ("hrv_avg", "mean"),
    "apple_sleeping_wrist_temperature": ("wrist_temp_delta", "mean"),
    "respiratory_rate":                 ("respiratory_rate", "mean"),
    "blood_oxygen_saturation":          ("oxygen_saturation_avg", "mean"),
    "oxygen_saturation":                ("oxygen_saturation_avg", "mean"),
}
VITAL_COLUMNS = sorted({c for c, _ in METRICS.values()})


def db_path() -> str:
    """SPINE_HEALTH_DB may relocate it; the default is var/health.db."""
    return os.environ.get("SPINE_HEALTH_DB") or paths.var("health.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


# ── parsing helpers ──────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _iso(value) -> str | None:
    ts = _parse_ts(value)
    return ts.isoformat() if ts else None


def _num(value) -> float | None:
    """A number, or the `qty` of a {qty, units} object."""
    if isinstance(value, dict):
        value = value.get("qty", value.get("Avg"))
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _minutes(hours) -> float | None:
    h = _num(hours)
    return round(h * 60, 1) if h is not None else None


# ── ingestion ────────────────────────────────────────────────────────

def ingest(payload: dict, conn: sqlite3.Connection) -> dict:
    """Upsert one Health Auto Export payload. Returns counts per table.
    Raises ValueError on a payload that is not the expected shape."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("payload.data must be an object")
    metrics = data.get("metrics") or []
    workouts = data.get("workouts") or []
    if not isinstance(metrics, list) or not isinstance(workouts, list):
        raise ValueError("metrics and workouts must be lists")

    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    sleep_rows = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        name = metric.get("name")
        entries = metric.get("data") or []
        if name == "sleep_analysis":
            sleep_rows.extend(_sleep_row(e) for e in entries if isinstance(e, dict))
            continue
        if name not in METRICS:
            continue
        column, _ = METRICS[name]
        for e in entries:
            if not isinstance(e, dict):
                continue
            ts = _parse_ts(e.get("date"))
            qty = _num(e.get("qty", e.get("Avg")))
            if ts is None or qty is None:
                continue
            samples[ts.date().isoformat()][column].append(qty)

    aggregation = {c: a for c, a in METRICS.values()}
    vitals = []
    for day, cols in samples.items():
        row = {c: None for c in VITAL_COLUMNS}
        for col, vals in cols.items():
            v = sum(vals) if aggregation[col] == "sum" else sum(vals) / len(vals)
            row[col] = int(round(v)) if col == "step_count" else round(v, 3)
        vitals.append((day, row))

    sleep_rows = [r for r in sleep_rows if r]
    workout_rows = [r for r in (_workout_row(w) for w in workouts if isinstance(w, dict)) if r]

    with conn:
        for day, row in vitals:
            cols = ", ".join(VITAL_COLUMNS)
            marks = ", ".join("?" for _ in VITAL_COLUMNS)
            updates = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in VITAL_COLUMNS)
            conn.execute(
                f"INSERT INTO daily_vitals (date, {cols}) VALUES (?, {marks}) "
                f"ON CONFLICT(date) DO UPDATE SET {updates}",
                [day] + [row[c] for c in VITAL_COLUMNS])
        for r in sleep_rows:
            conn.execute(
                "INSERT OR REPLACE INTO sleep_sessions VALUES (?,?,?,?,?,?,?,?,?)", r)
        for r in workout_rows:
            conn.execute(
                "INSERT OR REPLACE INTO workouts VALUES (?,?,?,?,?,?,?)", r)

    return {"daily_vitals": len(vitals), "sleep_sessions": len(sleep_rows),
            "workouts": len(workout_rows)}


def _sleep_row(e: dict):
    start = _iso(e.get("sleepStart") or e.get("startDate") or e.get("inBedStart"))
    if not start:
        return None
    end = _iso(e.get("sleepEnd") or e.get("endDate") or e.get("inBedEnd"))
    total = _minutes(e.get("totalSleep", e.get("asleep")))
    awake = _minutes(e.get("awake"))
    in_bed = _minutes(e.get("inBed"))
    if not in_bed and total is not None:
        in_bed = total + (awake or 0)
    eff = round(100 * total / in_bed, 1) if total is not None and in_bed else None
    return (start, start, end, total, _minutes(e.get("deep")), _minutes(e.get("rem")),
            _minutes(e.get("core")), awake, eff)


def _workout_row(w: dict):
    start = _iso(w.get("start"))
    wtype = w.get("name") or w.get("workout_type") or "Unknown"
    if not start:
        return None
    wid = w.get("id") or hashlib.sha1(f"{wtype}|{start}".encode()).hexdigest()[:16]
    duration_s = _num(w.get("duration"))
    if duration_s is None:
        end = _parse_ts(w.get("end"))
        duration_s = (end - _parse_ts(w.get("start"))).total_seconds() if end else None
    energy = _num(w.get("activeEnergyBurned") or w.get("activeEnergy"))
    avg_hr = _num(w.get("avgHeartRate") or (w.get("heartRate") or {}).get("avg"))
    max_hr = _num(w.get("maxHeartRate") or (w.get("heartRate") or {}).get("max"))
    return (str(wid), wtype, start,
            round(duration_s / 60, 1) if duration_s is not None else None,
            energy, avg_hr, max_hr)
