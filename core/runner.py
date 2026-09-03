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

from core import governor, registry
from core.job import Ctx, Job

EXIT_OK, EXIT_FAILED, EXIT_TIMEOUT, EXIT_NOJOB = 0, 1, 2, 3


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
            when: datetime | None = None, log=None) -> tuple[int, dict]:
    """Run one job. Always returns a state dict, even on failure."""
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
                     finished_at=_now(), duration_s=0.0)
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
