#!/usr/bin/env python3
"""
backtest.py — grade the forecasts against what actually happened.

Every other tool here is happy to print a projection. None of them ever checked
one. This module is the check: it walks the data month by month, hands each
predictor only the transactions dated *before* the month it is being asked
about, and scores the answer against the month that really happened.

    python backtest.py --db finances.db
    python backtest.py --db finances.db --category Dining
    python backtest.py --db finances.db --min-history 3
    python backtest.py --db finances.db --income 4000     # grade the planner too
    python backtest.py --db finances.db -o backtest.html

Two things get graded:

  * spending forecasts — engine.SpendingForecast (the incumbent, a trailing
    three-month mean) against trailing means over other windows, trailing
    medians, same-month-last-year, and committed-plus-variable, which starts
    from what Engine.recurring() says is already promised for the month and
    only has to guess the rest.
  * planner.py's goal ETAs — reconstructed from an earlier cutoff and compared
    against the surplus that actually turned up afterwards.

The honesty rules are part of the design rather than decoration. A statistic
over three months is not an accuracy claim, so every number is printed with the
number of months behind it, aggregates below MIN_REPORTABLE months are refused
outright, incomplete months are never scored, and a predictor that lacks the
history to run for some month is recorded as not having run rather than quietly
handed a default.

Reads the same store as everything else and writes nothing but the optional
HTML report.
"""

from __future__ import annotations

import argparse
import html
import os
import statistics as stats
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import DEFAULT_DB                                   # noqa: E402
from engine import Engine, SpendingForecast                       # noqa: E402
from utils import fmt_money as money, merchant_key                 # noqa: E402

# planner.py is imported, not copied: its allocation waterfall is the thing
# under test, so re-implementing it here would grade a lookalike.
try:
    from planner import load_plan, plan_goals
    PLANNER_IMPORTED = True
    PLANNER_NOTE = "imported plan_goals/_run_forward from planner.py"
except Exception as exc:                                          # pragma: no cover
    PLANNER_IMPORTED = False
    PLANNER_NOTE = f"planner.py could not be imported ({exc})"

# Below MIN_REPORTABLE scored months no aggregate is printed at all. Between
# that and MIN_SOUND every aggregate is labelled indicative. These are the two
# numbers that stop this tool from making claims the data cannot support.
MIN_REPORTABLE = 3
MIN_SOUND = 6

DASH = "—"


def signed(v: float) -> str:
    """``+$120`` / ``-$120`` — bias only means something with its sign attached."""
    return ("+" if v >= 0 else "-") + money(abs(v))


# --------------------------------------------------------------------------
# Months
# --------------------------------------------------------------------------

def month_start(m: str) -> date:
    return date(int(m[:4]), int(m[5:7]), 1)


def month_end(m: str) -> date:
    s = month_start(m)
    return (s.replace(day=28) + timedelta(days=7)).replace(day=1) - timedelta(days=1)


