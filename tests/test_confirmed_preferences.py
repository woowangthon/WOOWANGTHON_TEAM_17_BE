"""Offline checks for the direct confirmed-v1 path."""
from dataclasses import dataclass

import pytest

from app.candidate_selection import build_confirmed_candidate_selection
from app.confirmed_preferences import analyze_preferences, preference_issues


@dataclass
class Snapshot:
    conditions: dict
    confirmedPreferences: list
    fingerprint: str = "confirmed-source"

    def as_dict(self):
        return {"conditions": self.conditions, "confirmedPreferences": self.confirmedPreferences,
                "_identity": {"actorMemberId": "member-1", "members": [
                    {"memberId": "member-1", "role": "OWNER"}]}}


class Catalog:
    digest = "catalog"
    details = []

    def __init__(self):
        self.places = [{"contentid": 11, "title": "산책숲", "planningRole": "sightseeing_stop",
                        "recommendationUse": "MAIN_DESTINATION", "adminCity": "청주시",
                        "geoValidated": "yes", "mapx": 127.0, "mapy": 36.0}]
        self.details = [{"contentid": 11, "planningStatusV2": "candidate_needs_live_validation"}]
        self.preferences = [{"contentid": 11, "preferenceTags": "자연|카페", "vibeTags": ""}]


def _pref(likes="정확히 모르는 취향", avoid=None, food=None, importance="preferred", schedule=None):
    return {"memberRef": "p1", "likes": likes, "avoid": avoid, "avoidImportance": "required",
            "food": food, "foodImportance": importance, "walking": "40", "scheduleItems": schedule or []}


def _snapshot(pref):
    return Snapshot({"region": "청주시", "startDate": "2026-12-12", "endDate": "2026-12-12",
                     "lodging": None, "fixedSchedule": None}, [pref])


def test_unknown_soft_text_is_unscored_and_does_not_block():
    analysis = analyze_preferences([_pref()], {"자연", "카페"})
    assert analysis["predicates"] == []
    assert analysis["issues"] == []
    result = build_confirmed_candidate_selection(_snapshot(_pref()), Catalog(), analysis)
    assert result.ready_for_draft
    assert result.as_dict()["eligible"][0]["memberScores"]["p1"] == 0


def test_required_unknown_is_blocked_without_claiming_catalog_absence_is_safe():
    pref = _pref(avoid="못 찾는 안전 조건", importance="required")
    analysis = analyze_preferences([pref], {"자연", "카페"})
    result = build_confirmed_candidate_selection(_snapshot(pref), Catalog(), analysis)
    assert not result.ready_for_draft
    assert "REQUIRED_PREFERENCE_UNSUPPORTED" in result.as_dict()["unresolvedRequired"]


def test_required_food_requires_full_clause_coverage_and_only_scores_meal_rows():
    pref = _pref(food="카페, 땅콩 알레르기", importance="required")
    analysis = analyze_preferences([pref], {"자연", "카페"})
    assert any(i["code"] == "REQUIRED_PREFERENCE_UNSUPPORTED" for i in analysis["issues"])
    pref = _pref(food="카페", importance="preferred")
    analysis = analyze_preferences([pref], {"자연", "카페"})
    assert analysis["predicates"] == [{"memberRef": "p1", "field": "food", "tag": "카페",
                                        "polarity": "prefer", "scope": "meal", "required": False}]


@pytest.mark.parametrize("food", ["카페와 땅콩 알레르기", "카페 계단 없는 휠체어 접근"])
def test_required_food_mixed_same_clause_is_not_partially_accepted(food):
    analysis = analyze_preferences([_pref(food=food, importance="required")], {"자연", "카페"})
    assert any(issue["code"] == "REQUIRED_PREFERENCE_UNSUPPORTED" for issue in analysis["issues"])


def test_negated_soft_like_does_not_become_positive_substring_match():
    analysis = analyze_preferences([_pref(likes="자연은 싫고 카페")], {"자연", "카페"})
    assert not any(p["tag"] == "자연" and p["polarity"] == "prefer" for p in analysis["predicates"])
    assert {("likes", "PREFERENCE_CONFLICT")} <= {(i["field"], i["code"]) for i in analysis["issues"]}


