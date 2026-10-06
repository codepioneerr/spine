"""
core.brief — the deterministic layer of the morning brief.

This module contains no model call. That is the design, not a stage of it.

A brief that depends on a model is a brief that disappears the day the daemon
is down, the cap is hit, or the privacy gate refuses -- which are exactly the
mornings you would most want to know what the box saw. So the selection and
the wording of the digest are ordinary code, and judgment is an optional pass
layered on top of the text this produces (core.brief.render), never a
prerequisite for having one.

## Telemetry is excluded here, on the reader side

CLAUDE.md 7a: an item is something Nick might act on, and infrastructure
telemetry is not. heartbeat obeys that today by writing state instead of
items. But the rule only holds as long as every future collector remembers
it, and the failure is silent -- a chatty collector does not break anything,
it just quietly becomes 100 percent of the brief, which is what 97 heartbeat
rows would have done to a top-25 query during Phase 2a.

So the exclusion is enforced twice: collectors do not emit telemetry, and the
reader drops it anyway. The second check is the one that survives someone
being careless in six months.

## Why the cap is on items and not on characters

The brief is judged by whether it gets read. MAX_ITEMS is 10 because the
definition of done says under ten, and a digest you skim past is worth less
than no digest. Everything suppressed is still in the store and still one
bin/items invocation away -- the cap decides what is pushed, never what is
kept.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import store

# Sources whose rows are infrastructure, not things to act on. Belt and
# braces: none of these writes items today.
TELEMETRY_SOURCES = frozenset({"heartbeat", "heartbeat_vitals", "probe"})

MAX_ITEMS = 10

# CLAUDE.md 7a reserves above 80 for things worth interrupting over. The brief
# does not interrupt, but it marks them so the eye lands there first.
INTERRUPT_AT = 80

# Most urgent kind first. A deed you can still bid on outranks a price tick.
KIND_ORDER = ("alert", "task", "deal", "signal", "fact")
KIND_LABEL = {
    "alert": "ALERTS",
    "task": "TASKS",
    "deal": "PROPERTY RECORDS",
    "signal": "MARKET MOVES",
    "fact": "SIMULATED POSITIONS",
}


def select(rows, limit=MAX_ITEMS, exclude=TELEMETRY_SOURCES):
    """Pure. Drop telemetry, then keep the `limit` most important rows.

    The store already orders by importance DESC, ts DESC, but this re-sorts
    rather than trusting the caller to have asked for that -- the function is
    used by tests and by a future layer 2 with hand-built row lists.
    """
    keep = [r for r in rows if r["source"] not in exclude]
    # Two stable sorts compose into "importance desc, then newest first".
    keep.sort(key=lambda r: str(r["ts"]), reverse=True)
    keep.sort(key=lambda r: -int(r["importance"]))
    return keep[:limit]


def group(rows):
    """Pure. Rows bucketed by kind, in KIND_ORDER, skipping empty buckets."""
    out = []
    for kind in KIND_ORDER:
        bucket = [r for r in rows if r["kind"] == kind]
        if bucket:
            out.append((kind, bucket))
    # A kind nobody thought of still gets shown rather than silently dropped.
    known = set(KIND_ORDER)
    rest = [r for r in rows if r["kind"] not in known]
    if rest:
        out.append(("other", rest))
    return out


def _label(kind, n):
    """The kind name, singular when there is one of it.

    Every KIND_LABEL is a plain plural, so dropping the trailing s is correct
    for all of them. It reads as detail rather than as a template someone
    forgot to finish -- "1 signals" is the kind of thing that makes a daily
    message feel unmaintained.
    """
    word = KIND_LABEL.get(kind, kind).lower()
    return word[:-1] if n == 1 and word.endswith("s") else word


def render(rows, *, total_unacted=None, suppressed=None, now=None,
           title="spine brief"):
    """Pure. The digest as plain text, safe to send as one Telegram message.

    `suppressed` is an optional {kind: count} of what the cap left out. A bare
    "+ 126 more" says something is hidden without saying what, which is exactly
    the shape of a message people stop reading. Naming the kinds means a day
    when deals crowd out every position is visible rather than silent.
    """
    now = now or datetime.now(timezone.utc)
    shown = len(rows)
    stamp = now.strftime("%a %b %d %H:%M")
    lines = [title + " - " + stamp + " UTC"]

    if total_unacted is None:
        lines.append(f"{shown} item(s)")
    else:
        lines.append(f"{shown} of {total_unacted} unacted item(s)")

    if not rows:
        lines.append("")
        lines.append("nothing unacted. the collectors ran and found nothing worth your time.")
        return chr(10).join(lines)

    for kind, bucket in group(rows):
        lines.append("")
        lines.append(KIND_LABEL.get(kind, kind.upper()))
        for r in bucket:
            imp = int(r["importance"])
            mark = "!" if imp >= INTERRUPT_AT else " "
            lines.append(f"{mark} [{imp:>3}] " + str(r["title"]))

    if total_unacted and total_unacted > shown:
        rest = total_unacted - shown
        detail = ""
        if suppressed:
            parts = [f"{n} {_label(k, n)}"
                     for k, n in sorted(suppressed.items(),
                                        key=lambda kv: -kv[1]) if n]
            if parts:
                detail = " (" + ", ".join(parts) + ")"
        lines.append("")
        lines.append(f"+ {rest} more{detail}: bin/items --all")

    lines.append("")
    lines.append("read-only. nothing here sent, bought, posted or deleted.")
    lines.append("bin/items --act ID to clear, --dismiss ID to drop.")
    return chr(10).join(lines)


# Generous, because this bounds the accuracy of the suppressed-by-kind
# breakdown rather than the size of the brief. The brief itself is capped at
# MAX_ITEMS regardless.
FETCH_LIMIT = 1000


def build(st=None, limit=MAX_ITEMS, now=None):
    """Read the store and produce (text, rows). The only I/O in this module."""
    st = st or store.Store()
    fetched = st.unacted(limit=FETCH_LIMIT)
    rows = select(fetched, limit=limit)

    chosen = {r["key"] for r in rows}
    suppressed = {}
    for r in fetched:
        if r["key"] in chosen or r["source"] in TELEMETRY_SOURCES:
            continue
        kind = r["kind"]
        suppressed[kind] = suppressed.get(kind, 0) + 1

    counts = st.counts()
    total = sum(counts.get(k, 0) for k in ("new", "seen"))
    return (render(rows, total_unacted=total, suppressed=suppressed, now=now),
            rows)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="the deterministic morning brief")
    ap.add_argument("--limit", type=int, default=MAX_ITEMS)
    args = ap.parse_args(argv)
    text, _ = build(limit=args.limit)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
