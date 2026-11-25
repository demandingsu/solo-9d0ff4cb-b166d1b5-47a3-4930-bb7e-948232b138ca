"""Request/response schemas with Chinese OpenAPI descriptions."""

from __future__ import annotations

import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class RuleIn(BaseModel):
    rule_code: str = Field(..., min_length=1, description="规则编号，同一品项内唯一")
    from_unit: str = Field(..., min_length=1, description="左侧包装单位，例如 件")
    to_unit: str = Field(..., min_length=1, description="右侧包装单位，例如 盒")
    from_amount: int = Field(..., gt=0, description="左侧数量，正整数")
    to_amount: int = Field(..., gt=0, description="等量的右侧数量，正整数")
    start_date: datetime.date = Field(..., description="生效起日（含当天）")
    end_date: datetime.date = Field(..., description="生效止日（含当天）")

    @model_validator(mode="after")
    def check_dates(self) -> "RuleIn":
        if self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        if self.from_unit == self.to_unit:
            raise ValueError("from_unit 与 to_unit 不能相同")
        return self


class BatchIn(BaseModel):
    item_code: str = Field(..., min_length=1, description="套书品项编号")
    rules: List[RuleIn] = Field(..., min_length=1, description="本批规则，整批通过或整批退回")

    @model_validator(mode="after")
    def check_duplicate_codes(self) -> "BatchIn":
        codes = [r.rule_code for r in self.rules]
        if len(codes) != len(set(codes)):
            raise ValueError("同批规则编号不得重复")
        return self


class PathOut(BaseModel):
    units: List[str] = Field(..., description="换算经过的包装单位")
    rule_codes: List[str] = Field(..., description="路径上各条规则的编号")
    factor_num: int
    factor_den: int
    factor: str = Field(..., description="精确倍率（分数），如 8/1")


class ConflictOut(BaseModel):
    conflict_date: datetime.date = Field(..., description="最早冲突日")
    from_unit: str
    to_unit: str
    path_a: PathOut
    path_b: PathOut


class BatchResponse(BaseModel):
    submission_id: int
    item_code: str
    status: str = Field(..., description="accepted 或 rejected")
    rule_codes: List[str]
    conflict: Optional[ConflictOut] = None
    message: str


class ConversionResponse(BaseModel):
    item_code: str
    shipping_date: datetime.date
    from_unit: str
    to_unit: str
    available: bool
    factor_num: Optional[int] = None
    factor_den: Optional[int] = None
    factor: Optional[str] = None
    path: Optional[PathOut] = None
    message: str


class RuleOut(BaseModel):
    rule_code: str
    item_code: str
    from_unit: str
    to_unit: str
    factor_num: int
    factor_den: int
    factor: str
    start_date: datetime.date
    end_date: datetime.date


class SubmissionOut(BaseModel):
    id: int
    item_code: str
    status: str
    rule_codes: List[str]
    conflict: Optional[ConflictOut] = None
    created_at: str
