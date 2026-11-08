"""
practice.py — for bookkeepers: a folder per client, a whole folder of statements
in one go, and workpapers to file.

A client is a folder holding its own store and its own statements, and the
categories taught for it:

    clients/
      Acme Bakery/
        finances.db
        PDFs/
        learned_rules.json

Nothing is shared between clients and nothing leaves the machine, which is the
point when the data is someone else's.

    finances client list
    finances client intake "Acme Bakery" ~/Downloads/acme-2026   read every statement there
    finances client workpapers "Acme Bakery" -o acme.xlsx          the reconciliation, to file
    finances web --clients clients                                 switch between them

The workpapers are a workbook a reviewer can sign off: each statement with its
opening and closing balance, the total it prints against the total read from it,
the difference and the verdict, the file's SHA-256 so it can be shown to be the
same file later, and empty "Reviewed by" and "Date" columns for the sign-off.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from datetime import date

from finances import xlsx
from finances.database import FinanceDB

DB_NAME, PDF_NAME = "finances.db", "PDFs"
DEFAULT_ROOT = os.environ.get("FINANCES_CLIENTS") or "clients"


def safe_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 _.&'-]", "", (name or "")).strip(" .")
    if not clean:
        raise ValueError("a client needs a name made of letters, digits and spaces")
    return clean[:80]


def client_paths(root: str, name: str, create: bool = False) -> tuple[str, str]:
    folder = os.path.join(root, safe_name(name))
    if create:
        os.makedirs(os.path.join(folder, PDF_NAME), exist_ok=True)
    elif not os.path.isdir(folder):
        raise ValueError(f"no client called {name!r} in {root}")
    return os.path.join(folder, DB_NAME), os.path.join(folder, PDF_NAME)


def clients(root: str) -> list[dict]:
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root), key=str.lower):
        db = os.path.join(root, name, DB_NAME)
        if not os.path.isdir(os.path.join(root, name)) or name.startswith("."):
            continue
        row = {"name": name, "statements": 0, "reconciled": 0, "mismatched": 0, "through": None}
        if os.path.exists(db):
            with FinanceDB(db) as d:
                sts = [s for s in d.statement_models() if s.checkable]
            row.update(statements=len(sts),
                       reconciled=sum(1 for s in sts if s.reconciled == 1),
                       mismatched=sum(1 for s in sts if s.reconciled == 0),
                       through=max((s.period_end for s in sts if s.period_end), default=None))
        out.append(row)
    return out


def intake(root: str, name: str, folder: str, log=print) -> list[dict]:
    """Read every statement and export in ``folder`` into the client's store."""
    from finances.api import Api
    from finances.sources import looks_like_statement
    db, pdfs = client_paths(root, name, create=True)
    api = Api(db, pdfs)
    results = []
    for f in sorted(os.listdir(folder)):
        path = os.path.join(folder, f)
        if f.startswith(".") or not os.path.isfile(path) or not looks_like_statement(path):
            continue
        with open(path, "rb") as fh:
            r = api.upload(f, fh.read())
        st = r.get("statement") or {}
        row = {"file": f, "ok": bool(r.get("ok")), "account":
               f"{st.get('issuer', '')} {st.get('accountLast4', '')}".strip(),
               "period": st.get("period") or "", "status": st.get("status") or
               ("items" if r.get("lineItems") else "failed"),
               "transactions": r.get("transactionCount") or 0, "error": r.get("error")}
        results.append(row)
        if log:
            verdict = {"ok": "reconciles", "mismatch": "DOES NOT reconcile",
                       "unchecked": "no printed totals", "items": "line items",
                       "failed": f"could not read: {row['error'] or ''}"}[row["status"]]
            log(f"  {f[:44]:<44} {row['account'][:22]:<22} {verdict}")
    return results


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def workpapers(db_path: str, pdf_dir: str, out: str, client: str = "") -> str:
    """The reconciliation as a workbook to review, sign and file."""
    from finances.engine import Engine
    with Engine(db_path) as eng:
        stmts = sorted((s for s in eng.statements() if s.checkable),
                       key=lambda s: (s.issuer, s.account_last4, s.period_start or ""))
        cov = eng.coverage()
        txns = sorted(eng.transactions, key=lambda t: (t.date, t.source_file, t.seq))
        counts: dict[str, int] = {}
        for t in txns:
            counts[t.source_file] = counts.get(t.source_file, 0) + 1

    def digest(s) -> str:
        path = os.path.join(pdf_dir, s.source_file.split("#", 1)[0])
        return _sha256(path) if os.path.isfile(path) else ""

    verdict = {1: "Reconciles", 0: "Does not reconcile", None: "No printed total"}
    recon = [[s.issuer, s.account_last4, s.period_start or None, s.period_end or None,
              s.previous_balance, s.new_balance, s.purchases,
              s.charges_extracted, s.reconcile_diff, verdict[s.reconciled],
              s.reconcile_note, counts.get(s.source_file, 0), s.source_file, digest(s), "", ""]
             for s in stmts]
    exceptions = [["Does not reconcile", f"{s.issuer} {s.account_last4}".strip(),
                   s.period_end or None, s.reconcile_diff, s.reconcile_note, s.source_file]
                  for s in stmts if s.reconciled == 0]
    exceptions += [["Missing statement", g["account"], g["before"], None,
                    f"{g['days']} days with no statement after {g['after']}", ""]
                   for g in cov["gaps"]]
    summary = [["Client", client or os.path.basename(os.path.dirname(os.path.abspath(db_path)))],
               ["Prepared", date.today().isoformat()],
               ["Statements", cov["statements"]],
               ["Reconcile against their printed totals", cov["reconciled"]],
               ["Do not reconcile", cov["mismatched"]],
               ["Print no total to check", cov["unchecked"]],
               ["Gaps between statements", len(cov["gaps"])],
               ["Transactions", len(txns)],
               ["Method", "Each statement's extracted charges are summed and compared with the "
                          "total the statement prints; balances are chained statement to "
                          "statement. SHA-256 identifies each source file."]]
    sheets = [
        xlsx.Sheet("Summary", ["Item", "Value"], summary, ["text", "text"]),
        xlsx.Sheet("Reconciliation",
                   ["Bank", "Account", "From", "To", "Opening balance", "Closing balance",
                    "Printed total", "Read total", "Difference", "Verdict", "Note",
                    "Transactions", "File", "SHA-256", "Reviewed by", "Date reviewed"],
                   recon, ["text", "text", "date", "date", "money", "money", "money", "money",
                           "money", "text", "text", "int", "text", "text", "text", "text"]),
        xlsx.Sheet("Exceptions", ["Kind", "Account", "Date", "Amount", "Detail", "File"],
                   exceptions or [["None", "", None, None, "Every statement reconciles and "
                                   "none is missing.", ""]],
                   ["text", "text", "date", "money", "text", "text"]),
        xlsx.Sheet("Transactions",
                   ["Date", "Description", "Merchant", "Category", "Amount", "Account",
                    "File", "Page"],
                   [[t.date, t.description, t.merchant, t.category, t.amount,
                     f"{t.issuer} {t.account_last4}".strip(), t.source_file, t.page or None]
                    for t in txns],
                   ["date", "text", "text", "text", "money", "text", "text", "int"]),
    ]
    return xlsx.write(out, sheets)


