"""消費税の概算計算（税込経理・割戻し計算）.

申告書への転記用の目安です。返品・貸倒れ・中間納付・特定収入などは考慮していません。
"""

import datetime

from . import db

# 適格請求書がない仕入れの経過措置（控除できる割合）。
# 税制改正で変わることがあるため、申告前に国税庁の最新情報を確認してください。
TRANSITIONAL = [
    (datetime.date(2023, 10, 1), datetime.date(2026, 9, 30), 80),
    (datetime.date(2026, 10, 1), datetime.date(2029, 9, 30), 50),
]

# 簡易課税のみなし仕入率
SIMPLIFIED_RATES = {1: 90, 2: 80, 3: 70, 4: 60, 5: 50, 6: 40}
SIMPLIFIED_LABELS = {
    1: "第1種（卸売業）",
    2: "第2種（小売業など）",
    3: "第3種（製造業・建設業など）",
    4: "第4種（飲食店業など）",
    5: "第5種（サービス業・金融保険業など）",
    6: "第6種（不動産業）",
}
METHOD_LABELS = {
    "exempt": "免税事業者",
    "general": "一般課税",
    "simplified": "簡易課税",
    "niwari": "2割特例",
}


def invoice_ratio(invoice, date):
    """仕入税額控除できる割合(%)"""
    if invoice in ("Q", "S"):
        return 100
    if invoice == "N":
        d = datetime.date.fromisoformat(date)
        for start, end, pct in TRANSITIONAL:
            if start <= d <= end:
                return pct
    return 0


def _floor(value, unit):
    return value // unit * unit


def compute(conn, year, method=None, category=None):
    method = method or db.get_setting(conn, "tax_method")
    category = int(category or db.get_setting(conn, "simplified_category") or 5)
    rows = conn.execute(
        "SELECT e.date, l.side, l.amount, l.tax, l.invoice FROM lines l "
        "JOIN entries e ON e.id = l.entry_id WHERE e.deleted = 0 AND e.year = ? "
        "AND l.tax IN ('S10','S8','P10','P8')",
        (year,),
    ).fetchall()
    sales = {"S10": 0, "S8": 0}
    purchases = {}  # (税率, 控除割合) -> 税込金額
    for r in rows:
        if r["tax"] in sales:
            sales[r["tax"]] += r["amount"] if r["side"] == "C" else -r["amount"]
        else:
            key = (r["tax"], invoice_ratio(r["invoice"], r["date"]))
            purchases[key] = purchases.get(key, 0) + (r["amount"] if r["side"] == "D" else -r["amount"])

    base10 = _floor(max(sales["S10"], 0) * 100 // 110, 1000)
    base8 = _floor(max(sales["S8"], 0) * 100 // 108, 1000)
    tax10 = base10 * 78 // 1000
    tax8 = base8 * 624 // 10000
    sales_tax = tax10 + tax8

    purchase_rows = []
    general_deduction = 0
    for (tax, pct), amount in sorted(purchases.items()):
        if tax == "P10":
            t = amount * 78 * pct // 110000  # 税込 × 7.8/110 × 割合
        else:
            t = amount * 624 * pct // 1080000  # 税込 × 6.24/108 × 割合
        purchase_rows.append({"tax": tax, "ratio": pct, "amount": amount, "deduction": t})
        general_deduction += t

    if method == "general":
        deduction = general_deduction
    elif method == "simplified":
        deduction = sales_tax * SIMPLIFIED_RATES[category] // 100
    elif method == "niwari":
        deduction = sales_tax * 80 // 100
    else:
        deduction = 0

    if method == "exempt":
        national = local = 0
    else:
        diff = sales_tax - deduction
        national = _floor(diff, 100) if diff > 0 else diff
        local = _floor(national * 22 // 78, 100) if national > 0 else -((-national) * 22 // 78)

    return {
        "method": method,
        "category": category,
        "sales10": sales["S10"],
        "sales8": sales["S8"],
        "base10": base10,
        "base8": base8,
        "tax10": tax10,
        "tax8": tax8,
        "sales_tax": sales_tax,
        "purchases": purchase_rows,
        "general_deduction": general_deduction,
        "deduction": deduction,
        "national": national,
        "local": local,
        "total": national + local,
    }
