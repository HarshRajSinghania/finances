"""
The SimpleFIN live feed: claimed once, synced into the store, then checked
against the statement that later covers the same days.
"""

from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timezone

import support

from finances import simplefin
from finances.database import FinanceDB
from finances.engine import Engine
from finances.models import Statement

CLAIM = "https://bridge.example/simplefin/claim/abc"
ACCESS = "https://user:secret@bridge.example/simplefin"
TOKEN = base64.b64encode(CLAIM.encode()).decode()


def ts(d):
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()) + 12 * 3600


class FakeBridge:
    def __init__(self):
        self.calls = []

    def __call__(self, url, method="GET", data=None):
        self.calls.append((method, url))
        if url == CLAIM:
            return ACCESS.encode()
        return json.dumps({"errors": [], "accounts": [
            {"org": {"name": "Chase Bank"}, "id": "ACT-1", "name": "Sapphire Card (...1005)",
             "currency": "USD", "balance": "-412.50", "balance-date": ts("2026-11-05"),
             "transactions": [
                 {"id": "t1", "posted": ts("2026-10-30"), "amount": "-12.00",
                  "description": "BLUE BOTTLE COFFEE"},
                 {"id": "t2", "posted": ts("2026-11-03"), "amount": "-400.50",
                  "description": "DELTA AIR LINES"},
                 {"id": "t3", "posted": ts("2026-11-04"), "amount": "-9.99",
                  "description": "PENDING THING", "pending": True}]}]}).encode()


class FeedTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp_path("f.db")
        self.bridge = FakeBridge()
        self.out = simplefin.connect(self.path, TOKEN, self.bridge)

    def test_connect_claims_then_syncs(self):
        self.assertEqual(self.bridge.calls[0], ("POST", CLAIM))
        self.assertTrue(self.bridge.calls[1][1].startswith(ACCESS + "/accounts?start-date="))
        self.assertTrue(self.out["connected"])
        self.assertNotIn("secret", json.dumps(self.out))      # never handed back
        [acct] = self.out["accounts"]
        self.assertEqual((acct["issuer"], acct["last4"], acct["kind"], acct["balance"]),
                         ("Chase", "1005", "card", 412.5))

    def test_rows_are_signed_like_statements_and_pending_is_skipped(self):
        with FinanceDB(self.path) as db:
            rows = sorted((t.date, t.amount) for t in db.transactions())
        self.assertEqual(rows, [("2026-10-30", 12.0), ("2026-11-03", 400.5)])

    def test_the_statement_takes_over_and_checks_the_feed(self):
        with FinanceDB(self.path) as db:
            db.upsert_statements([Statement(
                source_file="chase-oct.pdf", issuer="Chase", account_last4="1005",
                period_start="2026-10-01", period_end="2026-10-31", reconciled=1)])
            db.upsert_transactions([support.txn("2026-10-30", 12.0, "BLUE BOTTLE",
                                                issuer="Chase", account_last4="1005",
                                                source_file="chase-oct.pdf")])
            # October now comes from the statement; November is still the feed's.
            visible = sorted((t.date, t.source_file) for t in db.transactions())
        self.assertEqual(visible, [("2026-10-30", "chase-oct.pdf"),
                                   ("2026-11-03", "simplefin#ACT-1")])
        [check] = simplefin.status(self.path)["checks"]
        self.assertEqual((check["feedRows"], check["matched"]), (1, 1))

    def test_a_feed_is_not_a_statement_to_reconcile(self):
        with Engine(self.path) as eng:
            self.assertEqual(eng.coverage()["statements"], 0)

    def test_disconnect_forgets_the_access(self):
        self.assertFalse(simplefin.disconnect(self.path)["connected"])
        with FinanceDB(self.path) as db:
            self.assertIsNone(db.get_setting(simplefin.KEY))

    def test_a_bad_token(self):
        with self.assertRaises(simplefin.FeedError):
            simplefin.claim("not base64 at all!", self.bridge)


if __name__ == "__main__":
    unittest.main()
