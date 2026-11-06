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
import threading
import unittest
from http.server import ThreadingHTTPServer

import support

import api
from database import FinanceDB


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


if __name__ == "__main__":
    unittest.main()
