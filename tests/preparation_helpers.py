from contextlib import contextmanager
import uuid

from fastapi.testclient import TestClient

from conftest import TEST_ORIGIN


CONDITIONS = {
    "region": "충북 단양", "startDate": "2026-12-12", "endDate": "2026-12-13",
    "expectedPeople": 1, "budget": 600000, "transport": "charter",
    "departure": "청주역", "returnRoute": "청주역", "lodging": "조용한 숙소",
    "fixedSchedule": "둘째 날 저녁 복귀", "departureTime": "09:00", "departureContentid": None,
}
DRAFT = {
    "originalText": "테스트용 개인 원문. 자연과 조용한 카페를 좋아합니다.",
    "likes": "자연과 카페", "avoid": "오래 걷는 것은 싫어요", "avoidImportance": "required",
    "food": "맵지 않은 음식", "foodImportance": "preferred", "walking": "20", "scheduleItems": [],
}
SUGGESTION = {"suggestions": {key: value for key, value in DRAFT.items() if key != "originalText"},
              "needsWalkingAnswer": False}


def headers(*, key=None):
    return {"Origin": TEST_ORIGIN, "Content-Type": "application/json",
            "Idempotency-Key": key or str(uuid.uuid4())}


def create_room(client):
    assert client.post("/api/v1/device-session", json={}, headers=headers()).status_code == 200
    response = client.post("/api/v1/trips", json={"name": "합성 검증 여행", "nickname": "방장"}, headers=headers())
    assert response.status_code == 201, response.text
    return response.json()


def base(room):
    return f"/api/v1/trips/{room['trip']['id']}"


def save_conditions(client, room, version=0, **overrides):
    return client.put(base(room) + "/conditions", headers=headers(), json={
        "expectedConditionsVersion": version, "conditions": CONDITIONS | overrides})


def save_personal(client, room, cv=0, pv=0, **overrides):
    return client.put(base(room) + "/preferences/me", headers=headers(), json={
        "expectedConditionsVersion": cv, "expectedPreferenceVersion": pv,
        "draft": DRAFT | overrides})


def confirm(client, room, cv=0, pv=1, **overrides):
    return client.post(base(room) + "/preferences/me/confirm", headers=headers(), json={
        "expectedConditionsVersion": cv, "expectedPreferenceVersion": pv,
        "reviewed": True} | overrides)


@contextmanager
def member_client(app, room, nickname="참여자"):
    with TestClient(app, base_url=TEST_ORIGIN) as client:
        assert client.post("/api/v1/device-session", json={}, headers=headers()).status_code == 200
        response = client.post("/api/v1/trip-memberships", headers=headers(), json={
            "inviteCode": room["trip"]["inviteCode"], "nickname": nickname})
        assert response.status_code == 201, response.text
        yield client, response.json()["member"]
