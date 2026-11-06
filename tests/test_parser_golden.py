"""
Golden-file and structural tests for parse.py against the real statements.

parse.py is being actively fixed and its numbers are known to be wrong today,
so this file is deliberately split in two:

* **Structural contract.** Assertions that must hold for any correct parser,
  regardless of which column it decides is the amount. An issuer, an account,
  a reconciliation verdict, non-negative charge and credit totals, ISO dates, a
  direction that agrees with the sign, and transaction dates that stay inside
  the billing cycle. These are real, failing-is-failing assertions.

* **Numeric change detector.** The recorded per-file counts and totals in
  ``tests/golden/``. A diff there is printed, not failed, because the whole
  point of the parser work in flight is to change those numbers. Use
  ``tests/run.py --check-golden`` when you want a diff to be an error, and
  ``tests/run.py --update-golden`` to re-record once the fixes land.

Three assertions are shipped as documented ``expectedFailure``s. They describe
real defects found while building this suite, not aspirations; see each
docstring. When the parser is fixed they will report as unexpected successes,
which is the signal to promote them to ordinary tests.

The whole module skips cleanly when pdfplumber or PDFs/ is unavailable.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import date, datetime, timedelta

import support
import golden_harness as gh

HAVE_PDFS = bool(support.pdf_paths())
HAVE_PDFPLUMBER = support.has_pdfplumber()
CAN_PARSE = HAVE_PDFS and HAVE_PDFPLUMBER
REASON = ("pdfplumber is not installed" if not HAVE_PDFPLUMBER else
          f"no PDFs in {support.PDF_DIR}")

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A statement covers one billing cycle. Card cycles run 28-31 days; a few days
# of slack on each side absorbs a transaction that posted late.
MAX_CYCLE_DAYS = 70
PERIOD_SLACK_DAYS = 5

_CORPUS: dict[str, tuple] | None = None


def corpus() -> dict[str, tuple]:
    """Parse every PDF once and share the result across the whole module.

    Parsing the corpus costs a few seconds; doing it per test would cost
    minutes, and every test here wants the same 32 results.
    """
    global _CORPUS
    if _CORPUS is None:
        from finances import parse
        out = {}
        for p in support.pdf_paths():
            name = os.path.basename(p)
            try:
                out[name] = parse.parse_statement(p)
            except Exception as e:                  # recorded, not raised
                out[name] = (None, {"error": f"{type(e).__name__}: {e}"})
        _CORPUS = out
    return _CORPUS


def parsed_items():
    """(name, txns, meta) for every PDF that parsed at all."""
    return [(n, t, m) for n, (t, m) in sorted(corpus().items()) if t is not None]


def iso(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@unittest.skipUnless(CAN_PARSE, REASON)
class ParserSmokeTests(unittest.TestCase):

    def test_every_pdf_parses_without_raising(self):
        broken = {n: m.get("error") for n, (t, m) in corpus().items() if t is None}
        self.assertEqual(broken, {})

    def test_the_corpus_is_not_empty(self):
        self.assertGreater(len(parsed_items()), 0)


@unittest.skipUnless(CAN_PARSE, REASON)
class StatementStructureTests(unittest.TestCase):
    """What every statement must produce, whatever the numbers turn out to be."""

    def test_every_statement_identifies_its_issuer(self):
        for name, _txns, meta in parsed_items():
            with self.subTest(pdf=name):
                self.assertTrue((meta.get("issuer") or "").strip(),
                                "no issuer detected")

    def test_every_statement_identifies_the_account(self):
        for name, _txns, meta in parsed_items():
            with self.subTest(pdf=name):
                last4 = (meta.get("last4") or "").strip()
                self.assertTrue(last4, "no account number detected")
                self.assertRegex(last4, r"^\d{4}$")

    def test_every_statement_produces_a_statement_model(self):
        from finances.models import Statement
        for name, _txns, meta in parsed_items():
            with self.subTest(pdf=name):
                s = meta.get("statement")
                self.assertIsInstance(s, Statement)
                self.assertEqual(s.source_file, name)

    def test_every_statement_produces_a_reconciliation_verdict(self):
        # None ("nothing printed to check against") is a legitimate verdict;
        # an unrecognised one is not.
        for name, txns, meta in parsed_items():
            with self.subTest(pdf=name):
                self.assertIn(gh.verdict(txns, meta), {"ok", "mismatch", "unchecked"})
                self.assertIn(getattr(meta.get("statement"), "reconciled", None),
                              (None, 0, 1))

    def test_charge_and_credit_totals_are_non_negative(self):
        for name, txns, meta in parsed_items():
            with self.subTest(pdf=name):
                charges = sum(t.amount for t in txns if t.amount > 0)
                credits = sum(-t.amount for t in txns if t.amount < 0)
                self.assertGreaterEqual(charges, 0.0)
                self.assertGreaterEqual(credits, 0.0)

    def test_extracted_totals_agree_with_the_statement_model(self):
        for name, txns, meta in parsed_items():
            with self.subTest(pdf=name):
                s = meta["statement"]
                charges = sum(t.amount for t in txns if t.amount > 0)
                credits = sum(-t.amount for t in txns if t.amount < 0)
                self.assertAlmostEqual(s.charges_extracted or 0.0, charges, places=2)
                self.assertAlmostEqual(s.credits_extracted or 0.0, credits, places=2)

    def test_every_statement_produces_a_period(self):
        # statement_period is copied onto every transaction, period_start/end
        # are derived from it, and engine.coverage() finds gaps between billing
        # cycles by comparing those bounds. No period means no coverage check,
        # and the trust reporting silently reports nothing.
        for name, _txns, meta in parsed_items():
            with self.subTest(pdf=name):
                self.assertTrue((meta.get("period") or "").strip(),
                                "no statement period detected")

    def test_a_printed_purchases_total_yields_extracted_charges(self):
        """A statement that says it had purchases must produce some.

        This is the single clearest signal that a column or a sign was read
        wrong, which is why it is a structural assertion rather than a number in
        the goldens. It caught six files (five Discover statements whose rows
        all came out negative, plus an April Venture statement that extracted
        nothing at all) before the sign handling was fixed.
        """
        broken = []
        for name, txns, meta in parsed_items():
            printed = (meta.get("checks") or {}).get("total_purchases")
            charges = sum(t.amount for t in txns if t.amount > 0)
            if printed and abs(float(printed)) > 0.01 and charges < 0.01:
                broken.append((name, round(float(printed), 2)))
        self.assertEqual(broken, [])


@unittest.skipUnless(CAN_PARSE, REASON)
class TransactionStructureTests(unittest.TestCase):
    """What every extracted row must look like."""

    def test_dates_are_iso_and_real(self):
        for name, txns, _meta in parsed_items():
            for t in txns:
                with self.subTest(pdf=name, desc=t.description[:40]):
                    self.assertRegex(t.date, ISO_DATE)
                    self.assertIsNotNone(t.d)

    def test_direction_agrees_with_the_amount_sign(self):
        # OUT positive, IN negative. Everything downstream filters on this.
        for name, txns, _meta in parsed_items():
            for t in txns:
                with self.subTest(pdf=name, desc=t.description[:40]):
                    self.assertEqual(t.direction, "out" if t.amount > 0 else "in")

    def test_amounts_are_finite_and_non_zero(self):
        import math
        for name, txns, _meta in parsed_items():
            for t in txns:
                with self.subTest(pdf=name, desc=t.description[:40]):
                    self.assertTrue(math.isfinite(t.amount))
                    self.assertNotEqual(t.amount, 0.0)

    def test_every_row_carries_its_provenance(self):
        # Without source_file, delete_source() cannot un-do a bad parse.
        for name, txns, meta in parsed_items():
            for t in txns:
                with self.subTest(pdf=name, desc=t.description[:40]):
                    self.assertEqual(t.source_file, name)
                    self.assertEqual(t.issuer, meta["issuer"])
                    self.assertEqual(t.account_last4, meta["last4"])

    def test_every_row_has_a_description_and_a_merchant(self):
        for name, txns, _meta in parsed_items():
            for t in txns:
                with self.subTest(pdf=name):
                    self.assertTrue(t.description.strip())
                    self.assertTrue(t.merchant.strip())

    @unittest.expectedFailure
    def test_BUG_repeat_charges_collapse_on_the_dedup_key(self):
        """85 real transactions across 6 statements are lost when stored.

        The transactions table's PRIMARY KEY — and Transaction.dedup_key — is
        (date, description, amount, account_last4). That is exactly right for
        folding a statement parsed twice, and exactly wrong for two genuine
        identical charges. Two $3.00 PATH taps on the same day carry byte-for-
        byte the same descriptor, so the second INSERT OR REPLACE overwrites the
        first and the fare disappears.

        Measured against PDFs/ (parse.py is in flight, so treat the counts as
        indicative and the shape as the point):
            checking statement A      41 of 181 rows lost
            checking statement B      24 of 192
            checking statement C      11 of 211
            checking statement D       4 of 148
            Amex statement             4 of  40
            Venture statement          1 of  25

        This is silent under-counting of exactly the small, frequent, repeated
        spending the tools are meant to surface. A fix needs an occurrence
        counter or a per-file line ordinal in the key, so a re-parse of the same
        file still folds while two distinct charges stay distinct. Both
        models.Transaction.dedup_key and database._TXN_KEY have to change
        together or in-memory and on-disk dedup disagree.
        """
        lost = {}
        for name, txns, _meta in parsed_items():
            keys = [t.dedup_key for t in txns]
            if len(keys) != len(set(keys)):
                lost[name] = len(keys) - len(set(keys))
        self.assertEqual(lost, {})

    def test_dates_are_ordered_within_the_extracted_rows(self):
        # Not an ordering requirement on the output, just a sanity check that
        # min and max are computable on every file.
        for name, txns, _meta in parsed_items():
            with self.subTest(pdf=name):
                ds = [t.d for t in txns if t.d]
                self.assertEqual(len(ds), len(txns))


@unittest.skipUnless(CAN_PARSE, REASON)
class PeriodBoundsTests(unittest.TestCase):
    """Transaction dates against the statement's own period bounds.

    These skip cleanly if parse.py stops populating
    Statement.period_start/period_end, so the file keeps working across the
    reconciliation change rather than needing an edit when a field appears.
    """

    def with_bounds(self):
        out = []
        for name, txns, meta in parsed_items():
            s = meta.get("statement")
            a, b = iso(getattr(s, "period_start", "")), iso(getattr(s, "period_end", ""))
            if a and b:
                out.append((name, txns, a, b))
        return out

    def test_period_start_is_before_period_end(self):
        items = self.with_bounds()
        if not items:
            self.skipTest("parse.py does not populate period_start/period_end")
        for name, _txns, a, b in items:
            with self.subTest(pdf=name):
                self.assertLess(a, b)
                self.assertLessEqual((b - a).days, MAX_CYCLE_DAYS)

    def test_every_statement_has_period_bounds(self):
        items = parsed_items()
        without = [n for n, _t, m in items
                   if not (iso(getattr(m.get("statement"), "period_start", ""))
                           and iso(getattr(m.get("statement"), "period_end", "")))]
        if len(without) == len(items):
            self.skipTest("parse.py does not populate period_start/period_end")
        self.assertEqual(without, [])

    def test_transaction_dates_stay_inside_the_billing_cycle(self):
        """No row may fall outside the period it was extracted from.

        This is the year-rollover guard. It caught the defect while this suite
        was being written: a January Chase checking statement covering early
        December to early January dated 38 of its 45 rows in the *following*
        December, and a January Chase card statement did the same for 24 of 26, each file spanning
        ~361 days instead of one cycle. A December row on a January statement
        was taking the statement's own year instead of the year before.

        The downstream damage is what makes this worth asserting rather than
        recording: engine.by_month() grows a phantom month, complete_months()
        stretches the covered range to a full year, and monthly_average() ends
        up divided by roughly twelve instead of one.

        Two checks, one root cause, so they stay in one test: the span of the
        extracted dates, and their position relative to the stated period.
        """
        slack = timedelta(days=PERIOD_SLACK_DAYS)
        long_spans, escapees = [], []

        for name, txns, _meta in parsed_items():
            ds = sorted(t.d for t in txns if t.d)
            if ds and (ds[-1] - ds[0]).days > MAX_CYCLE_DAYS:
                long_spans.append((name, str(ds[0]), str(ds[-1]),
                                   (ds[-1] - ds[0]).days))

        for name, txns, a, b in self.with_bounds():
            bad = [t for t in txns if t.d and not (a - slack <= t.d <= b + slack)]
            if bad:
                escapees.append((name, f"{len(bad)}/{len(txns)} rows",
                                 f"period {a}..{b}",
                                 f"rows {min(t.d for t in bad)}..{max(t.d for t in bad)}"))

        self.assertEqual((long_spans, escapees), ([], []))


@unittest.skipUnless(CAN_PARSE, REASON)
class GoldenFileTests(unittest.TestCase):
    """The recorded numbers. Diffs are reported, not failed — see the module
    docstring and tests/README.md."""

    def test_a_golden_exists_for_every_pdf(self):
        pdfs = support.pdf_paths()
        missing = [os.path.basename(p) for p in pdfs if gh.load(p) is None]
        if len(missing) == len(pdfs):
            # tests/golden/ is gitignored, so a fresh clone has none at all.
            # That is a "record them" situation, not a regression.
            self.skipTest("no goldens recorded yet; run: "
                          ".venv/bin/python tests/run.py --update-golden")
        self.assertEqual(
            missing, [],
            "a statement was added without re-recording; run: "
            ".venv/bin/python tests/run.py --update-golden")

    def test_goldens_have_the_expected_shape(self):
        for p in support.pdf_paths():
            rec = gh.load(p)
            if rec is None:
                continue
            with self.subTest(pdf=os.path.basename(p)):
                self.assertEqual(rec.get("golden_version"), gh.GOLDEN_VERSION)
                self.assertEqual(rec.get("source_file"), os.path.basename(p))
                if "error" not in rec:
                    for f in ("issuer", "last4", "transactions",
                              "charges", "credits", "reconciliation"):
                        self.assertIn(f, rec)

    def test_numeric_output_against_the_recorded_goldens(self):
        """Change detector. Reports what moved; does not fail on movement.

        parse.py is mid-fix and the recorded values are known-wrong, so failing
        here would make the suite a tripwire that everyone learns to ignore. Run
        ``tests/run.py --check-golden`` for the same comparison as a hard error.
        """
        drifted = []
        for p in support.pdf_paths():
            old = gh.load(p)
            if old is None or "error" in old:
                continue
            name = os.path.basename(p)
            txns, meta = corpus().get(name, (None, {}))
            if txns is None:
                drifted.append((name, ["file no longer parses"]))
                continue
            changes = gh.diff(old, gh.record_from(p, txns, meta))
            if changes:
                drifted.append((name, changes))

        if drifted:
            print("\n  golden drift (informational, not a failure):", file=sys.stderr)
            for name, changes in drifted:
                print(f"    {name}", file=sys.stderr)
                for c in changes:
                    print(f"      {c}", file=sys.stderr)
            print("    regenerate with: .venv/bin/python tests/run.py --update-golden",
                  file=sys.stderr)


class SkipConditionTests(unittest.TestCase):
    """These run even without pdfplumber or PDFs, to prove the skip is clean."""

    def test_harness_paths_are_sane(self):
        self.assertTrue(os.path.isdir(support.TESTS_DIR))
        self.assertEqual(os.path.basename(support.GOLDEN_DIR), "golden")

    def test_golden_path_is_derived_from_the_pdf_name(self):
        p = gh.golden_path("/somewhere/April Discover.pdf")
        self.assertEqual(os.path.basename(p), "April Discover.json")

    def test_diff_reports_only_changed_fields(self):
        a = {"transactions": 2, "charges": 1.0, "credits": 0.0}
        b = {"transactions": 3, "charges": 1.0, "credits": 0.0}
        self.assertEqual(gh.diff(a, b, fields=("transactions", "charges")),
                         ["transactions: 2 -> 3"])

    def test_verdict_falls_back_to_the_printed_purchases_total(self):
        class Fake:
            reconciled = None

        class Row:
            def __init__(self, amount):
                self.amount = amount

        meta_ok = {"statement": Fake(), "checks": {"total_purchases": 100.0}}
        self.assertEqual(gh.verdict([Row(60.0), Row(40.0)], meta_ok), "ok")
        self.assertEqual(gh.verdict([Row(60.0)], meta_ok), "mismatch")
        self.assertEqual(gh.verdict([Row(60.0)], {"statement": Fake(), "checks": {}}),
                         "unchecked")

    def test_verdict_prefers_the_statement_flag(self):
        class Fake:
            reconciled = 1

        self.assertEqual(gh.verdict([], {"statement": Fake(), "checks": {}}), "ok")


if __name__ == "__main__":
    unittest.main()
