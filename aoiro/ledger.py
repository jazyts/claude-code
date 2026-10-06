"""仕訳の登録・訂正・削除（履歴付き）・検索・期首残高."""

import datetime
import hashlib
import json

from . import db

TAX_LABELS = {
    "S10": "課税売上10%",
    "S8": "課税売上8%（軽減）",
    "P10": "課税仕入10%",
    "P8": "課税仕入8%（軽減）",
    "EX": "非課税",
    "NA": "対象外・不課税",
}

INVOICE_LABELS = {
    "Q": "適格請求書あり",
    "S": "少額特例（1万円未満）",
    "N": "適格請求書なし（経過措置）",
    "Z": "控除対象外",
}

GENESIS_HASH = "0" * 64


class LedgerError(ValueError):
    pass


def parse_date(value):
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except ValueError:
        raise LedgerError(f"日付の形式が不正です: {value}") from None


def _check_open(conn, year):
    if year in db.closed_years(conn):
        raise LedgerError(f"{year}年は締め済みです（締めを解除してから訂正してください）")


def _normalize_lines(conn, lines):
    accounts = db.account_map(conn)
    result = []
    for raw in lines:
        code = str(raw.get("account", "")).strip()
        amount = raw.get("amount")
        if not code and not amount:
            continue
        if code not in accounts:
            raise LedgerError(f"勘定科目が不正です: {code}")
        try:
            amount = int(str(amount).replace(",", ""))
        except (TypeError, ValueError):
            raise LedgerError(f"金額が不正です: {amount}") from None
        if amount <= 0:
            raise LedgerError("金額は1円以上を入力してください")
        side = raw.get("side")
        if side not in ("D", "C"):
            raise LedgerError("借方/貸方の指定が不正です")
        tax = raw.get("tax") or accounts[code]["tax_default"]
        if tax not in TAX_LABELS:
            raise LedgerError(f"税区分が不正です: {tax}")
        invoice = raw.get("invoice") or "Q"
        if invoice not in INVOICE_LABELS:
            raise LedgerError(f"インボイス区分が不正です: {invoice}")
        if not tax.startswith("P"):
            invoice = "Q"
        result.append(
            {
                "side": side,
                "account": code,
                "amount": amount,
                "tax": tax,
                "invoice": invoice,
                "memo": str(raw.get("memo", "")).strip(),
            }
        )
    debit = sum(l["amount"] for l in result if l["side"] == "D")
    credit = sum(l["amount"] for l in result if l["side"] == "C")
    if not debit or not credit:
        raise LedgerError("借方と貸方をそれぞれ1行以上入力してください")
    if debit != credit:
        raise LedgerError(f"貸借が一致しません（借方 {debit:,} 円 / 貸方 {credit:,} 円）")
    return result


def validate_lines(conn, lines):
    """登録せずに仕訳行を検証・正規化する（取込のプレビュー用）。"""
    return _normalize_lines(conn, lines)


def check_open(conn, year):
    _check_open(conn, year)


def _snapshot(conn, entry_id):
    e = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    lines = conn.execute(
        "SELECT side, account_code, amount, tax, invoice, memo FROM lines "
        "WHERE entry_id = ? ORDER BY line_no",
        (entry_id,),
    ).fetchall()
    return {
        "year": e["year"],
        "entry_no": e["entry_no"],
        "date": e["date"],
        "partner": e["partner"],
        "description": e["description"],
        "source": e["source"],
        "deleted": bool(e["deleted"]),
        "lines": [
            {
                "side": l["side"],
                "account": l["account_code"],
                "amount": l["amount"],
                "tax": l["tax"],
                "invoice": l["invoice"],
                "memo": l["memo"],
            }
            for l in lines
        ],
    }


