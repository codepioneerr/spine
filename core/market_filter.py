"""
core.market_filter — drop prediction-market signals about events that are
already over.

The bug this fixes (Oct 6 2026): the brief showed "up 57pt (42% -> 100%) —
Bitcoin Up or Down on October 5?" recorded at 18:30 UTC, while the market's
end_date was 16:00 UTC. A move to ~0% or ~100% after the end time is the
market settling, not news. darkweb-jobs' snapshots carry end_date; the
jump rows do not, so the item store did not know.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def status(end_date: str | None, jumped_at: str | None, now: datetime | None = None,
           new_price: float | None = None) -> str:
    """'expired' | 'settling' | 'live' | 'unknown'. Pure."""
    now = now or datetime.now(timezone.utc)
    end = _ts(end_date)
    if end is None:
        return "unknown"
    if end <= now:
        return "expired"
    j = _ts(jumped_at)
    if j and j >= end:
        return "settling"
    if new_price is not None and (new_price <= 0.02 or new_price >= 0.98) and \
            (end - now).total_seconds() < 6 * 3600:
        return "settling"
    return "live"


def end_dates(conn, market_ids) -> dict[str, str]:
    """market_id -> latest known end_date from prediction.db snapshots."""
    ids = list({str(m) for m in market_ids if m})
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for mid, end in conn.execute(
                f"SELECT market_id, MAX(end_date) FROM snapshots WHERE market_id IN "
                f"({','.join('?' * len(chunk))}) GROUP BY market_id", chunk):
            out[str(mid)] = end
    return out
