#!/usr/bin/env python3
"""
Your own inflation rate, measured from your own receipts.

Every budgeting tool that talks to a bank API throws away the one thing that
would answer the question people actually ask — "is it me, or is everything
getting more expensive?" — because those APIs hand back a category and a total
and forget the merchant. This toolchain keeps the merchant and the date on every
line for as long as the statements go back, which is enough to watch what one
particular shop charges you over time. That is the whole idea here.

Four things come out of it:

    per-merchant drift   what each place you go back to charges you now versus
                         what it charged you at the start
    a personal CPI       those drifts weighted by *your* basket, not the
                         national one, and set against CPI-U for the same period
    basket vs behaviour  how much of a rising bill is prices and how much is
                         you buying more, split as far as statement data allows
                         and no further
    a benchmark          your annualised spend against the BLS Consumer
                         Expenditure Survey for your income bracket

The honest part is the third one, and it deserves saying up front: a credit card
statement records a total, never a line item. Nobody can tell from "GROCERY 41.20"
whether milk went up or you bought a second bag of coffee. So "price" here means
cost per visit, which is a proxy and is labelled as one everywhere it appears.

The other honest part is the thresholds. A merchant seen twice is not a trend.
Nothing is reported below MIN_VISITS visits over MIN_SPAN_DAYS days, every
figure carries its sample size, and where the sample is too small to ever reach
significance the report says so rather than printing a shrug as a finding.

Usage:

    finances cpi --db finances.db              full report
    finances cpi --db finances.db --income 72000   adds the national benchmark
    finances cpi --db finances.db --merchants  per-merchant price table only
    finances cpi -o cpi.html                   self-contained page

National figures come from reference_data.py, which is frozen and cited. Nothing
here touches the network.
"""

from __future__ import annotations

import argparse
import html
import math
import os
import statistics as stats
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations


from finances import reference_data as ref                                     # noqa: E402
from finances.database import DEFAULT_DB                                  # noqa: E402
from finances.engine import Engine                                        # noqa: E402
from finances.models import Transaction                                   # noqa: E402
from finances.utils import fmt_money as money, merchant_key               # noqa: E402


# --------------------------------------------------------------------------
# Minimum evidence
# --------------------------------------------------------------------------
#
# These are the whole argument for trusting anything below, so they are constants
# at the top of the file rather than magic numbers buried in a function.
#
# MIN_VISITS      Six charges is the fewest that can carry a three-against-three
#                 comparison with a window on each side. Note the consequence,
#                 which the report states out loud: with three against three
#                 there are only twenty ways to shuffle the labels, so the
#                 smallest two-sided p-value reachable is 0.10. At n=6 a real
#                 price rise *cannot* be called significant. That is a property
#                 of the sample, not evidence of no change.
# MIN_SPAN_DAYS   Four months. Shorter than that and a seasonal swing or a single
#                 promotion looks like a trend.
# MIN_WINDOW      Each side of the comparison needs three charges of its own.
# MIN_GAP_DAYS    Two months between the midpoints of the two windows before any
#                 change is annualised. Annualising a fortnight turns noise into
#                 a triple-digit inflation rate.

MIN_VISITS = 6
MIN_SPAN_DAYS = 120
MIN_WINDOW = 3
MIN_GAP_DAYS = 60

# A personal CPI built on one category is not an index, it is that category.
MIN_CPI_CATEGORIES = 3
MIN_CPI_COVERAGE = 0.40          # share of spend the measured categories must cover
MIN_CATEGORY_MERCHANTS = 1       # qualifying merchants needed inside a category

# The basket/behaviour split needs enough visits on both sides of the midpoint
# for a visit count to mean anything.
MIN_SPLIT_TOTAL = 8
MIN_SPLIT_HALF = 3

# Anything past this is almost certainly a data problem, not inflation.
ABSURD_ANNUAL_PCT = 200.0

# Categories that have no price to move. CPI-U measures the cost of goods and
# services; it deliberately excludes interest, taxes and anything that is a
# transfer rather than a purchase. A credit card's interest charge rising is a
# fact about a balance, not about prices, and letting it into a personal CPI
# would make carrying more debt look like inflation. Uncategorized is out for
# the plainer reason that nobody knows what it is.
NON_PRICE = frozenset({"Uncategorized", "Fees/Interest", "Taxes", "Debt Payment"})

SIGNIFICANT = 0.05
SUGGESTIVE = 0.20

PERMUTATIONS = 20000             # cap on exact enumeration before sampling
SEED = 20260728                  # fixed so two runs on one database agree


# --------------------------------------------------------------------------
# Small statistics, stdlib only
# --------------------------------------------------------------------------

def _mad_ratio(values: list[float]) -> float:
    """Median absolute deviation over the median. 0 means perfectly steady."""
    med = stats.median(values)
    if not med:
        return float("inf")
    return stats.median([abs(v - med) for v in values]) / abs(med)


