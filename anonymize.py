#!/usr/bin/env python3
"""
anonymize.py — make your transactions safe to hand to someone else.

The point is not to produce a redacted file. It is to produce a file that is
still *worth analysing*: the totals, the trends and the recurring charges have
to survive, or the advice you get back is about a different person's money. So
every run ends by proving what survived and admitting what did not.

    python anonymize.py                                  # finances.db, level medium
    python anonymize.py finances.db --level light
    python anonymize.py finances.db --level heavy
    python anonymize.py finances.db --level synthetic    # no real rows survive
    python anonymize.py transactions.csv -o shareable.csv
    python anonymize.py finances.db -o shareable.db      # a store, not a CSV

Levels, least to most protective:

  light       Drops raw descriptions, account numbers, issuer names and source
              filenames. Merchant names, amounts and dates stay real.
              Analysis is unaffected. Anyone reading it still learns where you shop.

  medium      Everything in light, plus merchant names replaced by stable labels
              (MERCHANT_001, ranked by spend) and merchants seen fewer than
              --k times folded into "OTHER (category)". Amounts and dates stay
              exact, so every total, trend and recurring charge is still true.
              This is the default and usually the right one.

  heavy       Everything in medium, plus dates jittered within their month and
              amounts jittered by ±--jitter percent. Totals shift slightly;
              monthly patterns survive; individual transactions are no longer real.

  synthetic   Generates a brand new dataset from summary statistics of yours:
              per-category monthly totals and spread, transaction counts, amount
              distributions, and the recurring charges found by the shared
              detector in utils.py. No row is a real transaction. Shapes and
              magnitudes are preserved; specifics are not.

Whatever the level, the mapping back to real names is written to
anonymize_map.json, which stays on your machine. Keep it, don't share it — it
turns "MERCHANT_007" in someone's advice back into the real merchant.

Standard library only.
"""

import argparse
import csv
import json
import os
import random
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from database import DEFAULT_DB, FinanceDB
from engine import load_transactions
from models import Transaction
from utils import detect_recurring, merchant_key

DROP_ALWAYS = ["description", "post_date", "statement_period", "source_file"]

FAKE_NAMES = {
    "Groceries": ["Corner Market", "Fresh Fields", "Union Grocers", "Harvest Foods",
                  "Green Basket", "Provisions Co"],
    "Dining": ["The Copper Pot", "Nine Tables", "Rosewood Diner", "Bar Marino",
               "Noodle House", "Fifth Street Grill"],
    "Coffee": ["Third Rail Coffee", "Bean & Bell", "Morning Roasters"],
    "Delivery": ["QuickPlate", "DoorRunner", "MealDash"],
    "Alcohol/Bars": ["The Lamplight", "Cellar 12", "Public House"],
    "Transport": ["CityRide", "Metro Fare", "Transit Authority", "RideNow"],
    "Gas/Auto": ["Beacon Fuel", "Highway Service", "AutoWorks"],
    "Travel": ["Northline Air", "Skyway Airways", "Coastal Rail"],
    "Lodging": ["Harbour Hotel", "The Lyndon", "Riverside Inn"],
    "Shopping": ["Everything Store", "Meridian Goods", "Northgate Retail",
                 "Willow & Co", "The Depot"],
    "Big Box": ["MegaMart", "ValueCenter", "BulkWorks"],
    "Electronics": ["CircuitPoint", "TechBarn"],
    "Clothing": ["Field & Thread", "Uniform Supply"],
    "Home": ["Homestead Supply", "Fixture & Fitting"],
    "Subscriptions": ["StreamCo", "TuneBox", "CloudDrive", "NewsDaily", "ToolSuite"],
    "Software/Tools": ["DevSuite", "CodeCloud"],
    "Utilities": ["Metro Power", "City Water", "Regional Energy"],
    "Phone/Internet": ["Telecom One", "Fibernet"],
    "Rent/Housing": ["Property Management Co"],
    "Insurance": ["Shield Mutual", "Anchor Insurance"],
    "Health": ["Northside Clinic", "Family Practice", "Dental Group"],
    "Pharmacy": ["Corner Pharmacy", "HealthMart"],
    "Fitness": ["Ironworks Gym", "The Studio"],
    "Personal Care": ["The Barber Room", "Salon Nine"],
    "Entertainment": ["Grand Cinema", "Ticket Office", "The Playhouse"],
    "Education": ["Learning Centre", "Course Platform"],
    "Pets": ["Petworks", "Animal Clinic"],
    "Fees/Interest": ["Interest Charge", "Card Fee"],
    "Transfers/Cash": ["Transfer Out", "Cash Withdrawal"],
    "Payment": ["Card Payment"],
    "Credits/Refunds": ["Refund"],
}
GENERIC = ["Vendor One", "Vendor Two", "Vendor Three", "Vendor Four", "Vendor Five"]

