import copy

import pytest

pytest.importorskip("chungbuk_trip_ai")

from app import preference_profile
from app.group_preferences import build_group_preferences
from app.preparation import _confirmation_missing, _response, _validate_interpret_result
from chungbuk_trip_ai import build_atomic_preferences


class ApiError(Exception):
    def __init__(self, status, code, *args, **kwargs):
        self.status, self.code = status, code
        super().__init__(code)


CONDITIONS = {"region": "충북", "startDate": "2026-12-12", "endDate": "2026-12-13"}
EVIDENCE = "숲길은 꼭 가고 싶어요"


def atom(**updates):
    value = {"dimension": "activity", "target": "숲길", "polarity": "prefer", "priority": "required",
             "priorityBasis": "explicit", "scope": "trip", "audience": "group", "frequency": None,
             "value": None, "unit": None, "approximate": False, "evidence": EVIDENCE}
    value.update(updates)
    return value


def test_profile_digest_tracks_original_and_common_conditions():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    assert preference_profile.response_profile_status(profile, EVIDENCE, CONDITIONS)["status"] == "current"
    assert preference_profile.response_profile_status(profile, EVIDENCE + " ", CONDITIONS)["status"] == "current"
    assert preference_profile.response_profile_status(profile, EVIDENCE + " 추가 메모", CONDITIONS)["status"] == "stale"
    changed = {**CONDITIONS, "startDate": "2026-12-13"}
    assert preference_profile.response_profile_status(profile, EVIDENCE, changed)["status"] == "stale"
    edited = preference_profile.apply_client_profile_edits(profile, profile, EVIDENCE, changed)
    assert edited["status"] == "stale"


def test_semantic_user_correction_gets_user_origin_and_server_ids():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    incoming = copy.deepcopy(profile)
    incoming["conditions"][0]["target"] = "숲길 산책"
    incoming["conditions"][0]["id"] = "forged-id"
    incoming["sourceDigest"] = "forged-digest"
    incoming["status"] = "stale"
    incoming["conditions"][0]["origin"] = "model"
    updated = preference_profile.apply_client_profile_edits(incoming, profile, EVIDENCE, CONDITIONS)
    assert updated["status"] == "current"
    assert updated["conditions"][0]["origin"] == "user"
    assert updated["conditions"][0]["id"] != "forged-id"
    assert updated["sourceDigest"] != "forged-digest"


def test_receipt_bound_first_roundtrip_preserves_model_origin_and_unresolved():
    unresolved = [{"code": "walking_ambiguous", "evidence": "걷기 한계는 잘 모르겠어요", "message": "허용량이 분명하지 않습니다."}]
    profile = build_atomic_preferences([atom()], unresolved, EVIDENCE + " 걷기 한계는 잘 모르겠어요", CONDITIONS)
    echoed = copy.deepcopy(profile)
    echoed["conditions"][0]["id"] = "forged-id"
    echoed["sourceDigest"] = "forged-digest"
    updated = preference_profile.apply_client_profile_edits(
        echoed, None, EVIDENCE + " 걷기 한계는 잘 모르겠어요", CONDITIONS, trusted_profile=profile)
    assert updated["conditions"][0]["origin"] == "model"
    assert updated["unresolved"] == unresolved
    assert updated["conditions"][0]["id"] != "forged-id"


def test_cached_analysis_receipt_is_bound_to_profile_and_source():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    result = {"analysisMeta": {"requestId": "receipt-123"}, "sourceConditionsVersion": 4,
              "sourcePreferenceVersion": 2, "suggestions": {"atomicPreferences": profile}}
    trusted = preference_profile.profile_from_cached_result(result, "receipt-123", EVIDENCE, CONDITIONS)
    assert trusted["conditions"][0]["origin"] == "model"
    with pytest.raises(ValueError):
        preference_profile.profile_from_cached_result(result, "foreign", EVIDENCE, CONDITIONS)
    with pytest.raises(ValueError):
        preference_profile.profile_from_cached_result(result, "receipt-123", EVIDENCE + " changed", CONDITIONS)


