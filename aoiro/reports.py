"""帳簿（仕訳帳・総勘定元帳）と決算書類（試算表・損益計算書・貸借対照表）."""

from collections import defaultdict

from . import db, ledger

# 青色申告決算書（一般用）損益計算書の経費科目の並び
KESSAN_EXPENSES = [
    ("⑧", "租税公課"), ("⑨", "荷造運賃"), ("⑩", "水道光熱費"), ("⑪", "旅費交通費"),
    ("⑫", "通信費"), ("⑬", "広告宣伝費"), ("⑭", "接待交際費"), ("⑮", "損害保険料"),
    ("⑯", "修繕費"), ("⑰", "消耗品費"), ("⑱", "減価償却費"), ("⑲", "福利厚生費"),
    ("⑳", "給料賃金"), ("㉑", "外注工賃"), ("㉒", "利子割引料"), ("㉓", "地代家賃"),
    ("㉔", "貸倒金"),
]
KESSAN_FREE_SLOTS = ["㉕", "㉖", "㉗", "㉘", "㉙", "㉚"]
KESSAN_COST = ("期首商品棚卸高", "仕入金額", "期末商品棚卸高")


def _signed(category, debit, credit):
    return debit - credit if category in db.DEBIT_NORMAL else credit - debit


def balances(conn, year, date_to=None):
    """科目ごとの {opening, debit, credit, closing}（正常残高側がプラス）"""
    accounts = db.accounts(conn)
    opening = ledger.get_opening(conn, year)
    sql = (
        "SELECT l.account_code, l.side, SUM(l.amount) AS total FROM lines l "
        "JOIN entries e ON e.id = l.entry_id WHERE e.deleted = 0 AND e.year = ?"
    )
    params = [year]
    if date_to:
        sql += " AND e.date <= ?"
        params.append(date_to)
    sql += " GROUP BY l.account_code, l.side"
    sums = defaultdict(lambda: {"D": 0, "C": 0})
    for r in conn.execute(sql, params):
        sums[r["account_code"]][r["side"]] = r["total"]
    result = {}
    for a in accounts:
        o = opening.get(a["code"], 0)
        d, c = sums[a["code"]]["D"], sums[a["code"]]["C"]
        result[a["code"]] = {
            "account": a,
            "opening": o,
            "debit": d,
            "credit": c,
            "closing": o + _signed(a["category"], d, c),
        }
    return result


def journal(conn, year):
    return ledger.search(conn, year=year)


def general_ledger(conn, year, code):
    """総勘定元帳: 日付・相手科目・摘要・借方・貸方・残高"""
    accounts = db.account_map(conn)
    acct = accounts[code]
    balance = ledger.get_opening(conn, year).get(code, 0)
    rows = [{"date": f"{year}-01-01", "entry_no": "", "counter": "", "description": "前期繰越",
             "debit": 0, "credit": 0, "balance": balance, "entry_id": None}]
    for item in ledger.search(conn, year=year, account=code):
        e, lines = item["entry"], item["lines"]
        for l in lines:
            if l["account_code"] != code:
                continue
            others = {x["account_name"] for x in lines if x["side"] != l["side"]}
            counter = others.pop() if len(others) == 1 else "諸口"
            d = l["amount"] if l["side"] == "D" else 0
            c = l["amount"] if l["side"] == "C" else 0
            balance += _signed(acct["category"], d, c)
            desc = " ".join(x for x in (e["partner"], e["description"], l["memo"]) if x)
            rows.append({"date": e["date"], "entry_no": e["entry_no"], "counter": counter,
                         "description": desc, "debit": d, "credit": c, "balance": balance,
                         "entry_id": e["id"]})
    return acct, rows


def trial_balance(conn, year, date_to=None):
    rows = [b for b in balances(conn, year, date_to).values()
            if b["opening"] or b["debit"] or b["credit"]]
    total_debit = sum(b["debit"] for b in rows)
    total_credit = sum(b["credit"] for b in rows)
    return rows, total_debit, total_credit


def income(conn, year):
    """青色申告特別控除前の所得金額（収益 − 費用）"""
    b = balances(conn, year)
    rev = sum(x["closing"] for x in b.values() if x["account"]["category"] == "revenue")
    exp = sum(x["closing"] for x in b.values() if x["account"]["category"] == "expense")
    return rev - exp


def blue_deduction(conn, year):
    pre = income(conn, year)
    limit = int(db.get_setting(conn, "blue_deduction") or 0)
    return max(0, min(limit, pre))


