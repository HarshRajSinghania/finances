#!/usr/bin/env python3
"""
wrapped.py — your year in money, the way a music app does your year in songs.

Everything here is a plain count or sum over one calendar year of the store:
where the money went, the merchant you could not stay away from, the month
that got away from you, the subscriptions that quietly billed all year. It is
the same data as the dashboard, framed so you might actually look at it, and
reported with how much of it traces back to a statement that reconciled.

    finances wrapped               the latest year in the store
    finances wrapped --year 2025

Standard library only.
"""

from __future__ import annotations

import argparse
import calendar
import sys
from collections import Counter, defaultdict

from finances.database import DEFAULT_DB
from finances.engine import Engine
from finances.utils import fmt_money, merchant_key


def years(eng: Engine) -> list[int]:
    return sorted({int(t.date[:4]) for t in eng.spend() if t.date[:4].isdigit()})


def wrapped(eng: Engine, year: int | None = None) -> dict:
    available = years(eng)
    if not available:
        return {"year": year, "years": [], "empty": True}
    year = year or available[-1]
    ys = str(year)
    spend = [t for t in eng.spend() if t.date.startswith(ys)]
    income = [t for t in eng.income() if t.date.startswith(ys)]
    if not spend:
        return {"year": year, "years": available, "empty": True}

    total = sum(t.amount for t in spend)
    months = sorted({t.month for t in spend})

    by_cat: dict[str, float] = defaultdict(float)
    cat_n: Counter = Counter()
    by_merchant: dict[str, float] = defaultdict(float)
    visits: Counter = Counter()
    names: dict[str, str] = {}
    by_month: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    by_weekday: dict[int, float] = defaultdict(float)
    for t in spend:
        by_cat[t.category] += t.amount
        cat_n[t.category] += 1
        k = merchant_key(t.merchant) or t.merchant
        by_merchant[k] += t.amount
        visits[k] += 1
        names.setdefault(k, t.merchant)
        by_month[t.month] += t.amount
        by_day[t.date] += t.amount
        if t.d:
            by_weekday[t.d.weekday()] += t.amount

    # Merchants seen this year and never before it.
    before = {merchant_key(t.merchant) or t.merchant for t in eng.spend() if t.date < ys}
    new_merchants = [k for k in by_merchant if k not in before]

    biggest = max(spend, key=lambda t: t.amount)
    top_day = max(by_day.items(), key=lambda kv: kv[1])
    top_month = max(by_month.items(), key=lambda kv: kv[1])
    low_month = min(by_month.items(), key=lambda kv: kv[1])
    favourite = visits.most_common(1)[0]

    from finances.budgets import cancellable
    subs = [r for r in eng.subscriptions()
            if r.first_seen.year <= year <= r.last_seen.year and cancellable(r)]
    sub_paid = 0.0
    for t in spend:
        if any(merchant_key(t.merchant) == r.key for r in subs):
            sub_paid += t.amount

    prev = [t for t in eng.spend() if t.date.startswith(str(year - 1))]
    prev_months = {t.month for t in prev}
    # Only compare like with like: the same calendar months in both years.
    same = [m for m in months if f"{year - 1}{m[4:]}" in prev_months]
    vs_last = None
    if same:
        now_same = sum(by_month[m] for m in same)
        then_same = sum(t.amount for t in prev if f"{year}{t.month[4:]}" in same)
        if then_same > 0:
            vs_last = {"months": len(same), "now": round(now_same, 2),
                       "then": round(then_same, 2),
                       "pct": round((now_same - then_same) / then_same * 100, 1)}

    stmts = {s.source_file: s for s in eng.statements()}
    traced = sum(1 for t in spend if (s := stmts.get(t.source_file)) and s.reconciled == 1)
    earned = sum(-t.amount for t in income)

    return {
        "year": year,
        "years": available,
        "empty": False,
        "months": months,
        "fullYear": len(months) == 12,
        "total": round(total, 2),
        "transactions": len(spend),
        "perDay": round(total / (365 + calendar.isleap(year)), 2),
        "perMonth": round(total / len(months), 2),
        "income": round(earned, 2) if income else None,
        "kept": round(earned - total, 2) if income else None,
        "topCategories": [
            {"category": c, "total": round(v, 2), "share": round(v / total * 100, 1),
             "count": cat_n[c]}
            for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])[:6]],
        "topMerchants": [
            {"name": names[k], "total": round(v, 2), "visits": visits[k]}
            for k, v in sorted(by_merchant.items(), key=lambda kv: -kv[1])[:5]],
        "favourite": {"name": names[favourite[0]], "visits": favourite[1],
                      "total": round(by_merchant[favourite[0]], 2)},
        "biggest": {"name": biggest.merchant, "amount": round(biggest.amount, 2),
                    "date": biggest.date, "category": biggest.category},
        "biggestDay": {"date": top_day[0], "total": round(top_day[1], 2)},
        "biggestMonth": {"month": top_month[0], "total": round(top_month[1], 2)},
        "quietestMonth": {"month": low_month[0], "total": round(low_month[1], 2)},
        "byMonth": [{"month": m, "total": round(by_month[m], 2)} for m in months],
        "byDay": [{"date": d, "total": round(v, 2)} for d, v in sorted(by_day.items())],
        "weekday": calendar.day_name[max(by_weekday, key=by_weekday.get)] if by_weekday else None,
        "newMerchants": len(new_merchants),
        "merchants": len(by_merchant),
        "subscriptions": {"count": len(subs), "paid": round(sub_paid, 2),
                          "perYear": round(sum(r.per_year for r in subs), 2)},
        "vsLastYear": vs_last,
        "traced": round(traced / len(spend) * 100, 1),
    }


