#!/usr/bin/env python3
"""
importer.py — bank exports into the store: CSV, OFX/QFX, CAMT.053 and MT940.

Statement PDFs only cover the banks whose layouts parse.py knows. Nearly every
bank, anywhere, will also hand you a download, so reading those formats is what
makes this tool work for an account it has never seen.

An export does not come with a total to reconcile against the way a statement
PDF does, but most of them carry balances, and a balance is a stronger check:

* **CAMT.053 and MT940** (the European standard formats) print an opening and a
  closing balance. Opening plus every booked entry has to land on closing, to
  the cent, or something is missing.
* **CSV with a running balance column** checks every single row: each balance
  has to be the one before it plus that row's amount.
* **OFX/QFX** carry only a closing balance, so one file proves nothing on its
  own. Two consecutive exports do: the opening balance implied by the second
  has to be the closing balance of the first. That chain is checked across the
  whole store every time something is imported.
* **CSV with no balance at all** is imported and said to be unverified. A
  number nobody checked is labelled as such rather than presented as fact.

Overlapping downloads are normal (you exported "last 90 days" twice), so a row
already in the store for the same account, on the same day and for the same
amount, is skipped rather than counted twice.

Usage
-----
    finances import ~/Downloads/activity.csv
    finances import exports/                         # every supported file
    finances import chase.csv --account 4321 --issuer Chase --kind bank
    finances import giro.csv --date-format %d.%m.%Y --currency EUR
    finances import card.csv --sign out-negative     # purchases printed as -12.00

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from finances.database import DEFAULT_DB, FinanceDB
from finances.models import Statement, Transaction
from finances.utils import file_sha256

FORMATS = ("csv", "ofx", "camt053", "mt940")
EXTENSIONS = {".csv", ".tsv", ".ofx", ".qfx", ".qbo", ".xml", ".sta", ".mt940",
              ".940", ".txt"}

# Formats whose balances are carried from one file to the next.
CHAINED = {"ofx", "csv", "camt053", "mt940"}


# --------------------------------------------------------------------------
# What a reader hands back
# --------------------------------------------------------------------------

@dataclass
class Account:
    """One account's worth of rows out of one file.

    ``opening`` and ``closing`` are balances in the account's own sense: what
    is owed on a card, what is held in a bank account. ``checked`` is the
    verdict of the file's own arithmetic, None when it has none to offer.
    """
    fmt: str
    name: str                      # source_file for the store
    account: str = ""              # last four of the account number
    issuer: str = ""
    kind: str = "bank"             # "bank" | "card"
    currency: str = ""
    rows: list[Transaction] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    opening: float | None = None
    closing: float | None = None
    checked: bool | None = None
    note: str = ""
    # Where each row came from in the file (row number, FITID, bank reference,
    # the amount as written), keyed by id() of the row so sorting and dropping
    # rows cannot misalign them. Read by finances.verify for its evidence
    # trail; the store does not keep it.
    refs: dict = field(default_factory=dict, repr=False)

    def ref(self, t: Transaction) -> dict:
        return self.refs.get(id(t), {})

    def add(self, t: Transaction, **ref) -> Transaction:
        self.rows.append(t)
        self.refs[id(t)] = {k: v for k, v in ref.items() if v not in (None, "", {})}
        return t


def _house(native: float, kind: str) -> float:
    """A balance-relative amount in the house convention: money out positive.

    Every export format here signs from the account holder's side, where a
    purchase on a card and a withdrawal from a bank are both negative.
    """
    return round(-native, 2)


def _agrees(a: float, b: float) -> bool:
    return abs(a - b) <= 0.015


def _last4(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-4:] if digits else (raw or "")[-4:]


# --------------------------------------------------------------------------
# Numbers and dates in other people's locales
# --------------------------------------------------------------------------

_CURRENCY_JUNK = re.compile(r"[A-Za-z$€£¥₹\s '’]")


def decimal_comma(values: list[str]) -> bool:
    """True when the column writes 1.234,56 rather than 1,234.56.

    Decided once for the whole column. The last separator in a value is the
    decimal point when one or two digits follow it; three digits after a lone
    separator make it a thousands group, which is weaker evidence for the other
    reading. A single "1,234" is ambiguous, but a column always has a value
    that is not.
    """
    comma = dot = 0.0
    for v in values:
        v = _CURRENCY_JUNK.sub("", v or "").strip("()-+")
        m = re.search(r"([.,])(\d+)$", v)
        if not m:
            continue
        sep, tail = m.groups()
        if len(tail) <= 2:
            if sep == ",":
                comma += 1
            else:
                dot += 1
        elif len(tail) == 3 and (("," in v) != ("." in v)):
            if sep == ",":
                dot += 0.25
            else:
                comma += 0.25
    return comma > dot


def parse_amount(raw: str, comma: bool) -> float | None:
    """``-1.234,56 €``, ``(45.00)``, ``45.00-``, ``+12,5`` and friends."""
    s = (raw or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = _CURRENCY_JUNK.sub("", s).strip("()")
    if s.endswith("-"):
        neg, s = not neg, s[:-1]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if comma:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    if not re.fullmatch(r"\d*\.?\d+|\d+\.", s):
        return None
    v = float(s)
    return -v if neg else v


DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y",
                "%m/%d/%y", "%d/%m/%y", "%d.%m.%y", "%Y/%m/%d", "%Y%m%d",
                "%d %b %Y", "%b %d, %Y", "%d-%b-%Y", "%d %B %Y", "%B %d, %Y",
                "%m-%d-%Y", "%Y.%m.%d"]


def date_format(values: list[str], prefer_dmy: bool = False) -> str | None:
    """The one format every non-empty value in a column parses under.

    05/06 is May 6th in the US and June 5th nearly everywhere else, and a
    column of nothing but such dates cannot settle it. A single value over 12
    in the first place does; otherwise the number format breaks the tie, since
    a file that writes 1.234,56 is not American.
    """
    vals = [v.strip() for v in values if v and v.strip()][:400]
    if not vals:
        return None
    fits = []
    for fmt in DATE_FORMATS:
        try:
            for v in vals:
                datetime.strptime(v[:30], fmt)
        except ValueError:
            continue
        fits.append(fmt)
    if not fits:
        # Timestamps: "2025-01-03 14:22:01", "2025-01-03T14:22:01Z".
        if all(re.match(r"\d{4}-\d{2}-\d{2}[ T]", v) for v in vals):
            return "iso-prefix"
        return None
    if len(fits) > 1 and prefer_dmy:
        dmy = [f for f in fits if f.startswith("%d")]
        if dmy:
            return dmy[0]
    return fits[0]


def to_date(raw: str, fmt: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if fmt == "iso-prefix":
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        return datetime.strptime(raw[:30], fmt).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

# Header names seen in the wild, best match first. Lower-cased, with
# punctuation and any "(EUR)" suffix removed before comparing.
ALIASES: dict[str, list[str]] = {
    "date": ["transaction date", "trans date", "date", "booking date",
             "buchungstag", "buchungsdatum", "datum", "date operation",
             "date de l operation", "date de operation", "fecha operacion",
             "fecha", "data operazione", "data contabile", "data",
             "boekingsdatum", "transactiedatum", "dato", "bokforingsdag",
             "transaction posted date", "completed date", "started date",
             "posting date", "posted date", "post date", "value date",
             "valutadatum", "wertstellung", "date valeur", "fecha valor"],
    "post_date": ["post date", "posted date", "posting date", "value date",
                  "valutadatum", "wertstellung", "date valeur", "fecha valor",
                  "data valuta", "rentedatum"],
    "description": ["description", "transaction description", "payee",
                    "merchant", "name", "details", "narrative",
                    "verwendungszweck", "buchungstext", "beschreibung",
                    "libelle", "libelle operation", "concepto", "descripcion",
                    "omschrijving", "naam omschrijving", "mededelingen",
                    "descrizione", "causale", "tekst", "text", "beskrivning",
                    "reference", "memo", "transaction details"],
    "counterparty": ["beguenstigter zahlungspflichtiger", "auftraggeber begunstigter",
                     "auftraggeber beguenstigter", "empfanger", "empfaenger",
                     "zahlungsempfanger", "name zahlungsbeteiligter",
                     "counterparty", "tegenpartij", "beneficiaire",
                     "beneficiario", "payee name"],
    "amount": ["amount", "transaction amount", "betrag", "umsatz", "montant",
               "importe", "bedrag", "importo", "belop", "belopp", "value",
               "amount gbp", "amount eur", "amount usd", "net amount"],
    "debit": ["debit", "debits", "debit amount", "withdrawal", "withdrawals",
              "withdrawal amount", "money out", "paid out", "outflow", "soll",
              "ausgang", "cargo", "cargos", "uscite", "addebiti", "af",
              "charges"],
    "credit": ["credit", "credits", "credit amount", "deposit", "deposits",
               "deposit amount", "money in", "paid in", "inflow", "haben",
               "eingang", "abono", "abonos", "entrate", "accrediti", "bij",
               "payments"],
    "balance": ["balance", "running balance", "running bal", "saldo",
                "kontostand", "saldo nach buchung", "solde", "balance after",
                "available balance", "ledger balance"],
    "currency": ["currency", "waehrung", "wahrung", "devise", "moneda",
                 "munt", "currency code", "valuta"],
    "direction": ["af bij", "debit credit", "dr cr", "credit debit indicator",
                  "soll haben", "cr dr", "debit credit indicator", "s h"],
    "account": ["card no", "card number", "account", "account number",
                "konto", "kontonummer", "iban", "rekening", "auftragskonto",
                "account no"],
}

OUT_WORDS = {"d", "dr", "debit", "debet", "af", "s", "soll", "out", "withdrawal",
             "debito", "addebito"}
IN_WORDS = {"c", "cr", "credit", "bij", "h", "haben", "in", "deposit", "credito",
            "accredito"}


def _norm_header(h: str) -> tuple[str, str]:
    """``"Betrag (EUR)"`` -> ``("betrag", "EUR")``."""
    raw = (h or "").strip().strip("﻿")
    cur = ""
    m = re.search(r"[\(\[]\s*([A-Za-z]{3})\s*[\)\]]\s*$", raw)
    if m:
        cur = m.group(1).upper()
        raw = raw[:m.start()]
    t = raw.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("é", "e"),
                 ("è", "e"), ("ê", "e"), ("à", "a"), ("ó", "o"), ("í", "i"),
                 ("ç", "c"), ("å", "a"), ("ø", "o"), ("æ", "ae"), ("ñ", "n")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t, cur


def map_columns(header: list[str]) -> tuple[dict[str, int], str]:
    """Which column is which, and any currency named in a header."""
    normed = [_norm_header(h) for h in header]
    taken: set[int] = set()
    cols: dict[str, int] = {}
    # Exact matches first across every role, then prefixes, so "Post Date" is
    # never claimed by "date" just because it was asked first.
    for exact in (True, False):
        for role, names in ALIASES.items():
            if role in cols:
                continue
            for name in names:
                hit = next((i for i, (h, _) in enumerate(normed) if i not in taken
                            and (h == name if exact else
                                 (h.startswith(name + " ") or h.endswith(" " + name)))),
                           None)
                if hit is not None:
                    cols[role] = hit
                    taken.add(hit)
                    break
    header_cur = next((c for _, c in normed if c), "")
    return cols, header_cur


def _usable(cols: dict[str, int]) -> bool:
    return "date" in cols and ("amount" in cols or "debit" in cols or "credit" in cols)


def read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:40])
    counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


def _filename_hints(path: str) -> tuple[str, str, str]:
    """(issuer, last4, kind) guessed from a download's name."""
    base = os.path.splitext(os.path.basename(path))[0]
    low = base.lower()
    issuer = ""
    for name, pat in (("Chase", r"chase"), ("Amex", r"amex|american ?express"),
                      ("Capital One", r"capital ?one"), ("Discover", r"discover"),
                      ("Citi", r"\bciti"), ("Bank of America", r"bofa|bank ?of ?america"),
                      ("Wells Fargo", r"wells"), ("Apple Card", r"apple"),
                      ("Revolut", r"revolut"), ("Monzo", r"monzo"), ("N26", r"n26"),
                      ("Wise", r"\bwise\b|transferwise"), ("ING", r"\bing\b"),
                      ("Barclays", r"barclay"), ("HSBC", r"hsbc")):
        if re.search(pat, low):
            issuer = name
            break
    m = re.findall(r"(?<!\d)(\d{4})(?!\d)", base)
    last4 = next((x for x in reversed(m) if not x.startswith(("19", "20"))), "")
    kind = "card" if re.search(r"card|credit|amex|visa|master|discover", low) else ""
    return issuer, last4, kind


