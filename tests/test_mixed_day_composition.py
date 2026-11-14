"""Synthetic regressions for separate requested activity and meal counts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.day_schedule import build_scheduled_plan

DAY1 = "2026-12-12"
DAY2 = "2026-12-13"


def coord(cid):
    return {"latitude": 36.5, "longitude": 127 + cid / 10000, "crs": "WGS84"}


class SyntheticCatalog:
    def __init__(self):
        roles = {1: "meal_stop", 2: "meal_stop", 3: "meal_stop", 4: "meal_stop",
                 10: "sightseeing_stop", 11: "sightseeing_stop", 12: "sightseeing_stop"}
        self.places = [{"contentid": cid, "title": f"Synthetic {cid}", "planningRole": role,
                        "contenttypeid": 12 if role == "sightseeing_stop" else 39,
                        "cat3": "A0202" if role == "sightseeing_stop" else "A0502"}
                       for cid, role in roles.items()]
        self.details = [{"contentid": cid, "estimatedStayMinutes": 45,
                         "openingHoursText": "09:00~22:00", "feeText": "무료"}
                        for cid in roles]

    def presentation_place(self, cid):
        return {"contentid": cid, "title": f"Synthetic {cid}", "coordinate": coord(cid)}


class NearbyRouter:
    def __init__(self):
        self.calls = []

    def route(self, origin, destination, mode):
        self.calls.append((origin["longitude"], destination["longitude"]))
        return {"status": "verified", "mode": mode, "durationSeconds": 180,
                "distanceMeters": 100, "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": [], "geometry": [], "reasonCodes": []}


def inputs(*, activity=True):
    catalog = SyntheticCatalog()
    pool = []
    for cid, role, allowed in (
        (1, "meal_stop", [DAY1]), (2, "meal_stop", [DAY1]),
        (3, "meal_stop", [DAY2]), (4, "meal_stop", [DAY2]),
        (10, "sightseeing_stop", [DAY1]), (11, "sightseeing_stop", [DAY2]),
        (12, "sightseeing_stop", [DAY1, DAY2]),
    ):
        pool.append({"contentid": cid, "title": f"Synthetic {cid}", "planningRole": role,
                     "allowedDates": allowed, "selectionRank": cid, "groupScore": 0.0,
                     "estimatedStayMinutes": 45, "coordinate": coord(cid),
                     "categoryKey": f"{role}:{cid}"})
    count_rules = []
    if activity:
        count_rules.append({"dimension": "sightseeing", "scope": "day", "operator": "min", "count": 1,
                            "required": False, "polarity": "prefer"})
    count_rules.append({"dimension": "meals", "scope": "day", "operator": "min", "count": 2,
                        "required": False, "polarity": "prefer"})
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [],
                "departureAnchor": {"label": "start", "coordinate": coord(0)},
                "returnAnchor": {"label": "return", "coordinate": coord(20)},
                "_routePreferences": {"countRules": count_rules, "walkingRules": [], "issues": [], "softWarnings": []}}
    draft = {"days": [
        {"date": DAY1, "places": [catalog.presentation_place(1), catalog.presentation_place(2)]},
        {"date": DAY2, "places": [catalog.presentation_place(3), catalog.presentation_place(4)]},
    ], "optimizationCandidates": pool}
    return draft, catalog, settings, pool


def test_two_day_scheduler_recovers_a_sightseeing_stop_without_reducing_daily_meals():
    draft, catalog, settings, pool = inputs()
    report, router = {}, NearbyRouter()
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                               [], settings, router, optimization_report=report)
    by_day = {day["date"]: [node for node in day["nodes"] if node["kind"] == "PLACE"]
              for day in plan["days"]}
    for day, ids in ((DAY1, {1, 2}), (DAY2, {3, 4})):
        chosen = {node["contentid"] for node in by_day[day]}
        assert len(chosen & ids) == 2
        assert len(chosen & {10, 11, 12}) >= 1
    assert not plan["validation"]["violations"]
    assert "REQUESTED_ACTIVITY_UNFULFILLED" not in plan["validation"]["warnings"]
    assert report["searchEvaluationsUsed"] <= 24
    assert router.calls and len(router.calls) <= 12  # baseline routes plus six uncached search routes


def test_food_only_goal_does_not_create_a_tourism_floor_or_warning():
    draft, catalog, settings, _pool = inputs(activity=False)
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, NearbyRouter())
    for day in plan["days"]:
        chosen = [node for node in day["nodes"] if node["kind"] == "PLACE"]
        assert len(chosen) == 2
        assert all(node["contentid"] in {1, 2, 3, 4} for node in chosen)
    assert "REQUESTED_ACTIVITY_UNFULFILLED" not in plan["validation"]["warnings"]


def test_optional_visits_keep_the_original_schedule_when_extra_tolls_exceed_the_budget():
    draft, catalog, settings, _ = inputs()

    class TollRouter(NearbyRouter):
        def route(self, origin, destination, mode):
            return {**super().route(origin, destination, mode), "knownCostKrw": 10, "costComplete": False}

    conditions = {"transport": "car", "expectedPeople": 1, "budget": 45}
    router = TollRouter()
    plan = build_scheduled_plan(draft, catalog, conditions, [], settings, router)
    assert not plan["validation"]["violations"]
    assert plan["validation"]["knownCostKrw"] == 40
    assert [[n["contentid"] for n in d["nodes"] if n["kind"] == "PLACE"] for d in plan["days"]] == [[1, 2], [3, 4]]
    assert "REQUESTED_ACTIVITY_UNFULFILLED" in plan["validation"]["warnings"]
    assert len(router.calls) <= 10


def test_closed_first_choice_and_unpriced_car_legs_allow_another_activity():
    draft, catalog, settings, _ = inputs()
    next(d for d in catalog.details if d["contentid"] == 10)["openingHoursText"] = "06:00~06:30"

    class CarRouter(NearbyRouter):
        def route(self, origin, destination, mode):
            return {**super().route(origin, destination, mode), "costComplete": False}

    router = CarRouter()
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 1, "budget": 10000},
                                [], settings, router)
    visits = [{node["contentid"] for node in day["nodes"] if node["kind"] == "PLACE"} for day in plan["days"]]
    assert all(day & {11, 12} for day in visits)
    assert all(10 not in day for day in visits)
    assert "TRANSPORT_COST_INCOMPLETE" in plan["validation"]["warnings"]
    assert "REQUESTED_ACTIVITY_UNFULFILLED" not in plan["validation"]["warnings"]
    assert len(router.calls) <= 12


def test_existing_unknown_overnight_does_not_mask_new_unavailable_legs():
    from app.routing import unknown_route

    draft, catalog, settings, _ = inputs()
    activity_coordinates = {coord(cid)["longitude"] for cid in (10, 11, 12)}

    class NoActivityRoutes(NearbyRouter):
        def route(self, origin, destination, mode):
            if {origin["longitude"], destination["longitude"]} & activity_coordinates:
                return unknown_route(mode, "ROUTE_UNAVAILABLE")
            return super().route(origin, destination, mode)

    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 1, "budget": 10000},
                                [], settings, NoActivityRoutes())
    assert "OVERNIGHT_LOCATION_UNRESOLVED" in plan["validation"]["warnings"]
    assert "ROUTE_UNAVAILABLE" not in plan["validation"]["warnings"]
    assert "REQUESTED_ACTIVITY_UNFULFILLED" in plan["validation"]["warnings"]


def test_activity_count_shortfall_counts_only_visits_and_stays_advisory():
    from app.route_preferences import missing_visit_warnings

    draft, catalog, settings, _ = inputs()
    rule = {**settings["_routePreferences"]["countRules"][0], "count": 3, "operator": "target"}
    visits = [{"kind": "PLACE", "contentid": cid} for cid in (1, 10, 2)]
    plan = {"days": [{"date": DAY1, "nodes": visits}]}
    assert missing_visit_warnings(plan, {"countRules": [rule]}, catalog) == {"REQUESTED_ACTIVITY_UNFULFILLED"}
    plan["days"][0]["nodes"] += [{"kind": "PLACE", "contentid": cid} for cid in (11, 12)]
    assert not missing_visit_warnings(plan, {"countRules": [rule]}, catalog)


def test_only_confirmed_anchors_can_reuse_a_venue_across_dates():
    from app.itinerary_optimizer import _valid_candidate

    plan = {"days": [{"date": day, "places": [{"contentid": 7}]} for day in (DAY1, DAY2)]}
    pool = {7: {"planningRole": "overnight_lodging", "allowedDates": [DAY1, DAY2]}}
    assert _valid_candidate(plan, 2, pool, 8, {7}, anchors={(DAY1, 7), (DAY2, 7)})
    assert not _valid_candidate(plan, 2, pool, 8, {7}, anchors={(DAY1, 7)})


def test_wholly_unfulfilled_activity_is_reported_as_warning_not_hard_violation():
    draft, catalog, settings, pool = inputs()
    for row in pool:
        if row["planningRole"] == "sightseeing_stop":
            row["allowedDates"] = []
    report = {}
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, NearbyRouter(), optimization_report=report)
    assert "REQUESTED_ACTIVITY_UNFULFILLED" in plan["validation"]["warnings"]
    assert not any(item["code"] == "REQUIRED_PREFERENCE_UNSATISFIED"
                   for item in plan["validation"]["violations"])


def test_specific_activity_and_food_shortfalls_warn_even_when_generic_sightseeing_is_present():
    draft, catalog, settings, pool = inputs()
    for row in pool:
        if row["contentid"] == 12:
            row["allowedDates"] = []
    settings["_routePreferences"]["countRules"].extend([
        {"dimension": "catalog_activity", "contentids": [12], "scope": "trip", "operator": "min",
         "count": 1, "required": False, "polarity": "prefer"},
        {"dimension": "catalog_food", "contentids": [99], "scope": "trip", "operator": "min",
         "count": 1, "required": False, "polarity": "prefer"},
    ])
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, NearbyRouter())
    assert any(node["contentid"] in {10, 11} for day in plan["days"] for node in day["nodes"]
               if node["kind"] == "PLACE")
    assert "REQUESTED_ACTIVITY_UNFULFILLED" in plan["validation"]["warnings"]
    assert "REQUESTED_FOOD_UNFULFILLED" in plan["validation"]["warnings"]


def test_scheduler_applies_only_explicit_named_meal_period_waits():
    catalog = SyntheticCatalog()
    for detail in catalog.details:
        if detail["contentid"] in {1, 2, 3}:
            detail["openingHoursText"] = "06:00~22:00"
    pool = [{"contentid": cid, "planningRole": "meal_stop", "allowedDates": [DAY1],
             "estimatedStayMinutes": 45, "coordinate": coord(cid), "selectionRank": cid}
            for cid in (1, 2, 3)]
    settings = {"activityWindow": {"start": "07:00", "end": "20:00"}, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [],
                "departureAnchor": {"label": "start", "coordinate": coord(0)},
                "returnAnchor": {"label": "return", "coordinate": coord(20)},
                "_routePreferences": {"countRules": [{"dimension": "meals", "scope": "day",
                    "operator": "exact", "count": 3, "required": False, "polarity": "prefer",
                    "mealPeriods": ["breakfast", "lunch", "dinner"]}],
                    "walkingRules": [], "issues": [], "softWarnings": []}}
    draft = {"days": [{"date": DAY1,
                       "places": [catalog.presentation_place(cid) for cid in (1, 2, 3)]}],
             "optimizationCandidates": pool}
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, NearbyRouter())
    meals = [node for node in plan["days"][0]["nodes"]
             if node["kind"] == "PLACE" and node["contentid"] in {1, 2, 3}]
    starts = [datetime.fromisoformat(node["plannedStartAt"].replace("Z", "+00:00"))
              .astimezone(timezone(timedelta(hours=9))).strftime("%H:%M") for node in meals]
    assert starts == ["07:03", "11:30", "17:30"]
    assert "MEAL_SLOT_UNFILLED" not in plan["validation"]["warnings"]


def test_full_day_can_replace_only_a_surplus_unanchored_meal_with_requested_tourism():
    from copy import deepcopy
    from types import SimpleNamespace
    from app.itinerary_optimizer import optimize_itinerary

    ids = list(range(1, 9))
    pool = [{"contentid": cid, "planningRole": "meal_stop", "allowedDates": [DAY1],
             "coordinate": coord(cid), "selectionRank": cid, "categoryKey": f"meal:{cid}"} for cid in ids]
    pool.append({"contentid": 90, "planningRole": "sightseeing_stop", "allowedDates": [DAY1],
                 "coordinate": coord(5), "selectionRank": 0, "categoryKey": "sightseeing"})
    catalog = SimpleNamespace(places=[{"contentid": row["contentid"], "planningRole": row["planningRole"]}
                                       for row in pool], preferences=[])
    preferences = {"countRules": [
        {"dimension": "meals", "contentids": ids, "scope": "day", "operator": "min", "count": 3,
         "required": False, "polarity": "prefer"},
        {"dimension": "sightseeing", "contentids": [90], "scope": "day", "operator": "min", "count": 1,
         "required": False, "polarity": "prefer"},
    ], "walkingRules": []}
    initial = {"days": [{"date": DAY1, "places": [{"contentid": cid} for cid in ids]}]}

    def evaluate(candidate):
        return deepcopy(candidate), {"days": [{"edges": [{"status": "verified", "durationSeconds": 100}]}],
                                    "validation": {"warnings": [], "violations": []}}, {}

    result, _plan, _settings, report = optimize_itinerary(
        initial, {}, pool, evaluate, route_preferences=preferences, catalog=catalog)
    chosen = [place["contentid"] for place in result["days"][0]["places"]]
    assert len(chosen) == 8
    assert 90 in chosen and sum(cid in ids for cid in chosen) == 7
    assert report["acceptedMovesByPhase"]["composition_insert"] == 1


def test_named_meal_waits_fall_back_when_they_push_a_fixed_appointment_out():
    catalog = SyntheticCatalog()
    for detail in catalog.details:
        detail["openingHoursText"] = "06:00~22:00"
    pool = [{"contentid": cid, "planningRole": "meal_stop", "allowedDates": [DAY1],
             "estimatedStayMinutes": 45, "coordinate": coord(cid), "selectionRank": cid}
            for cid in (1, 2, 3)]
    pool.append({"contentid": 10, "planningRole": "sightseeing_stop", "allowedDates": [DAY1],
                 "estimatedStayMinutes": 30, "coordinate": coord(10), "selectionRank": 10})
    catalog.places.append({"contentid": 10, "title": "Synthetic appointment", "planningRole": "sightseeing_stop"})
    catalog.details.append({"contentid": 10, "estimatedStayMinutes": 30,
                            "openingHoursText": "06:00~22:00", "feeText": "무료"})
    settings = {"activityWindow": {"start": "07:00", "end": "19:00"}, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [{"kind": "appointment", "date": DAY1, "contentid": 10,
                                               "position": 3, "startTime": "17:00", "endTime": "17:30"}],
                "departureAnchor": {"label": "start", "coordinate": coord(0)},
                "returnAnchor": {"label": "return", "coordinate": coord(20)},
                "_routePreferences": {"countRules": [{"dimension": "meals", "scope": "day",
                    "operator": "exact", "count": 3, "required": False, "polarity": "prefer",
                    "mealPeriods": ["breakfast", "lunch", "dinner"]}],
                    "walkingRules": [], "issues": [], "softWarnings": []}}
    draft = {"days": [{"date": DAY1,
                       "places": [catalog.presentation_place(cid) for cid in (1, 2, 3, 10)]}],
             "optimizationCandidates": pool}
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, NearbyRouter())
    nodes = [node for node in plan["days"][0]["nodes"] if node["kind"] == "PLACE"]
    assert {node["contentid"] for node in nodes} == {1, 2, 3, 10}
    appointment = next(node for node in nodes if node["contentid"] == 10)
    assert appointment["plannedStartAt"].endswith("Z")
    assert not plan["validation"]["violations"]
