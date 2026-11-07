"""
Tests for finances.verify — the verification engine.

The fixtures in tests/fixtures/verify are one month of a small business: a bank
CSV, Stripe's itemized payout reconciliation report and a QuickBooks general
ledger. They contain, on purpose:

- a payout that arrives as one bank deposit (exact match),
- a payout the bank shows with its trace id (reference match),
- a ledger that booked a payout before fees: 260.00 against 252.00, the
  difference being the 8.00 of Stripe fees (explained, sent to review),
- a cheque deposit of 450.00 booked as two invoices of 300 and 150 (grouped),
- an AWS charge and a bank fee the books never recorded, and printer paper the
  bank never paid (findings),
- a payout dated after the bank export ends (not a finding: out of range).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal

import support

from finances.verify import (AMBIGUOUS, INCOMPLETE, MATCHED, MISMATCH, NEEDS_REVIEW,
                             UNRESOLVED, UNVERIFIABLE, VERIFIED, VerificationEngine,
                             check_report, money)
from finances.verify.match import Matcher, Options, subset_sums

F = os.path.join(support.TESTS_DIR, "fixtures", "verify")
X = os.path.join(support.TESTS_DIR, "fixtures", "exports")
BANK = os.path.join(F, "chase_business_checking_7788.csv")
STRIPE = os.path.join(F, "stripe_payout_reconciliation_itemized.csv")
LEDGER = os.path.join(F, "quickbooks_general_ledger.csv")


def scenario(**kw) -> VerificationEngine:
    eng = VerificationEngine(**kw)
    eng.add(BANK, role="bank")
    eng.add(STRIPE)
    eng.add(LEDGER, role="ledger", account="Checking")
    return eng


def rows(role, *items, name=None):
    """(date, amount, description) tuples as add_rows dicts."""
    return [{"date": d, "amount": a, "description": desc, "account": role}
            for d, a, desc in items]


class MoneyTest(unittest.TestCase):
    def test_floats_arrive_as_their_decimal_value(self):
        self.assertEqual(money.amount(0.1 + 0.2, "USD"), Decimal("0.30"))
        self.assertEqual(money.text(money.amount("-1,234.5", "USD")), "-1234.50")

    def test_minor_units_follow_iso_4217(self):
        self.assertEqual(money.minor(money.amount("1500", "JPY"), "JPY"), 1500)
        self.assertEqual(money.minor(money.amount("1.234", "KWD"), "KWD"), 1234)
        self.assertEqual(money.minor(money.amount("12.34", "USD"), "USD"), 1234)

    def test_rounding_is_half_even(self):
        self.assertEqual(money.amount("0.125", "USD"), Decimal("0.12"))
        self.assertEqual(money.amount("0.135", "USD"), Decimal("0.14"))


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = scenario().run()
        cls.by_id = {r.id: r for r in cls.report.records}

    def describe(self, m):
        return sorted(self.by_id[i].description for i in (*m.left, *m.right))

    def test_every_check_passes(self):
        self.assertEqual({c.rule: c.status for c in self.report.checks}, {
            "processor.row_arithmetic": VERIFIED, "statement.running_balance": VERIFIED,
            "statement.balance": VERIFIED, "ledger.running_balance": VERIFIED,
            "coverage.pair": INCOMPLETE})

    def test_a_payout_after_the_bank_export_is_not_compared_and_says_so(self):
        [c] = [c for c in self.report.checks if c.rule == "coverage.pair"]
        [rid] = c.records
        self.assertEqual(self.by_id[rid].refs["payout"], "po_1QACqrs")
        self.assertNotIn(rid, [r for f in self.report.findings for r in f.records])

    def test_payouts_meet_their_deposits(self):
        pm = [m for m in self.report.matches if m.pair == "bank~processor"]
        self.assertEqual(sorted((m.tier, m.decision) for m in pm),
                         [("exact", MATCHED), ("reference", MATCHED)])
        ref = next(m for m in pm if m.tier == "reference")
        self.assertEqual(ref.block["reference"], "ST-4F7KQ2")

    def test_the_fee_difference_is_explained_not_matched(self):
        [m] = [m for m in self.report.matches if m.decision == NEEDS_REVIEW]
        self.assertEqual((m.pair, m.tier, m.difference), ("bank~ledger", "explained",
                                                          Decimal("8.00")))
        self.assertIn("processing fees on payout po_1Q8AbcD", m.explanation)
        self.assertEqual(self.describe(m), ["STRIPE TRANSFER", "Stripe payout"])

    def test_a_deposit_matches_the_invoices_it_paid(self):
        [g] = [m for m in self.report.matches if m.tier == "group"]
        self.assertEqual(g.decision, MATCHED)
        self.assertEqual(sorted(self.by_id[i].amount for i in g.right),
                         [Decimal("150.00"), Decimal("300.00")])

    def test_shared_document_number_is_a_reference_match(self):
        [m] = [m for m in self.report.matches
               if m.pair == "bank~ledger" and m.tier == "reference"]
        self.assertEqual(m.block["reference"], "INV-1001")

    def test_findings_are_what_is_really_missing(self):
        kinds = sorted((f.kind, money.text(f.amount)) for f in self.report.findings)
        self.assertEqual(kinds, [("missing_in_bank", "-42.10"),
                                 ("missing_in_ledger", "-15.00"),
                                 ("missing_in_ledger", "-312.45")])
        self.assertEqual(self.report.findings[0].severity, "medium")   # largest first

    def test_every_record_points_back_at_its_source(self):
        for r in self.report.records:
            self.assertTrue(r.source and r.source.input_id)
        wire = next(r for r in self.report.records if "INV-1001" in r.description
                    and r.role == "bank")
        self.assertEqual(wire.source.locator, {"record": 11})
        self.assertEqual(wire.source.raw["amount"], "1200.00")
        self.assertEqual(wire.source.sign, "as written")

    def test_derived_payouts_carry_fees_and_trace(self):
        po = {r.refs["payout"]: r for r in self.report.records if r.kind == "payout"}
        self.assertEqual(po["po_1Q8AbcD"].amount, Decimal("252.00"))
        self.assertEqual(po["po_1Q8AbcD"].detail["fees"], Decimal("8.00"))
        self.assertEqual(po["po_1Q9BxyZ"].refs["trace_id"], "ST-4F7KQ2")

    def test_not_clean_until_someone_decides(self):
        self.assertFalse(self.report.clean)
        self.assertEqual(len(self.report.needs_review()), 1)


class DeterminismTest(support.TempDirMixin, unittest.TestCase):
    def test_same_inputs_same_digest_and_ids(self):
        a, b = scenario().run(), scenario().run()
        self.assertEqual(a.content_digest, b.content_digest)
        self.assertEqual([m.id for m in a.matches], [m.id for m in b.matches])

    def test_options_change_the_digest(self):
        self.assertNotEqual(scenario().run().content_digest,
                            scenario(window_days=1).run().content_digest)

    def test_report_round_trip_and_tamper(self):
        path = self.tmp_path("ev.json")
        scenario().run().save(path)
        self.assertTrue(check_report(path)["ok"])
        with open(path) as fh:
            doc = json.load(fh)
        doc["predicate"]["matches"][0]["decision"] = "rejected"
        with open(path, "w") as fh:
            json.dump(doc, fh)
        out = check_report(path)
        self.assertFalse(out["ok"])
        self.assertIn("changed after it was written", out["results"][0][1])

    def test_rerun_reproduces_and_catches_a_changed_input(self):
        path = self.tmp_path("ev.json")
        eng = scenario()
        rep = eng.run()
        eng.decide(rep.needs_review()[0].id, MATCHED, "pat", "fee entry added")
        eng.run().save(path)
        out = check_report(path, F, rerun=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["results"][-1][0], "ok")

        copy = self.tmp_path("inputs")
        shutil.copytree(F, copy)
        with open(os.path.join(copy, os.path.basename(BANK)), "a") as fh:
            fh.write("10/01/2026,SNEAKY,-1.00,4999.00\n")
        out = check_report(path, copy, rerun=True)
        self.assertFalse(out["ok"])
        self.assertTrue(any("sha256 is" in msg for s, msg in out["results"] if s == "fail"))


class ReviewTest(unittest.TestCase):
    def test_a_decision_is_recorded_beside_the_original(self):
        eng = scenario()
        target = eng.run().needs_review()[0]
        eng.decide(target.id, MATCHED, "pat", "booked the 8.00 fee expense")
        rep = eng.run()
        m = next(x for x in rep.matches if x.id == target.id)
        self.assertEqual(m.decision, MATCHED)
        self.assertEqual((m.override["from"], m.override["by"]), (NEEDS_REVIEW, "pat"))
        self.assertIn("person:pat", rep.provenance()["agents"])

    def test_only_matched_or_rejected(self):
        with self.assertRaises(ValueError):
            VerificationEngine().decide("m_x", "maybe", "pat")


class MatcherTest(unittest.TestCase):
    def match(self, left, right, **opts):
        eng = VerificationEngine(**opts)
        eng.add_rows(left, role="bank", name="bank")
        eng.add_rows(right, role="ledger", name="ledger")
        return eng.run()

    def test_equally_close_different_candidates_are_ambiguous(self):
        rep = self.match(rows("b", ("2026-03-10", "50.00", "DEPOSIT")),
                         rows("l", ("2026-03-09", "50.00", "Invoice 7"),
                              ("2026-03-11", "50.00", "Invoice 8")))
        self.assertEqual([m.decision for m in rep.matches], [AMBIGUOUS])

    def test_identical_copies_are_not_ambiguous(self):
        rep = self.match(rows("b", ("2026-03-10", "3.00", "SUBWAY FARE"),
                              ("2026-03-10", "3.00", "SUBWAY FARE")),
                         rows("l", ("2026-03-10", "3.00", "Transit"),
                              ("2026-03-10", "3.00", "Transit")))
        self.assertEqual([m.decision for m in rep.matches], [MATCHED, MATCHED])

    def test_nearest_date_wins_one_to_one(self):
        rep = self.match(rows("b", ("2026-03-01", "20.00", "A"), ("2026-03-05", "20.00", "B")),
                         rows("l", ("2026-03-04", "20.00", "x"), ("2026-03-02", "20.00", "y")))
        pairs = {(rep.record(m.left[0]).description, rep.record(m.right[0]).description)
                 for m in rep.matches}
        self.assertEqual(pairs, {("A", "y"), ("B", "x")})

    def test_outside_the_window_is_not_a_match(self):
        rep = self.match(rows("b", ("2026-03-01", "20.00", "A")),
                         rows("l", ("2026-03-09", "20.00", "x")), window_days=3)
        self.assertEqual(rep.matches, [])
        # The two sides share no dates, so nothing is "missing": it was never
        # comparable, and the report says that instead.
        self.assertEqual(rep.findings, [])
        [c] = [c for c in rep.checks if c.rule == "coverage.pair"]
        self.assertEqual(c.status, INCOMPLETE)
        self.assertIn("no dates at all", c.message)

    def test_two_combinations_are_ambiguous(self):
        rep = self.match(rows("b", ("2026-03-10", "300.00", "DEPOSIT")),
                         rows("l", ("2026-03-09", "100.00", "a"), ("2026-03-09", "200.00", "b"),
                              ("2026-03-08", "175.00", "c"), ("2026-03-08", "125.00", "d")))
        self.assertEqual([(m.tier, m.decision) for m in rep.matches], [("group", AMBIGUOUS)])

    def test_tolerance_is_explicit_and_recorded(self):
        left = rows("b", ("2026-03-10", "100.00", "X"))
        right = rows("l", ("2026-03-10", "100.01", "X"))
        self.assertEqual(self.match(left, right).matches[0].decision, NEEDS_REVIEW)
        m = self.match(left, right, tolerance="0.01").matches[0]
        self.assertEqual((m.decision, m.tier), (MATCHED, "tolerance"))

    def test_a_search_that_runs_out_says_so(self):
        left = [{"date": "2026-03-10", "amount": "1000.01", "description": "D"}]
        right = [{"date": "2026-03-09", "amount": str(100 + i), "description": f"r{i}"}
                 for i in range(20)]
        eng = VerificationEngine()
        eng.add_rows(left, role="bank")
        eng.add_rows(right, role="ledger")
        eng.opts.group_budget = 50
        rep = eng.run()
        self.assertIn(UNRESOLVED, [m.decision for m in rep.matches])
        self.assertNotIn("missing_in_ledger", [f.kind for f in rep.findings])

    def test_subset_sums_with_refunds(self):
        found, exhausted = subset_sums(25200, [(10000, "a"), (20000, "b"), (-4000, "c"),
                                              (-800, "d"), (500, "e")], 6, 10_000)
        self.assertFalse(exhausted)
        self.assertEqual([sorted(x) for x in found], [["a", "b", "c", "d"]])


class StatementChecksTest(support.TempDirMixin, unittest.TestCase):
    OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKACCTFROM><ACCTID>000123459876</BANKACCTFROM>
<BANKTRANLIST><DTSTART>{start}<DTEND>{end}
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{start}<TRNAMT>-10.00<FITID>{fitid}<NAME>X</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>{bal}<DTASOF>{end}</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

    def ofx(self, name, start, end, bal, fitid):
        p = self.tmp_path(name)
        with open(p, "w") as fh:
            fh.write(self.OFX.format(start=start, end=end, bal=bal, fitid=fitid))
        return p

    def test_chain_and_gap(self):
        eng = VerificationEngine()
        eng.add(self.ofx("jan.ofx", "20260101", "20260131", "100.00", "J1"))
        eng.add(self.ofx("mar.ofx", "20260301", "20260331", "80.00", "M1"))
        rules = {(c.rule, c.status) for c in eng.run().checks}
        self.assertIn(("statement.chain", MISMATCH), rules)     # opens 90, Jan closed 100
        self.assertIn(("coverage.gap", INCOMPLETE), rules)
        self.assertIn(("statement.balance", UNVERIFIABLE), rules)

    def test_exports_keep_their_identifiers(self):
        eng = VerificationEngine()
        eng.add(os.path.join(X, "checking.ofx"))
        eng.add(os.path.join(X, "camt053.xml"))
        recs = eng.run().records
        self.assertEqual(recs[0].source.locator["fitid"], "A1")
        self.assertEqual(recs[0].source.raw, {"TRNAMT": "-42.10"})
        camt = [r for r in recs if r.input_id == "in2"]
        self.assertTrue(all("entry" in r.source.locator for r in camt))
        self.assertTrue(all(r.currency == "EUR" for r in camt))

    def test_a_broken_ledger_balance_is_a_mismatch(self):
        p = self.tmp_path("ledger.csv")
        with open(p, "w") as fh:
            fh.write("Date,Description,Amount,Balance\n"
                     "03/01/2026,A,100.00,100.00\n03/02/2026,B,-20.00,90.00\n")
        eng = VerificationEngine()
        eng.add(p, role="ledger")
        rules = {c.rule: c.status for c in eng.run().checks}
        self.assertEqual(rules["ledger.running_balance"], MISMATCH)

    def test_overlapping_downloads_count_once(self):
        a = self.tmp_path("chase_business_checking_7788 (1).csv")
        shutil.copy(BANK, a)
        eng = VerificationEngine()
        eng.add(BANK, role="bank")
        eng.add(a, role="bank")
        eng.add(LEDGER, role="ledger", account="Checking")
        rep = eng.run()
        self.assertEqual(rep.summary["duplicates"], 10)
        self.assertEqual([f.kind for f in rep.findings if f.severity == "info"], ["overlap"])
        self.assertEqual(sum(1 for f in rep.findings if f.kind == "missing_in_ledger"), 2)

    def test_a_general_ledger_needs_the_account_named(self):
        with self.assertRaisesRegex(ValueError, "several accounts"):
            VerificationEngine().add(LEDGER, role="ledger")


class StripeBalanceHistoryTest(support.TempDirMixin, unittest.TestCase):
    CSV = ("id,Type,Source,Amount,Fee,Net,Currency,Created (UTC),Available On (UTC),"
           "Description,Transfer\n"
           "txn_1,charge,ch_1,100.00,3.20,96.80,usd,2026-05-01 10:00,2026-05-03 00:00,A,po_77\n"
           "txn_2,charge,ch_2,50.00,1.75,48.25,usd,2026-05-02 10:00,2026-05-04 00:00,B,po_77\n"
           "txn_3,payout,po_77,{payout},0.00,{payout},usd,2026-05-05 00:00,2026-05-05 00:00,"
           "STRIPE PAYOUT,\n")

    def read(self, payout):
        p = self.tmp_path("balance_history.csv")
        with open(p, "w") as fh:
            fh.write(self.CSV.format(payout=payout))
        eng = VerificationEngine()
        eng.add(p)
        return eng.run()

    def test_payout_row_against_its_items(self):
        rep = self.read("-145.05")
        [po] = [r for r in rep.records if r.kind == "payout"]
        self.assertEqual((po.amount, po.refs["payout"]), (Decimal("145.05"), "po_77"))
        [c] = [c for c in rep.checks if c.rule == "processor.payout_total"]
        self.assertEqual(c.status, VERIFIED)

    def test_a_payout_that_does_not_add_up(self):
        [c] = [c for c in self.read("-150.00").checks if c.rule == "processor.payout_total"]
        self.assertEqual((c.status, c.difference), (MISMATCH, Decimal("-4.95")))


class OutputTest(support.TempDirMixin, unittest.TestCase):
    def test_envelope_and_sarif(self):
        rep = scenario().run()
        doc = rep.to_dict()
        self.assertEqual(doc["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(doc["subject"][0]["digest"]["sha256"], rep.content_digest)
        self.assertTrue(all(isinstance(r["amount"], str) for r in doc["predicate"]["records"]))
        sarif = rep.to_sarif()
        self.assertEqual(sarif["version"], "2.1.0")
        kinds = {r["kind"] for r in sarif["runs"][0]["results"]}
        self.assertEqual(kinds, {"pass", "open", "review"})   # open = coverage.pair

    def test_cli_exit_codes(self):
        from finances.verify import cli
        out = self.tmp_path("ev.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_reconcile(
                ["--bank", BANK, "--processor", STRIPE, "--ledger", LEDGER,
                 "--ledger-account", "Checking", "--json", out]), 1)
            self.assertEqual(cli.cmd_verify([os.path.join(X, "camt053.xml")]), 0)
            self.assertEqual(cli.cmd_evidence(["check", out, "--inputs", F, "--rerun"]), 0)
            self.assertEqual(cli.cmd_reconcile(["--bank", BANK]), 2)


if __name__ == "__main__":
    unittest.main()
