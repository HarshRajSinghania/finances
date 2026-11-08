"""
Adapters: one per kind of source, all producing the same canonical records.

An adapter reads a file and returns three things: the records, what the file
says about each account's balances over its period, and any checks only it can
run (a Stripe row's gross − fee = net, a ledger's running balance). It never
decides anything about other files; that is the engine's job.

    statement PDFs      the app's PDF parser (pdfplumber)
    bank exports        CSV, OFX/QFX, CAMT.053, MT940 via the app's importer
    stripe              itemized payout reconciliation and balance-history CSVs
    ledger              accounting exports: QuickBooks bank-import and general
                        ledger, Xero, or any CSV with a date and an amount

Writing your own: a callable ``read(path, inp, **opts) -> Batch`` registered in
``ADAPTERS``. Records are built with ``Builder`` so ids come out the same way.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from finances.verify import money
from finances.verify.model import (EXTERNAL, INTERNAL, MISMATCH, UNVERIFIABLE, VERIFIED,
                                   Check, Input, Period, Record, SourceRef, digest,
                                   short_id)

VERSION = "1.0.0"


@dataclass
class Batch:
    records: list[Record] = field(default_factory=list)
    periods: list[Period] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


def make_check(rule: str, status: str, message: str, *, subject: dict | None = None,
               evidence: list[str] | None = None, **kw) -> Check:
    subject = subject or {}
    return Check(id=short_id("chk", {"rule": rule, "subject": subject,
                                     "evidence": evidence or []}),
                 rule=rule, status=status, message=message, subject=subject,
                 evidence=evidence or [], **kw)


class Builder:
    """Turns raw rows into records with stable, content-derived ids.

    ``content_key`` (account, date, amount, currency) is what matching compares;
    descriptions are left out on purpose because every source words them
    differently. ``id`` adds the input's digest and which occurrence of that key
    this is, so two identical coffees on one morning stay two records and the
    same file always produces the same ids.
    """

    def __init__(self, inp: Input, default_currency: str):
        self.inp = inp
        self.currency = default_currency
        self.seen: Counter = Counter()
        self.records: list[Record] = []

    def add(self, *, account: str, date: str, amount, currency: str = "",
            description: str = "", kind: str = "", refs: dict | None = None,
            dates: dict | None = None, detail: dict | None = None, group: str = "",
            locator: dict | None = None, raw: dict | None = None, sign: str = "",
            method: str = "deterministic") -> Record:
        cur = (currency or self.currency or "").upper()
        amt = money.amount(amount, cur)
        key = digest({"account": account, "date": date, "amount": money.text(amt),
                      "currency": cur})[:16]
        n = self.seen[key]
        self.seen[key] += 1
        rec = Record(
            id=short_id("rec", {"input": self.inp.sha256, "key": key, "n": n}),
            content_key=key, input_id=self.inp.id, role=self.inp.role, account=account,
            date=date, amount=amt, currency=cur,
            description=re.sub(r"\s+", " ", description or "").strip(),
            kind=kind, refs={k: v for k, v in (refs or {}).items() if v},
            dates={k: v for k, v in (dates or {}).items() if v},
            detail=detail or {}, group=group,
            source=SourceRef(self.inp.id, locator or {}, raw or {}, sign, method))
        self.records.append(rec)
        return rec


# --------------------------------------------------------------------------
# Statement PDFs
# --------------------------------------------------------------------------

def read_pdf(path: str, inp: Input, *, currency: str = "USD", password: str = "",
             **_) -> Batch:
    from finances import parse                     # pdfplumber is imported here

    txns, meta = parse.parse_statement(path, password=password or None)
    st, refs = meta["statement"], meta.get("refs") or []
    account = _account_label(st.issuer, st.account_last4, inp.name)
    b = Builder(inp, currency)
    for i, t in enumerate(txns):
        ref = refs[i] if len(refs) == len(txns) else {}
        b.add(account=account, date=t.date, amount=-money.amount(t.amount, currency),
              description=t.description, dates={"posted": t.post_date},
              locator={k: ref[k] for k in ("page", "line") if k in ref},
              raw=ref.get("raw") or {}, sign="inverted")
    if st.reconciled == 1:
        prior = (VERIFIED, "statement.printed_totals",
                 "extracted charges and credits equal the totals printed on the statement")
    elif st.reconciled == 0:
        prior = (MISMATCH, "statement.printed_totals", st.reconcile_note)
    else:
        prior = (UNVERIFIABLE, "statement.printed_totals",
                 st.reconcile_note or "the statement prints no totals to check against")
    period = Period(inp.id, account, currency.upper(), st.period_start, st.period_end,
                    declared=bool(st.period_start), records=[r.id for r in b.records],
                    prior=prior)
    return Batch(b.records, [period])


# --------------------------------------------------------------------------
# Bank exports: CSV, OFX/QFX, CAMT.053, MT940
# --------------------------------------------------------------------------

def read_export(path: str, inp: Input, *, currency: str = "USD", fmt: str | None = None,
                **opts) -> Batch:
    from finances import importer

    keep = {k: v for k, v in opts.items()
            if k in ("account", "issuer", "kind", "date_fmt", "sign") and v}
    accounts = importer.read_file(path, fmt or inp.format, **keep)
    b, periods = Builder(inp, currency), []
    for acct in accounts:
        cur = (acct.currency or currency).upper()
        account = _account_label(acct.issuer, acct.account, acct.name)
        first = len(b.records)
        for t in acct.rows:
            ref = acct.ref(t)
            locator = {k: ref[k] for k in ("record", "entry", "fitid", "trntype",
                                           "acct_svcr_ref", "end_to_end_id") if k in ref}
            b.add(account=account, date=t.date, amount=-money.amount(t.amount, cur),
                  currency=t.currency or cur, description=t.description,
                  dates={"posted": t.post_date},
                  refs={k: ref[k] for k in ("fitid", "acct_svcr_ref", "end_to_end_id")
                        if k in ref},
                  locator=locator, raw=ref.get("raw") or {},
                  # The importer records its own flip (to spending-positive); the
                  # engine's convention is the opposite of that one.
                  sign={"as written": "inverted", "inverted": "as written"}.get(
                      ref.get("sign", ""), ""))
        holder = (lambda v: None if v is None else
                  money.amount(-v if acct.kind == "card" else v, cur))
        prior = None
        if acct.fmt == "csv" and acct.checked is not None:
            prior = ((VERIFIED, "statement.running_balance",
                      "every row's printed balance follows from the row before it")
                     if acct.checked else
                     (MISMATCH, "statement.running_balance", acct.note))
        periods.append(Period(
            inp.id, account, cur,
            acct.start.isoformat() if acct.start else "",
            acct.end.isoformat() if acct.end else "",
            declared=acct.fmt in ("ofx", "camt053", "mt940"),
            opening=holder(acct.opening), closing=holder(acct.closing),
            records=[r.id for r in b.records[first:]], prior=prior,
            # An OFX file prints a closing balance only; its opening is worked
            # back from the rows, so opening + rows = closing proves nothing.
            identity_meaningful=acct.fmt != "ofx"))
    return Batch(b.records, periods)


# --------------------------------------------------------------------------
# CSV plumbing shared by the Stripe and ledger adapters
# --------------------------------------------------------------------------

def _norm(h: str) -> str:
    h = re.sub(r"\((utc|in business currency)\)", "", h.lower())
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def _table(path: str) -> list[list[str]]:
    from finances import importer
    text = importer.read_text(path)
    return list(csv.reader(io.StringIO(text), delimiter=importer.sniff_delimiter(text)))


def _find_header(table, aliases: dict, need) -> tuple[int, dict]:
    for i, row in enumerate(table[:40]):
        cols = {}
        names = [_norm(c) for c in row]
        for role, options in aliases.items():
            for opt in options:
                if opt in names:
                    cols[role] = names.index(opt)
                    break
        if need(cols):
            return i, cols
    return -1, {}


def _stamp(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.isdigit() and len(raw) >= 9:            # a Unix timestamp
        return datetime.fromtimestamp(int(raw), timezone.utc).date().isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# Stripe
# --------------------------------------------------------------------------

STRIPE_COLUMNS = {
    "id": ["balance_transaction_id", "id"],
    "type": ["type"],
    "category": ["reporting_category"],
    "gross": ["gross", "amount"],
    "fee": ["fee"],
    "net": ["net"],
    "currency": ["currency"],
    "created": ["created", "created_utc"],
    "available_on": ["available_on", "available_on_utc"],
    "payout": ["automatic_payout_id", "payout_id", "payout", "transfer"],
    "payout_date": ["automatic_payout_effective_at", "automatic_payout_effective_at_utc"],
    "description": ["description"],
    "source": ["source_id", "source", "charge_id"],
    "trace": ["trace_id"],
}


def _stripe_header(cols) -> bool:
    return {"fee", "net"} <= cols.keys() and bool(
        {"payout", "category", "type"} & cols.keys())


def sniff_stripe(path: str) -> bool:
    try:
        return _find_header(_table(path), STRIPE_COLUMNS, _stripe_header)[0] >= 0
    except (OSError, UnicodeDecodeError, csv.Error, ValueError):
        return False


def read_stripe(path: str, inp: Input, *, currency: str = "USD", **_) -> Batch:
    """Balance transactions grouped into payouts.

    Every row becomes a record (its ``net``, from the merchant's side), tagged
    with the payout it settled in. Each payout then becomes a record of its own
    — what should arrive at the bank — either from its own ``payout`` row or,
    in the itemized report where payouts have no row, as the sum of its items.
    Those payout records are what bank deposits are matched against; the items
    carry the fees that explain a ledger that booked the gross.
    """
    table = _table(path)
    at, cols = _find_header(table, STRIPE_COLUMNS, _stripe_header)
    if at < 0:
        raise ValueError("not a Stripe balance or payout reconciliation export")

    def col(role, r):
        i = cols.get(role)
        return r[i].strip() if i is not None and i < len(r) else ""

    b = Builder(inp, currency)
    bad_rows, payout_rows, items = [], {}, defaultdict(list)
    for n, r in enumerate(table[at + 1:], start=at + 2):
        if not any(c.strip() for c in r):
            continue
        cur = (col("currency", r) or currency).upper()
        try:
            gross, fee, net = (money.amount(col(k, r) or "0", cur)
                               for k in ("gross", "fee", "net"))
        except ValueError:
            continue
        if gross - fee != net and gross + fee != net:
            bad_rows.append(n)
        kind = (col("category", r) or col("type", r)).lower()
        when = _stamp(col("created", r)) or _stamp(col("available_on", r))
        po = col("payout", r)
        if kind == "payout" or col("type", r).lower() == "payout":
            # In the balance history the payout is a row of its own: its id is a
            # txn_…, the payout's po_… is its source.
            src = col("source", r)
            key = src if src.startswith("po_") else (po or col("id", r))
            payout_rows[key] = (n, -net, cur, when, col("trace", r), col("description", r))
            continue
        rec = b.add(account=f"stripe {cur}", date=when, amount=net, currency=cur,
                    description=col("description", r) or kind, kind=kind,
                    refs={"balance_transaction": col("id", r), "source": col("source", r),
                          "payout": po, "trace_id": col("trace", r)},
                    dates={"available_on": _stamp(col("available_on", r)),
                           "payout_date": _stamp(col("payout_date", r))},
                    detail={"gross": gross, "fee": fee}, group=po,
                    locator={"record": n},
                    raw={k: col(k, r) for k in ("gross", "fee", "net")}, sign="as written")
        if po:
            items[po].append(rec)

    checks = []
    subject = {"input": inp.id}
    if bad_rows:
        checks.append(make_check(
            "processor.row_arithmetic", MISMATCH,
            f"{len(bad_rows)} row(s) where gross − fee ≠ net", subject=subject,
            evidence=[inp.id], locations=[{"input": inp.id, "record": n} for n in bad_rows]))
    else:
        checks.append(make_check(
            "processor.row_arithmetic", VERIFIED,
            "gross − fee = net on every row", subject=subject, evidence=[inp.id]))

    for po in sorted(set(items) | set(payout_rows)):
        members = items.get(po, [])
        cur = members[0].currency if members else payout_rows[po][2]
        total = sum((m.amount for m in members), money.ZERO)
        fees = sum((m.detail["fee"] for m in members), money.ZERO)
        gross = sum((m.detail["gross"] for m in members), money.ZERO)
        if po in payout_rows:
            n, amount, cur, when, trace, desc = payout_rows[po]
            locator = {"record": n}
            if members:
                status = VERIFIED if total == amount else MISMATCH
                checks.append(make_check(
                    "processor.payout_total", status,
                    f"payout {po}: its {len(members)} balance transactions net to "
                    f"{money.show(total, cur)}"
                    + ("" if status == VERIFIED else f", but the payout is {money.show(amount, cur)}"),
                    subject={"payout": po}, evidence=[inp.id], expected=amount,
                    computed=total, difference=total - amount, currency=cur,
                    records=[m.id for m in members]))
        else:
            amount, desc = total, ""
            trace = next((m.refs["trace_id"] for m in members if m.refs.get("trace_id")), "")
            when = max((m.dates.get("payout_date") or m.dates.get("available_on")
                        or m.date for m in members), default="")
            locator = {"records": [m.source.locator.get("record") for m in members]}
        b.add(account=f"stripe {cur}", date=when, amount=amount, currency=cur,
              description=desc or f"Stripe payout {po}", kind="payout",
              refs={"payout": po, "trace_id": trace},
              detail={"gross": gross, "fees": fees, "items": len(members),
                      "derived": po not in payout_rows},
              group=po, locator=locator, sign="as written")
    return Batch(b.records, [], checks)


# --------------------------------------------------------------------------
# Accounting ledgers
# --------------------------------------------------------------------------

LEDGER_COLUMNS = {
    "date": ["date", "tx_date", "transaction_date", "txn_date", "posting_date", "date_posted"],
    "amount": ["amount", "subt_nat_amount", "signed_amount", "net_amount"],
    "debit": ["debit", "debt_amt", "debit_amount", "money_in", "spent"],
    "credit": ["credit", "credit_amt", "credit_amount", "money_out", "received"],
    "description": ["memo_description", "description", "memo", "details", "narrative",
                    "line_description"],
    "payee": ["name", "payee", "contact", "customer_vendor"],
    "reference": ["num", "no", "doc_num", "reference", "ref", "cheque_number",
                  "check_number", "check_no", "transaction_id", "journal_id"],
    "account": ["account", "account_name", "distribution_account", "gl_account"],
    "type": ["transaction_type", "txn_type", "type"],
    "balance": ["balance", "rbal_nat_amount", "running_balance"],
    "currency": ["currency"],
}


def _ledger_header(cols) -> bool:
    return "date" in cols and ("amount" in cols or {"debit", "credit"} <= cols.keys())


def read_ledger(path: str, inp: Input, *, currency: str = "USD", account: str = "",
                sign: str = "auto", date_fmt: str = "", **_) -> Batch:
    """An accounting system's view of one bank account.

    A single signed amount is read as written: QuickBooks and Xero both write
    money into the account as positive. Debit and credit columns are read the
    way an asset account works: debit in, credit out. ``sign="inverted"`` flips
    either. A general-ledger export carries every account; name the bank
    account with ``account=`` (a section heading or the Account column).
    """
    from finances import importer

    table = _table(path)
    at, cols = _find_header(table, LEDGER_COLUMNS, _ledger_header)
    if at < 0:
        raise ValueError("no header with a date and an amount (or debit and credit) column")

    def col(role, r):
        i = cols.get(role)
        return r[i].strip() if i is not None and i < len(r) else ""

    body = [(n, r) for n, r in enumerate(table[at + 1:], start=at + 2)
            if any(c.strip() for c in r)]
    money_cols = [k for k in ("amount", "debit", "credit", "balance") if k in cols]
    comma = importer.decimal_comma([col(k, r) for _, r in body for k in money_cols])
    # Section headings and "Total for …" lines sit in the date column too.
    fmt = date_fmt or importer.date_format(
        [col("date", r) for _, r in body if re.match(r"^\d", col("date", r))],
        prefer_dmy=comma)
    if not fmt:
        raise ValueError("could not tell the date format; pass date_fmt")

    rows, section = [], ""
    for n, r in body:
        filled = [c.strip() for c in r if c.strip()]
        d = importer.to_date(col("date", r), fmt)
        if d is None:
            # QuickBooks' general ledger names each account in a row of its own
            # and closes it with "Total for …".
            if len(filled) == 1 and not filled[0].lower().startswith("total"):
                section = filled[0]
            continue
        if "amount" in cols and col("amount", r):
            v = importer.parse_amount(col("amount", r), comma)
        else:
            dr = importer.parse_amount(col("debit", r), comma)
            cr = importer.parse_amount(col("credit", r), comma)
            if dr is None and cr is None:
                continue
            v = abs(dr or 0.0) - abs(cr or 0.0)
        if v is None:
            continue
        acct = col("account", r) or section or "ledger"
        rows.append((n, r, d, v, acct))

    names = sorted({a for *_, a in rows})
    if account:
        rows = [x for x in rows if account.lower() in x[4].lower()]
        if not rows:
            raise ValueError(f"no rows for account {account!r}; the file has: "
                             + ", ".join(names))
    elif len(names) > 1:
        raise ValueError("this ledger covers several accounts (" + ", ".join(names[:8])
                         + "); pick the bank account with account=")

    flip = -1 if sign == "inverted" else 1
    b = Builder(inp, currency)
    balances, chain_breaks = [], []
    for n, r, d, v, acct in rows:
        cur = (col("currency", r) or currency).upper()
        amt = money.amount(v * flip, cur)
        payee, memo = col("payee", r), col("description", r)
        desc = memo if not payee or payee.lower() in memo.lower() else f"{payee} {memo}"
        rec = b.add(account=acct, date=d.isoformat(), amount=amt, currency=cur,
                    description=desc or col("type", r), kind=col("type", r).lower(),
                    refs={"doc_num": col("reference", r)}, locator={"record": n},
                    raw={k: col(k, r) for k in ("amount", "debit", "credit", "balance")
                         if k in cols},
                    sign="inverted" if flip < 0 else "as written")
        bal = importer.parse_amount(col("balance", r), comma) if "balance" in cols else None
        if bal is not None:
            bal = money.amount(bal, cur)
            if balances and balances[-1][1] + amt != bal:
                chain_breaks.append(n)
            balances.append((rec, bal))

    prior = None
    if len(balances) >= 2:
        prior = ((VERIFIED, "ledger.running_balance",
                  "every row's balance follows from the row before it")
                 if not chain_breaks else
                 (MISMATCH, "ledger.running_balance",
                  f"the running balance breaks at {len(chain_breaks)} row(s): "
                  + ", ".join(map(str, chain_breaks[:6]))))
    recs = b.records
    period = Period(inp.id, recs[0].account if recs else account or "ledger",
                    recs[0].currency if recs else currency.upper(),
                    min((x.date for x in recs), default=""),
                    max((x.date for x in recs), default=""),
                    opening=(balances[0][1] - balances[0][0].amount) if balances else None,
                    closing=balances[-1][1] if balances else None,
                    records=[x.id for x in recs], prior=prior)
    return Batch(recs, [period])


# --------------------------------------------------------------------------
# Rows handed over in code
# --------------------------------------------------------------------------

def read_rows(rows: list[dict], inp: Input, *, currency: str = "USD", **_) -> Batch:
    """For developers whose data never was a file: dicts with date and amount.

    Optional keys: account, currency, description, kind, refs, group. Amounts
    follow the engine's convention (money in positive) and should be strings
    or Decimals; floats are accepted and converted through ``str``.
    """
    b = Builder(inp, currency)
    for i, r in enumerate(rows):
        b.add(account=r.get("account") or inp.name, date=str(r["date"])[:10],
              amount=r["amount"], currency=r.get("currency") or currency,
              description=r.get("description", ""), kind=r.get("kind", ""),
              refs=r.get("refs") or {}, group=r.get("group", ""),
              locator={"index": i}, raw={"amount": str(r["amount"])}, sign="as written")
    return Batch(b.records, [])


def _account_label(issuer: str, last4: str, fallback: str) -> str:
    """The account's identity across files: its last four digits when known.

    Not the issuer name: one bank's OFX names itself and the next download may
    not, and both are the same account that has to chain.
    """
    if last4:
        return f"…{last4}"
    return issuer or fallback


# --------------------------------------------------------------------------
# Choosing one
# --------------------------------------------------------------------------

ADAPTERS = {
    "pdf": (read_pdf, "application/pdf", EXTERNAL),
    "export": (read_export, "text/plain", EXTERNAL),
    "stripe": (read_stripe, "text/csv", EXTERNAL),
    "ledger": (read_ledger, "text/csv", INTERNAL),
    "rows": (read_rows, "application/json", EXTERNAL),
}


def choose(path: str, role: str | None) -> tuple[str, str, str]:
    """(adapter, format, role) for a file, from its content more than its name."""
    from finances import importer

    if path.lower().endswith(".pdf"):
        return "pdf", "pdf", role or "bank"
    fmt = importer.detect_format(path)
    if fmt in ("ofx", "camt053", "mt940", *importer.APPS):
        return "export", fmt, role or "bank"
    if sniff_stripe(path):
        return "stripe", "stripe", role or "processor"
    if role == "ledger":
        return "ledger", "ledger-csv", "ledger"
    if fmt == "csv":
        return "export", "csv", role or "bank"
    raise ValueError(f"{os.path.basename(path)}: not a format the engine reads")