def shift_month(m: str, n: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1 + n
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def months_covered(by_month: dict, first: date, last: date) -> list[str]:
    """The months this slice of data actually covers most of.

    Same test as ``Engine.complete_months`` — 85% of the calendar month present
    — but deliberately without engine's ``or sorted(self.by_month())`` fallback.
    A backtest that silently widened its own history when nothing qualified
    would be cheating, so an empty answer stays empty.
    """
    out = []
    for m in sorted(by_month):
        start, end = month_start(m), month_end(m)
        days = (min(end, last) - max(start, first)).days + 1
        if days >= (end - start).days * 0.85:
            out.append(m)
    return out


# --------------------------------------------------------------------------
# What a predictor is allowed to know
# --------------------------------------------------------------------------

class Snapshot:
    """The world as it looked on the first day of ``month``.

    Nothing dated on or after that day is in here, which is the whole point:
    the predictor cannot see the month it is predicting, and cannot see any
    month after it either.
    """

    def __init__(self, path: str, txns: list, month: str):
        self.month = month
        self.cutoff = month_start(month)
        self.txns = [t for t in txns if t.d and t.d < self.cutoff]
        self.engine = Engine(path, txns=self.txns)

        spend = self.engine.spend()
        self.first = min((t.d for t in spend), default=None)
        self.last = max((t.d for t in spend), default=None)

        # Every month present, partial ones included. This is what
        # SpendingForecast sees, so the incumbent is graded on its real
        # behaviour rather than a tidied-up version of it.
        self.raw_months = self.engine.by_month()

        cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for t in spend:
            cat[t.category][t.date[:7]] += t.amount
        self.cat = {c: dict(v) for c, v in cat.items()}

        self.months = ([] if self.first is None
                       else months_covered(self.raw_months, self.first, self.last))

    def value(self, month: str, category: str | None) -> float:
        if category:
            return self.cat.get(category, {}).get(month, 0.0)
        return self.raw_months.get(month, 0.0)

    def series(self, category: str | None = None) -> list[float]:
        """Spend per complete month of history, oldest first."""
        return [self.value(m, category) for m in self.months]

    def raw_series(self, category: str | None = None) -> list[float]:
        """Spend per month present in history, partial months included."""
        return [self.value(m, category) for m in sorted(self.raw_months)]

    def residual_series(self, keys: set, category: str | None = None) -> list[float]:
        """Per complete month, the spend that no recurring charge accounts for."""
        out: dict[str, float] = defaultdict(float)
        for t in self.engine.spend():
            if category and t.category != category:
                continue
            if merchant_key(t.merchant or t.description) in keys:
                continue
            out[t.date[:7]] += t.amount
        return [out.get(m, 0.0) for m in self.months]


# --------------------------------------------------------------------------
# Predictors
# --------------------------------------------------------------------------

class Predictor:
    """Answers one question: how much will be spent in ``month``.

    ``predict`` returns ``(value, note)``. A value of None means the predictor
    could not run and the note says why. Nothing is ever substituted for a
    missing answer — an unrunnable month is recorded, not filled in.
    """

    label = ""
    name = ""
    blurb = ""

    def predict(self, snap: Snapshot, month: str,
                category: str | None) -> tuple[float | None, str]:
        raise NotImplementedError


class IncumbentForecast(Predictor):
    """engine.SpendingForecast exactly as the other tools call it."""

    label = "fc3"
    name = "engine.SpendingForecast(3)"
    blurb = ("the incumbent: mean of the last three months present, partial "
             "months included, which is what every other tool prints today")

    def __init__(self, min_history: int = 3):
        self.min_history = min_history

    def predict(self, snap, month, category):
        n = len(snap.raw_months)
        if n < self.min_history:
            return None, f"needs {self.min_history} months of history, has {n}"
        if category is None:
            return SpendingForecast(snap.engine, window=3).next_month(), ""
        # SpendingForecast has no per-category mode; this applies its rule
        # (last three months present) to one category's series.
        vals = snap.raw_series(category)[-3:]
        return sum(vals) / len(vals), ""


class TrailingMean(Predictor):
    def __init__(self, window: int | None, min_history: int = 3):
        self.window = window
        self.min_history = min_history
        self.label = f"mean{window}" if window else "meanAll"
        self.name = (f"trailing mean, {window} month" + ("s" if window != 1 else "")
                     if window else "trailing mean, all history")
        self.blurb = ("the same idea as the incumbent over a different window, "
                      "and over complete months only")

    def predict(self, snap, month, category):
        s = snap.series(category)
        need = self.window or self.min_history
        if len(s) < need:
            return None, f"needs {need} complete months of history, has {len(s)}"
        vals = s[-self.window:] if self.window else s
        return sum(vals) / len(vals), ""


class TrailingMedian(Predictor):
    def __init__(self, window: int | None, min_history: int = 3):
        self.window = window
        self.min_history = min_history
        self.label = f"med{window}" if window else "medAll"
        self.name = (f"trailing median, {window} months" if window
                     else "trailing median, all history")
        self.blurb = "one freak month moves a mean and barely moves a median"

    def predict(self, snap, month, category):
        s = snap.series(category)
        need = self.window or self.min_history
        if len(s) < need:
            return None, f"needs {need} complete months of history, has {len(s)}"
        vals = s[-self.window:] if self.window else s
        return stats.median(vals), ""


class SameMonthLastYear(Predictor):
    label = "smly"
    name = "same month last year"
    blurb = "the only predictor here that can know December is not like March"

    def predict(self, snap, month, category):
        prev = shift_month(month, -12)
        if prev not in snap.months:
            return None, "the same month a year earlier is not a complete month of history"
        return snap.value(prev, category), ""


class CommittedPlusVariable(Predictor):
    """What is already promised, plus a guess at the rest.

    Engine.recurring() knows roughly what next month owes to subscriptions,
    rent and interest. That part does not need forecasting, only looking up.
    Only the residual — the discretionary noise — is averaged, so the averaging
    is applied to the one part that is actually noisy.
    """

    label = "cmt+var"
    name = "committed + variable"
    blurb = ("Engine.recurring() for the promised part, trailing mean of the "
             "non-recurring residual for the rest")

    def __init__(self, window: int = 3, min_history: int = 3):
        self.window = window
        self.min_history = min_history
        self.committed_share: list[float] = []

    def predict(self, snap, month, category):
        need = max(self.window, self.min_history)
        if len(snap.months) < need:
            return None, f"needs {need} complete months of history, has {len(snap.months)}"

        recs = snap.engine.recurring()
        keys = {r.key for r in recs}
        live = [r for r in recs if not r.is_stale(self.cutoff_of(snap))]
        if category:
            live = [r for r in live if r.category == category]
        committed = sum(r.per_month for r in live)

        resid = snap.residual_series(keys, category)[-self.window:]
        value = committed + sum(resid) / len(resid)
        if value > 0:
            self.committed_share.append(committed / value)
        return value, ""

    @staticmethod
    def cutoff_of(snap: Snapshot) -> date:
        return snap.cutoff


def build_predictors(min_history: int) -> list[Predictor]:
    """The incumbent first; everything after it is a challenger."""
    return [
        IncumbentForecast(min_history),
        TrailingMean(1, min_history),
        TrailingMean(3, min_history),
        TrailingMean(6, min_history),
        TrailingMean(None, min_history),
        TrailingMedian(3, min_history),
        TrailingMedian(None, min_history),
        SameMonthLastYear(),
        CommittedPlusVariable(3, min_history),
    ]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class Score:
    predictor: Predictor
    rows: list = field(default_factory=list)     # (month, actual, predicted)
    misses: list = field(default_factory=list)   # (month, why it could not run)

    @property
    def label(self) -> str:
        return self.predictor.label

    @property
    def name(self) -> str:
        return self.predictor.name

    @property
    def months(self) -> list[str]:
        return [r[0] for r in self.rows]

    @property
    def n(self) -> int:
        return len(self.rows)

    def _subset(self, only):
        return [r for r in self.rows if only is None or r[0] in only]

    def mae(self, only=None):
        rs = self._subset(only)
        return sum(abs(p - a) for _, a, p in rs) / len(rs) if rs else None

    def mape(self, only=None):
        """Mean absolute percentage error, over months that had any spend.

        A month with zero actual spend has no percentage error, so it is left
        out and the count it was computed over is reported separately.
        """
        rs = [r for r in self._subset(only) if r[1] > 0]
        if not rs:
            return None, 0
        return sum(abs(p - a) / a for _, a, p in rs) / len(rs) * 100.0, len(rs)

    def bias(self, only=None):
        """Mean signed error. Persistently high or low is a different fault
        from merely noisy, and averaging the absolute error hides it."""
        rs = self._subset(only)
        return sum(p - a for _, a, p in rs) / len(rs) if rs else None

    def prediction_for(self, month: str):
        for m, _a, p in self.rows:
            if m == month:
                return p
        return None


@dataclass
class Backtest:
    category: str | None
    scored: list                                  # months actually scored
    skipped: list                                 # (month, why) — never scored
    actual: dict                                  # month -> real spend
    scores: list                                  # list[Score]
    ranking: list = field(default_factory=list)   # ordered list[Score]
    common: set | None = None                     # months the ranking used
    verdict: str = ""


def run_backtest(path: str, txns: list, complete: list[str], predictors: list,
                 full_actual: dict, category: str | None,
                 min_history: int) -> Backtest:
    """Walk forward through the complete months, scoring every predictor."""
    scores = [Score(p) for p in predictors]
    scored, skipped, actual = [], [], {}

    for m in complete:
        snap = Snapshot(path, txns, m)
        if len(snap.months) < min_history:
            skipped.append((m, f"only {len(snap.months)} complete month(s) of "
                               f"history before it, {min_history} required"))
            continue
        scored.append(m)
        actual[m] = full_actual.get(m, 0.0)
        for sc in scores:
            value, note = sc.predictor.predict(snap, m, category)
            if value is None:
                sc.misses.append((m, note))
            else:
                sc.rows.append((m, actual[m], value))

    bt = Backtest(category, scored, skipped, actual, scores)
    bt.ranking, bt.common = rank(scores)
    bt.verdict = verdict(bt)
    return bt


def rank(scores: list) -> tuple[list, set | None]:
    """Order the predictors, comparing them on the same months where possible.

    Ranking by each predictor's own MAE would let a predictor that only ran in
    the two calm months beat one that had to face every month. So the ranking
    is done on the months every ranked predictor covered; when that intersection
    is too thin to mean anything, the ranking falls back to own coverage and
    ``common`` comes back None so the caller can say so.
    """
    live = [s for s in scores if s.n >= MIN_REPORTABLE]
    if not live:
        return [], None
    common = set(live[0].months)
    for s in live[1:]:
        common &= set(s.months)
    if len(common) < MIN_REPORTABLE:
        return sorted(live, key=lambda s: s.mae()), None
    return sorted(live, key=lambda s: s.mae(common)), common


def verdict(bt: Backtest) -> str:
    """Name a winner, or refuse to."""
    if not bt.ranking:
        return (f"No predictor was scorable on {MIN_REPORTABLE} or more months. "
                f"There is no winner to name.")
    best = bt.ranking[0]
    only = bt.common
    n = len(only) if only else best.n
    basis = (f"{n} shared months" if only
             else f"{n} months, and not the same months for each predictor")
    if len(bt.ranking) == 1:
        return (f"Only {best.name} cleared {MIN_REPORTABLE} scored months, so it "
                f"wins by default over {basis}. That is not evidence it is good.")
    second = bt.ranking[1]
    b, s = best.mae(only), second.mae(only)
    margin = (s - b) / s * 100.0 if s else 0.0
    if margin < 10.0:
        return (f"Too close to call over {basis}: {best.name} at {money(b)} MAE and "
                f"{second.name} at {money(s)} are within {margin:.1f}% of each other. "
                f"This sample cannot separate them.")
    line = (f"{best.name} wins over {basis}: {money(b)} MAE against "
            f"{money(s)} for {second.name}, {margin:.0f}% better.")
    if n < MIN_SOUND:
        line += (f" On {n} months. That is a direction, not a measurement — "
                 f"one more month could reorder it.")
    return line


# --------------------------------------------------------------------------
# Grading the planner
# --------------------------------------------------------------------------

@dataclass
class PlannerCheck:
    rows: list = field(default_factory=list)
    goal: str = ""
    target: float = 0.0
    synthetic: bool = False
    note: str = ""
    income: float | None = None


def grade_planner(path: str, txns: list, eng: Engine, complete: list[str],
                  min_history: int, plan_path: str,
                  income: float | None = None) -> PlannerCheck:
    """Re-run planner.plan_goals from an earlier cutoff and check the ETA.

    At each cutoff the planner is given only the surplus visible up to that
    point, exactly as `planner.py init` would have computed it, and asked when
    the goal lands. Then the months after the cutoff are played back to see when
    the money actually accumulated. The gap between the two is the honest cost
    of projecting a single average surplus forward forever.
    """
    out = PlannerCheck()
    if not PLANNER_IMPORTED:
        out.note = PLANNER_NOTE
        return out

    plan = load_plan(plan_path)
    goals = [g for g in plan.get("goals", []) if not g.get("done")]
    if not goals:
        # No plan file to grade, so stand up a goal the data itself implies.
        # Sized off total spending rather than Engine.essential_monthly(): when
        # most of a dataset is Uncategorized the essentials figure is far too
        # small, and a goal funded in one month grades nothing.
        base = eng.monthly_average()
        out.synthetic = True
        plan = {"goals": [{"name": "Emergency fund", "target": round(base * 6, 2),
                           "saved": 0.0, "by": None, "priority": 1,
                           "monthly": None, "done": False}],
                "apy": 0.0}
        goals = plan["goals"]
    goal = goals[0]
    out.goal, out.target = goal["name"], goal["target"]

    inc, spd = eng.income_by_month(), eng.by_month()

    for m in complete:
        future = [x for x in complete if x >= m]
        if len(future) < 2:
            continue                       # nothing to check the projection against
        snap = Snapshot(path, txns, m)
        if len(snap.months) < min_history:
            continue

        # The surplus the planner would have been handed. Computed over the
        # snapshot's own complete months rather than via Engine.surplus(),
        # whose complete_months() falls back to every month when none qualify.
        hist_spend = snap.series()
        hist_inc = ([income] * len(snap.months) if income is not None
                    else [inc.get(x, 0.0) for x in snap.months])
        row = {"cutoff": m, "observed": len(future)}
        if not any(hist_inc):
            row["note"] = ("no income in the history, planner would have refused to "
                           "guess — supply it with --income")
            out.rows.append(row)
            continue

        def earned(x: str) -> float:
            return income if income is not None else inc.get(x, 0.0)

        assumed = sum(hist_inc) / len(hist_inc) - sum(hist_spend) / len(hist_spend)
        realised = sum(earned(x) - spd.get(x, 0.0) for x in future) / len(future)
        row["assumed"] = assumed
        row["realised"] = realised

        projected, _left = plan_goals(plan, assumed, today=month_start(m))
        g = projected[0]
        row["eta"] = g["eta_months"]

        # What actually accumulated, month by month, from the cutoff forward.
        remaining, cum, hit = g["remaining"], 0.0, None
        for k, x in enumerate(future, start=1):
            cum += earned(x) - spd.get(x, 0.0)
            if cum >= remaining:
                hit = k
                break
        row["actual"] = hit
        row["banked"] = cum
        out.rows.append(row)

    return out


# --------------------------------------------------------------------------
# Text report
# --------------------------------------------------------------------------

def band(n: int) -> str:
    if n < MIN_REPORTABLE:
        return "refused"
    return "indicative" if n < MIN_SOUND else "reportable"


def print_report(rep: dict):
    bt: Backtest = rep["backtest"]
    meta = rep["meta"]
    what = f"category {bt.category}" if bt.category else "total spend"

    print()
    print(f"  FORECAST BACKTEST   {meta['source']}   {what}")
    print("  " + "─" * 96)
    print(f"  {meta['n_txns']} transactions, {meta['first']} to {meta['last']}")
    print(f"  {meta['n_present']} months carry data, {meta['n_complete']} of them "
          f"complete; incomplete months are never scored")
    if meta["gaps"]:
        g = meta["gaps"]
        print(f"  {len(g)} calendar month(s) inside that range hold no data at all: "
              f"{', '.join(g)}")
        print(f"  Those are absent, not zero, so they are not scored either")
    print(f"  {len(bt.scored)} month(s) scorable: complete, with at least "
          f"{meta['min_history']} complete months of history in front of them")
    for m, why in bt.skipped:
        print(f"    {m} not scored — {why}")
    print()

    print("  PREDICTORS")
    said = set()
    for s in bt.scores:
        print(f"    {s.label:<9} {s.name}")
        if s.predictor.blurb in said:      # the window variants share a rationale
            continue
        said.add(s.predictor.blurb)
        for line in wrap(s.predictor.blurb, 80):
            print(f"    {'':<9} {line}")
    print()

    # -- predicted vs actual, month by month --------------------------------
    order = [s for s in bt.scores]
    print("  PREDICTED vs ACTUAL")
    head = f"  {'month':<9}{'actual':>10}" + "".join(f"{s.label:>10}" for s in order)
    print(head)
    print("  " + "─" * (len(head) - 2))
    for m in bt.scored:
        line = f"  {m:<9}{money(bt.actual[m]):>10}"
        for s in order:
            p = s.prediction_for(m)
            line += f"{(money(p) if p is not None else DASH):>10}"
        print(line)
    if bt.scored:
        line = f"  {'MAE':<9}{'':>10}"
        for s in order:
            # Own-coverage MAE, and only where enough months back it. A mean
            # error over one month is a single error wearing a statistic's hat.
            ok = s.n >= MIN_REPORTABLE
            line += f"{(money(s.mae()) if ok else DASH):>10}"
        print(line + f"   own coverage, blank below {MIN_REPORTABLE} months")
    print()

    # -- the scoreboard -----------------------------------------------------
    print("  SCORES")
    print(f"  {'predictor':<30}{'ran':>7}{'MAE':>11}{'MAPE':>9}{'bias':>11}   basis")
    print("  " + "─" * 96)
    for s in bt.scores:
        ran = f"{s.n}/{len(bt.scored)}"
        if s.n < MIN_REPORTABLE:
            why = "no aggregate: too few months" if s.n else "never ran"
            print(f"  {s.name[:29]:<30}{ran:>7}{DASH:>11}{DASH:>9}{DASH:>11}   {why}")
            continue
        mape, n_mape = s.mape()
        # A percentage error needs a denominator, so months with no spend at all
        # drop out; if too few are left the MAPE is refused rather than shrunk.
        thin = mape is None or n_mape < MIN_REPORTABLE
        extra = (f", MAPE refused on {n_mape} month(s) with spend" if thin
                 else f", MAPE over {n_mape}" if n_mape != s.n else "")
        print(f"  {s.name[:29]:<30}{ran:>7}{money(s.mae()):>11}"
              f"{(DASH if thin else f'{mape:.0f}%'):>9}"
              f"{signed(s.bias()):>11}   {band(s.n)}, n={s.n}{extra}")
    print()
    print(f"  MAE and bias are in dollars per month; bias is the mean signed error, so")
    print(f"  positive means the predictor runs high. Anything under {MIN_SOUND} months is")
    print(f"  labelled indicative and under {MIN_REPORTABLE} months is not reported at all.")

    reasons = [(s, Counter(w for _m, w in s.misses)) for s in bt.scores if s.misses]
    if reasons:
        print()
        print("  COULD NOT RUN")
        for s, c in reasons:
            for why, k in c.most_common():
                print(f"    {s.label:<9} {k} month(s): {why}")
    print()

    # -- head to head -------------------------------------------------------
    inc = bt.scores[0]
    print(f"  HEAD TO HEAD vs {inc.name}")
    print(f"  {'challenger':<30}{'shared':>8}{'incumbent':>12}{'challenger':>12}"
          f"{'change':>10}")
    print("  " + "─" * 96)
    for s in bt.scores[1:]:
        shared = set(s.months) & set(inc.months)
        if len(shared) < MIN_REPORTABLE:
            print(f"  {s.name[:29]:<30}{len(shared):>8}   "
                  f"not enough shared months to compare")
            continue
        a, b = inc.mae(shared), s.mae(shared)
        delta = (b - a) / a * 100.0 if a else 0.0
        print(f"  {s.name[:29]:<30}{len(shared):>8}{money(a):>12}{money(b):>12}"
              f"{delta:>+9.0f}%")
    print()
    if meta.get("committed") is not None:
        c = meta["committed"]
        print(f"  Committed + variable had {c['recurring']} recurring charge(s) to work "
              f"from, covering")
        print(f"  {c['share'] * 100:.0f}% of its own prediction. The less of a month is "
              f"already promised, the")
        print(f"  more of it is a guess, and the closer this predictor gets to being a "
              f"plain mean.")
        print()
    print(f"  VERDICT")
    for line in wrap(bt.verdict, 92):
        print(f"    {line}")
    print()

    # -- per category -------------------------------------------------------
    if rep["categories"]:
        print("  PER CATEGORY")
        print(f"  {'category':<20}{'actual/mo':>12}{'best predictor':>30}{'MAE':>10}"
              f"{'fc3 MAE':>10}{'n':>5}")
        print("  " + "─" * 96)
        for row in rep["categories"]:
            if row["best"] is None:
                print(f"  {row['category'][:19]:<20}{money(row['avg']):>12}"
                      f"{'not scorable':>30}{DASH:>10}{DASH:>10}{row['n']:>5}")
                continue
            print(f"  {row['category'][:19]:<20}{money(row['avg']):>12}"
                  f"{row['best'][:29]:>30}{money(row['mae']):>10}"
                  f"{(money(row['fc3']) if row['fc3'] is not None else DASH):>10}"
                  f"{row['n']:>5}")
        print()
        print(f"  Each category is backtested on its own series. n is the months scored;")
        print(f"  at n<{MIN_SOUND} the winner of a category is a coin toss with an opinion.")
        print()

    # -- planner ------------------------------------------------------------
    pc: PlannerCheck = rep["planner"]
    print("  PLANNER BACKTEST")
    print("  " + "─" * 96)
    print(f"  {PLANNER_NOTE}")
    if not PLANNER_IMPORTED:
        print()
        return
    src = "no goals in the plan file, so a synthetic goal stands in: " if pc.synthetic \
        else "goal from the plan file: "
    print(f"  {src}{pc.goal}, {money(pc.target)}"
          + (" (six months of average spending)" if pc.synthetic else ""))
    if pc.income is not None:
        print(f"  income taken from --income {money(pc.income)}/mo rather than from the "
              f"statements")
    if not pc.rows:
        print("  No cutoff had both enough history to plan from and enough months")
        print("  afterwards to check against. The planner cannot be graded on this data.")
        print()
        return
    print()
    print(f"  {'cutoff':<9}{'surplus assumed':>17}{'actually ran at':>17}"
          f"{'predicted ETA':>15}{'actual':>17}{'error':>12}")
    print("  " + "─" * 96)
    for r in pc.rows:
        if "note" in r:
            print(f"  {r['cutoff']:<9}{r['note']}")
            continue
        eta, act, err = eta_cells(r)
        print(f"  {r['cutoff']:<9}{money(r['assumed']):>17}{money(r['realised']):>17}"
              f"{eta:>15}{act:>17}{err:>12}")
    print()
    for line in wrap(planner_verdict(pc), 92):
        print(f"    {line}")
    print()


def eta_cells(row: dict) -> tuple[str, str, str]:
    """The three ETA columns, with the difference between wrong and unknown kept.

    A goal that has not landed inside the observed window is only evidence of a
    bad ETA if the window was long enough to contain the ETA in the first place.
    """
    eta = f"{row['eta']} mo" if row["eta"] else "never"
    if row["actual"]:
        act = f"{row['actual']} mo"
        err = f"{row['eta'] - row['actual']:+d} mo" if row["eta"] else "missed"
    elif row["eta"] and row["observed"] >= row["eta"]:
        act, err = f"not in {row['observed']} mo", "missed"
    elif row["eta"]:
        act, err = f"only {row['observed']} mo seen", "unchecked"
    else:
        act, err = f"not in {row['observed']} mo", "agreed"
    return eta, act, err


def planner_verdict(pc: PlannerCheck) -> str:
    usable = [r for r in pc.rows if "assumed" in r]
    if not usable:
        return ("Every cutoff lacked income in its history, so the planner would have "
                "refused to project at all. Nothing to grade, which is the correct "
                "outcome rather than a bug.")
    drift = [r["realised"] - r["assumed"] for r in usable]
    graded = [r for r in usable if r["eta"] and r["actual"]]
    mean_drift = sum(drift) / len(drift)
    line = (f"Over {len(usable)} cutoff(s) the surplus that actually turned up ran "
            f"{money(abs(mean_drift))}/mo "
            f"{'above' if mean_drift > 0 else 'below'} what the planner assumed.")
    if graded:
        errs = [abs(r["eta"] - r["actual"]) for r in graded]
        unchecked = [r for r in usable
                     if r["eta"] and not r["actual"] and r["observed"] < r["eta"]]
        line += (f" On the {len(graded)} cutoff(s) where both an ETA and a real "
                 f"completion date exist, the ETA was out by "
                 f"{sum(errs) / len(errs):.1f} months on average, "
                 f"{'late' if sum(r['eta'] - r['actual'] for r in graded) > 0 else 'early'}"
                 f" on balance because the surplus estimate was conservative.")
        if unchecked:
            line += (f" {len(unchecked)} further cutoff(s) are not gradeable: the data "
                     f"ends before the ETA they projected, which is unknown rather "
                     f"than wrong.")
        if len(graded) < MIN_REPORTABLE:
            line += (f" {len(graded)} graded cutoff(s) is below the {MIN_REPORTABLE} "
                     f"this tool will average over, so read the rows, not the mean.")
    else:
        never = [r for r in usable if not r["eta"]]
        line += (f" No cutoff produced both a projected and a realised completion "
                 f"date: {len(never)} of {len(usable)} projected 'never' because the "
                 f"assumed surplus was not positive, and the goal was never funded "
                 f"inside the observed window either. The two agree, but agreeing on "
                 f"'never' tests nothing about the ETA arithmetic.")
    return line


def wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

def esc(s) -> str:
    return html.escape(str(s))


def render_html(rep: dict, title: str) -> str:
    bt: Backtest = rep["backtest"]
    meta, pc = rep["meta"], rep["planner"]
    what = f"category {bt.category}" if bt.category else "total spend"

    head = "".join(f"<th>{esc(s.label)}</th>" for s in bt.scores)
    rows = []
    for m in bt.scored:
        cells = "".join(
            f"<td>{money(s.prediction_for(m)) if s.prediction_for(m) is not None else DASH}</td>"
            for s in bt.scores)
        rows.append(f"<tr><th class='k'>{esc(m)}</th>"
                    f"<td class='act'>{money(bt.actual[m])}</td>{cells}</tr>")

    score_rows = []
    for s in bt.scores:
        ran = f"{s.n}/{len(bt.scored)}"
        if s.n < MIN_REPORTABLE:
            note = "too few months to report" if s.n else "never ran"
            score_rows.append(f"<tr class='thin'><th class='k'>{esc(s.name)}</th>"
                              f"<td>{ran}</td><td>{DASH}</td><td>{DASH}</td>"
                              f"<td>{DASH}</td><td class='note'>{note}</td></tr>")
            continue
        mape, n_mape = s.mape()
        thin = mape is None or n_mape < MIN_REPORTABLE
        cls = "" if s.n >= MIN_SOUND else " class='thin'"
        note = (f"{band(s.n)}, n={s.n}"
                + (f", MAPE refused on {n_mape} month(s) with spend" if thin
                   else f", MAPE over {n_mape}" if n_mape != s.n else ""))
        score_rows.append(
            f"<tr{cls}><th class='k'>{esc(s.name)}</th><td>{ran}</td>"
            f"<td>{money(s.mae())}</td><td>{DASH if thin else f'{mape:.0f}%'}</td>"
            f"<td>{signed(s.bias())}</td>"
            f"<td class='note'>{note}</td></tr>")

    inc = bt.scores[0]
    h2h = []
    for s in bt.scores[1:]:
        shared = set(s.months) & set(inc.months)
        if len(shared) < MIN_REPORTABLE:
            h2h.append(f"<tr class='thin'><th class='k'>{esc(s.name)}</th>"
                       f"<td>{len(shared)}</td><td colspan='3' class='note'>"
                       f"not enough shared months</td></tr>")
            continue
        a, b = inc.mae(shared), s.mae(shared)
        d = (b - a) / a * 100.0 if a else 0.0
        cls = "win" if d < 0 else "lose"
        h2h.append(f"<tr><th class='k'>{esc(s.name)}</th><td>{len(shared)}</td>"
                   f"<td>{money(a)}</td><td>{money(b)}</td>"
                   f"<td class='{cls}'>{d:+.0f}%</td></tr>")

    cat_rows = []
    for r in rep["categories"]:
        if r["best"] is None:
            cat_rows.append(f"<tr class='thin'><th class='k'>{esc(r['category'])}</th>"
                            f"<td>{money(r['avg'])}</td><td colspan='3' class='note'>"
                            f"not scorable</td><td>{r['n']}</td></tr>")
            continue
        cat_rows.append(f"<tr><th class='k'>{esc(r['category'])}</th>"
                        f"<td>{money(r['avg'])}</td><td>{esc(r['best'])}</td>"
                        f"<td>{money(r['mae'])}</td>"
                        f"<td>{money(r['fc3']) if r['fc3'] is not None else DASH}</td>"
                        f"<td>{r['n']}</td></tr>")

    plan_rows = []
    for r in getattr(pc, "rows", []):
        if "note" in r:
            plan_rows.append(f"<tr class='thin'><th class='k'>{esc(r['cutoff'])}</th>"
                             f"<td colspan='5' class='note'>{esc(r['note'])}</td></tr>")
            continue
        eta, act, err = eta_cells(r)
        plan_rows.append(f"<tr><th class='k'>{esc(r['cutoff'])}</th>"
                         f"<td>{money(r['assumed'])}</td><td>{money(r['realised'])}</td>"
                         f"<td>{eta}</td><td>{act}</td><td>{err}</td></tr>")

    gaps = (f"<li>{len(meta['gaps'])} calendar month(s) in range hold no data at all "
            f"({esc(', '.join(meta['gaps']))}) — absent, not zero, so not scored</li>"
            if meta["gaps"] else "")
    skipped = "".join(f"<li>{esc(m)} not scored — {esc(w)}</li>" for m, w in bt.skipped)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>
:root{{--paper:#FBFBF7;--band:#E7EFE3;--ink:#14211B;--soft:#5C6D62;--rule:#C2D1BC;
--green:#2E6A4C;--red:#A3372A;--amber:#9C7318;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:#DCE3D8;font-family:var(--ui);color:var(--ink);font-size:15px}}
.sheet{{max-width:1040px;margin:26px auto;background:var(--paper);
border:1px solid #8FA88A;padding:34px 40px 40px;
box-shadow:0 12px 34px rgba(20,33,27,.10)}}
.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
text-transform:uppercase;color:var(--soft)}}
h1{{font-family:var(--display);font-weight:400;font-size:34px;margin:6px 0 6px}}
h2{{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;
color:var(--soft);margin:30px 0 10px;border-bottom:1px solid var(--rule);padding-bottom:6px}}
.lede{{color:var(--soft);margin:0 0 18px}}
ul.facts{{font-size:13px;color:var(--soft);padding-left:18px;margin:0 0 18px}}
table{{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px}}
th,td{{text-align:right;padding:5px 7px;border-bottom:1px solid var(--rule)}}
thead th{{text-align:right;color:var(--soft);font-weight:400;font-size:11px;
text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--ink)}}
th.k,thead th:first-child{{text-align:left;font-weight:600}}
td.act{{font-weight:700;background:var(--band)}}
tr.thin{{color:var(--soft)}}
td.note{{text-align:left;font-size:11px;color:var(--soft)}}
.win{{color:var(--green);font-weight:700}}
.lose{{color:var(--red)}}
.verdict{{border-left:3px solid var(--green);background:#fff;border:1px solid var(--rule);
border-left:3px solid var(--green);padding:14px 16px;margin:16px 0}}
.scroll{{overflow-x:auto}}
footer{{margin-top:26px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft)}}
</style></head><body><div class="sheet">
<div class="kicker">Forecast backtest</div>
<h1>How wrong were the forecasts</h1>
<p class="lede">{esc(meta['source'])} &middot; {esc(what)} &middot; walk-forward, each
month predicted using only transactions dated before it.</p>
<ul class="facts">
<li>{meta['n_txns']} transactions, {esc(meta['first'])} to {esc(meta['last'])}</li>
<li>{meta['n_present']} months carry data, {meta['n_complete']} complete;
incomplete months are never scored</li>
{gaps}
<li>{len(bt.scored)} month(s) scorable at --min-history {meta['min_history']}</li>
{skipped}
</ul>

