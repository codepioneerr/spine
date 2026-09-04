"""
Phase 1 tests — the router, the privacy gate, the cap, and cost math.

Everything here runs OFFLINE against MockProvider. No key, no network, no
tokens spent. The single live call is Step 10, run by hand on the Dell.

The two tests that justify the whole phase are:

    test_private_job_refused_when_only_logging_providers_exist
    test_call_that_would_breach_the_cap_never_leaves_the_box

Both assert a REFUSAL. If either ever starts passing for the wrong reason —
because the gate got loosened, or because a provider's flag got flipped —
the safety property is gone and nothing else in the suite would notice.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import costs, models                                  # noqa: E402
from core.job import JobError, validate                         # noqa: E402
from core.models import (BudgetRefusal, GeminiProvider, MockProvider,  # noqa: E402
                         NoProviders, PrivacyRefusal, Provider, Router)

UTC = timezone.utc


class DbCase(unittest.TestCase):
    """Each test gets its own throwaway spine.db."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._dir.name, "spine.db")
        self._old = dict(os.environ)
        os.environ["SPINE_DB"] = self._db
        os.environ["SPINE_MONTHLY_USD_CAP"] = "5.00"
        os.environ["SPINE_MONTHLY_USD_TARGET"] = "5.00"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        self._dir.cleanup()

    def router(self, tiers, data="private", job="tj"):
        return Router(tiers=tiers, job=job, data=data)


# ─────────────────────────────────────────────────────────────────────────────

class TestPricing(unittest.TestCase):

    def test_known_prices_match_the_budget_memo(self):
        # 1M in + 1M out on Flash-Lite = $0.10 + $0.40
        self.assertAlmostEqual(
            costs.price("gemini-2.5-flash-lite", 1_000_000, 1_000_000), 0.50)
        # Haiku 4.5 = $1 + $5
        self.assertAlmostEqual(
            costs.price("claude-haiku-4.5", 1_000_000, 1_000_000), 6.00)

    def test_realistic_email_metadata_call_is_a_fraction_of_a_cent(self):
        """~120 tokens in, ~5 out. The memo's figure: ~83,000 emails per $1
        on Flash-Lite."""
        one = costs.price("gemini-2.5-flash-lite", 120, 5)
        self.assertLess(one, 0.00002)
        self.assertGreater(int(1 / one), 50_000)

    def test_unpriced_model_raises_rather_than_costing_zero(self):
        """A model with no price would log at $0 and be invisible to the cap.
        That is the exact failure this phase exists to prevent."""
        with self.assertRaises(costs.UnknownModel):
            costs.price("some-new-model-nobody-priced", 100, 100)

    def test_estimate_is_pessimistic(self):
        """The pre-flight number gates the cap, so it must assume the full
        output budget rather than a hopeful average."""
        est = costs.estimate("claude-haiku-4.5", "x" * 4000, max_tokens=1000)
        actual = costs.price("claude-haiku-4.5", 1000, 20)
        self.assertGreater(est, actual)


