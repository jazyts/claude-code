"""コマンドライン: python -m aoiro [serve|backup|verify]"""

import argparse
import datetime
import os
import sqlite3
import sys

from . import db, ledger, web


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aoiro", description="個人事業主向け 青色申告 会計ソフト")
    parser.add_argument("--db", default=os.environ.get("AOIRO_DB", "books.sqlite3"),
                        help="帳簿データのファイル（既定: books.sqlite3）")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="ブラウザ画面を起動する（既定）")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("backup", help="帳簿データをバックアップする")
    p.add_argument("dest", nargs="?", help="保存先ファイル（既定: backups/日時.sqlite3）")
    sub.add_parser("verify", help="訂正削除履歴の改ざん・不整合を検証する")
    args = parser.parse_args(argv)

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
    else:
        web.serve(args.db, port=getattr(args, "port", 8765))
