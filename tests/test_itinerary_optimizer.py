from __future__ import annotations

import copy

from app.itinerary_optimizer import optimize_itinerary


DAY = "2026-12-12"


def draft(ids):
    return {"days": [{"date": DAY, "places": [{"contentid": cid, "visitPosition": i} for i, cid in enumerate(ids)]}]}


def evaluator(matrix, *, warnings=(), violations=()):
    def evaluate(value):
        route = [0, *[p["contentid"] for p in value["days"][0]["places"]], 9]
        seconds = sum(matrix.get((a, b), 1000) for a, b in zip(route, route[1:]))
        edges = [{"status": "verified", "durationSeconds": seconds}]
        plan = {"days": [{"edges": edges}], "validation": {
            "warnings": list(warnings),
            "violations": [{"code": code} for code in violations],
        }}
        return copy.deepcopy(value), plan, {}
    return evaluate


def test_two_opt_improves_asymmetric_route_without_dropping_visits():
    original = draft([1, 2, 3])
    matrix = {
        (0, 1): 8, (1, 2): 8, (2, 3): 8, (3, 9): 8,
        (0, 3): 1, (3, 2): 1, (2, 1): 1, (1, 9): 1,
        (0, 2): 12, (1, 3): 12, (2, 9): 12, (3, 1): 12,
    }
    result, plan, _, report = optimize_itinerary(original, {}, [], evaluator(matrix))
    assert [p["contentid"] for p in result["days"][0]["places"]] == [3, 2, 1]
    assert report["baselineVisitCount"] == report["finalVisitCount"] == 3
    assert report["baselineTravelSeconds"] == 32
    assert report["finalTravelSeconds"] == 4
    assert report["acceptedMoves"] > 0
    assert plan["validation"]["violations"] == []
    assert [p["contentid"] for p in original["days"][0]["places"]] == [1, 2, 3]


def test_moves_that_introduce_unknown_route_facts_fall_back_to_baseline():
    original = draft([1, 2, 3])

    def evaluate(value):
        ids = [p["contentid"] for p in value["days"][0]["places"]]
        plan = {"days": [{"edges": [{"status": "verified", "durationSeconds": 30}]}],
                "validation": {"warnings": [] if ids == [1, 2, 3] else ["ROUTE_UNKNOWN"], "violations": []}}
        return copy.deepcopy(value), plan, {}

    result, _, _, report = optimize_itinerary(original, {}, [], evaluate)
    assert [p["contentid"] for p in result["days"][0]["places"]] == [1, 2, 3]
    assert report["acceptedMoves"] == 0
    assert report["fallback"] == "NO_SAFE_IMPROVEMENT"


def test_anchored_visit_stays_at_its_locked_day_and_position():
    original = draft([1, 2])
    settings = {"confirmedScheduleAnchors": [{"date": DAY, "contentid": 1, "position": 0}]}
    matrix = {(0, 2): 1, (2, 1): 1, (1, 9): 1, (0, 1): 10, (1, 2): 10, (2, 9): 10}
    result, _, _, report = optimize_itinerary(original, settings, [], evaluator(matrix))
    ids = [p["contentid"] for p in result["days"][0]["places"]]
    assert ids[0] == 1
    assert report["acceptedMoves"] == 0


def test_candidate_pool_replacement_keeps_dates_and_requires_better_score():
    original = draft([1, 2])
    pool = [
        {"contentid": 1, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": 0.2},
        {"contentid": 2, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": 0.1},
        {"contentid": 3, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": 0.9},
        {"contentid": 4, "planningRole": "meal_stop", "allowedDates": [DAY], "groupScore": 1.0},
    ]
    route = {(0, 1): 5, (1, 2): 5, (2, 9): 5, (0, 3): 4, (3, 2): 4}
    result, _, _, report = optimize_itinerary(original, {}, pool, evaluator(route))
    ids = [p["contentid"] for p in result["days"][0]["places"]]
    assert 3 in ids and 4 not in ids
    assert report["baselineVisitCount"] == report["finalVisitCount"] == 2
    assert report["finalPreferenceScore"] > report["baselinePreferenceScore"]


def test_candidate_replacement_filters_eligible_rows_before_capping():
    original = draft([1])
    pool = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY],
             "groupScore": 100.0 if cid == 25 else 0.1}
            for cid in range(1, 26)]

    def evaluate(candidate):
        plan = {"days": [{"edges": [{"status": "verified", "durationSeconds": 60}]}],
                "validation": {"warnings": [], "violations": []}}
        return copy.deepcopy(candidate), plan, {}

    result, _plan, _settings, report = optimize_itinerary(original, {}, pool, evaluate)
    assert result["days"][0]["places"][0]["contentid"] == 25
    assert report["acceptedMovesByPhase"]["candidate_replace"] >= 1


