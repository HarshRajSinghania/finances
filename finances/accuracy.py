"""
accuracy.py — how often the parser gets each bank right, from your own store.

Every statement says what its transactions add up to, so whether the parser
read one correctly is not a matter of opinion: it reconciled or it did not.
This counts, per bank, how many statement PDFs reconciled on the first pass
(the plain parser, no model, no OCR), how many needed a fallback to get there,
and how many did not reconcile or printed nothing to check.

The table contains only bank names and counts: no amounts, dates, account
numbers or file names, so it can be shared as it is.

    finances accuracy               a table in the terminal
    finances accuracy --markdown    the same, ready to paste into a README
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

from finances.database import DEFAULT_DB, FinanceDB


def table(statements) -> list[dict]:
    by: dict[str, dict] = defaultdict(lambda: {"statements": 0, "first": 0, "fallback": 0,
                                               "mismatch": 0, "unchecked": 0})
    for s in statements:
        if not s.checkable or not (s.source_format or "pdf").startswith("pdf"):
            continue
        row = by[s.issuer or "Unknown"]
        row["statements"] += 1
        if s.reconciled == 1:
            row["first" if (s.source_format or "pdf") == "pdf" else "fallback"] += 1
        elif s.reconciled == 0:
            row["mismatch"] += 1
        else:
            row["unchecked"] += 1
    out = []
    for bank, r in sorted(by.items(), key=lambda kv: (-kv[1]["statements"], kv[0])):
        checkable = r["statements"] - r["unchecked"]
        out.append({"bank": bank, **r,
                    "firstPct": round(100 * r["first"] / checkable, 1) if checkable else None})
    return out


def markdown(rows: list[dict]) -> str:
    lines = ["| Bank | Statements | Reconciled first try | With a fallback | Did not | No totals printed |",
             "|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        pct = f"{r['first']} ({r['firstPct']:.0f}%)" if r["firstPct"] is not None else "—"
        lines.append(f"| {r['bank']} | {r['statements']} | {pct} | {r['fallback']} | "
                     f"{r['mismatch']} | {r['unchecked']} |")
    total = sum(r["statements"] for r in rows)
    first = sum(r["first"] for r in rows)
    checkable = sum(r["statements"] - r["unchecked"] for r in rows)
    if checkable:
        lines.append(f"| **All** | {total} | {first} ({100 * first / checkable:.0f}%) | "
                     f"{sum(r['fallback'] for r in rows)} | {sum(r['mismatch'] for r in rows)} | "
                     f"{sum(r['unchecked'] for r in rows)} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances accuracy", description=__doc__.split("\n\n")[1])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--markdown", action="store_true", help="a Markdown table")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    with FinanceDB(args.db) as db:
        rows = table(db.statement_models())
    if not rows:
        print("No statement PDFs in this store yet.")
        return 1
    if args.json:
        print(json.dumps(rows, indent=1))
    elif args.markdown:
        print(markdown(rows))
    else:
        print(f"  {'bank':<20}{'PDFs':>6}{'first try':>12}{'fallback':>10}{'did not':>9}{'no totals':>11}")
        for r in rows:
            pct = f"{r['firstPct']:.0f}%" if r["firstPct"] is not None else "—"
            print(f"  {r['bank']:<20}{r['statements']:>6}{r['first']:>6} {pct:>5}"
                  f"{r['fallback']:>10}{r['mismatch']:>9}{r['unchecked']:>11}")
    return 0
