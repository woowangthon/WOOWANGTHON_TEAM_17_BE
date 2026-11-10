"""Append real generated PLACE nodes without replacing collaborative edits."""
from __future__ import annotations

import copy
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
    return {"source": {"planId": plan_id, "versionId": version_id, "nodeId": node["id"]},
            "schedule": {"startAt": _timestamp(node.get("estimatedStartAt")), "endAt": _timestamp(node.get("estimatedEndAt")),
                         "locked": node.get("locked") is True}}


def append_generated_plan(document, trip_id, plan_id, version_id, plan):
    """Pure append. Idempotency/tombstones are handled by the surrounding transaction."""
    result = copy.deepcopy(document)
    original_count = len(result["nodes"])
    days_by_date = {day["date"]: day["id"] for day in result["days"] if day["date"]}
    try:
        canonical_uuid(plan_id)
        canonical_uuid(version_id)
        for day in plan["days"]:
            day_date = day["date"]
            day_id = days_by_date.get(day_date)
            if day_id is None:
                day_id = str(uuid.uuid5(uuid.UUID(trip_id), "canvas-day:" + day_date))
                result["days"].append({"id": day_id, "date": day_date})
                days_by_date[day_date] = day_id
            y = max((node["position"]["y"] + 220 for node in result["nodes"] if node["dayId"] == day_id), default=100)
            if y > 99000:
                y = 100
            mapped = {}
            for node in day["nodes"]:
                if node["kind"] != "PLACE":
                    continue
                original = canonical_uuid(node["id"])
                node_id = imported_node_id(version_id, original)
                mapped[original] = node_id
                result["nodes"].append({"id": node_id, "kind": "place", "dayId": day_id,
                                        "position": {"x": 100 + (len(mapped) - 1) * 270, "y": y},
                                        "color": "default", "contentid": node["contentid"], "intent": "route",
                                        **_metadata(plan_id, version_id, node)})
            for edge in day["edges"]:
                source, target = mapped.get(edge["fromNodeId"]), mapped.get(edge["toNodeId"])
                if source and target:
                    result["edges"].append({"id": str(uuid.uuid5(uuid.UUID(version_id), "edge:" + canonical_uuid(edge["id"]))),
                                            "source": source, "target": target})
        if len(result["nodes"]) == original_count:
            raise CanvasImportError("PLAN_INVALID")
        return validate_document(result)
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


def validate_managed_nodes(conn, trip_id, document, existing):
    """Keep facts immutable; legitimate undo may restore canonical plan metadata."""
    previous = {node["id"]: node for node in (existing or {}).get("nodes", [])}
    versions = {}
    for node in document["nodes"]:
        old = previous.get(node["id"], {})
        if "source" not in node:
            if "source" in old and node.get("contentid") == old.get("contentid"):
                raise CanvasImportError("VALIDATION_ERROR", 400)
            continue
        if (old.get("source") == node["source"] and old.get("schedule") == node["schedule"]
                and old.get("contentid") == node["contentid"]):
            continue
        source = node["source"]
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
