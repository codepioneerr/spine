"""
core.items — terminal access to the item store.

    python3 -m core.items                    the unacted queue
    python3 -m core.items --all              everything
    python3 -m core.items --source acris     one collector
    python3 -m core.items --kind deal --min 80
    python3 -m core.items --show 42          one item in full
    python3 -m core.items --act 42 43        mark acted
    python3 -m core.items --dismiss 44
    python3 -m core.items --prune            apply retention
    python3 -m core.items --prune --dry-run  what retention would drop

The read commands are safe. The write ones (--act/--dismiss/--prune) change
state, so they say what they did rather than exiting silently.
"""

from __future__ import annotations

import argparse
import json
import sys

from core.store import Store


def _fmt(row) -> str:
    stamp = row["ts"][5:16].replace("T", " ")
    flag = {"new": "*", "seen": ".", "acted": "+", "dismissed": "-"}.get(
        row["status"], "?")
    return (f"{flag} {row['id']:>5}  {stamp}  {row['importance']:>3}  "
            f"{row['source']:<12} {row['kind']:<7} "
            f"{(row['title'] or row['key'])[:46]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.items")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--source")
    ap.add_argument("--kind")
    ap.add_argument("--min", type=int, dest="min_importance")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--show", type=int, metavar="ID")
    ap.add_argument("--act", type=int, nargs="+", metavar="ID")
    ap.add_argument("--seen", type=int, nargs="+", metavar="ID")
    ap.add_argument("--dismiss", type=int, nargs="+", metavar="ID")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    s = Store()
    try:
        if args.prune:
            removed = s.prune(dry_run=args.dry_run)
            verb = "would drop" if args.dry_run else "dropped"
            if not removed:
                print("retention: nothing to drop")
            for kind, n in removed.items():
                print(f"retention: {verb} {n} {kind}(s)")
            print("acted items are never dropped — they are the record of "
                  "what you actually did")
            return 0

        for ids, status in ((args.act, "acted"), (args.seen, "seen"),
                            (args.dismiss, "dismissed")):
            if ids:
                print(f"marked {s.mark(ids, status)} item(s) {status}")
                return 0

        if args.show:
            rows = s.conn.execute("SELECT * FROM items WHERE id=?",
                                  (args.show,)).fetchall()
            if not rows:
                print(f"no item {args.show}")
                return 1
            r = rows[0]
            for col in r.keys():
                val = r[col]
                if col == "data_json":
                    val = json.dumps(json.loads(val), indent=2)
                print(f"{col:>11}: {val}")
            return 0

        rows = (s.query(source=args.source, kind=args.kind,
                        min_importance=args.min_importance, limit=args.limit)
                if args.all else
                s.unacted(limit=args.limit,
                          min_importance=args.min_importance))
        if not rows:
            print("nothing to show" if args.all else
                  "queue is empty — nothing unacted")
            return 0
        print("  " + f"{'ID':>5}  {'WHEN':<11} {'IMP':>3}  {'SOURCE':<12} "
                     f"{'KIND':<7} TITLE")
        for r in rows:
            print(_fmt(r))
        print(f"\n{len(rows)} item(s).  "
              f"* new  . seen  + acted  - dismissed")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
