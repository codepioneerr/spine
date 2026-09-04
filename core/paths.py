"""
core.paths — where the checkout is. Exactly one answer, derived once.

This module exists because the same bug shipped twice.

The first time, `bin/run.sh` computed the root from its own path, then
sourced `.env` with `set -a`, which overwrote SPINE_ROOT with the
`/opt/spine` that `.env.example` used to ship. Every job died on
`cd: /opt/spine: No such file or directory`.

The second time, the shell was fixed but Python was not: `core.costs` still
read SPINE_ROOT from the environment, so `bin/smoke-live` — which also
sources `.env` — put the database at `/opt/spine/var/spine.db` and crashed
with `Permission denied`.

The lesson both times: **where the checkout lives is a fact, not a
preference.** The code can see it. Configuration must not be able to
contradict it, because a stale value in someone's `.env` is not a decision
anybody made — it is a leftover.

So `root()` is derived from this file's own location and reads no
environment variable at all. SPINE_DB stays overridable, because putting
the database on another disk IS a real decision.
"""

from __future__ import annotations

import os


def root() -> str:
    """The repository root. Not configurable, on purpose."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def var(*parts: str) -> str:
    """A path under var/, created on demand. Always inside the checkout."""
    path = os.path.join(root(), "var", *parts)
    os.makedirs(os.path.dirname(path) if parts else path, exist_ok=True)
    return path


def db_path() -> str:
    """The SQLite file. SPINE_DB may relocate it — that is a real choice —
    but the default is always inside this checkout."""
    return os.environ.get("SPINE_DB") or os.path.join(root(), "var", "spine.db")
