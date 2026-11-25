"""API-level tests for rule submission, conflicts and conversion queries."""

from concurrent.futures import ThreadPoolExecutor

from tests.conftest import rule


def test_basic_batch_accept_and_query_both_directions(client):
    payload = {
        "item_code": "SET-001",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31")],
    }
    resp = client.post("/api/v1/batches", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["conflict"] is None

    # 1 件 = 8 盒
    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-001", "shipping_date": "2026-06-01",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    assert q["available"] is True
    assert (q["factor_num"], q["factor_den"]) == (8, 1)
    assert q["path"]["rule_codes"] == ["R1"]

    # reverse direction: 1 盒 = 1/8 件, exact fraction
    q2 = client.get("/api/v1/conversion", params={
        "item_code": "SET-001", "shipping_date": "2026-06-01",
        "from_unit": "盒", "to_unit": "件",
    }).json()
    assert q2["available"] is True
    assert (q2["factor_num"], q2["factor_den"]) == (1, 8)


def test_multi_hop_path_and_exact_fraction(client):
    # 1 件 = 8 盒, 1 盒 = 6 套  => 1 件 = 48 套
    payload = {
        "item_code": "SET-002",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
        ],
    }
    assert client.post("/api/v1/batches", json=payload).json()["status"] == "accepted"

    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-002", "shipping_date": "2026-03-01",
        "from_unit": "件", "to_unit": "套",
    }).json()
    assert q["available"] is True
    assert q["factor"] == "48/1"
    assert q["path"]["units"] == ["件", "盒", "套"]
    assert q["path"]["rule_codes"] == ["R1", "R2"]

    # reduced fraction: 1 套 = 1/48 件
    q2 = client.get("/api/v1/conversion", params={
        "item_code": "SET-002", "shipping_date": "2026-03-01",
        "from_unit": "套", "to_unit": "件",
    }).json()
    assert q2["factor"] == "1/48"


def test_conflict_rejects_whole_batch_with_earliest_date_and_paths(client):
    first = {
        "item_code": "SET-003",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
        ],
    }
    assert client.post("/api/v1/batches", json=first).json()["status"] == "accepted"

    # R3 implies 1 件 = 12 套, but R1*R2 says 48 -> conflict on R3 start day
    second = {
        "item_code": "SET-003",
        "rules": [rule("R3", "件", "套", 1, 12, "2026-03-01", "2026-10-01")],
    }
    body = client.post("/api/v1/batches", json=second).json()
    assert body["status"] == "rejected"
    c = body["conflict"]
    assert c["conflict_date"] == "2026-03-01"
    assert c["from_unit"] == "件" and c["to_unit"] == "套"
    assert c["path_a"]["rule_codes"] == ["R1", "R2"]
    assert c["path_a"]["factor"] == "48/1"
    assert c["path_b"]["rule_codes"] == ["R3"]
    assert c["path_b"]["factor"] == "12/1"

    # nothing from the rejected batch was stored
    rules = client.get("/api/v1/rules", params={"item_code": "SET-003"}).json()
    assert {r["rule_code"] for r in rules} == {"R1", "R2"}


def test_boundary_days_are_inclusive_and_touching_intervals_conflict(client):
    """首尾日生效：两段有效期在相邻日期交接，交界当天规则仍然有效。"""
    first = {
        "item_code": "SET-004",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-06-30")],
    }
    assert client.post("/api/v1/batches", json=first).json()["status"] == "accepted"

    # R2 starts exactly on R1's end day -> both active that day -> conflict
    second = {
        "item_code": "SET-004",
        "rules": [rule("R2", "件", "盒", 1, 10, "2026-06-30", "2026-12-31")],
    }
    body = client.post("/api/v1/batches", json=second).json()
    assert body["status"] == "rejected"
    assert body["conflict"]["conflict_date"] == "2026-06-30"

    # R1 still active on its end day
    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-004", "shipping_date": "2026-06-30",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    assert q["available"] is True and q["factor"] == "8/1"


