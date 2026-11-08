"""
sources.py — getting statements in: which are due, where to get them, and a
folder watched for new ones.

Statements are monthly, so the store always knows when the next one should
exist: about a month after the last one closed, and a few days more before the
bank has it ready. ``due()`` lists every account whose next statement should be
downloadable by now, with the bank's own statements page and how to get there
(``banks.json``, checked against each bank's help pages).

``Watcher`` looks in a folder (Downloads, by default) for files that arrived
after watching was turned on, and adds the ones that are statements or bank
exports, the same way the upload box does. Anything else in the folder (a
receipt, a boarding pass, a spreadsheet) is left alone: a PDF has to read like
a statement, and a CSV has to have a date and an amount column.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
READY_AFTER = 34          # days after a statement closes until the next is usually out
WATCH_KEY = "watch"       # settings: {"folder", "since", "seen": {name: [size, mtime]}, "log"}
POLL_SECONDS = 20
EXPORTS = (".csv", ".tsv", ".ofx", ".qfx", ".qbo", ".xml", ".sta", ".mt940", ".940")
STATEMENT_WORDS = re.compile(
    r"statement\s+(period|date|closing)|closing\s+date|account\s+summary|"
    r"new\s+balance|ending\s+balance|beginning\s+balance|account\s+value|"
    r"payment\s+due\s+date|opening\s+balance|kontoauszug|relev[ée]", re.I)


@lru_cache(maxsize=1)
def banks() -> dict:
    try:
        with open(os.path.join(HERE, "banks.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def guide(issuer: str) -> dict | None:
    """How to download statements from this bank, if it is one we know."""
    known = banks()
    if issuer in known:
        return {"bank": issuer, **known[issuer]}
    low = (issuer or "").lower()
    for name, g in known.items():
        if name.lower() in low or low in name.lower():
            return {"bank": name, **g}
    return None


def due(statements, today: date | None = None) -> list[dict]:
    """Each account, its latest statement, and whether the next should be out."""
    today = today or date.today()
    latest: dict[tuple, object] = {}
    for s in statements:
        if not s.period_end or s.source_format in ("simplefin",):
            continue
        key = (s.issuer, s.account_last4)
        if key not in latest or s.period_end > latest[key].period_end:
            latest[key] = s
    out = []
    for (issuer, last4), s in sorted(latest.items(), key=lambda kv: kv[1].period_end):
        closed = date.fromisoformat(s.period_end[:10])
        expected = closed + timedelta(days=READY_AFTER)
        missing = max(0, (today - closed).days // 31)
        out.append({"issuer": issuer, "last4": last4, "lastClosed": s.period_end[:10],
                    "expected": expected.isoformat(), "ready": today >= expected,
                    "missing": missing, "guide": guide(issuer)})
    return out


# --------------------------------------------------------------------------
# What counts as a statement
# --------------------------------------------------------------------------

def looks_like_statement(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _pdf_is_statement(path)
    if ext in (".ofx", ".qfx", ".qbo", ".sta", ".mt940", ".940"):
        return True
    if ext == ".xml":
        with open(path, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
        return "camt.053" in head or "BkToCstmrStmt" in head
    if ext in (".csv", ".tsv"):
        from finances.lineitems import kind_of_file
        return _csv_is_transactions(path) or bool(kind_of_file(path))
    return False


def _pdf_is_statement(path: str) -> bool:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    except Exception:                                    # noqa: BLE001
        return False
    if not text.strip():
        # No text layer: a scan. Only worth trying if it is named like one.
        return bool(re.search(r"statement|estatement|stmt", os.path.basename(path), re.I))
    from finances.parse import detect_issuer
    hits = len(STATEMENT_WORDS.findall(text))
    return hits >= 2 or (hits >= 1 and detect_issuer(text) != "Unknown")


def _csv_is_transactions(path: str) -> bool:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline().lower()
    except OSError:
        return False
    return "date" in header and bool(re.search(r"amount|debit|credit|outflow|inflow", header))


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------

class Watcher:
    """Adds statements that land in a folder. One per server, in a thread."""

    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.error = ""

    # -- state, kept in the store so it survives a restart -----------------
    def state(self) -> dict:
        from finances.database import FinanceDB
        with FinanceDB(self.api.db_path) as db:
            st = db.get_setting(WATCH_KEY, {}) or {}
        return st if isinstance(st, dict) else {}

    def _save(self, st: dict):
        from finances.database import FinanceDB
        with FinanceDB(self.api.db_path) as db:
            db.set_setting(WATCH_KEY, st)

    def status(self) -> dict:
        st = self.state()
        return {"folder": st.get("folder") or "", "on": bool(st.get("folder")),
                "since": st.get("since"), "suggested": default_folder(),
                "log": (st.get("log") or [])[-10:], "error": self.error}

    def set(self, folder: str | None) -> dict:
        st = self.state()
        if folder:
            folder = os.path.abspath(os.path.expanduser(folder))
            if not os.path.isdir(folder):
                raise ValueError(f"{folder} is not a folder")
            # Only what arrives from now on: a Downloads folder holds years of
            # other files, and quietly importing old ones would surprise.
            st = {"folder": folder, "since": time.time(), "seen": {}, "log": st.get("log", [])}
        else:
            st = {"log": st.get("log", [])}
        self._save(st)
        self.start()
        return self.status()

    # -- the loop -------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="watch")
        self.thread.start()

    def _run(self):
        while True:
            if not self.state().get("folder"):
                return
            try:
                self.scan()
                self.error = ""
            except PermissionError:
                self.error = ("This app is not allowed to read that folder. On a Mac, "
                              "allow your terminal under System Settings > Privacy & "
                              "Security > Files and Folders.")
            except Exception as e:                       # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            time.sleep(POLL_SECONDS)

    def scan(self) -> list[dict]:
        """Add whatever new statements are in the folder. Returns what happened."""
        with self.lock:
            st = self.state()
            folder = st.get("folder")
            if not folder:
                return []
            seen, since, done = st.get("seen", {}), st.get("since", 0), []
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                ext = os.path.splitext(name)[1].lower()
                if name.startswith(".") or ext not in (".pdf", *EXPORTS) or not os.path.isfile(path):
                    continue
                info = os.stat(path)
                mark = [info.st_size, int(info.st_mtime)]
                if info.st_mtime < since or seen.get(name) == mark:
                    continue
                # A download still being written grows between scans; wait for it.
                if time.time() - info.st_mtime < 3:
                    continue
                seen[name] = mark
                if not looks_like_statement(path):
                    continue
                with open(path, "rb") as fh:
                    result = self.api.upload(name, fh.read())
                entry = {"file": name, "ok": bool(result.get("ok")), "at": int(time.time()),
                         "transactions": result.get("transactionCount"),
                         "error": result.get("error")}
                done.append(entry)
                st.setdefault("log", []).append(entry)
            st["seen"], st["log"] = seen, st.get("log", [])[-50:]
            self._save(st)
            return done


def default_folder() -> str:
    path = os.path.join(os.path.expanduser("~"), "Downloads")
    return path if os.path.isdir(path) else ""


def main() -> int:
    """finances watch [FOLDER]: add new statements from a folder as they arrive."""
    import argparse
    from finances.api import Api
    from finances.database import DEFAULT_DB
    ap = argparse.ArgumentParser(
        prog="finances watch",
        description="Add statements and bank exports to the store as they land in a "
                    "folder (Downloads by default). Only files that arrive after you "
                    "start count; anything that is not a statement is left alone.")
    ap.add_argument("folder", nargs="?", default=default_folder())
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--stop", action="store_true",
                    help="stop watching (the dashboard watches too, while it runs)")
    args = ap.parse_args()
    api = Api(args.db)
    if args.stop:
        api.watcher.set(None)
        print("stopped watching")
        return 0
    if not args.folder:
        ap.error("name a folder to watch")
    status = api.watcher.set(args.folder)
    print(f"watching {status['folder']} for new statements. Ctrl-C to stop.")
    try:
        while True:
            for e in api.watcher.scan():
                print(f"  {'added ' if e['ok'] else 'could not read'} {e['file']}"
                      + (f"  ({e['transactions']} transactions)" if e["ok"] else
                         f"  {e.get('error') or ''}"))
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
