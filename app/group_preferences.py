"""Private generation handoff of confirmed atomic preference references."""
from __future__ import annotations

import json
from typing import Any


def _key(atom: dict[str, Any]) -> tuple[Any, ...]:
    return (atom.get("dimension"), atom.get("target"), atom.get("polarity"), atom.get("priority"),
            atom.get("priorityBasis"), atom.get("scope"), json.dumps(atom.get("frequency"), sort_keys=True),
            json.dumps(atom.get("value"), ensure_ascii=False, sort_keys=True), atom.get("unit"), bool(atom.get("approximate")))


def build_group_preferences(members: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Aggregate confirmed/current atoms with one equal member contribution each.

    Inputs must already exclude unconfirmed members and stale profiles. No evidence
    quotes, member IDs, locations, dates, or route instructions leave this helper.
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    soft: dict[tuple[Any, ...], dict[str, Any]] = {}
    personal_hard: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    unresolved_handoff: list[dict[str, Any]] = []
    for member in members:
        member_ref = member.get("memberRef")
        profile = member.get("atomicPreferences")
        if member.get("confirmed") is not True or not isinstance(member_ref, str) or not isinstance(profile, dict) or profile.get("status") != "current":
            continue
        unresolved_seen: set[str] = set()
        for item in profile.get("unresolved", []):
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not isinstance(code, str) or not code or code in unresolved_seen:
                continue
            unresolved_seen.add(code)
            unresolved_handoff.append({"memberRef": member_ref, "code": code, "needsReview": True})
        seen: set[str] = set()
        for atom in profile.get("conditions", []):
            if not isinstance(atom, dict):
                continue
            audience, polarity, priority = atom.get("audience"), atom.get("polarity"), atom.get("priority")
            safe = {k: atom.get(k) for k in ("dimension", "target", "polarity", "priority", "priorityBasis", "scope", "frequency", "value", "unit", "approximate")}
            semantic = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if semantic in seen:
                continue
            seen.add(semantic)
            occurrence = {**safe, "memberRef": member_ref, "_audience": audience}
            occurrences.append(occurrence)
            if audience in {"personal", "unspecified"} and priority == "required":
                personal_hard.append({**safe, "memberRef": member_ref, "conditionId": atom.get("id")})
            if audience == "group":
                key = _key(atom)
                entry = groups.setdefault(key, {**safe, "memberCount": 0, "memberReferences": []})
                if member_ref not in entry["memberReferences"]:
                    entry["memberReferences"].append(member_ref)
                    entry["memberCount"] += 1
            # Personal preferences and non-required dislikes/limits provide one
            # soft contribution per member; no score becomes a hard filter.
            if audience in {"personal", "group", "unspecified"} and priority != "required":
                key = _key(atom)
                entry = soft.setdefault(key, {**safe, "supportType": "soft", "memberCount": 0, "memberReferences": []})
                if member_ref not in entry["memberReferences"]:
                    entry["memberReferences"].append(member_ref)
                    entry["memberCount"] += 1

    conflicts = []
    for index, left in enumerate(occurrences):
        for right in occurrences[index + 1:]:
            if left["memberRef"] == right["memberRef"]:
                continue
            if left["dimension"] != right["dimension"] or left["target"].casefold() != right["target"].casefold():
                continue
            if left["polarity"] != right["polarity"]:
                conflict_code = ("CONTRASTING_SOFT_PREFERENCES"
                                 if left["priority"] != "required" and right["priority"] != "required"
                                 else "OPPOSED_POLARITY")
                conflicts.append({"code": conflict_code, "dimension": left["dimension"], "target": left["target"],
                                  "memberReferences": sorted({left["memberRef"], right["memberRef"]}), "needsReview": True})
                continue
            left_freq, right_freq = left.get("frequency") or {}, right.get("frequency") or {}
            left_period, right_period = left_freq.get("period"), right_freq.get("period")
            left_op, right_op = left_freq.get("operator"), right_freq.get("operator")
            left_count, right_count = left_freq.get("count"), right_freq.get("count")
            left_min = left_count if left_op in {"min", "exact"} else None
            left_max = left_count if left_op in {"max", "exact"} else None
            right_min = right_count if right_op in {"min", "exact"} else None
            right_max = right_count if right_op in {"max", "exact"} else None
            incompatible = ((left_max is not None and right_min is not None and left_max < right_min)
                            or (right_max is not None and left_min is not None and right_max < left_min))
            if (left["priority"] == right["priority"] == "required" and left["polarity"] == "limit"
                    and left_period == right_period and left_period is not None and incompatible):
                conflicts.append({"code": "INCOMPATIBLE_HARD_LIMITS", "dimension": left["dimension"], "target": left["target"],
                                  "memberReferences": sorted({left["memberRef"], right["memberRef"]}), "needsReview": True})
    unique_conflicts = {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in conflicts}
    return {"conditions": list(groups.values()), "softSupport": list(soft.values()),
            "memberScopedConstraints": personal_hard, "conflicts": list(unique_conflicts.values()),
            "unresolved": unresolved_handoff}
