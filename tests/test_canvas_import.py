"""Offline plan-to-canvas contracts; no network listener or public database."""
from __future__ import annotations

import copy
import json
import uuid

import pytest
from sqlalchemy import text

from app.canvas_import import CanvasImportError, append_generated_plan
from test_canvas_api import DAY, MEMBER, PLACE, TRIP, setup_app  # noqa: F401


PLAN = "00000000-0000-4000-8000-000000000101"
VERSION = "00000000-0000-4000-8000-000000000102"
P1 = "00000000-0000-4000-8000-000000000103"
P2 = "00000000-0000-4000-8000-000000000104"
START = "00000000-0000-4000-8000-000000000105"
E1 = "00000000-0000-4000-8000-000000000106"
E2 = "00000000-0000-4000-8000-000000000107"
DATE = "2026-11-11"


def generated_plan():
    return {"days": [{"date": DATE, "nodes": [
        {"id": START, "kind": "START", "contentid": None, "locked": True},
        {"id": P1, "kind": "PLACE", "contentid": 101, "estimatedStartAt": None, "estimatedEndAt": None, "locked": False},
        {"id": P2, "kind": "PLACE", "contentid": 102, "estimatedStartAt": "2026-11-11T05:00:00.000Z", "estimatedEndAt": "2026-11-11T06:00:00.000Z", "locked": True}],
        "edges": [{"id": E1, "fromNodeId": START, "toNodeId": P1}, {"id": E2, "fromNodeId": P1, "toNodeId": P2}]}],
        "validation": {"status": "unknown"}}


def empty_document():
    return {"schemaVersion": 3, "days": [{"id": DAY, "date": DATE}], "nodes": [], "edges": []}


def headers(key=None):
    return {"Origin": "http://test", "Idempotency-Key": key or str(uuid.uuid4())}


@pytest.fixture
def import_app(setup_app):
    client, engine = setup_app
    with engine.begin() as conn:
        conn.execute(text("UPDATE trip_members SET role='OWNER' WHERE id=:m"), {"m": MEMBER})
        conn.execute(text("CREATE TABLE plan_versions(id TEXT PRIMARY KEY,plan_id TEXT,trip_id TEXT,validation_result TEXT)"))
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": VERSION, "p": PLAN, "t": TRIP, "d": json.dumps({"schema": "generation.v1", "document": generated_plan(), "sourceStamp": {"inputToken": "current"}})})
        conn.execute(text("INSERT INTO places SELECT 102,'Second',contentTypeName,planningRole,adminCity,addr1,addr2,mapx,mapy,geoValidated,tel,homepageUrl,overviewText FROM places WHERE contentid=101"))
    class FakeGenerationService:
        error = None
        def current_plan_for_canvas(self, conn, request, trip_id, plan_id, version_id):
            assert conn.in_transaction()
            if self.error:
                raise CanvasImportError(self.error)
            if (trip_id, plan_id, version_id) != (TRIP, PLAN, VERSION):
                raise CanvasImportError("PLAN_NOT_FOUND", 404)
            return {"public": {"planId": PLAN, "versionId": VERSION, "document": generated_plan()}}
    service = FakeGenerationService()
    client.app.state.generation_service = service
    return client, engine, service


def import_request(client, revision=0, key=None):
    return client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(key),
                       json={"versionId": VERSION, "expectedCanvasRevision": revision})


def test_append_keeps_edits_and_uses_only_actual_same_day_place_edges():
    original = empty_document()
    original["nodes"] = [{"id": PLACE, "kind": "comment", "dayId": DAY, "position": {"x": 20, "y": 800}, "color": "rose", "text": "Keep our shared note"}]
    untouched = copy.deepcopy(original)
    result = append_generated_plan(original, TRIP, PLAN, VERSION, generated_plan())
    assert original == untouched
    assert result["nodes"][0] == original["nodes"][0]
    places = [node for node in result["nodes"] if node["kind"] == "place"]
    assert [node["contentid"] for node in places] == [101, 102]
    assert len(result["edges"]) == 1
    assert result["edges"][0]["source"] == places[0]["id"]
    assert places[0]["schedule"] == {"startAt": None, "endAt": None, "locked": False}
    assert places[1]["source"] == {"planId": PLAN, "versionId": VERSION, "nodeId": P2}
    assert places[1]["position"]["y"] > 800


