#!/usr/bin/env python3
"""
planner.py — plan what you're saving for, using your own numbers.

It reads your transactions to work out what you actually have spare each month,
then tells you whether your goals fit in it and when each one lands.

    python planner.py init --source finances.db --income 5000
    python planner.py add "Emergency fund" --target 12000 --priority 1
    python planner.py add "New car" --target 20000 --by 2027-06 --saved 1500
    python planner.py add "Japan trip" --target 4500 --by 2026-11 --priority 3
    python planner.py status
    python planner.py contribute "New car" 500
    python planner.py simulate --monthly 900
    python planner.py simulate --cut Dining=30 --cut Delivery=50
    python planner.py report -o plan.html

--source takes either the shared store (finances.db) or a legacy transactions
CSV; the old --csv spelling still works. The numbers behind a plan — monthly
spend, income, essential spend, surplus, what repeats every month — come from
engine.Engine, so the planner, the dashboard and the auditor cannot quote three
different figures for the same file.

Emergency fund sizing is based on your essential spending, not a rule of thumb:
rent, utilities, groceries, transport, insurance and health, times however many
months you want covered.

Stored in plan.json. Standard library only. Arithmetic on your own statements —
not financial advice.
"""

import argparse
import csv
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# One store, one set of semantics. The Engine owns what counts as spending,
# which months are complete, what repeats and how much of it reconciles; the
# planner only decides what to do with the money that is left over.
from database import DEFAULT_DB  # noqa: E402
from engine import Engine  # noqa: E402
from utils import fmt_money as money, future_value  # noqa: E402

PLAN = "plan.json"


# --------------------------------------------------------------------------
# Money and time
# --------------------------------------------------------------------------

def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


def parse_month(s):
    """Accept 2027-06 or 2027-06-30."""
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().replace(day=1)
        except ValueError:
            continue
    raise ValueError(f"can't read the date '{s}' — use YYYY-MM")


def months_to_target(target, saved, monthly, apy=0.0):
    """How many months to reach the target, with interest if the money earns any."""
    need = target - saved
    if need <= 0:
        return 0
    if monthly <= 0:
        return None
    if apy <= 0:
        return math.ceil(need / monthly)
    r = (1 + apy / 100.0) ** (1 / 12.0) - 1
    n = 0
    bal = saved
    while bal < target and n < 1200:
        bal = bal * (1 + r) + monthly
        n += 1
    return n if bal >= target else None


# future_value lives in utils (shared with the analysis tools).


# --------------------------------------------------------------------------
# Plan file
# --------------------------------------------------------------------------

def load_plan(path=PLAN):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except json.JSONDecodeError:
            print(f"{path} is unreadable, starting fresh")
    return {"goals": [], "monthly_surplus": None, "essential_monthly": None,
            "income": None, "apy": 0.0, "contributions": []}


def save_plan(plan, path=PLAN):
    json.dump(plan, open(path, "w"), indent=1)


def find_goal(plan, name):
    lowered = name.lower()
    exact = [g for g in plan["goals"] if g["name"].lower() == lowered]
    if exact:
        return exact[0]
    part = [g for g in plan["goals"] if lowered in g["name"].lower()]
    if len(part) == 1:
        return part[0]
    if len(part) > 1:
        sys.exit(f"'{name}' matches {', '.join(g['name'] for g in part)}")
    return None


# --------------------------------------------------------------------------
# Reading the real numbers out of the transactions
# --------------------------------------------------------------------------

def open_engine(source, required=True):
    """Open a .db or a .csv through the shared engine.

    Existence is checked here rather than left to sqlite, which would happily
    create an empty store and then report that you have no transactions.
    """
    if not source:
        return None
    if not os.path.exists(source):
        if required:
            sys.exit(f"{source} does not exist. Build it with:  "
                     f"python parse.py PDFs --db {DEFAULT_DB}")
        return None
    return Engine.from_source(source)


def read_finances(source=DEFAULT_DB, income_override=None, eng=None):
    """What comes in, what goes out, what is already promised, and how much of
    it has been checked against a statement.

    All of this used to be re-derived here from a CSV, which is how the planner
    ended up quoting a different monthly average than the dashboard for the same
    file. The Engine owns those definitions now — complete months, spend,
    income, essentials, surplus, recurring charges — and this is only the
    adapter that reshapes them into what the plan file stores.
    """
    owned = eng is None
    eng = eng or open_engine(source)
    try:
        complete = eng.complete_months()
        if not complete:
            sys.exit(f"{source} has no usable transactions")

        # Per-category monthly averages, over the same complete months the
        # Engine averages everything else over, so `simulate --cut` is cutting
        # from the same denominator the surplus was computed with.
        window = set(complete)
        by_cat = defaultdict(float)
        for t in eng.spend():
            if t.date[:7] in window:
                by_cat[t.category] += t.amount
        n = len(complete)

        im = eng.income_by_month()
        earned = sum(im.get(m, 0.0) for m in complete) / n
        return {
            "months": n,
            "monthly_spend": eng.monthly_average(),
            "monthly_income": income_override if income_override else earned,
            "essential_monthly": eng.essential_monthly(),
            "surplus": eng.surplus(income_override or None),
            "committed": eng.committed_monthly(),
            # subscriptions(), not recurring(): the committed figure above
            # excludes interest, and the count beside it has to describe the
            # same set or the two contradict each other on screen.
            "recurring": eng.subscriptions(),
            "coverage": eng.coverage(),
            "by_cat_monthly": {c: v / n for c, v in by_cat.items()},
            "period": f"{complete[0]} to {complete[-1]}",
        }
    finally:
        if owned:
            eng.close()


