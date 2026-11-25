"""Unit tests for the sweep-line validator and rational arithmetic."""

from datetime import date

from app.rationals import invert, mul, reduce_frac
from app.validation import RuleRecord, validate_batch


def rec(code, fu, tu, num, den, start, end):
    return RuleRecord(
        rule_code=code, item_code="IT", from_unit=fu, to_unit=tu,
        factor=(num, den),
        start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
    )


def test_fraction_reduction_and_equality():
    assert reduce_frac(16, 2) == (8, 1)
    assert mul((1, 8), (1, 6)) == (1, 48)
    assert invert((8, 1)) == (1, 8)


def test_disjoint_intervals_allow_different_factors():
    rules = [
        rec("R1", "件", "盒", 8, 1, "2026-01-01", "2026-06-30"),
        rec("R2", "件", "盒", 10, 1, "2026-07-01", "2026-12-31"),
    ]
    assert validate_batch([], rules).ok is True


def test_touching_end_day_creates_conflict_on_that_day():
    existing = [rec("R1", "件", "盒", 8, 1, "2026-01-01", "2026-06-30")]
    candidates = [rec("R2", "件", "盒", 10, 1, "2026-06-30", "2026-12-31")]
    result = validate_batch(existing, candidates)
    assert result.ok is False
    assert result.conflict.conflict_date == date(2026, 6, 30)
    assert result.conflict.path_a.rule_codes == ["R1"]
    assert result.conflict.path_b.rule_codes == ["R2"]


def test_triangle_consistency_accepted_and_rejected():
    good = [
        rec("R1", "件", "盒", 8, 1, "2026-01-01", "2026-12-31"),
        rec("R2", "盒", "套", 6, 1, "2026-01-01", "2026-12-31"),
        rec("R3", "件", "套", 48, 1, "2026-01-01", "2026-12-31"),
    ]
    assert validate_batch([], good).ok is True

    bad = good[:2] + [rec("R3", "件", "套", 47, 1, "2026-01-01", "2026-12-31")]
    result = validate_batch([], bad)
    assert result.ok is False
    assert result.conflict.path_a.rule_codes == ["R1", "R2"]
    assert result.conflict.path_b.rule_codes == ["R3"]
    assert result.conflict.path_a.factor == (48, 1)
    assert result.conflict.path_b.factor == (47, 1)


def test_conflict_later_in_timeline_reports_later_start():
    existing = [rec("R1", "件", "盒", 8, 1, "2026-01-01", "2026-12-31")]
    candidates = [rec("R2", "件", "盒", 9, 1, "2026-09-01", "2026-12-31")]
    result = validate_batch(existing, candidates)
    assert result.ok is False
    assert result.conflict.conflict_date == date(2026, 9, 1)


def test_rules_with_no_unit_overlap_never_conflict():
    rules = [
        rec("R1", "件", "盒", 8, 1, "2026-01-01", "2026-12-31"),
        rec("R2", "托", "箱", 3, 1, "2026-01-01", "2026-12-31"),
    ]
    assert validate_batch([], rules).ok is True
