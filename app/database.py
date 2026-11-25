"""SQLite persistence for conversion rules and batch submissions."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Iterator, List, Optional, Sequence

from .validation import RuleRecord

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "conversion.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_code    TEXT NOT NULL,
    item_code    TEXT NOT NULL,
    from_unit    TEXT NOT NULL,
    to_unit      TEXT NOT NULL,
    factor_num   INTEGER NOT NULL,
    factor_den   INTEGER NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_code, rule_code)
);

CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    rule_codes  TEXT NOT NULL,
    conflict    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # WAL allows one writer plus concurrent readers; busy_timeout makes a
    # second writer wait on the write lock instead of failing immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # check_same_thread=False is safe here because connections are request
    # scoped (one connection per HTTP request, used by that request's single
    # worker thread) rather than shared across threads.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def request_connection() -> Iterator[sqlite3.Connection]:
    """Yield a fresh SQLite connection for one HTTP request, then close it.

    A request-scoped connection (instead of a thread-local one) avoids cross
    request state leaking through pool threads: worker threads are created and
    destroyed by the ASGI server, and a new thread may reuse the OS thread id
    of a dead one, which would make thread-local storage hand out the previous
    thread's connection and interleave two requests' transactions.

    Implemented as a plain generator so FastAPI can use it directly as a
    dependency (``Depends(request_connection)``).
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Open a write transaction immediately.

    ``BEGIN IMMEDIATE`` acquires SQLite's reserved write lock up front, so two
    concurrent batches are serialised: the second waits (up to busy_timeout)
    for the first to commit and then validates against the newly committed
    rules. The outcome is therefore identical to submitting the two batches in
    some sequential order.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # ROLLBACK can itself fail if the connection is already unusable;
        # preserve the original exception in that case.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def _row_to_record(row: sqlite3.Row) -> RuleRecord:
    return RuleRecord(
        rule_code=row["rule_code"],
        item_code=row["item_code"],
        from_unit=row["from_unit"],
        to_unit=row["to_unit"],
        factor=(row["factor_num"], row["factor_den"]),
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]),
    )


def get_rules(conn: sqlite3.Connection, item_code: str) -> List[RuleRecord]:
    rows = conn.execute(
        "SELECT * FROM rules WHERE item_code = ? ORDER BY rule_code",
        (item_code,),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def rule_code_exists(conn: sqlite3.Connection, item_code: str, code: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM rules WHERE item_code = ? AND rule_code = ? LIMIT 1",
        (item_code, code),
    ).fetchone()
    return row is not None


def insert_rule(conn: sqlite3.Connection, record: RuleRecord) -> None:
    conn.execute(
        """
        INSERT INTO rules (rule_code, item_code, from_unit, to_unit,
                           factor_num, factor_den, start_date, end_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.rule_code,
            record.item_code,
            record.from_unit,
            record.to_unit,
            record.factor[0],
            record.factor[1],
            record.start_date.isoformat(),
            record.end_date.isoformat(),
        ),
    )


def insert_submission(conn: sqlite3.Connection, item_code: str, status: str,
                      rule_codes: Sequence[str], conflict_json: Optional[str]) -> int:
    cur = conn.execute(
        """
        INSERT INTO submissions (item_code, status, rule_codes, conflict)
        VALUES (?, ?, ?, ?)
        """,
        (item_code, status, ",".join(rule_codes), conflict_json),
    )
    return int(cur.lastrowid)


def list_submissions(conn: sqlite3.Connection,
                     item_code: Optional[str] = None) -> List[sqlite3.Row]:
    if item_code is None:
        return conn.execute(
            "SELECT * FROM submissions ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM submissions WHERE item_code = ? ORDER BY id DESC LIMIT 100",
        (item_code,),
    ).fetchall()


def get_submission(conn: sqlite3.Connection, submission_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def active_rules_on(conn: sqlite3.Connection, item_code: str,
                    on_date: date) -> List[RuleRecord]:
    rows = conn.execute(
        """
        SELECT * FROM rules
        WHERE item_code = ? AND start_date <= ? AND end_date >= ?
        ORDER BY rule_code
        """,
        (item_code, on_date.isoformat(), on_date.isoformat()),
    ).fetchall()
    return [_row_to_record(row) for row in rows]
