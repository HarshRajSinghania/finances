#!/usr/bin/env python3
"""
analyze_finances.py — turn a cleaned transactions CSV into a spending dashboard.

Reads the CSV produced by parse_statements.py and writes:
    finance_dashboard.html   a single self-contained page (no internet needed)
    insights.md              the same findings as plain text

    python analyze_finances.py transactions.csv
    python analyze_finances.py transactions.csv --income 6200 -o dash.html
    python analyze_finances.py transactions.csv --exclude-merchant "TUITION|IRS"

Standard library only. Nothing is uploaded anywhere.
"""

import argparse
import csv
import html
import os
import re
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

# Category groups come from the shared taxonomy so every script agrees.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from categorizer import NON_SPEND, DISCRETIONARY, WASTE
except ImportError:
    NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}
    DISCRETIONARY = {"Dining", "Coffee", "Delivery", "Alcohol/Bars", "Shopping",
                     "Big Box", "Electronics", "Clothing", "Home", "Entertainment",
                     "Personal Care", "Travel", "Lodging", "Subscriptions",
                     "Software/Tools", "Gaming", "Gifts", "Pets", "Fitness"}
    WASTE = {"Fees/Interest"}

DELIVERY = re.compile(r"doordash|uber ?eats|grubhub|seamless|postmates|caviar", re.I)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

class Txn:
    __slots__ = ("d", "desc", "merchant", "amount", "category", "issuer",
                 "last4", "source", "key")

    def __init__(self, row):
        self.d = datetime.strptime(row["date"][:10], "%Y-%m-%d").date()
        self.desc = row.get("description", "")
        self.merchant = (row.get("merchant") or self.desc).strip()
        self.amount = float(row["amount"])
        self.category = row.get("category") or "Uncategorized"
        self.issuer = row.get("issuer", "")
        self.last4 = row.get("account_last4", "")
        self.source = row.get("source_file", "")
        self.key = merchant_key(self.merchant)

    @property
    def month(self):
        return self.d.strftime("%Y-%m")


def merchant_key(name):
    s = name.upper().strip()
    # Pseudonymised input (from anonymize.py or similar) is already a clean key.
    if re.fullmatch(r"[A-Z]+_\d+", s) or s.startswith("OTHER (") or s.startswith("CARD_"):
        return s
    s = re.sub(r"\bAMZN\b|\bAMAZON MKTPL\b", "AMAZON", s)
    s = re.sub(r"[^A-Z0-9_ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    stop = {"POS", "PURCHASE", "PAYMENT", "RECURRING", "INC", "LLC", "CO", "THE",
            "USA", "US", "COM", "STORE", "SQ", "TST", "PY"}
    toks = []
    for t in s.split():
        if t in stop or len(t) < 2:
            continue
        if t.isdigit() and len(t) >= 3:          # store or reference number
            continue
        if "_" not in t and len(t) >= 8 and sum(ch.isdigit() for ch in t) >= 2:
            continue                             # authorisation code
        toks.append(t)
    return " ".join(toks[:3]) or s


def load(path, exclude_merchant=None, exclude_source=None):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} is empty")
    missing = {"date", "amount"} - set(rows[0])
    if missing:
        sys.exit(f"{path} is missing required column(s): {', '.join(sorted(missing))}")

    txns = []
    for r in rows:
        try:
            t = Txn(r)
        except (ValueError, KeyError):
            continue
        if exclude_merchant and exclude_merchant.search(t.merchant + " " + t.desc):
            continue
        if exclude_source and exclude_source.search(t.source):
            continue
        txns.append(t)
    return txns


def load_statements(path):
    """Per-statement balances/limits/APR from parse_statements.py --statements."""
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    def num(r, k):
        try:
            return float(r[k]) if r.get(k) not in (None, "") else None
        except ValueError:
            return None

    out = []
    for r in rows:
        end = ""
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", r.get("statement_period", "") or "")
        if m:
            end = m.group(1)
        out.append({
            "card": f"{r.get('issuer','?')} ····{r.get('account_last4','????')}",
            "issuer": r.get("issuer", ""), "last4": r.get("account_last4", ""),
            "period": r.get("statement_period", ""), "end": end,
            "balance": num(r, "new_balance"), "limit": num(r, "credit_limit"),
            "util": num(r, "utilization_pct"), "apr": num(r, "purchase_apr"),
            "interest": num(r, "interest_charged") or 0.0,
            "fees": num(r, "fees_charged") or 0.0,
            "minimum": num(r, "minimum_payment"),
            "ytd_interest": num(r, "ytd_interest"),
        })
    out.sort(key=lambda s: s["end"])
    return out


def payoff_months(balance, apr, payment):
    """Months to clear a balance at a fixed monthly payment. None if it never clears."""
    if not balance or not apr or not payment or payment <= 0:
        return None
    r = apr / 100.0 / 12.0
    if payment <= balance * r + 0.01:
        return None
    import math
    return math.log(payment / (payment - balance * r)) / math.log(1 + r)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def month_range(a, b):
    out, cur = [], a.replace(day=1)
    while cur <= b:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=7)).replace(day=1)
    return out


def month_label(m):
    return datetime.strptime(m, "%Y-%m").strftime("%b %y")


def mad(values):
    if len(values) < 2:
        return 0.0
    m = stats.median(values)
    return stats.median([abs(v - m) for v in values])


