import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_ORIGIN
from preparation_helpers import (DRAFT, SUGGESTION, base, confirm, create_room, headers,
                                 member_client, save_conditions, save_personal)

pytestmark = pytest.mark.mysql


def interpret(client, room, cv=0, pv=1, key=None):
    return client.post(base(room) + "/preferences/me/interpret", headers=headers(key=key), json={
        "expectedConditionsVersion": cv, "expectedPreferenceVersion": pv})


def test_ai_success_is_only_proposal_and_persists_idempotent_result_across_restart(client, app):
    from app.main import create_app

    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    assert confirm(client, room).status_code == 200
    before = client.get(base(room) + "/preferences/me").json()
    observed = []

    def provider(original, conditions):
        observed.append((original, conditions))
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = provider
    key = str(uuid.uuid4())
    response = interpret(client, room, key=key)
    assert response.status_code == 200, response.text
    assert response.json() == {"sourceConditionsVersion": 0, "sourcePreferenceVersion": 1,
                               **SUGGESTION, "clarifications": []}
    assert client.get(base(room) + "/preferences/me").json() == before
    assert observed[0][0] == DRAFT["originalText"]
    replay = interpret(client, room, key=key)
    assert replay.json() == response.json()
    assert len(observed) == 1
    restarted_app = create_app(app.state.settings)

    def forbidden_provider(*args):
        raise AssertionError("Replay must not call provider")

    restarted_app.state.preparation_interpreter = forbidden_provider
    with TestClient(restarted_app, base_url=TEST_ORIGIN) as restarted:
        restarted.cookies.update(client.cookies)
        replay = interpret(restarted, room, key=key)
        assert replay.status_code == 200, replay.text
        assert replay.json() == response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", ["shape", "extra", "enum", "walking", "needs_walking", "length", "control"])
def test_invalid_ai_output_is_not_returned_or_saved_and_key_can_retry(client, app, kind):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    bad = copy.deepcopy(SUGGESTION)
    if kind == "shape":
        bad = {"suggestions": []}
    elif kind == "extra":
        bad["suggestions"]["private"] = "not allowed"
    elif kind == "enum":
        bad["suggestions"]["avoidImportance"] = "maybe"
    elif kind == "walking":
        bad["suggestions"]["walking"] = "30"
    elif kind == "needs_walking":
        bad["suggestions"]["walking"] = None
    elif kind == "length":
        bad["suggestions"]["likes"] = "x" * 501
    else:
        bad["suggestions"]["food"] = "x\ty"
    app.state.preparation_interpreter = lambda *args: bad
    key = str(uuid.uuid4())
    before = client.get(base(room) + "/preferences/me").json()
    failed = interpret(client, room, key=key)
    assert failed.status_code == 502, failed.text
    assert failed.json()["error"]["code"] == "AI_INVALID_RESPONSE"
    assert client.get(base(room) + "/preferences/me").json() == before
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    assert interpret(client, room, key=key).status_code == 200


def test_provider_failure_no_private_error_no_cached_success(client, app, caplog):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    private_message = "synthetic_provider_secret_source_7219"

    def unavailable(*args):
        raise RuntimeError(private_message)

    app.state.preparation_interpreter = unavailable
    before = client.get(base(room) + "/preferences/me").json()
    failed = interpret(client, room)
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "AI_UNAVAILABLE"
    assert private_message not in failed.text
    assert private_message not in caplog.text
    assert DRAFT["originalText"] not in failed.text
    assert client.get(base(room) + "/preferences/me").json() == before


def test_original_required_and_versions_checked_before_provider(client, app):
    room = create_room(client)

    def forbidden_provider(*args):
        raise AssertionError("Provider must not run for invalid source")

    app.state.preparation_interpreter = forbidden_provider
    missing = interpret(client, room, pv=0)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "ORIGINAL_TEXT_REQUIRED"
    assert save_personal(client, room).status_code == 200
    conflict = interpret(client, room, pv=0)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    invalid_key = client.post(base(room) + "/preferences/me/interpret", headers={"Origin": TEST_ORIGIN}, json={
        "expectedConditionsVersion": 0, "expectedPreferenceVersion": 1})
    assert invalid_key.status_code == 400


