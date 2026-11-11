"""
layout.py — a statement's layout as plain text, with nothing of yours in it.

Adding a bank means seeing its statements, and nobody should post their real
statement to an issue. A statement's layout is what the parser needs to see
anyway: which words sit where on each line, how the columns line up and what the
summary box calls things. So this reads a PDF and writes every line back out as
monospaced text at its own position, with the personal parts scrambled:

* every word the parser does not look for (names, addresses, merchants) gets
  random letters of the same length;
* every amount, account number and reference gets random digits of the same
  length, the same way each time it appears, so a balance printed twice is
  still printed twice;
* dates, the bank's name and the statement's own vocabulary ("New Balance",
  "Payments and Other Credits", "Amount") stay, since they are what a parser
  reads and say nothing about you.

The text turns back into a PDF (``to_pdf``), one character per grid cell in
Courier, which the parser reads exactly the way it reads the original. A
layout file is then also a test fixture: put made-up amounts in it that add up
and it proves the parser reads that bank.

    finances report-bank statement.pdf               a layout to attach to an issue
    finances report-bank statement.pdf --keep        no scrambling: for your own fixtures
    finances report-bank --check chase.txt           does this layout parse, and reconcile?

The file starts with ``#`` lines saying what the parser made of the original
(the bank, how many transactions, whether it reconciled). Read it before
posting it: it is plain text, so you can see that nothing of yours is left.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import tempfile

CW = 4.2            # pt per grid cell: Courier at 7pt is 0.6 * 7 wide
FONT_SIZE = 7
ROW = 9.0           # pt between lines
TOP = 30.0          # pt from the top of the page to the first line
CELL_GAP = 12.0     # parse.CELL_GAP: a wider gap starts a new cell
PAGE_BREAK = "--- page ---"
HEADER = "# finances layout v1"

COMMON = """
a about account accounts activity additional adjustments after all amount amounts and annual
any apr are as at atm auto autopay available average back balance balances bank banking be
beginning bill billing box by call can card cards cash category change charge charged charges
check checking checks closing code contact continued credit credits current customer cycle
daily date dates day days debit debits deposit deposits description detail details direct
due earned electronic end ending fee fees fixed for from fund funds go have if in information
interest is it its late limit line make merchant method minimum monthly more new next no not
notice number of on online opening or other our over overdraft page paid pay payment payments
pending per period please points post posted posting previous price pst purchase purchases
rate rates reference rewards savings service since statement statements summary than thank
that the this through to total totals transaction transactions transfer transfers type use
value variable visit was we when which will with withdrawal withdrawals year you your
"""


def _keep_words() -> set[str]:
    """The words the parser keys on, read out of its own patterns, plus the
    plain vocabulary of a statement. Anything else could be about you."""
    from finances import parse
    from finances.banks import investment_issuers, issuers
    from finances.utils import MONTHS
    pats = [parse.NOISE, parse.SEC_CREDIT, parse.SEC_DEBIT, parse.SEC_SKIP,
            parse.FEE_OR_INTEREST, parse.BANK_OUT, parse.BANK_IN]
    pats += [rx for _, rx in parse.SUMMARY_LABELS] + [rx for _, rx in parse.HEADER_ROLES]
    texts = [p.pattern for p in pats] + [p for p in parse.PERIOD_PATTERNS]
    texts += [p for _, p in parse.LOOSE_FIELDS] + [p for _, p in parse.LAST4_PATTERNS]
    texts += [p for _, p in issuers()] + [p for _, p in investment_issuers()]
    texts += [n for n, _ in issuers()] + [n for n, _ in investment_issuers()]
    words = {w for t in texts for w in re.findall(r"[a-z]{2,}", t.lower())}
    words |= set(COMMON.split()) | {m.lower() for m in MONTHS}
    words |= {"january", "february", "march", "april", "june", "july", "august",
              "september", "october", "november", "december", "cr", "dr"}
    return words


DATE_TOKEN = re.compile(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\*?")
YEAR_TOKEN = re.compile(r"(?:19|20)\d\d")


class Scrambler:
    """Same-shaped random replacements, the same each time a value repeats."""

    def __init__(self, keep: set[str], rng: random.Random):
        self.keep, self.rng, self.seen = keep, rng, {}

    def _digits(self, run: str) -> str:
        if run not in self.seen:
            out = "".join(self.rng.choice("0123456789") for _ in run)
            if len(run) > 1 and run[0] != "0":
                out = self.rng.choice("123456789") + out[1:]
            self.seen[run] = out
        return self.seen[run]

    def _letters(self, word: str) -> str:
        if word.lower() in self.keep:
            return word
        key = word.lower()
        if key not in self.seen:
            self.seen[key] = "".join(self.rng.choice("abcdefghijklmnopqrstuvwxyz")
                                     for _ in word)
        fake = self.seen[key]
        return "".join(f.upper() if c.isupper() else f for c, f in zip(word, fake))

    def token(self, t: str) -> str:
        bare = t.strip(",.;:*()")
        if DATE_TOKEN.fullmatch(bare) or YEAR_TOKEN.fullmatch(bare) \
                or re.fullmatch(r"\d{1,2}", bare):
            return t                     # dates and day counts
        if any(c.isdigit() for c in t):
            # Amounts keep their separators so they still read as amounts.
            if re.fullmatch(r"[-+$(]*[\d,]*\.\d\d\)?-?", t.replace("$", "")):
                head, cents = t.rsplit(".", 1)
                digits = re.sub(r"\D", "", head)
                fake = iter(self._digits(digits + cents[:2]))
                return re.sub(r"\d", lambda _: next(fake), t)
            t = re.sub(r"\d+", lambda m: self._digits(m.group()), t)
        return re.sub(r"[A-Za-z]+", lambda m: self._letters(m.group()), t)


# ---------------------------------------------------------------------------
# PDF -> text grid
# ---------------------------------------------------------------------------

def _is_money(t: str) -> bool:
    from finances.parse import MONEY_TOKEN
    return bool(MONEY_TOKEN.match(t))


def grid_line(ws, fix=lambda t: t) -> str:
    """One line of words at their own columns.

    Amounts are placed by their right edge (columns of figures are right
    aligned), everything else by its left edge, and the spacing keeps the
    parser's one structural rule: a gap wider than CELL_GAP starts a new cell,
    a narrower one does not.
    """
    out, end, prev = "", 0, None
    for w in ws:
        text = fix(w["text"])
        want = round(w["x1"] / CW) - len(text) if _is_money(w["text"]) \
            else round(w["x0"] / CW)
        if prev is not None:
            wide = w["x0"] - prev["x1"] > CELL_GAP
            lo = end + (3 if wide else 1)
            hi = 10 ** 6 if wide else end + 2
            want = min(max(want, lo), hi)
        start = max(want, 0)
        out = out.ljust(start) + text
        end, prev = start + len(text), w
    return out.rstrip()


def read_pages(path: str, password: str | None = None) -> list[list[list[dict]]]:
    import pdfplumber
    from finances.parse import page_lines
    kwargs = {"password": password} if password else {}
    with pdfplumber.open(path, **kwargs) as pdf:
        return [page_lines(p) for p in pdf.pages]


def summarize(path: str, password: str | None = None) -> dict:
    """What the parser made of a statement, without any of its amounts."""
    from finances.parse import parse_statement
    txns, meta = parse_statement(path, password)
    st = meta["statement"]
    read = {(t.page, t.line) for t in txns}
    found = sorted({k for k in ("previous_balance", "new_balance", "payments", "purchases",
                                "interest_charged", "fees_charged")
                    if getattr(st, k, None) is not None})
    verdict = {1: "reconciles", 0: "does not reconcile", None: "nothing printed to check"}
    return {"bank": st.issuer or "Unknown", "pages": meta.get("pages"),
            "transactions": len(txns), "verdict": verdict[st.reconciled],
            "summary": found, "read": read, "statement": st}


def from_pdf(path: str, scramble: bool = True, password: str | None = None,
             rng: random.Random | None = None) -> str:
    pages = read_pages(path, password)
    info = summarize(path, password)
    sc = Scrambler(_keep_words(), rng or random.Random()) if scramble else None
    fix = sc.token if sc else (lambda t: t)
    from finances.parse import words_text
    body, marked = [], []
    for pi, lines in enumerate(pages):
        if pi:
            body.append(PAGE_BREAK)
        for ws in lines:
            if (pi + 1, words_text(ws).strip()[:200]) in info["read"]:
                marked.append(len(body) + 1)
            body.append(grid_line(ws, fix))
    head = [HEADER,
            f"# bank: {info['bank']}",
            f"# pages: {info['pages']}",
            f"# transactions: {info['transactions']}",
            f"# verdict on the original: {info['verdict']}",
            f"# summary figures found: {', '.join(info['summary']) or 'none'}",
            f"# lines read as transactions (counted below this header): {_ranges(marked) or 'none'}"]
    if scramble:
        head.append("# Scrambled: names, merchants, amounts and account numbers are random.")
        head.append("# Dates and the statement's own words are kept. Read it before you post it.")
    return "\n".join(head + ["#"] + body) + "\n"


def _ranges(nums: list[int]) -> str:
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------------------
# text grid -> PDF
# ---------------------------------------------------------------------------

def header(text: str) -> dict:
    """The ``# key: value`` lines at the top of a layout."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        k, sep, v = line[1:].partition(":")
        if sep and k.strip():
            out[k.strip().lower()] = v.strip()
    return out


