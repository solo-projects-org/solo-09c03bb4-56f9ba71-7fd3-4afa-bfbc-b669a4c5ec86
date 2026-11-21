"""端到端自测：覆盖基准日前后折算、拆/并股、四舍五入、同版重送幂等、
异文退回、公告更正后凭送审号重现、重启持久化。"""
from __future__ import annotations

import importlib
import os
from fractions import Fraction

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


# ---------------------------------------------------------- 纯函数：因子与舍入

def test_factor_split_before_and_after_base_date(client):
    from app.services import factor_for_date, round_half_up
    events = [{"effective_date": "2026-01-10", "old_shares": 1, "new_shares": 2}]
    # 1:2 拆股，基准日在生效日之后
    assert factor_for_date("2026-01-05", "2026-01-15", events) == Fraction(1, 2)
    assert factor_for_date("2026-01-15", "2026-01-15", events) == 1
    # 基准日在生效日之前：基准日之后的价格需反向放大
    assert factor_for_date("2026-01-15", "2026-01-05", events) == 2
    # 同日的对称调用应互为倒数
    assert factor_for_date("2026-01-05", "2026-01-15", events) \
        * factor_for_date("2026-01-15", "2026-01-05", events) == 1


def test_factor_consolidation(client):
    from app.services import factor_for_date
    # 并股 3:1，生效日介于 d 与 b 之间
    events = [{"effective_date": "2026-01-10", "old_shares": 3, "new_shares": 1}]
    assert factor_for_date("2026-01-05", "2026-01-15", events) == Fraction(3, 1)
    assert factor_for_date("2026-01-15", "2026-01-05", events) == Fraction(1, 3)


def test_round_half_up_to_cents(client):
    from app.services import round_half_up
    assert round_half_up(Fraction(100, 3)) == 33       # 33.33 舍
    assert round_half_up(Fraction(5, 2)) == 3          # 2.5 入
    assert round_half_up(Fraction(7, 2)) == 4          # 3.5 入
    assert round_half_up(Fraction(1001, 1)) == 1001


def test_relevant_events_boundary(client):
    from app.services import relevant_events
    anns = [
        {"ann_no": "A", "revision": 1, "effective_date": "2026-01-01",
         "old_shares": 1, "new_shares": 2},
        {"ann_no": "B", "revision": 1, "effective_date": "2026-01-10",
         "old_shares": 1, "new_shares": 2},
        {"ann_no": "C", "revision": 1, "effective_date": "2026-01-31",
         "old_shares": 1, "new_shares": 2},
    ]
    rel = relevant_events("2026-01-05", "2026-01-20", "2026-01-15", anns)
    assert [a["ann_no"] for a in rel] == ["B"]  # 早于区间起与晚于 max(end,base) 的不留底


# ---------------------------------------------------------- 端到端：预览折算

def test_preview_split_across_base_date(client):
    c, _ = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    r = _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
             old_shares=1, new_shares=2)
    assert r.status_code == 200 and r.json()["outcome"] == "inserted"

    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"})
    assert resp.status_code == 200
    data = resp.json()
    by_date = {p["trade_date"]: p["adjusted_cents"] for p in data["points"]}
    # 生效日前按 1/2 折算，基准日当天不变
    assert by_date == {
        "2026-01-05": 500, "2026-01-08": 500,
        "2026-01-12": 2000, "2026-01-15": 2000,
    }
    assert data["stale"] is False
    assert len(data["basis"]) == 1


def test_preview_base_date_before_effective_date(client):
    """基准日在生效日之前（旧口径）：之后的新口径行情折算回基准日口径。"""
    c, _ = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-05"})
    by_date = {p["trade_date"]: p["adjusted_cents"]
               for p in resp.json()["points"]}
    # 基准日（旧口径）不变；生效日后为新口径报价，1 旧股=2 新股，
    # 折回旧口径需乘以 r=2：2000 新价 -> 4000
    assert by_date == {
        "2026-01-05": 1000, "2026-01-08": 1000,
        "2026-01-12": 4000, "2026-01-15": 4000,
    }


