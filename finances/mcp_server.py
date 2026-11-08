#!/usr/bin/env python3
"""
mcp_server.py — the store, as tools an AI assistant can call.

MCP (Model Context Protocol) is how Claude Desktop, Claude Code, Cursor and
other assistants call tools on your computer. ``finances mcp`` is one of those
tools: the assistant starts it, asks it questions ("where did my money go in
March?"), and gets real numbers back from the reconciled store — not guesses.

Nothing about getting data in changes. Statements and bank exports still come
from you; this only reads what ``parse`` and ``import`` already put in the
store, and never changes your data.

Where the answers go is up to the assistant you connect. A cloud model (Claude,
ChatGPT) receives the result of each tool call — totals, merchant names,
amounts — to write its reply. A local model keeps everything on the machine.

Setup (use an absolute path: assistants start servers from anywhere):

    Claude Desktop, in claude_desktop_config.json:
        {"mcpServers": {"finances": {
            "command": "uvx",
            "args": ["finances", "mcp", "--db", "/Users/you/finances/finances.db"]}}}

    Claude Code:
        claude mcp add finances -- uvx finances mcp --db /Users/you/finances/finances.db

The protocol is newline-delimited JSON-RPC over stdin and stdout, written here
with the standard library so the project keeps its single dependency.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime

from finances.database import DEFAULT_DB

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Read-only access to the user's personal finance store, built from their own "
    "bank statements and exports. Spending is positive, money in is negative; "
    "every amount is in the store's base currency (see `currency` in results). "
    "Call `check` before stating totals as fact: if statements do not reconcile "
    "or months are missing, say so. Card payments and transfers between the "
    "user's own accounts are not spending, and the spending tools already leave "
    "them out.")


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("finances")
    except Exception:                                     # noqa: BLE001
        return "dev"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso(v: str | None, name: str) -> str | None:
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-03-31, not {v!r}")


def _window(args: dict) -> tuple[str | None, str | None]:
    """start/end from explicit dates or a YYYY-MM month."""
    start, end = _iso(args.get("start"), "start"), _iso(args.get("end"), "end")
    month = args.get("month")
    if month:
        m = str(month)[:7]
        try:
            first = datetime.strptime(m, "%Y-%m").date()
        except ValueError:
            raise ValueError(f"month must look like 2026-03, not {month!r}")
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        start, end = first.isoformat(), date.fromordinal(nxt.toordinal() - 1).isoformat()
    return start, end


def _in(t, start, end) -> bool:
    return (not start or t.date >= start) and (not end or t.date <= end)


def _txn(t) -> dict:
    return {"date": t.date, "merchant": t.merchant, "description": t.description,
            "amount": round(t.amount, 2), "category": t.category,
            "account": f"{t.issuer} {t.account_last4}".strip(),
            "source_file": t.source_file, **({"page": t.page} if t.page else {})}


def _recurring(r) -> dict:
    return {"name": r.name, "category": r.category, "cadence": r.cadence,
            "amount": round(r.amount, 2), "per_month": round(r.per_month, 2),
            "per_year": round(r.per_year, 2), "charges_seen": r.n,
            "first_seen": r.first_seen.isoformat(), "last_seen": r.last_seen.isoformat(),
            "next_expected": r.next_expected.isoformat() if r.next_expected else None,
            "price_change_per_charge": round(r.price_change, 2),
            "price_change_pct": round(r.price_pct, 1)}


class Tools:
    """One method per tool. Each opens the store fresh, so a parse or import
    made while the assistant is running shows up on the next question."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _engine(self):
        from finances.engine import Engine
        if not os.path.exists(self.db_path):
            raise ValueError(
                f"no store at {self.db_path}. Build one with `finances parse <PDFs>` "
                f"or `finances import <exports>`, or point --db at it (absolute path).")
        return Engine(self.db_path)

    @staticmethod
    def _wrap(eng, payload: dict) -> dict:
        payload["currency"] = eng.currency
        if eng.fx_missing:
            payload["not_counted"] = {k: f"{v} transactions with no exchange rate set"
                                      for k, v in eng.fx_missing.items()}
        return payload

    # -- the tools -------------------------------------------------------
    def summary(self, a):
        with self._engine() as eng:
            rng = eng.dates()
            cov = eng.coverage()
            income = eng.income_average()
            return self._wrap(eng, {
                "first_date": rng[0].isoformat() if rng else None,
                "last_date": rng[1].isoformat() if rng else None,
                "transactions": len(eng.transactions),
                "total_spend": round(eng.total_spend(), 2),
                "monthly_average_spend": round(eng.monthly_average(), 2),
                "essential_monthly": round(eng.essential_monthly(), 2),
                "committed_monthly_recurring": round(eng.committed_monthly(), 2),
                "income_monthly_average": round(income, 2) if income is not None else None,
                "monthly_surplus": round(eng.surplus(), 2) if eng.surplus() is not None else None,
                "complete_months": eng.complete_months(),
                "accounts": sorted({f"{t.issuer} {t.account_last4}".strip()
                                    for t in eng.transactions if t.issuer}),
                "statements_reconciled": f"{cov['reconciled']}/{cov['statements']}",
                "trusted": cov["trusted"],
            })

    def spending_by_category(self, a):
        start, end = _window(a)
        with self._engine() as eng:
            rows = [t for t in eng.spend() if _in(t, start, end)]
            by, n = defaultdict(float), defaultdict(int)
            for t in rows:
                by[t.category] += t.amount
                n[t.category] += 1
            total = sum(by.values())
            return self._wrap(eng, {
                "start": start, "end": end, "total": round(total, 2),
                "categories": [{"category": c, "total": round(v, 2), "transactions": n[c],
                                "share_pct": round(v / total * 100, 1) if total else 0.0}
                               for c, v in sorted(by.items(), key=lambda kv: -kv[1])]})

    def monthly_totals(self, a):
        start, end = _window(a)
        with self._engine() as eng:
            spend, income = eng.by_month(), eng.income_by_month()
            complete = set(eng.complete_months())
            months = [m for m in sorted(set(spend) | set(income))
                      if (not start or m >= start[:7]) and (not end or m <= end[:7])]
            return self._wrap(eng, {"months": [
                {"month": m, "spend": round(spend.get(m, 0.0), 2),
                 "income": round(income.get(m, 0.0), 2),
                 "net": round(income.get(m, 0.0) - spend.get(m, 0.0), 2),
                 "complete": m in complete} for m in months]})

    def search_transactions(self, a):
        start, end = _window(a)
        q = (a.get("query") or "").lower().strip()
        cat = a.get("category")
        direction = a.get("direction")
        lo, hi = a.get("min_amount"), a.get("max_amount")
        limit = max(1, min(int(a.get("limit") or 50), 500))
        with self._engine() as eng:
            rows = [t for t in eng.transactions if _in(t, start, end)
                    and (not q or q in t.description.lower() or q in t.merchant.lower())
                    and (not cat or t.category.lower() == str(cat).lower())
                    and (not direction or t.direction == direction)
                    and (lo is None or abs(t.amount) >= float(lo))
                    and (hi is None or abs(t.amount) <= float(hi))]
            rows.sort(key=lambda t: t.date, reverse=True)
            return self._wrap(eng, {
                "matches": len(rows), "sum": round(sum(t.amount for t in rows), 2),
                "shown": min(len(rows), limit),
                "transactions": [_txn(t) for t in rows[:limit]]})

    def subscriptions(self, a):
        with self._engine() as eng:
            rows = eng.recurring() if a.get("include_bills", True) else eng.subscriptions()
            return self._wrap(eng, {
                "committed_monthly": round(eng.committed_monthly(), 2),
                "recurring": [_recurring(r) for r in rows],
                "price_changes": [_recurring(r) for r in rows if abs(r.price_pct) >= 1]})

    def cancel_list(self, a):
        from finances.budgets import review
        with self._engine() as eng:
            rv = review(eng)
            item = lambda i: {**_recurring(i["r"]), "reasons": i["reasons"]}  # noqa: E731
            return self._wrap(eng, {
                "savings_per_year_if_all_cancelled": rv["savingsPerYear"],
                "candidates": [item(i) for i in rv["candidates"]],
                "still_charging_after_cancel": [item(i) for i in rv["stillCharging"]],
                "kept": [i["r"].name for i in rv["kept"]],
                "cancelled": [i["r"].name for i in rv["cancelled"]],
                "stopped_on_their_own": [i["r"].name for i in rv["gone"]]})

    def cashflow_forecast(self, a):
        from finances import cashflow as cf
        days = max(7, min(int(a.get("days") or 60), 365))
        floor = float(a.get("min_balance") or 0.0)
        anchor = date.today()
        with self._engine() as eng:
            bal, notes = cf.find_balance(eng, anchor, a.get("starting_balance"))
            if bal is None:
                return self._wrap(eng, {
                    "error": "no checking balance found to start from",
                    "notes": notes,
                    "hint": "ask the user for their current checking balance and pass "
                            "it as starting_balance"})
            recurring = eng.recurring()
            proj = cf.project(bal, anchor, days, recurring, cf.detect_income(eng),
                              cf.build_variable(eng, recurring))
            low = min(proj, key=lambda d: d.balance)
            upcoming = [{"date": d.day.isoformat(), "name": e.name,
                         "amount": round(e.amount, 2), "kind": e.kind}
                        for d in proj for e in d.named][:25]
            below = [d for d in proj if d.balance < floor]
            return self._wrap(eng, {
                "start": {"balance": round(bal.amount, 2), "as_of": bal.as_of.isoformat(),
                          "source": bal.source},
                "days": days,
                "lowest": {"date": low.day.isoformat(), "balance": round(low.balance, 2),
                           "range": [round(low.lo, 2), round(low.hi, 2)]},
                "end": {"date": proj[-1].day.isoformat(), "balance": round(proj[-1].balance, 2)},
                "days_below_min_balance": len(below),
                "first_day_below_min_balance": below[0].day.isoformat() if below else None,
                "min_balance": floor,
                "upcoming": upcoming,
                "notes": notes,
                "method": "known recurring charges and paychecks on their dates, plus "
                          "average everyday spending; the range is a band, not a "
                          "confidence interval"})

    def check(self, a):
        with self._engine() as eng:
            cov = eng.coverage()
            return self._wrap(eng, {
                "statements": cov["statements"], "reconciled": cov["reconciled"],
                "mismatched": cov["mismatched"], "unchecked": cov["unchecked"],
                "trusted": cov["trusted"],
                "mismatches": [{"file": s.source_file, "off_by": s.reconcile_diff,
                                "note": s.reconcile_note} for s in cov["mismatches"][:20]],
                "missing_periods": cov["gaps"][:20],
                "meaning": "trusted means every statement's extracted transactions match "
                           "its own printed totals or balances and no months are missing"})

    def anomalies(self, a):
        from finances.analyze import find_anomalies
        from finances.utils import merchant_key
        with self._engine() as eng:
            spend = eng.spend()
            groups = defaultdict(list)
            for t in spend:
                groups[merchant_key(t.merchant)].append(t)
            found = find_anomalies(spend, groups)
            iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else d  # noqa: E731
            return self._wrap(eng, {
                "possible_duplicates": [{"merchant": d["name"], "amount": round(d["amount"], 2),
                                         "date": iso(d["date"]), "times": d["n"]}
                                        for d in found["dupes"]],
                "unusually_large": [{"merchant": o["name"], "date": iso(o["date"]),
                                     "amount": round(o["amount"], 2),
                                     "typical": round(o["typical"], 2)}
                                    for o in found["outliers"]],
                "largest": [_txn(t) for t in found["big"]]})

    def budgets(self, a):
        from finances.budgets import status
        with self._engine() as eng:
            return self._wrap(eng, status(eng))

    def wrapped(self, a):
        from finances.wrapped import wrapped
        with self._engine() as eng:
            return self._wrap(eng, wrapped(eng, int(a["year"]) if a.get("year") else None))

    def query(self, a):
        from finances.ask import Rejected, open_readonly, run_query, validate_select
        if not os.path.exists(self.db_path):
            raise ValueError(f"no store at {self.db_path}")
        try:
            sql = validate_select(str(a.get("sql") or ""), limit=200)
        except Rejected as e:
            raise ValueError(f"rejected: {e}")
        conn = open_readonly(self.db_path)
        try:
            cols, rows = run_query(conn, sql)
        finally:
            conn.close()
        return {"sql": sql, "columns": cols, "rows": [list(r) for r in rows],
                "note": "raw store values: amounts in each row's own currency "
                        "(blank = base), card payments and transfers included"}


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

