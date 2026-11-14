import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_ORIGIN
from preparation_helpers import (CONDITIONS, DRAFT, base, confirm, create_room, headers,
                                 member_client, save_conditions, save_personal)

pytestmark = pytest.mark.mysql


def test_defaults_get_is_read_only_and_error_envelope(client, db_engine):
    room = create_room(client)
    prep = client.get(base(room) + "/preparation")
    assert prep.status_code == 200, prep.text
    data = prep.json()
    assert data["conditionsVersion"] == 0
    assert data["conditions"] == {**{key: None for key in CONDITIONS}, "departureTime": "09:00"}
    assert data["activeMemberCount"] == 1
    assert data["canEditConditions"] is True
    assert data["members"] == [{"id": room["member"]["id"], "nickname": "방장", "role": "OWNER", "preparationStatus": "writing"}]
    assert data["readiness"]["conditionsReady"] is False
    assert {item["code"] for item in data["readiness"]["reasons"]} == {"CONDITIONS_INCOMPLETE", "PREFERENCES_UNCONFIRMED"}
    personal = client.get(base(room) + "/preferences/me").json()
    assert personal["preferenceVersion"] == 0
    assert personal["draft"] == {"originalText": None, "likes": None, "avoid": None,
        "avoidImportance": "required", "food": None, "foodImportance": "preferred", "walking": None, "scheduleItems": []}
    assert personal["clarifications"] == []
    assert personal["confirmation"] == {"status": "writing", "confirmedConditionsVersion": None,
        "confirmedPreferenceVersion": None, "confirmedAt": None}
    assert prep.headers["cache-control"] == "no-store"
    assert "httponly" in prep.headers.get("set-cookie", "").lower()
    with db_engine.connect() as conn:
        for table in ("trip_preparation_documents", "member_preparation_documents"):
            assert conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) == 0


def test_private_data_owner_isolation_and_readiness_invalidation(client, app, caplog):
    room = create_room(client)
    assert save_conditions(client, room, expectedPeople=2).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    with member_client(app, room) as (other, member):
        assert save_conditions(other, room, 1).status_code == 403
        own = other.get(base(room) + "/preferences/me").json()
        assert own["draft"]["originalText"] is None
        private_text = "private_member_source_4837"
        assert save_personal(other, room, cv=1, originalText=private_text, likes="private_member_likes_9238").status_code == 200
        assert confirm(other, room, cv=1).status_code == 200
        prep = client.get(base(room) + "/preparation")
        assert prep.json()["readiness"]["conditionsReady"] is True
        assert prep.json()["members"][0]["role"] == "OWNER"
        assert all(set(item) == {"id", "nickname", "role", "preparationStatus"} for item in prep.json()["members"])
        assert private_text not in prep.text
        assert "private_member_likes_9238" not in prep.text
        assert client.get(base(room) + "/preferences/me").json()["draft"]["originalText"] == DRAFT["originalText"]
        bypass = client.get(base(room) + "/preferences/me", params={"memberId": member["id"]})
        assert private_text not in bypass.text
        assert save_personal(other, room, cv=1, pv=1, originalText=private_text).status_code == 200
        assert client.get(base(room) + "/preferences/me").json()["confirmation"]["status"] == "confirmed"
        assert other.get(base(room) + "/preferences/me").json()["confirmation"]["status"] == "writing"
        assert confirm(other, room, cv=1, pv=2).status_code == 200
        changed_conditions = save_conditions(client, room, 1, expectedPeople=3)
        assert changed_conditions.status_code == 200, changed_conditions.text
        assert changed_conditions.json()["conditionsVersion"] == 2
        assert all(m["preparationStatus"] == "writing" for m in client.get(base(room) + "/preparation").json()["members"])
        assert other.get(base(room) + "/preferences/me").json()["draft"]["originalText"] == private_text
        assert private_text not in caplog.text


def test_versions_are_cas_and_confirmation_is_idempotent(client):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    first = confirm(client, room)
    assert first.status_code == 200, first.text
    second = confirm(client, room)
    assert second.json()["confirmation"]["confirmedAt"] == first.json()["confirmation"]["confirmedAt"]
    before = client.get(base(room) + "/preferences/me").json()
    conflict = save_personal(client, room, cv=0, pv=0, likes="not_saved")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    assert conflict.json()["error"]["currentPreferenceVersion"] == 1
    assert client.get(base(room) + "/preferences/me").json() == before
    assert save_conditions(client, room).status_code == 200
    assert save_conditions(client, room, 0, region="not_saved").status_code == 409
    stale = confirm(client, room, cv=0)
    assert stale.status_code == 409
    assert client.get(base(room) + "/preferences/me").json()["confirmation"]["status"] == "writing"


