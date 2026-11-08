"""
Net worth from the balances statements print, and investment statements.
"""

from __future__ import annotations

import unittest

from finances import parse
from finances.models import Statement
from finances.networth import net_worth


def st(name, issuer, end, balance, **kw):
    return Statement(source_file=name, issuer=issuer, account_last4=kw.pop("last4", "1"),
                     period_end=end, new_balance=balance, **kw)


class NetWorthTest(unittest.TestCase):
    def test_owned_minus_owed_month_by_month(self):
        out = net_worth([
            st("c1", "Chase", "2026-01-31", 5000.0),
            st("c2", "Chase", "2026-02-28", 5500.0),
            st("a1", "Amex", "2026-01-28", 1200.0, credit_limit=9000.0, last4="2"),
            st("a2", "Amex", "2026-02-27", 800.0, credit_limit=9000.0, last4="2"),
            st("f2", "Fidelity", "2026-02-28", 20000.0, account_type="investment", last4="3"),
        ])
        self.assertEqual([(m["month"], m["assets"], m["debts"], m["net"], m["partial"])
                          for m in out["months"]],
                         [("2026-01", 5000.0, 1200.0, 3800.0, True),
                          ("2026-02", 25500.0, 800.0, 24700.0, False)])
        self.assertEqual({a["issuer"]: a["kind"] for a in out["accounts"]},
                         {"Chase": "bank", "Amex": "card", "Fidelity": "investment"})

    def test_an_old_balance_is_carried_and_said_to_be_old(self):
        out = net_worth([st("c1", "Chase", "2026-01-31", 100.0),
                         st("s1", "Ally", "2026-03-31", 50.0, last4="9")])
        march = out["months"][-1]
        self.assertEqual((march["net"], march["stale"]), (150.0, ["Chase 1"]))

    def test_nothing_to_go_on(self):
        self.assertEqual(net_worth([])["months"], [])


class InvestmentStatementTest(unittest.TestCase):
    def test_reads_the_value_of_a_brokerage_statement(self):
        text = ("Fidelity Investments  Brokerage Account\n"
                "Beginning Account Value $10,000.00\nEnding Account Value $10,512.34")
        self.assertEqual(parse.investment_value(text), (10000.0, 10512.34))
        self.assertEqual(parse.investment_issuer(text), "Fidelity")

    def test_a_bank_or_card_statement_is_not_one(self):
        self.assertIsNone(parse.investment_value("Chase checking\nEnding balance $1,000.00"))
        self.assertIsNone(parse.investment_value("Total account value $5.00 cashback"))


if __name__ == "__main__":
    unittest.main()