<h2>Predicted vs actual</h2>
<div class="scroll"><table><thead><tr><th>month</th><th>actual</th>{head}</tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>

<h2>Scores</h2>
<table><thead><tr><th>predictor</th><th>ran</th><th>MAE</th><th>MAPE</th>
<th>bias</th><th>basis</th></tr></thead><tbody>{''.join(score_rows)}</tbody></table>
<p class="lede" style="font-size:12.5px;margin-top:10px">MAE and bias in dollars per
month. Bias is the mean signed error: positive means the predictor runs high, which is
a different fault from being noisy. Under {MIN_SOUND} months every figure is indicative;
under {MIN_REPORTABLE} months nothing is reported.</p>

<h2>Head to head against {esc(inc.name)}</h2>
<table><thead><tr><th>challenger</th><th>shared</th><th>incumbent MAE</th>
<th>challenger MAE</th><th>change</th></tr></thead>
<tbody>{''.join(h2h)}</tbody></table>
{f'''<p class="lede" style="font-size:12.5px;margin-top:10px">Committed + variable had
{meta['committed']['recurring']} recurring charge(s) to work from, covering
{meta['committed']['share'] * 100:.0f}% of its own prediction. The less of a month is
already promised, the more of it is a guess, and the closer this predictor gets to being
a plain trailing mean.</p>''' if meta.get('committed') else ''}
<div class="verdict">{esc(bt.verdict)}</div>

