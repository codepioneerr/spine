"""
core.bridge — read-only access to the darkweb-jobs SQLite files.

## Why a bridge instead of a reimplementation

Phase 2b was originally scoped as "reimplement acris/polymarket/eventbot in
Spine, then retire darkweb-jobs." That is the wrong order of operations, and
the reason is in CLAUDE.md section 6: *nothing is deleted until its
replacement is verified against real output.*

Reimplementing the fetchers means rewriting three working Socrata/Gamma
clients against APIs this code has never called, then trusting them on the
first unattended 06:00 run. The fetch layer is not the interesting part of
Spine — the item store is. So this phase reads what darkweb-jobs already
collected and leaves the fetching exactly where it works today.

**Consequence, stated plainly because it is a real architectural change:**
darkweb-jobs becomes the permanent fetch layer and Spine the item layer on
top. It is not retired at Phase 2b. Revisit only after `bin/compare-migration`
has run clean for a week — at which point the retirement is a decision made
with data instead of a hope.

## Read-only is enforced by SQLite, not by convention

Every connection is opened with `mode=ro`. This is not politeness: eventbot
writes to these files every five minutes and proptech writes nightly. A
read-write handle from Spine could hold a lock at exactly the wrong moment,
and a bug in a Spine collector could corrupt data Spine does not own.

`mode=ro` means the *database* cannot be written. SQLite still needs to
touch the `-shm` index file for a WAL database, which is why this is `ro`
and not `immutable=1` — `immutable` would promise the file is not changing
underneath us, and it demonstrably is.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone


class BridgeUnavailable(RuntimeError):
    """darkweb-jobs is not where we expected, or its database is not readable.

    Raised rather than returning None so a collector fails loudly with a
    path in the message, instead of quietly emitting zero items and looking
    like a slow news day.
    """


def dwj_root() -> str:
    """Where darkweb-jobs lives.

    Overridable because the tests point it at a temporary directory, and
    because a second box might lay things out differently.

    Contrast core.paths, where the checkout's own location is derived and
    deliberately NOT configurable — a stale value in someone's .env is a
    leftover, not a decision, and that bug shipped twice. This is the other
    case: darkweb-jobs is a *separate* checkout whose location Spine cannot
    see from its own file path, so here configuration is the only answer
    available rather than a way to contradict a known fact.
    """
    return os.environ.get(
        "SPINE_DWJ_ROOT", os.path.expanduser("~/darkweb-jobs"))


def db_file(name: str) -> str:
    return os.path.join(dwj_root(), "data", f"{name}.db")


def connect(name: str) -> sqlite3.Connection:
    """Open one darkweb-jobs database read-only."""
    path = db_file(name)
    if not os.path.exists(path):
        raise BridgeUnavailable(
            f"{name}.db not found at {path}. Set SPINE_DWJ_ROOT if "
            "darkweb-jobs lives elsewhere, or check that its cron is still "
            "running — this collector reads what darkweb-jobs writes and "
            "cannot fetch anything itself.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    except sqlite3.OperationalError as exc:
        raise BridgeUnavailable(
            f"cannot open {path} read-only — {exc}. If this says 'unable to "
            "open database file', the WAL sidecar (-shm) is probably owned "
            "by another user; Spine and darkweb-jobs must run as the same "
            "user.") from exc
    conn.row_factory = sqlite3.Row
    return conn


def since_iso(hours: int, now: datetime | None = None) -> str:
    """A lookback cutoff in the format darkweb-jobs writes.

    Every bridged collector needs one of these. Without a window, the first
    run of the polymarket bridge would emit all 3,022 rows in `jumps` and
    the eventbot bridge all 3,588 rows in `signals` — into a store that held
    97 items. The store would dedup them correctly and the brief would still
    be unreadable. Dedup protects against duplicates, not against volume.
    """
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def env_int(key: str, default: int) -> int:
    """An int from .env, falling back loudly rather than crashing a 06:30 run."""
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default
