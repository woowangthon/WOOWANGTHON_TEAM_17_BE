"""Persistent trip preparation endpoints, kept separate from trip entry APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool


CONDITION_KEYS = ("region", "startDate", "endDate", "expectedPeople", "budget", "transport", "departure", "returnRoute", "lodging", "fixedSchedule", "departureTime", "departureContentid")
PERSONAL_KEYS = ("originalText", "likes", "avoid", "avoidImportance", "food", "foodImportance", "walking")
SCHEDULE_ITEM_KEYS = {"id", "kind", "scope", "placeQuery", "contentid", "date", "time", "endDate", "endTime"}
SUGGESTION_KEYS = ("likes", "avoid", "avoidImportance", "food", "foodImportance", "walking", "scheduleItems")
TRANSPORTS = {"car", "transit", "charter", "walk"}
IMPORTANCE = {"required", "preferred"}
WALKING = {"20", "40", "unlimited"}
CONTROL = set(range(0x20)) | set(range(0x7f, 0xa0)) | {0x2028, 0x2029}
INTERPRET_PATH = "/api/v1/trips/{trip_id}/preferences/me/interpret"


class PrepFailure(Exception):
    def __init__(self, status: int, code: str, message: str = "요청을 처리하지 못했습니다.", fields: dict[str, str] | None = None, retry: int | None = None, conditions: int | None = None, preference: int | None = None):
        self.status, self.code, self.message = status, code, message
        self.fields, self.retry = fields or {}, retry
        self.conditions, self.preference = conditions, preference


def _fail(status: int, code: str, fields: dict[str, str] | None = None, retry: int | None = None, conditions: int | None = None, preference: int | None = None):
    raise PrepFailure(status, code, fields=fields, retry=retry, conditions=conditions, preference=preference)


def _obj(value: Any, keys: tuple[str, ...], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(400, "VALIDATION_ERROR", {field: "필수 필드 구성을 확인해 주세요."})
    return value


def _version(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(400, "VALIDATION_ERROR", {field: "0 이상의 정수를 입력해 주세요."})
    return value


def _text(value: Any, field: str, maximum: int, multiline: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(400, "VALIDATION_ERROR", {field: "문자열 또는 null을 입력해 주세요."})
    value = value.replace("\r\n", "\n") if multiline else value
    def invalid(ch: str) -> bool:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            return True
        if multiline:
            return (code < 0x20 and ch != "\n") or 0x7F <= code <= 0x9F
        return code in CONTROL

    if any(invalid(ch) for ch in value):
        _fail(400, "VALIDATION_ERROR", {field: "입력 길이 또는 문자를 확인해 주세요."})
    value = value.strip()
    if len(value) > maximum:
        _fail(400, "VALIDATION_ERROR", {field: "입력 길이 또는 문자를 확인해 주세요."})
    return value or None


def _date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail(400, "VALIDATION_ERROR", {field: "YYYY-MM-DD 날짜를 입력해 주세요."})
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(400, "VALIDATION_ERROR", {field: "실제 달력 날짜를 입력해 주세요."})
    return value


def validate_conditions(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - set(CONDITION_KEYS) or set(raw) - {"departureTime", "departureContentid"} != set(CONDITION_KEYS) - {"departureTime", "departureContentid"}:
        _fail(400, "VALIDATION_ERROR", {"conditions": "필수 필드 구성을 확인해 주세요."})
    data = {**_initial_conditions(), **raw}
    out: dict[str, Any] = {}
    out["region"] = _text(data["region"], "conditions.region", 100)
    out["startDate"] = _date(data["startDate"], "conditions.startDate")
    out["endDate"] = _date(data["endDate"], "conditions.endDate")
    if (out["startDate"] is None) != (out["endDate"] is None) or (out["startDate"] and out["endDate"] < out["startDate"]):
        _fail(400, "VALIDATION_ERROR", {"conditions.startDate": "여행 날짜 범위를 확인해 주세요.", "conditions.endDate": "여행 날짜 범위를 확인해 주세요."})
    for key, low, high in (("expectedPeople", 1, 50), ("budget", 1, 1_000_000_000)):
        value = data[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
            _fail(400, "VALIDATION_ERROR", {f"conditions.{key}": "허용 범위의 정수를 입력해 주세요."})
        out[key] = value
    transport = data["transport"]
    if transport is not None and (not isinstance(transport, str) or transport not in TRANSPORTS):
        _fail(400, "VALIDATION_ERROR", {"conditions.transport": "이동 수단을 확인해 주세요."})
    out["transport"] = transport
    for key, maximum in (("departure", 100), ("returnRoute", 100), ("lodging", 200), ("fixedSchedule", 500)):
        out[key] = _text(data[key], f"conditions.{key}", maximum)
    departure_time = data["departureTime"]
    if departure_time is None:
        out["departureTime"] = None if "departureTime" in raw else "09:00"
    elif isinstance(departure_time, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", departure_time):
        out["departureTime"] = departure_time
    else:
        _fail(400, "VALIDATION_ERROR", {"conditions.departureTime": "HH:MM 시각을 입력해 주세요."})
    contentid = data["departureContentid"]
    if contentid is not None and (isinstance(contentid, bool) or not isinstance(contentid, int) or not -(2**31) <= contentid <= 2**31 - 1):
        _fail(400, "VALIDATION_ERROR", {"conditions.departureContentid": "정수 또는 null을 입력해 주세요."})
    out["departureContentid"] = contentid
    return out


def validate_draft(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - (set(PERSONAL_KEYS) | {"scheduleItems"}) or set(raw) - {"scheduleItems"} != set(PERSONAL_KEYS):
        _fail(400, "VALIDATION_ERROR", {"draft": "필수 필드 구성을 확인해 주세요."})
    data = {**raw, "scheduleItems": raw.get("scheduleItems", [])}
    out = {"originalText": _text(data["originalText"], "draft.originalText", 5000, True)}
    for key in ("likes", "avoid", "food"):
        out[key] = _text(data[key], f"draft.{key}", 500, True)
    for key in ("avoidImportance", "foodImportance"):
        if not isinstance(data[key], str) or data[key] not in IMPORTANCE:
            _fail(400, "VALIDATION_ERROR", {f"draft.{key}": "required 또는 preferred를 입력해 주세요."})
        out[key] = data[key]
    walking = data["walking"]
    if walking is not None and (not isinstance(walking, str) or walking not in WALKING):
        _fail(400, "VALIDATION_ERROR", {"draft.walking": "허용된 도보 한계를 입력해 주세요."})
    out["walking"] = walking
    items = data["scheduleItems"]
    if not isinstance(items, list) or len(items) > 20:
        _fail(400, "VALIDATION_ERROR", {"draft.scheduleItems": "일정 항목은 20개 이하로 입력해 주세요."})
    clean_items = []
    seen = set()
    for index, item in enumerate(items):
        field = f"draft.scheduleItems.{index}"
        if not isinstance(item, dict) or set(item) != SCHEDULE_ITEM_KEYS:
            _fail(400, "VALIDATION_ERROR", {field: "일정 항목 구성을 확인해 주세요."})
        try:
            item_id = str(uuid.UUID(item["id"]))
            if item_id != item["id"].lower(): raise ValueError
        except (ValueError, AttributeError, TypeError):
            _fail(400, "VALIDATION_ERROR", {field + ".id": "UUID 형식이어야 합니다."})
        if item_id in seen: _fail(400, "VALIDATION_ERROR", {field + ".id": "중복 UUID입니다."})
        seen.add(item_id)
        kind, scope = item["kind"], item["scope"]
        if not isinstance(kind, str) or kind not in {"lodging", "appointment"} or (scope is not None and (not isinstance(scope, str) or scope not in {"personal", "group"})):
            _fail(400, "VALIDATION_ERROR", {field: "종류와 범위를 확인해 주세요."})
        if not isinstance(item["placeQuery"], str):
            _fail(400, "VALIDATION_ERROR", {field + ".placeQuery": "문자열을 입력해 주세요."})
        query = "" if item["placeQuery"] == "" else _text(item["placeQuery"], field + ".placeQuery", 200) or ""
        cid = item["contentid"]
        if cid is not None and (isinstance(cid, bool) or not isinstance(cid, int) or not -(2**31) <= cid <= 2**31 - 1):
            _fail(400, "VALIDATION_ERROR", {field + ".contentid": "정수 또는 null을 입력해 주세요."})
        values = {}
        for name in ("date", "endDate"):
            values[name] = _date(item[name], field + "." + name)
        if values["date"] and values["endDate"] and values["endDate"] < values["date"]:
            _fail(400, "VALIDATION_ERROR", {field + ".endDate": "시작일 이후 날짜를 입력해 주세요."})
        for name in ("time", "endTime"):
            rawtime = item[name]
            if rawtime is not None and (not isinstance(rawtime, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", rawtime)):
                _fail(400, "VALIDATION_ERROR", {field + "." + name: "HH:MM 시각을 입력해 주세요."})
            values[name] = rawtime
        # A draft keeps independently known checkout/end values. Missing start
        # values are clarified on the same form rather than invalidating AI output.
        clean_items.append({"id": item_id, "kind": kind, "scope": scope, "placeQuery": query,
                            "contentid": cid, **values})
    out["scheduleItems"] = clean_items
    return out


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _initial_conditions() -> dict[str, Any]:
    return {**{key: None for key in CONDITION_KEYS}, "departureTime": "09:00"}


def _initial_draft() -> dict[str, Any]:
    return {"originalText": None, "likes": None, "avoid": None, "avoidImportance": "required", "food": None, "foodImportance": "preferred", "walking": None, "scheduleItems": []}


def _response(row: Any, conditions_version: int) -> dict[str, Any]:
    draft = _json(row["draft_json"]) if row else _initial_draft()
    draft.setdefault("scheduleItems", [])
    version = int(row["preference_version"]) if row else 0
    confirmed = bool(row and row["confirmed_at"] is not None and row["confirmed_conditions_version"] == conditions_version and row["confirmed_preference_version"] == version)
    return {
        "conditionsVersion": conditions_version,
        "preferenceVersion": version,
        "draft": draft,
        "confirmation": {
            "status": "confirmed" if confirmed else "writing",
            "confirmedConditionsVersion": int(row["confirmed_conditions_version"]) if confirmed else None,
            "confirmedPreferenceVersion": int(row["confirmed_preference_version"]) if confirmed else None,
            "confirmedAt": _iso(row["confirmed_at"]) if confirmed else None,
        },
    }


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z" if value else None


def _trip_id(value: str, ApiError) -> str:
    try:
        parsed = str(uuid.UUID(value))
        if parsed != value.lower():
            raise ValueError
        return parsed
    except (ValueError, AttributeError):
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"tripId": "UUID 형식이어야 합니다."})


def _access(conn, request: Request, trip_id: str, find_session: Callable, cookie_name: Callable, *, owner: bool = False):
    session = find_session(conn, request)
    if not session:
        _fail(401, "SESSION_REQUIRED")
    trip = conn.execute(text("SELECT id,name,invite_code,status FROM trips WHERE id=:t FOR UPDATE"), {"t": trip_id}).mappings().first()
    if not trip:
        _fail(404, "TRIP_NOT_ACCESSIBLE")
    mapping = conn.execute(text("SELECT member_id FROM device_session_memberships WHERE session_id=:s AND trip_id=:t FOR UPDATE"), {"s": session["id"], "t": trip_id}).mappings().first()
    if not mapping:
        _fail(404, "TRIP_NOT_ACCESSIBLE")
    member = conn.execute(text("SELECT id,nickname,role,status,created_at FROM trip_members WHERE trip_id=:t AND id=:m FOR UPDATE"), {"t": trip_id, "m": mapping["member_id"]}).mappings().first()
    if not member or member["status"] != "ACTIVE":
        _fail(403, "MEMBERSHIP_INACTIVE")
    if trip["status"] == "CLOSED":
        _fail(409, "TRIP_CLOSED")
    if owner and member["role"] != "OWNER":
        _fail(403, "OWNER_REQUIRED")
    return session, trip, member


def _conditions(conn, trip_id: str):
    row = conn.execute(text("SELECT conditions_json,conditions_version FROM trip_preparation_documents WHERE trip_id=:t"), {"t": trip_id}).mappings().first()
    if not row: return _initial_conditions(), 0
    conditions = _json(row["conditions_json"])
    conditions.setdefault("departureTime", "09:00")
    conditions.setdefault("departureContentid", None)
    return conditions, int(row["conditions_version"])


def _personal(conn, trip_id: str, member_id: str):
    return conn.execute(text("SELECT draft_json,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at FROM member_preparation_documents WHERE trip_id=:t AND member_id=:m FOR UPDATE"), {"t": trip_id, "m": member_id}).mappings().first()


def _schedule_clarifications(conn, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve only exact catalog matches and return private correction DTOs."""
    resolved, clarifications = [], []
    for original in items:
        item = dict(original)
        if item["contentid"] is not None:
            rows = conn.execute(text("SELECT contentid,title,addr1 FROM places WHERE contentid=:id"), {"id": item["contentid"]}).mappings().all()
            candidates = rows
        else:
            rows = conn.execute(text("SELECT contentid,title,addr1 FROM places WHERE title=:q OR addr1=:q ORDER BY contentid LIMIT 3"), {"q": item["placeQuery"]}).mappings().all()
            candidates = [r for r in rows if item["placeQuery"] in {r["title"], r["addr1"]}]
        item["contentid"] = candidates[0]["contentid"] if len(candidates) == 1 else None
        resolved.append(item)
        fields = []
        if item["contentid"] is None: fields.append("placeQuery")
        if item["scope"] is None: fields.append("scope")
        if item["kind"] == "lodging" and item["date"] is None: fields.append("date")
        if item["kind"] == "appointment":
            if item["date"] is None: fields.append("date")
            if item["time"] is None: fields.append("time")
            if item.get("endDate") and item.get("date") and item["endDate"] != item["date"]:
                fields.append("endDate")
        if fields:
            clarifications.append({"itemId": item["id"], "fields": fields,
                                  "code": "SCHEDULE_ITEM_CLARIFICATION_REQUIRED",
                                  "candidates": [{"contentid": r["contentid"], "title": r["title"], "address": r["addr1"]} for r in candidates]})
    return resolved, clarifications