def analyze(txns, income=None, statements=None):
    spend = [t for t in txns if t.amount > 0 and t.category not in NON_SPEND]
    if not spend:
        sys.exit("No spending rows found (everything was a payment or transfer).")

    first, last = min(t.d for t in spend), max(t.d for t in spend)
    months = month_range(first, last)

    # A month is "complete" only if the data actually covers most of it.
    complete = []
    for m in months:
        y, mo = int(m[:4]), int(m[5:])
        m_start = date(y, mo, 1)
        m_end = (m_start.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)
        covered_start = max(m_start, first)
        covered_end = min(m_end, last)
        days = (covered_end - covered_start).days + 1
        if days >= (m_end - m_start).days * 0.85:
            complete.append(m)
    if not complete:
        complete = months[:]

    by_month = defaultdict(float)
    by_cat = defaultdict(float)
    cat_month = defaultdict(lambda: defaultdict(float))
    cat_count = Counter()
    for t in spend:
        by_month[t.month] += t.amount
        by_cat[t.category] += t.amount
        cat_month[t.category][t.month] += t.amount
        cat_count[t.category] += 1

    n_complete = max(len(complete), 1)
    total = sum(t.amount for t in spend)
    total_complete = sum(by_month[m] for m in complete)
    monthly_avg = total_complete / n_complete

    # merchants
    merch = defaultdict(list)
    for t in spend:
        merch[t.key].append(t)
    merchants = []
    for k, ts in merch.items():
        amts = [t.amount for t in ts]
        merchants.append({
            "key": k,
            "name": Counter(t.merchant for t in ts).most_common(1)[0][0],
            "category": Counter(t.category for t in ts).most_common(1)[0][0],
            "total": sum(amts), "n": len(ts),
            "avg": sum(amts) / len(ts),
            "last": max(t.d for t in ts),
        })
    merchants.sort(key=lambda m: -m["total"])

    recurring = detect_recurring(merch)
    committed = sum(r["monthly"] for r in recurring)

    waste_txns = [t for t in spend if t.category in WASTE]
    waste_total = sum(t.amount for t in waste_txns)

    disc_total = sum(by_cat[c] for c in by_cat if c in DISCRETIONARY)

    return {
        "txns": txns, "spend": spend, "first": first, "last": last,
        "months": months, "complete": complete, "n_complete": n_complete,
        "by_month": by_month, "by_cat": by_cat, "cat_month": cat_month,
        "cat_count": cat_count, "total": total, "monthly_avg": monthly_avg,
        "merchants": merchants, "recurring": recurring, "committed": committed,
        "waste_txns": waste_txns, "waste_total": waste_total,
        "disc_total": disc_total, "income": income,
        "anomalies": find_anomalies(spend, merch),
        "patterns": find_patterns(spend, by_month, complete, n_complete),
        "accounts": sorted({(t.issuer, t.last4) for t in txns if t.issuer}),
        "statements": statements or [],
    }


