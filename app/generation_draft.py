"""Private stage-3 selection and stage-4 proposal orchestration; never saves plans."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from .candidate_selection import build_candidate_selection, build_confirmed_candidate_selection


class GenerationDraftError(Exception):
    """An internal code with no source text, SQL, or provider diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, repr=False)
class CandidateDraft:
    _document: str = field(repr=False)

    def __repr__(self) -> str:
        return "CandidateDraft(<private, unverified proposal>)"

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self._document)


def _trip_dates(snapshot) -> list[str]:
    try:
        conditions = snapshot.as_dict()["conditions"]
        start, end = conditions["startDate"], conditions["endDate"]
        if not all(isinstance(v, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v) for v in (start, end)):
            raise ValueError
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        count = (last - first).days + 1
        if not 1 <= count <= 31:
            raise ValueError
        return [(first + timedelta(days=i)).isoformat() for i in range(count)]
    except Exception:
        raise GenerationDraftError("UNSUPPORTED_DRAFT_DATES") from None


def _default_drafter(dates, candidates, fixed_visits, max_visits_per_day, schedule_context=None):
    try:
        from chungbuk_trip_ai import draft_candidate_itinerary
    except Exception:
        raise GenerationDraftError("AI_UNAVAILABLE") from None
    return draft_candidate_itinerary(dates, candidates, fixed_visits, max_visits_per_day, schedule_context)


def _validate(dates, candidates, fixed_visits, max_visits_per_day, result, schedule_context=None):
    try:
        from chungbuk_trip_ai import validate_candidate_itinerary
    except Exception:
        raise GenerationDraftError("AI_UNAVAILABLE") from None
    flags = {"requiresReview": True, "routeVerified": False, "timeVerified": False, "budgetVerified": False}
    if not isinstance(result, dict) or set(result) not in ({"days"}, {"days", *flags}):
        raise GenerationDraftError("AI_INVALID_DRAFT")
    if set(result) != {"days"} and any(type(result[k]) is not bool or result[k] != value for k, value in flags.items()):
        raise GenerationDraftError("AI_INVALID_DRAFT")
    return validate_candidate_itinerary(dates, candidates, fixed_visits, max_visits_per_day,
                                        {"days": result["days"]}, schedule_context)


def _schedule_context(snapshot, common_settings, dates, catalog):
    """Pass only confirmed clock windows and captured endpoints to the model."""
    conditions = snapshot.as_dict()["conditions"]
    settings = common_settings or {}
    window = settings.get("activityWindow", {})
    start, end = window.get("start", "09:00"), window.get("end", "20:00")
    departure_time = conditions.get("departureTime")
    day_windows = [{"date": day, "startTime": departure_time if index == 0 and isinstance(departure_time, str) else start,
                   "endTime": end} for index, day in enumerate(dates)]
    def endpoint(value):
        if not isinstance(value, dict):
            return None
        return {"label": value.get("label"), "coordinate": value.get("coordinate")}
    departure = endpoint(settings.get("departureAnchor"))
    return_anchor = endpoint(settings.get("returnAnchor"))
    if conditions.get("departureContentid") is not None:
        place = catalog.presentation_place(conditions["departureContentid"])
        if place is not None:
            departure = {"label": place["title"], "coordinate": place.get("coordinate")}
    if departure is None and isinstance(conditions.get("departure"), str):
        departure = {"label": conditions["departure"], "coordinate": None}
    if return_anchor is None and conditions.get("returnRoute"):
        return_anchor = {"label": conditions["returnRoute"], "coordinate": None}
    context = {"dayWindows": day_windows, "departureAnchor": departure, "returnAnchor": return_anchor}
    times = {}
    for group in [*settings.get("reviewedCommonFields", []), {"anchors": settings.get("confirmedScheduleAnchors", [])}]:
        for a in group.get("anchors", []):
            clocks = {k: a[k] for k in ("startTime", "endTime", "arrivalDeadline", "checkoutTime") if a.get(k)}
            if clocks:
                times[(a["date"], a["contentid"])] = {"date": a["date"], "contentid": a["contentid"], **clocks}
    if times:
        context["fixedTimes"] = list(times.values())
    return context


async def _fresh(snapshot, catalog, recapture, reload_catalog):
    try:
        current = await asyncio.to_thread(recapture)
    except Exception:
        raise GenerationDraftError("SOURCE_NO_LONGER_ACCESSIBLE") from None
    if current.fingerprint != snapshot.fingerprint:
        raise GenerationDraftError("STALE_GENERATION_INPUT")
    try:
        latest_catalog = await asyncio.to_thread(reload_catalog)
    except Exception:
        raise GenerationDraftError("CATALOG_UNAVAILABLE") from None
    if latest_catalog.digest != catalog.digest:
        raise GenerationDraftError("STALE_PLACE_CATALOG")