def trust_note(cov):
    """One line on how far the numbers have been verified, or None if clean.

    A savings plan is only as good as the transactions under it. Projecting a
    finish date to the month off statements that never reconciled is false
    precision, so say it once and let the reader discount accordingly.
    """
    if not cov:
        return None
    if not cov.get("statements"):
        return ("nothing in this source to reconcile against, so these are the "
                "transactions as parsed, never checked against a statement total")
    if cov.get("trusted"):
        return None
    gaps = cov.get("gaps") or 0
    gaps = gaps if isinstance(gaps, int) else len(gaps)
    bits = []
    if cov.get("mismatched"):
        bits.append(f"{cov['mismatched']} disagree with their own totals")
    if cov.get("unchecked"):
        bits.append(f"{cov['unchecked']} never checked")
    if gaps:
        bits.append(f"{gaps} gap(s) in the statement history")
    return (f"{cov.get('pct', 0):.0f}% of {cov['statements']} statements reconcile"
            + (" — " + ", ".join(bits) if bits else "")
            + ". Treat these figures as provisional.")


def committed_of(plan, fin=None):
    """Fixed monthly commitments and the charges behind them.

    Falls back to whatever `init` recorded when the source is not to hand, so
    every command tells the same story about what is already promised.
    """
    fin = fin or {}
    committed = fin.get("committed")
    if committed is None:
        committed = plan.get("committed_monthly") or 0.0
    # Count only what the committed figure actually sums. `recurring` carries
    # every detected subscription including ones that stopped arriving, and
    # printing "$213 across 13 charges" beside a total drawn from 10 of them
    # invites the reader to divide one by the other and get a wrong answer.
    recurring = fin.get("recurring") or []
    live = [r for r in recurring if not getattr(r, "is_stale", None) or
            not r.is_stale(date.today())]
    n = len(live) or (plan.get("committed_count") or 0)
    return committed or 0.0, recurring, n


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

def plan_goals(plan, surplus, today=None):
    """Split the monthly surplus across goals and work out when each lands.

    Strictly in priority order. A priority-1 goal takes what it needs before
    anything else sees a dollar, even if something lower down has a deadline —
    that is what putting it first means. Within one priority level the nearest
    deadline goes first.
    """
    today = today or date.today()
    apy = plan.get("apy", 0.0) or 0.0
    goals = [dict(g) for g in plan["goals"] if not g.get("done")]
    for g in goals:
        g["remaining"] = max(g["target"] - g.get("saved", 0), 0)
        g["deadline_date"] = parse_month(g["by"]) if g.get("by") else None
        g["months_left"] = (months_between(today.replace(day=1), g["deadline_date"])
                            if g["deadline_date"] else None)
        g["required"] = (g["remaining"] / max(g["months_left"], 1)
                         if g["months_left"] is not None else None)

    ordered = sorted(goals, key=lambda g: (g.get("priority", 5),
                                           g["months_left"] if g["months_left"]
                                           is not None else 9999))

    # This month's split, which is what you actually need to move.
    left = surplus if surplus is not None else 0.0
    for g in ordered:
        if g["remaining"] <= 0:
            g["allocated"] = 0.0
            g["shortfall"] = 0.0
            continue
        if g.get("monthly"):                      # you set the amount yourself
            want = g["monthly"]
        elif g["required"] is not None:           # a deadline sets the amount
            want = g["required"]
        else:                                     # otherwise: as fast as possible
            want = max(left, 0)
        g["allocated"] = min(want, max(left, 0))
        g["shortfall"] = max((g["required"] or 0) - g["allocated"], 0)
        left -= g["allocated"]

    # Now run it forward. When a goal finishes, its money moves to the next one,
    # so a goal getting nothing today is not a goal that never happens.
    _run_forward(ordered, surplus if surplus is not None else 0.0, apy, today)
    return ordered, left


