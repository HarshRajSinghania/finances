# Contributing

Thanks for helping. The most useful thing anyone can add is **a bank**: every
bank whose statements read correctly is a bank whose customers can use this.
That is also the easiest way in, and it never needs anyone's real statement.

Questions go in [Discussions](https://github.com/roshanlam/finances/discussions);
issues are for bugs and banks.

## The one rule: no real financial data, ever

Not in an issue, a pull request, a fixture, a screenshot or a log. That covers
statements, exports, the store (`*.db`), account numbers, names and amounts,
even "anonymized" ones. Every fixture in this repository is invented by hand
and says so in its header. If you are not sure whether something is personal,
leave it out and ask.

## Set up (about 5 minutes)

You need [uv](https://docs.astral.sh/uv/). Node 20+ only if you touch the
dashboard.

```bash
git clone https://github.com/roshanlam/finances && cd finances
uv sync                                       # .venv with the project installed, editable
uv run python tests/run.py                    # the whole suite, about 20 seconds
uv run finances demo && uv run finances web --db demo.db --open
```

The tests are plain `unittest`, with no test dependencies; please keep it that
way. `tests/README.md` explains the layout.

Dashboard work: run `uv run finances web --db demo.db` in one terminal and
`cd web && npm ci && npm run dev` in another, then open `http://localhost:5173`.
`npm run build` type-checks.

## Add a bank

A bank is one small file, `finances/banks/<bank>.toml`, plus a made-up
statement in `tests/fixtures/banks/<bank>.txt` that proves the parser reads it.

### 1. See what the parser makes of your statement

```bash
finances report-bank ~/Downloads/statement.pdf
```

This writes `statement-layout.txt`: every line of the statement at its own
position, with names, merchants, amounts and account numbers replaced by random
ones. Its header says what the parser read: the bank, how many transactions,
and whether they added up to the statement's own totals. If it reconciles, your
bank already works and there is nothing to do except, perhaps, step 2.

If it does not, either open a
[Support a bank](https://github.com/roshanlam/finances/issues/new?template=new-bank.yml)
issue with that file attached (read it first: it is plain text), or carry on.

### 2. Describe the bank

Copy a short file such as `finances/banks/citi.toml`:

```toml
name = "My Bank"
statements = 'mybank\.com|my bank n\.a\.'   # lowercase regex, searched in the statement's text
notes = ["anything unusual about the layout"]

[guide]                                     # optional: how to download statements
url = "https://www.mybank.com/statements"
steps = ["Sign in", "Open Statements", "Download the PDF"]
exports = ["PDF", "CSV"]
```

`statements` should match something only this bank prints: its web address or
legal name, not a word that could appear in a transaction.

### 3. Write a made-up statement for it

Fixtures are layouts like the one `report-bank` writes, but invented. Start
from your layout file if you like, then make it yours to publish:

- keep the structure (headings, column positions, the summary box);
- replace every date, name, merchant and amount with invented ones, and keep
  only a handful of transactions;
- make the totals add up: the summary's purchases and payments totals, and
  previous balance + charges − payments = new balance;
- say what you invented in the `#` header, and set `# bank:` and
  `# transactions:`.

Then check it:

```bash
uv run finances report-bank --check tests/fixtures/banks/my-bank.txt
uv run python -m unittest discover tests -p "test_banks.py"
```

The test renders the layout to a PDF, parses it the way a real statement is
parsed, and requires it to be read as your bank, find the number of
transactions the header says, and reconcile.

### 4. If the parser itself needs a change

`finances/parse.py` is generic: column positions, section headings and summary
labels, not per-bank code. A fix there helps every bank with the same habit,
so describe the habit in a comment (the existing ones read like "Amex wraps a
long merchant name onto the line above the date") and add a fixture that shows
it. The whole suite has to pass, including every other bank's fixture.

## Other good first contributions

- **Categories.** `finances/categorizer.py` holds the merchant rules. A rule
  that fixes a real miscategorization, with a test line, is a small, welcome PR.
- **Download guides.** The `[guide]` table in a bank's file is what the
  dashboard shows under "Where to get it". Corrections are welcome when a
  bank's site changes.
- **Accuracy numbers.** Run `finances accuracy --markdown` and post the table in
  [Discussions](https://github.com/roshanlam/finances/discussions/categories/show-and-tell).
  It contains only bank names and counts, and it is how the README's table grows.

Issues labelled
[good first issue](https://github.com/roshanlam/finances/labels/good%20first%20issue)
name the files involved and what done looks like.

## Pull requests

- One change per PR, with a test that fails without it.
- Standard library only. `pdfplumber` is the one dependency, and adding another
  needs a very good reason.
- Comments say *why*, in plain sentences; match the code around you.
- Update `CHANGELOG.md` under "Unreleased" if users would notice.

By contributing you agree your work is released under the [MIT license](LICENSE),
and to follow the [code of conduct](CODE_OF_CONDUCT.md).
