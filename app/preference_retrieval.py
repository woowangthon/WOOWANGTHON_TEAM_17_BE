"""Deterministic, catalog-grounded retrieval for confirmed text preferences.

This module deliberately returns IDs only. It never uses embeddings, external
services, raw evidence, or fuzzy similarity to establish a match.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .korean_text import normalize_target


_ROLE_GROUPS = {
    "food": {"meal_stop"},
    "activity": {"sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop", "date_specific_event"},
    "atmosphere": {"meal_stop", "sightseeing_stop", "culture_stop", "activity_stop", "shopping_stop",
                   "date_specific_event"},
}
_DIMENSION_NAMES = {
    "food": "food", "meal": "food", "restaurant": "food",
    "activity": "activity", "place": "activity", "venue": "activity",
    "atmosphere": "atmosphere", "vibe": "atmosphere", "ambience": "atmosphere",
}
_TAG_FIELDS = ("preferenceTags", "vibeTags")
_FOOD_EQUIVALENTS = {
    "돈까스": ("돈까스", "돈가스", "돈카츠"),
    "돈가스": ("돈까스", "돈가스", "돈카츠"),
    "돈카츠": ("돈까스", "돈가스", "돈카츠"),
}
_ATMOSPHERE_CUES = {
    "quiet": ("조용", "한적", "고요"),
    "calm": ("차분", "평온", "잔잔"),
    "rest": ("휴식", "휴양", "힐링", "휴식힐링"),
    "nature": ("자연", "숲", "산림"),
    "cafe": ("카페",),
}
_UNSAFE_SINGLE = {"강", "산", "회"}
_MOUNTAIN_TITLE = re.compile(r"[가-힣]+산(?:\s*\([가-힣 ]+\))?")
_HIKING_TAGS = {"등산", "트레킹등산", "등산트레킹", "산행"}
_NON_DISH_LABELS = {"곳", "군데", "식사", "끼니", "식당", "음식", "메뉴"}
_TRAILING = re.compile(r"(?:에가고싶어요|에가고싶어|에가고싶다|먹고싶어요|먹고싶어|먹고싶다|"
                       r"방문하고싶어요|방문하고싶어|방문하고싶다|가고싶어요|가고싶어|가고싶다|"
                       r"좋아해요|좋아해|선호해요|선호해|원해요|원해|꼭|필수)+$")
_NONWORD = re.compile(r"[^0-9a-z가-힣]+", re.I)
_NEGATED_TARGET = re.compile(r"(?:하지\s*(?:는\s*)?않|하지\s*(?:는\s*)?못|아닌|아니|싫|피하|없(?:어요|습니다)?|금지|제외|not|no)", re.I)
_NEGATED_AVAILABILITY = re.compile(r"(?:제공|판매|취급|메뉴|운영).{0,12}(?:하지\s*않|하지\s*못|없|불가|종료|중단|품절)|(?:없|불가|종료|품절).{0,12}(?:제공|판매|취급|메뉴)|미(?:제공|판매|취급)|품절|판매\s*중단", re.I)
_NEARBY = re.compile(r"(?:근처|인근|주변|가까운|인접)(?:의|에|에있는|에위치한)?")
_FIELD_SPLIT = re.compile(r"[,，;；.!?\n|/·ㆍ:()\[\]{}]+")
_DIRECT_SUFFIXES = frozenset({"집", "식당", "전문점", "관", "장", "점", "가게", "체험", "명소",
                              "한", "한곳", "한공간", "스러운", "스럽게", "으로", "에서", "이", "가",
                              "은", "는", "의", "을", "를", "도", "하기", "하는", "하며", "하고", "한다"})


@dataclass(frozen=True, slots=True)
class _SearchSegment:
    text: str
    chunks: tuple[str, ...]
    unavailable: bool
    negated: bool
    nearby: bool


def _compact(value: Any) -> str:
    return _NONWORD.sub("", unicodedata.normalize("NFC", value).casefold()) if isinstance(value, str) else ""


def _normalized_query(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFC", value).casefold()
    cleaned = " ".join(re.sub(r"[^0-9a-z가-힣\s]+", " ", value).split())
    compact = _compact(cleaned)
    stripped = _TRAILING.sub("", compact)
    # Keep phrase boundaries for component matching unless a known polite or
    # availability wrapper was removed.
    if stripped != compact:
        suffix = compact[len(stripped):]
        suffix_pattern = r"\s*".join(re.escape(char) for char in suffix) + r"\s*$"
        cleaned = re.sub(suffix_pattern, "", cleaned).strip()
    return cleaned if _NEGATED_TARGET.search(value) else normalize_target(cleaned)


def _valid_id(row: Any) -> bool:
    value = row.get("contentid") if isinstance(row, Mapping) else None
    return type(value) is int and -(2**31) <= value <= 2**31 - 1


def _tag_values(row: Mapping[str, Any]) -> list[str]:
    result = []
    for name in _TAG_FIELDS:
        value = row.get(name)
        if isinstance(value, str):
            result.extend(part.strip() for part in value.split("|") if part.strip())
    return result


def _search_segments(value: Any) -> tuple[_SearchSegment, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    segments = []
    for raw in _FIELD_SPLIT.split(unicodedata.normalize("NFC", value).casefold()):
        text = _compact(raw)
        if not text:
            continue
        chunks = tuple(_compact(chunk) for chunk in re.split(r"\s+", raw) if _compact(chunk))
        segments.append(_SearchSegment(
            text=text,
            chunks=chunks,
            unavailable=bool(_NEGATED_AVAILABILITY.search(raw)),
            negated=bool(_NEGATED_TARGET.search(raw)),
            nearby=bool(_NEARBY.search(text)),
        ))
    return tuple(segments)


def _matches_segment(segment: _SearchSegment, phrase: str, *, positive_only: bool = True) -> bool:
    needle = phrase.casefold() if isinstance(phrase, str) and phrase.isalnum() else _compact(phrase)
    if not needle or segment.nearby:
        return False
    matched = segment.text == needle or any(
        chunk == needle or chunk.endswith(needle)
        or (chunk.startswith(needle) and chunk[len(needle):] in _DIRECT_SUFFIXES)
        for chunk in segment.chunks
    )
    return matched and not (positive_only and segment.negated)


def _query_terms(query: str, kind: str) -> tuple[str, ...] | None:
    compact = _compact(query)
    if not compact or len(compact) > 80:
        return None
    if compact in _UNSAFE_SINGLE:
        if compact in {"강", "산"} and kind == "activity":
            return (compact,)
        if compact == "회" and kind == "food":
            return (compact,)
        return None
    if kind == "food":
        if compact in _UNSAFE_SINGLE or compact in _NON_DISH_LABELS:
            return None
        if _NEGATED_TARGET.search(query):
            return None
        equivalents = _FOOD_EQUIVALENTS.get(compact)
        return equivalents or (compact,)
    if kind == "atmosphere":
        if _NEGATED_TARGET.search(query):
            return None
        return tuple(_compact(part) for part in query.split() if _compact(part))
    parts = tuple(_compact(part) for part in query.split() if _compact(part))
    if (_NEGATED_TARGET.search(query) or re.search(r"근처|인근|주변|가까운|인접", query)
            or len(compact) < 2 or compact in _UNSAFE_SINGLE):
        return None
    return parts or (compact,)


class PreferenceResolver:
    """Build one private index per compile and memoize normalized lookups."""

    def __init__(self, catalog: Any):
        places = [row for row in getattr(catalog, "places", ()) if isinstance(row, Mapping) and _valid_id(row)]
        counts = Counter(row["contentid"] for row in places)
        self._places = {row["contentid"]: dict(row) for row in places if counts[row["contentid"]] == 1}
        self._details = self._unique_rows(getattr(catalog, "details", ()))
        self._preferences = self._unique_rows(getattr(catalog, "preferences", ()))
        self._search: dict[int, dict[str, tuple[_SearchSegment, ...]]] = {}
        short_matches: dict[tuple[str, str], list[int]] = {}
        short_absences: dict[tuple[str, str], list[int]] = {}
        short_contradictions: dict[tuple[str, str], list[int]] = {}
        for cid, place in self._places.items():
            detail = self._details.get(cid, {})
            tags = _tag_values(self._preferences.get(cid, {}))
            title_fields = (place.get("title"), place.get("contentTypeName"))
            activity_fields = (*title_fields, place.get("overviewText"), detail.get("experienceText"),
                               detail.get("courseText"), *tags)
            activity_segments = tuple(segment for value in activity_fields for segment in _search_segments(value))
            self._search[cid] = {
                "food": tuple(segment for value in (*title_fields, detail.get("menuText"), *tags)
                              for segment in _search_segments(value)),
                "activity": activity_segments,
                "atmosphere": activity_segments,
                "cafe": tuple(segment for value in (*title_fields, *tags) for segment in _search_segments(value)),
            }
            if place.get("planningRole") in _ROLE_GROUPS["activity"]:
                exact_type = _compact(place.get("contentTypeName"))
                tag_keys = {_compact(tag) for tag in tags}
                # The catalog uses the broad type 관광지 for mountains. A proper
                # mountain name plus a hiking tag identifies the destination itself.
                title = place.get("title")
                mountain_name = unicodedata.normalize("NFKC", title).strip() if isinstance(title, str) else ""
                named_mountain = (place.get("planningRole") == "sightseeing_stop"
                                  and bool(tag_keys & _HIKING_TAGS) and _MOUNTAIN_TITLE.fullmatch(mountain_name))
                for term in {"강", "산"}:
                    if exact_type == term or term in tag_keys or (term == "산" and named_mountain):
                        short_matches.setdefault(("activity", term), []).append(cid)
            if place.get("planningRole") == "meal_stop":
                menu_segments = _search_segments(detail.get("menuText"))
                exact_menu = [segment for segment in menu_segments
                              if segment.text == "회" or "회" in segment.chunks]
                positive_menu = any(not segment.unavailable and not segment.negated for segment in exact_menu)
                unavailable_menu = any(segment.unavailable for segment in exact_menu)
                tag_claim = any(_compact(tag) == "회" for tag in tags)
                if (positive_menu and unavailable_menu) or (unavailable_menu and tag_claim):
                    short_contradictions.setdefault(("food", "회"), []).append(cid)
                elif positive_menu:
                    short_matches.setdefault(("food", "회"), []).append(cid)
                elif unavailable_menu:
                    short_absences.setdefault(("food", "회"), []).append(cid)
        self._short_matches = {key: tuple(sorted(ids)) for key, ids in short_matches.items()}
        self._short_absences = {key: tuple(sorted(ids)) for key, ids in short_absences.items()}
        self._short_contradictions = {key: tuple(sorted(ids)) for key, ids in short_contradictions.items()}
        self._cache: dict[tuple[str, str], tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None] = {}

    @staticmethod
    def _unique_rows(rows: Any) -> dict[int, dict[str, Any]]:
        valid = [row for row in rows if isinstance(row, Mapping) and _valid_id(row)]
        counts = Counter(row["contentid"] for row in valid)
        return {row["contentid"]: dict(row) for row in valid if counts[row["contentid"]] == 1}

    def resolve(self, target: Any, source_dimension: Any) -> list[int] | None:
        """Return positive catalog IDs, empty for a valid lookup with no hits.

        ``None`` means that the dimension or target is too ambiguous to ground.
        """
        kind = _DIMENSION_NAMES.get(source_dimension) if isinstance(source_dimension, str) else None
        query = _normalized_query(target)
        if kind is None:
            return None
        key = (kind, query)
        if key in self._cache:
            cached = self._cache[key]
            return list(cached[0]) if cached is not None else None
        terms = _query_terms(query, kind)
        if terms is None:
            self._cache[key] = None
            return None
        atmosphere_phrases = tuple(_atmosphere_phrases(term) for term in terms) if kind == "atmosphere" else ()
        compact_query = _compact(query)
        if compact_query in {"강", "산", "회"}:
            short_kind = "food" if compact_query == "회" else "activity"
            short_key = (short_kind, compact_query)
            result = (self._short_matches.get(short_key, ()), self._short_absences.get(short_key, ()),
                      self._short_contradictions.get(short_key, ()))
            self._cache[key] = result if kind == short_kind else None
            return list(result[0]) if kind == short_kind else None
        matches: list[int] = []
        known_absences: list[int] = []
        contradictions: list[int] = []
        for cid, place in self._places.items():
            if place.get("planningRole") not in _ROLE_GROUPS[kind]:
                continue
            segments = self._search[cid][kind]
            if kind == "food":
                explicit_unavailable = any(segment.unavailable and _matches_segment(segment, term, positive_only=False)
                                            for segment in segments for term in terms)
                positive = any(not segment.unavailable and _matches_segment(segment, term)
                               for segment in segments for term in terms)
                found = positive and not explicit_unavailable
                if explicit_unavailable and not positive:
                    known_absences.append(cid)
                elif explicit_unavailable and positive:
                    contradictions.append(cid)
            elif kind == "activity":
                explicit_negative = any((segment.negated or segment.unavailable)
                                        and any(_matches_segment(segment, term, positive_only=False) for term in terms)
                                        for segment in segments)
                positive = all(any(_matches_segment(segment, term) and not segment.unavailable
                                   for segment in segments) for term in terms)
                found = positive and not explicit_negative
                if explicit_negative and positive:
                    contradictions.append(cid)
            else:
                # Each cue must be supported by this same place. In particular,
                # a compound request such as 조용한 카페 needs both signals.
                found = True
                explicit_negative = any(segment.negated and any(
                    _matches_segment(segment, phrase, positive_only=False)
                    for phrases in atmosphere_phrases for phrase in phrases) for segment in segments)
                for cue, phrases in zip(terms, atmosphere_phrases, strict=True):
                    cue_fields = self._search[cid]["cafe"] if cue == "카페" else segments
                    cue_found = any(_matches_segment(segment, phrase) for segment in cue_fields for phrase in phrases)
                    if not cue_found:
                        found = False
                        break
                positive = found
                found = positive and not explicit_negative
                if explicit_negative and positive:
                    contradictions.append(cid)
            if found:
                matches.append(cid)
        result = (tuple(sorted(matches)), tuple(sorted(known_absences)), tuple(sorted(contradictions)))
        self._cache[key] = result
        return list(result[0])

    def known_contentids(self, target: Any, source_dimension: Any) -> list[int] | None:
        """Return only IDs with explicit negative/unavailable source evidence."""
        self.resolve(target, source_dimension)
        kind = _DIMENSION_NAMES.get(source_dimension) if isinstance(source_dimension, str) else None
        query = _normalized_query(target)
        value = self._cache.get((kind or "", query))
        return list(value[1]) if value is not None else None

    def contradicted_contentids(self, target: Any, source_dimension: Any) -> list[int] | None:
        """Return IDs whose public evidence includes both positive and negative claims."""
        self.resolve(target, source_dimension)
        kind = _DIMENSION_NAMES.get(source_dimension) if isinstance(source_dimension, str) else None
        query = _normalized_query(target)
        value = self._cache.get((kind or "", query))
        return list(value[2]) if value is not None else None


def _atmosphere_phrases(term: str) -> tuple[str, ...]:
    """Expand recognized literal mood words while preserving unknown qualifiers."""
    for _name, phrases in _ATMOSPHERE_CUES.items():
        normalized = tuple(_compact(phrase) for phrase in phrases)
        if any(term.startswith(phrase) for phrase in normalized):
            return phrases
    return (term,)
