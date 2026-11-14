"""KST planning estimates with explicit unknown facts and hard-violation checks."""
from __future__ import annotations

import copy
import math
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import place_catalog
from .generation_policy import car_leg_exceeds_limit
from .routing import KakaoRouter, _coord, unknown_route

KST = timezone(timedelta(hours=9), "Asia/Seoul")
UTC = timezone.utc


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


def _clock(day, value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise ValueError("INVALID_ACTIVITY_WINDOW")
    return datetime.fromisoformat(day + "T" + value).replace(tzinfo=KST)


def _domain(reasons, violated=False):
    return {"status": "violated" if violated else "unknown" if reasons else "verified",
            "reasonCodes": sorted(set(reasons))}


def is_late_booking(anchor, closing):
    return any(anchor.get(field) and _clock(closing.date().isoformat(), anchor[field]) >= closing
               for field in ("startTime", "endTime"))


def _price(detail, people):
    """Only explicit free/per-person/group literals, never numbers from prose."""
    text = (detail or {}).get("feeText")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if re.fullmatch(r"(?:입장료\s*[:：]\s*)?(?:무료|0\s*원)", text):
        return 0
    match = re.fullmatch(r"(1인|인당|전체)\s*([0-9]{1,9})\s*원", text)
    if not match:
        return None
    amount = int(match[2])
    return amount if match[1] == "전체" else amount * people


def _opening(detail, start, end):
    if start is None or end is None or not detail:
        return "unknown"
    closed = detail.get("closedDaysText")
    if isinstance(closed, str) and closed.strip():
        closed = closed.strip()
        weekly = re.fullmatch(r"매주\s*([월화수목금토일])요일", closed)
        if weekly:
            if "월화수목금토일".index(weekly[1]) == start.weekday():
                return "violated"
        elif closed not in {"연중무휴", "없음"}:
            return "unknown"
    hours = detail.get("openingHoursText")
    if not isinstance(hours, str):
        return "unknown"
    hours = hours.strip()
    if hours in {"24시간", "상시 개방"}:
        return "verified"
    match = re.fullmatch(r"([0-9]{2}:[0-9]{2})\s*[~–-]\s*([0-9]{2}:[0-9]{2})", hours)
    if not match:
        return "unknown"
    try:
        opened, closing = _clock(start.date().isoformat(), match[1]), _clock(start.date().isoformat(), match[2])
        if closing <= opened:
            return "unknown"
        return "verified" if opened <= start and end <= closing else "violated"
    except ValueError:
        return "unknown"


def _route_facts(router, origin, destination, mode):
    try:
        result = router.route(origin, destination, mode)
        if not isinstance(result, dict) or result.get("mode") != mode or result.get("status") not in {"verified", "unknown"}:
            raise ValueError
        if result["status"] == "unknown":
            # Provider error messages and arbitrary supplied fields are not public data.
            known = {"ROUTE_NOT_CONFIGURED", "ROUTE_MODE_UNSUPPORTED", "ROUTE_COORDINATE_INVALID", "ROUTE_TIMEOUT",
                     "ROUTE_UNAVAILABLE", "ROUTE_RATE_LIMITED", "ROUTE_NOT_FOUND", "ROUTE_RESPONSE_INVALID",
                     "ROUTE_RESPONSE_TOO_LARGE", "ROUTE_LIMIT_REACHED", "VALIDATION_TIMEOUT"}
            code = next((c for c in result.get("reasonCodes", []) if isinstance(c, str) and c in known), "ROUTE_UNAVAILABLE")
            return unknown_route(mode, code)
        for key in ("durationSeconds", "distanceMeters"):
            if type(result.get(key)) is not int or not 0 <= result[key] <= 2**32 - 1:
                raise ValueError
        cost = result.get("knownCostKrw")
        if cost is not None and (type(cost) is not int or not 0 <= cost <= 2**32 - 1):
            raise ValueError
        walks = result.get("walkingSeconds")
        if walks is not None and (not isinstance(walks, list) or len(walks) > 100
                                  or any(type(v) is not int or not 0 <= v <= 2**32 - 1 for v in walks)):
            raise ValueError
        geometry = result.get("geometry", [])
        if not isinstance(geometry, list) or len(geometry) > 2048:
            geometry = []
        if any(not isinstance(p, dict) or set(p) != {"latitude", "longitude"}
               or not _coord({**p, "crs": "WGS84"}) for p in geometry):
            geometry = []
        return {"status": "verified", "mode": mode, "durationSeconds": result["durationSeconds"],
                "distanceMeters": result["distanceMeters"], "knownCostKrw": cost,
                "costComplete": result.get("costComplete") is True, "walkingSeconds": walks,
                "geometry": copy.deepcopy(geometry), "checkedAt": _iso(datetime.now(UTC)), "reasonCodes": []}
    except Exception:
        return unknown_route(mode, "ROUTE_RESPONSE_INVALID")


def validate_generated_plan(draft, catalog, conditions, preferences, settings, router=None, timeout_seconds=90, *, planned_slots=None):
    """Validate a detached candidate draft; unresolved facts stay null, not invented."""
    if hasattr(draft, "as_dict"):
        draft = draft.as_dict()
    router = router or KakaoRouter()
    window = settings["activityWindow"]
    people = conditions["expectedPeople"]
    mode = conditions["transport"]
    walking_limits = [int(p["walking"]) * 60 for p in preferences if p.get("walking") in {"20", "40"}]
    max_walk = min(walking_limits) if walking_limits else None
    details = {}
    for detail in catalog.details:
        details.setdefault(detail["contentid"], []).append(detail)
    roles = {p["contentid"]: p.get("planningRole") for p in catalog.places}
    anchors = {}
    overnight = set()
    for reviewed in settings.get("reviewedCommonFields", []):
        for anchor in reviewed.get("anchors", []):
            anchors[(anchor["date"], anchor["position"], anchor["contentid"])] = anchor
            if reviewed["field"] == "lodging":
                overnight.add((anchor["date"], anchor["contentid"]))
    for anchor in settings.get("confirmedScheduleAnchors", []):
        if not isinstance(anchor, dict):
            continue
        key = (anchor.get("date"), anchor.get("position"), anchor.get("contentid"))
        if all(value is not None for value in key):
            anchors[key] = anchor
        if (anchor.get("kind") == "lodging" and anchor.get("endDate")
                and anchor.get("date") != anchor.get("endDate")):
            overnight.add((anchor["date"], anchor["contentid"]))
    deadline = time.monotonic() + min(90, max(0.01, timeout_seconds))
    counts = 0
    reasons = {key: [] for key in ("route", "walking", "time", "budget", "openingHours")}
    violations, warnings = [], []
    known_cost = 0
    days = []
    previous = None
    previous_overnight = False

    def fail(domain, code, day, cid=None):
        reasons[domain].append(code)
        item = {"code": code, "date": day}
        if cid is not None:
            item["contentid"] = cid
        violations.append(item)

    for day_index, source_day in enumerate(draft["days"]):
        day = source_day["date"]
        first_start = conditions.get("departureTime") if day_index == 0 else None
        opened, closing = _clock(day, first_start or window["start"]), _clock(day, window["end"])
        if closing <= opened:
            raise ValueError("INVALID_ACTIVITY_WINDOW")
        cursor_time = opened
        nodes, edges = [], []
        if day_index == 0:
            start = settings.get("departureAnchor")
            if conditions.get("departureContentid") is not None:
                departure_place = next((p for p in catalog.places if p.get("contentid") == conditions["departureContentid"]), None)
                if departure_place:
                    start = {"label": departure_place.get("title"), "coordinate": place_catalog._coord(departure_place)}
            sid = str(uuid.uuid4())
            previous = {"id": sid, "nodeKey": sid, "kind": "START", "contentid": None,
                        "title": (start or {}).get("label") or conditions.get("departure") or "출발 위치 미확인",
                        "coordinate": (start or {}).get("coordinate"), "estimatedStartAt": _iso(opened),
                        "estimatedEndAt": _iso(opened), "estimatedCostKrw": 0, "feeLabel": "요금 확인 필요",
                        "stayMinutes": 0, "locked": True}
            nodes.append(previous)
        elif not previous_overnight:
            reasons["route"].append("OVERNIGHT_LOCATION_UNRESOLVED")
            reasons["time"].append("OVERNIGHT_LOCATION_UNRESOLVED")
            cursor_time = None
        elif previous and previous.get("estimatedEndAt"):
            prior_end = datetime.fromisoformat(previous["estimatedEndAt"].replace("Z", "+00:00")).astimezone(KST)
            cursor_time = max(opened, prior_end)
        else:
            cursor_time = None
            reasons["time"].append("OVERNIGHT_ARRIVAL_UNKNOWN")

        source_places = source_day["places"]
        visits = [(place, i) for i, place in enumerate(source_places)]
        if day_index == len(draft["days"]) - 1:
            endpoint = settings.get("returnAnchor")
            visits.append(({"contentid": None, "title": (endpoint or {}).get("label") or conditions.get("returnRoute") or "복귀 위치 미확인",
                            "coordinate": (endpoint or {}).get("coordinate")}, -1))
        for source, position in visits:
            cid = source["contentid"]
            ending = position == -1
            presentation = catalog.presentation_place(cid) if not ending else source
            if presentation is None:
                raise ValueError("CATALOG_PLACE_MISSING")
            coordinate = presentation.get("coordinate")
            nid = str(uuid.uuid4())
            node = {"id": nid, "nodeKey": nid, "kind": "END" if ending else "PLACE", "contentid": cid,
                    "title": presentation["title"], "coordinate": coordinate,
                    "estimatedStartAt": None, "estimatedEndAt": None, "estimatedCostKrw": None,
                    "feeLabel": presentation.get("feeLabel", "요금 확인 필요"), "stayMinutes": None,
                    "locked": ending or (day, position, cid) in anchors and anchors[(day, position, cid)].get("kind") != "lodging" and any(
                        anchors[(day, position, cid)].get(field) for field in ("startTime", "arrivalDeadline", "endTime"))}
            if presentation.get("openingHoursText"):
                node["openingHoursText"] = presentation["openingHoursText"]
            route = unknown_route(mode, "ROUTE_COORDINATE_INVALID")
            car_leg_over_limit = False
            if previous and _coord(previous.get("coordinate")) and _coord(coordinate):
                if counts >= 64:
                    route = unknown_route(mode, "ROUTE_LIMIT_REACHED")
                elif time.monotonic() >= deadline:
                    route = unknown_route(mode, "VALIDATION_TIMEOUT")
                elif day_index and position == 0 and not previous_overnight:
                    route = unknown_route(mode, "ROUTE_COORDINATE_INVALID")
                else:
                    counts += 1
                    route = _route_facts(router, previous["coordinate"], coordinate, mode)
                    car_leg_over_limit = (route.get("status") == "verified" and car_leg_exceeds_limit(
                        mode, route.get("durationSeconds"), from_kind=previous["kind"], to_kind=node["kind"]))
                    if time.monotonic() > deadline:
                        route = unknown_route(mode, "VALIDATION_TIMEOUT")
            else:
                reasons["route"].append("RETURN_UNRESOLVED" if ending else "DEPARTURE_UNRESOLVED" if day_index == 0 and position == 0 else "ROUTE_COORDINATE_INVALID")
            if route["status"] != "verified":
                reasons["route"].extend(route["reasonCodes"])
                reasons["walking"].append("WALKING_ROUTE_UNKNOWN")
                reasons["time"].append("ARRIVAL_TIME_UNKNOWN")
                cursor_time = None
            else:
                if cursor_time is not None:
                    cursor_time += timedelta(minutes=math.ceil(route["durationSeconds"] / 60))
                walks = route["walkingSeconds"]
                if walks is None:
                    reasons["walking"].append("WALKING_ROUTE_UNKNOWN")
                elif max_walk is not None and any(seconds > max_walk for seconds in walks):
                    fail("walking", "WALKING_LIMIT_EXCEEDED", day, cid)
            if previous and (car_leg_over_limit or car_leg_exceeds_limit(
                    mode, route.get("durationSeconds"), from_kind=previous["kind"], to_kind=node["kind"])):
                fail("time", "CAR_LEG_TIME_LIMIT_EXCEEDED", day, cid)
            route_cost = route["knownCostKrw"]
            if route_cost is not None:
                route_cost *= people if mode == "transit" else 1
                known_cost += route_cost
                route["knownCostKrw"] = route_cost
            if not route["costComplete"]:
                reasons["budget"].append("TRANSPORT_COST_INCOMPLETE")
            edge = {"id": str(uuid.uuid4()), "fromNodeId": previous["id"], "toNodeId": nid, **route}
            edges.append(edge)
            anchor = anchors.get((day, position, cid), {})
            arrival = cursor_time
            for field, code in (("arrivalDeadline", "FIXED_DEADLINE_MISSED"), ("startTime", "FIXED_START_MISSED")):
                if anchor.get(field):
                    target = _clock(day, anchor[field])
                    if arrival is None:
                        reasons["time"].append("FIXED_TIME_UNVERIFIED")
                    elif arrival > target and not (field == "startTime" and anchor.get("kind") == "lodging"):
                        fail("time", code, day, cid)
            if anchor.get("startTime"):
                target = _clock(day, anchor["startTime"])
                cursor_time = max(cursor_time, target) if cursor_time is not None else (None if anchor.get("kind") == "lodging" else target)
            slot = (planned_slots or {}).get(day, {}).get(cid)
            if slot and cursor_time is not None:
                cursor_time = max(cursor_time, slot[0])
            visit_start = cursor_time
            node["estimatedStartAt"] = _iso(cursor_time)
            detail_rows = details.get(cid, [])
            detail = detail_rows[0] if len(detail_rows) == 1 else None
            duration = anchor.get("stayMinutes") or (detail or {}).get("estimatedStayMinutes")
            if ending:
                duration = 0
            if type(duration) is not int or not 0 <= duration <= 1440 or (duration == 0 and not ending):
                duration = None
            node["stayMinutes"] = duration
            overnight_stay = (not ending and day_index < len(draft["days"]) - 1
                              and position == len(source_places) - 1 and (day, cid) in overnight
                              and roles.get(cid) in {"overnight_lodging", "overnight_camping"})
            if overnight_stay and cursor_time is not None:
                if cursor_time > closing:
                    fail("time", "ACTIVITY_WINDOW_EXCEEDED", day, cid)
                next_day = draft["days"][day_index + 1]["date"]
                target = _clock(next_day, window["start"])
                if anchor.get("checkoutTime"):
                    target = min(target, _clock(next_day, anchor["checkoutTime"]))
                node["stayMinutes"] = max(0, math.ceil((target - cursor_time).total_seconds() / 60))
                cursor_time = max(cursor_time, target)
            elif cursor_time is not None and anchor.get("endTime"):
                target = _clock(day, anchor["endTime"])
                if target < cursor_time:
                    fail("time", "FIXED_END_MISSED", day, cid)
                else:
                    cursor_time = target
            elif cursor_time is not None and duration is not None:
                cursor_time += timedelta(minutes=duration)
            else:
                cursor_time = None
                if duration is None:
                    reasons["time"].append("MEAL_DURATION_UNKNOWN" if roles.get(cid) == "meal_stop" else "STAY_DURATION_UNKNOWN")
            node["estimatedEndAt"] = _iso(cursor_time)
            late_booking = planned_slots is not None and is_late_booking(anchor, closing)
            if ending and planned_slots is not None and previous:
                late_booking = any(a.get("date") == day and a.get("contentid") == previous.get("contentid")
                                   and is_late_booking(a, closing) for a in anchors.values())
            if cursor_time is not None and cursor_time > closing and not overnight_stay and not late_booking:
                fail("time", "ACTIVITY_WINDOW_EXCEEDED", day, cid)
            if not ending:
                price = _price(detail, people)
                if price is None:
                    reasons["budget"].append("PLACE_COST_UNKNOWN")
                else:
                    known_cost += price
                    node["estimatedCostKrw"] = price if price <= 2**32 - 1 else None
                opening = _opening(detail, visit_start, visit_start if overnight_stay else cursor_time)
                if opening == "violated":
                    fail("openingHours", "PLACE_CLOSED_AT_VISIT", day, cid)
                elif opening == "unknown":
                    reasons["openingHours"].append("OPENING_HOURS_UNKNOWN")
            nodes.append(node)
            previous = node
        last_cid = source_places[-1]["contentid"] if source_places else None
        previous_overnight = ((day, last_cid) in overnight and roles.get(last_cid) in {"overnight_lodging", "overnight_camping"})
        days.append({"date": day, "nodes": nodes, "edges": edges})
    if known_cost > conditions["budget"]:
        fail("budget", "KNOWN_COST_EXCEEDS_BUDGET", draft["days"][-1]["date"])
    # Catalog fees are not a complete ledger of all meals, fuel, parking or incidentals.
    reasons["budget"].append("OTHER_TRAVEL_EXPENSES_UNKNOWN")
    domains = {}
    violation_codes = {v["code"] for v in violations}
    for key in reasons:
        domains[key] = _domain(reasons[key], any(code in violation_codes for code in reasons[key]))
    status = "violated" if violations else "unknown" if any(d["status"] == "unknown" for d in domains.values()) else "verified"
    validation = {"status": status, **domains, "violations": violations,
                  "warnings": sorted(set(warnings + [code for codes in reasons.values() for code in codes])),
                  "knownCostKrw": known_cost, "totalCostKrw": None}
    return {"schemaVersion": 1, "days": days, "validation": validation, "createdAt": _iso(datetime.now(UTC))}
