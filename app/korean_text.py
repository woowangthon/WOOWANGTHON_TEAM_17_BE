"""Analyze the user's Korean wording once, before looking up public catalog facts."""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiwipiepy import Token

_MAX_QUERY_CHARS = 1000
_NOUN = {"NNG", "NNP", "SL", "SH", "SN", "NR"}
_PARTICLE = {"JKS", "JKC", "JKG", "JKO", "JKB", "JKV", "JKQ", "JX", "JC"}
# These refer to the request/visit itself. They are not public place facts.
_REQUEST_NOUNS = {"추천", "추천지", "요청", "여행", "여행지", "장소", "곳", "군데", "일정", "방문", "구경",
                  "오늘", "내일", "이번", "모두", "제외", "선호", "식사", "섭취", "명소"}
_INSIDE = {"안", "안쪽", "내", "내부", "구내"}
_NEAR = {"근처", "주변", "인근", "옆"}
_OPEN_QUERY = re.compile(r"추천|아무|어디|무엇|여행지|장소|갈\s*곳|가볼\s*[만한]*\s*곳|anywhere|recommend", re.I)
_KIWI = None
_INIT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class LocatedTerms:
    terms: tuple[str, ...]
    inside: tuple[str, ...]
    nearby: tuple[str, ...]
    negative: bool


@dataclass(frozen=True, slots=True)
class SearchIntent:
    terms: tuple[str, ...]
    excluded: tuple[str, ...]
    locations: tuple[str, ...]
    exclusive: bool
    food_terms: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()
    generic: bool = False
    nearby_locations: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, ...], ...] = ()
    scoped_locations: tuple[LocatedTerms, ...] = ()


def _tokenize(text: str):
    global _KIWI
    if _KIWI is None:
        with _INIT_LOCK:
            if _KIWI is None:
                from kiwipiepy import Kiwi
                _KIWI = Kiwi(num_workers=1)
    return _KIWI.tokenize(text)


def _clauses(tokens: list[Token]):
    """Keep auxiliaries with their verb; stop polarity leaking into the next visit."""
    start = 0
    for i, token in enumerate(tokens):
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        joins_actions = token.tag == "EC" and token.form in {"고", "지만", "는데", "으며", "며", "어서", "아서", "거나"}
        if joins_actions and following and following.tag in {"VX", "JX"}:
            continue
        if token.tag in {"SF", "SP"} or joins_actions:
            yield tokens[start:i + 1]
            start = i + 1
    if start < len(tokens):
        yield tokens[start:]


def _noun_phrases(text: str, clause: list[Token]):
    """Keep adjacent morphemes in their original word, such as 전통 + 시장."""
    i = 0
    while i < len(clause):
        if clause[i].tag not in _NOUN:
            i += 1
            continue
        start = clause[i].start
        end = start
        while i < len(clause) and clause[i].tag in _NOUN and clause[i].start == end:
            end = clause[i].start + clause[i].len
            i += 1
        word = text[start:end]
        if word in _REQUEST_NOUNS or word.isdecimal():
            continue
        particles = []
        while i < len(clause) and clause[i].tag in _PARTICLE:
            particles.append(clause[i].form)
            i += 1
        yield word, particles


def _adjectives(clause: list[Token]):
    for i, token in enumerate(clause[:-1]):
        following = clause[i + 1]
        if following.tag != "ETM":
            continue
        if token.tag.partition("-")[0] == "VA":
            yield token.form + "다"
        elif token.tag == "XSA" and i and clause[i - 1].tag in {"XR", "NNG"}:
            yield clause[i - 1].form + token.form + "다"


