"""
Getting statements in: what is due, where to get it, and the watched folder.
"""

from __future__ import annotations

import os
import shutil
import time
import unittest
from datetime import date
from unittest import mock

import support

from finances import api, sources
from finances.database import FinanceDB
from finances.models import Statement

EXPORTS = os.path.join(os.path.dirname(__file__), "fixtures", "exports")


class DueTest(unittest.TestCase):
    def test_next_statement_is_ready_about_a_month_after_the_last(self):
        rows = sources.due([
            Statement(source_file="a1.pdf", issuer="Chase", account_last4="1", period_end="2026-09-30"),
            Statement(source_file="a2.pdf", issuer="Chase", account_last4="1", period_end="2026-10-31"),
            Statement(source_file="b.pdf", issuer="Amex", account_last4="2", period_end="2026-08-15"),
        ], today=date(2026, 11, 20))
        self.assertEqual([(r["issuer"], r["ready"], r["missing"]) for r in rows],
                         [("Amex", True, 3), ("Chase", False, 0)])
        self.assertTrue(rows[1]["guide"]["url"].startswith("https://www.chase.com/"))

    def test_every_bank_has_a_page_on_its_own_site(self):
        for name, g in sources.banks().items():
            with self.subTest(bank=name):
                self.assertTrue(g["url"].startswith("https://"))
                self.assertTrue(g["steps"])


class WatcherTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp_path("Downloads")
        self.pdfs = self.tmp_path("PDFs")
        os.makedirs(self.folder)
        patch = mock.patch.object(api, "PDF_DIR", self.pdfs)
        patch.start()
        self.addCleanup(patch.stop)
        self.api = api.Api(self.tmp_path("w.db"))
        with FinanceDB(self.api.db_path):
            pass
        st = self.api.watcher.state()
        st.update(folder=self.folder, since=time.time() - 60, seen={})
        self.api.watcher._save(st)

    def drop(self, name, src=None, text=None):
        path = os.path.join(self.folder, name)
        if src:
            shutil.copy(os.path.join(EXPORTS, src), path)
        else:
            with open(path, "w") as fh:
                fh.write(text)
        old = time.time() - 10
        os.utime(path, (old, old))

    def test_adds_statements_and_leaves_the_rest(self):
        self.drop("checking.ofx", "checking.ofx")
        self.drop("guests.csv", text="name,email\nSam,sam@example.com\n")
        self.drop("notes.txt", text="hello")
        done = self.api.watcher.scan()
        self.assertEqual([(d["file"], d["ok"]) for d in done], [("checking.ofx", True)])
        self.assertTrue(os.path.exists(os.path.join(self.pdfs, "checking.ofx")))
        # Seen once is enough: a second scan adds nothing.
        self.assertEqual(self.api.watcher.scan(), [])

    def test_files_from_before_watching_are_left_alone(self):
        self.drop("checking.ofx", "checking.ofx")
        os.utime(os.path.join(self.folder, "checking.ofx"), (1, 1))
        self.assertEqual(self.api.watcher.scan(), [])

    def test_off_means_off(self):
        self.api.watcher._save({})
        self.drop("checking.ofx", "checking.ofx")
        self.assertEqual(self.api.watcher.scan(), [])


if __name__ == "__main__":
    unittest.main()
