"""Clarification and hard-conflict regression checks with synthetic inputs."""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.confirmed_preferences import analyze_preferences, group_schedule_anchors
from app.preference_grounding import ground_interpretation, schedule_conflict_fields
from app.preparation import _schedule_clarifications


def interpretation(walk="20"):
    return {"suggestions": {"likes": "자연", "avoid": None, "avoidImportance": "required",
                            "food": None, "foodImportance": "preferred", "walking": walk,
                            "scheduleItems": []}, "needsWalkingAnswer": walk is None}


def appointment(key="a", **extra):
    return {"id": key, "kind": "appointment", "scope": "group", "contentid": 1,
            "placeQuery": "합성 장소1", "date": "2026-12-12", "time": "15:00",
            "endDate": None, "endTime": None, **extra}


def pref(ref="p1", **extra):
    return {"memberRef": ref, "likes": "자연", "avoid": None, "avoidImportance": "required",
            "food": None, "foodImportance": "preferred", "walking": "20", "scheduleItems": [], **extra}


def test_competing_explicit_walk_limits_request_answer_and_do_not_mutate_provider():
    source = "도보는 한 번에 최대 20분까지 가능해요. 도보는 한 번에 최대 40분까지 가능해요."
    original = interpretation()
    before = copy.deepcopy(original)
    clean = ground_interpretation(source, original)
    assert clean["suggestions"]["walking"] is None and clean["needsWalkingAnswer"]
    assert original == before
    assert clean.keys() == original.keys()


def test_natural_competing_walking_bounds_are_not_silently_reduced_to_forty():
    source = "한 번에 최대 20분까지 걷는 것은 괜찮지만, 한 번에 최대 40분까지 도보 이동도 가능해요."
    result = ground_interpretation(source, interpretation("40"))
    assert result["needsWalkingAnswer"] is True and result["suggestions"]["walking"] is None


def test_negated_twenty_minute_tolerance_is_not_an_affirmative_competing_bound():
    source = "한 번에 최대 20분까지 걷는 것은 괜찮지 않아요. 도보는 한 번에 최대 40분까지 가능해요."
    assert ground_interpretation(source, interpretation("40")) == interpretation("40")


@pytest.mark.parametrize("source", ["카페에서 20분 쉬고 싶어", "많이 걷기 싫어", "도보 20분이 가능할까요?"])
def test_unfounded_walking_value_cannot_pass_the_adapter(source):
    result = ground_interpretation(source, interpretation())
    assert result["suggestions"]["walking"] is None
    assert result["needsWalkingAnswer"] is True


def test_clear_walk_limit_and_free_text_are_preserved_without_semantic_claim():
    raw = interpretation()
    assert ground_interpretation("한 번에 최대 20분까지 도보 이동 가능해요.", raw) == raw
    assert ground_interpretation("걷는 시간 제한이 없어요.", interpretation("unlimited")) == interpretation("unlimited")


def test_same_start_at_different_places_requires_clarification_for_both_items():
    a, b = appointment(), appointment("b", contentid=2, placeQuery="합성 장소2")
    assert schedule_conflict_fields([a, b]) == {"a": {"date", "time"}, "b": {"date", "time"}}


def test_explicit_interval_conflict_even_if_second_duration_unknown():
    a = appointment(endTime="16:00")
    b = appointment("b", contentid=2, placeQuery="합성 장소2", time="15:30")
    assert set(schedule_conflict_fields([a, b])) == {"a", "b"}
    b["time"] = "16:00"
    assert schedule_conflict_fields([a, b]) == {}


def test_unknown_durations_not_assumed_and_adjacent_hotel_nights_do_not_clash():
    a = appointment(kind="lodging", endDate="2026-12-13")
    b = appointment("b", kind="lodging", contentid=2, placeQuery="합성 숙소2", date="2026-12-13", endDate="2026-12-14")
    assert schedule_conflict_fields([a, b]) == {}
    b["date"] = "2026-12-12"
    assert set(schedule_conflict_fields([a, b])) == {"a", "b"}
    a, b = appointment(time="15:00"), appointment("b", contentid=2, time="15:10")
    assert schedule_conflict_fields([a, b]) == {}


