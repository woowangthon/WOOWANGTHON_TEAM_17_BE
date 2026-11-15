"""Offline regression vectors for bounded Korean query interpretation."""
import unicodedata

from app.korean_text import SearchIntent, analyze_search_intent, normalize_target


def test_location_and_primary_term_are_separated_for_market_meal_query():
    assert analyze_search_intent("전통시장에서 국수 먹고 싶어") == SearchIntent(
        terms=("국수",), excluded=(), locations=("전통시장",), exclusive=False, food_terms=("국수",))
    assert analyze_search_intent("전통시장 안에서 국수 먹고 싶어") == analyze_search_intent("전통시장에서 국수 먹고 싶어")
    assert analyze_search_intent("시장에서는 국수 말고 만두 먹고 싶어") == SearchIntent(
        terms=("만두",), excluded=("국수",), locations=("시장",), exclusive=False, food_terms=("만두",))
    assert analyze_search_intent("시장 근처에서 국수 먹고 싶어").nearby_locations == ("시장",)
    assert analyze_search_intent("전통시장에도 가고 싶어").terms == ("전통시장",)
    assert analyze_search_intent("전통시장에도 가고 싶어").locations == ()


def test_morphology_keeps_compounds_and_extracts_adjectives():
    assert analyze_search_intent("조용한 카페 추천해줘").terms == ("카페", "조용하다")
    assert analyze_search_intent("청주 전통시장 국수").terms == ("청주", "전통시장", "국수")
    assert analyze_search_intent("모두 요청해줘").terms == ()


def test_negative_scope_and_contrastive_positive_are_not_inverted():
    assert analyze_search_intent("시장 말고 공원") == SearchIntent(
        terms=("공원",), excluded=("시장",), locations=(), exclusive=False)
    assert analyze_search_intent("시장은 안 가고 공원은 가고 싶어") == SearchIntent(
        terms=("공원",), excluded=("시장",), locations=(), exclusive=False)
    assert analyze_search_intent("시장은 싫어").excluded == ("시장",)


def test_exclusive_and_particle_normalization_keep_lexical_names():
    assert analyze_search_intent("시장만").exclusive is True
    assert normalize_target("시장에서") == "시장"
    assert normalize_target("전통시장에도") == "전통시장"
    assert normalize_target("국수를") == "국수"
    assert normalize_target(unicodedata.normalize("NFD", "카페도")) == "카페"
    assert normalize_target("포도도") == "포도"
    assert normalize_target("포도") == "포도"
    assert normalize_target("포도까지") == "포도"
    assert normalize_target("여의도") == "여의도"
    assert normalize_target("여의도도") == "여의도"
    assert normalize_target("시장은 싫어") == "시장은 싫어"
    assert normalize_target("시장 말고 공원") == "시장 말고 공원"
    assert analyze_search_intent("A quiet cafe").terms == ("A", "quiet", "cafe")
    intent = analyze_search_intent("여행 " * 150 + "시장 빼고")
    assert intent.excluded == ("시장",)


def test_market_route_matching_accepts_particles_but_rejects_negated_targets():
    from app.preference_retrieval import _normalized_query
    from app.route_preferences import _market_target

    for target in ("시장", "시장만", "전통시장에서도", "재래시장을"):
        assert _market_target({"dimension": "activity", "target": target})
    for target in ("시장은 싫어", "시장 말고 공원", "여의도도"):
        assert not _market_target({"dimension": "activity", "target": target})
    assert _normalized_query("국수도 먹고 싶어") == _normalized_query("국수")
    assert _normalized_query("공원에도 가고 싶어") == _normalized_query("공원")
