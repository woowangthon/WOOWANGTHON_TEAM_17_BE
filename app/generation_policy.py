"""Shared deterministic generation-policy primitives."""
from __future__ import annotations

import math
from typing import Any, Mapping


GENERATION_POLICY_VERSION = "atomic-route-market-v7"
CAR_LEG_MAX_SECONDS = 40 * 60


def place_distance_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Straight-line distance for ordering trials; feasibility always uses routed legs."""
    ca, cb = a.get("coordinate"), b.get("coordinate")
    if not isinstance(ca, dict) or not isinstance(cb, dict):
        return 30.0
    try:
        lat1, lon1 = math.radians(float(ca["latitude"])), math.radians(float(ca["longitude"]))
        lat2, lon2 = math.radians(float(cb["latitude"])), math.radians(float(cb["longitude"]))
        if not all(math.isfinite(x) for x in (lat1, lon1, lat2, lon2)):
            return 30.0
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))
    except (KeyError, TypeError, ValueError, OverflowError):
        return 30.0


def car_leg_exceeds_limit(mode: Any, duration_seconds: Any, *, from_kind: Any, to_kind: Any) -> bool:
    """Return true only for a known car PLACE-to-PLACE leg over forty minutes."""
    return (mode == "car" and from_kind == "PLACE" and to_kind == "PLACE"
            and type(duration_seconds) is int and duration_seconds > CAR_LEG_MAX_SECONDS)


_ROLE_CATEGORIES = {
    "sightseeing_stop": "sightseeing",
    "culture_stop": "culture",
    "activity_stop": "activity",
    "shopping_stop": "shopping",
    "meal_stop": "meal",
    "overnight_lodging": "lodging",
    "overnight_camping": "lodging",
    "date_specific_event": "event",
}


def category_key(row: Mapping[str, Any] | None) -> str | None:
    """Build an internal diversity key from catalog-backed facts only."""
    if not isinstance(row, Mapping):
        return None
    # Exact public catalog tag is the only supported fine-grained category.
    tags = row.get("matchedTags")
    if isinstance(tags, (list, tuple)) and "카페" in tags:
        return "tag:카페"
    content_type_id = row.get("contenttypeid")
    if type(content_type_id) is int:
        return f"contenttypeid:{content_type_id}"
    name = row.get("contentTypeName")
    if isinstance(name, str) and name.strip():
        return "contentTypeName:" + name.strip()
    role = row.get("planningRole")
    return f"planningRole:{_ROLE_CATEGORIES[role]}" if role in _ROLE_CATEGORIES else None
