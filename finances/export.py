#!/usr/bin/env python3
"""
export.py — the store, in the formats other money tools read.

This tool is good at one thing: getting complete, checked transactions out of
statements and bank exports. Budgeting apps and plain-text ledgers are good at
other things, so this hands the rows over instead of competing with them.

    finances export beancount -o money.beancount
    finances export hledger   -o money.journal      # also reads as ledger-cli
    finances export ynab      -o exports/           # one CSV per account
    finances export actual    -o exports/           # one CSV per account
    finances export csv       -o everything.csv

``--balances`` adds balance assertions to the beancount and ledger output: for
every statement that reconciled, the balance it printed is asserted on the day
after it closed. ``bean-check`` or ``hledger check`` then verifies the whole
history again, independently of this tool.

Each account becomes a ledger account named after its issuer and last four
digits: ``Liabilities:Card:Amex-1005``, ``Assets:Bank:Chase-4321``. Money that
only moves between your own accounts (a card payment from checking) goes
through ``Assets:Transfers``, so it nets to zero instead of counting twice.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from finances.database import DEFAULT_DB, FinanceDB
from finances.fx import base_currency
from finances.models import Statement, Transaction

FORMATS = ("beancount", "hledger", "ledger", "ynab", "actual", "csv")

# Issuers that only ever issue cards, for statements that did not say.
CARD_ISSUERS = {"Amex", "Discover", "Capital One", "Apple Card", "Synchrony",
                "Barclays"}

TRANSFER = {"Payment", "Transfers/Cash"}


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def _component(s: str) -> str:
    """A valid account-name component: starts with a capital or digit, no spaces."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-") or "Unknown"
    return s[0].upper() + s[1:]


def account_kinds(statements: list[Statement]) -> dict[tuple[str, str], str]:
    kinds: dict[tuple[str, str], str] = {}
    for s in statements:
        k = (s.issuer, s.account_last4)
        if s.account_type:
            kinds[k] = s.account_type
        elif s.credit_limit or s.purchase_apr:
            kinds.setdefault(k, "card")
    return kinds


def kind_of(t_issuer: str, last4: str, kinds: dict) -> str:
    k = kinds.get((t_issuer, last4))
    if k:
        return k
    return "card" if t_issuer in CARD_ISSUERS else "bank"


def ledger_account(issuer: str, last4: str, kind: str) -> str:
    name = _component(issuer or "Account") + (f"-{last4}" if last4 else "")
    return f"Liabilities:Card:{name}" if kind == "card" else f"Assets:Bank:{name}"


def counter_account(t: Transaction) -> str:
    if t.category in TRANSFER:
        return "Assets:Transfers"
    if t.category == "Income":
        return "Income:Earned"
    if t.category == "Credits/Refunds":
        return "Income:Refunds"
    return "Expenses:" + _component(t.category or "Uncategorized")


# --------------------------------------------------------------------------
# Plain-text ledgers
# --------------------------------------------------------------------------

def _q(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _amt(v: float, cur: str) -> str:
    return f"{v:.2f} {cur}"


def _date(iso: str) -> date:
    return datetime.strptime(iso[:10], "%Y-%m-%d").date()


def _assertions(statements, kinds, base):
    """(date, account, amount, currency, kind) for every trustworthy balance."""
    out = []
    for s in statements:
        if s.reconciled != 1 or s.new_balance is None or not s.period_end:
            continue
        kind = s.account_type or kinds.get((s.issuer, s.account_last4)) or \
            ("card" if s.issuer in CARD_ISSUERS else "bank")
        acct = ledger_account(s.issuer, s.account_last4, kind)
        native = -s.new_balance if kind == "card" else s.new_balance
        out.append((_date(s.period_end) + timedelta(days=1), acct, native,
                    s.currency or base, s))
    return out


def _openings(statements, kinds, base):
    """The first reconciled statement per account fixes where the account starts."""
    first: dict[str, tuple] = {}
    for s in sorted(statements, key=lambda s: s.period_start or "9999"):
        if s.reconciled != 1 or s.previous_balance is None or not s.period_start:
            continue
        kind = s.account_type or kinds.get((s.issuer, s.account_last4)) or \
            ("card" if s.issuer in CARD_ISSUERS else "bank")
        acct = ledger_account(s.issuer, s.account_last4, kind)
        if acct in first:
            continue
        native = -s.previous_balance if kind == "card" else s.previous_balance
        first[acct] = (_date(s.period_start), native, s.currency or base)
    return first


def beancount(txns, statements, base, balances=False) -> str:
    kinds = account_kinds(statements)
    lines = [f'option "title" "finances export"',
             f'option "operating_currency" "{base}"', ""]
    accounts: dict[str, date] = {}
    body = []
    for t in txns:
        acct = ledger_account(t.issuer, t.account_last4, kind_of(t.issuer, t.account_last4, kinds))
        other = counter_account(t)
        d = _date(t.date)
        for a in (acct, other):
            accounts[a] = min(accounts.get(a, d), d)
        cur = t.currency or base
        body += [f"{t.date} * {_q(t.merchant)} {_q(t.description)}",
                 f"  source: {_q(t.source_file)}",
                 f"  {other:<44} {_amt(t.amount, cur)}",
                 f"  {acct:<44} {_amt(-t.amount, cur)}", ""]
    extra = []
    if balances:
        opens = _openings(statements, kinds, base)
        for acct, (d, native, cur) in opens.items():
            accounts["Equity:Opening-Balances"] = min(
                accounts.get("Equity:Opening-Balances", d), d - timedelta(days=1))
            accounts[acct] = min(accounts.get(acct, d), d - timedelta(days=1))
            extra += [f"{(d - timedelta(days=1)).isoformat()} pad {acct} Equity:Opening-Balances",
                      f"{d.isoformat()} balance {acct} {_amt(native, cur)}", ""]
        for d, acct, native, cur, s in _assertions(statements, kinds, base):
            if acct in accounts:
                extra.append(f"{d.isoformat()} balance {acct} {_amt(native, cur)}"
                             f"  ; {s.source_file}")
        extra.append("")
    for a, d in sorted(accounts.items()):
        lines.append(f"{(d - timedelta(days=1)).isoformat()} open {a}")
    return "\n".join(lines + [""] + extra + body)


def hledger(txns, statements, base, balances=False) -> str:
    kinds = account_kinds(statements)
    out = [f"; finances export, base currency {base}", ""]
    if balances:
        for acct, (d, native, cur) in _openings(statements, kinds, base).items():
            out += [f"{(d - timedelta(days=1)).isoformat()} Opening balance",
                    f"    {acct:<44} = {_amt(native, cur)}",
                    "    Equity:Opening-Balances", ""]
    entries = []
    for t in txns:
        acct = ledger_account(t.issuer, t.account_last4, kind_of(t.issuer, t.account_last4, kinds))
        cur = t.currency or base
        desc = t.merchant.replace(";", ",")
        entries.append((t.date, 0, [
            f"{t.date} {desc}  ; {t.description.replace(';', ',')}",
            f"    {counter_account(t):<44} {_amt(t.amount, cur)}",
            f"    {acct:<44} {_amt(-t.amount, cur)}", ""]))
    if balances:
        for d, acct, native, cur, s in _assertions(statements, kinds, base):
            entries.append((d.isoformat(), 1, [
                f"{d.isoformat()} Balance per {s.source_file}",
                f"    {acct:<44} 0 {cur} = {_amt(native, cur)}", ""]))
    for _, _, block in sorted(entries, key=lambda e: (e[0], e[1])):
        out += block
    return "\n".join(out)


# --------------------------------------------------------------------------
# Budgeting apps, one CSV per account
# --------------------------------------------------------------------------

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "account").strip("-").lower()


