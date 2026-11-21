"""FastAPI 入口：行情/公告导入、交易日更正、折算预览、锁定送审、凭号重开。"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import db_cursor, init_db
from app.models import (
    AdjustedPoint,
    AnnouncementIn,
    AnnouncementResult,
    CorrectionIn,
    CorrectionItemIn,
    CorrectionResult,
    LockInRequest,
    LockInResult,
    PreviewResult,
    PriceImportResult,
    PriceIn,
    ReopenResult,
)
from app.services import (
    apply_mapping,
    basis_hash_of,
    body_hash_of,
    build_series,
    correction_hash_of,
    CorrectionError,
    normalize_correction,
    relevant_events,
    replay_price_events,
    validate_correction,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="拆并股收盘价折算（含交易日更正）", version="1.1.0", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _current_price_seq(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM price_events").fetchone()["s"]


# ---------------------------------------------------------------- 行情导入

class PriceBatch(BaseModel):
    prices: list[PriceIn] = Field(min_length=1)


@app.post("/api/prices/import", response_model=PriceImportResult)
def import_prices(batch: PriceBatch):
    """一次导入单股日收盘价（分）。

    同日期同价视为重复跳过；同日期不同价属冲突，整批退回（409），
    底表只增不改不删，送审与更正共用此底表。新写入的行情登记一条 import
    价格事件，取得新的序列序号；空出的日期可随时重新补录。
    """
    received = len(batch.prices)
    seen: dict[str, int] = {}
    for item in batch.prices:
        if item.trade_date in seen and seen[item.trade_date] != item.close_cents:
            raise HTTPException(
                status_code=422,
                detail=f"批次内同一交易日 {item.trade_date} 出现不同收盘价",
            )
        seen[item.trade_date] = item.close_cents

    conflicts: list[dict] = []
    with db_cursor() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for trade_date, close_cents in seen.items():
            row = conn.execute(
                "SELECT close_cents FROM prices WHERE trade_date=?", (trade_date,)
            ).fetchone()
            if row is not None and row["close_cents"] != close_cents:
                conflicts.append({
                    "trade_date": trade_date,
                    "existing_cents": row["close_cents"],
                    "submitted_cents": close_cents,
                })
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={"message": "底表已存在同日期不同收盘价，整批未写入",
                        "conflicts": conflicts},
            )
        inserted_rows: list[tuple[int, str]] = []
        for trade_date, close_cents in seen.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO prices(trade_date, close_cents) VALUES (?,?)",
                (trade_date, close_cents),
            )
            if cur.rowcount:
                inserted_rows.append((cur.lastrowid, trade_date))

        if inserted_rows:
            conn.execute(
                "INSERT INTO price_events(kind, ref_no, payload)"
                " VALUES ('import', '', ?)",
                (json.dumps({"count": len(inserted_rows)}, ensure_ascii=False),),
            )
            seq = conn.execute("SELECT last_insert_rowid() AS s").fetchone()["s"]
            for price_id, trade_date in inserted_rows:
                conn.execute(
                    "INSERT INTO price_event_items(seq, price_id, from_date, to_date)"
                    " VALUES (?,?,?,?)",
                    (seq, price_id, "", trade_date),
                )
        current_seq = _current_price_seq(conn)
    return PriceImportResult(
        received=received,
        inserted=len(inserted_rows),
        skipped_identical=received - len(inserted_rows),
        conflicts=conflicts,
        price_seq=current_seq,
    )


@app.get("/api/prices")
def list_prices(start_date: str | None = None, end_date: str | None = None):
    sql = "SELECT trade_date, close_cents FROM prices"
    where, params = [], []
    if start_date:
        where.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("trade_date <= ?")
        params.append(end_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date"
    with db_cursor() as conn:
        rows = conn.execute(sql, params).fetchall()
        seq = _current_price_seq(conn)
    return {"price_seq": seq, "prices": [dict(r) for r in rows]}


# ------------------------------------------------------------ 交易日更正

def _stored_correction(conn, corr_no: str) -> dict:
    head = conn.execute(
        "SELECT corr_no, body_hash, seq FROM corrections WHERE corr_no=?",
        (corr_no,),
    ).fetchone()
    rows = conn.execute(
        "SELECT from_date, to_date FROM correction_items WHERE corr_no=?"
        " ORDER BY line_no",
        (corr_no,),
    ).fetchall()
    return {"seq": head["seq"], "items": [dict(r) for r in rows]}


@app.post("/api/corrections/preview")
def correction_preview(req: CorrectionIn):
    """只读校验更正单并预览移动效果，不写库；送审前的预览须重新刷新。"""
    try:
        mapping = normalize_correction([i.model_dump() for i in req.items])
    except CorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with db_cursor() as conn:
        live = {r["trade_date"] for r in
                conn.execute("SELECT trade_date FROM prices").fetchall()}
        try:
            validate_correction(mapping, live)
        except CorrectionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        current = {r["price_id"]: r["trade_date"]
                   for r in conn.execute("SELECT price_id, trade_date FROM prices")}
        values = {r["trade_date"]: r["close_cents"]
                  for r in conn.execute("SELECT trade_date, close_cents FROM prices")}
        projected = apply_mapping(current, mapping)
    moves = [
        {"from_date": m["from_date"], "to_date": m["to_date"],
         "close_cents": values[m["from_date"]]}
        for m in mapping
    ]
    projected_table = sorted(
        ({"trade_date": d, "close_cents": values[current[pid]]}
         for pid, d in projected.items()),
        key=lambda x: x["trade_date"],
    )
    return {
        "corr_no": req.corr_no,
        "valid": True,
        "moves": moves,
        "projected_prices": projected_table,
        "note": "校验通过：整单生效，只改日期归属，收盘价不变",
    }


@app.post("/api/corrections/apply", response_model=CorrectionResult)
def apply_correction(req: CorrectionIn):
    """整单提交交易日更正。

    - 单号首次出现且校验通过：整单原子生效（两阶段换键，冲突/失败全量回滚）；
    - 成功单原样重传（正文哈希一致）：返回原结果（reused），不重复移动；
    - 同号异文：409 冲突，底表不动。
    """
    raw_items = [i.model_dump() for i in req.items]
    digest = correction_hash_of(raw_items)
    with db_cursor() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT body_hash, seq FROM corrections WHERE corr_no=?",
            (req.corr_no,),
        ).fetchone()
        if existing is not None:
            stored = _stored_correction(conn, req.corr_no)
            if existing["body_hash"] == digest:
                return CorrectionResult(
                    corr_no=req.corr_no, outcome="reused", price_seq=stored["seq"],
                    items=stored["items"],
                    note="成功单原样重传，沿用原更正结果（未再次写入）",
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"更正单号 {req.corr_no} 已存在且对应表不同，同号异文冲突",
                    "existing_items": stored["items"],
                },
            )

        try:
            mapping = normalize_correction(raw_items)
        except CorrectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        live = {r["trade_date"] for r in
                conn.execute("SELECT trade_date FROM prices").fetchall()}
        try:
            validate_correction(mapping, live)
        except CorrectionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # 两阶段换键：先挂到带前缀的临时日期，再统一落到目标日期，
        # 避免 UNIQUE(trade_date) 冲突；任一 UPDATE 行数不符即抛错回滚整单。
        temp_prefix = "\x00corr:"
        for m in mapping:
            cur = conn.execute(
                "UPDATE prices SET trade_date=? WHERE trade_date=?",
                (temp_prefix + m["to_date"], m["from_date"]),
            )
            if cur.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"写入失败：原交易日 {m['from_date']} 行情缺失，整单回滚",
                )
        for m in mapping:
            cur = conn.execute(
                "UPDATE prices SET trade_date=? WHERE trade_date=?",
                (m["to_date"], temp_prefix + m["to_date"]),
            )
            if cur.rowcount != 1:
                raise HTTPException(
                    status_code=500,
                    detail=f"写入失败：目标交易日 {m['to_date']} 落位失败，整单回滚",
                )

        conn.execute(
            "INSERT INTO price_events(kind, ref_no, payload) VALUES ('correction', ?, ?)",
            (req.corr_no, json.dumps({"items": mapping}, ensure_ascii=False)),
        )
        seq = conn.execute("SELECT last_insert_rowid() AS s").fetchone()["s"]
        for m in mapping:
            # 更正项不挂 price_id（同 seq 内不引用同一行的旧归属），
            # 重放时按 from_date->to_date 移动归属；seq 必须落事件序号
            conn.execute(
                "INSERT INTO price_event_items(seq, price_id, from_date, to_date)"
                " VALUES (?, NULL, ?, ?)",
                (seq, m["from_date"], m["to_date"]),
            )
        conn.execute(
            "INSERT INTO corrections(corr_no, body_hash, seq) VALUES (?,?,?)",
            (req.corr_no, digest, seq),
        )
        for line_no, m in enumerate(mapping, start=1):
            conn.execute(
                "INSERT INTO correction_items(corr_no, line_no, from_date, to_date)"
                " VALUES (?,?,?,?)",
                (req.corr_no, line_no, m["from_date"], m["to_date"]),
            )
    return CorrectionResult(
        corr_no=req.corr_no, outcome="applied", price_seq=seq, items=mapping,
        note="整单生效：仅调整日期归属，收盘价保持不变",
    )


@app.get("/api/corrections")
def list_corrections():
    with db_cursor() as conn:
        heads = conn.execute(
            "SELECT corr_no, seq FROM corrections ORDER BY corr_no"
        ).fetchall()
        orders = []
        for h in heads:
            orders.append({
                "corr_no": h["corr_no"],
                "price_seq": h["seq"],
                "items": [dict(r) for r in conn.execute(
                    "SELECT from_date, to_date FROM correction_items"
                    " WHERE corr_no=? ORDER BY line_no", (h["corr_no"],))],
            })
    return {"corrections": orders}


# ---------------------------------------------------------------- 公告导入

def _current_announcements(conn) -> list[dict]:
    """每号取最高修订（ann_no 为主键，表中即现行版本）。"""
    rows = conn.execute(
        "SELECT ann_no, revision, effective_date, old_shares, new_shares, body_hash "
        "FROM announcements ORDER BY ann_no"
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/announcements/import", response_model=AnnouncementResult)
def import_announcement(ann: AnnouncementIn):
    """公告收发规则：

    - 同号同版原样重送（正文哈希一致）→ 沿用原公告，记 reused；
    - 同号同版异文重送 → 退回 409，记 rejected_diff；
    - 更高修订 → 更正现行版本，记 updated（旧版仅留流水，现行表只留最高修订）；
    - 修订号低于现行 → 退回 422，记 rejected_old。
    """
    payload = ann.model_dump()
    digest = body_hash_of(payload)
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT revision, body_hash FROM announcements WHERE ann_no=?",
            (ann.ann_no,),
        ).fetchone()

        if row is not None and ann.revision == row["revision"]:
            if digest == row["body_hash"]:
                conn.execute(
                    "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                    " old_shares, new_shares, body_hash, outcome, note)"
                    " VALUES (?,?,?,?,?,?, 'reused', '同号同版原样重送，沿用原公告')",
                    (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
                     ann.new_shares, digest),
                )
                return AnnouncementResult(**payload, outcome="reused",
                                          note="同号同版原样重送，沿用原公告")
            conn.execute(
                "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                " old_shares, new_shares, body_hash, outcome, note)"
                " VALUES (?,?,?,?,?,?, 'rejected_diff', '同号同版异文，退回')",
                (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
                 ann.new_shares, digest),
            )
            raise HTTPException(
                status_code=409,
                detail=f"公告 {ann.ann_no} 第 {ann.revision} 版已存在且正文不同，异文退回",
            )

        if row is not None and ann.revision < row["revision"]:
            conn.execute(
                "INSERT INTO announcements_log(ann_no, revision, effective_date,"
                " old_shares, new_shares, body_hash, outcome, note)"
                " VALUES (?,?,?,?,?,?, 'rejected_old', '修订号低于现行最高修订，退回')",
                (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
                 ann.new_shares, digest),
            )
            raise HTTPException(
                status_code=422,
                detail=f"公告 {ann.ann_no} 现行修订为第 {row['revision']} 版，"
                       f"收到第 {ann.revision} 版（更低），退回",
            )

        outcome = "inserted" if row is None else "updated"
        note = "首次导入" if row is None else f"更高修订，替代第 {row['revision']} 版"
        conn.execute(
            "INSERT INTO announcements(ann_no, revision, effective_date, old_shares,"
            " new_shares, body_hash) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(ann_no) DO UPDATE SET revision=excluded.revision,"
            " effective_date=excluded.effective_date, old_shares=excluded.old_shares,"
            " new_shares=excluded.new_shares, body_hash=excluded.body_hash",
            (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
             ann.new_shares, digest),
        )
        conn.execute(
            "INSERT INTO announcements_log(ann_no, revision, effective_date,"
            " old_shares, new_shares, body_hash, outcome, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ann.ann_no, ann.revision, ann.effective_date, ann.old_shares,
             ann.new_shares, digest, outcome, note),
        )
        return AnnouncementResult(**payload, outcome=outcome, note=note)


@app.get("/api/announcements")
def list_announcements():
    with db_cursor() as conn:
        anns = _current_announcements(conn)
    return {"announcements": anns}


# ---------------------------------------------------------------- 预览/送审

def _load_prices(conn, start_date: str, end_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, close_cents FROM prices"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_prices_at_seq(conn, start_date: str, end_date: str,
                        seq: int) -> list[dict]:
    """按事件流重放 seq 时刻的日期归属，再到 prices 单行取价（价格不复制）。"""
    items = conn.execute(
        "SELECT seq, price_id, from_date, to_date FROM price_event_items"
        " WHERE seq <= ? ORDER BY seq, id",
        (seq,),
    ).fetchall()
    owner = replay_price_events([dict(r) for r in items])  # trade_date -> price_id
    if not owner:
        return []
    ids = list(owner.values())
    placeholders = ",".join("?" * len(ids))
    value_rows = conn.execute(
        f"SELECT price_id, close_cents FROM prices WHERE price_id IN ({placeholders})",
        ids,
    ).fetchall()
    values = {r["price_id"]: r["close_cents"] for r in value_rows}
    out = [
        {"trade_date": d, "close_cents": values[pid]}
        for d, pid in owner.items()
        if start_date <= d <= end_date
    ]
    out.sort(key=lambda x: x["trade_date"])
    return out


def _validate_window(conn, start_date: str, end_date: str, base_date: str) -> None:
    if not (start_date <= end_date):
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    if not (start_date <= base_date <= end_date):
        raise HTTPException(status_code=422,
                            detail="基准交易日必须落在所选区间内")
    row = conn.execute(
        "SELECT 1 FROM prices WHERE trade_date=?", (base_date,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=422,
                            detail=f"基准交易日 {base_date} 在行情底表中不存在")
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM prices WHERE trade_date BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()["c"]
    if cnt == 0:
        raise HTTPException(status_code=422,
                            detail="所选区间内没有任何行情数据")


def _preview(conn, start_date: str, end_date: str, base_date: str,
             client_basis_hash: str | None,
             client_price_seq: int | None) -> PreviewResult:
    _validate_window(conn, start_date, end_date, base_date)
    anns = _current_announcements(conn)
    current_hash = basis_hash_of(anns)
    current_seq = _current_price_seq(conn)
    events = relevant_events(start_date, end_date, base_date, anns)
    prices = _load_prices(conn, start_date, end_date)
    points = build_series(prices, base_date, events)
    basis_stale = client_basis_hash is not None and client_basis_hash != current_hash
    seq_stale = client_price_seq is not None and client_price_seq != current_seq
    stale = basis_stale or seq_stale
    notes = []
    if basis_stale:
        notes.append("现行公告与预览所据依据不同，须刷新后再锁定")
    if seq_stale:
        notes.append(
            f"行情已发生更正/补录（预览序列 {client_price_seq} → 现行 {current_seq}），"
            "须刷新后再锁定"
        )
    return PreviewResult(
        start_date=start_date,
        end_date=end_date,
        base_date=base_date,
        basis_hash=current_hash,
        basis=events,
        points=points,
        price_seq=current_seq,
        stale=stale,
        note="；".join(notes),
    )


@app.get("/api/adjustments/preview", response_model=PreviewResult)
def preview(start_date: str, end_date: str, base_date: str,
            basis_hash: str | None = None, price_seq: int | None = None):
    """区间折算预览。可带预览时拿到的 basis_hash / price_seq 检测现行数据是否已变更。"""
    with db_cursor() as conn:
        return _preview(conn, start_date, end_date, base_date, basis_hash, price_seq)


@app.post("/api/submissions", response_model=LockInResult)
def lock_submission(req: LockInRequest):
    """锁定预览并取得送审号；依据指纹或价格序列与现行不一致（stale）时拒绝，须先刷新。"""
    with db_cursor() as conn:
        preview_data = _preview(
            conn, req.start_date, req.end_date, req.base_date,
            req.basis_hash, req.price_seq,
        )
        if preview_data.stale:
            raise HTTPException(
                status_code=409,
                detail={"message": preview_data.note,
                        "current_basis_hash": preview_data.basis_hash,
                        "current_price_seq": preview_data.price_seq},
            )
        submission_id = "SB-" + uuid.uuid4().hex[:16].upper()
        conn.execute(
            "INSERT INTO submissions(submission_id, start_date, end_date,"
            " base_date, basis_hash, price_seq) VALUES (?,?,?,?,?,?)",
            (submission_id, req.start_date, req.end_date, req.base_date,
             preview_data.basis_hash, preview_data.price_seq),
        )
        # 只留重算所需的依据，不复制行情底表
        for e in preview_data.basis:
            conn.execute(
                "INSERT INTO submission_basis(submission_id, ann_no, revision,"
                " effective_date, old_shares, new_shares) VALUES (?,?,?,?,?,?)",
                (submission_id, e["ann_no"], e["revision"], e["effective_date"],
                 e["old_shares"], e["new_shares"]),
            )
        return LockInResult(submission_id=submission_id,
                            basis_hash=preview_data.basis_hash,
                            price_seq=preview_data.price_seq,
                            points=preview_data.points)


@app.get("/api/submissions/{submission_id}", response_model=ReopenResult)
def reopen_submission(submission_id: str):
    """凭送审号重开：按快照依据 + 送审时价格序列（事件重放）重现原序列。

    - 启用交易日更正功能前发出的送审号：price_seq 已在迁移时回填为启用基线，
      重开结果以启用时查到的序列为准；
    - 启用后发出的送审号：按送审时记录的序列重放，公告更正、行情更正/补录或
      进程重启后，序列仍与锁定时逐点一致。
    同时返回现行指纹/序列与 stale 标记供比对。
    """
    with db_cursor() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if sub is None:
            raise HTTPException(status_code=404,
                                detail=f"送审号 {submission_id} 不存在")
        basis_rows = conn.execute(
            "SELECT ann_no, revision, effective_date, old_shares, new_shares"
            " FROM submission_basis WHERE submission_id=? ORDER BY effective_date, ann_no",
            (submission_id,),
        ).fetchall()
        basis = [dict(r) for r in basis_rows]
        prices = _load_prices_at_seq(
            conn, sub["start_date"], sub["end_date"], sub["price_seq"]
        )
        points = build_series(prices, sub["base_date"], basis)

        current_hash = basis_hash_of(_current_announcements(conn))
        current_seq = _current_price_seq(conn)
        basis_stale = current_hash != sub["basis_hash"]
        seq_stale = current_seq != sub["price_seq"]
        stale = basis_stale or seq_stale
        notes = []
        if basis_stale:
            notes.append("公告已更正：以下为按送审时依据快照重现的原序列")
        if seq_stale:
            notes.append(
                f"行情日期归属已变更（送审序列 {sub['price_seq']} → 现行 {current_seq}）："
                "以下为按送审时序列重现的原序列"
            )
        if not notes:
            notes.append("现行公告与行情序列和送审依据一致")
        return ReopenResult(
            submission_id=submission_id,
            start_date=sub["start_date"],
            end_date=sub["end_date"],
            base_date=sub["base_date"],
            basis_hash=sub["basis_hash"],
            current_basis_hash=current_hash,
            price_seq=sub["price_seq"],
            current_price_seq=current_seq,
            stale=stale,
            note="；".join(notes),
            points=points,
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
