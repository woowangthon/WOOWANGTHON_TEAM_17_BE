from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import EXPECTED_DATABASE, TEST_ORIGIN, _test_database_url

pytestmark = pytest.mark.mysql


def app_for(environment="test", origin=TEST_ORIGIN, local_cookie=True, rate_limit_enabled=False):
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(
        database_url=_test_database_url().render_as_string(hide_password=False),
        environment=environment,
        allowed_origins=(origin,),
        cursor_signing_key="entry-test-cursor-key-32-bytes-long",
        local_http_cookie=local_cookie,
        rate_limit_enabled=rate_limit_enabled,
    ))


def success_headers(origin=TEST_ORIGIN):
    return {"Origin": origin, "Content-Type": "application/json"}


def test_missing_expired_and_revoked_sessions_are_rejected(client, origin_headers, db_engine: Engine):
    no_cookie = client.get("/api/v1/device-session/trips", headers={"Origin": TEST_ORIGIN})
    assert no_cookie.status_code == 401
    assert no_cookie.json()["error"]["code"] == "SESSION_REQUIRED"

    bootstrap = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert bootstrap.status_code == 200, bootstrap.text
    with db_engine.begin() as connection:
        session_id = connection.scalar(text("SELECT id FROM device_sessions ORDER BY created_at DESC LIMIT 1"))
        connection.execute(text("UPDATE device_sessions SET expires_at=DATE_SUB(UTC_TIMESTAMP(3), INTERVAL 1 SECOND) WHERE id=:id"), {"id": session_id})
    expired = client.get("/api/v1/device-session/trips", headers={"Origin": TEST_ORIGIN})
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "SESSION_REQUIRED"

    # The still-present expired cookie is replaced with a new empty session by
    # bootstrap; revoking that session immediately blocks all authenticated use.
    renewed = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert renewed.status_code == 200, renewed.text
    with db_engine.begin() as connection:
        session_id = connection.scalar(text("SELECT id FROM device_sessions ORDER BY created_at DESC LIMIT 1"))
        connection.execute(text("UPDATE device_sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE id=:id"), {"id": session_id})
    revoked = client.get("/api/v1/device-session/trips", headers={"Origin": TEST_ORIGIN})
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "SESSION_REQUIRED"


def test_production_cookie_is_host_secure_http_only_and_slides_on_valid_request(db_engine: Engine):
    origin = "https://entry.example"
    app = app_for(environment="production", origin=origin, local_cookie=False)
    with TestClient(app, base_url=origin) as client:
        headers = success_headers(origin)
        boot = client.post("/api/v1/device-session", json={}, headers=headers)
        assert boot.status_code == 200, boot.text
        set_cookie = boot.headers.get("set-cookie", "")
        lower = set_cookie.lower()
        assert set_cookie.startswith("__Host-osami_session=")
        assert "secure" in lower and "httponly" in lower and "samesite=lax" in lower
        assert "path=/" in lower and "max-age=2592000" in lower
        assert "domain=" not in lower

        with db_engine.begin() as connection:
            session_id = connection.scalar(text("SELECT id FROM device_sessions ORDER BY created_at DESC LIMIT 1"))
            connection.execute(text("UPDATE device_sessions SET expires_at=DATE_ADD(UTC_TIMESTAMP(3), INTERVAL 1 DAY) WHERE id=:id"), {"id": session_id})
        current = client.get("/api/v1/device-session/trips", headers={"Origin": origin})
        assert current.status_code == 200, current.text
        refreshed = current.headers.get("set-cookie", "").lower()
        assert "__host-osami_session=" in refreshed
        assert "secure" in refreshed and "httponly" in refreshed and "samesite=lax" in refreshed
        with db_engine.connect() as connection:
            expires = connection.scalar(text("SELECT expires_at FROM device_sessions WHERE id=:id"), {"id": session_id})
        days_left = (expires - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 86400
        assert days_left > 29


def test_app_startup_does_not_create_or_change_schema(db_engine: Engine):
    with db_engine.connect() as connection:
        before = connection.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=:db ORDER BY TABLE_NAME"
        ), {"db": EXPECTED_DATABASE}).scalars().all()
        baseline = {
            name: connection.execute(text(f"SHOW CREATE TABLE `{name}`")).first()[1]
            for name in ("trips", "trip_members", "trip_conditions")
        }
    app = app_for()
    assert app.state.engine is not None
    with TestClient(app, base_url=TEST_ORIGIN):
        pass
    with db_engine.connect() as connection:
        after = connection.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=:db ORDER BY TABLE_NAME"
        ), {"db": EXPECTED_DATABASE}).scalars().all()
        baseline_after = {
            name: connection.execute(text(f"SHOW CREATE TABLE `{name}`")).first()[1]
            for name in ("trips", "trip_members", "trip_conditions")
        }
    assert before == after
    assert baseline == baseline_after


