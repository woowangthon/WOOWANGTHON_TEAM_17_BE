from datetime import datetime, timedelta

from app.meal_preferences import (
    meal_deviation_minutes,
    named_meal_earliest_starts,
    unserved_named_meal_periods,
)


DAY = "2026-12-12"
PERIODS = ["breakfast", "lunch", "dinner"]


def _stamp(hour, minute=0):
    return f"{DAY}T{hour:02d}:{minute:02d}:00+09:00"


def _day(meals, *, start="06:00", end="21:00"):
    nodes = [{"kind": "START", "estimatedStartAt": f"{DAY}T{start}:00+09:00"}]
    for cid, value in meals:
        node = {"kind": "PLACE", "contentid": cid}
        if value is not None:
            node["plannedStartAt"] = value
            node["plannedEndAt"] = (datetime.fromisoformat(value) + timedelta(minutes=60)).isoformat()
        nodes.append(node)
    return {"date": DAY, "nodes": nodes, "edges": []}


def _pool(*ids):
    return {cid: {"planningRole": "meal_stop"} for cid in ids}


def test_named_targets_include_breakfast_and_skip_fixed_actual_lunch():
    day = _day([(1, None), (2, _stamp(12, 15)), (3, None)])
    targets = named_meal_earliest_starts(day, _pool(1, 2, 3), PERIODS, {(DAY, 2)})
    assert targets == {1: datetime.fromisoformat(_stamp(7)),
                       3: datetime.fromisoformat(_stamp(17, 30))}


def test_late_start_omits_inapplicable_breakfast_and_respects_activity_end():
    day = _day([(1, None), (2, None)], start="12:00", end="19:00")
    targets = named_meal_earliest_starts(
        day, _pool(1, 2), PERIODS, activity_window={"start": "12:00", "end": "19:00"})
    assert targets == {1: datetime.fromisoformat(_stamp(12)),
                       2: datetime.fromisoformat(_stamp(17, 30))}


def test_configured_activity_start_wins_when_start_node_is_earlier():
    day = _day([(1, None)], start="06:00")
    targets = named_meal_earliest_starts(
        day, _pool(1), PERIODS, activity_window={"start": "12:00", "end": "20:00"})
    assert targets == {1: datetime.fromisoformat(_stamp(12))}


def test_fixed_meal_outside_named_window_does_not_occupy_a_period():
    day = _day([(1, _stamp(15)), (2, None)])
    targets = named_meal_earliest_starts(day, _pool(1, 2), PERIODS, {(DAY, 1)})
    assert targets == {2: datetime.fromisoformat(_stamp(7))}


def test_named_scoring_uses_named_breakfast_but_legacy_three_meals_stays_lunch_dinner():
    day = _day([(1, _stamp(8))])
    plan = {"days": [day]}
    assert meal_deviation_minutes(plan, _pool(1), meal_periods=PERIODS) == 0
    # With no explicit named-period contract, the established behavior remains lunch/dinner.
    assert meal_deviation_minutes(plan, _pool(1)) == 210


def test_named_scoring_does_not_reassign_fixed_anchor_and_empty_day_misses_applicable_periods():
    day = _day([(1, _stamp(12, 15)), (2, _stamp(17, 30)), (3, _stamp(8))])
    pool = _pool(1, 2, 3)
    assert meal_deviation_minutes({"days": [day]}, pool, {(DAY, 1)}, meal_periods=PERIODS) == 0
    assert unserved_named_meal_periods(_day([]), {}, PERIODS) == PERIODS


def test_unserved_named_periods_only_include_applicable_windows():
    day = _day([(1, _stamp(12))], start="12:00", end="16:00")
    assert unserved_named_meal_periods(day, _pool(1), PERIODS,
                                       activity_window={"start": "12:00", "end": "16:00"}) == []
