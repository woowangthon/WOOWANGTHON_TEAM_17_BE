"""Synthetic, DB-free tests for review-gated candidate selection."""
from dataclasses import dataclass

import pytest

from app.candidate_selection import (
    CandidateSelectionError,
    _tokens_region,
    build_candidate_selection,
    build_supported_tags,
    normalization_digest,
)


@dataclass
class Snapshot:
    conditions: dict
    confirmedPreferences: list
    fingerprint: str = "source-1"

    def as_dict(self):
        return {"conditions": self.conditions, "confirmedPreferences": self.confirmedPreferences}


class Catalog:
    digest = "catalog-1"

    def __init__(self, places, details=None, preferences=None):
        self.places = places
        self.details = details or []
        self.preferences = preferences or []


def source(*, avoid="무서운 놀이", region="청주시", lodging=None, fixed=None):
    return Snapshot(
        {"region": region, "startDate": "2026-12-12", "endDate": "2026-12-13",
         "lodging": lodging, "fixedSchedule": fixed},
        [{"memberRef": "p1", "likes": "자연 산책", "avoid": avoid,
          "avoidImportance": "required", "food": None, "foodImportance": "preferred", "walking": "20"}],
    )


def normalized(*, term_tag="자연", evidence="놀이", field="avoid", polarity="avoid", ref="p1", walking=None):
    terms = [] if field is None else [{"memberRef": ref, "field": field, "tag": term_tag, "evidence": evidence,
                                       "polarity": polarity, "importance": "required" if field == "avoid" else "preferred", "requiresReview": True}]
    return {
        "terms": terms,
        "unmapped": [{"memberRef": "p1", "field": fld, "text": text, "importance": "preferred" if fld == "likes" else "required" if fld == "avoid" else "preferred", "reason": "coverage_unverified" if field == fld else "uncovered"}
                     for fld, text in (("likes", "자연 산책"), ("avoid", "무서운 놀이")) if text and (field is None or field != fld or field == fld)],
        "walking": walking or [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20, "unlimited": False}],
        "conflictCandidates": [], "needsReview": True,
    }


def place(cid, *, role="sightseeing_stop", use="MAIN_DESTINATION", city="청주시", status="candidate_needs_live_validation", coord=True):
    return {"contentid": cid, "title": f"장소 {cid}", "planningRole": role, "recommendationUse": use,
            "adminCity": city, "geoValidated": "yes" if coord else "no", "mapx": 127.0, "mapy": 36.0}


def review(snapshot, norm, *, accepted=None, scope="all", common=(), absences=()):
    accepted = tuple(range(len(norm["terms"]))) if accepted is None else accepted
    fld = norm["terms"][0]["field"] if norm["terms"] else "avoid"
    txt = next(p[fld] for p in snapshot.confirmedPreferences if p["memberRef"] == "p1")
    return {"sourceFingerprint": snapshot.fingerprint, "normalizationDigest": normalization_digest(norm),
            "catalogDigest": "catalog-1", "acceptedTermIndexes": list(accepted),
            "reviewedFields": [{"memberRef": "p1", "field": fld, "text": txt,
                                "termIndexes": list(accepted), "scope": scope}] if accepted else [],
            "verifiedTagAbsences": list(absences), "reviewedCommonFields": list(common)}


def build(snap=None, norm=None, cat=None, rev=None, policy=None):
    snap = snap or source()
    norm = norm or normalized()
    cat = cat or Catalog([place(1)], preferences=[{"contentid": 1, "preferenceTags": "자연"}])
    norm = dict(norm)
    prefs = snap.confirmedPreferences
    norm["unmapped"] = [{"memberRef": p["memberRef"], "field": fld, "text": p[fld],
                         "importance": "preferred" if fld == "likes" else p[fld + "Importance"],
                         "reason": "coverage_unverified" if any(t["memberRef"] == p["memberRef"] and t["field"] == fld for t in norm["terms"]) else "uncovered"}
                        for p in prefs for fld in ("likes", "avoid", "food") if p.get(fld)]
    if rev is not None:
        rev = dict(rev, normalizationDigest=normalization_digest(norm))
    return build_candidate_selection(snap, norm, cat, rev, policy)


def test_hard_violation_excludes_even_if_other_member_scores_would_be_high():
    snap = source()
    norm = normalized()
    cat = Catalog([place(1), place(2)], preferences=[
        {"contentid": 1, "preferenceTags": "자연"}, {"contentid": 2, "preferenceTags": ""}])
    # Explicitly reviewed presence of the forbidden tag is a hard violation.
    result = build(snap, norm, cat, review(snap, norm))
    assert result.as_dict()["eligible"] == []
    assert result.as_dict()["excluded"][0]["reason"] == "REQUIRED_PREDICATE_VIOLATED"


