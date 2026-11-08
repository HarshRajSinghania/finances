#!/usr/bin/env python3
"""
api.py — a small JSON API over the finances store, for the web dashboard.

    finances web                      # serve on http://localhost:8787
    finances web --port 9000 --db finances.db
    finances web --open               # and open a browser

Written on ``http.server`` rather than Flask or FastAPI on purpose. The whole
claim of this project is that it runs on your machine with one dependency and
touches no network, and a web dashboard is exactly the point where a project
starts quietly growing a server framework, an ORM and a websocket layer. The
endpoints here are a few hundred lines of standard library, they bind to
localhost only, and they read the same :mod:`engine` every CLI tool reads — so
the page can never disagree with what ``finances check`` says.

Endpoints
---------
    GET  /api/health
    GET  /api/summary        headline numbers plus the reconciliation verdict
    GET  /api/statements     every parsed statement, with its reconciliation
    GET  /api/transactions   filterable, paginated
    GET  /api/categories     spend by category
    GET  /api/months         spend and income per month
    GET  /api/recurring      subscriptions, with price drift and next due date
    GET  /api/merchants      top merchants
    GET  /api/anomalies      duplicates, outliers and the largest charges
    GET  /api/wrapped        a year in review (?year=2025)
    GET  /api/budgets        this month against each category's limit
    GET  /api/goals          savings goals against the real monthly surplus
    GET  /api/subscriptions/review   the cancel list
    POST /api/upload         statement PDFs or bank exports, read straight into the store
    POST /api/budgets        {"category": "Dining", "limit": 300}  (null removes)
    POST /api/goals          {"name": "Trip", "target": 3000, "by": "2027-06"}
    POST /api/subscriptions/mark   {"key": "NETFLIX", "status": "keep"|"cancelled"|null}
    GET  /api/payments       each card's latest due date, and whether it is paid
    POST /api/payments/due-dates  read due dates off stored card statements
    GET  /api/networth       what you own minus what you owe, month by month
    GET  /api/sources        statements that should be ready, and where to get them
    POST /api/watch          {"folder": "~/Downloads"} adds new statements found there (null stops)
    GET  /api/feed           the SimpleFIN live feed, and how it agreed with each statement
    GET  /api/phone          the read-only phone view: {"on", "url"}
    POST /api/phone          {"on": true} opens it on the home network, false closes it
    GET  /api/session        {"readOnly": true} when this is the phone view
    GET  /api/clients        with --clients: every client folder and which one is open
    POST /api/clients        {"name": "Acme", "create": true} opens (or makes) a client
    GET  /api/workpapers.xlsx  the reconciliation as a workbook to review and file
    POST /api/feed           {"action": "connect", "token": ...} | sync | disconnect
    POST /api/transactions/category  {"rows": [...], "category": "Dining", "remember": true}
    GET  /api/export.xlsx    everything as an Excel workbook (download)
    GET  /api/originals/<source_file>  the PDF or export a statement was read from
    GET  /api/ask            which providers and models the Ask page can use
    GET  /api/ask/local      a local model server and its models (?endpoint=…)
    GET  /api/ask/history    the saved conversation
    DELETE /api/ask/history  forget it
    POST /api/ask            {"provider": "anthropic", "model": ..., "key": ...,
                              "messages": [{"role": "user", "content": "..."}]}
    DELETE /api/statements/<source_file>
    DELETE /api/goals/<name>

Every write must come from a page on localhost with a JSON body, so another
website open in the same browser cannot change budgets behind your back.

In production it also serves the built frontend from ``web/dist``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import secrets
import threading
import urllib.parse
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from finances import chat, sources
from finances.database import DEFAULT_DB, FinanceDB
from finances.engine import Engine, NON_SPEND
from finances.utils import merchant_key

# The repository root, when running from a checkout: the package lives one
# level down and the dashboard source beside it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.dirname(os.path.abspath(__file__))


def _web_dist() -> str:
    """Where the built dashboard is: a checkout's web/dist, else the copy in the wheel."""
    if os.environ.get("FINANCES_WEB_DIST"):
        return os.environ["FINANCES_WEB_DIST"]
    checkout = os.path.join(ROOT, "web", "dist")
    if os.path.isdir(checkout) or os.path.exists(os.path.join(ROOT, "web", "package.json")):
        return checkout
    return os.path.join(PACKAGE, "web_dist")


# Overridable so a container (or anyone running from another directory) can
# keep the code in one place and the data in another. Uploads land next to
# wherever finances.db is used from, the same way every CLI tool resolves it.
PDF_DIR = os.environ.get("FINANCES_PDF_DIR") or os.path.join(os.getcwd(), "PDFs")
PLAN_PATH = os.environ.get("FINANCES_PLAN") or "plan.json"
WEB_DIST = _web_dist()
MAX_UPLOAD = 32 * 1024 * 1024          # a statement PDF is tens of KB; 32MB is generous

# Names a browser may use to reach this server. Anything else in the Host header
# is a DNS-rebinding attempt: a hostile page pointing its own domain at 127.0.0.1
# to read the API as if it were same-origin.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
# Other names the dashboard is reached by, where the person running it chose
# them: a home server's "umbrel.local", say. Each one is trusted exactly as
# localhost is, for Host and for Origin, so list only names on your own network.
ALLOWED_HOSTS = LOCAL_HOSTS | {h.strip().lower() for h in
                               os.environ.get("FINANCES_ALLOWED_HOSTS", "").split(",")
                               if h.strip()}

# One writer at a time. Parsing mutates the store, and two uploads landing
# together would race on the same SQLite file.
_parse_lock = threading.RLock()


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


# How an original is shown in the browser. Only PDFs render as themselves;
# every export is shown as plain text, so an OFX or XML file can never run as
# a page on this origin.
_ORIGINAL_TYPES = {".pdf": "application/pdf"}


def _original(source_file: str, folder: str | None = None) -> str | None:
    """Where the file a statement was read from is kept, if it still is.

    A multi-account export is stored as "file.ofx#1234" per account; the
    original is the one file.
    """
    name = os.path.basename(source_file.split("#", 1)[0])
    path = os.path.join(folder or PDF_DIR, name)
    return path if name and os.path.isfile(path) else None


# Statements already read for a due date that print none, so they are not
# offered again.
DUE_CHECKED = "due_dates_checked"


