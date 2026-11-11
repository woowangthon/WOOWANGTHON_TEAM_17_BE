"""Offline plan-to-canvas contracts; no network listener or public database."""
from __future__ import annotations

import copy
import json
import uuid

import pytest
from sqlalchemy import text

from app.canvas_import import CanvasImportError, replace_generated_plan
from test_canvas_api import DAY, MEMBER, PLACE, TRIP, setup_app  # noqa: F401


PLAN = "00000000-0000-4000-8000-000000000101"
VERSION = "00000000-0000-4000-8000-000000000102"
P1 = "00000000-0000-4000-8000-000000000103"
P2 = "00000000-0000-4000-8000-000000000104"
START = "00000000-0000-4000-8000-000000000105"
E1 = "00000000-0000-4000-8000-000000000106"
E2 = "00000000-0000-4000-8000-000000000107"
V2 = "00000000-0000-4000-8000-000000000109"
V3 = "00000000-0000-4000-8000-000000000110"
V4 = "00000000-0000-4000-8000-000000000111"
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


def put_canvas(client, revision, document, source=None):
    return client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                      json={"expectedRevision": revision, "expectedSourcePlan": source, "document": document})


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


def test_replacement_discards_edits_and_uses_only_actual_same_day_place_edges():
    original = empty_document()
    original["nodes"] = [{"id": PLACE, "kind": "comment", "dayId": DAY, "position": {"x": 20, "y": 800}, "color": "rose", "text": "Keep our shared note"}]
    untouched = copy.deepcopy(original)
    result = replace_generated_plan(original, TRIP, PLAN, VERSION, generated_plan())
    assert original == untouched
    assert original["nodes"][0] not in result["nodes"]
    places = [node for node in result["nodes"] if node["kind"] == "place"]
    assert [node["contentid"] for node in places] == [101, 102]
    assert len(result["edges"]) == 1
    assert result["edges"][0]["source"] == places[0]["id"]
    assert places[0]["schedule"] == {"startAt": None, "endAt": None, "locked": False}
    assert places[1]["source"] == {"planId": PLAN, "versionId": VERSION, "nodeId": P2}
    assert places[1]["position"]["y"] == 100


def test_v4_import_rebuilds_dates_positions_and_chronological_links():
    old_day = str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:2026-11-10"))
    linked = {"schemaVersion": 4,
              "days": [{"id": DAY, "date": DATE, "position": {"x": 40, "y": 70}},
                       {"id": old_day, "date": "2026-11-10", "position": {"x": 400, "y": 70}}],
              "nodes": [], "edges": [],
              "dayLinks": [{"id": "00000000-0000-4000-8000-000000000108", "source": DAY, "target": old_day}]}
    result = replace_generated_plan(linked, TRIP, PLAN, VERSION, generated_plan())
    assert result["schemaVersion"] == 4
    assert "position" not in result["days"][0]
    assert all(day["id"] != old_day for day in result["days"])
    assert result["dayLinks"] == []
    empty_links = {**linked, "dayLinks": []}
    assert replace_generated_plan(empty_links, TRIP, PLAN, VERSION, generated_plan())["dayLinks"] == []


