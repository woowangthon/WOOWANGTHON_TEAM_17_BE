"""Fit a proposed day's stops to local clocks before persisting the shared itinerary."""
import copy
import math
import re
import time
from datetime import timedelta

from .confirmed_schedule import arrange_overnight_stops
from .generation_policy import car_leg_exceeds_limit
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
        self.optimization_calls = 0
        self.optimization_call_limit = 0

    def route(self, origin, destination, mode):
        key = (mode, origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"])
        if key not in self.results:
            if len(self.results) >= 64 or time.monotonic() >= self.deadline:
                return unknown_route(mode, "ROUTE_LIMIT_REACHED")
            if self.optimization_call_limit and self.optimization_calls >= self.optimization_call_limit:
                return unknown_route(mode, "OPTIMIZATION_ROUTE_BUDGET_REACHED")
            if self.optimization_call_limit:
                self.optimization_calls += 1
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


def _fit_day(stops, *, day, start, end, origin, destination, anchors, details, roles, mode, routes, overnight, alternatives,
             strict=False, origin_is_place=False):
    stops = list(stops)
    meal_windows = _meal_windows(start, end)
    while stops:
        now, earliest, previous, meals = start, start, origin, 0
        previous_is_place = origin_is_place
        slots, warnings = {}, set()
        problem = None
        closed_stop = False
        car_limit_problem = False
        for index, place in enumerate(stops):
            cid = place["contentid"]
            anchor = anchors.get(cid, {})
            if _coord(previous) and _coord(place.get("coordinate")):
                route = routes.route(previous, place["coordinate"], mode)
                if route.get("status") == "verified" and car_leg_exceeds_limit(
                        mode, route.get("durationSeconds"), from_kind="PLACE" if previous_is_place else "START",
                        to_kind="PLACE"):
                    problem = index
                    car_limit_problem = True
                    break
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
            if is_meal and meals < len(meal_windows):
                # General meal times are recommendations. Explicit anchor
                # times and actual operating hours remain hard constraints.
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
            if finish < now or finish > limit:
                problem = index
                break
            if cid not in overnight and _opening(detail, now, finish) == "violated":
                problem, closed_stop = index, True
                break
            now, previous, previous_is_place = finish, place.get("coordinate"), True
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
        if strict:
            # Optimization candidates must preserve their visit set. The normal
            # pre-optimization path may still repair a draft by dropping stops.
            raise ScheduleWindowError()
        # Remove a flexible visit before the collision. Preserve fixed bookings and meals first.
        choices = [i for i in range(problem, -1, -1) if stops[i]["contentid"] not in anchors
                   and (not car_limit_problem or roles.get(stops[i]["contentid"]) != "meal_stop")]
        if closed_stop and roles.get(stops[problem]["contentid"]) == "meal_stop" and problem in choices and alternatives:
            # These incoming IDs already passed the same date, region and hard preference filters.
            stops[problem] = alternatives.pop(0)
            continue
        if len(stops) <= 1 or not choices or (closed_stop and stops[problem]["contentid"] in anchors):
            raise ScheduleWindowError()
        remove = problem if closed_stop else next((i for i in choices if roles.get(stops[i]["contentid"]) != "meal_stop"), choices[0])
        stops.pop(remove)
    return [], {}, {"MEAL_SLOT_UNFILLED"} if meal_windows else set()


def build_scheduled_plan(draft, catalog, conditions, preferences, settings, router, timeout_seconds=90, *,
                         optimize=True, optimization_report=None, semantic_context=None,
                         use_alns=True, use_semantics=True):
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
    initial_origin = origin
    anchors = {}
    for entry in settings.get("reviewedCommonFields", []):
        for a in entry.get("anchors", []):
            anchors[(a["date"], a["contentid"])] = a
    for a in settings.get("confirmedScheduleAnchors", []):
        anchors[(a["date"], a["contentid"])] = a
    original_anchor_positions = _anchor_positions(settings, include_lodging=False)
    search_budget = None

    # Repair runs against the untouched proposed visit set before legacy fitting
    # gets a chance to remove visits. It has its own strict whole-trip evaluator;
    # route facts still share the same cache and 64-result hard cap.
    if optimize and use_alns and result.get("optimizationCandidates"):
        from .alns_repair import SearchBudget, repair_itinerary
        source_draft = copy.deepcopy(result)
        normalized_source, source_settings = arrange_overnight_stops(copy.deepcopy(source_draft), copy.deepcopy(settings))
        try:
            reference_plan = validate_generated_plan(
                normalized_source, catalog, conditions, preferences, source_settings, routes,
                timeout_seconds=max(.01, deadline - time.monotonic()))
        except Exception:
            reference_plan = {"validation": {"violations": [], "warnings": []}, "days": []}

        def strict_evaluate(candidate_draft):
            candidate, candidate_settings = arrange_overnight_stops(
                copy.deepcopy(candidate_draft), copy.deepcopy(settings))
            if not _hotel_constraints_preserved(normalized_source, source_settings, candidate, candidate_settings):
                raise ScheduleWindowError()
            candidate_anchors = {}
            for entry in candidate_settings.get("reviewedCommonFields", []):
                for anchor in entry.get("anchors", []):
                    candidate_anchors[(anchor["date"], anchor["contentid"])] = anchor
            for anchor in candidate_settings.get("confirmedScheduleAnchors", []):
                candidate_anchors[(anchor["date"], anchor["contentid"])] = anchor
            candidate_slots, candidate_warnings = {}, set()
            candidate_origin = initial_origin
            for day_index, section in enumerate(candidate["days"]):
                day = section["date"]
                window = candidate_settings["activityWindow"]
                start = _clock(day, (conditions.get("departureTime") if day_index == 0 else None) or window["start"])
                end = _clock(day, window["end"])
                if start >= end:
                    raise ScheduleWindowError()
                day_anchors = {cid: a for (anchor_date, cid), a in candidate_anchors.items() if anchor_date == day}
                overnight = {cid for cid in day_anchors
                             if roles.get(cid) in {"overnight_camping", "overnight_lodging"}}
                destination = (candidate_settings.get("returnAnchor") or {}).get("coordinate") if day_index == len(candidate["days"]) - 1 else None
                section["places"], candidate_slots[day], day_warnings = _fit_day(
                    section["places"], day=day, start=start, end=end, origin=candidate_origin,
                    destination=destination, anchors=day_anchors, details=details, roles=roles,
                    mode=conditions["transport"], routes=routes, overnight=overnight, alternatives=[],
                    strict=True, origin_is_place=day_index > 0 and candidate_origin is not None)
                candidate_warnings.update(day_warnings)
                last = section["places"][-1] if section["places"] else {}
                candidate_origin = last.get("coordinate") if last.get("contentid") in overnight else None
            if _anchor_positions(candidate_settings, include_lodging=False) != original_anchor_positions:
                raise ScheduleWindowError()
            checked = validate_generated_plan(
                candidate, catalog, conditions, preferences, candidate_settings, routes,
                timeout_seconds=max(.01, deadline - time.monotonic()), planned_slots=candidate_slots)
            for day in checked["days"]:
                for node in day["nodes"]:
                    slot = candidate_slots[day["date"]].get(node.get("contentid"))
                    if slot:
                        node["plannedStartAt"], node["plannedEndAt"] = map(_iso, slot)
            checked["validation"]["warnings"] = sorted(set(checked["validation"]["warnings"]) | candidate_warnings)
            return candidate, checked, candidate_settings

        try:
            strict_scheduled, strict_plan, strict_settings = strict_evaluate(source_draft)
            strict_fits = not strict_plan["validation"].get("violations")
        except Exception:
            strict_scheduled, strict_plan, strict_settings = None, reference_plan, source_settings
            strict_fits = False
        if not strict_fits and time.monotonic() < deadline:
            # Initial strict fit/reference checks are diagnostic work; only new
            # ALNS and legacy optimizer trials use the six-call search allowance.
            routes.optimization_call_limit = 6
            search_budget = SearchBudget(max_trials=24, seconds=min(1.0, max(0.0, deadline - time.monotonic())))
            candidates = source_draft.get("optimizationCandidates", [])
            try:
                repaired, repaired_plan, repaired_settings, report = repair_itinerary(
                    normalized_source, settings, candidates, strict_evaluate, reference_plan=reference_plan,
                    semantic_context=semantic_context if use_semantics else None,
                    max_visits_per_day=8, max_evaluations=24, search_budget=search_budget,
                    route_preferences=settings.get("_routePreferences"), catalog=catalog)
                report["strictBaselineChecks"] = 1
                report["searchEvaluationsUsed"] = search_budget.used_trials
                report["searchEvaluationsRemaining"] = search_budget.remaining
                if optimization_report is not None:
                    optimization_report.update(report)
                if repaired is not None and repaired_plan is not None:
                    # Continue with the existing 2-opt/relocate/TOPTW-inspired
                    # pass using the same elapsed-time, route, and evaluation
                    # budgets. A repaired feasible itinerary is its baseline.
                    try:
                        from .itinerary_optimizer import optimize_itinerary
                        optimized, optimized_plan, optimized_settings, local_report = optimize_itinerary(
                            repaired, repaired_settings, candidates, strict_evaluate,
                            max_visits_per_day=8, max_evaluations=24,
                            time_budget_seconds=min(1.0, max(0.0, deadline - time.monotonic())),
                            semantic_context=semantic_context if use_semantics else None,
                            search_budget=search_budget, route_preferences=settings.get("_routePreferences"), catalog=catalog)
                        if local_report.get("acceptedMoves", 0) > 0:
                            repaired, repaired_plan, repaired_settings = optimized, optimized_plan, optimized_settings
                    except Exception:
                        # The fully checked repair remains the valid baseline
                        # even if the optional follow-up search fails.
                        local_report = {"fallback": "OPTIMIZER_ERROR", "attemptedMoves": 0, "acceptedMoves": 0}
                    if optimization_report is not None:
                        optimization_report.update(local_report)
                        optimization_report["alns"] = report
                        optimization_report["combinedMethod"] = "adaptive-large-neighborhood-then-local-search"
                        optimization_report["searchEvaluationsUsed"] = search_budget.used_trials
                        optimization_report["searchEvaluationsRemaining"] = search_budget.remaining
                    return repaired_plan
            except Exception:
                if optimization_report is not None:
                    optimization_report["alnsFallback"] = "ALNS_ERROR"
            # Let the legacy baseline fitter use its normal route cache budget.
            # Keep the ALNS uncached-call count; the later optimizer resumes it.
            routes.optimization_call_limit = 0
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
            alternatives=alternatives, origin_is_place=index > 0 and origin is not None)
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
    if optimization_report is not None:
        optimization_report.update({
            "baselineVisitCount": sum(len(day["places"]) for day in result["days"]),
            "finalVisitCount": sum(len(day["places"]) for day in result["days"]),
            "baselineTravelSeconds": _verified_travel_seconds(plan),
            "finalTravelSeconds": _verified_travel_seconds(plan),
            "attemptedMoves": 0,
            "acceptedMoves": 0,
        })
    if optimize and time.monotonic() < deadline:
        # Each neighbor is scheduled without the legacy drop/replace repair, then
        # independently checked with the same route budget and deadline.
        baseline_anchor_positions = _anchor_positions(adjusted, include_lodging=False)
        # At most six new, uncached provider routes are requested by the local
        # search. Existing verified baseline edges remain cached and reusable.
        routes.optimization_call_limit = 6
        if search_budget is None:
            from .alns_repair import SearchBudget
            search_budget = SearchBudget(max_trials=24, seconds=min(1.0, max(0.0, deadline - time.monotonic())))

        def evaluate(candidate_draft):
            candidate = copy.deepcopy(candidate_draft)
            candidate_settings = copy.deepcopy(settings)
            # Put overnight hotels last before calculating times or routes. The
            # arrangement helper rebinds their internal slot after cross-day
            # allocation; hotel date and time facts remain anchored separately.
            candidate, candidate_settings = arrange_overnight_stops(candidate, candidate_settings)
            if not _hotel_constraints_preserved(result, adjusted, candidate, candidate_settings):
                raise ScheduleWindowError()
            candidate_anchors = {}
            for entry in candidate_settings.get("reviewedCommonFields", []):
                for anchor in entry.get("anchors", []):
                    candidate_anchors[(anchor["date"], anchor["contentid"])] = anchor
            for anchor in candidate_settings.get("confirmedScheduleAnchors", []):
                candidate_anchors[(anchor["date"], anchor["contentid"])] = anchor
            candidate_slots, candidate_warnings = {}, set()
            candidate_origin = initial_origin
            for day_index, section in enumerate(candidate["days"]):
                day = section["date"]
                window = candidate_settings["activityWindow"]
                start = _clock(day, (conditions.get("departureTime") if day_index == 0 else None) or window["start"])
                end = _clock(day, window["end"])
                if start >= end:
                    raise ScheduleWindowError()
                day_anchors = {cid: a for (anchor_date, cid), a in candidate_anchors.items() if anchor_date == day}
                day_overnight = {cid for cid in day_anchors if roles.get(cid) in {"overnight_camping", "overnight_lodging"}}
                day_destination = (candidate_settings.get("returnAnchor") or {}).get("coordinate") if day_index == len(candidate["days"]) - 1 else None
                section["places"], candidate_slots[day], day_warnings = _fit_day(
                    section["places"], day=day, start=start, end=end, origin=candidate_origin,
                    destination=day_destination, anchors=day_anchors, details=details, roles=roles,
                    mode=conditions["transport"], routes=routes, overnight=day_overnight,
                    alternatives=[], strict=True,
                    origin_is_place=day_index > 0 and candidate_origin is not None)
                candidate_warnings.update(day_warnings)
                last = section["places"][-1] if section["places"] else {}
                candidate_origin = last.get("coordinate") if last.get("contentid") in day_overnight else None
            # arrange_overnight_stops already ran above; schedule only after its
            # hotel-last ordering and derive appointment anchors from that result.
            if (_anchor_positions(candidate_settings, include_lodging=False) != baseline_anchor_positions
                    or not _hotel_constraints_preserved(result, adjusted, candidate, candidate_settings)):
                raise ScheduleWindowError()
            checked = validate_generated_plan(candidate, catalog, conditions, preferences, candidate_settings, routes,
                                               timeout_seconds=max(.01, deadline - time.monotonic()),
                                               planned_slots=candidate_slots)
            for day in checked["days"]:
                for node in day["nodes"]:
                    slot = candidate_slots[day["date"]].get(node.get("contentid"))
                    if slot:
                        node["plannedStartAt"], node["plannedEndAt"] = map(_iso, slot)
            checked["validation"]["warnings"] = sorted(set(checked["validation"]["warnings"]) | candidate_warnings)
            return candidate, checked, candidate_settings

        try:
            from .itinerary_optimizer import optimize_itinerary
            candidates = draft.get("optimizationCandidates", []) if isinstance(draft, dict) else []
            optimized, optimized_plan, optimized_settings, metrics = optimize_itinerary(
                result, adjusted, candidates, evaluate, max_visits_per_day=8,
                max_evaluations=24, time_budget_seconds=min(1.0, max(0.01, deadline - time.monotonic())),
                semantic_context=semantic_context if use_semantics else None, search_budget=search_budget,
                route_preferences=settings.get("_routePreferences"), catalog=catalog)
            metrics["searchEvaluationsUsed"] = search_budget.used_trials
            metrics["searchEvaluationsRemaining"] = search_budget.remaining
            if metrics.get("acceptedMoves", 0) > 0:
                result, plan, adjusted = optimized, optimized_plan, optimized_settings
            if optimization_report is not None:
                optimization_report.update(metrics)
        except Exception:
            # An optimizer error never replaces the valid baseline draft.
            if optimization_report is not None:
                optimization_report["fallback"] = "OPTIMIZER_ERROR"
    return plan


