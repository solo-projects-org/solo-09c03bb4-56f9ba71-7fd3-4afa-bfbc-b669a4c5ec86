"""交易日更正功能端到端自测。

覆盖：
- 纯函数：对应表规范化/校验、置换映射、事件重放；
- 更正整单生效、只改日期归属价格不变、两日互换、闭合链；
- 原日期无行情、目标日有行情但未同单移走、重复原/目标日期等冲突整单回滚；
- 成功单原样重传返回原结果、同号异文 409；
- 预览 stale（更正后旧序列须刷新才能送审）；
- 送审按送审时序列重开：迁出后补录、重启后重开旧送审；
- 启用功能前的旧送审号重开以启用时序列为准（旧库迁移）；
- 补录与更正交错的先提交成功判定；
- 原始价格记录只存一份（price_id 不变、行数不增）。
"""
from __future__ import annotations

import importlib
import os
import sqlite3

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    import app.db as db
    importlib.reload(db)
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c, db_path


def _import_prices(c, rows):
    resp = c.post("/api/prices/import", json={
        "prices": [{"trade_date": d, "close_cents": p} for d, p in rows]
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ann(c, **kw):
    return c.post("/api/announcements/import", json=kw)


def _correct(c, no, items, expected=200):
    resp = c.post("/api/corrections/apply", json={"corr_no": no, "items": items})
    assert resp.status_code == expected, resp.text
    return resp.json() if expected == 200 else resp


def _prices_map(c):
    rows = c.get("/api/prices").json()["prices"]
    return {p["trade_date"]: p["close_cents"] for p in rows}, rows


# ---------------------------------------------------------- 纯函数

def test_normalize_and_validate_correction():
    from app.services import (
        CorrectionError, normalize_correction, validate_correction,
        correction_hash_of, apply_mapping,
    )
    items = [{"from_date": "2026-01-05", "to_date": "2026-01-07"},
             {"from_date": "2026-01-07", "to_date": "2026-01-05"}]
    norm = normalize_correction(items)
    assert [i["from_date"] for i in norm] == ["2026-01-05", "2026-01-07"]
    # 互换在底表上合法（目标日有行情但同单移走）
    validate_correction(norm, {"2026-01-05", "2026-01-07"})

    with pytest.raises(CorrectionError):
        normalize_correction([{"from_date": "2026-01-05", "to_date": "2026-01-05"}])
    with pytest.raises(CorrectionError):
        normalize_correction([
            {"from_date": "2026-01-05", "to_date": "2026-01-08"},
            {"from_date": "2026-01-05", "to_date": "2026-01-09"},
        ])
    with pytest.raises(CorrectionError):
        normalize_correction([
            {"from_date": "2026-01-05", "to_date": "2026-01-08"},
            {"from_date": "2026-01-06", "to_date": "2026-01-08"},
        ])
    # 原日期无行情
    with pytest.raises(CorrectionError):
        validate_correction(norm, {"2026-01-05"})
    # 目标日有行情但未同单移走
    with pytest.raises(CorrectionError):
        validate_correction(
            [{"from_date": "2026-01-05", "to_date": "2026-01-07"}],
            {"2026-01-05", "2026-01-07"},
        )
    # 哈希对条目顺序不敏感（同一张表）
    h1 = correction_hash_of(items)
    h2 = correction_hash_of(list(reversed(items)))
    assert h1 == h2

    # 置换映射纯函数
    dates = {1: "2026-01-05", 2: "2026-01-07"}
    moved = apply_mapping(dates, norm)
    assert moved == {1: "2026-01-07", 2: "2026-01-05"}


def test_replay_price_events():
    from app.services import replay_price_events
    # 导入 seq1 两行；更正 seq2 互换
    items = [
        {"seq": 1, "price_id": 10, "from_date": "", "to_date": "2026-01-05"},
        {"seq": 1, "price_id": 11, "from_date": "", "to_date": "2026-01-07"},
        {"seq": 2, "price_id": None, "from_date": "2026-01-05", "to_date": "2026-01-07"},
        {"seq": 2, "price_id": None, "from_date": "2026-01-07", "to_date": "2026-01-05"},
    ]
    owner1 = replay_price_events([i for i in items if i["seq"] <= 1])
    assert owner1 == {"2026-01-05": 10, "2026-01-07": 11}
    owner2 = replay_price_events(items)
    assert owner2 == {"2026-01-05": 11, "2026-01-07": 10}


# ---------------------------------------------------------- 更正生效与互换

def test_correction_moves_dates_keeps_prices_and_single_copy(client):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-06", 1010),
                       ("2026-01-07", 1020)])
    # 06 -> 08（08 为空日期）
    r = _correct(c, "C1", [{"from_date": "2026-01-06", "to_date": "2026-01-08"}])
    assert r["outcome"] == "applied" and r["price_seq"] == 2
    m, rows = _prices_map(c)
    assert m == {"2026-01-05": 1000, "2026-01-07": 1020, "2026-01-08": 1010}
    # 原始价格记录只存一份：行数不变、无重复
    conn = sqlite3.connect(db_path)
    n_rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    n_ids = conn.execute("SELECT COUNT(DISTINCT price_id) FROM prices").fetchone()[0]
    n_corr_price_rows = conn.execute(
        "SELECT COUNT(*) FROM price_event_items WHERE price_id IS NULL").fetchone()[0]
    conn.close()
    assert n_rows == 3 and n_ids == 3 and n_corr_price_rows == 1


