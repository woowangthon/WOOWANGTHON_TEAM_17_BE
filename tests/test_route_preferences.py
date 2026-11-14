from types import SimpleNamespace

from app.route_preferences import (
    apply_route_preferences,
    compile_route_preferences,
    legacy_preferences,
    route_preference_penalty,
)


def _catalog():
    return SimpleNamespace(
        supported_tags=("자연", "초밥"),
        places=[
            {"contentid": 1, "planningRole": "sightseeing_stop"},
            {"contentid": 2, "planningRole": "meal_stop"},
            {"contentid": 3, "planningRole": "culture_stop"},
        ],
        preferences=[
            {"contentid": 1, "preferenceTags": "자연"},
            {"contentid": 2, "preferenceTags": "초밥"},
            {"contentid": 3, "preferenceTags": ""},
        ],
    )


def _atom(**changes):
    return {
        "memberRef": "p1", "dimension": "visit_count", "target": "방문 장소",
        "polarity": "limit", "priority": "required", "scope": "day",
        "frequency": {"operator": "max", "count": 1, "period": "day"},
        "value": None, "unit": None, "approximate": False,
        **changes,
    }


def _compile(*atoms, catalog=None):
    return compile_route_preferences({
        "atomicMemberRefs": ["p1"],
        "groupConditions": {"memberScopedConstraints": list(atoms)},
    }, catalog or _catalog())


def _plan(places, walks=None, status="verified", violations=None):
    edge_walks = None if walks == "unknown" else ([] if walks is None else walks)
    return {
        "days": [{"date": "2026-12-12", "nodes": [
            {"kind": "PLACE", **place} for place in places
        ], "edges": [{"status": "verified", "walkingSeconds": edge_walks}]}],
        "validation": {"status": status, "violations": list(violations or []), "warnings": []},
    }


def test_day_sightseeing_limit_excludes_meals_and_legacy_atomic_fields_clear():
    compiled = _compile(_atom())
    plan = apply_route_preferences(_plan([
        {"contentid": 1, "planningRole": "sightseeing_stop"},
        {"contentid": 3, "planningRole": "culture_stop"},
        {"contentid": 2, "planningRole": "meal_stop"},
    ]), compiled, _catalog())
    assert plan["validation"]["violations"][0]["code"] == "REQUIRED_PREFERENCE_UNSATISFIED"
    pref = {"memberRef": "p1", "likes": "자연", "avoid": "붐빔", "food": "초밥",
            "walking": "15", "scheduleItems": [{"kind": "appointment"}]}
    cleaned = legacy_preferences([pref], compiled)[0]
    assert all(cleaned[key] is None for key in ("likes", "avoid", "food", "walking"))
    assert cleaned["scheduleItems"] == pref["scheduleItems"]


def test_soft_target_is_a_penalty_and_exact_tag_does_not_invent_absence():
    soft = _atom(dimension="visit_count", target="방문 장소", priority="preferred",
                 frequency={"operator": "target", "count": 2, "period": "day"}, approximate=True)
    compiled = _compile(soft)
    assert route_preference_penalty({"days": [{"date": "2026-12-12", "places": [
        {"contentid": 1, "planningRole": "sightseeing_stop"}
    ]}]}, compiled, _catalog()) == 1.0
    tagged = _atom(dimension="activity", target="자연", polarity="prefer",
                   frequency={"operator": "exact", "count": 1, "period": "trip"}, scope="trip")
    tagged_plan = apply_route_preferences(_plan([
        {"contentid": 1, "planningRole": "sightseeing_stop"},
        {"contentid": 3, "planningRole": "culture_stop"},
    ]), _compile(tagged), _catalog())
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in tagged_plan["validation"]["warnings"]
    assert not tagged_plan["validation"]["violations"]


def test_15_minute_walking_bound_accepts_900_and_rejects_901_or_unknown():
    walk = _atom(dimension="walking", target="도보", polarity="limit", scope="trip",
                 frequency=None, value=15, unit="분")
    compiled = _compile(walk)
    under = apply_route_preferences(_plan([], [900]), compiled, _catalog())
    over = apply_route_preferences(_plan([], [901]), compiled, _catalog())
    unknown = apply_route_preferences(_plan([], "unknown"), compiled, _catalog())
    assert not under["validation"]["violations"]
    assert over["validation"]["violations"][0]["code"] == "REQUIRED_PREFERENCE_UNSATISFIED"
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in unknown["validation"]["warnings"]


def test_empty_projection_and_legacy_v1_behavior_are_noops():
    empty = compile_route_preferences({"atomicMemberRefs": [], "groupConditions": {}}, _catalog())
    source = _plan([], [0])
    assert not empty["issues"] and not empty["countRules"] and not empty["walkingRules"]
    assert apply_route_preferences(source, empty, _catalog()) == source
    assert route_preference_penalty({"days": []}, empty, _catalog()) == 0
