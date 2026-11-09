"""Explicit generation.v1 writes to the existing plan/version/node/edge model."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text


class PlanStoreError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decoded(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _at(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        raise PlanStoreError("INVALID_PLAN_DOCUMENT") from None


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


def _cost(value):
    return value if type(value) is int and 0 <= value <= 2**32 - 1 else None


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except Exception:
        raise PlanStoreError("INVALID_PLAN_DOCUMENT") from None


def create_generated_plan(conn, trip_id, member_id, snapshot, document, source_stamp):
    """Caller holds source/authorization locks and commits job and plan together."""
    source = snapshot.as_dict()
    identities = source.get("_identity", {}).get("members", [])
    revisions = {member["memberId"]: member["preferenceVersion"] for member in identities}
    snapshot_id, plan_id, version_id = (str(uuid.uuid4()) for _ in range(3))
    cv = source.get("conditionsVersion", source_stamp.get("conditionsVersion"))
    if type(cv) is not int or cv < 1 or document.get("schemaVersion") != 1:
        raise PlanStoreError("INVALID_PLAN_DOCUMENT")
    payload = {"schema": "generation.v1", "document": document, "sourceStamp": source_stamp}
    conn.execute(text("INSERT INTO group_snapshots(id,trip_id,condition_revision,member_revisions,input_snapshot,"
                      "merged_preferences,merged_constraints,conflicts,status,schema_version) "
                      "VALUES(:id,:trip,:cv,:revisions,:input,'[]','[]','[]','READY',1)"),
                 {"id": snapshot_id, "trip": trip_id, "cv": cv, "revisions": _json(revisions),
                  "input": _json({"schema": "generation.v1", "conditions": source["conditions"],
                                  "sourceFingerprint": snapshot.fingerprint,
                                  "catalogDigest": source_stamp.get("catalogDigest")})})
    conn.execute(text("INSERT INTO plans(id,trip_id,name) VALUES(:id,:trip,:name)"),
                 {"id": plan_id, "trip": trip_id, "name": "여행 일정"})
    conn.execute(text("INSERT INTO plan_versions(id,trip_id,plan_id,group_snapshot_id,status,origin,"
                      "rules_snapshot,excluded_place_ids,validation_result,summary,created_by,schema_version) "
                      "VALUES(:id,:trip,:plan,:snapshot,'DRAFT','GENERATED','[]','[]',:validation,:summary,:member,1)"),
                 {"id": version_id, "trip": trip_id, "plan": plan_id, "snapshot": snapshot_id,
                  "validation": _json(payload), "summary": "생성된 일정 초안 · 검증 상태 확인 필요", "member": member_id})
    seen = set()
    all_nodes = {}
    for day_index, day in enumerate(document["days"], 1):
        for position, node in enumerate(day["nodes"], 1):
            nid = _id(node["id"])
            key = _id(node["nodeKey"])
            if nid in seen or node["kind"] not in {"START", "PLACE", "END"}:
                raise PlanStoreError("INVALID_PLAN_DOCUMENT")
            seen.add(nid)
            title = node["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise PlanStoreError("INVALID_PLAN_DOCUMENT")
            cid = node["contentid"]
            if node["kind"] == "PLACE" and (type(cid) is not int or not -(2**31) <= cid < 2**31):
                raise PlanStoreError("INVALID_PLAN_DOCUMENT")
            all_nodes[nid] = node
            allowed = {k: node[k] for k in ("contentid", "title", "coordinate", "feeLabel", "stayMinutes") if k in node}
            if node.get("openingHoursText"):
                allowed["openingHoursText"] = node["openingHoursText"]
            conn.execute(text("INSERT INTO itinerary_nodes(id,version_id,node_key,day_no,order_index,node_kind,"
                              "place_id,title,start_at,end_at,estimated_cost_krw,is_locked,place_snapshot,schema_version) "
                              "VALUES(:id,:version,:key,:day,:position,:kind,:place,:title,:start,:end,:cost,:locked,:snapshot,1)"),
                         {"id": nid, "version": version_id, "key": key, "day": day_index, "position": position,
                          "kind": node["kind"], "place": cid, "title": title,
                          "start": _at(node.get("estimatedStartAt")), "end": _at(node.get("estimatedEndAt")),
                          "cost": _cost(node.get("estimatedCostKrw")), "locked": int(node.get("locked") is True),
                          "snapshot": _json({"schema": "generation.v1", **allowed})})
    modes = {"car": "CAR", "transit": "PUBLIC_TRANSIT", "walk": "WALK"}
    for day in document["days"]:
        for edge in day["edges"]:
            # CHARTER has no legacy enum counterpart. Preserve its logical edge in
            # generation.v1 JSON instead of inventing a CAR/WALK database value.
            if edge["mode"] not in modes:
                continue
            origin, destination = edge["fromNodeId"], edge["toNodeId"]
            if origin not in all_nodes or destination not in all_nodes or origin == destination:
                raise PlanStoreError("INVALID_PLAN_DOCUMENT")
            seconds = edge.get("durationSeconds")
            duration = (seconds + 59) // 60 if type(seconds) is int and seconds >= 0 else None
            conn.execute(text("INSERT INTO itinerary_edges(id,version_id,from_node_id,to_node_id,travel_mode,"
                              "departure_at,arrival_at,duration_min,distance_m,estimated_cost_krw,route_snapshot,schema_version) "
                              "VALUES(:id,:version,:origin,:destination,:mode,:departure,:arrival,:duration,:distance,:cost,:route,1)"),
                         {"id": _id(edge["id"]), "version": version_id, "origin": origin, "destination": destination,
                          "mode": modes[edge["mode"]], "departure": _at(all_nodes[origin].get("estimatedEndAt")),
                          "arrival": _at(all_nodes[destination].get("estimatedStartAt")),
                          "duration": _cost(duration), "distance": _cost(edge.get("distanceMeters")),
                          "cost": _cost(edge.get("knownCostKrw")), "route": _json({"schema": "generation.v1", **edge})})
    conn.execute(text("UPDATE plans SET active_draft_id=:version WHERE id=:id AND trip_id=:trip"),
                 {"version": version_id, "id": plan_id, "trip": trip_id})
    return get_plan_record(conn, trip_id, plan_id)


def get_plan_record(conn, trip_id, plan_id):
    row = conn.execute(text("SELECT p.id,p.lock_version, v.id AS version_id,v.status,v.version_no,v.saved_at,"
                            "v.validation_result FROM plans p JOIN plan_versions v "
                            "ON v.id=COALESCE(p.active_draft_id,p.current_version_id) AND v.plan_id=p.id AND v.trip_id=p.trip_id "
                            "WHERE p.id=:plan AND p.trip_id=:trip"), {"plan": plan_id, "trip": trip_id}).mappings().first()
    if not row:
        return None
    payload = _decoded(row["validation_result"])
    if not isinstance(payload, dict) or payload.get("schema") != "generation.v1":
        return None
    return {"public": {"planId": row["id"], "versionId": row["version_id"], "status": row["status"],
                       "lockVersion": int(row["lock_version"]), "versionNo": row["version_no"],
                       "savedAt": _iso(row["saved_at"]), "document": payload["document"]},
            "sourceStamp": payload["sourceStamp"]}


def save_generated_plan(conn, trip_id, plan_id, version_id, member_id, expected_lock_version,
                        acknowledge_unverified, current_input_token):
    plan = conn.execute(text("SELECT id,lock_version,active_draft_id,current_version_id FROM plans "
                             "WHERE id=:plan AND trip_id=:trip FOR UPDATE"),
                        {"plan": plan_id, "trip": trip_id}).mappings().first()
    if not plan:
        raise PlanStoreError("PLAN_NOT_FOUND")
    if type(expected_lock_version) is not int or expected_lock_version != plan["lock_version"]:
        raise PlanStoreError("VERSION_CONFLICT")
    version = conn.execute(text("SELECT id,status,validation_result FROM plan_versions "
                                "WHERE id=:version AND plan_id=:plan AND trip_id=:trip FOR UPDATE"),
                           {"version": version_id, "plan": plan_id, "trip": trip_id}).mappings().first()
    if not version or version_id not in {plan["active_draft_id"], plan["current_version_id"]}:
        raise PlanStoreError("VERSION_CONFLICT")
    payload = _decoded(version["validation_result"])
    if not isinstance(payload, dict) or payload.get("schema") != "generation.v1":
        raise PlanStoreError("PLAN_NOT_FOUND")
    if payload["sourceStamp"].get("inputToken") != current_input_token:
        raise PlanStoreError("STALE_GENERATION_INPUT")
    validation = payload["document"]["validation"]
    if validation.get("status") == "violated" or validation.get("violations"):
        raise PlanStoreError("VALIDATION_FAILED")
    if validation.get("status") != "verified" and acknowledge_unverified is not True:
        raise PlanStoreError("UNVERIFIED_ACK_REQUIRED")
    if version["status"] == "SAVED":
        return get_plan_record(conn, trip_id, plan_id)
    if version["status"] != "DRAFT" or plan["active_draft_id"] != version_id:
        raise PlanStoreError("VERSION_CONFLICT")
    next_no = conn.execute(text("SELECT COALESCE(MAX(version_no),0)+1 FROM plan_versions WHERE plan_id=:plan"),
                           {"plan": plan_id}).scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn.execute(text("UPDATE plan_versions SET status='SAVED',version_no=:number,saved_at=:now "
                      "WHERE id=:version AND trip_id=:trip"),
                 {"number": next_no, "now": now, "version": version_id, "trip": trip_id})
    conn.execute(text("UPDATE plans SET current_version_id=:version,active_draft_id=NULL,lock_version=lock_version+1 "
                      "WHERE id=:plan AND trip_id=:trip"), {"version": version_id, "plan": plan_id, "trip": trip_id})
    return get_plan_record(conn, trip_id, plan_id)