def test_import_cas_atomic_marker_and_repeat_after_deletion_preserve_latest(import_app):
    client, engine, _ = import_app
    key = str(uuid.uuid4())
    first = import_request(client, key=key)
    assert first.status_code == 200, first.text
    state = first.json()
    assert state["revision"] == 1 and len(state["document"]["nodes"]) == 2
    with engine.connect() as conn:
        payload = json.loads(conn.execute(text("SELECT validation_result FROM plan_versions")).scalar_one())
        assert payload["_canvasImport"] == {"tripId": TRIP, "revision": 1}
    # Ordinary shared save retains the source reference and deletes all cards.
    edited = {**state["document"], "nodes": [], "edges": []}
    saved = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json={"expectedRevision": 1, "document": edited})
    assert saved.status_code == 200
    assert saved.json()["sourcePlan"] == {"planId": PLAN, "versionId": VERSION}
    # Even another key and expired request rows must not resurrect the cards.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM trip_canvas_requests"))
    again = import_request(client, revision=2)
    assert again.status_code == 200
    assert again.json()["revision"] == 2
    assert again.json()["document"]["nodes"] == []


def test_import_conflict_rolls_back_marker_and_request(import_app):
    client, engine, _ = import_app
    conflict = import_request(client, revision=99)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trip_canvas_documents")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM trip_canvas_requests")).scalar_one() == 0
        assert "_canvasImport" not in json.loads(conn.execute(text("SELECT validation_result FROM plan_versions")).scalar_one())


def test_import_enforces_owner_session_origin_and_source_before_mutation(import_app):
    client, engine, service = import_app
    service.error = "PLAN_STALE"
    assert import_request(client).json()["error"]["code"] == "PLAN_STALE"
    service.error = None
    with engine.begin() as conn:
        conn.execute(text("UPDATE trip_members SET role='MEMBER'"))
    assert import_request(client).status_code == 403
    client.cookies.clear()
    assert import_request(client).status_code == 401
    client.cookies.set("session", "ok")
    denied = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", json={"versionId": VERSION, "expectedCanvasRevision": 0}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert denied.status_code == 403
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trip_canvas_documents")).scalar_one() == 0


def test_import_uncertain_replay_and_changed_body_key_conflict(import_app):
    client, _, _ = import_app
    key = str(uuid.uuid4())
    first = import_request(client, key=key)
    assert first.status_code == 200
    assert import_request(client, key=key).json() == first.json()
    changed = import_request(client, revision=1, key=key)
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_source_schedule_cannot_be_forged_but_layout_and_real_undo_work(import_app):
    client, _, _ = import_app
    state = import_request(client).json()
    document = copy.deepcopy(state["document"])
    document["nodes"][0]["position"]["x"] = 700
    moved = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json={"expectedRevision": 1, "document": document})
    assert moved.status_code == 200
    document["nodes"][0]["schedule"]["locked"] = True
    forged = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json={"expectedRevision": 2, "document": document})
    assert forged.status_code == 400
    document = copy.deepcopy(moved.json()["document"])
    document["nodes"], document["edges"] = [], []
    removed = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json={"expectedRevision": 2, "document": document})
    assert removed.status_code == 200
    restored = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json={"expectedRevision": 3, "document": moved.json()["document"]})
    assert restored.status_code == 200


def test_generated_capacity_overflow_is_explicit():
    document = empty_document()
    document["nodes"] = [{"id": str(uuid.uuid4()), "kind": "comment", "dayId": DAY, "position": {"x": 0, "y": 0}, "color": "default", "text": "note"} for _ in range(500)]
    with pytest.raises(CanvasImportError, match="CANVAS_CAPACITY_EXCEEDED"):
        append_generated_plan(document, TRIP, PLAN, VERSION, generated_plan())