def test_v4_import_builds_fresh_full_chronological_chain():
    dates = ["2026-11-11", "2026-11-12", "2026-11-13"]
    days = [{"id": str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + date)), "date": date}
            for date in dates]
    old_link = {"id": "00000000-0000-4000-8000-000000000108",
                "source": days[0]["id"], "target": days[1]["id"]}
    old = {"schemaVersion": 4, "days": days[:2], "nodes": [], "edges": [], "dayLinks": [old_link]}
    plan = multi_day_plan(dates, [1, 1, 1])
    extended = replace_generated_plan(old, TRIP, PLAN, VERSION, plan)
    assert extended["dayLinks"] == [{
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{days[0]['id']}:{days[1]['id']}")),
        "source": days[0]["id"], "target": days[1]["id"]}, {
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{days[1]['id']}:{days[2]['id']}")),
        "source": days[1]["id"], "target": days[2]["id"]}]

    no_links = {**old, "dayLinks": []}
    assert replace_generated_plan(no_links, TRIP, PLAN, VERSION, plan)["dayLinks"] == extended["dayLinks"]

    custom_link = {**old_link, "source": days[1]["id"], "target": days[0]["id"]}
    custom = {**old, "dayLinks": [custom_link]}
    assert replace_generated_plan(custom, TRIP, PLAN, VERSION, plan)["dayLinks"] == extended["dayLinks"]

    # A missing old pair is a deletion signal; importing a later day must not
    # recreate it or infer links beyond the already preserved intent.
    three_old_days = {"schemaVersion": 4, "days": days, "nodes": [], "edges": [],
                      "dayLinks": [old_link]}
    fourth_date = "2026-11-14"
    fourth = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + fourth_date)), "date": fourth_date}
    deleted_pair = replace_generated_plan(three_old_days, TRIP, PLAN, VERSION,
                                         multi_day_plan(dates + [fourth_date], [1, 1, 1, 1]))
    assert len(deleted_pair["dayLinks"]) == 3
    assert fourth["id"] in {day["id"] for day in deleted_pair["days"]}


def test_v3_two_to_three_day_regeneration_keeps_legacy_shape():
    old = replace_generated_plan(empty_document(), TRIP, PLAN, VERSION,
                                multi_day_plan(["2026-11-11", "2026-11-12"], [1, 2]))
    new_version = str(uuid.uuid4())
    result = replace_generated_plan(old, TRIP, PLAN, new_version,
                                   multi_day_plan(["2026-11-11", "2026-11-12", "2026-11-13"], [1, 2, 5]))
    assert result["schemaVersion"] == 3
    assert "dayLinks" not in result
    assert {day["date"] for day in result["days"]} == {"2026-11-11", "2026-11-12", "2026-11-13"}
    route = [node for node in result["nodes"] if node.get("source")]
    assert len(route) == 8
    assert {node["source"]["versionId"] for node in route} == {new_version}


