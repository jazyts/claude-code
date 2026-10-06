"""ブラウザで使う画面（標準ライブラリの http.server のみ使用、ローカル専用）."""

import csv
import html
import io
import json
import re
import secrets
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ctax, db, ledger, reports, yearend

E = html.escape
LINE_ROWS = 8

CSS = """
:root{--fg:#1d2330;--muted:#667085;--line:#d0d5dd;--bg:#fff;--soft:#f4f6fa;--accent:#1f5fbf;--bad:#b42318;--ok:#067647}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;color:var(--fg);background:var(--bg);font-size:14px}
header{background:#123e7c;color:#fff;padding:8px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
header a{color:#fff;text-decoration:none;padding:4px 6px;border-radius:4px}header a:hover{background:#ffffff22}
header .brand{font-weight:700;margin-right:8px}header form{margin-left:auto}
main{padding:16px;max-width:1200px;margin:0 auto}
h1{font-size:20px;margin:4px 0 16px}h2{font-size:16px;margin:24px 0 8px}
table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid var(--line);padding:4px 6px;vertical-align:top}
th{background:var(--soft);font-weight:600;text-align:left}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.total td{font-weight:700;background:var(--soft)}tr.deleted td{color:var(--muted);text-decoration:line-through}
input,select,textarea,button{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:4px;background:#fff}
input[type=number]{text-align:right}button,.btn{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer;text-decoration:none;padding:5px 12px;border-radius:4px;display:inline-block}
button.danger{background:var(--bad);border-color:var(--bad)}button.sub,.btn.sub{background:#fff;color:var(--accent)}
.msg{padding:8px 12px;border-radius:4px;margin:8px 0}.err{background:#fef3f2;color:var(--bad);border:1px solid #fecdca}.ok{background:#ecfdf3;color:var(--ok);border:1px solid #abefc6}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{border:1px solid var(--line);border-radius:6px;padding:12px}.card .big{font-size:22px;font-weight:700}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:6px 0}.row label{display:flex;flex-direction:column;font-size:12px;color:var(--muted)}
.tpl button{margin:2px}.scroll{overflow-x:auto}
@media print{header,.noprint{display:none}main{padding:0}}
"""

NAV = [
    ("/", "ホーム"), ("/entry/new", "仕訳入力"), ("/entries", "仕訳検索"),
    ("/reports/journal", "仕訳帳"), ("/reports/ledger", "総勘定元帳"), ("/reports/trial", "試算表"),
    ("/reports/pl", "決算書"), ("/reports/ctax", "消費税"), ("/yearend", "決算整理"),
    ("/settings", "設定"),
]

# 簡単入力テンプレート: (ラベル, [(side, 科目名, 税区分 or None)], 摘要)
TEMPLATES = [
    ("売上を計上（請求書発行時）", [("D", "売掛金", None), ("C", "売上高", None)], "売上"),
    ("売上の入金", [("D", "普通預金", None), ("C", "売掛金", None)], "売掛金入金"),
    ("入金（振込手数料が引かれた）", [("D", "普通預金", None), ("D", "支払手数料", None), ("C", "売掛金", None)], "売掛金入金"),
    ("経費を事業用口座から支払", [("D", "消耗品費", None), ("C", "普通預金", None)], ""),
    ("経費を個人のお金で支払", [("D", "消耗品費", None), ("C", "事業主借", None)], ""),
    ("事業用口座から生活費へ", [("D", "事業主貸", None), ("C", "普通預金", None)], "生活費"),
    ("消費税・前年分の納付", [("D", "租税公課", None), ("C", "普通預金", None)], "消費税納付"),
]


def yen(v):
    return f"{v:,}" if v else ("0" if v == 0 else "")


def yen_blank(v):
    return f"{v:,}" if v else ""


class App:
    def __init__(self, db_path):
        self.db_path = db_path
        self.token = secrets.token_urlsafe(24)

    def conn(self):
        return db.connect(self.db_path)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aoiro"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------------- 基本処理
        def _allowed_host(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost", "::1", "[::1]")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            if not self._allowed_host():
                return self._send(HTTPStatus.FORBIDDEN, "forbidden", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            self.form = {}
            self.form_lists = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                            keep_blank_values=True)
                self.form_lists = raw
                self.form = {k: v[-1] for k, v in raw.items()}
                if self.form.get("_token") != app.token:
                    return self._send(HTTPStatus.FORBIDDEN, "不正なリクエストです", "text/plain")
            self.c = app.conn()
            try:
                self.year = int(self.query.get("y") or db.get_setting(self.c, "current_year"))
                for pattern, handler in ROUTES:
                    m = re.fullmatch(pattern, url.path)
                    if m:
                        name = f"{method.lower()}_{handler}"
                        fn = getattr(self, name, None)
                        if not fn:
                            return self._send(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", "text/plain")
                        return fn(*m.groups())
                self._send(HTTPStatus.NOT_FOUND, self.page("見つかりません", "<p>ページがありません。</p>"))
            except Exception as exc:  # noqa: BLE001 ローカル利用のため内容を表示する
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                           self.page("エラー", f'<div class="msg err">{E(repr(exc))}</div>'))
            finally:
                self.c.close()

        def _send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def ok(self, body):
            self._send(HTTPStatus.OK, body)

        def redirect(self, location):
            self._send(HTTPStatus.SEE_OTHER, "", headers={"Location": location})

        def hidden(self):
            return f'<input type="hidden" name="_token" value="{E(app.token)}">'

        def page(self, title, body, msg=None, err=None):
            name = db.get_setting(self.c, "business_name") or "青色申告 会計"
            nav = "".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
            year = getattr(self, "year", "")
            notes = ""
            if msg:
                notes += f'<div class="msg ok">{E(msg)}</div>'
            if err:
                notes += f'<div class="msg err">{E(err)}</div>'
            closed = " （締め済み）" if year in db.closed_years(self.c) else ""
            return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} - {E(name)}</title>
