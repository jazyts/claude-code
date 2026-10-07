"""データベース（SQLite）のスキーマ・初期データ・設定・監査ログ."""

import datetime
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts(
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL CHECK(category IN ('asset','liability','equity','revenue','expense')),
    kessan      TEXT NOT NULL DEFAULT '',      -- 青色申告決算書上の科目（収支科目のみ）
    tax_default TEXT NOT NULL DEFAULT 'NA',    -- 既定の消費税区分
    active      INTEGER NOT NULL DEFAULT 1
);

-- 仕訳（現在の状態）。番号は年ごとの連番で、削除しても欠番として残る。
CREATE TABLE IF NOT EXISTS entries(
    id          INTEGER PRIMARY KEY,
    year        INTEGER NOT NULL,
    entry_no    INTEGER NOT NULL,
    date        TEXT NOT NULL,
    partner     TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual',
    version     INTEGER NOT NULL DEFAULT 1,
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(year, entry_no)
);
CREATE INDEX IF NOT EXISTS entries_date ON entries(date);

CREATE TABLE IF NOT EXISTS lines(
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER NOT NULL REFERENCES entries(id),
    line_no      INTEGER NOT NULL,
    side         TEXT NOT NULL CHECK(side IN ('D','C')),
    account_code TEXT NOT NULL REFERENCES accounts(code),
    amount       INTEGER NOT NULL CHECK(amount > 0),
    tax          TEXT NOT NULL DEFAULT 'NA',
    invoice      TEXT NOT NULL DEFAULT 'Q',
    memo         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS lines_entry ON lines(entry_id);
CREATE INDEX IF NOT EXISTS lines_account ON lines(account_code);

-- 訂正・削除履歴（追記のみ。ハッシュチェーンで改ざんを検知できる）
CREATE TABLE IF NOT EXISTS entry_history(
    id          INTEGER PRIMARY KEY,
    entry_id    INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    op          TEXT NOT NULL CHECK(op IN ('create','update','delete')),
    reason      TEXT NOT NULL DEFAULT '',
    snapshot    TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS entry_history_no_update BEFORE UPDATE ON entry_history
BEGIN SELECT RAISE(ABORT, '訂正削除履歴は変更できません'); END;
CREATE TRIGGER IF NOT EXISTS entry_history_no_delete BEFORE DELETE ON entry_history
BEGIN SELECT RAISE(ABORT, '訂正削除履歴は削除できません'); END;

-- 期首残高（各科目の正常残高側をプラスで保持）
CREATE TABLE IF NOT EXISTS opening(
    year         INTEGER NOT NULL,
    account_code TEXT NOT NULL REFERENCES accounts(code),
    amount       INTEGER NOT NULL,
    PRIMARY KEY(year, account_code)
);

CREATE TABLE IF NOT EXISTS fixed_assets(
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    account_code   TEXT NOT NULL REFERENCES accounts(code),
    acquired       TEXT NOT NULL,              -- 取得（事業供用）日
    cost           INTEGER NOT NULL,           -- 取得価額（税込経理なので税込）
    life           INTEGER NOT NULL,           -- 耐用年数
    business_ratio INTEGER NOT NULL DEFAULT 100,  -- 事業専用割合(%)
    base_year      INTEGER,                    -- 導入時の未償却残高の基準年（任意）
    base_book      INTEGER,                    -- base_year 期首の未償却残高（任意）
    disposed       TEXT                        -- 除却・売却日（任意）
);

-- 発行した請求書（保存のたびに内容を audit_log に記録）
CREATE TABLE IF NOT EXISTS invoices(
    id            INTEGER PRIMARY KEY,
    number        TEXT NOT NULL,
    issue_date    TEXT NOT NULL,
    partner       TEXT NOT NULL,
    data          TEXT NOT NULL,              -- 明細・発行者・振込先などの JSON
    subtotal      INTEGER NOT NULL,
    tax           INTEGER NOT NULL,
    withholding   INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    entry_id      INTEGER REFERENCES entries(id),   -- 売上の仕訳
    paid_entry_id INTEGER REFERENCES entries(id),   -- 入金の仕訳
    cancelled     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 請求書・領収書などの証憑ファイル（変更・削除不可。日付・金額・取引先で検索できる）
CREATE TABLE IF NOT EXISTS documents(
    id         INTEGER PRIMARY KEY,
    entry_id   INTEGER REFERENCES entries(id),
    kind       TEXT NOT NULL,                 -- 発行請求書 / 受領請求書 など
    date       TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    partner    TEXT NOT NULL DEFAULT '',
    filename   TEXT NOT NULL,
    mime       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_search ON documents(date, amount, partner);
CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON documents
BEGIN SELECT RAISE(ABORT, '証憑は変更できません'); END;
CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON documents
BEGIN SELECT RAISE(ABORT, '証憑は削除できません'); END;

-- 設定・期首残高・科目などの変更ログ（追記のみ）
CREATE TABLE IF NOT EXISTS audit_log(
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, '監査ログは変更できません'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, '監査ログは削除できません'); END;
"""

# (コード, 科目名, 区分, 決算書科目, 既定税区分)
DEFAULT_ACCOUNTS = [
    ("100", "現金", "asset", "", "NA"),
    ("110", "普通預金", "asset", "", "NA"),
    ("111", "定期預金", "asset", "", "NA"),
    ("120", "売掛金", "asset", "", "NA"),
    ("125", "未収入金", "asset", "", "NA"),
    ("130", "商品", "asset", "", "NA"),
    ("135", "前払金", "asset", "", "NA"),
    ("160", "建物", "asset", "", "P10"),
    ("161", "建物附属設備", "asset", "", "P10"),
    ("162", "機械装置", "asset", "", "P10"),
    ("163", "車両運搬具", "asset", "", "P10"),
    ("164", "工具器具備品", "asset", "", "P10"),
    ("165", "ソフトウエア", "asset", "", "P10"),
    ("170", "敷金", "asset", "", "NA"),
    ("190", "事業主貸", "asset", "", "NA"),
    ("200", "買掛金", "liability", "", "NA"),
    ("210", "未払金", "liability", "", "NA"),
    ("215", "前受金", "liability", "", "NA"),
    ("220", "預り金", "liability", "", "NA"),
    ("230", "借入金", "liability", "", "NA"),
    ("300", "元入金", "equity", "", "NA"),
    ("310", "事業主借", "equity", "", "NA"),
    ("400", "売上高", "revenue", "売上", "S10"),
    ("410", "雑収入", "revenue", "売上", "NA"),
    ("500", "期首商品棚卸高", "expense", "期首商品棚卸高", "NA"),
    ("510", "仕入高", "expense", "仕入金額", "P10"),
    ("520", "期末商品棚卸高", "expense", "期末商品棚卸高", "NA"),
    ("600", "租税公課", "expense", "租税公課", "NA"),
    ("601", "荷造運賃", "expense", "荷造運賃", "P10"),
    ("602", "水道光熱費", "expense", "水道光熱費", "P10"),
    ("603", "旅費交通費", "expense", "旅費交通費", "P10"),
    ("604", "通信費", "expense", "通信費", "P10"),
    ("605", "広告宣伝費", "expense", "広告宣伝費", "P10"),
    ("606", "接待交際費", "expense", "接待交際費", "P10"),
    ("607", "損害保険料", "expense", "損害保険料", "EX"),
    ("608", "修繕費", "expense", "修繕費", "P10"),
    ("609", "消耗品費", "expense", "消耗品費", "P10"),
    ("610", "減価償却費", "expense", "減価償却費", "NA"),
    ("611", "福利厚生費", "expense", "福利厚生費", "P10"),
    ("612", "給料賃金", "expense", "給料賃金", "NA"),
    ("613", "外注工賃", "expense", "外注工賃", "P10"),
    ("614", "利子割引料", "expense", "利子割引料", "EX"),
    ("615", "地代家賃", "expense", "地代家賃", "P10"),
    ("616", "貸倒金", "expense", "貸倒金", "NA"),
    ("620", "支払手数料", "expense", "支払手数料", "P10"),
    ("621", "新聞図書費", "expense", "新聞図書費", "P10"),
    ("622", "会議費", "expense", "会議費", "P10"),
    ("623", "研修費", "expense", "研修費", "P10"),
    ("629", "雑費", "expense", "雑費", "P10"),
]

DEFAULT_SETTINGS = {
    "business_name": "",
    "owner_name": "",
    "current_year": str(datetime.date.today().year),
    # exempt=免税事業者 / general=一般課税 / simplified=簡易課税 / niwari=2割特例
    "tax_method": "general",
    "simplified_category": "5",
    "blue_deduction": "650000",
    "closed_years": "",
    # 請求書の発行者・振込先
    "issuer_title": "",
    "issuer_zip": "",
    "issuer_address": "",
    "issuer_tel": "",
    "issuer_regno": "",
    "bank_name": "",
    "bank_branch": "",
    "bank_account_type": "普通預金",
    "bank_account_number": "",
    "bank_account_holder": "",
    "invoice_note": "振込手数料は御社のご負担にてお願いいたします。",
}

CATEGORY_LABELS = {
    "asset": "資産",
    "liability": "負債",
    "equity": "資本",
    "revenue": "収益",
    "expense": "費用",
}

DEBIT_NORMAL = ("asset", "expense")


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init(conn)
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    with conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        if not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO accounts(code, name, category, kessan, tax_default) VALUES (?,?,?,?,?)",
                DEFAULT_ACCOUNTS,
            )


def audit(conn, action, detail):
    conn.execute(
        "INSERT INTO audit_log(at, action, detail) VALUES (?,?,?)",
        (now(), action, json.dumps(detail, ensure_ascii=False)),
    )


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(conn, key, value):
    value = str(value)
    if get_setting(conn, key) == value:
        return
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        audit(conn, "setting", {"key": key, "value": value})


def closed_years(conn):
    raw = get_setting(conn, "closed_years")
    return {int(y) for y in raw.split(",") if y.strip()}


def accounts(conn, active_only=False):
    sql = "SELECT * FROM accounts"
    if active_only:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY code").fetchall()


def account_map(conn):
    return {a["code"]: a for a in accounts(conn)}


def upsert_account(conn, code, name, category, kessan, tax_default, active=True):
    code, name = code.strip(), name.strip()
    if not code or not name:
        raise ValueError("科目コードと科目名は必須です")
    if category not in CATEGORY_LABELS:
        raise ValueError("区分が不正です")
    if category not in ("revenue", "expense"):
        kessan = ""
    elif not kessan:
        kessan = name
    existing = conn.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
    if existing and existing["category"] != category:
        used = conn.execute(
            "SELECT 1 FROM lines WHERE account_code = ? LIMIT 1", (code,)
        ).fetchone()
        if used:
            raise ValueError("仕訳で使用中の科目は区分を変更できません")
    with conn:
        conn.execute(
            "INSERT INTO accounts(code, name, category, kessan, tax_default, active) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
            "category=excluded.category, kessan=excluded.kessan, "
            "tax_default=excluded.tax_default, active=excluded.active",
            (code, name, category, kessan, tax_default, 1 if active else 0),
        )
        audit(
            conn,
            "account",
            {"code": code, "name": name, "category": category, "kessan": kessan,
             "tax_default": tax_default, "active": bool(active)},
        )