def _month_name(ym: str) -> str:
    return calendar.month_name[int(ym[5:7])]


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances wrapped",
                                 description="Your year in money.")
    ap.add_argument("--year", type=int)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    with Engine(args.db) as eng:
        w = wrapped(eng, args.year)
    if w.get("empty"):
        print(f"No spending recorded for {w.get('year') or 'any year'}.")
        return 1
    m = fmt_money
    span = "" if w["fullYear"] else (f"  ({_month_name(w['months'][0])}–"
                                     f"{_month_name(w['months'][-1])} only)")
    print(f"\n  Your {w['year']} in money{span}\n")
    print(f"  You spent {m(w['total'])} across {w['transactions']} purchases "
          f"at {w['merchants']} places — {m(w['perDay'], 2)} a day.")
    if w["kept"] is not None:
        print(f"  You earned {m(w['income'])} and kept {m(w['kept'])}.")
    print("\n  Where it went")
    for c in w["topCategories"]:
        print(f"    {c['category']:<18} {m(c['total']):>10}  {c['share']:>4.0f}%")
    f = w["favourite"]
    print(f"\n  Your regular: {f['name']}, {f['visits']} visits, {m(f['total'])}.")
    b = w["biggest"]
    print(f"  Biggest single purchase: {b['name']}, {m(b['amount'], 2)} on {b['date']}.")
    print(f"  Most expensive day: {w['biggestDay']['date']}, {m(w['biggestDay']['total'])}.")
    print(f"  Biggest month: {_month_name(w['biggestMonth']['month'])} "
          f"({m(w['biggestMonth']['total'])}); quietest: "
          f"{_month_name(w['quietestMonth']['month'])} ({m(w['quietestMonth']['total'])}).")
    if w["weekday"]:
        print(f"  You spend the most on {w['weekday']}s.")
    s = w["subscriptions"]
    if s["count"]:
        print(f"  {s['count']} subscriptions billed you {m(s['paid'])} this year.")
    print(f"  {w['newMerchants']} places were new this year.")
    if w["vsLastYear"]:
        v = w["vsLastYear"]
        word = "more" if v["pct"] > 0 else "less"
        print(f"  Over the same {v['months']} months you spent {abs(v['pct']):.0f}% "
              f"{word} than the year before.")
    print(f"\n  {w['traced']:.0f}% of these purchases trace to a statement that reconciles.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
