"""Rank regional canvas candidates against the user's words and public place facts."""
from __future__ import annotations

import re
from types import SimpleNamespace

from .canvas_recommendation import MAX_POOL
from .korean_text import SearchIntent
from .preference_retrieval import PreferenceResolver
from .route_preferences import is_market_place

_MARKET = {"시장", "전통시장", "재래시장", "장터"}
_INSIDE = re.compile(r"\s*(?:(?:안쪽|안|내부|내|구내)(?=\s|$|에|의|로|[,.)]))")
_NEAR = re.compile(r"\s*(?:의\s*)?(?:인근|근처|주변|옆|맞은편|가까이|인접)")
_NOT_CURRENT = re.compile(r"있던|있었던|옮겼|이전했|아니|않|없|폐장|예전에")
_CONTEXT = {"near", "nearby", "around"}


def _located(candidate: dict, location: str, *, nearby: bool = False) -> bool:
    city = candidate.get("adminCity")
    if city and city in {location, location + "시", location + "군"}:
        return True
    # A named market can end in 시장; 전시장 is an exhibition hall.
    pattern = r"역전시장|(?<!전)시장|장터" if location in _MARKET else re.escape(location)
    for field in ("address", "overviewText"):
        for segment in re.split(r"[.!?\n;]", candidate.get(field) or ""):
            for match in re.finditer(pattern, segment, re.I):
                tail = segment[match.end():]
                if _NOT_CURRENT.search(tail[:35]):
                    continue
                if _INSIDE.match(tail) or (nearby and _NEAR.match(tail)):
                    return True
    return False


def _excerpt(value: str | None, terms: tuple[str, ...], size: int) -> str | None:
    """Keep a bounded provider excerpt around the matching fact, including later menu items."""
    if not value or len(value) <= size:
        return value or None
    needles = [term.removesuffix("하다") for term in terms if term]
    positions = [pos for term in needles if (pos := value.casefold().find(term.casefold())) >= 0]
    start = min(max(0, min(positions) - size // 3), len(value) - size) if positions else 0
    return value[start:start + size]


def _public_candidate(row: dict, terms: tuple[str, ...]) -> dict:
    result = {key: value for key, value in row.items() if key not in {"tags", "recommendationUse"}}
    overview = row.get("overviewText") or ""
    relevant_tags = [tag for tag in row["tags"] if any(term.removesuffix("하다") in tag for term in terms)]
    if relevant_tags:
        overview = " · ".join([*relevant_tags[:3], overview]).strip(" ·")
    result["overviewText"] = _excerpt(overview, terms, 80)
    result["menuText"] = _excerpt(row.get("menuText"), terms, 40)
    return result


def rank_candidates(rows: list[dict], intent: SearchIntent, limit: int) -> list[dict]:
    """Intersect positive evidence and exclude negative evidence before calling Gemini."""
    limit = max(0, min(limit, MAX_POOL))
    terms = tuple(t for t in intent.terms if t.casefold() not in _CONTEXT)
    scoped_places = tuple(loc for group in intent.scoped_locations for loc in (*group.inside, *group.nearby))
    evidence_terms = (*terms, *intent.locations, *intent.nearby_locations, *scoped_places)
    if not (evidence_terms or intent.excluded):
        return [_public_candidate(p, ()) for p in rows[:limit]] if intent.generic else []
    if len(set((*evidence_terms, *intent.excluded))) > 40:
        return []

    places = [{**row, "contentTypeName": row["category"]} for row in rows]
    details = [{"contentid": row["contentid"], "menuText": row["menuText"]} for row in rows]
    prefs = [{"contentid": row["contentid"], "preferenceTags": "|".join(row["tags"])} for row in rows]
    resolver = PreferenceResolver(SimpleNamespace(places=places, details=details, preferences=prefs))
    market_ids = {p["contentid"] for p in places if is_market_place(p, set(p["tags"]))}

    def matching_ids(term: str) -> set[int]:
        if term in intent.food_terms:
            return set(resolver.resolve(term, "food") or ())
        if term in _MARKET:
            return market_ids
        if term in intent.modifiers:
            return set(resolver.resolve(term, "atmosphere") or ())
        activity = set(resolver.resolve(term, "activity") or ())
        food = set(resolver.resolve(term, "food") or ())
        if term in {"산", "강", "회"}:
            return activity | food
        literal = term.casefold()
        direct = {p["contentid"] for p in rows if literal in {
            (p["title"] or "").casefold(), (p["category"] or "").casefold(), *(t.casefold() for t in p["tags"])
        }}
        if literal in {"카페", "자연"}:
            return set(resolver.resolve(term, "atmosphere") or ()) | direct
        if activity:
            return activity | direct
        if food:
            return food | direct
        if direct or len(literal) < 2:
            return direct
        return {p["contentid"] for p in rows if literal in p["title"].casefold()}

    hits = {term: matching_ids(term) for term in dict.fromkeys((*terms, *intent.excluded))}
    by_id = {row["contentid"]: row for row in rows}
    def scoped_hits(term: str, *, negative: bool) -> set[int]:
        groups = [g for g in intent.scoped_locations if g.negative == negative and term in g.terms]
        if not groups:
            return hits[term]
        return {cid for cid in hits[term] if any(
            all(_located(by_id[cid], loc) for loc in group.inside)
            and all(_located(by_id[cid], loc, nearby=True) for loc in group.nearby) for group in groups
        )}
    allowed = {term: scoped_hits(term, negative=False) for term in terms}
    choice_terms = {term for group in intent.alternatives for term in group}
    eligible = {p["contentid"] for p in rows}
    for term in terms:
        if term not in choice_terms:
            eligible.intersection_update(allowed[term])
    for group in intent.alternatives:
        eligible.intersection_update(set().union(*(allowed.get(t, ()) for t in group)))
    for term in intent.excluded:
        eligible.difference_update(scoped_hits(term, negative=True))
    ranked = []
    for row in rows:
        if row["contentid"] not in eligible:
            continue
        if not all(_located(row, loc) for loc in intent.locations):
            continue
        if not all(_located(row, loc, nearby=True) for loc in intent.nearby_locations):
            continue
        title = row["title"].casefold()
        score = sum(3 if title == t.casefold() else 2 if t.casefold() in title
                    else int(t in row["tags"]) for t in terms)
        ranked.append((-score, row["contentid"], row))
    ranked.sort(key=lambda item: (item[0], item[1]))
    # A single recommendation can fulfil either visit. Represent each alternative in the provider pool.
    first_per_choice = []
    for group in intent.alternatives:
        for term in group:
            first = next((cid for _, cid, _ in ranked if cid in allowed.get(term, ())), None)
            if first is not None:
                first_per_choice.append(first)
    ordered = list(dict.fromkeys([*first_per_choice, *(cid for _, cid, _ in ranked)]))[:limit]
    return [_public_candidate(by_id[cid], evidence_terms) for cid in ordered]
