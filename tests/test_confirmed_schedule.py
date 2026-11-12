"""Shared booking dates and actual final-order time anchors, offline only."""
import copy

from app.confirmed_preferences import group_schedule_anchors
from app.confirmed_schedule import arrange_overnight_stops
from app.plan_validation import validate_generated_plan
from test_routing_validation import COORD, Router


def booking():
    return {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "kind": "lodging", "scope": "group", "contentid": 22,
            "date": "2026-12-12", "time": "15:00", "endDate": "2026-12-14", "endTime": "08:00"}


def anchors():
    return group_schedule_anchors([{"memberRef": "p1", "scheduleItems": [booking()]}], {},
                                  {"members": [{"memberId": "owner", "role": "OWNER"}]})


def settings():
    return {"activityWindow": {"start": "09:00", "end": "18:00"},
            "departureAnchor": {"label": "출발", "coordinate": COORD}, "returnAnchor": None,
            "confirmedScheduleAnchors": anchors(), "reviewedCommonFields": []}


def draft():
    return {"days": [{"date": "2026-12-12", "places": [{"contentid": 22}, {"contentid": 11}]},
                     {"date": "2026-12-13", "places": [{"contentid": 22}, {"contentid": 11}]},
                     {"date": "2026-12-14", "places": [{"contentid": 11}]}]}


class Catalog:
    places = [{"contentid": 11, "planningRole": "sightseeing_stop"}, {"contentid": 22, "planningRole": "overnight_lodging"}]
    details = [{"contentid": 11, "estimatedStayMinutes": 60, "feeText": "무료", "openingHoursText": "24시간"},
               {"contentid": 22, "estimatedStayMinutes": None, "feeText": None, "openingHoursText": "09:00~22:00"}]
    def presentation_place(self, cid):
        return {"contentid": cid, "title": str(cid), "coordinate": COORD}


def test_checkout_date_is_not_another_night_or_duplicate_hotel_visit():
    actual = anchors()
    assert [a["date"] for a in actual] == ["2026-12-12", "2026-12-13"]
    assert actual[0]["startTime"] == "15:00" and actual[0]["checkoutTime"] is None
    assert actual[1]["endTime"] is None and actual[1]["checkoutTime"] == "08:00"


def test_hotel_moves_after_activities_and_rebinds_time_slots_without_new_facts():
    original_draft, original_settings = draft(), settings()
    before = copy.deepcopy((original_draft, original_settings))
    arranged, bound = arrange_overnight_stops(original_draft, original_settings)
    assert (original_draft, original_settings) == before
    assert [[p["contentid"] for p in d["places"]] for d in arranged["days"]] == [[11, 22], [11, 22], [11]]
    assert [a["position"] for a in bound["confirmedScheduleAnchors"]] == [1, 1]
    unknown_checkout = {**settings(), "confirmedScheduleAnchors": [{**anchors()[0], "endDate": None}]}
    arranged, bound = arrange_overnight_stops(draft(), unknown_checkout)
    assert [p["contentid"] for p in arranged["days"][0]["places"]] == [11, 22]
    assert bound["confirmedScheduleAnchors"][0]["endDate"] is None


def test_checkin_is_earliest_arrival_checkout_deadline_is_next_day_not_previous_morning():
    arranged, bound = arrange_overnight_stops(draft(), settings())
    result = validate_generated_plan(arranged, Catalog(), {"transport": "car", "expectedPeople": 1, "budget": 100000}, [{"walking": "40"}], bound, Router(seconds=60))
    first_hotel = result["days"][0]["nodes"][-1]
    final_hotel = result["days"][1]["nodes"][-1]
    assert first_hotel["contentid"] == 22 and first_hotel["locked"] is False
    assert first_hotel["estimatedStartAt"] == "2026-12-12T06:00:00.000Z"
    assert final_hotel["estimatedEndAt"] == "2026-12-13T23:00:00.000Z"
    assert not any(v["code"] in {"FIXED_END_MISSED", "PLACE_CLOSED_AT_VISIT"} for v in result["validation"]["violations"])


def test_unknown_route_does_not_gain_an_arrival_from_hotel_checkin_window():
    arranged, bound = arrange_overnight_stops(draft(), {**settings(), "departureAnchor": None})
    result = validate_generated_plan(arranged, Catalog(), {"transport": "car", "expectedPeople": 1, "budget": 100000}, [{"walking": "40"}], bound, Router())
    assert result["days"][0]["nodes"][-1]["estimatedStartAt"] is None
    assert result["days"][0]["nodes"][-1]["estimatedEndAt"] is None
