"""Pytest fixtures: isolated temp DB + FastAPI test client."""

import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp(prefix="conv-test-")
_db_path = os.path.join(_tmpdir, "test.db")
os.environ["DB_PATH"] = _db_path

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """Each test starts from an empty database."""
    conn = db.connect(_db_path)
    db.init_db(conn)
    conn.execute("DELETE FROM rules")
    conn.execute("DELETE FROM submissions")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    yield
    # Request-scoped connections opened by the app are closed when each
    # request finishes; nothing else lingers on this thread.



def rule(code, fu, tu, fa, ta, start, end):
    return {
        "rule_code": code,
        "from_unit": fu,
        "to_unit": tu,
        "from_amount": fa,
        "to_amount": ta,
        "start_date": start,
        "end_date": end,
    }
