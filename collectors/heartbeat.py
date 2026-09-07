"""
collectors/heartbeat — the reference collector.

It exists to prove the chain end to end: cron -> bin/run.sh -> flock ->
governor -> core.runner -> state file -> bin/darkweb. If the console shows a
recent green `heartbeat`, every part of Phase 0 is working.

It is also the shortest possible example of the contract, and the thing to
copy when writing a real collector.

**Permanent.** Nick's ruling, Sept 3 2026: 30 MB every 30 minutes is a
negligible price for knowing at a glance that the box is breathing. It stays
in the schedule for the life of the framework.

## Why this no longer writes to the item store (Sept 7 2026)

Phase 2a migrated heartbeat to `ctx.db` to prove `ctx.db` worked. It did —
and then it kept going. Ninety-seven rows accumulated at ~48/day, every one
of them `new`, none ever acted on, and by the time Phase 3 was scoped they
were **100% of the store**. A brief that selects "unacted items,
importance-ranked, capped at 25" would have been twenty-five heartbeats.

The rule being broken is in CLAUDE.md section 7a: *the item store is a
queue, not an archive.* An item is something Nick might act on. A heartbeat
is infrastructure telemetry with an existing surface — the SCHEDULE panel in
`bin/darkweb`, which reads the state file and shows the last run and its
outcome. It was never something to read.

So the write goes back to the state file, where Phase 0 had it. The item
store keeps the demonstration it needed and loses the pollution it did not.

**Belt and braces.** Phase 3's `core.brief` also excludes telemetry sources,
so a future collector that makes this mistake cannot silently take over the
brief. Fixing the emitter is the real fix; the reader-side filter exists
because the next mistake will be made by someone who has not read this.

Deliberately: no network, no model call, no writes outside var/. It costs
nothing to leave running forever.
"""

import os
import platform
import time

from core import registry

META = {
    "id": "heartbeat",
    # Fires :07 and :37 UTC, NOT :00/:30. darkweb-jobs runs its polymarket
    # snapshot on */30, and its lock and ours are separate domains — so we
    # stay out of its way by clock rather than by lock. Note the form:
    # "7/30" is the ambiguous spelling core.cron deliberately rejects;
    # "7-59/30" is the honest one.
    "schedule": "7-59/30 * * * *",
    "timeout": 30,
    "ram_mb": 30,
    "window": "any",
    "weight": "light",
    "tier": None,                 # no model call — the cheapest tier is none
    "description": "proves the runner chain is alive",
}


def vitals() -> dict:
    """The facts. Pure, so a test can check them without a filesystem."""
    load1, load5, load15 = os.getloadavg()
    return {
        "host": platform.node(),
        "kernel": platform.release(),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cpus": os.cpu_count(),
    }


def run(ctx):
    """Record that the box is alive.

    Returns the framework's shape — {"items": [...], "stats": {...}} — with
    an empty items list. That is not a degradation: it is this collector
    saying, accurately, that it produced nothing for Nick to look at.
    """
    ctx.log("heartbeat: reading box vitals")
    # data="private" by default and this job makes no model call, so nothing
    # here ever leaves the box.

    data = vitals()
    data["checked_at"] = ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    data["window"] = int(time.time() // 1800)

    if not ctx.dry_run:
        registry.write_state("heartbeat_vitals", data)

    ctx.log(f"heartbeat: {data['host']} load {data['load'][0]}")

    # No items, on purpose. See the docstring: telemetry is not a queue item.
    return {"items": [], "stats": {"load1": data["load"][0],
                                   "cpus": data["cpus"]}}