def per_account(txns) -> dict[tuple[str, str], list[Transaction]]:
    groups = defaultdict(list)
    for t in txns:
        groups[(t.issuer, t.account_last4)].append(t)
    return groups


def ynab_rows(txns):
    for t in txns:
        yield {"Date": _date(t.date).strftime("%m/%d/%Y"), "Payee": t.merchant,
               "Memo": t.description,
               "Outflow": f"{t.amount:.2f}" if t.amount > 0 else "",
               "Inflow": f"{-t.amount:.2f}" if t.amount < 0 else ""}


def actual_rows(txns):
    for t in txns:
        yield {"Date": t.date, "Payee": t.merchant, "Notes": t.description,
               "Category": t.category, "Amount": f"{-t.amount:.2f}"}


APPS = {"ynab": (["Date", "Payee", "Memo", "Outflow", "Inflow"], ynab_rows),
        "actual": (["Date", "Payee", "Notes", "Category", "Amount"], actual_rows)}


def write_per_account(fmt: str, txns, out_dir: str) -> list[str]:
    cols, rows = APPS[fmt]
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for (issuer, last4), items in sorted(per_account(txns).items()):
        path = os.path.join(out_dir, f"{fmt}-{_slug(issuer)}-{last4 or 'account'}.csv")
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows(items))
        written.append(path)
    return written


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_OUT = {"beancount": "finances.beancount", "hledger": "finances.journal",
               "ledger": "finances.ledger", "ynab": "exports", "actual": "exports",
               "csv": "transactions.csv"}


def select(txns: list[Transaction], start: str | None, end: str | None,
           account: str | None) -> list[Transaction]:
    out = [t for t in txns
           if (not start or t.date >= start) and (not end or t.date <= end)
           and (not account or t.account_last4 == account)]
    return sorted(out, key=lambda t: (t.date, t.source_file, t.seq))


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances export",
                                 description="Export the store for other money tools.")
    ap.add_argument("format", choices=FORMATS)
    ap.add_argument("-o", "--output", help="file, or folder for ynab/actual")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--start", help="first date, YYYY-MM-DD")
    ap.add_argument("--end", help="last date, YYYY-MM-DD")
    ap.add_argument("--account", help="only this account's last four digits")
    ap.add_argument("--balances", action="store_true",
                    help="beancount/ledger: assert every reconciled statement's balance")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"finances export: {args.db} does not exist", file=sys.stderr)
        return 1
    out = args.output or DEFAULT_OUT[args.format]
    with FinanceDB(args.db) as db:
        txns = select(db.transactions(), args.start, args.end, args.account)
        statements = db.statement_models()
        base = base_currency(db)
    if args.account:
        statements = [s for s in statements if s.account_last4 == args.account]
    if not txns:
        print("finances export: no transactions match", file=sys.stderr)
        return 1

    if args.format in APPS:
        for path in write_per_account(args.format, txns, out):
            print(f"  wrote {path}")
        print(f"{len(txns)} transactions. Import each file into the matching account.")
        return 0

    if args.format == "csv":
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=Transaction.columns())
            w.writeheader()
            w.writerows(t.to_row() for t in txns)
    else:
        render = beancount if args.format == "beancount" else hledger
        with open(out, "w") as fh:
            fh.write(render(txns, statements, base, balances=args.balances))
    print(f"Wrote {len(txns)} transactions -> {out}")
    if args.format == "beancount":
        print(f"Check it with:  bean-check {out}")
    elif args.format in ("hledger", "ledger"):
        print(f"Check it with:  hledger -f {out} check   (or ledger -f {out} balance)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
