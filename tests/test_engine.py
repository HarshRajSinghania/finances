"""
Unit tests for engine.py — the read model.

Every test here builds a small store by hand where the right answer is known by
arithmetic, not by running the code. That is the point: engine.py is where the
project decides what "spending" means, which months count towards an average,
and how much of the data is trustworthy, and those decisions are what the four
downstream tools all read off.

The fixture is deliberately awkward in the ways real data is awkward: it has a
positive-but-not-spending ATM withdrawal, a partial trailing month, income in
only some months, and statements in all three reconciliation states.
"""

from __future__ import annotations

import os
import unittest
from datetime import date

import support
from support import stmt, txn

from finances.database import FinanceDB
from finances.engine import (ESSENTIAL, NON_SPEND, Engine, SavingsForecast,
                    SpendingForecast, load_transactions, month_key)


# --------------------------------------------------------------------------
# The fixture. Spend by month works out to Jan 315.99, Feb 465.99, Mar 65.99;
# March is a stub (data stops on the 9th) and must not count in any average.
# --------------------------------------------------------------------------

SPEND_ROWS = [
    ("2026-01-02", 200.00, "WHOLE FOODS MKT", "Groceries"),
    ("2026-01-09", 15.99, "NETFLIX.COM", "Subscriptions"),
    ("2026-01-20", 100.00, "SQ *CAFE", "Dining"),
    ("2026-02-05", 300.00, "WHOLE FOODS MKT", "Groceries"),
    ("2026-02-09", 15.99, "NETFLIX.COM", "Subscriptions"),
    ("2026-02-14", 150.00, "SQ *CAFE", "Dining"),
    ("2026-03-02", 50.00, "WHOLE FOODS MKT", "Groceries"),
    ("2026-03-09", 15.99, "NETFLIX.COM", "Subscriptions"),
]

# Money leaving the account that is not spending: it moves between the user's
# own pockets. Positive, so only the category keeps it out of the totals.
NON_SPEND_ROWS = [
    ("2026-01-15", 400.00, "ATM WITHDRAWAL", "Transfers/Cash"),
    ("2026-02-20", 250.00, "AUTOPAY THANK YOU", "Payment"),
]

CREDIT_ROWS = [
    ("2026-01-31", -3000.00, "PAYROLL DIRECT DEP", "Income"),
    ("2026-02-27", -3000.00, "PAYROLL DIRECT DEP", "Income"),
    ("2026-03-05", -25.00, "RETURN CREDIT", "Credits/Refunds"),
]

JAN, FEB, MAR = 315.99, 465.99, 65.99
TOTAL_SPEND = JAN + FEB + MAR
COMPLETE = ["2026-01", "2026-02"]
MONTHLY_AVG = (JAN + FEB) / 2


def fixture_transactions():
    return [txn(d, amt, desc, category=cat, merchant=desc,
                issuer="Amex", account_last4="1001", source_file="amex.pdf")
            for d, amt, desc, cat in SPEND_ROWS + NON_SPEND_ROWS + CREDIT_ROWS]


class TaxonomyContractTests(unittest.TestCase):
    """The category sets engine.py filters on. If these move, the fixtures in
    this file stop meaning what they say, so fail here rather than mysteriously
    downstream."""

    def test_non_spend_covers_the_four_pass_through_categories(self):
        self.assertLessEqual({"Payment", "Credits/Refunds", "Transfers/Cash",
                              "Income"}, set(NON_SPEND))

    def test_essential_covers_the_obvious_essentials(self):
        self.assertLessEqual({"Rent/Housing", "Utilities", "Groceries",
                              "Transport", "Insurance", "Health"}, set(ESSENTIAL))

    def test_the_sets_do_not_overlap(self):
        self.assertEqual(set(NON_SPEND) & set(ESSENTIAL), set())


