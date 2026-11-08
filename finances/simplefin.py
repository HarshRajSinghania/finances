"""
simplefin.py — an optional live feed from your bank, checked by its statements.

SimpleFIN Bridge (https://beta-bridge.simplefin.org) links your banks for
$1.50 a month, paid to them, and hands out a setup token. Paste the token here
once: it is exchanged for an access URL, kept in this store and nowhere else,
and used to fetch recent transactions and balances. No bank password ever
reaches this app.

What the feed adds is freshness: this month's charges before the statement
exists. What it does not get is trust on its own word. When the statement for
those dates arrives, its rows (checked against its printed totals) become the
record, and the feed's rows for the same days are compared with them, so every
month the feed is either confirmed by the bank's own statement or shown not to
be. Until then, feed rows are marked as unconfirmed.

Protocol: https://www.simplefin.org/protocol.html. Standard library only.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from finances.database import FEED_PREFIX, FinanceDB
from finances.models import Statement, Transaction

KEY = "simplefin"          # settings: {"access", "claimed", "synced", "errors"}
FIRST_DAYS = 60            # how far back the first sync reaches
OVERLAP_DAYS = 7           # later syncs re-read this much, for late-posting rows
TIMEOUT = 60.0


class FeedError(Exception):
    pass


def _open(url: str, method: str = "GET", data: bytes | None = None) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise FeedError("SimpleFIN addresses must use https")
    headers = {"User-Agent": "finances", "Accept": "application/json"}
    if parts.username:
        cred = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        url = urllib.parse.urlunsplit((parts.scheme, parts.hostname + (f":{parts.port}" if parts.port else ""),
                                       parts.path, parts.query, ""))
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise FeedError("SimpleFIN refused the request: the token was already used, "
                            "or access was revoked. Make a new setup token.") from None
        raise FeedError(f"SimpleFIN answered {e.code}") from None
    except urllib.error.URLError as e:
        raise FeedError(f"could not reach SimpleFIN: {e.reason}") from None


def claim(setup_token: str, opener=_open) -> str:
    """Exchange a one-time setup token for the access URL that reads the feed."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except ValueError:
        raise FeedError("that is not a SimpleFIN setup token") from None
    if not claim_url.startswith("https://"):
        raise FeedError("that is not a SimpleFIN setup token")
    access = opener(claim_url, "POST", b"").decode().strip()
    if not access.startswith("https://"):
        raise FeedError("SimpleFIN did not return an access address")
    return access


def connect(db_path: str, setup_token: str, opener=_open) -> dict:
    access = claim(setup_token, opener)
    with FinanceDB(db_path) as db:
        db.set_setting(KEY, {"access": access, "claimed": int(time.time()),
                             "synced": None, "errors": []})
    return sync(db_path, opener)


def disconnect(db_path: str) -> dict:
    """Forget the access URL. Rows already fetched stay, as any import does."""
    with FinanceDB(db_path) as db:
        db.set_setting(KEY, None)
    return status(db_path)


def _issuer(org: dict) -> str:
    from finances.sources import guide
    name = (org or {}).get("name") or (org or {}).get("domain") or "Bank"
    g = guide(name)
    return g["bank"] if g else name


def _last4(name: str) -> str:
    m = re.search(r"(\d{4})\)?\s*$", name or "")
    return m.group(1) if m else ""


