"""FastAPI application: packaging-unit conversion service."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from typing import List, Optional

from fastapi import Depends, FastAPI, Query
from sqlite3 import Connection

from . import database as db
from .rationals import frac_str
from .schemas import (
    BatchIn,
    BatchResponse,
    ConversionResponse,
    PathOut,
    RuleOut,
    SubmissionOut,
)
from .services import conflict_to_out, lookup_conversion, submit_batch
from .validation import Conflict, PathInfo


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the schema up front; request-scoped connections are opened later
    # via the request_connection dependency.
    startup = db.connect()
    db.init_db(startup)
    startup.close()
    yield


app = FastAPI(
    title="套书包装单位换算 API",
    description=(
        "按套书品项批量报送包装换算规则（如 1 件 = 8 盒），"
        "与在用规则一起核定；同品项同日同一对单位沿任意有效路径倍率必须精确一致，"
        "冲突则整批退回并给出最早冲突日与两条规则路径。首尾日均生效，关系双向可换算。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn(conn: Connection = Depends(db.request_connection)) -> Connection:
    """One SQLite connection per HTTP request (closed when the request ends)."""
    return conn


def _conflict_from_row(conflict_text: Optional[str]) -> Optional[dict]:
    if not conflict_text:
        return None
    payload = json.loads(conflict_text)
    if "duplicated_rule_codes" in payload:
        return None
    return Conflict(
        conflict_date=date.fromisoformat(payload["conflict_date"]),
        from_unit=payload["from_unit"],
        to_unit=payload["to_unit"],
        path_a=_path_from_dict(payload["path_a"]),
        path_b=_path_from_dict(payload["path_b"]),
    )


def _path_from_dict(payload: dict) -> PathInfo:
    return PathInfo(
        units=payload["units"],
        rule_codes=payload["rule_codes"],
        factor=tuple(payload["factor"]),
    )


@app.post("/api/v1/batches", response_model=BatchResponse,
          summary="批量报送规则并核定（整批通过或整批退回）", tags=["规则报送"])
def post_batch(batch: BatchIn, conn: Connection = Depends(get_conn)) -> BatchResponse:
    result = submit_batch(conn, batch)
    return BatchResponse(
        submission_id=result.submission_id,
        item_code=batch.item_code,
        status=result.status,
        rule_codes=[r.rule_code for r in batch.rules],
        conflict=result.conflict,
        message=result.message,
    )


@app.get("/api/v1/conversion", response_model=ConversionResponse,
         summary="按品项、发运日和两种单位查询精确倍率", tags=["换算查询"])
def get_conversion(
    item_code: str = Query(..., description="套书品项编号"),
    shipping_date: date = Query(..., description="发运日（YYYY-MM-DD）"),
    from_unit: str = Query(..., description="源包装单位"),
    to_unit: str = Query(..., description="目标包装单位"),
    conn: Connection = Depends(get_conn),
) -> ConversionResponse:
    path, message = lookup_conversion(conn, item_code, shipping_date, from_unit, to_unit)
    if path is None:
        return ConversionResponse(
            item_code=item_code,
            shipping_date=shipping_date,
            from_unit=from_unit,
            to_unit=to_unit,
            available=False,
            message=message,
        )
    return ConversionResponse(
        item_code=item_code,
        shipping_date=shipping_date,
        from_unit=from_unit,
        to_unit=to_unit,
        available=True,
        factor_num=path.factor[0],
        factor_den=path.factor[1],
        factor=frac_str(path.factor),
        path=PathOut(
            units=path.units,
            rule_codes=path.rule_codes,
            factor_num=path.factor[0],
            factor_den=path.factor[1],
            factor=frac_str(path.factor),
        ),
        message="ok",
    )


@app.get("/api/v1/rules", response_model=List[RuleOut],
         summary="列出某品项的全部规则", tags=["规则报送"])
def get_rules(item_code: str, conn: Connection = Depends(get_conn)) -> List[RuleOut]:
    records = db.get_rules(conn, item_code)
    return [
        RuleOut(
            rule_code=r.rule_code,
            item_code=r.item_code,
            from_unit=r.from_unit,
            to_unit=r.to_unit,
            factor_num=r.factor[0],
            factor_den=r.factor[1],
            factor=frac_str(r.factor),
            start_date=r.start_date,
            end_date=r.end_date,
        )
        for r in records
    ]


@app.get("/api/v1/submissions", response_model=List[SubmissionOut],
         summary="查看送审批次记录（含退回原因）", tags=["规则报送"])
def get_submissions(
    item_code: Optional[str] = Query(None, description="可选：按品项过滤"),
    conn: Connection = Depends(get_conn),
) -> List[SubmissionOut]:
    rows = db.list_submissions(conn, item_code)
    result: List[SubmissionOut] = []
    for row in rows:
        conflict = _conflict_from_row(row["conflict"])
        result.append(
            SubmissionOut(
                id=row["id"],
                item_code=row["item_code"],
                status=row["status"],
                rule_codes=row["rule_codes"].split(",") if row["rule_codes"] else [],
                conflict=conflict_to_out(conflict) if conflict else None,
                created_at=row["created_at"],
            )
        )
    return result


@app.get("/health", summary="健康检查", tags=["系统"])
def health() -> dict:
    return {"status": "ok"}
