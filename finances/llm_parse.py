"""
llm_parse.py — a local model reads a statement layout the parser does not know,
and the statement's own totals decide whether to believe it.

parse.py knows a handful of issuers' layouts. On anything else it either finds
nothing or finds the wrong things, and reconciliation says so. This is what
happens next, when ``finances parse --llm`` is asked for: each page's text goes
to a model running on this machine, which lists the transaction rows it sees.

The model is never trusted on its own word. The statement's printed totals are
read by the deterministic summary reader, not by the model, so the model cannot
supply both the answer and the check. Its rows are kept only when they add up
to those printed totals, within the same tolerance a parsed statement gets.
Rows that do not add up are thrown away and the original verdict stands, unless
``--llm-accept-unverified`` says to keep them labelled as unverified.

Nothing leaves the machine: the endpoint is the same local server (Ollama,
LM Studio, llama.cpp) that ``finances ask`` and ``finances enrich`` use.
"""

from __future__ import annotations

import math
import os
from dataclasses import replace
from datetime import date, datetime

from finances.models import Statement, Transaction

SYSTEM = ("You extract transactions from bank and credit card statements into "
          "JSON. You copy what is printed; you never invent or total anything. "
          "Reply with JSON only.")

PROMPT = """Statement period: {period}. A date printed without a year falls inside this period.

Below is page {page} of {pages} of the statement's text. List every transaction row on this page: purchases, payments, refunds, fees, interest, deposits, withdrawals, transfers. Skip summary boxes, totals, balances, headings and rewards or points tables.

Reply with a JSON array, one object per row, in the order printed:
{{"date": "YYYY-MM-DD", "description": "as printed", "amount": 12.34, "direction": "out"}}
"amount" is always positive. "direction" is "out" for money spent, charged or withdrawn, and "in" for payments, refunds, credits and deposits.
If the page has no transaction rows, reply [].

PAGE TEXT:
{text}"""

MAX_PAGE_CHARS = 12000


def page_texts(path: str, password: str | None = None) -> list[str]:
    import pdfplumber
    kwargs = {"password": password} if password else {}
    with pdfplumber.open(path, **kwargs) as pdf:
        return [(p.extract_text() or "")[:MAX_PAGE_CHARS] for p in pdf.pages]


def rows_to_transactions(rows: list, statement: Statement, *,
                         start: date | None, end: date | None) -> tuple[list[Transaction], int]:
    """The model's rows as Transactions, dropping anything malformed.

    Returns the rows kept and how many were dropped. A date outside the
    statement period is re-yeared the way a parsed date is, and dropped when it
    still does not fit, because a row from another month is not this month's.
    """
    from finances.parse import categorize, clean_merchant, year_in_period

    kept, dropped = [], 0
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        try:
            d = datetime.strptime(str(r.get("date", ""))[:10], "%Y-%m-%d").date()
            amt = float(str(r.get("amount", "")).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            dropped += 1
            continue
        if not math.isfinite(amt) or abs(amt) > 1_000_000 or amt == 0:
            dropped += 1
            continue
        if end is not None:
            d = year_in_period(d, start, end)
            lo = (start or end).toordinal() - 45
            if not (lo <= d.toordinal() <= end.toordinal() + 5):
                dropped += 1
                continue
        desc = " ".join(str(r.get("description") or "").split())[:200]
        if not desc:
            dropped += 1
            continue
        direction = str(r.get("direction") or "out").lower()
        amount = abs(amt) if direction != "in" else -abs(amt)
        kept.append(Transaction(
            date=d.isoformat(), description=desc, amount=round(amount, 2),
            merchant=clean_merchant(desc), category=categorize(desc),
            issuer=statement.issuer, account_last4=statement.account_last4,
            statement_period=statement.statement_period,
            source_file=statement.source_file))
    return kept, dropped


def verify(txns: list[Transaction], statement: Statement, model: str) -> Statement:
    """Reconcile the model's rows against the printed totals, on a copy."""
    from finances.parse import reconcile

    st = replace(statement, reconciled=None, reconcile_diff=None, reconcile_note="")
    st.charges_extracted = round(sum(t.amount for t in txns if t.amount > 0), 2)
    st.credits_extracted = round(sum(-t.amount for t in txns if t.amount < 0), 2)
    reconcile(st, txns)
    st.source_format = "pdf+llm"
    lead = f"read by local model {model}"
    if st.reconciled == 1:
        st.reconcile_note = f"{lead}; agrees with the statement's printed totals"
    else:
        st.reconcile_note = f"{lead}; NOT verified: {st.reconcile_note}"
    return st


def needs_help(txns: list[Transaction], statement: Statement) -> bool:
    """Worth asking a model: nothing came out, or what came out is wrong."""
    return not txns or statement.reconciled == 0


def fallback(path: str, statement: Statement, *, endpoint: str | None = None,
             model: str | None = None, password: str | None = None,
             accept_unverified: bool = False, complete=None, log=print):
    """Try the model on one statement. Returns (txns, statement) to keep, or None.

    ``complete`` is injectable so tests can stand in for a model.
    """
    from finances.llm_enrich import extract_json_array

    if complete is None:
        from finances.ask import find_model
        from finances.llm_enrich import complete as _complete
        found = find_model(endpoint, model)
        if not found:
            log("    llm: no local model server found (start Ollama or LM Studio, "
                "or set FINANCES_LLM_ENDPOINT)")
            return None
        endpoint, model, where = found
        log(f"    llm: reading with {where}")

        def complete(prompt):
            return _complete(endpoint, model, prompt, system=SYSTEM)

    start = _parse_iso(statement.period_start)
    end = _parse_iso(statement.period_end)
    pages = page_texts(path, password)
    rows: list = []
    for i, text in enumerate(pages, 1):
        if not text.strip():
            continue
        try:
            reply = complete(PROMPT.format(period=statement.statement_period or "unknown",
                                           page=i, pages=len(pages), text=text))
        except Exception as e:                       # noqa: BLE001
            log(f"    llm: page {i} failed ({type(e).__name__}: {e})")
            return None
        got = extract_json_array(reply)
        if isinstance(got, list):
            rows.extend(got)

    txns, dropped = rows_to_transactions(rows, statement, start=start, end=end)
    if not txns:
        log("    llm: the model found no transaction rows")
        return None
    st = verify(txns, statement, model or "model")
    log(f"    llm: {len(txns)} rows" + (f" ({dropped} malformed, dropped)" if dropped else "")
        + f" -> {'reconciles' if st.reconciled == 1 else 'does not reconcile'}")
    if st.reconciled == 1 or accept_unverified:
        return txns, st
    log("    llm: discarded — rows that do not add up to the statement are not kept")
    return None


def _parse_iso(s: str) -> date | None:
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def enabled_by_env() -> bool:
    return os.environ.get("FINANCES_LLM_FALLBACK", "").lower() in ("1", "true", "yes")