class EngineTestCase(support.TempDirMixin, unittest.TestCase):

    def engine(self, txns=None, stmts=(), name="engine.db") -> Engine:
        path = self.tmp_path(name)
        with FinanceDB(path) as db:
            db.upsert_transactions(fixture_transactions() if txns is None else txns)
            if stmts:
                db.upsert_statements(list(stmts))
        eng = Engine(path)
        self.addCleanup(eng.close)
        return eng


class RowSelectionTests(EngineTestCase):

    def test_spend_excludes_non_spend_categories(self):
        eng = self.engine()
        self.assertEqual(len(eng.transactions), len(SPEND_ROWS) + len(NON_SPEND_ROWS)
                         + len(CREDIT_ROWS))
        self.assertEqual(len(eng.spend()), len(SPEND_ROWS))
        for t in eng.spend():
            self.assertNotIn(t.category, NON_SPEND)
        descs = {t.description for t in eng.spend()}
        self.assertNotIn("ATM WITHDRAWAL", descs)      # positive, but a transfer
        self.assertNotIn("AUTOPAY THANK YOU", descs)

    def test_spend_excludes_credits(self):
        eng = self.engine()
        self.assertTrue(all(t.amount > 0 for t in eng.spend()))

    def test_income_is_negative_rows_categorised_as_income(self):
        eng = self.engine()
        self.assertEqual(len(eng.income()), 2)
        self.assertTrue(all(t.amount < 0 for t in eng.income()))
        self.assertNotIn("RETURN CREDIT", {t.description for t in eng.income()})

    def test_dates_spans_the_spending_rows(self):
        eng = self.engine()
        self.assertEqual(eng.dates(), (date(2026, 1, 2), date(2026, 3, 9)))

    def test_dates_of_an_empty_store_is_none(self):
        self.assertIsNone(self.engine(txns=[], name="empty.db").dates())


class AggregateTests(EngineTestCase):

    def test_total_spend(self):
        self.assertAlmostEqual(self.engine().total_spend(), TOTAL_SPEND, places=2)

    def test_total_credits_is_reported_positive(self):
        # 3000 + 3000 + 25, sign-flipped so it reads as a magnitude.
        self.assertAlmostEqual(self.engine().total_credits(), 6025.00, places=2)

    def test_by_category(self):
        got = self.engine().by_category()
        self.assertEqual(list(got), ["Groceries", "Dining", "Subscriptions"])
        self.assertAlmostEqual(got["Groceries"], 550.00, places=2)
        self.assertAlmostEqual(got["Dining"], 250.00, places=2)
        self.assertAlmostEqual(got["Subscriptions"], 47.97, places=2)
        self.assertAlmostEqual(sum(got.values()), TOTAL_SPEND, places=2)

    def test_by_category_is_sorted_descending(self):
        vals = list(self.engine().by_category().values())
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_by_month(self):
        got = self.engine().by_month()
        self.assertEqual(list(got), ["2026-01", "2026-02", "2026-03"])
        self.assertAlmostEqual(got["2026-01"], JAN, places=2)
        self.assertAlmostEqual(got["2026-02"], FEB, places=2)
        self.assertAlmostEqual(got["2026-03"], MAR, places=2)

    def test_by_month_is_chronological(self):
        got = list(self.engine().by_month())
        self.assertEqual(got, sorted(got))

    def test_income_by_month_is_positive_and_only_covers_income_months(self):
        got = self.engine().income_by_month()
        self.assertEqual(got, {"2026-01": 3000.00, "2026-02": 3000.00})


