from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .preparation import register_preparation_routes

COOKIE_HOST = "__Host-osami_session"
COOKIE_DEV = "osami_session"
COOKIE_AGE = 30 * 24 * 60 * 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CONTROL = set(range(0x20)) | set(range(0x7f, 0xa0)) | {0x2028, 0x2029}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict[str, str] | None = None, retry: int | None = None):
        self.status, self.code, self.message = status, code, message
        self.fields, self.retry = fields or {}, retry
        self.current_conditions_version: int | None = None
        self.current_preference_version: int | None = None


class Limiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits: dict[str, list[float]] = {}

    def take(self, key: str, maximum: int) -> int | None:
        now = time.monotonic()
        with self.lock:
            times = [t for t in self.hits.get(key, []) if now - t < 60]
            if len(times) >= maximum:
                self.hits[key] = times
                return max(1, int(60 - (now - times[0])))
            times.append(now)
            self.hits[key] = times
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if dt else None


def valid_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "문자열을 입력해 주세요."})
    if any(ord(ch) in CONTROL or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "입력 길이 또는 문자를 확인해 주세요."})
    result = value.strip()
    if not 1 <= len(result) <= maximum:
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "입력 길이 또는 문자를 확인해 주세요."})
    return result


def parse_uuid(value: str, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value.lower():
            raise ValueError
        return str(parsed)
    except Exception:
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {field: "UUID 형식이어야 합니다."})


async def body_json(request: Request, allowed: set[str]) -> dict[str, Any]:
    ct = request.headers.get("content-type", "")
    if ct.split(";", 1)[0].strip().lower() != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "JSON 요청이 필요합니다.")
    try:
        value = await request.json()
    except Exception:
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
    if not isinstance(value, dict) or set(value) - allowed:
        raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {k: "허용되지 않는 필드입니다." for k in (set(value) - allowed if isinstance(value, dict) else [])})
    return value


def response_error(error: ApiError, request_id: str) -> JSONResponse:
    detail = {"code": error.code, "message": error.message, "fieldErrors": error.fields, "requestId": request_id}
    if error.current_conditions_version is not None:
        detail["currentConditionsVersion"] = error.current_conditions_version
    if error.current_preference_version is not None:
        detail["currentPreferenceVersion"] = error.current_preference_version
    payload = {"error": detail}
    headers = {"Cache-Control": "no-store"}
    if error.retry:
        headers["Retry-After"] = str(error.retry)
    return JSONResponse(payload, status_code=error.status, headers=headers)


def entry_dto(trip: dict[str, Any], member: dict[str, Any], already: bool) -> dict[str, Any]:
    return {"trip": {"id": trip["id"], "name": trip["name"], "status": trip["status"], "inviteCode": trip["invite_code"]},
            "member": {"id": member["id"], "nickname": member["nickname"], "role": member["role"], "status": member["status"]},
            "alreadyJoined": already}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def cursor_encode(key: str, session_id: str, last: datetime, trip_id: str) -> str:
    payload = json.dumps({"s": session_id, "t": iso(last), "i": trip_id}, separators=(",", ":")).encode()
    encoded = b64(payload)
    sig = b64(hmac.new(key.encode(), encoded.encode(), hashlib.sha256).digest())
    return encoded + "." + sig


def cursor_decode(key: str, token: str, session_id: str) -> tuple[datetime, str]:
    try:
        payload, signature = token.split(".", 1)
        expected = b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["s"] != session_id:
            raise ValueError
        return datetime.fromisoformat(data["t"].replace("Z", "+00:00")).replace(tzinfo=None), str(uuid.UUID(data["i"]))
    except Exception:
        raise ApiError(400, "INVALID_CURSOR", "커서를 확인해 주세요.", {"cursor": "유효하지 않은 커서입니다."})


