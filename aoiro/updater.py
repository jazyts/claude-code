"""アプリの更新（GitHub Releases から新しい aoiro.exe を取得して置き換える）."""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request

from . import __version__

REPO = "jazyts/claude-code"
ASSET = "aoiro.exe"
API = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(text):
    nums = []
    for part in str(text).lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits or 0))
    return tuple(nums + [0] * (3 - len(nums)))


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": f"aoiro/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_latest(timeout=5):
    """新しい版があれば {"version", "notes", "exe_url", "sha_url"} を返す。なければ None。"""
    try:
        data = json.loads(_get(API, timeout))
    except Exception:  # noqa: BLE001 オフラインなどは静かに無視
        return None
    version = data.get("tag_name", "")
    if parse_version(version) <= parse_version(__version__):
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    if ASSET not in assets:
        return None
    return {"version": version.lstrip("vV"), "notes": data.get("body") or "", "exe_url": assets[ASSET],
            "sha_url": assets.get(ASSET + ".sha256"), "page": data.get("html_url", "")}


def can_self_update():
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def cleanup_old():
    if not getattr(sys, "frozen", False):
        return
    old = os.path.join(os.path.dirname(sys.executable), "aoiro.old.exe")
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass


def apply(info, restart_args=None):
    """新しい exe をダウンロードして検証し、置き換えて起動し直す。成功したら True。"""
    if not can_self_update():
        raise RuntimeError("自動更新は Windows 版アプリ（aoiro.exe）でのみ使えます")
    exe = sys.executable
    folder = os.path.dirname(exe)
    new = os.path.join(folder, "aoiro.new.exe")
    old = os.path.join(folder, "aoiro.old.exe")
    data = _get(info["exe_url"], timeout=120)
    if info.get("sha_url"):
        expected = _get(info["sha_url"]).decode("ascii", "ignore").split()[0].strip().lower()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("ダウンロードしたファイルの検証に失敗しました（もう一度お試しください）")
    with open(new, "wb") as f:
        f.write(data)
    if os.path.exists(old):
        os.remove(old)
    os.replace(exe, old)  # 実行中の exe は削除できないが名前の変更はできる
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe] + list(restart_args or []), creationflags=flags, close_fds=True)
    return True