def main() -> int:
    ap = argparse.ArgumentParser(prog="finances client", description=__doc__.split("\n\n")[1])
    ap.add_argument("--root", default=DEFAULT_ROOT, help="the folder of client folders")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("name")
    i = sub.add_parser("intake", help="read every statement in a folder")
    i.add_argument("name")
    i.add_argument("folder")
    w = sub.add_parser("workpapers", help="the reconciliation as a workbook")
    w.add_argument("name")
    w.add_argument("-o", "--out")
    args = ap.parse_args()
    try:
        if args.cmd == "list":
            rows = clients(args.root)
            if not rows:
                print(f"No clients in {args.root} yet:  finances client add NAME")
            for r in rows:
                print(f"  {r['name']:<28} {r['reconciled']:>3}/{r['statements']:<3} reconcile"
                      + (f"  {r['mismatched']} do not" if r["mismatched"] else "")
                      + (f"  through {r['through']}" if r["through"] else ""))
        elif args.cmd == "add":
            client_paths(args.root, args.name, create=True)
            print(f"added {safe_name(args.name)} in {args.root}")
        elif args.cmd == "intake":
            if not os.path.isdir(args.folder):
                ap.error(f"{args.folder} is not a folder")
            rows = intake(args.root, args.name, args.folder)
            ok = sum(1 for r in rows if r["status"] == "ok")
            print(f"\n{len(rows)} files read, {ok} reconcile against their printed totals.")
        elif args.cmd == "workpapers":
            db, pdfs = client_paths(args.root, args.name)
            out = args.out or f"{safe_name(args.name)} workpapers {date.today().isoformat()}.xlsx"
            print(workpapers(db, pdfs, out, safe_name(args.name)))
    except ValueError as e:
        print(f"error: {e}")
        return 1
    return 0