def _verified_travel_seconds(plan):
    total = 0
    for day in plan["days"]:
        for edge in day["edges"]:
            if edge.get("status") != "verified" or type(edge.get("durationSeconds")) is not int:
                return None
            total += edge["durationSeconds"]
    return total


def _anchor_positions(settings, *, include_lodging=True):
    anchors = {}
    for group in settings.get("reviewedCommonFields", []):
        if group.get("field") == "lodging" and not include_lodging:
            continue
        for anchor in group.get("anchors", []):
            if type(anchor.get("position")) is int:
                anchors[(anchor["date"], anchor["contentid"])] = anchor["position"]
    for anchor in settings.get("confirmedScheduleAnchors", []):
        if anchor.get("kind") == "lodging" and not include_lodging:
            continue
        if type(anchor.get("position")) is int:
            anchors[(anchor["date"], anchor["contentid"])] = anchor["position"]
    return anchors


def _hotel_anchor_facts(settings):
    """Return immutable schedule facts for confirmed/shared lodging anchors."""
    result = []
    for group in settings.get("reviewedCommonFields", []):
        if group.get("field") == "lodging":
            result.extend(("reviewed", copy.deepcopy(anchor)) for anchor in group.get("anchors", []))
    result.extend(("confirmed", copy.deepcopy(anchor)) for anchor in settings.get("confirmedScheduleAnchors", [])
                  if anchor.get("kind") == "lodging")
    return result


def _hotel_constraints_preserved(baseline_draft, baseline_settings, candidate_draft, candidate_settings):
    """Preserve each hotel's date/time/checkout and require one last stop per anchor day."""
    before = _hotel_anchor_facts(baseline_settings)
    after = _hotel_anchor_facts(candidate_settings)
    if not before:
        return not after

    def immutable(items):
        # The internal position may change when flexible visits move between
        # days. All other source facts, including dates and checkout, are fixed.
        return sorted((source, repr({key: value for key, value in anchor.items() if key != "position"}))
                      for source, anchor in items)

    if immutable(before) != immutable(after):
        return False
    for draft, settings in ((baseline_draft, baseline_settings), (candidate_draft, candidate_settings)):
        by_day = {day["date"]: [place["contentid"] for place in day["places"]] for day in draft["days"]}
        for _, anchor in _hotel_anchor_facts(settings):
            stops = by_day.get(anchor.get("date"), [])
            cid = anchor.get("contentid")
            if not stops or stops.count(cid) != 1 or stops[-1] != cid:
                return False
    return True
