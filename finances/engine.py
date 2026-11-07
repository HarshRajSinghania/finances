#!/usr/bin/env python3
"""
The read model that ties the toolchain together.

The pipeline is:

    parse.py   PDFs  -> transactions + statements, persisted in SQLite
    engine.py  SQLite -> one queryable view + simple forecasts
    analyze / planner / financial_auditor  act on that same data

Every tool now shares one store (``finances.db``) and one row schema
(:mod:`models`), so nothing has to re-parse a PDF or re-derive a total that a
sibling script already computed. This module is the thin layer that reads the
store back and answers the questions the other tools ask over and over:
what did I spend, by category, by month, and where is it heading.
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from finances.database import DEFAULT_DB, FinanceDB
from finances.models import Statement, Transaction
from finances.utils import Recurring, detect_recurring, fmt_money, future_value, merchant_key

# The taxonomy lives in categorizer.py; fall back to the same literals the other
# tools use so engine.py still imports on its own.
try:
    from finances.categorizer import DISCRETIONARY, ESSENTIAL, NON_SPEND, WASTE
except ImportError:                                          # pragma: no cover
    NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}
    ESSENTIAL = {"Rent/Housing", "Utilities", "Groceries", "Transport",
                 "Insurance", "Health"}
    DISCRETIONARY: set[str] = set()
    WASTE = {"Fees/Interest"}


def load_transactions(source: str = DEFAULT_DB) -> list[Transaction]:
    """Read transactions from a SQLite store or a legacy CSV, transparently.

    Every tool takes its input through here, so ``--db finances.db`` and
    ``transactions.csv`` are interchangeable everywhere and no tool needs to know
    which one it was handed.
    """
    if source and source.lower().endswith(".csv"):
        with open(source, newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            raise SystemExit(f"{source} is empty")
        return [Transaction.from_row(r) for r in rows]
    if not os.path.exists(source):
        raise SystemExit(
            f"{source} does not exist. Build it with:  finances parse <PDFs> --db {source}")
    with FinanceDB(source) as db:
        return db.transactions()


def month_key(d: date | str) -> str:
    return d[:7] if isinstance(d, str) else d.strftime("%Y-%m")


def _modal(values) -> str:
    """The most common value, ties broken alphabetically.

    ``max(set(xs), key=xs.count)`` is the obvious way to write this and it is
    not reproducible: set iteration order varies between runs, so a merchant
    with an even split between two categories came back labelled differently
    each time the report was generated.
    """
    counts = Counter(values)
    if not counts:
        return ""
    best = max(counts.values())
    return min(v for v, n in counts.items() if n == best)


class Engine:
    """A queryable view over the persisted transactions.

    Holds the *semantics* — what counts as spending, which months are complete,
    what repeats — so analyze/planner/auditor stop each answering those
    questions slightly differently.
    """

    def __init__(self, db_path: str = DEFAULT_DB, txns: list[Transaction] | None = None):
        self.path = db_path
        self.db = None if txns is not None else FinanceDB(db_path)
        self._txns: list[Transaction] | None = txns
        self._recurring: list[Recurring] | None = None
        self._statements: list[Statement] | None = None
        # Every total is in one currency. Rows in another one with no rate set
        # are held out and counted here, so a caller can say so.
        self.currency: str = "USD"
        self.fx_missing: Counter = Counter()

    @classmethod
    def from_source(cls, source: str = DEFAULT_DB) -> "Engine":
        """Build from either a .db or a .csv path."""
        if source and source.lower().endswith(".csv"):
            return cls(source, txns=load_transactions(source))
        return cls(source)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        # A view from with_transactions() borrows the parent's connection, so it
        # must not close it out from under whoever still holds the parent.
        if self.db is not None and getattr(self, "_owns_db", True):
            self.db.close()
        self.db = None

    @property
    def transactions(self) -> list[Transaction]:
        if self._txns is None:
            from finances.fx import base_currency, rates, to_base
            self.currency = base_currency(self.db)
            self._txns, self.fx_missing = to_base(
                self.db.transactions(), self.currency, rates(self.db))
        return self._txns

    def with_transactions(self, rows: list[Transaction]) -> "Engine":
        """A view over a subset of the rows, keeping this store's statements.

        Exclusion filters (``--exclude-merchant``, a backtest cutoff) narrow the
        transactions but must not cost the caller its reconciliation verdicts —
        constructing ``Engine(path, txns=...)`` drops the DB handle and would
        silently report that nothing has been verified.
        """
        view = Engine.__new__(Engine)
        view.path = self.path
        view.db = self.db
        view._owns_db = False
        view._txns = rows
        view._recurring = None
        view._statements = self._statements
        view.currency = self.currency
        view.fx_missing = self.fx_missing
        return view

    def attach_statements(self, stmts: list[Statement]):
        """Supply statements from outside the store, for CSV input with a sidecar."""
        self._statements = list(stmts)

    def statements(self) -> list[Statement]:
        if self._statements is not None:
            return self._statements
        return self.db.statement_models() if self.db is not None else []

    # -- row selection ---------------------------------------------------
    def spend(self) -> list[Transaction]:
        """Money genuinely leaving the household: no payments, no transfers."""
        return [t for t in self.transactions
                if t.amount > 0 and t.category not in NON_SPEND]

    def income(self) -> list[Transaction]:
        return [t for t in self.transactions
                if t.amount < 0 and t.category == "Income"]

    def dates(self) -> tuple[date, date] | None:
        ds = [t.d for t in (self.spend() or self.transactions) if t.d]
        return (min(ds), max(ds)) if ds else None

    # -- aggregates ------------------------------------------------------
    def total_spend(self) -> float:
        return sum(t.amount for t in self.spend())

    def total_credits(self) -> float:
        return sum(-t.amount for t in self.transactions if t.amount < 0)

    def by_category(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for t in self.spend():
            out[t.category] += t.amount
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_month(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for t in self.spend():
            out[t.date[:7]] += t.amount
        return dict(sorted(out.items()))

    def income_by_month(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for t in self.income():
            out[t.date[:7]] += -t.amount
        return dict(sorted(out.items()))

    def income_average(self) -> float | None:
        """Take-home per complete month, or None when no income was found.

        None and 0.0 mean different things here — no paycheque in the data at
        all, versus a month that genuinely had none — so callers can tell the
        difference between "unknown" and "nothing came in".
        """
        im = self.income_by_month()
        months = self.complete_months()
        if not months or not any(im.get(m) for m in months):
            return None
        return sum(im.get(m, 0.0) for m in months) / len(months)

    def by_category_monthly(self) -> dict[str, float]:
        """Average monthly spend per category, over complete months only."""
        months = set(self.complete_months())
        if not months:
            return {}
        out: dict[str, float] = defaultdict(float)
        for t in self.spend():
            if t.date[:7] in months:
                out[t.category] += t.amount
        n = len(months)
        return dict(sorted(((c, v / n) for c, v in out.items()),
                           key=lambda kv: -kv[1]))

    def complete_months(self) -> list[str]:
        """Months the data actually covers most of.

        A partial first or last month drags every average down, so averages are
        taken over these only.
        """
        rng = self.dates()
        if not rng:
            return []
        first, last = rng
        out = []
        for m in sorted(self.by_month()):
            y, mo = int(m[:4]), int(m[5:7])
            start = date(y, mo, 1)
            end = (start.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)
            days = (min(end, last) - max(start, first)).days + 1
            if days >= (end - start).days * 0.85:
                out.append(m)
        return out or sorted(self.by_month())

    def monthly_average(self) -> float:
        months, complete = self.by_month(), self.complete_months()
        if not complete:
            return sum(months.values()) / len(months) if months else 0.0
        return sum(months.get(m, 0.0) for m in complete) / len(complete)

    def essential_monthly(self) -> float:
        complete = set(self.complete_months())
        if not complete:
            return 0.0
        total = sum(t.amount for t in self.spend()
                    if t.category in ESSENTIAL and t.date[:7] in complete)
        return total / len(complete)

    def surplus(self, income_override: float | None = None) -> float | None:
        """What is left over each month. None when income is unknown."""
        complete = self.complete_months()
        inc = income_override
        if inc is None:
            im = self.income_by_month()
            vals = [im.get(m, 0.0) for m in complete]
            inc = sum(vals) / len(vals) if vals and any(vals) else None
        return None if inc is None else inc - self.monthly_average()

    def merchants(self) -> list[dict]:
        groups: dict[str, list[Transaction]] = defaultdict(list)
        for t in self.spend():
            groups[merchant_key(t.merchant)].append(t)
        out = []
        for k, ts in groups.items():
            amts = [t.amount for t in ts]
            out.append({"key": k, "name": _modal(t.merchant for t in ts),
                        "category": _modal(t.category for t in ts),
                        "total": sum(amts), "n": len(ts),
                        "avg": sum(amts) / len(ts), "last": max(t.d for t in ts)})
        out.sort(key=lambda m: -m["total"])
        return out

    def recurring(self) -> list[Recurring]:
        if self._recurring is None:
            self._recurring = detect_recurring(self.spend())
        return self._recurring

    def subscriptions(self) -> list[Recurring]:
        """Recurring charges you actually signed up for.

        Interest lands on the card every month at a steady amount, so it looks
        exactly like a subscription to a cadence detector. It is a consequence
        of a balance rather than a thing to cancel, and counting it as a
        commitment makes a savings plan look more constrained than it is —
        so budgeting callers want this list, not :meth:`recurring`.
        """
        return [r for r in self.recurring() if r.category not in WASTE]

    def active_subscriptions(self) -> list[Recurring]:
        """Subscriptions that are still actually charging."""
        today = date.today()
        return [r for r in self.subscriptions() if not r.is_stale(today)]

    def stale_monthly(self) -> float:
        """What the charges that stopped arriving used to cost per month."""
        today = date.today()
        return sum(r.per_month for r in self.subscriptions() if r.is_stale(today))

    def committed_monthly(self, include_interest: bool = False,
                          include_stale: bool = False) -> float:
        """The part of next month that is already promised to someone else.

        Stale charges are excluded by default. A subscription whose next payment
        was due months ago is not a commitment — it was cancelled, or the card on
        file died. Counting it still shrinks the surplus a savings plan is built
        from, which understates what is genuinely available. The money is not
        forgotten: :meth:`stale_monthly` reports it separately, because a dead
        card means the charge returns the day it is replaced.
        """
        rows = self.recurring() if include_interest else self.subscriptions()
        if not include_stale:
            today = date.today()
            rows = [r for r in rows if not r.is_stale(today)]
        return sum(r.per_month for r in rows)

    # -- trust -----------------------------------------------------------
    def coverage(self) -> dict:
        """How much of this data is verified against the statements it came from.

        Reported rather than assumed: an unreconciled statement means the totals
        downstream may be wrong, and every tool should say so instead of
        printing a confident number.
        """
        stmts = self.statements()
        ok = [s for s in stmts if s.reconciled == 1]
        bad = [s for s in stmts if s.reconciled == 0]
        unknown = [s for s in stmts if s.reconciled is None]

        # Gaps between statement periods, per account.
        by_acct: dict[str, list[Statement]] = defaultdict(list)
        for s in stmts:
            if s.period_start and s.period_end:
                by_acct[f"{s.issuer} {s.account_last4}".strip()].append(s)
        gaps = []
        for acct, items in by_acct.items():
            items.sort(key=lambda s: s.period_start)
            for a, b in zip(items, items[1:]):
                try:
                    a_end = datetime.strptime(a.period_end[:10], "%Y-%m-%d").date()
                    b_start = datetime.strptime(b.period_start[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if (b_start - a_end).days > 5:
                    gaps.append({"account": acct, "after": a.period_end,
                                 "before": b.period_start,
                                 "days": (b_start - a_end).days})

        n = len(stmts)
        return {
            "statements": n,
            "reconciled": len(ok), "mismatched": len(bad), "unchecked": len(unknown),
            "pct": (len(ok) / n * 100.0) if n else 0.0,
            "mismatches": sorted(bad, key=lambda s: -abs(s.reconcile_diff or 0)),
            "gaps": sorted(gaps, key=lambda g: -g["days"]),
            "trusted": n > 0 and len(ok) == n and not gaps,
        }


class SpendingForecast:
    """Projects near-term spending from the trailing monthly average."""

    def __init__(self, engine: Engine, window: int = 3):
        self.months = engine.by_month()
        self.window = window

    def next_month(self) -> float:
        recent = list(self.months.values())[-self.window:]
        return sum(recent) / len(recent) if recent else 0.0

    def project(self, months: int) -> list[float]:
        return [self.next_month()] * months


class SavingsForecast:
    """Projects a savings balance forward at a fixed monthly contribution."""

    def __init__(self, starting: float = 0.0, apy: float = 0.0):
        self.starting = starting
        self.apy = apy

    def after(self, monthly: float, months: int) -> float:
        return future_value(self.starting, monthly, months, self.apy)


def main():
    ap = argparse.ArgumentParser(description="Unified view over the finances store.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite store (default {DEFAULT_DB})")
    ap.add_argument("--forecast", type=int, metavar="MONTHS", default=0,
                    help="also project spending this many months forward")
    args = ap.parse_args()

    with Engine(args.db) as eng:
        n = len(eng.transactions)
        if not n:
            print(f"{args.db} has no transactions yet. Run:  finances parse <PDFs> --db {args.db}")
            return
        print(f"{n} transactions | spend {fmt_money(eng.total_spend(), 2)} | "
              f"credits {fmt_money(eng.total_credits(), 2)} | "
              f"{fmt_money(eng.monthly_average(), 2)}/mo average\n")

        print("By category:")
        spend = eng.total_spend() or 1.0
        for cat, amt in eng.by_category().items():
            print(f"  {cat:<18} {fmt_money(amt, 2):>14}  ({amt / spend * 100:4.1f}%)")

        if args.forecast:
            fc = SpendingForecast(eng).next_month()
            print(f"\nProjected spend: ~{fmt_money(fc, 2)}/mo "
                  f"→ {fmt_money(fc * args.forecast, 2)} over {args.forecast} months")


if __name__ == "__main__":
    main()