<style>{CSS}</style></head><body><header><span class="brand">{E(name)}</span>{nav}
<form method="post" action="/year">{self.hidden()}<input type="number" name="year" value="{year}" style="width:90px">
<button class="sub">年度切替</button></form></header>
<main><h1>{E(title)} <span class="muted" style="font-size:14px">{year}年分{closed}</span></h1>{notes}{body}</main></body></html>"""

        def account_options(self, selected="", blank=True):
            out = ['<option value=""></option>'] if blank else []
            current = None
            for a in db.accounts(self.c):
                if not a["active"] and a["code"] != selected:
                    continue
                if a["category"] != current:
                    if current:
                        out.append("</optgroup>")
                    current = a["category"]
                    out.append(f'<optgroup label="{db.CATEGORY_LABELS[current]}">')
                sel = " selected" if a["code"] == selected else ""
                out.append(f'<option value="{a["code"]}"{sel}>{E(a["code"])} {E(a["name"])}</option>')
            out.append("</optgroup>")
            return "".join(out)

        @staticmethod
        def options(labels, selected):
            return "".join(
                f'<option value="{k}"{" selected" if k == selected else ""}>{E(v)}</option>'
                for k, v in labels.items()
            )

        # ---------------------------------------------------------- ホーム
        def get_home(self):
            pl = reports.profit_loss(self.c, self.year)
            tax = ctax.compute(self.c, self.year)
            bs = reports.balance_sheet(self.c, self.year)
            diff = ledger.opening_balanced(self.c, self.year)
            warn = ""
            if diff:
                warn += f'<div class="msg err">期首残高の貸借が {diff:,} 円ずれています。<a href="/opening">期首残高</a>を確認してください。</div>'
            if not bs["balanced"]:
                warn += '<div class="msg err">貸借対照表が一致していません。</div>'
            m = reports.monthly(self.c, self.year)
            months = "".join(f'<td class="n">{yen_blank(v)}</td>' for v in m["sales"])
            body = f"""{warn}<div class="grid">
<div class="card"><div class="muted">売上（収入）金額</div><div class="big">{pl['sales']:,} 円</div></div>
<div class="card"><div class="muted">経費（売上原価含む）</div><div class="big">{pl['cost'] + pl['expense_total']:,} 円</div></div>
<div class="card"><div class="muted">青色申告特別控除前の所得</div><div class="big">{pl['pre_income']:,} 円</div></div>
<div class="card"><div class="muted">消費税（{ctax.METHOD_LABELS[tax['method']]}）概算</div><div class="big">{tax['total']:,} 円</div></div>
</div>
<h2>月別売上</h2><div class="scroll"><table><tr>{''.join(f'<th class="n">{i}月</th>' for i in range(1, 13))}</tr><tr>{months}</tr></table></div>
<h2>メニュー</h2><ul>
<li><a href="/entry/new">仕訳入力</a>（簡単入力テンプレートあり） / <a href="/entries">仕訳検索・訂正</a></li>
<li>帳簿: <a href="/reports/journal">仕訳帳</a> / <a href="/reports/ledger">総勘定元帳</a> / <a href="/reports/trial">試算表</a></li>
<li>決算: <a href="/reports/pl">損益計算書</a> / <a href="/reports/bs">貸借対照表</a> / <a href="/reports/monthly">月別売上・仕入</a> / <a href="/reports/depr">減価償却費の計算</a> / <a href="/reports/ctax">消費税</a></li>
<li>準備・整理: <a href="/opening">期首残高</a> / <a href="/assets">固定資産</a> / <a href="/yearend">決算整理・年次繰越</a></li>
<li>管理: <a href="/accounts">勘定科目</a> / <a href="/settings">設定</a> / <a href="/verify">データ検証</a> / <a href="/audit">変更ログ</a> / CSV出力（<a href="/export/journal.csv?y={self.year}">仕訳帳</a>・<a href="/export/history.csv">訂正削除履歴</a>）</li>
</ul>"""
            self.ok(self.page("ホーム", body))

        def post_year(self):
            try:
                y = int(self.form.get("year"))
                if not 1990 <= y <= 2100:
                    raise ValueError
                db.set_setting(self.c, "current_year", y)
            except (TypeError, ValueError):
                pass
            self.redirect(self.headers.get("Referer") and urllib.parse.urlsplit(self.headers["Referer"]).path or "/")

        # ---------------------------------------------------------- 仕訳入力
        def entry_form(self, action, values, err=None, edit=False, title="仕訳入力"):
            amap = db.account_map(self.c)
            rows = []
            lines = list(values.get("lines", []))
            while len(lines) < LINE_ROWS:
                lines.append({})
            for i, l in enumerate(lines):
                side = l.get("side", "D" if i == 0 else "C" if i == 1 else "")
                rows.append(f"""<tr>
<td><select name="side">{self.options({'D': '借方', 'C': '貸方'}, side)}</select></td>
<td><select name="account" class="acct">{self.account_options(l.get('account', ''))}</select></td>
<td><input type="number" name="amount" class="amt" min="1" step="1" value="{E(str(l.get('amount', '')))}" style="width:130px"></td>
<td><select name="tax"><option value="">（科目の既定）</option>{self.options(ledger.TAX_LABELS, l.get('tax', ''))}</select></td>
<td><select name="invoice">{self.options(ledger.INVOICE_LABELS, l.get('invoice', 'Q'))}</select></td>
<td><input name="memo" value="{E(l.get('memo', ''))}"></td></tr>""")
            name_to_code = {a["name"]: a["code"] for a in amap.values()}
            tpl = ""
            if not edit:
                buttons = []
                for i, (label, tlines, desc) in enumerate(TEMPLATES):
                    spec = [[s, name_to_code.get(n, "")] for s, n, _ in tlines]
                    buttons.append(f'<button type="button" class="sub" '
                                   f'onclick="tpl({E(json.dumps(spec))}, {E(json.dumps(desc))})">{E(label)}</button>')
                tpl = f'<div class="tpl noprint"><span class="muted">簡単入力: </span>{"".join(buttons)}</div>'
            reason = ""
            if edit:
                reason = f'<div class="row"><label>訂正理由（必須・履歴に残ります）<input name="reason" size="60" value="{E(values.get("reason", ""))}"></label></div>'
            body = f"""{tpl}
