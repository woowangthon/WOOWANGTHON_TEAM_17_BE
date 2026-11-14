import unicodedata
from types import SimpleNamespace

from app.preference_retrieval import PreferenceResolver
from app.route_preferences import (
    apply_route_preferences,
    compile_route_preferences,
    draft_preference_rules,
    route_preference_cost,
)


def _catalog():
    return SimpleNamespace(
        supported_tags=("휴식힐링", "카페", "조용한"),
        places=[
            {"contentid": 1, "title": "청주 물막국수 식당", "planningRole": "meal_stop"},
            {"contentid": 2, "title": "들깨수제비", "planningRole": "meal_stop"},
            {"contentid": 3, "title": "약초밥상", "planningRole": "meal_stop"},
            {"contentid": 4, "title": "돈가스 정식", "planningRole": "meal_stop"},
            {"contentid": 5, "title": "조용한 카페", "contentTypeName": "카페", "planningRole": "meal_stop",
             "overviewText": "창가 좌석이 있는 조용한 공간"},
            {"contentid": 6, "title": "시끌벅적 카페", "contentTypeName": "카페", "planningRole": "meal_stop"},
            {"contentid": 7, "title": "조용한 전시관", "planningRole": "culture_stop",
             "overviewText": "고요한 실내 전시"},
            {"contentid": 8, "title": "막국수 체험관", "planningRole": "culture_stop",
             "overviewText": "막국수 역사 전시"},
            {"contentid": 9, "title": "막국수", "planningRole": "overnight_lodging"},
            {"contentid": 10, "title": "숲길 산책로", "planningRole": "activity_stop",
             "overviewText": "나무가 우거진 숲길"},
        ],
        details=[
            {"contentid": 1, "menuText": "물막국수, 들깨수제비"},
            {"contentid": 2, "menuText": "들깨 수제비"},
            {"contentid": 4, "menuText": "돈카츠"},
        ],
        preferences=[
            {"contentid": 5, "preferenceTags": "카페", "vibeTags": "조용한"},
            {"contentid": 6, "preferenceTags": "카페", "vibeTags": "활기찬"},
            {"contentid": 7, "preferenceTags": "휴식힐링"},
        ],
    )


def _atom(dimension, target, **changes):
    return {"memberRef": "p1", "dimension": dimension, "target": target,
            "polarity": "prefer", "priority": "required", "scope": "trip",
            "frequency": None, "approximate": False, **changes}


def _compile(atom, catalog=None):
    return compile_route_preferences({"atomicMemberRefs": ["p1"],
                                     "groupConditions": {"memberScopedConstraints": [atom]}},
                                    catalog or _catalog())


def test_resolver_matches_food_variants_and_uses_role_and_compound_boundaries():
    resolver = PreferenceResolver(_catalog())
    assert resolver.resolve("막국수", "food") == [1]
    assert resolver.resolve(unicodedata.normalize("NFD", "막국수"), "food") == [1]
    assert resolver.resolve("들깨수제비", "food") == [1, 2]
    assert resolver.resolve("돈까스", "food") == [4]
    assert 3 not in resolver.resolve("초밥", "food")
    assert 8 not in resolver.resolve("막국수", "food")
    assert 9 not in resolver.resolve("막국수", "food")
    assert resolver.resolve("회", "food") == []


def test_resolver_compound_atmosphere_requires_all_evidence_on_same_place():
    resolver = PreferenceResolver(_catalog())
    assert resolver.resolve("조용한 카페", "atmosphere") == [5]
    assert resolver.resolve("힐링", "atmosphere") == [7]
    assert resolver.resolve("조용한", "atmosphere") == [5, 7]


