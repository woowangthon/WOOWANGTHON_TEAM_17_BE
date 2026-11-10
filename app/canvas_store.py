"""SQLAlchemy Core persistence for canvas documents and request leases."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text


class CanvasStoreError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value):
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


class CanvasStore:
    def __init__(self, engine):
        self.engine = engine

    def load_document(self, conn, trip_id: str):
        row = conn.execute(text("SELECT revision,schema_version,document_json,source_plan_id,source_version_id,updated_at FROM trip_canvas_documents WHERE trip_id=:t"), {"t": trip_id}).mappings().first()
        if not row:
            return None
        return {"revision": int(row["revision"]), "document": _json(row["document_json"]),
                "sourcePlan": ({"planId": row["source_plan_id"], "versionId": row["source_version_id"]}
                               if row["source_plan_id"] and row["source_version_id"] else None),
                "updatedAt": row["updated_at"]}

    def save_document(self, conn, trip_id: str, expected_revision: int, document: dict, *, source_plan=None):
        existing = conn.execute(text("SELECT revision,source_plan_id,source_version_id FROM trip_canvas_documents WHERE trip_id=:t"), {"t": trip_id}).mappings().first()
        if existing and source_plan is None and existing["source_plan_id"] and existing["source_version_id"]:
            source_plan = {"planId": existing["source_plan_id"], "versionId": existing["source_version_id"]}
        now = _now()
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        if existing is None:
            if expected_revision != 0:
                raise CanvasStoreError("VERSION_CONFLICT")
            try:
                conn.execute(text("INSERT INTO trip_canvas_documents(trip_id,revision,schema_version,document_json,source_plan_id,source_version_id,created_at,updated_at) VALUES(:t,1,3,:d,:p,:v,:n,:n)"),
                             {"t": trip_id, "d": encoded, "p": source_plan["planId"] if source_plan else None,
                              "v": source_plan["versionId"] if source_plan else None, "n": now})
            except Exception:
                raise CanvasStoreError("VERSION_CONFLICT") from None
            revision = 1
        else:
            updated = conn.execute(text("UPDATE trip_canvas_documents SET revision=revision+1,schema_version=3,document_json=:d,source_plan_id=:p,source_version_id=:v,updated_at=:n WHERE trip_id=:t AND revision=:r"),
                                   {"d": encoded, "p": source_plan["planId"] if source_plan else None,
                                    "v": source_plan["versionId"] if source_plan else None, "n": now, "t": trip_id, "r": expected_revision})
            if updated.rowcount != 1:
                raise CanvasStoreError("VERSION_CONFLICT")
            revision = expected_revision + 1
        return {"revision": revision, "document": document, "sourcePlan": source_plan, "updatedAt": now}

    def reserve_request(self, conn, *, session_id: str, trip_id: str, member_id: str, kind: str,
                        method: str, path: str, key: str, fingerprint: str, lease_token: str,
                        lease_seconds: int = 45):
        now = _now()
        row = conn.execute(text("SELECT trip_id,member_id,kind,fingerprint,status,lease_expires_at,result_json FROM trip_canvas_requests WHERE session_id=:s AND method=:m AND path=:p AND idempotency_key=:k"),
                           {"s": session_id, "m": method, "p": path, "k": key}).mappings().first()
        if row:
            if row["trip_id"] != trip_id or row["member_id"] != member_id or row["kind"] != kind:
                raise CanvasStoreError("IDEMPOTENCY_KEY_REUSED")
            if row["fingerprint"] != fingerprint:
                raise CanvasStoreError("IDEMPOTENCY_KEY_REUSED")
            if row["status"] == "COMPLETE":
                return {"replay": _json(row["result_json"])}
            if row["lease_expires_at"] and _datetime(row["lease_expires_at"]) > now:
                raise CanvasStoreError("REQUEST_IN_PROGRESS")
            # An expired lease is an unknown provider/save outcome. It is fenced
            # permanently; paid recommendations are never automatically rerun.
            raise CanvasStoreError("REQUEST_IN_PROGRESS")
        conn.execute(text("INSERT INTO trip_canvas_requests(session_id,trip_id,member_id,kind,method,path,idempotency_key,fingerprint,status,lease_token,lease_expires_at,result_json,created_at,updated_at,expires_at) VALUES(:s,:t,:m,:kind,:method,:path,:key,:fingerprint,'PENDING',:token,:lease,NULL,:n,:n,:expiry)"),
                     {"s": session_id, "t": trip_id, "m": member_id, "kind": kind, "method": method,
                      "path": path, "key": key, "fingerprint": fingerprint, "token": lease_token,
                      "lease": now + timedelta(seconds=lease_seconds), "n": now, "expiry": now + timedelta(hours=24)})
        return {"leaseToken": lease_token}

    def complete_request(self, conn, *, session_id: str, method: str, path: str, key: str,
                         lease_token: str, result: Any):
        now = _now()
        updated = conn.execute(text("UPDATE trip_canvas_requests SET status='COMPLETE',result_json=:result,lease_expires_at=NULL,updated_at=:n,expires_at=:expiry WHERE session_id=:s AND method=:m AND path=:p AND idempotency_key=:k AND lease_token=:token AND status='PENDING' AND lease_expires_at>=:n"),
                               {"result": json.dumps(result, ensure_ascii=False, separators=(",", ":")), "n": now,
                                "expiry": now + timedelta(hours=24), "s": session_id, "m": method,
                                "p": path, "k": key, "token": lease_token})
        if updated.rowcount != 1:
            raise CanvasStoreError("REQUEST_IN_PROGRESS")

    def abandon_request(self, conn, *, session_id: str, method: str, path: str, key: str, lease_token: str):
        # Leave an unreplayable fenced row so an unknown paid result cannot be charged twice.
        conn.execute(text("UPDATE trip_canvas_requests SET lease_expires_at=:n,updated_at=:n WHERE session_id=:s AND method=:m AND path=:p AND idempotency_key=:k AND lease_token=:token AND status='PENDING'"),
                     {"n": _now(), "s": session_id, "m": method, "p": path, "k": key, "token": lease_token})
