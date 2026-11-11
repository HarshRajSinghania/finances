# Changelog

## [0.6.0] - 2026-11-11

### Added
- `finances report-bank statement.pdf`: the statement's layout as plain text,
  every line at its own position, with names, merchants, amounts and account
  numbers scrambled (the same way each time a value repeats). Dates and the
  statement's own headings stay, and the header says what the parser read:
  the bank, how many transactions, and whether they reconciled. The layout
  turns back into a PDF the parser reads exactly like the original, so it is
  what to attach to a "Support a bank" issue. `--check` parses a layout file
  and says whether it reconciles.
- One file per bank in `finances/banks/`: how its statements are recognised,
  what is unusual about them, and where to download them. Adding a bank is a
  new file instead of an edit to the parser.
- Made-up statements for Amex, Capital One, Chase (card and checking) and
  Discover in `tests/fixtures/banks/`, each exercising that bank's quirks. The
  suite renders each to a PDF and requires it to be read as its bank and to
  reconcile.
- A live demo of the dashboard on made-up data, with nothing to install:
  https://finances-demo-five.vercel.app (built by `scripts/demo_site.py`).
- CONTRIBUTING.md, a code of conduct, issue and pull request templates, and
  tests on every pull request.

### Changed
- The README opens with the demo and a per-bank accuracy table.
- `banks.json` is replaced by the `[guide]` table in each bank's file.

## [0.5.0] - 2026-11-08

### Added
- Ask answers are checked: every amount in an answer is looked for in what the
  model's lookups returned. A charge links to the statement page it is printed
  on, totals and differences say where they came from, and an amount that is
  not in the data is marked. Each answer carries a Verified / Unverified badge.
  A local model is now the default provider.
- Each transaction keeps the page and line it was read from.
- Net worth over time, from the balance each statement closed on, with
  brokerage statements read for their account value (not their trades).
- Bulk recategorization on the transactions page, optionally remembered for
  those merchants.
- Keeping up to date without a bank login: a watched Downloads folder
  (`finances watch`, or a switch on the Statements page), when each account's
  next statement should be out with a link to the bank's statements page and
  steps for 24 banks, and an Overview reminder when one is waiting.
- Optional SimpleFIN live feed (`finances feed`): this month's charges before
  the statement, replaced and confirmed by the statement when it arrives.
- Scanned PDFs are read through OCR (`ocrmypdf`) and held to the same
  reconciliation; unconfirmed rows say they came from a scan.
- `finances accuracy`: per bank, how many statements reconciled on the first try.
- Amazon order history and receipt CSVs: the items behind each charge.
- A read-only phone view over the home network, behind a QR-code link.
- For bookkeepers: `finances client` (a store per client, folder intake,
  workpapers with SHA-256 and sign-off columns) and `finances web --clients`.