def read_csv(path: str, *, account: str = "", issuer: str = "", kind: str = "",
             currency: str = "", date_fmt: str = "", sign: str = "auto") -> list[Account]:
    text = read_text(path)
    delim = sniff_delimiter(text)
    table = [r for r in csv.reader(io.StringIO(text), delimiter=delim)]

    # Some banks put a summary block above the real header (Bank of America
    # does), so look for the first row that names a date and an amount.
    head_at, cols, header_cur = None, {}, ""
    for i, row in enumerate(table[:40]):
        c, hc = map_columns(row)
        if _usable(c):
            head_at, cols, header_cur = i, c, hc
            break
    if head_at is None:
        raise ValueError("no header row with a date and an amount column; "
                         "is this a transaction export?")
    rows = [(n, r) for n, r in enumerate(table[head_at + 1:], start=head_at + 2)
            if any(x.strip() for x in r)]
    numbers = {id(r): n for n, r in rows}
    rows = [r for _, r in rows]

    def col(role, r):
        i = cols.get(role)
        return r[i].strip() if i is not None and i < len(r) else ""

    money_cols = [k for k in ("amount", "debit", "credit", "balance") if k in cols]
    comma = decimal_comma([col(k, r) for r in rows for k in money_cols])
    dfmt = date_fmt or date_format([col("date", r) for r in rows], prefer_dmy=comma)
    if not dfmt:
        raise ValueError(f"could not tell the date format from values like "
                         f"{col('date', rows[0]) if rows else '?'!r}; "
                         f"pass --date-format (for example %d.%m.%Y)")
    pfmt = date_format([col("post_date", r) for r in rows], prefer_dmy=comma) \
        if "post_date" in cols else None

    f_issuer, f_last4, f_kind = _filename_hints(path)
    acct_vals = Counter(col("account", r) for r in rows if col("account", r))
    last4 = account or (_last4(acct_vals.most_common(1)[0][0]) if acct_vals else f_last4)
    issuer = issuer or f_issuer or "CSV import"
    file_cur = (currency or header_cur).upper()

    # Raw signed amounts as the file writes them, before any convention.
    parsed = []
    for r in rows:
        d = to_date(col("date", r), dfmt)
        if d is None:
            continue
        desc = col("description", r)
        party = col("counterparty", r)
        if party and party.lower() not in desc.lower():
            desc = f"{party} {desc}".strip()
        desc = re.sub(r"\s+", " ", desc) or party or "(no description)"
        explicit_out = None
        if "amount" in cols:
            amt = parse_amount(col("amount", r), comma)
            dirn = col("direction", r).lower().strip(". ")
            if amt is not None and dirn:
                if dirn in OUT_WORDS:
                    amt, explicit_out = abs(amt), True
                elif dirn in IN_WORDS:
                    amt, explicit_out = -abs(amt), True
        else:
            deb = parse_amount(col("debit", r), comma)
            cred = parse_amount(col("credit", r), comma)
            if deb is None and cred is None:
                continue
            amt, explicit_out = abs(deb or 0.0) - abs(cred or 0.0), True
        if amt is None:
            continue
        parsed.append({
            "record": numbers[id(r)],
            "cells": {k: col(k, r) for k in ("date", "amount", "debit", "credit",
                                              "balance") if k in cols},
            "date": d, "desc": desc, "raw": amt, "explicit": explicit_out,
            "post": to_date(col("post_date", r), pfmt) if pfmt else None,
            "balance": parse_amount(col("balance", r), comma) if "balance" in cols else None,
            "currency": (col("currency", r) or file_cur).upper()[:3],
        })
    if not parsed:
        raise ValueError("header found, but no row had a date and an amount")

    kind = kind or f_kind or "bank"
    out_positive, checked, note = _csv_polarity(parsed, kind, sign)

    # Chronological order: files come newest-first about half the time.
    chron = parsed if parsed[0]["date"] <= parsed[-1]["date"] else parsed[::-1]
    acct = Account(fmt="csv", name=os.path.basename(path), account=last4,
                   issuer=issuer, kind=kind,
                   currency=Counter(p["currency"] for p in parsed).most_common(1)[0][0],
                   start=min(p["date"] for p in parsed),
                   end=max(p["date"] for p in parsed), checked=checked, note=note)
    for p in chron:
        house = p["raw"] if (p["explicit"] or out_positive) else -p["raw"]
        acct.add(_txn(acct, p["date"], p["desc"], house,
                      post=p["post"], currency=p["currency"]),
                 record=p["record"], raw=p["cells"],
                 sign="as written" if (p["explicit"] or out_positive) else "inverted")

    bals = [p for p in chron if p["balance"] is not None]
    if bals:
        first, last = bals[0], bals[-1]
        h_first = first["raw"] if (first["explicit"] or out_positive) else -first["raw"]
        # The balance on a row is after that row; step back one to open.
        acct.opening = round(first["balance"] + h_first if kind == "bank"
                             else first["balance"] - h_first, 2)
        acct.closing = round(last["balance"], 2)
    return [acct]


