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

from .confirmed_preferences import ALIASES, NEGATIVE, _clauses, analyze_preferences

POLICY_VERSION = "atomic-route-v2"
_TOURISM = {"sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop", "date_specific_event"}
_LODGING = {"overnight_lodging", "overnight_camping"}
_MARKET_DIMENSIONS = {"activity", "place", "venue", "category", "place_type", "visit_count", "other"}
_MARKET_TERMS = {"시장", "전통시장", "재래시장", "장터"}
_LEGACY_MARKET_WISHES = {"시장", "전통시장", "재래시장", "장터",
                         "시장에가고싶어요", "전통시장에가고싶어요", "재래시장에가고싶어요",
                         "장터에가고싶어요", "시장방문하고싶어요", "전통시장방문하고싶어요",
                         "재래시장방문하고싶어요", "장터방문하고싶어요"}


def _compact_korean(value: Any) -> str:
    return re.sub(r"\s+", "", value).casefold() if isinstance(value, str) else ""


def _market_target(atom: dict[str, Any]) -> bool:
    if atom.get("dimension") not in _MARKET_DIMENSIONS:
        return False
    target = _compact_korean(atom.get("target")).rstrip(".!?")
    return bool(re.fullmatch(
        r"(?:전통시장|재래시장|시장|장터)(?:에|을|를|에서)?"
        r"(?:방문|구경|가기|가고싶어요|가고싶어|가고싶다|방문하고싶어요|"
        r"방문하고싶어|방문하고싶다|구경하고싶어요|구경하고싶어|구경하고싶다)?",
        target))


