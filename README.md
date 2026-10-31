# finances

Personal finance analysis that reads your statement PDFs directly. No bank
login, no Plaid, no account linking, no cloud. Everything runs on your machine
and nothing is uploaded anywhere.

The thing that makes it different: **a statement prints its own totals, so the
extraction can be proved correct.** Every figure this produces traces back to a
line on a statement, and the tool tells you when it cannot.

```
$ python finances.py check

Store       finances.db
Rows        1180 transactions, 2025-01-03 to 2025-12-29
Statements  36

Reconciled  36/36 (100%)

Every statement reconciles and no months are missing. The totals downstream
are traceable to a statement line.
```

A tool built on a bank API cannot do this. It receives a feed of transactions
with no independent total to check them against, so a dropped charge or a
flipped sign is invisible. Here the statement is the ground truth, and
reconciliation is enforced rather than assumed.

---

# Running it

## 1. Install

Requires **Python 3.14+**. One dependency, `pdfplumber`, used by the PDF parser
alone — everything downstream is standard library.

```bash
git clone <this repo>
cd Finances

uv sync                      # recommended
# or:
python3 -m venv .venv && .venv/bin/pip install pdfplumber
```

## 2. Put your statements somewhere

Drop the PDFs your bank and card issuers give you into a folder. Any layout is
fine — subfolders are searched too.

```
PDFs/
  Amex-Jan_2025.pdf
  chase-checking-jan.pdf
  February Discover.pdf
  ...
```

Tested against Amex, Chase (card and checking), Capital One and Discover.

## 3. Run it

Every command is `python finances.py <command>`. Use `.venv/bin/python` if you
are not in an activated virtualenv.

```bash
.venv/bin/python finances.py pipeline PDFs/
```

That single command parses the PDFs, categorizes the transactions, builds the
dashboard, and prints the reconciliation report. It is the whole thing.

If you would rather run the steps yourself:

```bash
.venv/bin/python finances.py parse PDFs/        # PDFs      -> finances.db
.venv/bin/python finances.py categorize apply   # label the transactions
.venv/bin/python finances.py check              # do the numbers add up?
.venv/bin/python finances.py analyze            # -> finance_dashboard.html
```

**Always run `check` before trusting anything else.** If it does not say
100%, the numbers downstream are built on a bad extraction and every other
report will tell you so too.

## 4. Open the dashboard

```bash
cd web && npm install && npm run build && cd ..
.venv/bin/python finances.py web --open
```

A browser dashboard on `http://localhost:8787` — your statements, the
reconciliation verdict for each one, spending by month and category, detected
subscriptions, and a searchable transaction table. **You can drag new statement
PDFs straight onto it**: they are parsed on drop, and the reply tells you how
many transactions came out and whether the file reconciles against its own
printed totals.

The server binds to localhost only and makes no outbound requests.

While working on the frontend, run the two halves separately — `python api.py`
in one terminal, `cd web && npm run dev` in another, then use
`http://localhost:5173` (it proxies `/api` through).

## 5. Or stay in the terminal

```bash
.venv/bin/python finances.py analyze            # where the money went
.venv/bin/python finances.py cashflow           # the next 60 days
.venv/bin/python finances.py audit              # the reckoning
.venv/bin/python finances.py cpi                # your own inflation rate
.venv/bin/python finances.py backtest           # how accurate are the forecasts
.venv/bin/python finances.py ask "how much did I spend on coffee in March"
```

`python finances.py` on its own lists every command. `python finances.py
<command> --help` shows that tool's own options.

## Optional: the `finances` shortcut

To type `finances` instead of `python finances.py`:

```bash
uv sync                      # installs the console script
# or:
.venv/bin/pip install -e .
```

Then `finances check`, `finances analyze`, and so on. Every example below uses
the long form so it works whether or not you have done this.

---

# Command reference

| Command | What it does | Writes |
|---|---|---|
| `parse PDFs/` | extract transactions from statement PDFs | `finances.db` |
| `check` | reconciliation and coverage report | — |
| `categorize apply` | label transactions, write back to the store | `finances.db` |
| `analyze` | spending dashboard | `finance_dashboard.html`, `insights.md` |
| `cashflow` | daily balance projection with a low-point warning | `cashflow.html` |
| `plan status` | savings goals against your real surplus | `plan.json` |
| `audit` | a strict grandfather goes through your statements | `auditor_ledger.json` |
| `cpi` | personal inflation, per merchant and per category | `cpi.html` |
| `backtest` | grades the forecasts against what actually happened | `backtest.html` |
| `ask "..."` | plain-English questions, answered offline | — |
| `anonymize` | a copy safe to share | `transactions_shareable.csv` |
| `enrich` | local-LLM merchant cleanup | `finances.db` |
| `web` | serve the browser dashboard, with PDF drag-and-drop | — |
| `pipeline PDFs/` | parse + categorize + analyze in one run | all of the above |

Every command accepts either the SQLite store or a CSV, interchangeably:

```bash
.venv/bin/python finances.py analyze --db finances.db
.venv/bin/python finances.py analyze cleanedData/transactions.csv
```

## Things you will probably need

