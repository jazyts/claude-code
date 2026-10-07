"""請求書の発行・売上仕訳・入金・請求書PDFの読取り・証憑の保存."""

import datetime
import hashlib
import json
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

from . import db, ledger, pdftext

RATES = {"10": 10, "8": 8, "0": 0}  # 0 = 対象外
TAX_FOR_RATE = {10: "S10", 8: "S8", 0: "NA"}
PURCHASE_TAX_FOR_RATE = {10: "P10", 8: "P8", 0: "NA"}
ISSUER_KEYS = ("issuer_title", "owner_name", "issuer_zip", "issuer_address", "issuer_tel", "issuer_regno",
               "bank_name", "bank_branch", "bank_account_type", "bank_account_number",
               "bank_account_holder", "invoice_note")
WITHHOLDING_MEMO = "源泉所得税"


class InvoiceError(ValueError):
    pass


def _int(value, default=0):
    s = re.sub(r"[,¥￥円\s]", "", unicodedata.normalize("NFKC", str(value or "")))
    if not s:
        return default
    try:
        return int(Decimal(s).quantize(Decimal(1), ROUND_HALF_UP))
    except Exception:  # noqa: BLE001
        raise InvoiceError(f"数値が不正です: {value}") from None


def _dec(value):
    s = re.sub(r"[,¥￥円\s]", "", unicodedata.normalize("NFKC", str(value or "")))
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        raise InvoiceError(f"数値が不正です: {value}") from None


def withholding_tax(base):
    """報酬・料金の源泉徴収税額（100万円以下 10.21%、超える部分 20.42%）"""
    if base <= 0:
        return 0
    if base <= 1_000_000:
        return int(Decimal(base) * Decimal("0.1021"))
    return 102_100 + int((Decimal(base) - 1_000_000) * Decimal("0.2042"))