class CompleteMonthsTests(EngineTestCase):

    def test_a_partial_trailing_month_is_dropped(self):
        # Data stops on 2026-03-09, so March is 9/31 covered and must not drag
        # the average down.
        eng = self.engine()
        self.assertIn("2026-03", eng.by_month())
        self.assertEqual(eng.complete_months(), COMPLETE)

    def test_a_partial_leading_month_is_dropped(self):
        rows = [txn("2026-01-28", 100.0, "LATE START", category="Dining"),
                txn("2026-02-05", 200.0, "A", category="Dining"),
                txn("2026-02-25", 100.0, "B", category="Dining"),
                txn("2026-03-05", 200.0, "C", category="Dining"),
                txn("2026-03-30", 100.0, "D", category="Dining")]
        eng = self.engine(txns=rows, name="lead.db")
        self.assertEqual(eng.complete_months(), ["2026-02", "2026-03"])

    def test_a_single_full_month_is_complete(self):
        rows = [txn("2026-02-01", 10.0, "A", category="Dining"),
                txn("2026-02-28", 10.0, "B", category="Dining")]
        eng = self.engine(txns=rows, name="one.db")
        self.assertEqual(eng.complete_months(), ["2026-02"])

    def test_falls_back_to_every_month_when_none_qualify(self):
        # Two days of data: nothing is "complete", but returning [] would make
        # every average zero, which is worse than an honest small sample.
        rows = [txn("2026-02-10", 10.0, "A", category="Dining"),
                txn("2026-02-11", 10.0, "B", category="Dining")]
        eng = self.engine(txns=rows, name="stub.db")
        self.assertEqual(eng.complete_months(), ["2026-02"])

    def test_empty_store_has_no_complete_months(self):
        self.assertEqual(self.engine(txns=[], name="empty2.db").complete_months(), [])


class AverageTests(EngineTestCase):

    def test_monthly_average_uses_complete_months_only(self):
        eng = self.engine()
        self.assertAlmostEqual(eng.monthly_average(), MONTHLY_AVG, places=2)
        # The naive average over all three months would be materially lower.
        naive = TOTAL_SPEND / 3
        self.assertGreater(eng.monthly_average(), naive)

    def test_monthly_average_of_an_empty_store_is_zero(self):
        self.assertEqual(self.engine(txns=[], name="empty3.db").monthly_average(), 0.0)

    def test_essential_monthly_uses_complete_months_only(self):
        # Groceries only: 200 in January, 300 in February, averaged over two.
        self.assertAlmostEqual(self.engine().essential_monthly(), 250.00, places=2)

    def test_essential_monthly_is_zero_without_essential_spend(self):
        rows = [txn("2026-02-01", 10.0, "A", category="Dining"),
                txn("2026-02-28", 10.0, "B", category="Dining")]
        self.assertEqual(self.engine(txns=rows, name="noess.db").essential_monthly(), 0.0)


class SurplusTests(EngineTestCase):

    def test_surplus_is_income_minus_average_spend(self):
        self.assertAlmostEqual(self.engine().surplus(), 3000.0 - MONTHLY_AVG, places=2)

    def test_surplus_is_none_when_income_is_unknown(self):
        # No Income rows at all: guessing here would produce a confident wrong
        # number in the planner, so the contract is to say "I don't know".
        rows = [txn(d, amt, desc, category=cat)
                for d, amt, desc, cat in SPEND_ROWS]
        self.assertIsNone(self.engine(txns=rows, name="noinc.db").surplus())

    def test_surplus_is_none_when_income_is_all_zero(self):
        rows = [txn(d, amt, desc, category=cat) for d, amt, desc, cat in SPEND_ROWS]
        rows.append(txn("2026-01-31", 0.0, "ZERO DEPOSIT", category="Income"))
        self.assertIsNone(self.engine(txns=rows, name="zeroinc.db").surplus())

    def test_income_override_is_used_verbatim(self):
        eng = self.engine()
        self.assertAlmostEqual(eng.surplus(income_override=1000.0),
                               1000.0 - MONTHLY_AVG, places=2)

    def test_income_override_works_even_without_income_rows(self):
        rows = [txn(d, amt, desc, category=cat) for d, amt, desc, cat in SPEND_ROWS]
        eng = self.engine(txns=rows, name="ovr.db")
        self.assertIsNotNone(eng.surplus(income_override=500.0))


