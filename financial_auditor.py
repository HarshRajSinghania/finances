#!/usr/bin/env python3
"""
financial_auditor.py — your strict grandfather goes through your statements.

He is not impressed. He wants to know what each thing was for. If you can't
tell him, he assumes it's fraud and tells you to call the bank.

    python financial_auditor.py audit transactions.csv          # the reckoning
    python financial_auditor.py audit transactions.csv -i       # he asks, you answer
    python financial_auditor.py audit transactions.csv --clean  # no swearing
    python financial_auditor.py audit transactions.csv --report audit.md

    python financial_auditor.py use "Netflix"                   # log that you used it
    python financial_auditor.py usage                           # cost per use, verdicts
    python financial_auditor.py monitor --minutes 480           # watch what you open
    python financial_auditor.py disputes                        # what to call the bank about

Everything is stored locally in auditor_ledger.json. Answers persist, so he
only asks about a charge once — but he remembers what you told him.

Standard library only.
"""

import argparse
import csv
import json
import os
import re
import statistics as stats
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

LEDGER = "auditor_ledger.json"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from categorizer import ESSENTIAL as _ESSENTIAL, NON_SPEND as _NON_SPEND
except ImportError:
    _ESSENTIAL, _NON_SPEND = set(), set()

# He does not get to be clever about these. Nobody needs a lecture for filling a
# prescription or paying for childcare.
OFF_LIMITS = ({"Health", "Pharmacy", "Childcare", "Insurance", "Education",
               "Rent/Housing", "Utilities", "Phone/Internet", "Taxes", "Groceries",
               "Charity", "Payment", "Credits/Refunds", "Income"}
              | _ESSENTIAL | _NON_SPEND)

HABIT_CATEGORIES = {"Coffee", "Dining", "Delivery", "Alcohol/Bars"}
COFFEE_RE = re.compile(r"coffee|starbucks|dunkin|bean|roast|cafe|caf\u00e9|espresso|"
                       r"peet|costa|tim horton|blue bottle|philz|brew", re.I)

SERVICES = {
    "streaming": r"netflix|hulu|disney|hbo|max\.com|paramount|peacock|apple ?tv|"
                 r"crunchyroll|starz|showtime|britbox|mubi|shudder",
    "music": r"spotify|apple music|tidal|youtube ?premium|pandora|deezer|soundcloud",
    "ai": r"openai|chatgpt|anthropic|claude|midjourney|copilot|cursor|perplexity|"
          r"replit|elevenlabs|runway|v0\.dev",
    "dev": r"github|gitlab|jetbrains|aws|amazon web|google cloud|azure|digitalocean|"
           r"vercel|netlify|heroku|linode|render\.com|docker",
    "creative": r"adobe|figma|canva|sketch|affinity|capcut",
    "gaming": r"xbox|playstation|nintendo|steam|epic games|ea play|game ?pass",
    "news": r"nyt|new york times|wsj|washington post|economist|athletic|substack|medium",
    "fitness": r"peloton|strava|whoop|equinox|planet fit|classpass|gym",
    "storage": r"icloud|dropbox|google one|onedrive|backblaze|box\.com",
}
SERVICE_RE = {k: re.compile(v, re.I) for k, v in SERVICES.items()}

# Desktop processes the monitor can recognise. Browser tabs are invisible to it,
# which is why manual `use` logging exists.
PROCESS_MAP = {
    "Netflix": ["netflix"], "Spotify": ["spotify"], "Disney+": ["disney"],
    "Steam": ["steam"], "Adobe": ["photoshop", "illustrator", "premiere", "lightroom",
                                  "aftereffects", "acrobat", "indesign"],
    "JetBrains": ["pycharm", "intellij", "webstorm", "goland", "clion", "rider"],
    "Docker": ["docker"], "Slack": ["slack"], "Figma": ["figma"],
    "VS Code": ["code", "code helper"], "Xbox": ["xbox"], "Zoom": ["zoom"],
    "Kindle": ["kindle"], "Audible": ["audible"], "Prime Video": ["primevideo", "amazon prime video"],
    "Apple TV": ["tv app", "appletv"], "Music": ["music"], "Peloton": ["peloton"],
}


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------

class Grandpa:
    def __init__(self, salty=True, color=True):
        self.salty = salty
        self.color = color and sys.stdout.isatty()

    def c(self, s, code):
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def bold(self, s):   return self.c(s, "1")
    def red(self, s):    return self.c(s, "31")
    def green(self, s):  return self.c(s, "32")
    def amber(self, s):  return self.c(s, "33")
    def dim(self, s):    return self.c(s, "2")

    def swear(self, salty_version, clean_version):
        return salty_version if self.salty else clean_version


def money(v, dp=0):
    return f"${v:,.{dp}f}"


def rule(width=74, ch="─"):
    return ch * width


def header(g, title, subtitle=""):
    out = ["", g.bold("╔" + "═" * 72 + "╗"),
           g.bold("║ ") + g.bold(title.ljust(70)) + g.bold(" ║")]
    if subtitle:
        out.append(g.bold("║ ") + g.dim(subtitle.ljust(70)) + g.bold(" ║"))
    out.append(g.bold("╚" + "═" * 72 + "╝"))
    return "\n".join(out)


def section(g, n, title):
    return f"\n{g.amber(f'── {n}. {title.upper()} ')}{g.amber('─' * max(0, 68 - len(title)))}"


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def load_ledger(path=LEDGER):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except json.JSONDecodeError:
            pass
    return {"explanations": {}, "usage": [], "disputes": [], "decisions": {}}


def save_ledger(led, path=LEDGER):
    json.dump(led, open(path, "w"), indent=1)


def charge_id(t):
    return f"{t['date']}|{t['key']}|{t['amount']:.2f}"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def merchant_key(name):
    s = name.upper().strip()
    if re.fullmatch(r"[A-Z]+_\d+", s) or s.startswith("OTHER ("):
        return s
    s = re.sub(r"[^A-Z0-9_ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    stop = {"POS", "PURCHASE", "RECURRING", "INC", "LLC", "CO", "THE", "USA", "US",
            "COM", "STORE", "SQ", "TST", "PY"}
    toks = []
    for t in s.split():
        if t in stop or len(t) < 2:
            continue
        if t.isdigit() and len(t) >= 3:
            continue
        if "_" not in t and len(t) >= 8 and sum(ch.isdigit() for ch in t) >= 2:
            continue
        toks.append(t)
    return " ".join(toks[:3]) or s


