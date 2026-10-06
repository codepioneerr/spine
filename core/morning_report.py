"""
core.morning_report — one screen answering "did last night's runs work?"

Writes no data. Opening spine.db through core.proptech creates the
proptech tables if they are missing (empty, the same schema the collector
creates), so it is safe to run at any time, as often as you like. Built for the morning
after Phase 3/3b went live: the numbers that decide whether darkweb-jobs'
PLUTO slice needs widening are pluto_coverage_pct and the condo counts, and
until now they were only in a log line.

    bin/proptech-report
"""

from __future__ import annotations

from core import proptech, registry
from core.store import Store

JOBS = ("acris", "proptech", "brief")


def job_line(job_id: str, st: dict) -> str:
    """One line per job from its state file. Pure."""
    if not st:
        return f"{job_id:<9} never run"
    out = st.get("outcome", "?").upper()
    line = f"{job_id:<9} {out:<8} {st.get('finished_at', '?')}"
    if st.get("reason"):
        line += f"  ({st['reason']})"
    return line


def proptech_lines(stats: dict | None, counts: dict) -> list[str]:
    """Coverage and condo numbers: from the last run's stats when present,
    spine.db's live counts otherwise. Pure."""
    s = {**counts, **(stats or {})}
    return [
        f"documents kept    {s.get('docs', 0):,}",
        f"parcels linked    {s.get('parcels_linked', 0):,}",
        f"PLUTO coverage    {s.get('pluto_coverage_pct', 0.0)}%",
        f"  from slice      {s.get('parcels_dwj', 0):,}",
        f"  from lookups    {s.get('parcels_pluto', 0):,}",
        f"  absent          {s.get('parcels_absent', 0):,}",
        f"condo units       {s.get('condo_units_mapped', 0):,} mapped",
        f"last run asked    PLUTO {s.get('pluto_asked', '-')}, "
        f"condo {s.get('condo_asked', '-')}, failed batches "
        f"{s.get('pluto_failed_batches', '-')}/"
        f"{s.get('condo_failed_batches', '-')}",
        f"repeat sales      {s.get('repeat_sales', '-')}",
    ]


def brief_line(st: dict) -> str:
    stats = st.get("stats") or {}
    if st.get("outcome") != "ok":
        return "brief: not sent"
    if "judgment" not in stats:
        return "brief: sent"
    return ("brief: sent, with machine read" if stats["judgment"]
            else "brief: sent, machine read skipped (see var/log/brief.log)")


def render(states: dict, counts: dict, alerts: list) -> str:
    lines = ["== jobs"]
    lines += [job_line(j, states.get(j) or {}) for j in JOBS]
    lines += ["", "== proptech"]
    lines += proptech_lines((states.get("proptech") or {}).get("stats"), counts)
    lines += ["", "== alerts (open)"]
    lines += [f"[{a['importance']}] {a['title']}" for a in alerts] or ["none"]
    lines += ["", brief_line(states.get("brief") or {})]
    return "\n".join(lines)


def main() -> int:
    states = {j: registry.read_state(j) for j in JOBS}
    conn = proptech.connect()
    try:
        counts = proptech.counts(conn)
    finally:
        conn.close()
    store = Store(source="report")
    try:
        alerts = [dict(r) for r in store.query(kind="alert", status="new")]
    finally:
        store.close()
    print(render(states, counts, alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
