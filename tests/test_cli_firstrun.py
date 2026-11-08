"""
The first minute with the command: what someone sees before they have any data.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support

from finances import cli


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["finances", *argv]), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class FirstRunTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self.cwd)

    def test_reading_commands_never_create_an_empty_store(self):
        for cmd in ("check", "wrapped", "export", "budget"):
            with self.subTest(cmd=cmd):
                code, _, err = run(cmd)
                self.assertEqual(code, 1)
                self.assertIn("finances demo", err)
                self.assertFalse(os.path.exists("finances.db"))

    def test_a_named_store_that_is_missing_says_its_name(self):
        code, _, err = run("check", "--db", "elsewhere.db")
        self.assertEqual(code, 1)
        self.assertIn("no elsewhere.db here", err)

    def test_a_typo_gets_a_suggestion(self):
        code, _, err = run("wraped")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean 'finances wrapped'?", err)

    def test_help_starts_with_how_to_begin(self):
        code, out, _ = run()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("finances — see where your money goes"))
        self.assertLess(out.index("finances demo"), out.index("finances check"))


if __name__ == "__main__":
    unittest.main()
