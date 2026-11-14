"""Offline ablation of scheduler recovery and semantic diversity.

Handwritten drafts, synthetic 768-D vectors, synthetic directed route times.
No database, real users, Gemini drafting, embedding provider, or map requests.
Run from the repository: .venv/Scripts/python.exe scripts/evaluate_generation_search.py
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.day_schedule import ScheduleWindowError, build_scheduled_plan
from app.place_semantics import SemanticContext, semantic_redundancy


DAY, NEXT_DAY = "2026-12-12", "2026-12-13"


def coordinate(contentid):
    return {"latitude": 36.0, "longitude": 127.0 + contentid / 1000, "crs": "WGS84"}


class Catalog:
    def __init__(self, stays):
        self.places = [{"contentid": cid, "title": f"합성 장소 {cid}", "planningRole": "sightseeing_stop",
                        "mapx": coordinate(cid)["longitude"], "mapy": 36.0, "geoValidated": "yes"}
                       for cid in stays]
        self.details = [{"contentid": cid, "estimatedStayMinutes": minutes,
                         "openingHoursText": "09:00~22:00", "feeText": "무료"} for cid, minutes in stays.items()]

    def presentation_place(self, contentid):
        return {"contentid": contentid, "title": f"합성 장소 {contentid}",
                "planningRole": "sightseeing_stop", "coordinate": coordinate(contentid)}


class Router:
    def __init__(self, blocked=()):
        self.blocked, self.calls = set(blocked), 0

    def route(self, origin, destination, mode):
        self.calls += 1
        left, right = (round((point["longitude"] - 127) * 1000) for point in (origin, destination))
        seconds = 3600 if left and right and ({left, right} & self.blocked) else 300
        return {"status": "verified", "mode": mode, "durationSeconds": seconds,
                "distanceMeters": 1000, "knownCostKrw": 0, "costComplete": True,
                "walkingSeconds": [0], "geometry": [], "reasonCodes": []}


def make_case(name, stays, days, *, anchors=(), end="18:00", blocked=(), allowed=None):
    catalog = Catalog(stays)
    pool = [{**catalog.presentation_place(cid), "allowedDates": (allowed or {}).get(cid, [DAY, NEXT_DAY]),
             "groupScore": max(0.1, 1.0 - 0.03 * (cid - 1)), "categoryKey": f"distinct:{cid}"}
            for cid in stays]
    draft = {"days": [{"date": day, "places": [catalog.presentation_place(cid) for cid in ids]}
                      for day, ids in days], "optimizationCandidates": pool, "mealCandidates": []}
    settings = {"activityWindow": {"start": "09:00", "end": end}, "reviewedCommonFields": [],
                "confirmedScheduleAnchors": list(anchors),
                "departureAnchor": {"label": "합성 출발지", "coordinate": coordinate(0)},
                "returnAnchor": {"label": "합성 복귀지", "coordinate": coordinate(0)}}
    vectors = {}
    for index, cid in enumerate(stays):
        vector = [0.0] * 768
        vector[0 if name == "semantic_diversity" and cid in {1, 2} else index] = 1.0
        vectors[cid] = tuple(vector)
    return {"name": name, "catalog": catalog, "pool": pool, "draft": draft,
            "settings": settings, "blocked": blocked,
            "context": SemanticContext(True, "SYNTHETIC_FIXTURE", vectors=vectors),
            "feasible": name != "impossible_appointments"}


def cases():
    return [
        make_case("joint_two_replacements", {1: 45, 2: 45, 3: 45, 4: 45, 5: 60}, [(DAY, [1, 2, 5])],
                  anchors=[{"kind": "appointment", "date": DAY, "contentid": 5, "position": 2,
                            "startTime": "15:00", "endTime": "16:00"}], blocked=(1, 2)),
        make_case("date_reallocation", {1: 150, 2: 150, 3: 30}, [(DAY, [1, 2]), (NEXT_DAY, [3])],
                  end="13:00", allowed={1: [DAY], 2: [DAY, NEXT_DAY], 3: [NEXT_DAY]}),
        make_case("semantic_diversity", {1: 45, 2: 45, 3: 45}, [(DAY, [1, 2])]),
        make_case("impossible_appointments", {1: 60, 2: 60}, [(DAY, [1, 2])],
                  anchors=[{"kind": "appointment", "date": DAY, "contentid": cid, "position": index,
                            "startTime": time, "endTime": end}
                           for index, (cid, time, end) in enumerate([(1, "09:00", "10:00"), (2, "09:10", "10:10")])]),
    ]


def evaluate():
    rows = []
    for case in cases():
        for variant, alns, semantic in [("existing", False, False), ("alns_only", True, False),
                                         ("semantic_only", False, True), ("combined", True, True)]:
            draft, settings = copy.deepcopy((case["draft"], case["settings"]))
            router, metrics = Router(case["blocked"]), {}
            row = {"case": case["name"], "variant": variant, "expectedFeasible": case["feasible"],
                   "requestedVisits": sum(len(day["places"]) for day in draft["days"])}
            try:
                plan = build_scheduled_plan(draft, case["catalog"],
                    {"transport": "car", "expectedPeople": 1, "budget": 1_000_000}, [], settings, router,
                    use_alns=alns, semantic_context=case["context"] if semantic else None,
                    optimization_report=metrics)
                selected = {"days": [{"date": day["date"], "places": [node for node in day["nodes"] if node["kind"] == "PLACE"]}
                                     for day in plan["days"]]}
                edges = [edge for day in plan["days"] for edge in day["edges"]]
                row.update({"generated": True, "visits": sum(len(day["places"]) for day in selected["days"]),
                            "visitIds": [[p["contentid"] for p in day["places"]] for day in selected["days"]],
                            "knownViolations": len(plan["validation"]["violations"]),
                            "validationStatus": plan["validation"]["status"],
                            "unknownDomains": [domain for domain in ("route", "walking", "time", "budget", "openingHours")
                                               if plan["validation"][domain]["status"] == "unknown"],
                            "warnings": plan["validation"]["warnings"],
                            "travelSeconds": sum(edge["durationSeconds"] for edge in edges)
                                if all(edge["status"] == "verified" for edge in edges) else None,
                            "semanticRedundancy": semantic_redundancy(selected,
                                {p["contentid"]: p for p in case["pool"]}, case["context"],
                                {(a["date"], a["contentid"]) for a in case["settings"]["confirmedScheduleAnchors"]})})
            except ScheduleWindowError:
                row.update({"generated": False, "visits": 0, "knownViolations": None,
                            "travelSeconds": None, "semanticRedundancy": None,
                            "validationStatus": None, "unknownDomains": None, "warnings": None})
            row["routeCalls"] = router.calls
            row["searchMetrics"] = metrics
            rows.append(row)
    return {"data": "4 synthetic cases x 4 variants; handcrafted vectors and route times; no real users/providers",
            "results": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(evaluate(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