def _run_forward(ordered, surplus, apy, today, max_months=600):
    """Month-by-month waterfall. Fills in each goal's realistic finish date."""
    r = ((1 + apy / 100.0) ** (1 / 12.0) - 1) if apy > 0 else 0.0
    bal = {id(g): float(g.get("saved", 0)) for g in ordered}
    done_at = {}

    for m in range(1, max_months + 1):
        left = surplus
        for g in ordered:
            k = id(g)
            if k in done_at:
                continue
            if r:
                bal[k] *= (1 + r)
            need = g["target"] - bal[k]
            if need <= 0:
                done_at[k] = m - 1
                continue
            if g.get("monthly"):
                want = g["monthly"]
            elif g["deadline_date"] is not None:
                months_left = max(months_between(
                    (today.replace(day=1) + timedelta(days=31 * (m - 1))).replace(day=1),
                    g["deadline_date"]), 1)
                want = need / months_left
            else:
                want = left
            take = min(want, left, need)
            bal[k] += take
            left -= take
            if bal[k] >= g["target"] - 0.005:
                done_at[k] = m
        if len(done_at) == len(ordered) or (left >= surplus and surplus > 0
                                            and not any(id(g) not in done_at
                                                        for g in ordered)):
            break

    for g in ordered:
        n = done_at.get(id(g))
        g["eta_months"] = n
        if n is not None:
            end = (today.replace(day=1) + timedelta(days=31 * n)).replace(day=1)
            g["eta"] = end.strftime("%Y-%m")
            g["late"] = bool(g["deadline_date"] and end > g["deadline_date"])
        else:
            g["eta"], g["late"] = None, bool(g["deadline_date"])


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def bar(frac, width=22):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "·" * (width - filled)


def show_status(plan, finances=None, quiet=False):
    surplus = plan.get("monthly_surplus")
    goals, left = plan_goals(plan, surplus)
    committed, recurring, n_rec = committed_of(plan, finances)
    spend = (finances or {}).get("monthly_spend") or plan.get("monthly_spend") or 0.0

    print()
    print("  YOUR PLAN".ljust(74))
    print("  " + "─" * 72)
    if surplus is not None:
        src = "from your statements" if finances else "as recorded"
        print(f"  Spare each month   {money(surplus):>12}   {src}")
    else:
        print("  Spare each month          unknown   run `init --source finances.db`")
    if plan.get("essential_monthly"):
        print(f"  Essential spending {money(plan['essential_monthly']):>12}   "
              f"rent, utilities, food, transport, health")
    if committed:
        print(f"  Fixed commitments  {money(committed):>12}   "
              f"{n_rec} recurring charges, already inside the spending above")
    if plan.get("apy"):
        print(f"  Savings rate       {plan['apy']:>11.2f}%   compounded monthly")
    print()

    if committed and surplus is not None and committed >= surplus > 0:
        print(f"  Your fixed commitments ({money(committed)}/mo) are larger than what you")
        print(f"  have spare ({money(surplus)}/mo). They renew whether or not the month")
        print(f"  goes well, so the plan below is the first thing that gives.")
        print()

    if not goals:
        print("  No goals yet:  planner.py add \"Emergency fund\" --target 12000\n")
        return

    total_alloc = sum(g["allocated"] for g in goals)
    for g in goals:
        saved, target = g.get("saved", 0), g["target"]
        frac = saved / target if target else 0
        flag = ""
        if g.get("late"):
            flag = "  ** misses the deadline **"
        elif g["remaining"] <= 0:
            flag = "  ** funded **"
        print(f"  {g['name']}{flag}")
        print(f"    {bar(frac)}  {money(saved)} of {money(target)}  ({frac*100:.0f}%)")

        bits = []
        if g["allocated"] > 0:
            bits.append(f"putting aside {money(g['allocated'])}/mo")
        if g.get("by"):
            bits.append(f"wanted by {g['by']}")
        if g["eta"]:
            bits.append(f"ready {g['eta']}" + (f" ({g['eta_months']} months)"
                                               if g["eta_months"] else ""))
        elif g["remaining"] > 0:
            bits.append("never, at the current rate")
        print(f"    {' · '.join(bits)}")

        if g.get("shortfall", 0) > 1:
            print(f"    Short {money(g['shortfall'])}/mo to make that date. "
                  f"Needs {money(g['required'])}/mo, getting {money(g['allocated'])}.")
        print()

    if surplus is not None:
        print(f"  Allocated {money(total_alloc)} of {money(surplus)} spare"
              + (f", {money(left)} unassigned" if left > 1 else ""))
    unfunded = [g for g in goals if g.get("shortfall", 0) > 1]
    if unfunded and not quiet:
        gap = sum(g["shortfall"] for g in unfunded)
        print(f"\n  You are {money(gap)}/mo short of every deadline you've set.")
        print(f"  Three ways to close it, and only these three:")
        print(f"    · find {money(gap)}/mo   — `planner.py simulate --cut Dining=30`")
        print(f"    · move a deadline    — `planner.py add \"<name>\" --by 2028-01`")
        print(f"    · want less          — lower a target")
        if committed and spend:
            print(f"\n  Finding it means cutting spending, and {money(committed)}/mo of")
            print(f"  the {money(spend)}/mo you spend is fixed commitments that will not")
            print(f"  move on their own. That {money(gap)} has to come out of the")
            print(f"  {money(max(spend - committed, 0))}/mo that is flexible.")

    note = trust_note((finances or {}).get("coverage") or plan.get("coverage"))
    if note and not quiet:
        print(f"\n  Data: {note}")
    print()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args):
    plan = load_plan(args.plan)
    with open_engine(args.source) as eng:
        fin = read_finances(args.source, args.income, eng=eng)
    plan["source"] = args.source
    plan["monthly_spend"] = round(fin["monthly_spend"], 2)
    plan["essential_monthly"] = round(fin["essential_monthly"], 2)
    plan["committed_monthly"] = round(fin["committed"], 2)
    plan["committed_count"] = committed_of(plan, fin)[2]
    if fin["monthly_income"]:
        plan["income"] = round(fin["monthly_income"], 2)
        plan["monthly_surplus"] = round(fin["surplus"], 2)
    if args.apy is not None:
        plan["apy"] = args.apy
    plan["by_cat_monthly"] = {c: round(v, 2) for c, v in fin["by_cat_monthly"].items()}
    cov = fin["coverage"]
    plan["coverage"] = {"statements": cov["statements"], "reconciled": cov["reconciled"],
                        "mismatched": cov["mismatched"], "unchecked": cov["unchecked"],
                        "pct": round(cov["pct"], 1), "gaps": len(cov["gaps"]),
                        "trusted": cov["trusted"]}
    save_plan(plan, args.plan)

    print(f"\nRead {fin['months']} complete month(s), {fin['period']}\n")
    print(f"  Spending      {money(fin['monthly_spend']):>12} /mo")
    print(f"  of which essential {money(fin['essential_monthly']):>7} /mo")
    if fin["committed"]:
        _, _, n_live = committed_of(plan, fin)
        print(f"  of which committed {money(fin['committed']):>7} /mo across "
              f"{n_live} recurring charges")
    if fin["monthly_income"]:
        print(f"  Income        {money(fin['monthly_income']):>12} /mo")
        print(f"  Spare         {money(fin['surplus']):>12} /mo")
    else:
        print("\n  No income found in the file. If your pay lands in an account you")
        print("  didn't include, tell me directly:  planner.py init --source ... --income 6200")

    note = trust_note(fin["coverage"])
    if note:
        print(f"\n  Data: {note}")

    ess = fin["essential_monthly"]
    if ess:
        print(f"\n  An emergency fund based on YOUR essentials, not a rule of thumb:")
        for m in (3, 6):
            print(f"    {m} months of essentials = {money(ess * m)}")
        print(f"\n  Start it with:  planner.py add \"Emergency fund\" "
              f"--target {ess * 3:.0f} --priority 1")
    print()