def test_two_day_swap(client):
    """题目要求实测：两日互换。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-07", 2000)])
    r = _correct(c, "SWAP", [
        {"from_date": "2026-01-05", "to_date": "2026-01-07"},
        {"from_date": "2026-01-07", "to_date": "2026-01-05"},
    ])
    assert r["outcome"] == "applied"
    m, _ = _prices_map(c)
    # 收盘价照旧，只交换日期归属
    assert m == {"2026-01-05": 2000, "2026-01-07": 1000}
    # 再次互换（新单号）应能还原
    r2 = _correct(c, "SWAP2", [
        {"from_date": "2026-01-05", "to_date": "2026-01-07"},
        {"from_date": "2026-01-07", "to_date": "2026-01-05"},
    ])
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 1000, "2026-01-07": 2000}
    # 导入 seq1，两次更正各一个事件 seq2、seq3
    assert r2["price_seq"] == 3


def test_three_day_cycle(client):
    """闭合链 A->B, B->C, C->A。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 100), ("2026-01-06", 200),
                       ("2026-01-07", 300)])
    _correct(c, "CYCLE", [
        {"from_date": "2026-01-05", "to_date": "2026-01-06"},
        {"from_date": "2026-01-06", "to_date": "2026-01-07"},
        {"from_date": "2026-01-07", "to_date": "2026-01-05"},
    ])
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 300, "2026-01-06": 100, "2026-01-07": 200}


def test_conflict_cases_rollback(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-07", 2000)])
    # 原日期无行情 -> 409，底表不变
    r = c.post("/api/corrections/apply", json={"corr_no": "X1", "items": [
        {"from_date": "2026-01-06", "to_date": "2026-01-08"}]})
    assert r.status_code == 409
    # 目标日已有行情但未同单移走 -> 409
    r = c.post("/api/corrections/apply", json={"corr_no": "X2", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-07"}]})
    assert r.status_code == 409
    # 结构错误 -> 422（同日期重复、from==to）
    r = c.post("/api/corrections/apply", json={"corr_no": "X3", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-05"}]})
    assert r.status_code == 422
    r = c.post("/api/corrections/apply", json={"corr_no": "X4", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-08"},
        {"from_date": "2026-01-05", "to_date": "2026-01-09"}]})
    assert r.status_code == 422
    # 失败后底表保持原归属
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 1000, "2026-01-07": 2000}
    # 失败单号不留痕，可用同一单号重新提交成功单
    ok = _correct(c, "X1", [{"from_date": "2026-01-05", "to_date": "2026-01-08"}])
    assert ok["outcome"] == "applied"


def test_resend_same_returns_original_diff_conflicts(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-06", 1010)])
    items = [{"from_date": "2026-01-05", "to_date": "2026-01-08"}]
    first = _correct(c, "R1", items)
    assert first["outcome"] == "applied" and first["price_seq"] == 2
    # 原样重传（即使原日期现在已空出）：返回原结果，不再次移动、不新增事件
    again = _correct(c, "R1", items)
    assert again["outcome"] == "reused"
    assert again["price_seq"] == 2 and again["items"] == items
    listing = c.get("/api/corrections").json()["corrections"]
    assert len(listing) == 1 and listing[0]["corr_no"] == "R1"
    # 同号异文 -> 409
    diff = c.post("/api/corrections/apply", json={"corr_no": "R1", "items": [
        {"from_date": "2026-01-06", "to_date": "2026-01-09"}]})
    assert diff.status_code == 409
    m, _ = _prices_map(c)
    assert m == {"2026-01-06": 1010, "2026-01-08": 1000}


def test_correction_preview_is_read_only(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-07", 2000)])
    pv = c.post("/api/corrections/preview", json={"corr_no": "P1", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-07"},
        {"from_date": "2026-01-07", "to_date": "2026-01-05"},
    ]})
    assert pv.status_code == 200 and pv.json()["valid"] is True
    assert pv.json()["projected_prices"] == [
        {"trade_date": "2026-01-05", "close_cents": 2000},
        {"trade_date": "2026-01-07", "close_cents": 1000},
    ]
    # 预览不写库
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 1000, "2026-01-07": 2000}
    # 非法预览给 409/422
    bad = c.post("/api/corrections/preview", json={"corr_no": "P2", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-07"}]})
    assert bad.status_code == 409


# -------------------------------------------------- 更正后预览 stale 与送审

def test_stale_preview_must_refresh_before_lock(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-08", 2000)])
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    assert pv["price_seq"] == 1 and pv["stale"] is False
    # 更正后，带旧 price_seq 的预览报 stale
    _correct(c, "C1", [{"from_date": "2026-01-05", "to_date": "2026-01-06"}])
    stale = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "price_seq": pv["price_seq"]}).json()
    assert stale["stale"] is True and "须刷新" in stale["note"]
    # 用旧序列锁定 -> 409
    lock = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": stale["basis_hash"],
        "price_seq": pv["price_seq"]})
    assert lock.status_code == 409
    # 刷新后（不带旧 seq）锁定成功
    fresh = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    assert fresh["stale"] is False and fresh["price_seq"] == 2
    ok = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": fresh["basis_hash"],
        "price_seq": fresh["price_seq"]})
    assert ok.status_code == 200 and ok.json()["price_seq"] == 2