class TestCostsTable(DbCase):

    def test_records_and_sums_month_to_date(self):
        costs.record("j", "bulk", "mock", "claude-haiku-4.5",
                     tokens_in=1_000_000, tokens_out=0)
        costs.record("j", "bulk", "mock", "claude-haiku-4.5",
                     tokens_in=500_000, tokens_out=0)
        self.assertAlmostEqual(costs.month_to_date(), 1.50, places=6)

    def test_last_month_does_not_count_against_this_month(self):
        costs.record("j", "bulk", "mock", "claude-haiku-4.5",
                     tokens_in=1_000_000, tokens_out=0,
                     ts=datetime(2026, 8, 15, tzinfo=UTC))
        self.assertEqual(costs.month_to_date(datetime(2026, 9, 4, tzinfo=UTC)),
                         0.0)

    def test_failures_are_recorded_at_zero_but_still_recorded(self):
        costs.record("j", "bulk", "mock", "mock", outcome="fallback",
                     error="boom")
        self.assertEqual(costs.month_to_date(), 0.0)
        self.assertEqual(len(costs.recent(10)), 1)

    def test_headroom_reports_cap_and_target_separately(self):
        os.environ["SPINE_MONTHLY_USD_CAP"] = "25.00"
        os.environ["SPINE_MONTHLY_USD_TARGET"] = "5.00"
        costs.record("j", "bulk", "mock", "claude-haiku-4.5",
                     tokens_in=6_000_000, tokens_out=0)     # $6 — over target
        h = costs.headroom()
        self.assertAlmostEqual(h["spent"], 6.0)
        self.assertAlmostEqual(h["cap"], 25.0)
        self.assertAlmostEqual(h["target"], 5.0)
        self.assertTrue(h["over_target"])
        self.assertAlmostEqual(h["left_to_cap"], 19.0)

    def test_over_target_does_not_refuse_calls(self):
        """The target is a benchmark. Only the cap refuses."""
        os.environ["SPINE_MONTHLY_USD_CAP"] = "25.00"
        os.environ["SPINE_MONTHLY_USD_TARGET"] = "1.00"
        costs.record("j", "bulk", "mock", "claude-haiku-4.5",
                     tokens_in=2_000_000, tokens_out=0)     # $2 — 2x target
        r = self.router({"bulk": [MockProvider()]}, data="private")
        self.assertTrue(r.complete("still allowed", tier="bulk").text)

    def test_target_cannot_exceed_the_cap(self):
        """Otherwise the bar shows room right up to the refusal."""
        os.environ["SPINE_MONTHLY_USD_CAP"] = "5.00"
        os.environ["SPINE_MONTHLY_USD_TARGET"] = "50.00"
        self.assertEqual(costs.target_usd(), 5.0)


# ─────────────────────────────────────────────────────────────────────────────
# the privacy gate — the reason this phase is not just plumbing
# ─────────────────────────────────────────────────────────────────────────────

class TestPrivacyGate(DbCase):

    def logging(self):
        return MockProviderThatLogs()

    def test_private_job_refused_when_only_logging_providers_exist(self):
        r = self.router({"bulk": [self.logging()]}, data="private")
        with self.assertRaises(PrivacyRefusal) as e:
            r.complete("dean correspondence about my registration", tier="bulk")
        msg = str(e.exception)
        self.assertIn("PRIVATE", msg)
        self.assertIn("Refusing to send", msg)

    def test_refusal_is_not_a_silent_downgrade(self):
        """It must raise, not return a degraded result. A caller that gets a
        Completion back has no way to know its data was leaked."""
        r = self.router({"bulk": [self.logging()]}, data="private")
        try:
            r.complete("private", tier="bulk")
            self.fail("returned a completion instead of refusing")
        except PrivacyRefusal:
            pass

    def test_public_job_may_use_a_logging_provider(self):
        r = self.router({"bulk": [self.logging()]}, data="public")
        out = r.complete("a public headline", tier="bulk")
        self.assertTrue(out.text)

    def test_private_job_may_use_a_local_non_logging_provider(self):
        """MockProvider is free but LOCAL — nothing leaves the box — so it is
        not a privacy risk. The gate keys on logs_prompts, not on free."""
        r = self.router({"bulk": [MockProvider()]}, data="private")
        self.assertTrue(r.complete("private", tier="bulk").text)

    def test_gate_filters_rather_than_rejecting_a_mixed_chain(self):
        r = self.router({"bulk": [self.logging(), MockProvider(reply="safe")]},
                        data="private")
        out = r.complete("private", tier="bulk")
        self.assertEqual(out.text, "safe")
        self.assertEqual(out.provider, "mock")

    def test_default_is_private(self):
        """The whole design rests on omission being safe."""
        j = validate({"id": "anything", "schedule": "0 6 * * *"})
        self.assertEqual(j.data, "private")

    def test_router_takes_its_privacy_class_from_META_not_the_call_site(self):
        """A collector never passes `data` to complete(), so it cannot forget
        to, and cannot override it locally."""
        j = validate({"id": "pub", "schedule": "0 6 * * *", "data": "public"})
        r = models.for_job(j, tiers={"bulk": [self.logging()]})
        self.assertEqual(r.data, "public")
        self.assertTrue(r.complete("headline", tier="bulk").text)

    def test_the_shipped_gemini_provider_is_marked_as_logging(self):
        """If this flag is ever flipped to False, private data starts flowing
        to a free endpoint and every other test still passes."""
        self.assertTrue(GeminiProvider().logs_prompts)
        self.assertTrue(GeminiProvider().free)

    def test_smart_tier_is_empty_and_says_so_clearly(self):
        """Phase 1 ships with no non-logging paid endpoint. A private job
        asking for `smart` must get a clear error, not a surprise."""
        r = self.router(models.default_tiers(), data="private")
        with self.assertRaises(NoProviders) as e:
            r.complete("judgment needed", tier="smart")
        self.assertIn("no providers configured", str(e.exception))

    def test_default_bulk_tier_refuses_private_data(self):
        """As shipped: bulk is Gemini free, which logs. A private job routed
        to bulk is refused. This is the live safety invariant on the Dell."""
        r = self.router(models.default_tiers(), data="private")
        with self.assertRaises(PrivacyRefusal):
            r.complete("anything of Nick's", tier="bulk")


