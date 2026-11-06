"""
Unit tests for utils.py — the shared money/date/merchant/recurring primitives.

These are the highest-value tests in the project. Every dollar figure any tool
prints has passed through ``parse_money``; every month bucket through
``parse_date``; every merchant rollup through ``merchant_key``. A silent
regression here is a wrong number on every screen, so this file leans on
known-answer cases rather than on whatever the functions happen to do today.

``detect_recurring`` gets the most attention because it is new and it replaced
three divergent implementations. The synthetic fixtures below encode the
behaviours those old copies disagreed about, most importantly a subscription
whose billing day drifts across the month.
"""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

import support  # noqa: F401  (puts the repo root on sys.path)

from finances.utils import (Recurring, detect_recurring, file_sha256, fmt_money,
                   future_value, merchant_key, parse_date, parse_money,
                   payoff_months, read_csv_rows)


# --------------------------------------------------------------------------
# parse_money
# --------------------------------------------------------------------------

class ParseMoneyTests(unittest.TestCase):

    def test_plain_dollar_amount(self):
        self.assertEqual(parse_money("$1,234.56"), 1234.56)
        self.assertEqual(parse_money("1234.56"), 1234.56)
        self.assertEqual(parse_money("$0.00"), 0.0)

    def test_accountant_parentheses_are_negative(self):
        # (1,234.56) is how card statements print a credit in a debit column.
        self.assertEqual(parse_money("($1,234.56)"), -1234.56)
        self.assertEqual(parse_money("(1,234.56)"), -1234.56)

    def test_trailing_minus_is_negative(self):
        self.assertEqual(parse_money("1,234.56-"), -1234.56)

    def test_leading_minus_is_negative(self):
        self.assertEqual(parse_money("-1,234.56"), -1234.56)
        self.assertEqual(parse_money("-$1,234.56"), -1234.56)

    def test_empty_and_blank_are_none(self):
        self.assertIsNone(parse_money(""))
        self.assertIsNone(parse_money("   "))
        self.assertIsNone(parse_money(None))

    def test_garbage_is_none_not_zero(self):
        # Returning 0.0 here would silently swallow a failed extraction, so the
        # distinction between "no number" and "zero dollars" has to survive.
        for junk in ("n/a", "--", "$", "TOTAL", "abc"):
            with self.subTest(junk=junk):
                self.assertIsNone(parse_money(junk))

    def test_malformed_number_is_none(self):
        self.assertIsNone(parse_money("1.2.3"))

    def test_numeric_input_passes_through(self):
        self.assertEqual(parse_money(12.5), 12.5)
        self.assertEqual(parse_money(-12.5), -12.5)

    def test_european_decimal_comma_is_out_of_scope(self):
        # These statements are all US-issued, so "1.234,56" is not a shape the
        # parser claims to handle. Assert only that it degrades to a number
        # rather than raising; see tests/README.md for the limitation.
        self.assertIsInstance(parse_money("1.234,56"), float)

    def test_minus_is_read_on_either_side_of_the_currency_symbol(self):
        """Statements print the sign before or after the $; both are negative.

        This was a real defect: the sign was read from the first and last
        character of the raw string, so the minus in "$-45.00" was stripped and
        the value came back positive while "-$45.00" parsed correctly. A dropped
        sign turns a refund into a charge, which is the class of error the
        reconciliation work exists to catch.
        """
        for raw in ("$-45.00", "-$45.00", "-45.00", "45.00-", "(45.00)"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_money(raw), -45.0)
        self.assertEqual(parse_money("$45.00"), 45.0)


class FmtMoneyTests(unittest.TestCase):

    def test_default_is_whole_dollars_with_separators(self):
        self.assertEqual(fmt_money(1234.56), "$1,235")
        self.assertEqual(fmt_money(1234.56, 2), "$1,234.56")

    def test_negative_keeps_its_sign(self):
        self.assertEqual(fmt_money(-1234.5, 2), "$-1,234.50")

    def test_round_trips_with_parse_money(self):
        for v in (0.0, 12.34, 1234.56, 1_000_000.0):
            with self.subTest(v=v):
                self.assertAlmostEqual(parse_money(fmt_money(v, 2)), v, places=2)


# --------------------------------------------------------------------------
# parse_date
# --------------------------------------------------------------------------

