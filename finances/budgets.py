#!/usr/bin/env python3
"""
budgets.py — a monthly limit per category, and the subscriptions worth cancelling.

Budgets are measured against the latest month in the store, not the calendar:
statements arrive weeks after a month closes, so "this month" is the newest one
the data covers. A month still in progress is projected to its end at the pace
so far, which is what turns a limit into a warning before it is broken.

The cancel list ranks recurring charges by what dropping them would save, and
says why each one is there: a price that went up, three services doing the job
of one. Mark one as kept and it stops being suggested; mark one as cancelled
and any charge after that date is flagged, because a cancellation that did not
take is the most expensive kind.

    finances budget                     this month against every limit
    finances budget set Dining 300
    finances budget rm Dining
    finances budget suggest             limits from your own averages
    finances budget cancel              the cancel list
    finances budget keep netflix        stop suggesting it
    finances budget cancelled netflix   flag it if it bills again

Settings live in the store, so the dashboard and the CLI share them.
"""

from __future__ import annotations

import argparse
import calendar
import math
import sys
from collections import defaultdict
from datetime import date

from finances.database import DEFAULT_DB
from finances.engine import Engine
from finances.utils import fmt_money, merchant_key

BUDGETS_KEY = "budgets"
MARKS_KEY = "subscription_marks"

# Categories a monthly limit makes sense for: not transfers, not income.
SKIP = {"Payment", "Transfers/Cash", "Income", "Credits/Refunds", "Uncategorized"}

# Bills nobody cancels to save money. Rent repeats every month at a steady price,
# which makes it look exactly like a subscription, and suggesting you drop it
# makes the whole list look careless. Phone and internet stay in: people do
# switch plans and cut cable.
KEEP_OFF_CANCEL_LIST = {"Rent/Housing", "Insurance", "Utilities", "Health",
                        "Pharmacy", "Education", "Childcare", "Taxes",
                        "Debt Payment", "Fees/Interest", "Transport", "Gas/Auto"}


# Fixed bills: a budget on rent is a number you already know.
FIXED = {"Rent/Housing", "Insurance", "Taxes", "Debt Payment", "Childcare", "Education"}


def cancellable(r) -> bool:
    return r.category not in KEEP_OFF_CANCEL_LIST


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def get_budgets(eng: Engine) -> dict[str, float]:
    raw = (eng.db.get_setting(BUDGETS_KEY) or {}) if eng.db else {}
    return {k: float(v) for k, v in raw.items() if v is not None}


def set_budget(eng: Engine, category: str, limit: float | None):
    b = get_budgets(eng)
    if limit is None or limit <= 0:
        b.pop(category, None)
    else:
        b[category] = round(float(limit), 2)
    eng.db.set_setting(BUDGETS_KEY, b)


def _nice(v: float) -> float:
    """A limit someone would actually type: round up to 10, 25 or 50."""
    step = 10 if v < 200 else 25 if v < 1000 else 50
    return float(math.ceil(v / step) * step)


