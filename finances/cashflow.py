#!/usr/bin/env python3
"""
cashflow.py — what the balance does next, not what it did last quarter.

Every other tool in this toolchain reports the past. analyze.py tells you where
the money went, financial_auditor.py tells you what it cost you, planner.py
tells you when a goal lands. None of them answer the question that actually
wakes people up: does the account survive until payday.

This module projects the daily checking balance forward from what is already
known — the closing balance on the most recent statement, the recurring charges
detect_recurring() found, the paycheck cadence in the income rows, and the
everyday variable spend that is whatever is left over — and prints a calendar
with the low-water mark marked.

    finances cashflow --db finances.db --days 60
    finances cashflow --balance 2400 --days 90 --danger 500
    finances cashflow -o cashflow.html

It is a projection, so it is rendered as a band rather than a line. The width of
that band comes from real measurements: each recurring charge carries a
confidence from the detector, each paycheck carries the spread of the deposits
actually seen, and everyday spend carries its month-to-month variance. If the
statements have not been reconciled, that is said out loud before any number is
printed. Arithmetic on your own statements — not financial advice.
"""

from __future__ import annotations

import argparse
import calendar
import html
import os
import statistics as stats
import sys
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


from finances.database import DEFAULT_DB                                  # noqa: E402
from finances.engine import Engine                                        # noqa: E402
from finances.models import Statement                                     # noqa: E402
from finances.utils import Recurring, fmt_money as money, merchant_key    # noqa: E402

DAY = timedelta(days=1)
AVG_MONTH = 365.25 / 12.0          # days, so a monthly figure becomes a daily rate

# Filenames and issuers that mean "deposit account" rather than "credit card".
# A credit card statement's new_balance is money owed; using it as a checking
# balance would be exactly backwards, so the match has to be deliberate.
CHECKING_HINTS = ("checking", "chequing", "current account", "debit",
                  "deposit", "savings", "bank")

_STEP_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
_STEP_DAYS = {"weekly": 7, "biweekly": 14}


# --------------------------------------------------------------------------
# Small date helpers
# --------------------------------------------------------------------------

def add_months(d: date, n: int, day: int = 0) -> date:
    """Shift by whole months, landing on ``day`` where the month is long enough.

    Anchoring on the billing day rather than on the previous occurrence stops a
    charge that fell on the 31st from drifting permanently onto the 28th after
    one pass through February.
    """
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day or d.day, last))


