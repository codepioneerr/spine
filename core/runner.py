"""
core.runner — executes one job, under the governor.

`bin/run.sh` holds the global flock and then hands off to this module. The
split is deliberate: **serialization is a process-level concern and belongs
in the shell** (flock on a file descriptor, released by the kernel even if
Python dies), while the window check, RAM guard, timeout and state-writing
are logic and belong somewhere testable.

    python3 -m core.runner <job_id> [--dry-run] [--force]

Exit codes, chosen so cron mail stays quiet for normal operation:

    0   ran successfully, or was deliberately skipped by the governor
    1   the job raised
    2   the job timed out
    3   no such job / the collector failed to load
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from core import governor, paths, registry
from core.job import Ctx, Job

EXIT_OK, EXIT_FAILED, EXIT_TIMEOUT, EXIT_NOJOB = 0, 1, 2, 3

# ─────────────────────────────────────────────────────────────────────────────
# the RAM-skip alert
# ─────────────────────────────────────────────────────────────────────────────
#
# The skip is correct and stays correct. CLAUDE.md section 3: a skipped run is
# recoverable, an OOM-killed box at 3am is not. Nothing below changes a
# decision, lowers a threshold, retries, or queues anything.
#
# What was wrong is that the skip was *silent*. `brief` needs 4,524 MiB free
# (3500 + 1024 headroom) and the box has 7,815 MiB total, so one interactive
# session left running overnight is enough to cross the line. When that
# happens Nick gets no 05:40 message and no explanation -- and "no brief
# arrived" is indistinguishable from "nothing happened". The mornings the
# guard fires are exactly the mornings worth knowing about.
#
# Spine already owns the delivery path (core.notify -> hermes --deliver-only,
# relayed verbatim, no agent invocation, no model cost), so this is a
# notification on an existing branch rather than new infrastructure.

# Stay quiet about the same job for this long after alerting once. The guard
# can refuse on every tick of a frequent job, and an alert per tick is how a
# Telegram channel gets muted -- at which point the one alert that mattered
# goes unread too. Six hours clears by morning and can never stack overnight.
ALERT_QUIET_S = 6 * 3600

# Set SPINE_RAM_ALERT=0 to silence this without touching code. Present so the
# answer to a noisy channel is a config line, not a patch that then has to be
# remembered and reverted.
ALERT_ENV = "SPINE_RAM_ALERT"


def _alert_state_path() -> str:
    """Where the throttle remembers its last send.

    A small json file, not the item store. Same reasoning that took heartbeat
    out of it (CLAUDE.md 7a): "the guard refused again" is infrastructure
    telemetry, not something Nick acts on, and it must not take a slot in a
    brief that shows the top 25 unacted items.
    """
    return paths.var("state", "ram_alerts.json")


def _should_alert(job_id: str, now: float, path: str | None = None) -> bool:
    """True at most once per ALERT_QUIET_S per job. Records the send.

    Fails open: if the state file is unreadable we alert rather than stay
    quiet, because a broken throttle should cost noise, not silence.
    """
    path = path or _alert_state_path()
    try:
        with open(path) as fh:
            sent = json.load(fh)
        if not isinstance(sent, dict):
            sent = {}
    except Exception:
        sent = {}

    last = sent.get(job_id)
    if isinstance(last, (int, float)) and now - last < ALERT_QUIET_S:
        return False

    sent[job_id] = now
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sent, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass
    return True


def _alert_text(job: Job, decision: governor.Decision) -> str:
    # decision.avail_mb, not a fresh read: the message must quote the figure
    # the refusal was made on, or it can contradict the refusal it is
    # reporting.
    avail = decision.avail_mb
    headroom = governor.Settings.from_env().headroom_mb
    need = job.ram_required_mb(headroom)
    return (
        f"spine: {job.id} SKIPPED — not enough RAM\n\n"
        f"needs {need} MB free ({job.ram_mb} + {need - job.ram_mb} headroom)\n"
        f"had   {avail if avail is not None else '?'} MB at "
        f"{_now()}\n\n"
        "The guard refused rather than risk an OOM, which is the designed "
        "behaviour — nothing swapped and nothing crashed. The run was skipped, "
        "not queued, so it will not catch up later.\n\n"
        "Most likely cause: an interactive session (VSCode, Copilot, Claude "
        "Code) left running overnight. Closing it frees ~1.5-2.5 GB.\n\n"
        "The item store is untouched — bin/items still has the queue."
    )


def _alert_ram_skip(job: Job, decision: governor.Decision, log,
                    notifier=None) -> bool:
    """Announce a RAM refusal. Best-effort, throttled, never raises.

    Every failure path here is swallowed on purpose. A skip that could not be
    announced is still a correct skip, and letting this raise would convert an
    orderly refusal into a cron failure — the exact outcome the guard exists to
    prevent. So the alert can fail; the skip cannot.
    """
    if os.environ.get(ALERT_ENV, "1").strip().lower() in ("0", "false", "no"):
        log("ram alert suppressed", by=ALERT_ENV)
        return False

    try:
        if notifier is None:
            from core import notify as notify_mod
            from core.job import _Secrets
            notifier = notify_mod.Notifier(secrets=_Secrets(registry.root()))

        if not notifier.configured():
            # Not a failure to hide: delivery was never set up, and the skip
            # is already in the log and the state file.
            log("ram alert not sent", reason="delivery not configured")
            return False

        if not _should_alert(job.id, time.time()):
            log("ram alert throttled",
                quiet_for_s=ALERT_QUIET_S, job=job.id)
            return False

        notifier.send(_alert_text(job, decision))
        log("ram alert sent", job=job.id)
        return True
    except Exception as exc:
        log("ram alert FAILED", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(job_id: str, stream=None):
    stream = stream or sys.stderr

    def log(msg, **kw):
        extra = " ".join(f"{k}={v}" for k, v in kw.items())
        stream.write(f"{_now()} [{job_id}] {msg}{' ' + extra if extra else ''}\n")
        stream.flush()
    return log


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout()


def run_job(job: Job, *, dry_run: bool = False, force: bool = False,
            when: datetime | None = None, log=None,
            notifier=None) -> tuple[int, dict]:
    """Run one job. Always returns a state dict, even on failure.

    `notifier` is injectable so the tests can exercise the RAM-skip alert
    without a live hermes route — and, more importantly, without the suite
    quietly sending Nick a Telegram message every time it runs.
    """
    log = log or make_logger(job.id)
    when = when or datetime.now(timezone.utc)
    started = time.time()

    state = {
        "job": job.id,
        "started_at": _now(),
        "window": job.window,
        "weight": job.weight,
        "ram_mb": job.ram_mb,
    }

    decision = governor.admit(job, when=when)
    if force and not decision:
        log(f"governor said no ({decision.reason}) — overridden by --force")
        decision = governor.Decision(True, f"forced past: {decision.reason}")

    if not decision:
        log(f"SKIP  {decision.reason}")
        state.update(outcome="skipped", reason=decision.reason,
                     gate=decision.gate, finished_at=_now(), duration_s=0.0)
        # Only the RAM gate alerts. A window refusal is routine — a night job
        # invoked at noon is bookkeeping, not news — and alerting on it would
        # bury the one refusal that means a job Nick expected did not happen.
        if decision.gate == "ram":
            state["alerted"] = _alert_ram_skip(job, decision, log,
                                               notifier=notifier)
        registry.write_state(job.id, state)
        return EXIT_OK, state

    log(f"START {decision.reason}")

    if dry_run:
        log("DRY-RUN — run(ctx) not called")
        state.update(outcome="dry-run", reason=decision.reason,
                     finished_at=_now(), duration_s=0.0)
        registry.write_state(job.id, state)
        return EXIT_OK, state

    ctx = Ctx.build(job, log, registry.root(), dry_run=False)

    prev = None
    if hasattr(signal, "SIGALRM"):
        prev = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(job.timeout)

    try:
        result = job.run(ctx) or {}
        elapsed = round(time.time() - started, 2)
        items = len(result.get("items", []) or [])
        stats = result.get("stats", {}) or {}
        log(f"OK    {elapsed}s items={items}")
        state.update(outcome="ok", items=items, stats=stats,
                     finished_at=_now(), duration_s=elapsed)
        code = EXIT_OK

    except _Timeout:
        elapsed = round(time.time() - started, 2)
        log(f"TIMEOUT after {job.timeout}s — killed, lock released")
        state.update(outcome="timeout", finished_at=_now(),
                     duration_s=elapsed,
                     error=f"exceeded timeout of {job.timeout}s")
        code = EXIT_TIMEOUT

    except Exception as exc:
        elapsed = round(time.time() - started, 2)
        log(f"FAIL  {type(exc).__name__}: {exc}")
        state.update(outcome="failed", finished_at=_now(),
                     duration_s=elapsed,
                     error=f"{type(exc).__name__}: {exc}",
                     traceback=traceback.format_exc()[-2000:])
        code = EXIT_FAILED

    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            if prev is not None:
                signal.signal(signal.SIGALRM, prev)

    registry.write_state(job.id, state)
    return code, state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.runner")
    ap.add_argument("job_id")
    ap.add_argument("--dry-run", action="store_true",
                    help="check the gates, do not call run(ctx)")
    ap.add_argument("--force", action="store_true",
                    help="override the window and RAM gates (manual use only)")
    ap.add_argument("--json", action="store_true", help="emit the state dict")
    args = ap.parse_args(argv)

    jobs, errors = registry.discover()
    by_id = {j.id: j for j in jobs}

    if args.job_id not in by_id:
        sys.stderr.write(f"no such job: {args.job_id}\n")
        if errors:
            sys.stderr.write("collectors that failed to load:\n")
            for e in errors:
                sys.stderr.write(f"  {e}\n")
        known = ", ".join(sorted(by_id)) or "(none registered)"
        sys.stderr.write(f"known jobs: {known}\n")
        return EXIT_NOJOB

    code, state = run_job(by_id[args.job_id],
                          dry_run=args.dry_run, force=args.force)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
