"""
Unit tests for models.py — the row schema every tool shares.

Transaction and Statement are the contract between the parser, the store and
the four analysis tools. The interesting behaviour is all in the boundaries:
what happens when a CSV is missing a column an older writer never emitted, what
``__post_init__`` fills in, and what the dedup key is made of, because that key
is what stops a statement parsed twice from doubling the month's spending.
"""

from __future__ import annotations

import unittest
from dataclasses import fields

import support  # noqa: F401  (puts the repo root on sys.path)

from finances.models import Statement, Transaction


class TransactionColumnsTests(unittest.TestCase):

    def test_columns_matches_the_dataclass_fields(self):
        self.assertEqual(Transaction.columns(), [f.name for f in fields(Transaction)])

    def test_columns_starts_with_the_required_three(self):
        self.assertEqual(Transaction.columns()[:3], ["date", "description", "amount"])

    def test_to_row_covers_every_column(self):
        t = Transaction(date="2026-01-05", description="X", amount=1.0)
        self.assertEqual(sorted(t.to_row()), sorted(Transaction.columns()))


class TransactionPostInitTests(unittest.TestCase):

    def test_direction_defaults_from_the_amount_sign(self):
        # OUT is positive, IN is negative. This convention is load-bearing.
        self.assertEqual(Transaction("2026-01-05", "X", 10.0).direction, "out")
        self.assertEqual(Transaction("2026-01-05", "X", -10.0).direction, "in")

    def test_zero_is_treated_as_money_in(self):
        # amount > 0 is the only test, so a $0.00 line is "in", not "out". It
        # matters that this is deterministic: engine.spend() filters on > 0.
        self.assertEqual(Transaction("2026-01-05", "X", 0.0).direction, "in")

    def test_an_explicit_direction_is_not_overwritten(self):
        t = Transaction("2026-01-05", "X", 10.0, direction="in")
        self.assertEqual(t.direction, "in")

    def test_merchant_defaults_to_the_description(self):
        t = Transaction("2026-01-05", "SQ *BLUE BOTTLE", 10.0)
        self.assertEqual(t.merchant, "SQ *BLUE BOTTLE")

    def test_an_explicit_merchant_is_not_overwritten(self):
        t = Transaction("2026-01-05", "SQ *BLUE BOTTLE", 10.0, merchant="BLUE BOTTLE")
        self.assertEqual(t.merchant, "BLUE BOTTLE")

    def test_category_defaults_to_uncategorized(self):
        self.assertEqual(Transaction("2026-01-05", "X", 1.0).category, "Uncategorized")


class TransactionFromRowTests(unittest.TestCase):

    def test_minimal_row_with_every_optional_column_missing(self):
        t = Transaction.from_row({"date": "2026-01-05", "amount": "12.34"})
        self.assertEqual(t.date, "2026-01-05")
        self.assertEqual(t.amount, 12.34)
        self.assertEqual(t.description, "")
        self.assertEqual(t.category, "Uncategorized")
        self.assertEqual(t.direction, "out")
        self.assertEqual(t.post_date, "")
        self.assertEqual(t.issuer, "")
        self.assertEqual(t.account_last4, "")
        self.assertEqual(t.statement_period, "")
        self.assertEqual(t.source_file, "")

    def test_amount_is_coerced_from_a_string(self):
        self.assertIsInstance(
            Transaction.from_row({"date": "2026-01-05", "amount": "12.34"}).amount,
            float)

    def test_a_timestamp_date_is_truncated_to_the_day(self):
        t = Transaction.from_row({"date": "2026-01-05 00:00:00", "amount": "1"})
        self.assertEqual(t.date, "2026-01-05")

    def test_merchant_falls_back_to_the_description(self):
        t = Transaction.from_row({"date": "2026-01-05", "amount": "1",
                                  "description": "  SQ *CAFE  "})
        self.assertEqual(t.merchant, "SQ *CAFE")

    def test_an_empty_category_becomes_uncategorized(self):
        t = Transaction.from_row({"date": "2026-01-05", "amount": "1", "category": ""})
        self.assertEqual(t.category, "Uncategorized")

    def test_full_row_round_trips_through_to_row(self):
        t = Transaction(date="2026-01-05", description="SQ *CAFE", amount=4.5,
                        merchant="CAFE", category="Dining", direction="out",
                        post_date="2026-01-06", issuer="Amex",
                        account_last4="1234", statement_period="Jan 2026",
                        source_file="amex.pdf")
        self.assertEqual(Transaction.from_row(t.to_row()), t)

    def test_a_missing_required_column_raises(self):
        # A row with no date or no amount is not a transaction; failing loudly
        # here beats writing a row of zeros into the store.
        with self.assertRaises(KeyError):
            Transaction.from_row({"amount": "1"})
        with self.assertRaises(KeyError):
            Transaction.from_row({"date": "2026-01-05"})


