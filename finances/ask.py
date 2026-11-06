#!/usr/bin/env python3
"""
ask.py — ask the finances store a question in plain English, entirely offline.

The rest of the toolchain answers the questions someone anticipated: analyze.py
knows about categories, planner.py knows about goals. This answers the ones
nobody anticipated ("how much did I spend at that one bakery in March") by
letting a *local* model write the SQL and then refusing to run it unless it is
provably read-only.

    finances ask "how much did I spend on coffee in March"
    finances ask "what are my biggest merchants" --explain
    finances ask --sql "SELECT category, SUM(amount) FROM transactions GROUP BY 1"
    finances ask --examples                 # hand-written queries, no model needed
    finances ask --example top-merchants    # run one of them
    finances ask --schema                   # exactly what the model gets told

Nothing leaves the machine. The model, when there is one, is whatever
OpenAI-compatible server is already listening on localhost — llm_enrich.py
already knows how to find one, size it against available RAM and talk to it, so
all of that is imported rather than written again.

There is very often no model running, and that path is the important one: every
flag above except the plain-English question works with no model at all, and
``--examples`` ships hand-written SQL for the questions people actually ask.

The safety story, in order of how much it is trusted:

1. The connection is opened read-only (``file:finances.db?mode=ro``), so a write
   cannot reach the file even if everything else fails.
2. A SQLite authorizer denies every action except SELECT, READ, FUNCTION and
   RECURSIVE, so ATTACH, PRAGMA and DDL are refused by the engine itself.
3. The text is parsed before it is executed: string literals and comments are
   masked out, then the statement must be a single SELECT (or WITH ... SELECT)
   with no forbidden keyword and no second statement hiding after a semicolon.

A model that misbehaves is expected, not exceptional. Nothing is silently
rewritten into something safer: a rejected query is printed with the reason.

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import date


from finances.database import DEFAULT_DB
from finances.utils import fmt_money

try:
    from finances.engine import NON_SPEND
except ImportError:                                          # pragma: no cover
    NON_SPEND = {"Payment", "Credits/Refunds", "Transfers/Cash", "Income"}

# llm_enrich.py owns every part of "find a local model and talk to it". If it is
# missing, or its imports are, the whole natural-language path degrades to a
# message and --sql/--examples carry on working.
try:
    from finances.llm_enrich import (COMMON_ENDPOINTS, choose_model, describe,
                            describe_device, detect_device, discover,
                            http_json, total_ram_gb)
    HAVE_LLM = True
except ImportError as exc:                                   # pragma: no cover
    HAVE_LLM, _LLM_IMPORT_ERROR = False, exc
    COMMON_ENDPOINTS = [("http://localhost:11434/v1", "Ollama")]


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------
#
# Everything here runs before SQLite sees a single character.

class Rejected(Exception):
    """A query was not allowed to run. The message says exactly why."""


# Whole-word tokens that can never appear in a read-only query. REPLACE is the
# one ambiguous name — it is both a statement and a scalar function — so it is
# allowed when it is immediately followed by '(' and refused otherwise.
FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "attach", "detach",
    "pragma", "vacuum", "reindex", "truncate", "grant", "revoke", "begin",
    "commit", "rollback", "savepoint", "release", "trigger", "temp", "temporary",
    "analyze", "returning", "load_extension", "readfile", "writefile", "fts3_tokenizer",
}

_KEYWORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def mask_literals(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments.

    Every check below reads SQL *code*, never data. A merchant called
    "DELETE ME LLC" is not a DELETE statement, and ``-- drop table`` is a
    comment. Masking in place (same length, spaces substituted) keeps every
    offset valid so error messages can still point at the original text.
    """
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:        # '' escapes a quote
                        out[j] = out[j + 1] = " "
                        j += 2
                        continue
                    break
                out[j] = " "
                j += 1
            if j >= n:
                raise Rejected("unterminated string literal")
            i = j + 1
        elif ch == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise Rejected("unterminated [quoted identifier]")
            for k in range(i + 1, j):
                out[k] = " "
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _split_statements(masked: str) -> list[int]:
    """Offsets of top-level semicolons."""
    depth, hits = 0, []
    for i, ch in enumerate(masked):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            hits.append(i)
    return hits


