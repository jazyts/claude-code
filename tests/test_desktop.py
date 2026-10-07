import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from aoiro import db, ledger, maintenance, paths, updater, web


def make_books(path, amount=1100):
    conn = db.connect(path)
    ledger.post_entry(conn, "2026-04-02", [{"side": "D", "account": "609", "amount": amount},
                                           {"side": "C", "account": "310", "amount": amount}], "文具店")
    db.set_setting(conn, "current_year", 2026)
    conn.close()


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_migrate_legacy_once(self):
        old = os.path.join(self.tmp, "old", "books.sqlite3")
        os.makedirs(os.path.dirname(old))
        make_books(old)
        new = os.path.join(self.tmp, "home", "books.sqlite3")
        os.makedirs(os.path.dirname(new))
        db.connect(new).close()  # 空のデータがすでにある場合も引っ越す
        self.assertEqual(paths.migrate_legacy(new, [old]), old)
        self.assertTrue(paths.has_entries(new))
        self.assertIsNone(paths.migrate_legacy(new, [old]))  # 2回目は何もしない
        self.assertTrue(paths.is_ledger_file(new))
        bogus = os.path.join(self.tmp, "x.sqlite3")
        with open(bogus, "wb") as f:
            f.write(b"not sqlite")
        self.assertFalse(paths.is_ledger_file(bogus))

    def test_backup_and_summary_to_onedrive(self):
        books = os.path.join(self.tmp, "books.sqlite3")
        make_books(books)
        onedrive = os.path.join(self.tmp, "OneDrive")
        os.makedirs(onedrive)
        conn = db.connect(books)
        db.set_setting(conn, "onedrive_dir", onedrive)
        conn.close()
        made = maintenance.backup(books)
        self.assertEqual(len(made), 2)
        self.assertEqual(maintenance.backup(books), [])  # 同じ日は1回だけ
        for i in range(maintenance.KEEP_BACKUPS + 3):
            maintenance.backup(books, label=f"x{i:03d}", force=True)
        self.assertLessEqual(len(os.listdir(os.path.join(onedrive, "aoiro", "backups"))), maintenance.KEEP_BACKUPS)
        with mock.patch.object(maintenance, "find_edge", return_value=""):
            path = maintenance.write_summary(books)
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("1,100", html)
        self.assertIn("文具店", html)

    def test_update_check(self):
        release = {"tag_name": "v9.0.0", "body": "改善", "html_url": "https://example.invalid",
                   "assets": [{"name": "aoiro.exe", "browser_download_url": "https://example.invalid/aoiro.exe"},
                              {"name": "aoiro.exe.sha256", "browser_download_url": "https://example.invalid/s"}]}
        with mock.patch.object(updater, "_get", return_value=json.dumps(release).encode()):
            info = updater.check_latest()
        self.assertEqual(info["version"], "9.0.0")
        with mock.patch.object(updater, "_get", return_value=json.dumps(dict(release, tag_name="v0.0.1")).encode()):
            self.assertIsNone(updater.check_latest())
        with mock.patch.object(updater, "_get", side_effect=OSError("offline")):
            self.assertIsNone(updater.check_latest())
        self.assertGreater(updater.parse_version("v1.10.0"), updater.parse_version("1.9.9"))

    def test_restart_env_does_not_reuse_parent_bundle(self):
        with mock.patch.dict(os.environ, {"_MEIPASS2": "C:\\Temp\\_MEI123", "_PYI_APPLICATION_HOME_DIR": "x",
                                          "PATH": "p"}):
            env = updater.restart_env()
        self.assertNotIn("_MEIPASS2", env)
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["PATH"], "p")


class DesktopWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web.App(os.path.join(tempfile.mkdtemp(), "b.sqlite3"), desktop=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.app))
        cls.app.server = cls.server
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode("utf-8")

    def test_heartbeat_settings_update_restore(self):
        body = self.get("/")
        self.assertIn("/ping", body)
        self.assertIn("終了", body)
        before = self.app.last_ping
        self.assertEqual(self.get("/ping"), "ok")
        self.assertGreaterEqual(self.app.last_ping, before)
        settings = self.get("/settings")
        for text in ("OneDrive", "Claude Desktop に連携を設定する", "以前の帳簿データ"):
            self.assertIn(text, settings)
        with mock.patch.object(updater, "check_latest", return_value=None):
            self.assertIn("最新の版", self.get("/update"))
        self.app.update_info = {"version": "9.9.9", "notes": "x", "page": "https://example.invalid",
                                "exe_url": "", "sha_url": ""}
        self.assertIn("新しい版（9.9.9）", self.get("/"))
        self.app.update_info = None

        src = os.path.join(tempfile.mkdtemp(), "books.sqlite3")
        make_books(src, amount=7777)
        with open(src, "rb") as f:
            import base64
            data = base64.b64encode(f.read()).decode()
        req = urllib.request.Request(self.base + "/data/restore", data=urllib.parse.urlencode(
            {"_token": self.app.token, "data": data, "filename": "books.sqlite3"}).encode())
        with urllib.request.urlopen(req) as r:
            self.assertIn("読み込みました", r.read().decode("utf-8"))
        self.assertIn("7,777", self.get("/entries?y=2026"))


if __name__ == "__main__":
    unittest.main()