def _due_unread(statements, checked=(), folder: str | None = None) -> list:
    """Statement PDFs on disk that may print a due date nobody has read yet.

    Stores from before card statements were marked as cards cannot tell a card
    from a checking account, so anything not known to be a bank account is
    read once; the ones that print no due date are remembered in ``checked``.
    """
    return [s for s in statements
            if not s.payment_due and s.source_file not in checked
            and (s.source_format or "pdf").startswith("pdf") and s.account_type != "bank"
            and os.path.isfile(os.path.join(folder or PDF_DIR, s.source_file))]


def _statement_dict(s) -> dict:
    return {
        "sourceFile": s.source_file,
        "issuer": s.issuer,
        "accountLast4": s.account_last4,
        "period": s.statement_period,
        "periodStart": s.period_start or None,
        "periodEnd": s.period_end or None,
        "previousBalance": s.previous_balance,
        "newBalance": s.new_balance,
        "purchases": s.purchases,
        "payments": s.payments,
        "interestCharged": s.interest_charged,
        "feesCharged": s.fees_charged,
        "creditLimit": s.credit_limit,
        "purchaseApr": s.purchase_apr,
        "chargesExtracted": s.charges_extracted,
        "creditsExtracted": s.credits_extracted,
        # The verdict is the point of this whole project, so it is never
        # collapsed into a boolean: "not checked" and "does not add up" are
        # different answers and the UI has to be able to show them differently.
        "reconciled": s.reconciled,
        "reconcileDiff": s.reconcile_diff,
        "reconcileNote": s.reconcile_note or "",
        "status": ("ok" if s.reconciled == 1
                   else "mismatch" if s.reconciled == 0 else "unchecked"),
        "sourceFormat": s.source_format or "",
        "accountType": s.account_type or "",
        "paymentDue": s.payment_due or None,
    }


def _txn_dict(t) -> dict:
    return {
        "date": t.date,
        "postDate": t.post_date or None,
        "description": t.description,
        "merchant": t.merchant,
        "merchantKey": merchant_key(t.merchant),
        "amount": round(t.amount, 2),
        "direction": t.direction,
        "category": t.category,
        "issuer": t.issuer,
        "accountLast4": t.account_last4,
        "sourceFile": t.source_file,
        "seq": t.seq,
        "page": t.page,
    }


def _recurring_dict(r) -> dict:
    return {
        "key": r.key,
        "name": r.name,
        "category": r.category,
        "cadence": r.cadence,
        "amount": round(r.amount, 2),
        "n": r.n,
        "perMonth": round(r.per_month, 2),
        "perYear": round(r.per_year, 2),
        "firstSeen": r.first_seen,
        "lastSeen": r.last_seen,
        "dayOfMonth": r.day_of_month,
        "nextExpected": r.next_expected,
        "priceChange": round(r.price_change, 2),
        "pricePct": round(r.price_pct, 1),
        "confidence": r.confidence,
        "isStale": r.is_stale(date.today()),
    }


def _coverage_dict(cov) -> dict:
    return {
        "statements": cov["statements"],
        "reconciled": cov["reconciled"],
        "mismatched": cov["mismatched"],
        "unchecked": cov["unchecked"],
        "pct": round(cov["pct"], 1),
        "trusted": cov["trusted"],
        "gaps": cov["gaps"],
        "mismatches": [
            {"sourceFile": s.source_file, "issuer": s.issuer,
             "diff": s.reconcile_diff, "note": s.reconcile_note}
            for s in cov["mismatches"]
        ],
    }


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