def _csv_polarity(parsed: list[dict], kind: str, sign: str) -> tuple[bool, bool | None, str]:
    """Is a positive amount money out? And does the balance column agree, row by row?

    With a running balance this is not a guess: for each orientation of the
    file and each reading of the sign, count the consecutive rows whose balance
    moves by exactly that row's amount. The right reading accounts for every
    row; a wrong one for almost none.
    """
    has_bal = sum(1 for p in parsed if p["balance"] is not None) >= 2
    forced = {"out-positive": True, "out-negative": False}.get(sign)

    if not has_bal:
        if forced is not None:
            out_positive = forced
        else:
            signed = [p["raw"] for p in parsed if not p["explicit"]]
            # Most rows on any account are spending, whichever way it is signed.
            out_positive = sum(1 for v in signed if v > 0) >= sum(1 for v in signed if v < 0)
        return out_positive, None, ("no running balance in this export, so there "
                                    "is nothing to check the rows against")

    best = None
    for out_positive in ((forced,) if forced is not None else (True, False)):
        for seq in (parsed, parsed[::-1]):
            ok = total = 0
            first_break = None
            for a, b in zip(seq, seq[1:]):
                if a["balance"] is None or b["balance"] is None:
                    continue
                house = b["raw"] if (b["explicit"] or out_positive) else -b["raw"]
                step = -house if kind == "bank" else house
                total += 1
                if _agrees(a["balance"] + step, b["balance"]):
                    ok += 1
                elif first_break is None:
                    first_break = b
            score = (ok, out_positive == (forced if forced is not None else True))
            if best is None or score > best[0]:
                best = (score, out_positive, ok, total, first_break)
    _, out_positive, ok, total, brk = best
    if total and ok == total:
        return out_positive, True, f"all {total + 1} rows agree with the running balance"
    if total and ok >= total * 0.5:
        where = f" first break at {brk['date']} {brk['desc'][:30]}" if brk else ""
        return out_positive, False, (f"{total - ok} of {total} rows do not move the "
                                     f"running balance by their amount;{where}")
    # The balance column does not behave like one (some banks print the
    # available balance, which pending items move). Fall back to the count.
    signed = [p["raw"] for p in parsed if not p["explicit"]]
    out_positive = forced if forced is not None else \
        sum(1 for v in signed if v > 0) >= sum(1 for v in signed if v < 0)
    return out_positive, None, "the balance column does not track the rows, so it was not used"


