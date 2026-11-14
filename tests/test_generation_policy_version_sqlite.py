"""Policy rollouts invalidate private source tokens and old draft save paths."""
import json
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app import generation_policy, confirmed_preferences
from app.generation_service import GenerationService, _canonical
from app.plan_store import PlanStoreError, save_generated_plan
from test_generation_owner_readiness_sqlite import SQLiteConnectionAdapter


def test_generation_policy_change_invalidates_token_and_prevents_old_draft_save(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE trip_members(id TEXT,nickname TEXT,role TEXT,trip_id TEXT,status TEXT,created_at TEXT)"))
            conn.execute(text("CREATE TABLE member_preparation_documents(trip_id TEXT,member_id TEXT,preference_version INTEGER,confirmed_conditions_version INTEGER,confirmed_preference_version INTEGER,confirmed_at TEXT)"))
            conn.execute(text("CREATE TABLE plans(id TEXT,trip_id TEXT,lock_version INTEGER,active_draft_id TEXT,current_version_id TEXT)"))
            conn.execute(text("CREATE TABLE plan_versions(id TEXT,plan_id TEXT,trip_id TEXT,status TEXT,validation_result TEXT)"))
        service = GenerationService(SimpleNamespace(), engine, SimpleNamespace(), None, lambda: "cookie", None)
        with engine.begin() as raw:
            conn = SQLiteConnectionAdapter(raw)
            current_token, current_status, current_members = service._source_token(conn, "trip", 1, "catalog", 0)
            monkeypatch.setattr(generation_policy, "GENERATION_POLICY_VERSION", "atomic-route-soft-meals-alns-mmr-car40-v6")
            old_token, old_status, old_members = service._source_token(conn, "trip", 1, "catalog", 0)
            assert old_token != current_token
            assert old_status == current_status and old_members == current_members
            legacy_token = hashlib.sha256(_canonical({"policyVersion": confirmed_preferences.POLICY_VERSION,
                                                      "conditionsVersion": 1, "members": [], "settingsVersion": 0,
                                                      "catalogDigest": "catalog"}).encode()).hexdigest()
            assert legacy_token != current_token
            payload = {"schema": "generation.v1", "sourceStamp": {"inputToken": legacy_token},
                       "document": {"validation": {"status": "verified", "violations": []}}}
            raw.execute(text("INSERT INTO plans VALUES('plan','trip',0,'version',NULL)"))
            raw.execute(text("INSERT INTO plan_versions VALUES('version','plan','trip','DRAFT',:result)"),
                        {"result": json.dumps(payload)})
            with pytest.raises(PlanStoreError, match="STALE_GENERATION_INPUT"):
                save_generated_plan(conn, "trip", "plan", "version", "owner", 0, True, current_token)
            assert raw.scalar(text("SELECT status FROM plan_versions WHERE id='version'")) == "DRAFT"
            assert raw.scalar(text("SELECT lock_version FROM plans WHERE id='plan'")) == 0
    finally:
        engine.dispose()


def test_known_car_violation_cannot_be_saved_with_unknown_acknowledgement():
    from test_car_leg_limit import validation_for
    engine = create_engine("sqlite://")
    try:
        document = validation_for(2401)
        assert any(v["code"] == "CAR_LEG_TIME_LIMIT_EXCEEDED" for v in document["validation"]["violations"])
        with engine.begin() as raw:
            raw.execute(text("CREATE TABLE plans(id TEXT,trip_id TEXT,lock_version INTEGER,active_draft_id TEXT,current_version_id TEXT)"))
            raw.execute(text("CREATE TABLE plan_versions(id TEXT,plan_id TEXT,trip_id TEXT,status TEXT,validation_result TEXT)"))
            payload = {"schema": "generation.v1", "sourceStamp": {"inputToken": "current-policy-token"}, "document": document}
            raw.execute(text("INSERT INTO plans VALUES('p','t',0,'v',NULL)"))
            raw.execute(text("INSERT INTO plan_versions VALUES('v','p','t','DRAFT',:result)"), {"result": json.dumps(payload)})
            conn = SQLiteConnectionAdapter(raw)
            with pytest.raises(PlanStoreError, match="VALIDATION_FAILED"):
                save_generated_plan(conn, "t", "p", "v", "owner", 0, True, "current-policy-token")
            assert raw.scalar(text("SELECT status FROM plan_versions WHERE id='v'")) == "DRAFT"
    finally:
        engine.dispose()
