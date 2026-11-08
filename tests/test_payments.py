"""
Card payment due dates: read from the statement, then checked against payments.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta
from unittest import mock

import support

from finances import api, parse
from finances.database import FinanceDB
from finances.models import Statement


class DueDateTest(unittest.TestCase):
    def test_shapes_statements_print(self):
        closed = date(2025, 11, 27)
        self.assertEqual(parse.payment_due("Payment Due Date 12/22/25", closed), "2025-12-22")
        self.assertEqual(parse.payment_due("Payment Due Date: December 22, 2025", closed),
                         "2025-12-22")
        self.assertEqual(parse.payment_due("payment due date dec 22", closed), "2025-12-22")

    def test_a_january_due_date_on_a_december_statement(self):
        self.assertEqual(parse.payment_due("Payment Due Date: 01/22", date(2025, 12, 28)),
                         "2026-01-22")

    def test_none_printed(self):
        self.assertEqual(parse.payment_due("Minimum Payment Due $35.00", date(2025, 1, 1)), "")


class PaymentsApiTest(support.TempDirMixin, unittest.TestCase):
    TODAY = date(2026, 11, 8)

    def store(self, paid=None, due="2026-11-25"):
        path = self.tmp_path(f"p{paid}{due}.db")
        with FinanceDB(path) as db:
            db.upsert_statements([Statement(
                source_file="amex-2026-10.pdf", issuer="Amex", account_last4="1005",
                period_start="2026-10-01", period_end="2026-10-31", new_balance=1389.32,
                minimum_payment=35.0, payment_due=due)])
            if paid:
                db.upsert_transactions([support.txn("2026-11-05", -paid, "AUTOPAY", issuer="Amex",
                                                    account_last4="1005", category="Payment")])
        return path

    def state(self, path):
        with mock.patch.object(api, "date", wraps=date) as d:
            d.today.return_value = self.TODAY
            [row] = api.Api(path).payments({})["payments"]
        return row

    def test_states(self):
        self.assertEqual(self.state(self.store())["state"], "due")
        self.assertEqual(self.state(self.store())["daysLeft"], 17)
        self.assertEqual(self.state(self.store(paid=1389.32))["state"], "paid")
        self.assertEqual(self.state(self.store(paid=50.0))["state"], "minimum paid")

    def test_a_passed_date_is_only_unpaid_when_the_files_reach_it(self):
        # The files end Oct 31: a payment made after that is simply not visible.
        self.assertEqual(self.state(self.store(due="2026-11-01"))["state"], "unknown")


if __name__ == "__main__":
    unittest.main()


class ReadDueDatesTest(support.TempDirMixin, unittest.TestCase):
    """Statements stored before due dates were read get one, and nothing else moves."""

    def test_fills_only_the_due_date(self):
        pdfs = self.tmp_path("PDFs")
        os.makedirs(pdfs)
        for name in ("card.pdf", "checking.pdf"):
            open(os.path.join(pdfs, name), "wb").write(b"%PDF-1.4")
        path = self.tmp_path("old.db")
        with FinanceDB(path) as db:
            db.upsert_statements([
                Statement(source_file="card.pdf", issuer="Amex", account_last4="1005",
                          period_end="2026-10-31", new_balance=500.0, minimum_payment=35.0,
                          reconciled=1, source_format="pdf"),
                Statement(source_file="checking.pdf", issuer="Chase", account_last4="2002",
                          period_end="2026-10-31", source_format="pdf", account_type="bank"),
                Statement(source_file="gone.pdf", issuer="Citi", account_last4="3003",
                          period_end="2026-10-31", minimum_payment=25.0, source_format="pdf"),
            ])
        open(os.path.join(pdfs, "old-checking.pdf"), "wb").write(b"%PDF-1.4")
        with FinanceDB(path) as db:
            # A store from before card statements were marked: no type, no minimum.
            db.upsert_statements([Statement(source_file="old-checking.pdf", issuer="BofA",
                                            account_last4="4004", period_end="2026-10-31",
                                            source_format="pdf")])
        due = {"card.pdf": "2026-11-25", "old-checking.pdf": ""}
        with mock.patch.object(api, "PDF_DIR", pdfs), \
                mock.patch.object(parse, "read_payment_due",
                                  side_effect=lambda p, _: due[os.path.basename(p)]) as read:
            a = api.Api(path)
            self.assertEqual(a.payments({})["unread"], 2)
            r = a.read_due_dates({})
            # The one that printed none is not offered again.
            self.assertEqual(a.payments({})["unread"], 0)
        self.assertEqual(read.call_count, 2)
        self.assertEqual((r["checked"], r["found"], r["unread"]), (2, 1, 0))
        self.assertEqual([p["due"] for p in r["payments"]], ["2026-11-25"])
        with FinanceDB(path) as db:
            card = next(s for s in db.statement_models() if s.source_file == "card.pdf")
        self.assertEqual((card.payment_due, card.reconciled, card.new_balance),
                         ("2026-11-25", 1, 500.0))
