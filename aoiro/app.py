"""デスクトップアプリとしての起動（aoiro.exe をダブルクリックしたとき）.

- 帳簿データは ~/aoiro/books.sqlite3（初回は以前の版のデータを自動で引っ越し）
- 起動時に1日1回バックアップ（OneDrive があればそこにも）
- Edge のアプリモードで専用ウィンドウを開く（なければ既定のブラウザ）
- ウィンドウを閉じて一定時間たつと自動で終了
"""

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import maintenance, paths, updater, web

PORT = 8765


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_window(url):
    edge = maintenance.find_edge() if sys.platform == "win32" else ""
    if edge:
        profile = os.path.join(paths.data_dir(), "window-profile")
        try:
            subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                              "--no-default-browser-check", "--window-size=1280,900"],
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
            return
        except OSError:
            pass
    webbrowser.open(url)


def main(db_path=None, port=PORT, after_update=False):
    updater.cleanup_old()
    url = f"http://127.0.0.1:{port}/"
    if after_update:
        # 更新前の版が終了するのを待つ（開いているウィンドウはそのまま新しい版につながる）
        for _ in range(60):
            if not _port_in_use(port):
                break
            time.sleep(0.5)
    if _port_in_use(port):
        open_window(url)  # すでに起動中ならウィンドウを開くだけ
        return
    db_path = db_path or paths.default_db()
    migrated = paths.migrate_legacy(db_path)
    try:
        maintenance.backup(db_path)
    except Exception:  # noqa: BLE001 バックアップの失敗で起動を止めない
        pass
    if not after_update:
        threading.Timer(1.0, open_window, (url,)).start()
    web.serve(db_path, port=port, desktop=True, notice=(f"以前のデータ（{migrated}）を引き継ぎました" if migrated else None))