class Api:
    """Each method returns a plain dict; the handler does the JSON and HTTP."""

    def __init__(self, db_path: str, pdf_dir: str | None = None):
        self.db_path = db_path
        self._pdf_dir = pdf_dir
        self.watcher = sources.Watcher(self)
        self.workspaces: str | None = None     # a folder of client folders, when serving several
        self.client: str | None = None

    @property
    def pdf_dir(self) -> str:
        """Where this store's statements are kept: its own folder for a client."""
        return self._pdf_dir or PDF_DIR

    def engine(self) -> Engine:
        return Engine(self.db_path)

    def health(self, q) -> dict:
        exists = os.path.exists(self.db_path)
        return {"ok": True, "db": os.path.basename(self.db_path),
                "dbExists": exists,
                "transactions": FinanceDB(self.db_path).count() if exists else 0}

    def summary(self, q) -> dict:
        with self.engine() as eng:
            rng = eng.dates()
            months = eng.by_month()
            complete = eng.complete_months()
            income = eng.income_average()
            spend_total = eng.total_spend()
            return {
                "transactions": len(eng.transactions),
                "spendTransactions": len(eng.spend()),
                "totalSpend": round(spend_total, 2),
                "totalCredits": round(eng.total_credits(), 2),
                "monthlyAverage": round(eng.monthly_average(), 2),
                "essentialMonthly": round(eng.essential_monthly(), 2),
                "committedMonthly": round(eng.committed_monthly(), 2),
                "staleMonthly": round(eng.stale_monthly(), 2),
                "incomeAverage": round(income, 2) if income is not None else None,
                "surplus": (round(eng.surplus(), 2)
                            if eng.surplus() is not None else None),
                "firstDate": rng[0] if rng else None,
                "lastDate": rng[1] if rng else None,
                "monthsCovered": len(months),
                "completeMonths": complete,
                "subscriptionCount": len(eng.subscriptions()),
                "coverage": _coverage_dict(eng.coverage()),
                "accounts": [
                    {"issuer": i, "last4": l}
                    for i, l in sorted({(t.issuer, t.account_last4)
                                        for t in eng.transactions if t.issuer})
                ],
                "currency": eng.currency,
                "fxMissing": dict(eng.fx_missing),
            }

    def statements(self, q) -> dict:
        with self.engine() as eng:
            # A live feed is not a statement; it has its own panel.
            rows = [_statement_dict(s) for s in eng.statements()
                    if s.source_format != "simplefin"]
            # Transaction counts per file, so the UI can show what each yielded.
            counts: dict[str, int] = {}
            for t in eng.transactions:
                counts[t.source_file] = counts.get(t.source_file, 0) + 1
            for r in rows:
                r["transactionCount"] = counts.get(r["sourceFile"], 0)
                r["hasFile"] = _original(r["sourceFile"], self.pdf_dir) is not None
            rows.sort(key=lambda r: (r["periodStart"] or r["period"] or "",
                                     r["sourceFile"]))
            return {"statements": rows, "coverage": _coverage_dict(eng.coverage())}

    def transactions(self, q) -> dict:
        with self.engine() as eng:
            rows = eng.transactions
            search = (q.get("search") or "").strip().lower()
            category = q.get("category")
            source = q.get("sourceFile")
            direction = q.get("direction")
            start, end = q.get("start"), q.get("end")
            spend_only = q.get("spendOnly") == "true"

            if spend_only:
                rows = [t for t in rows
                        if t.amount > 0 and t.category not in NON_SPEND]
            if search:
                rows = [t for t in rows
                        if search in t.description.lower()
                        or search in t.merchant.lower()]
            if category:
                rows = [t for t in rows if t.category == category]
            if source:
                rows = [t for t in rows if t.source_file == source]
            if direction:
                rows = [t for t in rows if t.direction == direction]
            if start:
                rows = [t for t in rows if t.date >= start]
            if end:
                rows = [t for t in rows if t.date <= end]

            sort = q.get("sort") or "date"
            desc = q.get("order", "desc") == "desc"
            keyfn = {"date": lambda t: t.date,
                     "amount": lambda t: t.amount,
                     "merchant": lambda t: t.merchant.lower(),
                     "category": lambda t: t.category}.get(sort, lambda t: t.date)
            rows = sorted(rows, key=keyfn, reverse=desc)

            total = len(rows)
            try:
                limit = max(1, min(int(q.get("limit", 100)), 1000))
                offset = max(0, int(q.get("offset", 0)))
            except ValueError:
                limit, offset = 100, 0

            page = rows[offset:offset + limit]
            from finances.lineitems import items_by_txn
            items = items_by_txn(eng.db) if eng.db is not None else {}
            out = []
            for t in page:
                d = _txn_dict(t)
                found = items.get((t.date, t.description, round(t.amount, 2),
                                   t.account_last4, t.seq))
                if found:
                    d["items"] = found
                out.append(d)
            return {
                "transactions": out,
                "total": total, "limit": limit, "offset": offset,
                "sumAmount": round(sum(t.amount for t in rows), 2),
            }

    def categories(self, q) -> dict:
        with self.engine() as eng:
            by_cat = eng.by_category()
            monthly = eng.by_category_monthly()
            total = sum(by_cat.values()) or 1.0
            counts: dict[str, int] = {}
            for t in eng.spend():
                counts[t.category] = counts.get(t.category, 0) + 1
            return {"categories": [
                {"category": c, "total": round(v, 2),
                 "perMonth": round(monthly.get(c, 0.0), 2),
                 "share": round(v / total * 100, 1),
                 "count": counts.get(c, 0)}
                for c, v in by_cat.items()
            ]}

    def months(self, q) -> dict:
        with self.engine() as eng:
            spend, income = eng.by_month(), eng.income_by_month()
            complete = set(eng.complete_months())
            keys = sorted(set(spend) | set(income))
            return {"months": [
                {"month": m,
                 "spend": round(spend.get(m, 0.0), 2),
                 "income": round(income.get(m, 0.0), 2),
                 "net": round(income.get(m, 0.0) - spend.get(m, 0.0), 2),
                 "complete": m in complete}
                for m in keys
            ]}

    def recurring(self, q) -> dict:
        with self.engine() as eng:
            rows = (eng.recurring() if q.get("includeInterest") == "true"
                    else eng.subscriptions())
            return {"recurring": [_recurring_dict(r) for r in rows],
                    "committedMonthly": round(eng.committed_monthly(), 2),
                    "staleMonthly": round(eng.stale_monthly(), 2)}

    def merchants(self, q) -> dict:
        with self.engine() as eng:
            try:
                limit = max(1, min(int(q.get("limit", 50)), 500))
            except ValueError:
                limit = 50
            return {"merchants": [
                {"key": m["key"], "name": m["name"], "category": m["category"],
                 "total": round(m["total"], 2), "n": m["n"],
                 "avg": round(m["avg"], 2), "last": m["last"]}
                for m in eng.merchants()[:limit]
            ]}

    def anomalies(self, q) -> dict:
        """Duplicates, outliers and the biggest charges.

        Imported from analyze.py rather than reimplemented, so the page and the
        generated report cannot drift apart.
        """
        from collections import defaultdict

        from finances.analyze import find_anomalies
        with self.engine() as eng:
            spend = eng.spend()
            groups = defaultdict(list)
            for t in spend:
                groups[merchant_key(t.merchant)].append(t)
            a = find_anomalies(spend, groups)
            return {
                "duplicates": [
                    {"name": d["name"], "amount": round(d["amount"], 2),
                     "date": d["date"], "n": d["n"]} for d in a["dupes"]],
                "outliers": [
                    {"name": o["name"], "date": o["date"],
                     "amount": round(o["amount"], 2),
                     "typical": round(o["typical"], 2),
                     "category": o["category"]} for o in a["outliers"]],
                "largest": [_txn_dict(t) for t in a["big"]],
            }

    def wrapped(self, q) -> dict:
        from finances.wrapped import wrapped
        with self.engine() as eng:
            try:
                year = int(q["year"]) if q.get("year") else None
            except ValueError:
                year = None
            return wrapped(eng, year)

    def budgets(self, q) -> dict:
        from finances.budgets import status
        with self.engine() as eng:
            return status(eng)

    def subscription_review(self, q) -> dict:
        from finances.budgets import review
        with self.engine() as eng:
            rv = review(eng)
            def item(i):
                d = _recurring_dict(i["r"])
                d["reasons"] = i["reasons"]
                d["markedAt"] = i["mark"].get("at")
                return d
            return {k: ([item(i) for i in v] if isinstance(v, list) else v)
                    for k, v in rv.items()}

    def goals(self, q) -> dict:
        from finances.planner import load_plan, plan_goals
        plan = load_plan(PLAN_PATH)
        with self.engine() as eng:
            surplus = plan.get("monthly_surplus")
            source = "plan"
            if surplus is None:
                surplus, source = eng.surplus(), "statements"
            committed = eng.committed_monthly()
        goals, left = plan_goals(plan, surplus)
        return {
            "surplus": round(surplus, 2) if surplus is not None else None,
            "surplusSource": source if surplus is not None else None,
            "committedMonthly": round(committed, 2),
            "unallocated": round(left, 2) if surplus is not None else None,
            "goals": [{
                "name": g["name"], "target": g["target"], "saved": g.get("saved", 0.0),
                "by": g.get("by"), "priority": g.get("priority", 3),
                "monthly": g.get("monthly"), "note": g.get("note"),
                "remaining": round(g["remaining"], 2),
                "allocated": round(g["allocated"], 2),
                "required": round(g["required"], 2) if g["required"] is not None else None,
                "eta": g.get("eta"), "late": g.get("late", False),
                "pct": round(min(g.get("saved", 0.0) / g["target"] * 100, 100), 1)
                       if g["target"] else 0.0,
            } for g in goals],
        }

    # -- writes ----------------------------------------------------------
    def set_budget(self, body: dict) -> dict:
        from finances.budgets import set_budget
        cat = str(body.get("category") or "").strip()
        if not cat:
            raise ValueError("category is required")
        limit = body.get("limit")
        with self.engine() as eng:
            set_budget(eng, cat, None if limit in (None, "") else float(limit))
        return self.budgets({})

    def mark_subscription(self, body: dict) -> dict:
        from finances.budgets import mark
        key = str(body.get("key") or "").strip()
        state = body.get("status")
        if not key or state not in ("keep", "cancelled", None):
            raise ValueError("key and a status of keep, cancelled or null are required")
        with self.engine() as eng:
            mark(eng, key, state)
        return self.subscription_review({})

    def save_goal(self, body: dict) -> dict:
        from finances.planner import find_goal, load_plan, save_plan
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("a goal needs a name")
        plan = load_plan(PLAN_PATH)
        g = next((g for g in plan["goals"] if g["name"].lower() == name.lower()), None)
        fields = {}
        for k in ("target", "saved", "monthly"):
            if body.get(k) not in (None, ""):
                fields[k] = float(body[k])
        if body.get("priority") not in (None, ""):
            fields["priority"] = int(body["priority"])
        if "by" in body:
            fields["by"] = (str(body["by"])[:7] or None) if body["by"] else None
        if g is None:
            if "target" not in fields:
                raise ValueError("a new goal needs a target")
            plan["goals"].append({"name": name, "saved": 0.0, "priority": 3,
                                  "by": None, "monthly": None, "note": None,
                                  "created": date.today().isoformat(), "done": False,
                                  **fields})
        else:
            g.update(fields)
        save_plan(plan, PLAN_PATH)
        return self.goals({})

    def delete_goal(self, name: str) -> dict:
        from finances.planner import load_plan, save_plan
        name = urllib.parse.unquote(name)
        plan = load_plan(PLAN_PATH)
        plan["goals"] = [g for g in plan["goals"] if g["name"] != name]
        save_plan(plan, PLAN_PATH)
        return self.goals({})

    def payments(self, q) -> dict:
        """Each card's most recent statement with a due date, and what has been
        paid towards it since the statement closed.

        Paid means payments seen in your files: a payment made after the latest
        export is not here yet, so the page says "as far as your files show".
        """
        today = date.today()
        with FinanceDB(self.db_path) as db:
            statements = db.statement_models()
            txns = db.transactions()
            checked = set(db.get_setting(DUE_CHECKED, []))
        latest = {}
        for s in statements:
            if not s.payment_due:
                continue
            key = (s.issuer, s.account_last4)
            if key not in latest or s.payment_due > latest[key].payment_due:
                latest[key] = s
        seen_until = max((t.date for t in txns), default="")
        out = []
        for (issuer, last4), s in sorted(latest.items(), key=lambda kv: kv[1].payment_due):
            due = date.fromisoformat(s.payment_due)
            paid = round(sum(-t.amount for t in txns
                             if t.account_last4 == last4 and t.issuer == issuer
                             and t.category == "Payment" and t.amount < 0
                             and s.period_end < t.date <= s.payment_due), 2)
            balance, minimum = s.new_balance, s.minimum_payment
            if balance is not None and balance <= 0:
                state = "nothing due"
            elif balance is not None and paid >= balance - 0.01:
                state = "paid"
            elif minimum is not None and paid >= minimum - 0.01 and paid > 0:
                state = "minimum paid"
            elif due < today:
                state = "unpaid" if seen_until >= s.payment_due else "unknown"
            else:
                state = "due"
            out.append({"issuer": issuer, "last4": last4, "due": s.payment_due,
                        "daysLeft": (due - today).days, "balance": balance,
                        "minimum": minimum, "paid": paid, "state": state,
                        "statement": s.source_file, "closed": s.period_end})
        return {"payments": out, "seenUntil": seen_until,
                "unread": len(_due_unread(statements, checked, self.pdf_dir))}

    def set_category(self, body: dict) -> dict:
        """Put the chosen rows in a category, and optionally remember the merchants.

        Remembering teaches the categorizer, which is what every later import
        and re-parse goes through, so the same charges land there from now on;
        rows already in the store from those merchants move too.
        """
        from finances.categorizer import LEARNED, Categorizer, normalize
        category = str(body.get("category") or "").strip()
        rows = body.get("rows") or []
        if not category or len(category) > 40:
            raise ValueError("pick a category (at most 40 characters)")
        if not isinstance(rows, list) or not rows or len(rows) > 5000:
            raise ValueError("choose between 1 and 5000 transactions")
        keys = [(str(r["date"]), str(r["description"]), round(float(r["amount"]), 2),
                 str(r.get("accountLast4") or ""), int(r.get("seq") or 0)) for r in rows]
        taught: list[str] = []
        with FinanceDB(self.db_path) as db:
            cur = db.conn.executemany(
                "UPDATE transactions SET category = ? WHERE date = ? AND description = ? "
                "AND ROUND(amount, 2) = ? AND account_last4 = ? AND seq = ?",
                [(category, *k) for k in keys])
            updated = cur.rowcount
            also = 0
            if body.get("remember"):
                cz = Categorizer(os.path.join(os.path.dirname(os.path.abspath(self.db_path)),
                                              LEARNED))
                for desc in sorted({k[1] for k in keys}):
                    try:
                        taught.append(cz.teach(desc, category))
                    except ValueError:
                        continue
                cz.save()
                learned = set(taught)
                others = [(t.date, t.description, round(t.amount, 2), t.account_last4, t.seq)
                          for t in db.transactions()
                          if t.category != category and normalize(t.description)[0] in learned]
                also = db.conn.executemany(
                    "UPDATE transactions SET category = ? WHERE date = ? AND description = ? "
                    "AND ROUND(amount, 2) = ? AND account_last4 = ? AND seq = ?",
                    [(category, *k) for k in others]).rowcount
            db.conn.commit()
        return {"ok": True, "updated": updated, "alsoMoved": also,
                "remembered": sorted(set(taught))}

    def statement_sources(self, q) -> dict:
        """Which accounts have a statement waiting, where to get it, and the watcher."""
        with FinanceDB(self.db_path) as db:
            accounts = sources.due(db.statement_models())
        return {"accounts": accounts, "watch": self.watcher.status()}

    def set_watch(self, body: dict) -> dict:
        status = self.watcher.set(body.get("folder") or None)
        return {**status, "added": self.watcher.scan() if status["on"] else []}

    def feed(self, q) -> dict:
        from finances import simplefin
        return simplefin.status(self.db_path)

    def feed_action(self, body: dict) -> dict:
        from finances import simplefin
        action = body.get("action")
        try:
            if action == "connect":
                return simplefin.connect(self.db_path, str(body.get("token") or ""))
            if action == "sync":
                return simplefin.sync(self.db_path)
            if action == "disconnect":
                return simplefin.disconnect(self.db_path)
        except simplefin.FeedError as e:
            raise ValueError(str(e)) from None
        raise ValueError("action must be connect, sync or disconnect")

    def clients(self, q) -> dict:
        from finances import practice
        if not self.workspaces:
            return {"enabled": False, "clients": [], "current": None}
        return {"enabled": True, "clients": practice.clients(self.workspaces),
                "current": self.client}

    def networth(self, q) -> dict:
        from finances.networth import net_worth
        with FinanceDB(self.db_path) as db:
            return net_worth(db.statement_models())

    def read_due_dates(self, body: dict) -> dict:
        """Read the due date off each stored card statement that has none yet.

        Only the payment_due column is written, so nothing already in the store
        (transactions, categories, reconciliation) changes.
        """
        from finances import parse
        with FinanceDB(self.db_path) as db:
            checked = set(db.get_setting(DUE_CHECKED, []))
            todo = _due_unread(db.statement_models(), checked, self.pdf_dir)
        found, failed = {}, 0
        for s in todo:
            closing = date.fromisoformat(s.period_end) if s.period_end else None
            try:
                due = parse.read_payment_due(os.path.join(self.pdf_dir, s.source_file), closing)
            except Exception:                            # noqa: BLE001
                failed += 1
                continue
            if due:
                found[s.source_file] = due
            else:
                checked.add(s.source_file)
        with FinanceDB(self.db_path) as db:
            db.conn.executemany("UPDATE statements SET payment_due = ? WHERE source_file = ?",
                                [(d, f) for f, d in found.items()])
            db.set_setting(DUE_CHECKED, sorted(checked))
            db.conn.commit()
        return {"ok": True, "checked": len(todo), "found": len(found),
                "failed": failed, **self.payments({})}

    def ask(self, body: dict) -> dict:
        messages = body.get("messages") or []
        reply = chat.Chat(self.db_path).ask(body.get("provider", "anthropic"),
                                            body.get("model", ""), messages, body.get("key"),
                                            body.get("endpoint") or None)
        # The network call runs without the writer lock; saving takes it.
        with _parse_lock:
            chat.remember(self.db_path, str(messages[-1].get("content", "")), reply)
        return reply

    def upload(self, filename: str, blob: bytes) -> dict:
        """Save an uploaded PDF and parse it straight into the store."""
        from finances.importer import EXTENSIONS
        safe = re.sub(r"[^A-Za-z0-9 ._-]", "_", os.path.basename(filename)).strip()
        ext = os.path.splitext(safe)[1].lower()
        is_pdf = ext == ".pdf"
        if not is_pdf and ext not in EXTENSIONS:
            return {"ok": False, "error": "Upload a statement PDF, or a bank export "
                                          "(CSV, OFX, QFX, CAMT.053 XML, MT940)."}
        if is_pdf and not blob.startswith(b"%PDF"):
            return {"ok": False,
                    "error": f"{safe} is not a PDF — it does not start with %PDF."}

        os.makedirs(self.pdf_dir, exist_ok=True)
        dest = os.path.join(self.pdf_dir, safe)
        replaced = os.path.exists(dest)
        with open(dest, "wb") as fh:
            fh.write(blob)

        from finances import lineitems
        if not is_pdf and lineitems.kind_of_file(dest):
            # An order history or receipts: items under charges, not new rows.
            with _parse_lock:
                return {"ok": True, "file": safe, "replaced": replaced,
                        "lineItems": lineitems.import_file(self.db_path, dest)}

        # Absolute paths, because the parser runs in its own process: a relative
        # --db would otherwise resolve against wherever that process starts, and
        # the upload would land in a different store from the one being served.
        db_path = os.path.abspath(self.db_path)
        csv_out = os.path.join(os.path.dirname(db_path), "cleanedData", "transactions.csv")
        cmd = ([sys.executable, "-m", "finances.parse", dest,
                "--db", db_path, "-o", csv_out, "--force"] if is_pdf else
               [sys.executable, "-m", "finances.importer", dest, "--db", db_path, "--force"])
        # Run beside the store, so the categorizer reads the rules taught for
        # this store (a client's own, in a practice), with the code importable
        # from a checkout as well as an install.
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (ROOT, os.environ.get("PYTHONPATH", "")) if p)}
        with _parse_lock:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                  cwd=os.path.dirname(db_path))
        ok = proc.returncode == 0

        result: dict = {"ok": ok, "file": safe, "replaced": replaced,
                        "log": (proc.stdout or "") + (proc.stderr or "")}
        if not ok:
            # A file that will not parse should not sit in PDFs/ pretending it
            # was accepted; the next full run would silently pick it up again.
            if not replaced:
                try:
                    os.remove(dest)
                except OSError:
                    pass
            result["error"] = "Could not parse this statement."
            return result

        def mine(source):
            # A multi-account export is stored as "file.ofx#1234" per account.
            return source == safe or source.startswith(safe + "#")

        with self.engine() as eng:
            stmts = [s for s in eng.statements() if mine(s.source_file)]
            result["statement"] = _statement_dict(stmts[0]) if stmts else None
            result["accounts"] = len(stmts)
            result["coverage"] = _coverage_dict(eng.coverage())
            result["transactionCount"] = sum(
                1 for t in eng.transactions if mine(t.source_file))
        return result

    def delete_statement(self, source_file: str) -> dict:
        safe = os.path.basename(urllib.parse.unquote(source_file))
        with _parse_lock, FinanceDB(self.db_path) as db:
            removed = db.delete_source(safe)
            db.conn.execute("DELETE FROM statements WHERE source_file = ?", (safe,))
            db.conn.commit()
        path = os.path.join(self.pdf_dir, safe)
        if os.path.exists(path):
            os.remove(path)
        return {"ok": True, "file": safe, "transactionsRemoved": removed}


