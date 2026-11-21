"""SQLite 连接与建表。

表结构：
- prices           单股日收盘价底表（单位：分），一次导入后只增不改不删，
                   送审与更正均共用此底表，不复制行情。
- announcements    公告现行版本（每号一行，只保留最高修订）。
- announcements_log 公告收发流水，用于审计与“原样重送沿用 / 异文退回”。
- submissions      锁定送审记录（区间、基准日、依据指纹）。
- submission_basis 每次送审重算所需的公告依据快照（只留依据，不留行情副本）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

# 测试可通过环境变量改库；容器内固定 /data/app.db
DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "/data/app.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS prices (
    trade_date   TEXT NOT NULL,
    close_cents  INTEGER NOT NULL CHECK (close_cents >= 0),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (trade_date)
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


def init_db(db_path: str | None = None) -> None:
    with db_cursor(db_path) as conn:
        conn.executescript(SCHEMA)
