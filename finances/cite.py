"""
cite.py — check every amount in an answer against what the lookups returned.

A model is told to take its numbers from the tools, but nothing makes it. So
after it answers, each amount it wrote is looked for in the tool results it was
given, and the answer comes back with a citation per amount:

  - **row**      the amount of one transaction: links to the statement page it
                 was read from, and the line as printed.
  - **lookup**   a figure a lookup returned (a month's total, a category).
  - **derived**  the sum or difference of two figures from the lookups
                 ("$293 more than September").
  - **missing**  not in anything the model was shown. Rounded or not, it did
                 not come from the data, and the page says so.

Then a verdict for the whole answer: *verified* when every amount was found and
every statement in the store reconciles against its printed totals, *found*
when every amount was found but some statements could not be checked or do not
agree, *unverified* when any amount was not found. This does not need the model
to cooperate, so it works the same for Claude, OpenAI and a small local model.

Standard library only.
"""

from __future__ import annotations

import json
import re

# $1,234.56  -$12  € 9.99  £1,200  $3.7k   (the currency symbol is required: a
# bare "12" is as likely a count of days as an amount of money)
MONEY = re.compile(
    r"(?P<neg>[-−])?\s?(?P<sym>[$€£])\s?(?P<num>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?P<dec>\.\d+)?(?P<k>\s?[kK]\b)?")

MAX_VALUES = 400        # figures considered for sums and differences
# Numbers in a tool result that are not money: a count of 40 rows must not
# vouch for "$40".
NOT_MONEY = re.compile(r"count|^n$|^n_|days|months|^month|year|pct|percent|share|"
                       r"ratio|apr|rate|seq|page|rank|^id$|score|cadence|interval", re.I)
CENT = 0.005


def amounts(text: str) -> list[dict]:
    """Every amount of money written in ``text``, with where it is."""
    out = []
    for m in MONEY.finditer(text or ""):
        value = float(m["num"].replace(",", "") + (m["dec"] or ""))
        k = bool(m["k"])
        if k:
            value *= 1000
        # How precisely it was written decides how close the data must be:
        # "$3,680.23" must match to the cent, "$3,680" to the dollar, "$3.7k"
        # to the nearest hundred.
        if k:
            step = 10 ** -len((m["dec"] or ".")[1:]) * 1000
        elif m["dec"]:
            step = 10 ** -len(m["dec"][1:])
        else:
            step = 1.0
        out.append({"text": m.group(0).strip(), "start": m.start("sym") if not m["neg"]
                    else m.start("neg"), "end": m.end(), "value": value,
                    "tolerance": step / 2 + CENT})
    return out


def _walk(node, step: int, tool: str, out: list, parent=None):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                if NOT_MONEY.search(k):
                    continue
                out.append({"value": abs(float(v)), "key": k, "step": step, "tool": tool,
                            "row": node if _is_row(node) and k == "amount" else None})
            else:
                _walk(v, step, tool, out, node)
    elif isinstance(node, list):
        for v in node:
            _walk(v, step, tool, out, parent)


def _is_row(d: dict) -> bool:
    return "amount" in d and "source_file" in d and "date" in d


def figures(results: list[tuple[str, str]]) -> list[dict]:
    """Every number in the tool results: (tool name, JSON text) per call."""
    out: list[dict] = []
    for i, (tool, text) in enumerate(results):
        try:
            _walk(json.loads(text), i, tool, out)
        except (ValueError, TypeError):
            continue
    return [f for f in out if f["value"] >= 0.01]


def _source(f: dict) -> dict:
    src = {"kind": "row" if f["row"] else "lookup", "tool": f["tool"], "step": f["step"]}
    if f["row"]:
        r = f["row"]
        src.update({"file": r["source_file"], "page": r.get("page") or 0,
                    "date": r.get("date"), "merchant": r.get("merchant") or r.get("description"),
                    "amount": r.get("amount")})
    return src


def check(answer: str, results: list[tuple[str, str]], coverage: dict | None = None) -> dict:
    """Citations for each amount in ``answer`` and a verdict for the whole of it.

    ``coverage`` is the store's {"statements", "reconciled", "mismatched",
    "unchecked"}; without it an answer can be "found" but never "verified".
    """
    figs = figures(results)
    # Transaction rows first: an amount that is one charge links to its line.
    figs.sort(key=lambda f: f["row"] is None)
    pool = figs[:MAX_VALUES]
    cites = []
    for a in amounts(answer):
        hit = next((f for f in figs if abs(f["value"] - a["value"]) <= a["tolerance"]), None)
        cite = {k: a[k] for k in ("text", "start", "end", "value")}
        if hit:
            cite.update(status="found", source=_source(hit))
        else:
            pair = _derived(a, pool)
            if pair:
                x, y, op = pair
                cite.update(status="found", source={
                    "kind": "derived", "op": op, "of": [x["value"], y["value"]],
                    "tool": x["tool"], "step": x["step"]})
            else:
                cite.update(status="missing")
        cites.append(cite)
    return {"citations": cites, "verdict": verdict(cites, coverage)}


def _derived(a: dict, pool: list[dict]):
    """Two figures whose sum or difference is this amount, if any."""
    want, tol = a["value"], a["tolerance"]
    for i, x in enumerate(pool):
        for y in pool[i + 1:]:
            if abs(abs(x["value"] - y["value"]) - want) <= tol:
                return (x, y, "difference") if x["value"] >= y["value"] else (y, x, "difference")
            if abs(x["value"] + y["value"] - want) <= tol:
                return x, y, "sum"
    return None


def verdict(cites: list[dict], coverage: dict | None) -> dict:
    total = len(cites)
    missing = sum(1 for c in cites if c["status"] == "missing")
    cov = coverage or {}
    shaky = (cov.get("mismatched") or 0) + (cov.get("unchecked") or 0)
    if total == 0:
        level, text = "none", "No amounts to check in this answer."
    elif missing:
        level = "unverified"
        text = (f"{missing} of {total} amount{'s' if total != 1 else ''} "
                f"{'is' if missing == 1 else 'are'} not in your data. Treat "
                f"{'it' if missing == 1 else 'them'} as the model's own.")
    elif coverage is None or shaky:
        level = "found"
        text = (f"Every amount is in your data"
                + (f", but {shaky} statement{'s' if shaky != 1 else ''} could not be "
                   "checked against its printed totals." if shaky else "."))
    else:
        level = "verified"
        text = ("Every amount is in your data, and every statement behind it "
                "reconciles against its printed totals.")
    return {"level": level, "text": text, "amounts": total, "missing": missing}
