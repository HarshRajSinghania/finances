"""
VerificationEngine: add sources, run, get an evidence report.

    eng = VerificationEngine()
    eng.add("bank.csv", role="bank")
    eng.add("stripe_payouts.csv")                  # recognised as the processor
    eng.add("general_ledger.csv", role="ledger", account="Checking")
    report = eng.run()

Which pairs get matched follows from the roles present:

    bank ~ processor    each payout should arrive as one bank deposit
    bank ~ ledger       every bank movement should be in the books, and back
    processor ~ ledger  only when there is no bank data: payouts against the books

Reviewer decisions go in with ``decide()`` and come out in the report next to
the engine's own decision, which is never overwritten.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

from finances.verify import adapters, checks, money
from finances.verify.match import Matcher, Options
from finances.verify.model import (AMBIGUOUS, INCOMPLETE, MATCHED, NEEDS_REVIEW, REJECTED,
                                   UNRESOLVED,
                                   Finding, Input, Record, digest, short_id)
from finances.verify.report import Report

ROLES = ("bank", "processor", "ledger")


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("finances")
    except Exception:                                     # noqa: BLE001
        return "dev"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path: str) -> str:
    from finances.utils import file_sha256 as sha
    return sha(path)


class VerificationEngine:
    def __init__(self, *, currency: str = "USD", window_days: int = 3,
                 group_window_days: int = 7, group_size: int = 6,
                 tolerance: str | Decimal = "0", include_records: bool = True):
        self.currency = currency.upper()
        self.tolerance = money.amount(tolerance, self.currency)
        self.opts = Options(window_days=window_days, group_window_days=group_window_days,
                            group_size=group_size,
                            tolerance_minor=money.minor(self.tolerance, self.currency))
        self.include_records = include_records
        self.inputs: list[Input] = []
        self.batches: dict[str, adapters.Batch] = {}
        self.decisions: dict[str, dict] = {}

    # -- sources -----------------------------------------------------------
    def add(self, path: str, role: str | None = None, adapter: str | None = None,
            **options) -> Input:
        """Read one file. The adapter and role are worked out from the content
        when not given; options go to the adapter (``account=`` for a ledger,
        ``password=`` for a PDF, ``currency=`` for a file that does not say)."""
        if role and role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if adapter:
            fmt = {"export": options.pop("fmt", None) or "csv",
                   "ledger": "ledger-csv"}.get(adapter, adapter)
            role = role or ("ledger" if adapter == "ledger" else
                            "processor" if adapter == "stripe" else "bank")
        else:
            adapter, fmt, role = adapters.choose(path, role)
        read, media, source_class = adapters.ADAPTERS[adapter]
        sha = file_sha256(path)
        inp = Input(id=f"in{len(self.inputs) + 1}", name=os.path.basename(path), sha256=sha,
                    role=role, format=fmt, adapter=adapter,
                    adapter_version=adapters.VERSION, source_class=source_class,
                    media_type=media, options=_public(options), path=path)
        batch = read(path, inp, currency=options.pop("currency", None) or self.currency,
                     **options)
        inp.records = len(batch.records)
        self.inputs.append(inp)
        self.batches[inp.id] = batch
        return inp

    def add_rows(self, rows: list[dict], role: str, name: str = "rows", **options) -> Input:
        """Records that never were a file: dicts with at least date and amount."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        sha = digest([{k: (str(v) if not isinstance(v, (dict, list)) else v)
                       for k, v in r.items()} for r in rows])
        inp = Input(id=f"in{len(self.inputs) + 1}", name=name, sha256=sha, role=role,
                    format="rows", adapter="rows", adapter_version=adapters.VERSION,
                    source_class=adapters.INTERNAL if role == "ledger" else adapters.EXTERNAL,
                    media_type="application/json", options=_public(options))
        batch = adapters.read_rows(rows, inp, currency=options.get("currency") or self.currency)
        inp.records = len(batch.records)
        self.inputs.append(inp)
        self.batches[inp.id] = batch
        return inp

    def decide(self, match_id: str, decision: str, by: str, note: str = "",
               at: str | None = None):
        """A person's call on a match the engine sent to review."""
        if decision not in (MATCHED, REJECTED):
            raise ValueError("a reviewer can decide 'matched' or 'rejected'")
        self.decisions[match_id] = {"to": decision, "by": by, "note": note,
                                    "at": at or _now()}

    # -- running -------------------------------------------------------------
    def run(self) -> Report:
        started = _now()
        records = [r for i in self.inputs for r in self.batches[i.id].records]
        periods = [p for i in self.inputs for p in self.batches[i.id].periods]
        by_id = {r.id: r for r in records}

        findings = checks.mark_overlap(records, periods, [i.id for i in self.inputs])
        found = [c for i in self.inputs for c in self.batches[i.id].checks]
        found += checks.statement_checks(periods, by_id)
        found += checks.chain_checks(periods)

        roles = {i.role for i in self.inputs}
        by_role = {role: [r for r in records if r.role == role] for role in ROLES}
        payouts = [r for r in by_role["processor"] if r.kind == "payout"]
        matches = []
        settled: dict[str, Record] = {}         # bank record id -> the payout it is

        if "bank" in roles and "processor" in roles:
            m = Matcher("bank~processor", [r for r in by_role["bank"] if r.amount > 0],
                        payouts, self.opts)
            matches += m.run()
            for x in m.matches:
                if x.decision == MATCHED and len(x.left) == 1 and len(x.right) == 1:
                    settled[x.left[0]] = by_id[x.right[0]]
            # Every payout should have landed; a bank line nobody paid out is
            # normal (the bank holds everything else too).
            findings += self._leftovers("bank~processor", [], m.leftover[1],
                                        by_role["bank"], m.unresolved, found)

        if "bank" in roles and "ledger" in roles:
            m = Matcher("bank~ledger", by_role["bank"], by_role["ledger"], self.opts,
                        explain=self._fees(settled))
            matches += m.run()
            findings += self._leftovers("bank~ledger", *m.leftover, None, m.unresolved,
                                        found, both=(by_role["bank"], by_role["ledger"]))
        elif "processor" in roles and "ledger" in roles:
            m = Matcher("processor~ledger", payouts,
                        [r for r in by_role["ledger"] if r.amount > 0], self.opts,
                        explain=self._fees({p.id: p for p in payouts}))
            matches += m.run()
            findings += self._leftovers("processor~ledger", m.leftover[0], [],
                                        by_role["ledger"], m.unresolved, found)

        for m in matches:
            if m.id in self.decisions:
                d = self.decisions[m.id]
                m.override = {"from": m.decision, **d}
                m.decision = d["to"]
        findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2, "info": 3}[f.severity],
                                     -(abs(f.amount) if f.amount is not None else 0), f.id))
        return Report(engine={"name": "finances.verify", "version": _version()},
                      options={"currency": self.currency, **self.opts.to_dict()},
                      inputs=self.inputs, records=records, checks=found, matches=matches,
                      findings=findings, started=started, finished=_now(),
                      include_records=self.include_records)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _fees(payouts: dict[str, Record]):
        """Explain a difference by the processing fees on the payout behind it.

        The classic case: the processor paid out 252.00 after 8.00 of fees, the
        bank received 252.00, and the books recorded a 260.00 deposit — the
        sales before fees, with the fee expense never entered.
        """
        def explain(left: Record, right: Record, diff: Decimal) -> str:
            p = payouts.get(left.id) or (left if left.kind == "payout" else None)
            if not p or "fees" not in p.detail:
                return ""
            fees = p.detail["fees"]
            if fees and abs(diff) == abs(fees):
                po = p.refs.get("payout", p.id)
                verb = "more" if diff > 0 else "less"
                return (f"The difference, {money.show(abs(diff), p.currency)}, equals the "
                        f"processing fees on payout {po} ({p.detail.get('items', '?')} "
                        f"transactions). The books show {verb} than the bank received: "
                        f"the deposit looks recorded before fees. Check that the fee "
                        f"expense was entered before correcting it.")
            return ""
        return explain

    def _leftovers(self, pair: str, left: list[Record], right: list[Record],
                   context, unresolved: set[str], found: list, both=None) -> list[Finding]:
        """Unmatched records become findings, but only inside the dates both
        sides cover: a ledger that starts in January cannot be missing a bank
        line from December. What falls outside is not silently dropped; it is
        counted in a coverage check, so "nothing missing" never means "nothing
        compared"."""
        a, b = pair.split("~")
        if both:
            spans = [(min(r.date for r in rs), max(r.date for r in rs)) for rs in both if rs]
        else:
            spans = [(min(r.date for r in context), max(r.date for r in context))] \
                if context else []
        lo = max((s[0] for s in spans), default="")
        hi = min((s[1] for s in spans), default="9999")
        out, outside = [], []
        for side, missing_from, rows in ((a, b, left), (b, a, right)):
            for r in rows:
                if r.id in unresolved:
                    continue
                if not (lo <= r.date <= hi):
                    outside.append(r)
                    continue
                size = abs(r.amount)
                out.append(Finding(
                    id=short_id("x", {"pair": pair, "record": r.id}),
                    kind=f"missing_in_{missing_from}",
                    severity="high" if size >= 1000 else "medium" if size >= 100 else "low",
                    message=(f"{side} {r.date} {money.show(r.amount, r.currency)} "
                             f"“{r.description[:60]}” has no counterpart in the {missing_from}"),
                    records=[r.id], amount=r.amount, currency=r.currency, related=[pair],
                    locations=[{"input": r.input_id, **(r.source.locator if r.source else {})}]))
        if outside:
            span = f"{lo} to {hi}" if lo <= hi else "no dates at all"
            found.append(adapters.make_check(
                "coverage.pair", INCOMPLETE,
                f"{len(outside)} unmatched record(s) fall outside the dates both sides "
                f"cover ({span}) and could not be compared",
                subject={"pair": pair, "overlap": {"start": lo, "end": hi}},
                evidence=sorted({r.input_id for r in outside}),
                records=[r.id for r in outside]))
        return out


def _public(options: dict) -> dict:
    """Adapter options worth recording; secrets are not."""
    return {k: v for k, v in options.items() if k not in ("password",) and v not in (None, "")}


__all__ = ["VerificationEngine", "AMBIGUOUS", "MATCHED", "NEEDS_REVIEW", "UNRESOLVED"]
