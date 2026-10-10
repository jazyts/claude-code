"""Claude と連携するための MCP サーバー（標準入出力・JSON-RPC、標準ライブラリのみ）.

Claude Desktop に登録すると、Claude に話しかけるだけで仕訳の登録・検索・決算書の確認・
請求書の作成などができます。登録は `python -m aoiro mcp-install` で行います。
"""

import datetime
import json
import os
import sys
import traceback

from . import ctax, db, importer, invoices, itax, ledger, reports, yearend

PROTOCOL_VERSION = "2025-06-18"
WEB_URL = "http://127.0.0.1:8765"

INSTRUCTIONS = """個人事業主の青色申告用会計ソフト（複式簿記・税込経理）です。
- 金額はすべて消費税込みの整数（円）。仕訳は借方合計＝貸方合計。
- 事業用口座以外（個人のお金・カード）で払った経費は貸方「事業主借」、事業用口座から生活費は借方「事業主貸」。
- 源泉徴収された売上は「売掛金（入金予定額）＋ 事業主貸（メモ: 源泉所得税）／ 売上高（税込）」。請求書を作るなら create_invoice を使うと自動でこの仕訳になる。
- 勘定科目が分からないときは get_overview で一覧を確認する。科目は名前でもコードでも指定できる。
- 登録・訂正・削除の前に、内容（日付・科目・金額）をユーザーに示して確認を取ること。複数件はまず dry_run=true で検証する。
- 訂正・削除には理由が必要で、履歴が残る。締めた年は変更できない。"""


def _year(conn, value):
    return int(value or db.get_setting(conn, "current_year"))


def _fmt_entry(item):
    e = item["entry"]
    lines = "\n".join(
        f"  {'借方' if l['side'] == 'D' else '貸方'} {l['account_name']} {l['amount']:,}円"
        + (f" [{ledger.TAX_LABELS[l['tax']]}]" if l["tax"] != "NA" else "")
        + (f" ({l['memo']})" if l["memo"] else "")
        for l in item["lines"])
    head = f"{e['year']}年 No.{e['entry_no']} {e['date']} {e['partner']} {e['description']}".rstrip()
    return head + (" ［削除済み］" if e["deleted"] else "") + "\n" + lines


def _lines(conn, raw_lines):
    result = []
    for l in raw_lines or []:
        side = str(l.get("side", "")).strip()
        side = {"借方": "D", "貸方": "C", "debit": "D", "credit": "C", "d": "D", "c": "C"}.get(side.lower(), side)
        acct = importer.resolve_account(conn, l.get("account"))
        if acct is None:
            raise ledger.LedgerError("勘定科目が空欄の行があります")
        tax = importer.parse_tax(l.get("tax") or "", acct)
        result.append({"side": side, "account": acct["code"], "amount": importer.parse_amount(l.get("amount")),
                       "tax": tax, "invoice": importer.parse_invoice(l.get("invoice") or ""),
                       "memo": l.get("memo") or ""})
    return result


def _find_entry(conn, args):
    if args.get("entry_id"):
        row = conn.execute("SELECT id FROM entries WHERE id = ?", (int(args["entry_id"]),)).fetchone()
    else:
        row = conn.execute("SELECT id FROM entries WHERE year = ? AND entry_no = ?",
                           (_year(conn, args.get("year")), int(args.get("entry_no") or 0))).fetchone()
    if not row:
        raise ledger.LedgerError("仕訳が見つかりません（year と entry_no を指定してください）")
    return row["id"]


def _preview(conn, date, lines, partner, description):
    amap = db.account_map(conn)
    normalized = ledger.validate_lines(conn, lines)
    ledger.check_open(conn, ledger.parse_date(date).year)
    body = "\n".join(f"  {'借方' if l['side'] == 'D' else '貸方'} {amap[l['account']]['name']} {l['amount']:,}円"
                     + (f" [{ledger.TAX_LABELS[l['tax']]}]" if l["tax"] != "NA" else "") for l in normalized)
    return f"{date} {partner} {description}".rstrip() + "\n" + body


