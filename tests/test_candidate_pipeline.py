"""Real selection + AI SDK pipeline on synthetic SQLite catalog fixtures."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.candidate_selection import CandidateSelectionError, normalization_digest
from app.generation_draft import GenerationDraftError, generate_candidate_draft
from app.generation_foundation import ConfirmedPreparationSnapshot
from app.place_catalog import load_place_catalog


@pytest.fixture
def pipeline():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    places = Table("places", metadata, Column("contentid", Integer), Column("title", String),
                   Column("planningRole", String), Column("recommendationUse", String),
                   Column("adminCity", String), Column("mapx", String), Column("mapy", String),
                   Column("geoValidated", String))
    details = Table("place_details", metadata, Column("contentid", Integer), Column("planningStatusV2", String),
                    Column("feeText", String), Column("openingHoursText", String))
    prefs_table = Table("place_preferences", metadata, Column("contentid", Integer),
                        Column("preferenceTags", String), Column("vibeTags", String))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(places.insert(), [{"contentid": cid, "title": f"합성 장소{cid}",
                      "planningRole": "sightseeing_stop", "recommendationUse": "MAIN_DESTINATION",
                      "adminCity": "단양군", "mapx": "127.4341", "mapy": "36.6286", "geoValidated": "yes"}
                     for cid in (11, 22)])
        conn.execute(details.insert(), [{"contentid": cid, "planningStatusV2": "candidate_needs_live_validation",
                                         "feeText": None, "openingHoursText": None} for cid in (11, 22)])
        conn.execute(prefs_table.insert(), [{"contentid": cid, "preferenceTags": "자연", "vibeTags": "조용한"}
                                           for cid in (11, 22)])
        catalog = load_place_catalog(conn)
    preferences = [{"memberRef": "p1", "likes": "자연", "avoid": None, "avoidImportance": "required",
                    "food": None, "foodImportance": "preferred", "walking": "20"}]
    source = {"conditions": {"region": "충북 단양", "startDate": "2026-12-12", "endDate": "2026-12-13",
                              "expectedPeople": 1, "budget": 100000, "transport": "car", "departure": "청주역",
                              "returnRoute": "청주역", "lodging": None, "fixedSchedule": None},
              "confirmedPreferences": preferences}
    document = json.dumps(source, ensure_ascii=False, sort_keys=True)
    snapshot = ConfirmedPreparationSnapshot(document, hashlib.sha256(document.encode()).hexdigest())
    normalization = {"terms": [{"memberRef": "p1", "field": "likes", "tag": "자연", "evidence": "자연",
                                 "polarity": "prefer", "importance": "preferred", "requiresReview": True}],
                     "unmapped": [{"memberRef": "p1", "field": "likes", "text": "자연",
                                   "importance": "preferred", "reason": "coverage_unverified"}],
                     "walking": [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20,
                                   "unlimited": False}], "conflictCandidates": [], "needsReview": True}
    review = {"sourceFingerprint": snapshot.fingerprint, "normalizationDigest": normalization_digest(normalization),
              "catalogDigest": catalog.digest, "acceptedTermIndexes": [0],
              "reviewedFields": [{"memberRef": "p1", "field": "likes", "text": "자연", "termIndexes": [0], "scope": "all"}],
              "verifiedTagAbsences": [], "reviewedCommonFields": []}
    try:
        yield snapshot, normalization, catalog, review
    finally:
        engine.dispose()


def test_full_candidate_pipeline_with_real_sdk_and_fake_https(pipeline, monkeypatch):
    from chungbuk_trip_ai import scheduling
    from chungbuk_trip_ai.gemini import GeminiSettings

    snapshot, normalization, catalog, review = pipeline
    raw = {"days": [{"date": "2026-12-12", "contentids": [11]}, {"date": "2026-12-13", "contentids": [22]}]}
    body = json.dumps({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": json.dumps(raw)}]}}]}).encode()
    observed = []

    class FakeHTTP:
        def __init__(self, *_, **__):
            pass

        def request(self, *_, **kwargs):
            observed.append(json.loads(kwargs["body"]))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: body)

        def close(self):
            pass

    monkeypatch.setattr(scheduling, "HTTPSConnection", FakeHTTP)
    monkeypatch.setattr(scheduling, "load_gemini_settings", lambda: GeminiSettings("not-a-real-key"))
    result = asyncio.run(generate_candidate_draft(snapshot, normalization, catalog, review,
                                                  lambda: snapshot, lambda: catalog)).as_dict()
    assert [p["contentid"] for day in result["days"] for p in day["places"]] == [11, 22]
    assert result["days"][0]["places"][0]["feeLabel"] == "요금 확인 필요"
    assert result["days"][0]["places"][0]["coordinate"]["longitude"] == 127.4341
    assert all(value is False for value in result["validation"].values())
    wire = json.dumps(observed, ensure_ascii=False)
    assert "memberRef" not in wire and "evidence" not in wire and "originalText" not in wire
    assert "sourceFingerprint" not in wire and "memberScores" not in wire
    assert "categoryKey" not in wire
    assert all(item["categoryKey"] == "planningRole:sightseeing" for item in result["optimizationCandidates"])
    assert all("contenttypeid" not in item and "contentTypeName" not in item
               for item in result["optimizationCandidates"])


def test_review_bound_to_previous_catalog_or_unreviewed_source_is_not_adopted(pipeline):
    snapshot, normalization, catalog, review = pipeline
    with pytest.raises(CandidateSelectionError, match="STALE_REVIEW"):
        asyncio.run(generate_candidate_draft(snapshot, normalization, catalog, dict(review, catalogDigest="stale"),
                    lambda: snapshot, lambda: catalog, drafter=lambda *_: pytest.fail("must not call AI")))
