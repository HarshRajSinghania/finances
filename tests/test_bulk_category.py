"""
Moving many rows to a category at once, and teaching the merchant.
"""

from __future__ import annotations

import json
import os
import unittest

import support

from finances import api
from finances.database import FinanceDB


class SetCategoryTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp_path("b.db")
        with FinanceDB(self.path) as db:
            db.upsert_transactions([
                support.txn("2026-03-01", 12.0, "TST* CORNER DELI 0042", category="Shopping"),
                support.txn("2026-03-08", 9.5, "TST* CORNER DELI 0042", category="Shopping"),
                support.txn("2026-03-09", 30.0, "SHELL OIL 123", category="Gas")])
        self.a = api.Api(self.path)

    def row(self, date):
        with FinanceDB(self.path) as db:
            t = next(t for t in db.transactions() if t.date == date)
        return {"date": t.date, "description": t.description, "amount": t.amount,
                "accountLast4": t.account_last4, "seq": t.seq}

    def categories(self):
        with FinanceDB(self.path) as db:
            return {t.date: t.category for t in db.transactions()}

    def test_moves_only_the_chosen_rows(self):
        r = self.a.set_category({"rows": [self.row("2026-03-01")], "category": "Dining"})
        self.assertEqual((r["updated"], r["alsoMoved"], r["remembered"]), (1, 0, []))
        self.assertEqual(self.categories(), {"2026-03-01": "Dining", "2026-03-08": "Shopping",
                                             "2026-03-09": "Gas"})
        self.assertFalse(os.path.exists(self.tmp_path("learned_rules.json")))

    def test_remembering_moves_the_merchant_and_teaches_it(self):
        r = self.a.set_category({"rows": [self.row("2026-03-01")], "category": "Dining",
                                 "remember": True})
        self.assertEqual((r["updated"], r["alsoMoved"]), (1, 1))
        self.assertEqual(self.categories()["2026-03-08"], "Dining")
        self.assertEqual(self.categories()["2026-03-09"], "Gas")
        with open(self.tmp_path("learned_rules.json")) as fh:
            self.assertEqual(list(json.load(fh)["merchants"].values()), ["Dining"])

    def test_refuses_nonsense(self):
        for body in ({"rows": [], "category": "Dining"},
                     {"rows": [self.row("2026-03-01")], "category": " "}):
            with self.assertRaises(ValueError):
                self.a.set_category(body)


if __name__ == "__main__":
    unittest.main()