class ParseDateTests(unittest.TestCase):

    def test_month_day_needs_a_default_year(self):
        self.assertEqual(parse_date("05/14", 2024), date(2024, 5, 14))
        self.assertIsNone(parse_date("05/14"))

    def test_full_numeric_date(self):
        self.assertEqual(parse_date("05/14/2024"), date(2024, 5, 14))
        self.assertEqual(parse_date("05-14-2024"), date(2024, 5, 14))

    def test_two_digit_year_is_2000s(self):
        self.assertEqual(parse_date("05/14/24"), date(2024, 5, 14))
        self.assertEqual(parse_date("01/02/99"), date(2099, 1, 2))

    def test_explicit_year_beats_default_year(self):
        self.assertEqual(parse_date("05/14/2024", 1999), date(2024, 5, 14))

    def test_month_name_first(self):
        self.assertEqual(parse_date("May 14 2024"), date(2024, 5, 14))
        self.assertEqual(parse_date("May 14, 2024"), date(2024, 5, 14))
        self.assertEqual(parse_date("Sept. 3 2024"), date(2024, 9, 3))
        self.assertEqual(parse_date("May 14", 2024), date(2024, 5, 14))

    def test_day_first(self):
        self.assertEqual(parse_date("14 May 2024"), date(2024, 5, 14))
        self.assertEqual(parse_date("14 May", 2024), date(2024, 5, 14))

    def test_month_names_are_case_insensitive(self):
        self.assertEqual(parse_date("MAY 14 2024"), date(2024, 5, 14))
        self.assertEqual(parse_date("may 14 2024"), date(2024, 5, 14))

    def test_impossible_calendar_date_is_none(self):
        # 02/30 must not silently roll into March.
        self.assertIsNone(parse_date("02/30/2024"))
        self.assertIsNone(parse_date("Feb 30 2024"))
        self.assertIsNone(parse_date("13/45/2024"))

    def test_leap_day(self):
        self.assertEqual(parse_date("02/29/2024"), date(2024, 2, 29))
        self.assertIsNone(parse_date("02/29/2023"))

    def test_unparseable_is_none(self):
        for junk in ("", "garbage", "Balance", "1234567", "05/", "//"):
            with self.subTest(junk=junk):
                self.assertIsNone(parse_date(junk, 2024))

    def test_unknown_month_name_is_none(self):
        self.assertIsNone(parse_date("Xyz 14 2024"))


# --------------------------------------------------------------------------
# merchant_key
# --------------------------------------------------------------------------

class MerchantKeyTests(unittest.TestCase):

    def test_same_store_different_branch_collapses(self):
        a = merchant_key("SQ *BLUE BOTTLE COFFEE OAKLAND CA")
        b = merchant_key("SQ *BLUE BOTTLE COFFEE #123 SF CA")
        self.assertEqual(a, b)
        self.assertEqual(a, "BLUE BOTTLE COFFEE")

    def test_pos_prefix_and_store_number_are_stripped(self):
        self.assertEqual(merchant_key("POS PURCHASE WHOLE FOODS 10432"),
                         merchant_key("WHOLE FOODS 88811"))
        self.assertEqual(merchant_key("POS PURCHASE WHOLE FOODS 10432"),
                         "WHOLE FOODS")

    def test_authorisation_code_is_stripped(self):
        # An 8+ character token carrying digits is an auth/reference code, not
        # part of the name, and must not fork the group.
        self.assertEqual(merchant_key("UBER TRIP AB12CD34 SF"),
                         merchant_key("UBER TRIP XY98ZW76 SF"))

    def test_amazon_variants_normalise(self):
        self.assertTrue(merchant_key("AMZN Mktp US*2X4B9").startswith("AMAZON"))
        self.assertTrue(merchant_key("AMAZON MKTPL*ABC").startswith("AMAZON"))

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(merchant_key("trader joes #455"),
                         merchant_key("TRADER JOES 455"))
        self.assertEqual(merchant_key("trader joes #455"), "TRADER JOES")

    def test_pseudonymised_input_passes_through_unchanged(self):
        # anonymize.py hands back keys that are already canonical; re-keying
        # them would destroy the mapping.
        for name in ("MERCHANT_12", "GROCERY_3", "OTHER (DINING)", "CARD_4"):
            with self.subTest(name=name):
                self.assertEqual(merchant_key(name), name)

    def test_key_is_at_most_three_tokens(self):
        self.assertLessEqual(
            len(merchant_key("ALPHA BRAVO CHARLIE DELTA ECHO FOXTROT").split()), 3)

    def test_never_returns_empty(self):
        # A key of "" would merge unrelated rows into one bucket.
        for name in ("SQ", "POS", "US", "123456"):
            with self.subTest(name=name):
                self.assertTrue(merchant_key(name).strip())


