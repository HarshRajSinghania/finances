"""
Tests for importer.py — bank exports into the store.

Each fixture in tests/fixtures/exports is a small, made-up file in the shape a
real bank produces. What matters is not that rows come out, but that they come
out with the right sign and that the balance checks catch what they should:
a running-balance CSV that skips a row, a CAMT file whose entries do not reach
its closing balance, an OFX export that does not continue the one before it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import support

from finances import importer
from finances.database import FinanceDB

FIXTURES = os.path.join(support.TESTS_DIR, "fixtures", "exports")


def fixture(name):
    return os.path.join(FIXTURES, name)


def amounts(acct):
    return [t.amount for t in acct.rows]


class NumberAndDateTest(unittest.TestCase):
    def test_decimal_comma_is_detected_from_the_column(self):
        self.assertTrue(importer.decimal_comma(["-1.234,56", "5.000,00", "12,50"]))
        self.assertFalse(importer.decimal_comma(["-1,234.56", "5,000.00", "12.50"]))
        self.assertFalse(importer.decimal_comma(["1,234", "12.50"]))

    def test_amounts_in_every_shape(self):
        cases = [("-1.234,56", True, -1234.56), ("1,234.56", False, 1234.56),
                 ("(45.00)", False, -45.0), ("45.00-", False, -45.0),
                 ("+12,5", True, 12.5), ("€ 1.000,00", True, 1000.0),
                 ("$-3.10", False, -3.10), ("", False, None), ("n/a", False, None)]
        for raw, comma, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(importer.parse_amount(raw, comma), want)

    def test_day_first_wins_when_a_day_is_over_twelve(self):
        self.assertEqual(importer.date_format(["05/06/2026", "25/06/2026"]), "%d/%m/%Y")

    def test_ambiguous_dates_follow_the_number_format(self):
        self.assertEqual(importer.date_format(["05/06/2026"]), "%m/%d/%Y")
        self.assertEqual(importer.date_format(["05/06/2026"], prefer_dmy=True), "%d/%m/%Y")

    def test_header_aliases_in_several_languages(self):
        cols, cur = importer.map_columns(
            ["Buchungstag", "Valutadatum", "Verwendungszweck", "Betrag (EUR)", "Saldo"])
        self.assertEqual(cols["date"], 0)
        self.assertEqual(cols["post_date"], 1)
        self.assertEqual(cols["amount"], 3)
        self.assertEqual(cols["balance"], 4)
        self.assertEqual(cur, "EUR")


class CsvTest(unittest.TestCase):
    def test_running_balance_settles_the_sign_and_checks_every_row(self):
        [a] = importer.read_csv(fixture("chase_checking_4321.csv"))
        self.assertEqual(a.account, "4321")
        self.assertEqual(a.issuer, "Chase")
        # Chronological, money out positive.
        self.assertEqual(amounts(a), [100.0, -2400.0, 84.12])
        self.assertTrue(a.checked)
        self.assertEqual(a.opening, 700.0)
        self.assertEqual(a.closing, 2915.88)

    def test_card_export_with_purchases_positive(self):
        [a] = importer.read_csv(fixture("amex_activity.csv"))
        self.assertEqual(a.kind, "card")
        self.assertEqual(amounts(a), [17.99, 23.4, 6.5, -500.0])
        self.assertIsNone(a.checked)
        self.assertIn("no running balance", a.note)

    def test_sign_can_be_forced(self):
        [a] = importer.read_csv(fixture("amex_activity.csv"), sign="out-negative")
        self.assertEqual(amounts(a), [-17.99, -23.4, -6.5, 500.0])

    def test_summary_block_above_the_header_is_skipped(self):
        [a] = importer.read_csv(fixture("bofa_preamble.csv"))
        self.assertEqual(amounts(a), [-1500.0, 1200.0, 65.5])
        self.assertTrue(a.checked)

    def test_german_export(self):
        [a] = importer.read_csv(fixture("dkb_giro.csv"))
        self.assertEqual(a.currency, "EUR")
        self.assertEqual(amounts(a), [-5000.0, 1234.56])
        self.assertEqual(a.rows[0].date, "2026-01-02")
        self.assertTrue(a.checked)
        self.assertEqual(a.rows[0].category, "Income")
        self.assertEqual(a.rows[1].category, "Groceries")

    def test_a_missing_row_breaks_the_balance_chain(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "gap.csv")
        with open(path, "w") as fh:
            fh.write("Date,Description,Amount,Balance\n"
                     "2026-01-01,A,-10.00,90.00\n"
                     "2026-01-02,B,-10.00,80.00\n"
                     "2026-01-04,D,-10.00,60.00\n"     # C (-10) is missing
                     "2026-01-05,E,-10.00,50.00\n")
        [a] = importer.read_csv(path)
        self.assertFalse(a.checked)
        self.assertIn("1 of 3 rows", a.note)

    def test_not_an_export(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "notes.csv")
        with open(path, "w") as fh:
            fh.write("name,colour\nfoo,blue\n")
        with self.assertRaises(ValueError):
            importer.read_csv(path)


class StandardFormatsTest(unittest.TestCase):
    def test_ofx(self):
        [a] = importer.read_ofx(fixture("checking.ofx"))
        self.assertEqual((a.account, a.kind, a.currency), ("9876", "bank", "USD"))
        self.assertEqual(a.issuer, "First Example Bank")
        self.assertEqual(amounts(a), [42.1, -1500.0, 57.9])
        self.assertEqual(a.rows[2].description, "SAFEWAY & CO")
        self.assertEqual((a.opening, a.closing), (1000.0, 2400.0))
        self.assertIsNone(a.checked)

    def test_camt053_books_close(self):
        [a] = importer.read_camt(fixture("camt053.xml"))
        self.assertEqual((a.account, a.currency), ("4300", "EUR"))
        # The pending entry is not booked, so it is not a transaction yet.
        self.assertEqual(amounts(a), [324.5, -1200.0])
        self.assertTrue(a.checked)
        self.assertIn("Albert Heijn", a.rows[0].description)

    def test_camt053_missing_entry_is_caught(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "short.xml")
        with open(fixture("camt053.xml")) as fh:
            text = fh.read().replace("1875.50", "1900.00")
        with open(path, "w") as fh:
            fh.write(text)
        [a] = importer.read_camt(path)
        self.assertFalse(a.checked)
        self.assertIn("closes at 1,900.00", a.note)

    def test_mt940(self):
        [a] = importer.read_mt940(fixture("statement.sta"))
        self.assertEqual((a.account, a.currency), ("7890", "EUR"))
        self.assertEqual(amounts(a), [45.9, -2500.0])
        self.assertTrue(a.checked)
        self.assertIn("Telco GmbH", a.rows[0].description)

    def test_formats_are_detected_by_content(self):
        for name, fmt in (("checking.ofx", "ofx"), ("camt053.xml", "camt053"),
                          ("statement.sta", "mt940"), ("amex_activity.csv", "csv")):
            with self.subTest(name=name):
                self.assertEqual(importer.detect_format(fixture(name)), fmt)


class StoreTest(support.TempDirMixin, unittest.TestCase):
    def test_ofx_exports_chain_across_files_in_any_order(self):
        path = self.tmp_path("s.db")
        with FinanceDB(path) as db:
            importer.import_file(db, fixture("checking_feb.ofx"))
            importer.import_file(db, fixture("checking.ofx"))
            self.assertEqual(importer.chain(db, "9876"), ["checking_feb.ofx: continues checking.ofx"])
            st = db.statement_for("checking_feb.ofx")
            self.assertEqual(st["reconciled"], 1)

    def test_a_broken_chain_is_a_mismatch(self):
        tmp = self.tmp_path("feb_bad.ofx")
        with open(fixture("checking_feb.ofx")) as fh:
            text = fh.read().replace("<BALAMT>2300.00", "<BALAMT>2250.00")
        with open(tmp, "w") as fh:
            fh.write(text)
        with FinanceDB(self.tmp_path("s.db")) as db:
            importer.import_file(db, fixture("checking.ofx"))
            importer.import_file(db, tmp)
            importer.chain(db, "9876")
            st = db.statement_for("feb_bad.ofx")
            self.assertEqual(st["reconciled"], 0)
            self.assertIn("50.00 unaccounted", st["reconcile_note"])

    def test_overlapping_exports_do_not_double_count(self):
        a = self.tmp_path("a_4321.csv")
        b = self.tmp_path("b_4321.csv")
        with open(a, "w") as fh:
            fh.write("Date,Description,Amount\n2026-01-01,COFFEE,-3.00\n"
                     "2026-01-01,COFFEE,-3.00\n2026-01-02,LUNCH,-12.00\n")
        with open(b, "w") as fh:
            fh.write("Date,Description,Amount\n2026-01-01,COFFEE,-3.00\n"
                     "2026-01-02,LUNCH,-12.00\n2026-01-03,DINNER,-30.00\n")
        with FinanceDB(self.tmp_path("s.db")) as db:
            importer.import_file(db, a)
            [(acct, _, skipped)] = importer.import_file(db, b)
            self.assertEqual(skipped, 2)
            self.assertEqual(db.count(), 4)

    def test_reimporting_the_same_file_is_cached_then_replaced(self):
        with FinanceDB(self.tmp_path("s.db")) as db:
            importer.import_file(db, fixture("amex_activity.csv"))
            self.assertEqual(importer.import_file(db, fixture("amex_activity.csv")), [])
            importer.import_file(db, fixture("amex_activity.csv"), force=True)
            self.assertEqual(db.count(), 4)


if __name__ == "__main__":
    unittest.main()
