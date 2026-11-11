from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.canvas_models import CanvasValidationError, validate_document
from app.canvas_store import CanvasStore, CanvasStoreError
from app.canvas_recommendation import RecommendationError, recommend


TRIP = "00000000-0000-4000-8000-000000000001"
DAY = "00000000-0000-4000-8000-000000000002"
N1 = "00000000-0000-4000-8000-000000000003"
N2 = "00000000-0000-4000-8000-000000000004"


def doc():
    return {"schemaVersion": 3, "days": [{"id": DAY, "date": "2026-11-11"}],
            "nodes": [{"id": N1, "kind": "place", "dayId": DAY, "position": {"x": 1, "y": 2}, "color": "default", "contentid": 15, "intent": "route"},
                      {"id": N2, "kind": "place", "dayId": DAY, "position": {"x": 3, "y": 4}, "color": "sky", "contentid": 16, "intent": "candidate"}],
            "edges": [{"id": "00000000-0000-4000-8000-000000000005", "source": N1, "target": N2}]}


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE trip_canvas_documents(trip_id TEXT PRIMARY KEY,revision INTEGER,schema_version INTEGER,document_json TEXT,source_plan_id TEXT,source_version_id TEXT,created_at DATETIME,updated_at DATETIME)"))
        c.execute(text("CREATE TABLE trip_canvas_requests(session_id TEXT,trip_id TEXT,member_id TEXT,kind TEXT,method TEXT,path TEXT,idempotency_key TEXT,fingerprint TEXT,status TEXT,lease_token TEXT,lease_expires_at DATETIME,result_json TEXT,created_at DATETIME,updated_at DATETIME,expires_at DATETIME,PRIMARY KEY(session_id,method,path,idempotency_key))"))
    yield engine
    engine.dispose()


def test_canvas_validation_graph_rules_and_exact_fields():
    clean = validate_document(doc())
    assert clean["nodes"][0]["contentid"] == 15
    bad = doc()
    bad["schemaVersion"] = 3.0
    with pytest.raises(CanvasValidationError):
        validate_document(bad)
    bad = doc()
    bad["nodes"][0]["unexpected"] = "no"
    with pytest.raises(CanvasValidationError):
        validate_document(bad)
    bad = doc()
    bad["edges"].append({"id": "00000000-0000-4000-8000-000000000006", "source": N2, "target": N1})
    with pytest.raises(CanvasValidationError, match="invalid edge graph|edge cycle"):
        validate_document(bad)
    bad = doc()
    bad["nodes"][0]["position"]["x"] = float("inf")
    with pytest.raises(CanvasValidationError):
        validate_document(bad)
    bad = doc()
    bad["nodes"][0]["contentid"] = True
    with pytest.raises(CanvasValidationError):
        validate_document(bad)
    bad = {"schemaVersion": 3, "days": [{"id": DAY, "date": None}], "nodes": [
        {"id": str(uuid.uuid4()), "kind": "comment", "dayId": DAY, "position": {"x": 0, "y": 0}, "color": "default", "text": "x"}
        for _ in range(501)], "edges": []}
    with pytest.raises(CanvasValidationError):
        validate_document(bad)
    bad = doc()
    bad["nodes"][1]["dayId"] = "00000000-0000-4000-8000-000000000099"
    with pytest.raises(CanvasValidationError):
        validate_document(bad)


def test_v4_days_and_day_links_are_strict_and_empty_links_are_valid():
    value = {"schemaVersion": 4, "days": [{"id": DAY, "date": "2026-11-11", "position": {"x": -100000, "y": 8}},
                                         {"id": "00000000-0000-4000-8000-000000000099", "date": None}],
             "nodes": [], "edges": [], "dayLinks": []}
    clean = validate_document(value)
    assert clean == value
    linked = {**value, "dayLinks": [{"id": "00000000-0000-4000-8000-000000000098", "source": DAY,
                                      "target": "00000000-0000-4000-8000-000000000099"}]}
    assert validate_document(linked)["dayLinks"] == linked["dayLinks"]
    for invalid in (
        {**value, "dayLinks": [{"id": "00000000-0000-4000-8000-000000000098", "source": DAY, "target": DAY}]},
        {**value, "dayLinks": [{"id": "00000000-0000-4000-8000-000000000098", "source": DAY, "target": "00000000-0000-4000-8000-000000000099"},
                               {"id": "00000000-0000-4000-8000-000000000097", "source": DAY, "target": "00000000-0000-4000-8000-000000000099"}]},
        {**value, "days": [{"id": DAY, "date": None, "position": {"x": 100001, "y": 0}}, value["days"][1]]},
    ):
        with pytest.raises(CanvasValidationError):
            validate_document(invalid)


def test_canvas_cas_idempotency_and_atomic_rollback(engine):
    store = CanvasStore(engine)
    document = validate_document(doc())
    with engine.begin() as c:
        first = store.save_document(c, TRIP, 0, document)
        assert first["revision"] == 1
    with pytest.raises(CanvasStoreError, match="VERSION_CONFLICT"):
        with engine.begin() as c:
            store.save_document(c, TRIP, 0, document)
    with engine.connect() as c:
        assert store.load_document(c, TRIP)["revision"] == 1
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            store.save_document(c, TRIP, 1, {**document, "edges": []})
            raise RuntimeError("rollback")
    with engine.connect() as c:
        assert store.load_document(c, TRIP)["revision"] == 1
    with engine.begin() as c:
        reserved = store.reserve_request(c, session_id="s", trip_id=TRIP, member_id="m", kind="SAVE", method="PUT", path="/canvas", key="k", fingerprint="f", lease_token="l")
        result = {"revision": 1}
        store.complete_request(c, session_id="s", method="PUT", path="/canvas", key="k", lease_token=reserved["leaseToken"], result=result)
    with engine.begin() as c:
        replay = store.reserve_request(c, session_id="s", trip_id=TRIP, member_id="m", kind="SAVE", method="PUT", path="/canvas", key="k", fingerprint="f", lease_token="new")
        assert replay["replay"] == result
    with pytest.raises(CanvasStoreError, match="IDEMPOTENCY_KEY_REUSED"):
        with engine.begin() as c:
            store.reserve_request(c, session_id="s", trip_id=TRIP, member_id="m", kind="SAVE", method="PUT", path="/canvas", key="k", fingerprint="changed", lease_token="new")