@pytest.mark.parametrize("change", ["original", "manual", "common", "left", "closed", "revoked"])
def test_ai_releases_db_locks_and_rechecks_after_source_or_authorization_changes(client, app, db_engine, change):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    started = threading.Event()
    release = threading.Event()

    def delayed(*args):
        started.set()
        assert release.wait(timeout=8), "test failed to release provider"
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = delayed
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(interpret, client, room)
        try:
            assert started.wait(timeout=5)
            if change in {"original", "manual"}:
                response = save_personal(client, room, pv=1, **(
                    {"originalText": "새 원문"} if change == "original" else {"likes": "수동 수정"}))
                assert response.status_code == 200, response.text
            elif change == "common":
                response = save_conditions(client, room)
                assert response.status_code == 200, response.text
            else:
                with db_engine.begin() as conn:
                    if change == "left":
                        conn.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:id"), {"id": room["member"]["id"]})
                    elif change == "closed":
                        conn.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:id"), {"id": room["trip"]["id"]})
                    else:
                        conn.execute(text("UPDATE device_sessions SET revoked_at=UTC_TIMESTAMP(3)"))
        finally:
            release.set()
        response = future.result(timeout=8)
    expected_status = {"left": 403, "closed": 409, "revoked": 401}.get(change, 409)
    assert response.status_code == expected_status, response.text
    if change in {"original", "manual", "common"}:
        assert response.json()["error"]["code"] == "STALE_AI_RESULT"
    assert "suggestions" not in response.json()