def load(path):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        try:
            amt = float(r["amount"])
            d = datetime.strptime(r["date"][:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        name = (r.get("merchant") or r.get("description") or "").strip()
        out.append({"date": d.isoformat(), "d": d, "amount": amt, "merchant": name,
                    "key": merchant_key(name), "category": r.get("category") or "Uncategorized",
                    "card": r.get("issuer", "")})
    if not out:
        sys.exit("No usable rows. Expected columns: date, amount, merchant, category.")
    return out


def months_covered(txns):
    ms = {t["date"][:7] for t in txns}
    return max(len(ms), 1)


def detect_recurring(spend):
    by = defaultdict(list)
    for t in spend:
        by[t["key"]].append(t)
    out = []
    PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12,
                "quarterly": 4, "semiannual": 2, "annual": 1}
    for k, ts in by.items():
        if len(ts) < 3:
            continue
        ds = sorted(t["d"] for t in ts)
        if (ds[-1] - ds[0]).days < 50:
            continue
        amts = [t["amount"] for t in ts]
        med = stats.median(amts)
        spread = stats.median([abs(a - med) for a in amts])
        if med <= 0 or spread / med > 0.25:
            continue

        gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        med_gap = stats.median(gaps)

        # Subscriptions bill once a calendar month, but the day drifts — a card
        # reissue, a weekend, a billing-date change. Counting months is far more
        # reliable than measuring day gaps.
        months_hit = sorted({t["d"].strftime("%Y-%m") for t in ts})
        span_months = ((ds[-1].year - ds[0].year) * 12 + ds[-1].month - ds[0].month) + 1
        monthly_like = (len(months_hit) >= 3
                        and len(months_hit) >= span_months * 0.7
                        and len(ts) <= len(months_hit) * 1.4)

        if monthly_like:
            cadence = "monthly"
        elif 6 <= med_gap <= 400:
            gap_spread = stats.median([abs(g - med_gap) for g in gaps])
            if gap_spread / med_gap > 0.35:
                continue
            cadence = ("weekly" if med_gap <= 9 else "biweekly" if med_gap <= 18 else
                       "monthly" if med_gap <= 45 else "quarterly" if med_gap <= 115 else
                       "semiannual" if med_gap <= 200 else "annual")
        else:
            continue

        per_year = med * PER_YEAR[cadence]
        out.append({"key": k, "name": Counter(t["merchant"] for t in ts).most_common(1)[0][0],
                    "amount": med, "per_year": per_year, "per_month": per_year / 12,
                    "n": len(ts), "last": ds[-1], "first": ds[0],
                    "category": Counter(t["category"] for t in ts).most_common(1)[0][0],
                    "cadence": cadence})
    return sorted(out, key=lambda r: -r["per_year"])


def classify(name):
    for kind, rx in SERVICE_RE.items():
        if rx.search(name):
            return kind
    return None


# --------------------------------------------------------------------------
# The interrogation
# --------------------------------------------------------------------------

def find_questions(spend, recurring, led, months):
    """Charges grandpa wants explained, worst first."""
    qs = []
    explained = led["explanations"]
    by_key = defaultdict(list)
    for t in spend:
        by_key[t["key"]].append(t)

    rec_keys = {r["key"] for r in recurring}
    amounts = sorted(t["amount"] for t in spend)
    if not amounts:
        return qs
    p90 = amounts[int(len(amounts) * 0.90)] if len(amounts) > 10 else max(amounts)

    for t in spend:
        cid = charge_id(t)
        if cid in explained or t["category"] in OFF_LIMITS:
            continue
        reasons = []
        peers = [x["amount"] for x in by_key[t["key"]]]
        # one-off, large, never seen again
        if len(peers) == 1 and t["amount"] >= max(p90, 75):
            reasons.append("one charge, never seen before or since")
        # far above this merchant's own normal
        if len(peers) >= 5:
            med = stats.median(peers)
            if t["amount"] > max(med * 3, med + 60) and t["amount"] > 60:
                reasons.append(f"{t['amount']/med:.0f}x what you normally spend there "
                               f"({money(med, 2)})")
        # a recurring charge that changed price
        if t["key"] in rec_keys and t["category"] != "Fees/Interest":
            r = next(r for r in recurring if r["key"] == t["key"])
            if t["amount"] > r["amount"] * 1.25 and t["amount"] - r["amount"] > 5:
                reasons.append(f"this subscription normally costs {money(r['amount'], 2)}")
        if reasons:
            qs.append({"id": cid, "t": t, "why": reasons[0], "amount": t["amount"]})

    # same merchant, same amount, same day
    seen = defaultdict(list)
    for t in spend:
        seen[(t["key"], round(t["amount"], 2))].append(t["d"])
    for (k, amt), ds in seen.items():
        ds = sorted(ds)
        for i in range(len(ds) - 1):
            if (ds[i + 1] - ds[i]).days <= 1 and amt > 5:
                cid = f"{ds[i].isoformat()}|{k}|dup"
                if cid in explained:
                    break
                qs.append({"id": cid,
                           "t": {"date": ds[i].isoformat(), "merchant": k, "key": k,
                                 "amount": amt, "category": "", "d": ds[i]},
                           "why": "charged twice within a day — same merchant, same amount",
                           "amount": amt})
                break

    qs.sort(key=lambda q: -q["amount"])
    return qs


def fraud_checklist(g, items):
    out = [g.red(g.bold("\n  YOU COULDN'T EXPLAIN THESE. DO THIS TODAY:")), ""]
    total = sum(i["amount"] for i in items)
    for i in items:
        out.append(f"    {i['t']['date']}  {money(i['amount'], 2):>10}  {i['t']['merchant']}")
    out += ["", f"    {g.bold('Total in question: ' + money(total, 2))}", "",
            "    1. Call the number on the back of the card. Not a number from an email.",
            "    2. Say the words \"I want to dispute an unauthorised charge.\"",
            "    3. Ask them to freeze the card and issue a new number. If the card details",
            "       leaked, the same charge comes back next month under a different name.",
            "    4. Ask for the dispute in writing and keep the reference number.",
            "    5. Check the other cards for the same merchant. Whoever has your details",
            "       usually tries more than one.",
            "",
            g.dim("    In the US, the Fair Credit Billing Act generally gives you 60 days from"),
            g.dim("    the statement date to dispute in writing. Don't sit on it. Rules differ"),
            g.dim("    by country and card issuer, so ask them what their window is.")]
    return "\n".join(out)


# --------------------------------------------------------------------------
# The lectures
# --------------------------------------------------------------------------

def lecture_habits(g, spend, months):
    """Small repeated purchases. The coffee speech."""
    out = []
    by = defaultdict(list)
    for t in spend:
        if t["category"] not in HABIT_CATEGORIES or t["amount"] >= 40:
            continue
        if re.search(r"doordash|uber ?eats|grubhub|seamless|postmates", t["merchant"], re.I):
            continue                      # delivery gets its own lecture
        by[t["key"]].append(t)
    ranked = sorted(by.items(), key=lambda kv: -sum(t["amount"] for t in kv[1]))
    # a coffee habit is never allowed to slip past him on a technicality
    coffee = [kv for kv in ranked if COFFEE_RE.search(kv[0])]
    ranked = coffee[:2] + [kv for kv in ranked if kv not in coffee][:3]
    for key, ts in ranked:
        if len(ts) < 8:
            continue
        total = sum(t["amount"] for t in ts)
        per_year = total * 12 / months
        avg = total / len(ts)
        name = Counter(t["merchant"] for t in ts).most_common(1)[0][0]
        per_month = len(ts) / months
        out.append(f"  {g.bold(name)} — {len(ts)} visits, {money(total)}, "
                   f"average {money(avg, 2)}")
        out.append(f"    That's {per_month:.0f} times a month and {g.red(money(per_year))} a year.")
        if COFFEE_RE.search(name):
            out.append("    " + g.swear(
                "Fucking make the coffee at home. A bag of beans is $15 and lasts a month.",
                "Make the coffee at home. A bag of beans is $15 and lasts a month."))
            out.append(f"    Home brew runs about $0.40 a cup. You'd keep "
                       f"{g.green(money(per_year - len(ts) * 12 / months * 0.40))} a year.")
        else:
            half = per_year / 2
            out.append(f"    Cut it in half and that's {g.green(money(half))} back in your pocket.")
        out.append("")
    return out


def lecture_pro_tools(g, recurring):
    """Expensive software. He wants to see the return."""
    out = []
    for r in recurring:
        kind = classify(r["name"])
        if kind not in ("ai", "dev", "creative"):
            continue
        if r["per_month"] < 25:
            continue
        out.append(f"  {g.bold(r['name'])} — {money(r['amount'], 2)} {r['cadence']}, "
                   f"{g.red(money(r['per_year']))} a year")
        target = r["per_year"] * 5
        out.append(f"    So you're generating {g.bold(money(target))} a year with it, then?")
        out.append(f"    A tool that costs {money(r['per_month'])} a month needs to earn its keep.")
        out.append(f"    Break-even at $60/hour: it has to save you "
                   f"{r['per_month'] / 60:.1f} hours a month. Does it?")
        out.append(g.dim("    If yes, it's cheap and stop reading. If you can't answer, "
                         "that's your answer."))
        out.append("")
    return out


def lecture_streaming(g, recurring, led, months):
    """Overlapping entertainment services."""
    out = []
    groups = defaultdict(list)
    for r in recurring:
        kind = classify(r["name"])
        if kind in ("streaming", "music", "gaming", "news"):
            groups[kind].append(r)

    usage = usage_counts(led)
    for kind, items in groups.items():
        if len(items) < 2:
            continue
        total_year = sum(i["per_year"] for i in items)
        out.append(f"  {g.bold(str(len(items)) + ' ' + kind + ' services')} — "
                   f"{g.red(money(total_year))} a year, {money(total_year / 12)} a month")
        for i in items:
            used = usage.get(i["name"].lower(), 0)
            note = (g.dim("  (no usage logged)") if used == 0 else
                    g.dim(f"  ({used} uses logged)"))
            out.append(f"    · {i['name']:<28} {money(i['per_year']):>9}/yr{note}")
        out.append("")
        if kind == "streaming":
            out.append("    How many of these are you actually watching? You have one set of eyes.")
            cheapest = min(items, key=lambda i: i["per_year"])
            saving = total_year - cheapest["per_year"]
            out.append(f"    Keep one at a time and rotate. Same shows, "
                       f"{g.green(money(saving))} a year back.")
        elif kind == "music":
            out.append("    Two music services is one music service and a standing order "
                       "to a stranger.")
        else:
            out.append("    Pick the one you'd miss and drop the rest for three months. "
                       "See if you notice.")
        out.append(f"    A family plan split four ways puts these at about "
                   f"{money(total_year / 4 / 12)} each a month. Ask your friends.")
        out.append("")
    return out


def lecture_zombies(g, recurring, last_date):
    out = []
    for r in recurring:
        gap = (last_date - r["last"]).days
        if r["cadence"] == "monthly" and 40 < gap < 400:
            out.append(f"  {g.bold(r['name'])} — last charged {r['last']}, "
                       f"{gap} days ago, was {money(r['amount'], 2)} a month")
            out.append("    Either you cancelled it and forgot, or it's about to come back. "
                       "Check.")
            out.append("")
    return out


def lecture_fees(g, spend, months):
    out = []
    fees = [t for t in spend if t["category"] == "Fees/Interest"]
    if not fees:
        return out
    total = sum(t["amount"] for t in fees)
    per_year = total * 12 / months
    out.append(f"  {money(total)} in interest and fees over {months} months — "
               f"{g.red(money(per_year))} a year.")
    out.append("    You are paying rent on money you already spent. It bought you nothing.")
    kinds = Counter()
    for t in fees:
        kinds[t["merchant"][:30]] += t["amount"]
    for k, v in kinds.most_common(4):
        out.append(f"    · {k:<32} {money(v, 2):>10}")
    out.append("")
    out.append("    This is the only line on the whole statement where cutting it costs you")
    out.append("    nothing at all. No smaller apartment, no worse coffee. Just stop paying it.")
    out.append("")
    return out


def lecture_delivery(g, spend, months):
    out = []
    dining = [t for t in spend if t["category"] in ("Dining", "Delivery", "Coffee")]
    deliv = [t for t in dining if re.search(r"doordash|uber ?eats|grubhub|seamless|postmates",
                                            t["merchant"], re.I)]
    if not deliv or not dining:
        return out
    dt = sum(t["amount"] for t in deliv)
    at = sum(t["amount"] for t in dining)
    avg_d = dt / len(deliv)
    rest = [t for t in dining if t not in deliv]
    avg_r = (sum(t["amount"] for t in rest) / len(rest)) if rest else 0
    out.append(f"  {len(deliv)} delivery orders, {money(dt)} — {dt/at*100:.0f}% of "
               f"everything you spend on food out.")
    if avg_r:
        markup = (avg_d - avg_r) / avg_r * 100
        out.append(f"    {money(avg_d, 2)} a delivery order against {money(avg_r, 2)} "
                   f"in person. That's {markup:.0f}% for not walking.")
    out.append(f"    Halve it and keep {g.green(money(dt * 12 / months / 2))} a year.")
    out.append("")
    return out


INCOME_RE = re.compile(
    r"direct dep(osit)?|payroll|ach credit|salary|wages|"
    r"irs treas|tax ref(und)?|state refund|dividend|interest (paid|earned)|"
    r"mobile deposit|remote deposit|atm deposit|deposit from|"
    r"zelle from|venmo from|cash ?app from|paypal transfer from|"
    r"reimbursement|rebate|cashback bonus|settlement",
    re.I)


def is_income(t, led):
    """Money coming in: either it looks like it, or you told me it was."""
    rules = led.get("rules", {})
    if t["key"] in rules.get("income", []):
        return True
    return bool(INCOME_RE.search(t["merchant"]))


def is_ignored(t, led):
    return t["key"] in led.get("rules", {}).get("ignore", [])


# Debt that arrives disguised as a monthly expense.
INSTALMENT_RE = re.compile(
    r"affirm|klarna|afterpay|sezzle|zip ?co|quadpay|uplift|splitit|katapult|"
    r"instal?ment|loan payment|auto loan|car payment|student loan|navient|nelnet|"
    r"mohela|sallie mae|great lakes|aidvantage|earnest|sofi|upstart|lending ?club|"
    r"prosper|avant|oportun|best egg|happy money|payoff loan|consolidation",
    re.I)


# --------------------------------------------------------------------------
# Debt
# --------------------------------------------------------------------------

def import_statements(led, path):
    """Pull balance snapshots out of statements.csv from parse_statements.py."""
    if not os.path.exists(path):
        sys.exit(f"No such file: {path}")

    def num(r, k):
        v = r.get(k, "")
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    snaps = led.setdefault("debt", {}).setdefault("snapshots", [])
    have = {(s["card"], s["date"]) for s in snaps}
    added = 0
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            card = f"{r.get('issuer', '?')} ····{r.get('account_last4', '????')}"
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", r.get("statement_period", "") or "")
            when = m.group(1) if m else ""
            if not when or (card, when) in have:
                continue
            snaps.append({
                "card": card, "date": when,
                "balance": num(r, "new_balance"), "previous": num(r, "previous_balance"),
                "limit": num(r, "credit_limit"), "apr": num(r, "purchase_apr"),
                "minimum": num(r, "minimum_payment"),
                "interest": num(r, "interest_charged") or 0.0,
                "fees": num(r, "fees_charged") or 0.0,
                "payments": abs(num(r, "payments") or 0.0),
                "purchases": num(r, "purchases") or 0.0,
                "source": "statements.csv",
            })
            have.add((card, when))
            added += 1
    snaps.sort(key=lambda s: (s["card"], s["date"]))
    return added


def debt_picture(led):
    """Per card: where the balance is going, and whether payments touch it."""
    snaps = led.get("debt", {}).get("snapshots", [])
    by_card = defaultdict(list)
    for s in snaps:
        if s.get("balance") is not None:
            by_card[s["card"]].append(s)

    cards = []
    for card, items in by_card.items():
        items.sort(key=lambda s: s["date"])
        first, last = items[0], items[-1]
        months = max(len(items) - 1, 1)
        change = (last["balance"] or 0) - (first["balance"] or 0)
        per_month = change / months
        interest_total = sum(s["interest"] + s.get("fees", 0) for s in items)
        paid_total = sum(s["payments"] for s in items)
        spent_total = sum(s["purchases"] for s in items)
        # What actually came off the debt, once new spending is accounted for.
        net_paydown = paid_total - spent_total

        # Does the statement chain add up? previous + spend + interest - payments
        # should equal the new balance. If it doesn't, a statement is missing.
        breaks = []
        for a, b in zip(items, items[1:]):
            if a["balance"] is None or b.get("previous") is None:
                continue
            if abs(b["previous"] - a["balance"]) > 1.0:
                breaks.append((a["date"], b["date"], a["balance"], b["previous"]))

        apr = next((s["apr"] for s in reversed(items) if s.get("apr")), None)
        bal = last["balance"] or 0.0
        avg_interest = interest_total / len(items) if items else 0
        avg_payment = paid_total / len(items) if items else 0
        avg_principal = net_paydown / len(items) if items else 0

        # The only question that matters: is the balance actually going down?
        flat_band = max(bal * 0.005, 1.0)      # under 0.5% a month is not movement
        if bal < -1:
            verdict = "credit balance"
        elif abs(bal) <= 1:
            verdict = "clear"
        elif len(items) < 3:
            verdict = "too soon"               # one or two points is not a trend
        elif interest_total <= 0.5:
            verdict = "clear each month"       # carries a balance but pays in full
        elif per_month > flat_band:
            verdict = "growing"
        elif per_month >= -flat_band:
            verdict = "self-sustaining"        # frozen while interest accrues
        else:
            verdict = "falling"

        cards.append({
            "card": card, "months": len(items), "first": first, "last": last,
            "balance": bal, "limit": last.get("limit"), "apr": apr,
            "minimum": last.get("minimum"), "change": change, "per_month": per_month,
            "interest_total": interest_total, "avg_interest": avg_interest,
            "avg_payment": avg_payment, "avg_principal": avg_principal,
            "paid_total": paid_total, "spent_total": spent_total,
            "verdict": verdict, "breaks": breaks,
        })
    cards.sort(key=lambda c: -(c["apr"] or 0))
    return cards


def payoff_months(balance, apr, payment):
    if not balance or not apr or not payment or payment <= 0:
        return None
    import math
    r = apr / 100.0 / 12.0
    if payment <= balance * r + 0.01:
        return None
    return math.log(payment / (payment - balance * r)) / math.log(1 + r)


def lecture_debt(g, led, spend, months):
    """The debt section of the audit."""
    out = []
    cards = debt_picture(led)

    # Instalment plans hiding in the transaction list
    inst = defaultdict(list)
    for t in spend:
        if INSTALMENT_RE.search(t["merchant"]):
            inst[t["key"]].append(t)
    if inst:
        monthly = sum(sum(x["amount"] for x in ts) / months for ts in inst.values())
        out.append(f"  {g.bold('This is debt, not spending:')}")
        for k, ts in sorted(inst.items(), key=lambda kv: -sum(x["amount"] for x in kv[1])):
            tot = sum(x["amount"] for x in ts)
            out.append(f"    · {ts[0]['merchant'][:32]:<34}{len(ts):>3} payments  "
                       f"{money(tot):>9}  {money(tot / months)}/mo")
        out.append(f"    {money(monthly)} a month of your budget is already promised to "
                   f"someone else")
        out.append("    before you buy a single thing. Buy-now-pay-later is a loan wearing "
                   "a checkout button.")
        out.append("")

    if not cards:
        if not inst:
            return out
        out.append(g.dim("  No card balances on record. Run:"))
        out.append(g.dim("    financial_auditor.py debt --statements statements.csv"))
        out.append(g.dim("    financial_auditor.py debt add --card \"Chase 4821\" "
                         "--balance 4200 --apr 21.49 --min 85"))
        return out

    owed = [c for c in cards if c["balance"] > 1]
    total_bal = sum(c["balance"] for c in owed)
    total_int = sum(c["interest_total"] for c in cards)
    out.append(f"  You owe {g.bold(g.red(money(total_bal)))} across "
               f"{len(owed)} card{'s' if len(owed) != 1 else ''}.")
    out.append("")

    for c in cards:
        apr = f"{c['apr']:.2f}%" if c["apr"] else "APR unknown"
        out.append(f"  {g.bold(c['card'])} — {money(c['balance'])} at {apr}, "
                   f"{c['months']} statement(s) on record")

        if c["verdict"] == "credit balance":
            out.append(f"    You have overpaid by {money(abs(c['balance']))}. "
                       f"The card is holding your money and paying you nothing for it. "
                       f"Ask for it back.")
        elif c["verdict"] == "too soon":
            out.append(f"    {g.dim('Only ' + str(c['months']) + ' snapshot(s). I need three '
                                    'before I will tell you which way this is going.')}")
            out.append(f"    Record it again next month: "
                       f"`debt add --card '{c['card']}' --balance ...`")
        elif c["verdict"] == "clear":
            out.append(f"    {g.green('Nothing owed. Good. Keep it that way.')}")
        elif c["verdict"] == "clear each month":
            out.append(f"    {g.green('You clear this every month and pay no interest. '
                                      'That is how a card is meant to work.')}")
        elif c["verdict"] == "self-sustaining":
            out.append(f"    {g.red('This balance is not moving.')} It has changed by "
                       f"{money(abs(c['change']))} in {c['months']} statements, while you "
                       f"paid {money(c['interest_total'])} in interest.")
            out.append(f"    You pay {money(c['avg_payment'])} a month and the balance "
                       f"moves by {money(abs(c['per_month']))}. The rest of that payment "
                       f"funds new spending and interest.")
            out.append("    " + g.red(g.bold("This is a subscription you never signed up for "
                                             "and can't cancel.")))
            out.append(f"    It renews every month at {money(c['avg_interest'])} and it "
                       f"never ends until the balance does.")
        elif c["verdict"] == "growing":
            out.append(f"    {g.red('Going the wrong way.')} Up {money(c['change'])} over "
                       f"{c['months']} statements — {money(c['per_month'])} a month.")
            out.append(f"    At this rate you add {money(c['per_month'] * 12)} of debt a year "
                       f"and pay interest on all of it.")
        else:
            rate = abs(c["per_month"])
            eta = c["balance"] / rate if rate > 0 else None
            msg = f"    {g.green('Coming down')} {money(rate)} a month."
            if eta and eta < 600:
                msg += f" Clear in about {eta:.0f} months if you hold that line."
            out.append(msg)

        if c["apr"] and c["balance"] > 50 and c["verdict"] != "too soon":
            pay = max(c["avg_payment"] - (c["spent_total"] / max(c["months"], 1)), 0)
            realistic = pay if pay > c["balance"] * c["apr"] / 1200 else (c["minimum"] or 0)
            n0 = payoff_months(c["balance"], c["apr"], realistic)
            n1 = payoff_months(c["balance"], c["apr"], realistic + 100)
            if n0 and n1:
                i0 = n0 * realistic - c["balance"]
                i1 = n1 * (realistic + 100) - c["balance"]
                out.append(f"    At {money(realistic)}/month: {n0:.0f} months, "
                           f"{money(i0)} interest. "
                           f"At {money(realistic + 100)}: {n1:.0f} months, {money(i1)} — "
                           f"{g.green(money(i0 - i1) + ' saved')}.")
            elif realistic > 0:
                out.append(f"    {g.red('At ' + money(realistic) + ' a month this never '
                                        'clears.')} The interest outruns the payment.")

        if c["limit"] and c["balance"] > 0:
            util = c["balance"] / c["limit"] * 100
            flag = g.red(f"{util:.0f}%") if util >= 30 else g.green(f"{util:.0f}%")
            out.append(f"    Using {flag} of a {money(c['limit'])} limit.")

        for b in c["breaks"][:2]:
            out.append(f"    {g.amber('Gap in the record:')} {b[0]} closed at "
                       f"{money(b[2])} but {b[1]} opened at {money(b[3])}. "
                       f"A statement is missing or misread.")
        out.append("")

    revolvers = [c for c in cards if c["verdict"] in ("self-sustaining", "growing")]
    if len(cards) > 1 and revolvers:
        target = max(revolvers, key=lambda c: c["apr"] or 0)
        if target["apr"]:
            out.append(f"  {g.bold('Where the next spare dollar goes:')} "
                       f"{target['card']}, at {target['apr']:.2f}%.")
            out.append("    Highest rate first. Minimums on everything else. "
                       "It is not complicated.")
            out.append("")
    if total_int > 0:
        out.append(f"  {money(total_int)} of interest across these statements. "
                   f"That bought you nothing at all.")
        out.append("")
    return out


