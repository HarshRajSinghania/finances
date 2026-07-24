#!/usr/bin/env python3
"""
anonymize.py — make a transactions CSV safe to share.

    python anonymize.py transactions.csv                       # default: pseudonymise
    python anonymize.py transactions.csv --level light
    python anonymize.py transactions.csv --level heavy
    python anonymize.py transactions.csv --level synthetic     # no real rows survive

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
              distributions, and detected recurring charges. No row is a real
              transaction. Shapes and magnitudes are preserved; specifics are not.

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
import re
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

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

NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}


def read(path):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} is empty")
    return rows


def parse_d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def month_of(d):
    return d.strftime("%Y-%m")


def month_start(m):
    return datetime.strptime(m + "-01", "%Y-%m-%d").date()


def month_end(m):
    s = month_start(m)
    return (s.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)


# --------------------------------------------------------------------------

def build_labels(rows, k):
    """Stable pseudonyms, ranked by total spend so the biggest is MERCHANT_001."""
    tot, cnt, cat = defaultdict(float), Counter(), {}
    for r in rows:
        m = (r.get("merchant") or "").strip() or "(blank)"
        try:
            amt = float(r["amount"])
        except (ValueError, KeyError):
            amt = 0.0
        tot[m] += abs(amt)
        cnt[m] += 1
        cat.setdefault(m, r.get("category", "Uncategorized"))

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


def anonymize(rows, level, k, jitter, rng):
    labels, folded = build_labels(rows, k) if level in ("medium", "heavy") else ({}, 0)

    cards = {}
    out = []
    for r in rows:
        d = parse_d(r["date"])
        amt = float(r["amount"])
        merchant = (r.get("merchant") or "").strip()
        cat = r.get("category", "Uncategorized")

        if level == "heavy":
            ms, me = month_start(month_of(d)), month_end(month_of(d))
            d = ms + timedelta(days=rng.randrange((me - ms).days + 1))
            amt = round(amt * (1 + rng.uniform(-jitter, jitter) / 100.0), 2)

        key = (r.get("issuer", ""), r.get("account_last4", ""))
        if key not in cards:
            cards[key] = f"CARD_{chr(65 + len(cards))}"

        out.append({
            "date": d.isoformat(),
            "post_date": "",
            "description": labels.get(merchant, merchant) if labels else merchant,
            "merchant": labels.get(merchant, merchant) if labels else merchant,
            "amount": round(amt, 2),
            "direction": "out" if amt > 0 else "in",
            "category": cat,
            "issuer": cards[key],
            "account_last4": "",
            "statement_period": "",
            "source_file": "",
        })
    out.sort(key=lambda r: r["date"])
    return out, labels, folded


def _detect_recurring(by_merch):
    """Merchants that bill on a roughly monthly cadence at a stable price."""
    out = {}
    for m, items in by_merch.items():
        if len(items) < 3:
            continue
        ds = sorted(i[0] for i in items)
        gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        med_gap = stats.median(gaps)
        amts = [i[1] for i in items]
        med_amt = stats.median(amts)
        spread = stats.median([abs(a - med_amt) for a in amts])
        if 20 <= med_gap <= 45 and med_amt > 0 and spread <= med_amt * 0.2:
            out[m] = (items[0][2], med_amt)     # category, typical amount
    return out


def synthesize(rows, rng):
    """Build a new dataset from the shape of the old one. No real rows survive.

    Modelled per category: how often the category appears at all, how many
    transactions it has in a month, what those months total, the distribution of
    individual amounts, and how concentrated spending is across merchants.
    """
    real = []
    for r in rows:
        try:
            real.append((parse_d(r["date"]), float(r["amount"]),
                         r.get("category", "Uncategorized"),
                         (r.get("merchant") or "").strip()))
        except (ValueError, KeyError):
            continue
    if not real:
        sys.exit("nothing usable to model")

    months = sorted({month_of(d) for d, *_ in real})
    spend = [x for x in real if x[1] > 0 and x[2] not in NON_SPEND]
    if not spend:
        sys.exit("no spending rows to model")

    by_merch = defaultdict(list)
    for d, a, c, m in spend:
        by_merch[m].append((d, a, c))
    recurring = _detect_recurring(by_merch)

    # Everything not on a subscription cadence is modelled statistically.
    variable = [x for x in spend if x[3] not in recurring]

    cat_month_total = defaultdict(lambda: defaultdict(float))
    cat_month_n = defaultdict(lambda: defaultdict(int))
    cat_amts = defaultdict(list)
    cat_merch_counts = defaultdict(Counter)
    for d, a, c, m in variable:
        cat_month_total[c][month_of(d)] += a
        cat_month_n[c][month_of(d)] += 1
        cat_amts[c].append(a)
        cat_merch_counts[c][m] += 1

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

    out = []
    for m in months:
        ms, me = month_start(m), month_end(m)
        span = (me - ms).days

        for merch, (c, amt) in recurring.items():
            out.append({"date": (ms + timedelta(days=rng.randrange(min(28, span + 1)))).isoformat(),
                        "merchant": pick_merchant(c) if c in pools else
                                    (FAKE_NAMES.get(c) or GENERIC)[0],
                        "amount": round(amt, 2), "category": c})

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
                out.append({"date": (ms + timedelta(days=day)).isoformat(),
                            "merchant": pick_merchant(c),
                            "amount": round(max(a * scale, 0.5), 2), "category": c})

        month_spend = sum(o["amount"] for o in out if o["date"][:7] == m and o["amount"] > 0)
        out.append({"date": (ms + timedelta(days=min(25, span))).isoformat(),
                    "merchant": "Card Payment", "category": "Payment",
                    "amount": -round(month_spend * rng.uniform(0.85, 1.0), 2)})

    rows_out = [{"date": o["date"], "post_date": "", "description": o["merchant"],
                 "merchant": o["merchant"], "amount": o["amount"],
                 "direction": "out" if o["amount"] > 0 else "in",
                 "category": o["category"], "issuer": "CARD_A", "account_last4": "",
                 "statement_period": "", "source_file": ""} for o in out]
    rows_out.sort(key=lambda r: r["date"])
    return rows_out


# --------------------------------------------------------------------------

def compare(real, fake_rows, note_sparse=False):
    """Show how far the shareable file drifts from the original."""
    def agg(rs):
        t, n = defaultdict(float), Counter()
        for r in rs:
            a = float(r["amount"])
            if a > 0 and r.get("category") not in NON_SPEND:
                t[r.get("category", "Uncategorized")] += a
                n[r.get("category", "Uncategorized")] += 1
        return t, n
    rt, rn = agg(real)
    ft, fn = agg(fake_rows)
    print(f"\n{'category':<18}{'yours':>12}{'shared':>12}{'diff':>9}")
    for c in sorted(rt, key=lambda c: -rt[c])[:12]:
        d = (ft.get(c, 0) - rt[c]) / rt[c] * 100 if rt[c] else 0
        print(f"{c[:18]:<18}{rt[c]:>12,.0f}{ft.get(c, 0):>12,.0f}{d:>8.1f}%")
    tr, tf = sum(rt.values()), sum(ft.values())
    print(f"{'TOTAL':<18}{tr:>12,.0f}{tf:>12,.0f}{(tf-tr)/tr*100 if tr else 0:>8.1f}%")
    if note_sparse:
        months = {r["date"][:7] for r in real}
        seen = defaultdict(set)
        for r in real:
            if float(r["amount"]) > 0 and r.get("category") not in NON_SPEND:
                seen[r.get("category", "Uncategorized")].add(r["date"][:7])
        thin = [c for c in rt if len(seen[c]) <= max(3, len(months) // 5)]
        if thin:
            print("\nCategories with few months of history vary most between runs "
                  f"({', '.join(sorted(thin)[:5])}).")
            print("That is the model being honest about thin data, not an error. "
                  "Re-run with a different --seed to see the spread.")


def main():
    ap = argparse.ArgumentParser(description="Make a transactions CSV safe to share.")
    ap.add_argument("csv")
    ap.add_argument("-o", "--output", default="transactions_shareable.csv")
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

    rng = random.Random(args.seed)
    rows = read(args.csv)

    if args.level == "synthetic":
        out = synthesize(rows, rng)
        labels, folded = {}, 0
    else:
        out, labels, folded = anonymize(rows, args.level, args.k, args.jitter, rng)

    with open(args.output, "w", newline="") as fh:
        cols = ["date", "post_date", "description", "merchant", "amount", "direction",
                "category", "issuer", "account_last4", "statement_period", "source_file"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    if labels:
        with open(args.map, "w") as fh:
            json.dump({"level": args.level, "generated": date.today().isoformat(),
                       "labels": labels}, fh, indent=1)
        try:
            os.chmod(args.map, 0o600)
        except OSError:
            pass

    print(f"level: {args.level}")
    print(f"removed columns: {', '.join(DROP_ALWAYS)}, account numbers, issuer names")
    if args.level in ("medium", "heavy"):
        print(f"merchants relabelled: {len({v for v in labels.values() if v.startswith('MERCHANT')})}"
              f", folded into OTHER: {folded}")
    if args.level == "heavy":
        print(f"dates jittered within month, amounts jittered +/-{args.jitter:.0f}%")
    if args.level == "synthetic":
        print("every row is generated; no real transaction survives")

    compare(rows, out, note_sparse=(args.level == "synthetic"))

    print(f"\nWrote {args.output} — this is the file to share.")
    if labels:
        print(f"Kept {args.map} — this is the file to NOT share. It maps labels back to real names.")
    if args.level == "light":
        print("Note: level light keeps real merchant names, which identify where you shop.")


if __name__ == "__main__":
    main()