def test_reopen_reproduces_series_at_lock_sequence(client):
    """更正后查询按新交易日取价；旧送审重开按送审时序列重现。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-08", 2000)])
    _ann(c, ann_no="A1", revision=1, effective_date="2026-01-07",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    locked = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": pv["basis_hash"],
        "price_seq": pv["price_seq"]}).json()
    sub_id = locked["submission_id"]
    original = locked["points"]
    # 把 01-05 的行情迁到 01-06（空出 01-05）
    _correct(c, "C1", [{"from_date": "2026-01-05", "to_date": "2026-01-06"}])
    # 现行预览：01-05 已无行情，01-06 有 1000
    current = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    cur_dates = [p["trade_date"] for p in current["points"]]
    assert cur_dates == ["2026-01-06", "2026-01-08"]
    # 旧送审重开：仍是送审时的 01-05
    reopened = c.get(f"/api/submissions/{sub_id}").json()
    assert reopened["stale"] is True
    assert reopened["price_seq"] == 1 and reopened["current_price_seq"] == 2
    assert reopened["points"] == original
    assert [p["trade_date"] for p in reopened["points"]] == ["2026-01-05", "2026-01-08"]


def test_move_out_then_backfill_then_reopen(client):
    """题目要求实测：迁出后补录，再重开旧送审。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-08", 2000)])
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    sub_id = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": pv["basis_hash"],
        "price_seq": pv["price_seq"]}).json()["submission_id"]
    original = c.get(f"/api/submissions/{sub_id}").json()["points"]

    # 迁出：01-05 -> 01-06，空出 01-05
    _correct(c, "C1", [{"from_date": "2026-01-05", "to_date": "2026-01-06"}])
    # 向空出的 01-05 补录新价
    backfill = _import_prices(c, [("2026-01-05", 777)])
    assert backfill["inserted"] == 1 and backfill["price_seq"] == 3
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 777, "2026-01-06": 1000, "2026-01-08": 2000}
    # 补录后向 01-05 异价导入仍按既有规则 409（先提交成功的结果为准）
    clash = c.post("/api/prices/import", json={
        "prices": [{"trade_date": "2026-01-05", "close_cents": 888}]})
    assert clash.status_code == 409
    # 旧送审重开：重现送审时序列（01-05 仍是旧价 1000，且没有 01-06 行）
    reopened = c.get(f"/api/submissions/{sub_id}").json()
    assert reopened["points"] == original
    assert {p["trade_date"]: p["close_cents"] for p in reopened["points"]} == {
        "2026-01-05": 1000, "2026-01-08": 2000}
    # 现行预览则包含补录价与迁入价
    fresh = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    assert {p["trade_date"]: p["close_cents"] for p in fresh["points"]} == {
        "2026-01-05": 777, "2026-01-06": 1000, "2026-01-08": 2000}


