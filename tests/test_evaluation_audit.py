from types import SimpleNamespace

from app.evaluation_audit import audit_synthetic_plan


DAY = "2026-11-20"


def _catalog(contentid=1, role="attraction", *, fee=None, hours=None):
    place = {"contentid": contentid, "mapx": 127.0 + contentid / 100,
             "planningRole": role}
    detail = {"contentid": contentid}
    if fee is not None:
        detail["feeText"] = fee
    if hours is not None:
        detail["openingHoursText"] = hours
    return SimpleNamespace(places=[place], details=[detail])


def _node(kind, start, end, cid=None):
    result = {"kind": kind, "plannedStartAt": f"{DAY}T{start}:00+09:00",
              "plannedEndAt": f"{DAY}T{end}:00+09:00"}
    if kind in {"START", "END"}:
        result["coordinate"] = {"longitude": 127.0, "latitude": 36.6}
    if cid is not None:
        result["contentid"] = cid
    return result


def _plan(nodes):
    return {"validation": {"status": "verified", "valid": True},
            "days": [{"date": DAY, "nodes": nodes}]}


def _audit(plan, *, catalog=None, candidates=None, transport="car", preferences=None, settings=None):
    return audit_synthetic_plan(
        plan,
        catalog=catalog or _catalog(),
        conditions={"startDate": DAY, "endDate": DAY, "budget": 100_000,
                    "transport": transport},
        preferences=preferences or [],
        settings=settings or {"activityWindow": {"start": "09:00", "end": "18:00"}},
        eligible_candidates=candidates or [{"contentid": 1, "allowedDates": [DAY]}],
    )


def test_audit_ignores_verified_claim_and_flags_place_outside_eligible_pool():
    nodes = [_node("START", "09:00", "09:00"), _node("PLACE", "09:10", "10:00", 999),
             _node("END", "10:20", "10:20")]
    result = _audit(_plan(nodes))
    assert result["independentOfProductionValidation"] is True
    assert result["status"] == "violated"
    assert "PLACE_ID_UNKNOWN_OR_INVALID" in result["knownViolationCounts"]


def test_audit_flags_schedule_outside_activity_window_despite_verified_claim():
    nodes = [_node("START", "20:00", "20:00"), _node("PLACE", "20:10", "21:00", 1),
             _node("END", "21:20", "21:20")]
    result = _audit(_plan(nodes))
    assert result["status"] == "violated"
    assert result["knownViolationCounts"]["ACTIVITY_WINDOW_EXCEEDED"] >= 1


def test_walking_limit_is_checked_against_directed_synthetic_cost():
    catalog = _catalog(3)
    nodes = [_node("START", "09:00", "09:00"), _node("PLACE", "09:28", "10:00", 3),
             _node("END", "10:18", "10:18")]
    result = _audit(_plan(nodes), catalog=catalog,
                    candidates=[{"contentid": 3, "allowedDates": [DAY]}],
                    transport="walk", preferences=[{"walking": "20"}])
    assert result["knownViolationCounts"]["WALKING_LIMIT_EXCEEDED"] == 1
    assert "synthetic directed matrix" in result["routeFactSource"]


def test_missing_clock_fee_and_opening_facts_are_unknown_not_assumed_valid_or_free():
    place = {"kind": "PLACE", "contentid": 1}
    result = _audit(_plan([{"kind": "START", "coordinate": {"longitude": 127.0}}, place,
                           {"kind": "END", "coordinate": {"longitude": 127.0}}]))
    assert result["status"] == "unknown"
    assert result["unknownCounts"]["VISIT_CLOCK_UNKNOWN"] == 1
    assert result["unknownCounts"]["PLACE_FEE_UNKNOWN"] == 1
    assert result["unknownCounts"]["OPENING_HOURS_UNKNOWN"] == 1
    assert result["totalBudgetStatus"] == "unknown_other_expenses"
    assert result["knownPlaceSpendKrw"] == 0


def test_fixed_appointment_end_time_must_be_preserved():
    nodes = [_node("START", "09:00", "09:00"), _node("PLACE", "10:00", "11:01", 1),
             _node("END", "11:20", "11:20")]
    settings = {
        "activityWindow": {"start": "09:00", "end": "18:00"},
        "confirmedScheduleAnchors": [{"contentid": 1, "date": DAY, "kind": "appointment",
                                       "startTime": "10:00", "endTime": "11:00"}],
    }
    result = _audit(_plan(nodes), settings=settings)
    assert result["knownViolationCounts"]["FIXED_END_TIME_NOT_PRESERVED"] == 1


def test_independent_audit_checks_explicit_group_fee_against_budget():
    nodes = [_node("PLACE", "10:00", "11:00", 1)]
    result = _audit(_plan(nodes), catalog=_catalog(fee="전체 100001원"))
    assert result["knownViolationCounts"]["KNOWN_PLACE_FEES_EXCEED_BUDGET"] == 1


def test_meal_merely_overlapping_window_is_not_full_slot_compliance():
    nodes = [_node("PLACE", "10:00", "12:00", 1)]
    result = _audit(_plan(nodes), catalog=_catalog(role="meal_stop"))
    assert "MEAL_OUTSIDE_SYNTHETIC_SLOT" in result["knownViolationCounts"]
