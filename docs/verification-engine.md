# The verification engine: research, decisions, plan

`finances.verify` is the trust layer underneath the personal-finance app:
ingest financial records from any source, normalize them without losing the
original, check everything that can be independently checked, match records
across sources, and emit a machine-readable evidence report that other
software — including AI agents — can inspect and re-check.

Normalization answers *what does this record mean in a common format?*
Verification answers *what evidence supports that it is represented correctly?*
The engine does both, and keeps them apart.

This document records what the research found, the decisions taken because of
it, and what is built versus planned. Research was done on 2026-11-07.

---

## 1. What the research says

### Matching: the literature

The academic literature on bank reconciliation specifically is thin; most of
the useful theory comes from record linkage.

| Source | Finding | Decision it drives |
|---|---|---|
| Fellegi & Sunter, *A Theory for Record Linkage*, JASA 1969 | The optimal linkage rule has three outcomes — link, possible link (clerical review), non-link — and minimises the review region for stipulated false-match and false-non-match rates. | Three outcomes, not two: `matched`, `needs_review`, `unmatched`. Thresholds come from tolerated error, not a hand-picked score. |
| Sadinle, *Bayesian Estimation of Bipartite Matchings*, JASA 2017 | Model the bipartite matching, not independent pair decisions; leave uncertain parts deliberately unresolved. | An explicit `ambiguous` decision when equally good, distinguishable candidates compete. Never auto-pick a tie. |
| Jaro, JASA 1989 (via Winkler 1994) | Greedy pairwise matching produced bad assignments; enforcing one-to-one by linear sum assignment fixed it. | Resolve competing claims globally inside each block (an exact DP), never greedily. |
| Christen, *Survey of Indexing for Record Linkage*, IEEE TKDE 2012 | Blocking decides both cost and recall; a true match blocked apart is invisible. | Blocks (account × currency × exact amount × date window) are logged on every match so missed matches are auditable. |
| Czekaj et al., *Entity matching for automated settlement of receivables*, 2021 (arXiv:2205.10678) | Production systems operate at ~99% precision and maximise recall from there. | Deterministic tiers first; any scored or AI-suggested tier is advisory and lands in `needs_review`. |
| Hand & Christen, *Statistics and Computing* 2018 | F-measure is a misleading metric for linkage. | Evaluate with false-match rate and review workload, not F1. |
| Wu et al. (J.P. Morgan AI Research), *The Subset Sum Matching Problem*, ECAI 2025 (arXiv:2508.19218) | Many-to-one reconciliation is subset-sum: NP-complete; DP on integers is fast but search becomes impractical around 30 items, and solvers cannot prove "no match" for ≥20 items in 90 s. | Grouped matches use DP on integer minor units inside a small block, with a cap on group size and on work done. A search that runs out of budget is reported as `unresolved`, never as "no match". |
| Munoz, Jalili & Tafakori, J. Finance & Data Science 2025 | Industry tools are good at one-to-one and weak at one-to-many. | One-to-many is its own tier, with processor semantics (payout = Σ net) rather than a wider tolerance. |
| Kang & Liu 2023; Islam et al. (FinanceBench) 2023; Zhu et al. (TAT-QA) 2021 | LLMs hallucinate financial facts and are weak at numeric reasoning over tables. | Arithmetic is always deterministic. Model output is a proposal that must pass the same invariants (the existing `--llm` fallback already works this way). |
| Goldberg 1991; Cowlishaw 2003 | Binary floating point mis-rounds money (1.05 × 0.70 → $0.73). | `Decimal` and integer minor units inside the engine; amounts are decimal **strings** in JSON. |

### Evidence: standards worth borrowing

| Standard | What is borrowed |
|---|---|
| W3C PROV-DM (2013) | Entities (files, record sets, results), activities (parse, check, match), agents (engine version, reviewer). Overrides are new entities derived from the old, never edits. |
| Buneman, Khanna & Tan, *Why and Where*, ICDT 2001 | Every record keeps *where* (file hash, page/row/FITID); every match keeps *why* (the records and the rule that fired). |
| in-toto Statement v1 / SLSA provenance | The report envelope: `subject` with sha256 digests, a `predicateType`, inputs as resolved dependencies with digests, the engine as builder with a version. |
| RFC 8785 (JCS) | Canonical JSON for the digest: sorted keys, no whitespace, no floats (money as strings). |
| SARIF 2.1.0 | Result kinds map onto check statuses (`pass`, `fail`, `open`, `review`); exceptions carry physical locations; there is an exporter. |
| ISA 500 / PCAOB AS 1105 | Evidence from independent external sources is more reliable; company-produced data must be tested for completeness and accuracy. Inputs are classed by source; coverage gaps are reported as their own result. |
| ISA 230 | Documentation must let an experienced stranger understand what was done: every check and decision carries rule, inputs, expected and computed values. |
| ISO 4217 | Minor-unit exponents per currency (JPY 0, USD 2, KWD 3). |

### The market

| Product | Strength | What it leaves open |
|---|---|---|
| Plaid | Bank connectivity; pending→posted is *remove + add* with `pending_transaction_id`. | No verification against source statements. |
| Stripe | Balance transactions (`gross − fee = net`) and the itemized payout reconciliation report. | Matching payouts to bank deposits is left to the user (explicitly so for manual and instant payouts). |
| Rutter / Codat | Payout and bank-feed data pipes into accounting systems. | They do not do the matching; the integrator must. |
| bankstatementparser (OSS, Python) | Multi-format statements, `VERIFIED/DISCREPANCY/UNVERIFIABLE` balance check, row hashes. | Statement-centric; no processor or ledger model; hash includes the description. |
| Reconify (OSS, Go) | Generic matching, subset-sum, audit metadata. | No statement balances, no Stripe/OFX/camt semantics; Go not Python. |
| Actual, beancount-import | Ordered import matching (ID, then amount within a window). | Personal use; no evidence output. |