def test_resolved_private_clarification_keeps_existing_dto_and_does_not_choose_booking():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE places(contentid INTEGER,title TEXT,addr1 TEXT)"))
            conn.execute(text("INSERT INTO places VALUES(1,'합성 장소1','주소1'),(2,'합성 장소2','주소2')"))
            original = [appointment(), appointment("b", contentid=2, placeQuery="합성 장소2")]
            resolved, issues = _schedule_clarifications(conn, original)
            assert [i["contentid"] for i in resolved] == [1, 2]
            assert len(issues) == 2
            assert all(set(i) == {"itemId", "fields", "code", "candidates"} for i in issues)
            assert all(i["code"] == "SCHEDULE_ITEM_CLARIFICATION_REQUIRED" for i in issues)
    finally:
        engine.dispose()


def test_known_hard_conflict_across_members_blocks_instead_of_scoring_away():
    group = [pref(food="카페", foodImportance="required"), pref("p2", avoid="카페")]
    result = analyze_preferences(group, {"자연", "카페"})
    assert {i["code"] for i in result["issues"]} == {"REQUIRED_PREFERENCE_UNSATISFIED"}


def test_soft_preference_against_hard_avoid_is_a_tradeoff_not_hard_conflict():
    group = [pref(likes="카페"), pref("p2", avoid="카페")]
    assert analyze_preferences(group, {"자연", "카페"})["issues"] == []


def test_two_owner_shared_bookings_cannot_be_silently_accepted():
    group = [pref(scheduleItems=[appointment(), appointment("b", contentid=2, placeQuery="합성 장소2")])]
    with pytest.raises(ValueError, match="SCHEDULE_ITEM_CLARIFICATION_REQUIRED"):
        group_schedule_anchors(group, {}, {"members": [{"role": "OWNER"}]})


def test_personal_bookings_do_not_become_shared_anchors():
    group = [pref(scheduleItems=[appointment(scope="personal"), appointment("b", scope="personal", contentid=2)])]
    assert group_schedule_anchors(group, {}, {"members": [{"role": "OWNER"}]}) == []


def test_readiness_detects_confirmed_group_conflict_without_reading_unconfirmed_input():
    from app.generation_service import GenerationService
    from test_generation_owner_readiness_sqlite import SQLiteConnectionAdapter
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE trip_members(id TEXT,trip_id TEXT,role TEXT,status TEXT,created_at TEXT)"))
            conn.execute(text("CREATE TABLE trip_preparation_documents(trip_id TEXT,conditions_json TEXT,conditions_version INTEGER)"))
            conn.execute(text("CREATE TABLE member_preparation_documents(trip_id TEXT,member_id TEXT,draft_json TEXT,preference_version INTEGER,confirmed_conditions_version INTEGER,confirmed_preference_version INTEGER,confirmed_at TEXT)"))
            conn.execute(text("INSERT INTO trip_members VALUES('o','t','OWNER','ACTIVE','1'),('g','t','MEMBER','ACTIVE','2'),('u','t','MEMBER','ACTIVE','3')"))
            conn.execute(text("INSERT INTO trip_preparation_documents VALUES('t','{}',1)"))
            for mid, extra in [("o", {"food": "카페", "foodImportance": "required"}), ("g", {"avoid": "카페"})]:
                draft = {k: v for k, v in pref(**extra).items() if k != "memberRef"}
                draft["originalText"] = "비공개 합성 원문"
                conn.execute(text("INSERT INTO member_preparation_documents VALUES('t',:mid,:d,1,1,1,'2026-11-13')"),
                             {"mid": mid, "d": json.dumps(draft, ensure_ascii=False)})
            conn.execute(text("INSERT INTO member_preparation_documents VALUES('t','u','invalid-json',1,NULL,NULL,NULL)"))
        service = GenerationService(SimpleNamespace(), engine, SimpleNamespace(), None, lambda: "cookie", None)
        with engine.connect() as raw:
            conn = SQLiteConnectionAdapter(raw)
            issues = service._confirmed_issues(conn, "t", SimpleNamespace(preferences=[{"preferenceTags": "자연|카페", "vibeTags": ""}]))
            assert issues == ["REQUIRED_PREFERENCE_UNSATISFIED"]
            assert conn.draft_reads == ["o", "g"]
    finally:
        engine.dispose()
