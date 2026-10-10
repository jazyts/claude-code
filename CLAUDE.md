# CLAUDE.md — aoiro（個人事業主向け 青色申告 会計ソフト）

このファイルは Claude Code がこのリポジトリで作業するときの前提知識です。利用者向けの説明は README.md、変更履歴は CHANGELOG.md を参照。

## 何のプロジェクトか

- 個人事業主の青色申告（複式簿記・税込経理）用の会計ソフト。Windows 向けに `aoiro.exe` として配布し、Python からも動く。
- **依存ライブラリなし（標準ライブラリのみ）**。新しい依存を追加しない。`pyinstaller` はビルド時のみ。
- 利用者はプログラマーではない。画面・メッセージ・エラー文はすべて**日本語**で、専門用語は避ける。
- Claude Desktop と MCP で連携し、会話で仕訳登録・請求書作成ができる（`aoiro/mcp.py`）。

## 構成（aoiro/）

| ファイル | 役割 |
|---|---|
| `db.py` | SQLite スキーマ、勘定科目の初期データ（コード・名前・区分・既定の消費税区分）、設定、`closed_years` |
| `ledger.py` | 仕訳の登録・訂正・削除・検索。訂正削除履歴をハッシュ連鎖で残す（`verify` で検証） |
| `reports.py` | 試算表・損益計算書・貸借対照表・月別・総勘定元帳 |
| `ctax.py` | 消費税の計算 |
| `yearend.py` | 減価償却・家事按分・消費税未払計上などの決算整理、固定資産 |
| `invoices.py` | 請求書の作成・PDF・入金 |
| `importer.py` | Excel/CSV 取込、科目名・金額・税区分の解釈（`resolve_account` など） |
| `pdftext.py` / `xlsx.py` | PDF テキスト抽出・xlsx 読み書き（自前実装） |
| `web.py` | ブラウザ画面（`http://127.0.0.1:8765`、標準ライブラリの HTTP サーバー） |
| `app.py` | Windows デスクトップアプリ（専用ウィンドウ・自動バックアップ・OneDrive サマリー） |
| `mcp.py` | Claude Desktop 向け MCP サーバー（stdio・JSON-RPC）。ツール定義は `TOOLS` リスト |
| `updater.py` / `maintenance.py` / `paths.py` | 自己更新、バックアップ、データの保存場所 |
| `cli.py` | サブコマンド: `serve` `backup` `verify` `mcp` `mcp-install` `app` |

データは `~/aoiro/books.sqlite3`（環境変数 `AOIRO_HOME` / `AOIRO_DB` で変更可）。`.sqlite3` は Git に入れない。

## 会計上の決まり（コードを触る前に）

- 金額は**税込の整数（円）**。仕訳は借方合計＝貸方合計でなければ `ledger.LedgerError`。
- 勘定科目は 3 桁コード。よく使うもの: 110 普通預金、120 売掛金、190 事業主貸、300 元入金、310 事業主借、400 売上高。科目はコードでも名前でも指定できる（`importer.resolve_account`）。
- 事業用口座以外で払った経費は貸方「事業主借」、事業用口座からの生活費は借方「事業主貸」。
- 源泉徴収のある売上: 売掛金（入金予定額）＋ 事業主貸（源泉所得税）／ 売上高（税込）。
- 訂正・削除は理由必須で履歴が残る。`closed_years` に含まれる年は変更不可。履歴の改ざん検出があるので、`entries` / `lines` / 履歴テーブルを直接 UPDATE しない。必ず `ledger.py` の関数を通す。
- 仕訳番号は年ごとの連番で、削除しても欠番として残す。

## 開発コマンド

```bash
python -m unittest discover -s tests -v   # テスト（CI と同じ）
python -m aoiro serve                      # ブラウザ画面
python -m aoiro mcp                        # MCP サーバー（stdio）
python -m aoiro verify                     # 履歴の検証
```

- テストは `unittest` のみ（pytest 不要）。`db.connect(":memory:")` で作ったインメモリ DB に `ledger.post_entry` でデータを入れる形式（`tests/test_books.py` 参照）。
- 機能を足したら対応するテストを `tests/` に追加する。MCP ツールを足したら `tests/test_mcp.py` にも。
- CI（`.github/workflows/build.yml`）: Ubuntu でテスト → Windows でテスト・`pyinstaller` ビルド・`tools/smoke_exe.py` → main なら `tools/release.py` がリリース作成。

## MCP ツールを変更するとき

- `mcp.py` の `TOOLS` に `(name, handler, 説明, properties, required)` を追加し、書き込み系なら `WRITE_TOOLS` にも入れる（`readOnlyHint` に使う）。
- 説明文は Claude が読む。日本語で、いつ使うか・既定値・前提条件（例: 「小計＋消費税−源泉徴収＝合計」）を書く。
- 登録・訂正・削除の前にユーザー確認を取る方針（`INSTRUCTIONS`）を崩さない。複数件は `dry_run=true` で先に検証する設計。
- 返り値は人が読むテキスト（`_fmt_entry` の形式）。

## リリース

1. `aoiro/__init__.py` の `__version__` を上げる
2. `CHANGELOG.md` の先頭に `## <version>` の節を追加（利用者向けの言葉で）
3. main に取り込むと GitHub Actions がビルドしてリリースを作る。アプリの「更新」ボタンはこのリリースを見る

## 書き方の方針

- 変更は最小限。既存のモジュール境界（db / ledger / reports / web / mcp）を守る。
- 利用者に見える文言は日本語。コードのコメント・docstring も日本語で統一されている。
- Windows で動くことを常に考える（パス、文字コードは `encoding="utf-8"`、`PYTHONUTF8=1`）。