APQC (via CFO.com, May 2026) puts reconciling a single bank account at about
four hours at the median. Most other figures in circulation are vendor surveys
and are not repeated here.

**The opening:** one model across bank, processor and ledger; payout-to-bank
matching with real processor semantics; per-match machine-readable evidence;
statement balance proofs; a permissive, local-first Python library.

---

## 2. Decisions

1. **A separate package, not a rewrite.** `finances.verify` wraps the existing
   parsers through adapters. The app keeps working; it becomes one consumer.
2. **Canonical record, original preserved.** Amounts are `Decimal`, signed from
   the account holder's side (money in positive, money out negative — the ISO
   20022 / bank-export convention, *not* the app's spending convention).
   Every record carries a `SourceRef`: input digest, adapter, location (page and
   line, CSV row, OFX FITID, camt `AcctSvcrRef`/`EndToEndId`), the amount as
   written, and whether the sign was inverted.
3. **Two identities.** `content_key` = hash of (account, date, amount,
   currency) for matching — descriptions are deliberately excluded because they
   differ across sources. `id` = hash of (input digest, content key,
   occurrence) so two identical same-day coffees stay two records and a re-run
   reproduces the same ids.
4. **Five check statuses, never one score.** `verified`, `mismatch`,
   `unverifiable`, `needs_review`, `incomplete`, each with the rule, the
   expected and computed values, the difference, the tolerance and the evidence.
   A verified balance says nothing about categories, and the report never
   pretends otherwise.
5. **A matching cascade.** (1) shared reference, (2) exact amount within a date
   window, resolved one-to-one by DP per block, (3) grouped settlement by
   bounded subset-sum on minor units, (4) explained differences — a near match
   whose difference equals a known component (processor fees) becomes
   `needs_review` with the explanation, never `matched`. Leftovers are
   exceptions on the side they are missing from.
6. **Zero tolerance by default.** Tolerances are explicit, in minor units, and
   recorded on every decision.
7. **Deterministic and reproducible.** Stable ordering everywhere; the report's
   `content_digest` covers everything except wall-clock times, so the same
   inputs, engine version and options give the same digest.
8. **Reviewer decisions are provenance.** Accepting or rejecting a
   `needs_review` match is recorded as an override with who, when and why; the
   original decision stays in the report.
9. **Stdlib only.** Like the rest of the project, apart from pdfplumber for PDFs.

### What a hash does not prove

Input digests prove the retained evidence has not changed since the run. They
do not prove a file is authentic: a fabricated statement hashes as well as a
real one. Authenticity needs controls outside the engine (fetching directly
from the bank, signed statements). The report says so.

---

## 3. What is built (phase 1–2)

```python
from finances.verify import VerificationEngine

eng = VerificationEngine()
eng.add("bank.csv", role="bank")
eng.add("stripe_payouts.csv", role="processor")
eng.add("ledger.csv", role="ledger", account="Checking")

report = eng.run()                      # checks + bank↔processor + bank↔ledger
print(report.summary)                   # counts per status
report.save("evidence.json")            # versioned, digest-stamped
```

```bash
finances verify statement.pdf export.ofx            # statement checks only
finances reconcile --bank bank.csv --processor stripe.csv --ledger gl.csv \
                   --json evidence.json --sarif exceptions.sarif
finances evidence check evidence.json --inputs ./   # digest and input hashes
```

Adapters: statement PDFs, CSV, OFX/QFX, CAMT.053, MT940 (all through the
existing readers), Stripe itemized payout reconciliation CSV and balance
history CSV, and ledger CSVs (QuickBooks bank-import 3/4-column, QuickBooks
general-ledger detail, Xero, any CSV with date/amount or debit/credit).

Checks: statement totals and balances, running-balance continuity, balance
chain between consecutive statements, period coverage gaps, duplicate rows
across overlapping inputs, processor row arithmetic (gross − fee = net) and
payout totals.

## 4. Planned, in order

| Phase | Work | Exit criterion |
|---|---|---|
| 3 | REST service (same JSON), webhooks, a review UI in the dashboard, PROV-JSON and in-toto/DSSE signing export. | A third-party developer integrates without reading internals. |
| 4 | Pilot with consenting users on synthetic or anonymised data. Measure: false-match rate (target 0 auto-matched errors), share of records with independent evidence, review minutes per account-month vs the ~4 h APQC median, integration time. | Evidence the engine shortens a real workflow. |
| later | Plaid sync adapter (pending→posted as supersession), QuickBooks/Xero APIs, calibrated scored tier trained on reviewer decisions, hosted API. | Only after phase 4 shows value. |

Deliberately out of scope: bank connectivity, an accounting suite, payment
initiation, an AI agent framework.

## 5. Risks

- **Incomplete sources.** A CSV with no balances cannot prove completeness; it
  is reported `unverifiable`, not verified.
- **Legitimate differences.** Transfers, pending items, FX, split settlements
  and corrections cause real differences. The engine explains what it can and
  sends the rest to review rather than widening tolerances.
- **Hosted product security.** Encryption, tenant isolation, retention and
  access logging must be designed before any hosted version touches customer
  data. Not built here.
- **Competition.** Parsers are not a moat. Trust has to come from published
  accuracy across real cases, transparent evidence and a dependable interface.
- **Buyers may want a workflow, not an API.** Phase 4 interviews decide whether
  this is a developer product or an embedded feature.