def test_rounding_half_up_via_api(client):
    """2:3 拆股、基准日在生效日后（新口径），生效日前旧价乘 old/new=2/3：
    150 分 -> 100（整），151 分 -> 100.666… -> 101（四舍五入）。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 150), ("2026-01-06", 151),
                       ("2026-01-12", 100)])
    _ann(c, ann_no="X", revision=1, effective_date="2026-01-10",
         old_shares=2, new_shares=3)
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"})
    by_date = {p["trade_date"]: p["adjusted_cents"]
               for p in resp.json()["points"]}
    assert by_date["2026-01-05"] == 100
    assert by_date["2026-01-06"] == 101
    assert by_date["2026-01-12"] == 100


# ---------------------------------------------------------- 公告收发规则

def test_announcement_resend_rules(client):
    c, _ = client
    a1 = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
              old_shares=1, new_shares=2)
    assert a1.json()["outcome"] == "inserted"
    # 同号同版原样重送 -> reused 沿用
    same = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
                old_shares=1, new_shares=2)
    assert same.status_code == 200 and same.json()["outcome"] == "reused"
    # 同号同版异文 -> 409 退回
    diff = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-11",
                old_shares=1, new_shares=2)
    assert diff.status_code == 409
    # 低修订 -> 422 退回
    old = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
               old_shares=1, new_shares=3)
    assert old.status_code == 409  # 同版异文优先判定
    # 更高修订 -> updated 更正，现行只留最高修订
    upd = _ann(c, ann_no="A1", revision=2, effective_date="2026-01-10",
               old_shares=1, new_shares=3)
    assert upd.status_code == 200 and upd.json()["outcome"] == "updated"
    anns = c.get("/api/announcements").json()["announcements"]
    assert len(anns) == 1 and anns[0]["revision"] == 2 and anns[0]["new_shares"] == 3
    low = _ann(c, ann_no="A1", revision=1, effective_date="2026-01-10",
               old_shares=1, new_shares=2)
    assert low.status_code == 422


def test_price_import_idempotent_and_conflict(client):
    c, _ = client
    r1 = _import_prices(c, [("2026-01-05", 1000), ("2026-01-06", 1010)])
    assert r1["inserted"] == 2
    r2 = _import_prices(c, [("2026-01-05", 1000)])  # 同价重复
    assert r2["inserted"] == 0 and r2["skipped_identical"] == 1
    resp = c.post("/api/prices/import", json={
        "prices": [{"trade_date": "2026-01-05", "close_cents": 999}]})
    assert resp.status_code == 409  # 异价冲突整批退回
    # 底表未被改动
    got = c.get("/api/prices").json()["prices"]
    assert {p["trade_date"]: p["close_cents"] for p in got} == {
        "2026-01-05": 1000, "2026-01-06": 1010}


# ---------------------------------------------------------- 锁定、更正重开、重启

def test_lock_then_correction_then_reopen_reproduces(client):
    c, db_path = client
    _import_prices(c, [
        ("2026-01-05", 1000), ("2026-01-08", 1000),
        ("2026-01-12", 2000), ("2026-01-15", 2000),
    ])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"}).json()

    locked = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": pv["basis_hash"]})
    assert locked.status_code == 200
    sub_id = locked.json()["submission_id"]
    original_points = locked.json()["points"]
    original_hash = locked.json()["basis_hash"]

    # 用旧指纹锁定 -> 依据已不匹配前先做一次更正
    _ann(c, ann_no="A001", revision=2, effective_date="2026-01-10",
         old_shares=1, new_shares=4)
    stale_lock = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": original_hash})
    assert stale_lock.status_code == 409  # 现行公告与预览所据不同，须刷新

    # 预览自报 stale
    stale_pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15", "basis_hash": original_hash}).json()
    assert stale_pv["stale"] is True and stale_pv["note"]

    # 凭送审号重开：仍按旧依据重现原序列
    reopened = c.get(f"/api/submissions/{sub_id}").json()
    assert reopened["stale"] is True
    assert reopened["points"] == original_points
    assert reopened["basis_hash"] == original_hash
    assert reopened["current_basis_hash"] != original_hash

    # 现行口径下序列确实变了（1:4 后生效日前折算为 250）
    fresh = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-15",
        "base_date": "2026-01-15"}).json()
    assert fresh["points"][0]["adjusted_cents"] == 250


def test_reopen_survives_restart(client, monkeypatch):
    c, db_path = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-12", 2000)])
    _ann(c, ann_no="A001", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)
    pv = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12"}).json()
    sub_id = c.post("/api/submissions", json={
        "start_date": "2026-01-05", "end_date": "2026-01-12",
        "base_date": "2026-01-12", "basis_hash": pv["basis_hash"]}).json()["submission_id"]

    # 模拟“重启”：重新加载模块、重新建连接（同一 SQLite 文件）
    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c2:
        reopened = c2.get(f"/api/submissions/{sub_id}")
        assert reopened.status_code == 200
        points = reopened.json()["points"]
        assert points[0]["adjusted_cents"] == 500
        assert points[1]["adjusted_cents"] == 2000
        # 行情底表仍在
        prices = c2.get("/api/prices").json()["prices"]
        assert len(prices) == 2


def test_chained_events_accumulate(client):
    """区间内多次拆并股：因子按生效顺序连乘，基准日取最后口径。"""
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000), ("2026-01-15", 1000),
                       ("2026-01-25", 1000)])
    _ann(c, ann_no="A", revision=1, effective_date="2026-01-10",
         old_shares=1, new_shares=2)   # 1:2
    _ann(c, ann_no="B", revision=1, effective_date="2026-01-20",
         old_shares=4, new_shares=1)   # 4:1 并股
    resp = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-25",
        "base_date": "2026-01-25"}).json()
    by_date = {p["trade_date"]: p for p in resp["points"]}
    # 首日对两次事件都处旧口径：1/2 * 4 = 2
    assert by_date["2026-01-05"]["factor"] == "2"
    assert by_date["2026-01-05"]["adjusted_cents"] == 2000
    # 中间日仅对并股处旧口径：4
    assert by_date["2026-01-15"]["factor"] == "4"
    assert by_date["2026-01-15"]["adjusted_cents"] == 4000
    # 基准日与两端同口径：1
    assert by_date["2026-01-25"]["factor"] == "1"
    assert len(resp["basis"]) == 2


def test_validation_errors(client):
    c, _ = client
    _import_prices(c, [("2026-01-05", 1000)])
    # 基准日不在底表
    r = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-05", "end_date": "2026-01-20",
        "base_date": "2026-01-20"})
    assert r.status_code == 422
    # 基准日越出区间
    r = c.get("/api/adjustments/preview", params={
        "start_date": "2026-01-06", "end_date": "2026-01-20",
        "base_date": "2026-01-05"})
    assert r.status_code == 422
    # 不存在的送审号
    assert c.get("/api/submissions/SB-NOPE").status_code == 404
    # 非法日期格式
    bad = c.post("/api/prices/import",
                 json={"prices": [{"trade_date": "2026/01/05", "close_cents": 1}]})
    assert bad.status_code == 422