<form method="post" action="{action}">{self.hidden()}
<div class="row"><label>取引日<input type="date" name="date" required value="{E(values.get('date', ''))}"></label>
<label>取引先<input name="partner" value="{E(values.get('partner', ''))}" list="partners"></label>
<label>摘要<input name="description" size="40" value="{E(values.get('description', ''))}"></label></div>
<datalist id="partners">{''.join(f'<option value="{E(r["partner"])}">' for r in self.c.execute("SELECT DISTINCT partner FROM entries WHERE partner != '' ORDER BY partner"))}</datalist>
<div class="scroll"><table><tr><th>借/貸</th><th>勘定科目</th><th class="n">金額（税込）</th><th>税区分</th><th>インボイス</th><th>行メモ</th></tr>
{''.join(rows)}
<tr class="total"><td colspan="6">借方合計 <span id="td">0</span> 円 ／ 貸方合計 <span id="tc">0</span> 円 <span id="tdiff"></span></td></tr></table></div>
{reason}<button>{'訂正して保存' if edit else '登録'}</button>
</form>
<p class="muted">税込経理です。金額は消費税込みで入力してください。インボイス欄は課税仕入の行だけ使います。</p>
<script>
function recalc(){{let d=0,c=0;document.querySelectorAll('tbody tr, table tr').forEach(tr=>{{const s=tr.querySelector('[name=side]'),a=tr.querySelector('[name=amount]');if(!s||!a)return;const v=parseInt(a.value||0);if(s.value==='D')d+=v;else c+=v;}});
document.getElementById('td').textContent=d.toLocaleString();document.getElementById('tc').textContent=c.toLocaleString();
document.getElementById('tdiff').textContent=d===c?'':'（差額 '+(d-c).toLocaleString()+' 円）';}}
document.addEventListener('input',recalc);document.addEventListener('change',recalc);recalc();
function tpl(spec,desc){{const rows=[...document.querySelectorAll('table tr')].filter(tr=>tr.querySelector('[name=side]'));
rows.forEach((tr,i)=>{{const s=spec[i];tr.querySelector('[name=side]').value=s?s[0]:(i===0?'D':'C');tr.querySelector('[name=account]').value=s?s[1]:'';
tr.querySelector('[name=amount]').value='';tr.querySelector('[name=tax]').value='';tr.querySelector('[name=invoice]').value='Q';tr.querySelector('[name=memo]').value='';}});
const d=document.querySelector('[name=description]');if(desc&&!d.value)d.value=desc;rows[0].querySelector('[name=amount]').focus();recalc();}}
// 2行の仕訳は1行目の金額を2行目にも反映
document.addEventListener('input',e=>{{if(e.target.name!=='amount')return;const rows=[...document.querySelectorAll('[name=amount]')];
const filled=[...document.querySelectorAll('[name=account]')].filter(x=>x.value).length;if(filled===2&&e.target===rows[0]){{rows[1].value=e.target.value;recalc();}}}});
</script>"""
            return self.page(title, body, err=err)

        def _form_values(self):
            fl = self.form_lists
            keys = ("side", "account", "amount", "tax", "invoice", "memo")
            cols = [fl.get(k, []) for k in keys]
            n = max(len(c) for c in cols) if cols else 0
            lines = []
            for i in range(n):
                lines.append({k: (cols[j][i] if i < len(cols[j]) else "") for j, k in enumerate(keys)})
            return {
                "date": self.form.get("date", ""),
                "partner": self.form.get("partner", ""),
                "description": self.form.get("description", ""),
                "reason": self.form.get("reason", ""),
                "lines": lines,
            }

        def get_entry_new(self):
            self.ok(self.entry_form("/entry/new", {"date": self.query.get("date", "")}))

        def post_entry_new(self):
            v = self._form_values()
            try:
                eid = ledger.post_entry(self.c, v["date"], v["lines"], v["partner"], v["description"])
            except ledger.LedgerError as exc:
                return self.ok(self.entry_form("/entry/new", v, err=str(exc)))
            self.redirect(f"/entry/{eid}?created=1")

        def get_entry(self, eid):
            item = ledger.get_entry(self.c, int(eid))
            if not item:
                return self._send(HTTPStatus.NOT_FOUND, self.page("仕訳", "<p>見つかりません</p>"))
            e = item["entry"]
            self.year = e["year"]
            body = self.entry_table([item], show_link=False)
            body += f'<p class="muted">版数 {e["version"]} ／ 登録 {E(e["created_at"])} ／ 最終更新 {E(e["updated_at"])}</p>'
            if not e["deleted"]:
                body += f"""<div class="row noprint"><a class="btn" href="/entry/{e['id']}/edit">訂正</a>
<a class="btn sub" href="/entry/new?date={e['date']}">続けて入力</a></div>
<form method="post" action="/entry/{e['id']}/delete" class="row noprint">{self.hidden()}
<label>削除理由（必須）<input name="reason" size="40" required></label><button class="danger">削除</button></form>"""
            body += "<h2>訂正・削除履歴</h2>" + self.history_table(int(eid))
            msg = "登録しました" if self.query.get("created") else ("保存しました" if self.query.get("saved") else None)
            self.ok(self.page(f"仕訳 No.{e['entry_no']}{'（削除済み）' if e['deleted'] else ''}", body, msg=msg))

        def history_table(self, eid):
            amap = db.account_map(self.c)
            op_label = {"create": "登録", "update": "訂正", "delete": "削除"}
            rows = []
            for h in ledger.history(self.c, eid):
                s = h["snapshot"]
                lines = "<br>".join(
                    f"{'借' if l['side'] == 'D' else '貸'} {E(amap[l['account']]['name'] if l['account'] in amap else l['account'])} "
                    f"{l['amount']:,} {E(ledger.TAX_LABELS.get(l['tax'], l['tax']))}"
                    for l in s["lines"])
                rows.append(f"<tr><td>{h['version']}</td><td>{op_label[h['op']]}</td><td>{E(h['recorded_at'])}</td>"
                            f"<td>{E(h['reason'])}</td><td>{E(s['date'])} {E(s['partner'])} {E(s['description'])}<br>{lines}</td></tr>")
            return ("<table><tr><th>版</th><th>操作</th><th>記録日時</th><th>理由</th><th>その時点の内容</th></tr>"
                    + "".join(rows) + "</table>")

        def get_entry_edit(self, eid):
            item = ledger.get_entry(self.c, int(eid))
            if not item or item["entry"]["deleted"]:
                return self.redirect(f"/entry/{eid}")
            e = item["entry"]
            self.year = e["year"]
            values = {
                "date": e["date"], "partner": e["partner"], "description": e["description"],
                "lines": [{"side": l["side"], "account": l["account_code"], "amount": l["amount"],
                           "tax": l["tax"], "invoice": l["invoice"], "memo": l["memo"]} for l in item["lines"]],
            }
            self.ok(self.entry_form(f"/entry/{eid}/edit", values, edit=True, title=f"仕訳の訂正 No.{e['entry_no']}"))

        def post_entry_edit(self, eid):
            v = self._form_values()
            try:
                ledger.update_entry(self.c, int(eid), v["date"], v["lines"], v["partner"], v["description"], v["reason"])
            except ledger.LedgerError as exc:
                return self.ok(self.entry_form(f"/entry/{eid}/edit", v, err=str(exc), edit=True, title="仕訳の訂正"))
            self.redirect(f"/entry/{eid}?saved=1")

        def post_entry_delete(self, eid):
            try:
                ledger.delete_entry(self.c, int(eid), self.form.get("reason"))
            except ledger.LedgerError as exc:
                return self.ok(self.page("削除できません", f'<p><a href="/entry/{eid}">戻る</a></p>', err=str(exc)))
            self.redirect(f"/entry/{eid}")

        def entry_table(self, items, show_link=True):
            out = ['<div class="scroll"><table><tr><th>No.</th><th>日付</th><th>取引先・摘要</th><th>借方科目</th>'
                   '<th class="n">借方金額</th><th>貸方科目</th><th class="n">貸方金額</th><th>税区分</th></tr>']
            for item in items:
                e = item["entry"]
                debit = [l for l in item["lines"] if l["side"] == "D"]
                credit = [l for l in item["lines"] if l["side"] == "C"]
                n = max(len(debit), len(credit))
                cls = ' class="deleted"' if e["deleted"] else ""
                for i in range(n):
                    d = debit[i] if i < len(debit) else None
                    c = credit[i] if i < len(credit) else None
                    taxes = " / ".join(
                        ledger.TAX_LABELS[l["tax"]] + ("" if l["invoice"] in ("Q",) or not l["tax"].startswith("P")
                                                        else f"（{ledger.INVOICE_LABELS[l['invoice']]}）")
                        for l in (d, c) if l and l["tax"] != "NA")
                    head = ""
                    if i == 0:
                        no = f'<a href="/entry/{e["id"]}">{e["entry_no"]}</a>' if show_link else str(e["entry_no"])
                        desc = " ".join(x for x in (e["partner"], e["description"]) if x)
                        head = (f'<td rowspan="{n}">{no}</td><td rowspan="{n}">{E(e["date"])}</td>'
                                f'<td rowspan="{n}">{E(desc)}</td>')
                    memo = lambda l: f'<br><span class="muted">{E(l["memo"])}</span>' if l and l["memo"] else ""
                    out.append(
                        f"<tr{cls}>{head}<td>{E(d['account_name']) if d else ''}{memo(d)}</td>"
                        f"<td class=\"n\">{yen_blank(d['amount']) if d else ''}</td>"
                        f"<td>{E(c['account_name']) if c else ''}{memo(c)}</td>"
                        f"<td class=\"n\">{yen_blank(c['amount']) if c else ''}</td><td>{E(taxes)}</td></tr>")
            out.append("</table></div>")
            return "".join(out)

        # ---------------------------------------------------------- 検索
        def get_entries(self):
            q = self.query
            try:
                items = ledger.search(
                    self.c, year=None if q.get("all") else self.year,
                    date_from=q.get("from"), date_to=q.get("to"),
                    amount_min=q.get("min"), amount_max=q.get("max"),
                    partner=q.get("partner"), account=q.get("account"), text=q.get("text"),
                    include_deleted=bool(q.get("deleted")))
                err = None
            except (ledger.LedgerError, ValueError) as exc:
                items, err = [], str(exc)
            chk = lambda k: " checked" if q.get(k) else ""
            form = f"""<form method="get" class="row noprint">
