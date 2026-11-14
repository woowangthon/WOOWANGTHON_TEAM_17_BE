from app.ai_evaluation import (
    aggregate_interpretation,
    load_corpus,
    offline_report,
    score_interpretation,
    synthetic_route_score,
)


def test_frozen_corpus_has_five_named_synthetic_people_and_conflict_cases():
    corpus, digest = load_corpus()
    assert len(digest) == 64
    assert [x["label"] for x in corpus["primaryCases"]] == [f"사용자 {i}" for i in range(1, 6)]
    assert corpus["sampleType"] == "synthetic"
    ids = {x["id"] for x in corpus["challengeCases"]}
    assert {"same-user-hard-conflict", "group-hard-conflict", "soft-vs-hard",
            "overlapping-bookings", "ambiguous-walk-limit", "contradictory-walking-limits", "wish-or-cancelled",
            "missing-appointment-fields", "infeasible-schedule", "budget-and-unknown-facts"} <= ids


def test_interpretation_scorer_counts_missed_and_invented_atoms():
    case = {"id": "synthetic-case", "gold": {"likes": ["자연"], "avoid": ["등산"], "walking": "20"}}
    score = score_interpretation(case, {"suggestions": {"likes": "문화", "avoid": "등산", "walking": "40", "scheduleItems": []}, "needsWalkingAnswer": False})
    assert score["truePositive"] == 1
    assert score["falsePositive"] == 1
    assert score["falseNegative"] == 1
    assert score["walkingExpected"] == "20"
    assert score["walkingPredicted"] == "40"
    assert score["expectedAtoms"] == ["avoid:등산", "likes:자연"]


def test_reversing_likes_and_avoid_does_not_pass_the_gold_rubric():
    case = {"id": "polarity", "gold": {"likes": ["자연"], "avoid": ["등산"]}}
    result = {"suggestions": {"likes": "등산", "avoid": "자연", "food": None, "walking": None, "scheduleItems": []}, "needsWalkingAnswer": True}
    score = score_interpretation(case, result)
    assert score["truePositive"] == 0
    assert score["falsePositive"] == 2
    assert score["falseNegative"] == 2


def test_confirmed_appointment_is_scored_separately_from_interest_tags():
    case = {"id": "appointment", "gold": {"likes": [], "avoid": [], "appointment": {"time": "15:00"}}}
    missed = score_interpretation(case, {"suggestions": {"likes": None, "avoid": None, "walking": None, "scheduleItems": []}, "needsWalkingAnswer": True})
    hit = score_interpretation(case, {"suggestions": {"likes": None, "avoid": None, "walking": None, "scheduleItems": [{"time": "15:00"}]}, "needsWalkingAnswer": True})
    assert missed["appointmentFalseNegative"] == 1
    assert hit["appointmentTruePositive"] == 1
    assert hit["truePositive"] == 0


def test_unknown_appointment_facts_are_not_scored_as_correct_when_absent_from_gold():
    case = {"id": "missing-facts", "gold": {"appointments": [{"date": None, "time": None, "place": None, "scope": None}]}}
    omitted = score_interpretation(case, {"suggestions": {"scheduleItems": []}, "needsWalkingAnswer": True})
    invented = score_interpretation(case, {"suggestions": {"scheduleItems": [{"date": "2026-12-12", "time": "10:00", "placeQuery": "임의 장소"}]}, "needsWalkingAnswer": True})
    assert omitted["appointmentExpectedFields"] == 0
    assert omitted["appointmentFalseNegative"] == 0
    assert invented["appointmentHallucinatedUnknownFields"] == 3


def test_plural_fixed_appointments_compare_each_place_and_time():
    case = {"id": "two-bookings", "gold": {"appointments": [
        {"date": "2026-12-12", "time": "14:00", "place": "청주 호텔"},
        {"date": "2026-12-12", "time": "14:00", "place": "단양 별장"},
    ]}}
    result = {"suggestions": {"scheduleItems": [
        {"date": "2026-12-12", "time": "14:00", "placeQuery": "청주 호텔"},
        {"date": "2026-12-12", "time": "14:00", "placeQuery": "단양 별장"},
    ]}, "needsWalkingAnswer": True}
    score = score_interpretation(case, result)
    assert score["appointmentExpectedFields"] == 6
    assert score["appointmentMatchedFields"] == 6
    assert score["appointmentFalseNegative"] == 0


