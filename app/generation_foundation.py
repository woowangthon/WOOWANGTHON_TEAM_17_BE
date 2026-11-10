"""Private, read-only foundation for generation requests.

This module deliberately does not create routes or persist generation output.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import text

from app import preparation


class GenerationFoundationError(Exception):
    """Safe internal failure; messages never contain database or user values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    return preparation._json(value)


@dataclass(frozen=True, repr=False)
class ConfirmedPreparationSnapshot:
    """Immutable private capture. Public accessors return detached copies."""

    _document: str = field(repr=False)
    fingerprint: str

    def __repr__(self) -> str:
        return "ConfirmedPreparationSnapshot(<private>)"

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self._document)

    def confirmed_preferences(self) -> list[dict[str, Any]]:
        return json.loads(self._document)["confirmedPreferences"]


def _confirmed_doc(row: Any, conditions_version: int) -> bool:
    if not row or row["confirmed_at"] is None:
        return False
    confirmed_cv, confirmed_pv = row["confirmed_conditions_version"], row["confirmed_preference_version"]
    if confirmed_cv is None or confirmed_pv is None:
        return False
    return int(confirmed_cv) == conditions_version and int(confirmed_pv) == int(row["preference_version"])


def _clean_preference(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) not in (set(preparation.PERSONAL_KEYS), set(preparation.PERSONAL_KEYS) | {"scheduleItems"}):
        raise GenerationFoundationError("INVALID_PREPARATION_DOCUMENT")
    try:
        clean = preparation.validate_draft({**raw, "scheduleItems": raw.get("scheduleItems", [])})
    except preparation.PrepFailure:
        raise GenerationFoundationError("INVALID_PREPARATION_DOCUMENT") from None
    # Original prose is validated as stored but never copied into the capture.
    if not clean["originalText"]:
        raise GenerationFoundationError("PREPARATION_NOT_READY")
    return {key: clean[key] for key in ("likes", "avoid", "avoidImportance", "food", "foodImportance", "walking", "scheduleItems")}


def _capture(conn: Any, trip_id: str, authorize: Callable) -> ConfirmedPreparationSnapshot:
    try:
        session, trip, actor = authorize(conn, trip_id)
        if not session or not trip or not actor:
            raise GenerationFoundationError("NOT_AUTHORIZED")
        if str(trip.get("id")) != str(trip_id):
            raise GenerationFoundationError("NOT_AUTHORIZED")
        if trip.get("status") != "OPEN" or actor.get("status") != "ACTIVE" or actor.get("role") != "OWNER":
            raise GenerationFoundationError("NOT_AUTHORIZED")

        # Match preparation's locking order: trip is already locked by authorize;
        # read common conditions, then active membership rows, then documents.
        c_row = conn.execute(text("SELECT conditions_json,conditions_version FROM trip_preparation_documents WHERE trip_id=:t"), {"t": trip_id}).mappings().first()
        if not c_row:
            raise GenerationFoundationError("PREPARATION_NOT_READY")
        conditions = _json_value(c_row["conditions_json"])
        try:
            conditions = preparation.validate_conditions(conditions)
        except preparation.PrepFailure:
            raise GenerationFoundationError("INVALID_PREPARATION_DOCUMENT") from None
        cv = int(c_row["conditions_version"])

        members = conn.execute(text("SELECT id,role,status FROM trip_members WHERE trip_id=:t AND status='ACTIVE' ORDER BY CASE WHEN role='OWNER' THEN 0 ELSE 1 END,created_at,id FOR UPDATE"), {"t": trip_id}).mappings().all()
        if not members or any(m["status"] != "ACTIVE" for m in members):
            raise GenerationFoundationError("PREPARATION_NOT_READY")
        if not any(str(member["id"]) == str(actor["id"]) and member["role"] == "OWNER" for member in members):
            raise GenerationFoundationError("NOT_AUTHORIZED")
        docs = conn.execute(text("SELECT member_id,draft_json,preference_version,confirmed_conditions_version,confirmed_preference_version,confirmed_at FROM member_preparation_documents WHERE trip_id=:t FOR UPDATE"), {"t": trip_id}).mappings().all()
        by_member = {str(d["member_id"]): d for d in docs}
        active_count = len(members)
        required = ("region", "startDate", "endDate", "expectedPeople", "budget", "transport")
        if any(conditions.get(k) is None for k in required) or conditions["expectedPeople"] != active_count:
            raise GenerationFoundationError("PREPARATION_NOT_READY")

        prefs = []
        versions = []
        for index, member in enumerate(members, start=1):
            mid = str(member["id"])
            doc = by_member.get(mid)
            if not _confirmed_doc(doc, cv):
                raise GenerationFoundationError("PREPARATION_NOT_READY")
            pref = _clean_preference(_json_value(doc["draft_json"]))
            if not pref["likes"] or pref["walking"] not in preparation.WALKING:
                raise GenerationFoundationError("PREPARATION_NOT_READY")
            prefs.append({"memberRef": f"p{index}", **pref})
            versions.append({"memberId": mid, "role": str(member["role"]), "preferenceVersion": int(doc["preference_version"]),
                             "confirmedConditionsVersion": int(doc["confirmed_conditions_version"]),
                             "confirmedPreferenceVersion": int(doc["confirmed_preference_version"])})

        stamp = {"tripId": str(trip["id"]), "sessionId": str(session["id"]), "actorMemberId": str(actor["id"]),
                 "actorRole": str(actor["role"]), "conditionsVersion": cv,
                 "conditions": conditions, "members": versions}
        public = {"conditions": conditions, "conditionsVersion": cv, "confirmedPreferences": prefs}
        # Include sanitized preferences as well as versions, so out-of-band edits
        # cannot bypass stale-input detection by leaving version counters intact.
        fingerprint = hashlib.sha256(_canonical({**stamp, "confirmedPreferences": prefs}).encode("utf-8")).hexdigest()
        return ConfirmedPreparationSnapshot(_canonical({**public, "_identity": stamp}), fingerprint)
    except GenerationFoundationError:
        raise
    except Exception:
        # SQL/driver exception details can contain private values.
        raise GenerationFoundationError("PREPARATION_CAPTURE_FAILED") from None


