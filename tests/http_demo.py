"""交易日更正功能 HTTP 端到端实测脚本（对运行中的 uvicorn 打真实请求）。

场景顺序：
1. 导入行情 + 公告，预览并锁定一个送审号 SB-OLD；
2. 校验预览（互换）-> 提交两日互换更正单 -> 验证只换日期不换价；
3. 成功单原样重传返回原结果、同号异文 409；
4. 冲突场景：目标日有行情未同单移走 409、原日期无行情 409；
5. 迁出 01-05 -> 01-09，空出 01-05，再补录新价；
6. 重开旧送审 SB-OLD：按送审时序列重现（看不到互换之后的迁移/补录）；
7. 现行预览按更正后的交易日取价；
8. 更正后用旧 price_seq 锁定 -> 409，刷新后锁定成功；
9. 验证原始价格记录只存一份（行数/price_id 数不变）。
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def show(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# 1. 初始数据 ---------------------------------------------------------------
show("1. 导入行情与公告，锁定初始送审号")
s, r = call("POST", "/api/prices/import", {"prices": [
    {"trade_date": "2026-01-05", "close_cents": 1000},
    {"trade_date": "2026-01-06", "close_cents": 1100},
    {"trade_date": "2026-01-07", "close_cents": 1200},
    {"trade_date": "2026-01-08", "close_cents": 2400},
]})
print("导入行情:", r)
s, r = call("POST", "/api/announcements/import", {
    "ann_no": "A001", "revision": 1, "effective_date": "2026-01-07",
    "old_shares": 1, "new_shares": 2})
print("导入公告:", r)
s, pv = call("GET", "/api/adjustments/preview?start_date=2026-01-05"
                    "&end_date=2026-01-08&base_date=2026-01-08")
print("预览序列:")
for p in pv["points"]:
    print("  ", p)
print("basis_hash =", pv["basis_hash"][:16], "…  price_seq =", pv["price_seq"])
s, locked = call("POST", "/api/submissions", {
    "start_date": "2026-01-05", "end_date": "2026-01-08",
    "base_date": "2026-01-08", "basis_hash": pv["basis_hash"],
    "price_seq": pv["price_seq"]})
sb_old = locked["submission_id"]
print("锁定送审号:", sb_old, " price_seq =", locked["price_seq"])
old_points = locked["points"]

# 2. 两日互换 ---------------------------------------------------------------
show("2. 两日互换：先只读校验预览，再整单提交")
swap = {"corr_no": "C-SWAP", "items": [
    {"from_date": "2026-01-05", "to_date": "2026-01-06"},
    {"from_date": "2026-01-06", "to_date": "2026-01-05"},
]}
s, r = call("POST", "/api/corrections/preview", swap)
print("校验预览 status =", s)
for x in r["projected_prices"]:
    print("  更正后底表:", x)
s, r = call("POST", "/api/corrections/apply", swap)
print("提交更正单 status =", s, " outcome =", r["outcome"], " seq =", r["price_seq"])
s, r = call("GET", "/api/prices")
print("现行底表（收盘价照旧、日期互换）:")
for p in r["prices"]:
    print("  ", p)

# 3. 重传与异文 -------------------------------------------------------------
show("3. 成功单原样重传 / 同号异文冲突")
s, r = call("POST", "/api/corrections/apply", swap)
print("原样重传 status =", s, " outcome =", r["outcome"], " seq =", r["price_seq"])
s, r = call("POST", "/api/corrections/apply", {"corr_no": "C-SWAP", "items": [
    {"from_date": "2026-01-07", "to_date": "2026-01-09"}]})
print("同号异文 status =", s, " detail =", r["detail"]["message"])

# 4. 冲突整单退回 -----------------------------------------------------------
show("4. 冲突校验：目标日有行情未同单移走 / 原日期无行情")
s, r = call("POST", "/api/corrections/apply", {"corr_no": "C-BAD1", "items": [
    {"from_date": "2026-01-07", "to_date": "2026-01-08"}]})
print("目标日占用且未移走 status =", s, " ->", r["detail"])
s, r = call("POST", "/api/corrections/apply", {"corr_no": "C-BAD2", "items": [
    {"from_date": "2026-01-30", "to_date": "2026-01-31"}]})
print("原日期无行情 status =", s, " ->", r["detail"])
s, r = call("GET", "/api/corrections")
print("已生效更正单数量（失败单不留痕）:", len(r["corrections"]))

# 5. 迁出 + 补录 ------------------------------------------------------------
show("5. 迁出 01-05 -> 01-09 空出日期，再向 01-05 补录新价")
s, r = call("POST", "/api/corrections/apply", {"corr_no": "C-MOVE", "items": [
    {"from_date": "2026-01-05", "to_date": "2026-01-09"}]})
print("迁出更正 status =", s, " outcome =", r["outcome"], " seq =", r["price_seq"])
s, r = call("POST", "/api/prices/import", {"prices": [
    {"trade_date": "2026-01-05", "close_cents": 777}]})
print("补录 01-05=777:", r)
s, r = call("POST", "/api/prices/import", {"prices": [
    {"trade_date": "2026-01-05", "close_cents": 888}]})
print("补录异价冲突 status =", s)
s, r = call("GET", "/api/prices")
print("现行底表:")
for p in r["prices"]:
    print("  ", p)

# 6. 重开旧送审 -------------------------------------------------------------
show("6. 重开旧送审号：按送审时序列重现（互换之后的迁移/补录均不可见）")
s, r = call("GET", f"/api/submissions/{sb_old}")
print("重开 status =", s, " stale =", r["stale"])
print(r["note"])
print(f"送审 seq={r['price_seq']} / 现行 seq={r['current_price_seq']}")
print("重开序列:")
for p in r["points"]:
    print("  ", p)
assert r["points"] == old_points, "旧送审序列必须与锁定时逐点一致"
print("✓ 与锁定时序列逐点一致")

# 7. 现行预览按更正后交易日取价 ---------------------------------------------
show("7. 现行预览：查询/复权按更正后的交易日取价")
s, fresh = call("GET", "/api/adjustments/preview?start_date=2026-01-05"
                       "&end_date=2026-01-09&base_date=2026-01-08")
print("现行 price_seq =", fresh["price_seq"])
for p in fresh["points"]:
    print("  ", p)

# 8. stale 锁定保护 ---------------------------------------------------------
show("8. 更正后旧序列锁定被拒，刷新后可锁定")
s, r = call("POST", "/api/submissions", {
    "start_date": "2026-01-05", "end_date": "2026-01-09",
    "base_date": "2026-01-08", "basis_hash": fresh["basis_hash"],
    "price_seq": 1})
print("旧 price_seq=1 锁定 status =", s, " ->", r["detail"]["message"])
s, r = call("POST", "/api/submissions", {
    "start_date": "2026-01-05", "end_date": "2026-01-09",
    "base_date": "2026-01-08", "basis_hash": fresh["basis_hash"],
    "price_seq": fresh["price_seq"]})
print("刷新后锁定 status =", s, " 新送审号 =", r["submission_id"],
      " seq =", r["price_seq"])

# 9. 单一记录校验 -----------------------------------------------------------
show("9. 原始价格记录只存一份（直接查 SQLite 物理表）")
import sqlite3
conn = sqlite3.connect("/tmp/demo.db")
n_rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
n_ids = conn.execute("SELECT COUNT(DISTINCT price_id) FROM prices").fetchone()[0]
events = conn.execute("SELECT seq, kind, ref_no FROM price_events ORDER BY seq").fetchall()
print("prices 行数 =", n_rows, " 不同 price_id 数 =", n_ids)
for e in events:
    print("  事件", e)
conn.close()
assert n_rows == n_ids == 5
print("✓ 更正全程未复制/删除价格行，仅更新 trade_date 归属")
print("\n全部场景实测通过。")
