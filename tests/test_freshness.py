"""core.freshness: the stale-feed alert for polymarket and eventbot."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import freshness                  # noqa: E402
from core.store import KINDS                # noqa: E402
from collectors import eventbot, polymarket  # noqa: E402

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


def stale(newest, hours=3):
    return freshness.stale_item("polymarket", newest, NOW, hours, "snapshot")


class TestFreshness(unittest.TestCase):

    def test_fresh_is_quiet(self):
        self.assertIsNone(stale("2026-10-06T05:30:01Z"))

    def test_at_threshold_is_quiet(self):
        self.assertIsNone(stale("2026-10-06T03:00:00Z"))

    def test_frozen_alerts(self):
        it = stale("2026-10-06T01:00:01Z")
        self.assertEqual(it["kind"], "alert")
        self.assertIn(it["kind"], KINDS)
        self.assertEqual(it["key"], "feed-stale:2026-10-06T01")
        self.assertEqual(it["data"]["age_hours"], 5.0)
        self.assertGreaterEqual(it["importance"], 80)

    def test_key_is_stable_while_frozen(self):
        a = stale("2026-10-06T01:00:01Z")
        b = freshness.stale_item("polymarket", "2026-10-06T01:00:01Z",
                                 NOW + timedelta(hours=6), 3, "snapshot")
        self.assertEqual(a["key"], b["key"])

    def test_naive_timestamp_is_utc(self):
        self.assertIsNone(stale("2026-10-06T05:30:01"))

    def test_empty_table_alerts(self):
        self.assertEqual(stale(None)["key"], "feed-stale:empty")

    def test_garbage_is_ignored(self):
        self.assertIsNone(stale("not a time"))

    def test_thresholds_sit_well_above_the_tick(self):
        # polymarket snapshots every 30 min, eventbot ticks every 5 min.
        self.assertGreaterEqual(polymarket.STALE_HOURS, 2)
        self.assertGreaterEqual(eventbot.STALE_HOURS, 1)


if __name__ == "__main__":
    unittest.main()