def test_short_ambiguous_terms_need_an_exact_tag_subtype_or_menu_token():
    catalog = _catalog()
    catalog.places.extend([
        {"contentid": 23, "title": "한강대교", "contentTypeName": "교량", "planningRole": "sightseeing_stop"},
        {"contentid": 24, "title": "산", "contentTypeName": "산", "planningRole": "sightseeing_stop"},
        {"contentid": 25, "title": "강변 산책길", "planningRole": "activity_stop"},
        {"contentid": 26, "title": "회사", "planningRole": "meal_stop"},
        {"contentid": 27, "title": "회 코스", "planningRole": "meal_stop"},
        {"contentid": 28, "title": "생산 전시관", "planningRole": "culture_stop"},
        {"contentid": 29, "title": "회집", "planningRole": "meal_stop"},
    ])
    catalog.preferences.append({"contentid": 25, "preferenceTags": "강"})
    catalog.details.extend([
        {"contentid": 26, "menuText": "회사 운영"},
        {"contentid": 27, "menuText": "회, 매운탕"},
        {"contentid": 29, "menuText": "회 판매 중단"},
    ])
    catalog.preferences.append({"contentid": 29, "preferenceTags": "회"})
    resolver = PreferenceResolver(catalog)
    assert resolver.resolve("강", "activity") == [25]
    assert resolver.resolve("산", "activity") == [24]
    assert resolver.resolve("강", "atmosphere") is None
    assert resolver.resolve("회", "food") == [27]
    assert resolver.contradicted_contentids("회", "food") == [29]
    compiled = _compile(_atom("food", "회"), catalog)
    assert compiled["countRules"][0]["dimension"] == "catalog_food"
    assert compiled["countRules"][0]["contentids"] == [27]
    conflict = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 29, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, compiled, catalog)
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in conflict["validation"]["warnings"]


def test_mountain_name_and_hiking_tag_identify_the_destination_itself():
    catalog = _catalog()
    for cid, title, role in (
        (40, "솔바람산", "sightseeing_stop"), (41, "돌산 (괴산)", "sightseeing_stop"),
        (42, "솔바람산 전망대", "sightseeing_stop"), (43, "클럽 별산", "activity_stop"),
        (44, "산장식당", "meal_stop"), (45, "한빛부동산", "sightseeing_stop"),
    ):
        catalog.places.append({"contentid": cid, "title": title, "contentTypeName": "관광지",
                               "planningRole": role, "overviewText": "유명한 산 근처"})
        catalog.preferences.append({"contentid": cid, "preferenceTags": "자연|트레킹등산" if cid != 45 else "쇼핑"})
    assert PreferenceResolver(catalog).resolve("산", "activity") == [40, 41]


def test_activity_query_keeps_all_components_and_rejects_nearby_or_negated_mentions():
    catalog = _catalog()
    catalog.places.extend([
        {"contentid": 11, "title": "도자기 체험", "planningRole": "activity_stop",
         "overviewText": "도자기 빚기 체험"},
        {"contentid": 12, "title": "근처 숲길", "planningRole": "activity_stop",
         "overviewText": "이곳은 근처의 숲길을 소개합니다"},
        {"contentid": 13, "title": "막국수 식당", "planningRole": "meal_stop"},
        {"contentid": 14, "title": "청주 공원", "contentTypeName": "공원", "planningRole": "activity_stop",
         "overviewText": "산책하기 좋은 공간"},
        {"contentid": 15, "title": "공원", "contentTypeName": "공원", "planningRole": "activity_stop"},
        {"contentid": 16, "title": "일반 카페", "planningRole": "meal_stop",
         "overviewText": "조용한 분위기가 아닌 카페"},
        {"contentid": 17, "title": "도자기 체험 안내소", "planningRole": "culture_stop",
         "overviewText": "도자기 체험이 아닌 전시만 운영합니다"},
        {"contentid": 18, "title": "수제비누 카페", "planningRole": "meal_stop"},
        {"contentid": 19, "title": "스키마 전시관", "planningRole": "culture_stop"},
        {"contentid": 20, "title": "일반 카페", "planningRole": "meal_stop",
         "overviewText": "조용하지는 않은 카페"},
        {"contentid": 21, "title": "막국수 전문점", "planningRole": "meal_stop"},
        {"contentid": 22, "title": "한식당", "planningRole": "meal_stop"},
        {"contentid": 32, "title": "공원 산책", "planningRole": "activity_stop"},
        {"contentid": 33, "title": "도자기 체험 안내소", "planningRole": "activity_stop"},
    ])
    catalog.details.extend([
        {"contentid": 13, "menuText": "막국수는 제공하지 않습니다"},
        {"contentid": 18, "menuText": "수제비누"},
        {"contentid": 21, "menuText": "막국수는 제공하지 않습니다"},
        {"contentid": 22, "menuText": "막국수는 제공하지 않습니다"},
        {"contentid": 33, "experienceText": "체험 운영 종료"},
    ])
    catalog.places[-2]["overviewText"] = "산책 금지"
    resolver = PreferenceResolver(catalog)
    assert resolver.resolve("도자기 체험", "activity") == [11]
    assert resolver.resolve("공원 산책", "activity") == [14]
    assert resolver.resolve("숲길", "activity") == [10]
    assert resolver.resolve("근처의 숲길", "activity") is None
    assert resolver.resolve("조용한 카페", "atmosphere") == [5]
    assert 13 not in resolver.resolve("막국수", "food")
    assert 13 not in resolver.known_contentids("막국수", "food")
    assert 21 not in resolver.known_contentids("막국수", "food")
    assert 22 in resolver.known_contentids("막국수", "food")
    assert 18 not in resolver.resolve("수제비", "food")
    assert 19 not in resolver.resolve("스키", "activity")
    assert resolver.resolve("막국수 제공하지 않습니다", "food") is None
    assert resolver.resolve("조용한 카페", "atmosphere") == [5]


