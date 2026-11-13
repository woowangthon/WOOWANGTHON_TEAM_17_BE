"""Synthetic regressions for the verified car-leg time cap."""
import time

import pytest

from app.day_schedule import ScheduleWindowError, build_scheduled_plan
from app.plan_validation import validate_generated_plan
from app.routing import unknown_route
from test_day_schedule import TripCatalog, schedule
from test_routing_validation import COORD


class FixedSecondsRouter:
    def __init__(self, seconds, mode=None):
        self.seconds = seconds
        self.mode = mode

    def route(self, origin, destination, mode):
        return {"status": "verified", "mode": self.mode or mode,
                "durationSeconds": self.seconds, "distanceMeters": 1000,
                "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": [], "geometry": [], "reasonCodes": []}


class UnknownRouter:
    def route(self, origin, destination, mode):
        return unknown_route(mode, "ROUTE_UNAVAILABLE")


class LateOverlongRouter(FixedSecondsRouter):
    def __init__(self):
        super().__init__(0)
        self.calls = 0

    def route(self, origin, destination, mode):
        self.calls += 1
        if self.calls == 2:
            time.sleep(0.1)
            return super().route(origin, destination, mode) | {"durationSeconds": 2401}
        return super().route(origin, destination, mode)


def validation_for(seconds, *, mode="car", days=None, anchors=(), router=None, timeout_seconds=90):
    ids = sorted({cid for day in (days or [[1, 2]]) for cid in day})

    lodging_ids = {anchor.get("contentid") for anchor in anchors if anchor.get("kind") == "lodging"}

    class Catalog:
        details = [{"contentid": cid, "estimatedStayMinutes": 30, "feeText": "무료",
                    "openingHoursText": "24시간"} for cid in ids]

        def presentation_place(self, cid):
            return {"contentid": cid, "title": str(cid),
                    "coordinate": {**COORD, "longitude": COORD["longitude"] + cid / 10000}}

    catalog = Catalog()
    catalog.places = [{"contentid": cid, "planningRole": "overnight_lodging" if cid in lodging_ids else "sightseeing_stop"}
                      for cid in ids]
    draft = {"days": [{"date": f"2026-12-{12+i}", "places": [{"contentid": cid} for cid in visit_ids]}
                       for i, visit_ids in enumerate(days or [[1, 2]])]}
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "home", "coordinate": COORD},
                "returnAnchor": {"label": "home", "coordinate": COORD},
                "reviewedCommonFields": [], "confirmedScheduleAnchors": list(anchors)}
    return validate_generated_plan(draft, catalog,
            {"transport": mode, "expectedPeople": 1, "budget": 100000}, [], settings,
            router or FixedSecondsRouter(seconds), timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("seconds,violated", [(2399, False), (2400, False), (2401, True)])
def test_car_cap_uses_raw_seconds_for_place_to_place(seconds, violated):
    result = validation_for(seconds)
    codes = {item["code"] for item in result["validation"]["violations"]}
    assert ("CAR_LEG_TIME_LIMIT_EXCEEDED" in codes) is violated
    assert (result["validation"]["time"]["status"] == "violated") is violated


@pytest.mark.parametrize("mode", ["walk", "transit"])
def test_non_car_modes_have_no_car_leg_cap(mode):
    result = validation_for(2401, mode=mode)
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" not in {item["code"] for item in result["validation"]["violations"]}


def test_long_home_departure_and_final_return_legs_are_excluded():
    result = validation_for(3600, days=[[1]])
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" not in {item["code"] for item in result["validation"]["violations"]}


def test_overnight_hotel_to_next_day_place_is_a_capped_leg():
    anchors = [{"date": "2026-12-12", "endDate": "2026-12-13", "contentid": 1,
                "position": 0, "kind": "lodging", "startTime": "17:00"}]
    result = validation_for(2401, days=[[1], [2]], anchors=anchors)
    found = [item for item in result["validation"]["violations"]
             if item["code"] == "CAR_LEG_TIME_LIMIT_EXCEEDED"]
    assert found == [{"code": "CAR_LEG_TIME_LIMIT_EXCEEDED", "date": "2026-12-13", "contentid": 2}]


def test_schedule_repairs_flexible_overlong_place_leg_by_removing_a_stop():
    result = schedule(TripCatalog({1: 30, 2: 30}), [[1, 2]])
    stops = [node["contentid"] for node in result["days"][0]["nodes"] if node["kind"] == "PLACE"]
    assert len(stops) == 1
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" not in {item["code"] for item in result["validation"]["violations"]}


def test_schedule_fails_when_overlong_leg_is_between_fixed_bookings():
    anchors = [{"kind": "appointment", "date": "2026-12-12", "position": i,
                "contentid": cid, "startTime": "10:00" if i == 0 else "12:00",
                "endTime": "11:00" if i == 0 else "13:00"}
               for i, cid in enumerate((1, 2))]
    with pytest.raises(ScheduleWindowError):
        schedule(TripCatalog({1: 30, 2: 30}), [[1, 2]], anchors)


def test_car_cap_repair_cannot_remove_a_needed_meal_to_hide_a_long_leg():
    with pytest.raises(ScheduleWindowError):
        schedule(TripCatalog({1: 60, 2: 60}, meals={1, 2}), [[1, 2]])


def test_unknown_car_leg_stays_unknown_and_is_never_treated_as_zero_or_violation():
    result = validation_for(0, router=UnknownRouter())
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" not in {item["code"] for item in result["validation"]["violations"]}
    assert result["days"][0]["edges"][0]["status"] == "unknown"
    assert result["days"][0]["edges"][0]["durationSeconds"] is None
    assert result["validation"]["time"]["status"] == "unknown"


def test_timeout_unknown_fallback_does_not_hide_a_known_overlong_result():
    result = validation_for(0, router=LateOverlongRouter(), timeout_seconds=0.05)
    codes = {item["code"] for item in result["validation"]["violations"]}
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" in codes
    assert result["days"][0]["edges"][1]["status"] == "unknown"


def test_strict_candidate_schedule_rejects_overlong_place_leg():
    catalog = TripCatalog({1: 30, 2: 30})
    draft = {"days": [{"date": "2026-12-12", "places": [
        catalog.presentation_place(1), catalog.presentation_place(2)]}]}
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "home", "coordinate": COORD},
                "reviewedCommonFields": [], "confirmedScheduleAnchors": []}
    plan = build_scheduled_plan(draft, catalog,
        {"transport": "car", "expectedPeople": 1, "budget": 100000}, [], settings,
        FixedSecondsRouter(2401), optimize=True)
    # The baseline repair removes the flexible visit, so an accepted result can
    # never contain the invalid edge even if optimizer evaluation falls back.
    assert "CAR_LEG_TIME_LIMIT_EXCEEDED" not in {item["code"] for item in plan["validation"]["violations"]}
