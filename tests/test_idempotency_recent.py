from __future__ import annotations

import uuid
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import TEST_ORIGIN, _test_database_url

pytestmark = pytest.mark.mysql


def key() -> str:
    return str(uuid.uuid4())


def start_app():
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(
        database_url=_test_database_url().render_as_string(hide_password=False),
        environment="test",
        allowed_origins=(TEST_ORIGIN,),
        cursor_signing_key="entry-test-cursor-key-32-bytes-long",
        local_http_cookie=True,
        rate_limit_enabled=False,
    ))
    return app


def post_create(client: TestClient, headers: dict[str, str], idem: str, name: str = "친구 여행"):
    return client.post(
        "/api/v1/trips",
        json={"name": name, "nickname": "나"},
        headers={**headers, "Idempotency-Key": idem},
    )


def test_idempotency_replays_exact_status_and_body_without_duplicate_rows(client, origin_headers, db_engine: Engine):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    idem = key()
    first = post_create(client, origin_headers, idem)
    replay = post_create(client, origin_headers, idem)
    assert first.status_code == 201, first.text
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()

    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM trip_members")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM device_session_memberships")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM api_idempotency")) == 1


def test_idempotency_rejects_different_normalized_input_and_missing_or_bad_key(client, bootstrapped, origin_headers):
    idem = key()
    first = post_create(bootstrapped, origin_headers, idem, "원래 방")
    assert first.status_code == 201, first.text
    mismatch = post_create(bootstrapped, origin_headers, idem, "다른 방")
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    missing = bootstrapped.post("/api/v1/trips", json={"name": "새 방", "nickname": "나"}, headers=origin_headers)
    assert missing.status_code == 400, missing.text
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"
    malformed = bootstrapped.post("/api/v1/trips", json={"name": "새 방", "nickname": "나"}, headers={**origin_headers, "Idempotency-Key": "not-a-uuid"})
    assert malformed.status_code == 400, malformed.text


def test_idempotent_replay_after_app_restart(client, origin_headers, db_engine: Engine):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    idem = key()
    first = post_create(client, origin_headers, idem)
    assert first.status_code == 201, first.text
    cookie_header = client.cookies.get("osami_session")
    if cookie_header is None:
        # The implementation may retain the production cookie name in local
        # mode; copy the actual issued cookie without depending on internals.
        cookie_header = client.cookies.get("__Host-osami_session")
    assert cookie_header

    # A fresh app/client simulates process restart while keeping only the
    # browser's cookie.  Durable result lookup must return the original DTO.
    restarted = start_app()
    with TestClient(restarted, base_url=TEST_ORIGIN) as after_restart:
        replay = after_restart.post(
            "/api/v1/trips", json={"name": "친구 여행", "nickname": "나"},
            headers={**origin_headers, "Idempotency-Key": idem, "Cookie": f"osami_session={cookie_header}"},
        )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 1


def test_replay_checks_current_membership_and_trip_state(client, bootstrapped, origin_headers, db_engine: Engine):
    idem = key()
    first = post_create(bootstrapped, origin_headers, idem)
    assert first.status_code == 201, first.text
    result = first.json()
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:trip"), {"trip": result["trip"]["id"]})
    replay = post_create(bootstrapped, origin_headers, idem)
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "TRIP_CLOSED"
    assert result["member"]["id"] not in replay.text


def test_concurrent_same_key_create_returns_one_durable_result(client, origin_headers, db_engine: Engine):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    idem = key()
    # The browser cookie is copied into independent clients so requests truly
    # overlap instead of being serialized by one TestClient instance.
    cookie = client.cookies.get("osami_session") or client.cookies.get("__Host-osami_session")
    assert cookie

    def request_once():
        with TestClient(start_app(), base_url=TEST_ORIGIN) as concurrent_client:
            return concurrent_client.post(
                "/api/v1/trips", json={"name": "친구 여행", "nickname": "나"},
                headers={**origin_headers, "Idempotency-Key": idem, "Cookie": f"osami_session={cookie}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: request_once(), range(2)))
    successful = [response for response in responses if response.status_code == 201]
    busy = [response for response in responses if response.status_code == 409]
    assert len(successful) == 1 or len(successful) == 2, [r.text for r in responses]
    assert len(successful) + len(busy) == 2, [r.text for r in responses]
    for response in busy:
        assert response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
        assert response.headers.get("retry-after") == "1"
    if busy:
        retry = post_create(client, origin_headers, idem)
        assert retry.status_code == 201, retry.text
        assert retry.json() == successful[0].json()
    else:
        assert successful[0].json() == successful[1].json()
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM trip_members")) == 1


def test_named_lock_contention_returns_request_in_progress(client, origin_headers, db_engine: Engine):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    idem = key()
    with db_engine.connect() as observer:
        session_id = observer.scalar(text("SELECT id FROM device_sessions ORDER BY created_at DESC LIMIT 1"))
        lock_name = hashlib.sha256(f"{session_id}|POST|/api/v1/trips|{idem}".encode()).hexdigest()[:60]
        assert observer.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}) == 1
        observer.commit()
        busy = post_create(client, origin_headers, idem)
        assert busy.status_code == 409, busy.text
        assert busy.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
        assert busy.headers.get("retry-after") == "1"
        assert observer.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}) == 1
        observer.commit()

    retry = post_create(client, origin_headers, idem)
    assert retry.status_code == 201, retry.text
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 1