def _hash(prev_hash, entry_id, version, op, reason, snapshot, recorded_at):
    payload = json.dumps(
        [prev_hash, entry_id, version, op, reason, snapshot, recorded_at],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_history(conn, entry_id, version, op, reason):
    snapshot = json.dumps(_snapshot(conn, entry_id), ensure_ascii=False, sort_keys=True)
    row = conn.execute("SELECT hash FROM entry_history ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    recorded_at = db.now()
    digest = _hash(prev_hash, entry_id, version, op, reason, snapshot, recorded_at)
    conn.execute(
        "INSERT INTO entry_history(entry_id, version, op, reason, snapshot, recorded_at, "
        "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
        (entry_id, version, op, reason, snapshot, recorded_at, prev_hash, digest),
    )


def _write_lines(conn, entry_id, lines):
    conn.execute("DELETE FROM lines WHERE entry_id = ?", (entry_id,))
    conn.executemany(
        "INSERT INTO lines(entry_id, line_no, side, account_code, amount, tax, invoice, memo) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (entry_id, i + 1, l["side"], l["account"], l["amount"], l["tax"], l["invoice"], l["memo"])
            for i, l in enumerate(lines)
        ],
    )


def post_entry(conn, date, lines, partner="", description="", source="manual"):
    d = parse_date(date)
    _check_open(conn, d.year)
    lines = _normalize_lines(conn, lines)
    ts = db.now()
    with conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(entry_no), 0) + 1 AS n FROM entries WHERE year = ?", (d.year,)
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO entries(year, entry_no, date, partner, description, source, version, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (d.year, row["n"], d.isoformat(), partner.strip(), description.strip(), source, ts, ts),
        )
        entry_id = cur.lastrowid
        _write_lines(conn, entry_id, lines)
        _record_history(conn, entry_id, 1, "create", "")
    return entry_id


def _get_live(conn, entry_id):
    e = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not e:
        raise LedgerError("仕訳が見つかりません")
    if e["deleted"]:
        raise LedgerError("削除済みの仕訳です")
    return e


def update_entry(conn, entry_id, date, lines, partner, description, reason):
    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("訂正理由を入力してください")
    e = _get_live(conn, entry_id)
    d = parse_date(date)
    if d.year != e["year"]:
        raise LedgerError("年をまたぐ日付変更はできません。削除して新しい年で登録し直してください")
    _check_open(conn, e["year"])
    lines = _normalize_lines(conn, lines)
    version = e["version"] + 1
    with conn:
        conn.execute(
            "UPDATE entries SET date=?, partner=?, description=?, version=?, updated_at=? "
            "WHERE id=?",
            (d.isoformat(), partner.strip(), description.strip(), version, db.now(), entry_id),
        )
        _write_lines(conn, entry_id, lines)
        _record_history(conn, entry_id, version, "update", reason)


def delete_entry(conn, entry_id, reason):
    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("削除理由を入力してください")
    e = _get_live(conn, entry_id)
    _check_open(conn, e["year"])
    version = e["version"] + 1
    with conn:
        conn.execute(
            "UPDATE entries SET deleted=1, version=?, updated_at=? WHERE id=?",
            (version, db.now(), entry_id),
        )
        _record_history(conn, entry_id, version, "delete", reason)


def get_entry(conn, entry_id):
    e = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not e:
        return None
    lines = conn.execute(
        "SELECT l.*, a.name AS account_name FROM lines l JOIN accounts a ON a.code = l.account_code "
        "WHERE entry_id = ? ORDER BY line_no",
        (entry_id,),
    ).fetchall()
    return {"entry": e, "lines": lines}


def history(conn, entry_id):
    rows = conn.execute(
        "SELECT * FROM entry_history WHERE entry_id = ? ORDER BY version", (entry_id,)
    ).fetchall()
    return [dict(r, snapshot=json.loads(r["snapshot"])) for r in rows]