def test_valid_empty_lookup_stays_typed_and_never_claims_satisfaction():
    compiled = _compile(_atom("food", "라멘"))
    rule = compiled["countRules"][0]
    assert rule["dimension"] == "catalog_food"
    assert rule["contentids"] == []
    assert rule["sourceDimension"] == "food"
    assert rule["target"] == "라멘"
    assert rule["required"] is True
    plan = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 2, "planningRole": "meal_stop"}]}],
                                   "validation": {"status": "verified", "violations": [], "warnings": []}},
                                  compiled, _catalog())
    assert not plan["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in plan["validation"]["warnings"]
    assert draft_preference_rules(compiled, _catalog(), [1, 2, 3]) == []


def test_soft_empty_target_has_no_unknown_penalty_but_positive_ids_remain_ranked():
    empty = _compile(_atom("food", "라멘", priority="preferred"))
    assert route_preference_cost({"days": [{"places": []}]}, empty, _catalog()) == (0, 0.0)
    known = _compile(_atom("food", "막국수", priority="preferred", scope=None))
    unserved = {"days": [{"places": []}]}
    assert route_preference_cost(unserved, known, _catalog()) == (0, 1.0)


def test_hard_text_food_requires_positive_verified_id_and_draft_intersects_allowed():
    catalog = _catalog()
    compiled = _compile(_atom("food", "막국수"), catalog)
    rule = compiled["countRules"][0]
    assert rule["dimension"] == "catalog_food"
    assert rule["contentids"] == [1]
    assert draft_preference_rules(compiled, catalog, [2, 3, 1]) == [{
        "contentids": [1], "period": "trip", "operator": "min", "count": 1, "required": True,
    }]
    missing = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 3, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, compiled, catalog)
    assert not missing["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in missing["validation"]["warnings"]
    satisfied = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 1, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, compiled, catalog)
    assert not satisfied["validation"]["violations"]


def test_typed_draft_rule_uses_ids_without_thawing_catalog_rows():
    class NoReadCatalog:
        @property
        def places(self):
            raise AssertionError("typed ID rules must not scan catalog places")

        @property
        def preferences(self):
            raise AssertionError("typed ID rules must not scan catalog preferences")

    compiled = {"countRules": [{"dimension": "catalog_food", "contentids": [4, 2, 2],
                                 "scope": "trip", "operator": "min", "count": 1,
                                 "required": True}]}
    assert draft_preference_rules(compiled, NoReadCatalog(), [3, 4, 2]) == [{
        "contentids": [2, 4], "period": "trip", "operator": "min", "count": 1, "required": True,
    }]


