from datetime import datetime, timedelta, timezone

import pytest

from app.day_schedule import ScheduleWindowError, build_scheduled_plan
from test_routing_validation import COORD, Router


class TripCatalog:
    def __init__(self, stays, meals=()):
        self.places = [{"contentid": cid, "planningRole": "meal_stop" if cid in meals else "sightseeing_stop"}
                       for cid in stays]
        self.details = [{"contentid": cid, "estimatedStayMinutes": stay} for cid, stay in stays.items()]

    def presentation_place(self, cid):
        return {"contentid": cid, "title": str(cid), "coordinate": {**COORD, "longitude": 127 + cid / 1000}}


def schedule(catalog, dates, anchors=(), end="20:00", departure_coord=COORD, seconds=3600):
    draft = {"days": [{"date": f"2026-12-{12+i}", "places": [catalog.presentation_place(cid) for cid in ids]}
                      for i, ids in enumerate(dates)]}
    settings = {"activityWindow": {"start": "09:00", "end": end}, "confirmedScheduleAnchors": list(anchors),
                "departureAnchor": {"label": "출발", "coordinate": departure_coord}, "reviewedCommonFields": []}
    return build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 1000000},
                                [], settings, Router(seconds=seconds))


def test_two_days_fit_transfers_stays_and_real_meals_without_hotel():
    catalog = TripCatalog({1: 150, 2: 150, 3: 60, 4: 180, 5: 60, 6: 90, 7: 60, 8: 60}, meals={3, 5, 7, 8})
    result = schedule(catalog, [[1, 2, 3, 4, 5], [6, 7, 8]], seconds=1200)
    kst = timezone(timedelta(hours=9))
    days = [[n for n in day["nodes"] if n["kind"] == "PLACE"] for day in result["days"]]
    assert len(days[0]) < 5
    for day, expected_meals in zip(days, ({3, 5}, {7, 8})):
        assert expected_meals.issubset({n["contentid"] for n in day})
        prev = None
        for node in day:
            start, finish = [datetime.fromisoformat(node[k]).astimezone(kst) for k in ("plannedStartAt", "plannedEndAt")]
            assert finish - start == timedelta(minutes=node["stayMinutes"])
            assert 9 <= start.hour and (finish.hour, finish.minute) <= (20, 0)
            if prev:
                assert start - prev >= timedelta(minutes=20)
            prev = finish
    assert "OVERNIGHT_LOCATION_UNRESOLVED" in result["validation"]["warnings"]
    assert "MEAL_DURATION_UNKNOWN" not in result["validation"]["warnings"]


def test_infeasible_fixed_appointment_is_reported_instead_of_dropped():
    anchors = [{"kind": "appointment", "date": "2026-12-12", "position": 1, "contentid": 2,
                "startTime": "09:15", "endTime": "10:15"}]
    with pytest.raises(ScheduleWindowError):
        schedule(TripCatalog({1: 90, 2: 60}), [[1, 2]], anchors)


def test_unknown_first_leg_cannot_prove_a_fixed_appointment_is_impossible():
    anchors = [{"kind": "appointment", "date": "2026-12-12", "position": 0, "contentid": 2,
                "startTime": "09:15", "endTime": "10:15"}]
    plan = schedule(TripCatalog({2: 60}), [[2]], anchors, departure_coord=None)
    stop = next(n for n in plan["days"][0]["nodes"] if n["kind"] == "PLACE")
    assert stop["plannedStartAt"] == "2026-12-12T00:15:00.000Z"
    assert "FIXED_TIME_UNVERIFIED" in plan["validation"]["warnings"]


def test_only_explicit_evening_booking_extends_the_day():
    anchors = [{"kind": "appointment", "date": "2026-12-12", "position": 0, "contentid": 2,
                "startTime": "20:30", "endTime": "21:15"}]
    plan = schedule(TripCatalog({2: 45, 3: 60}), [[2, 3]], anchors)
    stops = [n for n in plan["days"][0]["nodes"] if n["kind"] == "PLACE"]
    assert [n["contentid"] for n in stops] == [2]
    assert stops[0]["plannedEndAt"] == "2026-12-12T12:15:00.000Z"
    assert not any(v["code"] == "ACTIVITY_WINDOW_EXCEEDED" for v in plan["validation"]["violations"])
