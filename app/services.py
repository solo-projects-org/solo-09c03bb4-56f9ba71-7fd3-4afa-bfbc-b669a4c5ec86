"""核心业务逻辑：折算因子、四舍五入、依据指纹、送审快照重算。

口径说明
--------
拆并股公告：生效日起 old_shares 股换成 new_shares 股，记 r = new_shares/old_shares。
价值守恒：1 旧股 = r 新股，故 P_old = r * P_new，即 P_new = P_old / r。

对任意交易日 d 与基准交易日 b，把 d 的收盘价折算到基准日的股份口径：

- d、b 同为旧口径（均早于生效日）或同为新口径（均不早于生效日）：因子 1；
- d 为旧口径、b 为新口径（d < 生效日 <= b）：因子 1/r = old/new；
- d 为新口径、b 为旧口径（b < 生效日 <= d）：因子 r = new/old。

adjusted_cents = round_half_up(close_cents * factor)，四舍五入到分。
全程使用 Fraction 精确计算，避免浮点误差。
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction

from app.models import AdjustedPoint


def round_half_up(value: Fraction) -> int:
    """银行家舍入之外的“四舍五入到整数分”：小数部分 >= 1/2 进位。"""
    q, r = divmod(value.numerator, value.denominator)
    return q + (1 if r * 2 >= value.denominator else 0)


def factor_for_date(trade_date: str, base_date: str, events: list[dict]) -> Fraction:
    """按上述口径规则计算单日折算因子。events 需含 effective_date/old/new。"""
    factor = Fraction(1)
    for e in events:
        eff = e["effective_date"]
        ratio = Fraction(e["new_shares"], e["old_shares"])
        day_is_new = trade_date >= eff
        base_is_new = base_date >= eff
        if day_is_new and not base_is_new:
            factor *= ratio        # 新口径报价 → 旧口径
        elif not day_is_new and base_is_new:
            factor *= 1 / ratio    # 旧口径报价 → 新口径
        # 两端同口径时因子为 1
    return factor


def canonical_announcement(ann: dict) -> dict:
    return {
        "ann_no": ann["ann_no"],
        "revision": int(ann["revision"]),
        "effective_date": ann["effective_date"],
        "old_shares": int(ann["old_shares"]),
        "new_shares": int(ann["new_shares"]),
    }


def body_hash_of(ann: dict) -> str:
    """同号同版比对用：对公告正文（不含编号以外的易变字段）取稳定哈希。"""
    payload = {
        "revision": int(ann["revision"]),
        "effective_date": ann["effective_date"],
        "old_shares": int(ann["old_shares"]),
        "new_shares": int(ann["new_shares"]),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def basis_hash_of(anns: list[dict]) -> str:
    """现行公告依据指纹：按编号排序后取规范 JSON 的 sha256。"""
    payload = [canonical_announcement(a) for a in anns]
    payload.sort(key=lambda x: x["ann_no"])
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def relevant_events(start_date: str, end_date: str, base_date: str,
                    anns: list[dict]) -> list[dict]:
    """筛选重算所需公告：生效日落在 (start, max(end, base)] 半开区间内。

    生效日 <= start 的公告对区间内每一天与基准日的相对因子恒为 1（两边同乘），
    不影响结果，无需留底；晚于 end 与 base 的公告同样不参与。
    """
    latest = max(end_date, base_date)
    out = []
    for a in anns:
        if start_date < a["effective_date"] <= latest:
            out.append(canonical_announcement(a))
    out.sort(key=lambda x: (x["effective_date"], x["ann_no"]))
    return out


def build_series(prices: list[dict], base_date: str,
                 events: list[dict]) -> list[AdjustedPoint]:
    """用给定行情与公告依据（快照或现行）计算折算序列。"""
    points: list[AdjustedPoint] = []
    for p in prices:
        f = factor_for_date(p["trade_date"], base_date, events)
        adjusted = round_half_up(Fraction(p["close_cents"]) * f)
        points.append(AdjustedPoint(
            trade_date=p["trade_date"],
            close_cents=int(p["close_cents"]),
            factor=str(f),
            adjusted_cents=adjusted,
        ))
    return points


# ------------------------------------------------------------ 交易日更正

class CorrectionError(ValueError):
    """更正单业务校验失败（结构/状态冲突），由路由层映射为 422/409。"""


def correction_hash_of(items: list[dict]) -> str:
    """更正单正文哈希：对排序后的 from->to 对应表取规范 JSON 的 sha256。

    用于“原样重传返回原结果、同号异文报冲突”。
    """
    payload = sorted(
        ({"from_date": i["from_date"], "to_date": i["to_date"]} for i in items),
        key=lambda x: x["from_date"],
    )
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_correction(items: list[dict]) -> list[dict]:
    """结构校验并规范化：非空、原日期/目标日期各自唯一、from != to。"""
    if not items:
        raise CorrectionError("更正对应表为空")
    from_seen: set[str] = set()
    to_seen: set[str] = set()
    normalized: list[dict] = []
    for i in items:
        f, t = i["from_date"], i["to_date"]
        if f == t:
            raise CorrectionError(f"原交易日与正确交易日相同（{f}），无需更正")
        if f in from_seen:
            raise CorrectionError(f"原交易日 {f} 在本单中出现多次，每个原日期只能出现一次")
        if t in to_seen:
            raise CorrectionError(f"目标交易日 {t} 在本单中出现多次，每个目标日期只能出现一次")
        from_seen.add(f)
        to_seen.add(t)
        normalized.append({"from_date": f, "to_date": t})
    normalized.sort(key=lambda x: x["from_date"])
    return normalized


def validate_correction(mapping: list[dict], live_dates: set[str]) -> None:
    """按现行底表校验更正单（结构校验已先行）：

    - 每个原日期须有行情；
    - 目标日期已有行情时，该目标日期必须也作为原日期在本单中移走
      （整体构成若干闭合链/互换，不留未处理的一进一出冲突）。
    """
    from_dates = {m["from_date"] for m in mapping}
    missing = sorted(d for d in from_dates if d not in live_dates)
    if missing:
        raise CorrectionError(
            "以下原交易日在行情底表中不存在，无法更正：" + "、".join(missing)
        )
    blocked = sorted(
        m["to_date"]
        for m in mapping
        if m["to_date"] in live_dates and m["to_date"] not in from_dates
    )
    if blocked:
        raise CorrectionError(
            "以下目标交易日已有行情，但未在同一单中作为原交易日移走："
            + "、".join(blocked)
        )


def apply_mapping(dates: dict[int, str], mapping: list[dict]) -> dict[int, str]:
    """纯函数：把 from->to 置换作用到 price_id -> trade_date 映射上。"""
    lookup = {d: pid for pid, d in dates.items()}
    next_dates = dict(dates)
    for m in mapping:
        next_dates[lookup[m["from_date"]]] = m["to_date"]
    return next_dates


def replay_price_events(event_items: list[dict]) -> dict[str, int]:
    """重放事件明细，还原某一历史序列时刻的 trade_date -> price_id 归属。

    event_items 须已按 seq、id 排序且只含 seq<=目标序号的明细：
    - 导入事件（含启用基线）：to_date 登记该 price_id；
    - 更正事件：同一 seq 的 from->to 是整单置换（可含互换/闭合链），
      先统一摘挂再统一落位，避免互换中间态的同名键互相覆盖。
    价格数值始终只存于 prices 表一行，这里只重建日期归属。
    """
    owner: dict[str, int] = {}
    pending: list[tuple[str, str]] = []
    current_seq = None

    def flush() -> None:
        if not pending:
            return
        moved: dict[str, int] = {}
        for from_date, to_date in pending:
            moved[to_date] = owner.pop(from_date)
        owner.update(moved)
        pending.clear()

    for it in event_items:
        if it["seq"] != current_seq:
            flush()
            current_seq = it["seq"]
        pid = it["price_id"]
        if pid is not None:
            # 导入事件（含启用基线）：to_date 登记该 price_id
            owner[it["to_date"]] = pid
        else:
            # 更正事件：收集整单置换，seq 结束时两阶段应用
            pending.append((it["from_date"], it["to_date"]))
    flush()
    return owner
