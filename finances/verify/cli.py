"""
The engine from the command line.

    finances verify statement.pdf march.ofx april.ofx
    finances reconcile --bank bank.csv --processor stripe.csv \\
                       --ledger gl.csv --ledger-account Checking --json evidence.json
    finances evidence check evidence.json --inputs ./exports --rerun

``verify`` runs the checks a set of files allows on its own. ``reconcile`` adds
matching between roles. ``evidence check`` re-checks a report: its digest, its
input files, and (with --rerun) that the same inputs give the same result.

Reviewer decisions for ``reconcile --decisions`` are a JSON list:
    [{"match": "m_…", "decision": "matched" | "rejected", "by": "name", "note": "…"}]

Exit status: 0 when the result is clean, 1 when something needs a person,
2 on bad input. Scripts can gate on it.
"""

from __future__ import annotations

import argparse
import json
import sys

from finances.verify.engine import VerificationEngine
from finances.verify.evidence import check_report


def _engine(args) -> VerificationEngine:
    return VerificationEngine(currency=args.currency, window_days=args.window,
                              group_size=args.group_size, tolerance=args.tolerance)


def _common(ap):
    ap.add_argument("--currency", default="USD",
                    help="currency for files that do not say (default USD)")
    ap.add_argument("--window", type=int, default=3,
                    help="days two records' dates may differ and still match (default 3)")
    ap.add_argument("--group-size", type=int, default=6,
                    help="most records one grouped match may combine (default 6)")
    ap.add_argument("--tolerance", default="0",
                    help="accepted amount difference, e.g. 0.01 (default 0)")
    ap.add_argument("--json", metavar="PATH", help="write the evidence report here")
    ap.add_argument("--sarif", metavar="PATH", help="write checks and findings as SARIF 2.1.0")
    ap.add_argument("--no-records", action="store_true",
                    help="leave the normalized records out of the JSON report")
    ap.add_argument("--quiet", action="store_true", help="no summary on stdout")


def _finish(report, args) -> int:
    if not args.quiet:
        print(report.text())
    if args.json:
        report.include_records = not args.no_records
        report.save(args.json)
        if not args.quiet:
            print(f"\nevidence report: {args.json} (content digest "
                  f"{report.content_digest[:16]}…)")
    if args.sarif:
        with open(args.sarif, "w", encoding="utf-8") as fh:
            json.dump(report.to_sarif(), fh, indent=2)
    return 0 if report.clean else 1


def cmd_verify(argv) -> int:
    ap = argparse.ArgumentParser(prog="finances verify",
                                 description="Check statements and exports on their own: "
                                             "balances, totals, chains and coverage.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--password", default="", help="for encrypted statement PDFs")
    _common(ap)
    args = ap.parse_args(argv)
    eng = _engine(args)
    try:
        for f in args.files:
            eng.add(f, **({"password": args.password} if f.lower().endswith(".pdf")
                          and args.password else {}))
    except (ValueError, OSError) as e:
        print(f"finances verify: {e}", file=sys.stderr)
        return 2
    return _finish(eng.run(), args)


def cmd_reconcile(argv) -> int:
    ap = argparse.ArgumentParser(prog="finances reconcile",
                                 description="Match bank, processor and ledger records, "
                                             "and explain what does not match.")
    ap.add_argument("--bank", nargs="*", default=[], help="bank statements or exports")
    ap.add_argument("--processor", nargs="*", default=[],
                    help="Stripe payout reconciliation or balance exports")
    ap.add_argument("--ledger", nargs="*", default=[], help="accounting exports")
    ap.add_argument("--ledger-account", default="",
                    help="the bank account's name in a general-ledger export")
    ap.add_argument("--ledger-sign", choices=("auto", "inverted"), default="auto",
                    help="flip the ledger's amounts if it writes money in as negative")
    ap.add_argument("--decisions", metavar="PATH",
                    help="reviewer decisions to apply (JSON list)")
    _common(ap)
    args = ap.parse_args(argv)
    if sum(bool(x) for x in (args.bank, args.processor, args.ledger)) < 2:
        print("finances reconcile: give at least two of --bank, --processor, --ledger",
              file=sys.stderr)
        return 2
    eng = _engine(args)
    try:
        for f in args.bank:
            eng.add(f, role="bank")
        for f in args.processor:
            eng.add(f, role="processor")
        for f in args.ledger:
            eng.add(f, role="ledger", adapter=None if f.lower().endswith(
                (".ofx", ".qfx", ".xml", ".sta", ".mt940")) else "ledger",
                    **({"account": args.ledger_account} if args.ledger_account else {}),
                    **({"sign": "inverted"} if args.ledger_sign == "inverted" else {}))
        if args.decisions:
            with open(args.decisions, encoding="utf-8") as fh:
                for d in json.load(fh):
                    eng.decide(d["match"], d["decision"], d.get("by", ""), d.get("note", ""),
                               d.get("at"))
    except (ValueError, OSError, KeyError) as e:
        print(f"finances reconcile: {e}", file=sys.stderr)
        return 2
    return _finish(eng.run(), args)


def cmd_evidence(argv) -> int:
    ap = argparse.ArgumentParser(prog="finances evidence",
                                 description="Re-check an evidence report.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="digest, input hashes and (with --rerun) reproduction")
    c.add_argument("report")
    c.add_argument("--inputs", metavar="DIR", help="where the input files are")
    c.add_argument("--rerun", action="store_true",
                   help="run the engine again and compare the result")
    args = ap.parse_args(argv)
    try:
        out = check_report(args.report, args.inputs, args.rerun)
    except (OSError, ValueError, KeyError) as e:
        print(f"finances evidence: {e}", file=sys.stderr)
        return 2
    for status, msg in out["results"]:
        print(f"  [{ {'ok': 'ok', 'fail': 'XX', 'skip': '--'}[status] }] {msg}")
    print("\nThe report checks out." if out["ok"] else "\nThe report does NOT check out.")
    return 0 if out["ok"] else 1


def main() -> int:
    prog = (sys.argv[0] or "").split()[-1]
    argv = sys.argv[1:]
    if prog not in ("verify", "reconcile", "evidence"):
        prog, argv = (argv[0], argv[1:]) if argv else ("verify", ["--help"])
    return {"verify": cmd_verify, "reconcile": cmd_reconcile,
            "evidence": cmd_evidence}[prog](argv)


if __name__ == "__main__":
    sys.exit(main())
