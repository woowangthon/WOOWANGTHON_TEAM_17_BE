"""HTTP/SQL v2 roundtrip, using a private SQLite adapter (not MySQL validation)."""
import copy
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.preparation import register_preparation_routes, validate_conditions
from chungbuk_trip_ai.ensemble import interpret_personal_draft_v2
from preparation_helpers import CONDITIONS


class ApiError(Exception):
    def __init__(self, status, code, message="", fields=None, retry=None):
        self.status, self.code, self.fields = status, code, fields or {}


class Result:
    def __init__(self, result): self.result = result
    def mappings(self): return self
    def first(self):
        row = self.result.mappings().first()
        if row is None: return None
        row = dict(row)
        for key in ("expires_at", "lease_expires_at", "confirmed_at"):
            if isinstance(row.get(key), str): row[key] = datetime.fromisoformat(row[key])
        return row
    def all(self): return [dict(row) for row in self.result.mappings().all()]
    def __getattr__(self, key): return getattr(self.result, key)


class Connection:
    def __init__(self, conn): self.conn = conn
    def execute(self, statement, params=None):
        sql = str(statement).replace(" FOR UPDATE", "")
        if "GET_LOCK(" in sql or "RELEASE_LOCK(" in sql: sql = "SELECT 1"
        return Result(self.conn.execute(text(sql), params or {}))
    def __getattr__(self, key): return getattr(self.conn, key)


@pytest.fixture
def personal_v2():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    trip, member, session = (str(uuid.uuid4()) for _ in range(3))
    conditions = validate_conditions(CONDITIONS)
    with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE trips(id TEXT,name TEXT,invite_code TEXT,status TEXT)",
            "CREATE TABLE trip_members(id TEXT,trip_id TEXT,nickname TEXT,role TEXT,status TEXT,created_at TEXT)",
            "CREATE TABLE device_session_memberships(session_id TEXT,trip_id TEXT,member_id TEXT)",
            "CREATE TABLE trip_preparation_documents(trip_id TEXT,conditions_json TEXT,conditions_version INTEGER)",
            "CREATE TABLE member_preparation_documents(trip_id TEXT,member_id TEXT,draft_json TEXT,preference_version INTEGER,confirmed_conditions_version INTEGER,confirmed_preference_version INTEGER,confirmed_at DATETIME)",
            "CREATE TABLE preparation_interpret_requests(session_id TEXT,method TEXT,path TEXT,idempotency_key TEXT,trip_id TEXT,member_id TEXT,conditions_version INTEGER,preference_version INTEGER,fingerprint TEXT,status TEXT,lease_token TEXT,lease_expires_at DATETIME,result_json TEXT,created_at DATETIME,expires_at DATETIME)",
        ): conn.execute(text(ddl))
        conn.execute(text("INSERT INTO trips VALUES(:t,'test','CB-TEST','OPEN')"), {"t": trip})
        conn.execute(text("INSERT INTO trip_members VALUES(:m,:t,'owner','OWNER','ACTIVE','1')"), {"m": member, "t": trip})
        conn.execute(text("INSERT INTO device_session_memberships VALUES(:s,:t,:m)"), {"s": session, "t": trip, "m": member})
        conn.execute(text("INSERT INTO trip_preparation_documents VALUES(:t,:c,1)"), {"t": trip, "c": json.dumps(conditions)})
    app = FastAPI()
    @app.exception_handler(ApiError)
    async def api_error(_request, exc):
        return JSONResponse({"code": exc.code, "fieldErrors": exc.fields}, status_code=exc.status)
    async def body_json(request, _keys): return await request.json()
    register_preparation_routes(app, SimpleNamespace(connect=lambda: Connection(engine.connect())),
        SimpleNamespace(rate_limit_enabled=False), find_session=lambda *_: {"id": session},
        cookie_name=lambda: "device", origin_check=lambda *_: None, body_json=body_json,
        ApiError=ApiError, set_session_cookie=lambda *_: None)
    evidence = "여행 중 초밥을 한 번 먹고 싶어요"
    atom = {"dimension": "food", "target": "초밥", "polarity": "prefer", "priority": "preferred", "priorityBasis": "unspecified",
            "scope": "trip", "audience": "personal", "frequency": {"count": 1, "operator": "exact", "period": "trip"},
            "value": None, "unit": None, "approximate": False, "evidence": evidence}
    raw = {"conditions": [atom], "unresolved": [], "scheduleItems": []}
    app.state.preparation_interpreter_v2 = lambda original, fixed: interpret_personal_draft_v2(original, fixed, provider="ensemble",
        providers={"gemini": lambda *_: copy.deepcopy(raw), "qwen": lambda *_: copy.deepcopy(raw)})
    with TestClient(app) as client:
        yield client, engine, trip, evidence
    engine.dispose()


