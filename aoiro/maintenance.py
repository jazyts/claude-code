"""自動バックアップと、スマホで見るためのサマリー（OneDrive に保存）."""

import datetime
import glob
import html
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from . import ctax, db, invoices, ledger, paths, reports

KEEP_BACKUPS = 30
SUMMARY_NAME = "aoiro_サマリー"
E = html.escape


def onedrive_folder(conn):
    base = db.get_setting(conn, "onedrive_dir") or paths.detect_onedrive()
    if not base or not os.path.isdir(base):
        return ""
    folder = os.path.join(base, "aoiro")
    os.makedirs(folder, exist_ok=True)
    return folder


def _prune(folder, pattern):
    files = sorted(glob.glob(os.path.join(folder, pattern)))
    for old in files[:-KEEP_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass


def backup(db_path, label=None, force=False):
    """1日1回（または force 時）に帳簿データを複製する。OneDrive があればそこにも置く。"""
    stamp = label or datetime.date.today().strftime("%Y%m%d")
    targets = [os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")]
    conn = db.connect(db_path)
    try:
        od = onedrive_folder(conn) if db.get_setting(conn, "onedrive_backup") != "0" else ""
    finally:
        conn.close()
    if od:
        targets.append(os.path.join(od, "backups"))
    made = []
    for folder in targets:
        dst = os.path.join(folder, f"books_{stamp}.sqlite3")
        if os.path.exists(dst) and not force:
            continue
        try:
            paths.copy_db(db_path, dst)
            _prune(folder, "books_*.sqlite3")
            made.append(dst)
        except OSError:
            pass
    return made


# ---------------------------------------------------------------- スマホ用サマリー

def summary_html(conn):
    year = int(db.get_setting(conn, "current_year"))
    pl = reports.profit_loss(conn, year)
    bal = reports.balances(conn, year)
    month = reports.monthly(conn, year)
    tax = ctax.compute(conn, year)
    wh = sum(r["withholding"] for r in invoices.withholding_summary(conn, year))
    unpaid = [r for r in invoices.listing(conn) if not r["cancelled"] and not r["paid_entry_id"]]
    recent = ledger.search(conn, year=year)[-15:][::-1]
    cash = [(b["account"]["name"], b["closing"]) for b in bal.values()
            if b["account"]["category"] in ("asset", "liability") and b["closing"]
            and b["account"]["name"] not in ("事業主貸",)]
    peak = max(month["sales"] + [1])
    bars = "".join(
        f'<tr><td>{i + 1}月</td><td class="n">{v:,}</td><td style="width:40%"><div class="bar" style="width:{v * 100 // peak}%"></div></td></tr>'
        for i, v in enumerate(month["sales"]))
    card = lambda label, value: f'<div class="card"><div class="l">{label}</div><div class="v">{value:,}<small>円</small></div></div>'
    unpaid_rows = "".join(f'<tr><td>{E(r["issue_date"][5:].replace("-", "/"))}</td><td>{E(r["partner"])}</td><td class="n">{r["total"]:,}</td></tr>'
                          for r in unpaid) or '<tr><td colspan="3" class="m">なし</td></tr>'
    recent_rows = "".join(
        f'<tr><td>{E(i["entry"]["date"][5:].replace("-", "/"))}</td><td>{E(i["entry"]["partner"] or i["entry"]["description"])}</td>'
        f'<td class="n">{sum(l["amount"] for l in i["lines"] if l["side"] == "D"):,}</td></tr>' for i in recent)
    expenses = "".join(f'<tr><td>{E(e["name"])}</td><td class="n">{e["amount"]:,}</td></tr>'
                       for e in sorted(pl["expenses"], key=lambda e: -e["amount"]) if e["amount"])
    balances = "".join(f'<tr><td>{E(n)}</td><td class="n">{v:,}</td></tr>' for n, v in cash)
    name = db.get_setting(conn, "business_name") or "青色申告"
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
    tax_card = card("消費税（概算）", tax["total"]) if tax["method"] != "exempt" else ""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(name)} {year}年</title>
<style>body{{font-family:"Yu Gothic","Hiragino Sans",sans-serif;margin:0;padding:12px;color:#1d2330;background:#fff;font-size:15px}}
h1{{font-size:18px;margin:0 0 2px}}.m{{color:#667085;font-size:12px}}h2{{font-size:15px;margin:18px 0 6px;border-left:4px solid #123e7c;padding-left:6px}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.card{{border:1px solid #d0d5dd;border-radius:8px;padding:8px}}
.l{{font-size:12px;color:#667085}}.v{{font-size:19px;font-weight:700}}.v small{{font-size:11px;font-weight:400;margin-left:2px}}
table{{width:100%;border-collapse:collapse}}td{{border-bottom:1px solid #eaecf0;padding:5px 2px}}.n{{text-align:right;white-space:nowrap}}
.bar{{height:10px;background:#1f5fbf;border-radius:2px;min-width:1px}}td:first-child{{white-space:nowrap;padding-right:8px}}</style></head><body>
<h1>{E(name)} {year}年</h1><div class="m">{now} 時点（パソコンのデータから自動作成・閲覧専用）</div>
<div class="cards" style="margin-top:10px">{card("売上（収入）", pl["sales"])}{card("経費", pl["cost"] + pl["expense_total"])}
{card("控除前の所得", pl["pre_income"])}{card("源泉徴収税額", wh)}{tax_card}</div>
<h2>未入金の請求書</h2><table>{unpaid_rows}</table>
<h2>月別売上</h2><table>{bars}</table>
<h2>経費の内訳</h2><table>{expenses or '<tr><td class="m">なし</td></tr>'}</table>
<h2>残高</h2><table>{balances or '<tr><td class="m">なし</td></tr>'}</table>
<h2>最近の仕訳</h2><table>{recent_rows or '<tr><td class="m">なし</td></tr>'}</table>
</body></html>"""


def find_edge():
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("msedge") or ""


def html_to_pdf(html_path, pdf_path):
    """Edge のヘッドレス印刷で PDF にする（スマホの OneDrive アプリで見やすいように）"""
    edge = find_edge()
    if not edge:
        return False
    profile = tempfile.mkdtemp(prefix="aoiro-pdf-")
    try:
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
        subprocess.run([edge, "--headless", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                        "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", "file:///" + html_path.replace("\\", "/")],
                       timeout=60, capture_output=True, **flags)
        return os.path.exists(pdf_path)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def write_summary(db_path):
    conn = db.connect(db_path)
    try:
        if db.get_setting(conn, "phone_summary") == "0":
            return None
        folder = onedrive_folder(conn)
        if not folder:
            return None
        content = summary_html(conn)
    finally:
        conn.close()
    html_path = os.path.join(folder, SUMMARY_NAME + ".html")
    tmp = html_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, html_path)
    pdf_path = os.path.join(folder, SUMMARY_NAME + ".pdf")
    tmp_pdf = os.path.join(tempfile.gettempdir(), "aoiro_summary_tmp.pdf")
    if html_to_pdf(html_path, tmp_pdf):
        shutil.move(tmp_pdf, pdf_path)
    return html_path


class SummaryScheduler:
    """変更のたびにすぐ作り直すと重いので、最後の変更から少し待ってまとめて作る。"""

    def __init__(self, db_path, delay=5.0):
        self.db_path = db_path
        self.delay = delay
        self.timer = None
        self.lock = threading.Lock()

    def touch(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._run)
            self.timer.daemon = True
            self.timer.start()

    def _run(self):
        try:
            write_summary(self.db_path)
        except Exception:  # noqa: BLE001 サマリーの失敗で本体を止めない
            pass

    def flush(self):
        with self.lock:
            timer, self.timer = self.timer, None
        if timer:
            timer.cancel()
            self._run()