def test_nonnegative_expected_version_outside_storage_range_is_a_conflict(client):
    room = create_room(client)
    response = save_conditions(client, room, 2 ** 80)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["currentConditionsVersion"] == 0


@pytest.mark.parametrize("field,value", [
    ("region", "x" * 101), ("region", "x\ny"), ("departure", "x\u2028y"),
    ("expectedPeople", True), ("expectedPeople", 0), ("expectedPeople", 51),
    ("budget", True), ("budget", 0), ("budget", 1000000001), ("budget", "100"),
    ("startDate", "2026-02-29"), ("startDate", "20261212"),
    ("endDate", None), ("endDate", "2026-12-11"), ("transport", "CAR"),
    ("lodging", "x" * 201), ("fixedSchedule", "x" * 501), ("unknown", 1),
])
def test_conditions_validation_no_partial_write(client, field, value):
    room = create_room(client)
    response = save_conditions(client, room, **{field: value})
    assert response.status_code == 400, response.text
    assert client.get(base(room) + "/preparation").json()["conditionsVersion"] == 0


@pytest.mark.parametrize("transport", ["car", "transit", "charter", "walk", None])
def test_text_conditions_transport_preserved_and_old_columns_untouched(client, db_engine, transport):
    room = create_room(client)
    response = save_conditions(client, room, transport=transport, lodging="  1박 희망  ", fixedSchedule="  오후 출발  ")
    assert response.status_code == 200, response.text
    conditions = response.json()["conditions"]
    assert conditions["transport"] == transport
    assert conditions["lodging"] == "1박 희망"
    assert conditions["fixedSchedule"] == "오후 출발"
    with db_engine.connect() as conn:
        old = conn.execute(text("SELECT * FROM trip_conditions WHERE trip_id=:id"), {"id": room["trip"]["id"]}).mappings().one()
    for key in ("region_codes", "start_date", "end_date", "party_size", "travel_mode", "budget_total_krw", "lodging_required", "fixed_events"):
        assert old[key] is None


@pytest.mark.parametrize("field,value", [("originalText", "x" * 5001), ("likes", "x" * 501),
    ("avoid", "x\ty"), ("food", "x\x85y"), ("walking", 20), ("walking", "30"),
    ("avoidImportance", "optional"), ("foodImportance", None), ("unknown", "x")])
def test_personal_validation_preserves_original(client, field, value):
    room = create_room(client)
    assert save_personal(client, room).status_code == 200
    before = client.get(base(room) + "/preferences/me").json()
    bad = save_personal(client, room, pv=1, **{field: value})
    assert bad.status_code == 400, bad.text
    assert client.get(base(room) + "/preferences/me").json() == before


def test_null_incomplete_draft_unicode_normalization_and_confirmation_requirements(client):
    room = create_room(client)
    incomplete = {"originalText": None, "likes": None, "avoid": None, "food": None, "walking": None}
    response = save_personal(client, room, **incomplete)
    assert response.status_code == 200, response.text
    preference_version = response.json()["preferenceVersion"]
    assert preference_version == client.get(base(room) + "/preferences/me").json()["preferenceVersion"]
    response = confirm(client, room, pv=preference_version)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PREFERENCES_INCOMPLETE"
    assert set(response.json()["error"]["fieldErrors"]) == {"originalText", "likes", "walking"}
    response = save_personal(client, room, pv=preference_version, originalText="  원문\r\n두번째 줄  ", likes=" ")
    assert response.status_code == 200, response.text
    preference_version = response.json()["preferenceVersion"]
    data = client.get(base(room) + "/preferences/me").json()
    assert data["draft"]["originalText"] == "원문\n두번째 줄"
    assert data["draft"]["likes"] is None
    assert confirm(client, room, pv=preference_version, reviewed=False).status_code == 400
    response = client.post(base(room) + "/preferences/me/confirm", json={"expectedConditionsVersion": 0, "expectedPreferenceVersion": preference_version}, headers=headers())
    assert response.status_code == 400


def test_auth_origin_media_inactive_closed_and_other_trip(client, app, db_engine):
    room = create_room(client)
    with TestClient(app, base_url=TEST_ORIGIN) as anonymous:
        assert anonymous.get(base(room) + "/preparation").status_code == 401
        assert anonymous.get(base(room) + "/preferences/me").status_code == 401
    assert client.get(f"/api/v1/trips/{uuid.uuid4()}/preparation").status_code == 404
    assert client.put(base(room) + "/conditions", json={}, headers={"Origin": "https://forbidden.example"}).status_code == 403
    assert client.put(base(room) + "/conditions", content="{}", headers={"Origin": TEST_ORIGIN, "Content-Type": "text/plain"}).status_code == 415
    with member_client(app, room) as (other, member):
        other_room = create_room(other)
        assert client.get(base(other_room) + "/preferences/me").status_code == 404
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:id"), {"id": member["id"]})
        assert other.get(base(room) + "/preferences/me").status_code == 403
        assert client.get(base(room) + "/preparation").json()["activeMemberCount"] == 1
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:id"), {"id": room["trip"]["id"]})
    assert client.get(base(room) + "/preparation").status_code == 409
    assert save_personal(client, room).status_code == 409