def test_adjacent_non_overlapping_intervals_with_different_factors_are_ok(client):
    """有效期首尾相接但不重叠（前一天止、次日起）允许不同倍率。"""
    first = {
        "item_code": "SET-005",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-06-30")],
    }
    assert client.post("/api/v1/batches", json=first).json()["status"] == "accepted"

    second = {
        "item_code": "SET-005",
        "rules": [rule("R2", "件", "盒", 1, 10, "2026-07-01", "2026-12-31")],
    }
    body = client.post("/api/v1/batches", json=second).json()
    assert body["status"] == "accepted"

    before = client.get("/api/v1/conversion", params={
        "item_code": "SET-005", "shipping_date": "2026-06-30",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    after = client.get("/api/v1/conversion", params={
        "item_code": "SET-005", "shipping_date": "2026-07-01",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    assert before["factor"] == "8/1"
    assert after["factor"] == "10/1"


def test_no_available_conversion_returns_hint(client):
    payload = {
        "item_code": "SET-006",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31")],
    }
    client.post("/api/v1/batches", json=payload)

    # unit never mentioned
    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-006", "shipping_date": "2026-06-01",
        "from_unit": "件", "to_unit": "托",
    }).json()
    assert q["available"] is False and "没有可从" in q["message"]

    # outside validity window
    q2 = client.get("/api/v1/conversion", params={
        "item_code": "SET-006", "shipping_date": "2027-01-01",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    assert q2["available"] is False


def test_duplicate_rule_code_rejects_batch(client):
    payload = {
        "item_code": "SET-007",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31")],
    }
    client.post("/api/v1/batches", json=payload)
    again = {
        "item_code": "SET-007",
        "rules": [rule("R1", "件", "盒", 1, 9, "2026-01-01", "2026-12-31")],
    }
    body = client.post("/api/v1/batches", json=again).json()
    assert body["status"] == "rejected"
    assert "R1" in body["message"]


def test_invalid_payloads(client):
    bad_dates = {
        "item_code": "X",
        "rules": [rule("R1", "件", "盒", 1, 8, "2026-12-31", "2026-01-01")],
    }
    assert client.post("/api/v1/batches", json=bad_dates).status_code == 422

    bad_amount = {
        "item_code": "X",
        "rules": [rule("R1", "件", "盒", 0, 8, "2026-01-01", "2026-12-31")],
    }
    assert client.post("/api/v1/batches", json=bad_amount).status_code == 422

    dup = {
        "item_code": "X",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R1", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
        ],
    }
    assert client.post("/api/v1/batches", json=dup).status_code == 422


def test_equivalent_factor_different_notation_is_consistent(client):
    """2 件 = 16 盒 is the same factor as 1 件 = 8 盒 -> must be accepted."""
    payload = {
        "item_code": "SET-008",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R2", "件", "盒", 2, 16, "2026-01-01", "2026-12-31"),
        ],
    }
    assert client.post("/api/v1/batches", json=payload).json()["status"] == "accepted"


def test_conflict_inside_one_batch(client):
    payload = {
        "item_code": "SET-009",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R2", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"),
            rule("R3", "件", "套", 1, 50, "2026-01-01", "2026-12-31"),
        ],
    }
    body = client.post("/api/v1/batches", json=payload).json()
    assert body["status"] == "rejected"
    assert body["conflict"]["conflict_date"] == "2026-01-01"
    assert client.get("/api/v1/rules", params={"item_code": "SET-009"}).json() == []


def test_two_batches_submitted_concurrently_are_serialized(client):
    """同时送审：结果必须与按某一次序依次提交一致。

    两批都定义 件->盒 且倍率冲突。无论线程如何交错，恰好一批接受、一批退回；
    最终生效倍率是先拿到写锁那批的倍率。
    """
    batch_a = {
        "item_code": "SET-CONC",
        "rules": [rule("CA1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31")],
    }
    batch_b = {
        "item_code": "SET-CONC",
        "rules": [rule("CB1", "件", "盒", 1, 10, "2026-01-01", "2026-12-31")],
    }

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(client.post, "/api/v1/batches", json=b)
                for b in (batch_a, batch_b)]
        outcomes = [f.result().json() for f in futs]

    statuses = sorted(o["status"] for o in outcomes)
    assert statuses == ["accepted", "rejected"]

    rules = client.get("/api/v1/rules", params={"item_code": "SET-CONC"}).json()
    assert len(rules) == 1
    stored_factor = rules[0]["factor"]
    assert stored_factor in ("8/1", "10/1")

    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-CONC", "shipping_date": "2026-06-01",
        "from_unit": "件", "to_unit": "盒",
    }).json()
    assert q["factor"] == stored_factor

    # audit trail: one accepted + one rejected submission
    subs = client.get("/api/v1/submissions",
                      params={"item_code": "SET-CONC"}).json()
    assert sorted(s["status"] for s in subs) == ["accepted", "rejected"]


def test_two_compatible_batches_concurrently_both_accepted(client):
    batch_a = {
        "item_code": "SET-CONC2",
        "rules": [rule("DA1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31")],
    }
    batch_b = {
        "item_code": "SET-CONC2",
        "rules": [rule("DB1", "盒", "套", 1, 6, "2026-01-01", "2026-12-31")],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(client.post, "/api/v1/batches", json=b)
                for b in (batch_a, batch_b)]
        outcomes = [f.result().json() for f in futs]
    assert all(o["status"] == "accepted" for o in outcomes)

    q = client.get("/api/v1/conversion", params={
        "item_code": "SET-CONC2", "shipping_date": "2026-06-01",
        "from_unit": "件", "to_unit": "套",
    }).json()
    assert q["available"] is True and q["factor"] == "48/1"


def test_earliest_conflict_date_is_reported_not_first_rule_date(client):
    # R1 and R2 agree while both active; conflict only starts when R3 begins
    first = {
        "item_code": "SET-010",
        "rules": [
            rule("R1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
            rule("R2", "件", "盒", 1, 8, "2026-01-01", "2026-05-31"),
        ],
    }
    assert client.post("/api/v1/batches", json=first).json()["status"] == "accepted"

    second = {
        "item_code": "SET-010",
        "rules": [rule("R3", "件", "盒", 1, 9, "2026-09-01", "2026-12-31")],
    }
    body = client.post("/api/v1/batches", json=second).json()
    assert body["status"] == "rejected"
    # R1 runs all year, R3 starts Sep -> earliest conflict day is Sep 1
    assert body["conflict"]["conflict_date"] == "2026-09-01"
