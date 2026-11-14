"""Offline tests for bounded public-catalog Gemini embeddings and MMR."""
from types import SimpleNamespace

import pytest

from app import place_semantics as semantics


@pytest.fixture(autouse=True)
def clear_process_cache():
    semantics._EMBEDDING_CACHE.clear()
    yield
    semantics._EMBEDDING_CACHE.clear()


def vector(axis=0, dimensions=semantics.EMBEDDING_DIMENSIONS, sign=1):
    result = [0.0] * dimensions
    if dimensions:
        result[axis % dimensions] = float(sign)
    return result


def rows(*, descriptions=None, extras=None):
    descriptions = descriptions or {1: "전망과 산책", 2: "전망을 즐기는 산책", 3: "조용한 전시"}
    extras = extras or {}
    return SimpleNamespace(places=[{
        "contentid": cid,
        "title": f"공개 장소 {cid}",
        "overviewText": description,
        "contentTypeName": "관광지",
        "planningRole": "sightseeing_stop",
        **extras.get(cid, {}),
    } for cid, description in descriptions.items()])


def candidates(ids=(1, 2, 3), **extra):
    return [{"contentid": cid, "groupScore": float(len(ids) - index), **extra}
            for index, cid in enumerate(ids)]


def test_provider_receives_only_allowlisted_public_catalog_text():
    catalog = rows(extras={1: {"nickname": "PRIVATE_MEMBER_TEXT", "memberScore": 99}})
    pool = candidates(groupScore="PRIVATE_GROUP_SCORE", memberRef="PRIVATE_MEMBER_REF",
                      originalText="PRIVATE_ORIGINAL_TEXT", preferences="PRIVATE_PREFS")
    captured = []

    def fake_provider(texts, *, timeout_seconds):
        captured.extend(texts)
        assert timeout_seconds <= semantics.EMBEDDING_BUDGET_SECONDS
        return [vector(i) for i, _ in enumerate(texts)]

    result = semantics.build_place_semantics(catalog, pool, provider=fake_provider)
    assert result.enabled is True
    assert result.status == "READY"
    assert len(result.vectors) == 3
    joined = "\n".join(captured)
    for private_value in ("PRIVATE_ORIGINAL_TEXT", "PRIVATE_PREFS", "PRIVATE_MEMBER_REF",
                          "PRIVATE_MEMBER_TEXT", "PRIVATE_GROUP_SCORE", "99"):
        assert private_value not in joined
    assert "title: 공개 장소 1" in joined
    assert "overviewText: 전망과 산책" in joined
    assert "contentTypeName: 관광지" in joined
    assert result.diagnostics["candidateCount"] == 3


def test_disabled_by_caller_never_calls_provider():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider should stay disabled")

    result = semantics.build_place_semantics(rows(), candidates(), provider=False)
    assert not result.enabled
    assert result.status == "DISABLED_BY_CALLER"


@pytest.mark.parametrize("bad_vectors", [
    [],
    [vector(dimensions=4), vector(), vector()],
    [vector(), [True] + [0.0] * (semantics.EMBEDDING_DIMENSIONS - 1), vector()],
    [vector(), [float("nan")] + [0.0] * (semantics.EMBEDDING_DIMENSIONS - 1), vector()],
    [vector(), [0.0] * semantics.EMBEDDING_DIMENSIONS, vector()],
])
def test_incomplete_or_invalid_embedding_response_disables_whole_pool(bad_vectors):
    result = semantics.build_place_semantics(rows(), candidates(), provider=lambda *_a, **_kw: bad_vectors)
    assert not result.enabled
    assert result.status == "INVALID_EMBEDDING_RESPONSE"
    assert result.vectors == {}


@pytest.mark.parametrize("error", [TimeoutError("private timeout detail"), RuntimeError("secret detail 123")])
def test_provider_failure_is_safe_and_disables_whole_pool(error):
    result = semantics.build_place_semantics(
        rows(), candidates(), provider=lambda *_a, **_kw: (_ for _ in ()).throw(error))
    assert not result.enabled
    assert result.status == "EMBEDDING_PROVIDER_ERROR"
    assert result.diagnostics["providerCalls"] == 1
    assert "secret" not in repr(result)
    assert "private" not in repr(result)
    assert result.vectors == {}