def cmd_debt(args):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)

    if args.card:
        snaps = led.setdefault("debt", {}).setdefault("snapshots", [])
        when = args.date or date.today().isoformat()
        snaps = [s for s in snaps if not (s["card"] == args.card and s["date"] == when)]
        snaps.append({"card": args.card, "date": when, "balance": args.balance,
                      "previous": None, "limit": args.limit, "apr": args.apr,
                      "minimum": args.min, "interest": args.interest or 0.0, "fees": 0.0,
                      "payments": args.paid or 0.0, "purchases": args.spent or 0.0,
                      "source": "manual"})
        snaps.sort(key=lambda s: (s["card"], s["date"]))
        led["debt"]["snapshots"] = snaps
        save_ledger(led, args.ledger)
        print(f"Recorded {args.card}: {money(args.balance or 0)} on {when}.")
        print("Add one every month and I can tell you whether it's actually moving.")
        return

    if args.statements:
        n = import_statements(led, args.statements)
        save_ledger(led, args.ledger)
        print(f"Imported {n} new statement snapshot(s).")

    cards = debt_picture(led)
    if not cards:
        print("No debt on record yet. Give me something to work with:\n"
              "  financial_auditor.py debt --statements cleanedData/statements.csv\n"
              "  financial_auditor.py debt add --card \"Chase 4821\" --balance 4200 "
              "--limit 9000 --apr 21.49 --min 85")
        return

    total = sum(c["balance"] for c in cards if c["balance"] > 1)
    print(header(g, "WHAT YOU OWE", f"{len(cards)} account(s) · {money(total)} outstanding"))
    print()
    spend = []
    if args.csv:
        txns = load(args.csv)
        spend = [t for t in txns if t["amount"] > 0
                 and t["category"] not in ("Payment", "Credits/Refunds", "Transfers/Cash")]
    print("\n".join(lecture_debt(g, led, spend, max(months_covered(spend), 1) if spend else 1)))

    hist = led.get("debt", {}).get("snapshots", [])
    if len(hist) >= 2:
        print(f"  {g.dim('Balance history')}")
        for c in cards:
            items = sorted([s for s in hist if s["card"] == c["card"]],
                           key=lambda s: s["date"])
            spark = "  ".join(f"{s['date'][:7]} {money(s['balance'] or 0)}"
                              for s in items[-6:])
            print(f"    {c['card']:<22} {spark}")
        print()
    print(g.dim("  Record a balance every month and this gets sharper: "
                "`debt add --card ... --balance ...`\n"))