# --------------------------------------------------------------------------
# OFX / QFX / QBO
# --------------------------------------------------------------------------

def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def _ofx_date(raw: str) -> date | None:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", raw or "")
    if not m:
        return None
    try:
        return date(*map(int, m.groups()))
    except ValueError:
        return None


def _ofx_amount(raw: str) -> float | None:
    s = (raw or "").strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&apos;", "'").replace("&quot;", '"'))


def read_ofx(path: str, *, issuer: str = "", **_) -> list[Account]:
    text = read_text(path)
    org = _tag(text, "ORG")
    f_issuer, _, _ = _filename_hints(path)
    out = []
    blocks = re.findall(r"<(STMTRS|CCSTMTRS)>(.*?)</\1>", text, re.S | re.I)
    if not blocks:
        raise ValueError("no <STMTRS> or <CCSTMTRS> statement in this OFX file")
    for i, (kind_tag, body) in enumerate(blocks):
        kind = "card" if kind_tag.upper() == "CCSTMTRS" else "bank"
        acct_id = _tag(body, "ACCTID")
        last4 = _last4(acct_id)
        name = os.path.basename(path) + (f"#{last4 or i}" if len(blocks) > 1 else "")
        acct = Account(fmt="ofx", name=name, account=last4,
                       issuer=issuer or (_unescape(org) if org else "") or f_issuer or "OFX import",
                       kind=kind, currency=_tag(body, "CURDEF").upper())
        tranlist = re.search(r"<BANKTRANLIST>(.*?)</BANKTRANLIST>", body, re.S | re.I)
        tl = tranlist.group(1) if tranlist else body
        acct.start = _ofx_date(_tag(tl, "DTSTART"))
        acct.end = _ofx_date(_tag(tl, "DTEND"))
        native_sum = 0.0
        seen_fitid = set()
        for t in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", tl, re.S | re.I):
            amt = _ofx_amount(_tag(t, "TRNAMT"))
            d = _ofx_date(_tag(t, "DTUSER")) or _ofx_date(_tag(t, "DTPOSTED"))
            if amt is None or d is None:
                continue
            fitid = _tag(t, "FITID")
            if fitid and fitid in seen_fitid:
                continue
            seen_fitid.add(fitid)
            nm = _unescape(_tag(t, "NAME") or _tag(t, "PAYEEID"))
            memo = _unescape(_tag(t, "MEMO"))
            desc = nm if not memo or memo.lower() in nm.lower() else f"{nm} {memo}".strip()
            native_sum += amt
            acct.add(_txn(acct, d, desc or "(no description)", _house(amt, kind),
                          post=_ofx_date(_tag(t, "DTPOSTED")), currency=acct.currency),
                     fitid=fitid, trntype=_tag(t, "TRNTYPE"), raw={"TRNAMT": _tag(t, "TRNAMT")},
                     sign="inverted")
        acct.rows.sort(key=lambda t: t.date)
        ledger = re.search(r"<LEDGERBAL>(.*?)</LEDGERBAL>", body, re.S | re.I)
        if ledger:
            bal = _ofx_amount(_tag(ledger.group(1), "BALAMT"))
            if bal is not None:
                # Native balances are asset-positive; a card's is what you owe.
                acct.closing = round(-bal if kind == "card" else bal, 2)
                native_open = bal - native_sum
                acct.opening = round(-native_open if kind == "card" else native_open, 2)
        if acct.rows:
            acct.start = acct.start or _ts_date(acct.rows[0].date)
            acct.end = acct.end or _ts_date(acct.rows[-1].date)
        acct.note = ("OFX carries a closing balance only; checked against the "
                     "previous export for this account")
        out.append(acct)
    return out


def _ts_date(iso: str) -> date:
    return datetime.strptime(iso[:10], "%Y-%m-%d").date()


# --------------------------------------------------------------------------
# CAMT.053 (ISO 20022 bank-to-customer statement)
# --------------------------------------------------------------------------