def test_later_manual_save_without_receipt_preserves_exact_model_facts_only():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    echoed = copy.deepcopy(profile)
    echoed["conditions"][0]["origin"] = "user"
    updated = preference_profile.apply_client_profile_edits(echoed, profile, EVIDENCE, CONDITIONS)
    assert updated["conditions"][0]["origin"] == "model"
    echoed["conditions"][0]["target"] = "수정된 숲길"
    changed = preference_profile.apply_client_profile_edits(echoed, profile, EVIDENCE, CONDITIONS)
    assert changed["conditions"][0]["origin"] == "user"


def test_v1_response_shape_hides_stored_atomic_profile():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    row = {"draft_json": {"originalText": EVIDENCE, "likes": "숲길", "avoid": None,
                          "avoidImportance": "required", "food": None, "foodImportance": "preferred",
                          "walking": None, "scheduleItems": [], "atomicPreferences": profile},
           "preference_version": 3, "confirmed_at": None, "confirmed_conditions_version": None,
           "confirmed_preference_version": None}
    assert "atomicPreferences" not in _response(row, 4)["draft"]
    changed_conditions = {**CONDITIONS, "startDate": "2026-12-13"}
    assert _response(row, 4, schema_version=2, conditions=changed_conditions)["draft"]["atomicPreferences"]["status"] == "stale"


def test_v2_interpret_result_canonicalizes_profile_and_does_not_require_walking():
    profile = build_atomic_preferences([atom()], [], EVIDENCE, CONDITIONS)
    result = {"suggestions": {"likes": "숲길", "avoid": None, "avoidImportance": "preferred",
                "food": None, "foodImportance": "preferred", "walking": None, "scheduleItems": []},
              "needsWalkingAnswer": False, "atomicPreferences": profile,
              "analysisMeta": {"provider": "ensemble", "providersUsed": ["gemini", "qwen"],
                               "degraded": False, "reviewRequired": False, "requestId": "private"}}
    public = _validate_interpret_result(result, ApiError, schema_version=2, original_text=EVIDENCE, conditions=CONDITIONS)
    assert public["needsWalkingAnswer"] is False
    assert public["suggestions"]["atomicPreferences"]["conditions"][0]["id"] == profile["conditions"][0]["id"]
    assert "requestId" not in public["analysisMeta"]
    assert "walking" not in _confirmation_missing({"originalText": EVIDENCE, "likes": "숲길", "walking": None, "atomicPreferences": profile}, 2)
    assert "walking" in _confirmation_missing({"originalText": EVIDENCE, "likes": "숲길", "walking": None}, 1)


def test_current_atomic_food_only_profile_does_not_force_extra_answers():
    text = "여행 중 초밥을 한 번 먹고 싶어요"
    profile = build_atomic_preferences([atom(dimension="food", target="초밥", priority="preferred", priorityBasis="unspecified", evidence=text)], [], text, CONDITIONS)
    draft = {"originalText": text, "likes": None, "walking": None, "food": "초밥", "atomicPreferences": profile}
    assert _confirmation_missing(draft, 2) == []
    assert "likes" in _confirmation_missing(draft, 1)
    draft["atomicPreferences"] = {**profile, "status": "stale"}
    assert "atomicPreferences" in _confirmation_missing(draft, 2)


def test_generation_group_projection_keeps_confirmed_group_and_personal_hard_only():
    profile = build_atomic_preferences([
        atom(),
        atom(target="땅콩", polarity="avoid", priority="required", priorityBasis="explicit", audience="personal", dimension="food"),
        atom(target="카페", priority="preferred", priorityBasis="explicit", audience="personal"),
    ], [], EVIDENCE + " 땅콩 알레르기가 있어요. 카페 좋아해요.", CONDITIONS)
    groups = build_group_preferences([{"memberRef": "p1", "confirmed": True, "atomicPreferences": profile}])
    assert len(groups["conditions"]) == 1
    assert groups["conditions"][0]["memberReferences"] == ["p1"]
    assert any(item["target"] == "카페" and item["memberReferences"] == ["p1"] for item in groups["softSupport"])
    assert groups["memberScopedConstraints"][0]["memberRef"] == "p1"
    assert groups["memberScopedConstraints"][0]["target"] == "땅콩"


