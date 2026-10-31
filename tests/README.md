# tests

The safety net for a 6,900-line toolchain that had none. Stdlib `unittest`
only — `pytest` is not a dependency of this project and must not become one.

## Running

```
.venv/bin/python -m unittest discover tests -v     # the canonical invocation
.venv/bin/python tests/run.py                      # same thing, quieter
.venv/bin/python tests/run.py -v                   # name every test
.venv/bin/python tests/run.py -k Recurring         # one slice
.venv/bin/python tests/run.py --update-golden      # re-record parse.py output
.venv/bin/python tests/run.py --check-golden       # diff goldens, exit 1 on drift
```

Use `.venv/bin/python`, not bare `python`. Nothing here writes to `finances.db`
or to anything outside a temp directory; the only files the suite ever writes
are `tests/golden/*.json`, and only when you ask for it.

`tests/golden/` is gitignored, because it summarises real statements. A fresh
clone therefore has no goldens; the golden tests skip with a "run
`--update-golden`" message rather than failing. Add a statement to `PDFs/`
without re-recording, though, and they do fail — that is a real gap. The whole
parser module skips if `PDFs/` or `pdfplumber` is missing.

## Layout

| file | what it covers |
| --- | --- |
| `support.py` | sys.path bootstrap, temp-dir mixin, `txn()` / `stmt()` fixture builders |
| `test_utils.py` | `parse_money`, `parse_date`, `merchant_key`, `payoff_months`, `future_value`, `detect_recurring`, file/CSV helpers |
| `test_models.py` | `Transaction` / `Statement` — `from_row`, `__post_init__` defaulting, `dedup_key`, `.d`, type coercion |
| `test_database.py` | round-trips, the primary-key fold, the `parsed_files` content-hash cache, the CSV bridge, and the `_migrate` path |
| `test_engine.py` | `spend`, `by_category`, `by_month`, `complete_months`, `monthly_average`, `surplus`, `coverage`, forecasts |
| `golden_harness.py` | records one JSON summary per PDF, and diffs two of them |
| `test_parser_golden.py` | the structural contract for `parse.py` plus the golden change detector |
| `golden/*.json` | one recorded summary per statement in `PDFs/` |

`models.py`, `database.py`, `utils.py` and `engine.py` are the stable core and
are tested hard, with known-answer arithmetic rather than "whatever it does
today". `parse.py`, `analyze.py`, `planner.py`, `financial_auditor.py`,
`categorizer.py`, `anonymize.py` and `llm_enrich.py` are being changed by other
people, so only their contracts are asserted.

## The golden files — read this before you trust them

**The goldens are a snapshot of known-wrong output and must be regenerated once
the parser fixes land.** They were recorded while `parse.py` was mid-repair,
against a parser that is documented to leak balance columns into amounts, flip
signs on Discover statements and mis-stamp the year on January statements.

So the golden tests are split deliberately:

* **Structural assertions really assert.** An issuer, a four-digit account, a
  `Statement` model, a reconciliation verdict in `{ok, mismatch, unchecked}`,
  non-negative charge and credit totals, ISO dates, a `direction` that matches
  the amount sign, provenance on every row. Those must hold no matter which
  column the parser decides is the amount, so breaking one is a real failure.
* **Numeric goldens only report.** `test_numeric_output_against_the_recorded_goldens`
  prints what moved and passes. Making it fail would turn the suite into a
  tripwire that fires on every commit of the work currently in progress, and a
  tripwire everyone ignores protects nothing. When you want drift to be an
  error — in CI, or once the parser settles — run `tests/run.py --check-golden`.

After the parser fixes land:

1. `.venv/bin/python tests/run.py --check-golden` and read the diff.
2. Confirm the new numbers are better, not merely different. The reconciliation
   verdict per file is the cheapest check: more `ok`, fewer `mismatch`.
3. `.venv/bin/python tests/run.py --update-golden` and commit the JSON.
4. Re-check the `expectedFailure` tests below — the ones that now pass should be
   promoted to ordinary tests.

`PeriodBoundsTests` guards itself with `skipTest` if `parse.py` ever stops
populating `Statement.period_start` / `period_end`, so the file survives that
field appearing and disappearing without needing an edit.