# --------------------------------------------------------------------------
# multipart/form-data
# --------------------------------------------------------------------------

def parse_multipart(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """Pull (filename, bytes) out of a multipart body.

    The stdlib ``cgi`` module did this and was removed in Python 3.13, so this
    is a small hand-rolled reader. It handles exactly what a browser file input
    sends and nothing more.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type, re.I)
    if not m:
        return []
    boundary = b"--" + m.group(1).strip().encode()
    out: list[tuple[str, bytes]] = []
    for part in body.split(boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not data:
            continue
        headers = head.decode("utf-8", "replace")
        fn = re.search(r'filename="([^"]*)"', headers)
        if not fn or not fn.group(1):
            continue
        out.append((fn.group(1), data.rstrip(b"\r\n")))
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    api: Api = None            # set in serve()
    server_version = "finances"
    read_only = False          # the phone view: see PhoneHandler

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            sys.stderr.write(f"  {self.command} {self.path} -> {args[1]}\n")

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, default=_json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # No CORS headers, deliberately. The page is served from this origin and the
    # Vite dev server proxies /api, so nothing legitimate is cross-origin — and
    # "Access-Control-Allow-Origin: *" would let any website open in the same
    # browser read every transaction from localhost.
    def _refuse_foreign(self) -> bool:
        """Answer 403 to a request that did not come from a local page."""
        raw = (self.headers.get("Host") or "").lower()
        host = raw[: raw.find("]") + 1] if raw.startswith("[") else raw.rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        origin_host = (urllib.parse.urlparse(origin).hostname or "") if origin else ""
        if host not in ALLOWED_HOSTS or (origin and origin_host not in ALLOWED_HOSTS):
            self._send_json({"error": "requests are accepted from localhost only"}, 403)
            return True
        return False

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        if self._refuse_foreign():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/api/session":
            return self._send_json({"readOnly": self.read_only})
        if path == "/api/phone":
            return self._send_json(phone_status())
        if path == "/api/export.xlsx":
            return self._send_workbook()
        if path == "/api/workpapers.xlsx":
            return self._send_workbook(workpapers=True)
        if path.startswith("/api/originals/"):
            return self._send_original(urllib.parse.unquote(path[len("/api/originals/"):]))
        if path.startswith("/api/"):
            name = path[5:].strip("/")
            fn = {"health": self.api.health, "summary": self.api.summary,
                  "statements": self.api.statements,
                  "transactions": self.api.transactions,
                  "categories": self.api.categories, "months": self.api.months,
                  "recurring": self.api.recurring, "merchants": self.api.merchants,
                  "anomalies": self.api.anomalies, "wrapped": self.api.wrapped,
                  "budgets": self.api.budgets, "goals": self.api.goals,
                  "subscriptions/review": self.api.subscription_review,
                  "payments": self.api.payments,
                  "networth": self.api.networth,
                  "sources": self.api.statement_sources,
                  "feed": self.api.feed,
                  "clients": self.api.clients,
                  "ask": lambda q: chat.status(),
                  "ask/history": lambda q: {"turns": chat.history(self.api.db_path)},
                  "ask/local": lambda q: chat.local_models(q.get("endpoint") or None),
                  }.get(name)
            if not fn:
                return self._send_json({"error": f"no endpoint {name}"}, 404)
            try:
                return self._send_json(fn(q))
            except SystemExit as e:
                return self._send_json({"error": str(e)}, 400)
            except chat.ChatError as e:
                return self._send_json({"error": str(e)}, e.status)
            except Exception as e:                       # noqa: BLE001
                import traceback
                traceback.print_exc()
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        return self._serve_static(path)

    def do_POST(self):
        if self._refuse_foreign():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            return self._upload()
        if path == "/api/phone":
            return self._phone()
        if path == "/api/clients":
            return self._switch_client()
        handler = {"/api/budgets": self.api.set_budget,
                   "/api/goals": self.api.save_goal,
                   "/api/subscriptions/mark": self.api.mark_subscription,
                   "/api/payments/due-dates": self.api.read_due_dates,
                   "/api/transactions/category": self.api.set_category,
                   "/api/watch": self.api.set_watch,
                   "/api/feed": self.api.feed_action,
                   "/api/ask": self.api.ask}.get(path)
        # A question carries the conversation so far, so it may be larger than a
        # settings write. It only reads, and waits on the network for a while,
        # so it must not hold the writer lock and stall an upload.
        asking = path == "/api/ask"
        if not handler:
            return self._send_json({"error": "not found"}, 404)
        # A JSON content type cannot be sent cross-site without a preflight,
        # which this server never answers, so a form on another site cannot
        # post here even if the Origin check were somehow passed.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send_json({"error": "send application/json"}, 415)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send_json({"error": "bad Content-Length"}, 400)
        # A question carries the conversation; a recategorization, its rows.
        limit = 512 if asking else 1024 if path == "/api/transactions/category" else 64
        if not 0 < length <= limit * 1024:
            return self._send_json({"error": "body missing or too large"}, 400)
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if asking:
                return self._send_json(handler(body))
            with _parse_lock:
                return self._send_json(handler(body))
        except chat.ChatError as e:
            return self._send_json({"error": str(e)}, e.status)
        except (ValueError, TypeError) as e:
            return self._send_json({"error": str(e)}, 400)
        except Exception as e:                           # noqa: BLE001
            return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _send_original(self, source_file: str):
        """The file a statement was read from, to look at in the browser.

        Only files the store has a statement for are served, so this cannot be
        used to read anything else in the statement folder, or outside it.
        """
        with FinanceDB(self.api.db_path) as db:
            known = {s.source_file.split("#", 1)[0] for s in db.statement_models()}
        name = os.path.basename(source_file.split("#", 1)[0])
        path = _original(name, self.api.pdf_dir) if name in known else None
        if path is None:
            return self._send_json({"error": f"{name} is not in the statement folder"}, 404)
        with open(path, "rb") as fh:
            blob = fh.read()
        ext = os.path.splitext(name)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type",
                         _ORIGINAL_TYPES.get(ext, "text/plain; charset=utf-8"))
        self.send_header("Content-Disposition",
                         f"inline; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ext not in _ORIGINAL_TYPES:
            self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _switch_client(self):
        """Serve another client's store (and make it, when asked)."""
        from finances import practice
        root = self.api.workspaces
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
            if not root:
                raise ValueError("start the dashboard with --clients FOLDER to keep clients")
            db, pdfs = practice.client_paths(root, str(body.get("name") or ""),
                                             create=bool(body.get("create")))
        except ValueError as e:
            return self._send_json({"error": str(e)}, 400)
        with _parse_lock:
            api = Api(db, pdfs)
            api.workspaces, api.client = root, practice.safe_name(body["name"])
            with FinanceDB(db):
                pass
            Handler.api = api
        return self._send_json(api.clients({}))

    def _phone(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 1024 else {}
            port = self.server.server_address[1]
            out = phone_start(port) if body.get("on") else phone_stop()
        except (ValueError, OSError) as e:
            return self._send_json({"error": str(e)}, 400)
        return self._send_json(out)

    def _send_workbook(self, workpapers: bool = False):
        """The whole store as one .xlsx, built fresh for each download."""
        import tempfile
        from finances import export, practice
        try:
            with FinanceDB(self.api.db_path) as db:
                txns = sorted(db.transactions(), key=lambda t: (t.date, t.source_file, t.seq))
                statements = db.statement_models()
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "finances.xlsx")
                path = (practice.workpapers(self.api.db_path, self.api.pdf_dir, out,
                                            self.api.client or "")
                        if workpapers else
                        export.workbook(txns, statements, self.api.db_path, out))
                with open(path, "rb") as fh:
                    blob = fh.read()
        except Exception as e:                           # noqa: BLE001
            return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{"workpapers" if workpapers else "finances"}'
                         f'-{date.today().isoformat()}.xlsx"')
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _upload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send_json({"error": "bad Content-Length"}, 400)
        if length <= 0:
            return self._send_json({"error": "empty upload"}, 400)
        if length > MAX_UPLOAD:
            return self._send_json(
                {"error": f"upload larger than {MAX_UPLOAD // 1024 // 1024}MB"}, 413)

        body = self.rfile.read(length)
        files = parse_multipart(body, self.headers.get("Content-Type", ""))
        if not files:
            return self._send_json({"error": "no file in the request"}, 400)

        results = [self.api.upload(name, blob) for name, blob in files]
        status = 200 if any(r.get("ok") for r in results) else 400
        return self._send_json({"results": results,
                                "uploaded": sum(1 for r in results if r.get("ok"))},
                               status)

    def do_DELETE(self):
        if self._refuse_foreign():
            return
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/statements/"):
            fn, arg = self.api.delete_statement, path[len("/api/statements/"):]
        elif path.startswith("/api/goals/"):
            fn, arg = self.api.delete_goal, path[len("/api/goals/"):]
        elif path == "/api/ask/history":
            fn, arg = (lambda _: (chat.forget(self.api.db_path), {"turns": []})[1]), None
        else:
            return self._send_json({"error": "not found"}, 404)
        try:
            with _parse_lock:
                return self._send_json(fn(arg))
        except Exception as e:                           # noqa: BLE001
            return self._send_json({"error": str(e)}, 500)

    # -- static ----------------------------------------------------------
    def _serve_static(self, path: str):
        """Serve the built frontend, falling back to index.html for routes."""
        if not os.path.isdir(WEB_DIST):
            return self._send_json({
                "error": "The web dashboard has not been built yet.",
                "hint": "Install Node and restart `finances web` (it builds itself), "
                        "run: cd web && npm ci && npm run build, or use Docker.",
            }, 503)

        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB_DIST, rel))
        if not full.startswith(WEB_DIST):                # traversal guard
            return self._send_json({"error": "forbidden"}, 403)
        if not os.path.isfile(full):
            full = os.path.join(WEB_DIST, "index.html")  # client-side routing
            if not os.path.isfile(full):
                return self._send_json({"error": "not found"}, 404)

        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if rel.startswith("assets/"):                    # hashed, safe to cache
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def ensure_built() -> bool:
    """Build the dashboard the first time, if it is missing and npm is here.

    Otherwise ``finances web`` would answer every page with "not built", and
    the person trying it has to discover a second toolchain to see anything.
    """
    if os.path.isdir(WEB_DIST):
        return True
    web = os.path.join(ROOT, "web")
    npm = shutil.which("npm")
    if not npm or not os.path.exists(os.path.join(web, "package.json")):
        return False
    print("finances: building the web dashboard (first run only) ...")
    steps = [] if os.path.isdir(os.path.join(web, "node_modules")) else [[npm, "ci"]]
    for cmd in steps + [[npm, "run", "build"]]:
        if subprocess.run(cmd, cwd=web).returncode != 0:
            print(f"finances: '{' '.join(cmd[1:])}' failed; serving the API only.",
                  file=sys.stderr)
            return False
    return os.path.isdir(WEB_DIST)


