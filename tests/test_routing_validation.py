"""Provider parsing and KST/time/constraint checks using only synthetic facts."""
import copy
import json
from types import SimpleNamespace

import pytest

from app import routing
from app.plan_validation import validate_generated_plan

COORD = {"latitude": 36.6286, "longitude": 127.4341, "crs": "WGS84"}


def fake_http(monkeypatch, data, status=200):
    requests = []

    class HTTP:
        def __init__(self, host, **kwargs):
            requests.append([host, kwargs])

        def request(self, method, path, **kwargs):
            requests.append([method, path, kwargs])

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda _: json.dumps(data).encode())

        def close(self):
            pass

    monkeypatch.setattr(routing, "HTTPSConnection", HTTP)
    return requests


def test_walk_route_uses_header_key_lng_lat_and_no_invented_geometry(monkeypatch):
    requests = fake_http(monkeypatch, {"status": "OK", "route": {"properties": {"totalTime": 1200, "totalDistance": 1000},
                        "legs": [{"steps": [{"path": {"points": [[127.43, 36.62], [127.44, 36.63]]}}]}]}})
    router = routing.KakaoRouter("synthetic-secret")
    result = router.route(COORD, COORD, "walk")
    assert result["status"] == "verified" and result["walkingSeconds"] == [1200]
    assert result["geometry"][0] == {"latitude": 36.62, "longitude": 127.43}
    assert "start_x=127.4341" in requests[1][1] and "start_y=36.6286" in requests[1][1]
    assert "synthetic-secret" not in requests[1][1] and "synthetic-secret" not in repr(router)
    assert requests[1][2]["headers"]["Authorization"] == "KakaoAK synthetic-secret"


def test_transit_combines_continuous_walking_and_keeps_grouping_at_bus(monkeypatch):
    steps = [{"properties": {"type": kind, "time": seconds}} for kind, seconds in
             [("WALKING", 700), ("WALKING", 700), ("BUS", 500), ("WALKING", 30)]]
    fake_http(monkeypatch, {"status": "OK", "routes": [{"properties": {
        "totalTime": 1930, "totalDistance": 5000, "fare": {"value": 1400}}, "steps": steps}]})
    result = routing.KakaoRouter("synthetic").route(COORD, COORD, "transit")
    assert result["walkingSeconds"] == [1400, 30]
    assert result["knownCostKrw"] == 1400 and result["costComplete"] is True


@pytest.mark.parametrize("data", [{"status": "NO_RESULTS"}, {"status": "OK", "route": {}},
                                   {"status": "OK", "route": {"properties": {"totalTime": True, "totalDistance": 5}}}])
def test_bad_routes_are_unknown_not_zero_or_provider_text(monkeypatch, data):
    fake_http(monkeypatch, data)
    result = routing.KakaoRouter("synthetic").route(COORD, COORD, "walk")
    assert result["status"] == "unknown" and result["durationSeconds"] is None
    assert result["geometry"] == []


def test_charter_and_missing_key_never_call_provider(monkeypatch):
    monkeypatch.setattr(routing, "HTTPSConnection", lambda *_a, **_k: pytest.fail("provider must not run"))
    assert routing.KakaoRouter("synthetic").route(COORD, COORD, "charter")["status"] == "unknown"
    assert routing.KakaoRouter().route(COORD, COORD, "car")["reasonCodes"] == ["ROUTE_NOT_CONFIGURED"]


class Catalog:
    places = [{"contentid": 11, "planningRole": "sightseeing_stop"}]
    details = [{"contentid": 11, "estimatedStayMinutes": 60, "feeText": None, "openingHoursText": None}]

    def presentation_place(self, cid):
        return {"contentid": cid, "title": "합성 장소", "coordinate": COORD, "feeLabel": "요금 확인 필요"}


class Router:
    def __init__(self, seconds=61, walk=None, mode=None):
        self.seconds, self.walk, self.mode = seconds, walk, mode

    def route(self, origin, destination, mode):
        return {"status": "verified", "mode": self.mode or mode, "durationSeconds": self.seconds,
                "distanceMeters": 1000, "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": self.walk if self.walk is not None else [self.seconds], "geometry": [], "reasonCodes": []}


def run_validation(*, router=None, catalog=None, settings=None, conditions=None, preferences=None):
    settings = settings or {"activityWindow": {"start": "09:00", "end": "18:00"},
                            "departureAnchor": {"label": "출발", "coordinate": COORD},
                            "returnAnchor": {"label": "복귀", "coordinate": COORD}, "reviewedCommonFields": []}
    return validate_generated_plan({"days": [{"date": "2026-12-12", "places": [{"contentid": 11}]}]},
            catalog or Catalog(), conditions or {"transport": "walk", "expectedPeople": 1, "budget": 10000},
            preferences or [{"walking": "20"}], settings, router or Router())


