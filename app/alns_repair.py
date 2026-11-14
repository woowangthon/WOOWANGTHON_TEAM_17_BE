"""Bounded adaptive destroy/repair for a strict, fully revalidated trip.

This is a heuristic repair step. A route estimate only orders insertions; the
caller-provided whole-plan evaluator is the sole feasibility authority.
"""
from __future__ import annotations

import copy
import hashlib
import math
import random
import time
from collections import Counter
from typing import Any, Callable

from .itinerary_optimizer import (
    _base_utility,
    _category_repetitions,
    _preference_score,
    _risk_counts,
    _travel_seconds,
    _violations,
)


_FLEXIBLE = {"sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop", "date_specific_event"}
_DESTROY = ("low_utility_related", "related", "seeded_random")
_REPAIR = ("greedy", "alternative_first")
_MAX_REMOVALS = 3
_BEAM_WIDTH = 2


class SearchBudget:
    """One evaluator-trial and wall-clock budget shared by ALNS and local search."""
    def __init__(self, max_trials: int = 24, seconds: float = 1.0):
        self.max_trials = max(0, int(max_trials))
        self.deadline = time.monotonic() + max(0.0, min(float(seconds), 1.0))
        self.used_trials = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_trials - self.used_trials)

    def reserve(self) -> bool:
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            return False
        self.used_trials += 1
        return True


def _role(place: dict[str, Any], pool: dict[int, dict[str, Any]]) -> str | None:
    return (pool.get(place.get("contentid")) or {}).get("planningRole", place.get("planningRole"))


def _compatible_role(left: str | None, right: str | None) -> bool:
    return ((left in _FLEXIBLE and right in _FLEXIBLE)
            or (left == right == "meal_stop"))


def _anchor_ids(settings: dict[str, Any]) -> set[tuple[str, int]]:
    result: set[tuple[str, int]] = set()
    for group in settings.get("reviewedCommonFields", []):
        result.update((a["date"], a["contentid"]) for a in group.get("anchors", [])
                      if type(a.get("contentid")) is int and isinstance(a.get("date"), str))
    result.update((a["date"], a["contentid"]) for a in settings.get("confirmedScheduleAnchors", [])
                  if type(a.get("contentid")) is int and isinstance(a.get("date"), str))
    return result


def _anchor_positions(settings: dict[str, Any]) -> dict[tuple[str, int], int]:
    result = {}
    for group in settings.get("reviewedCommonFields", []):
        if group.get("field") == "lodging":
            continue
        for anchor in group.get("anchors", []):
            if anchor.get("kind") != "lodging" and type(anchor.get("position")) is int:
                result[(anchor["date"], anchor["contentid"])] = anchor["position"]
    for anchor in settings.get("confirmedScheduleAnchors", []):
        if anchor.get("kind") == "lodging":
            continue
        if anchor.get("kind") != "lodging" and type(anchor.get("position")) is int:
            result[(anchor["date"], anchor["contentid"])] = anchor["position"]
    return result


def _hotel_facts(settings: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    facts = []
    for group in settings.get("reviewedCommonFields", []):
        if group.get("field") == "lodging":
            facts.extend(("reviewed", copy.deepcopy(a)) for a in group.get("anchors", []))
    facts.extend(("confirmed", copy.deepcopy(a)) for a in settings.get("confirmedScheduleAnchors", [])
                 if a.get("kind") == "lodging")
    return facts


def _hotel_constraints_preserved(source: dict[str, Any], source_settings: dict[str, Any],
                                 candidate: dict[str, Any], candidate_settings: dict[str, Any]) -> bool:
    before, after = _hotel_facts(source_settings), _hotel_facts(candidate_settings)
    immutable = lambda items: sorted((kind, repr({k: v for k, v in a.items() if k != "position"}))
                                     for kind, a in items)
    if immutable(before) != immutable(after):
        return False
    for draft, settings in ((source, source_settings), (candidate, candidate_settings)):
        by_day = {d["date"]: [p["contentid"] for p in d["places"]] for d in draft["days"]}
        for _, anchor in _hotel_facts(settings):
            places = by_day.get(anchor.get("date"), [])
            if places.count(anchor.get("contentid")) != 1 or places[-1] != anchor.get("contentid"):
                return False
    return True


def _fixed_visits_preserved(draft: dict[str, Any], expected: set[tuple[str, int]]) -> bool:
    actual = [(day["date"], place.get("contentid")) for day in draft.get("days", [])
              for place in day.get("places", []) if type(place.get("contentid")) is int]
    return all(actual.count(anchor) == 1 for anchor in expected)


def _anchor_positions_match(draft: dict[str, Any], expected: dict[tuple[str, int], int]) -> bool:
    by_date = {day["date"]: [place.get("contentid") for place in day["places"]]
               for day in draft.get("days", [])}
    return all((by_date.get(date, []).index(cid) if cid in by_date.get(date, []) else None) == position
               for (date, cid), position in expected.items())


def _visit_counts(draft: dict[str, Any]) -> dict[str, int]:
    return {d["date"]: len(d["places"]) for d in draft.get("days", [])}


def _meal_counts(draft: dict[str, Any], pool: dict[int, dict[str, Any]]) -> dict[str, int]:
    return {day["date"]: sum(_role(p, pool) == "meal_stop" for p in day["places"])
            for day in draft.get("days", [])}


def _distance_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    ca, cb = a.get("coordinate"), b.get("coordinate")
    if not isinstance(ca, dict) or not isinstance(cb, dict):
        return 30.0
    try:
        lat1, lon1 = math.radians(float(ca["latitude"])), math.radians(float(ca["longitude"]))
        lat2, lon2 = math.radians(float(cb["latitude"])), math.radians(float(cb["longitude"]))
        if not all(math.isfinite(x) for x in (lat1, lon1, lat2, lon2)):
            return 30.0
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))
    except (KeyError, TypeError, ValueError, OverflowError):
        return 30.0


