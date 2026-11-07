# Changelog

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
