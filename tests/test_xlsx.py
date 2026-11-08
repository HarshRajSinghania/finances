"""
Tests for xlsx.py and `finances export xlsx`.

The workbook is written by hand with zipfile, so these tests read it back the
same way: the XML parts must be well-formed, every sheet must be listed, and
dates and money must be stored as numbers with the right formats, not text.
"""

from __future__ import annotations

import os
import unittest
import zipfile
import xml.etree.ElementTree as ET
from datetime import date

import support

from finances import export
from finances.database import FinanceDB
from finances.xlsx import Sheet, col_letter, write

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class XlsxTest(support.TempDirMixin, unittest.TestCase):
    def test_column_letters(self):
        self.assertEqual([col_letter(i) for i in (0, 25, 26, 27, 701, 702)],
                         ["A", "Z", "AA", "AB", "ZZ", "AAA"])

    def test_cells_are_typed_and_parts_well_formed(self):
        path = write(self.tmp_path("t.xlsx"), [
            Sheet("Money & <stuff>", ["Date", "Name", "Amount", "Share"],
                  [["2026-03-05", "Tom & Jerry's <cafe>", 12.5, 0.25],
                   [date(2026, 3, 6), "", None, None]],
                  ["date", "text", "money", "pct"]),
            Sheet("Money & <stuff>", ["X"], [])])
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(z.read(name))            # well-formed or raises
            book = ET.fromstring(z.read("xl/workbook.xml"))
            names = [s.get("name") for s in book.find("m:sheets", NS)]
            self.assertEqual(names, ["Money & <stuff>", "Money & <stuff> 2"])
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        cells = {c.get("r"): c for c in sheet.iter(f"{{{NS['m']}}}c")}
        self.assertEqual(cells["A2"].find("m:v", NS).text, str((date(2026, 3, 5) - date(1899, 12, 30)).days))
        self.assertEqual(cells["C2"].find("m:v", NS).text, "12.5")
        self.assertEqual(cells["B2"].find("m:is/m:t", NS).text, "Tom & Jerry's <cafe>")
        self.assertNotIn("C3", cells)                       # empty cells are left out
        self.assertEqual(sheet.find("m:autoFilter", NS).get("ref"), "A1:D3")

    def test_export_writes_five_sheets_with_checks(self):
        db = self.tmp_path("s.db")
        with FinanceDB(db) as store:
            store.upsert_transactions([
                support.txn("2026-03-02", 15.49, "NETFLIX.COM", category="Subscriptions"),
                support.txn("2026-03-10", -2000.0, "ACME PAYROLL", category="Income")])
            txns, statements = store.transactions(), store.statement_models()
        out = export.workbook(txns, statements, db, self.tmp_path("f.xlsx"))
        with zipfile.ZipFile(out) as z:
            book = ET.fromstring(z.read("xl/workbook.xml"))
            tx = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        self.assertEqual([s.get("name") for s in book.find("m:sheets", NS)],
                         ["Transactions", "Months", "Categories", "Subscriptions", "Statements"])
        rows = tx.findall("m:sheetData/m:row", NS)
        self.assertEqual(len(rows), 3)                      # header + two transactions
        out_cell = {c.get("r"): c for c in rows[1]}["E2"]   # money out, not a signed amount
        self.assertEqual(out_cell.find("m:v", NS).text, "15.49")


if __name__ == "__main__":
    unittest.main()
