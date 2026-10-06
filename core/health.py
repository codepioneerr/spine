"""
core.health — Apple Health data from the Health Auto Export iOS app.

Its own database, `var/health.db`, not spine.db. Health data is reference
data about Nick, not something he acts on, so per CLAUDE.md 7a it does not
belong in `items`. Keeping it in a separate file also means the most
sensitive data on the box can be backed up, wiped or permissioned on its
own.

The app has five export streams, all POSTed to the same route. Each is a
key under `data`:

    metrics         -> health_metric_samples (every point, any metric name)
                       + daily_vitals and sleep_sessions (derived summaries)
    workouts        -> workouts
    symptoms        -> symptoms
    ecg             -> ecg_recordings (voltages zlib-compressed)
    stateOfMind     -> state_of_mind

## Nothing is dropped, nothing 500s

The raw body is stored (zlib, `raw_payloads`) BEFORE parsing, and parsing
is per record: a record that does not parse is counted in `errors`, the rest
of the payload still lands. Unknown top-level keys are counted as
`unhandled` and survive in the raw copy, so a shape this code has never seen
can be handled later with `python3 -m core.health --reprocess` rather than
lost. Raw copies are pruned after RAW_RETENTION_DAYS.

`health_metric_samples` is the extensible table: one row per (metric, ts,
source) for every metric the app sends, including the sparse ones (gait,
nutrition) that have no column anywhere. New metrics need no schema change.

## Idempotency, stated because it is the whole design

The app re-sends overlapping windows on every sync. Every write is an upsert
keyed on something stable across sends:

  - samples on (metric, ts, source); vitals on the date. Within one payload a
    day's samples are aggregated (sum for steps/energy, mean for the rest)
    and that aggregate REPLACES the stored value — re-sending never
    double-counts. Configure the app with "Aggregate data: by day".
  - A metric absent from a payload leaves that vitals column alone
    (COALESCE), so a send carrying only steps does not null out HRV.
  - sleep on its start time; workouts on the app's id (else a hash of type +
    start); symptoms on name + start; ECG on start; state of mind on its id.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core import paths

RAW_RETENTION_DAYS = 30

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
CREATE TABLE IF NOT EXISTS health_metric_samples (
    metric      TEXT NOT NULL,
    ts          TEXT NOT NULL,       -- ISO 8601 with the phone's offset
    date        TEXT NOT NULL,       -- local calendar date of ts
    source      TEXT NOT NULL DEFAULT '',
    units       TEXT,
    qty         REAL,
    min         REAL,
    avg         REAL,
    max         REAL,
    value_text  TEXT,                -- e.g. sleep stage name
    extra_json  TEXT,                -- any other fields, verbatim
    PRIMARY KEY (metric, ts, source)
);
CREATE INDEX IF NOT EXISTS samples_by_date ON health_metric_samples(date, metric);
CREATE TABLE IF NOT EXISTS symptoms (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    start_time  TEXT,
    end_time    TEXT,
    severity    TEXT,
    source      TEXT
);
CREATE TABLE IF NOT EXISTS ecg_recordings (
    id                  TEXT PRIMARY KEY,
    start_time          TEXT,
    end_time            TEXT,
    classification      TEXT,
    severity            TEXT,
    avg_heart_rate      REAL,
    sampling_frequency  REAL,
    sample_count        INTEGER,
    source              TEXT,
    voltages_zlib       BLOB         -- zlib(JSON list of [voltage...])
);
CREATE TABLE IF NOT EXISTS state_of_mind (
    id                     TEXT PRIMARY KEY,
    start_time             TEXT,
    end_time               TEXT,
    kind                   TEXT,
    valence                REAL,
    valence_classification TEXT,
    labels_json            TEXT,
    associations_json      TEXT
);
CREATE TABLE IF NOT EXISTS raw_payloads (
    sha256       TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    bytes        INTEGER,
    keys         TEXT,
    body_zlib    BLOB
);
"""

# Health Auto Export metric name -> (daily_vitals column, aggregation)
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
_AGG = {c: a for c, a in METRICS.values()}

