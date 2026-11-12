"""Arrange confirmed overnight stops after activities without another AI call."""
from __future__ import annotations

import copy


def arrange_overnight_stops(draft, settings):
    """Permute validated places only, then bind time anchors to their real slots.

    The provider's fixed slots ensure that booked places are present. Their
    final slots are derived here so a hotel is not the morning's first activity.
    No new place, date, duration, or catalogue fact is invented.
    """
    result, adjusted = copy.deepcopy((draft, settings))
    anchors = adjusted.get("confirmedScheduleAnchors", [])
    by_date = {day["date"]: day for day in result["days"]}
    overnight_by_date = {}
    for anchor in anchors:
        if anchor.get("kind") == "lodging":
            overnight_by_date.setdefault(anchor["date"], set()).add(anchor["contentid"])
    if any(len(ids) > 1 for ids in overnight_by_date.values()):
        raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
    for day in result["days"]:
        overnight_ids = overnight_by_date.get(day["date"], set())
        day["places"] = [place for place in day["places"] if place["contentid"] not in overnight_ids] + [place for place in day["places"] if place["contentid"] in overnight_ids]
        for position, place in enumerate(day["places"]):
            place["visitPosition"] = position
    all_anchors = list(anchors)
    for common in adjusted.get("reviewedCommonFields", []):
        all_anchors.extend(common.get("anchors", []))
    for anchor in all_anchors:
        day = by_date.get(anchor["date"])
        positions = [i for i, place in enumerate(day["places"] if day else []) if place["contentid"] == anchor["contentid"]]
        if len(positions) != 1:
            raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
        anchor["position"] = positions[0]
    return result, adjusted
