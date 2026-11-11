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
V2 = "00000000-0000-4000-8000-000000000109"
DATE = "2026-11-11"


def generated_plan():
    return {"days": [{"date": DATE, "nodes": [
        {"id": START, "kind": "START", "contentid": None, "locked": True},
        {"id": P1, "kind": "PLACE", "contentid": 101, "estimatedStartAt": None, "estimatedEndAt": None, "locked": False},
        {"id": P2, "kind": "PLACE", "contentid": 102, "estimatedStartAt": "2026-11-11T05:00:00.000Z", "estimatedEndAt": "2026-11-11T06:00:00.000Z", "locked": True}],
        "edges": [{"id": E1, "fromNodeId": START, "toNodeId": P1}, {"id": E2, "fromNodeId": P1, "toNodeId": P2}]}],
        "validation": {"status": "unknown"}}


def multi_day_plan(dates, counts):
    days = []
    next_id = 0x200
    for day_date, count in zip(dates, counts):
        nodes = []
        edges = []
        for _ in range(count):
            node_id = f"00000000-0000-4000-8000-{next_id:012x}"
            next_id += 1
            start_hour = 5 + len(nodes)
            nodes.append({"id": node_id, "kind": "PLACE", "contentid": 101 + (next_id % 2),
                          "estimatedStartAt": f"{day_date}T{start_hour:02d}:00:00.000Z",
                          "estimatedEndAt": f"{day_date}T{start_hour + 1:02d}:00:00.000Z",
                          "locked": len(nodes) % 2 == 0})
            if len(nodes) > 1:
                edges.append({"id": f"00000000-0000-4000-8000-{next_id:012x}",
                              "fromNodeId": nodes[-2]["id"], "toNodeId": node_id})
                next_id += 1
        days.append({"date": day_date, "nodes": nodes, "edges": edges})
    return {"days": days, "validation": {"status": "valid"}}


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
        conn.execute(text("INSERT INTO places SELECT 102,'Second',contentTypeName,planningRole,adminCity,addr1,addr2,mapx,mapy,geoValidated,tel,homepageUrl,overviewText,firstimage FROM places WHERE contentid=101"))
    class FakeGenerationService:
        error = None
        version = VERSION
        plan = generated_plan()
        def current_plan_for_canvas(self, conn, request, trip_id, plan_id, version_id):
            assert conn.in_transaction()
            if self.error:
                raise CanvasImportError(self.error)
            if (trip_id, plan_id, version_id) != (TRIP, PLAN, self.version):
                raise CanvasImportError("PLAN_NOT_FOUND", 404)
            return {"public": {"planId": PLAN, "versionId": self.version, "document": self.plan}}
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


def test_v4_import_preserves_day_positions_and_prunes_only_links_to_removed_dates():
    old_day = str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:2026-11-10"))
    linked = {"schemaVersion": 4,
              "days": [{"id": DAY, "date": DATE, "position": {"x": 40, "y": 70}},
                       {"id": old_day, "date": "2026-11-10", "position": {"x": 400, "y": 70}}],
              "nodes": [], "edges": [],
              "dayLinks": [{"id": "00000000-0000-4000-8000-000000000108", "source": DAY, "target": old_day}]}
    result = append_generated_plan(linked, TRIP, PLAN, VERSION, generated_plan())
    assert result["schemaVersion"] == 4
    assert result["days"][0]["position"] == {"x": 40, "y": 70}
    assert all(day["id"] != old_day for day in result["days"])
    assert result["dayLinks"] == []
    empty_links = {**linked, "dayLinks": []}
    assert append_generated_plan(empty_links, TRIP, PLAN, VERSION, generated_plan())["dayLinks"] == []


def test_v4_import_extends_only_a_complete_generated_date_chain():
    dates = ["2026-11-11", "2026-11-12", "2026-11-13"]
    days = [{"id": str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + date)), "date": date}
            for date in dates]
    old_link = {"id": "00000000-0000-4000-8000-000000000108",
                "source": days[0]["id"], "target": days[1]["id"]}
    old = {"schemaVersion": 4, "days": days[:2], "nodes": [], "edges": [], "dayLinks": [old_link]}
    plan = multi_day_plan(dates, [1, 1, 1])
    extended = append_generated_plan(old, TRIP, PLAN, VERSION, plan)
    assert extended["dayLinks"][0] == old_link
    assert extended["dayLinks"][1] == {
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{days[1]['id']}:{days[2]['id']}")),
        "source": days[1]["id"], "target": days[2]["id"]}

    no_links = {**old, "dayLinks": []}
    assert append_generated_plan(no_links, TRIP, PLAN, VERSION, plan)["dayLinks"] == []

    custom_link = {**old_link, "source": days[1]["id"], "target": days[0]["id"]}
    custom = {**old, "dayLinks": [custom_link]}
    assert append_generated_plan(custom, TRIP, PLAN, VERSION, plan)["dayLinks"] == [custom_link]

    # A missing old pair is a deletion signal; importing a later day must not
    # recreate it or infer links beyond the already preserved intent.
    three_old_days = {"schemaVersion": 4, "days": days, "nodes": [], "edges": [],
                      "dayLinks": [old_link]}
    fourth_date = "2026-11-14"
    fourth = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + fourth_date)), "date": fourth_date}
    deleted_pair = append_generated_plan(three_old_days, TRIP, PLAN, VERSION,
                                         multi_day_plan(dates + [fourth_date], [1, 1, 1, 1]))
    assert deleted_pair["dayLinks"] == [old_link]
    assert fourth["id"] in {day["id"] for day in deleted_pair["days"]}


