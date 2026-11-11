from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.canvas_api import register_canvas_routes


TRIP = "00000000-0000-4000-8000-000000000001"
DAY = "00000000-0000-4000-8000-000000000002"
MEMBER = "00000000-0000-4000-8000-000000000003"
SESSION = "00000000-0000-4000-8000-000000000004"
PLACE = "00000000-0000-4000-8000-000000000005"


class ApiError(Exception):
    def __init__(self, status, code, message, fields=None, retry=None):
        self.status, self.code, self.message, self.fields, self.retry = status, code, message, fields or {}, retry


@pytest.fixture
def setup_app():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as c:
        c.execute(text("CREATE TABLE trips(id TEXT,status TEXT,name TEXT,invite_code TEXT)"))
        c.execute(text("CREATE TABLE trip_preparation_documents(trip_id TEXT,conditions_json TEXT,conditions_version INTEGER)"))
        c.execute(text("CREATE TABLE trip_members(id TEXT,trip_id TEXT,nickname TEXT,role TEXT,status TEXT)"))
        c.execute(text("CREATE TABLE device_session_memberships(session_id TEXT,trip_id TEXT,member_id TEXT)"))
        c.execute(text("CREATE TABLE places(contentid INTEGER,title TEXT,contentTypeName TEXT,planningRole TEXT,adminCity TEXT,addr1 TEXT,addr2 TEXT,mapx REAL,mapy REAL,geoValidated TEXT,tel TEXT,homepageUrl TEXT,overviewText TEXT,firstimage TEXT)"))
        c.execute(text("CREATE TABLE place_details(contentid INTEGER,openingHoursText TEXT,closedDaysText TEXT,feeText TEXT,bookingText TEXT,parkingText TEXT,menuText TEXT,estimatedStayMinutes INTEGER)"))
        c.execute(text("CREATE TABLE trip_canvas_documents(trip_id TEXT PRIMARY KEY,revision INTEGER,schema_version INTEGER,document_json TEXT,source_plan_id TEXT,source_version_id TEXT,created_at DATETIME,updated_at DATETIME)"))
        c.execute(text("CREATE TABLE trip_canvas_requests(session_id TEXT,trip_id TEXT,member_id TEXT,kind TEXT,method TEXT,path TEXT,idempotency_key TEXT,fingerprint TEXT,status TEXT,lease_token TEXT,lease_expires_at DATETIME,result_json TEXT,created_at DATETIME,updated_at DATETIME,expires_at DATETIME,PRIMARY KEY(session_id,method,path,idempotency_key))"))
        c.execute(text("INSERT INTO trips VALUES(:t,'OPEN','trip','code')"), {"t": TRIP})
        c.execute(text("INSERT INTO trip_members VALUES(:m,:t,'Member','MEMBER','ACTIVE')"), {"m": MEMBER, "t": TRIP})
        c.execute(text("INSERT INTO device_session_memberships VALUES(:s,:t,:m)"), {"s": SESSION, "t": TRIP, "m": MEMBER})
        c.execute(text("INSERT INTO places VALUES(101,'Place','Museum','activity','City','Addr',NULL,127.5,36.6,'yes','010','javascript:alert(1)','overview','https://example.test/place.jpg')"))
        c.execute(text("INSERT INTO place_details VALUES(101,'Hours','Closed','Fee','Booking','Parking','Menu',75)"))

    def find_session(conn, request):
        return {"id": SESSION} if request.cookies.get("session") == "ok" else None

    def cookie_name(): return "session"
    def origin_check(request):
        if request.headers.get("origin") != "http://test": raise ApiError(403, "ORIGIN_NOT_ALLOWED", "bad")

    async def body_json(request, allowed):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json": raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "bad")
        data = await request.json()
        if not isinstance(data, dict) or set(data) - allowed: raise ApiError(400, "VALIDATION_ERROR", "bad")
        return data

    app = FastAPI()
    @app.exception_handler(ApiError)
    def api_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)
    register_canvas_routes(app, engine, find_session=find_session, cookie_name=cookie_name,
                           origin_check=origin_check, body_json=body_json, ApiError=ApiError,
                           set_session_cookie=lambda *args: None)
    client = TestClient(app, base_url="http://test")
    client.cookies.set("session", "ok")
    yield client, engine
    client.close()
    engine.dispose()


def test_get_hydrates_allowlisted_place_and_requires_membership(setup_app):
    client, _ = setup_app
    response = client.get(f"/api/v1/trips/{TRIP}/canvas")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == 0 and result["document"]["schemaVersion"] == 3
    client.cookies.clear()
    assert client.get(f"/api/v1/trips/{TRIP}/canvas").status_code == 401