class MerchantAndRecurringTests(EngineTestCase):

    def test_merchants_group_by_key_and_sort_by_total(self):
        got = self.engine().merchants()
        by_name = {m["name"]: m for m in got}
        self.assertAlmostEqual(by_name["WHOLE FOODS MKT"]["total"], 550.0, places=2)
        self.assertEqual(by_name["WHOLE FOODS MKT"]["n"], 3)
        self.assertEqual(by_name["WHOLE FOODS MKT"]["category"], "Groceries")
        self.assertAlmostEqual(by_name["WHOLE FOODS MKT"]["avg"], 550.0 / 3, places=2)
        self.assertEqual(by_name["WHOLE FOODS MKT"]["last"], date(2026, 3, 2))
        self.assertEqual([m["total"] for m in got],
                         sorted((m["total"] for m in got), reverse=True))

    def test_merchants_never_include_non_spend_rows(self):
        names = {m["name"] for m in self.engine().merchants()}
        self.assertNotIn("ATM WITHDRAWAL", names)

    def test_recurring_finds_the_subscription_and_nothing_else(self):
        # NETFLIX is charged on the 9th of three consecutive months at a flat
        # price. The groceries are the same merchant but wildly different
        # amounts, so they are not a subscription.
        found = self.engine().recurring()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "NETFLIX.COM")
        self.assertEqual(found[0].cadence, "monthly")
        self.assertAlmostEqual(found[0].amount, 15.99, places=2)

    def test_recurring_is_cached(self):
        eng = self.engine()
        self.assertIs(eng.recurring(), eng.recurring())

    def test_committed_monthly_counts_only_charges_still_arriving(self):
        """A charge that stopped months ago is not a commitment.

        The fixture's subscription last billed well over the staleness slack
        ago, so it is either cancelled or sitting on a dead card. Counting it
        would shrink the surplus a savings plan is built from and understate
        what is genuinely spare. It is still reachable — via `include_stale`,
        and as its own figure — because a dead card means the charge returns the
        day it is replaced.
        """
        eng = self.engine()
        self.assertEqual(eng.committed_monthly(), 0.0)
        self.assertAlmostEqual(
            eng.committed_monthly(include_stale=True), 15.99, places=2)
        self.assertAlmostEqual(eng.stale_monthly(), 15.99, places=2)
        self.assertEqual(eng.active_subscriptions(), [])
        self.assertEqual(len(eng.subscriptions()), 1)

    def test_committed_monthly_counts_a_subscription_that_is_still_live(self):
        from datetime import date, timedelta

        # Anchored to today so the charge stays current as the clock moves.
        today = date.today()
        rows = [
            txn((today - timedelta(days=30 * i)).isoformat(), 15.99, "STREAMER",
                category="Subscriptions")
            for i in range(5)
        ]
        eng = self.engine(txns=rows, name="live.db")
        self.assertAlmostEqual(eng.committed_monthly(), 15.99, places=1)
        self.assertEqual(eng.stale_monthly(), 0.0)
        self.assertEqual(len(eng.active_subscriptions()), 1)

    def test_committed_monthly_is_zero_with_nothing_recurring(self):
        rows = [txn("2026-01-02", 10.0, "ONE OFF A", category="Dining"),
                txn("2026-02-14", 90.0, "ONE OFF B", category="Dining"),
                txn("2026-03-20", 40.0, "ONE OFF C", category="Dining")]
        self.assertEqual(self.engine(txns=rows, name="norec.db").committed_monthly(), 0.0)


