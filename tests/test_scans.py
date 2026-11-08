"""
Scanned statements go through OCR, and investment statements are read for value.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import support
from pdfgen import scanned_pdf, text_pdf

if support.has_pdfplumber():
    from finances import parse

CARD = ["Example Bank Card Statement", "Account ending in 4242",
        "Statement period 10/01/2026 to 10/31/2026",
        "Previous Balance $0.00", "Purchases $16.50", "New Balance $16.50",
        "10/05 BLUE BOTTLE COFFEE 4.50", "10/12 CITY PARKING 12.00"]
BROKERAGE = ["Fidelity Investments", "Brokerage Account 7781",
             "Statement period 10/01/2026 to 10/31/2026",
             "Beginning Account Value $24,500.00", "Ending Account Value $25,130.12",
             "10/15 DIVIDEND RECEIVED VANGUARD 12.40"]


@unittest.skipUnless(support.has_pdfplumber(), "needs pdfplumber")
class ScanTest(support.TempDirMixin, unittest.TestCase):
    def write(self, name, blob):
        path = self.tmp_path(name)
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    def test_a_scan_without_ocr_says_how_to_read_it(self):
        path = self.write("scan-2026-10.pdf", scanned_pdf())
        with mock.patch("shutil.which", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "ocrmypdf"):
            parse.parse_statement(path)

    def test_a_scan_is_read_through_ocr_and_says_so(self):
        path = self.write("scan-2026-10.pdf", scanned_pdf())

        def fake_ocr(src, out_dir, password=None):
            return self.write_to(out_dir, os.path.basename(src), text_pdf(CARD))
        with mock.patch.object(parse, "ocr_copy", side_effect=fake_ocr):
            txns, meta = parse.parse_statement(path)
        st = meta["statement"]
        self.assertEqual((st.source_file, st.source_format), ("scan-2026-10.pdf", "pdf+ocr"))
        self.assertIn("OCR", st.reconcile_note)
        self.assertEqual({t.source_file for t in txns}, {"scan-2026-10.pdf"})
        self.assertEqual(sorted(t.amount for t in txns), [4.5, 12.0])

    def write_to(self, folder, name, blob):
        path = os.path.join(folder, name)
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    def test_a_brokerage_statement_is_its_value_not_its_trades(self):
        path = self.write("fidelity-2026-10.pdf", text_pdf(BROKERAGE))
        txns, meta = parse.parse_statement(path)
        st = meta["statement"]
        self.assertEqual(txns, [])
        self.assertEqual((st.account_type, st.issuer, st.previous_balance, st.new_balance),
                         ("investment", "Fidelity", 24500.0, 25130.12))
        self.assertFalse(st.checkable)


if __name__ == "__main__":
    unittest.main()