<label>日付（から）<input type="date" name="from" value="{E(q.get('from', ''))}"></label>
<label>日付（まで）<input type="date" name="to" value="{E(q.get('to', ''))}"></label>
<label>金額（以上）<input type="number" name="min" value="{E(q.get('min', ''))}" style="width:110px"></label>
<label>金額（以下）<input type="number" name="max" value="{E(q.get('max', ''))}" style="width:110px"></label>
<label>取引先<input name="partner" value="{E(q.get('partner', ''))}"></label>
<label>勘定科目<select name="account">{self.account_options(q.get('account', ''))}</select></label>
<label>摘要・メモ<input name="text" value="{E(q.get('text', ''))}"></label>
<label><span><input type="checkbox" name="all" value="1"{chk('all')}> 全年度</span>
<span><input type="checkbox" name="deleted" value="1"{chk('deleted')}> 削除済みも表示</span></label>
<button>検索</button></form><p class="muted">{len(items)} 件</p>"""
            self.ok(self.page("仕訳検索", form + self.entry_table(items), err=err))

        # ---------------------------------------------------------- 帳簿
        def get_journal(self):
            items = reports.journal(self.c, self.year)
            body = (f'<p class="noprint"><a href="/export/journal.csv?y={self.year}">CSVで出力</a></p>'
                    + self.entry_table(items))
            self.ok(self.page("仕訳帳", body))

        def get_ledger(self):
            code = self.query.get("code")
            accounts = db.account_map(self.c)
            used = {r["account_code"] for r in self.c.execute(
                "SELECT DISTINCT account_code FROM lines l JOIN entries e ON e.id=l.entry_id WHERE e.year=? AND e.deleted=0",
                (self.year,))} | set(ledger.get_opening(self.c, self.year))
            links = " ".join(f'<a href="/reports/ledger?code={c}">{E(accounts[c]["name"])}</a>' for c in sorted(used))
            body = f'<p class="noprint">{links or "仕訳がありません"}</p>'
            if self.query.get("all"):
                codes = sorted(used)
            elif code in accounts:
                codes = [code]
            else:
                codes = []
                body += '<p class="noprint"><a href="/reports/ledger?all=1">全科目を表示（印刷用）</a></p>'
            for c in codes:
                acct, rows = reports.general_ledger(self.c, self.year, c)
                trs = "".join(
                    f"<tr><td>{E(r['date'])}</td><td>{self.entry_link(r)}</td>"
                    f"<td>{E(r['counter'])}</td><td>{E(r['description'])}</td><td class=\"n\">{yen_blank(r['debit'])}</td>"
                    f"<td class=\"n\">{yen_blank(r['credit'])}</td><td class=\"n\">{yen(r['balance'])}</td></tr>"
                    for r in rows)
                d = sum(r["debit"] for r in rows)
                cr = sum(r["credit"] for r in rows)
                body += (f"<h2>{E(acct['code'])} {E(acct['name'])}</h2><table><tr><th>日付</th><th>No.</th><th>相手科目</th>"
                         f"<th>摘要</th><th class=\"n\">借方</th><th class=\"n\">貸方</th><th class=\"n\">残高</th></tr>{trs}"
                         f"<tr class=\"total\"><td colspan=\"4\">合計</td><td class=\"n\">{d:,}</td><td class=\"n\">{cr:,}</td>"
                         f"<td class=\"n\">{rows[-1]['balance']:,}</td></tr></table>")
            self.ok(self.page("総勘定元帳", body))

        @staticmethod
        def entry_link(r):
            return f'<a href="/entry/{r["entry_id"]}">{r["entry_no"]}</a>' if r["entry_id"] else ""

        def get_trial(self):
            rows, td, tc = reports.trial_balance(self.c, self.year, self.query.get("to") or None)
            trs = "".join(
                f"<tr><td>{E(b['account']['code'])}</td><td><a href=\"/reports/ledger?code={b['account']['code']}\">{E(b['account']['name'])}</a></td>"
                f"<td>{db.CATEGORY_LABELS[b['account']['category']]}</td><td class=\"n\">{yen(b['opening'])}</td>"
                f"<td class=\"n\">{yen(b['debit'])}</td><td class=\"n\">{yen(b['credit'])}</td><td class=\"n\">{yen(b['closing'])}</td></tr>"
                for b in rows)
            body = f"""<form class="row noprint"><label>この日まで<input type="date" name="to" value="{E(self.query.get('to', ''))}"></label><button>表示</button></form>
