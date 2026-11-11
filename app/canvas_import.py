"""Replace the shared canvas with the current generated plan."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from .canvas_models import CanvasValidationError, _timestamp, canonical_uuid, validate_document


class CanvasImportError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _decoded(value):
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def imported_node_id(version_id, node_id):
    return str(uuid.uuid5(uuid.UUID(version_id), "place:" + node_id))


def _metadata(plan_id, version_id, node):
    start, end = node.get("estimatedStartAt"), node.get("estimatedEndAt")
    if (not start or not end) and node.get("plannedStartAt") and node.get("plannedEndAt"):
        start, end = node["plannedStartAt"], node["plannedEndAt"]
    return {"source": {"planId": plan_id, "versionId": version_id, "nodeId": node["id"]},
            "schedule": {"startAt": _timestamp(start), "endAt": _timestamp(end),
                         "locked": node.get("locked") is True}}


def _route_position(index, y):
    row, column = divmod(index, 2)
    return {"x": 100 + (1 - column if row % 2 else column) * 270, "y": y + row * 185}


def current_route_document(document, version_id):
    """Legacy canvases may contain several imports of the same day's route."""
    obsolete = {
        node["id"] for node in document["nodes"]
        if node.get("intent") == "route" and node.get("source")
        and node["source"]["versionId"] != version_id
    }
    if not obsolete:
        return document
    nodes = [n for n in document["nodes"] if n["id"] not in obsolete]
    # Old imports allocated a horizontal row below all previous versions. Compact
    # only untouched rows in a stacked legacy document; retain custom placement.
    positions = {}
    for day in document["days"]:
        day_nodes = [n for n in nodes if n["dayId"] == day["id"]]
        managed = sorted((n for n in day_nodes if n.get("intent") == "route" and n.get("source")),
                         key=lambda n: n["position"]["x"])
        if len(managed) <= 2 or any(n["position"] != {"x": 100 + i * 270, "y": managed[0]["position"]["y"]}
                                    for i, n in enumerate(managed)):
            continue
        manual = [n for n in day_nodes if not (n.get("intent") == "route" and n.get("source"))]
        first_y = max((n["position"]["y"] + 220 for n in manual), default=100)
        if first_y + ((len(managed) - 1) // 2) * 185 <= 100000:
            positions.update({n["id"]: _route_position(i, first_y) for i, n in enumerate(managed)})
    return {**document,
            "nodes": [{**n, "position": positions[n["id"]]} if n["id"] in positions else n for n in nodes],
            "edges": [e for e in document["edges"] if e["source"] not in obsolete and e["target"] not in obsolete]}


def replace_generated_plan(document, trip_id, plan_id, version_id, plan):
    """Build a clean canvas from one plan while retaining only the schema version."""
    try:
        canonical_uuid(plan_id)
        canonical_uuid(version_id)
        schema_version = document.get("schemaVersion", 3)
        if schema_version not in (3, 4):
            raise CanvasImportError("PLAN_INVALID")
        result = {"schemaVersion": schema_version, "days": [], "nodes": [], "edges": []}
        if schema_version == 4:
            result["dayLinks"] = []
        for day_index, day in enumerate(plan["days"]):
            day_date = day["date"]
            day_id = str(uuid.uuid5(uuid.UUID(trip_id), "canvas-day:" + day_date))
            result["days"].append({"id": day_id, "date": day_date})
            if schema_version == 4 and day_index:
                previous_id = result["days"][day_index - 1]["id"]
                result["dayLinks"].append({
                    "id": str(uuid.uuid5(uuid.UUID(trip_id), f"canvas-day-link:{previous_id}:{day_id}")),
                    "source": previous_id, "target": day_id})
            y = 100
            mapped = {}
            place_index = 0
            for node in day["nodes"]:
                if node["kind"] != "PLACE":
                    continue
                original = canonical_uuid(node["id"])
                node_id = imported_node_id(version_id, original)
                mapped[original] = node_id
                result["nodes"].append({"id": node_id, "kind": "place", "dayId": day_id,
                                         "position": _route_position(place_index, y),
                                         "color": "default", "contentid": node["contentid"], "intent": "route",
                                         **_metadata(plan_id, version_id, node)})
                place_index += 1
            for edge in day["edges"]:
                source, target = mapped.get(edge["fromNodeId"]), mapped.get(edge["toNodeId"])
                if source and target:
                    result["edges"].append({"id": str(uuid.uuid5(uuid.UUID(version_id), "edge:" + canonical_uuid(edge["id"]))),
                                            "source": source, "target": target})
        if not result["nodes"]:
            raise CanvasImportError("PLAN_INVALID")
        return validate_document(result)
    except CanvasImportError:
        raise
    except CanvasValidationError as exc:
        code = "CANVAS_CAPACITY_EXCEEDED" if "limit" in str(exc) or "large" in str(exc) else "PLAN_INVALID"
        raise CanvasImportError(code) from None
    except (KeyError, ValueError, TypeError, AttributeError):
        raise CanvasImportError("PLAN_INVALID") from None


def load_import_payload(conn, trip_id, plan_id, version_id):
    suffix = "" if conn.dialect.name == "sqlite" else " FOR UPDATE"
    row = conn.execute(text("SELECT validation_result FROM plan_versions WHERE id=:v AND plan_id=:p AND trip_id=:t" + suffix),
                       {"v": version_id, "p": plan_id, "t": trip_id}).mappings().first()
    if not row:
        raise CanvasImportError("PLAN_NOT_FOUND", 404)
    payload = _decoded(row["validation_result"])
    if not isinstance(payload, dict) or payload.get("schema") != "generation.v1":
        raise CanvasImportError("PLAN_NOT_FOUND", 404)
    return payload


def mark_imported(conn, trip_id, plan_id, version_id, payload, revision):
    # A version-level marker survives request expiry, later imports and deleting
    # all imported nodes. No cleanup of request rows can accidentally restore them.
    marked = {**payload, "_canvasImport": {"tripId": trip_id, "revision": revision}}
    conn.execute(text("UPDATE plan_versions SET validation_result=:payload WHERE id=:v AND plan_id=:p AND trip_id=:t"),
                 {"payload": json.dumps(marked, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                  "v": version_id, "p": plan_id, "t": trip_id})


def validate_managed_nodes(conn, trip_id, document, existing, source_plan=None):
    """Keep facts immutable; legitimate undo may restore canonical plan metadata."""
    previous = {node["id"]: node for node in (existing or {}).get("nodes", [])}
    versions = {}
    for node in document["nodes"]:
        old = previous.get(node["id"], {})
        if "source" not in node:
            if "source" in old and node.get("contentid") == old.get("contentid"):
                raise CanvasImportError("VALIDATION_ERROR", 400)
            continue
        source = node["source"]
        current_source = source_plan
        if not current_source or (source["planId"], source["versionId"]) != (current_source["planId"], current_source["versionId"]):
            raise CanvasImportError("VALIDATION_ERROR", 400)
        if (old.get("source") == node["source"] and old.get("schedule") == node["schedule"]
                and old.get("contentid") == node["contentid"]):
            continue
        key = (source["planId"], source["versionId"])
        if key not in versions:
            payload = load_import_payload(conn, trip_id, *key)
            marker = payload.get("_canvasImport", {})
            if marker.get("tripId") != trip_id:
                raise CanvasImportError("VALIDATION_ERROR", 400)
            versions[key] = {n["id"]: n for day in payload["document"]["days"] for n in day["nodes"] if n["kind"] == "PLACE"}
        original = versions[key].get(source["nodeId"])
        if (not original or node["id"] != imported_node_id(source["versionId"], source["nodeId"])
                or node["contentid"] != original["contentid"]
                or {"source": node["source"], "schedule": node["schedule"]} != _metadata(*key, original)):
            raise CanvasImportError("VALIDATION_ERROR", 400)