STREAMS = ("metrics", "workouts", "symptoms", "ecg", "stateOfMind")
_SAMPLE_FIELDS = {"date", "qty", "Min", "Avg", "Max", "min", "avg", "max",
                  "source", "value", "units", "startDate", "endDate"}


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
    v = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value) -> str | None:
    ts = _parse_ts(value)
    return ts.isoformat() if ts else None


def _num(value) -> float | None:
    """A number, or the `qty` of a {qty, units} object."""
    if isinstance(value, dict):
        value = value.get("qty", value.get("Avg", value.get("avg")))
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _minutes(hours) -> float | None:
    h = _num(hours)
    return round(h * 60, 1) if h is not None else None


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _list(data: dict, key: str) -> list:
    v = data.get(key) or []
    if not isinstance(v, list):
        raise ValueError(f"data.{key} must be a list")
    return v


# ── raw store ────────────────────────────────────────────────────────

def store_raw(body: bytes, conn: sqlite3.Connection, keys: str = "") -> str:
    """Keep the exact bytes the phone sent. Returns the sha256."""
    digest = hashlib.sha256(body).hexdigest()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RAW_RETENTION_DAYS)).isoformat()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO raw_payloads VALUES (?,?,?,?,?)",
            (digest, now.isoformat(), len(body), keys, zlib.compress(body, 6)))
        conn.execute("DELETE FROM raw_payloads WHERE received_at < ?", (cutoff,))
    return digest


# ── ingestion ────────────────────────────────────────────────────────