# ---------------------------------------------------------------- ツール

def t_get_overview(conn, args):
    year = _year(conn, args.get("year"))
    accts = [f"{a['code']} {a['name']}（{db.CATEGORY_LABELS[a['category']]}・既定 {ledger.TAX_LABELS[a['tax_default']]}）"
             for a in db.accounts(conn, active_only=True)]
    pl = reports.profit_loss(conn, year)
    closed = sorted(db.closed_years(conn))
    return (f"屋号: {db.get_setting(conn, 'business_name')} / 氏名: {db.get_setting(conn, 'owner_name')}\n"
            f"表示中の年度: {year}年 / 締め済み: {closed or 'なし'}\n"
            f"消費税: {ctax.METHOD_LABELS[db.get_setting(conn, 'tax_method')]}\n"
            f"{year}年 売上 {pl['sales']:,}円 / 経費 {pl['cost'] + pl['expense_total']:,}円 / 控除前所得 {pl['pre_income']:,}円\n"
            f"画面: {WEB_URL}/\n\n勘定科目:\n" + "\n".join(accts))


def t_search_entries(conn, args):
    items = ledger.search(conn, year=None if args.get("all_years") else _year(conn, args.get("year")),
                          date_from=args.get("date_from"), date_to=args.get("date_to"),
                          amount_min=args.get("amount_min"), amount_max=args.get("amount_max"),
                          partner=args.get("partner"),
                          account=importer.resolve_account(conn, args["account"])["code"] if args.get("account") else None,
                          text=args.get("text"), include_deleted=bool(args.get("include_deleted")))
    limit = int(args.get("limit") or 100)
    out = "\n\n".join(_fmt_entry(i) for i in items[:limit])
    more = f"\n\n…ほか {len(items) - limit} 件" if len(items) > limit else ""
    return f"{len(items)} 件\n\n{out}{more}" if items else "該当する仕訳はありません"


def t_post_entries(conn, args):
    entries = args.get("entries") or []
    if not entries:
        raise ledger.LedgerError("entries が空です")
    prepared, errors = [], []
    for i, e in enumerate(entries, 1):
        try:
            lines = _lines(conn, e.get("lines"))
            prepared.append((e.get("date"), lines, e.get("partner") or "", e.get("description") or "",
                             _preview(conn, e.get("date"), lines, e.get("partner") or "", e.get("description") or "")))
        except (ledger.LedgerError, importer.ImportError_, ValueError) as exc:
            errors.append(f"{i}件目: {exc}")
    if errors:
        raise ledger.LedgerError("登録していません（エラーがあります）:\n" + "\n".join(errors))
    if args.get("dry_run"):
        return "検証OK（まだ登録していません）:\n\n" + "\n\n".join(p[4] for p in prepared)
    done = []
    for date, lines, partner, desc, _ in prepared:
        eid = ledger.post_entry(conn, date, lines, partner, desc, source="claude")
        done.append(_fmt_entry(ledger.get_entry(conn, eid)))
    return f"{len(done)} 件登録しました:\n\n" + "\n\n".join(done)


def t_update_entry(conn, args):
    eid = _find_entry(conn, args)
    cur = ledger.get_entry(conn, eid)
    e = cur["entry"]
    lines = _lines(conn, args["lines"]) if args.get("lines") else [
        {"side": l["side"], "account": l["account_code"], "amount": l["amount"], "tax": l["tax"],
         "invoice": l["invoice"], "memo": l["memo"]} for l in cur["lines"]]
    ledger.update_entry(conn, eid, args.get("date") or e["date"], lines,
                        args["partner"] if args.get("partner") is not None else e["partner"],
                        args["description"] if args.get("description") is not None else e["description"],
                        args.get("reason"))
    return "訂正しました:\n" + _fmt_entry(ledger.get_entry(conn, eid))


