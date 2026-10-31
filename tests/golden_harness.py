"""
The golden-file harness for parse.py.

parse.py is the riskiest code in the project: it recovers money out of PDF
column layouts, and a column picked one x-coordinate to the left turns an $11
charge into a $2,158 running balance. There is no unit-testable seam in there,
so the safety net is a recording of what each real statement in ``PDFs/``
currently produces, kept in ``tests/golden/<name>.json``.

Two different jobs are done with that recording, and it matters which is which:

* :mod:`test_parser_golden` asserts the **structural contract** — things that
  must hold no matter how the extraction is fixed (a period, an issuer, a
  reconciliation verdict, non-negative charge and credit totals, transaction
  dates inside the statement period). Those are real assertions.
* The **numeric** fields (counts and totals) are a change detector, not a
  specification. The current parser output is known to be wrong, so a diff here
  is information, not a failure. See tests/README.md.

Regenerate with::

    .venv/bin/python tests/run.py --update-golden
"""

from __future__ import annotations

import json
import os

import support

GOLDEN_VERSION = 1

# Fields that describe extracted money. A diff in any of these is reported by
# the change-detector test rather than asserted, because the parser is being
# fixed and the recorded values are known-wrong today.
NUMERIC_FIELDS = ("transactions", "charges", "credits", "date_min", "date_max")


def golden_path(pdf_path: str) -> str:
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(support.GOLDEN_DIR, f"{name}.json")


def _money(v) -> float | None:
    return None if v is None else round(float(v), 2)


def verdict(txns, meta) -> str:
    """The reconciliation outcome for one statement: ok, mismatch or unchecked.

    Prefers the Statement's own ``reconciled`` flag once parse.py starts setting
    it, and falls back to comparing extracted charges against the purchases
    total the statement prints. Written this way so the harness keeps working
    across the reconciliation change currently in flight.
    """
    s = meta.get("statement")
    flag = getattr(s, "reconciled", None)
    if flag is not None:
        return "ok" if flag == 1 else "mismatch"

    printed = (meta.get("checks") or {}).get("total_purchases")
    if printed in (None, ""):
        return "unchecked"
    charges = sum(t.amount for t in txns if t.amount > 0)
    return "ok" if abs(abs(float(printed)) - charges) < 0.02 else "mismatch"


def record(pdf_path: str) -> dict:
    """Parse one PDF and reduce it to the facts worth recording."""
    import parse

    return record_from(pdf_path, *parse.parse_statement(pdf_path))


def record_from(pdf_path: str, txns, meta) -> dict:
    """Reduce an already-parsed statement to the facts worth recording.

    Deliberately does not record individual transactions: the row-level output
    churns constantly while the totals, the issuer and the reconciliation
    verdict are the things a regression would show up in. Split from
    :func:`record` so the test module can parse the corpus once and reuse it.
    """
    s = meta.get("statement")
    dates = sorted(t.date for t in txns if t.date)
    charges = sum(t.amount for t in txns if t.amount > 0)
    credits = sum(-t.amount for t in txns if t.amount < 0)

    return {
        "golden_version": GOLDEN_VERSION,
        "source_file": os.path.basename(pdf_path),
        "issuer": meta.get("issuer") or "",
        "last4": meta.get("last4") or "",
        "period": meta.get("period") or "",
        "period_start": getattr(s, "period_start", "") or "",
        "period_end": getattr(s, "period_end", "") or "",
        "pages": meta.get("pages"),
        "transactions": len(txns),
        "charges": _money(charges),
        "credits": _money(credits),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "statement_purchases": _money((meta.get("checks") or {}).get("total_purchases")),
        "statement_payments": _money((meta.get("checks") or {}).get("total_payments")),
        "statement_new_balance": _money((meta.get("checks") or {}).get("new_balance")),
        "reconciliation": verdict(txns, meta),
        "reconcile_diff": _money(getattr(s, "reconcile_diff", None)),
        "unmatched_lines": len(meta.get("unmatched") or []),
    }


def load(pdf_path: str) -> dict | None:
    p = golden_path(pdf_path)
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return json.load(fh)


def save(rec: dict, pdf_path: str) -> str:
    os.makedirs(support.GOLDEN_DIR, exist_ok=True)
    p = golden_path(pdf_path)
    with open(p, "w") as fh:
        json.dump(rec, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def diff(old: dict, new: dict, fields=NUMERIC_FIELDS) -> list[str]:
    """Human-readable list of the fields that moved."""
    out = []
    for f in fields:
        a, b = old.get(f), new.get(f)
        if a != b:
            out.append(f"{f}: {a!r} -> {b!r}")
    return out


def update_all(verbose: bool = True) -> int:
    """Re-record every PDF. Returns the number of files written."""
    pdfs = support.pdf_paths()
    if not pdfs:
        print(f"No PDFs found in {support.PDF_DIR}; nothing to record.")
        return 0
    if not support.has_pdfplumber():
        print("pdfplumber is not installed; cannot record goldens.")
        return 0

    written = 0
    for p in pdfs:
        name = os.path.basename(p)
        try:
            rec = record(p)
        except Exception as e:                      # a PDF that will not parse
            rec = {"golden_version": GOLDEN_VERSION,
                   "source_file": name,
                   "error": f"{type(e).__name__}: {e}"}
        old = load(p)
        save(rec, p)
        written += 1
        if verbose:
            if old is None:
                print(f"  new      {name}")
            else:
                changes = diff(old, rec, fields=sorted(set(old) | set(rec)))
                if changes:
                    print(f"  changed  {name}")
                    for c in changes:
                        print(f"             {c}")
                else:
                    print(f"  same     {name}")
    if verbose:
        print(f"\n{written} golden file(s) in {support.GOLDEN_DIR}")
    return written