# --------------------------------------------------------------------------
# Debt and savings math
# --------------------------------------------------------------------------

class PayoffMonthsTests(unittest.TestCase):

    def test_known_answer(self):
        # $1,000 at 20% APR paying $100/mo. Closed form:
        #   r = 0.20/12; n = ln(P / (P - B*r)) / ln(1+r)
        r = 0.20 / 12
        expected = math.log(100 / (100 - 1000 * r)) / math.log(1 + r)
        self.assertAlmostEqual(payoff_months(1000, 20, 100), expected, places=6)
        self.assertAlmostEqual(payoff_months(1000, 20, 100), 11.0302, places=3)

    def test_a_bigger_payment_clears_sooner(self):
        self.assertLess(payoff_months(5000, 22, 500), payoff_months(5000, 22, 250))

    def test_a_higher_apr_takes_longer(self):
        self.assertGreater(payoff_months(5000, 29, 250), payoff_months(5000, 12, 250))

    def test_payment_below_interest_never_clears(self):
        # $10,000 at 24% accrues $200/mo; $100/mo never catches it.
        self.assertIsNone(payoff_months(10_000, 24, 100))

    def test_payment_exactly_equal_to_interest_never_clears(self):
        self.assertIsNone(payoff_months(10_000, 24, 200))

    def test_missing_or_nonsense_inputs_are_none(self):
        self.assertIsNone(payoff_months(0, 20, 100))
        self.assertIsNone(payoff_months(1000, 0, 100))
        self.assertIsNone(payoff_months(1000, 20, 0))
        self.assertIsNone(payoff_months(1000, 20, -50))
        self.assertIsNone(payoff_months(None, None, None))


class FutureValueTests(unittest.TestCase):

    def test_zero_apy_is_linear(self):
        self.assertEqual(future_value(0, 100, 12), 1200)
        self.assertEqual(future_value(500, 100, 12), 1700)
        self.assertEqual(future_value(500, 0, 12), 500)

    def test_zero_months_returns_the_starting_balance(self):
        self.assertEqual(future_value(1000, 100, 0), 1000)
        self.assertAlmostEqual(future_value(1000, 100, 0, 5.0), 1000.0, places=6)

    def test_lump_sum_compounds(self):
        # $1,000 for 12 months at 5% APY compounded monthly is exactly $1,050:
        # the monthly rate is (1.05)^(1/12) - 1, applied 12 times.
        self.assertAlmostEqual(future_value(1000, 0, 12, 5.0), 1050.0, places=6)

    def test_contributions_compound(self):
        # Hand-computed: r = 1.05^(1/12) - 1 = 0.0040741237836...
        #   1000*(1+r)^12 + 100*(((1+r)^12 - 1)/r)
        r = 1.05 ** (1 / 12) - 1
        expected = 1000 * (1 + r) ** 12 + 100 * (((1 + r) ** 12 - 1) / r)
        self.assertAlmostEqual(future_value(1000, 100, 12, 5.0), expected, places=6)
        self.assertAlmostEqual(future_value(1000, 100, 12, 5.0), 2277.2578, places=3)

    def test_interest_beats_no_interest(self):
        self.assertGreater(future_value(1000, 100, 24, 4.5),
                           future_value(1000, 100, 24, 0))


# --------------------------------------------------------------------------
# detect_recurring
# --------------------------------------------------------------------------

def row(d: date, amount: float, merchant: str, category: str = "Subscriptions") -> dict:
    """A dict row in the shape detect_recurring accepts from any caller."""
    return {"date": d.isoformat(), "amount": amount,
            "merchant": merchant, "category": category}


def monthly_series(merchant, amount, months, day=15, year=2026, start_month=1):
    return [row(date(year, start_month + i, day), amount, merchant)
            for i in range(months)]


