#!/usr/bin/env python3
"""
planner.py — plan what you're saving for, using your own numbers.

It reads your transactions to work out what you actually have spare each month,
then tells you whether your goals fit in it and when each one lands.

    python planner.py init --csv cleanedData/transactions.csv
    python planner.py add "Emergency fund" --target 12000 --priority 1
    python planner.py add "New car" --target 20000 --by 2027-06 --saved 1500
    python planner.py add "Japan trip" --target 4500 --by 2026-11 --priority 3
    python planner.py status
    python planner.py contribute "New car" 500
    python planner.py simulate --monthly 900
    python planner.py simulate --cut Dining=30 --cut Delivery=50
    python planner.py report -o plan.html

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
try:
    from categorizer import NON_SPEND, ESSENTIAL, DISCRETIONARY
except ImportError:
    NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}
    ESSENTIAL = {"Groceries", "Transport", "Gas/Auto", "Utilities", "Phone/Internet",
                 "Rent/Housing", "Insurance", "Health", "Pharmacy", "Education",
                 "Childcare", "Taxes"}
    DISCRETIONARY = {"Dining", "Coffee", "Delivery", "Alcohol/Bars", "Shopping",
                     "Big Box", "Electronics", "Clothing", "Home", "Entertainment",
                     "Personal Care", "Travel", "Lodging", "Subscriptions",
                     "Software/Tools", "Gaming", "Gifts", "Pets", "Fitness"}

PLAN = "plan.json"


# --------------------------------------------------------------------------
# Money and time
# --------------------------------------------------------------------------

def money(v, dp=0):
    return f"${v:,.{dp}f}"


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


def future_value(saved, monthly, n, apy=0.0):
    if apy <= 0:
        return saved + monthly * n
    r = (1 + apy / 100.0) ** (1 / 12.0) - 1
    return saved * (1 + r) ** n + monthly * (((1 + r) ** n - 1) / r)


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

def read_finances(csv_path, income_override=None):
    """What comes in, what goes out, and what's genuinely fixed."""
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{csv_path} is empty")

    by_month_spend = defaultdict(float)
    by_month_income = defaultdict(float)
    by_cat = defaultdict(float)
    essential = defaultdict(float)
    months = set()

    for r in rows:
        try:
            amt = float(r["amount"])
            d = datetime.strptime(r["date"][:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        cat = r.get("category") or "Uncategorized"
        m = d.strftime("%Y-%m")
        months.add(m)
        if cat == "Income" and amt < 0:
            by_month_income[m] += -amt
        elif amt > 0 and cat not in NON_SPEND:
            by_month_spend[m] += amt
            by_cat[cat] += amt
            if cat in ESSENTIAL:
                essential[m] += amt

    if not months:
        sys.exit("no usable rows")

    # Drop the first and last month: they're usually partial and would drag
    # every average down.
    ordered = sorted(months)
    full = ordered[1:-1] if len(ordered) > 2 else ordered
    n = max(len(full), 1)

    spend = sum(by_month_spend[m] for m in full) / n
    inc = income_override or (sum(by_month_income[m] for m in full) / n)
    ess = sum(essential[m] for m in full) / n

    return {
        "months": len(full), "monthly_spend": spend, "monthly_income": inc,
        "essential_monthly": ess,
        "surplus": (inc - spend) if inc else None,
        "by_cat_monthly": {c: v / n for c, v in by_cat.items()},
        "period": f"{full[0]} to {full[-1]}" if full else "",
    }


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

    print()
    print("  YOUR PLAN".ljust(74))
    print("  " + "─" * 72)
    if surplus is not None:
        src = "from your statements" if finances else "as recorded"
        print(f"  Spare each month   {money(surplus):>12}   {src}")
    else:
        print("  Spare each month          unknown   run `init --csv transactions.csv`")
    if plan.get("essential_monthly"):
        print(f"  Essential spending {money(plan['essential_monthly']):>12}   "
              f"rent, utilities, food, transport, health")
    if plan.get("apy"):
        print(f"  Savings rate       {plan['apy']:>11.2f}%   compounded monthly")
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
    print()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args):
    plan = load_plan(args.plan)
    fin = read_finances(args.csv, args.income)
    plan["monthly_spend"] = round(fin["monthly_spend"], 2)
    plan["essential_monthly"] = round(fin["essential_monthly"], 2)
    if fin["monthly_income"]:
        plan["income"] = round(fin["monthly_income"], 2)
        plan["monthly_surplus"] = round(fin["surplus"], 2)
    if args.apy is not None:
        plan["apy"] = args.apy
    plan["by_cat_monthly"] = {c: round(v, 2) for c, v in fin["by_cat_monthly"].items()}
    save_plan(plan, args.plan)

    print(f"\nRead {fin['months']} complete month(s), {fin['period']}\n")
    print(f"  Spending      {money(fin['monthly_spend']):>12} /mo")
    print(f"  of which essential {money(fin['essential_monthly']):>7} /mo")
    if fin["monthly_income"]:
        print(f"  Income        {money(fin['monthly_income']):>12} /mo")
        print(f"  Spare         {money(fin['surplus']):>12} /mo")
    else:
        print("\n  No income found in the file. If your pay lands in an account you")
        print("  didn't include, tell me directly:  planner.py init --csv ... --income 6200")

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


def cmd_status(args):
    plan = load_plan(args.plan)
    fin = None
    if args.csv:
        fin = read_finances(args.csv, args.income)
        plan["monthly_surplus"] = round(fin["surplus"], 2) if fin["surplus"] else \
            plan.get("monthly_surplus")
    show_status(plan, fin)


def cmd_simulate(args):
    plan = load_plan(args.plan)
    base = plan.get("monthly_surplus") or 0.0
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

    if args.monthly is not None:
        new_surplus = args.monthly
        notes.append(f"assuming {money(args.monthly)}/mo set aside")
    else:
        new_surplus = base + extra

    print(f"\n  now:      {money(base)}/mo spare")
    for nline in notes:
        print(f"            {nline}")
    print(f"  scenario: {money(new_surplus)}/mo spare\n")

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
    goals, left = plan_goals(plan, plan.get("monthly_surplus"))
    surplus = plan.get("monthly_surplus") or 0

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
footer{{margin-top:26px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft)}}
</style></head><body><div class="sheet">
<div class="kicker">Savings plan</div>
<h1>What you're saving for</h1>
<div class="summary">
  <div><span>spare each month</span><b>{money(surplus)}</b></div>
  <div><span>goals</span><b>{len(goals)}</b></div>
  <div><span>still to save</span><b>{money(sum(g['remaining'] for g in goals))}</b></div>
  <div><span>unassigned</span><b>{money(max(left,0))}</b></div>
