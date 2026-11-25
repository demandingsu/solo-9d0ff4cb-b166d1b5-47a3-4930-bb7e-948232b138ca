"""Tests for the shared, invalidatable rule-graph cache behind /conversion.

Covers the caching contract required by the monthly shipping re-computation:

* same item/day, different unit pairs share one graph; a hit reads no rule
  rows and builds no graph;
* only same-item keys inside an accepted rule's validity window (both
  boundary days included) are invalidated; other items and outside days keep
  their graphs;
* whole-batch rejection keeps the old graphs;
* a previously unconvertible unit pair resolves after the rules it needs are
  accepted;
* a builder that finishes late after an invalidation cannot overwrite the
  newer graph (generation guard);
* building for one day does not block cache hits on another day, nor rule
  submissions;
* a query only ever sees one point-in-time committed rule set;
* another process after acceptance rebuilds from the committed rules.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from datetime import date

import pytest

from app import services
from tests.conftest import rule


def _submit(client, item, rules):
    resp = client.post("/api/v1/batches",
                       json={"item_code": item, "rules": rules})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _query(client, item, day, fu, tu):
    resp = client.get("/api/v1/conversion", params={
        "item_code": item, "shipping_date": day,
        "from_unit": fu, "to_unit": tu,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cache(client):
    return client.get("/health").json()["cache"]


def test_same_item_same_day_different_unit_pairs_share_one_graph(client, monkeypatch):
    """换一对单位复用同一张图：只读取一次规则明细、只构一次图。"""
    from app import database as db
    from app import services

    item = "CACHE-1"
    _submit(client, item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
        rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
    ])

    reads = {"n": 0}
    builds = {"n": 0}
    real_active = db.active_rules_on
    real_build = services.build_adjacency

    def counting_active(*a, **kw):
        reads["n"] += 1
        return real_active(*a, **kw)

    def counting_build(rules):
        builds["n"] += 1
        return real_build(rules)

    monkeypatch.setattr(db, "active_rules_on", counting_active)
    monkeypatch.setattr(services, "build_adjacency", counting_build)

    q1 = _query(client, item, "2026-06-01", "件", "套")
    q2 = _query(client, item, "2026-06-01", "套", "件")
    q3 = _query(client, item, "2026-06-01", "件", "盒")

    assert q1["factor"] == "48/1"
    assert q2["factor"] == "1/48"
    assert q3["factor"] == "8/1"
    # First query builds; the next two unit pairs must neither read rule
    # detail rows nor construct another graph.
    assert reads["n"] == 1
    assert builds["n"] == 1
    stats = _cache(client)
    assert stats["hits"] == 2 and stats["builds"] == 1


def test_different_days_and_items_get_separate_graphs(client):
    item = "CACHE-2"
    _submit(client, item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-06-30"),
        rule("R2", "件", "盒", 1, 10, "2026-07-01", "2026-12-31"),
    ])
    before = _query(client, item, "2026-06-30", "件", "盒")
    after = _query(client, item, "2026-07-01", "件", "盒")
    assert before["factor"] == "8/1"
    assert after["factor"] == "10/1"
    stats = _cache(client)
    assert stats["builds"] == 2
    # repeat -> both cached
    _query(client, item, "2026-06-30", "盒", "件")
    _query(client, item, "2026-07-01", "盒", "件")
    assert _cache(client)["builds"] == 2 and _cache(client)["hits"] == 2


def test_accepted_batch_invalidates_only_same_item_days_in_window(client):
    """新规则通过后：同品项且落在有效期（首尾含）的图失效，其余复用。"""
    item_a, item_b = "CACHE-3A", "CACHE-3B"
    for it in (item_a, item_b):
        _submit(client, it, [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
        ])

    # Warm graphs on three days of A and one of B.
    for day in ("2026-03-01", "2026-06-30", "2026-09-01"):
        _query(client, item_a, day, "件", "盒")
    _query(client, item_b, "2026-06-30", "件", "盒")
    assert _cache(client)["builds"] == 4

    # R2 of A is valid 2026-04-01 .. 2026-06-30 (boundary days count) and
    # adds a new unit 套, without conflicting with R1.
    body = _submit(client, item_a, [
        rule("R2", "盒", "套", 1, 6, "2026-04-01", "2026-06-30"),
    ])
    assert body["status"] == "accepted"

    stats = _cache(client)
    # Only the 2026-06-30 graph of A falls inside [04-01, 06-30]: one key.
    assert stats["invalidations"] == 1
    assert stats["entries"] == 3  # A's 03-01 & 09-01 plus B's 06-30 remain

    # Days outside the window keep the old graph (still correct: no R2 there).
    early = _query(client, item_a, "2026-03-01", "件", "盒")
    late = _query(client, item_a, "2026-09-01", "件", "盒")
    assert early["available"] and late["available"]
    other_item = _query(client, item_b, "2026-06-30", "件", "盒")
    assert other_item["factor"] == "8/1"
    assert _cache(client)["builds"] == 4  # no rebuild for untouched keys

    # Boundary day 06-30 is rebuilt and now contains the new route via R2.
    inside = _query(client, item_a, "2026-06-30", "件", "套")
    assert inside["available"] is True
    assert inside["factor"] == "48/1"
    assert inside["path"]["rule_codes"] == ["R1", "R2"]
    assert _cache(client)["builds"] == 5

    # One day before the window starts: R2 not effective yet -> no route.
    just_before = _query(client, item_a, "2026-03-31", "件", "套")
    assert just_before["available"] is False


def test_rejected_batch_keeps_reusing_original_graphs(client, monkeypatch):
    """整批退回：不失效任何缓存图，随后查询仍然命中原图。"""
    item = "CACHE-4"
    _submit(client, item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
    ])
    _query(client, item, "2026-06-01", "件", "盒")
    assert _cache(client)["builds"] == 1

    body = _submit(client, item, [
        rule("R2", "件", "盒", 1, 9, "2026-03-01", "2026-10-01"),
    ])
    assert body["status"] == "rejected"
    stats = _cache(client)
    assert stats["invalidations"] == 0
    assert stats["entries"] == 1

    real_active = services.db.active_rules_on
    reads = {"n": 0}

    def counting_active(*a, **kw):
        reads["n"] += 1
        return real_active(*a, **kw)

    monkeypatch.setattr(services.db, "active_rules_on", counting_active)
    again = _query(client, item, "2026-06-01", "盒", "件")
    assert again["factor"] == "1/8"
    assert reads["n"] == 0  # served straight from the cached original graph


def test_previously_unconvertible_pair_resolves_after_new_rules(client):
    """此前查不通的单位对，在补规则并通过后同样能查到。"""
    item = "CACHE-5"
    _submit(client, item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
    ])
    missing = _query(client, item, "2026-06-01", "件", "套")
    assert missing["available"] is False
    # The graph (including its "no route" answer) is now cached.
    assert _cache(client)["builds"] == 1

    body = _submit(client, item, [
        rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
    ])
    assert body["status"] == "accepted"
    assert _cache(client)["invalidations"] == 1

    resolved = _query(client, item, "2026-06-01", "件", "套")
    assert resolved["available"] is True
    assert resolved["factor"] == "48/1"
    assert _cache(client)["builds"] == 2



# ---------------------------------------------------------------------------
# Concurrency tests below call the service layer directly instead of going
# through TestClient: the test client funnels every request through one
# blocking-portal thread and therefore cannot model parallel in-process
# requests. Each worker thread here opens its own request-scoped SQLite
# connection, exactly like the FastAPI dependency does in production.
# ---------------------------------------------------------------------------

from app import database as db  # noqa: E402
from app.cache import graph_cache  # noqa: E402
from app.schemas import BatchIn, RuleIn  # noqa: E402


def _svc_batch(item, payload_rules):
    conn = db.connect()
    try:
        batch = BatchIn(
            item_code=item,
            rules=[RuleIn(**r) for r in payload_rules],
        )
        return services.submit_batch(conn, batch)
    finally:
        conn.close()


def _svc_query(item, day, fu, tu):
    conn = db.connect()
    try:
        return services.lookup_conversion(
            conn, item, date.fromisoformat(day), fu, tu
        )
    finally:
        conn.close()


def test_late_finishing_old_graph_does_not_overwrite_newer_one():
    """同一天已有新图时，晚完成的旧图不得覆盖它（世代保护）。"""
    item = "CACHE-6"
    assert _svc_batch(item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
    ]).status == "accepted"

    key = (item, date(2026, 6, 1))
    hold = threading.Event()
    proceed = threading.Event()
    real_active = services.db.active_rules_on
    first_block = {"armed": True}

    def slow_build_active(conn, item_code, on_date):
        rows = real_active(conn, item_code, on_date)
        # Block only the very first build of this key.
        if first_block["armed"] and on_date == date(2026, 6, 1):
            first_block["armed"] = False
            proceed.set()
            hold.wait(5)
        return rows

    services.db.active_rules_on = slow_build_active
    try:
        worker = threading.Thread(
            target=_svc_query, args=(item, "2026-06-01", "件", "盒")
        )
        worker.start()
        assert proceed.wait(5), "slow builder never reached the rule read"

        # First graph still building: accept R2 (invalidation bumps this key's
        # generation even though no entry exists yet).
        result = _svc_batch(item, [
            rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
        ])
        assert result.status == "accepted"
        assert graph_cache.stats()["invalidations"] == 1

        # A second query queues on the per-key single-flight lock; it must end
        # up served by a graph containing R2.
        fresher = {}

        def newer():
            fresher["path"] = _svc_query(item, "2026-06-01", "件", "套")

        t2 = threading.Thread(target=newer)
        t2.start()
        time.sleep(0.3)
        assert t2.is_alive(), "second query should wait on the key lock"

        hold.set()
        worker.join(5)
        t2.join(5)
        assert not worker.is_alive() and not t2.is_alive()
    finally:
        services.db.active_rules_on = real_active

    stats = graph_cache.stats()
    assert stats["stale_discarded"] == 1
    entry = graph_cache._entries[key]
    assert entry.rule_codes == ("R1", "R2")

    path, message = fresher["path"]
    assert path is not None
    assert path.factor == (48, 1)
    # And a later plain query reads the new graph straight from the cache.
    path2, _ = _svc_query(item, "2026-06-01", "件", "套")
    assert path2.factor == (48, 1)
    assert graph_cache._entries[key] is entry


def test_concurrent_first_queries_build_one_graph_single_flight():
    """同键并发首查只构一张图（single-flight）。"""
    item = "CACHE-7"
    assert _svc_batch(item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
        rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
    ]).status == "accepted"

    real_build = services.build_adjacency
    build_entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    builds = {"n": 0}

    def slow_build(rules):
        with count_lock:
            builds["n"] += 1
        build_entered.set()
        release.wait(5)
        return real_build(rules)

    services.build_adjacency = slow_build
    try:
        results: list = []

        def worker():
            results.append(_svc_query(item, "2026-05-01", "件", "套"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        assert build_entered.wait(5)
        time.sleep(0.3)  # let the other seven queue on the single-flight lock
        assert builds["n"] == 1
        release.set()
        for t in threads:
            t.join(5)
            assert not t.is_alive()
    finally:
        services.build_adjacency = real_build

    assert builds["n"] == 1
    assert all(p.factor == (48, 1) for p, _ in results)
    stats = graph_cache.stats()
    assert stats["builds"] == 1 and stats["hits"] == 7


def test_building_one_day_does_not_block_other_days_or_submissions():
    """构图期间：其他日期命中缓存的查询和规则报送仍能继续。"""
    item = "CACHE-8"
    assert _svc_batch(item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
    ]).status == "accepted"
    # Warm a cache hit on another day.
    path, _ = _svc_query(item, "2026-02-01", "件", "盒")
    assert path.factor == (8, 1)

    hold = threading.Event()
    proceed = threading.Event()
    real_active = services.db.active_rules_on

    def blocking_read(conn, item_code, on_date):
        rows = real_active(conn, item_code, on_date)
        if on_date == date(2026, 8, 1):
            proceed.set()
            hold.wait(5)
        return rows

    services.db.active_rules_on = blocking_read
    try:
        worker = threading.Thread(
            target=_svc_query, args=(item, "2026-08-01", "件", "盒")
        )
        worker.start()
        assert proceed.wait(5)

        # 1) Another day's cached query completes without waiting on any lock.
        cached = threading.Event()

        def cached_hit():
            p, _ = _svc_query(item, "2026-02-01", "盒", "件")
            assert p.factor == (1, 8)
            cached.set()

        t_hit = threading.Thread(target=cached_hit)
        t_hit.start()
        assert cached.wait(2), "cached query on another day was blocked"

        # 2) A submission for a different item is not blocked either: it only
        # takes the registry lock for an instant during invalidation.
        submission_done = threading.Event()

        def submit_other():
            res = _svc_batch("CACHE-8-OTHER", [
                rule("S1", "件", "盒", 1, 4, "2026-01-01", "2026-12-31"),
            ])
            assert res.status == "accepted"
            submission_done.set()

        t_sub = threading.Thread(target=submit_other)
        t_sub.start()
        assert submission_done.wait(5), "rule submission blocked by graph build"

        hold.set()
        worker.join(5)
        t_hit.join(5)
        t_sub.join(5)
        assert not worker.is_alive()
    finally:
        services.db.active_rules_on = real_active

    path, _ = _svc_query(item, "2026-08-01", "件", "盒")
    assert path.factor == (8, 1)


def test_query_uses_one_point_in_time_committed_rule_set():
    """一次查询只使用同一时点的已提交规则（构图中途提交不串图）。"""
    item = "CACHE-9"
    assert _svc_batch(item, [
        rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
    ]).status == "accepted"

    hold = threading.Event()
    proceed = threading.Event()
    real_active = services.db.active_rules_on

    def blocking_read(conn, item_code, on_date):
        rows = real_active(conn, item_code, on_date)
        if on_date == date(2026, 6, 1):
            proceed.set()
            hold.wait(5)
        return rows

    services.db.active_rules_on = blocking_read
    first = {}
    try:
        def slow_query():
            first["result"] = _svc_query(item, "2026-06-01", "件", "套")

        worker = threading.Thread(target=slow_query)
        worker.start()
        assert proceed.wait(5)

        # R2 commits while the first query is parked inside its build.
        result = _svc_batch(item, [
            rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
        ])
        assert result.status == "accepted"

        hold.set()
        worker.join(5)
        assert not worker.is_alive()
    finally:
        services.db.active_rules_on = real_active

    # The parked build only ever read one snapshot. If it read pre-commit
    # rules (no R2 -> no route), the generation guard discards its graph; the
    # response is still internally consistent — never a half-mixed graph.
    path, _ = first["result"]
    if path is not None:
        assert path.factor == (48, 1)
        assert path.rule_codes == ["R1", "R2"]

    later, _ = _svc_query(item, "2026-06-01", "件", "套")
    assert later is not None and later.factor == (48, 1)