def test_v4_new_plan_version_replaces_entire_canvas_and_same_version_reopen_preserves_edits(import_app):
    client, engine, service = import_app
    dates = ["2026-11-11", "2026-11-12", "2026-11-13"]
    day_ids = [str(uuid.uuid5(uuid.UUID(TRIP), "canvas-day:" + day_date)) for day_date in dates]
    initial_link = {"id": "00000000-0000-4000-8000-000000000108",
                    "source": day_ids[0], "target": day_ids[1]}
    v4 = {"schemaVersion": 4,
          "days": [{"id": day_ids[0], "date": dates[0], "position": {"x": 10, "y": 15}},
                   {"id": day_ids[1], "date": dates[1], "position": {"x": 300, "y": 15}}],
          "nodes": [], "edges": [], "dayLinks": [initial_link]}
    saved = put_canvas(client, 0, v4)
    assert saved.status_code == 200, saved.text

    service.plan = multi_day_plan(dates, [1, 2, 5])
    first = import_request(client, revision=1)
    assert first.status_code == 200, first.text
    old_document = first.json()["document"]
    old_managed = [node for node in old_document["nodes"] if node.get("source")]
    old_ids = {node["id"] for node in old_managed}
    assert len(old_managed) == 8
    assert len(old_document["edges"]) == 5

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
    edit_response = put_canvas(client, 2, edited, {"planId": PLAN, "versionId": VERSION})
    assert edit_response.status_code == 200, edit_response.text

    service.version = V2
    service.plan = multi_day_plan(dates[:2], [2, 1])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": V2, "p": PLAN, "t": TRIP,
                      "d": json.dumps({"schema": "generation.v1", "document": service.plan,
                                       "sourceStamp": {"inputToken": "new"}})})
    second = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                         json={"versionId": V2, "expectedCanvasRevision": 3})
    assert second.status_code == 200, second.text
    middle = second.json()["document"]
    assert [day["date"] for day in middle["days"]] == dates[:2]
    assert len(middle["nodes"]) == 3
    middle_edits = copy.deepcopy(middle)
    middle_edits["nodes"].extend([manual, manual_route, candidate])
    middle_edits["edges"].append(manual_edge)
    edit_middle = put_canvas(client, 4, middle_edits, {"planId": PLAN, "versionId": V2})
    assert edit_middle.status_code == 200, edit_middle.text

    service.version = V3
    service.plan = multi_day_plan(dates, [1, 2, 5])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": V3, "p": PLAN, "t": TRIP,
                      "d": json.dumps({"schema": "generation.v1", "document": service.plan})})
    third = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                        json={"versionId": V3, "expectedCanvasRevision": 5})
    assert third.status_code == 200, third.text
    latest = third.json()["document"]
    latest_route = [node for node in latest["nodes"] if node.get("intent") == "route" and node.get("source")]
    assert len(latest_route) == 8
    assert {node["dayId"] for node in latest_route} == set(day_ids)
    assert all(node["source"]["versionId"] == V3 for node in latest_route)
    assert not old_ids & {node["id"] for node in latest["nodes"]}
    assert manual not in latest["nodes"] and manual_route not in latest["nodes"] and candidate not in latest["nodes"]
    assert not any(node["id"] == old_route["id"] for node in latest["nodes"])
    assert len(latest["edges"]) == 5
    assert all(edge["source"] not in old_ids and edge["target"] not in old_ids
               for edge in latest["edges"] if edge != manual_edge)
    expected_schedules = {
        node["id"]: {"startAt": node["estimatedStartAt"], "endAt": node["estimatedEndAt"],
                     "locked": node["locked"]}
        for day in service.plan["days"] for node in day["nodes"]
    }
    assert all(node["schedule"] == expected_schedules[node["source"]["nodeId"]] for node in latest_route)
    expected_links = [{
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{day_ids[0]}:{day_ids[1]}")),
        "source": day_ids[0], "target": day_ids[1]}, {
        "id": str(uuid.uuid5(uuid.UUID(TRIP), f"canvas-day-link:{day_ids[1]}:{day_ids[2]}")),
        "source": day_ids[1], "target": day_ids[2]}]
    assert latest["dayLinks"] == expected_links
    assert [day["date"] for day in latest["days"]] == dates

    forged = copy.deepcopy(latest)
    forged_route = next(node for node in forged["nodes"] if node.get("source"))
    forged_route["schedule"]["locked"] = not forged_route["schedule"]["locked"]
    rejected = put_canvas(client, 6, forged, {"planId": PLAN, "versionId": V3})
    assert rejected.status_code == 400
    replay = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                         json={"versionId": V3, "expectedCanvasRevision": 6})
    assert replay.status_code == 200, replay.text
    assert replay.json()["document"] == latest

    deleted = {**latest, "nodes": [], "edges": []}
    delete_response = put_canvas(client, 6, deleted, {"planId": PLAN, "versionId": V3})
    assert delete_response.status_code == 200, delete_response.text
    replay_after_delete = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                                      json={"versionId": V3, "expectedCanvasRevision": 7})
    assert replay_after_delete.status_code == 200, replay_after_delete.text
    assert replay_after_delete.json()["document"]["nodes"] == []

    # A shifted date range replaces even custom frames, cards, links, and layout.
    shifted_dates = ["2026-11-13", "2026-11-14"]
    custom_day = str(uuid.uuid5(uuid.UUID(TRIP), "custom-day"))
    custom_comment = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "custom-comment")), "kind": "comment",
                      "dayId": custom_day, "position": {"x": 999, "y": 999}, "color": "rose", "text": "old"}
    custom_place = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "custom-place")), "kind": "place",
                    "dayId": custom_day, "position": {"x": 1200, "y": 900}, "color": "lime",
                    "contentid": 101, "intent": "candidate"}
    custom_link = {"id": str(uuid.uuid5(uuid.UUID(TRIP), "custom-link")), "source": custom_day,
                   "target": day_ids[0]}
    custom_doc = {**latest, "days": [*latest["days"], {"id": custom_day, "date": None,
                                                           "position": {"x": 999, "y": 999}}],
                  "nodes": [custom_comment, custom_place], "edges": [], "dayLinks": [custom_link]}
    edited_again = put_canvas(client, 7, custom_doc, {"planId": PLAN, "versionId": V3})
    assert edited_again.status_code == 200, edited_again.text
    service.version = V4
    service.plan = multi_day_plan(shifted_dates, [4, 4])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": V4, "p": PLAN, "t": TRIP,
                      "d": json.dumps({"schema": "generation.v1", "document": service.plan})})
    shifted = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                          json={"versionId": V4, "expectedCanvasRevision": 8})
    assert shifted.status_code == 200, shifted.text
    assert [day["date"] for day in shifted.json()["document"]["days"]] == shifted_dates
    assert all(node["source"]["versionId"] == V4 for node in shifted.json()["document"]["nodes"])
    assert custom_comment not in shifted.json()["document"]["nodes"]
    assert custom_place not in shifted.json()["document"]["nodes"]
    assert custom_link not in shifted.json()["document"]["dayLinks"]


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
    saved = put_canvas(client, 1, edited, {"planId": PLAN, "versionId": VERSION})
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
    moved = put_canvas(client, 1, document, {"planId": PLAN, "versionId": VERSION})
    assert moved.status_code == 200
    document["nodes"][0]["schedule"]["locked"] = True
    forged = put_canvas(client, 2, document, {"planId": PLAN, "versionId": VERSION})
    assert forged.status_code == 400
    document = copy.deepcopy(moved.json()["document"])
    document["nodes"], document["edges"] = [], []
    removed = put_canvas(client, 2, document, {"planId": PLAN, "versionId": VERSION})
    assert removed.status_code == 200
    restored = put_canvas(client, 3, moved.json()["document"], {"planId": PLAN, "versionId": VERSION})
    assert restored.status_code == 200


