"""Deterministic confirmed-v1 mapping; unknown soft text remains unscored."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

POLICY_VERSION = "confirmed-v1"
ALIASES = {
    "자연": ("자연", "자연 관광", "자연 명소", "자연 풍경"),
    "문화": ("문화", "문화 체험", "역사 문화"),
    "액티비티": ("액티비티", "활동", "체험"),
    "휴식": ("휴식", "휴양", "힐링"),
    "쇼핑": ("쇼핑",),
    "맛집": ("맛집", "현지 음식", "음식"),
    "카페": ("카페",),
}
NEGATIVE = re.compile(r"싫|피하|빼|안\s*먹|먹지\s*않|못\s*먹|금지|제외|avoid|dislike|hate|no\b|not\b|don't|do not|cannot|can't", re.I)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，;；.!?\n]+|\s+(?:그리고|또는|하지만|그런데|인데|이고|및)\s+", text) if part.strip()]


def _mentions(clause: str, phrase: str) -> bool:
    return re.search(r"(?<![가-힣A-Za-z0-9])" + re.escape(phrase) + r"(?=$|[\s은는이가을를도만의와과로에])", clause) is not None


def _fully_covered(clause: str, phrases: list[str]) -> bool:
    remainder = clause
    for phrase in sorted(set(phrases), key=len, reverse=True):
        remainder = re.sub(re.escape(phrase), " ", remainder)
    # Accept only the small grammar used to express a known tag preference.
    # Any remaining content (notably allergy/accessibility requirements) keeps
    # the whole hard clause unsupported.
    remainder = re.sub(r"[\s은는이가을를도만의와과로에서에게한테]+", "", remainder)
    wrappers = ("좋아해요", "좋아해", "좋아합니다", "좋아", "선호해요", "선호합니다", "선호", "원해요", "원해",
                "가고싶어요", "가고싶어", "꼭", "필수", "싫어요", "싫어", "싫고", "피해주세요", "피하고싶어요",
                "피하고싶어", "제외해주세요", "빼주세요", "안먹어요", "안먹어", "먹지않아요", "먹지않아")
    for token in wrappers:
        remainder = remainder.replace(token, "")
    return remainder == ""


def analyze_preferences(preferences: list[dict[str, Any]], supported_tags) -> dict[str, Any]:
    tags = set(supported_tags)
    predicates: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for pref in preferences:
        ref = pref["memberRef"]
        for field in ("likes", "avoid", "food"):
            source = pref.get(field)
            if not isinstance(source, str) or not source.strip():
                continue
            importance = pref.get("foodImportance") if field == "food" else pref.get("avoidImportance") if field == "avoid" else "preferred"
            required = importance == "required"
            matches: set[tuple[str, str]] = set()
            conflict = False
            clauses = _clauses(source)
            unsupported_clause = False
            for clause in clauses:
                negative = bool(NEGATIVE.search(clause))
                clause_matches = 0
                matched_phrases = []
                for tag, aliases in ALIASES.items():
                    hits = [phrase for phrase in aliases if tag in tags and _mentions(clause, phrase)]
                    if not hits:
                        continue
                    matched_phrases.extend(hits)
                    if field == "likes" and negative:
                        conflict = True
                        continue
                    polarity = "avoid" if field == "avoid" or (field == "food" and negative) else "prefer"
                    if field == "food" and negative and not required:
                        continue
                    matches.add((tag, polarity))
                    clause_matches += 1
                if required and clause_matches == 0:
                    unsupported_clause = True
                if required and clause_matches and not _fully_covered(clause, matched_phrases):
                    unsupported_clause = True
            if conflict:
                issues.append({"field": field, "code": "PREFERENCE_CONFLICT"})
            if required and (not matches or unsupported_clause):
                issues.append({"field": field, "code": "REQUIRED_PREFERENCE_UNSUPPORTED"})
            for tag, polarity in sorted(matches):
                predicates.append({"memberRef": ref, "field": field, "tag": tag,
                                   "polarity": polarity, "scope": "meal" if field == "food" else "all", "required": required})
    doc = {"policyVersion": POLICY_VERSION, "predicates": predicates, "issues": issues}
    doc["digest"] = hashlib.sha256(_canonical(doc).encode()).hexdigest()
    return doc


def preference_issues(preferences: list[dict[str, Any]], supported_tags) -> list[dict[str, str]]:
    issues = analyze_preferences(preferences, supported_tags)["issues"]
    if any(item.get("scope") == "personal" for pref in preferences for item in pref.get("scheduleItems", [])):
        issues.append({"field": "scheduleItems", "code": "PERSONAL_SCHEDULE_NOT_SHARED"})
    return issues


def group_schedule_anchors(preferences: list[dict[str, Any]], common_settings: dict[str, Any] | None = None,
                           identity: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Create stable slots for owner-confirmed shared schedule items."""
    anchors = []
    for field in ("lodging", "fixedSchedule"):
        for item in (common_settings or {}).get("reviewedCommonFields", []):
            if isinstance(item, dict) and item.get("field") == field and item.get("reviewed") is True:
                anchors.extend(dict(a) for a in item.get("anchors", []) if isinstance(a, dict))
    occupied = {(a.get("date"), a.get("position")) for a in anchors}
    members = {f"p{index}": member for index, member in enumerate((identity or {}).get("members", []), 1)}
    shared = [(pref.get("memberRef", ""), item) for pref in preferences for item in pref.get("scheduleItems", [])
              if item.get("scope") == "group" and
              members.get(pref.get("memberRef", ""), {}).get("role") == "OWNER"]
    shared.sort(key=lambda pair: (pair[1].get("date") or "", pair[1].get("time") or "", pair[0], pair[1].get("id", "")))
    for _ref, item in shared:
        if item.get("contentid") is None or item.get("date") is None:
            continue
        try:
            first = date.fromisoformat(item["date"])
            last = date.fromisoformat(item.get("endDate") or item["date"])
            if last < first or (last - first).days > 30:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED") from None
        if item["kind"] == "appointment" and last != first:
            raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
        days = []
        current = first
        overnight_range = item["kind"] == "lodging" and last > first
        # A checkout date ends the stay; it is not another hotel visit/night.
        while current < last if overnight_range else current <= last:
            days.append(current.isoformat())
            current += timedelta(days=1)
        for day in days:
            position = next((n for n in range(8) if (day, n) not in occupied), None)
            if position is None:
                raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
            if item["kind"] == "lodging" and any(a.get("kind") == "lodging" and a.get("date") == day and a.get("contentid") != item["contentid"] for a in anchors):
                raise ValueError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
            occupied.add((day, position))
            first_day, last_day = day == first.isoformat(), day == last.isoformat()
            checkout_night = overnight_range and date.fromisoformat(day) == last - timedelta(days=1)
            anchors.append({"date": day, "contentid": item["contentid"], "position": position,
                            "kind": item["kind"],
                            "startTime": item.get("time") if first_day else None,
                            "endTime": item.get("endTime") if last_day and not overnight_range else None,
                            "checkoutTime": item.get("endTime") if checkout_night else None,
                            "endDate": item.get("endDate")})
    return anchors
