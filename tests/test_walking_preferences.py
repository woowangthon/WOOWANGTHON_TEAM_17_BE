"""Canonical walking-preference resolution cases and generation evidence checks."""
import json
from pathlib import Path

import pytest

from app.confirmed_preferences import analyze_preferences
from app.walking_preferences import resolve_walking_preference


CASES = json.loads((Path(__file__).parent / "fixtures" / "walking_preference_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_canonical_walking_preference_vectors(case):
    assert resolve_walking_preference(case["draft"]) == case["expected"]


def test_required_vague_walk_avoid_is_resolved_only_by_existing_selected_cap():
    base = {"memberRef": "p1", "likes": "자연", "avoid": "오래 걷는것은 싫어요", "avoidImportance": "required",
            "food": None, "foodImportance": "preferred", "walking": "20"}
    result = analyze_preferences([base], {"자연"})
    assert result["issues"] == []
    assert result["predicates"] == [{"memberRef": "p1", "field": "likes", "tag": "자연",
                                     "polarity": "prefer", "scope": "all", "required": False}]

    unresolved = analyze_preferences([{**base, "walking": "unlimited"}], {"자연"})
    assert {("walking", "REQUIRED_PREFERENCE_UNSATISFIED")} <= {
        (item["field"], item["code"]) for item in unresolved["issues"]
    }


def test_mixed_walking_and_other_avoid_is_not_erased_or_claimed_as_supported():
    mixed = {"memberRef": "p1", "likes": "자연", "avoid": "오래 걷는 것 싫어요, 계단 싫어요",
             "avoidImportance": "required", "food": None, "foodImportance": "preferred", "walking": "20"}
    resolved = resolve_walking_preference(mixed)
    assert resolved["remainingAvoid"] == "계단 싫어요"
    analysis = analyze_preferences([mixed], {"자연"})
    assert any(item["field"] == "avoid" and item["code"] == "REQUIRED_PREFERENCE_UNSUPPORTED"
               for item in analysis["issues"])


def test_distance_based_long_walk_avoid_stays_unsupported_and_preserved():
    draft = {"memberRef": "p1", "originalText": "장거리 도보 거리는 2km 이하로 해 주세요.",
             "likes": None, "avoid": "장거리 도보는 피하고 싶어요", "avoidImportance": "required",
             "food": None, "foodImportance": "preferred", "walking": "20"}
    result = resolve_walking_preference(draft)
    assert result["remainingAvoid"] == draft["avoid"]
    assert result["issue"] == "unsupportedScope"


def test_candidate_pipeline_accepts_only_after_walking_clause_is_satisfied():
    from dataclasses import dataclass
    from app.candidate_selection import build_confirmed_candidate_selection

    @dataclass
    class Snapshot:
        conditions: dict
        confirmedPreferences: list
        fingerprint: str = "walking-fixture"

        def as_dict(self):
            return {"conditions": self.conditions, "confirmedPreferences": self.confirmedPreferences,
                    "_identity": {"actorMemberId": "m1", "members": [{"memberId": "m1", "role": "OWNER"}]}}

    class Catalog:
        digest = "synthetic"
        places = [{"contentid": 11, "title": "숲길", "planningRole": "sightseeing_stop",
                   "recommendationUse": "MAIN_DESTINATION", "adminCity": "청주시",
                   "geoValidated": "yes", "mapx": 127.0, "mapy": 36.0}]
        details = [{"contentid": 11, "planningStatusV2": "candidate_needs_live_validation"}]
        preferences = [{"contentid": 11, "preferenceTags": "자연", "vibeTags": ""}]

    pref = {"memberRef": "p1", "likes": "자연", "avoid": "오래 걷는것은 싫어요", "avoidImportance": "required",
            "food": None, "foodImportance": "preferred", "walking": "20", "scheduleItems": []}
    snapshot = Snapshot({"region": "청주시", "startDate": "2026-12-12", "endDate": "2026-12-12",
                         "lodging": None, "fixedSchedule": None}, [pref])
    analysis = analyze_preferences([pref], {"자연"})
    result = build_confirmed_candidate_selection(snapshot, Catalog(), analysis)
    assert result.ready_for_draft
    assert result.as_dict()["eligible"][0]["contentid"] == 11

    blocked = {**pref, "walking": "unlimited"}
    denied = build_confirmed_candidate_selection(Snapshot(snapshot.conditions, [blocked]), Catalog(),
                                                 analyze_preferences([blocked], {"자연"}))
    assert not denied.ready_for_draft
    assert "REQUIRED_PREFERENCE_UNSATISFIED" in denied.as_dict()["unresolvedRequired"]


def test_source_readiness_checks_raw_walking_evidence_for_every_confirmed_member():
    from types import SimpleNamespace
    from sqlalchemy import create_engine, text
    from app.generation_service import GenerationService

    engine = create_engine("sqlite://")
    source = "도보 구간은 최대 20분 이내로 해 주세요. 오래 걷는 것은 싫어요."
    owner = {"originalText": source, "likes": "자연", "avoid": "오래 걷는 것은 싫어요",
             "avoidImportance": "required", "food": None, "foodImportance": "preferred",
             "walking": "40", "scheduleItems": []}
    member = {**owner, "likes": "카페"}
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE trip_members (id TEXT, trip_id TEXT, nickname TEXT, role TEXT, status TEXT, created_at TEXT)"))
        conn.execute(text("CREATE TABLE member_preparation_documents (trip_id TEXT, member_id TEXT, draft_json TEXT, preference_version INTEGER, confirmed_conditions_version INTEGER, confirmed_preference_version INTEGER, confirmed_at TEXT)"))
        conn.execute(text("INSERT INTO trip_members VALUES ('owner','trip','방장','OWNER','ACTIVE','1'), ('member','trip','참여자','MEMBER','ACTIVE','2')"))
        for member_id, draft in (("owner", owner), ("member", member)):
            conn.execute(text("INSERT INTO member_preparation_documents VALUES ('trip',:m,:d,1,1,1,'2026-11-14T00:00:00Z')"),
                          {"m": member_id, "d": json.dumps(draft, ensure_ascii=False)})

    class LocklessConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, params=None):
            return self.conn.execute(text(str(statement).replace(" FOR UPDATE", "")), params or {})

    service = GenerationService(SimpleNamespace(), engine, SimpleNamespace(), None, lambda: "cookie", None)
    try:
        with engine.connect() as conn:
            token, statuses, personal = service._source_token(LocklessConnection(conn), "trip", 1, "catalog", 0)
        assert token and [member["requiredReady"] for member in personal] == [False, False]
        assert [member["status"] for member in statuses] == ["confirmed", "confirmed"]
        assert source not in repr(personal)
    finally:
        engine.dispose()
