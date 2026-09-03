"""
core.cron — a strict, dependency-free cron parser.

This module exists because of one specific incident: the Aug 26 failure where
a `CRON_TZ` line and a DST shift silently moved a job. See
`incident-2026-08-26-move-and-config-parse-failure`.

The rule that came out of it, and that this module enforces:

    **Every schedule in Spine is UTC. Always. No CRON_TZ, no exceptions.**

UTC schedules do not drift twice a year, and a schedule that does not drift
is one you can reason about in March without remembering what the clocks did.
Human-facing *windows* (see core.window) are evaluated in local time at run
time, which is the only place the ambiguity is allowed to live.

Supported syntax, deliberately narrow:

    *            every value
    5            a literal
    1,5,9        a list
    1-5          a range
    */15         a step over the whole field
    1-20/5       a step over a range

Not supported, deliberately: @reboot, @daily and friends (implicit and
untestable), names like MON or JAN (locale-adjacent), and `?` / `L` / `W`
(Quartz extensions that vanilla cron does not implement).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("dom", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 6),
)

_TOKEN = re.compile(r"^(?:\*|\d+(?:-\d+)?)(?:/\d+)?$")


class CronError(ValueError):
    """A schedule string that cron would accept but a human would misread,
    or one it would reject outright. Either way we refuse it at load time
    rather than at 3am."""


def _parse_field(spec: str, lo: int, hi: int, name: str) -> set[int]:
    values: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            raise CronError(f"{name}: empty element in {spec!r}")
        if not _TOKEN.match(token):
            raise CronError(f"{name}: cannot parse {token!r}")

        body, _, step_s = token.partition("/")
        step = 1
        if step_s:
            step = int(step_s)
            if step < 1:
                raise CronError(f"{name}: step must be >= 1 in {token!r}")

        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, _, b = body.partition("-")
            start, end = int(a), int(b)
            if start > end:
                raise CronError(
                    f"{name}: range {body!r} runs backwards; cron will not "
                    f"wrap it the way you are hoping")
        else:
            start = end = int(body)
            if step_s:
                raise CronError(
                    f"{name}: {token!r} means 'every {step} starting at "
                    f"{start}', which vanilla cron does not do. Write a "
                    f"range: {start}-{hi}/{step}")

        if start < lo or end > hi:
            raise CronError(
                f"{name}: {body!r} outside valid range {lo}-{hi}")

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"{name}: {spec!r} matches nothing")
    return values


def parse(schedule: str) -> dict[str, set[int]]:
    """Parse a 5-field UTC cron schedule into sets of matching values."""
    if not isinstance(schedule, str):
        raise CronError(f"schedule must be a string, got {type(schedule).__name__}")
    if schedule.startswith("@"):
        raise CronError(
            f"{schedule!r}: shorthand schedules are not supported. Write the "
            "five fields out — implicit schedules are the ones nobody checks.")

    parts = schedule.split()
    if len(parts) != 5:
        raise CronError(
            f"{schedule!r}: expected 5 fields "
            "(minute hour day-of-month month day-of-week), "
            f"got {len(parts)}")

    return {
        name: _parse_field(part, lo, hi, name)
        for part, (name, lo, hi) in zip(parts, FIELDS)
    }


def matches(schedule: str, when: datetime) -> bool:
    """Does `when` (interpreted as UTC) fall on this schedule?

    Follows vanilla cron's day rule: when both day-of-month and day-of-week
    are restricted, either one matching is enough. This surprises people, so
    it is spelled out rather than left implicit.
    """
    f = parse(schedule)
    when = when.astimezone(timezone.utc) if when.tzinfo else when.replace(
        tzinfo=timezone.utc)

    if when.minute not in f["minute"] or when.hour not in f["hour"]:
        return False
    if when.month not in f["month"]:
        return False

    dom_restricted = len(f["dom"]) < 31
    dow_restricted = len(f["dow"]) < 7
    dom_ok = when.day in f["dom"]
    dow_ok = (when.weekday() + 1) % 7 in f["dow"]  # cron: Sunday == 0

    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def fire_times_utc(schedule: str, limit: int = 64) -> list[tuple[int, int]]:
    """The distinct (hour, minute) pairs in UTC this schedule fires at.

    Used by the registry to check that a night-window job actually lands in
    the night once local time and DST are accounted for.
    """
    f = parse(schedule)
    out = sorted((h, m) for h in sorted(f["hour"]) for m in sorted(f["minute"]))
    return out[:limit]


def describe(schedule: str) -> str:
    """A short human gloss, for the console and for crontab comments."""
    f = parse(schedule)
    parts = schedule.split()
    if parts[0].startswith("*/") and parts[1] == "*":
        every = f"every {parts[0][2:]} min"
        rest = " ".join(parts[2:])
        return every if rest == "* * *" else f"{every}, on a restricted calendar"
    if parts[1].startswith("*/") and parts[0].isdigit():
        return f"every {parts[1][2:]}h at :{int(parts[0]):02d} UTC"
    times = fire_times_utc(schedule, limit=3)
    n = len(f["hour"]) * len(f["minute"])
    stamp = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    if n > len(times):
        stamp += f", +{n - len(times)} more"
    if len(f["dom"]) == 31 and len(f["dow"]) == 7 and len(f["month"]) == 12:
        return f"daily at {stamp} UTC"
    return f"{stamp} UTC, on a restricted calendar"
