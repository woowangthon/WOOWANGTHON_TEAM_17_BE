from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.canvas_api import register_canvas_routes
from app.canvas_routes import RouteCache


TRIP = "00000000-0000-4000-8000-000000000001"
MEMBER = "00000000-0000-4000-8000-000000000003"
SESSION = "00000000-0000-4000-8000-000000000004"


class ApiError(Exception):
    def __init__(self, status, code, message, fields=None, retry=None):
        self.status, self.code, self.message, self.fields, self.retry = status, code, message, fields or {}, retry


class FakeRouter:
    def __init__(self, engine=None, *, status="verified"):
        self.engine = engine
        self.status = status
        self.calls = []
        self.on_call = None

    def route(self, origin, destination, mode):
        self.calls.append((origin.copy(), destination.copy(), mode))
        if self.on_call:
            self.on_call()
        if self.status == "unknown":
            return {"mode": mode, "status": "unknown", "reasonCodes": ["ROUTE_NOT_FOUND"]}
        return {"mode": mode, "status": "verified", "durationSeconds": 321,
                "distanceMeters": 654, "knownCostKrw": None, "costComplete": False,
                "walkingSeconds": None, "geometry": [], "reasonCodes": []}


@pytest.fixture
def route_setup():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as c:
        c.execute(text("CREATE TABLE trips(id TEXT,status TEXT,name TEXT,invite_code TEXT)"))
        c.execute(text("CREATE TABLE trip_preparation_documents(trip_id TEXT,conditions_json TEXT,conditions_version INTEGER)"))
        c.execute(text("CREATE TABLE trip_members(id TEXT,trip_id TEXT,nickname TEXT,role TEXT,status TEXT)"))
        c.execute(text("CREATE TABLE device_session_memberships(session_id TEXT,trip_id TEXT,member_id TEXT)"))
        c.execute(text("CREATE TABLE device_sessions(id TEXT PRIMARY KEY,revoked INTEGER)"))
        c.execute(text("CREATE TABLE places(contentid INTEGER,title TEXT,contentTypeName TEXT,planningRole TEXT,adminCity TEXT,addr1 TEXT,addr2 TEXT,mapx REAL,mapy REAL,geoValidated TEXT,tel TEXT,homepageUrl TEXT,overviewText TEXT,firstimage TEXT)"))
        c.execute(text("CREATE TABLE place_details(contentid INTEGER,openingHoursText TEXT,closedDaysText TEXT,feeText TEXT,bookingText TEXT,parkingText TEXT,menuText TEXT,estimatedStayMinutes INTEGER)"))
        c.execute(text("CREATE TABLE trip_canvas_documents(trip_id TEXT PRIMARY KEY,revision INTEGER,schema_version INTEGER,document_json TEXT,source_plan_id TEXT,source_version_id TEXT,created_at DATETIME,updated_at DATETIME)"))
        c.execute(text("CREATE TABLE trip_canvas_requests(session_id TEXT,trip_id TEXT,member_id TEXT,kind TEXT,method TEXT,path TEXT,idempotency_key TEXT,fingerprint TEXT,status TEXT,lease_token TEXT,lease_expires_at DATETIME,result_json TEXT,created_at DATETIME,updated_at DATETIME,expires_at DATETIME,PRIMARY KEY(session_id,method,path,idempotency_key))"))
        c.execute(text("INSERT INTO trips VALUES(:t,'OPEN','trip','code')"), {"t": TRIP})
        c.execute(text("INSERT INTO trip_members VALUES(:m,:t,'Member','MEMBER','ACTIVE')"), {"m": MEMBER, "t": TRIP})
        c.execute(text("INSERT INTO device_session_memberships VALUES(:s,:t,:m)"), {"s": SESSION, "t": TRIP, "m": MEMBER})
        c.execute(text("INSERT INTO device_sessions VALUES(:s,0)"), {"s": SESSION})
        c.execute(text("INSERT INTO places VALUES(101,'A',NULL,NULL,NULL,NULL,NULL,127.1,36.1,'yes',NULL,NULL,NULL,NULL),(102,'B',NULL,NULL,NULL,NULL,NULL,128.2,37.2,'yes',NULL,NULL,NULL,NULL),(103,'C',NULL,NULL,NULL,NULL,NULL,129.3,38.3,'yes',NULL,NULL,NULL,NULL)"))
        for cid in (101, 102, 103):
            c.execute(text("INSERT INTO place_details(contentid) VALUES(:id)"), {"id": cid})
        c.execute(text("INSERT INTO trip_preparation_documents VALUES(:t,:conditions,2)"), {"t": TRIP, "conditions": json.dumps({"transport": "car"})})

    def find_session(conn, request):
        row = conn.execute(text("SELECT revoked FROM device_sessions WHERE id=:s"), {"s": SESSION}).mappings().first()
        return {"id": SESSION} if request.cookies.get("session") == "ok" and row and not row["revoked"] else None

    def cookie_name(): return "session"
    def origin_check(request):
        if request.headers.get("origin") != "http://test":
            raise ApiError(403, "ORIGIN_NOT_ALLOWED", "bad")

    async def body_json(request, allowed):
        data = await request.json()
        if not isinstance(data, dict) or set(data) - allowed:
            raise ApiError(400, "VALIDATION_ERROR", "bad")
        return data

    app = FastAPI()
    @app.exception_handler(ApiError)
    def api_error(request, exc):
        from fastapi.responses import JSONResponse
        headers = {"Retry-After": str(exc.retry)} if exc.retry else {}
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status, headers=headers)
    router = FakeRouter(engine)
    app.state.generation_router = router
    register_canvas_routes(app, engine, find_session=find_session, cookie_name=cookie_name,
                           origin_check=origin_check, body_json=body_json, ApiError=ApiError,
                           set_session_cookie=lambda *args: None)
    client = TestClient(app, base_url="http://test")
    client.cookies.set("session", "ok")
    yield client, engine, router
    client.close()
    engine.dispose()


