"""
banks/ — one file per bank: how to recognise its statements, what is odd about
them, and where to download them.

    name = "Chase"
    statements = 'chase\\.com|jpmorgan'     # a card or bank statement names it this way
    investments = '...'                    # a brokerage statement names it this way
    notes = ["what is unusual about this bank's layout"]

    [guide]                                # how to get the statements (the dashboard shows it)
    url = "https://..."
    steps = ["Sign in", "..."]

The patterns are lowercase regular expressions searched in the statement's
text; whichever bank is named first on the page wins. A bank is added with
this file and a made-up statement layout in ``tests/fixtures/banks/`` with the
same name (see CONTRIBUTING.md), and the test suite checks that the layout is
read as this bank and reconciles.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE_KEYS = ("url", "help", "steps", "exports", "history", "bulk", "closes")


@lru_cache(maxsize=1)
def load() -> tuple[dict, ...]:
    out = []
    for f in sorted(os.listdir(HERE)):
        if f.endswith(".toml"):
            with open(os.path.join(HERE, f), "rb") as fh:
                bank = tomllib.load(fh)
            bank["slug"] = f[:-5]
            out.append(bank)
    return tuple(out)


def issuers() -> list[tuple[str, str]]:
    """(name, pattern) for card and bank statements."""
    return [(b["name"], b["statements"]) for b in load() if b.get("statements")]


def investment_issuers() -> list[tuple[str, str]]:
    """(name, pattern) for brokerage statements."""
    return [(b["name"], b["investments"]) for b in load() if b.get("investments")]


def guides() -> dict[str, dict]:
    """Bank name -> how to download its statements."""
    return {b["name"]: {k: b["guide"].get(k) for k in GUIDE_KEYS}
            for b in load() if b.get("guide")}