def test_place_replacement_can_clear_source_but_same_location_cannot(import_app):
    client, _, _ = import_app
    state = import_request(client).json()
    original = copy.deepcopy(state["document"])
    original_node = copy.deepcopy(original["nodes"][0])

    same_place = copy.deepcopy(original)
    same_place["nodes"][0].pop("source")
    same_place["nodes"][0].pop("schedule")
    rejected = put_canvas(client, 1, same_place, {"planId": PLAN, "versionId": VERSION})
    assert rejected.status_code == 400

    replaced = copy.deepcopy(original)
    replaced["nodes"][0]["contentid"] = 102
    replaced["nodes"][0].pop("source")
    replaced["nodes"][0].pop("schedule")
    saved = put_canvas(client, 1, replaced, {"planId": PLAN, "versionId": VERSION})
    assert saved.status_code == 200, saved.text
    assert saved.json()["document"]["nodes"][0]["contentid"] == 102
    assert "source" not in saved.json()["document"]["nodes"][0]
    assert "schedule" not in saved.json()["document"]["nodes"][0]

    undo = copy.deepcopy(saved.json()["document"])
    undo["nodes"][0] = original_node
    restored = put_canvas(client, 2, undo, {"planId": PLAN, "versionId": VERSION})
    assert restored.status_code == 200, restored.text
    assert restored.json()["document"]["nodes"][0] == original_node