def t_delete_entry(conn, args):
    eid = _find_entry(conn, args)
    ledger.delete_entry(conn, eid, args.get("reason"))
    return "削除しました（履歴は残ります）:\n" + _fmt_entry(ledger.get_entry(conn, eid))


def t_get_report(conn, args):
    year = _year(conn, args.get("year"))
    kind = args.get("kind")
    if kind == "tax_forecast":
        f = itax.forecast(conn, year)
        r = f["result"]
        sched = "\n".join(f"  {d} {label}: {v:,}" for d, label, v in f["schedule"])
        whatif = "\n".join(f"  {w['label']}: 税金 {w['saving']:,}円減（{w['note']}）" for w in f["whatif"])
        return (f"{year}年分 税金予測（概算）\n控除前所得 {r['pre_income']:,} / 青色申告特別控除 {r['blue']:,} / 総所得 {r['total_income']:,}\n"
                f"所得控除 合計（所得税）{r['deduction_total'][0]:,}\n課税所得 {r['taxable']:,}（限界税率 {r['marginal_rate']}%）\n"
                f"所得税及び復興特別所得税 {r['income_tax']:,} − 源泉徴収 {r['withholding']:,} − 予定納税 {r['prepaid']:,} "
                f"= {'納付' if r['income_tax_due'] >= 0 else '還付'} {abs(r['income_tax_due']):,}\n"
                f"住民税（翌年度）{r['resident_tax']:,} / 個人事業税 {r['biz_tax']:,} / 消費税 {r['ctax']:,}\n"
                f"税金合計 {r['taxes']:,}（所得の {r['effective_rate']}%）/ 税金・保険料を払った後の手取り {r['net']:,}\n"
                f"あと10万円なら:\n{whatif}\n納税スケジュール:\n{sched}\n"
                "所得控除（社会保険料・配偶者・扶養など）はアプリの「税金予測」画面で入力した値を使います。概算のため申告時は要確認。")
    if kind == "trial_balance":
        rows, d, c = reports.trial_balance(conn, year, args.get("date_to"))
        return f"{year}年 試算表\n" + "\n".join(
            f"{b['account']['name']}: 期首 {b['opening']:,} / 借方 {b['debit']:,} / 貸方 {b['credit']:,} / 残高 {b['closing']:,}"
            for b in rows) + f"\n合計 借方 {d:,} / 貸方 {c:,}"
    if kind == "profit_loss":
        pl = reports.profit_loss(conn, year)
        exp = "\n".join(f"  {e['no']} {e['name']}: {e['amount']:,}" for e in pl["expenses"] if e["amount"])
        return (f"{year}年 損益計算書（青色申告決算書）\n売上（収入）金額: {pl['sales']:,}\n差引原価: {pl['cost']:,}\n"
                f"経費:\n{exp}\n経費計: {pl['expense_total']:,}\n青色申告特別控除前の所得: {pl['pre_income']:,}\n"
                f"青色申告特別控除: {pl['blue_deduction']:,}\n所得金額: {pl['income']:,}")
    if kind == "balance_sheet":
        bs = reports.balance_sheet(conn, year)
        side = lambda rows: "\n".join(f"  {r['name']}: 期首 {r['opening']:,} / 期末 {r['closing']:,}" for r in rows)
        return (f"{year}年 貸借対照表\n資産:\n{side(bs['assets'])}\n負債・資本:\n{side(bs['liabilities'])}\n"
                f"一致: {'はい' if bs['balanced'] else 'いいえ（期首残高を確認）'}")
    if kind == "monthly":
        m = reports.monthly(conn, year)
        return f"{year}年 月別売上\n" + "\n".join(f"  {i + 1}月: {v:,}" for i, v in enumerate(m["sales"])) + \
            f"\n  雑収入: {m['misc_income']:,}\n  計: {m['total_sales']:,}"
    if kind == "consumption_tax":
        t = ctax.compute(conn, year)
        return (f"{year}年 消費税（{ctax.METHOD_LABELS[t['method']]}・概算）\n課税売上10% {t['sales10']:,} / 8% {t['sales8']:,}\n"
                f"売上の消費税額 {t['sales_tax']:,} / 控除 {t['deduction']:,}\n国税 {t['national']:,} / 地方 {t['local']:,} / 合計 {t['total']:,}")
    if kind == "withholding":
        rows = invoices.withholding_summary(conn, year)
        return f"{year}年 源泉徴収（確定申告書 第二表 所得の内訳）\n" + "\n".join(
            f"  {r['partner']}: 収入 {r['revenue']:,} / 源泉徴収税額 {r['withholding']:,}" for r in rows) + \
            f"\n  合計 {sum(r['withholding'] for r in rows):,}"
    if kind == "depreciation":
        rows = yearend.depreciation_schedule(conn, year)
        if not rows:
            return "償却資産はありません"
        return f"{year}年 減価償却\n" + "\n".join(
            f"  {r['asset']['name']}: 期首 {r['opening_book']:,} / 償却 {r['depreciation']:,}（経費 {r['business']:,}）/ 期末 {r['closing_book']:,}"
            for r in rows)
    raise ledger.LedgerError("kind が不正です")


