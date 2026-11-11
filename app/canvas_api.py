"""Shared canvas API; route registration leaves the existing generation API intact."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, text
from starlette.concurrency import run_in_threadpool

from . import canvas_import, canvas_models, canvas_recommendation, preparation
from .candidate_selection import _tokens_region
from .canvas_models import CanvasValidationError
from .canvas_store import CanvasStore, CanvasStoreError


class CanvasError(Exception):
    def __init__(self, code: str, status: int = 400, retry: int | None = None):
        self.code, self.status, self.retry = code, status, retry
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return _now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _access(conn, request, trip_id, find_session, cookie_name):
    # Use the existing adapter on the real MySQL backend. SQLite's grammar
    # lacks SELECT FOR UPDATE, so its in-process contract tests exercise the
    # same session/membership/ACTIVE/OPEN checks without the lock suffix.
    if conn.dialect.name != "sqlite":
        try:
            return preparation._access(conn, request, trip_id, find_session, cookie_name)
        except preparation.PrepFailure as exc:
            raise CanvasError(exc.code, exc.status, exc.retry) from None
    session = find_session(conn, request)
    if not session:
        raise CanvasError("SESSION_REQUIRED", 401)
    trip = conn.execute(text("SELECT id,name,invite_code,status FROM trips WHERE id=:t"), {"t": trip_id}).mappings().first()
    if not trip:
        raise CanvasError("TRIP_NOT_ACCESSIBLE", 404)
    mapping = conn.execute(text("SELECT member_id FROM device_session_memberships WHERE session_id=:s AND trip_id=:t"), {"s": session["id"], "t": trip_id}).mappings().first()
    if not mapping:
        raise CanvasError("TRIP_NOT_ACCESSIBLE", 404)
    member = conn.execute(text("SELECT id,nickname,role,status FROM trip_members WHERE trip_id=:t AND id=:m"), {"t": trip_id, "m": mapping["member_id"]}).mappings().first()
    if not member or member["status"] != "ACTIVE":
        raise CanvasError("MEMBERSHIP_INACTIVE", 403)
    if trip["status"] == "CLOSED":
        raise CanvasError("TRIP_CLOSED", 409)
    return session, trip, member


def _translate_store(exc: CanvasStoreError):
    table = {"VERSION_CONFLICT": ("VERSION_CONFLICT", 409), "IDEMPOTENCY_KEY_REUSED": ("IDEMPOTENCY_KEY_REUSED", 409),
             "REQUEST_IN_PROGRESS": ("REQUEST_IN_PROGRESS", 409)}
    code, status = table.get(exc.code, ("CANVAS_UNAVAILABLE", 503))
    return CanvasError(code, status, 1 if code == "REQUEST_IN_PROGRESS" else None)


def _abandon_request(engine, store, session_id, path, key, lease_token):
    cleanup = engine.connect()
    try:
        with cleanup.begin():
            store.abandon_request(cleanup, session_id=session_id, method="POST", path=path,
                                  key=key, lease_token=lease_token)
    except Exception:
        cleanup.rollback()
    finally:
        cleanup.close()


def _day_document(conn, trip_id):
    conditions, _ = preparation._conditions(conn, trip_id)
    start, end = conditions.get("startDate"), conditions.get("endDate")
    days = []
    if start and end:
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if 0 <= (last - first).days < 31:
                days = [{"id": str(uuid.uuid5(uuid.UUID(trip_id), f"canvas-day:{(first + timedelta(days=i)).isoformat()}")),
                         "date": (first + timedelta(days=i)).isoformat()} for i in range((last - first).days + 1)]
        except (ValueError, TypeError):
            pass
    if not days:
        days = [{"id": str(uuid.uuid5(uuid.UUID(trip_id), "canvas-day:empty")), "date": None}]
    return {"schemaVersion": 3, "days": days, "nodes": [], "edges": []}


def _safe_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http", "https"} and parsed.hostname and "\r" not in value and "\n" not in value else None
    except ValueError:
        return None


def _clean(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bounded(value, maximum):
    value = _clean(value)
    return value[:maximum] if value else None


def _place_details(conn, contentids: list[int]) -> list[dict[str, Any]]:
    if not contentids:
        return []
    ids = list(dict.fromkeys(contentids))
    def rows(query):
        return conn.execute(text(query).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings()

    places = {row["contentid"]: row for row in rows(
        "SELECT contentid,title,contentTypeName,planningRole,adminCity,addr1,mapx,mapy,geoValidated,tel,homepageUrl,overviewText FROM places WHERE contentid IN :ids")}
    details = {}
    for row in rows("SELECT contentid,openingHoursText,closedDaysText,feeText,bookingText,parkingText,menuText,estimatedStayMinutes FROM place_details WHERE contentid IN :ids"):
        details.setdefault(row["contentid"], []).append(row)
    output = []
    for contentid in ids:
        place = places.get(contentid)
        if not place:
            raise CanvasError("PLACE_NOT_FOUND", 404)
        detail_rows = details.get(contentid, [])
        detail = detail_rows[0] if len(detail_rows) == 1 else {}
        stay = detail.get("estimatedStayMinutes")
        coordinate = None
        try:
            if _truthy_geo(place["geoValidated"]):
                longitude, latitude = float(place["mapx"]), float(place["mapy"])
                if -180 <= longitude <= 180 and -90 <= latitude <= 90:
                    coordinate = {"latitude": latitude, "longitude": longitude, "crs": "WGS84"}
        except (TypeError, ValueError, OverflowError):
            pass
        output.append({"contentid": contentid, "title": _clean(place["title"]) or "", "address": _clean(place["addr1"]),
                       "adminCity": _clean(place["adminCity"]), "category": _clean(place["contentTypeName"]),
                       "planningRole": _clean(place["planningRole"]), "coordinate": coordinate,
                       "estimatedStayMinutes": stay if type(stay) is int and 1 <= stay <= 1440 else None,
                       "overviewText": _clean(place["overviewText"]), "menuText": _clean(detail.get("menuText")),
                       "openingHoursText": _clean(detail.get("openingHoursText")), "closedDaysText": _clean(detail.get("closedDaysText")),
                       "feeText": _clean(detail.get("feeText")), "bookingText": _clean(detail.get("bookingText")),
                       "parkingText": _clean(detail.get("parkingText")), "tel": _clean(place["tel"]),
                       "homepageUrl": _safe_url(place["homepageUrl"])})
    return output


def _truthy_geo(value):
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1) or (isinstance(value, str) and value.strip().lower() == "yes")


def _catalog_pool(conn, exclude: set[int], limit=40, *, region=None, prompt=""):
    params = {}
    region_sql = ""
    if region:
        cities, recognized = _tokens_region(region)
        if not recognized:
            return []
        filters = []
        for index, city in enumerate(sorted(cities)):
            key = f"city_{index}"
            params[key] = f"%{city[:-1]}%"
            filters.append(f"(adminCity LIKE :{key} OR addr1 LIKE :{key})")
        region_sql = " AND (" + " OR ".join(filters) + ")"
    rows = conn.execute(text("SELECT contentid,title,contentTypeName,planningRole,adminCity,addr1,overviewText,geoValidated,mapx,mapy FROM places WHERE contentid IS NOT NULL" + region_sql + " ORDER BY contentid"), params).mappings().all()
    detail_rows = conn.execute(text("SELECT contentid,menuText FROM place_details")).mappings().all()
    menus: dict[int, list[str]] = {}
    for row in detail_rows:
        if isinstance(row["contentid"], int):
            menus.setdefault(row["contentid"], []).append(row["menuText"] or "")
    tokens = [t.lower() for t in re.findall(r"[\w가-힣]{2,}", prompt)[:40]]
    pool = []
    for row in rows:
        cid = row["contentid"]
        if isinstance(cid, bool) or not isinstance(cid, int) or cid in exclude or not _clean(row["title"]):
            continue
        candidate = {"contentid": cid, "title": _bounded(row["title"], 80), "category": _bounded(row["contentTypeName"], 30),
                     "planningRole": _bounded(row["planningRole"], 30), "adminCity": _bounded(row["adminCity"], 40), "address": _bounded(row["addr1"], 80),
                     "overviewText": _bounded(row["overviewText"], 80),
                     "menuText": _bounded(menus[cid][0], 40) if len(menus.get(cid, [])) == 1 else None}
        try:
            if _truthy_geo(row["geoValidated"]):
                lon, lat = float(row["mapx"]), float(row["mapy"])
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    candidate["coordinate"] = {"latitude": lat, "longitude": lon, "crs": "WGS84"}
        except (ValueError, TypeError, OverflowError):
            pass
        searchable = " ".join(str(v).lower() for k, v in candidate.items() if k != "coordinate" and v is not None)
        score = sum(1 for token in tokens if token in searchable)
        pool.append((score, candidate))
    pool.sort(key=lambda item: (-item[0], item[1]["contentid"]))
    return [candidate for _, candidate in pool[:limit]]


def register_canvas_routes(app: FastAPI, engine, *, find_session: Callable, cookie_name: Callable,
                           origin_check: Callable, body_json: Callable, ApiError, set_session_cookie: Callable,
                           recommender=None):
    store = CanvasStore(engine)
    app.state.canvas_service = store
    app.state.canvas_recommender = recommender if recommender is not None else getattr(app.state, "canvas_recommender", None)

    from .canvas_routes import register_canvas_route
    register_canvas_route(app, engine, find_session=find_session, cookie_name=cookie_name,
                          set_session_cookie=set_session_cookie, ApiError=ApiError)

    def success(payload, request):
        response = JSONResponse(payload, headers={"Cache-Control": "no-store"})
        raw = request.cookies.get(cookie_name())
        if raw:
            set_session_cookie(response, request, raw)
        return response

    def parse_trip(value):
        try:
            return canvas_models.canonical_uuid(value)
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"tripId": "UUID 형식이어야 합니다."}) from None

    def request_key(request):
        try:
            return canvas_models.canonical_uuid(request.headers.get("idempotency-key", ""))
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"Idempotency-Key": "UUID 형식이어야 합니다."}) from None

    def public_state(conn, trip_id, saved=None):
        saved = saved if saved is not None else store.load_document(conn, trip_id)
        revision = saved["revision"] if saved else 0
        document = saved["document"] if saved else _day_document(conn, trip_id)
        if saved and saved["sourcePlan"]:
            document = canvas_import.current_route_document(document, saved["sourcePlan"]["versionId"])
        refs = [n["contentid"] for n in document["nodes"] if n["kind"] == "place"]
        places = _place_details(conn, refs)
        return {"tripId": trip_id, "revision": revision, "document": document, "places": places,
                "sourcePlan": saved["sourcePlan"] if saved else None, "updatedAt": _iso(saved["updatedAt"] if saved else None)}

    @app.get("/api/v1/trips/{trip_id}/canvas")
    def get_canvas(trip_id: str, request: Request):
        trip_id = parse_trip(trip_id)
        conn = engine.connect()
        try:
            _access(conn, request, trip_id, find_session, cookie_name)
            result = public_state(conn, trip_id)
            conn.commit()
            return success(result, request)
        except CanvasError as exc:
            conn.rollback()
            raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", retry=exc.retry) from None
        except Exception:
            conn.rollback()
            raise ApiError(503, "CANVAS_UNAVAILABLE", "캔버스를 불러오지 못했습니다.") from None
        finally:
            conn.close()

    @app.put("/api/v1/trips/{trip_id}/canvas")
    async def put_canvas(trip_id: str, request: Request):
        trip_id = parse_trip(trip_id)
        origin_check(request)
        if len(await request.body()) > canvas_models.MAX_DOCUMENT_BYTES + 1024:
            raise ApiError(400, "VALIDATION_ERROR", "캔버스 문서를 확인해 주세요.")
        body = await body_json(request, {"expectedRevision", "document"})
        if set(body) != {"expectedRevision", "document"} or isinstance(body.get("expectedRevision"), bool) or not isinstance(body.get("expectedRevision"), int) or not 0 <= body["expectedRevision"] <= 4_294_967_295:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        try:
            document = canvas_models.validate_document(body["document"])
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "캔버스 문서를 확인해 주세요.") from None
        key = request_key(request)
        fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        def persist():
            conn = engine.connect()
            try:
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    lease = str(uuid.uuid4())
                    try:
                        reservation = store.reserve_request(conn, session_id=session["id"], trip_id=trip_id, member_id=member["id"],
                                                            kind="SAVE", method="PUT", path=f"/api/v1/trips/{trip_id}/canvas",
                                                            key=key, fingerprint=fingerprint, lease_token=lease)
                    except CanvasStoreError as exc:
                        raise _translate_store(exc) from None
                    if "replay" in reservation:
                        result = reservation["replay"]
                    else:
                        existing = store.load_document(conn, trip_id)
                        if (existing["revision"] if existing else 0) != body["expectedRevision"]:
                            raise CanvasError("VERSION_CONFLICT", 409)
                        source = existing["sourcePlan"] if existing else None
                        # A tab opened on a stacked legacy document may still submit
                        # that entire draft. Accept its current edits without restoring retired routes.
                        current_doc = canvas_import.current_route_document(document, source["versionId"]) if source else document
                        try:
                            canvas_import.validate_managed_nodes(conn, trip_id, current_doc, existing["document"] if existing else None)
                        except canvas_import.CanvasImportError as exc:
                            raise CanvasError(exc.code, exc.status) from None
                        try:
                            ids = [n["contentid"] for n in current_doc["nodes"] if n["kind"] == "place"]
                            _place_details(conn, ids)
                            saved = store.save_document(conn, trip_id, body["expectedRevision"], current_doc)
                            result = public_state(conn, trip_id, saved)
                            store.complete_request(conn, session_id=session["id"], method="PUT", path=f"/api/v1/trips/{trip_id}/canvas",
                                                   key=key, lease_token=lease, result=result)
                        except CanvasStoreError as exc:
                            raise _translate_store(exc) from None
                return success(result, request)
            except CanvasError as exc:
                conn.rollback()
                raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", retry=exc.retry) from None
            except ApiError:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise ApiError(503, "CANVAS_UNAVAILABLE", "캔버스를 저장하지 못했습니다.") from None
            finally:
                conn.close()
        return await run_in_threadpool(persist)

    @app.post("/api/v1/trips/{trip_id}/plans/{plan_id}/canvas")
    async def import_plan_canvas(trip_id: str, plan_id: str, request: Request):
        trip_id = parse_trip(trip_id)
        try:
            plan_id = canvas_models.canonical_uuid(plan_id)
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "일정을 확인해 주세요.") from None
        origin_check(request)
        if len(await request.body()) > 4096:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        body = await body_json(request, {"versionId", "expectedCanvasRevision"})
        try:
            if set(body) != {"versionId", "expectedCanvasRevision"}:
                raise CanvasValidationError
            version_id = canvas_models.canonical_uuid(body["versionId"])
            revision = body["expectedCanvasRevision"]
            if type(revision) is not int or not 0 <= revision <= 4_294_967_295:
                raise CanvasValidationError
        except (CanvasValidationError, KeyError, TypeError):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.") from None
        key = request_key(request)
        path = f"/api/v1/trips/{trip_id}/plans/{plan_id}/canvas"
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        def persist_import():
            from .generation_service import ServiceError
            conn = engine.connect()
            try:
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    if member["role"] != "OWNER":
                        raise CanvasError("OWNER_REQUIRED", 403)
                    service = getattr(app.state, "generation_service", None)
                    if service is None:
                        raise CanvasError("GENERATION_UNAVAILABLE", 503)
                    # The service rechecks authority, source/catalog/settings and
                    # locks the plan version in THIS transaction, without a commit.
                    record = service.current_plan_for_canvas(conn, request, trip_id, plan_id, version_id)
                    public_plan = record.get("public", record)
                    payload = canvas_import.load_import_payload(conn, trip_id, plan_id, version_id)
                    lease = str(uuid.uuid4())
                    reservation = store.reserve_request(conn, session_id=session["id"], trip_id=trip_id, member_id=member["id"],
                                                        kind="SAVE", method="POST", path=path, key=key,
                                                        fingerprint=fingerprint, lease_token=lease)
                    if "replay" in reservation:
                        result = reservation["replay"]
                    else:
                        current = store.load_document(conn, trip_id)
                        marker = payload.get("_canvasImport") or {}
                        if marker.get("tripId") == trip_id:
                            # Compact the saved legacy stack once, using the room's
                            # current source even when an older import is revisited.
                            if current and current["sourcePlan"]:
                                visible = canvas_import.current_route_document(current["document"], current["sourcePlan"]["versionId"])
                                if visible != current["document"]:
                                    if current["revision"] != revision:
                                        raise CanvasError("VERSION_CONFLICT", 409)
                                    current = store.save_document(conn, trip_id, revision, visible, source_plan=current["sourcePlan"])
                            result = public_state(conn, trip_id, current)
                        else:
                            actual_revision = current["revision"] if current else 0
                            if actual_revision != revision:
                                raise CanvasError("VERSION_CONFLICT", 409)
                            base = current["document"] if current else _day_document(conn, trip_id)
                            document = canvas_import.append_generated_plan(base, trip_id, plan_id, version_id, public_plan["document"])
                            _place_details(conn, [node["contentid"] for node in document["nodes"] if node["kind"] == "place"])
                            saved = store.save_document(conn, trip_id, revision, document,
                                                        source_plan={"planId": plan_id, "versionId": version_id})
                            canvas_import.mark_imported(conn, trip_id, plan_id, version_id, payload, saved["revision"])
                            result = public_state(conn, trip_id, saved)
                        store.complete_request(conn, session_id=session["id"], method="POST", path=path, key=key,
                                               lease_token=lease, result=result)
                return success(result, request)
            except ServiceError as exc:
                conn.rollback()
                raise ApiError(exc.status, exc.code, "일정 상태를 다시 확인해 주세요.", retry=exc.retry) from None
            except (CanvasError, canvas_import.CanvasImportError) as exc:
                conn.rollback()
                raise ApiError(exc.status, exc.code, "일정을 캔버스로 옮기지 못했습니다.", retry=getattr(exc, "retry", None)) from None
            except CanvasStoreError as exc:
                conn.rollback()
                translated = _translate_store(exc)
                raise ApiError(translated.status, translated.code, "캔버스 상태를 다시 확인해 주세요.", retry=translated.retry) from None
            except Exception:
                conn.rollback()
                raise ApiError(503, "CANVAS_UNAVAILABLE", "일정을 캔버스로 옮기지 못했습니다.") from None
            finally:
                conn.close()
        return await run_in_threadpool(persist_import)

    @app.get("/api/v1/trips/{trip_id}/canvas/places/{contentid}")
    def get_canvas_place(trip_id: str, contentid: str, request: Request):
        trip_id = parse_trip(trip_id)
        if not re.fullmatch(r"-?[0-9]{1,10}", contentid):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        contentid = int(contentid)
        if not -(2**31) <= contentid <= 2**31 - 1:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        conn = engine.connect()
        try:
            _access(conn, request, trip_id, find_session, cookie_name)
            result = _place_details(conn, [contentid])[0]
            conn.commit()
            return success(result, request)
        except CanvasError as exc:
            conn.rollback()
            raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", retry=exc.retry) from None
        except Exception:
            conn.rollback()
            raise ApiError(503, "CANVAS_UNAVAILABLE", "장소 정보를 불러오지 못했습니다.") from None
        finally:
            conn.close()

    @app.post("/api/v1/trips/{trip_id}/canvas/recommendations")
    async def recommend_canvas_place(trip_id: str, request: Request):
        trip_id = parse_trip(trip_id)
        origin_check(request)
        if len(await request.body()) > 16 * 1024:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        body = await body_json(request, {"prompt", "dayId", "excludeContentids"})
        if set(body) != {"prompt", "dayId", "excludeContentids"}:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        prompt = body["prompt"]
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1000 or any(0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in "\n\t") for c in prompt):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        try:
            day_id = canvas_models.canonical_uuid(body["dayId"])
        except CanvasValidationError:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.") from None
        excluded = body["excludeContentids"]
        if not isinstance(excluded, list) or len(excluded) > 100 or any(isinstance(x, bool) or not isinstance(x, int) or not -(2**31) <= x <= 2**31 - 1 for x in excluded) or len(set(excluded)) != len(excluded):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        key = request_key(request)
        fingerprint = hashlib.sha256(json.dumps({"prompt": prompt.strip(), "dayId": day_id, "excludeContentids": excluded}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path = f"/api/v1/trips/{trip_id}/canvas/recommendations"
        def process():
            conn = engine.connect()
            lease_token = None
            session = None
            try:
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    current = store.load_document(conn, trip_id)
                    days = (current["document"] if current else _day_document(conn, trip_id))["days"]
                    if day_id not in {d["id"] for d in days}:
                        raise CanvasError("VALIDATION_ERROR", 400)
                    lease_token = str(uuid.uuid4())
                    try:
                        reservation = store.reserve_request(conn, session_id=session["id"], trip_id=trip_id, member_id=member["id"],
                                                            kind="RECOMMEND", method="POST", path=path, key=key,
                                                            fingerprint=fingerprint, lease_token=lease_token, lease_seconds=45)
                    except CanvasStoreError as exc:
                        raise _translate_store(exc) from None
                    if "replay" in reservation:
                        return success(reservation["replay"], request)
                    limiter = getattr(app.state, "limiter", None)
                    retry = limiter.take(f"canvas-recommendation:session:{session['id']}", 5) if limiter else None
                    if retry:
                        raise CanvasError("RATE_LIMITED", 429, retry)
                    current_conditions, _ = preparation._conditions(conn, trip_id)
                    candidates = _catalog_pool(conn, set(excluded), 40, region=current_conditions.get("region"), prompt=prompt)
                    day_date = next((d["date"] for d in days if d["id"] == day_id), None)
                    public_context = {field: current_conditions.get(field) for field in ("region", "startDate", "endDate", "expectedPeople", "transport")}
                # The provider call runs off the event loop, with no DB connection or transaction open.
                conn.close()
                if not candidates:
                    raise CanvasError("CATALOG_UNAVAILABLE", 503)
                ai_prompt = json.dumps({"prompt": prompt.strip(), "dayDate": day_date,
                                        "commonTravelConditions": public_context}, ensure_ascii=False, separators=(",", ":"))
                try:
                    result = canvas_recommendation.recommend(ai_prompt, candidates, provider=app.state.canvas_recommender)
                except canvas_recommendation.RecommendationError as exc:
                    raise CanvasError(exc.code, exc.status, exc.retry) from None
                conn = engine.connect()
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    request_row = conn.execute(text("SELECT lease_token,status,member_id FROM trip_canvas_requests WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k"), {"s": session["id"], "p": path, "k": key}).mappings().first()
                    if not request_row or request_row["lease_token"] != lease_token or request_row["status"] != "PENDING":
                        raise CanvasError("REQUEST_IN_PROGRESS", 409, 1)
                    if member["id"] != request_row["member_id"]:
                        raise CanvasError("TRIP_NOT_ACCESSIBLE", 404)
                    latest = store.load_document(conn, trip_id)
                    latest_days = (latest["document"] if latest else _day_document(conn, trip_id))["days"]
                    if day_id not in {d["id"] for d in latest_days}:
                        raise CanvasError("VERSION_CONFLICT", 409)
                    chosen_ids = [result["place"]["contentid"]] + [p["contentid"] for p in result["alternatives"]]
                    hydrated = _place_details(conn, chosen_ids)
                    by_id = {p["contentid"]: p for p in hydrated}
                    public = {"place": by_id[result["place"]["contentid"]],
                              "alternatives": [by_id[p["contentid"]] for p in result["alternatives"]], "requiresReview": True}
                    store.complete_request(conn, session_id=session["id"], method="POST", path=path, key=key,
                                           lease_token=lease_token, result=public)
                return success(public, request)
            except CanvasError as exc:
                conn.rollback()
                if lease_token and session:
                    _abandon_request(engine, store, session["id"], path, key, lease_token)
                raise ApiError(exc.status, exc.code, "요청을 처리하지 못했습니다.", retry=exc.retry) from None
            except ApiError:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                if lease_token and session:
                    _abandon_request(engine, store, session["id"], path, key, lease_token)
                raise ApiError(503, "CANVAS_UNAVAILABLE", "추천을 처리하지 못했습니다.") from None
            finally:
                conn.close()
        return await run_in_threadpool(process)
