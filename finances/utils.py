"""
Shared helpers used across the whole toolchain.

Historically parse.py, analyze.py, planner.py and financial_auditor.py each
carried their own copy of money/date parsing, merchant-name cleanup and the
debt payoff formula. Those copies drifted over time. This module is the single
source of truth so every tool agrees on what a dollar amount, a date and a
merchant key are.

Everything here is pure and dependency-free (stdlib only) so it is cheap to
import and easy to unit test.
"""

from __future__ import annotations
import calendar
import csv
import hashlib
import math
import re
import statistics as stats
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_MONEY_STRIP = re.compile(r"[^\d.]")
_MONEY_SYMBOLS = re.compile(r"[$€£¥\s,]")


def parse_money(raw) -> float | None:
    """Read a printed money value into a float.

    Handles $, thousands separators, leading and trailing minus and the
    accountant's ``(1,234.56)`` negative. Returns None when there is no number.

    The sign is read after the currency symbol is removed, because statements
    print it on either side of it: ``-$45.00`` and ``$-45.00`` both occur, and
    testing the raw first character sees only the ``$`` in the second case. A
    dropped minus turns a refund into a charge, which is precisely the error
    class reconciliation exists to catch.
    """
    s = str(raw).strip()
    body = _MONEY_SYMBOLS.sub("", s)
    neg = (s.startswith("(") or s.endswith(")")
           or body.startswith("-") or body.endswith("-"))
    digits = _MONEY_STRIP.sub("", body)
    if not digits:
        return None
    try:
        val = float(digits)
    except ValueError:
        return None
    return -val if neg else val


def fmt_money(v, dp: int = 0) -> str:
    """Format a number as ``$1,234`` (or with ``dp`` decimal places)."""
    return f"${v:,.{dp}f}"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_D_NUM = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_D_MDY = re.compile(r"^([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?: (\d{2,4}))?$")
_D_DMY = re.compile(r"^(\d{1,2}) ([A-Za-z]{3})[a-z]*\.?(?: (\d{2,4}))?$")
_WS = re.compile(r"\s+")


def parse_date(raw, default_year: int | None = None) -> date | None:
    """Parse the many date shapes statements use into a ``date``.

    Understands ``05/14``, ``05/14/2024``, ``May 14 2024`` and ``14 May 2024``.
    Two-digit years are assumed to be 2000+. Returns None when the year can't be
    determined and no ``default_year`` is given.
    """
    s = _WS.sub(" ", str(raw).strip().replace(",", " ")).strip()
    m = _D_NUM.match(s)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = _D_MDY.match(s)
        if m:
            mo = MONTHS.get(m.group(1).lower())
            dy, yr = int(m.group(2)), m.group(3)
        else:
            m = _D_DMY.match(s)
            if not m:
                return None
            dy, mo, yr = int(m.group(1)), MONTHS.get(m.group(2).lower()), m.group(3)
    if not mo:
        return None
    if yr:
        yr = int(yr)
        if yr < 100:
            yr += 2000
    else:
        yr = default_year
    if yr is None:
        return None
    try:
        return date(yr, mo, dy)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Merchant names
# --------------------------------------------------------------------------

_MK_PSEUDO = re.compile(r"[A-Z]+_\d+")
_MK_AMZN = re.compile(r"\bAMZN\b|\bAMAZON MKTPL\b")
_MK_NONWORD = re.compile(r"[^A-Z0-9_ ]+")
_MK_STOP = {"POS", "PURCHASE", "PAYMENT", "RECURRING", "INC", "LLC", "CO", "THE",
            "USA", "US", "COM", "STORE", "SQ", "TST", "PY"}


def merchant_key(name: str) -> str:
    """Collapse a raw descriptor to a stable grouping key.

    Two charges from the same place should land on the same key even when the
    statement pads them with store numbers, auth codes or POS prefixes.
    """
    s = name.upper().strip()
    # Pseudonymised input (from anonymize.py) is already a clean key.
    if _MK_PSEUDO.fullmatch(s) or s.startswith("OTHER (") or s.startswith("CARD_"):
        return s
    s = _MK_AMZN.sub("AMAZON", s)
    s = _MK_NONWORD.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    toks = []
    for t in s.split():
        if t in _MK_STOP or len(t) < 2:
            continue
        if t.isdigit() and len(t) >= 3:              # store or reference number
            continue
        if "_" not in t and len(t) >= 8 and sum(ch.isdigit() for ch in t) >= 2:
            continue                                 # authorisation code
        toks.append(t)
    return " ".join(toks[:3]) or s


