"""
SQLite store so the system does not have to re-parse or re-calculate every time.

Two things live here:

* The parsed transactions and statement summaries, so any downstream tool
  (analyze, planner, auditor) can read a single canonical store instead of
  re-reading whatever CSV happened to be lying around.
* A ``parsed_files`` cache keyed by content hash, so re-running the parser over
  a folder of PDFs skips every file that hasn't changed since last time.

The schema is generated from the dataclasses in models.py, so adding a field
there is the only edit needed — the table columns follow automatically.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager

from finances.models import Statement, Transaction

# FINANCES_DB lets a container, or a second store such as the demo, be chosen
# once instead of with --db on every command.
DEFAULT_DB = os.environ.get("FINANCES_DB") or "finances.db"

# Transaction columns that together identify a unique charge (a statement
# parsed twice produces the same key and is folded, not duplicated).
# Rows from a live bank feed are stored under this source, one per account.
FEED_PREFIX = "simplefin#"
_TXN_KEY = ("date", "description", "amount", "account_last4", "seq")


def _col_defs(columns: list[str], key: tuple[str, ...]) -> str:
    parts = [f'"{c}"' for c in columns]
    parts.append(f'PRIMARY KEY ({", ".join(key)})')
    return ", ".join(parts)


class FinanceDB:
    """Thin, dependency-free wrapper around a SQLite file."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    # -- lifecycle -------------------------------------------------------
    def _create_schema(self):
        self.conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                {_col_defs(Transaction.columns(), _TXN_KEY)}
            );
            CREATE TABLE IF NOT EXISTS statements (
                {_col_defs(Statement.columns(), ("source_file",))}
            );
            CREATE TABLE IF NOT EXISTS parsed_files (
                path       TEXT PRIMARY KEY,
                sha256     TEXT NOT NULL,
                mtime      REAL,
                parsed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                txn_count  INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Migrate before indexing. An index names a column, so creating one over
        # a store older than that column fails outright — and the oldest stores
        # are exactly the ones that need migrating.
        self._migrate("transactions", Transaction.columns(), _TXN_KEY)
        self._migrate("statements", Statement.columns(), ("source_file",))
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS ix_txn_cat  ON transactions(category);
            CREATE INDEX IF NOT EXISTS ix_txn_src  ON transactions(source_file);
        """)
        self.conn.commit()

    def _migrate(self, table: str, columns: list[str], key: tuple[str, ...]):
        """Bring an existing table up to the shape models.py now describes.

        ``CREATE TABLE IF NOT EXISTS`` silently keeps an old shape, so a store
        written before a field existed would otherwise fail every read of it.
        New columns are added in place; a changed primary key needs the table
        rebuilt, since SQLite cannot alter one.
        """
        info = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            return
        have = {r["name"] for r in info}
        for col in columns:
            if col not in have:
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}"')
                have.add(col)

        current = tuple(r["name"] for r in
                        sorted((r for r in info if r["pk"]), key=lambda r: r["pk"]))
        if current and current != tuple(key):
            self._rebuild(table, columns, key)

    def _rebuild(self, table: str, columns: list[str], key: tuple[str, ...]):
        """Recreate a table under a new primary key, carrying the rows over.

        Widening the key can only merge fewer rows than before, so this never
        loses data — it recovers rows an older, narrower key had folded away.
        """
        cols = ", ".join(f'"{c}"' for c in columns)
        self.conn.executescript(f"""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE {table}__new ({_col_defs(columns, key)});
            INSERT OR REPLACE INTO {table}__new ({cols}) SELECT {cols} FROM {table};
            DROP TABLE {table};
            ALTER TABLE {table}__new RENAME TO {table};
        """)

    def close(self):
        self.conn.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- parse cache -----------------------------------------------------
    def is_cached(self, path: str, sha: str) -> bool:
        """True if this exact file content was already parsed."""
        row = self.conn.execute(
            "SELECT sha256 FROM parsed_files WHERE path = ?", (path,)
        ).fetchone()
        return row is not None and row["sha256"] == sha

    def mark_parsed(self, path: str, sha: str, mtime: float, txn_count: int):
        self.conn.execute(
            "INSERT INTO parsed_files(path, sha256, mtime, txn_count) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
            "sha256=excluded.sha256, mtime=excluded.mtime, "
            "parsed_at=CURRENT_TIMESTAMP, txn_count=excluded.txn_count",
            (path, sha, mtime, txn_count),
        )
        self.conn.commit()

    # -- settings --------------------------------------------------------
    # Budgets, exchange rates and subscription decisions are about the data, so
    # they live next to it: copy the .db and they come along.
    def get_setting(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value):
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))
        self.conn.commit()

    # -- writes ----------------------------------------------------------
    def _upsert(self, table: str, columns: list[str], rows: list[dict]):
        if not rows:
            return
        cols = ", ".join(f'"{c}"' for c in columns)
        marks = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})"
        self.conn.executemany(sql, [[r.get(c) for c in columns] for r in rows])
        self.conn.commit()

    def upsert_transactions(self, txns: list[Transaction]):
        self._upsert("transactions", Transaction.columns(),
                     [t.to_row() for t in assign_sequence(txns)])

    def upsert_statements(self, stmts: list[Statement]):
        self._upsert("statements", Statement.columns(),
                     [s.to_row() for s in stmts])

    # -- reads -----------------------------------------------------------
    def transactions(self, feed: bool = False) -> list[Transaction]:
        """Every row, except live-feed rows a statement has since covered.

        A bank feed (SimpleFIN) brings this month's charges before the
        statement exists. Once the statement for those dates arrives, its rows
        are the record, checked against its printed totals, and the feed's
        copies of the same charges step aside. ``feed=True`` returns them all.
        """
        rows = self.conn.execute(
            "SELECT * FROM transactions ORDER BY date, source_file"
        ).fetchall()
        txns = [Transaction.from_row(dict(r)) for r in rows]
        if feed or not any(t.source_file.startswith(FEED_PREFIX) for t in txns):
            return txns
        covered: dict[str, list[tuple[str, str]]] = {}
        for r in self.conn.execute(
                "SELECT account_last4, period_start, period_end FROM statements "
                "WHERE source_file NOT LIKE ? AND period_start != '' AND period_end != ''",
                (FEED_PREFIX + "%",)):
            if r["account_last4"]:
                covered.setdefault(r["account_last4"], []).append(
                    (r["period_start"], r["period_end"]))
        return [t for t in txns if not (
            t.source_file.startswith(FEED_PREFIX)
            and any(a <= t.date <= b for a, b in covered.get(t.account_last4, ())))]

    def statements(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM statements ORDER BY statement_period, source_file"
        ).fetchall()
        return [dict(r) for r in rows]

    def statement_models(self) -> list[Statement]:
        """The same rows as :meth:`statements`, as typed objects."""
        return [Statement.from_row(r) for r in self.statements()]

    def date_range(self) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM transactions"
        ).fetchone()
        return (row[0], row[1]) if row and row[0] else None

    def source_files(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT source_file FROM transactions ORDER BY source_file")]

    def delete_source(self, source_file: str) -> int:
        """Drop every transaction from one statement, so it can be re-parsed."""
        cur = self.conn.execute(
            "DELETE FROM transactions WHERE source_file = ?", (source_file,))
        self.conn.execute("DELETE FROM parsed_files WHERE path LIKE ?",
                          (f"%{source_file}",))
        self.conn.commit()
        return cur.rowcount

    def transactions_for(self, source_file: str) -> list[Transaction]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE source_file = ? ORDER BY date",
            (source_file,),
        ).fetchall()
        return [Transaction.from_row(dict(r)) for r in rows]

    def statement_for(self, source_file: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM statements WHERE source_file = ?", (source_file,)
        ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    # -- CSV bridge (keeps the old file-based tools working) -------------
    def export_csv(self, txn_path: str, stmt_path: str | None = None):
        _write_csv(txn_path, Transaction.columns(),
                   [t.to_row() for t in self.transactions()])
        if stmt_path:
            _write_csv(stmt_path, Statement.columns(), self.statements())

    def import_csv(self, txn_path: str):
        with open(txn_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.upsert_transactions([Transaction.from_row(r) for r in rows])


def assign_sequence(txns: list[Transaction]) -> list[Transaction]:
    """Number repeated identical charges within each source statement.

    The primary key has to fold a statement parsed twice while keeping two
    genuinely identical charges on the same day. Ordering the occurrences within
    one ``source_file`` does both: a re-parse of that file produces the same
    ordinals and overwrites in place, while a second real $3.00 fare on the same
    morning takes ordinal 1 and keeps its own row.

    Without this the key collapsed them — one statement in the sample data lost
    41 of its 181 transactions on write, silently.
    """
    counts: dict[tuple, int] = defaultdict(int)
    for t in txns:
        k = (t.source_file, t.date, t.description, round(t.amount, 2),
             t.account_last4)
        t.seq = counts[k]
        counts[k] += 1
    return txns


def _write_csv(path: str, columns: list[str], rows: list[dict]):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


@contextmanager
def open_db(path: str = DEFAULT_DB):
    db = FinanceDB(path)
    try:
        yield db
    finally:
        db.close()