<table><tr><th>コード</th><th>科目</th><th>区分</th><th class="n">期首残高</th><th class="n">借方</th><th class="n">貸方</th><th class="n">残高</th></tr>{trs}
<tr class="total"><td colspan="4">合計</td><td class="n">{td:,}</td><td class="n">{tc:,}</td><td></td></tr></table>
<p class="muted">残高は各科目の通常の側（資産・費用は借方、負債・資本・収益は貸方）をプラスで表示。</p>"""
            self.ok(self.page("合計残高試算表", body, err=None if td == tc else "借方と貸方の合計が一致しません"))

        def get_pl(self):
            pl = reports.profit_loss(self.c, self.year)
            r = lambda no, name, v, cls="": f'<tr class="{cls}"><td>{no}</td><td>{E(name)}</td><td class="n">{yen(v)}</td></tr>'
            sales_note = "、".join(f"{E(n)} {v:,}" for n, v in pl["sales_breakdown"])
            body = f"""<p class="noprint"><a href="/reports/pl">損益計算書</a> | <a href="/reports/bs">貸借対照表</a> | <a href="/reports/monthly">月別売上・仕入</a> | <a href="/reports/depr">減価償却費の計算</a></p>
<p class="muted">青色申告決算書（一般用）1ページ目の番号に合わせています。様式は年分により変わることがあるため、転記時に確認してください。</p>
<table style="max-width:560px"><tr><th>番号</th><th>科目</th><th class="n">金額（円）</th></tr>
{r('①', '売上（収入）金額', pl['sales'])}
<tr><td></td><td colspan="2" class="muted">{sales_note}</td></tr>
{r('②', '期首商品（製品）棚卸高', pl['begin_inventory'])}{r('③', '仕入金額（製品製造原価）', pl['purchases'])}
{r('④', '小計（②＋③）', pl['subtotal'])}{r('⑤', '期末商品（製品）棚卸高', pl['end_inventory'])}
{r('⑥', '差引原価（④−⑤）', pl['cost'])}{r('⑦', '差引金額（①−⑥）', pl['gross'], 'total')}
{''.join(r(e['no'], e['name'], e['amount']) for e in pl['expenses'])}
{r('㉜', '経費計', pl['expense_total'], 'total')}{r('㉝', '差引金額（⑦−㉜）', pl['pre_income'], 'total')}
{r('', '青色申告特別控除前の所得金額', pl['pre_income'])}{r('', '青色申告特別控除額', pl['blue_deduction'])}
{r('', '所得金額', pl['income'], 'total')}</table>
<p class="muted">各種引当金・準備金、専従者給与は扱っていません（その場合は㉝と控除前所得が異なります）。</p>"""
            self.ok(self.page("損益計算書", body, err="／".join(pl["warnings"]) or None))

        def get_bs(self):
            bs = reports.balance_sheet(self.c, self.year)
            side = lambda rows: "".join(
                f"<tr><td>{E(x['name'])}</td><td class=\"n\">{yen_blank(x['opening'])}</td><td class=\"n\">{yen(x['closing'])}</td></tr>"
                for x in rows)
            body = f"""<p class="muted">青色申告決算書4ページ目「貸借対照表（資産負債調）」の形式（{self.year}年12月31日現在）。</p>
<div class="grid" style="grid-template-columns:1fr 1fr"><div><h2>資産の部</h2><table><tr><th>科目</th><th class="n">1月1日（期首）</th><th class="n">12月31日（期末）</th></tr>
{side(bs['assets'])}<tr class="total"><td>合計</td><td class="n">{bs['total_assets'][0]:,}</td><td class="n">{bs['total_assets'][1]:,}</td></tr></table></div>
<div><h2>負債・資本の部</h2><table><tr><th>科目</th><th class="n">1月1日（期首）</th><th class="n">12月31日（期末）</th></tr>
{side(bs['liabilities'])}<tr class="total"><td>合計</td><td class="n">{bs['total_liabilities'][0]:,}</td><td class="n">{bs['total_liabilities'][1]:,}</td></tr></table></div></div>"""
            self.ok(self.page("貸借対照表", body, err=None if bs["balanced"] else "資産と負債・資本の合計が一致しません（期首残高を確認してください）"))

        def get_monthly(self):
            m = reports.monthly(self.c, self.year)
            trs = "".join(f"<tr><td>{i + 1}月</td><td class=\"n\">{yen(m['sales'][i])}</td><td class=\"n\">{yen(m['purchases'][i])}</td></tr>"
                          for i in range(12))
            body = f"""<p class="muted">青色申告決算書2ページ目「月別売上（収入）金額及び仕入金額」。</p>
<table style="max-width:520px"><tr><th>月</th><th class="n">売上（収入）金額</th><th class="n">仕入金額</th></tr>{trs}
<tr><td>雑収入</td><td class="n">{yen(m['misc_income'])}</td><td></td></tr>
<tr class="total"><td>計</td><td class="n">{m['total_sales']:,}</td><td class="n">{m['total_purchases']:,}</td></tr></table>"""
            self.ok(self.page("月別売上・仕入金額", body))

        def get_depr(self):
            rows = yearend.depreciation_schedule(self.c, self.year)
            trs = "".join(
                f"<tr><td>{E(r['asset']['name'])}</td><td>{E(r['asset']['acquired'][:7])}</td><td class=\"n\">{r['asset']['cost']:,}</td>"
                f"<td>定額法</td><td class=\"n\">{r['asset']['life']}</td><td class=\"n\">0.{r['rate']:03d}</td><td class=\"n\">{r['months']}/12</td>"
                f"<td class=\"n\">{r['opening_book']:,}</td><td class=\"n\">{r['depreciation']:,}</td><td class=\"n\">{r['asset']['business_ratio']}%</td>"
                f"<td class=\"n\">{r['business']:,}</td><td class=\"n\">{r['closing_book']:,}</td></tr>" for r in rows)
            tot = sum(r["business"] for r in rows)
            body = f"""<p class="muted">青色申告決算書3ページ目「減価償却費の計算」。資産の登録は<a href="/assets">固定資産</a>から。</p>
