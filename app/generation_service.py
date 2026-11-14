"""Private persistence and orchestration for generation APIs.

Provider and routing work is deliberately performed after the short source
capture transaction has closed. Public DTO construction is allowlist based.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from . import confirmed_preferences, generation_foundation as foundation, generation_reviews, place_catalog, preparation, preference_profile, generation_policy
from .group_preferences import build_group_preferences
from .route_preferences import compile_route_preferences, combine_confirmed_evidence, legacy_preferences


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 409, fields: dict[str, str] | None = None, retry: int | None = None):
        self.code, self.status, self.fields, self.retry = code, status, fields or {}, retry
        super().__init__(code)


def _json(v):
    return preparation._json(v)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _default_settings():
    return {"activityWindow": {"start": "09:00", "end": "20:00"}, "departureAnchor": None,
            "returnAnchor": None, "reviewedCommonFields": []}


class GenerationService:
    def __init__(self, app, engine, settings, find_session, cookie_name, ApiError):
        self.app, self.engine, self.settings = app, engine, settings
        self.find_session, self.cookie_name, self.ApiError = find_session, cookie_name, ApiError

    def _conn(self):
        return self.engine.connect()

    def _auth(self, conn, request, trip_id, owner=False):
        try:
            return preparation._access(conn, request, trip_id, self.find_session, self.cookie_name, owner=owner)
        except preparation.PrepFailure as exc:
            raise ServiceError(exc.code, exc.status, exc.fields, exc.retry) from None

    def _rate(self, session_id, member_id):
        if not getattr(self.settings, "rate_limit_enabled", False): return
        limiter = getattr(self.app.state, "limiter", None)
        if not limiter: return
        for key in (f"generation:session:{session_id}", f"generation:member:{member_id}"):
            retry = limiter.take(key, 5)
            if retry: raise ServiceError("RATE_LIMITED", 429, retry=retry)

    def _catalog(self):
        conn = self._conn()
        try:
            value = place_catalog.load_place_catalog(conn)
            conn.commit()
            return value
        except Exception:
            conn.rollback()
            raise ServiceError("CATALOG_UNAVAILABLE", 503) from None
        finally:
            conn.close()

    def _settings(self, conn, trip_id, cv):
        row = conn.execute(text("SELECT conditions_version,settings_version,settings_json FROM generation_settings WHERE trip_id=:t FOR UPDATE"), {"t": trip_id}).mappings().first()
        if not row or int(row["conditions_version"]) != cv:
            return 0, _default_settings()
        return int(row["settings_version"]), _json(row["settings_json"])

    @staticmethod
    def _common_ready(conditions, settings):
        reviewed = {x.get("field"): x for x in settings.get("reviewedCommonFields", []) if isinstance(x, dict)}
        return all(not conditions.get(field) or bool(reviewed.get(field, {}).get("anchors"))
                   for field in ("lodging", "fixedSchedule"))

    def _source_token(self, conn, trip_id, cv, catalog_digest, settings_version):
        members = conn.execute(text("SELECT id,nickname,role FROM trip_members WHERE trip_id=:t AND status='ACTIVE' ORDER BY CASE WHEN role='OWNER' THEN 0 ELSE 1 END,created_at,id FOR UPDATE"), {"t": trip_id}).mappings().all()
        docs = conn.execute(text("SELECT member_id,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at FROM member_preparation_documents WHERE trip_id=:t FOR UPDATE"), {"t": trip_id}).mappings().all()
        docmap = {str(r["member_id"]): r for r in docs}
        personal = []
        statuses = []
        for m in members:
            mid = str(m["id"]); d = docmap.get(mid)
            confirmed = bool(d and d["confirmed_at"] and int(d["confirmed_conditions_version"] or 0) == cv and int(d["confirmed_preference_version"] or 0) == int(d["preference_version"]))
            schedule_ready = True
            source_document_digest = None
            required_ready = True
            if confirmed:
                try:
                    draft_row = conn.execute(text("SELECT draft_json FROM member_preparation_documents WHERE trip_id=:t AND member_id=:m"), {"t": trip_id, "m": mid}).mappings().first()
                    if not draft_row:
                        raise ValueError("missing confirmed document")
                    raw_draft = preparation.stored_draft_without_private_metadata(_json(draft_row["draft_json"]))
                    draft = preparation.validate_draft({**raw_draft, "scheduleItems": raw_draft.get("scheduleItems", [])})
                    source_document_digest = hashlib.sha256(_canonical(draft).encode()).hexdigest()
                    from .walking_preferences import resolve_walking_preference
                    walking_issue = resolve_walking_preference(draft)["issue"] if draft.get("atomicPreferences") is None else None
                    if walking_issue:
                        required_ready = False
                    if m["role"] == "OWNER":
                        profile = draft.get("atomicPreferences")
                        if profile is not None:
                            conditions, live_cv = preparation._conditions(conn, trip_id)
                            checked = preference_profile.response_profile_status(profile, draft.get("originalText"), conditions)
                            required_ready = required_ready and live_cv == cv and not preparation._confirmation_missing({**draft, "atomicPreferences": checked}, 2)
                        else:
                            required_ready = required_ready and bool(draft["likes"] and draft["walking"] in preparation.WALKING)
                    _, unresolved = preparation._schedule_clarifications(conn, draft["scheduleItems"])
                    schedule_ready = not unresolved
                except Exception:
                    source_document_digest = "invalid"
                    schedule_ready = False
                    required_ready = False
            status = "confirmed" if confirmed else "writing"
            statuses.append({"id": mid, "nickname": m["nickname"], "status": status})
            # Unconfirmed drafts are private editing state. They do not enter the
            # generation token or scheduling readiness until the member confirms.
            if confirmed:
                personal.append({"memberId": mid, "role": str(m["role"]), "preferenceVersion": int(d["preference_version"]),
                                 "confirmed": True,
                                 "confirmedConditionsVersion": int(d["confirmed_conditions_version"]),
                                 "confirmedPreferenceVersion": int(d["confirmed_preference_version"]),
                                 "sourceDocumentDigest": source_document_digest, "scheduleReady": schedule_ready,
                                 "requiredReady": required_ready})
        stamp = {"policyVersion": confirmed_preferences.POLICY_VERSION,
                 "generationPolicyVersion": generation_policy.GENERATION_POLICY_VERSION, "conditionsVersion": cv,
                 "members": personal, "settingsVersion": settings_version, "catalogDigest": catalog_digest}
        return hashlib.sha256(_canonical(stamp).encode()).hexdigest(), statuses, personal

    def _confirmed_issues(self, conn, trip_id, catalog, common_settings=None):
        from .candidate_selection import build_supported_tags
        conditions, conditions_version = preparation._conditions(conn, trip_id)
        members = conn.execute(text("SELECT id,role FROM trip_members WHERE trip_id=:t AND status='ACTIVE' ORDER BY created_at,id FOR UPDATE"), {"t": trip_id}).mappings().all()
        docs = conn.execute(text("SELECT member_id,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at FROM member_preparation_documents WHERE trip_id=:t FOR UPDATE"), {"t": trip_id}).mappings().all()
        by_member = {str(r["member_id"]): r for r in docs}
        tags = build_supported_tags(catalog)
        issues = set()
        confirmed_group = []
        atomic_members = []
        for member in members:
            doc = by_member.get(str(member["id"]))
            if not doc or not foundation._confirmed_doc(doc, conditions_version): continue
            try:
                draft_row = conn.execute(text("SELECT draft_json FROM member_preparation_documents WHERE trip_id=:t AND member_id=:m"),
                                         {"t": trip_id, "m": str(member["id"])}).mappings().first()
                if not draft_row:
                    issues.add("PREPARATION_NOT_READY"); continue
                raw_draft = preparation.stored_draft_without_private_metadata(_json(draft_row["draft_json"]))
                draft = preparation.validate_draft({**raw_draft, "scheduleItems": raw_draft.get("scheduleItems", [])})
            except Exception:
                issues.add("PREPARATION_NOT_READY"); continue
            if member["role"] != "OWNER" and any(item["scope"] == "group" for item in draft["scheduleItems"]):
                issues.add("SCHEDULE_SCOPE_INVALID")
            from .walking_preferences import resolve_walking_preference
            if draft.get("atomicPreferences") is None and resolve_walking_preference(draft)["issue"]:
                issues.add("REQUIRED_PREFERENCE_UNSATISFIED")
            pref = {"memberRef": f"p{len(confirmed_group) + 1}", **{k: draft[k] for k in ("likes", "avoid", "avoidImportance", "food", "foodImportance", "walking", "scheduleItems")}}
            confirmed_group.append(pref)
            if draft.get("atomicPreferences") is not None:
                checked = preference_profile.response_profile_status(draft["atomicPreferences"], draft.get("originalText"), conditions)
                if not isinstance(checked, dict) or checked.get("status") != "current":
                    issues.add("PREFERENCES_UNCONFIRMED")
                else:
                    atomic_members.append({"memberRef": pref["memberRef"], "confirmed": True, "atomicPreferences": checked})
            if member["role"] == "OWNER":
                try:
                    confirmed_preferences.group_schedule_anchors([pref], common_settings, {"members": [{"memberId": member["id"], "role": "OWNER"}]})
                except ValueError:
                    issues.add("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
        route_preferences = compile_route_preferences({"conditions": conditions,
            "atomicMemberRefs": [member["memberRef"] for member in atomic_members],
            "groupConditions": build_group_preferences(atomic_members)}, catalog)
        issues.update(item["code"] for item in combine_confirmed_evidence(confirmed_group, tags, route_preferences)["issues"]
                      if item["code"].startswith("REQUIRED_"))
        return sorted(issues)

    def _snapshot(self, conn, request, trip_id):
        # `_capture` uses the established trip -> membership -> member/doc lock order.
        def authorize(c, tid):
            try:
                s, t, m = self._auth(c, request, tid, owner=True)
                return s, t, m
            except ServiceError as exc:
                raise foundation.GenerationFoundationError(exc.code) from None
        return foundation._capture(conn, trip_id, authorize)

    def get_generation(self, request, trip_id):
        catalog = self._catalog()
        conn = self._conn()
        try:
            session, trip, actor = self._auth(conn, request, trip_id)
            conn.execute(text("UPDATE generation_jobs SET status='FAILED',phase='failed',error_code='PROCESS_INTERRUPTED' WHERE trip_id=:t AND status IN ('QUEUED','RUNNING') AND lease_expires_at<=:n"), {"t": trip_id, "n": _now()})
            conditions, cv = preparation._conditions(conn, trip_id)
            sv, settings = self._settings(conn, trip_id, cv)
            token, statuses, personal = self._source_token(conn, trip_id, cv, catalog.digest, sv)
            ready, reasons = True, []
            if not all(conditions.get(k) is not None for k in ("region", "startDate", "endDate", "expectedPeople", "budget", "transport")):
                ready = False; reasons.append({"code": "CONDITIONS_INCOMPLETE"})
            if not any(x["role"] == "OWNER" and x["requiredReady"] for x in personal):
                ready = False; reasons.append({"code": "PREFERENCES_UNCONFIRMED"})
            if any(not x["scheduleReady"] for x in personal):
                ready = False; reasons.append({"code": "SCHEDULE_ITEM_CLARIFICATION_REQUIRED"})
            for issue in self._confirmed_issues(conn, trip_id, catalog, settings):
                ready = False; reasons.append({"code": issue})
            if not self._common_ready(conditions, settings):
                ready = False
                reasons.extend({"code": "LODGING_ANCHOR_REQUIRED" if field == "lodging" else "FIXED_SCHEDULE_ANCHOR_REQUIRED"}
                               for field in ("lodging", "fixedSchedule") if conditions.get(field))
            latest = conn.execute(text("SELECT id,status,phase,plan_id,version_id,error_code,created_at,updated_at FROM generation_jobs WHERE trip_id=:t AND kind='GENERATE' ORDER BY created_at DESC LIMIT 1"), {"t": trip_id}).mappings().first()
            plan = None
            if latest and latest["plan_id"]:
                from . import plan_store
                rec = plan_store.get_plan_record(conn, trip_id, latest["plan_id"])
                if rec:
                    current = bool(rec["public"]["status"] in {"DRAFT", "SAVED"}
                                   and rec["public"]["versionId"] == latest["version_id"]
                                   and rec["sourceStamp"].get("inputToken") == token
                                   and rec["sourceStamp"].get("catalogDigest") == catalog.digest)
                    plan = {"planId": latest["plan_id"], "versionId": latest["version_id"],
                            "status": rec["public"]["status"], "current": current}
            conn.commit()
            return {"conditionsVersion": cv, "inputToken": token, "settingsVersion": sv, "settings": settings,
                    "preparationMembers": statuses,
                    "reviewMembers": [{**m, "status": "reviewed" if m["status"] == "confirmed" else "stale"}
                                      for m in statuses],
                    "readyForGeneration": bool(ready and actor["role"] == "OWNER"),
                    "reasons": reasons, "latestJob": self._job_public(latest) if latest else None, "latestPlan": plan}
        except ServiceError:
            conn.rollback(); raise
        except Exception:
            conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally:
            conn.close()

    @staticmethod
    def _job_public(row):
        def iso(v): return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if v else None
        return {"id": row["id"], "kind": "GENERATE", "status": row["status"], "phase": row["phase"],
                "planId": row["plan_id"], "versionId": row["version_id"], "errorCode": row["error_code"],
                "createdAt": iso(row["created_at"]), "updatedAt": iso(row["updated_at"])}

    def _reusable_generation_job(self, conn, trip_id, row, input_token, catalog_digest, now):
        stamp = _json(row["input_stamp_json"])
        if stamp.get("inputToken") != input_token or stamp.get("catalogDigest") != catalog_digest:
            return False
        if row["status"] in {"QUEUED", "RUNNING"}:
            return row["expires_at"] > now and row["lease_expires_at"] > now
        if row["status"] != "SUCCEEDED" or not row["plan_id"] or not row["version_id"]:
            return False
        from . import plan_store
        record = plan_store.get_plan_record(conn, trip_id, row["plan_id"])
        return bool(record and record["public"]["status"] in {"DRAFT", "SAVED"}
                    and record["public"]["versionId"] == row["version_id"]
                    and record["sourceStamp"].get("inputToken") == input_token
                    and record["sourceStamp"].get("catalogDigest") == catalog_digest)

    @staticmethod
    def _store_coalesced_key(conn, candidate, trip_id, member, session, path, key, fingerprint, now):
        """Bind a different key to the canonical job without creating another job row."""
        conn.execute(text("INSERT INTO api_idempotency(session_id,method,path,idempotency_key,fingerprint,state,trip_id,member_id,http_status,response_json,created_at,expires_at) VALUES(:s,'POST',:p,:k,:f,'COMPLETE',:t,:m,200,:response,:n,:e)"), {
            "s": session["id"], "p": path, "k": key, "f": fingerprint, "t": trip_id,
            "m": member["id"], "response": _canonical({"coalescedJobId": candidate["id"]}),
            "n": now, "e": now + timedelta(hours=24)})

    def _self_capture(self, conn, request, trip_id):
        session, trip, member = self._auth(conn, request, trip_id)
        conditions, cv = preparation._conditions(conn, trip_id)
        row = preparation._personal(conn, trip_id, member["id"])
        if not row or not foundation._confirmed_doc(row, cv):
            raise ServiceError("PREFERENCES_UNCONFIRMED", 409)
        draft = preparation.validate_draft(preparation.stored_draft_without_private_metadata(_json(row["draft_json"])))
        if not draft["originalText"]:
            raise ServiceError("PREFERENCES_UNCONFIRMED", 409)
        profile = draft.get("atomicPreferences")
        checked = preference_profile.response_profile_status(profile, draft.get("originalText"), conditions) if profile is not None else None
        if profile is not None and (not isinstance(checked, dict) or checked.get("status") != "current"):
            raise ServiceError("PREFERENCES_UNCONFIRMED", 409)
        prefs = [{"memberRef": "p1", **{k: draft[k] for k in ("likes", "avoid", "avoidImportance", "food", "foodImportance", "walking", "scheduleItems")}}]
        group_conditions = build_group_preferences([{"memberRef": "p1", "confirmed": True, "atomicPreferences": checked}]) if checked else build_group_preferences([])
        identity = {"tripId": trip_id, "sessionId": session["id"], "actorMemberId": member["id"],
                    "actorRole": member["role"], "conditionsVersion": cv,
                    "members": [{"memberId": member["id"], "preferenceVersion": int(row["preference_version"]),
                                 "confirmedConditionsVersion": int(row["confirmed_conditions_version"]),
                                 "confirmedPreferenceVersion": int(row["confirmed_preference_version"])}]}
        atomic_refs = ["p1"] if checked else []
        stamp = {"conditions": conditions, "conditionsVersion": cv, "confirmedPreferences": prefs,
                 "groupConditions": group_conditions, "atomicMemberRefs": atomic_refs, "_identity": identity}
        fingerprint = hashlib.sha256(_canonical({**identity, "confirmedPreferences": prefs,
                                               "groupConditions": group_conditions, "atomicMemberRefs": atomic_refs}).encode()).hexdigest()
        return foundation.ConfirmedPreparationSnapshot(_canonical(stamp), fingerprint), draft, int(row["preference_version"]), member, session

    def get_my_preference(self, request, trip_id):
        trip_id = str(uuid.UUID(trip_id)); catalog = self._catalog(); conn = self._conn()
        try:
            snapshot, draft, pv, member, _ = self._self_capture(conn, request, trip_id)
            cv = snapshot.as_dict()["conditionsVersion"]
            row = conn.execute(text("SELECT * FROM generation_member_reviews WHERE trip_id=:t AND member_id=:m FOR UPDATE"), {"t": trip_id, "m": member["id"]}).mappings().first()
            raw = _json(row["normalization_json"]) if row and row["catalog_digest"] == catalog.digest else None
            review = _json(row["review_json"]) if row and row["review_json"] else None
            version = int(row["review_version"]) if row else 0
            source_digest = hashlib.sha256(_canonical(draft).encode()).hexdigest()
            current = bool(row and int(row["conditions_version"]) == cv and int(row["preference_version"]) == pv and row["source_fingerprint"] == snapshot.fingerprint and row["catalog_digest"] == catalog.digest and _json(row["normalization_json"]).get("sourceDocumentDigest") == source_digest)
            conn.commit()
            dto = generation_reviews.normalization_dto(raw, review, version, current) if raw and current else {"normalization": None, "reviewVersion": version, "review": review if current else None, "stale": bool(row)}
            from .candidate_selection import build_supported_tags
            issues = confirmed_preferences.preference_issues(snapshot.confirmed_preferences(), build_supported_tags(catalog))
            from .walking_preferences import resolve_walking_preference
            if resolve_walking_preference(draft)["issue"]:
                walking_issue = {"field": "walking", "code": "REQUIRED_PREFERENCE_UNSATISFIED"}
                if walking_issue not in issues:
                    issues.append(walking_issue)
            clarifications = []
            if draft.get("scheduleItems"):
                _, clarifications = preparation._schedule_clarifications(conn, draft["scheduleItems"])
            return {"conditionsVersion": cv, "preferenceVersion": pv, "catalogDigest": catalog.digest,
                    "preparationIssues": issues, "clarifications": clarifications, **dto}
        except (ValueError, ServiceError): conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    async def normalize_self(self, request, trip_id, body, key):
        trip_id = str(uuid.UUID(trip_id)); catalog = await run_in_threadpool(self._catalog)
        reservation = await run_in_threadpool(self._reserve_normalize, request, trip_id, body, key, catalog)
        if "replay" in reservation: return reservation["replay"]
        snapshot, draft, pv, member, session, path, token = (reservation[k] for k in ("snapshot", "draft", "pv", "member", "session", "path", "token"))
        from .candidate_selection import build_supported_tags, _normalization
        tags = list(build_supported_tags(catalog))
        fn = self.app.state.generation_normalizer
        try:
            call = asyncio.to_thread(fn, snapshot.confirmed_preferences(), tags) if fn else asyncio.to_thread(self._default_normalizer, snapshot.confirmed_preferences(), tags)
            raw = await asyncio.wait_for(call, timeout=self.app.state.generation_normalize_timeout)
            raw = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
        except asyncio.TimeoutError:
            await run_in_threadpool(self._finish_normalize_error, session["id"], path, key, token, "AI_TIMEOUT")
            raise ServiceError("AI_TIMEOUT", 504) from None
        except foundation.GenerationFoundationError as exc:
            safe = "AI_TIMEOUT" if exc.code == "AI_TIMEOUT" else "AI_UNAVAILABLE" if exc.code == "AI_UNAVAILABLE" else "AI_INVALID_RESPONSE"
            await run_in_threadpool(self._finish_normalize_error, session["id"], path, key, token, safe)
            raise ServiceError(safe, 504 if safe == "AI_TIMEOUT" else 503 if safe == "AI_UNAVAILABLE" else 502) from None
        except Exception as exc:
            await run_in_threadpool(self._finish_normalize_error, session["id"], path, key, token, "AI_INVALID_RESPONSE")
            raise ServiceError("AI_INVALID_RESPONSE", 502) from None
        try:
            _normalization(raw, snapshot.confirmed_preferences(), set(tags))
        except Exception:
            await run_in_threadpool(self._finish_normalize_error, session["id"], path, key, token, "AI_INVALID_RESPONSE")
            raise ServiceError("AI_INVALID_RESPONSE", 502) from None
        return await run_in_threadpool(self._finish_normalize, request, trip_id, key, reservation, raw, catalog.digest)

    @staticmethod
    def _default_normalizer(preferences, tags):
        try:
            from chungbuk_trip_ai import normalize_confirmed_preferences
            from chungbuk_trip_ai import (GeminiError, GeminiInvalidResponseError,
                                          GeminiTimeoutError, GeminiUnavailableError)
        except Exception:
            raise foundation.GenerationFoundationError("AI_UNAVAILABLE") from None
        try:
            return normalize_confirmed_preferences(confirmed_preferences=preferences, supported_tags=tags)
        except GeminiTimeoutError:
            raise foundation.GenerationFoundationError("AI_TIMEOUT") from None
        except GeminiUnavailableError:
            raise foundation.GenerationFoundationError("AI_UNAVAILABLE") from None
        except GeminiInvalidResponseError:
            raise foundation.GenerationFoundationError("AI_INVALID_RESPONSE") from None
        except GeminiError:
            raise foundation.GenerationFoundationError("AI_INVALID_RESPONSE") from None
        except Exception:
            raise foundation.GenerationFoundationError("AI_UNAVAILABLE") from None

    def _reserve_normalize(self, request, trip_id, body, key, catalog):
        conn = self._conn()
        try:
            snapshot, draft, pv, member, session = self._self_capture(conn, request, trip_id)
            self._rate(session["id"], member["id"])
            cv = snapshot.as_dict()["conditionsVersion"]
            if body["expectedConditionsVersion"] != cv or body["expectedPreferenceVersion"] != pv:
                raise ServiceError("VERSION_CONFLICT")
            path = f"/api/v1/trips/{trip_id}/generation/preferences/me"
            fp = hashlib.sha256(_canonical(body).encode()).hexdigest()
            old = conn.execute(text("SELECT * FROM generation_jobs WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at>:n FOR UPDATE"), {"s": session["id"], "p": path, "k": key, "n": _now()}).mappings().first()
            if old:
                if old["fingerprint"] != fp: raise ServiceError("IDEMPOTENCY_CONFLICT")
                if old["status"] == "SUCCEEDED":
                    payload = _json(old["response_json"])
                    prior_stamp = _json(old["input_stamp_json"])
                    if prior_stamp.get("sourceFingerprint") != snapshot.fingerprint or prior_stamp.get("catalogDigest") != catalog.digest: raise ServiceError("STALE_GENERATION_INPUT")
                    conn.commit(); return {"replay": payload}
                if old["lease_expires_at"] > _now(): raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
                active = conn.execute(text("SELECT id FROM generation_jobs WHERE trip_id=:t AND kind='NORMALIZE_SELF' AND member_id=:m AND status='RUNNING' AND lease_expires_at>:n AND id<>:id LIMIT 1 FOR UPDATE"), {"t": trip_id, "m": member["id"], "n": _now(), "id": old["id"]}).first()
                if active: raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
                token = str(uuid.uuid4())
                conn.execute(text("UPDATE generation_jobs SET lease_token=:token,lease_expires_at=:lease,status='RUNNING',phase='normalizing',error_code=NULL WHERE id=:id"), {"token": token, "lease": _now()+timedelta(seconds=75), "id": old["id"]})
            else:
                active = conn.execute(text("SELECT id FROM generation_jobs WHERE trip_id=:t AND kind='NORMALIZE_SELF' AND member_id=:m AND status='RUNNING' AND lease_expires_at>:n LIMIT 1 FOR UPDATE"), {"t": trip_id, "m": member["id"], "n": _now()}).first()
                if active: raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
                job_id, token = str(uuid.uuid4()), str(uuid.uuid4())
                conn.execute(text("DELETE FROM generation_jobs WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at<=:n"), {"s": session["id"], "p": path, "k": key, "n": _now()})
                conn.execute(text("INSERT INTO generation_jobs(id,trip_id,member_id,session_id,kind,method,path,idempotency_key,fingerprint,lease_token,lease_expires_at,status,phase,input_stamp_json,created_at,updated_at,expires_at) VALUES(:id,:t,:m,:s,'NORMALIZE_SELF','POST',:p,:k,:f,:token,:lease,'RUNNING','normalizing',:stamp,:n,:n,:exp)"), {"id": job_id, "t": trip_id, "m": member["id"], "s": session["id"], "p": path, "k": key, "f": fp, "token": token, "lease": _now()+timedelta(seconds=75), "stamp": _canonical({"sourceFingerprint": snapshot.fingerprint, "sourceDocumentDigest": hashlib.sha256(_canonical(draft).encode()).hexdigest(), "conditionsVersion": cv, "preferenceVersion": pv, "catalogDigest": catalog.digest}), "n": _now(), "exp": _now()+timedelta(hours=24)})
            conn.commit()
            return {"snapshot": snapshot, "draft": draft, "pv": pv, "member": member, "session": session,
                    "path": path, "token": token}
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def _finish_normalize(self, request, trip_id, key, reservation, raw, catalog_digest):
        snapshot, draft, pv, member, session, path, token = (reservation[k] for k in ("snapshot", "draft", "pv", "member", "session", "path", "token"))
        conn = self._conn()
        try:
            fresh, fresh_draft, pv2, member2, session2 = self._self_capture(conn, request, trip_id)
            if (fresh.fingerprint != snapshot.fingerprint or pv2 != pv or member2["id"] != member["id"]
                    or session2["id"] != session["id"]
                    or hashlib.sha256(_canonical(fresh_draft).encode()).hexdigest() != hashlib.sha256(_canonical(draft).encode()).hexdigest()):
                conn.execute(text("UPDATE generation_jobs SET status='STALE',phase='failed',error_code='STALE_GENERATION_INPUT',lease_expires_at=:n WHERE session_id=:s AND path=:p AND idempotency_key=:k AND lease_token=:token AND status='RUNNING'"), {"n": _now(), "s": session["id"], "p": path, "k": key, "token": token})
                conn.commit()
                raise ServiceError("STALE_GENERATION_INPUT")
            cv = snapshot.as_dict()["conditionsVersion"]
            active = conn.execute(text("SELECT id FROM generation_jobs WHERE session_id=:s AND path=:p AND idempotency_key=:k AND kind='NORMALIZE_SELF' AND status='RUNNING' AND lease_token=:token AND lease_expires_at>:n FOR UPDATE"), {"s": session["id"], "p": path, "k": key, "token": token, "n": _now()}).first()
            if not active:
                raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
            norm_digest = hashlib.sha256(_canonical(raw).encode()).hexdigest()
            norm_id = str(uuid.uuid4())
            current_draft = preparation.validate_draft(preparation.stored_draft_without_private_metadata(
                _json(preparation._personal(conn, trip_id, member["id"])["draft_json"])))
            current_catalog = place_catalog.load_place_catalog(conn, lock=True)
            if current_catalog.digest != catalog_digest:
                raise ServiceError("STALE_PLACE_CATALOG")
            stored = {"id": norm_id, "sourceFingerprint": snapshot.fingerprint,
                      "sourceDocumentDigest": hashlib.sha256(_canonical(current_draft).encode()).hexdigest(),
                      "catalogDigest": catalog_digest, "normalization": raw}
            conn.execute(text("INSERT INTO generation_member_reviews(trip_id,member_id,conditions_version,preference_version,catalog_digest,source_fingerprint,normalization_digest,normalization_json,review_json,review_version) VALUES(:t,:m,:cv,:pv,:cd,:sf,:nd,:nj,NULL,0) ON DUPLICATE KEY UPDATE conditions_version=VALUES(conditions_version),preference_version=VALUES(preference_version),catalog_digest=VALUES(catalog_digest),source_fingerprint=VALUES(source_fingerprint),normalization_digest=VALUES(normalization_digest),normalization_json=VALUES(normalization_json),review_json=NULL,review_version=0"), {"t": trip_id, "m": member["id"], "cv": cv, "pv": pv, "cd": catalog_digest, "sf": snapshot.fingerprint, "nd": norm_digest, "nj": _canonical(stored)})
            dto = generation_reviews.normalization_dto(stored, None, 0, True)
            dto.update(conditionsVersion=cv, preferenceVersion=pv, catalogDigest=catalog_digest)
            conn.execute(text("UPDATE generation_jobs SET status='SUCCEEDED',phase='complete',response_json=:r,lease_expires_at=:now,expires_at=:exp WHERE session_id=:s AND path=:p AND idempotency_key=:k AND kind='NORMALIZE_SELF' AND lease_token=:token AND status='RUNNING' AND lease_expires_at>:now"), {"r": _canonical(dto), "now": _now(), "exp": _now()+timedelta(hours=24), "s": session["id"], "p": path, "k": key, "token": token})
            if conn.execute(text("SELECT ROW_COUNT()")).scalar() != 1: raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
            conn.commit(); return dto
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def _finish_normalize_error(self, session_id, path, key, token, code):
        conn = self._conn()
        try:
            conn.execute(text("UPDATE generation_jobs SET status='FAILED',phase='failed',error_code=:c,lease_expires_at=:n WHERE session_id=:s AND path=:p AND idempotency_key=:k AND kind='NORMALIZE_SELF' AND lease_token=:token AND status='RUNNING'"), {"c": code, "n": _now(), "s": session_id, "p": path, "k": key, "token": token})
            conn.commit()
        finally: conn.close()

    def save_review(self, request, trip_id, body):
        trip_id = str(uuid.UUID(trip_id)); catalog = self._catalog(); conn = self._conn()
        try:
            snapshot, draft, pv, member, _ = self._self_capture(conn, request, trip_id); cv = snapshot.as_dict()["conditionsVersion"]
            if place_catalog.load_place_catalog(conn, lock=True).digest != catalog.digest:
                raise ServiceError("STALE_PLACE_CATALOG")
            row = conn.execute(text("SELECT * FROM generation_member_reviews WHERE trip_id=:t AND member_id=:m FOR UPDATE"), {"t": trip_id, "m": member["id"]}).mappings().first()
            if (not row or int(row["conditions_version"]) != cv or int(row["preference_version"]) != pv
                    or row["catalog_digest"] != catalog.digest or row["source_fingerprint"] != snapshot.fingerprint
                    or _json(row["normalization_json"]).get("sourceDocumentDigest") != hashlib.sha256(_canonical(draft).encode()).hexdigest()):
                raise ServiceError("STALE_GENERATION_INPUT")
            stored = _json(row["normalization_json"]); review = generation_reviews.validate_review(body, stored, int(row["review_version"]))
            review.update({"normalizationId": stored["id"], "sourceFingerprint": snapshot.fingerprint,
                           "normalizationDigest": row["normalization_digest"], "catalogDigest": catalog.digest})
            conn.execute(text("UPDATE generation_member_reviews SET review_json=:r,review_version=review_version+1 WHERE trip_id=:t AND member_id=:m AND review_version=:v"), {"r": _canonical(review), "t": trip_id, "m": member["id"], "v": int(row["review_version"])})
            result = generation_reviews.normalization_dto(stored, review, int(row["review_version"])+1, True)
            result.update(conditionsVersion=cv, preferenceVersion=pv, catalogDigest=catalog.digest); conn.commit(); return result
        except ValueError as exc: conn.rollback(); raise ServiceError(str(exc), 409 if str(exc) == "VERSION_CONFLICT" else 400) from None
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def put_settings(self, request, trip_id, body):
        trip_id = str(uuid.UUID(trip_id)); conn = self._conn()
        try:
            _, _, owner = self._auth(conn, request, trip_id, owner=True); conditions, cv = preparation._conditions(conn, trip_id)
            exact = {"expectedConditionsVersion", "expectedSettingsVersion", "settings"}
            if not isinstance(body, dict) or set(body) != exact or any(isinstance(body[k], bool) or not isinstance(body[k], int) or body[k] < 0 for k in ("expectedConditionsVersion", "expectedSettingsVersion")):
                raise ServiceError("VALIDATION_ERROR", 400)
            if body["expectedConditionsVersion"] != cv:
                raise ServiceError("VERSION_CONFLICT")
            sv, _ = self._settings(conn, trip_id, cv)
            if body["expectedSettingsVersion"] != sv: raise ServiceError("VERSION_CONFLICT")
            value = self._validate_settings(body["settings"], conditions, conn)
            conn.execute(text("INSERT INTO generation_settings(trip_id,conditions_version,settings_version,settings_json) VALUES(:t,:cv,1,:j) ON DUPLICATE KEY UPDATE settings_version=IF(conditions_version=VALUES(conditions_version),settings_version+1,1),settings_json=VALUES(settings_json),conditions_version=VALUES(conditions_version)"), {"t": trip_id, "cv": cv, "j": _canonical(value)})
            new = sv + 1; conn.commit(); return {"settingsVersion": new, "conditionsVersion": cv, "settings": value}
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def _validate_settings(self, value, conditions, conn):
        if not isinstance(value, dict) or set(value) != {"activityWindow", "departureAnchor", "returnAnchor", "reviewedCommonFields"}:
            raise ServiceError("VALIDATION_ERROR", 400)
        window = value["activityWindow"]
        def minute(v):
            if not isinstance(v, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", v): return None
            try: h, m = map(int, v.split(":")); return h*60+m if 0 <= h <= 23 and 0 <= m <= 59 else None
            except Exception: return None
        if not isinstance(window, dict) or set(window) != {"start", "end"} or minute(window.get("start")) is None or minute(window.get("end")) is None or minute(window["end"]) <= minute(window["start"]):
            raise ServiceError("VALIDATION_ERROR", 400, {"settings.activityWindow": "시간 범위를 확인해 주세요."})
        def location(v):
            if v is None: return None
            if not isinstance(v, dict) or set(v) != {"label", "coordinate"} or not isinstance(v["label"], str) or not 1 <= len(v["label"].strip()) <= 100: raise ServiceError("VALIDATION_ERROR", 400)
            c = v["coordinate"]
            if not isinstance(c, dict) or set(c) != {"latitude", "longitude", "crs"} or c["crs"] != "WGS84": raise ServiceError("VALIDATION_ERROR", 400)
            try:
                if isinstance(c["latitude"], bool) or isinstance(c["longitude"], bool): raise ValueError
                lat, lon = float(c["latitude"]), float(c["longitude"])
            except Exception: raise ServiceError("VALIDATION_ERROR", 400) from None
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180): raise ServiceError("VALIDATION_ERROR", 400)
            return {"label": v["label"].strip(), "coordinate": {"latitude": lat, "longitude": lon, "crs": "WGS84"}}
        anchors = value["reviewedCommonFields"]
        if not isinstance(anchors, list) or len(anchors) > 2: raise ServiceError("VALIDATION_ERROR", 400)
        allowed_dates = set()
        try:
            from datetime import date, timedelta
            first, last = date.fromisoformat(conditions["startDate"]), date.fromisoformat(conditions["endDate"])
            if (last-first).days > 30: raise ValueError
            allowed_dates = {(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)}
        except Exception:
            if conditions.get("startDate") or conditions.get("endDate"):
                raise ServiceError("VALIDATION_ERROR", 400) from None
        place_rows = conn.execute(text("SELECT contentid FROM places WHERE contentid IS NOT NULL")).scalars().all()
        known_places = {int(x) for x in place_rows}
        clean_anchors = []; seen_fields = set(); seen_keys = set(); seen_places_by_day = set(); per_day = {}; total = 0
        for item in anchors:
            if (not isinstance(item, dict) or set(item) != {"field", "reviewed", "anchors"}
                    or item["field"] not in {"lodging", "fixedSchedule"} or item["field"] in seen_fields
                    or item["reviewed"] is not True or not isinstance(item["anchors"], list) or not item["anchors"]):
                raise ServiceError("VALIDATION_ERROR", 400)
            field = item["field"]
            if not conditions.get(field): raise ServiceError("VALIDATION_ERROR", 400)
            seen_fields.add(field); field_anchors = []
            for anchor in item["anchors"]:
                required = {"date", "contentid", "position", "startTime", "endTime", "arrivalDeadline", "stayMinutes"}
                if not isinstance(anchor, dict) or set(anchor) != required: raise ServiceError("VALIDATION_ERROR", 400)
                day, cid, pos = anchor["date"], anchor["contentid"], anchor["position"]
                if (day not in allowed_dates or isinstance(cid, bool) or not isinstance(cid, int) or cid not in known_places
                        or isinstance(pos, bool) or not isinstance(pos, int) or not 0 <= pos <= 7):
                    raise ServiceError("VALIDATION_ERROR", 400)
                k = (day, pos)
                place_day = (day, cid)
                if k in seen_keys or place_day in seen_places_by_day: raise ServiceError("VALIDATION_ERROR", 400)
                seen_keys.add(k); per_day[day] = per_day.get(day, 0)+1; total += 1
                seen_places_by_day.add(place_day)
                for clock in ("startTime", "endTime", "arrivalDeadline"):
                    if anchor[clock] is not None and minute(anchor[clock]) is None: raise ServiceError("VALIDATION_ERROR", 400)
                if anchor["stayMinutes"] is not None and (isinstance(anchor["stayMinutes"], bool) or not isinstance(anchor["stayMinutes"], int) or not 1 <= anchor["stayMinutes"] <= 1440):
                    raise ServiceError("VALIDATION_ERROR", 400)
                field_anchors.append({k: anchor[k] for k in required})
            clean_anchors.append({"field": field, "reviewed": True, "anchors": field_anchors})
        if total > 64 or any(count > 8 for count in per_day.values()): raise ServiceError("VALIDATION_ERROR", 400)
        for field in ("lodging", "fixedSchedule"):
            if conditions.get(field) and field not in seen_fields:
                raise ServiceError("REVIEW_REQUIRED")
        return {"activityWindow": {"start": window["start"], "end": window["end"]}, "departureAnchor": location(value["departureAnchor"]),
                "returnAnchor": location(value["returnAnchor"]), "reviewedCommonFields": clean_anchors}

    def search_places(self, request, trip_id, q, limit):
        if not isinstance(q, str) or len(q) > 100 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ServiceError("VALIDATION_ERROR", 400)
        self._auth_public(request, str(uuid.UUID(trip_id)))
        catalog = self._catalog(); needle = q.casefold(); places = []
        for p in catalog.places:
            if not p.get("contentid") or not isinstance(p.get("title"), str): continue
            coord = place_catalog._coord(p)
            if not coord: continue
            if needle and needle not in (str(p.get("title") or "") + " " + str(p.get("addr1") or "")).casefold(): continue
            places.append({"contentid": p["contentid"], "title": p["title"], "address": p.get("addr1"), "planningRole": p.get("planningRole"), "coordinate": coord})
            if len(places) >= limit: break
        return {"places": places}

    def _auth_public(self, request, tid):
        conn = self._conn()
        try: self._auth(conn, request, tid); conn.commit()
        except ServiceError: conn.rollback(); raise
        finally: conn.close()

    def create_job(self, request, trip_id, body, key):
        trip_id = str(uuid.UUID(trip_id)); catalog = self._catalog(); conn = self._conn()
        try:
            session, _, member = self._auth(conn, request, trip_id, owner=True); conditions, cv = preparation._conditions(conn, trip_id)
            if not isinstance(body, dict): raise ServiceError("VALIDATION_ERROR", 400)
            conn.execute(text("UPDATE generation_jobs SET status='FAILED',phase='failed',error_code='PROCESS_INTERRUPTED' WHERE trip_id=:t AND status IN ('QUEUED','RUNNING') AND lease_expires_at<=:n"), {"t": trip_id, "n": _now()})
            sv, _ = self._settings(conn, trip_id, cv); token, statuses, personal = self._source_token(conn, trip_id, cv, catalog.digest, sv)
            if set(body) != {"expectedInputToken", "expectedSettingsVersion"}:
                raise ServiceError("VALIDATION_ERROR", 400)
            path = f"/api/v1/trips/{trip_id}/generation/jobs"
            fp = hashlib.sha256(_canonical(body).encode()).hexdigest()
            now = _now()
            old = conn.execute(text("SELECT * FROM generation_jobs WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k FOR UPDATE"),
                               {"s": session["id"], "p": path, "k": key}).mappings().first()
            receipt = conn.execute(text("SELECT * FROM api_idempotency WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k FOR UPDATE"),
                                   {"s": session["id"], "p": path, "k": key}).mappings().first()
            if old and (old["trip_id"] != trip_id or old["member_id"] != member["id"]):
                raise ServiceError("NOT_AUTHORIZED", 403)
            if receipt and (receipt["trip_id"] != trip_id or receipt["member_id"] != member["id"]):
                raise ServiceError("NOT_AUTHORIZED", 403)
            if old and old["expires_at"] > now and old["fingerprint"] != fp:
                raise ServiceError("IDEMPOTENCY_CONFLICT")
            if receipt and receipt["expires_at"] > now and receipt["fingerprint"] != fp:
                raise ServiceError("IDEMPOTENCY_CONFLICT")
            if body["expectedInputToken"] != token: raise ServiceError("STALE_GENERATION_INPUT")
            if body["expectedSettingsVersion"] != sv: raise ServiceError("VERSION_CONFLICT")
            current_settings = self._settings(conn, trip_id, cv)[1]
            if any(conditions.get(k) is None for k in ("region", "startDate", "endDate", "expectedPeople", "budget", "transport")):
                raise ServiceError("CONDITIONS_INCOMPLETE")
            if not any(x["role"] == "OWNER" and x["requiredReady"] for x in personal): raise ServiceError("PREFERENCES_UNCONFIRMED")
            if not all(x["scheduleReady"] for x in personal): raise ServiceError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED")
            issues = self._confirmed_issues(conn, trip_id, catalog, current_settings)
            if issues: raise ServiceError(issues[0])
            if not self._common_ready(conditions, current_settings):
                raise ServiceError("LODGING_ANCHOR_REQUIRED" if conditions.get("lodging") else "FIXED_SCHEDULE_ANCHOR_REQUIRED")
            if old:
                if old["expires_at"] > now:
                    if old["fingerprint"] != fp: raise ServiceError("IDEMPOTENCY_CONFLICT")
                    if _json(old["input_stamp_json"]).get("inputToken") != token: raise ServiceError("STALE_GENERATION_INPUT")
                    job = self._job_public(old); conn.commit(); return job, 202
                if (old["fingerprint"] == fp and _json(old["input_stamp_json"]).get("inputToken") == token
                        and self._reusable_generation_job(conn, trip_id, old, token, catalog.digest, now)):
                    conn.execute(text("UPDATE generation_jobs SET expires_at=:e WHERE id=:id"),
                                 {"e": now + timedelta(hours=24), "id": old["id"]})
                    job = self._job_public(old); conn.commit(); return job, 202
                conn.execute(text("DELETE FROM generation_jobs WHERE id=:id"), {"id": old["id"]})
            if receipt and receipt["expires_at"] > now:
                if receipt["fingerprint"] != fp: raise ServiceError("IDEMPOTENCY_CONFLICT")
                pointer = _json(receipt["response_json"])
                canonical = conn.execute(text("SELECT * FROM generation_jobs WHERE id=:id AND trip_id=:t AND kind='GENERATE'"),
                                         {"id": pointer.get("coalescedJobId"), "t": trip_id}).mappings().first()
                if not canonical: raise ServiceError("STALE_GENERATION_INPUT")
                if _json(canonical["input_stamp_json"]).get("inputToken") != token:
                    raise ServiceError("STALE_GENERATION_INPUT")
                job = self._job_public(canonical); conn.commit(); return job, 200
            conn.execute(text("DELETE FROM api_idempotency WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at<=:n"),
                         {"s": session["id"], "p": path, "k": key, "n": _now()})
            now = _now()
            candidates = conn.execute(text("SELECT * FROM generation_jobs WHERE trip_id=:t AND kind='GENERATE' AND method='POST' AND path=:p AND ((status IN ('QUEUED','RUNNING') AND expires_at>:n AND lease_expires_at>:n) OR (status='SUCCEEDED' AND JSON_UNQUOTE(JSON_EXTRACT(input_stamp_json,'$.inputToken'))=:token)) ORDER BY created_at DESC FOR UPDATE"),
                                      {"t": trip_id, "p": path, "n": now, "token": token}).mappings().all()
            candidate = next((row for row in candidates if self._reusable_generation_job(
                conn, trip_id, row, token, catalog.digest, now)), None)
            if candidate:
                self._store_coalesced_key(conn, candidate, trip_id, member, session, path, key, fp, now)
                result = self._job_public(candidate)
                conn.commit()
                return result, 200
            active = conn.execute(text("SELECT id FROM generation_jobs WHERE trip_id=:t AND kind='GENERATE' AND status IN ('QUEUED','RUNNING') AND expires_at>:n AND lease_expires_at>:n LIMIT 1 FOR UPDATE"),
                                  {"t": trip_id, "n": now}).first()
            if active: raise ServiceError("GENERATION_IN_PROGRESS", retry=2)
            self._rate(session["id"], member["id"])
            job_id, lease_token = str(uuid.uuid4()), str(uuid.uuid4()); now = _now()
            conn.execute(text("INSERT INTO generation_jobs(id,trip_id,member_id,session_id,kind,method,path,idempotency_key,fingerprint,lease_token,lease_expires_at,status,phase,input_stamp_json,created_at,updated_at,expires_at) VALUES(:id,:t,:m,:s,'GENERATE','POST',:p,:k,:f,:token,:lease,'QUEUED','queued',:stamp,:n,:n,:exp)"), {"id": job_id, "t": trip_id, "m": member["id"], "s": session["id"], "p": path, "k": key, "f": fp, "token": lease_token, "lease": now+timedelta(seconds=240), "stamp": _canonical({"inputToken": token,"conditionsVersion":cv,"settingsVersion":sv,"catalogDigest":catalog.digest,"members":personal}), "n": now,"exp":now+timedelta(hours=24)})
            row = conn.execute(text("SELECT * FROM generation_jobs WHERE id=:id"), {"id": job_id}).mappings().first(); conn.commit(); return self._job_public(row), 202
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    async def run_job(self, job_id, request):
        token = None
        try:
            token = await run_in_threadpool(self._claim_job, job_id, request)
            if not token:
                return
            await asyncio.wait_for(run_in_threadpool(self._run_job_sync, job_id, request, token),
                                   timeout=float(self.app.state.generation_job_timeout))
        except asyncio.TimeoutError:
            if token: await run_in_threadpool(self._fail_job, job_id, "AI_TIMEOUT", token)
        except ServiceError as exc:
            if token: await run_in_threadpool(self._fail_job, job_id, exc.code, token)
        except Exception as exc:
            code = getattr(exc, "code", None) or (str(exc) if isinstance(exc, ValueError) else None)
            mapping = {"AI_INVALID_DRAFT": "AI_INVALID_RESPONSE", "INVALID_NORMALIZATION_RESULT": "AI_INVALID_RESPONSE",
                       "SCHEDULE_WINDOW_INFEASIBLE": "SCHEDULE_WINDOW_INFEASIBLE",
                       "INVALID_CANDIDATE_SELECTION_INPUT": "AI_INVALID_RESPONSE", "STALE_REVIEW": "REVIEW_REQUIRED",
                       "CANDIDATE_REVIEW_REQUIRED": "REVIEW_REQUIRED", "AI_DRAFT_FAILED": "AI_UNAVAILABLE",
                       "AI_TIMEOUT": "AI_TIMEOUT", "AI_UNAVAILABLE": "AI_UNAVAILABLE",
                       "STALE_PLACE_CATALOG": "STALE_PLACE_CATALOG", "STALE_GENERATION_INPUT": "STALE_GENERATION_INPUT",
                       "SOURCE_NO_LONGER_ACCESSIBLE": "STALE_GENERATION_INPUT",
                       "REQUIRED_PREFERENCE_UNSUPPORTED": "REQUIRED_PREFERENCE_UNSUPPORTED",
                       "REQUIRED_PREFERENCE_UNSATISFIED": "REQUIRED_PREFERENCE_UNSATISFIED"}
            if token: await run_in_threadpool(self._fail_job, job_id, mapping.get(code, "GENERATION_UNAVAILABLE"), token)

    def _claim_job(self, job_id, request):
        job_id = str(uuid.UUID(job_id)); conn = self._conn()
        try:
            row = conn.execute(text("SELECT trip_id FROM generation_jobs WHERE id=:id AND kind='GENERATE'"), {"id": job_id}).mappings().first()
            if not row: conn.rollback(); return None
            self._auth(conn, request, row["trip_id"], owner=True)
            candidate = conn.execute(text("SELECT lease_token,lease_expires_at FROM generation_jobs WHERE id=:id AND kind='GENERATE' AND status='QUEUED' FOR UPDATE"), {"id": job_id}).mappings().first()
            if not candidate or candidate["lease_expires_at"] <= _now():
                conn.rollback(); return None
            token = candidate["lease_token"]
            conn.execute(text("UPDATE generation_jobs SET status='RUNNING',phase='selecting',lease_expires_at=:e WHERE id=:id AND status='QUEUED' AND lease_token=:token AND lease_expires_at>:n"), {"e": _now()+timedelta(seconds=240), "id": job_id, "token": token, "n": _now()})
            if conn.execute(text("SELECT ROW_COUNT()")).scalar() != 1:
                conn.rollback(); return None
            conn.commit(); return token
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def _run_job_sync(self, job_id, request, token):
        from .generation_draft import generate_candidate_draft, GenerationDraftError
        from .candidate_selection import build_confirmed_candidate_selection, build_supported_tags
        import asyncio

        job_id = str(uuid.UUID(job_id)); deadline = time.monotonic() + float(self.app.state.generation_job_timeout); conn = self._conn()
        try:
            before = conn.execute(text("SELECT trip_id FROM generation_jobs WHERE id=:id AND kind='GENERATE'"), {"id": job_id}).mappings().first()
            if not before: conn.rollback(); return
            _, _, actor = self._auth(conn, request, before["trip_id"], owner=True)
            row = conn.execute(text("SELECT * FROM generation_jobs WHERE id=:id AND kind='GENERATE' AND status='RUNNING' AND lease_token=:token FOR UPDATE"), {"id": job_id, "token": token}).mappings().first()
            if not row:
                conn.rollback(); return
            if row["lease_expires_at"] <= _now():
                conn.execute(text("UPDATE generation_jobs SET status='FAILED',phase='failed',error_code='PROCESS_INTERRUPTED' WHERE id=:id"), {"id": job_id}); conn.commit(); return
            trip_id, member_id, token = row["trip_id"], row["member_id"], row["lease_token"]
            stamp = _json(row["input_stamp_json"])
            if actor["id"] != member_id: raise ServiceError("NOT_AUTHORIZED", 403)
            snapshot = self._snapshot(conn, request, trip_id)
            settings_version, gen_settings = self._settings(conn, trip_id, snapshot.as_dict()["conditionsVersion"])
            catalog = place_catalog.load_place_catalog(conn)
            input_token, _, member_stamps = self._source_token(conn, trip_id, snapshot.as_dict()["conditionsVersion"], catalog.digest, settings_version)
            if input_token != stamp.get("inputToken") or catalog.digest != stamp.get("catalogDigest"):
                raise ServiceError("STALE_GENERATION_INPUT")
            doc = snapshot.as_dict()
            remapped_prefs = doc["confirmedPreferences"]
            issues = self._confirmed_issues(conn, trip_id, catalog, gen_settings)
            if issues: raise ServiceError(issues[0])
            route_preferences = compile_route_preferences(doc, catalog)
            confirmed = combine_confirmed_evidence(remapped_prefs, build_supported_tags(catalog), route_preferences)
            gen_settings = {**gen_settings, "confirmedScheduleAnchors": confirmed_preferences.group_schedule_anchors(
                remapped_prefs, gen_settings, doc.get("_identity")), "_routePreferences": route_preferences}
            hard_issue = next((x["code"] for x in confirmed["issues"] if x["code"].startswith("REQUIRED_")), None)
            if hard_issue: raise ServiceError(hard_issue)
            selection = build_confirmed_candidate_selection(snapshot, catalog, confirmed, gen_settings,
                                                            route_preferences=route_preferences)
            if not selection.ready_for_draft:
                issues = selection.as_dict()["unresolvedRequired"]
                code = next((x for x in issues if x.startswith("REQUIRED_PREFERENCE_")), "REQUIRED_PREFERENCE_UNSATISFIED")
                raise ServiceError(code)
            conn.execute(text("UPDATE generation_jobs SET phase='generating' WHERE id=:id AND lease_token=:token"), {"id": job_id, "token": token}); conn.commit()
        except Exception:
            conn.rollback(); conn.close(); raise
        conn.close()

        # Route/AI work uses only detached validated snapshots and no DB locks.
        async def callbacks():
            def recapture():
                c = self._conn()
                try: return self._snapshot(c, request, trip_id)
                finally: c.close()
            def reload_catalog(): return self._catalog()
            return await generate_candidate_draft(snapshot, None, catalog, None, recapture, reload_catalog,
                                                   confirmed_evidence=confirmed, common_settings=gen_settings,
                                                   drafter=self.app.state.generation_drafter, timeout_seconds=60)
        draft = asyncio.run(callbacks()).as_dict()
        from .confirmed_schedule import arrange_overnight_stops
        try:
            draft, gen_settings = arrange_overnight_stops(draft, gen_settings)
        except ValueError:
            raise ServiceError("SCHEDULE_ITEM_CLARIFICATION_REQUIRED") from None
        if time.monotonic() >= deadline: raise ServiceError("AI_TIMEOUT", 504)
        conn = self._conn()
        try:
            conn.execute(text("UPDATE generation_jobs SET phase='validating' WHERE id=:id AND lease_token=:token AND status='RUNNING'"), {"id": job_id, "token": token}); conn.commit()
        finally: conn.close()
        # Root's validator and router own all route/time checks and public node IDs.
        from .day_schedule import build_scheduled_plan
        from .place_semantics import build_place_semantics
        semantic_context = build_place_semantics(
            catalog, draft.get("optimizationCandidates", []),
            provider=getattr(self.app.state, "generation_embedder", None),
            deadline=min(deadline, time.monotonic() + 8.0))
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ServiceError("AI_TIMEOUT", 504)
        router = self.app.state.generation_router
        document = build_scheduled_plan(draft, catalog, snapshot.as_dict()["conditions"], legacy_preferences(remapped_prefs, route_preferences),
                                       gen_settings, router, timeout_seconds=min(90, remaining),
                                       semantic_context=semantic_context)
        if (any(item.get("code") in {"REQUIRED_PREFERENCE_UNSATISFIED", "REQUIRED_PREFERENCE_UNSUPPORTED"}
                for item in document["validation"]["violations"])
                or "REQUIRED_PREFERENCE_UNVERIFIED" in document["validation"]["warnings"]):
            raise ServiceError("REQUIRED_PREFERENCE_UNSATISFIED")
        if time.monotonic() >= deadline: raise ServiceError("AI_TIMEOUT", 504)
        conn = self._conn()
        try:
            conn.execute(text("UPDATE generation_jobs SET phase='persisting' WHERE id=:id AND lease_token=:token AND status='RUNNING' AND lease_expires_at>:n"), {"id": job_id, "token": token, "n": _now()})
            if conn.execute(text("SELECT ROW_COUNT()")).scalar() != 1: raise ServiceError("STALE_GENERATION_INPUT")
            conn.commit()
        finally: conn.close()
        conn = self._conn()
        try:
            _, _, actor = self._auth(conn, request, trip_id, owner=True)
            latest = self._snapshot(conn, request, trip_id)
            current_catalog = place_catalog.load_place_catalog(conn, lock=True)
            sv, _ = self._settings(conn, trip_id, latest.as_dict()["conditionsVersion"])
            current_token, _, _ = self._source_token(conn, trip_id, latest.as_dict()["conditionsVersion"], current_catalog.digest, sv)
            live_job = conn.execute(text("SELECT lease_token,status,lease_expires_at FROM generation_jobs WHERE id=:id FOR UPDATE"), {"id": job_id}).mappings().first()
            if (latest.fingerprint != snapshot.fingerprint or current_token != stamp["inputToken"] or current_catalog.digest != catalog.digest
                    or not live_job or live_job["lease_token"] != token or live_job["status"] != "RUNNING" or live_job["lease_expires_at"] <= _now()):
                raise ServiceError("STALE_GENERATION_INPUT")
            from . import plan_store
            if time.monotonic() >= deadline: raise ServiceError("AI_TIMEOUT", 504)
            source_stamp = {"inputToken": current_token, "conditionsVersion": latest.as_dict()["conditionsVersion"],
                            "settingsVersion": sv, "catalogDigest": current_catalog.digest,
                            "memberVersions": member_stamps}
            record = plan_store.create_generated_plan(conn, trip_id, actor["id"], snapshot, document, source_stamp)
            if time.monotonic() >= deadline: raise ServiceError("AI_TIMEOUT", 504)
            pub = record["public"]
            result = {"id": job_id, "kind": "GENERATE", "status": "SUCCEEDED", "phase": "complete",
                      "planId": pub["planId"], "versionId": pub["versionId"], "errorCode": None,
                      "createdAt": None, "updatedAt": None}
            conn.execute(text("UPDATE generation_jobs SET status='SUCCEEDED',phase='complete',plan_id=:p,version_id=:v,error_code=NULL,lease_expires_at=:n,response_json=:r,expires_at=:e WHERE id=:id AND lease_token=:token AND status='RUNNING' AND lease_expires_at>:n"), {"p": pub["planId"], "v": pub["versionId"], "n": _now(), "r": _canonical(result), "e": _now()+timedelta(hours=24), "id": job_id, "token": token})
            if conn.execute(text("SELECT ROW_COUNT()")).scalar() != 1: raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
            conn.commit()
        except Exception:
            conn.rollback(); conn.close(); raise
        conn.close()

    @staticmethod
    def _common_review(conditions, settings):
        result = []
        for field in ("lodging", "fixedSchedule"):
            source = conditions.get(field)
            if not source: continue
            review = next((x for x in settings.get("reviewedCommonFields", []) if x.get("field") == field and x.get("reviewed") is True), None)
            if not review or not review.get("anchors"): raise ServiceError("REVIEW_REQUIRED")
            result.append({"field": field, "text": source, "fixedVisits": [{k: item[k] for k in ("date", "contentid", "position")} for item in review["anchors"]]})
        return result

    def _fail_job(self, job_id, code, token):
        conn = self._conn()
        try:
            safe = code if code in {"SCHEDULE_WINDOW_INFEASIBLE", "STALE_GENERATION_INPUT", "STALE_PLACE_CATALOG", "REVIEW_REQUIRED", "PROCESS_INTERRUPTED", "AI_TIMEOUT", "AI_UNAVAILABLE", "AI_INVALID_RESPONSE", "VALIDATION_FAILED", "REQUIRED_PREFERENCE_UNSUPPORTED", "REQUIRED_PREFERENCE_UNSATISFIED", "PREFERENCES_UNCONFIRMED", "SCHEDULE_ITEM_CLARIFICATION_REQUIRED", "LODGING_ANCHOR_REQUIRED", "FIXED_SCHEDULE_ANCHOR_REQUIRED"} else "GENERATION_UNAVAILABLE"
            status = "STALE" if safe.startswith("STALE_") else "FAILED"
            conn.execute(text("UPDATE generation_jobs SET status=:status,phase='failed',error_code=:code,lease_expires_at=:n WHERE id=:id AND status='RUNNING' AND lease_token=:token"), {"status": status, "code": safe, "n": _now(), "id": job_id, "token": token}); conn.commit()
        finally: conn.close()

    def _mark_job_unavailable(self, job_id):
        row_conn = self._conn()
        try:
            token = row_conn.execute(text("SELECT lease_token FROM generation_jobs WHERE id=:id"), {"id": job_id}).scalar()
            row_conn.commit()
        finally: row_conn.close()
        if token: self._fail_job(job_id, "GENERATION_UNAVAILABLE", token)

    def get_job(self, request, trip_id, job_id):
        trip_id, job_id = str(uuid.UUID(trip_id)), str(uuid.UUID(job_id)); conn = self._conn()
        try:
            _, _, actor = self._auth(conn, request, trip_id)
            row = conn.execute(text("SELECT * FROM generation_jobs WHERE id=:id AND trip_id=:t AND kind='GENERATE'"), {"id": job_id, "t": trip_id}).mappings().first()
            if not row: raise ServiceError("TRIP_NOT_ACCESSIBLE", 404)
            if row["status"] in {"QUEUED", "RUNNING"} and row["lease_expires_at"] <= _now():
                conn.execute(text("UPDATE generation_jobs SET status='FAILED',phase='failed',error_code='PROCESS_INTERRUPTED' WHERE id=:id AND status IN ('QUEUED','RUNNING')"), {"id": job_id}); row = dict(row); row.update(status="FAILED", phase="failed", error_code="PROCESS_INTERRUPTED")
            result = self._job_public(row); conn.commit(); return result
        except ServiceError: conn.rollback(); raise
        finally: conn.close()

    def get_plan(self, request, trip_id, plan_id):
        from . import plan_store
        trip_id, plan_id = str(uuid.UUID(trip_id)), str(uuid.UUID(plan_id)); conn = self._conn()
        try:
            _, _, actor = self._auth(conn, request, trip_id)
            record = plan_store.get_plan_record(conn, trip_id, plan_id)
            if not record: raise ServiceError("TRIP_NOT_ACCESSIBLE", 404)
            catalog = place_catalog.load_place_catalog(conn); _, cv = preparation._conditions(conn, trip_id)
            sv, _ = self._settings(conn, trip_id, cv); token, _, _ = self._source_token(conn, trip_id, cv, catalog.digest, sv)
            stamp = record.get("sourceStamp") or {}
            stale = (stamp.get("inputToken") != token or stamp.get("catalogDigest") != catalog.digest)
            document = record["public"].get("document") or {}
            violated = isinstance(document.get("validation"), dict) and document["validation"].get("status") == "violated"
            can_save = actor["role"] == "OWNER" and record["public"]["status"] == "DRAFT" and not stale and not violated
            conn.commit(); return {**record["public"], "stale": stale, "canSave": can_save}
        except ServiceError: conn.rollback(); raise
        except Exception: conn.rollback(); raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()

    def current_plan_for_canvas(self, conn, request, trip_id, plan_id, version_id):
        """Return a source-current active plan while the caller holds this connection.

        Canvas import calls this inside its own save transaction so authorization,
        plan version, catalog digest, and source token are checked atomically with
        the canvas CAS write.
        """
        self._auth(conn, request, trip_id, owner=True)
        plan = conn.execute(text("SELECT id,COALESCE(active_draft_id,current_version_id) AS current_version_id FROM plans WHERE id=:p AND trip_id=:t FOR UPDATE"),
                            {"p": plan_id, "t": trip_id}).mappings().first()
        if not plan: raise ServiceError("PLAN_NOT_FOUND", 404)
        locked_version = conn.execute(text("SELECT id FROM plan_versions WHERE id=:v AND plan_id=:p AND trip_id=:t FOR UPDATE"),
                                      {"v": version_id, "p": plan_id, "t": trip_id}).first()
        if not locked_version or plan["current_version_id"] != version_id:
            raise ServiceError("PLAN_STALE")
        from . import plan_store
        record = plan_store.get_plan_record(conn, trip_id, plan_id)
        if not record or record["public"]["versionId"] != version_id:
            raise ServiceError("PLAN_STALE")
        catalog = place_catalog.load_place_catalog(conn, lock=True)
        _, cv = preparation._conditions(conn, trip_id)
        settings_version, _ = self._settings(conn, trip_id, cv)
        current, _, _ = self._source_token(conn, trip_id, cv, catalog.digest, settings_version)
        if record.get("sourceStamp", {}).get("inputToken") != current or record.get("sourceStamp", {}).get("catalogDigest") != catalog.digest:
            raise ServiceError("PLAN_STALE")
        if record["public"].get("status") not in {"DRAFT", "SAVED"}:
            raise ServiceError("PLAN_STALE")
        return record["public"]

    def save_plan(self, request, trip_id, plan_id, body, key):
        from . import plan_store
        trip_id, plan_id = str(uuid.UUID(trip_id)), str(uuid.UUID(plan_id)); conn = self._conn()
        try:
            session, _, member = self._auth(conn, request, trip_id, owner=True)
            if (not isinstance(body, dict) or set(body) != {"versionId", "expectedLockVersion", "acknowledgeUnverified"}
                    or not isinstance(body["acknowledgeUnverified"], bool) or isinstance(body["expectedLockVersion"], bool)
                    or not isinstance(body["expectedLockVersion"], int) or body["expectedLockVersion"] < 1): raise ServiceError("VALIDATION_ERROR", 400)
            try: version_id = str(uuid.UUID(body["versionId"]))
            except (ValueError, TypeError, AttributeError): raise ServiceError("VALIDATION_ERROR", 400) from None
            catalog = place_catalog.load_place_catalog(conn, lock=True)
            _, cv = preparation._conditions(conn, trip_id)
            sv, _ = self._settings(conn, trip_id, cv)
            current, _, _ = self._source_token(conn, trip_id, cv, catalog.digest, sv)
            path = f"/api/v1/trips/{trip_id}/plans/{plan_id}/save"
            fingerprint = hashlib.sha256(_canonical({"planId": plan_id, **body}).encode()).hexdigest()
            old = conn.execute(text("SELECT * FROM generation_jobs WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at>:n FOR UPDATE"), {"s": session["id"], "p": path, "k": key, "n": _now()}).mappings().first()
            if old:
                if old["fingerprint"] != fingerprint: raise ServiceError("IDEMPOTENCY_CONFLICT")
                if _json(old["input_stamp_json"]).get("inputToken") != current: raise ServiceError("STALE_GENERATION_INPUT")
                if old["status"] != "SUCCEEDED": raise ServiceError("GENERATION_IN_PROGRESS", retry=1)
                payload = _json(old["response_json"]); conn.commit(); return payload
            conn.execute(text("DELETE FROM generation_jobs WHERE session_id=:s AND method='POST' AND path=:p AND idempotency_key=:k AND expires_at<=:n"), {"s": session["id"], "p": path, "k": key, "n": _now()})
            record = plan_store.save_generated_plan(conn, trip_id, plan_id, version_id, member["id"], body["expectedLockVersion"], body["acknowledgeUnverified"], current)
            result = {**record["public"], "stale": False, "canSave": False}
            now, job_id = _now(), str(uuid.uuid4())
            conn.execute(text("INSERT INTO generation_jobs(id,trip_id,member_id,session_id,kind,method,path,idempotency_key,fingerprint,lease_token,lease_expires_at,status,phase,input_stamp_json,response_json,plan_id,version_id,created_at,updated_at,expires_at) VALUES(:id,:t,:m,:s,'SAVE_PLAN','POST',:p,:k,:f,:token,:lease,'SUCCEEDED','complete',:stamp,:r,:plan,:version,:n,:n,:e)"), {"id": job_id,"t":trip_id,"m":member["id"],"s":session["id"],"p":path,"k":key,"f":fingerprint,"token":str(uuid.uuid4()),"lease":now+timedelta(seconds=1),"n":now,"stamp":_canonical({"inputToken":current,"conditionsVersion":cv,"settingsVersion":sv,"catalogDigest":catalog.digest}),"r":_canonical(result),"plan":plan_id,"version":body["versionId"],"e":now+timedelta(hours=24)})
            conn.commit(); return result
        except ServiceError: conn.rollback(); raise
        except Exception as exc:
            from .plan_store import PlanStoreError
            conn.rollback()
            if isinstance(exc, PlanStoreError): raise ServiceError(exc.code, 409) from None
            raise ServiceError("GENERATION_UNAVAILABLE", 503) from None
        finally: conn.close()
