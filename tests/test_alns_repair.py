import copy

from app.alns_repair import SearchBudget, _repair_variants, repair_itinerary


DAY1, DAY2 = "2026-12-12", "2026-12-13"


def _plan(violations=(), warning=()):
    return {"days": [{"edges": [{"status": "verified", "durationSeconds": 10}]}],
            "validation": {"violations": [{"code": code} for code in violations], "warnings": list(warning)}}


def _place(cid, role="sightseeing_stop"):
    return {"contentid": cid, "planningRole": role, "coordinate": {"latitude": 36.5, "longitude": 127 + cid / 1000}}


def test_multi_removal_repair_can_replace_two_stops_jointly():
    source = {"days": [{"date": DAY1, "places": [_place(1), _place(2)]}]}
    pool = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY1],
             "selectionRank": rank, "coordinate": _place(cid)["coordinate"]}
            for cid, rank in ((1, 3), (2, 4), (3, 0), (4, 1))]
    victims = [{"date": DAY1, "index": i, "place": _place(cid), "role": "sightseeing_stop"}
               for i, cid in enumerate((1, 2))]
    variants = _repair_variants(source, victims, {p["contentid"]: p for p in pool}, set(), {}, 2, {DAY1: 0}, 8, None, "greedy")
    assert variants
    assert any({p["contentid"] for p in day["places"]} == {3, 4} for variant in variants for day in variant["days"])


def test_repair_can_reallocate_a_tourism_stop_to_an_allowed_day():
    source = {"days": [{"date": DAY1, "places": [_place(1)]}, {"date": DAY2, "places": []}]}
    pool = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY1, DAY2],
             "selectionRank": rank, "coordinate": _place(cid)["coordinate"]}
            for cid, rank in ((1, 2), (2, 0))]
    victim = [{"date": DAY1, "index": 0, "place": _place(1), "role": "sightseeing_stop"}]
    variants = _repair_variants(source, victim, {p["contentid"]: p for p in pool}, set(), {}, 1,
                                {DAY1: 0, DAY2: 0}, 8, None, "greedy")
    assert any(not d["places"] and [p["contentid"] for p in variant["days"][1]["places"]] == [2]
               for variant in variants for d in variant["days"][:1])


def test_adaptive_operator_weights_change_after_a_safe_repair():
    source = {"days": [{"date": DAY1, "places": [_place(1), _place(2)]}]}
    pool = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY1],
             "selectionRank": rank, "coordinate": _place(cid)["coordinate"]}
            for cid, rank in ((1, 3), (2, 4), (3, 0), (4, 1))]
    evaluated = []

    def evaluate(candidate):
        evaluated.append(candidate)
        return copy.deepcopy(candidate), _plan(), {}

    result, plan, _settings, report = repair_itinerary(
        source, {}, pool, evaluate, reference_plan=_plan(), max_evaluations=8, time_budget_seconds=1.0)
    assert result is not None and not plan["validation"]["violations"]
    assert report["attemptedMoves"] > 0
    assert any(value != 1.0 for value in report["destroyOperatorWeights"].values()) or any(
        value != 1.0 for value in report["repairOperatorWeights"].values())
    assert len(evaluated) <= 8


def test_shared_search_budget_is_consumed_once_across_calls():
    budget = SearchBudget(max_trials=2, seconds=1)
    assert budget.reserve()
    assert budget.reserve()
    assert not budget.reserve()
    assert budget.used_trials == 2 and budget.remaining == 0


def test_multi_removal_can_temporarily_remove_meal_then_restore_same_day():
    source = {"days": [{"date": DAY1, "places": [_place(1), _place(2, "meal_stop")]}]}
    pool = [{"contentid": cid, "planningRole": role, "allowedDates": [DAY1], "groupScore": score,
             "coordinate": _place(cid, role)["coordinate"]}
            for cid, role, score in ((1, "sightseeing_stop", .8), (3, "culture_stop", .7),
                                     (2, "meal_stop", .6), (4, "meal_stop", .9))]
    victims = [{"date": DAY1, "index": 0, "place": _place(1), "role": "sightseeing_stop"},
               {"date": DAY1, "index": 1, "place": _place(2, "meal_stop"), "role": "meal_stop"}]
    variants = _repair_variants(source, victims, {p["contentid"]: p for p in pool}, set(), {}, 2,
                                {DAY1: 1}, 8, None, "greedy")
    assert variants
    assert all(sum(p.get("planningRole") == "meal_stop" for p in day["places"]) == 1
               for variant in variants for day in variant["days"])


def test_repair_preserves_same_hotel_anchor_id_on_two_different_days():
    hotel = {"contentid": 9, "planningRole": "overnight_lodging"}
    source = {"days": [{"date": DAY1, "places": [_place(1), hotel.copy()]},
                        {"date": DAY2, "places": [hotel.copy()]}]}
    pool = [{"contentid": cid, "planningRole": "sightseeing_stop", "allowedDates": [DAY1, DAY2],
             "groupScore": score, "coordinate": _place(cid)["coordinate"]}
            for cid, score in ((1, .2), (2, 1.0))]
    victim = [{"date": DAY1, "index": 0, "place": _place(1), "role": "sightseeing_stop"}]
    anchors = {(DAY1, 9), (DAY2, 9)}
    variants = _repair_variants(source, victim, {p["contentid"]: p for p in pool}, anchors, {}, 3,
                                {DAY1: 0, DAY2: 0}, 8, None, "greedy")
    assert variants
    assert [sum(p["contentid"] == 9 for p in day["places"]) for day in variants[0]["days"]] == [1, 1]
