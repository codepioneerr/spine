"""
eval/ tests — the frozen AVM benchmark and its scorer.

What is defended:
  1. the scorer fails closed: NaN/Inf/<=0/non-numeric, count or id
     mismatches, duplicates, catastrophic rate, regression, tampering
  2. metrics are the documented ones, on known numbers
  3. hygiene: each exclusion fires, and a clean sale is CLEAN_MARKET
  4. related-party heuristics catch the obvious and spare strangers
  5. the split is chronological with the 45-day lag, and PIT is strict
"""

import csv
import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import build_benchmark as bb                                  # noqa: E402
import score_avm as sa                                        # noqa: E402


def write_csv(path, cols, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


class Bench:
    """A tiny benchmark dir with a valid manifest."""

    def __init__(self, prices=(100.0, 200.0, 400.0, 800.0)):
        self.dir = tempfile.mkdtemp()
        self.truth = [{"sale_id": f"s{i}", "bbl": "1000010001", "sale_price": p}
                      for i, p in enumerate(prices)]
        write_csv(self.path(sa.Y_FILE), ("sale_id", "bbl", "sale_price"), self.truth)
        write_csv(self.path(sa.X_FILE), ("sale_id",), [{"sale_id": t["sale_id"]} for t in self.truth])
        with open(self.path(sa.MANIFEST), "w") as f:
            json.dump({"version": 1, "test_start": "2026-01-01", "test_end": "2026-03-31",
                       "sha256": {n: sa.sha256(self.path(n)) for n in (sa.X_FILE, sa.Y_FILE)}}, f)

    def path(self, name):
        return os.path.join(self.dir, name)

    def score(self, preds, baseline=None):
        p = self.path("preds.csv")
        write_csv(p, ("sale_id", "predicted_price"),
                  [{"sale_id": s, "predicted_price": v} for s, v in preds])
        return sa.evaluate(self.dir, p, baseline)

    def exact(self, factor=1.0):
        return [(t["sale_id"], t["sale_price"] * factor) for t in self.truth]


class Scorer(unittest.TestCase):

    def test_perfect_passes(self):
        r = Bench().score(Bench().exact())
        self.assertTrue(r["passed"], r)
        self.assertEqual(r["metrics"]["MdAPE"], 0)
        self.assertEqual(r["metrics"]["PE10"], 1)

    def test_bad_values_fail(self):
        for bad in ("nan", "inf", "-inf", "0", "-5", "abc", ""):
            b = Bench()
            preds = b.exact()
            preds[1] = (preds[1][0], bad)
            r = b.score(preds)
            self.assertFalse(r["passed"], bad)
            self.assertIsNone(r["metrics"])

    def test_count_and_ids(self):
        b = Bench()
        self.assertFalse(b.score(b.exact()[:-1])["passed"])
        self.assertFalse(b.score(b.exact() + [("extra", 5)])["passed"])
        p = b.exact()
        p[0] = ("nope", 100)
        self.assertFalse(b.score(p)["passed"])
        p = b.exact()
        p[0] = p[1]
        self.assertIn("duplicate", b.score(p)["failures"][0])

    def test_catastrophic_rate(self):
        b = Bench()
        p = b.exact()
        p[0] = (p[0][0], 100 * 2.5)          # APE 1.5 on 1 of 4 = 25%
        r = b.score(p)
        self.assertFalse(r["passed"])
        self.assertEqual(r["metrics"]["Catastrophic_Rate"], 0.25)

    def test_regression_against_baseline(self):
        b = Bench()
        base = b.path("base.json")
        with open(base, "w") as f:
            json.dump({"metrics": {"MdAPE": 0.05}}, f)
        self.assertFalse(b.score(b.exact(1.10), base)["passed"])
        self.assertTrue(b.score(b.exact(1.02), base)["passed"])

    def test_tampered_answer_key_refused(self):
        b = Bench()
        with open(b.path(sa.Y_FILE), "a") as f:
            f.write("s9,1000010001,1\n")
        r = b.score(b.exact())
        self.assertFalse(r["passed"])
        self.assertIn("integrity", r["failures"][0])

    def test_cli_exit_codes(self):
        b = Bench()
        p, m = b.path("p.csv"), b.path("m.json")
        write_csv(p, ("sale_id", "predicted_price"),
                  [{"sale_id": s, "predicted_price": v} for s, v in b.exact()])
        self.assertEqual(sa.main(["--predictions", p, "--output-metrics", m,
                                  "--bench-dir", b.dir]), 0)
        with open(m) as f:
            self.assertTrue(json.load(f)["passed"])
        write_csv(p, ("sale_id", "predicted_price"), [{"sale_id": "s0", "predicted_price": 1}])
        self.assertEqual(sa.main(["--predictions", p, "--output-metrics", m,
                                  "--bench-dir", b.dir]), 1)


class Metrics(unittest.TestCase):

    def test_known_values(self):
        # APEs: 0, .05, .15, .25, .5
        pairs = [(100, 100), (100, 105), (100, 85), (100, 125), (100, 150)]
        m = sa.metrics(pairs)
        self.assertAlmostEqual(m["MdAPE"], 0.15)
        self.assertAlmostEqual(m["Mean_APE"], 0.19)
        self.assertEqual((m["PE10"], m["PE20"], m["PE30"]), (0.4, 0.6, 0.8))
        self.assertAlmostEqual(m["P75_APE"], 0.25)
        self.assertAlmostEqual(m["Max_APE"], 0.5)
        self.assertEqual(m["Catastrophic_Rate"], 0)

    def test_quantile_matches_linear(self):
        self.assertAlmostEqual(sa._quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(sa._quantile([1, 2, 3, 4], 0.9), 3.7)


def sale(**kw):
    s = {"bbl": "3012340010", "bldgclass": "A1", "price": 900_000,
         "easement": None, "package": False, "sale_date": "2026-05-01"}
    s.update(kw)
    return s


DEED = {"document_id": "D1", "percent_trans": "100", "recorded_datetime": "2026-05-10T00:00:00"}
LEGAL = [{"bbl": "3012340010", "easement": "N", "partial_lot": "E",
          "air_rights": "N", "subterranean_rights": "N"}]


class Hygiene(unittest.TestCase):

    def lab(self, s=None, deed=DEED, legals=LEGAL, g=("SMITH, JOHN",), e=("JONES, MARY",)):
        return bb.classify(s or sale(), deed, legals, list(g), list(e))

    def test_clean(self):
        self.assertEqual(self.lab(), "CLEAN_MARKET")
        self.assertEqual(self.lab(deed={**DEED, "percent_trans": None}), "CLEAN_MARKET")

    def test_exclusions(self):
        cases = {
            "out_of_scope_class": dict(s=sale(bldgclass="C6")),      # co-op
            "invalid_bbl": dict(s=sale(bbl=None)),
            "condo_without_unit_lot": dict(s=sale(bldgclass="R4", bbl="1011427502")),
            "nominal_price": dict(s=sale(price=10)),
            "multi_parcel": dict(s=sale(package=True)),
            "no_acris_deed": dict(deed=None),
            "partial_interest": dict(deed={**DEED, "percent_trans": "50"}),
        }
        for want, kw in cases.items():
            self.assertEqual(self.lab(**kw), want, want)
        self.assertEqual(self.lab(s=sale(price=149_999)), "nominal_price")
        self.assertEqual(self.lab(s=sale(bldgclass="R4", bbl="1011421219")), "CLEAN_MARKET")
        two = LEGAL + [{**LEGAL[0], "bbl": "3012340011"}]
        self.assertEqual(self.lab(legals=two), "multi_parcel")
        for k, v in (("easement", "Y"), ("partial_lot", "P"), ("air_rights", "Y"),
                     ("subterranean_rights", "Y")):
            self.assertEqual(self.lab(legals=[{**LEGAL[0], k: v}]), "encumbrance", k)

    def test_related_party(self):
        rp = bb.related_party
        self.assertEqual(rp(["SMITH, JOHN"], ["SMITH, JOHN"]), "same_name")
        self.assertEqual(rp(["SMITH, JOHN"], ["SMITH, MARY"]), "shared_surname")
        self.assertEqual(rp(["123 MAIN ST LLC"], ["123 MAIN ST HOLDINGS LLC"]), "affiliate_entity")
        self.assertEqual(rp(["KOWALSKI REALTY LLC"], ["KOWALSKI, ADAM"]), "entity_to_officer")
        self.assertIsNone(rp(["SMITH, JOHN"], ["JONES, MARY"]))
        self.assertIsNone(rp(["ACME HOLDINGS LLC"], ["BETA REALTY LLC"]))
        self.assertIsNone(rp([], []))


class Split(unittest.TestCase):

    def test_window_lag(self):
        start, end = bb.window("2026-08-31T00:00:00")
        self.assertEqual(end, "2026-07-17")
        self.assertEqual(start, "2026-04-19")

    def test_pit_strict(self):
        asof = "2026-04-19T00:00:00"
        self.assertTrue(bb.pit_ok({"sale_date": "2026-04-01", "recorded_datetime": "2026-04-18T00:00:00"}, asof))
        self.assertFalse(bb.pit_ok({"sale_date": "2026-04-01", "recorded_datetime": asof}, asof))
        self.assertFalse(bb.pit_ok({"sale_date": "2026-04-19", "recorded_datetime": "2026-04-01"}, asof))

    def test_packages_flagged(self):
        rows = [sale(bbl="3012340010"), sale(bbl="3012340011"), sale(bbl="3099990001")]
        bb.flag_packages(rows)
        self.assertEqual([r["package"] for r in rows], [True, True, False])

    def test_sale_id_opaque_and_stable(self):
        a = bb.sale_id("2026050100001001", "3012340010")
        self.assertEqual(a, bb.sale_id("2026050100001001", "3012340010"))
        self.assertNotIn("2026", a[:4] if a.startswith("2026") else "")
        self.assertEqual(len(a), 16)

    def test_excel_date(self):
        self.assertEqual(bb._excel_date("46006"), "2025-12-15")
        self.assertTrue(math.isfinite(1))


if __name__ == "__main__":
    unittest.main()
