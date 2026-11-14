from types import SimpleNamespace

from app.route_preferences import (
    apply_route_preferences,
    compile_route_preferences,
    legacy_preferences,
    route_preference_penalty,
    draft_preference_rules,
    is_market_place,
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


def _market_catalog():
    return SimpleNamespace(
        supported_tags=("쇼핑", "시장"),
        places=[
            {"contentid": 40, "title": "청주 육거리종합시장", "planningRole": "shopping_stop",
             "recommendationUse": "SPECIAL_SHOPPING"},
            {"contentid": 41, "title": "사창시장약국", "planningRole": "shopping_stop",
             "recommendationUse": "GENERAL_SHOPPING"},
            {"contentid": 42, "title": "청주 쇼핑몰", "planningRole": "shopping_stop",
             "recommendationUse": "GENERAL_SHOPPING"},
            {"contentid": 43, "title": "문화전시장", "planningRole": "culture_stop",
             "recommendationUse": "MAIN_DESTINATION"},
            {"contentid": 44, "title": "역전시장", "planningRole": "shopping_stop",
             "recommendationUse": "GENERAL_SHOPPING"},
            {"contentid": 45, "title": "환경문화전시장", "planningRole": "shopping_stop",
             "recommendationUse": "SPECIAL_SHOPPING"},
            {"contentid": 46, "title": "충북전시시장", "planningRole": "shopping_stop",
             "recommendationUse": "SPECIAL_SHOPPING"},
        ],
        preferences=[
            {"contentid": 40, "preferenceTags": "쇼핑"},
            {"contentid": 41, "preferenceTags": "쇼핑"},
            {"contentid": 42, "preferenceTags": "쇼핑"},
            {"contentid": 43, "preferenceTags": "시장"},
            {"contentid": 44, "preferenceTags": "쇼핑"},
            {"contentid": 45, "preferenceTags": "쇼핑"},
            {"contentid": 46, "preferenceTags": "쇼핑"},
        ],
    )


def test_plain_market_wish_compiles_as_soft_trip_presence_and_grounds_specific_places():
    catalog = _market_catalog()
    compiled = compile_route_preferences({
        "atomicMemberRefs": ["p1"],
        "groupConditions": {"conditions": [{
            "memberRef": "p1", "dimension": "activity", "target": "시장에 가고 싶어요",
            "polarity": "prefer", "priority": "preferred", "scope": None,
            "frequency": None, "approximate": False,
        }]},
    }, catalog)

    assert compiled["issues"] == []
    assert compiled["countRules"] == [{
        "memberRef": "p1", "dimension": "market", "tag": None, "scope": "trip",
        "operator": "min", "count": 1, "required": False, "polarity": "prefer",
    }]
    assert is_market_place(catalog.places[0], {"쇼핑"})
    assert not is_market_place(catalog.places[1], {"쇼핑"})
    assert not is_market_place(catalog.places[2], {"쇼핑"})
    assert not is_market_place(catalog.places[3], {"시장"})
    assert is_market_place(catalog.places[4], {"쇼핑"})
    assert not is_market_place(catalog.places[5], {"쇼핑"})
    assert is_market_place(catalog.places[6], {"쇼핑"})
    assert not is_market_place({"contentid": 47, "title": "청주 쇼핑몰", "addr1": "시장 인근",
                                "planningRole": "shopping_stop", "recommendationUse": "GENERAL_SHOPPING"},
                               {"쇼핑"})
    assert draft_preference_rules(compiled, catalog, [40, 41, 42, 43]) == [{
        "contentids": [40], "period": "trip", "operator": "min", "count": 1, "required": False,
    }]

    assert route_preference_penalty({"days": [{"places": []}]}, compiled, catalog) == 1.0
    assert route_preference_penalty({"days": [{"places": [{"contentid": 40}]}]}, compiled, catalog) == 0.0
    absent = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": []}],
                                      "validation": {"status": "verified", "violations": [], "warnings": []}},
                                     compiled, catalog)
    assert absent["validation"]["status"] == "verified"
    assert "ATOMIC_PREFERENCE_UNRESOLVED" not in absent["validation"]["warnings"]


def test_duplicate_members_market_goal_counts_once_in_penalty():
    compiled = compile_route_preferences({
        "atomicMemberRefs": ["p1", "p2"],
        "groupConditions": {"conditions": [
            _atom(memberRef="p1", dimension="activity", target="시장", polarity="prefer",
                  priority="preferred", scope=None, frequency=None),
            _atom(memberRef="p2", dimension="activity", target="시장", polarity="prefer",
                  priority="preferred", scope=None, frequency=None),
        ]},
    }, _market_catalog())
    assert len(compiled["countRules"]) == 2
    assert route_preference_penalty({"days": [{"places": []}]}, compiled, _market_catalog()) == 1.0


def test_broad_shopping_tag_is_not_a_market_wish():
    compiled = compile_route_preferences({
        "atomicMemberRefs": ["p1"],
        "groupConditions": {"conditions": [{
            "memberRef": "p1", "dimension": "activity", "target": "쇼핑",
            "polarity": "prefer", "priority": "preferred", "scope": None,
            "frequency": None, "approximate": False,
        }]},
    }, _market_catalog())
    assert not compiled["countRules"]
    assert not any(rule.get("dimension") == "market" for rule in compiled["countRules"])


def test_legacy_market_recognition_is_exact_and_skips_atomic_profile_members():
    compiled = compile_route_preferences({
        "atomicMemberRefs": ["p1"],
        "confirmedPreferences": [
            {"memberRef": "p1", "likes": "시장에 가고 싶어요"},
            {"memberRef": "p2", "likes": "산책, 시장에 가고 싶어요"},
            {"memberRef": "p3", "likes": "시장"},
            {"memberRef": "p4", "likes": "쇼핑을 좋아해요"},
            {"memberRef": "p5", "likes": "시장 근처 카페에 가고 싶어요"},
            {"memberRef": "p6", "likes": "시장 또는 쇼핑"},
            {"memberRef": "p7", "likes": "시장 싫어요"},
        ],
        "groupConditions": {"conditions": [{
            "memberRef": "p1", "dimension": "activity", "target": "시장",
            "polarity": "prefer", "priority": "preferred", "scope": None,
            "frequency": None, "approximate": False,
        }]},
    }, _market_catalog())
    assert [rule["memberRef"] for rule in compiled["countRules"]] == ["p1", "p2", "p3"]


def test_required_market_needs_explicit_scope_and_unsupported_exact_max_do_not_claim_absence():
    unknown_scope = _compile(_atom(dimension="activity", target="시장", polarity="prefer",
                                   priority="required", scope=None, frequency=None))
    assert unknown_scope["countRules"] == []
    assert unknown_scope["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]

    explicit_trip = _compile(_atom(dimension="activity", target="시장", polarity="prefer",
                                    priority="required", scope="trip", frequency=None))
    assert explicit_trip["countRules"][0]["required"] is True
    assert explicit_trip["countRules"][0]["operator"] == "min"

    for operator in ("exact", "max"):
        compiled = _compile(_atom(dimension="activity", target="시장", polarity="prefer",
                                   priority="required", scope="trip",
                                   frequency={"operator": operator, "count": 1, "period": "trip"}))
        assert compiled["countRules"] == []
        assert compiled["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]

    hard_min = _compile(_atom(dimension="activity", target="시장", polarity="prefer",
                              priority="required", scope="trip", frequency=None))
    plan = apply_route_preferences(_plan([{"contentid": 42, "planningRole": "shopping_stop"}]),
                                   hard_min, _market_catalog())
    assert not plan["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in plan["validation"]["warnings"]