def t_general_ledger(conn, args):
    acct = importer.resolve_account(conn, args.get("account"))
    acct, rows = reports.general_ledger(conn, _year(conn, args.get("year")), acct["code"])
    return f"総勘定元帳 {acct['name']}\n" + "\n".join(
        f"{r['date']} No.{r['entry_no']} {r['counter']} {r['description']} 借方 {r['debit']:,} 貸方 {r['credit']:,} 残高 {r['balance']:,}"
        for r in rows)


def t_create_invoice(conn, args):
    form = dict(invoices.issuer_defaults(conn))
    form.update({k: args[k] for k in ("issue_date", "partner", "honorific", "due", "remarks", "post_date") if args.get(k)})
    form["withholding"] = args.get("withholding", True)
    form["show_number"] = bool(args.get("show_number"))
    form["items"] = [{**it, "rate": str(it.get("rate", 10))} for it in args.get("items") or []]
    if args.get("revenue_account"):
        form["revenue_account"] = importer.resolve_account(conn, args["revenue_account"])["code"]
    calc = invoices.calculate(form)
    summary = (f"宛先: {form.get('partner')} {form.get('honorific', '御中')} / 発行日: {form.get('issue_date')}\n"
               + "\n".join(f"  {i['description']} {i['quantity']}{i['unit']} × {i['unit_price']} = {i['amount']:,}" for i in calc["items"])
               + f"\n小計 {calc['subtotal']:,} / 消費税 {calc['tax']:,} / 源泉徴収 {calc['withholding']:,} / ご請求額 {calc['total']:,}")
    if args.get("dry_run"):
        return "計算結果（まだ作成していません）:\n" + summary
    iid = invoices.save(conn, form, post=args.get("post_entry", True))
    inv = invoices.get(conn, iid)
    return (f"請求書 {inv['number']} を作成しました。\n{summary}\n"
            f"印刷・PDF保存: {WEB_URL}/invoice/{iid}/print（会計ソフトの画面を起動してから開いてください）")


def t_list_invoices(conn, args):
    rows = invoices.listing(conn, _year(conn, args.get("year")))
    return "\n".join(
        f"{r['number']} {r['issue_date']} {r['partner']} ご請求額 {r['total']:,} "
        f"{'取消' if r['cancelled'] else '入金済' if r['paid_entry_id'] else '未入金'}" for r in rows) or "請求書はありません"


def t_record_payment(conn, args):
    row = conn.execute("SELECT id FROM invoices WHERE number = ? AND cancelled = 0", (args.get("number"),)).fetchone()
    if not row:
        raise ledger.LedgerError("請求書が見つかりません")
    account = importer.resolve_account(conn, args["account"])["code"] if args.get("account") else None
    eid = invoices.record_payment(conn, row["id"], args.get("date"), args.get("received"), args.get("fee") or 0, account)
    return "入金を登録しました:\n" + _fmt_entry(ledger.get_entry(conn, eid))