def _midranks(values: list[float]) -> list[float]:
    """Ranks 1..n, with tied values sharing the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def permutation_p(early: list[float], late: list[float]) -> tuple[float, float]:
    """Two-sided p that the two windows are drawn from the same prices.

    A permutation test rather than a t-test, because six charges from one shop
    are not normal, are not independent of the season, and a t-test on them
    would be arithmetic dressed as inference. Under the null the windows are
    interchangeable, so every way of splitting the pooled charges is equally
    likely and the p-value is the share of splits at least as extreme as the one
    observed. Small samples are enumerated exactly, larger ones sampled.

    The statistic is the rank sum of the late window, not the difference in
    medians. A subscription that steps cleanly from $15.99 to $18.99 defeats a
    median test: with ten charges at the old price and eight at the new, almost
    every relabelling also produces a $3.00 gap between the two medians, so the
    real step scores an unremarkable p. Ranks notice that all eight high charges
    landed on the same side, which is the thing actually worth noticing. Ties
    take midranks, so a flat price contributes no evidence either way.

    The second return value is 2/(number of distinct splits), the smallest
    p-value this test could possibly produce for this sample. When that floor is
    above 0.05 nothing from this merchant can be called significant however
    large the price rise, and the caller should say so rather than reporting
    "not significant" and letting it read as "no change".
    """
    n, k = len(early) + len(late), len(late)
    if k == 0 or k == n:
        return 1.0, 1.0
    pool = early + late
    ranks = _midranks(pool)
    expected = k * (n + 1) / 2.0
    observed = abs(sum(ranks[len(early):]) - expected)

    total_splits = math.comb(n, k)
    floor = 2.0 / total_splits

    if total_splits <= PERMUTATIONS:
        hits = sum(1 for pick in combinations(range(n), k)
                   if abs(sum(ranks[i] for i in pick) - expected) >= observed - 1e-9)
        return hits / total_splits, floor

    import random
    rng = random.Random(SEED)
    shuffled = list(ranks)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(shuffled)
        if abs(sum(shuffled[:k]) - expected) >= observed - 1e-9:
            hits += 1
    # +1 smoothing so a sampled test never claims p == 0 on finite resamples.
    return (hits + 1) / (PERMUTATIONS + 1), floor


def annualise(pct: float, days: float) -> float | None:
    """Restate a percent change over ``days`` days as an annual rate."""
    if days < MIN_GAP_DAYS or pct <= -100.0:
        return None
    return ((1.0 + pct / 100.0) ** (365.25 / days) - 1.0) * 100.0


# --------------------------------------------------------------------------
# Per-merchant price drift
# --------------------------------------------------------------------------

@dataclass(slots=True)
class PriceTrend:
    """What one merchant's charges did between the start and the end."""
    key: str
    name: str
    category: str
    n: int
    first: date
    last: date
    span_days: int
    early: float                  # median per-visit spend, first window
    late: float                   # median per-visit spend, last window
    pct: float
    gap_days: int                 # between the midpoints of the two windows
    annual_pct: float | None
    p_value: float
    p_floor: float
    dispersion: float             # MAD/median across every visit
    total: float
    subscription: bool            # a fixed recurring charge, so price is real
    sub_pct: float | None = None  # what detect_recurring made of the same drift

    @property
    def underpowered(self) -> bool:
        """True when this sample can never reach significance, whatever it shows."""
        return self.p_floor > SIGNIFICANT

    @property
    def verdict(self) -> str:
        if self.underpowered:
            return "underpowered"
        if self.p_value < SIGNIFICANT:
            return "significant"
        if self.p_value < SUGGESTIVE:
            return "suggestive"
        return "noise"

    @property
    def direction(self) -> str:
        return "up" if self.pct > 0 else "down" if self.pct < 0 else "flat"

    @property
    def implausible(self) -> bool:
        """An annualised rate this large is a parse error wearing a price tag."""
        return self.annual_pct is not None and abs(self.annual_pct) >= ABSURD_ANNUAL_PCT


_VERDICT_ORDER = {"significant": 0, "suggestive": 1, "underpowered": 2, "noise": 3}


def _split_windows(rows: list[tuple[date, float]]) -> tuple[list, list]:
    """First half against last half, by count, dropping an odd middle charge.

    Halves rather than the thirds detect_recurring uses. Thirds are right when
    you have thirty payments of a subscription; here six is a common sample and
    thirds would leave two charges a side.
    """
    k = len(rows) // 2
    return rows[:k], rows[len(rows) - k:]


def price_trends(engine: Engine, *, min_visits: int = MIN_VISITS,
                 min_span: int = MIN_SPAN_DAYS) -> tuple[list[PriceTrend], dict]:
    """Track per-visit spend over time for every repeat merchant.

    Returns the qualifying trends plus a census of what was rejected and why,
    because with thin data the rejects are the more informative half.
    """
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in engine.spend():
        if t.merchant:
            groups[merchant_key(t.merchant)].append(t)

    # Engine.merchants() picks the modal name and category with
    # ``max(set(...), key=list.count)``, and iterating a set of strings is not
    # order-stable across runs, so a tie there resolves differently each time.
    # Totals are taken from Engine so the two views agree; the labels are
    # recomputed with an alphabetical tie-break so a report is reproducible.
    meta = {m["key"]: m for m in engine.merchants()}

    def modal(values: list[str]) -> str:
        counts: dict[str, int] = defaultdict(int)
        for v in values:
            counts[v] += 1
        return min(counts, key=lambda v: (-counts[v], v))

    # detect_recurring already solves this for fixed-price subscriptions. Reuse
    # its answer rather than second-guessing it.
    recurring = {r.key: r for r in engine.recurring()}

    # Two windows of MIN_WINDOW is a hard floor, so asking for fewer visits than
    # that cannot widen the net and the census should not pretend otherwise.
    min_visits = max(min_visits, 2 * MIN_WINDOW)

    census = {"merchants": len(groups), "too_few": 0, "too_short": 0,
              "thin_window": 0, "qualified": 0, "spend_covered": 0.0,
              "spend_total": engine.total_spend(),
              "min_visits": min_visits, "min_span": min_span}

    out: list[PriceTrend] = []
    for key, txns in groups.items():
        txns.sort(key=lambda t: (t.d or date.min))
        rows = [(t.d, t.amount) for t in txns if t.d]
        if len(rows) < min_visits:
            census["too_few"] += 1
            continue
        span = (rows[-1][0] - rows[0][0]).days
        if span < min_span:
            census["too_short"] += 1
            continue
        early, late = _split_windows(rows)
        if len(early) < MIN_WINDOW or len(late) < MIN_WINDOW:
            census["thin_window"] += 1
            continue

        e_amt = [a for _, a in early]
        l_amt = [a for _, a in late]
        e_med, l_med = stats.median(e_amt), stats.median(l_amt)
        if e_med <= 0:
            census["thin_window"] += 1
            continue

        pct = (l_med - e_med) / e_med * 100.0
        gap = (stats.median([d.toordinal() for d, _ in late])
               - stats.median([d.toordinal() for d, _ in early]))
        p, floor = permutation_p(e_amt, l_amt)

        rec = recurring.get(key)
        info = meta.get(key, {})
        out.append(PriceTrend(
            key=key,
            name=modal([t.merchant for t in txns]),
            category=modal([t.category for t in txns]),
            n=len(rows), first=rows[0][0], last=rows[-1][0], span_days=span,
            early=e_med, late=l_med, pct=pct,
            gap_days=int(gap), annual_pct=annualise(pct, gap),
            p_value=p, p_floor=floor,
            dispersion=_mad_ratio([a for _, a in rows]),
            # Engine's total, so the weights below add up to the same spend the
            # rest of the toolchain reports for this merchant.
            total=info.get("total", sum(a for _, a in rows)),
            subscription=rec is not None,
            sub_pct=rec.price_pct if rec else None,
        ))
        census["qualified"] += 1
        census["spend_covered"] += out[-1].total

    # Strength of evidence first, size of the move second. Sorting on the size
    # of the move alone puts the noisiest merchant at the top of the page, which
    # is exactly the row a reader should trust least.
    out.sort(key=lambda p: (_VERDICT_ORDER[p.verdict], -abs(p.pct)))
    return out, census


# --------------------------------------------------------------------------
# A personal CPI
# --------------------------------------------------------------------------

