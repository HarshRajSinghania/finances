#!/usr/bin/env python3
"""
api.py — a small JSON API over the finances store, for the web dashboard.

    python api.py                      # serve on http://localhost:8787
    python api.py --port 9000 --db finances.db
    python api.py --open               # and open a browser

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
    POST /api/upload         multipart PDF upload, parsed straight into the store
    DELETE /api/statements/<source_file>

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
import threading
import urllib.parse
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from database import DEFAULT_DB, FinanceDB          # noqa: E402
from engine import Engine, NON_SPEND                # noqa: E402
from utils import merchant_key                      # noqa: E402

# Both overridable so a container (or anyone running from another directory)
# can keep the code in one place and the data in another.
PDF_DIR = os.environ.get("FINANCES_PDF_DIR") or os.path.join(HERE, "PDFs")
WEB_DIST = os.environ.get("FINANCES_WEB_DIST") or os.path.join(HERE, "web", "dist")
MAX_UPLOAD = 32 * 1024 * 1024          # a statement PDF is tens of KB; 32MB is generous

# Names a browser may use to reach this server. Anything else in the Host header
# is a DNS-rebinding attempt: a hostile page pointing its own domain at 127.0.0.1
# to read the API as if it were same-origin.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

# One writer at a time. Parsing mutates the store, and two uploads landing
# together would race on the same SQLite file.
_parse_lock = threading.Lock()


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


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

    def __init__(self, db_path: str):
        self.db_path = db_path

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
            }

    def statements(self, q) -> dict:
        with self.engine() as eng:
            rows = [_statement_dict(s) for s in eng.statements()]
            # Transaction counts per file, so the UI can show what each yielded.
            counts: dict[str, int] = {}
            for t in eng.transactions:
                counts[t.source_file] = counts.get(t.source_file, 0) + 1
            for r in rows:
                r["transactionCount"] = counts.get(r["sourceFile"], 0)
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
            return {
                "transactions": [_txn_dict(t) for t in page],
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

        from analyze import find_anomalies
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

    # -- writes ----------------------------------------------------------
    def upload(self, filename: str, blob: bytes) -> dict:
        """Save an uploaded PDF and parse it straight into the store."""
        safe = re.sub(r"[^A-Za-z0-9 ._-]", "_", os.path.basename(filename)).strip()
        if not safe.lower().endswith(".pdf"):
            return {"ok": False, "error": "Only PDF statements can be uploaded."}
        if not blob.startswith(b"%PDF"):
            return {"ok": False,
                    "error": f"{safe} is not a PDF — it does not start with %PDF."}

        os.makedirs(PDF_DIR, exist_ok=True)
        dest = os.path.join(PDF_DIR, safe)
        replaced = os.path.exists(dest)
        with open(dest, "wb") as fh:
            fh.write(blob)

        # Absolute paths, because the parser runs in its own process: a relative
        # --db would otherwise resolve against wherever that process starts, and
        # the upload would land in a different store from the one being served.
        db_path = os.path.abspath(self.db_path)
        csv_out = os.path.join(os.path.dirname(db_path), "cleanedData", "transactions.csv")
        with _parse_lock:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "parse.py"), dest,
                 "--db", db_path, "-o", csv_out, "--force"],
                capture_output=True, text=True,
            )
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

        with self.engine() as eng:
            stmt = next((s for s in eng.statements() if s.source_file == safe), None)
            result["statement"] = _statement_dict(stmt) if stmt else None
            result["coverage"] = _coverage_dict(eng.coverage())
            result["transactionCount"] = sum(
                1 for t in eng.transactions if t.source_file == safe)
        return result

    def delete_statement(self, source_file: str) -> dict:
        safe = os.path.basename(urllib.parse.unquote(source_file))
        with _parse_lock, FinanceDB(self.db_path) as db:
            removed = db.delete_source(safe)
            db.conn.execute("DELETE FROM statements WHERE source_file = ?", (safe,))
            db.conn.commit()
        path = os.path.join(PDF_DIR, safe)
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
        if host not in LOCAL_HOSTS or (origin and origin_host not in LOCAL_HOSTS):
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

        if path.startswith("/api/"):
            name = path[5:].strip("/").replace("-", "_")
            fn = {"health": self.api.health, "summary": self.api.summary,
                  "statements": self.api.statements,
                  "transactions": self.api.transactions,
                  "categories": self.api.categories, "months": self.api.months,
                  "recurring": self.api.recurring, "merchants": self.api.merchants,
                  "anomalies": self.api.anomalies}.get(name)
            if not fn:
                return self._send_json({"error": f"no endpoint {name}"}, 404)
            try:
                return self._send_json(fn(q))
            except SystemExit as e:
                return self._send_json({"error": str(e)}, 400)
            except Exception as e:                       # noqa: BLE001
                import traceback
                traceback.print_exc()
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        return self._serve_static(path)

    def do_POST(self):
        if self._refuse_foreign():
            return
        if urllib.parse.urlparse(self.path).path != "/api/upload":
            return self._send_json({"error": "not found"}, 404)
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
        if not path.startswith("/api/statements/"):
            return self._send_json({"error": "not found"}, 404)
        try:
            return self._send_json(
                self.api.delete_statement(path[len("/api/statements/"):]))
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
    web = os.path.join(HERE, "web")
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


def serve(db: str, port: int, open_browser: bool, host: str = "127.0.0.1"):
    Handler.api = Api(db)
    httpd = ThreadingHTTPServer((host, port), Handler)
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
    ap.add_argument("--no-build", action="store_true",
                    help="do not build the dashboard if web/dist is missing")
    args = ap.parse_args()
    if not args.no_build:
        ensure_built()
    serve(args.db, args.port, args.open, args.host)


if __name__ == "__main__":
    main()
