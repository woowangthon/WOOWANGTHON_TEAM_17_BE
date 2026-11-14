"""Bounded, explicit Gemini evaluation over the versioned synthetic corpus.

Offline mode reports fixture/coverage metadata only. It does not pretend a
mock response is an AI-quality measurement. Live mode performs one call per
case, records safe outcomes, and never retries.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORPUS_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "corpus.v1.json"
MAX_LIVE_CALLS = 25
ATOM_ALIASES = {
    "채식": ("채식",),
    "자연": ("자연", "풍경", "호수", "계곡", "숲"),
    "문화": ("문화", "박물관", "역사"),
    "휴식": ("휴식", "조용", "힐링"),
    "맛집": ("맛집", "현지 음식", "식당"),
    "카페": ("카페",),
    "채식": ("채식", "비건", "vegetarian"),
    "등산": ("등산", "산행"),
    "계단": ("계단",),
}


def load_corpus(path: Path | str = CORPUS_PATH) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1
            or value.get("sampleType") != "synthetic"
            or len(value.get("primaryCases", [])) != 5
            or len(value.get("challengeCases", [])) > 20):
        raise ValueError("Invalid synthetic evaluation corpus")
    if len({case.get("id") for case in value["primaryCases"]}) != 5:
        raise ValueError("Synthetic primary case IDs must be unique")
    digest = hashlib.sha256(raw).hexdigest()
    return value, digest


def _atoms(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    text = value.casefold()
    return {atom for atom, aliases in ATOM_ALIASES.items() if any(alias.casefold() in text for alias in aliases)}


def _prediction_atoms(result: Any) -> set[tuple[str, str]]:
    if not isinstance(result, dict) or not isinstance(result.get("suggestions"), dict):
        return set()
    suggestions = result["suggestions"]
    return {(field, atom) for field in ("likes", "avoid", "food")
            for atom in _atoms(suggestions.get(field))}


def score_interpretation(case: dict[str, Any], result: Any) -> dict[str, Any]:
    """Score only explicitly labelled lexical atoms; expose the narrow rubric."""
    gold = case.get("gold") if isinstance(case.get("gold"), dict) else {}
    lexical_scored = any(key in gold for key in ("fields", "likes", "avoid", "food", "foodPositive", "foodNegative"))
    appointment_scored = "appointment" in gold or "appointments" in gold
    expected = set()
    expected_fields = gold.get("fields", {}) if isinstance(gold.get("fields"), dict) else {}
    for field in ("likes", "avoid", "food", "foodPositive", "foodNegative"):
        values = expected_fields.get(field, gold.get(field, []))
        target_field = "food" if field.startswith("food") else field
        if isinstance(values, list):
            for value in values:
                expected |= {(target_field, atom) for atom in _atoms(value)}
    actual = _prediction_atoms(result)
    true_positive = len(expected & actual)
    false_positive = len(actual - expected) if lexical_scored else 0
    false_negative = len(expected - actual)
    suggestions = result.get("suggestions") if isinstance(result, dict) else None
    if not isinstance(suggestions, dict):
        outcome = "provider_error_or_invalid_response"
    elif suggestions.get("walking") is None and result.get("needsWalkingAnswer") is True:
        outcome = "walking_clarification"
    elif (gold.get("appointment") or gold.get("appointments")) and not suggestions.get("scheduleItems"):
        outcome = "appointment_missed_or_clarified"
    else:
        outcome = "parsed"
    appointment_expected = bool(gold.get("appointment") or gold.get("appointments"))
    appointment_count = len(suggestions.get("scheduleItems", [])) if isinstance(suggestions, dict) and isinstance(suggestions.get("scheduleItems"), list) else None
    appointment_tp = appointment_fp = appointment_fn = 0
    appointment_expected_fields = appointment_predicted_fields = 0
    predicted_items = suggestions.get("scheduleItems", []) if isinstance(suggestions, dict) and isinstance(suggestions.get("scheduleItems"), list) else []
    appointment_gold = gold.get("appointments") if isinstance(gold.get("appointments"), list) else gold.get("appointment")
    expected_items = appointment_gold if isinstance(appointment_gold, list) else [appointment_gold] if isinstance(appointment_gold, dict) else []
    predicted_items = [item for item in predicted_items if isinstance(item, dict)]
    matched_prediction_indexes = set()
    hallucinated_unknown_fields = 0
    for expected_item in expected_items:
        expected_fields = [key for key in ("date", "time", "place", "scope") if key in expected_item]
        appointment_expected_fields += sum(expected_item.get(key) is not None for key in expected_fields)
        best_index, best_match = None, -1
        for index, item in enumerate(predicted_items):
            if index in matched_prediction_indexes:
                continue
            matched = 0
            candidate_hallucinated = 0
            for key in expected_fields:
                if expected_item.get(key) is None:
                    result_key = "placeQuery" if key == "place" else key
                    candidate_hallucinated += int(item.get(result_key) not in (None, ""))
                elif key == "place":
                    matched += int(isinstance(item.get("placeQuery"), str) and expected_item[key].casefold() in item["placeQuery"].casefold())
                else:
                    matched += int(item.get(key) == expected_item[key])
            if matched > best_match:
                best_index, best_match = index, matched
                best_hallucinated = candidate_hallucinated
        if best_index is not None and (best_match > 0 or any(expected_item.get(key) is None for key in expected_fields)):
            matched_prediction_indexes.add(best_index)
            appointment_predicted_fields += max(0, best_match)
            hallucinated_unknown_fields += best_hallucinated
    appointment_tp = appointment_predicted_fields
    appointment_fn = appointment_expected_fields - appointment_predicted_fields
    if appointment_expected and len(predicted_items) > len(expected_items):
        appointment_fp = len(predicted_items) - len(expected_items)
    elif appointment_scored and not appointment_expected and predicted_items:
        appointment_fp = 1
    return {
        "caseId": case.get("id"), "outcome": outcome,
        "lexicalScored": lexical_scored, "appointmentScored": appointment_scored,
        "expectedAtoms": [f"{field}:{atom}" for field, atom in sorted(expected)],
        "predictedAtoms": [f"{field}:{atom}" for field, atom in sorted(actual)],
        "truePositive": true_positive, "falsePositive": false_positive,
        "falseNegative": false_negative,
        "walkingExpected": gold.get("walking"),
        "walkingPredicted": suggestions.get("walking") if isinstance(suggestions, dict) else None,
        "appointmentExpected": appointment_expected,
        "appointmentCount": appointment_count,
        "appointmentTruePositive": appointment_tp,
        "appointmentFalsePositive": appointment_fp,
        "appointmentFalseNegative": appointment_fn,
        "appointmentExpectedFields": appointment_expected_fields,
        "appointmentMatchedFields": appointment_predicted_fields,
        "appointmentHallucinatedUnknownFields": hallucinated_unknown_fields,
        "appointmentExpectedCount": len(expected_items) if appointment_scored else None,
        "appointmentMissingCount": max(0, len(expected_items) - len(predicted_items)) if appointment_scored else None,
        "walkingExactMatch": None if not isinstance(suggestions, dict) or "walking" not in gold else suggestions.get("walking") == gold["walking"],
        "walkingAnswerRequiredExact": None if not isinstance(result, dict) or "needsWalkingAnswer" not in gold else result.get("needsWalkingAnswer") is gold["needsWalkingAnswer"],
        "walkingStatus": "not_scored" if "walking" not in gold else "matched" if isinstance(suggestions, dict) and suggestions.get("walking") == gold["walking"] else "mismatched",
        "humanHandlingReview": "pending",
        "unscoredOpaqueFields": sorted(field for field in ("likes", "avoid", "food")
                                        if isinstance(suggestions, dict) and isinstance(suggestions.get(field), str)
                                        and suggestions[field] and not _atoms(suggestions[field])),
    }


def aggregate_interpretation(scores: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(item["truePositive"] for item in scores)
    fp = sum(item["falsePositive"] for item in scores)
    fn = sum(item["falseNegative"] for item in scores)
    ap = sum(item.get("appointmentTruePositive", 0) for item in scores)
    afp = sum(item.get("appointmentFalsePositive", 0) for item in scores)
    afn = sum(item.get("appointmentFalseNegative", 0) for item in scores)
    return {
        "attemptedCases": len(scores), "truePositive": tp, "falsePositive": fp, "falseNegative": fn,
        "lexicallyScoredCalls": sum(item.get("lexicalScored", True) for item in scores),
        "appointmentScoredCalls": sum(item.get("appointmentScored", False) for item in scores),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "lexicalRubric": "bounded aliases in app/ai_evaluation.py; partial lexical coverage, not human semantic adjudication",
        "appointmentCounts": {"truePositive": ap, "falsePositive": afp, "falseNegative": afn},
    }


def _score_request(case, result, member_id):
    # The frozen v1 corpus has no per-member lexical annotation. Do not grade a
    # member against the combined group's wishes. Keep the request in call counts.
    exclusion = None
    if member_id != "combined":
        exclusion = "member_prompt_has_no_individual_lexical_gold"
    elif case["id"] == "overlapping-bookings":
        exclusion = "invalid_v1_gold_confuses_meal_end_with_appointment_start"
    scoring_case = {"id": case["id"], "gold": {}} if exclusion else case
    score = score_interpretation(scoring_case, result)
    if exclusion:
        score["scoringExclusion"] = exclusion
    return score


def evaluation_provenance():
    """Hash evaluated code, including dirty changes; never inspect dotenv."""
    import subprocess
    root = CORPUS_PATH.parents[1]
    digest = hashlib.sha256()
    for path in sorted([*root.joinpath("app").glob("*.py"), root / "scripts" / "evaluate_ai.py"]):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    def git_value(*args):
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return {"backendHead": git_value("rev-parse", "HEAD"), "backendDirty": bool(git_value("status", "--porcelain")),
            "evaluatedSourceSha256": digest.hexdigest(), "pythonVersion": __import__("sys").version.split()[0]}


def rescore_interpretations(report):
    """Re-evaluate frozen provider responses without another paid call."""
    import copy
    from app.preference_grounding import ground_interpretation
    corpus, digest = load_corpus()
    if report.get("corpusSha256") != digest or report.get("executionMode") != "live_synthetic_inputs":
        raise ValueError("Saved responses do not match the frozen synthetic corpus")
    revised = copy.deepcopy(report)
    cases = {c["id"]: c for c in [*corpus["primaryCases"], *corpus["challengeCases"]]}
    for item in revised["items"]:
        case = cases[item["caseId"]]
        member_id = item.get("memberId", "combined")
        original = case["text"] if member_id == "combined" else case["memberTexts"][int(member_id.split("-")[1]) - 1]
        raw = item.get("rawSyntheticInputOnlyResult")
        if item.get("status") == "completed" and raw is not None:
            item["syntheticInputOnlyResult"] = ground_interpretation(original, raw)
        item["raw"] = _score_request(case, raw, member_id)
        item["grounded"] = _score_request(case, item.get("syntheticInputOnlyResult"), member_id)
    revised["rawMetrics"] = aggregate_interpretation([x["raw"] for x in revised["items"]])
    revised["groundedMetrics"] = aggregate_interpretation([x["grounded"] for x in revised["items"]])
    revised["groupConflictAnalysis"] = {
        cid: _group_conflict_status([x for x in revised["items"] if x["caseId"] == cid and x.get("memberId", "").startswith("member-")])
        for cid, case in cases.items() if case.get("memberTexts")}
    revised.update({"primaryInputCount": len(corpus["primaryCases"]), "challengeInputCount": len(corpus["challengeCases"]),
                    "caseCount": len(cases), "attemptedCalls": len(revised["items"]), "modelParsingCalls": len(revised["items"]),
                    "responseCount": sum(x["status"] == "completed" for x in revised["items"]),
                    "rescoreRequiresNoProviderCalls": True, "scoringRevision": "v2-context-and-polarity-aware",
                    "scoredAt": datetime.now(timezone.utc).isoformat(), "provenance": evaluation_provenance(),
                    "excludedAnnotationCount": 1, "excludedAnnotationCases": ["overlapping-bookings"],
                    "postRunMethodologyCorrections": [
                        "Individual member prompts excluded from lexical aggregate: no member-specific v1 gold.",
                        "Invalid end/start booking gold excluded; original corpus and response remain unchanged.",
                        "Scorer includes the prelabelled literal vegetarian atom and distinguishes empty unknown place from invention.",
                        "Stored responses reprocessed after a walking-contradiction guard regression fix; development evaluation, not holdout."]})
    return revised


def synthetic_route_score(case: dict[str, Any], order_by_day: dict[str, list[str]], *,
                          fixed_visits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Independent deterministic scorer for the bundled directed time matrix."""
    corpus, _ = load_corpus()
    fixture = corpus["scheduleFixture"]
    names = fixture["nodes"]
    matrix = fixture["travelMinutes"]
    indices = {name: index for index, name in enumerate(names)}
    unknown = 0
    total = 0
    count = 0
    details = []
    for day, stops in order_by_day.items():
        if not isinstance(stops, list) or any(name not in indices for name in stops):
            return {"status": "invalid_candidate", "travelMinutes": None, "visitCount": 0, "violations": ["unknown_node"]}
        route = ["START", *stops, "END"]
        minutes = sum(matrix[indices[a]][indices[b]] for a, b in zip(route, route[1:]))
        total += minutes
        count += len(stops)
        details.append({"date": day, "travelMinutes": minutes, "visitCount": len(stops)})
    violations = []
    expected = {(item.get("date"), item.get("contentid")) for item in (fixed_visits or [])}
    if expected:
        violations.append("fixture_content_ids_required_for_anchor_check")
        unknown += 1
    return {"status": "unknown" if unknown else "scored_synthetic_matrix", "travelMinutes": total,
            "visitCount": count, "violations": violations, "unknownCount": unknown, "days": details,
            "routeDataLabel": fixture["label"]}