def cmd_add(args):
    plan = load_plan(args.plan)
    existing = find_goal(plan, args.name)
    if existing:
        for k, v in (("target", args.target), ("by", args.by),
                     ("priority", args.priority), ("saved", args.saved),
                     ("monthly", args.monthly), ("note", args.note)):
            if v is not None:
                existing[k] = v
        save_plan(plan, args.plan)
        print(f"Updated '{existing['name']}'.")
    else:
        if args.target is None:
            sys.exit("A new goal needs --target")
        plan["goals"].append({
            "name": args.name, "target": args.target, "saved": args.saved or 0.0,
            "by": args.by, "priority": args.priority or 3, "note": args.note,
            "monthly": args.monthly,
            "created": date.today().isoformat(), "done": False,
        })
        save_plan(plan, args.plan)
        print(f"Added '{args.name}': {money(args.target)}"
              + (f" by {args.by}" if args.by else ""))
    show_status(plan, quiet=True)


def cmd_remove(args):
    plan = load_plan(args.plan)
    g = find_goal(plan, args.name)
    if not g:
        sys.exit(f"No goal matching '{args.name}'")
    plan["goals"].remove(g)
    save_plan(plan, args.plan)
    print(f"Removed '{g['name']}'.")


def cmd_contribute(args):
    plan = load_plan(args.plan)
    g = find_goal(plan, args.name)
    if not g:
        sys.exit(f"No goal matching '{args.name}'")
    g["saved"] = g.get("saved", 0) + args.amount
    plan["contributions"].append({"goal": g["name"], "amount": args.amount,
                                  "date": args.date or date.today().isoformat()})
    if g["saved"] >= g["target"]:
        g["done"] = True
        print(f"\n  '{g['name']}' is fully funded. {money(g['saved'])} of "
              f"{money(g['target'])}.\n")
    save_plan(plan, args.plan)
    frac = g["saved"] / g["target"] if g["target"] else 1
    print(f"  {g['name']}: {bar(frac)}  {money(g['saved'])} of {money(g['target'])} "
          f"({frac*100:.0f}%)")