# --------------------------------------------------------------------------
# Recurring charges
# --------------------------------------------------------------------------
#
# This used to live in three places (analyze.py, financial_auditor.py and
# anonymize.py) with three different thresholds, so the dashboard, the audit
# and the anonymiser each believed in a different set of subscriptions. This is
# the union of the three: the auditor's calendar-month test (which survives a
# billing day that drifts), analyze.py's cadence-regularity and price-drift
# tracking, and a next-due estimate the cash-flow calendar needs.

_CADENCE_PER_YEAR = {"weekly": 52.0, "biweekly": 26.0, "monthly": 12.0,
                     "quarterly": 4.0, "semiannual": 2.0, "annual": 1.0}


@dataclass(slots=True)
class Recurring:
    """One charge that repeats on a steady cadence at a steady price."""
    key: str
    name: str
    category: str
    cadence: str
    amount: float                 # median charge
    n: int
    first_seen: date
    last_seen: date
    day_of_month: int             # typical billing day (0 for non-monthly)
    next_expected: date | None
    price_change: float = 0.0     # absolute drift, early third -> late third
    price_pct: float = 0.0
    confidence: float = 0.0       # 0..1, how regular the cadence and price are

    @property
    def per_year(self) -> float:
        return self.amount * _CADENCE_PER_YEAR.get(self.cadence, 12.0)

    @property
    def per_month(self) -> float:
        return self.per_year / 12.0

    # Names the three old call sites used, kept so callers read naturally.
    annual = per_year
    monthly = per_month

    def is_stale(self, as_of: date, slack_days: int = 45) -> bool:
        """True when the next charge is well overdue — cancelled, or a card died."""
        if not self.next_expected:
            return False
        return (as_of - self.next_expected).days > slack_days


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v[:10], "%Y-%m-%d").date()
        except ValueError:
            return parse_date(v)
    return None


def _field(item, *names):
    """Read a field from a dataclass, an object or a dict row."""
    for n in names:
        if isinstance(item, dict):
            if n in item and item[n] not in (None, ""):
                return item[n]
        elif hasattr(item, n):
            v = getattr(item, n)
            if v not in (None, ""):
                return v
    return None


