"""Synthetic compiler coverage for runtime atomic preference targets."""
from app.route_preferences import compile_route_preferences, route_preference_penalty
from test_route_preferences import _atom, _catalog
import pytest


def _compile(atom, catalog=None):
    return compile_route_preferences({
        "atomicMemberRefs": ["p1"],
        "groupConditions": {"memberScopedConstraints": [{"memberRef": "p1", **atom}]},
    }, catalog or _catalog())


@pytest.mark.parametrize("unit", ["\uacf3", "\uad70\ub370"])
def test_visit_count_unit_target_maps_only_with_a_valid_count_period(unit):
    atom = _atom(dimension="visit_count", target=unit, polarity="prefer", priority="preferred",
                 scope="day", approximate=True,
                 frequency={"operator": "target", "count": 3, "period": "day"})
    compiled = _compile(atom)
    assert compiled["policyVersion"] == "atomic-route-v2"
    assert compiled["countRules"] == [{
        "memberRef": "p1", "dimension": "sightseeing", "tag": None, "scope": "day",
        "operator": "target", "count": 3, "required": False, "polarity": "prefer",
    }]
    catalog = _catalog()
    catalog.places.extend({"contentid": cid, "planningRole": "sightseeing_stop"} for cid in (4, 5, 6))
    catalog.preferences.extend({"contentid": cid, "preferenceTags": ""} for cid in (4, 5, 6))
    for count, expected in ((2, 1.0), (3, 0.0), (4, 1.0)):
        draft = {"days": [{"date": "2026-12-12", "places": [
            {"contentid": cid, "planningRole": "sightseeing_stop"}
            for cid in [1, 3, 4, 5, 6][:count]
        ]}]}
        assert route_preference_penalty(draft, compiled, catalog) == expected

    no_frequency = _compile({**atom, "frequency": None})
    assert not no_frequency["countRules"]
    assert no_frequency["softWarnings"] == [{
        "field": "atomicPreferences", "code": "ATOMIC_PREFERENCE_UNSUPPORTED",
    }]


def test_unit_label_is_not_a_generic_food_or_meal_match():
    atom = _atom(dimension="food", target="\uacf3", polarity="prefer", priority="required", scope="day",
                 frequency={"operator": "exact", "count": 3, "period": "day"})
    compiled = _compile(atom)
    assert compiled["countRules"] == []
    assert compiled["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]


def test_three_daily_meal_periods_are_a_soft_meal_count_with_scope_warning():
    targets = ("\uc544\uce68 \uc810\uc2ec \uc800\ub141", "\uc544\uce68\u00b7\uc810\uc2ec\u00b7\uc800\ub141", "\uc544\uce68/\uc810\uc2ec/\uc800\ub141")
    for target in targets:
        atom = _atom(dimension="meal_plan", target=target, polarity="prefer", priority="preferred", scope="day",
                     frequency={"operator": "exact", "count": 3, "period": "day"})
        compiled = _compile(atom)
        assert compiled["countRules"] == [{
            "memberRef": "p1", "dimension": "meals", "tag": None, "scope": "day",
            "operator": "exact", "count": 3, "required": False, "polarity": "prefer",
        }]
        assert compiled["softWarnings"] == [{"field": "atomicPreferences", "code": "ATOMIC_PREFERENCE_UNSUPPORTED"}]
        catalog = _catalog()
        catalog.places.extend({"contentid": cid, "planningRole": "meal_stop"} for cid in (4, 5, 6))
        catalog.preferences.extend({"contentid": cid, "preferenceTags": ""} for cid in (4, 5, 6))
        for count, expected in ((2, 1.0), (3, 0.0), (4, 1.0)):
            draft = {"days": [{"date": "2026-12-12", "places": [
                {"contentid": cid, "planningRole": "meal_stop"} for cid in [2, 4, 5, 6][:count]
            ]}]}
            assert route_preference_penalty(draft, compiled, catalog) == expected


def test_required_or_incomplete_meal_periods_remain_unsupported():
    three_periods = "\uc544\uce68 \uc810\uc2ec \uc800\ub141"
    base = _atom(dimension="meal_plan", target=three_periods, polarity="prefer", priority="preferred", scope="day",
                 frequency={"operator": "exact", "count": 3, "period": "day"})
    required = _compile({**base, "priority": "required"})
    assert required["countRules"] == []
    assert required["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]

    for target in ("\uc544\uce68", "\uc544\uce68 \uc810\uc2ec \uc810\uc2ec", three_periods):
        invalid = _compile({**base, "target": target,
                            "frequency": {"operator": "exact", "count": 2, "period": "day"}})
        assert invalid["countRules"] == []
        assert invalid["softWarnings"] == [{"field": "atomicPreferences", "code": "ATOMIC_PREFERENCE_UNSUPPORTED"}]
