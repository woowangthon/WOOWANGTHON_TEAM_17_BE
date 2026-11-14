from datetime import datetime, timedelta

from app.meal_preferences import meal_deviation_minutes
from app.itinerary_optimizer import _rank, optimize_itinerary


DAY = "2026-12-12"


def plan_at(meal_start, day_start="2026-12-12T00:00:00.000Z"):
    meal_end = None
    if meal_start is not None:
        meal_end = (datetime.fromisoformat(meal_start.replace("Z", "+00:00")) + timedelta(minutes=60)).isoformat().replace("+00:00", "Z")
    return {"days": [{"date": DAY, "nodes": [
        {"kind": "START", "estimatedStartAt": day_start},
        {"kind": "PLACE", "contentid": 1, "plannedStartAt": meal_start, "plannedEndAt": meal_end},
        {"kind": "END", "estimatedEndAt": "2026-12-12T11:00:00.000Z"},
    ], "edges": [{"status": "verified", "durationSeconds": 600}]}],
        "validation": {"violations": [], "warnings": []}}


POOL = {1: {"planningRole": "meal_stop", "groupScore": 0.5}}


def test_deviation_uses_kst_clock_and_meal_times_are_soft_tie_breakers():
    in_band = plan_at("2026-12-12T03:30:00.000Z")  # 12:30 KST
    late = plan_at("2026-12-12T04:30:00.000Z")  # 13:30 KST
    assert meal_deviation_minutes(in_band, POOL) == 0
    assert meal_deviation_minutes(late, POOL) == 0
    outside_lunch = plan_at("2026-12-12T06:00:00.000Z")  # 15:00 KST
    assert meal_deviation_minutes(outside_lunch, POOL) == 90
    assert _rank({"days": []}, in_band, POOL) < _rank({"days": []}, outside_lunch, POOL)
    # Departure after lunch makes the only applicable meal the dinner band.
    afternoon = plan_at("2026-12-12T06:00:00.000Z", "2026-12-12T06:00:00.000Z")  # 15:00 KST
    assert meal_deviation_minutes(afternoon, POOL) == 150


def test_unknown_meal_clock_is_not_scored_as_an_improvement():
    unknown = plan_at(None)
    known = plan_at("2026-12-12T03:30:00.000Z")
    assert meal_deviation_minutes(unknown, POOL) is None
    assert _rank({"days": []}, unknown, POOL)[2] > _rank({"days": []}, known, POOL)[2]


def test_explicit_meal_anchor_is_exempt_from_soft_score():
    plan = plan_at("2026-12-12T08:00:00.000Z")
    assert meal_deviation_minutes(plan, POOL, {(DAY, 1)}) == 0


def test_early_dinner_keeps_dinner_band_even_when_route_ends_early():
    plan = plan_at("2026-12-12T05:36:00.000Z")  # 14:36 KST
    plan["days"][0]["nodes"].insert(2, {
        "kind": "PLACE", "contentid": 2,
        "plannedStartAt": "2026-12-12T05:36:00.000Z",
        "plannedEndAt": "2026-12-12T06:36:00.000Z",
    })
    plan["days"][0]["nodes"][-1]["estimatedEndAt"] = "2026-12-12T06:49:00.000Z"
    pool = {**POOL, 2: {"planningRole": "meal_stop", "groupScore": 0.5}}
    assert meal_deviation_minutes(plan, pool) == 240


def test_all_anchored_meals_do_not_need_a_scoring_clock():
    plan = plan_at(None, day_start=None)
    assert meal_deviation_minutes(plan, POOL, {(DAY, 1)}) == 0


def test_anchored_lunch_still_assigns_next_automatic_meal_to_dinner_band():
    plan = plan_at("2026-12-12T05:36:00.000Z")
    plan["days"][0]["nodes"].insert(2, {
        "kind": "PLACE", "contentid": 2,
        "plannedStartAt": "2026-12-12T05:36:00.000Z",
        "plannedEndAt": "2026-12-12T06:36:00.000Z",
    })
    pool = {**POOL, 2: {"planningRole": "meal_stop", "groupScore": 0.5}}
    # Meal 1 is explicitly timed and exempt; the next meal is 14:36, 174 min
    # before the dinner recommendation.
    assert meal_deviation_minutes(plan, pool, {(DAY, 1)}) == 174


