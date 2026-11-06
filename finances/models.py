"""
The shared data schema.

Every tool moves transactions and statement summaries around as rows. Before,
each script hard-coded the column list in its own DictWriter/DictReader call, so
the CSV header lived in a dozen places. These two dataclasses are now the single
definition of what a transaction and a statement are. CSV columns, the SQLite
schema and the parser all derive their field list from here.

Sign convention (kept from parse.py): money OUT (purchases, fees) is POSITIVE,
money IN (payments, refunds, credits) is NEGATIVE.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


@dataclass(slots=True)
class Transaction:
    date: str                       # ISO yyyy-mm-dd (transaction date)
    description: str                # raw descriptor as printed
    amount: float                   # OUT positive, IN negative
    merchant: str = ""              # cleaned name for grouping/display
    category: str = "Uncategorized"
    direction: str = ""             # "out" | "in" (derived from amount sign)
    post_date: str = ""             # ISO yyyy-mm-dd, "" if the statement omits it
    issuer: str = ""
    account_last4: str = ""
    statement_period: str = ""
    source_file: str = ""
    # Which occurrence this is of an otherwise identical charge on the same day.
    # Two $3.00 tap-ins on the same morning are two real charges, not a double
    # parse; without this they collide on the primary key and one is lost.
    seq: int = 0

    def __post_init__(self):
        if not self.direction:
            self.direction = "out" if self.amount > 0 else "in"
        if not self.merchant:
            self.merchant = self.description

    # -- row (de)serialization -------------------------------------------
    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_row(cls, row: dict) -> "Transaction":
        """Build from a CSV/DB row, tolerating missing optional columns.

        Every optional string goes through ``or ""``, not ``get(k, "")``: after
        an ``ALTER TABLE`` migration the column exists holding NULL, so the key
        is present and the default never fires. That put None into
        ``dedup_key`` and through to every downstream string operation.
        """
        return cls(
            date=str(row["date"])[:10],
            description=row.get("description") or "",
            amount=float(row["amount"]),
            merchant=(row.get("merchant") or row.get("description") or "").strip(),
            category=row.get("category") or "Uncategorized",
            direction=row.get("direction") or "",
            post_date=row.get("post_date") or "",
            issuer=row.get("issuer") or "",
            account_last4=row.get("account_last4") or "",
            statement_period=row.get("statement_period") or "",
            source_file=row.get("source_file") or "",
            seq=int(row.get("seq") or 0),
        )

    def to_row(self) -> dict:
        return asdict(self)

    @property
    def dedup_key(self) -> tuple:
        """Natural key used to drop a statement parsed twice.

        ``seq`` is what separates "this file was parsed twice" from "this really
        did happen twice today". Re-parsing a statement regenerates the same
        ordinals and folds; two genuine identical charges keep ordinals 0 and 1
        and both survive.
        """
        return (self.date, self.description, round(self.amount, 2),
                self.account_last4, self.seq)

    @property
    def d(self):
        """The transaction date as a ``date``, or None if it is unparseable."""
        from datetime import datetime
        try:
            return datetime.strptime(self.date[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    @property
    def month(self) -> str:
        return self.date[:7]


# The per-statement summary (balances, limits, APR). Optional numeric fields are
# None when the statement doesn't print them.
@dataclass(slots=True)
class Statement:
    source_file: str
    issuer: str = ""
    account_last4: str = ""
    statement_period: str = ""
    previous_balance: float | None = None
    new_balance: float | None = None
    purchases: float | None = None
    payments: float | None = None
    interest_charged: float | None = None
    fees_charged: float | None = None
    minimum_payment: float | None = None
    credit_limit: float | None = None
    available_credit: float | None = None
    utilization_pct: float | None = None
    purchase_apr: float | None = None
    cash_advances: float | None = None
    ytd_interest: float | None = None
    ytd_fees: float | None = None
    rewards_balance: float | None = None
    charges_extracted: float | None = None
    credits_extracted: float | None = None

    # -- coverage ---------------------------------------------------------
    # ISO bounds of statement_period, so gaps between statements can be found
    # without re-parsing the free-text period string in every tool.
    period_start: str = ""
    period_end: str = ""

    # -- reconciliation ---------------------------------------------------
    # Does what we extracted agree with what the statement says about itself?
    # This is the only ground truth available, so it is stored, not just printed.
    reconciled: int | None = None        # 1 ok, 0 mismatch, None nothing to check
    reconcile_diff: float | None = None  # extracted charges - printed purchases
    reconcile_note: str = ""             # why it could not be checked, or what broke

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "Statement":
        """Build from a DB/CSV row, tolerating columns an older store lacks."""
        out = {}
        for f in fields(cls):
            v = row.get(f.name)
            if v is None or v == "":
                continue
            if f.type.startswith("float"):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
            elif f.type.startswith("int"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            out[f.name] = v
        out.setdefault("source_file", row.get("source_file", ""))
        return cls(**out)

    @property
    def trusted(self) -> bool:
        """True when the extracted charges match the statement's own total."""
        return self.reconciled == 1