def month_span(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def to_friday(d: date) -> date:
    """Payroll on a fixed calendar day pays early when the day is a weekend."""
    if d.weekday() == 5:
        return d - DAY
    if d.weekday() == 6:
        return d - 2 * DAY
    return d


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def dollars(v: float, dp: int = 0) -> str:
    """Money with the minus outside the dollar sign, so -$40 reads as a debt."""
    return ("-" if v < 0 else "") + money(abs(v), dp)


def wrap(text: str, indent: str = "  ", width: int = 76) -> str:
    return textwrap.fill(text, width=width, initial_indent=indent,
                         subsequent_indent=indent + "  ")


# --------------------------------------------------------------------------
# Starting balance
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Balance:
    """Where the projection starts, and how much we trust the number."""
    amount: float
    as_of: date
    source: str                  # human description of where it came from
    rolled: int = 0              # transactions applied to bring it up to date
    rolled_amount: float = 0.0


def looks_like_checking(s: Statement) -> str:
    """Classify a statement as a deposit account rather than a card.

    Returns ``"named"`` when the filename says so, ``"inferred"`` when only the
    absence of card fields suggests it, and ``""`` otherwise. The two are worth
    telling apart: an inference this weak has to be double-checked against the
    rest of the account's statements before a balance is trusted, because
    reading a card balance as cash gets the sign exactly backwards.
    """
    src = (s.source_file or "").lower()
    if any(h in src for h in CHECKING_HINTS):
        return "" if ("card" in src or "credit" in src) else "named"
    if (s.credit_limit is None and s.purchase_apr is None
            and s.minimum_payment is None and s.new_balance is not None):
        return "inferred"
    return ""


def statement_end(s: Statement, engine: Engine) -> date | None:
    """The date a statement's closing balance is true as of."""
    for raw in (s.period_end, s.statement_period.split(" to ")[-1].strip()):
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
    # The period was never parsed out. The last transaction on that statement is
    # the best available stand-in and is never later than the true period end.
    ds = [t.d for t in engine.transactions
          if t.source_file == s.source_file and t.d]
    return max(ds) if ds else None


def find_balance(engine: Engine, as_of: date,
                 override: float | None = None) -> tuple[Balance | None, list[str]]:
    """Locate the most recent checking balance, or explain why there isn't one.

    Returns ``(balance, notes)``. A None balance is not an error the caller can
    paper over: without it there is nothing to project, so main() prints the
    notes and stops rather than quietly assuming zero.
    """
    notes: list[str] = []
    if override is not None:
        return Balance(override, as_of, "passed with --balance"), notes

    stmts = engine.statements()
    # An account that prints a credit limit, an APR or a minimum payment on any
    # of its statements is a card, whatever a single quiet statement looks like.
    cards = {(s.issuer, s.account_last4) for s in stmts
             if s.credit_limit is not None or s.purchase_apr is not None
             or s.minimum_payment is not None}
    checking = [s for s in stmts
                if (kind := looks_like_checking(s))
                and (kind == "named" or (s.issuer, s.account_last4) not in cards)]
    if not checking:
        notes.append(
            f"No checking or deposit statements found among {plural(len(stmts), 'statement')}. "
            "Everything in the store looks like a credit card, and a card's balance "
            "is money owed, not money available.")
        return None, notes

    dated = [(statement_end(s, engine), s) for s in checking]
    with_bal = [(d, s) for d, s in dated if s.new_balance is not None and d is not None]
    if not with_bal:
        files = sorted({s.source_file for s in checking})[:4]
        notes.append(
            f"Found {plural(len(checking), 'checking statement')} "
            f"({', '.join(files)}{', ...' if len(checking) > len(files) else ''}) "
            "but not one of them printed a closing balance the parser could read.")
        return None, notes

    d, s = max(with_bal, key=lambda ds: ds[0])
    label = f"{s.issuer} ····{s.account_last4}".strip()
    bal = Balance(float(s.new_balance), d,
                  f"{label} closing balance, {s.source_file}")

    # Roll forward with whatever is already known to have happened since the
    # statement closed. Without this the projection starts weeks stale.
    later = [t for t in engine.transactions
             if t.issuer == s.issuer and t.account_last4 == s.account_last4
             and t.d and d < t.d <= as_of]
    if later:
        moved = sum(t.amount for t in later)
        bal.amount -= moved                       # OUT is positive, so it debits
        bal.rolled, bal.rolled_amount = len(later), moved
    return bal, notes


# --------------------------------------------------------------------------
# Income
# --------------------------------------------------------------------------

@dataclass(slots=True)
class IncomeStream:
    """One paycheck, with the shape of its cadence made explicit.

    Semi-monthly and biweekly are not the same thing and the difference is worth
    money: biweekly pays 26 times a year and lands three times in some months,
    semi-monthly pays 24 and never does. Guessing wrong puts a phantom paycheck
    in front of a rent day.
    """
    name: str
    cadence: str                          # weekly|biweekly|semimonthly|monthly|irregular
    amount: float                         # median deposit
    spread: float                         # stdev of deposits
    n: int
    first_seen: date
    last_seen: date
    days_of_month: tuple[int, ...] = ()   # semimonthly / monthly paydays
    gap: int = 0                          # weekly / biweekly step, in days
    confidence: float = 0.0

    @property
    def per_year(self) -> float:
        return {"weekly": 52.0, "biweekly": 26.0, "semimonthly": 24.0,
                "monthly": 12.0, "irregular": 12.0}[self.cadence] * self.amount

    @property
    def per_month(self) -> float:
        return self.per_year / 12.0

    def occurrences(self, start: date, end: date) -> list[date]:
        """Every payday in [start, end], projected on this stream's own cadence."""
        out: list[date] = []
        if self.cadence in _STEP_DAYS or self.cadence == "biweekly":
            step = timedelta(days=self.gap or _STEP_DAYS.get(self.cadence, 14))
            # The series is periodic in both directions, so walk to whichever
            # side of last_seen the window is on. It can be behind: a store with
            # future-dated rows (the known year-rollover bug) leaves last_seen
            # past the end of the window, and stepping only forward finds
            # nothing at all.
            d = self.last_seen
            while d - step >= start:
                d -= step
            while d < start:
                d += step
            while d <= end:
                out.append(d)
                d += step
            return out

        days = self.days_of_month or (self.last_seen.day,)
        cur = date(start.year, start.month, 1)
        while cur <= end:
            last = calendar.monthrange(cur.year, cur.month)[1]
            for dom in days:
                pay = to_friday(date(cur.year, cur.month, min(dom, last)))
                if start <= pay <= end:
                    out.append(pay)
            cur = add_months(cur, 1, day=1)
        return sorted(out)


def _cluster_days(days: list[int], tol: int = 3) -> list[list[int]]:
    """Group days-of-month that sit within ``tol`` of each other."""
    out: list[list[int]] = []
    for d in sorted(days):
        if out and d - out[-1][-1] <= tol:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def detect_income(engine: Engine, min_occurrences: int = 3) -> list[IncomeStream]:
    """Work out the paycheck cadence the same way recurring charges are found.

    Income rows are negative under the house sign convention; everything below
    works in positive dollars-in and flips the sign once, here.
    """
    groups: dict[str, list] = defaultdict(list)
    for t in engine.income():
        if t.d:
            groups[merchant_key(t.merchant)].append((t.d, -t.amount, t.merchant))

    streams: list[IncomeStream] = []
    for rows in groups.values():
        rows.sort(key=lambda r: r[0])
        ds = [r[0] for r in rows]
        amts = [r[1] for r in rows if r[1] > 0]
        if len(rows) < min_occurrences or len(amts) < min_occurrences:
            continue
        if (ds[-1] - ds[0]).days < 25:
            continue

        gaps = [g for g in ((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if g > 0]
        if not gaps:
            continue
        med_gap = stats.median(gaps)
        doms = [d.day for d in ds]
        clusters = _cluster_days(doms)
        per_month = len(ds) / max(month_span(ds[0], ds[-1]), 1)

        days_of_month: tuple[int, ...] = ()
        gap = 0
        if med_gap <= 9.5:
            cadence, gap = "weekly", 7
        elif 11 <= med_gap <= 18:
            # Two tight day-of-month clusters at roughly two deposits a month is
            # semi-monthly. A biweekly cheque walks around the calendar instead,
            # so its days-of-month never settle into two groups.
            if len(clusters) == 2 and 1.6 <= per_month <= 2.4:
                cadence = "semimonthly"
                days_of_month = tuple(sorted(int(stats.median(c)) for c in clusters))
            else:
                cadence, gap = "biweekly", 14
        elif 25 <= med_gap <= 35:
            cadence = "monthly"
            days_of_month = (int(stats.median(doms)),)
        else:
            cadence = "irregular"
            days_of_month = (int(stats.median(doms)),)

        med_amt = stats.median(amts)
        spread = stats.pstdev(amts) if len(amts) > 1 else 0.0
        # Confidence mirrors detect_recurring(): steadiness of the amount and of
        # the cadence, lifted by how many deposits we have actually seen.
        amt_wobble = min(1.0, (spread / med_amt) if med_amt else 1.0)
        gap_wobble = min(1.0, (stats.pstdev(gaps) / med_gap) if med_gap else 1.0)
        if cadence == "irregular":
            gap_wobble = 1.0
        steadiness = 1.0 - (0.5 * amt_wobble + 0.5 * gap_wobble)
        evidence = min(1.0, len(ds) / 6.0)
        conf = round(max(0.0, min(1.0, 0.30 + 0.45 * steadiness + 0.25 * evidence)), 2)

        streams.append(IncomeStream(
            name=max(set(r[2] for r in rows), key=[r[2] for r in rows].count),
            cadence=cadence, amount=med_amt, spread=spread, n=len(ds),
            first_seen=ds[0], last_seen=ds[-1],
            days_of_month=days_of_month, gap=gap, confidence=conf))

    streams.sort(key=lambda s: -s.per_year)
    return streams


# --------------------------------------------------------------------------
# Everyday variable spend
# --------------------------------------------------------------------------

@dataclass(slots=True)
class VariableSpend:
    """The part of a month that isn't a subscription: coffee, groceries, petrol.

    Modelled as a monthly figure spread across the week by the observed
    day-of-week distribution, because spending is not flat — analyze.py's
    find_patterns() already establishes that weekends carry more of it.
    """
    monthly: float
    sigma_frac: float                    # month-to-month variation, as a fraction
    weights: tuple[float, ...]           # 7 weekday shares, summing to 1
    n: int                               # transactions the shares came from
    observed_months: int = 0

    def on(self, d: date) -> float:
        """Expected everyday spend on one particular day."""
        return self.monthly / AVG_MONTH * self.weights[d.weekday()] * 7.0

    @property
    def weekend_share(self) -> float:
        return sum(self.weights[4:]) * 100.0


def build_variable(engine: Engine, recurring: list[Recurring]) -> VariableSpend:
    keys = {r.key for r in recurring}
    everyday = [t for t in engine.spend()
                if t.d and merchant_key(t.merchant) not in keys]

    # Subtract exactly the charges that get projected as named events, not
    # Engine.committed_monthly() — that one drops interest, because interest is
    # a consequence of a balance rather than a subscription you can cancel. It
    # still leaves the account every month, so a cash-flow projection has to
    # carry it, and it must be netted off here or it is counted twice.
    committed = sum(r.per_month for r in recurring)
    monthly = max(0.0, engine.monthly_average() - committed)

    by_dow: dict[int, float] = defaultdict(float)
    for t in everyday:
        by_dow[t.d.weekday()] += t.amount
    total = sum(by_dow.values())
    n = len(everyday)
    # Shrink the observed distribution toward flat in proportion to how little
    # evidence there is. Six Thursdays are not a weekday profile.
    lam = n / (n + 50.0) if n else 0.0
    raw = [lam * ((by_dow[i] / total) if total else 1 / 7) + (1 - lam) / 7
           for i in range(7)]
    s = sum(raw) or 1.0
    weights = tuple(w / s for w in raw)

    complete = sorted(set(engine.complete_months()))
    per_month: dict[str, float] = defaultdict(float)
    for t in everyday:
        if t.month in complete:
            per_month[t.month] += t.amount
    vals = [per_month.get(m, 0.0) for m in complete]
    mean = sum(vals) / len(vals) if vals else 0.0
    sd = stats.pstdev(vals) if len(vals) > 1 else 0.0
    # One month of history is no measurement of month-to-month variance. Half is
    # a wide, honest default; a confident zero would be a lie.
    sigma = min(1.0, sd / mean) if (mean and len(vals) > 1) else 0.5

    return VariableSpend(monthly=monthly, sigma_frac=sigma, weights=weights,
                         n=n, observed_months=len(vals))


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Event:
    """One named thing landing on one day. OUT positive, IN negative."""
    day: date
    name: str
    amount: float
    kind: str                    # "recurring" | "income"
    confidence: float = 0.5
    spread: float = 0.0          # absolute dollar uncertainty, income only

    @property
    def slack(self) -> float:
        """How far this charge is allowed to move, as a fraction of itself.

        A charge the detector is 60% sure of is projected anywhere between 0.4x
        and 1.6x its usual amount. Confidence is the only evidence available
        about how firm a projected charge is, so it is what sets the width.
        """
        return max(0.0, 1.0 - self.confidence)

    @property
    def low(self) -> float:
        """The cheap end: less spent, more received."""
        if self.kind == "income":
            return self.amount - self.spread          # amount is negative: more in
        return self.amount * max(0.0, 1.0 - self.slack)

    @property
    def high(self) -> float:
        """The dear end: more spent, less received."""
        if self.kind == "income":
            return min(0.0, self.amount + self.spread)   # never a negative paycheck
        return self.amount * (1.0 + self.slack)


@dataclass(slots=True)
class Day:
    day: date
    events: list[Event] = field(default_factory=list)
    variable: float = 0.0
    balance: float = 0.0
    lo: float = 0.0
    hi: float = 0.0

    @property
    def named(self) -> list[Event]:
        return sorted(self.events, key=lambda e: (e.kind != "income", -abs(e.amount)))

    @property
    def delta(self) -> float:
        """Net movement, in balance terms: positive means the balance rose."""
        return -(sum(e.amount for e in self.events) + self.variable)


@dataclass(slots=True)
class Projection:
    days: list[Day]
    start: Balance
    recurring: list[Recurring]
    streams: list[IncomeStream]
    variable: VariableSpend
    notes: list[str] = field(default_factory=list)

    @property
    def trough(self) -> Day:
        return min(self.days, key=lambda d: d.balance)

    @property
    def final(self) -> Day:
        return self.days[-1]

    def paydays(self) -> list[Day]:
        return [d for d in self.days if any(e.kind == "income" for e in d.events)]

    def next_payday_after(self, d: date) -> date | None:
        return next((p.day for p in self.paydays() if p.day > d), None)

    @property
    def usual_paydays(self) -> int:
        """Paychecks in an ordinary month, summed over every stream.

        Biweekly pays 26 times a year against twelve months, so two months in
        every year carry a third cheque. Naming which ones is the whole point of
        keeping biweekly and semi-monthly apart.
        """
        base = {"weekly": 4, "biweekly": 2, "semimonthly": 2,
                "monthly": 1, "irregular": 1}
        return sum(base.get(s.cadence, 1) for s in self.streams)


def _paydays_per_month(p: Projection) -> dict[str, int]:
    """Paycheck count for each month the window covers end to end.

    Months clipped by the edge of the window are excluded: they would always
    look short and every projection would claim a missing paycheck.
    """
    first, last = p.days[0].day, p.days[-1].day
    counts: dict[str, int] = defaultdict(int)
    for d in p.paydays():
        counts[f"{d.day:%Y-%m}"] += 1
    out = {}
    for m, k in counts.items():
        y, mo = int(m[:4]), int(m[5:7])
        if date(y, mo, 1) >= first and date(y, mo, calendar.monthrange(y, mo)[1]) <= last:
            out[m] = k
    return dict(sorted(out.items()))


def recurring_occurrences(r: Recurring, start: date, end: date) -> list[date]:
    """Project one recurring charge forward on its own cadence.

    Charges whose next_expected has already slipped into the past are rolled
    forward rather than dropped: an overdue subscription is usually a charge
    that is about to land, not one that vanished. The staleness is reported
    separately so the reader can decide.
    """
    step_m = _STEP_MONTHS.get(r.cadence)
    step_d = _STEP_DAYS.get(r.cadence)
    if step_m is None and step_d is None:
        step_m = 1

    d = r.next_expected or r.last_seen
    if d is None:
        return []
    guard = 0
    while d < start and guard < 600:
        d = add_months(d, step_m, r.day_of_month) if step_m else d + timedelta(days=step_d)
        guard += 1
    out: list[date] = []
    while d <= end and len(out) < 400:
        out.append(d)
        d = add_months(d, step_m, r.day_of_month) if step_m else d + timedelta(days=step_d)
    return out


def project(balance: Balance, anchor: date, days: int,
            recurring: list[Recurring], streams: list[IncomeStream],
            variable: VariableSpend, with_income: bool = True) -> list[Day]:
    """Walk the balance forward one day at a time, carrying three paths.

    The middle path is the best estimate. The two outer paths accumulate the
    pessimistic and optimistic end of every single assumption, which is why the
    band widens with distance — that is the honest shape of a forecast.
    """
    end = anchor + timedelta(days=days - 1)
    cells = {anchor + timedelta(days=i): Day(anchor + timedelta(days=i))
             for i in range(days)}

    for r in recurring:
        for d in recurring_occurrences(r, anchor, end):
            cells[d].events.append(
                Event(d, r.name, r.amount, "recurring", confidence=r.confidence))

    if with_income:
        for s in streams:
            for d in s.occurrences(anchor, end):
                cells[d].events.append(
                    Event(d, s.name, -s.amount, "income",
                          confidence=s.confidence, spread=s.spread))

    bal = lo = hi = balance.amount
    out: list[Day] = []
    for i in range(days):
        cell = cells[anchor + timedelta(days=i)]
        cell.variable = variable.on(cell.day)

        mid_out = sum(e.amount for e in cell.events) + cell.variable
        low_out = (sum(e.low for e in cell.events)
                   + cell.variable * (1.0 - variable.sigma_frac))
        high_out = (sum(e.high for e in cell.events)
                    + cell.variable * (1.0 + variable.sigma_frac))

        bal -= mid_out
        hi -= low_out                 # least spent, most received
        lo -= high_out                # most spent, least received
        cell.balance, cell.lo, cell.hi = bal, lo, hi
        out.append(cell)
    return out


def runway_days(balance: Balance, anchor: date, recurring: list[Recurring],
                variable: VariableSpend) -> tuple[int | None, date | None]:
    """How long the balance lasts if every paycheck stopped today.

    Projected over a year; beyond that the answer stops meaning anything and
    None is returned instead of a made-up number.
    """
    if balance.amount <= 0:
        return 0, anchor
    walk = project(balance, anchor, 366, recurring, [], variable, with_income=False)
    for i, d in enumerate(walk):
        if d.balance <= 0:
            return i + 1, d.day
    return None, None


def danger_episodes(days: list[Day], threshold: float) -> list[dict]:
    """Group consecutive days below the threshold, and blame the right charges.

    A dip is caused by what landed on the way down, not only by what landed on
    the day itself, so each episode collects the named charges from the last day
    the balance was clear through to the trough.
    """
    episodes: list[dict] = []
    run: list[Day] = []
    prev_clear: Day | None = None
    for d in days:
        if d.balance < threshold:
            run.append(d)
        else:
            if run:
                episodes.append({"days": run, "since": prev_clear})
                run = []
            prev_clear = d
    if run:
        episodes.append({"days": run, "since": prev_clear})

    for ep in episodes:
        run = ep["days"]
        trough = min(run, key=lambda d: d.balance)
        first = run[0]
        window_start = ep["since"].day if ep["since"] else first.day
        charges = [e for d in run + ([ep["since"]] if ep["since"] else [])
                   for e in d.events
                   if e.kind == "recurring" and window_start <= d.day <= trough.day]
        ep.update(start=first.day, end=run[-1].day, trough=trough,
                  charges=sorted(charges, key=lambda e: -e.amount))
    return episodes


# --------------------------------------------------------------------------
# Terminal rendering
# --------------------------------------------------------------------------

WIDTH = 78


def rule(width: int = WIDTH, ch: str = "─") -> str:
    return ch * width


def header(title: str, subtitle: str = "") -> str:
    out = ["", "╔" + "═" * (WIDTH - 2) + "╗",
           "║ " + title.ljust(WIDTH - 4) + " ║"]
    if subtitle:
        out.append("║ " + subtitle.ljust(WIDTH - 4) + " ║")
    out.append("╚" + "═" * (WIDTH - 2) + "╝")
    return "\n".join(out)


def section(title: str) -> str:
    return f"\n  ── {title.upper()} " + "─" * max(0, WIDTH - 8 - len(title))


def bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "·" * (width - filled)


def signed(v: float) -> str:
    return ("+" if v >= 0 else "-") + money(abs(v))


def per_mo(v: float, dp: int = 2) -> str:
    """A monthly rate, right-aligned in the same column as a plain amount."""
    return f"{dollars(v, dp)}/mo"


def charge_label(cell: Day, limit: int = 30) -> str:
    named = cell.named
    if not named:
        return ""
    first = named[0]
    tag = f"{first.name[:22]}  {money(abs(first.amount))}"
    if len(named) > 1:
        tag += f"  +{len(named) - 1}"
    return tag[:limit]


def print_coverage(cov: dict) -> None:
    print(section("data check"))
    if cov["statements"] == 0:
        print("  No statements in the store, so nothing here has been checked against a")
        print("  printed total. The projection below is only as good as the parser.")
        return
    print(f"  {cov['statements']} statements · {cov['reconciled']} reconciled · "
          f"{cov['mismatched']} mismatched · {cov['unchecked']} unchecked "
          f"({cov['pct']:.0f}% verified)")
    if cov["trusted"]:
        print("  Every statement agrees with its own printed totals.")
        return
    print("  This data is NOT reconciled. The projection inherits whatever the parser")
    print("  got wrong, so read the shape of the curve, not the cents.")
    for m in cov["mismatches"][:3]:
        print(f"    {m.source_file}: extracted charges off by "
              f"{dollars(abs(m.reconcile_diff or 0), 2)}")
    for g in cov["gaps"][:3]:
        print(f"    {g['account']}: {g['days']}-day gap between "
              f"{g['after']} and {g['before']}")


def print_summary(p: Projection, threshold: float, anchor: date, days: int) -> None:
    low = p.trough
    print(section("the answer"))

    when = f"{low.day:%a %d %b}"
    tail = ""
    nxt = p.next_payday_after(low.day)
    if nxt:
        gap = (nxt - low.day).days
        tail = f", {plural(gap, 'day')} before payday" if gap <= 10 else \
               f", next paycheck {nxt:%d %b}"
    print(f"  Lowest projected balance: {dollars(low.balance)} on {when}{tail}.")
    print(f"  Plausible range that day: {dollars(low.lo)} to {dollars(low.hi)}.")

    n_days, out_on = runway_days(p.start, anchor, p.recurring, p.variable)
    if n_days is None:
        print("  Runway if income stopped today: over a year at this burn rate.")
    elif n_days == 0:
        print("  Runway if income stopped today: none — the balance is already at or "
              "below zero.")
    else:
        print(f"  Runway if income stopped today: {plural(n_days, 'day')} "
              f"(empty {out_on:%d %b %Y}).")

    end = p.final
    move = end.balance - p.start.amount
    verb = "up" if move >= 0 else "down"
    print(f"  Over {plural(days, 'day')}: {dollars(p.start.amount)} → "
          f"{dollars(end.balance)}, {verb} {dollars(abs(move))} "
          f"({dollars(end.lo)} to {dollars(end.hi)}).")

    below = [d for d in p.days if d.balance < threshold]
    if below:
        print(f"  {plural(len(below), 'day')} below {dollars(threshold)}, "
              f"first on {below[0].day:%a %d %b}.")
    else:
        at_risk = [d for d in p.days if d.lo < threshold]
        if at_risk:
            print(wrap(f"Never dips below {dollars(threshold)} on the central "
                       f"estimate, but {plural(len(at_risk), 'day')} do on the "
                       f"pessimistic edge."))
        else:
            print(f"  Never dips below {dollars(threshold)}, on any path.")

    three = [m for m, k in _paydays_per_month(p).items() if k > p.usual_paydays]
    if three and p.streams:
        names = ", ".join(datetime.strptime(m, "%Y-%m").strftime("%B") for m in three)
        print(f"  {names} carries an extra paycheck — "
              f"{p.usual_paydays + 1} instead of {p.usual_paydays}.")


_NOTE_COL = WIDTH - 39


def _line(label: str, value: str, note: str) -> None:
    chunks = textwrap.wrap(note, width=_NOTE_COL) or [""]
    print(f"  {label:<20}{value:>14}   {chunks[0]}")
    for c in chunks[1:]:
        print(f"  {'':<20}{'':>14}   {c}")


def _cont(note: str) -> None:
    """A continuation of the previous assumption line, in the note column."""
    for chunk in textwrap.wrap(note, width=_NOTE_COL) or [""]:
        print(f"  {'':<20}{'':>14}   {chunk}")


def print_assumptions(p: Projection, anchor: date) -> None:
    print(section("what this is built on"))
    b = p.start
    _line("Starting balance", dollars(b.amount, 2), b.source)
    if b.rolled:
        _cont(f"as of {b.as_of:%d %b %Y}, rolled forward through "
              f"{plural(b.rolled, 'known transaction')} "
              f"({dollars(b.rolled_amount, 2)} net out)")
    elif b.as_of != anchor:
        _cont(f"as of {b.as_of:%d %b %Y}, "
              f"{plural((anchor - b.as_of).days, 'day')} stale, nothing known since")

    if p.recurring:
        committed = sum(r.per_month for r in p.recurring)
        _line("Committed charges", per_mo(committed),
              plural(len(p.recurring), "recurring charge"))
        for r in p.recurring[:8]:
            nxt = f"{r.next_expected:%d %b}" if r.next_expected else "unknown"
            stale = "  overdue" if r.next_expected and r.next_expected < anchor else ""
            print(f"      {r.name[:24]:<24} {r.cadence:<11} {dollars(r.amount, 2):>9}  "
                  f"next {nxt}  {r.confidence:.0%}{stale}")
        if len(p.recurring) > 8:
            print(f"      … and {len(p.recurring) - 8} more")
    else:
        _line("Committed charges", "none",
              "detect_recurring() found nothing repeating, so all of the "
              "outflow below is variable spend")

    if p.streams:
        shape = {"semimonthly": "twice a month on fixed days",
                 "biweekly": "every 14 days, so three paychecks in some months",
                 "weekly": "every 7 days",
                 "monthly": "once a month",
                 "irregular": "no steady cadence, treated as monthly"}
        for s in p.streams:
            _line("Income", dollars(s.amount, 2),
                  f"{s.name[:28]} · {s.cadence}, {per_mo(s.per_month, 0)}")
            _cont(f"{shape[s.cadence]}; {plural(s.n, 'deposit')} seen, "
                  f"±{dollars(s.spread, 2)} spread, confidence {s.confidence:.0%}")
    else:
        _line("Income", "none",
              "no Income-category deposits with a readable cadence, so this "
              "projection is a pure burn-down")

    v = p.variable
    _line("Everyday spend", per_mo(v.monthly), "monthly average minus committed")
    _cont(f"±{v.sigma_frac * 100:.0f}% month to month over "
          f"{plural(v.observed_months, 'complete month')}")
    _cont(f"{v.weekend_share:.0f}% of it lands Fri–Sun ({plural(v.n, 'charge')})")

    for n in p.notes:
        _line("Note", "", n)


def print_calendar(p: Projection, threshold: float) -> None:
    # The bar tracks the central estimate only. Scaling it to the full band
    # would squash the line everyone actually reads into two characters.
    bals = [d.balance for d in p.days] + [p.start.amount]
    floor = min(0.0, min(bals))
    ceiling = max(max(bals), floor + 1.0)
    span = ceiling - floor

    trough = p.trough
    print(section(f"calendar · {len(p.days)} days"))
    print("     DATE          WHAT LANDS THAT DAY            CHANGE      BALANCE")
    print("  " + rule(WIDTH - 2))

    month = None
    for d in p.days:
        if month is not None and d.day.month != month:
            label = f"{d.day:%B %Y}"
            print(f"  {'':<12}── {label} " + "─" * max(0, WIDTH - 20 - len(label)))
        month = d.day.month

        flag = "!" if d.balance < threshold else " "
        mark = ""
        if d.day == trough.day:
            mark = "  ← low"
        elif any(e.kind == "income" for e in d.events):
            mark = "  ← pay"
        print(f"  {flag}{d.day:%a %d %b}  {charge_label(d):<30} "
              f"{signed(d.delta):>9} {dollars(d.balance):>11}  "
              f"{bar((d.balance - floor) / span)}{mark}")
    print("  " + rule(WIDTH - 2))
    print(f"  Bars span {dollars(floor)} to {dollars(ceiling)}, on the central "
          f"estimate.")
    print(f"  '!' marks a day under {dollars(threshold)}.")


def print_danger(p: Projection, threshold: float) -> None:
    eps = danger_episodes(p.days, threshold)
    print(section(f"danger days · below {dollars(threshold)}"))
    if not eps:
        at_risk = [d for d in p.days if d.lo < threshold]
        print(f"  Nothing dips below {dollars(threshold)} on the central estimate.")
        if at_risk:
            print(wrap(f"The pessimistic edge of the band does, on "
                       f"{plural(len(at_risk), 'day')} starting "
                       f"{at_risk[0].day:%a %d %b}. That is the case where everyday "
                       f"spend runs high and every uncertain charge lands at full "
                       f"price."))
        print()
        return

    for ep in eps:
        t = ep["trough"]
        length = (ep["end"] - ep["start"]).days + 1
        print(f"  {ep['start']:%a %d %b} → {ep['end']:%a %d %b}  "
              f"({plural(length, 'day')}, low {dollars(t.balance)} on {t.day:%d %b})")
        if ep["charges"]:
            print("    pushed there by:")
            for e in ep["charges"][:5]:
                print(f"      {e.day:%d %b}  {e.name[:30]:<30} {dollars(e.amount, 2):>10}")
            rest = len(ep["charges"]) - 5
            if rest > 0:
                print(f"      … and {plural(rest, 'more charge')}")
            daily = p.variable.monthly / AVG_MONTH
            print(f"      plus roughly {dollars(daily, 2)}/day of everyday spend")
        else:
            print("    no named charge landed here — everyday spend alone "
                  "got it there")
        nxt = p.next_payday_after(t.day)
        if nxt:
            print(f"    clears at the next paycheck, {nxt:%a %d %b} "
                  f"({plural((nxt - t.day).days, 'day')} after the low)")
        else:
            print("    no projected paycheck after this — nothing brings it back up")
        print()


def print_footer(p: Projection) -> None:
    print(section("how to read the band"))
    print("  The middle line is the best estimate. The range around it accumulates the")
    print("  pessimistic and optimistic end of every assumption, so it widens with time:")
    print("    · each recurring charge varies by (1 - its confidence), so a 60%-confident")
    print("      charge is projected anywhere from 0.4x to 1.6x its usual amount")
    print("    · each paycheck varies by the standard deviation of the deposits seen")
    print(f"    · everyday spend varies by ±{p.variable.sigma_frac * 100:.0f}%, "
          "its measured month-to-month spread")
    print("  It is not a confidence interval and nothing here is a guarantee. One")
    print("  unbudgeted purchase moves the line further than any of the above.")
    print()


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def esc(s) -> str:
    return html.escape(str(s))


def svg_chart(p: Projection, threshold: float, w: int = 980, h: int = 260) -> str:
    """Inline SVG: the band, the central line, the threshold and the low point."""
    days = p.days
    n = len(days)
    if n < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 8, 66, 16, 26
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b

    ys = [d.lo for d in days] + [d.hi for d in days] + [p.start.amount, threshold, 0.0]
    ymin, ymax = min(ys), max(ys)
    if ymax - ymin < 1:
        ymax = ymin + 1
    pad = (ymax - ymin) * 0.08
    ymin, ymax = ymin - pad, ymax + pad

    def X(i: int) -> float:
        return pad_l + i * pw / (n - 1)

    def Y(v: float) -> float:
        return pad_t + (ymax - v) / (ymax - ymin) * ph

    band = (" ".join(f"{X(i):.1f},{Y(d.hi):.1f}" for i, d in enumerate(days))
            + " " + " ".join(f"{X(i):.1f},{Y(d.lo):.1f}"
                             for i, d in reversed(list(enumerate(days)))))
    line = " ".join(f"{X(i):.1f},{Y(d.balance):.1f}" for i, d in enumerate(days))

    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" '
             f'aria-label="Projected daily balance">',
             f'<polygon class="band" points="{band}"/>']

    if ymin < 0 < ymax:
        parts.append(f'<line class="zero" x1="{pad_l}" y1="{Y(0):.1f}" '
                     f'x2="{pad_l + pw}" y2="{Y(0):.1f}"/>')
    thresh_y = None
    if ymin < threshold < ymax:
        thresh_y = Y(threshold)
        parts.append(f'<line class="thresh" x1="{pad_l}" y1="{thresh_y:.1f}" '
                     f'x2="{pad_l + pw}" y2="{thresh_y:.1f}"/>'
                     f'<text class="lab" x="{pad_l + pw + 5}" '
                     f'y="{thresh_y + 4:.1f}">{esc(dollars(threshold))}</text>')

    parts.append(f'<polyline class="mid" points="{line}"/>')

    for i, d in enumerate(days):
        if any(e.kind == "income" for e in d.events):
            parts.append(f'<line class="pay" x1="{X(i):.1f}" y1="{pad_t + ph}" '
                         f'x2="{X(i):.1f}" y2="{pad_t + ph - 7}"><title>'
                         f'paycheck {d.day:%d %b}</title></line>')

    lo_i = min(range(n), key=lambda i: days[i].balance)
    lo_d = days[lo_i]
    parts.append(f'<circle class="low" cx="{X(lo_i):.1f}" cy="{Y(lo_d.balance):.1f}" r="4">'
                 f'<title>low {esc(dollars(lo_d.balance))} on {lo_d.day:%d %b}</title></circle>')
    anchor = "end" if lo_i > n * 0.7 else "start"
    dx = -8 if anchor == "end" else 8
    parts.append(f'<text class="lowlab" x="{X(lo_i) + dx:.1f}" '
                 f'y="{Y(lo_d.balance) + 18:.1f}" text-anchor="{anchor}">'
                 f'low {esc(dollars(lo_d.balance))} · {lo_d.day:%d %b}</text>')

    seen = set()
    for i, d in enumerate(days):
        key = (d.day.year, d.day.month)
        if key in seen:
            continue
        seen.add(key)
        # Nudge the first label inward so a centred "Jul" is not half cut off.
        parts.append(f'<text class="xlab" x="{max(X(i), pad_l + 12):.1f}" y="{h - 8}">'
                     f'{d.day:%b}</text>')

    # The closing-balance label sits at the right edge; nudge it clear of the
    # threshold label rather than letting the two print on top of each other.
    end_y = Y(days[-1].balance) + 4
    if thresh_y is not None and abs(end_y - (thresh_y + 4)) < 11:
        end_y += 12 if days[-1].balance <= threshold else -12
    parts.append(f'<text class="lab" x="{pad_l + pw + 5}" y="{end_y:.1f}">'
                 f'{esc(dollars(days[-1].balance))}</text></svg>')
    return "".join(parts)


CSS = """
:root{--paper:#FBFBF7;--band:#E7EFE3;--ink:#14211B;--soft:#5C6D62;--rule:#C2D1BC;
--rule-strong:#8FA88A;--green:#2E6A4C;--red:#A3372A;--amber:#9C7318;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:#DCE3D8;color:var(--ink);font-family:var(--ui);
font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.sheet{max-width:1000px;margin:26px auto;background:var(--paper);
border:1px solid var(--rule-strong);padding:34px 40px 40px;
box-shadow:0 12px 34px rgba(20,33,27,.10)}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
text-transform:uppercase;color:var(--soft)}
h1{font-family:var(--display);font-weight:400;font-size:34px;margin:6px 0 8px}
h2{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
color:var(--soft);margin:32px 0 10px;border-bottom:1px solid var(--rule);padding-bottom:6px}
.lede{font-family:var(--display);font-size:19px;margin:0 0 20px;max-width:66ch}
.lede b{font-weight:600}
.episode{font-family:var(--display);font-size:16.5px;margin:18px 0 6px;max-width:72ch;
border-left:3px solid var(--red);padding-left:12px}
.episode:first-of-type{margin-top:2px}
.episode.clear{border-left-color:var(--green)}
.episode b{font-weight:600}
.episode+ul.plain{margin-left:12px}
.warn{border-left:3px solid var(--amber);background:#FBF4E4;padding:10px 14px;
margin:0 0 22px;font-size:13.5px}
.warn b{color:var(--amber)}
.summary{display:flex;gap:34px;flex-wrap:wrap;border-top:2px solid var(--ink);
border-bottom:1px solid var(--rule);padding:14px 0;margin-bottom:24px}
.summary div{font-family:var(--mono)}
.summary span{display:block;font-size:11px;color:var(--soft);letter-spacing:.1em;
text-transform:uppercase}
.summary b{font-size:22px;font-weight:600}
.summary b.bad{color:var(--red)}
/* Chart styles stay scoped to the svg. Class names like .pay and .low are also
   used on table rows, and an unscoped opacity would quietly fade them. */
.chart{width:100%;height:auto;display:block;margin:6px 0 4px}
.chart .band{fill:rgba(46,106,76,.16);stroke:none}
.chart .mid{fill:none;stroke:var(--green);stroke-width:2}
.chart .thresh{stroke:var(--red);stroke-width:1;stroke-dasharray:4 3}
.chart .zero{stroke:var(--ink);stroke-width:1;opacity:.4}
.chart .pay{stroke:var(--green);stroke-width:2;opacity:.55}
.chart .low{fill:var(--red)}
.chart .lowlab{font-family:var(--mono);font-size:11px;fill:var(--red)}
.chart .lab{font-family:var(--mono);font-size:10.5px;fill:var(--soft)}
.chart .xlab{font-family:var(--mono);font-size:10.5px;fill:var(--soft);text-anchor:middle}
.range{font-family:var(--mono);font-size:10px;color:var(--soft);font-weight:400}
.caption{font-size:12.5px;color:var(--soft);margin:2px 0 0;max-width:84ch}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;
color:var(--soft);text-align:left;font-weight:400;padding:6px 8px;
border-bottom:1px solid var(--rule)}
td{padding:4px 8px;border-bottom:1px solid #EEF1EA;vertical-align:baseline}
td.num,th.num,td.money,th.money{text-align:right;font-family:var(--mono);
font-variant-numeric:tabular-nums}
tr.pay td{background:rgba(46,106,76,.06)}
tr.danger td{background:rgba(163,55,42,.08)}
tr.low td{outline:1px solid var(--red);outline-offset:-1px}
td.day{font-family:var(--mono);white-space:nowrap;color:var(--soft)}
tr.pay td.day,tr.danger td.day{color:var(--ink)}
.track{position:relative;height:8px;background:var(--band);min-width:90px}
.fill{position:absolute;left:0;top:0;bottom:0;background:var(--green);opacity:.55}
tr.danger .fill{background:var(--red)}
.neg{color:var(--red)}
.pos{color:var(--green)}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;
border:1px solid var(--rule-strong);padding:1px 5px;color:var(--soft);margin-left:6px}
ul.plain{margin:0;padding-left:18px;font-size:13.5px;color:var(--soft)}
ul.plain b{color:var(--ink)}
footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft)}
"""


def render_html(p: Projection, threshold: float, anchor: date, cov: dict) -> str:
    low = p.trough
    end = p.final
    n_days, out_on = runway_days(p.start, anchor, p.recurring, p.variable)
    eps = danger_episodes(p.days, threshold)

    nxt = p.next_payday_after(low.day)
    tail = ""
    if nxt:
        gap = (nxt - low.day).days
        tail = (f", {plural(gap, 'day')} before the next paycheck" if gap <= 10
                else f". The next paycheck is {nxt:%d %b}")

    runway = ("over a year" if n_days is None else "none" if n_days == 0
              else plural(n_days, "day"))

    warn = ""
    if not cov["trusted"]:
        warn = (f'<div class="warn"><b>Not reconciled.</b> '
                f'{cov["reconciled"]} of {cov["statements"]} statements have been '
                f'checked against their own printed totals '
                f'({cov["unchecked"]} unchecked, {cov["mismatched"]} mismatched). '
                f'Every figure below inherits whatever the parser got wrong. '
                f'Read the shape of the curve, not the cents.</div>')

    # --- calendar table ---
    lows = [d.lo for d in p.days]
    floor = min(0.0, min(lows))
    ceiling = max([d.hi for d in p.days] + [p.start.amount, floor + 1.0])
    span = ceiling - floor

    rows = []
    month = None
    for d in p.days:
        if d.day.month != month:
            month = d.day.month
            rows.append(f'<tr><th colspan="5">{d.day:%B %Y}</th></tr>')
        cls = []
        if d.balance < threshold:
            cls.append("danger")
        if any(e.kind == "income" for e in d.events):
            cls.append("pay")
        if d.day == low.day:
            cls.append("low")
        what = []
        for e in d.named:
            sign = "+" if e.amount < 0 else "−"
            klass = "pos" if e.amount < 0 else ""
            what.append(f'<span class="{klass}">{esc(e.name[:34])} '
                        f'{sign}{esc(dollars(abs(e.amount), 2))}</span>')
        delta_cls = "pos" if d.delta >= 0 else "neg"
        frac = max(0.0, min(1.0, (d.balance - floor) / span)) * 100
        rows.append(
            f'<tr class="{" ".join(cls)}">'
            f'<td class="day">{d.day:%a %d %b}</td>'
            f'<td>{" · ".join(what)}</td>'
            f'<td class="money {delta_cls}">{esc(signed(d.delta))}</td>'
            f'<td class="money"><b>{esc(dollars(d.balance))}</b>'
            f'<div class="range">{esc(dollars(d.lo))}–{esc(dollars(d.hi))}</div></td>'
            f'<td><div class="track"><div class="fill" style="width:{frac:.1f}%"></div>'
            f'</div></td></tr>')

    # --- assumptions ---
    assume = []
    b = p.start
    src = esc(b.source)
    if b.rolled:
        src += (f' — as of {b.as_of:%d %b %Y}, rolled forward through '
                f'{plural(b.rolled, "known transaction")}')
    elif b.as_of != anchor:
        src += f' — as of {b.as_of:%d %b %Y}, nothing known since'
    assume.append(f'<li><b>Starting balance {esc(dollars(b.amount, 2))}</b> · {src}</li>')

    if p.recurring:
        committed = sum(r.per_year for r in p.recurring) / 12.0
        bits = ", ".join(f'{esc(r.name[:22])} {esc(dollars(r.amount))} {r.cadence} '
                         f'({r.confidence:.0%})' for r in p.recurring[:6])
        assume.append(f'<li><b>{plural(len(p.recurring), "recurring charge")}, '
                      f'{esc(dollars(committed))}/mo</b> · {bits}</li>')
    else:
        assume.append('<li><b>No recurring charges detected.</b> Nothing repeating '
                      'passed detect_recurring(), so the whole projection is variable '
                      'spend.</li>')

    if p.streams:
        for s in p.streams:
            assume.append(
                f'<li><b>Income {esc(dollars(s.amount, 2))} {s.cadence}</b> · '
                f'{esc(s.name[:30])}, {plural(s.n, "deposit")} seen, '
                f'±{esc(dollars(s.spread, 2))} spread, confidence {s.confidence:.0%}'
                + (' · biweekly means three paychecks in some months'
                   if s.cadence == "biweekly" else '') + '</li>')
    else:
        assume.append('<li><b>No income detected.</b> No Income-category deposits with '
                      'a readable cadence, so this is a pure burn-down.</li>')

    v = p.variable
    assume.append(
        f'<li><b>Everyday spend {esc(dollars(v.monthly))}/mo</b> · monthly average minus '
        f'committed, spread across the week by the observed day-of-week profile '
        f'({v.weekend_share:.0f}% falls Fri–Sun). Varies ±{v.sigma_frac * 100:.0f}% '
        f'month to month over {plural(v.observed_months, "complete month")}.</li>')

    # --- danger episodes ---
    danger_html = []
    for ep in eps:
        t = ep["trough"]
        length = (ep["end"] - ep["start"]).days + 1
        charges = "".join(
            f'<li>{e.day:%d %b} — <b>{esc(e.name[:34])}</b> '
            f'{esc(dollars(e.amount, 2))}</li>' for e in ep["charges"][:6])
        after = p.next_payday_after(t.day)
        recover = (f'Clears at the next paycheck on {after:%a %d %b}.' if after
                   else 'No projected paycheck follows — nothing brings it back up.')
        danger_html.append(
            f'<p class="episode">'
            f'<b>{ep["start"]:%a %d %b} → {ep["end"]:%a %d %b}</b> · '
            f'{plural(length, "day")} under {esc(dollars(threshold))}, '
            f'low {esc(dollars(t.balance))} on {t.day:%d %b}. {recover}</p>'
            f'<ul class="plain">{charges or "<li>No named charge — this is everyday spend alone.</li>"}</ul>')
    if not eps:
        at_risk = [d for d in p.days if d.lo < threshold]
        danger_html.append(
            f'<p class="episode clear">Nothing dips below '
            f'{esc(dollars(threshold))} on the central estimate.'
            + (f' The pessimistic edge of the band does, on '
               f'{plural(len(at_risk), "day")} starting {at_risk[0].day:%d %b}.'
               if at_risk else '') + '</p>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cash-flow projection</title><style>{CSS}</style></head><body><div class="sheet">
<div class="kicker">Cash flow · {anchor:%d %b %Y} → {end.day:%d %b %Y}</div>
<h1>Where the balance goes next</h1>
<p class="lede">Lowest projected balance <b>{esc(dollars(low.balance))}</b> on
<b>{low.day:%a %d %b}</b>{tail}. Plausible range that day
{esc(dollars(low.lo))} to {esc(dollars(low.hi))}.</p>
{warn}
<div class="summary">
  <div><span>starting</span><b>{esc(dollars(p.start.amount))}</b></div>
  <div><span>low point</span><b class="{'bad' if low.balance < threshold else ''}">
    {esc(dollars(low.balance))}</b></div>
  <div><span>on</span><b>{low.day:%d %b}</b></div>
  <div><span>after {len(p.days)} days</span><b>{esc(dollars(end.balance))}</b></div>
  <div><span>runway, no income</span><b>{runway}</b></div>
  <div><span>days under {esc(dollars(threshold))}</span>
    <b class="{'bad' if eps else ''}">{sum(len(e['days']) for e in eps)}</b></div>
</div>
<h2>Projected daily balance</h2>
{svg_chart(p, threshold)}
<p class="caption">The shaded band is the range, not an error bar: it accumulates the
pessimistic and optimistic end of every assumption, which is why it widens with
distance. Ticks along the bottom are projected paydays.</p>
<h2>Danger days · below {esc(dollars(threshold))}</h2>
{"".join(danger_html)}
<h2>What this is built on</h2>
<ul class="plain">{"".join(assume)}</ul>
<h2>Day by day</h2>
<table><thead><tr><th>Date</th><th>What lands</th><th class="money">Change</th>
<th class="money">Balance</th><th>&nbsp;</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<footer>Generated locally by cashflow.py on {date.today():%d %b %Y} from
{len(p.days)} projected days. A projection, not a statement: one unbudgeted purchase
moves the line further than any assumption listed above. Arithmetic on your own
statements, not financial advice.</footer>
</div></body></html>"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Project the checking balance forward and say when it goes wrong.",
        epilog="Example:  finances cashflow --db finances.db --days 60 --danger 500")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"SQLite store or transactions CSV (default {DEFAULT_DB})")
    ap.add_argument("--days", type=int, default=60,
                    help="length of the projection window, in days (default 60)")
    ap.add_argument("--balance", type=float, default=None,
                    help="starting balance, overriding whatever the statements say")
    ap.add_argument("--danger", type=float, default=0.0, metavar="BELOW",
                    help="flag every day the balance dips under this (default 0)")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="project from this date instead of today")
    ap.add_argument("-o", "--out", default=None, metavar="FILE",
                    help="also write a self-contained HTML page")
    ap.add_argument("--no-calendar", action="store_true",
                    help="skip the day-by-day listing and print only the summary")
    args = ap.parse_args()

    if not 1 <= args.days <= 366:
        raise SystemExit("--days must be between 1 and 366. 30 to 90 is the useful range.")

    if args.as_of:
        try:
            anchor = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"--as-of {args.as_of!r} is not a YYYY-MM-DD date")
    else:
        anchor = date.today()

    if args.db.lower().endswith(".csv"):
        eng = Engine.from_source(args.db)
    else:
        if not os.path.exists(args.db):
            raise SystemExit(
                f"{args.db} does not exist. Build it with:  "
                f"finances parse <PDFs> --db {args.db}")
        eng = Engine(args.db)

    try:
        # A non-zero status when nothing could be projected, so a shell doing
        # `cashflow.py -o page.html && open page.html` does not open a stale page.
        raise SystemExit(run(eng, args, anchor))
    finally:
        eng.close()