def refresh(plan, source, income=None):
    """Re-read the source if it is there. Returns the finances dict, or None.

    Missing sources are not an error here: a plan is still readable without the
    transactions it was built from, it just cannot be brought up to date. The
    income recorded at init is reused unless a new one is given, otherwise a
    salary that lands outside these statements would vanish on every refresh.
    """
    eng = open_engine(source, required=False)
    if eng is None:
        return None
    with eng:
        fin = read_finances(source, income or plan.get("income"), eng=eng)
    if fin["surplus"] is not None:
        plan["monthly_surplus"] = round(fin["surplus"], 2)
    plan["monthly_spend"] = round(fin["monthly_spend"], 2)
    plan["essential_monthly"] = round(fin["essential_monthly"], 2)
    plan["committed_monthly"] = round(fin["committed"], 2)
    if fin["by_cat_monthly"]:
        plan["by_cat_monthly"] = {c: round(v, 2) for c, v in fin["by_cat_monthly"].items()}
    return fin


def cmd_status(args):
    plan = load_plan(args.plan)
    fin = refresh(plan, args.source, args.income)
    show_status(plan, fin)


def cmd_simulate(args):
    plan = load_plan(args.plan)
    fin = refresh(plan, args.source)
    committed, recurring, n_rec = committed_of(plan, fin)
    base = plan.get("monthly_surplus") or 0.0
    spend = plan.get("monthly_spend") or 0.0
    extra = 0.0
    notes = []

    for spec in args.cut or []:
        if "=" not in spec:
            sys.exit(f"--cut wants Category=percent, e.g. --cut Dining=30, got '{spec}'")
        cat, pct = spec.rsplit("=", 1)
        try:
            pct = float(pct)
        except ValueError:
            sys.exit(f"'{pct}' is not a percentage")
        monthly = (plan.get("by_cat_monthly") or {}).get(cat)
        if monthly is None:
            near = [c for c in (plan.get("by_cat_monthly") or {})
                    if c.lower().startswith(cat.lower())]
            if len(near) == 1:
                cat, monthly = near[0], plan["by_cat_monthly"][near[0]]
            else:
                sys.exit(f"No category '{cat}' in the plan. Run init first, or try: "
                         f"{', '.join(sorted((plan.get('by_cat_monthly') or {}))[:8])}")
        freed = monthly * pct / 100.0
        extra += freed
        notes.append(f"cutting {cat} by {pct:.0f}% frees {money(freed)}/mo "
                     f"(of {money(monthly)})")
        # A percentage off a category is easy to type and hard to do when most
        # of that category renews itself. Say which charges have to go.
        fixed = [r for r in recurring if r.category == cat]
        if fixed:
            locked = sum(r.per_month for r in fixed)
            names = ", ".join(r.name[:22] for r in fixed[:3])
            notes.append(f"  but {money(locked)}/mo of {cat} is {len(fixed)} recurring "
                         f"charge(s) — {names}")
            if freed > monthly - locked:
                notes.append(f"  so a {pct:.0f}% cut means cancelling one of them, not "
                             f"spending less on it")

    if args.monthly is not None:
        new_surplus = args.monthly
        notes.append(f"assuming {money(args.monthly)}/mo set aside")
    else:
        new_surplus = base + extra

    print(f"\n  now:      {money(base)}/mo spare")
    for nline in notes:
        print(f"            {nline}")
    print(f"  scenario: {money(new_surplus)}/mo spare\n")

    # Asserting a monthly amount above the surplus is not a scenario, it is a
    # cut that has not been made yet, and the fixed part of the spending it
    # would have to come out of cannot absorb it. (A --cut scenario says where
    # the money comes from already, so it needs no lecture.)
    short = new_surplus - base
    if args.monthly is not None and short > 1 and spend:
        flexible = max(spend - committed, 0)
        print(f"  That is {money(short)}/mo more than you have spare, so it comes out of")
        print(f"  the {money(spend)}/mo you spend — of which {money(committed)}/mo is "
              f"{n_rec} fixed")
        print(f"  commitments, leaving {money(flexible)}/mo that is actually flexible"
              + ("." if short <= flexible else ", less"))
        if short > flexible:
            print(f"  than the cut you are assuming.")
        print()

    before, _ = plan_goals(plan, base)
    after, left = plan_goals(plan, new_surplus)
    amap = {g["name"]: g for g in after}

    print(f"  {'goal':<24}{'now':>14}{'scenario':>14}   change")
    print("  " + "─" * 68)
    for g in before:
        a = amap.get(g["name"], {})
        t0 = g["eta"] or "never"
        t1 = a.get("eta") or "never"
        if g["eta_months"] and a.get("eta_months"):
            d = g["eta_months"] - a["eta_months"]
            ch = f"{d:+d} months" if d else "same"
        elif a.get("eta_months") and not g["eta_months"]:
            ch = "now reachable"
        else:
            ch = ""
        print(f"  {g['name'][:23]:<24}{t0:>14}{t1:>14}   {ch}")
    if left > 1:
        print(f"\n  {money(left)}/mo would still be unassigned.")
    print()


