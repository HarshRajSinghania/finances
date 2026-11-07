"""
Tests for mcp_server.py — the store as tools for AI assistants.

The server speaks newline-delimited JSON-RPC on stdin and stdout, so these
tests drive it exactly the way Claude Desktop does: write messages in, read
replies out. What matters is that the handshake is right, every tool answers
with numbers from the store, nothing can write to it, and stdout carries
nothing but protocol.
"""

from __future__ import annotations

import io
import json
import unittest

import support

from finances.database import FinanceDB
from finances.mcp_server import TOOLS, Server


def session(server, *messages):
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(server, name, **args):
    [reply] = session(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
    return reply["result"]


class McpTest(support.TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp_path("m.db")
        rows = [support.txn(f"2026-{m:02d}-03", 15.49, "NETFLIX.COM", merchant="NETFLIX",
                            category="Subscriptions") for m in range(1, 7)]
        rows += [support.txn("2026-03-10", 4.50, "BLUE BOTTLE", merchant="BLUE BOTTLE",
                             category="Coffee"),
                 support.txn("2026-03-12", -500.0, "AUTOPAY", category="Payment")]
        with FinanceDB(self.path) as db:
            db.upsert_transactions(rows)
        self.server = Server(self.path)

    def test_handshake_and_tool_list(self):
        init, tools = session(
            self.server,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "finances")
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertEqual(names, [n for n, _, _ in TOOLS])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools["result"]["tools"]))

    def test_an_unknown_protocol_version_gets_the_latest(self):
        [init] = session(self.server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")

    def test_spending_leaves_out_card_payments(self):
        r = call(self.server, "spending_by_category", month="2026-03")
        data = r["structuredContent"]
        self.assertEqual(data["total"], 19.99)
        self.assertEqual({c["category"] for c in data["categories"]}, {"Subscriptions", "Coffee"})
        self.assertEqual(json.loads(r["content"][0]["text"]), data)

    def test_search(self):
        data = call(self.server, "search_transactions", query="netflix", limit=2)["structuredContent"]
        self.assertEqual((data["matches"], data["shown"]), (6, 2))
        self.assertEqual(data["transactions"][0]["date"], "2026-06-03")

    def test_every_tool_answers(self):
        for name, _, schema in TOOLS:
            args = {"sql": "SELECT COUNT(*) AS n FROM transactions"} if name == "query" else {}
            with self.subTest(tool=name):
                r = call(self.server, name, **args)
                if name == "cashflow_forecast":
                    # No checking statement here, so it must ask rather than guess.
                    self.assertIn("starting_balance", r["structuredContent"]["hint"])
                else:
                    self.assertFalse(r.get("isError"), r)

    def test_query_cannot_write(self):
        for sql in ("DELETE FROM transactions", "DROP TABLE transactions",
                    "SELECT 1; DELETE FROM transactions"):
            with self.subTest(sql=sql):
                self.assertTrue(call(self.server, "query", sql=sql)["isError"])
        with FinanceDB(self.path) as db:
            self.assertEqual(db.count(), 8)

    def test_bad_input_is_a_tool_error_not_a_crash(self):
        r = call(self.server, "spending_by_category", month="March")
        self.assertTrue(r["isError"])
        self.assertIn("2026-03", r["content"][0]["text"])

    def test_missing_store_says_how_to_make_one(self):
        r = call(Server(self.tmp_path("nope.db")), "summary")
        self.assertTrue(r["isError"])
        self.assertIn("finances parse", r["content"][0]["text"])

    def test_unknown_method_and_garbage(self):
        out = io.StringIO()
        self.server.serve(io.StringIO('not json\n{"jsonrpc":"2.0","id":3,"method":"nope"}\n'), out)
        bad, unknown = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(bad["error"]["code"], -32700)
        self.assertEqual(unknown["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
