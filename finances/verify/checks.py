"""
Checks that need nothing but the inputs themselves.

    statement.balance       opening + every row = closing, in exact arithmetic
    statement.chain         each statement opens where the one before it closed
    coverage.gap            a stretch of time no statement covers
    inputs.overlap          the same rows in two files for one account (counted once)

Plus whatever a source's own reader already proved (printed totals on a PDF, a
CSV's running balance), carried through as a check of its own rather than
folded into one verdict.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from finances.verify import money
from finances.verify.adapters import make_check
from finances.verify.model import (INCOMPLETE, MISMATCH, UNVERIFIABLE, VERIFIED, Check,
                                   Finding, Period, Record, short_id)


def _subject(p: Period) -> dict:
    return {"account": p.account, "input": p.input_id,
            "period": {"start": p.start, "end": p.end}}


def statement_checks(periods: list[Period], records: dict[str, Record]) -> list[Check]:
    out = []
    for p in periods:
        if p.prior:
            status, rule, message = p.prior
            out.append(make_check(rule, status, message, subject=_subject(p),
                                  evidence=[p.input_id]))
        rows = [records[r] for r in p.records if r in records]
        if p.opening is not None and p.closing is not None and p.identity_meaningful:
            computed = p.opening + sum((r.amount for r in rows), money.ZERO)
            diff = computed - p.closing
            ok = diff == 0
            out.append(make_check(
                "statement.balance", VERIFIED if ok else MISMATCH,
                (f"opening {money.show(p.opening, p.currency)} plus {len(rows)} rows "
                 f"is {money.show(computed, p.currency)}"
                 + (", the closing balance" if ok else
                    f", but the closing balance is {money.show(p.closing, p.currency)}")),
                subject=_subject(p), evidence=[p.input_id], expected=p.closing,
                computed=computed, difference=diff, tolerance=money.ZERO,
                currency=p.currency))
        elif not p.prior:
            why = ("the file prints a closing balance only, so its rows cannot be "
                   "checked on their own; see statement.chain"
                   if p.opening is not None and not p.identity_meaningful else
                   "the source prints no opening and closing balance to check against")
            out.append(make_check("statement.balance", UNVERIFIABLE, why,
                                   subject=_subject(p), evidence=[p.input_id],
                                   currency=p.currency))
    return out


def chain_checks(periods: list[Period]) -> list[Check]:
    """Consecutive statements for one account must join up, in balance and in time."""
    out = []
    by_account = defaultdict(list)
    for p in periods:
        if p.start and p.end:
            by_account[(p.account, p.currency)].append(p)
    for (account, cur), ps in sorted(by_account.items()):
        ps.sort(key=lambda p: (p.end, p.start, p.input_id))
        for prev, nxt in zip(ps, ps[1:]):
            subject = {"account": account, "from": prev.input_id, "to": nxt.input_id}
            if prev.closing is not None and nxt.opening is not None and nxt.start >= prev.end:
                diff = nxt.opening - prev.closing
                ok = diff == 0
                out.append(make_check(
                    "statement.chain", VERIFIED if ok else MISMATCH,
                    (f"opens at {money.show(nxt.opening, cur)}"
                     + (", where the previous statement closed" if ok else
                        f" but the previous statement closed at {money.show(prev.closing, cur)}; "
                        f"{money.show(abs(diff), cur)} unaccounted for between "
                        f"{prev.end} and {nxt.start}")),
                    subject=subject, evidence=[prev.input_id, nxt.input_id],
                    expected=prev.closing, computed=nxt.opening, difference=diff,
                    tolerance=money.ZERO, currency=cur))
            if prev.declared and nxt.declared:
                gap = (date.fromisoformat(nxt.start) - date.fromisoformat(prev.end)).days
                if gap > 1:
                    out.append(make_check(
                        "coverage.gap", INCOMPLETE,
                        f"nothing covers {prev.end} to {nxt.start} ({gap - 1} days)",
                        subject=subject | {"gap": {"after": prev.end, "before": nxt.start}},
                        evidence=[prev.input_id, nxt.input_id], currency=cur))
    return out


def mark_overlap(records: list[Record], periods: list[Period],
                 order: list[str]) -> list[Finding]:
    """Rows a later file repeats from an earlier one for the same account.

    Downloads overlap: March 1–31 and March 15–April 15 share two weeks. The
    later file's copies are marked ``duplicate_of`` and left out of matching,
    counted by (account, date, amount) so two genuine identical charges on one
    day survive unless the earlier file already had both.
    """
    rank = {inp: i for i, inp in enumerate(order)}
    spans = {(p.input_id, p.account): (p.start, p.end) for p in periods}
    by_account = defaultdict(list)
    for r in records:
        by_account[(r.account, r.currency, r.role)].append(r)
    findings = []
    for (account, cur, _), rows in sorted(by_account.items()):
        inputs = sorted({r.input_id for r in rows}, key=lambda i: rank.get(i, 0))
        if len(inputs) < 2:
            continue
        seen: dict[str, list[Record]] = defaultdict(list)
        for inp in inputs:
            mine = [r for r in rows if r.input_id == inp]
            used = Counter()
            dupes = []
            for r in mine:
                earlier = [e for e in seen[r.content_key]
                           if _within(r.date, spans.get((e.input_id, account)))]
                if used[r.content_key] < len(earlier):
                    r.duplicate_of = earlier[used[r.content_key]].id
                    used[r.content_key] += 1
                    dupes.append(r)
            for r in mine:
                if not r.duplicate_of:
                    seen[r.content_key].append(r)
            if dupes:
                findings.append(Finding(
                    id=short_id("x", {"overlap": inp, "account": account}),
                    kind="overlap", severity="info",
                    message=(f"{len(dupes)} row(s) for {account} in this input were already "
                             f"in an earlier one covering the same days; counted once"),
                    records=[r.id for r in dupes], currency=cur))
    return findings


def _within(day: str, span) -> bool:
    if not span or not span[0] or not span[1]:
        return True
    return span[0] <= day <= span[1]