def _position_cost(day_places: list[dict[str, Any]], position: int, candidate: dict[str, Any]) -> float:
    """A geometry-only tie-breaker; never used to accept an itinerary."""
    before = day_places[position - 1] if position else None
    after = day_places[position] if position < len(day_places) else None
    base = _distance_km(before, after) if before and after else 0.0
    added = ((_distance_km(before, candidate) if before else 0.0)
             + (_distance_km(candidate, after) if after else 0.0))
    return added - base


def _score(row: dict[str, Any], pool: dict[int, dict[str, Any]]) -> float:
    ranks = [r.get("selectionRank") for r in pool.values()]
    count = max((r for r in ranks if type(r) is int and r >= 0), default=-1) + 1
    return _base_utility(row, count if count else None)


def _destroy(draft: dict[str, Any], pool: dict[int, dict[str, Any]], anchors: set[tuple[str, int]],
             operator: str, rng: random.Random) -> list[dict[str, Any]]:
    choices = []
    for day in draft["days"]:
        for index, place in enumerate(day["places"]):
            cid = place.get("contentid")
            if (day["date"], cid) in anchors or _role(place, pool) not in _FLEXIBLE | {"meal_stop"}:
                continue
            choices.append({"date": day["date"], "index": index, "place": copy.deepcopy(place),
                            "role": _role(place, pool)})
    if len(choices) < 2:
        return []
    size = min(_MAX_REMOVALS, len(choices))
    if operator == "seeded_random":
        selected = rng.sample(choices, size)
    elif operator == "related":
        seed = max(choices, key=lambda x: (_score(pool.get(x["place"]["contentid"], {}), pool),
                                           -x["index"], x["place"]["contentid"]))
        seed_row = pool.get(seed["place"]["contentid"], seed["place"])
        selected = sorted(choices, key=lambda x: (
            0 if x["date"] == seed["date"] else 1,
            0 if (pool.get(x["place"]["contentid"], {}).get("categoryKey")
                  == seed_row.get("categoryKey")) else 1,
            _distance_km(seed_row, pool.get(x["place"]["contentid"], x["place"])),
            x["index"], x["place"]["contentid"]))[:size]
    else:  # weak utility and same-category redundancy
        category_counts: Counter = Counter()
        for day in draft["days"]:
            for place in day["places"]:
                key = pool.get(place["contentid"], {}).get("categoryKey")
                if key:
                    category_counts[(day["date"], key)] += 1
        selected = sorted(choices, key=lambda x: (
            0 if category_counts[(x["date"], pool.get(x["place"]["contentid"], {}).get("categoryKey"))] > 1 else 1,
            _score(pool.get(x["place"]["contentid"], {}), pool),
            x["date"], x["index"], x["place"]["contentid"]))[:size]
    return selected[:size]