def _member_safe_suggestions(suggestions: dict[str, Any], role: str) -> dict[str, Any]:
    """Do not let model output promote a member's private request to group scope."""
    if role == "OWNER":
        return suggestions
    return {**suggestions, "scheduleItems": [
        ({**item, "scope": None} if item.get("scope") == "group" else item)
        for item in suggestions.get("scheduleItems", [])]}


def _api_error(exc: PrepFailure, ApiError):
    error = ApiError(exc.status, exc.code, exc.message, exc.fields, exc.retry)
    error.current_conditions_version = exc.conditions
    error.current_preference_version = exc.preference
    raise error


def _validated(call: Callable, ApiError):
    try:
        return call()
    except PrepFailure as exc:
        _api_error(exc, ApiError)


def _members_readiness(conn, trip_id: str, conditions: dict[str, Any], conditions_version: int):
    members = conn.execute(text("SELECT id,nickname,role,created_at FROM trip_members WHERE trip_id=:t AND status='ACTIVE' ORDER BY CASE WHEN role='OWNER' THEN 0 ELSE 1 END,created_at,id FOR UPDATE"), {"t": trip_id}).mappings().all()
    docs = conn.execute(text("SELECT member_id,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at FROM member_preparation_documents WHERE trip_id=:t FOR UPDATE"), {"t": trip_id}).mappings().all()
    by_member = {r["member_id"]: r for r in docs}
    response_members = []
    owner_pending = None
    for member in members:
        doc = by_member.get(member["id"])
        confirmed = bool(doc and doc["confirmed_at"] is not None and doc["confirmed_conditions_version"] == conditions_version and doc["confirmed_preference_version"] == doc["preference_version"])
        response_members.append({"id": member["id"], "nickname": member["nickname"], "role": member["role"], "preparationStatus": "confirmed" if confirmed else "writing"})
        if member["role"] == "OWNER" and not confirmed:
            owner_pending = member["id"]
    incomplete = [key for key in ("region", "startDate", "endDate", "expectedPeople", "budget", "transport") if conditions.get(key) is None]
    reasons: list[dict[str, Any]] = []
    if incomplete:
        reasons.append({"code": "CONDITIONS_INCOMPLETE", "fields": incomplete})
    if owner_pending or not any(member["role"] == "OWNER" for member in members):
        reasons.append({"code": "PREFERENCES_UNCONFIRMED", **({"memberId": owner_pending} if owner_pending else {})})
    return response_members, {"conditionsReady": not reasons, "reasons": reasons}


