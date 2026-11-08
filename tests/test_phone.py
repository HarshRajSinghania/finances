"""
The phone view: read-only, on the home network, only with the key from the link.
"""

from __future__ import annotations

import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

import support

from finances import api
from finances.database import FinanceDB


class PhoneViewTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        path = self.tmp_path("p.db")
        with FinanceDB(path) as db:
            db.upsert_transactions([support.txn("2026-01-05", 4.5, "SQ *CAFE")])
        api.Handler.api = api.Api(path)
        api.Handler.log_message = lambda *a, **k: None
        # Bound on loopback for the test, but answering only as the LAN view would.
        self.cls = type("Phone", (api.PhoneHandler,), {"token": "k3y", "host": "phone.test:1"})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.cls)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, path, method="GET", cookie=None, host="phone.test:1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, headers=headers)
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res, body

    def test_the_link_key_becomes_a_cookie(self):
        res, _ = self.req("/?key=k3y")
        self.assertEqual(res.status, 303)
        self.assertEqual(res.getheader("Location"), "/")
        self.assertIn("finances_phone=k3y", res.getheader("Set-Cookie"))
        self.assertIn("HttpOnly", res.getheader("Set-Cookie"))

    def test_no_key_no_data(self):
        for path in ("/api/summary", "/api/transactions", "/?key=wrong"):
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0].status, 403)

    def test_reads_work_and_say_read_only(self):
        res, body = self.req("/api/session", cookie="finances_phone=k3y")
        self.assertEqual((res.status, body), (200, b'{"readOnly": true}'))
        self.assertEqual(self.req("/api/transactions", cookie="finances_phone=k3y")[0].status, 200)

    def test_nothing_can_be_changed_or_asked(self):
        c = "finances_phone=k3y"
        self.assertEqual(self.req("/api/statements/x.pdf", "DELETE", c)[0].status, 403)
        self.assertEqual(self.req("/api/budgets", "POST", c)[0].status, 403)
        self.assertEqual(self.req("/api/ask", cookie=c)[0].status, 403)
        self.assertEqual(self.req("/api/phone", cookie=c)[0].status, 403)

    def test_another_host_name_is_refused(self):
        self.assertEqual(self.req("/api/session", cookie="finances_phone=k3y",
                                  host="evil.example:1")[0].status, 403)

    def test_the_computer_itself_is_not_read_only(self):
        self.assertFalse(api.Handler.read_only)


if __name__ == "__main__":
    unittest.main()