def _read_file(path):
    path = os.path.expanduser(str(path or "").strip().strip('"'))
    if not os.path.isfile(path):
        raise ledger.LedgerError(f"ファイルが見つかりません: {path}")
    with open(path, "rb") as f:
        return path, f.read()


def t_read_invoice_pdf(conn, args):
    _, data = _read_file(args.get("path"))
    r = invoices.read_pdf(conn, data)
    text = r.pop("text")
    return json.dumps(r, ensure_ascii=False, indent=1) + "\n\n--- PDFの文字 ---\n" + text[:4000]


def t_post_invoice_document(conn, args):
    path, data = _read_file(args.get("path"))
    form = dict(args)
    for key in ("account", "credit_account"):
        if form.get(key):
            form[key] = importer.resolve_account(conn, form[key])["code"]
    form["qualified"] = "1" if args.get("qualified") else ""
    dup = invoices.find_document_by_hash(conn, data)
    if dup and not args.get("allow_duplicate"):
        raise ledger.LedgerError(f"このPDFは {dup['created_at']} に保存済みです（仕訳 ID {dup['entry_id']}）")
    eid = invoices.entry_from_document(conn, form)
    kind = "発行請求書" if args.get("direction") == "sales" else "受領請求書"
    invoices.add_document(conn, data, os.path.basename(path), kind, args.get("date"),
                          invoices._int(args.get("total")), args.get("partner") or "", eid)
    return "仕訳を登録し、PDFを証憑として保存しました:\n" + _fmt_entry(ledger.get_entry(conn, eid))


def t_attach_document(conn, args):
    path, data = _read_file(args.get("path"))
    eid = _find_entry(conn, args)
    item = ledger.get_entry(conn, eid)
    e = item["entry"]
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    amount = sum(l["amount"] for l in item["lines"] if l["side"] == "D")
    invoices.add_document(conn, data, os.path.basename(path), args.get("kind") or "領収書等", e["date"], amount,
                          e["partner"], eid, mime)
    return f"No.{e['entry_no']} に {os.path.basename(path)} を添付しました"


def t_set_opening_balances(conn, args):
    year = _year(conn, args.get("year"))
    amounts = {}
    for name, value in (args.get("balances") or {}).items():
        amounts[importer.resolve_account(conn, name)["code"]] = importer.parse_amount(value)
    parsed = {"errors": [], "opening": amounts, "entries": [], "filename": "claude", "sha256": ""}
    importer.commit(conn, parsed, year, auto_capital=args.get("auto_capital", True))
    amap = db.account_map(conn)
    return f"{year}年の期首残高を登録しました:\n" + "\n".join(
        f"  {amap[c]['name']}: {v:,}" for c, v in ledger.get_opening(conn, year).items())


def t_year_end(conn, args):
    year = _year(conn, args.get("year"))
    action = args.get("action")
    if action == "depreciation":
        eid = yearend.post_depreciation(conn, year)
    elif action == "home_office":
        acct = importer.resolve_account(conn, args.get("account"))
        eid = yearend.post_kaji(conn, year, acct["code"], args.get("business_pct"))
    elif action == "consumption_tax_accrual":
        eid = yearend.post_ctax_accrual(conn, year, args.get("amount") or ctax.compute(conn, year)["total"])
    else:
        raise ledger.LedgerError("action が不正です")
    return "登録しました:\n" + _fmt_entry(ledger.get_entry(conn, eid))


LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "side": {"type": "string", "enum": ["借方", "貸方"]},
        "account": {"type": "string", "description": "勘定科目名またはコード"},
        "amount": {"type": "integer", "description": "税込金額（円）"},
        "tax": {"type": "string", "description": "課税売上10% / 課税売上8% / 課税仕入10% / 課税仕入8% / 非課税 / 対象外。省略で科目の既定"},
        "invoice": {"type": "string", "description": "課税仕入のみ: あり / なし / 少額。省略で「あり」"},
        "memo": {"type": "string"},
    },
    "required": ["side", "account", "amount"],
}
ENTRY_SCHEMA = {
    "type": "object",
    "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}, "partner": {"type": "string"},
                   "description": {"type": "string"}, "lines": {"type": "array", "items": LINE_SCHEMA}},
    "required": ["date", "lines"],
}
REF = {"year": {"type": "integer"}, "entry_no": {"type": "integer", "description": "その年の仕訳番号"},
       "entry_id": {"type": "integer"}}
YEAR = {"year": {"type": "integer", "description": "省略時は画面で選んでいる年度"}}

TOOLS = [
    ("get_overview", t_get_overview, "設定・年度・売上と所得の概況・勘定科目一覧を取得する。最初に呼ぶ。", YEAR, []),
    ("search_entries", t_search_entries, "仕訳を検索する（日付・金額の範囲、取引先、勘定科目、摘要）。",
     {**YEAR, "all_years": {"type": "boolean"}, "date_from": {"type": "string"}, "date_to": {"type": "string"},
      "amount_min": {"type": "integer"}, "amount_max": {"type": "integer"}, "partner": {"type": "string"},
      "account": {"type": "string"}, "text": {"type": "string"}, "include_deleted": {"type": "boolean"},
      "limit": {"type": "integer"}}, []),
    ("post_entries", t_post_entries,
     "仕訳を1件以上登録する。全件を検証し、1件でもエラーなら何も登録しない。dry_run=true で検証だけ行う。",
     {"entries": {"type": "array", "items": ENTRY_SCHEMA}, "dry_run": {"type": "boolean"}}, ["entries"]),
    ("update_entry", t_update_entry, "仕訳を訂正する（理由必須・履歴が残る）。lines を渡すと行を丸ごと置き換える。",
     {**REF, "date": {"type": "string"}, "partner": {"type": "string"}, "description": {"type": "string"},
      "lines": {"type": "array", "items": LINE_SCHEMA}, "reason": {"type": "string"}}, ["reason"]),
    ("delete_entry", t_delete_entry, "仕訳を削除する（理由必須・履歴が残る）。", {**REF, "reason": {"type": "string"}}, ["reason"]),
    ("get_report", t_get_report, "帳票を取得する: trial_balance(試算表) / profit_loss(損益計算書) / balance_sheet(貸借対照表) / "
     "monthly(月別売上) / consumption_tax(消費税) / withholding(源泉徴収の集計) / depreciation(減価償却) / "
     "tax_forecast(所得税・住民税・個人事業税・消費税の年税額予測と納税スケジュール)。",
     {**YEAR, "kind": {"type": "string", "enum": ["trial_balance", "profit_loss", "balance_sheet", "monthly",
                                                  "consumption_tax", "withholding", "depreciation", "tax_forecast"]},
      "date_to": {"type": "string"}}, ["kind"]),
    ("general_ledger", t_general_ledger, "勘定科目ごとの総勘定元帳を取得する。", {**YEAR, "account": {"type": "string"}}, ["account"]),
    ("create_invoice", t_create_invoice,
     "請求書を作成し、売上の仕訳（源泉徴収込み）も登録する。発行者・振込先は保存済みの設定を使う。dry_run=true で計算だけ。",
     {"issue_date": {"type": "string"}, "partner": {"type": "string"}, "honorific": {"type": "string", "enum": ["御中", "様"]},
      "items": {"type": "array", "items": {"type": "object", "properties": {
          "date": {"type": "string", "description": "例 2026/10/2"}, "description": {"type": "string"},
          "quantity": {"type": "number"}, "unit": {"type": "string"}, "unit_price": {"type": "integer", "description": "税抜単価"},
          "rate": {"type": "integer", "enum": [10, 8, 0]}}, "required": ["description", "quantity", "unit_price"]}},
      "withholding": {"type": "boolean", "description": "源泉徴収するか（既定 true）"}, "due": {"type": "string"},
      "remarks": {"type": "string"}, "post_date": {"type": "string", "description": "売上の計上日（既定は発行日）"},
      "show_number": {"type": "boolean"}, "post_entry": {"type": "boolean"}, "revenue_account": {"type": "string"},
      "dry_run": {"type": "boolean"}}, ["issue_date", "partner", "items"]),
    ("list_invoices", t_list_invoices, "発行した請求書の一覧と入金状況。", YEAR, []),
    ("record_invoice_payment", t_record_payment, "請求書の入金を登録する（入金額＋振込手数料＝請求額）。",
     {"number": {"type": "string"}, "date": {"type": "string"}, "received": {"type": "integer"},
      "fee": {"type": "integer"}, "account": {"type": "string"}}, ["number", "date", "received"]),
    ("read_invoice_pdf", t_read_invoice_pdf, "パソコン上の請求書PDFを読み取り、日付・取引先・金額の推定値と本文を返す。",
     {"path": {"type": "string", "description": "PDFのフルパス"}}, ["path"]),
    ("post_invoice_document", t_post_invoice_document,
     "請求書PDFの内容で仕訳を登録し、PDFを証憑として保存する。sales=発行した請求書（売掛金・源泉は事業主貸）、"
     "expense=受け取った請求書（経費／未払金、源泉は預り金）。小計＋消費税−源泉徴収＝合計 であること。",
     {"path": {"type": "string"}, "direction": {"type": "string", "enum": ["sales", "expense"]},
      "date": {"type": "string"}, "partner": {"type": "string"}, "description": {"type": "string"},
      "subtotal": {"type": "integer"}, "tax": {"type": "integer"}, "rate": {"type": "integer", "enum": [10, 8, 0]},
      "withholding": {"type": "integer"}, "total": {"type": "integer"},
      "account": {"type": "string", "description": "売上なら売上高、経費なら経費科目"},
      "credit_account": {"type": "string", "description": "経費の貸方（既定 未払金。支払済みなら普通預金・事業主借）"},
      "qualified": {"type": "boolean", "description": "登録番号のある適格請求書か"},
      "allow_duplicate": {"type": "boolean"}},
     ["path", "direction", "date", "subtotal", "tax", "withholding", "total"]),
    ("attach_document", t_attach_document, "領収書などのファイル（PDF・画像）を既存の仕訳に証憑として添付する。",
     {**REF, "path": {"type": "string"}, "kind": {"type": "string"}}, ["path"]),
    ("set_opening_balances", t_set_opening_balances,
     "期首残高を登録する（その年の期首残高を置き換える）。前年の決算書4ページ目の期末残高を使う。元入金は既定で自動計算。",
     {**YEAR, "balances": {"type": "object", "additionalProperties": {"type": "integer"}, "description": "{科目名: 金額}"},
      "auto_capital": {"type": "boolean"}}, ["balances"]),
    ("year_end", t_year_end, "決算整理仕訳を作る: depreciation(減価償却) / home_office(家事按分: account と business_pct) / "
     "consumption_tax_accrual(消費税の未払計上)。",
     {**YEAR, "action": {"type": "string", "enum": ["depreciation", "home_office", "consumption_tax_accrual"]},
      "account": {"type": "string"}, "business_pct": {"type": "integer"}, "amount": {"type": "integer"}}, ["action"]),
]
HANDLERS = {name: fn for name, fn, *_ in TOOLS}
WRITE_TOOLS = {"post_entries", "update_entry", "delete_entry", "create_invoice", "record_invoice_payment",
               "post_invoice_document", "attach_document", "set_opening_balances", "year_end"}


