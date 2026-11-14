"""Bounded Gemini text embeddings and MMR helpers for public place candidates.

This module is an internal ranking aid. Embeddings never establish eligibility,
hard constraints, route facts, or public plan fields. Any incomplete provider
result disables semantics for the whole candidate pool.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.client import HTTPSConnection
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIMENSIONS = 768
MAX_CANDIDATES = 200
BATCH_SIZE = 100
EMBEDDING_BUDGET_SECONDS = 8.0
MAX_REQUEST_BYTES = 256_000
MAX_PUBLIC_TEXT_BYTES = 1_200
MAX_RESPONSE_BYTES = 4_000_000
_API_HOST = "generativelanguage.googleapis.com"
_API_PATH = "/v1beta/models/gemini-embedding-2:batchEmbedContents"
_CACHE_CAPACITY = 512
_MMR_LAMBDA = 0.8
_SEMANTIC_PENALTY = 0.2
_FLEXIBLE_EXCLUDED_ROLES = {"overnight_lodging", "overnight_camping"}
_PUBLIC_TEXT_FIELDS = (
    ("title", 300),
    ("overviewText", 700),
    ("contentTypeName", 100),
    ("planningRole", 80),
    ("experienceText", 300),
    ("courseText", 300),
)


@dataclass(frozen=True, repr=False)
class SemanticContext:
    """Process-local, non-serializable semantic data with safe diagnostics only."""

    enabled: bool
    status: str
    model: str = EMBEDDING_MODEL
    vectors: Mapping[int, tuple[float, ...]] = field(default_factory=dict, repr=False)
    diagnostics: Mapping[str, int | str | bool] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "vectors", MappingProxyType({key: tuple(value) for key, value in self.vectors.items()}))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))

    def __repr__(self) -> str:
        return (f"SemanticContext(enabled={self.enabled}, status={self.status!r}, model={self.model!r}, "
                f"vectorCount={len(self.vectors)})")


class _VectorCache:
    """Small process-local LRU. Keys contain hashes, never catalog text."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, int, str], tuple[float, ...]] = OrderedDict()

    @staticmethod
    def key(text: str, model: str = EMBEDDING_MODEL) -> tuple[str, int, str]:
        digest = hashlib.sha256(text.encode("utf-8", "strict")).hexdigest()
        return model, EMBEDDING_DIMENSIONS, digest

    def get(self, key: tuple[str, int, str]) -> tuple[float, ...] | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put_many(self, values: Mapping[tuple[str, int, str], tuple[float, ...]]) -> None:
        with self._lock:
            for key, vector in values.items():
                self._items[key] = vector
                self._items.move_to_end(key)
            while len(self._items) > _CACHE_CAPACITY:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_EMBEDDING_CACHE = _VectorCache()


def _disabled(status: str, *, cache_hits: int = 0, provider_calls: int = 0) -> SemanticContext:
    return SemanticContext(False, status, diagnostics={
        "candidateCount": 0,
        "cacheHits": cache_hits,
        "providerCalls": provider_calls,
    })


def _finite_contentid(row: Mapping[str, Any]) -> int | None:
    value = row.get("contentid")
    if type(value) is int and -(2**31) <= value <= 2**31 - 1:
        return value
    return None


def _public_place_text(row: Mapping[str, Any]) -> str | None:
    """Compose explicit catalog facts; never serialize an entire candidate row."""
    if not isinstance(row, Mapping):
        return None
    fields: list[str] = []
    total = 0
    for name, limit in _PUBLIC_TEXT_FIELDS:
        value = row.get(name)
        if not isinstance(value, str):
            continue
        # Bound processing even if a source catalog cell is unexpectedly huge.
        value = " ".join(value[:limit * 2].split())
        if not value:
            continue
        value = value[:limit]
        total += len(value)
        if total > 1_600:
            value = value[:max(0, 1_600 - (total - len(value)))]
        if value:
            fields.append(f"{name}: {value}")
        if total >= 1_600:
            break
    text = "\n".join(fields)
    try:
        text = text.encode("utf-8", "strict")[:MAX_PUBLIC_TEXT_BYTES].decode("utf-8", "ignore")
    except UnicodeError:
        return None
    return text if text else None


