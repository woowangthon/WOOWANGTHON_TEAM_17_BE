"""Stage-4 orchestration with synthetic detached inputs and no external calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app import generation_draft as module


DATES = ["2026-12-12", "2026-12-13"]
CANDIDATES = [{"contentid": cid, "title": f"합성 장소{cid}", "planningRole": "sightseeing_stop",
               "adminCity": "단양군", "allowedDates": DATES, "groupScore": 0.0}
              for cid in (101, 102)]
RAW_DRAFT = {"days": [{"date": DATES[0], "contentids": [101]}, {"date": DATES[1], "contentids": [102]}]}


class Selection:
    ready_for_draft = True

    def draft_candidates(self):
        return [dict(row) for row in CANDIDATES]

    def fixed_visits(self):
        return []

    def as_dict(self):
        return {"normalizationDigest": "norm", "policyDigest": "policy", "pendingValidation": ["ROUTE_UNVERIFIED"]}


class Catalog:
    digest = "catalog"

    def presentation_place(self, contentid):
        return {"contentid": contentid, "title": f"DB 장소{contentid}", "feeLabel": "요금 확인 필요",
                "coordinate": {"latitude": 36.6286, "longitude": 127.4341, "crs": "WGS84"}}


@pytest.fixture
def inputs(monkeypatch):
    monkeypatch.setattr(module, "build_candidate_selection", lambda *_: Selection())
    snapshot = SimpleNamespace(fingerprint="source", as_dict=lambda: {"conditions": {
        "startDate": DATES[0], "endDate": DATES[1]}})
    return snapshot, Catalog()


def invoke(inputs, **kwargs):
    snapshot, catalog = inputs
    return asyncio.run(module.generate_candidate_draft(
        snapshot, {}, catalog, {}, kwargs.pop("recapture", lambda: snapshot),
        kwargs.pop("reload_catalog", lambda: catalog), **kwargs))


def test_draft_joins_only_catalog_facts_and_preserves_detached_unverified_status(inputs):
    observed = []

    def provider(dates, candidates, anchors, maximum):
        observed.append((dates, candidates, anchors, maximum))
        return RAW_DRAFT

    result = invoke(inputs, drafter=provider)
    document = result.as_dict()
    assert document["requiresReview"] is True
    assert set(document["validation"].values()) == {False}
    assert document["days"][0]["places"][0]["title"] == "DB 장소101"
    assert document["days"][0]["places"][0]["feeLabel"] == "요금 확인 필요"
    assert "openingHoursText" not in document["days"][0]["places"][0]
    assert "memberScores" not in str(observed)
    document["days"].clear()
    assert len(result.as_dict()["days"]) == 2
    assert "source" not in repr(result)


def test_unreviewed_selection_never_calls_provider(inputs, monkeypatch):
    selection = Selection()
    selection.ready_for_draft = False
    monkeypatch.setattr(module, "build_candidate_selection", lambda *_: selection)
    with pytest.raises(module.GenerationDraftError, match="CANDIDATE_REVIEW_REQUIRED"):
        invoke(inputs, drafter=lambda *_: pytest.fail("provider must not run"))


@pytest.mark.parametrize("changed", ["source", "catalog", "authority"])
def test_changes_during_provider_never_return_a_draft(inputs, changed):
    snapshot, catalog = inputs
    state = {"provider_ran": False}

    def provider(*_):
        state["provider_ran"] = True
        return RAW_DRAFT

    def recapture():
        if state["provider_ran"] and changed == "authority":
            raise RuntimeError("private authority diagnostic")
        if state["provider_ran"] and changed == "source":
            return SimpleNamespace(fingerprint="changed")
        return snapshot

    def reload_catalog():
        return SimpleNamespace(digest="changed") if state["provider_ran"] and changed == "catalog" else catalog

    with pytest.raises(module.GenerationDraftError) as error:
        invoke(inputs, recapture=recapture, reload_catalog=reload_catalog, drafter=provider)
    assert error.value.code in {"STALE_GENERATION_INPUT", "STALE_PLACE_CATALOG", "SOURCE_NO_LONGER_ACCESSIBLE"}
    assert "private" not in str(error.value)


def test_revoked_before_provider_and_late_timeout_are_not_adopted(inputs):
    def revoked():
        raise RuntimeError("private revocation reason")

    with pytest.raises(module.GenerationDraftError, match="SOURCE_NO_LONGER_ACCESSIBLE"):
        invoke(inputs, recapture=revoked, drafter=lambda *_: pytest.fail("provider must not run"))
    release = threading.Event()

    def late(*_):
        release.wait(2)
        return RAW_DRAFT

    async def exercise():
        snapshot, catalog = inputs
        try:
            with pytest.raises(module.GenerationDraftError, match="AI_TIMEOUT"):
                await module.generate_candidate_draft(snapshot, {}, catalog, {}, lambda: snapshot, lambda: catalog,
                                                      drafter=late, timeout_seconds=0.03)
        finally:
            release.set()

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["before", "after"])
def test_total_deadline_covers_database_freshness_callbacks(inputs, phase):
    snapshot, catalog = inputs
    release = threading.Event()
    state = {"provider_ran": False}

    def provider(*_):
        state["provider_ran"] = True
        return RAW_DRAFT

    def capture():
        if phase == "before" or state["provider_ran"]:
            release.wait(2)
        return snapshot

    async def exercise():
        try:
            with pytest.raises(module.GenerationDraftError, match="AI_TIMEOUT"):
                await module.generate_candidate_draft(snapshot, {}, catalog, {}, capture, lambda: catalog,
                                                      drafter=provider, timeout_seconds=0.03)
            if phase == "before":
                assert state["provider_ran"] is False
        finally:
            release.set()

    asyncio.run(exercise())


def test_known_ai_input_subclass_has_safe_input_failure_code(inputs):
    from chungbuk_trip_ai.scheduling import SchedulingInputError

    def invalid(*_):
        raise SchedulingInputError("private diagnostic")

    with pytest.raises(module.GenerationDraftError, match="AI_DRAFT_INPUT_INVALID") as error:
        invoke(inputs, drafter=invalid)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("mutation", ["candidate", "anchor", "dates"])
def test_provider_cannot_mutate_the_validator_authority(inputs, monkeypatch, mutation):
    selection = Selection()
    if mutation == "anchor":
        selection.fixed_visits = lambda: [{"date": DATES[0], "contentid": 101, "position": 0}]
    monkeypatch.setattr(module, "build_candidate_selection", lambda *_: selection)

    def mutate(dates, candidates, anchors, maximum):
        if mutation == "candidate":
            candidates.append(dict(candidates[0], contentid=999))
            return {"days": [{"date": dates[0], "contentids": [999]}, {"date": dates[1], "contentids": [102]}]}
        if mutation == "anchor":
            anchors.clear()
            return {"days": [{"date": dates[0], "contentids": [102]}, {"date": dates[1], "contentids": [101]}]}
        dates[0] = "2026-12-11"
        return {"days": [{"date": dates[0], "contentids": [101]}, {"date": dates[1], "contentids": [102]}]}

    with pytest.raises(module.GenerationDraftError):
        invoke(inputs, drafter=mutate)


@pytest.mark.parametrize("invalid", [
    {"days": [{"date": DATES[0], "contentids": [999]}]},
    {"days": RAW_DRAFT["days"], "routeVerified": True},
    {"days": RAW_DRAFT["days"], "fee": 0},
    {"days": [{"date": DATES[0], "contentids": [True]}, RAW_DRAFT["days"][1]]},
])
def test_injected_drafter_cannot_bypass_output_validation(inputs, invalid):
    with pytest.raises(module.GenerationDraftError):
        invoke(inputs, drafter=lambda *_: invalid)


@pytest.mark.parametrize("limits", [{"max_visits_per_day": True}, {"timeout_seconds": float("nan")},
                                    {"max_visits_per_day": 9}, {"timeout_seconds": 61}])
def test_limits_reject_invalid_values_before_provider(inputs, limits):
    with pytest.raises(module.GenerationDraftError, match="INVALID_DRAFT_LIMITS"):
        invoke(inputs, drafter=lambda *_: pytest.fail("provider must not run"), **limits)
