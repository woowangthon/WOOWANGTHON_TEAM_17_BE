from dataclasses import dataclass

from app.candidate_selection import build_confirmed_candidate_selection


@dataclass
class Snapshot:
    conditions: dict
    confirmedPreferences: list
    fingerprint: str = "atomic-route-test"

    def as_dict(self):
        return {"conditions": self.conditions, "confirmedPreferences": self.confirmedPreferences}


class Catalog:
    digest = "atomic-route-catalog"

    def __init__(self, count=210, *, matching_ids=()):
        self.places = [{"contentid": cid, "title": f"합성 장소 {cid}", "planningRole": "sightseeing_stop",
                        "recommendationUse": "MAIN_DESTINATION", "adminCity": "청주시",
                        "mapx": 127.0 + cid / 100000, "mapy": 36.0, "geoValidated": "yes"}
                       for cid in range(1, count + 1)]
        self.details = [{"contentid": cid, "planningStatusV2": "candidate_needs_live_validation"}
                        for cid in range(1, count + 1)]
        matches = set(matching_ids)
        self.preferences = [{"contentid": cid,
                             "preferenceTags": "required-tag" if cid in matches else "other-tag",
                             "vibeTags": None}
                            for cid in range(1, count + 1)]


def _snapshot():
    return Snapshot(
        {"region": "청주시", "startDate": "2026-12-12", "endDate": "2026-12-13",
         "expectedPeople": 1, "budget": 100000, "transport": "car", "departure": "청주역",
         "returnRoute": "청주역", "lodging": None, "fixedSchedule": None},
        [{"memberRef": "p1", "likes": None, "avoid": None, "avoidImportance": "preferred",
          "food": None, "foodImportance": "preferred", "walking": None, "scheduleItems": []}],
    )


def _evidence(predicates=()):
    return {"policyVersion": "confirmed-v1", "predicates": list(predicates), "issues": [], "digest": "test"}


def _required_rules(*, scope="trip", count=1):
    return {"policyVersion": "atomic-route-v1", "atomicMemberRefs": ["p1"], "predicates": [],
            "countRules": [{"memberRef": "p1", "dimension": "tag_activity", "tag": "required-tag",
                            "scope": scope, "operator": "min", "count": count,
                            "required": True, "polarity": "prefer"}],
            "issues": [], "softWarnings": [], "digest": "test"}


def _select(catalog, *, rules=None, evidence=None, max_candidates=5, common_settings=None, snapshot=None):
    return build_confirmed_candidate_selection(
        snapshot or _snapshot(), catalog, evidence or _evidence(), policy={
            "version": "candidate-v1", "maxCandidates": max_candidates, "reviewedRegionCities": []},
        common_settings=common_settings, route_preferences=rules,
    ).as_dict()


def test_low_ranked_sole_required_match_survives_candidate_cap():
    catalog = Catalog(matching_ids={210})
    selection = _select(catalog, rules=_required_rules(), max_candidates=5)
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert 210 in selected_ids
    assert len(selected_ids) == 5


def test_per_day_quota_retains_distinct_matches_for_each_day():
    catalog = Catalog(matching_ids={209, 210})
    selection = _select(catalog, rules=_required_rules(scope="day", count=1), max_candidates=2)
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert {209, 210}.issubset(selected_ids)
    assert len(selected_ids) == 2


def test_augmenting_assignment_uses_date_restricted_match_for_earlier_day():
    catalog = Catalog(matching_ids={1, 2})
    for cid, start, end in ((1, "2026-12-12", "2026-12-13"), (2, "2026-12-12", "2026-12-12")):
        place = next(row for row in catalog.places if row["contentid"] == cid)
        place.update(planningRole="date_specific_event", recommendationUse="EVENT")
        detail = next(row for row in catalog.details if row["contentid"] == cid)
        detail.update(planningStatusV2="trip_date_check_required", eventStartDate=start, eventEndDate=end)
    selection = _select(catalog, rules=_required_rules(scope="day", count=1), max_candidates=2)
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert {1, 2}.issubset(selected_ids)


