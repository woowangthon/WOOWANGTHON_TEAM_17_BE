"""Read-only shared-canvas route lookup with bounded caching and route-provider fencing."""
from __future__ import annotations

import copy
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import plan_validation, preparation


MAX_CACHE_ENTRIES = 512
VERIFIED_TTL_SECONDS = 300
UNKNOWN_TTL_SECONDS = 30
MAX_PROVIDER_CALLS = 2
PROVIDER_WAIT_SECONDS = 12
ACTOR_REQUESTS_PER_MINUTE = 120
UINT32_MAX = 4_294_967_295
INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647
ROUTE_MODES = {"car", "walk", "transit", "charter"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coordinate_key(coordinate: dict[str, Any] | None):
    if not plan_validation._coord(coordinate):
        return None
    return (float(coordinate["latitude"]), float(coordinate["longitude"]))


def _unknown(mode: str | None, reason: str) -> dict[str, Any]:
    return {"mode": mode, "status": "unknown", "durationSeconds": None,
            "distanceMeters": None, "checkedAt": _now_iso(), "reasonCodes": [reason], "geometry": []}


class RouteCache:
    """TTL LRU cache with per-key singleflight and a two-call provider ceiling."""

    def __init__(self, *, capacity: int = MAX_CACHE_ENTRIES, provider_limit: int = MAX_PROVIDER_CALLS,
                 verified_ttl: float = VERIFIED_TTL_SECONDS, unknown_ttl: float = UNKNOWN_TTL_SECONDS):
        self.capacity = capacity
        self.provider_limit = provider_limit
        self.verified_ttl = verified_ttl
        self.unknown_ttl = unknown_ttl
        self._condition = threading.Condition()
        self._items: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
        self._inflight: set[tuple] = set()
        self._providers = 0

    def resolve(self, key: tuple, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        deadline = time.monotonic() + PROVIDER_WAIT_SECONDS
        while True:
            with self._condition:
                now = time.monotonic()
                cached = self._items.get(key)
                if cached:
                    expiry, value = cached
                    if expiry > now:
                        self._items.move_to_end(key)
                        return copy.deepcopy(value)
                    del self._items[key]
                if key in self._inflight or self._providers >= self.provider_limit:
                    remaining = deadline - now
                    if remaining <= 0:
                        return _unknown(key[1], "ROUTE_UNAVAILABLE")
                    self._condition.wait(remaining)
                    continue
                self._inflight.add(key)
                self._providers += 1
            try:
                result = compute()
            except Exception:
                result = _unknown(key[1], "ROUTE_UNAVAILABLE")
            ttl = self.verified_ttl if result.get("status") == "verified" else self.unknown_ttl
            with self._condition:
                self._items[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                self._items.move_to_end(key)
                while len(self._items) > self.capacity:
                    self._items.popitem(last=False)
                self._providers -= 1
                self._inflight.discard(key)
                self._condition.notify_all()
            return copy.deepcopy(result)


def register_canvas_route(app: FastAPI, engine, *, find_session, cookie_name, set_session_cookie,
                          ApiError, route_cache: RouteCache | None = None):
    """Add GET /canvas/route without changing the existing canvas route handlers."""
    from .canvas_api import _access, _place_details, CanvasError

    cache = route_cache or RouteCache()
    app.state.canvas_route_cache = cache

    def success(payload, request):
        response = JSONResponse(payload, headers={"Cache-Control": "no-store"})
        raw = request.cookies.get(cookie_name())
        if raw:
            set_session_cookie(response, request, raw)
        return response

    def translate(exc):
        raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", retry=exc.retry) from None

    def parse_int(raw: str | None, *, signed: bool, maximum: int, field: str) -> int:
        pattern = r"-?[0-9]{1,10}" if signed else r"[0-9]{1,10}"
        if raw is None or not re.fullmatch(pattern, raw):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "정수 값을 확인해 주세요."})
        value = int(raw)
        low = INT32_MIN if signed else 0
        if not low <= value <= maximum:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "허용 범위의 정수를 입력해 주세요."})
        return value

    @app.get("/api/v1/trips/{trip_id}/canvas/route")
    def get_canvas_route(trip_id: str, request: Request):
        from .canvas_models import CanvasValidationError, canonical_uuid
        try:
            trip_id = canonical_uuid(trip_id)
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"tripId": "UUID 형식이어야 합니다."}) from None
        expected_version = parse_int(request.query_params.get("expectedConditionsVersion"), signed=False,
                                     maximum=UINT32_MAX, field="expectedConditionsVersion")
        source_id = parse_int(request.query_params.get("sourceContentid"), signed=True,
                              maximum=INT32_MAX, field="sourceContentid")
        target_id = parse_int(request.query_params.get("targetContentid"), signed=True,
                              maximum=INT32_MAX, field="targetContentid")
        requested_mode = request.query_params.get("mode")
        allowed_query_keys = {"sourceContentid", "targetContentid", "expectedConditionsVersion"}
        if "mode" in request.query_params:
            allowed_query_keys.add("mode")
            if requested_mode not in ROUTE_MODES:
                raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"mode": "지원 모드를 확인해 주세요."})
        if set(request.query_params.keys()) != allowed_query_keys:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        include_geometry = request.headers.get("X-Canvas-Route-Geometry") == "1"

        snapshot_conn = engine.connect()
        try:
            try:
                with snapshot_conn.begin():
                    session, _, member = _access(snapshot_conn, request, trip_id, find_session, cookie_name)
                    conditions, current_version = preparation._conditions(snapshot_conn, trip_id)
                    if current_version != expected_version:
                        raise CanvasError("STALE_ROUTE_INPUT", 409)
                    hydrated = _place_details(snapshot_conn, [source_id, target_id])
                    by_contentid = {place["contentid"]: place for place in hydrated}
                    source, target = by_contentid[source_id], by_contentid[target_id]
                    shared_mode = conditions.get("transport")
                    shared_mode = shared_mode if shared_mode in ROUTE_MODES else None
                    mode = requested_mode if requested_mode is not None else shared_mode
                    actor_session_id = session["id"]
                    actor_member_id = member["id"]
                    source_coord = source["coordinate"]
                    target_coord = target["coordinate"]
            except CanvasError as exc:
                translate(exc)
        except ApiError:
            snapshot_conn.rollback()
            raise
        except Exception:
            snapshot_conn.rollback()
            raise ApiError(503, "CANVAS_UNAVAILABLE", "캔버스 장소 정보를 불러오지 못했습니다.") from None
        finally:
            snapshot_conn.close()

        limiter = getattr(app.state, "limiter", None)
        retry = limiter.take(f"canvas-route:session:{actor_session_id}", ACTOR_REQUESTS_PER_MINUTE) if limiter else None
        if retry:
            raise ApiError(429, "RATE_LIMITED", "요청이 너무 많습니다.", retry=retry)

        router = getattr(app.state, "generation_router", None)
        source_key, target_key = _coordinate_key(source_coord), _coordinate_key(target_coord)
        route_mode = mode

        def compute():
            if source_key is None or target_key is None:
                return _unknown(route_mode, "ROUTE_COORDINATE_INVALID")
            if source_id == target_id:
                return _unknown(route_mode, "ROUTE_NOT_FOUND")
            if route_mode is None:
                return _unknown(None, "ROUTE_MODE_UNSUPPORTED")
            if route_mode == "charter":
                return _unknown("charter", "ROUTE_MODE_UNSUPPORTED")
            if router is None:
                return _unknown(route_mode, "ROUTE_NOT_CONFIGURED")
            origin = {"latitude": source_key[0], "longitude": source_key[1], "crs": "WGS84"}
            destination = {"latitude": target_key[0], "longitude": target_key[1], "crs": "WGS84"}
            facts = plan_validation._route_facts(router, origin, destination, route_mode)
            return {"mode": route_mode, "status": facts["status"],
                    "durationSeconds": facts["durationSeconds"], "distanceMeters": facts["distanceMeters"],
                    "checkedAt": facts["checkedAt"], "reasonCodes": facts["reasonCodes"],
                    "geometry": copy.deepcopy(facts.get("geometry", [])) if facts["status"] == "verified" else []}

        cache_key = (id(router), route_mode, source_key, target_key)
        result = cache.resolve(cache_key, compute)

        final_conn = engine.connect()
        try:
            try:
                with final_conn.begin():
                    final_session, _, final_member = _access(final_conn, request, trip_id, find_session, cookie_name)
                    conditions, final_version = preparation._conditions(final_conn, trip_id)
                    final_shared_mode = conditions.get("transport")
                    final_shared_mode = final_shared_mode if final_shared_mode in ROUTE_MODES else None
                    if (final_version != expected_version or final_version != current_version
                            or final_shared_mode != shared_mode or final_session["id"] != actor_session_id
                            or final_member["id"] != actor_member_id):
                        raise CanvasError("STALE_ROUTE_INPUT", 409)
                    final_places = _place_details(final_conn, [source_id, target_id])
                    final_by_contentid = {place["contentid"]: place for place in final_places}
                    if (_coordinate_key(final_by_contentid[source_id]["coordinate"]) != source_key
                            or _coordinate_key(final_by_contentid[target_id]["coordinate"]) != target_key):
                        raise CanvasError("STALE_ROUTE_INPUT", 409)
            except CanvasError as exc:
                translate(exc)
        except ApiError:
            final_conn.rollback()
            raise
        except Exception:
            final_conn.rollback()
            raise ApiError(503, "CANVAS_UNAVAILABLE", "캔버스 장소 정보를 불러오지 못했습니다.") from None
        finally:
            final_conn.close()
        payload = {"sourceContentid": source_id, "targetContentid": target_id,
                   "conditionsVersion": final_version, "mode": route_mode,
                   "status": result["status"], "durationSeconds": result["durationSeconds"],
                   "distanceMeters": result["distanceMeters"], "checkedAt": result["checkedAt"],
                   "reasonCodes": list(result["reasonCodes"])}
        if include_geometry:
            payload["geometry"] = copy.deepcopy(result["geometry"])
        return success(payload, request)
