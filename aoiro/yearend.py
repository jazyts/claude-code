"""決算整理（減価償却・家事按分・未払消費税）と年次繰越."""

import datetime

from . import db, ledger, reports


def code_of(conn, name):
    row = conn.execute("SELECT code FROM accounts WHERE name = ?", (name,)).fetchone()
    if not row:
        raise ledger.LedgerError(f"勘定科目「{name}」がありません")
    return row["code"]


def _existing(conn, year, source, account=None):
    return [x for x in ledger.search(conn, year=year, source=source, account=account)]


# ---------------------------------------------------------------- 減価償却

def straight_line_rate(life):
    """定額法の償却率（千分率）。例: 4年→250, 6年→167"""
    return -(-1000 // int(life))


def depreciation(asset, year):
    """指定年の減価償却（定額法）。None ならその年は対象外。"""
    acquired = datetime.date.fromisoformat(asset["acquired"])
    disposed = datetime.date.fromisoformat(asset["disposed"]) if asset["disposed"] else None
    if year < acquired.year or (disposed and year > disposed.year):
        return None
    rate = straight_line_rate(asset["life"])
    if asset["base_year"] and asset["base_book"] is not None and year >= asset["base_year"]:
        start, book = asset["base_year"], asset["base_book"]
    else:
        start, book = acquired.year, asset["cost"]
    for y in range(start, year + 1):
        first = 1 if y != acquired.year else acquired.month
        last = 12 if not (disposed and y == disposed.year) else disposed.month
        months = max(0, last - first + 1)
        dep = asset["cost"] * rate * months // 12000
        dep = max(0, min(dep, book - 1))
        if y == year:
            business = dep * asset["business_ratio"] // 100
            return {
                "opening_book": book,
                "rate": rate,
                "months": months,
                "depreciation": dep,
                "business": business,
                "private": dep - business,
                "closing_book": book - dep,
            }
        book -= dep
    return None


def fixed_assets(conn):
    return conn.execute(
        "SELECT f.*, a.name AS account_name FROM fixed_assets f "
        "JOIN accounts a ON a.code = f.account_code ORDER BY acquired, id"
    ).fetchall()


def save_asset(conn, data, asset_id=None):
    name = data.get("name", "").strip()
    if not name:
        raise ledger.LedgerError("資産名を入力してください")
    acquired = ledger.parse_date(data.get("acquired")).isoformat()
    disposed = data.get("disposed") or None
    if disposed:
        disposed = ledger.parse_date(disposed).isoformat()
    try:
        cost = int(str(data.get("cost")).replace(",", ""))
        life = int(data.get("life"))
        ratio = int(data.get("business_ratio") or 100)
        base_year = int(data["base_year"]) if data.get("base_year") else None
        base_book = int(str(data["base_book"]).replace(",", "")) if data.get("base_book") else None
    except (TypeError, ValueError):
        raise ledger.LedgerError("数値の入力が不正です") from None
    if cost <= 0 or life <= 0 or not 0 <= ratio <= 100:
        raise ledger.LedgerError("取得価額・耐用年数・事業割合を確認してください")
    if (base_year is None) != (base_book is None):
        raise ledger.LedgerError("導入時の未償却残高は「基準年」と「金額」を両方入力してください")
    values = (name, data.get("account_code"), acquired, cost, life, ratio, base_year, base_book, disposed)
    with conn:
        if asset_id:
            conn.execute(
                "UPDATE fixed_assets SET name=?, account_code=?, acquired=?, cost=?, life=?, "
                "business_ratio=?, base_year=?, base_book=?, disposed=? WHERE id=?",
                values + (asset_id,),
            )
        else:
            asset_id = conn.execute(
                "INSERT INTO fixed_assets(name, account_code, acquired, cost, life, business_ratio, "
                "base_year, base_book, disposed) VALUES (?,?,?,?,?,?,?,?,?)",
                values,
            ).lastrowid
        db.audit(conn, "fixed_asset", {"id": asset_id, "values": values})
    return asset_id


def depreciation_schedule(conn, year):
    rows = []
    for a in fixed_assets(conn):
        dep = depreciation(a, year)
        if dep:
            rows.append({"asset": a, **dep})
    return rows


def post_depreciation(conn, year):
    if _existing(conn, year, "depr"):
        raise ledger.LedgerError("この年の減価償却仕訳は登録済みです（削除してから再作成してください）")
    lines = []
    dep_code, private_code = code_of(conn, "減価償却費"), code_of(conn, "事業主貸")
    for r in depreciation_schedule(conn, year):
        if not r["depreciation"]:
            continue
        name = r["asset"]["name"]
        if r["business"]:
            lines.append({"side": "D", "account": dep_code, "amount": r["business"], "tax": "NA", "memo": name})
        if r["private"]:
            lines.append({"side": "D", "account": private_code, "amount": r["private"], "tax": "NA",
                          "memo": f"{name}（家事分）"})
        lines.append({"side": "C", "account": r["asset"]["account_code"], "amount": r["depreciation"],
                      "tax": "NA", "memo": name})
    if not lines:
        raise ledger.LedgerError("この年に償却する資産がありません")
    return ledger.post_entry(conn, f"{year}-12-31", lines, description="減価償却費の計上",
                             source="depr")


# ---------------------------------------------------------------- 家事按分

def kaji_preview(conn, year, account, business_pct):
    business_pct = int(business_pct)
    if not 0 <= business_pct < 100:
        raise ledger.LedgerError("事業割合は0〜99%で入力してください")
    rows = conn.execute(
        "SELECT l.side, l.amount, l.tax, l.invoice FROM lines l JOIN entries e ON e.id = l.entry_id "
        "WHERE e.deleted = 0 AND e.year = ? AND e.source != 'kaji' AND l.account_code = ?",
        (year, account),
    ).fetchall()
    groups = {}
    for r in rows:
        key = (r["tax"], r["invoice"])
        groups[key] = groups.get(key, 0) + (r["amount"] if r["side"] == "D" else -r["amount"])
    result = []
    for (tax, invoice), total in groups.items():
        private = total * (100 - business_pct) // 100
        if private > 0:
            result.append({"tax": tax, "invoice": invoice, "total": total, "private": private})
    return result


def post_kaji(conn, year, account, business_pct):
    if _existing(conn, year, "kaji", account):
        raise ledger.LedgerError("この科目の家事按分仕訳は登録済みです（削除してから再作成してください）")
    groups = kaji_preview(conn, year, account, business_pct)
    if not groups:
        raise ledger.LedgerError("按分する金額がありません")
    total = sum(g["private"] for g in groups)
    lines = [{"side": "D", "account": code_of(conn, "事業主貸"), "amount": total, "tax": "NA"}]
    for g in groups:
        lines.append({"side": "C", "account": account, "amount": g["private"], "tax": g["tax"],
                      "invoice": g["invoice"], "memo": f"家事分 {100 - int(business_pct)}%"})
    name = db.account_map(conn)[account]["name"]
    return ledger.post_entry(conn, f"{year}-12-31", lines,
                             description=f"{name}の家事按分（事業割合 {business_pct}%）", source="kaji")


# ---------------------------------------------------------------- 未払消費税

def post_ctax_accrual(conn, year, amount):
    if _existing(conn, year, "ctax"):
        raise ledger.LedgerError("この年の未払消費税仕訳は登録済みです")
    amount = int(amount)
    if amount <= 0:
        raise ledger.LedgerError("納付税額がありません")
    lines = [
        {"side": "D", "account": code_of(conn, "租税公課"), "amount": amount, "tax": "NA"},
        {"side": "C", "account": code_of(conn, "未払金"), "amount": amount, "tax": "NA"},
    ]
    return ledger.post_entry(conn, f"{year}-12-31", lines, partner="税務署",
                             description="消費税及び地方消費税の未払計上（税込経理）", source="ctax")


# ---------------------------------------------------------------- 年次繰越

def next_opening(conn, year):
    """翌年の期首残高: 元入金 = 元入金 + 所得 + 事業主借 − 事業主貸"""
    b = reports.balances(conn, year)
    capital, draw, contrib = code_of(conn, "元入金"), code_of(conn, "事業主貸"), code_of(conn, "事業主借")
    result = {}
    for code, x in b.items():
        if x["account"]["category"] in ("revenue", "expense") or code in (capital, draw, contrib):
            continue
        if x["closing"]:
            result[code] = x["closing"]
    result[capital] = (b[capital]["closing"] + reports.income(conn, year)
                       + b[contrib]["closing"] - b[draw]["closing"])
    return result


def close_year(conn, year):
    year = int(year)
    if year in db.closed_years(conn):
        raise ledger.LedgerError(f"{year}年は締め済みです")
    if (year + 1) in db.closed_years(conn):
        raise ledger.LedgerError(f"{year + 1}年が締め済みのため繰越できません")
    opening = next_opening(conn, year)
    ledger.set_opening(conn, year + 1, opening)
    years = sorted(db.closed_years(conn) | {year})
    db.set_setting(conn, "closed_years", ",".join(map(str, years)))
    return opening


def reopen_year(conn, year):
    year = int(year)
    if (year + 1) in db.closed_years(conn):
        raise ledger.LedgerError(f"先に{year + 1}年の締めを解除してください")
    years = sorted(db.closed_years(conn) - {year})
    db.set_setting(conn, "closed_years", ",".join(map(str, years)))