def test_fixed_future_anchor_is_not_borrowed_as_flexible_earlier_day_match():
    catalog = Catalog(matching_ids={1, 2})
    snapshot = _snapshot()
    snapshot.conditions["fixedSchedule"] = "고정 일정"
    common = {"reviewedCommonFields": [{"field": "fixedSchedule", "reviewed": True,
              "anchors": [{"date": "2026-12-13", "contentid": 1, "position": 0}]}]}
    selection = build_confirmed_candidate_selection(
        snapshot, catalog, _evidence(), common,
        {"version": "candidate-v1", "maxCandidates": 2, "reviewedRegionCities": []},
        route_preferences=_required_rules(scope="day", count=1),
    ).as_dict()
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert {1, 2}.issubset(selected_ids)


def test_shared_candidate_can_satisfy_intersecting_rules_with_tight_cap():
    catalog = Catalog(count=3)
    for cid, tags in ((1, "alpha"), (2, "alpha|beta"), (3, "beta")):
        catalog.preferences[cid - 1]["preferenceTags"] = tags
    rules = _required_rules()
    rules["countRules"] = [
        {"memberRef": "p1", "dimension": "tag_activity", "tag": tag, "scope": "trip",
         "operator": "min", "count": 1, "required": True, "polarity": "prefer"}
        for tag in ("alpha", "beta")
    ]
    selection = _select(catalog, rules=rules, max_candidates=1)
    assert selection["readyForDraft"]
    assert [item["contentid"] for item in selection["eligible"]] == [2]


def test_same_place_anchored_on_two_dates_counts_as_two_trip_occurrences():
    catalog = Catalog(count=1, matching_ids={1})
    snapshot = _snapshot()
    snapshot.conditions["fixedSchedule"] = "두 날짜에 같은 장소 예약"
    common = {"reviewedCommonFields": [{"field": "fixedSchedule", "reviewed": True, "anchors": [
        {"date": day, "contentid": 1, "position": 0}
        for day in ("2026-12-12", "2026-12-13")
    ]}]}
    selected = build_confirmed_candidate_selection(
        snapshot, catalog, _evidence(), common,
        {"version": "candidate-v1", "maxCandidates": 1, "reviewedRegionCities": []},
        route_preferences=_required_rules(scope="trip", count=2),
    ).as_dict()
    assert selected["readyForDraft"]
    assert [row["contentid"] for row in selected["eligible"]] == [1]


def test_duplicate_atomic_soft_predicate_does_not_change_member_score():
    catalog = Catalog(count=2, matching_ids={1})
    predicate = {"memberRef": "p1", "field": "atomicPreferences", "tag": "required-tag",
                 "polarity": "prefer", "scope": "all", "required": False}
    another = {"memberRef": "p1", "field": "likes", "tag": "other-tag",
               "polarity": "prefer", "scope": "all", "required": False}
    evidence = _evidence([predicate, another])
    with_atomic = _select(catalog, rules={"predicates": [predicate], "countRules": []}, evidence=evidence,
                          max_candidates=2)
    without_append = _select(catalog, evidence=evidence, max_candidates=2)
    score_with = next(item["memberScores"]["p1"] for item in with_atomic["eligible"] if item["contentid"] == 1)
    score_without = next(item["memberScores"]["p1"] for item in without_append["eligible"] if item["contentid"] == 1)
    assert score_with == score_without == 0.5


def test_required_per_day_count_uses_each_candidates_allowed_dates():
    catalog = Catalog(count=20, matching_ids={20})
    event = next(row for row in catalog.places if row["contentid"] == 20)
    event.update(planningRole="date_specific_event", recommendationUse="EVENT")
    detail = next(row for row in catalog.details if row["contentid"] == 20)
    detail.update(planningStatusV2="trip_date_check_required", eventStartDate="2026-12-12", eventEndDate="2026-12-12")
    selection = _select(catalog, rules=_required_rules(scope="day", count=1), max_candidates=5)
    assert "REQUIRED_PREFERENCE_UNSATISFIED" in selection["unresolvedRequired"]
    assert not selection["readyForDraft"]


