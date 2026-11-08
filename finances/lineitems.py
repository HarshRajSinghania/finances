"""
lineitems.py — what was in the box: the items behind an Amazon or store charge.

A card statement says "AMAZON MKTPL $86.41". Amazon's own order history says
it was a phone case, printer ink and a book. This reads that history (Amazon:
Account > "Request your data" > Your Orders, the Retail.OrderHistory CSV) and
puts each shipment's items under the card charge it explains.

A charge is matched only when a shipment's total equals it to the cent within
a few days of shipping, so nothing is attached by guesswork; shipments that
match nothing are counted and left out.

Any other shop works with a plain CSV of its receipts, one row per item:

    date,store,item,amount[,quantity]
    2026-10-04,Costco,Rotisserie chicken,4.99
    2026-10-04,Costco,Paper towels,21.99

(Costco and most shops do not export receipts, so that file is typed or
copied from the receipt; the matching is the same.)
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from finances.database import FinanceDB

AMAZON_COLS = {"order id", "order date", "total owed", "product name"}
RECEIPT_COLS = {"date", "store", "item", "amount"}
AMAZON_DESC = re.compile(r"amazon|amzn", re.I)
DAYS_BEFORE, DAYS_AFTER = 2, 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS line_items (
    source       TEXT NOT NULL,
    group_id     TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    quantity     REAL,
    amount       REAL,
    txn_date     TEXT,
    txn_description TEXT,
    txn_amount   REAL,
    txn_last4    TEXT,
    txn_seq      INTEGER,
    PRIMARY KEY (source, group_id, idx)
);
"""


def kind(header: list[str]) -> str | None:
    cols = {h.strip().lower() for h in header}
    if AMAZON_COLS <= cols:
        return "amazon"
    if RECEIPT_COLS <= cols:
        return "receipts"
    return None


def kind_of_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
            return kind(next(csv.reader(fh), []))
    except (OSError, StopIteration):
        return None


def _num(v) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _day(v) -> date | None:
    v = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v[:10], fmt).date()
        except ValueError:
            continue
    return None


def read(path: str) -> list[dict]:
    """Groups of items, each one charge: {id, date, store, total, items}."""
    k = kind_of_file(path)
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = [{(a or "").strip().lower(): (b or "").strip() for a, b in r.items()}
                for r in csv.DictReader(fh)]
    groups: dict[tuple, dict] = {}
    for r in rows:
        if k == "amazon":
            if "cancel" in r.get("order status", "").lower():
                continue
            # Amazon charges the card per shipment, not per order.
            d = _day(r.get("ship date")) or _day(r.get("order date"))
            key = (r["order id"], d)
            store, name = "Amazon", r.get("product name", "")
            amount, qty = _num(r.get("total owed")), _num(r.get("quantity") or 1)
        else:
            d = _day(r.get("date"))
            key = (r.get("store", ""), d)
            store, name = r.get("store", ""), r.get("item", "")
            amount, qty = _num(r.get("amount")), _num(r.get("quantity") or 1)
        if d is None or not name:
            continue
        g = groups.setdefault(key, {"id": f"{key[0]}@{d.isoformat()}", "date": d,
                                    "store": store, "total": 0.0, "items": []})
        g["items"].append({"name": name[:200], "quantity": qty, "amount": round(amount, 2)})
        g["total"] = round(g["total"] + amount, 2)
    return sorted(groups.values(), key=lambda g: g["date"])


def match(groups: list[dict], txns) -> list[tuple[dict, object]]:
    """Each group and the one charge it is, where exactly one fits."""
    taken, out = set(), []
    for g in groups:
        want = re.compile(re.escape(g["store"].split()[0]), re.I) if g["store"] != "Amazon" \
            else AMAZON_DESC
        fits = [t for t in txns
                if t.amount > 0 and abs(t.amount - g["total"]) <= 0.01
                and t.d and g["date"] - timedelta(days=DAYS_BEFORE) <= t.d
                <= g["date"] + timedelta(days=DAYS_AFTER)
                and want.search(t.description + " " + t.merchant)
                and id(t) not in taken]
        if fits:
            t = min(fits, key=lambda t: abs((t.d - g["date"]).days))
            taken.add(id(t))
            out.append((g, t))
    return out


def import_file(db_path: str, path: str) -> dict:
    import os
    groups = read(path)
    source = os.path.basename(path)
    with FinanceDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        pairs = match(groups, db.transactions())
        db.conn.execute("DELETE FROM line_items WHERE source = ?", (source,))
        db.conn.executemany(
            "INSERT INTO line_items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(source, g["id"], i, it["name"], it["quantity"], it["amount"],
              t.date, t.description, round(t.amount, 2), t.account_last4, t.seq)
             for g, t in pairs for i, it in enumerate(g["items"])])
        db.conn.commit()
    return {"file": source, "kind": kind_of_file(path), "groups": len(groups),
            "matched": len(pairs), "items": sum(len(g["items"]) for g, _ in pairs)}


def items_by_txn(db) -> dict[tuple, list[dict]]:
    """(date, description, amount, last4, seq) -> the items behind that charge."""
    db.conn.executescript(SCHEMA)
    out: dict[tuple, list[dict]] = defaultdict(list)
    for r in db.conn.execute("SELECT * FROM line_items ORDER BY source, group_id, idx"):
        out[(r["txn_date"], r["txn_description"], r["txn_amount"], r["txn_last4"],
             r["txn_seq"])].append({"name": r["name"], "quantity": r["quantity"],
                                    "amount": r["amount"]})
    return out


def main() -> int:
    import argparse
    from finances.database import DEFAULT_DB
    ap = argparse.ArgumentParser(prog="finances items", description=__doc__.split("\n\n")[1])
    ap.add_argument("files", nargs="+", help="Amazon Retail.OrderHistory CSV, or a receipts CSV")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    for path in args.files:
        if not kind_of_file(path):
            print(f"  {path}: not an Amazon order history or a receipts CSV")
            continue
        r = import_file(args.db, path)
        print(f"  {r['file']}: {r['matched']} of {r['groups']} "
              f"{'shipments' if r['kind'] == 'amazon' else 'receipts'} matched a charge, "
              f"{r['items']} items")
    return 0
