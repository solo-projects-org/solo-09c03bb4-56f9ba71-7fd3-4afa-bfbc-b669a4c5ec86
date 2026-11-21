# 拆并股收盘价折算（FastAPI + SQLite）

研究员复盘拆并股前后走势时，统一收盘价的股份口径。单股日收盘价（分）与拆并股公告
一次导入 SQLite；选区间和基准交易日，系统按基准日口径折算每日收盘价，四舍五入到分；
可锁定预览取得送审号，公告更正或进程重启后凭送审号仍能重现原序列。

## 折算口径

公告：生效日起 `old_shares` 股换成 `new_shares` 股，记 `r = new/old`，价值守恒
`P_old = r × P_new`。对交易日 d 与基准日 b：

| d 口径 | b 口径 | 因子 |
|---|---|---|
| 旧（d < 生效日） | 新（b ≥ 生效日） | `1/r = old/new` |
| 新 | 旧 | `r = new/old` |
| 同口径 | 同口径 | `1` |

多个公告按生效日连乘；全程 `Fraction` 精确计算，结果按“四舍五入”（小数部分 ≥ 0.5 进位）
取整到分，不使用浮点。

## 业务规则

- **行情底表** `prices`：按交易日唯一；同日期同价重复导入跳过，异价整批退回（409），
  底表只增不改不删，更正与送审共用，不复制行情。
- **公告** `announcements`：每号只保留最高修订（表中即现行版本），全部收发留审计流水
  `announcements_log`：
  - 同号同版原样重送 → `reused` 沿用原公告；
  - 同号同版异文 → 409 退回（`rejected_diff`）；
  - 更高修订 → `updated` 更正现行版本；
  - 低于现行修订 → 422 退回（`rejected_old`）。
- **预览** `GET /api/adjustments/preview`：返回折算序列、参与折算的公告依据与依据指纹
  `basis_hash`；带旧指纹调用时返回 `stale=true`，提示须刷新。
- **锁定送审** `POST /api/submissions`：指纹与现行公告不一致时 409 拒绝；成功后生成
  `SB-xxxx` 送审号，并在 `submission_basis` 只留重算所需的公告快照（生效日不晚于区间起、
  或晚于区间止/基准日的公告对相对因子恒为 1，不留底）。
- **凭号重开** `GET /api/submissions/{id}`：快照依据 + 同一行情底表重算，公告更正或重启后
  序列与锁定时逐点一致；同时回现行指纹与 `stale` 标记供比对。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/prices/import` | 批量导入日收盘价（分） |
| GET | `/api/prices` | 查询底表（可选 start_date/end_date） |
| POST | `/api/announcements/import` | 导入/重送/更正公告 |
| GET | `/api/announcements` | 现行公告（每号最高修订） |
| GET | `/api/adjustments/preview` | 区间折算预览（参数 start_date, end_date, base_date, 可选 basis_hash） |
| POST | `/api/submissions` | 锁定预览，取得送审号 |
| GET | `/api/submissions/{id}` | 凭送审号重开，重现原序列 |
| GET | `/api/health` | 健康检查 |
| GET | `/` | 网页（导入、预览、锁定、重开四步操作） |

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 库文件持久化在宿主机 `./data/app.db`（compose 已挂载卷）。

## 本地无 Docker 时运行

```bash
pip install -r requirements-dev.txt
export APP_DB_PATH=./data/app.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest -q
```

## 目录结构

```
app/
  db.py         # SQLite 连接与建表（5 张表）
  models.py     # Pydantic 请求/响应模型
  services.py   # 折算因子、四舍五入、依据指纹、序列重算（纯函数）
  main.py       # FastAPI 路由
  static/index.html  # 单页前端
tests/          # 13 项 pytest 端到端自测
Dockerfile / docker-compose.yml / requirements*.txt
```

## 自测覆盖

`tests/test_adjustments.py`（13 项，全部通过）：

1. 拆股/并股在基准日前、后的因子方向与倒数对称性；
2. 四舍五入到分（含 0.5 进位）；
3. 依据快照边界筛选（早于区间起、晚于 max(区间止,基准日) 不留底）；
4. API 端到端：基准日在生效日后/前的完整折算序列；
5. 多次拆并股连乘；
6. 公告同版原样重送沿用、异文退回、低修订退回、更高修订更正；
7. 行情重复导入跳过、异价冲突整批退回且底表不变；
8. 锁定后公告更正：旧指纹锁定 409、预览 stale、凭送审号重开逐点重现；
9. 模拟进程重启后凭送审号重开；
10. 参数校验（基准日不在底表、越出区间、送审号不存在、日期格式）。

## 已知遗留/约束

- 单股模型：底表未设股票代码，多股场景需扩展表结构与接口字段。
- 无鉴权/用户体系，送审号为不可猜随机号（UUID 前 16 位），仅适用于内网可信环境。
- 公告生效日按自然日比较；若生效日非交易日，按题意仍自该日起生效（首个晚于它的
  交易日适用新口径），未额外校验公告日历。
- 容器运行时在当前环境不可用，未实跑 `docker compose build`，仅做了 compose 配置静态
  校验与等效的 uvicorn 实进程冒烟。