def usage_counts(led, days=30):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    c = Counter()
    for u in led["usage"]:
        if u["date"] >= cutoff:
            c[u["service"].lower()] += 1
    return c


# --------------------------------------------------------------------------
# Subscription usage
# --------------------------------------------------------------------------

def cmd_use(args):
    led = load_ledger(args.ledger)
    led["usage"].append({"service": args.service, "date": date.today().isoformat(),
                         "minutes": args.minutes or 0})
    save_ledger(led, args.ledger)
    n = usage_counts(led)[args.service.lower()]
    print(f"Logged. {args.service}: {n} use(s) in the last 30 days.")


def cmd_usage(args):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)
    if not led["usage"]:
        print("No usage logged yet.\n"
              "  Log it as you go:   financial_auditor.py use \"Netflix\"\n"
              "  Or let it watch:    financial_auditor.py monitor --minutes 480")
        return

    subs = led.get("decisions", {}).get("subscriptions", {})
    if args.csv:
        txns = load(args.csv)
        spend = [t for t in txns if t["amount"] > 0]
        USABLE = {"Subscriptions", "Software/Tools", "Fitness", "Entertainment", "Gaming"}
        for r in detect_recurring(spend):
            if classify(r["name"]) is None and r["category"] not in USABLE:
                continue          # rent and interest are not things you "use"
            subs.setdefault(r["name"], {"per_month": round(r["per_month"], 2)})
        led.setdefault("decisions", {})["subscriptions"] = subs
        save_ledger(led, args.ledger)

    counts = usage_counts(led, args.days)
    print(header(g, "SUBSCRIPTION USAGE", f"last {args.days} days"))
    print(f"\n  {'service':<26}{'uses':>6}{'cost/mo':>10}{'cost/use':>11}  verdict")
    print("  " + rule(70))

    NOT_A_SERVICE = re.compile(r"interest|late fee|annual fee|membership fee|rent|"
                               r"mortgage|insurance|payment|electric|water|utility", re.I)
    subs = {k: v for k, v in subs.items() if not NOT_A_SERVICE.search(k)}
    led.setdefault("decisions", {})["subscriptions"] = subs
    save_ledger(led, args.ledger)

    rows = []
    for name, meta in subs.items():
        uses = counts.get(name.lower(), 0)
        cost = meta.get("per_month", 0)
        per_use = cost / uses if uses else None
        rows.append((name, uses, cost, per_use))
    for name, uses, cost in [(n, counts[n], 0) for n in counts
                             if n not in {s.lower() for s in subs}]:
        rows.append((name.title(), uses, 0, None))

    total_waste = 0.0
    for name, uses, cost, per_use in sorted(rows, key=lambda r: -(r[2] or 0)):
        if per_use is None and cost:
            verdict, colour = "CANCEL — you never opened it", g.red
            total_waste += cost * 12
        elif per_use is None:
            verdict, colour = "no price known", g.dim
        elif per_use > 15:
            verdict, colour = "CANCEL or share it", g.red
            total_waste += cost * 12 * 0.5
        elif per_use > 5:
            verdict, colour = "split it with friends", g.amber
        elif per_use > 1:
            verdict, colour = "fine", g.dim
        else:
            verdict, colour = "worth every penny", g.green
        pu = money(per_use, 2) if per_use else "—"
        print(f"  {name[:25]:<26}{uses:>6}{money(cost, 2):>10}{pu:>11}  {colour(verdict)}")

    print()
    if total_waste > 0:
        print(f"  {g.red(g.bold(money(total_waste) + ' a year'))} is going to things you "
              f"barely open.")
        print(f"  Split the shareable ones four ways and that's another "
              f"{g.green(money(total_waste / 4))} back.")
    else:
        print(f"  {g.green('Nothing obviously wasted. Fine. I said fine.')}")
    print(g.dim("\n  Nothing logged is not the same as never used. Log honestly for two "
                "weeks before you\n  cancel anything — then the numbers above mean "
                "something."))


