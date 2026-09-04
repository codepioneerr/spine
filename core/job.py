"""
core.job — the job contract.

Every collector in Spine is one file that declares a `META` dict and a
`run(ctx)` function. That is the whole interface. This module defines it,
validates it, and refuses to load anything that would misbehave on an 8 GB
box.

    # collectors/example.py
    META = {
        "id":       "example",
        "schedule": "0 6 * * *",    # UTC, always
        "timeout":  900,
        "ram_mb":   150,
        "window":   "night",        # night | day | any
        "weight":   "light",        # light | heavy
        "tier":     None,           # None | bulk | smart | frontier
    }

    def run(ctx):
        return {"items": [...], "stats": {...}}

**Validation happens at load time, not at run time.** A collector with a bad
schedule or a dishonest `ram_mb` fails when the registry is generated —
while Nick is sitting there — rather than at 03:00 on a Tuesday.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from core import cron

WINDOWS = ("night", "day", "any")
DATA_CLASSES = ("public", "private")
WEIGHTS = ("light", "heavy")
TIERS = (None, "bulk", "smart", "frontier")

# CLAUDE.md section 1: "A Python collector: < 200 MB -> light".
LIGHT_MAX_MB = 200

# Nothing may claim more than this. The box has 8 GB and ~1.5 GB is spoken
# for by the OS, Tailscale and Hermes before Spine starts.
HARD_MAX_MB = 3500

_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

REQUIRED = ("id", "schedule")
KNOWN = {"id", "schedule", "timeout", "ram_mb", "window", "weight", "tier",
         "data", "enabled", "description"}

DEFAULTS = {
    "timeout": 900,
    "ram_mb": 150,
    "window": "any",
    "weight": "light",
    "tier": None,
    # DEFAULTS TO PRIVATE, on purpose. A collector must actively declare its
    # data public before the router will send it to a logging endpoint. The
    # mistake being designed against is omission, so omission must be safe.
    "data": "private",
    "enabled": True,
    "description": "",
}


class JobError(ValueError):
    """A collector that cannot be trusted to run unattended."""


@dataclass(frozen=True)
class Job:
    id: str
    schedule: str
    timeout: int
    ram_mb: int
    window: str
    weight: str
    tier: str | None
    data: str
    enabled: bool
    description: str
    module: str = ""
    run: Callable[[Any], dict] | None = field(default=None, repr=False,
                                              compare=False)

    # ── derived ──────────────────────────────────────────────────────────

    @property
    def is_heavy(self) -> bool:
        return self.weight == "heavy"

    def ram_required_mb(self, headroom_mb: int) -> int:
        """What must be free before this job may start.

        Heavy jobs carry the full headroom margin. Light jobs are capped at
        200 MB by validation and are allowed to run against a smaller
        cushion — otherwise a busy box would starve the cheap work that
        keeps the item store fresh.
        """
        return self.ram_mb + (headroom_mb if self.is_heavy else headroom_mb // 4)

    def as_row(self) -> dict:
        return {
            "id": self.id,
            "schedule": self.schedule,
            "window": self.window,
            "weight": self.weight,
            "ram_mb": self.ram_mb,
            "tier": self.tier or "—",
            "data": self.data,
            "enabled": self.enabled,
        }


# ─────────────────────────────────────────────────────────────────────────────
# validation
# ─────────────────────────────────────────────────────────────────────────────

def validate(meta: dict, module: str = "") -> Job:
    """Turn a raw META dict into a Job, or raise JobError explaining why not.

    Every message names the collector and says what to change. An error that
    only says "invalid" costs more than the check saved.
    """
    where = f" in {module}" if module else ""

    if not isinstance(meta, dict):
        raise JobError(f"META must be a dict{where}, got {type(meta).__name__}")

    for key in REQUIRED:
        if key not in meta:
            raise JobError(f"META is missing {key!r}{where}")

    unknown = set(meta) - KNOWN
    if unknown:
        raise JobError(
            f"META has unknown key(s) {sorted(unknown)}{where}. "
            f"Known keys: {sorted(KNOWN)}. A typo'd key is silently ignored "
            "by every framework that does not do this check.")

    m = {**DEFAULTS, **meta}

    # id ------------------------------------------------------------------
    if not isinstance(m["id"], str) or not _SLUG.match(m["id"]):
        raise JobError(
            f"id {m['id']!r}{where} must be lowercase a-z0-9_, start with a "
            "letter, 2-32 chars. It becomes a filename, a crontab comment "
            "and a database key.")

    # schedule ------------------------------------------------------------
    try:
        cron.parse(m["schedule"])
    except cron.CronError as exc:
        raise JobError(f"{m['id']}{where}: bad schedule — {exc}") from exc

    # timeout -------------------------------------------------------------
    if not isinstance(m["timeout"], int) or isinstance(m["timeout"], bool) \
            or m["timeout"] < 1:
        raise JobError(f"{m['id']}{where}: timeout must be a positive int "
                       "(seconds)")
    if m["timeout"] > 6 * 3600:
        raise JobError(
            f"{m['id']}{where}: timeout {m['timeout']}s is longer than the "
            "night window. A job that can run that long holds the global "
            "flock and blocks everything else — split it.")

    # ram_mb --------------------------------------------------------------
    if not isinstance(m["ram_mb"], int) or isinstance(m["ram_mb"], bool) \
            or m["ram_mb"] < 1:
        raise JobError(f"{m['id']}{where}: ram_mb must be a positive int")
    if m["ram_mb"] > HARD_MAX_MB:
        raise JobError(
            f"{m['id']}{where}: ram_mb={m['ram_mb']} exceeds the "
            f"{HARD_MAX_MB} MB ceiling. The Dell has 8 GB with ~1.5 GB "
            "already spoken for. If the job really needs this, it does not "
            "belong on this box.")

    # window / weight -----------------------------------------------------
    if m["window"] not in WINDOWS:
        raise JobError(f"{m['id']}{where}: window must be one of {WINDOWS}")
    if m["weight"] not in WEIGHTS:
        raise JobError(f"{m['id']}{where}: weight must be one of {WEIGHTS}")

    # The two consistency rules that make the governor mean something.
    if m["weight"] == "light" and m["ram_mb"] > LIGHT_MAX_MB:
        raise JobError(
            f"{m['id']}{where}: weight='light' but ram_mb={m['ram_mb']}. "
            f"Light means under {LIGHT_MAX_MB} MB. Either the estimate is "
            "wrong or this job is heavy — say which.")
    if m["weight"] == "heavy" and m["window"] != "night":
        raise JobError(
            f"{m['id']}{where}: weight='heavy' requires window='night'. "
            "CLAUDE.md section 3 — heavy work does not run while Nick is "
            "using the box, and Telegram stays responsive during the day.")

    # tier ----------------------------------------------------------------
    if m["tier"] not in TIERS:
        raise JobError(
            f"{m['id']}{where}: tier must be one of {TIERS}. Jobs request a "
            "tier, never a model name — see CLAUDE.md section 10.")

    if m["data"] not in DATA_CLASSES:
        raise JobError(
            f"{m['id']}{where}: data must be one of {DATA_CLASSES}. "
            "'private' (the default) forbids logging/free endpoints; "
            "'public' is an explicit statement that this job handles nothing "
            "of Nick's. See core.models and CLAUDE.md section 10.")

    if not isinstance(m["enabled"], bool):
        raise JobError(f"{m['id']}{where}: enabled must be True or False")
    if not isinstance(m["description"], str):
        raise JobError(f"{m['id']}{where}: description must be a string")

    return Job(
        id=m["id"], schedule=m["schedule"], timeout=m["timeout"],
        ram_mb=m["ram_mb"], window=m["window"], weight=m["weight"],
        tier=m["tier"], data=m["data"], enabled=m["enabled"],
        description=m["description"],
        module=module,
    )


# ─────────────────────────────────────────────────────────────────────────────
# the context handed to run()
# ─────────────────────────────────────────────────────────────────────────────

class _Pending:
    """A capability that has not been built yet.

    Touching one raises with the phase that delivers it, instead of an
    AttributeError three frames deep. Same principle as the console: name
    the gap, do not fake the thing.
    """

    def __init__(self, name: str, phase: str):
        self._name, self._phase = name, phase

    def __getattr__(self, item):
        raise NotImplementedError(
            f"ctx.{self._name} is not available yet — it lands in {self._phase}.")

    def __call__(self, *a, **k):
        raise NotImplementedError(
            f"ctx.{self._name} is not available yet — it lands in {self._phase}.")

    def __repr__(self):
        return f"<pending ctx.{self._name}: {self._phase}>"


@dataclass
class Ctx:
    """What a collector is handed. Deliberately small.

    A collector gets exactly what it needs and nothing that would let it
    reach around the framework — no bare `os.environ`, no ad-hoc sqlite
    connection, no direct model client.
    """
    job: Job
    log: Callable[..., None]
    root: str
    now: datetime
    dry_run: bool = False
    secrets: Any = None
    db: Any = None
    http: Any = None
    models: Any = None
    notify: Any = None

    @classmethod
    def build(cls, job: Job, log, root: str, dry_run: bool = False) -> "Ctx":
        # Imported here rather than at module scope: core.models imports
        # core.costs which opens sqlite, and core.job must stay importable by
        # the registry without touching the database.
        from core import http, models, store
        secrets = _Secrets(root)
        return cls(
            job=job, log=log, root=root,
            now=datetime.now(timezone.utc), dry_run=dry_run,
            secrets=secrets,
            db=store.Store(source=job.id),
            http=http.Http(timeout=30),
            models=models.for_job(job, secrets=secrets, log=log),
            notify=_Pending("notify", "Phase 3 (Telegram)"),
        )


class _Secrets:
    """Read-only view over .env. The only path to a secret.

    Never logs a value, never returns the whole mapping, and refuses to read
    a world-readable file — a leaked key on a public-repo box is the one
    mistake with no undo.
    """

    def __init__(self, root: str):
        self._path = os.path.join(root, ".env")
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        data: dict[str, str] = {}
        if os.path.exists(self._path):
            mode = os.stat(self._path).st_mode & 0o777
            if mode & 0o077:
                raise JobError(
                    f".env is mode {mode:o}; must be 600. "
                    f"Run: chmod 600 {self._path}")
            with open(self._path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    data[k.strip()] = v.strip().strip('"').strip("'")
        self._cache = data
        return data

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._load().get(key, os.environ.get(key, default))

    def require(self, key: str) -> str:
        val = self.get(key)
        if not val:
            raise JobError(
                f"required secret {key!r} is empty. Add it to .env "
                "(never to a prompt, never to a commit).")
        return val

    def __repr__(self):
        return f"<Secrets {len(self._load())} keys, values hidden>"
