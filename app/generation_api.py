"""HTTP surface for private generation review and durable generation jobs."""
from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .generation_service import GenerationService, ServiceError


def register_generation_routes(app: FastAPI, engine, settings, *, find_session: Callable,
                               cookie_name: Callable, origin_check: Callable, body_json: Callable,
                               ApiError, set_session_cookie: Callable, normalizer=None,
                               drafter=None, router=None) -> None:
    """Register generation APIs; callers retain ownership of common middleware/auth."""
    service = GenerationService(app, engine, settings, find_session, cookie_name, ApiError)
    app.state.generation_service = service
    app.state.generation_normalizer = normalizer if normalizer is not None else getattr(app.state, "generation_normalizer", None)
    app.state.generation_drafter = drafter if drafter is not None else getattr(app.state, "generation_drafter", None)
    selected_router = router if router is not None else getattr(app.state, "generation_router", None)
    if selected_router is None:
        from .routing import KakaoRouter
        selected_router = KakaoRouter(getattr(settings, "kakao_rest_api_key", None))
    app.state.generation_router = selected_router
    app.state.generation_job_timeout = getattr(app.state, "generation_job_timeout", 180)
    app.state.generation_normalize_timeout = getattr(app.state, "generation_normalize_timeout", 60)
    app.state.generation_run_inline = getattr(app.state, "generation_run_inline", False)

    def success(payload, request: Request, status=200):
        response = JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})
        raw = request.cookies.get(cookie_name())
        if raw:
            set_session_cookie(response, request, raw)
        return response

    def parse_trip(value: str) -> str:
        try:
            parsed = str(uuid.UUID(value))
            if parsed != value.lower():
                raise ValueError
            return parsed
        except (ValueError, AttributeError):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"tripId": "UUID 형식이어야 합니다."})

    def key(request: Request) -> str:
        try:
            raw = request.headers.get("idempotency-key", "")
            value = str(uuid.UUID(raw))
            if value != raw.lower():
                raise ValueError
            return value
        except (ValueError, AttributeError):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"Idempotency-Key": "UUID 형식이어야 합니다."})

    def translate(exc: ServiceError):
        raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", exc.fields or {}, exc.retry)

    @app.get("/api/v1/trips/{trip_id}/generation")
    def get_generation(trip_id: str, request: Request):
        try:
            return success(service.get_generation(request, parse_trip(trip_id)), request)
        except ServiceError as exc:
            translate(exc)

    @app.get("/api/v1/trips/{trip_id}/generation/preferences/me")
    def get_my_generation_preference(trip_id: str, request: Request):
        try:
            return success(service.get_my_preference(request, parse_trip(trip_id)), request)
        except ServiceError as exc:
            translate(exc)

    @app.post("/api/v1/trips/{trip_id}/generation/preferences/me")
    async def normalize_my_preference(trip_id: str, request: Request):
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "expectedPreferenceVersion"})
        if (not isinstance(body, dict) or set(body) != {"expectedConditionsVersion", "expectedPreferenceVersion"}
                or any(isinstance(body[k], bool) or not isinstance(body[k], int) or body[k] < 0 for k in body)):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        idem = key(request)
        try:
            result = await service.normalize_self(request, parse_trip(trip_id), body, idem)
            return success(result, request)
        except ServiceError as exc:
            translate(exc)

    @app.put("/api/v1/trips/{trip_id}/generation/preferences/me/review")
    async def review_my_preference(trip_id: str, request: Request):
        origin_check(request)
        body = await body_json(request, {"normalizationId", "expectedReviewVersion", "acceptedTermIds", "reviewedFields"})
        try:
            return success(await run_in_threadpool(service.save_review, request, parse_trip(trip_id), body), request)
        except ServiceError as exc:
            translate(exc)

    @app.put("/api/v1/trips/{trip_id}/generation/settings")
    async def put_generation_settings(trip_id: str, request: Request):
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "expectedSettingsVersion", "settings"})
        try:
            return success(await run_in_threadpool(service.put_settings, request, parse_trip(trip_id), body), request)
        except ServiceError as exc:
            translate(exc)

    @app.get("/api/v1/trips/{trip_id}/generation/places")
    def search_generation_places(trip_id: str, request: Request, q: str = "", limit: str = "20"):
        try:
            if not isinstance(limit, str) or not limit.isascii() or not limit.isdecimal():
                raise ServiceError("VALIDATION_ERROR", 400)
            parsed_limit = int(limit)
            return success(service.search_places(request, parse_trip(trip_id), q, parsed_limit), request)
        except ServiceError as exc:
            translate(exc)

    @app.post("/api/v1/trips/{trip_id}/generation/jobs")
    async def create_generation_job(trip_id: str, request: Request, background: BackgroundTasks):
        origin_check(request)
        body = await body_json(request, {"expectedInputToken", "expectedSettingsVersion"})
        idem = key(request)
        try:
            job = await run_in_threadpool(service.create_job, request, parse_trip(trip_id), body, idem)
        except ServiceError as exc:
            translate(exc)
        if app.state.generation_run_inline:
            await service.run_job(job["id"], request)
            job = await run_in_threadpool(service.get_job, request, trip_id, job["id"])
        else:
            background.add_task(service.run_job, job["id"], request)
        return success(job, request, 202)

    @app.get("/api/v1/trips/{trip_id}/generation/jobs/{job_id}")
    def get_generation_job(trip_id: str, job_id: str, request: Request):
        try:
            return success(service.get_job(request, parse_trip(trip_id), parse_trip(job_id)), request)
        except ServiceError as exc:
            translate(exc)

    @app.get("/api/v1/trips/{trip_id}/plans/{plan_id}")
    def get_generated_plan(trip_id: str, plan_id: str, request: Request):
        try:
            return success(service.get_plan(request, parse_trip(trip_id), parse_trip(plan_id)), request)
        except ServiceError as exc:
            translate(exc)

    @app.post("/api/v1/trips/{trip_id}/plans/{plan_id}/save")
    async def save_generated_plan(trip_id: str, plan_id: str, request: Request):
        origin_check(request)
        body = await body_json(request, {"versionId", "expectedLockVersion", "acknowledgeUnverified"})
        idem = key(request)
        try:
            return success(await run_in_threadpool(service.save_plan, request, parse_trip(trip_id), parse_trip(plan_id), body, idem), request)
        except ServiceError as exc:
            translate(exc)