def detect_recurring(merch):
    """A charge is recurring if it repeats on a steady cadence at a steady price."""
    out = []
    for k, ts in merch.items():
        if len(ts) < 3:
            continue
        ds = sorted(t.d for t in ts)
        span = (ds[-1] - ds[0]).days
        if span < 55:
            continue
        gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
        gaps = [g for g in gaps if g > 0]
        if len(gaps) < 2:
            continue
        med_gap = stats.median(gaps)
        if med_gap < 6 or med_gap > 400:
            continue
        if mad(gaps) / med_gap > 0.34:          # cadence too irregular
            continue
        amts = [t.amount for t in ts]
        med_amt = stats.median(amts)
        if med_amt <= 0 or mad(amts) / med_amt > 0.22:   # price too variable
            continue

        cadence = ("weekly" if med_gap <= 9 else "biweekly" if med_gap <= 18 else
                   "monthly" if med_gap <= 45 else "quarterly" if med_gap <= 115 else
                   "semiannual" if med_gap <= 200 else "annual")
        per_year = 365.0 / med_gap
        first_amt = stats.median(amts[:max(1, len(amts) // 3)])
        last_amt = stats.median(amts[-max(1, len(amts) // 3):])
        out.append({
            "name": Counter(t.merchant for t in ts).most_common(1)[0][0],
            "category": Counter(t.category for t in ts).most_common(1)[0][0],
            "cadence": cadence, "amount": med_amt, "n": len(ts),
            "annual": med_amt * per_year, "monthly": med_amt * per_year / 12.0,
            "first_seen": ds[0], "last_seen": ds[-1],
            "price_change": (last_amt - first_amt) if first_amt else 0.0,
            "price_pct": ((last_amt - first_amt) / first_amt * 100) if first_amt else 0.0,
        })
    out.sort(key=lambda r: -r["annual"])
    return out


def find_anomalies(spend, merch):
    dupes, outliers, big = [], [], []

    seen = defaultdict(list)
    for t in spend:
        seen[(t.key, round(t.amount, 2))].append(t)
    for (k, amt), ts in seen.items():
        if len(ts) < 2:
            continue
        ds = sorted(t.d for t in ts)
        for i in range(len(ds) - 1):
            if (ds[i + 1] - ds[i]).days <= 1:
                dupes.append({"name": ts[0].merchant, "amount": amt, "date": ds[i],
                              "n": sum(1 for d in ds if abs((d - ds[i]).days) <= 1)})
                break

    for k, ts in merch.items():
        if len(ts) < 5:
            continue
        amts = [t.amount for t in ts]
        med = stats.median(amts)
        spread = mad(amts) or med * 0.2
        for t in ts:
            if t.amount > max(med * 3, med + 5 * spread) and t.amount > 60:
                outliers.append({"name": t.merchant, "date": t.d, "amount": t.amount,
                                 "typical": med, "category": t.category})
    outliers.sort(key=lambda o: -o["amount"])

    big = sorted(spend, key=lambda t: -t.amount)[:12]
    return {"dupes": sorted(dupes, key=lambda d: -d["amount"])[:12],
            "outliers": outliers[:12], "big": big}


def find_patterns(spend, by_month, complete, n_complete):
    """Cross-cutting observations that a category table alone won't surface."""
    p = {}
    dining = [t for t in spend if t.category in ("Dining", "Delivery", "Coffee")]
    deliv = [t for t in dining if DELIVERY.search(t.merchant + " " + t.desc)]
    if dining:
        p["dining_total"] = sum(t.amount for t in dining)
        p["delivery_total"] = sum(t.amount for t in deliv)
        p["delivery_share"] = (p["delivery_total"] / p["dining_total"] * 100) if dining else 0
        p["delivery_n"] = len(deliv)
        p["delivery_avg"] = (p["delivery_total"] / len(deliv)) if deliv else 0
        p["dining_in_person_avg"] = ((p["dining_total"] - p["delivery_total"]) /
                                     max(len(dining) - len(deliv), 1))

    small = [t for t in spend if t.amount < 15]
    p["small_n"] = len(small)
    p["small_total"] = sum(t.amount for t in small)
    p["small_monthly"] = p["small_total"] / max(n_complete, 1)

    weekend = sum(t.amount for t in spend if t.d.weekday() >= 4)
    p["weekend_share"] = weekend / max(sum(t.amount for t in spend), 1) * 100

    vals = [by_month[m] for m in complete]
    if vals:
        p["median_month"] = stats.median(vals)
        peak = max(complete, key=lambda m: by_month[m])
        p["peak_month"], p["peak_amount"] = peak, by_month[peak]
        low = min(complete, key=lambda m: by_month[m])
        p["low_month"], p["low_amount"] = low, by_month[low]

    # trend: average of last third vs first third of complete months
    if len(complete) >= 6:
        k = max(2, len(complete) // 3)
        early = sum(by_month[m] for m in complete[:k]) / k
        late = sum(by_month[m] for m in complete[-k:]) / k
        p["trend_pct"] = (late - early) / early * 100 if early else 0
        p["trend_early"], p["trend_late"] = early, late
    return p


def savings_scenarios(a):
    """Arithmetic only: what a given cut frees up per year."""
    rows = []
    n = a["n_complete"]
    for cat, total in a["by_cat"].items():
        if cat not in DISCRETIONARY:
            continue
        monthly = total / n
        if monthly < 25:
            continue
        rows.append({"cat": cat, "monthly": monthly,
                     "cut15": monthly * 0.15 * 12, "cut30": monthly * 0.30 * 12,
                     "cut50": monthly * 0.50 * 12})
    rows.sort(key=lambda r: -r["cut30"])
    return rows


# --------------------------------------------------------------------------
# Tiny SVG helpers (no chart library, no CDN)
# --------------------------------------------------------------------------

def sparkline(values, w=104, h=22):
    if not values or max(values) == 0:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or hi or 1
    step = w / max(len(values) - 1, 1)
    pts = " ".join(f"{i*step:.1f},{h - 2 - (v - lo) / rng * (h - 5):.1f}"
                   for i, v in enumerate(values))
    last_x = (len(values) - 1) * step
    last_y = h - 2 - (values[-1] - lo) / rng * (h - 5)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'aria-hidden="true"><polyline points="{pts}"/>'
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2"/></svg>')


def column_chart(months, values, w=980, h=190):
    if not values:
        return ""
    hi = max(values) or 1
    n = len(values)
    gap = 6
    bw = max((w - gap * (n - 1)) / n, 4)
    avg = sum(values) / n
    ay = h - 26 - (avg / hi) * (h - 44)
    bars, labels = [], []
    for i, (m, v) in enumerate(zip(months, values)):
        x = i * (bw + gap)
        bh = (v / hi) * (h - 44)
        y = h - 26 - bh
        bars.append(f'<rect class="col" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                    f'height="{bh:.1f}"><title>{month_label(m)}: ${v:,.0f}</title></rect>')
        if n <= 18 or i % 2 == 0:
            labels.append(f'<text class="xlab" x="{x + bw/2:.1f}" y="{h - 10}" '
                          f'text-anchor="middle">{month_label(m)}</text>')
        labels.append(f'<text class="vlab" x="{x + bw/2:.1f}" y="{y - 5:.1f}" '
                      f'text-anchor="middle">{v/1000:.1f}k</text>')
    return (f'<svg class="cols" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="Monthly spending">'
            f'{"".join(bars)}'
            f'<line class="avgline" x1="0" y1="{ay:.1f}" x2="{w}" y2="{ay:.1f}"/>'
            f'<text class="avglab" x="{w-4}" y="{ay-6:.1f}" text-anchor="end">'
            f'monthly average ${avg:,.0f}</text>{"".join(labels)}</svg>')


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{
  --paper:#FBFBF7; --band:#E7EFE3; --ink:#14211B; --soft:#5C6D62;
  --rule:#C2D1BC; --rule-strong:#8FA88A; --red:#A3372A; --green:#2E6A4C;
  --amber:#9C7318;
  --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:#DCE3D8;color:var(--ink);font-family:var(--ui);
     font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.sheet{max-width:1180px;margin:26px auto;background:var(--paper);
       border:1px solid var(--rule-strong);
       box-shadow:0 1px 0 rgba(0,0,0,.06),0 12px 34px rgba(20,33,27,.10);
       padding:0 46px 44px;position:relative}
/* perforated feed edge — the continuous-form paper this is modelled on */
.sheet::before,.sheet::after{content:"";position:absolute;top:0;bottom:0;width:26px;
  background-image:radial-gradient(circle at 13px 13px,#DCE3D8 3.5px,transparent 3.6px);
  background-size:26px 26px;border-right:1px solid var(--rule);}
.sheet::before{left:0}
.sheet::after{right:0;border-right:0;border-left:1px solid var(--rule)}
@media(max-width:820px){.sheet{padding:0 16px 30px;margin:0}
  .sheet::before,.sheet::after{display:none}}

header.head{padding:30px 0 18px;border-bottom:2px solid var(--ink);margin-bottom:26px}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;
        color:var(--soft)}
h1{font-family:var(--display);font-size:38px;font-weight:400;letter-spacing:-.01em;
   margin:6px 0 10px;line-height:1.05}
.headmeta{display:flex;flex-wrap:wrap;gap:8px 30px;font-family:var(--mono);font-size:12px;
          color:var(--soft)}
.headmeta b{color:var(--ink);font-weight:600}

section{margin:38px 0 0;scroll-margin-top:20px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
         color:var(--red);border-bottom:1px solid var(--rule);padding-bottom:6px;
         display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.eyebrow span{color:var(--soft);letter-spacing:.04em;text-transform:none}
h2{font-family:var(--display);font-size:25px;font-weight:400;margin:14px 0 4px}
.lede{color:var(--soft);max-width:62ch;margin:0 0 16px}

/* statement-style summary with leader dots */
.summary{list-style:none;padding:0;margin:0}
.summary li{display:flex;align-items:baseline;gap:8px;padding:9px 0;
            border-bottom:1px dotted var(--rule)}
.summary .lbl{font-family:var(--ui);font-size:15px}
.summary .dots{flex:1;border-bottom:1px dotted var(--rule);transform:translateY(-4px)}
.summary .val{font-family:var(--mono);font-size:18px;font-variant-numeric:tabular-nums;
              font-weight:600;white-space:nowrap}
.summary .note{font-family:var(--mono);font-size:11px;color:var(--soft);white-space:nowrap;
               min-width:150px;text-align:right}
.summary li.lead .val{font-size:27px}
.val.bad{color:var(--red)} .val.good{color:var(--green)}

.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
   color:var(--soft);text-align:right;padding:8px 9px;border-bottom:1.5px solid var(--rule-strong);
   white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--ink)}
td{padding:7px 9px;text-align:right;border-bottom:1px solid rgba(194,209,188,.55);
   font-variant-numeric:tabular-nums}
td.num,td.money{font-family:var(--mono)}
tbody tr:nth-child(even){background:var(--band)}
tbody tr:hover{background:#DBE8D6}
.name{font-weight:500}
.sub{color:var(--soft);font-size:12px;font-family:var(--mono)}
tfoot td{border-top:1.5px solid var(--ink);border-bottom:0;font-family:var(--mono);
         font-weight:700;padding-top:10px}

.matrix td.cell{font-family:var(--mono);font-size:12.5px;position:relative;min-width:56px}
.matrix td.cell.partial{opacity:.45;font-style:italic}
.spark{overflow:visible}
.spark polyline{fill:none;stroke:var(--ink);stroke-width:1.4;
                stroke-linejoin:round;stroke-linecap:round}
.spark circle{fill:var(--red)}
.cols{width:100%;height:auto;display:block;margin:6px 0 4px}
.cols .col{fill:#6E8F76}
.cols .col:hover{fill:var(--ink)}
.cols .avgline{stroke:var(--red);stroke-width:1;stroke-dasharray:5 4}
.cols .avglab,.cols .xlab,.cols .vlab{font-family:var(--mono);fill:var(--soft)}
.cols .avglab{font-size:10px;fill:var(--red)}
.cols .xlab{font-size:9.5px}
.cols .vlab{font-size:9px;fill:#7C8F82}

.flags{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px;margin-top:6px}
.flag{border:1px solid var(--rule);border-left:3px solid var(--amber);padding:12px 14px;
      background:#fff}
.flag.red{border-left-color:var(--red)}
.flag h3{margin:0 0 5px;font-size:14px;font-family:var(--ui);font-weight:600}
.flag p{margin:0;font-size:13.5px;color:var(--soft)}
.flag .fig{font-family:var(--mono);font-weight:700;color:var(--ink)}

.bar{display:inline-block;height:7px;background:#8FA88A;vertical-align:middle}
.bar.red{background:var(--red)}
.pill{font-family:var(--mono);font-size:10.5px;padding:1px 6px;border:1px solid var(--rule-strong);
      color:var(--soft);white-space:nowrap}
.pill.up{color:var(--red);border-color:var(--red)}
.delta.up{color:var(--red)} .delta.down{color:var(--green)}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);
       font-size:12.5px;color:var(--soft)}
footer code{font-family:var(--mono);font-size:12px}
.toolbar{display:flex;gap:10px;align-items:center;margin:10px 0 4px}
button.tgl{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  background:transparent;border:1px solid var(--rule-strong);color:var(--soft);padding:5px 10px;
  cursor:pointer}
button.tgl:hover{color:var(--ink);border-color:var(--ink)}
button.tgl:focus-visible,th.sortable:focus-visible{outline:2px solid var(--red);outline-offset:2px}
tr.extra{display:none} table.show-all tr.extra{display:table-row}
@media(prefers-reduced-motion:no-preference){.cols .col{transition:fill .15s}}
@media print{body{background:#fff}.sheet{box-shadow:none;border:0;margin:0}
  .sheet::before,.sheet::after{display:none}.toolbar{display:none}
  tr.extra{display:table-row}section{break-inside:avoid}}
"""

JS = """
document.querySelectorAll('th.sortable').forEach(function(th){
  th.tabIndex=0;
  function run(){
    var tbl=th.closest('table'), tb=tbl.tBodies[0], i=[].indexOf.call(th.parentNode.children,th);
    var dir=th.dataset.dir==='asc'?-1:1; th.dataset.dir=dir===1?'asc':'desc';
    var rows=[].slice.call(tb.rows);
    rows.sort(function(a,b){
      var x=a.cells[i].dataset.v, y=b.cells[i].dataset.v;
      if(x!==undefined&&y!==undefined) return (parseFloat(x)-parseFloat(y))*dir;
      return a.cells[i].textContent.localeCompare(b.cells[i].textContent)*dir;
    });
    rows.forEach(function(r){tb.appendChild(r);});
  }
  th.addEventListener('click',run);
  th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();run();}});
});
document.querySelectorAll('button.tgl').forEach(function(b){
  b.addEventListener('click',function(){
    var t=document.getElementById(b.dataset.target);
    t.classList.toggle('show-all');
    b.textContent=t.classList.contains('show-all')?b.dataset.less:b.dataset.more;
  });
});
"""


def money(v, dp=0):
    return f"${v:,.{dp}f}"


def esc(s):
    return html.escape(str(s))


def cards_section(a):
    stmts = a["statements"]
    if not stmts:
        return ""
    latest = {}
    for s in stmts:
        latest[(s["issuer"], s["last4"])] = s          # sorted by date, last wins
    cards = sorted(latest.values(), key=lambda s: -(s["balance"] or 0))

    total_bal = sum(s["balance"] or 0 for s in cards)
    total_lim = sum(s["limit"] or 0 for s in cards)
    total_int = sum(s["interest"] + s["fees"] for s in stmts)

    rows = []
    for s in cards:
        util = s["util"]
        if util is None and s["balance"] and s["limit"]:
            util = s["balance"] / s["limit"] * 100
        ucls = "" if util is None else ("red" if util >= 50 else "" if util >= 30 else "")
        ubar = ("" if util is None else
                f'<span class="bar {"red" if util >= 30 else ""}" '
                f'style="width:{min(util, 100) * 0.9:.0f}px;margin-right:8px"></span>')
        apr_txt = f'{s["apr"]:.2f}%' if s["apr"] else "&mdash;"
        bal_txt = money(s["balance"]) if s["balance"] is not None else "&mdash;"
        lim_txt = money(s["limit"]) if s["limit"] else "&mdash;"
        min_txt = money(s["minimum"]) if s["minimum"] else "&mdash;"
        util_txt = f"{util:.0f}%" if util is not None else "&mdash;"
        int_cls = "bad" if (s["interest"] + s["fees"]) else ""
        rows.append(
            f'<tr><td class="name">{esc(s["card"])}'
            f'<div class="sub">{esc(s["period"] or "-")}</div></td>'
            f'<td class="money">{bal_txt}</td>'
            f'<td class="money">{lim_txt}</td>'
            f'<td class="money">{ubar}{util_txt}</td>'
            f'<td class="num">{apr_txt}</td>'
            f'<td class="money {int_cls}">{money(s["interest"] + s["fees"], 2)}</td>'
            f'<td class="money">{min_txt}</td></tr>')

    plans = []
    for s in cards:
        if not (s["balance"] and s["apr"] and s["balance"] > 50):
            continue
        base = s["minimum"] or max(s["balance"] * 0.02, 25)
        n0 = payoff_months(s["balance"], s["apr"], base)
        n1 = payoff_months(s["balance"], s["apr"], base + 100)
        if n0 is None:
            plans.append(f'<div class="flag red"><h3>{esc(s["card"])}</h3><p>At the minimum '
                         f'payment of <span class="fig">{money(base)}</span> this balance never '
                         f'clears — the interest at {s["apr"]:.2f}% outruns the payment.</p></div>')
            continue
        i0 = n0 * base - s["balance"]
        txt = (f'Paying <span class="fig">{money(base)}</span> a month clears '
               f'{money(s["balance"])} in <span class="fig">{n0:.0f} months</span> '
               f'and costs {money(i0)} in interest.')
        if n1:
            i1 = n1 * (base + 100) - s["balance"]
            txt += (f' Adding $100 a month: <span class="fig">{n1:.0f} months</span>, '
                    f'{money(i1)} interest — {money(i0 - i1)} saved.')
        plans.append(f'<div class="flag"><h3>{esc(s["card"])} at {s["apr"]:.2f}%</h3>'
                     f'<p>{txt}</p></div>')

    util_all = (total_bal / total_lim * 100) if total_lim else None
    lede = (f'Across {len(cards)} card{"s" if len(cards) != 1 else ""} you are carrying '
            f'<b>{money(total_bal)}</b>')
    if util_all is not None:
        lede += (f' against {money(total_lim)} of limit — <b>{util_all:.0f}% utilization</b>. '
                 f'Scoring models commonly treat the 30% line as the point where utilization '
                 f'starts to weigh against you.')
    lede += f' Interest and fees across these statements: <b>{money(total_int)}</b>.'

    return f"""<section>
  <div class="eyebrow">Schedule E — cards <span>from the statement summary pages</span></div>
  <h2>What the debt costs</h2>
  <p class="lede">{lede}</p>
  <div class="scroll"><table><thead><tr><th>Card</th><th>Balance</th><th>Limit</th>
    <th>Utilization</th><th>APR</th><th>Interest + fees</th><th>Minimum</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
    <tfoot><tr><td>{len(cards)} cards &middot; interest column totals all {len(stmts)} statements</td>
      <td>{money(total_bal)}</td>
      <td>{money(total_lim) if total_lim else "—"}</td>
      <td>{f"{util_all:.0f}%" if util_all is not None else "—"}</td><td></td>
      <td>{money(total_int, 2)}</td><td></td></tr></tfoot></table></div>
  {"<h2 style='margin-top:26px'>How long it takes to clear</h2><div class='flags'>"
   + "".join(plans) + "</div>" if plans else ""}
</section>"""


def render_html(a, title):
    p, n = a["patterns"], a["n_complete"]
    complete = a["complete"]
    partial = [m for m in a["months"] if m not in complete]
    yr = 12.0 / n

    # ---------- summary ----------
    inc = a["income"]
    disc_monthly = a["disc_total"] / n
    summary = [
        ("Average month", money(a["monthly_avg"]), f"over {n} full month{'s' if n>1 else ''}", "lead", ""),
        ("Committed each month", money(a["committed"]),
         f"{len(a['recurring'])} recurring charges", "", ""),
        ("Discretionary each month", money(disc_monthly),
         f"{disc_monthly / a['monthly_avg'] * 100:.0f}% of spending", "", ""),
        ("Interest and fees", money(a["waste_total"]),
         f"{money(a['waste_total'] * yr)}/yr at this rate", "",
         "bad" if a["waste_total"] > 0 else ""),
        ("Total spending", money(a["total"]),
         f"{a['first']:%b %Y} – {a['last']:%b %Y}", "", ""),
    ]
    if inc:
        rate = (inc - a["monthly_avg"]) / inc * 100
        summary.insert(1, ("Left over each month", money(inc - a["monthly_avg"]),
                           f"{rate:.0f}% of {money(inc)} income", "",
                           "good" if rate > 0 else "bad"))
    sum_html = "".join(
        f'<li class="{cls}"><span class="lbl">{esc(lbl)}</span><span class="dots"></span>'
        f'<span class="note">{esc(note)}</span><span class="val {vc}">{val}</span></li>'
        for lbl, val, note, cls, vc in summary)

    # ---------- monthly columns ----------
    cols = column_chart(a["months"], [a["by_month"][m] for m in a["months"]])

    # ---------- the sheet: category x month ----------
    cats = sorted(a["by_cat"], key=lambda c: -a["by_cat"][c])
    show_months = a["months"][-14:]
    peak_cell = max((a["cat_month"][c][m] for c in cats for m in show_months), default=1) or 1
    head = "".join(f'<th>{month_label(m)}{"*" if m in partial else ""}</th>' for m in show_months)
    body = []
    for i, c in enumerate(cats):
        series = [a["cat_month"][c][m] for m in show_months]
        cells = []
        for m, v in zip(show_months, series):
            shade = min(v / peak_cell, 1) ** 0.6
            bg = f'background:rgba(46,106,76,{shade*0.42:.3f})' if v else ""
            cells.append(f'<td class="cell{" partial" if m in partial else ""}" style="{bg}">'
                         f'{("%.0f" % v) if v else "·"}</td>')
        tot = a["by_cat"][c]
        avg = tot / n
        recent = [a["cat_month"][c][m] for m in complete[-3:]] or [0]
        prior = [a["cat_month"][c][m] for m in complete[:-3]] or [0]
        d = (sum(recent) / len(recent)) - (sum(prior) / max(len(prior), 1))
        dcls = "up" if d > avg * 0.12 else "down" if d < -avg * 0.12 else ""
        arrow = "▲" if dcls == "up" else "▼" if dcls == "down" else "–"
        body.append(
            f'<tr class="{"extra" if i >= 9 else ""}"><td class="name">{esc(c)}'
            f'<div class="sub">{a["cat_count"][c]} charges</div></td>'
            f'{"".join(cells)}<td class="money">{sparkline(series)}</td>'
            f'<td class="money">{money(avg)}</td>'
            f'<td class="money delta {dcls}">{arrow} {money(abs(d))}</td>'
            f'<td class="money"><b>{money(tot)}</b></td></tr>')
    totals_row = "".join(f'<td>{a["by_month"][m]/1000:.1f}k</td>' for m in show_months)

    # ---------- recurring ----------
    rec_rows = []
    for i, r in enumerate(a["recurring"]):
        up = r["price_pct"] > 4
        stale = (a["last"] - r["last_seen"]).days > 70
        tags = []
        if up:
            tags.append(f'<span class="pill up">+{r["price_pct"]:.0f}% price</span>')
        if stale:
            tags.append(f'<span class="pill">last seen {r["last_seen"]:%b %Y}</span>')
        rec_rows.append(
            f'<tr class="{"extra" if i >= 12 else ""}"><td class="name">{esc(r["name"])} '
            f'{" ".join(tags)}<div class="sub">{esc(r["category"])}</div></td>'
            f'<td class="num">{r["cadence"]}</td>'
            f'<td class="money" data-v="{r["amount"]}">{money(r["amount"], 2)}</td>'
            f'<td class="num" data-v="{r["n"]}">{r["n"]}</td>'
            f'<td class="money" data-v="{r["monthly"]}">{money(r["monthly"])}</td>'
            f'<td class="money" data-v="{r["annual"]}"><b>{money(r["annual"])}</b></td></tr>')
    rec_annual = sum(r["annual"] for r in a["recurring"])

    # ---------- merchants ----------
    top = a["merchants"][:30]
    mmax = top[0]["total"] if top else 1
    m_rows = "".join(
        f'<tr class="{"extra" if i >= 12 else ""}"><td class="name">{esc(m["name"])}'
        f'<div class="sub">{esc(m["category"])}</div></td>'
        f'<td class="num" data-v="{m["n"]}">{m["n"]}</td>'
        f'<td class="money" data-v="{m["avg"]}">{money(m["avg"], 2)}</td>'
        f'<td class="money" data-v="{m["total"]/n}">{money(m["total"]/n)}</td>'
        f'<td class="money" data-v="{m["total"]}"><b>{money(m["total"])}</b>'
        f'<span class="bar" style="width:{m["total"]/mmax*90:.0f}px;margin-left:8px"></span></td></tr>'
        for i, m in enumerate(top))

    # ---------- leaks ----------
    flags = []
    if a["waste_total"] > 0:
        by_kind = Counter()
        for t in a["waste_txns"]:
            by_kind[t.merchant[:28]] += t.amount
        detail = ", ".join(f"{esc(k.lower())} {money(v)}" for k, v in by_kind.most_common(3))
        flags.append(("red", "Interest and fees",
                      f'<span class="fig">{money(a["waste_total"])}</span> paid so far — '
                      f'{money(a["waste_total"] * yr)} a year at this rate. Biggest: {detail}. '
                      f'This is the one category where cutting costs you nothing.'))
    if p.get("delivery_total", 0) > 0:
        flags.append(("", "Delivery vs. eating out",
                      f'{money(p["delivery_total"])} of your {money(p["dining_total"])} dining spend '
                      f'went to delivery apps — <span class="fig">{p["delivery_share"]:.0f}%</span>. '
                      f'Average delivery order {money(p["delivery_avg"], 2)} vs '
                      f'{money(p["dining_in_person_avg"], 2)} in person.'))
    if p.get("small_n"):
        flags.append(("", "Charges under $15",
                      f'<span class="fig">{p["small_n"]}</span> of them, '
                      f'{money(p["small_total"])} total — about {money(p["small_monthly"])} a month. '
                      f'Individually invisible, collectively a line item.'))
    if p.get("trend_pct") is not None and abs(p.get("trend_pct", 0)) > 8:
        up = p["trend_pct"] > 0
        flags.append(("red" if up else "", "Direction of travel",
                      f'Recent months average {money(p["trend_late"])} vs {money(p["trend_early"])} '
                      f'early on — <span class="fig">{"up" if up else "down"} '
                      f'{abs(p["trend_pct"]):.0f}%</span>.'))
    if p.get("peak_month"):
        flags.append(("", "Best and worst months",
                      f'{month_label(p["peak_month"])} cost {money(p["peak_amount"])}; '
                      f'{month_label(p["low_month"])} cost {money(p["low_amount"])}. '
                      f'The gap is <span class="fig">{money(p["peak_amount"]-p["low_amount"])}</span> '
                      f'— that spread is what a normal month could look like.'))
    if a["anomalies"]["dupes"]:
        d0 = a["anomalies"]["dupes"][0]
        flags.append(("red", "Possible double charges",
                      f'{len(a["anomalies"]["dupes"])} case(s) of the same merchant and amount '
                      f'within a day, e.g. {esc(d0["name"])} '
                      f'<span class="fig">{money(d0["amount"], 2)}</span> on {d0["date"]:%d %b %Y}. '
                      f'Worth checking against the statement.'))
    flags_html = "".join(f'<div class="flag {c}"><h3>{esc(t)}</h3><p>{b}</p></div>'
                         for c, t, b in flags)

    out_rows = "".join(
        f'<tr><td class="name">{esc(o["name"])}<div class="sub">{esc(o["category"])}</div></td>'
        f'<td class="num">{o["date"]:%d %b %Y}</td>'
        f'<td class="money">{money(o["typical"], 2)}</td>'
        f'<td class="money"><b>{money(o["amount"], 2)}</b></td>'
        f'<td class="num">{o["amount"]/o["typical"]:.0f}×</td></tr>'
        for o in a["anomalies"]["outliers"][:8])

    # ---------- scenarios ----------
    sc = savings_scenarios(a)
    sc_rows = "".join(
        f'<tr><td class="name">{esc(s["cat"])}</td>'
        f'<td class="money" data-v="{s["monthly"]}">{money(s["monthly"])}</td>'
        f'<td class="money" data-v="{s["cut15"]}">{money(s["cut15"])}</td>'
        f'<td class="money" data-v="{s["cut30"]}"><b>{money(s["cut30"])}</b></td>'
        f'<td class="money" data-v="{s["cut50"]}">{money(s["cut50"])}</td></tr>'
        for s in sc)
    sc_total30 = sum(s["cut30"] for s in sc)

    accounts = ", ".join(f"{i} ····{l}" for i, l in a["accounts"]) or "—"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<div class="sheet">
<header class="head">
  <div class="kicker">Statement of personal spending</div>
  <h1>{esc(title)}</h1>
  <div class="headmeta">
    <span>Period <b>{a['first']:%d %b %Y} – {a['last']:%d %b %Y}</b></span>
    <span>Accounts <b>{esc(accounts)}</b></span>
    <span>Transactions <b>{len(a['spend']):,}</b></span>
    <span>Prepared <b>{date.today():%d %b %Y}</b></span>
  </div>
</header>

<section>
  <div class="eyebrow">Summary of account <span>transfers and card payments excluded</span></div>
  <ul class="summary">{sum_html}</ul>
</section>

<section>
  <div class="eyebrow">Month by month <span>bars are total spending; dashed line is your average</span></div>
  <h2>Every month, side by side</h2>
  {cols}
</section>

<section>
  <div class="eyebrow">Schedule A — where it goes <span>shaded by intensity; * partial month</span></div>
  <h2>The sheet</h2>
  <p class="lede">Each row is a category, each column a month. Darker means heavier. The
  trend column compares your last three months against everything before them.</p>
  <div class="toolbar">
    <button class="tgl" data-target="matrix" data-more="Show all categories"
            data-less="Show top 9 only">Show all categories</button>
  </div>
  <div class="scroll"><table id="matrix" class="matrix"><thead><tr><th>Category</th>{head}
    <th>Trend</th><th>Per month</th><th>Change</th><th>Total</th></tr></thead>
    <tbody>{"".join(body)}</tbody>
    <tfoot><tr><td>All spending</td>{totals_row}<td></td>
      <td>{money(a['monthly_avg'])}</td><td></td><td>{money(a['total'])}</td></tr></tfoot>
  </table></div>
</section>

<section>
  <div class="eyebrow">Schedule B — standing charges <span>detected by cadence and price stability</span></div>
  <h2>What renews without asking</h2>
  <p class="lede">Charges that repeat on a steady schedule at a steady price. This is the
  part of your spending that continues whether you think about it or not —
  <b>{money(rec_annual)} a year</b>, {money(a['committed'])} a month.</p>
  <div class="scroll"><table id="rec"><thead><tr><th class="sortable">Merchant</th><th>Cadence</th>
    <th class="sortable">Each time</th><th class="sortable">Seen</th>
    <th class="sortable">Per month</th><th class="sortable">Per year</th></tr></thead>
    <tbody>{"".join(rec_rows)}</tbody>
    <tfoot><tr><td>{len(a['recurring'])} standing charges</td><td></td><td></td><td></td>
      <td>{money(a['committed'])}</td><td>{money(rec_annual)}</td></tr></tfoot></table></div>
  <div class="toolbar">
    <button class="tgl" data-target="rec" data-more="Show all" data-less="Show top 12">Show all</button>
  </div>
</section>

<section>
  <div class="eyebrow">Schedule C — merchants <span>sorted by total; click a heading to re-sort</span></div>
  <h2>Who you actually pay</h2>
  <p class="lede">Visits and average ticket together tell you how to cut: a big total from
  many small visits is a habit, a big total from one visit is a decision.</p>
  <div class="scroll"><table id="merch"><thead><tr><th class="sortable">Merchant</th>
    <th class="sortable">Visits</th><th class="sortable">Avg ticket</th>
    <th class="sortable">Per month</th><th class="sortable">Total</th></tr></thead>
    <tbody>{m_rows}</tbody></table></div>
  <div class="toolbar">
    <button class="tgl" data-target="merch" data-more="Show all 30" data-less="Show top 12">Show all 30</button>
  </div>
</section>

<section>
  <div class="eyebrow">Findings <span>things the totals alone won't tell you</span></div>
  <h2>Worth a second look</h2>
  <div class="flags">{flags_html}</div>
  {"<h2 style='margin-top:26px'>Charges well above that merchant's normal</h2>"
    "<div class='scroll'><table><thead><tr><th>Merchant</th><th>Date</th>"
    "<th>Usually</th><th>This time</th>"
    "<th>Ratio</th></tr></thead><tbody>" + out_rows + "</tbody></table></div>" if out_rows else ""}
</section>

{cards_section(a)}

<section>
  <div class="eyebrow">Schedule D — what a cut is worth <span>arithmetic, not advice</span></div>
  <h2>If you changed one thing</h2>
  <p class="lede">Discretionary categories only, at your current monthly rate. Trimming
  every line below by 30% would free <b>{money(sc_total30)}</b> a year — but the top row
  alone usually does most of the work.</p>
  <div class="scroll"><table id="sc"><thead><tr><th class="sortable">Category</th>
    <th class="sortable">Per month now</th><th class="sortable">Cut 15%</th>
    <th class="sortable">Cut 30%</th><th class="sortable">Cut 50%</th></tr></thead>
    <tbody>{sc_rows}</tbody></table></div>
</section>

<footer>
  <p><b>How these numbers were built.</b> Spending excludes card payments, transfers and
  refunds, so moving money between your own accounts is never counted as spending.
  Averages use only the {n} complete month{'s' if n != 1 else ''} in the data; partial months
  are marked <code>*</code> in the sheet and left out of every average.
  Recurring charges are inferred from timing and price stability, so a genuinely regular
  habit can look like a subscription — check the list before acting on it.</p>
  <p>Generated locally by <code>analyze_finances.py</code>. Nothing left this machine.
  Figures are arithmetic on your own statements, not financial advice.</p>
</footer>
</div><script>{JS}</script></body></html>"""


def render_markdown(a, title):
    p, n = a["patterns"], a["n_complete"]
    yr = 12.0 / n
    L = [f"# {title}", "",
         f"Period: {a['first']:%d %b %Y} – {a['last']:%d %b %Y} "
         f"({n} complete months, {len(a['spend']):,} spending transactions)", "",
         "## Summary", "",
         f"- Average month: {money(a['monthly_avg'])}",
         f"- Committed (recurring) per month: {money(a['committed'])}",
         f"- Discretionary per month: {money(a['disc_total']/n)}",
         f"- Interest and fees: {money(a['waste_total'])} "
         f"({money(a['waste_total']*yr)}/yr at this rate)",
         f"- Total spending: {money(a['total'])}", ""]
    if a["income"]:
        L.insert(6, f"- Left over each month: {money(a['income'] - a['monthly_avg'])}")

    L += ["## Categories", "", "| Category | Total | Per month | Share | Charges |",
          "|---|---:|---:|---:|---:|"]
    for c in sorted(a["by_cat"], key=lambda c: -a["by_cat"][c]):
        t = a["by_cat"][c]
        L.append(f"| {c} | {money(t)} | {money(t/n)} | {t/a['total']*100:.1f}% "
                 f"| {a['cat_count'][c]} |")

    L += ["", "## Standing charges", ""]
    for r in a["recurring"][:25]:
        flag = f"  (price up {r['price_pct']:.0f}%)" if r["price_pct"] > 4 else ""
        L.append(f"- {r['name']} — {money(r['amount'],2)} {r['cadence']}, "
                 f"{money(r['annual'])}/yr{flag}")
    L += ["", f"Total standing charges: {money(sum(r['annual'] for r in a['recurring']))}/yr", ""]

    L += ["## Top merchants", "", "| Merchant | Visits | Avg | Total |", "|---|---:|---:|---:|"]
    for m in a["merchants"][:20]:
        L.append(f"| {m['name']} | {m['n']} | {money(m['avg'],2)} | {money(m['total'])} |")

    L += ["", "## Findings", ""]
    if p.get("delivery_total"):
        L.append(f"- Delivery apps: {money(p['delivery_total'])} of {money(p['dining_total'])} "
                 f"dining ({p['delivery_share']:.0f}%), avg order {money(p['delivery_avg'],2)}")
    L.append(f"- Charges under $15: {p['small_n']} totalling {money(p['small_total'])} "
             f"({money(p['small_monthly'])}/month)")
    if p.get("trend_pct") is not None:
        L.append(f"- Trend: recent months {money(p.get('trend_late',0))} vs "
                 f"early {money(p.get('trend_early',0))} ({p.get('trend_pct',0):+.0f}%)")
    if p.get("peak_month"):
        L.append(f"- Most expensive month {month_label(p['peak_month'])} "
                 f"{money(p['peak_amount'])}; cheapest {month_label(p['low_month'])} "
                 f"{money(p['low_amount'])}")
    for d in a["anomalies"]["dupes"][:5]:
        L.append(f"- Possible double charge: {d['name']} {money(d['amount'],2)} on {d['date']}")
    for o in a["anomalies"]["outliers"][:5]:
        L.append(f"- Unusual for merchant: {o['name']} {money(o['amount'],2)} on {o['date']} "
                 f"(usually {money(o['typical'],2)})")

    L += ["", "## What a cut is worth (per year)", "",
          "| Category | Per month | -15% | -30% | -50% |", "|---|---:|---:|---:|---:|"]
    for s in savings_scenarios(a):
        L.append(f"| {s['cat']} | {money(s['monthly'])} | {money(s['cut15'])} "
                 f"| {money(s['cut30'])} | {money(s['cut50'])} |")
    L += ["", "_Averages use complete months only. Card payments, transfers and refunds are "
          "excluded from spending. Arithmetic on your own statements, not financial advice._"]
    return "\n".join(L)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Analyze a cleaned statement CSV.")
    ap.add_argument("csv", help="transactions.csv from parse_statements.py")
    ap.add_argument("-o", "--output", default="finance_dashboard.html")
    ap.add_argument("--markdown", default="insights.md")
    ap.add_argument("--title", default="Where the money went")
    ap.add_argument("--statements", help="statements.csv from parse_statements.py, "
                                        "adds balances, APR, utilization and payoff math")
    ap.add_argument("--income", type=float, help="monthly take-home pay, to show what's left over")
    ap.add_argument("--exclude-merchant", help="regex of merchants to drop entirely")
    ap.add_argument("--exclude-source", help="regex of source_file values to drop "
                                             "(e.g. a bank account that pays your cards)")
    args = ap.parse_args()

    for target in (args.output, args.markdown):
        d = os.path.dirname(os.path.abspath(target))
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                hint = (f"\n  '{target}' starts with a slash, so it points at the top of the "
                        f"filesystem. Drop it: -o {target.lstrip('/')}") if target.startswith("/") else ""
                sys.exit(f"Can't write to {target}: {e}{hint}")

    txns = load(args.csv,
                re.compile(args.exclude_merchant, re.I) if args.exclude_merchant else None,
                re.compile(args.exclude_source, re.I) if args.exclude_source else None)
    a = analyze(txns, args.income, load_statements(args.statements))

    with open(args.output, "w") as fh:
        fh.write(render_html(a, args.title))
    with open(args.markdown, "w") as fh:
        fh.write(render_markdown(a, args.title))

    print(f"{len(a['spend']):,} spending transactions over {a['n_complete']} complete months")
    print(f"  average month     {money(a['monthly_avg'])}")
    print(f"  committed/month   {money(a['committed'])} across {len(a['recurring'])} standing charges")
    print(f"  interest and fees {money(a['waste_total'])}")
    print(f"\nWrote {args.output} and {args.markdown}")
    print(f"Open it with:  open {args.output}   (or double-click)")


if __name__ == "__main__":
    main()