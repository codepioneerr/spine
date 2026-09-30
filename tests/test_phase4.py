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


class _FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDelivery(unittest.TestCase):
    """core.notify. Offline: urlopen is replaced, nothing reaches hermes."""

    URL = "http://127.0.0.1:8644/webhooks/spine-brief"
    SECRET = "test-secret-not-the-real-one"

    def _notifier(self, **kw):
        from core import notify
        return notify.Notifier(url=self.URL, secret=self.SECRET, **kw)

    def _capture(self, payload=None):
        """Patch urlopen, return (notifier, captured_requests)."""
        from core import notify
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _FakeResponse(payload if payload is not None
                                 else {"status": "delivered"})

        self._orig = notify.urllib.request.urlopen
        notify.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, notify.urllib.request, "urlopen", self._orig)
        return self._notifier(), captured

    def test_unconfigured_raises_and_says_how_to_fix_it(self):
        """A silent False is how darkweb-jobs delivered nothing for a month."""
        from core import notify
        n = notify.Notifier(url=None, secret=None)
        self.assertFalse(n.configured())
        with self.assertRaises(notify.NotifyError) as e:
            n.send("anything")
        msg = str(e.exception)
        self.assertIn("SPINE_BRIEF_WEBHOOK_URL", msg)
        self.assertIn("hermes webhook subscribe", msg)

    def test_empty_message_is_not_an_error(self):
        """Nothing to say is a normal outcome, not a failure."""
        n = self._notifier()
        self.assertFalse(n.send(""))
        self.assertFalse(n.send("   \n  "))

    def test_signature_is_hmac_over_timestamp_dot_body(self):
        """The exact scheme hermes validates. If this drifts, delivery starts
        returning 401 and the only symptom is a missing brief."""
        import hashlib
        import hmac

        n, captured = self._capture()
        self.assertTrue(n.send("hello"))
        self.assertEqual(len(captured), 1)
        req = captured[0]

        stamp = req.get_header("X-webhook-timestamp")
        sig = req.get_header("X-webhook-signature-v2")
        self.assertTrue(stamp and sig)

        expected = hmac.new(self.SECRET.encode(),
                            stamp.encode() + b"." + req.data,
                            hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_the_deprecated_v1_signature_is_not_sent(self):
        """hermes accepts a body-only V1 and warns that it is replay-
        vulnerable. There is no reason to send the weaker one."""
        n, captured = self._capture()
        n.send("hello")
        self.assertIsNone(captured[0].get_header("X-webhook-signature"))

    def test_body_is_the_text_field_the_route_template_expects(self):
        """The subscription renders {text}. A different key delivers nothing
        while still returning 200."""
        import json
        n, captured = self._capture()
        n.send("the brief")
        self.assertEqual(json.loads(captured[0].data.decode()), {"text": "the brief"})

    def test_over_long_text_is_truncated_not_rejected(self):
        """Telegram hard-limits at 4096. A long brief should arrive short."""
        import json
        from core import notify
        n, captured = self._capture()
        n.send("x" * 9000)
        sent = json.loads(captured[0].data.decode())["text"]
        self.assertLess(len(sent), 4096)
        self.assertIn("truncated", sent)

    def test_a_200_that_did_not_deliver_still_raises(self):
        """hermes can accept the post and fail to relay it. Accepting is not
        delivering, and the caller needs to know the difference."""
        from core import notify
        n, _ = self._capture(payload={"status": "queued"})
        with self.assertRaises(notify.NotifyError) as e:
            n.send("hello")
        self.assertIn("did not deliver", str(e.exception))

    def test_no_telegram_credential_in_the_code(self):
        """CLAUDE.md 6: hermes owns the gateway, so Spine must never hold a
        Telegram credential. If this fails, the delivery design drifted.

        Checks string literals that are not docstrings, rather than the raw
        source. The module prose names TELEGRAM_BOT_TOKEN precisely to explain
        why it is absent, and a test that cannot tell an explanation from an
        implementation is a test that punishes documentation."""
        import ast
        import inspect
        from core import notify

        tree = ast.parse(inspect.getsource(notify))

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))

        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and id(n) not in docstrings]

        for banned in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "api.telegram.org"):
            for lit in literals:
                self.assertNotIn(banned, lit,
                                 "core.notify references " + banned + " in code")


class _Job:
    id = "brief"
    tier = "smart"


class _Models:
    """Stands in for ctx.models. Either raises or returns a canned completion."""

    def __init__(self, exc=None, text="a paragraph"):
        self._exc = exc
        self._text = text
        self.calls = 0

    def complete(self, prompt, tier=None, max_tokens=None):
        self.calls += 1
        if self._exc:
            raise self._exc
        from core.models import Completion
        return Completion(text=self._text, tokens_in=10, tokens_out=5,
                          provider="fake", model="fake", latency_ms=1, usd=0.0)


class _Notify:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class _Ctx:
    def __init__(self, models=None, dry_run=False):
        from datetime import datetime, timezone
        self.job = _Job()
        self.models = models or _Models()
        self.notify = _Notify()
        self.dry_run = dry_run
        self.now = datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)
        self.logs = []

    def log(self, msg, **kw):
        self.logs.append((msg, kw))