def test_restart_restores_conditions_private_draft_and_confirmation(client, app):
    from app.main import create_app

    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    expected = client.get(base(room) + "/preferences/me").json()
    with TestClient(create_app(app.state.settings), base_url=TEST_ORIGIN) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(base(room) + "/preferences/me").json() == expected
        assert restarted.get(base(room) + "/preparation").json()["readiness"]["conditionsReady"] is True


def test_new_participant_does_not_block_owner_preparation_readiness(client, app):
    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200
    assert client.get(base(room) + "/preparation").json()["readiness"]["conditionsReady"] is True
    with member_client(app, room):
        prep = client.get(base(room) + "/preparation").json()
        assert prep["readiness"]["conditionsReady"] is True
        assert prep["activeMemberCount"] == 2
        assert prep["readiness"]["reasons"] == []


def test_normalized_condition_and_preference_noops_preserve_versions_and_confirmation(client):
    room = create_room(client)
    assert save_conditions(client, room).status_code == 200
    assert save_personal(client, room, cv=1).status_code == 200
    assert confirm(client, room, cv=1).status_code == 200

    same_conditions = save_conditions(client, room, version=1)
    assert same_conditions.status_code == 200, same_conditions.text
    assert same_conditions.json()["conditionsVersion"] == 1
    assert same_conditions.json()["requiresReconfirmation"] is False

    same_draft = save_personal(client, room, cv=1, pv=1)
    assert same_draft.status_code == 200, same_draft.text
    assert same_draft.json()["preferenceVersion"] == 1
    assert same_draft.json()["confirmation"]["status"] == "confirmed"


def test_required_distance_walk_fails_confirmation_without_mutating_then_pure_duration_confirms(client):
    room = create_room(client)
    original = "장거리 도보 거리는 2km 이하로 해 주세요. 장거리 도보는 피하고 싶어요."
    saved = save_personal(client, room, originalText=original,
                          avoid="장거리 도보는 피하고 싶어요", walking="20")
    assert saved.status_code == 200, saved.text
    before = client.get(base(room) + "/preferences/me").json()

    rejected = confirm(client, room, pv=1)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "PREFERENCES_INCOMPLETE"
    assert "walking" in rejected.json()["error"]["fieldErrors"]
    after = client.get(base(room) + "/preferences/me").json()
    assert after["preferenceVersion"] == before["preferenceVersion"] == 1
    assert after["confirmation"] == before["confirmation"]
    assert after["confirmation"]["status"] == "writing"

    corrected = save_personal(client, room, pv=1,
                              originalText="오래 걷는 것은 싫어요",
                              avoid="오래 걷는 것은 싫어요", walking="20")
    assert corrected.status_code == 200, corrected.text
    confirmed = confirm(client, room, pv=2)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmation"]["status"] == "confirmed"


@pytest.mark.parametrize("method,suffix,payload", [
    ("GET", "/preparation", None), ("GET", "/preferences/me", None),
    ("PUT", "/conditions", {"expectedConditionsVersion": 0, "conditions": CONDITIONS}),
    ("PUT", "/preferences/me", {"expectedConditionsVersion": 0, "expectedPreferenceVersion": 0, "draft": DRAFT}),
    ("POST", "/preferences/me/interpret", {"expectedConditionsVersion": 0, "expectedPreferenceVersion": 0}),
    ("POST", "/preferences/me/confirm", {"expectedConditionsVersion": 0, "expectedPreferenceVersion": 0, "reviewed": True}),
])
def test_all_six_paths_recheck_auth_inactive_and_closed(client, app, db_engine, method, suffix, payload):
    room = create_room(client)
    url = base(room) + suffix
    kwargs = {"headers": headers()}
    if payload is not None:
        kwargs["json"] = payload
    with TestClient(app, base_url=TEST_ORIGIN) as anonymous:
        response = anonymous.request(method, url, **kwargs)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "SESSION_REQUIRED"
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE trip_members SET status='LEFT' WHERE id=:id"), {"id": room["member"]["id"]})
    response = client.request(method, url, **kwargs)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "MEMBERSHIP_INACTIVE"
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE trip_members SET status='ACTIVE' WHERE id=:id"), {"id": room["member"]["id"]})
        conn.execute(text("UPDATE trips SET status='CLOSED' WHERE id=:id"), {"id": room["trip"]["id"]})
    response = client.request(method, url, **kwargs)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TRIP_CLOSED"