def test_meal_end_after_recommendation_is_counted_and_unknown_end_is_unknown():
    plan = plan_at("2026-12-12T05:00:00.000Z")  # 14:00 KST
    plan["days"][0]["nodes"][1]["plannedEndAt"] = "2026-12-12T05:45:00.000Z"  # 14:45 KST
    assert meal_deviation_minutes(plan, POOL) == 15
    plan["days"][0]["nodes"][1]["plannedEndAt"] = None
    assert meal_deviation_minutes(plan, POOL) is None


def test_second_day_uses_internal_activity_window_without_requiring_start_node():
    plan = {"days": [{"date": DAY, "nodes": [
        {"kind": "START", "estimatedStartAt": "2026-12-12T00:00:00.000Z"},
        {"kind": "PLACE", "contentid": 9, "plannedStartAt": "2026-12-12T00:00:00.000Z",
         "plannedEndAt": "2026-12-12T01:00:00.000Z"},
    ]}, {"date": "2026-12-13", "nodes": [
        {"kind": "PLACE", "contentid": 1, "plannedStartAt": "2026-12-13T06:00:00.000Z",
         "plannedEndAt": "2026-12-13T07:00:00.000Z"},
    ]}], "validation": {"violations": [], "warnings": []}}
    assert meal_deviation_minutes(plan, POOL, activity_window={"start": "15:00", "end": "20:00"}) == 150


def test_multiday_optimizer_uses_settings_to_move_meal_into_dinner_band():
    second = "2026-12-13"
    draft = {"days": [
        {"date": DAY, "places": [{"contentid": 9}]},
        {"date": second, "places": [{"contentid": 1}, {"contentid": 2}]},
    ]}
    settings = {"activityWindow": {"start": "15:00", "end": "20:00"}}
    pool = [
        {"contentid": 1, "planningRole": "meal_stop", "categoryKey": "food", "groupScore": 0.5,
         "allowedDates": [second]},
        {"contentid": 2, "planningRole": "sightseeing_stop", "categoryKey": "sight", "groupScore": 0.5,
         "allowedDates": [second]},
        {"contentid": 9, "planningRole": "sightseeing_stop", "categoryKey": "sight", "groupScore": 0.5,
         "allowedDates": [DAY]},
    ]

    def evaluate(candidate):
        days = []
        for index, section in enumerate(candidate["days"]):
            nodes = []
            if index == 0:
                nodes.append({"kind": "START", "estimatedStartAt": "2026-12-12T06:00:00.000Z"})
            cursor = datetime.fromisoformat(f"{section['date']}T06:00:00+00:00")
            if index == 0:
                cursor += timedelta(minutes=10)
            for place in section["places"]:
                start = cursor
                duration = timedelta(minutes=150) if place["contentid"] == 2 else timedelta(minutes=60)
                nodes.append({"kind": "PLACE", "contentid": place["contentid"],
                              "plannedStartAt": start.isoformat().replace("+00:00", "Z"),
                              "plannedEndAt": (start + duration).isoformat().replace("+00:00", "Z")})
                cursor += duration
            days.append({"date": section["date"], "nodes": nodes,
                         "edges": [{"status": "verified", "durationSeconds": 3600}]})
        plan = {"days": days, "validation": {"violations": [], "warnings": []}}
        return candidate, plan, settings

    reversed_draft = {"days": [draft["days"][0], {
        "date": second, "places": list(reversed(draft["days"][1]["places"]))}]}
    pool_by_id = {row["contentid"]: row for row in pool}
    assert _rank(draft, evaluate(draft)[1], pool_by_id, activity_window=settings["activityWindow"]) > \
        _rank(reversed_draft, evaluate(reversed_draft)[1], pool_by_id,
              activity_window=settings["activityWindow"])
    assert meal_deviation_minutes(evaluate(draft)[1], pool_by_id,
                                  activity_window=settings["activityWindow"]) == 150
    assert meal_deviation_minutes(evaluate(reversed_draft)[1], pool_by_id,
                                  activity_window=settings["activityWindow"]) == 0
    result, _plan, _settings, report = optimize_itinerary(
        draft, settings, pool, evaluate, max_evaluations=8, time_budget_seconds=1.0)
    assert [p["contentid"] for p in result["days"][1]["places"]] == [2, 1]
    assert report["acceptedMoves"] > 0
