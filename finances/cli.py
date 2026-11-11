#!/usr/bin/env python3
"""
finances — see where your money goes, from your own statements.

Start here:

    finances demo                a year of made-up data to try it on  -> demo.db
    finances web --db demo.db    ...and look at it in the dashboard

    finances parse ~/statements  your statement PDFs                  -> finances.db
    finances import ~/Downloads  bank CSV/OFX/CAMT.053/MT940, or a Mint,
                                 YNAB, Monarch, Copilot or Tiller export
    finances web                 the dashboard, in a browser
    finances watch               add new statements from Downloads as they arrive

Then:

    finances check               does every number agree with the statements?
    finances budget              monthly limits, and the subscriptions to cancel
    finances wrapped             your year in money
    finances cashflow            the next 60 days
    finances plan status         goals against your real surplus
    finances analyze             a one-page HTML report
    finances ask "..."           a question in plain English, offline
    finances export xlsx         Excel; also beancount, hledger, YNAB, Actual, CSV
    finances mcp                 let Claude and other AI assistants read the store
    finances feed connect TOKEN  optional live feed (SimpleFIN), checked by statements
    finances items orders.csv    what was in each Amazon order

For bookkeeping and developers:

    finances verify FILES        check statements and exports, with an evidence report
    finances reconcile           match bank, processor (Stripe) and ledger records
    finances evidence check      re-check an evidence report
    finances client intake ...   a store per client, folder intake, workpapers
    finances accuracy            per bank, how often statements reconcile first try
    finances report-bank FILE    a statement's layout, scrambled, for a bank-support issue

    finances categorize stats    how much is actually labelled
    finances audit               the reckoning
    finances cpi                 your own inflation rate
    finances backtest            how accurate the forecasts are
    finances anonymize           make it safe to share
    finances enrich              local LLM merchant cleanup
    finances pipeline PDFs       parse, categorize and analyze in one go

Every subcommand forwards the rest of the arguments to the underlying tool, so
``finances analyze --help`` shows analyze.py's own options and nothing is hidden
behind this wrapper.

Standard library only.
"""

from __future__ import annotations

import importlib
import sys

from finances.database import DEFAULT_DB

# subcommand -> (module, one-line help)
COMMANDS: dict[str, tuple[str, str]] = {
    "parse":      ("parse", "extract transactions from statement PDFs into the store"),
    "import":     ("importer", "bank exports (CSV, OFX/QFX, CAMT.053, MT940) into the store"),
    "check":      ("",      "reconciliation and coverage: can these numbers be trusted"),
    "analyze":    ("analyze", "build the spending dashboard"),
    "cashflow":   ("cashflow", "project the daily balance forward and find the low point"),
    "plan":       ("planner", "goals, surplus and when each one lands"),
    "budget":     ("budgets", "monthly limits per category, and the cancel list"),
    "wrapped":    ("wrapped", "your year in money, in one screen"),
    "audit":      ("financial_auditor", "the strict grandfather goes through your statements"),
    "cpi":        ("cpi", "your personal inflation rate, per merchant and per category"),
    "backtest":   ("backtest", "grade the forecasts against what actually happened"),
    "ask":        ("ask", "ask the store a question in plain English, offline"),
    "categorize": ("categorizer", "apply, review and score the category rules"),
    "anonymize":  ("anonymize", "produce a shareable copy of your data"),
    "export":     ("export", "write beancount, hledger/ledger, YNAB or Actual files"),
    "fx":         ("fx", "base currency and exchange rates for multi-currency stores"),
    "enrich":     ("llm_enrich", "clean up merchant names with a local LLM"),
    "engine":     ("engine", "the raw unified view over the store"),
    "watch":      ("sources", "add new statements from Downloads as they arrive"),
    "feed":       ("simplefin", "optional live bank feed (SimpleFIN), checked by statements"),
    "accuracy":   ("accuracy", "how often each bank's statements reconcile on the first try"),
    "report-bank": ("layout", "a statement's layout with your details scrambled, to report a bank"),
    "items":      ("lineitems", "the items behind Amazon (or any shop's) charges"),
    "client":     ("practice", "for bookkeepers: a store per client, intake, workpapers"),
    "web":        ("api", "serve the web dashboard on localhost"),
    "mcp":        ("mcp_server", "serve the store to AI assistants over MCP (read-only)"),
    "verify":     ("verify.cli", "check statements and exports, with an evidence report"),
    "reconcile":  ("verify.cli", "match bank, processor and ledger records; explain the rest"),
    "evidence":   ("verify.cli", "re-check an evidence report: digest, inputs, re-run"),
    "demo":       ("demo", "write a year of made-up statements to demo.db, to try it out"),
    "pipeline":   ("", "parse, categorize and analyze in one run"),
}

