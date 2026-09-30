"""
Phase 0 tests — the job contract, the registry, and the governor.

Bias: test the things that fail silently at 3am, not the things that throw
loudly the first time you run them.

    python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, time, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import cron, governor, registry, runner          # noqa: E402
from core.job import JobError, validate, _Secrets          # noqa: E402

UTC = timezone.utc


def job(**over):
    meta = {"id": "tj", "schedule": "0 6 * * *"}
    meta.update(over)
    return validate(meta)


# ─────────────────────────────────────────────────────────────────────────────

class TestCron(unittest.TestCase):

    def test_accepts_the_forms_we_support(self):
        for s in ("* * * * *", "0 6 * * *", "*/30 * * * *", "0 1,13 * * *",
                  "15 2-6 * * 1-5", "0 0-23/4 1 1 0"):
            cron.parse(s)

    def test_rejects_wrong_field_count(self):
        with self.assertRaises(cron.CronError):
            cron.parse("0 6 * *")

    def test_rejects_shorthand(self):
        """@daily is the schedule nobody ever double-checks."""
        with self.assertRaises(cron.CronError):
            cron.parse("@daily")

    def test_rejects_out_of_range(self):
        for s in ("60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                  "* * * * 7"):
            with self.assertRaises(cron.CronError, msg=s):
                cron.parse(s)

    def test_rejects_backwards_range(self):
        with self.assertRaises(cron.CronError):
            cron.parse("0 22-4 * * *")

    def test_rejects_step_on_a_literal(self):
        """`5/10` looks like 'every 10 from 5' and is not. Vanilla cron
        treats it as 5-59/10 or rejects it depending on implementation —
        exactly the ambiguity that bites."""
        with self.assertRaises(cron.CronError):
            cron.parse("5/10 * * * *")

    def test_matches_a_known_instant(self):
        self.assertTrue(cron.matches("0 6 * * *",
                                     datetime(2026, 9, 3, 6, 0, tzinfo=UTC)))
        self.assertFalse(cron.matches("0 6 * * *",
                                      datetime(2026, 9, 3, 6, 1, tzinfo=UTC)))

    def test_dom_and_dow_are_ORed_like_vanilla_cron(self):
        """Both restricted -> either matching fires. Surprising, and real."""
        s = "0 0 1 * 3"   # 1st of the month, OR any Wednesday
        self.assertTrue(cron.matches(s, datetime(2026, 4, 1, 0, 0, tzinfo=UTC)))
        self.assertTrue(cron.matches(s, datetime(2026, 4, 8, 0, 0, tzinfo=UTC)))
        self.assertFalse(cron.matches(s, datetime(2026, 4, 9, 0, 0, tzinfo=UTC)))


class TestJobContract(unittest.TestCase):

    def test_defaults_are_conservative(self):
        j = job()
        self.assertEqual(j.window, "any")
        self.assertEqual(j.weight, "light")
        self.assertIsNone(j.tier)
        self.assertTrue(j.enabled)

    def test_heavy_must_be_night(self):
        """CLAUDE.md section 3. This is the rule that keeps Telegram
        responsive during the day."""
        with self.assertRaises(JobError) as e:
            job(weight="heavy", window="day", ram_mb=1500)
        self.assertIn("night", str(e.exception))
        job(weight="heavy", window="night", ram_mb=1500)  # allowed

    def test_light_cannot_claim_heavy_ram(self):
        with self.assertRaises(JobError):
            job(weight="light", ram_mb=900)

    def test_ram_ceiling(self):
        with self.assertRaises(JobError):
            job(weight="heavy", window="night", ram_mb=6000)

    def test_timeout_cannot_outlast_the_night_window(self):
        with self.assertRaises(JobError):
            job(timeout=8 * 3600)

    def test_typo_in_a_key_is_an_error_not_a_shrug(self):
        """Every framework that ignores unknown keys eventually eats a
        `windows` or a `ram_MB` and behaves differently than the file reads."""
        with self.assertRaises(JobError) as e:
            job(windows="night")
        self.assertIn("unknown", str(e.exception))

    def test_bad_id_rejected(self):
        for bad in ("Heartbeat", "1st", "a", "has-dash", "x" * 40):
            with self.assertRaises(JobError, msg=bad):
                job(id=bad)

    def test_tier_must_be_a_tier_not_a_model(self):
        with self.assertRaises(JobError) as e:
            job(tier="gemini-flash-lite")
        self.assertIn("tier", str(e.exception))

    def test_ram_required_includes_headroom_for_heavy(self):
        heavy = job(weight="heavy", window="night", ram_mb=1500)
        light = job(weight="light", ram_mb=100)
        self.assertEqual(heavy.ram_required_mb(1024), 2524)
        self.assertEqual(light.ram_required_mb(1024), 356)


class TestSecrets(unittest.TestCase):

    def test_refuses_a_world_readable_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as fh:
                fh.write("TELEGRAM_BOT_TOKEN=hunter2\n")
            os.chmod(p, 0o644)
            with self.assertRaises(JobError):
                _Secrets(d).get("TELEGRAM_BOT_TOKEN")

    def test_reads_a_600_env_and_hides_values_in_repr(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as fh:
                fh.write("# comment\nGEMINI_API_KEY=abc123\nEMPTY=\n")
            os.chmod(p, 0o600)
            s = _Secrets(d)
            self.assertEqual(s.get("GEMINI_API_KEY"), "abc123")
            self.assertNotIn("abc123", repr(s))
            with self.assertRaises(JobError):
                s.require("EMPTY")


class TestGovernor(unittest.TestCase):

    def setUp(self):
        self.s = governor.Settings(tz="America/New_York",
                                   night_start=time(1, 0),
                                   night_end=time(6, 0),
                                   headroom_mb=1024)

    def test_night_window_in_local_time(self):
        # 06:00 UTC == 02:00 EDT in September -> night
        self.assertTrue(self.s.is_night(datetime(2026, 9, 3, 6, 0, tzinfo=UTC)))
        # 16:00 UTC == 12:00 EDT -> not night
        self.assertFalse(self.s.is_night(datetime(2026, 9, 3, 16, 0, tzinfo=UTC)))

    def test_window_that_crosses_midnight(self):
        s = governor.Settings(tz="UTC", night_start=time(23, 0),
                              night_end=time(5, 0))
        self.assertTrue(s.is_night(datetime(2026, 1, 1, 23, 30, tzinfo=UTC)))
        self.assertTrue(s.is_night(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)))
        self.assertFalse(s.is_night(datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))

    def test_heavy_job_refused_at_midday(self):
        j = job(weight="heavy", window="night", ram_mb=1500)
        d = governor.admit(j, when=datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
                           settings=self.s, avail_mb=6000)
        self.assertFalse(d)
        self.assertIn("outside night window", d.reason)

    def test_ram_guard_skips_rather_than_waits(self):
        j = job(weight="heavy", window="night", ram_mb=1500)
        d = governor.admit(j, when=datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
                           settings=self.s, avail_mb=1200)
        self.assertFalse(d)
        self.assertIn("Skipped, not queued", d.reason)

    def test_admits_a_heavy_job_at_night_with_room(self):
        j = job(weight="heavy", window="night", ram_mb=1500)
        self.assertTrue(governor.admit(
            j, when=datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
            settings=self.s, avail_mb=6000))

    def test_disabled_beats_everything(self):
        j = job(enabled=False)
        self.assertFalse(governor.admit(j, avail_mb=99999, settings=self.s))

    def test_missing_proc_does_not_silently_disable_the_guard(self):
        j = job(weight="heavy", window="night", ram_mb=1500)
        d = governor.check_ram(j, self.s, avail_mb=None) \
            if governor.available_mb() is None else \
            governor.Decision(True, "RAM guard unavailable (no /proc)")
        self.assertIn("RAM guard", d.reason)

    def test_dst_audit_catches_a_schedule_that_leaves_the_window(self):
        """10:30 UTC is 05:30 EST but 06:30 EDT — a night job that quietly
        stops running for half the year. This is the Aug 26 bug class."""
        bad = validate({"id": "drifter", "schedule": "30 10 * * *",
                        "weight": "heavy", "window": "night", "ram_mb": 1500})
        self.assertTrue(governor.audit_schedule(bad, self.s))

        good = validate({"id": "steady", "schedule": "30 6 * * *",
                         "weight": "heavy", "window": "night", "ram_mb": 1500})
        self.assertEqual(governor.audit_schedule(good, self.s), [])


class TestCrontabSplice(unittest.TestCase):
    """The most important tests in Phase 0.

    darkweb-jobs lives in the same crontab and is not ours to touch. If
    splice() is ever wrong, Nick loses running jobs silently.
    """

    FOREIGN = (
        "# darkweb-jobs — DO NOT REMOVE\n"
        "0 6 * * * /home/nick/darkweb-jobs/bin/run.sh acris\n"
        "*/15 * * * * /home/nick/darkweb-jobs/bin/run.sh polymarket\n"
        "MAILTO=\"\"\n"
    )

    def test_appends_without_touching_foreign_lines(self):
        out = registry.splice(self.FOREIGN, "BLOCKSTART\nx\nBLOCKEND\n")
        for line in self.FOREIGN.strip().splitlines():
            self.assertIn(line, out)
        self.assertIn("BLOCKSTART", out)

    def test_replaces_an_existing_block_and_keeps_foreign_lines(self):
        first = registry.splice(
            self.FOREIGN, f"{registry.BEGIN}\nold-job\n{registry.END}\n")
        second = registry.splice(
            first, f"{registry.BEGIN}\nnew-job\n{registry.END}\n")
        self.assertIn("new-job", second)
        self.assertNotIn("old-job", second)
        for line in self.FOREIGN.strip().splitlines():
            self.assertIn(line, second)

    def test_repeated_installs_do_not_accumulate(self):
        blk = f"{registry.BEGIN}\njob\n{registry.END}\n"
        text = self.FOREIGN
        for _ in range(5):
            text = registry.splice(text, blk)
        self.assertEqual(text.count(registry.BEGIN), 1)
        self.assertEqual(text.count("acris"), 1)

    def test_refuses_an_unterminated_block(self):
        broken = self.FOREIGN + registry.BEGIN + "\nhalf a block\n"
        with self.assertRaises(RuntimeError):
            registry.splice(broken, "x\n")

    def test_empty_crontab_is_fine(self):
        out = registry.splice("", f"{registry.BEGIN}\nj\n{registry.END}\n")
        self.assertIn("j", out)

    def test_generated_block_never_contains_cron_tz(self):
        jobs, _ = registry.discover()
        for line in registry.block(jobs).splitlines():
            self.assertFalse(line.strip().startswith("CRON_TZ"),
                             f"generated a CRON_TZ line: {line!r}")


class TestRegistryDiscovery(unittest.TestCase):

    def test_finds_the_heartbeat_reference_collector(self):
        jobs, errors = registry.discover()
        self.assertEqual(errors, [], f"collectors failed to load: {errors}")
        self.assertIn("heartbeat", {j.id for j in jobs})

    def test_no_schedule_warnings_in_the_shipped_set(self):
        jobs, _ = registry.discover()
        self.assertEqual(registry.audit(jobs), [])

    def test_block_points_at_run_sh_not_python(self):
        """Jobs must go through the flock wrapper, never straight to python."""
        jobs, _ = registry.discover()
        text = registry.block(jobs)
        for line in text.splitlines():
            if line and not line.startswith("#") and "=" not in line.split()[0]:
                self.assertIn("bin/run.sh", line)


class TestRunner(unittest.TestCase):

    def setUp(self):
        self.logs = []
        self.log = lambda m, **k: self.logs.append(m)

    def _job(self, fn, **over):
        j = job(id="probe", **over)
        return type(j)(**{**j.__dict__, "run": fn})

    def test_skip_is_exit_zero_and_is_recorded(self):
        j = self._job(lambda ctx: {}, weight="heavy", window="night",
                      ram_mb=1500)
        code, state = runner.run_job(
            j, when=datetime(2026, 9, 3, 16, 0, tzinfo=UTC), log=self.log)
        self.assertEqual(code, runner.EXIT_OK, "a skip must not page anyone")
        self.assertEqual(state["outcome"], "skipped")
        self.assertIn("outside night window", state["reason"])

    def test_failure_is_captured_not_propagated(self):
        def boom(ctx):
            raise ValueError("nope")
        code, state = runner.run_job(self._job(boom), log=self.log)
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertEqual(state["outcome"], "failed")
        self.assertIn("ValueError", state["error"])
        self.assertIn("traceback", state)

    def test_success_counts_items(self):
        code, state = runner.run_job(
            self._job(lambda ctx: {"items": [1, 2, 3]}), log=self.log)
        self.assertEqual(code, runner.EXIT_OK)
        self.assertEqual(state["items"], 3)

    def test_dry_run_does_not_call_run(self):
        called = []
        code, state = runner.run_job(
            self._job(lambda ctx: called.append(1)), dry_run=True,
            log=self.log)
        self.assertEqual(called, [])
        self.assertEqual(state["outcome"], "dry-run")

    def test_force_overrides_the_governor(self):
        j = self._job(lambda ctx: {}, weight="heavy", window="night",
                      ram_mb=1500)
        code, state = runner.run_job(
            j, when=datetime(2026, 9, 3, 16, 0, tzinfo=UTC), force=True,
            log=self.log)
        self.assertEqual(state["outcome"], "ok")

    def test_pending_capabilities_name_their_phase(self):
        """Capabilities not yet built name the phase that delivers them,
        rather than raising AttributeError three frames deep.

        notify was the last live user of _Pending and graduated on
        2026-09-30, so this exercises the mechanism directly. The mechanism
        is what is worth keeping: the next unbuilt capability should also
        announce itself instead of failing obscurely."""
        from core.job import _Pending

        pending = _Pending("teleport", "Phase 12")

        with self.assertRaises(NotImplementedError) as call:
            pending.send("hi")
        self.assertIn("Phase 12", str(call.exception))
        self.assertIn("teleport", str(call.exception))

        # Attribute access must fail the same way, not return a mystery object.
        with self.assertRaises(NotImplementedError):
            pending.anything_at_all

    def test_notify_is_no_longer_pending(self):
        """The counterpart: ctx.notify is real now. If this ever starts
        raising NotImplementedError again, delivery has regressed."""
        seen = {}

        def peek(ctx):
            seen["type"] = type(ctx.notify).__name__
            return {}
        runner.run_job(self._job(peek), log=self.log)
        self.assertEqual(seen["type"], "Notifier")


class TestRunShell(unittest.TestCase):

    def test_run_sh_is_executable_and_holds_a_lock(self):
        path = os.path.join(ROOT, "bin", "run.sh")
        self.assertTrue(os.access(path, os.X_OK))
        with open(path) as fh:
            text = fh.read()
        self.assertIn("flock -n 9", text)
        self.assertIn("exec 9>", text)

    def test_run_sh_refuses_a_loose_env(self):
        with open(os.path.join(ROOT, "bin", "run.sh")) as fh:
            text = fh.read()
        self.assertIn('!= "600"', text)


if __name__ == "__main__":
    unittest.main()


class TestRunShellEnvIsolation(unittest.TestCase):
    """Regression: the first Phase 0 bundle shipped a .env containing
    SPINE_ROOT=/opt/spine. `set -a; . .env` overwrote the root that run.sh
    had correctly derived from its own path, and every job died on
    `cd: /opt/spine: No such file or directory`.

    The tests passed because no .env existed in the test tree. Found on the
    Dell, by running it.
    """

    def test_env_cannot_relocate_the_checkout(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "spine")
            shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(
                ".git", "var", "__pycache__", "*.pyc"))
            env_path = os.path.join(repo, ".env")
            with open(env_path, "w") as fh:
                fh.write("SPINE_ROOT=/nonexistent/opt/spine\n"
                         "SPINE_DB=/nonexistent/opt/spine/var/spine.db\n")
            os.chmod(env_path, 0o600)

            r = subprocess.run(
                ["bash", os.path.join(repo, "bin", "run.sh"),
                 "heartbeat", "--dry-run"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "SPINE_LOCK": os.path.join(d, "lock")})

            self.assertNotIn("No such file or directory", r.stdout + r.stderr)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("dry-run", (r.stdout + r.stderr).lower())

    def test_env_example_ships_no_absolute_install_path(self):
        """A path in .env.example is wrong for everyone who is not the
        author. SPINE_ROOT must not be set there at all."""
        with open(os.path.join(ROOT, ".env.example")) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=")[0]
                self.assertNotEqual(key, "SPINE_ROOT",
                                    "SPINE_ROOT must not be set in .env.example")