def tool_list():
    result = []
    for name, _, desc, props, required in TOOLS:
        result.append({
            "name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required},
            "annotations": {"readOnlyHint": name not in WRITE_TOOLS,
                            "destructiveHint": name in ("delete_entry", "set_opening_balances")},
        })
    return result


def call_tool(conn, name, args):
    fn = HANDLERS.get(name)
    if not fn:
        return {"content": [{"type": "text", "text": f"不明なツール: {name}"}], "isError": True}
    try:
        text = fn(conn, args or {})
        return {"content": [{"type": "text", "text": text}]}
    except (ledger.LedgerError, importer.ImportError_, invoices.InvoiceError, ValueError, KeyError) as exc:
        return {"content": [{"type": "text", "text": f"エラー: {exc}"}], "isError": True}
    except Exception:  # noqa: BLE001 予期しないエラーも Claude に伝える
        return {"content": [{"type": "text", "text": "予期しないエラー:\n" + traceback.format_exc()}], "isError": True}


def handle(conn, msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "aoiro", "version": "0.2.0"}, "instructions": INSTRUCTIONS}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = call_tool(conn, params.get("name"), params.get("arguments"))
    elif method == "ping":
        result = {}
    elif mid is None:
        return None  # 通知には応答しない
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"未対応: {method}"}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _std_streams():
    """標準入出力。Windows のウィンドウ版 exe では sys.stdin が None なので OS のハンドルから開く。"""
    if sys.stdin is not None and sys.stdout is not None:
        return sys.stdin.buffer, sys.stdout.buffer
    import ctypes
    import msvcrt
    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    fin = msvcrt.open_osfhandle(kernel32.GetStdHandle(-10), os.O_RDONLY | os.O_BINARY)
    fout = msvcrt.open_osfhandle(kernel32.GetStdHandle(-11), os.O_WRONLY | os.O_BINARY)
    return os.fdopen(fin, "rb", buffering=0), os.fdopen(fout, "wb", buffering=0)