def analyze_search_intent(query: str) -> SearchIntent:
    if not isinstance(query, str) or len(query.strip()) > _MAX_QUERY_CHARS:
        return SearchIntent((), (), (), False)
    text = unicodedata.normalize("NFC", query.strip())
    if not text:
        return SearchIntent((), (), (), False, generic=True)
    terms, excluded, locations, nearby, food_terms, modifiers = [], [], [], [], [], []
    alternatives, independent_goals, located = [], [], []
    pending_inside, pending_near = [], []
    exclusive = optional = False
    for clause in _clauses(_tokenize(text)):
        forms = {t.form for t in clause}
        negative = any((t.form, t.tag) in {("안", "MAG"), ("못", "MAG"), ("말", "VV"), ("말", "VX"),
                                            ("싫", "VA"), ("싫어하", "VV"), ("빼", "VV"), ("피하", "VV"),
                                            ("제외", "NNG"), ("않", "VX"), ("아니", "VCN")} for t in clause)
        # 안 가도 돼 expresses permission to skip, not an exclusion or a required visit.
        if negative and "되" in forms and forms & {"어도", "아도"}:
            optional = True
            continue
        eats = any((t.tag == "VV" and t.form in {"먹", "마시", "맛보", "드시"}) or
                   (t.tag == "NNG" and t.form in {"식사", "섭취"}) for t in clause)
        heads = list(_noun_phrases(text, clause))
        start, negative_start = len(terms), len(excluded)
        inside, near = [], []
        if not negative:
            inside, near = pending_inside, pending_near
            pending_inside, pending_near = [], []
        index = 0
        while index < len(heads):
            word, particles = heads[index]
            index += 1
            if index < len(heads) and heads[index][0] in _INSIDE | _NEAR:
                relation, _ = heads[index]
                (near if relation in _NEAR else inside).append(word)
                index += 1
            elif index < len(heads) and any(p in {"에", "에서"} for p in particles):
                inside.append(word)
            elif negative:
                excluded.append(word)
            elif word in {"아이", "가족", "친구"} and set(particles) & {"와", "과", "랑", "이랑"}:
                # Companions describe the visit; the following noun identifies the destination.
                continue
            elif word == "밥" and index < len(heads) and eats:
                continue
            else:
                terms.append(word)
                exclusive |= "만" in particles
                if eats:
                    food_terms.append(word)
        cues = list(_adjectives(clause))
        if negative:
            excluded.extend(cues)
        else:
            requested = terms[start:]
            direct = [(word, parts) for word, parts in heads if word in requested]
            if len(direct) >= 2 and (all(set(p) & {"나", "이나"} for _, p in direct[:-1])
                                     or all("도" in p for _, p in direct)):
                alternatives.append(tuple(word for word, _ in direct))
            if len(requested) == 1 and not cues and (eats or "가" in forms or "방문" in forms):
                independent_goals.append(requested[0])
            terms.extend(cues)
            modifiers.extend(cues)
        if inside or near:
            # In "시장에서 국수 말고 만두", the following alternative inherits the venue.
            if negative and forms & {"말", "빼", "제외"}:
                pending_inside.extend(inside)
                pending_near.extend(near)
            clause_terms = excluded[negative_start:] if negative else terms[start:]
            located.append(LocatedTerms(tuple(clause_terms), tuple(inside), tuple(near), negative))

    def distinct(items):
        return tuple(dict.fromkeys(items))
    scoped = []
    for group in located:
        if not group.negative and (not group.terms or set(terms) <= set(group.terms)):
            locations.extend(group.inside)
            nearby.extend(group.nearby)
        else:
            scoped.append(group)
    # A relation required of every result also covers exclusions at that same location.
    scoped = [s for s in scoped if not (set(s.inside) <= set(locations) and set(s.nearby) <= set(nearby))]
    generic = not (terms or excluded or locations or nearby) and (optional or bool(_OPEN_QUERY.search(text)))
    if len(distinct(independent_goals)) > 1:
        alternatives.append(distinct(independent_goals))
    return SearchIntent(distinct(terms), distinct(excluded), distinct(locations), exclusive,
                        distinct(food_terms), distinct(modifiers), generic, distinct(nearby),
                        distinct(alternatives), tuple(scoped))


def normalize_target(value: str) -> str:
    """Strip trailing case/additive particles on an already structured noun phrase."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value.strip())
    if not text or len(text) > 120:
        return text
    if not re.search(r"[가-힣]$", text):
        return text
    tokens = _tokenize(text)
    stop = len(tokens)
    while stop and tokens[stop - 1].tag in _PARTICLE:
        stop -= 1
    if not stop or stop == len(tokens) or tokens[stop - 1].tag not in _NOUN:
        return text
    # A whole preference sentence may contain exclusions or multiple actions.
    # Only trim an actual noun phrase; leave its wording and evidence intact otherwise.
    if any(t.tag not in _NOUN | {"XR", "XSA", "VA", "ETM", "MM"} for t in tokens[:stop]):
        return text
    return text[:tokens[stop].start].rstrip()