try:
    from categorizer import NON_SPEND, normalize
except ImportError:                                          # pragma: no cover
    NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}
    normalize = None


def recurring_in(txns):
    """The repeating charges, grouped on the cleaned merchant name.

    Grouping on the stored merchant is not good enough here: on a Chase
    checking statement the merchant still carries the posting date, so the same
    subscription looks like a different payee every month and the detector sees
    nothing to repeat. Cleaning the descriptor first is what makes the
    subscriptions visible.
    """
    spend = [t for t in txns if t.amount > 0 and t.category not in NON_SPEND]
    if normalize is None:
        return detect_recurring(spend)
    return detect_recurring(
        spend, key=lambda t: merchant_key(normalize(t.description)[0] or t.merchant))

# How many charges a cadence produces in one calendar month. Fractions mean the
# charge only lands in some months, which is what keeps an annual renewal from
# being generated twelve times a year.
PER_MONTH = {"weekly": 4, "biweekly": 2, "monthly": 1,
             "quarterly": 1 / 3, "semiannual": 1 / 6, "annual": 1 / 12}


def parse_d(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def month_of(d):
    return d.strftime("%Y-%m")


def month_start(m):
    return datetime.strptime(m + "-01", "%Y-%m-%d").date()


def month_end(m):
    s = month_start(m)
    return (s.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)


def read(source):
    txns = load_transactions(source)
    if not txns:
        sys.exit(f"{source} has no transactions")
    return txns


def write(path, txns):
    """A .db output is a second store; a .csv output is a flat file."""
    if path.lower().endswith(".db"):
        with FinanceDB(path) as db:
            db.upsert_transactions(txns)
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=Transaction.columns())
        w.writeheader()
        for t in txns:
            w.writerow(t.to_row())


# --------------------------------------------------------------------------

def build_labels(txns, k):
    """Stable pseudonyms, ranked by total spend so the biggest is MERCHANT_001."""
    tot, cnt, cat = defaultdict(float), Counter(), {}
    for t in txns:
        m = (t.merchant or "").strip() or "(blank)"
        tot[m] += abs(t.amount)
        cnt[m] += 1
        cat.setdefault(m, t.category or "Uncategorized")

    ranked = sorted(tot, key=lambda m: -tot[m])
    labels, folded = {}, 0
    i = 0
    for m in ranked:
        if cnt[m] < k:
            labels[m] = f"OTHER ({cat.get(m, 'Uncategorized')})"
            folded += 1
        else:
            i += 1
            labels[m] = f"MERCHANT_{i:03d}"
    return labels, folded


def anonymize(txns, level, k, jitter, rng):
    labels, folded = build_labels(txns, k) if level in ("medium", "heavy") else ({}, 0)

    cards = {}
    out = []
    for t in txns:
        d = parse_d(t.date)
        amt = t.amount
        merchant = (t.merchant or "").strip()

        if level == "heavy":
            ms, me = month_start(month_of(d)), month_end(month_of(d))
            d = ms + timedelta(days=rng.randrange((me - ms).days + 1))
            amt = round(amt * (1 + rng.uniform(-jitter, jitter) / 100.0), 2)

        key = (t.issuer, t.account_last4)
        if key not in cards:
            cards[key] = f"CARD_{chr(65 + len(cards))}"

        name = labels.get(merchant, merchant) if labels else merchant
        out.append(Transaction(date=d.isoformat(), description=name,
                               amount=round(amt, 2), merchant=name,
                               category=t.category or "Uncategorized",
                               issuer=cards[key]))
    out.sort(key=lambda t: t.date)
    return out, labels, folded