def test_failed_model_calls_remain_in_macro_denominator():
    scores = [{"truePositive": 2, "falsePositive": 0, "falseNegative": 0},
              {"truePositive": 0, "falsePositive": 0, "falseNegative": 2}]
    result = aggregate_interpretation(scores)
    assert result["attemptedCases"] == 2
    assert result["precision"] == 1.0
    assert result["recall"] == 0.5


def test_offline_mode_does_not_claim_model_or_human_results():
    report = offline_report()
    assert report["modelParsingCalls"] == 0
    assert report["parsingMetrics"] is None
    assert report["humanEvaluation"]["respondents"] == 0
    assert report["humanEvaluation"]["satisfaction"] is None


def test_route_metrics_use_directed_fixture_matrix():
    corpus, _ = load_corpus()
    fixture = corpus["scheduleFixture"]
    matrix = fixture["travelMinutes"]
    # START -> A -> B -> END differs from START -> B -> A -> END.
    forward = matrix[0][1] + matrix[1][2] + matrix[2][5]
    reverse = matrix[0][2] + matrix[2][1] + matrix[1][5]
    assert forward != reverse


def test_unlabelled_member_prompt_is_not_scored_against_combined_wishes():
    from app.ai_evaluation import _score_request
    case = {"id": "user-2", "gold": {"likes": ["자연", "휴식"], "food": ["채식"]}}
    result = {"suggestions": {"likes": None, "food": "채식", "walking": None, "scheduleItems": []}}
    member = _score_request(case, result, "member-1")
    assert not member["lexicalScored"]
    assert member["truePositive"] == member["falsePositive"] == member["falseNegative"] == 0
    assert aggregate_interpretation([member])["attemptedCases"] == 1
    assert aggregate_interpretation([member])["lexicallyScoredCalls"] == 0


def test_invalid_v1_booking_annotation_is_excluded_without_changing_corpus():
    from app.ai_evaluation import _score_request
    corpus, _ = load_corpus()
    case = next(x for x in corpus["challengeCases"] if x["id"] == "overlapping-bookings")
    score = _score_request(case, {"suggestions": {"scheduleItems": []}}, "combined")
    assert not score["appointmentScored"]
    assert score["appointmentFalseNegative"] == 0
    assert "invalid_v1_gold" in score["scoringExclusion"]


def test_empty_place_query_is_a_valid_unknown_and_missing_appointment_is_counted():
    case = {"id": "missing", "gold": {"appointment": {"date": None, "place": None, "time": None}}}
    score = score_interpretation(case, {"suggestions": {"scheduleItems": [{"date": None, "placeQuery": "", "time": None}]}})
    assert score["appointmentHallucinatedUnknownFields"] == 0
    missed = score_interpretation(case, {"suggestions": {"scheduleItems": []}})
    assert missed["appointmentMissingCount"] == 1


def test_unsupported_requirement_is_not_reported_as_a_proven_conflict():
    from app.ai_evaluation import _group_conflict_status
    result = {"suggestions": {"likes": None, "food": "채식", "foodImportance": "required",
                               "avoid": None, "avoidImportance": "required", "walking": None, "scheduleItems": []}}
    assert _group_conflict_status([{"syntheticInputOnlyResult": result}])["status"] == "blocked_unsupported_required"


def test_invalid_report_path_is_rejected_before_any_live_call(monkeypatch, tmp_path):
    import pytest
    from scripts import evaluate_ai
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(evaluate_ai, "live_interpretations", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit):
        evaluate_ai.run(["--live", "--output", str(tmp_path / "outside.json")])
    assert calls == []


def test_production_group_classifier_distinguishes_hard_conflict_from_soft_tradeoff():
    def parsed(food, importance):
        return {"suggestions": {"likes": None, "avoid": None, "avoidImportance": "required",
                                "food": food, "foodImportance": importance,
                                "walking": None, "scheduleItems": []}, "needsWalkingAnswer": True}

    from app.ai_evaluation import _group_conflict_status
    hard = _group_conflict_status([
        {"syntheticInputOnlyResult": parsed("자연", "required")},
        {"syntheticInputOnlyResult": parsed("자연 빼주세요", "required")},
    ])
    tradeoff = _group_conflict_status([
        {"syntheticInputOnlyResult": {"suggestions": {"likes": "자연", "avoid": None, "food": None, "foodImportance": "preferred"}}},
        {"syntheticInputOnlyResult": parsed("자연 빼주세요", "required")},
    ])
    assert hard["status"] == "blocked_hard_conflict"
    assert tradeoff["status"] == "soft_hard_tradeoff_detected"
