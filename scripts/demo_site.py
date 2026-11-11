#!/usr/bin/env python3
"""
demo_site.py — the dashboard as a static site, on the made-up demo data.

Nothing runs on a server: the page is the normal dashboard built with
VITE_DEMO=1, and every /api answer it needs is written beside it as JSON from a
freshly made demo store (see web/src/lib/demo.ts). Every number in it is
invented by ``finances demo``; no real statement is read.

    uv run python scripts/demo_site.py                  # -> site/
    uv run python scripts/demo_site.py --out /tmp/site
    cd site && vercel link --yes --project finances-demo && vercel deploy --prod

Rebuild it after a release so the demo shows the current dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from finances import export                                   # noqa: E402
from finances.api import Api, _json_default, _txn_dict         # noqa: E402
from finances.categorizer import NON_SPEND                     # noqa: E402
from finances.database import FinanceDB                        # noqa: E402
from finances.lineitems import items_by_txn                    # noqa: E402

# The GET endpoints the dashboard calls with no arguments, or with the default ones.
PLAIN = ["health", "summary", "statements", "categories", "months", "recurring", "anomalies",
         "wrapped", "budgets", "goals", "subscriptions/review", "payments", "networth",
         "sources", "feed", "clients"]

VERCEL = {
    # Files are served first; any other path is a page of the app.
    "rewrites": [{"source": "/(.*)", "destination": "/index.html"}],
    "headers": [{"source": "/(.*)", "headers": [
        {"key": "X-Content-Type-Options", "value": "nosniff"},
        {"key": "Referrer-Policy", "value": "no-referrer"}]}],
}


def snapshot(db: str, out: str) -> int:
    api = Api(db, os.path.join(os.path.dirname(db), "PDFs"))
    os.makedirs(out, exist_ok=True)

    def write(name: str, payload) -> None:
        with open(os.path.join(out, name.replace("/", "__") + ".json"), "w") as fh:
            json.dump(payload, fh, default=_json_default, separators=(",", ":"))

    for name in PLAIN:
        write(name, getattr(api, {"subscriptions/review": "subscription_review",
                                  "sources": "statement_sources"}.get(name, name))({}))
    write("recurring__interest", api.recurring({"includeInterest": "true"}))
    write("merchants", api.merchants({"limit": "500"}))
    for year in api.wrapped({}).get("years") or []:
        write(f"wrapped__{year}", api.wrapped({"year": str(year)}))

    with api.engine() as eng:
        items = items_by_txn(eng.db) if eng.db is not None else {}
        rows = []
        for t in eng.transactions:
            d = _txn_dict(t)
            found = items.get((t.date, t.description, round(t.amount, 2), t.account_last4, t.seq))
            if found:
                d["items"] = found
            rows.append(d)
    write("transactions", {"transactions": rows, "nonSpend": sorted(NON_SPEND)})

    with FinanceDB(db) as store:
        txns = sorted(store.transactions(), key=lambda t: (t.date, t.source_file, t.seq))
        export.workbook(txns, store.statement_models(), db, os.path.join(out, "export.xlsx"))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--out", default=os.path.join(ROOT, "site"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)

    env = dict(os.environ, VITE_DEMO="1")
    web = os.path.join(ROOT, "web")
    if not os.path.isdir(os.path.join(web, "node_modules")):
        subprocess.run(["npm", "ci"], cwd=web, check=True)
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(["npm", "run", "build", "--", "--outDir", out, "--emptyOutDir"],
                   cwd=web, env=env, check=True)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "demo.db")
        subprocess.run([sys.executable, "-m", "finances", "demo", "--db", db],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
                       env=dict(os.environ, PYTHONPATH=ROOT))
        n = snapshot(db, os.path.join(out, "demo-api"))

    with open(os.path.join(out, "vercel.json"), "w") as fh:
        json.dump(VERCEL, fh, indent=1)
    with open(os.path.join(out, "robots.txt"), "w") as fh:
        fh.write("User-agent: *\nAllow: /\n")
    print(f"{out}: the dashboard and {n} made-up transactions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