def test_recent_trip_list_counts_members_dates_and_does_not_touch_ordering(client, origin_headers, db_engine: Engine):
    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    first = post_create(client, origin_headers, key(), "첫 번째 방").json()
    second = post_create(client, origin_headers, key(), "두 번째 방").json()
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trip_conditions SET start_date='2027-05-01', end_date='2027-05-03' WHERE trip_id=:trip"), {"trip": first["trip"]["id"]})
        connection.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:member"), {"member": second["member"]["id"]})

    before = client.get("/api/v1/device-session/trips?limit=10", headers={"Origin": TEST_ORIGIN})
    assert before.status_code == 200, before.text
    items = before.json()["items"]
    by_id = {item["tripId"]: item for item in items}
    assert by_id[first["trip"]["id"]]["startDate"] == "2027-05-01"
    assert by_id[first["trip"]["id"]]["endDate"] == "2027-05-03"
    assert by_id[first["trip"]["id"]]["activeMemberCount"] == 1
    assert by_id[second["trip"]["id"]]["startDate"] is None
    assert by_id[second["trip"]["id"]]["endDate"] is None
    assert by_id[second["trip"]["id"]]["canReconnect"] is False
    assert "inviteCode" not in before.text
    assert "reconnect_token_hash" not in before.text

    with db_engine.connect() as connection:
        old = connection.execute(text("SELECT trip_id,last_accessed_at FROM device_session_memberships ORDER BY trip_id")).all()
    after = client.get("/api/v1/device-session/trips?limit=10", headers={"Origin": TEST_ORIGIN})
    assert after.status_code == 200, after.text
    with db_engine.connect() as connection:
        new = connection.execute(text("SELECT trip_id,last_accessed_at FROM device_session_memberships ORDER BY trip_id")).all()
    assert old == new


def test_recent_trip_cursor_paginates_tamper_rejected_and_session_scoped(client, bootstrapped, origin_headers):
    results = [post_create(bootstrapped, origin_headers, key(), f"방 {i}").json() for i in range(3)]
    first = bootstrapped.get("/api/v1/device-session/trips?limit=1", headers={"Origin": TEST_ORIGIN})
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 1
    cursor = first.json()["nextCursor"]
    assert cursor
    second = bootstrapped.get("/api/v1/device-session/trips?limit=1&cursor=" + cursor, headers={"Origin": TEST_ORIGIN})
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert first.json()["items"][0]["tripId"] != second.json()["items"][0]["tripId"]

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    bad = bootstrapped.get("/api/v1/device-session/trips?limit=1&cursor=" + tampered, headers={"Origin": TEST_ORIGIN})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"

    from app.config import Settings
    from app.main import create_app
    from conftest import _test_database_url

    separate_app = create_app(Settings(
        database_url=_test_database_url().render_as_string(hide_password=False), environment="test",
        allowed_origins=(TEST_ORIGIN,), cursor_signing_key="entry-test-cursor-key-32-bytes-long",
        local_http_cookie=True, rate_limit_enabled=False,
    ))
    with TestClient(separate_app, base_url=TEST_ORIGIN) as separate:
        separate.post("/api/v1/device-session", json={}, headers=origin_headers)
        isolated = separate.get("/api/v1/device-session/trips?limit=1&cursor=" + cursor, headers={"Origin": TEST_ORIGIN})
    assert isolated.status_code == 400, isolated.text
    assert isolated.json()["error"]["code"] == "INVALID_CURSOR"


