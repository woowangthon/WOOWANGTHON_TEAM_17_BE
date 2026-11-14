"""Independent checks for schedules produced by the offline AI evaluator.

Only declared synthetic facts are checked here. This is not a production route
validator, and the fixture's transfer minutes are not real road estimates.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .generation_policy import CAR_LEG_MAX_SECONDS

KST = timezone(timedelta(hours=9))


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
    except (ValueError, OverflowError):
        return None


def _trip_dates(conditions: dict[str, Any]) -> list[str] | None:
    try:
        first = date.fromisoformat(conditions["startDate"])
        last = date.fromisoformat(conditions["endDate"])
        count = (last - first).days
        if not 0 <= count <= 30:
            return None
        return [(first + timedelta(days=n)).isoformat() for n in range(count + 1)]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _anchors(settings: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for group in settings.get("reviewedCommonFields", []):
        if not isinstance(group, dict):
            continue
        for anchor in group.get("anchors", []):
            if isinstance(anchor, dict):
                result.append({**anchor, "kind": anchor.get("kind") or group.get("field")})
    result.extend(x for x in settings.get("confirmedScheduleAnchors", []) if isinstance(x, dict))
    return result


def _fixture_index(node: dict[str, Any], places: dict[int, dict[str, Any]]) -> int | None:
    if node.get("kind") in {"START", "END"}:
        coordinate = node.get("coordinate")
        try:
            return 0 if abs(float(coordinate["longitude"]) - 127.0) < 1e-8 else None
        except (TypeError, KeyError, ValueError, OverflowError):
            return None
    cid = node.get("contentid")
    if type(cid) is not int or cid not in places:
        return None
    try:
        return cid if abs(float(places[cid]["mapx"]) - (127.0 + cid / 100)) < 1e-7 else None
    except (TypeError, KeyError, ValueError, OverflowError):
        return None


def _transfer_minutes(origin: int, destination: int) -> int:
    return 7 + ((origin * 13 + destination * 7 + origin * destination * 3) % 28)


def audit_synthetic_plan(
    plan: dict[str, Any], *, catalog: Any, conditions: dict[str, Any],
    preferences: list[dict[str, Any]], settings: dict[str, Any],
    eligible_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return known fixture violations and unknown facts without trusting plan flags."""
    violations: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []

    def add(target: list[dict[str, Any]], code: str, day: str | None = None,
            cid: int | None = None) -> None:
        item: dict[str, Any] = {"code": code}
        if day is not None:
            item["date"] = day
        if cid is not None:
            item["contentid"] = cid
        target.append(item)

    def fail(code: str, day: str | None = None, cid: int | None = None) -> None:
        add(violations, code, day, cid)

    def unsure(code: str, day: str | None = None, cid: int | None = None) -> None:
        add(unknowns, code, day, cid)

    places = {x["contentid"]: x for x in getattr(catalog, "places", [])
              if isinstance(x, dict) and type(x.get("contentid")) is int}
    details = {x["contentid"]: x for x in getattr(catalog, "details", [])
               if isinstance(x, dict) and type(x.get("contentid")) is int}
    pool = {x["contentid"]: x for x in eligible_candidates
            if isinstance(x, dict) and type(x.get("contentid")) is int}
    expected_dates = _trip_dates(conditions)
    sections = plan.get("days") if isinstance(plan, dict) else None
    if not isinstance(sections, list):
        sections = []
        fail("PLAN_DAYS_MISSING")
    if expected_dates is None:
        unsure("TRIP_DATE_RANGE_UNKNOWN")
    elif [x.get("date") for x in sections if isinstance(x, dict)] != expected_dates:
        fail("TRIP_DATES_NOT_PRESERVED")

    window = settings.get("activityWindow")
    try:
        activity_start = time.fromisoformat(window["start"])
        activity_end = time.fromisoformat(window["end"])
    except (TypeError, KeyError, ValueError):
        activity_start, activity_end = time(9), time(18)
        unsure("ACTIVITY_WINDOW_UNKNOWN")

    anchors = _anchors(settings)
    anchor_ids = {x.get("contentid") for x in anchors if type(x.get("contentid")) is int}
    occurrences: Counter[int] = Counter()
    places_by_day: dict[str, list[dict[str, Any]]] = {}
    meals_unfulfilled = 0
    known_place_spend = 0

    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("date"), str) or not isinstance(section.get("nodes"), list):
            fail("PLAN_DAY_INVALID")
            continue
        day = section["date"]
        nodes = section["nodes"]
        places_by_day[day] = [n for n in nodes if isinstance(n, dict) and n.get("kind") == "PLACE"]
        if len(places_by_day[day]) > 8:
            fail("DAY_VISIT_CAPACITY_EXCEEDED", day)
        if expected_dates and day != expected_dates[0]:
            # This standalone checker does not reconstruct overnight transfer
            # origins from the production validator. Keep that gap explicit.
            unsure("INTERDAY_TRANSFER_NOT_AUDITED", day)
        day_has_meal = False
        previous: dict[str, Any] | None = None
        for node in nodes:
            if not isinstance(node, dict):
                fail("PLAN_NODE_INVALID", day)
                continue
            kind, cid = node.get("kind"), node.get("contentid")
            if kind not in {"START", "PLACE", "END"}:
                fail("PLAN_NODE_KIND_INVALID", day)
                continue
            if kind == "PLACE":
                if type(cid) is not int or cid not in places:
                    fail("PLACE_ID_UNKNOWN_OR_INVALID", day, cid if type(cid) is int else None)
                    previous = node
                    continue
                occurrences[cid] += 1
                place = places[cid]
                candidate = pool.get(cid)
                if candidate is None:
                    fail("PLACE_NOT_FROM_ELIGIBLE_POOL", day, cid)
                elif day not in candidate.get("allowedDates", expected_dates or []):
                    fail("PLACE_OUTSIDE_ALLOWED_DATES", day, cid)

                start = _datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
                end = _datetime(node.get("plannedEndAt") or node.get("estimatedEndAt"))
                if start is None or end is None:
                    unsure("VISIT_CLOCK_UNKNOWN", day, cid)
                elif end < start:
                    fail("VISIT_END_BEFORE_START", day, cid)
                else:
                    role = place.get("planningRole")
                    overnight = role in {"overnight_lodging", "overnight_camping"}
                    if not overnight and (start.date().isoformat() != day or end.date().isoformat() != day):
                        fail("VISIT_CLOCK_DATE_MISMATCH", day, cid)
                    explicit_late = any(
                        a.get("contentid") == cid and a.get("date") == day
                        and a.get("kind") in {"appointment", "fixedSchedule"}
                        and isinstance(a.get("startTime"), str)
                        and a["startTime"] > activity_end.isoformat()
                        for a in anchors
                    )
                    if not overnight and not explicit_late and (
                        start.time() < activity_start or end.time() > activity_end
                    ):
                        fail("ACTIVITY_WINDOW_EXCEEDED", day, cid)
                    stay = details.get(cid, {}).get("estimatedStayMinutes")
                    if type(stay) is int and stay > 0 and (end - start).total_seconds() < stay * 60:
                        fail("KNOWN_STAY_DURATION_NOT_MET", day, cid)

                if place.get("planningRole") == "meal_stop":
                    day_has_meal = True
                    if start is None or end is None:
                        unsure("MEAL_SLOT_TIME_UNKNOWN", day, cid)
                    elif not ((time(11, 30) <= start.time() <= end.time() <= time(14, 30))
                              or (time(17, 30) <= start.time() <= end.time() <= time(20))):
                        fail("MEAL_OUTSIDE_SYNTHETIC_SLOT", day, cid)

                detail = details.get(cid, {})
                fee = detail.get("feeText")
                amount = re.fullmatch(r"\s*(1인|인당|전체)\s*(\d{1,9})\s*원\s*", fee) if isinstance(fee, str) else None
                if amount and amount[1] == "전체":
                    known_place_spend += int(amount[2])
                elif amount and type(conditions.get("expectedPeople")) is int and conditions["expectedPeople"] > 0:
                    known_place_spend += int(amount[2]) * conditions["expectedPeople"]
                elif isinstance(fee, str) and fee.strip() in {"무료", "무료입장"}:
                    pass
                else:
                    unsure("PLACE_FEE_UNKNOWN", day, cid)
                hours = detail.get("openingHoursText")
                hour_match = re.fullmatch(
                    r"((?:[01]\d|2[0-3]):[0-5]\d)\s*[~–-]\s*((?:[01]\d|2[0-3]):[0-5]\d)",
                    hours.strip(),
                ) if isinstance(hours, str) else None
                if hour_match and start and end:
                    opens, closes = map(time.fromisoformat, hour_match.groups())
                    if start.time() < opens or end.time() > closes:
                        fail("OUTSIDE_KNOWN_OPENING_HOURS", day, cid)
                else:
                    unsure("OPENING_HOURS_UNKNOWN", day, cid)

            if previous is not None:
                a, b = _fixture_index(previous, places), _fixture_index(node, places)
                prev_end = _datetime(previous.get("plannedEndAt") or previous.get("estimatedEndAt"))
                next_start = _datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
                if a is None or b is None:
                    unsure("SYNTHETIC_TRANSFER_FACT_UNKNOWN", day)
                else:
                    transfer = _transfer_minutes(a, b)
                    if (conditions.get("transport") == "car" and previous.get("kind") == "PLACE"
                            and node.get("kind") == "PLACE" and transfer * 60 > CAR_LEG_MAX_SECONDS):
                        fail("CAR_LEG_TIME_LIMIT_EXCEEDED", day, cid if type(cid) is int else None)
                    if prev_end is None or next_start is None:
                        unsure("SYNTHETIC_TRANSFER_FACT_UNKNOWN", day)
                    elif next_start < prev_end + timedelta(minutes=transfer):
                        fail("SYNTHETIC_TRANSFER_TIME_EXCEEDED", day, cid if type(cid) is int else None)
                    if conditions.get("transport") == "walk":
                        walk_limits = [int(p["walking"]) for p in preferences
                                       if isinstance(p, dict) and p.get("walking") in {"20", "40", 20, 40}]
                        if not walk_limits:
                            unsure("WALKING_LIMIT_UNKNOWN", day)
                        elif transfer > min(walk_limits):
                            fail("WALKING_LIMIT_EXCEEDED", day, cid if type(cid) is int else None)
            previous = node
        if not day_has_meal:
            meals_unfulfilled += 1

    for cid, count in occurrences.items():
        if count > 1:
            permitted_dates = {a.get("date") for a in anchors if a.get("contentid") == cid}
            actual_dates = [day for day, nodes in places_by_day.items() for n in nodes if n.get("contentid") == cid]
            if len(set(actual_dates)) != count or set(actual_dates) != permitted_dates:
                fail("DUPLICATE_FLEXIBLE_VISIT", cid=cid)

    for anchor in anchors:
        cid, day = anchor.get("contentid"), anchor.get("date")
        if type(cid) is not int or not isinstance(day, str):
            unsure("FIXED_ANCHOR_FACT_UNKNOWN")
            continue
        matching = [n for n in places_by_day.get(day, []) if n.get("contentid") == cid]
        if len(matching) != 1:
            fail("FIXED_ANCHOR_NOT_PRESERVED", day, cid)
            continue
        node = matching[0]
        kind = anchor.get("kind")
        if kind in {"lodging", "overnight_lodging", "overnight_camping"}:
            day_places = places_by_day.get(day, [])
            if not day_places or day_places[-1] is not node:
                fail("OVERNIGHT_ANCHOR_NOT_LAST", day, cid)
        for field, code, comparison in (
            ("startTime", "FIXED_START_NOT_MET", "min"),
            ("arrivalDeadline", "FIXED_DEADLINE_MISSED", "max"),
        ):
            value = anchor.get(field)
            if not isinstance(value, str):
                continue
            try:
                target = datetime.combine(date.fromisoformat(day), time.fromisoformat(value), tzinfo=KST)
            except ValueError:
                unsure("FIXED_ANCHOR_TIME_INVALID", day, cid)
                continue
            actual = _datetime(node.get("plannedStartAt") or node.get("estimatedStartAt"))
            if actual is None:
                unsure("FIXED_START_TIME_UNKNOWN", day, cid)
            elif field == "startTime" and kind in {"appointment", "fixedSchedule"} and actual != target:
                fail("FIXED_START_TIME_NOT_PRESERVED", day, cid)
            elif (comparison == "min" and actual < target) or (comparison == "max" and actual > target):
                fail(code, day, cid)
        if isinstance(anchor.get("endTime"), str):
            try:
                target = datetime.combine(date.fromisoformat(day), time.fromisoformat(anchor["endTime"]), tzinfo=KST)
            except ValueError:
                unsure("FIXED_ANCHOR_TIME_INVALID", day, cid)
            else:
                actual = _datetime(node.get("plannedEndAt") or node.get("estimatedEndAt"))
                if actual is None:
                    unsure("FIXED_END_TIME_UNKNOWN", day, cid)
                elif actual != target:
                    fail("FIXED_END_TIME_NOT_PRESERVED", day, cid)
        if isinstance(anchor.get("checkoutTime"), str):
            try:
                checkout_day = date.fromisoformat(anchor.get("endDate") or day)
                if checkout_day <= date.fromisoformat(day):
                    checkout_day = date.fromisoformat(day) + timedelta(days=1)
                due = datetime.combine(checkout_day, time.fromisoformat(anchor["checkoutTime"]), tzinfo=KST)
                actual = _datetime(node.get("estimatedEndAt"))
                if actual is None:
                    unsure("CHECKOUT_TIME_UNKNOWN", day, cid)
                elif actual > due:
                    fail("CHECKOUT_DEADLINE_MISSED", day, cid)
            except (TypeError, ValueError):
                unsure("CHECKOUT_TIME_UNKNOWN", day, cid)

    budget = conditions.get("budget")
    if type(budget) is int and known_place_spend > budget:
        fail("KNOWN_PLACE_FEES_EXCEED_BUDGET")
    if type(budget) is int:
        unsure("OTHER_TRAVEL_EXPENSES_UNAVAILABLE")
    else:
        unsure("GROUP_BUDGET_UNKNOWN")
    if meals_unfulfilled:
        for _ in range(meals_unfulfilled):
            unsure("MEAL_SLOT_UNFULFILLED")

    violation_counts = Counter(x["code"] for x in violations)
    unknown_counts = Counter(x["code"] for x in unknowns)
    return {
        "status": "violated" if violations else "unknown" if unknowns else "verified_on_synthetic_fixture",
        "independentOfProductionValidation": True,
        "knownViolationCount": len(violations),
        "knownViolationCounts": dict(sorted(violation_counts.items())),
        "unknownCount": len(unknowns),
        "unknownCounts": dict(sorted(unknown_counts.items())),
        "violations": violations,
        "unknowns": unknowns,
        "knownPlaceSpendKrw": known_place_spend,
        "totalBudgetStatus": "unknown_other_expenses",
        "mealSlotsUnfulfilled": meals_unfulfilled,
        "routeFactSource": "synthetic directed matrix only; never a real road estimate",
    }
