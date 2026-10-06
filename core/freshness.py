"""
core.freshness — "is the feed under this collector still moving?"

The ACRIS freeze (newest record 2026-08-31, noticed Oct 6) showed the
failure: every job reports OK, the item store simply stops filling, and a
quiet brief looks like a quiet week. collectors/acris has its own day-grained
check; this is the same idea for feeds that tick every few minutes.

The alert key carries the newest timestamp truncated to the hour, so a feed
that stays frozen raises one alert (dismissals stick), and a later, separate
freeze raises a new one.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def stale_item(feed: str, newest, now: datetime, max_age_hours: float,
               what: str, url: str | None = None) -> dict | None:
    """An alert item when `newest` is older than `max_age_hours`, else None.
    `newest` None means the source table is empty, which is also stale."""
    if newest is None:
        age_h, mark = None, "empty"
    else:
        d = _parse(newest)
        if d is None:
            return None
        age_h = (now - d).total_seconds() / 3600
        if age_h <= max_age_hours:
            return None
        mark = d.strftime("%Y-%m-%dT%H")
    since = f"{age_h:.1f} h" if age_h is not None else "ever"
    return {
        "kind": "alert",
        "key": f"feed-stale:{mark}",
        "title": f"{feed} feed stale — no new {what} for {since}",
        "body": (f"Newest {what}: {newest or 'none'} (threshold "
                 f"{max_age_hours:g} h). The darkweb-jobs fetch behind this "
                 "collector has stopped; items from it are not arriving."),
        "url": url,
        "importance": 82,
        "data": {"feed": feed, "newest": newest,
                 "age_hours": None if age_h is None else round(age_h, 1),
                 "max_age_hours": max_age_hours},
    }