def test_reopen_survives_restart_with_corrections(client):
    """模拟重启：事件流持久化，重开结果不变。"""
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-08", 2000)])
    _ann(c, ann_no="A1", revision=1, effective_date="2026-01-07",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    sub_before = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": pv["basis_hash"],
        "price_seq": pv["price_seq"]}).json()["submission_id"]
    _correct(c, "C1", [{"from_date": "2026-01-05", "to_date": "2026-01-06"}])
    pv2 = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08"}).json()
    sub_after = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-08",
        "base_date": "2026-01-08", "basis_hash": pv2["basis_hash"],
        "price_seq": pv2["price_seq"]}).json()["submission_id"]

    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c2:
        r1 = c2.get(f"/api/submissions/{sub_before}").json()
        r2 = c2.get(f"/api/submissions/{sub_after}").json()
        assert [p["trade_date"] for p in r1["points"]] == ["2026-01-05", "2026-01-08"]
        assert [p["trade_date"] for p in r2["points"]] == ["2026-01-06", "2026-01-08"]
        # 折算值也逐点重现（生效日前 1000 -> 500）
        assert r1["points"][0]["adjusted_cents"] == 500
        assert r2["points"][0]["adjusted_cents"] == 500


def test_legacy_submission_reopens_at_enable_time_sequence(tmp_path):
    """启用功能前发出的送审号：迁移后重开以启用时查到的序列为准。"""
    db_path = tmp_path / "legacy.db"
    # 手工搭一个“旧版”库：旧结构 prices（无 price_id）+ 旧送审（无 price_seq 列）
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE prices (
        trade_date TEXT NOT NULL, close_cents INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (trade_date)
    );
    CREATE TABLE announcements (
        ann_no TEXT NOT NULL PRIMARY KEY, revision INTEGER NOT NULL,
        effective_date TEXT NOT NULL, old_shares INTEGER NOT NULL,
        new_shares INTEGER NOT NULL, body_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE announcements_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ann_no TEXT, revision INTEGER,
        effective_date TEXT, old_shares INTEGER, new_shares INTEGER,
        body_hash TEXT, outcome TEXT, note TEXT DEFAULT '',
        received_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE submissions (
        submission_id TEXT PRIMARY KEY, start_date TEXT NOT NULL,
        end_date TEXT NOT NULL, base_date TEXT NOT NULL, basis_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE submission_basis (
        submission_id TEXT NOT NULL, ann_no TEXT NOT NULL, revision INTEGER NOT NULL,
        effective_date TEXT NOT NULL, old_shares INTEGER NOT NULL,
        new_shares INTEGER NOT NULL, PRIMARY KEY (submission_id, ann_no)
    );
    """)
    conn.execute("INSERT INTO prices(trade_date, close_cents) VALUES"
                 " ('2026-01-05', 1000), ('2026-01-08', 2000)")
    conn.execute("INSERT INTO submissions VALUES"
                 " ('SB-LEGACY', '2026-01-05', '2026-01-08', '2026-01-08',"
                 " 'hash-legacy', datetime('now'))")
    conn.commit()
    conn.close()

    os.environ["APP_DB_PATH"] = str(db_path)
    import app.db as db
    importlib.reload(db)
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        # 迁移：启用基线 seq=1，旧送审回填 price_seq=1
        info = c.get("/api/prices").json()
        assert info["price_seq"] == 1
        # 启用后做一次更正（迁移之后的新事件 seq=2）
        r = c.post("/api/corrections/apply", json={"corr_no": "C1", "items": [
            {"from_date": "2026-01-05", "to_date": "2026-01-06"}]})
        assert r.status_code == 200
        reopened = c.get("/api/submissions/SB-LEGACY").json()
        # 旧送审按启用时序列重现：仍看到 01-05 的旧行情，而非迁移后的 01-06
        assert reopened["price_seq"] == 1
        assert [p["trade_date"] for p in reopened["points"]] == ["2026-01-05", "2026-01-08"]
        assert reopened["current_price_seq"] == 2 and reopened["stale"] is True


def test_backfill_and_correction_interleave_first_commit_wins(client):
    """补录与更正交错：先提交成功者占用日期，后到者按冲突规则失败且不留痕。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000)])
    # 先补录空日期 01-06 成功
    r1 = _import_prices(c, [("2026-01-06", 500)])
    assert r1["inserted"] == 1
    # 再想把 01-05 迁到已被占用的 01-06，且未同单移走 -> 409，归属不变
    clash = c.post("/api/corrections/apply", json={"corr_no": "C1", "items": [
        {"from_date": "2026-01-05", "to_date": "2026-01-06"}]})
    assert clash.status_code == 409
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 1000, "2026-01-06": 500}
    # 反过来：先更正迁出 01-05 到 01-07 空出 01-05，再补录 01-05 成功
    _correct(c, "C2", [{"from_date": "2026-01-05", "to_date": "2026-01-07"}])
    r2 = _import_prices(c, [("2026-01-05", 900)])
    assert r2["inserted"] == 1
    m, _ = _prices_map(c)
    assert m == {"2026-01-05": 900, "2026-01-06": 500, "2026-01-07": 1000}