def sync(db_path: str, opener=_open, today: date | None = None) -> dict:
    """Fetch recent transactions and balances into the store."""
    with FinanceDB(db_path) as db:
        st = db.get_setting(KEY) or {}
    if not st.get("access"):
        raise FeedError("connect SimpleFIN first")
    today = today or date.today()
    since = (date.fromtimestamp(st["synced"]) - timedelta(days=OVERLAP_DAYS)
             if st.get("synced") else today - timedelta(days=FIRST_DAYS))
    start = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
    body = json.loads(opener(f"{st['access'].rstrip('/')}/accounts?start-date={start}"))

    txns, stmts = [], []
    for acct in body.get("accounts") or []:
        issuer, last4 = _issuer(acct.get("org")), _last4(acct.get("name", ""))
        source = f"{FEED_PREFIX}{acct.get('id')}"
        balance = float(acct.get("balance") or 0)
        card = balance < 0 or bool(re.search(r"card|credit", acct.get("name", ""), re.I))
        as_of = date.fromtimestamp(int(acct.get("balance-date") or time.time()))
        stmts.append(Statement(
            source_file=source, issuer=issuer, account_last4=last4,
            statement_period=f"live feed, {acct.get('name', '')}".strip(),
            period_end=as_of.isoformat(),
            # SimpleFIN signs a card balance as money owed (negative); statements
            # print what is owed as a positive new balance.
            new_balance=-balance if card else balance,
            account_type="card" if card else "bank", source_format="simplefin",
            currency=(acct.get("currency") or "").upper() if len(acct.get("currency") or "") == 3 else "",
            reconcile_note="Live bank feed: confirmed when the statement for these dates arrives."))
        seen: dict[tuple, int] = {}
        for t in acct.get("transactions") or []:
            if t.get("pending"):
                continue
            d = date.fromtimestamp(int(t.get("transacted_at") or t.get("posted") or 0))
            desc = (t.get("description") or t.get("payee") or "").strip()
            amount = round(-float(t.get("amount") or 0), 2)     # deposits are positive there
            k = (d, desc, amount)
            seen[k] = seen.get(k, -1) + 1
            txns.append(Transaction(date=d.isoformat(), description=desc, amount=amount,
                                    issuer=issuer, account_last4=last4,
                                    statement_period="live feed", source_file=source,
                                    seq=seen[k], line=str(t.get("id") or "")))

    if txns:
        from finances.categorizer import categorize_rows, Categorizer
        for t, r in zip(txns, categorize_rows(txns, Categorizer())):
            t.category = r.category
            if r.clean:
                t.merchant = r.clean
    with FinanceDB(db_path) as db:
        db.upsert_transactions(txns)
        db.upsert_statements(stmts)
        st.update(synced=int(time.time()), errors=[str(e) for e in body.get("errors") or []])
        db.set_setting(KEY, st)
    return status(db_path)


def agreement(db_path: str) -> list[dict]:
    """For each statement that covers feed rows: did the bank feed agree with it?"""
    with FinanceDB(db_path) as db:
        rows = db.transactions(feed=True)
        stmts = [s for s in db.statement_models()
                 if not s.source_file.startswith(FEED_PREFIX)
                 and s.period_start and s.period_end and s.account_last4]
    feed = [t for t in rows if t.source_file.startswith(FEED_PREFIX)]
    out = []
    for s in stmts:
        mine = [t for t in feed if t.account_last4 == s.account_last4
                and s.period_start <= t.date <= s.period_end]
        if not mine:
            continue
        theirs = [t for t in rows if t.source_file == s.source_file]
        pool = [(t.d, round(t.amount, 2)) for t in theirs]
        matched = 0
        for t in mine:
            hit = next((p for p in pool if p[1] == round(t.amount, 2) and p[0] and t.d
                        and abs((p[0] - t.d).days) <= 4), None)
            if hit:
                pool.remove(hit)
                matched += 1
        out.append({"statement": s.source_file, "issuer": s.issuer, "last4": s.account_last4,
                    "periodEnd": s.period_end, "feedRows": len(mine), "matched": matched,
                    "statementRows": len(theirs), "reconciled": s.reconciled})
    return out


def status(db_path: str) -> dict:
    with FinanceDB(db_path) as db:
        st = db.get_setting(KEY) or {}
        accounts = [{"issuer": s.issuer, "last4": s.account_last4, "balance": s.new_balance,
                     "kind": s.account_type, "asOf": s.period_end}
                    for s in db.statement_models() if s.source_file.startswith(FEED_PREFIX)]
    # Never the access URL itself: it is a password to the feed.
    return {"connected": bool(st.get("access")), "synced": st.get("synced"),
            "errors": st.get("errors") or [], "accounts": accounts,
            "checks": agreement(db_path) if st.get("access") or accounts else []}


def main() -> int:
    """finances feed connect TOKEN | sync | status | disconnect"""
    import argparse
    from finances.database import DEFAULT_DB
    ap = argparse.ArgumentParser(prog="finances feed", description=__doc__.split("\n\n")[1])
    ap.add_argument("action", choices=["connect", "sync", "status", "disconnect"])
    ap.add_argument("token", nargs="?")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    try:
        if args.action == "connect":
            if not args.token:
                ap.error("paste the setup token from SimpleFIN Bridge")
            out = connect(args.db, args.token)
        elif args.action == "sync":
            out = sync(args.db)
        elif args.action == "disconnect":
            out = disconnect(args.db)
        else:
            out = status(args.db)
    except FeedError as e:
        print(f"error: {e}")
        return 1
    print(f"{'connected' if out['connected'] else 'not connected'}"
          + (f", last synced {datetime.fromtimestamp(out['synced']):%Y-%m-%d %H:%M}"
             if out.get("synced") else ""))
    for a in out["accounts"]:
        print(f"  {a['issuer']} {a['last4']:>4}  balance {a['balance']:,.2f} as of {a['asOf']}")
    for c in out["checks"]:
        print(f"  {c['statement']}: {c['matched']} of {c['feedRows']} feed rows "
              f"match the statement")
    for e in out["errors"]:
        print(f"  SimpleFIN says: {e}")
    return 0