def _human_placeholder(case_ids: list[str]) -> dict[str, Any]:
    return {"respondents": 0, "satisfaction": None, "preferencesReflected": None,
            "wouldUseWithoutEdits": None,
            "responses": [{"caseId": case_id, "blindOrder": None, "rating1To5": None,
                           "preferredPlan": None, "constraintIssue": None, "comment": None}
                          for case_id in case_ids],
            "status": "not_measured_until_people_submit_ratings"}


def offline_report() -> dict[str, Any]:
    corpus, digest = load_corpus()
    return {
        "schemaVersion": 1, "executionMode": "offline_fixture_metadata_only",
        "createdAt": datetime.now(timezone.utc).isoformat(), "sampleType": "synthetic",
        "corpusSha256": digest, "primaryInputCount": len(corpus["primaryCases"]),
        "challengeInputCount": len(corpus["challengeCases"]),
        "modelParsingCalls": 0, "schedulingModelCalls": 0,
        "parsingMetrics": None, "scheduleMetrics": None,
        "limitations": ["Offline mode did not call Gemini and measures no model accuracy.",
                        "Bundled route matrix and catalog are synthetic; no real road travel was measured."],
        "humanEvaluation": _human_placeholder([x["id"] for x in corpus["primaryCases"]]),
    }


