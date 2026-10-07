"""Windows アプリ（aoiro.exe）の入口。引数なしならデスクトップアプリとして起動する。"""

import sys

from aoiro import app, cli

if __name__ == "__main__":
    if len(sys.argv) == 1:
        app.main()
    elif sys.argv[1:] == ["--after-update"]:
        app.main(after_update=True)
    else:
        cli.main()
