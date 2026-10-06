"""
health_brief — the morning health brief. Layer 4 in the Layer 2 directory,
for the same reason as collectors/brief.py: the registry only finds jobs here.

Reads var/health.db (written by health_receiver), never writes it. Layer 1
(core.health_brief.build) is always produced; layer 2 is the local model's
Gokhale-informed plan, gated on RAM exactly like the market brief, because
it is the same 3.6 GB model.

Output: var/health_brief/<date>.md, last 30 kept. Telegram delivery is OFF
unless SPINE_HEALTH_BRIEF_TELEGRAM=1, because it moves health data off the
box (through Hermes to Telegram's servers) and that is Nick's call, not a
default.

Runs at 09:10 UTC (05:10 EDT / 04:10 EST): after the phone's overnight sync,
inside the night window, and 30 minutes before the market brief so the two
model calls never contend (the global flock serializes them anyway).
"""

import glob
import io
import os
from datetime import timedelta

from collectors.brief import judgment_ram
from core import governor, health, paths
from core import health_brief as hb

META = {
    "id": "health_brief",
    "schedule": "10 9 * * *",
    "timeout": 1200,
    "ram_mb": 100,          # layer 1; the model's RAM is checked in run()
    "window": "night",
    "weight": "light",
    "tier": "smart",
    "data": "private",      # local model only — core.models enforces it
    "description": "morning health brief (Gokhale-informed, local model)",
}

MAX_TOKENS = 650
MODEL_TIMEOUT_S = 900
KEEP = 30


def target_day(now):
    """Yesterday, in Nick's timezone."""
    return (governor.Settings.from_env().localize(now) - timedelta(days=1)).date()


def plan(ctx, layer1: str):
    try:
        c = ctx.models.complete(hb.prompt(layer1, hb.load_profile()),
                                tier=ctx.job.tier or "smart",
                                max_tokens=MAX_TOKENS, timeout=MODEL_TIMEOUT_S)
    except Exception as exc:
        ctx.log("plan skipped", reason=type(exc).__name__, detail=str(exc)[:160])
        return None
    text = (c.text or "").strip()
    if text:
        ctx.log("plan ok", provider=c.provider, tokens_out=c.tokens_out, ms=c.latency_ms)
    return text or None


def _keep(day, text):
    path = paths.var("health_brief", f"{day.isoformat()}.md")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    for old in sorted(glob.glob(os.path.join(os.path.dirname(path), "*.md")))[:-KEEP]:
        os.remove(old)
    return path


def run(ctx):
    day = target_day(ctx.now)
    conn = health.connect()
    try:
        b = hb.build(conn, day)
    finally:
        conn.close()
    text = b["text"]
    ctx.log("health brief built", day=day.isoformat(), flags=len(b["flags"]),
            has_data=b["has_data"])

    note = None
    if b["has_data"]:
        ok, free, need = judgment_ram(ctx.log)
        if ok:
            note = plan(ctx, text)
        else:
            text += f"\n\n-- plan skipped: {free} MB free, needs {need} MB --"
    if note:
        text += "\n\n-- today's plan (local model, unverified; not medical advice) --\n" + note

    stats = {"day": day.isoformat(), "flags": len(b["flags"]), "plan": bool(note),
             "sent": False}
    if ctx.dry_run:
        return {"items": [], "stats": stats}

    kept = _keep(day, text)
    if os.environ.get("SPINE_HEALTH_BRIEF_TELEGRAM") == "1":
        ctx.notify.send(text)
        stats["sent"] = True
    ctx.log("health brief kept", path=kept, sent=stats["sent"])
    return {"items": [], "stats": stats}
