"""
Tests for the tools built on top of the store: exports, the LLM fallback,
budgets and the cancel list, Wrapped, and multi-currency totals.

The LLM fallback is tested with a stand-in model, because what matters is not
what a model says but what is done with it: rows that add up to the
statement's printed totals are kept, rows that do not are thrown away.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import unittest.mock
from datetime import date
from http.server import ThreadingHTTPServer

import support

from finances import budgets, export, fx, wrapped
from finances.database import FinanceDB
from finances.engine import Engine
from finances.models import Statement, Transaction


def store_with(path, txns, statements=()):
    with FinanceDB(path) as db:
        db.upsert_transactions(list(txns))
        db.upsert_statements(list(statements))


def monthly(merchant, amount, months, category="Subscriptions", day=3, year=2026):
    return [support.txn(f"{year}-{m:02d}-{day:02d}", amount, merchant,
                        merchant=merchant, category=category) for m in months]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

class ExportTest(unittest.TestCase):
    def setUp(self):
        self.txns = [
            support.txn("2026-01-03", 17.99, "NETFLIX.COM", merchant="NETFLIX",
                        category="Subscriptions", issuer="Amex", account_last4="1005",
                        source_file="amex.pdf"),
            support.txn("2026-01-10", -500.0, "AUTOPAY", merchant="AUTOPAY",
                        category="Payment", issuer="Amex", account_last4="1005",
                        source_file="amex.pdf"),
            support.txn("2026-01-10", 500.0, "AMEX EPAYMENT", merchant="AMEX",
                        category="Payment", issuer="Chase", account_last4="4321",
                        source_file="chk.pdf"),
            support.txn("2026-01-15", -2000.0, "ACME PAYROLL", merchant="ACME",
                        category="Income", issuer="Chase", account_last4="4321",
                        source_file="chk.pdf"),
        ]
        self.stmts = [
            Statement("amex.pdf", issuer="Amex", account_last4="1005",
                      period_start="2026-01-01", period_end="2026-01-31",
                      previous_balance=600.0, new_balance=117.99, reconciled=1,
                      credit_limit=5000.0),
            Statement("chk.pdf", issuer="Chase", account_last4="4321",
                      period_start="2026-01-01", period_end="2026-01-31",
                      previous_balance=1000.0, new_balance=2500.0, reconciled=1),
        ]

    def test_accounts_by_kind(self):
        kinds = export.account_kinds(self.stmts)
        self.assertEqual(export.ledger_account("Amex", "1005",
                                               export.kind_of("Amex", "1005", kinds)),
                         "Liabilities:Card:Amex-1005")
        self.assertEqual(export.ledger_account("Chase", "4321",
                                               export.kind_of("Chase", "4321", kinds)),
                         "Assets:Bank:Chase-4321")

    def test_every_beancount_transaction_balances(self):
        out = export.beancount(self.txns, self.stmts, "USD", balances=True)
        blocks = [b for b in out.split("\n\n") if " * " in b]
        self.assertEqual(len(blocks), 4)
        for b in blocks:
            nums = [float(x) for x in re.findall(r"(-?\d+\.\d{2}) USD", b)]
            self.assertAlmostEqual(sum(nums), 0.0)
        # A card owed 117.99 is a liability of -117.99 the day after it closes.
        self.assertIn("2026-02-01 balance Liabilities:Card:Amex-1005 -117.99 USD", out)
        self.assertIn("2026-02-01 balance Assets:Bank:Chase-4321 2500.00 USD", out)
        # Both legs of the card payment meet in the clearing account.
        self.assertEqual(out.count("Assets:Transfers"), 3)       # open + two postings

    def test_beancount_balances_tie_out(self):
        """Opening plus postings equals every asserted balance, as bean-check would see."""
        out = export.beancount(self.txns, self.stmts, "USD", balances=True)
        bal = {"Liabilities:Card:Amex-1005": -600.0, "Assets:Bank:Chase-4321": 1000.0}
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+(-?\d+\.\d{2}) USD$", line)
            if m and m.group(1) in bal:
                bal[m.group(1)] += float(m.group(2))
        self.assertAlmostEqual(bal["Liabilities:Card:Amex-1005"], -117.99)
        self.assertAlmostEqual(bal["Assets:Bank:Chase-4321"], 2500.0)

    def test_hledger_assertions(self):
        out = export.hledger(self.txns, self.stmts, "USD", balances=True)
        self.assertIn("Liabilities:Card:Amex-1005", out)
        self.assertIn("0 USD = -117.99 USD", out)
        self.assertIn("= 1000.00 USD", out)

    def test_ynab_and_actual_split_per_account(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        files = export.write_per_account("ynab", self.txns, tmp)
        self.assertEqual([os.path.basename(f) for f in files],
                         ["ynab-amex-1005.csv", "ynab-chase-4321.csv"])
        with open(files[0]) as fh:
            rows = fh.read().splitlines()
        self.assertEqual(rows[0], "Date,Payee,Memo,Outflow,Inflow")
        self.assertEqual(rows[1], "01/03/2026,NETFLIX,NETFLIX.COM,17.99,")
        export.write_per_account("actual", self.txns, tmp)
        with open(os.path.join(tmp, "actual-chase-4321.csv")) as fh:
            self.assertIn("2026-01-15,ACME,ACME PAYROLL,Income,2000.00", fh.read())


# --------------------------------------------------------------------------
# LLM fallback
# --------------------------------------------------------------------------

@unittest.skipUnless(support.has_pdfplumber(), "llm_parse uses parse.py's helpers")
class LlmFallbackTest(unittest.TestCase):
    def statement(self, **kw):
        kw.setdefault("purchases", 30.0)
        kw.setdefault("payments", -100.0)
        return Statement("odd.pdf", issuer="Unknown", account_last4="7777",
                         statement_period="01/01/2026 - 01/31/2026",
                         period_start="2026-01-01", period_end="2026-01-31", **kw)

    def rows(self):
        return [{"date": "2026-01-05", "description": "CAFE", "amount": 10, "direction": "out"},
                {"date": "2026-01-09", "description": "BOOKS", "amount": "20.00", "direction": "out"},
                {"date": "2026-01-20", "description": "PAYMENT", "amount": 100, "direction": "in"},
                {"date": "nonsense", "description": "X", "amount": 1},
                {"date": "2025-06-01", "description": "OTHER MONTH", "amount": 5}]

    def test_rows_that_add_up_are_kept(self):
        from finances import llm_parse
        txns, dropped = llm_parse.rows_to_transactions(
            self.rows(), self.statement(), start=date(2026, 1, 1), end=date(2026, 1, 31))
        self.assertEqual(dropped, 2)
        self.assertEqual([t.amount for t in txns], [10.0, 20.0, -100.0])
        st = llm_parse.verify(txns, self.statement(), "test-model")
        self.assertEqual(st.reconciled, 1)
        self.assertEqual(st.source_format, "pdf+llm")
        self.assertIn("agrees with the statement", st.reconcile_note)

    def test_rows_that_do_not_add_up_are_discarded(self):
        from finances import llm_parse
        st = self.statement(purchases=45.0)
        with unittest.mock.patch.object(llm_parse, "page_texts", return_value=["page one"]):
            got = llm_parse.fallback("odd.pdf", st, complete=lambda p: json.dumps(self.rows()),
                                     model="m", log=lambda *a: None)
            self.assertIsNone(got)
            kept = llm_parse.fallback("odd.pdf", st, complete=lambda p: json.dumps(self.rows()),
                                      model="m", accept_unverified=True, log=lambda *a: None)
        txns, verdict = kept
        self.assertEqual(verdict.reconciled, 0)
        self.assertIn("NOT verified", verdict.reconcile_note)

    def test_a_model_wrapping_json_in_prose_still_parses(self):
        from finances import llm_parse
        reply = "Sure! Here you go:\n```json\n" + json.dumps(self.rows()[:3]) + "\n```"
        with unittest.mock.patch.object(llm_parse, "page_texts", return_value=["p"]):
            txns, st = llm_parse.fallback("odd.pdf", self.statement(),
                                          complete=lambda p: reply, model="m",
                                          log=lambda *a: None)
        self.assertEqual(st.reconciled, 1)
        self.assertEqual(len(txns), 3)

    def test_only_broken_or_empty_results_ask_for_help(self):
        from finances import llm_parse
        ok = self.statement(reconciled=1)
        self.assertFalse(llm_parse.needs_help([support.txn("2026-01-01", 1)], ok))
        self.assertTrue(llm_parse.needs_help([], ok))
        self.assertTrue(llm_parse.needs_help([support.txn("2026-01-01", 1)],
                                             self.statement(reconciled=0)))


# --------------------------------------------------------------------------
# Budgets, the cancel list, Wrapped, currencies
# --------------------------------------------------------------------------

class BudgetTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp_path("b.db")
        txns = (monthly("NETFLIX", 15.49, range(1, 6)) + monthly("NETFLIX", 17.99, range(6, 11))
                + monthly("SPOTIFY", 11.99, range(1, 11), day=7)
                + monthly("GREYSTAR RENT", 1950.0, range(1, 11), category="Rent/Housing", day=1)
                + [support.txn("2026-10-20", 120.0, "BISTRO", merchant="BISTRO",
                               category="Dining"),
                   support.txn("2026-10-21", 90.0, "BISTRO", merchant="BISTRO",
                               category="Dining"),
                   support.txn("2026-09-20", 60.0, "BISTRO", merchant="BISTRO",
                               category="Dining")])
        store_with(self.path, txns)

    def test_budget_state_and_suggestions(self):
        with Engine(self.path) as eng:
            budgets.set_budget(eng, "Dining", 150)
            s = budgets.status(eng)
        self.assertEqual(s["month"], "2026-10")
        [b] = s["budgets"]
        self.assertEqual((b["spent"], b["state"], b["lastMonth"]), (210.0, "over", 60.0))
        self.assertNotIn("Rent/Housing", [g["category"] for g in s["suggestions"]])
        with Engine(self.path) as eng:
            budgets.set_budget(eng, "Dining", None)
            self.assertEqual(budgets.status(eng)["budgets"], [])

    def test_cancel_list_leaves_rent_alone_and_explains_itself(self):
        with Engine(self.path) as eng:
            rv = budgets.review(eng)
        names = [i["r"].name for i in rv["candidates"]]
        self.assertIn("NETFLIX", names)
        self.assertNotIn("GREYSTAR RENT", names)
        netflix = next(i for i in rv["candidates"] if i["r"].name == "NETFLIX")
        self.assertTrue(any("price up" in r for r in netflix["reasons"]))
        self.assertTrue(any("one of 2" in r for r in netflix["reasons"]))

    def test_a_charge_after_cancelling_is_flagged(self):
        with Engine(self.path) as eng:
            budgets.mark(eng, "SPOTIFY", "cancelled", on=date(2026, 8, 1))
            budgets.mark(eng, "NETFLIX", "keep")
            rv = budgets.review(eng)
        self.assertEqual([i["r"].name for i in rv["stillCharging"]], ["SPOTIFY"])
        self.assertEqual([i["r"].name for i in rv["kept"]], ["NETFLIX"])
        self.assertEqual(rv["candidates"], [])

    def test_wrapped(self):
        with Engine(self.path) as eng:
            w = wrapped.wrapped(eng)
        self.assertEqual(w["year"], 2026)
        self.assertFalse(w["fullYear"])
        self.assertEqual(w["topCategories"][0]["category"], "Rent/Housing")
        self.assertEqual(w["biggest"]["amount"], 1950.0)
        self.assertEqual(w["subscriptions"]["count"], 2)       # rent is not one
        self.assertEqual(w["favourite"]["visits"], 10)


class CurrencyTest(support.TempDirMixin, unittest.TestCase):
    def test_other_currencies_are_converted_or_held_out(self):
        path = self.tmp_path("c.db")
        store_with(path, [support.txn("2026-01-01", 10.0, "A"),
                          support.txn("2026-01-02", 10.0, "B", currency="EUR"),
                          support.txn("2026-01-03", 10.0, "C", currency="USD")])
        with Engine(path) as eng:
            self.assertEqual(eng.total_spend(), 20.0)
            self.assertEqual(dict(eng.fx_missing), {"EUR": 1})
        with FinanceDB(path) as db:
            db.set_setting(fx.RATES_KEY, {"EUR": 1.1})
        with Engine(path) as eng:
            self.assertAlmostEqual(eng.total_spend(), 31.0)
            self.assertFalse(eng.fx_missing)
        # The store itself still holds euros.
        with FinanceDB(path) as db:
            self.assertEqual(db.transactions()[1].amount, 10.0)

    def test_base_currency_can_change(self):
        path = self.tmp_path("c.db")
        store_with(path, [support.txn("2026-01-01", 10.0, "A", currency="EUR")])
        with FinanceDB(path) as db:
            db.set_setting(fx.BASE_KEY, "EUR")
        with Engine(path) as eng:
            self.assertEqual((eng.total_spend(), eng.currency), (10.0, "EUR"))


# --------------------------------------------------------------------------
# API writes
# --------------------------------------------------------------------------

class ApiWriteTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from finances import api
        path = self.tmp_path("w.db")
        store_with(path, monthly("NETFLIX", 15.49, range(1, 6)))
        api.Handler.api = api.Api(path)
        api.Handler.log_message = lambda *a, **k: None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, path, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": "localhost", "Content-Type": "application/json", **(headers or {})}
        conn.request("POST", path, body=json.dumps(body), headers=h)
        res = conn.getresponse()
        data = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, data

    def test_set_a_budget(self):
        status, data = self.post("/api/budgets", {"category": "Dining", "limit": 200})
        self.assertEqual(status, 200)
        self.assertEqual(data["budgets"][0]["limit"], 200.0)

    def test_writes_need_json_and_a_local_origin(self):
        status, _ = self.post("/api/budgets", {"category": "X", "limit": 1},
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.post("/api/budgets", {"category": "X", "limit": 1},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_bad_input_is_a_400(self):
        status, data = self.post("/api/subscriptions/mark", {"key": "NETFLIX", "status": "maybe"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