@dataclass(slots=True)
class CategoryRate:
    """One category's price change, and what the nation did in the same one."""
    category: str
    merchants: list[PriceTrend]
    category_spend: float          # all spend in this category
    measured_spend: float          # spend at merchants that qualified
    rate: float                    # spend-weighted annualised percent
    weight: float = 0.0            # share of the measured basket
    national: float | None = None
    series: str | None = None
    proxy: bool = False

    @property
    def coverage(self) -> float:
        return self.measured_spend / self.category_spend if self.category_spend else 0.0

    @property
    def gap(self) -> float | None:
        return None if self.national is None else self.rate - self.national

    @property
    def contribution(self) -> float | None:
        """Percentage points this category adds to the personal-national gap."""
        g = self.gap
        return None if g is None else g * self.weight


@dataclass(slots=True)
class PersonalCPI:
    categories: list[CategoryRate] = field(default_factory=list)
    rate: float | None = None
    national_basket: float | None = None   # your weights, national prices
    national_headline: float | None = None # CPI-U all items
    cpi_year: int = ref.CPI_LATEST_YEAR
    coverage: float = 0.0            # of spend that has a price at all
    significant_share: float = 0.0   # of measured spend whose trend cleared p<0.05
    measured_spend: float = 0.0
    priceable_spend: float = 0.0     # total spend minus the NON_PRICE categories
    total_spend: float = 0.0
    reasons: list[str] = field(default_factory=list)   # why there is no number
    notes: list[str] = field(default_factory=list)     # what was set aside on the way

    @property
    def ok(self) -> bool:
        return self.rate is not None


def personal_cpi(engine: Engine, trends: list[PriceTrend]) -> PersonalCPI:
    """Weight per-category price drift by that category's share of your spend.

    The national basket is not used for the weights, which is the entire point:
    if a third of your money goes on delivery then delivery prices matter to you
    a third as much as everything else put together, whatever CPI-U's weight for
    restaurants happens to be.

    Categories with no qualifying merchant are dropped rather than assumed flat.
    Assuming flat would drag the index toward zero and quietly turn missing data
    into a reassuring answer. The cost of dropping them is that the result only
    speaks for the covered share of spending, which is reported alongside it.
    """
    out = PersonalCPI()
    by_cat = engine.by_category()
    out.total_spend = sum(by_cat.values())

    priced = [t for t in trends if t.category not in NON_PRICE]
    hit = sorted({t.category for t in trends if t.category in NON_PRICE})
    if hit:
        out.notes.append(
            f"{len(trends) - len(priced)} merchant trend(s) excluded as non-price "
            f"({', '.join(hit)}): interest, tax and unknown charges are not the "
            f"price of anything, and CPI-U leaves them out for the same reason.")

    usable = [t for t in priced if t.annual_pct is not None and not t.implausible]
    dropped = len(priced) - len(usable)
    if dropped:
        out.notes.append(
            f"{dropped} merchant trend(s) set aside: the window between the two "
            f"halves was under {MIN_GAP_DAYS} days so no annual rate can be read "
            f"from it, or the implied rate reached {ABSURD_ANNUAL_PCT:.0f}%/yr or "
            f"more, which is a data problem rather than a price.")

    grouped: dict[str, list[PriceTrend]] = defaultdict(list)
    for t in usable:
        grouped[t.category].append(t)

    for cat, items in grouped.items():
        if len(items) < MIN_CATEGORY_MERCHANTS:
            continue
        measured = sum(i.total for i in items)
        if measured <= 0:
            continue
        rate = sum(i.annual_pct * i.total for i in items) / measured
        series = ref.CATEGORY_TO_CPI.get(cat)
        out.categories.append(CategoryRate(
            category=cat, merchants=items,
            category_spend=by_cat.get(cat, measured), measured_spend=measured,
            rate=rate, series=series,
            national=ref.cpi_rate(series, out.cpi_year) if series else None,
            proxy=cat in ref.CPI_PROXY,
        ))

    out.measured_spend = sum(c.measured_spend for c in out.categories)
    out.priceable_spend = sum(v for k, v in by_cat.items() if k not in NON_PRICE)
    out.coverage = (out.measured_spend / out.priceable_spend
                    if out.priceable_spend else 0.0)
    strong = sum(m.total for c in out.categories for m in c.merchants
                 if m.verdict == "significant")
    out.significant_share = strong / out.measured_spend if out.measured_spend else 0.0

    if len(out.categories) < MIN_CPI_CATEGORIES:
        out.reasons.append(
            f"{len(out.categories)} category/categories cleared the evidence bar; "
            f"{MIN_CPI_CATEGORIES} are needed before a weighted average is an "
            f"index rather than a single merchant wearing a hat.")
    if out.coverage < MIN_CPI_COVERAGE:
        out.reasons.append(
            f"measured merchants cover {out.coverage * 100:.1f}% of spending that "
            f"has a price ({money(out.measured_spend, 2)} of "
            f"{money(out.priceable_spend, 2)}); {MIN_CPI_COVERAGE * 100:.0f}% is "
            f"the floor for calling the result a rate for your whole basket.")

    # Weights are shares of the *measured* basket, not of all spending. That is
    # the standard treatment of a partial index and it carries an assumption
    # worth naming: the unmeasured share is presumed to have moved like the
    # measured share. Coverage is printed next to the headline for that reason.
    if out.measured_spend > 0:
        for c in out.categories:
            c.weight = c.measured_spend / out.measured_spend

    if not out.reasons:
        out.rate = sum(c.rate * c.weight for c in out.categories)
        priced = [c for c in out.categories if c.national is not None]
        w = sum(c.weight for c in priced)
        if w > 0:
            out.national_basket = sum(c.national * c.weight for c in priced) / w
        out.national_headline = ref.cpi_rate("All items", out.cpi_year)

    out.categories.sort(key=lambda c: -c.weight)
    return out


# --------------------------------------------------------------------------
# Basket against behaviour
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Split:
    """A category's spend change taken apart into cost-per-visit and visits."""
    category: str
    n_early: int
    n_late: int
    total_early: float
    total_late: float
    avg_early: float
    avg_late: float

    @property
    def change(self) -> float:
        return self.total_late - self.total_early

    @property
    def price_effect(self) -> float:
        """Dollars explained by each visit costing more."""
        return self.n_late * (self.avg_late - self.avg_early)

    @property
    def volume_effect(self) -> float:
        """Dollars explained by going more often."""
        return (self.n_late - self.n_early) * self.avg_early

    @property
    def price_pct(self) -> float:
        return (self.avg_late / self.avg_early - 1.0) * 100.0 if self.avg_early else 0.0

    @property
    def volume_pct(self) -> float:
        return (self.n_late / self.n_early - 1.0) * 100.0 if self.n_early else 0.0

    @property
    def fragile(self) -> bool:
        """True when the visit count is an artefact of where the midpoint fell.

        A category that is really one fixed monthly charge — rent, a
        subscription — has eighteen months land as ten visits then eight purely
        because 18 is odd. That reads as a 20 percent drop in behaviour and it
        is nothing of the kind.
        """
        return abs(self.n_late - self.n_early) <= 2 and max(self.n_early, self.n_late) <= 12

    @property
    def verdict(self) -> str:
        p, v = abs(self.price_effect), abs(self.volume_effect)
        if p + v == 0:
            return "unchanged"
        if self.fragile and v > p:
            return "too few visits to say"
        if p >= v * 2:
            return "mostly prices"
        if v >= p * 2:
            return "mostly behaviour"
        return "both"


