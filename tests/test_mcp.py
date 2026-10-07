import json
import os
import subprocess
import sys
import tempfile
import unittest

from aoiro import db, mcp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        db.set_setting(self.c, "current_year", 2026)

    def tearDown(self):
        self.c.close()

    def call(self, name, **args):
        res = mcp.call_tool(self.c, name, args)
        return res["content"][0]["text"], res.get("isError", False)

    def test_post_search_update_delete(self):
        entry = {"date": "2026-04-02", "partner": "文具店", "description": "コピー用紙",
                 "lines": [{"side": "借方", "account": "消耗品費", "amount": 1100},
                           {"side": "貸方", "account": "事業主借", "amount": 1100}]}
        bad = dict(entry, lines=[dict(entry["lines"][0], amount=1000), entry["lines"][1]])
        text, err = self.call("post_entries", entries=[entry, bad])
        self.assertTrue(err)
        self.assertIn("2件目", text)
        self.assertIn("該当する仕訳はありません", self.call("search_entries")[0])

        text, err = self.call("post_entries", entries=[entry], dry_run=True)
        self.assertFalse(err)
        self.assertIn("まだ登録していません", text)
        text, err = self.call("post_entries", entries=[entry])
        self.assertIn("No.1", text)
        self.assertIn("課税仕入10%", text)

        text, _ = self.call("search_entries", partner="文具")
        self.assertIn("コピー用紙", text)
        text, err = self.call("update_entry", year=2026, entry_no=1, description="トナー")
        self.assertTrue(err)  # 理由なし
        text, err = self.call("update_entry", year=2026, entry_no=1, description="トナー", reason="摘要誤り")
        self.assertIn("トナー", text)
        text, err = self.call("delete_entry", year=2026, entry_no=1, reason="二重計上")
        self.assertIn("削除済み", text)

    def test_invoice_and_reports(self):
        db.set_setting(self.c, "owner_name", "山田 太郎")
        text, err = self.call("create_invoice", issue_date="2026-10-02", partner="株式会社テスト",
                              items=[{"date": "2026/10/2", "description": "調査業務", "quantity": 5.6, "unit": "h",
                                      "unit_price": 14706, "rate": 10}])
        self.assertFalse(err, text)
        self.assertIn("ご請求額 82,181", text)
        self.assertIn("未入金", self.call("list_invoices")[0])
        text, err = self.call("record_invoice_payment", number="2026-001", date="2026-10-31", received=82181)
        self.assertFalse(err, text)
        self.assertIn("8,408", self.call("get_report", kind="withholding")[0])
        self.assertIn("90,589", self.call("get_report", kind="profit_loss")[0])
        self.assertIn("一致: はい", self.call("get_report", kind="balance_sheet")[0])
        for kind in ("trial_balance", "monthly", "consumption_tax", "depreciation"):
            self.assertFalse(self.call("get_report", kind=kind)[1])
        self.assertIn("売掛金", self.call("general_ledger", account="売掛金")[0])
        text, _ = self.call("set_opening_balances", balances={"普通預金": 500000, "未払金": 20000})
        self.assertIn("元入金: 480,000", text)
        self.assertIn("勘定科目", self.call("get_overview")[0])

    def test_stdio_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "get_overview", "arguments": {}}},
            ]
            stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8")
            out = subprocess.run([sys.executable, "-m", "aoiro", "--db", os.path.join(tmp, "b.sqlite3"), "mcp"],
                                 input=stdin, capture_output=True, cwd=ROOT, timeout=30, check=True)
            replies = [json.loads(l) for l in out.stdout.decode("utf-8").splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])
            self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "aoiro")
            names = {t["name"] for t in replies[1]["result"]["tools"]}
            self.assertIn("post_entries", names)
            self.assertIn("勘定科目", replies[2]["result"]["content"][0]["text"])

    def test_install_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Claude", "claude_desktop_config.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}, f)
            mcp.install(os.path.join(tmp, "books.sqlite3"), path)
            with open(path, encoding="utf-8") as f:
                config = json.load(f)
            self.assertEqual(config["theme"], "dark")
            self.assertIn("other", config["mcpServers"])
            self.assertEqual(config["mcpServers"]["aoiro"]["args"][-1], "mcp")


if __name__ == "__main__":
    unittest.main()
