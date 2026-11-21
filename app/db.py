"""SQLite 连接与建表（含交易日更正功能的事件溯源结构）。

表结构：
- prices            单股日收盘价底表（单位：分）。每行有稳定主键 price_id，
                    交易日更正只 UPDATE trade_date，收盘价不动、记录不复制不删除，
                    原始价格记录物理上只存一份；trade_date 仍保持全局唯一。
- price_events      价格底表变更事件流（只追加）：导入(import)/更正(correction)
                    各占一个递增 seq；重放 seq<=N 的事件即可还原任一历史时刻的
                    日期归属，供旧送审号按送审时序列重开。
- price_event_items 事件明细：导入记 price_id+trade_date；更正记 from_date->to_date。
- corrections       交易日更正单（单号唯一，正文哈希用于“原样重传/同号异文”判定）。
- correction_items  更正单内的原交易日 -> 正确交易日对应表。
- announcements     公告现行版本（每号一行，只保留最高修订）。
- announcements_log 公告收发流水。
- submissions       锁定送审记录（区间、基准日、依据指纹、送审时价格序列 price_seq）。
- submission_basis  每次送审重算所需的公告依据快照（只留依据，不留行情副本）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

# 测试可通过环境变量改库；容器内固定 /data/app.db
DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "/data/app.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS prices (
    price_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,
    close_cents  INTEGER NOT NULL CHECK (close_cents >= 0),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (trade_date)
);

CREATE TABLE IF NOT EXISTS price_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('import', 'correction')),
    ref_no      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_event_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seq         INTEGER NOT NULL,
    price_id    INTEGER,
    from_date   TEXT NOT NULL DEFAULT '',
    to_date     TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (seq) REFERENCES price_events(seq)
);

CREATE TABLE IF NOT EXISTS corrections (
    corr_no     TEXT NOT NULL PRIMARY KEY,
    body_hash   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS correction_items (
    corr_no     TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    from_date   TEXT NOT NULL,
    to_date     TEXT NOT NULL,
    PRIMARY KEY (corr_no, line_no),
    FOREIGN KEY (corr_no) REFERENCES corrections(corr_no)
);

CREATE TABLE IF NOT EXISTS announcements (
    ann_no       TEXT NOT NULL PRIMARY KEY,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    effective_date TEXT NOT NULL,
    old_shares   INTEGER NOT NULL CHECK (old_shares > 0),
    new_shares   INTEGER NOT NULL CHECK (new_shares > 0),
    body_hash    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS announcements_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ann_no       TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    effective_date TEXT NOT NULL,
    old_shares   INTEGER NOT NULL,
    new_shares   INTEGER NOT NULL,
    body_hash    TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('inserted', 'updated', 'reused', 'rejected_diff', 'rejected_old')),
    note         TEXT NOT NULL DEFAULT '',
    received_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    base_date     TEXT NOT NULL,
    basis_hash    TEXT NOT NULL,
    price_seq     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS submission_basis (
    submission_id  TEXT NOT NULL,
    ann_no         TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    effective_date TEXT NOT NULL,
    old_shares     INTEGER NOT NULL,
    new_shares     INTEGER NOT NULL,
    PRIMARY KEY (submission_id, ann_no),
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_ann_eff ON announcements(effective_date);
CREATE INDEX IF NOT EXISTS idx_event_items_seq ON price_event_items(seq);
"""


def get_db_path() -> str:
    return os.environ.get("APP_DB_PATH", DEFAULT_DB_PATH)


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor(db_path: str | None = None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_legacy_prices(conn: sqlite3.Connection) -> None:
    """旧库 prices 没有 price_id 主键：重建表并把启用时刻的底表固化为
    seq=1 的基线导入事件；启用前已存在的送审号 price_seq 回填为该基线序号，
    使其重开时按“功能启用时查到的序列”重现。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prices)").fetchall()}
    if "price_id" in cols:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE prices RENAME TO prices_legacy")
    conn.execute(
        "CREATE TABLE prices ("
        " price_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " trade_date TEXT NOT NULL,"
        " close_cents INTEGER NOT NULL CHECK (close_cents >= 0),"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (trade_date))"
    )
    legacy = conn.execute(
        "SELECT trade_date, close_cents, created_at FROM prices_legacy"
        " ORDER BY trade_date"
    ).fetchall()
    for r in legacy:
        conn.execute(
            "INSERT INTO prices(trade_date, close_cents, created_at)"
            " VALUES (?,?,?)",
            (r["trade_date"], r["close_cents"], r["created_at"]),
        )
    conn.execute("DROP TABLE prices_legacy")
    conn.execute("PRAGMA foreign_keys=ON")

    # 旧版 submissions 没有 price_seq 列：重建并保留原送审行
    sub_cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(submissions)").fetchall()}
    if sub_cols and "price_seq" not in sub_cols:
        conn.execute("ALTER TABLE submissions RENAME TO submissions_legacy")
        conn.execute(
            "CREATE TABLE submissions ("
            " submission_id TEXT PRIMARY KEY,"
            " start_date TEXT NOT NULL, end_date TEXT NOT NULL,"
            " base_date TEXT NOT NULL, basis_hash TEXT NOT NULL,"
            " price_seq INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO submissions(submission_id, start_date, end_date,"
            " base_date, basis_hash, price_seq, created_at)"
            " SELECT submission_id, start_date, end_date, base_date, basis_hash,"
            " 0, created_at FROM submissions_legacy"
        )
        conn.execute("DROP TABLE submissions_legacy")

    if legacy:
        # 启用时刻基线：一条 import 事件承载全部既有行情
        conn.execute(
            "INSERT INTO price_events(kind, ref_no, payload)"
            " VALUES ('import', '__baseline__', ?)",
            (json.dumps({"baseline": True, "count": len(legacy)},
                        ensure_ascii=False),),
        )
        seq = conn.execute("SELECT MAX(seq) AS s FROM price_events").fetchone()["s"]
        for r in conn.execute(
            "SELECT price_id, trade_date FROM prices ORDER BY trade_date"
        ).fetchall():
            conn.execute(
                "INSERT INTO price_event_items(seq, price_id, from_date, to_date)"
                " VALUES (?,?,?,?)",
                (seq, r["price_id"], "", r["trade_date"]),
            )
        # 启用功能前发出的送审号：重开以启用时查到的序列为准
        conn.execute(
            "UPDATE submissions SET price_seq=? WHERE price_seq IS NULL"
            " OR price_seq < ?",
            (seq, seq),
        )


def init_db(db_path: str | None = None) -> None:
    with db_cursor(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_prices(conn)
        # 旧库可能存在 price_seq 为 NULL 的送审行（理论上迁移已处理，再兜底一次）
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM price_events").fetchone()
        conn.execute(
            "UPDATE submissions SET price_seq=? WHERE price_seq IS NULL",
            (row["s"],),
        )
