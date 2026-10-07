"""main に取り込まれたとき、バージョンのリリースがまだなければ作成する（GitHub Actions で実行）。"""

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aoiro import __version__  # noqa: E402

tag = f"v{__version__}"
if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0:
    print(f"{tag} はリリース済みです")
    sys.exit(0)

with open("CHANGELOG.md", encoding="utf-8") as f:
    text = f.read()
m = re.search(rf"^## {re.escape(__version__)}\b.*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
notes = m.group(1).strip() if m else f"aoiro {__version__}"
with open("release-notes.md", "w", encoding="utf-8") as f:
    f.write(notes)
cmd = ["gh", "release", "create", tag, "dist/aoiro.exe", "dist/aoiro.exe.sha256",
       "--title", f"aoiro {__version__}", "--notes-file", "release-notes.md", "--target", os.environ.get("GITHUB_SHA", "main")]
for attempt in range(4):  # GitHub が一時的に 500 を返すことがあるので数回やり直す
    if subprocess.run(cmd).returncode == 0:
        break
    if subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0:
        # 途中まで作成された場合はファイルを上書きでアップロードし直す
        subprocess.run(["gh", "release", "upload", tag, "dist/aoiro.exe", "dist/aoiro.exe.sha256", "--clobber"], check=True)
        break
    time.sleep(15 * (attempt + 1))
else:
    sys.exit(f"{tag} のリリースを作成できませんでした")
print(f"{tag} をリリースしました")