def create_app(settings: Settings, preparation_interpreter=None) -> FastAPI:
    engine: Engine = create_engine(
        settings.database_url or "mysql+pymysql://invalid:invalid@127.0.0.1/invalid",
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"init_command": "SET time_zone = '+00:00'"},
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    app = FastAPI(lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.limiter = Limiter()

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return response_error(exc, getattr(request.state, "request_id", str(uuid.uuid4())))

    @app.middleware("http")
    async def envelope(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        try:
            if settings.local_http_cookie:
                check_cookie_configuration(request)
            response = await call_next(request)
        except ApiError as exc:
            response = response_error(exc, request.state.request_id)
        except Exception:
            response = response_error(ApiError(500, "INTERNAL_ERROR", "요청을 처리하지 못했습니다."), request.state.request_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    def db():
        if not settings.database_url:
            raise ApiError(500, "INTERNAL_ERROR", "요청을 처리하지 못했습니다.")
        return engine.connect()

    def origin_check(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin or origin not in settings.allowed_origins:
            raise ApiError(403, "ORIGIN_NOT_ALLOWED", "허용되지 않은 요청입니다.")

    def cookie_name() -> str:
        return COOKIE_DEV if settings.local_http_cookie else COOKIE_HOST

    def check_cookie_configuration(request: Request) -> None:
        if settings.local_http_cookie:
            if settings.environment.lower() not in {"development", "dev", "test"}:
                raise ApiError(500, "INTERNAL_ERROR", "요청을 처리하지 못했습니다.")
            host = (request.url.hostname or "").lower()
            loopback_hosts = {"localhost", "127.0.0.1", "::1"}
            configured_hosts = {urlsplit(origin).hostname for origin in settings.allowed_origins}
            if host not in loopback_hosts or any(h not in loopback_hosts for h in configured_hosts):
                raise ApiError(403, "ORIGIN_NOT_ALLOWED", "허용되지 않은 요청입니다.")

    def peer(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def rate(request: Request, kind: str, session_id: str | None, limit: int) -> None:
        if settings.rate_limit_enabled:
            lim = app.state.limiter
            for key in [f"{kind}:ip:{peer(request)}"] + ([f"{kind}:session:{session_id}"] if session_id else []):
                retry = lim.take(key, limit)
                if retry:
                    raise ApiError(429, "RATE_LIMITED", "요청이 너무 많습니다.", retry=retry)

    def find_session(conn, request: Request, refresh: bool = True) -> dict[str, Any] | None:
        raw = request.cookies.get(cookie_name())
        if not raw:
            return None
        digest = hashlib.sha256(raw.encode()).hexdigest()
        row = conn.execute(text("SELECT id, expires_at, revoked_at FROM device_sessions WHERE credential_hash=:h FOR UPDATE"), {"h": digest}).mappings().first()
        now = utcnow()
        if not row or row["revoked_at"] is not None or row["expires_at"] <= now:
            return None
        if refresh:
            expiry = now + timedelta(seconds=COOKIE_AGE)
            conn.execute(text("UPDATE device_sessions SET expires_at=:e, updated_at=:n WHERE id=:id"), {"e": expiry, "n": now, "id": row["id"]})
            return {"id": row["id"], "expires_at": expiry}
        return dict(row)

    def set_session_cookie(response: Response, request: Request, raw: str) -> None:
        dev = settings.local_http_cookie
        response.set_cookie(cookie_name(), raw, max_age=COOKIE_AGE, httponly=True, secure=not dev, samesite="lax", path="/")

    def idempotency_key(request: Request) -> str:
        return parse_uuid(request.headers.get("idempotency-key", ""), "Idempotency-Key")

    def lock_key(session_id: str, method: str, path: str, key: str) -> str:
        return hashlib.sha256(f"{session_id}|{method}|{path}|{key}".encode()).hexdigest()[:60]

    def start_lock(conn, name: str) -> None:
        # GET_LOCK implicitly starts autobegin; commit it before domain transaction.
        ok = conn.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": name}).scalar()
        conn.commit()
        if ok != 1:
            raise ApiError(409, "REQUEST_IN_PROGRESS", "요청을 처리 중입니다.", retry=1)

    def release_lock(conn, name: str) -> None:
        try:
            released = conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": name}).scalar()
            conn.commit()
            if released != 1:
                conn.invalidate()
        except Exception:
            conn.invalidate()

    def fetch_entry(conn, trip_id: str, member_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = conn.execute(text("SELECT t.id,t.name,t.status,t.invite_code,m.id AS member_id,m.nickname,m.role,m.status AS member_status FROM trips t JOIN trip_members m ON m.trip_id=t.id WHERE t.id=:t AND m.id=:m"), {"t": trip_id, "m": member_id}).mappings().first()
        if not row:
            return None
        return ({"id": row["id"], "name": row["name"], "status": row["status"], "invite_code": row["invite_code"]}, {"id": row["member_id"], "nickname": row["nickname"], "role": row["role"], "status": row["member_status"]})

    def replay_check(conn, session_id: str, idem: dict[str, Any]) -> dict[str, Any]:
        current_session = conn.execute(text("SELECT id FROM device_sessions WHERE id=:s AND revoked_at IS NULL AND expires_at>:n FOR UPDATE"), {"s": session_id, "n": utcnow()}).first()
        if not current_session:
            raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
        trip = conn.execute(text("SELECT status FROM trips WHERE id=:t FOR UPDATE"), {"t": idem["trip_id"]}).mappings().first()
        mapping = conn.execute(text("SELECT member_id FROM device_session_memberships WHERE session_id=:s AND trip_id=:t FOR UPDATE"), {"s": session_id, "t": idem["trip_id"]}).mappings().first()
        if not trip or not mapping or mapping["member_id"] != idem["member_id"]:
            raise ApiError(404, "TRIP_NOT_ACCESSIBLE", "여행방에 접근할 수 없습니다.")
        if trip["status"] == "CLOSED":
            raise ApiError(409, "TRIP_CLOSED", "종료된 여행방입니다.")
        member = conn.execute(text("SELECT status FROM trip_members WHERE trip_id=:t AND id=:m"), {"t": idem["trip_id"], "m": idem["member_id"]}).mappings().first()
        if not member or member["status"] != "ACTIVE":
            raise ApiError(403, "MEMBERSHIP_INACTIVE", "참여 상태를 확인해 주세요.")
        snapshot = idem["response_json"]
        if isinstance(snapshot, (str, bytes, bytearray)):
            snapshot = json.loads(snapshot)
        return {"status": idem["http_status"], "body": snapshot}

    def persist_entry(request: Request, mode: str, path: str, normalized: dict[str, str]):
        nickname = normalized["nickname"]
        name = normalized.get("name")
        code = normalized.get("inviteCode")
        key = idempotency_key(request)
        fingerprint = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        conn = db()
        lock = None
        try:
            session = find_session(conn, request)
            if not session:
                conn.rollback()
                raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
            sid = session["id"]
            rate(request, mode, sid, 10 if mode == "create" else 30)
            lock = lock_key(sid, "POST", path, key)
            start_lock(conn, lock)
            existing = conn.execute(text("SELECT * FROM api_idempotency WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at>:n FOR UPDATE"), {"s": sid, "p": path, "k": key, "n": utcnow()}).mappings().first()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    conn.rollback()
                    raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "요청 키가 다른 입력에 사용되었습니다.")
                body_result = replay_check(conn, sid, existing)
                conn.commit()
                response = JSONResponse(body_result["body"], status_code=body_result["status"])
                set_session_cookie(response, request, request.cookies.get(cookie_name(), ""))
                return response
            conn.execute(text("DELETE FROM api_idempotency WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at<=:n"), {"s": sid, "p": path, "k": key, "n": utcnow()})
            conn.commit()
            with conn.begin():
                locked_session = conn.execute(text("SELECT id FROM device_sessions WHERE id=:s AND revoked_at IS NULL AND expires_at>:n FOR UPDATE"), {"s": sid, "n": utcnow()}).first()
                if not locked_session:
                    raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
                if mode == "create":
                    trip_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
                    invite = "CB-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
                    for _ in range(5):
                        try:
                            with conn.begin_nested():
                                conn.execute(text("INSERT INTO trips(id,name,invite_code,status) VALUES(:id,:name,:code,'OPEN')"), {"id": trip_id, "name": name, "code": invite})
                            break
                        except Exception as ex:
                            if "Duplicate" not in str(ex) and "1062" not in str(ex):
                                raise
                            invite = "CB-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
                    else:
                        raise RuntimeError("invite allocation failed")
                    conn.execute(text("INSERT INTO trip_conditions(trip_id) VALUES(:t)"), {"t": trip_id})
                    conn.execute(text("INSERT INTO trip_members(id,trip_id,nickname,role,status) VALUES(:m,:t,:n,'OWNER','ACTIVE')"), {"m": member_id, "t": trip_id, "n": nickname})
                    access = utcnow()
                    conn.execute(text("INSERT INTO device_session_memberships(session_id,trip_id,member_id,last_accessed_at) VALUES(:s,:t,:m,:a)"), {"s": sid, "t": trip_id, "m": member_id, "a": access})
                    status, already = 201, False
                else:
                    trip = conn.execute(text("SELECT id,name,invite_code,status FROM trips WHERE invite_code=:c FOR UPDATE"), {"c": code}).mappings().first()
                    if not trip:
                        raise ApiError(404, "INVITE_NOT_FOUND", "초대 코드를 찾을 수 없습니다.")
                    if trip["status"] != "OPEN":
                        raise ApiError(409, "TRIP_CLOSED", "종료된 여행방입니다.")
                    mapping = conn.execute(text("SELECT member_id FROM device_session_memberships WHERE session_id=:s AND trip_id=:t FOR UPDATE"), {"s": sid, "t": trip["id"]}).mappings().first()
                    if mapping:
                        member = conn.execute(text("SELECT id,nickname,role,status FROM trip_members WHERE trip_id=:t AND id=:m FOR UPDATE"), {"t": trip["id"], "m": mapping["member_id"]}).mappings().first()
                        if not member or member["status"] != "ACTIVE":
                            raise ApiError(403, "MEMBERSHIP_INACTIVE", "참여 상태를 확인해 주세요.")
                        conn.execute(text("UPDATE device_session_memberships SET last_accessed_at=:n WHERE session_id=:s AND trip_id=:t"), {"n": utcnow(), "s": sid, "t": trip["id"]})
                        member_id, status, already = member["id"], 200, True
                    else:
                        member_id, status, already = str(uuid.uuid4()), 201, False
                        conn.execute(text("INSERT INTO trip_members(id,trip_id,nickname,role,status) VALUES(:m,:t,:n,'MEMBER','ACTIVE')"), {"m": member_id, "t": trip["id"], "n": nickname})
                        conn.execute(text("INSERT INTO device_session_memberships(session_id,trip_id,member_id,last_accessed_at) VALUES(:s,:t,:m,:a)"), {"s": sid, "t": trip["id"], "m": member_id, "a": utcnow()})
                    trip_id = trip["id"]
                entry = fetch_entry(conn, trip_id, member_id)
                result = entry_dto(*entry, already)
                conn.execute(text("INSERT INTO api_idempotency(session_id,method,path,idempotency_key,fingerprint,state,trip_id,member_id,http_status,response_json,created_at,expires_at) VALUES(:s,'POST',:p,:k,:f,'COMPLETE',:t,:m,:h,:r,:n,:e)"), {"s": sid, "p": path, "k": key, "f": fingerprint, "t": trip_id, "m": member_id, "h": status, "r": json.dumps(result, ensure_ascii=False, separators=(",", ":")), "n": utcnow(), "e": utcnow()+timedelta(hours=24)})
            response = JSONResponse(result, status_code=status)
            set_session_cookie(response, request, request.cookies.get(cookie_name(), ""))
            return response
        finally:
            if lock:
                try:
                    conn.rollback()
                except Exception:
                    pass
                release_lock(conn, lock)
            conn.close()

    async def create_or_join(request: Request, mode: str, path: str):
        origin_check(request)
        body = await body_json(request, {"name", "nickname"} if mode == "create" else {"inviteCode", "nickname"})
        if mode == "create":
            if set(body) != {"name", "nickname"}:
                raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
            name = valid_text(body["name"], "name", 40)
            nickname = valid_text(body["nickname"], "nickname", 20)
            normalized = {"name": name, "nickname": nickname}
        else:
            if set(body) != {"inviteCode", "nickname"}:
                raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.")
            raw_code = body["inviteCode"]
            if not isinstance(raw_code, str):
                raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"inviteCode": "초대 코드를 확인해 주세요."})
            code = raw_code.strip().upper()
            if not 1 <= len(code) <= 20 or any(ord(c) > 127 for c in code) or not all(c.isascii() and (c.isupper() or c.isdigit() or c == "-") for c in code):
                raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"inviteCode": "초대 코드를 확인해 주세요."})
            nickname = valid_text(body["nickname"], "nickname", 20)
            normalized = {"inviteCode": code, "nickname": nickname}
        return await run_in_threadpool(persist_entry, request, mode, path, normalized)

    def persist_bootstrap(request: Request):
        conn = db()
        try:
            rate(request, "bootstrap", None, 30)
            session = find_session(conn, request)
            if session:
                conn.commit()
                expiry = session["expires_at"]
                raw = request.cookies.get(cookie_name())
            else:
                conn.rollback()
                raw = secrets.token_urlsafe(32)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                expiry = utcnow() + timedelta(seconds=COOKIE_AGE)
                with conn.begin():
                    conn.execute(text("INSERT INTO device_sessions(id,credential_hash,expires_at) VALUES(:i,:h,:e)"), {"i": str(uuid.uuid4()), "h": digest, "e": expiry})
            resp = JSONResponse({"expiresAt": iso(expiry)})
            set_session_cookie(resp, request, raw)
            return resp
        finally:
            conn.close()

    register_preparation_routes(
        app, engine, settings, find_session=find_session, cookie_name=cookie_name,
        origin_check=origin_check, body_json=body_json, ApiError=ApiError,
        set_session_cookie=set_session_cookie,
        interpreter=preparation_interpreter,
    )

    @app.post("/api/v1/device-session")
    async def bootstrap(request: Request):
        origin_check(request)
        await body_json(request, set())
        return await run_in_threadpool(persist_bootstrap, request)

    @app.post("/api/v1/trips")
    async def create_trip(request: Request):
        return await create_or_join(request, "create", "/api/v1/trips")

    @app.post("/api/v1/trip-memberships")
    async def join_trip(request: Request):
        return await create_or_join(request, "join", "/api/v1/trip-memberships")

    @app.get("/api/v1/device-session/trips")
    def recent_trips(request: Request, limit: str = "10", cursor: str | None = None):
        try:
            n = int(limit)
            if str(n) != limit or not 1 <= n <= 50:
                raise ValueError
        except Exception:
            raise ApiError(400, "VALIDATION_ERROR", "입력을 확인해 주세요.", {"limit": "1~50 사이 정수여야 합니다."})
        if cursor is not None and (not cursor or len(cursor) > 2048):
            raise ApiError(400, "INVALID_CURSOR", "커서를 확인해 주세요.", {"cursor": "유효하지 않은 커서입니다."})
        if not settings.cursor_signing_key or len(settings.cursor_signing_key) < 32:
            raise ApiError(500, "INTERNAL_ERROR", "요청을 처리하지 못했습니다.")
        conn = db()
        try:
            session = find_session(conn, request)
            if not session:
                conn.rollback()
                raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
            sid = session["id"]
            params: dict[str, Any] = {"s": sid, "n": n + 1}
            extra = ""
            if cursor:
                last, tid = cursor_decode(settings.cursor_signing_key, cursor, sid)
                extra = " AND (m.last_accessed_at < :last OR (m.last_accessed_at = :last AND m.trip_id > :tid))"
                params.update(last=last, tid=tid)
            rows = conn.execute(text("SELECT t.id AS trip_id,t.name,t.status,m.member_id,tm.nickname,tm.role,tm.status AS member_status,tc.start_date,tc.end_date,m.last_accessed_at,(SELECT COUNT(*) FROM trip_members am WHERE am.trip_id=t.id AND am.status='ACTIVE') AS active_count FROM device_session_memberships m JOIN trips t ON t.id=m.trip_id JOIN trip_members tm ON tm.trip_id=m.trip_id AND tm.id=m.member_id LEFT JOIN trip_conditions tc ON tc.trip_id=t.id WHERE m.session_id=:s" + extra + " ORDER BY m.last_accessed_at DESC,m.trip_id ASC LIMIT :n"), params).mappings().all()
            conn.commit()
            more = len(rows) > n
            rows = rows[:n]
            items = [{"tripId": r["trip_id"], "name": r["name"], "status": r["status"], "memberId": r["member_id"], "nickname": r["nickname"], "role": r["role"], "startDate": r["start_date"].isoformat() if r["start_date"] else None, "endDate": r["end_date"].isoformat() if r["end_date"] else None, "activeMemberCount": int(r["active_count"]), "lastAccessedAt": iso(r["last_accessed_at"]), "canReconnect": r["status"] == "OPEN" and r["member_status"] == "ACTIVE"} for r in rows]
            next_cursor = cursor_encode(settings.cursor_signing_key, sid, rows[-1]["last_accessed_at"], rows[-1]["trip_id"]) if more and rows else None
            response = JSONResponse({"items": items, "nextCursor": next_cursor})
            set_session_cookie(response, request, request.cookies.get(cookie_name(), ""))
            return response
        finally:
            conn.close()

    def persist_reconnect(request: Request, tid: str):
        conn = db()
        try:
            session = find_session(conn, request)
            if not session:
                conn.rollback()
                raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
            conn.commit()
            with conn.begin():
                locked_session = conn.execute(text("SELECT id FROM device_sessions WHERE id=:s AND revoked_at IS NULL AND expires_at>:n FOR UPDATE"), {"s": session["id"], "n": utcnow()}).first()
                if not locked_session:
                    raise ApiError(401, "SESSION_REQUIRED", "기기 세션이 필요합니다.")
                trip = conn.execute(text("SELECT id,status FROM trips WHERE id=:t FOR UPDATE"), {"t": tid}).mappings().first()
                mapping = conn.execute(text("SELECT member_id FROM device_session_memberships WHERE session_id=:s AND trip_id=:t FOR UPDATE"), {"s": session["id"], "t": tid}).mappings().first()
                if not trip or not mapping:
                    raise ApiError(404, "TRIP_NOT_ACCESSIBLE", "여행방에 접근할 수 없습니다.")
                member = conn.execute(text("SELECT id,status FROM trip_members WHERE trip_id=:t AND id=:m FOR UPDATE"), {"t": tid, "m": mapping["member_id"]}).mappings().first()
                if not member or member["status"] != "ACTIVE":
                    raise ApiError(403, "MEMBERSHIP_INACTIVE", "참여 상태를 확인해 주세요.")
                if trip["status"] != "OPEN":
                    raise ApiError(409, "TRIP_CLOSED", "종료된 여행방입니다.")
                conn.execute(text("UPDATE device_session_memberships SET last_accessed_at=:n WHERE session_id=:s AND trip_id=:t"), {"n": utcnow(), "s": session["id"], "t": tid})
                entry = fetch_entry(conn, tid, member["id"])
            response = JSONResponse(entry_dto(*entry, True))
            set_session_cookie(response, request, request.cookies.get(cookie_name(), ""))
            return response
        finally:
            conn.close()

    @app.post("/api/v1/trips/{trip_id}/reconnect")
    async def reconnect(request: Request, trip_id: str):
        origin_check(request)
        await body_json(request, set())
        tid = parse_uuid(trip_id, "tripId")
        return await run_in_threadpool(persist_reconnect, request, tid)

    return app


app = create_app(Settings.from_env())