ALIASES = {
    "planner": "plan", "auditor": "audit", "dashboard": "analyze",
    "categorizer": "categorize", "flow": "cashflow", "inflation": "cpi",
    "query": "ask", "trust": "check", "serve": "web", "ui": "web",
    "dashboard-web": "web", "api": "web", "budgets": "budget",
    "subscriptions": "budget", "year": "wrapped",
}

# Tools with their own subcommands, where the name here is not the name there.
# ``finances audit`` should mean financial_auditor's *audit* subcommand, but
# ``finances audit debt`` should reach its *debt* one — so the verb is injected
# only when the first argument is not already one of the tool's own.
SUBCOMMANDS: dict[str, tuple[set[str], str]] = {
    "financial_auditor": (
        {"audit", "use", "usage", "monitor", "debt", "income", "ignore",
         "rules", "disputes"},
        "audit",
    ),
}


def usage(code: int = 0):
    print(__doc__.strip().split("Every subcommand")[0].rstrip())
    print("\nCommands:")
    for name, (_, blurb) in COMMANDS.items():
        print(f"  {name:<12} {blurb}")
    print("\n  finances <command> --help   for a command's own options")
    sys.exit(code)


def run(module_name: str, argv: list[str], prog: str | None = None) -> int:
    """Hand control to a tool's own main(), with argv shaped as it expects."""
    try:
        mod = importlib.import_module(f"finances.{module_name}")
    except ImportError as e:
        print(f"finances: cannot load finances.{module_name} ({e})", file=sys.stderr)
        return 1
    if not hasattr(mod, "main"):
        print(f"finances: finances.{module_name} has no main()", file=sys.stderr)
        return 1

    known, default = SUBCOMMANDS.get(module_name, (set(), ""))
    if default:
        first = next((a for a in argv if not a.startswith("-")), None)
        if first not in known:
            argv = [default, *argv]

    saved = sys.argv
    # argparse names itself after argv[0], so --help says "finances parse", not a
    # module path nobody types.
    sys.argv = [prog or f"finances {module_name}", *argv]
    try:
        return mod.main() or 0
    except SystemExit as e:                 # argparse and sys.exit() from the tool
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = saved