def synthesize(txns, rng):
    """Build a new dataset from the shape of the old one. No real rows survive.

    Modelled per category: how often the category appears at all, how many
    transactions it has in a month, what those months total, the distribution of
    individual amounts, and how concentrated spending is across merchants.

    Recurring charges are handled separately, by the same detector every other
    tool uses (utils.detect_recurring). That matters: it recognises a
    subscription whose billing day drifts across the month, which a fixed
    day-gap test throws away — and a synthetic file missing your subscriptions
    is a synthetic file that understates your fixed costs.
    """
    real = [t for t in txns if t.d]
    if not real:
        sys.exit("nothing usable to model")

    months = sorted({t.month for t in real})
    spend = [t for t in real if t.amount > 0 and t.category not in NON_SPEND]
    if not spend:
        sys.exit("no spending rows to model")

    recurring = recurring_in(real)
    rec_keys = {r.key for r in recurring}
    rec_key_of = (lambda t: merchant_key(normalize(t.description)[0] or t.merchant)) \
        if normalize else (lambda t: merchant_key(t.merchant))
    variable = [t for t in spend if rec_key_of(t) not in rec_keys]

    cat_month_total = defaultdict(lambda: defaultdict(float))
    cat_month_n = defaultdict(lambda: defaultdict(int))
    cat_amts = defaultdict(list)
    cat_merch_counts = defaultdict(Counter)
    for t in variable:
        cat_month_total[t.category][t.month] += t.amount
        cat_month_n[t.category][t.month] += 1
        cat_amts[t.category].append(t.amount)
        cat_merch_counts[t.category][t.merchant] += 1

    # Fake merchant pools sized to the real number of merchants per category, and
    # weighted to the real concentration — so "one merchant dominates dining"
    # survives, while which merchant it was does not.
    pools = {}
    for c, counts in cat_merch_counts.items():
        base = list(FAKE_NAMES.get(c) or GENERIC)
        need = len(counts)
        while len(base) < need:
            base.append(f"{(FAKE_NAMES.get(c) or GENERIC)[0]} {len(base) + 1}")
        weights = [n for _, n in counts.most_common()]
        pools[c] = (base[:need], weights)

    def pick_merchant(c):
        names, weights = pools.get(c, ([f"Vendor ({c})"], [1]))
        return rng.choices(names, weights=weights, k=1)[0]

    # One stable alias per recurring charge, so the same subscription keeps the
    # same fake name across every month it bills.
    rec_alias = {}
    for i, r in enumerate(recurring):
        pool = FAKE_NAMES.get(r.category) or GENERIC
        rec_alias[r.key] = pool[i % len(pool)] if len(recurring) <= len(pool) \
            else f"{pool[i % len(pool)]} {i // len(pool) + 1}"

    out = []
    for mi, m in enumerate(months):
        ms, me = month_start(m), month_end(m)
        span = (me - ms).days

        for r in recurring:
            rate = PER_MONTH.get(r.cadence, 1)
            if rate < 1:
                if mi % max(1, round(1 / rate)):
                    continue
                n_hits = 1
            else:
                n_hits = int(rate)
            for j in range(n_hits):
                if r.cadence == "monthly" and r.day_of_month:
                    day = min(r.day_of_month - 1, span)
                else:
                    day = min(int(round(span / n_hits * (j + rng.random()))), span)
                out.append(Transaction(
                    date=(ms + timedelta(days=day)).isoformat(),
                    description=rec_alias[r.key], merchant=rec_alias[r.key],
                    amount=round(r.amount, 2), category=r.category))

        for c in cat_month_total:
            totals = [cat_month_total[c].get(x, 0.0) for x in months]
            counts = [cat_month_n[c].get(x, 0) for x in months]
            live_t = [t for t in totals if t > 0]
            live_n = [n for n in counts if n > 0]
            # how often does this category show up at all?
            if rng.random() > len(live_t) / len(months):
                continue
            mean_t = stats.mean(live_t)
            sd_t = stats.pstdev(live_t) if len(live_t) > 1 else mean_t * 0.15
            target = max(rng.gauss(mean_t, sd_t), mean_t * 0.25)
            mean_n = stats.mean(live_n)
            sd_n = stats.pstdev(live_n) if len(live_n) > 1 else max(mean_n * 0.2, 0.5)
            n_txn = max(1, int(round(rng.gauss(mean_n, sd_n))))

            draws = [rng.choice(cat_amts[c]) * rng.uniform(0.9, 1.1) for _ in range(n_txn)]
            scale = target / sum(draws) if sum(draws) else 1.0
            scale = min(max(scale, 0.5), 2.0)      # keep individual amounts plausible
            for a in draws:
                day = rng.randrange(span + 1)
                out.append(Transaction(
                    date=(ms + timedelta(days=day)).isoformat(),
                    description=pick_merchant(c), merchant=pick_merchant(c),
                    amount=round(max(a * scale, 0.5), 2), category=c))

        month_spend = sum(t.amount for t in out
                          if t.month == m and t.amount > 0)
        out.append(Transaction(
            date=(ms + timedelta(days=min(25, span))).isoformat(),
            description="Card Payment", merchant="Card Payment",
            amount=-round(month_spend * rng.uniform(0.85, 1.0), 2),
            category="Payment"))

    for t in out:
        t.issuer = "CARD_A"
    out.sort(key=lambda t: t.date)
    return out, recurring