def test_default_rest_provider_uses_existing_key_loader_and_validated_public_request(monkeypatch):
    from chungbuk_trip_ai import gemini
    import json

    captured = {}

    class FakeResponse:
        status = 200
        body = json.dumps({"embeddings": [{"values": vector()}]}).encode()

        def read1(self, size):
            assert 0 < size <= 64_000
            body, self.body = self.body, b""
            return body

    class FakeConnection:
        def __init__(self, host, *, timeout):
            captured["host"] = host
            captured["timeout"] = timeout

        def request(self, method, path, *, body, headers):
            captured["method"], captured["path"] = method, path
            captured["body"] = body
            captured["headers"] = headers

        def getresponse(self):
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(gemini, "load_gemini_settings", lambda: SimpleNamespace(api_key="fake-test-key"))
    monkeypatch.setattr(semantics, "HTTPSConnection", FakeConnection)
    result = semantics._default_gemini_provider(["title: public test"], timeout_seconds=2.0)
    assert result == [vector()]
    assert captured["host"] == "generativelanguage.googleapis.com"
    assert captured["method"] == "POST"
    assert captured["path"] == semantics._API_PATH
    assert captured["headers"]["x-goog-api-key"] == "fake-test-key"
    request = json.loads(captured["body"])
    assert request["requests"] == [{
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": "title: public test"}]},
        "outputDimensionality": 768,
    }]
    assert captured["closed"] is True