def cmd_check(argv: list[str]) -> int:
    """Report whether the store's numbers can be traced back to the statements.

    This is the question the rest of the toolchain depends on and the one no
    bank-API tool can answer: a statement prints its own totals, so we can prove
    the extraction is complete instead of assuming it.
    """
    import argparse

    from finances.engine import Engine
    from finances.utils import fmt_money

    ap = argparse.ArgumentParser(prog="finances check",
                                 description="Reconciliation and coverage report.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero unless everything reconciles")
    args = ap.parse_args(argv)

    with Engine(args.db) as eng:
        n_txn = len(eng.transactions)
        cov = eng.coverage()
        rng = eng.db.date_range() if eng.db else None

        print(f"Store       {args.db}")
        print(f"Rows        {n_txn} transactions"
              + (f", {rng[0]} to {rng[1]}" if rng else ""))
        print(f"Statements  {cov['statements']}")
        print()

        if not cov["statements"]:
            print("No statement summaries on record, so nothing can be verified.")
            print(f"Run:  finances parse <PDFs> --db {args.db}")
            return 1 if args.strict else 0

        print(f"Reconciled  {cov['reconciled']}/{cov['statements']} "
              f"({cov['pct']:.0f}%)")
        if cov["mismatched"]:
            print(f"Mismatched  {cov['mismatched']}")
        if cov["unchecked"]:
            print(f"Unchecked   {cov['unchecked']}  "
                  f"(no printed total to compare against)")
        print()

        if cov["mismatches"]:
            print("Statements whose extracted charges do not match their own total:")
            for s in cov["mismatches"][:15]:
                diff = s.reconcile_diff or 0.0
                print(f"  {s.source_file[:44]:<46} off by {fmt_money(abs(diff), 2):>12}"
                      f"  {s.reconcile_note}")
            print()

        if cov["gaps"]:
            print("Gaps between statements — these months are missing entirely:")
            for g in cov["gaps"][:10]:
                print(f"  {g['account']:<28} {g['after']} -> {g['before']} "
                      f"({g['days']} days)")
            print()

        if cov["trusted"]:
            print("Every statement reconciles and no months are missing. "
                  "The totals downstream are traceable to a statement line.")
            return 0

        print("Totals computed from this store may be wrong. Diagnose a file with:")
        print("  finances parse PDFs/<file>.pdf --dump-columns")
        return 1 if args.strict else 0


def cmd_pipeline(argv: list[str]) -> int:
    """Parse, categorize and analyze in the order that actually works."""
    import argparse

    ap = argparse.ArgumentParser(prog="finances pipeline",
                                 description="Run the whole chain over a folder of PDFs.")
    ap.add_argument("input", help="PDF file or folder")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--income", type=float, help="monthly take-home, passed to analyze")
    args = ap.parse_args(argv)

    steps: list[tuple[str, str, list[str]]] = [
        ("Parsing statements", "parse", [args.input, "--db", args.db]),
        ("Categorizing", "categorizer", ["apply", args.db, "--db", args.db]),
        ("Building the dashboard", "analyze",
         ["--db", args.db] + (["--income", str(args.income)] if args.income else [])),
    ]
    for title, mod, sub_argv in steps:
        print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))
        rc = run(mod, sub_argv)
        if rc:
            print(f"\nfinances: {mod}.py exited {rc}; stopping here.", file=sys.stderr)
            return rc

    print("\n=== Trust " + "=" * 55)
    cmd_check(["--db", args.db])
    return 0


# Commands that only read the store. Run where there is none, they would leave an
# empty finances.db behind that every later command then trusts as real.
READS_STORE = {"check", "accuracy", "analyze", "cashflow", "wrapped", "cpi", "backtest", "export",
               "engine", "audit", "ask", "plan", "budget", "categorize"}

GETTING_STARTED = """\
There is no {db} here yet, so there is nothing to read.

  Try it on made-up data:   finances demo   then   finances web --db demo.db
  Use your own files:       finances parse ~/Downloads/statements
                            finances import ~/Downloads/transactions.csv

Or point at a store elsewhere with --db PATH."""


def _db_arg(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--db" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--db="):
            return a.split("=", 1)[1]
    return DEFAULT_DB


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
    cmd = ALIASES.get(argv[0], argv[0])
    if cmd not in COMMANDS:
        import difflib
        near = difflib.get_close_matches(argv[0], [*COMMANDS, *ALIASES], n=1, cutoff=0.6)
        hint = f" Did you mean 'finances {near[0]}'?" if near else ""
        print(f"finances: unknown command {argv[0]!r}.{hint}\n"
              f"Run 'finances' on its own for the list.", file=sys.stderr)
        return 2

    rest = argv[1:]
    if cmd in READS_STORE and not any(a in ("-h", "--help") for a in rest):
        import os
        db = _db_arg(rest)
        if not os.path.exists(db):
            print(GETTING_STARTED.format(db=db), file=sys.stderr)
            return 1
    if cmd == "check":
        return cmd_check(rest)
    if cmd == "pipeline":
        return cmd_pipeline(rest)
    return run(COMMANDS[cmd][0], rest, prog=f"finances {cmd}")


if __name__ == "__main__":
    sys.exit(main())