def test_v2_http_sql_analysis_receipt_confirm_and_changed_common_conditions(personal_v2):
    client, engine, trip, original = personal_v2
    path = f"/api/v1/trips/{trip}/preferences/me"
    headers = {"X-Preference-Schema": "2"}
    draft = {"originalText": original, "likes": None, "avoid": None, "avoidImportance": "preferred", "food": None,
             "foodImportance": "preferred", "walking": None, "scheduleItems": []}
    source = client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 0, "draft": draft})
    assert source.status_code == 200, source.text
    key = str(uuid.uuid4())
    analysis = client.post(path + "/interpret", headers={**headers, "Idempotency-Key": key},
                           json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1})
    assert analysis.status_code == 200, analysis.text
    result = analysis.json()
    assert result["analysisMeta"]["requestId"] == key
    merged = {"originalText": original, **result["suggestions"]}
    body = {"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1, "draft": merged, "analysisReceipt": key}
    rejected = client.put(path, headers=headers, json={**body, "analysisReceipt": str(uuid.uuid4())})
    assert rejected.status_code == 409 and rejected.json()["code"] == "STALE_AI_RESULT"
    saved = client.put(path, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft"]["atomicPreferences"]["conditions"][0]["origin"] == "model"
    assert "atomicPreferences" not in client.get(path).json()["draft"]
    confirmed = client.post(path + "/confirm", headers=headers,
        json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 2, "reviewed": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmation"]["status"] == "confirmed"
    with engine.begin() as conn:
        changed = {**validate_conditions(CONDITIONS), "startDate": "2026-12-13", "endDate": "2026-12-14"}
        conn.execute(text("UPDATE trip_preparation_documents SET conditions_json=:j,conditions_version=2"), {"j": json.dumps(changed)})
    stale = client.get(path, headers=headers).json()["draft"]["atomicPreferences"]
    assert stale["status"] == "stale"
    direct_confirm = client.post(path + "/confirm", headers=headers,
        json={"expectedConditionsVersion": 2, "expectedPreferenceVersion": 2, "reviewed": True})
    assert direct_confirm.status_code == 400, direct_confirm.text
    assert "atomicPreferences" in direct_confirm.json()["fieldErrors"]


def test_v2_manual_atomic_correction_updates_legacy_and_legacy_only_edit_cannot_recertify_old_atoms(personal_v2):
    client, _engine, trip, original = personal_v2
    path = f"/api/v1/trips/{trip}/preferences/me"
    headers = {"X-Preference-Schema": "2"}
    draft = {"originalText": original, "likes": None, "avoid": None, "avoidImportance": "preferred", "food": None,
             "foodImportance": "preferred", "walking": None, "scheduleItems": []}
    assert client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 0, "draft": draft}).status_code == 200
    key = str(uuid.uuid4())
    result = client.post(path + "/interpret", headers={**headers, "Idempotency-Key": key}, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1}).json()
    saved = client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1, "analysisReceipt": key,
        "draft": {"originalText": original, **result["suggestions"]}}).json()
    corrected = copy.deepcopy(saved["draft"])
    corrected["atomicPreferences"]["conditions"][0].update(target="피자", frequency={"count": 2, "operator": "exact", "period": "day"}, scope="day")
    edited = client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 2, "draft": corrected})
    assert edited.status_code == 200, edited.text
    document = edited.json()["draft"]
    assert document["food"] == "피자"
    assert document["atomicPreferences"]["conditions"][0]["origin"] == "user"
    assert document["atomicPreferences"]["status"] == "current"
    inconsistent = client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 3,
        "draft": {**document, "food": "국수"}})
    assert inconsistent.status_code == 200, inconsistent.text
    assert inconsistent.json()["draft"]["atomicPreferences"]["status"] == "stale"


def test_invalid_v2_output_is_safe_502_and_interpretation_lease_can_retry(personal_v2):
    client, _engine, trip, original = personal_v2
    path = f"/api/v1/trips/{trip}/preferences/me"
    headers = {"X-Preference-Schema": "2"}
    draft = {"originalText": original, "likes": None, "avoid": None, "avoidImportance": "preferred", "food": None,
             "foodImportance": "preferred", "walking": None, "scheduleItems": []}
    client.put(path, headers=headers, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 0, "draft": draft})
    good = client.app.state.preparation_interpreter_v2
    def invalid(*args):
        result = good(*args)
        result["atomicPreferences"]["conditions"][0]["dimension"] = "unsupported"
        return result
    client.app.state.preparation_interpreter_v2 = invalid
    key = str(uuid.uuid4())
    body = {"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1}
    failed = client.post(path + "/interpret", headers={**headers, "Idempotency-Key": key}, json=body)
    assert failed.status_code == 502 and failed.json()["code"] == "AI_INVALID_RESPONSE"
    client.app.state.preparation_interpreter_v2 = good
    assert client.post(path + "/interpret", headers={**headers, "Idempotency-Key": key}, json=body).status_code == 200