class DetectRecurringTests(unittest.TestCase):

    def only(self, items, **kw):
        """Run the detector and assert it found exactly one subscription."""
        found = detect_recurring(items, **kw)
        self.assertEqual(len(found), 1, f"expected one recurring charge, got {found}")
        return found[0]

    # -- the happy path --------------------------------------------------
    def test_clean_monthly_subscription_is_found(self):
        r = self.only(monthly_series("NETFLIX", 15.99, 7))
        self.assertEqual(r.cadence, "monthly")
        self.assertEqual(r.n, 7)
        self.assertAlmostEqual(r.amount, 15.99)
        self.assertEqual(r.day_of_month, 15)
        self.assertEqual(r.first_seen, date(2026, 1, 15))
        self.assertEqual(r.last_seen, date(2026, 7, 15))
        self.assertAlmostEqual(r.per_year, 15.99 * 12, places=6)
        self.assertAlmostEqual(r.per_month, 15.99, places=6)
        self.assertGreater(r.confidence, 0.8)

    def test_annual_and_monthly_aliases_exist(self):
        # analyze.py, planner.py and the auditor each used one of these names.
        r = self.only(monthly_series("NETFLIX", 10.0, 6))
        self.assertEqual(r.annual, r.per_year)
        self.assertEqual(r.monthly, r.per_month)

    # -- the case the old implementations got wrong ----------------------
    def test_drifting_billing_day_is_still_monthly(self):
        """A subscription billed near the start of the month lands on the 3rd,
        1st, 5th, 2nd... A pure day-gap test discards it. The calendar-month
        test must keep it."""
        days = [3, 1, 5, 2, 3, 4, 1]
        items = [row(date(2026, m, days[m - 1]), 9.99, "SPOTIFY")
                 for m in range(1, 8)]
        r = self.only(items)
        self.assertEqual(r.cadence, "monthly")
        self.assertEqual(r.n, 7)
        self.assertIn(r.day_of_month, range(1, 6))

    def test_drift_across_a_month_boundary_is_still_monthly(self):
        # Billing wanders from the 28th back to the 2nd of the next month.
        days = [(2026, 1, 28), (2026, 3, 2), (2026, 3, 30), (2026, 4, 28),
                (2026, 6, 1), (2026, 6, 29)]
        items = [row(date(*d), 12.0, "ADOBE") for d in days]
        found = detect_recurring(items)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].cadence, "monthly")

    # -- other cadences ---------------------------------------------------
    def test_weekly_charge_gets_weekly_cadence(self):
        start = date(2026, 1, 5)
        items = [row(start + timedelta(days=7 * i), 22.0, "CLASSPASS STUDIO")
                 for i in range(12)]
        r = self.only(items)
        self.assertEqual(r.cadence, "weekly")
        self.assertEqual(r.day_of_month, 0)      # meaningless for non-monthly
        self.assertAlmostEqual(r.per_year, 22.0 * 52, places=6)
        self.assertEqual(r.last_seen, date(2026, 3, 23))
        self.assertEqual(r.next_expected, date(2026, 3, 30))

    def test_biweekly_charge_gets_biweekly_cadence(self):
        start = date(2026, 1, 5)
        items = [row(start + timedelta(days=14 * i), 40.0, "GYM CLUB")
                 for i in range(8)]
        self.assertEqual(self.only(items).cadence, "biweekly")

    def test_quarterly_charge_gets_quarterly_cadence(self):
        items = [row(date(2025, m, 10), 60.0, "WATER UTILITY")
                 for m in (1, 4, 7, 10)]
        r = self.only(items)
        self.assertEqual(r.cadence, "quarterly")
        self.assertAlmostEqual(r.per_year, 240.0, places=6)

    def test_annual_charge_gets_annual_cadence(self):
        items = [row(date(2023, 3, 4), 119.0, "AMAZON PRIME MEMBER"),
                 row(date(2024, 3, 6), 119.0, "AMAZON PRIME MEMBER"),
                 row(date(2025, 3, 5), 119.0, "AMAZON PRIME MEMBER")]
        r = self.only(items)
        self.assertEqual(r.cadence, "annual")
        self.assertAlmostEqual(r.per_year, 119.0, places=6)
        self.assertAlmostEqual(r.per_month, 119.0 / 12, places=6)

    def test_results_are_sorted_by_annual_cost(self):
        items = (monthly_series("NETFLIX", 15.99, 6)
                 + monthly_series("ICLOUD PLUS", 2.99, 6, day=4)
                 + monthly_series("RENT PAYMENT", 1800.0, 6, day=1))
        found = detect_recurring(items)
        self.assertEqual([r.per_year for r in found],
                         sorted((r.per_year for r in found), reverse=True))
        self.assertEqual(found[0].key, merchant_key("RENT PAYMENT"))

    # -- what must NOT be reported ---------------------------------------
    def test_random_one_off_purchases_are_not_reported(self):
        names = ["HOME DEPOT", "SHELL OIL", "DELTA AIR LINES", "STEAM GAMES",
                 "CVS PHARMACY", "BEST BUY", "PETCO", "REI COOP"]
        items = [row(date(2026, 1, 1) + timedelta(days=11 * i), 10.0 + 7 * i, n)
                 for i, n in enumerate(names)]
        self.assertEqual(detect_recurring(items), [])

    def test_too_few_occurrences_is_not_reported(self):
        items = monthly_series("NETFLIX", 15.99, 2)
        self.assertEqual(detect_recurring(items), [])

    def test_too_short_a_span_is_not_reported(self):
        # Three charges inside one month is not evidence of a subscription.
        items = [row(date(2026, 1, d), 9.99, "NETFLIX") for d in (2, 12, 22)]
        self.assertEqual(detect_recurring(items), [])

    def test_wildly_varying_prices_are_not_reported(self):
        items = [row(date(2026, m, 15), 10.0 * m ** 2, "RANDOM SHOP")
                 for m in range(1, 8)]
        self.assertEqual(detect_recurring(items), [])

    def test_credits_and_payments_never_recur(self):
        # Amount <= 0 is money coming in; a monthly paycheque is not a
        # subscription and must not appear in committed spending.
        items = [row(date(2026, m, 1), -2500.0, "PAYROLL DEPOSIT", "Income")
                 for m in range(1, 8)]
        self.assertEqual(detect_recurring(items), [])

    def test_rows_without_a_usable_date_or_amount_are_skipped(self):
        items = monthly_series("NETFLIX", 15.99, 6) + [
            {"date": "", "amount": 15.99, "merchant": "NETFLIX"},
            {"date": "2026-08-15", "amount": None, "merchant": "NETFLIX"},
            {"date": "2026-09-15", "amount": 15.99, "merchant": ""},
        ]
        r = self.only(items)
        self.assertEqual(r.n, 6)

    # -- price drift ------------------------------------------------------
    def test_price_rise_is_reported(self):
        items = [row(date(2026, m, 10), 9.99 if m < 4 else 12.99, "HULU")
                 for m in range(1, 7)]
        r = self.only(items)
        self.assertAlmostEqual(r.price_change, 3.0, places=2)
        self.assertAlmostEqual(r.price_pct, 30.03, places=1)

    def test_a_steady_price_reports_no_drift(self):
        r = self.only(monthly_series("NETFLIX", 15.99, 7))
        self.assertAlmostEqual(r.price_change, 0.0, places=6)
        self.assertAlmostEqual(r.price_pct, 0.0, places=6)

    def test_price_cut_is_negative(self):
        items = [row(date(2026, m, 10), 12.0 if m < 4 else 10.0, "HULU")
                 for m in range(1, 7)]
        r = self.only(items)
        self.assertLess(r.price_change, 0)
        self.assertLess(r.price_pct, 0)

    # -- next_expected ----------------------------------------------------
    def test_next_expected_is_one_month_on(self):
        r = self.only(monthly_series("NETFLIX", 15.99, 6))
        self.assertEqual(r.last_seen, date(2026, 6, 15))
        self.assertEqual(r.next_expected, date(2026, 7, 15))

    def test_next_expected_clamps_at_a_short_month(self):
        """A charge on 31 January must not produce 31 February."""
        days = [(2025, 8, 31), (2025, 9, 30), (2025, 10, 31),
                (2025, 11, 30), (2025, 12, 31), (2026, 1, 31)]
        items = [row(date(*d), 10.0, "MONTH END BILL") for d in days]
        r = self.only(items)
        self.assertEqual(r.last_seen, date(2026, 1, 31))
        self.assertEqual(r.next_expected, date(2026, 2, 28))   # 2026 is not a leap year

    def test_next_expected_clamps_into_a_leap_february(self):
        days = [(2023, 8, 31), (2023, 9, 30), (2023, 10, 31),
                (2023, 11, 30), (2023, 12, 31), (2024, 1, 31)]
        items = [row(date(*d), 10.0, "MONTH END BILL") for d in days]
        self.assertEqual(self.only(items).next_expected, date(2024, 2, 29))

    def test_next_expected_rolls_over_the_year(self):
        items = monthly_series("NETFLIX", 15.99, 6, day=9, year=2026, start_month=7)
        r = self.only(items)
        self.assertEqual(r.last_seen, date(2026, 12, 9))
        self.assertEqual(r.next_expected, date(2027, 1, 9))

    def test_next_expected_is_always_after_last_seen(self):
        for items in (monthly_series("A SERVICE", 5.0, 6),
                      [row(date(2026, 1, 5) + timedelta(days=7 * i), 3.0, "B SERVICE")
                       for i in range(10)]):
            with self.subTest(cadence=len(items)):
                r = self.only(items)
                self.assertGreater(r.next_expected, r.last_seen)

    # -- staleness --------------------------------------------------------
    def test_is_stale_fires_once_overdue(self):
        r = self.only(monthly_series("NETFLIX", 15.99, 6))
        self.assertEqual(r.next_expected, date(2026, 7, 15))
        self.assertFalse(r.is_stale(date(2026, 7, 20)))         # not yet due-ish
        self.assertFalse(r.is_stale(date(2026, 8, 20)))         # inside 45d slack
        self.assertTrue(r.is_stale(date(2026, 9, 30)))          # well overdue
        self.assertTrue(r.is_stale(date(2026, 8, 1), slack_days=5))

    def test_is_stale_is_false_without_a_next_expected(self):
        r = Recurring(key="k", name="k", category="c", cadence="monthly",
                      amount=1.0, n=3, first_seen=date(2026, 1, 1),
                      last_seen=date(2026, 3, 1), day_of_month=1,
                      next_expected=None)
        self.assertFalse(r.is_stale(date(2030, 1, 1)))

    # -- input shapes -----------------------------------------------------
    def test_accepts_transaction_objects(self):
        from finances.models import Transaction
        items = [Transaction(date=f"2026-0{m}-15", description="NETFLIX.COM",
                             amount=15.99, category="Subscriptions")
                 for m in range(1, 7)]
        r = self.only(items)
        self.assertEqual(r.cadence, "monthly")
        self.assertEqual(r.category, "Subscriptions")

    def test_accepts_a_custom_key_function(self):
        # Two descriptors for what is really one biller, alternating months.
        # Keyed by name they look like two sparse charges; keyed together they
        # are one monthly subscription.
        items = ([row(date(2026, m, 15), 10.0, "STORE ALPHA") for m in (1, 3, 5)]
                 + [row(date(2026, m, 15), 10.0, "STORE BRAVO") for m in (2, 4, 6)])
        self.assertEqual(len(detect_recurring(items)), 2)
        merged = detect_recurring(items, key=lambda it: "ONE BILLER")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].n, 6)
        self.assertEqual(merged[0].key, "ONE BILLER")
        self.assertEqual(merged[0].cadence, "monthly")

    def test_thresholds_are_honoured(self):
        items = monthly_series("NETFLIX", 15.99, 4)
        self.assertEqual(detect_recurring(items, min_occurrences=5), [])
        self.assertEqual(detect_recurring(items, min_span_days=400), [])

    def test_empty_input(self):
        self.assertEqual(detect_recurring([]), [])


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------