def _has_top_level(masked: str, word: str) -> bool:
    """Is this keyword used outside every set of parentheses?

    A LIMIT inside a subquery does not limit the result, and the SELECT inside
    a CTE body does not make ``WITH x AS (SELECT 1)`` a query.
    """
    depth = 0
    for m in re.finditer(r"[()]|\b%s\b" % word, masked, re.I):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return True
    return False


def _forbidden_hit(masked: str) -> str | None:
    for m in _KEYWORD.finditer(masked):
        word = m.group(0).lower()
        if word == "replace":
            tail = masked[m.end():].lstrip()
            if tail.startswith("("):                          # the scalar function
                continue
            return "replace"
        if word in FORBIDDEN:
            return word
    return None


def validate_select(sql: str, limit: int = 50,
                    tolerate_trailing_prose: bool = False) -> str:
    """Return a runnable single SELECT, or raise :class:`Rejected` saying why.

    ``tolerate_trailing_prose`` is for model output only: small models like to
    append "This query returns ..." after the statement. That tail is dropped
    only when it contains nothing that looks like SQL — anything statement-like
    after the semicolon is a rejection, not something to quietly discard.
    """
    text = sql.strip()
    if not text:
        raise Rejected("empty query")

    full_masked = mask_literals(text)
    semis = _split_statements(full_masked)
    cut = semis[0] if semis else len(text)
    head, head_masked = text[:cut].strip(), full_masked[:cut]

    # The first statement is judged before the rest is even considered, so a
    # payload like "DROP TABLE x; SELECT 1" is reported as the DROP it is.
    if not head:
        raise Rejected("empty query")
    first = _KEYWORD.search(head_masked)
    verb = first.group(0).lower() if first else ""
    if verb not in ("select", "with"):
        raise Rejected(f"only SELECT is allowed, this starts with {verb.upper() or '?'}")
    bad = _forbidden_hit(head_masked)
    if bad:
        raise Rejected(f"contains the forbidden keyword {bad.upper()}")

    if semis:
        tail, tail_masked = text[cut + 1:], full_masked[cut + 1:]
        if tail_masked.strip():
            looks_like_sql = (_forbidden_hit(tail_masked)
                              or re.search(r"\b(select|with|union|values)\b",
                                           tail_masked, re.I)
                              or _split_statements(tail_masked))
            if looks_like_sql or not tolerate_trailing_prose:
                extra = " ".join(tail.split())[:80]
                raise Rejected(
                    f"more than one statement — everything after the first ';' "
                    f"is refused (found: {extra!r})")
    text, masked = head, head_masked

    # A CTE has to resolve to a SELECT of its own; the one inside the CTE body
    # does not count.
    if not _has_top_level(masked, "select"):
        raise Rejected("this does not end in a SELECT, so it returns nothing")

    if not _has_top_level(masked, "limit"):
        text = f"{text}\nLIMIT {int(limit)}"
    return text


_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
})


def _authorizer(action, arg1, arg2, db_name, trigger):
    """Second line of defence: SQLite itself refuses anything but a read."""
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def open_readonly(path: str) -> sqlite3.Connection:
    """Open the store read-only. Introspection runs before the guard is armed."""
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} does not exist. Build it with:  finances parse <PDFs> --db {path}")
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def arm(conn: sqlite3.Connection, timeout: float = 10.0):
    """Install the authorizer and the query timeout.

    Called *after* schema introspection, because PRAGMA table_info is exactly
    the sort of thing the authorizer is there to deny.
    """
    conn.set_authorizer(_authorizer)
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)