</div>
{''.join(rows)}
<div class="timeline">{''.join(ticks)}</div>
<footer>Generated locally by planner.py on {date.today():%d %b %Y}.
Timings assume the amounts above are actually set aside every month.
Arithmetic on your own statements, not financial advice.</footer>
</div></body></html>"""

    with open(args.output, "w") as fh:
        fh.write(doc)
    print(f"Wrote {args.output}")


def cmd_advice(args):
    """Where the next dollar goes when debt and goals compete."""
    plan = load_plan(args.plan)
    surplus = plan.get("monthly_surplus")
    ess = plan.get("essential_monthly") or 0
    print()
    if not surplus:
        print("  Run `init --csv transactions.csv` first so I know what's spare.\n")
        return

    debts = []
    if args.statements and os.path.exists(args.statements):
        with open(args.statements, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    bal = float(r.get("new_balance") or 0)
                    apr = float(r.get("purchase_apr") or 0)
                except ValueError:
                    continue
                if bal > 50 and apr > 0:
                    debts.append((f"{r.get('issuer','?')} ····{r.get('account_last4','')}",
                                  bal, apr))
    seen, uniq = set(), []
    for name, bal, apr in debts:
        if name in seen:
            continue
        seen.add(name)
        uniq.append((name, bal, apr))

    print("  ORDER OF OPERATIONS")
    print("  " + "─" * 72)
    starter = min(ess, 1000) if ess else 1000
    print(f"  1. A small buffer first — about {money(starter)}, roughly one month of")
    print(f"     essentials. Without it any surprise goes straight back onto a card.")

    if uniq:
        worst = max(uniq, key=lambda d: d[2])
        print(f"\n  2. Then the {worst[2]:.2f}% debt on {worst[0]} ({money(worst[1])}).")
        yearly = worst[1] * worst[2] / 100
        print(f"     That balance costs about {money(yearly)}/yr while it sits there.")
        print(f"     Paying it down avoids that cost with certainty. A savings account")
        print(f"     paying {plan.get('apy', 0) or 4:.1f}% earns roughly "
              f"{money(worst[1] * (plan.get('apy') or 4) / 100)}/yr on the same money.")
        print(f"     The gap is {money(yearly - worst[1] * (plan.get('apy') or 4) / 100)}/yr "
              f"in favour of clearing the debt.")
        print(f"\n  3. Then the full emergency fund: "
              f"{money(ess * 3)} to {money(ess * 6)} (3-6 months of essentials).")
        print(f"  4. Then everything else on your list.")
    else:
        print(f"\n  2. Then the full emergency fund: {money(ess * 3)} to {money(ess * 6)}")
        print(f"     (3 to 6 months of your essential spending, {money(ess)}/mo).")
        print(f"  3. Then everything else on your list.")
        if not args.statements:
            print(f"\n  No card balances given. If you carry any, add --statements "
                  f"statements.csv")
            print(f"  and I'll tell you whether paying them beats saving.")

    print(f"\n  This is arithmetic on your numbers, not financial advice. Rates, taxes")
    print(f"  and your own risk tolerance all matter and I don't know them.\n")


def main():
    ap = argparse.ArgumentParser(description="Plan what you're saving for.")
    ap.add_argument("--plan", default=PLAN)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="read your spending and size an emergency fund")
    i.add_argument("--csv", required=True)
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
    s.add_argument("--csv", help="refresh the surplus from transactions first")
    s.add_argument("--income", type=float)
    s.set_defaults(func=cmd_status)

    sim = sub.add_parser("simulate", help="what if you saved more, or cut something")
    sim.add_argument("--monthly", type=float, help="assume this much set aside")
    sim.add_argument("--cut", action="append", metavar="CAT=PCT",
                     help="e.g. --cut Dining=30 (repeatable)")
    sim.set_defaults(func=cmd_simulate)

    r = sub.add_parser("report", help="write an HTML plan")
    r.add_argument("-o", "--output", default="plan.html")
    r.set_defaults(func=cmd_report)

    ad = sub.add_parser("advice", help="debt or savings first, with the numbers")
    ad.add_argument("--statements", help="statements.csv, to weigh debt against saving")
    ad.set_defaults(func=cmd_advice)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()