def body_pages(text: str) -> list[list[str]]:
    lines = text.splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    pages = [[]]
    for line in lines:
        if line.strip() == PAGE_BREAK:
            pages.append([])
        else:
            pages[-1].append(line.rstrip())
    return pages


def _escape(s: str) -> bytes:
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return s.encode("latin-1", "replace")


def to_pdf(text: str) -> bytes:
    """The layout as a PDF: Courier, one character per grid cell."""
    pages = body_pages(text)
    objs: list[bytes] = [b"", b""]           # catalog and page tree, filled in last
    kids = []
    for lines in pages:
        width = max([612] + [int(len(ln) * CW) + 40 for ln in lines])
        row = min(ROW, (792 - 2 * TOP) / max(len(lines), 1))
        ops = [b"BT /F1 %d Tf" % FONT_SIZE]
        for i, ln in enumerate(lines):
            if ln.strip():
                ops.append(b"1 0 0 1 0 %.2f Tm (" % (792 - TOP - i * row) + _escape(ln) + b") Tj")
        ops.append(b"ET")
        content = b"\n".join(ops)
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d 792] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 %d 0 R >> >> >>"
                    % (width, len(objs), 3 + 2 * len(pages)))
        kids.append(len(objs))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def parse_layout(text: str, name: str = "layout.pdf"):
    """Parse a layout the way a real statement is parsed: (txns, meta).

    The PDF is named after the layout, since the parser reads a period out of
    the filename when the statement prints none.
    """
    from finances.parse import parse_statement
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, os.path.splitext(os.path.basename(name))[0] + ".pdf")
        with open(path, "wb") as fh:
            fh.write(to_pdf(text))
        return parse_statement(path)


