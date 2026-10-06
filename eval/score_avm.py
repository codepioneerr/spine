#!/usr/bin/env python3
"""
eval/score_avm.py — the frozen scorer for residential AVM candidates.

    python3 eval/score_avm.py --predictions preds.csv --output-metrics metrics.json

This file and the benchmark it reads are the trust boundary for any
automated optimisation loop. A worker may change how values are predicted;
it may never change how they are graded. Edits here are human edits.

## Input

`--predictions`: CSV (or Parquet, only if pyarrow happens to be installed —
Spine itself is stdlib-only, CLAUDE.md §5) with columns

    sale_id, predicted_price

and exactly one row per row of `benchmark_test_X.csv`.

The benchmark (eval/build_benchmark.py) lives under var/avm_benchmark/. Its
manifest records a SHA-256 for every file; scoring refuses to run if any
file no longer matches, so a quietly edited answer key fails loudly.

## Output

JSON on stdout and in `--output-metrics`. APE_i = |ŷ_i − y_i| / y_i.

    MdAPE                         primary
    Mean_APE, PE10, PE20, PE30    secondary accuracy
    P75/P90/P95/P99/Max_APE       tail, diagnostic only
    Catastrophic_Rate             share with APE > 1.0

Exit 0 when the evaluation passes, 1 when it does not:

- any prediction NaN, Inf or <= 0, or not a number,
- prediction count or sale_id set differs from the test set,
- Catastrophic_Rate > 0.05,
- MdAPE worse than the baseline (`--baseline`, default
  var/avm_benchmark/baseline_metrics.json when it exists). Promoting a new
  baseline is a human decision: copy a metrics file over it by hand.
- the benchmark failed its integrity check.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BENCH = os.path.join(ROOT, "var", "avm_benchmark")

X_FILE = "benchmark_test_X.csv"
Y_FILE = "benchmark_test_y.csv"
MANIFEST = "manifest.json"
BASELINE = "baseline_metrics.json"

CATASTROPHIC_APE = 1.0
MAX_CATASTROPHIC_RATE = 0.05


class EvalFailure(Exception):
    """A deterministic check failed; the message says which."""


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(bench_dir: str) -> dict:
    """The manifest, after checking every listed file still hashes the same."""
    path = os.path.join(bench_dir, MANIFEST)
    if not os.path.exists(path):
        raise EvalFailure(f"no benchmark at {bench_dir} (run eval/build_benchmark.py)")
    with open(path) as f:
        manifest = json.load(f)
    for name, digest in manifest["sha256"].items():
        fp = os.path.join(bench_dir, name)
        if not os.path.exists(fp) or sha256(fp) != digest:
            raise EvalFailure(f"benchmark integrity: {name} does not match manifest")
    return manifest


def read_rows(path: str) -> list[dict]:
    if path.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise EvalFailure("parquet predictions need pyarrow; write CSV instead") from exc
        return pq.read_table(path).to_pylist()
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _price(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise EvalFailure(f"prediction is not a number: {v!r}") from None
    if math.isnan(x) or math.isinf(x):
        raise EvalFailure(f"prediction is NaN or Inf: {v!r}")
    if x <= 0:
        raise EvalFailure(f"prediction is <= 0: {v!r}")
    return x


def align(truth: list[dict], preds: list[dict]) -> list[tuple[float, float]]:
    """(y, ŷ) pairs by sale_id. Raises on any count, id or value problem."""
    if len(preds) != len(truth):
        raise EvalFailure(f"prediction count {len(preds)} != test rows {len(truth)}")
    by_id: dict[str, float] = {}
    for r in preds:
        sid = str(r.get("sale_id"))
        if sid in by_id:
            raise EvalFailure(f"duplicate sale_id in predictions: {sid}")
        by_id[sid] = _price(r.get("predicted_price"))
    pairs = []
    for t in truth:
        sid = str(t["sale_id"])
        if sid not in by_id:
            raise EvalFailure(f"missing prediction for sale_id {sid}")
        pairs.append((float(t["sale_price"]), by_id[sid]))
    return pairs


def _quantile(xs: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def metrics(pairs: list[tuple[float, float]]) -> dict:
    if not pairs:
        raise EvalFailure("empty test set")
    ape = [abs(yhat - y) / y for y, yhat in pairs]
    n = len(ape)

    def within(t):
        return round(sum(a <= t for a in ape) / n, 6)

    return {
        "n": n,
        "MdAPE": round(_quantile(ape, 0.5), 6),
        "Mean_APE": round(sum(ape) / n, 6),
        "PE10": within(0.10), "PE20": within(0.20), "PE30": within(0.30),
        "P75_APE": round(_quantile(ape, 0.75), 6),
        "P90_APE": round(_quantile(ape, 0.90), 6),
        "P95_APE": round(_quantile(ape, 0.95), 6),
        "P99_APE": round(_quantile(ape, 0.99), 6),
        "Max_APE": round(max(ape), 6),
        "Catastrophic_Rate": round(sum(a > CATASTROPHIC_APE for a in ape) / n, 6),
    }


def evaluate(bench_dir: str, predictions: str, baseline: str | None) -> dict:
    """{passed, failures, metrics, benchmark} — never raises EvalFailure."""
    out: dict = {"passed": False, "failures": [], "metrics": None}
    try:
        manifest = verify_manifest(bench_dir)
        out["benchmark"] = {k: manifest[k] for k in ("version", "test_start", "test_end")}
        pairs = align(read_rows(os.path.join(bench_dir, Y_FILE)), read_rows(predictions))
        m = out["metrics"] = metrics(pairs)
    except (EvalFailure, OSError, KeyError, ValueError) as exc:
        out["failures"].append(str(exc))
        return out
    if m["Catastrophic_Rate"] > MAX_CATASTROPHIC_RATE:
        out["failures"].append(
            f"Catastrophic_Rate {m['Catastrophic_Rate']} > {MAX_CATASTROPHIC_RATE}")
    if baseline and os.path.exists(baseline):
        with open(baseline) as f:
            base = json.load(f)
        base_m = (base.get("metrics") or base).get("MdAPE")
        out["baseline_MdAPE"] = base_m
        if base_m is not None and m["MdAPE"] > base_m:
            out["failures"].append(f"regression: MdAPE {m['MdAPE']} > baseline {base_m}")
    out["passed"] = not out["failures"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--output-metrics", required=True)
    ap.add_argument("--bench-dir", default=DEFAULT_BENCH)
    ap.add_argument("--baseline", default=None,
                    help=f"defaults to <bench-dir>/{BASELINE} if present")
    a = ap.parse_args(argv)
    baseline = a.baseline or os.path.join(a.bench_dir, BASELINE)
    result = evaluate(a.bench_dir, a.predictions, baseline)
    payload = json.dumps(result, indent=2)
    with open(a.output_metrics, "w") as f:
        f.write(payload + "\n")
    print(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
