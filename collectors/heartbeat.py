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

Deliberately: no network, no model call, no writes outside var/. It costs
nothing to leave running forever.
"""

import os
import platform
import time

META = {
    "id": "heartbeat",
    # Fires :07 and :37 UTC, NOT :00/:30. darkweb-jobs runs its polymarket
    # snapshot on */30, and until that system is retired (Phase 2) its lock
    # and ours are separate domains — so we stay out of its way by clock
    # rather than by lock. Note the form: "7/30" is the ambiguous spelling
    # core.cron deliberately rejects; "7-59/30" is the honest one.
    "schedule": "7-59/30 * * * *",
    "timeout": 30,
    "ram_mb": 30,
    "window": "any",
    "weight": "light",
    "tier": None,                 # no model call — the cheapest tier is none
    "description": "proves the runner chain is alive",
}


def run(ctx):
    """Collect a few facts about the box.

    A collector returns {"items": [...], "stats": {...}}. Until the item
    store lands in Phase 2, `items` is simply carried in the state file and
    counted by the console — the shape is right, the sink comes later.
    """
    ctx.log("heartbeat: reading box vitals")

    load1, load5, load15 = os.getloadavg()
    item = {
        "source": "heartbeat",
        "kind": "fact",
        "key": f"heartbeat:{int(time.time() // 1800)}",  # dedups per window
        "title": f"{platform.node()} alive",
        "data": {
            "host": platform.node(),
            "kernel": platform.release(),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "cpus": os.cpu_count(),
        },
    }

    return {"items": [item],
            "stats": {"load1": round(load1, 2)}}