def request_route(client, source=101, target=102, cv=2):
    return client.get(f"/api/v1/trips/{TRIP}/canvas/route", params={
        "sourceContentid": source, "targetContentid": target, "expectedConditionsVersion": cv,
    })


def test_route_response_exact_shape_direction_mode_and_cache(route_setup):
    client, engine, router = route_setup
    response = request_route(client)
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"sourceContentid", "targetContentid", "conditionsVersion", "mode", "status",
                           "durationSeconds", "distanceMeters", "checkedAt", "reasonCodes"}
    assert result == {"sourceContentid": 101, "targetContentid": 102, "conditionsVersion": 2,
                      "mode": "car", "status": "verified", "durationSeconds": 321,
                      "distanceMeters": 654, "checkedAt": result["checkedAt"], "reasonCodes": []}
    assert result["checkedAt"].endswith("Z") and len(router.calls) == 1
    assert router.calls[0][0] == {"latitude": 36.1, "longitude": 127.1, "crs": "WGS84"}
    assert router.calls[0][1] == {"latitude": 37.2, "longitude": 128.2, "crs": "WGS84"}
    assert request_route(client).json() == result
    assert len(router.calls) == 1
    assert request_route(client, source=102, target=101).status_code == 200
    assert len(router.calls) == 2
    with engine.begin() as c:
        c.execute(text("UPDATE trip_preparation_documents SET conditions_json=:j,conditions_version=3 WHERE trip_id=:t"),
                  {"j": json.dumps({"transport": "walk"}), "t": TRIP})
    walking = request_route(client, cv=3)
    assert walking.json()["mode"] == "walk" and len(router.calls) == 3


