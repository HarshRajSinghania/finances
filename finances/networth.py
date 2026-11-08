"""
networth.py — what you own minus what you owe, month by month, from statements.

Every statement prints the balance it closed on, so the store already holds a
balance per account per month: bank and investment accounts are what you own,
cards are what you owe. Nothing is estimated in between. A month uses each
account's latest statement up to that month's end; an account whose latest
statement is more than STALE_DAYS old by then is still counted, and said to be
out of date, because dropping it would make net worth jump for no reason.

Accounts only count from their first statement: before it there is nothing to
go on, and a month that is missing an account is marked as partial.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

STALE_DAYS = 45
CARD_ISSUERS = {"Amex", "Discover", "Apple Card", "Synchrony", "Barclays"}


def kind(s) -> str:
    """"card", "investment" or "bank", from what the statement says about itself."""
    if s.account_type in ("card", "investment"):
        return s.account_type
    if (s.credit_limit is not None or s.minimum_payment is not None
            or s.purchase_apr is not None or s.payment_due or s.issuer in CARD_ISSUERS):
        return "card"
    return "bank"


def _month_end(m: str) -> date:
    y, mo = int(m[:4]), int(m[5:7])
    return date(y, mo, monthrange(y, mo)[1])


def _months(first: str, last: str) -> list[str]:
    out, y, m = [], int(first[:4]), int(first[5:7])
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def net_worth(statements) -> dict:
    accounts: dict[tuple, dict] = {}
    for s in statements:
        if s.new_balance is None or not s.period_end:
            continue
        key = (s.issuer, s.account_last4, s.source_file.split("#", 1)[1]
               if "#" in s.source_file and not s.account_last4 else "")
        a = accounts.setdefault(key, {"issuer": s.issuer, "last4": s.account_last4,
                                      "kind": kind(s), "points": {}})
        a["points"][s.period_end] = s.new_balance
    if not accounts:
        return {"months": [], "accounts": [], "latest": None}

    for a in accounts.values():
        a["dates"] = sorted(a["points"])
    first = min(a["dates"][0] for a in accounts.values())[:7]
    last = max(a["dates"][-1] for a in accounts.values())[:7]

    months = []
    for m in _months(first, last):
        end = _month_end(m)
        assets = debts = 0.0
        stale, missing = [], 0
        for a in accounts.values():
            known = [d for d in a["dates"] if d <= end.isoformat()]
            if not known:
                missing += 1
                continue
            bal = a["points"][known[-1]]
            if (end - date.fromisoformat(known[-1])).days > STALE_DAYS:
                stale.append(f"{a['issuer']} {a['last4']}".strip())
            if a["kind"] == "card":
                debts += bal
            else:
                assets += bal
        months.append({"month": m, "assets": round(assets, 2), "debts": round(debts, 2),
                       "net": round(assets - debts, 2), "stale": stale,
                       "partial": missing > 0})

    out_accounts = []
    for a in sorted(accounts.values(), key=lambda a: (a["kind"], a["issuer"], a["last4"])):
        d = a["dates"][-1]
        out_accounts.append({"issuer": a["issuer"], "last4": a["last4"], "kind": a["kind"],
                             "balance": a["points"][d], "asOf": d,
                             "first": a["dates"][0]})
    return {"months": months, "accounts": out_accounts, "latest": months[-1]}
