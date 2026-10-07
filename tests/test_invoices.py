import base64
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

from aoiro import db, invoices, ledger, pdftext, web


def make_pdf(rows):
    """テスト用: 日本語を含む文字列を指定位置に置いた PDF（Type0 フォント + ToUnicode）を作る。"""
    chars = sorted({ch for _, _, t in rows for ch in t})
    code = {ch: i + 1 for i, ch in enumerate(chars)}
    content = b"BT\n"
    for x, y, t in rows:
        hexs = "".join(f"{code[ch]:04X}" for ch in t)
        content += f"/F1 10 Tf 1 0 0 1 {x} {y} Tm <{hexs}> Tj\n".encode()
    content += b"ET\n"
    cmap = ("/CIDInit /ProcSet findresource begin begincmap\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            f"{len(chars)} beginbfchar\n"
            + "".join(f"<{code[ch]:04X}> <{ord(ch):04X}>\n" for ch in chars)
            + "endbfchar\nendcmap end\n").encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Test /Encoding /Identity-H /ToUnicode 6 0 R >>",
    ]
    out = b"%PDF-1.7\n"
    for i, o in enumerate(objs, 1):
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    for i, data in ((5, zlib.compress(content)), (6, cmap)):
        filt = b" /Filter /FlateDecode" if i == 5 else b""
        out += f"{i} 0 obj\n<< /Length {len(data)}".encode() + filt + b" >>\nstream\n" + data + b"\nendstream\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


SAMPLE_ROWS = [
    (250, 800, "請求書"), (400, 770, "発行日"), (450, 770, "2026/10/2"),
    (40, 740, "株式会社テスト 御中"), (380, 720, "公認会計士 山田 太郎"),
    (40, 690, "ご請求金額（税込）"), (150, 670, "¥82,181"),
    (40, 600, "日付"), (120, 600, "内容"), (400, 600, "金額（税抜）"),
    (40, 585, "2026/10/2"), (120, 585, "調査業務 8月分"), (400, 585, "¥82,354"),
    (420, 500, "小計"), (480, 500, "¥82,354"),
    (40, 485, "税率区分"), (100, 485, "消費税"), (420, 485, "消費税"), (480, 485, "¥8,235"),
    (420, 470, "源泉徴収"), (480, 470, "¥-8,408"),
    (420, 455, "合計"), (480, 455, "¥82,181"),
]

FORM = {"issue_date": "2026-10-02", "partner": "株式会社テスト", "withholding": True,
        "items": [{"date": "2026/10/2", "description": "調査業務 8月分", "quantity": "5.6", "unit": "h",
                   "unit_price": "14706", "rate": "10"}]}


class InvoiceTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")

    def tearDown(self):
        self.c.close()

    def test_calculation_matches_sample_invoice(self):
        calc = invoices.calculate(FORM)
        self.assertEqual((calc["subtotal"], calc["tax"], calc["withholding"], calc["total"]),
                         (82354, 8235, 8408, 82181))
        self.assertEqual(invoices.withholding_tax(1_500_000), 102_100 + 102_100)
        self.assertEqual(invoices.withholding_tax(1_000_000), 102_100)

    def test_issue_pay_and_cancel(self):
        iid = invoices.save(self.c, FORM)
        inv = invoices.get(self.c, iid)
        self.assertEqual(inv["number"], "2026-001")
        lines = {(l["account_code"], l["side"]): l for l in ledger.get_entry(self.c, inv["entry_id"])["lines"]}
        self.assertEqual(lines[("120", "D")]["amount"], 82181)
        self.assertEqual(lines[("190", "D")]["amount"], 8408)
        self.assertEqual(lines[("190", "D")]["memo"], invoices.WITHHOLDING_MEMO)
        self.assertEqual((lines[("400", "C")]["amount"], lines[("400", "C")]["tax"]), (90589, "S10"))

        changed = dict(FORM, items=[dict(FORM["items"][0], quantity="6")])
        invoices.save(self.c, changed, iid, reason="時間数の修正")
        self.assertEqual([h["op"] for h in ledger.history(self.c, inv["entry_id"])], ["create", "update"])
        total = invoices.get(self.c, iid)["total"]

        with self.assertRaises(invoices.InvoiceError):
            invoices.record_payment(self.c, iid, "2026-10-31", total - 1)
        invoices.record_payment(self.c, iid, "2026-10-31", total - 440, 440)
        summary = invoices.withholding_summary(self.c, 2026)
        self.assertEqual(summary[0]["partner"], "株式会社テスト")
        self.assertEqual(summary[0]["withholding"], invoices.withholding_tax(88236))

        invoices.cancel(self.c, iid, "宛先誤り")
        self.assertEqual(ledger.search(self.c, year=2026), [])
        self.assertEqual(ledger.verify(self.c), [])
        self.assertEqual(invoices.save(self.c, FORM), iid + 1)
        self.assertEqual(invoices.get(self.c, iid + 1)["number"], "2026-002")

    def test_read_pdf_and_post_entry(self):
        pdf = make_pdf(SAMPLE_ROWS)
        self.assertIn("源泉徴収 ¥-8,408", pdftext.text(pdf))
        r = invoices.read_pdf(self.c, pdf)
        self.assertEqual((r["direction"], r["date"], r["partner"]), ("sales", "2026-10-02", "株式会社テスト"))
        self.assertEqual((r["subtotal"], r["tax"], r["withholding"], r["total"]), (82354, 8235, 8408, 82181))
        self.assertEqual(r["description"], "調査業務 8月分")
        self.assertEqual(r["warnings"], [])

        eid = invoices.entry_from_document(self.c, {**r, "account": "400"})
        invoices.add_document(self.c, pdf, "a.pdf", "発行請求書", r["date"], r["total"], r["partner"], eid)
        self.assertEqual(len(invoices.search_documents(self.c, amount_min=82000, amount_max=83000, partner="テスト")), 1)
        self.assertIsNotNone(invoices.find_document_by_hash(self.c, pdf))
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("DELETE FROM documents")

        # 自分宛ての請求書は経費として読む
        db.set_setting(self.c, "owner_name", "山田 太郎")
        rows = [(x, y, t.replace("株式会社テスト 御中", "山田 太郎 様").replace("公認会計士 山田 太郎", "株式会社デザイン"))
                for x, y, t in SAMPLE_ROWS if t not in ("源泉徴収", "¥-8,408")]
        rows = [(x, y, "¥90,589" if t == "¥82,181" else t) for x, y, t in rows]
        r = invoices.read_pdf(self.c, make_pdf(rows))
        self.assertEqual((r["direction"], r["partner"], r["total"]), ("expense", "株式会社デザイン", 90589))
        eid = invoices.entry_from_document(self.c, {**r, "account": "613"})
        lines = ledger.get_entry(self.c, eid)["lines"]
        self.assertEqual([(l["account_code"], l["amount"], l["tax"], l["invoice"]) for l in lines],
                         [("613", 90589, "P10", "N"), ("210", 90589, "NA", "Q")])

    def test_mismatched_amounts_rejected(self):
        with self.assertRaises(invoices.InvoiceError):
            invoices.entry_from_document(self.c, {"direction": "sales", "date": "2026-10-02", "subtotal": "100",
                                                  "tax": "10", "withholding": "0", "total": "100", "account": "400"})


class InvoiceWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web.App(os.path.join(tempfile.mkdtemp(), "b.sqlite3"))
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

    def test_invoice_flow(self):
        self.assertIn("請求書の作成", self.get("/invoice/new"))
        pairs = [("issue_date", "2026-10-02"), ("partner", "株式会社テスト"), ("honorific", "御中"), ("withholding", "1"),
                 ("post", "1"), ("owner_name", "山田 太郎"), ("bank_name", "テスト銀行"), ("remember", "1")]
        pairs += [("i_date", "2026/10/2"), ("i_description", "調査業務"), ("i_quantity", "5.6"), ("i_unit", "h"),
                  ("i_unit_price", "14706"), ("i_rate", "10")]
        pairs += [(k, "") for k in ("i_date", "i_description", "i_quantity", "i_unit", "i_unit_price")] + [("i_rate", "10")]
        url, body = self.post("/invoice/new", pairs)
        self.assertIn("/print", url)
        for text in ("株式会社テスト 御中", "¥82,181", "¥-8,408", "¥8,235", "テスト銀行", "山田 太郎"):
            self.assertIn(text, body)
        iid = url.split("/invoice/")[1].split("/")[0]
        self.assertIn("2026-001", self.get("/invoices"))
        self.assertIn("山田 太郎", self.get("/invoice/new"))  # 発行者が次回の既定値になる
        url, body = self.post(f"/invoice/{iid}/paid", [("date", "2026-10-31"), ("received", "82181"), ("fee", "0"),
                                                      ("account", "110")])
        self.assertIn("入金済み", body)
        self.assertIn("8,408", self.get("/reports/withholding"))

        pdf = make_pdf(SAMPLE_ROWS)
        b64 = base64.b64encode(pdf).decode()
        _, body = self.post("/invoices/pdf", [("action", "preview"), ("data", b64), ("filename", "x.pdf")])
        self.assertIn('value="82181"', body)
        url, body = self.post("/invoices/pdf", [
            ("action", "commit"), ("data", b64), ("filename", "x.pdf"), ("direction", "sales"),
            ("date", "2026-10-02"), ("partner", "株式会社テスト2"), ("description", "調査"), ("subtotal", "82354"),
            ("tax", "8235"), ("rate", "10"), ("withholding", "8408"), ("total", "82181"), ("account", "400")])
        self.assertIn("/entry/", url)
        self.assertIn("x.pdf", body)
        self.assertIn("x.pdf", self.get("/documents?partner=テスト2".replace("テスト2", urllib.parse.quote("テスト2"))))
        _, body = self.post("/invoices/pdf", [("action", "preview"), ("data", b64), ("filename", "x.pdf")])
        self.assertIn("保存済み", body)


if __name__ == "__main__":
    unittest.main()
