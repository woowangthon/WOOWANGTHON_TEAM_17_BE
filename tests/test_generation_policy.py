from app.generation_policy import CAR_LEG_MAX_SECONDS, car_leg_exceeds_limit, category_key


def test_car_limit_only_flags_known_place_to_place_car_legs_over_40_minutes():
    assert CAR_LEG_MAX_SECONDS == 2400
    assert not car_leg_exceeds_limit("car", 2400, from_kind="PLACE", to_kind="PLACE")
    assert car_leg_exceeds_limit("car", 2401, from_kind="PLACE", to_kind="PLACE")
    assert not car_leg_exceeds_limit("walking", 3600, from_kind="PLACE", to_kind="PLACE")
    assert not car_leg_exceeds_limit("car", 3600, from_kind="PLACE", to_kind="DEPARTURE")
    assert not car_leg_exceeds_limit("car", None, from_kind="PLACE", to_kind="PLACE")
    assert not car_leg_exceeds_limit("car", True, from_kind="PLACE", to_kind="PLACE")


def test_category_key_prefers_catalog_identity_then_known_role_without_grouping_unknowns():
    assert category_key({"contenttypeid": 12, "contentTypeName": "관광지",
                         "planningRole": "sightseeing_stop"}) == "contenttypeid:12"
    assert category_key({"contentTypeName": "전시관", "planningRole": "culture_stop"}) == "contentTypeName:전시관"
    assert category_key({"planningRole": "shopping_stop"}) == "planningRole:shopping"
    assert category_key({}) is None
    assert category_key(None) is None


def test_cafe_fine_category_requires_exact_catalog_tag():
    assert category_key({"contenttypeid": 12, "matchedTags": ["카페"]}) == "tag:카페"
    assert category_key({"contenttypeid": 12, "matchedTags": ["카페거리"]}) == "contenttypeid:12"