def _safe_error(exc: Exception) -> str:
    name = type(exc).__name__
    return name if name in {"GeminiError", "GeminiInvalidResponseError", "GeminiTimeoutError", "GeminiUnavailableError", "TimeoutError"} else "EVALUATION_FAILED"


def _prefs_from_result(result: Any, member_ref: str) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not isinstance(result.get("suggestions"), dict):
        return None
    raw = result["suggestions"]
    return {"memberRef": member_ref, **{key: raw.get(key) for key in
            ("likes", "avoid", "avoidImportance", "food", "foodImportance", "walking")},
            "scheduleItems": raw.get("scheduleItems", [])}


def _group_conflict_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the production confirmed-preference conflict classifier to model output."""
    from app.confirmed_preferences import analyze_preferences

    prefs = []
    for index, record in enumerate(records, 1):
        parsed = _prefs_from_result(record.get("syntheticInputOnlyResult"), f"p{index}")
        if parsed is not None:
            prefs.append(parsed)
    if len(prefs) != len(records):
        return {"status": "not_evaluable_incomplete_member_parse", "memberCount": len(prefs), "issueCodes": []}
    analysis = analyze_preferences(prefs, {"자연", "문화", "액티비티", "휴식", "쇼핑", "맛집", "카페"})
    issues = sorted({item["code"] for item in analysis["issues"]})
    hard = "REQUIRED_PREFERENCE_UNSATISFIED" in issues
    unsupported = "REQUIRED_PREFERENCE_UNSUPPORTED" in issues
    predicates = analysis.get("predicates", [])
    mixed_polarity = any(a["tag"] == b["tag"] and a["polarity"] != b["polarity"]
                         and a["memberRef"] != b["memberRef"] for index, a in enumerate(predicates)
                         for b in predicates[index + 1:])
    soft_and_hard = mixed_polarity and not hard
    return {"status": "blocked_hard_conflict" if hard else "blocked_unsupported_required" if unsupported else "soft_hard_tradeoff_detected" if soft_and_hard else "no_supported_hard_conflict_detected",
            "memberCount": len(prefs), "issueCodes": issues,
            "scope": "bounded production tag detector over AI member drafts; misses unsupported/free-form semantics"}


def _synthetic_catalog(conditions: dict[str, Any]):
    import hashlib
    import json
    from app.place_catalog import PlaceCatalog, _freeze

    city = conditions["region"]
    tags = ["자연", "문화", "휴식", "맛집", "카페", "액티비티", "쇼핑"]
    roles = [("sightseeing_stop", "MAIN_DESTINATION"), ("culture_stop", "MAIN_DESTINATION"),
             ("activity_stop", "MAIN_DESTINATION"), ("meal_stop", "MEAL"),
             ("shopping_stop", "GENERAL_SHOPPING"), ("sightseeing_stop", "MAIN_DESTINATION"),
             ("culture_stop", "MAIN_DESTINATION"), ("meal_stop", "MEAL"),
             ("activity_stop", "MAIN_DESTINATION"), ("sightseeing_stop", "MAIN_DESTINATION")]
    places, details, preferences = [], [], []
    for index, (role, use) in enumerate(roles, 1):
        lon = 127.0 + index / 100
        lat = 36.0 + index / 100
        places.append({"contentid": index, "title": f"합성 {city} 장소 {index}", "planningRole": role,
                       "recommendationUse": use, "adminCity": city, "geoValidated": "yes",
                       "mapx": lon, "mapy": lat})
        details.append({"contentid": index, "estimatedStayMinutes": 45,
                        "planningStatusV2": "candidate_needs_live_validation"})
        preferences.append({"contentid": index, "preferenceTags": tags[index % len(tags)], "vibeTags": ""})
    digest = hashlib.sha256(json.dumps([places, details, preferences], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return PlaceCatalog(tuple(_freeze(row) for row in places), tuple(_freeze(row) for row in details),
                        tuple(_freeze(row) for row in preferences), tuple(tags), digest, ())


class _SyntheticSnapshot:
    def __init__(self, conditions: dict[str, Any], preferences: list[dict[str, Any]]):
        import hashlib
        import json
        self._doc = {"conditions": conditions, "confirmedPreferences": preferences}
        self.fingerprint = hashlib.sha256(json.dumps(self._doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def as_dict(self):
        import copy
        return copy.deepcopy(self._doc)

    def confirmed_preferences(self):
        return self.as_dict()["confirmedPreferences"]


class _SyntheticRouter:
    """Directed fictional minutes. It must never be represented as actual roads."""
    def route(self, origin, destination, mode):
        import math
        a = int(round((origin["longitude"] - 127.0) * 100)) if origin and origin["longitude"] >= 127.01 else 0
        b = int(round((destination["longitude"] - 127.0) * 100)) if destination and destination["longitude"] >= 127.01 else 0
        minutes = 7 + ((a * 13 + b * 7 + a * b * 3) % 28)
        return {"status": "verified", "mode": mode, "durationSeconds": minutes * 60,
                "distanceMeters": minutes * 700, "knownCostKrw": 0, "costComplete": False,
                "walkingSeconds": [] if mode != "walk" else [minutes * 60], "geometry": [],
                "reasonCodes": ["SYNTHETIC_ROUTE_MATRIX"]}


def _live_schedule(primary_case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Run model draft -> production selector -> baseline/optimizer in a detached synthetic sandbox."""
    from app.confirmed_preferences import analyze_preferences
    from app.candidate_selection import build_supported_tags
    from app.day_schedule import build_scheduled_plan
    from app.generation_draft import generate_candidate_draft

    conditions = dict(primary_case["conditions"])
    parsed = _prefs_from_result(result, "p1")
    if parsed is None:
        return {"status": "blocked_parser_failure", "aiDraftCallCount": 0, "planValidation": None}
    if parsed["walking"] is None:
        return {"status": "blocked_walking_clarification", "aiDraftCallCount": 0, "planValidation": None,
                "reason": "A walking limit or explicit unlimited setting is required before generation."}
    catalog = _synthetic_catalog(conditions)
    evidence = analyze_preferences([parsed], build_supported_tags(catalog))
    if any(issue["code"].startswith("REQUIRED_") for issue in evidence["issues"]):
        return {"status": "blocked_required_preference", "issueCodes": sorted({issue["code"] for issue in evidence["issues"]}),
                "aiDraftCallCount": 0, "planValidation": None}
    conditions.setdefault("departureTime", None)
    snapshot = _SyntheticSnapshot(conditions, [parsed])
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "reviewedCommonFields": [],
                "departureAnchor": {"label": "synthetic start", "coordinate": {"latitude": 36.0, "longitude": 127.0, "crs": "WGS84"}},
                "returnAnchor": {"label": "synthetic end", "coordinate": {"latitude": 36.0, "longitude": 127.0, "crs": "WGS84"}}}

    draft_call_count = [0]
    from chungbuk_trip_ai import draft_candidate_itinerary

    def _counted_drafter(*args, **kwargs):
        draft_call_count[0] += 1
        return draft_candidate_itinerary(*args, **kwargs)

    async def _draft():
        return await generate_candidate_draft(snapshot, None, catalog, None, lambda: snapshot, lambda: catalog,
                                              confirmed_evidence=evidence, common_settings=settings, timeout_seconds=60,
                                              drafter=_counted_drafter)
    try:
        draft = _draft()
        import asyncio
        proposed = asyncio.run(draft).as_dict()
    except Exception as exc:
        return {"status": "blocked_or_failed_ai_draft", "errorCode": _safe_error(exc),
                "aiDraftCallCount": draft_call_count[0], "planValidation": None}
    baseline_report, optimized_report = {}, {}
    try:
        baseline = build_scheduled_plan(proposed, catalog, conditions, [parsed], settings, _SyntheticRouter(),
                                        optimize=False, optimization_report=baseline_report)
        optimized = build_scheduled_plan(proposed, catalog, conditions, [parsed], settings, _SyntheticRouter(),
                                         optimize=True, optimization_report=optimized_report)
    except Exception as exc:
        return {"status": "schedule_validation_failed", "errorCode": _safe_error(exc),
                "aiDraftCallCount": draft_call_count[0], "planValidation": None}
    def summarize(plan):
        edges = [edge for day in plan.get("days", []) for edge in day.get("edges", [])]
        seconds = [edge.get("durationSeconds") for edge in edges if type(edge.get("durationSeconds")) is int]
        visits = [node for day in plan.get("days", []) for node in day.get("nodes", []) if node.get("kind") == "PLACE"]
        validation = plan.get("validation", {})
        return {"status": validation.get("status"), "visitCount": len(visits),
                "routeSeconds": sum(seconds) if len(seconds) == len(edges) else None,
                "violationCount": len(validation.get("violations", [])),
                "unknownDomains": sorted(key for key, value in validation.items()
                                          if isinstance(value, dict) and value.get("status") == "unknown"),
                "reasonCodes": sorted({warning for warning in validation.get("warnings", [])}),
                "fixture": "synthetic_catalog_and_directed_route_costs_not_real_places_or_roads"}
    def plan_orders(plan):
        return [{"date": day["date"], "contentids": [node.get("contentid") for node in day.get("nodes", [])
                                                     if node.get("kind") == "PLACE"]}
                for day in plan.get("days", [])]
    return {"status": "completed", "aiDraftCallCount": draft_call_count[0],
            "baseline": summarize(baseline), "optimized": summarize(optimized),
            "baselineSyntheticOrders": plan_orders(baseline), "optimizedSyntheticOrders": plan_orders(optimized),
            "optimizerDiagnostics": optimized_report,
            "sameGeminiDraftUsedForBoth": True,
            "syntheticDraft": proposed, "baselinePlan": baseline, "optimizedPlan": optimized,
            "independentAudits": _audit_pair(baseline, optimized, catalog, conditions, [parsed], settings,
                                             proposed.get("optimizationCandidates", [])),
            "scheduleInputScope": "combined_personal_only; other member drafts/group conflict decisions not injected",
            "limitations": ["synthetic catalog and directed route minutes, not real routes or operating schedules",
                            "unknown catalog prices/opening-hours remain unknown; no human satisfaction was measured"]}


