import datetime
import unittest

from aoiro import db, importer, ledger, xlsx


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")

    def tearDown(self):
        self.c.close()

    def test_template_round_trip(self):
        data = importer.template(self.c)
        parsed = importer.parse(self.c, data, "t.xlsx")
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(len(parsed["entries"]), 4)
        receipt = parsed["entries"][1]
        self.assertEqual(len(receipt["lines"]), 3)
        fee = [l for l in receipt["lines"] if l["account"] == "620"][0]
        self.assertEqual((fee["amount"], fee["tax"]), (440, "P10"))
        sale = parsed["entries"][0]["lines"]
        self.assertEqual([(l["account"], l["tax"]) for l in sale], [("120", "NA"), ("400", "S10")])

        ids = importer.commit(self.c, parsed, 2026)
        self.assertEqual(len(ids), 4)
        opening = ledger.get_opening(self.c, 2026)
        self.assertEqual(opening["300"], 1000000 + 2750000 - 50000)
        self.assertEqual(ledger.opening_balanced(self.c, 2026), 0)

        again = importer.parse(self.c, data, "t.xlsx")
        self.assertTrue(again.get("already_imported"))
        self.assertTrue(all(e.get("duplicate") for e in again["entries"]))
        self.assertEqual(importer.commit(self.c, again, 2026), [])

    def test_csv_shift_jis_and_continuation_rows(self):
        text = ("日付,取引先,摘要,借方科目,借方金額,貸方科目,貸方金額,税区分,インボイス\r\n"
                "令和8年5月1日,B社,入金,普通預金,\"99,560\",売掛金,100000,,\r\n"
                ",,,振込手数料,440,,,10%,あり\r\n"
                "2026-05-02,店,本,書籍代,\"￥3,300\",事業主借,,軽減8%,なし\r\n")
        parsed = importer.parse(self.c, text.encode("cp932"), "a.csv")
        self.assertEqual(parsed["errors"], [])
        first, second = parsed["entries"]
        self.assertEqual(first["date"], "2026-05-01")
        self.assertEqual(sum(l["amount"] for l in first["lines"] if l["side"] == "D"), 100000)
        book = [l for l in second["lines"] if l["side"] == "D"][0]
        self.assertEqual((book["account"], book["amount"], book["tax"], book["invoice"]), ("621", 3300, "P8", "N"))

    def test_errors_block_commit(self):
        rows = [importer.JOURNAL_HEADER,
                ["2026/1/5", "", "", "", "謎の科目", 100, "現金", 100],
                ["2026/1/6", "", "", "", "消耗品費", 100, "現金", 90]]
        parsed = importer.parse(self.c, xlsx.write({"仕訳": rows}), "x.xlsx")
        self.assertEqual(len(parsed["errors"]), 2)
        with self.assertRaises(importer.ImportError_):
            importer.commit(self.c, parsed, 2026)
        self.assertEqual(ledger.search(self.c), [])

    def test_file_saved_by_excel_library(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl なし")
        import io
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "仕訳"
        ws.append(["メモ欄", "", ""])
        ws.append(importer.JOURNAL_HEADER)
        ws.append([datetime.date(2026, 6, 30), None, "A社", "売上", "売掛金", 2750000, "売上高", 2750000])
        op = wb.create_sheet("期首残高")
        op.append(["勘定科目", "期首残高"])
        op.append(["普通預金", 500000])
        buf = io.BytesIO()
        wb.save(buf)
        parsed = importer.parse(self.c, buf.getvalue(), "o.xlsx")
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["entries"][0]["date"], "2026-06-30")
        self.assertEqual(parsed["opening"], {"110": 500000})

    def test_excel_date_serial(self):
        self.assertEqual(importer.parse_date(46106.0), datetime.date(2026, 3, 25))


if __name__ == "__main__":
    unittest.main()