def test_only_owner_confirmed_group_scope_becomes_fixed_visit_and_personal_gets_notice():
    schedule = [{"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "kind": "appointment", "scope": "personal",
                 "placeQuery": "산책숲", "contentid": 11, "date": "2026-12-12", "time": "11:00",
                 "endDate": None, "endTime": None}]
    pref = _pref(schedule=schedule)
    assert {"field": "scheduleItems", "code": "PERSONAL_SCHEDULE_NOT_SHARED"} in preference_issues([pref], {"자연", "카페"})
    analysis = analyze_preferences([pref], {"자연", "카페"})
    result = build_confirmed_candidate_selection(_snapshot(pref), Catalog(), analysis)
    assert result.as_dict()["fixedVisits"] == []
    group_pref = _pref(schedule=[{**schedule[0], "scope": "group"}])
    group_result = build_confirmed_candidate_selection(_snapshot(group_pref), Catalog(), analyze_preferences([group_pref], {"자연", "카페"}))
    assert group_result.as_dict()["fixedVisits"] == [{"date": "2026-12-12", "contentid": 11, "position": 0}]


def test_group_appointment_slots_follow_actual_times_and_ignore_member_group_scope():
    from app.confirmed_preferences import group_schedule_anchors

    base = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "kind": "appointment", "scope": "group",
            "placeQuery": "산책숲", "contentid": 11, "date": "2026-12-12", "time": "15:00",
            "endDate": None, "endTime": None}
    prefs = [_pref(schedule=[base, {**base, "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "time": "09:00"}])]
    owner_identity = {"actorMemberId": "member-1", "members": [{"memberId": "member-1", "role": "OWNER"}]}
    anchors = group_schedule_anchors(prefs, {}, owner_identity)
    assert [(a["startTime"], a["position"]) for a in anchors] == [("09:00", 0), ("15:00", 1)]
    member_identity = {"actorMemberId": "member-2", "members": [{"memberId": "member-2", "role": "MEMBER"}]}
    assert group_schedule_anchors(prefs, {}, member_identity) == []


@pytest.mark.parametrize("changes", [{"count": 9}, {"count": 1, "endDate": "2027-02-01"}])
def test_shared_schedule_overflow_or_unsupported_range_blocks_instead_of_omitting(changes):
    import uuid
    schedule = [{"id": str(uuid.uuid4()), "kind": "appointment", "scope": "group", "placeQuery": "산책숲",
                 "contentid": 11 + n, "date": "2026-12-12", "time": "11:00", "endTime": None,
                 "endDate": changes.get("endDate")} for n in range(changes["count"])]
    pref = _pref(schedule=schedule)
    result = build_confirmed_candidate_selection(_snapshot(pref), Catalog(), analyze_preferences([pref], {"자연", "카페"}))
    assert not result.ready_for_draft
    assert "REQUIRED_PREFERENCE_UNSATISFIED" in result.as_dict()["unresolvedRequired"]


def test_confirmed_generation_entry_uses_saved_group_appointment_without_review_object():
    import asyncio

    from types import SimpleNamespace
    from app.generation_draft import generate_candidate_draft

    item = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "kind": "appointment", "scope": "group",
            "placeQuery": "산책숲", "contentid": 11, "date": "2026-12-12", "time": "11:00",
            "endDate": None, "endTime": "12:00"}
    snapshot = Snapshot({"region": "청주시", "startDate": "2026-12-12", "endDate": "2026-12-12",
                         "lodging": None, "fixedSchedule": None}, [_pref(schedule=[item])])
    catalog = Catalog()
    catalog.presentation_place = lambda contentid: {"contentid": contentid, "title": "산책숲", "feeLabel": "요금 확인 필요"}
    analysis = analyze_preferences(snapshot.confirmedPreferences, {"자연", "카페"})
    seen = {}

    def provider(dates, candidates, fixed, maximum):
        seen["fixed"] = fixed
        return {"days": [{"date": dates[0], "contentids": [11]}]}

    result = asyncio.run(generate_candidate_draft(
        snapshot, None, catalog, None, lambda: snapshot, lambda: catalog,
        confirmed_evidence=analysis, common_settings={"reviewedCommonFields": []}, drafter=provider))
    assert seen["fixed"] == [{"date": "2026-12-12", "contentid": 11, "position": 0}]
    assert result.as_dict()["days"][0]["places"][0]["title"] == "산책숲"
