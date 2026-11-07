"""
The engine's vocabulary: inputs, records, checks, matches, findings.

Everything here is plain data. Each object turns itself into JSON with money as
decimal strings, so a report can be read back by a program that never imports
this module.

Sign convention: amounts are signed from the account holder's side. Money that
comes INTO the account is positive, money that goes OUT is negative. That is
the convention of ISO 20022, most bank exports and accounting ledgers. (The
personal-finance app above this engine counts spending as positive; adapters
convert, and record that they did.)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from finances.verify import money

# Check statuses. Deliberately not a score: a verified balance says nothing
# about whether categories are right, so each claim is checked and reported on
# its own.
VERIFIED = "verified"           # an invariant held, on evidence the engine has
MISMATCH = "mismatch"           # the evidence contradicts the expected result
UNVERIFIABLE = "unverifiable"   # nothing independent to check against
NEEDS_REVIEW = "needs_review"   # ambiguous; a person has to decide
INCOMPLETE = "incomplete"       # known gaps in what was supplied
STATUSES = (VERIFIED, MISMATCH, UNVERIFIABLE, NEEDS_REVIEW, INCOMPLETE)

# Match decisions (Fellegi–Sunter's link / possible link / non-link, plus an
# explicit tie and a search that ran out of budget).
MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"
REJECTED = "rejected"           # a reviewer said no
DECISIONS = (MATCHED, NEEDS_REVIEW, AMBIGUOUS, UNRESOLVED, REJECTED)

# Where an input sits on ISA 500's reliability scale.
EXTERNAL = "external"           # produced by the bank or processor
INTERNAL = "internal"           # produced by the business (its ledger)


def digest(obj) -> str:
    """sha256 of canonical JSON: sorted keys, no whitespace, no floats (RFC 8785 style)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_plain)
    return hashlib.sha256(blob.encode()).hexdigest()


def short_id(prefix: str, obj) -> str:
    return f"{prefix}_{digest(obj)[:16]}"


def _plain(o):
    if isinstance(o, Decimal):
        return money.text(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def _clean(d: dict) -> dict:
    """Drop empty optional fields so reports stay readable."""
    return {k: (money.text(v) if isinstance(v, Decimal) else v)
            for k, v in d.items() if v not in (None, "", [], {})}


@dataclass
class Input:
    """One file (or one batch of rows handed over in code)."""
    id: str
    name: str
    sha256: str
    role: str                       # bank | processor | ledger
    format: str                     # pdf, csv, ofx, camt053, mt940, stripe, ledger-csv, rows
    adapter: str
    adapter_version: str
    source_class: str = EXTERNAL
    media_type: str = ""
    options: dict = field(default_factory=dict)
    path: str = ""                  # not part of the report: machine-specific
    records: int = 0

    def to_dict(self) -> dict:
        return _clean({"id": self.id, "name": self.name, "digest": {"sha256": self.sha256},
                       "role": self.role, "format": self.format,
                       "adapter": {"name": self.adapter, "version": self.adapter_version},
                       "source_class": self.source_class, "media_type": self.media_type,
                       "options": self.options, "records": self.records})


@dataclass
class SourceRef:
    """Where a record came from, precisely enough to find it again by hand."""
    input_id: str
    locator: dict = field(default_factory=dict)     # page+line, row, fitid, entry …
    raw: dict = field(default_factory=dict)         # the values as the source wrote them
    sign: str = ""                                  # "as written" | "inverted"
    method: str = "deterministic"                   # or "llm" when a model read it

    def to_dict(self) -> dict:
        return _clean({"input": self.input_id, "locator": self.locator, "raw": self.raw,
                       "sign": self.sign, "method": self.method})


@dataclass
class Record:
    """One normalized money movement, with the original kept beside it."""
    id: str
    content_key: str
    input_id: str
    role: str
    account: str
    date: str                       # ISO; the booking date
    amount: Decimal                 # holder's side: in +, out −
    currency: str
    description: str
    kind: str = ""                  # charge, refund, fee, payout, deposit … when known
    refs: dict = field(default_factory=dict)    # payout id, trace id, FITID, doc number …
    dates: dict = field(default_factory=dict)   # value date, available_on …
    detail: dict = field(default_factory=dict)  # gross, fee, item count …
    group: str = ""                 # the settlement this record belongs to
    source: SourceRef | None = None
    duplicate_of: str = ""          # set when an overlapping input already had it

    @property
    def minor(self) -> int:
        return money.minor(self.amount, self.currency)

    def to_dict(self) -> dict:
        return _clean({
            "id": self.id, "content_key": self.content_key, "account": self.account,
            "date": self.date, "amount": self.amount, "currency": self.currency,
            "description": self.description, "kind": self.kind, "refs": self.refs,
            "dates": self.dates,
            "detail": {k: money.text(v) if isinstance(v, Decimal) else v
                       for k, v in self.detail.items()},
            "group": self.group, "duplicate_of": self.duplicate_of,
            "source": self.source.to_dict() if self.source else None})


@dataclass
class Period:
    """What an input says about one account over one stretch of time."""
    input_id: str
    account: str
    currency: str
    start: str = ""
    end: str = ""
    declared: bool = False          # the source states its period (not just first/last row)
    opening: Decimal | None = None  # holder's side
    closing: Decimal | None = None
    records: list[str] = field(default_factory=list)
    # A verdict the source's own reader already reached (printed totals on a PDF,
    # a running-balance chain in a CSV): (status, rule, message).
    prior: tuple[str, str, str] | None = None
    identity_meaningful: bool = True    # False when opening was derived from closing


@dataclass
class Check:
    id: str
    rule: str
    status: str
    message: str
    subject: dict = field(default_factory=dict)
    expected: Decimal | None = None
    computed: Decimal | None = None
    difference: Decimal | None = None
    tolerance: Decimal | None = None
    currency: str = ""
    evidence: list[str] = field(default_factory=list)   # input ids
    records: list[str] = field(default_factory=list)
    locations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _clean({k: getattr(self, k) for k in (
            "id", "rule", "status", "message", "subject", "expected", "computed",
            "difference", "tolerance", "currency", "evidence", "records", "locations")})


@dataclass
class Match:
    id: str
    pair: str                       # e.g. "bank~ledger"
    left: list[str]
    right: list[str]
    decision: str
    tier: str                       # reference | exact | group | explained | near
    reasons: list[str] = field(default_factory=list)
    difference: Decimal = money.ZERO
    currency: str = ""
    block: dict = field(default_factory=dict)
    explanation: str = ""
    override: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _clean({k: getattr(self, k) for k in (
            "id", "pair", "left", "right", "decision", "tier", "reasons", "difference",
            "currency", "block", "explanation", "override")}
            | {"difference": money.text(self.difference)})


@dataclass
class Finding:
    """Something a person should look at, ranked by how much money is involved."""
    id: str
    kind: str                       # missing_in_ledger, amount_difference, duplicate …
    severity: str                   # high | medium | low | info
    message: str
    records: list[str] = field(default_factory=list)
    amount: Decimal | None = None
    currency: str = ""
    related: list[str] = field(default_factory=list)    # check or match ids
    locations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _clean({k: getattr(self, k) for k in (
            "id", "kind", "severity", "message", "records", "amount", "currency",
            "related", "locations")})