def test_v4_write_on_unmigrated_schema_is_not_reported_as_revision_conflict():
    old_engine = create_engine("sqlite+pysqlite:///:memory:")
    with old_engine.begin() as conn:
        conn.execute(text("CREATE TABLE trip_canvas_documents(trip_id TEXT PRIMARY KEY,revision INTEGER,schema_version INTEGER CHECK(schema_version=3),document_json TEXT,source_plan_id TEXT,source_version_id TEXT,created_at DATETIME,updated_at DATETIME)"))
    v4 = validate_document({"schemaVersion": 4, "days": [{"id": DAY, "date": None}], "nodes": [], "edges": [], "dayLinks": []})
    with pytest.raises(CanvasStoreError, match="SCHEMA_MIGRATION_REQUIRED"):
        with old_engine.begin() as conn:
            CanvasStore(old_engine).save_document(conn, TRIP, 0, v4)
    old_engine.dispose()


def test_canvas_insert_conflict_translates_only_unique_trip_id_error():
    from app.canvas_store import _raise_write_conflict
    dup_engine = create_engine("sqlite+pysqlite:///:memory:")
    with dup_engine.begin() as conn:
        conn.execute(text("CREATE TABLE trip_canvas_documents(trip_id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO trip_canvas_documents VALUES('trip')"))
    with pytest.raises(IntegrityError) as caught:
        with dup_engine.begin() as conn:
            conn.execute(text("INSERT INTO trip_canvas_documents VALUES('trip')"))
    duplicate = caught.value
    with pytest.raises(CanvasStoreError, match="VERSION_CONFLICT"):
        _raise_write_conflict(duplicate)
    other = IntegrityError("insert", {}, sqlite3.IntegrityError(1, "CHECK constraint failed: unrelated"))
    with pytest.raises(IntegrityError):
        _raise_write_conflict(other)
    dup_engine.dispose()


@pytest.mark.parametrize("field,value", [("kind", []), ("kind", {}), ("color", []), ("color", {}), ("intent", []), ("intent", {})])
def test_canvas_rejects_non_string_enums_as_validation_errors(field, value):
    invalid = doc()
    invalid["nodes"][0][field] = value
    with pytest.raises(CanvasValidationError):
        validate_document(invalid)


def test_canvas_provider_transport_is_blocked_by_suite_guard():
    from app import canvas_recommendation
    with pytest.raises(AssertionError, match="Live provider calls are forbidden"):
        canvas_recommendation.HTTPSConnection("unused.invalid")


def test_expired_lease_cannot_be_completed_or_paid_again(engine):
    store = CanvasStore(engine)
    with engine.begin() as c:
        store.reserve_request(c, session_id="s", trip_id=TRIP, member_id="m", kind="RECOMMEND", method="POST", path="/recommend", key="k", fingerprint="f", lease_token="l")
        c.execute(text("UPDATE trip_canvas_requests SET lease_expires_at='2000-01-01 00:00:00' WHERE session_id='s'"))
    with pytest.raises(CanvasStoreError, match="REQUEST_IN_PROGRESS"):
        with engine.begin() as c:
            store.complete_request(c, session_id="s", method="POST", path="/recommend", key="k", lease_token="l", result={"place": 1})
    with pytest.raises(CanvasStoreError, match="REQUEST_IN_PROGRESS"):
        with engine.begin() as c:
            store.reserve_request(c, session_id="s", trip_id=TRIP, member_id="m", kind="RECOMMEND", method="POST", path="/recommend", key="k", fingerprint="f", lease_token="new")


def test_recommender_only_returns_subset_and_never_falls_back():
    candidates = [{"contentid": 1, "title": "A"}, {"contentid": 2, "title": "B"}]
    result = recommend("museum", candidates, provider=lambda prompt, pool: {"contentid": 2, "alternatives": [1]})
    assert result["place"]["contentid"] == 2 and result["requiresReview"] is True
    with pytest.raises(RecommendationError, match="AI_INVALID_RESPONSE"):
        recommend("museum", candidates, provider=lambda prompt, pool: {"contentid": 88, "alternatives": []})
    with pytest.raises(RecommendationError, match="CATALOG_UNAVAILABLE"):
        recommend("museum", [], provider=lambda *_: {"contentid": 1, "alternatives": []})


def test_recommendation_provider_does_not_make_live_https(monkeypatch):
    from app import canvas_recommendation
    from types import SimpleNamespace
    def forbidden(*args, **kwargs):
        raise AssertionError("live HTTPS call attempted")
    monkeypatch.setattr("chungbuk_trip_ai.gemini.load_gemini_settings", lambda: SimpleNamespace(api_key="fake", model="test"))
    monkeypatch.setattr(canvas_recommendation, "HTTPSConnection", forbidden)
    with pytest.raises(AssertionError):
        canvas_recommendation._request_gemini("museum", [{"contentid": 1}])
