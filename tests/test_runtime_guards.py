from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from conftest import TEST_ORIGIN, _test_database_url

pytestmark = pytest.mark.mysql


def make_app():
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(
        database_url=_test_database_url().render_as_string(hide_password=False),
        environment="test",
        allowed_origins=(TEST_ORIGIN,),
        cursor_signing_key="entry-test-cursor-key-32-bytes-long",
        local_http_cookie=True,
        rate_limit_enabled=False,
    ))


def test_app_database_sessions_are_utc_and_database_timestamps_match_utc(client, origin_headers, db_engine: Engine):
    with client.app.state.engine.connect() as connection:
        assert connection.scalar(text("SELECT @@session.time_zone")) == "+00:00"

    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    created = client.post(
        "/api/v1/trips", json={"name": "UTC 확인", "nickname": "나"},
        headers={**origin_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 201, created.text
    trip_id = created.json()["trip"]["id"]
    with client.app.state.engine.connect() as connection:
        seconds = connection.scalar(text("SELECT ABS(TIMESTAMPDIFF(SECOND, created_at, UTC_TIMESTAMP(3))) FROM trips WHERE id=:id"), {"id": trip_id})
    assert seconds < 2


def test_slow_database_request_does_not_block_an_unrelated_origin_rejection(client, app, origin_headers):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    query_started = threading.Event()

    def delay_session_select(conn, cursor, statement, parameters, context, executemany):
        if "FROM device_sessions WHERE credential_hash" in statement:
            query_started.set()
            time.sleep(0.8)

    event.listen(app.state.engine, "before_cursor_execute", delay_session_select)
    try:
        def slow_request():
            return client.get("/api/v1/device-session/trips", headers={"Origin": TEST_ORIGIN})

        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(slow_request)
            assert query_started.wait(timeout=2), "slow database query did not start"
            started = time.perf_counter()
            rejected = pool.submit(
                client.post,
                "/api/v1/trips",
                json={"name": "차단", "nickname": "나"},
                headers={"Origin": "https://invalid.example", "Idempotency-Key": str(uuid.uuid4())},
            ).result(timeout=0.5)
            elapsed = time.perf_counter() - started
            assert rejected.status_code == 403, rejected.text
            assert rejected.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
            assert elapsed < 0.5
            assert slow.result(timeout=2).status_code == 200
    finally:
        event.remove(app.state.engine, "before_cursor_execute", delay_session_select)


@pytest.mark.parametrize("override", ["host=example.invalid", "port=3306", "database=some_other_db"])
def test_test_db_provisioner_rejects_url_query_overrides_before_connect(monkeypatch, override):
    # Importing the validator is pure; it must reject SQLAlchemy URL query
    # options before a provisioner can create a database connection.
    from scripts.provision_test_db import checked_url

    base = _test_database_url().render_as_string(hide_password=False)
    monkeypatch.setenv("TEST_DATABASE_URL", f"{base}?{override}")
    with pytest.raises(SystemExit):
        checked_url()
