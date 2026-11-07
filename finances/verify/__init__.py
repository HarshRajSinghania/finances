"""
finances.verify — verified financial data, with the evidence attached.

Ingest bank statements and exports, payment-processor payouts and accounting
ledgers; normalize them into one model without losing the original; check what
can be independently checked; match records across sources; and write an
evidence report another program can inspect and re-check.

    from finances.verify import VerificationEngine

    eng = VerificationEngine()
    eng.add("bank.csv", role="bank")
    eng.add("stripe_payouts.csv")
    eng.add("ledger.csv", role="ledger")
    report = eng.run()
    print(report.text())
    report.save("evidence.json")

See docs/verification-engine.md for the research behind the design.
"""

from finances.verify.engine import VerificationEngine
from finances.verify.evidence import check_report
from finances.verify.model import (AMBIGUOUS, INCOMPLETE, MATCHED, MISMATCH, NEEDS_REVIEW,
                                   REJECTED, UNRESOLVED, UNVERIFIABLE, VERIFIED, Check,
                                   Finding, Input, Match, Record)
from finances.verify.report import Report

__all__ = ["VerificationEngine", "Report", "check_report", "Record", "Input", "Check",
           "Match", "Finding", "VERIFIED", "MISMATCH", "UNVERIFIABLE", "NEEDS_REVIEW",
           "INCOMPLETE", "MATCHED", "AMBIGUOUS", "UNRESOLVED", "REJECTED"]
