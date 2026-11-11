"""Fit a proposed day's stops to local clocks before persisting the shared itinerary."""
import copy
import math
import re
import time
from datetime import timedelta

from .confirmed_schedule import arrange_overnight_stops
from .plan_validation import _clock, _iso, _opening, _route_facts, is_late_booking, validate_generated_plan
from .routing import _coord, unknown_route


class ScheduleWindowError(ValueError):
    code = "SCHEDULE_WINDOW_INFEASIBLE"


class RouteBudget:
    """Planning and validation share provider results, a deadline and a call budget."""
    def __init__(self, router, deadline):
        self.router = router
        self.deadline = deadline
        self.results = {}

    def route(self, origin, destination, mode):
        key = (mode, origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"])
        if key not in self.results:
            if len(self.results) >= 64 or time.monotonic() >= self.deadline:
                return unknown_route(mode, "ROUTE_LIMIT_REACHED")
            self.results[key] = _route_facts(self.router, origin, destination, mode)
        return self.results[key]

    def travel_minutes(self, origin, destination, mode):
        if _coord(origin) and _coord(destination):
            route = self.route(origin, destination, mode)
            if route["status"] == "verified":
                return math.ceil(route["durationSeconds"] / 60), True
        # Reserve a transfer slot in the proposed timetable. The route stays unverified.
        return 30, False


def _meal_windows(start, end):
    day = start.date().isoformat()
    return [(max(start, _clock(day, lo)), min(end, _clock(day, hi)))
            for lo, hi in (("11:30", "14:30"), ("17:30", "20:00"))
            if min(end, _clock(day, hi)) - max(start, _clock(day, lo)) >= timedelta(minutes=45)]


def _fit_day(stops, *, day, start, end, origin, destination, anchors, details, roles, mode, routes, overnight, alternatives):
    stops = list(stops)
    meal_windows = _meal_windows(start, end)
    while stops:
        now, earliest, previous, meals = start, start, origin, 0
        slots, warnings = {}, set()
        problem = None
        closed_stop = False
        for index, place in enumerate(stops):
            cid = place["contentid"]
            anchor = anchors.get(cid, {})
            minutes, known = routes.travel_minutes(previous, place.get("coordinate"), mode)
            if not known:
                warnings.add("TRANSFER_TIME_ESTIMATED")
            now += timedelta(minutes=minutes)
            if known:
                earliest += timedelta(minutes=minutes)
            if anchor.get("arrivalDeadline"):
                due = _clock(day, anchor["arrivalDeadline"])
                if earliest > due:
                    problem = index
                    break
                now = min(now, due)
            if anchor.get("startTime"):
                fixed_start = _clock(day, anchor["startTime"])
                if earliest > fixed_start and cid not in overnight:
                    problem = index
                    break
                now = max(now, fixed_start) if cid in overnight else fixed_start
                earliest = max(earliest, fixed_start)
            detail = details.get(cid) or {}
            hours = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)\s*[~–-]\s*([01]\d|2[0-3]):([0-5]\d)",
                                 str(detail.get("openingHoursText") or "").strip())
            if hours and not anchor.get("startTime"):
                opens = _clock(day, f"{hours[1]}:{hours[2]}")
                closes = _clock(day, f"{hours[3]}:{hours[4]}")
                if opens < closes:
                    now = max(now, opens)
                    earliest = max(earliest, opens)
            is_meal = roles.get(cid) == "meal_stop"
            duration = anchor.get("stayMinutes") or detail.get("estimatedStayMinutes")
            known_stay = type(duration) is int and 1 <= duration <= 1440
            if not known_stay:
                duration = 0 if cid in overnight else 60
                if cid not in overnight:
                    warnings.add("STAY_TIME_ESTIMATED")
            meal_end = None
            if is_meal and meals < len(meal_windows):
                meal_start, meal_end = meal_windows[meals]
                if not anchor.get("startTime"):
                    now = max(now, meal_start)
                    earliest = max(earliest, meal_start)
                meals += 1
            finish = now + timedelta(minutes=duration)
            if anchor.get("endTime"):
                finish = _clock(day, anchor["endTime"])
                earliest = finish
            elif known_stay:
                earliest += timedelta(minutes=duration)
            slots[cid] = (now, finish)
            # Explicit late appointments may extend this day. Ordinary visits use the daily limit.
            limit = max(end, finish) if is_late_booking(anchor, end) else end
            if finish < now or finish > limit or (meal_end and finish > meal_end and not anchor):
                problem = index
                break
            if cid not in overnight and _opening(detail, now, finish) == "violated":
                problem, closed_stop = index, True
                break
            now, previous = finish, place.get("coordinate")
        if problem is None and destination is not None:
            minutes, known = routes.travel_minutes(previous, destination, mode)
            if not known:
                warnings.add("TRANSFER_TIME_ESTIMATED")
            if now + timedelta(minutes=minutes) > end and not is_late_booking(anchors.get(stops[-1]["contentid"], {}), end):
                problem = len(stops) - 1
        if problem is None:
            if meals < len(meal_windows):
                warnings.add("MEAL_SLOT_UNFILLED")
            return stops, slots, warnings
        # Remove a flexible visit before the collision. Preserve fixed bookings and meals first.
        choices = [i for i in range(problem, -1, -1) if stops[i]["contentid"] not in anchors]
        if closed_stop and roles.get(stops[problem]["contentid"]) == "meal_stop" and problem in choices and alternatives:
            # These incoming IDs already passed the same date, region and hard preference filters.
            stops[problem] = alternatives.pop(0)
            continue
        if len(stops) <= 1 or not choices or (closed_stop and stops[problem]["contentid"] in anchors):
            raise ScheduleWindowError()
        remove = problem if closed_stop else next((i for i in choices if roles.get(stops[i]["contentid"]) != "meal_stop"), choices[0])
        stops.pop(remove)
    return [], {}, {"MEAL_SLOT_UNFILLED"} if meal_windows else set()