def test_low_ranked_market_match_survives_the_bounded_pool_without_promoting_shopping():
    catalog = Catalog(count=210)
    for row in catalog.preferences:
        row["preferenceTags"] = "favorite|쇼핑"
    market = next(row for row in catalog.places if row["contentid"] == 210)
    market.update(title="청주 육거리종합시장", planningRole="shopping_stop",
                  recommendationUse="SPECIAL_SHOPPING")
    market_rule = {"memberRef": "p1", "dimension": "market", "tag": None,
                   "scope": "trip", "operator": "min", "count": 1,
                   "required": False, "polarity": "prefer"}
    evidence = _evidence([{"memberRef": "p1", "tag": "favorite", "polarity": "prefer",
                           "scope": "all", "required": False}])

    selection = _select(catalog, rules={"predicates": [], "countRules": [market_rule]},
                        evidence=evidence, max_candidates=5)
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert 210 in selected_ids
    assert len(selected_ids) == 5


def test_market_match_can_share_cap_with_required_candidates_and_meals_when_space_exists():
    catalog = Catalog(count=20, matching_ids={20})
    for row in catalog.places[:3]:
        row.update(planningRole="meal_stop", recommendationUse="MEAL")
    market = next(row for row in catalog.places if row["contentid"] == 19)
    market.update(title="사창시장", planningRole="shopping_stop",
                  recommendationUse="GENERAL_SHOPPING")
    catalog.preferences[18]["preferenceTags"] = "쇼핑"
    rules = _required_rules()
    rules["countRules"].append({"memberRef": "p1", "dimension": "market", "tag": None,
                                "scope": "trip", "operator": "min", "count": 1,
                                "required": False, "polarity": "prefer"})
    selection = _select(catalog, rules=rules, max_candidates=5)
    selected_ids = {item["contentid"] for item in selection["eligible"]}
    assert selection["readyForDraft"]
    assert 19 in selected_ids and 20 in selected_ids
    assert sum(item["planningRole"] == "meal_stop" for item in selection["eligible"]) >= 3


def test_market_retention_does_not_displace_available_meals_when_cap_is_tight():
    catalog = Catalog(count=20, matching_ids={20})
    for row in catalog.places[:3]:
        row.update(planningRole="meal_stop", recommendationUse="MEAL")
    market = next(row for row in catalog.places if row["contentid"] == 19)
    market.update(title="사창시장", planningRole="shopping_stop",
                  recommendationUse="GENERAL_SHOPPING")
    catalog.preferences[18]["preferenceTags"] = "쇼핑"
    rules = _required_rules()
    rules["countRules"].append({"memberRef": "p1", "dimension": "market", "tag": None,
                                "scope": "trip", "operator": "min", "count": 1,
                                "required": False, "polarity": "prefer"})
    selection = _select(catalog, rules=rules, max_candidates=4)
    selected = selection["eligible"]
    assert selection["readyForDraft"]
    assert 20 in {item["contentid"] for item in selected}
    assert sum(item["planningRole"] == "meal_stop" for item in selected) == 3
    assert 19 not in {item["contentid"] for item in selected}


def test_soft_market_representative_yields_to_meals_when_all_remaining_capacity_is_used():
    catalog = _market_catalog(count=20, market_ids=(19,))
    for row in catalog.places[:3]:
        row.update(planningRole="meal_stop", recommendationUse="MEAL")
    selection = _select(catalog, rules={"predicates": [],
                        "countRules": [_market_quota_rule("trip", "min", 1)]}, max_candidates=2)
    rows = selection["eligible"]
    assert sum(row["planningRole"] == "meal_stop" for row in rows) == 2
    assert 19 not in {row["contentid"] for row in rows}


