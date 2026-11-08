"""
The per-bank accuracy table: counts only, from the reconciliation verdicts.
"""

from __future__ import annotations

import unittest

from finances import accuracy
from finances.models import Statement


def st(issuer, reconciled, fmt="pdf", **kw):
    return Statement(source_file=f"{issuer}{reconciled}{fmt}.pdf", issuer=issuer,
                     reconciled=reconciled, source_format=fmt, **kw)


class AccuracyTest(unittest.TestCase):
    def test_counts_first_try_fallback_and_misses(self):
        rows = accuracy.table([
            st("Chase", 1), st("Chase", 1), st("Chase", 1, "pdf+llm"), st("Chase", 0),
            st("Amex", 1), st("Amex", None),
            st("Fidelity", None, account_type="investment"),   # not a reconciliation
            st("Ally", None, "ofx"),                          # not a PDF
        ])
        self.assertEqual([(r["bank"], r["statements"], r["first"], r["fallback"],
                           r["mismatch"], r["unchecked"], r["firstPct"]) for r in rows],
                         [("Chase", 4, 2, 1, 1, 0, 50.0), ("Amex", 2, 1, 0, 0, 1, 100.0)])
        table = accuracy.markdown(rows)
        self.assertIn("| Chase | 4 | 2 (50%) | 1 | 1 | 0 |", table)
        self.assertIn("| **All** | 6 | 3 (60%)", table)


if __name__ == "__main__":
    unittest.main()
