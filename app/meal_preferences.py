"""Soft scoring for ordinary meal timing recommendations."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


_KST = timezone(timedelta(hours=9))
_MEAL_BANDS = ((time(11, 30), time(14, 30)), (time(17, 30), time(20, 0)))


def _local_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(_KST)
    except (TypeError, ValueError, OverflowError):
        return None


def _applicable_bands(day, activity_window=None):
    start_node = next((node for node in day.get("nodes", []) if node.get("kind") == "START"), None)
    start_value = ((start_node or {}).get("plannedStartAt") or (start_node or {}).get("estimatedStartAt"))
    start = _local_datetime(start_value)
    if start is None and isinstance(activity_window, dict):
        raw_start = activity_window.get("start")
        try:
            start = datetime.combine(date.fromisoformat(day["date"]), time.fromisoformat(raw_start), _KST)
        except (KeyError, TypeError, ValueError):
            start = None
    if start is None:
        return None
    day_start = start.date()
    configured_end = None
    if isinstance(activity_window, dict):
        try:
            configured_end = datetime.combine(day_start, time.fromisoformat(activity_window["end"]), _KST)
        except (KeyError, TypeError, ValueError):
            configured_end = None
    bands = []
    for band_start, band_end in _MEAL_BANDS:
        lower = datetime.combine(day_start, band_start, _KST)
        upper = datetime.combine(day_start, band_end, _KST)
        applicable_end = min(upper, configured_end) if configured_end is not None else upper
        if applicable_end - max(lower, start) >= timedelta(minutes=45):
            bands.append((lower, upper))
    return bands


def meal_deviation_minutes(plan, candidate_pool, anchors=None, *, activity_window=None):
    """Return total minutes outside applicable meal bands, or None if unknown.

    Anchored meals occupy their usual meal slot but are exempt from the soft
    score: their user supplied timing is a hard condition elsewhere.
    """
    anchors = anchors or set()
    total = 0
    found = False
    meal_visits = [
        (day, node) for day in plan.get("days", []) for node in day.get("nodes", [])
        if candidate_pool.get(node.get("contentid"), {}).get("planningRole", node.get("planningRole")) == "meal_stop"
    ]
    if not meal_visits:
        return 0.0
    for day in plan.get("days", []):
        day_meals = [node for node in day.get("nodes", [])
                     if candidate_pool.get(node.get("contentid"), {}).get("planningRole", node.get("planningRole")) == "meal_stop"]
        if not any((day.get("date"), node.get("contentid")) not in anchors for node in day_meals):
            continue
        bands = _applicable_bands(day, activity_window)
        if bands is None:
            return None
        meal_index = 0
        for node in day.get("nodes", []):
            cid = node.get("contentid")
            row = candidate_pool.get(cid, {})
            if row.get("planningRole", node.get("planningRole")) != "meal_stop":
                continue
            if meal_index < len(bands):
                if (day.get("date"), cid) not in anchors:
                    found = True
                    value = node.get("plannedStartAt") or node.get("estimatedStartAt")
                    local = _local_datetime(value)
                    end_value = node.get("plannedEndAt") or node.get("estimatedEndAt")
                    local_end = _local_datetime(end_value)
                    if local is None or local_end is None:
                        return None
                    lower, upper = bands[meal_index]
                    if local < lower:
                        total += int((lower - local).total_seconds() // 60)
                    if local_end > upper:
                        total += int((local_end - upper).total_seconds() // 60)
            meal_index += 1
    return float(total) if found else 0.0
