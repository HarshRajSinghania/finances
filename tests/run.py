#!/usr/bin/env python3
"""
Entry point for the test suite.

The suite is plain stdlib ``unittest`` — pytest is not a dependency of this
project and must not become one — so this file exists mainly to give the golden
files a home for their two verbs. Running the tests themselves needs no wrapper:

    .venv/bin/python -m unittest discover tests -v
    .venv/bin/python tests/run.py                    # the same thing
    .venv/bin/python tests/run.py -k recurring       # one slice
    .venv/bin/python tests/run.py --update-golden    # re-record parse.py output
    .venv/bin/python tests/run.py --check-golden     # diff, and exit 1 on drift

``--update-golden`` re-parses every PDF in ``PDFs/`` and rewrites
``tests/golden/<name>.json``, printing what moved. Do that after a parser change
you believe in, and read the diff before committing it.

``--check-golden`` is the same comparison as an error. The unittest suite only
*reports* golden drift, because parse.py is being fixed and its numbers are
expected to change; use this when you want drift to break a build.
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
import golden_harness as gh  # noqa: E402


def update_golden() -> int:
    print(f"Re-recording goldens from {support.PDF_DIR}")
    return 0 if gh.update_all() else 1


def check_golden() -> int:
    pdfs = support.pdf_paths()
    if not pdfs:
        print(f"No PDFs in {support.PDF_DIR}; nothing to check.")
        return 0
    if not support.has_pdfplumber():
        print("pdfplumber is not installed; nothing to check.")
        return 0

    missing, drifted = [], []
    for p in pdfs:
        name = os.path.basename(p)
        old = gh.load(p)
        if old is None:
            missing.append(name)
            continue
        try:
            new = gh.record(p)
        except Exception as e:
            drifted.append((name, [f"no longer parses: {type(e).__name__}: {e}"]))
            continue
        changes = gh.diff(old, new)
        if changes:
            drifted.append((name, changes))

    for name in missing:
        print(f"  missing golden  {name}")
    for name, changes in drifted:
        print(f"  drift  {name}")
        for c in changes:
            print(f"           {c}")

    if not missing and not drifted:
        print(f"All {len(pdfs)} goldens match.")
        return 0
    print(f"\n{len(drifted)} file(s) drifted, {len(missing)} missing. "
          f"Re-record with: {sys.executable} tests/run.py --update-golden")
    return 1


def run_tests(pattern: str | None, verbosity: int, failfast: bool) -> int:
    loader = unittest.TestLoader()
    if pattern:
        loader.testNamePatterns = [f"*{pattern}*"]
    suite = loader.discover(support.TESTS_DIR, top_level_dir=support.TESTS_DIR)
    result = unittest.TextTestRunner(verbosity=verbosity,
                                     failfast=failfast).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the finances test suite.",
        epilog="With no options this is equivalent to "
               "'python -m unittest discover tests'.")
    ap.add_argument("--update-golden", action="store_true",
                    help="re-parse PDFs/ and rewrite tests/golden/*.json")
    ap.add_argument("--check-golden", action="store_true",
                    help="compare PDFs/ against tests/golden and exit 1 on drift")
    ap.add_argument("-k", "--pattern", metavar="SUBSTRING",
                    help="only run tests whose module.Class.method name contains "
                         "SUBSTRING (case-sensitive, glob metacharacters allowed)")
    ap.add_argument("-q", "--quiet", action="store_true", help="one line of output")
    ap.add_argument("-v", "--verbose", action="store_true", help="name every test")
    ap.add_argument("-x", "--failfast", action="store_true", help="stop at the first failure")
    args = ap.parse_args()

    if args.update_golden and args.check_golden:
        ap.error("--update-golden and --check-golden are mutually exclusive")
    if args.update_golden:
        return update_golden()
    if args.check_golden:
        return check_golden()

    verbosity = 0 if args.quiet else 2 if args.verbose else 1
    return run_tests(args.pattern, verbosity, args.failfast)


if __name__ == "__main__":
    sys.exit(main())