def _run_sql_script(connection, path: Path) -> None:
    contents = path.read_text(encoding="utf-8")
    # The migration files contain simple MySQL DDL statements and no stored
    # program delimiters.  Strip whole-line comments, then execute statements.
    sql = "\n".join(line for line in contents.splitlines() if not line.lstrip().startswith("--"))
    for statement in sql.split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement.strip())


def test_entry_migration_down_up_preserves_exact_baseline_ddl(db_engine: Engine):
    migration_dir = Path(__file__).resolve().parents[1] / "migrations"
    up = migration_dir / "001_device_entry.up.sql"
    down = migration_dir / "001_device_entry.down.sql"
    assert up.is_file() and down.is_file()
    baseline_names = ("trips", "trip_members", "trip_conditions")

    def snapshot() -> dict[str, str]:
        with db_engine.connect() as connection:
            return {name: connection.execute(text(f"SHOW CREATE TABLE `{name}`")).first()[1] for name in baseline_names}

    before = snapshot()
    with db_engine.connect() as connection:
        _run_sql_script(connection, down)
        assert connection.scalar(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=:db AND TABLE_NAME IN ('device_sessions','device_session_memberships','api_idempotency')"), {"db": EXPECTED_DATABASE}) == 0
        _run_sql_script(connection, up)
    assert snapshot() == before


def test_cookie_is_not_rotated_for_an_existing_live_session(client, origin_headers):
    first = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert first.status_code == 200, first.text
    original = client.cookies.get("osami_session")
    reused = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert reused.status_code == 200, reused.text
    assert client.cookies.get("osami_session") == original
    assert "set-cookie" in reused.headers


def test_create_rate_limit_uses_ten_per_minute_and_retry_after():
    app = app_for(rate_limit_enabled=True)
    with TestClient(app, base_url=TEST_ORIGIN) as client:
        headers = success_headers()
        assert client.post("/api/v1/device-session", json={}, headers=headers).status_code == 200
        responses = [client.post("/api/v1/trips", json={"name": f"방 {i}", "nickname": "나"}, headers=headers | {"Idempotency-Key": f"00000000-0000-4000-8000-{i:012d}"}) for i in range(10)]
        assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
        limited = client.post("/api/v1/trips", json={"name": "열한 번째", "nickname": "나"}, headers=headers | {"Idempotency-Key": "00000000-0000-4000-8000-000000000011"})
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert re.fullmatch(r"[1-9][0-9]*", limited.headers.get("retry-after", ""))


def test_join_rate_limit_uses_thirty_per_minute_and_retry_after():
    app = app_for(rate_limit_enabled=True)
    with TestClient(app, base_url=TEST_ORIGIN) as client:
        headers = success_headers()
        assert client.post("/api/v1/device-session", json={}, headers=headers).status_code == 200
        responses = [client.post("/api/v1/trip-memberships", json={"inviteCode": "NO-SUCH-CODE", "nickname": "나"}, headers=headers | {"Idempotency-Key": f"10000000-0000-4000-8000-{i:012d}"}) for i in range(30)]
        assert all(response.status_code == 404 for response in responses), [r.text for r in responses]
        limited = client.post("/api/v1/trip-memberships", json={"inviteCode": "NO-SUCH-CODE", "nickname": "나"}, headers=headers | {"Idempotency-Key": "10000000-0000-4000-8000-000000000031"})
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert re.fullmatch(r"[1-9][0-9]*", limited.headers.get("retry-after", ""))


def test_bootstrap_rate_limit_uses_thirty_per_minute_and_retry_after():
    app = app_for(rate_limit_enabled=True)
    with TestClient(app, base_url=TEST_ORIGIN) as client:
        headers = success_headers()
        responses = []
        for _ in range(30):
            client.cookies.clear()  # each request has no prior session cookie
            responses.append(client.post("/api/v1/device-session", json={}, headers=headers))
        assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
        client.cookies.clear()
        limited = client.post("/api/v1/device-session", json={}, headers=headers)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert re.fullmatch(r"[1-9][0-9]*", limited.headers.get("retry-after", ""))
