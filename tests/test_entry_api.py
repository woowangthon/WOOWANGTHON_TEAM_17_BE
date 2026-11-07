from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import TEST_ORIGIN, _test_database_url


pytestmark = pytest.mark.mysql


def key() -> str:
    return str(uuid.uuid4())


def err(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", body["error"]["requestId"])
    assert response.headers.get("cache-control") == "no-store"


def create_trip(client: TestClient, headers: dict[str, str], *, name="단양 주말 여행", nickname="여행자 A", idem=None):
    return client.post(
        "/api/v1/trips",
        json={"name": name, "nickname": nickname},
        headers={**headers, "Idempotency-Key": idem or key()},
    )


def test_create_bootstrap_and_initial_conditions_are_undecided(client, origin_headers, db_engine: Engine):
    boot = client.post("/api/v1/device-session", json={}, headers=origin_headers)
    assert boot.status_code == 200, boot.text
    assert "expiresAt" in boot.json()
    cookie = boot.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "max-age=2592000" in cookie
    assert "domain=" not in cookie

    response = create_trip(client, origin_headers)
    assert response.status_code == 201, response.text
    body = response.json()
    trip, member = body["trip"], body["member"]
    assert body["alreadyJoined"] is False
    assert set(trip) == {"id", "name", "status", "inviteCode"}
    assert set(member) == {"id", "nickname", "role", "status"}
    assert trip["name"] == "단양 주말 여행" and trip["status"] == "OPEN"
    assert re.fullmatch(r"CB-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{10}", trip["inviteCode"])
    assert member["role"] == "OWNER" and member["status"] == "ACTIVE"
    assert member["nickname"] == "여행자 A"
    assert "reconnect_token_hash" not in response.text and "credential" not in response.text
    assert response.headers.get("cache-control") == "no-store"

    # Assert persisted values through a fresh connection, independent of the
    # request's transaction/connection and ORM implementation.
    with db_engine.connect() as connection:
        persisted = connection.execute(
            text("SELECT name, status FROM trips WHERE id=:id"), {"id": trip["id"]}
        ).mappings().one()
        member_row = connection.execute(
            text("SELECT trip_id, nickname, role, status, active_owner_trip_id FROM trip_members WHERE id=:id"),
            {"id": member["id"]},
        ).mappings().one()
        condition = connection.execute(
            text("SELECT region_codes,start_date,end_date,start_point,end_point,party_size,travel_mode,budget_total_krw,lodging_required,fixed_events FROM trip_conditions WHERE trip_id=:id"),
            {"id": trip["id"]},
        ).mappings().one()
    assert persisted["name"] == "단양 주말 여행" and persisted["status"] == "OPEN"
    assert member_row["trip_id"] == trip["id"]
    assert member_row["nickname"] == "여행자 A" and member_row["role"] == "OWNER"
    assert member_row["active_owner_trip_id"] == trip["id"]
    assert all(value is None for value in condition.values())


def test_create_requires_session_and_uses_normalized_trimmed_values(client, origin_headers):
    response = create_trip(client, origin_headers)
    err(response, 401, "SESSION_REQUIRED")

    client.post("/api/v1/device-session", json={}, headers=origin_headers)
    response = create_trip(client, origin_headers, name="  😀친구 여행😀  ", nickname="  나  ")
    assert response.status_code == 201, response.text
    assert response.json()["trip"]["name"] == "😀친구 여행😀"
    assert response.json()["member"]["nickname"] == "나"


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"name": "  ", "nickname": "나"}, "name"),
        ({"name": "x" * 41, "nickname": "나"}, "name"),
        ({"name": "여행", "nickname": "x" * 21}, "nickname"),
        ({"name": "여행\n방", "nickname": "나"}, "name"),
        ({"name": "여행\u2028방", "nickname": "나"}, "name"),
        ({"name": "여행", "nickname": "나\x7f"}, "nickname"),
        ({"name": "여행", "nickname": "나\u0085"}, "nickname"),
        ({"name": 3, "nickname": "나"}, "name"),
        ({"name": "여행", "nickname": None}, "nickname"),
    ],
)
def test_create_validation_boundaries(client, bootstrapped, origin_headers, payload, field):
    response = bootstrapped.post(
        "/api/v1/trips", json=payload,
        headers={**origin_headers, "Idempotency-Key": key()},
    )
    err(response, 400, "VALIDATION_ERROR")
    assert field in response.json()["error"].get("fieldErrors", {})


@pytest.mark.parametrize("payload", [
    {"name": "여행", "nickname": "나", "role": "OWNER"},
    {"name": "여행", "nickname": "나", "memberId": str(uuid.uuid4())},
    {"name": "여행", "nickname": "나", "status": "ACTIVE"},
    {"name": "여행", "nickname": "나", "reconnectTokenHash": "bad"},
])
def test_create_rejects_authority_and_unknown_fields(client, bootstrapped, origin_headers, payload):
    response = bootstrapped.post(
        "/api/v1/trips", json=payload,
        headers={**origin_headers, "Idempotency-Key": key()},
    )
    err(response, 400, "VALIDATION_ERROR")