_DATES = {
    "start": {"type": "string", "description": "first day, YYYY-MM-DD"},
    "end": {"type": "string", "description": "last day, YYYY-MM-DD"},
    "month": {"type": "string", "description": "a whole month, YYYY-MM (instead of start/end)"},
}


def _schema(props=None, required=()):
    return {"type": "object", "properties": props or {}, "required": list(required),
            "additionalProperties": False}


TOOLS = [
    ("summary", "Headline numbers: date range, total and average monthly spending, "
     "income, surplus, accounts, and whether the statements reconcile.", _schema()),
    ("spending_by_category", "Spending per category over a period, largest first. "
     "Excludes card payments, transfers and income.", _schema(_DATES)),
    ("monthly_totals", "Spending, income and net per month, with whether each month "
     "is fully covered by statements.", _schema(_DATES)),
    ("search_transactions", "Find transactions by merchant or description text, "
     "category, date range, amount or direction. Newest first.", _schema({
         **_DATES,
         "query": {"type": "string", "description": "text in the merchant or description"},
         "category": {"type": "string"},
         "direction": {"type": "string", "enum": ["out", "in"]},
         "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}})),
    ("subscriptions", "Every recurring charge found in the transactions, with cadence, "
     "yearly cost, next expected date and price changes.", _schema({
         "include_bills": {"type": "boolean", "default": True,
                           "description": "false to leave out interest"}})),
    ("cancel_list", "Recurring charges worth reconsidering, ranked by what dropping "
     "them saves, each with reasons (price rises, overlapping services). Rent, "
     "insurance and utilities are left off.", _schema()),
    ("cashflow_forecast", "Project the checking balance forward day by day from known "
     "recurring charges, paychecks and average spending. Finds the low point.", _schema({
         "days": {"type": "integer", "minimum": 7, "maximum": 365, "default": 60},
         "starting_balance": {"type": "number",
                              "description": "current checking balance, if the user gave one"},
         "min_balance": {"type": "number", "default": 0,
                         "description": "report days the balance falls below this"}})),
    ("check", "Whether the numbers can be trusted: statements whose extracted "
     "transactions do not match their printed totals, and missing months.", _schema()),
    ("anomalies", "Possible duplicate charges, unusually large charges for a merchant, "
     "and the largest purchases. Useful before disputing a charge.", _schema()),
    ("budgets", "This month's spending against each category's monthly limit, "
     "projected to month end, plus suggested limits.", _schema()),
    ("wrapped", "A year in review: total, top categories and places, biggest purchase, "
     "day and month, subscriptions paid, comparison with the year before.", _schema({
         "year": {"type": "integer"}})),
    ("query", "Run one read-only SQL SELECT on the store. Tables: transactions(date, "
     "description, amount, merchant, category, direction, post_date, issuer, "
     "account_last4, statement_period, source_file, seq, currency) and "
     "statements(source_file, issuer, account_last4, statement_period, period_start, "
     "period_end, previous_balance, new_balance, purchases, payments, interest_charged, "
     "fees_charged, credit_limit, purchase_apr, reconciled, reconcile_note, currency, "
     "account_type, source_format). amount: spending positive, money in negative. "
     "Prefer the other tools; use this for questions they cannot answer.",
     _schema({"sql": {"type": "string"}}, required=("sql",))),
]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d, "inputSchema": s,
             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
            for n, d, s in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


class Server:
    def __init__(self, db_path: str):
        self.tools = Tools(db_path)
        self.names = {n for n, _, _ in TOOLS}

    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:                       # a notification: nothing to answer
            return None
        try:
            result = self.dispatch(method, msg.get("params") or {})
        except LookupError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": str(e)}}
        except Exception as e:                # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def dispatch(self, method: str, params: dict):
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "finances", "version": _version()},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_list()}
        if method == "tools/call":
            return self.call(params.get("name"), params.get("arguments") or {})
        raise LookupError(f"method not found: {method}")

    def call(self, name: str, args: dict) -> dict:
        if name not in self.names:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name!r}"}]}
        try:
            # Tool code prints progress in places; stdout belongs to the protocol.
            with contextlib.redirect_stdout(sys.stderr):
                out = getattr(self.tools, name)(args)
        except (ValueError, TypeError) as e:
            return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
        except Exception as e:                # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"isError": True,
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}
        text = json.dumps(out, default=_default, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}],
                "structuredContent": json.loads(text)}

    def serve(self, stdin=None, stdout=None):
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}}
            else:
                batch = msg if isinstance(msg, list) else [msg]
                replies = [r for r in (self.handle(m) for m in batch if isinstance(m, dict)) if r]
                if not replies:
                    continue
                reply = replies if isinstance(msg, list) else replies[0]
            stdout.write(json.dumps(reply, default=_default, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="finances mcp",
        description="Serve the store to AI assistants over MCP (stdio). Read-only.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="the store; use an absolute path, since assistants start "
                         "servers from their own working directory")
    args = ap.parse_args()
    db = os.path.abspath(os.path.expanduser(args.db))
    print(f"finances mcp: serving {db} (read-only) on stdio", file=sys.stderr)
    if not os.path.exists(db):
        print(f"finances mcp: warning: {db} does not exist yet", file=sys.stderr)
    try:
        Server(db).serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