def test_v3_two_to_three_day_regeneration_keeps_legacy_shape():
    old = append_generated_plan(empty_document(), TRIP, PLAN, VERSION,
                                multi_day_plan(["2026-11-11", "2026-11-12"], [1, 2]))
    new_version = str(uuid.uuid4())
    result = append_generated_plan(old, TRIP, PLAN, new_version,
                                   multi_day_plan(["2026-11-11", "2026-11-12", "2026-11-13"], [1, 2, 5]))
    assert result["schemaVersion"] == 3
    assert "dayLinks" not in result
    assert {day["date"] for day in result["days"]} == {"2026-11-11", "2026-11-12", "2026-11-13"}
    route = [node for node in result["nodes"] if node.get("source")]
    assert len(route) == 8
    assert {node["source"]["versionId"] for node in route} == {new_version}


def test_v4_three_day_regeneration_keeps_manual_edits_and_replaces_managed_route(import_app):
    client, engine, service = import_app
    dates = ["2026-11-11", "2026-11-12", "2026-11-13"]
    day_ids = [str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + day_date)) for day_date in dates]
    initial_link = {"id": "00000000-0000-4000-8000-000000000108",
                    "source": day_ids[0], "target": day_ids[1]}
    v4 = {"schemaVersion": 4,
          "days": [{"id": day_ids[0], "date": dates[0], "position": {"x": 10, "y": 15}},
                   {"id": day_ids[1], "date": dates[1], "position": {"x": 300, "y": 15}}],
          "nodes": [], "edges": [], "dayLinks": [initial_link]}
    saved = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                       json={"expectedRevision": 0, "document": v4})
    assert saved.status_code == 200, saved.text

    service.plan = multi_day_plan(dates[:2], [2, 1])
    first = import_request(client, revision=1)
    assert first.status_code == 200, first.text
    old_document = first.json()["document"]
    old_managed = [node for node in old_document["nodes"] if node.get("source")]
    old_ids = {node["id"] for node in old_managed}
    assert len(old_managed) == 3
    assert len(old_document["edges"]) == 1

    edited = copy.deepcopy(old_document)
    old_route = next(node for node in edited["nodes"] if node.get("source"))
    old_route["position"] = {"x": 777, "y": 888}
    old_route["color"] = "rose"
    manual = {"id": PLACE, "kind": "comment", "dayId": day_ids[0],
              "position": {"x": 15, "y": 20}, "color": "sky", "text": "Keep this note"}
    manual_route = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "route:manual")), "kind": "place",
                    "dayId": day_ids[0], "position": {"x": 200, "y": 20}, "color": "amber",
                    "contentid": 102, "intent": "route"}
    candidate = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "candidate:manual")), "kind": "place",
                 "dayId": day_ids[0], "position": {"x": 350, "y": 20}, "color": "lime",
                 "contentid": 101, "intent": "candidate"}
    manual_edge = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "edge:manual-to-candidate")),
                   "source": manual_route["id"], "target": candidate["id"]}
    edited["nodes"].extend([manual, manual_route, candidate])
    edited["edges"].append(manual_edge)
    edit_response = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                               json={"expectedRevision": 2, "document": edited})
    assert edit_response.status_code == 200, edit_response.text

    service.version = V2
    service.plan = multi_day_plan(dates, [1, 2, 5])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": V2, "p": PLAN, "t": TRIP,
                      "d": json.dumps({"schema": "generation.v1", "document": service.plan,
                                       "sourceStamp": {"inputToken": "new"}})})
    second = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                         json={"versionId": V2, "expectedCanvasRevision": 3})
    assert second.status_code == 200, second.text
    latest = second.json()["document"]
    latest_route = [node for node in latest["nodes"] if node.get("intent") == "route" and node.get("source")]
    assert len(latest_route) == 8
    assert {node["dayId"] for node in latest_route} == set(day_ids)
    assert all(node["source"]["versionId"] == V2 for node in latest_route)
    assert not old_ids & {node["id"] for node in latest["nodes"]}
    assert manual in latest["nodes"] and manual_route in latest["nodes"] and candidate in latest["nodes"]
    assert not any(node["id"] == old_route["id"] for node in latest["nodes"])
    assert len(latest["edges"]) == 6
    assert manual_edge in latest["edges"]
    assert all(edge["source"] not in old_ids and edge["target"] not in old_ids
               for edge in latest["edges"] if edge != manual_edge)
    expected_schedules = {
        node["id"]: {"startAt": node["estimatedStartAt"], "endAt": node["estimatedEndAt"],
                     "locked": node["locked"]}
        for day in service.plan["days"] for node in day["nodes"]
    }
    assert all(node["schedule"] == expected_schedules[node["source"]["nodeId"]] for node in latest_route)
    expected_links = [initial_link, {
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{day_ids[1]}:{day_ids[2]}")),
        "source": day_ids[1], "target": day_ids[2]}]
    assert latest["dayLinks"] == expected_links
    assert [day["date"] for day in latest["days"]] == dates

    forged = copy.deepcopy(latest)
    forged_route = next(node for node in forged["nodes"] if node.get("source"))
    forged_route["schedule"]["locked"] = not forged_route["schedule"]["locked"]
    rejected = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                          json={"expectedRevision": 4, "document": forged})
    assert rejected.status_code == 400
    replay = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                         json={"versionId": V2, "expectedCanvasRevision": 4})
    assert replay.status_code == 200, replay.text
    assert replay.json()["document"] == latest

    deleted = {**latest, "nodes": [], "edges": []}
    delete_response = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                                 json={"expectedRevision": 4, "document": deleted})
    assert delete_response.status_code == 200, delete_response.text
    replay_after_delete = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                                      json={"versionId": V2, "expectedCanvasRevision": 5})
    assert replay_after_delete.status_code == 200, replay_after_delete.text
    assert replay_after_delete.json()["document"]["nodes"] == []


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


