"""Excel（.xlsx）・CSV からの仕訳と期首残高の取込."""

import csv
import datetime
import hashlib
import io
import json
import re
import unicodedata

from . import db, ledger, xlsx

COLUMNS = {
    "date": ("日付", "取引日", "年月日", "取引年月日"),
    "group": ("伝票番号", "伝票no", "伝票", "no", "番号"),
    "partner": ("取引先", "相手先"),
    "description": ("摘要", "内容"),
    "dr_account": ("借方科目", "借方勘定科目"),
    "dr_amount": ("借方金額",),
    "cr_account": ("貸方科目", "貸方勘定科目"),
    "cr_amount": ("貸方金額",),
    "tax": ("税区分", "消費税区分"),
    "invoice": ("インボイス", "適格請求書"),
    "memo": ("メモ", "備考"),
}
OPENING_COLUMNS = {
    "account": ("科目", "勘定科目"),
    "amount": ("金額", "期首残高", "残高"),
}
JOURNAL_HEADER = ["日付", "伝票番号", "取引先", "摘要", "借方科目", "借方金額", "貸方科目", "貸方金額",
                  "税区分", "インボイス", "メモ"]

# よくある言い換え → 科目名
ALIASES = {
    "売上": "売上高", "仕入": "仕入高", "預金": "普通預金", "交際費": "接待交際費", "家賃": "地代家賃",
    "手数料": "支払手数料", "振込手数料": "支払手数料", "減価償却": "減価償却費", "外注費": "外注工賃",
    "保険料": "損害保険料", "支払利息": "利子割引料", "光熱費": "水道光熱費", "交通費": "旅費交通費",
    "書籍代": "新聞図書費", "図書費": "新聞図書費", "備品": "工具器具備品",
}


class ImportError_(ValueError):
    pass


def _norm(value):
    return unicodedata.normalize("NFKC", str(value)).strip()


def _key(value):
    return re.sub(r"[\s.．・/]", "", _norm(value)).lower()


def _map_header(row, spec):
    keys = [_key(c) for c in row]
    found = {}
    for field, names in spec.items():
        for i, k in enumerate(keys):
            if k in names:
                found[field] = i
                break
    return found


def _find_header(rows, spec, required):
    for i, row in enumerate(rows[:20]):
        mapping = _map_header(row, spec)
        if all(f in mapping for f in required):
            return i, mapping
    return None, None