def register_preparation_routes(app: FastAPI, engine, settings, *, find_session: Callable, cookie_name: Callable,
                                origin_check: Callable, body_json: Callable, ApiError, set_session_cookie: Callable, interpreter=None):
    """Register the six endpoints. Existing entry/session handlers remain untouched."""
    app.state.preparation_interpreter = interpreter
    app.state.preparation_ai_timeout = 60

    def execute(request: Request, action: Callable):
        conn = engine.connect()
        try:
            try:
                result = action(conn)
                conn.commit()
                return result
            except PrepFailure as exc:
                conn.rollback()
                _api_error(exc, ApiError)
            except ApiError:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def success(payload: Any, request: Request):
        response = JSONResponse(payload, headers={"Cache-Control": "no-store"})
        raw = request.cookies.get(cookie_name())
        if raw:
            set_session_cookie(response, request, raw)
        return response

    @app.get("/api/v1/trips/{trip_id}/preparation")
    def get_preparation(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        def action(conn):
            session, trip, member = _access(conn, request, trip_id, find_session, cookie_name)
            conditions, version = _conditions(conn, trip_id)
            rows = conn.execute(text("SELECT COUNT(*) FROM trip_members WHERE trip_id=:t AND status='ACTIVE'"), {"t": trip_id}).scalar()
            active_count = int(rows or 0)
            members, readiness = _members_readiness(conn, trip_id, conditions, version)
            return {"trip": {"id": trip["id"], "name": trip["name"], "status": trip["status"], "inviteCode": trip["invite_code"]},
                    "currentMember": {"id": member["id"], "nickname": member["nickname"], "role": member["role"]},
                    "canEditConditions": member["role"] == "OWNER", "conditionsVersion": version,
                    "conditions": conditions, "activeMemberCount": active_count, "members": members, "readiness": readiness}
        return success(execute(request, action), request)

    @app.put("/api/v1/trips/{trip_id}/conditions")
    async def put_conditions(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "conditions"})
        if set(body) != {"expectedConditionsVersion", "conditions"}:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        expected = _validated(lambda: _version(body["expectedConditionsVersion"], "expectedConditionsVersion"), ApiError)
        conditions = _validated(lambda: validate_conditions(body["conditions"]), ApiError)
        def action(conn):
            _, _, _member = _access(conn, request, trip_id, find_session, cookie_name, owner=True)
            if conditions["departureContentid"] is not None:
                place = conn.execute(text("SELECT contentid FROM places WHERE contentid=:id"), {"id": conditions["departureContentid"]}).first()
                if not place: _fail(400, "VALIDATION_ERROR", {"conditions.departureContentid": "장소를 확인해 주세요."})
            current, version = _conditions(conn, trip_id)
            if expected != version:
                _fail(409, "VERSION_CONFLICT", conditions=version)
            try:
                current_normalized = validate_conditions(current)
            except PrepFailure:
                current_normalized = None
            if current_normalized == conditions:
                return {"conditionsVersion": version, "conditions": conditions, "requiresReconfirmation": False}
            next_version = version + 1
            encoded = json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
            if version == 0:
                conn.execute(text("INSERT INTO trip_preparation_documents(trip_id,conditions_json,conditions_version) VALUES(:t,:j,:v)"), {"t": trip_id, "j": encoded, "v": next_version})
            else:
                conn.execute(text("UPDATE trip_preparation_documents SET conditions_json=:j,conditions_version=:v WHERE trip_id=:t"), {"j": encoded, "v": next_version, "t": trip_id})
            conn.execute(text("UPDATE member_preparation_documents d JOIN trip_members m ON m.trip_id=d.trip_id AND m.id=d.member_id SET d.confirmed_conditions_version=NULL,d.confirmed_preference_version=NULL,d.confirmed_at=NULL WHERE d.trip_id=:t AND m.status='ACTIVE'"), {"t": trip_id})
            return {"conditionsVersion": next_version, "conditions": conditions, "requiresReconfirmation": True}
        try:
            return success(await run_in_threadpool(execute, request, action), request)
        except PrepFailure as exc:
            _api_error(exc, ApiError)

    @app.get("/api/v1/trips/{trip_id}/preferences/me")
    def get_my_preferences(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        def action(conn):
            _, _, member = _access(conn, request, trip_id, find_session, cookie_name)
            _, conditions_version = _conditions(conn, trip_id)
            row = _personal(conn, trip_id, member["id"])
            dto = _response(row, conditions_version)
            resolved, clarifications = _schedule_clarifications(conn, dto["draft"].get("scheduleItems", []))
            dto["draft"]["scheduleItems"] = resolved
            dto["clarifications"] = clarifications
            return dto
        return success(execute(request, action), request)

    @app.put("/api/v1/trips/{trip_id}/preferences/me")
    async def put_my_preferences(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "expectedPreferenceVersion", "draft"})
        if set(body) != {"expectedConditionsVersion", "expectedPreferenceVersion", "draft"}:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        ec = _validated(lambda: _version(body["expectedConditionsVersion"], "expectedConditionsVersion"), ApiError)
        ep = _validated(lambda: _version(body["expectedPreferenceVersion"], "expectedPreferenceVersion"), ApiError)
        draft = _validated(lambda: validate_draft(body["draft"]), ApiError)
        def action(conn):
            _, _, member = _access(conn, request, trip_id, find_session, cookie_name)
            group_scope_errors = {f"draft.scheduleItems.{index}.scope": "공통 일정은 방장만 지정할 수 있습니다."
                                  for index, item in enumerate(draft["scheduleItems"])
                                  if member["role"] != "OWNER" and item["scope"] == "group"}
            if group_scope_errors:
                _fail(400, "VALIDATION_ERROR", group_scope_errors)
            resolved, _ = _schedule_clarifications(conn, draft["scheduleItems"])
            draft["scheduleItems"] = resolved
            _, cv = _conditions(conn, trip_id)
            row = _personal(conn, trip_id, member["id"])
            pv = int(row["preference_version"]) if row else 0
            if ec != cv or ep != pv:
                _fail(409, "VERSION_CONFLICT", conditions=cv, preference=pv)
            current_draft = _initial_draft()
            if row:
                try:
                    raw_current = _json(row["draft_json"])
                    current_draft = validate_draft({**raw_current, "scheduleItems": raw_current.get("scheduleItems", [])})
                except Exception:
                    current_draft = None
            if current_draft == draft:
                return _response(row, cv)
            new_pv = pv + 1
            encoded = json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
            if row:
                conn.execute(text("UPDATE member_preparation_documents SET draft_json=:j,preference_version=:v,confirmed_conditions_version=NULL,confirmed_preference_version=NULL,confirmed_at=NULL WHERE trip_id=:t AND member_id=:m"), {"j": encoded, "v": new_pv, "t": trip_id, "m": member["id"]})
            else:
                conn.execute(text("INSERT INTO member_preparation_documents(trip_id,member_id,draft_json,preference_version) VALUES(:t,:m,:j,:v)"), {"t": trip_id, "m": member["id"], "j": encoded, "v": new_pv})
            fresh = _personal(conn, trip_id, member["id"])
            return _response(fresh, cv)
        try:
            return success(await run_in_threadpool(execute, request, action), request)
        except PrepFailure as exc:
            _api_error(exc, ApiError)

    @app.post("/api/v1/trips/{trip_id}/preferences/me/confirm")
    async def confirm_my_preferences(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "expectedPreferenceVersion", "reviewed"})
        if set(body) != {"expectedConditionsVersion", "expectedPreferenceVersion", "reviewed"}:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        ec = _validated(lambda: _version(body["expectedConditionsVersion"], "expectedConditionsVersion"), ApiError)
        ep = _validated(lambda: _version(body["expectedPreferenceVersion"], "expectedPreferenceVersion"), ApiError)
        if body["reviewed"] is not True:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        def action(conn):
            _, _, member = _access(conn, request, trip_id, find_session, cookie_name)
            _, cv = _conditions(conn, trip_id)
            row = _personal(conn, trip_id, member["id"])
            pv = int(row["preference_version"]) if row else 0
            if ec != cv or ep != pv:
                _fail(409, "VERSION_CONFLICT", conditions=cv, preference=pv)
            draft = _json(row["draft_json"]) if row else _initial_draft()
            draft.setdefault("scheduleItems", [])
            group_scope_errors = {f"scheduleItems.{index}.scope": "공통 일정은 방장만 지정할 수 있습니다."
                                  for index, item in enumerate(draft["scheduleItems"])
                                  if member["role"] != "OWNER" and item.get("scope") == "group"}
            if group_scope_errors:
                _fail(400, "VALIDATION_ERROR", group_scope_errors)
            missing = [field for field in ("originalText", "likes") if not draft[field]]
            if draft["walking"] not in WALKING:
                missing.append("walking")
            resolved, clarifications = _schedule_clarifications(conn, draft.get("scheduleItems", []))
            if clarifications:
                missing.extend(f"scheduleItems.{item['itemId']}" for item in clarifications)
            if missing:
                _fail(400, "PREFERENCES_INCOMPLETE", {field: "확정 전에 입력해 주세요." for field in missing})
            if row and row["confirmed_conditions_version"] == cv and row["confirmed_preference_version"] == pv and row["confirmed_at"] is not None:
                return _response(row, cv)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if row:
                conn.execute(text("UPDATE member_preparation_documents SET confirmed_conditions_version=:cv,confirmed_preference_version=:pv,confirmed_at=:n WHERE trip_id=:t AND member_id=:m"), {"cv": cv, "pv": pv, "n": now, "t": trip_id, "m": member["id"]})
            else:
                conn.execute(text("INSERT INTO member_preparation_documents(trip_id,member_id,draft_json,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at) VALUES(:t,:m,:j,1,:cv,1,:n)"), {"t": trip_id, "m": member["id"], "j": json.dumps(_initial_draft(), separators=(",", ":")), "cv": cv, "n": now})
            return _response(_personal(conn, trip_id, member["id"]), cv)
        try:
            return success(await run_in_threadpool(execute, request, action), request)
        except PrepFailure as exc:
            _api_error(exc, ApiError)

    def rate_interpret(session_id: str, member_id: str):
        if not settings.rate_limit_enabled:
            return
        limiter = app.state.limiter
        for key in (f"preparation:session:{session_id}", f"preparation:member:{member_id}"):
            retry = limiter.take(key, 5)
            if retry:
                raise ApiError(429, "RATE_LIMITED", "요청이 너무 많습니다.", retry=retry)

    def reserve_interpret(request: Request, trip_id: str, key: str, expected_cv: int, expected_pv: int):
        conn = engine.connect()
        lock_name = None
        locked = False
        try:
            # Authenticate before deriving/claiming the per-key serialization lock.
            session = find_session(conn, request)
            if not session:
                raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
            sid = session["id"]
            path = INTERPRET_PATH.format(trip_id=trip_id)
            lock_name = "prep:" + hashlib.sha256(f"{sid}|POST|{path}|{key}".encode()).hexdigest()[:59]
            locked = conn.execute(text("SELECT GET_LOCK(:n,0)"), {"n": lock_name}).scalar() == 1
            conn.commit()
            if not locked:
                raise ApiError(409, "REQUEST_IN_PROGRESS", "요청을 처리 중입니다.", retry=1)
            try:
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    rate_interpret(session["id"], member["id"])
                    conditions, cv = _conditions(conn, trip_id)
                    row = _personal(conn, trip_id, member["id"])
                    pv = int(row["preference_version"]) if row else 0
                    fingerprint = hashlib.sha256(f"{trip_id}|{expected_cv}|{expected_pv}".encode()).hexdigest()
                    idem = conn.execute(text("SELECT * FROM preparation_interpret_requests WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k FOR UPDATE"), {"s": session["id"], "p": path, "k": key}).mappings().first()
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    token = str(uuid.uuid4())
                    if idem and idem["expires_at"] > now:
                        if idem["trip_id"] != trip_id or idem["member_id"] != member["id"]:
                            _fail(409, "IDEMPOTENCY_KEY_REUSED")
                        if idem["fingerprint"] != fingerprint:
                            _fail(409, "IDEMPOTENCY_KEY_REUSED")
                        if idem["status"] == "COMPLETE":
                            if int(idem["conditions_version"]) != cv or int(idem["preference_version"]) != pv:
                                _fail(409, "STALE_AI_RESULT")
                            return {"replay": _json(idem["result_json"])}
                        if cv != expected_cv or pv != expected_pv:
                            _fail(409, "STALE_AI_RESULT", conditions=cv, preference=pv)
                        if idem["lease_expires_at"] > now:
                            _fail(409, "REQUEST_IN_PROGRESS", retry=1)
                        conn.execute(text("UPDATE preparation_interpret_requests SET lease_token=:token,lease_expires_at=:lease WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k"), {"token": token, "lease": now + timedelta(seconds=75), "s": session["id"], "p": path, "k": key})
                    else:
                        if cv != expected_cv or pv != expected_pv:
                            _fail(409, "VERSION_CONFLICT", conditions=cv, preference=pv)
                        if idem:
                            conn.execute(text("DELETE FROM preparation_interpret_requests WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k"), {"s": session["id"], "p": path, "k": key})
                        conn.execute(text("INSERT INTO preparation_interpret_requests(session_id,method,path,idempotency_key,trip_id,member_id,conditions_version,preference_version,fingerprint,status,lease_token,lease_expires_at,result_json,created_at,expires_at) VALUES(:s,'POST',:p,:k,:t,:m,:cv,:pv,:f,'RUNNING',:token,:lease,NULL,:n,:e)"), {"s": session["id"], "p": path, "k": key, "t": trip_id, "m": member["id"], "cv": cv, "pv": pv, "f": fingerprint, "token": token, "lease": now + timedelta(seconds=75), "n": now, "e": now + timedelta(hours=24)})
                    draft = _json(row["draft_json"]) if row else _initial_draft()
                    if not draft["originalText"]:
                        _fail(400, "ORIGINAL_TEXT_REQUIRED", {"originalText": "먼저 원문을 저장해 주세요."})
                    return {"token": token, "session_id": session["id"], "member_id": member["id"], "member_role": member["role"], "conditions_version": cv, "preference_version": pv, "original_text": draft["originalText"], "conditions": conditions, "path": path}
            except PrepFailure as exc:
                _api_error(exc, ApiError)
        finally:
            if locked and lock_name:
                try:
                    conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": lock_name})
                    conn.commit()
                except Exception:
                    conn.invalidate()
            conn.close()

    def abandon_interpret(reservation):
        conn = engine.connect()
        try:
            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            conn.execute(text("UPDATE preparation_interpret_requests SET lease_token=:new,lease_expires_at=:now WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND lease_token=:old AND status='RUNNING'"), {"new": str(uuid.uuid4()), "now": expired_at, "s": reservation["session_id"], "p": reservation["path"], "k": reservation["key"], "old": reservation["token"]})
            conn.commit()
        finally:
            conn.close()

    def finish_interpret(request: Request, trip_id: str, key: str, reservation: dict, result: dict):
        conn = engine.connect()
        try:
            try:
                with conn.begin():
                    session, _, member = _access(conn, request, trip_id, find_session, cookie_name)
                    conditions, cv = _conditions(conn, trip_id)
                    row = _personal(conn, trip_id, member["id"])
                    pv = int(row["preference_version"]) if row else 0
                    if session["id"] != reservation["session_id"] or member["id"] != reservation["member_id"]:
                        _fail(403, "MEMBERSHIP_INACTIVE")
                    if cv != reservation["conditions_version"] or pv != reservation["preference_version"]:
                        expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
                        conn.execute(text("UPDATE preparation_interpret_requests SET lease_token=:new,lease_expires_at=:now WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND lease_token=:old AND status='RUNNING'"), {"new": str(uuid.uuid4()), "now": expired_at, "s": reservation["session_id"], "p": reservation["path"], "k": key, "old": reservation["token"]})
                        _fail(409, "STALE_AI_RESULT", conditions=cv, preference=pv)
                    updated = conn.execute(text("UPDATE preparation_interpret_requests SET status='COMPLETE',result_json=:result,expires_at=:expiry WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND lease_token=:token AND status='RUNNING'"), {"result": json.dumps(result, ensure_ascii=False, separators=(",", ":")), "expiry": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24), "s": reservation["session_id"], "p": reservation["path"], "k": key, "token": reservation["token"]})
                    if updated.rowcount != 1:
                        _fail(409, "REQUEST_IN_PROGRESS", retry=1)
                return result
            except PrepFailure as exc:
                _api_error(exc, ApiError)
        finally:
            conn.close()

    @app.post("/api/v1/trips/{trip_id}/preferences/me/interpret")
    async def interpret_my_preferences(trip_id: str, request: Request):
        trip_id = _trip_id(trip_id, ApiError)
        origin_check(request)
        body = await body_json(request, {"expectedConditionsVersion", "expectedPreferenceVersion"})
        if set(body) != {"expectedConditionsVersion", "expectedPreferenceVersion"}:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
        cv = _validated(lambda: _version(body["expectedConditionsVersion"], "expectedConditionsVersion"), ApiError)
        pv = _validated(lambda: _version(body["expectedPreferenceVersion"], "expectedPreferenceVersion"), ApiError)
        raw_key = request.headers.get("idempotency-key", "")
        try:
            key = str(uuid.UUID(raw_key))
            if key != raw_key.lower():
                raise ValueError
        except (ValueError, AttributeError):
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"Idempotency-Key": "UUID 형식이어야 합니다."})
        reservation = await run_in_threadpool(reserve_interpret, request, trip_id, key, cv, pv)
        if "replay" in reservation:
            return success(reservation["replay"], request)
        reservation["key"] = key
        interpreter_fn = app.state.preparation_interpreter or _default_interpreter
        try:
            result = await asyncio.wait_for(run_in_threadpool(interpreter_fn, reservation["original_text"], reservation["conditions"]), timeout=app.state.preparation_ai_timeout)
        except asyncio.TimeoutError:
            await run_in_threadpool(abandon_interpret, reservation)
            raise ApiError(504, "AI_TIMEOUT", "AI 해석 시간이 초과되었습니다.")
        except Exception as exc:
            await run_in_threadpool(abandon_interpret, reservation)
            name = type(exc).__name__
            if name == "GeminiInvalidResponseError":
                raise ApiError(502, "AI_INVALID_RESPONSE", "AI 응답을 처리하지 못했습니다.")
            if name == "GeminiTimeoutError" or isinstance(exc, TimeoutError):
                raise ApiError(504, "AI_TIMEOUT", "AI 해석 시간이 초과되었습니다.")
            raise ApiError(503, "AI_UNAVAILABLE", "AI 서비스를 사용할 수 없습니다.")
        try:
            validated = _validate_interpret_result(result, ApiError)
        except ApiError:
            await run_in_threadpool(abandon_interpret, reservation)
            raise
        validated["suggestions"] = _member_safe_suggestions(validated["suggestions"], reservation["member_role"])
        payload = {"sourceConditionsVersion": cv, "sourcePreferenceVersion": pv, **validated}
        clarification_conn = engine.connect()
        try:
            _, payload["clarifications"] = _schedule_clarifications(clarification_conn, payload["suggestions"]["scheduleItems"])
            clarification_conn.commit()
        finally:
            clarification_conn.close()
        return success(await run_in_threadpool(finish_interpret, request, trip_id, key, reservation, payload), request)