class TransactionDerivedTests(unittest.TestCase):

    def test_dedup_key_fields(self):
        t = Transaction(date="2026-01-05", description="SQ *CAFE", amount=4.499,
                        account_last4="1234", source_file="a.pdf")
        self.assertEqual(t.dedup_key, ("2026-01-05", "SQ *CAFE", 4.5, "1234", 0))

    def test_dedup_key_separates_genuinely_repeated_charges(self):
        """seq is what tells a double parse apart from a charge that repeated.

        Without it the key collapsed two identical same-day fares into one row,
        and one statement in the sample data lost 41 of its 181 transactions on
        write with no warning.
        """
        a = Transaction("2026-01-05", "PATH TAP", 3.0, account_last4="1234")
        b = Transaction("2026-01-05", "PATH TAP", 3.0, account_last4="1234", seq=1)
        self.assertNotEqual(a.dedup_key, b.dedup_key)

    def test_dedup_key_ignores_the_source_file(self):
        # The same charge parsed out of two exports is one charge.
        a = Transaction("2026-01-05", "SQ *CAFE", 4.5, account_last4="1234",
                        source_file="one.pdf")
        b = Transaction("2026-01-05", "SQ *CAFE", 4.5, account_last4="1234",
                        source_file="two.pdf", category="Dining")
        self.assertEqual(a.dedup_key, b.dedup_key)

    def test_dedup_key_separates_two_identical_charges_on_different_cards(self):
        a = Transaction("2026-01-05", "SQ *CAFE", 4.5, account_last4="1234")
        b = Transaction("2026-01-05", "SQ *CAFE", 4.5, account_last4="9999")
        self.assertNotEqual(a.dedup_key, b.dedup_key)

    def test_dedup_key_rounds_to_cents(self):
        a = Transaction("2026-01-05", "X", 4.5000001, account_last4="1")
        b = Transaction("2026-01-05", "X", 4.4999999, account_last4="1")
        self.assertEqual(a.dedup_key, b.dedup_key)

    def test_d_parses_an_iso_date(self):
        from datetime import date
        self.assertEqual(Transaction("2026-01-05", "X", 1.0).d, date(2026, 1, 5))

    def test_d_is_none_for_a_malformed_date(self):
        for bad in ("not-a-date", "", "2026-13-45", "01/05/2026", "2026-01"):
            with self.subTest(bad=bad):
                self.assertIsNone(Transaction(bad, "X", 1.0).d)

    def test_month_is_the_first_seven_characters(self):
        self.assertEqual(Transaction("2026-01-05", "X", 1.0).month, "2026-01")


class StatementTests(unittest.TestCase):

    def test_columns_matches_the_dataclass_fields(self):
        self.assertEqual(Statement.columns(), [f.name for f in fields(Statement)])

    def test_only_source_file_is_required(self):
        s = Statement(source_file="a.pdf")
        self.assertEqual(s.issuer, "")
        self.assertIsNone(s.purchases)
        self.assertIsNone(s.reconciled)
        self.assertEqual(s.period_start, "")
        self.assertEqual(s.reconcile_note, "")

    def test_from_row_coerces_floats(self):
        s = Statement.from_row({"source_file": "a.pdf", "purchases": "123.45",
                                "new_balance": "-10", "credit_limit": 5000})
        self.assertEqual(s.purchases, 123.45)
        self.assertEqual(s.new_balance, -10.0)
        self.assertEqual(s.credit_limit, 5000.0)
        self.assertIsInstance(s.credit_limit, float)

    def test_from_row_coerces_ints(self):
        s = Statement.from_row({"source_file": "a.pdf", "reconciled": "1"})
        self.assertEqual(s.reconciled, 1)
        self.assertIsInstance(s.reconciled, int)

    def test_from_row_leaves_unparseable_numbers_at_their_default(self):
        s = Statement.from_row({"source_file": "a.pdf", "purchases": "n/a",
                                "reconciled": "maybe"})
        self.assertIsNone(s.purchases)
        self.assertIsNone(s.reconciled)

    def test_from_row_treats_empty_string_and_none_as_absent(self):
        s = Statement.from_row({"source_file": "a.pdf", "purchases": "",
                                "payments": None, "issuer": ""})
        self.assertIsNone(s.purchases)
        self.assertIsNone(s.payments)
        self.assertEqual(s.issuer, "")

    def test_from_row_keeps_a_genuine_zero(self):
        # "the statement charged no fees" and "we could not find the fee line"
        # are different facts and must not collapse.
        s = Statement.from_row({"source_file": "a.pdf", "fees_charged": 0.0,
                                "reconciled": 0})
        self.assertEqual(s.fees_charged, 0.0)
        self.assertEqual(s.reconciled, 0)

    def test_from_row_tolerates_a_store_without_the_new_columns(self):
        # An older DB has no period_start/period_end/reconcile_* columns at all.
        legacy = {"source_file": "a.pdf", "issuer": "Amex", "account_last4": "1001",
                  "statement_period": "Jan 10 - Feb 6 2026", "purchases": 512.30}
        s = Statement.from_row(legacy)
        self.assertEqual(s.issuer, "Amex")
        self.assertEqual(s.period_start, "")
        self.assertEqual(s.period_end, "")
        self.assertIsNone(s.reconciled)
        self.assertIsNone(s.reconcile_diff)
        self.assertEqual(s.reconcile_note, "")

    def test_from_row_ignores_unknown_columns(self):
        s = Statement.from_row({"source_file": "a.pdf", "not_a_field": "x"})
        self.assertEqual(s.source_file, "a.pdf")

    def test_from_row_defaults_the_source_file(self):
        self.assertEqual(Statement.from_row({}).source_file, "")

    def test_full_row_round_trips(self):
        s = Statement(source_file="a.pdf", issuer="Amex", account_last4="1001",
                      statement_period="Jan 10 - Feb 6 2026",
                      previous_balance=100.0, new_balance=250.0, purchases=200.0,
                      payments=-50.0, period_start="2026-01-10",
                      period_end="2026-02-06", reconciled=1, reconcile_diff=0.0,
                      reconcile_note="")
        self.assertEqual(Statement.from_row(s.to_row()), s)

    def test_trusted_only_when_reconciled_is_one(self):
        self.assertTrue(Statement("a.pdf", reconciled=1).trusted)
        self.assertFalse(Statement("a.pdf", reconciled=0).trusted)
        self.assertFalse(Statement("a.pdf").trusted)      # nothing to check != OK


if __name__ == "__main__":
    unittest.main()