def _unit_vector(raw: Any) -> tuple[float, ...] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != EMBEDDING_DIMENSIONS:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in raw):
        return None
    # hypot is scaled to avoid overflow from otherwise finite provider values.
    norm = math.hypot(*(float(value) for value in raw))
    if not math.isfinite(norm) or norm <= 0:
        return None
    return tuple(float(value) / norm for value in raw)


def _default_gemini_provider(texts: list[str], *, timeout_seconds: float) -> list[list[float]]:
    """Use the existing AI package's key loader without reading BE config."""
    from chungbuk_trip_ai.gemini import load_gemini_settings

    provider_deadline = time.monotonic() + max(0.0, timeout_seconds)
    api_key = load_gemini_settings().api_key
    body = json.dumps({
        "requests": [{
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": EMBEDDING_DIMENSIONS,
        } for text in texts],
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("embedding request exceeded its safe size")
    remaining = provider_deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("embedding request deadline expired")
    connection = HTTPSConnection(_API_HOST, timeout=remaining)
    try:
        connection.timeout = max(0.01, provider_deadline - time.monotonic())
        connection.request("POST", _API_PATH, body=body, headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        })
        remaining = provider_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("embedding request deadline expired")
        connection.timeout = remaining
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        response = connection.getresponse()
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_RESPONSE_BYTES:
            remaining = provider_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("embedding response deadline expired")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            read_count = min(64_000, MAX_RESPONSE_BYTES + 1 - total)
            read_chunk = getattr(response, "read1", None) or response.read
            chunk = read_chunk(read_count)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if time.monotonic() >= provider_deadline:
                raise TimeoutError("embedding response deadline expired")
        raw = b"".join(chunks)
        if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("embedding provider request failed")
    finally:
        connection.close()
    payload = json.loads(raw)
    embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(embeddings, list):
        raise ValueError("embedding provider returned an invalid response")
    result = []
    for entry in embeddings:
        values = entry.get("values") if isinstance(entry, dict) else None
        if not isinstance(values, list):
            raise ValueError("embedding provider returned an invalid response")
        result.append(values)
    return result


def build_place_semantics(catalog: Any, candidate_pool: list[dict[str, Any]], *,
                          provider: Callable[..., list[list[float]]] | None = None,
                          deadline: float | None = None) -> SemanticContext:
    """Embed a complete, bounded candidate pool or disable semantic scoring.

    `deadline` is an absolute `time.monotonic()` deadline. Provider errors and
    incomplete coverage degrade to the existing category score without exposing
    error text, API keys, or catalog descriptions.
    """
    if provider is False:
        return _disabled("DISABLED_BY_CALLER")
    if not isinstance(candidate_pool, list) or len(candidate_pool) > MAX_CANDIDATES:
        return _disabled("CANDIDATE_LIMIT_EXCEEDED")
    if not candidate_pool:
        return _disabled("NO_CANDIDATES")

    catalog_rows = getattr(catalog, "places", ())
    catalog_by_id = {row.get("contentid"): row for row in catalog_rows
                      if isinstance(row, Mapping) and type(row.get("contentid")) is int}
    pairs: list[tuple[int, str, tuple[str, int, str]]] = []
    seen: set[int] = set()
    for candidate in candidate_pool:
        if not isinstance(candidate, Mapping):
            return _disabled("INVALID_CANDIDATE")
        contentid = _finite_contentid(candidate)
        row = catalog_by_id.get(contentid)
        if contentid is None or contentid in seen or not isinstance(row, Mapping):
            return _disabled("INVALID_CANDIDATE")
        seen.add(contentid)
        public_text = _public_place_text(row)
        if public_text is None:
            return _disabled("PUBLIC_TEXT_UNAVAILABLE")
        pairs.append((contentid, public_text, _EMBEDDING_CACHE.key(public_text)))

    vectors: dict[int, tuple[float, ...]] = {}
    pending_texts: list[str] = []
    pending_keys: list[tuple[str, int, str]] = []
    cache_hits = 0
    for contentid, text, key in pairs:
        cached = _EMBEDDING_CACHE.get(key)
        if cached is None:
            pending_texts.append(text)
            pending_keys.append(key)
        else:
            vectors[contentid] = cached
            cache_hits += 1

    provider_calls = 0
    staged_cache: dict[tuple[str, int, str], tuple[float, ...]] = {}
    request_provider = provider or _default_gemini_provider
    end = min(deadline if deadline is not None else float("inf"),
              time.monotonic() + EMBEDDING_BUDGET_SECONDS)
    offset = 0
    try:
        while offset < len(pending_texts):
            remaining = end - time.monotonic()
            if remaining <= 0:
                return _disabled("EMBEDDING_BUDGET_EXHAUSTED", cache_hits=cache_hits,
                                 provider_calls=provider_calls)
            batch_texts = pending_texts[offset:offset + BATCH_SIZE]
            provider_calls += 1
            raw_vectors = request_provider(batch_texts, timeout_seconds=remaining)
            if time.monotonic() >= end:
                return _disabled("EMBEDDING_BUDGET_EXHAUSTED", cache_hits=cache_hits,
                                 provider_calls=provider_calls)
            if not isinstance(raw_vectors, (list, tuple)) or len(raw_vectors) != len(batch_texts):
                return _disabled("INVALID_EMBEDDING_RESPONSE", cache_hits=cache_hits,
                                 provider_calls=provider_calls)
            batch_vectors = [_unit_vector(vector) for vector in raw_vectors]
            if any(vector is None for vector in batch_vectors):
                return _disabled("INVALID_EMBEDDING_RESPONSE", cache_hits=cache_hits,
                                 provider_calls=provider_calls)
            for key, vector in zip(pending_keys[offset:offset + len(batch_texts)], batch_vectors, strict=True):
                staged_cache[key] = vector
            offset += len(batch_texts)
        for (contentid, _text, key) in pairs:
            vectors[contentid] = _EMBEDDING_CACHE.get(key) or staged_cache.get(key)  # type: ignore[assignment]
        if len(vectors) != len(candidate_pool) or any(vector is None for vector in vectors.values()):
            return _disabled("INCOMPLETE_COVERAGE", cache_hits=cache_hits, provider_calls=provider_calls)
        if time.monotonic() >= end:
            return _disabled("EMBEDDING_BUDGET_EXHAUSTED", cache_hits=cache_hits,
                             provider_calls=provider_calls)
        _EMBEDDING_CACHE.put_many(staged_cache)
    except Exception:
        return _disabled("EMBEDDING_PROVIDER_ERROR", cache_hits=cache_hits, provider_calls=provider_calls)

    return SemanticContext(True, "READY", vectors=vectors, diagnostics={
        "candidateCount": len(candidate_pool),
        "cacheHits": cache_hits,
        "providerCalls": provider_calls,
    })


def _cosine(context: SemanticContext, left: int, right: int) -> float | None:
    a, b = context.vectors.get(left), context.vectors.get(right)
    if a is None or b is None or len(a) != EMBEDDING_DIMENSIONS or len(b) != EMBEDDING_DIMENSIONS:
        return None
    value = math.sumprod(a, b)
    if not math.isfinite(value):
        return None
    return max(-1.0, min(1.0, value))


def _is_excluded(row: Mapping[str, Any], anchor: bool) -> bool:
    return anchor or row.get("planningRole") in _FLEXIBLE_EXCLUDED_ROLES


def semantic_redundancy(draft: dict[str, Any], candidate_pool: dict[int, dict[str, Any]],
                        context: SemanticContext | None,
                        anchors: set[tuple[str, int]] | None = None) -> float | None:
    """Return order-invariant within-day semantic overlap, or None if unavailable."""
    if context is None or not context.enabled:
        return None
    anchors = anchors or set()
    day_values: list[float] = []
    for day in draft.get("days", []):
        eligible: list[int] = []
        for place in day.get("places", []):
            cid = place.get("contentid")
            row = candidate_pool.get(cid, place)
            if not isinstance(cid, int) or _is_excluded(row, (day.get("date"), cid) in anchors):
                continue
            if cid not in context.vectors:
                return None
            eligible.append(cid)
        if len(eligible) < 2:
            day_values.append(0.0)
            continue
        similarities = []
        for index, contentid in enumerate(eligible):
            related = [_cosine(context, contentid, other) for j, other in enumerate(eligible) if j != index]
            if any(value is None for value in related):
                return None
            similarities.append(max(0.0, max(related)))
        day_values.append(math.fsum(sorted(similarities)) / 2.0)
    return math.fsum(day_values)


def semantic_adjusted_score(draft: dict[str, Any], candidate_pool: dict[int, dict[str, Any]],
                            category_score: float, context: SemanticContext | None,
                            anchors: set[tuple[str, int]] | None = None) -> float:
    """Keep the category score and subtract a small order-independent overlap cost."""
    if (context is None or not context.enabled
            or any(type(cid) is not int or cid not in context.vectors for cid in candidate_pool)):
        return category_score
    redundancy = semantic_redundancy(draft, candidate_pool, context, anchors)
    if redundancy is None:
        return category_score
    return category_score - _SEMANTIC_PENALTY * redundancy


def mmr_order(candidates: list[dict[str, Any]], selected_ids: Iterable[int], context: SemanticContext | None,
              utility: Callable[[dict[str, Any]], float], *, limit: int | None = None,
              deadline: float | None = None) -> list[dict[str, Any]]:
    """Greedily order candidates by relevance (0.8) and semantic novelty (0.2)."""
    original = list(candidates)
    count = len(original) if limit is None else max(0, min(len(original), limit))
    fallback = original[:count]
    if not original:
        return []
    if len(original) > MAX_CANDIDATES or context is None or not context.enabled or any(
            type(row.get("contentid")) is not int or row["contentid"] not in context.vectors
            for row in original):
        return fallback
    try:
        relevance = []
        for row in original:
            value = utility(row)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return fallback
            relevance.append(float(value))
    except Exception:
        return fallback
    # Upstream utility is already normalized (rank utility is bounded [0, 1]).
    # Preserve its scale so small relevance differences can trade against novelty.
    normalized = [max(0.0, min(1.0, value)) for value in relevance]
    selected = {cid for cid in selected_ids if type(cid) is int}
    # Cache each unordered pair once and update only the similarity to the
    # newly chosen item. Recomputing every selected pair each round is cubic.
    pair_cache: dict[tuple[int, int], float | None] = {}

    def similarity(left, right):
        key = (min(left, right), max(left, right))
        if key not in pair_cache:
            pair_cache[key] = _cosine(context, left, right)
        return pair_cache[key]

    def expired():
        return deadline is not None and time.monotonic() >= deadline

    max_similarity: dict[int, float] = {}
    for index, row in enumerate(original):
        values = []
        for prior in selected:
            if expired():
                return fallback
            value = similarity(row["contentid"], prior)
            if value is None:
                return fallback
            values.append(value)
        max_similarity[index] = max(0.0, max(values, default=0.0))
    remaining = list(range(len(original)))
    ordered: list[dict[str, Any]] = []
    while remaining and len(ordered) < count:
        if expired():
            return fallback
        chosen_index = max(remaining, key=lambda index: (
            _MMR_LAMBDA * normalized[index] - (1.0 - _MMR_LAMBDA) * max_similarity[index], -index))
        chosen = original[chosen_index]
        ordered.append(chosen)
        remaining.remove(chosen_index)
        if len(ordered) == count:
            break
        for index in remaining:
            if expired():
                return fallback
            value = similarity(original[index]["contentid"], chosen["contentid"])
            if value is None:
                return fallback
            max_similarity[index] = max(max_similarity[index], value)
    return ordered
