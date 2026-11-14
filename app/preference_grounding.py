"""Conservative clarification guards; this is not a general semantic verifier."""
from __future__ import annotations

import copy
import re
from datetime import date, timedelta


def ground_interpretation(original_text: str, result: dict) -> dict:
    """Keep the existing DTO and ask again for competing explicit walking bounds.

    The AI package already checks affirmative evidence. This additional guard
    never invents a preference or picks one of conflicting limits. It deliberately
    does not claim to prove the meaning of free-form likes/avoid/food fields.
    """
    clean = copy.deepcopy(result)
    suggestions = clean.get("suggestions")
    if not isinstance(suggestions, dict):
        return clean
    from chungbuk_trip_ai.gemini import _walking_limit_is_explicit

    bounds = {limit for limit in ("20", "40", "unlimited")
              if _walking_limit_is_explicit(original_text, limit)}
    # The existing provider check does not recognize "걷는 것은 괜찮지만".
    # Use this narrow affirmative grammar only to detect competing bounds;
    # never manufacture a walking answer from an absent model answer.
    for clause in re.split(r"[.!?。！？\n]", original_text):
        if re.search(r"괜찮(?:지|지는)\s*않|괜찮지\s*못|불가능|아니|힘들|어렵|무리", clause):
            continue
        for match in re.finditer(r"한\s*번에\s*(?:최대\s*)?(20|40)\s*분까지\s*걷는\s*(?:것은|건|것도)?\s*괜찮", clause):
            bounds.add(match[1])
    chosen = suggestions.get("walking")
    if chosen is not None and (chosen not in bounds or len(bounds) > 1):
        suggestions["walking"] = None
        clean["needsWalkingAnswer"] = True
    return clean


def schedule_conflict_fields(items: list[dict]) -> dict[str, set[str]]:
    """Find only demonstrable clashes; missing durations are never guessed."""
    conflicts: dict[str, set[str]] = {}

    def flag(a, b, *fields):
        for item in (a, b):
            if item.get("id"):
                conflicts.setdefault(item["id"], set()).update(fields)

    def place(item):
        return ("id", item["contentid"]) if item.get("contentid") is not None else ("query", item.get("placeQuery"))

    for index, a in enumerate(items):
        for b in items[index + 1:]:
            # Without identified locations a spatial contradiction is unknown.
            if not a.get("placeQuery") and a.get("contentid") is None:
                continue
            if not b.get("placeQuery") and b.get("contentid") is None:
                continue
            if place(a) == place(b) or not a.get("date") or not b.get("date"):
                continue
            if a.get("kind") == b.get("kind") == "lodging":
                try:
                    start_a, start_b = date.fromisoformat(a["date"]), date.fromisoformat(b["date"])
                    end_a = date.fromisoformat(a["endDate"]) if a.get("endDate") else start_a + timedelta(days=1)
                    end_b = date.fromisoformat(b["endDate"]) if b.get("endDate") else start_b + timedelta(days=1)
                except (ValueError, TypeError):
                    continue
                end_a, end_b = max(end_a, start_a + timedelta(days=1)), max(end_b, start_b + timedelta(days=1))
                if max(start_a, start_b) < min(end_a, end_b):
                    flag(a, b, "date", "endDate")
            elif a.get("kind") == b.get("kind") == "appointment" and a["date"] == b["date"]:
                ta, tb = a.get("time"), b.get("time")
                if not ta or not tb:
                    continue
                # Same starts are impossible at distinct places even if duration
                # is unknown. Otherwise use only explicitly supplied end clocks.
                overlap = ta == tb
                if a.get("endTime"):
                    overlap |= ta <= tb < a["endTime"]
                if b.get("endTime"):
                    overlap |= tb <= ta < b["endTime"]
                if overlap:
                    flag(a, b, "date", "time")
    return conflicts
