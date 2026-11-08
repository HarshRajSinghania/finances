"""
Checking an answer's amounts against what the lookups returned.
"""

from __future__ import annotations

import json
import unittest

import support
from test_chat import ASK, FakeProvider, anthropic_text, anthropic_tool

from finances import cite
from finances.chat import Chat
from finances.database import FinanceDB
from finances.models import Statement

ROWS = json.dumps({"transactions": [
    {"date": "2026-03-10", "merchant": "BLUE BOTTLE", "amount": 4.5,
     "source_file": "amex-2026-03.pdf", "page": 2},
    {"date": "2026-03-11", "merchant": "BLUE BOTTLE", "amount": 5.25,
     "source_file": "amex-2026-03.pdf", "page": 2}], "count": 2})
MONTHS = json.dumps({"months": [{"month": "2026-02", "spent": 3386.99},
                                {"month": "2026-03", "spent": 3680.23}]})
COVERED = {"statements": 2, "reconciled": 2, "mismatched": 0, "unchecked": 0}


class AmountsTest(unittest.TestCase):
    def test_shapes(self):
        got = [(a["text"], a["value"]) for a in cite.amounts(
            "You spent $3,680.23, about $3.7k, vs -$12 and €9.99; 40 rows.")]
        self.assertEqual(got, [("$3,680.23", 3680.23), ("$3.7k", 3700.0),
                               ("-$12", 12.0), ("€9.99", 9.99)])


class CheckTest(unittest.TestCase):
    def check(self, answer, coverage=COVERED):
        return cite.check(answer, [("search_transactions", ROWS), ("monthly_totals", MONTHS)],
                          coverage)

    def test_a_charge_links_to_its_statement_page(self):
        [c] = self.check("The second was **$5.25**.")["citations"]
        self.assertEqual(c["status"], "found")
        self.assertEqual((c["source"]["kind"], c["source"]["file"], c["source"]["page"]),
                         ("row", "amex-2026-03.pdf", 2))

    def test_totals_rounding_and_differences(self):
        out = self.check("March was $3,680 against $3,386.99, $293.24 more.")
        self.assertEqual([c["source"]["kind"] for c in out["citations"]],
                         ["lookup", "lookup", "derived"])
        self.assertEqual(out["verdict"]["level"], "verified")

    def test_an_amount_from_nowhere_is_flagged(self):
        out = self.check("You spent $9.75 on coffee and $1,200 on rent.")
        self.assertEqual([c["status"] for c in out["citations"]], ["found", "missing"])
        self.assertEqual(out["verdict"]["level"], "unverified")

    def test_a_count_does_not_vouch_for_money(self):
        [c] = self.check("That is $2.")["citations"]
        self.assertEqual(c["status"], "missing")

    def test_unreconciled_statements_stop_short_of_verified(self):
        out = self.check("$5.25", {**COVERED, "unchecked": 1})
        self.assertEqual(out["verdict"]["level"], "found")

    def test_no_amounts(self):
        self.assertEqual(self.check("Mostly coffee.")["verdict"]["level"], "none")


class ChatCheckTest(support.TempDirMixin, unittest.TestCase):
    def test_every_reply_carries_its_check(self):
        path = self.tmp_path("c.db")
        with FinanceDB(path) as db:
            db.upsert_transactions([
                support.txn("2026-03-10", 4.50, "BLUE BOTTLE", category="Coffee"),
                support.txn("2026-03-11", 5.25, "BLUE BOTTLE", category="Coffee")])
            db.upsert_statements([Statement(source_file="x.pdf", reconciled=1)])
        fake = FakeProvider(anthropic_tool("spending_by_category", {"month": "2026-03"}),
                            anthropic_text("You spent **$9.75**, not $90."))
        out = Chat(path, post=fake).ask("anthropic", "", ASK, key="k")
        self.assertEqual([c["status"] for c in out["check"]["citations"]],
                         ["found", "missing"])
        self.assertEqual(out["check"]["verdict"]["missing"], 1)


if __name__ == "__main__":
    unittest.main()
