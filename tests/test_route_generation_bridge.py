"""Synthetic source-to-drafter integration for the atomic route handoff."""
import asyncio
import hashlib
import json

from chungbuk_trip_ai import build_atomic_preferences

from app.generation_draft import generate_candidate_draft
from app.generation_foundation import ConfirmedPreparationSnapshot
from app.group_preferences import build_group_preferences
from test_candidate_pipeline import pipeline


def make_profile():
    text = "자연은 여행 중 꼭 한 번 가고 싶어. 하루에 세 곳 정도면 좋아."
    atoms = [
        {"dimension": "activity", "target": "자연", "polarity": "prefer",
         "priority": "required", "priorityBasis": "explicit", "scope": "trip",
         "audience": "personal", "frequency": {"operator": "min", "count": 1, "period": "trip"},
         "value": None, "unit": None, "approximate": False,
         "evidence": "자연은 여행 중 꼭 한 번 가고 싶어"},
        {"dimension": "visit_count", "target": "관광 장소", "polarity": "limit",
         "priority": "preferred", "priorityBasis": "unspecified", "scope": "day",
         "audience": "personal", "frequency": {"operator": "target", "count": 3, "period": "day"},
         "value": None, "unit": None, "approximate": True,
         "evidence": "하루에 세 곳 정도면 좋아"},
    ]
    return build_atomic_preferences(atoms, [], text)


def test_atomic_snapshot_reaches_normal_drafter_as_private_id_only_rules(pipeline):
    legacy_snapshot, _, catalog, _ = pipeline
    doc = legacy_snapshot.as_dict()
    doc["atomicMemberRefs"] = ["p1"]
    doc["groupConditions"] = build_group_preferences([
        {"memberRef": "p1", "confirmed": True, "atomicPreferences": make_profile()}
    ])
    # A v1 fallback projection is deliberately unsupported/hard. The current
    # atomic profile is the authority and must not re-harden this projection.
    doc["confirmedPreferences"][0].update(
        food="지원되지 않는 옛 음식 표현", foodImportance="required",
        avoid=None, walking=None,
    )
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    snapshot = ConfirmedPreparationSnapshot(encoded, hashlib.sha256(encoded.encode()).hexdigest())
    observed = {}

    def drafter(dates, candidates, fixed, maximum, schedule_context):
        observed.update(schedule_context)
        return {"days": [{"date": dates[0], "contentids": [11]}, {"date": dates[1], "contentids": [22]}]}

    result = asyncio.run(generate_candidate_draft(
        snapshot, None, catalog, None, lambda: snapshot, lambda: catalog, drafter=drafter,
    ))
    assert result.as_dict()["days"][0]["places"][0]["contentid"] == 11
    assert observed["preferenceRules"]
    assert any(rule["period"] == "trip" and rule["required"] and rule["count"] == 1
               for rule in observed["preferenceRules"])
    assert any(rule["period"] == "day" and rule["operator"] == "target" and rule["count"] == 3
               and rule["required"] is False for rule in observed["preferenceRules"])
    serialized = json.dumps(observed, ensure_ascii=False)
    for private in ("memberRef", "evidence", "atomicPreferences", "지원되지 않는 옛 음식 표현"):
        assert private not in serialized
    assert all(set(rule) == {"contentids", "period", "operator", "count", "required"}
               for rule in observed["preferenceRules"])


def test_continuous_walking_marker_never_generalizes_a_named_day():
    def atom(evidence, scope="segment"):
        return {
            "dimension": "walking", "target": "도보", "polarity": "limit", "priority": "required",
            "priorityBasis": "explicit", "scope": scope, "audience": "personal", "frequency": None,
            "value": 15, "unit": "분", "approximate": False, "evidence": evidence,
        }

    for evidence, expected in [
        ("연속 15분까지만 걷기는 꼭 지켜줘", "explicit_segment"),
        ("둘째 날은 연속 15분까지만 걷기는 꼭 지켜줘", None),
    ]:
        profile = build_atomic_preferences([atom(evidence)], [], evidence)
        group = build_group_preferences([{"memberRef": "p1", "confirmed": True, "atomicPreferences": profile}])
        assert group["memberScopedConstraints"][0].get("walkingScope") == expected
        assert evidence not in json.dumps(group, ensure_ascii=False)

class JobConnection:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, statement, params=None):
        from types import SimpleNamespace
        self.statements.append(str(statement))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.row))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_generation_job_stops_before_persistence_when_hard_condition_is_unverified(pipeline, monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    import uuid
    import pytest
    from app import day_schedule, generation_draft, place_catalog, place_semantics, plan_store
    from app.generation_service import GenerationService, ServiceError

    snapshot, _, catalog, _ = pipeline
    doc = snapshot.as_dict()
    doc["conditionsVersion"] = 1
    doc["atomicMemberRefs"] = ["p1"]
    doc["groupConditions"] = build_group_preferences([
        {"memberRef": "p1", "confirmed": True, "atomicPreferences": make_profile()}
    ])
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    snapshot = ConfirmedPreparationSnapshot(encoded, "route-source")
    row = {
        "trip_id": str(uuid.uuid4()), "member_id": "owner", "lease_token": "lease",
        "lease_expires_at": datetime.utcnow() + timedelta(minutes=5),
        "input_stamp_json": json.dumps({"inputToken": "input-token", "catalogDigest": catalog.digest}),
    }
    conn = JobConnection(row)
    state = SimpleNamespace(generation_job_timeout=180, generation_drafter=None, generation_router=None)
    service = GenerationService(SimpleNamespace(state=state), None, None, None, None, None)
    service._conn = lambda: conn
    service._auth = lambda *a, **kw: ({}, {}, {"id": "owner"})
    service._snapshot = lambda *a: snapshot
    service._settings = lambda *a: (1, {"activityWindow": {"start": "09:00", "end": "20:00"},
                                       "reviewedCommonFields": []})
    service._source_token = lambda *a: ("input-token", [], [])
    service._confirmed_issues = lambda *a: []
    monkeypatch.setattr(place_catalog, "load_place_catalog", lambda *a, **kw: catalog)

    async def drafter(*args, **kwargs):
        return SimpleNamespace(as_dict=lambda: {"days": [
            {"date": "2026-12-12", "places": [catalog.presentation_place(11)]},
            {"date": "2026-12-13", "places": [catalog.presentation_place(22)]},
        ], "optimizationCandidates": []})

    monkeypatch.setattr(generation_draft, "generate_candidate_draft", drafter)
    monkeypatch.setattr(place_semantics, "build_place_semantics", lambda *a, **kw: None)
    monkeypatch.setattr(day_schedule, "build_scheduled_plan", lambda *a, **kw: {
        "validation": {"violations": [], "warnings": ["REQUIRED_PREFERENCE_UNVERIFIED"]}
    })
    stored = []
    monkeypatch.setattr(plan_store, "create_generated_plan", lambda *a, **kw: stored.append(True))
    with pytest.raises(ServiceError) as exc:
        service._run_job_sync(str(uuid.uuid4()), object(), "lease")
    assert exc.value.code == "REQUIRED_PREFERENCE_UNSATISFIED"
    assert not stored
    assert not any("phase='persisting'" in sql for sql in conn.statements)
