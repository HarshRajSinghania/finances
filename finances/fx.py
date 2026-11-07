#!/usr/bin/env python3
"""
fx.py — one base currency for every total, and the rates to get there.

A store can hold a euro current account next to a dollar card. Adding those
amounts together gives a number that means nothing, so every total the engine
reports is in one base currency, and a row in any other currency is converted
at a rate you set. There is no rate feed: this tool never goes online, and a
rate you typed is one you can check.

A row in a currency with no rate is left out of the totals and counted, so the
dashboard can say "12 EUR transactions not counted" instead of quietly adding
euros to dollars.

    finances fx                       base currency, rates, what is missing
    finances fx --base EUR            change the base currency
    finances fx GBP=1.27 EUR=1.08     1 GBP is 1.27 of the base currency
    finances fx --clear GBP           forget a rate

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import replace

from finances.database import DEFAULT_DB, FinanceDB
from finances.models import Transaction

BASE_KEY = "base_currency"
RATES_KEY = "fx_rates"
DEFAULT_BASE = "USD"


def base_currency(db: FinanceDB | None = None) -> str:
    """FINANCES_CURRENCY, else what the store says, else US dollars."""
    env = (os.environ.get("FINANCES_CURRENCY") or "").strip().upper()
    if env:
        return env
    if db is not None:
        return (db.get_setting(BASE_KEY) or DEFAULT_BASE).upper()
    return DEFAULT_BASE


def rates(db: FinanceDB | None) -> dict[str, float]:
    if db is None:
        return {}
    raw = db.get_setting(RATES_KEY) or {}
    out = {}
    for k, v in raw.items():
        try:
            out[k.upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def to_base(txns: list[Transaction], base: str,
            table: dict[str, float]) -> tuple[list[Transaction], Counter]:
    """Rows converted to the base currency, and a count of the ones that could not be.

    A blank currency is the base currency. Converted rows are copies, so the
    store's own figures are never rewritten.
    """
    out: list[Transaction] = []
    missing: Counter = Counter()
    for t in txns:
        cur = (t.currency or base).upper()
        if cur == base:
            out.append(t)
            continue
        rate = table.get(cur)
        if rate is None:
            missing[cur] += 1
            continue
        out.append(replace(t, amount=round(t.amount * rate, 2)))
    return out, missing


def currencies(db: FinanceDB) -> Counter:
    return Counter((r[0] or "").upper() for r in
                   db.conn.execute("SELECT currency FROM transactions"))


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances fx",
                                 description="Base currency and exchange rates.")
    ap.add_argument("rates", nargs="*", metavar="CUR=RATE",
                    help="1 CUR is RATE of the base currency, e.g. EUR=1.08")
    ap.add_argument("--base", help="set the base currency (ISO code, e.g. EUR)")
    ap.add_argument("--clear", nargs="+", metavar="CUR", default=[],
                    help="forget these rates")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    with FinanceDB(args.db) as db:
        if args.base:
            db.set_setting(BASE_KEY, args.base.strip().upper())
        table = rates(db)
        for item in args.rates:
            cur, _, val = item.partition("=")
            try:
                table[cur.strip().upper()] = float(val)
            except ValueError:
                print(f"finances fx: cannot read {item!r}; use CUR=RATE",
                      file=sys.stderr)
                return 2
        for cur in args.clear:
            table.pop(cur.strip().upper(), None)
        if args.rates or args.clear:
            db.set_setting(RATES_KEY, table)

        base = base_currency(db)
        print(f"Base currency  {base}"
              + ("  (from FINANCES_CURRENCY)" if os.environ.get("FINANCES_CURRENCY") else ""))
        for cur, rate in sorted(table.items()):
            print(f"  1 {cur} = {rate:g} {base}")
        seen = currencies(db)
        missing = {(c or base): n for c, n in seen.items()
                   if (c or base) != base and (c or base) not in table}
        if missing:
            print("\nNot counted in totals until a rate is set:")
            for cur, n in sorted(missing.items()):
                print(f"  {n:>6} {cur} transactions   finances fx {cur}=<rate>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
