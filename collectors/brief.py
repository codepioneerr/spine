"""
brief — the morning brief. Layer 4 living in the Layer 2 directory.

This is not a collector. It emits no items; it reads the ones other jobs
wrote and sends one message. It sits here because core.registry.discover
scans collectors/ and that is the only place the runner finds jobs, which is
the same reason heartbeat lives here without writing items. If a second
assistant job ever appears, teach the registry a second package rather than
letting this directory quietly become "jobs".

## Two layers, and why the order matters

Layer 1 is core.brief: deterministic selection and wording, no model. Layer 2
asks the smart tier for one paragraph of judgment over that text. Layer 1 is
sent whether or not layer 2 works, because the mornings when the daemon is
down or the gate refuses are exactly the mornings worth having a brief.

Layer 2 output is labelled as machine commentary and placed after the list,
never instead of it. Measured on 2026-09-30, qwen3.5:4b at 2.8 tok/s wrote a
usable paragraph that also managed to merge two separate Manhattan deeds into
one claim. A 4.7B model at Q4 is worth a second opinion and is not worth
trusting as the record.

## Why heavy and night

The model is ~3.6 GB resident, allocated inside the Ollama process, which
core.governor.available_mb cannot see or attribute. Declaring ram_mb makes
the RAM guard honest about a cost it would otherwise miss entirely. weight
heavy then forces window night at load time, which is correct anyway: 09:40
UTC is 05:40 ET in summer and 04:40 in winter, both inside the 01:00-06:00
window, and both early enough that the brief is waiting rather than arriving.
"""

import io

from core import brief as brief_mod
from core import paths

META = {
    "id": "brief",
    # UTC, like every schedule here. 05:40 EDT / 04:40 EST.
    "schedule": "40 9 * * *",
    # 2.8 tok/s measured, so a 300-token paragraph is ~110 s plus model load.
    # 900 s is slack, not an expectation.
    "timeout": 900,
    # HARD_MAX_MB. Understates the observed 3667 MB by ~270 MB, and a heavy job
    # also gets the full 1024 MB headroom, so the guard still holds real margin.
    "ram_mb": 3500,
    "window": "night",
    "weight": "heavy",
    "tier": "smart",
    # The brief is built from the item store, which holds eventbot positions.
    # Private is also the default and the correct answer here regardless.
    "data": "private",
    "description": "the morning brief",
}

# Short on purpose. The model is slow and a long paragraph is not more useful
# than a short one, so this bounds latency as much as verbosity.
JUDGMENT_MAX_TOKENS = 220

PROMPT = (
    "Below is an automated queue of items from a personal monitoring system: "
    "New York property records, prediction-market moves, and paper-trading "
    "positions.\n\n"
    "Write ONE short paragraph saying what deserves attention first and why. "
    "Be specific and terse. Do not restate the list. Do not invent anything "
    "that is not shown. If nothing stands out, say so plainly.\n\n"
)


def judgment(ctx, digest):
    """Layer 2. Returns a paragraph, or None if it could not be produced.

    Every failure path returns None rather than raising: a refusal, an outage
    or a slow model must cost the brief its commentary, never its delivery.
    """
    try:
        c = ctx.models.complete(PROMPT + digest,
                                tier=ctx.job.tier or "smart",
                                max_tokens=JUDGMENT_MAX_TOKENS)
    except Exception as exc:
        ctx.log("judgment skipped", reason=type(exc).__name__, detail=str(exc)[:160])
        return None
    text = (c.text or "").strip()
    if not text:
        ctx.log("judgment skipped", reason="empty response")
        return None
    ctx.log("judgment ok", provider=c.provider, tokens_out=c.tokens_out,
            ms=c.latency_ms, usd=c.usd)
    return text


def _keep(text):
    """Write the brief where a human can find it, and return the path.

    Deliberately one fixed file that is overwritten rather than a timestamped
    series: this is a recovery copy for the current morning, not an archive,
    and an unbounded pile of briefs on a 232 GB disk is a slow leak nobody
    would notice. The item store is the durable record.
    """
    path = paths.var("state", "brief_last.txt")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run(ctx):
    text, rows = brief_mod.build(now=ctx.now)
    ctx.log("brief built", items=len(rows), chars=len(text))

    note = judgment(ctx, text) if rows else None
    if note:
        text = text + "\n\n-- machine read (qwen3.5:4b, local, unverified) --\n" + note

    if ctx.dry_run:
        # Defensive only: core.runner short-circuits a dry run before calling
        # run(), so nothing reaches here via bin/run.sh --dry-run. Kept for a
        # direct caller, and it logs rather than prints so the test suite stays
        # quiet.
        ctx.log("dry run, not sending", chars=len(text))
        return {"items": [], "stats": {"items_in_brief": len(rows),
                                       "judgment": bool(note), "sent": False}}

    # Written before the send, not after. A brief that was generated and then
    # failed to leave the box is still worth having -- the alternative is that
    # a 401, a stopped gateway or a network blip silently costs you the whole
    # morning and leaves nothing but an exit code.
    kept = _keep(text)

    try:
        ctx.notify.send(text)
    except Exception as exc:
        ctx.log("DELIVERY FAILED", error=str(exc)[:200], preserved_at=kept)
        raise

    ctx.log("brief sent", chars=len(text), preserved_at=kept)
    return {"items": [], "stats": {"items_in_brief": len(rows),
                                   "judgment": bool(note), "sent": True}}