def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _txt(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _camt_amount(el: ET.Element) -> tuple[float, str]:
    amt_el = el.find("Amt")
    v = float((amt_el.text or "0").strip()) if amt_el is not None else 0.0
    cur = amt_el.get("Ccy", "") if amt_el is not None else ""
    if _txt(el, "CdtDbtInd") == "DBIT":
        v = -v
    return v, cur


def read_camt(path: str, *, issuer: str = "", **_) -> list[Account]:
    root = _strip_ns(ET.parse(path).getroot())
    stmts = root.findall(".//Stmt") or root.findall(".//Rpt")
    if not stmts:
        raise ValueError("no <Stmt> in this CAMT file")
    f_issuer, _, _ = _filename_hints(path)
    out = []
    for i, st in enumerate(stmts):
        acct_el = st.find("Acct")
        acct_id = _txt(acct_el, "Id/IBAN") or _txt(acct_el, "Id/Othr/Id")
        bank = _txt(acct_el, "Svcr/FinInstnId/Nm") or _txt(acct_el, "Svcr/FinInstnId/BIC") \
            or _txt(acct_el, "Svcr/FinInstnId/BICFI")
        last4 = _last4(acct_id)
        name = os.path.basename(path) + (f"#{last4 or i}" if len(stmts) > 1 else "")
        acct = Account(fmt="camt053", name=name, account=last4,
                       issuer=issuer or bank or f_issuer or "CAMT import", kind="bank",
                       currency=_txt(acct_el, "Ccy").upper())
        opening = closing = None
        for bal in st.findall("Bal"):
            code = _txt(bal, "Tp/CdOrPrtry/Cd") or _txt(bal, "Tp/CdOrPrtry/Prtry")
            v, cur = _camt_amount(bal)
            acct.currency = acct.currency or cur.upper()
            d = _txt(bal, "Dt/Dt") or _txt(bal, "Dt/DtTm")[:10]
            if code in ("OPBD", "PRCD") and opening is None:
                opening = v
                if d and code == "OPBD":
                    acct.start = _ts_date(d)
            elif code == "CLBD":
                closing = v
                if d:
                    acct.end = _ts_date(d)
        native_sum = 0.0
        for n_entry, e in enumerate(st.findall("Ntry"), start=1):
            status = _txt(e, "Sts") or _txt(e, "Sts/Cd")
            if status and status not in ("BOOK",):
                continue                          # pending or informational
            v, cur = _camt_amount(e)
            d = _txt(e, "BookgDt/Dt") or _txt(e, "BookgDt/DtTm")[:10] \
                or _txt(e, "ValDt/Dt")
            if not d:
                continue
            tx = e.find("NtryDtls/TxDtls")
            party = ""
            if tx is not None:
                party = (_txt(tx, "RltdPties/Cdtr/Nm") or _txt(tx, "RltdPties/Cdtr/Pty/Nm")
                         if v < 0 else
                         _txt(tx, "RltdPties/Dbtr/Nm") or _txt(tx, "RltdPties/Dbtr/Pty/Nm"))
            purpose = " ".join(u.text.strip() for u in e.iter("Ustrd") if u.text) \
                or _txt(e, "AddtlNtryInf")
            desc = re.sub(r"\s+", " ", f"{party} {purpose}").strip() or "(no description)"
            native_sum += v
            refs = tx.find("Refs") if tx is not None else None
            acct.add(_txt_row(acct, d, desc, v, _txt(e, "ValDt/Dt"), cur or acct.currency),
                     entry=n_entry,
                     acct_svcr_ref=_txt(e, "AcctSvcrRef") or _txt(refs, "AcctSvcrRef"),
                     end_to_end_id=_txt(refs, "EndToEndId"),
                     raw={"Amt": _txt(e, "Amt"), "CdtDbtInd": _txt(e, "CdtDbtInd")},
                     sign="inverted")
        acct.rows.sort(key=lambda t: t.date)
        _close_the_books(acct, opening, native_sum, closing)
        if acct.rows:
            acct.start = acct.start or _ts_date(acct.rows[0].date)
            acct.end = acct.end or _ts_date(acct.rows[-1].date)
        out.append(acct)
    return out


def _txt_row(acct: Account, d: str, desc: str, native: float, post: str, cur: str):
    return _txn(acct, _ts_date(d), desc, _house(native, "bank"),
                post=_ts_date(post) if post else None, currency=cur.upper())


def _close_the_books(acct: Account, opening, native_sum, closing):
    """Opening plus every booked entry must equal closing. Bank accounts only."""
    acct.opening = round(opening, 2) if opening is not None else None
    acct.closing = round(closing, 2) if closing is not None else None
    if opening is None or closing is None:
        acct.checked = None
        acct.note = "the file prints no opening and closing balance pair to check"
        return
    expect = opening + native_sum
    if _agrees(expect, closing):
        acct.checked = True
        acct.note = ""
    else:
        acct.checked = False
        acct.note = (f"opening {opening:,.2f} plus {len(acct.rows)} entries is "
                     f"{expect:,.2f}, but the file closes at {closing:,.2f} "
                     f"({expect - closing:+,.2f})")


# --------------------------------------------------------------------------
# MT940 (SWIFT customer statement)
# --------------------------------------------------------------------------

_MT_61 = re.compile(r"^(\d{6})(\d{4})?(RC|RD|C|D)([A-Z])?(\d+,\d*)")
_MT_BAL = re.compile(r"^([CD])(\d{6})([A-Z]{3})(\d+,\d*)")


def _mt_date(yymmdd: str) -> date:
    return date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))


def _mt_amount(raw: str) -> float:
    return float(raw.replace(",", "."))


