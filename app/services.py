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
