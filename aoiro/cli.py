"""コマンドライン: python -m aoiro [serve|backup|verify|mcp|mcp-install]"""

import argparse
import datetime
import os
import sqlite3
import sys

from . import db, ledger, paths, web


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aoiro", description="個人事業主向け 青色申告 会計ソフト")
    parser.add_argument("--db", default=None, help="帳簿データのファイル（既定: ホームフォルダの aoiro/books.sqlite3）")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="ブラウザ画面を起動する（既定）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="起動後にブラウザで画面を開く")
    p = sub.add_parser("backup", help="帳簿データをバックアップする")
    p.add_argument("dest", nargs="?", help="保存先ファイル（既定: backups/日時.sqlite3）")
    sub.add_parser("verify", help="訂正削除履歴の改ざん・不整合を検証する")
    sub.add_parser("mcp", help="Claude 連携用の MCP サーバーとして動く（Claude Desktop から起動される）")
    sub.add_parser("mcp-install", help="Claude Desktop に連携を登録する")
    p = sub.add_parser("app", help="デスクトップアプリとして起動する（専用ウィンドウ・自動バックアップ）")
    p.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command in ("app",):
        from . import app
        return app.main(args.db, after_update=args.after_update)
    if args.db is None:
        args.db = paths.default_db()
        if args.command in (None, "serve", "mcp-install"):
            moved = paths.migrate_legacy(args.db)
            if moved:
                print(f"以前のデータ（{moved}）を {args.db} に引き継ぎました")

    if args.command == "backup":
        dest = args.dest or os.path.join(
            "backups", f"books_{datetime.datetime.now():%Y%m%d_%H%M%S}.sqlite3")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        src = db.connect(args.db)
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
        src.close()
        print(f"バックアップしました: {dest}")
    elif args.command == "verify":
        conn = db.connect(args.db)
        problems = ledger.verify(conn)
        for p in problems:
            print(p)
        print("問題は見つかりませんでした" if not problems else f"{len(problems)} 件の問題があります")
        sys.exit(1 if problems else 0)
    elif args.command == "mcp":
        from . import mcp
        mcp.serve(args.db)
    elif args.command == "mcp-install":
        from . import mcp
        for path in mcp.install_all(args.db):
            print(f"Claude Desktop に登録しました: {path}")
        print(f"帳簿データ: {os.path.abspath(args.db)}")
        print("Claude Desktop を完全に終了して（タスクトレイのアイコンも「終了」）、起動し直してください。")
    else:
        web.serve(args.db, port=getattr(args, "port", 8765), open_browser=getattr(args, "open", False))