def _ai_error_code(exc: Exception) -> str:
    try:
        from chungbuk_trip_ai import (
            GeminiError, GeminiInvalidResponseError, GeminiTimeoutError, GeminiUnavailableError,
        )
    except Exception:
        return "AI_DRAFT_FAILED"
    for cls, code in ((GeminiInvalidResponseError, "AI_INVALID_DRAFT"), (GeminiTimeoutError, "AI_TIMEOUT"),
                      (GeminiUnavailableError, "AI_UNAVAILABLE"), (GeminiError, "AI_DRAFT_INPUT_INVALID")):
        if isinstance(exc, cls):
            return code
    return "AI_DRAFT_FAILED"


async def generate_candidate_draft(snapshot, normalization_result, catalog, review, recapture: Callable,
                                   reload_catalog: Callable, *, policy=None, drafter=None,
                                   confirmed_evidence=None, common_settings=None,
                                   max_visits_per_day: int = 8, timeout_seconds: float = 60) -> CandidateDraft:
    """Select internally, check freshness before/after provider, and return a proposal.

    review and callbacks are trusted internal integration boundaries, not HTTP
    authorization or evidence of human review. Each callback must close its own
    short DB transaction. Final route validation/save must check freshness again.
    """
    if (type(max_visits_per_day) is not int or not 1 <= max_visits_per_day <= 8
            or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
        raise GenerationDraftError("INVALID_DRAFT_LIMITS")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    dates = _trip_dates(snapshot)
    # Rebuild from bound source/review data; never trust an external readiness flag.
    selection = (build_confirmed_candidate_selection(snapshot, catalog, confirmed_evidence, common_settings, policy)
                 if confirmed_evidence is not None
                 else build_candidate_selection(snapshot, normalization_result, catalog, review, policy))
    if not selection.ready_for_draft:
        raise GenerationDraftError("CANDIDATE_REVIEW_REQUIRED")
    candidates, fixed_visits = selection.draft_candidates(), selection.fixed_visits()
    schedule_context = _schedule_context(snapshot, common_settings, dates, catalog)
    async def propose():
        await _fresh(snapshot, catalog, recapture, reload_catalog)
        # A local provider/stub may mutate its arguments. Keep the validator's
        # original authority separate from the provider-owned request objects.
        request_dates, request_candidates, request_visits, request_context = copy.deepcopy((dates, candidates, fixed_visits, schedule_context))
        selected_drafter = drafter or _default_drafter
        args = (request_dates, request_candidates, request_visits, max_visits_per_day)
        try:
            signature = inspect.signature(selected_drafter)
            try:
                signature.bind(*args, request_context)
                context_mode = "positional"
            except TypeError:
                try:
                    signature.bind(*args, schedule_context=request_context)
                    context_mode = "keyword"
                except TypeError:
                    context_mode = None
        except (TypeError, ValueError):
            context_mode = "positional" if drafter is None else None
        if context_mode == "positional":
            result = await asyncio.to_thread(selected_drafter, *args, request_context)
        elif context_mode == "keyword":
            result = await asyncio.to_thread(selected_drafter, *args, schedule_context=request_context)
        else:
            result = await asyncio.to_thread(selected_drafter, *args)
        validated = _validate(dates, candidates, fixed_visits, max_visits_per_day, result, schedule_context)
        await _fresh(snapshot, catalog, recapture, reload_catalog)
        return validated

    try:
        validated = await asyncio.wait_for(propose(), timeout=max(0, deadline - loop.time()))
    except asyncio.TimeoutError:
        raise GenerationDraftError("AI_TIMEOUT") from None
    except GenerationDraftError:
        raise
    except Exception as exc:
        raise GenerationDraftError(_ai_error_code(exc)) from None
    # The model never supplies displayed facts: always join the captured catalog.
    days = []
    for day in validated["days"]:
        places = []
        for position, contentid in enumerate(day["contentids"]):
            place = catalog.presentation_place(contentid)
            if place is None:
                raise GenerationDraftError("STALE_PLACE_CATALOG")
            places.append({**place, "visitPosition": position})
        days.append({"date": day["date"], "places": places})
    selection_data = selection.as_dict()
    candidate_ranks = {item["contentid"]: index for index, item in enumerate(selection_data.get("eligible", []))}
    output = {
        "sourceFingerprint": snapshot.fingerprint, "catalogDigest": catalog.digest,
        "normalizationDigest": selection_data["normalizationDigest"],
        "policyDigest": selection_data["policyDigest"],
        "selectionDigest": hashlib.sha256(_canonical(selection_data).encode()).hexdigest(),
        "days": days, "requiresReview": True,
        # Detached eligible pool for bounded internal optimization only. Never
        # include member scores/source text or forward this into PlanDocument.
        "optimizationCandidates": [{**copy.deepcopy(item), **({"selectionRank": candidate_ranks[item["contentid"]]}
                                    if item["contentid"] in candidate_ranks else {})} for item in candidates],
        "mealCandidates": [{"contentid": c["contentid"], "allowedDates": c["allowedDates"]}
                           for c in candidates if c["planningRole"] == "meal_stop"],
        "validation": {"routeVerified": False, "timeVerified": False, "budgetVerified": False,
                       "walkingVerified": False, "openingHoursVerified": False},
        "pendingValidation": selection_data["pendingValidation"],
    }
    result = CandidateDraft(_canonical(output))
    if loop.time() > deadline:
        raise GenerationDraftError("AI_TIMEOUT")
    return result