def test_kst_projection_ceil_minutes_and_unknown_prices_hours():
    result = run_validation()
    place = result["days"][0]["nodes"][1]
    assert place["estimatedStartAt"] == "2026-12-12T00:02:00.000Z"
    assert place["estimatedEndAt"] == "2026-12-12T01:02:00.000Z"
    assert place["estimatedCostKrw"] is None and place["feeLabel"] == "요금 확인 필요"
    assert "openingHoursText" not in place
    assert result["validation"]["budget"]["status"] == "unknown"
    assert result["validation"]["totalCostKrw"] is None


def test_departure_time_and_confirmed_group_clock_anchor_reach_time_validation():
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "departureAnchor": None,
                "returnAnchor": None, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [{"date": "2026-12-12", "contentid": 11, "position": 0,
                                               "kind": "appointment", "startTime": "11:00",
                                               "endTime": "12:00", "endDate": None}]}
    result = run_validation(settings=settings, conditions={"transport": "walk", "expectedPeople": 1,
                              "budget": 10000, "departureTime": "10:00"})
    visit = result["days"][0]["nodes"][1]
    assert visit["estimatedStartAt"] == "2026-12-12T02:00:00.000Z"
    assert visit["estimatedEndAt"] == "2026-12-12T03:00:00.000Z"
    assert visit["locked"] is True


def test_lodging_checkin_time_is_not_treated_as_an_arrival_deadline():
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "departureAnchor": None,
                "returnAnchor": None, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [{"date": "2026-12-12", "contentid": 11, "position": 0,
                                               "kind": "lodging", "startTime": "10:00",
                                               "endTime": None, "endDate": "2026-12-13"}]}
    result = run_validation(settings=settings, conditions={"transport": "walk", "expectedPeople": 1,
                              "budget": 10000, "departureTime": "11:00"})
    assert "FIXED_START_MISSED" not in {item["code"] for item in result["validation"]["violations"]}


@pytest.mark.parametrize("seconds, expected", [(1200, "verified"), (1201, "violated")])
def test_walking_limit_is_per_leg_and_not_rounded_down(seconds, expected):
    result = run_validation(router=Router(seconds=seconds))
    assert result["validation"]["walking"]["status"] == expected


def test_missing_origin_does_not_create_an_arrival_chain():
    result = run_validation(settings={"activityWindow": {"start": "09:00", "end": "18:00"},
                                      "departureAnchor": None, "returnAnchor": None, "reviewedCommonFields": []})
    assert result["days"][0]["nodes"][1]["estimatedStartAt"] is None
    assert result["validation"]["route"]["status"] == "unknown"


def test_deadline_window_closure_and_known_budget_violation_are_hard_failures():
    catalog = Catalog()
    catalog.details = [{"contentid": 11, "estimatedStayMinutes": 60, "feeText": "1인 2000원", "openingHoursText": "09:00~09:30"}]
    settings = {"activityWindow": {"start": "09:00", "end": "09:30"},
                "departureAnchor": {"label": "출발", "coordinate": COORD},
                "returnAnchor": {"label": "복귀", "coordinate": COORD},
                "reviewedCommonFields": [{"field": "fixedSchedule", "anchors": [{"date": "2026-12-12", "contentid": 11,
                 "position": 0, "arrivalDeadline": "09:01"}]}]}
    result = run_validation(catalog=catalog, settings=settings,
                             conditions={"transport": "walk", "expectedPeople": 3, "budget": 5000})
    codes = {v["code"] for v in result["validation"]["violations"]}
    assert {"FIXED_DEADLINE_MISSED", "ACTIVITY_WINDOW_EXCEEDED", "PLACE_CLOSED_AT_VISIT", "KNOWN_COST_EXCEEDS_BUDGET"} <= codes
    assert result["validation"]["knownCostKrw"] == 6000


def test_waiting_until_fixed_start_is_not_treated_as_visiting_before_opening():
    catalog = Catalog()
    catalog.details = [{"contentid": 11, "estimatedStayMinutes": 60, "feeText": "무료", "openingHoursText": "10:00~18:00"}]
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"},
                "departureAnchor": {"label": "출발", "coordinate": COORD},
                "returnAnchor": {"label": "복귀", "coordinate": COORD},
                "reviewedCommonFields": [{"field": "fixedSchedule", "anchors": [{"date": "2026-12-12", "contentid": 11,
                   "position": 0, "startTime": "10:00"}]}]}
    result = run_validation(catalog=catalog, settings=settings)
    assert result["validation"]["openingHours"]["status"] == "verified"
    assert result["days"][0]["nodes"][1]["estimatedStartAt"] == "2026-12-12T01:00:00.000Z"
