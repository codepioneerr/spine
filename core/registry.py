"""
core.registry — discovers collectors and owns the crontab.

Nobody hand-edits cron again. That is the whole point of this file.

The crontab is written as a **marked block**:

    # >>> spine: managed block, do not edit by hand >>>
    ...generated lines...
    # <<< spine: managed block <<<

Everything outside those markers is preserved byte for byte. That is not
politeness — it is the mechanism that keeps `darkweb-jobs` running untouched
while Spine is built alongside it, and the reason installing a new schedule
is a safe operation rather than a destructive one.

    python3 -m core.registry --list       what Spine knows about
    python3 -m core.registry --show       the crontab block it would write
    python3 -m core.registry --diff       what installing would change
    python3 -m core.registry --install    write it (asks first)
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import importlib.util
import json
import os
import pkgutil
import subprocess
import sys
from datetime import datetime, timezone

from core import cron, governor
from core.job import Job, JobError, validate

BEGIN = "# >>> spine: managed block, do not edit by hand >>>"
END = "# <<< spine: managed block <<<"


def root() -> str:
    return os.environ.get("SPINE_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# discovery
# ─────────────────────────────────────────────────────────────────────────────

def discover(package: str = "collectors") -> tuple[list[Job], list[str]]:
    """Import every collector module and validate its META.

    Returns (jobs, errors). A broken collector does not stop the others from
    loading — but it does keep the registry from installing, because a
    crontab generated from a partially-loaded set would silently drop jobs.
    """
    jobs: list[Job] = []
    errors: list[str] = []

    base = root()
    if base not in sys.path:
        sys.path.insert(0, base)

    pkg_path = os.path.join(base, package)
    if not os.path.isdir(pkg_path):
        return jobs, [f"no {package}/ directory at {base}"]

    for mod in pkgutil.iter_modules([pkg_path]):
        if mod.name.startswith("_"):
            continue
        name = f"{package}.{mod.name}"
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            errors.append(f"{name}: import failed — {exc}")
            continue

        meta = getattr(module, "META", None)
        if meta is None:
            errors.append(f"{name}: no META block — every collector declares "
                          "one (see core.job)")
            continue
        runner = getattr(module, "run", None)
        if not callable(runner):
            errors.append(f"{name}: no run(ctx) function")
            continue

        try:
            job = validate(meta, module=name)
        except JobError as exc:
            errors.append(str(exc))
            continue

        jobs.append(Job(**{**job.__dict__, "run": runner}))

    seen: dict[str, str] = {}
    for j in jobs:
        if j.id in seen:
            errors.append(
                f"duplicate job id {j.id!r} in {seen[j.id]} and {j.module}")
        seen[j.id] = j.module

    jobs.sort(key=lambda j: j.id)
    return jobs, errors


def audit(jobs: list[Job]) -> list[str]:
    """Non-fatal warnings: schedules that fight their own window."""
    settings = governor.Settings.from_env()
    out: list[str] = []
    for j in jobs:
        out.extend(governor.audit_schedule(j, settings))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# state — what the console shows until the item store exists (Phase 2)
# ─────────────────────────────────────────────────────────────────────────────

def state_dir() -> str:
    return os.path.join(root(), "var", "state")


def read_state(job_id: str) -> dict:
    path = os.path.join(state_dir(), f"{job_id}.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state(job_id: str, data: dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    path = os.path.join(state_dir(), f"{job_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def all_jobs() -> list[dict]:
    """Rows for bin/darkweb's SCHEDULE panel."""
    jobs, _ = discover()
    rows = []
    for j in jobs:
        st = read_state(j.id)
        rows.append({**j.as_row(),
                     "last": st.get("finished_at", "—"),
                     "outcome": st.get("outcome", "—"),
                     "duration": st.get("duration_s")})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# crontab generation
# ─────────────────────────────────────────────────────────────────────────────