def status(eng: Engine) -> dict:
    rng = eng.dates()
    budgets = get_budgets(eng)
    if not rng:
        return {"month": None, "budgets": [], "suggestions": []}
    last = rng[1]
    month = last.strftime("%Y-%m")
    days = calendar.monthrange(last.year, last.month)[1]
    elapsed = last.day
    in_progress = elapsed < days - 1

    prev_y, prev_m = (last.year, last.month - 1) if last.month > 1 else (last.year - 1, 12)
    prev = f"{prev_y}-{prev_m:02d}"

    this, before = defaultdict(float), defaultdict(float)
    for t in eng.spend():
        if t.month == month:
            this[t.category] += t.amount
        elif t.month == prev:
            before[t.category] += t.amount
    avg = eng.by_category_monthly()

    rows = []
    for cat, limit in sorted(budgets.items()):
        spent = this.get(cat, 0.0)
        projected = spent / elapsed * days if in_progress and elapsed else spent
        state = ("over" if spent > limit else
                 "pace" if projected > limit * 1.02 else "ok")
        rows.append({"category": cat, "limit": limit, "spent": round(spent, 2),
                     "projected": round(projected, 2), "lastMonth": round(before.get(cat, 0.0), 2),
                     "average": round(avg.get(cat, 0.0), 2),
                     "left": round(limit - spent, 2), "state": state,
                     "pct": round(spent / limit * 100, 1) if limit else 0.0})
    rows.sort(key=lambda r: ({"over": 0, "pace": 1, "ok": 2}[r["state"]], -r["pct"]))

    suggestions = [{"category": c, "average": round(v, 2), "suggested": _nice(v * 1.05)}
                   for c, v in sorted(avg.items(), key=lambda kv: -kv[1])
                   if c not in budgets and c not in SKIP | FIXED and v >= 20][:8]
    return {"month": month, "asOf": last.isoformat(), "inProgress": in_progress,
            "daysElapsed": elapsed, "daysInMonth": days,
            "budgets": rows, "suggestions": suggestions,
            "totalLimit": round(sum(budgets.values()), 2),
            "totalSpent": round(sum(this.get(c, 0.0) for c in budgets), 2)}


# --------------------------------------------------------------------------
# The cancel list
# --------------------------------------------------------------------------

def get_marks(eng: Engine) -> dict[str, dict]:
    return (eng.db.get_setting(MARKS_KEY) or {}) if eng.db else {}


def mark(eng: Engine, key: str, state: str | None, on: date | None = None):
    marks = get_marks(eng)
    if state is None:
        marks.pop(key, None)
    else:
        marks[key] = {"status": state, "at": (on or date.today()).isoformat()}
    eng.db.set_setting(MARKS_KEY, marks)


def resolve_key(eng: Engine, name: str) -> str | None:
    """A subscription's key from something a person would type."""
    want = merchant_key(name) or name.upper()
    subs = [r for r in eng.subscriptions() if cancellable(r)]
    for r in subs:
        if r.key == want:
            return r.key
    hits = [r.key for r in subs if want in r.key or name.lower() in r.name.lower()]
    return hits[0] if len(hits) == 1 else None


def review(eng: Engine) -> dict:
    """Every subscription, sorted into suggest / kept / cancelled / gone."""
    rng = eng.dates()
    as_of = rng[1] if rng else date.today()
    marks = get_marks(eng)
    subs = [r for r in eng.subscriptions() if cancellable(r)]

    by_cat = defaultdict(list)
    for r in subs:
        if not r.is_stale(as_of):
            by_cat[r.category].append(r)

    out = {"candidates": [], "kept": [], "cancelled": [], "stillCharging": [], "gone": []}
    for r in subs:
        m = marks.get(r.key) or {}
        reasons = []
        score = r.per_year
        if r.price_pct >= 5 and r.price_change > 0:
            reasons.append(f"price up {r.price_pct:.0f}% since it started "
                           f"(+{fmt_money(r.price_change, 2)} a charge)")
            score *= 1.5
        peers = by_cat.get(r.category, [])
        if len(peers) >= 2 and r in peers:
            together = sum(p.per_month for p in peers)
            reasons.append(f"one of {len(peers)} {r.category.lower()} charges, "
                           f"{fmt_money(together)}/mo together")
            score *= 1.25
        if r.cadence == "annual":
            reasons.append(f"renews yearly; next around {r.next_expected}")
        reasons.append(f"{fmt_money(r.per_year)} a year")
        item = {"r": r, "reasons": reasons, "score": score, "mark": m}

        if m.get("status") == "cancelled":
            if r.last_seen.isoformat() > m.get("at", ""):
                item["reasons"] = [f"charged on {r.last_seen} after you cancelled on "
                                   f"{m['at']}"] + reasons
                out["stillCharging"].append(item)
            else:
                out["cancelled"].append(item)
        elif m.get("status") == "keep":
            out["kept"].append(item)
        elif r.is_stale(as_of):
            out["gone"].append(item)
        else:
            out["candidates"].append(item)

    for k in out:
        out[k].sort(key=lambda i: -i["score"])
    out["savingsPerYear"] = round(sum(i["r"].per_year for i in out["candidates"]), 2)
    out["savedPerYear"] = round(sum(i["r"].per_year for i in out["cancelled"]), 2)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_status(s: dict):
    if not s["month"]:
        print("No spending in the store yet.")
        return
    print(f"\n  Budgets for {s['month']}"
          + (f"  (day {s['daysElapsed']} of {s['daysInMonth']}, projected to month end)"
             if s["inProgress"] else "") + "\n")
    if not s["budgets"]:
        print("  No budgets set. Try:  finances budget suggest")
    for b in s["budgets"]:
        flag = {"over": "OVER", "pace": "on pace to go over", "ok": ""}[b["state"]]
        print(f"  {b['category']:<18} {fmt_money(b['spent']):>9} of {fmt_money(b['limit']):<8}"
              f" {b['pct']:>5.0f}%  {flag}")
    if s["suggestions"]:
        print("\n  Suggested from your averages:")
        for g in s["suggestions"]:
            print(f"    finances budget set {g['category']!r:<18} {g['suggested']:.0f}"
                  f"    (you average {fmt_money(g['average'])})")
    print()


