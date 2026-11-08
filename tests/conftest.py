"""Fixtures for the travel-entry contract tests.

The MySQL fixtures intentionally read only TEST_DATABASE_URL.  They never load
the application's dotenv/configuration, and reject any target other than the
dedicated local test schema before opening a connection.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from fastapi.testclient import TestClient


EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 3317
EXPECTED_DATABASE = "osami_entry_test"
TEST_ORIGIN = "http://127.0.0.1:8000"


def _test_database_url() -> URL:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("MySQL contract suite requires TEST_DATABASE_URL")
    try:
        url = make_url(raw)
    except Exception:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy URL")

    # Reject socket, aliases, remote hosts, and any database except the private
    # per-task schema.  Do not include the URL in errors because it may contain
    # credentials.
    safe = (
        url.drivername in {"mysql+pymysql", "mysql"}
        and url.host == EXPECTED_HOST
        and url.port == EXPECTED_PORT
        and url.database == EXPECTED_DATABASE
        and not url.query
    )
    if not safe:
        pytest.fail(
            "Refusing MySQL tests: TEST_DATABASE_URL must target "
            "127.0.0.1:3317/osami_entry_test"
        )
    return url


@pytest.fixture(scope="session")
def mysql_url() -> URL:
    return _test_database_url()


@pytest.fixture(scope="session")
def db_engine(mysql_url: URL) -> Iterator[Engine]:
    engine = create_engine(mysql_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    try:
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT VERSION()"))
            assert version and str(version).startswith("8."), "Tests require MySQL 8"
            schema = connection.scalar(text("SELECT DATABASE()"))
            assert schema == EXPECTED_DATABASE
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_dedicated_test_schema(request: pytest.FixtureRequest) -> Iterator[None]:
    """Clear isolated test rows in FK order, before and after each DB case."""
    if "mysql" not in request.node.keywords:
        yield
        return
    # Resolve the validated URL before connecting.  This fixture must not use
    # application settings or create baseline tables.
    url = _test_database_url()
    engine = create_engine(url, pool_pre_ping=True)
    try:
        _delete_test_rows(engine)
        yield
        _delete_test_rows(engine)
    finally:
        engine.dispose()


def _delete_test_rows(engine: Engine) -> None:
    # Tests run only against this dedicated empty schema.  These are data
    # deletes, never DDL; baseline tables are retained unchanged.
    with engine.begin() as connection:
        for table in (
            "preparation_interpret_requests",
            "member_preparation_documents",
            "trip_preparation_documents",
            "api_idempotency",
            "device_session_memberships",
            "device_sessions",
            "trip_conditions",
            "trip_members",
            "trips",
        ):
            connection.execute(text(f"DELETE FROM `{table}`"))


@pytest.fixture
def app():
    # Lazy import lets pytest report a clean skip when the implementation has
    # not landed yet, without reading .env or contacting a database.
    from app.config import Settings
    from app.main import create_app

    return create_app(
        Settings(
            database_url=_test_database_url().render_as_string(hide_password=False),
            environment="test",
            allowed_origins=(TEST_ORIGIN,),
            cursor_signing_key="entry-test-cursor-key-32-bytes-long",
            local_http_cookie=True,
            rate_limit_enabled=False,
        )
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, base_url=TEST_ORIGIN) as test_client:
        yield test_client


@pytest.fixture
def origin_headers() -> dict[str, str]:
    return {"Origin": TEST_ORIGIN, "Content-Type": "application/json"}


@pytest.fixture
def bootstrapped(client: TestClient, origin_headers: dict[str, str]) -> TestClient:
    response = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert response.status_code == 200, response.text
    return client