# --------------------------------------------------------------------------
# The phone view: read-only, on the home network, behind a one-time link
# --------------------------------------------------------------------------
#
# The dashboard listens on localhost only. To look at it from a phone, a second
# listener opens on this computer's address on the home network, serving the
# same pages read-only. It answers only a browser holding the key from the link
# (kept as a cookie after the first visit), only GET requests, and never the
# Ask page. The key is made fresh each time and lives in memory, so turning it
# off, or stopping the dashboard, ends every phone's access.

PHONE_BLOCKED = ("/api/ask", "/api/phone", "/api/feed")
_phone: dict = {"httpd": None, "url": "", "token": ""}


class PhoneHandler(Handler):
    read_only = True
    token = ""
    host = ""                  # "192.168.1.20:8787", the only Host it answers to

    def _refuse_foreign(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        if host != self.host or (origin and urllib.parse.urlparse(origin).netloc != self.host):
            self._send_json({"error": "not here"}, 403)
            return True
        parsed = urllib.parse.urlparse(self.path)
        key = urllib.parse.parse_qs(parsed.query).get("key", [""])[0]
        if key and secrets.compare_digest(key, self.token):
            # Trade the key in the link for a cookie, and drop it from the address bar.
            self.send_response(303)
            self.send_header("Set-Cookie", f"finances_phone={self.token}; Path=/; HttpOnly; "
                                           "SameSite=Strict; Max-Age=86400")
            self.send_header("Location", parsed.path or "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        cookies = dict(c.strip().split("=", 1) for c in
                       (self.headers.get("Cookie") or "").split(";") if "=" in c)
        if not secrets.compare_digest(cookies.get("finances_phone", ""), self.token):
            body = (b"<!doctype html><meta name=viewport content='width=device-width'>"
                    b"<p style='font:16px system-ui;margin:2em'>Open the link or scan the "
                    b"code shown on your computer's dashboard.</p>")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if parsed.path.startswith(PHONE_BLOCKED):
            self._send_json({"error": "not available on this device"}, 403)
            return True
        return False

    def do_POST(self):
        if not self._refuse_foreign():
            self._send_json({"error": "read-only on this device"}, 403)

    def do_DELETE(self):
        if not self._refuse_foreign():
            self._send_json({"error": "read-only on this device"}, 403)


def lan_address() -> str:
    """This computer's address on the home network, or "" if it has none."""
    import ipaddress
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))          # no packet is sent for UDP connect
            ip = sk.getsockname()[0]
    except OSError:
        return ""
    addr = ipaddress.ip_address(ip)
    return ip if addr.is_private and not addr.is_loopback else ""