def test_market_maximum_and_zero_count_do_not_receive_positive_ranking_or_retention():
    catalog = Catalog(count=210)
    for row in catalog.preferences:
        row["preferenceTags"] = "favorite|쇼핑"
    market = next(row for row in catalog.places if row["contentid"] == 210)
    market.update(title="청주 육거리종합시장", planningRole="shopping_stop",
                  recommendationUse="SPECIAL_SHOPPING")
    evidence = _evidence([{"memberRef": "p1", "tag": "favorite", "polarity": "prefer",
                           "scope": "all", "required": False}])
    for operator, count in (("max", 1), ("min", 0)):
        market_rule = {"memberRef": "p1", "dimension": "market", "tag": None,
                       "scope": "trip", "operator": operator, "count": count,
                       "required": False, "polarity": "prefer"}
        selection = _select(catalog, rules={"predicates": [], "countRules": [market_rule]},
                            evidence=evidence, max_candidates=5)
        assert 210 not in {item["contentid"] for item in selection["eligible"]}


def _market_quota_rule(scope, operator, count):
    return {"memberRef": "p1", "dimension": "market", "tag": None,
            "scope": scope, "operator": operator, "count": count,
            "required": False, "polarity": "prefer"}


def _market_catalog(count=210, market_ids=(209, 210)):
    catalog = Catalog(count=count)
    for cid in market_ids:
        row = next(row for row in catalog.places if row["contentid"] == cid)
        row.update(title=f"합성 {cid}시장", planningRole="shopping_stop",
                   recommendationUse="SPECIAL_SHOPPING")
        catalog.preferences[cid - 1]["preferenceTags"] = "쇼핑"
    return catalog


def test_soft_market_quota_retains_distinct_candidates_for_each_scope_and_operator():
    cases = (("trip", "min", 2), ("trip", "exact", 2), ("trip", "target", 2),
             ("day", "min", 1), ("day", "exact", 1), ("day", "target", 1))
    for scope, operator, count in cases:
        catalog = _market_catalog()
        rules = {"predicates": [], "countRules": [_market_quota_rule(scope, operator, count)]}
        selection = _select(catalog, rules=rules, max_candidates=2)
        assert selection["readyForDraft"], (scope, operator, count)
        assert {209, 210}.issubset({row["contentid"] for row in selection["eligible"]}), (scope, operator, count)


def test_two_day_market_count_two_keeps_both_matching_ids_without_repeated_bonus():
    catalog = _market_catalog()
    selection = _select(catalog, rules={"predicates": [],
                        "countRules": [_market_quota_rule("trip", "min", 2)]}, max_candidates=2)
    rows = selection["eligible"]
    assert {row["contentid"] for row in rows} == {209, 210}
    assert all(row["memberScores"]["p1"] == row["groupScore"] == 0 for row in rows)


def test_soft_daily_market_quota_accounts_for_a_fixed_visit_date():
    catalog = _market_catalog()
    snapshot = _snapshot()
    snapshot.conditions["fixedSchedule"] = "고정 시장 방문"
    common = {"reviewedCommonFields": [{"field": "fixedSchedule", "reviewed": True,
              "anchors": [{"date": "2026-12-13", "contentid": 210, "position": 0}]}]}
    selection = _select(catalog, rules={"predicates": [], "countRules": [_market_quota_rule("day", "min", 1)]},
                        max_candidates=2, common_settings=common, snapshot=snapshot)
    assert selection["readyForDraft"]
    assert {209, 210}.issubset({row["contentid"] for row in selection["eligible"]})


def test_single_market_goal_does_not_add_per_candidate_score_bonus():
    catalog = _market_catalog()
    selection = _select(catalog, rules={"predicates": [], "countRules": [_market_quota_rule("trip", "min", 1)]},
                        max_candidates=2)
    market_rows = [row for row in selection["eligible"] if row["contentid"] in {209, 210}]
    assert len(market_rows) == 1
    assert market_rows[0]["memberScores"]["p1"] == market_rows[0]["groupScore"] == 0


def test_market_can_use_spare_capacity_above_the_meal_floor():
    catalog = _market_catalog(count=8, market_ids=(8,))
    for row in catalog.places[:7]:
        row.update(planningRole="meal_stop", recommendationUse="MEAL")
    rules = {"predicates": [], "countRules": [_market_quota_rule("trip", "min", 1)]}
    rows = _select(catalog, rules=rules, max_candidates=7)["eligible"]
    assert sum(row["planningRole"] == "meal_stop" for row in rows) == 6
    assert any(row["contentid"] == 8 for row in rows)