def run_query(conn: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    try:
        cur = conn.execute(sql)
        rows = cur.fetchall()
    except sqlite3.DatabaseError as exc:
        # The progress handler aborts by raising here, and so does the
        # authorizer; both mean the query never touched anything it shouldn't.
        if "interrupted" in str(exc).lower():
            raise Rejected("it ran longer than the query timeout and was "
                           "cancelled (raise it with --query-timeout)") from exc
        raise Rejected(f"SQLite refused it: {exc}") from exc
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, [tuple(r) for r in rows]


# --------------------------------------------------------------------------
# Showing the answer
# --------------------------------------------------------------------------

_MONEY_WORDS = ("amount", "total", "spend", "spent", "sum", "avg", "average",
                "balance", "cost", "charge", "price", "fee", "interest",
                "payment", "purchase", "credit", "income", "median", "cash",
                "dollars", "usd", "diff", "limit_")
_COUNT_WORDS = ("count", "n", "txns", "rows", "months", "days", "num", "times")


def _numbers(values) -> list[float]:
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _column_kind(name: str, values: list) -> str:
    """money, number or text — decides alignment and formatting."""
    present = [v for v in values if v is not None]
    nums = _numbers(present)
    if not present or len(nums) != len(present):
        return "text"
    low = name.lower()
    if any(re.search(rf"\b{w}\b", low) for w in _COUNT_WORDS):
        return "number"
    if any(w in low for w in _MONEY_WORDS):
        return "money"
    return "number"


def _cell(v, kind: str, width: int = 44) -> str:
    if v is None:
        return "-"
    if kind == "money":
        return fmt_money(v, 2)
    if kind == "number":
        if isinstance(v, float) and not v.is_integer():
            return f"{v:,.2f}"
        return f"{int(v):,}"
    s = str(v)
    return s if len(s) <= width else s[:width - 1] + "…"


def render(cols: list[str], rows: list[tuple], indent: str = "  ") -> str:
    """A plain aligned table. Money goes through fmt_money, so every tool in the
    toolchain prints a dollar the same way."""
    if not cols:
        return indent + "(no columns)"
    if not rows:
        return indent + "no rows"

    kinds = [_column_kind(c, [r[i] for r in rows]) for i, c in enumerate(cols)]
    cells = [[_cell(r[i], kinds[i]) for i in range(len(cols))] for r in rows]
    widths = [max(len(cols[i]), *(len(row[i]) for row in cells))
              for i in range(len(cols))]

    def line(values):
        out = []
        for i, v in enumerate(values):
            out.append(v.rjust(widths[i]) if kinds[i] != "text" else v.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    head = indent + "  ".join(
        (cols[i].rjust(widths[i]) if kinds[i] != "text" else cols[i].ljust(widths[i]))
        for i in range(len(cols))).rstrip()
    rule = indent + "  ".join("-" * w for w in widths)
    return "\n".join([head, rule] + [line(c) for c in cells])


# --------------------------------------------------------------------------
# What the model is told
# --------------------------------------------------------------------------

SYSTEM = (
    "You translate questions about a personal finance database into one SQLite "
    "SELECT statement. You reply with SQL and nothing else — no prose, no "
    "markdown fences, no explanation, no trailing semicolon commentary."
)

_NON_SPEND_SQL = ", ".join(f"'{c}'" for c in sorted(NON_SPEND))


def _col_types(conn, table: str, columns: list[str]) -> dict[str, str]:
    """These tables are created without declared types, so ask the data.

    ``typeof()`` on the first non-null value is a far better hint to the model
    than the empty string PRAGMA table_info reports.
    """
    out = {}
    for c in columns:
        try:
            row = conn.execute(
                f'SELECT typeof("{c}") FROM {table} WHERE "{c}" IS NOT NULL LIMIT 1'
            ).fetchone()
        except sqlite3.DatabaseError:
            row = None
        out[c] = (row[0] if row else "text").upper()
    return out


def schema_context(conn: sqlite3.Connection, examples: int = 3) -> str:
    """Describe the database as it actually is, not as the schema declares it.

    The category vocabulary matters more than anything else here: a model that
    can read the real list writes ``category = 'Coffee'``, and one that cannot
    invents ``category = 'coffee shops'`` and returns zero rows.
    """
    parts = ["You are querying a local SQLite database of parsed bank and credit "
             "card statements.",
             f"Today is {date.today().isoformat()}."]

    for table in ("transactions", "statements"):
        cols = [r["name"] for r in
                conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not cols:
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        types = _col_types(conn, table, cols)
        parts.append(f"\nTABLE {table} — {n} rows")
        parts.append("  " + ", ".join(f"{c} {types[c]}" for c in cols))

    rng = conn.execute("SELECT MIN(date), MAX(date) FROM transactions").fetchone()
    if rng and rng[0]:
        parts.append(f"\nDates run {rng[0]} to {rng[1]} (TEXT, ISO 'YYYY-MM-DD').")

    cats = conn.execute(
        "SELECT category, COUNT(*) AS n FROM transactions "
        "GROUP BY category ORDER BY n DESC").fetchall()
    if cats:
        parts.append("\nEvery category present, with row counts. Use these exact "
                     "spellings:")
        parts.append("  " + ", ".join(f"{r['category']} ({r['n']})" for r in cats))

    issuers = conn.execute(
        "SELECT issuer, account_last4, COUNT(*) AS n FROM transactions "
        "GROUP BY issuer, account_last4 ORDER BY n DESC LIMIT 12").fetchall()
    if issuers:
        parts.append("\nAccounts (issuer, account_last4):")
        parts.append("  " + ", ".join(
            f"{r['issuer'] or '?'} {r['account_last4'] or '?'} ({r['n']})"
            for r in issuers))

    tops = conn.execute(
        "SELECT merchant, COUNT(*) AS n FROM transactions "
        "WHERE merchant <> '' GROUP BY merchant ORDER BY n DESC LIMIT 15").fetchall()
    if tops:
        parts.append("\nCommon merchant values (the column is messy, match with LIKE):")
        parts.append("  " + ", ".join(f"{r['merchant']}" for r in tops))

    if examples:
        rows = conn.execute(
            "SELECT date, description, merchant, amount, category, issuer "
            "FROM transactions WHERE amount > 0 ORDER BY date DESC LIMIT ?",
            (examples,)).fetchall()
        if rows:
            parts.append("\nExample rows:")
            for r in rows:
                parts.append("  " + " | ".join(str(v) for v in tuple(r)))

    parts.append(f"""
Facts about the data:
- amount is REAL. Money OUT is POSITIVE, money IN is NEGATIVE. Spending is amount > 0.
- Real spending excludes the non-spend categories {_NON_SPEND_SQL}, which are
  card payments, refunds, transfers between your own accounts, and salary.
- date is TEXT: use substr(date, 1, 7) for a month ('2026-03') and
  substr(date, 1, 4) for a year. Do not use date functions on it.
- description is the raw statement line. merchant is the cleaned name and is the
  better one to group by.
- statements joins to transactions on source_file.

Rules for the SQL you write:
- Exactly one SELECT statement. Never INSERT, UPDATE, DELETE, DROP, ALTER,
  ATTACH or PRAGMA. Never chain statements with a semicolon.
- Alias every computed column, and ROUND money to 2 decimals.
- Order the results so the interesting rows are first, and LIMIT long lists.
- Reply with the SQL only.""")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# The local model
# --------------------------------------------------------------------------

def find_model(endpoint=None, model=None, max_params=14.0, prefer="largest",
               force=False):
    """Locate a local server and pick a model that fits in RAM.

    All of this is llm_enrich's work; this only decides what to do with the
    answer. Returns (endpoint, model_name, note) or None.
    """
    if not HAVE_LLM:
        return None
    base, models, label = discover(endpoint)
    if not base or not models:
        return None
    if model:
        names = [m["name"] for m in models]
        if model not in names:
            near = [n for n in names if n.startswith(model)]
            if not near:
                raise SystemExit(
                    f"{base} ({label}) does not serve {model!r}. It has: "
                    + ", ".join(names))
            model = near[0]
        return base, model, f"{label} at {base}"

    device = detect_device()
    pick, ok, too_big, unknown = choose_model(models, device.get("ram"),
                                              max_params, prefer, force)
    if not pick:
        biggest = ", ".join(describe(m) for m in (too_big + unknown)[:4])
        raise SystemExit(
            f"{label} at {base} is running, but nothing it serves fits in "
            f"{describe_device(device)}.\nToo large: {biggest}\n"
            f"Pull something smaller (ollama pull llama3.1:8b), raise the ceiling "
            f"with --max-params, or name one with --model.")
    return base, pick["name"], f"{label} at {base}, {describe(pick)}"


def ask_model(endpoint: str, model: str, question: str, schema: str,
              timeout: float = 240.0) -> str:
    """Send the question plus the real schema, get SQL back.

    This posts through llm_enrich.http_json rather than llm_enrich.complete
    because complete() hardcodes its merchant-labelling system prompt, which
    steers a model badly when the task is SQL. The transport, the discovery and
    the sizing are all still llm_enrich's.
    """
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user",
                      "content": f"{schema}\n\nQuestion: {question}\n\nSQL:"}],
        "temperature": 0.0,
        "stream": False,
    }
    out = http_json(endpoint.rstrip("/") + "/chat/completions", body, timeout=timeout)
    try:
        return out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise Rejected(f"the server replied in a shape this tool does not "
                       f"understand: {str(out)[:200]}") from exc


