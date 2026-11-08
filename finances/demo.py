#!/usr/bin/env python3
"""
demo.py — a year of made-up statements, so the tools can be tried before
trusting them with real ones.

    finances demo                       # -> demo.db
    finances demo --db other.db --force
    finances web --db demo.db --open     # then look around

Nobody should have to hand over their bank statements to find out whether a
tool is worth using. This writes twelve months of a checking account and a
credit card into a separate store: paychecks, rent, subscriptions (one of which
raises its price), groceries, a holiday spike and a refund. Every number is
invented and seeded, so two runs produce the same store.

The statements are not marked as reconciled by hand. Each one carries printed
totals the way a real statement does, and goes through the same
``parse.reconcile`` and the same categorizer as a parsed PDF — so ``check``
on the demo is exercising the real thing, not a stub.

It writes to ``demo.db``, never to your real store, and refuses to overwrite a
store that holds anything other than demo statements.
"""

from __future__ import annotations

import argparse
import calendar
import os
import random
import sys
from collections import Counter
from datetime import date, timedelta


from finances.categorizer import Categorizer, categorize_rows  # noqa: E402
from finances.database import FinanceDB                        # noqa: E402
from finances.models import Statement, Transaction             # noqa: E402
from finances.parse import clean_merchant, reconcile           # noqa: E402

DEMO_DB = "demo.db"
PREFIX = "demo-"
MONTHS = 12

CARD = ("Amex", "1005")
BANK = ("Chase", "4321")
CREDIT_LIMIT = 9000.00


def month_starts(n: int, today: date) -> list[date]:
    """The first day of each of the last ``n`` complete months."""
    first = today.replace(day=1)
    out = []
    for _ in range(n):
        first = (first - timedelta(days=1)).replace(day=1)
        out.append(first)
    return out[::-1]


def last_day(m: date) -> date:
    return m.replace(day=calendar.monthrange(m.year, m.month)[1])