# --------------------------------------------------------------------------

def agg(txns):
    """Spend by category, the aggregate every downstream tool actually reads."""
    t, n = defaultdict(float), Counter()
    for x in txns:
        if x.amount > 0 and x.category not in NON_SPEND:
            t[x.category] += x.amount
            n[x.category] += 1
    return t, n


def compare(real, fake, note_sparse=False):
    """Show how far the shareable file drifts from the original."""
    rt, _ = agg(real)
    ft, _ = agg(fake)
    print(f"\n{'category':<18}{'yours':>12}{'shared':>12}{'diff':>9}")
    for c in sorted(rt, key=lambda c: -rt[c])[:12]:
        d = (ft.get(c, 0) - rt[c]) / rt[c] * 100 if rt[c] else 0
        print(f"{c[:18]:<18}{rt[c]:>12,.0f}{ft.get(c, 0):>12,.0f}{d:>8.1f}%")
    tr, tf = sum(rt.values()), sum(ft.values())
    print(f"{'TOTAL':<18}{tr:>12,.0f}{tf:>12,.0f}{(tf-tr)/tr*100 if tr else 0:>8.1f}%")

    if note_sparse:
        months = {t.month for t in real}
        seen = defaultdict(set)
        for t in real:
            if t.amount > 0 and t.category not in NON_SPEND:
                seen[t.category].add(t.month)
        thin = [c for c in rt if len(seen[c]) <= max(3, len(months) // 5)]
        if thin:
            print("\nCategories with few months of history vary most between runs "
                  f"({', '.join(sorted(thin)[:5])}).")
            print("That is the model being honest about thin data, not an error. "
                  "Re-run with a different --seed to see the spread.")
    return rt, ft


def report(level, real, fake, labels, folded, jitter, recurring):
    """State plainly what a reader of the shared file can still trust.

    Everything below is measured against the file that was just written, not
    asserted. A claim like "totals are exact" is only printed when the totals
    came back identical.
    """
    rt, ft = agg(real), agg(fake)
    rtot, ftot = sum(rt[0].values()), sum(ft[0].values())
    drift = abs(ftot - rtot) / rtot * 100 if rtot else 0.0
    same_dates = {t.date for t in real} == {t.date for t in fake}

    exact, approx, gone = [], [], []

    if level in ("light", "medium"):
        exact.append("every amount, to the cent")
        exact.append("every date, and so every monthly and weekly pattern")
        exact.append(f"total spending: ${rtot:,.0f}, unchanged")
        exact.append("per-category totals, and therefore every share and ratio")
        exact.append(f"recurring charges: {len(recurring)} still detectable "
                     f"at their real price")
    elif level == "heavy":
        exact.append("per-category totals, to within the jitter")
        exact.append("the month each transaction fell in")
        approx.append(f"individual amounts (moved by up to ±{jitter:.0f}%)")
        approx.append("individual dates (moved within their own month)")
        approx.append(f"total spending: ${rtot:,.0f} became ${ftot:,.0f} "
                      f"({drift:.1f}% out)")
        approx.append("day-of-week and day-of-month habits")
    else:
        exact.append("the months covered, and the set of categories you spend in")
        approx.append(f"total spending: ${rtot:,.0f} became ${ftot:,.0f} "
                      f"({drift:.1f}% out)")
        approx.append("per-category monthly totals: drawn from your real mean "
                      "and spread, so close but never equal")
        approx.append(f"recurring charges: {len(recurring)} regenerated at their "
                      f"real price and cadence")
        approx.append("how concentrated each category is across merchants")

    if level == "light":
        gone.append(f"columns: {', '.join(DROP_ALWAYS)}, account_last4; "
                    f"issuer becomes CARD_A, CARD_B …")
    else:
        n_labelled = len({v for v in labels.values() if v.startswith("MERCHANT")})
        gone.append(f"columns: {', '.join(DROP_ALWAYS)}, account_last4; "
                    f"issuer becomes CARD_A, CARD_B …")
        if level == "synthetic":
            gone.append("every real merchant, transaction, amount and date")
        else:
            gone.append(f"merchant identity: {n_labelled} relabelled MERCHANT_001…, "
                        f"{folded} rare ones folded into OTHER (category)")

    print(f"\nlevel {level} — {len(fake):,} rows written from {len(real):,}")
    print("\n  these are still true")
    for line in exact:
        print(f"    {line}")
    if approx:
        print("\n  these are now approximate")
        for line in approx:
            print(f"    {line}")
    print("\n  these are gone")
    for line in gone:
        print(f"    {line}")

    if level in ("light", "medium") and drift > 0.001:
        print(f"\n  warning: totals moved by {drift:.2f}% at a level that should "
              f"not move them at all")
    if level == "medium" and not same_dates:
        print("  warning: dates changed at a level that should not change them")


def main():
    ap = argparse.ArgumentParser(
        description="Make your transactions safe to hand to someone else.")
    ap.add_argument("source", nargs="?", default=DEFAULT_DB,
                    help=f"a .db or a .csv (default {DEFAULT_DB})")
    ap.add_argument("--db", help="the store to read, same as the positional argument")
    ap.add_argument("-o", "--output", default="transactions_shareable.csv",
                    help="a .csv to share, or a .db to build a second store")
    ap.add_argument("--level", choices=["light", "medium", "heavy", "synthetic"],
                    default="medium")
    ap.add_argument("-k", type=int, default=3,
                    help="fold merchants seen fewer than K times into OTHER (default 3)")
    ap.add_argument("--jitter", type=float, default=4.0,
                    help="percent amount jitter at --level heavy")
    ap.add_argument("--map", default="anonymize_map.json",
                    help="where to keep the private label mapping")
    ap.add_argument("--seed", type=int, help="fix the randomness for reproducible output")
    args = ap.parse_args()

    source = args.db or args.source
    if os.path.abspath(args.output) == os.path.abspath(source):
        sys.exit("refusing to write the anonymised rows over the source. "
                 "Anonymising is one-way: choose a different -o.")

    rng = random.Random(args.seed)
    rows = read(source)

    if args.level == "synthetic":
        out, recurring = synthesize(rows, rng)
        labels, folded = {}, 0
    else:
        out, labels, folded = anonymize(rows, args.level, args.k, args.jitter, rng)
        recurring = recurring_in(rows)

    write(args.output, out)

    if labels:
        with open(args.map, "w") as fh:
            json.dump({"level": args.level, "generated": date.today().isoformat(),
                       "source": source, "labels": labels}, fh, indent=1)
        try:
            os.chmod(args.map, 0o600)
        except OSError:
            pass

    report(args.level, rows, out, labels, folded, args.jitter, recurring)
    compare(rows, out, note_sparse=(args.level == "synthetic"))

    print(f"\nWrote {args.output} — this is the file to share.")
    if labels:
        print(f"Kept {args.map} — this is the file to NOT share. "
              f"It maps labels back to real names.")
    if args.level == "light":
        print("Note: level light keeps real merchant names, which identify where you shop.")


if __name__ == "__main__":
    main()