def test_group_projection_deduplicates_members_and_reports_reviewable_opposition():
    seafood = atom(dimension="food", target="해산물", polarity="prefer", priority="preferred", audience="personal", priorityBasis="unspecified", evidence="해산물 먹고 싶어요")
    avoid = atom(dimension="food", target="해산물", polarity="avoid", priority="preferred", audience="personal", priorityBasis="unspecified", evidence="해산물은 피하고 싶어요")
    first = build_atomic_preferences([seafood, seafood], [], "해산물 먹고 싶어요", CONDITIONS)
    second = build_atomic_preferences([avoid], [], "해산물은 피하고 싶어요", CONDITIONS)
    projected = build_group_preferences([
        {"memberRef": "p1", "confirmed": True, "atomicPreferences": first},
        {"memberRef": "p2", "confirmed": True, "atomicPreferences": second},
        {"memberRef": "p3", "confirmed": False, "atomicPreferences": first},
        {"memberRef": "p4", "confirmed": True, "atomicPreferences": {**first, "status": "stale"}},
    ])
    assert projected["softSupport"][0]["memberCount"] == 1
    assert len(projected["conflicts"]) == 1
    assert projected["conflicts"][0]["code"] == "CONTRASTING_SOFT_PREFERENCES"
    assert projected["conflicts"][0]["memberReferences"] == ["p1", "p2"]
    assert not any("memberId" in item or "evidence" in item for values in projected.values() for item in values)


def test_unspecified_audience_soft_preferences_and_personal_required_positive_request_are_preserved():
    text = "초밥은 꼭 한 번 먹고 싶어요. 산을 좋아해요"
    first = atom(dimension="food", target="초밥", priority="required", priorityBasis="explicit", audience="personal", evidence="초밥은 꼭 한 번 먹고 싶어요")
    second = atom(target="산", priority="preferred", priorityBasis="unspecified", audience="unspecified", evidence="산을 좋아해요")
    profile = build_atomic_preferences([first, second], [], text, CONDITIONS)
    group = build_group_preferences([{"memberRef": "p1", "confirmed": True, "atomicPreferences": profile}])
    assert group["memberScopedConstraints"][0]["target"] == "초밥"
    assert group["memberScopedConstraints"][0]["priority"] == "required"
    assert group["softSupport"][0]["target"] == "산"


def test_group_projection_flags_only_incompatible_hard_numeric_ranges():
    maximum = atom(dimension="visit_count", target="방문 장소", polarity="limit", priority="required",
                   priorityBasis="explicit", audience="group", frequency={"count": 2, "operator": "max", "period": "day"},
                   evidence="하루 최대 두 곳은 꼭")
    minimum = atom(dimension="visit_count", target="방문 장소", polarity="limit", priority="required",
                   priorityBasis="explicit", audience="group", frequency={"count": 3, "operator": "min", "period": "day"},
                   evidence="하루 최소 세 곳은 꼭")
    a = build_atomic_preferences([maximum], [], maximum["evidence"], CONDITIONS)
    b = build_atomic_preferences([minimum], [], minimum["evidence"], CONDITIONS)
    projected = build_group_preferences([{"memberRef": "p1", "confirmed": True, "atomicPreferences": a},
                                         {"memberRef": "p2", "confirmed": True, "atomicPreferences": b}])
    assert projected["conflicts"] == [{"code": "INCOMPATIBLE_HARD_LIMITS", "dimension": "visit_count",
                                        "target": "방문 장소", "memberReferences": ["p1", "p2"], "needsReview": True}]