def _audit_pair(baseline, optimized, catalog, conditions, preferences, settings, candidates):
    from app.evaluation_audit import audit_synthetic_plan
    return {name: audit_synthetic_plan(plan, catalog=catalog, conditions=conditions,
                                      preferences=preferences, settings=settings, eligible_candidates=candidates)
            for name, plan in (("baseline", baseline), ("optimized", optimized))}


def audit_saved_schedules(schedule_report, parsing_report):
    """Audit saved output orders without additional Gemini calls.

    Early development artifacts stored fitted output orders only. Their original
    unmodified AI draft cannot be recovered; state this explicitly on replay.
    """
    import copy
    from app.candidate_selection import build_confirmed_candidate_selection, build_supported_tags
    from app.confirmed_preferences import analyze_preferences
    from app.day_schedule import build_scheduled_plan
    corpus, digest = load_corpus()
    if schedule_report.get("corpusSha256") != digest or parsing_report.get("corpusSha256") != digest:
        raise ValueError("Saved schedule/parsing corpus mismatch")
    cases = {c["id"]: c for c in corpus["primaryCases"]}
    parsings = {r["caseId"]: r for r in parsing_report["items"] if r.get("memberId") == "combined"}
    revised = copy.deepcopy(schedule_report)
    for item in revised.get("schedules", []):
        if not item.get("baselineSyntheticOrders"):
            continue
        case = cases[item["caseId"]]
        conditions = dict(case["conditions"])
        conditions.setdefault("departureTime", None)
        pref = _prefs_from_result(parsings[item["caseId"]]["syntheticInputOnlyResult"], "p1")
        catalog = _synthetic_catalog(conditions)
        settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "reviewedCommonFields": [],
                    "departureAnchor": {"label": "synthetic start", "coordinate": {"latitude": 36.0, "longitude": 127.0, "crs": "WGS84"}},
                    "returnAnchor": {"label": "synthetic end", "coordinate": {"latitude": 36.0, "longitude": 127.0, "crs": "WGS84"}}}
        snapshot = _SyntheticSnapshot(conditions, [pref])
        evidence = analyze_preferences([pref], build_supported_tags(catalog))
        selection = build_confirmed_candidate_selection(snapshot, catalog, evidence, settings)
        candidates = selection.draft_candidates()
        plans = {}
        for name in ("baseline", "optimized"):
            orders = item[name + "SyntheticOrders"]
            detached = {"days": [{"date": day["date"], "places": [catalog.presentation_place(cid) for cid in day["contentids"]]}
                                  for day in orders]}
            plans[name] = build_scheduled_plan(detached, catalog, conditions, [pref], settings, _SyntheticRouter(), optimize=False)
        item["independentAudits"] = _audit_pair(plans["baseline"], plans["optimized"], catalog, conditions, [pref], settings, candidates)
        item["replayedPlans"] = plans
        item["replayLimitation"] = "Original AI draft was not captured in this early run; audit reconstructs clocks from saved fitted output orders. No Gemini call repeated."
        item["allParticipantConstraintsVerified"] = False
    revised.update({"auditedAt": datetime.now(timezone.utc).isoformat(), "auditProvenance": evaluation_provenance(),
                    "newProviderCallsForAudit": 0, "independentAuditLinked": True,
                    "generationSourceProvenance": {"backendBase": "e4be3b9", "workingTreeModified": True,
                                                    "exactSourceHashAtInvocation": None,
                                                    "limitation": "Development invocation preceded exact source hashing; subsequent replay hash is recorded."}})
    return revised