_FENCE = re.compile(r"^```[A-Za-z]*\s*|\s*```$", re.M)
_THINK = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.S | re.I)


def clean_model_sql(text: str) -> str:
    """Dig the statement out of whatever the model wrapped it in.

    Reasoning tags and markdown fences are stripped. A prose preamble is
    dropped only when it contains nothing SQL-shaped — if the model wrote
    "DROP TABLE x; SELECT ..." the preamble is kept so the guard sees it and
    rejects the whole thing.
    """
    t = _THINK.sub(" ", text or "").strip()
    t = _FENCE.sub("", t).strip()
    t = re.sub(r"^(?:sql|query|answer)\s*[:\-]\s*", "", t, flags=re.I).strip()

    m = re.search(r"\b(select|with)\b", t, re.I)
    if m and m.start() > 0:
        prefix = mask_literals(t[:m.start()])
        if not _forbidden_hit(prefix) and ";" not in prefix:
            t = t[m.start():]
    return t.strip()


# --------------------------------------------------------------------------
# Hand-written queries, for when there is no model
# --------------------------------------------------------------------------
#
# These carry the tool on their own. Each one is the query someone would
# actually write for that question, so they double as worked examples of the
# schema for anyone reading --examples.

SPEND = f"amount > 0 AND category NOT IN ({_NON_SPEND_SQL})"

