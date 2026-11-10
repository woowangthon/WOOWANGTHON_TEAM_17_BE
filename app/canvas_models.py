"""Strict validation for the shared canvas v3 wire document."""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date
from typing import Any


class CanvasValidationError(ValueError):
    pass


COLORS = {"default", "lime", "sky", "rose", "amber"}
INTENTS = {"route", "candidate"}
DAY_KEYS = {"id", "date"}
EDGE_KEYS = {"id", "source", "target"}
COMMON_NODE_KEYS = {"id", "kind", "dayId", "position", "color"}


def canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise CanvasValidationError("UUID required")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise CanvasValidationError("UUID required") from None
    result = str(parsed)
    if value != result:
        raise CanvasValidationError("canonical UUID required")
    return result


def _text(value: Any, low: int, high: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise CanvasValidationError("invalid text")
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF or (code < 32 and (not multiline or char != "\n")) or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029):
            raise CanvasValidationError("invalid text")
    return value


def validate_document(document: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CanvasValidationError("invalid JSON") from None
    if len(encoded) > 200 * 1024:
        raise CanvasValidationError("document too large")
    if (not isinstance(document, dict) or set(document) != {"schemaVersion", "days", "nodes", "edges"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 3):
        raise CanvasValidationError("invalid document shape")
    days, nodes, edges = document["days"], document["nodes"], document["edges"]
    if not isinstance(days, list) or not 1 <= len(days) <= 31 or not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(edges, list) or len(edges) > 100:
        raise CanvasValidationError("document limits exceeded")
    day_ids: set[str] = set()
    dates: set[str] = set()
    clean_days = []
    for day in days:
        if not isinstance(day, dict) or set(day) != DAY_KEYS:
            raise CanvasValidationError("invalid day")
        day_id = canonical_uuid(day["id"])
        if day_id in day_ids:
            raise CanvasValidationError("duplicate day")
        day_ids.add(day_id)
        day_date = day["date"]
        if day_date is not None:
            if not isinstance(day_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_date):
                raise CanvasValidationError("invalid date")
            try:
                date.fromisoformat(day_date)
            except ValueError:
                raise CanvasValidationError("invalid date") from None
            if day_date in dates:
                raise CanvasValidationError("duplicate date")
            dates.add(day_date)
        clean_days.append({"id": day_id, "date": day_date})
    ids: set[str] = set()
    clean_nodes = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise CanvasValidationError("invalid node")
        kind = node.get("kind")
        expected = COMMON_NODE_KEYS | ({"contentid", "intent"} if kind == "place" else {"text"} if kind == "comment" else set())
        if set(node) != expected or not isinstance(kind, str) or kind not in {"place", "comment"}:
            raise CanvasValidationError("invalid node fields")
        node_id = canonical_uuid(node["id"])
        day_id = canonical_uuid(node["dayId"])
        if node_id in ids or day_id not in day_ids:
            raise CanvasValidationError("duplicate node or unknown day")
        ids.add(node_id)
        position = node["position"]
        if not isinstance(position, dict) or set(position) != {"x", "y"}:
            raise CanvasValidationError("invalid position")
        coords = {}
        for axis in ("x", "y"):
            value = position[axis]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 100000:
                raise CanvasValidationError("invalid position")
            coords[axis] = value
        if not isinstance(node["color"], str) or node["color"] not in COLORS:
            raise CanvasValidationError("invalid color")
        clean = {"id": node_id, "kind": kind, "dayId": day_id, "position": coords, "color": node["color"]}
        if kind == "place":
            contentid = node["contentid"]
            if isinstance(contentid, bool) or not isinstance(contentid, int) or not -(2**31) <= contentid <= 2**31 - 1 or not isinstance(node["intent"], str) or node["intent"] not in INTENTS:
                raise CanvasValidationError("invalid place node")
            clean.update(contentid=contentid, intent=node["intent"])
        else:
            clean["text"] = _text(node["text"], 1, 1000, multiline=True)
            if not clean["text"].strip():
                raise CanvasValidationError("empty comment")
        clean_nodes.append(clean)
        nodes_by_id[node_id] = clean
    clean_edges = []
    edge_ids: set[str] = set()
    outgoing: dict[str, str] = {}
    incoming: dict[str, str] = {}
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != EDGE_KEYS:
            raise CanvasValidationError("invalid edge")
        edge_id, source, target = (canonical_uuid(edge[k]) for k in ("id", "source", "target"))
        if edge_id in edge_ids or source == target or source not in nodes_by_id or target not in nodes_by_id:
            raise CanvasValidationError("invalid edge reference")
        left, right = nodes_by_id[source], nodes_by_id[target]
        if left["kind"] != "place" or right["kind"] != "place" or left["dayId"] != right["dayId"] or source in outgoing or target in incoming:
            raise CanvasValidationError("invalid edge graph")
        edge_ids.add(edge_id)
        outgoing[source], incoming[target] = target, source
        clean_edges.append({"id": edge_id, "source": source, "target": target})
    for start in outgoing:
        seen: set[str] = set()
        current = start
        while current in outgoing:
            if current in seen:
                raise CanvasValidationError("edge cycle")
            seen.add(current)
            current = outgoing[current]
    return {"schemaVersion": 3, "days": clean_days, "nodes": clean_nodes, "edges": clean_edges}
