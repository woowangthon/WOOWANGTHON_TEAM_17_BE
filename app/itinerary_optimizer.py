"""Small, conservative local search for already validated itinerary drafts.

This is a bounded TOPTW-inspired heuristic, not an exact solver. The caller
supplies a complete schedule-and-validation function so every accepted move is
checked against the same route, time, opening-hour, meal, walking and budget
rules as the current planner.
"""
from __future__ import annotations

import copy
import time
from collections import Counter, deque
from typing import Any, Callable

from .generation_policy import GENERATION_POLICY_VERSION


_FLEXIBLE_ROLES = {"sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop", "date_specific_event"}


def _visit_ids(draft: dict[str, Any]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return tuple((day["date"], tuple(place["contentid"] for place in day["places"])) for day in draft["days"])


def _violations(plan: dict[str, Any]) -> Counter:
    return Counter(item.get("code", "UNKNOWN") for item in plan["validation"].get("violations", []))


def _warnings(plan: dict[str, Any]) -> Counter:
    return Counter(plan["validation"].get("warnings", []))


def _travel_seconds(plan: dict[str, Any]) -> int | None:
    seconds = 0
    for day in plan["days"]:
        for edge in day["edges"]:
            if edge.get("status") != "verified" or type(edge.get("durationSeconds")) is not int:
                return None
            seconds += edge["durationSeconds"]
    return seconds


def _base_utility(row: dict[str, Any], rank_count: int | None) -> float:
    rank = row.get("selectionRank")
    if rank_count is not None and type(rank) is int and 0 <= rank < rank_count:
        return (rank_count - rank) / (rank_count + 1)
    try:
        score = float(row.get("groupScore", 0.0))
        return score if score == score and abs(score) != float("inf") else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _rank_count(candidates: dict[int, dict[str, Any]]) -> int | None:
    ranks = [row.get("selectionRank") for row in candidates.values()]
    if ranks and all(type(rank) is int and rank >= 0 for rank in ranks):
        # CandidateSelection ranks can contain gaps after a detached subset is
        # formed; max+1 keeps their original ordering and positive normalization.
        return max(ranks) + 1
    return None


def _preference_score(draft: dict[str, Any], candidates: dict[int, dict[str, Any]],
                      anchors: set[tuple[str, int]] | None = None) -> float:
    """Same-day category utility with a 1/k marginal discount for repeats."""
    rank_count = _rank_count(candidates)
    anchors = anchors or set()
    score = 0.0
    for day in draft["days"]:
        groups: dict[str, list[float]] = {}
        for place in day["places"]:
            row = candidates.get(place["contentid"], {})
            role = row.get("planningRole", place.get("planningRole"))
            utility = _base_utility(row, rank_count)
            category = row.get("categoryKey")
            if ((day["date"], place["contentid"]) in anchors
                    or role in {"overnight_lodging", "overnight_camping"}
                    or not isinstance(category, str) or not category):
                score += utility
                continue
            groups.setdefault(category, []).append(utility)
        for values in groups.values():
            positives = sorted((value for value in values if value > 0), reverse=True)
            score += sum(value / (index + 1) for index, value in enumerate(positives))
            # Negative fallback scores represent avoids. Repetition must never
            # make them less negative or turn an avoided category into a gain.
            score += sum(value for value in values if value <= 0)
    return score


def _category_repetitions(draft: dict[str, Any], candidates: dict[int, dict[str, Any]],
                          anchors: set[tuple[str, int]] | None = None) -> int:
    anchors = anchors or set()
    count = 0
    for day in draft["days"]:
        seen: set[str] = set()
        for place in day["places"]:
            if (day["date"], place["contentid"]) in anchors:
                continue
            row = candidates.get(place["contentid"], {})
            if row.get("planningRole", place.get("planningRole")) in {"overnight_lodging", "overnight_camping"}:
                continue
            key = row.get("categoryKey")
            if isinstance(key, str) and key:
                if key in seen:
                    count += 1
                seen.add(key)
    return count


def _rank(draft: dict[str, Any], plan: dict[str, Any], candidates: dict[int, dict[str, Any]],
          anchors: set[tuple[str, int]] | None = None):
    travel = _travel_seconds(plan)
    # Unknown routes are never assigned a zero cost or treated as an improvement.
    return (sum(_violations(plan).values()), -_preference_score(draft, candidates, anchors),
            travel if travel is not None else float("inf"))


def _anchored_positions(settings: dict[str, Any]) -> dict[tuple[str, int], int]:
    result = {}
    for group in settings.get("reviewedCommonFields", []):
        for anchor in group.get("anchors", []):
            if type(anchor.get("position")) is int:
                result[(anchor["date"], anchor["contentid"])] = anchor["position"]
    for anchor in settings.get("confirmedScheduleAnchors", []):
        if type(anchor.get("position")) is int:
            result[(anchor["date"], anchor["contentid"])] = anchor["position"]
    return result


def _risk_counts(plan: dict[str, Any]) -> Counter:
    risks = Counter()
    for domain in ("route", "walking", "time", "budget", "openingHours"):
        value = plan["validation"].get(domain, {})
        for code in value.get("reasonCodes", []):
            risks[(domain, code)] += 1
    for day in plan.get("days", []):
        for edge in day.get("edges", []):
            if edge.get("status") != "verified":
                risks[("edge", tuple(edge.get("reasonCodes", [])))] += 1
            if edge.get("walkingSeconds") is None:
                risks[("walking", "WALKING_ROUTE_UNKNOWN")] += 1
            if edge.get("costComplete") is not True:
                risks[("budget", "TRANSPORT_COST_INCOMPLETE")] += 1
    return risks


def _valid_candidate(draft: dict[str, Any], baseline_visit_count: int, pool: dict[int, dict[str, Any]],
                     max_visits_per_day: int, baseline_ids: set[int]) -> bool:
    dates = {day["date"] for day in draft["days"]}
    visit_count = 0
    for day in draft["days"]:
        if len(day["places"]) > max_visits_per_day:
            return False
        seen_today: set[int] = set()
        for place in day["places"]:
            cid = place.get("contentid")
            if type(cid) is not int or cid in seen_today or (cid not in pool and cid not in baseline_ids):
                return False
            seen_today.add(cid)
            visit_count += 1
            candidate = pool.get(cid)
            if candidate is not None and day["date"] not in candidate.get("allowedDates", dates):
                return False
    # A route-order move cannot silently drop a current visit. Pool selection
    # may replace one with another eligible candidate, but count stays fixed.
    return visit_count == baseline_visit_count


def _phase_neighbors(current: dict[str, Any], pool: dict[int, dict[str, Any]], anchors: dict[tuple[str, int], int],
                     max_visits_per_day: int, phase: str, day_filter: int):
    dates = [d["date"] for d in current["days"]]
    used = {p["contentid"] for d in current["days"] for p in d["places"]}
    for day_index, section in enumerate(current["days"]):
        if day_index != day_filter:
            continue
        date = section["date"]
        places = section["places"]
        n = len(places)
        # Segment reversals (2-opt) work with asymmetric direction matrices;
        # the caller scores the full directed route after each reversal.
        for lo in range(n) if phase == "two_opt" else ():
            for hi in range(lo + 1, n):
                changed = copy.deepcopy(current)
                changed["days"][day_index]["places"][lo:hi + 1] = reversed(
                    changed["days"][day_index]["places"][lo:hi + 1])
                if any(changed["days"][day_index]["places"][expected]["contentid"] != cid
                       for (anchor_date, cid), expected in anchors.items() if anchor_date == date):
                    continue
                yield "two_opt", changed
        # Relocate one flexible visit within a day.
        for source in range(n) if phase == "relocate" else ():
            cid = places[source]["contentid"]
            if anchors.get((date, cid)) == source:
                continue
            for target in range(n):
                if target == source:
                    continue
                changed = copy.deepcopy(current)
                stop = changed["days"][day_index]["places"].pop(source)
                changed["days"][day_index]["places"].insert(target, stop)
                if any(changed["days"][day_index]["places"][expected]["contentid"] != anchor_id
                       for (anchor_date, anchor_id), expected in anchors.items() if anchor_date == date):
                    continue
                yield "relocate", changed
        # TOPTW-inspired date relocation retains the trip-wide visit set.
        for source, stop in enumerate(places) if phase == "date_reallocation" else ():
            cid = stop["contentid"]
            if (date, cid) in anchors:
                continue
            candidate = pool.get(cid)
            if (candidate or stop).get("planningRole") in {"meal_stop", "overnight_lodging", "overnight_camping"}:
                # Meals and overnight stays keep their original day; changing
                # their date can hide a lost meal behind an existing warning.
                continue
            allowed = set(candidate.get("allowedDates", dates)) if candidate else set(dates)
            for target_index, target in enumerate(current["days"]):
                if target_index == day_index or target["date"] not in allowed or len(target["places"]) >= max_visits_per_day:
                    continue
                changed = copy.deepcopy(current)
                moved = changed["days"][day_index]["places"].pop(source)
                changed["days"][target_index]["places"].append(moved)
                yield "date_reallocation", changed
        # Replace flexible tourism roles only. Role and hard feasibility checks
        # remain with the whole-plan evaluator; adjusted utility decides gains.
        if phase == "candidate_replace" and pool and places:
            for source, stop in enumerate(places):
                cid = stop["contentid"]
                if (date, cid) in anchors:
                    continue
                old = pool.get(cid)
                if old is None:
                    continue
                old_role = old.get("planningRole")
                if old_role not in _FLEXIBLE_ROLES | {"meal_stop"}:
                    continue
                for replacement, info in pool.items():
                    if replacement in used or date not in info.get("allowedDates", dates):
                        continue
                    new_role = info.get("planningRole")
                    if not ((old_role in _FLEXIBLE_ROLES and new_role in _FLEXIBLE_ROLES)
                            or (old_role == new_role == "meal_stop")):
                        continue
                    changed = copy.deepcopy(current)
                    changed["days"][day_index]["places"][source] = {
                        **{k: v for k, v in info.items() if k in {"contentid", "title", "planningRole", "adminCity", "coordinate", "estimatedStayMinutes", "categoryKey"}},
                        "visitPosition": source,
                    }
                    yield "candidate_replace", changed


def _neighbors(current, pool, anchors, max_visits_per_day):
    """Share the bounded evaluation budget across methods and trip days."""
    pending = deque(_phase_neighbors(current, pool, anchors, max_visits_per_day, phase, day)
                    for day in range(len(current["days"]))
                    for phase in ("two_opt", "relocate", "date_reallocation", "candidate_replace"))
    while pending:
        iterator = pending.popleft()
        try:
            neighbor = next(iterator)
        except StopIteration:
            continue
        yield neighbor
        pending.append(iterator)


def optimize_itinerary(draft: dict[str, Any], settings: dict[str, Any], candidate_pool: list[dict[str, Any]],
                       evaluate: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
                       *, max_visits_per_day: int = 8, max_evaluations: int = 24,
                       time_budget_seconds: float = 1.0) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Try a small set of local moves and retain only fully checked safe gains.

    `evaluate` returns (scheduled draft, public validation plan, adjusted settings).
    Every move is evaluated as a whole plan, sharing the caller's route cache and
    deadline. A new hard violation or unknown validation warning rejects a move.
    """
    baseline = copy.deepcopy(draft)
    pool = {row["contentid"]: row for row in candidate_pool
            if isinstance(row, dict) and type(row.get("contentid")) is int}
    ids = {p["contentid"] for day in draft["days"] for p in day["places"]}
    try:
        baseline_scheduled, baseline_plan, baseline_settings = evaluate(baseline)
    except Exception:
        return draft, {}, settings, {"fallback": "BASELINE_INVALID", "attemptedMoves": 0, "acceptedMoves": 0}
    initial_warnings = _warnings(baseline_plan)
    initial_violations = _violations(baseline_plan)
    initial_risks = _risk_counts(baseline_plan)
    current_draft, current_plan, current_settings = baseline_scheduled, baseline_plan, baseline_settings
    # The caller's captured anchors are the authority; evaluator output may
    # intentionally omit settings when used by a local/offline score harness.
    anchors = _anchored_positions(settings)
    anchor_ids = set(anchors)
    current_rank = _rank(current_draft, current_plan, pool, anchor_ids)
    attempted = accepted = 0
    utility_screened = 0
    attempted_by_phase = Counter()
    accepted_by_phase = Counter()
    deadline = time.monotonic() + max(0.01, min(time_budget_seconds, 1.0))
    fallback = None
    seen = {_visit_ids(current_draft)}
    for phase, neighbor in _neighbors(current_draft, pool, anchors, max_visits_per_day):
        if attempted >= max_evaluations or time.monotonic() >= deadline:
            fallback = "SEARCH_BUDGET_REACHED"
            break
        signature = _visit_ids(neighbor)
        if signature in seen:
            continue
        seen.add(signature)
        # Visit count must remain unchanged; the evaluator may reorder or replace,
        # but additions are refused because the current human-reviewed draft has
        # not yet established that an extra stop is desirable.
        if not _valid_candidate(neighbor, sum(len(day["places"]) for day in current_draft["days"]),
                                pool, max_visits_per_day, ids):
            continue
        # With no hard violation to repair, a lower-utility neighbor cannot win
        # the lexicographic objective even with a shorter route. Do not spend
        # the six-route budget on it before reaching useful diversity moves.
        # This check is outside the lazy generator so the wall-clock deadline
        # still applies to every screened candidate.
        if (not _violations(current_plan)
                and _preference_score(neighbor, pool, anchor_ids) < _preference_score(current_draft, pool, anchor_ids)):
            utility_screened += 1
            continue
        attempted += 1
        attempted_by_phase[phase] += 1
        try:
            scheduled, plan, adjusted = evaluate(neighbor)
        except Exception:
            continue
        warnings = _warnings(plan)
        violations = _violations(plan)
        risks = _risk_counts(plan)
        current_warnings = _warnings(current_plan)
        current_violations = _violations(current_plan)
        current_risks = _risk_counts(current_plan)
        if (warnings - initial_warnings or warnings - current_warnings
                or violations - initial_violations or violations - current_violations
                or risks - initial_risks or risks - current_risks):
            continue
        new_rank = _rank(scheduled, plan, pool, anchor_ids)
        if new_rank < current_rank:
            current_draft, current_plan, current_settings = scheduled, plan, adjusted
            current_rank = new_rank
            accepted += 1
            accepted_by_phase[phase] += 1
    if current_draft is baseline_scheduled:
        fallback = fallback or "NO_SAFE_IMPROVEMENT"
    metrics = {
        "baselineVisitCount": sum(len(day["places"]) for day in baseline_scheduled["days"]),
        "finalVisitCount": sum(len(day["places"]) for day in current_draft["days"]),
        "baselineTravelSeconds": _travel_seconds(baseline_plan),
        "finalTravelSeconds": _travel_seconds(current_plan),
        "baselinePreferenceScore": _preference_score(baseline_scheduled, pool, anchor_ids),
        "finalPreferenceScore": _preference_score(current_draft, pool, anchor_ids),
        "baselineAdjustedUtility": _preference_score(baseline_scheduled, pool, anchor_ids),
        "finalAdjustedUtility": _preference_score(current_draft, pool, anchor_ids),
        "baselineCategoryRepetitions": _category_repetitions(baseline_scheduled, pool, anchor_ids),
        "finalCategoryRepetitions": _category_repetitions(current_draft, pool, anchor_ids),
        "policyVersion": GENERATION_POLICY_VERSION,
        "attemptedMoves": attempted,
        "acceptedMoves": accepted,
        "utilityScreenedMoves": utility_screened,
        "attemptedMovesByPhase": dict(attempted_by_phase),
        "acceptedMovesByPhase": dict(accepted_by_phase),
        "method": "bounded-2opt-relocate-toptw-inspired",
        "optimalityGuaranteed": False,
    }
    if fallback:
        metrics["fallback"] = fallback
    return current_draft, current_plan, current_settings, metrics