EXAMPLES: list[tuple[str, str, str]] = [
    ("top-categories", "Where does my money go?", f"""
SELECT category,
       ROUND(SUM(amount), 2) AS spend,
       COUNT(*)              AS n,
       ROUND(AVG(amount), 2) AS avg_charge
FROM transactions
WHERE {SPEND}
GROUP BY category
ORDER BY spend DESC"""),

    ("top-merchants", "Which merchants take the most of it?", f"""
SELECT merchant,
       ROUND(SUM(amount), 2) AS spend,
       COUNT(*)              AS n,
       MIN(date)             AS first_seen,
       MAX(date)             AS last_seen
FROM transactions
WHERE {SPEND} AND merchant <> ''
GROUP BY merchant
ORDER BY spend DESC
LIMIT 25"""),

    ("by-month", "What does a month cost me?", f"""
SELECT substr(date, 1, 7)    AS month,
       ROUND(SUM(amount), 2) AS spend,
       COUNT(*)              AS n
FROM transactions
WHERE {SPEND}
GROUP BY month
ORDER BY month"""),

    ("biggest-charges", "What were the largest single charges?", f"""
SELECT date, merchant, description, category,
       ROUND(amount, 2) AS amount
FROM transactions
WHERE {SPEND}
ORDER BY amount DESC
LIMIT 25"""),

    ("subscriptions", "What is billing me every month?", f"""
SELECT merchant,
       COUNT(DISTINCT substr(date, 1, 7)) AS months,
       COUNT(*)                           AS n,
       ROUND(AVG(amount), 2)              AS avg_charge,
       ROUND(SUM(amount), 2)              AS total,
       MAX(date)                          AS last_seen
FROM transactions
WHERE {SPEND} AND merchant <> ''
GROUP BY merchant
HAVING months >= 3 AND MAX(amount) <= MIN(amount) * 1.35
ORDER BY avg_charge * months DESC
LIMIT 25"""),

    ("category-by-month", "How has each category moved month to month?", f"""
SELECT category,
       substr(date, 1, 7)    AS month,
       ROUND(SUM(amount), 2) AS spend
FROM transactions
WHERE {SPEND}
GROUP BY category, month
ORDER BY category, month
LIMIT 200"""),

    ("coffee", "How much goes on coffee, by month?", """
SELECT substr(date, 1, 7)    AS month,
       ROUND(SUM(amount), 2) AS spend,
       COUNT(*)              AS cups
FROM transactions
WHERE amount > 0 AND category = 'Coffee'
GROUP BY month
ORDER BY month"""),

    ("fees", "What am I paying in fees and interest?", """
SELECT date, issuer, description, ROUND(amount, 2) AS amount
FROM transactions
WHERE category = 'Fees/Interest' AND amount > 0
ORDER BY amount DESC
LIMIT 25"""),

    ("income", "What came in?", """
SELECT substr(date, 1, 7)     AS month,
       ROUND(-SUM(amount), 2) AS income,
       COUNT(*)               AS n
FROM transactions
WHERE category = 'Income'
GROUP BY month
ORDER BY month"""),

    ("accounts", "How is spending split across my cards?", f"""
SELECT issuer, account_last4,
       ROUND(SUM(amount), 2) AS spend,
       COUNT(*)              AS n,
       MIN(date)             AS first_seen,
       MAX(date)             AS last_seen
FROM transactions
WHERE {SPEND}
GROUP BY issuer, account_last4
ORDER BY spend DESC"""),

    ("uncategorized", "What is the categoriser still missing?", """
SELECT date, description, merchant, ROUND(amount, 2) AS amount
FROM transactions
WHERE category = 'Uncategorized' AND amount > 0
ORDER BY amount DESC
LIMIT 25"""),

    ("statements", "What do the statements themselves say?", """
SELECT source_file, issuer, account_last4, statement_period,
       ROUND(new_balance, 2)     AS new_balance,
       ROUND(purchases, 2)       AS purchases,
       ROUND(interest_charged, 2) AS interest,
       reconciled
FROM statements
ORDER BY period_start, issuer
LIMIT 50"""),
]