def build_scheduled_plan(draft, catalog, conditions, preferences, settings, router, timeout_seconds=90):
    from .place_catalog import _coord as catalog_coord
    deadline = time.monotonic() + timeout_seconds
    routes = RouteBudget(router, deadline)
    result = copy.deepcopy(draft)
    places = {p["contentid"]: p for p in catalog.places}
    roles = {cid: p.get("planningRole") for cid, p in places.items()}
    details = {}
    for detail in catalog.details:
        cid = detail["contentid"]
        details[cid] = None if cid in details else detail
    departure = places.get(conditions.get("departureContentid"))
    origin = catalog_coord(departure) if departure else (settings.get("departureAnchor") or {}).get("coordinate")
    anchors = {}
    for entry in settings.get("reviewedCommonFields", []):
        for a in entry.get("anchors", []):
            anchors[(a["date"], a["contentid"])] = a
    for a in settings.get("confirmedScheduleAnchors", []):
        anchors[(a["date"], a["contentid"])] = a
    slots, warnings = {}, set()
    used = {p["contentid"] for day in result["days"] for p in day["places"]}
    for index, section in enumerate(result["days"]):
        day = section["date"]
        window = settings["activityWindow"]
        start = _clock(day, (conditions.get("departureTime") if index == 0 else None) or window["start"])
        end = _clock(day, window["end"])
        if start >= end:
            raise ScheduleWindowError()
        day_anchors = {cid: a for (date, cid), a in anchors.items() if date == day}
        overnight = {cid for cid in day_anchors if roles.get(cid) in {"overnight_camping", "overnight_lodging"}}
        destination = (settings.get("returnAnchor") or {}).get("coordinate") if index == len(result["days"]) - 1 else None
        cities = {places[p["contentid"]].get("adminCity") for p in section["places"] if p["contentid"] in places}
        eligible = [c["contentid"] for c in result.get("mealCandidates", ())
                    if c["contentid"] not in used and day in c["allowedDates"] and roles.get(c["contentid"]) == "meal_stop"]
        eligible.sort(key=lambda cid: places[cid].get("adminCity") not in cities)
        alternatives = [{"contentid": cid, "coordinate": catalog_coord(places[cid])} for cid in eligible[:8]]
        section["places"], slots[day], day_warnings = _fit_day(
            section["places"], day=day, start=start, end=end, origin=origin, destination=destination,
            anchors=day_anchors, details=details, roles=roles, mode=conditions["transport"], routes=routes, overnight=overnight,
            alternatives=alternatives)
        used.update(p["contentid"] for p in section["places"])
        warnings.update(day_warnings)
        last = section["places"][-1] if section["places"] else {}
        origin = last.get("coordinate") if last.get("contentid") in overnight else None
    result, adjusted = arrange_overnight_stops(result, settings)
    plan = validate_generated_plan(result, catalog, conditions, preferences, adjusted, routes,
                                   timeout_seconds=max(.01, deadline - time.monotonic()), planned_slots=slots)
    for section in plan["days"]:
        for node in section["nodes"]:
            slot = slots[section["date"]].get(node.get("contentid"))
            if slot:
                node["plannedStartAt"], node["plannedEndAt"] = map(_iso, slot)
    plan["validation"]["warnings"] = sorted(set(plan["validation"]["warnings"]) | warnings)
    return plan