def live_schedules(parsing_report: dict[str, Any], *, maximum_total_calls: int = MAX_LIVE_CALLS) -> dict[str, Any]:
    """Replay saved parser results through selection/drafting without reparsing.

    At most five additional draft calls are made. The same saved grounded parse
    feeds baseline and optimized routing, so the schedule comparison is paired.
    """
    corpus, digest = load_corpus()
    if (parsing_report.get("executionMode") != "live_synthetic_inputs"
            or parsing_report.get("corpusSha256") != digest):
        raise ValueError("Saved parsing report does not match the frozen synthetic corpus")
    parser_calls = parsing_report.get("attemptedCalls")
    if type(parser_calls) is not int or parser_calls + len(corpus["primaryCases"]) > maximum_total_calls or maximum_total_calls > MAX_LIVE_CALLS:
        raise ValueError("Combined provider call limit would be exceeded")
    primary_cases = {item["id"]: item for item in corpus["primaryCases"]}
    by_case = {}
    for item in parsing_report.get("items", []):
        if item.get("memberId") == "combined" and item.get("caseId") in primary_cases:
            by_case[item["caseId"]] = item
    schedules = []
    for case_id, case in primary_cases.items():
        record = by_case.get(case_id)
        parsed = record.get("syntheticInputOnlyResult") if record else None
        if record is None or record.get("status") != "completed" or not isinstance(parsed, dict):
            schedules.append({"caseId": case_id, "status": "blocked_parser_error_or_missing_parse",
                              "aiDraftCallCount": 0, "planValidation": None})
            continue
        schedules.append({"caseId": case_id, **_live_schedule(case, parsed)})
    calls = sum(item.get("aiDraftCallCount", 0) for item in schedules)
    return {"executionMode": "live_synthetic_schedule_replay", "sampleType": "synthetic",
            "createdAt": datetime.now(timezone.utc).isoformat(), "corpusSha256": digest,
            "modelParsingCalls": parser_calls, "schedulingModelCalls": calls, "parserCallsReused": parser_calls, "draftCalls": calls,
            "totalLiveCalls": parser_calls + calls,
            "humanEvaluation": _human_placeholder(list(primary_cases)),
            "routeDataLabel": "synthetic catalog and directed time costs, not live routes",
            "schedules": schedules}


