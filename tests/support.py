"""
Shared scaffolding for the test suite.

Every test module imports this first. It does two jobs that would otherwise be
copy-pasted into each file: it puts the repository root on ``sys.path`` so the
tests can ``import utils`` the same way the real tools do, and it hands out the
small builders (a throwaway SQLite store, a transaction factory) that the
database and engine tests need.

The suite is stdlib-only and must stay that way — ``pytest`` is not a
dependency of this project. Run it with::

    .venv/bin/python -m unittest discover tests -v
    .venv/bin/python tests/run.py
"""

from __future__ import annotations

import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
GOLDEN_DIR = os.path.join(TESTS_DIR, "golden")
PDF_DIR = os.path.join(REPO_ROOT, "PDFs")

# Import the modules under test the way the shipped tools do, not as a package.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def has_pdfplumber() -> bool:
    """True when the one third-party dependency is importable."""
    try:
        import pdfplumber  # noqa: F401
    except Exception:
        return False
    return True


def pdf_paths() -> list[str]:
    """Every statement PDF on disk, sorted, or an empty list if none are here."""
    if not os.path.isdir(PDF_DIR):
        return []
    return sorted(os.path.join(PDF_DIR, f) for f in os.listdir(PDF_DIR)
                  if f.lower().endswith(".pdf"))


class TempDirMixin:
    """Gives a test case a scratch directory that is removed afterwards.

    The database tests write real SQLite files (WAL mode leaves ``-wal`` and
    ``-shm`` siblings behind), so a directory is cleaner to manage than a
    NamedTemporaryFile.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="finances-tests-")
        self.tmpdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def tmp_path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)


def txn(date, amount, description="THING", **kw):
    """A :class:`models.Transaction` with sensible defaults for a test row.

    Positional args are the two fields that carry meaning in nearly every
    assertion; everything else is keyword so the intent of a fixture stays
    readable at the call site.
    """
    from finances.models import Transaction

    kw.setdefault("category", "Shopping")
    kw.setdefault("account_last4", "1111")
    kw.setdefault("source_file", "fixture.pdf")
    return Transaction(date=date, description=description, amount=amount, **kw)


def stmt(source_file, **kw):
    """A :class:`models.Statement` for a test row."""
    from finances.models import Statement

    kw.setdefault("issuer", "TestBank")
    kw.setdefault("account_last4", "1111")
    return Statement(source_file=source_file, **kw)
