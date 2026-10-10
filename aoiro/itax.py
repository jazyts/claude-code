"""所得税・住民税・個人事業税の年税額予測（概算）.

帳簿の事業所得に、画面で入力した所得控除（社会保険料・配偶者・扶養など）を合わせて、
確定申告でいくら納める（戻る）かを見積もる。税率・控除額は税制改正で変わるため、
申告時は国税庁・自治体の最新情報を確認すること。
"""

import json

from . import ctax, db, invoices, reports

# 所得税の速算表（課税所得の上限, 税率%, 控除額）
BRACKETS = [
    (1_950_000, 5, 0),
    (3_300_000, 10, 97_500),
    (6_950_000, 20, 427_500),
    (9_000_000, 23, 636_000),
    (18_000_000, 33, 1_536_000),
    (40_000_000, 40, 2_796_000),
    (None, 45, 4_796_000),
]
RECONSTRUCTION_PCT = 2.1   # 復興特別所得税
RESIDENT_RATE = 10         # 住民税 所得割
RESIDENT_FLAT = 5_000      # 均等割（標準）
FOREST_TAX = 1_000         # 森林環境税（2024年度〜）
BIZ_TAX_DEDUCTION = 2_900_000  # 個人事業税の事業主控除
BIZ_TAX_RATES = {"5": "5%（多くの業種）", "4": "4%（畜産・水産・薪炭製造業）",
                 "3": "3%（あん摩・はり等、装蹄師業）", "0": "非課税（文筆・翻訳・画家・作曲等）"}
ADVANCE_THRESHOLD = 150_000  # 予定納税基準額がこれ以上なら予定納税が発生

# 入力項目（キー, ラベル, 既定値）
INPUT_FIELDS = [
    ("social_insurance", "社会保険料（国民年金・国民健康保険など、支払った額）", 0),
    ("mutual_aid", "小規模企業共済・iDeCo の掛金", 0),
    ("life_insurance", "生命保険料控除の額（所得税分・最大12万円）", 0),
    ("earthquake_insurance", "地震保険料（支払った額・最大5万円）", 0),
    ("medical", "医療費（支払った額）", 0),
    ("furusato", "ふるさと納税などの寄附金", 0),
    ("spouse", "配偶者控除（配偶者の所得が58万円以下なら 1）", 0),
    ("dependents", "一般の扶養親族の人数（16歳以上）", 0),
    ("dependents_specific", "特定扶養親族の人数（19〜22歳）", 0),
    ("other_income", "事業以外の所得（給与所得控除後など）", 0),
    ("prepaid", "予定納税した額（第1期＋第2期）", 0),
]
SETTING_KEY = "itax_inputs"


def _floor(value, unit):
    return value // unit * unit if value > 0 else 0


def basic_deduction(total_income, year):
    """所得税の基礎控除（2025年改正。2025・2026年分は所得に応じた上乗せあり）"""
    if total_income > 25_000_000:
        return 0
    if total_income > 24_500_000:
        return 160_000
    if total_income > 24_000_000:
        return 320_000
    if total_income > 23_500_000:
        return 480_000
    if total_income <= 1_320_000:
        return 950_000
    if year <= 2026:
        if total_income <= 3_360_000:
            return 880_000
        if total_income <= 4_890_000:
            return 680_000
        if total_income <= 6_550_000:
            return 630_000
    return 580_000


def resident_basic_deduction(total_income):
    if total_income > 25_000_000:
        return 0
    if total_income > 24_500_000:
        return 150_000
    if total_income > 24_000_000:
        return 290_000
    return 430_000


def spouse_deduction(total_income):
    """配偶者控除（所得税, 住民税）"""
    if total_income > 10_000_000:
        return 0, 0
    if total_income > 9_500_000:
        return 130_000, 110_000
    if total_income > 9_000_000:
        return 260_000, 220_000
    return 380_000, 330_000


def income_tax(taxable):
    """課税所得（千円未満切捨て後）に対する所得税額（復興特別所得税を含まない）"""
    taxable = _floor(taxable, 1000)
    for limit, pct, sub in BRACKETS:
        if limit is None or taxable <= limit:
            return taxable * pct // 100 - sub
    return 0


def marginal_rate(taxable):
    taxable = _floor(taxable, 1000)
    for limit, pct, _ in BRACKETS:
        if limit is None or taxable <= limit:
            return pct
    return 45