def _validate_interpret_result(result: Any, ApiError) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {"suggestions", "needsWalkingAnswer"} or not isinstance(result["needsWalkingAnswer"], bool):
        raise ApiError(502, "AI_INVALID_RESPONSE", "AI 응답을 처리하지 못했습니다.")
    try:
        if not isinstance(result["suggestions"], dict):
            raise PrepFailure(502, "AI_INVALID_RESPONSE")
        raw = dict(result["suggestions"])
        old_suggestion_keys = set(SUGGESTION_KEYS) - {"scheduleItems"}
        if set(raw) == old_suggestion_keys:
            raw["scheduleItems"] = []
        elif set(raw) != set(SUGGESTION_KEYS):
            raise PrepFailure(502, "AI_INVALID_RESPONSE")
        if not isinstance(raw["scheduleItems"], list) or len(raw["scheduleItems"]) > 20:
            raise PrepFailure(502, "AI_INVALID_RESPONSE")
        items = []
        expected_model_keys = SCHEDULE_ITEM_KEYS
        legacy_model_keys = SCHEDULE_ITEM_KEYS - {"id", "contentid"}
        for item in raw["scheduleItems"]:
            if not isinstance(item, dict) or set(item) not in (expected_model_keys, legacy_model_keys):
                raise PrepFailure(502, "AI_INVALID_RESPONSE")
            items.append({**item, "id": str(uuid.uuid4()), "contentid": None})
        raw["scheduleItems"] = items
        suggestions = validate_draft({"originalText": None, **raw})
    except PrepFailure:
        raise ApiError(502, "AI_INVALID_RESPONSE", "AI 응답을 처리하지 못했습니다.") from None
    if result["needsWalkingAnswer"] != (suggestions["walking"] is None):
        raise ApiError(502, "AI_INVALID_RESPONSE", "AI 응답을 처리하지 못했습니다.")
    return {"suggestions": {key: suggestions[key] for key in SUGGESTION_KEYS}, "needsWalkingAnswer": result["needsWalkingAnswer"]}


def _default_interpreter(original_text: str, conditions: dict[str, Any]):
    try:
        from chungbuk_trip_ai import interpret_personal_draft
        return interpret_personal_draft(original_text=original_text, conditions=conditions)
    except Exception as exc:
        # Preserve typed provider distinctions without exposing provider details.
        if type(exc).__name__ in {"GeminiError", "GeminiUnavailableError", "GeminiTimeoutError", "GeminiInvalidResponseError"}:
            raise
        raise RuntimeError("AI provider unavailable") from None