def test_unknown_required_absence_does_not_become_safe_without_review_fact():
    snap = source()
    norm = normalized()
    cat = Catalog([place(1), place(2, city="충주시")], preferences=[{"contentid": 1, "preferenceTags": ""}, {"contentid": 2, "preferenceTags": "자연"}])
    result = build(snap, norm, cat, review(snap, norm))
    assert result.as_dict()["eligible"] == []
    assert {x["reason"] for x in result.as_dict()["reviewCandidates"]} >= {"REQUIRED_EVIDENCE_UNKNOWN"}
    reviewed = review(snap, norm, absences=[{"contentid": 1, "tag": "자연"}])
    assert build(snap, norm, cat, reviewed).as_dict()["eligible"][0]["contentid"] == 1


def test_missing_review_blocks_required_filters_and_digest_is_bound():
    snap, norm = source(), normalized()
    result = build(snap, norm)
    assert not result.ready_for_draft
    assert "REQUIRED_TERM_REVIEW_REQUIRED" in result.as_dict()["unresolvedRequired"]
    stale = review(snap, norm) | {"catalogDigest": "other"}
    with pytest.raises(CandidateSelectionError, match="STALE_REVIEW"):
        build(snap, norm, rev=stale)


def test_forged_or_stale_normalization_term_is_rejected():
    snap = source()
    norm = normalized(evidence="다른 문장")
    with pytest.raises(CandidateSelectionError, match="INVALID_NORMALIZATION_RESULT"):
        build(snap, norm, rev=review(snap, norm))
    norm = normalized(ref="p999")
    with pytest.raises(CandidateSelectionError, match="INVALID_NORMALIZATION_RESULT"):
        build(snap, norm)
    valid = normalized()
    stale_source = review(snap, valid) | {"sourceFingerprint": "other"}
    with pytest.raises(CandidateSelectionError, match="STALE_REVIEW"):
        build(snap, valid, rev=stale_source)


def test_tags_are_exact_tokens_and_duplicates_do_not_inflate_scores():
    cat = Catalog([], preferences=[{"contentid": 1, "preferenceTags": "자연| 자연 |자연스럽게", "vibeTags": "조용한"}])
    assert build_supported_tags(cat) == ("자연", "자연스럽게", "조용한")
    snap = source(avoid=None)
    norm = normalized(field=None)
    norm["terms"] = [{"memberRef": "p1", "field": "likes", "tag": "자연", "evidence": "자연",
                      "polarity": "prefer", "importance": "preferred", "requiresReview": True}]
    norm["unmapped"] = [{"memberRef": "p1", "field": "likes", "text": "자연 산책", "importance": "preferred", "reason": "coverage_unverified"}]
    norm["walking"] = [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20, "unlimited": False}]
    cat = Catalog([place(2), place(1)], preferences=[
        {"contentid": 1, "preferenceTags": "자연|자연|자연"}, {"contentid": 2, "preferenceTags": "자연"}])
    rev = {"sourceFingerprint": snap.fingerprint, "normalizationDigest": normalization_digest(norm), "catalogDigest": cat.digest,
           "acceptedTermIndexes": [0], "reviewedFields": [{"memberRef": "p1", "field": "likes", "text": "자연 산책", "termIndexes": [0], "scope": "all"}],
           "verifiedTagAbsences": [], "reviewedCommonFields": []}
    rows = build(snap, norm, cat, rev).as_dict()["eligible"]
    assert len(rows) == 2
    assert rows[0]["groupScore"] == rows[1]["groupScore"] == 1.0
    assert [x["contentid"] for x in rows] == [1, 2]


def test_substring_tag_does_not_satisfy_a_required_predicate():
    snap, norm = source(), normalized(term_tag="자연", evidence="놀이")
    cat = Catalog([place(1), place(2, city="충주시")], preferences=[
        {"contentid": 1, "preferenceTags": "자연스럽게"}, {"contentid": 2, "preferenceTags": "자연"}])
    result = build(snap, norm, cat, review(snap, norm))
    assert not result.as_dict()["eligible"]
    assert any(x["contentid"] == 1 and x["reason"] == "REQUIRED_EVIDENCE_UNKNOWN" for x in result.as_dict()["reviewCandidates"])