def calculate(data):
    """明細から金額を計算する。明細の金額 = 数量 × 単価（四捨五入）、消費税は税率ごとに切捨て。"""
    items = []
    by_rate = {10: 0, 8: 0, 0: 0}
    for raw in data.get("items", []):
        desc = (raw.get("description") or "").strip()
        qty, price = _dec(raw.get("quantity")), _dec(raw.get("unit_price"))
        amount = raw.get("amount")
        if not desc and qty is None and price is None and not amount:
            continue
        if qty is not None and price is not None:
            amount = int((qty * price).quantize(Decimal(1), ROUND_HALF_UP))
        else:
            amount = _int(amount)
        rate = RATES.get(str(raw.get("rate", "10")), 10)
        by_rate[rate] += amount
        items.append({"date": (raw.get("date") or "").strip(), "description": desc,
                      "quantity": "" if qty is None else format(qty.normalize(), "f"),
                      "unit": (raw.get("unit") or "").strip(),
                      "unit_price": "" if price is None else int(price), "rate": rate, "amount": amount})
    tax_by_rate = {10: by_rate[10] * 10 // 100, 8: by_rate[8] * 8 // 100, 0: 0}
    subtotal = sum(by_rate.values())
    tax = sum(tax_by_rate.values())
    withholding = withholding_tax(subtotal) if data.get("withholding") else 0
    return {"items": items, "by_rate": by_rate, "tax_by_rate": tax_by_rate, "subtotal": subtotal,
            "tax": tax, "withholding": withholding, "total": subtotal + tax - withholding}


def issuer_defaults(conn):
    return {k: db.get_setting(conn, k) for k in ISSUER_KEYS}


def next_number(conn, issue_date):
    year = issue_date[:4]
    rows = conn.execute("SELECT number FROM invoices WHERE number LIKE ?", (f"{year}-%",)).fetchall()
    n = max([int(m.group(1)) for r in rows if (m := re.fullmatch(rf"{year}-(\d+)", r["number"]))] or [0])
    return f"{year}-{n + 1:03d}"


def default_due(issue_date):
    d = datetime.date.fromisoformat(issue_date)
    return f"{d.year}年{d.month}月末"


def get(conn, invoice_id):
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        return None
    return {**dict(row), "data": json.loads(row["data"])}


def listing(conn, year=None):
    sql = "SELECT * FROM invoices"
    params = []
    if year:
        sql += " WHERE issue_date LIKE ?"
        params.append(f"{year}-%")
    return conn.execute(sql + " ORDER BY issue_date DESC, id DESC", params).fetchall()


def sales_lines(conn, calc, revenue_account, receivable_account=None, memo=""):
    """売上の仕訳行（税込経理）: 売掛金 / 事業主貸（源泉所得税） / 売上高（税率ごと）"""
    receivable = receivable_account or _code(conn, "売掛金")
    lines = [{"side": "D", "account": receivable, "amount": calc["total"], "tax": "NA"}]
    if calc["withholding"]:
        lines.append({"side": "D", "account": _code(conn, "事業主貸"), "amount": calc["withholding"],
                      "tax": "NA", "memo": WITHHOLDING_MEMO})
    for rate in (10, 8, 0):
        gross = calc["by_rate"][rate] + calc["tax_by_rate"][rate]
        if gross:
            lines.append({"side": "C", "account": revenue_account, "amount": gross,
                          "tax": TAX_FOR_RATE[rate], "memo": memo})
    return lines


def _code(conn, name):
    row = conn.execute("SELECT code FROM accounts WHERE name = ?", (name,)).fetchone()
    if not row:
        raise InvoiceError(f"勘定科目「{name}」がありません")
    return row["code"]


def save(conn, form, invoice_id=None, post=True, reason=""):
    """請求書を保存し、必要なら売上の仕訳を作成・訂正する。"""
    issue_date = ledger.parse_date(form.get("issue_date")).isoformat()
    partner = (form.get("partner") or "").strip()
    if not partner:
        raise InvoiceError("宛先（取引先）を入力してください")
    calc = calculate(form)
    if not calc["items"]:
        raise InvoiceError("明細を1行以上入力してください")
    if calc["total"] <= 0:
        raise InvoiceError("請求金額が0円以下です")
    data = {
        "honorific": form.get("honorific") or "御中",
        "due": (form.get("due") or "").strip() or default_due(issue_date),
        "remarks": (form.get("remarks") or "").strip(),
        "withholding": bool(form.get("withholding")),
        "show_number": bool(form.get("show_number")),
        "post_date": (form.get("post_date") or "").strip() or issue_date,
        "revenue_account": form.get("revenue_account") or _code(conn, "売上高"),
        "items": calc["items"],
        "issuer": {k: (form.get(k) or "").strip() for k in ISSUER_KEYS},
    }
    current = get(conn, invoice_id) if invoice_id else None
    number = ((form.get("number") or "").strip() or (current["number"] if current else "")
              or next_number(conn, issue_date))
    if current and current["cancelled"]:
        raise InvoiceError("取消済みの請求書は変更できません")
    ts = db.now()
    description = f"請求書 {number}" + (f" {calc['items'][0]['description']}" if calc["items"] else "")
    entry_id = current["entry_id"] if current else None
    if post:
        lines = sales_lines(conn, calc, data["revenue_account"])
        post_date = ledger.parse_date(data["post_date"]).isoformat()
        if entry_id:
            live = conn.execute("SELECT deleted FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if live and not live["deleted"]:
                ledger.update_entry(conn, entry_id, post_date, lines, partner, description,
                                    reason or "請求書の修正")
            else:
                entry_id = None
        if not entry_id:
            entry_id = ledger.post_entry(conn, post_date, lines, partner, description, source="invoice")
    with conn:
        values = (number, issue_date, partner, json.dumps(data, ensure_ascii=False), calc["subtotal"],
                  calc["tax"], calc["withholding"], calc["total"], entry_id, ts)
        if current:
            conn.execute(
                "UPDATE invoices SET number=?, issue_date=?, partner=?, data=?, subtotal=?, tax=?, "
                "withholding=?, total=?, entry_id=?, updated_at=? WHERE id=?", values + (invoice_id,))
        else:
            invoice_id = conn.execute(
                "INSERT INTO invoices(number, issue_date, partner, data, subtotal, tax, withholding, total, "
                "entry_id, updated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", values + (ts,)).lastrowid
        db.audit(conn, "invoice", {"id": invoice_id, "number": number, "issue_date": issue_date,
                                   "partner": partner, "total": calc["total"], "data": data})
    if form.get("remember"):
        for k in ISSUER_KEYS:
            db.set_setting(conn, k, data["issuer"][k])
    return invoice_id


def record_payment(conn, invoice_id, date, received, fee=0, account=None):
    inv = get(conn, invoice_id)
    if not inv or inv["cancelled"]:
        raise InvoiceError("請求書が見つかりません")
    if inv["paid_entry_id"]:
        raise InvoiceError("入金は登録済みです")
    received, fee = _int(received), _int(fee)
    if received + fee != inv["total"]:
        raise InvoiceError(f"入金額＋振込手数料（{received + fee:,}円）が請求額（{inv['total']:,}円）と一致しません")
    lines = [{"side": "D", "account": account or _code(conn, "普通預金"), "amount": received, "tax": "NA"}]
    if fee:
        lines.append({"side": "D", "account": _code(conn, "支払手数料"), "amount": fee, "memo": "振込手数料"})
    lines.append({"side": "C", "account": _code(conn, "売掛金"), "amount": inv["total"], "tax": "NA"})
    eid = ledger.post_entry(conn, date, lines, inv["partner"], f"請求書 {inv['number']} 入金", source="invoice")
    with conn:
        conn.execute("UPDATE invoices SET paid_entry_id=?, updated_at=? WHERE id=?", (eid, db.now(), invoice_id))
        db.audit(conn, "invoice_paid", {"id": invoice_id, "entry_id": eid})
    return eid


def cancel(conn, invoice_id, reason):
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("取消の理由を入力してください")
    inv = get(conn, invoice_id)
    if not inv or inv["cancelled"]:
        raise InvoiceError("請求書が見つかりません")
    for eid in (inv["paid_entry_id"], inv["entry_id"]):
        if eid:
            live = conn.execute("SELECT deleted FROM entries WHERE id = ?", (eid,)).fetchone()
            if live and not live["deleted"]:
                ledger.delete_entry(conn, eid, f"請求書 {inv['number']} の取消: {reason}")
    with conn:
        conn.execute("UPDATE invoices SET cancelled=1, updated_at=? WHERE id=?", (db.now(), invoice_id))
        db.audit(conn, "invoice_cancel", {"id": invoice_id, "reason": reason})


# ---------------------------------------------------------------- 請求書 PDF の読取り

AMOUNT_RE = re.compile(r"^[▲△-]?[¥￥\\]?[▲△-]?\d{1,3}(,\d{3})+円?$|^[▲△-]?[¥￥\\]?[▲△-]?\d+円?$")
DATE_RE = re.compile(r"(\d{4})\s*[/年.\-]\s*(\d{1,2})\s*[/月.\-]\s*(\d{1,2})")
REIWA_RE = re.compile(r"令和\s*(\d+)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
LABELS = {
    "total": ("合計", "合計金額", "ご請求金額", "請求金額", "総額", "お支払金額"),
    "subtotal": ("小計", "税抜合計", "税抜金額"),
    "tax": ("消費税", "消費税額", "消費税等", "内消費税"),
    "withholding": ("源泉徴収", "源泉徴収税", "源泉徴収税額", "源泉所得税"),
}


def _norm(s):
    return unicodedata.normalize("NFKC", s).strip()


def _amount(token):
    t = _norm(token).replace(" ", "")
    if not AMOUNT_RE.match(t) or not re.search(r"\d", t):
        return None
    if re.fullmatch(r"\d{1,2}", t) and "¥" not in t:
        return None  # 「1」「10」のような番号は金額とみなさない
    neg = t[0] in "▲△-" or "-" in t[:3]
    v = int(re.sub(r"\D", "", t))
    return -v if neg else v


def _label_key(token):
    t = re.sub(r"[（(].*?[）)]|[:：\s]", "", _norm(token))
    for key, names in LABELS.items():
        if t in names:
            return key
    return None


def _parse_date(text):
    m = DATE_RE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = REIWA_RE.search(_norm(text))
    if m:
        return datetime.date(2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def read_pdf(conn, data):
    """請求書 PDF から日付・宛先・金額などを推定する（確認画面で修正できる前提）。"""
    try:
        rows = pdftext.lines(data)
    except Exception as exc:  # noqa: BLE001 壊れた・未対応の PDF
        raise InvoiceError(f"PDFを読めませんでした: {exc}") from None
    if not rows:
        raise InvoiceError("PDFから文字を読み取れませんでした（スキャン画像のPDFは読めません）")
    text = "\n".join(" ".join(r) for r in rows)
    tokens = [[t for cell in r for t in re.split(r"\s+", _norm(cell)) if t] for r in rows]
    found = {}
    for i, toks in enumerate(tokens):
        for j, tok in enumerate(toks):
            key = _label_key(tok)
            if not key or key in found:
                continue
            nxt = _amount(toks[j + 1]) if j + 1 < len(toks) else None
            if nxt is None and j + 1 == len(toks) and i + 1 < len(tokens):
                # 見出しの次の行に金額がある形式（「ご請求金額」の枠など）
                cand = [_amount(t) for t in tokens[i + 1]]
                cand = [c for c in cand if c is not None]
                nxt = cand[0] if len(cand) == 1 else None
            if nxt is not None:
                found[key] = abs(nxt)

    issue = None
    for r in rows:
        joined = " ".join(r)
        if re.search(r"発行日|請求日|発行年月日|請求年月日", joined) and _parse_date(joined):
            issue = _parse_date(joined)
            break
    issue = issue or _parse_date(text)

    addressee = ""
    for r in rows:
        for cell in r:
            m = re.match(r"^(.*?)\s*(御中|様)\s*$", _norm(cell))
            if m and m.group(1):
                addressee = m.group(1).strip()
                break
        if addressee:
            break

    description = ""
    for i, r in enumerate(rows):
        if any(re.search(r"内容|品名|品目|摘要|項目", c) for c in r) and i + 1 < len(rows):
            cands = [c for c in rows[i + 1] if not _amount(c) and not DATE_RE.search(c)
                     and not re.fullmatch(r"[\d.\s%h時間個式件]+", _norm(c))]
            if cands:
                description = max(cands, key=len)
            break

    owner = re.sub(r"\s", "", _norm(db.get_setting(conn, "owner_name") or ""))
    flat = re.sub(r"\s", "", _norm(text))
    if owner and owner in re.sub(r"\s", "", addressee):
        direction = "expense"
    elif owner and owner in flat:
        direction = "sales"
    else:
        direction = "sales" if found.get("withholding") else "expense"

    issuer = ""
    if direction == "expense":
        for r in rows[:15]:
            for cell in r:
                c = _norm(cell)
                if re.search(r"株式会社|有限会社|合同会社|事務所|商店|\(株\)|㈱", c) and "御中" not in c and "様" not in c:
                    issuer = c
                    break
            if issuer:
                break

    subtotal, tax = found.get("subtotal"), found.get("tax")
    withholding, total = found.get("withholding", 0), found.get("total")
    if total is None and subtotal is not None:
        total = subtotal + (tax or 0) - withholding
    if subtotal is None and total is not None:
        subtotal = total - (tax or 0) + withholding
    rate = 10
    if subtotal and tax is not None:
        rate = 8 if abs(subtotal * 8 // 100 - tax) <= 1 else 0 if tax == 0 else 10
    regno = re.search(r"T\d{13}", flat)
    warnings = []
    if total is None:
        warnings.append("合計金額を読み取れませんでした。手入力してください")
    elif subtotal is not None and subtotal + (tax or 0) - withholding != total:
        warnings.append("小計＋消費税−源泉徴収 が合計と一致しません。金額を確認してください")
    if direction == "sales" and withholding and subtotal and withholding != withholding_tax(subtotal):
        warnings.append(f"源泉徴収額が計算値（{withholding_tax(subtotal):,}円）と異なります")
    return {
        "direction": direction,
        "date": issue.isoformat() if issue else "",
        "partner": addressee if direction == "sales" else (issuer or ""),
        "description": description,
        "subtotal": subtotal or 0,
        "tax": tax or 0,
        "withholding": withholding,
        "total": total or 0,
        "rate": rate,
        "qualified": bool(regno),
        "regno": regno.group(0) if regno else "",
        "warnings": warnings,
        "text": text,
    }


def entry_from_document(conn, form):
    """確認画面の内容から仕訳を作る。"""
    direction = form.get("direction")
    date = ledger.parse_date(form.get("date")).isoformat()
    partner = (form.get("partner") or "").strip()
    description = (form.get("description") or "").strip()
    subtotal, tax = _int(form.get("subtotal")), _int(form.get("tax"))
    withholding, total = _int(form.get("withholding")), _int(form.get("total"))
    rate = RATES.get(str(form.get("rate", "10")), 10)
    gross = subtotal + tax
    if gross <= 0:
        raise InvoiceError("金額を入力してください")
    if gross - withholding != total:
        raise InvoiceError(f"小計＋消費税−源泉徴収（{gross - withholding:,}円）が合計（{total:,}円）と一致しません")
    if direction == "sales":
        calc = {"by_rate": {10: 0, 8: 0, 0: 0}, "tax_by_rate": {10: 0, 8: 0, 0: 0},
                "withholding": withholding, "total": total}
        calc["by_rate"][rate] = subtotal
        calc["tax_by_rate"][rate] = tax
        lines = sales_lines(conn, calc, form.get("account") or _code(conn, "売上高"))
    elif direction == "expense":
        account = form.get("account")
        if not account:
            raise InvoiceError("経費の勘定科目を選んでください")
        invoice = "Q" if form.get("qualified") else "N"
        lines = [{"side": "D", "account": account, "amount": gross, "tax": PURCHASE_TAX_FOR_RATE[rate],
                  "invoice": invoice}]
        lines.append({"side": "C", "account": form.get("credit_account") or _code(conn, "未払金"),
                      "amount": total, "tax": "NA"})
        if withholding:
            lines.append({"side": "C", "account": _code(conn, "預り金"), "amount": withholding,
                          "tax": "NA", "memo": WITHHOLDING_MEMO})
    else:
        raise InvoiceError("売上か経費かを選んでください")
    return ledger.post_entry(conn, date, lines, partner, description, source="document")


# ---------------------------------------------------------------- 証憑

def add_document(conn, data, filename, kind, date, amount, partner, entry_id=None, mime="application/pdf"):
    digest = hashlib.sha256(data).hexdigest()
    with conn:
        doc_id = conn.execute(
            "INSERT INTO documents(entry_id, kind, date, amount, partner, filename, mime, sha256, data, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry_id, kind, date, int(amount), partner, filename, mime, digest, data, db.now())).lastrowid
    return doc_id


def find_document_by_hash(conn, data):
    digest = hashlib.sha256(data).hexdigest()
    return conn.execute("SELECT id, entry_id, created_at FROM documents WHERE sha256 = ?", (digest,)).fetchone()


def search_documents(conn, date_from=None, date_to=None, amount_min=None, amount_max=None, partner=None, kind=None):
    where, params = [], []
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)
    if amount_min not in (None, ""):
        where.append("amount >= ?")
        params.append(int(amount_min))
    if amount_max not in (None, ""):
        where.append("amount <= ?")
        params.append(int(amount_max))
    if partner:
        where.append("partner LIKE ?")
        params.append(f"%{partner}%")
    if kind:
        where.append("kind = ?")
        params.append(kind)
    sql = "SELECT id, entry_id, kind, date, amount, partner, filename, created_at FROM documents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY date DESC, id DESC", params).fetchall()


def documents_for_entry(conn, entry_id):
    return conn.execute("SELECT id, kind, filename, created_at FROM documents WHERE entry_id = ? ORDER BY id",
                        (entry_id,)).fetchall()


# ---------------------------------------------------------------- 源泉徴収の集計

def withholding_summary(conn, year):
    """支払者ごとの売上（税込）と源泉徴収税額（確定申告書 第二表「所得の内訳」用）"""
    rows = conn.execute(
        "SELECT e.partner, a.category, a.name, l.side, l.amount, l.memo FROM lines l "
        "JOIN entries e ON e.id = l.entry_id JOIN accounts a ON a.code = l.account_code "
        "WHERE e.deleted = 0 AND e.year = ?", (year,)).fetchall()
    result = {}
    for r in rows:
        p = r["partner"] or "（取引先なし）"
        item = result.setdefault(p, {"partner": p, "revenue": 0, "withholding": 0})
        if r["category"] == "revenue":
            item["revenue"] += r["amount"] if r["side"] == "C" else -r["amount"]
        elif r["name"] == "事業主貸" and "源泉" in r["memo"]:
            item["withholding"] += r["amount"] if r["side"] == "D" else -r["amount"]
    return sorted((v for v in result.values() if v["withholding"]), key=lambda v: -v["revenue"])