def block(jobs: list[Job]) -> str:
    base = root()
    run = os.path.join(base, "bin", "run.sh")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        BEGIN,
        f"# generated by core.registry at {stamp} — edits here are lost.",
        "# Schedules are UTC. SPINE_TZ only governs the day/night window,",
        "# which bin/run.sh evaluates at run time. Do not add CRON_TZ.",
        "SHELL=/bin/bash",
        "PATH=/usr/local/bin:/usr/bin:/bin",
    ]

    if not jobs:
        lines.append("# (no collectors registered yet)")

    for j in jobs:
        if not j.enabled:
            lines.append(f"# {j.id}: disabled in META")
            continue
        note = f"{j.weight}/{j.window}, {j.ram_mb} MB — {cron.describe(j.schedule)}"
        if j.description:
            note = f"{j.description} | {note}"
        lines.append(f"# {j.id}: {note}")
        lines.append(f"{j.schedule} {run} {j.id}")

    lines.append(END)
    return "\n".join(lines) + "\n"


def current_crontab() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def splice(existing: str, new_block: str) -> str:
    """Replace the managed block, preserving every foreign line.

    This function is the one that must never be wrong. `darkweb-jobs` lives
    in the same crontab and is not ours to touch.
    """
    lines = existing.splitlines(keepends=True)
    out, inside, replaced = [], False, False

    for line in lines:
        stripped = line.strip()
        if stripped == BEGIN:
            inside = True
            out.append(new_block)
            replaced = True
            continue
        if stripped == END:
            inside = False
            continue
        if not inside:
            out.append(line)

    if inside:
        raise RuntimeError(
            "crontab has an unterminated spine block — refusing to write. "
            "Inspect it by hand with `crontab -l`.")

    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if out:
            out.append("\n")
        out.append(new_block)

    return "".join(out)


def install(text: str) -> None:
    p = subprocess.run(["crontab", "-"], input=text, text=True,
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"crontab install failed: {p.stderr.strip()}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.registry")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="registered collectors")
    g.add_argument("--show", action="store_true", help="print the block")
    g.add_argument("--diff", action="store_true", help="what would change")
    g.add_argument("--install", action="store_true", help="write the crontab")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args(argv)

    jobs, errors = discover()
    warnings = audit(jobs)

    for e in errors:
        print(f"ERROR  {e}", file=sys.stderr)
    for w in warnings:
        print(f"WARN   {w}", file=sys.stderr)

    if args.list:
        if not jobs:
            print("no collectors registered yet "
                  "(collectors/ is empty — that is expected at Phase 0)")
        for j in jobs:
            st = read_state(j.id)
            print(f"{j.id:<16} {j.schedule:<14} {j.window:<6} {j.weight:<6} "
                  f"{j.ram_mb:>5} MB  last={st.get('finished_at', '—')}")
        return 1 if errors else 0

    if args.show:
        print(block(jobs), end="")
        return 1 if errors else 0

    new = splice(current_crontab(), block(jobs))

    if args.diff:
        cur = current_crontab()
        d = list(difflib.unified_diff(
            cur.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="crontab (current)", tofile="crontab (proposed)"))
        sys.stdout.writelines(d or ["(no change)\n"])
        return 1 if errors else 0

    # --install
    if errors:
        print("\nRefusing to install: fix the errors above first. A crontab "
              "generated from a partially-loaded set silently drops jobs.",
              file=sys.stderr)
        return 2

    if not args.yes:
        cur = current_crontab()
        foreign = len([l for l in cur.splitlines()
                       if l.strip() and not l.strip().startswith("#")])
        print(f"About to rewrite the crontab. {foreign} existing "
              f"line(s) outside the spine block will be preserved.")
        print(f"Spine block: {len([j for j in jobs if j.enabled])} job(s).")
        try:
            if input("Type 'install' to continue: ").strip() != "install":
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1

    install(new)
    print(f"installed — {len([j for j in jobs if j.enabled])} job(s) in the "
          "spine block; everything else preserved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