def run(eng: Engine, args, anchor: date) -> int:
    if not eng.transactions:
        print(f"\n  {args.db} has no transactions, so there is nothing to project.")
        print(f"  Run:  finances parse <PDFs> --db {args.db}\n")
        return 1

    cov = eng.coverage()
    print(header("CASH-FLOW CALENDAR",
                 f"{args.days} days from {anchor:%a %d %b %Y}"))
    print_coverage(cov)

    bal, notes = find_balance(eng, anchor, args.balance)
    if bal is None:
        print(section("no starting balance"))
        for n in notes:
            print(wrap(n))
        print()
        print("  A projection with no starting balance is meaningless, and assuming zero")
        print("  would be a lie, so nothing is projected. Pass the number yourself:")
        print()
        print(f"      finances cashflow --db {args.db} --balance 2400 --days {args.days}")
        print()
        print("  Read it off your banking app, or off the last page of the most recent")
        print("  checking statement. Everything else below the balance is already known.")
        if args.out:
            print(f"\n  {args.out} was not written — there is nothing to draw.")
        print()
        return 1

    recurring = eng.recurring()
    streams = detect_income(eng)
    variable = build_variable(eng, recurring)

    notes = list(notes)
    stale = [r for r in recurring if r.next_expected and r.is_stale(anchor)]
    if stale:
        notes.append(
            f"{plural(len(stale), 'recurring charge')} more than 45 days overdue "
            f"({', '.join(r.name[:20] for r in stale[:3])}). Still projected, because "
            "an overdue charge is usually about to land — but it may equally have been "
            "cancelled, or the card on file may have died")
    if variable.monthly <= 0 and eng.monthly_average() > 0:
        notes.append("committed charges already exceed the monthly average, so everyday "
                     "spend is projected at zero")
    if variable.observed_months < 3:
        notes.append(f"only {plural(variable.observed_months, 'complete month')} of "
                     "history, so the variance band is thin evidence")
    ahead = [t.d for t in eng.transactions if t.d and t.d > anchor]
    if ahead:
        notes.append(
            f"{plural(len(ahead), 'transaction')} in the store "
            f"{'is' if len(ahead) == 1 else 'are'} dated after {anchor:%d %b %Y}, the "
            f"latest {max(ahead):%d %b %Y}. Cadences are still projected onto the "
            "window, but future-dated rows usually mean the parser guessed a year wrong")

    days = project(bal, anchor, args.days, recurring, streams, variable)
    p = Projection(days=days, start=bal, recurring=recurring, streams=streams,
                   variable=variable, notes=notes)

    print_summary(p, args.danger, anchor, args.days)
    print_assumptions(p, anchor)
    if not args.no_calendar:
        print_calendar(p, args.danger)
    print_danger(p, args.danger)
    print_footer(p)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(render_html(p, args.danger, anchor, cov))
        print(f"  Wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    main()