def test_unknown_route_cost_is_not_imputed_as_zero():
    value = draft([1])
    def evaluate(candidate):
        plan = {"days": [{"edges": [{"status": "unknown", "durationSeconds": None}]}],
                "validation": {"warnings": ["ROUTE_UNAVAILABLE"], "violations": []}}
        return copy.deepcopy(candidate), plan, {}
    _, _, _, report = optimize_itinerary(value, {}, [], evaluate)
    assert report["baselineTravelSeconds"] is None
    assert report["finalTravelSeconds"] is None


def test_production_candidate_rank_takes_precedence_over_higher_group_mean():
    original = draft([1])
    pool = [
        {"contentid": 1, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": .8, "selectionRank": 1},
        {"contentid": 2, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": .4, "selectionRank": 0},
        {"contentid": 3, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "groupScore": 1., "selectionRank": 2},
    ]
    result, _, _, report = optimize_itinerary(original, {}, pool, evaluator({}))
    assert [p["contentid"] for p in result["days"][0]["places"]] == [2]
    assert report["acceptedMovesByPhase"] == {"candidate_replace": 1}


def test_date_reallocation_respects_allowed_dates_and_preserves_visit_count():
    second = "2026-12-13"
    original = {"days": [{"date": DAY, "places": [{"contentid": 1}, {"contentid": 2}]},
                         {"date": second, "places": [{"contentid": 3}]}]}
    pool = [{"contentid": n, "allowedDates": [DAY, second] if n == 2 else [DAY] if n == 1 else [second], "groupScore": 0}
            for n in (1, 2, 3)]
    def evaluate(value):
        ids = {d["date"]: [p["contentid"] for p in d["places"]] for d in value["days"]}
        cost = 10 if ids[DAY] == [1] and ids[second] == [3, 2] else 100
        return copy.deepcopy(value), {"days": [{"edges": [{"status": "verified", "durationSeconds": cost}]}],
                                      "validation": {"warnings": [], "violations": []}}, {}
    result, _, _, report = optimize_itinerary(original, {}, pool, evaluate)
    assert [p["contentid"] for p in result["days"][1]["places"]] == [3, 2]
    assert report["acceptedMovesByPhase"]["date_reallocation"] == 1
    assert report["baselineVisitCount"] == report["finalVisitCount"] == 3


class ScheduledCatalog:
    def __init__(self, fee=None):
        self.places = [{"contentid": n, "title": f"합성 {n}",
                        "planningRole": "overnight_lodging" if n == 5 else "sightseeing_stop"}
                       for n in (1, 2, 3, 4, 5)]
        self.details = [{"contentid": n, "estimatedStayMinutes": 10, "openingHoursText": "09:00~18:00",
                         "feeText": (fee or {}).get(n, "무료")} for n in (1, 2, 3, 4)]

    def presentation_place(self, cid):
        return {"contentid": cid, "title": f"합성 {cid}", "coordinate": coordinate(cid)}


def coordinate(cid):
    return {"latitude": 36.5, "longitude": 127 + cid / 1000, "crs": "WGS84"}


class DirectedRouter:
    def __init__(self, matrix, unknown=False):
        self.matrix, self.unknown, self.calls = matrix, unknown, []

    def route(self, origin, destination, mode):
        a, b = (round((v["longitude"] - 127) * 1000) for v in (origin, destination))
        self.calls.append((a, b))
        if self.unknown and (a, b) not in {(0, 1), (1, 2), (2, 3), (3, 9)}:
            return {"status": "unknown", "mode": mode, "reasonCodes": ["SYNTHETIC_UNKNOWN"]}
        return {"status": "verified", "mode": mode, "durationSeconds": self.matrix.get((a, b), 3600),
                "distanceMeters": 100, "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": [], "geometry": [], "reasonCodes": []}


def scheduled_inputs(catalog=None):
    catalog = catalog or ScheduledCatalog()
    value = {"days": [{"date": DAY, "places": [catalog.presentation_place(cid) for cid in (1, 2, 3)]}]}
    settings = {"activityWindow": {"start": "09:00", "end": "11:00"}, "reviewedCommonFields": [],
                "departureAnchor": {"label": "시작", "coordinate": coordinate(0)},
                "returnAnchor": {"label": "복귀", "coordinate": coordinate(9)}}
    matrix = {(0, 1): 480, (1, 2): 480, (2, 3): 480, (3, 9): 480,
              (0, 3): 60, (3, 2): 60, (2, 1): 60, (1, 9): 60}
    return value, catalog, settings, matrix


def test_full_scheduler_optimizes_directed_route_and_respects_call_budget():
    from app.day_schedule import build_scheduled_plan
    value, catalog, settings, matrix = scheduled_inputs()
    router, report = DirectedRouter(matrix), {}
    plan = build_scheduled_plan(value, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, router, optimization_report=report)
    visits = [n["contentid"] for n in plan["days"][0]["nodes"] if n["kind"] == "PLACE"]
    assert visits == [3, 2, 1]
    assert not plan["validation"]["violations"]
    assert report["baselineTravelSeconds"] == 1920 and report["finalTravelSeconds"] == 240
    assert len(router.calls) <= 10  # four cached baseline edges plus at most six new routes
    assert "optimizationCandidates" not in plan and "acceptedMoves" not in plan


def test_full_scheduler_keeps_baseline_when_new_routes_are_unknown():
    from app.day_schedule import build_scheduled_plan
    value, catalog, settings, matrix = scheduled_inputs()
    report = {}
    plan = build_scheduled_plan(value, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, DirectedRouter(matrix, unknown=True), optimization_report=report)
    assert [n["contentid"] for n in plan["days"][0]["nodes"] if n["kind"] == "PLACE"] == [1, 2, 3]
    assert plan["validation"]["route"]["status"] == "verified"
    assert report["acceptedMoves"] == 0


def test_full_scheduler_cannot_move_a_fixed_booking_or_exceed_known_budget():
    from app.day_schedule import build_scheduled_plan
    value, catalog, settings, matrix = scheduled_inputs(ScheduledCatalog({4: "전체 1000000원"}))
    settings["confirmedScheduleAnchors"] = [{"date": DAY, "contentid": 1, "position": 0, "startTime": "09:10", "kind": "appointment"}]
    value["optimizationCandidates"] = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY],
                                         "groupScore": 1 if cid == 4 else 0, "coordinate": coordinate(cid)} for cid in (1, 2, 3, 4)]
    # Replacements are reachable with known times but unaffordable.
    matrix.update({(1, 4): 60, (4, 3): 60, (2, 4): 60, (4, 9): 60})
    plan = build_scheduled_plan(value, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, DirectedRouter(matrix))
    nodes = [n for n in plan["days"][0]["nodes"] if n["kind"] == "PLACE"]
    assert nodes[0]["contentid"] == 1 and 4 not in [n["contentid"] for n in nodes]
    assert not plan["validation"]["violations"]