def test_reconnect_updates_last_access_without_creating_a_member(client, bootstrapped, origin_headers, db_engine: Engine):
    result = post_create(bootstrapped, origin_headers, key()).json()
    member_id = result["member"]["id"]
    with db_engine.begin() as connection:
        before_count = connection.scalar(text("SELECT COUNT(*) FROM trip_members"))
        connection.execute(text("UPDATE device_session_memberships SET last_accessed_at='2020-01-01 00:00:00' WHERE trip_id=:trip"), {"trip": result["trip"]["id"]})
    reconnect = bootstrapped.post(f"/api/v1/trips/{result['trip']['id']}/reconnect", json={}, headers=origin_headers)
    assert reconnect.status_code == 200, reconnect.text
    with db_engine.connect() as connection:
        after_count = connection.scalar(text("SELECT COUNT(*) FROM trip_members"))
        last = connection.scalar(text("SELECT last_accessed_at FROM device_session_memberships WHERE trip_id=:trip"), {"trip": result["trip"]["id"]})
        persisted_member = connection.scalar(text("SELECT id FROM trip_members WHERE id=:id"), {"id": member_id})
    assert after_count == before_count == 1
    assert persisted_member == member_id
    assert str(last)[:4] != "2020"


def test_distinct_keys_same_device_join_one_member(client, bootstrapped, origin_headers, db_engine: Engine):
    # An independent device owns the target room.
    other = start_app()
    with TestClient(other, base_url=TEST_ORIGIN) as owner:
        owner.post("/api/v1/device-session", json={}, headers=origin_headers)
        created = owner.post("/api/v1/trips", json={"name": "초대 방", "nickname": "방장"}, headers={**origin_headers, "Idempotency-Key": key()})
    assert created.status_code == 201, created.text
    code = created.json()["trip"]["inviteCode"]

    first = client.post("/api/v1/trip-memberships", json={"inviteCode": code, "nickname": "참여자"}, headers={**origin_headers, "Idempotency-Key": key()})
    second = client.post("/api/v1/trip-memberships", json={"inviteCode": code, "nickname": "참여자"}, headers={**origin_headers, "Idempotency-Key": key()})
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["member"]["id"] == second.json()["member"]["id"]
    assert first.json()["member"]["role"] == second.json()["member"]["role"] == "MEMBER"
    assert first.json()["alreadyJoined"] is False and second.json()["alreadyJoined"] is True
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trip_members WHERE trip_id=:trip"), {"trip": created.json()["trip"]["id"]}) == 2


def test_failed_domain_write_rolls_back_and_same_key_can_retry(app, client, bootstrapped, origin_headers, db_engine: Engine):
    from sqlalchemy import event

    fired = {"value": False}

    def fail_member_insert(conn, cursor, statement, parameters, context, executemany):
        if not fired["value"] and statement.lstrip().upper().startswith("INSERT INTO TRIP_MEMBERS"):
            fired["value"] = True
            raise RuntimeError("injected test write failure")

    event.listen(app.state.engine, "before_cursor_execute", fail_member_insert)
    idem = key()
    try:
        failed = post_create(bootstrapped, origin_headers, idem)
        assert failed.status_code == 500, failed.text
        assert failed.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "injected test" not in failed.text
        with db_engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 0
            assert connection.scalar(text("SELECT COUNT(*) FROM trip_members")) == 0
            assert connection.scalar(text("SELECT COUNT(*) FROM device_session_memberships")) == 0
            assert connection.scalar(text("SELECT COUNT(*) FROM api_idempotency")) == 0
    finally:
        event.remove(app.state.engine, "before_cursor_execute", fail_member_insert)

    retry = post_create(bootstrapped, origin_headers, idem)
    assert retry.status_code == 201, retry.text
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trips")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM trip_members")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM api_idempotency")) == 1


def test_replay_uses_original_response_snapshot_after_member_changes(client, bootstrapped, origin_headers, db_engine: Engine):
    idem = key()
    first = post_create(bootstrapped, origin_headers, idem)
    assert first.status_code == 201, first.text
    snapshot = first.json()
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trip_members SET nickname='수정 후 이름' WHERE id=:member"), {"member": snapshot["member"]["id"]})
    replay = post_create(bootstrapped, origin_headers, idem)
    assert replay.status_code == 201, replay.text
    assert replay.json() == snapshot
    assert replay.json()["member"]["nickname"] != "수정 후 이름"


def test_replay_rejects_left_membership(client, bootstrapped, origin_headers, db_engine: Engine):
    idem = key()
    first = post_create(bootstrapped, origin_headers, idem)
    assert first.status_code == 201, first.text
    response = first.json()
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:member"), {"member": response["member"]["id"]})
    replay = post_create(bootstrapped, origin_headers, idem)
    assert replay.status_code == 403, replay.text
    assert replay.json()["error"]["code"] == "MEMBERSHIP_INACTIVE"
    assert response["member"]["id"] not in replay.text