The whole parser module skips cleanly via `unittest.skipUnless` when
`pdfplumber` is not importable or `PDFs/` is absent.

## Bugs found, shipped as `expectedFailure`

Every one of these is a real defect confirmed against the code or the real
statements, not an aspiration. They pass today (as expected failures); when a
fix lands, unittest reports an *unexpected success*, which is the signal to
delete the decorator. Each test's docstring carries the full detail.

**Core (owned by the orchestrator — reported, not fixed):**

1. `test_utils.test_BUG_interior_minus_after_currency_symbol_loses_the_sign` —
   `parse_money("$-45.00")` returns `+45.00`. The sign is decided from the first
   and last characters of the raw string, so a minus printed after the `$` is
   stripped. `"-$45.00"` parses correctly, so this is an asymmetry, not a
   documented limitation.
2. `test_database.test_BUG_a_store_predating_the_source_file_column_cannot_be_opened` —
   `FinanceDB._create_schema` raises `no such column: source_file`. The
   `CREATE INDEX ... ON transactions(source_file)` statement is in the same
   `executescript` as the `CREATE TABLE IF NOT EXISTS` and runs *before*
   `_migrate` can add the column, so the migration path only works for columns
   no index touches. Fix: migrate first, then create the indexes.
3. `test_database.test_BUG_migrated_string_columns_read_back_as_none` —
   `Transaction.from_row` uses `row.get("issuer", "")`, which only defaults when
   the *key* is absent. After a migration the key exists with a NULL value, so
   `issuer`, `post_date`, `account_last4`, `statement_period` and `source_file`
   come back as `None`. Fix: `(row.get("issuer") or "")`.
4. `test_parser_golden.test_BUG_repeat_charges_collapse_on_the_dedup_key` —
   85 real transactions across 6 statements are silently lost. The primary key
   `(date, description, amount, account_last4)` cannot tell "the same statement
   parsed twice" from "two identical $3.00 PATH taps on the same day". Worst
   case is one Chase checking statement, where 41 of 181 rows vanish. Fixing it means
   changing `models.Transaction.dedup_key` and `database._TXN_KEY` together.

All four are in the shared core, which this suite does not own. They are
reported here, not fixed.

### Fixed while this suite was being written

Three structural assertions started life as `expectedFailure`s against defects
this suite found, and have since been promoted to ordinary tests because the
parser work landed. They stay as regression guards:

* `test_every_statement_produces_a_period` — 20 of the sample statements used to
  return an empty `statement_period`, and the 5 Amex files returned a bare
  closing date rather than a range. Without it there are no period bounds, so
  `engine.coverage()` could not find gaps between billing cycles.
* `test_a_printed_purchases_total_yields_extracted_charges` — six statements
  printed a purchases total and extracted zero charges: five Discover files
  whose rows all came out negative, plus an April Venture statement that
  extracted nothing at all.
* `test_transaction_dates_stay_inside_the_billing_cycle` — the year-rollover
  defect. A January Chase checking statement covering early December to early
  January dated 38 of its 45 rows in the *following* December, and a January
  Chase card statement did the same for 24 of 26, each spanning ~361 days rather than one cycle. `by_month()` grew a
  phantom future-December bucket and `monthly_average()` divided by roughly twelve
  months instead of one.

## Known limitations recorded, not asserted

* `parse_money("1.234,56")` returns `1.23456`. European decimal-comma input is
  not a shape these US statements produce, so the test only checks it degrades
  to a number instead of raising.
* `parse_money("$1,234.56 CR")` returns a positive number. If issuers start
  printing `CR` suffixes, that needs handling.
* `Transaction` treats `amount == 0` as direction `in`, because `spend()`
  filters on `amount > 0`. Deterministic, but worth knowing.

## Adding tests

Import `support` first — it puts the repo root on `sys.path` — then import the
module under test the way the real tools do (`from utils import ...`, not
`from finances.utils import ...`). Use `support.TempDirMixin` for anything that
touches the filesystem, and `support.txn()` / `support.stmt()` to build rows so
fixtures stay readable. Prefer arithmetic you can check by hand in a comment
over a number copied out of a previous run.