def test_new_version_replaces_generated_route_but_keeps_personal_placements():
    old = append_generated_plan(empty_document(), TRIP, PLAN, VERSION, generated_plan())
    alternative = {**copy.deepcopy(old["nodes"][0]), "intent": "candidate"}
    old["nodes"][0] = alternative
    note = {"id": PLACE, "kind": "comment", "dayId": DAY, "position": {"x": 10, "y": 20},
            "color": "rose", "text": "keep this note"}
    old["nodes"].append(note)
    next_version = str(uuid.uuid4())
    new = append_generated_plan(old, TRIP, PLAN, next_version, generated_plan())
    assert alternative in new["nodes"] and note in new["nodes"]
    route = [n for n in new["nodes"] if n.get("intent") == "route"]
    assert [n["contentid"] for n in route] == [101, 102]
    assert all(n["source"]["versionId"] == next_version for n in route)
    assert len(new["edges"]) == 1
    assert set(new["edges"][0][k] for k in ("source", "target")) == {n["id"] for n in route}


def test_reading_an_old_stacked_canvas_exposes_only_its_current_generated_route(import_app):
    client, engine, _ = import_app
    current = import_request(client).json()
    day_id = current["document"]["days"][0]["id"]
    empty = {**empty_document(), "days": current["document"]["days"]}
    obsolete = append_generated_plan(empty, TRIP, PLAN, str(uuid.uuid4()), generated_plan())
    stacked = copy.deepcopy(current["document"])
    stacked["nodes"] += obsolete["nodes"]
    stacked["edges"] += obsolete["edges"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE trip_canvas_documents SET document_json=:d WHERE trip_id=:t"),
                     {"d": json.dumps(stacked), "t": TRIP})
    visible = client.get(f"/api/v1/trips/{TRIP}/canvas").json()
    assert visible["document"] == current["document"]
    assert visible["revision"] == current["revision"]

    # Opening the existing itinerary compacts the stored stack exactly once.
    assert import_request(client, revision=current["revision"] - 1).status_code == 409
    cleaned = import_request(client, revision=current["revision"]).json()
    assert cleaned["revision"] == current["revision"] + 1
    assert import_request(client, revision=cleaned["revision"]).json()["revision"] == cleaned["revision"]
    with engine.connect() as conn:
        stored = json.loads(conn.execute(text("SELECT document_json FROM trip_canvas_documents")).scalar_one())
    assert stored == visible["document"]

    # A second tab submits its old full document with a new comment: its stale
    # revision conflicts, and even a rebased write cannot resurrect the obsolete route.
    note = {"id": PLACE, "kind": "comment", "dayId": day_id, "position": {"x": 0, "y": 0},
            "color": "rose", "text": "shared comment"}
    stacked["nodes"].append(note)
    url = f"/api/v1/trips/{TRIP}/canvas"
    assert client.put(url, headers=headers(), json={"document": stacked, "expectedRevision": current["revision"]}).status_code == 409
    edited = client.put(url, headers=headers(), json={"document": stacked, "expectedRevision": cleaned["revision"]})
    assert edited.status_code == 200, edited.text
    with engine.connect() as conn:
        stored = json.loads(conn.execute(text("SELECT document_json FROM trip_canvas_documents")).scalar_one())
    assert stored["nodes"] == [*visible["document"]["nodes"], note]
    assert stored["edges"] == visible["document"]["edges"]
