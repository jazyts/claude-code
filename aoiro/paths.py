"""データの保存場所・OneDrive の場所・旧データの引っ越し."""

import os
import shutil
import sqlite3
import sys

DB_NAME = "books.sqlite3"


def data_dir():
    """帳簿データの保存先（アプリ本体とは別の固定の場所。OneDrive の同期対象外）"""
    base = os.environ.get("AOIRO_HOME") or os.path.join(os.path.expanduser("~"), "aoiro")
    os.makedirs(base, exist_ok=True)
    return base


def default_db():
    return os.environ.get("AOIRO_DB") or os.path.join(data_dir(), DB_NAME)


def app_dir():
    """exe（またはソース）の置き場所"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_onedrive():
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        path = os.environ.get(key)
        if path and os.path.isdir(path):
            return path
    home = os.path.expanduser("~")
    for name in ("OneDrive",):
        path = os.path.join(home, name)
        if os.path.isdir(path):
            return path
    return ""


def has_entries(path):
    """帳簿データとして使われている（仕訳や期首残高がある）か"""
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT (SELECT COUNT(*) FROM entries) + (SELECT COUNT(*) FROM opening)").fetchone()[0]
        finally:
            conn.close()
        return n > 0
    except sqlite3.Error:
        return False


def is_ledger_file(path):
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1 FROM entries LIMIT 1")
            conn.execute("SELECT 1 FROM entry_history LIMIT 1")
        finally:
            conn.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def copy_db(src, dst):
    """SQLite のバックアップ機能で安全に複製する（書込み中でも整合性を保つ）"""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
    finally:
        d.close()
        s.close()


def migrate_legacy(db_path, candidates=None):
    """以前の版（フォルダ内の books.sqlite3）から、固定の保存先へデータを引っ越す。"""
    if has_entries(db_path):
        return None
    candidates = candidates or [os.path.join(os.getcwd(), DB_NAME), os.path.join(app_dir(), DB_NAME)]
    for path in candidates:
        if os.path.abspath(path) != os.path.abspath(db_path) and has_entries(path):
            for suffix in ("", "-wal", "-shm"):  # 空のデータは付属ファイルごと退避する
                if os.path.exists(db_path + suffix):
                    shutil.move(db_path + suffix, db_path + ".empty-before-migration" + suffix)
            copy_db(path, db_path)
            return path
    return None