class MockProviderThatLogs(MockProvider):
    """A stand-in for a free hosted endpoint: works fine, retains prompts."""

    def __init__(self):
        super().__init__(name="logging_free")
        self.logs_prompts = True
        self.free = True


# ─────────────────────────────────────────────────────────────────────────────

class TestBudgetGate(DbCase):

    def test_call_that_would_breach_the_cap_never_leaves_the_box(self):
        called = []

        class Spy(MockProvider):
            def complete(self, prompt, **kw):
                called.append(1)
                return "should not happen", 1, 1

        os.environ["SPINE_MONTHLY_USD_CAP"] = "1.00"
        costs.record("prior", "bulk", "x", "claude-haiku-4.5",
                     tokens_in=990_000, tokens_out=0)   # $0.99 spent

        spy = Spy()
        spy.model = "claude-haiku-4.5"
        r = self.router({"bulk": [spy]}, data="private")

        with self.assertRaises(BudgetRefusal) as e:
            r.complete("x" * 40_000, tier="bulk", max_tokens=1000)

        self.assertEqual(called, [], "the provider was called anyway")
        self.assertIn("refused before sending", str(e.exception))

    def test_cap_allows_a_call_that_fits(self):
        os.environ["SPINE_MONTHLY_USD_CAP"] = "5.00"
        os.environ["SPINE_MONTHLY_USD_TARGET"] = "5.00"
        r = self.router({"bulk": [MockProvider()]}, data="private")
        self.assertTrue(r.complete("hello", tier="bulk").text)

    def test_spend_accumulates_across_calls(self):
        r = self.router({"bulk": [MockProvider()]}, data="private")
        for _ in range(3):
            r.complete("hello", tier="bulk")
        self.assertEqual(sum(n for _, _, n in costs.by_outcome()), 3)


class TestFallback(DbCase):

    def test_falls_through_a_failing_provider_to_a_working_one(self):
        r = self.router({"bulk": [
            MockProvider(name="first", fail=True),
            MockProvider(name="second", reply="from the second"),
        ]}, data="private")
        out = r.complete("hi", tier="bulk")
        self.assertEqual(out.provider, "second")
        self.assertEqual(out.text, "from the second")

    def test_the_failed_attempt_is_logged_as_its_own_row(self):
        """The logs must show WHICH provider actually answered, or a chain
        that has silently been running on its backup for a month looks
        healthy."""
        r = self.router({"bulk": [
            MockProvider(name="first", fail=True),
            MockProvider(name="second"),
        ]}, data="private")
        r.complete("hi", tier="bulk")
        outcomes = {r_["provider"]: r_["outcome"] for r_ in costs.recent(10)}
        self.assertEqual(outcomes["first"], "fallback")
        self.assertEqual(outcomes["second"], "ok")

    def test_all_providers_failing_raises(self):
        r = self.router({"bulk": [
            MockProvider(name="a", fail=True),
            MockProvider(name="b", fail=True),
        ]}, data="private")
        with self.assertRaises(models.ModelError):
            r.complete("hi", tier="bulk")

    def test_a_provider_missing_its_key_is_skipped_not_fatal(self):
        needs_key = Provider(name="needs_key", model="mock",
                             logs_prompts=False, key_env="DEFINITELY_NOT_SET")
        r = self.router({"bulk": [needs_key, MockProvider(reply="ok")]},
                        data="private")
        self.assertEqual(r.complete("hi", tier="bulk").text, "ok")

    def test_unknown_tier_names_the_valid_ones(self):
        r = self.router({"bulk": [MockProvider()]})
        with self.assertRaises(models.ModelError) as e:
            r.complete("hi", tier="cheap")
        self.assertIn("never a model name", str(e.exception))