def check(text: str, name: str = "layout.pdf") -> dict:
    """What the parser makes of a layout, against what its header expects."""
    txns, meta = parse_layout(text, name)
    st = meta["statement"]
    want = header(text)
    problems = []
    if want.get("bank") and want["bank"] != st.issuer:
        problems.append(f"read as {st.issuer or 'Unknown'}, expected {want['bank']}")
    if want.get("transactions", "").isdigit() and int(want["transactions"]) != len(txns):
        problems.append(f"{len(txns)} transactions read, expected {want['transactions']}")
    if st.reconciled != 1:
        problems.append(f"does not reconcile: {st.reconcile_note or 'no printed total found'}")
    return {"bank": st.issuer, "transactions": len(txns), "reconciled": st.reconciled,
            "note": st.reconcile_note, "problems": problems, "statement": st, "txns": txns}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="finances report-bank",
                                 description=__doc__.split("\n\n")[1])
    ap.add_argument("file", help="a statement PDF, or with --check a layout .txt")
    ap.add_argument("-o", "--out", help="where to write the layout (default: next to you, .txt)")
    ap.add_argument("--keep", action="store_true",
                    help="do not scramble (only for files you will not share)")
    ap.add_argument("--check", action="store_true", help="parse a layout file and report")
    ap.add_argument("--password")
    args = ap.parse_args(argv)
    try:
        return _run(ap, args)
    except Exception as e:                       # noqa: BLE001  (a scan, a password, a bad file)
        print(f"error: {e or type(e).__name__}", file=sys.stderr)
        return 1


def _run(ap, args) -> int:
    if args.check:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
        if not any(line.strip() for page in body_pages(text) for line in page):
            raise ValueError(f"{args.file} has no layout in it, only a header")
        r = check(text, args.file)
        print(f"  bank: {r['bank'] or 'Unknown'}   transactions: {r['transactions']}   "
              f"{'reconciles' if r['reconciled'] == 1 else 'does not reconcile'}")
        for p in r["problems"]:
            print(f"  - {p}")
        return 1 if r["problems"] else 0
    if not args.file.lower().endswith(".pdf") or not os.path.isfile(args.file):
        ap.error(f"{args.file} is not a statement PDF")
    text = from_pdf(args.file, scramble=not args.keep, password=args.password)
    out = args.out or os.path.splitext(os.path.basename(args.file))[0] + "-layout.txt"
    if os.path.abspath(out) == os.path.abspath(args.file):
        ap.error("refusing to overwrite the statement")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    info = header(text)
    print(f"wrote {out}")
    print(f"  {info.get('bank')}: {info.get('transactions')} transactions read, "
          f"{info.get('verdict on the original')}")
    if not args.keep:
        print("  Open it and read it before you attach it: it is plain text.")
        print("  Then: https://github.com/roshanlam/finances/issues/new?template=new-bank.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