def weekday_before(d: date) -> date:
    """Payroll on a weekend pays the Friday before, as real payroll does."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def card_month(rng: random.Random, m: date, i: int) -> list[tuple[date, str, float]]:
    end = last_day(m)
    rows: list[tuple[date, str, float]] = []

    def on(day: int) -> date:
        return m.replace(day=min(day, end.day))

    def some(k: int) -> list[date]:
        return [on(rng.randint(1, end.day)) for _ in range(k)]

    # Subscriptions, on fixed days. Netflix raises its price partway through the
    # year, which is what the subscription and CPI views exist to notice.
    rows.append((on(3), "NETFLIX.COM 866-579-7172 CA", 15.49 if i < 5 else 17.99))
    rows.append((on(9), "SPOTIFY USA 877-778-1161 NY", 11.99))
    rows.append((on(14), "APPLE.COM/BILL 866-712-7753 CA", 2.99))
    rows.append((on(21), "PLANET FITNESS 844-880-7180 NH", 24.99))

    for d in some(rng.randint(5, 7)):
        rows.append((d, rng.choice(["TRADER JOE'S #542 NEW YORK NY",
                                    "WHOLE FOODS MKT 10289 NEW YORK NY"]),
                     round(rng.uniform(32, 135), 2)))
    for d in some(rng.randint(8, 13)):
        rows.append((d, "SQ *BLUE BOTTLE COFFEE OAKLAND CA", round(rng.uniform(4.5, 7.25), 2)))
    for d in some(rng.randint(4, 7)):
        rows.append((d, rng.choice(["TST* JOE'S PIZZA - BLEECKER NEW YORK NY",
                                    "TST*SWEETGREEN - FLATIRON NEW YORK NY",
                                    "CHIPOTLE 2291 NEW YORK NY"]),
                     round(rng.uniform(12, 58), 2)))
    for d in some(rng.randint(1, 3)):
        rows.append((d, "DOORDASH*THAI VILLA 855-973-1040 CA", round(rng.uniform(24, 46), 2)))
    for d in some(rng.randint(2, 4)):
        rows.append((d, "UBER *TRIP HELP.UBER.COM CA", round(rng.uniform(9, 31), 2)))
    for d in some(2):
        rows.append((d, "SHELL OIL 57442163 NEW YORK NY", round(rng.uniform(36, 58), 2)))
    for d in some(rng.randint(2, 4)):
        rows.append((d, "AMAZON MKTPLACE PMTS AMZN.COM/BILL WA", round(rng.uniform(11, 92), 2)))

    # The shape of a real year: a summer trip, a holiday spike, one refund.
    if m.month == 7:
        rows.append((on(6), "DELTA AIR LINES ATLANTA GA", 412.60))
        rows.append((on(19), "AIRBNB * HMQ8D2 SAN FRANCISCO CA", 538.00))
    if m.month in (11, 12):
        rows.append((on(rng.randint(20, 28)), "BEST BUY 00012 NEW YORK NY",
                     round(rng.uniform(180, 420), 2)))
        rows.append((on(rng.randint(1, 18)), "TARGET 00023187 NEW YORK NY",
                     round(rng.uniform(60, 160), 2)))
    if i == 4:
        rows.append((on(17), "AMAZON MKTPLACE PMTS AMZN.COM/BILL WA", -46.19))
    return rows


def bank_month(rng: random.Random, m: date) -> list[tuple[date, str, float]]:
    end = last_day(m)

    def on(day: int) -> date:
        return m.replace(day=min(day, end.day))

    pay = "ACME CORP PAYROLL DIRECT DEP PPD ID: 9876543210"
    rows = [
        (weekday_before(on(15)), pay, -2750.00),
        (weekday_before(end), pay, -2750.00),
        (on(1), "GREYSTAR RENT PAYMENT", 1950.00),
        (on(5), "GEICO *AUTO 800-841-3000 DC", 112.40),
        (on(12), "COMCAST CABLE COMM 800-266-2278 PA", 79.99),
        (on(20), "VERIZON WIRELESS PAYMENTS", 65.00),
        # Electricity follows the weather.
        (on(24), "CON EDISON PAYMENT", round(55 + 50 * abs(6.5 - m.month) / 5.5
                                             + rng.uniform(-8, 8), 2)),
        (on(16), "Online Transfer To Sav ...9876 Transaction#: 12345678901", 1250.00),
        (on(rng.randint(2, 27)), "ATM Withdrawal 120 Main St New York NY Card 1234",
         float(rng.choice([60, 80, 100]))),
    ]
    if rng.random() < 0.6:
        rows.append((on(rng.randint(2, 27)), "Zelle Payment To Alex Chen Jpm42B81Rr9K",
                     round(rng.uniform(20, 60), 2)))
    if rng.random() < 0.4:
        rows.append((on(rng.randint(2, 27)), "Zelle Payment From Jordan Rivera Xk4Q8Mz2Pa71",
                     -round(rng.uniform(20, 60), 2)))
    return rows


def as_transactions(rows, issuer, last4, period, source) -> list[Transaction]:
    seen: Counter = Counter()
    out = []
    for d, desc, amt in sorted(rows, key=lambda r: (r[0], r[1])):
        key = (d, desc, round(amt, 2))
        out.append(Transaction(date=d.isoformat(), description=desc, amount=round(amt, 2),
                               issuer=issuer, account_last4=last4,
                               statement_period=period, source_file=source,
                               seq=seen[key]))
        seen[key] += 1
    return out


def build(today: date, seed: int = 7) -> tuple[list[Transaction], list[Statement]]:
    rng = random.Random(seed)
    txns: list[Transaction] = []
    stmts: list[Statement] = []
    card_balance, bank_balance = 1240.18, 3800.00
    invested = 24_500.00

    for i, m in enumerate(month_starts(MONTHS, today)):
        end = last_day(m)
        period = f"{m.isoformat()} to {end.isoformat()}"
        tag = m.strftime("%Y-%m")

        # The card is paid in full from checking, so the same payment appears
        # on both statements: a credit on the card, a debit from the bank.
        paid = round(card_balance, 2)
        pay_day = m.replace(day=8)

        card_rows = card_month(rng, m, i)
        card_rows.append((pay_day, "AUTOPAY PAYMENT - THANK YOU", -paid))
        bank_rows = bank_month(rng, m)
        bank_rows.append((pay_day, "American Express ACH Pmt M1234 Web ID: 2000000001", paid))

        for (issuer, last4), rows, kind in ((CARD, card_rows, "amex"),
                                            (BANK, bank_rows, "chase-checking")):
            source = f"{PREFIX}{kind}-{tag}.pdf"
            ts = as_transactions(rows, issuer, last4, period, source)
            charges = round(sum(t.amount for t in ts if t.amount > 0), 2)
            credits = round(sum(-t.amount for t in ts if t.amount < 0), 2)

            st = Statement(source_file=source, issuer=issuer, account_last4=last4,
                           statement_period=period, period_start=m.isoformat(),
                           period_end=end.isoformat(),
                           # Printed totals, as the statement would show them.
                           purchases=charges, payments=-credits,
                           charges_extracted=charges, credits_extracted=credits)
            if kind == "amex":
                st.previous_balance = round(card_balance, 2)
                card_balance = round(card_balance + charges - credits, 2)
                st.new_balance = card_balance
                st.credit_limit = CREDIT_LIMIT
                st.available_credit = round(CREDIT_LIMIT - card_balance, 2)
                st.utilization_pct = round(100 * card_balance / CREDIT_LIMIT, 1)
                st.purchase_apr = 24.99
                st.minimum_payment = round(max(35.0, card_balance * 0.01), 2)
                st.payment_due = (end + timedelta(days=25)).isoformat()
                st.interest_charged = 0.0
                st.fees_charged = 0.0
            else:
                st.previous_balance = round(bank_balance, 2)
                bank_balance = round(bank_balance - charges + credits, 2)
                st.new_balance = bank_balance

            reconcile(st, ts)
            txns += ts
            stmts.append(st)

        # A brokerage account: read for its value, which drifts with the market
        # and a monthly contribution, never for its trades.
        start = invested
        invested = round(invested * (1 + rng.uniform(-0.03, 0.04)) + 250, 2)
        stmts.append(Statement(
            source_file=f"{PREFIX}fidelity-{tag}.pdf", issuer="Fidelity", account_last4="7781",
            statement_period=period, period_start=m.isoformat(), period_end=end.isoformat(),
            previous_balance=start, new_balance=invested, account_type="investment",
            reconcile_note="Investment account: its value is read, not its trades, "
                           "so there is nothing to reconcile."))

    # The same categorizer a parsed statement goes through, without anything
    # you have taught it: the demo should look the same on every machine.
    results = categorize_rows(txns, Categorizer(learned_path=None))
    for t, res in zip(txns, results):
        t.category = res.category
        t.merchant = res.clean or clean_merchant(t.description)
    return txns, stmts


def main() -> int:
    ap = argparse.ArgumentParser(description="Write a year of made-up statements to a "
                                             "separate store, to try the tools on.")
    ap.add_argument("--db", default=DEMO_DB, help=f"where to write it (default {DEMO_DB})")
    ap.add_argument("--force", action="store_true", help="replace an existing demo store")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if os.path.exists(args.db):
        with FinanceDB(args.db) as db:
            real = [f for f in db.source_files() if not f.startswith(PREFIX)]
        if real:
            print(f"demo: {args.db} holds {len(real)} real statement(s) "
                  f"(e.g. {real[0]}). Not touching it.", file=sys.stderr)
            return 1
        if not args.force:
            print(f"demo: {args.db} already exists. Use --force to rebuild it.",
                  file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(args.db + suffix):
                os.remove(args.db + suffix)

    txns, stmts = build(date.today(), args.seed)
    with FinanceDB(args.db) as db:
        db.upsert_transactions(txns)
        db.upsert_statements(stmts)

    checked = [s for s in stmts if s.checkable]
    ok = sum(1 for s in checked if s.reconciled == 1)
    uncategorized = sum(1 for t in txns if t.category == "Uncategorized")
    print(f"Wrote {len(txns):,} made-up transactions across {len(stmts)} statements "
          f"to {args.db}")
    print(f"  reconciled     {ok}/{len(checked)}")
    print(f"  uncategorized  {uncategorized}")
    print("\nTry it:")
    flag = "" if args.db == os.environ.get("FINANCES_DB") else f" --db {args.db}"
    print(f"  finances web{flag} --open")
    print(f"  finances check{flag}")
    print(f"  finances cashflow{flag}")
    print(f"  finances ask --example top-merchants{flag}")
    return 0 if ok == len(checked) else 1


if __name__ == "__main__":
    sys.exit(main())