def test_route_stale_conditions_rejected_before_provider(route_setup):
    client, _, router = route_setup
    response = request_route(client, cv=1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_ROUTE_INPUT"
    assert router.calls == []


@pytest.mark.parametrize("mode", ["charter", None])
def test_charter_and_missing_mode_are_unknown_without_provider(route_setup, mode):
    client, engine, router = route_setup
    with engine.begin() as c:
        c.execute(text("UPDATE trip_preparation_documents SET conditions_json=:j WHERE trip_id=:t"),
                  {"j": json.dumps({"transport": mode}), "t": TRIP})
    result = request_route(client).json()
    assert result["status"] == "unknown" and result["durationSeconds"] is None and result["distanceMeters"] is None
    assert result["reasonCodes"] and router.calls == []


def test_invalid_or_missing_coordinates_unknown_without_provider(route_setup):
    client, engine, router = route_setup
    with engine.begin() as c:
        c.execute(text("UPDATE places SET geoValidated='no' WHERE contentid=101"))
    result = request_route(client).json()
    assert result["status"] == "unknown" and result["reasonCodes"] == ["ROUTE_COORDINATE_INVALID"]
    assert router.calls == []
    assert request_route(client, source=999).status_code == 404
    same = request_route(client, source=102, target=102)
    assert same.status_code == 200 and same.json()["status"] == "unknown"
    assert router.calls == []


def test_route_reauth_after_provider_and_stale_coordinate_fence(route_setup):
    client, engine, router = route_setup
    def change_membership():
        with engine.begin() as c:
            c.execute(text("UPDATE trip_members SET status='INACTIVE' WHERE id=:m"), {"m": MEMBER})
    router.on_call = change_membership
    response = request_route(client)
    assert response.status_code == 403 and len(router.calls) == 1


def test_coordinate_change_during_provider_returns_stale(route_setup):
    client, engine, router = route_setup
    def change_coordinate():
        with engine.begin() as c:
            c.execute(text("UPDATE places SET mapx=130 WHERE contentid=101"))
    router.on_call = change_coordinate
    response = request_route(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_ROUTE_INPUT"


def test_conditions_version_change_during_provider_returns_stale(route_setup):
    client, engine, router = route_setup
    def change_version():
        with engine.begin() as c:
            c.execute(text("UPDATE trip_preparation_documents SET conditions_version=3 WHERE trip_id=:t"), {"t": TRIP})
    router.on_call = change_version
    response = request_route(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_ROUTE_INPUT"


def test_session_revocation_during_provider_returns_unauthorized(route_setup):
    client, engine, router = route_setup
    def revoke():
        with engine.begin() as c:
            c.execute(text("UPDATE device_sessions SET revoked=1 WHERE id=:s"), {"s": SESSION})
    router.on_call = revoke
    response = request_route(client)
    assert response.status_code == 401


@pytest.mark.parametrize("params", [
    {"sourceContentid": "1.2", "targetContentid": "102", "expectedConditionsVersion": "2"},
    {"sourceContentid": "2147483648", "targetContentid": "102", "expectedConditionsVersion": "2"},
    {"sourceContentid": "101", "targetContentid": "102", "expectedConditionsVersion": "-1"},
    {"sourceContentid": "101", "targetContentid": "102", "expectedConditionsVersion": "2", "other": "x"},
])
def test_route_query_validation_is_400(params, route_setup):
    client, _, router = route_setup
    response = client.get(f"/api/v1/trips/{TRIP}/canvas/route", params=params)
    assert response.status_code == 400 and router.calls == []


def test_unknown_route_facts_are_sanitized_and_cached_briefly(route_setup):
    client, _, router = route_setup
    router.status = "unknown"
    first = request_route(client)
    second = request_route(client)
    assert first.status_code == 200 and first.json()["status"] == "unknown"
    assert first.json()["durationSeconds"] is None and first.json()["reasonCodes"] == ["ROUTE_NOT_FOUND"]
    assert first.json() == second.json() and len(router.calls) == 1


def test_route_cache_limits_distinct_provider_calls_to_two():
    cache = RouteCache(provider_limit=2, verified_ttl=0.01, unknown_ttl=0.01)
    active = 0
    peak = 0
    lock = threading.Lock()
    start = threading.Event()
    def worker(i):
        def compute():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            start.wait(1)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"status": "verified", "mode": "car", "durationSeconds": 1,
                    "distanceMeters": 2, "checkedAt": "2026-01-01T00:00:00Z", "reasonCodes": []}
        return cache.resolve((i, "car", i, i + 1), compute)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads: thread.start()
    time.sleep(0.03)
    start.set()
    for thread in threads: thread.join(2)
    assert peak <= 2
    assert all(not thread.is_alive() for thread in threads)


def test_route_cache_singleflight_calls_provider_once_for_same_key():
    cache = RouteCache()
    calls = []
    barrier = threading.Barrier(4)
    def worker():
        barrier.wait()
        return cache.resolve(("router", "car", 1, 2), lambda: (calls.append(1) or {
            "status": "verified", "mode": "car", "durationSeconds": 1,
            "distanceMeters": 2, "checkedAt": "2026-01-01T00:00:00Z", "reasonCodes": []}))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(2)
    assert len(calls) == 1
    assert all(not thread.is_alive() for thread in threads)


def test_actor_rate_limit_has_positive_retry_after_and_skips_provider(route_setup):
    client, _, router = route_setup
    class Limiter:
        def take(self, key, maximum):
            assert maximum == 120
            return 7
    client.app.state.limiter = Limiter()
    response = request_route(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert router.calls == []