def test_row_permutation_does_not_change_selection_and_scores_are_equal_member_weight():
    snap = source(avoid=None)
    norm = normalized()
    norm["terms"] = [
        {"memberRef": "p1", "field": "likes", "tag": "자연", "evidence": "자연", "polarity": "prefer", "importance": "preferred", "requiresReview": True},
    ]
    norm["unmapped"] = [{"memberRef": "p1", "field": "likes", "text": "자연 산책", "importance": "preferred", "reason": "coverage_unverified"}]
    norm["walking"] = [{"memberRef": "p1", "scope": "between_places", "maxMinutes": 20, "unlimited": False}]
    # Include another neutral member; duplicated catalog evidence must not
    # become an extra scoring dimension or alter deterministic ordering.
    snap.confirmedPreferences.append({"memberRef": "p2", "likes": "문화", "avoid": None, "avoidImportance": "required", "food": None, "foodImportance": "preferred", "walking": "40"})
    norm["walking"].append({"memberRef": "p2", "scope": "between_places", "maxMinutes": 40, "unlimited": False})
    cat_a = Catalog([place(3), place(1)], preferences=[{"contentid": 1, "preferenceTags": "자연"}, {"contentid": 3, "preferenceTags": ""}])
    cat_b = Catalog(list(reversed(cat_a.places)), list(reversed(cat_a.details)), list(reversed(cat_a.preferences)))
    rv = {"sourceFingerprint": snap.fingerprint, "normalizationDigest": normalization_digest(norm), "catalogDigest": "catalog-1", "acceptedTermIndexes": [0],
          "reviewedFields": [{"memberRef": "p1", "field": "likes", "text": "자연 산책", "termIndexes": [0], "scope": "all"}], "verifiedTagAbsences": [], "reviewedCommonFields": []}
    a = build(snap, norm, cat_a, rv).as_dict()
    b = build(snap, norm, cat_b, rv).as_dict()
    assert a == b
    assert a["eligible"][0]["groupScore"] == 0.5


def test_unresolved_region_does_not_use_substring_geography():
    snap, norm = source(region="충북 좋아요") , normalized(field=None)
    result = build(snap, norm, Catalog([place(1)], preferences=[]), review(snap, norm))
    assert "REGION_REVIEW_REQUIRED" in result.as_dict()["unresolvedRequired"]
    assert not result.ready_for_draft


def test_nullable_metadata_coordinates_and_event_window_are_not_assumed():
    snap, norm = source(avoid=None), normalized(field=None)
    cat = Catalog([place(1, coord=False), place(2, role="date_specific_event", use="EVENT")],
                  details=[{"contentid": 1, "planningStatusV2": "candidate_needs_live_validation"},
                           {"contentid": 2, "planningStatusV2": "trip_date_check_required", "eventStartDate": "20270101", "eventEndDate": "20270102"}],
                  preferences=[])
    result = build(snap, norm, cat)
    reasons = {x["contentid"]: x["reason"] for x in result.as_dict()["reviewCandidates"]}
    assert reasons[1] == "COORDINATE_REVIEW_REQUIRED"
    assert result.as_dict()["excluded"][0]["reason"] == "EVENT_OUTSIDE_TRIP"
    assert "LIVE_PLACE_VALIDATION_REQUIRED" in result.as_dict()["pendingValidation"]


def test_fixed_common_text_needs_exact_reviewed_anchor_and_detached_accessors():
    snap, norm = source(lodging="숙소는 청주", avoid=None), normalized(field=None)
    cat = Catalog([place(1)], preferences=[])
    result = build(snap, norm, cat, review(snap, norm))
    assert "COMMON_ANCHOR_REVIEW_REQUIRED" in result.as_dict()["unresolvedRequired"]
    fixed = {"field": "lodging", "text": "숙소는 청주", "fixedVisits": [{"date": "2026-12-12", "contentid": 1, "position": 0}]}
    result = build(snap, norm, cat, review(snap, norm, common=[fixed]))
    exported = result.as_dict(); exported["eligible"].clear()
    assert result.as_dict()["eligible"]
    assert result.fixed_visits() == fixed["fixedVisits"]
    assert "숙소는 청주" not in repr(result)


def test_region_parser_rejects_province_and_whole_tokens_out_of_position():
    assert _tokens_region("청주충북") == (set(), False)
    assert _tokens_region("전체 청주") == (set(), False)
    assert _tokens_region("충북 전체 청주") == (set(), False)
    assert _tokens_region("충북 증평") == ({"증평군"}, True)


def test_low_ranked_fixed_anchor_is_reserved_inside_bounded_candidate_pool():
    snap, norm = source(avoid=None), normalized(field=None)
    cat = Catalog([place(1), place(2)], preferences=[])
    snap.conditions["fixedSchedule"] = "꼭 들를 곳"
    common = {"field": "fixedSchedule", "text": "꼭 들를 곳",
              "fixedVisits": [{"date": "2026-12-12", "contentid": 2, "position": 0}]}
    result = build(snap, norm, cat, review(snap, norm, common=[common]),
                   {"version": "candidate-v1", "maxCandidates": 1, "reviewedRegionCities": []})
    assert [x["contentid"] for x in result.as_dict()["eligible"]] == [2]
    assert result.as_dict()["omittedCount"] == 1


def test_anchor_missing_from_eligible_pool_blocks_draft_readiness():
    snap, norm = source(avoid=None), normalized(field=None)
    snap.conditions["fixedSchedule"] = "꼭 들를 곳"
    common = {"field": "fixedSchedule", "text": "꼭 들를 곳",
              "fixedVisits": [{"date": "2026-12-12", "contentid": 9, "position": 0}]}
    result = build(snap, norm, Catalog([place(1, coord=False)]), review(snap, norm, common=[common]))
    assert "FIXED_VISIT_NOT_ELIGIBLE" in result.as_dict()["unresolvedRequired"]
    assert not result.ready_for_draft