class TestContractIntegration(DbCase):

    def test_data_must_be_public_or_private(self):
        with self.assertRaises(JobError):
            validate({"id": "x1", "schedule": "0 6 * * *", "data": "maybe"})

    def test_data_appears_in_the_registry_row(self):
        j = validate({"id": "x2", "schedule": "0 6 * * *"})
        self.assertEqual(j.as_row()["data"], "private")

    def test_ctx_exposes_a_bound_router_and_http(self):
        from core.job import Ctx
        j = validate({"id": "x3", "schedule": "0 6 * * *", "data": "public"})
        ctx = Ctx.build(j, lambda *a, **k: None, ROOT)
        self.assertEqual(ctx.models.job, "x3")
        self.assertEqual(ctx.models.data, "public")
        self.assertTrue(hasattr(ctx.http, "get_json"))

    def test_db_still_pending_until_phase_2(self):
        from core.job import Ctx
        j = validate({"id": "x4", "schedule": "0 6 * * *"})
        ctx = Ctx.build(j, lambda *a, **k: None, ROOT)
        with self.assertRaises(NotImplementedError) as e:
            ctx.db.query("select 1")
        self.assertIn("Phase 2", str(e.exception))


class TestPlainSurface(unittest.TestCase):

    def test_status_is_a_wrapper_not_a_second_implementation(self):
        with open(os.path.join(ROOT, "bin", "status")) as fh:
            text = fh.read()
        self.assertIn("--plain", text)
        self.assertIn("darkweb", text)
        self.assertTrue(os.access(os.path.join(ROOT, "bin", "status"), os.X_OK))

    def test_plain_output_has_no_ansi_and_carries_the_numbers(self):
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "darkweb"),
                            "--plain"], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\033[", r.stdout)
        for expected in ("SYSTEM", "INTEGRITY", "SCHEDULE", "SPEND", "cap"):
            self.assertIn(expected, r.stdout)


if __name__ == "__main__":
    unittest.main()


class TestAuditRedaction(unittest.TestCase):
    """The audit reads files that contain live keys and prints what it finds.

    Redaction is therefore not a nicety — it is the only thing standing
    between `bin/audit-perplexity` and a key in a screenshot. The first
    version of the pattern missed TELEGRAM_BOT_TOKEN because \\b does not
    fire after an underscore. Probe it with real shapes, not invented ones.
    """

    def setUp(self):
        import importlib.machinery
        import importlib.util
        path = os.path.join(ROOT, "bin", "audit-perplexity")
        spec = importlib.util.spec_from_loader(
            "audit", importlib.machinery.SourceFileLoader("audit", path))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_masks_every_key_shape_on_this_box(self):
        secrets = [
            ("pplx", 'PPLX_KEY = "pplx-abc123def456ghi789jkl012"'),
            ("anthropic", "ANTHROPIC_API_KEY=sk-ant-api03-XXXXXXXXXXXXXXXX"),
            ("telegram", "export TELEGRAM_BOT_TOKEN=8123456789:AAF-abcdefgh"),
            ("alpaca", "ALPACA_SECRET_KEY: aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
            ("bearer", 'headers={"Authorization": "Bearer sk-proj-9f8e7d6c5b"}'),
            ("gemini", "GEMINI_API_KEY=AIzaSyD-1234567890abcdefghijklmnop"),
        ]
        for label, line in secrets:
            out = self.mod.redact(line)
            self.assertIn("[redacted]", out, f"{label} was not masked: {out}")
            tail = line.split("=")[-1].split(":")[-1].strip().strip('"')
            self.assertNotIn(tail[8:], out,
                             f"{label} leaked its tail: {out}")

    def test_leaves_ordinary_lines_alone(self):
        for benign in ('model="sonar-pro", temperature=0.2',
                       "https://api.perplexity.ai/chat/completions",
                       "def research(query):"):
            self.assertEqual(self.mod.redact(benign), benign)

    def test_audit_is_read_only_and_offline(self):
        """No sockets, no writes. It runs against a box holding live keys."""
        with open(os.path.join(ROOT, "bin", "audit-perplexity")) as fh:
            src = fh.read()
        for forbidden in ("urllib.request", "requests.", "socket.",
                          "http.client"):
            self.assertNotIn(forbidden, src)
        self.assertIn("mode=ro", src)   # sqlite opened read-only


class TestPathingCannotBeOverridden(unittest.TestCase):
    """Regression: the /opt/spine bug, which shipped TWICE.

    First in bin/run.sh — fixed by re-asserting after sourcing .env. Then
    again in Python, where core.costs still read SPINE_ROOT and put the
    database at /opt/spine/var/spine.db. bin/smoke-live sources .env too, so
    the stale value came straight back and the live call died with
    Permission denied.

    The fix is not another re-assertion. It is that Python no longer reads
    SPINE_ROOT at all: where the checkout lives is a fact the code can see,
    and configuration must not be able to contradict it.
    """

    def setUp(self):
        self._old = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_env_cannot_move_the_repository_root(self):
        from core import paths
        os.environ["SPINE_ROOT"] = "/opt/spine"
        self.assertEqual(paths.root(), ROOT)

    def test_env_cannot_move_the_database_into_a_root_owned_path(self):
        from core import costs, paths
        os.environ["SPINE_ROOT"] = "/opt/spine"
        os.environ.pop("SPINE_DB", None)
        for fn in (paths.db_path, costs.db_path):
            self.assertTrue(fn().startswith(ROOT), f"{fn.__name__} -> {fn()}")
            self.assertNotIn("/opt/spine", fn())

    def test_spine_db_may_still_relocate_the_database(self):
        """Putting the DB on another disk IS a real decision. Only the
        checkout location is non-negotiable."""
        from core import paths
        os.environ["SPINE_DB"] = "/tmp/elsewhere.db"
        self.assertEqual(paths.db_path(), "/tmp/elsewhere.db")

    def test_registry_and_console_agree_with_paths(self):
        from core import registry
        os.environ["SPINE_ROOT"] = "/opt/spine"
        self.assertEqual(registry.root(), ROOT)
        with open(os.path.join(ROOT, "bin", "darkweb")) as fh:
            src = fh.read()
        self.assertNotIn('environ.get("SPINE_ROOT")', src)

    def test_no_module_reads_SPINE_ROOT_anymore(self):
        """The grep that keeps this from coming back a third time."""
        import glob
        offenders = []
        for path in glob.glob(os.path.join(ROOT, "core", "*.py")):
            with open(path) as fh:
                if "SPINE_ROOT" in fh.read():
                    offenders.append(os.path.basename(path))
        self.assertEqual(
            [o for o in offenders if o != "paths.py"], [],
            f"these still read SPINE_ROOT: {offenders}")


class TestGeminiAdapter(DbCase):

    def test_uses_the_model_string_that_actually_resolves(self):
        """gemini-2.5-flash-lite 404'd against Nick's AI Studio key."""
        self.assertEqual(GeminiProvider().model, "gemini-3.5-flash-lite")

    def test_free_tier_is_billed_at_zero_not_at_list_price(self):
        """Billing a free call at the paid rate inflates month-to-date with
        dollars nobody was charged, and the cap refuses on that number — so
        an overstating table eventually blocks calls that cost nothing."""
        p = GeminiProvider()
        self.assertEqual(p.billed_as, "gemini-free-tier")
        self.assertEqual(costs.price(p.billed_as, 1_000_000, 1_000_000), 0.0)

    def test_a_paid_gemini_key_bills_at_the_real_rate(self):
        p = GeminiProvider(free=False)
        self.assertEqual(p.billed_as, "gemini-3.5-flash-lite")
        self.assertGreater(costs.price(p.billed_as, 1_000_000, 0), 0.0)

    def test_free_tier_calls_never_trip_the_cap(self):
        os.environ["SPINE_MONTHLY_USD_CAP"] = "0.01"
        p = GeminiProvider()
        r = Router(tiers={"bulk": [p]}, job="t", data="public")
        r.check_budget(p.billed_as, "x" * 100_000)   # must not raise

    def test_the_key_never_goes_in_the_query_string(self):
        """A URL with a key in it lands in logs, proxies and history."""
        with open(os.path.join(ROOT, "core", "models.py")) as fh:
            src = fh.read()
        self.assertIn("x-goog-api-key", src)
        self.assertNotIn("?key=", src)
