# Changelog

## [0.4.0] - unreleased

### Added
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