def _print_review(rv: dict):
    def line(i):
        r = i["r"]
        return f"  {r.name[:28]:<30} {fmt_money(r.per_month, 2):>9}/mo   " + "; ".join(i["reasons"])
    if rv["stillCharging"]:
        print("\n  Still charging after you cancelled:")
        for i in rv["stillCharging"]:
            print(line(i))
    print(f"\n  Worth a look — {fmt_money(rv['savingsPerYear'])} a year if all of these went:")
    for i in rv["candidates"]:
        print(line(i))
    if rv["kept"]:
        print("\n  Kept: " + ", ".join(i["r"].name for i in rv["kept"]))
    if rv["cancelled"]:
        print(f"  Cancelled: " + ", ".join(i["r"].name for i in rv["cancelled"])
              + f"  (saving {fmt_money(rv['savedPerYear'])}/yr)")
    if rv["gone"]:
        print("  Stopped billing on their own: " + ", ".join(i["r"].name for i in rv["gone"]))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances budget",
                                 description="Monthly budgets and the cancel list.")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "set", "rm", "suggest", "cancel", "keep",
                             "cancelled", "unmark"])
    ap.add_argument("name", nargs="?", help="category, or subscription name")
    ap.add_argument("amount", nargs="?", type=float)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    with Engine(args.db) as eng:
        if args.action == "set":
            if not args.name or args.amount is None:
                ap.error("usage: finances budget set CATEGORY AMOUNT")
            set_budget(eng, args.name, args.amount)
        elif args.action == "rm":
            set_budget(eng, args.name or "", None)
        elif args.action == "suggest":
            for g in status(eng)["suggestions"]:
                set_budget(eng, g["category"], g["suggested"])
                print(f"  set {g['category']} to {fmt_money(g['suggested'])}")
        elif args.action in ("keep", "cancelled", "unmark"):
            key = resolve_key(eng, args.name or "")
            if not key:
                print(f"finances budget: no single subscription matches {args.name!r}",
                      file=sys.stderr)
                return 1
            mark(eng, key, None if args.action == "unmark" else
                 ("keep" if args.action == "keep" else "cancelled"))
            print(f"  {key}: {args.action}")
        if args.action in ("cancel", "keep", "cancelled", "unmark"):
            _print_review(review(eng))
        else:
            _print_status(status(eng))
    return 0


if __name__ == "__main__":
    sys.exit(main())
