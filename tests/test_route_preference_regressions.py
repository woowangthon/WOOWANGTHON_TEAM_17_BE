"""Regressions for atomic ambiguity, partial route evidence and safe search."""
import copy
import math

import pytest

from app.alns_repair import _safe_to_accept
from app.itinerary_optimizer import _rank
from app.route_preferences import apply_route_preferences, draft_preference_rules, route_preference_penalty
from test_route_preferences import _atom, _catalog, _compile, _plan


@pytest.mark.parametrize("operator", ["max", "exact"])
def test_confirmed_excess_stays_a_violation_with_other_unknown_places(operator):
    catalog = _catalog()
    catalog.places.append({"contentid": 4, "planningRole": "sightseeing_stop"})
    catalog.preferences.append({"contentid": 4, "preferenceTags": "자연"})
    atom = _atom(dimension="activity", target="자연", polarity="prefer", scope="trip",
                 frequency={"operator": operator, "count": 1, "period": "trip"})
    checked = apply_route_preferences(_plan([
        {"contentid": 1}, {"contentid": 4}, {"contentid": 3},
    ]), _compile(atom, catalog=catalog), catalog)
    assert checked["validation"]["status"] == "violated"
    assert any(item["code"] == "REQUIRED_PREFERENCE_UNSATISFIED" for item in checked["validation"]["violations"])


@pytest.mark.parametrize("change", [
    {"frequency": {"operator": "target", "count": 2, "period": "trip"}, "scope": "trip"},
    {"frequency": {"operator": "exact", "count": 2, "period": "trip"}, "scope": "day"},
    {"frequency": {"operator": "min", "count": 1, "period": "trip"}, "scope": "segment"},
])
def test_unsupported_frequency_never_silently_falls_back_to_presence(change):
    atom = _atom(dimension="activity", target="자연", polarity="prefer", **change)
    compiled = _compile(atom)
    assert compiled["issues"] == [{"field": "atomicPreferences", "code": "REQUIRED_PREFERENCE_UNSUPPORTED"}]
    assert compiled["countRules"] == []


def test_canonical_meal_count_and_exact_sightseeing_count_apply_to_correct_roles():
    meals = _atom(dimension="meal_plan", target="식사", polarity="limit",
                  frequency={"operator": "exact", "count": 1, "period": "day"})
    visits = _atom(frequency={"operator": "min", "count": 2, "period": "day"})
    compiled = _compile(meals, visits)
    assert not compiled["issues"]
    plan = _plan([{"contentid": 1}, {"contentid": 2}, {"contentid": 3}])
    checked = apply_route_preferences(plan, compiled, _catalog())
    assert not checked["validation"]["violations"]


def test_food_presence_cannot_be_satisfied_by_a_tag_on_an_unrelated_role():
    catalog = _catalog()
    catalog.preferences[0]["preferenceTags"] = "초밥"
    atom = _atom(dimension="food", target="초밥", polarity="prefer", scope="trip", frequency=None)
    compiled = _compile(atom, catalog=catalog)
    assert compiled["countRules"][0]["dimension"] == "tag_food"
    checked = apply_route_preferences(_plan([{"contentid": 1}]), compiled, catalog)
    assert checked["validation"]["status"] == "violated"


def test_soft_avoid_is_negative_support_and_segment_avoid_stays_unapplied():
    avoid = _atom(dimension="activity", target="자연", priority="unspecified",
                  polarity="avoid", frequency=None, scope="trip")
    compiled = _compile(avoid)
    assert compiled["predicates"][0]["polarity"] == "avoid"
    assert compiled["predicates"][0]["required"] is False
    segmented = _compile({**avoid, "scope": "segment"})
    assert not segmented["predicates"]
    assert segmented["softWarnings"]


