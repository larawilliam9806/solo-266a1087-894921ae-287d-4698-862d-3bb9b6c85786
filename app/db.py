"""SQLite 存储层。

表结构：
- sample_groups        样组（登记后固定）：读数总和、独立测量方差之和
- group_blank_usages   样组对各空白样的使用次数（每次测量只用一份空白样，
                       因此一个样组的使用次数之和即其测量总数）
- blanks               空白样：扣减值与方差，可另行登记、更正
- summaries            汇总结果快照（生成时的计算结果）
- summary_groups       汇总实际收录的样组（已去重）
- summary_blanks       汇总生成时采用的空白样数据快照（查旧汇总时原样返回）
- summary_aggregates   汇总存量聚合（续算底稿）：以精确有理数分量保存读数
                       总和、独立测量方差之和及各空白样使用次数。升级前生成
                       的汇总首次成功被引用时补读一次，之后跨重启复用。
"""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_groups (
    group_id     TEXT PRIMARY KEY,
    analyte      TEXT NOT NULL,
    unit         TEXT NOT NULL,
    reading_sum  REAL NOT NULL,
    variance_sum REAL NOT NULL CHECK (variance_sum >= 0),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_blank_usages (
    group_id    TEXT NOT NULL REFERENCES sample_groups (group_id),
    blank_id    TEXT NOT NULL,
    usage_count INTEGER NOT NULL CHECK (usage_count > 0),
    PRIMARY KEY (group_id, blank_id)
);

CREATE TABLE IF NOT EXISTS blanks (
    blank_id   TEXT PRIMARY KEY,
    deduction  REAL NOT NULL,
    variance   REAL NOT NULL CHECK (variance >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analyte               TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    total_measurements    INTEGER NOT NULL,
    reading_sum           REAL NOT NULL,
    variance_sum          REAL NOT NULL,
    blank_deduction_total REAL NOT NULL,
    corrected_mean        REAL NOT NULL,
    standard_error        REAL NOT NULL,
    source_group_ids      TEXT NOT NULL,
    source_summary_ids    TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_groups (
    summary_id INTEGER NOT NULL REFERENCES summaries (summary_id),
    group_id   TEXT NOT NULL,
    PRIMARY KEY (summary_id, group_id)
);

CREATE TABLE IF NOT EXISTS summary_blanks (
    summary_id  INTEGER NOT NULL REFERENCES summaries (summary_id),
    blank_id    TEXT NOT NULL,
    usage_count INTEGER NOT NULL,
    deduction   REAL NOT NULL,
    variance    REAL NOT NULL,
    PRIMARY KEY (summary_id, blank_id)
);

CREATE TABLE IF NOT EXISTS summary_aggregates (
    summary_id          INTEGER PRIMARY KEY REFERENCES summaries (summary_id),
    reading_num         TEXT NOT NULL,
    reading_den         TEXT NOT NULL,
    variance_num        TEXT NOT NULL,
    variance_den        TEXT NOT NULL,
    blank_usages_json   TEXT NOT NULL,
    backfilled          INTEGER NOT NULL DEFAULT 0
);
"""

# 聚合分量以十进制整数字符串保存（分子/分母可能超出 SQLite 64 位整数范围）。
# 若库中残留早期 INTEGER 定义的同名表（本功能开发期间的旧版），直接重建：
# 表内只是可重新补读的缓存，丢失不影响已保存汇总的快照。
_MIGRATIONS = [
    (
        "SELECT 1 FROM pragma_table_info('summary_aggregates') "
        "WHERE name = 'reading_num' AND type = 'INTEGER'",
        "DROP TABLE summary_aggregates",
    ),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        for probe, fix in _MIGRATIONS:
            if conn.execute(probe).fetchone() is not None:
                conn.execute(fix)
        conn.executescript(SCHEMA)