class FileHelperTests(support.TempDirMixin, unittest.TestCase):

    def test_sha256_is_content_addressed(self):
        a, b, c = (self.tmp_path(n) for n in ("a.txt", "b.txt", "c.txt"))
        for p, data in ((a, b"hello"), (b, b"hello"), (c, b"world")):
            with open(p, "wb") as fh:
                fh.write(data)
        self.assertEqual(file_sha256(a), file_sha256(b))
        self.assertNotEqual(file_sha256(a), file_sha256(c))
        self.assertEqual(
            file_sha256(a),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_sha256_handles_a_file_larger_than_one_chunk(self):
        p = self.tmp_path("big.bin")
        with open(p, "wb") as fh:
            fh.write(b"x" * (3 * (1 << 20) + 17))
        self.assertEqual(len(file_sha256(p)), 64)

    def test_read_csv_rows(self):
        p = self.tmp_path("rows.csv")
        with open(p, "w", newline="") as fh:
            fh.write("date,amount\n2026-01-01,5\n2026-01-02,6\n")
        rows = read_csv_rows(p, required=("date", "amount"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], "5")

    def test_read_csv_rows_exits_on_a_missing_column(self):
        p = self.tmp_path("rows.csv")
        with open(p, "w", newline="") as fh:
            fh.write("date\n2026-01-01\n")
        with self.assertRaises(SystemExit):
            read_csv_rows(p, required=("date", "amount"))

    def test_read_csv_rows_exits_on_an_empty_file(self):
        p = self.tmp_path("empty.csv")
        with open(p, "w", newline="") as fh:
            fh.write("date,amount\n")
        with self.assertRaises(SystemExit):
            read_csv_rows(p)


if __name__ == "__main__":
    unittest.main()