def profit_loss(conn, year):
    """青色申告決算書（一般用）1ページ目の損益計算書の形に集計する。"""
    b = balances(conn, year)
    by_kessan = defaultdict(int)
    breakdown = defaultdict(list)
    for x in b.values():
        a = x["account"]
        if a["category"] not in ("revenue", "expense") or not x["closing"]:
            continue
        by_kessan[a["kessan"]] += x["closing"]
        breakdown[a["kessan"]].append((a["name"], x["closing"]))

    sales = sum(x["closing"] for x in b.values() if x["account"]["category"] == "revenue")
    begin_inv = by_kessan.pop("期首商品棚卸高", 0)
    purchases = by_kessan.pop("仕入金額", 0)
    # 期末商品棚卸高は貸方に計上されるため費用科目としてはマイナス残高になる
    end_inv = -by_kessan.pop("期末商品棚卸高", 0)
    subtotal = begin_inv + purchases
    cost = subtotal - end_inv
    gross = sales - cost

    expenses = []
    for no, name in KESSAN_EXPENSES:
        expenses.append({"no": no, "name": name, "amount": by_kessan.pop(name, 0)})
    misc = by_kessan.pop("雑費", 0)
    by_kessan.pop("売上", None)
    custom = sorted(by_kessan.items(), key=lambda kv: -kv[1])
    warnings = []
    slots = list(KESSAN_FREE_SLOTS)
    for name, amount in custom:
        if slots:
            expenses.append({"no": slots.pop(0), "name": name, "amount": amount})
        else:
            misc += amount
            warnings.append(f"空欄科目が足りないため「{name}」を雑費に含めました")
    for no in slots:
        expenses.append({"no": no, "name": "", "amount": 0})
    expenses.append({"no": "㉛", "name": "雑費", "amount": misc})
    expense_total = sum(e["amount"] for e in expenses)
    pre_income = gross - expense_total
    deduction = max(0, min(int(db.get_setting(conn, "blue_deduction") or 0), pre_income))
    return {
        "sales": sales,
        "sales_breakdown": breakdown.get("売上", []),
        "begin_inventory": begin_inv,
        "purchases": purchases,
        "subtotal": subtotal,
        "end_inventory": end_inv,
        "cost": cost,
        "gross": gross,
        "expenses": expenses,
        "expense_total": expense_total,
        "pre_income": pre_income,
        "blue_deduction": deduction,
        "income": pre_income - deduction,
        "warnings": warnings,
    }


def balance_sheet(conn, year):
    """貸借対照表（資産負債調）: 期首・期末"""
    b = balances(conn, year)
    assets, liabilities, equity = [], [], []
    owner_draw = owner_contrib = capital = None
    for x in b.values():
        a = x["account"]
        if a["category"] in ("revenue", "expense"):
            continue
        if not (x["opening"] or x["closing"] or a["name"] in ("事業主貸", "事業主借", "元入金")):
            continue
        row = {"name": a["name"], "code": a["code"], "opening": x["opening"], "closing": x["closing"]}
        if a["name"] == "事業主貸":
            owner_draw = row
        elif a["name"] == "事業主借":
            owner_contrib = row
        elif a["name"] == "元入金":
            capital = row
        elif a["category"] == "asset":
            assets.append(row)
        elif a["category"] == "liability":
            liabilities.append(row)
        else:
            equity.append(row)
    pre_income = income(conn, year)
    # 事業主貸・事業主借・所得は期末欄のみに記載する
    if owner_draw:
        owner_draw["opening"] = 0
        assets.append(owner_draw)
    tail = []
    if owner_contrib:
        owner_contrib["opening"] = 0
        tail.append(owner_contrib)
    if capital:
        tail.append(capital)
    tail.append({"name": "青色申告特別控除前の所得金額", "code": "", "opening": 0,
                 "closing": pre_income})
    right = liabilities + equity + tail
    total_assets_open = sum(r["opening"] for r in assets)
    total_assets_close = sum(r["closing"] for r in assets)
    total_right_open = sum(r["opening"] for r in right)
    total_right_close = sum(r["closing"] for r in right)
    return {
        "assets": assets,
        "liabilities": right,
        "total_assets": (total_assets_open, total_assets_close),
        "total_liabilities": (total_right_open, total_right_close),
        "balanced": total_assets_open == total_right_open and total_assets_close == total_right_close,
    }


def monthly(conn, year):
    """月別売上（収入）金額及び仕入金額（決算書2ページ目）"""
    accounts = db.account_map(conn)
    sales = [0] * 12
    misc_income = 0
    purchases = [0] * 12
    rows = conn.execute(
        "SELECT e.date, l.side, l.amount, l.account_code FROM lines l JOIN entries e "
        "ON e.id = l.entry_id WHERE e.deleted = 0 AND e.year = ?",
        (year,),
    )
    for r in rows:
        a = accounts[r["account_code"]]
        m = int(r["date"][5:7]) - 1
        if a["category"] == "revenue":
            v = _signed("revenue", r["amount"] if r["side"] == "D" else 0,
                        r["amount"] if r["side"] == "C" else 0)
            if a["name"] == "雑収入":
                misc_income += v
            else:
                sales[m] += v
        elif a["kessan"] == "仕入金額":
            purchases[m] += _signed("expense", r["amount"] if r["side"] == "D" else 0,
                                    r["amount"] if r["side"] == "C" else 0)
    return {"sales": sales, "misc_income": misc_income, "purchases": purchases,
            "total_sales": sum(sales) + misc_income, "total_purchases": sum(purchases)}
