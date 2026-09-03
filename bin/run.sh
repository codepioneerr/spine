#!/usr/bin/env bash
#
# bin/run.sh — the only way a job starts.
#
#   bin/run.sh <job_id> [--dry-run] [--force]
#
# This script does exactly one thing that could not be done in Python: it
# takes a GLOBAL, NON-BLOCKING flock before handing off to core.runner.
#
# Why the shell and not Python: flock(2) on a file descriptor is released by
# the kernel when the process dies, however it dies — SIGKILL, OOM killer,
# power cut. A lock held in application code is not. On a box whose whole
# design assumption is "never run two things at once," the lock has to
# survive the worst case, which is precisely the case where Python is not
# around to clean up.
#
# Non-blocking is also deliberate. If another job holds the lock, this run is
# SKIPPED, not queued. Queueing on an 8 GB box means a backlog that lands all
# at once at 06:00. A skipped run happens again tomorrow. See CLAUDE.md
# section 2 and section 3.
#
set -uo pipefail

JOB="${1:-}"
if [ -z "$JOB" ]; then
  echo "usage: bin/run.sh <job_id> [--dry-run] [--force]" >&2
  exit 64
fi
shift

SPINE_ROOT="${SPINE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export SPINE_ROOT

LOCK="${SPINE_LOCK:-/tmp/spine.lock}"
LOG_DIR="$SPINE_ROOT/var/log"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$JOB.log"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Load .env if it is there and safely permissioned. A world-readable .env on
# a box whose repo is public is not a warning, it is a stop.
if [ -f "$SPINE_ROOT/.env" ]; then
  MODE=$(stat -c '%a' "$SPINE_ROOT/.env" 2>/dev/null || stat -f '%Lp' "$SPINE_ROOT/.env")
  if [ "$MODE" != "600" ]; then
    echo "$(stamp) [$JOB] REFUSE .env is mode $MODE, must be 600" | tee -a "$LOG" >&2
    exit 77
  fi
  set -a
  # shellcheck disable=SC1091
  . "$SPINE_ROOT/.env"
  set +a
fi

PYTHON="${SPINE_PYTHON:-python3}"

exec 9>"$LOCK" || { echo "$(stamp) [$JOB] cannot open $LOCK" >&2; exit 70; }

if ! flock -n 9; then
  HOLDER=$(cat "$LOCK" 2>/dev/null || echo "?")
  echo "$(stamp) [$JOB] SKIP  global lock held by pid ${HOLDER:-?} — not queued" \
    | tee -a "$LOG"
  exit 0
fi

echo $$ >&9

cd "$SPINE_ROOT" || exit 70
"$PYTHON" -m core.runner "$JOB" "$@" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}

# The lock releases when fd 9 closes at exit. Nothing to clean up by hand,
# which is the point.
exit "$RC"