def test_successful_ai_replay_noop_then_rechecks_changed_versions_and_input_fingerprint(client, app):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    calls = []

    def provider(*args):
        calls.append(True)
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = provider
    key = str(uuid.uuid4())
    first = interpret(client, room, key=key)
    assert first.status_code == 200, first.text
    assert first.json()["clarifications"] == []
    same_draft = save_personal(client, room, pv=1)
    assert same_draft.status_code == 200, same_draft.text
    assert same_draft.json()["preferenceVersion"] == 1
    replay = interpret(client, room, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(calls) == 1
    changed = save_personal(client, room, pv=1, likes="자연과 조용한 전시")
    assert changed.status_code == 200, changed.text
    assert changed.json()["preferenceVersion"] == 2
    stale = interpret(client, room, key=key)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_AI_RESULT"
    reused = interpret(client, room, pv=2, key=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(calls) == 1


def test_concurrent_identical_interpret_returns_in_progress_without_second_call(client, app):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    started = threading.Event()
    release = threading.Event()
    calls = []

    def delayed(*args):
        calls.append(True)
        started.set()
        assert release.wait(timeout=8)
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = delayed
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(interpret, client, room, key=key)
        try:
            assert started.wait(timeout=5)
            duplicate = interpret(client, room, key=key)
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
            assert int(duplicate.headers["retry-after"]) > 0
        finally:
            release.set()
        assert future.result(timeout=8).status_code == 200
    assert len(calls) == 1


def test_timeout_does_not_save_late_provider_result_and_same_key_can_retry(client, app):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    started = threading.Event()
    release = threading.Event()

    def delayed(*args):
        started.set()
        release.wait(timeout=5)
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = delayed
    app.state.preparation_ai_timeout = 0.05
    key = str(uuid.uuid4())
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(interpret, client, room, key=key)
            assert started.wait(timeout=5)
            response = future.result(timeout=2)
            assert response.status_code == 504, response.text
            assert response.json()["error"]["code"] == "AI_TIMEOUT"
    finally:
        release.set()
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    app.state.preparation_ai_timeout = 1
    retried = interpret(client, room, key=key)
    assert retried.status_code == 200, retried.text
    assert client.get(base(room) + "/preferences/me").json()["confirmation"]["status"] == "writing"


def test_interpret_rate_limit_is_five_per_minute(client, app):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    object.__setattr__(app.state.settings, "rate_limit_enabled", True)
    try:
        results = [interpret(client, room) for _ in range(5)]
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        limited = interpret(client, room)
        assert limited.status_code == 429, limited.text
        assert int(limited.headers["retry-after"]) > 0
    finally:
        object.__setattr__(app.state.settings, "rate_limit_enabled", False)


def test_idempotency_scope_uses_concrete_trip_path(client, app):
    first = create_room(client)
    second = create_room(client)
    assert save_personal(client, first).status_code == 200
    assert save_personal(client, second).status_code == 200
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    key = str(uuid.uuid4())
    assert interpret(client, first, key=key).status_code == 200
    response = interpret(client, second, key=key)
    assert response.status_code == 200, response.text


def test_expired_execution_lease_fences_late_provider_and_preserves_new_result(client, app, db_engine):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    started = threading.Event()
    release = threading.Event()

    def old_provider(*args):
        started.set()
        assert release.wait(timeout=8)
        return copy.deepcopy(SUGGESTION)

    app.state.preparation_interpreter = old_provider
    key = str(uuid.uuid4())
    new_result = copy.deepcopy(SUGGESTION)
    new_result["suggestions"]["likes"] = "새 실행 결과"
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(interpret, client, room, key=key)
        try:
            assert started.wait(timeout=5)
            with db_engine.begin() as conn:
                conn.execute(text("UPDATE preparation_interpret_requests SET lease_expires_at=DATE_SUB(UTC_TIMESTAMP(3), INTERVAL 1 SECOND) WHERE idempotency_key=:k"), {"k": key})
            app.state.preparation_interpreter = lambda *args: new_result
            replacement = interpret(client, room, key=key)
            assert replacement.status_code == 200, replacement.text
        finally:
            release.set()
        assert old.result(timeout=8).status_code == 409
    replay = interpret(client, room, key=key)
    assert replay.status_code == 200
    assert replay.json()["suggestions"]["likes"] == "새 실행 결과"


def test_session_interpret_limit_is_shared_across_its_different_memberships(client, app):
    rooms = [create_room(client) for _ in range(6)]
    for room in rooms:
        assert save_personal(client, room).status_code == 200
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    object.__setattr__(app.state.settings, "rate_limit_enabled", True)
    try:
        assert all(interpret(client, room).status_code == 200 for room in rooms[:5])
        limited = interpret(client, rooms[5])
        assert limited.status_code == 429, limited.text
        assert int(limited.headers["retry-after"]) > 0
    finally:
        object.__setattr__(app.state.settings, "rate_limit_enabled", False)


def test_member_interpret_limit_is_shared_across_its_authorized_sessions(client, app, db_engine):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    with TestClient(app, base_url=TEST_ORIGIN) as second_session:
        assert second_session.post("/api/v1/device-session", json={}, headers=headers()).status_code == 200
        digest = hashlib.sha256(second_session.cookies.get("osami_session").encode()).hexdigest()
        with db_engine.begin() as conn:
            session_id = conn.scalar(text("SELECT id FROM device_sessions WHERE credential_hash=:h"), {"h": digest})
            conn.execute(text("INSERT INTO device_session_memberships(session_id,trip_id,member_id,last_accessed_at) VALUES(:s,:t,:m,UTC_TIMESTAMP(3))"), {"s": session_id, "t": room["trip"]["id"], "m": room["member"]["id"]})
        object.__setattr__(app.state.settings, "rate_limit_enabled", True)
        try:
            for target in (client, second_session, client, second_session, client):
                response = interpret(target, room)
                assert response.status_code == 200, response.text
            limited = interpret(second_session, room)
            assert limited.status_code == 429, limited.text
        finally:
            object.__setattr__(app.state.settings, "rate_limit_enabled", False)


def test_default_adapter_reuses_public_ai_function_without_casting_v1_conditions(client, app, monkeypatch):
    import chungbuk_trip_ai

    room = create_room(client)
    response = save_conditions(client, room, transport="walk", lodging="숙박은 아직 결정하지 않음")
    assert response.status_code == 200
    conditions = response.json()["conditions"]
    assert save_personal(client, room, cv=1).status_code == 200
    observed = []

    def public_function(*, original_text, conditions):
        observed.append((original_text, conditions))
        return copy.deepcopy(SUGGESTION)

    monkeypatch.setattr(chungbuk_trip_ai, "interpret_personal_draft", public_function)
    response = interpret(client, room, cv=1)
    assert response.status_code == 200, response.text
    assert observed == [(DRAFT["originalText"], conditions)]


@pytest.mark.parametrize("error_name,status,code", [
    ("GeminiInvalidResponseError", 502, "AI_INVALID_RESPONSE"),
    ("GeminiUnavailableError", 503, "AI_UNAVAILABLE"),
    ("GeminiTimeoutError", 504, "AI_TIMEOUT"),
])
def test_public_ai_error_types_are_mapped_without_provider_detail(client, app, monkeypatch, error_name, status, code):
    import chungbuk_trip_ai

    room = create_room(client)
    assert save_personal(client, room).status_code == 200

    def failed_provider(**kwargs):
        raise getattr(chungbuk_trip_ai, error_name)("synthetic private provider detail")

    monkeypatch.setattr(chungbuk_trip_ai, "interpret_personal_draft", failed_provider)
    response = interpret(client, room)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert "synthetic private provider detail" not in response.text


def test_cached_private_proposal_is_bound_to_original_member_mapping(client, app, db_engine):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    app.state.preparation_interpreter = lambda *args: copy.deepcopy(SUGGESTION)
    key = str(uuid.uuid4())
    assert interpret(client, room, key=key).status_code == 200
    with member_client(app, room) as (other, member):
        assert save_personal(other, room, originalText="새 참여자 본인 원문").status_code == 200
        digest = hashlib.sha256(client.cookies.get("osami_session").encode()).hexdigest()
        with db_engine.begin() as conn:
            session_id = conn.scalar(text("SELECT id FROM device_sessions WHERE credential_hash=:h"), {"h": digest})
            conn.execute(text("UPDATE device_session_memberships SET member_id=:m WHERE session_id=:s AND trip_id=:t"), {"m": member["id"], "s": session_id, "t": room["trip"]["id"]})
        replay = interpret(client, room, key=key)
        assert replay.status_code == 409, replay.text
        assert "suggestions" not in replay.json()
