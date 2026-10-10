import json
import sqlite3
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from aoiro import ctax, db, ledger, reports, web, yearend


def L(side, account, amount, tax=None, invoice="Q", memo=""):
    return {"side": side, "account": account, "amount": amount, "tax": tax, "invoice": invoice, "memo": memo}


class BooksTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        ledger.set_opening(self.c, 2026, {"110": 500000, "300": 500000})
        for month in (3, 6, 9, 12):
            ledger.post_entry(self.c, f"2026-{month:02d}-25", [L("D", "120", 2750000), L("C", "400", 2750000)],
                              "株式会社A", "業務委託料")
            ledger.post_entry(self.c, f"2026-{month:02d}-30", [L("D", "110", 2750000), L("C", "120", 2750000)],
                              "株式会社A", "入金")
        ledger.post_entry(self.c, "2026-01-31", [L("D", "615", 1200000), L("C", "110", 1200000)], "大家", "家賃")
        ledger.post_entry(self.c, "2026-04-01", [L("D", "164", 330000), L("C", "110", 330000)], "家電店", "PC")
        ledger.post_entry(self.c, "2026-02-01", [L("D", "604", 120000), L("C", "310", 120000)], "通信会社", "携帯")
        ledger.post_entry(self.c, "2026-05-10", [L("D", "613", 110000, invoice="N"), L("C", "110", 110000)], "個人B")
        ledger.post_entry(self.c, "2026-11-10", [L("D", "613", 110000, invoice="N"), L("C", "110", 110000)], "個人B")
        ledger.post_entry(self.c, "2026-12-01", [L("D", "190", 1000000), L("C", "110", 1000000)], "", "生活費")
        yearend.save_asset(self.c, {"name": "PC", "account_code": "164", "acquired": "2026-04-01",
                                    "cost": "330000", "life": "4", "business_ratio": "100"})

    def tearDown(self):
        self.c.close()

    def test_unbalanced_entry_rejected(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.post_entry(self.c, "2026-01-01", [L("D", "609", 1000), L("C", "110", 999)])

    def test_history_is_append_only_and_verifiable(self):
        eid = ledger.post_entry(self.c, "2026-07-01", [L("D", "609", 5500), L("C", "110", 5500)], "店", "文具")
        with self.assertRaises(ledger.LedgerError):
            ledger.update_entry(self.c, eid, "2026-07-01", [L("D", "609", 6600), L("C", "110", 6600)], "店", "文具", "")
        ledger.update_entry(self.c, eid, "2026-07-02", [L("D", "609", 6600), L("C", "110", 6600)], "店", "文具", "金額誤り")
        ledger.delete_entry(self.c, eid, "二重計上")
        h = ledger.history(self.c, eid)
        self.assertEqual([x["op"] for x in h], ["create", "update", "delete"])
        self.assertEqual(h[0]["snapshot"]["lines"][0]["amount"], 5500)
        self.assertEqual(ledger.verify(self.c), [])
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE entry_history SET reason = 'x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("DELETE FROM entry_history")
        # 履歴を経由しない直接変更は検証で検出される
        self.c.execute("UPDATE lines SET amount = 1 WHERE entry_id = 1 AND line_no = 1")
        self.assertTrue(ledger.verify(self.c))

    def test_search_by_date_amount_partner(self):
        r = ledger.search(self.c, date_from="2026-06-01", date_to="2026-09-30", amount_min=2000000,
                          amount_max=3000000, partner="株式会社A")
        self.assertEqual(len(r), 4)
        self.assertEqual(len(ledger.search(self.c, year=2026, amount_min=100000, amount_max=110000)), 2)

    def test_depreciation(self):
        dep = yearend.depreciation(yearend.fixed_assets(self.c)[0], 2026)
        self.assertEqual(dep["rate"], 250)
        self.assertEqual(dep["depreciation"], 61875)
        later = yearend.depreciation(yearend.fixed_assets(self.c)[0], 2030)
        self.assertEqual(later["closing_book"], 1)
        self.assertEqual(yearend.straight_line_rate(6), 167)
        self.assertEqual(yearend.straight_line_rate(15), 67)

    def test_year_end_and_reports(self):
        yearend.post_depreciation(self.c, 2026)
        yearend.post_kaji(self.c, 2026, "615", 30)
        with self.assertRaises(ledger.LedgerError):
            yearend.post_kaji(self.c, 2026, "615", 30)
        t = ctax.compute(self.c, 2026, method="general")
        self.assertEqual(t["base10"], 10000000)
        self.assertEqual(t["sales_tax"], 780000)
        self.assertEqual(t["general_deduction"], 57436 + 6240 + 3900)
        self.assertEqual(t["national"], 712400)
        self.assertEqual(t["local"], 200900)  # 712,400 × 22/78 → 百円未満切捨て
        simple = ctax.compute(self.c, 2026, method="simplified", category=5)
        self.assertEqual(simple["deduction"], 390000)
        yearend.post_ctax_accrual(self.c, 2026, t["total"])

        pl = reports.profit_loss(self.c, 2026)
        self.assertEqual(pl["sales"], 11000000)
        expenses = {e["name"]: e["amount"] for e in pl["expenses"]}
        self.assertEqual(expenses["地代家賃"], 360000)
        self.assertEqual(expenses["減価償却費"], 61875)
        self.assertEqual(expenses["租税公課"], 913300)
        self.assertEqual(expenses["外注工賃"], 220000)
        pre = 11000000 - 360000 - 61875 - 913300 - 220000 - 120000
        self.assertEqual(pl["pre_income"], pre)
        self.assertEqual(pl["blue_deduction"], 650000)
        bs = reports.balance_sheet(self.c, 2026)
        self.assertTrue(bs["balanced"], bs)
        rows, d, c = reports.trial_balance(self.c, 2026)
        self.assertEqual(d, c)
        self.assertEqual(reports.monthly(self.c, 2026)["sales"][2], 2750000)

        opening = yearend.close_year(self.c, 2026)
        with self.assertRaises(ledger.LedgerError):
            ledger.post_entry(self.c, "2026-12-31", [L("D", "609", 100), L("C", "110", 100)])
        self.assertEqual(ledger.opening_balanced(self.c, 2027), 0)
        # 元入金 = 500,000 + 所得 + 事業主借 120,000 + 家事按分 840,000 分の事業主貸 − 事業主貸
        self.assertEqual(opening["300"], 500000 + pre + 120000 - 1000000 - 840000)
        self.assertNotIn("190", opening)
        self.assertEqual(opening["210"], 913300)
        self.assertTrue(reports.balance_sheet(self.c, 2027)["balanced"])


class WebSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile, os
        cls.tmp = tempfile.mkdtemp()
        cls.app = web.App(os.path.join(cls.tmp, "b.sqlite3"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode("utf-8")

    def post(self, path, pairs):
        data = urllib.parse.urlencode([("_token", self.app.token)] + pairs).encode()
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data)) as r:
            return r.geturl(), r.read().decode("utf-8")

    def test_pages_and_entry(self):
        url, body = self.post("/entry/new", [
            ("date", "2026-03-25"), ("partner", "株式会社A"), ("description", "売上"),
            ("side", "D"), ("account", "120"), ("amount", "2750000"), ("tax", ""), ("invoice", "Q"), ("memo", ""),
            ("side", "C"), ("account", "400"), ("amount", "2750000"), ("tax", ""), ("invoice", "Q"), ("memo", ""),
            ("side", "D"), ("account", ""), ("amount", ""), ("tax", ""), ("invoice", "Q"), ("memo", ""),
        ])
        self.assertIn("/entry/", url)
        self.assertIn("登録しました", body)
        for path in ["/", "/entries", "/entries?partner=A&min=1", "/reports/journal", "/reports/ledger?code=400",
                     "/reports/ledger?all=1", "/reports/trial", "/reports/pl", "/reports/bs", "/reports/monthly",
                     "/reports/depr", "/reports/ctax", "/reports/itax", "/opening", "/assets", "/yearend", "/settings",
                     "/accounts", "/verify", "/audit", "/entry/1", "/entry/1/edit", "/entry/new"]:
            with self.subTest(path=path):
                self.assertIn("</html>", self.get(path))
        self.assertIn("2,750,000", self.get("/reports/pl"))
        csv_body = self.get("/export/journal.csv?y=2026")
        self.assertIn("株式会社A", csv_body)
        _, body = self.post("/entry/1/edit", [("date", "2026-03-25"), ("side", "D"), ("account", "120"),
                                              ("amount", "2750000"), ("side", "C"), ("account", "400"),
                                              ("amount", "2750000"), ("reason", "")])
        self.assertIn("訂正理由を入力してください", body)

    def test_bad_token_rejected(self):
        req = urllib.request.Request(self.base + "/settings", data=b"_token=x&business_name=y")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