def verify(conn):
    """履歴のハッシュチェーンと、現在の仕訳が最新履歴と一致するかを検証する。"""
    problems = []
    prev = GENESIS_HASH
    latest = {}
    for r in conn.execute("SELECT * FROM entry_history ORDER BY id"):
        if r["prev_hash"] != prev:
            problems.append(f"履歴 #{r['id']}: 直前のハッシュが一致しません")
        expected = _hash(
            r["prev_hash"], r["entry_id"], r["version"], r["op"], r["reason"],
            r["snapshot"], r["recorded_at"],
        )
        if expected != r["hash"]:
            problems.append(f"履歴 #{r['id']}: ハッシュが一致しません（改ざんの可能性）")
        prev = r["hash"]
        latest[r["entry_id"]] = r
    for e in conn.execute("SELECT id, version, year, entry_no FROM entries"):
        h = latest.get(e["id"])
        label = f"{e['year']}年 No.{e['entry_no']}"
        if not h:
            problems.append(f"仕訳 {label}: 履歴がありません")
            continue
        if h["version"] != e["version"]:
            problems.append(f"仕訳 {label}: 版数が履歴と一致しません")
        current = json.dumps(_snapshot(conn, e["id"]), ensure_ascii=False, sort_keys=True)
        if current != h["snapshot"]:
            problems.append(f"仕訳 {label}: 内容が履歴と一致しません（履歴外の変更）")
    return problems


def search(conn, year=None, date_from=None, date_to=None, amount_min=None, amount_max=None,
           partner=None, account=None, text=None, include_deleted=False, source=None):
    """取引年月日・取引金額（範囲指定可）・取引先などを組み合わせて検索する。"""
    where, params = [], []
    if year:
        where.append("e.year = ?")
        params.append(int(year))
    if date_from:
        where.append("e.date >= ?")
        params.append(parse_date(date_from).isoformat())
    if date_to:
        where.append("e.date <= ?")
        params.append(parse_date(date_to).isoformat())
    if amount_min not in (None, "") or amount_max not in (None, ""):
        # いずれかの仕訳行の金額が範囲内にある仕訳
        lo = int(amount_min) if amount_min not in (None, "") else 0
        hi = int(amount_max) if amount_max not in (None, "") else 10**15
        where.append(
            "EXISTS (SELECT 1 FROM lines x WHERE x.entry_id = e.id AND x.amount BETWEEN ? AND ?)"
        )
        params += [lo, hi]
    if partner:
        where.append("e.partner LIKE ?")
        params.append(f"%{partner}%")
    if account:
        where.append("EXISTS (SELECT 1 FROM lines x WHERE x.entry_id = e.id AND x.account_code = ?)")
        params.append(account)
    if text:
        where.append(
            "(e.description LIKE ? OR EXISTS (SELECT 1 FROM lines x WHERE x.entry_id = e.id "
            "AND x.memo LIKE ?))"
        )
        params += [f"%{text}%", f"%{text}%"]
    if source:
        where.append("e.source = ?")
        params.append(source)
    if not include_deleted:
        where.append("e.deleted = 0")
    sql = "SELECT e.* FROM entries e"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.date, e.entry_no"
    entries = conn.execute(sql, params).fetchall()
    return [get_entry(conn, e["id"]) for e in entries]


def get_opening(conn, year):
    rows = conn.execute("SELECT account_code, amount FROM opening WHERE year = ?", (year,))
    return {r["account_code"]: r["amount"] for r in rows}


def set_opening(conn, year, amounts):
    """期首残高を保存する。amounts: {科目コード: 金額（正常残高側がプラス）}"""
    year = int(year)
    _check_open(conn, year)
    accounts = db.account_map(conn)
    clean = {}
    for code, amount in amounts.items():
        if code not in accounts:
            raise LedgerError(f"勘定科目が不正です: {code}")
        if accounts[code]["category"] in ("revenue", "expense"):
            continue
        amount = int(str(amount or 0).replace(",", ""))
        if amount:
            clean[code] = amount
    with conn:
        conn.execute("DELETE FROM opening WHERE year = ?", (year,))
        conn.executemany(
            "INSERT INTO opening(year, account_code, amount) VALUES (?,?,?)",
            [(year, c, a) for c, a in clean.items()],
        )
        db.audit(conn, "opening", {"year": year, "amounts": clean})
    return clean


def opening_balanced(conn, year):
    """期首残高の貸借差額（資産 − 負債 − 資本）。0 なら一致。"""
    accounts = db.account_map(conn)
    diff = 0
    for code, amount in get_opening(conn, year).items():
        if accounts[code]["category"] == "asset":
            diff += amount
        else:
            diff -= amount
    return diff