def live_interpretations(*, maximum_calls: int = MAX_LIVE_CALLS) -> dict[str, Any]:
    """Call the currently installed AI parser once per synthetic primary/challenge.

    The parser results are synthetic-input data; no custom/private corpus is
    accepted by this function, and provider exceptions are reduced to safe type.
    """
    if type(maximum_calls) is not int or not 1 <= maximum_calls <= MAX_LIVE_CALLS:
        raise ValueError("Invalid live call limit")
    corpus, digest = load_corpus()
    cases = [*corpus["primaryCases"], *corpus["challengeCases"]]
    requests = []
    for case in cases:
        if isinstance(case.get("memberTexts"), list) and case["memberTexts"]:
            requests.extend((case, text, f"member-{idx + 1}") for idx, text in enumerate(case["memberTexts"]))
            if case.get("text"):
                requests.append((case, case["text"], "combined"))
        elif case.get("text"):
            requests.append((case, case["text"], "combined"))
    if len(requests) + len(corpus["primaryCases"]) > maximum_calls:
        raise ValueError("Configured limit is too low for the bounded evaluation")
    try:
        from chungbuk_trip_ai import interpret_personal_draft
        from app.preference_grounding import ground_interpretation
    except Exception as exc:
        return {"executionMode": "live_requested", "corpusSha256": digest,
                "modelParsingCalls": 0, "items": [], "errorCode": _safe_error(exc)}
    records = []
    for case, original_text, member_id in requests:
        started = time.monotonic()
        try:
            raw = interpret_personal_draft(original_text, case["conditions"])
            grounded = ground_interpretation(original_text, raw)
            records.append({"caseId": case["id"], "memberId": member_id, "status": "completed",
                            "raw": score_interpretation(case, raw),
                            "grounded": score_interpretation(case, grounded),
                            "rawSyntheticInputOnlyResult": raw,
                            "syntheticInputOnlyResult": grounded,
                            "elapsedSeconds": round(time.monotonic() - started, 3)})
        except Exception as exc:
            records.append({"caseId": case["id"], "memberId": member_id, "status": "failed", "errorCode": _safe_error(exc),
                            "raw": score_interpretation(case, None),
                            "grounded": score_interpretation(case, None),
                            "elapsedSeconds": round(time.monotonic() - started, 3)})
    group_evaluations = {}
    for group_id, group_case in [("user-2", corpus["primaryCases"][1]),
                                 ("group-hard-conflict", next(c for c in corpus["challengeCases"] if c["id"] == "group-hard-conflict")),
                                 ("soft-vs-hard", next(c for c in corpus["challengeCases"] if c["id"] == "soft-vs-hard"))]:
        member_records = [x for x in records if x["caseId"] == group_id and x.get("memberId", "").startswith("member-")]
        if member_records:
            group_evaluations[group_id] = _group_conflict_status(member_records)
    try:
        from chungbuk_trip_ai.gemini import load_gemini_settings
        model = load_gemini_settings().model
    except Exception:
        model = None
    return {"executionMode": "live_synthetic_inputs", "sampleType": "synthetic",
            "corpusSha256": digest, "modelParsingCalls": len(records),
            "attemptedCalls": len(requests), "caseCount": len(cases), "model": model,
            "createdAt": datetime.now(timezone.utc).isoformat(), "items": records,
            "groupConflictAnalysis": group_evaluations,
            "rawMetrics": aggregate_interpretation([x["raw"] for x in records]),
            "groundedMetrics": aggregate_interpretation([x["grounded"] for x in records]),
            "challengeHandling": "manual review required; semantic contradiction coverage is not fully automated"}