class CoverageTests(EngineTestCase):

    def statements(self):
        return [
            stmt("amex-jan.pdf", issuer="Amex", account_last4="1001",
                 period_start="2026-01-01", period_end="2026-01-31",
                 purchases=315.99, charges_extracted=315.99,
                 reconciled=1, reconcile_diff=0.0),
            stmt("amex-feb.pdf", issuer="Amex", account_last4="1001",
                 period_start="2026-02-01", period_end="2026-02-28",
                 purchases=586.49, charges_extracted=465.99,
                 reconciled=0, reconcile_diff=-120.50,
                 reconcile_note="extracted charges short of printed purchases"),
            stmt("amex-apr.pdf", issuer="Amex", account_last4="1001",
                 period_start="2026-04-10", period_end="2026-05-09",
                 reconciled=1, reconcile_diff=0.0),
            stmt("disc-jan.pdf", issuer="Discover", account_last4="2336",
                 period_start="2026-01-05", period_end="2026-02-04",
                 reconciled=None, reconcile_note="no purchases total printed"),
        ]

    def test_counts_by_reconciliation_state(self):
        cov = self.engine(stmts=self.statements(), name="cov.db").coverage()
        self.assertEqual(cov["statements"], 4)
        self.assertEqual(cov["reconciled"], 2)
        self.assertEqual(cov["mismatched"], 1)
        self.assertEqual(cov["unchecked"], 1)
        self.assertAlmostEqual(cov["pct"], 50.0, places=6)

    def test_the_three_states_partition_the_statements(self):
        cov = self.engine(stmts=self.statements(), name="cov2.db").coverage()
        self.assertEqual(cov["reconciled"] + cov["mismatched"] + cov["unchecked"],
                         cov["statements"])

    def test_mismatches_are_listed_worst_first(self):
        cov = self.engine(stmts=self.statements(), name="cov3.db").coverage()
        self.assertEqual([s.source_file for s in cov["mismatches"]], ["amex-feb.pdf"])
        diffs = [abs(s.reconcile_diff or 0) for s in cov["mismatches"]]
        self.assertEqual(diffs, sorted(diffs, reverse=True))

    def test_gaps_between_periods_on_the_same_account(self):
        cov = self.engine(stmts=self.statements(), name="cov4.db").coverage()
        self.assertEqual(len(cov["gaps"]), 1)
        gap = cov["gaps"][0]
        self.assertEqual(gap["account"], "Amex 1001")
        self.assertEqual(gap["after"], "2026-02-28")
        self.assertEqual(gap["before"], "2026-04-10")
        self.assertEqual(gap["days"], 41)

    def test_a_single_statement_per_account_produces_no_gap(self):
        one = [stmt("disc-jan.pdf", issuer="Discover", account_last4="2336",
                    period_start="2026-01-05", period_end="2026-02-04",
                    reconciled=1)]
        cov = self.engine(stmts=one, name="cov5.db").coverage()
        self.assertEqual(cov["gaps"], [])

    def test_trusted_only_when_everything_reconciles_and_nothing_is_missing(self):
        clean = [stmt("a.pdf", issuer="Amex", account_last4="1001",
                      period_start="2026-01-01", period_end="2026-01-31", reconciled=1),
                 stmt("b.pdf", issuer="Amex", account_last4="1001",
                      period_start="2026-02-01", period_end="2026-02-28", reconciled=1)]
        self.assertTrue(self.engine(stmts=clean, name="cov6.db").coverage()["trusted"])

        dirty = clean + [stmt("c.pdf", issuer="Amex", account_last4="1001",
                              period_start="2026-03-01", period_end="2026-03-31",
                              reconciled=0, reconcile_diff=5.0)]
        self.assertFalse(self.engine(stmts=dirty, name="cov7.db").coverage()["trusted"])

    def test_a_gap_alone_breaks_trust(self):
        gappy = [stmt("a.pdf", issuer="Amex", account_last4="1001",
                      period_start="2026-01-01", period_end="2026-01-31", reconciled=1),
                 stmt("b.pdf", issuer="Amex", account_last4="1001",
                      period_start="2026-05-01", period_end="2026-05-31", reconciled=1)]
        cov = self.engine(stmts=gappy, name="cov8.db").coverage()
        self.assertEqual(cov["reconciled"], 2)
        self.assertTrue(cov["gaps"])
        self.assertFalse(cov["trusted"])

    def test_no_statements_is_not_trusted(self):
        cov = self.engine(name="cov9.db").coverage()
        self.assertEqual(cov["statements"], 0)
        self.assertEqual(cov["pct"], 0.0)
        self.assertFalse(cov["trusted"])

    def test_statements_without_period_bounds_produce_no_gaps(self):
        old = [stmt("a.pdf", issuer="Amex", account_last4="1001", reconciled=1),
               stmt("b.pdf", issuer="Amex", account_last4="1001", reconciled=1)]
        cov = self.engine(stmts=old, name="cov10.db").coverage()
        self.assertEqual(cov["gaps"], [])
        self.assertTrue(cov["trusted"])


