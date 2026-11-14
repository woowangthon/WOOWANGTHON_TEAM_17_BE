from app.day_schedule import build_scheduled_plan
from test_itinerary_optimizer import DAY, coordinate

NEXT_DAY = "2026-12-13"


class RepairCatalog:
    def __init__(self):
        self.places = [{"contentid": cid, "title": f"Stop {cid}", "planningRole": "sightseeing_stop"}
                       for cid in range(1, 6)]
        self.details = [{"contentid": cid, "estimatedStayMinutes": 60 if cid == 5 else 45,
                         "openingHoursText": "09:00~22:00", "feeText": "무료"}
                        for cid in range(1, 6)]

    def presentation_place(self, cid):
        return {"contentid": cid, "title": f"Stop {cid}", "coordinate": coordinate(cid)}


class FastRouter:
    def route(self, origin, destination, mode):
        left, right = (round((point["longitude"] - 127) * 1000) for point in (origin, destination))
        seconds = 3600 if left and right and ({left, right} & {1, 2}) else 300
        return {"status": "verified", "mode": mode, "durationSeconds": seconds, "distanceMeters": 100,
                "knownCostKrw": 0, "costComplete": True, "walkingSeconds": [], "geometry": [],
                "reasonCodes": []}


def joint_replacement_case():
    catalog = RepairCatalog()
    day = {"date": DAY, "places": [catalog.presentation_place(1), catalog.presentation_place(2),
                                    catalog.presentation_place(5)]}
    draft = {"days": [day, {"date": NEXT_DAY, "places": []}], "optimizationCandidates": [
        {"contentid": cid, "title": f"Stop {cid}", "planningRole": "sightseeing_stop",
         "allowedDates": [DAY, NEXT_DAY], "groupScore": score,
         "coordinate": coordinate(cid), "estimatedStayMinutes": 45 if cid <= 4 else 60,
         "categoryKey": f"category:{cid}"}
        for cid, score in ((1, 1.0), (2, .97), (3, .94), (4, .91), (5, .88))]}
    settings = {"activityWindow": {"start": "09:00", "end": "18:00"}, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": [{"kind": "appointment", "date": DAY, "contentid": 5,
                                                "position": 2, "startTime": "15:00", "endTime": "16:00"}],
                "departureAnchor": {"label": "start", "coordinate": coordinate(0)},
                "returnAnchor": {"label": "end", "coordinate": coordinate(9)}}
    return catalog, draft, settings


def test_scheduler_runs_alns_before_legacy_visit_drops_and_finds_joint_replacement():
    catalog, draft, settings = joint_replacement_case()
    fixed_candidate = {**draft, "days": [{"date": DAY, "places": [catalog.presentation_place(3),
                                                                        catalog.presentation_place(4),
                                                                        catalog.presentation_place(5)]},
                                           {"date": NEXT_DAY, "places": []}]}
    direct = build_scheduled_plan(fixed_candidate, catalog,
        {"transport": "car", "expectedPeople": 2, "budget": 10000}, [], settings, FastRouter(), optimize=False)
    assert len([node for node in direct["days"][0]["nodes"] if node["kind"] == "PLACE"]) == 3
    report = {}

    plan = build_scheduled_plan(draft, catalog, {"transport": "car", "expectedPeople": 2, "budget": 10000},
                                [], settings, FastRouter(), optimization_report=report)

    visits = [node["contentid"] for node in plan["days"][0]["nodes"] if node["kind"] == "PLACE"]
    assert len(visits) == 3, report
    assert set(visits) == {3, 4, 5}
    assert visits[2] == 5
    assert not plan["validation"]["violations"]
    assert report["alns"]["method"] == "adaptive-large-neighborhood-repair"
    assert report["alns"]["strictBaselineChecks"] == 1
    assert report["alns"]["baselineVisitCount"] == report["alns"]["finalVisitCount"] == 3
    assert report["searchEvaluationsUsed"] <= 24


def test_post_repair_optimizer_error_keeps_the_checked_repair(monkeypatch):
    from app import itinerary_optimizer
    catalog, draft, settings = joint_replacement_case()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic optional optimizer error")

    monkeypatch.setattr(itinerary_optimizer, "optimize_itinerary", unavailable)
    report = {}
    plan = build_scheduled_plan(draft, catalog,
        {"transport": "car", "expectedPeople": 2, "budget": 10000}, [], settings, FastRouter(),
        optimization_report=report)
    visits = [node["contentid"] for node in plan["days"][0]["nodes"] if node["kind"] == "PLACE"]
    assert set(visits) == {3, 4, 5} and visits[2] == 5
    assert not plan["validation"]["violations"]
    assert report["fallback"] == "OPTIMIZER_ERROR"
    assert report["alns"]["finalVisitCount"] == 3
