"""Boundary helpers for the opt-in private atomic preference profile."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def _core():
    try:
        from chungbuk_trip_ai import build_atomic_preferences, source_digest, validate_atomic_preferences
        from chungbuk_trip_ai import mark_atomic_preferences_stale
    except Exception:
        return None
    return build_atomic_preferences, source_digest, validate_atomic_preferences, mark_atomic_preferences_stale


def digest(original_text: str | None, conditions: dict[str, Any] | None) -> str:
    functions = _core()
    if functions:
        return functions[1](original_text or "", conditions or {})
    payload = {"originalText": (original_text or "").replace("\r\n", "\n").strip(), "conditions": conditions or {}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def canonical_interpretation(profile: Any, original_text: str | None, conditions: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a provider profile, replacing its digest/IDs and model provenance."""
    functions = _core()
    if not functions or not isinstance(original_text, str) or not original_text:
        raise ValueError("Atomic preference support is unavailable")
    build, source_digest, validate, _ = functions
    if (not isinstance(profile, dict) or set(profile) != {"schemaVersion", "sourceDigest", "status", "conditions", "unresolved"}
            or profile.get("schemaVersion") != 2):
        raise ValueError("Invalid atomic preference profile")
    # Provider metadata is not authoritative. Keep only model facts and let the
    # canonical builder generate all identifiers and the source digest.
    raw_conditions = []
    allowed = {"id", "dimension", "target", "polarity", "priority", "priorityBasis", "scope", "audience", "frequency", "value", "unit", "approximate", "evidence", "origin"}
    if not isinstance(profile["conditions"], list):
        raise ValueError("Invalid atomic preference conditions")
    for atom in profile["conditions"]:
        if not isinstance(atom, dict) or set(atom) - allowed:
            raise ValueError("Invalid atomic preference condition")
        raw_conditions.append({k: copy.deepcopy(v) for k, v in atom.items() if k not in {"id", "origin"}})
    canonical = build(raw_conditions, profile["unresolved"], original_text, conditions)
    canonical["status"] = "current"
    return canonical


def profile_from_cached_result(result: Any, receipt: str, original_text: str | None,
                               conditions: dict[str, Any]) -> dict[str, Any]:
    """Validate a server-cached analysis receipt and return its trusted profile."""
    if not isinstance(result, dict) or not isinstance(result.get("analysisMeta"), dict):
        raise ValueError("Invalid analysis receipt")
    if result["analysisMeta"].get("requestId") != receipt:
        raise ValueError("Invalid analysis receipt")
    if (result.get("suggestions", {}).get("atomicPreferences") is None or not isinstance(original_text, str)
            or result.get("sourceConditionsVersion") is None or result.get("sourcePreferenceVersion") is None):
        raise ValueError("Invalid analysis receipt")
    profile = result["suggestions"]["atomicPreferences"]
    if profile.get("status") != "current" or profile.get("sourceDigest") != digest(original_text, conditions):
        raise ValueError("Stale analysis receipt")
    return canonical_interpretation(profile, original_text, conditions)


def response_profile_status(profile: Any, original_text: str | None, conditions: dict[str, Any]) -> dict[str, Any] | None:
    """Return a detached canonical profile with stale status derived from sources."""
    if profile is None:
        return None
    functions = _core()
    if not functions or not isinstance(original_text, str):
        result = copy.deepcopy(profile)
        if isinstance(result, dict): result["status"] = "stale"
        return result
    _, source_digest, validate, mark_stale = functions
    try:
        result = validate(profile, original_text, conditions)
    except Exception:
        result = copy.deepcopy(profile)
        if isinstance(result, dict): result["status"] = "stale"
        return result
    if profile.get("status") != "current" or profile.get("sourceDigest") != source_digest(original_text, conditions):
        result = mark_stale(result)
    return result


def apply_client_profile_edits(incoming: Any, existing: Any, original_text: str | None,
                               conditions: dict[str, Any], *, trusted_profile: Any = None) -> dict[str, Any]:
    """Treat edited/new atoms as user authored; preserve trusted origin for exact matches.

    Client IDs, digest, status, unresolved, and origin are never accepted as facts.
    Existing unresolved notes are retained only when still source-grounded.
    """
    functions = _core()
    if not functions or not isinstance(original_text, str) or not original_text:
        raise ValueError("Atomic preference support is unavailable")
    build, source_digest, _, _ = functions
    if not isinstance(incoming, dict) or set(incoming) != {"schemaVersion", "sourceDigest", "status", "conditions", "unresolved"} or incoming.get("schemaVersion") != 2:
        raise ValueError("Invalid atomic preference profile")
    if not isinstance(incoming["conditions"], list) or len(incoming["conditions"]) > 80:
        raise ValueError("Invalid atomic preference conditions")
    old_current = response_profile_status(existing, original_text, conditions) if existing else None
    trusted_current = response_profile_status(trusted_profile, original_text, conditions) if trusted_profile else None
    old_by_fact = {}
    old_facts = set()
    trusted_facts = set()
    for profile in (old_current, trusted_current):
        if isinstance(profile, dict):
            target = trusted_facts if profile is trusted_current and trusted_profile is not None else old_facts
            for atom in profile.get("conditions", []):
                fact = {k: v for k, v in atom.items() if k not in {"id", "origin"}}
                key = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                target.add(key)
                # A trusted server interpretation takes precedence over a client echo.
                if profile is trusted_current and trusted_profile is not None:
                    old_by_fact[key] = atom.get("origin", "model")
                else:
                    old_by_fact.setdefault(key, atom.get("origin", "model"))
    incoming_facts = set()
    for atom in incoming["conditions"]:
        if isinstance(atom, dict):
            fact = {k: v for k, v in atom.items() if k not in {"id", "origin"}}
            incoming_facts.add(json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    manual_change = incoming_facts != old_facts
    trusted_match = incoming_facts.issubset(trusted_facts) and trusted_profile is not None
    same_source = bool(trusted_match and isinstance(trusted_current, dict) and trusted_current.get("status") == "current"
                       or isinstance(existing, dict) and existing.get("sourceDigest") == source_digest(original_text, conditions)
                       and existing.get("status") == "current")
    raw = []
    for atom in incoming["conditions"]:
        if not isinstance(atom, dict):
            raise ValueError("Invalid atomic preference condition")
        fact = {k: copy.deepcopy(v) for k, v in atom.items() if k not in {"id", "origin"}}
        key = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fact["origin"] = old_by_fact.get(key, "user")
        raw.append(fact)
    # Unresolved records are server analysis metadata; ignore the request copy.
    unresolved = []
    notes_source = trusted_current if isinstance(trusted_current, dict) and trusted_profile is not None else old_current
    if isinstance(notes_source, dict):
        for item in notes_source.get("unresolved", []):
            evidence = item.get("evidence", "")
            if not evidence or evidence in original_text:
                unresolved.append(item)
    result = build(raw, unresolved, original_text, conditions)
    result["status"] = "current" if same_source or manual_change else "stale"
    return result