def test_default_budget_does_not_starve_selection_behind_five_stop_permutations():
    original = draft([1, 2, 3, 4, 5])
    pool = [{"contentid": n, "planningRole": "sightseeing_stop", "allowedDates": [DAY],
             "groupScore": 1 if n == 6 else 0} for n in range(1, 7)]
    def constant_safe(candidate):
        return copy.deepcopy(candidate), {"days": [{"edges": [{"status": "verified", "durationSeconds": 10}]}],
                                          "validation": {"warnings": [], "violations": []}}, {}
    result, _, _, report = optimize_itinerary(original, {}, pool, constant_safe)
    assert 6 in [p["contentid"] for p in result["days"][0]["places"]]
    assert report["attemptedMoves"] <= 24
    assert report["acceptedMovesByPhase"]["candidate_replace"] >= 1


def test_same_day_category_repeats_have_diminishing_positive_utility():
    from app.itinerary_optimizer import _preference_score

    pool = {cid: {"selectionRank": rank, "categoryKey": category, "planningRole": "sightseeing_stop"}
            for cid, rank, category in ((1, 0, "type:1"), (2, 1, "type:1"), (3, 2, "type:2"))}
    repeated = draft([1, 2])
    diverse = draft([1, 3])
    assert _preference_score(repeated, pool) == .75 + .5 / 2
    assert _preference_score(diverse, pool) == .75 + .25


def test_category_discount_is_per_day_unknown_is_not_a_shared_category():
    from app.itinerary_optimizer import _preference_score

    pool = {1: {"selectionRank": 0, "categoryKey": "x"},
            2: {"selectionRank": 1, "categoryKey": "x"}}
    across_days = {"days": [{"date": DAY, "places": [{"contentid": 1}]},
                          {"date": "2026-12-13", "places": [{"contentid": 2}]}]}
    assert _preference_score(across_days, pool) == 2 / 3 + 1 / 3
    unknown = {"days": [{"date": DAY, "places": [{"contentid": 1}, {"contentid": 2}]}]}
    assert _preference_score(unknown, {1: {"selectionRank": 0}, 2: {"selectionRank": 1}}) == 2 / 3 + 1 / 3


def test_repeated_negative_avoid_score_does_not_gain_from_discount():
    from app.itinerary_optimizer import _preference_score

    pool = {1: {"groupScore": -.5, "categoryKey": "type:1"},
            2: {"groupScore": -.25, "categoryKey": "type:1"}}
    assert _preference_score(draft([1, 2]), pool) == -.75


