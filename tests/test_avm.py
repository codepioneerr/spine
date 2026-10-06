"""
core.avm tests — comps-based valuation v0.

What is defended:
  1. $0 / nominal transfers, mortgages, multi-lot, partial and condo-unit
     deeds are never priced (each would poison a median)
  2. a sale is never its own comp (leave-one-out)
  3. the most specific group with enough comps wins; fallbacks work
  4. the backtest grades what it says it grades
  5. nothing is surfaceable until its level backtests well
  6. both loaders read real schemas
"""

import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import avm, proptech                              # noqa: E402


def sale(doc, amount=5_000_000, bbl="3012340010", zipcode="11201",
         bldgclass="C1", unitsres=10, bldgarea=10_000, **kw):
    return {"document_id": doc, "doc_type": "DEED", "amount": amount,
            "bbl": bbl, "zipcode": zipcode, "bldgclass": bldgclass,
            "unitsres": unitsres, "bldgarea": bldgarea, "n_parcels": 1,
            "percent_trans": 100, **kw}


def market(n=6, ppsf=500, **kw):
    """n identical comps at `ppsf` $/sqft on 10,000 sqft."""
    return [sale(f"C{i}", amount=ppsf * 10_000, bbl=f"30123400{i + 20:02d}", **kw)
            for i in range(n)]


class Priceable(unittest.TestCase):

    def test_a_clean_deed_is_priced(self):
        p = avm.priceable(sale("D1"))
        self.assertEqual(p["ppsf"], 500)
        self.assertEqual(p["ppu"], 500_000)
        self.assertEqual(p["keys"]["zip+class"], ("11201", "C"))

    def test_zero_and_nominal_transfers_are_not(self):
        for amt in (0, None, 10, 9_999, "n/a"):
            self.assertIsNone(avm.priceable(sale("D", amount=amt)), amt)

    def test_mortgages_multi_lot_and_partials_are_not(self):
        self.assertIsNone(avm.priceable(sale("D", doc_type="MTGE")))
        self.assertIsNone(avm.priceable(sale("D", n_parcels=3)))
        self.assertIsNone(avm.priceable(sale("D", percent_trans=50)))

    def test_condo_unit_lots_are_not(self):
        """Building facts divided into one unit's price is off by the building."""
        self.assertIsNone(avm.priceable(sale("D", bbl="3023101001")))

    def test_no_building_facts_is_not(self):
        self.assertIsNone(avm.priceable(sale("D", unitsres=None, bldgarea=0)))

    def test_bad_bbl_is_not(self):
        for b in ("", None, "123", "abcdefghij"):
            self.assertIsNone(avm.priceable(sale("D", bbl=b)), b)

    def test_unrecorded_percent_is_whole(self):
        self.assertIsNotNone(avm.priceable(sale("D", percent_trans=None)))


class Estimate(unittest.TestCase):

    def test_median_of_comps_times_subject_size(self):
        c = avm.Comps(market(6, ppsf=500))
        e = c.estimate(sale("S", amount=6_000_000, bbl="3012340099"))
        self.assertEqual(e["value"], 5_000_000)
        self.assertEqual(e["ratio"], 1.2)
        self.assertEqual((e["metric"], e["level"], e["n"]), ("ppsf", "zip+class", 6))

    def test_a_sale_is_never_its_own_comp(self):
        comps = market(5) + [sale("S", amount=50_000_000, bbl="3012340099")]
        c = avm.Comps(comps)
        e = c.estimate(comps[-1])
        self.assertEqual(e["n"], 5)
        self.assertEqual(e["value"], 5_000_000)

    def test_too_few_comps_falls_back_a_level(self):
        comps = market(3) + market(3, bldgclass="D4")   # zip has 6, zip+C has 3
        e = avm.Comps(comps).estimate(sale("S", bbl="3012340099"))
        self.assertEqual(e["level"], "zip")

    def test_borough_class_fallback_when_zip_is_thin(self):
        comps = market(6, zipcode="11215")
        e = avm.Comps(comps).estimate(sale("S", bbl="3012340099"))
        self.assertEqual(e["level"], "boro+class")

    def test_no_area_uses_per_unit(self):
        e = avm.Comps(market(6)).estimate(sale("S", bldgarea=None, unitsres=20,
                                                bbl="3012340099"))
        self.assertEqual((e["metric"], e["value"]), ("ppu", 10_000_000))

    def test_nothing_to_compare_is_none(self):
        self.assertIsNone(avm.Comps(market(2)).estimate(sale("S")))
        self.assertIsNone(avm.Comps(market(6)).estimate(sale("S", amount=0)))