def test_save_cas_and_same_key_replay(setup_app):
    client, _ = setup_app
    day = "00000000-0000-4000-8000-000000000006"
    node = "00000000-0000-4000-8000-000000000007"
    document = {"schemaVersion": 3, "days": [{"id": day, "date": None}], "nodes": [
        {"id": node, "kind": "place", "dayId": day, "position": {"x": 1, "y": 1}, "color": "default", "contentid": 101, "intent": "candidate"}], "edges": []}
    headers = {"Origin": "http://test", "Idempotency-Key": str(uuid.uuid4())}
    body = {"expectedRevision": 0, "document": document}
    response = client.put(f"/api/v1/trips/{TRIP}/canvas", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    replay = client.put(f"/api/v1/trips/{TRIP}/canvas", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    stale = client.put(f"/api/v1/trips/{TRIP}/canvas", json={**body, "expectedRevision": 0}, headers={**headers, "Idempotency-Key": str(uuid.uuid4())})
    assert stale.status_code == 409
    detail = client.get(f"/api/v1/trips/{TRIP}/canvas/places/101", headers={"X-Canvas-Version": "4"}).json()
    assert detail["homepageUrl"] is None
    assert detail["imageUrl"] == "https://example.test/place.jpg"
    assert detail["estimatedStayMinutes"] == 75
    legacy_detail = client.get(f"/api/v1/trips/{TRIP}/canvas/places/101").json()
    assert "imageUrl" not in legacy_detail


def test_v4_place_metadata_is_gated_by_canvas_version_header(setup_app):
    client, _ = setup_app
    legacy = client.get(f"/api/v1/trips/{TRIP}/canvas/places/101")
    modern = client.get(f"/api/v1/trips/{TRIP}/canvas/places/101", headers={"X-Canvas-Version": "4"})
    assert "imageUrl" not in legacy.json()
    assert modern.json()["imageUrl"] == "https://example.test/place.jpg"


def test_v4_typed_price_labels_use_matching_role_and_keep_zero_explicit(setup_app):
    client, engine = setup_app
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE restaurant_details(place_id INTEGER,avg_price_krw INTEGER)"))
        conn.execute(text("CREATE TABLE tourism_details(place_id INTEGER,admission_fee_krw INTEGER)"))
        conn.execute(text("CREATE TABLE lodging_details(place_id INTEGER,estimated_nightly_krw INTEGER,price_basis_people INTEGER)"))
        conn.execute(text("INSERT INTO restaurant_details VALUES(101,0)"))
        conn.execute(text("INSERT INTO tourism_details VALUES(101,2500)"))
        conn.execute(text("INSERT INTO lodging_details VALUES(101,120000,2)"))
        conn.execute(text("UPDATE place_details SET feeText=NULL WHERE contentid=101"))
    url = f"/api/v1/trips/{TRIP}/canvas/places/101"
    headers = {"X-Canvas-Version": "4"}
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET planningRole='meal_stop' WHERE contentid=101"))
    assert client.get(url, headers=headers).json()["priceLabel"] == "1인 약 0원"
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET planningRole='sightseeing_stop' WHERE contentid=101"))
    assert client.get(url, headers=headers).json()["priceLabel"] == "1인 입장료 2,500원"
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET planningRole='overnight_lodging' WHERE contentid=101"))
    assert client.get(url, headers=headers).json()["priceLabel"] == "1박 120,000원 (기준 2명)"
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET planningRole='meal_stop' WHERE contentid=101"))
        conn.execute(text("UPDATE place_details SET feeText='가격 문의' WHERE contentid=101"))
    assert client.get(url, headers=headers).json()["priceLabel"] is None


def test_v4_empty_day_links_persist_and_matching_revision_downgrade_is_rejected(setup_app):
    client, engine = setup_app
    day = "00000000-0000-4000-8000-000000000006"
    document = {"schemaVersion": 4, "days": [{"id": day, "date": None, "position": {"x": -50, "y": 250}}],
                "nodes": [], "edges": [], "dayLinks": []}
    headers = {"Origin": "http://test", "Idempotency-Key": str(uuid.uuid4())}
    saved = client.put(f"/api/v1/trips/{TRIP}/canvas", json={"expectedRevision": 0, "document": document}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["document"] == document
    with engine.connect() as conn:
        row = conn.execute(text("SELECT schema_version,document_json FROM trip_canvas_documents WHERE trip_id=:t"), {"t": TRIP}).mappings().first()
        assert row["schema_version"] == 4
        assert json.loads(row["document_json"])["dayLinks"] == []
    legacy = {"schemaVersion": 3, "days": [{"id": day, "date": None}], "nodes": [], "edges": []}
    rejected = client.put(f"/api/v1/trips/{TRIP}/canvas", json={"expectedRevision": 1, "document": legacy},
                          headers={"Origin": "http://test", "Idempotency-Key": str(uuid.uuid4())})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "SCHEMA_VERSION_CONFLICT"
    stale = client.put(f"/api/v1/trips/{TRIP}/canvas", json={"expectedRevision": 0, "document": legacy},
                       headers={"Origin": "http://test", "Idempotency-Key": str(uuid.uuid4())})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_recommendation_final_auth_blocks_replay_and_paid_duplicate(setup_app):
    calls = []
    client, engine = setup_app
    def provider(prompt, pool):
        calls.append((prompt, pool))
        with engine.begin() as c:
            c.execute(text("UPDATE trip_members SET status='INACTIVE' WHERE id=:m"), {"m": MEMBER})
        return {"contentid": pool[0]["contentid"], "alternatives": []}
    client.app.state.canvas_recommender = provider
    # Find a prompt day from initial canvas.
    day = client.get(f"/api/v1/trips/{TRIP}/canvas").json()["document"]["days"][0]["id"]
    key = str(uuid.uuid4())
    result = client.post(f"/api/v1/trips/{TRIP}/canvas/recommendations", json={"prompt": "museum", "dayId": day, "excludeContentids": []},
                         headers={"Origin": "http://test", "Idempotency-Key": key})
    assert result.status_code == 403 and len(calls) == 1
    assert set(calls[0][1][0]) <= {"contentid", "title", "category", "planningRole", "adminCity", "address", "overviewText", "menuText", "coordinate"}
    assert "private" not in calls[0][0].lower()
    with engine.connect() as c:
        saved_request = c.execute(text("SELECT fingerprint,result_json FROM trip_canvas_requests WHERE idempotency_key=:k"), {"k": key}).mappings().first()
        assert saved_request["result_json"] is None
        assert len(saved_request["fingerprint"]) == 64
    again = client.post(f"/api/v1/trips/{TRIP}/canvas/recommendations", json={"prompt": "museum", "dayId": day, "excludeContentids": []},
                        headers={"Origin": "http://test", "Idempotency-Key": key})
    assert again.status_code == 403 and len(calls) == 1


def test_successful_recommendation_same_key_replays_without_provider_call(setup_app):
    calls = []
    client, _ = setup_app
    class Limiter:
        calls = 0
        def take(self, key, maximum):
            self.calls += 1
            assert maximum == 5
            return None
    limiter = Limiter()
    client.app.state.limiter = limiter
    client.app.state.canvas_recommender = lambda prompt, pool: (calls.append(prompt) or {"contentid": pool[0]["contentid"], "alternatives": []})
    day = client.get(f"/api/v1/trips/{TRIP}/canvas").json()["document"]["days"][0]["id"]
    headers = {"Origin": "http://test", "Idempotency-Key": str(uuid.uuid4())}
    body = {"prompt": "nearby museum", "dayId": day, "excludeContentids": []}
    first = client.post(f"/api/v1/trips/{TRIP}/canvas/recommendations", json=body, headers=headers)
    replay = client.post(f"/api/v1/trips/{TRIP}/canvas/recommendations", json=body, headers=headers)
    assert first.status_code == 200 and replay.status_code == 200
    assert first.json() == replay.json() and len(calls) == 1
    assert limiter.calls == 1


@pytest.mark.parametrize("region", ["충북", "충청북도", "충북 전체", "충북 청주", "충북 청주시", "청주시"])
def test_catalog_recommendation_recognizes_actual_frontend_region_values(setup_app, region):
    from app.canvas_api import _catalog_pool
    _, engine = setup_app
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET adminCity='청주시',addr1='충청북도 청주시 테스트 주소'"))
    with engine.connect() as conn:
        pool = _catalog_pool(conn, set(), region=region, prompt="Place")
    assert [place["contentid"] for place in pool] == [101]


def test_catalog_recommendation_does_not_substitute_another_city(setup_app):
    from app.canvas_api import _catalog_pool
    _, engine = setup_app
    with engine.begin() as conn:
        conn.execute(text("UPDATE places SET adminCity='청주시',addr1='충청북도 청주시 테스트 주소'"))
    with engine.connect() as conn:
        assert _catalog_pool(conn, set(), region="충북 단양", prompt="Place") == []


@pytest.mark.parametrize("invalid_id", ["unknown", "1.5", "1e3", "2147483648"])
def test_place_detail_invalid_path_uses_the_common_error_envelope(setup_app, invalid_id):
    client, _ = setup_app
    result = client.get(f"/api/v1/trips/{TRIP}/canvas/places/{invalid_id}")
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "VALIDATION_ERROR"