def test_slightly_lower_rank_different_category_can_win_after_repeat_discount():
    original = draft([1, 2])
    pool = [
        {"contentid": 1, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "selectionRank": 0, "categoryKey": "type:a"},
        {"contentid": 2, "planningRole": "sightseeing_stop", "allowedDates": [DAY], "selectionRank": 1, "categoryKey": "type:a"},
        {"contentid": 3, "planningRole": "culture_stop", "allowedDates": [DAY], "selectionRank": 2, "categoryKey": "type:b"},
        {"contentid": 4, "planningRole": "activity_stop", "allowedDates": [DAY], "selectionRank": 3, "categoryKey": "type:c"},
    ]
    result, _, _, report = optimize_itinerary(original, {}, pool, evaluator({}))
    ids = [p["contentid"] for p in result["days"][0]["places"]]
    assert ids == [1, 3]
    assert report["finalAdjustedUtility"] > report["baselineAdjustedUtility"]
    assert report["finalCategoryRepetitions"] == 0


def test_candidate_pool_role_anchors_protect_meals_appointments_and_lodging():
    original = draft([1, 2, 3, 4])
    original["days"][0]["places"][2]["planningRole"] = "meal_stop"
    original["days"][0]["places"][3]["planningRole"] = "overnight_lodging"
    settings = {"confirmedScheduleAnchors": [{"date": DAY, "contentid": 1, "position": 0, "kind": "appointment"},
                                               {"date": DAY, "contentid": 3, "position": 2, "kind": "meal"},
                                               {"date": DAY, "contentid": 4, "position": 3, "kind": "lodging"}]}
    pool = [{"contentid": n, "planningRole": role, "allowedDates": [DAY], "groupScore": score,
             "categoryKey": f"type:{n}"}
            for n, role, score in [(1, "sightseeing_stop", 0), (2, "sightseeing_stop", 0),
                                   (3, "meal_stop", 0), (4, "overnight_lodging", 0),
                                   (5, "activity_stop", 100), (6, "meal_stop", 100)]]
    result, _, _, _ = optimize_itinerary(original, settings, pool, evaluator({}))
    ids = [p["contentid"] for p in result["days"][0]["places"]]
    assert ids[0] == 1
    assert ids[2:] == [3, 4]
    assert 6 not in ids


def test_two_day_reallocation_keeps_hotel_last_and_preserves_checkout_anchor():
    from app.day_schedule import build_scheduled_plan

    second = "2026-12-13"
    catalog = ScheduledCatalog()
    value = {"days": [
        {"date": DAY, "places": [catalog.presentation_place(n) for n in (1, 2, 5)]},
        {"date": second, "places": [catalog.presentation_place(3)]},
    ], "optimizationCandidates": [
        {"contentid": n, "planningRole": "overnight_lodging" if n == 5 else "sightseeing_stop",
         "allowedDates": [DAY, second] if n == 2 else [DAY] if n in {1, 5} else [second], "groupScore": 0}
        for n in (1, 2, 3, 5)
    ]}
    hotel_anchor = {"date": DAY, "contentid": 5, "position": 2, "kind": "lodging",
                    "startTime": "10:00", "endDate": second, "checkoutTime": "09:00"}
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "출발", "coordinate": coordinate(0)},
                "returnAnchor": {"label": "복귀", "coordinate": coordinate(9)},
                "reviewedCommonFields": [], "confirmedScheduleAnchors": [hotel_anchor]}
    matrix = {
        (0, 1): 60, (1, 2): 1200, (2, 5): 1200, (5, 3): 60, (3, 9): 60,
        (1, 5): 60, (3, 2): 60, (2, 9): 60,
    }
    router, report = DirectedRouter(matrix), {}
    plan = build_scheduled_plan(value, catalog, {"transport": "car", "expectedPeople": 2, "budget": 100000},
                                [], settings, router, optimization_report=report)
    by_date = {day["date"]: [node for node in day["nodes"] if node["kind"] == "PLACE"]
               for day in plan["days"]}
    assert [node["contentid"] for node in by_date[DAY]] == [1, 5]
    assert [node["contentid"] for node in by_date[second]] == [3, 2]
    assert by_date[DAY][-1]["contentid"] == hotel_anchor["contentid"]
    assert by_date[DAY][-1]["plannedStartAt"] == "2026-12-12T01:00:00.000Z"
    assert by_date[DAY][-1]["estimatedEndAt"] == "2026-12-13T00:00:00.000Z"
    assert report["acceptedMovesByPhase"]["date_reallocation"] == 1
    assert report["baselineTravelSeconds"] == 2580
    assert report["finalTravelSeconds"] == 300
    assert not plan["validation"]["violations"]