def compute(p):
    """税額を計算する。p は dict:
    year, pre_income（青色控除前の事業所得）, blue_limit（青色申告特別控除の上限）,
    withholding（源泉徴収税額）, ctax（消費税の納付見込み）, biz_tax_rate（"5"など）, 各 INPUT_FIELDS
    """
    year = p["year"]
    pre_income = p["pre_income"]
    blue = max(0, min(p.get("blue_limit", 0), pre_income))
    business_income = pre_income - blue
    total_income = business_income + p.get("other_income", 0)

    # ---- 所得控除（所得税, 住民税）
    medical = min(2_000_000, max(0, p.get("medical", 0) - min(100_000, total_income * 5 // 100)))
    life = min(120_000, p.get("life_insurance", 0))
    life_r = min(70_000, life * 7 // 10)
    quake = min(50_000, p.get("earthquake_insurance", 0))
    donation = max(0, min(p.get("furusato", 0), total_income * 40 // 100) - 2000)
    sp_i, sp_r = spouse_deduction(total_income) if p.get("spouse") else (0, 0)
    dep, dep_s = p.get("dependents", 0), p.get("dependents_specific", 0)
    deductions = [
        ("社会保険料控除", p.get("social_insurance", 0), p.get("social_insurance", 0)),
        ("小規模企業共済等掛金控除", p.get("mutual_aid", 0), p.get("mutual_aid", 0)),
        ("生命保険料控除", life, life_r),
        ("地震保険料控除", quake, quake // 2),
        ("医療費控除", medical, medical),
        ("寄附金控除（所得税のみ。住民税は税額控除）", donation, 0),
        ("配偶者控除", sp_i, sp_r),
        ("扶養控除", dep * 380_000 + dep_s * 630_000, dep * 330_000 + dep_s * 450_000),
        ("基礎控除", basic_deduction(total_income, year), resident_basic_deduction(total_income)),
    ]
    deductions = [d for d in deductions if d[1] or d[2]]
    ded_i = sum(d[1] for d in deductions)
    ded_r = sum(d[2] for d in deductions)

    # ---- 所得税
    taxable_i = _floor(max(0, total_income - ded_i), 1000)
    base_tax = income_tax(taxable_i)
    reconstruction = int(base_tax * RECONSTRUCTION_PCT / 100)
    tax_total = base_tax + reconstruction
    withholding = p.get("withholding", 0)
    prepaid = p.get("prepaid", 0)
    diff = tax_total - withholding - prepaid
    income_tax_due = _floor(diff, 100) if diff > 0 else diff  # マイナスは還付
    rate = marginal_rate(taxable_i)

    # 予定納税（翌年）: 予定納税基準額が15万円以上なら 1/3 ずつ 7月・11月
    advance_base = tax_total - withholding
    advance = _floor(advance_base // 3, 100) if advance_base >= ADVANCE_THRESHOLD else 0

    # ---- 住民税（翌年度に納付）
    if total_income <= 450_000:
        taxable_r = adjust = resident_income_part = furusato_credit = 0
        resident_total = 0
    else:
        taxable_r = _floor(max(0, total_income - ded_r), 1000)
        personal_gap = 50_000 + (50_000 if sp_i else 0) + dep * 50_000 + dep_s * 180_000
        if taxable_r <= 2_000_000:
            adjust = min(personal_gap, taxable_r) * 5 // 100
        else:
            adjust = max((personal_gap - (taxable_r - 2_000_000)) * 5 // 100, 2_500)
        if total_income > 25_000_000:
            adjust = 0
        resident_income_part = max(0, taxable_r * RESIDENT_RATE // 100 - adjust)
        # ふるさと納税: 基本控除 10% ＋ 特例控除（所得割の2割が上限）
        furusato_credit = 0
        if donation:
            basic = donation * 10 // 100
            special = donation * (90 - rate * 1.021) / 100
            special = min(int(special), resident_income_part * 20 // 100)
            furusato_credit = min(resident_income_part, basic + special)
        resident_total = _floor(resident_income_part - furusato_credit, 100) + RESIDENT_FLAT + FOREST_TAX

    # ---- 個人事業税（青色申告特別控除前の所得から事業主控除を引く）
    biz_rate = int(p.get("biz_tax_rate", "5") or 0)
    biz_base = _floor(max(0, pre_income - BIZ_TAX_DEDUCTION), 1000)
    biz_tax = _floor(biz_base * biz_rate // 100, 100)

    ctax_total = max(0, p.get("ctax", 0))
    taxes = tax_total + resident_total + biz_tax + ctax_total
    net = (pre_income - p.get("social_insurance", 0) - p.get("mutual_aid", 0) - taxes)
    return {
        "pre_income": pre_income, "blue": blue, "business_income": business_income,
        "total_income": total_income, "deductions": deductions,
        "deduction_total": (ded_i, ded_r),
        "taxable": taxable_i, "base_tax": base_tax, "reconstruction": reconstruction,
        "income_tax": tax_total, "withholding": withholding, "prepaid": prepaid,
        "income_tax_due": income_tax_due, "marginal_rate": rate, "advance": advance,
        "resident_taxable": taxable_r, "resident_adjust": adjust,
        "resident_income_part": resident_income_part, "furusato_credit": furusato_credit,
        "resident_flat": RESIDENT_FLAT + FOREST_TAX, "resident_tax": resident_total,
        "biz_base": biz_base, "biz_rate": biz_rate, "biz_tax": biz_tax,
        "ctax": ctax_total, "taxes": taxes, "net": net,
        "effective_rate": round(taxes * 100 / pre_income, 1) if pre_income > 0 else 0,
    }


def load_inputs(conn):
    raw = db.get_setting(conn, SETTING_KEY)
    data = json.loads(raw) if raw else {}
    out = {k: int(data.get(k, default) or 0) for k, _, default in INPUT_FIELDS}
    out["biz_tax_rate"] = str(data.get("biz_tax_rate", "5"))
    if out["biz_tax_rate"] not in BIZ_TAX_RATES:
        out["biz_tax_rate"] = "5"
    return out


def save_inputs(conn, form):
    data = {}
    for k, _, _ in INPUT_FIELDS:
        v = str(form.get(k, "")).replace(",", "").strip()
        data[k] = max(0, int(v)) if v else 0
    data["biz_tax_rate"] = form.get("biz_tax_rate", "5")
    db.set_setting(conn, SETTING_KEY, json.dumps(data, ensure_ascii=False))
    return data


def params(conn, year, inputs=None):
    """帳簿から計算に必要な値を集める"""
    inputs = inputs or load_inputs(conn)
    p = dict(inputs)
    p["year"] = year
    p["pre_income"] = reports.income(conn, year)
    p["blue_limit"] = int(db.get_setting(conn, "blue_deduction") or 0)
    p["withholding"] = sum(r["withholding"] for r in invoices.withholding_summary(conn, year))
    p["ctax"] = ctax.compute(conn, year)["total"]
    return p


def forecast(conn, year, inputs=None):
    """税額予測と、「あと10万円」の比較（経費・小規模企業共済・ふるさと納税）"""
    p = params(conn, year, inputs)
    base = compute(p)
    step = 100_000
    whatif = []
    for label, key, delta, note in [
        ("経費をあと10万円使う", "pre_income", -step, "手元のお金は10万円減る"),
        ("小規模企業共済・iDeCoにあと10万円", "mutual_aid", step, "掛金は将来受け取れる"),
        ("ふるさと納税をあと10万円", "furusato", step, "返礼品がもらえる（自己負担が2,000円なら全額控除の範囲内）"),
    ]:
        q = dict(p)
        q[key] = q[key] + delta
        r = compute(q)
        whatif.append({"label": label, "saving": base["taxes"] - r["taxes"], "note": note,
                       "taxes": r["taxes"]})
    blue_options = []
    for limit, label in [(650_000, "65万円（e-Tax または電子帳簿保存）"), (550_000, "55万円（紙で提出）"),
                         (100_000, "10万円（簡易な簿記）")]:
        q = dict(p)
        q["blue_limit"] = limit
        blue_options.append({"label": label, "taxes": compute(q)["taxes"], "limit": limit})
    return {"params": p, "result": base, "whatif": whatif, "blue_options": blue_options,
            "schedule": schedule(year, base)}


def schedule(year, r):
    """翌年の納税スケジュール（納期限の目安）。(日付, 内容, 金額) を日付順に返す"""
    y = year + 1
    rows = []
    if r["income_tax_due"] > 0:
        rows.append(((y, 3, 16), f"{y}年3月16日", "所得税・復興特別所得税（確定申告）", r["income_tax_due"]))
    elif r["income_tax_due"] < 0:
        rows.append(((y, 4, 1), f"{y}年3〜4月ごろ", "所得税の還付（申告後1か月〜1か月半で入金）", r["income_tax_due"]))
    if r["ctax"]:
        rows.append(((y, 3, 31), f"{y}年3月31日", "消費税・地方消費税", r["ctax"]))
    if r["resident_tax"]:
        q = _floor(r["resident_tax"] // 4, 100)
        rows.append(((y, 6, 30), f"{y}年6月30日", "住民税 第1期（普通徴収・年4回）", r["resident_tax"] - q * 3))
        rows.append(((y, 8, 31), f"{y}年8月31日", "住民税 第2期", q))
        rows.append(((y, 10, 31), f"{y}年10月31日", "住民税 第3期", q))
        rows.append(((y + 1, 1, 31), f"{y + 1}年1月31日", "住民税 第4期", q))
    if r["advance"]:
        rows.append(((y, 7, 31), f"{y}年7月31日", "所得税 予定納税 第1期（翌年分の前払い）", r["advance"]))
        rows.append(((y, 12, 1), f"{y}年12月1日", "所得税 予定納税 第2期", r["advance"]))
    if r["biz_tax"]:
        half = _floor(r["biz_tax"] // 2, 100)
        rows.append(((y, 8, 31), f"{y}年8月31日", "個人事業税 第1期", r["biz_tax"] - half))
        rows.append(((y, 12, 1), f"{y}年12月1日", "個人事業税 第2期", half))
    rows.sort(key=lambda x: x[0])
    return [row[1:] for row in rows]
