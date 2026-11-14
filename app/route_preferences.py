"""Private compiler and validator adapter for confirmed atomic route preferences.

Only reviewed catalog facts are used to establish a predicate. Unsupported or
unknown evidence stays generic and never enters public errors.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any

from .confirmed_preferences import ALIASES, analyze_preferences

POLICY_VERSION = "atomic-route-v1"
_TOURISM = {"sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop", "date_specific_event"}
_LODGING = {"overnight_lodging", "overnight_camping"}


def _digest(doc: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _catalog_facts(catalog: Any) -> tuple[dict[int, dict[str, Any]], dict[int, set[str] | None], set[str]]:
    places = {row["contentid"]: row for row in getattr(catalog, "places", ()) if type(row.get("contentid")) is int}
    tags: dict[int, set[str] | None] = {}
    vocab = set()
    for row in getattr(catalog, "preferences", ()):
        cid = row.get("contentid")
        if type(cid) is not int:
            continue
        raw = [row.get(key) for key in ("preferenceTags", "vibeTags")]
        if any(value is not None and not isinstance(value, str) for value in raw):
            tags[cid] = None
            continue
        tags[cid] = {part.strip() for value in raw if value for part in value.split("|") if part.strip()}
        vocab.update(tags[cid] or ())
    return places, tags, vocab


def _scope(atom: dict[str, Any]) -> str | None:
    scope = atom.get("scope")
    if scope in {"day", "daily", "per_day"}:
        return "day"
    if scope in {"trip", "whole_trip", "travel_period"}:
        return "trip"
    return None


def _frequency(atom: dict[str, Any]) -> dict[str, Any] | None:
    freq = atom.get("frequency")
    if not isinstance(freq, dict) or freq.get("operator") not in {"min", "max", "exact", "target"}:
        return None
    count = freq.get("count")
    period = freq.get("period")
    if type(count) is not int or not 0 <= count <= 365 or period not in {"day", "trip"}:
        return None
    atom_scope = atom.get("scope")
    if atom_scope in {"day", "trip"} and atom_scope != period:
        return None
    return {"operator": freq["operator"], "count": count, "period": period}


def _atom_key(atom: dict[str, Any]) -> str:
    core = {k: atom.get(k) for k in ("dimension", "target", "polarity", "priority", "scope", "frequency", "value", "unit", "approximate", "walkingScope")}
    return json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _matcher(atom: dict[str, Any], vocabulary: set[str]) -> tuple[str, str | None] | None:
    dimension, target = atom.get("dimension"), atom.get("target")
    if dimension == "visit_count" and target in {
        "방문 장소", "방문 장소 수", "관광지", "관광지 수", "관광 장소", "관광 장소 수", "장소", "sightseeing",
    }:
        return "sightseeing", None
    if dimension in {"meal_plan", "food"} and target in {
        "식사", "식사 횟수", "끼니", "끼니 수", "식당", "meal", "meals", "meal_stop",
    }:
        return "meals", None
    tag = target if target in vocabulary else next(
        (name for name, aliases in ALIASES.items() if name in vocabulary and target in aliases), None)
    if tag is None:
        return None
    if dimension == "food":
        return "tag_food", tag
    if dimension in {"activity", "visit_count"}:
        return "tag_activity", tag
    return "tag", tag


def compile_route_preferences(snapshot_doc: dict[str, Any], catalog: Any) -> dict[str, Any]:
    """Compile trusted current group atoms into bounded count/walking rules."""
    places, catalog_tags, vocab = _catalog_facts(catalog)
    supported = set(getattr(catalog, "supported_tags", ()) or ()) | vocab
    refs = snapshot_doc.get("atomicMemberRefs", []) if isinstance(snapshot_doc, dict) else []
    atomic_refs = sorted({x for x in refs if isinstance(x, str) and x}) if isinstance(refs, list) else []
    group = snapshot_doc.get("groupConditions", {}) if isinstance(snapshot_doc, dict) else {}
    if not isinstance(group, dict):
        group = {}
    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    predicates: list[dict[str, Any]] = []
    count_rules: list[dict[str, Any]] = []
    walking_rules: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    atoms = []
    for key in ("conditions", "memberScopedConstraints", "softSupport"):
        value = group.get(key, [])
        if isinstance(value, list):
            atoms.extend(a for a in value if isinstance(a, dict))
    for atom in atoms:
        members = atom.get("memberReferences")
        if not isinstance(members, list):
            members = []
        if isinstance(atom.get("memberRef"), str):
            members = [*members, atom["memberRef"]]
        # Group preference aggregation already deduplicates within member; this
        # key prevents duplicate condition/softSupport contributions.
        semantic = _atom_key(atom)
        for ref in sorted({r for r in members if r in atomic_refs}):
            pair = (ref, semantic)
            if pair in seen:
                continue
            seen.add(pair)
            hard = atom.get("priority") == "required" and atom.get("supportType") != "soft"
            field = "atomicPreferences"
            dimension, polarity = atom.get("dimension"), atom.get("polarity")
            scope = _scope(atom)
            approximate = atom.get("approximate") is True
            if atom.get("scope") == "segment" and dimension != "walking":
                (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                continue
            if dimension == "walking" or (dimension == "mobility" and atom.get("target") in {"walking", "walking_time"}):
                value = atom.get("value")
                unit = atom.get("unit")
                minutes = None
                if type(value) in (int, float) and math.isfinite(value) and value >= 0 and unit in {"minute", "minutes", "min", "분"}:
                    minutes = float(value)
                elif isinstance(value, str) and unit in {"minute", "minutes", "min", "분"} and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", value):
                    minutes = float(value)
                walking_scope = atom.get("walkingScope")
                if (minutes is None or not math.isfinite(minutes * 60) or polarity != "limit"
                        or atom.get("target") not in {"도보", "걷기", "걷는 시간", "걷기 시간", "도보 시간", "도보 구간", "walking", "walking_time"}
                        or atom.get("frequency") is not None
                        or walking_scope not in {None, "explicit_segment"}
                        or (scope is None and walking_scope != "explicit_segment")):
                    (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                    continue
                if hard and approximate:
                    issues.append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED"})
                    continue
                walking_rules.append({"memberRef": ref, "seconds": int(math.floor(minutes * 60)), "scope": walking_scope or scope,
                                      "required": hard, "approximate": approximate})
                continue
            if polarity not in {"prefer", "limit"}:
                matcher = _matcher(atom, supported)
                if not hard and polarity == "avoid" and matcher is not None and matcher[1] is not None:
                    predicates.append({"memberRef": ref, "field": field, "tag": matcher[1], "polarity": "avoid",
                                       "scope": "meal" if dimension == "food" else "all", "required": False})
                    continue
                (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                continue
            frequency = _frequency(atom)
            if atom.get("scope") == "segment" or (atom.get("frequency") is not None and frequency is None):
                (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                continue
            if frequency:
                scope = frequency["period"]
            matcher = _matcher(atom, supported)
            unsupported = (matcher is None or (hard and (approximate or dimension == "accessibility"
                           or scope is None or frequency is not None and frequency["operator"] == "target")))
            if unsupported:
                (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                continue
            kind, tag = matcher
            if frequency is None and hard and polarity == "prefer":
                frequency = {"operator": "min", "count": 1, "period": scope}
            if frequency is not None:
                count_rules.append({"memberRef": ref, "dimension": kind, "tag": tag, "scope": frequency["period"],
                                    "operator": frequency["operator"], "count": frequency["count"],
                                    "required": hard, "polarity": polarity})
            elif polarity == "prefer" and tag is not None:
                predicates.append({"memberRef": ref, "field": field, "tag": tag, "polarity": "prefer",
                                   "scope": "meal" if dimension == "food" else "all", "required": False})
            else:
                (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
    # Unresolved notes are intentionally reduced to generic codes.
    for item in group.get("unresolved", []) if isinstance(group.get("unresolved"), list) else []:
        if isinstance(item, dict) and item.get("memberRef") in atomic_refs:
            warnings.append({"field": "atomicPreferences", "code": "ATOMIC_PREFERENCE_UNRESOLVED"})
    doc = {"policyVersion": POLICY_VERSION, "atomicMemberRefs": atomic_refs, "predicates": predicates,
           "countRules": count_rules, "walkingRules": walking_rules,
           "issues": _unique(issues), "softWarnings": _unique(warnings)}
    doc["digest"] = _digest(doc)
    return doc


def _unique(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted({(x["field"], x["code"]): x for x in items}.values(), key=lambda x: (x["field"], x["code"]))


def legacy_preferences(preferences: list[dict[str, Any]], compiled: dict[str, Any]) -> list[dict[str, Any]]:
    refs = set(compiled.get("atomicMemberRefs", []))
    return [{**copy.deepcopy(pref), **({"likes": None, "avoid": None, "food": None, "walking": None}
                                      if pref.get("memberRef") in refs else {})} for pref in preferences]


def combine_confirmed_evidence(preferences: list[dict[str, Any]], tags: Any, compiled: dict[str, Any]) -> dict[str, Any]:
    evidence = analyze_preferences(legacy_preferences(preferences, compiled), tags)
    evidence["predicates"].extend(copy.deepcopy(compiled.get("predicates", [])))
    evidence["issues"].extend(copy.deepcopy(compiled.get("issues", [])))
    evidence["issues"] = _unique(evidence["issues"])
    evidence["atomicPreferenceDigest"] = compiled.get("digest")
    evidence["digest"] = _digest({k: evidence[k] for k in ("policyVersion", "predicates", "issues", "atomicPreferenceDigest")})
    return evidence


def _place_count(place: dict[str, Any], rule: dict[str, Any], roles: dict[int, str], tag_facts: dict[int, set[str] | None]) -> tuple[int, bool]:
    cid = place.get("contentid")
    role = roles.get(cid, place.get("planningRole"))
    if role in _LODGING:
        return 0, True
    if rule.get("dimension") == "meals":
        return (1, True) if role == "meal_stop" else (0, True)
    if rule.get("dimension") == "sightseeing":
        return (1, True) if role in _TOURISM else (0, True)
    if rule.get("dimension") == "tag_food" and role != "meal_stop":
        return 0, True
    if rule.get("dimension") == "tag_activity" and role not in _TOURISM:
        return 0, True
    if rule.get("dimension") == "tag" and role in _LODGING:
        return 0, True
    facts = tag_facts.get(cid)
    if facts is None:
        return 0, False
    present = rule.get("tag") in facts
    return int(present), present


def _rule_counts(draft: dict[str, Any], rule: dict[str, Any], roles: dict[int, str], tag_facts: dict[int, set[str] | None]):
    days = draft.get("days", [])
    sections = [[day] for day in days] if rule.get("scope") == "day" else [days]
    for section in sections:
        count, known = 0, True
        for day in section:
            for place in day.get("places", []):
                n, verified = _place_count(place, rule, roles, tag_facts)
                count += n
                known &= verified
        yield section, count, known


def apply_route_preferences(plan: dict[str, Any], compiled: dict[str, Any], catalog: Any) -> dict[str, Any]:
    """Attach generic hard count violations and uncertainty warnings."""
    result = copy.deepcopy(plan)
    validation = result.setdefault("validation", {})
    violations = validation.setdefault("violations", [])
    warnings = set(validation.setdefault("warnings", []))
    draft = {"days": [{"date": d.get("date"), "places": [n for n in d.get("nodes", []) if n.get("kind") == "PLACE"]}
                      for d in result.get("days", [])]}
    places, tag_facts, _ = _catalog_facts(catalog)
    roles = {cid: row.get("planningRole") for cid, row in places.items()}
    unresolved_hard = False
    for issue in compiled.get("issues", []):
        code = issue.get("code")
        if code in {"REQUIRED_PREFERENCE_UNSUPPORTED", "REQUIRED_PREFERENCE_UNSATISFIED"}:
            date = result.get("days", [{}])[0].get("date") if result.get("days") else None
            if not any(item.get("code") == code and item.get("date") == date for item in violations):
                violations.append({"code": code, "date": date})
    for rule in compiled.get("countRules", []):
        if not rule.get("required"):
            continue
        for section, count, known in _rule_counts(draft, rule, roles, tag_facts):
            op, target = rule["operator"], rule["count"]
            violated = count < target if op == "min" else count > target if op == "max" else count != target
            exceeded = op in {"max", "exact"} and count > target
            if exceeded or violated and known:
                date = section[-1].get("date") if section else None
                code = "REQUIRED_PREFERENCE_UNSATISFIED"
                if not any(v.get("code") == code and v.get("date") == date for v in violations):
                    violations.append({"code": code, "date": date})
            elif not known and ((op in {"max", "exact"} and count <= target) or (op == "min" and count < target)):
                unresolved_hard = True
                warnings.add("REQUIRED_PREFERENCE_UNVERIFIED")
    for rule in compiled.get("walkingRules", []):
        if not rule.get("required"):
            continue
        days = result.get("days", [])
        sections = [[day] for day in days] if rule.get("scope") == "day" else [days]
        for section in sections:
            seconds, known = _walking_seconds(section)
            measured = max(seconds, default=0) if rule.get("scope") == "explicit_segment" else sum(seconds)
            if measured > rule["seconds"]:
                code = "REQUIRED_PREFERENCE_UNSATISFIED"
                date = section[-1].get("date") if section else None
                if not any(v.get("code") == code and v.get("date") == date for v in violations):
                    violations.append({"code": code, "date": date})
            elif not known:
                unresolved_hard = True
                warnings.add("REQUIRED_PREFERENCE_UNVERIFIED")
    warnings.update(item["code"] for item in compiled.get("softWarnings", []))
    validation["warnings"] = sorted(warnings)
    if violations:
        validation["status"] = "violated"
    elif unresolved_hard and validation.get("status") == "verified":
        validation["status"] = "unknown"
    return result


def route_preference_penalty(draft: dict[str, Any], compiled: dict[str, Any], catalog: Any) -> float:
    """Nonnegative soft deviation penalty; unknown exact facts are not inferred."""
    places, facts, _ = _catalog_facts(catalog)
    roles = {cid: row.get("planningRole") for cid, row in places.items()}
    draft = {"days": [{**day, "places": day.get("places", [node for node in day.get("nodes", [])
                                                           if node.get("kind") == "PLACE"])}
                      for day in draft.get("days", [])]}
    penalty = 0.0
    for rule in compiled.get("countRules", []):
        if rule.get("required"):
            continue
        for _section, count, known in _rule_counts(draft, rule, roles, facts):
            target = rule["count"]
            if not known and not (rule["operator"] == "min" and count >= target):
                return float("inf")
            if rule["operator"] == "min":
                penalty += max(0, target - count)
            elif rule["operator"] == "max":
                penalty += max(0, count - target)
            elif rule["operator"] == "target":
                penalty += abs(count - target)
            else:
                penalty += abs(count - target)
    for rule in compiled.get("walkingRules", []):
        if rule.get("required"):
            continue
        days = draft.get("days", [])
        sections = [[day] for day in days] if rule.get("scope") == "day" else [days]
        for section in sections:
            walks, known = _walking_seconds(section)
            if not known:
                return float("inf")
            if rule.get("scope") == "explicit_segment":
                penalty += sum(max(0, value - rule["seconds"]) for value in walks) / 60
            else:
                penalty += max(0, sum(walks) - rule["seconds"]) / 60
    return float(penalty)


def draft_preference_rules(compiled: dict[str, Any], catalog: Any, candidate_ids: list[int]) -> list[dict[str, Any]]:
    """Resolve bounded count rules to eligible IDs for the draft prompt."""
    places, facts, _ = _catalog_facts(catalog)
    allowed = {cid for cid in candidate_ids if type(cid) is int}
    rules = []
    for rule in compiled.get("countRules", []):
        if rule.get("dimension") == "sightseeing":
            ids = [cid for cid in sorted(allowed) if cid in places and places[cid].get("planningRole") in _TOURISM]
        elif rule.get("dimension") == "meals":
            ids = [cid for cid in sorted(allowed) if cid in places and places[cid].get("planningRole") == "meal_stop"]
        else:
            ids = [cid for cid in sorted(allowed) if cid in places and facts.get(cid) is not None
                   and rule.get("tag") in facts[cid]
                   and (rule.get("dimension") != "tag_food" or places[cid].get("planningRole") == "meal_stop")
                   and (rule.get("dimension") != "tag_activity" or places[cid].get("planningRole") in _TOURISM)]
        if not ids:
            continue
        rules.append({"contentids": ids, "period": rule["scope"], "operator": rule["operator"],
                      "count": rule["count"], "required": rule["required"]})
    unique = {json.dumps(rule, sort_keys=True, separators=(",", ":")): rule for rule in rules}
    return list(unique.values())


def _walking_seconds(days: list[dict[str, Any]]) -> tuple[list[int], bool]:
    """Keep verified partial sums while marking missing or invalid legs unknown."""
    seconds = []
    known = True
    for day in days:
        edges = day.get("edges")
        if not isinstance(edges, list):
            known = False
            continue
        if not edges and (day.get("places") or any(node.get("kind") == "PLACE" for node in day.get("nodes", []))):
            known = False
        for edge in edges:
            values = edge.get("walkingSeconds")
            if (edge.get("status") != "verified" or not isinstance(values, list)
                    or any(type(value) is not int or value < 0 for value in values)):
                known = False
                continue
            seconds.extend(values)
    return seconds, known
