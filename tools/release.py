"""main に取り込まれたとき、バージョンのリリースがまだなければ作成する（GitHub Actions で実行）。"""

import os
import re
import subprocess
import sys

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
subprocess.run(["gh", "release", "create", tag, "dist/aoiro.exe", "dist/aoiro.exe.sha256",
                "--title", f"aoiro {__version__}", "--notes-file", "release-notes.md", "--target", os.environ.get("GITHUB_SHA", "main")],
               check=True)
print(f"{tag} をリリースしました")
