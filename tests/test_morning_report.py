"""bin/proptech-report: the pure rendering. No database, no state files."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import morning_report as mr  # noqa: E402


class TestMorningReport(unittest.TestCase):

    def test_never_run_job(self):
        self.assertIn("never run", mr.job_line("proptech", {}))

    def test_skip_reason_is_shown(self):
        line = mr.job_line("brief", {"outcome": "skipped",
                                     "finished_at": "t", "reason": "low RAM"})
        self.assertIn("SKIPPED", line)
        self.assertIn("low RAM", line)

    def test_run_stats_win_over_live_counts(self):
        out = "\n".join(mr.proptech_lines({"pluto_coverage_pct": 41.2},
                                          {"pluto_coverage_pct": 0.0,
                                           "docs": 1200}))
        self.assertIn("41.2%", out)
        self.assertIn("1,200", out)

    def test_brief_states(self):
        self.assertEqual(mr.brief_line({}), "brief: not sent")
        self.assertIn("with machine read", mr.brief_line(
            {"outcome": "ok", "stats": {"judgment": True}}))
        self.assertIn("skipped", mr.brief_line(
            {"outcome": "ok", "stats": {"judgment": False}}))

    def test_alerts_listed_or_none(self):
        self.assertIn("none", mr.render({}, {}, []))
        out = mr.render({}, {}, [{"importance": 85,
                                  "title": "ACRIS feed stale"}])
        self.assertIn("[85] ACRIS feed stale", out)


if __name__ == "__main__":
    unittest.main()