class Backtest(unittest.TestCase):

    def test_a_uniform_market_has_zero_error(self):
        r = avm.backtest(market(8))
        self.assertEqual((r["priced"], r["n"], r["mdape_pct"], r["within_20_pct"]),
                         (8, 8, 0.0, 100.0))
        self.assertEqual(r["coverage_pct"], 100.0)

    def test_errors_are_absolute_and_median(self):
        s = market(6) + [sale("HI", amount=10_000_000, bbl="3012340098")]
        r = avm.backtest(s)
        self.assertEqual(r["n"], 7)
        self.assertEqual(r["mdape_pct"], 0.0)        # six of seven exact
        self.assertLess(r["within_20_pct"], 100)

    def test_only_docs_grades_a_subset_but_keeps_all_comps(self):
        s = market(6) + [sale("R", amount=6_000_000, bbl="3012340098")]
        r = avm.backtest(s, only_docs={"R"})
        self.assertEqual((r["graded"], r["n"], r["mdape_pct"]), (1, 1, 16.7))

    def test_empty_is_safe(self):
        r = avm.backtest([])
        self.assertEqual((r["priced"], r["mdape_pct"], r["coverage_pct"]), (0, None, 0.0))


class Surfaceable(unittest.TestCase):

    def test_gate(self):
        good = {"by_level": {"zip+class": {"mdape_pct": 12.0}}}
        bad = {"by_level": {"zip+class": {"mdape_pct": 51.0}}}
        e = {"level": "zip+class"}
        self.assertTrue(avm.surfaceable(e, good))
        self.assertFalse(avm.surfaceable(e, bad))
        self.assertFalse(avm.surfaceable({"level": "zip"}, good))
        self.assertFalse(avm.surfaceable(None, good))


class Loaders(unittest.TestCase):

    def test_from_spine(self):
        conn = proptech.connect(":memory:")
        proptech.upsert_docs(conn, [
            {"document_id": "D1", "doc_type": "DEED", "document_date": "2026-08-01",
             "recorded_datetime": "2026-08-05", "document_amt": 5_000_000,
             "percent_trans": 100, "bbl": "3012340010"},
            {"document_id": "Z0", "doc_type": "DEED", "document_date": "2026-08-01",
             "recorded_datetime": "2026-08-05", "document_amt": 0,
             "percent_trans": 100, "bbl": "3012340011"}])
        proptech.upsert_parcels(conn, [{"bbl": "3012340010", "zipcode": "11201",
                                        "bldgclass": "C1", "unitsres": 10,
                                        "bldgarea": 10_000}], "pluto")
        rows = avm.sales_from_spine(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(avm.priceable(rows[0])["ppsf"], 500)

    def test_from_dwj_counts_parcels(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
          CREATE TABLE acris_master (document_id TEXT PRIMARY KEY, doc_type TEXT,
            document_date TEXT, recorded_datetime TEXT, document_amt REAL,
            percent_trans REAL, fetched_ts TEXT);
          CREATE TABLE acris_legals (document_id TEXT, borough TEXT, block INTEGER,
            lot INTEGER, bbl TEXT, street_number TEXT, street_name TEXT, unit TEXT,
            property_type TEXT);
          CREATE TABLE pluto (bbl TEXT PRIMARY KEY, zipcode TEXT, bldgclass TEXT,
            unitsres INTEGER, bldgarea REAL);
          INSERT INTO acris_master VALUES ('ONE','DEED','2026-08-01','x',5e6,100,'t'),
                                          ('TWO','DEED','2026-08-01','x',9e6,100,'t');
          INSERT INTO acris_legals (document_id, bbl) VALUES ('ONE','3012340010'),
                                   ('TWO','3012340011'), ('TWO','3012340012');
          INSERT INTO pluto VALUES ('3012340010','11201','C1',10,10000);
        """)
        rows = {r["document_id"]: r for r in avm.sales_from_dwj(conn)}
        self.assertEqual(rows["TWO"]["n_parcels"], 2)
        self.assertIsNone(avm.priceable(rows["TWO"]))
        self.assertEqual(avm.priceable(rows["ONE"])["ppsf"], 500)


if __name__ == "__main__":
    unittest.main()