def _mt_description(text: str) -> str:
    """:86: is free text, or ?20..?63 subfields in the German structured form."""
    text = text.replace("\n", "")
    if re.search(r"\?\d\d", text):
        fields = dict(re.findall(r"\?(\d\d)([^?]*)", text))
        name = " ".join(fields.get(k, "") for k in ("32", "33")).strip()
        purpose = "".join(fields.get(f"{k}", "") for k in
                          [str(n) for n in range(20, 30)] + [str(n) for n in range(60, 64)])
        text = f"{name} {purpose}".strip() or fields.get("00", "")
    text = re.sub(r"/(?:EREF|KREF|MREF|CRED|IBAN|BIC|SVWZ|ABWA|NAME|REMI)/", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_mt940(path: str, *, issuer: str = "", **_) -> list[Account]:
    text = read_text(path).replace("\r\n", "\n")
    # Fold continuation lines into the field they belong to.
    fields: list[tuple[str, str]] = []
    for line in text.split("\n"):
        m = re.match(r"^:(\d{2}[A-Z]?):(.*)$", line)
        if m:
            fields.append((m.group(1), m.group(2)))
        elif fields and line.strip() and not line.startswith(("-}", "{", "-")):
            tag, val = fields[-1]
            fields[-1] = (tag, val + "\n" + line)
    if not any(t == "20" for t, _ in fields):
        raise ValueError("no :20: statement header; not an MT940 file")

    f_issuer, _, _ = _filename_hints(path)
    out: list[Account] = []
    cur: Account | None = None
    opening = closing = None
    native_sum = 0.0
    pending: dict | None = None
    n_61 = 0

    def finish():
        nonlocal pending
        if cur is None:
            return
        if pending:
            _mt_row(cur, pending)
            pending = None
        cur.rows.sort(key=lambda t: t.date)
        _close_the_books(cur, opening, native_sum, closing)
        if cur.rows:
            cur.start = cur.start or _ts_date(cur.rows[0].date)
            cur.end = cur.end or _ts_date(cur.rows[-1].date)
        out.append(cur)

    for tag, val in fields:
        if tag == "20":
            finish()
            cur = Account(fmt="mt940", name="", issuer=issuer or f_issuer or "MT940 import")
            opening = closing = None
            native_sum = 0.0
        elif cur is None:
            continue
        elif tag == "25":
            acct_id = val.strip().split("\n")[0]
            cur.account = _last4(acct_id.split("/")[-1])
        elif tag in ("60F", "60M"):
            m = _MT_BAL.match(val.strip())
            if m and opening is None:
                sign = -1 if m.group(1) == "D" else 1
                opening = sign * _mt_amount(m.group(4))
                cur.currency = m.group(3)
                cur.start = _mt_date(m.group(2)) + (timedelta(days=1) if tag == "60F" else timedelta())
        elif tag in ("62F", "62M"):
            m = _MT_BAL.match(val.strip())
            if m:
                sign = -1 if m.group(1) == "D" else 1
                closing = sign * _mt_amount(m.group(4))
                cur.end = _mt_date(m.group(2))
        elif tag == "61":
            if pending:
                _mt_row(cur, pending)
            m = _MT_61.match(val.strip())
            if not m:
                pending = None
                continue
            vdate = _mt_date(m.group(1))
            book = vdate
            if m.group(2):
                mo, dy = int(m.group(2)[:2]), int(m.group(2)[2:])
                try:
                    book = date(vdate.year, mo, dy)
                    # An entry date in January on a December value date.
                    if (book - vdate).days > 180:
                        book = date(vdate.year - 1, mo, dy)
                    elif (vdate - book).days > 180:
                        book = date(vdate.year + 1, mo, dy)
                except ValueError:
                    book = vdate
            amt = _mt_amount(m.group(5))
            native = -amt if m.group(3) in ("D", "RC") else amt
            native_sum += native
            rest = val[m.end():].split("\n", 1)
            n_61 += 1
            pending = {"date": book, "post": vdate, "native": native,
                       "desc": (rest[1] if len(rest) > 1 else "").strip(),
                       "entry": n_61, "line": rest[0].strip()}
        elif tag == "86" and pending is not None:
            pending["desc"] = _mt_description(val) or pending["desc"]
    finish()

    base = os.path.basename(path)
    for i, a in enumerate(out):
        a.name = base + (f"#{i + 1}" if len(out) > 1 else "")
        for t in a.rows:
            t.source_file = a.name
    return out


def _mt_row(acct: Account, p: dict) -> Transaction:
    return acct.add(_txn(acct, p["date"], p["desc"] or "(no description)",
                         _house(p["native"], "bank"), post=p["post"], currency=acct.currency),
                    entry=p["entry"], raw={":61:": p["line"]}, sign="inverted")


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------

_categorizer = None


def _classify(desc: str) -> tuple[str, str]:
    """(merchant, category) from the same rules a parsed PDF gets."""
    global _categorizer
    try:
        from finances.categorizer import Categorizer, normalize
        if _categorizer is None:
            _categorizer = Categorizer()
        cleaned, _ = normalize(desc)
        return cleaned or desc, _categorizer.categorize(desc).category
    except Exception:                               # noqa: BLE001
        return desc, "Uncategorized"


def _txn(acct: Account, d: date, desc: str, amount: float, *, post: date | None = None,
         currency: str = "") -> Transaction:
    merchant, category = _classify(desc)
    return Transaction(date=d.isoformat(), description=desc, amount=round(amount, 2),
                       merchant=merchant, category=category,
                       post_date=post.isoformat() if post else "",
                       issuer=acct.issuer, account_last4=acct.account,
                       source_file=acct.name, currency=(currency or "").upper())


# --------------------------------------------------------------------------
# Exports from other budgeting apps
# --------------------------------------------------------------------------
#
# People leaving Mint, YNAB, Monarch, Copilot or Tiller take their history with
# them as one CSV covering every account. Each app has its own columns and its
# own sign convention, so each is recognised by its header and read as such.
# The column names are what these apps' exports have used; a file that differs
# is reported, not guessed at. There are no balances in any of them, so nothing
# here can be checked against a statement: the rows are imported as unverified.

APPS = {
    # name: (columns that identify it, how amounts are signed)
    "mint": ({"date", "original description", "amount", "transaction type", "account name"},
             "type"),          # amount always positive; Transaction Type debit/credit
    "ynab": ({"account", "date", "payee", "outflow", "inflow"}, "split"),
    "monarch": ({"date", "merchant", "account", "original statement", "amount"},
                "in-positive"),
    "tiller": ({"date", "description", "amount", "account", "institution", "full description"},
               "in-positive"),
    "copilot": ({"date", "name", "amount", "account", "parent category", "excluded"},
                "out-positive"),
}
APP_LABELS = {"mint": "Mint", "ynab": "YNAB", "monarch": "Monarch", "tiller": "Tiller",
              "copilot": "Copilot"}


# The other app's category, when it says something ours can use. Matched as
# words in its category name, first hit wins; anything else keeps our own guess.
APP_CATEGORIES = [
    (r"paycheck|salary|wages|income|ready to assign|inflow|deposit|interest income|dividend", "Income"),
    (r"credit card payment|card payment|loan payment", "Payment"),
    (r"transfer", "Transfers/Cash"),
    (r"refund|reimburse", "Credits/Refunds"),
    (r"grocer|supermarket", "Groceries"),
    (r"coffee", "Coffee"),
    (r"restaurant|dining|fast food|food & drink|takeout|eating out", "Dining"),
    (r"delivery", "Delivery"),
    (r"alcohol|bar", "Alcohol/Bars"),
    (r"gas|fuel|auto|parking|car ", "Gas/Auto"),
    (r"public transit|transport|rideshare|taxi|uber|lyft", "Transport"),
    (r"rent|mortgage|housing", "Rent/Housing"),
    (r"utilit|electric|water|power", "Utilities"),
    (r"phone|internet|mobile|cable", "Phone/Internet"),
    (r"insurance", "Insurance"),
    (r"pharmacy", "Pharmacy"),
    (r"health|medical|doctor|dentist", "Health"),
    (r"gym|fitness", "Fitness"),
    (r"stream|subscription|music|movies", "Subscriptions"),
    (r"software", "Software/Tools"),
    (r"entertainment|games|gaming", "Entertainment"),
    (r"travel|flight|airline", "Travel"),
    (r"hotel|lodging", "Lodging"),
    (r"cloth|apparel", "Clothing"),
    (r"electronics", "Electronics"),
    (r"shopping|amazon", "Shopping"),
    (r"personal care|hair|beauty", "Personal Care"),
    (r"education|tuition", "Education"),
    (r"childcare|daycare", "Childcare"),
    (r"pet", "Pets"),
    (r"charity|donation", "Charity"),
    (r"gift", "Gifts"),
    (r"fee|interest charge|finance charge", "Fees/Interest"),
    (r"tax", "Taxes"),
    (r"home|furnish|household", "Home"),
]


def app_category(name: str, house: float) -> str | None:
    """Our category for another app's, or None when it does not map cleanly."""
    low = (name or "").lower()
    if not low:
        return None
    for pattern, ours in APP_CATEGORIES:
        if re.search(rf"\b(?:{pattern})", low):
            # Money coming in is never spending, whatever the label says.
            if house < 0 and ours not in ("Income", "Transfers/Cash", "Credits/Refunds",
                                         "Payment"):
                return None
            return ours
    return None


def detect_app(header: list[str]) -> str | None:
    names = {_norm_header(h)[0] for h in header}
    for app, (needs, _) in APPS.items():
        if needs <= names:
            return app
    return None


def read_app_export(path: str, *, app: str | None = None, **_) -> list[Account]:
    """One budgeting app's export, split into an Account per account it names."""
    text = read_text(path)
    table = list(csv.reader(io.StringIO(text), delimiter=sniff_delimiter(text)))
    if not table:
        raise ValueError("empty file")
    header = table[0]
    app = app or detect_app(header)
    if app not in APPS:
        raise ValueError("not an export from Mint, YNAB, Monarch, Copilot or Tiller")
    # "Account #" (Tiller's masked number) would otherwise collide with "Account".
    idx = {_norm_header(h)[0] + (" no" if "#" in h else ""): i for i, h in enumerate(header)}
    _, convention = APPS[app]

    def col(name, r):
        i = idx.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    rows = [(n, r) for n, r in enumerate(table[1:], start=2) if any(c.strip() for c in r)]
    comma = decimal_comma([col(k, r) for _, r in rows
                           for k in ("amount", "outflow", "inflow")])
    dfmt = date_format([col("date", r) for _, r in rows], prefer_dmy=comma)
    if not dfmt:
        raise ValueError(f"could not tell the date format of this {APP_LABELS[app]} export")

    accounts: dict[str, Account] = {}
    base = os.path.basename(path)
    for n, r in rows:
        d = to_date(col("date", r), dfmt)
        if d is None:
            continue
        if convention == "split":
            out = parse_amount(col("outflow", r), comma) or 0.0
            inn = parse_amount(col("inflow", r), comma) or 0.0
            house = abs(out) - abs(inn)
        else:
            v = parse_amount(col("amount", r), comma)
            if v is None:
                continue
            if convention == "type":
                house = abs(v) if col("transaction type", r).lower() == "debit" else -abs(v)
            elif convention == "in-positive":
                house = -v
            else:                                   # out-positive (Copilot)
                house = -abs(v) if col("type", r).lower() == "income" else v
        if house == 0:
            continue
        name = col("account name", r) or col("account", r) or APP_LABELS[app]
        # The last four digits, where the app exports them, let these rows line up
        # with (and be de-duplicated against) the bank's own files.
        masked = col("account mask", r) or col("account no", r)
        named = re.search(r"\.{2,}\s*(\d{4})\)?\s*$", name)       # Monarch: "Amex (...1005)"
        last4 = _last4(masked) if re.search(r"\d", masked) else (named.group(1) if named else "")
        key = f"{name}|{last4}"
        acct = accounts.get(key)
        if acct is None:
            kind = "card" if re.search(r"card|credit|visa|master|amex|discover", name, re.I) else "bank"
            issuer = col("institution", r) or APP_LABELS[app]
            acct = accounts[key] = Account(
                fmt=app, name=f"{base}#{name}", account=last4, issuer=issuer, kind=kind,
                note=f"imported from {APP_LABELS[app]}; the export carries no balances, "
                     f"so these rows cannot be checked against a statement")
        payee = col("original description", r) or col("original statement", r) \
            or col("full description", r)
        desc = payee or col("description", r) or col("merchant", r) or col("payee", r) \
            or col("name", r) or "(no description)"
        t = acct.add(_txn(acct, d, re.sub(r"\s+", " ", desc), round(house, 2)),
                     record=n, raw={"amount": col("amount", r) or
                                    f"{col('outflow', r)}/{col('inflow', r)}"},
                     app_category=col("category", r))
        # Our categories drive budgets and the subscription features, so ours
        # stand; theirs win only where ours had nothing, or where theirs says
        # the money is not spending at all (pay, transfers, refunds, card payments).
        theirs = app_category(col("category", r), house)
        if theirs and (t.category == "Uncategorized" or theirs in
                       ("Income", "Transfers/Cash", "Credits/Refunds", "Payment")):
            t.category = theirs
    for acct in accounts.values():
        acct.rows.sort(key=lambda t: t.date)
        if acct.rows:
            acct.start = _ts_date(acct.rows[0].date)
            acct.end = _ts_date(acct.rows[-1].date)
    if not accounts:
        raise ValueError(f"no rows with a date and an amount in this {APP_LABELS[app]} export")
    return sorted(accounts.values(), key=lambda a: a.name)


def detect_format(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as fh:
        head = fh.read(4096).decode("latin-1", "replace")
    if ext in (".ofx", ".qfx", ".qbo") or "OFXHEADER" in head or "<OFX>" in head.upper():
        return "ofx"
    if "camt.053" in head or ("<Document" in head and "BkToCstmrStmt" in head):
        return "camt053"
    if re.search(r"^:20:", head, re.M) and re.search(r"^:(25|60F):", head, re.M):
        return "mt940"
    if ext in (".csv", ".tsv", ".txt"):
        first = next(csv.reader(io.StringIO(head.split("\n", 1)[0])), [])
        return detect_app(first) or "csv"
    if ext == ".xml":
        full = read_text(path)
        if "BkToCstmrStmt" in full:
            return "camt053"
    return None


READERS = {"csv": read_csv, "ofx": read_ofx, "camt053": read_camt, "mt940": read_mt940,
           **{app: read_app_export for app in APPS}}


def read_file(path: str, fmt: str | None = None, **opts) -> list[Account]:
    fmt = fmt or detect_format(path)
    if fmt is None:
        raise ValueError("not a format this can read (CSV, OFX/QFX, CAMT.053, MT940)")
    return READERS[fmt](path, **opts)


def find_files(inp: str) -> list[str]:
    """Every export in a folder. Loose .txt and .xml files count only when
    their content says they are MT940 or CAMT, so a folder of notes is left alone."""
    if not os.path.isdir(inp):
        return [inp]
    out = []
    for dp, _, fs in os.walk(inp):
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            if f.startswith(".") or ext not in EXTENSIONS:
                continue
            path = os.path.join(dp, f)
            if ext in (".txt", ".xml") and detect_format(path) not in ("mt940", "camt053"):
                continue
            out.append(path)
    return sorted(out)


# --------------------------------------------------------------------------
# Into the store
# --------------------------------------------------------------------------

def statement_for(acct: Account) -> Statement:
    out = sum(t.amount for t in acct.rows if t.amount > 0)
    inn = sum(-t.amount for t in acct.rows if t.amount < 0)
    st = Statement(
        source_file=acct.name, issuer=acct.issuer, account_last4=acct.account,
        statement_period=(f"{acct.start:%m/%d/%Y} - {acct.end:%m/%d/%Y}"
                          if acct.start and acct.end else ""),
        period_start=acct.start.isoformat() if acct.start else "",
        period_end=acct.end.isoformat() if acct.end else "",
        previous_balance=acct.opening, new_balance=acct.closing,
        charges_extracted=round(out, 2), credits_extracted=round(inn, 2),
        currency=acct.currency, account_type=acct.kind, source_format=acct.fmt,
        reconcile_note=acct.note,
        reconciled=None if acct.checked is None else int(acct.checked),
    )
    return st


def drop_overlap(db: FinanceDB, acct: Account) -> int:
    """Remove rows another file already put in the store for this account.

    Matched on day and amount, counted, so two genuine $3.00 fares on one
    morning survive unless the store already holds both of them.
    """
    if not acct.account:
        return 0
    have = Counter(
        (r["date"], round(r["amount"], 2)) for r in db.conn.execute(
            "SELECT date, amount FROM transactions "
            "WHERE account_last4 = ? AND source_file != ?",
            (acct.account, acct.name)))
    if not have:
        return 0
    keep, dropped = [], 0
    for t in acct.rows:
        k = (t.date, round(t.amount, 2))
        if have[k] > 0:
            have[k] -= 1
            dropped += 1
        else:
            keep.append(t)
    acct.rows = keep
    return dropped


def chain(db: FinanceDB, last4: str) -> list[str]:
    """Check each export's opening balance against the closing balance before it.

    Run over the whole account, not just the file being imported, because
    exports arrive in any order: importing March after April is what lets
    April finally be checked. Returns a line per verdict that changed.
    """
    rows = [s for s in db.statement_models()
            if s.account_last4 == last4 and s.period_start and s.period_end]
    rows.sort(key=lambda s: (s.period_end, s.period_start))
    changed = []
    for i, s in enumerate(rows):
        if s.source_format not in CHAINED or s.previous_balance is None:
            continue
        prior = [p for p in rows[:i] if p.new_balance is not None
                 and p.period_end <= s.period_start and p.currency == s.currency]
        if not prior:
            continue
        p = prior[-1]
        base_note = s.reconcile_note.split(" | chain:")[0]
        if _agrees(p.new_balance, s.previous_balance):
            verdict = "continues " + p.source_file
            if s.reconciled is None and s.source_format == "ofx":
                s.reconciled = 1
            note = base_note if s.source_format != "ofx" else verdict
        else:
            gap = s.previous_balance - p.new_balance
            verdict = (f"opens at {s.previous_balance:,.2f} but {p.source_file} "
                       f"closed at {p.new_balance:,.2f}; {abs(gap):,.2f} unaccounted "
                       f"for between {p.period_end} and {s.period_start}")
            if s.source_format == "ofx":
                s.reconciled = 0
            note = (base_note + " | chain: " if base_note and s.source_format != "ofx"
                    else "") + verdict
        if note != s.reconcile_note:
            s.reconcile_note = note
            db.upsert_statements([s])
            changed.append(f"{s.source_file}: {verdict}")
    return changed


def import_file(db: FinanceDB, path: str, *, force: bool = False, fmt: str | None = None,
                **opts) -> list[tuple[Account, Statement, int]]:
    """Read one export and write it. Returns (account, statement, rows skipped)."""
    sha = file_sha256(path)
    if not force and db.is_cached(path, sha):
        return []
    results = []
    for acct in read_file(path, fmt, **opts):
        db.delete_source(acct.name)
        db.conn.execute("DELETE FROM statements WHERE source_file = ?", (acct.name,))
        skipped = drop_overlap(db, acct)
        st = statement_for(acct)
        db.upsert_transactions(acct.rows)
        db.upsert_statements([st])
        results.append((acct, st, skipped))
    db.mark_parsed(path, sha, os.path.getmtime(path),
                   sum(len(a.rows) for a, _, _ in results))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="finances import",
        description="Import bank exports (CSV, OFX/QFX, CAMT.053, MT940) into the store.")
    ap.add_argument("input", nargs="+", help="files or folders")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--format", choices=FORMATS, help="skip detection")
    ap.add_argument("--account", default="", help="last four digits, for a CSV that omits them")
    ap.add_argument("--issuer", default="", help="bank name, for a CSV that omits it")
    ap.add_argument("--kind", choices=["bank", "card"], default="",
                    help="what the account is, for a CSV (default: guessed, else bank)")
    ap.add_argument("--currency", default="", help="ISO code for a CSV that does not say")
    ap.add_argument("--date-format", default="",
                    help="strptime format for the date column, e.g. %%d.%%m.%%Y")
    ap.add_argument("--sign", choices=["auto", "out-positive", "out-negative"],
                    default="auto", help="how a CSV signs spending (default: worked out)")
    ap.add_argument("--force", action="store_true", help="re-import unchanged files")
    args = ap.parse_args()

    files = [f for inp in args.input for f in find_files(inp)]
    if not files:
        print("finances import: nothing to import", file=sys.stderr)
        return 1

    opts = {"account": args.account, "issuer": args.issuer, "kind": args.kind,
            "currency": args.currency, "date_fmt": args.date_format, "sign": args.sign}
    touched, failed, total = set(), 0, 0
    with FinanceDB(args.db) as db:
        for path in files:
            name = os.path.basename(path)
            from finances import lineitems
            if lineitems.kind_of_file(path):
                r = lineitems.import_file(args.db, path)
                print(f"  {name}\n    items: {r['matched']} of {r['groups']} matched a "
                      f"charge, {r['items']} items\n")
                continue
            try:
                results = import_file(db, path, force=args.force, fmt=args.format,
                                      **opts)
            except (ValueError, ET.ParseError, OSError) as e:
                print(f"  !! {name}: {e}")
                failed += 1
                continue
            if not results:
                print(f"  {name}\n    cached — unchanged since last import\n")
                continue
            for acct, st, skipped in results:
                touched.add(acct.account)
                total += len(acct.rows)
                print(f"  {acct.name}")
                print(f"    {acct.fmt.upper()} | {acct.issuer} "
                      f"{'····' + acct.account if acct.account else acct.name.rsplit('#', 1)[-1]}"
                      f" | {acct.kind} | {st.statement_period or 'no dates'}"
                      + (f" | {acct.currency}" if acct.currency else ""))
                print(f"    {len(acct.rows)} transactions"
                      + (f" ({skipped} already in the store, skipped)" if skipped else ""))
                verdict = {1: "OK", 0: "MISMATCH", None: "not checked"}[st.reconciled]
                print(f"    balances: {verdict}" + (f" — {st.reconcile_note}"
                                                  if st.reconcile_note else ""))
                print()
        for last4 in sorted(t for t in touched if t):
            for line in chain(db, last4):
                print(f"  chain  {line}")

    print(f"Imported {total} transactions from {len(files) - failed} file(s)"
          + (f"; {failed} could not be read" if failed else "") + f" -> {args.db}")
    print("Check the whole store with:  finances check")
    return 1 if failed and failed == len(files) else 0


if __name__ == "__main__":
    sys.exit(main())
