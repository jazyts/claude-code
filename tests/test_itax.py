import unittest

from aoiro import db, itax, ledger


def _params(**over):
    p = {k: d for k, _, d in itax.INPUT_FIELDS}
    p.update(year=2026, pre_income=0, blue_limit=650_000, withholding=0, ctax=0, biz_tax_rate="5")
    p.update(over)
    return p


class IncomeTaxTest(unittest.TestCase):
    def test_brackets_match_official_table(self):
        self.assertEqual(itax.income_tax(1_950_000), 97_500)
        self.assertEqual(itax.income_tax(3_300_000), 232_500)
        self.assertEqual(itax.income_tax(6_950_000), 962_500)
        self.assertEqual(itax.income_tax(9_000_000), 1_434_000)
        self.assertEqual(itax.income_tax(18_000_000), 4_404_000)
        self.assertEqual(itax.income_tax(40_000_000), 13_204_000)
        self.assertEqual(itax.income_tax(50_000_000), 17_704_000)
        self.assertEqual(itax.income_tax(1_234), 50)  # 千円未満切捨て

    def test_basic_deduction_2025_reform(self):
        self.assertEqual(itax.basic_deduction(1_000_000, 2026), 950_000)
        self.assertEqual(itax.basic_deduction(3_000_000, 2026), 880_000)
        self.assertEqual(itax.basic_deduction(5_000_000, 2026), 630_000)
        self.assertEqual(itax.basic_deduction(5_000_000, 2027), 580_000)
        self.assertEqual(itax.basic_deduction(30_000_000, 2026), 0)

    def test_full_computation(self):
        r = itax.compute(_params(pre_income=8_000_000, withholding=1_000_000, ctax=500_000,
                                 social_insurance=900_000, furusato=50_000))
        self.assertEqual(r["business_income"], 7_350_000)
        self.assertEqual(r["taxable"], 5_822_000)           # 7,350,000 − (900,000 + 48,000 + 580,000)
        self.assertEqual(r["base_tax"], 736_900)            # × 20% − 427,500
        self.assertEqual(r["reconstruction"], 15_474)
        self.assertEqual(r["income_tax_due"], -247_626)     # 源泉が多いので還付
        self.assertEqual(r["biz_tax"], 255_000)             # (8,000,000 − 2,900,000) × 5%
        self.assertEqual(r["resident_flat"], 6_000)
        self.assertEqual(r["furusato_credit"], 38_198)
        self.assertEqual(r["taxes"], r["income_tax"] + r["resident_tax"] + r["biz_tax"] + r["ctax"])
        self.assertEqual(r["marginal_rate"], 20)

    def test_payment_and_advance_tax(self):
        r = itax.compute(_params(pre_income=12_000_000, withholding=100_000))
        self.assertGreater(r["income_tax_due"], 0)
        self.assertEqual(r["income_tax_due"] % 100, 0)
        self.assertGreater(r["advance"], 0)
        dates = [d for d, _, _ in itax.schedule(2026, r)]
        self.assertEqual(dates[0], "2027年3月16日")
        self.assertIn("2027年7月31日", dates)

    def test_low_income_has_no_tax(self):
        r = itax.compute(_params(pre_income=400_000))
        self.assertEqual((r["income_tax"], r["resident_tax"], r["biz_tax"]), (0, 0, 0))
        self.assertEqual(itax.schedule(2026, r), [])

    def test_family_deductions(self):
        base = itax.compute(_params(pre_income=6_000_000))
        fam = itax.compute(_params(pre_income=6_000_000, spouse=1, dependents=1, dependents_specific=1))
        self.assertEqual(base["taxable"] - fam["taxable"], 380_000 + 380_000 + 630_000)
        self.assertEqual(base["resident_taxable"] - fam["resident_taxable"], 330_000 + 330_000 + 450_000)
        self.assertLess(fam["taxes"], base["taxes"])


class ForecastTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        ledger.post_entry(self.c, "2026-03-25", [
            {"side": "D", "account": "120", "amount": 4_000_000, "tax": None, "invoice": "Q", "memo": ""},
            {"side": "D", "account": "190", "amount": 408_400, "tax": None, "invoice": "Q", "memo": "源泉所得税"},
            {"side": "C", "account": "400", "amount": 4_408_400, "tax": "S10", "invoice": "Q", "memo": ""},
        ], "株式会社A", "業務委託料")
        ledger.post_entry(self.c, "2026-04-01", [
            {"side": "D", "account": "615", "amount": 600_000, "tax": "P10", "invoice": "Q", "memo": ""},
            {"side": "C", "account": "110", "amount": 600_000, "tax": None, "invoice": "Q", "memo": ""},
        ], "大家", "家賃")

    def tearDown(self):
        self.c.close()

    def test_forecast_uses_books_and_saved_inputs(self):
        saved = itax.save_inputs(self.c, {"social_insurance": "500,000", "mutual_aid": "", "biz_tax_rate": "0"})
        self.assertEqual(saved["social_insurance"], 500_000)
        self.assertEqual(itax.load_inputs(self.c)["biz_tax_rate"], "0")
        f = itax.forecast(self.c, 2026)
        r = f["result"]
        self.assertEqual(r["pre_income"], 4_408_400 - 600_000)
        self.assertEqual(r["withholding"], 408_400)
        self.assertEqual(r["biz_tax"], 0)  # 非課税業種
        self.assertGreater(r["ctax"], 0)
        self.assertEqual(len(f["whatif"]), 3)
        self.assertTrue(all(w["saving"] > 0 for w in f["whatif"]))
        self.assertEqual([b["limit"] for b in f["blue_options"]], [650_000, 550_000, 100_000])
        self.assertLess(f["blue_options"][0]["taxes"], f["blue_options"][2]["taxes"])

    def test_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            itax.save_inputs(self.c, {"social_insurance": "abc"})


if __name__ == "__main__":
    unittest.main()
