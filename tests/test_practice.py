"""
Bookkeepers: a store per client, a folder read in one go, and workpapers.
"""

from __future__ import annotations

import os
import shutil
import unittest
import zipfile

import support

from finances import practice

EXPORTS = os.path.join(os.path.dirname(__file__), "fixtures", "exports")


class PracticeTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp_path("clients")
        self.inbox = self.tmp_path("inbox")
        os.makedirs(self.inbox)
        for f in ("checking.ofx", "camt053.xml"):
            shutil.copy(os.path.join(EXPORTS, f), self.inbox)
        with open(os.path.join(self.inbox, "notes.txt"), "w") as fh:
            fh.write("not a statement")

    def test_intake_reads_a_folder_into_the_clients_own_store(self):
        rows = practice.intake(self.root, "Acme Bakery", self.inbox, log=None)
        self.assertEqual(sorted(r["file"] for r in rows), ["camt053.xml", "checking.ofx"])
        self.assertTrue(all(r["ok"] for r in rows))
        folder = os.path.join(self.root, "Acme Bakery")
        self.assertTrue(os.path.exists(os.path.join(folder, "finances.db")))
        self.assertTrue(os.path.exists(os.path.join(folder, "PDFs", "checking.ofx")))
        [c] = practice.clients(self.root)
        self.assertEqual((c["name"], c["statements"] > 0), ("Acme Bakery", True))

    def test_workpapers(self):
        practice.intake(self.root, "Acme", self.inbox, log=None)
        db, pdfs = practice.client_paths(self.root, "Acme")
        out = practice.workpapers(db, pdfs, self.tmp_path("wp.xlsx"), "Acme")
        with zipfile.ZipFile(out) as z:
            book = z.read("xl/workbook.xml").decode()
            recon = z.read("xl/worksheets/sheet2.xml").decode()
        for sheet in ("Summary", "Reconciliation", "Exceptions", "Transactions"):
            self.assertIn(f'name="{sheet}"', book)
        self.assertIn("Reviewed by", recon)
        self.assertRegex(recon, r"[0-9a-f]{64}")          # each file's SHA-256

    def test_names_cannot_escape_the_folder(self):
        self.assertEqual(practice.safe_name("../../etc"), "etc")
        with self.assertRaises(ValueError):
            practice.safe_name("///")


if __name__ == "__main__":
    unittest.main()
