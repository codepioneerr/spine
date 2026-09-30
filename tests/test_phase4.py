"""Phase 4 — the deterministic brief.

Every test here is offline and model-free by construction. core.brief makes
no model call, which is the property these tests are really protecting: if a
future edit makes the digest depend on a completion, this file starts needing
a network and that is the signal.
"""

import unittest
from datetime import datetime, timezone

from core import brief


def row(key, kind="deal", source="acris", importance=50, title=None, ts="2026-09-30T12:00:00Z"):
    return {"key": key, "kind": kind, "source": source, "importance": importance,
            "title": title if title is not None else "item " + key, "ts": ts,
            "body": "", "url": None, "status": "new"}


class TestTelemetryIsExcluded(unittest.TestCase):

    def test_heartbeat_rows_never_reach_the_brief(self):
        """The reader-side half of the rule. collectors/heartbeat.py promises
        not to emit items; this makes the brief safe even if one day it does,
        or if some other collector starts writing telemetry."""
        rows = [row("a", source="heartbeat", importance=99),
                row("b", source="acris", importance=10)]
        kept = brief.select(rows)
        self.assertEqual([r["source"] for r in kept], ["acris"])

    def test_telemetry_is_dropped_even_when_it_is_the_most_important(self):
        """importance 99 would otherwise lead the brief. Source wins over
        score, because a telemetry row is not a thing to act on at any score."""
        rows = [row("hb", source="heartbeat", importance=99)]
        self.assertEqual(brief.select(rows), [])


class TestSelection(unittest.TestCase):

    def test_cap_is_honoured(self):
        rows = [row(str(i), importance=i) for i in range(40)]
        self.assertEqual(len(brief.select(rows, limit=10)), 10)

    def test_most_important_first(self):
        rows = [row("low", importance=10), row("high", importance=90),
                row("mid", importance=50)]
        self.assertEqual([r["key"] for r in brief.select(rows)],
                         ["high", "mid", "low"])

    def test_ties_break_to_the_newest(self):
        """Two deeds at the same score: the one recorded today is the one you
        can still do something about."""
        rows = [row("old", importance=60, ts="2026-09-01T00:00:00Z"),
                row("new", importance=60, ts="2026-09-30T00:00:00Z")]
        self.assertEqual([r["key"] for r in brief.select(rows)], ["new", "old"])

    def test_selection_does_not_mutate_the_caller_list(self):
        rows = [row("a", importance=1), row("b", importance=2)]
        before = [r["key"] for r in rows]
        brief.select(rows)
        self.assertEqual([r["key"] for r in rows], before)


class TestGrouping(unittest.TestCase):

    def test_kinds_come_out_in_declared_order(self):
        rows = [row("f", kind="fact"), row("a", kind="alert"), row("d", kind="deal")]
        self.assertEqual([k for k, _ in brief.group(rows)], ["alert", "deal", "fact"])

    def test_empty_kinds_are_skipped(self):
        rows = [row("d", kind="deal")]
        self.assertEqual([k for k, _ in brief.group(rows)], ["deal"])

    def test_an_unknown_kind_is_shown_not_dropped(self):
        """A kind added to core.store without being added to KIND_ORDER must
        still appear. Silently vanishing is the worse failure."""
        rows = [row("x", kind="invented"), row("d", kind="deal")]
        groups = dict(brief.group(rows))
        self.assertIn("other", groups)
        self.assertEqual([r["key"] for r in groups["other"]], ["x"])


class TestRendering(unittest.TestCase):

    NOW = datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)

    def test_interrupt_mark_only_above_the_threshold(self):
        rows = [row("hi", importance=brief.INTERRUPT_AT),
                row("lo", importance=brief.INTERRUPT_AT - 1)]
        out = brief.render(rows, now=self.NOW)
        lines = [l for l in out.split(chr(10)) if "item " in l]
        self.assertTrue(lines[0].startswith("!"))
        self.assertFalse(lines[1].startswith("!"))

    def test_empty_store_says_so_instead_of_rendering_nothing(self):
        out = brief.render([], now=self.NOW)
        self.assertIn("nothing unacted", out)

    def test_suppressed_count_points_at_the_escape_hatch(self):
        rows = [row("a")]
        out = brief.render(rows, total_unacted=50, now=self.NOW)
        self.assertIn("+ 49 more", out)
        self.assertIn("bin/items", out)

    def test_no_more_line_when_nothing_is_suppressed(self):
        rows = [row("a")]
        out = brief.render(rows, total_unacted=1, now=self.NOW)
        self.assertNotIn("more:", out)

    def test_the_read_only_promise_is_in_every_brief(self):
        """CLAUDE.md 7: the assistant observes and proposes. The surface says
        so on every message, the same way bin/darkweb footer does."""
        out = brief.render([row("a")], now=self.NOW)
        self.assertIn("read-only", out)

    def test_fits_one_telegram_message(self):
        """4096 is the Telegram limit; common/notify truncates at 4000. A full
        brief must not be near either."""
        rows = [row(str(i), importance=90 - i,
                    title="DEED $50,000,000 - 158-162 WEST 25TH STREET, Manhattan")
                for i in range(brief.MAX_ITEMS)]
        out = brief.render(rows, total_unacted=500, now=self.NOW)
        self.assertLess(len(out), 2000)


class TestNoModelCall(unittest.TestCase):

    def test_brief_module_does_not_import_the_router(self):
        """Layer 1 must stand alone. If core.brief grows a models import, the
        brief has quietly acquired a dependency on a daemon, a cap and a
        privacy gate -- on the one morning all three might be against you."""
        import inspect
        src = inspect.getsource(brief)
        self.assertNotIn("from core import models", src)
        self.assertNotIn("core.models", src)


if __name__ == "__main__":
    unittest.main()