def test_typed_max_does_not_certify_an_unlisted_candidate_without_explicit_negative():
    catalog = _catalog()
    atom = _atom("food", "막국수", polarity="prefer",
                  frequency={"operator": "max", "count": 0, "period": "trip"})
    compiled = _compile(atom, catalog)
    plan = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 3, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, compiled, catalog)
    assert not plan["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in plan["validation"]["warnings"]


def test_empty_typed_rules_are_known_for_inapplicable_roles_or_explicit_absence():
    catalog = _catalog()
    catalog.places.append({"contentid": 22, "title": "한식당", "planningRole": "meal_stop"})
    catalog.details.append({"contentid": 22, "menuText": "막국수는 제공하지 않습니다"})
    max_rule = _compile(_atom("food", "막국수", polarity="limit",
                              frequency={"operator": "max", "count": 0, "period": "trip"}), catalog)
    unavailable = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 22, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, max_rule, catalog)
    assert not unavailable["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" not in unavailable["validation"]["warnings"]
    inapplicable = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 10, "planningRole": "activity_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, max_rule, catalog)
    assert not inapplicable["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" not in inapplicable["validation"]["warnings"]


def test_free_text_limit_max_zero_stays_max_zero():
    atom = _atom("food", "막국수", polarity="limit",
                 frequency={"operator": "max", "count": 0, "period": "trip"})
    compiled = _compile(atom)
    rule = compiled["countRules"][0]
    assert rule["dimension"] == "catalog_food"
    assert rule["operator"] == "max"
    assert rule["count"] == 0


def test_activity_and_compound_atmosphere_compile_to_typed_positive_ids():
    activity = _compile(_atom("activity", "숲길"))
    assert activity["countRules"][0]["dimension"] == "catalog_activity"
    assert activity["countRules"][0]["contentids"] == [10]
    atmosphere = _compile(_atom("atmosphere", "조용한 카페"))
    assert atmosphere["countRules"][0]["dimension"] == "catalog_atmosphere"
    assert atmosphere["countRules"][0]["contentids"] == [5]


def test_real_compound_atmosphere_tag_alias_keeps_actual_tag_name():
    catalog = _catalog()
    compiled = _compile(_atom("atmosphere", "힐링", priority="preferred", scope=None), catalog)
    assert compiled["countRules"] == []
    assert compiled["predicates"][0]["tag"] == "휴식힐링"


def test_soft_avoid_unions_exact_tag_and_menu_evidence():
    catalog = SimpleNamespace(
        supported_tags=("막국수",),
        places=[
            {"contentid": 21, "title": "막국수 전문점", "planningRole": "meal_stop"},
            {"contentid": 22, "title": "한식당", "planningRole": "meal_stop"},
        ],
        details=[{"contentid": 21, "menuText": "막국수"}, {"contentid": 22, "menuText": "수제비"}],
        preferences=[{"contentid": 22, "preferenceTags": "막국수"}],
    )
    compiled = _compile(_atom("food", "막국수", polarity="avoid", priority="preferred", scope=None), catalog)
    assert compiled["predicates"] == [{
        "memberRef": "p1", "field": "atomicPreferences", "tag": "막국수",
        "polarity": "avoid", "scope": "meal", "required": False,
        "contentids": [21, 22], "sourceDimension": "food", "target": "막국수",
    }]


def test_exact_tag_does_not_override_explicit_menu_contradiction():
    catalog = SimpleNamespace(
        supported_tags=("수제비",),
        places=[{"contentid": 31, "title": "수제비집", "planningRole": "meal_stop"}],
        details=[{"contentid": 31, "menuText": "수제비 판매 중단"}],
        preferences=[{"contentid": 31, "preferenceTags": "수제비"}],
    )
    compiled = _compile(_atom("food", "수제비"), catalog)
    assert compiled["countRules"][0]["dimension"] == "catalog_food"
    assert compiled["countRules"][0]["contentids"] == []
    plan = apply_route_preferences({"days": [{"date": "2026-12-12", "nodes": [
        {"kind": "PLACE", "contentid": 31, "planningRole": "meal_stop"}],
    }], "validation": {"status": "verified", "violations": [], "warnings": []}}, compiled, catalog)
    assert not plan["validation"]["violations"]
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in plan["validation"]["warnings"]


def test_soft_text_target_gets_id_predicate_and_trip_goal_but_soft_avoid_is_not_a_goal():
    wish = _compile(_atom("food", "막국수", priority="preferred", scope=None))
    assert wish["predicates"] == [{
        "memberRef": "p1", "field": "atomicPreferences", "tag": None,
        "contentids": [1], "sourceDimension": "food", "target": "막국수",
        "polarity": "prefer", "scope": "meal", "required": False,
    }]
    assert wish["countRules"][0]["operator"] == "min"
    assert wish["countRules"][0]["count"] == 1
    avoid = _compile(_atom("food", "막국수", polarity="avoid", priority="preferred", scope=None))
    assert avoid["predicates"][0]["polarity"] == "avoid"
    assert not avoid["countRules"]


def test_hard_avoid_and_ambiguous_one_character_queries_remain_unsupported():
    avoided = _compile(_atom("food", "땅콩", polarity="avoid"))
    assert avoided["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]
    ambiguous = _compile(_atom("food", "회"))
    assert ambiguous["issues"] == []
    assert ambiguous["countRules"][0]["dimension"] == "catalog_food"
    assert ambiguous["countRules"][0]["contentids"] == []
