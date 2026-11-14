"""Narrow resolver for walking-duration avoidance in private preference drafts.

Only a complete, duration-scoped walking clause can be satisfied by the
existing walking selector. The original request is inspected privately for
explicit limits, but is never returned by this module.
"""
from __future__ import annotations

import re
from typing import Any


_CLAUSE_SPLIT = re.compile(r"[,，;；.。\n]+|\s+(?:그리고|또는|하지만|그런데|이며|이고|및)\s+")
_DURATION = re.compile(r"(?:오래\s*걷(?:는\s*(?:것|건|게|건은|것은)?|기)|긴\s*도보(?:\s*이동)?|장시간\s*도보|도보\s*이동이\s*긴)")
_AVOID = re.compile(r"(?:싫|피하|줄이고\s*싶|원하지\s*않|부담|힘들|어려워|avoid|dislike|hate)", re.I)
_WALK_WORD = r"(?:도보|걷(?:기|는\s*(?:구간|시간|이동)))"
_EXPLICIT_LIMIT_PATTERNS = (
    re.compile(rf"{_WALK_WORD}[^0-9,，;；.!?。！？\n]{{0,12}}(?:최대|최고)\s*(\d{{1,3}})\s*분"),
    re.compile(rf"{_WALK_WORD}[^0-9,，;；.!?。！？\n]{{0,12}}(\d{{1,3}})\s*분\s*(?:이내|이하|까지만|을?\s*넘지\s*않게|을?\s*넘지\s*않도록)"),
    re.compile(rf"(?:최대|최고)\s*(\d{{1,3}})\s*분(?:\s*(?:이내|이하))?\s*{_WALK_WORD}"),
    re.compile(rf"(\d{{1,3}})\s*분\s*(?:이내|이하|까지만|을?\s*넘지\s*않게|을?\s*넘지\s*않도록)\s*{_WALK_WORD}"),
    re.compile(r"(?<!\d)(\d{1,3})\s*분\s*까지만\s*(?:걷|걸|도보)"),
    re.compile(r"(?<!\d)(\d{1,3})\s*분(?:을)?\s*(?:넘게|초과(?:해서)?)\s*(?:걷|도보)[^0-9,，;；.!?。！？\n]{0,16}(?:싫어요|피하고\s*싶어요)"),
)
_PURE_WALK_AVOID = re.compile(
    r"^(?:오래\s*걷(?:는\s*(?:것(?:은|이|도)?|건(?:은|이|도)?|게|코스(?:는)?|일정(?:은)?)|기)|"
    r"긴\s*도보(?:\s*이동)?)(?:은|는|이|을)?\s*(?:정말\s*)?(?:싫(?:어요|어)|"
    r"피하고\s*싶(?:어요|어)|부담(?:돼요|스러워요))?$"
)
_UNSUPPORTED_SCOPE = re.compile(r"계단|등산|산행|휠체어|접근성|보행\s*보조|하루|일일|총\s*(?:도보|걷)|전체\s*(?:도보|걷)|누적|킬로미터|킬로|km|거리|아예|전혀|한\s*걸음도|걷지\s*않|도보\s*없이|질문|궁금|인가요|일까요|않아요\s*\?", re.I)
_NEGATED_OR_QUESTION = re.compile(r"(?:걷는\s*것은?\s*싫지\s*않|오래\s*걷(?:기|는\s*(?:것|게|건))?\s*싫지\s*않|피하고\s*싶지\s*않|피해야\s*하나요|\?|？)")


_STRICT_UPPER = re.compile(r"\d{1,3}\s*분\s*이상\s*(?:걷|도보)[^,，;；.!?。！？\n]{0,16}(?:싫어요|피하고\s*싶어요)")


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def _walking_avoid_clause(clause: str) -> bool:
    # Require the entire clause to be a duration-only avoid request. This
    # leaves mixed categories and extra conditions available for ordinary
    # required-preference validation.
    return bool(_PURE_WALK_AVOID.fullmatch(clause.strip())
                and not _UNSUPPORTED_SCOPE.search(clause)
                and not _NEGATED_OR_QUESTION.search(clause))


def _explicit_max(text: str) -> int | None:
    values = []
    for clause in _clauses(text):
        if _NEGATED_OR_QUESTION.search(clause) or _UNSUPPORTED_SCOPE.search(clause):
            continue
        for pattern in _EXPLICIT_LIMIT_PATTERNS:
            values.extend(int(match.group(1)) for match in pattern.finditer(clause))
    return min(values) if values else None


def resolve_walking_preference(draft: dict[str, Any]) -> dict[str, Any]:
    """Return residual avoid text plus whether the selected cap resolves it.

    `originalText` is deliberately only read for explicit bound evidence; it
    is not included in the result or any public issue detail.
    """
    avoid = draft.get("avoid") if isinstance(draft.get("avoid"), str) else ""
    original = draft.get("originalText") if isinstance(draft.get("originalText"), str) else ""
    source_max = _explicit_max(" ".join((original, avoid)))
    selected = draft.get("walking")
    selected_max = int(selected) if selected in {"20", "40"} else None
    clauses = _clauses(avoid)
    recognized = [clause for clause in clauses if _walking_avoid_clause(clause)]
    residual = [clause for clause in clauses if clause not in recognized]
    required = draft.get("avoidImportance") == "required"

    source_has_unresolved_walking_scope = any(
        _STRICT_UPPER.search(clause) or (
            _UNSUPPORTED_SCOPE.search(clause) and re.search(r"도보|걷", clause)
            and (_AVOID.search(clause) or re.search(r"(?:최대|이내|이하|까지만|넘지\s*않)", clause)))
        for clause in _clauses(original)
    )

    issue = None
    if required and recognized:
        if source_has_unresolved_walking_scope:
            issue = "unsupportedScope"
        elif selected == "unlimited":
            issue = "unlimited"
        elif selected_max is None:
            issue = "missing"
        elif source_max is not None and selected_max > source_max:
            issue = "conflict"
    elif required and any(((_DURATION.search(clause) and _AVOID.search(clause))
                           or (_UNSUPPORTED_SCOPE.search(clause) and re.search(r"도보|걷", clause)))
                          for clause in clauses):
        issue = "unsupportedScope"

    return {
        "recognizedWalkingAvoid": bool(recognized),
        "remainingAvoid": " 그리고 ".join(residual) or None,
        "issue": issue,
        "sourceMaxMinutes": source_max,
    }