EXAMPLE_INDEX = {key: (question, sql.strip()) for key, question, sql in EXAMPLES}

_STOP = {"the", "a", "an", "my", "me", "i", "how", "much", "what", "on", "in",
         "of", "is", "are", "do", "did", "does", "for", "to", "at", "and", "was",
         "spend", "spent", "show", "list", "give"}


def suggest_examples(question: str, n: int = 3) -> list[str]:
    """Closest canned queries, by word overlap. Used when there is no model."""
    words = {w for w in re.findall(r"[a-z]+", question.lower()) if w not in _STOP}
    if not words:
        return []
    scored = []
    for key, q, _ in EXAMPLES:
        hay = set(re.findall(r"[a-z]+", (key + " " + q).lower().replace("-", " ")))
        hit = len(words & hay)
        if hit:
            scored.append((hit, key))
    scored.sort(reverse=True)
    return [k for _, k in scored[:n]]


# --------------------------------------------------------------------------
# No model? Say what to do about it.
# --------------------------------------------------------------------------

def no_model_message(question: str | None, endpoint: str | None) -> str:
    endpoint = endpoint or os.environ.get("FINANCES_LLM_ENDPOINT")
    checked = ([endpoint] if endpoint else
               [f"{u}  ({label})" for u, label in COMMON_ENDPOINTS])
    lines = ["No local model is running, so the question cannot be translated "
             "into SQL.", "", "Looked for an OpenAI-compatible server on:"]
    lines += [f"  {c}" for c in checked]
    if not HAVE_LLM:
        lines.append(f"  (llm_enrich.py could not be imported: {_LLM_IMPORT_ERROR})")

    works = [("finances ask --examples",
              "hand-written queries for the common questions"),
             ("finances ask --example top-merchants", "run one of them"),
             ('finances ask --sql "SELECT ..."', "run your own SQL"),
             ("finances ask --schema",
              "the schema, so you can write that SQL")]
    pad = max(len(c) for c, _ in works)
    lines += ["", "This tool still works without one:"]
    lines += [f"  {cmd:<{pad}}   {what}" for cmd, what in works]

    close = suggest_examples(question or "")
    if close:
        lines += ["", "Closest canned queries to what you asked:"]
        for key in close:
            lines.append(f"  finances ask --example {key:<18} {EXAMPLE_INDEX[key][0]}")

    lines += ["", "To answer questions in plain English, run a model locally "
              "(one time, then offline forever):",
              "  brew install ollama            # or: curl -fsSL https://ollama.com/install.sh | sh",
              "  ollama pull llama3.1:8b        # ~5 GB, plenty for writing SQL",
              "  ollama serve                   # usually already running after install",
              "",
              "Then re-run the same command. LM Studio, llama.cpp --server and vLLM "
              "work too; point at one with --endpoint."]
    if HAVE_LLM:
        try:
            ram = total_ram_gb()
            if ram:
                lines.append(f"\nThis machine: {describe_device(detect_device())} — "
                             f"an 8B model fits comfortably.")
        except Exception:                                    # pragma: no cover
            pass
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_examples() -> int:
    print("Hand-written queries. No model needed — run one with --example KEY.\n")
    for key, question, sql in EXAMPLES:
        print(f"  {key}")
        print(f"      {question}")
        for line in sql.strip().splitlines():
            print(f"      | {line}")
        print()
    return 0