<div class="scroll"><table><tr><th>資産名</th><th>取得年月</th><th class="n">取得価額</th><th>償却方法</th><th class="n">耐用年数</th><th class="n">償却率</th>
<th class="n">本年中の償却期間</th><th class="n">期首未償却残高</th><th class="n">本年分の償却費</th><th class="n">事業専用割合</th><th class="n">必要経費算入額</th><th class="n">期末未償却残高</th></tr>
{trs}<tr class="total"><td colspan="10">計</td><td class="n">{tot:,}</td><td></td></tr></table></div>"""
            self.ok(self.page("減価償却費の計算", body))

        def get_ctax(self):
            t = ctax.compute(self.c, self.year)
            prs = "".join(
                f"<tr><td>{ledger.TAX_LABELS[p['tax']]}</td><td class=\"n\">{p['ratio']}%</td><td class=\"n\">{p['amount']:,}</td><td class=\"n\">{p['deduction']:,}</td></tr>"
                for p in t["purchases"])
            method = ctax.METHOD_LABELS[t["method"]]
            if t["method"] == "simplified":
                method += f"（{ctax.SIMPLIFIED_LABELS[t['category']]}・みなし仕入率 {ctax.SIMPLIFIED_RATES[t['category']]}%）"
            body = f"""<p>計算方法: <b>{E(method)}</b>（<a href="/settings">設定で変更</a>）</p>
<table style="max-width:640px">
<tr><th colspan="2">売上</th></tr>
<tr><td>課税売上（10%）税込</td><td class="n">{t['sales10']:,}</td></tr>
<tr><td>課税標準額（10%）千円未満切捨て</td><td class="n">{t['base10']:,}</td></tr>
<tr><td>消費税額（7.8%）</td><td class="n">{t['tax10']:,}</td></tr>
<tr><td>課税売上（8%軽減）税込</td><td class="n">{t['sales8']:,}</td></tr>
<tr><td>課税標準額（8%）千円未満切捨て</td><td class="n">{t['base8']:,}</td></tr>
<tr><td>消費税額（6.24%）</td><td class="n">{t['tax8']:,}</td></tr>
<tr class="total"><td>売上に係る消費税額</td><td class="n">{t['sales_tax']:,}</td></tr>
<tr><td>控除対象仕入税額</td><td class="n">{t['deduction']:,}</td></tr>
<tr><td>差引税額（国税・百円未満切捨て）</td><td class="n">{t['national']:,}</td></tr>
<tr><td>地方消費税（譲渡割額）</td><td class="n">{t['local']:,}</td></tr>
<tr class="total"><td>納付税額 合計（概算）</td><td class="n">{t['total']:,}</td></tr></table>
<h2>課税仕入（一般課税の場合の控除計算・割戻し）</h2>
<table style="max-width:640px"><tr><th>区分</th><th class="n">控除割合</th><th class="n">税込金額</th><th class="n">仕入税額</th></tr>{prs}
<tr class="total"><td colspan="3">合計</td><td class="n">{t['general_deduction']:,}</td></tr></table>
<p class="muted">あくまで概算です。返還・貸倒れ・中間納付・積上げ計算等は考慮していません。経過措置の割合（80%→50%等）は税制改正で変わる場合があります。
税込経理では、この納付額を「租税公課」として経費にします（年末に未払計上するか、翌年の納付時に計上）。<a href="/yearend">決算整理</a>から未払計上できます。</p>"""
            self.ok(self.page("消費税（概算）", body))

        # ---------------------------------------------------------- 期首残高
        def get_opening(self, err=None, msg=None, values=None):
            opening = values if values is not None else ledger.get_opening(self.c, self.year)
            rows = "".join(
                f"<tr><td>{E(a['code'])}</td><td>{E(a['name'])}</td><td>{db.CATEGORY_LABELS[a['category']]}</td>"
                f"<td><input type=\"number\" name=\"a_{a['code']}\" value=\"{E(str(opening.get(a['code'], '') or ''))}\" style=\"width:150px\"></td></tr>"
                for a in db.accounts(self.c) if a["category"] in ("asset", "liability", "equity")
                and a["name"] not in ("事業主貸", "事業主借"))
            diff = ledger.opening_balanced(self.c, self.year)
            body = f"""<p>{self.year}年1月1日時点の残高を入力します。前年をこのソフトで締めた場合は自動で入ります。<br>
<span class="muted">元入金 ＝ 資産合計 − 負債合計 になるように入力してください（事業主貸・事業主借は期首は0）。</span></p>
<form method="post">{self.hidden()}<table style="max-width:600px"><tr><th>コード</th><th>科目</th><th>区分</th><th>期首残高</th></tr>{rows}</table>
<label><input type="checkbox" name="auto_capital" value="1" checked> 元入金を自動計算する（資産 − 負債）</label><br><br><button>保存</button></form>
<p>現在の貸借差額: <b>{diff:,}</b> 円</p>"""
            self.ok(self.page("期首残高", body, err=err, msg=msg))

        def post_opening(self):
            amounts = {k[2:]: v for k, v in self.form.items() if k.startswith("a_")}
            try:
                if self.form.get("auto_capital"):
                    amap = db.account_map(self.c)
                    capital = yearend.code_of(self.c, "元入金")
                    total = 0
                    for code, v in amounts.items():
                        if code == capital or not v:
                            continue
                        v = int(str(v).replace(",", ""))
                        cat = amap[code]["category"]
                        total += v if cat == "asset" else -v
                    amounts[capital] = total
                ledger.set_opening(self.c, self.year, amounts)
            except (ledger.LedgerError, ValueError) as exc:
                return self.get_opening(err=str(exc))
            self.get_opening(msg="保存しました")

        # ---------------------------------------------------------- 固定資産
        def get_assets(self, err=None):
            edit_id = self.query.get("id")
            current = None
            if edit_id:
                current = self.c.execute("SELECT * FROM fixed_assets WHERE id=?", (int(edit_id),)).fetchone()
            cur = dict(current) if current else {"business_ratio": 100}
            v = lambda k: E(str(cur.get(k) if cur.get(k) is not None else ""))
            asset_accounts = "".join(
                f'<option value="{a["code"]}"{" selected" if a["code"] == cur.get("account_code") else ""}>{E(a["name"])}</option>'
                for a in db.accounts(self.c, True) if a["category"] == "asset")
            rows = "".join(
                f"<tr><td><a href=\"/assets?id={a['id']}\">{E(a['name'])}</a></td><td>{E(a['account_name'])}</td><td>{E(a['acquired'])}</td>"
                f"<td class=\"n\">{a['cost']:,}</td><td class=\"n\">{a['life']}年</td><td class=\"n\">{a['business_ratio']}%</td><td>{E(a['disposed'] or '')}</td></tr>"
                for a in yearend.fixed_assets(self.c))
            body = f"""<p class="muted">10万円以上の資産（青色申告者は30万円未満なら一括で経費にできる特例もあります）。償却方法は個人の法定償却方法である定額法です。</p>
