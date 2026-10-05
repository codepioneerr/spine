"""
core.governor — the resource governor.

Nick's constraint, verbatim: *"I just don't want it to run twenty four hours
where I don't have space on my CPU or my RAM... I want the computer to be
smart about what it is building."* And: Telegram must stay responsive all
day.

Three gates, in order, before any job is allowed to start:

    1. enabled       — is it turned on at all
    2. window        — is now the right time of day for this weight
    3. RAM guard     — is there actually room, right now

Every refusal is a **skip**, logged with a reason. Never a wait, never a
swap, never an OOM. A skipped run is recoverable; an OOM-killed box at 3am
is not.

Schedules are UTC (core.cron). Windows are **local**, because "night" is a
fact about Nick's day, not about UTC. That split is deliberate and it is the
only place local time is allowed to matter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

DEFAULT_TZ = "America/New_York"
DEFAULT_NIGHT_START = "01:00"
DEFAULT_NIGHT_END = "06:00"
DEFAULT_HEADROOM_MB = 1024


@dataclass(frozen=True)
class Decision:
    """The verdict, plus which gate produced it.

    `gate` is here so a caller can act on *why* it was refused without
    pattern-matching the prose in `reason`. The runner needs that distinction:
    a window refusal is routine bookkeeping (a night job invoked at noon), but
    a RAM refusal means a job Nick expected simply did not happen, and those
    two deserve very different treatment. Matching on reason text would make
    every future reword of a message a silent behaviour change.

    Defaulted so existing two-argument construction keeps working.
    """
    ok: bool
    reason: str
    gate: str = ""
    # What the guard actually observed, carried so a caller reporting the
    # refusal quotes the number the decision was MADE on. Re-reading
    # /proc/meminfo a moment later can hand back a different figure, and an
    # alert saying "had 5099 MB, needs 4524 MB" next to a refusal is worse
    # than no alert — it reads as a bug in the guard.
    avail_mb: int | None = None

    def __bool__(self) -> bool:
        return self.ok


def _parse_hhmm(value: str, fallback: str) -> time:
    try:
        h, _, m = value.partition(":")
        return time(int(h), int(m or 0))
    except (ValueError, AttributeError):
        h, _, m = fallback.partition(":")
        return time(int(h), int(m or 0))


@dataclass(frozen=True)
class Settings:
    tz: str = DEFAULT_TZ
    night_start: time = time(1, 0)
    night_end: time = time(6, 0)
    headroom_mb: int = DEFAULT_HEADROOM_MB

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = env if env is not None else os.environ
        try:
            headroom = int(e.get("SPINE_RAM_HEADROOM_MB", DEFAULT_HEADROOM_MB))
        except (TypeError, ValueError):
            headroom = DEFAULT_HEADROOM_MB
        return cls(
            tz=e.get("SPINE_TZ", DEFAULT_TZ),
            night_start=_parse_hhmm(e.get("SPINE_NIGHT_START", ""),
                                    DEFAULT_NIGHT_START),
            night_end=_parse_hhmm(e.get("SPINE_NIGHT_END", ""),
                                  DEFAULT_NIGHT_END),
            headroom_mb=max(0, headroom),
        )

    def localize(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if ZoneInfo is None:
            return when
        try:
            return when.astimezone(ZoneInfo(self.tz))
        except Exception:
            return when.astimezone()

    def is_night(self, when: datetime) -> bool:
        """Is `when` inside the night window, in local time?

        Handles a window that crosses midnight (23:00-05:00) as well as one
        that does not (01:00-06:00).
        """
        t = self.localize(when).time()
        if self.night_start <= self.night_end:
            return self.night_start <= t < self.night_end
        return t >= self.night_start or t < self.night_end


# ─────────────────────────────────────────────────────────────────────────────
# memory
# ─────────────────────────────────────────────────────────────────────────────

def available_mb() -> int | None:
    """Free memory in MB, or None where /proc is not available.

    Uses MemAvailable, which is the kernel's own estimate of what a new
    process can get without swapping — not MemFree, which excludes reclaimable
    page cache and would make a healthy box look full.
    """
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# the gates
# ─────────────────────────────────────────────────────────────────────────────

def check_window(job, when: datetime, settings: Settings) -> Decision:
    if job.window == "any":
        return Decision(True, "window=any", gate="window")

    night = settings.is_night(when)
    local = settings.localize(when).strftime("%H:%M %Z")

    if job.window == "night":
        if night:
            return Decision(True, f"night window, local {local}", gate="window")
        return Decision(
            False,
            f"outside night window ({settings.night_start:%H:%M}-"
            f"{settings.night_end:%H:%M} local); now {local}",
            gate="window")

    if night:
        return Decision(
            False, f"day-window job, but it is night locally ({local})",
            gate="window")
    return Decision(True, f"day window, local {local}", gate="window")


def check_ram(job, settings: Settings, avail_mb: int | None = None) -> Decision:
    avail = available_mb() if avail_mb is None else avail_mb
    if avail is None:
        # No /proc: not the Dell. Do not block, but say so — silently
        # skipping the guard is how it gets forgotten.
        return Decision(True, "RAM guard unavailable (no /proc); not enforced",
                        gate="ram")

    need = job.ram_required_mb(settings.headroom_mb)
    if avail >= need:
        return Decision(True, f"{avail} MB free, needs {need} MB", gate="ram",
                        avail_mb=avail)
    return Decision(
        False,
        f"insufficient RAM: {avail} MB free, needs {need} MB "
        f"({job.ram_mb} + {need - job.ram_mb} headroom). Skipped, not queued.",
        gate="ram", avail_mb=avail)


def admit(job, when: datetime | None = None,
          settings: Settings | None = None,
          avail_mb: int | None = None) -> Decision:
    """The single entry point. Returns the first refusal, or an approval."""
    when = when or datetime.now(timezone.utc)
    settings = settings or Settings.from_env()

    if not job.enabled:
        return Decision(False, "disabled in META", gate="enabled")

    win = check_window(job, when, settings)
    if not win:
        return win

    ram = check_ram(job, settings, avail_mb)
    if not ram:
        return ram

    return Decision(True, f"{win.reason}; {ram.reason}")


# ─────────────────────────────────────────────────────────────────────────────
# static analysis — catches the DST bug class before it ships
# ─────────────────────────────────────────────────────────────────────────────

def audit_schedule(job, settings: Settings | None = None) -> list[str]:
    """Warn where a job's UTC schedule and its declared window disagree.

    This is the check that would have caught the Aug 26 incident. A heavy job
    scheduled at 06:30 UTC is 01:30 in winter and 02:30 in summer — fine. One
    at 10:30 UTC is 05:30 winter, 06:30 summer, so it silently stops running
    for half the year. Cron never complains; this does.
    """
    from core import cron

    settings = settings or Settings.from_env()
    if job.window == "any" or ZoneInfo is None:
        return []

    warnings: list[str] = []
    # One winter date and one summer date: covers both UTC offsets.
    probes = ((1, 15, "winter"), (7, 15, "summer"))

    for hh, mm in cron.fire_times_utc(job.schedule):
        for month, day, season in probes:
            when = datetime(2026, month, day, hh, mm, tzinfo=timezone.utc)
            night = settings.is_night(when)
            local = settings.localize(when).strftime("%H:%M")
            wants_night = job.window == "night"
            if night != wants_night:
                warnings.append(
                    f"{job.id}: fires {hh:02d}:{mm:02d} UTC = {local} local in "
                    f"{season}, which is outside its '{job.window}' window — "
                    f"it will be skipped for half the year. "
                    f"Adjust the UTC schedule.")
    return warnings