def capture_confirmed_preparation(engine: Any, trip_id: str, authorize: Callable) -> ConfirmedPreparationSnapshot:
    """Capture confirmed preparation while keeping DB work inside a short connection."""
    conn = None
    try:
        conn = engine.connect()
        with conn.begin():
            result = _capture(conn, trip_id, authorize)
        return result
    except GenerationFoundationError:
        raise
    except Exception:
        raise GenerationFoundationError("PREPARATION_CAPTURE_FAILED") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _default_normalizer(confirmed_preferences: list[dict[str, Any]], supported_tags: list[str]) -> dict[str, Any]:
    try:
        from chungbuk_trip_ai import normalize_confirmed_preferences
    except Exception:
        raise GenerationFoundationError("AI_UNAVAILABLE") from None
    try:
        return normalize_confirmed_preferences(confirmed_preferences=confirmed_preferences, supported_tags=supported_tags)
    except Exception as exc:
        # Keep provider internals out of errors and logs; preserve only failure class.
        raise GenerationFoundationError("AI_NORMALIZATION_FAILED") from None


async def normalize_snapshot(snapshot: ConfirmedPreparationSnapshot, catalog: Any,
                             recapture: Callable[[], ConfirmedPreparationSnapshot],
                             normalizer: Callable | None = None, timeout_seconds: float = 60) -> dict[str, Any]:
    """Run provider work without a DB connection, then reject stale captures."""
    fn = normalizer or _default_normalizer
    try:
        normalized = await asyncio.wait_for(
            asyncio.to_thread(fn, snapshot.confirmed_preferences(), list(catalog.supported_tags)),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise GenerationFoundationError("AI_TIMEOUT") from None
    except GenerationFoundationError:
        raise
    except Exception:
        raise GenerationFoundationError("AI_NORMALIZATION_FAILED") from None
    try:
        # Detach provider-owned state; the orchestrator can validate its shape.
        normalized = json.loads(json.dumps(copy.deepcopy(normalized), ensure_ascii=False, allow_nan=False))
    except Exception:
        raise GenerationFoundationError("AI_INVALID_RESPONSE") from None
    latest = await asyncio.to_thread(recapture)
    if latest.fingerprint != snapshot.fingerprint:
        raise GenerationFoundationError("STALE_GENERATION_INPUT")
    return {"sourceFingerprint": snapshot.fingerprint, "catalogDigest": catalog.digest,
            "normalization": normalized}
