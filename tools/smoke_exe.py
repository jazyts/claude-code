"""ビルドした aoiro.exe を実際に動かして確認する（GitHub Actions の Windows 上で実行）。

1. コマンド（verify）が動く
2. Claude 連携（MCP）がパイプ越しに応答する（ウィンドウ版 exe の標準入出力の扱いを確認）
3. デスクトップアプリとして起動し、画面を返し、「終了」で止まる
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

exe = os.path.abspath(sys.argv[1])
home = tempfile.mkdtemp()
env = dict(os.environ, AOIRO_HOME=home)
db = os.path.join(home, "books.sqlite3")

r = subprocess.run([exe, "--db", db, "verify"], env=env, timeout=120)
assert r.returncode == 0, f"verify failed: {r.returncode}"
print("verify OK")

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                    "clientInfo": {"name": "smoke"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "post_entries", "arguments": {"entries": [
        {"date": "2026-04-02", "partner": "文具店", "lines": [{"side": "借方", "account": "消耗品費", "amount": 1100},
                                                           {"side": "貸方", "account": "事業主借", "amount": 1100}]}]}}},
]
stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8")
r = subprocess.run([exe, "--db", db, "mcp"], input=stdin, capture_output=True, env=env, timeout=120)
replies = [json.loads(line) for line in r.stdout.decode("utf-8").splitlines() if line.strip()]
assert [x["id"] for x in replies] == [1, 2], (r.stdout, r.stderr)
assert "1 件登録しました" in replies[1]["result"]["content"][0]["text"], replies[1]
print("mcp OK")

proc = subprocess.Popen([exe], env=env)
base = "http://127.0.0.1:8765"
for _ in range(120):
    try:
        with urllib.request.urlopen(base + "/ping", timeout=2) as resp:
            if resp.status == 200:
                break
    except OSError:
        time.sleep(0.5)
else:
    proc.kill()
    raise SystemExit("app did not start")
with urllib.request.urlopen(base + "/entries?y=2026", timeout=10) as resp:
    page = resp.read().decode("utf-8")
assert "文具店" in page, "entry posted via MCP is not visible in the app"
with urllib.request.urlopen(base + "/favicon.ico", timeout=10) as resp:
    assert resp.read()[:4] == b"\x00\x00\x01\x00"
token = re.search(r'name="_token" value="([^"]+)"', page).group(1)
req = urllib.request.Request(base + "/quit", data=urllib.parse.urlencode({"_token": token}).encode())
urllib.request.urlopen(req, timeout=10).read()
proc.wait(timeout=30)
print("app OK")