def show(sql: str, cols, rows, elapsed: float, limit: int):
    print(render(cols, rows))
    note = f"\n{len(rows)} row{'' if len(rows) == 1 else 's'} in {elapsed * 1000:.0f} ms"
    if len(rows) == limit and re.search(rf"\bLIMIT\s+{limit}\s*$", sql, re.I):
        note += f" (cut off at the {limit}-row limit, raise it with --limit)"
    print(note)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ask the finances store a question, offline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  finances ask "how much did I spend on coffee in March"
  finances ask "what are my biggest merchants" --explain
  finances ask --sql "SELECT category, SUM(amount) FROM transactions GROUP BY 1"
  finances ask --examples
  finances ask --schema""")
    ap.add_argument("question", nargs="*", help="the question, in plain English")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite store (default {DEFAULT_DB})")
    ap.add_argument("--sql", help="run this SQL instead of asking a model")
    ap.add_argument("--examples", action="store_true",
                    help="list the hand-written queries and exit")
    ap.add_argument("--example", metavar="KEY", help="run one hand-written query")
    ap.add_argument("--schema", action="store_true",
                    help="print exactly what the model would be told")
    ap.add_argument("--explain", action="store_true",
                    help="print the SQL before running it")
    ap.add_argument("--limit", type=int, default=50,
                    help="row limit applied when the query has none (default 50)")
    ap.add_argument("--query-timeout", type=float, default=10.0, metavar="SEC",
                    help="cancel a query that runs longer than this (default 10)")
    ap.add_argument("--endpoint", help="local server base URL, e.g. http://localhost:11434/v1")
    ap.add_argument("--model", help="model name to use instead of the automatic pick")
    ap.add_argument("--max-params", type=float, default=14.0,
                    help="never auto-pick a model larger than this many B (default 14)")
    ap.add_argument("--prefer", choices=("largest", "smallest"), default="largest",
                    help="among models that fit (default largest)")
    ap.add_argument("--force", action="store_true",
                    help="allow a model whose size cannot be determined")
    ap.add_argument("--llm-timeout", type=float, default=240.0, metavar="SEC",
                    help="how long to wait for the model (default 240)")
    ap.add_argument("--list-models", action="store_true",
                    help="what the local server offers, and what fits here")
    args = ap.parse_args()

    if args.examples:
        return cmd_examples()

    if args.list_models:
        if not HAVE_LLM:
            print(no_model_message(None, args.endpoint))
            return 1
        base, models, label = discover(args.endpoint)
        if not base:
            print(no_model_message(None, args.endpoint))
            return 1
        device = detect_device()
        print(f"{label} at {base}\n{describe_device(device)}\n")
        pick, ok, too_big, unknown = choose_model(models, device.get("ram"),
                                                  args.max_params, args.prefer,
                                                  args.force)
        for m in ok:
            print(f"  fits      {describe(m)}" + ("   <- would use" if m is pick else ""))
        for m in too_big:
            print(f"  too big   {describe(m)}")
        for m in unknown:
            print(f"  unknown   {describe(m)}")
        return 0

    question = " ".join(args.question).strip()
    conn = open_readonly(args.db)

    try:
        if args.schema:
            print(schema_context(conn))
            return 0

        source, raw_sql = None, None
        if args.example:
            if args.example not in EXAMPLE_INDEX:
                print(f"No such example: {args.example}\nAvailable: "
                      + ", ".join(EXAMPLE_INDEX), file=sys.stderr)
                return 2
            q, raw_sql = EXAMPLE_INDEX[args.example]
            source = f"example {args.example}: {q}"
        elif args.sql:
            raw_sql, source = args.sql, "your SQL"
        elif not question:
            ap.print_help()
            return 2

        # -- the model path ------------------------------------------------
        if raw_sql is None:
            found = find_model(args.endpoint, args.model, args.max_params,
                               args.prefer, args.force)
            if not found:
                print(no_model_message(question, args.endpoint), file=sys.stderr)
                return 3
            endpoint, model, note = found
            schema = schema_context(conn)
            print(f"asking {note} …", file=sys.stderr)
            t0 = time.time()
            try:
                reply = ask_model(endpoint, model, question, schema, args.llm_timeout)
            except Rejected:
                raise
            except Exception as exc:
                print(f"\nThe model at {endpoint} failed: {exc}\n"
                      f"Nothing was run. Use --sql to write the query yourself, "
                      f"or --examples for the hand-written ones.", file=sys.stderr)
                return 3
            print(f"  {model} answered in {time.time() - t0:.1f}s", file=sys.stderr)
            raw_sql = clean_model_sql(reply)
            source = f"{model} via {endpoint}"
            if not raw_sql:
                print(f"\n{model} returned no SQL. It said:\n{reply.strip()[:800]}",
                      file=sys.stderr)
                return 3

        # -- the guard -----------------------------------------------------
        try:
            sql = validate_select(raw_sql, args.limit,
                                  tolerate_trailing_prose=(args.sql is None
                                                           and args.example is None))
        except Rejected as exc:
            print(f"\nRejected ({source}): {exc}\n", file=sys.stderr)
            print("The query that was refused, unchanged:\n", file=sys.stderr)
            for line in raw_sql.strip().splitlines():
                print(f"  {line}", file=sys.stderr)
            print("\nNothing was executed and the database was never opened for "
                  "writing.", file=sys.stderr)
            return 2

        if args.explain or args.example:
            print(f"SQL ({source}):")
            for line in sql.strip().splitlines():
                print(f"  {line}")
            print()

        arm(conn, args.query_timeout)
        t0 = time.time()
        try:
            cols, rows = run_query(conn, sql)
        except Rejected as exc:
            print(f"\nCould not run it: {exc}\n", file=sys.stderr)
            if not (args.explain or args.example):
                print("The query was:\n", file=sys.stderr)
                for line in sql.strip().splitlines():
                    print(f"  {line}", file=sys.stderr)
            return 2
        show(sql, cols, rows, time.time() - t0, args.limit)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
