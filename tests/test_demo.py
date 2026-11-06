"""
Tests for demo.py — the made-up store people try the tools on first.

The demo is the first thing a new user sees, so it has to pass the same checks
a real statement does: every statement reconciles through ``parse.reconcile``,
every row gets a category, and no month is missing. If a categorizer change
starts leaving demo rows Uncategorized, this is where it shows.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from unittest import mock

import support

if support.has_pdfplumber():
    from finances import demo
    from finances.database import FinanceDB
    from finances.engine import Engine


@unittest.skipUnless(support.has_pdfplumber(), "demo.py imports parse.py, which needs pdfplumber")
class DemoTest(support.TempDirMixin, unittest.TestCase):
    TODAY = date(2026, 3, 15)

    def test_every_statement_reconciles(self):
        _, stmts = demo.build(self.TODAY)
        self.assertEqual(len(stmts), 2 * demo.MONTHS)
        bad = [(s.source_file, s.reconcile_note) for s in stmts if s.reconciled != 1]
        self.assertEqual(bad, [])

    def test_every_transaction_is_categorized(self):
        txns, _ = demo.build(self.TODAY)
        left = sorted({t.description for t in txns if t.category == "Uncategorized"})
        self.assertEqual(left, [])

    def test_the_same_seed_gives_the_same_store(self):
        a, _ = demo.build(self.TODAY, seed=3)
        b, _ = demo.build(self.TODAY, seed=3)
        self.assertEqual([t.to_row() for t in a], [t.to_row() for t in b])

    def test_covers_the_twelve_months_before_today(self):
        txns, _ = demo.build(self.TODAY)
        months = sorted({t.month for t in txns})
        self.assertEqual(months[0], "2025-03")
        self.assertEqual(months[-1], "2026-02")
        self.assertEqual(len(months), 12)

    def test_written_store_is_trusted_with_no_gaps(self):
        path = self.tmp_path("demo.db")
        with mock.patch.object(sys, "argv", ["demo.py", "--db", path]), \
                mock.patch("sys.stdout"):
            self.assertEqual(demo.main(), 0)
        with Engine(path) as eng:
            cov = eng.coverage()
        self.assertTrue(cov["trusted"])
        self.assertEqual(cov["gaps"], [])

    def test_refuses_to_overwrite_a_real_store(self):
        path = self.tmp_path("finances.db")
        with FinanceDB(path) as db:
            db.upsert_transactions([support.txn("2026-01-05", 4.5, "SQ *CAFE",
                                                source_file="Amex-Jan.pdf")])
        before = os.path.getsize(path)
        with mock.patch.object(sys, "argv", ["demo.py", "--db", path, "--force"]), \
                mock.patch("sys.stderr"):
            self.assertEqual(demo.main(), 1)
        self.assertEqual(os.path.getsize(path), before)


if __name__ == "__main__":
    unittest.main()