def serve(db_path, stdin=None, stdout=None):
    if stdin is None or stdout is None:
        stdin, stdout = _std_streams()
    from . import maintenance
    scheduler = maintenance.SummaryScheduler(db_path, delay=3.0)
    conn = db.connect(db_path)
    try:
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                resp = handle(conn, msg)
                if msg.get("method") == "tools/call" and (msg.get("params") or {}).get("name") in WRITE_TOOLS:
                    scheduler.touch()  # スマホ用サマリーを作り直す
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                stdout.flush()
    finally:
        conn.close()
        scheduler.flush()


def config_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "Claude", "claude_desktop_config.json")


def config_paths():
    """設定ファイルの候補（Microsoft Store 版の Claude は別の場所に保存する）"""
    paths = [config_path()]
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local and os.path.isdir(os.path.join(local, "Packages")):
        for name in os.listdir(os.path.join(local, "Packages")):
            if name.startswith("Claude_"):
                paths.append(os.path.join(local, "Packages", name, "LocalCache", "Roaming", "Claude",
                                          "claude_desktop_config.json"))
    return paths


def install_all(db_path):
    return [install(db_path, p) for p in config_paths()]


def install(db_path, path=None):
    """Claude Desktop の設定ファイルに aoiro を追加する（既存の設定は残す）。"""
    path = path or config_path()
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            config = json.loads(text)
        backup = path + f".bak-{datetime.datetime.now():%Y%m%d%H%M%S}"
        with open(backup, "w", encoding="utf-8") as f:
            f.write(text)
    if getattr(sys, "frozen", False):  # Windows アプリ版（aoiro.exe）
        entry = {"command": sys.executable, "args": ["--db", os.path.abspath(db_path), "mcp"],
                 "env": {"PYTHONUTF8": "1"}}
    else:
        entry = {"command": sys.executable, "args": ["-m", "aoiro", "--db", os.path.abspath(db_path), "mcp"],
                 "env": {"PYTHONPATH": package_dir, "PYTHONUTF8": "1"}}
    config.setdefault("mcpServers", {})["aoiro"] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return path