def parse_date(value):
    if value in ("", None):
        return None
    if isinstance(value, float):
        return xlsx.serial_to_date(value)
    s = _norm(value)
    m = re.fullmatch(r"(令和|R)(\d+)[年./-](\d+)[月./-](\d+)日?", s, re.I)
    if m:
        return datetime.date(2018 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?(\s.*)?", s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"\d+(\.0+)?", s) and 30000 < float(s) < 80000:
        return xlsx.serial_to_date(float(s))
    raise ImportError_(f"日付が読めません: {value}")


def parse_amount(value):
    if value in ("", None):
        return 0
    if isinstance(value, float):
        return int(round(value))
    s = re.sub(r"[,¥￥円\s]", "", _norm(value))
    if not s:
        return 0
    try:
        return int(round(float(s)))
    except ValueError:
        raise ImportError_(f"金額が読めません: {value}") from None


def resolve_account(conn, value):
    s = _norm(value)
    if not s:
        return None
    accounts = db.accounts(conn)
    by_code = {a["code"]: a for a in accounts}
    by_name = {_key(a["name"]): a for a in accounts}
    if s in by_code:
        return by_code[s]
    m = re.match(r"^(\d+)\s*(.*)$", s)  # 「609 消耗品費」のような形式
    if m and m.group(1) in by_code:
        return by_code[m.group(1)]
    k = _key(s)
    if k in by_name:
        return by_name[k]
    alias = ALIASES.get(s)
    if alias and _key(alias) in by_name:
        return by_name[_key(alias)]
    raise ImportError_(f"勘定科目「{s}」がありません（科目名を直すか、勘定科目の画面で追加してください）")


def parse_tax(value, account):
    s = _key(value)
    if not s:
        return None
    if value in ledger.TAX_LABELS:
        return value
    for code, label in ledger.TAX_LABELS.items():
        if s == _key(label):
            return code
    if "非課税" in s:
        return "EX"
    if any(w in s for w in ("不課税", "対象外", "課税対象外", "なし")):
        return "NA"
    rate = "8" if "8" in s else "10" if "10" in s else None
    if rate:
        if "売上" in s:
            return "S" + rate
        if "仕入" in s:
            return "P" + rate
        return ("S" if account["category"] == "revenue" else "P") + rate
    raise ImportError_(f"税区分が読めません: {value}")


def parse_invoice(value):
    s = _key(value)
    if not s:
        return "Q"
    if "少額" in s:
        return "S"
    if any(w in s for w in ("控除不可", "対象外", "控除対象外")):
        return "Z"
    if any(w in s for w in ("なし", "無", "×", "x", "経過", "未登録")):
        return "N"
    if any(w in s for w in ("あり", "有", "○", "〇", "適格", "q")):
        return "Q"
    raise ImportError_(f"インボイス区分が読めません: {value}")


def _taxable_side(dr, cr):
    """税区分を付ける側（収益・費用・固定資産など既定税区分が対象外でない科目）"""
    for a, side in ((dr, "D"), (cr, "C")):
        if a is not None and (a["category"] in ("revenue", "expense") or a["tax_default"] != "NA"):
            return side
    return None


def read_rows(data, filename):
    """ファイルを {シート名: 行の配列} にする。"""
    name = filename.lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        try:
            return xlsx.read(data)
        except Exception as exc:  # noqa: BLE001 壊れたファイルなど
            raise ImportError_(f"Excelファイルを読めません（.xlsx 形式で保存してください）: {exc}") from None
    if name.endswith(".xls"):
        raise ImportError_("古い .xls 形式は読めません。Excelで「.xlsx」形式で保存し直してください")
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("CSVの文字コードが読めません（UTF-8 か Shift_JIS で保存してください）")
    return {"仕訳": list(csv.reader(io.StringIO(text)))}


def parse(conn, data, filename=""):
    sheets = read_rows(data, filename)
    result = {"entries": [], "opening": None, "errors": [], "warnings": [],
              "sha256": hashlib.sha256(data).hexdigest(), "filename": filename}

    journal = None
    for name, rows in sheets.items():
        if "期首" in name:
            continue
        idx, mapping = _find_header(rows, COLUMNS, ("dr_account", "cr_account"))
        if idx is not None:
            journal = (name, rows, idx, mapping)
            break
    if journal:
        _parse_journal(conn, journal, result)

    for name, rows in sheets.items():
        if "期首" not in name:
            continue
        idx, mapping = _find_header(rows, OPENING_COLUMNS, ("account", "amount"))
        if idx is not None:
            _parse_opening(conn, name, rows, idx, mapping, result)

    if not journal and result["opening"] is None:
        result["errors"].append(("", "見出し行が見つかりません（「借方科目」「貸方科目」の列、"
                                     "または「期首残高」シートの「科目」「金額」の列が必要です）"))
    _check_duplicates(conn, result)
    return result


def _parse_journal(conn, journal, result):
    sheet, rows, header_idx, m = journal
    get = lambda row, f: row[m[f]] if f in m and m[f] < len(row) else ""
    groups = []  # [{"rows": [...], "key": ...}]
    by_key = {}
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        rowno = i + 1
        if not any(_norm(c) for c in row):
            continue
        key = _norm(get(row, "group"))
        date_raw = get(row, "date")
        if key and key in by_key:
            g = by_key[key]
        elif not key and date_raw in ("", None) and groups:
            g = groups[-1]  # 日付が空欄の行は直前の仕訳の続き
        else:
            g = {"rows": [], "key": key}
            groups.append(g)
            if key:
                by_key[key] = g
        g["rows"].append((rowno, row))

    for g in groups:
        rownos = [r for r, _ in g["rows"]]
        label = f"{sheet} {rownos[0]}行目" + (f"〜{rownos[-1]}行目" if len(rownos) > 1 else "")
        try:
            date = partner = description = None
            lines = []
            for rowno, row in g["rows"]:
                d = parse_date(get(row, "date"))
                date = date or d
                partner = partner or _norm(get(row, "partner"))
                description = description or _norm(get(row, "description"))
                memo = _norm(get(row, "memo"))
                dr = resolve_account(conn, get(row, "dr_account"))
                cr = resolve_account(conn, get(row, "cr_account"))
                dr_amt = parse_amount(get(row, "dr_amount"))
                cr_amt = parse_amount(get(row, "cr_amount"))
                if dr and not dr_amt:
                    dr_amt = cr_amt if cr else 0
                if cr and not cr_amt:
                    cr_amt = dr_amt if dr else 0
                tax_side = _taxable_side(dr, cr)
                tax_value = get(row, "tax")
                invoice = parse_invoice(get(row, "invoice"))
                for acct, amt, side in ((dr, dr_amt, "D"), (cr, cr_amt, "C")):
                    if acct is None:
                        if amt:
                            raise ImportError_(f"{rowno}行目: 金額があるのに{'借方' if side == 'D' else '貸方'}科目が空欄です")
                        continue
                    if amt <= 0:
                        raise ImportError_(f"{rowno}行目: {acct['name']} の金額がありません")
                    tax = parse_tax(tax_value, acct) if side == tax_side else None
                    lines.append({"side": side, "account": acct["code"], "amount": amt, "tax": tax,
                                  "invoice": invoice, "memo": memo})
            if not date:
                raise ImportError_("日付がありません")
            ledger.check_open(conn, date.year)
            lines = ledger.validate_lines(conn, lines)
            result["entries"].append({"label": label, "date": date.isoformat(), "partner": partner or "",
                                      "description": description or "", "lines": lines})
        except (ImportError_, ledger.LedgerError, ValueError) as exc:
            result["errors"].append((label, str(exc)))


def _parse_opening(conn, sheet, rows, header_idx, m, result):
    amounts = {}
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        get = lambda f: row[m[f]] if m[f] < len(row) else ""
        if not _norm(get("account")):
            continue
        try:
            acct = resolve_account(conn, get("account"))
            if acct["category"] in ("revenue", "expense"):
                raise ImportError_(f"{acct['name']} は期首残高のない科目です")
            if acct["name"] in ("事業主貸", "事業主借"):
                continue  # 期首は常に0
            amounts[acct["code"]] = amounts.get(acct["code"], 0) + parse_amount(get("amount"))
        except (ImportError_, ValueError) as exc:
            result["errors"].append((f"{sheet} {i + 1}行目", str(exc)))
    result["opening"] = amounts


def _check_duplicates(conn, result):
    for e in result["entries"]:
        total = sum(l["amount"] for l in e["lines"] if l["side"] == "D")
        for item in ledger.search(conn, date_from=e["date"], date_to=e["date"]):
            t = sum(l["amount"] for l in item["lines"] if l["side"] == "D")
            if t == total and item["entry"]["partner"] == e["partner"]:
                e["duplicate"] = item["entry"]["entry_no"]
                result["warnings"].append(
                    f"{e['label']}: 同じ日付・取引先・金額の仕訳が登録済みです（No.{item['entry']['entry_no']}）")
                break
    for r in conn.execute("SELECT at, detail FROM audit_log WHERE action = 'import' ORDER BY id"):
        if json.loads(r["detail"]).get("sha256") == result["sha256"]:
            result["warnings"].insert(0, f"このファイルは {r['at']} に取り込み済みです")
            result["already_imported"] = True
            break


def commit(conn, parsed, year, skip_duplicates=True, auto_capital=True):
    """parse() の結果を登録する。エラーがある場合は何も登録しない。"""
    if parsed["errors"]:
        raise ImportError_("エラーがあるため取り込めません")
    year = int(year)
    if parsed["opening"] is not None:
        amounts = dict(parsed["opening"])
        capital = conn.execute("SELECT code FROM accounts WHERE name = '元入金'").fetchone()["code"]
        if auto_capital:
            amap = db.account_map(conn)
            amounts[capital] = sum(v if amap[c]["category"] == "asset" else -v
                                   for c, v in amounts.items() if c != capital)
        ledger.set_opening(conn, year, amounts)
    ids = []
    for e in parsed["entries"]:
        if skip_duplicates and e.get("duplicate"):
            continue
        ids.append(ledger.post_entry(conn, e["date"], e["lines"], e["partner"], e["description"],
                                     source="import"))
    with conn:
        db.audit(conn, "import", {"filename": parsed["filename"], "sha256": parsed["sha256"],
                                  "entries": len(ids), "opening": parsed["opening"] is not None})
    return ids


def template(conn):
    """取込用の Excel テンプレート"""
    journal = [JOURNAL_HEADER,
               ["2026/3/25", "", "株式会社A", "3月分 業務委託料", "売掛金", 2750000, "売上高", 2750000, "課税売上10%", "", ""],
               ["2026/3/31", "1", "株式会社A", "3月分 入金", "普通預金", 2749560, "売掛金", 2750000, "", "", ""],
               ["", "1", "", "", "支払手数料", 440, "", "", "課税仕入10%", "あり", "振込手数料"],
               ["2026/4/2", "", "文具店", "コピー用紙", "消耗品費", 1100, "事業主借", 1100, "課税仕入10%", "あり", ""],
               ["2026/4/5", "", "", "生活費", "事業主貸", 300000, "普通預金", 300000, "", "", ""]]
    opening = [["科目", "金額"], ["普通預金", 1000000], ["売掛金", 2750000], ["未払金", 50000]]
    accts = [["コード", "科目名", "区分", "既定の税区分"]] + [
        [a["code"], a["name"], db.CATEGORY_LABELS[a["category"]], ledger.TAX_LABELS[a["tax_default"]]]
        for a in db.accounts(conn, active_only=True)]
    return xlsx.write({"仕訳": journal, "期首残高": opening, "勘定科目一覧": accts})


def claude_prompt(conn):
    names = "、".join(a["name"] for a in db.accounts(conn, active_only=True))
    exempt = ("- 免税事業者なので「税区分」「インボイス」の列は空欄でよい\n"
              if db.get_setting(conn, "tax_method") == "exempt" else "")
    return f"""個人事業主（青色申告・税込経理）の仕訳を、会計ソフトに取り込める Excel（.xlsx）にしてください。

【シート「仕訳」】1行目に次の見出しを付けてください:
{' / '.join(JOURNAL_HEADER)}
- 日付: 2026/3/25 の形式
- 金額は消費税込みの整数（円）。借方合計と貸方合計を一致させる
- 1つの取引が3行以上になる場合（振込手数料が引かれた入金など）は、同じ「伝票番号」を付けて複数行に分ける。2行で済む取引は伝票番号を空欄でよい
- 税区分: 課税売上10% / 課税売上8% / 課税仕入10% / 課税仕入8% / 非課税 / 対象外 のいずれか（空欄なら科目の既定）
- インボイス: 課税仕入のときだけ「あり」「なし」「少額」のいずれか（空欄は「あり」扱い）
- 事業用口座以外（個人のお金・個人カード）で払った経費は貸方を「事業主借」、事業用口座から生活費に回したお金は借方を「事業主貸」にする
{exempt}- 勘定科目は次の中から選ぶ: {names}

【シート「期首残高」】（期首残高も入れる場合のみ）見出しは「科目」「金額」。
前年の青色申告決算書4ページ目の期末の資産・負債を、正の金額で書く（事業主貸・事業主借・元入金は不要。元入金は自動計算）。
"""