def ingest(payload: dict, conn: sqlite3.Connection) -> dict:
    """Upsert one Health Auto Export payload (any of the five streams).

    Returns counts per table plus `errors` (records that failed to parse) and
    `unhandled` (unknown top-level keys). Raises ValueError only when the
    payload is not an object of the expected outer shape."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("payload.data must be an object")
    streams = {k: _list(data, k) for k in STREAMS}

    out = defaultdict(int)
    errors: list[str] = []
    unhandled = sorted(k for k in data if k not in STREAMS)

    def guarded(label, fn, rec):
        try:
            return fn(rec)
        except Exception as exc:  # one bad record never sinks the payload
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:80]}")
            return None

    samples, vitals_acc, sleep_rows = [], defaultdict(lambda: defaultdict(list)), []
    for metric in streams["metrics"]:
        if not isinstance(metric, dict) or not metric.get("name"):
            errors.append("metrics: entry without a name")
            continue
        name, units = str(metric["name"]), metric.get("units")
        entries = metric.get("data") or []
        if not isinstance(entries, list):
            errors.append(f"metrics.{name}: data is not a list")
            continue
        if name == "sleep_analysis":
            rows = _sleep_rows([e for e in entries if isinstance(e, dict)], errors)
            sleep_rows.extend(rows)
        for e in entries:
            row = guarded(f"metrics.{name}", lambda r: _sample_row(name, units, r), e)
            if not row:
                continue
            samples.append(row)
            if name in METRICS and row[5] is not None:
                vitals_acc[row[2]][METRICS[name][0]].append(row[5])

    workouts = [r for r in (guarded("workouts", _workout_row, w)
                            for w in streams["workouts"]) if r]
    symptoms = [r for r in (guarded("symptoms", _symptom_row, s)
                            for s in streams["symptoms"]) if r]
    ecgs = [r for r in (guarded("ecg", _ecg_row, e) for e in streams["ecg"]) if r]
    moods = [r for r in (guarded("stateOfMind", _mood_row, m)
                         for m in streams["stateOfMind"]) if r]

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO health_metric_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            samples)
        for day, cols in vitals_acc.items():
            row = {c: None for c in VITAL_COLUMNS}
            for col, vals in cols.items():
                v = sum(vals) if _AGG[col] == "sum" else sum(vals) / len(vals)
                row[col] = int(round(v)) if col == "step_count" else round(v, 3)
            cl = ", ".join(VITAL_COLUMNS)
            marks = ", ".join("?" for _ in VITAL_COLUMNS)
            upd = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in VITAL_COLUMNS)
            conn.execute(
                f"INSERT INTO daily_vitals (date, {cl}) VALUES (?, {marks}) "
                f"ON CONFLICT(date) DO UPDATE SET {upd}",
                [day] + [row[c] for c in VITAL_COLUMNS])
        conn.executemany("INSERT OR REPLACE INTO sleep_sessions VALUES (?,?,?,?,?,?,?,?,?)", sleep_rows)
        conn.executemany("INSERT OR REPLACE INTO workouts VALUES (?,?,?,?,?,?,?)", workouts)
        conn.executemany("INSERT OR REPLACE INTO symptoms VALUES (?,?,?,?,?,?)", symptoms)
        conn.executemany("INSERT OR REPLACE INTO ecg_recordings VALUES (?,?,?,?,?,?,?,?,?,?)", ecgs)
        conn.executemany("INSERT OR REPLACE INTO state_of_mind VALUES (?,?,?,?,?,?,?,?)", moods)

    out.update(health_metric_samples=len(samples), daily_vitals=len(vitals_acc),
               sleep_sessions=len(sleep_rows), workouts=len(workouts),
               symptoms=len(symptoms), ecg_recordings=len(ecgs),
               state_of_mind=len(moods))
    out = dict(out)
    out["errors"] = len(errors)
    if errors:
        out["error_samples"] = errors[:5]
    if unhandled:
        out["unhandled"] = unhandled
    return out


def _sample_row(name, units, e: dict):
    ts = _parse_ts(e.get("date") or e.get("startDate"))
    if ts is None:
        raise ValueError("no parseable date")
    lo, mid, hi = (_num(e.get(k, e.get(k.lower()))) for k in ("Min", "Avg", "Max"))
    qty = _num(e.get("qty"))
    if qty is None:
        qty = mid
    value = e.get("value")
    value_text = value if isinstance(value, str) else None
    if qty is None and value_text is None and not isinstance(value, (int, float)):
        extra_only = True
    else:
        extra_only = False
    if qty is None and isinstance(value, (int, float)) and not isinstance(value, bool):
        qty = float(value)
    extra = {k: v for k, v in e.items() if k not in _SAMPLE_FIELDS}
    if e.get("endDate"):
        extra["endDate"] = e["endDate"]
    if extra_only and not extra:
        raise ValueError("no value")
    return (name, ts.isoformat(), ts.date().isoformat(), str(e.get("source") or ""),
            units, qty, lo, mid, hi, value_text,
            json.dumps(extra, sort_keys=True) if extra else None)


_STAGES = {"deep": "deep", "rem": "rem", "core": "core", "awake": "awake",
           "asleep": "core", "inbed": "inbed", "in bed": "inbed"}


SESSION_GAP_MIN = 120   # stage segments further apart than this are separate sleeps


def _sleep_rows(entries: list[dict], errors: list) -> list[tuple]:
    """Aggregated entries (totalSleep/deep/rem...) map one-to-one. Raw stage
    segments (value + startDate/endDate) are clustered into continuous
    sessions (gaps < SESSION_GAP_MIN) and keyed by the session's start, so a
    night crossing midnight stays ONE night. (Grouping by each segment's own
    end date split 23:00-00:30 into two "nights" - fixed 2026-10-06.)
    Idempotent as long as the whole night is sent."""
    rows, segs = [], []
    for e in entries:
        try:
            if any(k in e for k in ("totalSleep", "asleep", "deep", "rem", "core")) and "value" not in e:
                r = _sleep_summary(e)
                if r:
                    rows.append(r)
                continue
            stage = _STAGES.get(str(e.get("value", "")).strip().lower())
            s, t = _parse_ts(e.get("startDate")), _parse_ts(e.get("endDate"))
            if not stage or not s or not t:
                continue
            segs.append((s, t, stage))
        except Exception as exc:
            errors.append(f"sleep_analysis: {type(exc).__name__}: {str(exc)[:80]}")
    segs.sort(key=lambda x: x[0])
    sessions: list[list] = []
    for s, t, stage in segs:
        if sessions and (s - sessions[-1][1]).total_seconds() / 60 <= SESSION_GAP_MIN:
            cur = sessions[-1]
            cur[1] = max(cur[1], t)
        else:
            cur = [s, t, defaultdict(float)]
            sessions.append(cur)
        cur[2][stage] += (t - s).total_seconds() / 60
    for s, t, st in sessions:
        total = st["deep"] + st["rem"] + st["core"]
        in_bed = st["inbed"] or (total + st["awake"])
        eff = round(100 * total / in_bed, 1) if in_bed else None
        rows.append((s.isoformat(), s.isoformat(), t.isoformat(), round(total, 1),
                     round(st["deep"], 1), round(st["rem"], 1), round(st["core"], 1),
                     round(st["awake"], 1), eff))
    return rows


def _sleep_summary(e: dict):
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
    if not isinstance(w, dict):
        raise ValueError("not an object")
    start = _iso(w.get("start"))
    wtype = w.get("name") or w.get("workout_type") or "Unknown"
    if not start:
        raise ValueError("no start")
    wid = w.get("id") or _hid(wtype, start)
    duration_s = _num(w.get("duration"))
    if duration_s is None:
        end = _parse_ts(w.get("end"))
        duration_s = (end - _parse_ts(w.get("start"))).total_seconds() if end else None
    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    energy = _num(w.get("activeEnergyBurned") or w.get("activeEnergy"))
    avg_hr = _num(w.get("avgHeartRate") or hr.get("avg"))
    max_hr = _num(w.get("maxHeartRate") or hr.get("max"))
    return (str(wid), str(wtype), start,
            round(duration_s / 60, 1) if duration_s is not None else None,
            energy, avg_hr, max_hr)


def _symptom_row(s: dict):
    if not isinstance(s, dict):
        raise ValueError("not an object")
    start = _iso(s.get("start") or s.get("date"))
    name = s.get("name")
    if not start or not name:
        raise ValueError("symptom needs name and start")
    sev = s.get("severity")
    return (_hid(name, start), str(name), start, _iso(s.get("end")),
            None if sev is None else str(sev), s.get("source"))


def _ecg_row(e: dict):
    if not isinstance(e, dict):
        raise ValueError("not an object")
    start = _iso(e.get("start"))
    if not start:
        raise ValueError("no start")
    volts = e.get("voltageMeasurements") or []
    series = [_num(v.get("voltage") if isinstance(v, dict) else v) for v in volts]
    blob = zlib.compress(json.dumps(series).encode(), 6) if series else None
    return (start, start, _iso(e.get("end")), e.get("classification"),
            e.get("severity"), _num(e.get("averageHeartRate")),
            _num(e.get("samplingFrequency")),
            int(_num(e.get("numberOfVoltageMeasurements")) or len(series)),
            e.get("source"), blob)


def _mood_row(m: dict):
    if not isinstance(m, dict):
        raise ValueError("not an object")
    start = _iso(m.get("start") or m.get("date"))
    if not start:
        raise ValueError("no start")
    return (str(m.get("id") or _hid(start, m.get("kind"))), start, _iso(m.get("end")),
            m.get("kind"), _num(m.get("valence")), m.get("valenceClassification"),
            json.dumps(m.get("labels") or []), json.dumps(m.get("associations") or []))


def reprocess(conn: sqlite3.Connection) -> dict:
    """Re-ingest every stored raw payload with the current parser."""
    total = defaultdict(int)
    for (blob,) in conn.execute("SELECT body_zlib FROM raw_payloads ORDER BY received_at").fetchall():
        try:
            result = ingest(json.loads(zlib.decompress(blob)), conn)
        except ValueError:
            total["rejected"] += 1
            continue
        for k, v in result.items():
            if isinstance(v, int):
                total[k] += v
    return dict(total)


if __name__ == "__main__":
    if sys.argv[1:] == ["--reprocess"]:
        c = connect()
        print(json.dumps(reprocess(c), indent=2))
        c.close()
    else:
        sys.exit("usage: python3 -m core.health --reprocess")