def phone_status() -> dict:
    return {"on": _phone["httpd"] is not None, "url": _phone["url"]}


def phone_start(port: int) -> dict:
    if _phone["httpd"] is not None:
        return phone_status()
    ip = lan_address()
    if not ip:
        raise ValueError("this computer is not on a home network (no private address)")
    token = secrets.token_urlsafe(18)
    last_error = None
    for p in (port, *range(port + 1, port + 6)):
        cls = type("Phone", (PhoneHandler,), {"token": token, "host": f"{ip}:{p}"})
        try:
            httpd = ThreadingHTTPServer((ip, p), cls)
        except OSError as e:
            last_error = e
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True, name="phone").start()
        _phone.update(httpd=httpd, token=token, url=f"http://{ip}:{p}/?key={token}")
        return phone_status()
    raise OSError(f"could not open a port for the phone view: {last_error}")


def phone_stop() -> dict:
    httpd = _phone["httpd"]
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
    _phone.update(httpd=None, url="", token="")
    return phone_status()


FEED_EVERY = 6 * 3600


def _keep_feed_fresh(db: str):
    """Sync a connected SimpleFIN feed every few hours while the dashboard runs."""
    import time
    from finances import simplefin
    while True:
        try:
            if os.path.exists(db):
                st = simplefin.status(db)
                if st["connected"] and time.time() - (st["synced"] or 0) > FEED_EVERY:
                    with _parse_lock:
                        simplefin.sync(db)
        except Exception as e:                           # noqa: BLE001
            sys.stderr.write(f"  feed sync failed: {e}\n")
        time.sleep(600)