def test_invite_join_normalizes_code_and_reuses_existing_member_without_renaming(
    client, bootstrapped, origin_headers, db_engine: Engine
):
    created = create_trip(bootstrapped, origin_headers, nickname="처음 이름")
    assert created.status_code == 201, created.text
    info = created.json()
    code = info["trip"]["inviteCode"]

    # Same device already has ACTIVE membership; validation and normalization
    # still run, but the original member/nickname are returned.
    joined = bootstrapped.post(
        "/api/v1/trip-memberships",
        json={"inviteCode": f" {code.lower()} ", "nickname": "변경 이름"},
        headers={**origin_headers, "Idempotency-Key": key()},
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["alreadyJoined"] is True
    assert joined.json()["member"] == info["member"]
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM trip_members WHERE trip_id=:trip"), {"trip": info["trip"]["id"]}) == 1


def test_distinct_device_can_join_and_unknown_code_is_generic(client, bootstrapped, origin_headers):
    created = create_trip(bootstrapped, origin_headers)
    assert created.status_code == 201, created.text
    code = created.json()["trip"]["inviteCode"]

    # A separate client gets an isolated cookie/session.
    from app.config import Settings
    from app.main import create_app

    second_app = create_app(Settings(
        database_url=_test_database_url().render_as_string(hide_password=False), environment="test",
        allowed_origins=(TEST_ORIGIN,), cursor_signing_key="entry-test-cursor-key-32-bytes-long",
        local_http_cookie=True, rate_limit_enabled=False,
    ))
    with TestClient(second_app, base_url=TEST_ORIGIN) as second:
        second.post("/api/v1/device-session", json={}, headers=origin_headers)
        joined = second.post("/api/v1/trip-memberships", json={"inviteCode": code, "nickname": "여행자 B"}, headers={**origin_headers, "Idempotency-Key": key()})
    assert joined.status_code == 201, joined.text
    assert joined.json()["alreadyJoined"] is False
    assert joined.json()["member"]["role"] == "MEMBER"

    unknown = bootstrapped.post("/api/v1/trip-memberships", json={"inviteCode": "missing", "nickname": "나"}, headers={**origin_headers, "Idempotency-Key": key()})
    err(unknown, 404, "INVITE_NOT_FOUND")


@pytest.mark.parametrize("code", ["", "x" * 21, "한글", "A B", "A_1", "A\n1"])
def test_join_invite_code_format_validation(client, bootstrapped, origin_headers, code):
    response = bootstrapped.post("/api/v1/trip-memberships", json={"inviteCode": code, "nickname": "나"}, headers={**origin_headers, "Idempotency-Key": key()})
    err(response, 400, "VALIDATION_ERROR")


def test_post_origin_content_type_body_and_request_id_contract(client, bootstrapped, origin_headers):
    headers = {"Idempotency-Key": key()}
    denied = bootstrapped.post("/api/v1/trips", json={"name": "여행", "nickname": "나"}, headers={**headers, "Origin": "https://evil.invalid"})
    err(denied, 403, "ORIGIN_NOT_ALLOWED")
    missing = bootstrapped.post("/api/v1/trips", json={"name": "여행", "nickname": "나"}, headers=headers)
    err(missing, 403, "ORIGIN_NOT_ALLOWED")
    unsupported = bootstrapped.post("/api/v1/trips", content=b"{}", headers={"Origin": TEST_ORIGIN, "Content-Type": "text/plain", "Idempotency-Key": key()})
    err(unsupported, 415, "UNSUPPORTED_MEDIA_TYPE")


def test_reconnect_requires_current_access_and_preserves_owner(client, bootstrapped, origin_headers, db_engine: Engine):
    created = create_trip(bootstrapped, origin_headers)
    info = created.json()
    path = f"/api/v1/trips/{info['trip']['id']}/reconnect"
    reconnect = bootstrapped.post(path, json={}, headers=origin_headers)
    assert reconnect.status_code == 200, reconnect.text
    assert reconnect.json()["alreadyJoined"] is True
    assert reconnect.json()["member"] == info["member"]

    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:member"), {"member": info["member"]["id"]})
    inactive = bootstrapped.post(path, json={}, headers=origin_headers)
    err(inactive, 403, "MEMBERSHIP_INACTIVE")


def test_closed_trip_blocks_reconnect_and_join(client, bootstrapped, origin_headers, db_engine: Engine):
    created = create_trip(bootstrapped, origin_headers)
    info = created.json()
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:trip"), {"trip": info["trip"]["id"]})
    reconnect = bootstrapped.post(f"/api/v1/trips/{info['trip']['id']}/reconnect", json={}, headers=origin_headers)
    err(reconnect, 409, "TRIP_CLOSED")
    join = bootstrapped.post("/api/v1/trip-memberships", json={"inviteCode": info["trip"]["inviteCode"], "nickname": "다른 이름"}, headers={**origin_headers, "Idempotency-Key": key()})
    err(join, 409, "TRIP_CLOSED")


def test_reconnect_hides_other_session_and_unknown_trip_equally(client, bootstrapped, origin_headers):
    trip_id = str(uuid.uuid4())
    unknown = bootstrapped.post(f"/api/v1/trips/{trip_id}/reconnect", json={}, headers=origin_headers)
    err(unknown, 404, "TRIP_NOT_ACCESSIBLE")


def test_all_success_and_error_responses_are_no_store(client, bootstrapped, origin_headers):
    response = bootstrapped.get("/api/v1/device-session/trips", headers={"Origin": TEST_ORIGIN})
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
