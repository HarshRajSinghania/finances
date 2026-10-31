"""
Unit tests for database.py — the SQLite store.

Everything here runs against a throwaway database in a temp directory; nothing
touches finances.db. The three things that actually matter are covered hardest:

* the primary key, because it is the only thing stopping a statement parsed
  twice from doubling a month's spending,
* the parsed_files content hash, because that is what makes re-running the
  parser over a folder cheap, and
* ``_migrate``, because ``CREATE TABLE IF NOT EXISTS`` silently keeps an old
  column set and a store written before a field existed has to keep working.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import unittest

import support
from support import stmt, txn

from database import DEFAULT_DB, FinanceDB, open_db
from models import Statement, Transaction


class DatabaseTestCase(support.TempDirMixin, unittest.TestCase):
    """Base class handing out a fresh store per test."""

    def db(self, name: str = "test.db") -> FinanceDB:
        d = FinanceDB(self.tmp_path(name))
        self.addCleanup(d.close)
        return d


class SchemaTests(DatabaseTestCase):

    def test_default_db_name(self):
        self.assertEqual(DEFAULT_DB, "finances.db")

    def test_tables_are_created_on_open(self):
        db = self.db()
        names = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"transactions", "statements", "parsed_files"}, names)

    def test_schema_columns_come_from_the_dataclasses(self):
        db = self.db()
        for table, model in (("transactions", Transaction), ("statements", Statement)):
            with self.subTest(table=table):
                cols = [r["name"] for r in
                        db.conn.execute(f"PRAGMA table_info({table})")]
                self.assertEqual(cols, model.columns())

    def test_opening_an_existing_store_twice_is_harmless(self):
        p = self.tmp_path("twice.db")
        with FinanceDB(p) as a:
            a.upsert_transactions([txn("2026-01-05", 10.0)])
        with FinanceDB(p) as b:
            self.assertEqual(b.count(), 1)

    def test_open_db_contextmanager_commits_and_closes(self):
        p = self.tmp_path("ctx.db")
        with open_db(p) as db:
            db.upsert_transactions([txn("2026-01-05", 10.0)])
        with open_db(p) as db:
            self.assertEqual(db.count(), 1)


class TransactionRoundTripTests(DatabaseTestCase):

    def test_round_trip_preserves_every_field(self):
        t = Transaction(date="2026-01-05", description="SQ *CAFE", amount=4.5,
                        merchant="CAFE", category="Dining", direction="out",
                        post_date="2026-01-06", issuer="Amex",
                        account_last4="1001", statement_period="Jan 2026",
                        source_file="amex.pdf")
        db = self.db()
        db.upsert_transactions([t])
        self.assertEqual(db.transactions(), [t])

    def test_negative_amounts_survive(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", -157.98, "PAYMENT THANK YOU",
                                    category="Payment")])
        got = db.transactions()[0]
        self.assertEqual(got.amount, -157.98)
        self.assertEqual(got.direction, "in")

    def test_transactions_come_back_ordered_by_date(self):
        db = self.db()
        db.upsert_transactions([txn("2026-03-01", 3.0, "C"),
                                txn("2026-01-01", 1.0, "A"),
                                txn("2026-02-01", 2.0, "B")])
        self.assertEqual([t.date for t in db.transactions()],
                         ["2026-01-01", "2026-02-01", "2026-03-01"])

    def test_upserting_an_empty_list_is_a_no_op(self):
        db = self.db()
        db.upsert_transactions([])
        self.assertEqual(db.count(), 0)

    def test_date_range_and_source_files(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 1.0, "A", source_file="one.pdf"),
                                txn("2026-04-20", 2.0, "B", source_file="two.pdf")])
        self.assertEqual(db.date_range(), ("2026-01-05", "2026-04-20"))
        self.assertEqual(db.source_files(), ["one.pdf", "two.pdf"])

    def test_date_range_of_an_empty_store_is_none(self):
        self.assertIsNone(self.db().date_range())

    def test_transactions_for_one_source(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 1.0, "A", source_file="one.pdf"),
                                txn("2026-01-06", 2.0, "B", source_file="two.pdf")])
        got = db.transactions_for("one.pdf")
        self.assertEqual([t.description for t in got], ["A"])

    def test_delete_source_removes_only_that_statement(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 1.0, "A", source_file="one.pdf"),
                                txn("2026-01-06", 2.0, "B", source_file="two.pdf")])
        db.mark_parsed("/PDFs/one.pdf", "sha-one", 1.0, 1)
        removed = db.delete_source("one.pdf")
        self.assertEqual(removed, 1)
        self.assertEqual(db.source_files(), ["two.pdf"])
        # The parse cache entry goes too, so the file can be re-parsed.
        self.assertFalse(db.is_cached("/PDFs/one.pdf", "sha-one"))


class DedupTests(DatabaseTestCase):
    """The primary key is (date, description, amount, account_last4)."""

    def test_reparsing_the_same_statement_folds_rather_than_duplicates(self):
        db = self.db()
        batch = [txn("2026-01-05", 4.5, "SQ *CAFE"),
                 txn("2026-01-06", 12.0, "SHELL OIL")]
        db.upsert_transactions(batch)
        db.upsert_transactions(batch)
        db.upsert_transactions(batch)
        self.assertEqual(db.count(), 2)

    def test_an_upsert_updates_the_non_key_columns(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5, "SQ *CAFE",
                                    category="Uncategorized")])
        db.upsert_transactions([txn("2026-01-05", 4.5, "SQ *CAFE",
                                    category="Dining", merchant="BLUE BOTTLE")])
        self.assertEqual(db.count(), 1)
        got = db.transactions()[0]
        self.assertEqual(got.category, "Dining")
        self.assertEqual(got.merchant, "BLUE BOTTLE")

    def test_the_same_charge_on_two_cards_is_two_rows(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5, "SQ *CAFE", account_last4="1111"),
                                txn("2026-01-05", 4.5, "SQ *CAFE", account_last4="2222")])
        self.assertEqual(db.count(), 2)

    def test_two_genuinely_different_amounts_are_two_rows(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5, "SQ *CAFE"),
                                txn("2026-01-05", 6.75, "SQ *CAFE")])
        self.assertEqual(db.count(), 2)

    def test_the_primary_key_matches_the_dedup_key(self):
        # models.Transaction.dedup_key and the table's PRIMARY KEY have to agree
        # or in-memory dedup and on-disk dedup disagree about what is a dupe.
        db = self.db()
        pk = [r["name"] for r in db.conn.execute("PRAGMA table_info(transactions)")
              if r["pk"]]
        self.assertEqual(
            sorted(pk),
            sorted(["date", "description", "amount", "account_last4", "seq"]))

    def test_identical_same_day_charges_both_survive_a_write(self):
        # Three real $3.00 fares on one morning are three charges, not one.
        db = self.db()
        db.upsert_transactions([
            Transaction("2026-01-05", "PATH TAP", 3.0, account_last4="1234",
                        source_file="chase.pdf") for _ in range(3)])
        self.assertEqual(db.count(), 3)

    def test_reparsing_the_same_statement_folds_instead_of_duplicating(self):
        db = self.db()
        for _ in range(2):
            db.upsert_transactions([
                Transaction("2026-01-05", "PATH TAP", 3.0, account_last4="1234",
                            source_file="chase.pdf") for _ in range(3)])
        self.assertEqual(db.count(), 3)


class StatementTests(DatabaseTestCase):

    def test_round_trip_preserves_every_field(self):
        s = Statement(source_file="amex.pdf", issuer="Amex", account_last4="1001",
                      statement_period="Jan 10 - Feb 6 2026",
                      previous_balance=100.0, new_balance=250.0, purchases=200.0,
                      payments=-50.0, interest_charged=0.0, purchase_apr=23.49,
                      charges_extracted=200.0, credits_extracted=50.0,
                      period_start="2026-01-10", period_end="2026-02-06",
                      reconciled=1, reconcile_diff=0.0, reconcile_note="")
        db = self.db()
        db.upsert_statements([s])
        self.assertEqual(db.statement_models(), [s])

    def test_statements_are_keyed_by_source_file(self):
        db = self.db()
        db.upsert_statements([stmt("amex.pdf", purchases=100.0)])
        db.upsert_statements([stmt("amex.pdf", purchases=200.0)])
        rows = db.statements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purchases"], 200.0)

    def test_statement_for_a_missing_file_is_none(self):
        self.assertIsNone(self.db().statement_for("nope.pdf"))

    def test_statement_for_returns_a_dict_row(self):
        db = self.db()
        db.upsert_statements([stmt("amex.pdf", purchases=100.0)])
        row = db.statement_for("amex.pdf")
        self.assertEqual(row["issuer"], "TestBank")
        self.assertEqual(row["purchases"], 100.0)

    def test_none_numeric_fields_stay_none(self):
        db = self.db()
        db.upsert_statements([stmt("amex.pdf")])
        s = db.statement_models()[0]
        self.assertIsNone(s.purchases)
        self.assertIsNone(s.reconciled)


class ParseCacheTests(DatabaseTestCase):

    def test_unknown_file_is_not_cached(self):
        self.assertFalse(self.db().is_cached("/PDFs/a.pdf", "abc"))

    def test_marked_file_is_cached_for_the_same_content(self):
        db = self.db()
        db.mark_parsed("/PDFs/a.pdf", "abc", 1234.5, 17)
        self.assertTrue(db.is_cached("/PDFs/a.pdf", "abc"))

    def test_changed_content_invalidates_the_cache(self):
        db = self.db()
        db.mark_parsed("/PDFs/a.pdf", "abc", 1234.5, 17)
        self.assertFalse(db.is_cached("/PDFs/a.pdf", "def"))

    def test_remarking_a_file_updates_rather_than_duplicates(self):
        db = self.db()
        db.mark_parsed("/PDFs/a.pdf", "abc", 1.0, 17)
        db.mark_parsed("/PDFs/a.pdf", "def", 2.0, 19)
        rows = list(db.conn.execute("SELECT * FROM parsed_files"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sha256"], "def")
        self.assertEqual(rows[0]["txn_count"], 19)
        self.assertFalse(db.is_cached("/PDFs/a.pdf", "abc"))
        self.assertTrue(db.is_cached("/PDFs/a.pdf", "def"))

    def test_the_cache_survives_a_reopen(self):
        p = self.tmp_path("cache.db")
        with FinanceDB(p) as db:
            db.mark_parsed("/PDFs/a.pdf", "abc", 1.0, 17)
        with FinanceDB(p) as db:
            self.assertTrue(db.is_cached("/PDFs/a.pdf", "abc"))


class CsvBridgeTests(DatabaseTestCase):

    def test_export_import_round_trip(self):
        src = self.db("src.db")
        rows = [txn("2026-01-05", 4.5, "SQ *CAFE", merchant="CAFE",
                    category="Dining", issuer="Amex", post_date="2026-01-06",
                    statement_period="Jan 2026", source_file="amex.pdf"),
                txn("2026-01-20", -157.98, "PAYMENT THANK YOU",
                    category="Payment", issuer="Amex", source_file="amex.pdf")]
        src.upsert_transactions(rows)
        src.upsert_statements([stmt("amex.pdf", purchases=4.5, payments=-157.98)])

        txn_csv, stmt_csv = self.tmp_path("t.csv"), self.tmp_path("s.csv")
        src.export_csv(txn_csv, stmt_csv)

        dst = self.db("dst.db")
        dst.import_csv(txn_csv)
        self.assertEqual(dst.transactions(), src.transactions())

    def test_exported_header_is_the_model_column_list(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5)])
        txn_csv, stmt_csv = self.tmp_path("t.csv"), self.tmp_path("s.csv")
        db.export_csv(txn_csv, stmt_csv)
        with open(txn_csv, newline="") as fh:
            self.assertEqual(next(csv.reader(fh)), Transaction.columns())
        with open(stmt_csv, newline="") as fh:
            self.assertEqual(next(csv.reader(fh)), Statement.columns())

    def test_export_creates_a_missing_output_folder(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5)])
        out = self.tmp_path(os.path.join("nested", "deeper", "t.csv"))
        db.export_csv(out)
        self.assertTrue(os.path.exists(out))

    def test_importing_the_same_csv_twice_does_not_duplicate(self):
        db = self.db()
        db.upsert_transactions([txn("2026-01-05", 4.5, "SQ *CAFE")])
        out = self.tmp_path("t.csv")
        db.export_csv(out)
        db.import_csv(out)
        db.import_csv(out)
        self.assertEqual(db.count(), 1)

    def test_importing_a_legacy_csv_with_only_the_required_columns(self):
        out = self.tmp_path("legacy.csv")
        with open(out, "w", newline="") as fh:
            fh.write("date,description,amount\n"
                     "2026-01-05,SQ *CAFE,4.50\n"
                     "2026-01-20,PAYMENT THANK YOU,-157.98\n")
        db = self.db()
        db.import_csv(out)
        self.assertEqual(db.count(), 2)
        got = {t.description: t for t in db.transactions()}
        self.assertEqual(got["SQ *CAFE"].amount, 4.5)
        self.assertEqual(got["SQ *CAFE"].merchant, "SQ *CAFE")
        self.assertEqual(got["PAYMENT THANK YOU"].direction, "in")


class MigrationTests(DatabaseTestCase):
    """A store written before models.py grew a field must keep working."""

    def _legacy_store(self, txn_cols, stmt_cols, txn_row, stmt_row) -> str:
        p = self.tmp_path("legacy.db")
        conn = sqlite3.connect(p)
        conn.execute(
            f'CREATE TABLE transactions ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in txn_cols)}, '
            f'PRIMARY KEY (date, description, amount))')
        conn.execute(
            f'CREATE TABLE statements ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in stmt_cols)}, '
            f'PRIMARY KEY (source_file))')
        conn.execute(f"INSERT INTO transactions VALUES ({', '.join('?' * len(txn_cols))})",
                     txn_row)
        conn.execute(f"INSERT INTO statements VALUES ({', '.join('?' * len(stmt_cols))})",
                     stmt_row)
        conn.commit()
        conn.close()
        return p

    def _old_store(self) -> str:
        # An older shape: no issuer/account_last4/direction/post_date on
        # transactions, and none of the coverage or reconciliation columns on
        # statements. source_file is present because an index depends on it.
        return self._legacy_store(
            ["date", "description", "amount", "merchant", "category", "source_file"],
            ["source_file", "issuer", "account_last4", "statement_period", "purchases"],
            ("2026-01-05", "SQ *CAFE", 4.5, "CAFE", "Dining", "amex.pdf"),
            ("amex.pdf", "Amex", "1001", "Jan 10 - Feb 6 2026", 512.30),
        )

    def test_migrate_adds_the_new_transaction_columns(self):
        db = FinanceDB(self._old_store())
        self.addCleanup(db.close)
        cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(transactions)")]
        self.assertEqual(sorted(cols), sorted(Transaction.columns()))

    def test_migrate_adds_the_new_statement_columns(self):
        db = FinanceDB(self._old_store())
        self.addCleanup(db.close)
        cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(statements)")]
        self.assertEqual(sorted(cols), sorted(Statement.columns()))
        for new in ("period_start", "period_end", "reconciled",
                    "reconcile_diff", "reconcile_note"):
            self.assertIn(new, cols)

    def test_migrate_does_not_lose_data(self):
        db = FinanceDB(self._old_store())
        self.addCleanup(db.close)
        self.assertEqual(db.count(), 1)
        t = db.transactions()[0]
        self.assertEqual(t.date, "2026-01-05")
        self.assertEqual(t.description, "SQ *CAFE")
        self.assertEqual(t.amount, 4.5)
        self.assertEqual(t.category, "Dining")
        self.assertEqual(t.source_file, "amex.pdf")

        s = db.statement_models()[0]
        self.assertEqual(s.issuer, "Amex")
        self.assertEqual(s.account_last4, "1001")
        self.assertEqual(s.purchases, 512.30)
        self.assertEqual(s.period_start, "")        # absent column -> default
        self.assertIsNone(s.reconciled)

    def test_a_migrated_store_is_writable_with_the_new_fields(self):
        db = FinanceDB(self._old_store())
        self.addCleanup(db.close)
        db.upsert_transactions([txn("2026-02-01", 9.99, "NETFLIX",
                                    issuer="Amex", account_last4="1001")])
        db.upsert_statements([stmt("disc.pdf", reconciled=1, reconcile_diff=0.0,
                                   period_start="2026-02-01",
                                   period_end="2026-02-28")])
        self.assertEqual(db.count(), 2)
        got = {s.source_file: s for s in db.statement_models()}
        self.assertEqual(got["disc.pdf"].reconciled, 1)
        self.assertEqual(got["disc.pdf"].period_start, "2026-02-01")

    def test_migration_is_idempotent(self):
        p = self._old_store()
        FinanceDB(p).close()
        db = FinanceDB(p)
        self.addCleanup(db.close)
        cols = [r["name"] for r in db.conn.execute("PRAGMA table_info(transactions)")]
        self.assertEqual(len(cols), len(set(cols)))
        self.assertEqual(db.count(), 1)

    def test_a_store_predating_the_source_file_column_opens(self):
        """Indexes are created after migration, not in the same script.

        The index DDL (``CREATE INDEX ... ON transactions(source_file)``) is in
        the same ``executescript`` as the ``CREATE TABLE IF NOT EXISTS``, and it
        runs *before* ``_migrate`` gets a chance to add the column. So the
        migration path only works for columns no index touches, which defeats
        the point: the oldest stores are exactly the ones missing source_file.
        Fix: run ``_migrate`` before creating the indexes, or create the indexes
        in a second executescript afterwards.

        Secondary effect: __init__ has already opened self.conn when it raises,
        so the caller never gets a handle and the connection leaks. That is why
        this test keeps only the message and drops the traceback before failing:
        holding the traceback keeps the FinanceDB frame — and its open
        connection — alive, and the ResourceWarning then surfaces in whichever
        unrelated test happens to trigger the next collection.
        """
        import gc
        import warnings

        p = self._legacy_store(
            ["date", "description", "amount", "merchant", "category"],
            ["source_file", "issuer", "purchases"],
            ("2026-01-05", "SQ *CAFE", 4.5, "CAFE", "Dining"),
            ("amex.pdf", "Amex", 512.30),
        )
        problem, db = None, None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            try:
                db = FinanceDB(p)
            except Exception as e:
                problem = f"opening a pre-source_file store raised {type(e).__name__}: {e}"
            gc.collect()          # drop the orphaned connection here, quietly
        if problem:
            self.fail(problem)
        self.addCleanup(db.close)
        self.assertEqual(db.count(), 1)

    def test_migrated_string_columns_read_back_as_empty_strings(self):
        """Transaction.from_row coerces a migrated NULL column to "", not None.

        ``row.get("issuer", "")`` only defaults when the *key* is absent. After
        ``_migrate`` the key is present with a NULL value, so issuer, post_date,
        account_last4, statement_period and source_file all come back as None on
        rows written before the column existed. Downstream that means
        ``dedup_key`` carrying None, ``source_files()`` returning None entries
        and any ``.strip()``/f-string on those fields behaving oddly.
        Fix: ``(row.get("issuer") or "")``.
        """
        db = FinanceDB(self._old_store())
        self.addCleanup(db.close)
        t = db.transactions()[0]
        self.assertEqual(t.issuer, "")
        self.assertEqual(t.account_last4, "")
        self.assertEqual(t.post_date, "")
        self.assertEqual(t.statement_period, "")


if __name__ == "__main__":
    unittest.main()