def serve(db: str, port: int, open_browser: bool, host: str = "127.0.0.1",
          clients: str | None = None):
    Handler.api = Api(db)
    if clients:
        from finances import practice
        os.makedirs(clients, exist_ok=True)
        existing = practice.clients(clients)
        if existing:
            # Open the first client rather than a store outside the practice.
            cdb, pdfs = practice.client_paths(clients, existing[0]["name"])
            Handler.api = Api(cdb, pdfs)
            Handler.api.client = existing[0]["name"]
            db = cdb
        Handler.api.workspaces = os.path.abspath(clients)
    httpd = ThreadingHTTPServer((host, port), Handler)
    if os.path.exists(db) and Handler.api.watcher.status()["on"]:
        Handler.api.watcher.start()
    threading.Thread(target=_keep_feed_fresh, args=(db,), daemon=True).start()
    url = f"http://localhost:{port}"

    built = os.path.isdir(WEB_DIST)
    print(f"finances api  {url}")
    print(f"  store       {db}"
          f"{'' if os.path.exists(db) else '  (missing — run: finances parse PDFs/, or finances demo)'}")
    print(f"  dashboard   {'serving ' + WEB_DIST if built else 'not built — run: cd web && npm ci && npm run build'}")
    if host in ("127.0.0.1", "localhost", "::1"):
        print("  bound to localhost only. Ctrl-C to stop.\n")
    else:
        # Only sensible inside a container, where the port is published to the
        # host's loopback. Requests still have to name localhost to be served.
        print(f"  bound to {host}; requests are still accepted from localhost only.\n")

    if open_browser and built:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    ap = argparse.ArgumentParser(description="JSON API over the finances store.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--port", type=int, default=int(os.environ.get("FINANCES_PORT", 8787)))
    ap.add_argument("--host", default=os.environ.get("FINANCES_HOST", "127.0.0.1"),
                    help="interface to bind (default 127.0.0.1; 0.0.0.0 only inside a container)")
    ap.add_argument("--open", action="store_true", help="open a browser once serving")
    ap.add_argument("--clients", default=os.environ.get("FINANCES_CLIENTS"),
                    help="a folder of client folders (finances client); switch between "
                         "them in the dashboard")
    ap.add_argument("--no-build", action="store_true",
                    help="do not build the dashboard if web/dist is missing")
    args = ap.parse_args()
    if not args.no_build:
        ensure_built()
    serve(args.db, args.port, args.open, args.host, args.clients)


if __name__ == "__main__":
    main()