def sample_processes():
    """One snapshot of running desktop apps."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10)
        else:
            out = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True,
                                 timeout=10)
        text = out.stdout.lower()
    except Exception:
        return set()
    hits = set()
    for service, needles in PROCESS_MAP.items():
        if any(n in text for n in needles):
            hits.add(service)
    return hits


def cmd_monitor(args):
    led = load_ledger(args.ledger)
    print(f"Watching for known apps every {args.every}s. Ctrl-C to stop.")
    print("Browser tabs are invisible to this — log those with `use`.\n")
    seen_today = {u["service"] for u in led["usage"] if u["date"] == date.today().isoformat()}
    end = time.time() + args.minutes * 60
    try:
        while time.time() < end:
            for s in sample_processes():
                if s not in seen_today:
                    seen_today.add(s)
                    led["usage"].append({"service": s, "date": date.today().isoformat(),
                                         "minutes": 0, "source": "monitor"})
                    save_ledger(led, args.ledger)
                    print(f"  {datetime.now():%H:%M}  saw {s}")
            if args.once:
                break
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nStopped.")
    print(f"\n{len(seen_today)} service(s) seen today. Run `usage` for the verdict.")


def _rule_change(args, kind, label):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)
    key = merchant_key(args.merchant)
    lst = led.setdefault("rules", {}).setdefault(kind, [])

    if args.undo:
        led["rules"][kind] = [k for k in lst if k != key]
        save_ledger(led, args.ledger)
        print(f"Fine. {args.merchant} counts as spending again.")
        return

    if key not in lst:
        lst.append(key)
    save_ledger(led, args.ledger)
    print(f"Noted. Anything matching {g.bold(key)} is {label} from now on.")

    if args.csv:
        txns = load(args.csv)
        hits = [t for t in txns if t["key"] == key and t["amount"] > 0]
        if hits:
            tot = sum(t["amount"] for t in hits)
            print(f"That's {len(hits)} transaction(s) worth {money(tot)} that will stop "
                  f"counting against you.")
        else:
            print(f"No transactions matched {key} in that file. Check the spelling — "
                  f"I match on the cleaned merchant name.")


def cmd_income(args):
    _rule_change(args, "income", "money coming in, not spending")


def cmd_ignore(args):
    _rule_change(args, "ignore", "left out of the audit")


def cmd_rules(args):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)
    rules = led.get("rules", {})
    if not any(rules.values()):
        print("You haven't told me anything yet.")
        return
    for kind, label in (("income", "counted as money coming in"),
                        ("ignore", "left out entirely")):
        if rules.get(kind):
            print(f"\n  {g.bold(label.title())}:")
            for k in rules[kind]:
                print(f"    · {k}")
    print()


def cmd_disputes(args):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)
    if not led["disputes"]:
        print("Nothing flagged. Either you explained everything or you haven't run "
              "`audit -i` yet.")
        return
    print(header(g, "CHARGES YOU COULD NOT EXPLAIN"))
    print(fraud_checklist(g, [{"amount": d["amount"],
                               "t": {"date": d["date"], "merchant": d["merchant"]}}
                              for d in led["disputes"]]))


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def cmd_audit(args):
    g = Grandpa(not args.clean, not args.no_color)
    led = load_ledger(args.ledger)
    txns = load(args.csv)
    skip = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"} | _NON_SPEND
    all_spend = [t for t in txns if t["amount"] > 0 and t["category"] not in skip]
    income = [t for t in all_spend if is_income(t, led)]
    ignored = [t for t in all_spend if is_ignored(t, led) and t not in income]
    spend = [t for t in all_spend if t not in income and t not in ignored]
    if not spend:
        sys.exit("Every row is a payment or a transfer. Check the signs in your CSV.")

    months = months_covered(spend)
    last_date = max(t["d"] for t in spend)
    total = sum(t["amount"] for t in spend)
    per_month = total * 1.0 / months
    recurring = detect_recurring(spend)
    committed = sum(r["per_month"] for r in recurring)
    lines = []

    print(header(g, "SIT DOWN. WE NEED TO TALK ABOUT YOUR SPENDING.",
                 f"{len(spend):,} charges · {months} months · {min(t['date'] for t in spend)}"
                 f" to {max(t['date'] for t in spend)}"))

    print(f"\n  You spent {g.bold(g.red(money(total)))} in {months} months. "
          f"That's {g.bold(money(per_month))} a month.")
    if income or ignored:
        bits = []
        if income:
            bits.append(f"{money(sum(t['amount'] for t in income))} of money coming in")
        if ignored:
            bits.append(f"{len(ignored)} charge(s) you told me to ignore")
        print(f"  {g.dim('Left out of that: ' + ' and '.join(bits) + '.')}")
    print(f"  {money(committed)} of that renews every month whether you think about it or not.")
    unc = sum(t["amount"] for t in spend if t["category"] == "Uncategorized")
    if unc > total * 0.15:
        print(f"  {g.red(money(unc))} of it is filed under 'Uncategorized'. "
              f"That's {unc/total*100:.0f}%.")
        print("  You don't know where a fifth of your money went. We'll come back to that.")

    n = 0

    # 1. interrogation
    questions = find_questions(spend, recurring, led, months)
    if questions:
        n += 1
        print(section(g, n, "explain yourself"))
        print(f"\n  {len(questions)} charge(s) need an explanation.\n")
        if args.interactive:
            unexplained = []
            for q in questions[:args.limit]:
                t = q["t"]
                print(f"  {g.bold(money(q['amount'], 2))}  {t['date']}  {g.bold(t['merchant'])}")
                print(f"    {g.dim(q['why'])}")
                try:
                    ans = input("    What was it? (enter = I don't recognise it) > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n  Fine. Walk away. We'll do this later.")
                    break
                if ans:
                    led["explanations"][q["id"]] = {"answer": ans,
                                                    "when": date.today().isoformat()}
                    if re.search(r"\bincome\b|salary|paycheck|payroll|paid me|payment to me|"
                                 r"client|invoice|refund|reimburs|deposit|sold|"
                                 r"money (coming )?in", ans, re.I):
                        key = t.get("key") or merchant_key(t["merchant"])
                        led.setdefault("rules", {}).setdefault("income", [])
                        if key not in led["rules"]["income"]:
                            led["rules"]["income"].append(key)
                        print(f"    {g.green('Noted — and that is money coming in, not '
                                             'spending.')}")
                        print(f"    {g.dim('I will stop counting ' + t['merchant'] + ' as spending.')}\n")
                    else:
                        print(f"    {g.green('Noted.')}\n")
                else:
                    unexplained.append(q)
                    led["disputes"].append({"date": t["date"], "merchant": t["merchant"],
                                            "amount": q["amount"],
                                            "flagged": date.today().isoformat()})
                    print(f"    {g.red('Then it is not yours until proven otherwise.')}\n")
            save_ledger(led, args.ledger)
            if unexplained:
                print(fraud_checklist(g, unexplained))
        else:
            for q in questions[:args.limit]:
                t = q["t"]
                print(f"  {money(q['amount'], 2):>10}  {t['date']}  {t['merchant']}")
                print(f"              {g.dim(q['why'])}")
            print(f"\n  {g.bold('Run with -i and I will ask you about each one.')}")
            print(g.dim("  Anything you can't explain gets treated as fraud, which is how "
                        "it should be."))
            print(g.dim("  If one of those was money coming IN, tell me once and I'll stop "
                        "counting it as"))
            print(g.dim("  spending:  financial_auditor.py income \"MERCHANT NAME\""))

    for title, body in [
        ("the habit", lecture_habits(g, spend, months)),
        ("delivery", lecture_delivery(g, spend, months)),
        ("what you pay for tools", lecture_pro_tools(g, recurring)),
        ("how many screens do you have", lecture_streaming(g, recurring, led, months)),
        ("subscriptions that stopped charging", lecture_zombies(g, recurring, last_date)),
        ("money for nothing", lecture_fees(g, spend, months)),
        ("what you owe", lecture_debt(g, led, spend, months)),
    ]:
        if body:
            n += 1
            print(section(g, n, title))
            print()
            print("\n".join(body))

    # closing
    n += 1
    print(section(g, n, "what I want you to do"))
    todo = []
    if questions:
        todo.append(f"Explain or dispute {len(questions)} charge(s). "
                    f"Run this again with -i.")
    fees_year = sum(t["amount"] for t in spend if t["category"] == "Fees/Interest") * 12 / months
    if fees_year > 50:
        todo.append(f"Stop paying {money(fees_year)}/yr in interest and fees. "
                    f"Pay the full balance, not the minimum.")
    subs_year = sum(r["per_year"] for r in recurring
                    if classify(r["name"]) in ("streaming", "music", "gaming", "news"))
    if subs_year > 100:
        todo.append(f"Log what you actually use for two weeks "
                    f"(`use \"Netflix\"`), then cancel what's left. "
                    f"{money(subs_year)}/yr is on the table.")
    if unc > total * 0.15:
        todo.append(f"Categorise the {money(unc)} you can't account for. "
                    f"Run llm_enrich.py first.")
    stuck = [c for c in debt_picture(led)
             if c["verdict"] in ("self-sustaining", "growing")]
    if stuck:
        worst = max(stuck, key=lambda c: c["apr"] or 0)
        todo.append(f"Your balance on {worst['card']} isn't moving. Put every spare "
                    f"dollar there first — it's the highest rate you're paying.")
    elif not debt_picture(led):
        todo.append("Give me your balances so I can tell you if the debt is actually "
                    "going down: `debt --statements statements.csv`")
    if not todo:
        todo.append("Nothing egregious. Don't let it go to your head.")
    print()
    for i, item in enumerate(todo, 1):
        print(f"  {g.bold(str(i) + '.')} {item}")
    print()
    print(g.dim("  Ledger saved to " + args.ledger + ". I remember what you told me."))
    print()

    if args.report:
        with open(args.report, "w") as fh:
            plain = Grandpa(not args.clean, color=False)
            fh.write(f"# Audit — {date.today():%d %b %Y}\n\n")
            fh.write(f"- Period: {min(t['date'] for t in spend)} to "
                     f"{max(t['date'] for t in spend)} ({months} months)\n")
            fh.write(f"- Total spend: {money(total)} ({money(per_month)}/month)\n")
            fh.write(f"- Committed monthly: {money(committed)}\n")
            fh.write(f"- Charges needing explanation: {len(questions)}\n\n")
            fh.write("## Charges to explain\n\n")
            for q in questions[:args.limit]:
                fh.write(f"- {q['t']['date']}  {money(q['amount'], 2)}  "
                         f"{q['t']['merchant']} — {q['why']}\n")
            fh.write("\n## Standing charges\n\n")
            for r in recurring:
                fh.write(f"- {r['name']}: {money(r['amount'], 2)} {r['cadence']}, "
                         f"{money(r['per_year'])}/yr\n")
            fh.write("\n## To do\n\n")
            for i, item in enumerate(todo, 1):
                fh.write(f"{i}. {re.sub(chr(27) + r'.[0-9;]*m', '', item)}\n")
        print(g.dim(f"  Written to {args.report}\n"))


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Your grandfather reviews your statements. He has opinions.")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--clean", action="store_true", help="he keeps it polite")
    ap.add_argument("--no-color", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="go through the statements")
    a.add_argument("csv")
    a.add_argument("-i", "--interactive", action="store_true",
                   help="he asks about each charge and remembers your answers")
    a.add_argument("--limit", type=int, default=15, help="how many charges to question")
    a.add_argument("--report", help="also write a markdown report")
    a.set_defaults(func=cmd_audit)

    u = sub.add_parser("use", help="log that you used a subscription today")
    u.add_argument("service")
    u.add_argument("--minutes", type=int)
    u.set_defaults(func=cmd_use)

    g = sub.add_parser("usage", help="cost per use, and the verdict on each subscription")
    g.add_argument("--csv", help="transactions CSV, to price the subscriptions")
    g.add_argument("--days", type=int, default=30)
    g.set_defaults(func=cmd_usage)

    m = sub.add_parser("monitor", help="watch which apps you actually open")
    m.add_argument("--minutes", type=int, default=480, help="how long to watch")
    m.add_argument("--every", type=int, default=60, help="seconds between checks")
    m.add_argument("--once", action="store_true", help="take one sample and exit")
    m.set_defaults(func=cmd_monitor)

    b = sub.add_parser("debt", help="what you owe, and whether it is actually moving")
    b.add_argument("--statements", help="statements.csv from parse_statements.py")
    b.add_argument("--csv", help="transactions CSV, to spot instalment plans")
    b.add_argument("add", nargs="?", help=argparse.SUPPRESS)
    b.add_argument("--card", help="e.g. \"Chase 4821\"")
    b.add_argument("--balance", type=float)
    b.add_argument("--limit", type=float)
    b.add_argument("--apr", type=float)
    b.add_argument("--min", type=float, help="minimum payment")
    b.add_argument("--interest", type=float, help="interest charged this statement")
    b.add_argument("--paid", type=float, help="payments made this statement")
    b.add_argument("--spent", type=float, help="new purchases this statement")
    b.add_argument("--date", help="statement date, defaults to today")
    b.set_defaults(func=cmd_debt)

    inc = sub.add_parser("income", help="mark a merchant as money coming IN, not spending")
    inc.add_argument("merchant")
    inc.add_argument("--csv", help="transactions CSV, to show what this changes")
    inc.add_argument("--undo", action="store_true")
    inc.set_defaults(func=cmd_income)

    ig = sub.add_parser("ignore", help="leave a merchant out of the audit entirely")
    ig.add_argument("merchant")
    ig.add_argument("--csv")
    ig.add_argument("--undo", action="store_true")
    ig.set_defaults(func=cmd_ignore)

    r = sub.add_parser("rules", help="show what you've told me")
    r.set_defaults(func=cmd_rules)

    d = sub.add_parser("disputes", help="what to call the bank about")
    d.set_defaults(func=cmd_disputes)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()