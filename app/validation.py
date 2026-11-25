"""Batch consistency validation for packaging-unit conversion rules.

A rule states an equality such as ``1 件 = 8 盒``; both directions are usable.
For any one item, on any given day, every valid path between the same pair of
units must yield the *exact* same factor.

Validation is event based ("sweep line") rather than day-by-day expansion: the
active rule set only changes on a rule's start date or the day after its end
date, so the timeline is split into segments and each segment is checked once.

Within a segment rules are inserted into a weighted union-find structure whose
weights are exact rational factors. A new edge whose endpoints are already
connected with a different factor is a conflict. Two full conversion paths are
then reconstructed with BFS over an adjacency graph so that the caller can be
shown both routes and the rule codes that define them.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .rationals import Fraction, div, invert, mul, reduce_frac

# A directed graph edge: (neighbour, factor from this node to the neighbour).
Edge = Tuple[str, Fraction]


@dataclass(frozen=True)
class RuleRecord:
    """One conversion rule (existing or candidate in the batch under review)."""

    rule_code: str
    item_code: str
    from_unit: str
    to_unit: str
    # factor: quantity of ``to_unit`` equal to one ``from_unit``
    factor: Fraction
    start_date: date
    end_date: date


@dataclass(frozen=True)
class PathInfo:
    """A concrete conversion route between two units."""

    units: List[str]
    rule_codes: List[str]
    factor: Fraction  # units[0] -> units[-1]


@dataclass(frozen=True)
class Conflict:
    conflict_date: date
    from_unit: str
    to_unit: str
    path_a: PathInfo
    path_b: PathInfo


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    conflict: Optional[Conflict] = None


class WeightedDSU:
    """Union-find with rational edge weights, no destructive path compression.

    ``weight[x]`` is the factor converting one unit of ``parent[x]`` into one
    unit of ``x`` (i.e. value(x) = weight[x] * value(parent[x])). Compression is
    skipped because the parent chain is only O(n) deep for the batch sizes
    involved and keeping the original links makes behaviour easy to reason
    about; the adjacency graph is used for path reconstruction anyway.
    """
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.weight: Dict[str, Fraction] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.weight[x] = (1, 1)

    def root(self, x: str) -> str:
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def factor_to_root(self, x: str) -> Fraction:
        """Factor converting one unit of the root into one unit of ``x``.

        With ``weight[x]`` such that value(x) = weight[x] * value(parent[x]),
        walking the chain to the root multiplies the weights.
        """
        result: Fraction = (1, 1)
        while self.parent[x] != x:
            result = mul(self.weight[x], result)
            x = self.parent[x]
        return result

    def connected_factor(self, a: str, b: str) -> Optional[Fraction]:
        """Factor converting one unit of ``a`` into units of ``b``.

        value(b) = fb * value(root) and value(a) = fa * value(root), hence
        value(b) = fb/fa * value(a), i.e. one unit of a equals fb/fa units of b.
        """
        if a not in self.parent or b not in self.parent:
            return None
        if self.root(a) != self.root(b):
            return None
        fa = self.factor_to_root(a)
        fb = self.factor_to_root(b)
        return div(fb, fa)

    def union(self, a: str, b: str, factor_ab: Fraction) -> None:
        """Merge components given value(b) = factor_ab * value(a)."""
        self.add(a)
        self.add(b)
        ra, rb = self.root(a), self.root(b)
        if ra == rb:
            return
        # value(a) = fa * value(ra), value(b) = fb * value(rb)
        fa = self.factor_to_root(a)
        fb = self.factor_to_root(b)
        # value(rb) = value(b)/fb = factor_ab * value(a)/fb
        #           = factor_ab * fa/fb * value(ra)
        self.parent[rb] = ra
        self.weight[rb] = reduce_frac(factor_ab[0] * fa[0] * fb[1],
                                      factor_ab[1] * fa[1] * fb[0])


def _bfs_path(adj: Dict[str, List[Tuple[str, Fraction, str]]],
              start: str, goal: str) -> Optional[List[Tuple[str, Fraction, str]]]:
    """Shortest hop path in the active graph; edges are (neighbour, factor, rule)."""
    if start == goal:
        return []
    # prev[node] = (predecessor, factor predecessor->node, rule code)
    prev: Dict[str, Tuple[str, Fraction, str]] = {}
    seen: Set[str] = {start}
    queue: deque[str] = deque([start])
    found = False
    while queue and not found:
        node = queue.popleft()
        for nxt, frac, rule_code in adj[node]:
            if nxt in seen:
                continue
            seen.add(nxt)
            prev[nxt] = (node, frac, rule_code)
            if nxt == goal:
                found = True
                break
            queue.append(nxt)
    if not found:
        return None
    edges: List[Tuple[str, Fraction, str]] = []
    node = goal
    while node != start:
        parent, frac, rule_code = prev[node]
        edges.append((node, frac, rule_code))
        node = parent
    edges.reverse()
    return edges


def _build_path(adj, start: str, goal: str) -> PathInfo:
    """Walk the BFS path and collect units, rule codes and the total factor."""
    edges = _bfs_path(adj, start, goal)
    assert edges is not None, "units must already be connected"
    units: List[str] = [start]
    rule_codes: List[str] = []
    factor: Fraction = (1, 1)
    for neighbour, frac, rule_code in edges:
        factor = mul(factor, frac)
        rule_codes.append(rule_code)
        units.append(neighbour)
    return PathInfo(units=units, rule_codes=rule_codes, factor=factor)


def validate_batch(existing: Sequence[RuleRecord],
                   candidates: Sequence[RuleRecord]) -> ValidationResult:
    """Check candidates together with all in-use rules for one item.

    Returns the earliest conflict found; the whole batch is rejected on any
    conflict. Rules with disjoint validity intervals never meet, so different
    factors between the same pair of units are allowed across disjoint periods.
    """
    rules = sorted(list(existing) + list(candidates), key=lambda r: r.rule_code)
    if not rules:
        return ValidationResult(ok=True)

    # event date -> rules that start there / rules that expire the day before
    starts: Dict[date, List[RuleRecord]] = defaultdict(list)
    ends: Dict[date, List[RuleRecord]] = defaultdict(list)
    event_dates: Set[date] = set()
    for rule in rules:
        starts[rule.start_date].append(rule)
        after_end = rule.end_date + timedelta(days=1)
        ends[after_end].append(rule)
        event_dates.add(rule.start_date)
        event_dates.add(after_end)

    active: Set[str] = set()
    for segment_start in sorted(event_dates):
        for rule in ends.get(segment_start, ()):
            active.discard(rule.rule_code)
        for rule in starts.get(segment_start, ()):
            active.add(rule.rule_code)
        if not active:
            continue

        dsu = WeightedDSU()
        adj: Dict[str, List[Tuple[str, Fraction, str]]] = defaultdict(list)
        active_rules = sorted((r for r in rules if r.rule_code in active),
                              key=lambda r: r.rule_code)
        for rule in active_rules:
            a, b = rule.from_unit, rule.to_unit
            factor_ab = rule.factor  # value(b) = factor_ab * value(a)
            known = dsu.connected_factor(a, b)
            if known is not None and known != factor_ab:
                path_a = _build_path(adj, a, b)
                # the new rule itself is the second, single-edge route
                path_b = PathInfo(
                    units=[a, b],
                    rule_codes=[rule.rule_code],
                    factor=factor_ab,
                )
                return ValidationResult(
                    ok=False,
                    conflict=Conflict(
                        conflict_date=segment_start,
                        from_unit=a,
                        to_unit=b,
                        path_a=path_a,
                        path_b=path_b,
                    ),
                )
            dsu.union(a, b, factor_ab)
            adj[a].append((b, factor_ab, rule.rule_code))
            adj[b].append((a, invert(factor_ab), rule.rule_code))

    return ValidationResult(ok=True)
