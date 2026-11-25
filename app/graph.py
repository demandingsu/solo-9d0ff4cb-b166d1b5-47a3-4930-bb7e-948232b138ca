"""Graph lookup for the effective factor between two units on a given day."""

from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Tuple

from .rationals import Fraction, invert, mul
from .validation import PathInfo, RuleRecord


def build_adjacency(rules: List[RuleRecord]) -> Dict[str, List[Tuple[str, Fraction, str]]]:
    adj: Dict[str, List[Tuple[str, Fraction, str]]] = {}
    for rule in rules:
        adj.setdefault(rule.from_unit, []).append(
            (rule.to_unit, rule.factor, rule.rule_code)
        )
        adj.setdefault(rule.to_unit, []).append(
            (rule.from_unit, invert(rule.factor), rule.rule_code)
        )
    return adj


def find_path(adj: Dict[str, List[Tuple[str, Fraction, str]]],
              from_unit: str, to_unit: str) -> Optional[PathInfo]:
    """BFS shortest-hop conversion path; returns None when no route exists."""
    if from_unit == to_unit:
        return PathInfo(units=[from_unit], rule_codes=[], factor=(1, 1))
    if from_unit not in adj or to_unit not in adj:
        return None

    prev: Dict[str, Tuple[str, Fraction, str]] = {}
    seen = {from_unit}
    queue: deque[str] = deque([from_unit])
    found = False
    while queue and not found:
        node = queue.popleft()
        for neighbour, frac, rule_code in adj[node]:
            if neighbour in seen:
                continue
            seen.add(neighbour)
            prev[neighbour] = (node, frac, rule_code)
            if neighbour == to_unit:
                found = True
                break
            queue.append(neighbour)

    if not found:
        return None

    units: List[str] = [to_unit]
    edges: List[Tuple[str, Fraction, str]] = []
    node = to_unit
    while node != from_unit:
        parent, frac, rule_code = prev[node]
        edges.append((node, frac, rule_code))
        units.append(parent)
        node = parent
    units.reverse()
    edges.reverse()

    factor: Fraction = (1, 1)
    rule_codes: List[str] = []
    for _neighbour, edge_factor, rule_code in edges:
        factor = mul(factor, edge_factor)
        rule_codes.append(rule_code)
    return PathInfo(units=units, rule_codes=rule_codes, factor=factor)
