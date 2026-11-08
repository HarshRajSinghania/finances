"""
Items behind a charge: Amazon order history and plain receipt CSVs.
"""

from __future__ import annotations

import os
import unittest

import support

from finances import api, lineitems
from finances.database import FinanceDB

AMAZON = os.path.join(os.path.dirname(__file__), "fixtures", "items",
                      "Retail.OrderHistory.1.csv")


class LineItemTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp_path("i.db")
        with FinanceDB(self.path) as db:
            db.upsert_transactions([
                support.txn("2026-10-04", 46.53, "AMZN Mktp US*2K4", category="Shopping"),
                support.txn("2026-10-12", 19.49, "AMAZON.COM*AB12", category="Shopping"),
                support.txn("2026-10-12", 19.49, "TARGET 0042", category="Shopping")])

    def test_reads_shipments_and_skips_cancelled(self):
        groups = lineitems.read(AMAZON)
        self.assertEqual([(g["total"], len(g["items"])) for g in groups],
                         [(46.53, 2), (19.49, 1)])

    def test_each_shipment_lands_under_its_charge(self):
        r = lineitems.import_file(self.path, AMAZON)
        self.assertEqual((r["kind"], r["groups"], r["matched"], r["items"]),
                         ("amazon", 2, 2, 3))
        rows = api.Api(self.path).transactions({})["transactions"]
        by = {t["description"]: [i["name"] for i in t.get("items", [])] for t in rows}
        self.assertEqual(by["AMZN Mktp US*2K4"], ["Phone case, clear", "Printer ink, black XL"])
        self.assertEqual(by["AMAZON.COM*AB12"], ["A paperback novel"])
        self.assertEqual(by["TARGET 0042"], [])     # same amount, wrong shop

    def test_importing_again_replaces(self):
        lineitems.import_file(self.path, AMAZON)
        lineitems.import_file(self.path, AMAZON)
        with FinanceDB(self.path) as db:
            n = db.conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
        self.assertEqual(n, 3)

    def test_receipts_csv(self):
        path = self.tmp_path("costco.csv")
        with open(path, "w") as fh:
            fh.write("date,store,item,amount\n2026-10-04,Costco,Chicken,4.99\n"
                     "2026-10-04,Costco,Paper towels,21.99\n")
        with FinanceDB(self.path) as db:
            db.upsert_transactions([support.txn("2026-10-05", 26.98, "COSTCO WHSE #1")])
        r = lineitems.import_file(self.path, path)
        self.assertEqual((r["kind"], r["matched"], r["items"]), ("receipts", 1, 2))


if __name__ == "__main__":
    unittest.main()