def _legacy_market_wish(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if re.search(r"(?:\b(?:or|either)\b|이나|또는|혹은|아니면)", value, re.I):
        return False
    clauses = _clauses(value)
    if any(NEGATIVE.search(clause) and any(term in _compact_korean(clause) for term in _MARKET_TERMS)
           for clause in clauses):
        return False
    return any(not NEGATIVE.search(clause)
               and _compact_korean(clause).rstrip(".!?") in _LEGACY_MARKET_WISHES for clause in clauses)


def is_market_place(place: dict[str, Any], tags: set[str] | None = None) -> bool:
    """Ground a market match only in a shopping role plus a specific market fact."""
    if (place.get("planningRole") != "shopping_stop"
            or place.get("recommendationUse") not in {"GENERAL_SHOPPING", "SPECIAL_SHOPPING"}):
        return False
    title = _compact_korean(place.get("title"))
    if title.endswith("전시장") and not title.endswith("역전시장"):
        return False
    title_match = any(title.endswith(term) for term in _MARKET_TERMS)
    type_match = _compact_korean(place.get("contentTypeName")) in _MARKET_TERMS
    tag_match = bool(tags and tags & _MARKET_TERMS)
    return title_match or type_match or tag_match


def _requested_market_ids(compiled, places, facts):
    if any(rule.get("dimension") == "market" for rule in compiled.get("countRules", [])):
        return {cid for cid, place in places.items() if is_market_place(place, facts.get(cid))}
    return set()


def _market_unknown_ids(places: dict[int, dict[str, Any]], market_ids: set[int]) -> set[int]:
    return {cid for cid, place in places.items() if cid not in market_ids
            and place.get("planningRole") == "shopping_stop"
            and place.get("recommendationUse") in {"GENERAL_SHOPPING", "SPECIAL_SHOPPING"}}


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
    compact = "".join(target.split()) if isinstance(target, str) else ""
    if compact in {"\uacf3", "\uad70\ub370"}:
        return ("sightseeing", None) if dimension == "visit_count" and _frequency(atom) is not None else None
    if dimension == "visit_count" and compact in {
        "\ubc29\ubb38\uc7a5\uc18c", "\ubc29\ubb38\uc7a5\uc18c\uc218", "\uad00\uad11\uc9c0",
        "\uad00\uad11\uc9c0\uc218", "\uad00\uad11\uc7a5\uc18c", "\uad00\uad11\uc7a5\uc18c\uc218",
        "\uc7a5\uc18c", "sightseeing",
    }:
        return "sightseeing", None
    if dimension in {"meal_plan", "food"} and target in {
        "\uc2dd\uc0ac", "\uc2dd\uc0ac \ud69f\uc218", "\ub07c\ub2c8", "\ub07c\ub2c8 \uc218", "\uc2dd\ub2f9",
        "meal", "meals", "meal_stop",
    }:
        return "meals", None
    if _market_target(atom):
        return "market", None
    tag = target if target in vocabulary else next(
        (name for name, aliases in ALIASES.items() if name in vocabulary and target in aliases), None)
    if tag is None:
        return None
    if dimension == "food":
        return "tag_food", tag
    if dimension in {"activity", "visit_count"}:
        return "tag_activity", tag
    return "tag", tag


def _three_meal_periods(target: Any) -> bool:
    if not isinstance(target, str):
        return False
    periods = re.split(r"[\s\u00b7\u318d,\uff0c/]+", target.strip())
    return len(periods) == 3 and set(periods) == {"\uc544\uce68", "\uc810\uc2ec", "\uc800\ub141"}


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
            if dimension == "meal_plan" and _three_meal_periods(atom.get("target")):
                if not frequency or frequency["period"] != "day" or frequency["count"] != 3 or frequency["operator"] != "exact":
                    (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                    continue
                if hard:
                    issues.append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED"})
                    continue
                count_rules.append({"memberRef": ref, "dimension": "meals", "tag": None,
                                    "scope": "day", "operator": "exact", "count": 3,
                                    "required": False, "polarity": polarity})
                warnings.append({"field": field, "code": "ATOMIC_PREFERENCE_UNSUPPORTED"})
                continue
            matcher = _matcher(atom, supported)
            if matcher and matcher[0] == "market":
                if polarity != "prefer":
                    (issues if hard else warnings).append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED" if hard else "ATOMIC_PREFERENCE_UNSUPPORTED"})
                    continue
                if hard and scope is None and frequency is None:
                    issues.append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED"})
                    continue
                if frequency is None:
                    frequency = {"operator": "min", "count": 1, "period": scope or "trip"}
                if hard and (approximate or frequency["operator"] in {"target", "max", "exact"}):
                    issues.append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED"})
                    continue
                count_rules.append({"memberRef": ref, "dimension": "market", "tag": None,
                                    "scope": frequency["period"], "operator": frequency["operator"],
                                    "count": frequency["count"], "required": hard, "polarity": "prefer"})
                continue
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
    # A small exact allowlist keeps legacy v1 market wishes grounded without
    # reinterpreting broad shopping text or recovering discarded source text.
    legacy_preferences = snapshot_doc.get("confirmedPreferences", []) if isinstance(snapshot_doc, dict) else []
    if isinstance(legacy_preferences, list):
        for pref in legacy_preferences:
            if (not isinstance(pref, dict) or pref.get("memberRef") in atomic_refs
                    or not _legacy_market_wish(pref.get("likes"))):
                continue
            count_rules.append({"memberRef": pref["memberRef"], "dimension": "market", "tag": None,
                                "scope": "trip", "operator": "min", "count": 1,
                                "required": False, "polarity": "prefer"})
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


def _place_count(place: dict[str, Any], rule: dict[str, Any], roles: dict[int, str],
                 tag_facts: dict[int, set[str] | None], market_ids: set[int],
                 market_unknown_ids: set[int]) -> tuple[int, bool]:
    cid = place.get("contentid")
    role = roles.get(cid, place.get("planningRole"))
    if role in _LODGING:
        return 0, True
    if rule.get("dimension") == "meals":
        return (1, True) if role == "meal_stop" else (0, True)
    if rule.get("dimension") == "market":
        if cid in market_ids:
            return 1, True
        return 0, not (rule.get("required") and cid in market_unknown_ids)
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


def _rule_counts(draft: dict[str, Any], rule: dict[str, Any], roles: dict[int, str],
                 tag_facts: dict[int, set[str] | None], market_ids: set[int],
                 market_unknown_ids: set[int]):
    days = draft.get("days", [])
    sections = [[day] for day in days] if rule.get("scope") == "day" else [days]
    for section in sections:
        count, known = 0, True
        for day in section:
            for place in day.get("places", []):
                n, verified = _place_count(place, rule, roles, tag_facts, market_ids, market_unknown_ids)
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
    market_ids = _requested_market_ids(compiled, places, tag_facts)
    hard_market = any(rule.get("required") and rule.get("dimension") == "market" for rule in compiled.get("countRules", []))
    market_unknown_ids = _market_unknown_ids(places, market_ids) if hard_market else set()
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
        for section, count, known in _rule_counts(draft, rule, roles, tag_facts, market_ids, market_unknown_ids):
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


def route_preference_cost(draft: dict[str, Any], compiled: dict[str, Any], catalog: Any) -> tuple[int, float]:
    """Count unverified soft goals separately so independently known deviations can still be ranked."""
    places, facts, _ = _catalog_facts(catalog)
    roles = {cid: row.get("planningRole") for cid, row in places.items()}
    market_ids = _requested_market_ids(compiled, places, facts)
    draft = {"days": [{**day, "places": day.get("places", [node for node in day.get("nodes", [])
                                                           if node.get("kind") == "PLACE"])}
                      for day in draft.get("days", [])]}
    penalty = 0.0
    unverified = 0
    seen_market_rules = set()
    for rule in compiled.get("countRules", []):
        if rule.get("dimension") == "market":
            identity = (rule.get("scope"), rule.get("operator"), rule.get("count"),
                       rule.get("required"), rule.get("polarity"))
            if identity in seen_market_rules:
                continue
            seen_market_rules.add(identity)
        if rule.get("required"):
            continue
        for _section, count, known in _rule_counts(draft, rule, roles, facts, market_ids, set()):
            target = rule["count"]
            if not known and not (rule["operator"] == "min" and count >= target):
                unverified += 1
                continue
            if rule["operator"] == "min":
                penalty += max(0, target - count)
            elif rule["operator"] == "max":
                penalty += max(0, count - target)
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
                unverified += 1
                continue
            if rule.get("scope") == "explicit_segment":
                penalty += sum(max(0, value - rule["seconds"]) for value in walks) / 60
            else:
                penalty += max(0, sum(walks) - rule["seconds"]) / 60
    return unverified, penalty


def route_preference_penalty(draft: dict[str, Any], compiled: dict[str, Any], catalog: Any) -> float:
    """Scalar callers cannot treat an unverified goal as satisfied."""
    unverified, penalty = route_preference_cost(draft, compiled, catalog)
    return float("inf") if unverified else penalty


def draft_preference_rules(compiled: dict[str, Any], catalog: Any, candidate_ids: list[int]) -> list[dict[str, Any]]:
    """Resolve bounded count rules to eligible IDs for the draft prompt."""
    places, facts, _ = _catalog_facts(catalog)
    market_ids = _requested_market_ids(compiled, places, facts)
    allowed = {cid for cid in candidate_ids if type(cid) is int}
    rules = []
    for rule in compiled.get("countRules", []):
        if rule.get("dimension") == "market":
            ids = [cid for cid in sorted(allowed) if cid in places and cid in market_ids]
        elif rule.get("dimension") == "sightseeing":
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