class TestBriefJobDegrades(unittest.TestCase):
    """The brief must survive every way the model can fail."""

    DIGEST = "spine brief - test\n1 item(s)\n\nDEALS\n  [ 60] a deed"

    def _patch_build(self, rows=1):
        """Keep the job away from the real store so these stay offline."""
        from collectors import brief as job
        rowlist = [{"key": "k", "kind": "deal", "source": "acris",
                    "importance": 60, "title": "a deed", "ts": "2026-09-30T00:00:00Z"}]
        original = job.brief_mod.build
        job.brief_mod.build = lambda now=None: (self.DIGEST, rowlist[:rows])
        self.addCleanup(setattr, job.brief_mod, "build", original)
        return job

    def test_a_model_refusal_does_not_stop_delivery(self):
        """PrivacyRefusal, BudgetRefusal and NoProviders all land here. The
        morning the gate refuses is a morning you still want the list."""
        from core.models import PrivacyRefusal
        job = self._patch_build()
        ctx = _Ctx(models=_Models(exc=PrivacyRefusal("nope")))
        out = job.run(ctx)
        self.assertEqual(len(ctx.notify.sent), 1)
        self.assertIn("a deed", ctx.notify.sent[0])
        self.assertFalse(out["stats"]["judgment"])
        self.assertTrue(out["stats"]["sent"])

    def test_a_dead_daemon_does_not_stop_delivery(self):
        job = self._patch_build()
        ctx = _Ctx(models=_Models(exc=OSError("connection refused")))
        job.run(ctx)
        self.assertEqual(len(ctx.notify.sent), 1)

    def test_an_empty_completion_is_treated_as_no_judgment(self):
        """A model that answers with whitespace has told you nothing, and
        appending a blank commentary block would only look broken."""
        job = self._patch_build()
        ctx = _Ctx(models=_Models(text="   "))
        out = job.run(ctx)
        self.assertFalse(out["stats"]["judgment"])
        self.assertNotIn("machine read", ctx.notify.sent[0])

    def test_judgment_is_appended_and_labelled_never_substituted(self):
        """The deterministic list is the record. Commentary sits after it and
        says what produced it, because a 4.7B model at Q4 is a second opinion."""
        job = self._patch_build()
        ctx = _Ctx(models=_Models(text="watch the Brooklyn mortgage"))
        job.run(ctx)
        sent = ctx.notify.sent[0]
        self.assertIn("a deed", sent)
        self.assertIn("machine read", sent)
        self.assertLess(sent.index("a deed"), sent.index("machine read"))

    def test_no_model_call_when_there_is_nothing_to_judge(self):
        """An empty queue does not need 40 seconds of inference to confirm it."""
        job = self._patch_build(rows=0)
        models = _Models()
        ctx = _Ctx(models=models)
        job.run(ctx)
        self.assertEqual(models.calls, 0)
        self.assertEqual(len(ctx.notify.sent), 1)

    def test_dry_run_sends_nothing(self):
        job = self._patch_build()
        ctx = _Ctx(dry_run=True)
        out = job.run(ctx)
        self.assertEqual(ctx.notify.sent, [])
        self.assertFalse(out["stats"]["sent"])


class TestSuppressedBreakdown(unittest.TestCase):
    """The footer says what the cap left out, not just how much."""

    NOW = datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)

    def _out(self, suppressed, total=200):
        return brief.render([row("a")], total_unacted=total,
                            suppressed=suppressed, now=self.NOW)

    def test_kinds_are_named_and_ordered_by_count(self):
        out = self._out({"signal": 1, "deal": 98, "fact": 24})
        line = [l for l in out.split(chr(10)) if l.startswith("+ ")][0]
        self.assertIn("98 deals", line)
        self.assertIn("24 positions", line)
        self.assertIn("1 signal", line)
        self.assertLess(line.index("98 deals"), line.index("24 positions"))
        self.assertLess(line.index("24 positions"), line.index("1 signal"))

    def test_one_of_something_reads_singular(self):
        """1 signals is the kind of thing that makes a daily message feel
        unmaintained."""
        out = self._out({"signal": 1})
        self.assertIn("1 signal", out)
        self.assertNotIn("1 signals", out)

    def test_many_stays_plural(self):
        out = self._out({"signal": 2})
        self.assertIn("2 signals", out)

    def test_a_zero_count_is_not_listed(self):
        out = self._out({"deal": 5, "alert": 0})
        self.assertIn("5 deals", out)
        self.assertNotIn("alert", out.split("+ ")[-1])

    def test_no_breakdown_still_gives_a_bare_count(self):
        """render stays usable without the breakdown -- layer 2 and any future
        caller may build rows by hand."""
        out = brief.render([row("a")], total_unacted=50, now=self.NOW)
        self.assertIn("+ 49 more:", out)

    def test_an_unknown_kind_is_named_not_swallowed(self):
        out = self._out({"invented": 3})
        self.assertIn("3 invented", out)


if __name__ == "__main__":
    unittest.main()
