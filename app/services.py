"""Application services: batch submission and conversion lookup."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from typing import List, Optional

from . import database as db
from .graph import build_adjacency, find_path
from .rationals import frac_str, reduce_frac
from .schemas import BatchIn, ConflictOut, PathOut
from .validation import Conflict, RuleRecord, validate_batch


@dataclass(frozen=True)
class SubmissionResult:
    submission_id: int
    status: str  # accepted | rejected
    conflict: Optional[ConflictOut]
    message: str


def _to_records(batch: BatchIn) -> List[RuleRecord]:
    return [
        RuleRecord(
            rule_code=r.rule_code,
            item_code=batch.item_code,
            from_unit=r.from_unit,
            to_unit=r.to_unit,
            factor=reduce_frac(r.to_amount, r.from_amount),
            start_date=r.start_date,
            end_date=r.end_date,
        )
        for r in batch.rules
    ]


def _path_out(path) -> PathOut:
    return PathOut(
        units=list(path.units),
        rule_codes=list(path.rule_codes),
        factor_num=path.factor[0],
        factor_den=path.factor[1],
        factor=frac_str(path.factor),
    )


def conflict_to_out(conflict: Conflict) -> ConflictOut:
    return ConflictOut(
        conflict_date=conflict.conflict_date,
        from_unit=conflict.from_unit,
        to_unit=conflict.to_unit,
        path_a=_path_out(conflict.path_a),
        path_b=_path_out(conflict.path_b),
    )


def _conflict_json(conflict: Conflict) -> str:
    payload = asdict(conflict)
    payload["conflict_date"] = conflict.conflict_date.isoformat()
    payload["path_a"]["factor"] = list(conflict.path_a.factor)
    payload["path_b"]["factor"] = list(conflict.path_b.factor)
    return json.dumps(payload, ensure_ascii=False)


def submit_batch(conn: sqlite3.Connection, batch: BatchIn) -> SubmissionResult:
    """Validate candidate rules against in-use rules inside one write txn."""
    candidates = _to_records(batch)
    codes = [r.rule_code for r in candidates]

    with db.immediate_transaction(conn):
        existing = db.get_rules(conn, batch.item_code)

        # Duplicate rule codes against stored rules are a hard rejection of
        # the whole batch (a re-submitted number could otherwise silently
        # denote two different rules).
        duplicated = sorted(
            {r.rule_code for r in candidates
             if db.rule_code_exists(conn, batch.item_code, r.rule_code)}
        )
        if duplicated:
            message = f"规则编号已存在，整批退回：{', '.join(duplicated)}"
            submission_id = db.insert_submission(
                conn, batch.item_code, "rejected", codes,
                json.dumps({"duplicated_rule_codes": duplicated}, ensure_ascii=False),
            )
            return SubmissionResult(submission_id, "rejected", None, message)

        result = validate_batch(existing, candidates)
        if not result.ok:
            assert result.conflict is not None
            conflict_out = conflict_to_out(result.conflict)
            message = (
                f"发现冲突，整批退回。最早冲突日 {result.conflict.conflict_date.isoformat()}，"
                f"{result.conflict.from_unit}→{result.conflict.to_unit} 两条路径倍率不一致："
                f"路径A {frac_str(result.conflict.path_a.factor)}"
                f"（规则 {'、'.join(result.conflict.path_a.rule_codes)}），"
                f"路径B {frac_str(result.conflict.path_b.factor)}"
                f"（规则 {'、'.join(result.conflict.path_b.rule_codes)}）"
            )
            submission_id = db.insert_submission(
                conn, batch.item_code, "rejected", codes,
                _conflict_json(result.conflict),
            )
            return SubmissionResult(submission_id, "rejected", conflict_out, message)

        for record in candidates:
            db.insert_rule(conn, record)
        submission_id = db.insert_submission(
            conn, batch.item_code, "accepted", codes, None
        )
    return SubmissionResult(
        submission_id, "accepted", None, f"已接受并生效 {len(candidates)} 条规则"
    )


def lookup_conversion(conn: sqlite3.Connection, item_code: str, shipping_date: date,
                      from_unit: str, to_unit: str):
    rules = db.active_rules_on(conn, item_code, shipping_date)
    adj = build_adjacency(rules)
    path = find_path(adj, from_unit, to_unit)
    if path is None:
        return None, (
            f"{shipping_date.isoformat()} 品项 {item_code} 没有可从 "
            f"{from_unit} 换算到 {to_unit} 的有效规则路径"
        )
    return path, "ok"