<h2>Per category</h2>
<table><thead><tr><th>category</th><th>actual/mo</th><th>best predictor</th>
<th>MAE</th><th>fc3 MAE</th><th>n</th></tr></thead>
<tbody>{''.join(cat_rows) or "<tr><td colspan='6' class='note'>nothing scorable</td></tr>"}</tbody></table>

<h2>Planner ETAs</h2>
<p class="lede" style="font-size:12.5px">{esc(PLANNER_NOTE)}.
{esc(('No goals in the plan file, so a synthetic goal stands in: ' if pc.synthetic
      else 'Goal from the plan file: ') + f'{pc.goal}, {money(pc.target)}')}</p>
<table><thead><tr><th>cutoff</th><th>surplus assumed</th><th>actually ran at</th>
<th>predicted ETA</th><th>actual</th><th>error</th></tr></thead>
<tbody>{''.join(plan_rows) or "<tr><td colspan='6' class='note'>no cutoff had both enough history to plan from and enough months afterwards to check against</td></tr>"}</tbody></table>
<div class="verdict">{esc(planner_verdict(pc) if pc.rows else
    'The planner cannot be graded on this data.')}</div>

<footer>Generated locally by backtest.py on {date.today():%d %b %Y}.
Every figure above is scored against months that had already happened, using only
data dated before the month being predicted. Sample sizes are printed because at
this length of history they are the most important number on the page.</footer>
</div></body></html>"""


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def category_table(path, txns, complete, full_cats, min_history, top_n):
    """Backtest each of the biggest categories on its own series."""
    ranked = sorted(full_cats.items(), key=lambda kv: -sum(kv[1].values()))[:top_n]
    out = []
    n_months = max(len(complete), 1)
    for cat, series in ranked:
        preds = build_predictors(min_history)
        bt = run_backtest(path, txns, complete, preds, series, cat, min_history)
        row = {"category": cat, "avg": sum(series.values()) / n_months,
               "n": len(bt.scored), "best": None, "mae": None, "fc3": None}
        fc3 = bt.scores[0]
        row["fc3"] = fc3.mae(bt.common) if fc3.n >= MIN_REPORTABLE else None
        if bt.ranking:
            best = bt.ranking[0]
            row["best"], row["mae"] = best.name, best.mae(bt.common)
        out.append(row)
    return out


def run(args) -> dict:
    eng = Engine.from_source(args.db)
    txns = eng.transactions
    if not txns:
        raise SystemExit(f"{args.db} has no transactions. Run:  "
                         f"python parse.py <PDFs> --db {args.db}")

    by_month = eng.by_month()
    complete = eng.complete_months()
    rng = eng.dates()

    full_cats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in eng.spend():
        full_cats[t.category][t.date[:7]] += t.amount
    full_cats = {c: dict(v) for c, v in full_cats.items()}

    if args.category:
        if args.category not in full_cats:
            near = [c for c in full_cats if c.lower().startswith(args.category.lower())]
            if len(near) != 1:
                raise SystemExit(f"No category '{args.category}'. Known: "
                                 f"{', '.join(sorted(full_cats))}")
            args.category = near[0]
        target = full_cats[args.category]
    else:
        target = by_month

    # Calendar months inside the range that carry no data at all. They are a
    # coverage hole, not a month of zero spending, so nothing scores them.
    gaps = []
    if by_month:
        m, last = min(by_month), max(by_month)
        while m < last:
            m = shift_month(m, 1)
            if m < last and m not in by_month:
                gaps.append(m)

    preds = build_predictors(args.min_history)
    bt = run_backtest(args.db, txns, complete, preds, target,
                      args.category, args.min_history)

    # How much of the committed+variable prediction was actually committed. When
    # this is near zero the predictor is a trailing mean with extra steps, and
    # saying so is more useful than letting it quietly tie.
    cmt = next((p for p in preds if isinstance(p, CommittedPlusVariable)), None)
    committed = None
    if cmt is not None and cmt.committed_share:
        last = Snapshot(args.db, txns, bt.scored[-1]) if bt.scored else None
        committed = {"share": sum(cmt.committed_share) / len(cmt.committed_share),
                     "recurring": len(last.engine.recurring()) if last else 0}

    meta = {
        "source": args.db, "n_txns": len(txns), "committed": committed,
        "first": rng[0].isoformat() if rng else "?",
        "last": rng[1].isoformat() if rng else "?",
        "n_present": len(by_month), "n_complete": len(complete),
        "gaps": gaps, "min_history": args.min_history,
    }
    cats = ([] if args.category else
            category_table(args.db, txns, complete, full_cats,
                           args.min_history, args.top_categories))
    planner = grade_planner(args.db, txns, eng, complete, args.min_history,
                            args.plan, args.income)
    planner.income = args.income
    eng.close()
    return {"meta": meta, "backtest": bt, "categories": cats, "planner": planner}


def main():
    ap = argparse.ArgumentParser(
        description="Score this project's forecasts against what actually happened.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"SQLite store or CSV (default {DEFAULT_DB})")
    ap.add_argument("--category", help="backtest one category's series instead of "
                                       "total spend")
    ap.add_argument("--min-history", type=int, default=3, metavar="N",
                    help="complete months a month needs in front of it before it is "
                         "scored at all (default 3)")
    ap.add_argument("--top-categories", type=int, default=8, metavar="N",
                    help="how many categories to grade individually (default 8)")
    ap.add_argument("--plan", default="plan.json",
                    help="plan file for the planner backtest (default plan.json)")
    ap.add_argument("--income", type=float, metavar="AMOUNT",
                    help="monthly take-home, when it does not appear in the "
                         "statements; without it the planner cannot be graded")
    ap.add_argument("-o", "--output", help="also write an HTML report here")
    ap.add_argument("--title", default="Forecast backtest")
    args = ap.parse_args()

    rep = run(args)
    print_report(rep)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(render_html(rep, args.title))
        print(f"  Wrote {args.output}\n")


if __name__ == "__main__":
    main()
