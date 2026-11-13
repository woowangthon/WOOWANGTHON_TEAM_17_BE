"""Category decisions through the real scheduler and its hard validator."""
import json
import copy

from app.day_schedule import build_scheduled_plan
from test_itinerary_optimizer import DAY, ScheduledCatalog, coordinate


class ConstantRouter:
    def __init__(self):
        self.calls = 0

    def route(self, origin, destination, mode):
        self.calls += 1
        return {"status": "verified", "mode": mode, "durationSeconds": 60,
                "distanceMeters": 100, "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": [], "geometry": [], "reasonCodes": []}


def generate(*, meals=False):
    catalog = ScheduledCatalog()
    if meals:
        for row in catalog.places:
            row["planningRole"] = "meal_stop" if row["contentid"] != 4 else "culture_stop"
    else:
        catalog.places[2]["planningRole"] = "culture_stop"
    pool = [{"contentid": cid, "selectionRank": cid - 1, "groupScore": 0,
             "allowedDates": [DAY], "planningRole": catalog.places[cid - 1]["planningRole"],
             "categoryKey": "tag:카페" if cid in {1, 2} else f"category:{cid}", "coordinate": coordinate(cid)}
            for cid in (1, 2, 3, 4)]
    draft = {"days": [{"date": DAY, "places": [catalog.presentation_place(cid) for cid in (1, 2)]}],
             "optimizationCandidates": pool}
    settings = {"activityWindow": {"start": "09:00", "end": "16:00" if meals else "11:00"},
                "reviewedCommonFields": [], "departureAnchor": {"label": "home", "coordinate": coordinate(0)},
                "returnAnchor": {"label": "home", "coordinate": coordinate(9)}}
    report, router = {}, ConstantRouter()
    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, router, optimization_report=report)
    return plan, report, router


def test_real_schedule_prefers_lower_base_rank_diversity_at_equal_travel_cost():
    plan, report, router = generate()
    assert [node["contentid"] for node in plan["days"][0]["nodes"] if node["kind"] == "PLACE"] == [1, 3]
    assert report["baselineVisitCount"] == report["finalVisitCount"] == 2
    assert report["baselineTravelSeconds"] == report["finalTravelSeconds"]
    assert report["acceptedMovesByPhase"]["candidate_replace"] >= 1
    assert not plan["validation"]["violations"]
    assert router.calls <= 9  # three baseline edges and no more than six new ones
    assert "categoryKey" not in json.dumps(plan) and "selectionRank" not in json.dumps(plan)


def test_cafe_diversity_replaces_meal_with_meal_and_keeps_meal_slots():
    plan, report, _router = generate(meals=True)
    assert [node["contentid"] for node in plan["days"][0]["nodes"] if node["kind"] == "PLACE"] == [1, 3]
    assert report["baselineVisitCount"] == report["finalVisitCount"] == 2
    assert "MEAL_SLOT_UNFILLED" not in plan["validation"]["warnings"]
    assert not plan["validation"]["violations"]


def test_utility_screen_does_not_block_lower_score_hard_violation_repair():
    from app.itinerary_optimizer import optimize_itinerary
    original = {"days": [{"date": DAY, "places": [{"contentid": 1}]}]}
    pool = [{"contentid": cid, "groupScore": score, "planningRole": "sightseeing_stop", "allowedDates": [DAY]}
            for cid, score in ((1, 1.), (2, .5))]
    def evaluate(candidate):
        cid = candidate["days"][0]["places"][0]["contentid"]
        return copy.deepcopy(candidate), {"days": [{"edges": [{"status": "verified", "durationSeconds": 60}]}],
                                          "validation": {"warnings": [], "violations": [{"code": "WALKING_LIMIT_EXCEEDED"}] if cid == 1 else []}}, {}
    result, plan, _settings, _report = optimize_itinerary(original, {}, pool, evaluate)
    assert result["days"][0]["places"][0]["contentid"] == 2
    assert not plan["validation"]["violations"]


def test_date_allocation_cannot_shift_meals_to_another_day_for_a_better_route():
    from app.itinerary_optimizer import optimize_itinerary
    second = "2026-12-13"
    original = {"days": [{"date": DAY, "places": [{"contentid": 1}, {"contentid": 2}]},
                         {"date": second, "places": [{"contentid": 3}, {"contentid": 4}]}]}
    pool = [{"contentid": cid, "planningRole": "meal_stop" if cid in {1, 3} else "sightseeing_stop",
             "allowedDates": [DAY, second], "groupScore": 0} for cid in (1, 2, 3, 4)]
    def evaluate(candidate):
        shifted = any(p["contentid"] == 1 for p in candidate["days"][1]["places"])
        plan = {"days": [{"edges": [{"status": "verified", "durationSeconds": 1 if shifted else 100}]}],
                "validation": {"warnings": ["MEAL_SLOT_UNFILLED"], "violations": []}}
        return copy.deepcopy(candidate), plan, {}
    result, _plan, _settings, _report = optimize_itinerary(original, {}, pool, evaluate)
    assert [p["contentid"] for p in result["days"][0]["places"] if p["contentid"] in {1, 3}] == [1]
    assert [p["contentid"] for p in result["days"][1]["places"] if p["contentid"] in {1, 3}] == [3]