<table><tr><th>資産名</th><th>科目</th><th>取得日</th><th class="n">取得価額</th><th class="n">耐用年数</th><th class="n">事業割合</th><th>除却日</th></tr>{rows}</table>
<h2>{'資産の修正' if current else '資産の登録'}</h2>
<form method="post" action="/assets{('/' + str(current['id'])) if current else ''}">{self.hidden()}
<div class="row"><label>資産名<input name="name" value="{v('name')}" required></label>
<label>勘定科目<select name="account_code">{asset_accounts}</select></label>
<label>取得（事業供用）日<input type="date" name="acquired" value="{v('acquired')}" required></label>
<label>取得価額（税込）<input type="number" name="cost" value="{v('cost')}" required></label>
<label>耐用年数<input type="number" name="life" value="{v('life')}" required style="width:80px"></label>
<label>事業割合(%)<input type="number" name="business_ratio" value="{v('business_ratio')}" style="width:80px"></label>
<label>除却・売却日<input type="date" name="disposed" value="{v('disposed')}"></label></div>
<div class="row"><label>（導入前から持っている資産）基準年<input type="number" name="base_year" value="{v('base_year')}" style="width:90px"></label>
<label>基準年1月1日の未償却残高<input type="number" name="base_book" value="{v('base_book')}"></label></div>
<button>保存</button></form>
<p class="muted">資産の購入時は「工具器具備品 / 普通預金」などで仕訳し、年末に<a href="/yearend">決算整理</a>で減価償却仕訳を作成します。</p>"""
            self.ok(self.page("固定資産", body, err=err))

        def post_assets(self, asset_id=None):
            try:
                yearend.save_asset(self.c, self.form, int(asset_id) if asset_id else None)
            except ledger.LedgerError as exc:
                return self.get_assets(err=str(exc))
            self.redirect("/assets")

        # ---------------------------------------------------------- 決算整理
        def get_yearend(self, msg=None, err=None):
            existing = {s: ledger.search(self.c, year=self.year, source=s) for s in ("depr", "kaji", "ctax")}
            link = lambda items: "、".join(f'<a href="/entry/{i["entry"]["id"]}">No.{i["entry"]["entry_no"]}</a>' for i in items) or "未作成"
            t = ctax.compute(self.c, self.year)
            closed = self.year in db.closed_years(self.c)
            expense_opts = "".join(f'<option value="{a["code"]}">{E(a["name"])}</option>'
                                   for a in db.accounts(self.c, True) if a["category"] == "expense")
            body = f"""<ol>
<li><h2>減価償却費の計上</h2><p>作成済み: {link(existing['depr'])}</p>
<form method="post">{self.hidden()}<input type="hidden" name="action" value="depr"><button>減価償却仕訳を作成</button>
<a href="/reports/depr">計算内容を見る</a></form></li>
<li><h2>家事按分</h2><p class="muted">自宅兼事務所の家賃・光熱費・通信費など、プライベート分を事業主貸に振り替えます。作成済み: {link(existing['kaji'])}</p>
<form method="post" class="row">{self.hidden()}<input type="hidden" name="action" value="kaji">
<label>科目<select name="account">{expense_opts}</select></label>
<label>事業割合(%)<input type="number" name="pct" min="0" max="99" value="50" style="width:80px"></label><button>家事按分仕訳を作成</button></form></li>
<li><h2>消費税の未払計上（任意）</h2><p class="muted">税込経理では、納付する消費税を当年の租税公課にできます（未払計上しない場合は翌年の納付時に租税公課）。
概算納付額 {t['total']:,} 円。作成済み: {link(existing['ctax'])}</p>
<form method="post" class="row">{self.hidden()}<input type="hidden" name="action" value="ctax">
<label>金額<input type="number" name="amount" value="{t['total'] if t['total'] > 0 else ''}"></label><button>未払計上する</button></form></li>
<li><h2>年次繰越（締め）</h2><p class="muted">決算書を確定したら締めます。締めた年は仕訳を変更できなくなり、翌年の期首残高（元入金 ＝ 元入金＋所得＋事業主借−事業主貸）が作成されます。</p>
<form method="post">{self.hidden()}<input type="hidden" name="action" value="{'reopen' if closed else 'close'}">
<button class="{'danger' if closed else ''}">{f'{self.year}年の締めを解除' if closed else f'{self.year}年を締めて翌年へ繰越'}</button></form></li></ol>"""
            self.ok(self.page("決算整理・年次繰越", body, msg=msg, err=err))

        def post_yearend(self):
            action = self.form.get("action")
            try:
                if action == "depr":
                    eid = yearend.post_depreciation(self.c, self.year)
                    msg = "減価償却仕訳を作成しました"
                elif action == "kaji":
                    eid = yearend.post_kaji(self.c, self.year, self.form.get("account"), self.form.get("pct"))
                    msg = "家事按分仕訳を作成しました"
                elif action == "ctax":
                    eid = yearend.post_ctax_accrual(self.c, self.year, self.form.get("amount") or 0)
                    msg = "未払消費税を計上しました"
                elif action == "close":
                    yearend.close_year(self.c, self.year)
                    return self.get_yearend(msg=f"{self.year}年を締め、{self.year + 1}年の期首残高を作成しました")
                elif action == "reopen":
                    yearend.reopen_year(self.c, self.year)
                    return self.get_yearend(msg="締めを解除しました")
                else:
                    raise ledger.LedgerError("不明な操作です")
            except (ledger.LedgerError, ValueError) as exc:
                return self.get_yearend(err=str(exc))
            self.get_yearend(msg=f"{msg}（仕訳 ID {eid}）")

        # ---------------------------------------------------------- 設定・科目
        def get_settings(self, msg=None):
            s = lambda k: E(db.get_setting(self.c, k))
            method = db.get_setting(self.c, "tax_method")
            cat = db.get_setting(self.c, "simplified_category")
            ded = db.get_setting(self.c, "blue_deduction")
            body = f"""<form method="post">{self.hidden()}