def cmd_report(args):
    plan = load_plan(args.plan)
    fin = refresh(plan, args.source)
    committed, recurring, n_rec = committed_of(plan, fin)
    goals, left = plan_goals(plan, plan.get("monthly_surplus"))
    surplus = plan.get("monthly_surplus") or 0
    note = trust_note((fin or {}).get("coverage") or plan.get("coverage"))

    rows = []
    for g in goals:
        frac = min(g.get("saved", 0) / g["target"], 1) if g["target"] else 0
        late = g.get("late")
        rows.append(f"""
        <div class="goal{' late' if late else ''}">
          <div class="gh"><h3>{html.escape(g['name'])}</h3>
            <span class="amt">{money(g.get('saved',0))} <i>of {money(g['target'])}</i></span></div>
          <div class="track"><div class="fill" style="width:{frac*100:.1f}%"></div></div>
          <div class="meta">
            <span>{money(g['allocated'])}/mo set aside</span>
            <span>{'ready ' + g['eta'] if g['eta'] else 'not reachable at this rate'}</span>
            {'<span>wanted by ' + html.escape(str(g['by'])) + '</span>' if g.get('by') else ''}
            {'<span class="warn">short ' + money(g['shortfall']) + '/mo</span>'
             if g.get('shortfall', 0) > 1 else ''}
          </div>
        </div>""")

    horizon = max([g["eta_months"] or 0 for g in goals] + [12])
    horizon = min(horizon, 120)
    ticks = []
    placed = []
    for g in sorted(goals, key=lambda g: g["eta_months"] or 0):
        if not g["eta_months"]:
            continue
        x = g["eta_months"] / horizon * 100
        # stack labels that would land on top of each other
        row = 0
        while any(abs(x - px) < 16 and row == pr for px, pr in placed):
            row += 1
        placed.append((x, row))
        ticks.append(f'<div class="tick" style="left:{x:.1f}%;top:{row * 30}px">'
                     f'<span>{html.escape(g["name"][:18])}<br><b>{g["eta"]}</b></span></div>')

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Savings plan</title><style>
:root{{--paper:#FBFBF7;--band:#E7EFE3;--ink:#14211B;--soft:#5C6D62;--rule:#C2D1BC;
--green:#2E6A4C;--red:#A3372A;--amber:#9C7318;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:#DCE3D8;font-family:var(--ui);color:var(--ink);font-size:15px}}
.sheet{{max-width:900px;margin:26px auto;background:var(--paper);
border:1px solid #8FA88A;padding:34px 40px 40px;
box-shadow:0 12px 34px rgba(20,33,27,.10)}}
.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
text-transform:uppercase;color:var(--soft)}}
h1{{font-family:var(--display);font-weight:400;font-size:34px;margin:6px 0 18px}}
.summary{{display:flex;gap:34px;flex-wrap:wrap;border-top:2px solid var(--ink);
border-bottom:1px solid var(--rule);padding:14px 0;margin-bottom:26px}}
.summary div{{font-family:var(--mono)}}
.summary span{{display:block;font-size:11px;color:var(--soft);letter-spacing:.1em;
text-transform:uppercase}}
.summary b{{font-size:22px;font-weight:600}}
.goal{{border:1px solid var(--rule);border-left:3px solid var(--green);
padding:14px 16px;margin-bottom:14px;background:#fff}}
.goal.late{{border-left-color:var(--red)}}
.gh{{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
h3{{margin:0;font-size:16px}}
.amt{{font-family:var(--mono);font-weight:600}}
.amt i{{color:var(--soft);font-style:normal;font-weight:400;font-size:13px}}
.track{{height:9px;background:var(--band);margin:10px 0 8px;overflow:hidden}}
.fill{{height:100%;background:var(--green)}}
.goal.late .fill{{background:var(--amber)}}
.meta{{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--mono);font-size:11.5px;
color:var(--soft)}}
.meta .warn{{color:var(--red)}}
.timeline{{position:relative;height:130px;margin:30px 0 10px;
border-top:1px solid var(--rule)}}
.tick{{position:absolute;top:0;transform:translateX(-50%);text-align:center;
font-family:var(--mono);font-size:10.5px;color:var(--soft);padding-top:10px}}
.tick:before{{content:"";position:absolute;top:0;left:50%;width:1px;height:8px;
background:var(--green)}}
.tick b{{color:var(--ink)}}
.caveat{{border-left:3px solid var(--amber);background:#FBF6E9;padding:10px 14px;
margin:18px 0 0;font-size:12.5px;color:var(--soft)}}
.caveat b{{color:var(--ink);font-weight:600}}
footer{{margin-top:26px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft)}}
</style></head><body><div class="sheet">
<div class="kicker">Savings plan</div>
<h1>What you're saving for</h1>
<div class="summary">
  <div><span>spare each month</span><b>{money(surplus)}</b></div>
  <div><span>committed</span><b>{money(committed)}</b></div>
  <div><span>goals</span><b>{len(goals)}</b></div>
  <div><span>still to save</span><b>{money(sum(g['remaining'] for g in goals))}</b></div>
  <div><span>unassigned</span><b>{money(max(left,0))}</b></div>
</div>
{''.join(rows)}
<div class="timeline">{''.join(ticks)}</div>
{f'<p class="caveat"><b>Committed:</b> {money(committed)}/mo of the spending behind '
 f'this plan is {n_rec} recurring charge(s) that renew on their own. It is already '
 f'deducted from the figure above, but it is the part of a bad month you cannot cut '
 f'in time.</p>' if committed else ''}
{f'<p class="caveat"><b>Data:</b> {html.escape(note)}</p>' if note else ''}
<footer>Generated locally by planner.py on {date.today():%d %b %Y}.
Timings assume the amounts above are actually set aside every month.
Arithmetic on your own statements, not financial advice.</footer>
</div></body></html>"""

    with open(args.output, "w") as fh:
        fh.write(doc)
    print(f"Wrote {args.output}")


def debts_from_csv(path):
    """Balances and rates out of a legacy statements CSV."""
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                bal = float(r.get("new_balance") or 0)
                apr = float(r.get("purchase_apr") or 0)
            except ValueError:
                continue
            if bal > 50 and apr > 0:
                out.append((f"{r.get('issuer','?')} ····{r.get('account_last4','')}",
                            bal, apr))
    return out


def debts_from_store(statements):
    """The latest balance and rate on each card, straight from the store.

    The statements are already parsed and sitting next to the transactions, so
    there is no reason to ask for a separate CSV of the same numbers.
    """
    latest = {}
    for s in statements:
        name = f"{s.issuer or '?'} ····{s.account_last4 or ''}".strip()
        cur = latest.get(name)
        if cur is None or (s.period_end or "") >= (cur.period_end or ""):
            latest[name] = s
    return [(name, s.new_balance, s.purchase_apr) for name, s in latest.items()
            if (s.new_balance or 0) > 50 and (s.purchase_apr or 0) > 0]


def cmd_advice(args):
    """Where the next dollar goes when debt, commitments and goals compete."""
    plan = load_plan(args.plan)
    fin = None
    debts = []
    eng = open_engine(args.source, required=False)
    if eng is not None:
        with eng:
            fin = read_finances(args.source, plan.get("income"), eng=eng)
            debts = debts_from_store(eng.statements())
        if fin["surplus"] is not None:
            plan["monthly_surplus"] = round(fin["surplus"], 2)
            plan["essential_monthly"] = round(fin["essential_monthly"], 2)
    if args.statements and os.path.exists(args.statements):
        debts = debts_from_csv(args.statements)

    surplus = plan.get("monthly_surplus")
    ess = plan.get("essential_monthly") or 0
    committed, recurring, n_rec = committed_of(plan, fin)
    print()
    if not surplus:
        print("  Run `init --source finances.db` first so I know what's spare.\n")
        return

    seen, uniq = set(), []
    for name, bal, apr in debts:
        if name in seen:
            continue
        seen.add(name)
        uniq.append((name, bal, apr))

    step = 0

    def n():
        nonlocal step
        step += 1
        return step

    print("  ORDER OF OPERATIONS")
    print("  " + "─" * 72)
    starter = min(ess, 1000) if ess else 1000
    print(f"  {n()}. A small buffer first — about {money(starter)}, roughly one month of")
    print(f"     essentials. Without it any surprise goes straight back onto a card.")

    if committed:
        print(f"\n  {n()}. Know what is already promised: {money(committed)}/mo across "
              f"{n_rec} recurring")
        print(f"     charges, {money(committed * 12)} a year. Your surplus is "
              f"{money(surplus)}/mo, and that")
        print(f"     commitment is already deducted from it — but it is the part of a bad")
        print(f"     month you cannot cancel in time, so it caps how much of the surplus")
        print(f"     is really yours to move.")
        for r in recurring[:3]:
            print(f"       · {r.name[:28]:<28} {money(r.per_month):>8}/mo  "
                  f"{r.cadence}, {money(r.per_year)}/yr")
        if committed >= surplus:
            print(f"     These commitments are larger than the {money(surplus)}/mo you have")
            print(f"     spare. Cancelling one is worth more than any goal below.")

    if uniq:
        worst = max(uniq, key=lambda d: d[2])
        print(f"\n  {n()}. Then the {worst[2]:.2f}% debt on {worst[0]} ({money(worst[1])}).")
        yearly = worst[1] * worst[2] / 100
        print(f"     That balance costs about {money(yearly)}/yr while it sits there.")
        print(f"     Paying it down avoids that cost with certainty. A savings account")
        print(f"     paying {plan.get('apy', 0) or 4:.1f}% earns roughly "
              f"{money(worst[1] * (plan.get('apy') or 4) / 100)}/yr on the same money.")
        print(f"     The gap is {money(yearly - worst[1] * (plan.get('apy') or 4) / 100)}/yr "
              f"in favour of clearing the debt.")
        print(f"\n  {n()}. Then the full emergency fund: "
              f"{money(ess * 3)} to {money(ess * 6)} (3-6 months of essentials).")
        print(f"  {n()}. Then everything else on your list.")
    else:
        print(f"\n  {n()}. Then the full emergency fund: {money(ess * 3)} to {money(ess * 6)}")
        print(f"     (3 to 6 months of your essential spending, {money(ess)}/mo).")
        print(f"  {n()}. Then everything else on your list.")
        if not args.statements:
            print(f"\n  No card balances found. If you carry any, point --source at a store")
            print(f"  with statements in it, or add --statements statements.csv,")
            print(f"  and I'll tell you whether paying them beats saving.")

    note = trust_note((fin or {}).get("coverage") or plan.get("coverage"))
    if note:
        print(f"\n  Data: {note}")
    print(f"\n  This is arithmetic on your numbers, not financial advice. Rates, taxes")
    print(f"  and your own risk tolerance all matter and I don't know them.\n")


def add_source(parser):
    """--source is either finances.db or a legacy transactions CSV.

    --csv was the old name for the same argument. It still works, undocumented,
    so existing scripts and muscle memory do not break.
    """
    parser.add_argument("--source", default=DEFAULT_DB,
                        help=f"finances.db or a transactions CSV (default {DEFAULT_DB})")
    parser.add_argument("--csv", dest="source", help=argparse.SUPPRESS)


def main():
    ap = argparse.ArgumentParser(description="Plan what you're saving for.")
    ap.add_argument("--plan", default=PLAN)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="read your spending and size an emergency fund")
    add_source(i)
    i.add_argument("--income", type=float, help="monthly take-home, if it isn't in the file")
    i.add_argument("--apy", type=float, help="what your savings account pays")
    i.set_defaults(func=cmd_init)

    a = sub.add_parser("add", help="add or update a goal")
    a.add_argument("name")
    a.add_argument("--target", type=float)
    a.add_argument("--by", help="YYYY-MM")
    a.add_argument("--priority", type=int, help="1 is highest")
    a.add_argument("--saved", type=float, help="how much you already have")
    a.add_argument("--monthly", type=float,
                   help="fix the amount for this goal instead of letting it take "
                        "whatever is spare")
    a.add_argument("--note")
    a.set_defaults(func=cmd_add)

    rm = sub.add_parser("remove", help="drop a goal")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_remove)

    c = sub.add_parser("contribute", help="record money put aside")
    c.add_argument("name")
    c.add_argument("amount", type=float)
    c.add_argument("--date")
    c.set_defaults(func=cmd_contribute)

    s = sub.add_parser("status", help="where everything stands")
    add_source(s)
    s.add_argument("--income", type=float)
    s.set_defaults(func=cmd_status)

    sim = sub.add_parser("simulate", help="what if you saved more, or cut something")
    add_source(sim)
    sim.add_argument("--monthly", type=float, help="assume this much set aside")
    sim.add_argument("--cut", action="append", metavar="CAT=PCT",
                     help="e.g. --cut Dining=30 (repeatable)")
    sim.set_defaults(func=cmd_simulate)

    r = sub.add_parser("report", help="write an HTML plan")
    add_source(r)
    r.add_argument("-o", "--output", default="plan.html")
    r.set_defaults(func=cmd_report)

    ad = sub.add_parser("advice", help="debt or savings first, with the numbers")
    add_source(ad)
    ad.add_argument("--statements",
                    help="legacy statements.csv; the store's own statements are "
                         "used when this is not given")
    ad.set_defaults(func=cmd_advice)

    args = ap.parse_args()

    args.func(args)


if __name__ == "__main__":
    main()