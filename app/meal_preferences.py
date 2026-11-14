"""Soft scoring for ordinary meal timing recommendations."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


_KST = timezone(timedelta(hours=9))
_MEAL_BANDS = ((time(11, 30), time(14, 30)), (time(17, 30), time(20, 0)))
_NAMED_MEAL_BANDS = {
    "breakfast": (time(7, 0), time(10, 0)),
    "lunch": (time(11, 30), time(14, 30)),
    "dinner": (time(17, 30), time(20, 0)),
}


def requested_meal_periods(compiled):
    return next((rule["mealPeriods"] for rule in compiled.get("countRules", ())
                 if rule.get("dimension") == "meals" and isinstance(rule.get("mealPeriods"), list)), None)


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


def _day_clock_bounds(day, activity_window=None):
    """Return a day's activity start/end as KST datetimes when available."""
    start_node = next((node for node in day.get("nodes", []) if node.get("kind") == "START"), None)
    start_value = ((start_node or {}).get("plannedStartAt") or (start_node or {}).get("estimatedStartAt"))
    start = _local_datetime(start_value)
    configured_start = None
    if isinstance(activity_window, dict):
        try:
            configured_start = datetime.combine(date.fromisoformat(day["date"]),
                                                time.fromisoformat(activity_window["start"]), _KST)
        except (KeyError, TypeError, ValueError):
            configured_start = None
    if start is None:
        start = configured_start
    if start is None:
        return None, None
    if configured_start is not None:
        start = max(start, configured_start)
    end = None
    if isinstance(activity_window, dict):
        try:
            end = datetime.combine(start.date(), time.fromisoformat(activity_window["end"]), _KST)
        except (KeyError, TypeError, ValueError):
            end = None
    return start, end


def _named_period_windows(day, meal_periods, activity_window=None):
    """Return applicable `(name, lower, upper)` windows in period order."""
    start, configured_end = _day_clock_bounds(day, activity_window)
    if start is None:
        return None
    windows = []
    for name in meal_periods or ():
        if name not in _NAMED_MEAL_BANDS:
            continue
        band_start, band_end = _NAMED_MEAL_BANDS[name]
        lower = datetime.combine(start.date(), band_start, _KST)
        upper = datetime.combine(start.date(), band_end, _KST)
        applicable_end = min(upper, configured_end) if configured_end is not None else upper
        if applicable_end - max(lower, start) >= timedelta(minutes=45):
            windows.append((name, lower, applicable_end))
    return windows


def _is_meal(node, candidate_pool):
    return candidate_pool.get(node.get("contentid"), {}).get(
        "planningRole", node.get("planningRole")) == "meal_stop"


def _named_meal_assignments(day, candidate_pool, meal_periods, anchors=None, *, activity_window=None):
    """Assign meal visits to applicable named periods without moving anchors."""
    windows = _named_period_windows(day, meal_periods, activity_window)
    if windows is None:
        return None
    anchors = anchors or set()
    day_date = day.get("date")
    meals = [node for node in day.get("nodes", []) if _is_meal(node, candidate_pool)]
    used = set()
    assignments = {}
    period_cursor = 0
    # Reserve anchored periods up front, including anchors later in route order.
    for node in meals:
        if (day_date, node.get("contentid")) not in anchors:
            continue
        actual = _local_datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
        if actual is not None:
            occupied = next((index for index, (_name, lower, upper) in enumerate(windows)
                             if lower <= actual < upper), None)
            if occupied is not None:
                used.add(occupied)
    for node in meals:
        cid = node.get("contentid")
        if (day_date, cid) in anchors:
            actual = _local_datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
            if actual is not None:
                occupied = next((index for index, (_name, lower, upper) in enumerate(windows)
                                 if lower <= actual < upper), None)
                if occupied is not None:
                    period_cursor = max(period_cursor, occupied + 1)
            continue
        index = next((i for i in range(period_cursor, len(windows)) if i not in used), None)
        if index is None:
            continue
        name, lower, upper = windows[index]
        start, _end = _day_clock_bounds(day, activity_window)
        earliest = max(lower, start)
        if earliest < upper:
            assignments[cid] = (name, earliest, upper)
            used.add(index)
            period_cursor = index + 1
    return assignments


def named_meal_earliest_starts(day, candidate_pool, meal_periods, anchors=None, *, activity_window=None):
    """Map unanchored restaurant IDs to earliest applicable KST meal starts.

    Periods are considered in the supplied order. A fixed restaurant visit only
    occupies a period when its actual KST start falls inside that period's
    applicable window. Unknown and out-of-window anchors remain untouched and
    do not consume a named period.
    """
    assignments = _named_meal_assignments(
        day, candidate_pool, meal_periods, anchors, activity_window=activity_window)
    if assignments is None:
        return {}
    return {cid: values[1] for cid, values in assignments.items()}


def unserved_named_meal_periods(day, candidate_pool, meal_periods, anchors=None, *, activity_window=None):
    """List applicable requested periods with no restaurant start in-window.

    A period with no restaurant stop is unserved. The caller can use this to
    expose missed soft named-period goals even when the final day has no meals.
    """
    windows = _named_period_windows(day, meal_periods, activity_window)
    if windows is None:
        return []
    meals = [node for node in day.get("nodes", []) if _is_meal(node, candidate_pool)]
    served = set()
    for node in meals:
        actual = _local_datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
        if actual is None:
            continue
        served.update(name for name, lower, upper in windows if lower <= actual < upper)
    return [name for name, _lower, _upper in windows if name not in served]


def meal_deviation_minutes(plan, candidate_pool, anchors=None, *, activity_window=None, meal_periods=None):
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
        if meal_periods is not None:
            assignments = _named_meal_assignments(
                day, candidate_pool, meal_periods, anchors, activity_window=activity_window)
            if assignments is None:
                return None
            for node in day_meals:
                cid = node.get("contentid")
                if (day.get("date"), cid) in anchors or cid not in assignments:
                    continue
                found = True
                value = node.get("plannedStartAt") or node.get("estimatedStartAt")
                local = _local_datetime(value)
                end_value = node.get("plannedEndAt") or node.get("estimatedEndAt")
                local_end = _local_datetime(end_value)
                if local is None or local_end is None:
                    return None
                _name, lower, upper = assignments[cid]
                if local < lower:
                    total += int((lower - local).total_seconds() // 60)
                if local_end > upper:
                    total += int((local_end - upper).total_seconds() // 60)
            continue
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