def basket_vs_behaviour(engine: Engine) -> tuple[list[Split], str]:
    """Split each category's change into cost per visit and number of visits.

    The decomposition is exact:

        late - early = n_late * (avg_late - avg_early)   the price part
                     + (n_late - n_early) * avg_early    the volume part

    with the cross term (more visits *and* dearer visits) folded into the price
    part, which is the usual Laspeyres convention and is stated here so nobody
    has to reverse-engineer it.

    What it cannot do is separate a dearer basket from a bigger one. A statement
    line is a total. If the weekly shop goes from $40 to $52 there is no way to
    know from this data whether that is the same trolley at 2025 prices or the
    same prices and more in the trolley. Cost per visit is the best proxy
    available and it is a proxy, not a price.
    """
    rng = engine.dates()
    if not rng:
        return [], "no dated spending"
    first, last = rng
    midpoint = first + timedelta(days=(last - first).days // 2)

    early: dict[str, list[float]] = defaultdict(list)
    late: dict[str, list[float]] = defaultdict(list)
    for t in engine.spend():
        if not t.d:
            continue
        (early if t.d < midpoint else late)[t.category].append(t.amount)

    out = []
    for cat in set(early) | set(late):
        e, l = early.get(cat, []), late.get(cat, [])
        if len(e) + len(l) < MIN_SPLIT_TOTAL:
            continue
        if len(e) < MIN_SPLIT_HALF or len(l) < MIN_SPLIT_HALF:
            continue
        out.append(Split(
            category=cat, n_early=len(e), n_late=len(l),
            total_early=sum(e), total_late=sum(l),
            avg_early=sum(e) / len(e), avg_late=sum(l) / len(l)))

    out.sort(key=lambda s: -abs(s.change))
    note = (f"{first:%d %b %Y} to {midpoint:%d %b %Y} against "
            f"{midpoint:%d %b %Y} to {last:%d %b %Y}")
    return out, note


# --------------------------------------------------------------------------
# Benchmark against the Consumer Expenditure Survey
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BenchmarkRow:
    group: str
    categories: tuple[str, ...]
    user_annual: float
    national: float
    unreliable: bool

    @property
    def ratio(self) -> float | None:
        return self.user_annual / self.national if self.national else None


@dataclass(slots=True)
class Benchmark:
    quintile: int
    label: str
    rows: list[BenchmarkRow]
    months: int
    scale: float
    unmapped: dict[str, float]
    unmapped_total: float
    mapped_total: float
    note: str


def benchmark(engine: Engine, income: float) -> Benchmark:
    """Annualise the user's spend per group and set it beside the BLS figure.

    Annualising uses only the months the data actually covers in full, then
    scales to twelve. A partial month at either end would otherwise depress
    every ratio in the table and make the comparison flattering for the wrong
    reason.
    """
    q = ref.quintile_for_income(income)
    complete = set(engine.complete_months())
    if not complete:
        complete = set(engine.by_month())
    months = len(complete) or 1
    scale = 12.0 / months

    per_cat: dict[str, float] = defaultdict(float)
    for t in engine.spend():
        if t.date[:7] in complete:
            per_cat[t.category] += t.amount

    rows, unmapped = [], {}
    mapped_total = 0.0
    grouped: dict[str, float] = defaultdict(float)
    for cat, amt in per_cat.items():
        g = ref.group_for_category(cat)
        if g is None:
            unmapped[cat] = amt * scale
        else:
            grouped[g] += amt
            mapped_total += amt

    for g, amt in grouped.items():
        got = ref.group_annual(g, q)
        if got is None:
            continue
        national, shaky = got
        rows.append(BenchmarkRow(
            group=g, categories=ref.BENCHMARK_GROUPS[g]["categories"],
            user_annual=amt * scale, national=national, unreliable=shaky))

    rows.sort(key=lambda r: -(r.ratio or 0))
    return Benchmark(
        quintile=q, label=ref.QUINTILES[q]["label"], rows=rows,
        months=months, scale=scale, unmapped=dict(sorted(
            unmapped.items(), key=lambda kv: -kv[1])),
        unmapped_total=sum(unmapped.values()),
        mapped_total=mapped_total * scale,
        note=f"{months} complete month(s) scaled by {scale:.2f} to a year")


# --------------------------------------------------------------------------
# Text report
# --------------------------------------------------------------------------

def _pct(v: float | None, dp: int = 1) -> str:
    return "n/a" if v is None else f"{v:+.{dp}f}%"


def print_merchants(trends: list[PriceTrend], census: dict, *, limit: int = 0):
    print("PER-MERCHANT PRICE DRIFT")
    print(f"Merchants seen: {census['merchants']}. Qualifying "
          f"({census['min_visits']}+ visits over {census['min_span']}+ days, "
          f"{MIN_WINDOW}+ per window): {census['qualified']}.")
    print(f"Rejected: {census['too_few']} too few visits, "
          f"{census['too_short']} too short a span, "
          f"{census['thin_window']} not splittable into two windows.")
    cov = (census["spend_covered"] / census["spend_total"] * 100.0
           if census["spend_total"] else 0.0)
    print(f"Qualifying merchants hold {money(census['spend_covered'], 2)} of "
          f"{money(census['spend_total'], 2)} spend ({cov:.1f}%).\n")

    if not trends:
        print("  Not enough data. No merchant in this database has been visited")
        print(f"  {census['min_visits']} times across {census['min_span']} days, "
              f"which is the least")
        print("  that can distinguish a price change from a different order.\n")
        return

    rows = trends[:limit] if limit else trends
    print(f"  {'merchant':<26}{'cat':<14}{'n':>3} {'first':>8} {'now':>9} "
          f"{'change':>9} {'per yr':>9} {'p':>7}  verdict")
    for t in rows:
        tag = "sub" if t.subscription else ""
        print(f"  {t.name[:25]:<26}{t.category[:13]:<14}{t.n:>3} "
              f"{money(t.early, 2):>8} {money(t.late, 2):>9} "
              f"{_pct(t.pct):>9} {_pct(t.annual_pct):>9} "
              f"{t.p_value:>7.3f}  {t.verdict}{' ' + tag if tag else ''}")

    under = [t for t in rows if t.underpowered]
    if under:
        floor = max(t.p_floor for t in under)
        print(f"\n  {len(under)} of these are underpowered: with samples this small "
              f"the smallest\n  p-value the test can return is {floor:.2f}, so "
              f"significance is unreachable\n  no matter how large the change. "
              f"Read those rows as a direction, not a finding.")

    noisy = [t for t in rows if t.dispersion > 0.5 and not t.subscription]
    if noisy:
        print(f"\n  {len(noisy)} merchant(s) have charge-to-charge variation above "
              f"50% of the median.\n  For those, cost per visit is tracking basket "
              f"size more than price.")
    print()


def print_report(engine: Engine, args):
    rng = engine.dates()
    n = len(engine.transactions)
    print("=" * 74)
    print("PERSONAL INFLATION")
    print("=" * 74)
    if rng:
        span = (rng[1] - rng[0]).days
        print(f"{n} transactions | {rng[0]} to {rng[1]} ({span} days, "
              f"{len(engine.complete_months())} complete months) | "
              f"{money(engine.total_spend(), 2)} spend")
    cov = engine.coverage()
    if cov["statements"] and not cov["trusted"]:
        print(f"Statement reconciliation: {cov['reconciled']}/{cov['statements']} "
              f"verified ({cov['pct']:.0f}%), {cov['mismatched']} mismatched, "
              f"{cov['unchecked']} unchecked, {len(cov['gaps'])} period gap(s).")
        for g in cov["gaps"][:3]:
            print(f"  gap: {g['account']} {g['after']} to {g['before']} "
                  f"({g['days']} days)")
        print("A gap is spending nobody recorded, and that moves every weight below.")
    print()

    trends, census = price_trends(engine, min_visits=args.min_visits,
                                  min_span=args.min_span)
    print_merchants(trends, census)
    if args.merchants:
        return

    # -- personal CPI --
    cpi = personal_cpi(engine, trends)
    print("YOUR PERSONAL INFLATION RATE")
    if not cpi.ok:
        print("  Not enough data to compute one. Specifically:")
        for r in cpi.reasons + cpi.notes:
            print(f"    - {r}")
        print("  A personal CPI needs several merchants you return to, in several")
        print("  categories, over enough months for a price to visibly move. This")
        print(f"  database has {census['qualified']} qualifying merchant(s) in "
              f"{len(cpi.categories)} category/categories.")
        if cpi.categories:
            print("\n  What was measurable, for what it is worth:")
            for c in cpi.categories:
                print(f"    {c.category:<16} {_pct(c.rate)}/yr from "
                      f"{len(c.merchants)} merchant(s), "
                      f"{money(c.measured_spend, 2)} of "
                      f"{money(c.category_spend, 2)} category spend")
    else:
        nat = cpi.national_headline
        print(f"  Your personal inflation rate over this period was "
              f"{cpi.rate:+.1f}%/yr,")
        print(f"  against CPI-U all items of {nat:+.1f}% for {cpi.cpi_year}.")
        if cpi.national_basket is not None:
            print(f"  Your basket at national prices would have been "
                  f"{cpi.national_basket:+.1f}%/yr.")
        print(f"  Covers {cpi.coverage * 100:.0f}% of your spending that has a "
              f"price ({money(cpi.measured_spend, 2)} of "
              f"{money(cpi.priceable_spend, 2)}); the rest is assumed to have "
              f"moved the same way.")
        print(f"  {cpi.significant_share * 100:.0f}% of that rests on a merchant "
              f"trend that cleared p<{SIGNIFICANT}; the remainder is a direction "
              f"with the sample to back it, not a proven move.")
        for note in cpi.notes:
            print(f"  Note: {note}")
        print()
        print(f"  {'category':<16}{'weight':>8}{'yours':>10}{'national':>10}"
              f"{'gap':>9}{'drives':>9}  series")
        for c in cpi.categories:
            print(f"  {c.category:<16}{c.weight * 100:>7.1f}%{_pct(c.rate):>10}"
                  f"{_pct(c.national):>10}{_pct(c.gap):>9}"
                  f"{_pct(c.contribution, 2):>9}  "
                  f"{(c.series or 'unmapped')}{' (proxy)' if c.proxy else ''}")
    print()

    # -- basket against behaviour --
    splits, note = basket_vs_behaviour(engine)
    print("PRICES OR BEHAVIOUR")
    print(f"  First half against second half: {note}.")
    print("  Cost per visit is a proxy for price. Statements carry totals, not")
    print("  line items, so a bigger basket and a dearer basket look identical")
    print("  here. This split says how much of the change came with more visits;")
    print("  it cannot say how much of the rest came with more in the bag.\n")
    if not splits:
        print(f"  Not enough data. No category has {MIN_SPLIT_TOTAL}+ transactions "
              f"with at least\n  {MIN_SPLIT_HALF} on each side of the midpoint.\n")
    else:
        print(f"  {'category':<16}{'was':>10}{'now':>10}{'change':>10}"
              f"{'prices':>10}{'visits':>10}  reading")
        for s in splits:
            print(f"  {s.category:<16}{money(s.total_early, 0):>10}"
                  f"{money(s.total_late, 0):>10}{money(s.change, 0):>10}"
                  f"{money(s.price_effect, 0):>10}{money(s.volume_effect, 0):>10}"
                  f"  {s.verdict}")
            print(f"  {'':<16}{s.n_early:>9} v{s.n_late:>9} visits"
                  f"{'':>10}{_pct(s.price_pct):>10}{_pct(s.volume_pct):>10}")
        print()

    # -- benchmark --
    print("AGAINST THE NATIONAL AVERAGE")
    if args.income is None:
        print("  Skipped. Pass --income with your annual pre-tax household income.")
        print("  The BLS publishes this by income quintile and the brackets are")
        print("  wide: the middle fifth runs "
              f"${ref.QUINTILES[3]['lower']:,} to ${ref.QUINTILES[3]['upper']:,}.")
        print("  Guessing which one you are in would decide the answer, so it is")
        print("  refused rather than defaulted.\n")
    else:
        bm = benchmark(engine, args.income)
        src = ref.SOURCE["ce"]
        print(f"  Income {money(args.income)} puts you in the {bm.label} "
              f"({bm.quintile} of 5).")
        print(f"  {src['survey']} {src['survey_year']}, {src['table']}, "
              f"released {src['released']}.")
        print(f"  Your side is {bm.note}.\n")
        if not bm.rows:
            print("  Nothing to compare: no spending landed in a category with a")
            print("  Consumer Expenditure Survey counterpart.\n")
        else:
            print(f"  {'group':<30}{'you/yr':>11}{'national':>11}{'ratio':>9}")
            for r in bm.rows:
                mark = "*" if r.unreliable else " "
                print(f"  {r.group:<30}{money(r.user_annual, 0):>11}"
                      f"{money(r.national, 0):>11}{r.ratio:>8.2f}x{mark}")
            loud = [r for r in bm.rows if r.ratio and r.ratio >= 1.5]
            for r in loud:
                print(f"\n  You spend {r.ratio:.1f}x the national average for your "
                      f"income bracket on\n  {r.group} "
                      f"({', '.join(r.categories)}).")
            if any(r.unreliable for r in bm.rows):
                print("\n  * BLS flags a component of this row as having a relative "
                      "standard\n    error of 25% or more and considers it unreliable.")
        if bm.unmapped:
            print(f"\n  Outside the comparison: {money(bm.unmapped_total, 0)}/yr "
                  f"against {money(bm.mapped_total, 0)}/yr inside it.")
            for cat, amt in list(bm.unmapped.items())[:8]:
                why = ref.UNMAPPED.get(cat, "no CE counterpart")
                print(f"    {cat:<16}{money(amt, 0):>10}  {why}")
        print()

    print("READ THIS BEFORE QUOTING ANY OF IT")
    for c in ref.CAVEATS:
        print(f"  - {c}")


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{--paper:#FBFBF7;--band:#E7EFE3;--ink:#14211B;--soft:#5C6D62;--rule:#C2D1BC;
--rule-strong:#8FA88A;--red:#A3372A;--green:#2E6A4C;--amber:#9C7318;
--display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:#DCE3D8;color:var(--ink);font-family:var(--ui);
font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.sheet{max-width:1020px;margin:26px auto;background:var(--paper);
border:1px solid var(--rule-strong);padding:0 44px 40px;
box-shadow:0 1px 0 rgba(0,0,0,.06),0 12px 34px rgba(20,33,27,.10)}
@media(max-width:820px){.sheet{padding:0 16px 28px;margin:0}}
header.head{padding:30px 0 18px;border-bottom:2px solid var(--ink);margin-bottom:8px}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
text-transform:uppercase;color:var(--soft)}
h1{font-family:var(--display);font-size:36px;font-weight:400;margin:6px 0 10px;
line-height:1.05}
.headmeta{display:flex;flex-wrap:wrap;gap:8px 28px;font-family:var(--mono);
font-size:12px;color:var(--soft)}
.headmeta b{color:var(--ink);font-weight:600}
section{margin:34px 0 0}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;
text-transform:uppercase;color:var(--red);border-bottom:1px solid var(--rule);
padding-bottom:6px;display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.eyebrow span{color:var(--soft);letter-spacing:.04em;text-transform:none}
h2{font-family:var(--display);font-size:24px;font-weight:400;margin:14px 0 4px}
.lede{color:var(--soft);max-width:64ch;margin:0 0 14px}
.headline{font-family:var(--display);font-size:30px;line-height:1.25;margin:12px 0 6px}
.headline b{font-family:var(--mono);font-size:30px;font-weight:600}
.headline b.up{color:var(--red)}.headline b.down{color:var(--green)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
color:var(--soft);text-align:right;padding:8px 9px;
border-bottom:1.5px solid var(--rule-strong);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:7px 9px;text-align:right;border-bottom:1px solid rgba(194,209,188,.55);
font-variant-numeric:tabular-nums}
td.mono{font-family:var(--mono)}
tbody tr:nth-child(even){background:var(--band)}
.up{color:var(--red)}.down{color:var(--green)}
.pill{font-family:var(--mono);font-size:10.5px;padding:1px 6px;
border:1px solid var(--rule-strong);color:var(--soft);white-space:nowrap}
.pill.sig{color:var(--red);border-color:var(--red)}
.pill.weak{color:var(--amber);border-color:var(--amber)}
.note{border-left:3px solid var(--amber);background:#FBF6E9;padding:11px 14px;
margin:16px 0 0;font-size:13px;color:var(--soft)}
.note b{color:var(--ink);font-weight:600}
.note.hard{border-left-color:var(--red);background:#FAF0EE}
ul.caveats{margin:10px 0 0;padding-left:18px;font-size:13px;color:var(--soft)}
ul.caveats li{margin:6px 0}
.sub{color:var(--soft);font-size:12px;font-family:var(--mono)}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft)}
@media print{body{background:#fff}.sheet{box-shadow:none;border:0;margin:0}
section{break-inside:avoid}}
"""


def esc(s) -> str:
    return html.escape(str(s))


def _td(v: float | None, extra: str = "mono") -> str:
    """Open a table cell tinted by the sign of the number it will hold."""
    tint = "" if v is None or v == 0 else " up" if v > 0 else " down"
    return f'<td class="{extra}{tint}">'


def render_html(engine: Engine, args, path: str):
    rng = engine.dates()
    trends, census = price_trends(engine, min_visits=args.min_visits,
                                  min_span=args.min_span)
    cpi = personal_cpi(engine, trends)
    splits, split_note = basket_vs_behaviour(engine)
    bm = benchmark(engine, args.income) if args.income is not None else None
    cov = engine.coverage()
    parts: list[str] = []

    # -- headline --
    if cpi.ok:
        d = "up" if cpi.rate > 0 else "down"
        head = (f'<p class="headline">Your personal inflation rate over this period '
                f'was <b class="{d}">{cpi.rate:+.1f}%</b> a year, against CPI-U all '
                f'items of <b>{cpi.national_headline:+.1f}%</b> for {cpi.cpi_year}.</p>'
                f'<p class="lede">Weighted by your basket, not the national one. '
                f'Covers {cpi.coverage * 100:.0f}% of your spending that has a price '
                f'({esc(money(cpi.measured_spend, 2))} of '
                f'{esc(money(cpi.priceable_spend, 2))}); the uncovered share is '
                f'assumed to have moved the same way. '
                f'{cpi.significant_share * 100:.0f}% of the measured spend rests on '
                f'a trend that cleared p&lt;{SIGNIFICANT}.</p>')
        rows = "".join(
            f'<tr><td>{esc(c.category)}</td>'
            f'<td class="mono">{c.weight * 100:.1f}%</td>'
            f'{_td(c.rate)}{_pct(c.rate)}</td>'
            f'{_td(c.national)}{_pct(c.national)}</td>'
            f'{_td(c.gap)}{_pct(c.gap)}</td>'
            f'{_td(c.contribution)}{_pct(c.contribution, 2)}</td>'
            f'<td class="sub">{esc(c.series or "unmapped")}'
            f'{" (proxy)" if c.proxy else ""}</td></tr>'
            for c in cpi.categories)
        head += ('<div class="scroll"><table><thead><tr><th>Category</th>'
                 '<th>Weight</th><th>Yours</th><th>National</th><th>Gap</th>'
                 '<th>Drives</th><th>CPI series</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div>')
        for note in cpi.notes:
            head += f'<p class="note">{esc(note)}</p>'
    else:
        reasons = "".join(f"<li>{esc(r)}</li>" for r in cpi.reasons + cpi.notes)
        head = (f'<p class="headline">Not enough data for a personal '
                f'inflation rate.</p><ul class="caveats">{reasons}</ul>'
                f'<p class="lede">This is the correct output for this database, '
                f'not a failure to compute. {census["qualified"]} of '
                f'{census["merchants"]} merchants clear the evidence bar, in '
                f'{len(cpi.categories)} category/categories. A rate would be '
                f'arithmetic on nothing.</p>')
        if cpi.categories:
            rows = "".join(
                f'<tr><td>{esc(c.category)}</td>'
                f'{_td(c.rate)}{_pct(c.rate)}</td>'
                f'<td class="mono">{len(c.merchants)}</td>'
                f'<td class="mono">{esc(money(c.measured_spend, 2))}</td>'
                f'<td class="mono">{esc(money(c.category_spend, 2))}</td>'
                f'{_td(c.national)}{_pct(c.national)}</td></tr>'
                for c in cpi.categories)
            head += ('<p class="lede">What was measurable, for what it is worth:</p>'
                     '<div class="scroll"><table><thead><tr><th>Category</th>'
                     '<th>Rate/yr</th><th>Merchants</th><th>Measured</th>'
                     '<th>Category spend</th><th>National</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
    parts.append(f'<section><div class="eyebrow">Personal CPI<span>'
                 f'weights from your own spending</span></div>{head}</section>')

    # -- merchants --
    cov_pct = (census["spend_covered"] / census["spend_total"] * 100.0
               if census["spend_total"] else 0.0)
    if trends:
        rows = []
        for t in trends:
            klass = {"significant": "pill sig", "suggestive": "pill weak"}.get(
                t.verdict, "pill")
            rows.append(
                f'<tr><td>{esc(t.name)}'
                f'{" <span class=\"pill\">sub</span>" if t.subscription else ""}'
                f'<div class="sub">{esc(t.first)} to {esc(t.last)}, '
                f'{t.span_days} days</div></td>'
                f'<td>{esc(t.category)}</td><td class="mono">{t.n}</td>'
                f'<td class="mono">{esc(money(t.early, 2))}</td>'
                f'<td class="mono">{esc(money(t.late, 2))}</td>'
                f'{_td(t.pct)}{_pct(t.pct)}</td>'
                f'{_td(t.annual_pct)}{_pct(t.annual_pct)}'
                f'{"<div class=\"sub\">implausible</div>" if t.implausible else ""}'
                f'</td>'
                f'<td class="mono">{t.p_value:.3f}</td>'
                f'<td><span class="{klass}">{t.verdict}</span></td></tr>')
        table = ('<div class="scroll"><table><thead><tr><th>Merchant</th>'
                 '<th>Category</th><th>Visits</th><th>Then</th><th>Now</th>'
                 '<th>Change</th><th>Per year</th><th>p</th><th></th>'
                 f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
        under = [t for t in trends if t.underpowered]
        if under:
            table += (f'<p class="note"><b>{len(under)} row(s) are underpowered.</b> '
                      f'With samples this small the smallest p-value the permutation '
                      f'test can return is {max(t.p_floor for t in under):.2f}. '
                      f'Significance is unreachable however large the change, so '
                      f'read those as a direction and not a finding.</p>')
    else:
        table = (f'<p class="note hard"><b>Not enough data.</b> No merchant here has '
                 f'been visited {MIN_VISITS} times across {MIN_SPAN_DAYS} days, which '
                 f'is the least that can tell a price change from a different order. '
                 f'{census["too_few"]} merchants fall short on visits, '
                 f'{census["too_short"]} on span.</p>')
    parts.append(
        f'<section><div class="eyebrow">Per-merchant drift'
        f'<span>{census["qualified"]} of {census["merchants"]} merchants qualify'
        f'</span></div><h2>What each place charges you now</h2>'
        f'<p class="lede">A merchant qualifies on {MIN_VISITS} or more visits '
        f'spanning {MIN_SPAN_DAYS} or more days, with at least {MIN_WINDOW} charges '
        f'in each half. Qualifying merchants hold {cov_pct:.1f}% of spending. '
        f'The p-value comes from a permutation test on the difference in medians, '
        f'not a t-test: six charges from one shop are not normally distributed.'
        f'</p>{table}</section>')

    # -- basket vs behaviour --
    if splits:
        rows = "".join(
            f'<tr><td>{esc(s.category)}<div class="sub">{s.n_early} then '
            f'{s.n_late} visits</div></td>'
            f'<td class="mono">{esc(money(s.total_early, 0))}</td>'
            f'<td class="mono">{esc(money(s.total_late, 0))}</td>'
            f'{_td(s.change)}{esc(money(s.change, 0))}</td>'
            f'{_td(s.price_effect)}{esc(money(s.price_effect, 0))}'
            f'<div class="sub">{_pct(s.price_pct)}</div></td>'
            f'{_td(s.volume_effect)}{esc(money(s.volume_effect, 0))}'
            f'<div class="sub">{_pct(s.volume_pct)}</div></td>'
            f'<td>{esc(s.verdict)}</td></tr>' for s in splits)
        body = ('<div class="scroll"><table><thead><tr><th>Category</th><th>Was</th>'
                '<th>Now</th><th>Change</th><th>Cost per visit</th><th>Visits</th>'
                f'<th>Reading</th></tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        body = (f'<p class="note hard"><b>Not enough data.</b> No category has '
                f'{MIN_SPLIT_TOTAL} or more transactions with at least '
                f'{MIN_SPLIT_HALF} on each side of the midpoint.</p>')
    parts.append(
        f'<section><div class="eyebrow">Prices or behaviour'
        f'<span>{esc(split_note)}</span></div>'
        f'<h2>Is it costing more, or are you buying more</h2>'
        f'<p class="lede">The change in each category splits exactly into '
        f'visits &times; the change in cost per visit, plus the change in visits '
        f'&times; the old cost per visit. The cross term goes to the price side, '
        f'the usual convention.</p>{body}'
        f'<p class="note"><b>Where this stops.</b> A statement line is a total, '
        f'never a list of items. If the weekly shop goes from $40 to $52 there is '
        f'nothing in this data to say whether that is the same trolley at new '
        f'prices or old prices and more in the trolley. Cost per visit is the best '
        f'proxy available and it is a proxy. The visits column is solid; the price '
        f'column is an upper bound on how much of the change was really price.</p>'
        f'</section>')

    # -- benchmark --
    if bm is None:
        body = (f'<p class="note"><b>No income given.</b> The Consumer Expenditure '
                f'Survey publishes by income quintile and the brackets are wide: the '
                f'middle fifth runs {esc(money(ref.QUINTILES[3]["lower"]))} to '
                f'{esc(money(ref.QUINTILES[3]["upper"]))}. Guessing the bracket would '
                f'decide the answer, so it is refused. Re-run with '
                f'<code>--income</code>.</p>')
        sub = "not requested"
    else:
        src = ref.SOURCE["ce"]
        sub = f'{bm.label}, CE {src["survey_year"]}'
        if bm.rows:
            rows = "".join(
                f'<tr><td>{esc(r.group)}<div class="sub">'
                f'{esc(", ".join(r.categories))}</div></td>'
                f'<td class="mono">{esc(money(r.user_annual, 0))}</td>'
                f'<td class="mono">{esc(money(r.national, 0))}'
                f'{"*" if r.unreliable else ""}</td>'
                f'{_td((r.ratio or 1) - 1)}{r.ratio:.2f}x</td></tr>'
                for r in bm.rows)
            body = ('<div class="scroll"><table><thead><tr><th>Group</th>'
                    '<th>You, annualised</th><th>National average</th>'
                    f'<th>Ratio</th></tr></thead><tbody>{rows}</tbody>'
                    '</table></div>')
            loud = [r for r in bm.rows if r.ratio and r.ratio >= 1.5]
            for r in loud:
                body += (f'<p class="note"><b>{r.ratio:.1f}x.</b> You spend '
                         f'{r.ratio:.1f} times the national average for your income '
                         f'bracket on {esc(r.group)} '
                         f'({esc(", ".join(r.categories))}).</p>')
            if any(r.unreliable for r in bm.rows):
                body += ('<p class="note">* BLS flags a component of that row as '
                         'having a relative standard error of 25% or more and '
                         'considers the estimate unreliable.</p>')
        else:
            body = ('<p class="note hard"><b>Nothing to compare.</b> No spending '
                    'landed in a category with a Consumer Expenditure Survey '
                    'counterpart.</p>')
        if bm.unmapped:
            rows = "".join(
                f'<tr><td>{esc(cat)}</td><td class="mono">{esc(money(amt, 0))}</td>'
                f'<td class="sub">{esc(ref.UNMAPPED.get(cat, "no CE counterpart"))}'
                f'</td></tr>' for cat, amt in list(bm.unmapped.items())[:10])
            body += (f'<p class="lede">Outside the comparison: '
                     f'{esc(money(bm.unmapped_total, 0))} a year against '
                     f'{esc(money(bm.mapped_total, 0))} inside it.</p>'
                     '<div class="scroll"><table><thead><tr><th>Category</th>'
                     '<th>Annualised</th><th>Why it is out</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div>')
        body += (f'<p class="sub">{esc(src["survey"])} {src["survey_year"]}, '
                 f'{esc(src["table"])}, released {src["released"]} '
                 f'({src["release_id"]}). Your side is {esc(bm.note)}.</p>')
    parts.append(f'<section><div class="eyebrow">National benchmark'
                 f'<span>{esc(sub)}</span></div>'
                 f'<h2>Against households on your income</h2>{body}</section>')

    caveats = "".join(f"<li>{esc(c)}</li>" for c in ref.CAVEATS)
    parts.append('<section><div class="eyebrow">Before quoting any of this</div>'
                 f'<ul class="caveats">{caveats}</ul></section>')

    meta = [f"<b>{len(engine.transactions)}</b> transactions"]
    if rng:
        meta.append(f"<b>{rng[0]}</b> to <b>{rng[1]}</b>")
        meta.append(f"<b>{len(engine.complete_months())}</b> complete months")
    meta.append(f"<b>{esc(money(engine.total_spend(), 2))}</b> spend")
    trust = ""
    if cov["statements"] and not cov["trusted"]:
        why = []
        if cov["mismatched"]:
            why.append(f'{cov["mismatched"]} statement(s) do not reconcile against '
                       f'their own totals')
        if cov["unchecked"]:
            why.append(f'{cov["unchecked"]} statement(s) could not be checked')
        if cov["gaps"]:
            worst = cov["gaps"][0]
            why.append(f'{len(cov["gaps"])} gap(s) between statement periods, the '
                       f'longest {worst["days"]} days on {esc(worst["account"])}')
        trust = (f'<p class="note hard"><b>Source data is not fully verified.</b> '
                 f'{cov["reconciled"]} of {cov["statements"]} statements reconcile '
                 f'({cov["pct"]:.0f}%), but {"; ".join(why)}. A gap is missing '
                 f'spending, and missing spending changes every weight on this '
                 f'page.</p>')

    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>Personal inflation</title><style>{CSS}</style></head><body>'
           f'<div class="sheet"><header class="head">'
           f'<div class="kicker">Personal inflation</div>'
           f'<h1>Is it you, or is it prices</h1>'
           f'<div class="headmeta">{"".join(f"<div>{m}</div>" for m in meta)}</div>'
           f'</header>{trust}{"".join(parts)}'
           f'<footer>Generated locally by cpi.py on {date.today():%d %b %Y} from '
           f'{esc(os.path.basename(args.db))}. National figures are frozen in '
           f'reference_data.py: {esc(ref.SOURCE["ce"]["survey"])} '
           f'{ref.SOURCE["ce"]["survey_year"]} and CPI-U through '
           f'{ref.CPI_LATEST_YEAR}, both U.S. Bureau of Labor Statistics. '
           f'Nothing left this machine. Arithmetic on your own statements, not '
           f'financial advice.</footer></div></body></html>')

    with open(path, "w") as fh:
        fh.write(doc)
    print(f"Wrote {path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Your own inflation rate, from your own merchants.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"SQLite store or CSV (default {DEFAULT_DB})")
    ap.add_argument("--income", type=float, default=None,
                    help="annual pre-tax household income, to pick a BLS bracket")
    ap.add_argument("--merchants", action="store_true",
                    help="per-merchant price table only")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write a self-contained HTML page instead of printing")
    ap.add_argument("--min-visits", type=int, default=MIN_VISITS,
                    help=f"visits before a merchant counts (default and floor "
                         f"{MIN_VISITS}; two windows of {MIN_WINDOW} is the least "
                         f"that can be tested)")
    ap.add_argument("--min-span", type=int, default=MIN_SPAN_DAYS,
                    help=f"days a merchant must span (default {MIN_SPAN_DAYS})")
    args = ap.parse_args()

    if args.income is not None and args.income <= 0:
        raise SystemExit("--income must be a positive annual figure")
    if args.min_visits < 2 * MIN_WINDOW:
        raise SystemExit(
            f"--min-visits below {2 * MIN_WINDOW} has no effect: each window needs "
            f"{MIN_WINDOW} charges, so {2 * MIN_WINDOW} is the floor. Below that "
            f"the test cannot separate a price change from an unusual order.")

    with Engine.from_source(args.db) as eng:
        if not eng.transactions:
            raise SystemExit(
                f"{args.db} has no transactions. Run:  "
                f"finances parse <PDFs> --db {args.db}")
        if args.output:
            render_html(eng, args, args.output)
        else:
            print_report(eng, args)


if __name__ == "__main__":
    main()