@pytest.mark.parametrize("change", [
    {"polarity": "prefer"}, {"scope": "segment"},
    {"target": "등산 시간"}, {"value": 1e308}, {"approximate": True},
])
def test_unsupported_walking_semantics_never_become_a_hard_daily_max(change):
    atom = _atom(dimension="walking", target="도보", polarity="limit",
                 scope="trip", frequency=None, value=15, unit="분")
    atom.update(change)
    compiled = _compile(atom)
    assert compiled["issues"]
    assert not compiled["walkingRules"]


def test_verified_partial_walking_excess_cannot_be_hidden_by_an_unknown_leg():
    atom = _atom(dimension="walking", target="도보", polarity="limit",
                 scope="trip", frequency=None, value=15, unit="분")
    plan = _plan([], [901])
    plan["days"][0]["edges"].append({"status": "unknown", "walkingSeconds": None})
    checked = apply_route_preferences(plan, _compile(atom), _catalog())
    assert checked["validation"]["status"] == "violated"


def test_unknown_status_cannot_supply_verified_walking_seconds():
    atom = _atom(dimension="walking", target="도보", polarity="limit",
                 scope="trip", frequency=None, value=15, unit="분")
    plan = _plan([], [0])
    plan["days"][0]["edges"][0]["status"] = "unknown"
    checked = apply_route_preferences(plan, _compile(atom), _catalog())
    assert "REQUIRED_PREFERENCE_UNVERIFIED" in checked["validation"]["warnings"]


def test_alns_cannot_replace_a_checked_quota_with_unverified_evidence():
    baseline = _plan([], [0])
    candidate = copy.deepcopy(baseline)
    candidate["validation"]["warnings"] = ["REQUIRED_PREFERENCE_UNVERIFIED"]
    assert not _safe_to_accept(candidate, baseline)


def test_optimizer_soft_walking_uses_checked_route_seconds():
    atom = _atom(dimension="walking", target="도보", polarity="limit", priority="preferred",
                 scope="trip", frequency=None, value=15, unit="분")
    compiled = _compile(atom)
    draft = {"days": [{"date": "2026-12-12", "places": []}]}
    good = _rank(draft, _plan([], [900]), {}, route_preferences=compiled, catalog=_catalog())
    bad = _rank(draft, _plan([], [901]), {}, route_preferences=compiled, catalog=_catalog())
    assert good < bad


def test_unknown_soft_quota_never_scores_as_satisfied_zero():
    atom = _atom(dimension="activity", target="자연", polarity="prefer", priority="preferred",
                 scope="trip", frequency={"operator": "target", "count": 2, "period": "trip"})
    compiled = _compile(atom)
    partial = {"days": [{"date": "2026-12-12", "places": [{"contentid": 1}]}]}
    unknown = {"days": [{"date": "2026-12-12", "places": [{"contentid": 3}]}]}
    assert route_preference_penalty(partial, compiled, _catalog()) == 1
    assert math.isinf(route_preference_penalty(unknown, compiled, _catalog()))


def test_daily_and_trip_counts_are_distinct_and_zero_is_preserved():
    daily = _atom(frequency={"operator": "min", "count": 1, "period": "day"})
    plan = _plan([{"contentid": 1}, {"contentid": 3}])
    plan["days"].append({"date": "2026-12-13", "nodes": [], "edges": []})
    assert apply_route_preferences(plan, _compile(daily), _catalog())["validation"]["violations"]
    trip = {**daily, "scope": "trip", "frequency": {"operator": "min", "count": 2, "period": "trip"}}
    assert not apply_route_preferences(plan, _compile(trip), _catalog())["validation"]["violations"]
    zero = _atom(frequency={"operator": "max", "count": 0, "period": "day"})
    assert apply_route_preferences(plan, _compile(zero), _catalog())["validation"]["violations"]


def test_repeated_member_rules_are_deduplicated_in_the_id_only_prompt():
    compiled = _compile(_atom())
    compiled["countRules"] *= 100
    rules = draft_preference_rules(compiled, _catalog(), [1, 2, 3])
    assert len(rules) == 1