<div class="row"><label>屋号<input name="business_name" value="{s('business_name')}"></label>
<label>氏名<input name="owner_name" value="{s('owner_name')}"></label></div>
<div class="row"><label>消費税の計算方法<select name="tax_method">{self.options(ctax.METHOD_LABELS, method)}</select></label>
<label>簡易課税の事業区分<select name="simplified_category">{self.options({str(k): v for k, v in ctax.SIMPLIFIED_LABELS.items()}, cat)}</select></label></div>
<div class="row"><label>青色申告特別控除額<select name="blue_deduction">{self.options({'650000': '65万円（e-Tax または 優良な電子帳簿）', '550000': '55万円', '100000': '10万円'}, ded)}</select></label></div>
<button>保存</button></form>
<h2>補足</h2><ul class="muted">
<li>基準期間（2年前）の課税売上高が1,000万円を超える年は消費税の課税事業者です（簡易課税は5,000万円以下で届出が必要）。</li>
<li>2割特例は免税事業者からインボイス登録で課税事業者になった方向けの経過措置です。基準期間の課税売上高が1,000万円を超える年は使えません。</li>
<li>控除額の要件は税制改正で変わることがあるので、申告前に国税庁の案内を確認してください。</li></ul>
<p><a href="/accounts">勘定科目の設定</a></p>"""
            self.ok(self.page("設定", body, msg=msg))

        def post_settings(self):
            for key in ("business_name", "owner_name", "tax_method", "simplified_category", "blue_deduction"):
                if key in self.form:
                    db.set_setting(self.c, key, self.form[key].strip())
            self.get_settings(msg="保存しました")

        def get_accounts(self, err=None, msg=None):
            cats = db.CATEGORY_LABELS
            rows = "".join(
                f"<tr><td>{E(a['code'])}</td><td>{E(a['name'])}</td><td>{cats[a['category']]}</td><td>{E(a['kessan'])}</td>"
                f"<td>{E(ledger.TAX_LABELS[a['tax_default']])}</td><td>{'○' if a['active'] else '×'}</td></tr>"
                for a in db.accounts(self.c))
            body = f"""<table><tr><th>コード</th><th>科目名</th><th>区分</th><th>決算書の科目</th><th>既定の税区分</th><th>使用</th></tr>{rows}</table>
<h2>科目の追加・変更（同じコードを入力すると上書き）</h2>
<form method="post" class="row">{self.hidden()}
<label>コード<input name="code" required style="width:80px"></label><label>科目名<input name="name" required></label>
<label>区分<select name="category">{self.options(cats, 'expense')}</select></label>
<label>決算書の科目（空欄なら科目名）<input name="kessan"></label>
<label>既定の税区分<select name="tax_default">{self.options(ledger.TAX_LABELS, 'P10')}</select></label>
<label>使用<select name="active"><option value="1">する</option><option value="0">しない</option></select></label><button>保存</button></form>
<p class="muted">決算書の科目に標準科目（租税公課〜貸倒金・雑費）以外の名前を付けると、決算書の空欄行（㉕〜㉚）に表示されます。</p>"""
            self.ok(self.page("勘定科目", body, err=err, msg=msg))

        def post_accounts(self):
            f = self.form
            try:
                db.upsert_account(self.c, f.get("code", ""), f.get("name", ""), f.get("category"),
                                  f.get("kessan", "").strip(), f.get("tax_default", "NA"), f.get("active") == "1")
            except (ValueError, Exception) as exc:  # noqa: BLE001 一意制約違反なども表示する
                return self.get_accounts(err=str(exc))
            self.get_accounts(msg="保存しました")

        # ---------------------------------------------------------- 検証・ログ・出力
        def get_verify(self):
            problems = ledger.verify(self.c)
            n = self.c.execute("SELECT COUNT(*) FROM entry_history").fetchone()[0]
            body = (f"<p>訂正削除履歴 {n} 件のハッシュチェーンと、現在の仕訳との整合性を検証しました。</p>"
                    + ("<ul>" + "".join(f"<li>{E(p)}</li>" for p in problems) + "</ul>" if problems else ""))
            self.ok(self.page("データ検証", body, msg=None if problems else "問題は見つかりませんでした",
                              err="問題が見つかりました" if problems else None))

        def get_audit(self):
            rows = "".join(f"<tr><td>{E(r['at'])}</td><td>{E(r['action'])}</td><td>{E(r['detail'])}</td></tr>"
                           for r in self.c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500"))
            self.ok(self.page("変更ログ", f"<p class=\"muted\">設定・期首残高・勘定科目・固定資産の変更記録（最新500件）。</p><table><tr><th>日時</th><th>種類</th><th>内容</th></tr>{rows}</table>"))

        def _csv(self, filename, header, rows):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(header)
            w.writerows(rows)
            data = "﻿" + buf.getvalue()  # Excel で文字化けしないよう BOM 付き
            self._send(HTTPStatus.OK, data, "text/csv; charset=utf-8",
                       {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"})

        def get_export_journal(self):
            rows = []
            for item in reports.journal(self.c, self.year):
                e = item["entry"]
                for l in item["lines"]:
                    rows.append([e["entry_no"], e["date"], e["partner"], e["description"],
                                 "借方" if l["side"] == "D" else "貸方", l["account_code"], l["account_name"],
                                 l["amount"], ledger.TAX_LABELS[l["tax"]],
                                 ledger.INVOICE_LABELS[l["invoice"]] if l["tax"].startswith("P") else "", l["memo"]])
            self._csv(f"仕訳帳_{self.year}.csv",
                      ["No", "日付", "取引先", "摘要", "貸借", "科目コード", "勘定科目", "金額", "税区分", "インボイス", "メモ"], rows)

        def get_export_history(self):
            rows = [[r["id"], r["entry_id"], r["version"], r["op"], r["reason"], r["recorded_at"], r["snapshot"], r["hash"]]
                    for r in self.c.execute("SELECT * FROM entry_history ORDER BY id")]
            self._csv("訂正削除履歴.csv", ["履歴ID", "仕訳ID", "版", "操作", "理由", "記録日時", "内容(JSON)", "ハッシュ"], rows)

    return Handler


ROUTES = [
    (r"/", "home"),
    (r"/year", "year"),
    (r"/entry/new", "entry_new"),
    (r"/entry/(\d+)", "entry"),
    (r"/entry/(\d+)/edit", "entry_edit"),
    (r"/entry/(\d+)/delete", "entry_delete"),
    (r"/entries", "entries"),
    (r"/reports/journal", "journal"),
    (r"/reports/ledger", "ledger"),
    (r"/reports/trial", "trial"),
    (r"/reports/pl", "pl"),
    (r"/reports/bs", "bs"),
    (r"/reports/monthly", "monthly"),
    (r"/reports/depr", "depr"),
    (r"/reports/ctax", "ctax"),
    (r"/opening", "opening"),
    (r"/assets", "assets"),
    (r"/assets/(\d+)", "assets"),
    (r"/yearend", "yearend"),
    (r"/settings", "settings"),
    (r"/accounts", "accounts"),
    (r"/verify", "verify"),
    (r"/audit", "audit"),
    (r"/export/journal\.csv", "export_journal"),
    (r"/export/history\.csv", "export_history"),
]


def serve(db_path, host="127.0.0.1", port=8765):
    app = App(db_path)
    db.connect(db_path).close()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"青色申告 会計ソフトを起動しました: http://{host}:{port}/  （終了は Ctrl+C）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