class LoadingTests(EngineTestCase):

    def test_load_transactions_from_a_db(self):
        path = self.tmp_path("load.db")
        with FinanceDB(path) as db:
            db.upsert_transactions(fixture_transactions())
        self.assertEqual(len(load_transactions(path)), len(fixture_transactions()))

    def test_load_transactions_from_a_csv(self):
        path, csv_path = self.tmp_path("load2.db"), self.tmp_path("t.csv")
        with FinanceDB(path) as db:
            db.upsert_transactions(fixture_transactions())
            db.export_csv(csv_path)
        self.assertEqual(load_transactions(csv_path), load_transactions(path))

    def test_load_transactions_exits_on_a_missing_source(self):
        with self.assertRaises(SystemExit):
            load_transactions(self.tmp_path("nope.db"))

    def test_load_transactions_exits_on_an_empty_csv(self):
        p = self.tmp_path("empty.csv")
        with open(p, "w", newline="") as fh:
            fh.write("date,description,amount\n")
        with self.assertRaises(SystemExit):
            load_transactions(p)

    def test_from_source_handles_both_shapes(self):
        path, csv_path = self.tmp_path("src.db"), self.tmp_path("src.csv")
        with FinanceDB(path) as db:
            db.upsert_transactions(fixture_transactions())
            db.export_csv(csv_path)
        with Engine.from_source(path) as a, Engine.from_source(csv_path) as b:
            self.assertAlmostEqual(a.total_spend(), b.total_spend(), places=2)
            self.assertEqual(a.by_month(), b.by_month())
            # A CSV carries no statement summaries, so coverage is empty.
            self.assertEqual(b.statements(), [])
            self.assertEqual(b.coverage()["statements"], 0)

    def test_close_is_idempotent(self):
        eng = self.engine(name="close.db")
        eng.close()
        eng.close()

    def test_month_key_accepts_a_string_or_a_date(self):
        self.assertEqual(month_key("2026-01-05"), "2026-01")
        self.assertEqual(month_key(date(2026, 1, 5)), "2026-01")


class ForecastTests(EngineTestCase):

    def test_spending_forecast_averages_the_trailing_window(self):
        eng = self.engine()
        self.assertAlmostEqual(SpendingForecast(eng, window=2).next_month(),
                               (FEB + MAR) / 2, places=2)
        self.assertAlmostEqual(SpendingForecast(eng, window=99).next_month(),
                               TOTAL_SPEND / 3, places=2)

    def test_spending_forecast_projects_a_flat_line(self):
        eng = self.engine()
        fc = SpendingForecast(eng, window=2)
        self.assertEqual(fc.project(3), [fc.next_month()] * 3)

    def test_spending_forecast_of_an_empty_store_is_zero(self):
        self.assertEqual(
            SpendingForecast(self.engine(txns=[], name="empty4.db")).next_month(), 0.0)

    def test_savings_forecast_matches_future_value(self):
        self.assertEqual(SavingsForecast(1000.0).after(100.0, 12), 2200.0)
        self.assertAlmostEqual(SavingsForecast(1000.0, 5.0).after(0.0, 12),
                               1050.0, places=6)


if __name__ == "__main__":
    unittest.main()
