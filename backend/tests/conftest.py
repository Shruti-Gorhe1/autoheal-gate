"""Test fixtures.

Every test gets a fresh SQLite file and its own workspace root, so tests never
see each other's state and can run in parallel.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# This must happen at import time, before pytest collects any test module.
# A test module doing `from app.config import settings` at file scope would
# otherwise trigger the settings singleton with defaults, before a
# session-scoped fixture ever got a chance to run.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="autoheal-tests-"))
os.environ.update(
    {
        "AUTOHEAL_ENV_FILE": str(_TEST_ROOT / "absent.env"),
        "DATABASE_URL": f"sqlite:///{_TEST_ROOT / 'test.db'}",
        "SECRET_KEY": "test-secret-key",
        "WORKSPACE_ROOT": str(_TEST_ROOT / "workspaces"),
        "AGENT_ENGINE": "deterministic",
        "PHOENIX_ENABLED": "false",
        "SEED_DEMO_REPOSITORY": "false",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "GITHUB_CLIENT_ID": "test-client-id",
        "GITHUB_CLIENT_SECRET": "test-client-secret",
    }
)


@pytest.fixture(scope="session", autouse=True)
def _environment():
    yield _TEST_ROOT


@pytest.fixture(scope="session")
def app(_environment):
    from app.db.base import init_db
    from app.main import app as fastapi_app

    init_db()
    return fastapi_app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_ingested_events(_environment):
    """Keep webhook idempotency records isolated between tests.

    A duplicate-delivery test still exercises persistence within one test;
    this only prevents one test's synthetic webhook from becoming a duplicate
    of another test's fixture payload.
    """
    from sqlalchemy import delete

    from app.db.base import SessionLocal, init_db
    from app.db.models import IngestedEvent, JobRecord

    init_db()
    db = SessionLocal()
    try:
        db.execute(delete(IngestedEvent))
        db.execute(delete(JobRecord))
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture
def db():
    from app.db.base import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db):
    """A signed-up GitHub identity."""
    from app.auth.session_store import upsert_user

    return upsert_user(
        db,
        {
            "id": 4242,
            "login": "octocat",
            "name": "Mona Lisa",
            "email": "octocat@example.com",
            "avatar_url": "https://example.com/a.png",
        },
    )


@pytest.fixture
def auth_client(client, db, user):
    """A client carrying a valid session cookie, as a browser would."""
    from app.auth.session_store import create_session
    from app.config import settings

    session = create_session(db, user=user, access_token="gho_test_token")
    client.cookies.set(settings.session_cookie_name, session.id)
    return client


@pytest.fixture
def broken_repo(tmp_path):
    """A tiny repository whose test suite fails on an arithmetic bug."""
    repo = tmp_path / "calc-service"
    (repo / "tests").mkdir(parents=True)
    (repo / "calculator.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "from calculator import add, multiply\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "\n"
        "\n"
        "def test_multiply():\n"
        "    assert multiply(2, 3) == 6\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    return repo


FAILING_LOG = """COMMAND: pytest -q
EXIT CODE: 1

STDOUT:
============================= test session starts =============================
collected 2 items

tests/test_calculator.py F.                                              [100%]

================================== FAILURES ===================================
__________________________________ test_add ___________________________________

    def test_add():
>       assert add(2, 3) == 5
E       assert -1 == 5
E        +  where -1 = add(2, 3)

tests/test_calculator.py:5: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_calculator.py::test_add - assert -1 == 5
1 failed, 1 passed in 0.04s
"""


@pytest.fixture
def failing_log():
    return FAILING_LOG