- `FINANCES_ALLOWED_HOSTS` for reaching the dashboard by another name on your
  own network (a home server's `umbrel.local`).
- Packaging drafts: a Homebrew formula, a Docker image workflow for ghcr.io,
  Umbrel and CasaOS app manifests, and an MCP registry `server.json`.
- View the original statement from the dashboard: each statement's file name,
  a view icon on its row, and a "View statement" button in its details open the
  PDF or export it was read from, in a new tab. Only files the store has a
  statement for are served; exports are shown as plain text.
- Import from Mint, YNAB, Monarch, Copilot and Tiller: their CSV exports are
  recognised by their columns, split into one account per account named, signed
  by each app's convention, and keep the app's own labels for pay, transfers,
  refunds and card payments. Imported as unverified, since they carry no balances.
  They can be dropped on the dashboard's upload box like any bank export.
- `finances export xlsx`: an Excel workbook (transactions, months, categories,
  subscriptions, statements with their check results), written with the
  standard library. The dashboard downloads it from the Excel button in the header.
- Card payment due dates: read from each card statement ("Payment Due Date"),
  and a Card payments panel on the overview with the statement balance, the
  minimum, what has been paid since the statement closed, and whether it is due,
  paid or missed. Statements added before this get theirs from a "Read due dates"
  button on that panel, which fills in only the due date. Statement details show
  it too.

### Changed
- First run: `finances` on its own starts with how to begin. Commands that only
  read no longer leave an empty `finances.db` behind when there is none; they
  say how to get started instead. A mistyped command suggests the right one.

## [0.4.1] - 2026-11-07

### Added
- Ask works with a local model: Ollama, LM Studio, llama.cpp or vLLM, found
  automatically or named by address (only on this machine or network). No key,
  nothing leaves the network, and models without tool support are given the
  main figures up front.

### Changed
- Ask keeps the conversation: answered questions are saved with the data and
  are there after leaving the page or restarting. A delete button (with a
  confirmation) removes them. Only the last 20 messages go to the provider with
  each question.

## [0.4.0] - 2026-11-07

### Added
- `finances.verify`, a verification engine usable from Python, plus three
  commands: `finances verify`, `finances reconcile` and `finances evidence
  check`.
  - **Inputs:** statement PDFs, CSV, OFX, CAMT.053, MT940, Stripe payout
    reconciliation and balance exports, and ledger CSVs (QuickBooks, Xero or
    generic). Records are normalized to exact decimals and keep their source
    location and raw values.
  - **Checks:** statement balances, running balances, balance chains, coverage
    gaps and overlapping downloads.
  - **Matching:** cross-source matching in tiers (reference, exact, grouped,
    near-miss for review), with fee-explained differences.
  - **Output:** a reproducible evidence report (in-toto envelope, W3C PROV
    provenance, SARIF export) that can be re-checked and re-run. See
    docs/verification-engine.md.
- Readers now record where each row came from: CSV row, OFX FITID, CAMT
  entry and references, MT940 entry, PDF page and line.
- `finances mcp`: an MCP server, so Claude Desktop, Claude Code, Cursor and other
  assistants can answer questions from the reconciled store. Twelve read-only
  tools (summary, spending by category, monthly totals, transaction search,
  subscriptions, cancel list, cash-flow forecast, check, anomalies, budgets,
  Wrapped and a guarded SQL query). Standard library only.
- Dashboard **Ask** page: questions in plain English, answered with your own
  Claude or OpenAI API key. The model uses the same read-only tools as the MCP
  server, and each answer shows which ones it called. The key is kept in the
  browser (for the tab, or on the device if you choose), and the server only
  passes it on. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` work as a server-side
  fallback.

## [0.3.1] - 2026-11-06

### Changed
- README: install from PyPI, and absolute links so images and links work on
  the PyPI page.

## [0.3.0] - 2026-11-06

### Added
- `finances import`: bank exports from any bank — CSV (column, date-format,
  decimal-separator and sign detection, headers in several languages), OFX/QFX/QBO,
  CAMT.053 and MT940. Each file is checked against the balances it carries;
  OFX exports are chained across files. Overlapping downloads are not double counted.
- `finances parse --llm`: a local model reads statement layouts the parser does
  not know. Its rows are kept only if they reconcile with the statement's
  printed totals.
- `finances export`: beancount, hledger/ledger (with optional balance
  assertions), YNAB and Actual (one CSV per account), and plain CSV.
- `finances wrapped`: your year in money, also a dashboard page.
- `finances budget`: monthly limits per category with month-end projection, and
  a cancel list for subscriptions with keep / cancelled marks.
- Dashboard: Wrapped, Budgets & goals, a "Cancel these" section, and drag-and-drop
  for bank exports.
- Multi-currency: transactions keep their currency; `finances fx` sets the base
  currency and rates. Rows without a rate are held out of totals and reported.
- European merchant and salary/rent keywords in the categorizer.
- Published to PyPI with the dashboard bundled: `uvx finances demo`.

### Changed
- Dashboard uploads are saved to `./PDFs` in the working directory.

## [0.2.0]

- Easy setup with uv and Docker, `finances demo`, the `finances/` package layout.
