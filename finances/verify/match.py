"""
Matching records across two sources, as a cascade.

    1. reference    a shared identifier (payout id, trace id, end-to-end id,
                    document number) on both sides
    2. exact        the same amount, in minor units, within a date window;
                    one-to-one per block, solved exactly rather than greedily
    3. group        one record on one side equals a small set on the other
                    (a payout and its charges, a deposit of several cheques):
                    bounded subset-sum on integer minor units
    4. near         what is left, paired with a record of almost the same
                    amount; never matched, always sent to review, with the
                    difference explained when something known accounts for it

Every decision says which tier made it, the block it searched, and why. The
design follows the record-linkage literature: a review band rather than a
forced choice (Fellegi & Sunter 1969), ties left unresolved rather than picked
(Sadinle 2017), one-to-one enforced globally inside a block (Jaro 1989), and a
subset search that admits when it ran out of budget instead of reporting "no
match" (Wu et al. 2025).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from finances.verify import money
from finances.verify.model import (AMBIGUOUS, MATCHED, NEEDS_REVIEW, UNRESOLVED, Match,
                                   Record, short_id)

REFERENCE_KEYS = ("payout", "trace_id", "end_to_end_id", "acct_svcr_ref", "doc_num")


@dataclass
class Options:
    window_days: int = 3            # booking dates may differ by this much
    group_window_days: int = 7      # charges settle up to a week before their payout
    group_size: int = 6             # most records one grouped match may hold
    group_pool: int = 24            # nearest candidates searched per group
    group_budget: int = 100_000     # search nodes per group before giving up
    tolerance_minor: int = 0        # accepted difference, in minor units
    near_ratio: Decimal = Decimal("0.10")   # how close counts as "near" for review
    reference_min_length: int = 6   # shorter ids collide too easily

    def to_dict(self) -> dict:
        return {k: (money.text(v) if isinstance(v, Decimal) else v)
                for k, v in self.__dict__.items()}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class Matcher:
    """Matches ``left`` against ``right``. Each record ends up in at most one match."""

    def __init__(self, pair: str, left: list[Record], right: list[Record],
                 opts: Options | None = None,
                 explain: Callable[[Record, Record, Decimal], str] | None = None):
        self.pair, self.opts = pair, opts or Options()
        key = lambda r: (r.date, r.id)                      # noqa: E731
        self.left = sorted((r for r in left if not r.duplicate_of), key=key)
        self.right = sorted((r for r in right if not r.duplicate_of), key=key)
        self.free = {r.id for r in self.left} | {r.id for r in self.right}
        self.explain = explain or (lambda a, b, d: "")
        self.matches: list[Match] = []
        self.unresolved: set[str] = set()

    # -- bookkeeping ----------------------------------------------------
    def _add(self, left, right, decision, tier, reasons, block=None, explanation="",
             consume=None):
        cur = (left or right)[0].currency
        diff = (sum((r.amount for r in right), money.ZERO)
                - sum((r.amount for r in left), money.ZERO))
        m = Match(id=short_id("m", {"pair": self.pair, "l": sorted(r.id for r in left),
                                    "r": sorted(r.id for r in right)}),
                  pair=self.pair, left=[r.id for r in left], right=[r.id for r in right],
                  decision=decision, tier=tier, reasons=reasons, difference=diff,
                  currency=cur, block=block or {}, explanation=explanation)
        self.matches.append(m)
        # Records in a match, even an undecided one, are out of the later tiers:
        # a tie sent to review must not be quietly settled by a looser rule.
        for r in (consume if consume is not None else (*left, *right)):
            self.free.discard(r.id)
        return m

    def _open(self, rows):
        return [r for r in rows if r.id in self.free]

    def run(self) -> list[Match]:
        self.by_reference()
        self.exact()
        self.groups(self.left, self.right, one_on_left=True)
        self.groups(self.right, self.left, one_on_left=False)
        self.near()
        return self.matches

    @property
    def leftover(self) -> tuple[list[Record], list[Record]]:
        return self._open(self.left), self._open(self.right)

    # -- 1. shared references -----------------------------------------------
    def by_reference(self):
        n = self.opts.reference_min_length
        index = {}
        for rows in (self.left, self.right):
            for r in rows:
                for k in REFERENCE_KEYS:
                    v = str(r.refs.get(k) or "").strip()
                    if len(v) >= n:
                        index.setdefault(v.lower(), v)

        def mentions(r: Record, ref: str) -> bool:
            return ref in _norm(r.description) or ref in {str(v).lower() for v in r.refs.values()}

        for ref in sorted(index):
            shown = index[ref]
            ls = [r for r in self._open(self.left) if mentions(r, ref)]
            rs = [r for r in self._open(self.right) if mentions(r, ref)]
            if len(ls) != 1 or len(rs) != 1:
                continue
            l, r = ls[0], rs[0]
            if l.currency != r.currency:
                continue
            diff = r.amount - l.amount
            within = abs(money.minor(diff, l.currency)) <= self.opts.tolerance_minor
            reasons = [f"both carry reference {shown}"]
            if within:
                reasons.append("amounts equal" if diff == 0 else
                               f"amounts within tolerance ({money.text(diff)})")
                self._add([l], [r], MATCHED, "reference", reasons, {"reference": shown})
            else:
                why = self.explain(l, r, diff)
                reasons.append(f"amounts differ by {money.show(diff, l.currency)}")
                self._add([l], [r], NEEDS_REVIEW, "reference", reasons,
                          {"reference": shown}, why)

    # -- 2. exact amount, nearest dates ------------------------------------
    def exact(self):
        w = self.opts.window_days
        blocks = defaultdict(lambda: ([], []))
        for r in self._open(self.left):
            blocks[(r.currency, r.minor)][0].append(r)
        for r in self._open(self.right):
            blocks[(r.currency, r.minor)][1].append(r)
        for (cur, units), (ls, rs) in sorted(blocks.items()):
            if not ls or not rs:
                continue
            block = {"currency": cur, "amount": money.text(money.from_minor(units, cur)),
                     "window_days": w}
            for l, r in self._assign(ls, rs, w):
                dt = abs(_days(r.date, l.date))
                reasons = ["amounts equal", f"dates {dt} day(s) apart (window {w})"]
                rivals = self._rivals(l, r, ls, rs, dt, w)
                if rivals:
                    reasons.append(f"{rivals} other record(s) fit equally well")
                    self._add([l], [r], AMBIGUOUS, "exact", reasons, block)
                else:
                    self._add([l], [r], MATCHED, "exact", reasons, block)

    @staticmethod
    def _assign(ls, rs, w):
        """Most pairs, then least total date distance, one-to-one.

        Both sides are sorted by date, and for points on a line the optimal
        pairing never crosses, so a DP over the two sorted lists is exact.
        """
        n, m = len(ls), len(rs)
        best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                opt = max(best[i + 1][j], best[i][j + 1])
                dt = abs(_days(rs[j].date, ls[i].date))
                if dt <= w:
                    c, cost = best[i + 1][j + 1]
                    opt = max(opt, (c + 1, cost - dt))
                best[i][j] = opt
        pairs, i, j = [], 0, 0
        while i < n and j < m:
            dt = abs(_days(rs[j].date, ls[i].date))
            if dt <= w and best[i][j] == (best[i + 1][j + 1][0] + 1,
                                          best[i + 1][j + 1][1] - dt):
                pairs.append((ls[i], rs[j]))
                i, j = i + 1, j + 1
            elif best[i][j] == best[i + 1][j]:
                i += 1
            else:
                j += 1
        return pairs

    @staticmethod
    def _rivals(l, r, ls, rs, dt, w) -> int:
        """Other records that fit as well and are not interchangeable copies."""
        count = 0
        for other in rs:
            if other is not r and abs(_days(other.date, l.date)) == dt and (
                    other.date != r.date or _norm(other.description) != _norm(r.description)):
                count += 1
        for other in ls:
            if other is not l and abs(_days(r.date, other.date)) == dt and (
                    other.date != l.date or _norm(other.description) != _norm(l.description)):
                count += 1
        return count

    # -- 3. one record against a set ---------------------------------------
    def groups(self, ones: list[Record], many: list[Record], one_on_left: bool):
        o = self.opts
        for t in sorted(self._open(ones), key=lambda r: (-abs(r.amount), r.date, r.id)):
            if t.id not in self.free:
                continue
            pool = [c for c in self._open(many) if c.currency == t.currency
                    and -o.group_window_days <= _days(c.date, t.date) <= o.window_days]
            if len(pool) < 2:
                continue
            pool.sort(key=lambda c: (abs(_days(c.date, t.date)), c.date, c.id))
            pool = pool[:o.group_pool]
            found, exhausted = subset_sums(t.minor, [(c.minor, c) for c in pool],
                                           o.group_size, o.group_budget)
            block = {"currency": t.currency, "candidates": len(pool),
                     "max_group": o.group_size,
                     "window_days": [-o.group_window_days, o.window_days]}
            if len(found) == 1:
                members = found[0]
                reasons = [f"{len(members)} records sum exactly to "
                           f"{money.show(t.amount, t.currency)}",
                           "no other combination of the candidates does"]
                left, right = ([t], members) if one_on_left else (members, [t])
                self._add(left, right, MATCHED, "group", reasons, block)
            elif len(found) > 1:
                left, right = ([t], found[0]) if one_on_left else (found[0], [t])
                self._add(left, right, AMBIGUOUS, "group",
                          ["more than one combination sums to the same amount"], block,
                          consume=[t])
            elif exhausted:
                self.unresolved.add(t.id)
                left, right = ([t], []) if one_on_left else ([], [t])
                self._add(left, right, UNRESOLVED, "group",
                          [f"search stopped after {o.group_budget:,} steps without "
                           "proving there is no combination"], block, consume=[t])

    # -- 4. near misses, for review ----------------------------------------
    def near(self):
        o, w = self.opts, self.opts.window_days
        options = []
        for l in self._open(self.left):
            for r in self._open(self.right):
                if r.currency != l.currency or (r.amount > 0) != (l.amount > 0):
                    continue
                dt = abs(_days(r.date, l.date))
                if dt > w:
                    continue
                diff = r.amount - l.amount
                units = abs(money.minor(diff, l.currency))
                if units <= o.tolerance_minor:
                    options.append((0, 0, units, dt, l, r, diff, ""))
                    continue
                why = self.explain(l, r, diff)
                if why or abs(diff) <= abs(l.amount) * o.near_ratio:
                    options.append((1, 0 if why else 1, units, dt, l, r, diff, why))
        for rank, _, units, dt, l, r, diff, why in sorted(
                options, key=lambda x: (x[0], x[1], x[2], x[3], x[4].id, x[5].id)):
            if l.id not in self.free or r.id not in self.free:
                continue
            block = {"currency": l.currency, "window_days": w}
            if rank == 0:
                self._add([l], [r], MATCHED, "tolerance",
                          [f"amounts within tolerance ({units} minor units)",
                           f"dates {dt} day(s) apart"], block)
            else:
                self._add([l], [r], NEEDS_REVIEW, "explained" if why else "near",
                          [f"amounts differ by {money.show(diff, l.currency)}",
                           f"dates {dt} day(s) apart"], block, why)


def subset_sums(target: int, items: list[tuple[int, object]], k: int,
                budget: int) -> tuple[list[list], bool]:
    """Up to two subsets of 2..k items summing exactly to ``target``.

    Depth-first over items in descending size, pruned by what the remaining
    positives and negatives could still add. Returns (solutions, exhausted):
    stopping at two is enough to know a match is ambiguous, and ``exhausted``
    means the budget ran out, which proves nothing either way.
    """
    items = sorted(items, key=lambda x: (-abs(x[0]), getattr(x[1], "id", "")))
    n = len(items)
    pos = [0] * (n + 1)
    neg = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        v = items[i][0]
        pos[i] = pos[i + 1] + max(v, 0)
        neg[i] = neg[i + 1] + min(v, 0)
    found: list[list] = []
    nodes = 0
    chosen: list[int] = []

    def dfs(i: int, total: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > budget:
            return False
        if total == target and len(chosen) >= 2:
            found.append([items[c][1] for c in chosen])
            return len(found) < 2
        if i == n or len(chosen) == k:
            return True
        if total + pos[i] < target or total + neg[i] > target:
            return True
        chosen.append(i)
        go = dfs(i + 1, total + items[i][0])
        chosen.pop()
        return go and dfs(i + 1, total)

    dfs(0, 0)
    return found, nodes > budget