def test_existing_past_version_candidate_is_rejected_under_current_source_fence(import_app):
    client, engine, service = import_app
    first = import_request(client).json()
    service.version = V2
    service.plan = generated_plan()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO plan_versions VALUES(:v,:p,:t,:d)"),
                     {"v": V2, "p": PLAN, "t": TRIP,
                      "d": json.dumps({"schema": "generation.v1", "document": service.plan})})
    current = client.post(f"/api/v1/trips/{TRIP}/plans/{PLAN}/canvas", headers=headers(),
                          json={"versionId": V2, "expectedCanvasRevision": 1}).json()
    obsolete_candidate = copy.deepcopy(first["document"]["nodes"][0])
    obsolete_candidate["intent"] = "candidate"
    corrupt = copy.deepcopy(current["document"])
    corrupt["nodes"].append(obsolete_candidate)
    with engine.begin() as conn:
        conn.execute(text("UPDATE trip_canvas_documents SET document_json=:d WHERE trip_id=:t"),
                     {"d": json.dumps(corrupt), "t": TRIP})
    response = put_canvas(client, current["revision"], corrupt, {"planId": PLAN, "versionId": V2})
    assert response.status_code == 400


def test_put_requires_exact_source_fence_and_old_tab_fence_conflicts(import_app):
    client, _, _ = import_app
    state = import_request(client).json()
    body = {"expectedRevision": 1, "document": state["document"]}
    missing = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(), json=body)
    assert missing.status_code == 400
    old_fence = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(),
                           json={**body, "expectedSourcePlan": None})
    assert old_fence.status_code == 409
    assert old_fence.json()["error"]["code"] == "VERSION_CONFLICT"


def test_put_idempotency_fingerprint_includes_source_fence(import_app):
    client, _, _ = import_app
    state = import_request(client).json()
    key = str(uuid.uuid4())
    payload = {"expectedRevision": 1, "expectedSourcePlan": {"planId": PLAN, "versionId": VERSION},
               "document": state["document"]}
    first = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(key), json=payload)
    assert first.status_code == 200
    changed = {**payload, "expectedSourcePlan": None}
    replay = client.put(f"/api/v1/trips/{TRIP}/canvas", headers=headers(key), json=changed)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_generated_capacity_overflow_is_explicit():
    document = empty_document()
    document["nodes"] = [{"id": str(uuid.uuid4()), "kind": "comment", "dayId": DAY, "position": {"x": 0, "y": 0}, "color": "default", "text": "note"} for _ in range(500)]
    result = replace_generated_plan(document, TRIP, PLAN, VERSION, generated_plan())
    assert len(result["nodes"]) == 2


def test_new_version_replaces_the_entire_canvas():
    old = replace_generated_plan(empty_document(), TRIP, PLAN, VERSION, generated_plan())
    alternative = {**copy.deepcopy(old["nodes"][0]), "intent": "candidate"}
    old["nodes"][0] = alternative
    note = {"id": PLACE, "kind": "comment", "dayId": DAY, "position": {"x": 10, "y": 20},
            "color": "rose", "text": "keep this note"}
    old["nodes"].append(note)
    next_version = str(uuid.uuid4())
    new = replace_generated_plan(old, TRIP, PLAN, next_version, generated_plan())
    assert alternative not in new["nodes"] and note not in new["nodes"]
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
    obsolete = replace_generated_plan(empty, TRIP, PLAN, str(uuid.uuid4()), generated_plan())
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
    stale = client.put(url, headers=headers(), json={"document": stacked, "expectedRevision": current["revision"],
                                                    "expectedSourcePlan": {"planId": PLAN, "versionId": VERSION}})
    assert stale.status_code == 409
    edited = put_canvas(client, cleaned["revision"], stacked, {"planId": PLAN, "versionId": VERSION})
    assert edited.status_code == 200, edited.text
    with engine.connect() as conn:
        stored = json.loads(conn.execute(text("SELECT document_json FROM trip_canvas_documents")).scalar_one())
    assert stored["nodes"] == [*visible["document"]["nodes"], note]
    assert stored["edges"] == visible["document"]["edges"]