def _add_months(d: date, n: int) -> date:
    """Shift by whole months, clamping to the end of a short month."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _mad_ratio(values: list[float]) -> float:
    """Median absolute deviation over the median. 0 means perfectly steady."""
    med = stats.median(values)
    if not med:
        return float("inf")
    return stats.median([abs(v - med) for v in values]) / abs(med)


def detect_recurring(items, *, key=None, min_occurrences: int = 3,
                     min_span_days: int = 50, amount_tolerance: float = 0.25,
                     cadence_tolerance: float = 0.35) -> list[Recurring]:
    """Find the charges that repeat, from transactions in any row shape.

    Accepts :class:`models.Transaction` objects, plain objects or dict rows —
    anything carrying a date, an amount, a merchant and a category.

    A charge qualifies when it appears at least ``min_occurrences`` times over
    at least ``min_span_days``, at a stable price, on either a stable day gap or
    (for the monthly case) in a stable run of calendar months. The calendar-month
    test matters: a subscription billed on the 3rd can land on the 1st, 5th and
    2nd across a quarter, and a pure day-gap test throws it away.

    Returns the list sorted by annual cost, most expensive first.
    """
    groups: dict[str, list] = defaultdict(list)
    for it in items:
        amt = _field(it, "amount")
        d = _as_date(_field(it, "date", "d", "post_date"))
        if amt is None or d is None:
            continue
        amt = float(amt)
        if amt <= 0:                       # credits and payments never recur
            continue
        name = str(_field(it, "merchant", "description", "name") or "").strip()
        if not name:
            continue
        k = key(it) if key else merchant_key(name)
        cat = str(_field(it, "category") or "Uncategorized")
        groups[k].append((d, amt, name, cat))

    out: list[Recurring] = []
    for k, rows in groups.items():
        if len(rows) < min_occurrences:
            continue
        rows.sort(key=lambda r: r[0])
        ds = [r[0] for r in rows]
        amts = [r[1] for r in rows]
        if (ds[-1] - ds[0]).days < min_span_days:
            continue

        med_amt = stats.median(amts)
        amt_spread = _mad_ratio(amts)
        if med_amt <= 0 or amt_spread > amount_tolerance:
            continue

        gaps = [g for g in ((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if g > 0]
        if not gaps:
            continue
        med_gap = stats.median(gaps)
        gap_spread = _mad_ratio(gaps)

        # Calendar-month test first: it tolerates a billing day that drifts.
        months_hit = sorted({(d.year, d.month) for d in ds})
        span_months = ((ds[-1].year - ds[0].year) * 12
                       + ds[-1].month - ds[0].month) + 1
        monthly_like = (len(months_hit) >= 3
                        and len(months_hit) >= span_months * 0.7
                        and len(rows) <= len(months_hit) * 1.4)

        if monthly_like:
            cadence = "monthly"
        elif 6 <= med_gap <= 400 and gap_spread <= cadence_tolerance:
            cadence = ("weekly" if med_gap <= 9 else "biweekly" if med_gap <= 18 else
                       "monthly" if med_gap <= 45 else "quarterly" if med_gap <= 115 else
                       "semiannual" if med_gap <= 200 else "annual")
        else:
            continue

        # Price drift: median of the first third against the last third.
        third = max(1, len(amts) // 3)
        early, late = stats.median(amts[:third]), stats.median(amts[-third:])
        change = late - early
        pct = (change / early * 100.0) if early else 0.0

        if cadence == "monthly":
            day = int(stats.median([d.day for d in ds]))
            nxt = _add_months(ds[-1], 1)
        else:
            day = 0
            nxt = ds[-1] + timedelta(days=int(round(med_gap)))

        # Confidence falls off as either the price or the cadence wobbles, and
        # rises with how many times we have actually seen it.
        steadiness = 1.0 - min(1.0, (amt_spread / amount_tolerance) * 0.5
                               + (min(gap_spread, 1.0) / cadence_tolerance) * 0.5)
        evidence = min(1.0, len(rows) / 6.0)
        confidence = round(max(0.0, min(1.0, 0.35 + 0.4 * steadiness + 0.25 * evidence)), 2)

        out.append(Recurring(
            key=k,
            name=Counter(r[2] for r in rows).most_common(1)[0][0],
            category=Counter(r[3] for r in rows).most_common(1)[0][0],
            cadence=cadence, amount=med_amt, n=len(rows),
            first_seen=ds[0], last_seen=ds[-1],
            day_of_month=day, next_expected=nxt,
            price_change=change, price_pct=pct, confidence=confidence,
        ))

    out.sort(key=lambda r: -r.per_year)
    return out


# --------------------------------------------------------------------------
# Debt math
# --------------------------------------------------------------------------

def payoff_months(balance, apr, payment) -> float | None:
    """Months to clear ``balance`` at a fixed monthly ``payment``.

    Returns None when the payment never overtakes the interest (so the balance
    never clears).
    """
    if not balance or not apr or not payment or payment <= 0:
        return None
    r = apr / 100.0 / 12.0
    if payment <= balance * r + 0.01:
        return None
    return math.log(payment / (payment - balance * r)) / math.log(1 + r)


def future_value(saved, monthly, n, apy: float = 0.0) -> float:
    """Balance after ``n`` months of saving ``monthly`` at annual ``apy``."""
    if apy <= 0:
        return saved + monthly * n
    r = (1 + apy / 100.0) ** (1 / 12.0) - 1
    return saved * (1 + r) ** n + monthly * (((1 + r) ** n - 1) / r)


# --------------------------------------------------------------------------
# File / CSV helpers
# --------------------------------------------------------------------------

def file_sha256(path: str, _chunk: int = 1 << 20) -> str:
    """Content hash of a file, read in chunks so large PDFs stay cheap."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def read_csv_rows(path: str, required: tuple[str, ...] = ()) -> list[dict]:
    """Load a CSV into a list of dict rows, exiting with a clear message on
    an empty file or a missing required column."""
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} is empty")
    if required:
        missing = set(required) - set(rows[0])
        if missing:
            sys.exit(f"{path} is missing required column(s): {', '.join(sorted(missing))}")
    return rows