```bash
# Encrypted PDFs
.venv/bin/python finances.py parse PDFs/ --password 1234

# A statement extracted wrong — see what the parser saw
.venv/bin/python parse.py "PDFs/one bad file.pdf" --dump-columns

# Fail the build if anything does not reconcile (for a cron job)
.venv/bin/python finances.py check --strict

# Re-parse everything, ignoring the cache
.venv/bin/python finances.py parse PDFs/ --force

# Tell it your take-home pay, so it can work out what is left over
.venv/bin/python finances.py analyze --income 6200

# Teach the categorizer, biggest money first
.venv/bin/python finances.py categorize review
```

## Asking questions offline

`ask` needs a local model server. Without one, `--sql` and `--examples` still
work:

```bash
.venv/bin/python finances.py ask --examples
.venv/bin/python finances.py ask --sql "SELECT category, SUM(amount) FROM transactions GROUP BY 1 ORDER BY 2 DESC"
```

With one:

```bash
brew install ollama && ollama pull llama3.1:8b
.venv/bin/python finances.py ask "what did I spend on delivery last month" --explain
```

Something in the 3B–8B range is plenty. Avoid reasoning models here — they take
minutes per question for no benefit on this task. Pick explicitly with
`--model llama3.1:8b`.

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `finances.db does not exist` | Run `parse` first. |
| `check` reports mismatches | The extraction is wrong for those files. Run `parse.py <file> --dump-columns` to see the columns it found. |
| `check` reports gaps | A statement month is missing from your folder. Totals are being computed on incomplete data. |
| Lots of `Uncategorized` | `categorize review` to teach it, or `enrich` to use a local LLM. |
| `cashflow` will not project | It could not find a checking-account balance. Pass `--balance 2400`. |
| Amounts look wrong | Almost always a column-detection problem. `--dump-columns`, then `--amount-col last` or `second-last` to override. |

---

# How it fits together

```
PDFs ──▶ parse.py ──▶ finances.db ──▶ engine.py ──▶ every other tool
              │                          │
       reconciles each              the read model: what counts as
       statement against            spending, which months are complete,
       its printed totals           what repeats, what is verified
```

- **`models.py`** — one definition of a transaction and a statement. The SQLite
  schema and the CSV columns are both generated from it, so adding a field is a
  single edit and the store migrates itself.
- **`database.py`** — the store, plus a content-hash cache so re-running the
  parser skips files that have not changed.
- **`utils.py`** — money, date and merchant parsing, the debt formulas, and the
  recurring-charge detector.
- **`engine.py`** — the semantics. Everything downstream agrees about what a
  month, a surplus and a subscription are because they all ask this.

# What it does that other tools do not

**Proves its own numbers.** Extracted charges are checked against the total the
statement prints for itself, and the verdict is stored, not just displayed.
Missing statement months are detected and reported as gaps, so an average is
never quietly computed over incomplete data.

This is not decorative. Building it found a noise filter matching the month
"Apr" and discarding entire statements, sign inversions on two issuers, a
year-rollover that dated 62 transactions to the wrong December, and a primary
key that silently dropped 41 of one statement's 181 transactions. All of them
looked fine in a dashboard. None survive a reconciliation check.

**Tells you about next month, not just last month.** `cashflow` projects the
daily balance forward from known recurring charges and detected paycheck
cadence — distinguishing semi-monthly from biweekly, because biweekly gives you
three-paycheck months and that changes the answer. It reports a band rather than
a line and says plainly that it is not a confidence interval.

**Measures your inflation, not the country's.** `cpi` weights per-category price
changes by your spending and tracks per-merchant prices over time, against real
BLS Consumer Expenditure and CPI-U reference data shipped with the project.

**Grades its own forecasts.** `backtest` walks forward month by month, predicts
each month using only data from before it, and scores nine predictors against
what happened. It refuses to name a winner when the sample cannot support one.

**Makes your data shareable.** `anonymize` has four levels up to fully
synthetic, and proves what survived — per-category totals are printed side by
side so you can see which figures are still exactly true.

# Categorization

Rules, in order, first hit wins. Every answer records which rule fired and how
confident it is:

1. what you taught it (`learned_rules.json`, always wins)
2. context rules — `COSTCO`+`GAS` is fuel, `UBER`+`EATS` is delivery
3. bank phrases — a deposit account names its own transaction type
4. brand table, exact token match, never substring
5. keyword rules, word-boundary regex
6. issuer MCC labels, where the statement prints its own category
7. behaviour — cadence and amount, when the name says nothing

Scored against 159 real-shaped descriptors with processor prefixes, glued
city/state suffixes and issuer preambles:

```
$ .venv/bin/python finances.py categorize eval
  correct         159  100.0%
  wrong             0    0.0%   <- these corrupt analysis
  left unknown      0    0.0%   <- these are honest
```

# Tests

```bash
.venv/bin/python -m unittest discover tests
```

238 tests, no test dependencies. The golden-file harness records per-statement
extraction totals so a parser change that moves a number is visible:

```bash
.venv/bin/python tests/run.py --update-golden   # after a deliberate change
.venv/bin/python tests/run.py --check-golden    # exits 1 on drift
```

# Privacy

Your statements and the store never leave the machine. `PDFs/`, `*.db`, the
generated dashboards and `anonymize_map.json` are all gitignored. The map file
is what turns `MERCHANT_007` back into a real name — keep it, do not share it.

`ask` and `enrich` talk only to a model server on localhost. Nothing else in
the project makes a network request at all.

# Not financial advice

Arithmetic on your own statements. The forecasts are extrapolations of your past
spending and say so, `backtest` will tell you how wrong they have been, and none
of it knows anything about your life beyond what the statements print.
