import pytest

from app.generation_reviews import bind_private_review, normalization_dto, validate_review
from preparation_helpers import base, create_room, headers


def _stored():
    return {"id": "norm-1", "sourceFingerprint": "private-source-hash", "catalogDigest": "private-catalog-hash",
            "normalization": {"terms": [
                {"memberRef": "p1", "field": "likes", "tag": "nature", "evidence": "산책",
                 "polarity": "prefer", "importance": "preferred", "requiresReview": True}],
                "unmapped": [{"memberRef": "p1", "field": "likes", "text": "산책", "importance": "preferred", "reason": "coverage_unverified"}],
                "walking": [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20, "unlimited": False}],
                "conflictCandidates": [], "needsReview": True}}


def test_review_validation_binds_only_current_term_ids_and_full_field_decision():
    stored = _stored()
    review = validate_review({"normalizationId": "norm-1", "expectedReviewVersion": 0,
                              "acceptedTermIds": ["t0"],
                              "reviewedFields": [{"field": "likes", "termIds": ["t0"], "scope": "all", "fullyReviewed": True}]}, stored, 0)
    assert review["ready"] is True
    assert review["acceptedTermIds"] == ["t0"]


def test_review_rejects_unowned_evidence_and_stale_version():
    stored = _stored()
    with pytest.raises(ValueError, match="VALIDATION_ERROR"):
        validate_review({"normalizationId": "norm-1", "expectedReviewVersion": 0,
                         "acceptedTermIds": ["t99"], "reviewedFields": []}, stored, 0)
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        validate_review({"normalizationId": "norm-1", "expectedReviewVersion": 2,
                         "acceptedTermIds": [], "reviewedFields": []}, stored, 0)


def test_public_normalization_dto_omits_internal_source_hashes_and_member_refs():
    stored = _stored()
    dto = normalization_dto(stored, {"acceptedTermIds": ["t0"], "reviewedFields": [],
                                     "sourceFingerprint": "private-source-hash", "ready": True}, 1)
    text = str(dto)
    assert "private-source-hash" not in text
    assert "private-catalog-hash" not in text
    assert "memberRef" not in text


def test_group_review_projection_uses_server_bound_source_and_empty_absence_claims():
    stored = _stored()
    review = {"acceptedTermIds": ["t0"], "reviewedFields": [{"field": "likes", "termIds": ["t0"],
                "scope": "all", "fullyReviewed": True}]}
    bound = bind_private_review(stored, review, "bound-source", "bound-catalog")
    assert bound["sourceFingerprint"] == "bound-source"
    assert bound["catalogDigest"] == "bound-catalog"
    assert bound["acceptedTermIndexes"] == [0]
    assert bound["verifiedTagAbsences"] == []


@pytest.mark.parametrize("error_name,expected", [
    ("GeminiTimeoutError", "AI_TIMEOUT"), ("GeminiUnavailableError", "AI_UNAVAILABLE"),
    ("GeminiInvalidResponseError", "AI_INVALID_RESPONSE"),
])
def test_normalizer_classifies_public_provider_errors_without_details(monkeypatch, error_name, expected):
    import chungbuk_trip_ai as ai
    from app.generation_service import GenerationService
    from app.generation_foundation import GenerationFoundationError
    def unavailable(**kwargs):
        raise getattr(ai, error_name)("synthetic provider detail that must not escape")
    monkeypatch.setattr(ai, "normalize_confirmed_preferences", unavailable)
    with pytest.raises(GenerationFoundationError) as exc:
        GenerationService._default_normalizer([], [])
    assert exc.value.code == expected and "provider detail" not in str(exc.value)


@pytest.mark.mysql
def test_generation_job_requires_all_current_private_reviews(client):
    room = create_room(client)
    state = client.get(base(room) + "/generation").json()
    response = client.post(base(room) + "/generation/jobs", headers=headers(), json={
        "expectedInputToken": state["inputToken"], "expectedSettingsVersion": state["settingsVersion"]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_REQUIRED"