def _candidate_options(victim: dict[str, Any], day_dates: list[str], current_ids: set[int],
                       pool: dict[int, dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    role = victim["role"]
    old_id = victim["place"]["contentid"]
    options = []
    old_row = pool.get(old_id, victim["place"])
    allowed_old = old_row.get("allowedDates", day_dates)
    target_dates = [victim["date"]] if role == "meal_stop" else day_dates
    for target_date in target_dates:
        if target_date in allowed_old:
            options.append((copy.deepcopy(victim["place"]), target_date))
    for cid, row in pool.items():
        new_role = row.get("planningRole")
        role_matches = (role in _FLEXIBLE and new_role in _FLEXIBLE) or (role == new_role == "meal_stop")
        if cid in current_ids or not role_matches:
            continue
        target_dates = [victim["date"]] if role == "meal_stop" else day_dates
        allowed_dates = set(row.get("allowedDates", day_dates))
        for target_date in target_dates:
            if target_date in allowed_dates:
                place = {key: copy.deepcopy(row[key]) for key in (
                    "contentid", "title", "planningRole", "adminCity", "coordinate", "estimatedStayMinutes",
                    "categoryKey") if key in row}
                options.append((place, target_date))
    return options


def _insert_options(partial: dict[str, Any], victim: dict[str, Any], options: list[tuple[dict[str, Any], str]],
                    pool: dict[int, dict[str, Any]], max_visits: int,
                    semantic_context: Any, repair_operator: str,
                    anchor_positions: dict[tuple[str, int], int]) -> list[tuple[float, dict[str, Any]]]:
    selected_ids = {p["contentid"] for day in partial["days"] for p in day["places"]}
    unique_rows = {}
    for place, _target_date in options:
        cid = place.get("contentid")
        if cid not in selected_ids and _compatible_role(_role(place, pool), victim["role"]):
            unique_rows.setdefault(cid, place)
    rows = list(unique_rows.values())
    # MMR is quadratic in candidate count. The candidate pool may have 200
    # entries; retain the strongest 48 eligible rows before semantic ranking.
    rows = sorted(rows, key=lambda row: (-_score(pool.get(row["contentid"], row), pool), row["contentid"]))[:48]
    if semantic_context is not None and getattr(semantic_context, "enabled", False):
        try:
            from .place_semantics import mmr_order
            row_order = mmr_order(rows, selected_ids, semantic_context,
                                  utility=lambda row: _score(pool.get(row["contentid"], row), pool),
                                  limit=8, deadline=time.monotonic() + 0.05)
            order = {p["contentid"]: i for i, p in enumerate(row_order)}
        except Exception:
            order = {}
    else:
        order = {}
    candidate_ranks = sorted(rows, key=lambda p: (
        0 if repair_operator == "alternative_first" and p["contentid"] != victim["place"]["contentid"] else 1,
        order.get(p["contentid"], len(order) + 1), -_score(pool.get(p["contentid"], p), pool),
        p["contentid"]))
    # Alternative-first ranks replacements ahead of reinserting the removed
    # item; acceptance still uses the normal whole-plan score.
    dates_by_place: dict[int, set[str]] = {}
    for place, target_date in options:
        dates_by_place.setdefault(place["contentid"], set()).add(target_date)
    candidate_ranks = candidate_ranks[:8]
    scored = []
    for day in partial["days"]:
        for place in candidate_ranks:
            cid = place["contentid"]
            for target_date in sorted(dates_by_place.get(cid, ()))[:3]:
                if target_date != day["date"] or len(day["places"]) >= max_visits:
                    continue
                for position in sorted(set((0, len(day["places"]), len(day["places"]) // 2))):
                    preserves_anchor_slots = True
                    for (anchor_date, anchor_id), target_position in anchor_positions.items():
                        if anchor_date != target_date:
                            continue
                        current_positions = [i for i, stop in enumerate(day["places"])
                                             if stop.get("contentid") == anchor_id]
                        if not current_positions:
                            continue
                        current_position = current_positions[0]
                        if ((current_position < target_position and position > current_position)
                                or (current_position == target_position and position <= current_position)):
                            preserves_anchor_slots = False
                            break
                    if not preserves_anchor_slots:
                        continue
                    changed = copy.deepcopy(partial)
                    section = changed["days"][[d["date"] for d in changed["days"]].index(target_date)]
                    section["places"].insert(position, copy.deepcopy(place))
                    utility = _score(pool.get(cid, place), pool)
                    if repair_operator == "alternative_first" and cid != victim["place"]["contentid"]:
                        utility += 0.25
                    for (anchor_date, anchor_id), target_position in anchor_positions.items():
                        if anchor_date != target_date:
                            continue
                        current_position = next((i for i, stop in enumerate(day["places"])
                                                 if stop.get("contentid") == anchor_id), None)
                        if current_position is not None and current_position < target_position and position <= current_position:
                            utility += 0.5
                            break
                    cost = _position_cost(day["places"], position, place)
                    scored.append((utility - 0.002 * min(500.0, cost), changed))
                    if len(scored) >= 18:
                        return sorted(scored, key=lambda x: (-x[0], repr(x[1])))
    return sorted(scored, key=lambda x: (-x[0], repr(x[1])))


def _repair_variants(draft: dict[str, Any], victims: list[dict[str, Any]], pool: dict[int, dict[str, Any]],
                     anchors: set[tuple[str, int]], anchor_positions: dict[tuple[str, int], int],
                     expected_count: int, expected_meals: dict[str, int],
                     max_visits: int, semantic_context: Any, repair_operator: str) -> list[dict[str, Any]]:
    base = copy.deepcopy(draft)
    for victim in sorted(victims, key=lambda x: (x["date"], -x["index"])):
        section = next(day for day in base["days"] if day["date"] == victim["date"])
        idx = next((i for i, place in enumerate(section["places"])
                    if place.get("contentid") == victim["place"].get("contentid")), None)
        if idx is None:
            return []
        section["places"].pop(idx)
    dates = [day["date"] for day in draft["days"]]
    current_ids = {p["contentid"] for day in base["days"] for p in day["places"]}
    victim_ids = {victim["place"].get("contentid") for victim in victims}
    beam = [base]
    for victim_index, victim in enumerate(victims):
        options = _candidate_options(victim, dates, current_ids, pool)
        options = [(place, date) for place, date in options
                   if place.get("contentid") not in victim_ids
                   or place.get("contentid") == victim["place"].get("contentid")]
        expanded = []
        for partial in beam:
            for _, changed in _insert_options(partial, victim, options, pool, max_visits,
                                               semantic_context, repair_operator, anchor_positions)[:6]:
                day_ids = [[p["contentid"] for p in day["places"]] for day in changed["days"]]
                if any(len(ids) != len(set(ids)) for ids in day_ids):
                    continue
                if not _fixed_visits_preserved(changed, anchors):
                    continue
                expected_partial = expected_count - (len(victims) - victim_index - 1)
                if (sum(_visit_counts(changed).values()) != expected_partial
                        or any(count > expected_meals.get(date, 0)
                               for date, count in _meal_counts(changed, pool).items())
                        or any(len(day["places"]) > max_visits for day in changed["days"])):
                    continue
                expanded.append(changed)
        # Stable top-K beam avoids combinatorial growth while retaining distinct
        # greedy and regret insertion branches.
        unique = {_signature(candidate): candidate for candidate in expanded}
        beam = list(unique.values())[:_BEAM_WIDTH]
        if not beam:
            return []
        current_ids = {p["contentid"] for day in beam[0]["days"] for p in day["places"]}
    return [candidate for candidate in beam if _meal_counts(candidate, pool) == expected_meals
            and _anchor_positions_match(candidate, anchor_positions)]


def _signature(draft: dict[str, Any]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return tuple((day["date"], tuple(place["contentid"] for place in day["places"])) for day in draft["days"])


def _safe_to_accept(plan: dict[str, Any], reference: dict[str, Any]) -> bool:
    if _violations(plan):
        return False
    # A repaired trip may not add to the original route, walking, time, budget,
    # opening-hours, or unknown-edge risks. Unknown never becomes zero.
    return not (_risk_counts(plan) - _risk_counts(reference))


def repair_itinerary(draft: dict[str, Any], settings: dict[str, Any], candidate_pool: list[dict[str, Any]],
                     evaluate: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
                     *, reference_plan: dict[str, Any], semantic_context: Any = None,
                     max_visits_per_day: int = 8, max_evaluations: int = 24,
                     time_budget_seconds: float = 1.0,
                     deadline: float | None = None, search_budget: SearchBudget | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None,
                                                            dict[str, Any], dict[str, Any]]:
    """Search bounded multi-stop replacements/date moves and fully validate each.

    `evaluate` shares the caller's route budget and validates the complete trip.
    It must reject appointment position/date and lodging-fact changes.
    """
    pool = {row["contentid"]: row for row in candidate_pool
            if isinstance(row, dict) and type(row.get("contentid")) is int}
    source = copy.deepcopy(draft)
    expected_count = sum(len(day["places"]) for day in source["days"])
    expected_visits = _visit_counts(source)
    expected_meals = _meal_counts(source, pool)
    anchors = _anchor_ids(settings)
    anchor_positions = _anchor_positions(settings)
    current_ids = {p["contentid"] for day in source["days"] for p in day["places"]}
    seed_data = repr(_signature(source)).encode("utf-8")
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_data).digest()[:8], "big"))
    search_deadline = min(deadline or float("inf"), search_budget.deadline if search_budget else
                          time.monotonic() + max(0.01, min(1.0, time_budget_seconds)))
    destroy_weights = {name: 1.0 for name in _DESTROY}
    repair_weights = {name: 1.0 for name in _REPAIR}
    tried_destroy: Counter = Counter()
    tried_repair: Counter = Counter()
    outcome_counts: Counter = Counter()
    attempted = accepted = feasible = 0
    best_draft = best_plan = best_settings = None
    reference_risk = _risk_counts(reference_plan)

    def choose(weights: dict[str, float]) -> str:
        total = sum(weights.values())
        pivot = rng.random() * total
        for name in sorted(weights):
            pivot -= weights[name]
            if pivot <= 0:
                return name
        return sorted(weights)[-1]

    iterations = 0
    while attempted < max(0, max_evaluations) and time.monotonic() < search_deadline:
        iterations += 1
        destroy = choose(destroy_weights)
        repair = choose(repair_weights)
        victims = _destroy(source if best_draft is None else best_draft, pool, anchors, destroy, rng)
        if not victims:
            destroy_weights[destroy] = max(0.2, destroy_weights[destroy] * 0.8)
            continue
        base = source if best_draft is None else best_draft
        base_count = _visit_counts(base)
        base_meals = _meal_counts(base, pool)
        candidates = _repair_variants(base, victims, pool, anchors, anchor_positions, expected_count, expected_meals,
                                      max_visits_per_day, semantic_context, repair)
        if not candidates:
            outcome_counts["unrepairable"] += 1
            destroy_weights[destroy] = max(0.2, destroy_weights[destroy] * 0.8)
            repair_weights[repair] = max(0.2, repair_weights[repair] * 0.8)
            continue
        outcome = 0.25
        for candidate in candidates:
            if attempted >= max_evaluations or time.monotonic() >= search_deadline:
                break
            if search_budget is not None and not search_budget.reserve():
                break
            attempted += 1
            tried_destroy[destroy] += 1
            tried_repair[repair] += 1
            try:
                scheduled, plan, adjusted = evaluate(copy.deepcopy(candidate))
            except Exception as exc:
                outcome_counts[f"infeasible:{type(exc).__name__}"] += 1
                outcome_counts["infeasible"] += 1
                continue
            if (sum(len(day["places"]) for day in scheduled.get("days", [])) != expected_count
                    or not _fixed_visits_preserved(scheduled, anchors)
                    or sum(_visit_counts(scheduled).values()) != expected_count
                    or _meal_counts(scheduled, pool) != base_meals
                    or not _hotel_constraints_preserved(draft, settings, scheduled, adjusted)
                    or _anchor_positions(adjusted) != anchor_positions
                    or not _safe_to_accept(plan, reference_plan)):
                outcome_counts["rejected_validation"] += 1
                continue
            feasible += 1
            outcome_counts["feasible"] += 1
            if best_draft is None:
                best_draft, best_plan, best_settings = scheduled, plan, adjusted
                outcome = 2.0
                continue
            try:
                from .itinerary_optimizer import _rank
                old_rank = _rank(best_draft, best_plan, pool, anchors, semantic_context)
                new_rank = _rank(scheduled, plan, pool, anchors, semantic_context)
            except Exception:
                old_rank = new_rank = (0, 0, float("inf"))
            if new_rank < old_rank:
                best_draft, best_plan, best_settings = scheduled, plan, adjusted
                accepted += 1
                outcome = 5.0
                outcome_counts["accepted"] += 1
            else:
                outcome = max(outcome, 1.0)
        # Rewarded operators gain weight; unused or infeasible operators decay.
        destroy_weights[destroy] = max(0.2, 0.75 * destroy_weights[destroy] + 0.25 * outcome)
        repair_weights[repair] = max(0.2, 0.75 * repair_weights[repair] + 0.25 * outcome)

    report = {
        "method": "adaptive-large-neighborhood-repair",
        "baselineVisitCount": expected_count,
        "finalVisitCount": sum(len(day["places"]) for day in best_draft["days"])
            if best_draft is not None else expected_count,
        "attemptedMoves": attempted,
        "acceptedMoves": accepted,
        "feasibleCandidates": feasible,
        "destroyOperatorsTried": dict(tried_destroy),
        "repairOperatorsTried": dict(tried_repair),
        "destroyOperatorWeights": {k: round(v, 4) for k, v in destroy_weights.items()},
        "repairOperatorWeights": {k: round(v, 4) for k, v in repair_weights.items()},
        "outcomes": dict(outcome_counts),
        "optimalityGuaranteed": False,
    }
    return best_draft, best_plan, best_settings if best_settings is not None else settings, report