def test_provider_cannot_enable_semantics_after_passing_its_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(semantics, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def slow_provider(texts, *, timeout_seconds):
        # Advance an isolated clock: Windows timer granularity makes a real
        # 1ms deadline plus an 11ms sleep an unreliable timeout regression.
        now[0] += timeout_seconds + 1.0
        return [vector(index) for index, _ in enumerate(texts)]

    result = semantics.build_place_semantics(
        rows(), candidates(), provider=slow_provider, deadline=now[0] + 0.001)
    assert not result.enabled
    assert result.status == "EMBEDDING_BUDGET_EXHAUSTED"
    assert result.vectors == {}


def test_absolute_deadline_exhaustion_skips_provider():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("expired deadline must skip provider")

    result = semantics.build_place_semantics(rows(), candidates(), provider=forbidden, deadline=0.0)
    assert not result.enabled
    assert result.status == "EMBEDDING_BUDGET_EXHAUSTED"


def test_missing_public_text_or_invalid_pool_disables_without_provider():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid candidate must not call provider")

    assert semantics.build_place_semantics(SimpleNamespace(places=[]), candidates(), provider=forbidden).status == "INVALID_CANDIDATE"
    no_title = SimpleNamespace(places=[{"contentid": 1, "overviewText": None}])
    assert semantics.build_place_semantics(no_title, candidates((1,)), provider=forbidden).status == "PUBLIC_TEXT_UNAVAILABLE"
    assert semantics.build_place_semantics(rows(), candidates((1, 1)), provider=forbidden).status == "INVALID_CANDIDATE"
    assert semantics.build_place_semantics(rows(), candidates(tuple(range(201))), provider=forbidden).status == "CANDIDATE_LIMIT_EXCEEDED"


def test_batches_at_one_hundred_and_disables_all_if_later_batch_fails(monkeypatch):
    semantics._EMBEDDING_CACHE.clear()
    ids = tuple(range(1, 102))
    catalog = rows(descriptions={cid: f"장소 설명 {cid}" for cid in ids})
    pool = candidates(ids)
    calls = []

    def provider(texts, *, timeout_seconds):
        calls.append((len(texts), timeout_seconds))
        if len(calls) == 2:
            raise TimeoutError("second batch unavailable")
        return [vector(index) for index, _ in enumerate(texts)]

    result = semantics.build_place_semantics(catalog, pool, provider=provider)
    assert [count for count, _ in calls] == [100, 1]
    assert not result.enabled
    assert result.status == "EMBEDDING_PROVIDER_ERROR"
    assert result.vectors == {}
    assert semantics._EMBEDDING_CACHE.get(semantics._VectorCache.key("title: 공개 장소 1\noverviewText: 장소 설명 1\ncontentTypeName: 관광지\nplanningRole: sightseeing_stop")) is None


def test_vector_cache_uses_model_and_text_digest_and_only_caches_complete_result():
    semantics._EMBEDDING_CACHE.clear()
    first_calls = []

    def first(texts, *, timeout_seconds):
        first_calls.append(texts)
        return [vector()] * len(texts)

    first_result = semantics.build_place_semantics(rows(), candidates(), provider=first)
    assert first_result.enabled and len(first_calls) == 1
    second_calls = []
    second_result = semantics.build_place_semantics(
        rows(), candidates(), provider=lambda texts, **_kw: second_calls.append(texts) or [])
    assert second_result.enabled
    assert second_calls == []
    assert second_result.diagnostics["cacheHits"] == 3

    changed = semantics._VectorCache.key("different description")
    changed_model = semantics._VectorCache.key("title: 공개 장소 1\noverviewText: 전망과 산책\ncontentTypeName: 관광지\nplanningRole: sightseeing_stop",
                                                model="different-model")
    standard = semantics._VectorCache.key("title: 공개 장소 1\noverviewText: 전망과 산책\ncontentTypeName: 관광지\nplanningRole: sightseeing_stop")
    assert changed != standard
    assert changed_model != standard


def test_semantic_context_hides_vectors_and_unknown_semantics_fall_back_to_category_score():
    context = semantics.SemanticContext(True, "READY", vectors={1: tuple(vector())})
    assert "1.0" not in repr(context)
    assert "vectors=" not in repr(context)
    original = {"days": [{"date": "2026-12-12", "places": [{"contentid": 1}, {"contentid": 2}]}]}
    pool = {1: {"contentid": 1, "planningRole": "sightseeing_stop"},
            2: {"contentid": 2, "planningRole": "sightseeing_stop"}}
    assert semantics.semantic_redundancy(original, pool, context) is None
    assert semantics.semantic_adjusted_score(original, pool, 4.5, context) == 4.5
    assert semantics.semantic_adjusted_score(original, pool, 4.5, None) == 4.5


def test_semantic_redundancy_and_adjusted_score_do_not_depend_on_visit_order():
    context = semantics.SemanticContext(True, "READY", vectors={
        1: tuple(vector(0)),
        2: tuple(vector(0)),
        3: tuple(vector(1)),
        4: tuple(vector(0)),
    })
    pool = {cid: {"contentid": cid, "planningRole": "sightseeing_stop"} for cid in range(1, 5)}
    first = {"days": [{"date": "2026-12-12", "places": [{"contentid": 1}, {"contentid": 2}, {"contentid": 3}]}]}
    reordered = {"days": [{"date": "2026-12-12", "places": [{"contentid": 3}, {"contentid": 2}, {"contentid": 1}]}]}
    assert semantics.semantic_redundancy(first, pool, context) == semantics.semantic_redundancy(reordered, pool, context)
    assert semantics.semantic_adjusted_score(first, pool, 3.0, context) == semantics.semantic_adjusted_score(reordered, pool, 3.0, context)
    assert semantics.semantic_redundancy(first, pool, context, anchors={("2026-12-12", 1)}) == 0.0


def test_mmr_moves_a_relevant_but_less_redundant_candidate_ahead():
    context = semantics.SemanticContext(True, "READY", vectors={
        1: tuple(vector(0)),
        2: tuple(vector(0)),
        3: tuple(vector(1)),
    })
    pool = candidates()
    pool_by_id = {row["contentid"]: row for row in pool}
    pool_by_id[1]["groupScore"] = 1.0
    pool_by_id[2]["groupScore"] = 0.95
    pool_by_id[3]["groupScore"] = 0.90
    before = list(pool)
    ordered = semantics.mmr_order(pool, (), context, lambda row: row["groupScore"])
    assert [row["contentid"] for row in ordered] == [1, 3, 2]
    assert pool == before
    assert semantics.mmr_order([], (), context, lambda row: 0.0) == []


def test_mmr_returns_original_order_for_partial_coverage_and_never_weakens_required_filters():
    context = semantics.SemanticContext(True, "READY", vectors={1: tuple(vector(0)), 2: tuple(vector(1))})
    pool = candidates()
    ordered = semantics.mmr_order(pool, (), context, lambda row: row["groupScore"])
    assert [row["contentid"] for row in ordered] == [1, 2, 3]
    # Ineligible candidates are not generated here; MMR only ranks its given pool.
    pool.append({"contentid": 4, "planningRole": "unreviewed_medical", "requiredFilterPassed": False})
    ordered = semantics.mmr_order(pool, (), context, lambda row: row.get("groupScore", 0.0))
    assert ordered == pool


def test_semantic_objective_falls_back_when_replacement_pool_has_a_missing_vector():
    context = semantics.SemanticContext(True, "READY", vectors={1: tuple(vector(0)), 2: tuple(vector(1))})
    draft = {"days": [{"date": "2026-12-12", "places": [{"contentid": 1}, {"contentid": 2}]}]}
    pool = {1: {"contentid": 1, "planningRole": "sightseeing_stop"},
            2: {"contentid": 2, "planningRole": "sightseeing_stop"},
            3: {"contentid": 3, "planningRole": "sightseeing_stop"}}
    assert semantics.semantic_adjusted_score(draft, pool, 2.0, context) == 2.0


@pytest.mark.parametrize("request_seconds", [1.5, 2.1])
def test_rest_response_wait_uses_only_deadline_remaining_after_request(monkeypatch, request_seconds):
    from chungbuk_trip_ai import gemini
    import json

    now, observed = [0.0], {}

    class Response:
        status = 200
        body = json.dumps({"embeddings": [{"values": vector()}]}).encode()

        def read1(self, _size):
            result, self.body = self.body, b""
            return result

    class Connection:
        def __init__(self, _host, *, timeout):
            self.sock = SimpleNamespace(settimeout=lambda value: observed.update(socketTimeout=value))

        def request(self, *_args, **_kwargs):
            now[0] += request_seconds

        def getresponse(self):
            observed["responseRequested"] = True
            assert self.timeout == pytest.approx(0.5)
            assert observed["socketTimeout"] == pytest.approx(0.5)
            return Response()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(gemini, "load_gemini_settings", lambda: SimpleNamespace(api_key="fake-test-key"))
    monkeypatch.setattr(semantics.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(semantics, "HTTPSConnection", Connection)
    if request_seconds > 2:
        with pytest.raises(TimeoutError):
            semantics._default_gemini_provider(["public"], timeout_seconds=2)
        assert "responseRequested" not in observed
    else:
        assert semantics._default_gemini_provider(["public"], timeout_seconds=2) == [vector()]
    assert observed["closed"]


def test_rich_korean_catalog_fits_two_byte_bounded_batches_and_failed_second_call_counts():
    catalog = rows(descriptions={cid: "전망과 문화 체험 " * 500 for cid in range(200)})
    pool = candidates(tuple(range(200)))
    calls = []

    def provider(texts, **_kwargs):
        calls.append(len(texts))
        assert all(len(text.encode("utf-8")) <= semantics.MAX_PUBLIC_TEXT_BYTES for text in texts)
        return [vector()] * len(texts)

    result = semantics.build_place_semantics(catalog, pool, provider=provider)
    assert result.enabled and calls == [100, 100]
    semantics._EMBEDDING_CACHE.clear()
    calls.clear()

    def failing(texts, **kwargs):
        if calls:
            raise TimeoutError("synthetic second batch failure")
        return provider(texts, **kwargs)

    failed = semantics.build_place_semantics(catalog, pool, provider=failing)
    assert not failed.enabled and not failed.vectors
    assert failed.diagnostics["providerCalls"] == 2


def test_full_production_pool_mmr_evaluates_each_pair_at_most_once(monkeypatch):
    pool = candidates(tuple(range(200)))
    context = semantics.SemanticContext(True, "SYNTHETIC", vectors={cid: tuple(vector(cid)) for cid in range(200)})
    cosine = semantics._cosine
    calls = []

    def counted(context, left, right):
        calls.append((min(left, right), max(left, right)))
        return cosine(context, left, right)

    monkeypatch.setattr(semantics, "_cosine", counted)
    ordered = semantics.mmr_order(pool, (), context, lambda row: 0.5)
    assert len(ordered) == 200
    assert len(calls) == len(set(calls)) == 200 * 199 // 2
    calls.clear()
    assert len(semantics.mmr_order(pool, (), context, lambda row: 0.5, limit=8)) == 8
    assert len(calls) < 200 * 8
    assert semantics.mmr_order(pool, (), context, lambda row: 0.5, deadline=0) == pool
