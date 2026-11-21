"""Pydantic 请求/响应模型。日期统一 ISO 字符串 YYYY-MM-DD。"""
from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, Field, field_validator

DATE_FMT = "%Y-%m-%d"


def _validate_date(value: str) -> str:
    try:
        _dt.datetime.strptime(value, DATE_FMT)
    except (ValueError, TypeError) as exc:
        raise ValueError("日期必须为 YYYY-MM-DD 格式") from exc
    return value


class PriceIn(BaseModel):
    trade_date: str
    close_cents: int = Field(ge=0, description="收盘价（分），非负整数")

    _v = field_validator("trade_date")(_validate_date)


class PriceImportResult(BaseModel):
    received: int
    inserted: int
    skipped_identical: int
    conflicts: list[dict]


class AnnouncementIn(BaseModel):
    ann_no: str = Field(min_length=1, description="公告编号")
    revision: int = Field(ge=1, description="修订版本号，从 1 开始")
    effective_date: str = Field(description="生效日 YYYY-MM-DD")
    old_shares: int = Field(gt=0, description="旧股数（每股折算前股数）")
    new_shares: int = Field(gt=0, description="新股数（折算后股数）")

    _v = field_validator("effective_date")(_validate_date)


class AnnouncementResult(BaseModel):
    ann_no: str
    revision: int
    effective_date: str
    old_shares: int
    new_shares: int
    outcome: str
    note: str = ""


class AdjustedPoint(BaseModel):
    trade_date: str
    close_cents: int = Field(description="原始收盘价（分）")
    factor: str = Field(description="折算因子（精确分数 new_shares/old_shares 的连乘）")
    adjusted_cents: int = Field(description="折算到基准日口径并四舍五入到分")


class PreviewResult(BaseModel):
    start_date: str
    end_date: str
    base_date: str
    basis_hash: str
    basis: list[dict]
    points: list[AdjustedPoint]
    stale: bool
    note: str = ""


class LockInRequest(BaseModel):
    start_date: str
    end_date: str
    base_date: str
    basis_hash: str = Field(description="预览返回的依据指纹，须与现行公告一致")

    _v1 = field_validator("start_date")(_validate_date)
    _v2 = field_validator("end_date")(_validate_date)
    _v3 = field_validator("base_date")(_validate_date)


class LockInResult(BaseModel):
    submission_id: str
    basis_hash: str
    points: list[AdjustedPoint]


class ReopenResult(BaseModel):
    submission_id: str
    start_date: str
    end_date: str
    base_date: str
    basis_hash: str
    current_basis_hash: str
    stale: bool
    note: str
    points: list[AdjustedPoint]
