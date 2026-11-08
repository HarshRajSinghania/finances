"""
Tests for api.py's request guard.

The dashboard serves every transaction you have to whoever can reach it, so
the server answers only requests that come from a local page. Two attacks are
covered: a hostile site reading the API cross-origin (no CORS header is ever
sent), and DNS rebinding, where a hostile domain resolves to 127.0.0.1 so the
browser considers it same-origin — caught by the Host header.
"""

from __future__ import annotations

import http.client
import os
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

import support

from finances import api
from finances.database import FinanceDB
from finances.models import Statement


class GuardTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        path = self.tmp_path("t.db")
        with FinanceDB(path) as db:
            db.upsert_transactions([support.txn("2026-01-05", 4.5, "SQ *CAFE")])
        api.Handler.api = api.Api(path)
        api.Handler.log_message = lambda *a, **k: None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method="GET", path="/api/health", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        res = conn.getresponse()
        res.read()
        conn.close()
        return res

    def test_local_requests_are_served(self):
        for host in ("localhost:8787", "127.0.0.1:8787", "[::1]:8787", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host}).status, 200)

    def test_a_local_origin_is_allowed(self):
        res = self.request(headers={"Host": "localhost:8787",
                                    "Origin": "http://localhost:5173"})
        self.assertEqual(res.status, 200)

    def test_dns_rebinding_is_refused(self):
        res = self.request(path="/api/transactions",
                           headers={"Host": "attacker.example:8787"})
        self.assertEqual(res.status, 403)

    def test_cross_site_writes_are_refused(self):
        for method, path in (("POST", "/api/upload"), ("DELETE", "/api/statements/x.pdf")):
            with self.subTest(method=method):
                res = self.request(method, path, {"Host": "localhost:8787",
                                                  "Origin": "https://attacker.example"})
                self.assertEqual(res.status, 403)

    def test_no_cors_header_is_sent(self):
        res = self.request(headers={"Host": "localhost:8787"})
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))


class OriginalTest(GuardTest):
    """The file a statement was read from opens in the browser, and nothing else does."""

    def setUp(self):
        super().setUp()
        self.pdfs = self.tmp_path("PDFs")
        os.makedirs(self.pdfs)
        for name, body in (("card.pdf", b"%PDF-1.4 card"), ("bank.ofx", b"<OFX><b>x</b>"),
                           ("other.pdf", b"%PDF-1.4 not a statement")):
            with open(os.path.join(self.pdfs, name), "wb") as fh:
                fh.write(body)
        with FinanceDB(api.Handler.api.db_path) as db:
            db.upsert_statements([Statement(source_file="card.pdf"),
                                  Statement(source_file="bank.ofx#1234"),
                                  Statement(source_file="gone.pdf")])
        patch = mock.patch.object(api, "PDF_DIR", self.pdfs)
        patch.start()
        self.addCleanup(patch.stop)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "localhost"})
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res, body

    def test_a_pdf_is_served_as_itself(self):
        res, body = self.get("/api/originals/card.pdf")
        self.assertEqual((res.status, res.getheader("Content-Type"), body),
                         (200, "application/pdf", b"%PDF-1.4 card"))
        self.assertTrue(res.getheader("Content-Disposition").startswith("inline"))

    def test_an_export_is_plain_text_in_a_sandbox(self):
        res, body = self.get("/api/originals/" + urllib.parse.quote("bank.ofx#1234"))
        self.assertEqual(res.status, 200)
        self.assertTrue(res.getheader("Content-Type").startswith("text/plain"))
        self.assertEqual(res.getheader("Content-Security-Policy"), "sandbox")
        self.assertEqual(body, b"<OFX><b>x</b>")

    def test_only_statement_files_are_served(self):
        for path in ("other.pdf", "gone.pdf", "..%2Ft.db", "%2E%2E%2F%2E%2E%2Fetc%2Fpasswd"):
            with self.subTest(path=path):
                self.assertEqual(self.get("/api/originals/" + path)[0].status, 404)

    def test_the_list_says_which_can_be_opened(self):
        rows = {r["sourceFile"]: r["hasFile"]
                for r in api.Handler.api.statements({})["statements"]}
        self.assertEqual(rows, {"card.pdf": True, "bank.ofx#1234": True, "gone.pdf": False})


if __name__ == "__main__":
    unittest.main()
