"""Real preparation route checks using isolated SQLite and stubbed access only.

This does not certify MySQL locking, authentication, or the shared database.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text

from app import preparation
from app.main import ApiError, body_json
from test_generation_owner_readiness_sqlite import SQLiteConnectionAdapter


@pytest.mark.parametrize("already_confirmed", [False, True])
def test_conflict_blocks_confirmation_and_draft_can_still_be_saved(tmp_path, monkeypatch, already_confirmed):
    trip_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
    engine = create_engine(f"sqlite:///{tmp_path / 'walking.db'}", connect_args={
        "check_same_thread": False, "detect_types": sqlite3.PARSE_DECLTYPES,
    })
    draft = {"originalText": "도보 구간은 최대 20분 이내로 해 주세요.", "likes": "자연",
             "avoid": "긴 도보 이동", "avoidImportance": "required", "walking": "40",
             "food": None, "foodImportance": "preferred", "scheduleItems": []}
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE member_preparation_documents (trip_id TEXT, member_id TEXT, draft_json TEXT, preference_version INTEGER, confirmed_conditions_version INTEGER, confirmed_preference_version INTEGER, confirmed_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO member_preparation_documents VALUES (:t,:m,:d,1,:cv,:pv,:at)"), {
            "t": trip_id, "m": member_id, "d": json.dumps(draft, ensure_ascii=False),
            "cv": 1 if already_confirmed else None, "pv": 1 if already_confirmed else None,
            "at": datetime(2026, 11, 14) if already_confirmed else None,
        })
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def error_handler(_request, exc):
        return JSONResponse({"code": exc.code, "fieldErrors": exc.fields}, status_code=exc.status)

    monkeypatch.setattr(preparation, "_access", lambda *_args, **_kwargs: ({}, {}, {"id": member_id, "role": "OWNER"}))
    monkeypatch.setattr(preparation, "_conditions", lambda *_args: ({}, 1))
    preparation.register_preparation_routes(app,
        SimpleNamespace(connect=lambda: SQLiteConnectionAdapter(engine.connect())),
        SimpleNamespace(rate_limit_enabled=False), find_session=lambda *_args: None,
        cookie_name=lambda: "test", origin_check=lambda _request: None, body_json=body_json,
        ApiError=ApiError, set_session_cookie=lambda *_args: None)
    path = f"/api/v1/trips/{trip_id}/preferences/me"
    try:
        with TestClient(app) as client:
            body = {"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1, "reviewed": True}
            blocked = client.post(path + "/confirm", json=body)
            assert blocked.status_code == 400, blocked.text
            assert blocked.json()["code"] == "PREFERENCES_INCOMPLETE"
            assert "walking" in blocked.json()["fieldErrors"]

            # An unresolved choice remains saveable; it is not auto-confirmed.
            saved = client.put(path, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 1,
                                          "draft": {**draft, "walking": "unlimited"}})
            assert saved.status_code == 200, saved.text
            assert saved.json()["draft"]["originalText"] == draft["originalText"]
            assert saved.json()["confirmation"]["status"] == "writing"
            assert client.post(path + "/confirm", json={**body, "expectedPreferenceVersion": 2}).status_code == 400

            corrected = client.put(path, json={"expectedConditionsVersion": 1, "expectedPreferenceVersion": 2,
                                              "draft": {**draft, "walking": "20"}})
            assert corrected.status_code == 200, corrected.text
            final_body = {**body, "expectedPreferenceVersion": 3}
            confirmed = client.post(path + "/confirm", json=final_body)
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["confirmation"]["status"] == "confirmed"
            assert client.post(path + "/confirm", json=final_body).json() == confirmed.json()
            assert client.post(path + "/confirm", json=body).status_code == 409
    finally:
        engine.dispose()
